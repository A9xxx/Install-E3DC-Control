import os
import datetime
import pwd
import stat
import subprocess
import sys
from dataclasses import dataclass
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
from .backup_retention import (
    UPDATE_BACKUP_KEEP_COUNT,
    WEB_INSTALLER_BACKUP_KEEP_COUNT,
    delete_verified_backup_family,
    prune_install_backups,
)
from .backup_integrity import (
    BackupIntegrityError,
    BoundPersistentInstallRoot,
    LEGACY_ML_MODEL,
    PRIVATE_ML_ROOT,
    PersistentSource,
    QuiescedOverlayRestoreGuard,
    QUIESCED_OVERLAY_KIND,
    SYSTEMD_ADMIN_UNIT_DIR,
    SYSTEM_BACKUP_KIND,
    _lexical_absolute,
    _normalized_backup_id,
    _normalized_transaction_id,
    _open_root_controlled_backup_directory_chain,
    bind_persistent_source_root,
    build_systemd_mask_state_contract,
    copy_persistent_sources,
    default_backup_root,
    estimate_persistent_sources_size,
    finalize_backup,
    normalize_private_ml_lock_metadata,
    restore_persistent_payload,
    secure_backup_tree,
    validate_existing_backup_root,
    validate_private_ml_store,
    verify_backup,
    verify_bound_persistent_install_root,
    verified_manifest_sha256,
    verify_systemd_mask_state_contract,
)
from .installer_config import get_install_path, get_user_ids, get_www_data_gid, load_config
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning
from .service_catalog import allowed_services
from .ha_root_runtime import HA_ROOT_RUNTIME_BASE

INSTALL_PATH = get_install_path()
backup_logger = get_or_create_logger("backup")
SYSTEMD_UNIT_DIRS = (
    SYSTEMD_ADMIN_UNIT_DIR,
    Path("/usr/lib/systemd/system"),
    Path("/lib/systemd/system"),
)
APACHE_SECURITY_CONFIG = Path("/etc/apache2/conf-available/e3dc-control-security.conf")
MANAGER_LOCK_TMPFILES_CONFIG = Path("/etc/tmpfiles.d/e3dc-control-locks.conf")
WEB_ROOT = Path("/var/www/html")
WEB_PERSISTENT_FILE_NAMES = (
    "e3dc.config.txt",
    "e3dc.strompreise.txt",
    "e3dc.wallbox.out",
    "e3dc.wallbox.txt",
    "e3dc_paths.json",
    "live_history.txt",
)
WEB_HISTORY_BACKUPS = WEB_ROOT / "history_backups"


@dataclass(frozen=True)
class BackupProfileOptions:
    """Enger Backup-Vertrag; der strenge Pfad bleibt der Standard."""

    name: str
    keep_root_ownership: bool = False
    raise_errors: bool = False
    tolerate_replaceable_entries: bool = False
    allow_quiesced_source_drift: bool = False
    include_optional_ml: bool = True
    enforce_systemd_mask_contract: bool = True
    retention_failure_is_fatal: bool = True


STRICT_BACKUP_PROFILE = BackupProfileOptions(name="strict")
REPAIR_UPDATE_BACKUP_PROFILE = BackupProfileOptions(
    name="repair-update",
    keep_root_ownership=True,
    raise_errors=True,
    tolerate_replaceable_entries=True,
    allow_quiesced_source_drift=True,
    include_optional_ml=False,
    enforce_systemd_mask_contract=False,
    retention_failure_is_fatal=False,
)


def _bootstrap_repair_update_profile_bound(install_path) -> bool:
    """Aktiviert das Reparaturprofil nur für den vollständig gebundenen Runner."""

    values = {
        "target": str(os.environ.get("E3DC_BOOTSTRAP_ROOT") or "").strip(),
        "runner": str(os.environ.get("E3DC_BOOTSTRAP_RUNNER_ROOT") or "").strip(),
        "user": str(os.environ.get("E3DC_BOOTSTRAP_USER") or "").strip(),
        "mode": str(os.environ.get("E3DC_BOOTSTRAP_ENTRY_MODE") or "").strip(),
    }
    if not all(values.values()) or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return False
    if values["mode"] not in {"regular", "rescue"} or values["user"] in {"root", "www-data"}:
        return False
    try:
        pwd.getpwnam(values["user"])
        target = _lexical_absolute(values["target"])
        runner = _lexical_absolute(values["runner"])
        install = _lexical_absolute(install_path or INSTALL_PATH)
        module_root = _lexical_absolute(Path(__file__).absolute().parent.parent)
        common = Path(os.path.commonpath((str(runner), str(target))))
    except (KeyError, OSError, ValueError, BackupIntegrityError):
        return False
    return (
        target == install
        and runner == module_root
        and runner != target
        and common not in {runner, target}
    )


def _selected_backup_profile(profile, install_path) -> BackupProfileOptions:
    if isinstance(profile, BackupProfileOptions):
        return profile
    if profile is None:
        return (
            REPAIR_UPDATE_BACKUP_PROFILE
            if _bootstrap_repair_update_profile_bound(install_path)
            else STRICT_BACKUP_PROFILE
        )
    name = str(profile).strip().lower()
    if name == STRICT_BACKUP_PROFILE.name:
        return STRICT_BACKUP_PROFILE
    if name == REPAIR_UPDATE_BACKUP_PROFILE.name:
        return REPAIR_UPDATE_BACKUP_PROFILE
    raise BackupIntegrityError(f"Unbekanntes Backup-Profil: {profile}")


def _warn_backup(message: str) -> None:
    """Gibt eine Warnung genau einmal über das zentrale Installationslog aus."""

    log_warning("backup", message)


def _info_backup(message: str) -> None:
    """Zeigt erwartbare Profilhinweise einmalig und ohne Warnstufe an."""

    print(f"[INFO] {message}", flush=True)
    backup_logger.info(message)


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


def _systemd_dropin_paths():
    """Liefert ausschließlich Drop-in-Verzeichnisse des gebundenen Dienstkatalogs."""

    return tuple(
        path.parent / f"{path.name}.d"
        for path in _systemd_admin_unit_paths()
    )


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


def _persistent_sources(
    install_path=None,
    systemd_mask_state=None,
    *,
    profile=STRICT_BACKUP_PROFILE,
):
    """Return the complete legacy and current recovery surface."""
    install = _lexical_absolute(install_path or INSTALL_PATH)
    options = _selected_backup_profile(profile, install)
    if options.name == REPAIR_UPDATE_BACKUP_PROFILE.name:
        # Der Reparatur-Updater bindet den Installationsroot separat. Sein venv
        # liegt nach aktuellem Vertrag direkt im Benutzer-Home; ein privilegierter
        # Altconfig-Read ist für die Produktsicherung weder nötig noch zulässig.
        configured_venv = ".venv_e3dc"
    else:
        configured_venv = str(
            load_config().get("venv_name") or ".venv_e3dc"
        ).strip()
    venv_exclusions = tuple(
        sorted({name for name in (configured_venv, ".venv_e3dc", ".venv", "venv") if name and "/" not in name and "\\" not in name})
    )
    replaceable_policy = (
        "diagnose-skip" if options.tolerate_replaceable_entries else "fail"
    )
    install_exclusions = [
        ".git", "backups", "e3dc-control-backups",
        ".e3dc-control-backups", *venv_exclusions,
    ]
    if options.name == REPAIR_UPDATE_BACKUP_PROFILE.name:
        install_exclusions.extend(("data", "e3dc.config.txt"))
    web_program_exclusions = ["data", "logs", "ramdisk", "tmp"]
    if options.name == REPAIR_UPDATE_BACKUP_PROFILE.name:
        web_program_exclusions.extend((*WEB_PERSISTENT_FILE_NAMES, "history_backups"))
    sources = [
        PersistentSource(
            "install-tree",
            install,
            exclude_top_level=tuple(install_exclusions),
            exclude_anywhere=("__pycache__", "node_modules"),
            unsafe_entry_policy=replaceable_policy,
            allow_live_drift=options.tolerate_replaceable_entries,
        ),
    ]
    if options.name == REPAIR_UPDATE_BACKUP_PROFILE.name:
        sources.extend((
            PersistentSource(
                "install-config",
                install / "e3dc.config.txt",
                allow_live_drift=options.allow_quiesced_source_drift,
            ),
            PersistentSource(
                "install-data",
                install / "data",
                allow_live_drift=options.allow_quiesced_source_drift,
            ),
        ))
    sources.extend((
        PersistentSource(
            "web-data",
            WEB_ROOT / "data",
            exclude_top_level=(LEGACY_ML_MODEL.name,),
            allow_live_drift=options.allow_quiesced_source_drift,
        ),
        PersistentSource(
            "web-program",
            WEB_ROOT,
            exclude_top_level=tuple(web_program_exclusions),
            unsafe_entry_policy=replaceable_policy,
            allow_live_drift=options.tolerate_replaceable_entries,
        ),
        PersistentSource(
            "system-state",
            Path("/var/lib/e3dc-control"),
            exclude_top_level=(() if options.include_optional_ml else ("ml",)),
            allow_live_drift=options.allow_quiesced_source_drift,
        ),
        PersistentSource(
            "system-config",
            Path("/etc/e3dc-control"),
            allow_live_drift=options.allow_quiesced_source_drift,
        ),
        PersistentSource(
            "service-launcher",
            Path("/usr/local/sbin/e3dc-service-control"),
            unsafe_entry_policy=replaceable_policy,
            allow_live_drift=options.tolerate_replaceable_entries,
        ),
        PersistentSource(
            "update-launcher",
            Path("/usr/local/sbin/e3dc-web-update-launcher"),
            unsafe_entry_policy=replaceable_policy,
            allow_live_drift=options.tolerate_replaceable_entries,
        ),
        PersistentSource(
            "runtime-permissions-launcher",
            Path("/usr/local/sbin/e3dc-runtime-permissions-repair"),
            unsafe_entry_policy=replaceable_policy,
            allow_live_drift=options.tolerate_replaceable_entries,
        ),
        PersistentSource(
            "ha-root-runtime",
            HA_ROOT_RUNTIME_BASE,
            unsafe_entry_policy=replaceable_policy,
            allow_live_drift=options.tolerate_replaceable_entries,
        ),
        PersistentSource(
            "sudoers",
            Path("/etc/sudoers.d/020_e3dc_services"),
            unsafe_entry_policy=replaceable_policy,
            allow_live_drift=options.tolerate_replaceable_entries,
        ),
        PersistentSource(
            "apache-config",
            APACHE_SECURITY_CONFIG,
            unsafe_entry_policy=replaceable_policy,
            allow_live_drift=options.tolerate_replaceable_entries,
        ),
        PersistentSource(
            "tmpfiles-config",
            MANAGER_LOCK_TMPFILES_CONFIG,
            unsafe_entry_policy=replaceable_policy,
            allow_live_drift=options.tolerate_replaceable_entries,
        ),
    ))
    if options.name == REPAIR_UPDATE_BACKUP_PROFILE.name:
        sources.extend(
            PersistentSource(
                "web-root-persistent",
                WEB_ROOT / name,
                allow_live_drift=options.allow_quiesced_source_drift,
            )
            for name in WEB_PERSISTENT_FILE_NAMES
        )
        sources.append(
            PersistentSource(
                "web-history-backups",
                WEB_HISTORY_BACKUPS,
                allow_live_drift=options.allow_quiesced_source_drift,
            )
        )
    mask_entries = None
    if options.enforce_systemd_mask_contract:
        mask_entries = _mask_entries_by_path(
            systemd_mask_state or _systemd_mask_state_contract()
        )
    for unit_path in _systemd_unit_paths():
        # Eine kanonische /dev/null-Maske ist kein Unit-Payload. Sie wird als
        # vollständiger SHA-256-gebundener Zustandsvertrag manifestiert. Der
        # synthetische missing-Source-Eintrag wird erst nach der Dateikopie
        # ergänzt, damit Restore die Maskenstelle transaktional freiräumt.
        if (
            mask_entries is not None
            and unit_path.parent == SYSTEMD_ADMIN_UNIT_DIR
            and mask_entries[unit_path]["state"] == "masked"
        ):
            continue
        sources.append(
            PersistentSource(
                "systemd",
                unit_path,
                unsafe_entry_policy=replaceable_policy,
                allow_live_drift=options.tolerate_replaceable_entries,
            )
        )
    for dropin_path in _systemd_dropin_paths():
        service_name = dropin_path.name.removesuffix(".service.d")
        sources.append(
            PersistentSource(
                f"systemd-dropin-{service_name}",
                dropin_path,
                unsafe_entry_policy=replaceable_policy,
                allow_live_drift=options.tolerate_replaceable_entries,
            )
        )
    for watchdog in (Path("/usr/local/bin/boot_notify.sh"), Path("/usr/local/bin/pi_guard.sh")):
        sources.append(
            PersistentSource(
                "watchdog",
                watchdog,
                unsafe_entry_policy=replaceable_policy,
                allow_live_drift=options.tolerate_replaceable_entries,
            )
        )
    return sources


def estimate_full_backup_size(install_path=None, systemd_mask_state=None):
    """Schätzt die vollständige Recovery-Fläche ohne Dateien zu verändern."""

    active_install_path = _lexical_absolute(install_path or INSTALL_PATH)
    mask_state = systemd_mask_state or _systemd_mask_state_contract()
    return estimate_persistent_sources_size(
        _persistent_sources(active_install_path, mask_state)
    )


def _restore_allowlist(install_path=None):
    install = _lexical_absolute(install_path or INSTALL_PATH)
    roots = [
        install,
        Path("/var/www/html"),
        Path("/var/lib/e3dc-control"),
        Path("/etc/e3dc-control"),
        HA_ROOT_RUNTIME_BASE,
        *_systemd_dropin_paths(),
    ]
    files = _systemd_unit_paths()
    files.extend(
        (
            Path("/usr/local/bin/boot_notify.sh"),
            Path("/usr/local/bin/pi_guard.sh"),
            Path("/usr/local/sbin/e3dc-service-control"),
            Path("/usr/local/sbin/e3dc-web-update-launcher"),
            Path("/usr/local/sbin/e3dc-runtime-permissions-repair"),
            Path("/etc/sudoers.d/020_e3dc_services"),
            APACHE_SECURITY_CONFIG,
            MANAGER_LOCK_TMPFILES_CONFIG,
        )
    )
    return roots, files


def _backup_current_version_v2(
    install_path=None,
    preserve_backup_paths=None,
    verified_pre_chown_callback=None,
    *,
    profile=STRICT_BACKUP_PROFILE,
    expected_install_root_identity=None,
):
    active_install_path = _lexical_absolute(install_path or INSTALL_PATH)
    options = _selected_backup_profile(profile, active_install_path)
    backup_dir = None
    try:
        # A legacy web Pickle is deliberately excluded above. A private model
        # is copied only after its non-executable manifest/hash contract passes.
        if options.include_optional_ml:
            _prepare_private_ml_store_for_backup(PRIVATE_ML_ROOT)
        elif os.path.lexists(str(PRIVATE_ML_ROOT)):
            _info_backup(
                "Der optionale ML-Store bleibt für das Core-Update unverändert "
                "und wird nicht Bestandteil des Rückfallbackups."
            )
        backup_root = default_backup_root(active_install_path)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        backup_dir = backup_root / timestamp
        os.mkdir(str(backup_dir), 0o700)
        print(
            f"→ Erstelle verifiziertes Backup unter {backup_dir} "
            f"(Profil: {options.name})…"
        )
        systemd_mask_state = None
        mask_entries = {}
        mask_state_before = None
        if options.enforce_systemd_mask_contract:
            systemd_mask_state = _systemd_mask_state_contract()
            mask_entries = _mask_entries_by_path(systemd_mask_state)
        else:
            try:
                mask_state_before = _systemd_mask_state_contract()
            except Exception as exc:
                _warn_backup(
                    "Der alte systemd-Maskenzustand ist nicht eindeutig lesbar und "
                    f"wird beim Reparaturupdate nicht als Rückfallvoraussetzung verwendet: {exc}"
                )
        with bind_persistent_source_root(
            active_install_path,
            expected_identity=expected_install_root_identity,
        ) as source_binding:
            mapped_entries, source_records = copy_persistent_sources(
                backup_dir,
                _persistent_sources(
                    active_install_path,
                    systemd_mask_state,
                    profile=options,
                ),
                bound_source_root=source_binding,
            )
        if options.enforce_systemd_mask_contract:
            source_records.extend(
                _missing_systemd_source_record(path)
                for path, entry in mask_entries.items()
                if entry["state"] == "masked"
            )
            if _systemd_mask_state_contract() != systemd_mask_state:
                raise BackupIntegrityError("Systemd-Maskenzustand driftete während des Backups")
        else:
            try:
                mask_state_after = _systemd_mask_state_contract()
            except Exception as exc:
                mask_state_after = None
                _warn_backup(
                    "Der systemd-Maskenzustand war nach dem Reparaturbackup nicht "
                    f"eindeutig lesbar; das verifizierte Backup bleibt gültig: {exc}"
                )
            if (
                mask_state_before is not None
                and mask_state_after is not None
                and mask_state_after != mask_state_before
            ):
                _warn_backup(
                    "Der systemd-Maskenzustand änderte sich während des Live-Backups; "
                    "das Reparaturupdate ersetzt die betroffenen Core-Units direkt."
                )
        skipped = [
            (record.get("source"), item)
            for record in source_records
            for item in record.get("skipped_entries", [])
            if isinstance(record, dict) and isinstance(item, dict)
        ]
        for source, item in skipped:
            if (
                source == str(SYSTEMD_ADMIN_UNIT_DIR / "e3dc.service")
                and item.get("path") == "."
                and item.get("reason") == "symlink"
            ):
                _info_backup(
                    "Die alte e3dc.service-Verknüpfung wird bewusst nicht als Datei "
                    "gesichert; der veröffentlichte Dienststand wird beim Update neu gesetzt."
                )
            else:
                _warn_backup(
                    "Backupquelle wurde nofollow übersprungen: "
                    f"{source} / {item.get('path')} ({item.get('reason')})"
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
            systemd_mask_state=(
                systemd_mask_state if options.enforce_systemd_mask_contract else None
            ),
        )
        if verified_pre_chown_callback is not None:
            if not callable(verified_pre_chown_callback):
                raise BackupIntegrityError("Backup-Receipt-Callback ist nicht aufrufbar")
            verified_pre_chown_callback(str(backup_dir), manifest)
        # Ein Update-Recovery-Receipt darf seine Autorität nicht unmittelbar
        # nach dem Callback wieder an den Installationsnutzer verlieren. Der
        # hierfür erzeugte Transaktionsbaum bleibt deshalb root:root 0700;
        # normale manuelle Backups ohne Receipt behalten ihr bisheriges
        # Besitzmodell.
        if verified_pre_chown_callback is None and not options.keep_root_ownership:
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
                raise BackupIntegrityError(
                    f"Backup-Besitzrechte konnten nicht sicher gesetzt werden: {exc}"
                )
        preserve = list(preserve_backup_paths or ()) + [backup_dir]
        try:
            retention = prune_install_backups(
                active_install_path,
                backup_root=backup_root,
                preserve_paths=preserve,
                logger=backup_logger,
            )
            if not retention.get("success"):
                raise BackupIntegrityError("Backup-Retention ist fehlgeschlagen.")
            if not retention.get("limit_satisfied", True):
                update_retention = retention.get("update_backups")
                web_retention = retention.get("web_installer_backups")
                retention_payloads = [
                    payload
                    for payload in (update_retention, web_retention)
                    if isinstance(payload, dict)
                ]
                unclassified_count = sum(
                    len(payload.get("unclassified") or [])
                    for payload in retention_payloads
                )
                verified_over_limit = any(
                    isinstance(payload.get("verified_count_after"), int)
                    and not isinstance(payload.get("verified_count_after"), bool)
                    and isinstance(payload.get("keep_count"), int)
                    and not isinstance(payload.get("keep_count"), bool)
                    and payload["verified_count_after"] > payload["keep_count"]
                    for payload in retention_payloads
                )
                if retention.get("blocked") or verified_over_limit:
                    detail = (
                        "Eine laufende Schutz- oder Recovery-Bindung hält den "
                        "verifizierten Zielbestand vorübergehend über der Grenze. "
                        "Der nächste sichere Retention-Lauf prüft ihn nach Ende der "
                        "Bindung erneut."
                    )
                elif unclassified_count:
                    detail = (
                        f"{unclassified_count} nicht sicher klassifizierbare "
                        "Altbestände bleiben unverändert außerhalb der "
                        "verifizierten Rotation; sie zählen nicht als verifizierte "
                        "Backup-Familien."
                    )
                else:
                    detail = (
                        "Die sichere Bereinigung ist noch nicht vollständig "
                        "abgeschlossen und wird beim nächsten Retention-Lauf erneut "
                        "geprüft."
                    )
                _info_backup(
                    "Das neue verifizierte Backup bleibt gültig. " + detail
                )
        except Exception as exc:
            if options.retention_failure_is_fatal:
                raise
            _warn_backup(
                "Alte Backups konnten nicht vollständig bereinigt werden; "
                f"das neue verifizierte Backup bleibt gültig: {exc}"
            )
        count = len(manifest.get("files", []))
        print(f"  ✓ Manifest und SHA-256 für {count} Dateien verifiziert")
        log_task_completed("Backup erstellen", details=f"{count} Dateien in {backup_dir.name}")
        return str(backup_dir)
    except Exception as exc:
        log_error("backup", f"Verifiziertes Backup fehlgeschlagen: {exc}", exc)
        # Leave incomplete, non-manifested directories quarantined. Retention
        # ignores them; a recursive cleanup here would reopen a TOCTOU window.
        print(f"✗ Fehler beim Backup: {exc}\n")
        if options.raise_errors:
            raise
        return None


def backup_current_version(
    install_path=None,
    preserve_backup_paths=None,
    verified_pre_chown_callback=None,
    *,
    profile=None,
    expected_install_root_identity=None,
):
    """Create the only supported, complete, manifest-verified system backup."""
    options = _selected_backup_profile(profile, install_path or INSTALL_PATH)
    return _backup_current_version_v2(
        install_path=install_path,
        preserve_backup_paths=preserve_backup_paths,
        verified_pre_chown_callback=verified_pre_chown_callback,
        profile=options,
        expected_install_root_identity=expected_install_root_identity,
    )


def _quiesced_overlay_sources(install_path=None):
    """Kleine, nach Aktorruhe erneut zu versiegelnde Veränderungsfläche."""

    install = _lexical_absolute(install_path or INSTALL_PATH)
    repair_update = _bootstrap_repair_update_profile_bound(install)
    # Der versionsgebundene Restorevertrag erlaubt bewusst nur diese exakten
    # Ziele. Weitere Webroot-Legacydateien bleiben im verifizierten Vollbackup,
    # bis Backup- und Restorevertrag gemeinsam erweitert werden können.
    sources = [
        PersistentSource("install-config", install / "e3dc.config.txt"),
        PersistentSource("install-data", install / "data"),
        PersistentSource(
            "web-data",
            WEB_ROOT / "data",
            exclude_top_level=(LEGACY_ML_MODEL.name,),
        ),
        PersistentSource("web-history-backups", WEB_HISTORY_BACKUPS),
        PersistentSource(
            "system-state",
            Path("/var/lib/e3dc-control"),
            exclude_top_level=(("ml",) if repair_update else ()),
        ),
        PersistentSource("system-config", Path("/etc/e3dc-control")),
    ]
    sources.extend(
        PersistentSource("web-root-persistent", WEB_ROOT / name)
        for name in WEB_PERSISTENT_FILE_NAMES
    )
    return sources


def estimate_quiesced_overlay_size(install_path=None):
    """Schätzt die nach Dienststopp erneut zu sichernde mutable Fläche."""

    install = _lexical_absolute(install_path or INSTALL_PATH)
    return estimate_persistent_sources_size(_quiesced_overlay_sources(install))


def create_quiesced_overlay(
    overlay_dir,
    install_path=None,
    *,
    transaction_id,
    parent_backup_dir,
    parent_backup_id=None,
    expected_install_root_identity=None,
):
    """Versiegelt mutable Nutzerdaten nach bestätigtem Dienststopp.

    Das große Systembackup bleibt der vollständige Rückfall. Diese kleine,
    root-eigene ruhende Daten-Nachsicherung hält ausschließlich den späteren,
    ruhenden Stand der während des Live-Backups noch veränderbaren Daten fest.
    """

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise BackupIntegrityError(
            "Ruhende Daten-Nachsicherung darf ausschließlich Root erzeugen"
        )
    install = _lexical_absolute(install_path or INSTALL_PATH)
    transaction = _normalized_transaction_id(transaction_id)
    expected_parent_backup_id = (
        _normalized_backup_id(parent_backup_id, label="Parent-Backup-ID")
        if parent_backup_id is not None
        else None
    )
    target = _lexical_absolute(overlay_dir)
    parent_backup = _lexical_absolute(parent_backup_dir)
    collection = validate_existing_backup_root(parent_backup.parent, install)
    expected_target = collection / ".{}.quiesced-{}".format(
        parent_backup.name,
        transaction,
    )
    if (
        parent_backup.parent != collection
        or target.parent != collection
        or target != expected_target
    ):
        raise BackupIntegrityError(
            "Ziel der ruhenden Daten-Nachsicherung ist nicht an Vollbackup und Transaktion gebunden"
        )

    collection_descriptor = _open_root_controlled_backup_directory_chain(
        collection,
        leaf_mode=0o700,
    )
    parent_descriptor = None
    target_descriptor = None
    try:
        collection_metadata = os.fstat(collection_descriptor)
        parent_before = os.stat(
            parent_backup.name,
            dir_fd=collection_descriptor,
            follow_symlinks=False,
        )
        parent_descriptor = _open_root_controlled_backup_directory_chain(
            parent_backup,
            leaf_mode=0o700,
        )
        parent_metadata = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent_before.st_mode)
            or (parent_before.st_dev, parent_before.st_ino)
            != (parent_metadata.st_dev, parent_metadata.st_ino)
        ):
            raise BackupIntegrityError(
                "Parent-Backup wurde während der Bindung ausgetauscht"
            )
        parent_manifest = verify_backup(
            parent_backup,
            expected_kind=SYSTEM_BACKUP_KIND,
        )
        verified_parent_backup_id = _normalized_backup_id(
            parent_manifest.get("backup_id"),
            label="Parent-Backup-ID",
        )
        if (
            expected_parent_backup_id is not None
            and verified_parent_backup_id != expected_parent_backup_id
        ) or str(parent_manifest.get("install_root") or "") != str(install):
            raise BackupIntegrityError(
                "Parent-Backup stimmt nicht mit ID und Installationspfad überein"
            )
        expected_parent_backup_id = verified_parent_backup_id
        parent_manifest_sha256 = verified_manifest_sha256(
            parent_backup,
            expected_kind=SYSTEM_BACKUP_KIND,
            preverified_manifest=parent_manifest,
        )
        parent_after = os.stat(
            parent_backup.name,
            dir_fd=collection_descriptor,
            follow_symlinks=False,
        )
        if (parent_after.st_dev, parent_after.st_ino) != (
            parent_metadata.st_dev,
            parent_metadata.st_ino,
        ):
            raise BackupIntegrityError(
                "Parent-Backup driftete vor der ruhenden Daten-Nachsicherung"
            )
        try:
            os.stat(
                target.name,
                dir_fd=collection_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise BackupIntegrityError("Ziel der ruhenden Daten-Nachsicherung existiert bereits")
        os.mkdir(target.name, 0o700, dir_fd=collection_descriptor)
        os.fsync(collection_descriptor)
        target_before = os.stat(
            target.name,
            dir_fd=collection_descriptor,
            follow_symlinks=False,
        )
        target_descriptor = _open_root_controlled_backup_directory_chain(
            target,
            leaf_mode=0o700,
        )
        target_metadata = os.fstat(target_descriptor)
        if (
            not stat.S_ISDIR(target_before.st_mode)
            or (target_before.st_dev, target_before.st_ino)
            != (target_metadata.st_dev, target_metadata.st_ino)
        ):
            raise BackupIntegrityError(
                "Ruhende Daten-Nachsicherung wurde während der Anlage ausgetauscht"
            )

        with bind_persistent_source_root(
            install,
            expected_identity=expected_install_root_identity,
        ) as source_binding:
            mapped_entries, source_records = copy_persistent_sources(
                target,
                _quiesced_overlay_sources(install),
                bound_source_root=source_binding,
            )
        secure_backup_tree(target)
        manifest = finalize_backup(
            target,
            mapped_entries,
            source_records,
            kind=QUIESCED_OVERLAY_KIND,
            install_root=install,
            transaction_id=transaction,
            parent_backup_id=expected_parent_backup_id,
        )
        manifest_sha256 = verified_manifest_sha256(
            target,
            expected_kind=QUIESCED_OVERLAY_KIND,
            preverified_manifest=manifest,
        )
        os.fsync(target_descriptor)
        os.fsync(collection_descriptor)
        target_after = os.stat(
            target.name,
            dir_fd=collection_descriptor,
            follow_symlinks=False,
        )
        parent_final = os.stat(
            parent_backup.name,
            dir_fd=collection_descriptor,
            follow_symlinks=False,
        )
        if (
            (target_after.st_dev, target_after.st_ino)
            != (target_metadata.st_dev, target_metadata.st_ino)
            or (parent_final.st_dev, parent_final.st_ino)
            != (parent_metadata.st_dev, parent_metadata.st_ino)
        ):
            raise BackupIntegrityError(
                "Ruhende Daten-Nachsicherung oder Vollbackup driftete vor dem Receipt"
            )
        guard = QuiescedOverlayRestoreGuard(
            transaction_id=transaction,
            overlay_dir=str(target),
            overlay_dev=int(target_metadata.st_dev),
            overlay_ino=int(target_metadata.st_ino),
            backup_id=str(manifest["backup_id"]),
            manifest_sha256=manifest_sha256,
            install_root=str(install),
            parent_backup_dir=str(parent_backup),
            parent_backup_dev=int(parent_metadata.st_dev),
            parent_backup_ino=int(parent_metadata.st_ino),
            parent_backup_id=expected_parent_backup_id,
            parent_backup_manifest_sha256=parent_manifest_sha256,
            collection_dir=str(collection),
            collection_dev=int(collection_metadata.st_dev),
            collection_ino=int(collection_metadata.st_ino),
        )
        print(
            "  ✓ Ruhende Daten-Nachsicherung für {} Dateien verifiziert".format(
                len(manifest.get("files", []))
            )
        )
        return str(target), manifest, guard
    except Exception:
        # Eine unvollständige ruhende Daten-Nachsicherung bleibt absichtlich
        # root-privat für die Diagnose liegen. Ohne vollständiges Manifest
        # autorisiert sie niemals einen Restore.
        raise
    finally:
        if target_descriptor is not None:
            os.close(target_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        os.close(collection_descriptor)


def restore_quiesced_overlay(
    overlay_dir,
    install_path=None,
    *,
    guard: QuiescedOverlayRestoreGuard,
    restored_payload_guard=None,
    bound_install_root: BoundPersistentInstallRoot = None,
):
    """Spielt den verifizierten ruhenden Datenstand über das Vollbackup."""

    install = _lexical_absolute(install_path or INSTALL_PATH)
    if (
        not isinstance(guard, QuiescedOverlayRestoreGuard)
        or _lexical_absolute(guard.install_root) != install
        or _lexical_absolute(guard.overlay_dir) != _lexical_absolute(overlay_dir)
    ):
        raise BackupIntegrityError(
            "Wiederherstellung der ruhenden Daten-Nachsicherung widerspricht Pfad oder Installations-Guard"
        )
    if (
        bound_install_root is not None
        and (
            not isinstance(bound_install_root, BoundPersistentInstallRoot)
            or _lexical_absolute(bound_install_root.path) != install
        )
    ):
        raise BackupIntegrityError(
            "Ruhende Daten-Nachsicherung widerspricht der gebundenen "
            "Installationswurzel"
        )
    return restore_persistent_payload(
        overlay_dir,
        expected_kind=QUIESCED_OVERLAY_KIND,
        allowed_roots=(
            install / "data",
            WEB_ROOT / "data",
            WEB_HISTORY_BACKUPS,
            Path("/var/lib/e3dc-control"),
            Path("/etc/e3dc-control"),
        ),
        allowed_files=(
            install / "e3dc.config.txt",
            *(WEB_ROOT / name for name in WEB_PERSISTENT_FILE_NAMES),
        ),
        overlay_restore_guard=guard,
        restored_payload_guard=restored_payload_guard,
        bound_install_root=bound_install_root,
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
    allowed = frozenset({
        "enabled", "enabled-runtime", "disabled", "static", "indirect",
        "generated", "transient", "alias", "linked", "linked-runtime",
        "not-found", "masked", "masked-runtime",
    })

    for path in sorted(states):
        disk_state = _systemd_path_state(path)
        expected_masked = bool(states[path])
        if expected_masked != (disk_state == "masked"):
            raise BackupIntegrityError(
                f"Systemd-Maskenzustand weicht auf Platte ab "
                f"({disk_state!r}): {path}"
            )
        result = subprocess.run(
            ["systemctl", "is-enabled", path.name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        status = next((
            line.strip().lower()
            for stream in (result.stdout, result.stderr)
            for line in str(stream or "").splitlines()
            if line.strip().lower() in allowed
        ), "")
        show_result = None
        if not status:
            show_result = subprocess.run(
                [
                    "systemctl", "show", path.name,
                    "--property=LoadState", "--property=UnitFileState",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            fields = dict(
                line.split("=", 1)
                for line in str(show_result.stdout or "").splitlines()
                if "=" in line
            )
            unit_file_state = fields.get("UnitFileState", "").strip().lower()
            # ``states`` ist zuvor bereits exakt an die interne, feste
            # systemd-Positivliste gebunden. Fehlt eine dieser Units auf
            # Platte und soll sie ausdrücklich nicht maskiert sein, ist
            # ``not-found`` deshalb ein gültiger Zustand – auch für
            # katalogisierte Legacy-/Kompatibilitätsnamen wie e3dc.service.
            # Eine beliebige fremde Unit kann diesen Zweig nicht erreichen.
            optional_missing = (
                not expected_masked
                and disk_state == "missing"
            )
            if show_result.returncode == 0 and unit_file_state in allowed:
                status = unit_file_state
            elif (
                show_result.returncode == 0
                and optional_missing
                and fields.get("LoadState", "").strip().lower() == "not-found"
                and unit_file_state in {"", "not-found"}
            ):
                status = "not-found"
        if status not in allowed:
            detail = (
                f"is-enabled: stdout={str(result.stdout or '')!r}, "
                f"stderr={str(result.stderr or '')!r}, rc={result.returncode!r}"
            )
            if show_result is not None:
                detail += (
                    f"; show: stdout={str(show_result.stdout or '')!r}, "
                    f"stderr={str(show_result.stderr or '')!r}, "
                    f"rc={show_result.returncode!r}"
                )
            raise BackupIntegrityError(
                f"Systemd-Maskenzustand ist nicht lesbar: {path}; {detail}"
            )
        is_masked = status in {"masked", "masked-runtime"}
        if expected_masked != is_masked:
            raise BackupIntegrityError(
                f"Systemd meldet unerwarteten Maskenzustand {status!r}: {path}"
            )
        disk_state_after = _systemd_path_state(path)
        if disk_state_after != disk_state:
            raise BackupIntegrityError(
                f"Systemd-Maskenzustand driftete während der Prüfung "
                f"({disk_state!r} -> {disk_state_after!r}): {path}"
            )


def _restore_payload_with_mask_contract(
    backup_path,
    manifest,
    allowed_roots,
    allowed_files,
    *,
    verified_manifest_guard=None,
    restored_payload_guard=None,
    restore_metadata_overrides=None,
    bound_install_root: BoundPersistentInstallRoot = None,
):
    contract = manifest.get("systemd_mask_state")
    if contract is None:
        # Bestehende Schema-2-Backups hatten keinen Maskenvertrag. Sie bleiben
        # lesbar, autorisieren aber bewusst weder mask noch unmask. Dadurch wird
        # aus fehlender Alt-Evidenz kein erfundener Aktivierungszustand.
        return restore_persistent_payload(
            backup_path,
            allowed_roots=allowed_roots,
            allowed_files=allowed_files,
            restored_payload_guard=restored_payload_guard,
            restore_metadata_overrides=restore_metadata_overrides,
            verified_manifest_guard=verified_manifest_guard,
            bound_install_root=bound_install_root,
        )

    entries = _mask_entries_by_path(contract)
    expected = {path: entry["state"] == "masked" for path, entry in entries.items()}
    original = {path: _systemd_path_state(path) == "masked" for path in entries}
    if bound_install_root is not None:
        verify_bound_persistent_install_root(bound_install_root)
    try:
        for path in sorted(entries):
            if original[path]:
                _remove_canonical_systemd_mask(path)
        if bound_install_root is not None:
            verify_bound_persistent_install_root(bound_install_root)
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
        if bound_install_root is not None:
            verify_bound_persistent_install_root(bound_install_root)
        created = {}
        try:
            created = _apply_systemd_mask_states(expected)
            _reload_and_verify_systemd_mask_states(expected)
            if bound_install_root is not None:
                verify_bound_persistent_install_root(bound_install_root)
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
            restored_payload_guard=restored_payload_guard,
            restore_metadata_overrides=restore_metadata_overrides,
            verified_manifest_guard=verified_manifest_guard,
            bound_install_root=bound_install_root,
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


def restore_verified_backup(
    backup_path,
    install_path=None,
    verified_manifest_guard=None,
    restored_payload_guard=None,
    restore_metadata_overrides=None,
    *,
    bound_install_root: BoundPersistentInstallRoot = None,
):
    """Restore the complete manifested recovery surface without prompting."""
    manifest = verify_backup(backup_path, expected_kind=SYSTEM_BACKUP_KIND)
    if verified_manifest_guard is not None:
        if not callable(verified_manifest_guard):
            raise BackupIntegrityError("Restore-Manifestguard ist nicht aufrufbar")
        verified_manifest_guard(manifest)
    install = _lexical_absolute(install_path or INSTALL_PATH)
    if (
        bound_install_root is not None
        and (
            not isinstance(bound_install_root, BoundPersistentInstallRoot)
            or _lexical_absolute(bound_install_root.path) != install
        )
    ):
        raise BackupIntegrityError(
            "Vollrestore widerspricht der gebundenen Installationswurzel"
        )
    allowed_roots, allowed_files = _restore_allowlist(install)
    restored = _restore_payload_with_mask_contract(
        backup_path,
        manifest,
        allowed_roots,
        allowed_files,
        verified_manifest_guard=verified_manifest_guard,
        restored_payload_guard=restored_payload_guard,
        restore_metadata_overrides=restore_metadata_overrides,
        bound_install_root=bound_install_root,
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
        deleted = delete_verified_backup_family(
            backup_path,
            get_backup_root(),
            INSTALL_PATH,
        )
        overlay_count = int(deleted.get("removed_quiesced_overlays", 0))
        if overlay_count == 1:
            print(
                "✓ Backup einschließlich einer ruhenden Daten-Nachsicherung gelöscht.\n"
            )
        elif overlay_count:
            print(
                "✓ Backup einschließlich {} ruhender Daten-Nachsicherungen gelöscht.\n".format(
                    overlay_count
                )
            )
        else:
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
    try:
        retention = prune_install_backups(
            INSTALL_PATH,
            backup_root=get_backup_root(),
            logger=backup_logger,
            explicit_maintenance=True,
        )
    except Exception as exc:
        print(f"\n✗ Backup-Bereinigung sicher abgebrochen: {exc}\n")
        log_error("backup", f"Backup-Bereinigung sicher abgebrochen: {exc}", exc)
        return False
    removed_quiesced = len(
        retention.get("quiesced_overlays", {}).get("removed", [])
    )
    removed_update = len(retention.get("update_backups", {}).get("removed", []))
    removed_web = len(retention.get("web_installer_backups", {}).get("removed", []))
    skipped_quiesced = len(
        retention.get("quiesced_overlays", {}).get("skipped", [])
    )
    print("\n=== Backup-Limit ===")
    print(f"System-Backup-Familien maximal: {UPDATE_BACKUP_KEEP_COUNT}")
    print(f"Web-Installer-Sicherungen maximal: {WEB_INSTALLER_BACKUP_KEEP_COUNT}")
    print(f"Entfernte ruhende Daten-Nachsicherungen: {removed_quiesced}")
    print(f"Entfernte alte System-/Web-Backups: {removed_update + removed_web}")
    if retention.get("blocked"):
        print(
            "⚠ Bereinigung blockiert: {}".format(
                retention.get("blocker") or "Updateabschluss oder Recovery ist noch aktiv."
            )
        )
    elif skipped_quiesced:
        if skipped_quiesced == 1:
            print(
                "⚠ Eine nicht sicher klassifizierbare ruhende Daten-Nachsicherung blieb unangetastet."
            )
        else:
            print(
                "⚠ {} nicht sicher klassifizierbare ruhende Daten-Nachsicherungen blieben unangetastet.".format(
                    skipped_quiesced
                )
            )
    if retention.get("blocked"):
        print("⚠ Es wurden keine weiteren Backups rotiert.")
    elif not retention.get("success", True):
        print("⚠ Einzelne Backups konnten nicht entfernt werden. Details stehen im Installer-Log.")
    elif skipped_quiesced:
        print(
            "⚠ Die Systembackup-Rotation wurde deshalb aus Sicherheitsgründen nicht ausgeführt."
        )
    elif not retention.get("limit_satisfied", False):
        print(
            "⚠ Das Limit ist wegen einer laufenden Schutz- oder Recovery-Bindung "
            "noch nicht vollständig erreicht; geschützte Backups bleiben erhalten."
        )
    else:
        print("✓ Backup-Limit angewendet.")
    print("")
    return bool(
        retention.get("success", True)
        and not retention.get("blocked")
        and retention.get("limit_satisfied", False)
        and not skipped_quiesced
    )


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
