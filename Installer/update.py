import ast
import errno
import ipaddress
import os
import sys
import json
import subprocess
import shutil
import time
import io
import shlex
import fcntl
import re
import urllib.error
import urllib.request
import hashlib
import math
import pwd
import grp
import queue
import secrets
import signal
import stat
import tarfile
import tempfile
import threading
from collections import Counter, deque
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
from .backup_integrity import (
    DEFAULT_BACKUP_ROOT,
    MANIFEST_NAME,
    SYSTEM_BACKUP_KIND,
    _open_directory_nofollow,
    _open_regular_file_nofollow,
    configured_backup_root,
    ensure_external_backup_root,
    validate_existing_backup_root,
    verify_backup,
)
from .utils import (
    StorageUnitMigrationError,
    _approved_storage_manager_unit_payloads,
    _migrate_approved_storage_manager_unit_owner,
    capture_systemd_service_bundle,
    cleanup_pycache,
    ensure_manager_lock_namespace,
    require_bound_venv_runtime,
    run_command,
)
from .installer_config import (
    WEB_CONFIG_START_DEFAULTS,
    ensure_web_config,
    get_install_path,
    get_install_user,
    get_venv_path,
    load_config,
)
from .transition_context import (
    get_transition_context,
    venv_directory_chain_is_trusted,
    venv_group_is_private,
    venv_has_extended_acl,
    venv_metadata_is_trusted,
)
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning
from .config_secret_permissions import config_secret_dir_mode_text, config_secret_file_mode_text
from .git_commit_reader import (
    isolated_git_command,
    isolated_git_environment_assignments,
    read_commit_entries,
    run_isolated_remote_git,
)
try:
    from .service_catalog import allowed_services, get_module_by_service
    from .optional_service_contract import preinstalled_optional_service_expected
except ImportError:  # pragma: no cover - direct script execution fallback
    from service_catalog import allowed_services, get_module_by_service
    from optional_service_contract import preinstalled_optional_service_expected

INSTALL_PATH   = get_install_path()
INSTALLER_DIR  = os.path.dirname(os.path.abspath(__file__))
UPDATE_POLICY  = os.path.join(INSTALL_PATH, 'UPDATE_POLICY.json')
update_logger  = get_or_create_logger('update')

# Self-Update: Unser Repo (Native Python + PHP)
SELFUPDATE_REPO = 'https://github.com/A9xxx/Install-E3DC-Control.git'
SELFUPDATE_ALLOWED_ORIGINS = frozenset((SELFUPDATE_REPO, SELFUPDATE_REPO.removesuffix('.git')))
WATCHDOG_PAUSE_FILE = '/var/www/html/ramdisk/watchdog.update_pause'
WATCHDOG_GRACE_FILE = '/var/www/html/ramdisk/watchdog.update_grace'
WATCHDOG_POST_UPDATE_GRACE_S = 300
RECOVERY_BOOTBLOCK_STATE_DIR = "/var/lib/e3dc-update-safety"
RECOVERY_BOOTBLOCK_MARKER = os.path.join(
    RECOVERY_BOOTBLOCK_STATE_DIR,
    "recovery.block",
)
RECOVERY_BOOTBLOCK_DROPIN_NAME = "00-e3dc-recovery-bootblock.conf"
RECOVERY_BOOTBLOCK_DROPIN_PAYLOAD = (
    "# E3DC_RECOVERY_BOOTBLOCK_V1\n"
    "[Unit]\n"
    f"ConditionPathExists=!{RECOVERY_BOOTBLOCK_MARKER}\n"
).encode("utf-8")
RECOVERY_BOOTBLOCK_TRANSACTION_RE = re.compile(r"[0-9a-f]{64}\Z")
UPDATE_SAFETY_RECEIPT_SCHEMA = "e3dc_update_safety_v1"
UPDATE_SAFETY_RECEIPT_NAME = "transaction.json"
UPDATE_FINALIZER_UNIT_PREFIX = "e3dc-update-finalizer-"
UPDATE_FINALIZER_RUNTIME_SUFFIX = "-runtime"
UPDATE_FINALIZER_TOKEN_NAME = "start.token"
UPDATE_FINALIZER_RUNTIME_MAX_S = 35 * 60
UPDATE_FINALIZER_TIMEOUT_STOP_S = 15
UPDATE_FINALIZER_TERMINAL_STABLE_READS = 3
UPDATE_FINALIZER_TERMINAL_STABLE_INTERVAL_S = 0.2

FULL_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
PACKAGE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\Z")
LOCAL_HEALTH_URLS = (
    "http://127.0.0.1/index.php",
    "http://127.0.0.1/help.php",
)
APACHE_SECURITY_CONF_AVAILABLE = (
    "/etc/apache2/conf-available/e3dc-control-security.conf"
)
APACHE_SECURITY_CONF_ENABLED = (
    "/etc/apache2/conf-enabled/e3dc-control-security.conf"
)
ROOT_RECOVERY_FILE_CONTRACTS = (
    ("/usr/local/sbin/e3dc-service-control", 0o755, 64 * 1024),
    ("/usr/local/sbin/e3dc-web-update-launcher", 0o755, 64 * 1024),
    ("/etc/sudoers.d/020_e3dc_services", 0o440, 256 * 1024),
    ("/etc/apache2/sites-available/000-default.conf", 0o644, 256 * 1024),
    (
        "/etc/apache2/conf-available/e3dc-control-access-log.conf",
        0o644,
        64 * 1024,
    ),
    ("/etc/logrotate.d/e3dc-control", 0o644, 64 * 1024),
)
LOGROTATE_CONFIG_PATH = "/etc/logrotate.d/e3dc-control"
LOGROTATE_SOURCE_RELATIVE_PATH = "Installer/logrotate/e3dc-control"

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
TARGET_UPDATER_SUCCESS = "E3DC_RELEASE_TARGET_UPDATER_OK"
TARGET_UPDATER_NOOP = "E3DC_RELEASE_TARGET_UPDATER_NOOP"
TARGET_FINALIZER_RELATIVE_FILES = (
    "Installer/__init__.py",
    "Installer/git_commit_reader.py",
    "Installer/optional_service_contract.py",
    "Installer/release_finalize.py",
    "Installer/update.py",
)
TARGET_EXECUTION_SNAPSHOT_ROOT_FILES = (
    "VERSION",
    "installer_main.py",
)
TARGET_EXECUTION_SNAPSHOT_PARENT = "/run"
TARGET_EXECUTION_SNAPSHOT_MAX_FILES = 4096
TARGET_EXECUTION_SNAPSHOT_MAX_FILE_BYTES = 8 * 1024 * 1024
TARGET_EXECUTION_SNAPSHOT_MAX_TOTAL_BYTES = 128 * 1024 * 1024
TARGET_UPDATER_SNAPSHOT_PREFIX = ".e3dc-release-updater-"
TARGET_COMPAT_UPDATER_SNAPSHOT_PREFIX = ".e3dc-release-compat-updater-"
TARGET_FINALIZER_SNAPSHOT_PREFIX = ".e3dc-release-finalizer-"
TARGET_FINALIZER_TIMEOUT_S = 30 * 60
TARGET_PROCESS_HEARTBEAT_S = 30
TARGET_PROCESS_TERMINATE_GRACE_S = 10
TARGET_PROCESS_DIAGNOSTIC_LINES = 512
SYSTEMD_SETTLE_TIMEOUT_S = 30
SYSTEMD_SETTLE_POLL_S = 2
UPDATE_ALREADY_CURRENT = "already_current"
UPDATE_EXTERNAL_ACTION_REQUIRED = "EXTERNAL_ACTION_REQUIRED"
UPDATE_LOCK_PATH = "/run/lock/e3dc-control/update.lock"
UPDATE_LOCK_ENV = "E3DC_UPDATE_LOCK_FD"


class _DeferredParentSignal(BaseException):
    """Signalisiert einen Nutzerabbruch erst nach sicherem Kindprozessende."""

    def __init__(self, signum: int):
        self.signum = int(signum)
        super().__init__(f"Elternprozess erhielt Signal {self.signum}")


class _TerminalSignalGuard:
    """Blockiert Folgesignale, bis ein mutierender Kindprozess beendet ist."""

    def __init__(self):
        self.requested_signum: int | None = None
        self._previous_handlers: dict[int, object] = {}
        self._previous_mask = None
        self._installed = False
        self._armed = False

    def install(self) -> None:
        if (
            os.name != "posix"
            or threading.current_thread() is not threading.main_thread()
        ):
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)
        self._installed = True

    def _handle(self, signum, _frame) -> None:
        if self.requested_signum is not None:
            return
        self.requested_signum = int(signum)
        if self._armed and hasattr(signal, "pthread_sigmask"):
            self._previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                {signal.SIGINT, signal.SIGTERM},
            )

    def arm(self) -> None:
        """Blockiert Folgesignale erst, nachdem das Kind sicher gestartet ist."""

        self._armed = True
        if (
            self.requested_signum is not None
            and self._previous_mask is None
            and hasattr(signal, "pthread_sigmask")
        ):
            self._previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                {signal.SIGINT, signal.SIGTERM},
            )

    def raise_if_requested(self) -> None:
        if self.requested_signum is not None:
            raise _DeferredParentSignal(self.requested_signum)

    def restore(self) -> None:
        if not self._installed:
            return
        self._installed = False
        for signum, previous in self._previous_handlers.items():
            signal.signal(signum, previous)
        if self._previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, self._previous_mask)


class UpdateTransactionBusy(RuntimeError):
    """Ein anderer Release-Wechsel hält bereits den systemweiten Lock."""


def _assert_trusted_update_lock_directory(path: Path) -> None:
    """Erlaubt nur kanonische, root-kontrollierte Lock-Verzeichnisse."""

    directory = Path(path)
    if not directory.is_absolute() or directory.resolve(strict=True) != directory:
        raise RuntimeError("Update-Lock-Verzeichnis ist nicht kanonisch")
    current = Path(directory.anchor)
    for component in directory.parts[1:]:
        current /= component
        metadata = current.lstat()
        shared_lock_root = current == Path("/run/lock")
        shared_lock_root_safe = (
            shared_lock_root
            and metadata.st_uid == 0
            and metadata.st_gid == 0
            and bool(metadata.st_mode & stat.S_ISVTX)
        )
        if (
            current.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or (
                not shared_lock_root_safe
                and (
                    metadata.st_mode & stat.S_IWOTH
                    or metadata.st_mode & stat.S_IWGRP
                )
            )
        ):
            raise RuntimeError(
                f"Update-Lock-Pfad besitzt eine unsichere Komponente: {current}"
            )


def _open_update_lock_directory(*, create: bool) -> int:
    """Öffnet den privaten root:root-0700-Unterbaum unter dem Sticky-Lockroot."""

    shared_root = Path("/run/lock")
    shared_metadata = shared_root.lstat()
    if (
        shared_root.is_symlink()
        or not stat.S_ISDIR(shared_metadata.st_mode)
        or shared_metadata.st_uid != 0
        or shared_metadata.st_gid != 0
        or (
            shared_metadata.st_mode & stat.S_IWOTH
            and not shared_metadata.st_mode & stat.S_ISVTX
        )
    ):
        raise RuntimeError("Systemweites Lock-Verzeichnis ist nicht vertrauenswürdig")
    shared_fd = os.open(
        str(shared_root),
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if create:
            try:
                os.mkdir("e3dc-control", 0o700, dir_fd=shared_fd)
            except FileExistsError:
                pass
        private_fd = os.open(
            "e3dc-control",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=shared_fd,
        )
    finally:
        os.close(shared_fd)
    metadata = os.fstat(private_fd)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        os.close(private_fd)
        raise RuntimeError("Privates Update-Lock-Verzeichnis besitzt unsichere Metadaten")
    return private_fd


def _validate_update_lock_fd(descriptor: int) -> int:
    """Bindet ein offenes Lock-FD an die feste root:root-0600-Datei."""

    fd = int(descriptor)
    if fd < 3:
        raise RuntimeError("Update-Lock-FD ist unzulässig")
    lock_path = Path(UPDATE_LOCK_PATH)
    _assert_trusted_update_lock_directory(lock_path.parent)
    path_metadata = lock_path.lstat()
    fd_metadata = os.fstat(fd)
    if (
        lock_path.is_symlink()
        or not stat.S_ISREG(path_metadata.st_mode)
        or path_metadata.st_nlink != 1
        or path_metadata.st_uid != 0
        or path_metadata.st_gid != 0
        or stat.S_IMODE(path_metadata.st_mode) != 0o600
        or (
            path_metadata.st_dev,
            path_metadata.st_ino,
        )
        != (
            fd_metadata.st_dev,
            fd_metadata.st_ino,
        )
        or not stat.S_ISREG(fd_metadata.st_mode)
        or fd_metadata.st_nlink != 1
        or fd_metadata.st_uid != 0
        or fd_metadata.st_gid != 0
        or stat.S_IMODE(fd_metadata.st_mode) != 0o600
    ):
        raise RuntimeError("Update-Lock besitzt unsichere Datei- oder FD-Metadaten")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise UpdateTransactionBusy(
            "Ein anderer E3DC-Control-Releasewechsel läuft bereits"
        ) from exc
    return fd


def _acquire_or_inherit_update_lock() -> tuple[int, bool]:
    """Übernimmt den geerbten Lock oder erwirbt ihn systemweit nonblocking."""

    inherited = str(os.environ.get(UPDATE_LOCK_ENV) or "").strip()
    if inherited:
        if not inherited.isdecimal():
            raise RuntimeError("Geerbter Update-Lock-FD ist ungültig")
        return _validate_update_lock_fd(int(inherited)), False
    if os.geteuid() != 0:
        raise RuntimeError("Release-Wechsel benötigt Root für den systemweiten Update-Lock")

    lock_path = Path(UPDATE_LOCK_PATH)
    directory_fd = _open_update_lock_directory(create=True)
    try:
        descriptor = os.open(
            lock_path.name,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)
    try:
        return _validate_update_lock_fd(descriptor), True
    except Exception:
        os.close(descriptor)
        raise


def _required_update_lock_fd() -> int:
    """Verlangt in jedem Snapshotprozess den vom Elternprozess gehaltenen Lock."""

    inherited = str(os.environ.get(UPDATE_LOCK_ENV) or "").strip()
    if not inherited or not inherited.isdecimal():
        raise RuntimeError("Versiegelter Updateprozess besitzt keinen geerbten Transaktionslock")
    return _validate_update_lock_fd(int(inherited))


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
    preactive_units: frozenset[str] = frozenset()
    bootstrap_legacy_config: bool = False
    legacy_e3dc_activity: str = "absent"


@dataclass(frozen=True)
class ApacheSecurityPreimage:
    available: bool
    payload: bytes
    uid: int
    gid: int
    mode: int
    enabled: bool
    enabled_target: str
    apache_available: bool
    apache_activity: str


@dataclass(frozen=True)
class RootManagedFilePreimage:
    path: str
    existed: bool
    payload: bytes
    uid: int
    gid: int
    mode: int
    parent_dev: int
    parent_ino: int


@dataclass(frozen=True)
class RecoverySurfaceInventory:
    web_program_entries: frozenset[str]
    watchdog_files: frozenset[str]
    unit_enablement: tuple[tuple[str, str], ...]
    root_managed_files: tuple[RootManagedFilePreimage, ...]
    apache_security: ApacheSecurityPreimage


@dataclass(frozen=True)
class RepoRecoveryContract:
    install_root: str
    install_user: str
    expected_commit: str
    tracked_files: tuple[tuple[str, int, str, str, int, int, int, int], ...]
    dirty_paths: tuple[str, ...]


@dataclass(frozen=True)
class PrivilegedBackupFileReceipt:
    restore_path: str
    category: str
    backup_relative_path: str
    parent_path_chain: tuple[tuple[str, int, int, int, int, int], ...]
    dev: int
    ino: int
    sha256: str
    size: int
    mode: int
    uid: int
    gid: int
    nlink: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class RecoveryBackupReceipt:
    backup_dir: str
    backup_dev: int
    backup_ino: int
    parent_dev: int
    parent_ino: int
    backup_path_chain: tuple[tuple[str, int, int, int, int, int], ...]
    transaction_id: str
    backup_id: str
    manifest_sha256: str
    manifest_semantic_sha256: str
    install_root: str
    expected_commit: str
    tracked_files: tuple[tuple[str, str, int, int, int, int], ...]
    tracked_directories: tuple[tuple[str, int, int, int], ...]
    manifest_files: tuple[tuple[str, str, int, int, int, int, str, str], ...]
    privileged_files: tuple[tuple[str, str, str, int, int, int, int], ...]
    privileged_backup_files: tuple[PrivilegedBackupFileReceipt, ...]


@dataclass(frozen=True)
class RecoveryBootblockContract:
    units: tuple[str, ...]
    created_directories: tuple[str, ...]
    transaction_id: str
    dropin_identities: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True)
class RecoveryBootblockPartialContract:
    """Bewahrt jeden bereits erzeugten eigenen Drop-in-Inode bis zur Vollendung."""

    units: tuple[str, ...]
    created_directories: tuple[str, ...]
    transaction_id: str
    dropin_identities: tuple[tuple[str, int, int], ...]
    allow_missing_directories: bool


class RecoveryBootblockArmError(RuntimeError):
    """Bewahrt den eigenen Inodevertrag, falls ein Fresh-Arm nicht abräumen kann."""

    def __init__(
        self,
        message: str,
        contract: RecoveryBootblockContract | RecoveryBootblockPartialContract,
    ):
        self.contract = contract
        super().__init__(message)


class UpdateSafetyPostCommitError(RuntimeError):
    """Der Zielstand ist committed; ein Altstand-Rollback ist verboten."""


class UpdateSafetyManagedServiceUnquiescedError(RuntimeError):
    """Finalizer-/Receipt-/Writer-Endgate ist unbewiesen; Recovery ist verboten."""


@dataclass(frozen=True)
class UpdateSafetyContract:
    """Persistenter Vertrag des normalen versiegelten Ziel-Updaters."""

    schema: str
    state: str
    transaction_id: str
    target_commit: str
    target_tag: str
    role: str
    backup_dir: str
    backup_dev: int
    backup_ino: int
    backup_id: str
    backup_manifest_sha256: str
    units: tuple[str, ...]
    created_directories: tuple[str, ...]
    dropin_identities: tuple[tuple[str, int, int], ...]
    dropin_payload_sha256: str
    finalizer_unit: str
    runtime_directory: str
    token_path: str
    receipt_path: str
    receipt_dev: int
    receipt_ino: int
    receipt_sha256: str


@dataclass(frozen=True)
class RecoveryTransitionResult:
    recovered: bool
    bootblock_contract: (
        RecoveryBootblockContract | RecoveryBootblockPartialContract | None
    )

    def __bool__(self) -> bool:
        return self.recovered

    def __iter__(self):
        yield self.recovered
        yield self.bootblock_contract


@dataclass(frozen=True)
class RecoveryBootblockEnforcementResult:
    enforced: bool
    bootblock_contract: (
        RecoveryBootblockContract | RecoveryBootblockPartialContract | None
    )

    def __bool__(self) -> bool:
        return self.enforced

    def __iter__(self):
        yield self.enforced
        yield self.bootblock_contract


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
    runtime_venv_required: bool = False


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
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "stdout": "",
            "stderr": str(exc),
            "returncode": -1,
            "timed_out": True,
        }
    except OSError as exc:
        return {
            "success": False,
            "stdout": "",
            "stderr": str(exc),
            "returncode": -1,
            "timed_out": False,
        }
    return {
        "success": completed.returncode == 0,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
        "returncode": completed.returncode,
        "timed_out": False,
    }


def _run_streaming_argv(
    argv,
    *,
    timeout: int | None,
    env=None,
    pass_fds=(),
    stdin_fd: int | None = None,
    heartbeat_s: int = TARGET_PROCESS_HEARTBEAT_S,
    label: str = "Prozess",
) -> dict:
    """Streamt lange Kindprozesse; nur explizit begrenzte Phasen werden beendet."""

    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("argv muss eine nicht-leere Liste sein")
    args = [str(item) for item in argv]
    if any("\x00" in item for item in args):
        raise ValueError("NUL-Byte in argv")
    if timeout is not None and int(timeout) < 1:
        raise ValueError("Zeitlimit muss positiv oder None sein")
    inherited_fds = tuple(int(item) for item in pass_fds)
    if any(item < 3 for item in inherited_fds):
        raise ValueError("Vererbte Dateideskriptoren sind unzulässig")
    if stdin_fd is not None and int(stdin_fd) < 3:
        raise ValueError("stdin-Dateideskriptor ist unzulässig")

    signal_guard = _TerminalSignalGuard()
    signal_guard.install()
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            stdin=(int(stdin_fd) if stdin_fd is not None else None),
            start_new_session=os.name == "posix",
            pass_fds=inherited_fds,
        )
    except OSError as exc:
        signal_guard.restore()
        return {
            "success": False,
            "stdout": "",
            "stderr": str(exc),
            "returncode": -1,
            "stdout_line_counts": {},
            "timed_out": False,
        }
    except BaseException:
        signal_guard.restore()
        raise
    signal_guard.arm()

    events: queue.Queue[tuple[str, str | None]] = queue.Queue()
    tails = {
        "stdout": deque(maxlen=TARGET_PROCESS_DIAGNOSTIC_LINES),
        "stderr": deque(maxlen=TARGET_PROCESS_DIAGNOSTIC_LINES),
    }
    stdout_line_counts: Counter[str] = Counter()

    def _drain(stream_name: str, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                events.put((stream_name, line))
        finally:
            events.put((stream_name, None))
            stream.close()

    threads = [
        threading.Thread(target=_drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=_drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    started = time.monotonic()
    last_heartbeat = started
    completed_streams: set[str] = set()
    timed_out = False
    termination_started = 0.0
    force_sent = False

    def _stop_process_tree(*, force: bool) -> None:
        try:
            if os.name == "posix":
                os.killpg(
                    process.pid,
                    signal.SIGKILL if force else signal.SIGTERM,
                )
            elif process.poll() is not None:
                return
            elif force:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        except OSError:
            if process.poll() is None:
                try:
                    process.kill() if force else process.terminate()
                except ProcessLookupError:
                    pass

    try:
        while len(completed_streams) < 2 or process.poll() is None:
            signal_guard.raise_if_requested()
            now = time.monotonic()
            elapsed = now - started
            if timeout is not None and not timed_out and elapsed >= int(timeout):
                timed_out = True
                termination_started = now
                _stop_process_tree(force=False)
                print(
                    f"[!] {label} überschreitet das Zeitlimit von {int(timeout)} Sekunden; "
                    "der sichere Rückweg wird vorbereitet.",
                    flush=True,
                )
            elif (
                timed_out
                and not force_sent
                and now - termination_started >= TARGET_PROCESS_TERMINATE_GRACE_S
            ):
                _stop_process_tree(force=True)
                force_sent = True

            wait_s = 0.2
            if timeout is not None and not timed_out:
                wait_s = min(wait_s, max(0.01, int(timeout) - elapsed))
            try:
                stream_name, line = events.get(timeout=wait_s)
            except queue.Empty:
                stream_name, line = "", ""

            if stream_name and line is None:
                completed_streams.add(stream_name)
            elif stream_name:
                tails[stream_name].append(line)
                if stream_name == "stdout":
                    stdout_line_counts[line.strip()] += 1
                    target = sys.stdout
                else:
                    target = sys.stderr
                target.write(line)
                target.flush()

            now = time.monotonic()
            if (
                not timed_out
                and process.poll() is None
                and heartbeat_s > 0
                and now - last_heartbeat >= heartbeat_s
            ):
                print(
                    f"[i] {label} läuft weiter "
                    f"({int(now - started)} Sekunden seit Start).",
                    flush=True,
                )
                last_heartbeat = now
    except BaseException:
        # Der äußere Ziel-Updater besitzt Backup und Recovery selbst. Ein
        # abgebrochener Web-/Terminal-Ausgabekanal darf ihn daher bei
        # timeout=None niemals töten. Wir konsumieren weiter still bis zu
        # seinem eindeutigen Ende und geben erst danach die Parent-Exception
        # zurück. Nur ein explizit zeitbegrenzter Kindprozess darf beendet
        # werden; dessen Eltern-Ziel-Updater besitzt anschließend Recovery.
        try:
            if timeout is None:
                while len(completed_streams) < 2 or process.poll() is None:
                    try:
                        stream_name, line = events.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if stream_name and line is None:
                        completed_streams.add(stream_name)
                    elif stream_name:
                        tails[stream_name].append(line)
                        if stream_name == "stdout":
                            stdout_line_counts[line.strip()] += 1
                process.wait()
            else:
                _stop_process_tree(force=False)
                try:
                    process.wait(timeout=TARGET_PROCESS_TERMINATE_GRACE_S)
                except subprocess.TimeoutExpired:
                    _stop_process_tree(force=True)
                    process.wait()
            for thread in threads:
                thread.join(timeout=1)
        finally:
            signal_guard.restore()
        raise

    returncode = process.wait()
    for thread in threads:
        thread.join(timeout=1)
    stderr = "".join(tails["stderr"])
    if timed_out:
        stderr += (
            f"\n{label} wurde nach {int(timeout)} Sekunden "
            "wegen Zeitüberschreitung beendet.\n"
        )
    try:
        signal_guard.raise_if_requested()
        return {
            "success": returncode == 0 and not timed_out,
            "stdout": "".join(tails["stdout"]),
            "stderr": stderr,
            "returncode": returncode,
            "stdout_line_counts": dict(stdout_line_counts),
            "timed_out": timed_out,
        }
    finally:
        signal_guard.restore()


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
    # Der Updater läuft als Root und projiziert anschließend den kanonischen
    # Zielbesitzer. Git wird deshalb im Root-Prozess nicht künstlich auf die
    # möglicherweise durch historische chmod/chown-Zustände ausgesperrte
    # Altidentität abgesenkt. Die isolierte Git-Umgebung deaktiviert weiterhin
    # Hooks, fremde Konfiguration, Credentials, Replace-Refs und unsichere
    # Protokolle. Nicht privilegierte Diagnoseaufrufe bleiben beim gebundenen
    # Installationsbenutzer.
    git_user = None if hasattr(os, "geteuid") and os.geteuid() == 0 else install_user
    return _run_argv(
        isolated_git_command(repo_dir, *args, run_as_user=git_user),
        timeout=timeout,
    )


def _initialize_bootstrap_git(repo_dir: str, install_user: str) -> None:
    """Erzeugt nur die neue Git-Wurzel als gebundener Installationsbenutzer."""

    result = _run_argv(
        [
            "/usr/bin/sudo",
            "-n",
            "-H",
            "-u",
            str(install_user),
            "--",
            "/usr/bin/env",
            "-i",
            *isolated_git_environment_assignments(),
            "/usr/bin/git",
            "-c",
            "init.defaultBranch=main",
            "init",
            repo_dir,
        ],
        timeout=30,
    )
    if not result["success"]:
        raise RuntimeError("Git-Init fehlgeschlagen: " + result["stderr"].strip())


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


def _enable_watchdog_update_pause(reason: str = 'update') -> None:
    """Aktiviert den Latch; nur Erfolg oder bewiesene Recovery dürfen ihn löschen."""

    _set_watchdog_update_pause(True, reason=reason)


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


def migrate_storage_manager_next_override(
    override_file="/etc/systemd/system/e3dc-storage-manager.service.d/override.conf",
    command_runner=None,
    reload_systemd=True,
    *,
    allow_redundant_current_override=False,
) -> bool:
    """Migriert oder entfernt einen eng belegten Storage-Legacy-Override.

    Der normale Pfad behält sein bisheriges Verhalten und ersetzt lediglich
    bekannte alte Skriptnamen. Erst der ausdrücklich gebundene
    Download-Bootstrap darf nach dem verifizierten Backup einen bereits
    kanonischen, vollständig redundanten ExecStart-Override entfernen. Andere
    Drop-ins oder abweichende Writer bleiben unverändert und blockieren.
    """

    runner = command_runner or run_command
    target = Path(str(override_file or ""))
    legacy_names = ("storage_manager_next.py", "storage_manager_legacy.py")
    maximum_bytes = 256 * 1024
    parent_descriptor = None
    original_payload = None
    original_metadata = None
    preimage_readback = None
    installed_inode = None
    redundant_override_removed = False

    def _identity(metadata):
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def _read_named_payload():
        before = os.stat(
            target.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > maximum_bytes
        ):
            raise RuntimeError("Storage-Override besitzt keinen sicheren Dateivertrag")
        descriptor = os.open(
            target.name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
        try:
            opened = os.fstat(descriptor)
            if _identity(opened) != _identity(before):
                raise RuntimeError("Storage-Override driftete beim Öffnen")
            if (
                allow_redundant_current_override
                and _repo_descriptor_has_unsafe_xattrs(descriptor)
            ):
                raise RuntimeError("Storage-Override besitzt ACLs oder andere xattrs")
            chunks = []
            remaining = maximum_bytes + 1
            while remaining > 0:
                block = os.read(descriptor, min(64 * 1024, remaining))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            after = os.fstat(descriptor)
            if _identity(after) != _identity(opened):
                raise RuntimeError("Storage-Override driftete beim Lesen")
        finally:
            os.close(descriptor)
        payload = b"".join(chunks)
        if len(payload) > maximum_bytes or b"\x00" in payload:
            raise RuntimeError("Storage-Override ist unplausibel oder enthält NUL")
        return payload, before

    def _redundant_current_exec_argv(source):
        """Erkennt ausschließlich den früher erzeugten reinen ExecStart-Override."""

        section_seen = False
        commands = []
        for raw_line in str(source).splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                if section_seen or line[1:-1].strip().lower() != "service":
                    raise RuntimeError(
                        "Storage-Override besitzt einen fremden systemd-Abschnitt"
                    )
                section_seen = True
                continue
            if not section_seen or "=" not in line:
                raise RuntimeError("Storage-Override besitzt eine fremde Direktive")
            key, value = line.split("=", 1)
            if key.strip().lower() != "execstart":
                raise RuntimeError("Storage-Override besitzt eine fremde Direktive")
            commands.append(value.strip())

        if not section_seen or len(commands) != 2 or commands[0] or not commands[1]:
            raise RuntimeError(
                "Storage-Override ist kein eindeutiger ExecStart-Reset mit einem Writer"
            )
        try:
            argv = tuple(shlex.split(commands[1]))
        except ValueError as exc:
            raise RuntimeError("Storage-Override-ExecStart ist nicht eindeutig") from exc
        canonical_script = os.path.normpath(
            os.path.join(get_install_path(), "Installer", "storage_manager.py")
        )
        venv_bin = os.path.join(
            os.path.normpath(get_venv_path(get_install_user())),
            "bin",
        )
        allowed_executors = {
            os.path.join(venv_bin, "python"),
            os.path.join(venv_bin, "python3"),
        }
        if (
            len(argv) != 2
            or argv[0] not in allowed_executors
            or argv[1] != canonical_script
        ):
            raise RuntimeError(
                "Storage-Override setzt keinen exakt kanonischen Storage-Writer"
            )
        return argv

    def _effective_exec_argv(readback):
        value = str((readback or {}).get("ExecStart") or "")
        marker = "argv[]="
        if marker not in value:
            raise RuntimeError("Effektiver Storage-ExecStart enthält kein argv[]")
        argv_text = value.split(marker, 1)[1].split(" ;", 1)[0].strip()
        try:
            argv = tuple(shlex.split(argv_text))
        except ValueError as exc:
            raise RuntimeError("Effektiver Storage-ExecStart ist nicht eindeutig") from exc
        if not argv:
            raise RuntimeError("Effektiver Storage-ExecStart ist leer")
        return argv

    def _atomic_replace(payload, preserved_metadata, expected_identity):
        nonlocal installed_inode

        temporary_name = (
            f".{target.name}.e3dc-migrate-{os.getpid()}-{secrets.token_hex(6)}"
        )
        temporary_descriptor = None
        replaced = False
        try:
            temporary_descriptor = os.open(
                temporary_name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
            current_temporary = os.fstat(temporary_descriptor)
            if (
                current_temporary.st_uid != preserved_metadata.st_uid
                or current_temporary.st_gid != preserved_metadata.st_gid
            ):
                os.fchown(
                    temporary_descriptor,
                    preserved_metadata.st_uid,
                    preserved_metadata.st_gid,
                )
            os.fchmod(
                temporary_descriptor,
                stat.S_IMODE(preserved_metadata.st_mode),
            )
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = os.write(temporary_descriptor, view[written:])
                if count <= 0:
                    raise RuntimeError("Storage-Override konnte nicht vollständig geschrieben werden")
                written += count
            os.fsync(temporary_descriptor)
            os.utime(
                temporary_name,
                ns=(preserved_metadata.st_atime_ns, preserved_metadata.st_mtime_ns),
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            hardened = os.fstat(temporary_descriptor)
            if (
                not stat.S_ISREG(hardened.st_mode)
                or hardened.st_nlink != 1
                or hardened.st_uid != preserved_metadata.st_uid
                or hardened.st_gid != preserved_metadata.st_gid
                or stat.S_IMODE(hardened.st_mode)
                != stat.S_IMODE(preserved_metadata.st_mode)
                or hardened.st_size != len(payload)
                or hardened.st_mtime_ns != preserved_metadata.st_mtime_ns
            ):
                raise RuntimeError("Temporärer Storage-Override ist nicht sicher gebunden")

            if expected_identity is None:
                try:
                    os.stat(
                        target.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise RuntimeError(
                        "Storage-Override erschien fremd vor dem atomaren Restore"
                    )
            else:
                named_before = os.stat(
                    target.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if _identity(named_before) != expected_identity:
                    raise RuntimeError("Storage-Override driftete vor dem atomaren Ersatz")
            os.replace(
                temporary_name,
                target.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            replaced = True
            installed_inode = (hardened.st_dev, hardened.st_ino)
            os.fsync(parent_descriptor)

            named_after = os.stat(
                target.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                named_after.st_dev,
                named_after.st_ino,
            ) != installed_inode:
                raise RuntimeError("Atomar ersetzter Storage-Override driftete")
            os.lseek(temporary_descriptor, 0, os.SEEK_SET)
            readback = os.read(temporary_descriptor, len(payload) + 1)
            if readback != payload:
                raise RuntimeError("Atomar ersetzter Storage-Override weicht bytegenau ab")
            return named_after
        finally:
            if temporary_descriptor is not None:
                if not replaced:
                    try:
                        named_temporary = os.stat(
                            temporary_name,
                            dir_fd=parent_descriptor,
                            follow_symlinks=False,
                        )
                        opened_temporary = os.fstat(temporary_descriptor)
                        if (
                            named_temporary.st_dev,
                            named_temporary.st_ino,
                        ) == (
                            opened_temporary.st_dev,
                            opened_temporary.st_ino,
                        ):
                            os.unlink(temporary_name, dir_fd=parent_descriptor)
                    except FileNotFoundError:
                        pass
                os.close(temporary_descriptor)

    def _unit_readback():
        result = runner(
            "systemctl show -p LoadState -p ExecStart "
            "e3dc-storage-manager.service",
            timeout=15,
        )
        if not result.get("success"):
            raise RuntimeError(
                "Effektiver Storage-Writer ist nicht lesbar: "
                + str(result.get("stderr") or result.get("returncode") or "unbekannt")
            )
        properties = {}
        for line in str(result.get("stdout") or "").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key] = value.strip()
        if properties.get("LoadState") != "loaded" or not properties.get("ExecStart"):
            raise RuntimeError("Effektiver Storage-Writer besitzt keinen geladenen ExecStart")
        return {
            "LoadState": properties["LoadState"],
            "ExecStart": properties["ExecStart"],
        }

    def _daemon_reload():
        result = runner("sudo systemctl daemon-reload", timeout=15)
        if not result.get("success"):
            raise RuntimeError(
                "systemd daemon-reload nach Storage-Migration fehlgeschlagen: "
                + str(result.get("stderr") or result.get("returncode") or "unbekannt")
            )

    try:
        if not target.is_absolute() or os.path.normpath(str(target)) != str(target):
            raise RuntimeError("Storage-Override-Pfad ist nicht absolut und kanonisch")
        try:
            parent_descriptor = _open_directory_nofollow(target.parent)
        except FileNotFoundError:
            return True
        if allow_redundant_current_override:
            parent_metadata = os.fstat(parent_descriptor)
            if (
                not stat.S_ISDIR(parent_metadata.st_mode)
                or parent_metadata.st_uid != 0
                or parent_metadata.st_gid != 0
                or parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise RuntimeError(
                    "Storage-Override-Verzeichnis ist nicht root-kontrolliert"
                )
        try:
            original_payload, original_metadata = _read_named_payload()
        except FileNotFoundError:
            return True
        if allow_redundant_current_override and (
            original_metadata.st_uid != 0
            or original_metadata.st_gid != 0
            or stat.S_IMODE(original_metadata.st_mode) != 0o644
        ):
            raise RuntimeError(
                "Storage-Override besitzt nicht den sicheren root:root-0644-Vertrag"
            )
        try:
            source = original_payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Storage-Override ist nicht UTF-8-lesbar") from exc
        if allow_redundant_current_override:
            override_argv = _redundant_current_exec_argv(source)
            if not reload_systemd:
                raise RuntimeError(
                    "Storage-Override-Migration benötigt zwingend daemon-reload"
                )
            preimage_readback = _unit_readback()
            if _effective_exec_argv(preimage_readback) != override_argv:
                raise RuntimeError(
                    "Storage-Override stimmt nicht mit dem effektiven Writer überein"
                )
            named_before = os.stat(
                target.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if _identity(named_before) != _identity(original_metadata):
                raise RuntimeError("Storage-Override driftete vor der Entfernung")
            os.unlink(target.name, dir_fd=parent_descriptor)
            redundant_override_removed = True
            os.fsync(parent_descriptor)
            try:
                os.stat(
                    target.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("Redundanter Storage-Override blieb vorhanden")
            _daemon_reload()
            if _effective_exec_argv(_unit_readback()) != override_argv:
                raise RuntimeError(
                    "Storage-Override war nicht redundant; effektiver Writer änderte sich"
                )
            print(
                "  [OK] Redundanter Storage-ExecStart-Override nach Backup entfernt."
            )
            return True

        if not any(name in source for name in legacy_names):
            return True
        if not reload_systemd:
            raise RuntimeError("Storage-Override-Migration benötigt zwingend daemon-reload")

        preimage_readback = _unit_readback()
        updated = source
        for legacy_name in legacy_names:
            updated = updated.replace(legacy_name, "storage_manager.py")
        if any(name in updated for name in legacy_names):
            raise RuntimeError("Legacy-Storage-ExecStart blieb nach Migration erhalten")
        updated_payload = updated.encode("utf-8")

        installed_metadata = _atomic_replace(
            updated_payload,
            original_metadata,
            _identity(original_metadata),
        )
        _daemon_reload()
        effective = _unit_readback()
        effective_exec = effective["ExecStart"]
        canonical_path = os.path.realpath(
            os.path.join(get_install_path(), "Installer", "storage_manager.py")
        )
        if any(name in effective_exec for name in legacy_names) or not re.search(
            re.escape(canonical_path) + r'(?=$|[\s;\}\]\"])',
            effective_exec,
        ):
            raise RuntimeError(
                "Effektiver Storage-Writer ist nach daemon-reload nicht kanonisch"
            )
        installed_inode = (installed_metadata.st_dev, installed_metadata.st_ino)
        print("  [OK] Alter Storage-Manager-Override auf storage_manager.py migriert.")
        return True
    except Exception as exc:
        rollback_errors = []
        if (
            parent_descriptor is not None
            and original_payload is not None
            and original_metadata is not None
            and (installed_inode is not None or redundant_override_removed)
        ):
            try:
                if redundant_override_removed:
                    try:
                        os.stat(
                            target.name,
                            dir_fd=parent_descriptor,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        restore_expected_identity = None
                    else:
                        raise RuntimeError(
                            "Storage-Override erschien fremd vor dem Restore"
                        )
                else:
                    current = os.stat(
                        target.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    if (current.st_dev, current.st_ino) != installed_inode:
                        raise RuntimeError(
                            "Installierter Storage-Override driftete vor dem Rollback"
                        )
                    restore_expected_identity = _identity(current)
                _atomic_replace(
                    original_payload,
                    original_metadata,
                    restore_expected_identity,
                )
                _daemon_reload()
                restored_payload, restored_metadata = _read_named_payload()
                if (
                    restored_payload != original_payload
                    or restored_metadata.st_uid != original_metadata.st_uid
                    or restored_metadata.st_gid != original_metadata.st_gid
                    or stat.S_IMODE(restored_metadata.st_mode)
                    != stat.S_IMODE(original_metadata.st_mode)
                    or restored_metadata.st_nlink != original_metadata.st_nlink
                    or restored_metadata.st_mtime_ns != original_metadata.st_mtime_ns
                ):
                    raise RuntimeError("Storage-Override-Preimage wurde nicht vollständig restauriert")
                if preimage_readback is not None and _unit_readback() != preimage_readback:
                    raise RuntimeError("Effektiver Storage-Writer wich nach Restore vom Preimage ab")
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        detail = str(exc)
        if rollback_errors:
            detail += (
                "; Restore fehlgeschlagen; Update bleibt fail-closed abgebrochen: "
                + "; ".join(rollback_errors)
            )
        print(f"  [!] Storage-Manager-Override konnte nicht migriert werden: {detail}")
        return False
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


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
    'e3dc-forecast-evidence',
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

PIGUARD_UNIT = "piguard.service"
PIGUARD_FRAGMENT_PATH = "/etc/systemd/system/piguard.service"
PIGUARD_EXECUTABLE_PATH = "/usr/local/bin/pi_guard.sh"


def _recovery_bootblock_units() -> tuple[str, ...]:
    units = tuple(dict.fromkeys(
        tuple(
            f"{name}.service"
            for name in _catalog_service_names(include_legacy=True)
        )
        + (PIGUARD_UNIT,)
    ))
    if not units or len(set(units)) != len(units):
        raise RuntimeError("Recovery-Bootblock besitzt keinen eindeutigen Unitumfang")
    if any(
        not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", unit)
        for unit in units
    ):
        raise RuntimeError("Recovery-Bootblock enthält einen ungültigen Unitnamen")
    return units


def _require_root_controlled_directory(descriptor: int, label: str, mode: int | None = None):
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
        or _repo_descriptor_has_unsafe_xattrs(descriptor)
    ):
        raise RuntimeError(f"Recovery-Bootblock-Verzeichnis ist unsicher: {label}")
    return metadata


def _read_exact_root_file_at(
    parent_descriptor: int,
    name: str,
    payload: bytes,
    mode: int,
    *,
    allow_missing: bool = False,
):
    try:
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise RuntimeError("Recovery-Bootblock benötigt O_NOFOLLOW")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != 0
        or before.st_gid != 0
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_size != len(payload)
    ):
        raise RuntimeError(f"Recovery-Bootblock-Datei ist unsicher: {name}")
    descriptor = os.open(
        name,
        os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        signature = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_nlink,
            before.st_uid,
            before.st_gid,
            stat.S_IMODE(before.st_mode),
        )
        if signature != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
            opened.st_nlink,
            opened.st_uid,
            opened.st_gid,
            stat.S_IMODE(opened.st_mode),
        ) or _repo_descriptor_has_unsafe_xattrs(descriptor):
            raise RuntimeError(f"Recovery-Bootblock-Datei driftete beim Öffnen: {name}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        actual = bytearray()
        while len(actual) <= len(payload):
            block = os.read(descriptor, len(payload) + 1 - len(actual))
            if not block:
                break
            actual.extend(block)
        after = os.fstat(descriptor)
        named_after = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            bytes(actual) != payload
            or signature
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_nlink,
                after.st_uid,
                after.st_gid,
                stat.S_IMODE(after.st_mode),
            )
            or signature
            != (
                named_after.st_dev,
                named_after.st_ino,
                named_after.st_size,
                named_after.st_mtime_ns,
                named_after.st_ctime_ns,
                named_after.st_nlink,
                named_after.st_uid,
                named_after.st_gid,
                stat.S_IMODE(named_after.st_mode),
            )
        ):
            raise RuntimeError(f"Recovery-Bootblock-Datei besitzt falsche Bytes: {name}")
        return after
    finally:
        os.close(descriptor)


def _create_exact_root_file_at(
    parent_descriptor: int,
    name: str,
    payload: bytes,
    mode: int,
) -> None:
    if _read_exact_root_file_at(
        parent_descriptor,
        name,
        payload,
        mode,
        allow_missing=True,
    ) is not None:
        return
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise RuntimeError("Recovery-Bootblock benötigt O_NOFOLLOW")
    temporary_name = f".e3dc-recovery-{os.getpid()}-{secrets.token_hex(12)}"
    descriptor = None
    linked = False
    try:
        descriptor = os.open(
            temporary_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | nofollow
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("Recovery-Bootblock-Datei konnte nicht geschrieben werden")
            view = view[written:]
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        temporary = os.fstat(descriptor)
        if (
            not stat.S_ISREG(temporary.st_mode)
            or temporary.st_nlink != 1
            or temporary.st_uid != 0
            or temporary.st_gid != 0
            or stat.S_IMODE(temporary.st_mode) != mode
            or temporary.st_size != len(payload)
            or _repo_descriptor_has_unsafe_xattrs(descriptor)
        ):
            raise RuntimeError("Temporärer Recovery-Bootblock ist unsicher")
        # linkat wirkt im root-kontrollierten Verzeichnis als atomare
        # NOREPLACE-Installation; ein vorhandener reservierter Name wird nie
        # überschrieben.
        os.link(
            temporary_name,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        linked = True
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        _read_exact_root_file_at(parent_descriptor, name, payload, mode)
    except Exception:
        if linked:
            try:
                current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
                opened = os.fstat(descriptor) if descriptor is not None else None
                if opened is not None and (current.st_dev, current.st_ino) == (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    os.unlink(name, dir_fd=parent_descriptor)
            except (FileNotFoundError, OSError):
                pass
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_or_create_recovery_directory(
    parent_descriptor: int,
    name: str,
    *,
    mode: int,
    label: str,
) -> tuple[int, bool]:
    created = False
    try:
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        os.mkdir(name, mode, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        created = True
    if not stat.S_ISDIR(before.st_mode):
        raise RuntimeError(f"Recovery-Bootblock-Pfad ist kein Verzeichnis: {label}")
    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not directory or not nofollow:
        raise RuntimeError("Recovery-Bootblock benötigt O_DIRECTORY und O_NOFOLLOW")
    descriptor = os.open(
        name,
        os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_descriptor,
    )
    try:
        opened = _require_root_controlled_directory(descriptor, label, mode)
        named_after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or (
            named_after.st_dev,
            named_after.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise RuntimeError(f"Recovery-Bootblock-Verzeichnis driftete: {label}")
        return descriptor, created
    except Exception:
        os.close(descriptor)
        if created:
            try:
                os.rmdir(name, dir_fd=parent_descriptor)
            except OSError:
                pass
        raise


def _recovery_dropin_path(unit: str) -> str:
    return os.path.join(
        "/etc/systemd/system",
        f"{unit}.d",
        RECOVERY_BOOTBLOCK_DROPIN_NAME,
    )


def _recovery_bootblock_marker_payload(transaction_id: str) -> bytes:
    value = str(transaction_id or "")
    if not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(value):
        raise RuntimeError("Recovery-Bootblock besitzt keine gültige Transaktions-ID")
    return f"E3DC_RECOVERY_BOOTBLOCK_V2:{value}\n".encode("ascii")


def _validate_recovery_bootblock_contract(
    contract: RecoveryBootblockContract,
) -> dict[str, tuple[int, int]]:
    if (
        not isinstance(contract, RecoveryBootblockContract)
        or contract.units != _recovery_bootblock_units()
        or not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(contract.transaction_id)
        or not set(contract.created_directories).issubset(contract.units)
    ):
        raise RuntimeError("Recovery-Bootblock-Vertrag driftete")
    identities = {
        str(unit): (int(device), int(inode))
        for unit, device, inode in contract.dropin_identities
    }
    if (
        len(identities) != len(contract.dropin_identities)
        or set(identities) != set(contract.units)
        or any(device < 0 or inode <= 0 for device, inode in identities.values())
    ):
        raise RuntimeError("Recovery-Bootblock-Inodevertrag driftete")
    return identities


def _validate_partial_recovery_bootblock_contract(
    contract: RecoveryBootblockPartialContract,
) -> dict[str, tuple[int, int]]:
    if (
        not isinstance(contract, RecoveryBootblockPartialContract)
        or contract.units != _recovery_bootblock_units()
        or not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(contract.transaction_id)
        or not set(contract.created_directories).issubset(contract.units)
        or len(set(contract.created_directories)) != len(contract.created_directories)
        or not isinstance(contract.allow_missing_directories, bool)
    ):
        raise RuntimeError("Partieller Recovery-Bootblock-Vertrag driftete")
    identities = {
        str(unit): (int(device), int(inode))
        for unit, device, inode in contract.dropin_identities
    }
    if (
        len(identities) != len(contract.dropin_identities)
        or not set(identities).issubset(contract.units)
        or any(device < 0 or inode <= 0 for device, inode in identities.values())
    ):
        raise RuntimeError("Partieller Recovery-Bootblock-Inodevertrag driftete")
    return identities


def _assert_no_same_transaction_finalizer_processes(
    contract: UpdateSafetyContract,
) -> None:
    """Beweist für committed Cleanup null Prozesse derselben Finalizer-Lease."""

    unit_needle = contract.finalizer_unit.encode("ascii")
    transaction_needle = contract.transaction_id.encode("ascii")
    findings = []
    for entry in os.listdir("/proc"):
        if not entry.isdecimal() or int(entry) == os.getpid():
            continue
        try:
            raw = Path(f"/proc/{entry}/cmdline").read_bytes()
            cgroup = Path(f"/proc/{entry}/cgroup").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise RuntimeError(
                f"Same-tx-Prozess {entry} ist beim committed Cleanup nicht lesbar"
            ) from exc
        argv = tuple(raw.rstrip(b"\0").split(b"\0")) if raw else ()
        transaction_marked = any(
            value == transaction_needle
            and index > 0
            and argv[index - 1] == b"--update-safety-transaction"
            for index, value in enumerate(argv)
        )
        if (
            any(unit_needle in value for value in argv)
            or transaction_marked
            or (b"/" + unit_needle) in cgroup
        ):
            findings.append(int(entry))
    if findings:
        raise RuntimeError(
            "Committed Finalizer-Lease besitzt noch same-tx-Prozesse: "
            + ",".join(str(pid) for pid in sorted(findings))
        )


def _assert_committed_finalizer_lease_inactive(
    contract: UpdateSafetyContract,
) -> None:
    """Bindet eine vollständig inaktive oder bereits entladene transiente Lease."""

    properties = (
        "Id",
        "LoadState",
        "ActiveState",
        "SubState",
        "MainPID",
        "ControlGroup",
        "FragmentPath",
        "DropInPaths",
        "Transient",
        "RuntimeDirectory",
    )
    result = _run_argv(
        [
            "systemctl",
            "show",
            "--no-pager",
            *(f"--property={name}" for name in properties),
            contract.finalizer_unit,
        ],
        timeout=15,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )
    values = {}
    for line in str(result.get("stdout") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator != "=" or key not in properties or key in values:
            values = {}
            break
        values[key] = value
    if (
        not result.get("success")
        or result.get("timed_out")
        or int(result.get("returncode", -1)) != 0
        or str(result.get("stderr") or "")
        or set(values) != set(properties)
        or values.get("Id") != contract.finalizer_unit
        or values.get("ActiveState") != "inactive"
        or values.get("SubState") != "dead"
        or values.get("MainPID") != "0"
        or values.get("DropInPaths") != ""
    ):
        raise RuntimeError("Committed Finalizer-Lease ist nicht eindeutig inaktiv")

    load_state = values.get("LoadState")
    fragment = values.get("FragmentPath", "")
    control_group = values.get("ControlGroup", "")
    if load_state == "not-found":
        if (
            fragment
            or control_group
            or values.get("Transient") != "no"
            or values.get("RuntimeDirectory")
        ):
            raise RuntimeError("Entladene committed Finalizer-Lease besitzt Residuen")
    elif load_state == "loaded":
        expected_fragment = f"/run/systemd/transient/{contract.finalizer_unit}"
        if (
            fragment != expected_fragment
            or values.get("Transient") != "yes"
            or values.get("RuntimeDirectory") != contract.runtime_directory
            or (
                control_group
                and not control_group.endswith("/" + contract.finalizer_unit)
            )
        ):
            raise RuntimeError("Geladene committed Finalizer-Lease driftete")
        fragment_metadata = os.lstat(expected_fragment)
        if (
            not stat.S_ISREG(fragment_metadata.st_mode)
            or fragment_metadata.st_nlink != 1
            or fragment_metadata.st_uid != 0
            or fragment_metadata.st_gid != 0
            or fragment_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("Transiente committed Unitdatei ist unsicher")
    else:
        raise RuntimeError(
            f"Committed Finalizer-Lease besitzt LoadState {load_state!r}"
        )

    if control_group:
        cgroup_path = Path("/sys/fs/cgroup") / control_group.lstrip("/")
        try:
            processes = (cgroup_path / "cgroup.procs").read_text(
                encoding="ascii"
            )
        except FileNotFoundError:
            processes = ""
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("Committed Finalizer-cgroup ist nicht lesbar") from exc
        if processes.strip():
            raise RuntimeError("Committed Finalizer-cgroup besitzt noch Prozesse")


def _finish_committed_update_safety_residue_if_safe() -> bool:
    """Räumt nur vollständig gebundene committed Residuen vor einem neuen Lauf."""

    residue = _read_update_safety_contract(allow_missing=True)
    if residue is None or residue.state != "committed":
        return False
    _assert_committed_finalizer_lease_inactive(residue)
    if os.path.lexists(f"/run/{residue.runtime_directory}") or os.path.lexists(
        residue.token_path
    ):
        raise RuntimeError("Committed Finalizer-Runtime/Token ist noch vorhanden")
    _assert_no_same_transaction_finalizer_processes(residue)
    _finish_committed_update_safety_cleanup(
        residue,
        remove_receipt=True,
    )
    if _read_update_safety_contract(allow_missing=True) is not None:
        raise RuntimeError("Committed Update-Sicherheitsreceipt blieb nach Cleanup")
    return True


def _assert_no_existing_recovery_bootblock() -> None:
    """Blockiert einen neuen Updatepfad bei jedem fremden/stalen Recovery-Gate."""

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("Recovery-Bootblock-Preflight darf ausschließlich Root ausführen")
    _finish_committed_update_safety_residue_if_safe()
    var_lib_descriptor = _open_directory_nofollow("/var/lib")
    try:
        _require_root_controlled_directory(var_lib_descriptor, "/var/lib")
        state_name = Path(RECOVERY_BOOTBLOCK_STATE_DIR).name
        try:
            state_before = os.stat(
                state_name,
                dir_fd=var_lib_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(state_before.st_mode):
                raise RuntimeError("Recovery-Bootblock-Statepfad ist nicht sicher")
            state_descriptor = os.open(
                state_name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=var_lib_descriptor,
            )
            try:
                opened = _require_root_controlled_directory(
                    state_descriptor,
                    RECOVERY_BOOTBLOCK_STATE_DIR,
                    0o700,
                )
                if (opened.st_dev, opened.st_ino) != (
                    state_before.st_dev,
                    state_before.st_ino,
                ):
                    raise RuntimeError("Recovery-Bootblock-Statepfad driftete")
                if Path(RECOVERY_BOOTBLOCK_MARKER).name in set(
                    os.listdir(state_descriptor)
                ):
                    raise RuntimeError(
                        "Vorhandener Recovery-Bootblock-Marker sperrt einen neuen Updatepfad"
                    )
            finally:
                os.close(state_descriptor)
    finally:
        os.close(var_lib_descriptor)

    systemd_descriptor = _open_directory_nofollow("/etc/systemd/system")
    try:
        _require_root_controlled_directory(systemd_descriptor, "/etc/systemd/system")
        for entry in os.listdir(systemd_descriptor):
            if not entry.endswith(".d"):
                continue
            before = os.stat(entry, dir_fd=systemd_descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise RuntimeError(
                    f"Systemd-Drop-in-Pfad ist nicht sicher prüfbar: {entry}"
                )
            directory_descriptor = os.open(
                entry,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=systemd_descriptor,
            )
            try:
                opened = _require_root_controlled_directory(
                    directory_descriptor,
                    f"/etc/systemd/system/{entry}",
                )
                named_after = os.stat(
                    entry,
                    dir_fd=systemd_descriptor,
                    follow_symlinks=False,
                )
                if (
                    (opened.st_dev, opened.st_ino)
                    != (before.st_dev, before.st_ino)
                    or (named_after.st_dev, named_after.st_ino)
                    != (opened.st_dev, opened.st_ino)
                ):
                    raise RuntimeError(f"Systemd-Drop-in-Pfad driftete: {entry}")
                if RECOVERY_BOOTBLOCK_DROPIN_NAME in set(
                    os.listdir(directory_descriptor)
                ):
                    raise RuntimeError(
                        "Vorhandenes Recovery-Bootblock-Drop-in sperrt einen neuen Updatepfad"
                    )
            finally:
                os.close(directory_descriptor)
    finally:
        os.close(systemd_descriptor)

    residue = _read_update_safety_contract(allow_missing=True)
    if residue is not None:
        raise RuntimeError(
            "Pending oder unvollständig bereinigtes Update-Sicherheitsreceipt "
            "verlangt eine manuelle fail-closed Prüfung"
        )


def _assert_exclusive_not_found_recovery_dropin(
    unit: str,
    *,
    expected_payload: bytes = RECOVERY_BOOTBLOCK_DROPIN_PAYLOAD,
) -> None:
    """Bindet bei fehlendem Fragment nur Recovery- und kanonisches RAM-Disk-Drop-in."""

    systemd_descriptor = _open_directory_nofollow("/etc/systemd/system")
    directory_descriptor = None
    directory_name = f"{unit}.d"
    try:
        _require_root_controlled_directory(
            systemd_descriptor,
            "/etc/systemd/system",
        )
        before = os.stat(
            directory_name,
            dir_fd=systemd_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(before.st_mode):
            raise RuntimeError(
                f"Recovery-Bootblock-Pfad ist kein Verzeichnis: {unit}"
            )
        directory_descriptor = os.open(
            directory_name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=systemd_descriptor,
        )
        opened = _require_root_controlled_directory(
            directory_descriptor,
            _recovery_dropin_path(unit),
            0o755,
        )
        named_after = os.stat(
            directory_name,
            dir_fd=systemd_descriptor,
            follow_symlinks=False,
        )
        if (
            (before.st_dev, before.st_ino)
            != (opened.st_dev, opened.st_ino)
            or (named_after.st_dev, named_after.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise RuntimeError(
                f"Recovery-Bootblock-Pfad driftete bei not-found: {unit}"
            )
        from .ramdisk_guard import (
            RAMDISK_DROPIN_NAME,
            render_ramdisk_service_dropin,
        )

        entries = tuple(os.listdir(directory_descriptor))
        entry_names = set(entries)
        allowed_shapes = (
            {RECOVERY_BOOTBLOCK_DROPIN_NAME},
            {RECOVERY_BOOTBLOCK_DROPIN_NAME, RAMDISK_DROPIN_NAME},
        )
        if len(entries) != len(entry_names) or entry_names not in allowed_shapes:
            raise RuntimeError(
                "Fehlende systemd-Unit besitzt eine fremde On-Disk-Drop-in-Fläche: "
                f"{unit}"
            )
        _read_exact_root_file_at(
            directory_descriptor,
            RECOVERY_BOOTBLOCK_DROPIN_NAME,
            bytes(expected_payload),
            0o644,
        )
        if RAMDISK_DROPIN_NAME in entry_names:
            _read_exact_root_file_at(
                directory_descriptor,
                RAMDISK_DROPIN_NAME,
                render_ramdisk_service_dropin().encode("utf-8"),
                0o644,
            )
        if set(os.listdir(directory_descriptor)) != entry_names:
            raise RuntimeError(
                f"Recovery-Bootblock-Pfad driftete beim not-found-Readback: {unit}"
            )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Recovery-Bootblock-Pfad fehlt bei not-found: {unit}"
        ) from exc
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        os.close(systemd_descriptor)


def _reload_and_verify_recovery_dropins(
    units: tuple[str, ...],
    *,
    expected_present: bool,
) -> None:
    systemd_env = dict(os.environ)
    systemd_env.update({"LC_ALL": "C", "LANG": "C"})
    reload_result = _run_argv(
        ["systemctl", "daemon-reload"],
        timeout=30,
        env=systemd_env,
    )
    if (
        not reload_result.get("success")
        or reload_result.get("timed_out")
        or str(reload_result.get("stderr") or "")
        or int(reload_result.get("returncode", -1)) != 0
    ):
        raise RuntimeError(
            "systemd daemon-reload für Recovery-Bootblock fehlgeschlagen: "
            + _combined_process_diagnostics(reload_result, maximum=800)
        )
    marker = "# E3DC_RECOVERY_BOOTBLOCK_V1"
    def show_value(unit: str, property_name: str) -> str:
        result = _run_argv(
            [
                "systemctl",
                "show",
                f"--property={property_name}",
                "--value",
                unit,
            ],
            timeout=15,
            env=systemd_env,
        )
        output = str(result.get("stdout") or "")
        if (
            not result.get("success")
            or result.get("timed_out")
            or str(result.get("stderr") or "")
            or int(result.get("returncode", -1)) != 0
            or "\x00" in output
            or len(output.splitlines()) > 1
        ):
            raise RuntimeError(
                f"systemd-{property_name}-Readback ist unklar: {unit}"
            )
        return output.strip()

    for unit in units:
        load_state = show_value(unit, "LoadState").lower()
        result = _run_argv(
            ["systemctl", "cat", "--no-pager", unit],
            timeout=15,
            env=systemd_env,
        )
        output = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        if "\x00" in output or "\x00" in stderr:
            raise RuntimeError(f"systemd-cat-Readback enthält NUL: {unit}")
        canonical_not_found = bool(
            not result.get("success")
            and not result.get("timed_out")
            and int(result.get("returncode", -1)) == 1
            and output == ""
            and stderr == f"No files found for {unit}.\n"
        )
        dropin_value = show_value(unit, "DropInPaths")
        try:
            dropin_paths = tuple(shlex.split(dropin_value)) if dropin_value else ()
        except ValueError as exc:
            raise RuntimeError(
                f"systemd-DropInPaths sind nicht eindeutig lesbar: {unit}"
            ) from exc
        own_dropin = _recovery_dropin_path(unit)
        marker_lines = [line for line in output.splitlines() if line.strip() == marker]
        effective_conditions = []
        condition_syntax_ambiguous = False
        for line in output.splitlines():
            match = re.fullmatch(r"\s*(Condition[A-Za-z0-9]+)\s*=(.*)", line)
            stripped = line.lstrip()
            if (
                stripped.startswith("Condition")
                and not stripped.startswith(("#", ";"))
                and match is None
            ):
                condition_syntax_ambiguous = True
            if match:
                assignment = (match.group(1), match.group(2).strip())
                if assignment[1].endswith("\\"):
                    condition_syntax_ambiguous = True
                # systemd setzt bei jeder leeren Condition*-Zuweisung die
                # komplette bis dahin aufgebaute Condition-Liste zurück.
                # Wir werten deshalb die zusammengefügte Unit in derselben
                # Reihenfolge aus, statt bloß Textvorkommen zu zählen.
                if not assignment[1]:
                    effective_conditions.clear()
                else:
                    effective_conditions.append(assignment)
        marker_path_conditions = [
            value
            for name, value in effective_conditions
            if name == "ConditionPathExists"
            and value.lstrip("|!") == RECOVERY_BOOTBLOCK_MARKER
        ]

        if expected_present:
            # Eine exakt maskierte Unit ist bereits stärker rebootfest
            # gesperrt; systemd muss deren Drop-in nicht in die effektive
            # Unitansicht aufnehmen.
            if load_state == "masked":
                if show_value(unit, "UnitFileState").lower() != "masked":
                    raise RuntimeError(
                        f"Nur eine persistente systemd-Maske sperrt rebootfest: {unit}"
                    )
                continue
            # Katalogisierte optionale Units dürfen auf heterogenen Anlagen
            # vollständig fehlen. `not-found` ist nur dann ein sicherer
            # Abwesenheitsbeweis, wenn systemd weder Fragment noch wirksamen
            # Drop-in-Pfad meldet; der On-Disk-Inodevertrag wird vom direkten
            # Caller separat descriptorgebunden gehalten.
            if load_state == "not-found":
                if (
                    not canonical_not_found
                    or dropin_paths
                    or marker_lines
                    or effective_conditions
                    or condition_syntax_ambiguous
                ):
                    raise RuntimeError(
                        f"systemd meldet einen inkonsistenten not-found-Bootblock: {unit}"
                    )
                # Bei einer aktuell fehlenden Basis-Unit lädt systemd Drop-ins
                # nicht in seine effektive Sicht. Ein später restauriertes
                # Fragment könnte deshalb einen bislang unsichtbaren, späteren
                # Condition-Reset aktivieren. Zulässig ist hier ausschließlich
                # unser bereits transaktionsgebundener 00-Inode.
                _assert_exclusive_not_found_recovery_dropin(unit)
                continue
            if (
                load_state != "loaded"
                or not result.get("success")
                or result.get("timed_out")
                or str(result.get("stderr") or "")
                or int(result.get("returncode", -1)) != 0
                or dropin_paths.count(own_dropin) != 1
                or len(marker_lines) != 1
                or condition_syntax_ambiguous
                or marker_path_conditions
                != [f"!{RECOVERY_BOOTBLOCK_MARKER}"]
            ):
                raise RuntimeError(
                    f"systemd-Readback des Recovery-Bootblocks weicht ab: {unit}"
                )
        else:
            if load_state not in {"loaded", "masked", "not-found"}:
                raise RuntimeError(
                    f"systemd-Readback nach Bootblock-Entfernung ist unklar: {unit}"
                )
            if (
                own_dropin in dropin_paths
                or marker_lines
                or (load_state == "not-found" and not canonical_not_found)
                or (
                    "ConditionPathExists",
                    f"!{RECOVERY_BOOTBLOCK_MARKER}",
                )
                in effective_conditions
                or (
                    load_state == "loaded"
                    and (
                        not result.get("success")
                        or result.get("timed_out")
                        or str(result.get("stderr") or "")
                        or int(result.get("returncode", -1)) != 0
                    )
                )
            ):
                raise RuntimeError(
                    f"systemd-Readback enthält weiterhin den Recovery-Bootblock: {unit}"
                )


def _complete_partial_recovery_bootblock(
    partial: RecoveryBootblockPartialContract,
) -> RecoveryBootblockContract:
    """Vollendet nur die transaktionsgebundenen, exakt lesbaren Residual-Inodes."""

    identities = _validate_partial_recovery_bootblock_contract(partial)
    created_directories = list(partial.created_directories)
    absent_directories_seen: set[str] = set()
    systemd_descriptor = _open_directory_nofollow("/etc/systemd/system")
    try:
        _require_root_controlled_directory(
            systemd_descriptor,
            "/etc/systemd/system",
        )
        try:
            for unit in partial.units:
                directory_name = f"{unit}.d"
                try:
                    os.stat(
                        directory_name,
                        dir_fd=systemd_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    absent_directories_seen.add(unit)
                    if (
                        not partial.allow_missing_directories
                        and unit not in set(created_directories)
                    ):
                        raise RuntimeError(
                            f"Eigener Recovery-Bootblock-Pfad fehlt: {unit}"
                        )
                directory_descriptor, directory_created = (
                    _open_or_create_recovery_directory(
                        systemd_descriptor,
                        directory_name,
                        mode=0o755,
                        label=_recovery_dropin_path(unit),
                    )
                )
                if directory_created and unit not in created_directories:
                    created_directories.append(unit)
                try:
                    metadata = _read_exact_root_file_at(
                        directory_descriptor,
                        RECOVERY_BOOTBLOCK_DROPIN_NAME,
                        RECOVERY_BOOTBLOCK_DROPIN_PAYLOAD,
                        0o644,
                        allow_missing=True,
                    )
                    if metadata is None:
                        _create_exact_root_file_at(
                            directory_descriptor,
                            RECOVERY_BOOTBLOCK_DROPIN_NAME,
                            RECOVERY_BOOTBLOCK_DROPIN_PAYLOAD,
                            0o644,
                        )
                        metadata = _read_exact_root_file_at(
                            directory_descriptor,
                            RECOVERY_BOOTBLOCK_DROPIN_NAME,
                            RECOVERY_BOOTBLOCK_DROPIN_PAYLOAD,
                            0o644,
                        )
                    current_identity = (
                        int(metadata.st_dev),
                        int(metadata.st_ino),
                    )
                    if (
                        unit in identities
                        and identities[unit] != current_identity
                    ):
                        raise RuntimeError(
                            "Recovery-Bootblock-Inode wurde nicht von dieser "
                            f"Transaktion erzeugt: {unit}"
                        )
                    identities[unit] = current_identity
                finally:
                    os.close(directory_descriptor)
        except Exception as exc:
            # Ein Fehler nach linkat/fsync darf den bereits erzeugten Inode
            # nicht aus dem Prozessvertrag verlieren. Rescan übernimmt nur
            # bytegenaue root:root-Inodes an zuvor autorisierten Namen.
            for unit in partial.units:
                directory_name = f"{unit}.d"
                directory_descriptor = None
                try:
                    directory_before = os.stat(
                        directory_name,
                        dir_fd=systemd_descriptor,
                        follow_symlinks=False,
                    )
                    directory_descriptor = os.open(
                        directory_name,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=systemd_descriptor,
                    )
                    directory_opened = _require_root_controlled_directory(
                        directory_descriptor,
                        _recovery_dropin_path(unit),
                        0o755,
                    )
                    directory_after = os.stat(
                        directory_name,
                        dir_fd=systemd_descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        (directory_before.st_dev, directory_before.st_ino)
                        != (directory_opened.st_dev, directory_opened.st_ino)
                        or (directory_after.st_dev, directory_after.st_ino)
                        != (directory_opened.st_dev, directory_opened.st_ino)
                    ):
                        raise RuntimeError(
                            f"Recovery-Bootblock-Pfad driftete: {unit}"
                        )
                    if (
                        unit in absent_directories_seen
                        and unit not in created_directories
                    ):
                        created_directories.append(unit)
                    metadata = _read_exact_root_file_at(
                        directory_descriptor,
                        RECOVERY_BOOTBLOCK_DROPIN_NAME,
                        RECOVERY_BOOTBLOCK_DROPIN_PAYLOAD,
                        0o644,
                        allow_missing=True,
                    )
                    if metadata is None:
                        continue
                    current_identity = (
                        int(metadata.st_dev),
                        int(metadata.st_ino),
                    )
                    if unit not in identities or identities[unit] == current_identity:
                        identities[unit] = current_identity
                except Exception:
                    continue
                finally:
                    if directory_descriptor is not None:
                        os.close(directory_descriptor)
            residual = RecoveryBootblockPartialContract(
                units=partial.units,
                created_directories=tuple(created_directories),
                transaction_id=partial.transaction_id,
                dropin_identities=tuple(
                    (unit, *identities[unit])
                    for unit in partial.units
                    if unit in identities
                ),
                allow_missing_directories=partial.allow_missing_directories,
            )
            raise RecoveryBootblockArmError(
                f"Recovery-Bootblock-Vollendung blieb partiell: {exc}",
                residual,
            ) from exc
    finally:
        os.close(systemd_descriptor)
    complete = RecoveryBootblockContract(
        units=partial.units,
        created_directories=tuple(created_directories),
        transaction_id=partial.transaction_id,
        dropin_identities=tuple(
            (unit, *identities[unit]) for unit in partial.units
        ),
    )
    _validate_recovery_bootblock_contract(complete)
    return complete


def _prepare_persistent_recovery_bootblock(
    transaction_id: str,
) -> RecoveryBootblockContract:
    """Installiert rebootfeste Conditions mit erhaltener Partial-Autorität."""

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("Recovery-Bootblock darf ausschließlich Root verwalten")
    _recovery_bootblock_marker_payload(transaction_id)
    _assert_no_existing_recovery_bootblock()
    return _complete_partial_recovery_bootblock(
        RecoveryBootblockPartialContract(
            units=_recovery_bootblock_units(),
            created_directories=(),
            transaction_id=transaction_id,
            dropin_identities=(),
            allow_missing_directories=True,
        )
    )


def _rebind_owned_recovery_dropins(
    contract: RecoveryBootblockContract,
    *,
    recreate_missing: bool,
) -> RecoveryBootblockContract:
    identities = _validate_recovery_bootblock_contract(contract)
    systemd_descriptor = _open_directory_nofollow("/etc/systemd/system")
    rebound = {}
    missing = set()
    try:
        _require_root_controlled_directory(systemd_descriptor, "/etc/systemd/system")
        for unit in contract.units:
            directory_name = f"{unit}.d"
            try:
                os.stat(
                    directory_name,
                    dir_fd=systemd_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not recreate_missing or unit not in set(contract.created_directories):
                    raise RuntimeError(
                        f"Eigener Recovery-Bootblock-Pfad fehlt: {unit}"
                    )
                missing.add(unit)
                continue
            directory_descriptor, _created = _open_or_create_recovery_directory(
                systemd_descriptor,
                directory_name,
                mode=0o755,
                label=_recovery_dropin_path(unit),
            )
            try:
                metadata = _read_exact_root_file_at(
                    directory_descriptor,
                    RECOVERY_BOOTBLOCK_DROPIN_NAME,
                    RECOVERY_BOOTBLOCK_DROPIN_PAYLOAD,
                    0o644,
                    allow_missing=True,
                )
                if metadata is None:
                    if not recreate_missing:
                        raise RuntimeError(f"Eigener Recovery-Bootblock fehlt: {unit}")
                    missing.add(unit)
                    continue
                if (metadata.st_dev, metadata.st_ino) != identities[unit]:
                    raise RuntimeError(
                        f"Recovery-Bootblock-Inode wurde nicht von dieser Transaktion erzeugt: {unit}"
                    )
                rebound[unit] = (int(metadata.st_dev), int(metadata.st_ino))
            finally:
                os.close(directory_descriptor)
    finally:
        os.close(systemd_descriptor)
    if not missing:
        return contract
    return _complete_partial_recovery_bootblock(
        RecoveryBootblockPartialContract(
            units=contract.units,
            created_directories=contract.created_directories,
            transaction_id=contract.transaction_id,
            dropin_identities=tuple(
                (unit, *rebound[unit])
                for unit in contract.units
                if unit in rebound
            ),
            allow_missing_directories=False,
        )
    )


def _open_recovery_bootblock_state_directory() -> int:
    parent = Path(RECOVERY_BOOTBLOCK_STATE_DIR).parent
    parent_descriptor = _open_directory_nofollow(parent)
    try:
        _require_root_controlled_directory(parent_descriptor, str(parent))
        descriptor, _created = _open_or_create_recovery_directory(
            parent_descriptor,
            Path(RECOVERY_BOOTBLOCK_STATE_DIR).name,
            mode=0o700,
            label=RECOVERY_BOOTBLOCK_STATE_DIR,
        )
        return descriptor
    finally:
        os.close(parent_descriptor)


def _verify_recovery_bootblock_marker(
    contract: RecoveryBootblockContract,
    *,
    expected_present: bool,
) -> None:
    _validate_recovery_bootblock_contract(contract)
    marker_payload = _recovery_bootblock_marker_payload(contract.transaction_id)
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        present = _read_exact_root_file_at(
            state_descriptor,
            Path(RECOVERY_BOOTBLOCK_MARKER).name,
            marker_payload,
            0o600,
            allow_missing=True,
        ) is not None
        if present != expected_present:
            raise RuntimeError("Recovery-Bootblock-Marker besitzt den falschen Zustand")
    finally:
        os.close(state_descriptor)


def _arm_persistent_recovery_bootblock(
    contract: (
        RecoveryBootblockContract | RecoveryBootblockPartialContract | None
    ) = None,
    *,
    transaction_id: str | None = None,
) -> RecoveryBootblockContract:
    if contract is None:
        value = str(transaction_id or "")
        if not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(value):
            raise RuntimeError("Recovery-Bootblock fehlt die vorab gebundene Transaktions-ID")
        prepared = _prepare_persistent_recovery_bootblock(value)
    elif isinstance(contract, RecoveryBootblockPartialContract):
        if transaction_id is not None and contract.transaction_id != transaction_id:
            raise RuntimeError("Recovery-Bootblock-Transaktions-ID driftete")
        prepared = _complete_partial_recovery_bootblock(contract)
    else:
        if transaction_id is not None and contract.transaction_id != transaction_id:
            raise RuntimeError("Recovery-Bootblock-Transaktions-ID driftete")
        prepared = _rebind_owned_recovery_dropins(
            contract,
            recreate_missing=True,
        )
    try:
        marker_payload = _recovery_bootblock_marker_payload(prepared.transaction_id)
        state_descriptor = _open_recovery_bootblock_state_directory()
        try:
            _create_exact_root_file_at(
                state_descriptor,
                Path(RECOVERY_BOOTBLOCK_MARKER).name,
                marker_payload,
                0o600,
            )
            os.fsync(state_descriptor)
        finally:
            os.close(state_descriptor)
        _verify_recovery_bootblock_marker(prepared, expected_present=True)
        _reload_and_verify_recovery_dropins(prepared.units, expected_present=True)
        return prepared
    except Exception as arm_exc:
        # Ab dem ersten eigenen Inode bleibt der Contract erhalten. Ein
        # Folgeversuch bindet exakt diese Inodes und vervollständigt Marker/
        # effektive systemd-Sicht; er beginnt nie als fremder Fresh-Arm.
        raise RecoveryBootblockArmError(
            f"Recovery-Bootblock-Aktivierung blieb unvollständig: {arm_exc}",
            prepared,
        ) from arm_exc


def _clear_recovery_bootblock_marker(contract: RecoveryBootblockContract) -> None:
    contract = _rebind_owned_recovery_dropins(contract, recreate_missing=False)
    # Vor dem Öffnen des Gates muss die aktuelle systemd-Sicht nach Restore
    # erneut beweisen, dass jede inzwischen geladene Unit exakt unsere eine
    # negative Marker-Condition nutzt. Ein beim Fresh-Arm fehlender optionaler
    # Dienst darf hier nicht still als weiterhin abwesend imputiert werden.
    _reload_and_verify_recovery_dropins(contract.units, expected_present=True)
    marker_payload = _recovery_bootblock_marker_payload(contract.transaction_id)
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        marker = _read_exact_root_file_at(
            state_descriptor,
            Path(RECOVERY_BOOTBLOCK_MARKER).name,
            marker_payload,
            0o600,
            allow_missing=False,
        )
        if marker is None:
            raise RuntimeError("Eigener Recovery-Bootblock-Marker fehlt")
        os.unlink(Path(RECOVERY_BOOTBLOCK_MARKER).name, dir_fd=state_descriptor)
        os.fsync(state_descriptor)
    finally:
        os.close(state_descriptor)
    _verify_recovery_bootblock_marker(contract, expected_present=False)
    _reload_and_verify_recovery_dropins(contract.units, expected_present=True)


def _remove_persistent_recovery_bootblock(
    contract: RecoveryBootblockContract,
) -> None:
    """Entfernt nur den exakt gebundenen Block nach erfolgreichem Endgate."""

    contract = _rebind_owned_recovery_dropins(contract, recreate_missing=False)
    identities = _validate_recovery_bootblock_contract(contract)
    _verify_recovery_bootblock_marker(contract, expected_present=False)
    systemd_descriptor = _open_directory_nofollow("/etc/systemd/system")
    try:
        _require_root_controlled_directory(
            systemd_descriptor,
            "/etc/systemd/system",
        )
        for unit in contract.units:
            directory_descriptor, _created = _open_or_create_recovery_directory(
                systemd_descriptor,
                f"{unit}.d",
                mode=0o755,
                label=_recovery_dropin_path(unit),
            )
            try:
                metadata = _read_exact_root_file_at(
                    directory_descriptor,
                    RECOVERY_BOOTBLOCK_DROPIN_NAME,
                    RECOVERY_BOOTBLOCK_DROPIN_PAYLOAD,
                    0o644,
                )
                if (metadata.st_dev, metadata.st_ino) != identities[unit]:
                    raise RuntimeError(
                        f"Fremdes Recovery-Bootblock-Drop-in wird nicht entfernt: {unit}"
                    )
                os.unlink(
                    RECOVERY_BOOTBLOCK_DROPIN_NAME,
                    dir_fd=directory_descriptor,
                )
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        for unit in reversed(contract.created_directories):
            try:
                os.rmdir(f"{unit}.d", dir_fd=systemd_descriptor)
            except OSError:
                pass
    finally:
        os.close(systemd_descriptor)
    _reload_and_verify_recovery_dropins(contract.units, expected_present=False)


def _update_safety_names(transaction_id: str) -> tuple[str, str, str]:
    """Leitet Unit, RuntimeDirectory und Token ausschließlich aus der txid ab."""

    value = str(transaction_id or "")
    if not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(value):
        raise RuntimeError("Update-Sicherheitsvertrag besitzt keine gültige Transaktions-ID")
    unit = f"{UPDATE_FINALIZER_UNIT_PREFIX}{value}.service"
    runtime = f"{UPDATE_FINALIZER_UNIT_PREFIX}{value}{UPDATE_FINALIZER_RUNTIME_SUFFIX}"
    token = f"/run/{runtime}/{UPDATE_FINALIZER_TOKEN_NAME}"
    return unit, runtime, token


def _render_update_safety_dropin(transaction_id: str) -> bytes:
    """Erzeugt das marker-/lease-gebundene Startgate für genau eine Transaktion."""

    unit, _runtime, token = _update_safety_names(transaction_id)
    return (
        "# E3DC_UPDATE_SAFETY_V1\n"
        "[Unit]\n"
        f"BindsTo={unit}\n"
        f"After={unit}\n"
        f"ConditionPathExists=|!{RECOVERY_BOOTBLOCK_MARKER}\n"
        f"ConditionPathExists=|{token}\n"
    ).encode("utf-8")


def _read_bound_root_file_at(
    parent_descriptor: int,
    name: str,
    *,
    maximum: int,
    mode: int,
    allow_missing: bool = False,
) -> tuple[bytes, os.stat_result] | None:
    """Liest eine variable root:root-Datei descriptor- und inodegebunden."""

    try:
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != 0
        or before.st_gid != 0
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_size < 1
        or before.st_size > int(maximum)
    ):
        raise RuntimeError(f"Root-Dateivertrag ist unsicher: {name}")
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        signature = _file_identity(opened)
        if signature != _file_identity(before) or _repo_descriptor_has_unsafe_xattrs(descriptor):
            raise RuntimeError(f"Root-Dateivertrag driftete beim Öffnen: {name}")
        payload = bytearray()
        while len(payload) <= int(maximum):
            block = os.read(descriptor, min(64 * 1024, int(maximum) + 1 - len(payload)))
            if not block:
                break
            payload.extend(block)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            len(payload) > int(maximum)
            or signature != _file_identity(after)
            or signature != _file_identity(named_after)
        ):
            raise RuntimeError(f"Root-Dateivertrag driftete beim Lesen: {name}")
        return bytes(payload), after
    finally:
        os.close(descriptor)


def _create_owned_exact_root_file_at(
    parent_descriptor: int,
    name: str,
    payload: bytes,
    mode: int,
) -> os.stat_result:
    """Installiert einen neuen Namen atomar; vorhandene Namen werden nie adoptiert."""

    temporary_name = f".e3dc-update-safety-{os.getpid()}-{secrets.token_hex(12)}"
    descriptor = None
    linked = False
    try:
        descriptor = os.open(
            temporary_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        view = memoryview(bytes(payload))
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("Update-Sicherheitsdatei konnte nicht vollständig geschrieben werden")
            view = view[written:]
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, int(mode))
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != int(mode)
            or metadata.st_size != len(payload)
            or _repo_descriptor_has_unsafe_xattrs(descriptor)
        ):
            raise RuntimeError("Update-Sicherheitsdatei verletzt den Root-Vertrag")
        os.link(
            temporary_name,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        linked = True
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        rebound = _read_exact_root_file_at(
            parent_descriptor,
            name,
            bytes(payload),
            int(mode),
        )
        if (rebound.st_dev, rebound.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError("Update-Sicherheitsdatei driftete nach linkat")
        return rebound
    except BaseException:
        if linked and descriptor is not None:
            try:
                current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
                opened = os.fstat(descriptor)
                if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                    os.unlink(name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
            except (FileNotFoundError, OSError):
                pass
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _update_safety_receipt_record(
    *,
    state: str,
    transaction_id: str,
    target_commit: str,
    target_tag: str,
    role: str,
    backup_receipt: RecoveryBackupReceipt,
    units: tuple[str, ...],
    created_directories: tuple[str, ...],
    dropin_identities: tuple[tuple[str, int, int], ...],
) -> dict:
    unit, runtime, token = _update_safety_names(transaction_id)
    payload = _render_update_safety_dropin(transaction_id)
    return {
        "schema": UPDATE_SAFETY_RECEIPT_SCHEMA,
        "state": state,
        "transaction_id": transaction_id,
        "target": {
            "commit": _validate_full_commit(target_commit),
            "tag": _normalize_release_tag(target_tag),
            "role": str(role),
        },
        "backup": {
            "dir": str(backup_receipt.backup_dir),
            "dev": int(backup_receipt.backup_dev),
            "ino": int(backup_receipt.backup_ino),
            "id": str(backup_receipt.backup_id),
            "manifest_sha256": str(backup_receipt.manifest_sha256),
        },
        "bootblock": {
            "units": list(units),
            "created_directories": list(created_directories),
            "dropin_payload_sha256": hashlib.sha256(payload).hexdigest(),
            "dropin_identities": [list(item) for item in dropin_identities],
        },
        "finalizer": {
            "unit": unit,
            "runtime_directory": runtime,
            "token_path": token,
        },
    }


def _canonical_update_safety_receipt_bytes(record: dict) -> bytes:
    return (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _parse_update_safety_receipt(
    payload: bytes,
    metadata: os.stat_result,
) -> UpdateSafetyContract:
    try:
        record = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Update-Sicherheitsreceipt ist nicht kanonisch lesbar") from exc
    if (
        not isinstance(record, dict)
        or _canonical_update_safety_receipt_bytes(record) != payload
        or set(record) != {
            "schema", "state", "transaction_id", "target", "backup", "bootblock", "finalizer"
        }
    ):
        raise RuntimeError("Update-Sicherheitsreceipt besitzt kein kanonisches Schema")
    transaction_id = str(record.get("transaction_id") or "")
    expected_unit, expected_runtime, expected_token = _update_safety_names(transaction_id)
    target = record.get("target")
    backup = record.get("backup")
    bootblock = record.get("bootblock")
    finalizer = record.get("finalizer")
    if (
        record.get("schema") != UPDATE_SAFETY_RECEIPT_SCHEMA
        or record.get("state") not in {"pending", "committed"}
        or not isinstance(target, dict)
        or set(target) != {"commit", "tag", "role"}
        or not isinstance(backup, dict)
        or set(backup) != {"dir", "dev", "ino", "id", "manifest_sha256"}
        or not isinstance(bootblock, dict)
        or set(bootblock) != {
            "units", "created_directories", "dropin_payload_sha256", "dropin_identities"
        }
        or not isinstance(finalizer, dict)
        or set(finalizer) != {"unit", "runtime_directory", "token_path"}
    ):
        raise RuntimeError("Update-Sicherheitsreceipt besitzt eine unbekannte Form")
    commit = _validate_full_commit(str(target.get("commit") or ""))
    tag = _normalize_release_tag(str(target.get("tag") or ""))
    role = str(target.get("role") or "")
    if role not in VALID_HA_ROLES:
        raise RuntimeError("Update-Sicherheitsreceipt besitzt eine ungültige Rolle")
    units = tuple(str(item) for item in bootblock.get("units") or ())
    created = tuple(str(item) for item in bootblock.get("created_directories") or ())
    try:
        identities = tuple(
            (str(item[0]), int(item[1]), int(item[2]))
            for item in (bootblock.get("dropin_identities") or ())
            if isinstance(item, list) and len(item) == 3
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Update-Sicherheitsreceipt besitzt ungültige Inodes") from exc
    identity_map = {unit: (device, inode) for unit, device, inode in identities}
    expected_payload_hash = hashlib.sha256(
        _render_update_safety_dropin(transaction_id)
    ).hexdigest()
    if (
        units != _recovery_bootblock_units()
        or len(set(units)) != len(units)
        or not set(created).issubset(units)
        or len(set(created)) != len(created)
        or len(identity_map) != len(identities)
        or set(identity_map) != set(units)
        or any(device < 0 or inode <= 0 for device, inode in identity_map.values())
        or bootblock.get("dropin_payload_sha256") != expected_payload_hash
        or finalizer.get("unit") != expected_unit
        or finalizer.get("runtime_directory") != expected_runtime
        or finalizer.get("token_path") != expected_token
        or not os.path.isabs(str(backup.get("dir") or ""))
        or int(backup.get("dev", -1)) < 0
        or int(backup.get("ino", 0)) <= 0
        or not str(backup.get("id") or "")
        or not re.fullmatch(r"[0-9a-f]{64}", str(backup.get("manifest_sha256") or ""))
    ):
        raise RuntimeError("Update-Sicherheitsreceipt driftete vom abgeleiteten Vertrag")
    return UpdateSafetyContract(
        schema=UPDATE_SAFETY_RECEIPT_SCHEMA,
        state=str(record["state"]),
        transaction_id=transaction_id,
        target_commit=commit,
        target_tag=tag,
        role=role,
        backup_dir=str(backup["dir"]),
        backup_dev=int(backup["dev"]),
        backup_ino=int(backup["ino"]),
        backup_id=str(backup["id"]),
        backup_manifest_sha256=str(backup["manifest_sha256"]),
        units=units,
        created_directories=created,
        dropin_identities=identities,
        dropin_payload_sha256=expected_payload_hash,
        finalizer_unit=expected_unit,
        runtime_directory=expected_runtime,
        token_path=expected_token,
        receipt_path=os.path.join(RECOVERY_BOOTBLOCK_STATE_DIR, UPDATE_SAFETY_RECEIPT_NAME),
        receipt_dev=int(metadata.st_dev),
        receipt_ino=int(metadata.st_ino),
        receipt_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _read_update_safety_contract(
    *,
    allow_missing: bool = False,
) -> UpdateSafetyContract | None:
    receipt_path = os.path.join(RECOVERY_BOOTBLOCK_STATE_DIR, UPDATE_SAFETY_RECEIPT_NAME)
    if not os.path.lexists(receipt_path):
        if allow_missing:
            return None
        raise RuntimeError("Update-Sicherheitsreceipt fehlt")
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        readback = _read_bound_root_file_at(
            state_descriptor,
            UPDATE_SAFETY_RECEIPT_NAME,
            maximum=256 * 1024,
            mode=0o600,
            allow_missing=allow_missing,
        )
        if readback is None:
            return None
        payload, metadata = readback
        return _parse_update_safety_receipt(payload, metadata)
    finally:
        os.close(state_descriptor)


def _validate_update_safety_contract(
    contract: UpdateSafetyContract,
    *,
    expected_state: str | None = None,
) -> UpdateSafetyContract:
    if not isinstance(contract, UpdateSafetyContract):
        raise RuntimeError("Update-Sicherheitsvertrag besitzt den falschen Typ")
    current = _read_update_safety_contract()
    if current != contract:
        raise RuntimeError("Update-Sicherheitsreceipt oder sein Inode driftete")
    if expected_state is not None and current.state != expected_state:
        raise RuntimeError(
            f"Update-Sicherheitsreceipt ist {current.state!r} statt {expected_state!r}"
        )
    return current


def _same_update_safety_transaction_shape(
    first: UpdateSafetyContract,
    second: UpdateSafetyContract,
) -> bool:
    """Vergleicht alles außer Zustand und atomar wechselnder Receipt-Identität."""

    if not isinstance(first, UpdateSafetyContract) or not isinstance(
        second,
        UpdateSafetyContract,
    ):
        return False
    ignored = {"state", "receipt_dev", "receipt_ino", "receipt_sha256"}
    return all(
        getattr(first, name) == getattr(second, name)
        for name in UpdateSafetyContract.__dataclass_fields__
        if name not in ignored
    )


def _write_pending_update_safety_receipt(record: dict) -> UpdateSafetyContract:
    payload = _canonical_update_safety_receipt_bytes(record)
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        metadata = _create_owned_exact_root_file_at(
            state_descriptor,
            UPDATE_SAFETY_RECEIPT_NAME,
            payload,
            0o600,
        )
        os.fsync(state_descriptor)
        return _parse_update_safety_receipt(payload, metadata)
    finally:
        os.close(state_descriptor)


def _replace_update_safety_receipt(
    contract: UpdateSafetyContract,
    record: dict,
) -> UpdateSafetyContract:
    """Ersetzt nur das exakt gebundene aktuelle Receipt atomar und fsync-sicher."""

    _validate_update_safety_contract(contract)
    payload = _canonical_update_safety_receipt_bytes(record)
    state_descriptor = _open_recovery_bootblock_state_directory()
    temporary_name = f".e3dc-update-receipt-{os.getpid()}-{secrets.token_hex(12)}"
    descriptor = None
    replaced = False
    irreversible_commit_boundary = False
    result: UpdateSafetyContract | None = None
    primary_error: BaseException | None = None
    try:
        current = _read_bound_root_file_at(
            state_descriptor,
            UPDATE_SAFETY_RECEIPT_NAME,
            maximum=256 * 1024,
            mode=0o600,
        )
        if current is None:
            raise RuntimeError("Update-Sicherheitsreceipt verschwand vor dem Ersatz")
        _current_payload, current_metadata = current
        if (current_metadata.st_dev, current_metadata.st_ino) != (
            contract.receipt_dev,
            contract.receipt_ino,
        ):
            raise RuntimeError("Update-Sicherheitsreceipt driftete vor dem Ersatz")
        descriptor = os.open(
            temporary_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=state_descriptor,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("Update-Sicherheitsreceipt blieb unvollständig")
            view = view[written:]
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        staged = os.fstat(descriptor)
        if (
            not stat.S_ISREG(staged.st_mode)
            or staged.st_nlink != 1
            or staged.st_uid != 0
            or staged.st_gid != 0
            or stat.S_IMODE(staged.st_mode) != 0o600
            or staged.st_size != len(payload)
            or _repo_descriptor_has_unsafe_xattrs(descriptor)
        ):
            raise RuntimeError("Gestagtes Update-Sicherheitsreceipt ist unsicher")
        # Ab diesem Punkt darf selbst ein scheinbarer rename-Fehler niemals
        # mehr zum Altpreimage-Restore führen: Ein Signal oder Plattformfehler
        # kann nach der Namensmutation, aber vor Python-Readback eintreffen.
        irreversible_commit_boundary = True
        os.replace(
            temporary_name,
            UPDATE_SAFETY_RECEIPT_NAME,
            src_dir_fd=state_descriptor,
            dst_dir_fd=state_descriptor,
        )
        replaced = True
        os.fsync(state_descriptor)
        rebound = _read_bound_root_file_at(
            state_descriptor,
            UPDATE_SAFETY_RECEIPT_NAME,
            maximum=256 * 1024,
            mode=0o600,
        )
        if rebound is None:
            raise RuntimeError("Update-Sicherheitsreceipt fehlt nach dem Ersatz")
        rebound_payload, rebound_metadata = rebound
        if (
            rebound_payload != payload
            or (rebound_metadata.st_dev, rebound_metadata.st_ino)
            != (staged.st_dev, staged.st_ino)
        ):
            raise RuntimeError("Update-Sicherheitsreceipt driftete nach dem Ersatz")
        result = _parse_update_safety_receipt(rebound_payload, rebound_metadata)
    except BaseException as exc:
        primary_error = exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException as close_exc:
                if primary_error is None:
                    primary_error = close_exc
                else:
                    update_logger.critical(
                        "Receipt-Staging-FD-Cleanup scheiterte zusätzlich: %s",
                        close_exc,
                    )
        if not replaced:
            try:
                os.unlink(temporary_name, dir_fd=state_descriptor)
            except FileNotFoundError:
                pass
            except BaseException as unlink_exc:
                if primary_error is None:
                    primary_error = unlink_exc
                else:
                    update_logger.critical(
                        "Receipt-Staging-Cleanup scheiterte zusätzlich: %s",
                        unlink_exc,
                    )
        try:
            os.close(state_descriptor)
        except BaseException as close_exc:
            if primary_error is None:
                primary_error = close_exc
            else:
                update_logger.critical(
                    "Receipt-Verzeichnis-FD-Cleanup scheiterte zusätzlich: %s",
                    close_exc,
                )
    if primary_error is not None:
        if irreversible_commit_boundary and not isinstance(
            primary_error,
            UpdateSafetyPostCommitError,
        ):
            raise UpdateSafetyPostCommitError(
                "Committed-Receipt-Grenze wurde betreten; Altstand-Rollback ist gesperrt"
            ) from primary_error
        raise primary_error
    if result is None:
        if irreversible_commit_boundary:
            raise UpdateSafetyPostCommitError(
                "Committed-Receipt-Grenze lieferte keinen gebundenen Abschluss"
            )
        raise RuntimeError("Receipt-Ersatz lieferte kein Ergebnis")
    return result


def _remove_exact_update_safety_receipt(contract: UpdateSafetyContract) -> None:
    _validate_update_safety_contract(contract)
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        current = _read_bound_root_file_at(
            state_descriptor,
            UPDATE_SAFETY_RECEIPT_NAME,
            maximum=256 * 1024,
            mode=0o600,
        )
        if current is None or (current[1].st_dev, current[1].st_ino) != (
            contract.receipt_dev,
            contract.receipt_ino,
        ):
            raise RuntimeError("Fremdes Update-Sicherheitsreceipt wird nicht entfernt")
        os.unlink(UPDATE_SAFETY_RECEIPT_NAME, dir_fd=state_descriptor)
        os.fsync(state_descriptor)
    finally:
        os.close(state_descriptor)
    if os.path.lexists(contract.receipt_path):
        raise RuntimeError("Update-Sicherheitsreceipt blieb nach unlink vorhanden")


def _rebind_update_safety_dropins(
    contract: UpdateSafetyContract,
    *,
    allow_missing: bool = False,
) -> dict[str, tuple[int, int] | None]:
    identities = {
        unit: (device, inode)
        for unit, device, inode in contract.dropin_identities
    }
    payload = _render_update_safety_dropin(contract.transaction_id)
    rebound: dict[str, tuple[int, int] | None] = {}
    systemd_descriptor = _open_directory_nofollow("/etc/systemd/system")
    try:
        _require_root_controlled_directory(systemd_descriptor, "/etc/systemd/system")
        for unit in contract.units:
            directory_descriptor = None
            try:
                directory_descriptor = os.open(
                    f"{unit}.d",
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=systemd_descriptor,
                )
                _require_root_controlled_directory(
                    directory_descriptor,
                    _recovery_dropin_path(unit),
                    0o755,
                )
                metadata = _read_exact_root_file_at(
                    directory_descriptor,
                    RECOVERY_BOOTBLOCK_DROPIN_NAME,
                    payload,
                    0o644,
                    allow_missing=allow_missing,
                )
                if metadata is None:
                    rebound[unit] = None
                    continue
                current_identity = (int(metadata.st_dev), int(metadata.st_ino))
                if identities.get(unit) != current_identity:
                    raise RuntimeError(
                        f"Dynamischer 00-Inode wurde nicht von dieser Transaktion erzeugt: {unit}"
                    )
                rebound[unit] = current_identity
            except FileNotFoundError:
                if not allow_missing:
                    raise RuntimeError(f"Dynamischer 00-Pfad fehlt: {unit}")
                rebound[unit] = None
            finally:
                if directory_descriptor is not None:
                    os.close(directory_descriptor)
    finally:
        os.close(systemd_descriptor)
    return rebound


def _update_safety_expected_dropins(
    contract: UpdateSafetyContract,
    *,
    selected_units: tuple[str, ...] | None = None,
) -> dict[str, dict[str, dict[str, object]]]:
    """Projiziert den gebundenen 00-Inodevertrag für Unit-Bundle-Prüfer."""

    _validate_update_safety_contract(contract)
    identities = {
        unit: (device, inode)
        for unit, device, inode in contract.dropin_identities
    }
    payload = _render_update_safety_dropin(contract.transaction_id)
    selected = tuple(selected_units or contract.units)
    if any(unit not in identities for unit in selected):
        raise RuntimeError("Update-Sicherheits-Drop-in-Auswahl ist nicht gebunden")
    return {
        unit: {
            _recovery_dropin_path(unit): {
                "bytes": payload,
                "dev": identities[unit][0],
                "ino": identities[unit][1],
                "uid": 0,
                "gid": 0,
                "mode": 0o644,
                "nlink": 1,
                "size": len(payload),
            }
        }
        for unit in selected
    }


def _update_safety_record_from_contract(
    contract: UpdateSafetyContract,
    *,
    state: str | None = None,
    created_directories: tuple[str, ...] | None = None,
    dropin_identities: tuple[tuple[str, int, int], ...] | None = None,
) -> dict:
    return {
        "schema": UPDATE_SAFETY_RECEIPT_SCHEMA,
        "state": str(state or contract.state),
        "transaction_id": contract.transaction_id,
        "target": {
            "commit": contract.target_commit,
            "tag": contract.target_tag,
            "role": contract.role,
        },
        "backup": {
            "dir": contract.backup_dir,
            "dev": contract.backup_dev,
            "ino": contract.backup_ino,
            "id": contract.backup_id,
            "manifest_sha256": contract.backup_manifest_sha256,
        },
        "bootblock": {
            "units": list(contract.units),
            "created_directories": list(
                contract.created_directories
                if created_directories is None
                else created_directories
            ),
            "dropin_payload_sha256": contract.dropin_payload_sha256,
            "dropin_identities": [
                list(item)
                for item in (
                    contract.dropin_identities
                    if dropin_identities is None
                    else dropin_identities
                )
            ],
        },
        "finalizer": {
            "unit": contract.finalizer_unit,
            "runtime_directory": contract.runtime_directory,
            "token_path": contract.token_path,
        },
    }


def _remove_owned_update_safety_dropins(
    *,
    units: tuple[str, ...],
    identities: dict[str, tuple[int, int]],
    created_directories: tuple[str, ...],
    payload: bytes,
    allow_missing: bool,
) -> None:
    """Entfernt nur explizit erzeugte, unveränderte 00-Inodes."""

    systemd_descriptor = _open_directory_nofollow("/etc/systemd/system")
    try:
        _require_root_controlled_directory(systemd_descriptor, "/etc/systemd/system")
        for unit in reversed(units):
            expected_identity = identities.get(unit)
            if expected_identity is None:
                continue
            directory_descriptor = None
            try:
                directory_descriptor = os.open(
                    f"{unit}.d",
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=systemd_descriptor,
                )
                _require_root_controlled_directory(
                    directory_descriptor,
                    _recovery_dropin_path(unit),
                    0o755,
                )
                metadata = _read_exact_root_file_at(
                    directory_descriptor,
                    RECOVERY_BOOTBLOCK_DROPIN_NAME,
                    payload,
                    0o644,
                    allow_missing=allow_missing,
                )
                if metadata is None:
                    continue
                if (metadata.st_dev, metadata.st_ino) != expected_identity:
                    raise RuntimeError(
                        f"Fremder dynamischer 00-Inode wird nicht entfernt: {unit}"
                    )
                os.unlink(
                    RECOVERY_BOOTBLOCK_DROPIN_NAME,
                    dir_fd=directory_descriptor,
                )
                os.fsync(directory_descriptor)
            except FileNotFoundError:
                if not allow_missing:
                    raise
            finally:
                if directory_descriptor is not None:
                    os.close(directory_descriptor)
        for unit in reversed(created_directories):
            try:
                os.rmdir(f"{unit}.d", dir_fd=systemd_descriptor)
                os.fsync(systemd_descriptor)
            except OSError:
                pass
    finally:
        os.close(systemd_descriptor)


def _prepare_update_safety_contract(
    *,
    transaction_id: str,
    target_commit: str,
    target_tag: str,
    role: str,
    backup_receipt: RecoveryBackupReceipt,
) -> UpdateSafetyContract:
    """Installiert 00-Inodes und persistiert danach das pending Receipt."""

    if os.geteuid() != 0:
        raise RuntimeError("Vor-Mutations-Sicherheitsvertrag benötigt Root")
    if (
        not isinstance(backup_receipt, RecoveryBackupReceipt)
        or backup_receipt.transaction_id != transaction_id
    ):
        raise RuntimeError("Backup-Receipt ist nicht an die Update-Transaktion gebunden")
    _assert_no_existing_recovery_bootblock()
    units = _recovery_bootblock_units()
    payload = _render_update_safety_dropin(transaction_id)
    identities: dict[str, tuple[int, int]] = {}
    created_directories: list[str] = []
    systemd_descriptor = _open_directory_nofollow("/etc/systemd/system")
    try:
        _require_root_controlled_directory(systemd_descriptor, "/etc/systemd/system")
        for unit in units:
            directory_descriptor, created = _open_or_create_recovery_directory(
                systemd_descriptor,
                f"{unit}.d",
                mode=0o755,
                label=_recovery_dropin_path(unit),
            )
            if created:
                created_directories.append(unit)
            try:
                if _read_exact_root_file_at(
                    directory_descriptor,
                    RECOVERY_BOOTBLOCK_DROPIN_NAME,
                    payload,
                    0o644,
                    allow_missing=True,
                ) is not None:
                    raise RuntimeError(
                        f"Vorhandener dynamischer 00-Name wird nicht adoptiert: {unit}"
                    )
                metadata = _create_owned_exact_root_file_at(
                    directory_descriptor,
                    RECOVERY_BOOTBLOCK_DROPIN_NAME,
                    payload,
                    0o644,
                )
                identities[unit] = (int(metadata.st_dev), int(metadata.st_ino))
            finally:
                os.close(directory_descriptor)
    except BaseException:
        try:
            _remove_owned_update_safety_dropins(
                units=units,
                identities=identities,
                created_directories=tuple(created_directories),
                payload=payload,
                allow_missing=True,
            )
        except Exception as cleanup_exc:
            update_logger.critical(
                "Partieller Vor-Mutations-00-Vertrag blieb fail-closed liegen: %s",
                cleanup_exc,
            )
        raise
    finally:
        os.close(systemd_descriptor)
    try:
        record = _update_safety_receipt_record(
            state="pending",
            transaction_id=transaction_id,
            target_commit=target_commit,
            target_tag=target_tag,
            role=role,
            backup_receipt=backup_receipt,
            units=units,
            created_directories=tuple(created_directories),
            dropin_identities=tuple(
                (unit, *identities[unit]) for unit in units
            ),
        )
        contract = _write_pending_update_safety_receipt(record)
        _rebind_update_safety_dropins(contract)
        return contract
    except BaseException:
        try:
            _remove_owned_update_safety_dropins(
                units=units,
                identities=identities,
                created_directories=tuple(created_directories),
                payload=payload,
                allow_missing=True,
            )
        except Exception as cleanup_exc:
            update_logger.critical(
                "Vor-Mutations-00-Vertrag blieb nach Receiptfehler liegen: %s",
                cleanup_exc,
            )
        raise


def _verify_update_safety_marker(
    contract: UpdateSafetyContract,
    *,
    expected_present: bool,
) -> None:
    payload = _recovery_bootblock_marker_payload(contract.transaction_id)
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        present = _read_exact_root_file_at(
            state_descriptor,
            Path(RECOVERY_BOOTBLOCK_MARKER).name,
            payload,
            0o600,
            allow_missing=True,
        ) is not None
        if present != bool(expected_present):
            raise RuntimeError("Dynamischer Update-Marker besitzt den falschen Zustand")
    finally:
        os.close(state_descriptor)


def _systemd_scalar_value(unit: str, property_name: str) -> str:
    result = _run_argv(
        ["systemctl", "show", f"--property={property_name}", "--value", unit],
        timeout=15,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )
    output = str(result.get("stdout") or "")
    if (
        not result.get("success")
        or result.get("timed_out")
        or int(result.get("returncode", -1)) != 0
        or str(result.get("stderr") or "")
        or "\x00" in output
        or len(output.splitlines()) > 1
    ):
        raise RuntimeError(f"systemd-{property_name}-Readback ist unklar: {unit}")
    return output.strip()


def _reload_and_verify_update_safety_dropins(
    contract: UpdateSafetyContract,
    *,
    expected_present: bool,
) -> None:
    """Bindet daemon-reload, Condition-OR und Lease-Abhängigkeiten gemeinsam."""

    if expected_present:
        _rebind_update_safety_dropins(contract)
    reload_result = _run_argv(
        ["systemctl", "daemon-reload"],
        timeout=30,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )
    if (
        not reload_result.get("success")
        or reload_result.get("timed_out")
        or int(reload_result.get("returncode", -1)) != 0
        or str(reload_result.get("stderr") or "")
    ):
        raise RuntimeError(
            "systemd daemon-reload für dynamischen Update-Bootblock fehlgeschlagen: "
            + _combined_process_diagnostics(reload_result, maximum=800)
        )
    payload = _render_update_safety_dropin(contract.transaction_id)
    marker_comment = "# E3DC_UPDATE_SAFETY_V1"
    own_marker_condition = f"|!{RECOVERY_BOOTBLOCK_MARKER}"
    own_token_condition = f"|{contract.token_path}"
    for unit in contract.units:
        load_state = _systemd_scalar_value(unit, "LoadState").lower()
        dropin_value = _systemd_scalar_value(unit, "DropInPaths")
        try:
            dropin_paths = tuple(shlex.split(dropin_value)) if dropin_value else ()
            binds_to = tuple(shlex.split(_systemd_scalar_value(unit, "BindsTo")))
            after = tuple(shlex.split(_systemd_scalar_value(unit, "After")))
        except ValueError as exc:
            raise RuntimeError(f"systemd-Abhängigkeiten sind unklar: {unit}") from exc
        cat_result = _run_argv(
            ["systemctl", "cat", "--no-pager", unit],
            timeout=15,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        output = str(cat_result.get("stdout") or "")
        stderr = str(cat_result.get("stderr") or "")
        canonical_not_found = bool(
            not cat_result.get("success")
            and not cat_result.get("timed_out")
            and int(cat_result.get("returncode", -1)) == 1
            and output == ""
            and stderr == f"No files found for {unit}.\n"
        )
        conditions: list[tuple[str, str]] = []
        ambiguous = False
        condition_reset_seen = False
        for line in output.splitlines():
            match = re.fullmatch(r"\s*(Condition[A-Za-z0-9]+)\s*=(.*)", line)
            stripped = line.lstrip()
            if stripped.startswith("Condition") and match is None:
                ambiguous = True
            if match:
                condition_name = match.group(1)
                value = match.group(2).strip()
                if value.endswith("\\"):
                    ambiguous = True
                if value:
                    conditions.append((condition_name, value))
                else:
                    condition_reset_seen = True
                    conditions = [
                        condition
                        for condition in conditions
                        if condition[0] != condition_name
                    ]
        own_path = _recovery_dropin_path(unit)
        if expected_present:
            if load_state == "masked":
                if _systemd_scalar_value(unit, "UnitFileState").lower() != "masked":
                    raise RuntimeError(f"Nur persistente Maskierung gilt als Startblock: {unit}")
                continue
            if load_state == "not-found":
                if not canonical_not_found or dropin_paths or binds_to or conditions or ambiguous:
                    raise RuntimeError(f"Inkonsistenter not-found-Bootblock: {unit}")
                _assert_exclusive_not_found_recovery_dropin(
                    unit,
                    expected_payload=payload,
                )
                continue
            if (
                load_state != "loaded"
                or not cat_result.get("success")
                or cat_result.get("timed_out")
                or int(cat_result.get("returncode", -1)) != 0
                or stderr
                or dropin_paths.count(own_path) != 1
                or output.splitlines().count(marker_comment) != 1
                or output.splitlines().count(f"BindsTo={contract.finalizer_unit}") != 1
                or output.splitlines().count(f"After={contract.finalizer_unit}") != 1
                or binds_to.count(contract.finalizer_unit) != 1
                or after.count(contract.finalizer_unit) != 1
                or conditions.count(
                    ("ConditionPathExists", own_marker_condition)
                )
                != 1
                or conditions.count(
                    ("ConditionPathExists", own_token_condition)
                )
                != 1
                or [
                    (condition_name, condition_value)
                    for condition_name, condition_value in conditions
                    if condition_value.startswith("|")
                ]
                != [
                    ("ConditionPathExists", own_marker_condition),
                    ("ConditionPathExists", own_token_condition),
                ]
                or condition_reset_seen
                or ambiguous
            ):
                raise RuntimeError(f"Dynamischer Update-Bootblock driftete: {unit}")
        else:
            if load_state not in {"loaded", "masked", "not-found"}:
                raise RuntimeError(f"systemd-Zustand nach dynamischem Cleanup ist unklar: {unit}")
            if (
                own_path in dropin_paths
                or marker_comment in output.splitlines()
                or contract.finalizer_unit in binds_to
                or contract.finalizer_unit in after
                or ("ConditionPathExists", own_marker_condition) in conditions
                or ("ConditionPathExists", own_token_condition) in conditions
                or ambiguous
                or (load_state == "not-found" and not canonical_not_found)
                or (
                    load_state == "loaded"
                    and (
                        not cat_result.get("success")
                        or cat_result.get("timed_out")
                        or int(cat_result.get("returncode", -1)) != 0
                        or stderr
                    )
                )
            ):
                raise RuntimeError(f"Dynamischer Update-Bootblock blieb effektiv: {unit}")


def _arm_update_safety_contract(
    contract: UpdateSafetyContract,
) -> UpdateSafetyContract:
    contract = _validate_update_safety_contract(contract, expected_state="pending")
    _rebind_update_safety_dropins(contract)
    marker_payload = _recovery_bootblock_marker_payload(contract.transaction_id)
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        _create_exact_root_file_at(
            state_descriptor,
            Path(RECOVERY_BOOTBLOCK_MARKER).name,
            marker_payload,
            0o600,
        )
        os.fsync(state_descriptor)
    finally:
        os.close(state_descriptor)
    _verify_update_safety_marker(contract, expected_present=True)
    _reload_and_verify_update_safety_dropins(contract, expected_present=True)
    return contract


def _clear_update_safety_marker(contract: UpdateSafetyContract) -> None:
    _validate_update_safety_contract(contract)
    _rebind_update_safety_dropins(contract)
    _reload_and_verify_update_safety_dropins(contract, expected_present=True)
    marker_payload = _recovery_bootblock_marker_payload(contract.transaction_id)
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        marker = _read_exact_root_file_at(
            state_descriptor,
            Path(RECOVERY_BOOTBLOCK_MARKER).name,
            marker_payload,
            0o600,
        )
        if marker is None:
            raise RuntimeError("Eigener dynamischer Update-Marker fehlt")
        os.unlink(Path(RECOVERY_BOOTBLOCK_MARKER).name, dir_fd=state_descriptor)
        os.fsync(state_descriptor)
    finally:
        os.close(state_descriptor)
    _verify_update_safety_marker(contract, expected_present=False)


def _remove_update_safety_dropins(contract: UpdateSafetyContract) -> None:
    _validate_update_safety_contract(contract)
    _verify_update_safety_marker(contract, expected_present=False)
    identities = {
        unit: (device, inode)
        for unit, device, inode in contract.dropin_identities
    }
    _remove_owned_update_safety_dropins(
        units=contract.units,
        identities=identities,
        created_directories=contract.created_directories,
        payload=_render_update_safety_dropin(contract.transaction_id),
        allow_missing=False,
    )
    _reload_and_verify_update_safety_dropins(contract, expected_present=False)


def _commit_update_safety_receipt(
    contract: UpdateSafetyContract,
) -> UpdateSafetyContract:
    contract = _validate_update_safety_contract(contract, expected_state="pending")
    _verify_update_safety_marker(contract, expected_present=True)
    _reload_and_verify_update_safety_dropins(contract, expected_present=True)
    return _replace_update_safety_receipt(
        contract,
        _update_safety_record_from_contract(contract, state="committed"),
    )


def _finish_committed_update_safety_cleanup(
    contract: UpdateSafetyContract,
    *,
    remove_receipt: bool,
) -> UpdateSafetyContract:
    """Räumt nach Commit nur eigene Marker/00-Inodes; niemals Produktpreimages."""

    current = _read_update_safety_contract()
    if current is None or contract.state != "committed" or current != contract:
        raise RuntimeError("Committed Update-Sicherheitsreceipt driftete")
    # Vor dem Öffnen des Markergates müssen alle noch vorhandenen eigenen
    # 00-Namen exakt den Receipt-Inodes und -Bytes entsprechen. Ein fremder
    # Ersatz darf niemals erst nach Marker-Unlink erkannt werden.
    _rebind_update_safety_dropins(contract, allow_missing=True)
    marker_payload = _recovery_bootblock_marker_payload(contract.transaction_id)
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        marker = _read_exact_root_file_at(
            state_descriptor,
            Path(RECOVERY_BOOTBLOCK_MARKER).name,
            marker_payload,
            0o600,
            allow_missing=True,
        )
        if marker is not None:
            os.unlink(Path(RECOVERY_BOOTBLOCK_MARKER).name, dir_fd=state_descriptor)
            os.fsync(state_descriptor)
    finally:
        os.close(state_descriptor)
    _verify_update_safety_marker(contract, expected_present=False)
    _remove_owned_update_safety_dropins(
        units=contract.units,
        identities={
            unit: (device, inode)
            for unit, device, inode in contract.dropin_identities
        },
        created_directories=contract.created_directories,
        payload=_render_update_safety_dropin(contract.transaction_id),
        allow_missing=True,
    )
    _reload_and_verify_update_safety_dropins(contract, expected_present=False)
    if remove_receipt:
        _remove_exact_update_safety_receipt(contract)
    return contract


def _rearm_pending_update_safety_contract(
    contract: UpdateSafetyContract,
) -> UpdateSafetyContract:
    """Vervollständigt denselben tx-/payloadgebundenen pending Vertrag erneut."""

    # Dieser Contract wurde bereits am Managed-Finalizer-Endgate vollständig
    # gebunden. Ein atomar ersetztes, nur ähnlich geformtes Receipt darf hier
    # weder neue 00-Inodes noch einen Marker, Stop oder Restore autorisieren.
    current = _validate_update_safety_contract(
        contract,
        expected_state="pending",
    )
    if current != contract:
        raise RuntimeError(
            "Pending Update-Sicherheitsvertrag kann nicht exakt rearmed werden"
        )
    payload = _render_update_safety_dropin(current.transaction_id)
    prior_identities = {
        unit: (device, inode)
        for unit, device, inode in current.dropin_identities
    }
    identities: dict[str, tuple[int, int]] = {}
    created = list(current.created_directories)
    systemd_descriptor = _open_directory_nofollow("/etc/systemd/system")
    try:
        _require_root_controlled_directory(systemd_descriptor, "/etc/systemd/system")
        for unit in current.units:
            directory_descriptor, directory_created = _open_or_create_recovery_directory(
                systemd_descriptor,
                f"{unit}.d",
                mode=0o755,
                label=_recovery_dropin_path(unit),
            )
            if directory_created and unit not in created:
                created.append(unit)
            try:
                metadata = _read_exact_root_file_at(
                    directory_descriptor,
                    RECOVERY_BOOTBLOCK_DROPIN_NAME,
                    payload,
                    0o644,
                    allow_missing=True,
                )
                was_missing = metadata is None
                if was_missing:
                    metadata = _create_owned_exact_root_file_at(
                        directory_descriptor,
                        RECOVERY_BOOTBLOCK_DROPIN_NAME,
                        payload,
                        0o644,
                    )
                identity = (int(metadata.st_dev), int(metadata.st_ino))
                prior = prior_identities.get(unit)
                if prior is not None and prior != identity and not was_missing:
                    # Ein fehlender alter Name darf neu erzeugt werden. Ein
                    # weiterhin vorhandener anderer Inode wurde oben bereits
                    # bytegebunden gelesen, ist aber nicht unser Eigentum.
                    raise RuntimeError(
                        f"Dynamischer 00-Inode driftete beim Re-Arm: {unit}"
                    )
                identities[unit] = identity
            finally:
                os.close(directory_descriptor)
    finally:
        os.close(systemd_descriptor)
    new_identities = tuple((unit, *identities[unit]) for unit in current.units)
    if (
        new_identities != current.dropin_identities
        or tuple(created) != current.created_directories
    ):
        current = _replace_update_safety_receipt(
            current,
            _update_safety_record_from_contract(
                current,
                state="pending",
                created_directories=tuple(created),
                dropin_identities=new_identities,
            ),
        )
    return _arm_update_safety_contract(current)


def _enforce_update_safety_fail_closed(
    contract: UpdateSafetyContract,
    *,
    repo_dir: str,
) -> UpdateSafetyContract:
    """Rearmed denselben Vertrag und beweist danach Writer-Ruhe ohne Start."""

    current = _rearm_pending_update_safety_contract(contract)
    if not _stop_v4_services(V4_SERVICES):
        raise RuntimeError("Dynamischer Fail-closed-Stop blieb unvollständig")
    _assert_strict_update_writer_quiescence(
        repo_dir=repo_dir,
        transaction_id=current.transaction_id,
    )
    _verify_update_safety_marker(current, expected_present=True)
    _reload_and_verify_update_safety_dropins(current, expected_present=True)
    return current


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
    "get_shadow_snapshot.php",
    "solar.js",
    "solar.min.js",
)


def _unit_name(service: str) -> str:
    name = str(service).strip()
    return name if name.endswith('.service') else f'{name}.service'


def _systemd_activity_readback_matches(result: dict, *, should_be_active: bool) -> bool:
    """Bindet is-active an stdout, rc, stderr und Timeoutstatus gemeinsam."""

    activity = str(result.get("stdout") or "").strip().lower()
    stderr = str(result.get("stderr") or "")
    returncode = int(result.get("returncode", -1))
    if result.get("timed_out") or stderr:
        return False
    if should_be_active:
        return bool(result.get("success")) and returncode == 0 and activity == "active"
    return (
        not result.get("success")
        and returncode == 3
        and activity in {"inactive", "failed"}
    )


def _service_unit_exists(service: str) -> bool:
    unit = _unit_name(service)
    return any(os.path.exists(os.path.join(unit_dir, unit)) for unit_dir in SYSTEMD_UNIT_DIRS)


SYSTEMD_KNOWN_UNIT_FILE_STATES = {
    "enabled", "enabled-runtime", "disabled", "static", "indirect",
    "generated", "transient", "alias", "linked", "linked-runtime",
    "masked", "masked-runtime", "not-found",
}


def _systemd_state_from_result(result: dict, allowed_states) -> str:
    """Extrahiert ausschließlich einen kanonischen systemd-Zustandswert."""

    allowed = set(allowed_states)
    for stream in ("stdout", "stderr"):
        for line in str(result.get(stream) or "").splitlines():
            value = line.strip().lower()
            if value in allowed:
                return value
    return ""


def _command_result_diagnostic(result: dict) -> str:
    """Bewahrt stdout, stderr und Returncode für eine konkrete Fehleranalyse."""

    return (
        f"stdout={str(result.get('stdout') or '')!r}, "
        f"stderr={str(result.get('stderr') or '')!r}, "
        f"rc={result.get('returncode')!r}"
    )


def _command_timed_out(result: dict) -> bool:
    """Erkennt ausschließlich den kanonischen Timeout des Installer-Runners."""

    return (
        result.get("returncode") == -1
        and str(result.get("stderr") or "").strip() == "Timeout"
    )


def _systemd_show_end_state(
    service: str,
    *,
    timeout_s: int = 10,
) -> tuple[str, str, str, dict]:
    """Liest den kanonischen Lade-, Enablement- und Aktivzustand einer Unit."""

    result = _run_argv(
        [
            "systemctl",
            "show",
            _unit_name(service),
            "--no-pager",
            "--property=LoadState",
            "--property=UnitFileState",
            "--property=ActiveState",
        ],
        timeout=max(1, int(timeout_s)),
    )
    values = {}
    if result.get("success"):
        for line in str(result.get("stdout") or "").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip()] = value.strip().lower()
    return (
        values.get("LoadState", ""),
        values.get("UnitFileState", ""),
        values.get("ActiveState", ""),
        result,
    )


def _capture_transition_unit_activity(
    unit: str,
    *,
    piguard_contract: bool = False,
) -> str:
    """Erfasst den Altbetrieb, ohne seine Unitform zur Zielautorität zu machen.

    Unterschiedliche Community-Systeme besitzen historisch reguläre, maskierte,
    deaktivierte oder beschädigte Produktunits. Diese Form wird nach dem Backup
    durch den Zielrelease normalisiert. Der Preflight muss deshalb nur sicher
    lesen können, ob ein bekannter Writer lief; die endgültige Aktorruhe wird
    später separat über ``inactive/dead/MainPID=0`` bewiesen.
    """

    unit_name = _unit_name(unit)
    property_names = (
        "LoadState",
        "ActiveState",
        "SubState",
        "UnitFileState",
        "FragmentPath",
    )
    result = _run_argv(
        [
            "systemctl",
            "show",
            "--no-pager",
            *(f"--property={name}" for name in property_names),
            unit_name,
        ],
        timeout=10,
    )
    stdout = str(result.get("stdout") or "")
    if (
        not result.get("success")
        or result.get("timed_out")
        or int(result.get("returncode", -1)) != 0
        or str(result.get("stderr") or "")
        or "\x00" in stdout
    ):
        raise RuntimeError(f"Betriebszustand von {unit_name} ist nicht lesbar")

    values: dict[str, str] = {}
    expected = set(property_names)
    for raw_line in stdout.splitlines():
        if not raw_line:
            continue
        key, separator, value = raw_line.partition("=")
        if (
            separator != "="
            or key not in expected
            or key in values
            or value != value.strip()
        ):
            raise RuntimeError(f"Betriebszustand von {unit_name} ist widersprüchlich")
        values[key] = value
    if set(values) != expected:
        raise RuntimeError(f"Betriebszustand von {unit_name} ist unvollständig")

    load_state = values["LoadState"].lower()
    active_state = values["ActiveState"].lower()
    sub_state = values["SubState"].lower()
    unit_file_state = values["UnitFileState"].lower()
    fragment_path = values["FragmentPath"]
    if (
        load_state == "not-found"
        and active_state == "inactive"
        and sub_state == "dead"
        and unit_file_state in {"", "not-found"}
        and fragment_path == ""
    ):
        return "absent"
    # Ein laufender oder gerade wechselnder bekannter Writer wird konservativ
    # als aktiv aufgenommen und anschließend vom zentralen Stopgate beendet.
    if active_state in {
        "active",
        "activating",
        "deactivating",
        "reloading",
    }:
        return "active"
    if active_state == "inactive":
        return "inactive"
    if active_state == "failed":
        return "failed"
    raise RuntimeError(
        f"Betriebszustand von {unit_name} ist nicht lesbar "
        f"(load={load_state or '-'}, active={active_state or '-'}, "
        f"sub={sub_state or '-'}, "
        f"unit_file={unit_file_state or '-'}, fragment={fragment_path or '-'})"
    )


def _capture_piguard_transition_activity() -> str:
    """Bindet PiGuard loaded/enabled/exact-fragment oder exakt absent."""

    return _capture_transition_unit_activity(
        PIGUARD_UNIT,
        piguard_contract=True,
    )


def _wait_for_systemd_end_state(
    service: str,
    *,
    timeout_s: int = SYSTEMD_SETTLE_TIMEOUT_S,
    poll_s: int = SYSTEMD_SETTLE_POLL_S,
) -> tuple[bool, tuple[str, str, str], dict]:
    """Gibt einem nach Timeout noch konvergierenden Dienst ein begrenztes Fenster."""

    deadline = time.monotonic() + max(0, int(timeout_s))
    last_state = ("", "", "")
    last_result = {
        "success": False,
        "stdout": "",
        "stderr": "kein Endzustand gelesen",
        "returncode": -1,
    }
    while True:
        remaining_before_probe = deadline - time.monotonic()
        if remaining_before_probe <= 0:
            return False, last_state, last_result
        load_state, unit_file_state, active_state, last_result = _systemd_show_end_state(
            service,
            timeout_s=max(1, min(10, math.ceil(remaining_before_probe))),
        )
        last_state = (load_state, unit_file_state, active_state)
        if last_state == ("loaded", "enabled", "active"):
            return True, last_state, last_result
        if load_state in {"not-found", "masked"} or active_state == "failed":
            return False, last_state, last_result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, last_state, last_result
        time.sleep(min(max(1, int(poll_s)), remaining))


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


FIRST_INSTALL_METADATA_KEYS = frozenset({
    "install_user",
    "home_dir",
    "install_path",
    "venv_name",
    "venv_path",
})


def classify_installation_state(config_path: str = HA_CONFIG_PATH) -> tuple[str, str]:
    """Classify the local installation without guessing an existing role.

    Only the exact, product-created default configuration without any E3DC
    installation marker is a first installation. A complete legacy
    installation needs the two Web entrypoints, every install-center core
    service and a valid HA/Shadow role. Incomplete but safely bound states are
    resumable; unreadable or unbound states remain blocked.
    """

    try:
        installed_units = tuple(
            unit
            for unit in (*_catalog_units_strict(), "e3dc.service")
            if _service_unit_exists(unit)
        )
    except Exception as exc:
        return "blocked", f"E3DC-Dienstkatalog ist nicht sicher prüfbar: {exc}"
    web_program_paths = (
        "/var/www/html/index.php",
        "/var/www/html/helpers.php",
    )
    present_web_files = tuple(path for path in web_program_paths if os.path.exists(path))
    missing_web_files = tuple(path for path in web_program_paths if path not in present_web_files)
    missing_core_units = tuple(
        _unit_name(service)
        for service in INSTALL_CENTER_CORE_SERVICES
        if not _service_unit_exists(service)
    )
    has_installation_marker = bool(installed_units or present_web_files)
    try:
        config, _raw = _read_json_nofollow(config_path)
    except FileNotFoundError:
        if has_installation_marker:
            return "blocked", "V4-Konfiguration fehlt, obwohl Installationsbestandteile vorhanden sind"
        return "fresh", "keine V4-Konfiguration und keine Installationsbestandteile vorhanden"
    except Exception as exc:
        return "blocked", f"V4-Konfiguration ist nicht sicher lesbar: {exc}"

    allowed_keys = set(FIRST_INSTALL_METADATA_KEYS) | set(WEB_CONFIG_START_DEFAULTS) | {"ha_mode"}
    is_default_config = set(config).issubset(allowed_keys)
    if is_default_config:
        for key, default in WEB_CONFIG_START_DEFAULTS.items():
            if config.get(key, default) != default:
                is_default_config = False
                break
    default_role = str(config.get("ha_mode") or "").strip().lower()
    if is_default_config and default_role not in {"", "off"}:
        is_default_config = False

    if not has_installation_marker and is_default_config:
        return "fresh", "nur die unveränderte Startkonfiguration ist vorhanden"

    role = str(config.get("ha_mode") or "").strip().lower()
    if role not in VALID_HA_ROLES:
        return "blocked", "bestehende Betriebskonfiguration enthält keine gültige HA-/Shadow-Rolle"

    if not missing_web_files and not missing_core_units:
        return "ready", "vollständige Installation mit gebundener HA-/Shadow-Rolle"

    missing = [
        *(f"Webdatei fehlt: {path}" for path in missing_web_files),
        *(f"Kerndienst fehlt: {unit}" for unit in missing_core_units),
    ]
    return "partial", "unvollständige Installation: " + "; ".join(missing)


def start_installation_or_update(
    *,
    allow_first_install: bool,
    headless: bool = False,
    reinstall_current: bool = False,
):
    """Route menu and direct update entrypoints through one conservative gate."""

    state, detail = classify_installation_state()
    if state == "fresh":
        if not allow_first_install:
            print("[!] Erstinstallation erkannt; ein Release-Update wurde nicht gestartet.")
            print("    Starte zuerst die vollständige Installation über das Konsolenmenü.")
            return False
        print("[i] Erstinstallation erkannt; starte das vollständige E3DC-Control-Setup.")
        from .install_all import install_all_main
        return install_all_main(
            headless=headless,
            bind_first_install_role=True,
        )
    if state == "partial":
        if not allow_first_install:
            print(f"[!] Unvollständige Installation erkannt: {detail}.")
            print("    Setze die vollständige Installation über das Konsolenmenü fort.")
            return False
        print(f"[i] Unvollständige Installation erkannt: {detail}.")
        print("    Die vollständige Installation wird sicher fortgesetzt.")
        from .install_all import install_all_main
        return install_all_main(headless=headless)
    if state == "blocked":
        print(f"[!] Installation / Update wurde nicht gestartet: {detail}.")
        print("    Bitte zuerst die bestehende Installation über Systemreparatur prüfen.")
        return False
    if state == "ready":
        return update_e3dc(
            headless=headless,
            reinstall_current=reinstall_current,
        )
    print(f"[!] Unbekannter Installationszustand: {state!r}.")
    return False


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
    inventory: set[str] = set()
    activities: dict[str, str] = {}
    for unit in sorted(
        set((*_catalog_units_strict(), PIGUARD_UNIT, "e3dc.service"))
    ):
        activity = (
            _capture_piguard_transition_activity()
            if unit == PIGUARD_UNIT
            else _capture_transition_unit_activity(unit)
        )
        if activity == "absent":
            continue
        inventory.add(unit)
        activities[unit] = activity
    legacy_activity = activities.get("e3dc.service", "absent")
    return TransitionState(
        ha_role=role,
        config=dict(config),
        config_sha256=hashlib.sha256(raw).hexdigest(),
        config_path=config_path,
        preinstalled_units=frozenset(inventory),
        preactive_units=frozenset(
            unit for unit, activity in activities.items() if activity == "active"
        ),
        bootstrap_legacy_config=legacy,
        legacy_e3dc_activity=legacy_activity,
    )


def _bound_explicit_bootstrap_role_peer(state: TransitionState) -> str:
    """Bindet genau eine numerische Peer-IP an den unveränderlichen Rollenstand."""

    configured_peer = state.config.get("ha_peer_ip")
    if state.ha_role in {"master", "slave"}:
        if not isinstance(configured_peer, str):
            raise RuntimeError(
                "HA-Rollenanker verlangt genau eine numerische Peer-IP"
            )
        peer_text = configured_peer.strip()
        try:
            peer_address = ipaddress.ip_address(peer_text)
        except ValueError as exc:
            raise RuntimeError(
                "HA-Rollenanker verlangt genau eine gültige numerische Peer-IP"
            ) from exc
        if (
            peer_address.is_unspecified
            or peer_address.is_loopback
            or peer_address.is_multicast
        ):
            raise RuntimeError(
                "HA-Rollenanker verlangt eine eindeutige entfernte Peer-IP"
            )
        return str(peer_address)
    return ""


def _explicit_bootstrap_role_anchor_needed(
    state: TransitionState,
    *,
    target_install_path: str | None,
    sealed_target_updater: bool = False,
) -> bool:
    """Prüft rein, ob der explizit gebundene Anlagenknoten einen Anker braucht."""

    if target_install_path and sealed_target_updater:
        raise RuntimeError(
            "Erstinstallations-Bootstrap und versiegelter Ziel-Updater "
            "dürfen die Rollenanker-Autorität nicht gemeinsam besitzen"
        )
    if not target_install_path and not sealed_target_updater:
        return False
    from .ha_writer_admission import (
        INSTANCE_ROLE_ANCHOR_PATH,
        instance_role_anchor_matches,
    )

    peer_ip = _bound_explicit_bootstrap_role_peer(state)
    if instance_role_anchor_matches(state.ha_role, peer_ip=peer_ip) is True:
        return False
    if state.ha_role in {"master", "slave"}:
        if not target_install_path or sealed_target_updater:
            raise RuntimeError(
                "Ein fehlender HA-Rollenanker darf nur der explizite "
                "Download-Bootstrap erzeugen"
            )
        if state.bootstrap_legacy_config:
            raise RuntimeError(
                "HA-Rollenanker verlangt eine bestehende gebundene "
                "Betriebskonfiguration"
            )
    elif state.ha_role == "off" and str(
        state.config.get("ha_peer_ip") or ""
    ).strip():
        raise RuntimeError(
            "Ein fehlender Rollenanker darf automatisch nur für einen "
            "explizit gebundenen Einzelknoten ohne HA-Peer erzeugt werden"
        )
    elif state.ha_role != "off":
        raise RuntimeError(
            "Ein fehlender Rollenanker darf automatisch nur für einen "
            "explizit gebundenen Einzelknoten oder ein bestehendes "
            "Master-/Slave-System erzeugt werden"
        )
    if os.path.lexists(INSTANCE_ROLE_ANCHOR_PATH):
        raise RuntimeError(
            "Vorhandener Instanzrollen-Anker widerspricht der expliziten "
            "Rollenanker-Projektion"
        )
    return True


def _bind_explicit_bootstrap_role_anchor(
    state: TransitionState,
    *,
    target_install_path: str | None,
    sealed_target_updater: bool = False,
) -> bool:
    """Erzeugt den rein geprüften Anker erst im gesicherten Mutationsblock."""

    if not _explicit_bootstrap_role_anchor_needed(
        state,
        target_install_path=target_install_path,
        sealed_target_updater=sealed_target_updater,
    ):
        return False
    from .ha_writer_admission import (
        instance_role_anchor_matches,
        project_instance_role_anchor,
    )

    peer_ip = _bound_explicit_bootstrap_role_peer(state)
    if project_instance_role_anchor(state.ha_role, peer_ip=peer_ip) is not True:
        raise RuntimeError(
            "Explizit gebundener Instanzrollen-Anker konnte nicht erstellt werden"
        )
    if instance_role_anchor_matches(state.ha_role, peer_ip=peer_ip) is not True:
        raise RuntimeError("Instanzrollen-Anker ist nach dem Bootstrap nicht bestätigt")
    return True


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

    if result is not True:
        details = captured.getvalue().strip()
        print(f"  [!] {label}: Installer bestätigt keinen vollständigen Erfolg.")
        if details:
            print("      " + details.splitlines()[-1][:160])
        return False

    print(f"  [OK] {label}")
    return True


def _ensure_install_center_core_services(
    *,
    expected_recovery_dropins=None,
    allow_optional_not_found_compat=False,
) -> bool:
    """Installiert fehlende Kernsystem-Units aus dem Install-Center nach."""
    print('\n[->] Stelle Kernsystem-Dienste aus dem Install-Center sicher...')
    ok = True

    try:
        from .epex_manager import install_epex_service
        ok = _run_core_service_installer(
            "Live, Marktpreise, Forecast, Storage und WebSocket",
            lambda: install_epex_service(
                start_services=False,
                include_websocket=True,
                expected_recovery_dropins=expected_recovery_dropins,
                allow_optional_not_found_compat=allow_optional_not_found_compat,
            ),
        ) and ok
    except Exception as exc:
        print(f"  [!] Kern-Manager-Installer konnte nicht geladen werden: {exc}")
        update_logger.warning(f"Kern-Manager-Installer konnte nicht geladen werden: {exc}")
        ok = False

    try:
        from .install_notifier import install_notifier
        ok = _run_core_service_installer(
            "Zeitplanung und Langzeit-Archiv",
            lambda: install_notifier(
                start_service=False,
                migrate_legacy_config=False,
                expected_recovery_dropins={
                    unit: contract
                    for unit, contract in dict(
                        expected_recovery_dropins or {}
                    ).items()
                    if unit == "e3dc-notifier.service"
                },
            ),
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


WALLBOX_MODE5_USER_START_REQUEST_FILE = (
    "/var/www/html/data/wallbox_mode5_user_start_request.json"
)


def _mode5_user_start_request_nodes_safe(path: str, *, legacy_parent=False) -> bool:
    """Prüft Parent, Request und Lock ohne irgendeine Zielnormalisierung."""

    try:
        account = pwd.getpwnam("www-data")
        group = grp.getgrnam("www-data")
        manager = pwd.getpwnam(get_install_user())
        allowed_parent_uids = {int(account.pw_uid), int(manager.pw_uid)}
        parent = os.lstat(os.path.dirname(path))
        parent_mode = stat.S_IMODE(parent.st_mode)
        if (
            stat.S_ISLNK(parent.st_mode)
            or not stat.S_ISDIR(parent.st_mode)
            or bool(parent_mode & 0o002)
            or parent.st_uid not in allowed_parent_uids
            or parent.st_gid != int(group.gr_gid)
            or (
                parent_mode != 0o775
                if legacy_parent
                else parent_mode != 0o2775
            )
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
    path: str = WALLBOX_MODE5_USER_START_REQUEST_FILE,
) -> bool:
    """Prüft die persistente PHP-Autorität read-only und exakt."""

    return _mode5_user_start_request_nodes_safe(path, legacy_parent=False)


def _repair_mode5_user_start_legacy_parent(
    path: str = WALLBOX_MODE5_USER_START_REQUEST_FILE,
) -> bool:
    """Hebt ausschließlich den bekannten 0775-Parent descriptorgebunden an."""

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
        manager = pwd.getpwnam(get_install_user())
        allowed_parent_uids = {int(account.pw_uid), int(manager.pw_uid)}
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(directory, flags)
        current = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or current.st_uid not in allowed_parent_uids
            or current.st_gid != int(group.gr_gid)
            or stat.S_IMODE(current.st_mode) != 0o775
        ):
            return False
        os.fchmod(descriptor, 0o2775)
        changed = os.fstat(descriptor)
        named = os.lstat(directory)
        if (
            stat.S_IMODE(changed.st_mode) != 0o2775
            or (named.st_dev, named.st_ino) != (changed.st_dev, changed.st_ino)
            or named.st_uid not in allowed_parent_uids
            or stat.S_IMODE(named.st_mode) != 0o2775
        ):
            return False
    except (KeyError, OSError, TypeError, ValueError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return _mode5_user_start_request_surface_safe(path)


def _fix_webroot_permissions() -> bool:
    install_user = shlex.quote(get_install_user())
    secret_file_mode = config_secret_file_mode_text()
    secret_dir_mode = config_secret_dir_mode_text()
    repo_v4_config = shlex.quote(os.path.join(get_install_path(), "data", "e3dc_v4.json"))
    web_backup_dir = "/var/www/html/data/config_backups"
    repo_backup_dir = os.path.join(get_install_path(), "data", "config_backups")
    protected_mode5_request = WALLBOX_MODE5_USER_START_REQUEST_FILE
    protected_mode5_lock = protected_mode5_request + ".lock"
    if not _repair_mode5_user_start_legacy_parent(protected_mode5_request):
        raise RuntimeError(
            "Persistente Modus-5-Anforderungsfläche ist unsicher; "
            "keine Webroot-Reparatur ausgeführt"
        )
    run_command(f"sudo usermod -aG www-data {install_user} 2>/dev/null || true", timeout=10)
    protected_wallbox_jobs = "/var/www/html/data/.wallbox_plan_jobs"
    protected_matter_storage = "/var/www/html/data/matter-storage"
    run_command(
        "sudo find -P /var/www/html -xdev "
        f"\\( -path {protected_wallbox_jobs} "
        f"-o -path {protected_mode5_request} "
        f"-o -path {protected_mode5_lock} \\) -prune -o "
        f"\\( -type d -o -type f \\) -exec chown {install_user}:www-data {{}} +",
        timeout=60,
    )
    run_command(
        "sudo find -P /var/www/html -xdev "
        "\\( -path /var/www/html/data/e3dc_v4.json "
        "-o -path /var/www/html/data/config_backups "
        f"-o -path {protected_matter_storage} "
        f"-o -path {protected_wallbox_jobs} "
        f"-o -path {protected_mode5_request} "
        f"-o -path {protected_mode5_lock} \\) -prune -o "
        "-type d -exec chmod 775 {} +",
        timeout=60,
    )
    run_command(
        "sudo find -P /var/www/html -xdev "
        "\\( -path /var/www/html/data/e3dc_v4.json "
        "-o -path /var/www/html/data/config_backups "
        f"-o -path {protected_matter_storage} "
        f"-o -path {protected_wallbox_jobs} "
        f"-o -path {protected_mode5_request} "
        f"-o -path {protected_mode5_lock} \\) -prune -o "
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
    if not _mode5_user_start_request_surface_safe(protected_mode5_request):
        raise RuntimeError(
            "Persistente Modus-5-Anforderungsfläche wechselte während der "
            "Webroot-Reparatur"
        )
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


def _require_strict_forward_update_ancestry(
    repo_dir: str,
    install_user: str,
    current_commit: str,
    target_commit: str,
) -> None:
    """Beweist, dass der normale Zielstand ein echter Git-Nachfolger ist."""

    current = _validate_full_commit(current_commit)
    target = _validate_full_commit(target_commit)
    if _exact_commit_matches(current, target):
        raise RuntimeError(
            "Rollenanker-Autorität verlangt einen echten vorwärtsgerichteten Zielcommit"
        )
    result = _git_argv(
        repo_dir,
        install_user,
        "merge-base",
        "--is-ancestor",
        current,
        target,
        timeout=15,
    )
    if (
        result.get("success")
        and not result.get("timed_out")
        and int(result.get("returncode", -1)) == 0
        and str(result.get("stdout") or "") == ""
        and str(result.get("stderr") or "") == ""
    ):
        return
    if (
        not result.get("timed_out")
        and int(result.get("returncode", -1)) == 1
        and str(result.get("stdout") or "") == ""
        and str(result.get("stderr") or "") == ""
    ):
        raise RuntimeError(
            "Flagloser Zielcommit ist kein vorwärtsgerichteter Nachfolger des aktuellen HEAD"
        )
    raise RuntimeError(
        "Git-Ancestry des normalen Ziel-Updaters ist nicht beweisbar: "
        + _combined_process_diagnostics(result, maximum=800)
    )


def _resolve_git_commit(repo_dir: str, ref: str, install_user: str) -> str | None:
    result = _git_argv(repo_dir, install_user, "rev-parse", "--verify", str(ref) + "^{commit}", timeout=15)
    if not result['success']:
        return None
    try:
        return _validate_full_commit(result['stdout'].strip())
    except ValueError:
        return None


def _require_bound_origin(repo_dir: str, install_user: str) -> None:
    """Akzeptiert ausschließlich einen lokalen, include-freien origin-Rohwert."""

    result = _git_argv(
        repo_dir,
        install_user,
        "config",
        "--local",
        "--no-includes",
        "--get-all",
        "remote.origin.url",
        timeout=10,
    )
    values = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
    if (
        not result["success"]
        or len(values) != 1
        or values[0] not in SELFUPDATE_ALLOWED_ORIGINS
    ):
        raise RuntimeError("Git-Origin weicht vom fest freigegebenen Release-Repository ab")


def _official_remote_refs(*refs: str) -> dict[str, str]:
    """Bindet veröffentlichte Refs außerhalb des nutzerbeschreibbaren Repos."""

    patterns = tuple(str(ref or "").strip() for ref in refs)
    if not patterns or any(
        not re.fullmatch(r"refs/(?:heads|tags)/[A-Za-z0-9._/-]+(?:\^\{\})?", ref)
        or ".." in ref
        for ref in patterns
    ):
        raise RuntimeError("Remote-Ref ist nicht kanonisch")
    completed = run_isolated_remote_git(
        SELFUPDATE_REPO,
        "ls-remote",
        "--exit-code",
        SELFUPDATE_REPO,
        *patterns,
        timeout=60,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError("Offizieller Release-Ref konnte nicht gebunden werden: " + detail[-500:])
    result: dict[str, str] = {}
    for raw_line in bytes(completed.stdout or b"").splitlines():
        try:
            raw_sha, raw_ref = raw_line.split(b"\t", 1)
            sha = _validate_full_commit(raw_sha.decode("ascii"))
            ref = raw_ref.decode("ascii")
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("Offizieller Release-Ref besitzt kein eindeutiges Format") from exc
        if ref not in patterns or ref in result:
            raise RuntimeError("Offizieller Release-Ref ist mehrdeutig")
        result[ref] = sha
    if set(result) != set(patterns):
        raise RuntimeError("Offizieller Release-Ref ist unvollständig")
    return result


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


def _bind_bootstrap_git_prestate(
    repo_dir: str,
    *,
    explicit_bootstrap: bool,
) -> tuple[str | None, bool]:
    """Liest einen brauchbaren Alt-HEAD oder erlaubt dem Rettungsweg Neubau.

    Die Git-Metadaten sind Updatewerkzeug, nicht EMS-Laufzeit. Ein normaler
    Self-Update darf ein vorhandenes Repository weiterverwenden. Der ausdrücklich
    gestartete Download-Bootstrap ignoriert dagegen jeden Alt-Git-Zustand und
    baut die Zielmetadaten erst nach dem verifizierten Backup frisch auf.
    """

    git_path = os.path.join(os.path.abspath(repo_dir), ".git")
    if explicit_bootstrap:
        return None, os.path.lexists(git_path)

    usable_directory = os.path.isdir(git_path) and not os.path.islink(git_path)
    if not usable_directory:
        raise RuntimeError(
            "Installation ohne gebundenes Git darf nur über den "
            "expliziten Download-Bootstrap wechseln"
        )

    try:
        commit = get_current_commit(repo_dir)
    except Exception:
        commit = None
    if commit:
        return commit, False
    raise RuntimeError("Aktueller HEAD konnte nicht als volle Commit-SHA verifiziert werden")


def _repo_descriptor_has_unsafe_xattrs(descriptor: int) -> bool:
    try:
        names = set(os.listxattr(descriptor))
    except AttributeError:
        return True
    except OSError as exc:
        if exc.errno in {
            getattr(errno, "ENODATA", -1),
            getattr(errno, "ENOTSUP", -1),
            getattr(errno, "EOPNOTSUPP", -1),
        }:
            return False
        return True
    # Erweiterte Attribute gehören nicht zum Release-Sollzustand. Der Aufrufer
    # nutzt dieses Signal, um den bekannten Git-Blob nach dem Backup auf einen
    # neuen kanonischen Inode zu projizieren und damit Alt-ACLs/xattrs zu
    # entfernen; sie sind kein eigener Update-Blocker.
    return bool(names)


def _descriptor_plain_sha256(descriptor: int, expected_size: int) -> str:
    size = int(expected_size)
    if size < 0:
        raise RuntimeError("Recovery-Dateigröße ist ungültig")
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = size
    while remaining:
        block = os.read(descriptor, min(1024 * 1024, remaining))
        if not block:
            raise RuntimeError("Recovery-Datei endet vor ihrer gebundenen Größe")
        digest.update(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise RuntimeError("Recovery-Datei überschreitet ihre gebundene Größe")
    return digest.hexdigest()


def _capture_repo_recovery_contract(
    repo_dir: str,
    install_user: str,
    expected_commit: str,
) -> RepoRecoveryContract:
    """Friert Worktree-Bytes vor Backup und erster Mutation descriptorgebunden ein."""

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("Repo-Recovery-Vertrag darf ausschließlich Root erzeugen")
    root = os.path.abspath(repo_dir)
    commit = _validate_full_commit(expected_commit)
    if _bound_release_head_commit(root, install_user) != commit:
        raise RuntimeError("Repo-Recovery-Vertrag sieht nicht den Ausgangs-Commit")
    # Der Installationsbenutzer muss existieren, ist aber keine Autorität für
    # den Altzustand. Community-Installationen enthalten nach manuellen
    # Kopien, chmod/chown oder älteren Root-Installern legitimerweise andere
    # numerische Besitzer. Der Root-Backupvertrag friert diese Metadaten ein;
    # erst die Zielprojektion setzt anschließend den kanonischen Besitzer.
    pwd.getpwnam(str(install_user))
    tracked_entries = _tracked_release_file_contracts(
        root,
        install_user,
        target_commit=commit,
    )
    frozen = []
    dirty_paths = []
    for relative_path, git_mode, git_oid in tracked_entries:
        target = os.path.join(root, relative_path)
        descriptor, before = _open_regular_file_nofollow(target)
        try:
            mode = stat.S_IMODE(before.st_mode)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
            ):
                raise RuntimeError(
                    f"Repo-Recovery-Preimage besitzt unsichere Metadaten: {relative_path}"
                )
            actual_oid = _git_blob_oid_from_descriptor(
                descriptor,
                before.st_size,
                git_oid,
            )
            digest = _descriptor_plain_sha256(descriptor, before.st_size)
            after = os.fstat(descriptor)
            named_after = os.lstat(target)
            signature = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_nlink,
                before.st_uid,
                before.st_gid,
                stat.S_IMODE(before.st_mode),
            )
            if signature != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_nlink,
                after.st_uid,
                after.st_gid,
                stat.S_IMODE(after.st_mode),
            ) or signature != (
                named_after.st_dev,
                named_after.st_ino,
                named_after.st_size,
                named_after.st_mtime_ns,
                named_after.st_ctime_ns,
                named_after.st_nlink,
                named_after.st_uid,
                named_after.st_gid,
                stat.S_IMODE(named_after.st_mode),
            ):
                raise RuntimeError(
                    f"Repo-Recovery-Preimage driftete beim Einfrieren: {relative_path}"
                )
            frozen.append(
                (
                    relative_path,
                    git_mode,
                    git_oid,
                    digest,
                    int(before.st_size),
                    mode,
                    int(before.st_uid),
                    int(before.st_gid),
                )
            )
            if actual_oid != git_oid:
                dirty_paths.append(relative_path)
        finally:
            os.close(descriptor)
    if _bound_release_head_commit(root, install_user) != commit:
        raise RuntimeError("Repository-HEAD driftete beim Recovery-Preflight")
    return RepoRecoveryContract(
        install_root=root,
        install_user=str(install_user),
        expected_commit=commit,
        tracked_files=tuple(frozen),
        dirty_paths=tuple(sorted(dirty_paths)),
    )


def _verify_repo_recovery_prestate(
    repo_dir: str,
    install_user: str,
    contract: RepoRecoveryContract,
) -> None:
    """Bindet den eingefrorenen Live-Worktree erneut direkt vor der Mutation."""

    if not isinstance(contract, RepoRecoveryContract):
        raise RuntimeError("Repo-Recovery-Prestate-Vertrag fehlt")
    root = os.path.abspath(repo_dir)
    user = str(install_user)
    commit = _validate_full_commit(contract.expected_commit)
    if (
        contract.install_root != root
        or contract.install_user != user
        or _bound_release_head_commit(root, user) != commit
    ):
        raise RuntimeError("Repo-Recovery-Prestate weicht vom eingefrorenen HEAD ab")
    expected_git_entries = tuple(
        (relative_path, git_mode, git_oid)
        for (
            relative_path,
            git_mode,
            git_oid,
            _digest,
            _size,
            _mode,
            _uid,
            _gid,
        ) in contract.tracked_files
    )
    current_git_entries = tuple(
        _tracked_release_file_contracts(
            root,
            user,
            target_commit=commit,
        )
    )
    if current_git_entries != expected_git_entries:
        raise RuntimeError("Repo-Recovery-Prestate besitzt einen anderen Git-Dateivertrag")

    dirty_paths: list[str] = []
    for (
        relative_path,
        git_mode,
        git_oid,
        expected_digest,
        expected_size,
        expected_mode,
        expected_uid,
        expected_gid,
    ) in contract.tracked_files:
        target = os.path.join(root, relative_path)
        descriptor, before = _open_regular_file_nofollow(target)
        try:
            before_identity = (
                int(before.st_dev),
                int(before.st_ino),
                int(before.st_size),
                int(before.st_mtime_ns),
                int(before.st_ctime_ns),
                int(before.st_nlink),
                int(before.st_uid),
                int(before.st_gid),
                int(stat.S_IMODE(before.st_mode)),
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != expected_size
                or stat.S_IMODE(before.st_mode) != expected_mode
                or before.st_uid != expected_uid
                or before.st_gid != expected_gid
                or _descriptor_plain_sha256(descriptor, expected_size)
                != expected_digest
            ):
                raise RuntimeError(
                    f"Repo-Recovery-Prestate driftete: {relative_path}"
                )
            actual_oid = _git_blob_oid_from_descriptor(
                descriptor,
                expected_size,
                git_oid,
            )
            after = os.fstat(descriptor)
            named_after = os.lstat(target)
            after_identity = (
                int(after.st_dev),
                int(after.st_ino),
                int(after.st_size),
                int(after.st_mtime_ns),
                int(after.st_ctime_ns),
                int(after.st_nlink),
                int(after.st_uid),
                int(after.st_gid),
                int(stat.S_IMODE(after.st_mode)),
            )
            named_identity = (
                int(named_after.st_dev),
                int(named_after.st_ino),
                int(named_after.st_size),
                int(named_after.st_mtime_ns),
                int(named_after.st_ctime_ns),
                int(named_after.st_nlink),
                int(named_after.st_uid),
                int(named_after.st_gid),
                int(stat.S_IMODE(named_after.st_mode)),
            )
            if before_identity != after_identity or before_identity != named_identity:
                raise RuntimeError(
                    f"Repo-Recovery-Prestate driftete beim Readback: {relative_path}"
                )
            if actual_oid != git_oid:
                dirty_paths.append(relative_path)
        finally:
            os.close(descriptor)
    if tuple(sorted(dirty_paths)) != contract.dirty_paths:
        raise RuntimeError("Repo-Recovery-Prestate besitzt eine andere Dirty-Pfadmenge")
    if _bound_release_head_commit(root, user) != commit:
        raise RuntimeError("Repository-HEAD driftete beim finalen Prestate-Readback")


def _verify_recovered_repo_contract(
    repo_dir: str,
    install_user: str,
    contract: RepoRecoveryContract,
) -> None:
    """Beweist nach der Härtung Bytes, Zielrechte, HEAD und Dirty-Pfadmenge."""

    if not isinstance(contract, RepoRecoveryContract):
        raise RuntimeError("Repo-Recovery-Endvertrag fehlt")
    root = os.path.abspath(repo_dir)
    account = pwd.getpwnam(str(install_user))
    if (
        contract.install_root != root
        or contract.install_user != str(install_user)
        or _bound_release_head_commit(root, install_user) != contract.expected_commit
    ):
        raise RuntimeError("Repo-Recovery-Endvertrag weicht vom Rückfallziel ab")
    dirty_paths = []
    for (
        relative_path,
        git_mode,
        git_oid,
        expected_sha256,
        expected_size,
        _pre_mode,
        _pre_uid,
        _pre_gid,
    ) in contract.tracked_files:
        target = os.path.join(root, relative_path)
        descriptor, before = _open_regular_file_nofollow(target)
        try:
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != account.pw_uid
                or before.st_gid != account.pw_gid
                or stat.S_IMODE(before.st_mode) != git_mode
                or before.st_size != expected_size
                or _repo_descriptor_has_unsafe_xattrs(descriptor)
                or _descriptor_plain_sha256(descriptor, expected_size) != expected_sha256
            ):
                raise RuntimeError(
                    f"Repo-Recovery-Endvertrag ist verletzt: {relative_path}"
                )
            actual_oid = _git_blob_oid_from_descriptor(
                descriptor,
                expected_size,
                git_oid,
            )
            after = os.fstat(descriptor)
            named_after = os.lstat(target)
            signature = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_nlink,
                before.st_uid,
                before.st_gid,
                stat.S_IMODE(before.st_mode),
            )
            if signature != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_nlink,
                after.st_uid,
                after.st_gid,
                stat.S_IMODE(after.st_mode),
            ) or signature != (
                named_after.st_dev,
                named_after.st_ino,
                named_after.st_size,
                named_after.st_mtime_ns,
                named_after.st_ctime_ns,
                named_after.st_nlink,
                named_after.st_uid,
                named_after.st_gid,
                stat.S_IMODE(named_after.st_mode),
            ):
                raise RuntimeError(
                    f"Repo-Recovery-Datei driftete im Endgate: {relative_path}"
                )
            if actual_oid != git_oid:
                dirty_paths.append(relative_path)
        finally:
            os.close(descriptor)
    if tuple(sorted(dirty_paths)) != contract.dirty_paths:
        raise RuntimeError("Repo-Recovery-Dirty-Pfadmenge weicht vom Vorzustand ab")


def _recovery_repo_contracts_from_manifest(
    manifest: dict,
    repo_dir: str,
    tracked_entries: list[tuple[str, int, str]],
) -> tuple[
    dict[str, tuple[str, int, int, int, int]],
    dict[str, tuple[int, int, int]],
]:
    """Bindet getrackte Recovery-Preimages ausschließlich an das Backupmanifest.

    Der Git-Commit bleibt Autorität für Umfang und Zielmodus des Produktbaums.
    Nur die Bytes und Ausgangsmetadaten dürfen bei einem Recovery absichtlich
    vom Commit abweichen; ihre Autorität stammt aus dem erneut vollständig
    verifizierten Systembackup, niemals aus dem aktuellen Worktree.
    """

    root = os.path.abspath(repo_dir)
    manifest_root = str(manifest.get("install_root") or "")
    if (
        not os.path.isabs(manifest_root)
        or os.path.abspath(manifest_root) != manifest_root
        or manifest_root != root
    ):
        raise RuntimeError(
            "Recovery-Backup ist nicht exakt an den Installationsbaum gebunden"
        )

    tracked_paths = {relative_path for relative_path, _mode, _oid in tracked_entries}
    file_contracts: dict[str, tuple[str, int, int, int, int]] = {}
    for raw_entry in manifest.get("files") or ():
        if not isinstance(raw_entry, dict) or not raw_entry.get("restore_path"):
            continue
        raw_path = str(raw_entry["restore_path"])
        if not os.path.isabs(raw_path) or os.path.abspath(raw_path) != raw_path:
            raise RuntimeError("Recovery-Manifest enthält einen nichtkanonischen Zielpfad")
        try:
            within_root = os.path.commonpath((root, raw_path)) == root
        except ValueError as exc:
            raise RuntimeError("Recovery-Manifest enthält einen fremden Zielpfad") from exc
        if not within_root or raw_path == root:
            continue
        relative_path = os.path.relpath(raw_path, root).replace(os.sep, "/")
        if relative_path not in tracked_paths:
            continue
        if raw_entry.get("category") != "install-tree" or relative_path in file_contracts:
            raise RuntimeError(
                f"Recovery-Dateivertrag ist nicht eindeutig: {relative_path}"
            )
        digest = str(raw_entry.get("sha256") or "").strip().lower()
        try:
            size = int(raw_entry.get("size", -1))
            mode = int(raw_entry.get("mode", -1))
            uid = int(raw_entry.get("uid", -1))
            gid = int(raw_entry.get("gid", -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Recovery-Dateivertrag besitzt ungültige Metadaten: {relative_path}"
            ) from exc
        if (
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            or size < 0
            or mode < 0
            or mode > 0o7777
            or uid < 0
            or gid < 0
        ):
            raise RuntimeError(
                f"Recovery-Dateivertrag besitzt ungültige Werte: {relative_path}"
            )
        file_contracts[relative_path] = (digest, size, mode, uid, gid)
    if set(file_contracts) != tracked_paths:
        missing = sorted(tracked_paths - set(file_contracts))
        raise RuntimeError(
            "Recovery-Backup deckt den getrackten Produktbaum nicht vollständig ab: "
            + ", ".join(missing[:5])
        )

    source_records = [
        record
        for record in manifest.get("sources") or ()
        if isinstance(record, dict)
        and record.get("category") == "install-tree"
        and record.get("source") == root
    ]
    if len(source_records) != 1:
        raise RuntimeError("Recovery-Verzeichnisvertrag ist nicht eindeutig")
    source = source_records[0]
    if source.get("source_type") != "directory" or source.get("present") is not True:
        raise RuntimeError("Recovery-Installationsbaum war im Backup nicht vorhanden")

    directory_contracts: dict[str, tuple[int, int, int]] = {}

    def add_directory(relative_path: str, raw: dict) -> None:
        if relative_path in directory_contracts:
            raise RuntimeError(
                f"Recovery-Verzeichnisvertrag ist doppelt: {relative_path or root}"
            )
        try:
            mode = int(raw.get("mode", -1))
            uid = int(raw.get("uid", -1))
            gid = int(raw.get("gid", -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Recovery-Verzeichnisvertrag ist ungültig: {relative_path or root}"
            ) from exc
        if mode < 0 or mode > 0o7777 or uid < 0 or gid < 0:
            raise RuntimeError(
                f"Recovery-Verzeichnisvertrag ist ungültig: {relative_path or root}"
            )
        directory_contracts[relative_path] = (mode, uid, gid)

    add_directory("", source)
    for raw_directory in source.get("directories") or ():
        if not isinstance(raw_directory, dict):
            raise RuntimeError("Recovery-Verzeichnisvertrag enthält keinen Objekteintrag")
        relative_path = str(raw_directory.get("path") or "")
        candidate = Path(relative_path)
        if (
            not relative_path
            or candidate.is_absolute()
            or ".." in candidate.parts
            or candidate.as_posix() != relative_path
        ):
            raise RuntimeError("Recovery-Verzeichnisvertrag enthält einen ungültigen Pfad")
        add_directory(relative_path, raw_directory)

    tracked_directories = {""}
    for relative_path in tracked_paths:
        parent = Path(relative_path).parent
        while str(parent) not in {"", "."}:
            tracked_directories.add(parent.as_posix())
            parent = parent.parent
    if not tracked_directories.issubset(directory_contracts):
        missing = sorted(tracked_directories - set(directory_contracts))
        raise RuntimeError(
            "Recovery-Backup deckt getrackte Produktverzeichnisse nicht ab: "
            + ", ".join(missing[:5])
        )
    return file_contracts, {
        relative_path: directory_contracts[relative_path]
        for relative_path in tracked_directories
    }


def _read_stable_verified_backup_manifest(
    backup_dir: str,
) -> tuple[dict, str]:
    """Liest Manifest und Digest mit einem Gleichheitsgate um die Vollprüfung."""

    root = os.path.abspath(str(backup_dir or ""))
    if not root or not os.path.isabs(root) or root != str(backup_dir):
        raise RuntimeError("Recovery-Backuppfad ist nicht kanonisch")
    manifest_path = os.path.join(root, MANIFEST_NAME)
    digest_before, size_before = _regular_file_sha256(manifest_path)
    manifest = verify_backup(root, expected_kind=SYSTEM_BACKUP_KIND)
    digest_after, size_after = _regular_file_sha256(manifest_path)
    if digest_before != digest_after or size_before != size_after:
        raise RuntimeError("Recovery-Manifest driftete während der Root-Bindung")
    return manifest, digest_after


def _manifest_file_receipt(
    manifest: dict,
) -> tuple[tuple[str, str, int, int, int, int, str, str], ...]:
    result = []
    for entry in manifest.get("files") or ():
        if not isinstance(entry, dict):
            raise RuntimeError("Backup-Receipt enthält einen ungültigen Dateieintrag")
        result.append(
            (
                str(entry.get("path") or ""),
                str(entry.get("sha256") or ""),
                int(entry.get("size", -1)),
                int(entry.get("mode", -1)),
                int(entry.get("uid", -1)),
                int(entry.get("gid", -1)),
                str(entry.get("category") or ""),
                str(entry.get("restore_path") or ""),
            )
        )
    return tuple(sorted(result))


def _manifest_semantic_sha256(manifest: dict) -> str:
    try:
        encoded = json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeError("Backupmanifest besitzt keinen kanonischen Semantikvertrag") from exc
    return hashlib.sha256(encoded).hexdigest()


def _open_root_receipt_directory_chain(
    path: str,
) -> tuple[int, tuple[tuple[str, int, int, int, int, int], ...]]:
    """Öffnet und bindet jede Komponente eines Root-Receipt-Pfads."""

    candidate = str(path or "")
    if not os.path.isabs(candidate) or os.path.abspath(candidate) != candidate:
        raise RuntimeError("Recovery-Backup-Pfad ist nicht kanonisch")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise RuntimeError("Recovery-Receipt benötigt O_NOFOLLOW und O_DIRECTORY")
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open("/", flags)
    receipts: list[tuple[str, int, int, int, int, int]] = []

    def bind_component(
        opened_descriptor: int,
        absolute_path: str,
        named_metadata=None,
    ) -> tuple[str, int, int, int, int, int]:
        metadata = os.fstat(opened_descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or _repo_descriptor_has_unsafe_xattrs(opened_descriptor)
        ):
            raise RuntimeError(
                f"Recovery-Backup-Elternpfad ist nicht root-kontrolliert: {absolute_path}"
            )
        if named_metadata is not None and (
            named_metadata.st_dev,
            named_metadata.st_ino,
            named_metadata.st_uid,
            named_metadata.st_gid,
            stat.S_IMODE(named_metadata.st_mode),
        ) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_uid,
            metadata.st_gid,
            mode,
        ):
            raise RuntimeError(
                f"Recovery-Backup-Elternpfad driftete beim Öffnen: {absolute_path}"
            )
        return (
            absolute_path,
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_uid),
            int(metadata.st_gid),
            int(mode),
        )

    try:
        receipts.append(bind_component(descriptor, "/"))
        current_path = ""
        for component in Path(candidate).parts[1:]:
            before = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            current_path = os.path.join(current_path, component)
            absolute_path = "/" + current_path
            try:
                receipt = bind_component(
                    next_descriptor,
                    absolute_path,
                    before,
                )
                named_after = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    named_after.st_dev,
                    named_after.st_ino,
                    named_after.st_uid,
                    named_after.st_gid,
                    stat.S_IMODE(named_after.st_mode),
                ) != receipt[1:]:
                    raise RuntimeError(
                        f"Recovery-Backup-Elternpfad driftete beim Readback: {absolute_path}"
                    )
            except Exception:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
            receipts.append(receipt)
        return descriptor, tuple(receipts)
    except Exception:
        os.close(descriptor)
        raise


def _privileged_restore_path_allowed(path: str, category: str) -> bool:
    candidate = str(path or "")
    if not os.path.isabs(candidate) or os.path.abspath(candidate) != candidate:
        return False
    if category == "systemd":
        return os.path.dirname(candidate) in {
            "/etc/systemd/system",
            "/lib/systemd/system",
            "/usr/lib/systemd/system",
        }
    if category == "watchdog":
        return candidate in {
            "/usr/local/bin/boot_notify.sh",
            "/usr/local/bin/pi_guard.sh",
        }
    if category == "system-config":
        try:
            return os.path.commonpath(("/etc/e3dc-control", candidate)) == "/etc/e3dc-control"
        except ValueError:
            return False
    return False


def _read_privileged_restore_source(
    path: str,
    category: str,
    install_user: str,
) -> tuple[str, str, str, int, int, int, int]:
    """Bindet eine privilegiert restaurierte Quelle vor jeder Mutation."""

    if not _privileged_restore_path_allowed(path, category):
        raise RuntimeError(f"Privilegierter Restorepfad ist nicht freigegeben: {path}")
    parent_descriptor, _parent_chain = _open_root_receipt_directory_chain(
        os.path.dirname(path),
    )
    descriptor = None
    try:
        name = os.path.basename(path)
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if (
            not nofollow
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > 8 * 1024 * 1024
            or before.st_mode
            & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        ):
            raise RuntimeError(f"Privilegierte Restorequelle ist unsicher: {path}")
        descriptor = os.open(
            name,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_uid,
            before.st_gid,
            stat.S_IMODE(before.st_mode),
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if identity != (
            opened.st_dev,
            opened.st_ino,
            opened.st_uid,
            opened.st_gid,
            stat.S_IMODE(opened.st_mode),
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) or _repo_descriptor_has_unsafe_xattrs(descriptor):
            raise RuntimeError(f"Privilegierte Restorequelle driftete beim Öffnen: {path}")
        digest = _descriptor_plain_sha256(descriptor, opened.st_size)
        from .ha_writer_admission import INSTANCE_ROLE_ANCHOR_PATH

        role_anchor_exception = path == INSTANCE_ROLE_ANCHOR_PATH
        storage_exception = path == "/etc/systemd/system/e3dc-storage-manager.service"
        if role_anchor_exception:
            try:
                www_data_gid = int(grp.getgrnam("www-data").gr_gid)
            except (KeyError, OSError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Instanzrollen-Ankergruppe ist nicht lokal gebunden"
                ) from exc
            if (
                opened.st_uid != 0
                or opened.st_gid != www_data_gid
                or stat.S_IMODE(opened.st_mode) != 0o640
            ):
                raise RuntimeError(
                    "Instanzrollen-Anker besitzt nicht root:www-data 0640"
                )
        elif opened.st_uid == 0 and opened.st_gid == 0:
            pass
        elif storage_exception:
            account = pwd.getpwnam(str(install_user))
            if (
                opened.st_uid != account.pw_uid
                or opened.st_gid != account.pw_gid
                or stat.S_IMODE(opened.st_mode) != 0o644
                or opened.st_size > 256 * 1024
            ):
                raise RuntimeError("Storage-Unit-Altbesitz ist nicht lokal gebunden")
            os.lseek(descriptor, 0, os.SEEK_SET)
            payload_chunks = []
            remaining = 256 * 1024 + 1
            while remaining:
                block = os.read(descriptor, min(64 * 1024, remaining))
                if not block:
                    break
                payload_chunks.append(block)
                remaining -= len(block)
            payload = b"".join(payload_chunks)
            if len(payload) != opened.st_size:
                raise RuntimeError("Storage-Unit-Altbesitz driftete beim Lesen")
            if payload not in set(_approved_storage_manager_unit_payloads()):
                raise RuntimeError(
                    "Storage-Unit-Altbesitz besitzt keine freigegebenen Bytes"
                )
        else:
            raise RuntimeError(
                f"Privilegierte Restorequelle ist nicht root-eigen: {path}"
            )
        after = os.fstat(descriptor)
        named_after = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_uid,
            after.st_gid,
            stat.S_IMODE(after.st_mode),
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        named_identity = (
            named_after.st_dev,
            named_after.st_ino,
            named_after.st_uid,
            named_after.st_gid,
            stat.S_IMODE(named_after.st_mode),
            named_after.st_nlink,
            named_after.st_size,
            named_after.st_mtime_ns,
            named_after.st_ctime_ns,
        )
        if identity != after_identity or identity != named_identity:
            raise RuntimeError(f"Privilegierte Restorequelle driftete beim Readback: {path}")
        return (
            path,
            category,
            digest,
            int(opened.st_size),
            int(stat.S_IMODE(opened.st_mode)),
            int(opened.st_uid),
            int(opened.st_gid),
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def _privileged_restore_contract_from_manifest(
    manifest: dict,
    install_user: str,
    *,
    verify_sources: bool,
) -> tuple[tuple[str, str, str, int, int, int, int], ...]:
    privileged_categories = {"systemd", "watchdog", "system-config"}
    result = []
    seen = set()
    for entry in manifest.get("files") or ():
        if not isinstance(entry, dict):
            raise RuntimeError("Backupmanifest enthält einen ungültigen Dateieintrag")
        category = str(entry.get("category") or "")
        if category not in privileged_categories:
            continue
        path = str(entry.get("restore_path") or "")
        if path in seen or not _privileged_restore_path_allowed(path, category):
            raise RuntimeError("Privilegierter Restorevertrag ist nicht eindeutig")
        seen.add(path)
        try:
            manifest_record = (
                path,
                category,
                str(entry.get("sha256") or "").lower(),
                int(entry.get("size", -1)),
                int(entry.get("mode", -1)),
                int(entry.get("uid", -1)),
                int(entry.get("gid", -1)),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Privilegierter Restorevertrag besitzt ungültige Metadaten") from exc
        if (
            not re.fullmatch(r"[0-9a-f]{64}", manifest_record[2])
            or manifest_record[3] < 0
        ):
            raise RuntimeError("Privilegierter Restorevertrag besitzt ungültige Werte")
        if verify_sources:
            source_record = _read_privileged_restore_source(
                path,
                category,
                install_user,
            )
            if source_record != manifest_record:
                raise RuntimeError(
                    f"Backup bindet die privilegierte Quelle nicht exakt: {path}"
                )
        result.append(manifest_record)
    return tuple(sorted(result))


def _privileged_backup_payload_receipts(
    backup_dir: str,
    manifest: dict,
) -> tuple[PrivilegedBackupFileReceipt, ...]:
    """Bindet privilegierte Backup-Payloads samt kompletter Inode-Elternkette."""

    backup_root = os.path.abspath(str(backup_dir or ""))
    if backup_root != str(backup_dir):
        raise RuntimeError("Privilegierter Backup-Payloadpfad ist nicht kanonisch")
    privileged_categories = {"systemd", "watchdog", "system-config"}
    receipts = []
    seen_restore_paths = set()
    for entry in manifest.get("files") or ():
        if not isinstance(entry, dict):
            raise RuntimeError("Backupmanifest enthält einen ungültigen Dateieintrag")
        category = str(entry.get("category") or "")
        if category not in privileged_categories:
            continue
        restore_path = str(entry.get("restore_path") or "")
        relative_text = str(entry.get("path") or "")
        relative = Path(relative_text)
        if (
            restore_path in seen_restore_paths
            or not _privileged_restore_path_allowed(restore_path, category)
            or relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or relative.as_posix() != relative_text
        ):
            raise RuntimeError("Privilegierter Backup-Payloadvertrag ist nicht eindeutig")
        seen_restore_paths.add(restore_path)
        parent_path = os.path.join(
            backup_root,
            *relative.parts[:-1],
        )
        parent_descriptor, parent_chain = _open_root_receipt_directory_chain(
            parent_path,
        )
        descriptor = None
        try:
            name = relative.parts[-1]
            before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if (
                not nofollow
                or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != 0
                or before.st_gid != 0
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size < 0
                or before.st_size > 8 * 1024 * 1024
            ):
                raise RuntimeError(
                    f"Privilegierter Backup-Payload ist unsicher: {relative_text}"
                )
            descriptor = os.open(
                name,
                os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
            opened = os.fstat(descriptor)
            identity = (
                int(before.st_dev),
                int(before.st_ino),
                int(before.st_size),
                int(stat.S_IMODE(before.st_mode)),
                int(before.st_uid),
                int(before.st_gid),
                int(before.st_nlink),
                int(before.st_mtime_ns),
                int(before.st_ctime_ns),
            )
            opened_identity = (
                int(opened.st_dev),
                int(opened.st_ino),
                int(opened.st_size),
                int(stat.S_IMODE(opened.st_mode)),
                int(opened.st_uid),
                int(opened.st_gid),
                int(opened.st_nlink),
                int(opened.st_mtime_ns),
                int(opened.st_ctime_ns),
            )
            if identity != opened_identity or _repo_descriptor_has_unsafe_xattrs(
                descriptor
            ):
                raise RuntimeError(
                    f"Privilegierter Backup-Payload driftete beim Öffnen: {relative_text}"
                )
            digest = _descriptor_plain_sha256(descriptor, opened.st_size)
            after = os.fstat(descriptor)
            named_after = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            after_identity = (
                int(after.st_dev),
                int(after.st_ino),
                int(after.st_size),
                int(stat.S_IMODE(after.st_mode)),
                int(after.st_uid),
                int(after.st_gid),
                int(after.st_nlink),
                int(after.st_mtime_ns),
                int(after.st_ctime_ns),
            )
            named_identity = (
                int(named_after.st_dev),
                int(named_after.st_ino),
                int(named_after.st_size),
                int(stat.S_IMODE(named_after.st_mode)),
                int(named_after.st_uid),
                int(named_after.st_gid),
                int(named_after.st_nlink),
                int(named_after.st_mtime_ns),
                int(named_after.st_ctime_ns),
            )
            if (
                identity != after_identity
                or identity != named_identity
                or digest != str(entry.get("sha256") or "").lower()
                or opened.st_size != int(entry.get("size", -1))
            ):
                raise RuntimeError(
                    f"Privilegierter Backup-Payload weicht vom Manifest ab: {relative_text}"
                )
            receipts.append(
                PrivilegedBackupFileReceipt(
                    restore_path=restore_path,
                    category=category,
                    backup_relative_path=relative_text,
                    parent_path_chain=parent_chain,
                    dev=identity[0],
                    ino=identity[1],
                    sha256=digest,
                    size=identity[2],
                    mode=identity[3],
                    uid=identity[4],
                    gid=identity[5],
                    nlink=identity[6],
                    mtime_ns=identity[7],
                    ctime_ns=identity[8],
                )
            )
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_descriptor)
    return tuple(sorted(receipts, key=lambda item: item.restore_path))


def _verify_restored_privileged_files(
    receipt: RecoveryBackupReceipt,
    install_user: str,
    *,
    allow_storage_owner_promotion: bool = False,
) -> None:
    if not isinstance(receipt, RecoveryBackupReceipt):
        raise RuntimeError("Privilegierter Restore besitzt keinen Root-Receipt")
    current = tuple(
        sorted(
            _read_privileged_restore_source(path, category, install_user)
            for path, category, _digest, _size, _mode, _uid, _gid
            in receipt.privileged_files
        )
    )
    expected = list(receipt.privileged_files)
    if allow_storage_owner_promotion:
        storage_path = "/etc/systemd/system/e3dc-storage-manager.service"
        expected = [
            (
                path,
                category,
                digest,
                size,
                mode,
                0 if path == storage_path else uid,
                0 if path == storage_path else gid,
            )
            for path, category, digest, size, mode, uid, gid in expected
        ]
    if current != tuple(sorted(expected)):
        raise RuntimeError("Privilegierte Restorequellen weichen nach Recovery ab")


def _capture_recovery_backup_receipt(
    backup_dir: str,
    verified_manifest: dict,
    repo_contract: RepoRecoveryContract,
    transaction_id: str,
) -> RecoveryBackupReceipt:
    """Friert den noch root-kontrollierten Backupbaum vor dessen Chown ein."""

    if not isinstance(repo_contract, RepoRecoveryContract):
        raise RuntimeError("Repo-Recovery-Vertrag fehlt vor dem Backup-Receipt")
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("Recovery-Receipt darf ausschließlich Root erzeugen")
    if not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(str(transaction_id or "")):
        raise RuntimeError("Recovery-Receipt besitzt keine gebundene Transaktions-ID")
    backup_root = os.path.abspath(backup_dir)
    backup_descriptor, backup_path_chain = _open_root_receipt_directory_chain(
        backup_root,
    )
    try:
        backup_metadata = os.fstat(backup_descriptor)
    finally:
        os.close(backup_descriptor)
    if len(backup_path_chain) < 2:
        raise RuntimeError("Backupbaum besitzt keine gebundene Elternkette")
    parent_receipt = backup_path_chain[-2]
    backup_receipt = backup_path_chain[-1]
    if (
        backup_root != backup_dir
        or not stat.S_ISDIR(backup_metadata.st_mode)
        or backup_metadata.st_uid != 0
        or backup_metadata.st_gid != 0
        or stat.S_IMODE(backup_metadata.st_mode) != 0o700
        or (backup_receipt[1], backup_receipt[2])
        != (backup_metadata.st_dev, backup_metadata.st_ino)
    ):
        raise RuntimeError("Backupbaum ist vor dem Receipt nicht root:root 0700")
    root = repo_contract.install_root
    install_user = repo_contract.install_user
    commit = repo_contract.expected_commit
    if _bound_release_head_commit(root, install_user) != commit:
        raise RuntimeError("Recovery-Receipt sieht nicht den gebundenen Ausgangs-Commit")
    manifest, manifest_sha256 = _read_stable_verified_backup_manifest(backup_root)
    if manifest != verified_manifest:
        raise RuntimeError("Backupmanifest driftete vor dem Root-Receipt")
    stable_descriptor, stable_chain = _open_root_receipt_directory_chain(backup_root)
    os.close(stable_descriptor)
    if stable_chain != backup_path_chain:
        raise RuntimeError("Backup-Elternkette driftete vor dem Root-Receipt")
    privileged_files = _privileged_restore_contract_from_manifest(
        manifest,
        install_user,
        verify_sources=True,
    )
    privileged_backup_files = _privileged_backup_payload_receipts(
        backup_root,
        manifest,
    )
    tracked_entries = [
        (relative_path, git_mode, git_oid)
        for relative_path, git_mode, git_oid, _sha, _size, _mode, _uid, _gid
        in repo_contract.tracked_files
    ]
    files, directories = _recovery_repo_contracts_from_manifest(
        manifest,
        root,
        tracked_entries,
    )
    preimages = {
        relative_path: (digest, size, mode, uid, gid)
        for relative_path, _git_mode, _git_oid, digest, size, mode, uid, gid
        in repo_contract.tracked_files
    }
    if files != preimages:
        raise RuntimeError(
            "Backup erfasst nicht exakt die vorab eingefrorenen Repo-Preimages"
        )
    return RecoveryBackupReceipt(
        backup_dir=backup_root,
        backup_dev=int(backup_metadata.st_dev),
        backup_ino=int(backup_metadata.st_ino),
        parent_dev=int(parent_receipt[1]),
        parent_ino=int(parent_receipt[2]),
        backup_path_chain=backup_path_chain,
        transaction_id=transaction_id,
        backup_id=str(manifest.get("backup_id") or ""),
        manifest_sha256=manifest_sha256,
        manifest_semantic_sha256=_manifest_semantic_sha256(manifest),
        install_root=root,
        expected_commit=commit,
        tracked_files=tuple(
            (relative_path, digest, size, mode, uid, gid)
            for relative_path, (digest, size, mode, uid, gid) in sorted(files.items())
        ),
        tracked_directories=tuple(
            (relative_path, mode, uid, gid)
            for relative_path, (mode, uid, gid) in sorted(directories.items())
        ),
        manifest_files=_manifest_file_receipt(manifest),
        privileged_files=privileged_files,
        privileged_backup_files=privileged_backup_files,
    )


def _revalidate_recovery_backup_receipt(
    receipt: RecoveryBackupReceipt,
    repo_contract: RepoRecoveryContract,
    *,
    backup_dir: str,
    repo_dir: str,
    expected_commit: str,
    install_user: str,
) -> tuple[
    dict[str, tuple[str, int, int, int, int]],
    dict[str, tuple[int, int, int]],
]:
    """Akzeptiert nur exakt den vor Mutation im Root-Prozess eingefrorenen Beleg."""

    if not isinstance(receipt, RecoveryBackupReceipt):
        raise RuntimeError("Recovery-Receipt fehlt oder besitzt einen falschen Typ")
    if not isinstance(repo_contract, RepoRecoveryContract):
        raise RuntimeError("Repo-Recovery-Vertrag fehlt oder besitzt einen falschen Typ")
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("Recovery-Receipt darf ausschließlich Root auswerten")
    root = os.path.abspath(repo_dir)
    commit = _validate_full_commit(expected_commit)
    if (
        os.path.abspath(backup_dir) != backup_dir
        or receipt.backup_dir != backup_dir
        or not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(receipt.transaction_id)
        or receipt.install_root != root
        or receipt.expected_commit != commit
        or repo_contract.install_root != root
        or repo_contract.install_user != str(install_user)
        or repo_contract.expected_commit != commit
        or _bound_release_head_commit(root, install_user) != commit
    ):
        raise RuntimeError("Recovery-Receipt weicht vom gebundenen Rückfallziel ab")
    backup_descriptor, backup_path_chain = _open_root_receipt_directory_chain(
        backup_dir,
    )
    try:
        backup_metadata = os.fstat(backup_descriptor)
    finally:
        os.close(backup_descriptor)
    if (
        not stat.S_ISDIR(backup_metadata.st_mode)
        or backup_metadata.st_uid != 0
        or backup_metadata.st_gid != 0
        or stat.S_IMODE(backup_metadata.st_mode) != 0o700
        or backup_path_chain != receipt.backup_path_chain
        or len(backup_path_chain) < 2
        or (backup_path_chain[-2][1], backup_path_chain[-2][2])
        != (receipt.parent_dev, receipt.parent_ino)
        or (backup_metadata.st_dev, backup_metadata.st_ino)
        != (receipt.backup_dev, receipt.backup_ino)
    ):
        raise RuntimeError("Recovery-Backup- oder Elterninode weicht vom Root-Receipt ab")
    manifest, manifest_sha256 = _read_stable_verified_backup_manifest(backup_dir)
    stable_descriptor, stable_chain = _open_root_receipt_directory_chain(backup_dir)
    os.close(stable_descriptor)
    if (
        stable_chain != receipt.backup_path_chain
        or
        manifest_sha256 != receipt.manifest_sha256
        or _manifest_semantic_sha256(manifest) != receipt.manifest_semantic_sha256
        or str(manifest.get("backup_id") or "") != receipt.backup_id
        or str(manifest.get("install_root") or "") != receipt.install_root
        or _manifest_file_receipt(manifest) != receipt.manifest_files
        or _privileged_restore_contract_from_manifest(
            manifest,
            install_user,
            verify_sources=False,
        )
        != receipt.privileged_files
        or _privileged_backup_payload_receipts(backup_dir, manifest)
        != receipt.privileged_backup_files
    ):
        raise RuntimeError("Recovery-Backup weicht vom Root-Receipt ab")
    tracked_entries = _tracked_release_file_contracts(
        root,
        install_user,
        target_commit=commit,
    )
    frozen_git_entries = tuple(
        (relative_path, git_mode, git_oid)
        for relative_path, git_mode, git_oid, _sha, _size, _mode, _uid, _gid
        in repo_contract.tracked_files
    )
    if tuple(tracked_entries) != frozen_git_entries:
        raise RuntimeError("Git-Dateivertrag weicht vom Repo-Recovery-Vertrag ab")
    files, directories = _recovery_repo_contracts_from_manifest(
        manifest,
        root,
        tracked_entries,
    )
    current_files = tuple(
        (relative_path, digest, size, mode, uid, gid)
        for relative_path, (digest, size, mode, uid, gid) in sorted(files.items())
    )
    current_directories = tuple(
        (relative_path, mode, uid, gid)
        for relative_path, (mode, uid, gid) in sorted(directories.items())
    )
    if (
        current_files != receipt.tracked_files
        or current_directories != receipt.tracked_directories
    ):
        raise RuntimeError("Recovery-Produktvertrag weicht vom Root-Receipt ab")
    preimages = {
        relative_path: (digest, size, mode, uid, gid)
        for relative_path, _git_mode, _git_oid, digest, size, mode, uid, gid
        in repo_contract.tracked_files
    }
    if files != preimages:
        raise RuntimeError("Backup-Receipt weicht vom Repo-Recovery-Preimage ab")
    return preimages, directories


def _guard_recovery_manifest(
    manifest: dict,
    receipt: RecoveryBackupReceipt,
) -> None:
    """Bindet genau das vom Restore verwendete Manifest an den Root-Receipt."""

    if not isinstance(manifest, dict) or not isinstance(receipt, RecoveryBackupReceipt):
        raise RuntimeError("Restore-Manifestguard besitzt keinen gültigen Receipt")
    if (
        str(manifest.get("backup_id") or "") != receipt.backup_id
        or str(manifest.get("install_root") or "") != receipt.install_root
        or _manifest_semantic_sha256(manifest) != receipt.manifest_semantic_sha256
        or _manifest_file_receipt(manifest) != receipt.manifest_files
        or _privileged_restore_contract_from_manifest(
            manifest,
            "",
            verify_sources=False,
        )
        != receipt.privileged_files
    ):
        raise RuntimeError("Restore verwendet nicht das root-autorisierte Backupmanifest")


def _secure_repo_permissions(
    repo_dir: str,
    install_user: str,
    *,
    expected_commit: str | None = None,
    recovery_backup_dir: str | None = None,
    recovery_repo_contract: RepoRecoveryContract | None = None,
    recovery_backup_receipt: RecoveryBackupReceipt | None = None,
) -> None:
    """Härtet ausschließlich den von Git gebundenen Produktbaum.

    Unversionierte Laufzeitdaten können absichtlich innerhalb des
    Installationsbaums liegen, etwa der private Matter-Zustand. Sie gehören
    nicht zum Release und dürfen deshalb weder rekursiv umgehängt noch auf
    allgemeine Repository-Rechte aufgeweitet werden.
    """
    root = os.path.abspath(repo_dir)
    account = pwd.getpwnam(str(install_user))
    bound_commit = (
        _validate_full_commit(expected_commit)
        if expected_commit is not None
        else _bound_release_head_commit(root, install_user)
    )
    if _bound_release_head_commit(root, install_user) != bound_commit:
        raise RuntimeError(
            "Repository-HEAD weicht vor der Rechtehärtung vom gebundenen "
            "Produkt-Commit ab"
        )
    tracked_entries = _tracked_release_file_contracts(
        root,
        install_user,
        target_commit=bound_commit,
    )
    recovery_file_contracts: dict[str, tuple[str, int, int, int, int]] = {}
    recovery_directory_contracts: dict[str, tuple[int, int, int]] = {}
    recovery_requested = any(
        item is not None
        for item in (
            recovery_backup_dir,
            recovery_repo_contract,
            recovery_backup_receipt,
        )
    )
    if recovery_requested:
        if (
            recovery_backup_dir is None
            or recovery_repo_contract is None
            or recovery_backup_receipt is None
            or expected_commit is None
        ):
            raise RuntimeError("Recovery-Rechtehärtung besitzt keinen vollständigen Receipt-Vertrag")
        recovery_file_contracts, recovery_directory_contracts = (
            _revalidate_recovery_backup_receipt(
                recovery_backup_receipt,
                recovery_repo_contract,
                backup_dir=recovery_backup_dir,
                repo_dir=root,
                expected_commit=bound_commit,
                install_user=install_user,
            )
        )
    tracked_directories = {""}
    for relative_path, _expected_mode, _expected_oid in tracked_entries:
        parent = Path(relative_path).parent
        while str(parent) not in {"", "."}:
            tracked_directories.add(parent.as_posix())
            parent = parent.parent

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    temporary_file_flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    directory_descriptors: dict[str, int] = {}
    directory_contracts: dict[str, tuple[int, int, int, int, int]] = {}
    file_contracts: dict[
        str,
        tuple[int, int, int, int, int, int, int, int, int],
    ] = {}
    entry_by_path = {
        relative_path: (expected_mode, expected_oid)
        for relative_path, expected_mode, expected_oid in tracked_entries
    }
    root_path = Path(root)
    root_name = root_path.name
    if not root_name or root_path.parent == root_path:
        raise RuntimeError("Repository-Wurzel ist für eine sichere Bindung ungeeignet")
    root_parent_descriptor: int | None = None

    def directory_contract(metadata) -> tuple[int, int, int, int, int]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_uid,
            metadata.st_gid,
            stat.S_IMODE(metadata.st_mode),
        )

    def file_contract(metadata) -> tuple[int, int, int, int, int, int, int, int, int]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            metadata.st_nlink,
            metadata.st_uid,
            metadata.st_gid,
            stat.S_IMODE(metadata.st_mode),
        )

    def descriptor_sha256(descriptor: int, expected_size: int) -> str:
        if int(expected_size) < 0:
            raise RuntimeError("Recovery-Dateigröße ist ungültig")
        digest = hashlib.sha256()
        os.lseek(descriptor, 0, os.SEEK_SET)
        remaining = int(expected_size)
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                raise RuntimeError("Recovery-Produktdatei endet vor der Manifestgröße")
            digest.update(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise RuntimeError("Recovery-Produktdatei überschreitet die Manifestgröße")
        return digest.hexdigest()

    def descriptor_content_matches(
        descriptor: int,
        relative_path: str,
        expected_size: int,
        expected_oid: str,
    ) -> bool:
        recovery = recovery_file_contracts.get(relative_path)
        if recovery is not None:
            digest, size, _mode, _uid, _gid = recovery
            return int(expected_size) == size and descriptor_sha256(
                descriptor,
                size,
            ) == digest
        return (
            _git_blob_oid_from_descriptor(
                descriptor,
                expected_size,
                expected_oid,
            )
            == expected_oid
        )

    def open_bound_directory(
        parent_descriptor: int,
        name: str,
        label: str,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> int:
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            raise RuntimeError(
                f"Getracktes Produktverzeichnis ist kein Verzeichnis: {label}"
            )
        descriptor = os.open(
            name,
            directory_flags,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(before.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (before.st_dev, before.st_ino)
            or (
                expected_identity is not None
                and (opened.st_dev, opened.st_ino) != expected_identity
            )
        ):
            os.close(descriptor)
            raise RuntimeError(
                "Getracktes Produktverzeichnis wurde während der Bindung "
                f"ausgetauscht: {label}"
            )
        return descriptor

    def copy_to_hardened_inode(
        *,
        parent_descriptor: int,
        name: str,
        relative_path: str,
        source_descriptor: int,
        source_metadata,
        expected_mode: int,
        expected_oid: str,
    ) -> int:
        """Ersetzt nur bei Metadatenbedarf atomar durch einen neuen Produkt-Inode."""

        temporary_name = ""
        temporary_descriptor: int | None = None
        installed = False
        try:
            for _attempt in range(32):
                temporary_name = (
                    f".e3dc-permissions-{os.getpid()}-{secrets.token_hex(12)}"
                )
                try:
                    temporary_descriptor = os.open(
                        temporary_name,
                        temporary_file_flags,
                        0o600,
                        dir_fd=parent_descriptor,
                    )
                    break
                except FileExistsError:
                    temporary_name = ""
            if temporary_descriptor is None:
                raise RuntimeError(
                    f"Temporärer Produkt-Inode konnte nicht erzeugt werden: {relative_path}"
                )

            os.lseek(source_descriptor, 0, os.SEEK_SET)
            remaining = int(source_metadata.st_size)
            while remaining:
                block = os.read(source_descriptor, min(1024 * 1024, remaining))
                if not block:
                    raise RuntimeError(
                        f"Produktdatei endete während der Kopie: {relative_path}"
                    )
                view = memoryview(block)
                while view:
                    written = os.write(temporary_descriptor, view)
                    if written <= 0:
                        raise RuntimeError(
                            f"Produktdatei konnte nicht kopiert werden: {relative_path}"
                        )
                    view = view[written:]
                remaining -= len(block)
            if os.read(source_descriptor, 1):
                raise RuntimeError(
                    f"Produktdatei wuchs während der Kopie: {relative_path}"
                )
            # Daten zuerst als vollständigen Temp-Payload persistieren; der
            # zweite fsync nach den Metadaten bindet anschließend den finalen
            # Inodevertrag.
            os.fsync(temporary_descriptor)
            os.fchown(temporary_descriptor, account.pw_uid, account.pw_gid)
            os.fchmod(temporary_descriptor, expected_mode)
            os.utime(
                temporary_descriptor,
                ns=(source_metadata.st_atime_ns, source_metadata.st_mtime_ns),
            )
            # Erst die finalen Bytes *und* Metadaten dauerhaft schreiben. Ein
            # früheres fsync vor chown/chmod/utime würde nur den 0600-Tempstand
            # belegen und könnte nach Gate-Clear beim Neustart zurückfallen.
            os.fsync(temporary_descriptor)
            hardened = os.fstat(temporary_descriptor)
            if (
                not stat.S_ISREG(hardened.st_mode)
                or hardened.st_nlink != 1
                or hardened.st_uid != account.pw_uid
                or hardened.st_gid != account.pw_gid
                or stat.S_IMODE(hardened.st_mode) != expected_mode
                or hardened.st_size != source_metadata.st_size
                or not descriptor_content_matches(
                    temporary_descriptor,
                    relative_path,
                    hardened.st_size,
                    expected_oid,
                )
            ):
                raise RuntimeError(
                    f"Neuer Produkt-Inode ist nicht exakt gebunden: {relative_path}"
                )

            source_after = os.fstat(source_descriptor)
            live_source = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(source_after.st_mode)
                or source_after.st_nlink != 1
                or (
                    source_after.st_dev,
                    source_after.st_ino,
                    source_after.st_size,
                    source_after.st_mtime_ns,
                    source_after.st_ctime_ns,
                )
                != (
                    source_metadata.st_dev,
                    source_metadata.st_ino,
                    source_metadata.st_size,
                    source_metadata.st_mtime_ns,
                    source_metadata.st_ctime_ns,
                )
                or (live_source.st_dev, live_source.st_ino)
                != (source_after.st_dev, source_after.st_ino)
            ):
                raise RuntimeError(
                    f"Produktdatei driftete vor dem atomaren Einsatz: {relative_path}"
                )

            live_temporary = os.stat(
                temporary_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                live_temporary.st_dev,
                live_temporary.st_ino,
            ) != (
                hardened.st_dev,
                hardened.st_ino,
            ):
                raise RuntimeError(
                    f"Temporärer Produkt-Inode wurde ausgetauscht: {relative_path}"
                )
            os.replace(
                temporary_name,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            # Der neue Inode ist erst nach dem Directory-fsync auch als Name
            # rebootfest. Jeder Fehler bleibt im Recoverypfad fail-closed.
            os.fsync(parent_descriptor)
            installed = True
            live_hardened = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            hardened_after = os.fstat(temporary_descriptor)
            if (
                file_contract(live_hardened)
                != file_contract(hardened_after)
                or not descriptor_content_matches(
                    temporary_descriptor,
                    relative_path,
                    hardened_after.st_size,
                    expected_oid,
                )
            ):
                raise RuntimeError(
                    f"Atomar eingesetzter Produkt-Inode driftete: {relative_path}"
                )
            result = temporary_descriptor
            temporary_descriptor = None
            return result
        finally:
            if temporary_descriptor is not None:
                try:
                    if temporary_name and not installed:
                        named = os.stat(
                            temporary_name,
                            dir_fd=parent_descriptor,
                            follow_symlinks=False,
                        )
                        opened = os.fstat(temporary_descriptor)
                        if (named.st_dev, named.st_ino) == (
                            opened.st_dev,
                            opened.st_ino,
                        ):
                            os.unlink(temporary_name, dir_fd=parent_descriptor)
                except (FileNotFoundError, OSError):
                    pass
                os.close(temporary_descriptor)

    def verify_live_generation() -> None:
        rebound_descriptors: dict[str, int] = {}
        rebound_parent_descriptor: int | None = None
        try:
            rebound_parent_descriptor = _open_directory_nofollow(root_path.parent)
            root_descriptor = open_bound_directory(
                rebound_parent_descriptor,
                root_name,
                root,
                expected_identity=directory_contracts[""][:2],
            )
            rebound_descriptors[""] = root_descriptor
            for relative_directory in sorted(
                tracked_directories - {""},
                key=lambda item: (len(Path(item).parts), item),
            ):
                parent_relative = Path(relative_directory).parent.as_posix()
                if parent_relative == ".":
                    parent_relative = ""
                descriptor = open_bound_directory(
                    rebound_descriptors[parent_relative],
                    Path(relative_directory).name,
                    relative_directory,
                    expected_identity=directory_contracts[relative_directory][:2],
                )
                rebound_descriptors[relative_directory] = descriptor
                if directory_contract(os.fstat(descriptor)) != directory_contracts[
                    relative_directory
                ]:
                    raise RuntimeError(
                        "Getracktes Produktverzeichnis driftete nach der "
                        f"Härtung: {relative_directory}"
                    )

            if directory_contract(os.fstat(root_descriptor)) != directory_contracts[""]:
                raise RuntimeError(
                    f"Getracktes Produktverzeichnis driftete nach der Härtung: {root}"
                )

            for relative_path, expected_metadata in file_contracts.items():
                expected_mode, expected_oid = entry_by_path[relative_path]
                parent_relative = Path(relative_path).parent.as_posix()
                if parent_relative == ".":
                    parent_relative = ""
                parent_descriptor = rebound_descriptors[parent_relative]
                name = Path(relative_path).name
                before = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or file_contract(before) != expected_metadata
                ):
                    raise RuntimeError(
                        f"Getrackte Produktdatei driftete nach der Härtung: {relative_path}"
                    )
                descriptor = os.open(
                    name,
                    file_flags,
                    dir_fd=parent_descriptor,
                )
                try:
                    opened = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or file_contract(opened) != expected_metadata
                        or (opened.st_dev, opened.st_ino)
                        != (before.st_dev, before.st_ino)
                        or opened.st_uid != account.pw_uid
                        or opened.st_gid != account.pw_gid
                        or stat.S_IMODE(opened.st_mode) != expected_mode
                        or not descriptor_content_matches(
                            descriptor,
                            relative_path,
                            opened.st_size,
                            expected_oid,
                        )
                    ):
                        raise RuntimeError(
                            "Getrackte Produktdatei besitzt keinen stabilen "
                            f"Endvertrag: {relative_path}"
                        )
                    after_hash = os.fstat(descriptor)
                    named_after = os.stat(
                        name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        file_contract(after_hash) != expected_metadata
                        or file_contract(named_after) != expected_metadata
                    ):
                        raise RuntimeError(
                            "Getrackte Produktdatei driftete nach dem "
                            f"Endhash: {relative_path}"
                        )
                finally:
                    os.close(descriptor)

            # Nach allen Datei-Hashes wird jede Verzeichnisbezeichnung erneut
            # von ihrer gebundenen Elternkante geprüft. Ein Austausch nach
            # os.open bleibt damit nicht unbemerkt an einem alten FD hängen.
            live_root = os.stat(
                root_name,
                dir_fd=rebound_parent_descriptor,
                follow_symlinks=False,
            )
            if directory_contract(live_root) != directory_contracts[""]:
                raise RuntimeError(
                    f"Getracktes Produktverzeichnis driftete nach der Härtung: {root}"
                )
            for relative_directory in sorted(
                tracked_directories - {""},
                key=lambda item: (len(Path(item).parts), item),
            ):
                parent_relative = Path(relative_directory).parent.as_posix()
                if parent_relative == ".":
                    parent_relative = ""
                live_directory = os.stat(
                    Path(relative_directory).name,
                    dir_fd=rebound_descriptors[parent_relative],
                    follow_symlinks=False,
                )
                if (
                    directory_contract(live_directory)
                    != directory_contracts[relative_directory]
                ):
                    raise RuntimeError(
                        "Getracktes Produktverzeichnis driftete nach der "
                        f"Härtung: {relative_directory}"
                    )
        finally:
            for descriptor in reversed(tuple(rebound_descriptors.values())):
                os.close(descriptor)
            if rebound_parent_descriptor is not None:
                os.close(rebound_parent_descriptor)

    try:
        root_parent_descriptor = _open_directory_nofollow(root_path.parent)
        root_descriptor = open_bound_directory(
            root_parent_descriptor,
            root_name,
            root,
        )
        directory_descriptors[""] = root_descriptor
        for relative_directory in sorted(
            tracked_directories,
            key=lambda item: (len(Path(item).parts), item),
        ):
            descriptor = (
                root_descriptor
                if relative_directory == ""
                else open_bound_directory(
                    directory_descriptors[
                        (
                            ""
                            if Path(relative_directory).parent.as_posix() == "."
                            else Path(relative_directory).parent.as_posix()
                        )
                    ],
                    Path(relative_directory).name,
                    relative_directory,
                )
            )
            if relative_directory:
                directory_descriptors[relative_directory] = descriptor
            before = os.fstat(descriptor)
            recovery_directory = recovery_directory_contracts.get(relative_directory)
            if recovery_directory is not None and (
                stat.S_IMODE(before.st_mode),
                before.st_uid,
                before.st_gid,
            ) != recovery_directory:
                raise RuntimeError(
                    "Recovery-Produktverzeichnis weicht vom Backupmanifest ab: "
                    + (relative_directory or root)
                )
            if not stat.S_ISDIR(before.st_mode):
                raise RuntimeError(
                    "Getracktes Produktverzeichnis besitzt unsichere Metadaten: "
                    + (relative_directory or root)
                )
            if before.st_uid != account.pw_uid or before.st_gid != account.pw_gid:
                os.fchown(descriptor, account.pw_uid, account.pw_gid)
            if stat.S_IMODE(before.st_mode) != 0o755:
                os.fchmod(descriptor, 0o755)
            # Auch Verzeichnis-Metadaten müssen vor dem Recovery-Endgate auf
            # dem gebundenen Inode dauerhaft sein. Ein Live-Readback allein
            # schützt nicht gegen ein Metadaten-Rollback nach Stromverlust.
            os.fsync(descriptor)
            after = os.fstat(descriptor)
            if relative_directory:
                parent_relative = Path(relative_directory).parent.as_posix()
                if parent_relative == ".":
                    parent_relative = ""
                live_parent_descriptor = directory_descriptors[parent_relative]
                live_name = Path(relative_directory).name
            else:
                live_parent_descriptor = root_parent_descriptor
                live_name = root_name
            live_after = os.stat(
                live_name,
                dir_fd=live_parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(after.st_mode)
                or (after.st_dev, after.st_ino)
                != (before.st_dev, before.st_ino)
                or directory_contract(live_after) != directory_contract(after)
                or after.st_uid != account.pw_uid
                or after.st_gid != account.pw_gid
                or stat.S_IMODE(after.st_mode) != 0o755
            ):
                raise RuntimeError(
                    "Getracktes Produktverzeichnis konnte nicht exakt "
                    f"gehärtet werden: {relative_directory or root}"
                )
            directory_contracts[relative_directory] = directory_contract(after)

        for relative_path, expected_mode, expected_oid in tracked_entries:
            parent_relative = Path(relative_path).parent.as_posix()
            if parent_relative == ".":
                parent_relative = ""
            parent_descriptor = directory_descriptors[parent_relative]
            name = Path(relative_path).name
            before = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            recovery_file = recovery_file_contracts.get(relative_path)
            if recovery_file is not None:
                _digest, recovery_size, recovery_mode, recovery_uid, recovery_gid = (
                    recovery_file
                )
                if (
                    before.st_size != recovery_size
                    or stat.S_IMODE(before.st_mode) != recovery_mode
                    or before.st_uid != recovery_uid
                    or before.st_gid != recovery_gid
                ):
                    raise RuntimeError(
                        "Recovery-Produktdatei weicht in den Metadaten vom "
                        f"Backupmanifest ab: {relative_path}"
                    )
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
            ):
                raise RuntimeError(
                    "Getrackte Produktdatei besitzt unsichere oder "
                    f"abweichende Metadaten: {relative_path}"
                )
            descriptor = os.open(
                name,
                file_flags,
                dir_fd=parent_descriptor,
            )
            try:
                opened = os.fstat(descriptor)
                stable_identity = (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                    opened.st_nlink,
                    opened.st_uid,
                    opened.st_gid,
                    stat.S_IMODE(opened.st_mode),
                )
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino)
                    != (before.st_dev, before.st_ino)
                    or not descriptor_content_matches(
                        descriptor,
                        relative_path,
                        opened.st_size,
                        expected_oid,
                    )
                ):
                    raise RuntimeError(
                        "Getrackte Produktdatei besitzt unsichere oder "
                        f"abweichende Metadaten: {relative_path}"
                    )
                stable_before_mutation = os.fstat(descriptor)
                if (
                    stable_before_mutation.st_dev,
                    stable_before_mutation.st_ino,
                    stable_before_mutation.st_size,
                    stable_before_mutation.st_mtime_ns,
                    stable_before_mutation.st_ctime_ns,
                    stable_before_mutation.st_nlink,
                    stable_before_mutation.st_uid,
                    stable_before_mutation.st_gid,
                    stat.S_IMODE(stable_before_mutation.st_mode),
                ) != stable_identity:
                    raise RuntimeError(
                        f"Getrackte Produktdatei driftete vor der Härtung: {relative_path}"
                    )
                if (
                    stable_before_mutation.st_uid != account.pw_uid
                    or stable_before_mutation.st_gid != account.pw_gid
                    or stat.S_IMODE(stable_before_mutation.st_mode)
                    != expected_mode
                    or _repo_descriptor_has_unsafe_xattrs(descriptor)
                ):
                    replacement_descriptor = copy_to_hardened_inode(
                        parent_descriptor=parent_descriptor,
                        name=name,
                        relative_path=relative_path,
                        source_descriptor=descriptor,
                        source_metadata=stable_before_mutation,
                        expected_mode=expected_mode,
                        expected_oid=expected_oid,
                    )
                    os.close(descriptor)
                    descriptor = replacement_descriptor
                after = os.fstat(descriptor)
                after_contract = file_contract(after)
                if (
                    not stat.S_ISREG(after.st_mode)
                    or after.st_nlink != 1
                    or after.st_uid != account.pw_uid
                    or after.st_gid != account.pw_gid
                    or stat.S_IMODE(after.st_mode) != expected_mode
                    or not descriptor_content_matches(
                        descriptor,
                        relative_path,
                        after.st_size,
                        expected_oid,
                    )
                ):
                    raise RuntimeError(
                        "Getrackte Produktdatei konnte nicht exakt "
                        f"gehärtet werden: {relative_path}"
                    )
                after_hash = os.fstat(descriptor)
                named_after = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    file_contract(after_hash) != after_contract
                    or file_contract(named_after) != after_contract
                ):
                    raise RuntimeError(
                        f"Getrackte Produktdatei driftete nach der Härtung: {relative_path}"
                    )
                file_contracts[relative_path] = after_contract
            finally:
                os.close(descriptor)

        verify_live_generation()
        if _bound_release_head_commit(root, install_user) != bound_commit:
            raise RuntimeError(
                "Repository-HEAD driftete während der Rechtehärtung vom "
                "gebundenen Produkt-Commit"
            )
    finally:
        for descriptor in reversed(tuple(directory_descriptors.values())):
            os.close(descriptor)
        if root_parent_descriptor is not None:
            os.close(root_parent_descriptor)
    if recovery_repo_contract is not None:
        _verify_recovered_repo_contract(
            root,
            install_user,
            recovery_repo_contract,
        )


def _service_expected(service: str, state: TransitionState) -> tuple[bool, str]:
    name = str(service).removesuffix(".service")
    if name == "e3dc":
        return False, "Legacy-C++-Dienst bleibt dauerhaft deaktiviert"
    if name in _ha_slave_standby_services(state):
        return False, f"durch Rolle {state.ha_role} explizit Standby"
    if name in INSTALL_CENTER_CORE_SERVICES:
        return True, "Pflichtdienst des Install-Centers"
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
    expected_when_installed = preinstalled_optional_service_expected(name, state.config)
    if not expected_when_installed:
        return False, "Feature ist in eingefrorener Konfiguration explizit deaktiviert"
    if unit not in state.preinstalled_units:
        return False, "optionaler Dienst war vor dem Wechsel nicht installiert"
    return True, "vor dem Wechsel installiert und nicht explizit deaktiviert"


def _release_service_expected(
    service: str,
    state: TransitionState,
    *,
    projected_piguard: bool = False,
) -> tuple[bool, str]:
    """Erweitert ausschließlich den Releasepfad um ein quiesced projiziertes PiGuard."""

    name = str(service).removesuffix(".service")
    if name == "piguard" and projected_piguard:
        return True, "Watchdog wurde vor dem Lease-Token quiesced projiziert"
    return _service_expected(service, state)


def _validated_restart_services(policy: dict, state: TransitionState) -> list[str]:
    if str(policy.get("restart_service_contract") or "") != "core_plus_preinstalled_v1":
        raise RuntimeError("restart_service_contract ist unbekannt oder fehlt")
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
    if tuple(normalized) != INSTALL_CENTER_CORE_SERVICES:
        raise RuntimeError(
            "restart_services muss exakt die Pflichtdienste des Install-Centers enthalten"
        )
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


def _harden_existing_release_venv(
    install_user: str,
    expected_path: str,
) -> None:
    """Migriert ausschließlich ein eindeutig gebundenes Bestands-venv auf 0755/0644-Sicherheit.

    Historische Installationen verwendeten teilweise die private Benutzergruppe
    mit 0775/0664. Das war für den damaligen Betrieb ausreichend, widerspricht
    aber dem heutigen Interpreter-Vertrag. Die Migration entfernt nur
    Schreibrechte für Gruppe und Andere; Eigentümer, Lese-/Ausführungsbits und
    Symlinkziele bleiben unverändert. Historisch root-installierte Pakete sind
    dabei ebenso vertrauenswürdig wie Pfade des gebundenen Installationsnutzers;
    andere Eigentümer bleiben gesperrt.
    """

    try:
        account = pwd.getpwnam(str(install_user))
    except KeyError as exc:
        raise RuntimeError("Installationsbenutzer für venv-Rechtemigration fehlt") from exc

    raw_home = Path(account.pw_dir)
    raw_target = Path(str(expected_path or ""))
    if (
        not raw_home.is_absolute()
        or raw_home.is_symlink()
        or not raw_target.is_absolute()
        or raw_target.is_symlink()
    ):
        raise RuntimeError("venv-Rechtemigration besitzt keinen kanonischen Ausgangspfad")
    home = raw_home.resolve(strict=True)
    target = raw_target.resolve(strict=True)
    if home != raw_home or target != raw_target or target.parent != home:
        raise RuntimeError("venv-Rechtemigration verlässt das gebundene Benutzer-Home")
    configured = Path(get_venv_path(str(install_user))).resolve(strict=True)
    if configured != target:
        raise RuntimeError("venv-Rechtemigration weicht vom konfigurierten venv ab")

    allowed_link_names = {"bin/python", "bin/python3"}
    entries: list[tuple[Path, os.stat_result]] = []
    entry_paths: set[Path] = set()
    for directory, dirnames, filenames in os.walk(
        target,
        topdown=True,
        followlinks=False,
    ):
        directory_path = Path(directory)
        names = [".", *sorted(dirnames), *sorted(filenames)]
        for name in names:
            path = directory_path if name == "." else directory_path / name
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise RuntimeError("venv-Rechtemigration konnte einen Pfad nicht binden") from exc
            relative = path.relative_to(target).as_posix() if path != target else "."
            if stat.S_ISLNK(metadata.st_mode):
                link_target = ""
                link_target_ok = False
                if relative == "lib64":
                    try:
                        link_target = os.readlink(path)
                        resolved_link = (path.parent / link_target).resolve(strict=True)
                        resolved_metadata = resolved_link.lstat()
                        link_target_ok = bool(
                            link_target == "lib"
                            and resolved_link == target / "lib"
                            and stat.S_ISDIR(resolved_metadata.st_mode)
                            and resolved_metadata.st_uid == account.pw_uid
                            and not venv_has_extended_acl(resolved_link)
                        )
                    except (OSError, RuntimeError):
                        link_target_ok = False
                if (
                    metadata.st_uid not in (0, account.pw_uid)
                    or venv_has_extended_acl(path)
                    or not (
                        relative in allowed_link_names
                        or re.fullmatch(r"bin/python3\.\d+", relative)
                        or link_target_ok
                    )
                ):
                    raise RuntimeError(
                        f"venv-Rechtemigration verweigert einen fremden Link: {relative}"
                    )
                continue
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise RuntimeError(
                    f"venv-Rechtemigration verweigert einen Sonderpfad: {relative}"
                )
            if venv_has_extended_acl(path):
                raise RuntimeError(
                    f"venv-Rechtemigration verweigert erweiterte ACLs: {relative}"
                )
            if metadata.st_uid not in (0, account.pw_uid):
                raise RuntimeError(
                    f"venv-Rechtemigration verweigert einen fremden Eigentümer: {relative}"
                )
            if metadata.st_mode & stat.S_IWOTH:
                raise RuntimeError(
                    f"venv-Rechtemigration verweigert einen weltbeschreibbaren Pfad: {relative}"
                )
            if (
                metadata.st_mode & stat.S_IWGRP
                and not venv_group_is_private(metadata.st_gid, account.pw_name)
            ):
                raise RuntimeError(
                    f"venv-Rechtemigration verweigert eine fremd beschreibbare Gruppe: {relative}"
                )
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                raise RuntimeError(
                    f"venv-Rechtemigration verweigert eine mehrfach verlinkte Datei: {relative}"
                )
            if path not in entry_paths:
                entries.append((path, metadata))
                entry_paths.add(path)

    for path, before in entries:
        requested_mode = stat.S_IMODE(before.st_mode) & ~0o022
        if requested_mode == stat.S_IMODE(before.st_mode):
            continue
        descriptor = -1
        try:
            if stat.S_ISDIR(before.st_mode):
                descriptor = _open_directory_nofollow(path)
            else:
                descriptor, _opened = _open_regular_file_nofollow(path)
            current = os.fstat(descriptor)
            if (
                current.st_dev != before.st_dev
                or current.st_ino != before.st_ino
                or current.st_uid != before.st_uid
                or current.st_gid != before.st_gid
                or current.st_mode != before.st_mode
                or current.st_nlink != before.st_nlink
                or venv_has_extended_acl(path)
            ):
                raise RuntimeError("venv-Pfad driftete vor der Rechtemigration")
            os.fchmod(descriptor, requested_mode)
            after = os.fstat(descriptor)
            path_after = path.lstat()
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_uid != before.st_uid
                or after.st_gid != before.st_gid
                or stat.S_IFMT(after.st_mode) != stat.S_IFMT(before.st_mode)
                or after.st_nlink != before.st_nlink
                or stat.S_IMODE(after.st_mode) != requested_mode
                or path_after.st_dev != after.st_dev
                or path_after.st_ino != after.st_ino
                or path_after.st_uid != after.st_uid
                or path_after.st_gid != after.st_gid
                or path_after.st_mode != after.st_mode
                or path_after.st_nlink != after.st_nlink
                or venv_has_extended_acl(path)
            ):
                raise RuntimeError(
                    "venv-Rechtemigration konnte einen Modus nicht verifizieren: "
                    f"{path.relative_to(target)} erwartet={requested_mode:04o} "
                    f"gefunden={stat.S_IMODE(after.st_mode):04o}"
                )
        finally:
            if descriptor >= 0:
                os.close(descriptor)


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
        process_bound_venv_path = bool(expected_venv_path)
        if expected_venv_state not in {"present", "missing"}:
            raise RuntimeError("Erwarteter venv-Ausgangszustand ist ungültig")
        if expected_venv_state == "present" and process_bound_venv_path:
            _harden_existing_release_venv(
                install_user,
                expected_venv_path,
            )
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
        if process_bound_venv_path:
            require_bound_venv_runtime(
                install_user=install_user,
                venv_path=actual_venv_path,
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
    require_runtime_venv: bool = False,
) -> PackageTransactionState:
    if not isinstance(require_runtime_venv, bool):
        raise RuntimeError("Laufzeit-venv-Vertrag ist nicht boolesch")
    apt_requested = tuple(_validated_release_apt_packages(policy))
    pip_requested = tuple(_validated_venv_pip_packages(policy))
    apt_before = _installed_apt_packages() if apt_requested else frozenset()
    venv_python = (
        _find_venv_python(install_user)
        if pip_requested or require_runtime_venv
        else None
    )
    venv_existed = bool(venv_python)
    venv_path = str(Path(venv_python).parent.parent) if venv_python else None
    if require_runtime_venv and not venv_python:
        raise RuntimeError(
            "Installierter Watchdog besitzt keinen vertrauensgebundenen "
            "venv-Interpreter"
        )
    if pip_requested and not venv_python:
        if not allow_missing_venv:
            raise RuntimeError("Python-Pakete angefordert, aber kein verifiziertes venv gefunden")
        if "python3-venv" not in apt_requested:
            raise RuntimeError("Missing-venv-Bootstrap verlangt python3-venv in der Release-Policy")
        venv_path = _release_venv_path(install_user)
        if os.path.lexists(venv_path):
            raise RuntimeError("Fehlendes venv ist durch einen bestehenden Pfad blockiert")
    pip_before = (
        _installed_pip_packages(venv_python, install_user)
        if pip_requested and venv_python
        else {}
    )
    return PackageTransactionState(
        apt_before=apt_before,
        pip_before=tuple(sorted(pip_before.items())),
        venv_python=venv_python,
        install_user=str(install_user),
        apt_requested=apt_requested,
        pip_requested=pip_requested,
        venv_path=venv_path,
        venv_existed=venv_existed,
        runtime_venv_required=require_runtime_venv,
    )


def _watchdog_runtime_venv_required(state: TransitionState) -> bool:
    """Erkennt einen vorhandenen Watchdog unabhängig von der pip-Policy."""

    return bool(
        PIGUARD_UNIT in state.preinstalled_units
        or os.path.exists(PIGUARD_FRAGMENT_PATH)
        or os.path.exists(PIGUARD_EXECUTABLE_PATH)
    )


def _finalizer_venv_contract(
    package_transaction: PackageTransactionState,
) -> tuple[str, str]:
    """Trennt Paketmutation und bereits vorhandenen Watchdog-Laufzeitkontext."""

    runtime_required = bool(
        getattr(package_transaction, "runtime_venv_required", False)
    )
    if not package_transaction.pip_requested and not runtime_required:
        return "unused", ""
    state = "present" if package_transaction.venv_existed else "missing"
    path = str(package_transaction.venv_path or "")
    if not os.path.isabs(path) or os.path.abspath(path) != path:
        raise RuntimeError("venv-Preimage besitzt keinen kanonischen absoluten Pfad")
    if runtime_required and state != "present":
        raise RuntimeError("Watchdog-Laufzeit-venv ist nicht vorhanden")
    return state, path


def _validate_watchdog_runtime_venv_contract(
    *,
    required: bool,
    expected_venv_state: str,
    expected_venv_path: str,
    target_root: str,
    install_user: str,
):
    """Prüft den Watchdog-Interpreter explizit vor jedem finalen Dienststart."""

    if not required:
        return None
    if expected_venv_state != "present":
        raise RuntimeError("Installierter Watchdog besitzt kein vorhandenes Laufzeit-venv")
    path = str(expected_venv_path or "")
    if not os.path.isabs(path) or os.path.abspath(path) != path:
        raise RuntimeError("Watchdog-Laufzeit-venv besitzt keinen kanonischen Pfad")
    context = get_transition_context(
        explicit_install_path=target_root,
        explicit_install_user=install_user,
        explicit_venv_path=path,
        require_trusted=True,
    )
    expected_python = os.path.join(path, "bin", "python3")
    if (
        not context.trusted
        or context.install_path != target_root
        or context.install_user != install_user
        or context.venv_path != path
        or context.venv_python != expected_python
    ):
        raise RuntimeError("Watchdog-Laufzeitkontext weicht vom gebundenen venv ab")
    return context


def _run_with_bound_bootstrap_venv(venv_path: str, callback):
    """Gibt genau einem synchronen Watchdog-Aufruf den vorgeprüften venv-Pfad."""

    path = str(venv_path or "")
    if not os.path.isabs(path) or os.path.abspath(path) != path:
        raise RuntimeError("Temporäre Watchdog-venv-Bindung ist nicht kanonisch")
    variable = "E3DC_BOOTSTRAP_VENV"
    previous = os.environ.get(variable)
    os.environ[variable] = path
    try:
        return callback()
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous


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


def get_current_commit(repo_dir: str) -> str | None:
    """Liest den aktuellen Git-Commit-Hash des Repos."""
    install_user = get_install_user()
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
    try:
        _require_bound_origin(repo_dir, install_user)
    except RuntimeError:
        return None
    return SELFUPDATE_REPO


def check_for_updates(repo_dir: str) -> int | None:
    """
    Prueft ob Updates verfuegbar sind.
    Gibt die Anzahl fehlender Commits zurueck, None bei Fehler.
    """
    install_user = get_install_user()
    try:
        _require_bound_origin(repo_dir, install_user)
        official = _official_remote_refs("refs/heads/main")["refs/heads/main"]
    except RuntimeError as exc:
        log_warning('update', f'GitHub-main konnte nicht gebunden werden: {exc}')
        return None
    fetch = _git_argv(
        repo_dir,
        install_user,
        "fetch",
        "--no-tags",
        SELFUPDATE_REPO,
        "+refs/heads/main:refs/remotes/origin/main",
        timeout=120,
    )
    if not fetch['success']:
        log_warning('update', f'git fetch fehlgeschlagen: {fetch["stderr"]}')
        return None
    fetched = _resolve_git_commit(repo_dir, "refs/remotes/origin/main", install_user)
    if not fetched or not _exact_commit_matches(fetched, official):
        log_warning('update', 'Gefetchtes origin/main weicht vom isoliert gebundenen GitHub-Ref ab')
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


def _stop_v4_services_impl(services=None):
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
    stop_order = tuple(dict.fromkeys(("piguard", *all_names, "e3dc")))
    # Hardware-Safety: PiGuard und danach jeder katalogisierte Writer erhalten
    # den Stop aus dem bereits geladenen systemd-Zustand, bevor die potenziell
    # langsameren Show-Inventarisierungen beginnen. Ein fehlgeschlagenes
    # Stop-Kommando ist nur dann unschädlich, wenn der anschließende strikte
    # Readback exakt inactive/failed oder canonical not-found beweist.
    stop_results = {}
    for srv in stop_order:
        stop_results[srv] = _run_argv(
            ["sudo", "systemctl", "stop", _unit_name(srv)],
            timeout=15,
        )
    for srv in stop_order:
        stopped = stop_results[srv]
        active = _run_argv(
            ["systemctl", "is-active", _unit_name(srv)],
            timeout=10,
        )
        activity_text = str(active.get("stdout") or "").strip().lower()
        if _systemd_activity_readback_matches(
            active,
            should_be_active=False,
        ):
            print(f"  [OK] {srv}")
            continue
        try:
            activity = (
                _capture_piguard_transition_activity()
                if _unit_name(srv) == PIGUARD_UNIT
                else _capture_transition_unit_activity(srv)
            )
        except Exception as exc:
            errors.append(
                f"{_unit_name(srv)} ist nach Sofortstop unklar: {exc}; "
                f"Stop={_command_result_diagnostic(stopped)}, "
                f"is-active={_command_result_diagnostic(active)}"
            )
            continue
        if activity == "absent":
            print(f"  [OK] {srv} (nicht installiert)")
            continue
        errors.append(
            f"{srv} hat nach Sofortstop keinen beweisbaren inaktiven Status "
            f"({activity_text or activity or 'unlesbar'}; "
            f"Stop={_command_result_diagnostic(stopped)})"
        )
    try:
        install_user = get_install_user()
    except Exception as exc:
        install_user = None
        errors.append(f"Installationsbenutzer ist für Legacy-Stop unklar: {exc}")
    screen_users = tuple(
        dict.fromkeys(
            ([str(install_user)] if install_user is not None else []) + ["root"]
        )
    )
    for screen_user in screen_users:
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
    for screen_user in screen_users:
        prefix = ["sudo", "-u", screen_user] if screen_user != "root" else ["sudo"]
        listing = _run_argv([*prefix, "screen", "-ls"], timeout=10)
        sessions = listing.get("stdout", "")
        if re.search(r"\.(?:e3dc|E3DC)(?:\s|$)", sessions):
            errors.append(f"Legacy-Screen-Session fuer {screen_user} ist weiterhin aktiv")
    # Stop-Abhängigkeiten und Watchdogs können eine zuvor gestoppte Unit
    # erneut aktivieren. Erst dieser zweite vollständige Pass beweist die
    # globale Aktorruhe; transient/unknown/unlesbar bleibt fail-closed.
    for srv in stop_order:
        try:
            show_activity = (
                _capture_piguard_transition_activity()
                if _unit_name(srv) == PIGUARD_UNIT
                else _capture_transition_unit_activity(srv)
            )
        except Exception as exc:
            errors.append(
                f"{_unit_name(srv)} ist im globalen Stop-Endgate unklar: {exc}"
            )
            continue
        if show_activity == "absent":
            continue
        active = _run_argv(
            ["systemctl", "is-active", _unit_name(srv)],
            timeout=10,
        )
        activity = str(active.get("stdout") or "").strip().lower()
        if not _systemd_activity_readback_matches(
            active,
            should_be_active=False,
        ):
            errors.append(
                f"{srv} ist im globalen Stop-Endgate nicht beweisbar inaktiv "
                f"({activity or 'unlesbar'})"
            )
    if errors:
        for error in errors:
            print(f'  [!] {error}')
        return False
    print('  [OK] Aktor-/Writer-Dienste sind fuer den Release-Wechsel in Ruhe.')
    return True


def _stop_v4_services(services=None):
    """Totaler Fail-closed-Wrapper: kein lokaler Bindefehler verlässt den Stop."""

    try:
        return bool(_stop_v4_services_impl(services))
    except Exception as exc:
        print(f"  [!] Stop-/Endgateprüfung brach intern ab: {exc}")
        update_logger.error("Stop-/Endgateprüfung brach intern ab: %s", exc)
        return False


def _assert_no_rogue_product_processes(repo_dir: str) -> None:
    """Blockiert katalogisierte Writer, die außerhalb der gestoppten Units laufen."""

    root = os.path.realpath(str(repo_dir))
    expected_scripts = set()
    for unit in _catalog_units_strict():
        module = get_module_by_service(unit)
        script = str(getattr(module, "script", "") or "")
        if script:
            expected_scripts.add(
                os.path.realpath(os.path.join(root, "Installer", script))
            )
    expected_scripts.add(os.path.realpath(PIGUARD_EXECUTABLE_PATH))
    findings = []
    for entry in os.listdir("/proc"):
        if not entry.isdecimal() or int(entry) == os.getpid():
            continue
        try:
            raw = Path(f"/proc/{entry}/cmdline").read_bytes()
            if not raw:
                continue
            argv = [
                item.decode("utf-8", errors="surrogateescape")
                for item in raw.rstrip(b"\0").split(b"\0")
            ]
            cwd = os.path.realpath(os.readlink(f"/proc/{entry}/cwd"))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise RuntimeError(
                f"Produktprozess {entry} ist am finalen Writer-Gate nicht lesbar"
            ) from exc
        matched = False
        for item in argv:
            if not item or item.startswith("-"):
                continue
            candidate = (
                os.path.realpath(item)
                if os.path.isabs(item)
                else os.path.realpath(os.path.join(cwd, item))
            )
            if candidate in expected_scripts:
                matched = True
                break
            if os.path.basename(item) in {"E3DC-Control", "E3DC.sh"}:
                matched = True
                break
        if matched:
            findings.append(int(entry))
    if findings:
        raise RuntimeError(
            "Katalogisierte Produktprozesse laufen außerhalb des gebundenen "
            "systemd-Endzustands: " + ",".join(str(pid) for pid in sorted(findings))
        )


def _assert_no_concurrent_update_processes(transaction_id: str) -> None:
    """Verwirft jede zweite transaktionsmarkierte Finalizer-/systemd-run-Kette."""

    _update_safety_names(transaction_id)
    foreign = []
    own_unit = _update_safety_names(transaction_id)[0]
    for entry in os.listdir("/proc"):
        if not entry.isdecimal() or int(entry) == os.getpid():
            continue
        try:
            raw = Path(f"/proc/{entry}/cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise RuntimeError(
                f"Updateprozess {entry} ist am finalen Gate nicht lesbar"
            ) from exc
        text = raw.replace(b"\0", b" ").decode("utf-8", errors="replace")
        if UPDATE_FINALIZER_UNIT_PREFIX not in text and "--update-safety-transaction" not in text:
            continue
        if own_unit not in text and transaction_id not in text:
            foreign.append(int(entry))
    transient_root = Path("/run/systemd/transient")
    try:
        entries = tuple(transient_root.iterdir())
    except FileNotFoundError:
        entries = ()
    for path in entries:
        if (
            path.name.startswith(UPDATE_FINALIZER_UNIT_PREFIX)
            and path.name.endswith(".service")
            and path.name != own_unit
        ):
            raise RuntimeError(
                f"Fremde transiente Update-Finalizer-Unit ist vorhanden: {path.name}"
            )
    if foreign:
        raise RuntimeError(
            "Fremde Update-Finalizer-Prozesse sind vorhanden: "
            + ",".join(str(pid) for pid in sorted(foreign))
        )


def _assert_strict_update_writer_quiescence(
    *,
    repo_dir: str,
    transaction_id: str,
) -> None:
    """Unmittelbares Endgate: jede Unit inactive/dead, MainPID 0, keine Rogue-Writer."""

    _assert_no_rogue_product_processes(repo_dir)
    _assert_no_concurrent_update_processes(transaction_id)
    expected_keys = {"LoadState", "ActiveState", "SubState", "MainPID"}
    errors = []
    for unit in _recovery_bootblock_units():
        result = _run_argv(
            [
                "systemctl",
                "show",
                "--no-pager",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=MainPID",
                unit,
            ],
            timeout=10,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        values = {}
        for line in str(result.get("stdout") or "").splitlines():
            key, separator, value = line.partition("=")
            if separator != "=" or key not in expected_keys or key in values:
                values = {}
                break
            values[key] = value
        load_state = values.get("LoadState", "").lower()
        if (
            not result.get("success")
            or result.get("timed_out")
            or int(result.get("returncode", -1)) != 0
            or str(result.get("stderr") or "")
            or set(values) != expected_keys
            or load_state not in {"loaded", "masked", "not-found"}
            or values.get("ActiveState", "").lower() != "inactive"
            or values.get("SubState", "").lower() != "dead"
            or values.get("MainPID") != "0"
        ):
            errors.append(
                f"{unit}={load_state or 'unlesbar'}/"
                f"{values.get('ActiveState') or 'unlesbar'}/"
                f"PID{values.get('MainPID') or '?'}"
            )
    # Die beiden Prozessprüfungen werden nach sämtlichen Unit-Readbacks erneut
    # ausgeführt. Damit bleibt kein Inventarisierungsfenster vor der Mutation.
    _assert_no_rogue_product_processes(repo_dir)
    _assert_no_concurrent_update_processes(transaction_id)
    if errors:
        raise RuntimeError(
            "Striktes Writer-Endgate ist nicht erfüllt: " + "; ".join(errors)
        )


def _read_finalizer_service_properties(unit: str) -> dict[str, str]:
    names = (
        "Id",
        "LoadState",
        "ActiveState",
        "SubState",
        "MainPID",
        "InvocationID",
        "ControlGroup",
        "FragmentPath",
        "DropInPaths",
        "Transient",
        "Type",
        "ExitType",
        "KillMode",
        "Restart",
        "User",
        "Group",
        "DynamicUser",
        "WorkingDirectory",
        "UMask",
        "Environment",
        "RuntimeDirectory",
        "RuntimeDirectoryMode",
        "RuntimeDirectoryPreserve",
        "RuntimeMaxUSec",
        "TimeoutStopUSec",
        "SendSIGKILL",
        "OOMPolicy",
    )
    result = _run_argv(
        [
            "systemctl",
            "show",
            "--no-pager",
            *(f"--property={name}" for name in names),
            unit,
        ],
        timeout=15,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )
    values = {}
    for line in str(result.get("stdout") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator != "=" or key not in names or key in values or value != value.strip():
            values = {}
            break
        values[key] = value
    if (
        not result.get("success")
        or result.get("timed_out")
        or int(result.get("returncode", -1)) != 0
        or str(result.get("stderr") or "")
        or set(values) != set(names)
    ):
        raise RuntimeError(f"Transiente Finalizer-Unit ist nicht vollständig lesbar: {unit}")
    return values


def _assert_managed_finalizer_service(contract: UpdateSafetyContract) -> dict[str, str]:
    """Bindet PID, Invocation, cgroup und sämtliche sicherheitsrelevanten Properties."""

    invocation = str(os.environ.get("E3DC_UPDATE_FINALIZER_INVOCATION_ID") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", invocation):
        raise RuntimeError("Finalizer besitzt keine gebundene systemd-InvocationID")
    values = _read_finalizer_service_properties(contract.finalizer_unit)
    expected = {
        "Id": contract.finalizer_unit,
        "LoadState": "loaded",
        "ActiveState": "active",
        "MainPID": str(os.getpid()),
        "InvocationID": invocation,
        "FragmentPath": f"/run/systemd/transient/{contract.finalizer_unit}",
        "DropInPaths": "",
        "Transient": "yes",
        "Type": "exec",
        "ExitType": "main",
        "KillMode": "control-group",
        "Restart": "no",
        "User": "root",
        "Group": "root",
        "DynamicUser": "no",
        "WorkingDirectory": "/",
        "UMask": "0077",
        "RuntimeDirectory": contract.runtime_directory,
        "RuntimeDirectoryMode": "0700",
        "RuntimeDirectoryPreserve": "no",
        "RuntimeMaxUSec": "35min",
        "TimeoutStopUSec": "15s",
        "SendSIGKILL": "yes",
        "OOMPolicy": "stop",
    }
    mismatches = [
        f"{name}={values.get(name)!r}"
        for name, value in expected.items()
        if values.get(name) != value
    ]
    control_group = values.get("ControlGroup", "")
    if (
        values.get("SubState") not in {"running", "start"}
        or not control_group.startswith("/")
        or not control_group.endswith("/" + contract.finalizer_unit)
    ):
        mismatches.append("cgroup/runtime/timeout")
    expected_environment = (
        f"E3DC_BOOTSTRAP_ROOT={os.environ.get('E3DC_BOOTSTRAP_ROOT', '')}",
        f"E3DC_BOOTSTRAP_RUNNER_ROOT={os.environ.get('E3DC_BOOTSTRAP_RUNNER_ROOT', '')}",
        f"E3DC_BOOTSTRAP_USER={os.environ.get('E3DC_BOOTSTRAP_USER', '')}",
        f"E3DC_INSTALL_ROOT={os.environ.get('E3DC_INSTALL_ROOT', '')}",
        "PYTHONNOUSERSITE=1",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONUNBUFFERED=1",
        "LC_ALL=C.UTF-8",
        "LANG=C.UTF-8",
    )
    try:
        actual_environment = tuple(shlex.split(values.get("Environment", "")))
    except ValueError:
        actual_environment = ()
    if actual_environment != expected_environment:
        mismatches.append("Environment")
    try:
        own_cgroups = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Eigene Finalizer-cgroup ist nicht lesbar") from exc
    if not any(line.partition("::")[2] == control_group for line in own_cgroups):
        mismatches.append("proc-cgroup")
    runtime_path = Path("/run") / contract.runtime_directory
    try:
        runtime_metadata = runtime_path.lstat()
    except FileNotFoundError:
        mismatches.append("RuntimeDirectory-fehlt")
    else:
        if (
            runtime_path.is_symlink()
            or not stat.S_ISDIR(runtime_metadata.st_mode)
            or runtime_metadata.st_uid != 0
            or runtime_metadata.st_gid != 0
            or stat.S_IMODE(runtime_metadata.st_mode) != 0o700
        ):
            mismatches.append("RuntimeDirectory-Metadaten")
    if mismatches:
        raise RuntimeError(
            "Transiente Finalizer-Servicebindung driftete: " + ", ".join(mismatches)
        )
    return values


def _verify_watchdog_pause_fresh(reason: str) -> None:
    descriptor, before = _open_regular_file_nofollow(WATCHDOG_PAUSE_FILE)
    try:
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o664
            or before.st_size < 2
            or before.st_size > 4096
        ):
            raise RuntimeError("Watchdog-Pause besitzt unsichere Metadaten")
        payload = os.read(descriptor, 4097)
        after = os.fstat(descriptor)
        named_after = os.stat(WATCHDOG_PAUSE_FILE, follow_symlinks=False)
        if (
            _file_identity(before) != _file_identity(after)
            or _file_identity(before) != _file_identity(named_after)
            or len(payload) > 4096
            or _repo_descriptor_has_unsafe_xattrs(descriptor)
        ):
            raise RuntimeError("Watchdog-Pause driftete beim Lesen")
    finally:
        os.close(descriptor)
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Watchdog-Pause ist nicht JSON-gebunden") from exc
    now = time.time()
    if (
        not isinstance(record, dict)
        or record.get("active") is not True
        or record.get("reason") != reason
        or int(record.get("pid", -1)) != os.getpid()
        or abs(now - float(record.get("ts", 0))) > 30
        or now - (before.st_mtime_ns / 1_000_000_000) > 30
        or (before.st_mtime_ns / 1_000_000_000) - now > 5
    ):
        raise RuntimeError("Watchdog-Pause ist nicht frisch und prozessgebunden")


def _create_update_safety_start_token(
    contract: UpdateSafetyContract,
    *,
    repo_dir: str,
) -> None:
    """Öffnet den Startpfad erst im gebundenen laufenden Finalizer-Service."""

    _validate_update_safety_contract(contract, expected_state="pending")
    _verify_update_safety_marker(contract, expected_present=True)
    _reload_and_verify_update_safety_dropins(contract, expected_present=True)
    _assert_managed_finalizer_service(contract)
    _assert_strict_update_writer_quiescence(
        repo_dir=repo_dir,
        transaction_id=contract.transaction_id,
    )
    runtime_path = Path("/run") / contract.runtime_directory
    runtime_descriptor = _open_directory_nofollow(runtime_path)
    try:
        _require_root_controlled_directory(
            runtime_descriptor,
            str(runtime_path),
            0o700,
        )
        payload = f"E3DC_UPDATE_START_LEASE_V1:{contract.transaction_id}\n".encode("ascii")
        _create_owned_exact_root_file_at(
            runtime_descriptor,
            UPDATE_FINALIZER_TOKEN_NAME,
            payload,
            0o600,
        )
        os.fsync(runtime_descriptor)
        _read_exact_root_file_at(
            runtime_descriptor,
            UPDATE_FINALIZER_TOKEN_NAME,
            payload,
            0o600,
        )
    finally:
        os.close(runtime_descriptor)


def _require_exact_pending_update_safety_for_recovery(
    contract: UpdateSafetyContract,
) -> UpdateSafetyContract:
    """Erlaubt Recovery nur mit demselben vollständigen Pending-Inodevertrag."""

    try:
        current = _validate_update_safety_contract(
            contract,
            expected_state="pending",
        )
        if current != contract:
            raise RuntimeError("Pending Update-Sicherheitsreceipt wurde ersetzt")
        _verify_update_safety_marker(contract, expected_present=True)
        _reload_and_verify_update_safety_dropins(contract, expected_present=True)
    except BaseException as exc:
        if isinstance(exc, UpdateSafetyManagedServiceUnquiescedError):
            raise
        raise UpdateSafetyManagedServiceUnquiescedError(
            "Ursprünglicher Pending-Receipt-/Marker-/00-Inodevertrag ist vor "
            "einer Recoverymutation nicht mehr exakt beweisbar"
        ) from exc
    return contract


def _read_managed_finalizer_terminal_snapshot(
    contract: UpdateSafetyContract,
) -> tuple[str, ...]:
    """Bindet einen einzelnen vollständigen Lease-/cgroup-/Prozess-Endzustand."""

    names = (
        "Id",
        "LoadState",
        "ActiveState",
        "SubState",
        "MainPID",
        "ControlGroup",
        "FragmentPath",
        "DropInPaths",
        "Transient",
        "RuntimeDirectory",
    )
    result = _run_argv(
        [
            "systemctl",
            "show",
            "--no-pager",
            *(f"--property={name}" for name in names),
            contract.finalizer_unit,
        ],
        timeout=10,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )
    values = {}
    for line in str(result.get("stdout") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator != "=" or key not in names or key in values:
            values = {}
            break
        values[key] = value
    if (
        not result.get("success")
        or result.get("timed_out")
        or int(result.get("returncode", -1)) != 0
        or str(result.get("stderr") or "")
        or set(values) != set(names)
        or values.get("Id") != contract.finalizer_unit
        or values.get("ActiveState") != "inactive"
        or values.get("SubState") != "dead"
        or values.get("MainPID") != "0"
        or values.get("DropInPaths") != ""
    ):
        raise RuntimeError("Transiente Finalizer-Unit ist nicht vollständig terminal")

    load_state = values.get("LoadState")
    fragment = values.get("FragmentPath", "")
    control_group = values.get("ControlGroup", "")
    if load_state == "not-found":
        if (
            fragment
            or control_group
            or values.get("Transient") != "no"
            or values.get("RuntimeDirectory")
        ):
            raise RuntimeError("Entladene Finalizer-Unit besitzt Residuen")
    elif load_state == "loaded":
        expected_fragment = f"/run/systemd/transient/{contract.finalizer_unit}"
        if (
            fragment != expected_fragment
            or values.get("Transient") != "yes"
            or values.get("RuntimeDirectory") != contract.runtime_directory
            or (
                control_group
                and (
                    not control_group.startswith("/")
                    or not control_group.endswith("/" + contract.finalizer_unit)
                )
            )
        ):
            raise RuntimeError("Geladene terminale Finalizer-Unit driftete")
        fragment_metadata = os.lstat(expected_fragment)
        if (
            not stat.S_ISREG(fragment_metadata.st_mode)
            or fragment_metadata.st_nlink != 1
            or fragment_metadata.st_uid != 0
            or fragment_metadata.st_gid != 0
            or fragment_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("Transiente terminale Finalizer-Unitdatei ist unsicher")
    else:
        raise RuntimeError(f"Finalizer-Unit besitzt LoadState {load_state!r}")

    if control_group:
        cgroup_path = Path("/sys/fs/cgroup") / control_group.lstrip("/")
        try:
            processes = (cgroup_path / "cgroup.procs").read_text(encoding="ascii")
        except FileNotFoundError:
            processes = ""
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("Finalizer-cgroup ist nicht eindeutig lesbar") from exc
        if processes.strip():
            raise RuntimeError("Finalizer-cgroup besitzt noch Prozesse")
    if os.path.lexists(f"/run/{contract.runtime_directory}") or os.path.lexists(
        contract.token_path
    ):
        raise RuntimeError("Finalizer-RuntimeDirectory oder Starttoken blieb vorhanden")
    # Anders als das normale Concurrent-Gate erlaubt dieser Endbeweis auch
    # keinen Prozess der eigenen txid außerhalb oder innerhalb der alten cgroup.
    _assert_no_same_transaction_finalizer_processes(contract)
    return tuple(values[name] for name in names)


def _wait_managed_finalizer_inactive(
    contract: UpdateSafetyContract,
    *,
    timeout_s: int = 30,
    repo_dir: str | None = None,
    require_pending_contract: bool = False,
) -> None:
    """Verlangt mehrfach denselben vollständigen terminalen Endzustand."""

    if require_pending_contract and not repo_dir:
        raise RuntimeError("Pending Finalizer-Endgate benötigt den Produktpfad")
    deadline = time.monotonic() + max(1, int(timeout_s))
    stable_signature = None
    stable_reads = 0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if require_pending_contract:
                _require_exact_pending_update_safety_for_recovery(contract)
            if repo_dir is not None:
                _assert_strict_update_writer_quiescence(
                    repo_dir=repo_dir,
                    transaction_id=contract.transaction_id,
                )
            signature = _read_managed_finalizer_terminal_snapshot(contract)
        except Exception as exc:
            last_error = exc
            stable_signature = None
            stable_reads = 0
        else:
            if signature == stable_signature:
                stable_reads += 1
            else:
                stable_signature = signature
                stable_reads = 1
            if stable_reads >= UPDATE_FINALIZER_TERMINAL_STABLE_READS:
                return
        time.sleep(UPDATE_FINALIZER_TERMINAL_STABLE_INTERVAL_S)
    detail = f": {last_error}" if last_error is not None else ""
    raise RuntimeError(
        "Transiente Finalizer-Unit erreichte keinen mehrfach stabilen, "
        f"prozessfreien Endzustand{detail}"
    )


def _kill_managed_finalizer_and_quiesce(
    contract: UpdateSafetyContract,
    *,
    repo_dir: str,
    require_pending_contract: bool = True,
) -> None:
    """Beendet die ganze cgroup und beweist danach erneut vollständige Writer-Ruhe."""

    action_failures = []
    for label, argv in (
        (
            "kill",
            [
                "systemctl",
                "kill",
                "--kill-whom=all",
                "--signal=SIGKILL",
                contract.finalizer_unit,
            ],
        ),
        ("stop", ["systemctl", "stop", contract.finalizer_unit]),
    ):
        result = _run_argv(
            argv,
            timeout=15,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        if (
            not isinstance(result, dict)
            or not result.get("success")
            or result.get("timed_out")
            or int(result.get("returncode", -1)) != 0
            or str(result.get("stderr") or "")
        ):
            action_failures.append(
                f"{label}={_combined_process_diagnostics(result or {}, maximum=600)}"
            )
    if not _stop_v4_services(V4_SERVICES):
        raise RuntimeError("Writer konnten nach Finalizer-Tod nicht gestoppt werden")
    _wait_managed_finalizer_inactive(
        contract,
        repo_dir=repo_dir,
        require_pending_contract=require_pending_contract,
    )
    if action_failures:
        update_logger.warning(
            "Managed Finalizer kill/stop meldeten Fehler; der vollständige "
            "mehrfache Endbeweis kompensierte sie: %s",
            "; ".join(action_failures),
        )
    _assert_no_same_transaction_finalizer_processes(contract)
    _assert_strict_update_writer_quiescence(
        repo_dir=repo_dir,
        transaction_id=contract.transaction_id,
    )
    if require_pending_contract:
        _require_exact_pending_update_safety_for_recovery(contract)
    # Der letzte Schritt ist erneut der vollständige Mehrfachnachweis; nach
    # ihm folgt vor der Fehlerklassifikation kein systemd-Mutator mehr.
    _wait_managed_finalizer_inactive(
        contract,
        repo_dir=repo_dir,
        require_pending_contract=require_pending_contract,
    )


def _post_update_healthcheck(
    services=None,
    transition_state: TransitionState | None = None,
    *,
    legacy_recovery: bool = False,
    projected_piguard: bool = False,
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
    if (
        projected_piguard or "piguard.service" in state.preinstalled_units
    ) and "piguard" not in health_services:
        health_services.append("piguard")

    for srv in health_services:
        if not srv or srv == 'e3dc':
            continue
        try:
            expected, reason = _release_service_expected(
                srv,
                state,
                projected_piguard=projected_piguard,
            )
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


def _verify_prepared_service_quiesced(service: str) -> None:
    """Beweist nach Enable/Reset-failed den noch nicht gestarteten Unit-Zustand."""

    unit = _unit_name(service)
    property_names = ("LoadState", "ActiveState", "SubState", "MainPID")
    result = _run_argv(
        [
            "systemctl",
            "show",
            "--no-pager",
            *(f"--property={name}" for name in property_names),
            unit,
        ],
        timeout=10,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )
    values: dict[str, str] = {}
    for raw_line in str(result.get("stdout") or "").splitlines():
        key, separator, value = raw_line.partition("=")
        if (
            separator != "="
            or key not in property_names
            or key in values
            or value != value.strip()
        ):
            values = {}
            break
        values[key] = value
    if (
        not result.get("success")
        or result.get("timed_out")
        or int(result.get("returncode", -1)) != 0
        or str(result.get("stderr") or "")
        or values
        != {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "SubState": "dead",
            "MainPID": "0",
        }
    ):
        raise RuntimeError(
            f"{unit} ist vor dem Lease-Token nicht loaded/inactive/dead/PID0"
        )


LEGACY_E3DC_ADMIN_UNIT = "/etc/systemd/system/e3dc.service"


def _normalize_legacy_e3dc_service() -> None:
    """Projiziert den bekannten C++-Altdienst auf einen kanonischen Auszustand."""

    # Der Altzustand ist im verifizierten Backup enthalten. Ab hier zählt der
    # kanonische Releasezustand: gestoppt, disabled und persistent maskiert.
    # Das gilt auch für generierte/transiente Altunits, die keinen der vier
    # klassischen Unit-Dateipfade besitzen.
    _run_argv(["sudo", "systemctl", "stop", "e3dc.service"], timeout=30)
    _run_argv(["sudo", "systemctl", "unmask", "e3dc.service"], timeout=30)
    _run_argv(["sudo", "systemctl", "disable", "e3dc.service"], timeout=30)

    if os.path.lexists(LEGACY_E3DC_ADMIN_UNIT):
        metadata = os.lstat(LEGACY_E3DC_ADMIN_UNIT)
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise RuntimeError(
                "e3dc.service besitzt am kanonischen Unitpfad einen "
                "nicht normalisierbaren Dateityp"
            )
        removed = _run_argv(
            ["sudo", "rm", "-f", "--", LEGACY_E3DC_ADMIN_UNIT],
            timeout=15,
        )
        if not removed.get("success") or os.path.lexists(LEGACY_E3DC_ADMIN_UNIT):
            raise RuntimeError("Alte e3dc.service-Unit konnte nicht ersetzt werden")

    masked = _run_argv(
        ["sudo", "systemctl", "mask", "--force", "e3dc.service"],
        timeout=30,
    )
    reloaded = _run_argv(["sudo", "systemctl", "daemon-reload"], timeout=30)
    _run_argv(
        ["sudo", "systemctl", "reset-failed", "e3dc.service"],
        timeout=30,
    )
    if not masked.get("success") or not reloaded.get("success"):
        raise RuntimeError("e3dc.service konnte nicht kanonisch maskiert werden")

    try:
        metadata = os.lstat(LEGACY_E3DC_ADMIN_UNIT)
        target = os.readlink(LEGACY_E3DC_ADMIN_UNIT)
    except OSError as exc:
        raise RuntimeError("Persistente e3dc.service-Maske fehlt") from exc
    if not stat.S_ISLNK(metadata.st_mode) or target != "/dev/null":
        raise RuntimeError("Persistente e3dc.service-Maske ist nicht kanonisch")

    show = _run_argv(
        [
            "systemctl",
            "show",
            "e3dc.service",
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--property=UnitFileState",
        ],
        timeout=10,
    )
    values: dict[str, str] = {}
    for line in str(show.get("stdout") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    if (
        not show.get("success")
        or values.get("LoadState") != "masked"
        or values.get("ActiveState") != "inactive"
        or values.get("SubState") != "dead"
        or values.get("MainPID") != "0"
        or values.get("UnitFileState") != "masked"
    ):
        raise RuntimeError("e3dc.service ist nach der Zielprojektion nicht sicher aus")


def _prepare_v4_service_activation(
    *,
    services,
    transition_state: TransitionState,
    projected_piguard: bool = False,
) -> bool:
    """Erledigt persistente Startvorbereitung vollständig vor dem Lease-Token."""

    try:
        state = transition_state
        _verify_transition_state(state)
        from .ha_writer_admission import instance_role_anchor_matches

        if instance_role_anchor_matches(
            state.ha_role,
            peer_ip=str(state.config.get("ha_peer_ip") or ""),
        ) is not True:
            raise RuntimeError("Vorhandener Instanzrollen-Anker widerspricht dem Update")
        if ensure_manager_lock_namespace() is not True:
            raise RuntimeError(
                "Root-kontrollierter Manager-Locknamespace ist nicht herstellbar"
            )
    except Exception as exc:
        print(f"  [!] Persistente Startvorbereitung ist nicht beweisbar: {exc}")
        return False

    install_user = get_install_user()
    errors = []
    try:
        _normalize_legacy_e3dc_service()
    except Exception as exc:
        errors.append(str(exc))
    for screen_user in tuple(dict.fromkeys((str(install_user), "root"))):
        for screen_name in ("e3dc", "E3DC"):
            prefix = ["sudo", "-u", screen_user] if screen_user != "root" else ["sudo"]
            _run_argv([*prefix, "screen", "-S", screen_name, "-X", "quit"], timeout=10)
    _run_argv(["sudo", "pkill", "-x", "E3DC-Control"], timeout=10)
    _run_argv(["sudo", "pkill", "-f", r"(^|/)E3DC\.sh([[:space:]]|$)"], timeout=10)

    standby = _ha_slave_standby_services(state)
    start_services = _normalize_restart_services(services)
    if (
        projected_piguard or "piguard.service" in state.preinstalled_units
    ) and "piguard" not in start_services:
        start_services.append("piguard")
    for service in start_services:
        if not service or service == "e3dc":
            continue
        try:
            expected, reason = _release_service_expected(
                service,
                state,
                projected_piguard=projected_piguard,
            )
        except Exception as exc:
            errors.append(str(exc))
            continue
        if not expected or service in standby:
            if _service_unit_exists(service):
                stopped = _run_argv(
                    ["sudo", "systemctl", "stop", _unit_name(service)],
                    timeout=30,
                )
                inactive = _run_argv(
                    ["systemctl", "is-active", _unit_name(service)],
                    timeout=10,
                )
                if (
                    not stopped.get("success")
                    or not _systemd_activity_readback_matches(
                        inactive,
                        should_be_active=False,
                    )
                ):
                    errors.append(
                        f"{service} blieb in {reason} nicht beweisbar gestoppt"
                    )
            continue
        if not _service_unit_exists(service):
            errors.append(f"{_unit_name(service)} fehlt, obwohl erwartet ({reason})")
            continue
        reset_failed = _run_argv(
            ["sudo", "systemctl", "reset-failed", _unit_name(service)],
            timeout=30,
        )
        if (
            not reset_failed.get("success")
            or reset_failed.get("timed_out")
            or int(reset_failed.get("returncode", -1)) != 0
            or str(reset_failed.get("stderr") or "")
        ):
            errors.append(f"{service} konnte vor dem Startgate nicht reset-failed werden")
            continue
        enabled = _run_argv(
            ["sudo", "systemctl", "enable", _unit_name(service)],
            timeout=30,
        )
        readback = _run_argv(
            ["systemctl", "is-enabled", _unit_name(service)],
            timeout=10,
        )
        if (
            not enabled.get("success")
            or enabled.get("timed_out")
            or _systemd_state_from_result(
                readback,
                SYSTEMD_KNOWN_UNIT_FILE_STATES,
            )
            != "enabled"
        ):
            errors.append(f"{service} ist vor dem Start nicht rebootfest aktiviert")
            continue
        try:
            _verify_prepared_service_quiesced(service)
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        for error in errors:
            print(f"  [!] {error}")
        return False
    return True


def _restart_v4_services(
    headless: bool = False,
    services=None,
    transition_state: TransitionState | None = None,
    *,
    legacy_recovery: bool = False,
    prepared_start_only: bool = False,
    projected_piguard: bool = False,
) -> bool:
    """Startet die installierten E3DC-Control-Dienste neu."""
    try:
        state = transition_state or _capture_transition_state()
        _verify_transition_state(state, expect_legacy_config_missing=legacy_recovery)
    except Exception as exc:
        print(f"  [!] HA-/Shadow-Zustand nicht beweisbar: {exc}")
        return False
    try:
        from .ha_writer_admission import instance_role_anchor_matches

        # Ein reguläres Update darf eine fehlende privilegierte Anlagenrolle
        # niemals rückwärts aus der web-schreibbaren Laufzeitkonfiguration
        # autorisieren. Der Anker entsteht nur bei der Erstinstallation oder
        # einem ausdrücklich gebundenen Rollenwechsel.
        if instance_role_anchor_matches(
            state.ha_role,
            peer_ip=str(state.config.get("ha_peer_ip") or ""),
        ) is not True:
            raise RuntimeError("Vorhandener Instanzrollen-Anker widerspricht dem Update")
    except Exception as exc:
        print(f"  [!] Root-kontrollierte Instanzrolle ist nicht bestätigbar: {exc}")
        return False
    if not prepared_start_only and ensure_manager_lock_namespace() is not True:
        print("  [!] Root-kontrollierter Manager-Locknamespace ist nicht herstellbar.")
        return False
    if not prepared_start_only:
        print('\n[->] Bereinige alte C++ E3DC-Dienste/Screens (falls vorhanden)...')
        install_user = get_install_user()
        legacy_unit_present = any(os.path.lexists(path) for path in (
            '/etc/systemd/system/e3dc.service',
            '/lib/systemd/system/e3dc.service',
            '/usr/lib/systemd/system/e3dc.service',
            '/run/systemd/system/e3dc.service',
        ))
        if legacy_unit_present and not legacy_recovery:
            try:
                _normalize_legacy_e3dc_service()
            except Exception as exc:
                print(f'  [!] Legacy e3dc.service konnte nicht normalisiert werden: {exc}')
                return False
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
    if (
        projected_piguard or "piguard.service" in state.preinstalled_units
    ) and "piguard" not in start_services:
        start_services.append("piguard")
    errors = []
    for srv in start_services:
        if not srv or srv == 'e3dc':
            if srv == 'e3dc':
                print('  [SKIP] e3dc ist Legacy C++ und wird im Update nicht gestartet.')
            continue
        try:
            expected, reason = _release_service_expected(
                srv,
                state,
                projected_piguard=projected_piguard,
            )
        except Exception as exc:
            errors.append(str(exc))
            continue
        if not expected or srv in ha_slave_services:
            if _service_unit_exists(srv):
                if prepared_start_only:
                    try:
                        _verify_prepared_service_quiesced(srv)
                    except Exception as exc:
                        errors.append(str(exc))
                else:
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
            if prepared_start_only:
                enable = {
                    "success": True,
                    "stdout": "vorbereitet",
                    "stderr": "",
                    "returncode": 0,
                }
            else:
                run_command(f'sudo systemctl reset-failed {srv} 2>/dev/null || true', timeout=10)
                enable = run_command(f'sudo systemctl enable {srv}', timeout=15)
            start_action = "start" if prepared_start_only else "restart"
            res = run_command(
                f'sudo systemctl {start_action} {srv}',
                timeout=15,
            )
            enabled_probe = run_command(f'systemctl is-enabled {srv}', timeout=10)
            enabled_state = _systemd_state_from_result(
                enabled_probe,
                SYSTEMD_KNOWN_UNIT_FILE_STATES,
            )
            active_probe = run_command(f'systemctl is-active {srv}', timeout=10)
            active_state = _systemd_state_from_result(
                active_probe,
                {"active", "inactive", "failed", "activating", "deactivating", "reloading"},
            )
            settled_after_timeout = False
            settle_state = ("", "", "")
            settle_result = {
                "success": False,
                "stdout": "",
                "stderr": "",
                "returncode": -1,
            }
            command_results = (
                ("enable", enable),
                (start_action, res),
                ("is-enabled", enabled_probe),
                ("is-active", active_probe),
            )
            timed_out_commands = [
                name
                for name, result in command_results
                if _command_timed_out(result)
            ]
            hard_command_failures = [
                name
                for name, result in command_results
                if not result.get("success") and not _command_timed_out(result)
            ]
            needs_settle_window = (
                bool(timed_out_commands)
                or active_state in {"activating", "deactivating", "reloading"}
            )
            if needs_settle_window:
                print(
                    f"  [i] {srv}: langsamer systemd-Übergang; "
                    f"warte höchstens {SYSTEMD_SETTLE_TIMEOUT_S} Sekunden auf den Endzustand."
                )
                settled_after_timeout, settle_state, settle_result = _wait_for_systemd_end_state(srv)
                if settled_after_timeout:
                    _load_state, enabled_state, active_state = settle_state
            # Release-Dienste müssen nach einem Update rebootfest aktiviert
            # sein. Runtime-, static-, linked- oder generated-Zustände reichen
            # trotz aktuell aktivem Prozess nicht als Persistenzbeweis.
            enabled_ok = enabled_state == "enabled"
            active_ok = active_state == "active"
            commands_ok = not hard_command_failures and (
                not timed_out_commands or settled_after_timeout
            )
            end_state_ok = enabled_ok and active_ok
            service_ok = commands_ok and end_state_ok
            print(f"  {'[OK]' if service_ok else '[!] FEHLER'} {srv}")
            command_notes = []
            for name, result in command_results:
                if not result.get("success"):
                    command_notes.append(
                        f"{name} " + _command_result_diagnostic(result)
                    )
            if command_notes:
                print(
                    f"  [HINWEIS] {srv}: " + "; ".join(command_notes)
                    + f"; Endzustand={enabled_state or 'unlesbar'}/{active_state or 'unlesbar'}"
                )
            if not service_ok:
                settle_detail = (
                    "; systemd-Endzustand "
                    f"{settle_state[0] or 'unlesbar'}/"
                    f"{settle_state[1] or 'unlesbar'}/"
                    f"{settle_state[2] or 'unlesbar'}; "
                    f"show {_command_result_diagnostic(settle_result)}"
                    if needs_settle_window
                    else ""
                )
                errors.append(
                    f"{srv} besitzt keinen sicheren Start-Endzustand "
                    f"({enabled_state or 'unlesbar'}/{active_state or 'unlesbar'}); "
                    f"enable {_command_result_diagnostic(enable)}; "
                    f"{start_action} {_command_result_diagnostic(res)}; "
                    f"is-enabled {_command_result_diagnostic(enabled_probe)}; "
                    f"is-active {_command_result_diagnostic(active_probe)}"
                    f"{settle_detail}"
                )
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
                    else:
                        # Der gesamte Baum ist hier bereits das ausdrücklich zum
                        # Verwerfen gebundene Werkzeugverzeichnis (zum Beispiel
                        # alte .git-Metadaten). FIFO-/Socket-Reste darin sind
                        # keine Produktdateien und dürfen den Neubau nicht
                        # verhindern; unlink folgt ihnen nicht.
                        os.unlink(entry, dir_fd=descriptor)
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


def _open_root_managed_parent(path: str) -> int:
    """Öffnet die vollständige root-kontrollierte Elternkette ohne Symlinks."""

    target = Path(str(path))
    normalized = os.path.normpath(str(target))
    if (
        not target.is_absolute()
        or str(target) != normalized
        or target.name in {"", ".", ".."}
    ):
        raise RuntimeError("Root-Recovery-Pfad ist nicht absolut und kanonisch")

    current = Path(target.anchor)
    for component in target.parent.parts[1:]:
        current /= component
        metadata = os.lstat(current)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError(
                f"Root-Recovery-Elternpfad ist nicht root-kontrolliert: {current}"
            )

    descriptor = _open_directory_nofollow(target.parent)
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        os.close(descriptor)
        raise RuntimeError("Root-Recovery-Elternverzeichnis ist nicht vertrauenswürdig")
    return descriptor


def _capture_bound_managed_file_preimage(
    path: str,
    *,
    expected_mode: int,
    maximum_bytes: int,
    expected_uid: int = 0,
    expected_gid: int = 0,
) -> RootManagedFilePreimage:
    """Bindet Existenz, Bytes, Owner und Modus einer festen Systemdatei."""

    parent_descriptor = _open_root_managed_parent(path)
    parent_metadata = os.fstat(parent_descriptor)
    name = os.path.basename(path)
    try:
        try:
            before = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return RootManagedFilePreimage(
                path=path,
                existed=False,
                payload=b"",
                uid=-1,
                gid=-1,
                mode=0,
                parent_dev=int(parent_metadata.st_dev),
                parent_ino=int(parent_metadata.st_ino),
            )

        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != expected_uid
            or before.st_gid != expected_gid
            or stat.S_IMODE(before.st_mode) != expected_mode
            or before.st_size < 0
            or before.st_size > maximum_bytes
        ):
            raise RuntimeError(
                f"Root-Recovery-Datei besitzt kein vertrauenswürdiges Preimage: {path}"
            )

        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
        try:
            opened = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(before):
                raise RuntimeError(
                    f"Root-Recovery-Datei driftete beim Öffnen: {path}"
                )
            chunks = []
            remaining = maximum_bytes + 1
            while remaining > 0:
                block = os.read(descriptor, min(64 * 1024, remaining))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            payload = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                len(payload) != before.st_size
                or _file_identity(after) != _file_identity(before)
            ):
                raise RuntimeError(
                    f"Root-Recovery-Datei driftete während der Erfassung: {path}"
                )
        finally:
            os.close(descriptor)

        path_after = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _file_identity(path_after) != _file_identity(before):
            raise RuntimeError(
                f"Root-Recovery-Datei wurde nach der Erfassung ausgetauscht: {path}"
            )
        return RootManagedFilePreimage(
            path=path,
            existed=True,
            payload=payload,
            uid=int(before.st_uid),
            gid=int(before.st_gid),
            mode=stat.S_IMODE(before.st_mode),
            parent_dev=int(parent_metadata.st_dev),
            parent_ino=int(parent_metadata.st_ino),
        )
    finally:
        os.close(parent_descriptor)


def _restore_bound_managed_file_preimage(
    preimage: RootManagedFilePreimage,
    *,
    expected_mode: int,
    maximum_bytes: int,
    expected_uid: int = 0,
    expected_gid: int = 0,
) -> None:
    """Stellt ein gebundenes Systemdatei-Preimage atomar und bytegenau wieder her."""

    existing_invalid = (
        preimage.existed
        and (
            preimage.uid != expected_uid
            or preimage.gid != expected_gid
            or preimage.mode != expected_mode
        )
    )
    missing_invalid = (
        not preimage.existed
        and (
            preimage.payload != b""
            or preimage.uid != -1
            or preimage.gid != -1
            or preimage.mode != 0
        )
    )
    if existing_invalid or missing_invalid or len(preimage.payload) > maximum_bytes:
        raise RuntimeError(
            f"Root-Recovery-Preimage ist nicht vertrauenswürdig: {preimage.path}"
        )

    parent_descriptor = _open_root_managed_parent(preimage.path)
    parent_metadata = os.fstat(parent_descriptor)
    if (
        int(parent_metadata.st_dev) != preimage.parent_dev
        or int(parent_metadata.st_ino) != preimage.parent_ino
    ):
        os.close(parent_descriptor)
        raise RuntimeError(
            f"Root-Recovery-Elternverzeichnis driftete: {preimage.path}"
        )

    name = os.path.basename(preimage.path)
    temporary_path = ""
    try:
        if preimage.existed:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{name}.e3dc-recovery-",
                dir=os.path.dirname(preimage.path),
            )
            try:
                offset = 0
                while offset < len(preimage.payload):
                    offset += os.write(descriptor, preimage.payload[offset:])
                os.fchown(descriptor, preimage.uid, preimage.gid)
                os.fchmod(descriptor, preimage.mode)
                os.fsync(descriptor)
                written = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(written.st_mode)
                    or written.st_nlink != 1
                    or written.st_uid != preimage.uid
                    or written.st_gid != preimage.gid
                    or stat.S_IMODE(written.st_mode) != preimage.mode
                    or written.st_size != len(preimage.payload)
                ):
                    raise RuntimeError(
                        f"Root-Recovery-Tempdatei ist nicht exakt: {preimage.path}"
                    )
            finally:
                os.close(descriptor)

            os.replace(
                os.path.basename(temporary_path),
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            temporary_path = ""
            os.fsync(parent_descriptor)
        else:
            try:
                current = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                current = None
            if current is not None:
                if stat.S_ISDIR(current.st_mode):
                    raise RuntimeError(
                        f"Neu angelegter Root-Recovery-Pfad ist ein Verzeichnis: {preimage.path}"
                    )
                os.unlink(name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
        if temporary_path and os.path.lexists(temporary_path):
            os.unlink(temporary_path)

    restored = _capture_bound_managed_file_preimage(
        preimage.path,
        expected_mode=expected_mode,
        maximum_bytes=maximum_bytes,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    if restored != preimage:
        raise RuntimeError(
            f"Root-Recovery-Endgate weicht vom gebundenen Preimage ab: {preimage.path}"
        )


def _capture_root_managed_preimages() -> tuple[RootManagedFilePreimage, ...]:
    return tuple(
        _capture_bound_managed_file_preimage(
            path,
            expected_mode=mode,
            maximum_bytes=maximum,
        )
        for path, mode, maximum in ROOT_RECOVERY_FILE_CONTRACTS
    )


def _restore_root_managed_preimages(
    preimages: tuple[RootManagedFilePreimage, ...],
) -> None:
    expected = {
        path: (mode, maximum)
        for path, mode, maximum in ROOT_RECOVERY_FILE_CONTRACTS
    }
    actual = {preimage.path: preimage for preimage in preimages}
    if len(actual) != len(preimages) or set(actual) != set(expected):
        raise RuntimeError("Root-Recovery-Preimages sind unvollständig oder doppelt")

    errors = []
    for path, mode, maximum in ROOT_RECOVERY_FILE_CONTRACTS:
        try:
            _restore_bound_managed_file_preimage(
                actual[path],
                expected_mode=mode,
                maximum_bytes=maximum,
            )
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        raise RuntimeError(
            "Root-Recovery konnte nicht vollständig verifiziert werden: "
            + "; ".join(errors)
        )


def _validate_logrotate_config(path: str) -> None:
    """Parst eine gebundene Logrotate-Datei mit dem echten Systemparser."""

    binary = "/usr/sbin/logrotate"
    metadata = os.lstat(binary)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not metadata.st_mode & 0o111
    ):
        raise RuntimeError("Logrotate-Systemparser ist nicht vertrauenswürdig")
    result = _run_argv(
        [binary, "-d", "-s", "/dev/null", path],
        timeout=30,
    )
    if not result["success"]:
        detail = (result.get("stderr") or result.get("stdout") or "").strip()
        raise RuntimeError("Logrotate-Konfiguration ist ungültig: " + detail[-1000:])


def _project_bare_metal_logrotate_config(
    *,
    repo_dir: str,
    target_commit: str,
    install_user: str,
) -> None:
    """Projiziert die LF-Konfiguration am letzten Release-Gate atomar nach /etc."""

    payload = _read_commit_blob(
        repo_dir,
        target_commit,
        LOGROTATE_SOURCE_RELATIVE_PATH,
        install_user,
    )
    mode = _read_commit_file_mode(
        repo_dir,
        target_commit,
        LOGROTATE_SOURCE_RELATIVE_PATH,
        install_user,
    )
    if (
        mode not in {0o644, 0o755}
        or not payload
        or len(payload) > 64 * 1024
        or b"\r" in payload
        or not payload.endswith(b"\n")
    ):
        raise RuntimeError("Logrotate-Release-Blob besitzt keinen reinen LF-Vertrag")
    try:
        payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Logrotate-Release-Blob ist nicht UTF-8") from exc

    preimage = _capture_bound_managed_file_preimage(
        LOGROTATE_CONFIG_PATH,
        expected_mode=0o644,
        maximum_bytes=64 * 1024,
    )
    parent_descriptor = _open_root_managed_parent(LOGROTATE_CONFIG_PATH)
    parent = os.path.dirname(LOGROTATE_CONFIG_PATH)
    name = os.path.basename(LOGROTATE_CONFIG_PATH)
    staged_path = ""
    replaced = False
    try:
        descriptor, staged_path = tempfile.mkstemp(
            prefix=f".{name}.e3dc-release-",
            dir=parent,
        )
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fchown(descriptor, 0, 0)
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
            staged = os.fstat(descriptor)
            if (
                not stat.S_ISREG(staged.st_mode)
                or staged.st_nlink != 1
                or staged.st_uid != 0
                or staged.st_gid != 0
                or stat.S_IMODE(staged.st_mode) != 0o644
                or staged.st_size != len(payload)
            ):
                raise RuntimeError("Logrotate-Stagingdatei besitzt keine gebundenen Metadaten")
        finally:
            os.close(descriptor)

        _validate_logrotate_config(staged_path)
        os.replace(
            os.path.basename(staged_path),
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        staged_path = ""
        replaced = True
        os.fsync(parent_descriptor)
        _validate_logrotate_config(LOGROTATE_CONFIG_PATH)
        projected = _capture_bound_managed_file_preimage(
            LOGROTATE_CONFIG_PATH,
            expected_mode=0o644,
            maximum_bytes=64 * 1024,
        )
        if not projected.existed or projected.payload != payload:
            raise RuntimeError("Logrotate-Endgate stimmt nicht mit dem Release-Blob überein")
    except Exception:
        if replaced:
            _restore_bound_managed_file_preimage(
                preimage,
                expected_mode=0o644,
                maximum_bytes=64 * 1024,
            )
        raise
    finally:
        os.close(parent_descriptor)
        if staged_path and os.path.lexists(staged_path):
            os.unlink(staged_path)


def _capture_apache_security_preimage() -> ApacheSecurityPreimage:
    available_path = APACHE_SECURITY_CONF_AVAILABLE
    enabled_path = APACHE_SECURITY_CONF_ENABLED
    payload = b""
    uid = -1
    gid = -1
    mode = 0
    available = os.path.lexists(available_path)
    if available:
        metadata = os.lstat(available_path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or metadata.st_size > 64 * 1024
        ):
            raise RuntimeError(
                "Apache-Schutzkonfiguration besitzt kein vertrauenswürdiges Preimage"
            )
        descriptor = os.open(
            available_path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_size != metadata.st_size
            ):
                raise RuntimeError(
                    "Apache-Schutzkonfiguration driftete bei der Preimage-Erfassung"
                )
            payload = os.read(descriptor, metadata.st_size + 1)
        finally:
            os.close(descriptor)
        if len(payload) != metadata.st_size:
            raise RuntimeError(
                "Apache-Schutzkonfiguration konnte nicht vollständig gebunden werden"
            )
        uid = int(metadata.st_uid)
        gid = int(metadata.st_gid)
        mode = stat.S_IMODE(metadata.st_mode)

    enabled = os.path.lexists(enabled_path)
    enabled_target = ""
    if enabled:
        enabled_metadata = os.lstat(enabled_path)
        if (
            not stat.S_ISLNK(enabled_metadata.st_mode)
            or enabled_metadata.st_uid != 0
            or enabled_metadata.st_gid != 0
            or not available
        ):
            raise RuntimeError(
                "Aktivierung der Apache-Schutzkonfiguration besitzt kein "
                "vertrauenswürdiges Preimage"
            )
        enabled_target = os.readlink(enabled_path)
        if os.path.realpath(enabled_path) != os.path.realpath(available_path):
            raise RuntimeError(
                "Apache-Schutzaktivierung verweist nicht auf die gebundene Konfiguration"
            )

    load_state_result = _run_argv(
        [
            "systemctl",
            "show",
            "apache2.service",
            "--property=LoadState",
            "--value",
        ],
        timeout=15,
    )
    load_state = str(load_state_result.get("stdout") or "").strip().lower()
    if (
        not load_state_result.get("success")
        or load_state not in {"loaded", "not-found"}
    ):
        raise RuntimeError(
            "Apache-LoadState ist für das Recovery-Preimage nicht stabil"
        )
    apache_available = load_state == "loaded"
    apache_activity = "absent"
    if apache_available:
        apache_ctl = os.lstat("/usr/sbin/apache2ctl")
        if (
            not stat.S_ISREG(apache_ctl.st_mode)
            or apache_ctl.st_uid != 0
            or apache_ctl.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("apache2ctl besitzt kein vertrauenswürdiges Preimage")
        activity_result = _run_argv(
            ["systemctl", "is-active", "apache2.service"],
            timeout=15,
        )
        apache_activity = str(activity_result.get("stdout") or "").strip().lower()
        if not activity_result.get("success") or apache_activity != "active":
            raise RuntimeError(
                "Apache muss vor dem HTTP-basierten Release-Wechsel aktiv sein"
            )

    return ApacheSecurityPreimage(
        available=available,
        payload=payload,
        uid=uid,
        gid=gid,
        mode=mode,
        enabled=enabled,
        enabled_target=enabled_target,
        apache_available=apache_available,
        apache_activity=apache_activity,
    )


def _restore_apache_security_preimage(preimage: ApacheSecurityPreimage) -> None:
    remove_enabled = _run_argv(
        ["sudo", "rm", "-f", APACHE_SECURITY_CONF_ENABLED],
        timeout=15,
    )
    if not remove_enabled["success"]:
        raise RuntimeError("Apache-Schutzaktivierung konnte nicht zurückgesetzt werden")

    if preimage.available:
        if (
            preimage.uid != 0
            or preimage.gid != 0
            or preimage.mode & (stat.S_IWGRP | stat.S_IWOTH)
            or len(preimage.payload) > 64 * 1024
        ):
            raise RuntimeError("Apache-Recovery-Preimage ist nicht vertrauenswürdig")
        temporary_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                prefix="e3dc-apache-recovery-",
                delete=False,
            ) as temporary:
                temporary.write(preimage.payload)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = temporary.name
            restored = _run_argv(
                [
                    "sudo",
                    "install",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    f"{preimage.mode:04o}",
                    temporary_path,
                    APACHE_SECURITY_CONF_AVAILABLE,
                ],
                timeout=30,
            )
            if not restored["success"]:
                raise RuntimeError(
                    "Apache-Schutzkonfiguration konnte nicht wiederhergestellt werden"
                )
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)
    else:
        removed = _run_argv(
            ["sudo", "rm", "-f", APACHE_SECURITY_CONF_AVAILABLE],
            timeout=15,
        )
        if not removed["success"]:
            raise RuntimeError(
                "Neue Apache-Schutzkonfiguration konnte nicht entfernt werden"
            )

    if preimage.enabled:
        if (
            preimage.enabled_target == ""
            or "\x00" in preimage.enabled_target
            or os.path.realpath(
                os.path.join(
                    os.path.dirname(APACHE_SECURITY_CONF_ENABLED),
                    preimage.enabled_target,
                )
            )
            != os.path.realpath(APACHE_SECURITY_CONF_AVAILABLE)
        ):
            raise RuntimeError("Apache-Aktivierungs-Preimage ist ungültig")
        enabled = _run_argv(
            [
                "sudo",
                "ln",
                "-s",
                preimage.enabled_target,
                APACHE_SECURITY_CONF_ENABLED,
            ],
            timeout=15,
        )
        if not enabled["success"]:
            raise RuntimeError(
                "Apache-Schutzaktivierung konnte nicht wiederhergestellt werden"
            )

    if preimage.apache_available:
        configtest = _run_argv(
            ["sudo", "/usr/sbin/apache2ctl", "configtest"],
            timeout=30,
        )
        if not configtest["success"]:
            raise RuntimeError("Apache-Konfiguration ist nach Recovery ungültig")
        service_result = _run_argv(
            ["sudo", "systemctl", "reload", "apache2.service"],
            timeout=30,
        )
        if not service_result["success"]:
            raise RuntimeError(
                "Apache-Aktivitätszustand konnte nach Recovery nicht "
                "wiederhergestellt werden"
            )

    if _capture_apache_security_preimage() != preimage:
        raise RuntimeError("Apache-Recovery weicht vom gebundenen Preimage ab")


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
    return RecoverySurfaceInventory(
        web_program_entries=web_inventory,
        watchdog_files=watchdogs,
        unit_enablement=tuple(enablement),
        root_managed_files=_capture_root_managed_preimages(),
        apache_security=_capture_apache_security_preimage(),
    )


def _restore_recovery_surface(inventory: RecoverySurfaceInventory, state: TransitionState) -> None:
    # Die beiden privilegierten Web-Aktoren werden vom Ziel-Finalizer früh
    # mutiert. Ihr Rücklauf hat deshalb Vorrang vor allen nachfolgenden,
    # voneinander unabhängigen Recovery-Schritten.
    _restore_root_managed_preimages(inventory.root_managed_files)
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
    _restore_apache_security_preimage(inventory.apache_security)
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
    raw = _read_commit_blob(
        repo_dir,
        verified,
        path,
        install_user,
        maximum=1024 * 1024,
    )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{path} ist im verifizierten Ziel-Commit kein UTF-8") from exc


def _fetch_target_commit(repo_dir: str, install_user: str, target_tag: str | None) -> str:
    _require_bound_origin(repo_dir, install_user)
    if target_tag:
        storage_ref = f"refs/tags/{target_tag}"
        peeled_ref = storage_ref + "^{}"
        official = _official_remote_refs(storage_ref, peeled_ref)
        refspec = f"+{storage_ref}:{storage_ref}"
        result = _git_argv(
            repo_dir,
            install_user,
            "fetch",
            "--no-tags",
            SELFUPDATE_REPO,
            refspec,
            timeout=120,
        )
        if not result["success"]:
            raise RuntimeError("Release-Tag-Fetch fehlgeschlagen: " + result["stderr"].strip())
        object_type = _git_argv(repo_dir, install_user, "cat-file", "-t", storage_ref, timeout=15)
        if not object_type["success"] or object_type["stdout"].strip() != "tag":
            raise RuntimeError(f"Release-Tag {target_tag} ist nicht annotiert")
        tag_object = _git_argv(repo_dir, install_user, "rev-parse", "--verify", storage_ref + "^{tag}", timeout=15)
        if (
            not tag_object["success"]
            or not _exact_commit_matches(tag_object["stdout"].strip(), official[storage_ref])
        ):
            raise RuntimeError(f"Release-Tag {target_tag} weicht vom offiziellen Tagobjekt ab")
        commit = _resolve_git_commit(repo_dir, storage_ref, install_user)
        if not commit or not _exact_commit_matches(commit, official[peeled_ref]):
            raise RuntimeError(f"Release-Tag {target_tag} weicht vom offiziellen Zielcommit ab")
    else:
        official = _official_remote_refs("refs/heads/main")["refs/heads/main"]
        result = _git_argv(
            repo_dir,
            install_user,
            "fetch",
            "--no-tags",
            SELFUPDATE_REPO,
            "+refs/heads/main:refs/remotes/origin/main",
            timeout=120,
        )
        if not result["success"]:
            raise RuntimeError("git fetch origin/main fehlgeschlagen: " + result["stderr"].strip())
        commit = _resolve_git_commit(repo_dir, "refs/remotes/origin/main", install_user)
        if not commit or not _exact_commit_matches(commit, official):
            raise RuntimeError("Gefetchtes origin/main weicht vom offiziellen GitHub-Ref ab")
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
    expected_stable = _normalize_release_tag(version)
    if stable != expected_stable:
        raise RuntimeError(
            "Stable-Tag und VERSION des Ziel-Commits stimmen nicht überein"
        )
    if target_tag and stable != target_tag:
        raise RuntimeError(f"Ziel-Tag {target_tag} ist nicht Stable des Ziel-Commits ({stable})")
    if policy.get("run_permissions") is not True:
        raise RuntimeError(
            "Verifizierte Ziel-Policy muss die gebundene Berechtigungs- und "
            "Root-Launcher-Aktualisierung aktivieren"
        )
    # Auch der bereits explizit angeforderte Tag wird im Zielprozess erneut
    # direkt von origin geladen und als annotiertes Tag auf exakt denselben
    # Commit gebunden. Die Vorprüfung des Alt-Updaters ist kein Ersatz dafür.
    stable_commit = _fetch_target_commit(repo_dir, install_user, stable)
    if not stable_commit or not _exact_commit_matches(stable_commit, target_commit):
        raise RuntimeError(
            "Stable-Tag der Ziel-Policy verweist nicht exakt auf den Ziel-Commit"
        )
    return stable


def _assert_tree_no_symlinks(root: str) -> None:
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        for name in (*dirnames, *filenames):
            path = os.path.join(directory, name)
            if stat.S_ISLNK(os.lstat(path).st_mode):
                raise RuntimeError(f"Symlink in Release-Baum nicht erlaubt: {path}")


def _sync_release_web(
    repo_dir: str,
    policy: dict,
    *,
    allow_config_bootstrap: bool = False,
) -> None:
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
    if allow_config_bootstrap:
        if not repair_legacy_paths_file():
            raise RuntimeError("Legacy-Pfadvertrag konnte nicht repariert werden")
    else:
        print(
            "  [OK] Bestehende Betriebskonfiguration und Legacy-Pfadspiegel "
            "bleiben im Release-Fenster unverändert."
        )
    from .apache_security import ensure_apache_runtime_path_protection
    if not ensure_apache_runtime_path_protection(
        run_command,
        reload_apache=True,
        allow_mutation=True,
    ):
        raise RuntimeError(
            "Apache-Schutz für Daten-, Log-, Ramdisk- und Temp-Pfade "
            "konnte nicht aktiviert werden"
        )
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
    """Bindet Rolle/Konfiguration; Unit-Altformen bleiben Rückfallinformation.

    Der Finalizer läuft bereits mit dem Zielcode und darf deshalb bekannte
    Produktunits normalisieren. Inventar, Aktivität, Maskierung und Enablement
    des Altstands sind keine Zielautorität und müssen sich nach Stop,
    ``daemon-reload`` oder Unitprojektion ändern dürfen.
    """

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
    if expected_legacy_activity not in {"absent", "active", "inactive", "failed"}:
        raise RuntimeError("Erwarteter Legacy-Betriebszustand ist ungültig")
    current_units_sha256 = _transition_units_sha256(state.preinstalled_units)
    if (
        current_units_sha256 != expected_units_sha256
        or state.legacy_e3dc_activity != expected_legacy_activity
    ):
        print(
            "  [i] Alter Unitbestand änderte sich im sicheren Updatefenster; "
            "der Zielrelease normalisiert den Sollzustand."
        )


def _announce_finalizer_phase(index: int, total: int, label: str) -> None:
    """Macht lange Release-Phasen im Web- und Konsolenprotokoll sichtbar."""

    print(f"[PHASE {index}/{total}] {label}", flush=True)


def finalize_release_from_target(
    *,
    repo_dir: str,
    execution_root: str,
    target_commit: str,
    target_tag: str,
    expected_role: str,
    expected_config_state: str,
    expected_config_sha256: str,
    expected_units_sha256: str,
    expected_legacy_activity: str,
    expected_venv_state: str,
    expected_venv_path: str,
    update_safety_transaction: str | None = None,
    update_safety_receipt_sha256: str | None = None,
    update_safety_service_unit: str | None = None,
    update_safety_runtime_directory: str | None = None,
    update_safety_token_path: str | None = None,
    explicit_download_bootstrap: bool = False,
    headless: bool = True,
    privileged_preimages=None,
    postcommit_state: dict[str, bool] | None = None,
) -> None:
    """Finalisiert einen Reset ausschließlich aus dem versiegelten Commit-Snapshot."""

    phase_total = 7
    _announce_finalizer_phase(1, phase_total, "Zielbindung und Release-Policy prüfen")
    target_root = _validate_bootstrap_install_path(repo_dir)
    snapshot_root = _validate_bootstrap_install_path(execution_root)
    loaded_root = os.path.dirname(INSTALLER_DIR)
    if (
        os.path.realpath(loaded_root) != snapshot_root
        or os.path.realpath(INSTALL_PATH) != target_root
        or os.path.realpath(os.environ.get("E3DC_BOOTSTRAP_ROOT", "")) != target_root
        or os.path.realpath(os.environ.get("E3DC_BOOTSTRAP_RUNNER_ROOT", "")) != snapshot_root
    ):
        raise RuntimeError("Target-Finalizer wurde nicht aus dem versiegelten Ausführungssnapshot geladen")
    commit = _validate_full_commit(target_commit)
    tag = _normalize_release_tag(target_tag)
    role = str(expected_role or "").strip().lower()
    if role not in VALID_HA_ROLES:
        raise RuntimeError("Erwartete HA-/Shadow-Rolle ist ungültig")
    if not isinstance(explicit_download_bootstrap, bool):
        raise RuntimeError("Download-Bootstrap-Vertrag ist nicht boolesch")

    safety_values = (
        update_safety_transaction,
        update_safety_receipt_sha256,
        update_safety_service_unit,
        update_safety_runtime_directory,
        update_safety_token_path,
    )
    if any(safety_values) and not all(safety_values):
        raise RuntimeError("Target-Finalizer besitzt einen partiellen Update-Sicherheitsvertrag")
    if postcommit_state is not None and postcommit_state != {
        "commit_attempted": False
    }:
        raise RuntimeError("Target-Finalizer besitzt keinen frischen PostCommit-Grenzzustand")
    safety_contract = None
    if all(safety_values):
        safety_contract = _read_update_safety_contract()
        if (
            safety_contract is None
            or safety_contract.state != "pending"
            or safety_contract.transaction_id != update_safety_transaction
            or safety_contract.receipt_sha256 != update_safety_receipt_sha256
            or safety_contract.finalizer_unit != update_safety_service_unit
            or safety_contract.runtime_directory != update_safety_runtime_directory
            or safety_contract.token_path != update_safety_token_path
            or safety_contract.target_commit != commit
            or safety_contract.target_tag != _normalize_release_tag(target_tag)
            or safety_contract.role != role
        ):
            raise RuntimeError("Target-Finalizer sieht nicht das pending Sicherheitsreceipt")
        _verify_update_safety_marker(safety_contract, expected_present=True)
        _reload_and_verify_update_safety_dropins(safety_contract, expected_present=True)
        _assert_managed_finalizer_service(safety_contract)
        _assert_strict_update_writer_quiescence(
            repo_dir=target_root,
            transaction_id=safety_contract.transaction_id,
        )

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
    watchdog_runtime_required = _watchdog_runtime_venv_required(state)

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
    elif watchdog_runtime_required:
        if expected_venv_state != "present":
            raise RuntimeError("Watchdog-Laufzeit-venv ist im Finalizer nicht vorhanden")
        if not os.path.isabs(str(expected_venv_path or "")):
            raise RuntimeError("Watchdog-Laufzeit-venv besitzt keinen absoluten Pfad")
    elif expected_venv_state != "unused" or expected_venv_path:
        raise RuntimeError("venv-Preimage ist ohne Python-Paketpolicy unzulässig")
    _validate_watchdog_runtime_venv_contract(
        required=watchdog_runtime_required,
        expected_venv_state=expected_venv_state,
        expected_venv_path=expected_venv_path,
        target_root=target_root,
        install_user=install_user,
    )

    _announce_finalizer_phase(2, phase_total, "Paket- und Repositoryzustand herstellen")
    _secure_repo_permissions(
        target_root,
        install_user,
        expected_commit=commit,
    )
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
    if safety_contract is not None:
        _validate_update_safety_contract(safety_contract, expected_state="pending")
        _verify_update_safety_marker(safety_contract, expected_present=True)
        _reload_and_verify_update_safety_dropins(safety_contract, expected_present=True)

    _announce_finalizer_phase(3, phase_total, "Webroot und Berechtigungen synchronisieren")
    _sync_release_web(
        target_root,
        policy,
        allow_config_bootstrap=state.bootstrap_legacy_config,
    )
    if policy.get("run_permissions", True):
        from .permissions import run_permissions_wizard
        expected_commit_env = "E3DC_RELEASE_EXPECTED_COMMIT"
        previous_expected_commit = os.environ.get(expected_commit_env)
        os.environ[expected_commit_env] = commit
        try:
            if run_permissions_wizard(
                headless=True,
                release_quiesced=True,
                bound_privileged_preimages=privileged_preimages,
            ) is False:
                raise RuntimeError("Berechtigungsreparatur fehlgeschlagen")
        finally:
            if previous_expected_commit is None:
                os.environ.pop(expected_commit_env, None)
            else:
                os.environ[expected_commit_env] = previous_expected_commit
        _secure_repo_permissions(
            target_root,
            install_user,
            expected_commit=commit,
        )
    if safety_contract is not None:
        _validate_update_safety_contract(safety_contract, expected_state="pending")
        _verify_update_safety_marker(safety_contract, expected_present=True)
        _reload_and_verify_update_safety_dropins(safety_contract, expected_present=True)

    from .permissions import (
        PI_GUARD_PATH,
        ensure_private_ml_model_store,
        harden_web_program_permissions,
        refresh_watchdog_guard_script,
    )
    if not ensure_private_ml_model_store():
        raise RuntimeError("Privater ML-Modellspeicher konnte nicht sicher vorbereitet werden")
    if not harden_web_program_permissions():
        raise RuntimeError("Web-Programmrechte konnten nicht gehärtet werden")
    _announce_finalizer_phase(4, phase_total, "Kernservices und Migrationen vorbereiten")
    expected_service_dropins = (
        _update_safety_expected_dropins(safety_contract)
        if safety_contract is not None
        else None
    )
    # Der bekannte Storage-Alt-Override muss vor dem Bundle-Capture
    # normalisiert werden. Andernfalls stuft der absichtlich strenge
    # Service-Snapshot selbst einen semantisch identischen Produkt-Override
    # als fremd ein und verhindert seine eigene Migration.
    if not migrate_storage_manager_next_override(
        allow_redundant_current_override=explicit_download_bootstrap,
    ):
        raise RuntimeError("Storage-Service-Migration ist fehlgeschlagen")
    if not _ensure_install_center_core_services(
        expected_recovery_dropins=expected_service_dropins,
        allow_optional_not_found_compat=explicit_download_bootstrap,
    ):
        raise RuntimeError("Kernservice-Installation ist unvollständig")
    from .permissions import storage_manager_writer_contract
    storage_writer = storage_manager_writer_contract()
    if not storage_writer.get("ok"):
        raise RuntimeError(
            "Storage-Single-Writer-Vertrag ist nach der Unit-Migration verletzt: "
            + ", ".join(storage_writer.get("blockers") or ["unbekannt"])
        )
    from .ramdisk_guard import require_ramdisk_service_dropins
    ramdisk_dropins = require_ramdisk_service_dropins()
    if ramdisk_dropins.get("skipped"):
        raise RuntimeError(
            "Bare-Metal-Update hat die tmpfs-Startsperren unerwartet übersprungen"
        )

    def refresh_bound_watchdog(*, start_service=True):
        if not watchdog_runtime_required:
            return refresh_watchdog_guard_script(start_service=start_service)
        return _run_with_bound_bootstrap_venv(
            expected_venv_path,
            lambda: refresh_watchdog_guard_script(start_service=start_service),
        )

    watchdog_refresh_required = bool(
        os.path.exists(PI_GUARD_PATH) or _service_unit_exists("piguard")
    )
    if watchdog_refresh_required != watchdog_runtime_required:
        raise RuntimeError("Watchdog-Bestand driftete vor der Guard-Projektion")

    projected_piguard = False
    if safety_contract is not None:
        _validate_update_safety_contract(safety_contract, expected_state="pending")
        _verify_update_safety_marker(safety_contract, expected_present=True)
        _reload_and_verify_update_safety_dropins(safety_contract, expected_present=True)
        _assert_managed_finalizer_service(safety_contract)
        _assert_strict_update_writer_quiescence(
            repo_dir=target_root,
            transaction_id=safety_contract.transaction_id,
        )
        if not refresh_bound_watchdog(start_service=False):
            raise RuntimeError("Watchdog-Guard konnte unter Bootblock nicht aktualisiert werden")
        projected_piguard = watchdog_refresh_required
        if projected_piguard and not _service_unit_exists("piguard"):
            raise RuntimeError("Quiesced Watchdog-Projektion besitzt keine geladene Unit")
        _project_bare_metal_logrotate_config(
            repo_dir=target_root,
            target_commit=commit,
            install_user=install_user,
        )
        if not _prepare_v4_service_activation(
            services=restart_services,
            transition_state=state,
            projected_piguard=projected_piguard,
        ):
            raise RuntimeError(
                "Persistente Dienstvorbereitung blieb vor dem Startgate unvollständig"
            )
        pause_reason = f"update-safety:{safety_contract.transaction_id}"
        _enable_watchdog_update_pause(pause_reason)
        _verify_watchdog_pause_fresh(pause_reason)
        _validate_update_safety_contract(safety_contract, expected_state="pending")
        _verify_update_safety_marker(safety_contract, expected_present=True)
        _reload_and_verify_update_safety_dropins(safety_contract, expected_present=True)
        _assert_managed_finalizer_service(safety_contract)
        _assert_strict_update_writer_quiescence(
            repo_dir=target_root,
            transaction_id=safety_contract.transaction_id,
        )
        _verify_transition_state(state)
        _create_update_safety_start_token(
            safety_contract,
            repo_dir=target_root,
        )

    if safety_contract is None:
        _verify_transition_state(state)
    _announce_finalizer_phase(5, phase_total, "Dienste aktivieren und geordnet starten")
    if not _restart_v4_services(
        headless=headless,
        services=restart_services,
        transition_state=state,
        prepared_start_only=safety_contract is not None,
        projected_piguard=projected_piguard,
    ):
        raise RuntimeError("Erwartete Dienste konnten nicht vollständig gestartet werden")
    if safety_contract is None and not refresh_bound_watchdog():
        _stop_v4_services(restart_services)
        raise RuntimeError("Watchdog-Guard konnte nach dem finalen Dienststart nicht aktualisiert werden")
    _announce_finalizer_phase(6, phase_total, "Gesundheit und Bootvertrag verifizieren")
    if not _post_update_healthcheck(
        restart_services,
        transition_state=state,
        projected_piguard=projected_piguard,
    ):
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
    current_head = _resolve_git_commit(target_root, "HEAD", install_user)
    if not current_head or not _exact_commit_matches(commit, current_head):
        raise RuntimeError("Repository-HEAD driftete im finalen Target-Readback")
    if safety_contract is not None:
        _validate_update_safety_contract(safety_contract, expected_state="pending")
        _verify_update_safety_marker(safety_contract, expected_present=True)
        _reload_and_verify_update_safety_dropins(safety_contract, expected_present=True)
        _assert_managed_finalizer_service(safety_contract)
        commit_may_be_irreversible = False
        try:
            # Die Grenze liegt bewusst vor dem Call. Dadurch bleibt auch ein
            # Signal nach möglicher Receipt-Mutation, aber vor Return/Assign,
            # konservativ und kann niemals Altpreimages restaurieren.
            commit_may_be_irreversible = True
            if postcommit_state is not None:
                postcommit_state["commit_attempted"] = True
            committed_contract = _commit_update_safety_receipt(safety_contract)
            _clear_update_safety_marker(committed_contract)
            _remove_update_safety_dropins(committed_contract)
            _announce_finalizer_phase(
                7,
                phase_total,
                "Initiale lokale Prognose aktualisieren",
            )
            try:
                run_initial_forecast(os.path.join(target_root, "Installer"))
            except Exception as exc:
                update_logger.warning(
                    "Initiale Prognose blieb nach committed Update best-effort: %s",
                    exc,
                )
        except BaseException as exc:
            if isinstance(exc, UpdateSafetyPostCommitError):
                raise
            if not commit_may_be_irreversible:
                raise
            raise UpdateSafetyPostCommitError(
                "Target-Finalizer brach nach Eintritt in seine irreversible "
                "Commit-/PostCommit-Kapsel ab"
            ) from exc
    else:
        _announce_finalizer_phase(7, phase_total, "Initiale lokale Prognose aktualisieren")
        run_initial_forecast(os.path.join(target_root, "Installer"))
        _project_bare_metal_logrotate_config(
            repo_dir=target_root,
            target_commit=commit,
            install_user=install_user,
        )


def _target_execution_archive_entries(
    *,
    repo_dir: str,
    target_commit: str,
    install_user: str,
) -> dict[str, tuple[bytes, int]]:
    """Liest den vollständigen ausführbaren Installer-Baum direkt aus dem Commit."""

    required = set(TARGET_EXECUTION_SNAPSHOT_ROOT_FILES) | set(TARGET_FINALIZER_RELATIVE_FILES)
    return read_commit_entries(
        repo_dir,
        _validate_full_commit(target_commit),
        (*TARGET_EXECUTION_SNAPSHOT_ROOT_FILES, "Installer"),
        required_paths=required,
        run_as_user=install_user,
        maximum_files=TARGET_EXECUTION_SNAPSHOT_MAX_FILES,
        maximum_file_bytes=TARGET_EXECUTION_SNAPSHOT_MAX_FILE_BYTES,
        maximum_total_bytes=TARGET_EXECUTION_SNAPSHOT_MAX_TOTAL_BYTES,
    )


def _snapshot_python_contract(
    entries: dict[str, tuple[bytes, int]],
    relative_path: str,
) -> ast.Module:
    """Parst ausschließlich den bereits commitgebundenen Snapshot-Blob."""

    try:
        payload, _mode = entries[relative_path]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Ziel-Updater-Vertrag fehlt im Snapshot: {relative_path}"
        ) from exc
    try:
        source = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"Ziel-Updater-Vertrag ist nicht als UTF-8 gebunden: {relative_path}"
        ) from exc
    try:
        return ast.parse(source, filename=relative_path)
    except SyntaxError as exc:
        raise RuntimeError(
            f"Ziel-Updater-Vertrag besitzt ungültige Python-Syntax: {relative_path}"
        ) from exc


def _module_function_parameters(module: ast.Module, name: str) -> frozenset[str]:
    functions = [
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(functions) != 1:
        return frozenset()
    function = functions[0]
    return frozenset(
        argument.arg
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    )


def _module_has_function(module: ast.Module, name: str) -> bool:
    return (
        sum(
            1
            for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        )
        == 1
    )


def _module_string_literals(module: ast.Module) -> frozenset[str]:
    return frozenset(
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _target_snapshot_updater_mode(
    entries: dict[str, tuple[bytes, int]],
) -> str:
    """Wählt nur einen strukturell belegten Ziel-Einstieg, sonst fail-closed."""

    finalizer_module = _snapshot_python_contract(
        entries,
        "Installer/release_finalize.py",
    )
    update_module = _snapshot_python_contract(
        entries,
        "Installer/update.py",
    )
    finalizer_literals = _module_string_literals(finalizer_module)
    native_finalizer = bool(
        _module_function_parameters(
            finalizer_module,
            "_run_target_updater_handoff",
        )
        and "--target-updater-handoff" in finalizer_literals
        and _module_function_parameters(
            update_module,
            "execute_verified_target_update",
        )
        >= {
            "repo_dir",
            "target_commit",
            "target_tag",
            "expected_role",
        }
    )
    if native_finalizer:
        return "native"

    # Veröffentlichte Übergangsversionen vor dem Ziel-Updater-Handoff
    # besitzen bereits den SHA-/Rollen-gebundenen Bootstrap und den separaten
    # Target-Finalizer. Genau dieser vorhandene Vertrag darf über den kleinen,
    # ebenfalls versiegelten Kompatibilitäts-Runner betreten werden.
    legacy_parameters = _module_function_parameters(update_module, "update_e3dc")
    legacy_finalizer = (
        _module_has_function(finalizer_module, "main")
        and _module_has_function(
            finalizer_module,
            "_bind_execution_snapshot",
        )
        and _module_has_function(
            finalizer_module,
            "_run_legacy_product_bridge",
        )
        and _module_has_function(
            finalizer_module,
            "_bind_legacy_product_invocation",
        )
        and "--expected-release-sha" in finalizer_literals
        and "--expected-release-tag" in finalizer_literals
        and "--expected-ha-role" in finalizer_literals
        and "E3DC_RELEASE_TARGET_FINALIZER_OK" in finalizer_literals
    )
    legacy_update = all(
        _module_has_function(update_module, name)
        for name in (
            "_invoke_target_finalizer",
            "_normalize_target_finalizer_files",
            "_target_tag_authorized",
            "_validate_target_release",
            "_recover_failed_transition",
        )
    )
    if legacy_finalizer and legacy_update and legacy_parameters >= {
        "headless",
        "target_ref",
        "target_install_path",
        "expected_release_sha",
        "expected_ha_role",
    }:
        return "compat"
    raise RuntimeError(
        "Ziel-Release besitzt weder den nativen Ziel-Updater noch den "
        "sicher gebundenen Bootstrap-Kompatibilitätsvertrag"
    )


def _snapshot_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _verify_target_execution_snapshot(
    snapshot_root: str,
    entries: dict[str, tuple[bytes, int]],
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    """Bindet einen geschlossenen, nicht beschreibbaren Snapshot bytegenau."""

    root = os.path.abspath(snapshot_root)
    if os.path.realpath(root) != root:
        raise RuntimeError("Target-Ausführungssnapshot darf kein Symlinkpfad sein")
    parent_descriptor = _open_directory_nofollow(Path(root).parent)
    try:
        parent = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid not in {0, owner_uid}
            or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("Elternverzeichnis des Target-Ausführungssnapshots ist nicht vertrauenswürdig")
    finally:
        os.close(parent_descriptor)

    expected_directories = {""}
    for relative_path in entries:
        parts = Path(relative_path).parts
        for length in range(1, len(parts)):
            expected_directories.add(Path(*parts[:length]).as_posix())

    actual_directories = {""}
    actual_files = set()
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        directory_relative = (
            "" if directory_path == Path(root)
            else directory_path.relative_to(root).as_posix()
        )
        metadata = os.lstat(directory)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or metadata.st_gid != owner_gid
            or stat.S_IMODE(metadata.st_mode) != 0o555
        ):
            raise RuntimeError(
                f"Target-Ausführungssnapshot besitzt ein beschreibbares oder fremdes Verzeichnis: "
                f"{directory_relative or '.'}"
            )
        actual_directories.add(directory_relative)
        for name in list(dirnames):
            candidate = directory_path / name
            candidate_metadata = os.lstat(candidate)
            if stat.S_ISLNK(candidate_metadata.st_mode) or not stat.S_ISDIR(candidate_metadata.st_mode):
                raise RuntimeError("Target-Ausführungssnapshot enthält eine Symlink-/Nichtverzeichniskomponente")
        for name in filenames:
            candidate = directory_path / name
            candidate_metadata = os.lstat(candidate)
            if stat.S_ISLNK(candidate_metadata.st_mode) or not stat.S_ISREG(candidate_metadata.st_mode):
                raise RuntimeError("Target-Ausführungssnapshot enthält eine nicht reguläre Datei")
            actual_files.add(candidate.relative_to(root).as_posix())

    if actual_directories != expected_directories or actual_files != set(entries):
        raise RuntimeError("Target-Ausführungssnapshot besitzt nicht den exakt gebundenen Dateibaum")

    for relative_path, (expected, expected_mode) in entries.items():
        target = os.path.join(root, relative_path)
        descriptor, before = _open_regular_file_nofollow(target)
        try:
            if (
                before.st_nlink != 1
                or before.st_uid != owner_uid
                or before.st_gid != owner_gid
                or stat.S_IMODE(before.st_mode) != expected_mode
                or before.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                or before.st_size != len(expected)
            ):
                raise RuntimeError(
                    "Target-Ausführungssnapshot besitzt unzulässige Dateimetadaten: "
                    + _target_metadata_detail(relative_path, before)
                )
            identity = _snapshot_file_identity(before)
            actual = _read_descriptor_bytes(descriptor, len(expected))
            after = os.fstat(descriptor)
            if _snapshot_file_identity(after) != identity:
                raise RuntimeError(
                    f"Target-Ausführungssnapshot driftete während der Bindung: {relative_path}"
                )
            if actual != expected:
                raise RuntimeError(
                    f"Target-Ausführungssnapshot weicht bytegenau vom Commit ab: {relative_path}"
                )
        finally:
            os.close(descriptor)


def _create_target_execution_snapshot(
    entries: dict[str, tuple[bytes, int]],
    *,
    snapshot_parent: str = TARGET_EXECUTION_SNAPSHOT_PARENT,
    snapshot_prefix: str = TARGET_FINALIZER_SNAPSHOT_PREFIX,
) -> str:
    """Erzeugt den Root-Finalizer-Baum zunächst privat und versiegelt ihn dann."""

    if (
        not re.fullmatch(r"\.[a-z0-9.-]{8,80}-", str(snapshot_prefix or ""))
        or "/" in snapshot_prefix
        or "\\" in snapshot_prefix
    ):
        raise RuntimeError("Target-Ausführungssnapshot besitzt kein zulässiges Präfix")
    owner_uid = os.geteuid()
    owner_gid = os.getegid()
    parent_descriptor = _open_directory_nofollow(snapshot_parent)
    try:
        parent = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid not in {0, owner_uid}
            or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("Snapshot-Elternverzeichnis ist nicht vertrauenswürdig")
    finally:
        os.close(parent_descriptor)

    snapshot_root = tempfile.mkdtemp(
        prefix=snapshot_prefix,
        dir=os.path.abspath(snapshot_parent),
    )
    directories = {Path(snapshot_root)}
    try:
        root_metadata = os.lstat(snapshot_root)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != owner_uid
            or root_metadata.st_gid != owner_gid
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise RuntimeError("Target-Ausführungssnapshot wurde nicht privat erzeugt")

        for relative_path, (payload, final_mode) in sorted(entries.items()):
            if final_mode not in {0o444, 0o555}:
                raise RuntimeError("Target-Ausführungssnapshot enthält einen beschreibbaren Zielmodus")
            target = Path(snapshot_root, relative_path)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            current = target.parent
            while current != Path(snapshot_root):
                directories.add(current)
                current = current.parent
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                view = memoryview(payload)
                written = 0
                while written < len(view):
                    count = os.write(descriptor, view[written:])
                    if count <= 0:
                        raise RuntimeError("Target-Ausführungssnapshot konnte einen Blob nicht vollständig schreiben")
                    written += count
                os.fsync(descriptor)
                os.fchmod(descriptor, final_mode)
                sealed = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(sealed.st_mode)
                    or sealed.st_nlink != 1
                    or sealed.st_uid != owner_uid
                    or sealed.st_gid != owner_gid
                    or stat.S_IMODE(sealed.st_mode) != final_mode
                    or sealed.st_size != len(payload)
                ):
                    raise RuntimeError("Target-Ausführungssnapshot konnte eine Datei nicht versiegeln")
            finally:
                os.close(descriptor)

        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            os.chmod(directory, 0o555)
        _verify_target_execution_snapshot(
            snapshot_root,
            entries,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        return snapshot_root
    except Exception:
        _remove_target_execution_snapshot(snapshot_root)
        raise


def _remove_target_execution_snapshot(snapshot_root: str) -> None:
    """Entfernt ausschließlich den selbst erzeugten, regulären Snapshotbaum."""

    root = os.path.abspath(snapshot_root)
    try:
        metadata = os.lstat(root)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("Target-Ausführungssnapshot ist vor der Bereinigung kein Verzeichnis")
    for directory, dirnames, _filenames in os.walk(root, topdown=True, followlinks=False):
        for name in dirnames:
            candidate = os.path.join(directory, name)
            if stat.S_ISLNK(os.lstat(candidate).st_mode):
                raise RuntimeError("Target-Ausführungssnapshot enthält vor der Bereinigung einen Symlink")
        os.chmod(directory, 0o700)
    shutil.rmtree(root)


def _cleanup_stale_target_execution_snapshots(
    snapshot_parent: str,
    *,
    prefixes,
) -> int:
    """Entfernt unter gehaltenem Lock nur eigene, sicher gebundene Snapshotreste."""

    parent = os.path.abspath(snapshot_parent)
    _open_fd = _required_update_lock_fd()
    del _open_fd
    parent_descriptor = _open_directory_nofollow(parent)
    try:
        parent_metadata = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != 0
            or parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError(
                "Snapshot-Restebereinigung besitzt keinen sicheren Elternpfad"
            )
    finally:
        os.close(parent_descriptor)

    allowed_prefixes = tuple(str(item) for item in prefixes)
    if (
        not allowed_prefixes
        or any(
            prefix
            not in {
                TARGET_UPDATER_SNAPSHOT_PREFIX,
                TARGET_COMPAT_UPDATER_SNAPSHOT_PREFIX,
                TARGET_FINALIZER_SNAPSHOT_PREFIX,
            }
            for prefix in allowed_prefixes
        )
    ):
        raise RuntimeError("Snapshot-Restebereinigung besitzt kein freigegebenes Präfix")

    stale_roots = []
    with os.scandir(parent) as entries:
        for entry in entries:
            if not entry.name.startswith(allowed_prefixes):
                continue
            path = os.path.abspath(entry.path)
            if os.path.dirname(path) != parent:
                raise RuntimeError("Snapshot-Restepfad verlässt den gebundenen Elternpfad")
            metadata = entry.stat(follow_symlinks=False)
            if (
                entry.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != os.getegid()
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise RuntimeError(
                    f"Snapshot-Rest besitzt unsichere Metadaten: {entry.name}"
                )
            for directory, dirnames, filenames in os.walk(
                path,
                topdown=True,
                followlinks=False,
            ):
                directory_metadata = os.lstat(directory)
                if (
                    stat.S_ISLNK(directory_metadata.st_mode)
                    or not stat.S_ISDIR(directory_metadata.st_mode)
                    or directory_metadata.st_uid != os.geteuid()
                    or directory_metadata.st_gid != os.getegid()
                    or directory_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    raise RuntimeError(
                        f"Snapshot-Restbaum ist nicht sicher gebunden: {entry.name}"
                    )
                for name in dirnames:
                    child = os.path.join(directory, name)
                    child_metadata = os.lstat(child)
                    if (
                        stat.S_ISLNK(child_metadata.st_mode)
                        or not stat.S_ISDIR(child_metadata.st_mode)
                    ):
                        raise RuntimeError(
                            f"Snapshot-Rest enthält einen Fremdpfad: {entry.name}"
                        )
                for name in filenames:
                    child = os.path.join(directory, name)
                    child_metadata = os.lstat(child)
                    if (
                        stat.S_ISLNK(child_metadata.st_mode)
                        or not stat.S_ISREG(child_metadata.st_mode)
                        or child_metadata.st_nlink != 1
                        or child_metadata.st_uid != os.geteuid()
                        or child_metadata.st_gid != os.getegid()
                        or child_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                    ):
                        raise RuntimeError(
                            f"Snapshot-Rest enthält eine unsichere Datei: {entry.name}"
                        )
            stale_roots.append(path)

    for stale_root in stale_roots:
        _remove_target_execution_snapshot(stale_root)
    if stale_roots:
        print(
            f"[i] {len(stale_roots)} sicher gebundene alte "
            "Updater-Snapshotreste wurden entfernt.",
            flush=True,
        )
    return len(stale_roots)


def _trusted_same_filesystem_snapshot_parent(repo_dir: str) -> str:
    """Findet einen root-kontrollierten Snapshot-Ort auf dem Produkt-Dateisystem."""

    root = Path(os.path.abspath(str(repo_dir or "")))
    if not root.is_absolute() or os.path.realpath(root) != str(root):
        raise RuntimeError("Produktpfad für den Ziel-Updater ist nicht kanonisch")
    root_metadata = os.lstat(root)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise RuntimeError("Produktpfad für den Ziel-Updater ist kein Verzeichnis")
    product_device = root_metadata.st_dev

    candidate = root.parent
    while True:
        metadata = os.lstat(candidate)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("Snapshot-Pfad enthält eine Symlink-/Nichtverzeichniskomponente")
        if (
            metadata.st_dev == product_device
            and metadata.st_uid == 0
            and not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            and os.path.realpath(candidate) == str(candidate)
        ):
            return str(candidate)
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    raise RuntimeError(
        "Kein root-kontrollierter Snapshot-Ort auf dem Produkt-Dateisystem verfügbar"
    )


def _current_compat_updater_bridge_entries(
    *,
    install_user: str,
) -> tuple[dict[str, tuple[bytes, int]], str]:
    """Bindet ausschließlich den kleinen, aktuell laufenden Compat-Runner."""

    source = os.path.abspath(
        os.path.join(INSTALLER_DIR, "release_finalize.py")
    )
    if os.path.realpath(source) != source:
        raise RuntimeError(
            "Kompatibilitäts-Runner besitzt keinen kanonischen Quellpfad"
        )
    try:
        account = pwd.getpwnam(str(install_user))
    except KeyError as exc:
        raise RuntimeError(
            "Installationsbenutzer für den Kompatibilitäts-Runner fehlt"
        ) from exc

    descriptor, before = _open_regular_file_nofollow(source)
    try:
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, account.pw_uid}
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size < 1
            or before.st_size > TARGET_EXECUTION_SNAPSHOT_MAX_FILE_BYTES
        ):
            raise RuntimeError(
                "Kompatibilitäts-Runner besitzt unzulässige Quellmetadaten"
            )
        identity = _snapshot_file_identity(before)
        payload = _read_descriptor_bytes(descriptor, before.st_size)
        after = os.fstat(descriptor)
        if (
            _snapshot_file_identity(after) != identity
            or len(payload) != before.st_size
        ):
            raise RuntimeError(
                "Kompatibilitäts-Runner driftete während der Bindung"
            )
    finally:
        os.close(descriptor)

    current = os.lstat(source)
    if _snapshot_file_identity(current) != identity:
        raise RuntimeError(
            "Kompatibilitäts-Runner driftete nach der Bindung"
        )
    module = _snapshot_python_contract(
        {"Installer/release_finalize.py": (payload, 0o444)},
        "Installer/release_finalize.py",
    )
    if (
        not _module_function_parameters(
            module,
            "_run_compat_target_updater_handoff",
        )
        or "--compat-target-updater-handoff"
        not in _module_string_literals(module)
    ):
        raise RuntimeError(
            "Aktueller Finalizer besitzt keinen geprüften Kompatibilitäts-Einstieg"
        )
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "Installer/release_finalize.py": (payload, 0o444),
    }, digest


def _invoke_verified_target_updater(
    *,
    repo_dir: str,
    target_commit: str,
    target_tag: str,
    requested_target_tag: str | None,
    expected_role: str,
    install_user: str,
    reinstall_current: bool = False,
) -> bool | str:
    """Übergibt die eigentliche Transaktion an den Updater des Ziel-Commits."""

    lock_fd = _required_update_lock_fd()
    snapshot_entries = _target_execution_archive_entries(
        repo_dir=repo_dir,
        target_commit=target_commit,
        install_user=install_user,
    )
    updater_mode = _target_snapshot_updater_mode(snapshot_entries)
    compat_entries: dict[str, tuple[bytes, int]] | None = None
    compat_digest = ""
    if updater_mode == "compat":
        compat_entries, compat_digest = _current_compat_updater_bridge_entries(
            install_user=install_user,
        )

    snapshot_parent = _trusted_same_filesystem_snapshot_parent(repo_dir)
    _cleanup_stale_target_execution_snapshots(
        snapshot_parent,
        prefixes=(
            TARGET_UPDATER_SNAPSHOT_PREFIX,
            TARGET_COMPAT_UPDATER_SNAPSHOT_PREFIX,
            TARGET_FINALIZER_SNAPSHOT_PREFIX,
        ),
    )
    snapshot_root = ""
    compat_root = ""
    try:
        snapshot_root = _create_target_execution_snapshot(
            snapshot_entries,
            snapshot_parent=snapshot_parent,
            snapshot_prefix=TARGET_UPDATER_SNAPSHOT_PREFIX,
        )
        if os.lstat(snapshot_root).st_dev != os.lstat(repo_dir).st_dev:
            raise RuntimeError(
                "Ziel-Updater-Snapshot liegt nicht auf dem Produkt-Dateisystem"
            )

        runner_root = snapshot_root
        if updater_mode == "compat":
            if compat_entries is None or not compat_digest:
                raise RuntimeError(
                    "Kompatibilitäts-Runner wurde nicht vollständig gebunden"
                )
            compat_root = _create_target_execution_snapshot(
                compat_entries,
                snapshot_parent=snapshot_parent,
                snapshot_prefix=TARGET_COMPAT_UPDATER_SNAPSHOT_PREFIX,
            )
            if (
                os.lstat(compat_root).st_dev != os.lstat(repo_dir).st_dev
                or os.path.realpath(compat_root) == os.path.realpath(snapshot_root)
            ):
                raise RuntimeError(
                    "Kompatibilitäts-Runner ist nicht getrennt an das "
                    "Produkt-Dateisystem gebunden"
                )
            runner_root = compat_root

        python = _trusted_system_python()
        updater = os.path.join(
            runner_root,
            "Installer",
            "release_finalize.py",
        )
        environment = dict(os.environ)
        for name in (
            "E3DC_BOOTSTRAP_ROOT",
            "E3DC_BOOTSTRAP_RUNNER_ROOT",
            "E3DC_BOOTSTRAP_SOURCE_COMMIT",
            "E3DC_BOOTSTRAP_USER",
            "E3DC_BOOTSTRAP_VENV",
            "PYTHONHOME",
            "PYTHONPATH",
        ):
            environment.pop(name, None)
        environment["E3DC_BOOTSTRAP_ROOT"] = repo_dir
        environment["E3DC_BOOTSTRAP_RUNNER_ROOT"] = runner_root
        environment["E3DC_BOOTSTRAP_USER"] = install_user
        environment["E3DC_INSTALL_ROOT"] = repo_dir
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        environment["LC_ALL"] = "C.UTF-8"
        environment["LANG"] = "C.UTF-8"
        environment[UPDATE_LOCK_ENV] = str(lock_fd)

        _verify_target_execution_snapshot(
            snapshot_root,
            snapshot_entries,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )
        if updater_mode == "compat":
            _verify_target_execution_snapshot(
                compat_root,
                compat_entries,
                owner_uid=os.geteuid(),
                owner_gid=os.getegid(),
            )

        command = [
            python,
            "-I",
            "-B",
            "-u",
            updater,
        ]
        if updater_mode == "native":
            command.extend((
                "--target-updater-handoff",
            ))
        else:
            command.extend((
                "--compat-target-updater-handoff",
                "--target-execution-root", snapshot_root,
                "--compat-bridge-sha256", compat_digest,
            ))
        command.extend((
                "--install-path", repo_dir,
                "--expected-release-sha", target_commit,
                "--expected-release-tag", target_tag,
                "--expected-ha-role", expected_role,
        ))
        if requested_target_tag:
            command.extend(("--requested-release-tag", requested_target_tag))
        if reinstall_current:
            command.append("--reinstall-current")
        result = _run_streaming_argv(
            command,
            # Backup und Recovery gehören dem Ziel-Updater. Der alte Prozess
            # darf diese Gesamttransaktion deshalb niemals hart abbrechen.
            timeout=None,
            env=environment,
            pass_fds=(lock_fd,),
            label="Ziel-Updater",
        )
    finally:
        if compat_root:
            try:
                _remove_target_execution_snapshot(compat_root)
            except Exception as exc:
                update_logger.warning(
                    "Kompatibilitäts-Runner-Snapshot konnte nicht bereinigt werden: %s",
                    exc,
                )
        try:
            if snapshot_root:
                _remove_target_execution_snapshot(snapshot_root)
        except Exception as exc:
            update_logger.warning("Ziel-Updater-Snapshot konnte nicht bereinigt werden: %s", exc)
    marker = f"{TARGET_UPDATER_SUCCESS} {target_commit} {target_tag}"
    noop_marker = f"{TARGET_UPDATER_NOOP} {target_commit} {target_tag}"
    marker_count = int(
        (result.get("stdout_line_counts") or {}).get(marker, 0)
    )
    noop_count = int(
        (result.get("stdout_line_counts") or {}).get(noop_marker, 0)
    )
    if (
        not result.get("success")
        or marker_count + noop_count != 1
        or marker_count > 1
        or noop_count > 1
    ):
        raise RuntimeError(
            "Ziel-Updater fehlgeschlagen: "
            + _combined_process_diagnostics(result)
        )
    return UPDATE_ALREADY_CURRENT if noop_count == 1 else True


def _invoke_target_finalizer(
    *,
    repo_dir: str,
    target_commit: str,
    target_tag: str,
    state: TransitionState,
    package_transaction: PackageTransactionState,
    update_safety_contract: UpdateSafetyContract | None = None,
    explicit_download_bootstrap: bool = False,
) -> None:
    """Startet den Zielprozess direkt oder im crash-sicheren transienten Service."""

    lock_fd = _required_update_lock_fd()
    install_user = get_install_user()
    if update_safety_contract is not None:
        update_safety_contract = _validate_update_safety_contract(
            update_safety_contract,
            expected_state="pending",
        )
        if (
            update_safety_contract.target_commit != target_commit
            or update_safety_contract.target_tag != _normalize_release_tag(target_tag)
            or update_safety_contract.role != state.ha_role
        ):
            raise RuntimeError("Target-Finalizer widerspricht dem Update-Sicherheitsreceipt")
        _verify_update_safety_marker(update_safety_contract, expected_present=True)
        _reload_and_verify_update_safety_dropins(
            update_safety_contract,
            expected_present=True,
        )
        _assert_strict_update_writer_quiescence(
            repo_dir=repo_dir,
            transaction_id=update_safety_contract.transaction_id,
        )
    bound_target_files = {
        relative_path: _bind_target_file_to_commit(
            repo_dir=repo_dir,
            target_commit=target_commit,
            relative_path=relative_path,
            install_user=install_user,
        )
        for relative_path in TARGET_FINALIZER_RELATIVE_FILES
    }
    snapshot_entries = _target_execution_archive_entries(
        repo_dir=repo_dir,
        target_commit=target_commit,
        install_user=install_user,
    )

    for relative_path, expected_identity in bound_target_files.items():
        current = os.lstat(os.path.join(repo_dir, relative_path))
        if _file_identity(current) != expected_identity:
            raise RuntimeError(f"Target-Modul wurde nach der Commit-Bindung ausgetauscht: {relative_path}")

    config_state = "missing" if state.bootstrap_legacy_config else "present"
    venv_state, venv_path = _finalizer_venv_contract(package_transaction)

    snapshot_parent = _trusted_same_filesystem_snapshot_parent(repo_dir)
    _cleanup_stale_target_execution_snapshots(
        snapshot_parent,
        prefixes=(TARGET_FINALIZER_SNAPSHOT_PREFIX,),
    )
    snapshot_root = _create_target_execution_snapshot(
        snapshot_entries,
        snapshot_parent=snapshot_parent,
        snapshot_prefix=TARGET_FINALIZER_SNAPSHOT_PREFIX,
    )
    if os.lstat(snapshot_root).st_dev != os.lstat(repo_dir).st_dev:
        _remove_target_execution_snapshot(snapshot_root)
        raise RuntimeError(
            "Target-Finalizer-Snapshot liegt nicht auf dem Produkt-Dateisystem"
        )
    finalizer = os.path.join(snapshot_root, "Installer", "release_finalize.py")
    try:
        python = _trusted_system_python()
    except Exception:
        _remove_target_execution_snapshot(snapshot_root)
        raise

    environment = dict(os.environ)
    for name in (
        "E3DC_BOOTSTRAP_ROOT",
        "E3DC_BOOTSTRAP_RUNNER_ROOT",
        "E3DC_BOOTSTRAP_SOURCE_COMMIT",
        "E3DC_BOOTSTRAP_USER",
        "E3DC_BOOTSTRAP_VENV",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        environment.pop(name, None)
    environment["E3DC_BOOTSTRAP_ROOT"] = repo_dir
    environment["E3DC_BOOTSTRAP_RUNNER_ROOT"] = snapshot_root
    environment["E3DC_BOOTSTRAP_USER"] = install_user
    environment["E3DC_INSTALL_ROOT"] = repo_dir
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    environment["LC_ALL"] = "C.UTF-8"
    environment["LANG"] = "C.UTF-8"
    environment[UPDATE_LOCK_ENV] = str(lock_fd)
    finalizer_args = [
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
    ]
    if explicit_download_bootstrap:
        finalizer_args.append("--explicit-download-bootstrap")
    if update_safety_contract is not None:
        finalizer_args.extend((
            "--update-safety-transaction", update_safety_contract.transaction_id,
            "--update-safety-receipt-sha256", update_safety_contract.receipt_sha256,
            "--update-safety-service-unit", update_safety_contract.finalizer_unit,
            "--update-safety-runtime-directory", update_safety_contract.runtime_directory,
            "--update-safety-token-path", update_safety_contract.token_path,
        ))
    result = None
    managed_service_spawn_attempted = False
    managed_service_quiesced = False
    managed_service_quiesce_error: BaseException | None = None
    durable_committed_observed: UpdateSafetyContract | None = None
    try:
        _verify_target_execution_snapshot(
            snapshot_root,
            snapshot_entries,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )
        if update_safety_contract is None:
            result = _run_streaming_argv(
                [python, "-I", "-B", "-u", finalizer, *finalizer_args],
                timeout=TARGET_FINALIZER_TIMEOUT_S,
                env=environment,
                pass_fds=(lock_fd,),
                label="Target-Finalizer",
            )
        else:
            systemd_run = Path("/usr/bin/systemd-run")
            systemd_metadata = systemd_run.lstat()
            if (
                systemd_run.is_symlink()
                or not stat.S_ISREG(systemd_metadata.st_mode)
                or systemd_metadata.st_uid != 0
                or systemd_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or not systemd_metadata.st_mode & 0o111
            ):
                raise RuntimeError("Fester systemd-run-Pfad ist nicht vertrauenswürdig")
            lock_metadata = os.fstat(lock_fd)
            service_environment = (
                ("E3DC_BOOTSTRAP_ROOT", repo_dir),
                ("E3DC_BOOTSTRAP_RUNNER_ROOT", snapshot_root),
                ("E3DC_BOOTSTRAP_USER", install_user),
                ("E3DC_INSTALL_ROOT", repo_dir),
                ("PYTHONNOUSERSITE", "1"),
                ("PYTHONDONTWRITEBYTECODE", "1"),
                ("PYTHONUNBUFFERED", "1"),
                ("LC_ALL", "C.UTF-8"),
                ("LANG", "C.UTF-8"),
            )
            command = [
                str(systemd_run),
                "--system",
                "--quiet",
                "--wait",
                "--pipe",
                f"--unit={update_safety_contract.finalizer_unit}",
                "--service-type=exec",
                "--property=ExitType=main",
                "--property=KillMode=control-group",
                "--property=Restart=no",
                "--property=User=root",
                "--property=Group=root",
                "--property=DynamicUser=no",
                "--property=WorkingDirectory=/",
                "--property=UMask=0077",
                f"--property=RuntimeDirectory={update_safety_contract.runtime_directory}",
                "--property=RuntimeDirectoryMode=0700",
                "--property=RuntimeDirectoryPreserve=no",
                f"--property=RuntimeMaxSec={UPDATE_FINALIZER_RUNTIME_MAX_S}s",
                f"--property=TimeoutStopSec={UPDATE_FINALIZER_TIMEOUT_STOP_S}s",
                "--property=SendSIGKILL=yes",
                "--property=OOMPolicy=stop",
                *(f"--setenv={name}={value}" for name, value in service_environment),
                python,
                "-I",
                "-B",
                "-u",
                finalizer,
                "--systemd-finalizer-wrapper",
                "--install-path", repo_dir,
                "--execution-root", snapshot_root,
                "--expected-release-sha", target_commit,
                "--expected-install-user", install_user,
                "--update-safety-transaction", update_safety_contract.transaction_id,
                "--update-safety-receipt-sha256", update_safety_contract.receipt_sha256,
                "--update-safety-service-unit", update_safety_contract.finalizer_unit,
                "--update-safety-runtime-directory", update_safety_contract.runtime_directory,
                "--update-safety-token-path", update_safety_contract.token_path,
                "--expected-lock-device", str(lock_metadata.st_dev),
                "--expected-lock-inode", str(lock_metadata.st_ino),
                "--",
                *finalizer_args,
            ]
            managed_service_spawn_attempted = True
            result = _run_streaming_argv(
                command,
                timeout=UPDATE_FINALIZER_RUNTIME_MAX_S + 60,
                env={
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                    "LC_ALL": "C.UTF-8",
                    "LANG": "C.UTF-8",
                },
                pass_fds=(lock_fd,),
                stdin_fd=lock_fd,
                label="Verwalteter Target-Finalizer",
            )
            if not result or not result.get("success"):
                raise RuntimeError(
                    "Verwalteter Target-Finalizer fehlgeschlagen: "
                    + _combined_process_diagnostics(result or {})
                )
            _wait_managed_finalizer_inactive(update_safety_contract)
            managed_service_quiesced = True

        if result is None:
            raise RuntimeError("Target-Finalizer lieferte kein Ergebnis")
        marker = f"{TARGET_FINALIZER_SUCCESS} {target_commit} {target_tag}"
        marker_count = int(
            (result.get("stdout_line_counts") or {}).get(marker, 0)
        )
        if not result.get("success") or marker_count != 1:
            raise RuntimeError(
                "Target-Finalizer fehlgeschlagen: "
                + _combined_process_diagnostics(result)
            )
        if update_safety_contract is not None:
            committed = _read_update_safety_contract()
            if (
                committed is None
                or committed.state != "committed"
                or committed.transaction_id != update_safety_contract.transaction_id
                or not _same_update_safety_transaction_shape(
                    committed,
                    update_safety_contract,
                )
            ):
                raise RuntimeError(
                    "Finalizer-Erfolg besitzt nicht den committed Ersatz seines "
                    "ursprünglichen Sicherheitsreceipts"
                )
            # Ab diesem erfolgreichen durable Readback ist ein Altstand-Rollback
            # auch dann verboten, wenn unlink/fsync oder ein Signal das Receipt-
            # Cleanup unterbricht und ein nachfolgender Readback nichts mehr sieht.
            durable_committed_observed = committed
            _verify_update_safety_marker(committed, expected_present=False)
            _reload_and_verify_update_safety_dropins(committed, expected_present=False)
            if os.path.lexists(committed.token_path) or os.path.lexists(
                f"/run/{committed.runtime_directory}"
            ):
                raise RuntimeError("Finalizer-Lease blieb nach erfolgreichem Serviceende liegen")
            _remove_exact_update_safety_receipt(committed)
    except BaseException as original_error:
        if update_safety_contract is not None and managed_service_spawn_attempted:
            try:
                _kill_managed_finalizer_and_quiesce(
                    update_safety_contract,
                    repo_dir=repo_dir,
                    require_pending_contract=durable_committed_observed is None,
                )
                managed_service_quiesced = True
            except BaseException as quiesce_error:
                managed_service_quiesce_error = quiesce_error
                update_logger.critical(
                    "Verwaltete Finalizer-cgroup/Writer-Ruhe blieb unbewiesen: %s",
                    quiesce_error,
                )
            if durable_committed_observed is not None:
                try:
                    current = _read_update_safety_contract(allow_missing=True)
                except Exception as receipt_error:
                    update_logger.critical(
                        "Committed Update-Sicherheitsreceipt ist beim Cleanup unlesbar: %s",
                        receipt_error,
                    )
                    current = None
                if (
                    managed_service_quiesced
                    and current == durable_committed_observed
                ):
                    try:
                        _finish_committed_update_safety_cleanup(
                            durable_committed_observed,
                            remove_receipt=False,
                        )
                    except Exception as cleanup_error:
                        update_logger.critical(
                            "Committed Update-Gate blieb fail-closed stehen: %s",
                            cleanup_error,
                        )
                raise UpdateSafetyPostCommitError(
                    "Receipt-Cleanup brach nach durable committed ab"
                ) from original_error
            if not managed_service_quiesced:
                raise UpdateSafetyManagedServiceUnquiescedError(
                    "Verwaltete Finalizer-cgroup/Writer-Ruhe blieb nach "
                    "Kindprozessfehler unbewiesen; Recoverymutation ist gesperrt: "
                    f"{managed_service_quiesce_error}"
                ) from original_error
        raise
    finally:
        if (
            update_safety_contract is not None
            and managed_service_spawn_attempted
            and not managed_service_quiesced
        ):
            update_logger.critical(
                "Finalizer-Snapshot bleibt wegen unbewiesener Service-Ruhe erhalten: %s",
                snapshot_root,
            )
        else:
            try:
                if os.path.lexists(snapshot_root):
                    if os.lstat(snapshot_root).st_dev != os.lstat(repo_dir).st_dev:
                        raise RuntimeError(
                            "Target-Finalizer-Snapshot driftete vom Produkt-Dateisystem"
                        )
                _remove_target_execution_snapshot(snapshot_root)
            except BaseException as exc:
                if durable_committed_observed is not None:
                    raise UpdateSafetyPostCommitError(
                        "Snapshot-Cleanup brach nach durable committed ab"
                    ) from exc
                if not isinstance(exc, Exception):
                    raise
                update_logger.warning(
                    "Target-Ausführungssnapshot konnte nicht bereinigt werden: %s",
                    exc,
                )


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _target_metadata_detail(relative_path: str, metadata: os.stat_result) -> str:
    """Formatiert nur supportrelevante, nicht private Dateimetadaten."""
    return (
        f"{relative_path} "
        f"(uid={metadata.st_uid}, gid={metadata.st_gid}, "
        f"mode={stat.S_IMODE(metadata.st_mode):04o}, nlink={metadata.st_nlink})"
    )


def _read_bound_regular_file(path: str, maximum: int = 1024 * 1024) -> tuple[bytes, tuple[int, ...]]:
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
    entries = read_commit_entries(
        repo_dir,
        commit,
        (relative_path,),
        required_paths=(relative_path,),
        run_as_user=install_user,
        maximum_files=1,
        maximum_file_bytes=maximum,
        maximum_total_bytes=maximum,
    )
    payload, _mode = entries[relative_path]
    if len(payload) < 1:
        raise RuntimeError("Target-Blob fehlt oder besitzt eine unzulässige Größe")
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
    entries = read_commit_entries(
        repo_dir,
        commit,
        (relative_path,),
        required_paths=(relative_path,),
        run_as_user=install_user,
        maximum_files=1,
        maximum_file_bytes=TARGET_EXECUTION_SNAPSHOT_MAX_FILE_BYTES,
        maximum_total_bytes=TARGET_EXECUTION_SNAPSHOT_MAX_FILE_BYTES,
    )
    _payload, sealed_mode = entries[relative_path]
    return 0o755 if sealed_mode == 0o555 else 0o644


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
                raise RuntimeError(
                    "Target-Datei besitzt mehrere Hardlinks: "
                    + _target_metadata_detail(relative_path, before)
                )
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
            not stat.S_ISREG(current_path.st_mode)
            or current_path.st_nlink != 1
            or current_path.st_dev != before.st_dev
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
) -> tuple[int, ...]:
    target = os.path.join(os.path.abspath(repo_dir), relative_path)
    payload, identity = _read_bound_regular_file(target)
    metadata = os.lstat(target)
    try:
        account = pwd.getpwnam(str(install_user))
    except KeyError as exc:
        raise RuntimeError("Installationsbenutzer für Target-Bindung fehlt") from exc
    if metadata.st_uid not in (0, account.pw_uid) or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError(
            "Target-Datei besitzt keine vertrauenswürdigen Eigentümer-/Schreibrechte: "
            + _target_metadata_detail(relative_path, metadata)
        )
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


def _complete_dynamic_recovery_start(
    contract: UpdateSafetyContract,
    *,
    repo_dir: str,
    state: TransitionState,
) -> bool:
    """Öffnet das Recovery-Gate signalgeschützt und rearmed jeden Fehler."""

    signal_guard = _TerminalSignalGuard()
    signal_guard.install()
    signal_guard.arm()
    original_error: BaseException | None = None
    rearm_error: BaseException | None = None
    try:
        # Wegen BindsTo/After muss die dynamische Abhängigkeit vor dem ersten
        # Altstart vollständig aus der effektiven Unitansicht verschwinden.
        _clear_update_safety_marker(contract)
        _remove_update_safety_dropins(contract)
        if not _recover_pretransaction_service_state(state):
            raise RuntimeError("Recovery-Altstart blieb unvollständig")
        _remove_exact_update_safety_receipt(contract)
    except BaseException as exc:
        original_error = exc
        try:
            _enforce_update_safety_fail_closed(
                contract,
                repo_dir=repo_dir,
            )
        except BaseException as enforcement_exc:
            rearm_error = enforcement_exc
    requested_signum = signal_guard.requested_signum
    signal_guard.restore()
    if requested_signum is not None:
        if rearm_error is not None:
            update_logger.critical(
                "Signalabbruch und dynamisches Recovery-Re-Arm schlugen fehl: %s",
                rearm_error,
            )
        raise _DeferredParentSignal(requested_signum)
    if original_error is None:
        return True
    if rearm_error is not None:
        update_logger.critical(
            "Dynamisches Recovery-Re-Arm blieb nach Altstartfehler unvollständig: %s",
            rearm_error,
        )
    update_logger.error(
        "Dynamisches Recovery-Gate/Altstart schlug fehl: %s",
        original_error,
    )
    if not isinstance(original_error, Exception):
        raise original_error
    return False


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
    repo_recovery_contract: RepoRecoveryContract | None = None,
    backup_receipt: RecoveryBackupReceipt | None = None,
    bootblock_contract: (
        RecoveryBootblockContract | RecoveryBootblockPartialContract | None
    ) = None,
    update_safety_contract: UpdateSafetyContract | None = None,
    recovery_transaction_id: str | None = None,
) -> RecoveryTransitionResult:
    """Restore old Git/tree/persistent state and verify role/services after any mutation failure."""
    recovery_ok = True
    if (
        not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(
            str(recovery_transaction_id or "")
        )
        or (
            backup_receipt is not None
            and backup_receipt.transaction_id != recovery_transaction_id
        )
    ):
        update_logger.error("Recovery-Transaktions-ID ist nicht an den Receipt gebunden")
        return RecoveryTransitionResult(False, None)
    dynamic_safety = update_safety_contract is not None
    if dynamic_safety:
        try:
            update_safety_contract = _enforce_update_safety_fail_closed(
                update_safety_contract,
                repo_dir=repo_dir,
            )
        except Exception as exc:
            update_logger.error(
                "Dynamischer Recovery-Bootblock ist nicht vollständig beweisbar: %s",
                exc,
            )
            return RecoveryTransitionResult(False, None)
    else:
        try:
            initial_quiesced = bool(_stop_v4_services(V4_SERVICES))
        except Exception as exc:
            initial_quiesced = False
            update_logger.error(
                "Recovery-Sofortstop warf vor persistentem Bootblock einen Fehler: %s",
                exc,
            )
        if not initial_quiesced:
            update_logger.error(
                "Recovery-Sofortstop vor persistentem Bootblock ist nicht vollständig beweisbar"
            )
        try:
            bootblock_contract = _arm_persistent_recovery_bootblock(
                bootblock_contract,
                transaction_id=recovery_transaction_id,
            )
        except RecoveryBootblockArmError as exc:
            update_logger.error(
                "Recovery-Bootblock blieb nach fehlgeschlagenem Fresh-Arm "
                "nur über seinen eigenen Inodevertrag kontrollierbar: %s",
                exc,
            )
            return RecoveryTransitionResult(False, exc.contract)
        except Exception as exc:
            update_logger.error(
                "Recovery-Bootblock konnte nicht rebootfest aktiviert werden: %s",
                exc,
            )
            return RecoveryTransitionResult(False, None)
        try:
            final_quiesced = bool(_stop_v4_services(V4_SERVICES))
        except Exception as exc:
            final_quiesced = False
            update_logger.error(
                "Recovery-Stop-Endpass warf nach persistentem Bootblock einen Fehler: %s",
                exc,
            )
        if not initial_quiesced or not final_quiesced:
            update_logger.error("Recovery abgebrochen: Aktor-/Writer-Ruhe ist nicht beweisbar")
            return RecoveryTransitionResult(False, bootblock_contract)
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
        restore_manifest_guard = None
        if old_commit is not None:
            if repo_recovery_contract is None or backup_receipt is None:
                raise RuntimeError(
                    "Recovery besitzt keinen vorab eingefrorenen Repo-/Backup-Receipt"
                )
            _revalidate_recovery_backup_receipt(
                backup_receipt,
                repo_recovery_contract,
                backup_dir=backup_dir,
                repo_dir=repo_dir,
                expected_commit=old_commit,
                install_user=install_user,
            )

            def restore_manifest_guard(manifest):
                _guard_recovery_manifest(manifest, backup_receipt)

        restore_metadata_overrides = {}
        if backup_receipt is not None:
            storage_path = "/etc/systemd/system/e3dc-storage-manager.service"
            for path, _category, _digest, _size, mode, uid, gid in (
                backup_receipt.privileged_files
            ):
                if path == storage_path and (uid != 0 or gid != 0):
                    if mode != 0o644:
                        raise RuntimeError(
                            "Storage-Unit-Altbesitz besitzt keinen 0644-Receipt"
                        )
                    restore_metadata_overrides[path] = (0o644, 0, 0)

        def restored_payload_guard():
            if not hasattr(os, "geteuid") or os.geteuid() != 0:
                raise RuntimeError(
                    "Privilegierter Restore-Endguard darf ausschließlich Root ausführen"
                )
            if backup_receipt is not None:
                _verify_restored_privileged_files(
                    backup_receipt,
                    install_user,
                    allow_storage_owner_promotion=True,
                )

        restore_verified_backup(
            backup_dir,
            install_path=repo_dir,
            verified_manifest_guard=restore_manifest_guard,
            restored_payload_guard=(
                restored_payload_guard if backup_receipt is not None else None
            ),
            restore_metadata_overrides=restore_metadata_overrides,
        )
        _restore_recovery_surface(recovery_inventory, state)
        if backup_receipt is not None:
            _verify_restored_privileged_files(
                backup_receipt,
                install_user,
                allow_storage_owner_promotion=True,
            )
            if any(
                path == "/etc/systemd/system/e3dc-storage-manager.service"
                for path, _category, _digest, _size, _mode, _uid, _gid
                in backup_receipt.privileged_files
            ):
                storage_unit = "e3dc-storage-manager.service"
                if dynamic_safety:
                    expected_recovery_dropins = _update_safety_expected_dropins(
                        update_safety_contract,
                        selected_units=(storage_unit,),
                    )
                else:
                    recovery_identities = _validate_recovery_bootblock_contract(
                        bootblock_contract
                    )
                    recovery_path = _recovery_dropin_path(storage_unit)
                    recovery_dev, recovery_ino = recovery_identities[storage_unit]
                    expected_recovery_dropins = {
                        storage_unit: {
                            recovery_path: {
                                "bytes": RECOVERY_BOOTBLOCK_DROPIN_PAYLOAD,
                                "dev": recovery_dev,
                                "ino": recovery_ino,
                                "uid": 0,
                                "gid": 0,
                                "mode": 0o644,
                                "nlink": 1,
                                "size": len(RECOVERY_BOOTBLOCK_DROPIN_PAYLOAD),
                            }
                        }
                    }
                capture_systemd_service_bundle(
                    ("e3dc-storage-manager",),
                    expected_recovery_dropins=expected_recovery_dropins,
                )
        # Recovery installiert keine Units aus dem temporären Archivbaum. Die
        # verifizierte Sicherung stellt die alten Unitdateien wieder her; hier
        # werden ausschließlich Web- und Repo-Rechte am Zielbaum gehärtet.
        if not _fix_webroot_permissions():
            raise RuntimeError("Web-Programmrechte konnten nach Recovery nicht gehärtet werden")
        recovery_permission_args = {}
        if repo_recovery_contract is not None or backup_receipt is not None:
            recovery_permission_args = {
                "recovery_backup_dir": backup_dir,
                "recovery_repo_contract": repo_recovery_contract,
                "recovery_backup_receipt": backup_receipt,
            }
        if old_commit is not None:
            _secure_repo_permissions(
                repo_dir,
                install_user,
                expected_commit=old_commit,
                **recovery_permission_args,
            )
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
    if recovery_ok and backup_receipt is not None:
        try:
            _verify_restored_privileged_files(
                backup_receipt,
                install_user,
                allow_storage_owner_promotion=True,
            )
        except Exception as exc:
            update_logger.error(
                "Privilegierter Restore-Endvertrag fehlgeschlagen: %s",
                exc,
            )
            recovery_ok = False
    if not recovery_ok:
        return RecoveryTransitionResult(False, bootblock_contract)
    if dynamic_safety:
        if not _complete_dynamic_recovery_start(
            update_safety_contract,
            repo_dir=repo_dir,
            state=state,
        ):
            return RecoveryTransitionResult(False, None)
        return RecoveryTransitionResult(True, None)
    try:
        # Erst der vollständig verifizierte Datei-/Paket-Rücklauf darf das
        # atomare Startgate für den kontrollierten Service-Endtest öffnen.
        _clear_recovery_bootblock_marker(bootblock_contract)
    except Exception as exc:
        update_logger.error("Recovery-Bootblock konnte nicht kontrolliert geöffnet werden: %s", exc)
        enforcement = _enforce_fail_closed_after_recovery_failure(
            bootblock_contract,
            recovery_transaction_id=recovery_transaction_id,
        )
        bootblock_contract = enforcement.bootblock_contract
        return RecoveryTransitionResult(False, bootblock_contract)
    if not _recover_pretransaction_service_state(state):
        enforcement = _enforce_fail_closed_after_recovery_failure(
            bootblock_contract,
            recovery_transaction_id=recovery_transaction_id,
        )
        bootblock_contract = enforcement.bootblock_contract
        return RecoveryTransitionResult(False, bootblock_contract)
    try:
        _remove_persistent_recovery_bootblock(bootblock_contract)
    except Exception as exc:
        update_logger.error("Recovery-Bootblock konnte nach Endgate nicht entfernt werden: %s", exc)
        enforcement = _enforce_fail_closed_after_recovery_failure(
            bootblock_contract,
            recovery_transaction_id=recovery_transaction_id,
        )
        bootblock_contract = enforcement.bootblock_contract
        return RecoveryTransitionResult(False, bootblock_contract)
    return RecoveryTransitionResult(True, None)


def _recover_pretransaction_service_state(state: TransitionState) -> bool:
    """Stellt exakt den vor dem Stop gebundenen Aktivitätszustand wieder her."""

    if not state.preactive_units.issubset(state.preinstalled_units):
        update_logger.error(
            "Recovery-Preimage enthält aktive, aber nicht installierte Units"
        )
        return False
    try:
        _verify_transition_state(
            state,
            expect_legacy_config_missing=state.bootstrap_legacy_config,
        )
    except Exception as exc:
        update_logger.error(f"Recovery-Konfigurationsbindung fehlgeschlagen: {exc}")
        return False

    prior_units = sorted(
        unit for unit in state.preinstalled_units if unit != "e3dc.service"
    )
    if not prior_units and not state.bootstrap_legacy_config:
        # Ein normaler V4/V5-Bestand ohne gebundene Units ist kein
        # beweisbarer Rückkehrzustand.
        return False

    recovered = True
    for unit in prior_units:
        should_be_active = unit in state.preactive_units
        action = "start" if should_be_active else "stop"
        changed = run_command(
            f"sudo systemctl {action} {unit}",
            timeout=30,
        )
        status = _run_argv(["systemctl", "is-active", unit], timeout=10)
        activity = status.get("stdout", "").strip().lower()
        end_state_ok = _systemd_activity_readback_matches(
            status,
            should_be_active=should_be_active,
        )
        if not end_state_ok:
            update_logger.error(
                "Recovery-Dienstzustand weicht ab: "
                f"{unit} erwartet={'active' if should_be_active else 'inactive'}, "
                f"gefunden={activity or 'unlesbar'}, "
                f"Aktion={_command_result_diagnostic(changed)}"
            )
            recovered = False

    if state.bootstrap_legacy_config or "e3dc.service" in state.preinstalled_units:
        if not _restore_legacy_runtime_state(state):
            recovered = False

    # Ein später gestarteter Dienst kann über systemd-Abhängigkeiten eine
    # zuvor korrekt gestoppte Unit wieder aktivieren. Deshalb ist keine
    # unmittelbare Einzelprüfung ein hinreichender Recovery-Beweis: Erst ein
    # zweiter vollständiger Pass nach allen Stop-/Start-Aktionen bindet den
    # tatsächlich erreichten globalen Endzustand.
    for unit in prior_units:
        should_be_active = unit in state.preactive_units
        status = _run_argv(["systemctl", "is-active", unit], timeout=10)
        activity = status.get("stdout", "").strip().lower()
        end_state_ok = _systemd_activity_readback_matches(
            status,
            should_be_active=should_be_active,
        )
        if not end_state_ok:
            update_logger.error(
                "Globaler Recovery-Endzustand weicht ab: "
                f"{unit} erwartet={'active' if should_be_active else 'inactive'}, "
                f"gefunden={activity or 'unlesbar'}"
            )
            recovered = False
    return recovered


def _enforce_fail_closed_after_recovery_failure(
    bootblock_contract: (
        RecoveryBootblockContract | RecoveryBootblockPartialContract | None
    ) = None,
    *,
    recovery_transaction_id: str | None = None,
) -> RecoveryBootblockEnforcementResult:
    """Setzt rebootfestes Startgate, stoppt Writer und beweist beide Schranken."""

    blocked = False
    latest_contract = bootblock_contract
    try:
        initial_quiesced = bool(_stop_v4_services(V4_SERVICES))
    except Exception as exc:
        initial_quiesced = False
        update_logger.error(
            "Fail-closed-Sofortstop warf vor persistentem Bootblock einen Fehler: %s",
            exc,
        )
    if not initial_quiesced:
        update_logger.error(
            "Fail-closed-Sofortstop vor persistentem Bootblock ist nicht vollständig beweisbar"
        )
    try:
        latest_contract = _arm_persistent_recovery_bootblock(
            bootblock_contract,
            transaction_id=recovery_transaction_id,
        )
        blocked = True
    except RecoveryBootblockArmError as exc:
        latest_contract = exc.contract
        update_logger.error(
            "Erster Recovery-Bootblock-Arm benötigte seinen erhaltenen "
            "Inodevertrag: %s",
            exc,
        )
        try:
            latest_contract = _arm_persistent_recovery_bootblock(
                exc.contract,
                transaction_id=recovery_transaction_id,
            )
            blocked = True
        except RecoveryBootblockArmError as retry_exc:
            latest_contract = retry_exc.contract
            update_logger.error(
                "Persistenter Recovery-Bootblock blieb auch beim Retry "
                "partiell; neuester Inodevertrag wurde erhalten: %s",
                retry_exc,
            )
        except Exception as retry_exc:
            update_logger.error(
                "Persistenter Recovery-Bootblock ist auch mit eigenem "
                "Inodevertrag nicht beweisbar: %s",
                retry_exc,
            )
    except Exception as exc:
        update_logger.error("Persistenter Recovery-Bootblock ist nicht beweisbar: %s", exc)
    try:
        final_quiesced = bool(_stop_v4_services(V4_SERVICES))
    except Exception as exc:
        final_quiesced = False
        update_logger.error(
            "Fail-closed-Stop-Endpass warf nach persistentem Bootblock einen Fehler: %s",
            exc,
        )
    quiesced = initial_quiesced and final_quiesced
    if quiesced and blocked:
        print(
            "[!] Recovery blieb unvollständig; rebootfester systemd-Bootblock "
            "und Aktor-/Writer-Ruhe sind bewiesen."
        )
    elif quiesced:
        print(
            "[!] Recovery blieb unvollständig; die Writer sind gestoppt, der "
            "rebootfeste systemd-Bootblock ist jedoch nicht beweisbar."
        )
    else:
        print(
            "[!] Recovery, rebootfester Bootblock oder erneute Aktorruhe sind "
            "nicht vollständig beweisbar; die Watchdog-Sperre bleibt aktiv."
        )
    return RecoveryBootblockEnforcementResult(
        quiesced and blocked,
        latest_contract,
    )


def _regular_file_sha256(path: str) -> tuple[str, int]:
    """Hasht ausschließlich eine reguläre Datei ohne Symlink-Folge."""

    descriptor, metadata = _open_regular_file_nofollow(path)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest(), int(metadata.st_size)


def _bound_release_head_commit(
    repo_dir: str,
    install_user: str,
) -> str:
    """Bindet HEAD ausschließlich als vollständigen Commit-Hash."""

    result = _git_argv(
        repo_dir,
        install_user,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        timeout=10,
    )
    if not result.get("success"):
        raise RuntimeError(
            "Repository-HEAD ist nicht lesbar: "
            + _combined_process_diagnostics(result, maximum=800)
        )
    try:
        return _validate_full_commit(str(result.get("stdout") or "").strip())
    except ValueError as exc:
        raise RuntimeError("Repository-HEAD ist kein vollständiger Commit") from exc


def _tracked_release_file_contracts(
    repo_dir: str,
    install_user: str,
    *,
    target_commit: str | None = None,
) -> list[tuple[str, int, str]]:
    """Bindet Pfad, Modus und Blob-ID aus einem exakten Produkt-Commit."""

    commit = (
        _validate_full_commit(target_commit)
        if target_commit is not None
        else _bound_release_head_commit(repo_dir, install_user)
    )

    verified_entries = read_commit_entries(
        os.path.abspath(repo_dir),
        commit,
        (),
        include_all=True,
        run_as_user=install_user,
        maximum_files=TARGET_EXECUTION_SNAPSHOT_MAX_FILES,
        maximum_file_bytes=TARGET_EXECUTION_SNAPSHOT_MAX_FILE_BYTES,
        maximum_total_bytes=TARGET_EXECUTION_SNAPSHOT_MAX_TOTAL_BYTES,
    )
    root = os.path.abspath(repo_dir)
    entries: list[tuple[str, int, str]] = []
    for relative_path, (payload, sealed_mode) in sorted(verified_entries.items()):
        target = os.path.abspath(os.path.join(root, relative_path))
        if os.path.commonpath((root, target)) != root or target == root:
            raise RuntimeError("Getrackte Produktdatei verlässt das Repository")
        object_id = hashlib.new(
            "sha1",
            b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload,
            usedforsecurity=False,
        ).hexdigest()
        entries.append(
            (
                relative_path,
                0o755 if sealed_mode == 0o555 else 0o644,
                object_id,
            )
        )
    if not entries:
        raise RuntimeError("Git-Dateivertrag ist leer")
    return entries


def _tracked_release_file_modes(
    repo_dir: str,
    install_user: str,
) -> list[tuple[str, int]]:
    """Liest den kanonischen Modus jeder getrackten regulären Produktdatei."""

    return [
        (relative_path, mode)
        for relative_path, mode, _object_id in _tracked_release_file_contracts(
            repo_dir,
            install_user,
        )
    ]


def _assert_target_worktree_replaceable(
    repo_dir: str,
    install_user: str,
    target_commit: str,
) -> None:
    """Blockiert nur Dateitypen, die ein sicheres Zielkopieren verhindern.

    Besitzer, Gruppe, Modus und xattrs bekannter regulärer Produktdateien
    werden nach dem Backup normalisiert. Symlinks, Hardlinks und Spezialdateien
    dürfen dagegen nicht erst durch ``git reset`` berührt werden.
    """

    root = os.path.abspath(repo_dir)
    for relative_path, _mode, _object_id in _tracked_release_file_contracts(
        root,
        install_user,
        target_commit=_validate_full_commit(target_commit),
    ):
        parts = Path(relative_path).parts
        current = root
        parent_missing = False
        for component in parts[:-1]:
            current = os.path.join(current, component)
            if parent_missing or not os.path.lexists(current):
                parent_missing = True
                continue
            metadata = os.lstat(current)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(
                    f"Ziel-Elternpfad ist nicht sicher ersetzbar: {relative_path}"
                )
        target = os.path.join(root, relative_path)
        if parent_missing or not os.path.lexists(target):
            continue
        metadata = os.lstat(target)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise RuntimeError(
                f"Ziel-Dateipfad ist nicht sicher ersetzbar: {relative_path}"
            )


def _git_blob_oid_from_descriptor(
    descriptor: int,
    expected_size: int,
    expected_oid: str,
) -> str:
    """Berechnet eine Git-Blob-ID direkt aus dem bereits gebundenen FD."""

    object_id = str(expected_oid or "").strip().lower()
    algorithm = {40: "sha1", 64: "sha256"}.get(len(object_id))
    if (
        algorithm is None
        or not re.fullmatch(r"[0-9a-f]+", object_id)
        or int(expected_size) < 0
    ):
        raise RuntimeError("Git-Blob-Vertrag ist ungültig")
    digest = hashlib.new(algorithm)
    digest.update(f"blob {int(expected_size)}\0".encode("ascii"))
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = int(expected_size)
    while remaining:
        block = os.read(descriptor, min(1024 * 1024, remaining))
        if not block:
            raise RuntimeError(
                "Getrackte Produktdatei endete vor der gebundenen Größe"
            )
        digest.update(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise RuntimeError(
            "Getrackte Produktdatei überschreitet die gebundene Größe"
        )
    return digest.hexdigest()


def _append_directory_metadata_errors(
    *,
    directories,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    label: str,
    errors: list[str],
    maximum: int,
) -> None:
    """Prüft kanonische Verzeichnis-Metadaten ohne Symlink-Folge."""

    for directory in sorted({os.path.abspath(str(item)) for item in directories}):
        if len(errors) >= maximum:
            return
        try:
            metadata = os.lstat(directory)
        except OSError as exc:
            errors.append(f"{label} fehlt oder ist nicht lesbar: {directory}: {exc}")
            continue
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or metadata.st_gid != expected_gid
            or stat.S_IMODE(metadata.st_mode) != expected_mode
        ):
            errors.append(
                f"{label} besitzt abweichende Metadaten: {directory} "
                f"(uid={metadata.st_uid}, gid={metadata.st_gid}, "
                f"mode={stat.S_IMODE(metadata.st_mode):04o})"
            )


def _append_regular_metadata_error(
    *,
    path: str,
    relative_path: str,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    label: str,
    errors: list[str],
    maximum: int,
) -> None:
    """Prüft Owner, Gruppe, Hardlinks und Modus einer gebundenen Datei."""

    if len(errors) >= maximum:
        return
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        errors.append(f"{label} fehlt oder ist nicht lesbar: {relative_path}: {exc}")
        return
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != expected_uid
        or metadata.st_gid != expected_gid
        or stat.S_IMODE(metadata.st_mode) != expected_mode
    ):
        errors.append(
            f"{label} besitzt abweichende Metadaten: {relative_path} "
            f"(uid={metadata.st_uid}, gid={metadata.st_gid}, "
            f"mode={stat.S_IMODE(metadata.st_mode):04o}, "
            f"nlink={metadata.st_nlink})"
        )


def _same_release_service_errors(
    services,
    state: TransitionState,
    *,
    maximum: int = 12,
) -> list[str]:
    """Beweist Pflicht-/aktive Optionsdienste ohne deaktivierte Features hochzustufen."""

    errors: list[str] = []
    names: list[str] = []
    for raw_service in services or ():
        name = str(raw_service).strip().removesuffix(".service")
        if name and name not in names:
            names.append(name)
    if (
        "piguard.service" in state.preinstalled_units
        and "piguard" not in names
    ):
        names.append("piguard")
    for service in names:
        if len(errors) >= maximum:
            break
        if service == "e3dc":
            continue
        try:
            expected, reason = _service_expected(service, state)
        except Exception as exc:
            errors.append(f"{_unit_name(service)} ist nicht klassifizierbar: {exc}")
            continue
        if not expected:
            continue
        load_state, unit_file_state, active_state, result = _systemd_show_end_state(
            service,
            timeout_s=10,
        )
        if (
            not result.get("success")
            or (load_state, unit_file_state, active_state)
            != ("loaded", "enabled", "active")
        ):
            errors.append(
                f"{_unit_name(service)} besitzt keinen intakten Endzustand "
                f"(load={load_state or 'unlesbar'}, "
                f"enabled={unit_file_state or 'unlesbar'}, "
                f"active={active_state or 'unlesbar'}; {reason})"
            )
    return errors[:maximum]


def _same_release_integrity_errors(
    repo_dir: str,
    install_user: str,
    *,
    web_root: str = "/var/www/html",
    maximum: int = 12,
) -> list[str]:
    """Prüft eng nur Releasequellen und ihre kanonische Webprojektion."""

    errors: list[str] = []
    status = _git_argv(
        repo_dir,
        install_user,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
        timeout=30,
    )
    if not status.get("success"):
        return [
            "getrackter Releasezustand ist nicht lesbar: "
            + _combined_process_diagnostics(status, maximum=800)
        ]
    tracked_changes = [
        line.rstrip()
        for line in str(status.get("stdout") or "").splitlines()
        if line.strip()
    ]
    if tracked_changes:
        errors.extend(
            f"getrackte Produktdatei weicht ab: {line}"
            for line in tracked_changes[:maximum]
        )
        if len(tracked_changes) > maximum:
            errors.append(
                f"{len(tracked_changes) - maximum} weitere getrackte Abweichungen"
            )
        return errors[:maximum]

    try:
        account = pwd.getpwnam(str(install_user))
        web_group = grp.getgrnam("www-data")
        tracked_entries = _tracked_release_file_modes(repo_dir, install_user)
    except Exception as exc:
        return [f"Release-Metadatenvertrag ist nicht lesbar: {exc}"]

    repo_root = os.path.abspath(repo_dir)
    repo_directories = {repo_root}
    for relative_path, expected_mode in tracked_entries:
        if len(errors) >= maximum:
            return errors[:maximum]
        source = os.path.abspath(os.path.join(repo_root, relative_path))
        current = os.path.dirname(source)
        while current != repo_root:
            repo_directories.add(current)
            current = os.path.dirname(current)
        _append_regular_metadata_error(
            path=source,
            relative_path=relative_path,
            expected_uid=account.pw_uid,
            expected_gid=account.pw_gid,
            expected_mode=expected_mode,
            label="Getrackte Produktdatei",
            errors=errors,
            maximum=maximum,
        )
    _append_directory_metadata_errors(
        directories=repo_directories,
        expected_uid=account.pw_uid,
        expected_gid=account.pw_gid,
        expected_mode=0o755,
        label="Produktverzeichnis",
        errors=errors,
        maximum=maximum,
    )
    if errors:
        return errors[:maximum]

    html_root = os.path.join(repo_dir, "html")
    try:
        _assert_tree_no_symlinks(html_root)
    except Exception as exc:
        return [f"Webquelle ist nicht kanonisch: {exc}"]

    excluded = {"data", "logs", "ramdisk", "tmp"}
    projected_sources: list[tuple[str, str]] = []
    for directory, dirnames, filenames in os.walk(
        html_root,
        topdown=True,
        followlinks=False,
    ):
        dirnames[:] = [name for name in dirnames if name not in excluded]
        for filename in filenames:
            source = os.path.join(directory, filename)
            relative = os.path.relpath(source, html_root)
            projected_sources.append((source, os.path.join(web_root, relative)))
    for name in ("VERSION", "CHANGELOG.md", "UPDATE_POLICY.json"):
        projected_sources.append(
            (os.path.join(repo_dir, name), os.path.join(web_root, name))
        )

    web_directories = {os.path.abspath(web_root)}
    for _source, target in projected_sources:
        current = os.path.dirname(os.path.abspath(target))
        while os.path.commonpath((os.path.abspath(web_root), current)) == os.path.abspath(
            web_root
        ):
            web_directories.add(current)
            if current == os.path.abspath(web_root):
                break
            current = os.path.dirname(current)
    _append_directory_metadata_errors(
        directories=web_directories,
        expected_uid=account.pw_uid,
        expected_gid=web_group.gr_gid,
        expected_mode=0o755,
        label="Web-Programmverzeichnis",
        errors=errors,
        maximum=maximum,
    )

    for source, target in projected_sources:
        if len(errors) >= maximum:
            break
        relative_target = os.path.relpath(target, web_root)
        _append_regular_metadata_error(
            path=target,
            relative_path=relative_target,
            expected_uid=account.pw_uid,
            expected_gid=web_group.gr_gid,
            expected_mode=0o644,
            label="Web-Programmdatei",
            errors=errors,
            maximum=maximum,
        )
        if len(errors) >= maximum:
            break
        try:
            source_hash, source_size = _regular_file_sha256(source)
        except Exception as exc:
            errors.append(
                f"Releasequelle {os.path.relpath(source, repo_dir)} ist nicht sicher lesbar: {exc}"
            )
            continue
        try:
            target_hash, target_size = _regular_file_sha256(target)
        except FileNotFoundError:
            errors.append(
                f"Webprojektion fehlt: {relative_target}"
            )
            continue
        except Exception as exc:
            errors.append(
                f"Webprojektion {relative_target} ist nicht sicher lesbar: {exc}"
            )
            continue
        if source_size != target_size or source_hash != target_hash:
            errors.append(
                f"Webprojektion weicht ab: {relative_target}"
            )
    return errors[:maximum]


def _prepare_backup_collection(
    repo_dir: str,
    *,
    explicit_download_bootstrap: bool,
) -> str:
    """Bindet einen vorhandenen Root oder erzeugt nur den fehlenden Standardroot."""

    backup_root = configured_backup_root(repo_dir)
    if (
        explicit_download_bootstrap
        and Path(backup_root) == DEFAULT_BACKUP_ROOT
        and not os.path.lexists(backup_root)
    ):
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise RuntimeError(
                "Fehlender Backup-Root darf nur im Root-Download-Bootstrap "
                "initialisiert werden"
            )
        backup_root = ensure_external_backup_root(backup_root, repo_dir)
    return str(validate_existing_backup_root(backup_root, repo_dir))


def _execute_update_transaction(
    headless: bool = False,
    target_ref: str | None = None,
    target_install_path: str | None = None,
    expected_release_sha: str | None = None,
    expected_ha_role: str | None = None,
    *,
    preverified_target_commit: str | None = None,
    preverified_target_tag: str | None = None,
    transaction_repo_dir: str | None = None,
    sealed_target_updater: bool = False,
    reinstall_current: bool = False,
):
    """Transactional stable update, SHA-bound rollback, or unrelated-history bootstrap."""
    if _is_docker_environment():
        print(
            f"[!] {UPDATE_EXTERNAL_ACTION_REQUIRED}: Docker-Umgebung erkannt; "
            "im Container wurde kein Release-Wechsel ausgeführt."
        )
        print("    Bitte auf dem Docker-Host im Compose-Verzeichnis ausführen:")
        print("    (")
        print("      set -euo pipefail")
        print("      if [ -f ./docker_compose_update.py ]; then")
        print("        E3DC_DOCKER_HELPER=./docker_compose_update.py")
        print("      elif [ -f ./Installer/docker_compose_update.py ]; then")
        print("        E3DC_DOCKER_HELPER=./Installer/docker_compose_update.py")
        print("      else")
        print("        echo 'docker_compose_update.py fehlt; aktuellen Release-Verwaltungsbaum bereitstellen.' >&2")
        print("        exit 2")
        print("      fi")
        print('      sudo python3 "$E3DC_DOCKER_HELPER" --compose-dir . --sudo')
        print("      sudo docker compose logs --tail=80 e3dc-control")
        print("    )")
        return False

    try:
        target_tag = _normalize_release_tag(target_ref) if target_ref else None
        expected_sha = _validate_full_commit(expected_release_sha) if expected_release_sha else None
        verified_commit = (
            _validate_full_commit(preverified_target_commit)
            if preverified_target_commit
            else None
        )
        verified_tag = (
            _normalize_release_tag(preverified_target_tag)
            if preverified_target_tag
            else None
        )
    except ValueError as exc:
        print(f"[!] {exc}")
        return False

    if bool(verified_commit) != bool(verified_tag):
        print("[!] Vorverifizierter Ziel-Commit und Ziel-Tag müssen gemeinsam gebunden sein.")
        return False
    if reinstall_current and (target_install_path or target_ref or not verified_commit):
        print(
            "[!] Neuinstallation des aktuellen Releases verlangt den "
            "versiegelten Ziel-Updater ohne Bootstrap-/Rollbackziel."
        )
        return False
    if target_install_path and verified_commit:
        print("[!] Erstinstallations-Bootstrap und Ziel-Updater-Handoff dürfen nicht vermischt werden.")
        return False
    if sealed_target_updater and (
        target_install_path
        or target_tag
        or reinstall_current
        or not verified_commit
        or not verified_tag
        or not transaction_repo_dir
        or not expected_sha
        or not expected_ha_role
    ):
        print(
            "[!] Rollenanker-Autorität verlangt den vollständig "
            "versiegelten nativen Ziel-Updater."
        )
        return False
    if target_install_path and not target_tag:
        print("[!] Bootstrap verlangt einen expliziten Release-Tag.")
        return False
    if target_install_path and (not expected_sha or not expected_ha_role):
        print("[!] Bootstrap verlangt --expected-release-sha und --expected-ha-role.")
        return False
    transition_name = (
        "release-bootstrap"
        if target_install_path
        else (
            "release-rollback"
            if target_tag
            else ("release-reinstall" if reinstall_current else "self-update")
        )
    )
    if not sys.stdout.isatty():
        headless = True

    try:
        _assert_no_existing_recovery_bootblock()
        repo_dir = (
            _validate_bootstrap_install_path(target_install_path)
            if target_install_path
            else _validate_bootstrap_install_path(
                transaction_repo_dir or INSTALL_PATH
            )
        )
        old_commit, bootstrap_rebuild_git = _bind_bootstrap_git_prestate(
            repo_dir,
            explicit_bootstrap=bool(target_install_path),
        )
        bootstrap_without_git = old_commit is None
        state = _capture_transition_state(
            expected_role=expected_ha_role,
            allow_missing_config=bool(target_install_path and bootstrap_without_git),
        )
        role_anchor_needed = _explicit_bootstrap_role_anchor_needed(
            state,
            target_install_path=target_install_path,
            sealed_target_updater=sealed_target_updater,
        )
        inventory = _capture_install_inventory(repo_dir)
        recovery_inventory = _capture_recovery_surface(state)
    except Exception as exc:
        print(f"[!] Release-Preflight fehlgeschlagen: {exc}")
        update_logger.error(f"Release-Preflight fehlgeschlagen: {exc}")
        return False
    recovery_transaction_id = secrets.token_hex(32)

    print("\n" + "=" * 60)
    print("  E3DC-CONTROL " + transition_name.upper())
    print("=" * 60)
    print(f"    Repository       : {repo_dir}")
    print(f"    Ausgangs-SHA     : {old_commit or 'ZIP/V3'}")
    print(f"    Eingefrorene Rolle: {state.ha_role}")
    if role_anchor_needed:
        print("    Rollenanker      : fehlt; wird nach Backup und Aktorruhe einmalig erstellt")
    if target_tag:
        print(f"    Ziel-Release     : {target_tag}")
    if expected_sha:
        print(f"    Erwartete SHA    : {expected_sha}")

    if not headless:
        answer = input("\nGeprueften Release-Wechsel jetzt starten? (j/n): ").strip().lower()
        if answer != "j":
            print("[i] Release-Wechsel abgebrochen.")
            return True

    repo_recovery_contract = None
    backup_receipt = None
    if old_commit is not None:
        try:
            preflight_install_user = get_install_user()
            repo_recovery_contract = _capture_repo_recovery_contract(
                repo_dir,
                preflight_install_user,
                old_commit,
            )
        except Exception as exc:
            print(f"[!] Recovery-Preimage konnte nicht sicher eingefroren werden: {exc}")
            update_logger.error(f"Recovery-Preimage-Preflight fehlgeschlagen: {exc}")
            return False

    try:
        # Bestehende Flächen bleiben strikt read-only validiert. Ausschließlich
        # der explizite Root-Download-Bootstrap darf den wirklich fehlenden
        # kanonischen Root einmalig anlegen und markieren.
        backup_collection = _prepare_backup_collection(
            repo_dir,
            explicit_download_bootstrap=bool(target_install_path),
        )
        backup_collection_descriptor, _backup_collection_chain = (
            _open_root_receipt_directory_chain(backup_collection)
        )
        try:
            collection_metadata = os.fstat(backup_collection_descriptor)
            if stat.S_IMODE(collection_metadata.st_mode) != 0o700:
                raise RuntimeError("Backup-Root besitzt nicht den Modus 0700")
        finally:
            os.close(backup_collection_descriptor)
    except Exception as exc:
        print(f"[!] Root-kontrollierter Backup-Pfad fehlt: {exc}")
        update_logger.error("Backup-Root-Preflight fehlgeschlagen: %s", exc)
        return False

    _enable_watchdog_update_pause(transition_name)
    print("\n[->] Erstelle vollstaendiges externes, verifiziertes Backup...")

    def freeze_backup_receipt(backup_path, verified_manifest):
        nonlocal backup_receipt
        if repo_recovery_contract is None or backup_receipt is not None:
            raise RuntimeError("Backup-Receipt-Callback besitzt keinen eindeutigen Zustand")
        backup_receipt = _capture_recovery_backup_receipt(
            backup_path,
            verified_manifest,
            repo_recovery_contract,
            recovery_transaction_id,
        )

    try:
        backup_dir = backup_current_version(
            install_path=repo_dir,
            verified_pre_chown_callback=(
                freeze_backup_receipt
                if repo_recovery_contract is not None
                else None
            ),
        )
    except Exception as exc:
        backup_dir = None
        update_logger.error(f"Backup vor Release-Wechsel fehlgeschlagen: {exc}")
    if not backup_dir or (
        repo_recovery_contract is not None and backup_receipt is None
    ):
        print("[!] Backup fehlgeschlagen; Release-Wechsel hart abgebrochen.")
        _set_watchdog_update_pause(False, reason=transition_name)
        return False

    install_user = None
    sealed_storage_payloads = None
    sealed_storage_expected_dropins = None
    storage_unit_promoted = False
    storage_promotion_state_uncertain = False
    if repo_recovery_contract is not None and not sealed_target_updater:
        try:
            install_user = get_install_user()
            if repo_recovery_contract.install_user != install_user:
                raise RuntimeError(
                    "Installationsbenutzer driftete seit dem Recovery-Preflight"
                )
            try:
                storage_unit_promoted = bool(
                    _migrate_approved_storage_manager_unit_owner(
                        _approved_storage_manager_unit_payloads(),
                        install_user=install_user,
                    )
                )
            except StorageUnitMigrationError as promotion_exc:
                if not promotion_exc.root_unit_committed:
                    storage_promotion_state_uncertain = True
                    raise
                # Der atomare Namensersatz ist bereits exakt root-gebunden;
                # nur sein nachgelagerter Helper-Postcheck scheiterte. Der
                # folgende daemon-reload plus Bundle-Readback entscheidet
                # deshalb weiterhin fail-closed über den effektiven Vertrag.
                storage_unit_promoted = True
            if storage_unit_promoted:
                reload_result = _run_argv(
                    ["systemctl", "daemon-reload"],
                    timeout=30,
                    env={**os.environ, "LC_ALL": "C", "LANG": "C"},
                )
                if (
                    not reload_result.get("success")
                    or reload_result.get("timed_out")
                    or str(reload_result.get("stderr") or "")
                    or int(reload_result.get("returncode", -1)) != 0
                ):
                    raise RuntimeError(
                        "systemd daemon-reload nach Storage-Unitmigration "
                        "fehlgeschlagen: "
                        + _combined_process_diagnostics(
                            reload_result,
                            maximum=800,
                        )
                    )
                capture_systemd_service_bundle(("e3dc-storage-manager",))
        except Exception as exc:
            print(f"[!] Frühe Storage-Unitmigration fehlgeschlagen: {exc}")
            update_logger.error("Frühe Storage-Unitmigration fehlgeschlagen: %s", exc)
            if storage_unit_promoted:
                # Der benannte Inode ist bereits root-kontrolliert. Ein
                # synchron erkannter Folgefehler darf daher den normalen
                # rebootfesten Fail-closed-Pfad sicher daemon-reloaden.
                _enforce_fail_closed_after_recovery_failure(
                    recovery_transaction_id=recovery_transaction_id,
                )
            elif storage_promotion_state_uncertain:
                # Kein daemon-reload auf einem nicht mehr eindeutig
                # gebundenen Namen. Stop-Aufrufe verwenden nur den letzten
                # systemd-Cache; die Update-Pause bleibt zur manuellen
                # Recovery bestehen.
                _stop_v4_services(V4_SERVICES)
                update_logger.critical(
                    "Storage-Unitzustand ist nach atomarem Ersatz unklar; "
                    "kein daemon-reload und kein Dienststart"
                )
            else:
                # Pre-Rename-Fehler: kein Produktinode wurde verändert und
                # kein Dienst gestoppt. Der bestehende systemd-Cache bleibt
                # unangetastet.
                _set_watchdog_update_pause(False, reason=transition_name)
            return False

    update_safety_contract = None
    if sealed_target_updater:
        try:
            if (
                repo_recovery_contract is None
                or backup_receipt is None
                or not verified_commit
                or not verified_tag
            ):
                raise RuntimeError(
                    "Versiegelter Ziel-Updater besitzt keinen vollständigen Backup-/Zielvertrag"
                )
            update_safety_contract = _prepare_update_safety_contract(
                transaction_id=recovery_transaction_id,
                target_commit=verified_commit,
                target_tag=verified_tag,
                role=state.ha_role,
                backup_receipt=backup_receipt,
            )
            update_safety_contract = _arm_update_safety_contract(
                update_safety_contract
            )
            if not _stop_v4_services(V4_SERVICES):
                raise RuntimeError("Sichere Aktorruhe konnte unter Bootblock nicht nachgewiesen werden")
            install_user = get_install_user()
            if repo_recovery_contract.install_user != install_user:
                raise RuntimeError(
                    "Installationsbenutzer driftete seit dem Recovery-Preflight"
                )
            sealed_storage_payloads = _approved_storage_manager_unit_payloads()
            sealed_storage_expected_dropins = _update_safety_expected_dropins(
                update_safety_contract,
                selected_units=("e3dc-storage-manager.service",),
            )
            _validate_update_safety_contract(
                update_safety_contract,
                expected_state="pending",
            )
            _verify_update_safety_marker(
                update_safety_contract,
                expected_present=True,
            )
            _reload_and_verify_update_safety_dropins(
                update_safety_contract,
                expected_present=True,
            )
            _revalidate_recovery_backup_receipt(
                backup_receipt,
                repo_recovery_contract,
                backup_dir=backup_dir,
                repo_dir=repo_dir,
                expected_commit=old_commit,
                install_user=install_user,
            )
            _verify_restored_privileged_files(
                backup_receipt,
                install_user,
            )
            _verify_repo_recovery_prestate(
                repo_dir,
                install_user,
                repo_recovery_contract,
            )
            _verify_transition_state(state)
            _assert_strict_update_writer_quiescence(
                repo_dir=repo_dir,
                transaction_id=recovery_transaction_id,
            )
        except BaseException as exc:
            print(f"[!] Vor-Mutations-Sicherheitsgate fehlgeschlagen: {exc}")
            update_logger.critical(
                "Versiegeltes Vor-Mutations-Sicherheitsgate blieb fail-closed: %s",
                exc,
            )
            return False

    # Dieser exakte Aufruf bleibt für nicht versiegelte Altpfade als
    # statisch prüfbarer Aktorruhevertrag erhalten.
    if not sealed_target_updater:
        quiescence_error = None
        if not _stop_v4_services(V4_SERVICES):
            quiescence_error = RuntimeError(
                "Sichere Aktorruhe konnte nicht nachgewiesen werden"
            )
        else:
            try:
                _assert_strict_update_writer_quiescence(
                    repo_dir=repo_dir,
                    transaction_id=recovery_transaction_id,
                )
            except Exception as exc:
                quiescence_error = exc
        if quiescence_error is not None:
            print(f"[!] Sichere Aktorruhe konnte nicht nachgewiesen werden: {quiescence_error}")
            # Ein fehlgeschlagenes Rogue-/Writer-Gate darf niemals durch das
            # Starten des alten Unitbestands kompensiert werden. Erst eine
            # bewiesene globale Ruhe könnte einen kontrollierten Altstart
            # autorisieren; in diesem Fehlerzweig ist sie gerade nicht belegt.
            _enforce_fail_closed_after_recovery_failure(
                recovery_transaction_id=recovery_transaction_id,
            )
            return False

    git_created = False
    mutated = storage_unit_promoted
    bootblock_contract = None
    role_anchor_created = False
    target_commit = None
    package_transaction = None
    packages_mutated = False
    try:
        if not sealed_target_updater:
            install_user = install_user or get_install_user()
        if (
            repo_recovery_contract is not None
            and repo_recovery_contract.install_user != install_user
        ):
            raise RuntimeError(
                "Installationsbenutzer driftete seit dem Recovery-Preflight"
            )
        if sealed_target_updater:
            if update_safety_contract is None:
                raise RuntimeError("Versiegelter Ziel-Updater verlor seinen Bootblockvertrag")
            mutated = True
            try:
                storage_unit_promoted = bool(
                    _migrate_approved_storage_manager_unit_owner(
                        sealed_storage_payloads,
                        install_user=install_user,
                        expected_recovery_dropins=sealed_storage_expected_dropins[
                            "e3dc-storage-manager.service"
                        ],
                    )
                )
            except StorageUnitMigrationError as promotion_exc:
                if not promotion_exc.root_unit_committed:
                    storage_promotion_state_uncertain = True
                    raise
                storage_unit_promoted = True
            if storage_unit_promoted:
                reload_result = _run_argv(
                    ["systemctl", "daemon-reload"],
                    timeout=30,
                    env={**os.environ, "LC_ALL": "C", "LANG": "C"},
                )
                if (
                    not reload_result.get("success")
                    or reload_result.get("timed_out")
                    or str(reload_result.get("stderr") or "")
                    or int(reload_result.get("returncode", -1)) != 0
                ):
                    raise RuntimeError(
                        "systemd daemon-reload nach versiegelter Storage-Unitmigration fehlgeschlagen: "
                        + _combined_process_diagnostics(reload_result, maximum=800)
                    )
                capture_systemd_service_bundle(
                    ("e3dc-storage-manager",),
                    expected_recovery_dropins=sealed_storage_expected_dropins,
                )
            _validate_update_safety_contract(
                update_safety_contract,
                expected_state="pending",
            )
            _verify_update_safety_marker(
                update_safety_contract,
                expected_present=True,
            )
            _reload_and_verify_update_safety_dropins(
                update_safety_contract,
                expected_present=True,
            )
            _assert_strict_update_writer_quiescence(
                repo_dir=repo_dir,
                transaction_id=recovery_transaction_id,
            )
        if role_anchor_needed:
            # Ab hier besitzt jede Mutation ein verifiziertes Backup und eine
            # bestätigte Aktorruhe. Auch der einmalige Rollenanker fällt damit
            # bei jedem Folgefehler unter den vollständigen Rückweg.
            if sealed_target_updater and (
                repo_recovery_contract is None or backup_receipt is None
            ):
                raise RuntimeError(
                    "Versiegelter Rollenanker besitzt keinen Root-Receipt-gebundenen "
                    "Recovery-Vertrag"
                )
            mutated = True
            role_anchor_created = _bind_explicit_bootstrap_role_anchor(
                state,
                target_install_path=target_install_path,
                sealed_target_updater=sealed_target_updater,
            )
            if role_anchor_created is not True:
                raise RuntimeError(
                    "Fehlender Instanzrollen-Anker wurde nicht eindeutig gebunden"
                )
        mutated = True
        cleanup_pycache(repo_dir)
        if bootstrap_without_git:
            mutated = True
            git_created = True
            git_path = os.path.join(repo_dir, ".git")
            if bootstrap_rebuild_git and os.path.lexists(git_path):
                print(
                    "  [i] Unbrauchbare Alt-Git-Metadaten werden nach dem "
                    "verifizierten Backup neu aufgebaut."
                )
                _remove_tree_nofollow(git_path)
            # Nur die frisch erzeugte Metadatenwurzel gehört von Anfang an dem
            # gebundenen Installationsbenutzer. Alle folgenden Projektionen
            # dürfen weiter als Root laufen, damit beliebige Altmodi im
            # Produktbaum das Update nach dem verifizierten Backup nicht
            # blockieren. Der Ziel-Finalizer kann .git dadurch anschließend
            # als genau diesen Nicht-Root-Besitz erneut binden.
            _initialize_bootstrap_git(repo_dir, install_user)
            mutated = True
            remote_add = _git_argv(repo_dir, install_user, "remote", "add", "origin", SELFUPDATE_REPO, timeout=15)
            if not remote_add["success"]:
                raise RuntimeError("Git-Origin konnte nicht gesetzt werden: " + remote_add["stderr"].strip())

        _require_bound_origin(repo_dir, install_user)

        if verified_commit:
            target_commit = verified_commit
            # Der äußere Handoff hat Commit und Stable-Tag bereits gemeinsam
            # gebunden. Auch eine ausdrücklich gewünschte Neuinstallation
            # eines älteren, weiterhin veröffentlichten Stands muss deshalb
            # gegen genau diesen Tag geprüft werden – niemals erneut gegen ein
            # inzwischen weitergelaufenes origin/main.
            bound_ref = f"refs/tags/{verified_tag}"
            resolved = _resolve_git_commit(repo_dir, bound_ref, install_user)
            if not resolved or not _exact_commit_matches(resolved, target_commit):
                raise RuntimeError(
                    "Vorverifizierter Ziel-Commit ist nicht mehr an den erwarteten Git-Ref gebunden"
                )
        else:
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
        if verified_tag and bound_target_tag != verified_tag:
            raise RuntimeError(
                "Ziel-Policy driftete gegenüber dem gebundenen Ziel-Updater-Tag"
            )
        _validated_restart_services(policy, state)
        watchdog_runtime_required = _watchdog_runtime_venv_required(state)
        package_transaction = _capture_package_transaction(
            policy,
            install_user,
            # Auch ein normaler Self-Update darf einen wirklich fehlenden,
            # policygebundenen Benutzer-venv erstmals erzeugen. Capture
            # erlaubt dies nur bei freigegebenem python3-venv und exakt
            # absentem kanonischem Zielpfad; jeder belegte Fremdpfad stoppt.
            allow_missing_venv=True,
            # Der Watchdog benötigt seinen Interpreter auch dann, wenn der
            # Release selbst keine Python-Pakete ändert. Diese Laufzeitbindung
            # darf deshalb nicht aus der pip-Policy abgeleitet werden.
            require_runtime_venv=watchdog_runtime_required,
        )

        _assert_target_worktree_replaceable(
            repo_dir,
            install_user,
            target_commit,
        )

        mutated = True
        reset = _git_argv(repo_dir, install_user, "reset", "--hard", target_commit, timeout=120)
        if not reset["success"]:
            raise RuntimeError("git reset --hard fehlgeschlagen: " + reset["stderr"].strip())
        new_commit = _resolve_git_commit(repo_dir, "HEAD", install_user)
        if not new_commit or not _exact_commit_matches(target_commit, new_commit):
            raise RuntimeError("HEAD stimmt nicht exakt mit dem freigegebenen Ziel-SHA ueberein")

        mutated = True
        _normalize_target_finalizer_files(
            repo_dir=repo_dir,
            target_commit=target_commit,
            install_user=install_user,
        )

        mutated = True
        packages_mutated = True
        _invoke_target_finalizer(
            repo_dir=repo_dir,
            target_commit=target_commit,
            target_tag=bound_target_tag,
            state=state,
            package_transaction=package_transaction,
            update_safety_contract=update_safety_contract,
            explicit_download_bootstrap=bool(target_install_path),
        )
    except BaseException as exc:
        print(f"[!] {transition_name} fehlgeschlagen: {exc}")
        update_logger.error(f"{transition_name} fehlgeschlagen: {exc}")
        if isinstance(exc, UpdateSafetyPostCommitError):
            update_logger.critical(
                "Zielstand ist committed; Altstand-Rollback bleibt ausdrücklich gesperrt"
            )
            return False
        if isinstance(exc, UpdateSafetyManagedServiceUnquiescedError):
            update_logger.critical(
                "Managed Finalizer-/Writer-Ruhe ist unbewiesen; "
                "jede Recoverymutation bleibt ausdrücklich gesperrt"
            )
            return False
        recovered = False
        if mutated:
            recovered, bootblock_contract = _recover_failed_transition(
                repo_dir=repo_dir,
                install_user=install_user,
                backup_dir=backup_dir,
                old_commit=old_commit,
                git_created=git_created,
                inventory=inventory,
                recovery_inventory=recovery_inventory,
                state=state,
                package_transaction=package_transaction if packages_mutated else None,
                repo_recovery_contract=repo_recovery_contract,
                backup_receipt=backup_receipt,
                bootblock_contract=bootblock_contract,
                update_safety_contract=update_safety_contract,
                recovery_transaction_id=recovery_transaction_id,
            )
        else:
            recovered = _recover_pretransaction_service_state(state)
        if recovered:
            print("[OK] Ausgangszustand wurde automatisch und verifiziert wiederhergestellt.")
            _set_watchdog_update_pause(False, reason=transition_name)
        else:
            if update_safety_contract is not None:
                try:
                    _enforce_update_safety_fail_closed(
                        update_safety_contract,
                        repo_dir=repo_dir,
                    )
                except Exception as enforcement_exc:
                    update_logger.critical(
                        "Dynamischer Update-Bootblock ist nicht vollständig beweisbar: %s",
                        enforcement_exc,
                    )
            else:
                _enforce_fail_closed_after_recovery_failure(
                    bootblock_contract,
                    recovery_transaction_id=recovery_transaction_id,
                )
        return False

    _set_watchdog_update_pause(False, reason=transition_name)
    print(f"\n[OK] {transition_name} auf {target_commit} abgeschlossen.")
    update_logger.info(f"E3DC-Control {transition_name} abgeschlossen: {old_commit} -> {target_commit}")
    log_task_completed(
        "E3DC-Control " + transition_name,
        details=f"{old_commit or 'ZIP/V3'} -> {target_commit}",
    )
    return True


def _read_handoff_role(config_path: str = HA_CONFIG_PATH) -> str:
    """Bindet vor dem Handoff ausschließlich die vorhandene Anlagenrolle."""

    config, _raw = _read_json_nofollow(config_path)
    role = str(config.get("ha_mode") or "").strip().lower()
    if role not in VALID_HA_ROLES:
        raise RuntimeError("HA-/Shadow-Rolle fehlt oder ist ungültig")
    return role


def _bound_handoff_source_commit(repo_dir: str) -> str | None:
    """Bindet den alten Updater entweder direkt oder an seinen Web-Snapshot."""

    product_root = os.path.realpath(repo_dir)
    module_root = os.path.realpath(os.path.dirname(INSTALLER_DIR))
    if module_root == product_root:
        return None

    bootstrap_root = str(os.environ.get("E3DC_BOOTSTRAP_ROOT") or "").strip()
    bootstrap_runner = str(
        os.environ.get("E3DC_BOOTSTRAP_RUNNER_ROOT") or ""
    ).strip()
    source_commit = str(
        os.environ.get("E3DC_BOOTSTRAP_SOURCE_COMMIT") or ""
    ).strip().lower()
    if (
        not bootstrap_root
        or not bootstrap_runner
        or os.path.realpath(bootstrap_root) != product_root
        or os.path.realpath(bootstrap_runner) != module_root
        or not FULL_COMMIT_RE.fullmatch(source_commit)
    ):
        raise RuntimeError(
            "Alt-Updater besitzt keinen vollständig gebundenen "
            "Web-Ausführungssnapshot"
        )
    return source_commit


def _handoff_to_verified_target_updater(
    *,
    headless: bool,
    target_ref: str | None,
    expected_release_sha: str | None,
    reinstall_current: bool = False,
) -> bool:
    """Lädt nur Zielobjekte und startet danach ausschließlich den Ziel-Updater."""

    try:
        requested_tag = _normalize_release_tag(target_ref) if target_ref else None
        expected_sha = (
            _validate_full_commit(expected_release_sha)
            if expected_release_sha
            else None
        )
        repo_dir = _validate_bootstrap_install_path(INSTALL_PATH)
        snapshot_source_commit = _bound_handoff_source_commit(repo_dir)
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            raise RuntimeError(
                "Installation ohne Git darf nur über den expliziten Erstinstallations-Bootstrap wechseln"
            )
        install_user = get_install_user()
        expected_role = _read_handoff_role()
        old_commit = _resolve_git_commit(repo_dir, "HEAD", install_user)
        if not old_commit:
            raise RuntimeError("Aktueller HEAD konnte nicht als volle Commit-SHA verifiziert werden")
        if snapshot_source_commit and not _exact_commit_matches(
            snapshot_source_commit,
            old_commit,
        ):
            raise RuntimeError(
                "Web-Ausführungssnapshot stimmt nicht mit dem aktuellen HEAD überein"
            )
        _require_bound_origin(repo_dir, install_user)

        effective_target_tag = requested_tag
        if reinstall_current:
            current_policy = _read_policy_from_commit(
                repo_dir,
                old_commit,
                install_user,
            )
            effective_target_tag = _normalize_release_tag(
                str(current_policy.get("stable_release") or "")
            )
        target_commit = _fetch_target_commit(
            repo_dir,
            install_user,
            effective_target_tag,
        )
        if expected_sha and not _exact_commit_matches(expected_sha, target_commit):
            raise RuntimeError(
                f"Ziel-SHA weicht von der expliziten Freigabe ab: {target_commit} != {expected_sha}"
            )
        if requested_tag and not _target_tag_authorized(
            requested_tag,
            policy_repo=repo_dir,
            target_commit=target_commit,
            expected_release_sha=expected_sha,
            install_user=install_user,
        ):
            raise RuntimeError("Release-Tag ist nicht durch exakte Policy-/SHA-Bindung autorisiert")
        if reinstall_current and not _exact_commit_matches(old_commit, target_commit):
            raise RuntimeError(
                "Die aktuelle Version ist nicht mehr exakt durch ihren "
                "veröffentlichten Stable-Tag gebunden"
            )

        policy = _read_policy_from_commit(repo_dir, target_commit, install_user)
        bound_target_tag = _validate_target_release(
            policy,
            repo_dir,
            target_commit,
            effective_target_tag,
            install_user,
        )
    except Exception as exc:
        print(f"[!] Ziel-Updater-Preflight fehlgeschlagen: {exc}")
        update_logger.error(f"Ziel-Updater-Preflight fehlgeschlagen: {exc}")
        return False

    print("\n" + "=" * 60)
    print("  E3DC-CONTROL ZIEL-UPDATER-HANDOFF")
    print("=" * 60)
    print(f"    Repository       : {repo_dir}")
    print(f"    Ausgangs-SHA     : {old_commit}")
    print(f"    Ziel-Release     : {bound_target_tag}")
    print(f"    Ziel-SHA         : {target_commit}")
    print(f"    Gebundene Rolle  : {expected_role}")
    if reinstall_current:
        print("    Betriebsart      : Aktuelle Version ausdrücklich neu installieren")
    print("    Ausführung       : versiegelter Ziel-Updater auf Produkt-Dateisystem")
    if not headless and sys.stdout.isatty():
        answer = input("\nGeprüften Release-Wechsel jetzt starten? (j/n): ").strip().lower()
        if answer != "j":
            print("[i] Release-Wechsel abgebrochen.")
            return True

    try:
        outcome = _invoke_verified_target_updater(
            repo_dir=repo_dir,
            target_commit=target_commit,
            target_tag=bound_target_tag,
            requested_target_tag=requested_tag,
            expected_role=expected_role,
            install_user=install_user,
            reinstall_current=reinstall_current,
        )
    except Exception as exc:
        print(f"[!] Ziel-Updater-Handoff fehlgeschlagen: {exc}")
        update_logger.error(f"Ziel-Updater-Handoff fehlgeschlagen: {exc}")
        return False
    if outcome == UPDATE_ALREADY_CURRENT:
        return UPDATE_ALREADY_CURRENT
    print(f"\n[OK] Ziel-Updater hat den Release-Wechsel auf {target_commit} abgeschlossen.")
    return True


def execute_verified_target_update(
    *,
    repo_dir: str,
    target_commit: str,
    target_tag: str,
    expected_role: str,
    requested_target_tag: str | None = None,
    reinstall_current: bool = False,
) -> bool:
    """Eintritt des versiegelten Ziel-Updaters in die eigentliche Transaktion."""

    _required_update_lock_fd()
    product_root = _validate_bootstrap_install_path(repo_dir)
    snapshot_root = os.path.dirname(INSTALLER_DIR)
    if (
        os.path.realpath(os.environ.get("E3DC_BOOTSTRAP_ROOT", "")) != product_root
        or os.path.realpath(os.environ.get("E3DC_BOOTSTRAP_RUNNER_ROOT", "")) != snapshot_root
        or os.path.realpath(INSTALL_PATH) != product_root
        or snapshot_root == product_root
        or os.lstat(snapshot_root).st_dev != os.lstat(product_root).st_dev
    ):
        raise RuntimeError(
            "Ziel-Updater besitzt keinen getrennten, dateisystemgebundenen Ausführungssnapshot"
        )
    commit = _validate_full_commit(target_commit)
    tag = _normalize_release_tag(target_tag)
    role = str(expected_role or "").strip().lower()
    if role not in VALID_HA_ROLES:
        raise RuntimeError("Ziel-Updater besitzt keine gültige Rollenbindung")
    requested = _normalize_release_tag(requested_target_tag) if requested_target_tag else None
    if reinstall_current and requested:
        raise RuntimeError(
            "Explizite Neuinstallation und Release-Rollback dürfen nicht vermischt werden"
        )

    # Diese Prüfung läuft bereits mit dem Code des Ziel-Commits. Die Altstufe
    # bindet nur Commit, Stable-Tag und Rolle und interpretiert keine
    # zielversionsspezifischen Dienst-, Paket- oder Löschverträge.
    install_user = get_install_user()
    current_commit = get_current_commit(product_root)
    if not current_commit:
        raise RuntimeError("Aktueller HEAD konnte im Ziel-Updater nicht gebunden werden")
    policy = _read_policy_from_commit(product_root, commit, install_user)
    bound_tag = _validate_target_release(
        policy,
        product_root,
        commit,
        requested,
        install_user,
    )
    if bound_tag != tag:
        raise RuntimeError("Ziel-Tag driftete gegenüber dem versiegelten Updater-Handoff")
    strict_forward_update = False
    if (
        requested is None
        and not reinstall_current
        and not _exact_commit_matches(current_commit, commit)
    ):
        _require_strict_forward_update_ancestry(
            product_root,
            install_user,
            current_commit,
            commit,
        )
        strict_forward_update = True
    transition_state = _capture_transition_state(expected_role=role)
    # Zielversionsspezifische Aktionslisten werden ausschließlich hier, also
    # bereits mit dem versiegelten Code des Ziel-Releases, interpretiert.
    # Ungültige Dienstverträge stoppen damit noch vor Backup und Aktorruhe.
    restart_services = _validated_restart_services(policy, transition_state)

    if reinstall_current and not _exact_commit_matches(current_commit, commit):
        raise RuntimeError(
            "Explizite Neuinstallation ist nur für den aktuell laufenden Release-Commit zulässig"
        )
    if (
        not reinstall_current
        and requested is None
        and _exact_commit_matches(current_commit, commit)
    ):
        integrity_errors = _same_release_integrity_errors(
            product_root,
            install_user,
        )
        if len(integrity_errors) < 12:
            integrity_errors.extend(
                _same_release_service_errors(
                    restart_services,
                    transition_state,
                    maximum=12 - len(integrity_errors),
                )
            )
        if integrity_errors:
            print("[!] REPAIR_REQUIRED: Die aktuelle Releaseprojektion weicht ab.")
            for error in integrity_errors:
                print(f"    - {error}")
            print(
                "    Keine Produkt- oder Webdatei und kein Dienstzustand wurde verändert. "
                "Starte ausdrücklich --reinstall-current, um diese Version nach "
                "verifiziertem Backup neu einzuspielen."
            )
            return False
        print(f"[OK] Du bist auf dem neuesten Stand: {commit}.")
        print(
            "    Keine Produkt- oder Webdatei und kein Dienstzustand wurde verändert. "
            "Kein Backup und kein Dienststopp erforderlich."
        )
        return UPDATE_ALREADY_CURRENT

    return _execute_update_transaction(
        headless=True,
        target_ref=requested,
        expected_release_sha=commit,
        expected_ha_role=role,
        preverified_target_commit=commit,
        preverified_target_tag=tag,
        transaction_repo_dir=product_root,
        sealed_target_updater=strict_forward_update,
        reinstall_current=reinstall_current,
    )


def _update_e3dc_locked(
    headless: bool = False,
    target_ref: str | None = None,
    target_install_path: str | None = None,
    expected_release_sha: str | None = None,
    expected_ha_role: str | None = None,
    reinstall_current: bool = False,
):
    """Startet Updates zweistufig; echte Erstinstallationen bleiben explizit getrennt."""

    if reinstall_current and (
        target_ref
        or target_install_path
        or expected_release_sha
        or expected_ha_role
    ):
        print(
            "[!] --reinstall-current darf nicht mit Bootstrap-, Rollback- "
            "oder Ziel-SHA-Optionen kombiniert werden."
        )
        return False
    if target_install_path:
        return _execute_update_transaction(
            headless=headless,
            target_ref=target_ref,
            target_install_path=target_install_path,
            expected_release_sha=expected_release_sha,
            expected_ha_role=expected_ha_role,
        )
    if expected_ha_role:
        print("[!] Eine erwartete Rolle ist ohne expliziten Erstinstallationspfad unzulässig.")
        return False
    if _is_docker_environment():
        return _execute_update_transaction(
            headless=headless,
            target_ref=target_ref,
            expected_release_sha=expected_release_sha,
        )
    return _handoff_to_verified_target_updater(
        headless=headless,
        target_ref=target_ref,
        expected_release_sha=expected_release_sha,
        reinstall_current=reinstall_current,
    )


def update_e3dc(
    headless: bool = False,
    target_ref: str | None = None,
    target_install_path: str | None = None,
    expected_release_sha: str | None = None,
    expected_ha_role: str | None = None,
    reinstall_current: bool = False,
):
    """Serialisiert jeden Release-Wechsel über einen kernelgebundenen Lock."""

    try:
        lock_fd, lock_owned = _acquire_or_inherit_update_lock()
    except UpdateTransactionBusy as exc:
        print(f"[!] {exc}. Es wurde keine Release-Transaktion gestartet.")
        return False
    except Exception as exc:
        print(f"[!] Systemweiter Update-Lock konnte nicht sicher gebunden werden: {exc}")
        return False

    previous_lock_env = os.environ.get(UPDATE_LOCK_ENV)
    os.environ[UPDATE_LOCK_ENV] = str(lock_fd)
    try:
        return _update_e3dc_locked(
            headless=headless,
            target_ref=target_ref,
            target_install_path=target_install_path,
            expected_release_sha=expected_release_sha,
            expected_ha_role=expected_ha_role,
            reinstall_current=reinstall_current,
        )
    finally:
        if previous_lock_env is None:
            os.environ.pop(UPDATE_LOCK_ENV, None)
        else:
            os.environ[UPDATE_LOCK_ENV] = previous_lock_env
        if lock_owned:
            # Kinder teilen dieselbe offene Dateibeschreibung. Der Kernel hält
            # den Lock, bis auch der letzte geerbte Deskriptor nach belegtem
            # Prozessende geschlossen ist; daher nur den Owner-FD schließen.
            os.close(lock_fd)


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


def _assert_root_controlled_directory_chain(path: Path) -> None:
    """Bindet jede kanonische Verzeichniskomponente an Root und sichere Modi."""

    directory = Path(path)
    if not directory.is_absolute() or directory.resolve(strict=True) != directory:
        raise RuntimeError("Systempfad enthält eine nicht kanonische Verzeichniskomponente")
    current = Path(directory.anchor)
    for component in directory.parts[1:]:
        current /= component
        metadata = current.lstat()
        if (
            current.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError(
                f"Systempfad besitzt eine unsichere Verzeichniskomponente: {current}"
            )


def _trusted_system_python() -> str:
    """Liefert ausschließlich den festen, root-kontrollierten Systeminterpreter."""

    candidate = Path("/usr/bin/python3")
    try:
        _assert_root_controlled_directory_chain(candidate.parent)
        link_info = candidate.lstat()
        target = candidate.resolve(strict=True)
        _assert_root_controlled_directory_chain(target.parent)
        target_info = target.stat()
    except OSError as exc:
        raise RuntimeError("Fester System-Python ist nicht verfügbar") from exc
    if not (stat.S_ISLNK(link_info.st_mode) or stat.S_ISREG(link_info.st_mode)):
        raise RuntimeError("Fester System-Python ist weder Link noch reguläre Datei")
    if link_info.st_uid != 0:
        raise RuntimeError("System-Python-Pfad besitzt unsichere Metadaten")
    if (
        stat.S_ISREG(link_info.st_mode)
        and link_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError("System-Python-Datei ist gruppen- oder weltbeschreibbar")
    if target.parent != Path("/usr/bin"):
        raise RuntimeError("System-Python-Ziel liegt nicht im festen Systempfad")
    if (
        not stat.S_ISREG(target_info.st_mode)
        or target_info.st_uid != 0
        or target_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not target_info.st_mode & 0o111
    ):
        raise RuntimeError("System-Python-Ziel besitzt unsichere Metadaten")
    descriptor = os.open(
        str(target),
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
            opened.st_gid,
        ) != (
            target_info.st_dev,
            target_info.st_ino,
            target_info.st_mode,
            target_info.st_uid,
            target_info.st_gid,
        ):
            raise RuntimeError("System-Python-Ziel driftete während der Bindung")
    finally:
        os.close(descriptor)
    return str(target)


def select_wrapper_python(action: str) -> str:
    """Bindet Release-Einstiege an System-Python, Wartung optional ans venv."""
    normalized = str(action or "").strip().lower()
    if normalized not in {
        "check",
        "fix_permissions",
        "update_e3dc",
        "reinstall_current",
        "install_release",
    }:
        raise RuntimeError("Wrapper-Python darf für diese Aktion nicht gewählt werden")
    if normalized in {"update_e3dc", "reinstall_current", "install_release"}:
        return _trusted_system_python()
    install_user = get_install_user()
    venv_python = _find_venv_python(install_user)
    if venv_python:
        return venv_python
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
    return start_installation_or_update(allow_first_install=True)


# Im Docker-Container kein Update-Menueintrag
if not _is_docker_environment():
    register_command('11', 'Installation / Update', update_menu, sort_order=11)
