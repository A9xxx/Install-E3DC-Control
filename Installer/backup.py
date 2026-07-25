import os
import datetime
import stat
import subprocess
import sys
from pathlib import Path

# Standard-Ausgabe auf UTF-8 erzwingen
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
    else:
        sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from .core import register_command
from .backup_retention import UPDATE_BACKUP_KEEP_COUNT, delete_verified_backup, prune_install_backups
from .backup_integrity import (
    BackupIntegrityError,
    LEGACY_ML_MODEL,
    PRIVATE_ML_ROOT,
    PersistentSource,
    SYSTEMD_ADMIN_UNIT_DIR,
    SYSTEM_BACKUP_KIND,
    _lexical_absolute,
    build_systemd_mask_state_contract,
    copy_persistent_sources,
    default_backup_root,
    finalize_backup,
    normalize_private_ml_lock_metadata,
    restore_persistent_payload,
    secure_backup_tree,
    validate_private_ml_store,
    verify_backup,
    verify_systemd_mask_state_contract,
)
from .installer_config import get_install_path, get_user_ids, get_www_data_gid, load_config
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning
from .service_catalog import allowed_services

INSTALL_PATH = get_install_path()
backup_logger = get_or_create_logger("backup")
SYSTEMD_UNIT_DIRS = (
    SYSTEMD_ADMIN_UNIT_DIR,
    Path("/usr/lib/systemd/system"),
    Path("/lib/systemd/system"),
)


def _prepare_private_ml_store_for_backup(
    model_root=PRIVATE_ML_ROOT,
    *,
    expected_uid=None,
    expected_gid=None,
):
    """Normalisiert nur einen bereits als sicher reparierbar geprüften ML-Lock."""

    if expected_uid is None or expected_gid is None:
        install_uid, install_gid = get_user_ids()
        expected_uid = install_uid if expected_uid is None else expected_uid
        expected_gid = install_gid if expected_gid is None else expected_gid

    preflight = validate_private_ml_store(
        model_root,
        expected_uid=int(expected_uid),
        allow_missing=True,
        allow_repairable_lock=True,
    )
    if preflight.get("repairable_lock"):
        print("→ Normalisiere sicher gebundene ML-Sperrdatei vor dem Backup…")
        result = normalize_private_ml_lock_metadata(
            model_root,
            expected_uid=int(expected_uid),
            expected_gid=int(expected_gid),
        )
        if result.get("state") != "ready":
            raise BackupIntegrityError(
                "ML-Sperrdatei wurde vor dem Backup nicht eindeutig normalisiert"
            )
        backup_logger.info(
            "Sicher gebundene ML-Sperrdatei vor dem Backup normalisiert "
            "(Metadaten geaendert=%s).",
            bool(result.get("changed")),
        )

    return validate_private_ml_store(
        model_root,
        expected_uid=int(expected_uid),
        allow_missing=True,
    )


def _systemd_unit_paths():
    units = sorted(set(allowed_services()) | {"piguard.service", "e3dc.service"})
    paths = []
    for unit_dir in SYSTEMD_UNIT_DIRS:
        current = Path("/")
        unsafe = False
        for component in unit_dir.parts[1:]:
            current /= component
            if os.path.lexists(str(current)) and current.is_symlink():
                unsafe = True
                break
        if unsafe:
            continue
        paths.extend(unit_dir / unit for unit in units)
    return paths


def _systemd_path_state_at(parent_fd, name, path):
    """Liefert missing, regular oder masked, ohne einem Eintrag zu folgen."""

    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(before.st_mode):
        target = os.readlink(name, dir_fd=parent_fd)
        state = "masked" if target == "/dev/null" else "unsafe-symlink"
    elif stat.S_ISREG(before.st_mode):
        state = "regular"
    else:
        raise BackupIntegrityError(f"Systemd-Unitpfad hat unzulässigen Dateityp: {path}")
    after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise BackupIntegrityError(f"Systemd-Unitpfad wurde während der Prüfung ausgetauscht: {path}")
    if state == "unsafe-symlink":
        raise BackupIntegrityError(
            f"Nichtkanonischer systemd-Symlink ist nicht sicherbar: {path}"
        )
    return state


def _systemd_path_state(unit_path):
    path = Path(unit_path)
    if path.parent != SYSTEMD_ADMIN_UNIT_DIR:
        raise BackupIntegrityError(f"Systemd-Maskenpfad liegt außerhalb der Admin-Unitfläche: {path}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(str(path.parent), flags)
    except FileNotFoundError:
        return "missing"
    try:
        return _systemd_path_state_at(parent_fd, path.name, path)
    finally:
        os.close(parent_fd)


def _is_expected_systemd_mask(unit_path):
    """Bindet ausschließlich eine kanonische systemd-Maske ohne Zielauflösung."""

    path = Path(unit_path)
    return path.parent == SYSTEMD_ADMIN_UNIT_DIR and _systemd_path_state(path) == "masked"


def _systemd_admin_unit_paths():
    return sorted(path for path in _systemd_unit_paths() if path.parent == SYSTEMD_ADMIN_UNIT_DIR)


def _systemd_mask_state_contract():
    entries = []
    for path in _systemd_admin_unit_paths():
        masked = _systemd_path_state(path) == "masked"
        entries.append({
            "path": str(path),
            "state": "masked" if masked else "unmasked",
            "target": "/dev/null" if masked else None,
        })
    return build_systemd_mask_state_contract(entries)


def _mask_entries_by_path(contract):
    verified = verify_systemd_mask_state_contract(contract)
    entries = {Path(str(item["path"])): item for item in verified["entries"]}
    expected = set(_systemd_admin_unit_paths())
    if set(entries) != expected:
        raise BackupIntegrityError("Systemd-Maskenumfang stimmt nicht mit dem Unitkatalog überein")
    return entries


def _missing_systemd_source_record(path):
    return {
        "category": "systemd",
        "source": str(path),
        "present": False,
        "files": 0,
        "source_type": "missing",
        "exclude_top_level": [],
        "exclude_anywhere": [],
        "directories": [],
    }


def get_backup_root(install_path=None):
    """Return a protected backup root outside the installation tree."""
    install = _lexical_absolute(install_path or INSTALL_PATH)
    return str(default_backup_root(install))


def _persistent_sources(install_path=None, systemd_mask_state=None):
    """Return the complete legacy and current recovery surface."""
    install = _lexical_absolute(install_path or INSTALL_PATH)
    configured_venv = str(load_config().get("venv_name") or ".venv_e3dc").strip()
    venv_exclusions = tuple(
        sorted({name for name in (configured_venv, ".venv_e3dc", ".venv", "venv") if name and "/" not in name and "\\" not in name})
    )
    sources = [
        PersistentSource(
            "install-tree",
            install,
            exclude_top_level=(
                ".git", "backups", "e3dc-control-backups",
                ".e3dc-control-backups", *venv_exclusions,
            ),
            exclude_anywhere=("__pycache__", "node_modules"),
        ),
        PersistentSource(
            "web-data",
            Path("/var/www/html/data"),
            exclude_top_level=(LEGACY_ML_MODEL.name,),
        ),
        PersistentSource(
            "web-program",
            Path("/var/www/html"),
            exclude_top_level=("data", "logs", "ramdisk", "tmp"),
        ),
        PersistentSource("system-state", Path("/var/lib/e3dc-control")),
        PersistentSource("system-config", Path("/etc/e3dc-control")),
    ]
    mask_entries = _mask_entries_by_path(systemd_mask_state or _systemd_mask_state_contract())
    for unit_path in _systemd_unit_paths():
        # Eine kanonische /dev/null-Maske ist kein Unit-Payload. Sie wird als
        # vollständiger SHA-256-gebundener Zustandsvertrag manifestiert. Der
        # synthetische missing-Source-Eintrag wird erst nach der Dateikopie
        # ergänzt, damit Restore die Maskenstelle transaktional freiräumt.
        if unit_path.parent == SYSTEMD_ADMIN_UNIT_DIR and mask_entries[unit_path]["state"] == "masked":
            continue
        sources.append(PersistentSource("systemd", unit_path))
    for watchdog in (Path("/usr/local/bin/boot_notify.sh"), Path("/usr/local/bin/pi_guard.sh")):
        sources.append(PersistentSource("watchdog", watchdog))
    return sources


def _restore_allowlist(install_path=None):
    install = _lexical_absolute(install_path or INSTALL_PATH)
    roots = [
        install,
        Path("/var/www/html"),
        Path("/var/lib/e3dc-control"),
        Path("/etc/e3dc-control"),
    ]
    files = _systemd_unit_paths()
    files.extend((Path("/usr/local/bin/boot_notify.sh"), Path("/usr/local/bin/pi_guard.sh")))
    return roots, files


def _backup_current_version_v2(install_path=None, preserve_backup_paths=None):
    active_install_path = _lexical_absolute(install_path or INSTALL_PATH)
    backup_dir = None
    try:
        # A legacy web Pickle is deliberately excluded above. A private model
        # is copied only after its non-executable manifest/hash contract passes.
        _prepare_private_ml_store_for_backup(PRIVATE_ML_ROOT)
        backup_root = default_backup_root(active_install_path)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        backup_dir = backup_root / timestamp
        os.mkdir(str(backup_dir), 0o700)
        print(f"→ Erstelle verifiziertes Backup unter {backup_dir}…")
        systemd_mask_state = _systemd_mask_state_contract()
        mask_entries = _mask_entries_by_path(systemd_mask_state)
        mapped_entries, source_records = copy_persistent_sources(
            backup_dir,
            _persistent_sources(active_install_path, systemd_mask_state),
        )
        source_records.extend(
            _missing_systemd_source_record(path)
            for path, entry in mask_entries.items()
            if entry["state"] == "masked"
        )
        if _systemd_mask_state_contract() != systemd_mask_state:
            raise BackupIntegrityError("Systemd-Maskenzustand driftete während des Backups")
        if not mapped_entries:
            raise BackupIntegrityError("Es wurden keine wiederherstellbaren Dateien gesichert.")
        secure_backup_tree(backup_dir)
        manifest = finalize_backup(
            backup_dir,
            mapped_entries,
            source_records,
            kind=SYSTEM_BACKUP_KIND,
            install_root=active_install_path,
            systemd_mask_state=systemd_mask_state,
        )
        verify_backup(backup_dir, expected_kind=SYSTEM_BACKUP_KIND)
        try:
            uid, _ = get_user_ids()
            gid = get_www_data_gid()
            for root, dirs, files in os.walk(str(backup_dir), followlinks=False):
                os.chown(root, uid, gid)
                for name in dirs:
                    os.chown(os.path.join(root, name), uid, gid)
                for name in files:
                    os.chown(os.path.join(root, name), uid, gid)
        except Exception as exc:
            raise BackupIntegrityError(f"Backup-Besitzrechte konnten nicht sicher gesetzt werden: {exc}")
        preserve = list(preserve_backup_paths or ()) + [backup_dir]
        retention = prune_install_backups(
            active_install_path,
            backup_root=backup_root,
            preserve_paths=preserve,
            logger=backup_logger,
        )
        if not retention.get("success"):
            raise BackupIntegrityError("Backup-Retention ist fehlgeschlagen.")
        count = len(manifest.get("files", []))
        print(f"  ✓ Manifest und SHA-256 für {count} Dateien verifiziert")
        log_task_completed("Backup erstellen", details=f"{count} Dateien in {backup_dir.name}")
        return str(backup_dir)
    except Exception as exc:
        log_error("backup", f"Verifiziertes Backup fehlgeschlagen: {exc}", exc)
        # Leave incomplete, non-manifested directories quarantined. Retention
        # ignores them; a recursive cleanup here would reopen a TOCTOU window.
        print(f"✗ Fehler beim Backup: {exc}\n")
        return None


def backup_current_version(install_path=None, preserve_backup_paths=None):
    """Create the only supported, complete, manifest-verified system backup."""
    return _backup_current_version_v2(
        install_path=install_path,
        preserve_backup_paths=preserve_backup_paths,
    )


def choose_backup_version(action_text="wiederherstellen"):
    """Wählt eine Backup-Version für die angegebene Aktion aus."""
    try:
        backup_root = get_backup_root()
    except BackupIntegrityError as exc:
        print(f"✗ Backup-Pfad ist ungültig: {exc}\n")
        return None
    if not os.path.exists(backup_root):
        print("✗ Keine Backups vorhanden.\n")
        backup_logger.warning("Kein Backup-Verzeichnis gefunden.")
        return None

    try:
        versions = []
        for name in sorted(os.listdir(backup_root)):
            if name == "web_installer":
                continue
            candidate = os.path.join(backup_root, name)
            try:
                verify_backup(candidate, expected_kind=SYSTEM_BACKUP_KIND)
                versions.append(name)
            except Exception:
                continue
    except Exception as e:
        print(f"✗ Fehler beim Lesen der Backups: {e}\n")
        log_error("backup", f"Fehler beim Lesen des Backup-Verzeichnisses: {e}", e)
        return None

    if not versions:
        print("✗ Keine Backups vorhanden.\n")
        return None

    print("\nVerfügbare Backups:")
    for i, v in enumerate(versions):
        print(f"  {i+1}: {v}")

    choice = input(f"\nWelche Version {action_text}? (Nummer): ").strip()
    if not choice.isdigit():
        print("✗ Ungültige Eingabe.\n")
        return None

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(versions):
            print("✗ Ungültige Auswahl.\n")
            return None
        return os.path.join(backup_root, versions[idx])
    except (ValueError, IndexError):
        print("✗ Fehler bei der Auswahl.\n")
        return None


def _systemd_identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _remove_canonical_systemd_mask(path, expected_identity=None):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(str(path.parent), flags)
    try:
        state = _systemd_path_state_at(parent_fd, path.name, path)
        if state != "masked":
            raise BackupIntegrityError(f"Erwartete kanonische systemd-Maske fehlt: {path}")
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if expected_identity is not None and _systemd_identity(before) != expected_identity:
            raise BackupIntegrityError(f"Systemd-Maske wurde vor dem Entfernen ausgetauscht: {path}")
        os.unlink(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        if _systemd_path_state_at(parent_fd, path.name, path) != "missing":
            raise BackupIntegrityError(f"Systemd-Maske konnte nicht sicher entfernt werden: {path}")
    finally:
        os.close(parent_fd)


def _create_canonical_systemd_mask(path):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(str(path.parent), flags)
    created_identity = None
    try:
        if _systemd_path_state_at(parent_fd, path.name, path) != "missing":
            raise BackupIntegrityError(f"Systemd-Maskenziel ist vor dem Restore nicht frei: {path}")
        os.symlink("/dev/null", path.name, dir_fd=parent_fd)
        created_identity = _systemd_identity(
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        )
        os.fsync(parent_fd)
        if _systemd_path_state_at(parent_fd, path.name, path) != "masked":
            raise BackupIntegrityError(f"Systemd-Maske konnte nicht verifiziert werden: {path}")
        if _systemd_identity(os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)) != created_identity:
            raise BackupIntegrityError(f"Systemd-Maske driftete nach ihrer Erzeugung: {path}")
        return created_identity
    except Exception as create_exc:
        if created_identity is not None:
            try:
                current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                if _systemd_identity(current) != created_identity:
                    raise BackupIntegrityError("erzeugte Maske wurde fremd ersetzt")
                os.unlink(path.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except Exception as rollback_exc:
                raise BackupIntegrityError(
                    f"Maskenerzeugung und Rücklauf sind fehlgeschlagen: {create_exc}; {rollback_exc}"
                ) from create_exc
        raise
    finally:
        os.close(parent_fd)


def _apply_systemd_mask_states(states, *, allow_existing=False):
    """Setzt einen vollständigen Maskensatz mit Rücklauf bei Teilfehlern."""

    paths = sorted(states)
    for path in paths:
        state = _systemd_path_state(path)
        if states[path] and state not in {"missing", "masked"}:
            raise BackupIntegrityError(f"Maskierter Backupzustand trifft auf Unit-Payload: {path}")
        if states[path] and state == "masked" and not allow_existing:
            raise BackupIntegrityError(f"Systemd-Maske erschien unerwartet während der Transaktion: {path}")
        if not states[path] and state == "masked":
            raise BackupIntegrityError(f"Unerwartete systemd-Maske am unmaskierten Ziel: {path}")

    created = {}
    try:
        for path in paths:
            if states[path] and _systemd_path_state(path) == "missing":
                created[path] = _create_canonical_systemd_mask(path)
        for path in paths:
            actual = _systemd_path_state(path)
            if states[path] != (actual == "masked"):
                raise BackupIntegrityError(f"Systemd-Maskenzustand wurde nicht exakt restauriert: {path}")
    except Exception:
        rollback_errors = []
        for path in reversed(list(created)):
            try:
                _remove_canonical_systemd_mask(path, created[path])
            except Exception as exc:
                rollback_errors.append(f"{path}: {exc}")
        if rollback_errors:
            raise BackupIntegrityError(
                "Maskenrestore und dessen Rücklauf sind fehlgeschlagen: " + "; ".join(rollback_errors)
            )
        raise
    return created


def _remove_created_systemd_masks(created):
    errors = []
    for path in reversed(list(created)):
        try:
            _remove_canonical_systemd_mask(path, created[path])
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        raise BackupIntegrityError("Erzeugte systemd-Masken konnten nicht sicher entfernt werden: " + "; ".join(errors))


def _reload_and_verify_systemd_mask_states(states):
    """Lädt systemd neu und bindet dessen Sicht an den Plattenzustand."""

    reload_result = subprocess.run(
        ["systemctl", "daemon-reload"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if reload_result.returncode != 0:
        raise BackupIntegrityError(
            "systemd daemon-reload nach Maskenrestore fehlgeschlagen: "
            + (reload_result.stderr or reload_result.stdout or "unbekannter Fehler").strip()
        )
    allowed = {
        "enabled", "enabled-runtime", "disabled", "static", "indirect",
        "generated", "transient", "alias", "linked", "linked-runtime",
        "not-found", "masked", "masked-runtime",
    }
    for path in sorted(states):
        result = subprocess.run(
            ["systemctl", "is-enabled", path.name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        value = (result.stdout or result.stderr or "").strip().splitlines()
        status = value[0].strip().lower() if value else ""
        if status not in allowed:
            raise BackupIntegrityError(f"Systemd-Maskenzustand ist nicht lesbar: {path}")
        is_masked = status in {"masked", "masked-runtime"}
        if states[path] != is_masked:
            raise BackupIntegrityError(
                f"Systemd meldet unerwarteten Maskenzustand {status!r}: {path}"
            )


def _restore_payload_with_mask_contract(backup_path, manifest, allowed_roots, allowed_files):
    contract = manifest.get("systemd_mask_state")
    if contract is None:
        # Bestehende Schema-2-Backups hatten keinen Maskenvertrag. Sie bleiben
        # lesbar, autorisieren aber bewusst weder mask noch unmask. Dadurch wird
        # aus fehlender Alt-Evidenz kein erfundener Aktivierungszustand.
        return restore_persistent_payload(
            backup_path,
            allowed_roots=allowed_roots,
            allowed_files=allowed_files,
        )

    entries = _mask_entries_by_path(contract)
    expected = {path: entry["state"] == "masked" for path, entry in entries.items()}
    original = {path: _systemd_path_state(path) == "masked" for path in entries}
    try:
        for path in sorted(entries):
            if original[path]:
                _remove_canonical_systemd_mask(path)
    except Exception as exc:
        try:
            _apply_systemd_mask_states(original, allow_existing=True)
            _reload_and_verify_systemd_mask_states(original)
        except Exception as rollback_exc:
            raise BackupIntegrityError(
                f"Masken-Preflight und Rücklauf sind fehlgeschlagen: {exc}; {rollback_exc}"
            ) from exc
        raise

    def apply_masks_before_commit():
        created = {}
        try:
            created = _apply_systemd_mask_states(expected)
            _reload_and_verify_systemd_mask_states(expected)
        except Exception as exc:
            try:
                _remove_created_systemd_masks(created)
                _reload_and_verify_systemd_mask_states({path: False for path in entries})
            except Exception as rollback_exc:
                raise BackupIntegrityError(
                    f"Masken-Commit und sein Rücklauf sind fehlgeschlagen: {exc}; {rollback_exc}"
                ) from exc
            raise

    try:
        restored = restore_persistent_payload(
            backup_path,
            allowed_roots=allowed_roots,
            allowed_files=allowed_files,
            before_commit=apply_masks_before_commit,
        )
    except Exception as exc:
        try:
            _apply_systemd_mask_states(original, allow_existing=True)
            _reload_and_verify_systemd_mask_states(original)
        except Exception as rollback_exc:
            raise BackupIntegrityError(
                f"Payload-Restore und Maskenrücklauf sind fehlgeschlagen: {exc}; {rollback_exc}"
            ) from exc
        raise
    return restored


def restore_verified_backup(backup_path, install_path=None):
    """Restore the complete manifested recovery surface without prompting."""
    manifest = verify_backup(backup_path, expected_kind=SYSTEM_BACKUP_KIND)
    allowed_roots, allowed_files = _restore_allowlist(install_path)
    restored = _restore_payload_with_mask_contract(
        backup_path,
        manifest,
        allowed_roots,
        allowed_files,
    )
    validate_private_ml_store(PRIVATE_ML_ROOT, allow_missing=True)
    return restored


def restore_backup(backup_path, install_path=None, confirmed=False):
    """Restore only a complete, immutable manifest backup."""
    try:
        verify_backup(backup_path, expected_kind=SYSTEM_BACKUP_KIND)
    except BackupIntegrityError as exc:
        print(f"[!] Backup ist unvollstaendig oder nicht lesbar: {exc}")
        backup_logger.error(f"Backup-Integritaetspruefung fehlgeschlagen: {exc}")
        return False

    if not confirmed:
        confirm = input(
            f"Backup {os.path.basename(backup_path)} wirklich wiederherstellen? (ja/nein): "
        ).strip().lower()
        if confirm != "ja":
            print("[i] Wiederherstellung abgebrochen.")
            return False
    try:
        restored = restore_verified_backup(backup_path, install_path=install_path)
    except Exception as exc:
        print(f"[!] Verifizierte Wiederherstellung fehlgeschlagen: {exc}")
        log_error("backup", f"Verifizierte Wiederherstellung fehlgeschlagen: {exc}", exc)
        return False
    print(f"[OK] {restored} Dateien transaktional und mit SHA-256 wiederhergestellt.")
    log_task_completed(
        "Backup wiederherstellen",
        details=f"{restored} Dateien aus {os.path.basename(backup_path)}",
    )
    return True


def delete_backup():
    """Löscht eine ausgewählte Backup-Version."""
    backup_path = choose_backup_version("löschen")
    if not backup_path:
        return False

    try:
        verify_backup(backup_path, expected_kind=SYSTEM_BACKUP_KIND)
    except Exception as exc:
        print(f"✗ Ungültiges Backup: {exc}\n")
        return False

    print(f"\n⚠ Willst du wirklich das Backup {os.path.basename(backup_path)} löschen?")
    confirm = input("Ja/Nein [n]: ").strip().lower()
    if confirm != "ja":
        print("✗ Abgebrochen.\n")
        return False

    try:
        delete_verified_backup(
            backup_path,
            get_backup_root(),
            expected_kind=SYSTEM_BACKUP_KIND,
        )
        print("✓ Backup gelöscht.\n")
        backup_logger.info(f"Backup gelöscht: {os.path.basename(backup_path)}")
        log_task_completed("Backup löschen", details=os.path.basename(backup_path))
        return True
    except Exception as e:
        print(f"✗ Fehler beim Löschen des Backups: {e}\n")
        log_error("backup", f"Fehler beim Löschen des Backups: {e}", e)
        return False


def apply_backup_limit():
    """Wendet das Backup-Limit manuell auf vorhandene Backups an."""
    retention = prune_install_backups(
        INSTALL_PATH,
        backup_root=get_backup_root(),
        logger=backup_logger,
    )
    removed_update = len(retention.get("update_backups", {}).get("removed", []))
    removed_web = len(retention.get("web_installer_backups", {}).get("removed", []))
    print("\n=== Backup-Limit ===")
    print(f"System-Backups behalten: {UPDATE_BACKUP_KEEP_COUNT}")
    print(f"Entfernte alte Backups: {removed_update + removed_web}")
    if not retention.get("success", True):
        print("⚠ Einzelne Backups konnten nicht entfernt werden. Details stehen im Installer-Log.")
    else:
        print("✓ Backup-Limit angewendet.")
    print("")
    return bool(retention.get("success", True))


def backup_full():
    """Erstellt ein vollständiges Backup aller Komponenten."""
    return backup_current_version()


def backup_menu():
    """Menü für Backup-Verwaltung."""
    if os.path.exists('/.dockerenv'):
        print("\n=== Docker Hinweis ===")
        print("In einer Docker-Umgebung sollten Backups besser durch die Sicherung")
        print("der entsprechenden Host-Volumes (z.B. ./data) erfolgen.")
        print("Dieses Skript sichert die Daten lediglich IN den Container.")
        if input("\nMöchten Sie trotzdem ein Container-Backup erstellen? (j/n): ").strip().lower() != "j":
            return

    print("\n=== Backup-Verwaltung ===\n")
    print("1 = Vollständiges Backup erstellen")
    print("2 = Backup wiederherstellen")
    print("3 = Backup löschen")
    print("4 = Backup-Limit jetzt anwenden")
    choice = input("Auswahl: ").strip()

    if choice == "1":
        backup_full()
    elif choice == "2":
        backup_path = choose_backup_version()
        if backup_path:
            from .rollback import rollback
            rollback(backup_path)
    elif choice == "3":
        delete_backup()
    elif choice == "4":
        apply_backup_limit()
    else:
        print("✗ Ungültige Auswahl.\n")


register_command("13", "System-Backup erstellen / verwalten", backup_menu, sort_order=13)

