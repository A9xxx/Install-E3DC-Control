import os
import sys
import json
import subprocess
import shutil
import time
import io
import shlex
import atexit
import re
import urllib.error
import urllib.request
import hashlib
import pwd
import stat
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
from .backup import backup_current_version, restore_verified_backup
from .backup_integrity import _open_directory_nofollow, _open_regular_file_nofollow
from .utils import run_command, cleanup_pycache
from .installer_config import (
    ensure_web_config,
    get_install_path,
    get_install_user,
    get_venv_path,
    load_config,
)
from .transition_context import (
    venv_directory_chain_is_trusted,
    venv_metadata_is_trusted,
)
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning
from .config_secret_permissions import config_secret_dir_mode_text, config_secret_file_mode_text
try:
    from .service_catalog import allowed_services, get_module_by_service
except ImportError:  # pragma: no cover - direct script execution fallback
    from service_catalog import allowed_services, get_module_by_service

INSTALL_PATH   = get_install_path()
INSTALLER_DIR  = os.path.dirname(os.path.abspath(__file__))
UPDATE_POLICY  = os.path.join(INSTALL_PATH, 'UPDATE_POLICY.json')
update_logger  = get_or_create_logger('update')

# Self-Update: Unser Repo (Native Python + PHP)
SELFUPDATE_REPO = 'https://github.com/A9xxx/Install-E3DC-Control.git'
WATCHDOG_PAUSE_FILE = '/var/www/html/ramdisk/watchdog.update_pause'
WATCHDOG_GRACE_FILE = '/var/www/html/ramdisk/watchdog.update_grace'
WATCHDOG_POST_UPDATE_GRACE_S = 300
_watchdog_pause_registered = False

FULL_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
PACKAGE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\Z")
LOCAL_HEALTH_URLS = (
    "http://127.0.0.1/index.php",
    "http://127.0.0.1/help.php",
)

# Stale paths may only be removed when both the release policy and this code
# explicitly name the exact absolute target. Directories need a separate list.
APPROVED_STALE_DELETE_FILES = frozenset({
    "/var/www/html/luxtronik.php",
    "/var/www/html/tmp/luxtronik.php",
    "/var/www/html/test.php",
    "/var/www/html/test_keys.php",
    "/var/www/html/test_real.php",
    "/var/www/html/test_diff.php",
    "/var/www/html/test_merge.php",
    "/var/www/html/reorder.py",
    "/var/www/html/ramdisk/bluelink_debug.json",
    "/var/www/html/data/morning_boost_state.json",
    "/var/www/html/assets/vendor/ASSET_PROVENANCE.json",
})
APPROVED_STALE_DELETE_DIRS = frozenset({"/var/www/html/app"})

# Package changes are release code, not arbitrary policy input.  A verified
# policy may select from these reviewed sets, but it cannot inject options,
# shell fragments or new package sources.
APPROVED_APT_PACKAGES = frozenset({
    "php-sqlite3", "php-mbstring", "libapache2-mod-php", "mosquitto-clients",
    "python3-sklearn", "python3-numpy", "python3-cryptography",
    "python3-websockets", "nodejs", "npm", "avahi-daemon", "avahi-utils",
    "dbus", "rsync",
})
# Alte signierte Policies führen diese optionalen Pakete noch im Core-Block.
# Sie bleiben prüfbar, werden aber ausschließlich vom Matter-Installer gesetzt.
MATTER_ONLY_APT_PACKAGES = frozenset({
    "nodejs", "npm", "avahi-daemon", "avahi-utils", "dbus",
})
APPROVED_PIP_PACKAGES = frozenset({
    "paho-mqtt", "requests", "hyundai_kia_connect_api", "websocket-client",
    "websockets", "pymodbus", "pywebpush", "pycryptodome",
})
MANAGED_VENV_APT_POLICY_KEY = "managed_venv_apt_packages"
APPROVED_MANAGED_VENV_APT_PACKAGES = frozenset({"python3-venv"})
MANAGED_VENV_PIP_POLICY_KEY = "managed_venv_pip_packages"
VENV_PIP_POLICY_KEY = "venv_pip_packages"
LEGACY_PIP_POLICY_KEY = "pip_packages"
VALID_HA_ROLES = frozenset({"off", "master", "slave", "shadow"})
HA_CONFIG_PATH = "/var/www/html/data/e3dc_v4.json"
TARGET_FINALIZER_SUCCESS = "E3DC_RELEASE_TARGET_FINALIZER_OK"
TARGET_FINALIZER_RELATIVE_FILES = (
    "Installer/__init__.py",
    "Installer/release_finalize.py",
    "Installer/update.py",
)


def _is_docker_environment() -> bool:
    """Erkennt offizielle Images über Docker-Marker oder expliziten Modus."""
    if Path("/.dockerenv").is_file():
        return True
    explicit = str(os.environ.get("E3DC_CONTAINER_MODE") or "").strip().lower()
    return explicit in {"1", "true", "yes", "docker"}


@dataclass(frozen=True)
class TransitionState:
    """Immutable pre-transition role, feature and service inventory."""

    ha_role: str
    config: dict
    config_sha256: str
    config_path: str
    preinstalled_units: frozenset[str]
    bootstrap_legacy_config: bool = False
    legacy_e3dc_activity: str = "absent"


@dataclass(frozen=True)
class RecoverySurfaceInventory:
    web_program_entries: frozenset[str]
    watchdog_files: frozenset[str]
    unit_enablement: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PackageTransactionState:
    apt_before: frozenset[str]
    pip_before: tuple[tuple[str, str], ...]
    venv_python: str | None
    install_user: str
    apt_requested: tuple[str, ...]
    pip_requested: tuple[str, ...]
    venv_path: str | None = None
    venv_existed: bool = True


def _transition_units_sha256(units) -> str:
    """Bindet die vollständige, sortierte Unit-Ausgangsmenge ohne Pfadannahmen."""

    payload = "\n".join(sorted(str(unit) for unit in units)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _run_argv(argv, *, timeout: int = 30, env=None) -> dict:
    """Run a fixed argv vector without a shell and return run_command shape."""
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("argv muss eine nicht-leere Liste sein")
    args = [str(item) for item in argv]
    if any("\x00" in item for item in args):
        raise ValueError("NUL-Byte in argv")
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"success": False, "stdout": "", "stderr": str(exc), "returncode": -1}
    return {
        "success": completed.returncode == 0,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
        "returncode": completed.returncode,
    }


def _combined_process_diagnostics(result: dict, maximum: int = 4000) -> str:
    """Bewahrt stdout und stderr eines fehlgeschlagenen Kindprozesses gemeinsam."""
    stdout = str(result.get("stdout") or "").strip()
    stderr = str(result.get("stderr") or "").strip()
    streams = sum(bool(value) for value in (stdout, stderr))
    if streams == 0:
        return "kein Fehlertext"
    per_stream = max(256, int(maximum) // streams - 16)
    sections = []
    if stdout:
        sections.append("stdout:\n" + stdout[-per_stream:])
    if stderr:
        sections.append("stderr:\n" + stderr[-per_stream:])
    return "\n".join(sections)


def _git_argv(repo_dir: str, install_user: str, *args: str, timeout: int = 30) -> dict:
    return _run_argv(
        ["sudo", "-H", "-u", str(install_user), "git", "-C", str(repo_dir), *args],
        timeout=timeout,
    )


def _set_watchdog_update_grace(reason: str = 'update') -> None:
    """Gibt piguard nach Update-Restarts Zeit fuer frische State-Dateien."""
    try:
        ramdisk_dir = os.path.dirname(WATCHDOG_GRACE_FILE)
        os.makedirs(ramdisk_dir, exist_ok=True)
        payload = {
            'active': True,
            'reason': reason,
            'ts': int(time.time()),
            'grace_s': WATCHDOG_POST_UPDATE_GRACE_S,
            'pid': os.getpid(),
        }
        with open(WATCHDOG_GRACE_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        os.chmod(WATCHDOG_GRACE_FILE, 0o664)
    except Exception:
        pass


def _set_watchdog_update_pause(active: bool, reason: str = 'update') -> None:
    """Signalisiert piguard ein bewusstes Update-/Neustartfenster."""
    try:
        ramdisk_dir = os.path.dirname(WATCHDOG_PAUSE_FILE)
        os.makedirs(ramdisk_dir, exist_ok=True)
        if active:
            payload = {
                'active': True,
                'reason': reason,
                'ts': int(time.time()),
                'pid': os.getpid(),
            }
            with open(WATCHDOG_PAUSE_FILE, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
            os.chmod(WATCHDOG_PAUSE_FILE, 0o664)
        else:
            try:
                os.remove(WATCHDOG_PAUSE_FILE)
            except FileNotFoundError:
                pass
            _set_watchdog_update_grace(reason)
    except Exception:
        pass


def _clear_watchdog_update_pause() -> None:
    _set_watchdog_update_pause(False)


def _enable_watchdog_update_pause(reason: str = 'update') -> None:
    global _watchdog_pause_registered
    _set_watchdog_update_pause(True, reason=reason)
    if not _watchdog_pause_registered:
        atexit.register(_clear_watchdog_update_pause)
        _watchdog_pause_registered = True


def repair_legacy_paths_file() -> bool:
    """Synchronise the compatibility mirror from the stable local owner."""
    try:
        install_user = get_install_user()
        if install_user and install_user != "www-data" and ensure_web_config(install_user):
            print("  [OK] Pfadmetadaten aus lokalem Installationskontext synchronisiert.")
            return True
    except Exception:
        pass
    print("  [!] Pfadmetadaten wurden wegen ungültigem Installationskontext nicht verändert.")
    return False


def migrate_storage_manager_next_override() -> bool:
    """Migrate old test overrides from storage_manager_next.py to canonical storage_manager.py."""
    override_file = "/etc/systemd/system/e3dc-storage-manager.service.d/override.conf"
    try:
        if not os.path.exists(override_file):
            return True
        with open(override_file, "r", encoding="utf-8") as f:
            source = f.read()
        if "storage_manager_next.py" not in source:
            return True
        updated = source.replace("storage_manager_next.py", "storage_manager.py")
        backup_file = override_file + ".before-v5-canonical"
        try:
            shutil.copy2(override_file, backup_file)
        except Exception:
            pass
        with open(override_file, "w", encoding="utf-8") as f:
            f.write(updated)
        run_command("sudo systemctl daemon-reload", timeout=15)
        print("  [OK] Alter Storage-Manager-Next-Override auf storage_manager.py migriert.")
        return True
    except Exception as exc:
        print(f"  [!] Storage-Manager-Override konnte nicht migriert werden: {exc}")
        return False


CATALOG_FALLBACK_SERVICES = (
    'e3dc-live',
    'e3dc-storage-manager',
    'e3dc-storage-simulator',
    'e3dc-epex-manager',
    'e3dc-weather-manager',
    'e3dc-wallbox-manager',
    'energy_manager',
    'e3dc-lux-live',
    'e3dc-idm-live',
    'e3dc-stiebel-live',
    'e3dc-dimplex-live',
    'e3dc-heizstab',
    'e3dc-climate-live',
    'e3dc-climate-control',
    'e3dc-ha',
    'e3dc-matter-bridge',
    'e3dc-bluelink',
    'e3dc-notifier',
    'e3dc-mqtt-hub',
    'e3dc-websocket',
    'e3dc-shadow-sync',
)


def _catalog_service_names(include_legacy: bool = False, exclude: set[str] | None = None) -> tuple[str, ...]:
    """Liefert Dienstnamen ohne .service aus dem zentralen Service-Katalog."""
    excluded = set(exclude or ())
    names: list[str] = []
    if include_legacy and 'e3dc' not in excluded:
        names.append('e3dc')

    try:
        service_units = allowed_services()
    except Exception:
        service_units = tuple(f'{name}.service' for name in CATALOG_FALLBACK_SERVICES)

    for unit in service_units:
        name = str(unit or '').strip()
        if name.endswith('.service'):
            name = name[:-8]
        if not name or name in excluded or name in names:
            continue
        names.append(name)
    return tuple(names)


# Liste aller katalogisierten V4-Dienste, die nach einem Update neu gestartet werden.
V4_SERVICES = list(_catalog_service_names())

INSTALL_CENTER_CORE_SERVICES = (
    'e3dc-live',
    'e3dc-epex-manager',
    'e3dc-weather-manager',
    'e3dc-storage-simulator',
    'e3dc-storage-manager',
    'e3dc-websocket',
    'e3dc-notifier',
)

HA_SLAVE_STANDBY_SERVICES = _catalog_service_names(include_legacy=True, exclude={'e3dc-ha'})
SHADOW_STANDBY_SERVICES = _catalog_service_names(include_legacy=True, exclude={'e3dc-shadow-sync'})

SYSTEMD_UNIT_DIRS = (
    '/etc/systemd/system',
    '/lib/systemd/system',
    '/usr/lib/systemd/system',
)

REQUIRED_WEB_FILES = (
    "index.html",
    "index.php",
    "helpers.php",
    "solar.js",
    "solar.min.js",
)


def _unit_name(service: str) -> str:
    name = str(service).strip()
    return name if name.endswith('.service') else f'{name}.service'


def _service_unit_exists(service: str) -> bool:
    unit = _unit_name(service)
    return any(os.path.exists(os.path.join(unit_dir, unit)) for unit_dir in SYSTEMD_UNIT_DIRS)


def _read_json_nofollow(path: str) -> tuple[dict, bytes]:
    """Read one bounded regular JSON file without accepting symlink components."""
    candidate = os.path.abspath(str(path))
    descriptor, before = _open_regular_file_nofollow(candidate)
    try:
        if not stat.S_ISREG(before.st_mode) or before.st_size > 4 * 1024 * 1024:
            raise RuntimeError(f"Ungueltige Konfigurationsdatei: {candidate}")
        chunks = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
            raise RuntimeError("Konfiguration wurde waehrend des Lesens veraendert")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Konfiguration ist nicht lesbares JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Konfiguration muss ein JSON-Objekt sein")
    return data, raw


def _current_ha_mode(config_path: str = HA_CONFIG_PATH) -> str:
    """Read the HA/Shadow role strictly; unreadable state is never treated as off."""
    data, _raw = _read_json_nofollow(config_path)
    if "ha_mode" not in data:
        raise RuntimeError("HA-/Shadow-Rolle fehlt in der Konfiguration")
    mode = str(data.get("ha_mode")).strip().lower()
    if mode not in VALID_HA_ROLES:
        raise RuntimeError(f"Ungueltige HA-/Shadow-Rolle: {mode!r}")
    return mode


def _catalog_units_strict() -> tuple[str, ...]:
    units = tuple(str(unit).strip() for unit in allowed_services())
    if not units or any(not unit.endswith(".service") for unit in units):
        raise RuntimeError("Service-Katalog ist unvollstaendig oder ungueltig")
    if len(set(units)) != len(units):
        raise RuntimeError("Service-Katalog enthaelt doppelte Units")
    return units


def _capture_transition_state(
    *,
    expected_role: str | None = None,
    allow_missing_config: bool = False,
    config_path: str = HA_CONFIG_PATH,
) -> TransitionState:
    requested = str(expected_role or "").strip().lower() or None
    if requested is not None and requested not in VALID_HA_ROLES:
        raise RuntimeError(f"Ungueltige erwartete HA-/Shadow-Rolle: {requested!r}")
    legacy = False
    try:
        config, raw = _read_json_nofollow(config_path)
        if "ha_mode" not in config:
            raise RuntimeError("HA-/Shadow-Rolle fehlt in der Konfiguration")
        role = str(config.get("ha_mode")).strip().lower()
        if role not in VALID_HA_ROLES:
            raise RuntimeError(f"Ungueltige HA-/Shadow-Rolle: {role!r}")
    except FileNotFoundError:
        if not allow_missing_config or requested is None:
            raise RuntimeError(f"HA-/Shadow-Konfiguration fehlt: {config_path}")
        config, raw, role, legacy = {"ha_mode": requested}, b"", requested, True
    if requested is not None and role != requested:
        raise RuntimeError(f"Erwartete HA-/Shadow-Rolle {requested}, gefunden {role}")
    inventory = {
        unit
        for unit in (*_catalog_units_strict(), "piguard.service", "e3dc.service")
        if _service_unit_exists(unit)
    }
    legacy_activity = "absent"
    if "e3dc.service" in inventory:
        legacy_status = run_command("systemctl is-active e3dc.service", timeout=10)
        legacy_activity = legacy_status.get("stdout", "").strip().lower()
        if legacy_activity not in {"active", "inactive", "failed"}:
            raise RuntimeError("Legacy-e3dc-Betriebszustand ist nicht lesbar")
    return TransitionState(
        ha_role=role,
        config=dict(config),
        config_sha256=hashlib.sha256(raw).hexdigest(),
        config_path=config_path,
        preinstalled_units=frozenset(inventory),
        bootstrap_legacy_config=legacy,
        legacy_e3dc_activity=legacy_activity,
    )


def _verify_transition_state(state: TransitionState, *, expect_legacy_config_missing: bool = False) -> None:
    if expect_legacy_config_missing:
        if not state.bootstrap_legacy_config:
            raise RuntimeError("Legacy-Konfigurationspruefung ist fuer diesen Ausgangszustand ungueltig")
        try:
            _read_json_nofollow(state.config_path)
        except FileNotFoundError:
            return
        except Exception as exc:
            raise RuntimeError("Urspruenglich fehlende V4-Konfiguration ist nach Recovery unsicher") from exc
        raise RuntimeError("Urspruenglich fehlende V4-Konfiguration blieb nach Recovery bestehen")
    config, _raw = _read_json_nofollow(state.config_path)
    if "ha_mode" not in config:
        raise RuntimeError("HA-/Shadow-Rolle fehlt nach dem Release-Wechsel")
    role = str(config.get("ha_mode")).strip().lower()
    if role not in VALID_HA_ROLES or role != state.ha_role:
        raise RuntimeError(
            f"HA-/Shadow-Rolle driftete waehrend Release-Wechsel: {state.ha_role} -> {role}"
        )
    if not state.bootstrap_legacy_config and config != state.config:
        raise RuntimeError("Betriebskonfiguration wurde waehrend Release-Wechsel veraendert")


def _read_legacy_config_nofollow(path: str, maximum: int = 2 * 1024 * 1024) -> dict | None:
    """Read one legacy key=value file without following a leaf or parent symlink."""

    try:
        descriptor, metadata = _open_regular_file_nofollow(path)
    except FileNotFoundError:
        return None
    try:
        if metadata.st_size > maximum:
            raise RuntimeError("Legacy-Konfiguration ist unplausibel gross")
        chunks = []
        remaining = maximum + 1
        while remaining > 0:
            block = os.read(descriptor, min(65536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > maximum or b"\x00" in raw:
        raise RuntimeError("Legacy-Konfiguration ist ungueltig")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Legacy-Konfiguration ist nicht UTF-8-lesbar") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise RuntimeError("Legacy-Konfiguration enthaelt eine ungueltige Zeile")
        key, value = stripped.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if not re.fullmatch(r"[a-z0-9_]+", key):
            raise RuntimeError("Legacy-Konfiguration enthaelt einen ungueltigen Schluessel")
        if key in values and values[key] != value:
            raise RuntimeError("Legacy-Konfiguration enthaelt widerspruechliche Doppelwerte")
        values[key] = value
    return values


def _migrate_bootstrap_legacy_config(repo_dir: str, state: TransitionState) -> None:
    """Create the V4 JSON transactionally for an explicitly role-bound V3/ZIP bootstrap."""

    if not state.bootstrap_legacy_config:
        return
    from .config_manager import V4_ALL_KEYS, _sort_by_blocks

    target = Path(state.config_path)
    candidates = (
        target.parent / "e3dc.config.txt",
        Path(repo_dir) / "e3dc.config.txt",
    )
    merged: dict[str, str] = {}
    found = False
    for candidate in candidates:
        values = _read_legacy_config_nofollow(str(candidate))
        if values is None:
            continue
        found = True
        for key, value in values.items():
            if key in merged and merged[key] != value:
                raise RuntimeError("Legacy-Konfigurationsquellen widersprechen sich")
            merged[key] = value

    legacy_role = str(merged.get("ha_mode", "")).strip().lower()
    if legacy_role and legacy_role != state.ha_role:
        raise RuntimeError("Legacy-HA-/Shadow-Rolle weicht von --expected-ha-role ab")
    migrated = {key: value for key, value in merged.items() if key in V4_ALL_KEYS and value != ""}
    migrated["ha_mode"] = state.ha_role
    payload = (json.dumps(_sort_by_blocks(migrated), ensure_ascii=False, indent=4) + "\n").encode("utf-8")

    parent_descriptor = _open_directory_nofollow(target.parent)
    temporary = ".{}.e3dc-migrate-{}".format(target.name, os.getpid())
    placeholder_created = False
    temporary_created = False
    try:
        try:
            os.stat(target.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("V4-Konfiguration entstand unerwartet waehrend der Migration")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        temporary_created = True
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        placeholder = os.open(
            target.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        os.close(placeholder)
        placeholder_created = True
        os.replace(
            temporary,
            target.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_created = False
        placeholder_created = False
        os.fsync(parent_descriptor)
        written, _raw = _read_json_nofollow(str(target))
        if written != _sort_by_blocks(migrated):
            raise RuntimeError("Migrierte V4-Konfiguration konnte nicht exakt verifiziert werden")
    finally:
        if temporary_created:
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        if placeholder_created:
            try:
                os.unlink(target.name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)
    print("  [OK] V3/ZIP-Konfiguration sicher migriert ({} lokale Werte).".format(len(migrated) - 1 if found else 0))


def _ha_slave_standby_services(state: TransitionState | None = None) -> set[str]:
    mode = state.ha_role if state is not None else _current_ha_mode()
    if mode == 'slave':
        return set(HA_SLAVE_STANDBY_SERVICES)
    if mode == 'shadow':
        return set(SHADOW_STANDBY_SERVICES)
    return set()


def _ha_standby_label(state: TransitionState | None = None) -> str:
    mode = state.ha_role if state is not None else _current_ha_mode()
    return 'Shadow-Standby' if mode == 'shadow' else 'HA-Slave-Standby'


def _installation_missing_reasons() -> list[str]:
    """Erkennt, ob ein geklontes Repository schon als System installiert ist."""
    checks = [
        ("/var/www/html/index.php", "Webportal fehlt: /var/www/html/index.php"),
        ("/var/www/html/helpers.php", "Webportal unvollständig: /var/www/html/helpers.php"),
    ]
    missing = [message for path, message in checks if not os.path.exists(path)]
    missing.extend(
        f"Kerndienst fehlt: {_unit_name(service)}"
        for service in INSTALL_CENTER_CORE_SERVICES
        if not _service_unit_exists(service)
    )
    return missing


def _run_core_service_installer(label: str, installer) -> bool:
    print(f"  [->] {label}...")
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        result = installer()
    except Exception as exc:
        sys.stdout = old_stdout
        details = captured.getvalue().strip()
        print(f"  [!] {label}: {exc}")
        if details:
            print("      " + details.splitlines()[-1][:160])
        update_logger.warning(f"{label} konnte nicht sichergestellt werden: {exc}")
        return False
    finally:
        sys.stdout = old_stdout

    if result is False:
        details = captured.getvalue().strip()
        print(f"  [!] {label}: Installer meldet Fehler.")
        if details:
            print("      " + details.splitlines()[-1][:160])
        return False

    print(f"  [OK] {label}")
    return True


def _ensure_install_center_core_services() -> bool:
    """Installiert fehlende Kernsystem-Units aus dem Install-Center nach."""
    print('\n[->] Stelle Kernsystem-Dienste aus dem Install-Center sicher...')
    ok = True

    try:
        from .epex_manager import install_epex_service
        ok = _run_core_service_installer(
            "Live, Marktpreise, Forecast und Storage-Dienste",
            lambda: install_epex_service(start_services=False),
        ) and ok
    except Exception as exc:
        print(f"  [!] Kern-Manager-Installer konnte nicht geladen werden: {exc}")
        update_logger.warning(f"Kern-Manager-Installer konnte nicht geladen werden: {exc}")
        ok = False

    try:
        from .system import setup_websocket_service
        ok = _run_core_service_installer(
            "WebUI Live-Animationen",
            lambda: setup_websocket_service(start_service=False),
        ) and ok
    except Exception as exc:
        print(f"  [!] WebSocket-Installer konnte nicht geladen werden: {exc}")
        update_logger.warning(f"WebSocket-Installer konnte nicht geladen werden: {exc}")
        ok = False

    try:
        from .install_notifier import install_notifier
        ok = _run_core_service_installer(
            "Zeitplanung und Langzeit-Archiv",
            lambda: install_notifier(start_service=False),
        ) and ok
    except Exception as exc:
        print(f"  [!] Notifier-/Archivar-Installer konnte nicht geladen werden: {exc}")
        update_logger.warning(f"Notifier-/Archivar-Installer konnte nicht geladen werden: {exc}")
        ok = False

    missing = [
        _unit_name(service)
        for service in INSTALL_CENTER_CORE_SERVICES
        if not _service_unit_exists(service)
    ]
    if missing:
        print("  [!] Folgende Kernsystem-Units fehlen weiterhin: " + ", ".join(missing))
        return False

    print("  [OK] Alle Kernsystem-Units sind installiert.")
    return ok


def _required_web_file_errors(html_src: str) -> list[str]:
    errors = []
    for rel_path in REQUIRED_WEB_FILES:
        path = os.path.join(html_src, rel_path)
        if not os.path.exists(path):
            errors.append(f"fehlt: html/{rel_path}")
        elif os.path.getsize(path) <= 0:
            errors.append(f"leer: html/{rel_path}")
    return errors


def _restore_repo_web_files(repo_dir: str, target_commit: str | None = None) -> bool:
    """Restore web files only from an already verified commit object."""
    print("  [!] Repo-Webdateien fehlen oder sind leer.")
    try:
        commit = _validate_full_commit(str(target_commit or ""))
    except ValueError:
        print("  [!] Ohne exakte Ziel-SHA wird kein Webbaum wiederhergestellt.")
        return False
    restore_target = ("html", "VERSION", "CHANGELOG.md", "UPDATE_POLICY.json")
    result = _git_argv(
        repo_dir,
        get_install_user(),
        "restore",
        f"--source={commit}",
        "--",
        *restore_target,
        timeout=60,
    )
    if result["success"]:
        print("  [OK] Repo-Webdateien aus verifiziertem Ziel-Commit wiederhergestellt.")
        return True
    print(f"  [!] Repo-Webdateien konnten nicht wiederhergestellt werden: {result['stderr']}")
    return False


def _ensure_rsync_available() -> bool:
    if shutil.which("rsync"):
        return True
    print("  [->] rsync fehlt, installiere Paket...")
    result = _run_argv(["sudo", "apt-get", "install", "-y", "--", "rsync"], timeout=180)
    if result["success"]:
        print("  [OK] rsync installiert.")
        return True
    print(f"  [!] rsync konnte nicht installiert werden: {result['stderr']}")
    return False


def _prepare_webroot_dirs() -> None:
    run_command("sudo mkdir -p /var/www/html/data /var/www/html/logs /var/www/html/ramdisk /var/www/html/tmp", timeout=20)


def _aux_inverter_migration_backup_structure_safe(path: str) -> bool:
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


def _verify_aux_inverter_migration_backup_modes(path: str) -> bool:
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


def _harden_aux_inverter_migration_backups(path: str) -> bool:
    if not os.path.lexists(path):
        return True
    if not _aux_inverter_migration_backup_structure_safe(path):
        return False
    quoted = shlex.quote(path)
    commands = (
        f"sudo chmod 00700 {quoted}",
        f"sudo find -P {quoted} -type d -exec chmod 00700 {{}} +",
        f"sudo find -P {quoted} -type f -exec chmod 00600 {{}} +",
    )
    for command in commands:
        result = run_command(command, timeout=10)
        if not isinstance(result, dict) or not result.get("success"):
            return False
    return _verify_aux_inverter_migration_backup_modes(path)


def _fix_webroot_permissions() -> bool:
    install_user = shlex.quote(get_install_user())
    secret_file_mode = config_secret_file_mode_text()
    secret_dir_mode = config_secret_dir_mode_text()
    repo_v4_config = shlex.quote(os.path.join(get_install_path(), "data", "e3dc_v4.json"))
    web_backup_dir = "/var/www/html/data/config_backups"
    repo_backup_dir = os.path.join(get_install_path(), "data", "config_backups")
    run_command(f"sudo usermod -aG www-data {install_user} 2>/dev/null || true", timeout=10)
    protected_wallbox_jobs = "/var/www/html/data/.wallbox_plan_jobs"
    protected_matter_storage = "/var/www/html/data/matter-storage"
    run_command(
        "sudo find -P /var/www/html -xdev "
        f"\\( -path {protected_wallbox_jobs} \\) -prune -o "
        f"\\( -type d -o -type f \\) -exec chown {install_user}:www-data {{}} +",
        timeout=60,
    )
    run_command(
        "sudo find -P /var/www/html -xdev "
        "\\( -path /var/www/html/data/e3dc_v4.json "
        "-o -path /var/www/html/data/config_backups "
        f"-o -path {protected_matter_storage} "
        f"-o -path {protected_wallbox_jobs} \\) -prune -o "
        "-type d -exec chmod 775 {} +",
        timeout=60,
    )
    run_command(
        "sudo find -P /var/www/html -xdev "
        "\\( -path /var/www/html/data/e3dc_v4.json "
        "-o -path /var/www/html/data/config_backups "
        f"-o -path {protected_matter_storage} "
        f"-o -path {protected_wallbox_jobs} \\) -prune -o "
        "-type f -exec chmod 664 {} +",
        timeout=60,
    )
    run_command("sudo chmod 2775 /var/www/html/data /var/www/html/logs /var/www/html/ramdisk /var/www/html/tmp 2>/dev/null || true", timeout=10)
    run_command(f"sudo chmod {secret_file_mode} /var/www/html/data/e3dc_v4.json 2>/dev/null || true", timeout=5)
    run_command(f"sudo chmod {secret_file_mode} {repo_v4_config} 2>/dev/null || true", timeout=5)
    for raw_backup_dir in (web_backup_dir, repo_backup_dir):
        raw_migration_dir = os.path.join(raw_backup_dir, "aux_inverter_migration")
        config_backup_dir = shlex.quote(raw_backup_dir)
        migration_backup_dir = shlex.quote(raw_migration_dir)
        run_command(f"sudo chmod {secret_dir_mode} {config_backup_dir} 2>/dev/null || true", timeout=5)
        run_command(
            f"sudo find -P {config_backup_dir} -path {migration_backup_dir} -prune -o "
            f"-type d -exec chmod {secret_dir_mode} {{}} + 2>/dev/null || true",
            timeout=10,
        )
        run_command(
            f"sudo find -P {config_backup_dir} -path {migration_backup_dir} -prune -o "
            f"-type f -exec chmod {secret_file_mode} {{}} + 2>/dev/null || true",
            timeout=10,
        )
        if not _harden_aux_inverter_migration_backups(raw_migration_dir):
            raise RuntimeError("Zusatz-WR-Migrationsbackups konnten nicht sicher gehärtet werden")
    run_command("sudo chmod 664 /var/www/html/ramdisk/value_filter.json 2>/dev/null || true", timeout=5)
    return True


def _send_telegram(message: str):
    """Sendet optional eine Telegram-Nachricht via boot_notify.sh."""
    notify_script = '/usr/local/bin/boot_notify.sh'
    if os.path.exists(notify_script) and os.access(notify_script, os.X_OK):
        try:
            subprocess.run([notify_script, message],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def _normalize_release_tag(tag: str) -> str:
    """Validiert einen Release-Tag fuer gezielte Rueckfallinstallationen."""
    tag = str(tag or '').strip()
    if not re.fullmatch(r'v?\d+\.\d+\.\d+[A-Za-z0-9._-]*', tag):
        raise ValueError('Ungueltiger Release-Tag.')
    return tag if tag.startswith('v') else 'v' + tag


def _validate_full_commit(commit: str) -> str:
    value = str(commit or '').strip().lower()
    if not FULL_COMMIT_RE.fullmatch(value):
        raise ValueError('Ziel-Commit muss eine vollstaendige 40-stellige SHA-1 sein.')
    return value


def _exact_commit_matches(expected: str, actual: str) -> bool:
    """Pure exact-SHA gate used by update and regression simulations."""
    try:
        return _validate_full_commit(expected) == _validate_full_commit(actual)
    except ValueError:
        return False


def _resolve_git_commit(repo_dir: str, ref: str, install_user: str) -> str | None:
    result = _git_argv(repo_dir, install_user, "rev-parse", "--verify", str(ref) + "^{commit}", timeout=15)
    if not result['success']:
        return None
    try:
        return _validate_full_commit(result['stdout'].strip())
    except ValueError:
        return None


def _delete_approved_stale_paths(
    paths,
    *,
    allowed_files=APPROVED_STALE_DELETE_FILES,
    allowed_dirs=APPROVED_STALE_DELETE_DIRS,
) -> tuple[bool, list[str]]:
    """Delete only exact, code-reviewed stale targets from a positive list."""
    errors: list[str] = []
    for raw_path in paths or []:
        if not isinstance(raw_path, str) or not raw_path.startswith('/'):
            errors.append(f'Ungueltiger Stale-Pfad: {raw_path!r}')
            continue
        path = os.path.abspath(raw_path)
        if path != raw_path:
            errors.append(f'Nicht normalisierter Stale-Pfad: {raw_path}')
            continue
        try:
            if path in allowed_files:
                if not os.path.lexists(path):
                    continue
                metadata = os.lstat(path)
                if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                    errors.append(f'Erwartete Datei ist ein Verzeichnis: {path}')
                    continue
                os.unlink(path)
            elif path in allowed_dirs:
                if not os.path.lexists(path):
                    continue
                metadata = os.lstat(path)
                if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                    _remove_tree_nofollow(path)
                else:
                    os.unlink(path)
            else:
                errors.append(f'Nicht freigegebener Stale-Pfad: {path}')
        except Exception as exc:
            errors.append(f'Stale-Pfad konnte nicht entfernt werden ({path}): {exc}')
    return not errors, errors


def _local_http_healthcheck(urls=LOCAL_HEALTH_URLS, timeout: float = 10.0) -> list[str]:
    """Return local-only HTTP hard-gate errors without contacting a remote host."""
    errors: list[str] = []
    for url in urls:
        try:
            request = urllib.request.Request(url, method='GET', headers={'User-Agent': 'E3DC-Update-Health/1'})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, 'status', response.getcode()))
                if status != 200:
                    errors.append(f'{url} liefert HTTP {status}')
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            errors.append(f'{url} nicht gesund: {exc}')
    return errors


def _read_version_file(repo_dir: str) -> str:
    for candidate in (os.path.join(repo_dir, 'VERSION'), '/var/www/html/VERSION'):
        try:
            with open(candidate, 'r', encoding='utf-8') as f:
                version = f.read().strip()
            if version:
                return version.lstrip('v')
        except Exception:
            pass
    return ''


def _read_policy_from_commit(repo_dir: str, commit: str, install_user: str | None = None) -> dict:
    """Read UPDATE_POLICY.json from one verified commit object, never the worktree."""
    verified_commit = _validate_full_commit(commit)
    user = install_user or get_install_user()
    raw = _read_commit_blob(
        repo_dir,
        verified_commit,
        "UPDATE_POLICY.json",
        user,
        maximum=1024 * 1024,
    )
    if not raw or len(raw) > 1024 * 1024:
        raise RuntimeError("UPDATE_POLICY.json ist leer oder zu gross")
    try:
        policy = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"UPDATE_POLICY.json im Commit ist ungueltig: {exc}") from exc
    if not isinstance(policy, dict):
        raise RuntimeError("UPDATE_POLICY.json muss ein JSON-Objekt sein")
    return policy


def _rollback_release_map(repo_dir: str, *, head_commit: str | None = None, install_user: str | None = None) -> dict[str, str]:
    """Liefert nur explizit für Bare Metal freigegebene Tag-zu-SHA-Bindungen."""
    user = install_user or get_install_user()
    commit = head_commit or _resolve_git_commit(repo_dir, "HEAD", user)
    if not commit:
        raise RuntimeError("HEAD fuer Rueckfallpolicy konnte nicht verifiziert werden")
    policy = _read_policy_from_commit(repo_dir, commit, user)
    explicit = policy.get("rollback_release_shas") or {}
    if not isinstance(explicit, dict):
        raise RuntimeError("rollback_release_shas muss ein Objekt sein")
    declared: dict[str, str] = {}
    result: dict[str, str] = {}
    for item in policy.get("rollback_releases") or []:
        if not isinstance(item, dict):
            raise RuntimeError("rollback_releases enthaelt einen ungueltigen Eintrag")
        tag = _normalize_release_tag(str(item.get("tag") or item.get("version") or ""))
        raw_sha = item.get("commit_sha") or item.get("sha") or explicit.get(tag)
        sha = _validate_full_commit(str(raw_sha or ""))
        bare_metal_supported = item.get("bare_metal_supported")
        docker_supported = item.get("docker_supported")
        if not isinstance(bare_metal_supported, bool) or not isinstance(docker_supported, bool):
            raise RuntimeError(f"Rollback-Ziel {tag} besitzt keine eindeutige Umgebungsfreigabe")
        if tag in declared and declared[tag] != sha:
            raise RuntimeError(f"Rollback-Tag ist mehrdeutig: {tag}")
        declared[tag] = sha
        if bare_metal_supported:
            result[tag] = sha
    if set(explicit) - set(declared):
        raise RuntimeError("rollback_release_shas enthaelt nicht deklarierte Tags")
    return result


def _allowed_rollback_tags(repo_dir: str) -> set[str]:
    """Compatibility view of the strict, SHA-bound rollback map."""
    return set(_rollback_release_map(repo_dir))


def _target_tag_authorized(
    target_tag: str,
    *,
    policy_repo: str,
    target_commit: str | None = None,
    expected_release_sha: str | None = None,
    install_user: str | None = None,
    bootstrap_runner_repo: str | None = None,
) -> bool:
    """Authorize only a policy-mapped rollback or exact bootstrap tag+SHA pair."""
    try:
        normalized_tag = _normalize_release_tag(target_tag)
        normalized_target = _validate_full_commit(target_commit) if target_commit else None
    except ValueError:
        return False
    if bootstrap_runner_repo:
        runner_version = _read_version_file(bootstrap_runner_repo)
        try:
            expected = _validate_full_commit(str(expected_release_sha or ""))
        except ValueError:
            return False
        return bool(
            runner_version
            and normalized_tag == _normalize_release_tag(runner_version)
            and normalized_target
            and _exact_commit_matches(expected, normalized_target)
        )
    try:
        mapped = _rollback_release_map(policy_repo, install_user=install_user)
    except (RuntimeError, ValueError):
        return False
    required_sha = mapped.get(normalized_tag)
    return bool(required_sha and normalized_target and _exact_commit_matches(required_sha, normalized_target))


def _validate_policy_packages(policy: dict, key: str, allowlist: frozenset[str]) -> list[str]:
    raw = policy.get(key) or []
    if not isinstance(raw, list):
        raise RuntimeError(f"{key} muss eine Liste sein")
    packages: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not PACKAGE_NAME_RE.fullmatch(item):
            raise RuntimeError(f"Ungueltiger Paketname in {key}")
        if item not in allowlist:
            raise RuntimeError(f"Nicht freigegebenes Paket in {key}: {item}")
        if item in packages:
            raise RuntimeError(f"Doppeltes Paket in {key}: {item}")
        packages.append(item)
    return packages


def _validated_venv_pip_packages(policy: dict) -> list[str]:
    """Liest den neuen Managed-Key und bleibt für alte Policies kompatibel."""
    managed = _validate_policy_packages(
        policy,
        MANAGED_VENV_PIP_POLICY_KEY,
        APPROVED_PIP_PACKAGES,
    )
    explicit = _validate_policy_packages(policy, VENV_PIP_POLICY_KEY, APPROVED_PIP_PACKAGES)
    legacy = _validate_policy_packages(policy, LEGACY_PIP_POLICY_KEY, APPROVED_PIP_PACKAGES)
    if managed:
        if (explicit and explicit != managed) or (legacy and legacy != managed):
            raise RuntimeError("Managed-venv- und Legacy-pip-Pakete widersprechen sich")
        return managed
    if explicit and legacy and explicit != legacy:
        raise RuntimeError("venv_pip_packages und pip_packages widersprechen sich")
    return explicit or legacy


def _validated_core_apt_packages(policy: dict) -> list[str]:
    """Hält optionale Matter-Abhängigkeiten aus dem Core-Update heraus."""
    packages = _validate_policy_packages(policy, "apt_packages", APPROVED_APT_PACKAGES)
    return [package for package in packages if package not in MATTER_ONLY_APT_PACKAGES]


def _validated_release_apt_packages(policy: dict) -> list[str]:
    """Verbindet alte Core-Pakete mit dem altupdater-neutralen venv-Bootstrap."""

    core = _validated_core_apt_packages(policy)
    managed = _validate_policy_packages(
        policy,
        MANAGED_VENV_APT_POLICY_KEY,
        APPROVED_MANAGED_VENV_APT_PACKAGES,
    )
    return [*core, *(package for package in managed if package not in core)]


def _verify_worktree_policy(repo_dir: str, verified_policy: dict) -> None:
    worktree_policy, _raw = _read_json_nofollow(os.path.join(repo_dir, "UPDATE_POLICY.json"))
    if worktree_policy != verified_policy:
        raise RuntimeError("Worktree-Policy weicht vom verifizierten HEAD-Blob ab")


def _secure_repo_permissions(repo_dir: str, install_user: str) -> None:
    """Keep Git/code owner-writable only; www-data never receives repo write access."""
    root = os.path.abspath(repo_dir)
    current = os.path.sep
    for component in Path(root).parts[1:]:
        current = os.path.join(current, component)
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"Symlink im Repositorypfad: {current}")
    account = pwd.getpwnam(str(install_user))
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        for name in list(dirnames):
            path = os.path.join(directory, name)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                os.lchown(path, account.pw_uid, account.pw_gid)
                dirnames.remove(name)
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(f"Ungueltiger Eintrag im Repository: {path}")
            os.chown(path, account.pw_uid, account.pw_gid)
            os.chmod(path, 0o755)
        for name in filenames:
            path = os.path.join(directory, name)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                os.lchown(path, account.pw_uid, account.pw_gid)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError(f"Nicht-regulaere Datei im Repository: {path}")
            os.chown(path, account.pw_uid, account.pw_gid)
            os.chmod(path, 0o755 if info.st_mode & 0o111 else 0o644)
    os.chown(root, account.pw_uid, account.pw_gid)
    os.chmod(root, 0o755)


def _truthy_feature_value(value) -> bool:
    if isinstance(value, dict):
        for key in ("enabled", "active", "enable"):
            if key in value:
                return _truthy_feature_value(value.get(key))
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "none", "null", "disabled", "nein"}
    return bool(value)


OPTIONAL_SERVICE_ENABLE_KEYS = {
    "e3dc-wallbox-manager": ("wb_native_enable",),
    "energy_manager": ("luxtronik", "wp_type", "luxtronik_ip", "idm_ip", "stiebel_isg_ip", "dimplex_ip"),
    "e3dc-lux-live": ("luxtronik", "luxtronik_ip"),
    "e3dc-idm-live": ("idm_ip",),
    "e3dc-stiebel-live": ("stiebel_isg_ip",),
    "e3dc-dimplex-live": ("dimplex_ip",),
    "e3dc-heizstab": ("heizstab", "heizstab_ip", "shelly_heiz_ip"),
    "e3dc-climate-live": ("climate_enable",),
    "e3dc-climate-control": ("climate_control_enable",),
    "e3dc-matter-bridge": ("matter_bridge",),
    "e3dc-bluelink": ("bluelink_refresh_token", "bluelink_vin"),
    "e3dc-mqtt-hub": ("mqtt_hub_ip", "mqtt_hub_topic"),
}


def _service_expected(service: str, state: TransitionState) -> tuple[bool, str]:
    name = str(service).removesuffix(".service")
    if name == "e3dc":
        return False, "Legacy-C++-Dienst bleibt dauerhaft deaktiviert"
    if name in _ha_slave_standby_services(state):
        return False, f"durch Rolle {state.ha_role} explizit Standby"
    unit = _unit_name(name)
    module = get_module_by_service(unit)
    if module is None:
        if name == "piguard":
            return unit in state.preinstalled_units, "Watchdog war vor dem Wechsel nicht installiert"
        raise RuntimeError(f"Dienst fehlt im Service-Katalog: {unit}")
    if not module.optional:
        return True, "Pflichtdienst"
    if name == "e3dc-ha":
        return state.ha_role in {"master", "slave"}, f"HA-Rolle ist {state.ha_role}"
    if name == "e3dc-shadow-sync":
        return state.ha_role == "shadow", f"HA-/Shadow-Rolle ist {state.ha_role}"
    keys = OPTIONAL_SERVICE_ENABLE_KEYS.get(name)
    if keys is not None and not any(_truthy_feature_value(state.config.get(key)) for key in keys):
        return False, "Feature ist in eingefrorener Konfiguration explizit deaktiviert"
    if unit in state.preinstalled_units:
        return True, "vor dem Wechsel installiert und nicht explizit deaktiviert"
    return True, "Policy erwartet Dienst; kein expliziter Feature-Disable vorhanden"


def _validated_restart_services(policy: dict, state: TransitionState) -> list[str]:
    raw = policy.get("restart_services")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("restart_services fehlt oder ist leer")
    allowed = {unit.removesuffix(".service") for unit in _catalog_units_strict()}
    normalized: list[str] = []
    for item in raw:
        name = str(item).strip().removesuffix(".service")
        if name not in allowed or name == "e3dc":
            raise RuntimeError(f"Nicht freigegebener Restart-Dienst: {item!r}")
        if name not in normalized:
            normalized.append(name)
    role_service = {"master": "e3dc-ha", "slave": "e3dc-ha", "shadow": "e3dc-shadow-sync"}.get(state.ha_role)
    if role_service and role_service not in normalized:
        normalized.append(role_service)
    for unit in sorted(state.preinstalled_units):
        name = unit.removesuffix(".service")
        if name in allowed and name not in normalized:
            normalized.append(name)
    return normalized


def _release_venv_path(install_user: str) -> str:
    """Bindet den einzigen für einen Release-Bootstrap erlaubten venv-Pfad."""

    user = str(install_user or "").strip()
    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise RuntimeError("Installationsbenutzer für venv-Bootstrap fehlt") from exc
    home_raw = Path(account.pw_dir)
    if not home_raw.is_absolute() or home_raw.is_symlink() or not home_raw.is_dir():
        raise RuntimeError("Home-Verzeichnis für venv-Bootstrap ist nicht eindeutig")
    home = home_raw.resolve(strict=True)
    if home != home_raw:
        raise RuntimeError("Home-Verzeichnis für venv-Bootstrap enthält einen Alias")
    home_info = home.stat()
    if (
        home_info.st_uid not in (0, account.pw_uid)
        or home_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError("Home-Verzeichnis für venv-Bootstrap ist nicht vertrauenswürdig")

    config = load_config()
    name = str(config.get("venv_name") or ".venv_e3dc").strip()
    if (
        not name
        or name in {".", ".."}
        or os.path.basename(name) != name
        or "\x00" in name
    ):
        raise RuntimeError("venv-Name für Release-Bootstrap ist ungültig")
    candidate = home / name
    if candidate.parent != home:
        raise RuntimeError("venv-Pfad liegt nicht direkt im Benutzer-Home")

    # Bei einem fehlenden venv darf kein historisch konfigurierter Custom-Pfad
    # still ignoriert und daneben ein zweites Environment angelegt werden.
    # Vorhandene Custom-venvs werden ausschließlich von _find_venv_python()
    # geprüft; diese Funktion bindet nur den Missing-Bootstrap.
    configured_paths: list[tuple[str, object]] = [
        ("Installer-Konfiguration", config.get("venv_path")),
    ]
    if os.path.lexists(HA_CONFIG_PATH):
        web_config, _raw = _read_json_nofollow(HA_CONFIG_PATH)
        configured_paths.append(("Web-Konfiguration", web_config.get("venv_path")))
    for source, raw_path in configured_paths:
        value = str(raw_path or "").strip()
        if not value:
            continue
        configured = Path(value)
        if not configured.is_absolute() or configured != candidate:
            raise RuntimeError(
                f"{source} bindet einen abweichenden venv-Pfad; Missing-Bootstrap stoppt"
            )
    return str(candidate)


def _create_release_venv(install_user: str, expected_path: str) -> str:
    """Erzeugt ein zuvor als fehlend gebundenes Benutzer-venv ohne System-pip."""

    planned = _release_venv_path(install_user)
    if os.path.abspath(str(expected_path or "")) != planned:
        raise RuntimeError("Erwarteter venv-Pfad driftete vor der Erstellung")
    if os.path.lexists(planned):
        raise RuntimeError("Als fehlend gebundenes venv ist vor der Erstellung vorhanden")
    result = _run_argv(
        [
            "sudo", "-H", "-u", str(install_user),
            "python3", "-m", "venv", "--system-site-packages", planned,
        ],
        timeout=120,
    )
    if not result["success"]:
        raise RuntimeError("Benutzer-venv konnte nicht erstellt werden: " + result["stderr"].strip())
    venv_python = _find_venv_python(install_user)
    if not venv_python or str(Path(venv_python).parent.parent) != planned:
        raise RuntimeError("Neu erstelltes Benutzer-venv konnte nicht verifiziert werden")
    return venv_python


def _apply_verified_package_policy(
    policy: dict,
    install_user: str,
    *,
    expected_venv_state: str = "present",
    expected_venv_path: str = "",
) -> None:
    apt_packages = _validated_release_apt_packages(policy)
    pip_packages = _validated_venv_pip_packages(policy)
    git_repos = policy.get("git_repos") or []
    if git_repos:
        raise RuntimeError("Externe Git-Repositories sind im Release-Update nicht freigegeben")
    if apt_packages:
        result = _run_argv(["sudo", "apt-get", "install", "-y", "--no-upgrade", "--", *apt_packages], timeout=300)
        if not result["success"]:
            raise RuntimeError("apt-Installation fehlgeschlagen: " + result["stderr"].strip())
    if pip_packages:
        if expected_venv_state not in {"present", "missing"}:
            raise RuntimeError("Erwarteter venv-Ausgangszustand ist ungültig")
        venv_python = _find_venv_python(install_user)
        if expected_venv_state == "missing":
            if venv_python:
                raise RuntimeError("Als fehlend gebundenes venv ist vor Paketinstallation vorhanden")
            if "python3-venv" not in apt_packages:
                raise RuntimeError("Missing-venv-Bootstrap verlangt python3-venv in der Release-Policy")
            venv_python = _create_release_venv(install_user, expected_venv_path)
        elif not venv_python:
            raise RuntimeError("Python-Pakete angefordert, aber kein verifiziertes venv gefunden")
        actual_venv_path = str(Path(venv_python).parent.parent)
        if expected_venv_state == "present" and not expected_venv_path:
            # Direkte interne Aufrufer dürfen einen bereits verifizierten
            # Bestands-Pfad übernehmen. Der prozessübergreifende Finalizer
            # liefert immer den zuvor gebundenen absoluten Pfad.
            expected_venv_path = actual_venv_path
        if os.path.abspath(str(expected_venv_path or "")) != actual_venv_path:
            raise RuntimeError("Verifiziertes venv weicht vom gebundenen Paket-Preimage ab")
        pip_argv = [
            "sudo", "-H", "-u", str(install_user),
            venv_python, "-m", "pip", "install", "--quiet",
        ]
        if expected_venv_state == "present":
            # Ein bestehendes Installations-venv besitzt bereits seine
            # transitive Laufzeitbasis. Beim Releasewechsel dürfen deshalb nur
            # die policygebundenen Top-Level-Pakete verändert werden.
            pip_argv.append("--no-deps")
        else:
            # Ein neu erzeugtes venv wäre mit reinen Top-Level-Wheels nicht
            # lauffähig. Die dabei zusätzlich aufgelösten Abhängigkeiten werden
            # vom Paket-Preimage vollständig erfasst und bei einem Rücklauf
            # wieder entfernt.
            pip_argv.append("--prefer-binary")
        pip_argv.extend(["--", *pip_packages])
        result = _run_argv(pip_argv, timeout=180)
        if not result["success"]:
            package_names = ", ".join(pip_packages)
            raise RuntimeError(
                f"venv-pip-Installation fehlgeschlagen (Pakete: {package_names}): "
                + result["stderr"].strip()
            )


def _installed_apt_packages() -> frozenset[str]:
    result = _run_argv(
        ["dpkg-query", "-W", "-f=${binary:Package}\t${db:Status-Abbrev}\n"],
        timeout=60,
    )
    if not result["success"]:
        raise RuntimeError("Installierter apt-Paketstand ist nicht lesbar")
    installed = set()
    for line in result["stdout"].splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].startswith("ii "):
            installed.add(parts[0].strip())
    if not installed:
        raise RuntimeError("Installierter apt-Paketstand ist leer oder unplausibel")
    return frozenset(installed)


def _normalize_python_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", str(value or "").strip().lower())


def _installed_pip_packages(venv_python: str, install_user: str) -> dict[str, str]:
    result = _run_argv(
        [
            "sudo", "-H", "-u", str(install_user),
            venv_python, "-m", "pip", "list", "--format=json", "--disable-pip-version-check",
        ],
        timeout=90,
    )
    if not result["success"]:
        raise RuntimeError("Installierter venv-Paketstand ist nicht lesbar")
    try:
        rows = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        raise RuntimeError("Installierter venv-Paketstand ist ungueltig") from exc
    if not isinstance(rows, list):
        raise RuntimeError("Installierter venv-Paketstand ist ungueltig")
    installed: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Installierter venv-Paketstand ist ungueltig")
        name = _normalize_python_package_name(row.get("name"))
        version = str(row.get("version") or "").strip()
        if not name or not version or name in installed:
            raise RuntimeError("Installierter venv-Paketstand ist mehrdeutig")
        installed[name] = version
    return installed


def _capture_package_transaction(
    policy: dict,
    install_user: str,
    *,
    allow_missing_venv: bool = False,
) -> PackageTransactionState:
    apt_requested = tuple(_validated_release_apt_packages(policy))
    pip_requested = tuple(_validated_venv_pip_packages(policy))
    apt_before = _installed_apt_packages() if apt_requested else frozenset()
    venv_python = _find_venv_python(install_user) if pip_requested else None
    venv_existed = bool(venv_python)
    venv_path = str(Path(venv_python).parent.parent) if venv_python else None
    if pip_requested and not venv_python:
        if not allow_missing_venv:
            raise RuntimeError("Python-Pakete angefordert, aber kein verifiziertes venv gefunden")
        if "python3-venv" not in apt_requested:
            raise RuntimeError("Missing-venv-Bootstrap verlangt python3-venv in der Release-Policy")
        venv_path = _release_venv_path(install_user)
        if os.path.lexists(venv_path):
            raise RuntimeError("Fehlendes venv ist durch einen bestehenden Pfad blockiert")
    pip_before = _installed_pip_packages(venv_python, install_user) if venv_python else {}
    return PackageTransactionState(
        apt_before=apt_before,
        pip_before=tuple(sorted(pip_before.items())),
        venv_python=venv_python,
        install_user=str(install_user),
        apt_requested=apt_requested,
        pip_requested=pip_requested,
        venv_path=venv_path,
        venv_existed=venv_existed,
    )


def _remove_transaction_created_venv(state: PackageTransactionState) -> None:
    """Entfernt ausschließlich ein vor der Transaktion nachweislich fehlendes venv."""

    if state.venv_existed or not state.venv_path:
        return
    expected = _release_venv_path(state.install_user)
    path = os.path.abspath(state.venv_path)
    if path != expected:
        raise RuntimeError("Neu erzeugtes venv weicht vom gebundenen Rücklaufpfad ab")
    if not os.path.lexists(path):
        return
    metadata = os.lstat(path)
    try:
        account = pwd.getpwnam(state.install_user)
    except KeyError as exc:
        raise RuntimeError("Installationsbenutzer für venv-Rücklauf fehlt") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in (0, account.pw_uid)
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or os.path.realpath(path) != path
    ):
        raise RuntimeError("Neu erzeugtes venv ist für sicheren Rücklauf nicht eindeutig")
    shutil.rmtree(path)
    if os.path.lexists(path):
        raise RuntimeError("Neu erzeugtes venv blieb nach Rücklauf vorhanden")


def _restore_package_transaction(state: PackageTransactionState) -> None:
    """Remove only packages introduced by this no-upgrade/no-deps transaction."""

    if state.pip_requested:
        if not state.venv_existed:
            _remove_transaction_created_venv(state)
        else:
            if not state.venv_python:
                raise RuntimeError("venv-Python fehlt fuer Paket-Ruecklauf")
            before = dict(state.pip_before)
            after = _installed_pip_packages(state.venv_python, state.install_user)
            introduced = sorted(set(after) - set(before))
            changed = sorted(name for name in set(after) & set(before) if after[name] != before[name])
            if introduced:
                result = _run_argv(
                    [
                        "sudo", "-H", "-u", state.install_user,
                        state.venv_python, "-m", "pip", "uninstall", "--yes", "--",
                        *introduced,
                    ],
                    timeout=180,
                )
                if not result["success"]:
                    raise RuntimeError("Neu installierte venv-Pakete konnten nicht entfernt werden")
            if changed:
                pins = ["{}=={}".format(name, before[name]) for name in changed]
                result = _run_argv(
                    [
                        "sudo", "-H", "-u", state.install_user,
                        state.venv_python, "-m", "pip", "install", "--quiet", "--no-deps", "--",
                        *pins,
                    ],
                    timeout=180,
                )
                if not result["success"]:
                    raise RuntimeError("Geaenderte venv-Paketversionen konnten nicht restauriert werden")
            if _installed_pip_packages(state.venv_python, state.install_user) != before:
                raise RuntimeError("venv-Paketstand stimmt nach Ruecklauf nicht exakt")
    if state.apt_requested:
        apt_after = _installed_apt_packages()
        introduced = sorted(apt_after - state.apt_before)
        if introduced:
            result = _run_argv(["sudo", "apt-get", "remove", "-y", "--", *introduced], timeout=300)
            if not result["success"]:
                raise RuntimeError("Neu installierte apt-Pakete konnten nicht zurueckgerollt werden")
        if _installed_apt_packages() != state.apt_before:
            raise RuntimeError("apt-Paketstand stimmt nach Ruecklauf nicht exakt")


def _release_version_tuple(version: str) -> tuple:
    match = re.match(r'v?(\d+)\.(\d+)\.(\d+)([A-Za-z0-9._-]*)$', str(version or '').strip())
    if not match:
        return ()
    major, minor, patch, suffix = match.groups()
    suffix = suffix.strip('._-').lower()
    if not suffix:
        return (int(major), int(minor), int(patch), 0, ())
    suffix_key = []
    for token in re.findall(r'\d+|[a-z]+', suffix):
        if token.isdigit():
            suffix_key.append((1, int(token)))
        else:
            suffix_key.append((0, token))
    return (int(major), int(minor), int(patch), 1, tuple(suffix_key))


def _bootstrap_source_kind(version: str, has_git: bool) -> str:
    """Classify supported legacy sources for deterministic migration tests."""
    if not has_git:
        return 'v3-zip'
    parsed = _release_version_tuple(version)
    if parsed and parsed[:3] >= (4, 0, 1) and parsed[:3] <= (4, 0, 5):
        return 'v4.0.1-v4.0.5'
    return 'git-installation'


def _validate_bootstrap_install_path(path: str) -> str:
    raw = os.path.expanduser(str(path or ""))
    if not os.path.isabs(raw):
        raise ValueError('Bootstrap-Installationspfad muss absolut sein.')
    candidate = os.path.abspath(raw)
    if candidate in {'/', '/home', '/var', '/usr', '/etc', '/bin', '/sbin', '/lib'}:
        raise ValueError('Bootstrap-Installationspfad ist zu weit gefasst.')
    current = os.path.sep
    for component in Path(candidate).parts[1:]:
        current = os.path.join(current, component)
        try:
            info = os.lstat(current)
        except FileNotFoundError as exc:
            raise ValueError('Bootstrap-Installationspfad existiert nicht.') from exc
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f'Symlink im Bootstrap-Installationspfad: {current}')
    if not stat.S_ISDIR(os.lstat(candidate).st_mode):
        raise ValueError('Bootstrap-Installationspfad existiert nicht.')
    markers = (
        os.path.join(candidate, 'Installer'),
        os.path.join(candidate, 'installer_main.py'),
        os.path.join(candidate, 'e3dc.config.txt'),
        os.path.join(candidate, 'E3DC-Control'),
    )
    if not any(os.path.lexists(marker) and not stat.S_ISLNK(os.lstat(marker).st_mode) for marker in markers):
        raise ValueError('Bootstrap-Ziel ist keine erkennbare E3DC-Control Installation.')
    return candidate


def _prepare_git_repository(repo_dir: str, install_user: str | None = None, include_root: bool = False) -> None:
    """Erlaubt sichere Git-Zugriffe und ignoriert chmod-Reparaturen im Arbeitsbaum."""
    user = install_user or get_install_user()
    if include_root:
        result = _run_argv(["git", "config", "--global", "--add", "safe.directory", str(repo_dir)], timeout=10)
        if not result["success"]:
            raise RuntimeError("Root-safe.directory konnte nicht gesetzt werden")
        result = _run_argv(["git", "-C", str(repo_dir), "config", "core.fileMode", "false"], timeout=10)
        if not result["success"]:
            raise RuntimeError("Root core.fileMode konnte nicht gesetzt werden")
    result = _run_argv(
        ["sudo", "-H", "-u", str(user), "git", "config", "--global", "--add", "safe.directory", str(repo_dir)],
        timeout=10,
    )
    if not result["success"]:
        raise RuntimeError("safe.directory fuer Installationsnutzer konnte nicht gesetzt werden")
    result = _git_argv(repo_dir, user, "config", "core.fileMode", "false", timeout=10)
    if not result["success"]:
        raise RuntimeError("core.fileMode fuer Installationsnutzer konnte nicht gesetzt werden")


def get_current_commit(repo_dir: str) -> str | None:
    """Liest den aktuellen Git-Commit-Hash des Repos."""
    install_user = get_install_user()
    _prepare_git_repository(repo_dir, install_user=install_user)

    result = _git_argv(repo_dir, install_user, "rev-parse", "--verify", "HEAD^{commit}", timeout=5)
    if result['success']:
        try:
            return _validate_full_commit(result['stdout'].strip())
        except ValueError:
            return None
    else:
        print(f"[!] git rev-parse Fehler: {result['stderr'].strip()}")
        return None


def get_repo_url(repo_dir: str) -> str | None:
    """Liest die Remote-URL aus der Git-Config aus."""
    install_user = get_install_user()
    _prepare_git_repository(repo_dir, install_user=install_user)
    result = _git_argv(repo_dir, install_user, "remote", "get-url", "origin", timeout=5)
    return result['stdout'].strip() if result['success'] else None


def check_for_updates(repo_dir: str) -> int | None:
    """
    Prueft ob Updates verfuegbar sind.
    Gibt die Anzahl fehlender Commits zurueck, None bei Fehler.
    """
    install_user = get_install_user()
    _prepare_git_repository(repo_dir, install_user=install_user)
    
    fetch = _git_argv(repo_dir, install_user, "fetch", "origin", timeout=20)
    if not fetch['success']:
        log_warning('update', f'git fetch fehlgeschlagen: {fetch["stderr"]}')
        return None

    count = _git_argv(repo_dir, install_user, "rev-list", "--count", "HEAD..origin/main", timeout=5)
    if count['success']:
        try:
            return int(count['stdout'].strip())
        except ValueError:
            return None
    return None


def list_pending_commits(repo_dir: str) -> str:
    """Gibt die Liste ausstehender Commits als String zurueck."""
    install_user = get_install_user()
    _prepare_git_repository(repo_dir, install_user=install_user)
    result = _git_argv(repo_dir, install_user, "log", "HEAD..origin/main", "--oneline", timeout=5)
    return result['stdout'].strip() if result['success'] else ''


def _normalize_restart_services(services) -> list:
    """Normalisiert Dienstnamen und entfernt Duplikate bei erhaltener Reihenfolge."""
    normalized = []
    seen = set()
    for srv in services or V4_SERVICES:
        name = str(srv).replace('.service', '').strip()
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append(name)
    return normalized


def _stop_v4_services(services=None):
    """Stop every catalogued writer/integration plus watchdog and legacy core."""
    print('\n[->] Stoppe E3DC-Control-Dienste fuer Release-Wechsel...')
    errors = []
    try:
        all_names = [unit.removesuffix(".service") for unit in _catalog_units_strict()]
    except Exception as exc:
        print(f"  [!] Service-Katalog nicht lesbar: {exc}")
        return False
    for name in _normalize_restart_services(services):
        if name not in all_names and name not in {"piguard", "e3dc"}:
            errors.append(f"Nicht katalogisierter Stop-Dienst: {name}")
    for srv in (*all_names, "piguard", "e3dc"):
        if _service_unit_exists(srv):
            res = run_command(f'sudo systemctl stop {srv}', timeout=15)
            status = '[OK]' if res['success'] else '[!] FEHLER'
            print(f'  {status} {srv}')
            if not res['success']:
                errors.append(f'{srv} konnte nicht gestoppt werden')
                continue
            active = run_command(f'systemctl is-active {srv}', timeout=10)
            activity = active.get('stdout', '').strip().lower()
            if activity not in {'inactive', 'failed'}:
                errors.append(f'{srv} hat nach Stop keinen beweisbaren inaktiven Status ({activity or "unlesbar"})')
            if srv == "e3dc":
                run_command("sudo systemctl disable e3dc.service", timeout=15)
                run_command("sudo systemctl mask e3dc.service", timeout=15)
    install_user = get_install_user()
    for screen_user in (str(install_user), "root"):
        for screen_name in ("e3dc", "E3DC"):
            prefix = ["sudo", "-u", screen_user] if screen_user != "root" else ["sudo"]
            _run_argv([*prefix, "screen", "-S", screen_name, "-X", "quit"], timeout=10)
    _run_argv(["sudo", "pkill", "-x", "E3DC-Control"], timeout=10)
    _run_argv(["sudo", "pkill", "-f", r"(^|/)E3DC\.sh([[:space:]]|$)"], timeout=10)
    for probe in (
        ["pgrep", "-x", "E3DC-Control"],
        ["pgrep", "-f", r"(^|/)[E]3DC\.sh([[:space:]]|$)"],
    ):
        result = _run_argv(probe, timeout=10)
        if result.get("returncode") != 1:
            errors.append("Legacy-Screen-/Prozesspfad ist nicht beweisbar gestoppt")
    for screen_user in (str(install_user), "root"):
        prefix = ["sudo", "-u", screen_user] if screen_user != "root" else ["sudo"]
        listing = _run_argv([*prefix, "screen", "-ls"], timeout=10)
        sessions = listing.get("stdout", "")
        if re.search(r"\.(?:e3dc|E3DC)(?:\s|$)", sessions):
            errors.append(f"Legacy-Screen-Session fuer {screen_user} ist weiterhin aktiv")
    if errors:
        for error in errors:
            print(f'  [!] {error}')
        return False
    print('  [OK] Aktor-/Writer-Dienste sind fuer den Release-Wechsel in Ruhe.')
    return True


def _post_update_healthcheck(
    services=None,
    transition_state: TransitionState | None = None,
    *,
    legacy_recovery: bool = False,
) -> bool:
    """Kleiner Gesundheitstest nach Update oder Release-Rueckfall."""
    print('\n[->] Gesundheitstest...')
    errors = []
    try:
        state = transition_state or _capture_transition_state()
        _verify_transition_state(state, expect_legacy_config_missing=legacy_recovery)
        ha_slave_services = _ha_slave_standby_services(state)
        standby_label = _ha_standby_label(state)
    except Exception as exc:
        print(f"  [!] HA-/Shadow-Zustand nicht beweisbar: {exc}")
        return False
    if not os.path.exists('/var/www/html/index.php'):
        errors.append('/var/www/html/index.php fehlt')

    if shutil.which('php') and os.path.exists('/var/www/html/index.php'):
        lint = run_command('php -l /var/www/html/index.php', timeout=15)
        if not lint['success']:
            errors.append('PHP-Lint index.php fehlgeschlagen: ' + (lint['stderr'] or lint['stdout']).strip())

    health_services = _normalize_restart_services(services)
    for core_srv in INSTALL_CENTER_CORE_SERVICES:
        if core_srv not in health_services:
            health_services.append(core_srv)
    if "piguard.service" in state.preinstalled_units and "piguard" not in health_services:
        health_services.append("piguard")

    for srv in health_services:
        if not srv or srv == 'e3dc':
            continue
        try:
            expected, reason = _service_expected(srv, state)
        except Exception as exc:
            errors.append(str(exc))
            continue
        if not _service_unit_exists(srv):
            if expected:
                errors.append(f'{_unit_name(srv)} fehlt, obwohl erwartet ({reason})')
            continue
        status = run_command(f'systemctl is-active {srv}', timeout=10)
        activity = status.get('stdout', '').strip().lower()
        if not expected or srv in ha_slave_services:
            if activity not in {'inactive', 'failed'}:
                errors.append(f'{srv} ist im Standby nicht beweisbar inaktiv ({activity or "unlesbar"})')
            continue
        if not status['success'] or activity != 'active':
            errors.append(f'{srv} ist nicht aktiv')

    if _service_unit_exists("e3dc"):
        legacy = run_command("systemctl is-active e3dc", timeout=10)
        legacy_activity = legacy.get("stdout", "").strip().lower()
        if legacy_recovery and state.legacy_e3dc_activity == "active":
            if not legacy.get("success") or legacy_activity != "active":
                errors.append("Legacy e3dc.service wurde nach Recovery nicht aktiv")
        elif legacy_activity not in {"inactive", "failed"}:
            errors.append(f"Legacy e3dc.service ist nicht beweisbar inaktiv ({legacy_activity or 'unlesbar'})")

    errors.extend(_local_http_healthcheck())

    if errors:
        for error in errors[:8]:
            print(f'  [!] {error}')
        if len(errors) > 8:
            print(f'  [!] ... plus {len(errors) - 8} weitere Meldungen')
        return False

    print('  [OK] Gesundheitstest bestanden.')
    return True


def _restart_v4_services(
    headless: bool = False,
    services=None,
    transition_state: TransitionState | None = None,
    *,
    legacy_recovery: bool = False,
) -> bool:
    """Startet die installierten E3DC-Control-Dienste neu."""
    try:
        state = transition_state or _capture_transition_state()
        _verify_transition_state(state, expect_legacy_config_missing=legacy_recovery)
    except Exception as exc:
        print(f"  [!] HA-/Shadow-Zustand nicht beweisbar: {exc}")
        return False
    print('\n[->] Bereinige alte C++ E3DC-Dienste/Screens (falls vorhanden)...')
    install_user = get_install_user()
    legacy_unit_present = any(os.path.exists(path) for path in (
        '/etc/systemd/system/e3dc.service',
        '/lib/systemd/system/e3dc.service',
        '/usr/lib/systemd/system/e3dc.service',
    ))
    if legacy_unit_present and not legacy_recovery:
        run_command('sudo systemctl stop e3dc.service 2>/dev/null', timeout=15)
        run_command('sudo systemctl disable e3dc.service 2>/dev/null', timeout=15)
        run_command('sudo systemctl mask e3dc.service 2>/dev/null', timeout=15)
        print('  [OK] Legacy e3dc.service gestoppt/deaktiviert.')
    elif not legacy_unit_present:
        print('  [OK] Kein Legacy e3dc.service vorhanden.')
    if legacy_recovery:
        print('  [OK] Legacy-Betriebszustand bleibt fuer die Recovery erhalten.')
    else:
        run_command(f'sudo -u {install_user} screen -S e3dc -X quit 2>/dev/null', timeout=5)
        run_command(f'sudo -u {install_user} screen -S E3DC -X quit 2>/dev/null', timeout=5)
        run_command('sudo screen -S e3dc -X quit 2>/dev/null', timeout=5)
        run_command('sudo screen -S E3DC -X quit 2>/dev/null', timeout=5)
        run_command('sudo pkill -x E3DC-Control 2>/dev/null', timeout=5)
        run_command(r"sudo pkill -f '(^|/)E3DC\.sh([[:space:]]|$)' 2>/dev/null", timeout=5)
        print('  [OK] Legacy-Cleanup abgeschlossen; der alte C++ Kern wird nicht gestartet.')

    print('\n[->] E3DC-Control-Dienste werden aktiviert und gestartet...')
    ha_slave_services = _ha_slave_standby_services(state)
    standby_label = _ha_standby_label(state)
    start_services = _normalize_restart_services(services)
    if "piguard.service" in state.preinstalled_units and "piguard" not in start_services:
        start_services.append("piguard")
    errors = []
    for srv in start_services:
        if not srv or srv == 'e3dc':
            if srv == 'e3dc':
                print('  [SKIP] e3dc ist Legacy C++ und wird im Update nicht gestartet.')
            continue
        try:
            expected, reason = _service_expected(srv, state)
        except Exception as exc:
            errors.append(str(exc))
            continue
        if not expected or srv in ha_slave_services:
            if _service_unit_exists(srv):
                stopped = run_command(f'sudo systemctl stop {srv}', timeout=15)
                inactive = run_command(f'systemctl is-active {srv}', timeout=10)
                activity = inactive.get('stdout', '').strip().lower()
                if not stopped['success'] or activity not in {'inactive', 'failed'}:
                    errors.append(f'{srv} konnte fuer Standby nicht sicher gestoppt werden')
            print(f'  [SKIP] {srv} bleibt gestoppt: {reason}.')
            continue
        if not _service_unit_exists(srv):
            errors.append(f'{_unit_name(srv)} fehlt, obwohl erwartet ({reason})')
            continue
        if _service_unit_exists(srv):
            run_command(f'sudo systemctl reset-failed {srv} 2>/dev/null || true', timeout=10)
            enable = run_command(f'sudo systemctl enable {srv}', timeout=15)
            res = run_command(f'sudo systemctl restart {srv}', timeout=15)
            status = '[OK]' if enable['success'] and res['success'] else '[!] FEHLER'
            print(f'  {status} {srv}')
            if not enable['success'] or not res['success']:
                errors.append(f'{srv} konnte nicht aktiviert/gestartet werden')
    if errors:
        for error in errors:
            print(f"  [!] {error}")
        return False
    return True


def _capture_tree_inventory(
    root_path: str,
    *,
    excluded_top: tuple[str, ...] = (),
    excluded_anywhere: tuple[str, ...] = (),
) -> frozenset[str]:
    """Capture one no-symlink tree while deliberately excluding persistent subtrees."""
    root = os.path.abspath(root_path)
    if not os.path.isdir(root) or os.path.islink(root):
        raise RuntimeError(f"Inventarwurzel fehlt oder ist unsicher: {root}")
    entries: set[str] = set()
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        relative = os.path.relpath(directory, root)
        dirnames[:] = [
            name for name in dirnames
            if name not in excluded_anywhere and not (relative == "." and name in excluded_top)
        ]
        for name in dirnames:
            path = os.path.join(directory, name)
            if stat.S_ISLNK(os.lstat(path).st_mode):
                raise RuntimeError(f"Symlink im Installationsbaum: {path}")
            entries.add(os.path.relpath(path, root))
        for name in filenames:
            path = os.path.join(directory, name)
            if stat.S_ISLNK(os.lstat(path).st_mode):
                raise RuntimeError(f"Symlink im Installationsbaum: {path}")
            entries.add(os.path.relpath(path, root))
    return frozenset(entries)


def _install_tree_exclusions() -> tuple[tuple[str, ...], tuple[str, ...]]:
    configured_venv = str(load_config().get("venv_name") or ".venv_e3dc").strip()
    top_exclusions = tuple(
        sorted({name for name in (".git", configured_venv, ".venv_e3dc", ".venv", "venv") if name and "/" not in name and "\\" not in name})
    )
    return top_exclusions, ("node_modules", "__pycache__")


def _capture_install_inventory(repo_dir: str) -> frozenset[str]:
    top_exclusions, anywhere_exclusions = _install_tree_exclusions()
    return _capture_tree_inventory(
        repo_dir,
        excluded_top=top_exclusions,
        excluded_anywhere=anywhere_exclusions,
    )


def _remove_tree_nofollow(path: str) -> None:
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    name = os.path.basename(target)
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            os.unlink(name, dir_fd=parent_fd)
            return

        def remove_directory(parent_descriptor: int, child_name: str, dev: int, ino: int) -> None:
            descriptor = os.open(
                child_name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (dev, ino):
                    raise RuntimeError("Zu entfernender Baum wurde ausgetauscht")
                for entry in sorted(os.listdir(descriptor)):
                    info = os.stat(entry, dir_fd=descriptor, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        remove_directory(descriptor, entry, info.st_dev, info.st_ino)
                    elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                        os.unlink(entry, dir_fd=descriptor)
                    else:
                        raise RuntimeError("Unerlaubter Dateityp im zu entfernenden Baum")
            finally:
                os.close(descriptor)
            current = os.stat(child_name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (dev, ino):
                raise RuntimeError("Zu entfernender Baum wurde vor rmdir ausgetauscht")
            os.rmdir(child_name, dir_fd=parent_descriptor)

        remove_directory(parent_fd, name, metadata.st_dev, metadata.st_ino)
    finally:
        os.close(parent_fd)


def _remove_entries_not_in_inventory(
    root_path: str,
    inventory: frozenset[str],
    *,
    remove_git: bool = False,
    excluded_top: tuple[str, ...] = (".git",),
    excluded_anywhere: tuple[str, ...] = (),
) -> None:
    root = os.path.abspath(root_path)
    found_files: list[tuple[str, str]] = []
    found_dirs: list[tuple[str, str]] = []
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        relative = os.path.relpath(directory, root)
        dirnames[:] = [
            name for name in dirnames
            if name not in excluded_anywhere and not (relative == "." and name in excluded_top)
        ]
        for name in filenames:
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, root)
            found_files.append((path, rel))
        for name in dirnames:
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, root)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError(f"Symlink im wiederherzustellenden Baum: {path}")
            found_dirs.append((path, rel))
    for path, rel in found_files:
        if rel not in inventory:
            os.unlink(path)
    for path, rel in sorted(found_dirs, key=lambda item: item[1].count(os.sep), reverse=True):
        if rel not in inventory:
            os.rmdir(path)
    git_dir = os.path.join(root, ".git")
    if remove_git and os.path.lexists(git_dir):
        _remove_tree_nofollow(git_dir)


def _capture_recovery_surface(state: TransitionState) -> RecoverySurfaceInventory:
    web_inventory = _capture_tree_inventory(
        "/var/www/html",
        excluded_top=("data", "logs", "ramdisk", "tmp"),
    )
    watchdogs = frozenset(
        path for path in ("/usr/local/bin/boot_notify.sh", "/usr/local/bin/pi_guard.sh")
        if os.path.lexists(path)
    )
    enablement = []
    for unit in sorted(state.preinstalled_units):
        status = run_command(f"systemctl is-enabled {unit}", timeout=10)
        value = status.get("stdout", "").strip().lower()
        if value not in {"enabled", "enabled-runtime", "disabled", "static", "indirect", "masked", "generated", "transient", "alias"}:
            raise RuntimeError(f"Enablement-Status von {unit} ist nicht lesbar")
        enablement.append((unit, value))
    return RecoverySurfaceInventory(web_inventory, watchdogs, tuple(enablement))


def _restore_recovery_surface(inventory: RecoverySurfaceInventory, state: TransitionState) -> None:
    _remove_entries_not_in_inventory(
        "/var/www/html",
        inventory.web_program_entries,
        excluded_top=("data", "logs", "ramdisk", "tmp"),
    )
    allowed_units = set(_catalog_units_strict()) | {"piguard.service", "e3dc.service"}
    for unit in sorted(allowed_units - set(state.preinstalled_units)):
        path = os.path.join("/etc/systemd/system", unit)
        if os.path.lexists(path):
            os.unlink(path)
    for path in ("/usr/local/bin/boot_notify.sh", "/usr/local/bin/pi_guard.sh"):
        if path not in inventory.watchdog_files and os.path.lexists(path):
            os.unlink(path)
    daemon_reload = run_command("sudo systemctl daemon-reload", timeout=20)
    if not daemon_reload["success"]:
        raise RuntimeError("systemd daemon-reload nach Recovery fehlgeschlagen")
    for unit, previous in inventory.unit_enablement:
        if previous in {"enabled", "enabled-runtime"}:
            command = f"sudo systemctl enable {unit}"
        elif previous == "masked":
            command = f"sudo systemctl mask {unit}"
        elif previous == "disabled":
            command = f"sudo systemctl disable {unit}"
        else:
            continue
        result = run_command(command, timeout=20)
        if not result["success"]:
            raise RuntimeError(f"Enablement von {unit} konnte nicht wiederhergestellt werden")


def _read_commit_text(repo_dir: str, commit: str, path: str, install_user: str) -> str:
    verified = _validate_full_commit(commit)
    result = _git_argv(repo_dir, install_user, "show", f"{verified}:{path}", timeout=15)
    if not result["success"]:
        raise RuntimeError(f"{path} fehlt im verifizierten Ziel-Commit")
    return result["stdout"]


def _fetch_target_commit(repo_dir: str, install_user: str, target_tag: str | None) -> str:
    if target_tag:
        storage_ref = f"refs/tags/{target_tag}"
        refspec = f"+{storage_ref}:{storage_ref}"
        result = _git_argv(repo_dir, install_user, "fetch", "--no-tags", "origin", refspec, timeout=120)
        if not result["success"]:
            raise RuntimeError("Release-Tag-Fetch fehlgeschlagen: " + result["stderr"].strip())
        object_type = _git_argv(repo_dir, install_user, "cat-file", "-t", storage_ref, timeout=15)
        if not object_type["success"] or object_type["stdout"].strip() != "tag":
            raise RuntimeError(f"Release-Tag {target_tag} ist nicht annotiert")
        commit = _resolve_git_commit(repo_dir, storage_ref, install_user)
    else:
        result = _git_argv(
            repo_dir,
            install_user,
            "fetch",
            "--no-tags",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
            timeout=120,
        )
        if not result["success"]:
            raise RuntimeError("git fetch origin/main fehlgeschlagen: " + result["stderr"].strip())
        commit = _resolve_git_commit(repo_dir, "refs/remotes/origin/main", install_user)
    if not commit:
        raise RuntimeError("Exakter Ziel-Commit konnte nicht aufgeloest werden")
    return commit


def _validate_target_release(
    policy: dict,
    repo_dir: str,
    target_commit: str,
    target_tag: str | None,
    install_user: str,
) -> str:
    version = _read_commit_text(repo_dir, target_commit, "VERSION", install_user).strip().lstrip("v")
    if not version or str(policy.get("version") or "").strip().lstrip("v") != version:
        raise RuntimeError("VERSION und verifizierte UPDATE_POLICY stimmen nicht ueberein")
    stable = _normalize_release_tag(str(policy.get("stable_release") or ""))
    if target_tag and stable != target_tag:
        raise RuntimeError(f"Ziel-Tag {target_tag} ist nicht Stable des Ziel-Commits ({stable})")
    if not target_tag:
        storage_ref = f"refs/tags/{stable}"
        refspec = f"+{storage_ref}:{storage_ref}"
        fetched = _git_argv(repo_dir, install_user, "fetch", "--no-tags", "origin", refspec, timeout=120)
        if not fetched["success"]:
            raise RuntimeError("Stable-Tag des Ziel-Commits konnte nicht geladen werden")
        object_type = _git_argv(repo_dir, install_user, "cat-file", "-t", storage_ref, timeout=15)
        if not object_type["success"] or object_type["stdout"].strip() != "tag":
            raise RuntimeError("Stable-Tag des Ziel-Commits ist nicht annotiert")
        stable_commit = _resolve_git_commit(repo_dir, storage_ref, install_user)
        if not stable_commit or not _exact_commit_matches(stable_commit, target_commit):
            raise RuntimeError("origin/main ist nicht exakt durch den Stable-Tag der Policy gebunden")
    return stable


def _assert_tree_no_symlinks(root: str) -> None:
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        for name in (*dirnames, *filenames):
            path = os.path.join(directory, name)
            if stat.S_ISLNK(os.lstat(path).st_mode):
                raise RuntimeError(f"Symlink in Release-Baum nicht erlaubt: {path}")


def _sync_release_web(repo_dir: str, policy: dict) -> None:
    html_src = os.path.join(repo_dir, "html")
    errors = _required_web_file_errors(html_src)
    if errors:
        raise RuntimeError("Webquelle unvollstaendig: " + "; ".join(errors))
    _assert_tree_no_symlinks(html_src)
    if not _ensure_rsync_available():
        raise RuntimeError("rsync ist nicht verfuegbar")
    _prepare_webroot_dirs()
    result = _run_argv(
        [
            "sudo", "rsync", "-a",
            "--exclude", "data", "--exclude", "logs", "--exclude", "ramdisk", "--exclude", "tmp",
            html_src.rstrip(os.sep) + os.sep,
            "/var/www/html/",
        ],
        timeout=60,
    )
    if not result["success"]:
        raise RuntimeError("Websync fehlgeschlagen: " + result["stderr"].strip())
    if not os.path.exists(os.path.join(html_src, "app")):
        ok, delete_errors = _delete_approved_stale_paths(["/var/www/html/app"])
        if not ok:
            raise RuntimeError("Stale-Webpfad konnte nicht entfernt werden: " + "; ".join(delete_errors))
    for name in ("VERSION", "CHANGELOG.md", "UPDATE_POLICY.json"):
        source = os.path.join(repo_dir, name)
        if not os.path.isfile(source) or os.path.islink(source):
            raise RuntimeError(f"Release-Datei fehlt oder ist Symlink: {name}")
        result = _run_argv(["sudo", "install", "-m", "0644", source, os.path.join("/var/www/html", name)], timeout=15)
        if not result["success"]:
            raise RuntimeError(f"{name} konnte nicht in Webroot installiert werden")
    if not repair_legacy_paths_file():
        raise RuntimeError("Legacy-Pfadvertrag konnte nicht repariert werden")
    if not policy.get("run_permissions", True):
        if _fix_webroot_permissions() is not True:
            raise RuntimeError("Webroot-Rechtehärtung fehlgeschlagen")


def _restore_legacy_runtime_state(state: TransitionState) -> bool:
    """Restore and prove the pre-bootstrap legacy service activity only during recovery."""

    desired = state.legacy_e3dc_activity
    if desired == "absent":
        return not _service_unit_exists("e3dc")
    if desired not in {"active", "inactive", "failed"} or not _service_unit_exists("e3dc"):
        return False
    action = "start" if desired == "active" else "stop"
    changed = _run_argv(["sudo", "systemctl", action, "e3dc.service"], timeout=30)
    if not changed["success"]:
        return False
    status = _run_argv(["systemctl", "is-active", "e3dc.service"], timeout=15)
    activity = status.get("stdout", "").strip().lower()
    if desired == "active":
        return bool(status.get("success") and activity == "active")
    return activity in {"inactive", "failed"}


def _verify_bound_target_state(
    state: TransitionState,
    *,
    expected_config_state: str,
    expected_config_sha256: str,
    expected_units_sha256: str,
    expected_legacy_activity: str,
) -> None:
    """Beweist, dass Archiv- und Target-Prozess denselben Ausgangszustand sehen."""

    if expected_config_state not in {"present", "missing"}:
        raise RuntimeError("Ungültiger erwarteter Konfigurationszustand")
    expected_missing = expected_config_state == "missing"
    if bool(state.bootstrap_legacy_config) != expected_missing:
        raise RuntimeError("Konfigurationszustand driftete zwischen Archiv und Target-Finalizer")
    if not re.fullmatch(r"[0-9a-f]{64}", str(expected_config_sha256 or "")):
        raise RuntimeError("Erwarteter Konfigurationshash ist ungültig")
    if state.config_sha256 != expected_config_sha256:
        raise RuntimeError("Konfiguration driftete zwischen Archiv und Target-Finalizer")
    if not re.fullmatch(r"[0-9a-f]{64}", str(expected_units_sha256 or "")):
        raise RuntimeError("Erwarteter Unit-Inventarhash ist ungültig")
    if _transition_units_sha256(state.preinstalled_units) != expected_units_sha256:
        raise RuntimeError("Unit-Inventar driftete zwischen Archiv und Target-Finalizer")
    if expected_legacy_activity not in {"absent", "active", "inactive", "failed"}:
        raise RuntimeError("Erwarteter Legacy-Betriebszustand ist ungültig")
    if state.legacy_e3dc_activity != expected_legacy_activity:
        raise RuntimeError("Legacy-Betriebszustand driftete zwischen Archiv und Target-Finalizer")


def finalize_release_from_target(
    *,
    repo_dir: str,
    target_commit: str,
    target_tag: str,
    expected_role: str,
    expected_config_state: str,
    expected_config_sha256: str,
    expected_units_sha256: str,
    expected_legacy_activity: str,
    expected_venv_state: str,
    expected_venv_path: str,
    headless: bool = True,
) -> None:
    """Finalisiert einen Reset ausschließlich aus den geladenen Target-Dateien."""

    target_root = _validate_bootstrap_install_path(repo_dir)
    loaded_root = os.path.dirname(INSTALLER_DIR)
    if os.path.realpath(loaded_root) != target_root or os.path.realpath(INSTALL_PATH) != target_root:
        raise RuntimeError("Target-Finalizer wurde nicht aus dem gebundenen Zielbaum geladen")
    commit = _validate_full_commit(target_commit)
    tag = _normalize_release_tag(target_tag)
    role = str(expected_role or "").strip().lower()
    if role not in VALID_HA_ROLES:
        raise RuntimeError("Erwartete HA-/Shadow-Rolle ist ungültig")

    actual_commit = _resolve_git_commit(target_root, "HEAD", get_install_user())
    if not actual_commit or not _exact_commit_matches(commit, actual_commit):
        raise RuntimeError("Target-Finalizer sieht nicht den freigegebenen Ziel-Commit")

    state = _capture_transition_state(
        expected_role=role,
        allow_missing_config=expected_config_state == "missing",
    )
    _verify_bound_target_state(
        state,
        expected_config_state=expected_config_state,
        expected_config_sha256=expected_config_sha256,
        expected_units_sha256=expected_units_sha256,
        expected_legacy_activity=expected_legacy_activity,
    )

    install_user = get_install_user()
    policy = _read_policy_from_commit(target_root, commit, install_user)
    _validate_target_release(policy, target_root, commit, tag, install_user)
    restart_services = _validated_restart_services(policy, state)
    pip_packages = _validated_venv_pip_packages(policy)
    if pip_packages:
        if expected_venv_state not in {"present", "missing"}:
            raise RuntimeError("Paket-Preimage besitzt keinen gültigen venv-Zustand")
        if not os.path.isabs(str(expected_venv_path or "")):
            raise RuntimeError("Paket-Preimage besitzt keinen absoluten venv-Pfad")
    elif expected_venv_state != "unused" or expected_venv_path:
        raise RuntimeError("venv-Preimage ist ohne Python-Paketpolicy unzulässig")

    _secure_repo_permissions(target_root, install_user)
    _verify_worktree_policy(target_root, policy)
    _migrate_bootstrap_legacy_config(target_root, state)
    _apply_verified_package_policy(
        policy,
        install_user,
        expected_venv_state=expected_venv_state,
        expected_venv_path=expected_venv_path,
    )

    delete_ok, delete_errors = _delete_approved_stale_paths(policy.get("delete_files") or [])
    if not delete_ok:
        raise RuntimeError("Stale-Delete-Positivliste verletzt: " + "; ".join(delete_errors))

    _sync_release_web(target_root, policy)
    if policy.get("run_permissions", True):
        from .permissions import run_permissions_wizard
        if run_permissions_wizard(headless=True, release_quiesced=True) is False:
            raise RuntimeError("Berechtigungsreparatur fehlgeschlagen")
        _secure_repo_permissions(target_root, install_user)

    from .permissions import (
        ensure_private_ml_model_store,
        harden_web_program_permissions,
        refresh_watchdog_guard_script,
    )
    if not ensure_private_ml_model_store():
        raise RuntimeError("Privater ML-Modellspeicher konnte nicht sicher vorbereitet werden")
    if not harden_web_program_permissions():
        raise RuntimeError("Web-Programmrechte konnten nicht gehärtet werden")
    if not _ensure_install_center_core_services():
        raise RuntimeError("Kernservice-Installation ist unvollständig")
    if not migrate_storage_manager_next_override():
        raise RuntimeError("Storage-Service-Migration ist fehlgeschlagen")

    _verify_transition_state(state)
    if not _restart_v4_services(
        headless=headless,
        services=restart_services,
        transition_state=state,
    ):
        raise RuntimeError("Erwartete Dienste konnten nicht vollständig gestartet werden")
    if not refresh_watchdog_guard_script():
        _stop_v4_services(restart_services)
        raise RuntimeError("Watchdog-Guard konnte nach dem finalen Dienststart nicht aktualisiert werden")
    if not _post_update_healthcheck(restart_services, transition_state=state):
        _stop_v4_services(restart_services)
        raise RuntimeError("Dienst-/HTTP-/HA-Gesundheitsgate fehlgeschlagen")

    try:
        from .boot_sanity import check_boot_sanity
        boot_ok = check_boot_sanity(verbose=True)
    except Exception as exc:
        raise RuntimeError(f"Boot-Sanitycheck konnte nicht ausgeführt werden: {exc}") from exc
    if not boot_ok:
        _stop_v4_services(restart_services)
        raise RuntimeError("Boot-Sanity-Gate fehlgeschlagen")

    _verify_transition_state(state)
    if expected_config_state == "present":
        _config, raw = _read_json_nofollow(state.config_path)
        if hashlib.sha256(raw).hexdigest() != expected_config_sha256:
            raise RuntimeError("Betriebskonfiguration driftete während des Target-Finalizers")
    run_initial_forecast(os.path.join(target_root, "Installer"))


def _invoke_target_finalizer(
    *,
    repo_dir: str,
    target_commit: str,
    target_tag: str,
    state: TransitionState,
    package_transaction: PackageTransactionState,
) -> None:
    """Startet den SHA-gebundenen zweiten Prozess mit bereinigtem Importkontext."""

    install_user = get_install_user()
    bound_target_files = {
        relative_path: _bind_target_file_to_commit(
            repo_dir=repo_dir,
            target_commit=target_commit,
            relative_path=relative_path,
            install_user=install_user,
        )
        for relative_path in TARGET_FINALIZER_RELATIVE_FILES
    }
    finalizer = os.path.join(repo_dir, "Installer", "release_finalize.py")
    python = str(sys.executable or "")
    if not os.path.isabs(python) or not os.access(python, os.X_OK):
        raise RuntimeError("Python-Interpreter des Archiv-Runners ist nicht eindeutig ausführbar")

    environment = dict(os.environ)
    for name in (
        "E3DC_BOOTSTRAP_ROOT",
        "E3DC_BOOTSTRAP_RUNNER_ROOT",
        "E3DC_BOOTSTRAP_USER",
        "E3DC_BOOTSTRAP_VENV",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        environment.pop(name, None)
    environment["E3DC_INSTALL_ROOT"] = repo_dir
    environment["PYTHONNOUSERSITE"] = "1"
    environment["LC_ALL"] = "C.UTF-8"
    environment["LANG"] = "C.UTF-8"

    for relative_path, expected_identity in bound_target_files.items():
        current = os.lstat(os.path.join(repo_dir, relative_path))
        if _file_identity(current) != expected_identity:
            raise RuntimeError(f"Target-Modul wurde nach der Commit-Bindung ausgetauscht: {relative_path}")

    config_state = "missing" if state.bootstrap_legacy_config else "present"
    if not package_transaction.pip_requested:
        venv_state = "unused"
        venv_path = ""
    else:
        venv_state = "present" if package_transaction.venv_existed else "missing"
        venv_path = str(package_transaction.venv_path or "")
        if not os.path.isabs(venv_path):
            raise RuntimeError("Paket-Preimage besitzt keinen absoluten venv-Pfad")
    result = _run_argv(
        [
            python,
            finalizer,
            "--install-path", repo_dir,
            "--expected-release-sha", target_commit,
            "--expected-release-tag", target_tag,
            "--expected-ha-role", state.ha_role,
            "--expected-config-state", config_state,
            "--expected-config-sha256", state.config_sha256,
            "--expected-units-sha256", _transition_units_sha256(state.preinstalled_units),
            "--expected-legacy-activity", state.legacy_e3dc_activity,
            "--expected-venv-state", venv_state,
            "--expected-venv-path", venv_path,
        ],
        timeout=900,
        env=environment,
    )
    marker = f"{TARGET_FINALIZER_SUCCESS} {target_commit} {target_tag}"
    lines = [line.strip() for line in result.get("stdout", "").splitlines()]
    if not result.get("success") or lines.count(marker) != 1:
        raise RuntimeError(
            "Target-Finalizer fehlgeschlagen: "
            + _combined_process_diagnostics(result)
        )


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _read_bound_regular_file(path: str, maximum: int = 1024 * 1024) -> tuple[bytes, tuple[int, int, int, int, int]]:
    descriptor, before = _open_regular_file_nofollow(path)
    try:
        if before.st_nlink != 1 or before.st_size < 1 or before.st_size > maximum:
            raise RuntimeError("Target-Datei besitzt unzulässige Metadaten")
        chunks = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _file_identity(after) != _file_identity(before):
            raise RuntimeError("Target-Datei wurde während der Commit-Bindung verändert")
    finally:
        os.close(descriptor)
    return b"".join(chunks), _file_identity(before)


def _read_commit_blob(
    repo_dir: str,
    target_commit: str,
    relative_path: str,
    install_user: str,
    maximum: int = 1024 * 1024,
) -> bytes:
    commit = _validate_full_commit(target_commit)
    if relative_path.startswith("/") or ".." in Path(relative_path).parts:
        raise RuntimeError("Target-Blobpfad ist nicht relativ und kanonisch")
    size_result = _git_argv(
        repo_dir,
        install_user,
        "cat-file",
        "-s",
        f"{commit}:{relative_path}",
        timeout=15,
    )
    try:
        expected_size = int(size_result["stdout"].strip()) if size_result["success"] else -1
    except (TypeError, ValueError):
        expected_size = -1
    if expected_size < 1 or expected_size > maximum:
        raise RuntimeError("Target-Blob fehlt oder besitzt eine unzulässige Größe")
    try:
        completed = subprocess.run(
            [
                "sudo", "-H", "-u", str(install_user),
                "git", "-C", str(repo_dir),
                "cat-file", "blob", f"{commit}:{relative_path}",
            ],
            capture_output=True,
            text=False,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Target-Blob konnte nicht gelesen werden") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError("Target-Blob fehlt im freigegebenen Commit: " + detail[-500:])
    payload = bytes(completed.stdout or b"")
    if len(payload) != expected_size:
        raise RuntimeError("Target-Blobgröße driftete während des Lesens")
    return payload


def _read_commit_file_mode(
    repo_dir: str,
    target_commit: str,
    relative_path: str,
    install_user: str,
) -> int:
    """Liest den freigegebenen Git-Dateimodus ohne locale-abhängige Textumwandlung."""
    commit = _validate_full_commit(target_commit)
    if relative_path.startswith("/") or ".." in Path(relative_path).parts:
        raise RuntimeError("Target-Moduspfad ist nicht relativ und kanonisch")
    try:
        completed = subprocess.run(
            [
                "sudo", "-H", "-u", str(install_user),
                "git", "-C", str(repo_dir),
                "ls-tree", "-z", commit, "--", relative_path,
            ],
            capture_output=True,
            text=False,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Target-Dateimodus konnte nicht gelesen werden") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError("Target-Dateimodus fehlt im freigegebenen Commit: " + detail[-500:])
    records = [record for record in bytes(completed.stdout or b"").split(b"\0") if record]
    if len(records) != 1:
        raise RuntimeError("Target-Dateimodus ist nicht eindeutig")
    try:
        header, raw_path = records[0].split(b"\t", 1)
        raw_mode, object_type, _object_id = header.split(b" ", 2)
        parsed_path = raw_path.decode("utf-8")
        mode_text = raw_mode.decode("ascii")
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("Target-Dateimodus besitzt ein ungültiges Git-Format") from exc
    if parsed_path != relative_path or object_type != b"blob" or mode_text not in {"100644", "100755"}:
        raise RuntimeError("Target-Dateimodus ist nicht als reguläre Produktdatei freigegeben")
    return 0o755 if mode_text == "100755" else 0o644


def _read_descriptor_bytes(descriptor: int, maximum: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise RuntimeError("Target-Datei ist größer als der freigegebene Blob")
    return b"".join(chunks)


def _normalize_target_finalizer_files(
    *,
    repo_dir: str,
    target_commit: str,
    install_user: str,
) -> None:
    """Normalisiert nur bytegenau gebundene Finalizer-Dateien über offene FDs."""
    try:
        account = pwd.getpwnam(str(install_user))
    except KeyError as exc:
        raise RuntimeError("Installationsbenutzer für Target-Normalisierung fehlt") from exc
    root = os.path.abspath(repo_dir)
    for relative_path in TARGET_FINALIZER_RELATIVE_FILES:
        target = os.path.abspath(os.path.join(root, relative_path))
        if os.path.commonpath((root, target)) != root:
            raise RuntimeError("Target-Normalisierung verlässt das Installationsverzeichnis")
        expected = _read_commit_blob(
            repo_dir,
            target_commit,
            relative_path,
            install_user,
        )
        expected_mode = _read_commit_file_mode(
            repo_dir,
            target_commit,
            relative_path,
            install_user,
        )
        descriptor, before = _open_regular_file_nofollow(target)
        try:
            if before.st_nlink != 1:
                raise RuntimeError("Target-Datei besitzt mehrere Hardlinks")
            if before.st_uid not in (0, account.pw_uid):
                raise RuntimeError("Target-Datei besitzt einen nicht vertrauenswürdigen Eigentümer")
            if before.st_size != len(expected):
                raise RuntimeError("Target-Dateigröße stimmt nicht mit dem freigegebenen Commit überein")
            if _read_descriptor_bytes(descriptor, len(expected)) != expected:
                raise RuntimeError("Target-Datei stimmt nicht bytegenau mit dem freigegebenen Commit überein")
            stable_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != stable_before:
                raise RuntimeError("Target-Datei driftete vor der Metadaten-Normalisierung")
            os.fchown(descriptor, account.pw_uid, account.pw_gid)
            os.fchmod(descriptor, expected_mode)
            os.fsync(descriptor)
            after = os.fstat(descriptor)
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or after.st_uid != account.pw_uid
                or after.st_gid != account.pw_gid
                or stat.S_IMODE(after.st_mode) != expected_mode
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != stable_before
            ):
                raise RuntimeError("Target-Metadaten konnten nicht exakt normalisiert werden")
            if _read_descriptor_bytes(descriptor, len(expected)) != expected:
                raise RuntimeError("Target-Datei driftete während der Metadaten-Normalisierung")
        finally:
            os.close(descriptor)
        current_path = os.lstat(target)
        if (
            current_path.st_dev != before.st_dev
            or current_path.st_ino != before.st_ino
            or current_path.st_uid != account.pw_uid
            or current_path.st_gid != account.pw_gid
            or stat.S_IMODE(current_path.st_mode) != expected_mode
        ):
            raise RuntimeError("Target-Datei wurde nach der Metadaten-Normalisierung ausgetauscht")


def _bind_target_file_to_commit(
    *,
    repo_dir: str,
    target_commit: str,
    relative_path: str,
    install_user: str,
) -> tuple[int, int, int, int, int]:
    target = os.path.join(os.path.abspath(repo_dir), relative_path)
    payload, identity = _read_bound_regular_file(target)
    metadata = os.lstat(target)
    try:
        account = pwd.getpwnam(str(install_user))
    except KeyError as exc:
        raise RuntimeError("Installationsbenutzer für Target-Bindung fehlt") from exc
    if metadata.st_uid not in (0, account.pw_uid) or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError("Target-Datei besitzt keine vertrauenswürdigen Eigentümer-/Schreibrechte")
    expected = _read_commit_blob(
        repo_dir,
        target_commit,
        relative_path,
        install_user,
    )
    if payload != expected:
        raise RuntimeError("Target-Datei stimmt nicht bytegenau mit dem freigegebenen Commit überein")
    if _file_identity(metadata) != identity:
        raise RuntimeError("Target-Datei driftete nach der Commit-Bindung")
    return identity


def _recover_failed_transition(
    *,
    repo_dir: str,
    install_user: str,
    backup_dir: str,
    old_commit: str | None,
    git_created: bool,
    inventory: frozenset[str],
    recovery_inventory: RecoverySurfaceInventory,
    state: TransitionState,
    package_transaction: PackageTransactionState | None = None,
) -> bool:
    """Restore old Git/tree/persistent state and verify role/services after any mutation failure."""
    recovery_ok = True
    if not _stop_v4_services(V4_SERVICES):
        update_logger.error("Recovery abgebrochen: Aktor-/Writer-Ruhe ist nicht beweisbar")
        return False
    if old_commit and not git_created:
        reset = _git_argv(repo_dir, install_user, "reset", "--hard", old_commit, timeout=120)
        recovery_ok = recovery_ok and reset["success"]
    try:
        top_exclusions, anywhere_exclusions = _install_tree_exclusions()
        _remove_entries_not_in_inventory(
            repo_dir,
            inventory,
            remove_git=git_created,
            excluded_top=top_exclusions,
            excluded_anywhere=anywhere_exclusions,
        )
        restore_verified_backup(backup_dir, install_path=repo_dir)
        _restore_recovery_surface(recovery_inventory, state)
        # Recovery installiert keine Units aus dem temporären Archivbaum. Die
        # verifizierte Sicherung stellt die alten Unitdateien wieder her; hier
        # werden ausschließlich Web- und Repo-Rechte am Zielbaum gehärtet.
        if not _fix_webroot_permissions():
            raise RuntimeError("Web-Programmrechte konnten nach Recovery nicht gehärtet werden")
        _secure_repo_permissions(repo_dir, install_user)
        _verify_transition_state(
            state,
            expect_legacy_config_missing=state.bootstrap_legacy_config,
        )
    except Exception as exc:
        update_logger.error(f"Automatische Wiederherstellung fehlgeschlagen: {exc}")
        recovery_ok = False
    if package_transaction is not None:
        try:
            _restore_package_transaction(package_transaction)
        except Exception as exc:
            update_logger.error(f"Paket-Ruecklauf fehlgeschlagen: {exc}")
            recovery_ok = False
    restart_names = [unit.removesuffix(".service") for unit in state.preinstalled_units if unit != "e3dc.service"]
    if recovery_ok:
        recovery_ok = _restart_v4_services(
            services=restart_names,
            transition_state=state,
            legacy_recovery=state.bootstrap_legacy_config,
        )
    if recovery_ok and state.bootstrap_legacy_config:
        recovery_ok = _restore_legacy_runtime_state(state)
    if recovery_ok:
        recovery_ok = _post_update_healthcheck(
            restart_names,
            transition_state=state,
            legacy_recovery=state.bootstrap_legacy_config,
        )
    return recovery_ok


def update_e3dc(
    headless: bool = False,
    target_ref: str | None = None,
    target_install_path: str | None = None,
    expected_release_sha: str | None = None,
    expected_ha_role: str | None = None,
):
    """Transactional stable update, SHA-bound rollback, or unrelated-history bootstrap."""
    if _is_docker_environment():
        print("[i] Docker-Umgebung erkannt: Im Container wird kein Release-Wechsel ausgeführt.")
        print("    Bitte auf dem Docker-Host im Compose-Verzeichnis ausführen:")
        print("    docker compose pull")
        print("    docker compose up -d --force-recreate")
        return True

    try:
        target_tag = _normalize_release_tag(target_ref) if target_ref else None
        expected_sha = _validate_full_commit(expected_release_sha) if expected_release_sha else None
    except ValueError as exc:
        print(f"[!] {exc}")
        return False

    if target_install_path and not target_tag:
        print("[!] Bootstrap verlangt einen expliziten Release-Tag.")
        return False
    if target_install_path and (not expected_sha or not expected_ha_role):
        print("[!] Bootstrap verlangt --expected-release-sha und --expected-ha-role.")
        return False
    transition_name = "release-bootstrap" if target_install_path else ("release-rollback" if target_tag else "self-update")
    if not sys.stdout.isatty():
        headless = True

    try:
        repo_dir = (
            _validate_bootstrap_install_path(target_install_path)
            if target_install_path
            else os.path.dirname(INSTALLER_DIR)
        )
        bootstrap_without_git = not os.path.isdir(os.path.join(repo_dir, ".git"))
        if bootstrap_without_git and not target_install_path:
            raise RuntimeError("Installation ohne Git darf nur ueber den expliziten Bootstrapweg migriert werden")
        old_commit = None if bootstrap_without_git else get_current_commit(repo_dir)
        if not bootstrap_without_git and not old_commit:
            raise RuntimeError("Aktueller HEAD konnte nicht als volle Commit-SHA verifiziert werden")
        state = _capture_transition_state(
            expected_role=expected_ha_role,
            allow_missing_config=bool(target_install_path and bootstrap_without_git),
        )
        inventory = _capture_install_inventory(repo_dir)
        recovery_inventory = _capture_recovery_surface(state)
    except Exception as exc:
        print(f"[!] Release-Preflight fehlgeschlagen: {exc}")
        update_logger.error(f"Release-Preflight fehlgeschlagen: {exc}")
        return False

    print("\n" + "=" * 60)
    print("  E3DC-CONTROL " + transition_name.upper())
    print("=" * 60)
    print(f"    Repository       : {repo_dir}")
    print(f"    Ausgangs-SHA     : {old_commit or 'ZIP/V3'}")
    print(f"    Eingefrorene Rolle: {state.ha_role}")
    if target_tag:
        print(f"    Ziel-Release     : {target_tag}")
    if expected_sha:
        print(f"    Erwartete SHA    : {expected_sha}")

    if not headless:
        answer = input("\nGeprueften Release-Wechsel jetzt starten? (j/n): ").strip().lower()
        if answer != "j":
            print("[i] Release-Wechsel abgebrochen.")
            return True

    _enable_watchdog_update_pause(transition_name)
    print("\n[->] Erstelle vollstaendiges externes, verifiziertes Backup...")
    try:
        backup_dir = backup_current_version(install_path=repo_dir)
    except Exception as exc:
        backup_dir = None
        update_logger.error(f"Backup vor Release-Wechsel fehlgeschlagen: {exc}")
    if not backup_dir:
        print("[!] Backup fehlgeschlagen; Release-Wechsel hart abgebrochen.")
        _set_watchdog_update_pause(False, reason=transition_name)
        return False

    # Dieser exakte Aufruf bleibt als statisch pruefbarer Aktorruhevertrag erhalten.
    if not _stop_v4_services(V4_SERVICES):
        print("[!] Sichere Aktorruhe konnte nicht nachgewiesen werden.")
        _set_watchdog_update_pause(False, reason=transition_name)
        return False

    install_user = get_install_user()
    git_created = False
    mutated = False
    target_commit = None
    package_transaction = None
    packages_mutated = False
    try:
        cleanup_pycache(repo_dir)
        if bootstrap_without_git:
            init = _run_argv(
                ["sudo", "-H", "-u", str(install_user), "git", "-C", repo_dir, "init"],
                timeout=30,
            )
            if not init["success"]:
                raise RuntimeError("Git-Init fehlgeschlagen: " + init["stderr"].strip())
            git_created = True
            mutated = True
            remote_add = _git_argv(repo_dir, install_user, "remote", "add", "origin", SELFUPDATE_REPO, timeout=15)
            if not remote_add["success"]:
                raise RuntimeError("Git-Origin konnte nicht gesetzt werden: " + remote_add["stderr"].strip())

        _prepare_git_repository(repo_dir, install_user=install_user, include_root=True)
        remote = _git_argv(repo_dir, install_user, "remote", "get-url", "origin", timeout=15)
        if not remote["success"] or remote["stdout"].strip() != SELFUPDATE_REPO:
            raise RuntimeError("Git-Origin weicht vom fest freigegebenen Release-Repository ab")

        mutated = True
        target_commit = _fetch_target_commit(repo_dir, install_user, target_tag)
        if expected_sha and not _exact_commit_matches(expected_sha, target_commit):
            raise RuntimeError(
                f"Ziel-SHA weicht von der expliziten Freigabe ab: {target_commit} != {expected_sha}"
            )

        runner_repo = os.path.dirname(INSTALLER_DIR)
        if target_tag and not _target_tag_authorized(
            target_tag,
            policy_repo=repo_dir,
            target_commit=target_commit,
            expected_release_sha=expected_sha,
            install_user=install_user,
            bootstrap_runner_repo=runner_repo if target_install_path else None,
        ):
            raise RuntimeError("Release-Tag ist nicht durch exakte Policy-/SHA-Bindung autorisiert")

        policy = _read_policy_from_commit(repo_dir, target_commit, install_user)
        bound_target_tag = _validate_target_release(
            policy,
            repo_dir,
            target_commit,
            target_tag,
            install_user,
        )
        _validated_restart_services(policy, state)
        package_transaction = _capture_package_transaction(
            policy,
            install_user,
            # Auch ein normaler Self-Update darf einen wirklich fehlenden,
            # policygebundenen Benutzer-venv erstmals erzeugen. Capture
            # erlaubt dies nur bei freigegebenem python3-venv und exakt
            # absentem kanonischem Zielpfad; jeder belegte Fremdpfad stoppt.
            allow_missing_venv=True,
        )

        reset = _git_argv(repo_dir, install_user, "reset", "--hard", target_commit, timeout=120)
        if not reset["success"]:
            raise RuntimeError("git reset --hard fehlgeschlagen: " + reset["stderr"].strip())
        new_commit = _resolve_git_commit(repo_dir, "HEAD", install_user)
        if not new_commit or not _exact_commit_matches(target_commit, new_commit):
            raise RuntimeError("HEAD stimmt nicht exakt mit dem freigegebenen Ziel-SHA ueberein")

        _normalize_target_finalizer_files(
            repo_dir=repo_dir,
            target_commit=target_commit,
            install_user=install_user,
        )

        packages_mutated = True
        _invoke_target_finalizer(
            repo_dir=repo_dir,
            target_commit=target_commit,
            target_tag=bound_target_tag,
            state=state,
            package_transaction=package_transaction,
        )
    except Exception as exc:
        print(f"[!] {transition_name} fehlgeschlagen: {exc}")
        update_logger.error(f"{transition_name} fehlgeschlagen: {exc}")
        recovered = False
        if mutated:
            recovered = _recover_failed_transition(
                repo_dir=repo_dir,
                install_user=install_user,
                backup_dir=backup_dir,
                old_commit=old_commit,
                git_created=git_created,
                inventory=inventory,
                recovery_inventory=recovery_inventory,
                state=state,
                package_transaction=package_transaction if packages_mutated else None,
            )
        else:
            prior_services = [
                unit.removesuffix(".service")
                for unit in state.preinstalled_units
                if unit != "e3dc.service"
            ]
            recovered = _restart_v4_services(services=prior_services, transition_state=state)
            if recovered:
                recovered = _post_update_healthcheck(prior_services, transition_state=state)
        if recovered:
            print("[OK] Ausgangszustand wurde automatisch und verifiziert wiederhergestellt.")
            _set_watchdog_update_pause(False, reason=transition_name)
        else:
            print("[!] Automatische Wiederherstellung nicht beweisbar; Writer bleiben gestoppt.")
            try:
                atexit.unregister(_clear_watchdog_update_pause)
            except Exception:
                pass
        return False

    _set_watchdog_update_pause(False, reason=transition_name)
    try:
        atexit.unregister(_clear_watchdog_update_pause)
    except Exception:
        pass
    print(f"\n[OK] {transition_name} auf {target_commit} abgeschlossen.")
    update_logger.info(f"E3DC-Control {transition_name} abgeschlossen: {old_commit} -> {target_commit}")
    log_task_completed(
        "E3DC-Control " + transition_name,
        details=f"{old_commit or 'ZIP/V3'} -> {target_commit}",
    )
    return True


def _find_venv_python(install_user: str | None = None) -> str | None:
    """Liefert nur einen nachweislich aktiven Interpreter des Benutzer-venv."""
    try:
        user = str(install_user or get_install_user()).strip()
        account = pwd.getpwnam(user)
        raw_home = Path(account.pw_dir)
        if not raw_home.is_absolute() or raw_home.is_symlink():
            return None
        home = raw_home.resolve(strict=True)
        if home != raw_home or not venv_metadata_is_trusted(
            home,
            account,
            kind="directory",
        ):
            return None
        raw_venv = Path(get_venv_path(user))
        if not raw_venv.is_absolute() or raw_venv.is_symlink() or not raw_venv.is_dir():
            return None
        venv = raw_venv.resolve(strict=True)
        if venv != raw_venv:
            return None
        if os.path.commonpath((str(home), str(venv))) != str(home):
            return None
        if not venv_directory_chain_is_trusted(home, venv, account):
            return None

        marker = venv / "pyvenv.cfg"
        if not venv_metadata_is_trusted(
            marker,
            account,
            kind="regular",
            single_link=True,
        ):
            return None

        bin_dir = venv / "bin"
        if not venv_directory_chain_is_trusted(venv, bin_dir, account):
            return None

        candidate = bin_dir / "python3"
        link_info = candidate.lstat()
        target = candidate.resolve(strict=True)
        if not (stat.S_ISLNK(link_info.st_mode) or stat.S_ISREG(link_info.st_mode)):
            return None
        if link_info.st_uid not in (0, account.pw_uid):
            return None
        if not venv_metadata_is_trusted(target, account, kind="regular"):
            return None
        if not os.access(candidate, os.X_OK):
            return None

        probe = _run_argv(
            [
                "sudo", "-H", "-u", user,
                str(candidate),
                "-c",
                (
                    "import json,os,sys,sysconfig; import pip; "
                    "print(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
                    "'executable':sys.executable,'purelib':sysconfig.get_paths()['purelib'],"
                    "'purelib_writable':os.access(sysconfig.get_paths()['purelib'],os.W_OK),"
                    "'bin_writable':os.access(os.path.dirname(sys.executable),os.W_OK)},sort_keys=True))"
                ),
            ],
            timeout=15,
        )
        if not probe["success"]:
            return None
        payload = json.loads(probe["stdout"].strip())
        if os.path.realpath(str(payload.get("prefix") or "")) != str(venv):
            return None
        if os.path.realpath(str(payload.get("base_prefix") or "")) == str(venv):
            return None
        if os.path.abspath(str(payload.get("executable") or "")) != str(candidate):
            return None
        raw_purelib = Path(str(payload.get("purelib") or ""))
        if not raw_purelib.is_absolute() or raw_purelib.is_symlink():
            return None
        purelib = raw_purelib.resolve(strict=True)
        if purelib != raw_purelib or os.path.commonpath((str(venv), str(purelib))) != str(venv):
            return None
        if not venv_directory_chain_is_trusted(venv, purelib, account):
            return None
        if payload.get("purelib_writable") is not True or payload.get("bin_writable") is not True:
            return None
        return str(candidate)
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError):
        return None


def _trusted_system_python() -> str:
    """Liefert ausschließlich den festen, root-kontrollierten Systeminterpreter."""
    candidate = Path("/usr/bin/python3")
    try:
        link_info = candidate.lstat()
        target = candidate.resolve(strict=True)
        target_info = target.stat()
    except OSError as exc:
        raise RuntimeError("Fester System-Python ist nicht verfügbar") from exc
    if not (stat.S_ISLNK(link_info.st_mode) or stat.S_ISREG(link_info.st_mode)):
        raise RuntimeError("Fester System-Python ist weder Link noch reguläre Datei")
    if link_info.st_uid != 0 or link_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError("System-Python-Pfad besitzt unsichere Metadaten")
    if (
        not stat.S_ISREG(target_info.st_mode)
        or target_info.st_uid != 0
        or target_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not os.access(candidate, os.X_OK)
    ):
        raise RuntimeError("System-Python-Ziel besitzt unsichere Metadaten")
    return str(candidate)


def select_wrapper_python(action: str) -> str:
    """Wählt für Wrapper-Aktionen deterministisch venv oder sicheren Bootstrap-Python."""
    normalized = str(action or "").strip().lower()
    if normalized not in {"check", "fix_permissions", "update_e3dc", "install_release"}:
        raise RuntimeError("Wrapper-Python darf für diese Aktion nicht gewählt werden")
    install_user = get_install_user()
    venv_python = _find_venv_python(install_user)
    if venv_python:
        return venv_python
    if normalized in {"update_e3dc", "install_release"}:
        if _is_docker_environment():
            # Im Container führt update_e3dc keinen Release-Wechsel aus,
            # sondern gibt ausschließlich den Host-Compose-Hinweis aus.
            return _trusted_system_python()
        raise RuntimeError(
            "Release-Wechsel benötigt einen vorhandenen und vertrauenswürdigen Python-venv"
        )
    return _trusted_system_python()


def run_initial_forecast(installer_dir: str | None = None):
    """
    Fuehrt nach Installation/Update einen einmaligen Sofort-Forecast durch,
    damit pv_forecast.json und ml_prediction.json sofort verfuegbar sind.

    Ohne diesen Schritt wuerde der weather-manager erst nach 60 Minuten
    seinen ersten API-Call machen (normales Daemon-Interval).

    Ablauf:
      1. pv_forecast_service.py: Holt aktuelle PV-Prognose von Forecast.Solar
         und Open-Meteo und schreibt pv_forecast.json + weather_forecast.json.
      2. ml_predictor.py --model-ready/--train/--predict: Akzeptiert nur den
         privaten Manifest-/Hashvertrag. Fehlt er, wird aus nicht ausfuehrbaren
         lokalen Trainingsdaten neu trainiert; sonst bleibt der Fallback aktiv.

    NICHT ausgeführt:
      - Ein Legacy-Pickle wird niemals geladen oder übernommen.
    """
    # In Docker: kein direkter Script-Aufruf noetig (Service startet selbst)
    if _is_docker_environment():
        print('[i] Docker: Forecast-Init wird vom Container-Daemon uebernommen.')
        return

    # This transition consumer deliberately follows the already running
    # trusted interpreter and does not depend on the broad runtime-context API.
    python = sys.executable if os.path.isabs(sys.executable or "") and os.access(sys.executable, os.X_OK) else 'python3'
    installer_dir = installer_dir or INSTALLER_DIR
    forecast_script = os.path.join(installer_dir, 'Forecast', 'pv_forecast_service.py')
    ml_script       = os.path.join(installer_dir, 'ml_predictor.py')
    forecast_output = '/var/www/html/ramdisk/pv_forecast.json'
    legacy_ml_model = '/var/www/html/data/ml_model.pkl'

    print('\n[-] Initiale PV-Prognose wird abgerufen (einmaliger Sofort-Fetch)...')
    print('    (Normaler Daemon-Zyklus: 60 Min -- dieser Schritt macht pv_forecast.json')
    print('     sofort verfuegbar ohne Wartezeit)\n')

    # Schritt 1: PV-Prognose
    if os.path.exists(forecast_script):
        try:
            result = subprocess.run(
                [python, forecast_script, '--once'],  # --once: kein Daemon, endet nach 1 Fetch
                timeout=120,       # API-Calls koennen bei Lastspitzen dauern
                capture_output=False,
                text=True,
            )
            if result.returncode == 0 and os.path.exists(forecast_output):
                import json as _j
                with open(forecast_output) as _f:
                    _slots = len(_j.load(_f))
                print(f'[OK] pv_forecast.json erstellt ({_slots} Slots).')
            else:
                print(f'[!] pv_forecast_service.py Fehler (Code {result.returncode}).')
                print('    -> Prognose erscheint beim naechsten Daemon-Zyklus.')
        except subprocess.TimeoutExpired:
            print('[!] Forecast-Timeout (>120s) -- API evtl. nicht erreichbar.')
        except Exception as _e:
            print(f'[!] Forecast-Init Fehler: {_e}')
    else:
        print(f'[!] Forecast-Script nicht gefunden: {forecast_script}')

    # Schritt 2: Nur der private Manifest-/Hashvertrag entscheidet ueber ML.
    # Das alte Web-Pickle ist ausschliesslich ein Neutrainingssignal und wird
    # weder geoeffnet noch kopiert oder als Modellbereitschaft akzeptiert.
    if os.path.exists(ml_script):
        try:
            ready = subprocess.run(
                [python, ml_script, '--model-ready'],
                timeout=30,
                capture_output=True,
                text=True,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired) as _e:
            ready = False
            print(f'[!] ML-Bereitschaft konnte nicht geprueft werden: {_e}')

        if not ready:
            if os.path.lexists(legacy_ml_model):
                print('\n[i] Legacy-ML-Modell erkannt; es wird niemals geladen oder uebernommen.')
                print('    -> Sicheres Neutraining erfolgt nur aus SQLite-/JSON-/Text-Trainingsdaten.')
            try:
                train_result = subprocess.run(
                    [python, ml_script, '--train'],
                    timeout=180,
                    capture_output=False,
                    text=True,
                )
                ready = train_result.returncode == 0
            except subprocess.TimeoutExpired:
                print('[!] ML-Neutraining Timeout; konservativer Fallback bleibt aktiv.')
            except Exception as _e:
                print(f'[!] ML-Neutraining fehlgeschlagen: {_e}')

        print('\n[-] ML-Vorhersage wird sicher geprueft/berechnet (ml_predictor --predict)...')
        try:
            result = subprocess.run(
                [python, ml_script, '--predict'],
                timeout=60,
                capture_output=False,
                text=True,
            )
            if result.returncode == 0:
                print('[OK] ml_prediction.json erstellt.')
            elif ready:
                print(f'[!] ML-Predict Fehler (Code {result.returncode}); konservativer Fallback aktiv.')
            else:
                print('[i] Noch kein sicheres ML-Modell; konservativer Fallback aktiv.')
        except subprocess.TimeoutExpired:
            print('[!] ML-Predict Timeout; konservativer Fallback bleibt aktiv.')
        except Exception as _e:
            print(f'[!] ML-Predict Fehler; konservativer Fallback bleibt aktiv: {_e}')
    else:
        print(f'[i] ml_predictor.py nicht gefunden: {ml_script}')

    print()


def update_menu():
    update_e3dc()


# Im Docker-Container kein Update-Menueintrag
if not _is_docker_environment():
    register_command('11', 'Installation / Update', update_menu, sort_order=11)
