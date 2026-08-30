import os
import pwd
import grp
import subprocess
import logging
import tempfile
import json
import sys
import time
import shlex
import stat
import inspect
import hashlib
import types
import re

# Standard-Ausgabe auf UTF-8 erzwingen
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
    else:
        sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from Installer.core import CAT_ENV, register_command
    from Installer.utils import run_command
    from Installer.installer_config import CONFIG_FILE, get_install_path, get_install_user, get_home_dir, get_venv_path, get_www_data_gid, load_config
    from Installer.logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning
    from Installer.config_manager import run_config_wizard
    from Installer.config_secret_permissions import (
        config_secret_dir_mode,
        config_secret_dir_mode_text,
        config_secret_file_mode_text,
    )
    from Installer.service_catalog import allowed_services, iter_modules
    from Installer.optional_service_contract import (
        configured_optional_services,
        preinstalled_optional_service_expected,
    )
    from Installer.ramdisk_guard import probe_ramdisk_tmpfs
    from Installer import backup_integrity as _backup_integrity
else:
    from .core import CAT_ENV, register_command
    from .utils import run_command
    from .installer_config import CONFIG_FILE, get_install_path, get_install_user, get_home_dir, get_venv_path, get_www_data_gid, load_config
    from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning
    from .config_manager import run_config_wizard
    from .config_secret_permissions import (
        config_secret_dir_mode,
        config_secret_dir_mode_text,
        config_secret_file_mode_text,
    )
    from .service_catalog import allowed_services, iter_modules
    from .optional_service_contract import (
        configured_optional_services,
        preinstalled_optional_service_expected,
    )
    from .ramdisk_guard import probe_ramdisk_tmpfs
    from . import backup_integrity as _backup_integrity

BackupIntegrityError = _backup_integrity.BackupIntegrityError
PRIVATE_ML_ROOT = _backup_integrity.PRIVATE_ML_ROOT
_open_regular_file_nofollow = _backup_integrity._open_regular_file_nofollow
validate_private_ml_store = _backup_integrity.validate_private_ml_store
# Ein Self-Update aus älteren Releases kann dieses Modul nach dem Git-Wechsel
# in demselben Python-Prozess laden, während ``backup_integrity`` noch aus der
# alten Generation im Modulcache liegt. Die neue Reparaturfunktion ist deshalb
# optional; ein Altvalidator bleibt strikt read-only und fail-closed.
normalize_private_ml_lock_metadata = getattr(
    _backup_integrity,
    "normalize_private_ml_lock_metadata",
    None,
)

INSTALL_USER = get_install_user()
INSTALL_HOME = get_home_dir(INSTALL_USER)
try:
    INSTALL_GROUP = grp.getgrgid(pwd.getpwnam(INSTALL_USER).pw_gid).gr_name
except (KeyError, OSError):
    INSTALL_GROUP = INSTALL_USER
INSTALL_PATH = os.path.abspath(get_install_path())
# In einem Release-Bootstrap wird dieses Modul aus einem versiegelten
# Ausführungssnapshot importiert. ``__file__`` bezeichnet dann ausschließlich
# den unveränderlichen Code-Root; alle Rechteprüfungen und Korrekturen müssen
# weiterhin auf den explizit gebundenen Produktbaum zeigen.
INSTALL_ROOT = INSTALL_PATH
INSTALLER_DIR = os.path.join(INSTALL_ROOT, "Installer")
CONFIG_SECRET_FILE_MODE = config_secret_file_mode_text()
CONFIG_SECRET_DIR_MODE = config_secret_dir_mode_text()

LEGACY_E3DC_SERVICE = "e3dc"
NATIVE_LIVE_SERVICE = "e3dc-live"
LEGACY_SCREEN_NAMES = ("e3dc", "E3DC")
STORAGE_MANAGER_SERVICE = "e3dc-storage-manager"
STORAGE_MANAGER_CANONICAL_SCRIPT = "storage_manager.py"
STORAGE_MANAGER_LEGACY_SCRIPT = "storage_manager_legacy.py"
STORAGE_MANAGER_LEGACY_SCRIPTS = (
    "storage_manager_next.py",
    STORAGE_MANAGER_LEGACY_SCRIPT,
)
PI_GUARD_PATH = "/usr/local/bin/pi_guard.sh"
PIGUARD_SERVICE = "/etc/systemd/system/piguard.service"
WATCHDOG_UPDATE_PAUSE_FILE = "/var/www/html/ramdisk/watchdog.update_pause"
WALLBOX_PLAN_JOB_ROOT = "/var/www/html/data/.wallbox_plan_jobs"
MATTER_RESET_QUARANTINE_NAME = ".matter-storage-reset-quarantine"
MATTER_RESET_QUARANTINE_PREPARE_NAME = ".matter-storage-reset-quarantine.prepare"
MATTER_RESET_RECEIPT_NAME = ".e3dc-matter-reset-transaction.json"
MATTER_RESET_STAGE_PREFIX = ".matter-storage-reset-stage-"
MATTER_RESET_PROTECTED_DATA_NAMES = frozenset(
    {
        MATTER_RESET_QUARANTINE_NAME,
        MATTER_RESET_QUARANTINE_PREPARE_NAME,
        MATTER_RESET_RECEIPT_NAME,
    }
)
MATTER_RESET_PROTECTED_DATA_PREFIXES = (MATTER_RESET_STAGE_PREFIX,)
WALLBOX_MODE5_USER_START_REQUEST_FILE = (
    "/var/www/html/data/wallbox_mode5_user_start_request.json"
)
HA_MANAGED_SERVICES = (
    "e3dc", "e3dc-live", "e3dc-storage-manager", "e3dc-storage-simulator",
    "e3dc-epex-manager", "e3dc-weather-manager", "e3dc-forecast-evidence", "e3dc-wallbox-manager",
    "energy_manager", "e3dc-idm-live", "e3dc-lux-live", "e3dc-stiebel-live", "e3dc-dimplex-live", "e3dc-heizstab", "e3dc-climate-live", "e3dc-climate-control",
    "e3dc-mqtt-hub", "e3dc-bluelink", "e3dc-matter-bridge", "e3dc-notifier", "e3dc-websocket", "e3dc-shadow-sync",
)
FORECAST_EVIDENCE_BASE = "/var/lib/e3dc-control"
FORECAST_EVIDENCE_ROOT = f"{FORECAST_EVIDENCE_BASE}/forecast-evidence"
FORECAST_EVIDENCE_PRIVATE_FILES = (
    f"{FORECAST_EVIDENCE_ROOT}/pv_forecast_evidence.db",
    f"{FORECAST_EVIDENCE_ROOT}/writer.lock",
)


CATALOG_FALLBACK_SERVICES = tuple(srv for srv in HA_MANAGED_SERVICES if srv != "e3dc") + ("e3dc-ha",)


def _catalog_service_names(include_legacy=False, exclude=None):
    """Liefert Dienstnamen ohne .service aus dem zentralen Service-Katalog."""
    excluded = set(exclude or ())
    names = []
    if include_legacy and "e3dc" not in excluded:
        names.append("e3dc")
    try:
        service_units = allowed_services()
    except Exception:
        service_units = tuple(f"{name}.service" for name in CATALOG_FALLBACK_SERVICES)
    for unit in service_units:
        name = str(unit or "").strip()
        if name.endswith(".service"):
            name = name[:-8]
        if not name or name in excluded or name in names:
            continue
        names.append(name)
    return tuple(names)


HA_MANAGED_SERVICES = _catalog_service_names(include_legacy=True, exclude={"e3dc-ha"})
SHADOW_MANAGED_SERVICES = _catalog_service_names(include_legacy=True, exclude={"e3dc-shadow-sync"})

def _ml_directory_snapshot(path):
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return {"exists": False, "path": path}
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise BackupIntegrityError(f"Privater ML-Pfad ist kein echtes Verzeichnis: {path}")
    return {
        "exists": True,
        "path": path,
        "uid": int(metadata.st_uid),
        "gid": int(metadata.st_gid),
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _rollback_ml_directories(snapshots):
    success = True
    for snapshot in reversed(snapshots):
        path = shlex.quote(str(snapshot["path"]))
        if snapshot["exists"]:
            owner = f"{int(snapshot['uid'])}:{int(snapshot['gid'])}"
            owner_result = run_command(f"sudo chown {owner} -- {path}", timeout=15)
            mode_result = run_command(f"sudo chmod {int(snapshot['mode']):04o} -- {path}", timeout=15)
            success = bool(owner_result.get("success")) and bool(mode_result.get("success")) and success
        else:
            result = run_command(f"sudo rmdir -- {path}", timeout=15)
            success = bool(result.get("success")) and success
    return success


def _validate_private_ml_store_for_permissions(expected_uid, expected_gid):
    """Validiert den ML-Store auch mit einem gecachten Altvertrag strikt."""

    validator_parameters = inspect.signature(validate_private_ml_store).parameters
    repairable_lock_supported = "allow_repairable_lock" in validator_parameters
    validator_kwargs = {
        "expected_uid": expected_uid,
        "allow_missing": False,
    }
    if repairable_lock_supported:
        validator_kwargs["allow_repairable_lock"] = True
    ml_preflight = validate_private_ml_store(
        PRIVATE_ML_ROOT,
        **validator_kwargs,
    )
    if ml_preflight.get("repairable_lock"):
        if not callable(normalize_private_ml_lock_metadata):
            raise BackupIntegrityError(
                "ML-Sperrdatei benötigt eine Metadatenreparatur, "
                "aber der geladene Altvertrag unterstützt sie nicht"
            )
        normalize_private_ml_lock_metadata(
            PRIVATE_ML_ROOT,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
    validate_private_ml_store(
        PRIVATE_ML_ROOT,
        expected_uid=expected_uid,
        allow_missing=False,
    )


def ensure_private_ml_model_store():
    """Repariert die privaten ML-Verzeichnisse und nur die bekannte Sperrdatei."""

    try:
        account = pwd.getpwnam(INSTALL_USER)
        if account.pw_name == "www-data":
            raise BackupIntegrityError("Privater ML-Store darf nicht dem Web-Benutzer gehoeren")
        group_name = grp.getgrgid(account.pw_gid).gr_name
        for ancestor in ("/var", "/var/lib"):
            metadata = os.lstat(ancestor)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise BackupIntegrityError(f"Unsichere Elternkomponente fuer ML-Store: {ancestor}")
        base = str(PRIVATE_ML_ROOT.parent)
        root = str(PRIVATE_ML_ROOT)
        snapshots = [_ml_directory_snapshot(base), _ml_directory_snapshot(root)]
    except Exception as exc:
        perm_logger.error("Privater ML-Store konnte nicht vorbereitet werden: %s", exc)
        return False


    commands = (
        "sudo install -d -o root -g root -m 0711 -- {}".format(shlex.quote(base)),
        "sudo install -d -o {} -g {} -m 0700 -- {}".format(
            shlex.quote(account.pw_name),
            shlex.quote(group_name),
            shlex.quote(root),
        ),
    )
    try:
        for command in commands:
            result = run_command(command, timeout=20)
            if not result.get("success"):
                raise BackupIntegrityError("Privates ML-Verzeichnis konnte nicht transaktional angelegt werden")
        base_metadata = os.lstat(base)
        root_gid = grp.getgrnam("root").gr_gid
        if (
            stat.S_ISLNK(base_metadata.st_mode)
            or not stat.S_ISDIR(base_metadata.st_mode)
            or base_metadata.st_uid != 0
            or base_metadata.st_gid != root_gid
            or stat.S_IMODE(base_metadata.st_mode) != 0o711
        ):
            raise BackupIntegrityError("ML-Basisverzeichnis besitzt nicht root:root 0711")
        _validate_private_ml_store_for_permissions(
            account.pw_uid,
            account.pw_gid,
        )
        perm_logger.info("Privater ML-Store ist manifestgebunden und nicht webbeschreibbar.")
        return True
    except Exception as exc:
        rollback_ok = _rollback_ml_directories(snapshots)
        if not rollback_ok:
            perm_logger.error("ML-Verzeichnisrollback unvollstaendig; ML bleibt gesperrt: %s", exc)
        else:
            perm_logger.error("ML-Verzeichnisvorbereitung zurueckgerollt: %s", exc)
        return False


def ensure_private_forecast_evidence_store():
    """Bindet den Diagnosezustand an einen privaten Ein-Writer-Pfad."""

    try:
        account = pwd.getpwnam(INSTALL_USER)
        if account.pw_name == "www-data":
            raise BackupIntegrityError(
                "Private Rohdaten der Prognosediagnose dürfen nicht dem Web-Benutzer gehören"
            )
        group_name = grp.getgrgid(account.pw_gid).gr_name
        for ancestor in ("/var", "/var/lib"):
            metadata = os.lstat(ancestor)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise BackupIntegrityError(
                    f"Unsichere Elternkomponente für Prognosediagnose: {ancestor}"
                )
        if os.path.lexists(FORECAST_EVIDENCE_BASE):
            base_metadata = os.lstat(FORECAST_EVIDENCE_BASE)
            if stat.S_ISLNK(base_metadata.st_mode) or not stat.S_ISDIR(base_metadata.st_mode):
                raise BackupIntegrityError("Unsicheres E3DC-Zustandsverzeichnis")
        if os.path.lexists(FORECAST_EVIDENCE_ROOT):
            root_metadata = os.lstat(FORECAST_EVIDENCE_ROOT)
            if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
                raise BackupIntegrityError("Unsicheres Prognosediagnose-Verzeichnis")
    except Exception as exc:
        perm_logger.error("Private Prognosediagnose konnte nicht geprüft werden: %s", exc)
        return False

    commands = (
        "sudo install -d -o root -g root -m 0711 -- {}".format(
            shlex.quote(FORECAST_EVIDENCE_BASE)
        ),
        "sudo install -d -o {} -g {} -m 0700 -- {}".format(
            shlex.quote(account.pw_name),
            shlex.quote(group_name),
            shlex.quote(FORECAST_EVIDENCE_ROOT),
        ),
    )
    try:
        for command in commands:
            result = run_command(command, timeout=20)
            if not result.get("success"):
                raise BackupIntegrityError(
                    "Privates Prognosediagnose-Verzeichnis konnte nicht angelegt werden"
                )
        root_metadata = os.lstat(FORECAST_EVIDENCE_ROOT)
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != account.pw_uid
            or root_metadata.st_gid != account.pw_gid
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise BackupIntegrityError(
                "Prognosediagnose-Verzeichnis besitzt nicht Nutzergruppe 0700"
            )
        for path in FORECAST_EVIDENCE_PRIVATE_FILES:
            if not os.path.lexists(path):
                continue
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise BackupIntegrityError(
                    f"Unsichere private Prognosediagnose-Datei: {path}"
                )
            result = run_command(
                "sudo chown {}:{} -- {} && sudo chmod 0600 -- {}".format(
                    shlex.quote(account.pw_name),
                    shlex.quote(group_name),
                    shlex.quote(path),
                    shlex.quote(path),
                ),
                timeout=20,
            )
            if not result.get("success"):
                raise BackupIntegrityError(
                    f"Private Prognosediagnose-Datei konnte nicht gehärtet werden: {path}"
                )
        perm_logger.info(
            "Private Prognosediagnose ist außerhalb des Webverzeichnisses gebunden."
        )
        return True
    except Exception as exc:
        perm_logger.error(
            "Private Prognosediagnose konnte nicht vorbereitet werden: %s",
            exc,
        )
        return False

# ANSI Colors
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
RESET = '\033[0m'


def refresh_watchdog_guard_script(*, start_service=True):
    """Aktualisiert das installierte piguard-Skript, ohne neue Nutzerkonfiguration zu erfinden."""
    if not isinstance(start_service, bool):
        print(f"{RED}[!]{RESET} Watchdog-Startmodus ist nicht eindeutig.")
        return False
    if not (os.path.exists(PI_GUARD_PATH) or os.path.exists(PIGUARD_SERVICE)):
        return True
    try:
        if __package__ in (None, ""):
            from Installer.install_watchdog import get_current_config, create_pi_guard
        else:
            from .install_watchdog import get_current_config, create_pi_guard

        current = get_current_config()
        router_ip = str(current.get("ROUTER_IP") or "").strip()
        if not router_ip:
            print(f"{RED}[!]{RESET} Watchdog-Skript nicht verändert: keine Router-IP konfiguriert.")
            log_warning("permissions", "Watchdog-Skript nicht verändert: keine Router-IP konfiguriert.")
            return False
        monitor_file = current.get("MONITOR_FILE") or ""
        if create_pi_guard(
            router_ip,
            monitor_file,
            start_service=start_service,
        ) is not True:
            raise RuntimeError("Watchdog-Bundle-Transaktion meldet keinen Erfolg")
        suffix = " und inaktiv gehalten" if not start_service else ""
        print(f"{GREEN}[OK]{RESET} Watchdog-Skript aktualisiert{suffix}.")
        return True
    except Exception as exc:
        print(f"{RED}[!]{RESET} Watchdog-Skript konnte nicht aktualisiert werden: {exc}")
        log_warning("permissions", f"Watchdog-Skript konnte nicht aktualisiert werden: {exc}")
        return False


def _systemd_unit_exists(service_name):
    """True, wenn die systemd-Unit lokal vorhanden ist."""
    unit = service_name if service_name.endswith(".service") else f"{service_name}.service"
    return (
        os.path.exists(f"/etc/systemd/system/{unit}")
        or os.path.exists(f"/lib/systemd/system/{unit}")
        or os.path.exists(f"/usr/lib/systemd/system/{unit}")
    )


def _parse_systemd_show_properties(raw):
    properties = {}
    for line in str(raw or "").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip():
            properties[key.strip()] = value.strip()
    return properties


def _storage_writer_process_snapshot(proc_root="/proc"):
    """Liest nur exakte Python-Skriptnamen aus dem aktuellen /proc-Snapshot."""
    canonical_pids = []
    legacy_pids = []
    errors = []
    try:
        entries = list(os.scandir(proc_root))
    except OSError as exc:
        return {
            "complete": False,
            "canonical_pids": [],
            "legacy_pids": [],
            "errors": [str(exc)],
        }

    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            with open(
                os.path.join(proc_root, entry.name, "cmdline"),
                "rb",
            ) as handle:
                payload = handle.read(64 * 1024 + 1)
        except FileNotFoundError:
            # Ein zwischen Auflistung und Lesen beendeter Prozess ist kein
            # fortbestehender Writer und deshalb kein unvollständiger Befund.
            continue
        except OSError as exc:
            errors.append(f"pid={pid}: {exc}")
            continue
        if len(payload) > 64 * 1024:
            errors.append(f"pid={pid}: cmdline_unplausibly_large")
            continue
        names = {
            os.path.basename(token.decode("utf-8", errors="replace"))
            for token in payload.split(b"\0")
            if token
        }
        if STORAGE_MANAGER_CANONICAL_SCRIPT in names:
            canonical_pids.append(pid)
        if any(script_name in names for script_name in STORAGE_MANAGER_LEGACY_SCRIPTS):
            legacy_pids.append(pid)

    return {
        "complete": not errors,
        "canonical_pids": sorted(set(canonical_pids)),
        "legacy_pids": sorted(set(legacy_pids)),
        "errors": errors[:8],
    }


def storage_manager_writer_contract(
    *,
    command_runner=None,
    proc_root="/proc",
    unit_exists=None,
    require_canonical_unit=True,
):
    """Bindet den einzigen zulässigen Storage-Writer an Unit und MainPID."""
    runner = command_runner or run_command
    unit_present = (
        _systemd_unit_exists(STORAGE_MANAGER_SERVICE)
        if unit_exists is None
        else bool(unit_exists)
    )
    blockers = []
    properties = {}

    if unit_present:
        result = runner(
            "systemctl show -p LoadState -p ActiveState -p MainPID -p ExecStart "
            f"{STORAGE_MANAGER_SERVICE}.service"
        )
        if not result.get("success"):
            blockers.append("effective_unit_contract_unreadable")
        else:
            properties = _parse_systemd_show_properties(result.get("stdout"))
            exec_start = properties.get("ExecStart", "")
            canonical_path = os.path.realpath(
                os.path.join(INSTALLER_DIR, STORAGE_MANAGER_CANONICAL_SCRIPT)
            )
            canonical_bound = bool(
                re.search(
                    re.escape(canonical_path) + r'(?=$|[\s;\}\]\"])',
                    exec_start,
                )
            )
            if properties.get("LoadState") != "loaded":
                blockers.append("storage_unit_not_loaded")
            if require_canonical_unit and any(
                script_name in exec_start
                for script_name in STORAGE_MANAGER_LEGACY_SCRIPTS
            ):
                blockers.append("legacy_execstart")
            if require_canonical_unit and not canonical_bound:
                blockers.append("effective_execstart_not_canonical")

    processes = _storage_writer_process_snapshot(proc_root)
    canonical_pids = list(processes.get("canonical_pids") or [])
    legacy_pids = list(processes.get("legacy_pids") or [])
    if not processes.get("complete"):
        blockers.append("storage_process_snapshot_incomplete")
    if legacy_pids:
        blockers.append("legacy_storage_process_running")
    if len(canonical_pids) > 1:
        blockers.append("multiple_native_storage_processes")
    if not unit_present and canonical_pids:
        blockers.append("native_storage_process_without_unit")

    active_state = properties.get("ActiveState", "")
    try:
        main_pid = int(properties.get("MainPID") or 0)
    except (TypeError, ValueError):
        main_pid = 0
    if unit_present and active_state == "active":
        if main_pid <= 1 or canonical_pids != [main_pid]:
            blockers.append("active_unit_mainpid_not_only_native_writer")
    elif unit_present and active_state in {
        "activating",
        "deactivating",
        "reloading",
    }:
        blockers.append("storage_unit_state_transitional")
    elif canonical_pids:
        blockers.append("native_storage_process_outside_active_unit")

    blockers = list(dict.fromkeys(blockers))
    return {
        "schema_version": "storage_manager_writer_contract_v1",
        "ok": not blockers,
        "status": "canonical_single_writer" if not blockers else "blocked",
        "unit_present": bool(unit_present),
        "unit_active_state": active_state or None,
        "unit_main_pid": main_pid or None,
        "canonical_process_pids": canonical_pids,
        "legacy_process_pids": legacy_pids,
        "process_snapshot_complete": bool(processes.get("complete")),
        "blockers": blockers,
    }


def _is_v4_native_mode():
    """V4 nutzt e3dc-live; der alte e3dc.service ist dann nur Legacy-Cleanup."""
    if _systemd_unit_exists(NATIVE_LIVE_SERVICE):
        return True
    res_active = run_command(f"systemctl is-active {NATIVE_LIVE_SERVICE}")
    res_enabled = run_command(f"systemctl is-enabled {NATIVE_LIVE_SERVICE}")
    return (
        res_active["stdout"].strip() == "active"
        or res_enabled["stdout"].strip() in ("enabled", "static")
    )


def _legacy_screen_sessions_active():
    """True, wenn alte E3DC screen-Sessions laufen."""
    for cmd in (f"sudo -u {INSTALL_USER} screen -ls", "sudo screen -ls"):
        res = run_command(f"{cmd} 2>/dev/null")
        if not res["success"]:
            continue
        for line in res["stdout"].splitlines():
            if any(f".{name}" in line for name in LEGACY_SCREEN_NAMES):
                return True
    return False


def _legacy_e3dc_process_active():
    """True, wenn der alte C++-Prozess direkt laeuft."""
    res_bin = run_command("pgrep -x E3DC-Control")
    if res_bin["success"] and res_bin["stdout"].strip():
        return True
    res_script = run_command(r"pgrep -f '(^|/)E3DC\.sh([[:space:]]|$)'")
    return res_script["success"] and res_script["stdout"].strip() != ""


def _cleanup_legacy_e3dc():
    """Beendet alte C++-Altlasten, startet sie aber niemals wieder."""
    if _systemd_unit_exists(LEGACY_E3DC_SERVICE):
        run_command(f"sudo systemctl stop {LEGACY_E3DC_SERVICE} 2>/dev/null")
        run_command(f"sudo systemctl disable {LEGACY_E3DC_SERVICE} 2>/dev/null")
        run_command(f"sudo systemctl mask {LEGACY_E3DC_SERVICE} 2>/dev/null")

    for screen_name in LEGACY_SCREEN_NAMES:
        run_command(f"sudo -u {INSTALL_USER} screen -S {screen_name} -X quit 2>/dev/null")
        run_command(f"sudo screen -S {screen_name} -X quit 2>/dev/null")

    run_command("sudo pkill -x E3DC-Control 2>/dev/null")
    run_command(r"sudo pkill -f '(^|/)E3DC\.sh([[:space:]]|$)' 2>/dev/null")

def _strip_utf8_bom(path):
    """Entfernt UTF-8 BOM, falls vorhanden."""
    try:
        with open(path, "rb") as f:
            content = f.read()
        bom = b"\xef\xbb\xbf"
        if content.startswith(bom):
            with open(path, "wb") as f:
                f.write(content[len(bom):])
            print(f"  {GREEN}✓{RESET} BOM entfernt: {os.path.basename(path)}")
    except Exception:
        pass


def setup_permissions_logging():
    """Initialisiert Logging für Berechtigungen über logging_manager."""
    log_dir = os.path.join(INSTALL_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "permissions.log")
    perm_logger = get_or_create_logger("permissions", log_file)
    return perm_logger


perm_logger = setup_permissions_logging()


def _install_user_in_www_data_group() -> bool:
    """True, wenn der Install-User Dateien mit Gruppe www-data lesen kann."""
    try:
        user = pwd.getpwnam(INSTALL_USER)
        www_data = grp.getgrnam("www-data")
        return user.pw_gid == www_data.gr_gid or INSTALL_USER in www_data.gr_mem
    except Exception:
        return True


def _web_config_allowed_owners() -> set[str]:
    """Erlaubt den atomaren Web-Publisher nur bei bestätigter Gruppenbindung."""
    owners = {INSTALL_USER}
    try:
        user = pwd.getpwnam(INSTALL_USER)
        www_data = grp.getgrnam("www-data")
        if user.pw_gid == www_data.gr_gid or INSTALL_USER in www_data.gr_mem:
            owners.add("www-data")
    except Exception:
        pass
    return owners


def _ensure_install_user_www_data_group() -> bool:
    """Nimmt den Install-User in www-data auf, damit 660-Config lesbar bleibt."""
    if _install_user_in_www_data_group():
        return True
    result = run_command(f"sudo usermod -aG www-data {shlex.quote(str(INSTALL_USER))}")
    if result["success"]:
        perm_logger.info("%s zur Gruppe www-data hinzugefügt.", INSTALL_USER)
        return True
    perm_logger.error("%s konnte nicht zur Gruppe www-data hinzugefügt werden: %s", INSTALL_USER, result["stderr"])
    return False


def _open_absolute_directory_nofollow(path):
    """Öffnet jede Komponente eines absoluten Pfads ohne Symlink-Folge."""

    normalized = os.path.normpath(os.path.abspath(str(path)))
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory or not normalized.startswith(os.sep):
        raise RuntimeError("Sichere Verzeichnisbindung ist nicht verfügbar")
    descriptor = os.open(os.sep, flags)
    try:
        for component in [part for part in normalized.split(os.sep) if part]:
            named = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode):
                raise RuntimeError(
                    f"Pfadkomponente ist kein echtes Verzeichnis: {normalized}"
                )
            child = os.open(component, flags, dir_fd=descriptor)
            opened = os.fstat(child)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (named.st_dev, named.st_ino)
            ):
                os.close(child)
                raise RuntimeError(
                    f"Pfadkomponente driftete beim Öffnen: {normalized}"
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _set_live_directory_metadata(path, *, uid, gid, mode=0o755):
    """Ändert ein komponentenweise gebundenes Verzeichnis, nie seinen Inhalt."""

    normalized = os.path.normpath(os.path.abspath(str(path)))
    parent_path, name = os.path.split(normalized)
    if not name:
        raise RuntimeError("Dateisystemwurzel ist kein zulässiges Rechteziel")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    parent = _open_absolute_directory_nofollow(parent_path)
    descriptor = -1
    rebound_parent = -1
    changed = False
    before = None
    try:
        parent_identity = os.fstat(parent)
        named_before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=parent)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(before.st_mode)
            or not stat.S_ISDIR(named_before.st_mode)
            or (before.st_dev, before.st_ino)
            != (named_before.st_dev, named_before.st_ino)
        ):
            raise RuntimeError(f"Rechteziel ist kein echtes Verzeichnis: {normalized}")
        os.fchown(descriptor, int(uid), int(gid))
        changed = True
        os.fchmod(descriptor, int(mode))
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent, follow_symlinks=False)
        rebound_parent = _open_absolute_directory_nofollow(parent_path)
        rebound_parent_metadata = os.fstat(rebound_parent)
        rebound_named = os.stat(
            name,
            dir_fd=rebound_parent,
            follow_symlinks=False,
        )
        if (
            (rebound_parent_metadata.st_dev, rebound_parent_metadata.st_ino)
            != (parent_identity.st_dev, parent_identity.st_ino)
            or not stat.S_ISDIR(after.st_mode)
            or not stat.S_ISDIR(named_after.st_mode)
            or not stat.S_ISDIR(rebound_named.st_mode)
            or (after.st_dev, after.st_ino)
            != (before.st_dev, before.st_ino)
            or (named_after.st_dev, named_after.st_ino)
            != (after.st_dev, after.st_ino)
            or (rebound_named.st_dev, rebound_named.st_ino)
            != (after.st_dev, after.st_ino)
            or after.st_uid != int(uid)
            or after.st_gid != int(gid)
            or stat.S_IMODE(after.st_mode) != int(mode)
            or named_after.st_uid != int(uid)
            or named_after.st_gid != int(gid)
            or stat.S_IMODE(named_after.st_mode) != int(mode)
            or rebound_named.st_uid != int(uid)
            or rebound_named.st_gid != int(gid)
            or stat.S_IMODE(rebound_named.st_mode) != int(mode)
        ):
            raise RuntimeError(f"Verzeichnisrechte blieben abweichend: {normalized}")
    except Exception as original_error:
        if changed and descriptor >= 0 and before is not None:
            rollback_error = None
            try:
                os.fchown(descriptor, before.st_uid, before.st_gid)
                os.fchmod(descriptor, stat.S_IMODE(before.st_mode))
                os.fsync(descriptor)
                restored = os.fstat(descriptor)
                restore_parent = _open_absolute_directory_nofollow(parent_path)
                try:
                    restore_parent_identity = os.fstat(restore_parent)
                    restored_named = os.stat(
                        name,
                        dir_fd=restore_parent,
                        follow_symlinks=False,
                    )
                    if (
                        (restore_parent_identity.st_dev, restore_parent_identity.st_ino)
                        != (parent_identity.st_dev, parent_identity.st_ino)
                        or (restored.st_dev, restored.st_ino)
                        != (before.st_dev, before.st_ino)
                        or (restored_named.st_dev, restored_named.st_ino)
                        != (before.st_dev, before.st_ino)
                        or restored.st_uid != before.st_uid
                        or restored.st_gid != before.st_gid
                        or stat.S_IMODE(restored.st_mode)
                        != stat.S_IMODE(before.st_mode)
                        or restored_named.st_uid != before.st_uid
                        or restored_named.st_gid != before.st_gid
                        or stat.S_IMODE(restored_named.st_mode)
                        != stat.S_IMODE(before.st_mode)
                    ):
                        raise RuntimeError(
                            f"Verzeichnisrechte wurden nicht vollständig restauriert: {normalized}"
                        )
                finally:
                    os.close(restore_parent)
            except Exception as exc:
                rollback_error = exc
            if rollback_error is not None:
                raise RuntimeError(
                    "Die Verzeichnisrechte-Projektion scheiterte und ihr lokaler "
                    f"Rückfall blieb unvollständig: {normalized} ({rollback_error})"
                ) from original_error
        raise
    finally:
        if rebound_parent >= 0:
            os.close(rebound_parent)
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _set_live_regular_file_metadata(path, *, uid, gid, mode):
    """Projiziert Metadaten auf eine komponentenweise gebundene Einzeldatei."""

    normalized = os.path.normpath(os.path.abspath(str(path)))
    parent_path, name = os.path.split(normalized)
    if not name:
        raise RuntimeError("Dateisystemwurzel ist kein zulässiges Rechteziel")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = (
        os.O_RDONLY
        | nofollow
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if not nofollow:
        raise RuntimeError("Sichere Dateibindung ist nicht verfügbar")
    parent = _open_absolute_directory_nofollow(parent_path)
    descriptor = -1
    rebound_parent = -1
    changed = False
    before = None
    try:
        parent_identity = os.fstat(parent)
        named_before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(named_before.st_mode) or named_before.st_nlink != 1:
            raise RuntimeError(f"Rechteziel ist keine reguläre Einzeldatei: {normalized}")
        descriptor = os.open(name, flags, dir_fd=parent)
        before = os.fstat(descriptor)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_nlink,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino)
            != (named_before.st_dev, named_before.st_ino)
        ):
            raise RuntimeError(f"Rechteziel driftete beim Öffnen: {normalized}")
        os.fchown(descriptor, int(uid), int(gid))
        changed = True
        os.fchmod(descriptor, int(mode))
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent, follow_symlinks=False)
        rebound_parent = _open_absolute_directory_nofollow(parent_path)
        rebound_parent_metadata = os.fstat(rebound_parent)
        rebound_named = os.stat(
            name,
            dir_fd=rebound_parent,
            follow_symlinks=False,
        )
        if (
            (rebound_parent_metadata.st_dev, rebound_parent_metadata.st_ino)
            != (parent_identity.st_dev, parent_identity.st_ino)
            or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_nlink,
            )
            != stable
            or (
                named_after.st_dev,
                named_after.st_ino,
                named_after.st_size,
                named_after.st_mtime_ns,
                named_after.st_nlink,
            )
            != stable
            or (
                rebound_named.st_dev,
                rebound_named.st_ino,
                rebound_named.st_size,
                rebound_named.st_mtime_ns,
                rebound_named.st_nlink,
            )
            != stable
            or after.st_uid != int(uid)
            or after.st_gid != int(gid)
            or stat.S_IMODE(after.st_mode) != int(mode)
            or named_after.st_uid != int(uid)
            or named_after.st_gid != int(gid)
            or stat.S_IMODE(named_after.st_mode) != int(mode)
            or rebound_named.st_uid != int(uid)
            or rebound_named.st_gid != int(gid)
            or stat.S_IMODE(rebound_named.st_mode) != int(mode)
        ):
            raise RuntimeError(f"Dateirechte blieben abweichend: {normalized}")
    except Exception as original_error:
        if changed and descriptor >= 0 and before is not None:
            rollback_error = None
            try:
                os.fchown(descriptor, before.st_uid, before.st_gid)
                os.fchmod(descriptor, stat.S_IMODE(before.st_mode))
                os.fsync(descriptor)
                restored = os.fstat(descriptor)
                restore_parent = _open_absolute_directory_nofollow(parent_path)
                try:
                    restore_parent_identity = os.fstat(restore_parent)
                    restored_named = os.stat(
                        name,
                        dir_fd=restore_parent,
                        follow_symlinks=False,
                    )
                    restored_stable = (
                        restored.st_dev,
                        restored.st_ino,
                        restored.st_size,
                        restored.st_mtime_ns,
                        restored.st_nlink,
                    )
                    restored_named_stable = (
                        restored_named.st_dev,
                        restored_named.st_ino,
                        restored_named.st_size,
                        restored_named.st_mtime_ns,
                        restored_named.st_nlink,
                    )
                    if (
                        (restore_parent_identity.st_dev, restore_parent_identity.st_ino)
                        != (parent_identity.st_dev, parent_identity.st_ino)
                        or restored_stable != stable
                        or restored_named_stable != stable
                        or restored.st_uid != before.st_uid
                        or restored.st_gid != before.st_gid
                        or stat.S_IMODE(restored.st_mode)
                        != stat.S_IMODE(before.st_mode)
                        or restored_named.st_uid != before.st_uid
                        or restored_named.st_gid != before.st_gid
                        or stat.S_IMODE(restored_named.st_mode)
                        != stat.S_IMODE(before.st_mode)
                    ):
                        raise RuntimeError(
                            f"Dateirechte wurden nicht vollständig restauriert: {normalized}"
                        )
                finally:
                    os.close(restore_parent)
            except Exception as exc:
                rollback_error = exc
            if rollback_error is not None:
                raise RuntimeError(
                    "Die Dateirechte-Projektion scheiterte und ihr lokaler Rückfall "
                    f"blieb unvollständig: {normalized} ({rollback_error})"
                ) from original_error
        raise
    finally:
        if rebound_parent >= 0:
            os.close(rebound_parent)
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _validated_configured_venv_path():
    """Bindet ausschließlich das kanonische, strukturell belegte Benutzer-venv."""

    account = pwd.getpwnam(INSTALL_USER)
    home = os.path.normpath(os.path.abspath(account.pw_dir))
    candidate = os.path.normpath(os.path.abspath(get_venv_path(INSTALL_USER)))
    raw_name = os.path.basename(candidate)
    if (
        not raw_name
        or len(raw_name) > 128
        or raw_name in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9._-]+", raw_name) is None
        or os.path.dirname(candidate) != home
        or candidate == os.path.normpath(os.path.abspath(INSTALL_PATH))
    ):
        raise RuntimeError(
            "Konfigurierter venv-Pfad ist nicht das kanonische direkte Home-venv"
        )
    if not os.path.lexists(candidate):
        return raw_name, "", None

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = _open_absolute_directory_nofollow(home)
    descriptor = -1
    try:
        parent_metadata = os.fstat(parent_descriptor)
        named = os.stat(raw_name, dir_fd=parent_descriptor, follow_symlinks=False)
        descriptor = os.open(
            raw_name,
            os.O_RDONLY | nofollow | directory | cloexec,
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino)
            != (named.st_dev, named.st_ino)
            or metadata.st_uid not in {0, account.pw_uid}
            or (metadata.st_uid == 0 and stat.S_IMODE(metadata.st_mode) & 0o022)
        ):
            raise RuntimeError("Konfiguriertes venv besitzt keinen sicheren Stamm")

        marker_fd = os.open(
            "pyvenv.cfg",
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=descriptor,
        )
        try:
            marker = os.fstat(marker_fd)
            if (
                not stat.S_ISREG(marker.st_mode)
                or marker.st_nlink != 1
                or marker.st_size <= 0
                or marker.st_size > 64 * 1024
                or marker.st_uid not in {0, account.pw_uid}
            ):
                raise RuntimeError("pyvenv.cfg besitzt keinen sicheren Dateivertrag")
            payload = os.read(marker_fd, marker.st_size + 1)
            text = payload.decode("utf-8", errors="strict")
            if len(payload) != marker.st_size or not any(
                line.strip().lower().startswith("home =")
                for line in text.splitlines()
            ):
                raise RuntimeError("pyvenv.cfg belegt keine Python-Umgebung")
        finally:
            os.close(marker_fd)

        bin_named = os.stat("bin", dir_fd=descriptor, follow_symlinks=False)
        bin_fd = os.open(
            "bin",
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=descriptor,
        )
        try:
            bin_opened = os.fstat(bin_fd)
            if (
                not stat.S_ISDIR(bin_named.st_mode)
                or (bin_named.st_dev, bin_named.st_ino)
                != (bin_opened.st_dev, bin_opened.st_ino)
            ):
                raise RuntimeError("venv-bin-Verzeichnis ist nicht eindeutig")
            for executable in ("python3", "pip"):
                entry = os.stat(
                    executable,
                    dir_fd=bin_fd,
                    follow_symlinks=False,
                )
                if not (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode)):
                    raise RuntimeError(
                        f"venv-{executable} ist weder Datei noch zulässiger Symlink"
                    )
                resolved = os.path.realpath(os.path.join(candidate, "bin", executable))
                resolved_metadata = os.stat(resolved)
                if (
                    not stat.S_ISREG(resolved_metadata.st_mode)
                    or resolved_metadata.st_uid not in {0, account.pw_uid}
                    or (
                        resolved_metadata.st_uid == 0
                        and stat.S_IMODE(resolved_metadata.st_mode) & 0o022
                    )
                ):
                    raise RuntimeError(f"venv-{executable} besitzt kein sicheres Ziel")
        finally:
            os.close(bin_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)
    return (
        raw_name,
        candidate,
        (
            int(parent_metadata.st_dev),
            int(parent_metadata.st_ino),
            int(metadata.st_dev),
            int(metadata.st_ino),
        ),
    )


def _required_web_traversal_ancestors():
    """Liefert nur echte Vorfahren zwischen Home und Installationsstamm."""

    home = os.path.normpath(os.path.abspath(str(INSTALL_HOME)))
    target = os.path.normpath(os.path.abspath(str(INSTALL_PATH)))
    if not os.path.lexists(target) or target == home:
        return ()
    try:
        if os.path.commonpath((home, target)) != home:
            return ()
    except ValueError:
        return ()
    relative = os.path.relpath(target, home)
    parts = tuple(part for part in relative.split(os.sep) if part and part != ".")
    if not parts:
        return ()
    ancestors = [home]
    current = home
    for component in parts[:-1]:
        current = os.path.join(current, component)
        ancestors.append(current)
    return tuple(ancestors)


def _bound_directory_metadata(path):
    descriptor = _open_absolute_directory_nofollow(path)
    try:
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def _web_can_traverse(metadata, web_gid, path=None):
    if bool(metadata.st_mode & 0o001) or (
        metadata.st_gid == int(web_gid) and bool(metadata.st_mode & 0o010)
    ):
        return True
    if path is None:
        return False
    if __package__ in (None, ""):
        from Installer.update_simple import (
            _read_bound_directory_prestate,
            _web_account_can_traverse_bound_directory,
        )
    else:
        from .update_simple import (
            _read_bound_directory_prestate,
            _web_account_can_traverse_bound_directory,
        )
    expected = _read_bound_directory_prestate(path)
    if (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_uid),
        int(metadata.st_gid),
        int(stat.S_IMODE(metadata.st_mode)),
    ) != (
        expected.device,
        expected.inode,
        expected.uid,
        expected.gid,
        expected.mode,
    ):
        raise RuntimeError(f"Installationspfad-Vorfahre driftete: {path}")
    return _web_account_can_traverse_bound_directory(expected)


def _project_required_web_traversal(paths):
    """Projiziert benötigte Vorfahren eng und rollt Teilfolgen strikt zurück."""

    if __package__ in (None, ""):
        from Installer.update_simple import (
            DirectoryMetadataTransition,
            _apply_bound_directory_transition,
            _read_bound_directory_prestate,
            _web_traversal_projection_strategy,
        )
    else:
        from .update_simple import (
            DirectoryMetadataTransition,
            _apply_bound_directory_transition,
            _read_bound_directory_prestate,
            _web_traversal_projection_strategy,
        )

    account = pwd.getpwnam(INSTALL_USER)
    web_gid = int(grp.getgrnam("www-data").gr_gid)
    plan = []
    for path in paths:
        current = _read_bound_directory_prestate(path)
        strategy = _web_traversal_projection_strategy(
            current,
            install_user=INSTALL_USER,
            install_uid=int(account.pw_uid),
            web_gid=web_gid,
        )
        plan.append((current, strategy))
    transitions = []
    try:
        for current, strategy in plan:
            if strategy == "ready":
                continue
            current_mode = current.mode
            if strategy == "web-group":
                desired_gid = web_gid
                desired_mode = current_mode | 0o010
            elif strategy == "private-group-rebind":
                desired_gid = web_gid
                desired_mode = (current_mode & ~0o070) | 0o010
            else:
                raise RuntimeError(
                    f"Unbekannte Traversal-Projektionsstrategie: {strategy}"
                )
            projected = _apply_bound_directory_transition(
                current,
                uid=current.uid,
                gid=desired_gid,
                mode=desired_mode,
            )
            transitions.append(
                DirectoryMetadataTransition(
                    previous=current,
                    projected=projected,
                )
            )
    except Exception as original_error:
        rollback_failures = []
        for transition in reversed(transitions):
            try:
                _apply_bound_directory_transition(
                    transition.projected,
                    uid=transition.previous.uid,
                    gid=transition.previous.gid,
                    mode=transition.previous.mode,
                )
            except Exception as exc:
                rollback_failures.append(f"{transition.previous.path}: {exc}")
        if rollback_failures:
            raise RuntimeError(
                "Traversierrechte-Projektion und ihr Rückfall blieben unvollständig: "
                + "; ".join(rollback_failures)
            ) from original_error
        raise
    return tuple(transitions)


def repair_web_install_traversal_preflight():
    """Repariert den engen Pfadzugriff als eigene transaktionsfreie Vorstufe."""

    paths = _required_web_traversal_ancestors()
    if not paths:
        return True
    try:
        web_gid = get_www_data_gid()
        missing = tuple(
            path
            for path in paths
            if not _web_can_traverse(
                _bound_directory_metadata(path), web_gid, path
            )
        )
        if missing:
            _project_required_web_traversal(missing)
        for path in paths:
            if not _web_can_traverse(
                _bound_directory_metadata(path), web_gid, path
            ):
                raise RuntimeError(
                    f"Installationspfad-Vorfahre blieb für www-data gesperrt: {path}"
                )
    except Exception as exc:
        print(
            f"{RED}[!]{RESET} Installationspfad-Reparatur wurde sicher gesperrt: {exc}"
        )
        perm_logger.error(
            "Eigenständige Installationspfad-Reparatur fehlgeschlagen: %s",
            exc,
        )
        return False
    return True


def check_permissions():
    """Prüft Installation-Verzeichnis."""
    print("\n=== Verzeichnis-Rechteprüfung ===\n")
    perm_logger.info("--- Starte Verzeichnis-Rechteprüfung ---")

    def format_dir_issue(owner, group, mode, expected_owner, expected_group, expected_mode):
        details = []
        if owner != expected_owner:
            details.append(f"Owner={owner} (soll: {expected_owner})")
        if group != expected_group:
            details.append(f"Gruppe={group} (soll: {expected_group})")
        if mode != expected_mode:
            details.append(f"Modus={mode} (soll: {expected_mode})")
        return ", ".join(details) if details else "unbekannte Abweichung"

    issues = []
    # Nur die tatsächlich zwischen Home und Installation liegenden Vorfahren
    # müssen für www-data betretbar sein. Installationen unter /opt verändern
    # das Benutzer-Home deshalb nicht.
    www_data_gid = get_www_data_gid()
    for traversal_path in _required_web_traversal_ancestors():
        issue_key = (
            "home"
            if os.path.normpath(traversal_path)
            == os.path.normpath(os.path.abspath(str(INSTALL_HOME)))
            else f"web_traversal:{traversal_path}"
        )
        try:
            metadata = _bound_directory_metadata(traversal_path)
            if not _web_can_traverse(metadata, www_data_gid, traversal_path):
                print(
                    f"{RED}✗{RESET} {traversal_path} ist NICHT für www-data erreichbar"
                )
                perm_logger.error(
                    "Installationspfad-Vorfahre nicht für www-data erreichbar: %s",
                    traversal_path,
                )
                issues.append(issue_key)
            else:
                print(f"{GREEN}✓{RESET} {traversal_path} ist für www-data erreichbar")
                perm_logger.info(
                    "Installationspfad-Vorfahre OK: %s",
                    traversal_path,
                )
        except Exception as e:
            print(f"{RED}✗{RESET} Fehler beim Prüfen von {traversal_path}: {e}")
            perm_logger.error(
                "Fehler beim Prüfen des Installationspfad-Vorfahren %s: %s",
                traversal_path,
                e,
            )
            issues.append(issue_key)

    if not _install_user_in_www_data_group():
        print(f"{RED}✗{RESET} {INSTALL_USER} ist nicht Mitglied der Gruppe www-data")
        perm_logger.error("%s fehlt in Gruppe www-data; 660-Config wäre für Dienste nicht lesbar.", INSTALL_USER)
        issues.append("install_user_www_data_group")
    else:
        print(f"{GREEN}✓{RESET} {INSTALL_USER} kann Dateien der Gruppe www-data lesen")
        perm_logger.info("%s ist Mitglied der Gruppe www-data oder nutzt www-data als Primärgruppe.", INSTALL_USER)

    # INSTALL_PATH prüfen
    if not os.path.exists(INSTALL_PATH):
        print(f"{GREEN}✓{RESET} {INSTALL_PATH} existiert noch nicht – überspringe Rechteprüfung.\n")
        perm_logger.info(f"Install-Pfad existiert noch nicht: {INSTALL_PATH}")
        return issues
    if not os.path.isdir(INSTALL_PATH):
        print(f"{RED}✗{RESET} {INSTALL_PATH} ist kein Verzeichnis!")
        perm_logger.error(f"Install-Pfad ist kein Verzeichnis: {INSTALL_PATH}")
        issues.append("notdir")
        return issues
    try:
        st = os.stat(INSTALL_PATH)
        owner = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
        mode = f"{stat.S_IMODE(st.st_mode):o}"
        if owner != INSTALL_USER or group != "www-data":
            details = format_dir_issue(owner, group, mode, INSTALL_USER, "www-data", "755")
            print(f"{RED}✗{RESET} {INSTALL_PATH} Problem: {details}")
            perm_logger.error(f"INSTALL_PATH Besitzer/Gruppe falsch: {details}")
            issues.append("owner")
        else:
            print(f"{GREEN}✓{RESET} {INSTALL_PATH} gehört {INSTALL_USER}:www-data")
            perm_logger.info(f"INSTALL_PATH Besitzer OK: {INSTALL_USER}:www-data")
        if mode != "755":
            print(f"{RED}✗{RESET} {INSTALL_PATH} hat Rechte {mode} statt 755")
            perm_logger.error(f"INSTALL_PATH Modus falsch: {mode} (soll: 755)")
            issues.append("mode")
        else:
            print(f"{GREEN}✓{RESET} {INSTALL_PATH} hat korrekte Rechte (755)")
            perm_logger.info(f"INSTALL_PATH Modus OK: 755")

        # Apache/PHP starts some diagnostics directly from Installer/.
        # File mode 755 is not enough if the containing directory is 700.
        if os.path.isdir(INSTALLER_DIR):
            st_installer = os.stat(INSTALLER_DIR)
            installer_owner = pwd.getpwuid(st_installer.st_uid).pw_name
            installer_group = grp.getgrgid(st_installer.st_gid).gr_name
            installer_mode = f"{stat.S_IMODE(st_installer.st_mode):o}"
            if installer_owner != INSTALL_USER or installer_group != "www-data" or installer_mode != "755":
                details = format_dir_issue(installer_owner, installer_group, installer_mode, INSTALL_USER, "www-data", "755")
                print(f"{RED}x{RESET} {INSTALLER_DIR} Problem: {details}")
                perm_logger.error(f"INSTALLER_DIR fuer Web-Diagnose nicht betretbar: {details}")
                issues.append("installer_dir")
            else:
                print(f"{GREEN}OK{RESET} {INSTALLER_DIR} ist fuer Web-Diagnose betretbar (755)")
                perm_logger.info("INSTALLER_DIR Modus OK: 755")

        # VENV prüfen (falls vorhanden)
        try:
            venv_name, venv_path, _venv_identity = _validated_configured_venv_path()
        except Exception as exc:
            print(f"{RED}✗{RESET} Konfigurierter venv-Pfad ist unsicher: {exc}")
            perm_logger.error("Unsicherer venv-Pfad: %s", exc)
            issues.append("venv_unsafe")
            venv_name, venv_path = "", ""

        if venv_name and venv_path:
            st_venv = os.stat(venv_path)
            owner_venv = pwd.getpwuid(st_venv.st_uid).pw_name
            if owner_venv != INSTALL_USER:
                print(f"{RED}✗{RESET} {venv_name} gehört {owner_venv} (soll: {INSTALL_USER})")
                issues.append("venv_owner")
            else:
                print(f"{GREEN}✓{RESET} {venv_name} gehört {INSTALL_USER}")

            # Prüfen ob executables ausführbar sind
            pip_bin = os.path.join(venv_path, "bin", "pip")
            if os.path.exists(pip_bin) and not os.access(pip_bin, os.X_OK):
                print(f"{RED}✗{RESET} {venv_name}/bin/pip ist nicht ausführbar")
                issues.append("venv_mode")

    except Exception as e:
        print(f"{RED}✗{RESET} Fehler beim Prüfen: {e}")
        perm_logger.error(f"Fehler beim Prüfen von INSTALL_PATH: {e}")
        issues.append("error")


            
    return issues


def _private_matter_storage_issues(storage_path):
    """Return owner/mode/unsafe findings without following storage links."""

    findings = set()
    if not os.path.lexists(storage_path):
        return findings
    try:
        root_stat = os.lstat(storage_path)
    except OSError:
        return {"unsafe"}
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return {"unsafe"}

    entries = [(storage_path, root_stat, "dir")]
    try:
        for current_dir, dirnames, filenames in os.walk(storage_path, followlinks=False):
            for name in dirnames:
                path = os.path.join(current_dir, name)
                entries.append((path, os.lstat(path), "dir"))
            for name in filenames:
                path = os.path.join(current_dir, name)
                entries.append((path, os.lstat(path), "file"))
    except OSError:
        findings.add("unsafe")

    for _path, metadata, expected_kind in entries:
        is_expected_kind = (
            stat.S_ISDIR(metadata.st_mode)
            if expected_kind == "dir"
            else stat.S_ISREG(metadata.st_mode)
        )
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not is_expected_kind
            or (expected_kind == "file" and metadata.st_nlink != 1)
        ):
            findings.add("unsafe")
            continue
        try:
            owner = pwd.getpwuid(metadata.st_uid).pw_name
            group = grp.getgrgid(metadata.st_gid).gr_name
        except KeyError:
            findings.add("owner")
        else:
            if owner != INSTALL_USER or group != "www-data":
                findings.add("owner")
        expected_mode = 0o700 if expected_kind == "dir" else 0o600
        if stat.S_IMODE(metadata.st_mode) != expected_mode:
            findings.add("mode")
    return findings


def _private_wallbox_plan_job_issues(storage_path=WALLBOX_PLAN_JOB_ROOT):
    """Prüft den ausschließlich von www-data verwendeten Planner-Transaktionsbaum."""

    if not os.path.lexists(storage_path):
        return {"missing"}
    findings = set()
    try:
        root_stat = os.lstat(storage_path)
    except OSError:
        return {"unsafe"}
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return {"unsafe"}

    entries = [(storage_path, root_stat, "dir")]
    try:
        for current_dir, dirnames, filenames in os.walk(
            storage_path,
            topdown=True,
            followlinks=False,
        ):
            for name in dirnames:
                path = os.path.join(current_dir, name)
                entries.append((path, os.lstat(path), "dir"))
            for name in filenames:
                path = os.path.join(current_dir, name)
                entries.append((path, os.lstat(path), "file"))
    except OSError:
        findings.add("unsafe")

    for _path, metadata, expected_kind in entries:
        expected_type = (
            stat.S_ISDIR(metadata.st_mode)
            if expected_kind == "dir"
            else stat.S_ISREG(metadata.st_mode)
        )
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not expected_type
            or (expected_kind == "file" and metadata.st_nlink != 1)
        ):
            findings.add("unsafe")
            continue
        try:
            owner = pwd.getpwuid(metadata.st_uid).pw_name
            group = grp.getgrgid(metadata.st_gid).gr_name
        except KeyError:
            findings.add("owner")
        else:
            if owner != "www-data" or group != "www-data":
                findings.add("owner")
        expected_mode = 0o700 if expected_kind == "dir" else 0o600
        if stat.S_IMODE(metadata.st_mode) != expected_mode:
            findings.add("mode")
    return findings


def _mode5_user_start_request_nodes_safe(path, *, legacy_parent=False):
    """Prüft Parent, Request und Lock ohne irgendeine Zielnormalisierung."""

    try:
        account = pwd.getpwnam("www-data")
        group = grp.getgrnam("www-data")
        manager = pwd.getpwnam(INSTALL_USER)
        allowed_parent_uids = {int(account.pw_uid), int(manager.pw_uid)}
        parent = os.lstat(os.path.dirname(path))
        parent_mode = stat.S_IMODE(parent.st_mode)
        expected_parent_mode = int(config_secret_dir_mode())
        required_parent_mode = (
            expected_parent_mode & 0o777
            if legacy_parent
            else expected_parent_mode
        )
        if (
            stat.S_ISLNK(parent.st_mode)
            or not stat.S_ISDIR(parent.st_mode)
            or bool(parent_mode & 0o002)
            or parent.st_uid not in allowed_parent_uids
            or parent.st_gid != int(group.gr_gid)
            or parent_mode != required_parent_mode
        ):
            return False
        contracts = (
            (path, {int(account.pw_uid)}, True),
            (
                path + ".lock",
                {0, int(account.pw_uid), int(manager.pw_uid)},
                False,
            ),
        )
        for target, allowed_uids, payload_file in contracts:
            if not os.path.lexists(target):
                continue
            metadata = os.lstat(target)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o660
                or metadata.st_uid not in allowed_uids
                or metadata.st_gid != int(group.gr_gid)
                or metadata.st_size > 65536
                or (payload_file and metadata.st_size < 1)
            ):
                return False
        return True
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _mode5_user_start_request_surface_safe(
    path=WALLBOX_MODE5_USER_START_REQUEST_FILE,
):
    """Prüft die PHP-eigene Modus-5-Anforderung ohne Reparaturversuch."""

    return _mode5_user_start_request_nodes_safe(path, legacy_parent=False)


def _repair_mode5_user_start_legacy_parent(
    path=WALLBOX_MODE5_USER_START_REQUEST_FILE,
):
    """Ergänzt nur dem konfigurierten Datenmodus das Setgid-Bit."""

    if _mode5_user_start_request_surface_safe(path):
        return True
    if not _mode5_user_start_request_nodes_safe(path, legacy_parent=True):
        return False
    directory = os.path.dirname(path)
    descriptor = None
    try:
        before = os.lstat(directory)
        group = grp.getgrnam("www-data")
        account = pwd.getpwnam("www-data")
        manager = pwd.getpwnam(INSTALL_USER)
        allowed_parent_uids = {int(account.pw_uid), int(manager.pw_uid)}
        expected_parent_mode = int(config_secret_dir_mode())
        legacy_parent_mode = expected_parent_mode & 0o777
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(directory, flags)
        current = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or current.st_uid not in allowed_parent_uids
            or current.st_gid != int(group.gr_gid)
            or stat.S_IMODE(current.st_mode) != legacy_parent_mode
        ):
            return False
        os.fchmod(descriptor, expected_parent_mode)
        changed = os.fstat(descriptor)
        named = os.lstat(directory)
        if (
            stat.S_IMODE(changed.st_mode) != expected_parent_mode
            or (named.st_dev, named.st_ino) != (changed.st_dev, changed.st_ino)
            or named.st_uid not in allowed_parent_uids
            or stat.S_IMODE(named.st_mode) != expected_parent_mode
        ):
            return False
    except (KeyError, OSError, TypeError, ValueError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return _mode5_user_start_request_surface_safe(path)


def check_webportal_permissions(include_service_checks=True):
    """Prüft Webportal-Verzeichnis."""
    print("\n=== Webportal-Rechteprüfung ===\n")
    perm_logger.info("--- Starte Webportal-Rechteprüfung ---")

    def format_wp_issue(owner, group, mode, expected_owner, expected_group, expected_mode):
        details = []
        if owner != expected_owner:
            details.append(f"Owner={owner} (soll: {expected_owner})")
        if group != expected_group:
            details.append(f"Gruppe={group} (soll: {expected_group})")
        if mode != expected_mode:
            details.append(f"Modus={mode} (soll: {expected_mode})")
        return ", ".join(details) if details else "unbekannte Abweichung"

    issues = []
    secret_dir_mode = config_secret_dir_mode_text()
    wp_path = "/var/www/html"
    if not os.path.lexists(wp_path):
        print(f"{RED}✗{RESET} {wp_path} existiert nicht – Webportal nicht installiert")
        issues.append("wp_missing")
        return issues
    try:
        st = os.lstat(wp_path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            print(f"{RED}✗{RESET} {wp_path} ist kein eindeutiges Verzeichnis")
            issues.append("wp_unsafe")
            return issues
        owner = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
        mode = f"{stat.S_IMODE(st.st_mode):o}"

        # KRITISCH: Prüfe ob www-data das Verzeichnis überhaupt betreten kann.
        # Nach 'git pull' kann /var/www/html auf 500 (dr-x------) fallen →
        # Apache bekommt 403 Forbidden auf ALLEN Seiten!
        www_data_gid = get_www_data_gid()
        has_group_x  = st.st_gid == www_data_gid and bool(st.st_mode & 0o010)
        has_other_x  = bool(st.st_mode & 0o001)
        if not has_group_x and not has_other_x:
            print(f"{RED}✗{RESET} {wp_path} kein Execute-Bit für www-data (aktuell: {mode}) → Apache 403!")
            perm_logger.error(f"{wp_path} fehlt Execute-Bit fuer www-data: mode={mode}")
            issues.append("wp_mode")   # löst fix_webportal_permissions → chmod 775 aus

        # Der Webroot schützt insbesondere den persistenten Namen des
        # RAM-Disk-Mountpoints. Schreibbar sind nur die ausdrücklich
        # vorgesehenen Unterverzeichnisse, niemals deren Elternverzeichnis.
        if owner != "root" or group != "www-data":
            details = format_wp_issue(owner, group, mode, "root", "www-data", "755")
            print(f"{RED}✗{RESET} {wp_path} Problem: {details}")
            issues.append("wp_owner")
        else:
            print(f"{GREEN}✓{RESET} {wp_path} gehört root:www-data")
        if mode != "755":
            if "wp_mode" not in issues:   # nicht doppelt melden
                print(f"{RED}✗{RESET} {wp_path} hat Rechte {mode} statt 755")
                issues.append("wp_mode")
        else:
            print(f"{GREEN}✓{RESET} {wp_path} hat korrekte Rechte (755)")
        # Sub-Ordner prüfen
        subfolders = [
            (f"{wp_path}/tmp", "2775"),
            (f"{wp_path}/ramdisk", "2775"),
            (f"{wp_path}/data/history_backups", secret_dir_mode),
            (f"{wp_path}/data/luxtronik_archive", secret_dir_mode),
            (f"{wp_path}/logs", "2775"),
            (f"{wp_path}/data", secret_dir_mode),
            (f"{wp_path}/data/matter-storage", "700")
        ]
        for folder_path, expected_mode in subfolders:
            if not os.path.lexists(folder_path):
                print(f"{RED}✗{RESET} {folder_path} existiert nicht")
                issues.append(f"{os.path.basename(folder_path)}_missing")
            else:
                st_sub = os.lstat(folder_path)
                if stat.S_ISLNK(st_sub.st_mode) or not stat.S_ISDIR(st_sub.st_mode):
                    print(
                        f"{RED}✗{RESET} {folder_path} ist kein eindeutiges Verzeichnis"
                    )
                    issues.append(f"{os.path.basename(folder_path)}_unsafe")
                    continue
                mode_sub = f"{stat.S_IMODE(st_sub.st_mode):o}"
                owner_sub = pwd.getpwuid(st_sub.st_uid).pw_name
                group_sub = grp.getgrgid(st_sub.st_gid).gr_name
                # Prüfe Owner/Group separat
                owner_group_issue = owner_sub != INSTALL_USER or group_sub != "www-data"
                mode_issue = mode_sub != expected_mode
                if owner_group_issue or mode_issue:
                    details = format_wp_issue(owner_sub, group_sub, mode_sub, INSTALL_USER, "www-data", expected_mode)
                    print(f"{RED}✗{RESET} {folder_path} Problem: {details}")
                    # Separate Issue-Keys für Owner und Mode
                    folder_name = os.path.basename(folder_path)
                    if owner_group_issue:
                        issues.append(f"{folder_name}_owner")
                    if mode_issue:
                        issues.append(f"{folder_name}_mode")
                else:
                    print(f"{GREEN}✓{RESET} {folder_path} OK ({INSTALL_USER}:www-data, {expected_mode})")

                # tmp-Ordner Schreibprüfung für www-data
                if os.path.basename(folder_path) == "tmp":
                    www_data_gid = get_www_data_gid()
                    other_wx = (st_sub.st_mode & 0o003) == 0o003
                    if not other_wx and group_sub != "www-data":
                         print(f"{RED}✗{RESET} {folder_path} Not writable for www-data")
                         issues.append(f"{folder_name}_not_writable")

                    group_wx = st_sub.st_gid == www_data_gid and (st_sub.st_mode & 0o030) == 0o030
                    if not other_wx and not group_wx:
                        print(f"{RED}✗{RESET} {folder_path} ist für www-data nicht schreibbar")
                        issues.append("tmp_not_writable")
                    else:
                        print(f"{GREEN}✓{RESET} {folder_path} ist für www-data schreibbar")

                # Die RAM-Disk muss exakt am kanonischen Ziel als tmpfs gebunden
                # sein. Eine globale ``mount``-Textsuche darf weder einen
                # fremden tmpfs-Mount noch einen Bind-Mount am Ziel akzeptieren.
                # In Docker-Umgebungen wird tmpfs über Compose bereitgestellt.
                _is_docker_env = os.path.exists('/.dockerenv')
                if os.path.basename(folder_path) == "ramdisk" and not _is_docker_env:
                    try:
                        ramdisk_probe = probe_ramdisk_tmpfs(
                            ramdisk_path=folder_path,
                        )
                        if ramdisk_probe.get("ok"):
                            print(f"{GREEN}[OK]{RESET} {folder_path} ist als tmpfs (RAM) eingehangen")
                        else:
                            print(f"{RED}[!]{RESET} {folder_path} existiert aber ist KEIN tmpfs-Mount!")
                            print(f"      -> Wird automatisch eingerichtet (fstab + mount).")
                            issues.append("ramdisk_not_mounted")
                            perm_logger.error(f"Ramdisk {folder_path} ist kein tmpfs-Mount - wird automatisch gefixt.")
                    except Exception as _me:
                        perm_logger.warning(f"Mount-Check fuer Ramdisk fehlgeschlagen: {_me}")


        # Matter-Fabric und Commissioning-Zugangsdaten bleiben auch nach einer
        # allgemeinen Rechtekorrektur strikt privat. Neben dem Root-Verzeichnis
        # werden deshalb alle vorhandenen Einträge geprüft; Links und sonstige
        # Sonderdateien werden nicht automatisch verändert.
        matter_storage = f"{wp_path}/data/matter-storage"
        for matter_issue in sorted(_private_matter_storage_issues(matter_storage)):
            issue_key = f"matter-storage_{matter_issue}"
            if issue_key not in issues:
                issues.append(issue_key)
        for planner_issue in sorted(_private_wallbox_plan_job_issues()):
            issue_key = f"wallbox-plan-jobs_{planner_issue}"
            if issue_key not in issues:
                issues.append(issue_key)


        # NEU (Bare Metal): Prüfe ob Apache läuft und enabled ist
        home_dir = get_home_dir(INSTALL_USER)
        is_docker = os.path.exists(os.path.join(home_dir, "e3dc-docker", "docker-compose.yml"))
        if include_service_checks and not is_docker:
            res_active = run_command("systemctl is-active apache2")
            res_enabled = run_command("systemctl is-enabled apache2")
            php_mod = run_command("apache2ctl -M 2>/dev/null | grep -E 'php.*_module|php_module'")
            if not php_mod['success']:
                print(f"{RED}✗{RESET} Apache PHP-Modul fehlt - PHP würde als Klartext ausgeliefert")
                issues.append("apache_php_module")
            else:
                print(f"{GREEN}✓{RESET} Apache PHP-Modul aktiv")
            if res_active['stdout'].strip() != "active" or res_enabled['stdout'].strip() != "enabled":
                print(f"{RED}✗{RESET} Apache Webserver: Inaktiv oder Autostart deaktiviert")
                issues.append("apache_service")
            else:
                print(f"{GREEN}✓{RESET} Apache Webserver: Aktiv & Autostart bereit")
    except Exception as e:
        print(f"{RED}✗{RESET} Fehler beim Prüfen: {e}")
        issues.append("error")
    return issues


# Gebundene Resolver-/Import-Closure von install_center.php -> helpers.php ->
# web_installer.py. Diese Liste ist bewusst endlich: Der Reparaturassistent
# darf keine unbekannten lokalen Dateien durch einen rekursiven Produktbaum-
# Scan vereinnahmen.
LIVE_INSTALL_CENTER_PRODUCT_CLOSURE = (
    "VERSION",
    "Installer/__init__.py",
    "Installer/backup_integrity.py",
    "Installer/backup_retention.py",
    "Installer/config_secret_permissions.py",
    "Installer/git_commit_reader.py",
    "Installer/installer_config.py",
    "Installer/logging_manager.py",
    "Installer/release_version.py",
    "Installer/secure_file_transaction.py",
    "Installer/service_catalog.py",
    "Installer/utils.py",
    "Installer/web_installer.py",
)


# Vollständige, eingecheckte Web-Produkt-Positivliste für veröffentlichte
# Alt-Aufrufer, die noch keinen zielcommitgebundenen Inventarvertrag übergeben.
# Die Liste wird bei jedem Release gegen ``git ls-files html`` getestet. Sie
# darf keine Laufzeitfläche enthalten; unbekannte lokale Webpfade bleiben
# dadurch auch im Kompatibilitätsweg unangetastet.
WEB_PROGRAM_FALLBACK_FILES = (
    "CHANGELOG.md",
    "UPDATE_POLICY.json",
    "VERSION",
    "Wallbox.php",
    "app-icon-192.png",
    "app-icon-512.png",
    "assets/vendor/bootstrap-icons/LICENSE.txt",
    "assets/vendor/bootstrap-icons/bootstrap-icons.min.css",
    "assets/vendor/bootstrap-icons/fonts/bootstrap-icons.woff",
    "assets/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2",
    "assets/vendor/bootstrap/LICENSE.txt",
    "assets/vendor/bootstrap/css/bootstrap.min.css",
    "assets/vendor/bootstrap/js/bootstrap.bundle.min.js",
    "assets/vendor/chart.js/LICENSE.txt",
    "assets/vendor/chart.js/chart.umd.min.js",
    "assets/vendor/chartjs-plugin-zoom/LICENSE.txt",
    "assets/vendor/chartjs-plugin-zoom/chartjs-plugin-zoom.min.js",
    "assets/vendor/fontawesome/LICENSE.txt",
    "assets/vendor/fontawesome/css/all.min.css",
    "assets/vendor/fontawesome/webfonts/fa-brands-400.ttf",
    "assets/vendor/fontawesome/webfonts/fa-brands-400.woff2",
    "assets/vendor/fontawesome/webfonts/fa-regular-400.ttf",
    "assets/vendor/fontawesome/webfonts/fa-regular-400.woff2",
    "assets/vendor/fontawesome/webfonts/fa-solid-900.ttf",
    "assets/vendor/fontawesome/webfonts/fa-solid-900.woff2",
    "assets/vendor/fontawesome/webfonts/fa-v4compatibility.ttf",
    "assets/vendor/fontawesome/webfonts/fa-v4compatibility.woff2",
    "assets/vendor/hammerjs/LICENSE.txt",
    "assets/vendor/hammerjs/hammer.min.js",
    "assets/vendor/jquery/LICENSE.txt",
    "assets/vendor/jquery/jquery-3.6.0.min.js",
    "backup_history.php",
    "config_editor.php",
    "eeg_tariff_tables.php",
    "fahrzeug.php",
    "favicon.ico",
    "get_chart_data.php",
    "get_forecast_data.php",
    "get_live_json.php",
    "get_shadow_snapshot.php",
    "help.php",
    "helpers.php",
    "history.php",
    "index.html",
    "index.php",
    "install_center.php",
    "install_wizard.php",
    "klima.php",
    "langzeit.php",
    "logic.php",
    "retention.php",
    "manifest.json",
    "manifest_mobile.json",
    "manual_bat_cmd.php",
    "matter.php",
    "mobile.php",
    "openwb_cmd.php",
    "repair_wb_history.php",
    "rule_calm_analysis.php",
    "send_daily_telegram.php",
    "send_status_telegram.php",
    "send_weekly_telegram.php",
    "service_control.php",
    "solar.js",
    "solar.min.js",
    "style.css",
    "sw.js",
    "vitals.php",
    "waermepumpe.php",
    "wallbox_transaction.php",
    "webhook.php",
    "webpush_api.php",
)
WEB_PROGRAM_FALLBACK_DIRECTORIES = (
    "assets",
    "assets/vendor",
    "assets/vendor/bootstrap",
    "assets/vendor/bootstrap-icons",
    "assets/vendor/bootstrap-icons/fonts",
    "assets/vendor/bootstrap/css",
    "assets/vendor/bootstrap/js",
    "assets/vendor/chart.js",
    "assets/vendor/chartjs-plugin-zoom",
    "assets/vendor/fontawesome",
    "assets/vendor/fontawesome/css",
    "assets/vendor/fontawesome/webfonts",
    "assets/vendor/hammerjs",
    "assets/vendor/jquery",
)


# Definition der zu prüfenden Dateien und ihrer Berechtigungen
FILE_DEFINITIONS = [
    # Installer Skripte (Sicherstellen, dass sie ausführbar sind)
    {"path": f"{INSTALL_ROOT}/installer_main.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": True},
    {"path": f"{INSTALLER_DIR}/self_update.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": True},
    # Installer Config (Sonderfall, weil sie nicht im INSTALL_PATH liegen muss)
    # Konfigurationsdateien
    # [LEGACY C++] e3dc.config.txt: wird in V4 nicht mehr aktiv beschrieben.
    # Einige Dienste lesen sie noch als Fallback (Uebergangsphase). optional=True = kein Alarm wenn fehlt.
    {"path": f"{INSTALL_PATH}/e3dc.config.txt", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # [LEGACY C++] e3dc.wallbox.txt: NUR im Legacy-Modus (wb_native_enable=0). In V4 Native: DARF nicht beschrieben werden!
    {"path": f"{INSTALL_PATH}/e3dc.wallbox.txt", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/data/e3dc_v4.json", "mode": CONFIG_SECRET_FILE_MODE, "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/data/wallbox_phase_transition_state.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALL_PATH}/data/e3dc_v4.json", "mode": CONFIG_SECRET_FILE_MODE, "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # [LEGACY C++] e3dc.strompreise.txt: optionaler, vom PHP-Frontend gelesener Preis-Fallback.
    {"path": f"{INSTALL_PATH}/e3dc.strompreise.txt", "mode": "640", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALLER_DIR}/wallbox_manager.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/storage_manager.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/storage_manager_legacy.py", "mode": "644", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALLER_DIR}/storage_simulator.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/direct_marketing.py", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALLER_DIR}/direct_marketing_dispatch_planner.py", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALLER_DIR}/epex_manager.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/Forecast/pv_forecast_service.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/forecast_evidence_sidecar.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/e3dc_websocket.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/sqlite_archiver.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/vital_stats.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/shadow_sync.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/service_load_snapshot.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/install_local_mqtt.py", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALLER_DIR}/install_bluelink.py", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALLER_DIR}/bluelink_client.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/e3dc_mqtt_hub.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/generate_vapid.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    # Web-Ausgabedateien
    {"path": "/var/www/html/index.html", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/index.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/helpers.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/retention.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/logic.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/solar.js", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/solar.min.js", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/minify_solar.py", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/Wallbox.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/mobile.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/history.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/archiv.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/vitals.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/langzeit.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/get_live_json.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/get_shadow_snapshot.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/config_editor.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/backup_history.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/send_weekly_telegram.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/send_daily_telegram.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/send_status_telegram.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/waermepumpe.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/e3dc_paths.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/sw.js", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/webpush_api.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/manifest.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/repair_wb_history.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/get_chart_data.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/get_forecast_data.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/fahrzeug.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/webhook.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/data/morning_boost_state.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/data/external_pv_topology.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/data/e3dc_stats.db", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/pv_forecast_diagnostic_summary.json", "mode": "644", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # Wallbox-Session-Helferdateien: PHP/www-data und Python-Dienste lesen/schreiben gemeinsam.
    {"path": "/var/www/html/tmp/car_charge_session.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/tmp/car_charge_session_wb2.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/tmp/vital_stats.lock", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/manual_soc_wb1.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/manual_soc_wb2.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/vehicle_soc_tracker_wb1.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/vehicle_soc_tracker_wb2.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/get_live_json_snapshot.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/wallbox_decision_latest.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # Luxtronik Dateien
    {"path": f"{INSTALLER_DIR}/luxtronik/energy_manager.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/luxtronik/lux_live.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/idm/idm_live.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/idm/idm_scanner.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/stiebel/stiebel_live.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/dimplex/dimplex_live.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": "/var/www/html/ramdisk/waermepumpe.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALLER_DIR}/luxtronik/set_manual_boost.py", "mode": "644", "owner": INSTALL_USER, "group": INSTALL_GROUP, "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/luxtronik.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/stiebel_isg.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/dimplex_wpm.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/manual_boost.flag", "mode": "640", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/logs/energy_manager.log", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/logs/stiebel_live.log", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/logs/dimplex_live.log", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/logs/wallbox_manager.log", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/logs/wallbox_command_audit.log", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # HA Manager Dateien
    {"path": f"{INSTALLER_DIR}/ha_manager.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": "/var/www/html/ramdisk/ha_status.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/logs/ha_manager.log", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/shadow_sync_status.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/logs/shadow_sync.log", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # Notifier Dateien
    {"path": f"{INSTALLER_DIR}/notification_manager.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    # Matter Dateien
    {"path": f"{INSTALLER_DIR}/matter/matter_bridge.js", "mode": "644", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALLER_DIR}/matter/commissioning_credentials.js", "mode": "644", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALLER_DIR}/matter/package.json", "mode": "644", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALLER_DIR}/matter/package-lock.json", "mode": "644", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALLER_DIR}/install_matter.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": "/var/www/html/matter.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # Neue Wallbox & MQTT Hub Dateien
    {"path": "/var/www/html/ramdisk/vehicles.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/wallbox_native.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/openwb_data.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/native_wallbox_schedule.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/external_wb.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/value_filter.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/logs/e3dc_mqtt_hub.log", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # Heizstab / Shelly Manager (wp_type=2) & RSCP Live-Dienst
    {"path": f"{INSTALLER_DIR}/heizstab_manager.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/e3dc_live.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALLER_DIR}/e3dc-heizstab.service", "mode": "644", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALLER_DIR}/e3dc-live.service", "mode": "644", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/live_data_py.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/heizstab_data.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/climate_load.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/climate_control.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/logs/heizstab_manager.log", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/logs/e3dc_live.log", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # Storage Manager & Batterie-Override (V4.5.x)
    # manual_bat_override.json: PHP (manual_bat_cmd.php) schreibt, storage_manager.py liest
    {"path": "/var/www/html/ramdisk/manual_bat_override.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # storage_manager_state.json: storage_manager.py schreibt, energy_manager/wallbox_manager lesen
    {"path": "/var/www/html/ramdisk/storage_manager_state.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # native_schedule_aborted.flag: PHP ('Plan loeschen') schreibt, wallbox_manager.py liest
    {"path": "/var/www/html/ramdisk/native_schedule_aborted.flag", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # value_filter.json: get_live_json.php schreibt (PHP/www-data), solar.js liest -> gruppenschreibbar
    {"path": "/var/www/html/ramdisk/value_filter.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
]

for _relative_product_path in LIVE_INSTALL_CENTER_PRODUCT_CLOSURE:
    _absolute_product_path = os.path.join(INSTALL_ROOT, _relative_product_path)
    if not any(
        os.path.abspath(str(_definition.get("path") or ""))
        == os.path.abspath(_absolute_product_path)
        for _definition in FILE_DEFINITIONS
    ):
        FILE_DEFINITIONS.append(
            {
                "path": _absolute_product_path,
                "mode": "644",
                "owner": INSTALL_USER,
                "group": "www-data",
                "optional": False,
                "executable": False,
            }
        )


_WEB_WRITABLE_TOP = frozenset({"data", "logs", "ramdisk", "tmp"})
_WEB_PRIVATE_RUNTIME_FILES = frozenset(
    {
        "live_history.txt",
        "e3dc_paths.json",
        "e3dc.config.txt",
        "e3dc.strompreise.txt",
        "e3dc.wallbox.txt",
        "e3dc.wallbox.out",
    }
)
_WEB_PRIVATE_RUNTIME_DIRECTORIES = frozenset({"history_backups"})


def _live_product_file_mode(path):
    """Spiegelt den Live-Vertrag des Ziel-Updaters ohne Git-Modusannahme."""

    descriptor = -1
    parent = -1
    try:
        normalized = os.path.normpath(os.path.abspath(str(path)))
        parent_path, name = os.path.split(normalized)
        if not name:
            return "644"
        parent = _open_absolute_directory_nofollow(parent_path)
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent,
        )
        metadata = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return "644"
        if (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino):
            return "644"
        return "755" if os.read(descriptor, 2) == b"#!" else "644"
    except OSError:
        return "644"
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent >= 0:
            os.close(parent)


# Program and Git files may be world-readable/executable where required, but
# are writable only by the installation user. Shared operational files remain
# under /var/www with their explicit www-data contract.
for _definition in FILE_DEFINITIONS:
    _definition_path = os.path.abspath(str(_definition.get("path") or ""))
    _definition_basename = os.path.basename(_definition_path)
    try:
        _inside_repo = os.path.commonpath((_definition_path, os.path.abspath(INSTALL_ROOT))) == os.path.abspath(INSTALL_ROOT)
    except ValueError:
        _inside_repo = False
    if _inside_repo:
        # Privates Release-Staging bleibt getrennt. Der betriebene Produktbaum
        # folgt dagegen demselben lesbaren Vertrag wie update_simple.py.
        _definition["group"] = "www-data"
        if _definition_basename in {
            "e3dc_v4.json",
            "e3dc.config.txt",
            "e3dc.wallbox.txt",
            "e3dc.strompreise.txt",
        }:
            _definition["mode"] = "640"
            _definition["executable"] = False
        else:
            _definition["mode"] = _live_product_file_mode(_definition_path)
            _definition["executable"] = _definition["mode"] == "755"
    _web_root = os.path.abspath("/var/www/html")
    try:
        _inside_web = os.path.commonpath((_definition_path, _web_root)) == _web_root
    except ValueError:
        _inside_web = False
    if _inside_web:
        _relative_web = os.path.relpath(_definition_path, _web_root)
        _top_web = _relative_web.split(os.sep, 1)[0]
        if _top_web not in _WEB_WRITABLE_TOP:
            _definition["group"] = "www-data"
            if _top_web in _WEB_PRIVATE_RUNTIME_FILES:
                _definition["mode"] = "640"
            elif _top_web in _WEB_PRIVATE_RUNTIME_DIRECTORIES:
                _definition["mode"] = "750"
            else:
                _definition["mode"] = "755" if _definition.get("executable") else "644"


def _permission_mount_id(descriptor):
    """Bindet einen offenen Deskriptor zusätzlich zur Geräte-ID an seinen Mount."""

    try:
        with open(
            f"/proc/self/fdinfo/{int(descriptor)}",
            "r",
            encoding="ascii",
            errors="strict",
        ) as handle:
            payload = handle.read()
    except OSError as exc:
        raise RuntimeError(
            "Die Mount-Bindung für die sichere Rechteprojektion ist nicht verfügbar"
        ) from exc
    for line in payload.splitlines():
        if line.startswith("mnt_id:"):
            value = line.partition(":")[2].strip()
            if value.isdigit():
                return value
    raise RuntimeError("Die Mount-ID des Rechtepfads konnte nicht gebunden werden")


def _copy_bound_permission_file(
    parent_fd,
    name,
    source_fd,
    *,
    uid,
    gid,
    mode,
    expected_mount_id,
):
    """Bricht einen Mehrfachlink atomar, ohne den fremden Inode zu verändern."""

    temporary_name = f".e3dc-permissions-{os.getpid()}-{os.urandom(12).hex()}"
    temporary_fd = -1
    replaced = False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                offset += os.write(temporary_fd, chunk[offset:])
        source = os.fstat(source_fd)
        os.utime(
            temporary_fd,
            ns=(int(source.st_atime_ns), int(source.st_mtime_ns)),
        )
        os.fchown(temporary_fd, int(uid), int(gid))
        os.fchmod(temporary_fd, int(mode))
        os.fsync(temporary_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (source.st_dev, source.st_ino):
            raise RuntimeError(
                f"Rechtepfad wechselte vor dem sicheren Mehrfachlink-Austausch: {name}"
            )
        if _permission_mount_id(source_fd) != expected_mount_id:
            raise RuntimeError(f"Rechtedatei liegt auf einem verschachtelten Mount: {name}")
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        secured = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(secured.st_mode)
            or secured.st_nlink != 1
            or secured.st_uid != int(uid)
            or secured.st_gid != int(gid)
            or stat.S_IMODE(secured.st_mode) != int(mode)
        ):
            raise RuntimeError(
                f"Mehrfachlink-Rechteprojektion blieb unvollständig: {name}"
            )
        replaced = True
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if not replaced:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _normalize_permission_tree_fd(
    root,
    contract,
    *,
    excluded_top_level=(),
    excluded_top_level_prefixes=(),
    reject_unsafe_entries=False,
    expected_root_identity=None,
):
    """Projiziert Baumrechte fd-relativ, nofollow-, mount- und hardlinksicher.

    ``contract(relative, metadata, is_directory)`` liefert ``(uid, gid, mode)``.
    Ein einzelner Wert ``None`` erhält die betreffende Metadatenkomponente; ein
    vollständig leeres Ergebnis lässt den gebundenen Eintrag unverändert.
    Symlinks und Spezialdateien werden niemals verfolgt oder verändert.
    """

    root = os.path.abspath(str(root))
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory:
        raise RuntimeError("Sichere fd-relative Rechteprojektion ist nicht verfügbar")
    root_parent_fd = -1
    if expected_root_identity is None:
        root_fd = _open_absolute_directory_nofollow(root)
    else:
        parent_path, root_name = os.path.split(root)
        if not root_name:
            raise RuntimeError("Gebundene Rechtewurzel besitzt keinen Dateinamen")
        expected_parent_dev, expected_parent_ino, expected_dev, expected_ino = (
            int(item) for item in expected_root_identity
        )
        root_parent_fd = _open_absolute_directory_nofollow(parent_path)
        parent_metadata = os.fstat(root_parent_fd)
        named_metadata = os.stat(
            root_name,
            dir_fd=root_parent_fd,
            follow_symlinks=False,
        )
        if (
            (parent_metadata.st_dev, parent_metadata.st_ino)
            != (expected_parent_dev, expected_parent_ino)
            or not stat.S_ISDIR(named_metadata.st_mode)
            or (named_metadata.st_dev, named_metadata.st_ino)
            != (expected_dev, expected_ino)
        ):
            os.close(root_parent_fd)
            raise RuntimeError("Gebundene Rechtewurzel wurde vor der Projektion ersetzt")
        root_fd = os.open(
            root_name,
            os.O_RDONLY | nofollow | directory | cloexec,
            dir_fd=root_parent_fd,
        )
    before_root = os.fstat(root_fd)
    if expected_root_identity is not None and (
        before_root.st_dev,
        before_root.st_ino,
    ) != (expected_dev, expected_ino):
        os.close(root_fd)
        os.close(root_parent_fd)
        raise RuntimeError("Gebundene Rechtewurzel driftete beim Öffnen")
    skipped = []
    excluded = frozenset(str(item) for item in excluded_top_level)
    excluded_prefixes = tuple(str(item) for item in excluded_top_level_prefixes)
    rebound_root = -1

    def desired_values(metadata, result):
        if result is None:
            return None
        uid, gid, mode = result
        return (
            metadata.st_uid if uid is None else int(uid),
            metadata.st_gid if gid is None else int(gid),
            stat.S_IMODE(metadata.st_mode) if mode is None else int(mode),
        )

    def verify_named(parent_fd, name, descriptor, *, uid, gid, mode, kind):
        secured = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            (secured.st_dev, secured.st_ino) != (named.st_dev, named.st_ino)
            or secured.st_uid != uid
            or secured.st_gid != gid
            or stat.S_IMODE(secured.st_mode) != mode
        ):
            raise RuntimeError(f"{kind} blieb nach der Rechteprojektion nicht gebunden: {name}")

    def normalize_regular(parent_fd, name, relative, metadata, mount_id):
        descriptor = os.open(
            name,
            os.O_RDONLY | nofollow | cloexec,
            dir_fd=parent_fd,
        )
        try:
            current = os.fstat(descriptor)
            if (
                not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino)
                != (metadata.st_dev, metadata.st_ino)
                or _permission_mount_id(descriptor) != mount_id
            ):
                raise RuntimeError(
                    f"Rechtedatei wechselte oder liegt auf einem verschachtelten Mount: "
                    f"{os.path.join(root, *relative)}"
                )
            desired = desired_values(current, contract(relative, current, False))
            if desired is None:
                return
            uid, gid, mode = desired
            if current.st_nlink > 1:
                _copy_bound_permission_file(
                    parent_fd,
                    name,
                    descriptor,
                    uid=uid,
                    gid=gid,
                    mode=mode,
                    expected_mount_id=mount_id,
                )
                return
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, mode)
            verify_named(
                parent_fd,
                name,
                descriptor,
                uid=uid,
                gid=gid,
                mode=mode,
                kind="Rechtedatei",
            )
        finally:
            os.close(descriptor)

    def normalize_directory(parent_fd, name, relative, metadata, mount_id):
        descriptor = os.open(
            name,
            os.O_RDONLY | nofollow | directory | cloexec,
            dir_fd=parent_fd,
        )
        try:
            current = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino)
                != (metadata.st_dev, metadata.st_ino)
                or _permission_mount_id(descriptor) != mount_id
            ):
                raise RuntimeError(
                    f"Rechteverzeichnis wechselte oder liegt auf einem verschachtelten Mount: "
                    f"{os.path.join(root, *relative)}"
                )
            walk(descriptor, relative, mount_id)
            desired = desired_values(current, contract(relative, current, True))
            if desired is None:
                return
            uid, gid, mode = desired
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, mode)
            verify_named(
                parent_fd,
                name,
                descriptor,
                uid=uid,
                gid=gid,
                mode=mode,
                kind="Rechteverzeichnis",
            )
        finally:
            os.close(descriptor)

    def walk(parent_fd, relative, mount_id):
        for name in sorted(os.listdir(parent_fd)):
            if not relative and (
                name in excluded or name.startswith(excluded_prefixes)
            ):
                continue
            child_relative = (*relative, name)
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                normalize_directory(parent_fd, name, child_relative, metadata, mount_id)
            elif stat.S_ISREG(metadata.st_mode):
                normalize_regular(parent_fd, name, child_relative, metadata, mount_id)
            else:
                skipped.append(os.path.join(root, *child_relative))
                if reject_unsafe_entries:
                    raise RuntimeError(
                        f"Symlink oder Spezialdatei im Rechtebaum: {skipped[-1]}"
                    )

    try:
        opened_root = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(opened_root.st_mode)
            or (opened_root.st_dev, opened_root.st_ino)
            != (before_root.st_dev, before_root.st_ino)
        ):
            raise RuntimeError(f"Rechtewurzel wechselte beim Öffnen: {root}")
        mount_id = _permission_mount_id(root_fd)
        walk(root_fd, (), mount_id)
        desired = desired_values(opened_root, contract((), opened_root, True))
        if desired is not None:
            uid, gid, mode = desired
            os.fchown(root_fd, uid, gid)
            os.fchmod(root_fd, mode)
            secured = os.fstat(root_fd)
            rebound_root = _open_absolute_directory_nofollow(root)
            named = os.fstat(rebound_root)
            if (
                not stat.S_ISDIR(named.st_mode)
                or (secured.st_dev, secured.st_ino) != (named.st_dev, named.st_ino)
                or secured.st_uid != uid
                or secured.st_gid != gid
                or stat.S_IMODE(secured.st_mode) != mode
            ):
                raise RuntimeError(f"Rechtewurzel blieb nach der Projektion nicht gebunden: {root}")
        else:
            secured = os.fstat(root_fd)
            rebound_root = _open_absolute_directory_nofollow(root)
            named = os.fstat(rebound_root)
            if (secured.st_dev, secured.st_ino) != (named.st_dev, named.st_ino):
                raise RuntimeError(f"Rechtewurzel driftete während der Projektion: {root}")
        if expected_root_identity is not None:
            parent_after = os.fstat(root_parent_fd)
            named_after = os.stat(
                root_name,
                dir_fd=root_parent_fd,
                follow_symlinks=False,
            )
            if (
                (parent_after.st_dev, parent_after.st_ino)
                != (expected_parent_dev, expected_parent_ino)
                or (named_after.st_dev, named_after.st_ino)
                != (expected_dev, expected_ino)
                or (secured.st_dev, secured.st_ino)
                != (expected_dev, expected_ino)
            ):
                raise RuntimeError(
                    f"Gebundene Rechtewurzel wurde während der Projektion ersetzt: {root}"
                )
    finally:
        if rebound_root >= 0:
            os.close(rebound_root)
        os.close(root_fd)
        if root_parent_fd >= 0:
            os.close(root_parent_fd)
    if skipped:
        perm_logger.warning(
            "Rechteprojektion ließ %d Symlink-/Spezialeinträge unverändert: %s",
            len(skipped),
            ", ".join(skipped[:8]),
        )
    return {"changed_surface": root, "skipped": tuple(skipped)}


def _web_runtime_permission_contract(top_name, install_uid, www_uid, www_gid, config):
    data_dir_mode = int(config_secret_dir_mode(config))
    data_file_mode = int(config_secret_file_mode_text(config), 8)

    def contract(relative, metadata, is_directory):
        def desired(uid, gid, mode=None):
            target_mode = stat.S_IMODE(metadata.st_mode) if mode is None else int(mode)
            if (
                metadata.st_uid == int(uid)
                and metadata.st_gid == int(gid)
                and stat.S_IMODE(metadata.st_mode) == target_mode
            ):
                return None
            return int(uid), int(gid), target_mode

        if top_name == "data":
            if relative and relative[0] == "matter-storage":
                return desired(install_uid, www_gid, 0o700 if is_directory else 0o600)
            if relative and relative[0] == ".wallbox_plan_jobs":
                return desired(www_uid, www_gid, 0o700 if is_directory else 0o600)
            if relative[:2] == ("config_backups", "aux_inverter_migration"):
                return desired(install_uid, www_gid, 0o700 if is_directory else 0o600)
            if relative and relative[0] in {
                "config_backups",
                "history_backups",
                "luxtronik_archive",
            }:
                return desired(
                    install_uid,
                    www_gid,
                    data_dir_mode if is_directory else data_file_mode,
                )
            if relative in {
                ("e3dc_v4.json",),
            }:
                return desired(
                    www_uid,
                    www_gid,
                    data_dir_mode if is_directory else data_file_mode,
                )
            if relative in {
                ("wallbox_mode5_user_start_request.json",),
                ("wallbox_mode5_user_start_request.json.lock",),
            }:
                return desired(
                    www_uid,
                    www_gid,
                    data_dir_mode if is_directory else 0o660,
                )
            return desired(
                install_uid,
                www_gid,
                data_dir_mode if not relative and is_directory else None,
            )
        if top_name == "history_backups":
            return desired(install_uid, www_gid, 0o750 if is_directory else 0o640)
        if top_name == "tmp" and relative and relative[0] in {
            "rule_calm_current",
            "rule_calm_uploads",
        }:
            return desired(www_uid, www_gid, 0o700 if is_directory else 0o600)
        if (
            top_name == "ramdisk"
            and relative
            and relative[0].startswith("rule_calm_analysis.json")
        ):
            return desired(www_uid, www_gid, 0o2775 if is_directory else 0o600)
        return desired(
            install_uid,
            www_gid,
            0o2775 if not relative and is_directory else None,
        )

    return contract


def _normalize_web_runtime_permissions(web_root="/var/www/html", config=None):
    """Normalisiert nur freigegebene Web-Schreibflächen mit eigener Mount-Bindung."""

    account = pwd.getpwnam(INSTALL_USER)
    web_account = pwd.getpwnam("www-data")
    web_group = grp.getgrnam("www-data")
    runtime_config = dict(config or load_config() or {})
    root = os.path.abspath(str(web_root))
    root_fd = _open_absolute_directory_nofollow(root)
    try:
        root_metadata = os.fstat(root_fd)
        root_mount_id = _permission_mount_id(root_fd)
        docker_environment = os.path.exists("/.dockerenv")
        for top_name in ("data", "history_backups", "logs", "ramdisk", "tmp"):
            target = os.path.join(root, top_name)
            try:
                named = os.stat(top_name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(named.st_mode) or not stat.S_ISDIR(named.st_mode):
                raise RuntimeError(
                    f"Web-Laufzeitfläche ist kein echtes Verzeichnis: {target}"
                )
            child_fd = os.open(
                top_name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=root_fd,
            )
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                    raise RuntimeError(
                        f"Web-Laufzeitfläche driftete beim Öffnen: {target}"
                    )
                child_mount_id = _permission_mount_id(child_fd)
            finally:
                os.close(child_fd)

            separate_mount_allowed = bool(
                docker_environment and top_name in {"data", "logs"}
            )
            if top_name == "ramdisk" and child_mount_id != root_mount_id:
                probe = probe_ramdisk_tmpfs(ramdisk_path=target)
                if not probe.get("ok"):
                    raise RuntimeError(
                        "Separate Ramdisk ist nicht als exakter tmpfs-Produktmount gebunden"
                    )
                separate_mount_allowed = True
            if child_mount_id != root_mount_id and not separate_mount_allowed:
                raise RuntimeError(
                    f"Fremder Mount auf Web-Laufzeitfläche wird nicht verändert: {target}"
                )

            _normalize_permission_tree_fd(
                target,
                _web_runtime_permission_contract(
                    top_name,
                    account.pw_uid,
                    web_account.pw_uid,
                    web_group.gr_gid,
                    runtime_config,
                ),
                excluded_top_level=(
                    MATTER_RESET_PROTECTED_DATA_NAMES
                    if top_name == "data"
                    else ()
                ),
                excluded_top_level_prefixes=(
                    MATTER_RESET_PROTECTED_DATA_PREFIXES
                    if top_name == "data"
                    else ()
                ),
                expected_root_identity=(
                    int(root_metadata.st_dev),
                    int(root_metadata.st_ino),
                    int(opened.st_dev),
                    int(opened.st_ino),
                ),
            )
    finally:
        os.close(root_fd)
    return True


def harden_web_program_permissions(
    web_root="/var/www/html",
    install_user=None,
    web_group="www-data",
    *,
    program_files=None,
    program_directories=None,
):
    """Härtet ausschließlich eine explizite Web-Produkt-Positivliste.

    Ohne übergebenen Zielvertrag wird aus Kompatibilitätsgründen nur die
    bestehende statische Produktdateiliste berücksichtigt. Unbekannte lokale
    Pfade und persistente Laufzeitdateien bleiben immer unverändert.
    """

    root = os.path.abspath(str(web_root))
    account_name = str(install_user or INSTALL_USER)
    explicit_contract = program_files is not None or program_directories is not None
    root_descriptor = -1
    try:
        account = pwd.getpwnam(account_name)
        group = grp.getgrnam(str(web_group))
        preserved = (
            set(_WEB_WRITABLE_TOP)
            | set(_WEB_PRIVATE_RUNTIME_FILES)
            | set(_WEB_PRIVATE_RUNTIME_DIRECTORIES)
        )

        def _normalize_relative(value):
            raw = str(value or "").replace("\\", "/")
            parts = tuple(raw.split("/"))
            if (
                not raw
                or raw.startswith("/")
                or any(part in {"", ".", ".."} for part in parts)
                or parts[0] in preserved
            ):
                raise RuntimeError("Web-Produktvertrag enthält einen unzulässigen Pfad")
            return parts

        if not explicit_contract:
            selected_files = {
                "/".join(_normalize_relative(item))
                for item in WEB_PROGRAM_FALLBACK_FILES
            }
            selected_directories = {
                "/".join(_normalize_relative(item))
                for item in WEB_PROGRAM_FALLBACK_DIRECTORIES
            }
        else:
            selected_files = {
                "/".join(_normalize_relative(item))
                for item in (program_files or ())
            }
            selected_directories = {
                "/".join(_normalize_relative(item))
                for item in (program_directories or ())
            }
        if selected_files & selected_directories:
            raise RuntimeError("Web-Produktvertrag enthält Datei-/Verzeichniskollisionen")
        for relative in tuple(selected_files) + tuple(selected_directories):
            parts = relative.split("/")
            selected_directories.update(
                "/".join(parts[:depth]) for depth in range(1, len(parts))
            )

        root_info = os.lstat(root)
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise RuntimeError("Web-Programmroot ist kein sicheres Verzeichnis")
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        if not nofollow or not directory:
            raise RuntimeError("Sicheres Webroot-Öffnen ist nicht verfügbar")
        root_descriptor = os.open(
            root,
            os.O_RDONLY
            | nofollow
            | directory
            | getattr(os, "O_CLOEXEC", 0),
        )
        opened_root = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(opened_root.st_mode)
            or (opened_root.st_dev, opened_root.st_ino)
            != (root_info.st_dev, root_info.st_ino)
        ):
            raise RuntimeError("Web-Programmroot wechselte beim Öffnen")
        # Root kontrolliert den dauerhaften Mountpoint-Namensraum. Apache darf
        # ihn lesen und betreten, aber weder ramdisk noch andere Programmnamen
        # austauschen. Schreibflächen liegen ausschließlich darunter.
        os.fchown(root_descriptor, 0, group.gr_gid)
        os.fchmod(root_descriptor, 0o755)
        secured_root = os.fstat(root_descriptor)
        named_root = os.lstat(root)
        if (
            not stat.S_ISDIR(named_root.st_mode)
            or stat.S_ISLNK(named_root.st_mode)
            or (secured_root.st_dev, secured_root.st_ino)
            != (named_root.st_dev, named_root.st_ino)
            or secured_root.st_uid != 0
            or secured_root.st_gid != group.gr_gid
            or stat.S_IMODE(secured_root.st_mode) != 0o755
        ):
            raise RuntimeError("Webroot-Owner- oder Namensvertrag ist nicht wirksam")
        root_device = secured_root.st_dev
        root_mount_id = _permission_mount_id(root_descriptor)

        def _open_parent(parts):
            descriptor = os.dup(root_descriptor)
            try:
                for component in parts:
                    named = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                    if stat.S_ISLNK(named.st_mode) or not stat.S_ISDIR(named.st_mode):
                        raise RuntimeError("Web-Produktparent ist kein echtes Verzeichnis")
                    child = os.open(
                        component,
                        os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=descriptor,
                    )
                    opened = os.fstat(child)
                    if (
                        (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                        or _permission_mount_id(child) != root_mount_id
                    ):
                        os.close(child)
                        raise RuntimeError("Web-Produktparent driftete oder liegt auf Fremdmount")
                    os.close(descriptor)
                    descriptor = child
                return descriptor
            except Exception:
                os.close(descriptor)
                raise

        for relative in sorted(
            selected_directories,
            key=lambda value: (value.count("/"), value),
        ):
            parts = relative.split("/")
            try:
                parent_fd = _open_parent(parts[:-1])
            except FileNotFoundError:
                if explicit_contract:
                    raise RuntimeError(
                        f"Web-Produktparent fehlt: {relative}"
                    )
                continue
            try:
                try:
                    named = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    if explicit_contract:
                        raise RuntimeError(f"Web-Produktverzeichnis fehlt: {relative}")
                    continue
                if stat.S_ISLNK(named.st_mode) or not stat.S_ISDIR(named.st_mode):
                    raise RuntimeError(f"Web-Produktverzeichnis ist unsicher: {relative}")
                child_fd = os.open(
                    parts[-1],
                    os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if (
                        (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                        or opened.st_dev != root_device
                        or _permission_mount_id(child_fd) != root_mount_id
                    ):
                        raise RuntimeError(f"Web-Produktverzeichnis driftete: {relative}")
                    os.fchown(child_fd, account.pw_uid, group.gr_gid)
                    os.fchmod(child_fd, 0o755)
                    changed = os.fstat(child_fd)
                    named_after = os.stat(
                        parts[-1], dir_fd=parent_fd, follow_symlinks=False
                    )
                    if (
                        not stat.S_ISDIR(changed.st_mode)
                        or (changed.st_dev, changed.st_ino)
                        != (named_after.st_dev, named_after.st_ino)
                        or changed.st_uid != account.pw_uid
                        or changed.st_gid != group.gr_gid
                        or stat.S_IMODE(changed.st_mode) != 0o755
                    ):
                        raise RuntimeError(
                            f"Web-Produktverzeichnis blieb nicht gebunden: {relative}"
                        )
                finally:
                    os.close(child_fd)
            finally:
                os.close(parent_fd)

        for relative in sorted(selected_files):
            parts = relative.split("/")
            try:
                parent_fd = _open_parent(parts[:-1])
            except FileNotFoundError:
                if explicit_contract:
                    raise RuntimeError(
                        f"Web-Produktparent fehlt: {relative}"
                    )
                continue
            try:
                try:
                    named = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    if explicit_contract:
                        raise RuntimeError(f"Web-Produktdatei fehlt: {relative}")
                    continue
                if (
                    not stat.S_ISREG(named.st_mode)
                    or named.st_nlink != 1
                    or named.st_dev != root_device
                ):
                    raise RuntimeError(f"Web-Produktdatei ist unsicher: {relative}")
                file_fd = os.open(
                    parts[-1],
                    os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
                try:
                    opened = os.fstat(file_fd)
                    if (
                        (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                        or opened.st_nlink != 1
                        or _permission_mount_id(file_fd) != root_mount_id
                    ):
                        raise RuntimeError(f"Web-Produktdatei driftete: {relative}")
                    os.fchown(file_fd, account.pw_uid, group.gr_gid)
                    os.fchmod(file_fd, 0o644)
                    changed = os.fstat(file_fd)
                    named_after = os.stat(
                        parts[-1], dir_fd=parent_fd, follow_symlinks=False
                    )
                    if (
                        (changed.st_dev, changed.st_ino)
                        != (named_after.st_dev, named_after.st_ino)
                        or changed.st_uid != account.pw_uid
                        or changed.st_gid != group.gr_gid
                        or stat.S_IMODE(changed.st_mode) != 0o644
                    ):
                        raise RuntimeError(f"Web-Produktdatei blieb nicht gebunden: {relative}")
                finally:
                    os.close(file_fd)
            finally:
                os.close(parent_fd)

        named_root = os.lstat(root)
        current_root = os.fstat(root_descriptor)
        if (named_root.st_dev, named_root.st_ino) != (
            current_root.st_dev,
            current_root.st_ino,
        ):
            raise RuntimeError("Web-Programmroot driftete während der Positivlisten-Härtung")
        return True
    except Exception as exc:
        perm_logger.error("Web-Programmrechte konnten nicht gehaertet werden: %s", exc)
        return False
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)

def check_file_permissions():
    """Prüft Dateien, die PHP schreiben muss (config/wallbox) und Python-Dateien."""
    print("\n=== Datei-Rechteprüfung ===\n")
    
    dynamic_log_file = None
    shared_tmp_state_files = {
        "/var/www/html/tmp/car_charge_session.json",
        "/var/www/html/tmp/car_charge_session_wb2.json",
        "/var/www/html/tmp/vital_stats.lock",
        "/var/www/html/ramdisk/manual_soc_wb1.json",
        "/var/www/html/ramdisk/manual_soc_wb2.json",
    }
    # Logfile-Pfad aus V4 JSON lesen (Single Source of Truth)
    try:
        v4_path = "/var/www/html/data/e3dc_v4.json"
        if os.path.lexists(v4_path):
            v4_data = _read_release_config_nofollow(v4_path)
            log_val = str(v4_data.get("logfile", "") or "").strip().strip('"').strip("'")
            if log_val:
                full_log_path = os.path.abspath(
                    log_val if log_val.startswith("/") else os.path.join(INSTALL_PATH, log_val)
                )
                allowed_log_roots = (
                    os.path.abspath(os.path.join(INSTALL_PATH, "logs")),
                    os.path.abspath("/var/www/html/logs"),
                )
                log_path_allowed = any(
                    full_log_path != root
                    and os.path.commonpath((root, full_log_path)) == root
                    for root in allowed_log_roots
                )
                if not log_path_allowed:
                    print(
                        f"{YELLOW}[i]{RESET} Benutzerdefinierter Logpfad liegt "
                        "außerhalb der verwalteten Logverzeichnisse und bleibt unverändert"
                    )
                    perm_logger.warning(
                        "Benutzerdefinierter Logpfad wird aus Kompatibilitäts- und "
                        "Sicherheitsgründen nicht automatisch verändert: %s",
                        full_log_path,
                    )
                else:
                    dynamic_log_file = full_log_path
                if dynamic_log_file and not any(d['path'] == full_log_path for d in FILE_DEFINITIONS):
                    FILE_DEFINITIONS.append({
                        "path": full_log_path, "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False
                    })
                    print(f"  Logdatei erkannt: {os.path.basename(full_log_path)}")
    except Exception:
        pass

    # Unbekannte lokale Dateien sind keine Produktinventar-Autorität. Nur die
    # explizite Definitionstabelle darf dieser Legacy-Prüfer verändern; die
    # vollständige Releaseprojektion gehört ausschließlich dem Ziel-Updater.

    issues = {}
    for fdef in FILE_DEFINITIONS:
        path = fdef["path"]
        expected_mode = fdef["mode"]
        expected_owner = fdef["owner"]
        expected_group = fdef["group"]
        is_optional = fdef["optional"]
        is_executable = fdef["executable"]
        file_name = os.path.basename(path)

        if not os.path.lexists(path):
            if not is_optional:
                print(f"{RED}✗{RESET} {file_name} fehlt")
                issues[path] = {"missing": True}
            continue
        try:
            st = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                print(f"{RED}✗{RESET} {file_name} ist keine reguläre Einzeldatei")
                issues[path] = {"unsafe": True}
                continue
            mode = f"{stat.S_IMODE(st.st_mode):o}"
            owner = pwd.getpwuid(st.st_uid).pw_name
            group = grp.getgrgid(st.st_gid).gr_name
            allowed_owners = {expected_owner}
            if os.path.abspath(path) == "/var/www/html/data/e3dc_v4.json":
                allowed_owners = _web_config_allowed_owners()
            owner_ok = owner in allowed_owners
            expected_owner_text = " oder ".join(sorted(allowed_owners))
            
            # Toleranz für RAM-Disk & TMP Dateien: Webserver (www-data) oder System-Dienste (root) erstellen diese oft.
            if not owner_ok and expected_owner == INSTALL_USER and owner in ["www-data", "root"]:
                if "/ramdisk/" in path or "/tmp/" in path:
                    owner_ok = True

            group_ok = group == expected_group
            mode_ok = mode == expected_mode
            
            # Toleranz für Datei-Modus (Webserver / C++ Binary erstellt oft mit 644 statt 664)
            if not mode_ok and mode == "644" and expected_mode == "664":
                if path not in shared_tmp_state_files and ("/ramdisk/" in path or "/tmp/" in path or path == dynamic_log_file):
                    mode_ok = True
                    
            # Toleranz für die Gruppe der E3DC-Control C++ Logdatei (wird meist als pi:pi angelegt)
            if not group_ok and path == dynamic_log_file and group == expected_owner:
                group_ok = True

            exec_ok = not is_executable or (is_executable and bool(st.st_mode & 0o111))
            if owner_ok and group_ok and mode_ok and exec_ok:
                exec_str = ", ausführbar" if is_executable else ""
                print(f"{GREEN}✓{RESET} {file_name} OK ({expected_owner_text}:{expected_group}, {expected_mode}{exec_str})")
            else:
                details = []
                if not owner_ok: details.append(f"Owner={owner} (soll: {expected_owner_text})")
                if not group_ok: details.append(f"Gruppe={group} (soll: {expected_group})")
                if not mode_ok: details.append(f"Modus={mode} (soll: {expected_mode})")
                if not exec_ok: details.append("nicht ausführbar")
                print(f"{RED}✗{RESET} {file_name} Problem: {', '.join(details)}")
                issues[path] = {
                    "owner": not owner_ok,
                    "group": not group_ok,
                    "mode": not mode_ok,
                    "exec": not exec_ok
                }
        except Exception as e:
            print(f"{RED}✗{RESET} Fehler bei {path}: {e}")
            issues[path] = {"error": str(e)}
    return issues


def fix_permissions(issues):
    """Korrigiert Installation-Verzeichnis-Rechte."""
    print("\n→ Korrigiere Verzeichnis-Berechtigungen…\n")
    success = True
    if "install_user_www_data_group" in issues:
        print(f"  → Füge {INSTALL_USER} zur Gruppe www-data hinzu")
        if _ensure_install_user_www_data_group():
            print(f"{GREEN}✓{RESET} {INSTALL_USER}: Gruppenmitgliedschaft www-data gesetzt")
        else:
            success = False
    traversal_paths = []
    required_traversal = _required_web_traversal_ancestors()
    for path in required_traversal:
        issue_key = (
            "home"
            if os.path.normpath(path)
            == os.path.normpath(os.path.abspath(str(INSTALL_HOME)))
            else f"web_traversal:{path}"
        )
        if issue_key in issues:
            traversal_paths.append(path)
    if traversal_paths:
        print("  → Binde enge Traversierrechte auf benötigte Installationspfad-Vorfahren")
        try:
            _project_required_web_traversal(tuple(traversal_paths))
            for path in traversal_paths:
                print(f"{GREEN}✓{RESET} {path}: Traversierrecht sicher gesetzt")
        except Exception as exc:
            print(
                f"{RED}[!]{RESET} Installationspfad-Traversierrechte wurden "
                f"nicht verändert: {exc}"
            )
            perm_logger.error(
                "Installationspfad-Traversierrechte konnten nicht sicher gesetzt werden: %s",
                exc,
            )
            success = False
    if "owner" in issues or "mode" in issues:
        print(
            f"  → Setze gebundene Stammrechte sicher: "
            f"{INSTALL_PATH} -> {INSTALL_USER}:www-data, Verzeichnis 755"
        )
        try:
            account = pwd.getpwnam(INSTALL_USER)
            group = grp.getgrnam("www-data")
            _set_live_directory_metadata(
                INSTALL_PATH,
                uid=account.pw_uid,
                gid=group.gr_gid,
                mode=0o755,
            )
            print(f"{GREEN}✓{RESET} {INSTALL_PATH}: Stammrechte fd-gebunden gesetzt")
        except Exception as exc:
            print(f"{RED}✗{RESET} Sichere Installations-Stammrechte fehlgeschlagen: {exc}")
            perm_logger.error("Sichere Installations-Stammrechte fehlgeschlagen: %s", exc)
            success = False
    if "installer_dir" in issues:
        print(f"  -> Setze Installer-Verzeichnisrechte: {INSTALLER_DIR} -> {INSTALL_USER}:www-data, 755")
        try:
            account = pwd.getpwnam(INSTALL_USER)
            group = grp.getgrnam("www-data")
            _set_live_directory_metadata(
                INSTALLER_DIR,
                uid=account.pw_uid,
                gid=group.gr_gid,
                mode=0o755,
            )
            print(f"{GREEN}OK{RESET} {INSTALLER_DIR}: fuer Web-Diagnose betretbar")
        except Exception as exc:
            perm_logger.error("Sichere Installer-Verzeichnisrechte fehlgeschlagen: %s", exc)
            success = False
    if "venv_unsafe" in issues:
        print(
            f"{RED}✗{RESET} Unsicherer venv-Pfad wird nicht automatisch verändert; "
            "bitte venv_name auf einen einzelnen Verzeichnisnamen zurücksetzen"
        )
        success = False
    if "venv_owner" in issues or "venv_mode" in issues:
        try:
            venv_name, venv_path, venv_identity = _validated_configured_venv_path()
        except Exception as exc:
            print(f"{RED}✗{RESET} Sichere venv-Bindung fehlgeschlagen: {exc}")
            perm_logger.error("Sichere venv-Bindung fehlgeschlagen: %s", exc)
            return False
        print(f"  → Projiziere Python-Umgebung sicher: {venv_name}")
        if not venv_path:
            print(f"{RED}✗{RESET} Konfigurierte Python-Umgebung fehlt")
            return False
        try:
            account = pwd.getpwnam(INSTALL_USER)

            def venv_contract(relative, metadata, is_directory):
                in_bin = bool(relative and relative[0] == "bin")
                mode = (
                    stat.S_IMODE(metadata.st_mode) | 0o111
                    if "venv_mode" in issues and in_bin
                    else None
                )
                return (
                    account.pw_uid if "venv_owner" in issues else None,
                    account.pw_gid if "venv_owner" in issues else None,
                    mode,
                )

            _normalize_permission_tree_fd(
                venv_path,
                venv_contract,
                expected_root_identity=venv_identity,
            )
            print(f"{GREEN}✓{RESET} {venv_name}: Rechte sicher projiziert")
        except Exception as exc:
            print(f"{RED}✗{RESET} Sichere venv-Rechteprojektion fehlgeschlagen: {exc}")
            perm_logger.error("Sichere venv-Rechteprojektion fehlgeschlagen: %s", exc)
            success = False
    if "notdir" in issues:
        print(f"{RED}✗{RESET} {INSTALL_PATH} ist keine Ordnerstruktur")
        success = False
    if "apache_php_module" in issues:
        print("  → Repariere Apache PHP-Modul…")
        run_command("sudo apt-get update", timeout=120)
        run_command("sudo apt-get install -y php php-cli libapache2-mod-php php-curl php-sqlite3 php-mbstring", timeout=300)
        run_command("sudo a2dismod mpm_event mpm_worker >/dev/null 2>&1 || true", timeout=30)
        run_command("sudo a2enmod mpm_prefork", timeout=30)
        php_cmd = (
            "PHP_MOD=$(php -r 'echo \"php\".PHP_MAJOR_VERSION.\".\".PHP_MINOR_VERSION;' 2>/dev/null); "
            "if [ -n \"$PHP_MOD\" ]; then sudo a2enmod \"$PHP_MOD\"; "
            "else sudo a2enmod php8.4 || sudo a2enmod php8.3 || sudo a2enmod php8.2 || sudo a2enmod php8.1 || sudo a2enmod php7.4; fi"
        )
        result = run_command(php_cmd, timeout=30)
        run_command("sudo apache2ctl configtest", timeout=30)
        restart = run_command("sudo systemctl restart apache2", timeout=30)
        if result['success'] and restart['success']:
            print(f"{GREEN}✓{RESET} Apache PHP-Modul aktiviert")
        else:
            print(f"{RED}✗{RESET} Apache PHP-Modul konnte nicht repariert werden")
            success = False

    return success


def fix_webportal_permissions(issues):
    """Korrigiert Webportal-Rechte."""
    print("\n→ Korrigiere Webportal-Berechtigungen…\n")
    issues = list(issues)
    success = True
    wp_path = "/var/www/html"
    matter_storage = f"{wp_path}/data/matter-storage"
    wallbox_plan_jobs = WALLBOX_PLAN_JOB_ROOT
    mode5_request = WALLBOX_MODE5_USER_START_REQUEST_FILE
    config_backup_dir = f"{wp_path}/data/config_backups"
    secret_dir_mode = config_secret_dir_mode_text()
    secret_file_mode = config_secret_file_mode_text()
    if "wp_unsafe" in issues:
        print(
            f"{RED}✗{RESET} Unsicherer Webroot wird nicht automatisch "
            "dereferenziert oder repariert."
        )
        return False
    for matter_issue in sorted(_private_matter_storage_issues(matter_storage)):
        issue_key = f"matter-storage_{matter_issue}"
        if issue_key not in issues:
            issues.append(issue_key)
    for planner_issue in sorted(_private_wallbox_plan_job_issues(wallbox_plan_jobs)):
        issue_key = f"wallbox-plan-jobs_{planner_issue}"
        if issue_key not in issues:
            issues.append(issue_key)
    unsafe_web_directories = sorted(
        issue
        for issue in issues
        if issue in {
            "wp_unsafe",
            "tmp_unsafe",
            "ramdisk_unsafe",
            "history_backups_unsafe",
            "luxtronik_archive_unsafe",
            "logs_unsafe",
            "data_unsafe",
        }
    )
    if unsafe_web_directories:
        print(
            f"{RED}✗{RESET} Unsichere Webverzeichnis-Namen werden nicht "
            "automatisch dereferenziert: " + ", ".join(unsafe_web_directories)
        )
        return False
    if not _repair_mode5_user_start_legacy_parent(mode5_request):
        print(
            f"{RED}✗{RESET} Persistente Modus-5-Anforderungsfläche ist "
            "unsicher; keine Webportal-Reparatur ausgeführt."
        )
        return False
    if not _ensure_install_user_www_data_group():
        success = False
    if "apache_php_module" in issues:
        print("  -> Repariere Apache PHP-Modul")
        run_command("sudo apt-get update", timeout=120)
        run_command("sudo apt-get install -y php php-cli libapache2-mod-php php-curl php-sqlite3 php-mbstring", timeout=300)
        run_command("sudo a2dismod mpm_event mpm_worker >/dev/null 2>&1 || true", timeout=30)
        run_command("sudo a2enmod mpm_prefork", timeout=30)
        php_cmd = (
            "PHP_MOD=$(php -r 'echo \"php\".PHP_MAJOR_VERSION.\".\".PHP_MINOR_VERSION;' 2>/dev/null); "
            "if [ -n \"$PHP_MOD\" ]; then sudo a2enmod \"$PHP_MOD\"; "
            "else sudo a2enmod php8.4 || sudo a2enmod php8.3 || sudo a2enmod php8.2 || sudo a2enmod php8.1 || sudo a2enmod php7.4; fi"
        )
        result = run_command(php_cmd, timeout=30)
        run_command("sudo apache2ctl configtest", timeout=30)
        restart = run_command("sudo systemctl restart apache2", timeout=30)
        if result['success'] and restart['success']:
            print(f"{GREEN}✓{RESET} Apache PHP-Modul aktiviert")
        else:
            print(f"{RED}✗{RESET} Apache PHP-Modul konnte nicht repariert werden")
            success = False
    if "wp_missing" in issues:
        print(f"  → Erstelle Webportal-Verzeichnis: {wp_path}")
        result = run_command(f"sudo mkdir -p {wp_path}")
        if result['success']:
            print(f"{GREEN}✓{RESET} {wp_path} erstellt")
        else:
            success = False
    if "wp_owner" in issues or "wp_mode" in issues:
        print("  → Härte Webroot und Programminhalte fd-relativ und ohne Symlink-Folgen")
        if harden_web_program_permissions(
            web_root=wp_path,
            install_user=INSTALL_USER,
            web_group="www-data",
        ):
            print(f"{GREEN}✓{RESET} Webroot und Programminhalte sicher gehärtet")
        else:
            print(f"{RED}✗{RESET} Webroot-Härtung fehlgeschlagen")
            success = False
    if "tmp_missing" in issues:
        print(f"  → Erstelle tmp-Verzeichnis: {wp_path}/tmp")
        result = run_command(f"sudo mkdir -p {wp_path}/tmp")
        if result['success']:
            print("✓ tmp-Ordner erstellt")
        else:
            success = False
    if "tmp_missing" in issues or "tmp_mode" in issues or "tmp_not_writable" in issues:
        print(f"  → Setze tmp-Rechte: {wp_path}/tmp -> 2775 (nicht-rekursiv)")
        result = run_command(f"sudo chmod 2775 {wp_path}/tmp")
        if result['success']:
            print(f"{GREEN}✓{RESET} tmp-Rechte korrigiert")
        else:
            success = False
    if "tmp_owner" in issues:
        print("  → tmp-Besitzer wird mit der sicheren Web-Laufzeitprojektion korrigiert")
    ramdisk_issues = {
        "ramdisk_missing",
        "ramdisk_unsafe",
        "ramdisk_owner",
        "ramdisk_mode",
        "ramdisk_not_mounted",
    }
    if ramdisk_issues.intersection(issues):
        print("  -> RAM-Disk-Vertrag ist unvollständig; richte ihn transaktional ein...")
        try:
            from .ramdisk import setup_ramdisk
            if setup_ramdisk() is True:
                print(f"{GREEN}[OK]{RESET} RAM-Disk automatisch eingerichtet (fstab + mount + Rechte).")
            else:
                print(f"{RED}[!]{RESET} Automatisches RAM-Disk Setup fehlgeschlagen oder abgebrochen.")
                success = False
        except Exception as _re:
            print(f"{RED}[!]{RESET} Automatisches RAM-Disk Setup fehlgeschlagen: {_re}")
            print(f"  -> Bitte manuell im Installer 'Rechte pruefen & korrigieren' erneut ausfuehren!")
            success = False


    # Hinzugefügt für History-Backups
    if "history_backups_missing" in issues:
        print(f"  → Erstelle Backup-Verzeichnis: {wp_path}/data/history_backups")
        result = run_command(f"sudo mkdir -p {wp_path}/data/history_backups")
        if result['success']:
            print(f"{GREEN}✓{RESET} history_backups-Ordner erstellt")
        else:
            success = False
    if "history_backups_owner" in issues:
        print("  → Backup-Besitzer wird mit der sicheren Web-Laufzeitprojektion korrigiert")
    if "history_backups_missing" in issues or "history_backups_mode" in issues:
        print(
            f"  → Setze Backup-Verzeichnis Rechte: "
            f"{wp_path}/data/history_backups -> "
            f"{secret_dir_mode}/{secret_file_mode}"
        )
        print("    fd-relative Projektion vorgemerkt")

    if "luxtronik_archive_missing" in issues:
        print(f"  → Erstelle Archiv-Verzeichnis: {wp_path}/data/luxtronik_archive")
        result = run_command(f"sudo mkdir -p {wp_path}/data/luxtronik_archive")
        if result['success']:
            print(f"{GREEN}✓{RESET} luxtronik_archive-Ordner erstellt")
        else:
            success = False
    if "luxtronik_archive_owner" in issues:
        print("  → Archiv-Besitzer wird mit der sicheren Web-Laufzeitprojektion korrigiert")
    if "luxtronik_archive_missing" in issues or "luxtronik_archive_mode" in issues:
        print(
            f"  → Setze Archiv-Verzeichnis Rechte: "
            f"{wp_path}/data/luxtronik_archive -> "
            f"{secret_dir_mode}/{secret_file_mode}"
        )
        print("    fd-relative Projektion vorgemerkt")

    if "logs_missing" in issues:
        print(f"  → Erstelle Log-Verzeichnis: {wp_path}/logs")
        result = run_command(
            f"sudo install -d -m 2775 -o {INSTALL_USER} -g www-data {wp_path}/logs"
        )
        if not result['success']:
            success = False
    if "logs_owner" in issues:
        print("  → Log-Besitzer wird mit der sicheren Web-Laufzeitprojektion korrigiert")
    if "logs_missing" in issues or "logs_mode" in issues:
        print(f"  → Setze Log-Verzeichnis Rechte: {wp_path}/logs -> 2775")
        result = run_command(f"sudo chmod 2775 {wp_path}/logs")
        if not result['success']:
            success = False

    if "matter-storage_missing" in issues:
        print(f"  → Erstelle Matter-Storage: {wp_path}/data/matter-storage")
        result = run_command(
            f"sudo install -d -m 700 -o {INSTALL_USER} -g www-data "
            f"{wp_path}/data/matter-storage"
        )
        if result['success']:
            print(f"{GREEN}✓{RESET} matter-storage-Ordner erstellt")
        else:
            success = False
    if "matter-storage_owner" in issues and "matter-storage_unsafe" not in issues:
        print("  → Matter-Storage-Besitzer wird fd-relativ korrigiert")
    if (
        ("matter-storage_missing" in issues or "matter-storage_mode" in issues)
        and "matter-storage_unsafe" not in issues
    ):
        print(f"  → Matter-Storage-Rechte werden fd-relativ auf 700/600 gesetzt")
    if "matter-storage_unsafe" in issues:
        print(f"{RED}✗{RESET} Matter-Storage enthält unsichere Links oder Sonderdateien; keine automatische Änderung.")
        success = False

    if "data_missing" in issues:
        print(f"  → Erstelle Datenbank-Verzeichnis: {wp_path}/data")
        result = run_command(f"sudo mkdir -p {wp_path}/data")
        if result['success']:
            print(f"{GREEN}✓{RESET} data-Ordner erstellt")
        else:
            success = False
    if "data_owner" in issues:
        print("  → Daten-Besitzer wird mit typisierten Ausnahmen fd-relativ korrigiert")
    if "data_missing" in issues or "data_mode" in issues:
        print(
            f"  -> Setze Datenbank-Verzeichnis Rechte: "
            f"{wp_path}/data -> {secret_dir_mode}"
        )
        result = run_command(f"sudo chmod {secret_dir_mode} {wp_path}/data")
        if not result['success']:
            success = False

    # Der Planner-Transaktionsbaum gehört ausschließlich dem PHP-Prozess.
    # Alle breiten Web-/data-Reparaturen prunen ihn; erst danach wird seine
    # private 0700/0600-Grenze als eigener Endzustand gesetzt.
    if "wallbox-plan-jobs_missing" in issues:
        print(f"  → Erstelle privaten Wallbox-Planer-Transaktionsbaum: {wallbox_plan_jobs}")
        result = run_command(
            f"sudo install -d -m 0700 -o www-data -g www-data {wallbox_plan_jobs}"
        )
        if not result["success"]:
            print(f"{RED}✗{RESET} Privater Wallbox-Planer-Transaktionsbaum konnte nicht erstellt werden.")
            success = False
    if "wallbox-plan-jobs_unsafe" in issues:
        print(
            f"{RED}✗{RESET} Wallbox-Planer-Transaktionsbaum enthält Links, "
            "Sonderdateien oder Mehrfachlinks; keine automatische Änderung."
        )
        success = False
    elif os.path.lexists(wallbox_plan_jobs):
        print(
            "  → Setze privaten Wallbox-Planer-Transaktionsbaum: "
            f"{wallbox_plan_jobs} -> www-data:www-data 700/600"
        )
        print("    fd-relative Projektion vorgemerkt")

    if os.path.isdir(config_backup_dir):
        print(
            f"  -> Config-Backup-Rechte werden fd-relativ auf "
            f"{secret_dir_mode}/{secret_file_mode} projiziert"
        )

    try:
        print("  → Projiziere alle Web-Laufzeitrechte fd-relativ und mountgebunden")
        _normalize_web_runtime_permissions(wp_path, config=load_config())
        print(f"{GREEN}✓{RESET} Web-Laufzeitrechte sicher projiziert")
    except Exception as exc:
        print(f"{RED}✗{RESET} Sichere Web-Laufzeitprojektion fehlgeschlagen: {exc}")
        perm_logger.error("Sichere Web-Laufzeitprojektion fehlgeschlagen: %s", exc)
        success = False

    if os.path.lexists(matter_storage):
        remaining = _private_matter_storage_issues(matter_storage)
        if remaining:
            print(
                f"{RED}✗{RESET} Private Matter-Rechte bleiben unvollständig: "
                + ", ".join(sorted(remaining))
            )
            success = False
    if os.path.lexists(wallbox_plan_jobs):
        remaining = _private_wallbox_plan_job_issues(wallbox_plan_jobs)
        if remaining:
            print(
                f"{RED}✗{RESET} Private Wallbox-Planer-Rechte bleiben unvollständig: "
                + ", ".join(sorted(remaining))
            )
            success = False

    if "apache_service" in issues:
        print("  → Repariere Apache Webserver (Start & Enable)…")
        run_command("sudo systemctl enable apache2")
        result = run_command("sudo systemctl start apache2")
        if result['success']:
            print(f"{GREEN}✓{RESET} Apache gestartet und für Autostart aktiviert")
        else:
            print(f"{RED}✗{RESET} Fehler beim Starten von Apache.")
            success = False

    if not harden_web_program_permissions(
        web_root=wp_path,
        install_user=INSTALL_USER,
        web_group="www-data",
    ):
        print(
            f"{RED}✗{RESET} Webroot/Programmbaum konnten nicht auf den "
            "root-kontrollierten Endzustand gehärtet werden."
        )
        success = False

    if not _mode5_user_start_request_surface_safe(mode5_request):
        print(
            f"{RED}✗{RESET} Persistente Modus-5-Anforderungsfläche "
            "wechselte während der Webportal-Reparatur."
        )
        success = False
    return success


def _aux_inverter_migration_backup_structure_safe(path):
    if not os.path.lexists(path):
        return True
    try:
        root_stat = os.lstat(path)
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            return False
        for directory, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
            for name in dirnames:
                metadata = os.lstat(os.path.join(directory, name))
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    return False
            for name in filenames:
                metadata = os.lstat(os.path.join(directory, name))
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    return False
        return True
    except OSError:
        return False


def _verify_aux_inverter_migration_backup_modes(path):
    if not _aux_inverter_migration_backup_structure_safe(path):
        return False
    if not os.path.lexists(path):
        return True
    if stat.S_IMODE(os.lstat(path).st_mode) != 0o700:
        return False
    for directory, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
        if stat.S_IMODE(os.lstat(directory).st_mode) != 0o700:
            return False
        for name in dirnames:
            if stat.S_IMODE(os.lstat(os.path.join(directory, name)).st_mode) != 0o700:
                return False
        for name in filenames:
            if stat.S_IMODE(os.lstat(os.path.join(directory, name)).st_mode) != 0o600:
                return False
    return True


def _harden_aux_inverter_migration_backups(path):
    if not os.path.lexists(path):
        return True
    if not _aux_inverter_migration_backup_structure_safe(path):
        return False
    try:
        account = pwd.getpwnam(INSTALL_USER)
        group = grp.getgrnam("www-data")
        _normalize_permission_tree_fd(
            path,
            lambda _relative, _metadata, is_directory: (
                account.pw_uid,
                group.gr_gid,
                0o700 if is_directory else 0o600,
            ),
            reject_unsafe_entries=True,
        )
    except Exception as exc:
        perm_logger.error(
            "Zusatz-WR-Migrationsbackups konnten nicht sicher gehärtet werden: %s",
            exc,
        )
        return False
    return _verify_aux_inverter_migration_backup_modes(path)


def cleanup_root_owned_files(*, program_files=None, program_directories=None):
    """Härtet ausschließlich die explizit getrennten Web-Programmpfade.

    Der Produktbaum wird vom Ziel-Updater über das gebundene Releaseinventar
    projiziert. Ein rekursiver Chown unbekannter Dateien wäre weder eine
    belastbare Produktinventur noch mit privaten lokalen Nebenpfaden vereinbar.
    """
    print("\n■ Prüfe gebundene Web-Programmrechte…\n")

    try:
        # Der Webroot selbst bleibt root-kontrolliert; Programminhalte und
        # Laufzeitflächen werden durch zwei getrennte fd-relative Verträge
        # normalisiert.
        if os.path.exists("/var/www/html"):
            if not harden_web_program_permissions(
                web_root="/var/www/html",
                install_user=INSTALL_USER,
                web_group="www-data",
                program_files=program_files,
                program_directories=program_directories,
            ):
                return False
            _normalize_web_runtime_permissions("/var/www/html", config=load_config())
    except Exception as e:
        print(f"{RED}✗{RESET} Fehler beim Scannen: {e}")
        return False

    print(f"{GREEN}✓{RESET} Gebundene Web-Programmrechte geprüft\n")
    return True


def cleanup_stale_v4_processes():
    """Beendet alte Einzelstarts und doppelte Daemons ausserhalb von systemd."""
    print("\n■ Prüfe auf alte/hängende V4-Prozesse…\n")

    once_patterns = [
        "storage_simulator.py --once",
        "pv_forecast_service.py --once",
    ]
    killed = []

    def _pgrep(pattern):
        try:
            res = subprocess.run(
                ["pgrep", "-af", pattern],
                text=True, capture_output=True, check=False
            )
            if res.returncode != 0:
                return []
            rows = []
            for line in res.stdout.splitlines():
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2 and parts[0].isdigit():
                    rows.append((int(parts[0]), parts[1]))
            return rows
        except Exception:
            return []

    def _kill_pids(pids, reason):
        nonlocal killed
        pids = sorted({p for p in pids if p > 1 and p != os.getpid()})
        if not pids:
            return
        pid_list = " ".join(str(p) for p in pids)
        run_command(f"sudo kill -TERM {pid_list} 2>/dev/null || true")
        time.sleep(0.5)
        run_command(f"sudo kill -KILL {pid_list} 2>/dev/null || true")
        killed.extend(pids)
        print(f"  {GREEN}✓{RESET} {reason}: {pid_list}")

    for pattern in once_patterns:
        _kill_pids([pid for pid, _cmd in _pgrep(pattern)], f"hängende {pattern}")

    service_scripts = {
        "e3dc-storage-simulator": ["storage_simulator.py"],
        "e3dc-storage-manager": ["storage_manager.py"],
        "e3dc-live": ["e3dc_live.py"],
        "e3dc-epex-manager": ["epex_manager.py"],
        "e3dc-wallbox-manager": ["wallbox_manager.py"],
        "e3dc-heizstab": ["heizstab_manager.py"],
    }
    for module in iter_modules():
        if not module.service or not module.script:
            continue
        script_name = os.path.basename(module.script)
        if not script_name:
            continue
        scripts = service_scripts.setdefault(module.service, [])
        if script_name not in scripts:
            scripts.append(script_name)

    for service, scripts in service_scripts.items():
        rows = []
        for script in scripts:
            rows.extend(_pgrep(script))
        if len(rows) <= 1:
            continue
        main_pid = 0
        try:
            res = run_command(f"systemctl show -p MainPID --value {service}")
            main_pid = int((res.get("stdout") or "0").strip() or "0")
        except Exception:
            main_pid = 0
        stale = [pid for pid, cmd in rows if pid != main_pid and "--once" not in cmd]
        _kill_pids(stale, f"doppelte {'/'.join(scripts)}-Prozesse")

    if killed:
        perm_logger.info(f"Alte/haengende V4-Prozesse beendet: {killed}")
    else:
        print(f"{GREEN}✓{RESET} Keine alten V4-Prozesse gefunden")

def cleanup_legacy_plots():
    """Entfernt nur feste Legacy-Leaves aus gehaltenen, echten Verzeichnissen."""

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory:
        perm_logger.error("Sichere Legacy-Plot-Bereinigung ist nicht verfügbar")
        return False

    bindings = []

    def verify_root(binding):
        parent = os.fstat(binding["parent_fd"])
        opened = os.fstat(binding["root_fd"])
        named = os.stat(
            binding["name"],
            dir_fd=binding["parent_fd"],
            follow_symlinks=False,
        )
        if (
            (parent.st_dev, parent.st_ino) != binding["parent_identity"]
            or not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != binding["root_identity"]
            or (named.st_dev, named.st_ino) != binding["root_identity"]
        ):
            raise RuntimeError(
                f"Legacy-Cleanup-Wurzel wurde ausgetauscht: {binding['path']}"
            )

    def bind_root(path, *, parent_binding=None, require_mount_id=None):
        normalized = os.path.abspath(str(path))
        parent_path, name = os.path.split(normalized)
        if not name:
            raise RuntimeError("Dateisystemwurzel ist kein Legacy-Cleanup-Ziel")
        if parent_binding is None:
            try:
                parent_fd = _open_absolute_directory_nofollow(parent_path)
            except FileNotFoundError:
                return None
        else:
            verify_root(parent_binding)
            parent_fd = os.dup(parent_binding["root_fd"])
        root_fd = -1
        try:
            try:
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.close(parent_fd)
                return None
            if stat.S_ISLNK(named.st_mode) or not stat.S_ISDIR(named.st_mode):
                raise RuntimeError(f"Legacy-Cleanup-Wurzel ist unsicher: {normalized}")
            root_fd = os.open(
                name,
                os.O_RDONLY | nofollow | directory | cloexec,
                dir_fd=parent_fd,
            )
            opened = os.fstat(root_fd)
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise RuntimeError(
                    f"Legacy-Cleanup-Wurzel driftete beim Öffnen: {normalized}"
                )
            mount_id = _permission_mount_id(root_fd)
            if require_mount_id is not None and mount_id != require_mount_id:
                raise RuntimeError(
                    f"Fremder Mount ist kein Legacy-Cleanup-Ziel: {normalized}"
                )
            parent = os.fstat(parent_fd)
            binding = {
                "path": normalized,
                "name": name,
                "parent_fd": parent_fd,
                "root_fd": root_fd,
                "parent_identity": (parent.st_dev, parent.st_ino),
                "root_identity": (opened.st_dev, opened.st_ino),
                "mount_id": mount_id,
            }
            verify_root(binding)
            bindings.append(binding)
            return binding
        except Exception:
            if root_fd >= 0:
                os.close(root_fd)
            os.close(parent_fd)
            raise

    def delete_leaf(binding, name, *, large_only=False):
        verify_root(binding)
        try:
            before = os.stat(name, dir_fd=binding["root_fd"], follow_symlinks=False)
        except FileNotFoundError:
            return False
        if large_only and (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
        ):
            perm_logger.warning("Unsicheres optionales nohup.out bleibt unverändert")
            return False
        if stat.S_ISREG(before.st_mode):
            if before.st_nlink != 1:
                raise RuntimeError(f"Legacy-Cleanup-Leaf besitzt Hardlinks: {name}")
            opened_receipt = None
            descriptor = os.open(
                name,
                os.O_RDONLY
                | nofollow
                | getattr(os, "O_NONBLOCK", 0)
                | cloexec,
                dir_fd=binding["root_fd"],
            )
            try:
                opened = os.fstat(descriptor)
                if (
                    (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or opened.st_nlink != 1
                    or _permission_mount_id(descriptor) != binding["mount_id"]
                ):
                    raise RuntimeError(f"Legacy-Cleanup-Leaf driftete: {name}")
                if large_only and opened.st_size <= 1024 * 1024:
                    return False
                opened_receipt = (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_mode,
                    opened.st_nlink,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
            finally:
                os.close(descriptor)
        elif stat.S_ISLNK(before.st_mode) and not large_only:
            pass
        else:
            raise RuntimeError(f"Legacy-Cleanup-Leaf besitzt unsicheren Typ: {name}")

        verify_root(binding)
        current = os.stat(name, dir_fd=binding["root_fd"], follow_symlinks=False)
        current_receipt = (
            current.st_dev,
            current.st_ino,
            current.st_mode,
            current.st_nlink,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        )
        expected_receipt = (
            opened_receipt
            if stat.S_ISREG(before.st_mode)
            else (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
        )
        if current_receipt != expected_receipt:
            raise RuntimeError(f"Legacy-Cleanup-Leaf wurde vor dem Löschen ersetzt: {name}")
        os.unlink(name, dir_fd=binding["root_fd"])
        os.fsync(binding["root_fd"])
        verify_root(binding)
        return True

    install_names = (
        "plot_soc_changes.py",
        "plot_live_history.py",
        "diagram_helpers.py",
        "diagram_config.json",
    )
    web_names = (
        "diagramm.html",
        "archiv_diagramm.html",
        "diagramm_mobile.html",
        "live_diagramm.html",
    )
    tmp_names = (
        "plot_soc_done",
        "plot_soc_done_archiv",
        "plot_soc_done_mobile",
        "plot_soc_error",
        "plot_soc_error_archiv",
        "plot_soc_error_mobile",
        "plot_live_history_last_run",
        "plot_soc_last_run",
        "plot_soc_running",
        "plot_soc_running_mobile",
        "plot_soc_running_archiv",
        "plot_live_history_running",
    )

    has_cleaned = False
    try:
        install_binding = bind_root(INSTALL_PATH)
        web_binding = bind_root("/var/www/html")
        tmp_binding = (
            bind_root(
                "/var/www/html/tmp",
                parent_binding=web_binding,
                require_mount_id=web_binding["mount_id"],
            )
            if web_binding is not None
            else None
        )
        if install_binding is not None:
            for name in install_names:
                has_cleaned = delete_leaf(install_binding, name) or has_cleaned
            has_cleaned = (
                delete_leaf(install_binding, "nohup.out", large_only=True)
                or has_cleaned
            )
        if web_binding is not None:
            for name in web_names:
                has_cleaned = delete_leaf(web_binding, name) or has_cleaned
        if tmp_binding is not None:
            for name in tmp_names:
                has_cleaned = delete_leaf(tmp_binding, name) or has_cleaned
        for binding in bindings:
            verify_root(binding)
    except Exception as exc:
        perm_logger.error("Legacy-Plot-Bereinigung wurde fail-closed beendet: %s", exc)
        print(f"{RED}✗{RESET} Sichere Legacy-Plot-Bereinigung fehlgeschlagen: {exc}")
        return False
    finally:
        for binding in reversed(bindings):
            os.close(binding["root_fd"])
            os.close(binding["parent_fd"])

    if has_cleaned:
        print(f"  {GREEN}✓{RESET} Veraltete Python-Plot-Skripte und HTML-Caches entfernt")
        perm_logger.info("Veraltete Plot-Skripte und Caches entfernt.")
    return True

def fix_file_permissions(issues):
    """Korrigiert Datei-Rechte basierend auf den FILE_DEFINITIONS."""
    if not issues:
        return True
    print("\n→ Korrigiere Datei-Berechtigungen…\n")
    success = True
    # Erstelle eine Map von Pfad zu Definition für schnellen Zugriff
    defs_map = {d["path"]: d for d in FILE_DEFINITIONS}
    for path, file_issues in issues.items():
        if path not in defs_map:
            perm_logger.warning(f"Keine Definition für Pfad gefunden: {path}")
            success = False
            continue
        definition = defs_map[path]
        expected_owner = definition["owner"]
        expected_group = definition["group"]
        expected_mode = definition["mode"]
        file_name = os.path.basename(path)
        # Produkt- und Webprogrammdateien dürfen niemals als leere Attrappe
        # erzeugt werden. Ihre Bytes kann nur ein gebundener Release-Updater
        # wiederherstellen.
        if file_issues.get("missing"):
            print(f"{RED}✗{RESET} {file_name} fehlt; Wiederherstellung aus dem gebundenen Release erforderlich")
            perm_logger.error("Fehlende Produktdatei wird nicht leer erzeugt: %s", path)
            success = False
            continue
        if file_issues.get("unsafe"):
            print(f"{RED}✗{RESET} {file_name} besitzt einen unsicheren Pfadtyp")
            success = False
            continue
        try:
            owner = pwd.getpwnam(expected_owner)
            group = grp.getgrnam(expected_group)
            _set_live_regular_file_metadata(
                path,
                uid=owner.pw_uid,
                gid=group.gr_gid,
                mode=int(str(expected_mode), 8),
            )
            print(
                f"{GREEN}✓{RESET} {file_name}: "
                f"{expected_owner}:{expected_group} {expected_mode} fd-gebunden gesetzt"
            )
        except Exception as exc:
            success = False
            perm_logger.error("Sichere Dateirechteprojektion fehlgeschlagen für %s: %s", path, exc)
            print(f"{RED}✗{RESET} {file_name}: sichere Rechteprojektion fehlgeschlagen")
    return success

def cleanup_legacy_cronjobs():
    """Prüft alte Cronjobs; die Notifier-Transaktion entfernt sie später."""
    print("\n■ Prüfe veraltete Cronjobs (Bereinigung folgt transaktional)…\n")
    users_to_check = [INSTALL_USER, "root", "www-data"]
    markers = (
        "get_live_json.php",
        "sqlite_archiver.py",
        "diagram_helpers.py",
        "boot_notify.sh",
        "send_daily_telegram.php",
        "send_status_telegram.php",
        "send_weekly_telegram.php",
        "backup_history.php",
        "plot_soc_changes.py",
    )
    found = []
    for u in users_to_check:
        res = run_command(f"sudo crontab -u {u} -l")
        if res['success']:
            lines = res['stdout'].splitlines()
            if any(any(marker in line for marker in markers) for line in lines):
                found.append(u)
            continue
        message = str(res.get("stderr") or "").strip().lower()
        if res.get("returncode") == 1 and f"no crontab for {u}".lower() in message:
            continue
        print(f"  {RED}✗{RESET} Crontab für '{u}' ist nicht eindeutig lesbar")
        return False

    if found:
        print(
            f"  {YELLOW}[i]{RESET} Alte Einträge bei {', '.join(found)} bleiben "
            "bis zur persistenten Notifier-Transaktion unverändert."
        )
    else:
        print(f"  {GREEN}✓{RESET} Keine alten Cronjobs gefunden")
    return True


def check_wrapper_integrity():
    """Bindet Quellen und root-eigene Launcher read-only an Git-HEAD."""
    print("\n=== Wrapper-Integritätsprüfung (lokaler Git-HEAD) ===\n")
    try:
        if __package__ in (None, ""):
            from Installer import web_installer
        else:
            from . import web_installer
        result = web_installer.wrapper_integrity_preview(INSTALL_ROOT)
        root_launchers = (
            (
                "Root-eigener Service-Launcher",
                web_installer.service_launcher_integrity_preview(),
            ),
            (
                "Root-eigener Web-Update-Launcher",
                web_installer.web_update_launcher_integrity_preview(),
            ),
        )
    except Exception as exc:
        print(f"{RED}✗{RESET} Wrapper-Integrität konnte nicht geprüft werden: {exc}")
        return [{"wrapper_integrity": True, "status": "check_error", "error": str(exc)}]

    sources_ok = bool(result.get("success")) and not result.get("repair_needed")
    root_launchers_ok = all(
        bool(preview.get("success")) for _label, preview in root_launchers
    )
    if sources_ok and root_launchers_ok:
        print(
            f"{GREEN}✓{RESET} Wrapperquellen und root-eigene Launcher stimmen "
            f"bytegenau mit Git-HEAD {result.get('head', '')[:12]} überein."
        )
        return []

    labels = {
        "missing": "fehlt und kann ausschließlich aus Git-HEAD wiederhergestellt werden",
        "crlf_only": "hat ausschließlich CRLF-Zeilenenden und kann atomar aus Git-HEAD repariert werden",
        "mode_drift": "stimmt bytegenau mit Git-HEAD überein, ist aber nicht mit Modus 0755 ausführbar",
        "content_drift": "weicht inhaltlich von Git-HEAD ab; automatische Reparatur ist gesperrt",
        "symlink": "ist ein Symlink; automatische Reparatur ist gesperrt",
        "hardlink": "hat mehrere Hardlinks; automatische Reparatur ist gesperrt",
        "not_regular": "ist keine reguläre Datei; automatische Reparatur ist gesperrt",
        "read_error": "konnte nicht sicher gelesen werden",
    }
    issues = []
    if not sources_ok:
        for item in result.get("items", []):
            status = str(item.get("status") or "unknown")
            if status == "ok":
                continue
            detail = labels.get(status, f"hat den unbekannten Zustand {status}")
            print(f"{RED}✗{RESET} {item.get('path')}: {detail}.")
        for blocker in result.get("hard_blockers", []):
            if blocker.get("status") == "head_error":
                print(f"{RED}✗{RESET} Git-HEAD-Bindung fehlgeschlagen: {blocker.get('error', 'unbekannt')}")
        issues.append({
            "wrapper_integrity": True,
            "file": INSTALLER_DIR,
            "head": result.get("head"),
            "repairable": bool(result.get("success")),
            "result": result,
        })

    for label, preview in root_launchers:
        if preview.get("success"):
            continue
        status = str(preview.get("status") or "unknown")
        print(
            f"{RED}✗{RESET} {label} {preview.get('path')}: "
            f"stimmt nicht mit dem gebundenen Git-HEAD überein ({status})."
        )
        issues.append({
            "root_launcher_integrity": True,
            "file": preview.get("path"),
            "head": preview.get("head") or result.get("head"),
            "status": status,
            "result": preview,
        })
    return issues


def check_sudoers_permissions():
    """Prüft, ob www-data die notwendigen Sudo-Rechte für Web-Funktionen hat."""
    print("\n=== Sudoers-Prüfung (Web-Funktionen) ===\n")
    perm_logger.info("--- Starte Sudoers-Prüfung ---")

    try:
        if __package__ in (None, ""):
            from Installer import web_installer
        else:
            from . import web_installer
        sudoers_findings = web_installer.sudoers_file_findings()
        wrapper_sudoers_content = web_installer.desired_sudoers_content().strip()
        sudoers_file_path = str(web_installer.SUDOERS_FILE)
    except Exception as e:
        print(f"{RED}FEHLER{RESET} Zentrale Sudoers-Klassifizierung fehlgeschlagen: {e}")
        perm_logger.error("Zentrale Sudoers-Klassifizierung fehlgeschlagen: %s", e)
        return [{"scan_error": True, "file": "/etc/sudoers.d", "error": str(e)}]

    expected_sudoers_files = [
        {
            "file": sudoers_file_path,
            "content": wrapper_sudoers_content,
            "description": "WebUI Service-, Update- und Installer-Wrapper"
        }
    ]

    issues = []
    repairable_findings = [
        f"{item.get('file')}:{item.get('line_no')}: {item.get('line')}"
        for item in sudoers_findings.get("repairable_lines", [])
    ]
    external_systemctl_findings = [
        f"{item.get('file')}:{item.get('line_no')}: {item.get('line')}"
        for item in sudoers_findings.get("external_systemctl_lines", [])
    ]
    external_web_findings = [
        f"{item.get('file')}:{item.get('line_no')}: {item.get('line')}"
        for item in sudoers_findings.get("external_direct_web_lines", [])
    ]
    external_e3dc_systemctl_findings = [
        f"{item.get('file')}:{item.get('line_no')}: {item.get('line')}"
        for item in sudoers_findings.get("e3dc_systemctl_lines", [])
        if item.get("scope") != "e3dc"
    ]

    for sudo_def in expected_sudoers_files:
        sudoers_file = sudo_def["file"]
        expected_content = sudo_def["content"]
        description = sudo_def["description"]

        if not os.path.exists(sudoers_file):
            print(f"{RED}✗{RESET} Sudoers-Datei für '{description}' fehlt: {os.path.basename(sudoers_file)}.")
            issues.append({"missing": True, "file": sudoers_file, "content": expected_content})
        else:
            try:
                st = os.stat(sudoers_file)
                owner = pwd.getpwuid(st.st_uid).pw_name
                group = grp.getgrgid(st.st_gid).gr_name
                mode = oct(st.st_mode)[-4:] # 4-stellig für 0440

                with open(sudoers_file, "r") as f:
                    content = f.read().strip()
                
                content_ok = (content == expected_content)
                owner_ok = (owner == "root")
                group_ok = (group == "root")
                mode_ok = (mode == "0440")

                if content_ok and owner_ok and group_ok and mode_ok:
                    print(f"{GREEN}✓{RESET} Sudoers-Konfiguration für '{description}' korrekt.")
                    perm_logger.info(f"Sudoers-Konfiguration '{description}' korrekt.")
                else:
                    details = []
                    if not content_ok: details.append("Inhalt veraltet")
                    if not owner_ok: details.append(f"Owner={owner} (soll: root)")
                    if not group_ok: details.append(f"Gruppe={group} (soll: root)")
                    if not mode_ok: details.append(f"Rechte={mode} (soll: 0440)")
                    print(f"{RED}✗{RESET} Sudoers-Problem für '{description}': {', '.join(details)}.")
                    issues.append({"missing": False, "file": sudoers_file, "content": expected_content})
            except Exception as e:
                print(f"{RED}✗{RESET} Fehler beim Lesen von {sudoers_file}: {e}")
                perm_logger.error(f"Fehler beim Lesen von {sudoers_file}: {e}")
                issues.append({"error": True, "file": sudoers_file})
    if repairable_findings:
        print(f"{RED}FEHLER{RESET} Alte direkte E3DC-WebUI-sudoers-Freigaben gefunden ({len(repairable_findings)} Zeile(n)).")
        for finding in repairable_findings[:8]:
            print(f"  - {finding}")
        if len(repairable_findings) > 8:
            print(f"  ... {len(repairable_findings) - 8} weitere Zeile(n)")
        perm_logger.warning("Alte direkte E3DC-WebUI-sudoers-Freigaben gefunden: %s", repairable_findings)
        issues.append({
            "legacy_cleanup": True,
            "file": "/etc/sudoers.d",
            "content": wrapper_sudoers_content,
            "findings": repairable_findings,
        })
    if external_web_findings:
        print(
            f"{RED}FEHLER{RESET} Fremdes sudoers-Fragment gewährt www-data direkte Kommandos "
            f"({len(external_web_findings)} Zeile(n)); der E3DC-Installer verändert es nicht automatisch."
        )
        for finding in external_web_findings[:8]:
            print(f"  - {finding}")
        if len(external_web_findings) > 8:
            print(f"  ... {len(external_web_findings) - 8} weitere Zeile(n)")
        perm_logger.error(
            "Fremde direkte www-data-Freigaben blockieren fail-closed: %s",
            external_web_findings,
        )
        issues.append({
            "external_direct_web": True,
            "file": "/etc/sudoers.d",
            "findings": external_web_findings,
        })
    if external_e3dc_systemctl_findings:
        print(
            f"{RED}FEHLER{RESET} Fremdes sudoers-Fragment steuert E3DC-Dienste direkt "
            f"({len(external_e3dc_systemctl_findings)} Zeile(n)); "
            "der E3DC-Installer verändert es nicht automatisch."
        )
        for finding in external_e3dc_systemctl_findings[:8]:
            print(f"  - {finding}")
        if len(external_e3dc_systemctl_findings) > 8:
            print(f"  ... {len(external_e3dc_systemctl_findings) - 8} weitere Zeile(n)")
        perm_logger.error(
            "Fremde direkte E3DC-systemctl-Freigaben blockieren fail-closed: %s",
            external_e3dc_systemctl_findings,
        )
        issues.append({
            "external_e3dc_systemctl": True,
            "file": "/etc/sudoers.d",
            "findings": external_e3dc_systemctl_findings,
        })
    if external_systemctl_findings:
        print(
            f"{YELLOW}⚠{RESET} Fremdverwaltete direkte sudoers-Freigaben gefunden "
            f"({len(external_systemctl_findings)} Zeile(n)); sie bleiben unverändert und blockieren das Update nicht."
        )
        for finding in external_systemctl_findings[:8]:
            print(f"  - {finding}")
        if len(external_systemctl_findings) > 8:
            print(f"  ... {len(external_systemctl_findings) - 8} weitere Zeile(n)")
        perm_logger.warning(
            "Fremdverwaltete sudoers-Freigaben bleiben unverändert: %s",
            external_systemctl_findings,
        )
    return issues


def _print_update_readiness_blockers(readiness):
    """Gibt jeden harten Endgate-Blocker mit Ziel und nächster Prüfung aus."""

    if not isinstance(readiness, dict):
        return
    blockers = readiness.get("hard_blockers") or []
    if not isinstance(blockers, list) or not blockers:
        return

    print(f"\n{RED}[ABBRUCH] E3DC-UPD-READINESS-BLOCKED{RESET}")
    print("Was ist passiert: Die Rechteprojektion bestand ihr abschließendes Freigabegate nicht.")
    print("Was läuft jetzt: Der Updater führt den gebundenen automatischen Rückfall aus.")
    print("Betroffene Prüfungen:")
    for index, blocker in enumerate(blockers, start=1):
        if not isinstance(blocker, dict):
            print(f"  {index}. Unlesbarer Readiness-Blocker")
            continue
        label = str(blocker.get("label") or "Unbenannte Prüfung").strip()
        issue = str(blocker.get("issue") or "Prüfung ist nicht erfüllt").strip()
        path = str(blocker.get("path") or "").strip()
        status = str(blocker.get("status") or "").strip()
        print(f"  {index}. {label}: {issue}")
        if path:
            print(f"     Datei/Unit: {path}")
        if status:
            print(f"     Zustand: {status}")
        details = blocker.get("details")
        if details not in (None, "", [], {}):
            try:
                encoded = json.dumps(details, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                encoded = str(details)
            print(f"     Details: {encoded[:2000]}")

    print("Lösung und Ziel:")
    print("  Die oben genannte Datei oder Unit zuerst korrigieren; danach muss das Readiness-Gate 0 harte Blocker melden.")
    print("Prüfen:")
    print("  sudo /usr/sbin/visudo -cf /etc/sudoers")
    print("  sudo systemctl status --no-pager e3dc-web-update.service")
    next_step = str(readiness.get("next_step") or "").strip()
    if next_step:
        print(f"Danach: {next_step}")


def fix_sudoers_permissions(issues, *, bound_privileged_preimages=None):
    """Erstellt oder korrigiert die Sudoers-Dateien für Web-Funktionen."""
    print("\n→ Richte Sudoers für Web-Funktionen ein…\n")
    if issues:
        if any(
            issue.get("external_direct_web") or issue.get("external_e3dc_systemctl")
            for issue in issues
            if isinstance(issue, dict)
        ):
            print(
                f"{RED}FEHLER{RESET} Fremde direkte Web-/E3DC-Freigaben werden aus Sicherheitsgründen "
                "weder automatisch verändert noch durch einen E3DC-Reparaturlauf überdeckt."
            )
            perm_logger.error(
                "Sudoers-Reparatur ohne Mutation abgebrochen: fremde direkte Web-/E3DC-Freigabe."
            )
            return False
        try:
            if __package__ in (None, ""):
                from Installer import web_installer
            else:
                from . import web_installer

            print("  -> Nutze zentrale Web-Launcher-Rechte-Reparatur (mit Backup/visudo).")
            result = web_installer.repair_permissions(
                repair_runtime=False,
                bound_privileged_preimages=bound_privileged_preimages,
            )
            for step in result.get("steps", []):
                status = GREEN + "OK" + RESET if step.get("ok") else RED + "FEHLER" + RESET
                label = step.get("step", "step")
                path = step.get("path") or step.get("backup") or ""
                print(f"  {status} {label} {path}")
            if result.get("success"):
                print(
                    f"{GREEN}OK{RESET} Sudoers-Reparatur abgeschlossen: "
                    "E3DC-WebUI nur noch über Wrapper; fremde Fragmente unverändert."
                )
                perm_logger.info("Sudoers ueber Web-Installer-Reparatur bereinigt.")
                return True
            _print_update_readiness_blockers(result.get("readiness"))
            print(f"{RED}FEHLER{RESET} Web-Installer-Reparatur meldet Fehler: {result.get('message', 'unbekannt')}")
            perm_logger.error("Web-Installer-Reparatur fehlgeschlagen: %s", result)
            return False
        except Exception as e:
            print(f"{RED}FEHLER{RESET} Web-Installer-Reparatur konnte nicht ausgefuehrt werden: {e}")
            perm_logger.error("Web-Installer-Reparatur konnte nicht ausgefuehrt werden: %s", e)
            return False

    success = True
    for issue in issues:
        if "content" in issue:
            path = issue["file"]
            content = issue["content"]
            print(f"  → Schreibe {path}…")
            try:
                # Sicherer Weg über temporäre Datei, um Shell-Probleme zu vermeiden
                with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".sudoers") as tmp:
                    tmp.write(content + "\n")
                    tmp_path = tmp.name
                
                # Kopieren und Rechte setzen
                run_command(f"sudo cp {tmp_path} {path}")
                run_command(f"sudo chown root:root {path}")
                run_command(f"sudo chmod 0440 {path}")
                os.unlink(tmp_path)

                print(f"{GREEN}✓{RESET} Sudoers-Datei erstellt/aktualisiert.")
                perm_logger.info(f"Sudoers-Datei erstellt/aktualisiert: {path}")
            except Exception as e:
                print(f"{RED}✗{RESET} Fehler: {e}")
                perm_logger.error(f"Fehler beim Erstellen der Sudoers-Datei {path}: {e}")
                success = False
    return success

def check_services():
    """Prueft ob V4-Kern-Dienste laufen (immer) und installierte optionale Dienste."""
    print("\n=== Service-Pruefung ===")
    perm_logger.info("--- Starte Service-Pruefung ---")
    issues = {}

    is_docker = os.path.exists(os.path.join(get_home_dir(INSTALL_USER), "e3dc-docker", "docker-compose.yml"))
    if is_docker:
        print(f"{GREEN}[OK]{RESET} Docker-Umgebung erkannt. Lokale Host-Dienste werden ignoriert.")
        perm_logger.info("Docker-Umgebung erkannt. Service-Pruefung uebersprungen.")
        return issues

    # HA Mode aus V4 JSON ermitteln
    ha_mode = "off"
    try:
        v4_path = "/var/www/html/data/e3dc_v4.json"
        if os.path.lexists(v4_path):
            v4_data = _read_release_config_nofollow(v4_path)
            ha_mode = str(v4_data.get("ha_mode", "off")).strip().lower()
    except Exception: pass

    # ------------------------------------------------------------------
    # Legacy C++ e3dc.service: muss bei V4 deaktiviert sein
    # ------------------------------------------------------------------
    is_v4_native = _is_v4_native_mode()

    legacy_service_present = _systemd_unit_exists(LEGACY_E3DC_SERVICE)
    screen_hanging = _legacy_screen_sessions_active()
    process_hanging = _legacy_e3dc_process_active()

    if is_v4_native or legacy_service_present or screen_hanging or process_hanging:
        res_active  = run_command("systemctl is-active e3dc")
        res_enabled = run_command("systemctl is-enabled e3dc")
        is_active   = res_active['stdout'].strip()  == "active"
        is_enabled  = res_enabled['stdout'].strip() == "enabled"
        if is_active or is_enabled or screen_hanging or process_hanging:
            print(f"{RED}[!]{RESET} Legacy C++ E3DC Dienst/Screen blockiert V4-Modus!")
            issues["e3dc_legacy_conflict"] = {
                "active": is_active,
                "enabled": is_enabled,
                "screen": screen_hanging,
                "process": process_hanging,
            }
        else:
            print(f"{GREEN}[OK]{RESET} Keine aktiven Legacy C++ E3DC Dienste oder Screen-Sessions.")

    # ------------------------------------------------------------------
    # KERN-DIENSTE: Muessen auf JEDEM V4-System laufen.
    # ------------------------------------------------------------------
    print("\n  [Kern-Dienste] (Pflicht auf jedem V4-System)")
    try:
        kern_services = [
            (module.service, module.display_name)
            for module in iter_modules(include_optional=False)
            if module.service and module.service != LEGACY_E3DC_SERVICE
        ]
    except Exception:
        kern_services = [
            ("e3dc-live",              "RSCP Live"),
            ("e3dc-epex-manager",      "EPEX/Strompreise"),
            ("e3dc-weather-manager",   "PV-Prognose und ML"),
            ("e3dc-storage-simulator", "SoC-Simulator"),
            ("e3dc-storage-manager",   "Speicher-Manager"),
            ("e3dc-notifier",          "Zeitplanung und Langzeit-Archiv"),
        ]
    for srv, label in kern_services:
        if not os.path.exists(f"/etc/systemd/system/{srv}.service"):
            print(f"{RED}[!]{RESET} {label} ({srv}): Service-Datei fehlt -> Installation / Update ausführen!")
            issues[srv] = {"active": False, "enabled": False}
            continue
        res_a = run_command(f"systemctl is-active {srv}")
        res_e = run_command(f"systemctl is-enabled {srv}")
        is_active  = res_a['stdout'].strip() == "active"
        is_enabled = res_e['stdout'].strip() in ("enabled", "static")
        if (
            (ha_mode == "slave" and srv in HA_MANAGED_SERVICES)
            or (ha_mode == "shadow" and srv in SHADOW_MANAGED_SERVICES)
        ):
            if is_active:
                print(f"{RED}[!]{RESET} {label}: laeuft, soll aber im {ha_mode.upper()}-Modus gestoppt sein!")
                issues[srv] = {"active": True, "enabled": is_enabled, "ha_slave_violation": True, "standby_mode": ha_mode.upper()}
            else:
                print(f"{GREEN}[OK]{RESET} {label}: korrekt gestoppt ({ha_mode.upper()}-Modus)")
            continue
        if is_active and is_enabled:
            print(f"{GREEN}[OK]{RESET} {label}: aktiv & enabled")
            perm_logger.info(f"Kern-Dienst '{srv}' OK.")
        else:
            details = []
            if not is_active:  details.append("nicht aktiv")
            if not is_enabled: details.append("nicht enabled")
            print(f"{RED}[!]{RESET} {label}: {', '.join(details)}")
            perm_logger.warning(f"Kern-Dienst '{srv}' Problem: {', '.join(details)}")
            issues[srv] = {"active": is_active, "enabled": is_enabled}

    # Piguard Watchdog
    if os.path.exists("/usr/local/bin/pi_guard.sh"):
        res_a = run_command("systemctl is-active piguard")
        is_active = res_a['stdout'].strip() == "active"
        if is_active:
            print(f"{GREEN}[OK]{RESET} Watchdog (piguard): aktiv")
        else:
            print(f"{RED}[!]{RESET} Watchdog (piguard): nicht aktiv")
            issues["piguard"] = {"active": False, "enabled": False}

    # ------------------------------------------------------------------
    # OPTIONALE DIENSTE: Nur pruefen wenn Service-Datei existiert
    # (= wurde ueber den Installer installiert)
    # ------------------------------------------------------------------
    print("\n  [Optionale Dienste] (nur wenn installiert)")
    optional_services = []
    core_service_names = {srv for srv, _label in kern_services}
    service_candidates = list(_catalog_service_names(include_legacy=True)) + ["mosquitto"]

    for svc in service_candidates:
        if svc == LEGACY_E3DC_SERVICE or svc in core_service_names:
            continue
        svc_path = f"/etc/systemd/system/{svc}.service"
        lib_path = f"/lib/systemd/system/{svc}.service"
        if os.path.exists(svc_path) or os.path.exists(lib_path):
            optional_services.append(svc)

    if not optional_services:
        print(f"  (keine optionalen Dienste installiert)")

    for srv in optional_services:
        res_a = run_command(f"systemctl is-active {srv}")
        res_e = run_command(f"systemctl is-enabled {srv}")
        is_active  = res_a['stdout'].strip() == "active"
        is_enabled = res_e['stdout'].strip() in ("enabled", "static")
        raw_status = res_a['stdout'].strip() or "unknown"

        if (
            (ha_mode == "slave" and srv in HA_MANAGED_SERVICES)
            or (ha_mode == "shadow" and srv in SHADOW_MANAGED_SERVICES)
        ):
            if is_active:
                print(f"{RED}[!]{RESET} {srv}: laeuft, soll aber im {ha_mode.upper()}-Modus gestoppt sein!")
                issues[srv] = {"active": True, "enabled": is_enabled, "ha_slave_violation": True, "standby_mode": ha_mode.upper()}
            else:
                print(f"{GREEN}[OK]{RESET} {srv}: korrekt gestoppt ({ha_mode.upper()}-Modus)")
            continue

        if is_active and is_enabled:
            print(f"{GREEN}[OK]{RESET} {srv}: aktiv & enabled")
            perm_logger.info(f"Optionaler Dienst '{srv}' OK.")
        else:
            details = []
            if not is_active:  details.append(f"nicht aktiv ({raw_status})")
            if not is_enabled: details.append("nicht enabled")
            print(f"{RED}[!]{RESET} {srv}: {', '.join(details)}")
            perm_logger.warning(f"Optionaler Dienst '{srv}': {', '.join(details)}")
            issues[srv] = {"active": is_active, "enabled": is_enabled}

    return issues


def fix_services(issues):
    """Korrigiert Service-Status."""
    print("\n-> Korrigiere Services...\n")
    success = True
    # Nur fuer Rechtekorrekturen vor einem Neustart; systemd selbst enthaelt
    # den vollstaendigen Pfad zum jeweiligen Skript.
    service_script_map = {
        "e3dc-bluelink":          "bluelink_client.py",
        "e3dc-mqtt-hub":          "e3dc_mqtt_hub.py",
        "energy_manager":         "energy_manager.py",
        "e3dc-lux-live":          "lux_live.py",
        "e3dc-stiebel-live":      "stiebel_live.py",
        "e3dc-dimplex-live":      "dimplex_live.py",
        "e3dc-notifier":          "notification_manager.py",
        "e3dc-ha":                "ha_manager.py",
        "e3dc-wallbox-manager":   "wallbox_manager.py",
        "e3dc-weather-manager":   "pv_forecast_service.py",
        "e3dc-epex-manager":      "epex_manager.py",
        "e3dc-storage-simulator": "storage_simulator.py",
        "e3dc-storage-manager":   "storage_manager.py",
        "e3dc-live":              "e3dc_live.py",
        "e3dc-heizstab":          "heizstab_manager.py",
        "e3dc-shadow-sync":       "shadow_sync.py",
    }
    for module in iter_modules():
        if not module.service or not module.script:
            continue
        service_script_map.setdefault(module.service, os.path.basename(module.script))
    # Erstelle eine Map von Dateiname zu Pfad fuer schnellen Zugriff
    file_path_map = {os.path.basename(d['path']): d['path'] for d in FILE_DEFINITIONS}


    for srv, data in issues.items():
        if srv == "e3dc_legacy_conflict":
            print("  → Legacy C++ E3DC Dienst/Screen pruefen und bereinigen...")
            _cleanup_legacy_e3dc()
            print(f"{GREEN}✓{RESET} Legacy C++ Altlasten bereinigt (kein Startversuch).")
            continue

        if data.get("ha_slave_violation"):
            print(f"  → Stoppe {srv} ({data.get('standby_mode', 'SLAVE')}-Modus)...")
            run_command(f"sudo systemctl stop {srv}")
            continue

        if srv == LEGACY_E3DC_SERVICE and _is_v4_native_mode():
            print(f"  → {srv} ist Legacy C++ und wird im V4-Modus nicht gestartet.")
            _cleanup_legacy_e3dc()
            continue

        # NEU: Vor dem Start die Rechte des Skripts sicherstellen
        if srv in service_script_map:
            script_basename = service_script_map[srv]
            if script_basename in file_path_map:
                script_path = file_path_map[script_basename]
                if os.path.exists(script_path) and not os.access(script_path, os.X_OK):
                    print(f"  → Setze Ausführungsrechte für {script_basename} vor dem Start...")
                    run_command(f"sudo chmod +x {script_path}")

        if not data.get("enabled"):
            print(f"  → Enable {srv}...")
            run_command(f"sudo systemctl enable {srv}")
        
        if not data.get("active"):
            print(f"  → Start {srv}...")
            run_command(f"sudo systemctl start {srv}")
            
        # Verify
        res = run_command(f"systemctl is-active {srv}")
        if res['stdout'].strip() == "active":
            print(f"{GREEN}✓{RESET} {srv} läuft nun.")
        else:
            print(f"{RED}✗{RESET} {srv} konnte nicht gestartet werden.")
            success = False
    return success

def check_legacy_autostart():
    """Prüft auf alte Autostart-Einträge in /etc/rc.local."""
    print("\n=== Legacy Autostart Prüfung ===\n")
    perm_logger.info("--- Starte Legacy Autostart Prüfung ---")
    issues = []
    rc_local = "/etc/rc.local"
    
    if os.path.exists(rc_local):
        try:
            with open(rc_local, "r") as f:
                for line in f:
                    if "E3DC.sh" in line and "screen" in line and not line.strip().startswith("#"):
                        print(f"{RED}✗{RESET} Alter Autostart in {rc_local} gefunden: {line.strip()}")
                        issues.append("rc_local_legacy")
                        break
        except Exception as e:
            print(f"⚠ Fehler beim Lesen von {rc_local}: {e}")
    
    if not issues:
        print(f"{GREEN}✓{RESET} Keine Legacy-Einträge in rc.local gefunden.")
    
    return issues

def fix_legacy_autostart(issues):
    """Entfernt Legacy-Einträge und bereinigt Prozesse."""
    print("\n→ Bereinige Legacy Autostart…\n")
    success = True
    
    if "rc_local_legacy" in issues:
        rc_local = "/etc/rc.local"
        try:
            with open(rc_local, "r") as f:
                lines = f.readlines()
            new_lines = []
            for line in lines:
                if "E3DC.sh" in line and "screen" in line and not line.strip().startswith("#"):
                    continue
                new_lines.append(line)
            with open(rc_local, "w") as f:
                f.writelines(new_lines)
            print(f"{GREEN}✓{RESET} {rc_local} bereinigt.")
            
            # Laufende Screen-Sessions killen (sowohl User als auch Root/Andere)
            print("  → Beende laufende E3DC Screen-Sessions (Cleanup)...")
            run_command(f"sudo -u {INSTALL_USER} screen -S E3DC -X quit")
            run_command("sudo screen -S E3DC -X quit")
            
            print("  → Legacy C++ bleibt gestoppt; V4-Dienste werden separat verwaltet.")
                
            perm_logger.info("Legacy Autostart entfernt.")
            
        except Exception as e:
            print(f"{RED}✗{RESET} Fehler: {e}")
            perm_logger.error(f"Fehler beim Fixen von Legacy Autostart: {e}")
            success = False
            
    return success

def _release_quiesced_from_current_process(*, required_uid=0, max_age_s=3600):
    """Erkennt ausschließlich das frische Update-Pausefenster dieses Prozesses."""
    try:
        descriptor, before = _open_regular_file_nofollow(WATCHDOG_UPDATE_PAUSE_FILE)
        try:
            if (
                before.st_uid != int(required_uid)
                or before.st_nlink != 1
                or before.st_size < 2
                or before.st_size > 4096
                or before.st_mode & stat.S_IWOTH
            ):
                return False
            payload_raw = os.read(descriptor, before.st_size + 1)
            after = os.fstat(descriptor)
            if (
                len(payload_raw) != before.st_size
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            ):
                return False
        finally:
            os.close(descriptor)
        payload = json.loads(payload_raw.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("active") is not True:
            return False
        if int(payload.get("pid")) != os.getpid():
            return False
        if str(payload.get("reason") or "") not in {
            "self-update",
            "release-rollback",
            "release-bootstrap",
        }:
            return False
        timestamp = float(payload.get("ts"))
        age_s = time.time() - timestamp
        return -5.0 <= age_s <= float(max_age_s)
    except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _release_configured_missing_optional_services(config):
    """Meldet konfigurierte, aber bislang nicht installierte Zusatzmodule."""
    return [
        service
        for service in configured_optional_services(config)
        if not _systemd_unit_exists(service)
    ]


def _read_release_config_nofollow(path="/var/www/html/data/e3dc_v4.json"):
    descriptor, before = _open_regular_file_nofollow(path)
    try:
        if (
            before.st_nlink != 1
            or before.st_size < 2
            or before.st_size > 4 * 1024 * 1024
        ):
            raise RuntimeError("Betriebskonfiguration besitzt eine unzulässige Größe")
        chunks = []
        remaining = before.st_size
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) != before.st_size
            or after.st_nlink != 1
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise RuntimeError("Betriebskonfiguration änderte sich während der Prüfung")
    finally:
        os.close(descriptor)
    config = json.loads(payload.decode("utf-8"))
    if not isinstance(config, dict):
        raise RuntimeError("Betriebskonfiguration ist kein JSON-Objekt")
    return config


LEGACY_532B_COMMIT = "4b19d7136bcd7c5906dcdd2d49903a2fd4645192"
LEGACY_532B_UPDATE_SOURCE_SHA256 = "e32ab215f8396381ce114b986792fd46eed5a9bf38448659d5f37df0862f49b6"
LEGACY_532B_HYBRID_POLICY_KEY = "legacy_hybrid_updates"
LEGACY_532B_SAFE_APT_PACKAGES = (
    "rsync",
    "php-cli",
    "php-sqlite3",
    "php-mbstring",
    "libapache2-mod-php",
    "mosquitto-clients",
    "python3-sklearn",
    "python3-numpy",
    "python3-cryptography",
    "python3-websockets",
)
RELEASE_CORE_SERVICES = (
    "e3dc-live",
    "e3dc-epex-manager",
    "e3dc-weather-manager",
    "e3dc-storage-simulator",
    "e3dc-storage-manager",
    "e3dc-websocket",
    "e3dc-notifier",
)
LEGACY_532B_BOUND_FUNCTIONS = (
    "_service_expected",
    "_validated_restart_services",
    "_restart_v4_services",
    "_post_update_healthcheck",
    "_recover_failed_transition",
    "run_initial_forecast",
    "update_e3dc",
)


def _release_git_read(repo_dir, *args, maximum=2 * 1024 * 1024):
    command = ["/usr/bin/git", "-C", str(repo_dir), *[str(item) for item in args]]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError("Git-Bindung des 5.3.2b-Ersthops fehlgeschlagen: " + detail[-500:])
    if len(completed.stdout) > maximum:
        raise RuntimeError("Git-Bindung des 5.3.2b-Ersthops ist unplausibel groß")
    return completed.stdout


def _top_level_function_codes(source, filename):
    module_code = compile(
        source.decode("utf-8"),
        filename,
        "exec",
        dont_inherit=True,
        optimize=sys.flags.optimize,
    )
    found = {}
    for constant in module_code.co_consts:
        if isinstance(constant, types.CodeType) and constant.co_name in LEGACY_532B_BOUND_FUNCTIONS:
            if constant.co_name in found:
                raise RuntimeError("5.3.2b-Quellbindung enthält eine doppelte Funktion")
            found[constant.co_name] = constant
    if tuple(sorted(found)) != tuple(sorted(LEGACY_532B_BOUND_FUNCTIONS)):
        raise RuntimeError("5.3.2b-Quellbindung ist unvollständig")
    return found


def _loaded_legacy_functions_match(update_module, source):
    update_entry = getattr(update_module, "update_e3dc", None)
    update_code = getattr(update_entry, "__code__", None)
    if not isinstance(update_code, types.CodeType):
        return False
    expected = _top_level_function_codes(source, update_code.co_filename)
    for name in LEGACY_532B_BOUND_FUNCTIONS:
        actual = getattr(getattr(update_module, name, None), "__code__", None)
        if not isinstance(actual, types.CodeType):
            return False
        if actual != expected[name]:
            return False
    return True


def _stack_contains_code(code):
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame is not None else None
        while frame is not None:
            if frame.f_code is code:
                return True
            frame = frame.f_back
    finally:
        del frame
    return False


def _legacy_532b_update_context(update_entry):
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame is not None else None
        for _ in range(32):
            if frame is None:
                break
            if frame.f_code is getattr(update_entry, "__code__", None):
                return dict(frame.f_locals)
            frame = frame.f_back
    finally:
        del frame
    raise RuntimeError("5.3.2b-Updatekontext ist im laufenden Prozess nicht gebunden")


def _legacy_532b_target_policy_supported(target_policy, target_version):
    """Bindet künftige Zielreleases nur über einen expliziten Altpfadvertrag."""

    if not isinstance(target_policy, dict):
        return False
    contracts = target_policy.get(LEGACY_532B_HYBRID_POLICY_KEY)
    if not isinstance(contracts, list) or len(contracts) != 1:
        return False
    contract = contracts[0]
    expected_contract = {
        "source_version": "5.3.2b",
        "source_commit": LEGACY_532B_COMMIT,
        "source_update_sha256": LEGACY_532B_UPDATE_SOURCE_SHA256,
        "entrypoint": "--update-e3dc",
        "requires_existing_venv": True,
        "bare_metal_supported": True,
    }
    if not isinstance(contract, dict) or contract != expected_contract:
        return False

    version = str(target_version or "").strip().lstrip("v")
    policy_version = str(target_policy.get("version") or "").strip().lstrip("v")
    stable_release = str(target_policy.get("stable_release") or "").strip()
    history = target_policy.get("history_transition")
    required_history = {
        "strategy": "installer_bootstrap",
        "manual_git_pull_supported": False,
        "requires_verified_external_backup": True,
        "requires_exact_target_sha": True,
        "requires_safe_actor_state": True,
        "requires_boot_service_http_ha_health_validation": True,
    }
    return bool(
        version
        and policy_version == version
        and stable_release == f"v{version}"
        and target_policy.get("restart_service_contract") == "core_plus_preinstalled_v1"
        and tuple(target_policy.get("restart_services") or ()) == RELEASE_CORE_SERVICES
        and tuple(target_policy.get("apt_packages") or ()) == LEGACY_532B_SAFE_APT_PACKAGES
        and target_policy.get("pip_packages") == []
        and target_policy.get("venv_pip_packages") == []
        and target_policy.get("run_permissions") is True
        and history == required_history
    )


def _install_legacy_532b_service_contract():
    """Begrenzt den bekannten 5.3.2b-Ersthop und erhält dessen Recovery unverändert."""

    update_module = sys.modules.get("Installer.update")
    if update_module is None and __package__:
        update_module = sys.modules.get(f"{__package__}.update")
    if update_module is None or callable(
        getattr(update_module, "finalize_release_from_target", None)
    ):
        return False

    package_module = sys.modules.get("Installer")
    # Dieser Binder repariert ausschließlich den veröffentlichten
    # 5.3.2b-Ersthop. Andere Alt-Updater wie 5.4.0a besitzen bereits ihren
    # eigenen eingefrorenen Releasevertrag und dürfen nicht erst nach dem
    # Git-Wechsel gegen die 5.3.2b-Quellbindung geprüft werden.
    source_version = str(
        getattr(package_module, "__version__", "") or ""
    ).strip()
    if source_version == "5.4.0a":
        return False
    if source_version != "5.3.2b":
        raise RuntimeError(
            "Laufender Alt-Updater besitzt keine freigegebene Quellgeneration"
        )

    service_expected = getattr(update_module, "_service_expected", None)
    validated_services = getattr(update_module, "_validated_restart_services", None)
    restart_services_function = getattr(update_module, "_restart_v4_services", None)
    healthcheck_function = getattr(update_module, "_post_update_healthcheck", None)
    recover_transition = getattr(update_module, "_recover_failed_transition", None)
    initial_forecast = getattr(update_module, "run_initial_forecast", None)
    update_entry = getattr(update_module, "update_e3dc", None)
    expected_core = tuple(getattr(update_module, "INSTALL_CENTER_CORE_SERVICES", ()))
    context = _legacy_532b_update_context(update_entry)
    state = context.get("state")
    policy = context.get("policy")
    restart_services = context.get("restart_services")
    old_commit = str(context.get("old_commit") or "").strip().lower()
    target_commit = str(context.get("target_commit") or "").strip().lower()
    repo_dir = str(context.get("repo_dir") or "")
    install_user = str(context.get("install_user") or "")
    old_source = _release_git_read(
        repo_dir,
        "show",
        f"{LEGACY_532B_COMMIT}:Installer/update.py",
    )
    current_head = _release_git_read(
        repo_dir,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        maximum=256,
    ).decode("ascii", errors="strict").strip().lower()
    target_policy_raw = _release_git_read(
        repo_dir,
        "show",
        f"{target_commit}:UPDATE_POLICY.json",
    )
    target_version = _release_git_read(
        repo_dir,
        "show",
        f"{target_commit}:VERSION",
        maximum=256,
    ).decode("utf-8", errors="strict").strip()
    remote_url = _release_git_read(
        repo_dir,
        "remote",
        "get-url",
        "origin",
        maximum=1024,
    ).decode("utf-8", errors="strict").strip()
    try:
        target_policy = json.loads(target_policy_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Zielpolicy des 5.3.2b-Ersthops ist ungültig") from exc
    signature_ok = (
        getattr(update_module, "__name__", "") == "Installer.update"
        and source_version == "5.3.2b"
        and callable(service_expected)
        and callable(validated_services)
        and callable(restart_services_function)
        and callable(healthcheck_function)
        and callable(recover_transition)
        and callable(initial_forecast)
        and callable(update_entry)
        and expected_core == RELEASE_CORE_SERVICES
        and old_commit == LEGACY_532B_COMMIT
        and len(target_commit) == 40
        and all(character in "0123456789abcdef" for character in target_commit)
        and isinstance(policy, dict)
        and policy.get("restart_service_contract") == "core_plus_preinstalled_v1"
        and tuple(policy.get("restart_services") or ()) == RELEASE_CORE_SERVICES
        and state is not None
        and isinstance(getattr(state, "config", None), dict)
        and list(restart_services or ()) == list(validated_services(policy, state))
        and context.get("headless") is True
        and context.get("target_ref") is None
        and context.get("target_install_path") is None
        and context.get("transition_name") == "self-update"
        and context.get("mutated") is True
        and "--update-e3dc" in sys.argv
        and os.path.abspath(repo_dir) == os.path.abspath(INSTALL_ROOT)
        and os.path.realpath(repo_dir) == os.path.abspath(repo_dir)
        and install_user == INSTALL_USER
        and current_head == target_commit
        and hashlib.sha256(old_source).hexdigest() == LEGACY_532B_UPDATE_SOURCE_SHA256
        and _loaded_legacy_functions_match(update_module, old_source)
        and target_policy == policy
        and _legacy_532b_target_policy_supported(target_policy, target_version)
        and remote_url == getattr(update_module, "SELFUPDATE_REPO", None)
    )
    if not signature_ok:
        raise RuntimeError("Laufender Alt-Updater besitzt nicht den gebundenen 5.3.2b-Vertrag")

    def bounded_service_expected(service, transition_state):
        name = str(service).removesuffix(".service")
        if name == "e3dc":
            return False, "Legacy-C++-Dienst bleibt dauerhaft deaktiviert"
        if name in update_module._ha_slave_standby_services(transition_state):
            return False, f"durch Rolle {transition_state.ha_role} explizit Standby"
        if name in RELEASE_CORE_SERVICES:
            return True, "Pflichtdienst des Install-Centers"
        unit = update_module._unit_name(name)
        module = update_module.get_module_by_service(unit)
        if module is None:
            if name == "piguard":
                return (
                    unit in transition_state.preinstalled_units,
                    "Watchdog war vor dem Wechsel nicht installiert",
                )
            raise RuntimeError(f"Dienst fehlt im Service-Katalog: {unit}")
        if not module.optional:
            return True, "Pflichtdienst"
        if name == "e3dc-ha":
            return (
                transition_state.ha_role in {"master", "slave"},
                f"HA-Rolle ist {transition_state.ha_role}",
            )
        if name == "e3dc-shadow-sync":
            return (
                transition_state.ha_role == "shadow",
                f"HA-/Shadow-Rolle ist {transition_state.ha_role}",
            )
        expected_when_installed = preinstalled_optional_service_expected(
            name,
            transition_state.config,
        )
        if not expected_when_installed:
            return False, "Feature ist in eingefrorener Konfiguration explizit deaktiviert"
        if unit not in transition_state.preinstalled_units:
            return False, "optionaler Dienst war vor dem Wechsel nicht installiert"
        return True, "vor dem Wechsel installiert und nicht explizit deaktiviert"

    forward_names = tuple(str(item).removesuffix(".service") for item in restart_services)
    recovery_names = frozenset(
        str(unit).removesuffix(".service")
        for unit in state.preinstalled_units
        if str(unit) != "e3dc.service"
    )

    def bound_call(function, args, kwargs):
        signature = inspect.signature(function)
        call = signature.bind_partial(*args, **kwargs)
        call.apply_defaults()
        if call.arguments.get("transition_state") is not state:
            raise RuntimeError("5.3.2b-Dienstübergang verlor die eingefrorene Generation")
        names = tuple(
            str(item).removesuffix(".service")
            for item in (call.arguments.get("services") or ())
        )
        if names != forward_names and frozenset(names) != recovery_names:
            raise RuntimeError("5.3.2b-Dienstübergang besitzt eine fremde Dienstmenge")
        if not _stack_contains_code(update_entry.__code__):
            raise RuntimeError("5.3.2b-Dienstübergang liegt außerhalb des gebundenen Updates")
        update_module._service_expected = bounded_service_expected
        try:
            return function(*args, **kwargs)
        finally:
            if update_module._service_expected is not bounded_service_expected:
                raise RuntimeError("5.3.2b-Dienstvertrag wurde während des Aufrufs verändert")
            update_module._service_expected = service_expected

    def cleanup():
        replacements = (
            ("_restart_v4_services", restart_with_bounded_service_contract, restart_services_function),
            ("_post_update_healthcheck", healthcheck_with_bounded_service_contract, healthcheck_function),
            ("_recover_failed_transition", recover_with_cleanup, recover_transition),
            ("run_initial_forecast", forecast_with_cleanup, initial_forecast),
        )
        for name, wrapper, original in replacements:
            if getattr(update_module, name, None) is wrapper:
                setattr(update_module, name, original)
        if getattr(update_module, "_service_expected", None) is bounded_service_expected:
            update_module._service_expected = service_expected

    def restart_with_bounded_service_contract(*args, **kwargs):
        return bound_call(restart_services_function, args, kwargs)

    def healthcheck_with_bounded_service_contract(*args, **kwargs):
        return bound_call(healthcheck_function, args, kwargs)

    def recover_with_cleanup(*args, **kwargs):
        cleanup()
        return recover_transition(*args, **kwargs)

    def forecast_with_cleanup(*args, **kwargs):
        result = initial_forecast(*args, **kwargs)
        cleanup()
        return result

    update_module._restart_v4_services = restart_with_bounded_service_contract
    update_module._post_update_healthcheck = healthcheck_with_bounded_service_contract
    update_module._recover_failed_transition = recover_with_cleanup
    update_module.run_initial_forecast = forecast_with_cleanup
    return True


def run_permissions_wizard(
    headless=False,
    release_quiesced=None,
    bound_privileged_preimages=None,
    program_files=None,
    program_directories=None,
):
    """Hauptlogik für Rechteprüfung und -korrektur."""
    if release_quiesced is None:
        # Kompatibilität für alte, bereits veröffentlichte Updater: Nach deren
        # git reset lädt der laufende Prozess diese neue Funktion, übergibt den
        # Parameter aber noch nicht. Nur das eigene frische Pausefenster darf
        # den service-neutralen Release-Modus aktivieren.
        release_quiesced = _release_quiesced_from_current_process()
    else:
        release_quiesced = bool(release_quiesced)

    writer_contract = storage_manager_writer_contract(
        require_canonical_unit=not release_quiesced,
    )
    if not writer_contract.get("ok"):
        blockers = ", ".join(writer_contract.get("blockers") or ["unbekannt"])
        print(
            f"{RED}[!]{RESET} Storage-Single-Writer-Preflight blockiert: "
            f"{blockers}"
        )
        perm_logger.error(
            "Storage-Single-Writer-Preflight blockiert: %s",
            blockers,
        )
        return False
    
    # Die Release-Transaktion hat die Betriebskonfiguration bereits eingefroren.
    # Eine Migration in diesem Fenster würde den Rückfallvertrag des alten wie
    # des neuen Updaters verletzen. Reguläre Rechteprüfungen migrieren weiter.
    if release_quiesced:
        config_ready = True
        print("→ Betriebskonfiguration bleibt im Release-Fenster unverändert.")
    else:
        config_ready = run_config_wizard()
    if config_ready is not True:
        print("⚠ Konfigurationsmigration fehlgeschlagen; Rechte- und Servicekorrekturen werden nicht gestartet.")
        perm_logger.error("Konfigurationsmigration fehlgeschlagen; Permissions-Wizard bricht fail-closed ab.")
        return False
    configured_missing_services = []
    release_ha_mode = "off"
    legacy_532b_contract_bound = False
    runtime_config = {}
    if release_quiesced:
        try:
            legacy_532b_contract_bound = _install_legacy_532b_service_contract()
            release_config = _read_release_config_nofollow()
            runtime_config = release_config
            release_ha_mode = str(release_config.get("ha_mode") or "off").strip().lower()
            configured_missing_services = _release_configured_missing_optional_services(release_config)
        except Exception as exc:
            print(f"{RED}[!]{RESET} Konfigurierte Zusatzmodule konnten nicht sicher geprüft werden: {exc}")
            perm_logger.error("Release-Zusatzmodulprüfung fehlgeschlagen: %s", exc)
            return False
        if configured_missing_services:
            role_note = (
                f" in der gebundenen Rolle {release_ha_mode}"
                if release_ha_mode in {"slave", "shadow"}
                else ""
            )
            print(
                f"{YELLOW}[i]{RESET} Konfigurierte, bislang nicht installierte "
                f"Zusatzdienste werden{role_note} nicht automatisch aktiviert:"
            )
            for service in configured_missing_services:
                hint = (
                    "vor einem Rollenwechsel bewusst über das Install-Center prüfen"
                    if release_ha_mode in {"slave", "shadow"}
                    else "bei Bedarf bewusst über das Install-Center installieren"
                )
                print(f"    - {service} ({hint})")
            perm_logger.warning(
                "Release erhält den Vorzustand: konfigurierte Zusatzdienste ohne "
                "vorinstallierte Unit: %s",
                ", ".join(configured_missing_services),
            )
    else:
        try:
            runtime_config = _read_release_config_nofollow()
        except Exception as exc:
            # Die Diagnose ist standardmäßig aus. Eine fehlende optionale
            # Diagnosekonfiguration darf eine reguläre Installation deshalb
            # nicht blockieren; der Sidecar bleibt fail-closed gestoppt.
            perm_logger.warning(
                "PV-Prognosediagnose-Konfiguration nicht sicher lesbar; "
                "optional deaktiviert behandelt: %s",
                exc,
            )
            runtime_config = {}
    forecast_evidence_required = str(
        runtime_config.get("forecast_diagnostics_enable", "0")
    ).strip().lower() in {"1", "true", "yes", "on", "ein"}
    if __package__ in (None, ""):
        from Installer.apache_security import ensure_apache_runtime_path_protection
    else:
        from .apache_security import ensure_apache_runtime_path_protection
    apache_runtime_protected = ensure_apache_runtime_path_protection(
        run_command,
        reload_apache=not release_quiesced,
        allow_mutation=not release_quiesced,
    )
    if not apache_runtime_protected:
        print(
            f"{RED}[!]{RESET} Apache schützt Daten-, Log-, Ramdisk- und "
            "Temp-Pfade nicht sicher vor direktem HTTP-Zugriff."
        )
        perm_logger.error("Apache-Laufzeitpfadschutz fehlt oder konnte nicht aktiviert werden.")
    ml_store_ready = ensure_private_ml_model_store()
    forecast_evidence_store_ready = ensure_private_forecast_evidence_store()
    if not forecast_evidence_store_ready and not forecast_evidence_required:
        print(
            f"{YELLOW}[i]{RESET} Privater Prognosediagnose-Pfad ist nicht "
            "vorbereitet; die ausgeschaltete optionale Diagnose bleibt gestoppt."
        )
        perm_logger.warning(
            "Optionale PV-Prognosediagnose bleibt ohne privaten Zustandspfad "
            "deaktiviert; Installation wird nicht blockiert."
        )
        forecast_evidence_store_ready = True

    # Als erstes: Bereinige root-eigene Dateien falls vorhanden
    cleanup_success = cleanup_root_owned_files(
        program_files=program_files,
        program_directories=program_directories,
    )
    if not cleanup_success:
        print(f"{RED}[!]{RESET} Webrechte-Reparatur wurde sicher abgebrochen.")
        log_warning("permissions", "Webrechte-Reparatur wurde fail-closed beendet")
        return False

    if cleanup_legacy_plots() is not True:
        return False
    
    # Nur lesen: Erst die persistente Notifier-/Watchdog-Transaktion darf
    # Cron-Vorzustände verändern und bei Stromausfall wiederherstellen.
    if cleanup_legacy_cronjobs() is not True:
        return False

    issues = check_permissions()
    traversal_issues = tuple(
        issue
        for issue in issues
        if issue == "home" or str(issue).startswith("web_traversal:")
    )
    if release_quiesced and traversal_issues:
        print(
            f"{RED}[!]{RESET} Installationspfad-Traversierrechte drifteten nach "
            "der eigenständigen Update-Vorstufe; im Release-Fenster werden sie "
            "nicht erneut verändert."
        )
        perm_logger.error(
            "Release-Fenster sah abweichende Traversierrechte: %s",
            ", ".join(str(item) for item in traversal_issues),
        )
        return False
    wp_issues = check_webportal_permissions(include_service_checks=not release_quiesced)
    ramdisk_issue_keys = {
        "ramdisk_missing",
        "ramdisk_unsafe",
        "ramdisk_owner",
        "ramdisk_mode",
        "ramdisk_not_mounted",
    }
    if not release_quiesced and not ramdisk_issue_keys.intersection(wp_issues):
        # Nur ein bereits exakt bestätigter tmpfs-Vertrag darf die obsolete
        # Unit unabhängig entfernen. Muss die RAM-Disk erst repariert werden,
        # übernimmt setup_ramdisk() Prestate, Commit und Rollback gemeinsam.
        from .ramdisk import remove_legacy_grabber_unit_transactionally

        if remove_legacy_grabber_unit_transactionally() is not True:
            print(
                f"{RED}[!]{RESET} Veralteter Live-Grabber konnte nicht "
                "transaktional entfernt werden."
            )
            perm_logger.error(
                "Legacy-Grabber-Unit konnte nicht transaktional entfernt werden."
            )
            return False
    file_issues = check_file_permissions()
    sudo_issues = check_wrapper_integrity() + check_sudoers_permissions()
    service_issues = [] if release_quiesced else check_services()
    legacy_issues = [] if release_quiesced else check_legacy_autostart()
    watchdog_installed = os.path.exists(PI_GUARD_PATH) or os.path.exists(PIGUARD_SERVICE)
    watchdog_refreshed = True if release_quiesced else refresh_watchdog_guard_script()
    web_program_hardened = harden_web_program_permissions(
        program_files=program_files,
        program_directories=program_directories,
    )

    has_issues = bool(issues) or bool(wp_issues) or bool(file_issues) or bool(sudo_issues) or bool(service_issues) or bool(legacy_issues) or not watchdog_refreshed or not web_program_hardened or not apache_runtime_protected or not ml_store_ready or not forecast_evidence_store_ready
    if not has_issues:
        if release_quiesced:
            print(f"\n{GREEN}✓{RESET} Service-neutrale Release-Berechtigungsprüfung bestanden.\n")
            perm_logger.info("✓ Service-neutrale Release-Berechtigungsprüfung bestanden.")
            details = "Release-Berechtigungen OK; Dienste absichtlich nicht geprüft"
        else:
            print(f"\n{GREEN}✓{RESET} Alle Berechtigungen und Services sind korrekt.\n")
            perm_logger.info("✓ Prüfung bestanden: Keine Probleme gefunden.")
            details = "Alle Checks OK"
        if watchdog_installed and watchdog_refreshed:
            details += ", Watchdog aktualisiert" if not release_quiesced else ", Watchdog unverändert"
        if configured_missing_services:
            details += ", konfigurierte Zusatzdienste bewusst unverändert"
        if legacy_532b_contract_bound:
            details += ", 5.3.2b-Ersthop recovery-sicher gebunden"
        log_task_completed("Rechte prüfen & korrigieren", details=details)
        return True

    print("\n⚠ Probleme gefunden.")
    perm_logger.warning(f"⚠ Probleme erkannt: {len(issues)} Verz., {len(wp_issues)} Web, {len(file_issues)} Dateien, {len(sudo_issues)} Sudoers, {len(service_issues)} Services, {len(legacy_issues)} Legacy")
    
    print("→ Automatische Rechte-Korrektur...")

    all_success = bool(watchdog_refreshed) and bool(apache_runtime_protected) and bool(ml_store_ready) and bool(forecast_evidence_store_ready)
    if issues:
        success = fix_permissions(issues)
        all_success = all_success and success
    if wp_issues:
        success = fix_webportal_permissions(wp_issues)
        all_success = all_success and success
    if file_issues:
        success = fix_file_permissions(file_issues)
        all_success = all_success and success
    if sudo_issues:
        success = fix_sudoers_permissions(
            sudo_issues,
            bound_privileged_preimages=bound_privileged_preimages,
        )
        all_success = all_success and success
    if service_issues:
        success = fix_services(service_issues)
        all_success = all_success and success
    if legacy_issues:
        success = fix_legacy_autostart(legacy_issues)
        all_success = all_success and success
    all_success = harden_web_program_permissions(
        program_files=program_files,
        program_directories=program_directories,
    ) and all_success

    if all_success:
        print(f"\n{GREEN}✓{RESET} Alle Probleme korrigiert.\n")
        perm_logger.info("✓ Alle Korrekturen erfolgreich durchgeführt.")
        log_task_completed("Rechte prüfen & korrigieren", details="Alle Probleme behoben")
        return True
    else:
        print("\n⚠ Einige Probleme konnten nicht korrigiert werden.\n")
        perm_logger.error("⚠ Einige Probleme konnten nicht automatisch korrigiert werden - manuelle Intervention notwendig.")
        log_error("permissions", "Einige Probleme konnten nicht automatisch korrigiert werden")
        return False


register_command("24", "Rechte prüfen & korrigieren", run_permissions_wizard, sort_order=24, category=CAT_ENV)


if __name__ == "__main__":
    sys.exit(0 if run_permissions_wizard(headless=True) is not False else 1)
