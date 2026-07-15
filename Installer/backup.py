import os
import datetime
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
    SYSTEM_BACKUP_KIND,
    _lexical_absolute,
    copy_persistent_sources,
    default_backup_root,
    finalize_backup,
    restore_persistent_payload,
    secure_backup_tree,
    validate_private_ml_store,
    verify_backup,
)
from .installer_config import get_install_path, get_user_ids, get_www_data_gid, load_config
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning
from .service_catalog import allowed_services

INSTALL_PATH = get_install_path()
backup_logger = get_or_create_logger("backup")


def _systemd_unit_paths():
    units = sorted(set(allowed_services()) | {"piguard.service", "e3dc.service"})
    paths = []
    for unit_dir in (Path("/etc/systemd/system"), Path("/usr/lib/systemd/system"), Path("/lib/systemd/system")):
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


def get_backup_root(install_path=None):
    """Return a protected backup root outside the installation tree."""
    install = _lexical_absolute(install_path or INSTALL_PATH)
    return str(default_backup_root(install))


def _persistent_sources(install_path=None):
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
    for unit_path in _systemd_unit_paths():
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
        validate_private_ml_store(PRIVATE_ML_ROOT, allow_missing=True)
        backup_root = default_backup_root(active_install_path)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        backup_dir = backup_root / timestamp
        os.mkdir(str(backup_dir), 0o700)
        print(f"→ Erstelle verifiziertes Backup unter {backup_dir}…")
        mapped_entries, source_records = copy_persistent_sources(
            backup_dir,
            _persistent_sources(active_install_path),
        )
        if not mapped_entries:
            raise BackupIntegrityError("Es wurden keine wiederherstellbaren Dateien gesichert.")
        secure_backup_tree(backup_dir)
        manifest = finalize_backup(
            backup_dir,
            mapped_entries,
            source_records,
            kind=SYSTEM_BACKUP_KIND,
            install_root=active_install_path,
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


def restore_verified_backup(backup_path, install_path=None):
    """Restore the complete manifested recovery surface without prompting."""
    verify_backup(backup_path, expected_kind=SYSTEM_BACKUP_KIND)
    allowed_roots, allowed_files = _restore_allowlist(install_path)
    restored = restore_persistent_payload(
        backup_path,
        allowed_roots=allowed_roots,
        allowed_files=allowed_files,
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

