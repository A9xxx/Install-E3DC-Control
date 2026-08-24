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
from dataclasses import dataclass, replace
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
from .backup import (
    backup_current_version,
    create_quiesced_overlay,
    estimate_full_backup_size,
    estimate_quiesced_overlay_size,
    restore_quiesced_overlay,
    restore_verified_backup,
)
from .backup_integrity import (
    BACKUP_ESTIMATE_DIRECTORY_OVERHEAD_BYTES,
    BACKUP_ESTIMATE_FILE_OVERHEAD_BYTES,
    BACKUP_ESTIMATE_FIXED_OVERHEAD_BYTES,
    BACKUP_ESTIMATE_SOURCE_OVERHEAD_BYTES,
    DEFAULT_BACKUP_ROOT,
    MANIFEST_NAME,
    QuiescedOverlayRestoreGuard,
    QUIESCED_OVERLAY_KIND,
    SYSTEM_BACKUP_KIND,
    _open_directory_nofollow,
    _open_regular_file_nofollow,
    configured_backup_root,
    ensure_external_backup_root,
    validate_existing_backup_root,
    verify_backup,
)
from .update_offline_preflight import (
    OfflinePackageReceipt,
    OfflinePreflightError,
    build_offline_install_commands,
    cleanup_offline_cache,
    cleanup_offline_package_artifacts,
    create_preparation_plan,
    execute_preparation,
    materialize_wheel_mirror,
    parse_offline_package_receipt,
    require_conservative_free_space,
    serialize_offline_package_receipt,
    verify_preparation,
)
from . import update_recovery_context as recovery_context_codec
from . import update_recovery_journal as recovery_journal
from . import update_legacy_safety as legacy_safety_codec
from . import update_prejournal_construction as prejournal_codec
from . import update_recovery_surface as recovery_surface_codec
from .update_recovery_surface import (
    CrontabInventory,
    RootFileInventory,
    capture_crontab_preimages,
    capture_crontab_restore_guard,
    capture_root_file_preimages,
    capture_root_file_restore_guard,
    restore_crontab_preimages,
    restore_root_file_preimages,
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
    project_download_bootstrap_metadata,
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
RECOVERY_BOOTBLOCK_DROPIN_MARKER = "# E3DC_RECOVERY_BOOTBLOCK_V2"
RECOVERY_BOOTBLOCK_TRANSACTION_RE = re.compile(r"[0-9a-f]{64}\Z")
UPDATE_SAFETY_RECEIPT_SCHEMA = "e3dc_update_safety_v2"
UPDATE_SAFETY_RECEIPT_NAME = "transaction.json"
QUIESCED_OVERLAY_RECEIPT_NAME = "quiesced-overlay.json"
QUIESCED_OVERLAY_RECEIPT_SCHEMA = "e3dc_quiesced_overlay_v1"
UPDATE_FINALIZER_UNIT_PREFIX = "e3dc-update-finalizer-"
UPDATE_FINALIZER_RUNTIME_SUFFIX = "-runtime"
UPDATE_FINALIZER_TOKEN_NAME = "start.token"
UPDATE_FINALIZER_RUNTIME_MAX_S = 35 * 60
UPDATE_FINALIZER_TIMEOUT_STOP_S = 15
UPDATE_FINALIZER_TERMINAL_STABLE_READS = 3
UPDATE_FINALIZER_TERMINAL_STABLE_INTERVAL_S = 0.2

FULL_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
PACKAGE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\Z")
PREPARED_PACKAGE_STATE_SCHEMA = "e3dc_prepared_package_state_v1"
PACKAGE_TRANSACTION_STATE_SCHEMA = "e3dc_package_transaction_state_v1"
PREPARED_PACKAGE_RECEIPT_SCHEMA = "e3dc_prepared_package_receipt_v1"
PREPARED_PACKAGE_RECEIPT_NAME = "prepared-packages.json"
PREPARED_PACKAGE_RECEIPT_MAX_BYTES = 2 * 1024 * 1024
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
UPDATE_DISPATCHER = "/usr/local/sbin/e3dc-web-update-launcher"
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
UPDATE_RUNTIME_APT_PACKAGES = ("rsync",)
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
    "Installer/secure_file_transaction.py",
    "Installer/update.py",
    "Installer/update_legacy_forward.py",
    "Installer/update_legacy_safety.py",
    "Installer/update_offline_preflight.py",
    "Installer/update_prejournal_construction.py",
    "Installer/update_recovery_context.py",
    "Installer/update_recovery_journal.py",
    "Installer/update_recovery_surface.py",
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


class ActionableUpdateAbort(RuntimeError):
    """Ein sicherer Abbruch mit genau einer ausführbaren nächsten Aktion."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        system_state: str,
        target: str,
        solution: str,
    ):
        self.code = str(code).strip()
        self.detail = str(detail).strip()
        self.system_state = str(system_state).strip()
        self.target = str(target).strip()
        self.solution = str(solution).strip()
        if (
            not re.fullmatch(r"E3DC-UPD-[A-Z0-9-]+", self.code)
            or not self.detail
            or not self.system_state
            or not self.target
            or not self.solution
            or any(
                "\n" in value or "\r" in value
                for value in (
                    self.code,
                    self.detail,
                    self.system_state,
                    self.target,
                    self.solution,
                )
            )
        ):
            raise ValueError("Strukturierter Updateabbruch ist unvollständig")
        super().__init__(self.detail)


def _print_actionable_update_abort(error: ActionableUpdateAbort) -> None:
    print(f"[ABBRUCH] {error.code}")
    print(f"Was ist passiert: {error.detail}")
    print(f"Systemzustand: {error.system_state}")
    print(f"Ziel: {error.target}")
    print(f"Lösung: {error.solution}")


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
    apache_was_active: bool
    apache_unit_file_state: str


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
    root_file_preimages: RootFileInventory | None = None
    crontab_preimages: CrontabInventory | None = None


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
class QuiescedOverlayReceipt:
    transaction_id: str
    overlay_dir: str
    overlay_dev: int
    overlay_ino: int
    parent_dev: int
    parent_ino: int
    backup_id: str
    manifest_sha256: str
    install_root: str
    full_backup_dir: str
    full_backup_dev: int
    full_backup_ino: int
    full_backup_id: str
    full_backup_manifest_sha256: str
    receipt_dev: int
    receipt_ino: int
    receipt_sha256: str


def _canonical_quiesced_overlay_receipt_bytes(record: dict) -> bytes:
    """Serialisiert ausschließlich den eng typisierten Overlay-Rückfallbeleg."""

    try:
        payload = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeError("Quiesced-Overlay-Receipt ist nicht kanonisch") from exc
    if len(payload) > 64 * 1024:
        raise RuntimeError("Quiesced-Overlay-Receipt ist unplausibel groß")
    return payload


@dataclass(frozen=True)
class RecoveryBootblockContract:
    units: tuple[str, ...]
    created_directories: tuple[str, ...]
    transaction_id: str
    dropin_identities: tuple[tuple[str, int, int], ...]

    @property
    def finalizer_unit(self) -> str:
        return _update_safety_names(self.transaction_id)[0]

    @property
    def runtime_directory(self) -> str:
        return _update_safety_names(self.transaction_id)[1]

    @property
    def token_path(self) -> str:
        return _update_safety_names(self.transaction_id)[2]


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
    apache_completion_required: bool = False
    apache_available: bool = False
    apache_was_active: bool = False
    apache_unit_file_state: str = "absent"


@dataclass(frozen=True)
class RecoveryTransitionResult:
    recovered: bool
    bootblock_contract: (
        RecoveryBootblockContract | RecoveryBootblockPartialContract | None
    )
    recovery_journal_contract: (
        recovery_journal.RecoveryJournalContract | None
    ) = None

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
    apt_before: tuple[tuple[str, str], ...]
    pip_before: tuple[tuple[str, str], ...]
    venv_python: str | None
    install_user: str
    apt_requested: tuple[str, ...]
    pip_requested: tuple[str, ...]
    apt_candidate_packages: tuple[str, ...] = ()
    venv_path: str | None = None
    venv_existed: bool = True
    runtime_venv_required: bool = False


@dataclass(frozen=True)
class PreparedPackageState:
    transaction_id: str
    offline_receipt_json: str
    offline_receipt_sha256: str
    apt_after: tuple[tuple[str, str], ...]
    pip_after: tuple[tuple[str, str], ...]
    venv_python: str | None
    install_user: str
    apt_requested: tuple[str, ...]
    pip_requested: tuple[str, ...]


@dataclass(frozen=True)
class PreparedPackageReceipt:
    """Rebootfester Pre-/Postzustand genau einer Offline-Pakettransaktion."""

    state: str
    transaction_id: str
    install_root: str
    full_backup_id: str
    payload_sha256: str
    package_transaction: PackageTransactionState
    prepared_state: PreparedPackageState | None
    receipt_path: str
    receipt_dev: int
    receipt_ino: int
    receipt_sha256: str
    target_commit: str = ""
    target_tag: str = ""
    role: str = ""
    apache_completion_required: bool = False
    apache_available: bool = False
    apache_was_active: bool = False
    apache_unit_file_state: str = "absent"
    static_recovery_contract_json: str = ""


@dataclass(frozen=True)
class PackageRecoveryReceipt:
    """Nur der cacheunabhängige Rücklaufabschnitt eines Paket-Receipts."""

    state: str
    transaction_id: str
    install_root: str
    full_backup_id: str
    payload_sha256: str
    package_transaction: PackageTransactionState
    receipt_path: str
    receipt_dev: int
    receipt_ino: int
    receipt_sha256: str
    target_commit: str = ""
    target_tag: str = ""
    role: str = ""
    apache_completion_required: bool = False
    apache_available: bool = False
    apache_was_active: bool = False
    apache_unit_file_state: str = "absent"
    static_recovery_contract_json: str = ""


class PreparedPackageReceiptTransitionError(RuntimeError):
    """Bewahrt den exakt gebundenen Prestate an der atomaren Replace-Grenze."""

    def __init__(
        self,
        message: str,
        recovery_receipt: PreparedPackageReceipt | PackageRecoveryReceipt,
    ):
        if not isinstance(
            recovery_receipt,
            (PreparedPackageReceipt, PackageRecoveryReceipt),
        ):
            raise TypeError("Paket-Receipt-Transition besitzt keinen Recovery-Vertrag")
        self.recovery_receipt = recovery_receipt
        self.package_transaction = recovery_receipt.package_transaction
        super().__init__(message)


@dataclass(frozen=True)
class TargetReleaseSpaceEstimate:
    """Commitgebundener zusätzlicher Speicherbedarf der Zielprojektion."""

    product_tree_bytes: int
    web_projection_bytes: int
    finalizer_snapshot_bytes: int


@dataclass(frozen=True)
class PersistentRecoveryBundle:
    """Alle rebootfesten Begleitverträge genau einer Update-Transaktion."""

    journal: recovery_journal.RecoveryJournalContract
    # In einer laufenden Transaktion sind alle drei Belege zwingend vorhanden.
    # Nach durable ``committed`` beziehungsweise ``rolled_back`` darf ein
    # Prozessabbruch jedoch zwischen zwei exakt gebundenen Unlinks liegen. Der
    # nächste Lauf muss diesen terminalen Cleanup fortsetzen können, ohne einen
    # bereits entfernten Begleiter künstlich wieder zu verlangen.
    context: recovery_context_codec.RecoveryContextContract | None
    surface: recovery_surface_codec.PersistedRecoverySurfaceReceipt | None
    systemd: recovery_surface_codec.PersistedSystemdRecoveryReceipt | None


@dataclass(frozen=True)
class ReconstructedRecoveryTransaction:
    """Vollständig aus rebootfesten Belegen rekonstruierte Transaktion."""

    bundle: PersistentRecoveryBundle
    transition_state: TransitionState | None
    install_inventory: frozenset[str] | None
    recovery_inventory: RecoverySurfaceInventory | None
    repo_contract: RepoRecoveryContract | None
    backup_receipt: RecoveryBackupReceipt | None
    package_receipt: PreparedPackageReceipt | PackageRecoveryReceipt | None
    package_transaction: PackageTransactionState | None
    update_safety_contract: UpdateSafetyContract | None
    static_bootblock_contract: RecoveryBootblockContract | None
    overlay_receipt: QuiescedOverlayReceipt | None
    offline_receipt: OfflinePackageReceipt | None


@dataclass
class UpdateFilesystemDemand:
    """Noch nicht belegter Bedarf eines einzelnen realen Dateisystems."""

    device: int
    representative_path: str
    labels: list[str]
    payload_bytes: int = 0
    backup_bytes: int = 0
    working_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return self.payload_bytes + self.backup_bytes + self.working_bytes


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


def _git_argv(
    repo_dir: str,
    install_user: str,
    *args: str,
    timeout: int = 30,
    root_authority: bool = False,
) -> dict:
    # Der Updater läuft als Root und projiziert anschließend den kanonischen
    # Zielbesitzer. Git wird deshalb im Root-Prozess nicht künstlich auf die
    # möglicherweise durch historische chmod/chown-Zustände ausgesperrte
    # Altidentität abgesenkt. Die isolierte Git-Umgebung deaktiviert weiterhin
    # Hooks, fremde Konfiguration, Credentials, Replace-Refs und unsichere
    # Protokolle. Nicht privilegierte Diagnoseaufrufe bleiben beim gebundenen
    # Installationsbenutzer.
    if not isinstance(root_authority, bool):
        raise ValueError("Git-Root-Autorität muss boolesch sein")
    if root_authority and (
        not hasattr(os, "geteuid") or os.geteuid() != 0
    ):
        raise RuntimeError(
            "Explizite Bootstrap-Git-Autorität verlangt einen Root-Prozess"
        )
    git_user = None if (root_authority and hasattr(os, "geteuid") and os.geteuid() == 0) else install_user
    return _run_argv(
        isolated_git_command(repo_dir, *args, run_as_user=git_user),
        timeout=timeout,
    )


def _root_git_call_kwargs(enabled: bool) -> dict[str, bool]:
    """Lässt normale Git-Aufrufsignaturen bytegenau unverändert."""

    if not isinstance(enabled, bool):
        raise ValueError("Git-Root-Autorität muss boolesch sein")
    return {"root_authority": True} if enabled else {}


def _initialize_bootstrap_git(repo_dir: str, install_user: str) -> None:
    """Erzeugt die frische Git-Wurzel unter durchgehender Root-Autorität."""

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("Bootstrap-Git-Init darf ausschließlich Root ausführen")
    try:
        account = pwd.getpwnam(str(install_user))
    except KeyError as exc:
        raise RuntimeError("Gebundener Installationsbenutzer fehlt lokal") from exc
    if account.pw_uid == 0:
        raise RuntimeError("Installationsbenutzer darf nicht Root sein")

    result = _run_argv(
        [
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


def _persistent_recovery_expected_dropins(
    contract: RecoveryBootblockContract,
    *,
    selected_units=(),
) -> dict[str, dict[str, dict[str, object]]]:
    """Projiziert den gebundenen statischen Bootblock als Service-Readbackvertrag."""

    identities = _validate_recovery_bootblock_contract(contract)
    selected = tuple(str(unit) for unit in selected_units) or contract.units
    if not set(selected).issubset(contract.units):
        raise RuntimeError("Recovery-Drop-in-Auswahl enthält eine fremde Unit")
    payload = _render_recovery_bootblock_dropin(contract.transaction_id)
    result: dict[str, dict[str, dict[str, object]]] = {}
    for unit in selected:
        device, inode = identities[unit]
        path = _recovery_dropin_path(unit)
        result[unit] = {
            path: {
                "bytes": payload,
                "dev": device,
                "ino": inode,
                "uid": 0,
                "gid": 0,
                "mode": 0o644,
                "nlink": 1,
                "size": len(payload),
            }
        }
    return result


def _serialize_recovery_bootblock_contract(
    contract: RecoveryBootblockContract,
) -> str:
    """Serialisiert nur den bereits validierten Inodevertrag für den Ziel-Finalizer."""

    _validate_recovery_bootblock_contract(contract)
    record = {
        "created_directories": list(contract.created_directories),
        "dropin_identities": [list(item) for item in contract.dropin_identities],
        "transaction_id": contract.transaction_id,
        "units": list(contract.units),
    }
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _parse_recovery_bootblock_contract(
    payload: str,
    *,
    verify_active_gate: bool = True,
) -> RecoveryBootblockContract:
    """Bindet den statischen Cutover-Bootblock im getrennten Finalizerprozess."""

    raw = str(payload or "")
    if not raw or len(raw.encode("utf-8")) > 128 * 1024:
        raise RuntimeError("Statischer Recovery-Bootblock-Vertrag fehlt oder ist zu groß")
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Statischer Recovery-Bootblock-Vertrag ist kein JSON") from exc
    if (
        not isinstance(record, dict)
        or set(record)
        != {"created_directories", "dropin_identities", "transaction_id", "units"}
        or json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        != raw
    ):
        raise RuntimeError("Statischer Recovery-Bootblock-Vertrag ist nicht kanonisch")
    try:
        contract = RecoveryBootblockContract(
            units=tuple(str(item) for item in record["units"]),
            created_directories=tuple(
                str(item) for item in record["created_directories"]
            ),
            transaction_id=str(record["transaction_id"]),
            dropin_identities=tuple(
                (str(item[0]), int(item[1]), int(item[2]))
                for item in record["dropin_identities"]
            ),
        )
    except (TypeError, ValueError, IndexError) as exc:
        raise RuntimeError("Statischer Recovery-Bootblock-Vertrag ist ungültig") from exc
    _validate_recovery_bootblock_contract(contract)
    if verify_active_gate:
        _verify_recovery_bootblock_marker(contract, expected_present=True)
        _reload_and_verify_recovery_dropins(
            contract.units,
            expected_present=True,
            transaction_id=contract.transaction_id,
        )
    return contract


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
    contract: (
        UpdateSafetyContract
        | RecoveryBootblockContract
        | legacy_safety_codec.LegacyUpdateSafetyReceipt
    ),
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
    contract: (
        UpdateSafetyContract
        | legacy_safety_codec.LegacyUpdateSafetyReceipt
    ),
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


def _read_bound_legacy_update_safety_receipt(
    *,
    allow_missing: bool = False,
) -> tuple[
    legacy_safety_codec.LegacyUpdateSafetyReceipt,
    os.stat_result,
] | None:
    """Liest nur den exakt veröffentlichten v5.4.4/v5.4.4a-Vertrag."""

    receipt_path = legacy_safety_codec.LEGACY_UPDATE_SAFETY_RECEIPT_PATH
    if not os.path.lexists(receipt_path):
        if allow_missing:
            return None
        raise RuntimeError("Legacy-Update-Sicherheitsreceipt fehlt")
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        readback = _read_bound_root_file_at(
            state_descriptor,
            UPDATE_SAFETY_RECEIPT_NAME,
            maximum=legacy_safety_codec.LEGACY_UPDATE_SAFETY_MAX_BYTES,
            mode=0o600,
            allow_missing=allow_missing,
        )
        if readback is None:
            return None
        payload, metadata = readback
        receipt = legacy_safety_codec.parse_legacy_update_safety_receipt(payload)
        if receipt.receipt_path != receipt_path:
            raise RuntimeError("Legacy-Receiptpfad driftete vom veröffentlichten Vertrag")
        return receipt, metadata
    finally:
        os.close(state_descriptor)


def _read_bound_active_legacy_forward_receipt(
    *,
    expected_target_commit: str,
    expected_target_tag: str,
) -> tuple[
    legacy_safety_codec.LegacyUpdateSafetyReceipt,
    os.stat_result,
]:
    """Liest den laufenden v1-Vertrag nur gegen den gebundenen Ziel-Snapshot."""

    receipt_path = legacy_safety_codec.LEGACY_UPDATE_SAFETY_RECEIPT_PATH
    if not os.path.lexists(receipt_path):
        raise RuntimeError("Aktives Legacy-Forward-Receipt fehlt")
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        readback = _read_bound_root_file_at(
            state_descriptor,
            UPDATE_SAFETY_RECEIPT_NAME,
            maximum=legacy_safety_codec.LEGACY_UPDATE_SAFETY_MAX_BYTES,
            mode=0o600,
        )
        if readback is None:
            raise RuntimeError("Aktives Legacy-Forward-Receipt verschwand")
        payload, metadata = readback
        receipt = legacy_safety_codec.parse_active_legacy_forward_receipt(
            payload,
            expected_target_commit=expected_target_commit,
            expected_target_tag=expected_target_tag,
        )
        if receipt.receipt_path != receipt_path:
            raise RuntimeError(
                "Aktiver Legacy-Forward-Receiptpfad driftete vom veröffentlichten Vertrag"
            )
        return receipt, metadata
    finally:
        os.close(state_descriptor)


def _revalidate_bound_active_legacy_forward_receipt(
    receipt: legacy_safety_codec.LegacyUpdateSafetyReceipt,
    *,
    receipt_dev: int,
    receipt_ino: int,
) -> os.stat_result:
    current, metadata = _read_bound_active_legacy_forward_receipt(
        expected_target_commit=receipt.target_commit,
        expected_target_tag=receipt.target_tag,
    )
    if (
        current != receipt
        or (int(metadata.st_dev), int(metadata.st_ino))
        != (int(receipt_dev), int(receipt_ino))
    ):
        raise RuntimeError("Aktives Legacy-Forward-Receipt oder Inode driftete")
    return metadata


def _bind_active_legacy_forward_backup_source(
    receipt: legacy_safety_codec.LegacyUpdateSafetyReceipt,
    *,
    requested_install_root: str,
) -> str:
    """Beweist den v5.4.4/v5.4.4a-Writer über vier Vollbackup-Blobs."""

    root = _bind_legacy_update_backup_instance(
        receipt,
        requested_install_root=requested_install_root,
    )
    manifest, manifest_sha256 = _read_stable_verified_backup_manifest(
        receipt.backup_dir
    )
    if manifest_sha256 != receipt.backup_manifest_sha256:
        raise RuntimeError("Legacy-Forward-Backupmanifest driftete")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise RuntimeError("Legacy-Forward-Vollbackup besitzt keine Dateiliste")
    by_restore: dict[str, dict] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            raise RuntimeError("Legacy-Forward-Vollbackup besitzt einen ungültigen Eintrag")
        restore_path = str(raw.get("restore_path") or "")
        if restore_path:
            if restore_path in by_restore:
                raise RuntimeError(
                    f"Legacy-Forward-Vollbackup besitzt ein doppeltes Ziel: {restore_path}"
                )
            by_restore[restore_path] = raw

    matching_sources: list[str] = []
    for source_tag, expected_files in (
        legacy_safety_codec.LEGACY_FORWARD_SOURCE_BLOBS.items()
    ):
        matched = True
        for relative_path, expected_sha256 in expected_files.items():
            restore_path = os.path.join(root, relative_path)
            entry = by_restore.get(restore_path)
            if (
                entry is None
                or entry.get("category") != "install-tree"
                or entry.get("sha256") != expected_sha256
            ):
                matched = False
                break
        if matched:
            matching_sources.append(source_tag)
    if len(matching_sources) != 1:
        raise RuntimeError(
            "Legacy-Forward-Parent ist im Vollbackup nicht eindeutig als "
            "veröffentlichtes v5.4.4 oder v5.4.4a gebunden"
        )
    return matching_sources[0]


def _revalidate_bound_legacy_update_safety_receipt(
    receipt: legacy_safety_codec.LegacyUpdateSafetyReceipt,
    *,
    receipt_dev: int,
    receipt_ino: int,
) -> os.stat_result:
    current = _read_bound_legacy_update_safety_receipt()
    if current is None:
        raise RuntimeError("Legacy-Update-Sicherheitsreceipt verschwand")
    rebound, metadata = current
    if (
        rebound != receipt
        or (int(metadata.st_dev), int(metadata.st_ino))
        != (int(receipt_dev), int(receipt_ino))
    ):
        raise RuntimeError("Legacy-Update-Sicherheitsreceipt oder Inode driftete")
    return metadata


def _bind_legacy_update_backup_instance(
    receipt: legacy_safety_codec.LegacyUpdateSafetyReceipt,
    *,
    requested_install_root: str,
) -> str:
    """Bindet die im alten Receipt fehlende Instanz über dessen Vollbackup."""

    backup_dir = receipt.backup_dir
    metadata = os.lstat(backup_dir)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or os.path.realpath(backup_dir) != backup_dir
        or (int(metadata.st_dev), int(metadata.st_ino))
        != (receipt.backup_dev, receipt.backup_ino)
    ):
        raise RuntimeError("Legacy-Vollbackup besitzt nicht mehr seinen Root-/Inodevertrag")
    manifest, manifest_sha256 = _read_stable_verified_backup_manifest(backup_dir)
    if (
        manifest_sha256 != receipt.backup_manifest_sha256
        or str(manifest.get("backup_id") or "") != receipt.backup_id
    ):
        raise RuntimeError("Legacy-Receipt und verifiziertes Vollbackup widersprechen sich")
    manifest_root = str(manifest.get("install_root") or "")
    requested = os.path.realpath(os.path.abspath(requested_install_root))
    if (
        not manifest_root
        or not os.path.isabs(manifest_root)
        or os.path.abspath(manifest_root) != manifest_root
        or os.path.realpath(manifest_root) != requested
    ):
        raise RuntimeError(
            "Legacy-Vollbackup gehört nicht zur angeforderten Installation "
            f"({manifest_root or 'unbekannt'} != {requested})"
        )
    return requested


def _rebind_legacy_update_safety_dropins(
    receipt: legacy_safety_codec.LegacyUpdateSafetyReceipt,
    *,
    allow_missing: bool,
) -> dict[str, tuple[int, int]]:
    """Beweist vor Marker-Unlink, dass kein fremdes 00-Gate übersehen wird."""

    expected = {
        unit: (int(device), int(inode))
        for unit, device, inode in receipt.dropin_identities
    }
    payload = receipt.dropin_payload
    rebound: dict[str, tuple[int, int]] = {}
    systemd_descriptor = _open_directory_nofollow("/etc/systemd/system")
    try:
        _require_root_controlled_directory(
            systemd_descriptor,
            "/etc/systemd/system",
        )
        for entry in os.listdir(systemd_descriptor):
            if not entry.endswith(".d"):
                continue
            before = os.stat(
                entry,
                dir_fd=systemd_descriptor,
                follow_symlinks=False,
            )
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
                    0o755,
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
                if RECOVERY_BOOTBLOCK_DROPIN_NAME not in set(
                    os.listdir(directory_descriptor)
                ):
                    continue
                unit = entry[:-2]
                if unit not in expected:
                    raise RuntimeError(
                        f"Fremdes Recovery-Drop-in sperrt Legacy-Cleanup: {unit}"
                    )
                dropin = _read_exact_root_file_at(
                    directory_descriptor,
                    RECOVERY_BOOTBLOCK_DROPIN_NAME,
                    payload,
                    0o644,
                    allow_missing=False,
                )
                identity = (int(dropin.st_dev), int(dropin.st_ino))
                if identity != expected[unit]:
                    raise RuntimeError(
                        f"Legacy-Drop-in-Inode driftete vor Cleanup: {unit}"
                    )
                rebound[unit] = identity
            finally:
                os.close(directory_descriptor)
    finally:
        os.close(systemd_descriptor)
    if not allow_missing and set(rebound) != set(receipt.units):
        missing = sorted(set(receipt.units) - set(rebound))
        raise RuntimeError(
            "Pending Legacy-Gate ist nicht vollständig vorhanden: "
            + ", ".join(missing[:5])
        )
    return rebound


def _remove_exact_legacy_update_safety_receipt(
    receipt: legacy_safety_codec.LegacyUpdateSafetyReceipt,
    *,
    receipt_dev: int,
    receipt_ino: int,
) -> None:
    _revalidate_bound_legacy_update_safety_receipt(
        receipt,
        receipt_dev=receipt_dev,
        receipt_ino=receipt_ino,
    )
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        current = _read_bound_root_file_at(
            state_descriptor,
            UPDATE_SAFETY_RECEIPT_NAME,
            maximum=legacy_safety_codec.LEGACY_UPDATE_SAFETY_MAX_BYTES,
            mode=0o600,
        )
        if current is None:
            raise RuntimeError("Legacy-Receipt verschwand vor dem Unlink")
        rebound = legacy_safety_codec.parse_legacy_update_safety_receipt(current[0])
        if (
            rebound != receipt
            or (int(current[1].st_dev), int(current[1].st_ino))
            != (int(receipt_dev), int(receipt_ino))
        ):
            raise RuntimeError("Fremdes Legacy-Receipt wird nicht entfernt")
        os.unlink(UPDATE_SAFETY_RECEIPT_NAME, dir_fd=state_descriptor)
        os.fsync(state_descriptor)
    finally:
        os.close(state_descriptor)
    if os.path.lexists(receipt.receipt_path):
        raise RuntimeError("Legacy-Update-Sicherheitsreceipt blieb erhalten")


def _remove_exact_legacy_update_safety_marker(
    receipt: legacy_safety_codec.LegacyUpdateSafetyReceipt,
    *,
    receipt_dev: int,
    receipt_ino: int,
) -> None:
    """Entfernt nur den unmittelbar erneut gebundenen Legacy-Marker."""

    _revalidate_bound_legacy_update_safety_receipt(
        receipt,
        receipt_dev=receipt_dev,
        receipt_ino=receipt_ino,
    )
    _rebind_legacy_update_safety_dropins(receipt, allow_missing=True)
    marker_name = Path(RECOVERY_BOOTBLOCK_MARKER).name
    marker_payload = _recovery_bootblock_marker_payload(receipt.transaction_id)
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        marker = _read_exact_root_file_at(
            state_descriptor,
            marker_name,
            marker_payload,
            0o600,
            allow_missing=True,
        )
        if marker is None:
            return
        marker_identity = _file_identity(marker)
        _revalidate_bound_legacy_update_safety_receipt(
            receipt,
            receipt_dev=receipt_dev,
            receipt_ino=receipt_ino,
        )
        rebound = _read_exact_root_file_at(
            state_descriptor,
            marker_name,
            marker_payload,
            0o600,
            allow_missing=False,
        )
        named_after = os.stat(
            marker_name,
            dir_fd=state_descriptor,
            follow_symlinks=False,
        )
        if (
            rebound is None
            or _file_identity(rebound) != marker_identity
            or _file_identity(named_after) != marker_identity
        ):
            raise RuntimeError("Legacy-Marker driftete unmittelbar vor dem Unlink")
        os.unlink(marker_name, dir_fd=state_descriptor)
        os.fsync(state_descriptor)
    finally:
        os.close(state_descriptor)
    _verify_update_safety_marker(receipt, expected_present=False)


def _finish_committed_legacy_update_safety_residue(
    receipt: legacy_safety_codec.LegacyUpdateSafetyReceipt,
    *,
    receipt_dev: int,
    receipt_ino: int,
) -> None:
    """Vollendet nur ein bereits dauerhaft committed altes Startgate."""

    _assert_legacy_recovery_namespace_exclusive()
    _revalidate_bound_legacy_update_safety_receipt(
        receipt,
        receipt_dev=receipt_dev,
        receipt_ino=receipt_ino,
    )
    _rebind_legacy_update_safety_dropins(receipt, allow_missing=True)
    _settle_terminal_finalizer_lease(receipt)
    if os.path.lexists(f"/run/{receipt.runtime_directory}") or os.path.lexists(
        receipt.token_path
    ):
        raise RuntimeError("Committed Legacy-Finalizer-Runtime/Token ist noch vorhanden")
    _assert_no_same_transaction_finalizer_processes(receipt)
    _revalidate_bound_legacy_update_safety_receipt(
        receipt,
        receipt_dev=receipt_dev,
        receipt_ino=receipt_ino,
    )
    _assert_legacy_recovery_namespace_exclusive()
    _rebind_legacy_update_safety_dropins(receipt, allow_missing=True)
    _remove_exact_legacy_update_safety_marker(
        receipt,
        receipt_dev=receipt_dev,
        receipt_ino=receipt_ino,
    )
    _remove_owned_update_safety_dropins(
        units=receipt.units,
        identities={
            unit: (int(device), int(inode))
            for unit, device, inode in receipt.dropin_identities
        },
        created_directories=receipt.created_directories,
        payload=receipt.dropin_payload,
        allow_missing=True,
    )
    _reload_and_verify_update_safety_dropins(receipt, expected_present=False)
    _remove_exact_legacy_update_safety_receipt(
        receipt,
        receipt_dev=receipt_dev,
        receipt_ino=receipt_ino,
    )


def _finish_committed_update_safety_residue_if_safe() -> bool:
    """Räumt nur vollständig gebundene committed Residuen vor einem neuen Lauf."""

    residue = _read_update_safety_contract(allow_missing=True)
    if residue is None:
        return _finish_committed_package_residue_if_safe()
    if residue.state != "committed":
        return False
    expected_root = _validate_bootstrap_install_path(INSTALL_PATH)
    package_receipt = _read_prepared_package_receipt(allow_missing=True)
    if package_receipt is not None:
        if (
            package_receipt.state not in {"prepared", "committed"}
            or package_receipt.transaction_id != residue.transaction_id
            or package_receipt.install_root != expected_root
            or package_receipt.full_backup_id != residue.backup_id
            or package_receipt.target_commit != residue.target_commit
            or package_receipt.target_tag != residue.target_tag
            or package_receipt.role != residue.role
            or not package_receipt.apache_completion_required
            or package_receipt.apache_available != residue.apache_available
            or package_receipt.apache_was_active != residue.apache_was_active
            or package_receipt.apache_unit_file_state
            != residue.apache_unit_file_state
            or package_receipt.static_recovery_contract_json
        ):
            raise RuntimeError(
                "Committed Update-Sicherheitsreceipt und Paket-Receipt widersprechen sich"
            )
        if package_receipt.prepared_state is None:
            raise RuntimeError("Committed Paket-Receipt besitzt keinen Postzustand")
        _package_transaction_from_receipt(package_receipt)
    _settle_terminal_finalizer_lease(residue)
    if os.path.lexists(f"/run/{residue.runtime_directory}") or os.path.lexists(
        residue.token_path
    ):
        raise RuntimeError("Committed Finalizer-Runtime/Token ist noch vorhanden")
    _assert_no_same_transaction_finalizer_processes(residue)
    # Marker und Drop-ins dürfen zuerst fallen, der durable committed-Anker
    # bleibt aber bis nach dem exakt inodegebundenen Paket-Receipt erhalten.
    # Ein Absturz zwischen beiden Unlinks hinterlässt dadurch niemals ein
    # orphaned Paket-Receipt ohne den zugehörigen Commitnachweis.
    _finish_committed_update_safety_cleanup(
        residue,
        remove_receipt=False,
    )
    # Der durable Beleg bleibt bestehen, bis auch die äußeren, weiterhin
    # transaktionsgebundenen Arbeitsartefakte bestätigt entfernt sind.
    if package_receipt is not None:
        offline_receipt = parse_offline_package_receipt(
            package_receipt.prepared_state.offline_receipt_json.encode("utf-8")
        )
        if not _cleanup_terminal_offline_package_receipt(
            offline_receipt,
            terminal_state="committed neue Stand",
        ):
            raise RuntimeError("Committed Offline-Paketcache blieb beim Retry erhalten")
    overlay_receipt = _read_quiesced_overlay_receipt(allow_missing=True)
    if overlay_receipt is not None:
        if (
            overlay_receipt.transaction_id != residue.transaction_id
            or overlay_receipt.install_root != expected_root
        ):
            raise RuntimeError("Committed Overlay-Receipt widerspricht dem Update-Receipt")
        _remove_quiesced_overlay_receipt_and_tree(overlay_receipt)
    snapshot_parent = _trusted_same_filesystem_snapshot_parent(expected_root)
    _cleanup_stale_target_execution_snapshots(
        snapshot_parent,
        prefixes=(TARGET_FINALIZER_SNAPSHOT_PREFIX,),
    )
    if package_receipt is not None:
        _remove_exact_prepared_package_receipt(package_receipt)
    _remove_exact_update_safety_receipt(residue)
    if _read_update_safety_contract(allow_missing=True) is not None:
        raise RuntimeError("Committed Update-Sicherheitsreceipt blieb nach Cleanup")
    if _read_package_recovery_receipt(allow_missing=True) is not None:
        raise RuntimeError("Committed Paket-Receipt blieb nach Cleanup")
    return True


def _finish_committed_package_gate_cleanup(
    contract: PreparedPackageReceipt,
) -> PreparedPackageReceipt:
    """Vollendet den statischen Bootblock und Apache nur aus committed Beleg."""

    current = _validate_prepared_package_receipt(
        contract,
        expected_state="committed",
    )
    static_contract = None
    if current.static_recovery_contract_json:
        static_contract = _parse_recovery_bootblock_contract(
            current.static_recovery_contract_json,
            verify_active_gate=False,
        )
        marker_payload = _recovery_bootblock_marker_payload(
            static_contract.transaction_id
        )
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
        _verify_recovery_bootblock_marker(static_contract, expected_present=False)
        _remove_owned_update_safety_dropins(
            units=static_contract.units,
            identities={
                unit: (device, inode)
                for unit, device, inode in static_contract.dropin_identities
            },
            created_directories=static_contract.created_directories,
            payload=_render_recovery_bootblock_dropin(
                static_contract.transaction_id
            ),
            allow_missing=True,
        )
        _reload_and_verify_recovery_dropins(
            static_contract.units,
            expected_present=False,
            transaction_id=static_contract.transaction_id,
        )
    elif os.path.lexists(RECOVERY_BOOTBLOCK_MARKER):
        raise RuntimeError(
            "Committed Paket-Receipt ohne statischen Vertrag sieht einen Marker"
        )
    if current.apache_completion_required:
        _complete_bound_apache_after_commit(
            expected_available=current.apache_available,
            expected_active=current.apache_was_active,
            expected_unit_file_state=current.apache_unit_file_state,
        )
    return current


def _finish_committed_package_residue_if_safe() -> bool:
    """Wiederholt einen statischen/direct PostCommit-Abschluss beim nächsten Lauf."""

    package_receipt = _read_prepared_package_receipt(allow_missing=True)
    if package_receipt is None or package_receipt.state != "committed":
        return False
    static_contract = (
        _parse_recovery_bootblock_contract(
            package_receipt.static_recovery_contract_json,
            verify_active_gate=False,
        )
        if package_receipt.static_recovery_contract_json
        else RecoveryBootblockContract(
            units=(),
            created_directories=(),
            transaction_id=package_receipt.transaction_id,
            dropin_identities=(),
        )
    )
    _settle_terminal_finalizer_lease(static_contract)
    if os.path.lexists(f"/run/{static_contract.runtime_directory}") or os.path.lexists(
        static_contract.token_path
    ):
        raise RuntimeError("Committed statische Finalizer-Runtime/Token ist noch vorhanden")
    _assert_no_same_transaction_finalizer_processes(static_contract)
    _finish_committed_package_gate_cleanup(package_receipt)
    if package_receipt.prepared_state is None:
        raise RuntimeError("Committed Paket-Receipt besitzt keinen Postzustand")
    offline_receipt = parse_offline_package_receipt(
        package_receipt.prepared_state.offline_receipt_json.encode("utf-8")
    )
    if not _cleanup_terminal_offline_package_receipt(
        offline_receipt,
        terminal_state="committed neue Stand",
    ):
        raise RuntimeError("Committed Offline-Paketcache blieb beim Retry erhalten")
    overlay_receipt = _read_quiesced_overlay_receipt(allow_missing=True)
    if overlay_receipt is not None:
        if (
            overlay_receipt.transaction_id != package_receipt.transaction_id
            or overlay_receipt.install_root != package_receipt.install_root
        ):
            raise RuntimeError("Committed Paket- und Overlay-Receipt widersprechen sich")
        _remove_quiesced_overlay_receipt_and_tree(overlay_receipt)
    _cleanup_stale_target_execution_snapshots(
        _trusted_same_filesystem_snapshot_parent(package_receipt.install_root),
        prefixes=(TARGET_FINALIZER_SNAPSHOT_PREFIX,),
    )
    _remove_exact_prepared_package_receipt(package_receipt)
    return True


def _assert_no_recovery_bootblock_dropins() -> None:
    """Prüft den globalen systemd-Gate-Namensraum ohne Mutation."""

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
                state_entries = set(os.listdir(state_descriptor))
                if PREPARED_PACKAGE_RECEIPT_NAME in state_entries:
                    raise RuntimeError(
                        "[E3DC-UPD-PACKAGE-PENDING] Ein unvollständiger "
                        "Update-Lauf besitzt einen rebootfesten Paket-Prestate. "
                        f"Lösung: {os.path.join(RECOVERY_BOOTBLOCK_STATE_DIR, PREPARED_PACKAGE_RECEIPT_NAME)} "
                        "nicht löschen und Pakete nicht manuell ändern. Prüfe zuerst "
                        "sudo dpkg --audit und sudo journalctl -b -u "
                        "'e3dc-*update*' --no-pager; führe anschließend nur den "
                        "dort für diese Transaktion genannten Recovery-Schritt aus."
                    )
                if Path(RECOVERY_BOOTBLOCK_MARKER).name in state_entries:
                    raise RuntimeError(
                        "Vorhandener Recovery-Bootblock-Marker sperrt einen neuen Updatepfad"
                    )
                if QUIESCED_OVERLAY_RECEIPT_NAME in state_entries:
                    raise RuntimeError(
                        "[E3DC-UPD-OVERLAY-PENDING] Ein unvollständiger Update-"
                        "Rückfall besitzt noch Nutzerdaten. Lösung: Dienste nicht "
                        "manuell starten oder Dateien löschen; zuerst "
                        "sudo journalctl -u 'e3dc-*update*' --no-pager prüfen und "
                        "den dort genannten Recovery-Befehl ausführen."
                    )
            finally:
                os.close(state_descriptor)
    finally:
        os.close(var_lib_descriptor)

    _assert_no_recovery_bootblock_dropins()

    residue = _read_update_safety_contract(allow_missing=True)
    if residue is not None:
        raise RuntimeError(
            "Pending oder unvollständig bereinigtes Update-Sicherheitsreceipt "
            "verlangt eine manuelle fail-closed Prüfung"
        )


def _assert_active_recovery_transaction_namespace(
    journal_contract: recovery_journal.RecoveryJournalContract,
    *,
    expected_transaction_id: str,
) -> PersistentRecoveryBundle:
    """Bindet den noch leeren Gate-Namensraum derselben Preproduct-Transaktion.

    Der äußere Update-Einstieg behandelt Altlasten und vorherige Transaktionen.
    Dieser interne Guard darf dagegen weder Cleanup ausführen noch das eigene
    Master-Journal als fremde Altlast fehlklassifizieren.
    """

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("Interner Recovery-Guard darf ausschließlich Root ausführen")
    current = recovery_journal.read_recovery_journal(allow_missing=False)
    current = recovery_journal.verify_recovery_journal(current)
    expected = recovery_journal.verify_recovery_journal(journal_contract)
    if (
        expected.payload.phase != recovery_journal.PHASE_PREPRODUCT
        or current.payload.phase != recovery_journal.PHASE_PREPRODUCT
        or expected.payload.transaction_id != expected_transaction_id
        or current.payload.transaction_id != expected_transaction_id
        or not _same_recovery_journal_transaction_shape(current, expected)
        or current.journal_device != expected.journal_device
        or current.journal_inode != expected.journal_inode
        or current.journal_sha256 != expected.journal_sha256
        or current.payload.package is not None
        or current.payload.safety is not None
        or current.payload.overlay is not None
    ):
        raise RuntimeError(
            "Interner Recovery-Guard widerspricht dem aktiven Preproduct-Journal"
        )
    if os.path.lexists(
        prejournal_codec.PREJOURNAL_CONSTRUCTION_PATH
    ):
        raise RuntimeError(
            "Aktiver Preproduct-Parent besitzt noch einen Construction-Receipt"
        )

    bundle = _load_persistent_recovery_bundle(current)
    if bundle.context is None or bundle.surface is None or bundle.systemd is None:
        raise RuntimeError("Aktiver Preproduct-Parent ist unvollständig")

    forbidden_paths = (
        os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            PREPARED_PACKAGE_RECEIPT_NAME,
        ),
        os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            UPDATE_SAFETY_RECEIPT_NAME,
        ),
        os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            QUIESCED_OVERLAY_RECEIPT_NAME,
        ),
        RECOVERY_BOOTBLOCK_MARKER,
    )
    present = tuple(path for path in forbidden_paths if os.path.lexists(path))
    if present:
        raise RuntimeError(
            "Aktiver Preproduct-Parent besitzt bereits Gate-/Paketartefakte: "
            + ", ".join(present)
        )
    for unit in _recovery_bootblock_units():
        path = _recovery_dropin_path(unit)
        if os.path.lexists(path):
            raise RuntimeError(
                "Aktiver Preproduct-Parent besitzt bereits ein Gate-Drop-in: "
                + path
            )
    return bundle


def _assert_exclusive_not_found_recovery_dropin(
    unit: str,
    *,
    expected_payload: bytes,
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
    transaction_id: str,
) -> None:
    transaction_id = str(transaction_id or "")
    if not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(transaction_id):
        raise RuntimeError("Recovery-Bootblock-Readback besitzt keine gültige Transaktion")
    payload = _render_recovery_bootblock_dropin(transaction_id)
    finalizer_unit, _runtime_directory, token_path = _update_safety_names(
        transaction_id
    )
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
    marker = RECOVERY_BOOTBLOCK_DROPIN_MARKER
    marker_condition = f"|!{RECOVERY_BOOTBLOCK_MARKER}"
    token_condition = f"|{token_path}"

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
        effective_conditions: list[tuple[str, str]] = []
        condition_syntax_ambiguous = False
        condition_reset_seen = False
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
                    condition_reset_seen = True
                    effective_conditions.clear()
                else:
                    effective_conditions.append(assignment)
        own_or_conditions = [
            condition
            for condition in effective_conditions
            if condition[1].startswith("|")
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
                _assert_exclusive_not_found_recovery_dropin(
                    unit,
                    expected_payload=payload,
                )
                continue
            try:
                binds_to = tuple(shlex.split(show_value(unit, "BindsTo")))
                after = tuple(shlex.split(show_value(unit, "After")))
            except ValueError as exc:
                raise RuntimeError(
                    f"systemd-Lease-Abhängigkeiten sind unklar: {unit}"
                ) from exc
            if (
                load_state != "loaded"
                or not result.get("success")
                or result.get("timed_out")
                or str(result.get("stderr") or "")
                or int(result.get("returncode", -1)) != 0
                or dropin_paths.count(own_dropin) != 1
                or len(marker_lines) != 1
                or condition_syntax_ambiguous
                or condition_reset_seen
                or output.splitlines().count(f"BindsTo={finalizer_unit}") != 1
                or output.splitlines().count(f"After={finalizer_unit}") != 1
                or binds_to.count(finalizer_unit) != 1
                or after.count(finalizer_unit) != 1
                or effective_conditions.count(
                    ("ConditionPathExists", marker_condition)
                )
                != 1
                or effective_conditions.count(
                    ("ConditionPathExists", token_condition)
                )
                != 1
                or own_or_conditions
                != [
                    ("ConditionPathExists", marker_condition),
                    ("ConditionPathExists", token_condition),
                ]
            ):
                raise RuntimeError(
                    f"systemd-Readback des Recovery-Bootblocks weicht ab: {unit}"
                )
        else:
            if load_state not in {"loaded", "masked", "not-found"}:
                raise RuntimeError(
                    f"systemd-Readback nach Bootblock-Entfernung ist unklar: {unit}"
                )
            binds_to: tuple[str, ...] = ()
            after: tuple[str, ...] = ()
            if load_state == "loaded":
                try:
                    binds_to = tuple(shlex.split(show_value(unit, "BindsTo")))
                    after = tuple(shlex.split(show_value(unit, "After")))
                except ValueError as exc:
                    raise RuntimeError(
                        f"systemd-Lease-Abhängigkeiten sind unklar: {unit}"
                    ) from exc
            if (
                own_dropin in dropin_paths
                or marker_lines
                or finalizer_unit in binds_to
                or finalizer_unit in after
                or (load_state == "not-found" and not canonical_not_found)
                or (
                    "ConditionPathExists",
                    marker_condition,
                )
                in effective_conditions
                or (
                    "ConditionPathExists",
                    token_condition,
                )
                in effective_conditions
                or condition_reset_seen
                or condition_syntax_ambiguous
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
    payload = _render_recovery_bootblock_dropin(partial.transaction_id)
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
                        payload,
                        0o644,
                        allow_missing=True,
                    )
                    if metadata is None:
                        _create_exact_root_file_at(
                            directory_descriptor,
                            RECOVERY_BOOTBLOCK_DROPIN_NAME,
                            payload,
                            0o644,
                        )
                        metadata = _read_exact_root_file_at(
                            directory_descriptor,
                            RECOVERY_BOOTBLOCK_DROPIN_NAME,
                            payload,
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
                        payload,
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
    *,
    recovery_journal_contract: recovery_journal.RecoveryJournalContract,
) -> RecoveryBootblockContract:
    """Installiert rebootfeste Conditions mit erhaltener Partial-Autorität."""

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("Recovery-Bootblock darf ausschließlich Root verwalten")
    _recovery_bootblock_marker_payload(transaction_id)
    _assert_active_recovery_transaction_namespace(
        recovery_journal_contract,
        expected_transaction_id=transaction_id,
    )
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
    payload = _render_recovery_bootblock_dropin(contract.transaction_id)
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
                    payload,
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
    recovery_journal_contract: recovery_journal.RecoveryJournalContract | None = None,
) -> RecoveryBootblockContract:
    if contract is None:
        value = str(transaction_id or "")
        if not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(value):
            raise RuntimeError("Recovery-Bootblock fehlt die vorab gebundene Transaktions-ID")
        if recovery_journal_contract is None:
            raise RuntimeError(
                "Recovery-Bootblock fehlt der gebundene Master-Journal-Parent"
            )
        prepared = _prepare_persistent_recovery_bootblock(
            value,
            recovery_journal_contract=recovery_journal_contract,
        )
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
        _reload_and_verify_recovery_dropins(
            prepared.units,
            expected_present=True,
            transaction_id=prepared.transaction_id,
        )
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
    _reload_and_verify_recovery_dropins(
        contract.units,
        expected_present=True,
        transaction_id=contract.transaction_id,
    )
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
    _reload_and_verify_recovery_dropins(
        contract.units,
        expected_present=True,
        transaction_id=contract.transaction_id,
    )


def _remove_persistent_recovery_bootblock(
    contract: RecoveryBootblockContract,
) -> None:
    """Entfernt nur den exakt gebundenen Block nach erfolgreichem Endgate."""

    contract = _rebind_owned_recovery_dropins(contract, recreate_missing=False)
    identities = _validate_recovery_bootblock_contract(contract)
    payload = _render_recovery_bootblock_dropin(contract.transaction_id)
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
                    payload,
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
    _reload_and_verify_recovery_dropins(
        contract.units,
        expected_present=False,
        transaction_id=contract.transaction_id,
    )


def _update_safety_names(transaction_id: str) -> tuple[str, str, str]:
    """Leitet Unit, RuntimeDirectory und Token ausschließlich aus der txid ab."""

    value = str(transaction_id or "")
    if not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(value):
        raise RuntimeError("Update-Sicherheitsvertrag besitzt keine gültige Transaktions-ID")
    unit = f"{UPDATE_FINALIZER_UNIT_PREFIX}{value}.service"
    runtime = f"{UPDATE_FINALIZER_UNIT_PREFIX}{value}{UPDATE_FINALIZER_RUNTIME_SUFFIX}"
    token = f"/run/{runtime}/{UPDATE_FINALIZER_TOKEN_NAME}"
    return unit, runtime, token


def _render_recovery_bootblock_dropin(transaction_id: str) -> bytes:
    """Erzeugt das rebootfeste statische Gate mit einer flüchtigen Startlease."""

    unit, _runtime, token = _update_safety_names(transaction_id)
    return (
        f"{RECOVERY_BOOTBLOCK_DROPIN_MARKER}\n"
        "[Unit]\n"
        f"BindsTo={unit}\n"
        f"After={unit}\n"
        f"ConditionPathExists=|!{RECOVERY_BOOTBLOCK_MARKER}\n"
        f"ConditionPathExists=|{token}\n"
    ).encode("utf-8")


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
    apache_preimage: ApacheSecurityPreimage,
    units: tuple[str, ...],
    created_directories: tuple[str, ...],
    dropin_identities: tuple[tuple[str, int, int], ...],
) -> dict:
    unit, runtime, token = _update_safety_names(transaction_id)
    payload = _render_update_safety_dropin(transaction_id)
    if not isinstance(apache_preimage, ApacheSecurityPreimage):
        raise RuntimeError("Update-Sicherheitsreceipt besitzt kein Apache-Preimage")
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
        "apache": {
            "completion_required": True,
            "available": apache_preimage.apache_available,
            "was_active": apache_preimage.apache_was_active,
            "unit_file_state": apache_preimage.apache_unit_file_state,
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
            "schema", "state", "transaction_id", "target", "backup", "apache",
            "bootblock", "finalizer"
        }
    ):
        raise RuntimeError("Update-Sicherheitsreceipt besitzt kein kanonisches Schema")
    transaction_id = str(record.get("transaction_id") or "")
    expected_unit, expected_runtime, expected_token = _update_safety_names(transaction_id)
    target = record.get("target")
    backup = record.get("backup")
    apache = record.get("apache")
    bootblock = record.get("bootblock")
    finalizer = record.get("finalizer")
    if (
        record.get("schema") != UPDATE_SAFETY_RECEIPT_SCHEMA
        or record.get("state") not in {"pending", "committed"}
        or not isinstance(target, dict)
        or set(target) != {"commit", "tag", "role"}
        or not isinstance(backup, dict)
        or set(backup) != {"dir", "dev", "ino", "id", "manifest_sha256"}
        or not isinstance(apache, dict)
        or set(apache) != {
            "completion_required", "available", "was_active", "unit_file_state"
        }
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
    apache_available = apache.get("available")
    apache_was_active = apache.get("was_active")
    apache_unit_file_state = str(apache.get("unit_file_state") or "").strip().lower()
    accepted_apache_unit_states = {
        "enabled", "enabled-runtime", "disabled", "static", "indirect",
        "masked", "masked-runtime", "generated", "transient", "alias",
        "linked", "linked-runtime",
    }
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
        or apache.get("completion_required") is not True
        or not isinstance(apache_available, bool)
        or not isinstance(apache_was_active, bool)
        or apache_was_active and not apache_available
        or (
            not apache_available
            and (apache_was_active or apache_unit_file_state != "absent")
        )
        or (
            apache_available
            and apache_unit_file_state not in accepted_apache_unit_states
        )
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
        apache_completion_required=True,
        apache_available=apache_available,
        apache_was_active=apache_was_active,
        apache_unit_file_state=apache_unit_file_state,
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
        raise primary_error
    if result is None:
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
        "apache": {
            "completion_required": contract.apache_completion_required,
            "available": contract.apache_available,
            "was_active": contract.apache_was_active,
            "unit_file_state": contract.apache_unit_file_state,
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
    apache_preimage: ApacheSecurityPreimage,
    recovery_journal_contract: recovery_journal.RecoveryJournalContract,
) -> UpdateSafetyContract:
    """Installiert 00-Inodes und persistiert danach das pending Receipt."""

    if os.geteuid() != 0:
        raise RuntimeError("Vor-Mutations-Sicherheitsvertrag benötigt Root")
    if (
        not isinstance(backup_receipt, RecoveryBackupReceipt)
        or backup_receipt.transaction_id != transaction_id
    ):
        raise RuntimeError("Backup-Receipt ist nicht an die Update-Transaktion gebunden")
    _assert_active_recovery_transaction_namespace(
        recovery_journal_contract,
        expected_transaction_id=transaction_id,
    )
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
            apache_preimage=apache_preimage,
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
    """Räumt eigene Gates und vollendet danach Apache; niemals Altpreimages."""

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
    # Erst das durable committed Receipt autorisiert den extern erreichbaren
    # Webabschluss. Der Aufruf bleibt wiederholbar: fehlende eigene Gate-Namen
    # sind oben zulässig, systemctl start ist idempotent und HTTP wird erneut
    # ausschließlich über Loopback geprüft.
    _complete_committed_apache_from_receipt(contract)
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


def _start_background_update_dispatcher() -> bool:
    """Startet den installierten, selbst asynchronen Release-Dispatcher."""
    if os.geteuid() != 0:
        print("[!] Der Update-Dispatcher benötigt Root-Rechte.")
        print(f"    Starte: sudo {UPDATE_DISPATCHER}")
        return False
    try:
        result = subprocess.run(
            [UPDATE_DISPATCHER],
            check=False,
        )
    except OSError as exc:
        print(f"[!] Update-Dispatcher konnte nicht gestartet werden: {exc}")
        return False
    if result.returncode != 0:
        print(f"[!] Update-Dispatcher endete mit Exitcode {result.returncode}.")
        return False
    print("[OK] Updateauftrag läuft als root-kontrollierter Systemjob im Hintergrund.")
    print("    Update gestartet: e3dc-web-update.service")
    print("    Status: systemctl status --no-pager e3dc-web-update.service")
    print("    Protokoll: journalctl -fu e3dc-web-update.service")
    print("    Dateilog: tail -f /var/log/e3dc-control/web-update.log")
    return True


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
        if reinstall_current:
            return update_e3dc(
                headless=headless,
                reinstall_current=True,
            )
        return _start_background_update_dispatcher()
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


def _ensure_rsync_available(*, allow_install: bool = True) -> bool:
    if shutil.which("rsync"):
        return True
    if not allow_install:
        print(
            "  [!] [E3DC-UPD-RSYNC-MISSING] rsync fehlt nach dem Dienststopp. "
            "Lösung: Altstand nicht manuell verändern; das Update stellt ihn "
            "wieder her. Danach sudo apt-get install rsync ausführen und erneut starten."
        )
        return False
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


def _resolve_git_commit(
    repo_dir: str,
    ref: str,
    install_user: str,
    *,
    root_authority: bool = False,
) -> str | None:
    result = _git_argv(
        repo_dir,
        install_user,
        "rev-parse",
        "--verify",
        str(ref) + "^{commit}",
        timeout=15,
        **_root_git_call_kwargs(root_authority),
    )
    if not result['success']:
        return None
    try:
        return _validate_full_commit(result['stdout'].strip())
    except ValueError:
        return None


def _require_bound_origin(
    repo_dir: str,
    install_user: str,
    *,
    root_authority: bool = False,
) -> None:
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
        **_root_git_call_kwargs(root_authority),
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


def _read_policy_from_commit(
    repo_dir: str,
    commit: str,
    install_user: str | None = None,
    *,
    root_authority: bool = False,
) -> dict:
    """Read UPDATE_POLICY.json from one verified commit object, never the worktree."""
    verified_commit = _validate_full_commit(commit)
    user = install_user or get_install_user()
    raw = _read_commit_blob(
        repo_dir,
        verified_commit,
        "UPDATE_POLICY.json",
        user,
        maximum=1024 * 1024,
        **_root_git_call_kwargs(root_authority),
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
    """Bindet Release-Pakete plus interne, versionsneutrale Updatewerkzeuge."""

    core = _validated_core_apt_packages(policy)
    managed = _validate_policy_packages(
        policy,
        MANAGED_VENV_APT_POLICY_KEY,
        APPROVED_MANAGED_VENV_APT_PACKAGES,
    )
    packages = [*core, *(package for package in managed if package not in core)]
    packages.extend(
        package
        for package in UPDATE_RUNTIME_APT_PACKAGES
        if package not in packages
    )
    return packages


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


def _revalidate_recovery_backup_payload_receipt(
    receipt: RecoveryBackupReceipt,
    *,
    backup_dir: str,
    repo_dir: str,
    transaction_id: str,
) -> dict:
    """Prüft das Rückfallarchiv vor jeder Git-/Dateimutation ohne HEAD-Annahme."""

    if not isinstance(receipt, RecoveryBackupReceipt):
        raise RuntimeError("Recovery-Backup-Receipt fehlt vor der Rückfallmutation")
    root = os.path.abspath(str(repo_dir or ""))
    backup = os.path.abspath(str(backup_dir or ""))
    if (
        root != str(repo_dir)
        or backup != str(backup_dir)
        or receipt.backup_dir != backup
        or receipt.install_root != root
        or receipt.transaction_id != str(transaction_id or "")
    ):
        raise RuntimeError("Recovery-Backup-Receipt widerspricht der Transaktion")
    descriptor, chain = _open_root_receipt_directory_chain(backup)
    try:
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        chain != receipt.backup_path_chain
        or len(chain) < 2
        or (metadata.st_dev, metadata.st_ino)
        != (receipt.backup_dev, receipt.backup_ino)
        or (chain[-2][1], chain[-2][2])
        != (receipt.parent_dev, receipt.parent_ino)
    ):
        raise RuntimeError("Recovery-Backup- oder Parent-Inode driftete")
    manifest, manifest_sha256 = _read_stable_verified_backup_manifest(backup)
    if (
        manifest_sha256 != receipt.manifest_sha256
        or _manifest_semantic_sha256(manifest) != receipt.manifest_semantic_sha256
        or str(manifest.get("backup_id") or "") != receipt.backup_id
        or str(manifest.get("install_root") or "") != receipt.install_root
        or _manifest_file_receipt(manifest) != receipt.manifest_files
        or _privileged_backup_payload_receipts(backup, manifest)
        != receipt.privileged_backup_files
    ):
        raise RuntimeError("Recovery-Backup-Payload weicht vom Root-Receipt ab")
    return manifest


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


def _git_argv_as_install_user(
    repo_dir: str,
    install_user: str,
    *args: str,
    timeout: int = 30,
) -> dict:
    """Beweist Git-Lesbarkeit ausdrücklich unter der Zielidentität."""

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("Git-Zielidentitätsprobe darf ausschließlich Root starten")
    try:
        account = pwd.getpwnam(str(install_user))
    except KeyError as exc:
        raise RuntimeError("Gebundener Installationsbenutzer fehlt lokal") from exc
    if account.pw_uid == 0:
        raise RuntimeError("Git-Zielidentität darf nicht Root sein")
    return _run_argv(
        isolated_git_command(repo_dir, *args, run_as_user=account.pw_name),
        timeout=timeout,
    )


def _project_bootstrap_git_metadata_permissions(
    repo_dir: str,
    install_user: str,
) -> None:
    """Übergibt frische Root-Git-Metadaten nofollow an den Installationsnutzer.

    Der Aufruf erfolgt, solange der Produktroot noch nicht kanonisch auf den
    Installationsnutzer projiziert wurde. Damit bleibt die komplette
    Init-/Fetch-/Objekt-/Reset-Kette Root-autorisiert; erst diese gebundene
    Metadatenprojektion übergibt das dauerhaft betriebene Repository.
    """

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("Bootstrap-Git-Rechteprojektion verlangt Root")
    try:
        account = pwd.getpwnam(str(install_user))
    except KeyError as exc:
        raise RuntimeError("Installationsbenutzer für Git-Projektion fehlt") from exc
    if account.pw_uid == 0:
        raise RuntimeError("Installationsbenutzer für Git-Projektion darf nicht Root sein")

    root = os.path.abspath(repo_dir)
    root_descriptor = _open_directory_nofollow(root)
    git_descriptor: int | None = None
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

    def metadata_identity(metadata) -> tuple[int, int]:
        return int(metadata.st_dev), int(metadata.st_ino)

    def require_root_preimage(metadata, label: str, *, directory: bool) -> None:
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            not expected_type(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (not directory and metadata.st_nlink != 1)
        ):
            raise RuntimeError(
                "Frische Git-Metadaten besitzen keine eindeutige Root-Autorität: "
                + label
            )

    def project_directory(descriptor: int, label: str) -> None:
        opened_before = os.fstat(descriptor)
        require_root_preimage(opened_before, label, directory=True)
        names_before = tuple(sorted(os.listdir(descriptor)))
        for name in names_before:
            if (
                not name
                or name in {".", ".."}
                or "/" in name
                or "\\" in name
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
            ):
                raise RuntimeError("Git-Metadaten enthalten einen unzulässigen Namen")
            named_before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            child_label = f"{label}/{name}"
            if stat.S_ISDIR(named_before.st_mode):
                child = os.open(name, directory_flags, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    if metadata_identity(opened) != metadata_identity(named_before):
                        raise RuntimeError(
                            "Git-Metadatenverzeichnis wurde ausgetauscht: " + child_label
                        )
                    project_directory(child, child_label)
                    after = os.fstat(child)
                    named_after = os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        metadata_identity(after) != metadata_identity(opened)
                        or metadata_identity(named_after) != metadata_identity(after)
                        or after.st_uid != account.pw_uid
                        or after.st_gid != account.pw_gid
                        or stat.S_IMODE(after.st_mode) != 0o700
                    ):
                        raise RuntimeError(
                            "Git-Metadatenverzeichnis driftete bei der Projektion: "
                            + child_label
                        )
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(named_before.st_mode):
                raise RuntimeError(
                    "Git-Metadaten enthalten einen Symlink oder Spezialpfad: "
                    + child_label
                )
            require_root_preimage(named_before, child_label, directory=False)
            child = os.open(name, file_flags, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                stable = (
                    int(opened.st_dev),
                    int(opened.st_ino),
                    int(opened.st_size),
                    int(opened.st_mtime_ns),
                    int(opened.st_nlink),
                )
                if metadata_identity(opened) != metadata_identity(named_before):
                    raise RuntimeError(
                        "Git-Metadatendatei wurde ausgetauscht: " + child_label
                    )
                require_root_preimage(opened, child_label, directory=False)
                os.fchown(child, account.pw_uid, account.pw_gid)
                os.fchmod(child, 0o600)
                os.fsync(child)
                after = os.fstat(child)
                named_after = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                current = (
                    int(after.st_dev),
                    int(after.st_ino),
                    int(after.st_size),
                    int(after.st_mtime_ns),
                    int(after.st_nlink),
                )
                if (
                    current != stable
                    or metadata_identity(named_after) != metadata_identity(after)
                    or after.st_uid != account.pw_uid
                    or after.st_gid != account.pw_gid
                    or stat.S_IMODE(after.st_mode) != 0o600
                ):
                    raise RuntimeError(
                        "Git-Metadatendatei driftete bei der Projektion: " + child_label
                    )
            finally:
                os.close(child)
        if tuple(sorted(os.listdir(descriptor))) != names_before:
            raise RuntimeError("Git-Metadatenbaum driftete während der Projektion: " + label)
        os.fchown(descriptor, account.pw_uid, account.pw_gid)
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if (
            metadata_identity(after) != metadata_identity(opened_before)
            or after.st_uid != account.pw_uid
            or after.st_gid != account.pw_gid
            or stat.S_IMODE(after.st_mode) != 0o700
        ):
            raise RuntimeError("Git-Metadatenverzeichnis blieb unvollständig: " + label)

    try:
        root_before = os.fstat(root_descriptor)
        git_before = os.stat(".git", dir_fd=root_descriptor, follow_symlinks=False)
        require_root_preimage(git_before, ".git", directory=True)
        git_descriptor = os.open(".git", directory_flags, dir_fd=root_descriptor)
        if metadata_identity(os.fstat(git_descriptor)) != metadata_identity(git_before):
            raise RuntimeError("Git-Metadatenwurzel wurde vor der Projektion ausgetauscht")
        project_directory(git_descriptor, ".git")
        git_after = os.fstat(git_descriptor)
        named_after = os.stat(".git", dir_fd=root_descriptor, follow_symlinks=False)
        root_after = os.fstat(root_descriptor)
        if (
            metadata_identity(git_after) != metadata_identity(git_before)
            or metadata_identity(named_after) != metadata_identity(git_after)
            or metadata_identity(root_after) != metadata_identity(root_before)
        ):
            raise RuntimeError("Git-Metadatenwurzel driftete bei der Rechteprojektion")
    finally:
        if git_descriptor is not None:
            os.close(git_descriptor)
        os.close(root_descriptor)


def _secure_repo_permissions(
    repo_dir: str,
    install_user: str,
    *,
    expected_commit: str | None = None,
    recovery_backup_dir: str | None = None,
    recovery_repo_contract: RepoRecoveryContract | None = None,
    recovery_backup_receipt: RecoveryBackupReceipt | None = None,
    root_git_authority: bool = False,
) -> None:
    """Härtet ausschließlich den von Git gebundenen Produktbaum.

    Unversionierte Laufzeitdaten können absichtlich innerhalb des
    Installationsbaums liegen, etwa der private Matter-Zustand. Sie gehören
    nicht zum Release und dürfen deshalb weder rekursiv umgehängt noch auf
    allgemeine Repository-Rechte aufgeweitet werden.
    """
    root = os.path.abspath(repo_dir)
    account = pwd.getpwnam(str(install_user))
    if not isinstance(root_git_authority, bool):
        raise ValueError("Git-Rechteprojektionsautorität muss boolesch sein")
    if root_git_authority and (
        not hasattr(os, "geteuid") or os.geteuid() != 0
    ):
        raise RuntimeError("Bootstrap-Git-Rechteprojektion verlangt Root")
    bound_commit = (
        _validate_full_commit(expected_commit)
        if expected_commit is not None
        else _bound_release_head_commit(
            root,
            install_user,
            root_authority=root_git_authority,
        )
    )
    if _bound_release_head_commit(
        root,
        install_user,
        root_authority=root_git_authority,
    ) != bound_commit:
        raise RuntimeError(
            "Repository-HEAD weicht vor der Rechtehärtung vom gebundenen "
            "Produkt-Commit ab"
        )
    tracked_entries = _tracked_release_file_contracts(
        root,
        install_user,
        target_commit=bound_commit,
        root_authority=root_git_authority,
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
        if root_git_authority:
            _project_bootstrap_git_metadata_permissions(root, install_user)
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
        if root_git_authority:
            user_head = _git_argv_as_install_user(
                root,
                install_user,
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
                timeout=15,
            )
            if (
                not user_head.get("success")
                or not _exact_commit_matches(
                    bound_commit,
                    str(user_head.get("stdout") or "").strip(),
                )
            ):
                raise RuntimeError(
                    "Projizierte Git-Metadaten sind für den Installationsbenutzer "
                    "nicht commitgebunden lesbar"
                )
            user_entries = _tracked_release_file_contracts(
                root,
                install_user,
                target_commit=bound_commit,
            )
            if user_entries != tracked_entries:
                raise RuntimeError(
                    "Projizierte Git-Objekte weichen unter der Zielidentität "
                    "vom Root-gebundenen Dateivertrag ab"
                )
        elif _bound_release_head_commit(root, install_user) != bound_commit:
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


def _installed_apt_packages() -> dict[str, str]:
    result = _run_argv(
        [
            "dpkg-query",
            "-W",
            "-f=${binary:Package}\t${Version}\t${db:Status-Abbrev}\n",
        ],
        timeout=60,
    )
    if not result["success"]:
        raise RuntimeError("Installierter apt-Paketstand ist nicht lesbar")
    installed: dict[str, str] = {}
    for line in result["stdout"].splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[2].startswith("ii "):
            continue
        name = parts[0].strip()
        version = parts[1].strip()
        if (
            not name
            or not version
            or name in installed
            or any(char in name + version for char in "\x00\r\n\t")
        ):
            raise RuntimeError("Installierter apt-Paketstand ist mehrdeutig")
        installed[name] = version
    if not installed:
        raise RuntimeError("Installierter apt-Paketstand ist leer oder unplausibel")
    return installed


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


def _apt_binary_names_for_candidates(
    installed: dict[str, str],
    candidates,
) -> tuple[str, ...]:
    """Löst Debian-Paketnamen eindeutig auf ihren installierten Binärnamen auf."""

    resolved = []
    for raw_candidate in candidates:
        candidate = str(raw_candidate or "").strip()
        if not PACKAGE_NAME_RE.fullmatch(candidate):
            raise RuntimeError("Offline-APT-Kandidat besitzt einen ungültigen Paketnamen")
        matches = sorted(
            name
            for name in installed
            if name == candidate or name.partition(":")[0] == candidate
        )
        if len(matches) > 1:
            raise RuntimeError(
                f"APT-Paket {candidate} ist über mehrere Architekturen mehrdeutig"
            )
        if matches and matches[0] not in resolved:
            resolved.append(matches[0])
    return tuple(resolved)


def _apt_archive_candidate_packages(
    receipt: OfflinePackageReceipt,
) -> tuple[str, ...]:
    """Bindet alle durch die versiegelten DEBs überhaupt mutierbaren Pakete."""

    verify_preparation(receipt)
    candidates = []
    for archive in receipt.apt_archives:
        result = _run_argv(
            ["/usr/bin/dpkg-deb", "--field", archive, "Package"],
            timeout=30,
        )
        name = str(result.get("stdout") or "").strip()
        if (
            not result.get("success")
            or not PACKAGE_NAME_RE.fullmatch(name)
            or name in candidates
        ):
            raise RuntimeError(
                f"Offline-DEB besitzt keinen eindeutigen Paketvertrag: {archive}"
            )
        candidates.append(name)
    return tuple(sorted(candidates))


def _bind_package_transaction_to_offline_receipt(
    state: PackageTransactionState,
    receipt: OfflinePackageReceipt,
) -> PackageTransactionState:
    """Erweitert das Preimage nur um manifestgebundene APT-Mutationskandidaten."""

    verified = verify_preparation(receipt)
    expected_venv_state, _expected_venv_path = _finalizer_venv_contract(state)
    offline_expected_venv_state = (
        expected_venv_state
        if expected_venv_state in {"present", "missing"}
        else "present"
    )
    if (
        verified.apt_packages != state.apt_requested
        or verified.pip_packages != state.pip_requested
        or verified.expected_venv_state != offline_expected_venv_state
    ):
        raise RuntimeError("Offline-Paketreceipt widerspricht dem Paket-Preimage")
    return replace(
        state,
        apt_candidate_packages=_apt_archive_candidate_packages(verified),
    )


def _prepared_package_state_mapping(state: PreparedPackageState) -> dict:
    return {
        "schema": PREPARED_PACKAGE_STATE_SCHEMA,
        "transaction_id": state.transaction_id,
        "offline_receipt_json": state.offline_receipt_json,
        "offline_receipt_sha256": state.offline_receipt_sha256,
        "apt_after": [list(item) for item in state.apt_after],
        "pip_after": [list(item) for item in state.pip_after],
        "venv_python": state.venv_python,
        "install_user": state.install_user,
        "apt_requested": list(state.apt_requested),
        "pip_requested": list(state.pip_requested),
    }


def _canonical_prepared_package_state_json(mapping: dict) -> str:
    try:
        payload = json.dumps(
            mapping,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeError("Vorbereiteter Paketvertrag ist nicht kanonisch") from exc
    if len(payload.encode("utf-8")) > 512 * 1024 or "\x00" in payload:
        raise RuntimeError("Vorbereiteter Paketvertrag ist unplausibel groß")
    return payload


def _validate_package_version_rows(raw, *, label: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        raise RuntimeError(f"{label} ist keine Paketversionsliste")
    rows = []
    for entry in raw:
        if not isinstance(entry, list) or len(entry) != 2:
            raise RuntimeError(f"{label} enthält keinen Paketversionssatz")
        name, version = entry
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not name
            or not version
            or any(char in name + version for char in "\x00\r\n\t")
            or any(previous[0] == name for previous in rows)
        ):
            raise RuntimeError(f"{label} enthält mehrdeutige Paketmetadaten")
        rows.append((name, version))
    if rows != sorted(rows):
        raise RuntimeError(f"{label} ist nicht kanonisch sortiert")
    return tuple(rows)


def _validate_package_name_list(raw, *, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise RuntimeError(f"{label} ist keine Paketliste")
    values = []
    for item in raw:
        if (
            not isinstance(item, str)
            or not PACKAGE_NAME_RE.fullmatch(item)
            or item in values
        ):
            raise RuntimeError(f"{label} enthält einen ungültigen Paketnamen")
        values.append(item)
    return tuple(values)


def _serialize_prepared_package_state(state: PreparedPackageState) -> str:
    if not isinstance(state, PreparedPackageState):
        raise TypeError("Vorbereiteter Paketvertrag fehlt")
    receipt_payload = state.offline_receipt_json.encode("utf-8")
    if hashlib.sha256(receipt_payload).hexdigest() != state.offline_receipt_sha256:
        raise RuntimeError("Offline-Receipt-Hash widerspricht dem Paketvertrag")
    receipt = parse_offline_package_receipt(receipt_payload)
    if (
        receipt.cache.transaction_id != state.transaction_id
        or receipt.apt_packages != state.apt_requested
        or receipt.pip_packages != state.pip_requested
    ):
        raise RuntimeError("Offline-Receipt widerspricht dem vorbereiteten Paketvertrag")
    payload = _canonical_prepared_package_state_json(
        _prepared_package_state_mapping(state)
    )
    if _parse_prepared_package_state(payload) != state:
        raise RuntimeError("Vorbereiteter Paketvertrag ist nicht roundtrip-stabil")
    return payload


def _parse_prepared_package_state(payload: str) -> PreparedPackageState:
    if not isinstance(payload, str) or not payload or "\x00" in payload:
        raise RuntimeError("Vorbereiteter Paketvertrag fehlt oder ist ungültig")
    try:
        mapping = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Vorbereiteter Paketvertrag ist kein gültiges JSON") from exc
    required = {
        "schema",
        "transaction_id",
        "offline_receipt_json",
        "offline_receipt_sha256",
        "apt_after",
        "pip_after",
        "venv_python",
        "install_user",
        "apt_requested",
        "pip_requested",
    }
    if (
        not isinstance(mapping, dict)
        or set(mapping) != required
        or mapping.get("schema") != PREPARED_PACKAGE_STATE_SCHEMA
        or _canonical_prepared_package_state_json(mapping) != payload
    ):
        raise RuntimeError("Vorbereiteter Paketvertrag besitzt kein exaktes Schema")
    transaction_id = str(mapping.get("transaction_id") or "")
    receipt_json = mapping.get("offline_receipt_json")
    receipt_sha256 = str(mapping.get("offline_receipt_sha256") or "")
    install_user = str(mapping.get("install_user") or "")
    venv_python = mapping.get("venv_python")
    if (
        not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(transaction_id)
        or not isinstance(receipt_json, str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt_sha256)
        or not install_user
        or any(char in install_user for char in "\x00\r\n\t/")
        or (
            venv_python is not None
            and (
                not isinstance(venv_python, str)
                or not os.path.isabs(venv_python)
                or os.path.abspath(venv_python) != venv_python
            )
        )
    ):
        raise RuntimeError("Vorbereiteter Paketvertrag enthält ungültige Bindungsfelder")
    receipt_payload = receipt_json.encode("utf-8")
    if hashlib.sha256(receipt_payload).hexdigest() != receipt_sha256:
        raise RuntimeError("Vorbereiteter Paketvertrag verlor seinen Receipt-Hash")
    receipt = parse_offline_package_receipt(receipt_payload)
    state = PreparedPackageState(
        transaction_id=transaction_id,
        offline_receipt_json=receipt_json,
        offline_receipt_sha256=receipt_sha256,
        apt_after=_validate_package_version_rows(
            mapping["apt_after"],
            label="apt_after",
        ),
        pip_after=_validate_package_version_rows(
            mapping["pip_after"],
            label="pip_after",
        ),
        venv_python=venv_python,
        install_user=install_user,
        apt_requested=_validate_package_name_list(
            mapping["apt_requested"],
            label="apt_requested",
        ),
        pip_requested=_validate_package_name_list(
            mapping["pip_requested"],
            label="pip_requested",
        ),
    )
    if (
        receipt.cache.transaction_id != transaction_id
        or receipt.apt_packages != state.apt_requested
        or receipt.pip_packages != state.pip_requested
    ):
        raise RuntimeError("Vorbereiteter Paketvertrag und Offline-Receipt widersprechen sich")
    return state


def _package_transaction_state_mapping(state: PackageTransactionState) -> dict:
    if not isinstance(state, PackageTransactionState):
        raise TypeError("Paket-Prestate fehlt")
    return {
        "schema": PACKAGE_TRANSACTION_STATE_SCHEMA,
        "apt_before": [list(item) for item in state.apt_before],
        "pip_before": [list(item) for item in state.pip_before],
        "venv_python": state.venv_python,
        "install_user": state.install_user,
        "apt_requested": list(state.apt_requested),
        "pip_requested": list(state.pip_requested),
        "apt_candidate_packages": list(state.apt_candidate_packages),
        "venv_path": state.venv_path,
        "venv_existed": state.venv_existed,
        "runtime_venv_required": state.runtime_venv_required,
    }


def _parse_package_transaction_state(mapping: object) -> PackageTransactionState:
    required = {
        "schema",
        "apt_before",
        "pip_before",
        "venv_python",
        "install_user",
        "apt_requested",
        "pip_requested",
        "apt_candidate_packages",
        "venv_path",
        "venv_existed",
        "runtime_venv_required",
    }
    if (
        not isinstance(mapping, dict)
        or set(mapping) != required
        or mapping.get("schema") != PACKAGE_TRANSACTION_STATE_SCHEMA
    ):
        raise RuntimeError("Paket-Prestate besitzt kein exaktes Schema")
    install_user = str(mapping.get("install_user") or "")
    venv_python = mapping.get("venv_python")
    venv_path = mapping.get("venv_path")
    venv_existed = mapping.get("venv_existed")
    runtime_required = mapping.get("runtime_venv_required")
    for value, label in (
        (venv_python, "venv_python"),
        (venv_path, "venv_path"),
    ):
        if value is not None and (
            not isinstance(value, str)
            or not os.path.isabs(value)
            or os.path.abspath(value) != value
            or any(char in value for char in "\x00\r\n\t")
            or (label == "venv_path" and os.path.realpath(value) != value)
        ):
            raise RuntimeError(f"Paket-Prestate besitzt keinen kanonischen {label}")
    if (
        not install_user
        or any(char in install_user for char in "\x00\r\n\t/")
        or not isinstance(venv_existed, bool)
        or not isinstance(runtime_required, bool)
    ):
        raise RuntimeError("Paket-Prestate besitzt ungültige Bindungsfelder")
    state = PackageTransactionState(
        apt_before=_validate_package_version_rows(
            mapping["apt_before"],
            label="apt_before",
        ),
        pip_before=_validate_package_version_rows(
            mapping["pip_before"],
            label="pip_before",
        ),
        venv_python=venv_python,
        install_user=install_user,
        apt_requested=_validate_package_name_list(
            mapping["apt_requested"],
            label="apt_requested",
        ),
        pip_requested=_validate_package_name_list(
            mapping["pip_requested"],
            label="pip_requested",
        ),
        apt_candidate_packages=_validate_package_name_list(
            mapping["apt_candidate_packages"],
            label="apt_candidate_packages",
        ),
        venv_path=venv_path,
        venv_existed=venv_existed,
        runtime_venv_required=runtime_required,
    )
    venv_contract_needed = bool(state.pip_requested or state.runtime_venv_required)
    if (
        (not state.apt_requested and (state.apt_before or state.apt_candidate_packages))
        or (not state.pip_requested and state.pip_before)
        or (state.runtime_venv_required and not state.venv_existed)
        or (
            state.venv_existed
            and venv_contract_needed
            and (not state.venv_python or not state.venv_path)
        )
        or (
            not state.venv_existed
            and (state.venv_python is not None or (state.pip_requested and not state.venv_path))
        )
        or (
            not venv_contract_needed
            and (state.venv_python is not None or state.venv_path is not None)
        )
        or (
            state.venv_python is not None
            and state.venv_path is not None
            and os.path.dirname(os.path.dirname(state.venv_python)) != state.venv_path
        )
        or _package_transaction_state_mapping(state) != mapping
    ):
        raise RuntimeError("Paket-Prestate widerspricht seinem Rücklaufvertrag")
    return state


def _canonical_prepared_package_receipt_payload(payload: dict) -> bytes:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeError("Paket-Receipt-Payload ist nicht kanonisch") from exc


def _prepared_package_receipt_record(
    *,
    state: str,
    transaction_id: str,
    install_root: str,
    full_backup_id: str,
    package_transaction: PackageTransactionState,
    prepared_state: PreparedPackageState | None,
    target_commit: str,
    target_tag: str,
    role: str,
    apache_available: bool,
    apache_was_active: bool,
    apache_unit_file_state: str,
    static_recovery_contract_json: str,
) -> dict:
    transaction = str(transaction_id or "")
    root = os.path.abspath(str(install_root or ""))
    backup_id = str(full_backup_id or "")
    unit_state = str(apache_unit_file_state or "").strip().lower()
    accepted_apache_unit_states = {
        "enabled", "enabled-runtime", "disabled", "static", "indirect",
        "masked", "masked-runtime", "generated", "transient", "alias",
        "linked", "linked-runtime",
    }
    if (
        state not in {"applying", "prepared", "committed"}
        or not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(transaction)
        or not os.path.isabs(root)
        or root != str(install_root or "")
        or os.path.realpath(root) != root
        or not re.fullmatch(r"[0-9a-f]{64}", backup_id)
        or (state == "applying") != (prepared_state is None)
        or not isinstance(apache_available, bool)
        or not isinstance(apache_was_active, bool)
        or (apache_was_active and not apache_available)
        or (
            not apache_available
            and (apache_was_active or unit_state != "absent")
        )
        or (apache_available and unit_state not in accepted_apache_unit_states)
    ):
        raise RuntimeError("Paket-Receipt besitzt keinen vollständigen Transaktionsanker")
    prestate_mapping = _package_transaction_state_mapping(package_transaction)
    if _parse_package_transaction_state(prestate_mapping) != package_transaction:
        raise RuntimeError("Paket-Prestate ist vor der Persistierung nicht roundtrip-stabil")
    if prepared_state is not None:
        prepared_state = _parse_prepared_package_state(
            _serialize_prepared_package_state(prepared_state)
        )
        if (
            prepared_state.transaction_id != transaction
            or prepared_state.install_user != package_transaction.install_user
            or prepared_state.apt_requested != package_transaction.apt_requested
            or prepared_state.pip_requested != package_transaction.pip_requested
        ):
            raise RuntimeError("Paket-Poststate widerspricht seinem Prestate")
    payload = {
        "prestate": prestate_mapping,
        "poststate": (
            _prepared_package_state_mapping(prepared_state)
            if prepared_state is not None
            else None
        ),
    }
    payload_sha256 = hashlib.sha256(
        _canonical_prepared_package_receipt_payload(payload)
    ).hexdigest()
    commit = _validate_full_commit(target_commit)
    tag = _normalize_release_tag(target_tag)
    bound_role = str(role or "").strip().lower()
    if bound_role not in VALID_HA_ROLES:
        raise RuntimeError("Paket-Receipt besitzt keine gültige Zielrolle")
    static_contract = str(static_recovery_contract_json or "")
    if static_contract:
        _parse_recovery_bootblock_contract(static_contract)
    return {
        "schema": PREPARED_PACKAGE_RECEIPT_SCHEMA,
        "state": state,
        "transaction_id": transaction,
        "install_root": root,
        "full_backup_id": backup_id,
        "payload_sha256": payload_sha256,
        "payload": payload,
        "completion": {
            "target_commit": commit,
            "target_tag": tag,
            "role": bound_role,
            "apache": {
                "completion_required": True,
                "available": apache_available,
                "was_active": apache_was_active,
                "unit_file_state": unit_state,
            },
            "static_recovery_contract_json": static_contract,
        },
    }


def _canonical_prepared_package_receipt_bytes(record: dict) -> bytes:
    payload = _canonical_prepared_package_receipt_payload(record) + b"\n"
    if len(payload) > PREPARED_PACKAGE_RECEIPT_MAX_BYTES:
        raise RuntimeError("Paket-Receipt ist unplausibel groß")
    return payload


def _parse_package_completion_record(
    completion: object,
    *,
    transaction_id: str,
) -> tuple[str, str, str, bool, bool, str, str]:
    if not isinstance(completion, dict) or set(completion) != {
        "target_commit", "target_tag", "role", "apache",
        "static_recovery_contract_json",
    }:
        raise RuntimeError("Paket-Receipt besitzt keinen Abschlussvertrag")
    apache = completion.get("apache")
    if not isinstance(apache, dict) or set(apache) != {
        "completion_required", "available", "was_active", "unit_file_state"
    }:
        raise RuntimeError("Paket-Receipt besitzt keinen Apache-Abschlussvertrag")
    commit = _validate_full_commit(str(completion.get("target_commit") or ""))
    tag = _normalize_release_tag(str(completion.get("target_tag") or ""))
    role = str(completion.get("role") or "").strip().lower()
    available = apache.get("available")
    was_active = apache.get("was_active")
    unit_state = str(apache.get("unit_file_state") or "").strip().lower()
    accepted_states = {
        "enabled", "enabled-runtime", "disabled", "static", "indirect",
        "masked", "masked-runtime", "generated", "transient", "alias",
        "linked", "linked-runtime",
    }
    static_json = str(completion.get("static_recovery_contract_json") or "")
    if (
        role not in VALID_HA_ROLES
        or apache.get("completion_required") is not True
        or not isinstance(available, bool)
        or not isinstance(was_active, bool)
        or (was_active and not available)
        or (not available and (was_active or unit_state != "absent"))
        or (available and unit_state not in accepted_states)
    ):
        raise RuntimeError("Paket-Receipt besitzt ungültige Abschlussfelder")
    if static_json:
        static_contract = _parse_recovery_bootblock_contract(static_json)
        if static_contract.transaction_id != transaction_id:
            raise RuntimeError("Paket-Receipt und statischer Bootblock widersprechen sich")
    return commit, tag, role, available, was_active, unit_state, static_json


def _parse_package_recovery_receipt(
    payload: bytes,
    metadata: os.stat_result,
) -> PackageRecoveryReceipt:
    """Parst den Rücklauf-Prestate ohne flüchtigen Offline-Cache-Readback."""

    try:
        record = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Paket-Recovery-Receipt ist nicht kanonisch lesbar") from exc
    required = {
        "schema",
        "state",
        "transaction_id",
        "install_root",
        "full_backup_id",
        "payload_sha256",
        "payload",
        "completion",
    }
    state_payload = record.get("payload") if isinstance(record, dict) else None
    state = str(record.get("state") or "") if isinstance(record, dict) else ""
    transaction_id = (
        str(record.get("transaction_id") or "") if isinstance(record, dict) else ""
    )
    install_root = (
        str(record.get("install_root") or "") if isinstance(record, dict) else ""
    )
    full_backup_id = (
        str(record.get("full_backup_id") or "") if isinstance(record, dict) else ""
    )
    payload_sha256 = (
        str(record.get("payload_sha256") or "") if isinstance(record, dict) else ""
    )
    poststate = state_payload.get("poststate") if isinstance(state_payload, dict) else None
    prepared_keys = {
        "schema",
        "transaction_id",
        "offline_receipt_json",
        "offline_receipt_sha256",
        "apt_after",
        "pip_after",
        "venv_python",
        "install_user",
        "apt_requested",
        "pip_requested",
    }
    if (
        not isinstance(record, dict)
        or set(record) != required
        or record.get("schema") != PREPARED_PACKAGE_RECEIPT_SCHEMA
        or _canonical_prepared_package_receipt_bytes(record) != payload
        or state not in {"applying", "prepared", "committed"}
        or not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(transaction_id)
        or not os.path.isabs(install_root)
        or os.path.abspath(install_root) != install_root
        or os.path.realpath(install_root) != install_root
        or not re.fullmatch(r"[0-9a-f]{64}", full_backup_id)
        or not re.fullmatch(r"[0-9a-f]{64}", payload_sha256)
        or not isinstance(state_payload, dict)
        or set(state_payload) != {"prestate", "poststate"}
        or hashlib.sha256(
            _canonical_prepared_package_receipt_payload(state_payload)
        ).hexdigest()
        != payload_sha256
        or (state == "applying" and poststate is not None)
        or (
            state in {"prepared", "committed"}
            and (
                not isinstance(poststate, dict)
                or set(poststate) != prepared_keys
                or poststate.get("schema") != PREPARED_PACKAGE_STATE_SCHEMA
            )
        )
    ):
        raise RuntimeError("Paket-Recovery-Receipt besitzt ungültige Bindungsfelder")
    (
        target_commit,
        target_tag,
        role,
        apache_available,
        apache_was_active,
        apache_unit_file_state,
        static_recovery_contract_json,
    ) = _parse_package_completion_record(
        record.get("completion"),
        transaction_id=transaction_id,
    )
    return PackageRecoveryReceipt(
        state=state,
        transaction_id=transaction_id,
        install_root=install_root,
        full_backup_id=full_backup_id,
        payload_sha256=payload_sha256,
        package_transaction=_parse_package_transaction_state(
            state_payload["prestate"]
        ),
        receipt_path=os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            PREPARED_PACKAGE_RECEIPT_NAME,
        ),
        receipt_dev=int(metadata.st_dev),
        receipt_ino=int(metadata.st_ino),
        receipt_sha256=hashlib.sha256(payload).hexdigest(),
        target_commit=target_commit,
        target_tag=target_tag,
        role=role,
        apache_completion_required=True,
        apache_available=apache_available,
        apache_was_active=apache_was_active,
        apache_unit_file_state=apache_unit_file_state,
        static_recovery_contract_json=static_recovery_contract_json,
    )


def _parse_prepared_package_receipt(
    payload: bytes,
    metadata: os.stat_result,
) -> PreparedPackageReceipt:
    recovery = _parse_package_recovery_receipt(payload, metadata)
    try:
        record = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Paket-Receipt ist nicht kanonisch lesbar") from exc
    required = {
        "schema",
        "state",
        "transaction_id",
        "install_root",
        "full_backup_id",
        "payload_sha256",
        "payload",
        "completion",
    }
    if (
        not isinstance(record, dict)
        or set(record) != required
        or record.get("schema") != PREPARED_PACKAGE_RECEIPT_SCHEMA
        or _canonical_prepared_package_receipt_bytes(record) != payload
    ):
        raise RuntimeError("Paket-Receipt besitzt kein exaktes kanonisches Schema")
    state = str(record.get("state") or "")
    transaction_id = str(record.get("transaction_id") or "")
    install_root = str(record.get("install_root") or "")
    full_backup_id = str(record.get("full_backup_id") or "")
    payload_sha256 = str(record.get("payload_sha256") or "")
    state_payload = record.get("payload")
    if (
        state not in {"applying", "prepared", "committed"}
        or not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(transaction_id)
        or not os.path.isabs(install_root)
        or os.path.abspath(install_root) != install_root
        or os.path.realpath(install_root) != install_root
        or not re.fullmatch(r"[0-9a-f]{64}", full_backup_id)
        or not re.fullmatch(r"[0-9a-f]{64}", payload_sha256)
        or not isinstance(state_payload, dict)
        or set(state_payload) != {"prestate", "poststate"}
        or hashlib.sha256(
            _canonical_prepared_package_receipt_payload(state_payload)
        ).hexdigest()
        != payload_sha256
    ):
        raise RuntimeError("Paket-Receipt besitzt ungültige Bindungsfelder")
    package_transaction = _parse_package_transaction_state(
        state_payload["prestate"]
    )
    poststate_mapping = state_payload["poststate"]
    prepared_state = None
    if poststate_mapping is not None:
        prepared_state = _parse_prepared_package_state(
            _canonical_prepared_package_state_json(poststate_mapping)
        )
    if (
        (state == "applying") != (prepared_state is None)
        or (
            prepared_state is not None
            and (
                prepared_state.transaction_id != transaction_id
                or prepared_state.install_user != package_transaction.install_user
                or prepared_state.apt_requested != package_transaction.apt_requested
                or prepared_state.pip_requested != package_transaction.pip_requested
            )
        )
    ):
        raise RuntimeError("Paket-Receipt verlor seinen Pre-/Postzustandsvertrag")
    if package_transaction != recovery.package_transaction:
        raise RuntimeError("Paket-Receipt-Prestate driftete zwischen den Parsern")
    return PreparedPackageReceipt(
        state=state,
        transaction_id=transaction_id,
        install_root=install_root,
        full_backup_id=full_backup_id,
        payload_sha256=payload_sha256,
        package_transaction=package_transaction,
        prepared_state=prepared_state,
        receipt_path=os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            PREPARED_PACKAGE_RECEIPT_NAME,
        ),
        receipt_dev=int(metadata.st_dev),
        receipt_ino=int(metadata.st_ino),
        receipt_sha256=hashlib.sha256(payload).hexdigest(),
        target_commit=recovery.target_commit,
        target_tag=recovery.target_tag,
        role=recovery.role,
        apache_completion_required=recovery.apache_completion_required,
        apache_available=recovery.apache_available,
        apache_was_active=recovery.apache_was_active,
        apache_unit_file_state=recovery.apache_unit_file_state,
        static_recovery_contract_json=recovery.static_recovery_contract_json,
    )


def _read_prepared_package_receipt(
    *,
    allow_missing: bool = False,
) -> PreparedPackageReceipt | None:
    path = os.path.join(
        RECOVERY_BOOTBLOCK_STATE_DIR,
        PREPARED_PACKAGE_RECEIPT_NAME,
    )
    if not os.path.lexists(path):
        if allow_missing:
            return None
        raise RuntimeError("Paket-Receipt fehlt")
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        readback = _read_bound_root_file_at(
            state_descriptor,
            PREPARED_PACKAGE_RECEIPT_NAME,
            maximum=PREPARED_PACKAGE_RECEIPT_MAX_BYTES,
            mode=0o600,
            allow_missing=allow_missing,
        )
        if readback is None:
            return None
        return _parse_prepared_package_receipt(*readback)
    finally:
        os.close(state_descriptor)


def _read_package_recovery_receipt(
    *,
    allow_missing: bool = False,
) -> PackageRecoveryReceipt | None:
    """Liest nur den dauerhaften Prestate; /run-Cache darf bereits fehlen."""

    path = os.path.join(
        RECOVERY_BOOTBLOCK_STATE_DIR,
        PREPARED_PACKAGE_RECEIPT_NAME,
    )
    if not os.path.lexists(path):
        if allow_missing:
            return None
        raise RuntimeError("Paket-Recovery-Receipt fehlt")
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        readback = _read_bound_root_file_at(
            state_descriptor,
            PREPARED_PACKAGE_RECEIPT_NAME,
            maximum=PREPARED_PACKAGE_RECEIPT_MAX_BYTES,
            mode=0o600,
            allow_missing=allow_missing,
        )
        if readback is None:
            return None
        return _parse_package_recovery_receipt(*readback)
    finally:
        os.close(state_descriptor)


def _require_prepared_package_receipt_binding(
    *,
    expected_state: str,
    expected_transaction_id: str | None,
    expected_install_root: str,
    expected_full_backup_id: str,
    expected_receipt_sha256: str,
    expected_receipt_dev: int,
    expected_receipt_ino: int,
) -> PreparedPackageReceipt:
    expected_root = os.path.abspath(str(expected_install_root or ""))
    if (
        expected_state not in {"applying", "prepared", "committed"}
        or (
            expected_transaction_id is not None
            and not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(
                str(expected_transaction_id or "")
            )
        )
        or not os.path.isabs(expected_root)
        or expected_root != str(expected_install_root or "")
        or os.path.realpath(expected_root) != expected_root
        or not re.fullmatch(r"[0-9a-f]{64}", str(expected_full_backup_id or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(expected_receipt_sha256 or ""))
        or isinstance(expected_receipt_dev, bool)
        or not isinstance(expected_receipt_dev, int)
        or expected_receipt_dev < 0
        or isinstance(expected_receipt_ino, bool)
        or not isinstance(expected_receipt_ino, int)
        or expected_receipt_ino <= 0
    ):
        raise RuntimeError("Paket-Receipt-Handoff besitzt ungültige Metadaten")
    current = _read_prepared_package_receipt()
    if (
        current is None
        or current.state != expected_state
        or (
            expected_transaction_id is not None
            and current.transaction_id != str(expected_transaction_id or "")
        )
        or current.install_root != expected_root
        or current.full_backup_id != str(expected_full_backup_id or "")
        or current.receipt_sha256 != str(expected_receipt_sha256 or "")
        or current.receipt_dev != expected_receipt_dev
        or current.receipt_ino != expected_receipt_ino
    ):
        raise RuntimeError("Paket-Receipt oder sein Inode driftete vom Handoff")
    return current


def _validate_prepared_package_receipt(
    contract: PreparedPackageReceipt,
    *,
    expected_state: str | None = None,
) -> PreparedPackageReceipt:
    if not isinstance(contract, PreparedPackageReceipt):
        raise RuntimeError("Paket-Receipt besitzt den falschen Typ")
    state = expected_state or contract.state
    current = _require_prepared_package_receipt_binding(
        expected_state=state,
        expected_transaction_id=contract.transaction_id,
        expected_install_root=contract.install_root,
        expected_full_backup_id=contract.full_backup_id,
        expected_receipt_sha256=contract.receipt_sha256,
        expected_receipt_dev=contract.receipt_dev,
        expected_receipt_ino=contract.receipt_ino,
    )
    if current != contract:
        raise RuntimeError("Paket-Receipt-Payload driftete vom gebundenen Vertrag")
    return current


def _same_prepared_package_transaction_shape(
    first: PreparedPackageReceipt,
    second: PreparedPackageReceipt,
) -> bool:
    if not isinstance(first, PreparedPackageReceipt) or not isinstance(
        second,
        PreparedPackageReceipt,
    ):
        return False
    ignored = {"state", "receipt_dev", "receipt_ino", "receipt_sha256"}
    return all(
        getattr(first, name) == getattr(second, name)
        for name in PreparedPackageReceipt.__dataclass_fields__
        if name not in ignored
    )


def _same_package_receipt_recovery_anchor(
    first: PreparedPackageReceipt,
    second: PreparedPackageReceipt,
) -> bool:
    """Vergleicht die unveränderlichen Anker über applying -> prepared."""

    if not isinstance(first, PreparedPackageReceipt) or not isinstance(
        second,
        PreparedPackageReceipt,
    ):
        return False
    fields = (
        "transaction_id",
        "install_root",
        "full_backup_id",
        "package_transaction",
        "receipt_path",
        "target_commit",
        "target_tag",
        "role",
        "apache_completion_required",
        "apache_available",
        "apache_was_active",
        "apache_unit_file_state",
        "static_recovery_contract_json",
    )
    return all(getattr(first, name) == getattr(second, name) for name in fields)


def _package_receipt_transition_error(
    *,
    original_error: Exception,
    applying_receipt: PreparedPackageReceipt,
    prepared_receipt: PreparedPackageReceipt | None,
    replace_attempted: bool,
    replace_completed: bool,
) -> PreparedPackageReceiptTransitionError:
    """Wählt ausschließlich den exakten alten oder vorgestagten Receipt-Inode."""

    if not replace_attempted:
        raise RuntimeError(
            "Paket-Receipt-Fehler trat vor der atomaren Replace-Grenze auf"
        ) from original_error
    selected = applying_receipt
    if replace_completed:
        if prepared_receipt is None:
            raise RuntimeError(
                "Paket-Receipt-Replace endete ohne gebundenen prepared-Inode"
            ) from original_error
        selected = prepared_receipt
    else:
        # os.replace kann bei einer Signal-/Wrappergrenze bereits gewirkt haben,
        # obwohl der Aufruf einen Fehler meldet. Nur ein exakter Readback eines
        # der beiden gebundenen Inodes löst diese Mehrdeutigkeit auf.
        try:
            persisted = _read_prepared_package_receipt()
        except Exception as readback_error:
            raise RuntimeError(
                "Paket-Receipt-Replace blieb ohne eindeutigen Inode-Readback"
            ) from readback_error
        if persisted == applying_receipt:
            selected = applying_receipt
        elif prepared_receipt is not None and persisted == prepared_receipt:
            selected = prepared_receipt
        else:
            raise RuntimeError(
                "Paket-Receipt-Replace ergab weder den alten noch den neuen Vertrag"
            ) from original_error
    if (
        selected.state not in {"applying", "prepared"}
        or applying_receipt.state != "applying"
        or not _same_package_receipt_recovery_anchor(
            applying_receipt,
            selected,
        )
    ):
        raise RuntimeError(
            "Paket-Receipt-Transition verlor Transaktion, Installationspfad, "
            "Backup oder Paket-Prestate"
        ) from original_error
    return PreparedPackageReceiptTransitionError(
        f"Paket-Receipt-Transition blieb nach {selected.state!r} unvollständig: "
        f"{original_error}",
        selected,
    )


def _write_applying_prepared_package_receipt(
    *,
    transaction_id: str,
    install_root: str,
    full_backup_id: str,
    package_transaction: PackageTransactionState,
    target_commit: str,
    target_tag: str,
    role: str,
    apache_preimage: ApacheSecurityPreimage,
    static_recovery_contract_json: str,
) -> PreparedPackageReceipt:
    record = _prepared_package_receipt_record(
        state="applying",
        transaction_id=transaction_id,
        install_root=install_root,
        full_backup_id=full_backup_id,
        package_transaction=package_transaction,
        prepared_state=None,
        target_commit=target_commit,
        target_tag=target_tag,
        role=role,
        apache_available=apache_preimage.apache_available,
        apache_was_active=apache_preimage.apache_was_active,
        apache_unit_file_state=apache_preimage.apache_unit_file_state,
        static_recovery_contract_json=static_recovery_contract_json,
    )
    payload = _canonical_prepared_package_receipt_bytes(record)
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        metadata = _create_owned_exact_root_file_at(
            state_descriptor,
            PREPARED_PACKAGE_RECEIPT_NAME,
            payload,
            0o600,
        )
        os.fsync(state_descriptor)
        return _parse_prepared_package_receipt(payload, metadata)
    finally:
        os.close(state_descriptor)


def _replace_prepared_package_receipt(
    contract: PreparedPackageReceipt,
    prepared_state: PreparedPackageState,
    *,
    expected_state: str = "applying",
    target_state: str = "prepared",
) -> PreparedPackageReceipt:
    if (expected_state, target_state) not in {
        ("applying", "prepared"),
        ("prepared", "committed"),
    }:
        raise RuntimeError("Paket-Receipt-Ersatz besitzt keinen zulässigen Übergang")
    current = _validate_prepared_package_receipt(
        contract,
        expected_state=expected_state,
    )
    record = _prepared_package_receipt_record(
        state=target_state,
        transaction_id=current.transaction_id,
        install_root=current.install_root,
        full_backup_id=current.full_backup_id,
        package_transaction=current.package_transaction,
        prepared_state=prepared_state,
        target_commit=current.target_commit,
        target_tag=current.target_tag,
        role=current.role,
        apache_available=current.apache_available,
        apache_was_active=current.apache_was_active,
        apache_unit_file_state=current.apache_unit_file_state,
        static_recovery_contract_json=current.static_recovery_contract_json,
    )
    payload = _canonical_prepared_package_receipt_bytes(record)
    state_descriptor = _open_recovery_bootblock_state_directory()
    temporary_name = f".e3dc-package-receipt-{os.getpid()}-{secrets.token_hex(12)}"
    descriptor = None
    replaced = False
    replace_attempted = False
    prepared_transition_receipt = None
    try:
        rebound = _read_bound_root_file_at(
            state_descriptor,
            PREPARED_PACKAGE_RECEIPT_NAME,
            maximum=PREPARED_PACKAGE_RECEIPT_MAX_BYTES,
            mode=0o600,
        )
        if rebound is None or (rebound[1].st_dev, rebound[1].st_ino) != (
            current.receipt_dev,
            current.receipt_ino,
        ):
            raise RuntimeError("Paket-Receipt driftete vor dem vorbereiteten Ersatz")
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
                raise RuntimeError("Vorbereitetes Paket-Receipt blieb unvollständig")
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
            raise RuntimeError("Gestagtes Paket-Receipt ist unsicher")
        if (expected_state, target_state) == ("applying", "prepared"):
            prepared_transition_receipt = _parse_prepared_package_receipt(
                payload,
                staged,
            )
            if (
                prepared_transition_receipt.state != "prepared"
                or prepared_transition_receipt.prepared_state != prepared_state
                or not _same_package_receipt_recovery_anchor(
                    current,
                    prepared_transition_receipt,
                )
            ):
                raise RuntimeError(
                    "Gestagtes Paket-Receipt verlor seinen Recovery-Anker"
                )
        replace_attempted = True
        os.replace(
            temporary_name,
            PREPARED_PACKAGE_RECEIPT_NAME,
            src_dir_fd=state_descriptor,
            dst_dir_fd=state_descriptor,
        )
        replaced = True
        os.fsync(state_descriptor)
        rebound = _read_bound_root_file_at(
            state_descriptor,
            PREPARED_PACKAGE_RECEIPT_NAME,
            maximum=PREPARED_PACKAGE_RECEIPT_MAX_BYTES,
            mode=0o600,
        )
        if rebound is None or rebound[0] != payload or (
            rebound[1].st_dev,
            rebound[1].st_ino,
        ) != (staged.st_dev, staged.st_ino):
            raise RuntimeError("Vorbereitetes Paket-Receipt driftete nach dem Ersatz")
        return _parse_prepared_package_receipt(*rebound)
    except Exception as exc:
        if (
            (expected_state, target_state) == ("applying", "prepared")
            and replace_attempted
        ):
            raise _package_receipt_transition_error(
                original_error=exc,
                applying_receipt=current,
                prepared_receipt=prepared_transition_receipt,
                replace_attempted=replace_attempted,
                replace_completed=replaced,
            ) from exc
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not replaced:
            try:
                os.unlink(temporary_name, dir_fd=state_descriptor)
            except FileNotFoundError:
                pass
        os.close(state_descriptor)


def _commit_prepared_package_receipt(
    contract: PreparedPackageReceipt,
) -> PreparedPackageReceipt:
    if contract.prepared_state is None:
        raise RuntimeError("Paket-Receipt besitzt vor Commit keinen Postzustand")
    return _replace_prepared_package_receipt(
        contract,
        contract.prepared_state,
        expected_state="prepared",
        target_state="committed",
    )


def _package_transaction_from_receipt(
    contract: PreparedPackageReceipt | PackageRecoveryReceipt,
) -> PackageTransactionState:
    if not isinstance(contract, (PreparedPackageReceipt, PackageRecoveryReceipt)):
        raise RuntimeError("Paket-Recovery-Receipt besitzt den falschen Typ")
    current = _read_package_recovery_receipt()
    if (
        current is None
        or current.state != contract.state
        or current.transaction_id != contract.transaction_id
        or current.install_root != contract.install_root
        or current.full_backup_id != contract.full_backup_id
        or current.payload_sha256 != contract.payload_sha256
        or current.package_transaction != contract.package_transaction
        or current.receipt_path != contract.receipt_path
        or current.receipt_dev != contract.receipt_dev
        or current.receipt_ino != contract.receipt_ino
        or current.receipt_sha256 != contract.receipt_sha256
        or current.target_commit != contract.target_commit
        or current.target_tag != contract.target_tag
        or current.role != contract.role
        or current.apache_completion_required
        != contract.apache_completion_required
        or current.apache_available != contract.apache_available
        or current.apache_was_active != contract.apache_was_active
        or current.apache_unit_file_state != contract.apache_unit_file_state
        or current.static_recovery_contract_json
        != contract.static_recovery_contract_json
    ):
        raise RuntimeError("Paket-Recovery-Receipt oder sein Inode driftete")
    return current.package_transaction


def _remove_exact_prepared_package_receipt(
    contract: PreparedPackageReceipt | PackageRecoveryReceipt,
) -> None:
    _package_transaction_from_receipt(contract)
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        rebound = _read_bound_root_file_at(
            state_descriptor,
            PREPARED_PACKAGE_RECEIPT_NAME,
            maximum=PREPARED_PACKAGE_RECEIPT_MAX_BYTES,
            mode=0o600,
        )
        if rebound is None or (rebound[1].st_dev, rebound[1].st_ino) != (
            contract.receipt_dev,
            contract.receipt_ino,
        ):
            raise RuntimeError("Fremdes Paket-Receipt wird nicht entfernt")
        os.unlink(PREPARED_PACKAGE_RECEIPT_NAME, dir_fd=state_descriptor)
        os.fsync(state_descriptor)
    finally:
        os.close(state_descriptor)
    if os.path.lexists(contract.receipt_path):
        raise RuntimeError("Paket-Receipt blieb nach unlink vorhanden")


def _cleanup_confirmed_prepared_package_receipt(
    contract: PreparedPackageReceipt | PackageRecoveryReceipt | None,
    *,
    terminal_state: str,
) -> bool:
    """Entfernt nach bestätigtem Erfolg/Rücklauf nur den gebundenen Inode."""

    if contract is None:
        return True
    try:
        _remove_exact_prepared_package_receipt(contract)
        return True
    except Exception as exc:
        print(f"[HINWEIS] E3DC-UPD-PACKAGE-RECEIPT-CLEANUP: {exc}")
        print(
            f"Lösung: Der {terminal_state} ist bestätigt. "
            f"{contract.receipt_path} nicht löschen oder verändern; prüfe "
            "sudo journalctl -u 'e3dc-*update*' --no-pager, bevor Du ein "
            "weiteres Update startest."
        )
        update_logger.critical(
            "Gebundenes Paket-Receipt blieb nach %s erhalten: %s",
            terminal_state,
            exc,
        )
        return False


def _bind_package_apply_failure_recovery(
    *,
    error: Exception,
    receipt: PreparedPackageReceipt | PackageRecoveryReceipt | None,
    packages_mutated: bool,
    expected_transaction_id: str,
    expected_install_root: str,
    expected_full_backup_id: str,
    expected_package_transaction: PackageTransactionState,
) -> tuple[
    PreparedPackageReceipt | PackageRecoveryReceipt | None,
    PackageTransactionState | None,
]:
    """Bindet den Prestate auch bei einem Fehler nach erfolgreichem Replace."""

    if not packages_mutated:
        return receipt, None
    candidate = receipt
    if isinstance(error, PreparedPackageReceiptTransitionError):
        candidate = error.recovery_receipt
        package_transaction = error.package_transaction
    else:
        if candidate is None:
            raise RuntimeError("Paket-Recovery-Receipt fehlt nach Paketmutation")
        package_transaction = _package_transaction_from_receipt(candidate)
    if (
        not isinstance(candidate, (PreparedPackageReceipt, PackageRecoveryReceipt))
        or candidate.transaction_id != expected_transaction_id
        or candidate.install_root != expected_install_root
        or candidate.full_backup_id != expected_full_backup_id
        or candidate.package_transaction != expected_package_transaction
        or package_transaction != expected_package_transaction
    ):
        raise RuntimeError("Paket-Recovery-Prestate widerspricht der Update-Transaktion")
    return candidate, package_transaction


def _capture_prepared_package_state(
    state: PackageTransactionState,
    receipt: OfflinePackageReceipt,
) -> PreparedPackageState:
    """Beweist den rein additiven Paket-Postzustand unmittelbar nach Anwendung."""

    apt_after_all = _installed_apt_packages() if state.apt_requested else {}
    apt_before = dict(state.apt_before)
    missing_before = sorted(set(apt_before) - set(apt_after_all))
    changed_before = sorted(
        name
        for name, version in apt_before.items()
        if apt_after_all.get(name) != version
    )
    candidate_binary_names = set(
        _apt_binary_names_for_candidates(
            apt_after_all,
            state.apt_candidate_packages,
        )
    )
    introduced = set(apt_after_all) - set(apt_before)
    unexpected_introduced = sorted(introduced - candidate_binary_names)
    missing_requested = sorted(
        package
        for package in state.apt_requested
        if not _apt_binary_names_for_candidates(apt_after_all, (package,))
    )
    if missing_before or changed_before or unexpected_introduced or missing_requested:
        raise RuntimeError(
            "APT-Postzustand verletzt den rein additiven --no-upgrade-Vertrag"
        )

    venv_python = state.venv_python
    pip_after = {}
    if state.pip_requested:
        venv_python = _find_venv_python(state.install_user)
        if not venv_python:
            raise RuntimeError("Vorbereitetes venv ist nach Offline-Installation nicht gebunden")
        pip_after = _installed_pip_packages(venv_python, state.install_user)
        requested_normalized = {
            _normalize_python_package_name(name)
            for name in state.pip_requested
        }
        if not requested_normalized.issubset(pip_after):
            raise RuntimeError("Vorbereitete Python-Pakete fehlen im gebundenen venv")
        if state.venv_existed:
            pip_before = dict(state.pip_before)
            changed = sorted(
                name
                for name, version in pip_before.items()
                if pip_after.get(name) != version
            )
            introduced_pip = set(pip_after) - set(pip_before)
            if changed or not introduced_pip.issubset(requested_normalized):
                raise RuntimeError(
                    "venv-Postzustand verletzt den rein additiven --no-deps-Vertrag"
                )

    if not _ensure_rsync_available(allow_install=False):
        raise RuntimeError("rsync fehlt trotz vorbereiteter Offline-Pakettransaktion")
    receipt_json = serialize_offline_package_receipt(receipt).decode("utf-8")
    return PreparedPackageState(
        transaction_id=receipt.cache.transaction_id,
        offline_receipt_json=receipt_json,
        offline_receipt_sha256=hashlib.sha256(receipt_json.encode("utf-8")).hexdigest(),
        apt_after=tuple(
            sorted(
                (name, apt_after_all[name])
                for name in candidate_binary_names
            )
        ),
        pip_after=tuple(sorted(pip_after.items())),
        venv_python=venv_python,
        install_user=state.install_user,
        apt_requested=state.apt_requested,
        pip_requested=state.pip_requested,
    )


def _apply_prepared_offline_package_policy(
    state: PackageTransactionState,
    receipt: OfflinePackageReceipt,
) -> tuple[PackageTransactionState, PreparedPackageState]:
    """Wendet nach dem Vollbackup nur versiegelte lokale Paketartefakte an."""

    bound_state = _bind_package_transaction_to_offline_receipt(state, receipt)
    apt_commands = build_offline_install_commands(
        receipt,
        include_pip=False,
    )
    if apt_commands.apt_install_argv is not None:
        result = _run_argv(list(apt_commands.apt_install_argv), timeout=600)
        if not result.get("success"):
            raise RuntimeError(
                "Offline-APT-Installation fehlgeschlagen: "
                + _combined_process_diagnostics(result, maximum=1600)
            )

    venv_python = bound_state.venv_python
    if bound_state.pip_requested:
        if not bound_state.venv_existed:
            venv_python = _create_release_venv(
                bound_state.install_user,
                str(bound_state.venv_path or ""),
            )
        if not venv_python:
            raise RuntimeError("Gebundener venv-Interpreter fehlt vor Offline-pip")
        pip_commands = build_offline_install_commands(
            receipt,
            venv_python=venv_python,
        )
        if pip_commands.pip_install_argv is None:
            raise RuntimeError("Offline-pip-Kommando fehlt trotz Paketpolicy")
        result = _run_argv(
            [
                "sudo",
                "-H",
                "-u",
                bound_state.install_user,
                *pip_commands.pip_install_argv,
            ],
            timeout=600,
        )
        if not result.get("success"):
            raise RuntimeError(
                "Offline-venv-pip-Installation fehlgeschlagen: "
                + _combined_process_diagnostics(result, maximum=1600)
            )
        require_bound_venv_runtime(
            install_user=bound_state.install_user,
            venv_path=str(bound_state.venv_path or ""),
        )

    prepared = _capture_prepared_package_state(bound_state, receipt)
    _serialize_prepared_package_state(prepared)
    return bound_state, prepared


def _verify_prepared_package_policy_applied(
    policy: dict,
    install_user: str,
    *,
    expected_transaction_id: str,
    prepared: PreparedPackageState,
) -> PreparedPackageState:
    """Finalizer-Readback ohne Paketmutation und ohne Netzwerkzugriff."""

    if not isinstance(prepared, PreparedPackageState):
        raise RuntimeError("Vorbereiteter Paket-Postzustand fehlt")
    prepared = _parse_prepared_package_state(
        _serialize_prepared_package_state(prepared)
    )
    if (
        prepared.transaction_id != expected_transaction_id
        or prepared.install_user != install_user
        or prepared.apt_requested != tuple(_validated_release_apt_packages(policy))
        or prepared.pip_requested != tuple(_validated_venv_pip_packages(policy))
    ):
        raise RuntimeError("Vorbereiteter Paketvertrag widerspricht Zielpolicy oder Transaktion")
    apt_now = _installed_apt_packages() if prepared.apt_requested else {}
    missing_requested = [
        package
        for package in prepared.apt_requested
        if not _apt_binary_names_for_candidates(apt_now, (package,))
    ]
    if missing_requested or any(
        apt_now.get(name) != version for name, version in prepared.apt_after
    ):
        raise RuntimeError("APT-Postzustand driftete vor dem Target-Finalizer")
    if prepared.pip_requested:
        actual_venv = _find_venv_python(install_user)
        if actual_venv != prepared.venv_python:
            raise RuntimeError("venv-Interpreter driftete vor dem Target-Finalizer")
        if _installed_pip_packages(actual_venv, install_user) != dict(prepared.pip_after):
            raise RuntimeError("venv-Paketstand driftete vor dem Target-Finalizer")
    if not _ensure_rsync_available(allow_install=False):
        raise RuntimeError("rsync fehlt vor dem Target-Finalizer")
    return prepared


def _cleanup_terminal_offline_package_receipt(
    receipt: OfflinePackageReceipt | None,
    *,
    terminal_state: str,
) -> bool:
    """Räumt nur nach bewiesenem Erfolg/Rücklauf auf; Fehler bleiben nicht fatal."""

    if receipt is None:
        return True
    try:
        cleanup_offline_package_artifacts(receipt)
        return True
    except Exception as exc:
        print(f"[HINWEIS] E3DC-UPD-OFFLINE-CLEANUP: {exc}")
        print(
            f"Lösung: Der {terminal_state} ist bereits verifiziert. Den Cache "
            f"{receipt.cache.root} nicht rekursiv oder mit Wildcards löschen; "
            "prüfe vor dem nächsten Update das vollständige Journal."
        )
        update_logger.warning(
            "Offline-Paketartefakte blieben nach %s erhalten: %s",
            terminal_state,
            exc,
        )
        return False


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
    apt_before = _installed_apt_packages() if apt_requested else {}
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
        apt_before=tuple(sorted(apt_before.items())),
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
    """Entfernt ausschließlich von dieser Offline-Transaktion eingeführte Pakete."""

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
            allowed_introduced = {
                _normalize_python_package_name(name)
                for name in state.pip_requested
            }
            unexpected = sorted(set(introduced) - allowed_introduced)
            if changed or unexpected:
                raise RuntimeError(
                    "venv-Paketstand driftete außerhalb des rein additiven "
                    "Offline-Vertrags; automatischer Rücklauf bleibt fail-closed"
                )
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
            if _installed_pip_packages(state.venv_python, state.install_user) != before:
                raise RuntimeError("venv-Paketstand stimmt nach Rücklauf nicht exakt")
    if state.apt_requested:
        apt_after = _installed_apt_packages()
        before = dict(state.apt_before)
        changed = sorted(
            name
            for name, version in before.items()
            if name in apt_after and apt_after[name] != version
        )
        missing = sorted(set(before) - set(apt_after))
        candidates = set(
            _apt_binary_names_for_candidates(
                apt_after,
                state.apt_candidate_packages,
            )
        )
        introduced = sorted(
            name for name in candidates if name not in before and name in apt_after
        )
        if changed or missing:
            raise RuntimeError(
                "apt-Paketstand driftete außerhalb des --no-upgrade-Vertrags; "
                "automatischer Rücklauf bleibt fail-closed"
            )
        if introduced:
            result = _run_argv(
                [
                    "/usr/bin/apt-get",
                    "--no-download",
                    "remove",
                    "-y",
                    "--",
                    *introduced,
                ],
                timeout=300,
            )
            if not result["success"]:
                raise RuntimeError("Neu installierte apt-Pakete konnten nicht zurückgerollt werden")
        restored = _installed_apt_packages()
        if any(restored.get(name) != version for name, version in before.items()):
            raise RuntimeError("apt-Paketstand stimmt nach Rücklauf nicht exakt")
        if any(name in restored for name in introduced):
            raise RuntimeError("Transaktionseigene apt-Pakete blieben nach Rücklauf installiert")


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
    bootstrap_root = str(os.environ.get("E3DC_BOOTSTRAP_ROOT") or "").strip()
    runner_root = str(
        os.environ.get("E3DC_BOOTSTRAP_RUNNER_ROOT") or ""
    ).strip()
    independent_runner = bool(
        bootstrap_root
        and runner_root
        and os.path.realpath(bootstrap_root) == os.path.realpath(candidate)
        and os.path.realpath(runner_root) != os.path.realpath(candidate)
        and os.path.isfile(os.path.join(runner_root, "installer_main.py"))
        and not os.path.islink(os.path.join(runner_root, "installer_main.py"))
    )
    if not independent_runner:
        markers = (
            os.path.join(candidate, 'Installer'),
            os.path.join(candidate, 'installer_main.py'),
            os.path.join(candidate, 'e3dc.config.txt'),
            os.path.join(candidate, 'E3DC-Control'),
        )
        if not any(
            os.path.lexists(marker)
            and not stat.S_ISLNK(os.lstat(marker).st_mode)
            for marker in markers
        ):
            raise ValueError(
                'Bootstrap-Ziel ist keine erkennbare E3DC-Control Installation.'
            )
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


def _read_stopped_unit_contract(service: str) -> dict[str, object]:
    """Liest den vollständigen Stopzustand einer Unit in genau einem Readback."""

    unit = _unit_name(service)
    diagnostic_hint = (
        f"Prüfen: sudo systemctl status --no-pager {unit}; "
        f"sudo journalctl -u {unit} --no-pager -n 100"
    )
    properties = ("LoadState", "ActiveState", "SubState", "MainPID")
    result = _run_argv(
        [
            "systemctl",
            "show",
            "--no-pager",
            *(f"--property={name}" for name in properties),
            unit,
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
        raise RuntimeError(
            f"Stopzustand von {unit} ist nicht lesbar: "
            + _command_result_diagnostic(result)
            + f". {diagnostic_hint}"
        )
    values: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        if not raw_line:
            continue
        key, separator, value = raw_line.partition("=")
        if (
            separator != "="
            or key not in properties
            or key in values
            or value != value.strip()
        ):
            raise RuntimeError(
                f"Stopzustand von {unit} ist widersprüchlich. {diagnostic_hint}"
            )
        values[key] = value
    if set(values) != set(properties):
        raise RuntimeError(
            f"Stopzustand von {unit} ist unvollständig. {diagnostic_hint}"
        )
    try:
        main_pid = int(values["MainPID"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"MainPID von {unit} ist ungültig. {diagnostic_hint}"
        ) from exc
    if main_pid < 0:
        raise RuntimeError(
            f"MainPID von {unit} ist ungültig. {diagnostic_hint}"
        )
    return {
        "unit": unit,
        "load_state": values["LoadState"].lower(),
        "active_state": values["ActiveState"].lower(),
        "sub_state": values["SubState"].lower(),
        "main_pid": main_pid,
    }


def _normalize_stopped_unit_contract(
    service: str,
    *,
    allow_failed_reset: bool = True,
) -> dict[str, object]:
    """Setzt ausschließlich stale ``failed/PID0`` auf ``inactive/dead`` zurück."""

    state = _read_stopped_unit_contract(service)
    diagnostic_hint = (
        f"Prüfen: sudo systemctl status --no-pager {state['unit']}; "
        f"sudo journalctl -u {state['unit']} --no-pager -n 100"
    )

    def state_text(current: dict[str, object]) -> str:
        return (
            f"{current['load_state']}/{current['active_state']}/"
            f"{current['sub_state']}/MainPID={current['main_pid']}"
        )

    absent = (
        state["load_state"] == "not-found"
        and state["active_state"] == "inactive"
        and state["sub_state"] == "dead"
        and state["main_pid"] == 0
    )
    if absent:
        return state
    if state["active_state"] == "failed":
        if state["main_pid"] != 0:
            raise RuntimeError(
                f"{state['unit']} besitzt nach Stop einen unsicheren Zustand "
                f"({state_text(state)}); failed mit MainPID>0 wird nicht "
                f"zurückgesetzt. {diagnostic_hint}"
            )
        if not allow_failed_reset:
            raise RuntimeError(
                f"{state['unit']} driftete nach dem bestätigten Stop erneut "
                f"in failed ({state_text(state)}); ein zweites reset-failed "
                f"ist nicht zulässig. {diagnostic_hint}"
            )
        reset = _run_argv(
            ["sudo", "systemctl", "reset-failed", str(state["unit"])],
            timeout=10,
        )
        if (
            not reset.get("success")
            or reset.get("timed_out")
            or int(reset.get("returncode", -1)) != 0
            or str(reset.get("stderr") or "")
        ):
            raise RuntimeError(
                f"Failed-Zustand von {state['unit']} ({state_text(state)}) "
                "konnte nicht zurückgesetzt werden: "
                + _command_result_diagnostic(reset)
                + f". {diagnostic_hint}"
            )
        state = _read_stopped_unit_contract(service)
    if not (
        state["load_state"] in {"loaded", "masked", "not-found"}
        and state["active_state"] == "inactive"
        and state["sub_state"] == "dead"
        and state["main_pid"] == 0
    ):
        raise RuntimeError(
            f"{state['unit']} besitzt keinen sicheren Stopzustand "
            f"({state_text(state)}). {diagnostic_hint}"
        )
    return state


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
    # langsameren Show-Readbacks beginnen. Unmittelbar nach diesem Stop-Burst
    # wird jede Unit kanonisch gebunden; failed/PID0 wird dabei höchstens einmal
    # zurückgesetzt und muss danach frisch inactive/dead/MainPID=0 liefern.
    stop_results = {}
    for srv in stop_order:
        stop_results[srv] = _run_argv(
            ["sudo", "systemctl", "stop", _unit_name(srv)],
            timeout=15,
        )
    for srv in stop_order:
        stopped = stop_results[srv]
        try:
            stopped_state = _normalize_stopped_unit_contract(srv)
        except Exception as exc:
            errors.append(
                f"{_unit_name(srv)} ist nach Sofortstop nicht sicher gebunden: "
                f"{exc}; Stop={_command_result_diagnostic(stopped)}"
            )
            continue
        if stopped_state["load_state"] == "not-found":
            print(f"  [OK] {srv} (nicht installiert)")
            continue
        print(f"  [OK] {srv}")
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
    # globale Aktorruhe. Ein erneutes failed ist Drift und darf nicht durch
    # ein zweites reset-failed verdeckt werden.
    for srv in stop_order:
        try:
            stopped_state = _normalize_stopped_unit_contract(
                srv,
                allow_failed_reset=False,
            )
        except Exception as exc:
            errors.append(
                f"{_unit_name(srv)} ist im globalen Stop-Endgate nicht sicher "
                f"gebunden: {exc}"
            )
            continue
        if stopped_state["load_state"] == "not-found":
            continue
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


def _assert_managed_finalizer_service(
    contract: UpdateSafetyContract | RecoveryBootblockContract,
) -> dict[str, str]:
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
    contract: UpdateSafetyContract | RecoveryBootblockContract,
    *,
    repo_dir: str,
) -> None:
    """Öffnet den Startpfad erst im gebundenen laufenden Finalizer-Service."""

    if isinstance(contract, UpdateSafetyContract):
        _validate_update_safety_contract(contract, expected_state="pending")
        _verify_update_safety_marker(contract, expected_present=True)
        _reload_and_verify_update_safety_dropins(contract, expected_present=True)
    elif isinstance(contract, RecoveryBootblockContract):
        _validate_recovery_bootblock_contract(contract)
        _verify_recovery_bootblock_marker(contract, expected_present=True)
        _reload_and_verify_recovery_dropins(
            contract.units,
            expected_present=True,
            transaction_id=contract.transaction_id,
        )
    else:
        raise RuntimeError("Startlease besitzt keinen unterstützten Bootblockvertrag")
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
    contract: UpdateSafetyContract | RecoveryBootblockContract,
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
    contract: UpdateSafetyContract | RecoveryBootblockContract,
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
    contract: UpdateSafetyContract | RecoveryBootblockContract,
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
    check_web: bool = True,
    check_http: bool = True,
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
    if not isinstance(check_web, bool):
        errors.append("Web-Gesundheitsvertrag ist nicht boolesch")
    elif check_web:
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

    if not isinstance(check_http, bool):
        errors.append("HTTP-Gesundheitsvertrag ist nicht boolesch")
    elif check_http:
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


def _capture_apache_service_prestate(
    *,
    settle_timeout_s: float = 8.0,
    poll_s: float = 0.25,
) -> tuple[bool, bool, str]:
    """Bindet Load-, Aktivitäts- und Enablementzustand in einem Show-Snapshot."""

    deadline = time.monotonic() + max(0.0, float(settle_timeout_s))
    expected_keys = {"LoadState", "ActiveState", "UnitFileState"}
    accepted_unit_states = {
        "enabled",
        "enabled-runtime",
        "disabled",
        "static",
        "indirect",
        "masked",
        "masked-runtime",
        "generated",
        "transient",
        "alias",
        "linked",
        "linked-runtime",
    }
    while True:
        result = _run_argv(
            [
                "systemctl",
                "show",
                "apache2.service",
                "--no-pager",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=UnitFileState",
            ],
            timeout=15,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        values: dict[str, str] = {}
        for line in str(result.get("stdout") or "").splitlines():
            key, separator, value = line.partition("=")
            if separator != "=" or key not in expected_keys or key in values:
                values = {}
                break
            values[key] = value.strip().lower()
        if (
            not result.get("success")
            or result.get("timed_out")
            or int(result.get("returncode", -1)) != 0
            or str(result.get("stderr") or "")
            or set(values) != expected_keys
        ):
            raise RuntimeError(
                "Apache-LoadState/Aktivitäts-/Enablement-Snapshot ist nicht strikt lesbar"
            )
        load_state = values["LoadState"]
        active_state = values["ActiveState"]
        unit_file_state = values["UnitFileState"]
        if load_state == "not-found":
            if active_state not in {"", "inactive"}:
                raise RuntimeError("Abwesender Apache besitzt einen aktiven Zustand")
            return False, False, "absent"
        if load_state != "loaded" or unit_file_state not in accepted_unit_states:
            raise RuntimeError(
                "Apache-Load-/Enablementzustand ist für das Recovery-Preimage unzulässig"
            )
        if active_state == "active":
            return True, True, unit_file_state
        if active_state in {"inactive", "failed"}:
            return True, False, unit_file_state
        if active_state not in {
            "activating",
            "deactivating",
            "reloading",
            "maintenance",
        }:
            raise RuntimeError(f"Apache-Aktivitätszustand ist unklar: {active_state}")
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Apache blieb in einem transienten Zustand: {active_state}"
            )
        time.sleep(max(0.01, min(float(poll_s), 1.0)))


def _quiesce_apache_for_cutover(preimage: ApacheSecurityPreimage) -> None:
    """Stoppt ausschließlich einen zuvor aktiven Apache und beweist Web-Schreibruhe."""

    if not isinstance(preimage, ApacheSecurityPreimage):
        raise RuntimeError("Apache-Cutover besitzt kein gebundenes Preimage")
    if preimage.apache_available and preimage.apache_was_active:
        stopped = _run_argv(
            ["sudo", "systemctl", "stop", "apache2.service"],
            timeout=30,
        )
        if not stopped.get("success"):
            raise RuntimeError(
                "Apache konnte für das kurze Daten-/Web-Cutover nicht gestoppt werden: "
                + _combined_process_diagnostics(stopped, maximum=800)
            )
    available, active, unit_state = _capture_apache_service_prestate()
    if (
        available != preimage.apache_available
        or active
        or unit_state != preimage.apache_unit_file_state
    ):
        raise RuntimeError(
            "Apache besitzt keine gebundene inaktive Cutover-Lage "
            f"(loaded={available}, active={active}, unit={unit_state})"
        )


def _restore_apache_after_successful_cutover(
    *,
    expected_available: bool,
    expected_active: bool,
    expected_unit_file_state: str,
) -> None:
    """Stellt vor dem HTTP-Gate exakt den gebundenen Apache-Aktivzustand her."""

    if not isinstance(expected_available, bool) or not isinstance(expected_active, bool):
        raise RuntimeError("Apache-Zielzustand ist nicht boolesch gebunden")
    unit_state = str(expected_unit_file_state or "").strip().lower()
    if expected_available and expected_active:
        started = _run_argv(
            ["sudo", "systemctl", "start", "apache2.service"],
            timeout=30,
        )
        if not started.get("success"):
            raise RuntimeError(
                "Apache konnte nach dem Cutover nicht gestartet werden: "
                + _combined_process_diagnostics(started, maximum=800)
            )
    available, active, current_unit_state = _capture_apache_service_prestate()
    if (available, active, current_unit_state) != (
        expected_available,
        expected_active,
        unit_state,
    ):
        raise RuntimeError(
            "Apache-Endzustand weicht vom gebundenen Vorzustand ab "
            f"(loaded={available}, active={active}, unit={current_unit_state})"
        )


def _verify_apache_quiesced_before_commit(
    *,
    expected_available: bool,
    expected_active: bool,
    expected_unit_file_state: str,
) -> None:
    """Prüft Apache vollständig, ohne den externen Webzugang zu öffnen."""

    if not isinstance(expected_available, bool) or not isinstance(expected_active, bool):
        raise RuntimeError("Apache-PreCommit-Vertrag ist nicht boolesch gebunden")
    unit_state = str(expected_unit_file_state or "").strip().lower()
    if expected_active and not expected_available:
        raise RuntimeError("Abwesender Apache kann nicht als zuvor aktiv gebunden sein")
    available, active, current_unit_state = _capture_apache_service_prestate()
    if (available, active, current_unit_state) != (
        expected_available,
        False,
        unit_state,
    ):
        raise RuntimeError(
            "Apache blieb vor der durable Commit-Grenze nicht exakt inaktiv "
            f"(loaded={available}, active={active}, unit={current_unit_state})"
        )
    if expected_available:
        configtest = _run_argv(
            ["sudo", "/usr/sbin/apache2ctl", "configtest"],
            timeout=30,
        )
        if not configtest.get("success"):
            raise RuntimeError(
                "Apache-Konfiguration ist vor der Commit-Grenze ungültig: "
                + _combined_process_diagnostics(configtest, maximum=800)
            )


def _complete_bound_apache_after_commit(
    *,
    expected_available: bool,
    expected_active: bool,
    expected_unit_file_state: str,
) -> None:
    """Stellt Apache nach Commit idempotent her und prüft nur lokalen HTTP-Zugriff."""

    if not isinstance(expected_available, bool) or not isinstance(expected_active, bool):
        raise RuntimeError("Apache-PostCommit-Vertrag ist nicht boolesch gebunden")
    unit_state = str(expected_unit_file_state or "").strip().lower()
    if expected_active and not expected_available:
        raise RuntimeError("Abwesender Apache kann nach Commit nicht gestartet werden")

    available, active, current_unit_state = _capture_apache_service_prestate()
    if available != expected_available or current_unit_state != unit_state:
        raise RuntimeError(
            "Apache driftete vor dem wiederholbaren PostCommit-Abschluss "
            f"(loaded={available}, active={active}, unit={current_unit_state})"
        )
    if not expected_active:
        if active:
            raise RuntimeError("Absichtlich inaktiver Apache wurde nach Commit aktiv")
        return

    configtest = _run_argv(
        ["sudo", "/usr/sbin/apache2ctl", "configtest"],
        timeout=30,
    )
    if not configtest.get("success"):
        raise RuntimeError(
            "Apache-Konfiguration ist beim PostCommit-Abschluss ungültig: "
            + _combined_process_diagnostics(configtest, maximum=800)
        )
    _restore_apache_after_successful_cutover(
        expected_available=expected_available,
        expected_active=expected_active,
        expected_unit_file_state=unit_state,
    )
    http_errors = _local_http_healthcheck()
    if http_errors:
        raise RuntimeError("; ".join(http_errors[:4]))


def _complete_committed_apache_from_receipt(
    contract: UpdateSafetyContract,
) -> None:
    """Bindet einen wiederholten Apache-Abschluss an das durable Commit-Receipt."""

    current = _validate_update_safety_contract(contract, expected_state="committed")
    if not current.apache_completion_required:
        return
    _complete_bound_apache_after_commit(
        expected_available=current.apache_available,
        expected_active=current.apache_was_active,
        expected_unit_file_state=current.apache_unit_file_state,
    )


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

    (
        apache_available,
        apache_was_active,
        apache_unit_file_state,
    ) = _capture_apache_service_prestate()
    if apache_available:
        apache_ctl = os.lstat("/usr/sbin/apache2ctl")
        if (
            not stat.S_ISREG(apache_ctl.st_mode)
            or apache_ctl.st_uid != 0
            or apache_ctl.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("apache2ctl besitzt kein vertrauenswürdiges Preimage")

    return ApacheSecurityPreimage(
        available=available,
        payload=payload,
        uid=uid,
        gid=gid,
        mode=mode,
        enabled=enabled,
        enabled_target=enabled_target,
        apache_available=apache_available,
        apache_was_active=apache_was_active,
        apache_unit_file_state=apache_unit_file_state,
    )


def _restore_apache_unit_file_state(preimage: ApacheSecurityPreimage) -> None:
    """Stellt nur die gebundene Apache-Enablementklasse wieder her."""

    target = preimage.apache_unit_file_state
    if target == "absent":
        return
    commands: list[list[str]] = []
    if target == "enabled":
        commands = [
            ["sudo", "systemctl", "unmask", "apache2.service"],
            ["sudo", "systemctl", "enable", "apache2.service"],
        ]
    elif target == "enabled-runtime":
        commands = [
            ["sudo", "systemctl", "unmask", "apache2.service"],
            ["sudo", "systemctl", "enable", "--runtime", "apache2.service"],
        ]
    elif target == "disabled":
        commands = [
            ["sudo", "systemctl", "unmask", "apache2.service"],
            ["sudo", "systemctl", "disable", "apache2.service"],
        ]
    elif target in {"masked", "masked-runtime"}:
        command = ["sudo", "systemctl", "mask"]
        if target == "masked-runtime":
            command.append("--runtime")
        commands = [[*command, "apache2.service"]]
    else:
        # static/indirect/generated/alias/linked werden nicht künstlich in
        # einen anderen Enablementtyp übersetzt. Sie müssen unverändert sein.
        available, _active, current = _capture_apache_service_prestate()
        if not available or current != target:
            raise RuntimeError(
                "Apache-Enablementklasse kann nicht verlustfrei wiederhergestellt werden"
            )
        return
    for command in commands:
        result = _run_argv(command, timeout=30)
        if not result.get("success"):
            raise RuntimeError(
                "Apache-Enablementzustand konnte nach Recovery nicht wiederhergestellt werden"
            )


def _restore_apache_security_preimage(
    preimage: ApacheSecurityPreimage,
    *,
    restore_activity: bool = True,
) -> None:
    """Stellt Dateien/Enablement her; der Prozessstart kann bis zum Endgate warten."""

    if not isinstance(restore_activity, bool):
        raise RuntimeError("Apache-Recovery-Aktivitätsvertrag ist nicht boolesch")
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
        _restore_apache_unit_file_state(preimage)
        action = "start" if restore_activity and preimage.apache_was_active else "stop"
        service_result = _run_argv(
            ["sudo", "systemctl", action, "apache2.service"],
            timeout=30,
        )
        if not service_result.get("success"):
            raise RuntimeError(
                "Apache-Aktivitätszustand konnte nach Recovery nicht "
                "wiederhergestellt werden"
            )
        if restore_activity and preimage.apache_was_active:
            reload_result = _run_argv(
                ["sudo", "systemctl", "reload", "apache2.service"],
                timeout=30,
            )
            if not reload_result.get("success"):
                raise RuntimeError(
                    "Apache konnte nach aktiver Recovery nicht neu geladen werden"
                )

    expected = (
        preimage
        if restore_activity
        else replace(preimage, apache_was_active=False)
    )
    if _capture_apache_security_preimage() != expected:
        raise RuntimeError("Apache-Recovery weicht vom gebundenen Preimage ab")


def _capture_recovery_surface(
    state: TransitionState,
    install_user: str | None = None,
) -> RecoverySurfaceInventory:
    bound_install_user = str(install_user or get_install_user())
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
        root_file_preimages=capture_root_file_preimages(),
        crontab_preimages=capture_crontab_preimages(bound_install_user),
    )


def _surface_receipt_from_inventory(
    inventory: RecoverySurfaceInventory,
    *,
    transaction_id: str,
    install_root: str,
    full_backup_id: str,
) -> recovery_surface_codec.RecoverySurfaceReceipt:
    """Überführt nur bereits eingefrorene Nebenflächen in den neutralen Codec."""

    if inventory.root_file_preimages is None or inventory.crontab_preimages is None:
        raise RuntimeError("Recovery-Nebenfläche besitzt kein vollständiges Preimage")
    root_managed = tuple(
        recovery_surface_codec.RootManagedFileRecoveryPreimage(
            path=item.path,
            existed=item.existed,
            payload=item.payload if item.existed else None,
            sha256=(hashlib.sha256(item.payload).hexdigest() if item.existed else None),
            uid=item.uid if item.existed else None,
            gid=item.gid if item.existed else None,
            mode=item.mode if item.existed else None,
            parent_dev=item.parent_dev,
            parent_ino=item.parent_ino,
        )
        for item in inventory.root_managed_files
    )
    apache = inventory.apache_security
    apache_neutral = recovery_surface_codec.ApacheSecurityRecoveryPreimage(
        available=apache.available,
        payload=apache.payload if apache.available else None,
        sha256=(hashlib.sha256(apache.payload).hexdigest() if apache.available else None),
        uid=apache.uid if apache.available else None,
        gid=apache.gid if apache.available else None,
        mode=apache.mode if apache.available else None,
        enabled=apache.enabled,
        enabled_target=apache.enabled_target if apache.enabled else None,
        apache_available=apache.apache_available,
        apache_was_active=apache.apache_was_active,
        apache_unit_file_state=apache.apache_unit_file_state,
    )
    return recovery_surface_codec.create_recovery_surface_receipt(
        transaction_id=transaction_id,
        install_root=install_root,
        full_backup_id=full_backup_id,
        root_files=inventory.root_file_preimages,
        crontabs=inventory.crontab_preimages,
        root_managed_files=root_managed,
        apache_security=apache_neutral,
    )


def _context_directory_chain(
    chain: tuple[tuple[str, int, int, int, int, int], ...],
) -> tuple[recovery_context_codec.DirectoryIdentity, ...]:
    return tuple(
        recovery_context_codec.DirectoryIdentity(
            path=str(path),
            device=int(device),
            inode=int(inode),
            uid=int(uid),
            gid=int(gid),
            mode=int(mode),
        )
        for path, device, inode, uid, gid, mode in chain
    )


def _context_privileged_payloads(
    payloads: tuple[PrivilegedBackupFileReceipt, ...],
) -> tuple[recovery_context_codec.PrivilegedBackupPayloadBinding, ...]:
    return tuple(
        recovery_context_codec.PrivilegedBackupPayloadBinding(
            restore_path=item.restore_path,
            category=item.category,
            backup_relative_path=item.backup_relative_path,
            parent_path_chain=_context_directory_chain(item.parent_path_chain),
            device=item.dev,
            inode=item.ino,
            sha256=item.sha256,
            size=item.size,
            mode=item.mode,
            uid=item.uid,
            gid=item.gid,
            nlink=item.nlink,
            mtime_ns=item.mtime_ns,
            ctime_ns=item.ctime_ns,
        )
        for item in payloads
    )


def _context_receipt_reference(
    binding,
) -> recovery_context_codec.RecoveryReceiptReference:
    return recovery_context_codec.RecoveryReceiptReference(
        path=str(binding.path),
        device=int(binding.dev),
        inode=int(binding.ino),
        sha256=str(binding.sha256),
    )


def _capture_context_backup_binding(
    *,
    backup_dir: str,
    manifest: dict,
    recovery_receipt: RecoveryBackupReceipt | None,
) -> recovery_context_codec.RecoveryBackupBinding:
    stable_manifest, manifest_sha256 = _read_stable_verified_backup_manifest(
        backup_dir
    )
    if stable_manifest != manifest:
        raise RuntimeError("Vollbackup driftete vor der Recovery-Kontextbindung")
    if recovery_receipt is not None:
        chain = recovery_receipt.backup_path_chain
        backup_device = recovery_receipt.backup_dev
        backup_inode = recovery_receipt.backup_ino
        parent_device = recovery_receipt.parent_dev
        parent_inode = recovery_receipt.parent_ino
    else:
        descriptor, chain = _open_root_receipt_directory_chain(backup_dir)
        try:
            metadata = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if len(chain) < 2:
            raise RuntimeError("Vollbackup besitzt keine gebundene Elternkette")
        backup_device = int(metadata.st_dev)
        backup_inode = int(metadata.st_ino)
        parent_device = int(chain[-2][1])
        parent_inode = int(chain[-2][2])
    return recovery_context_codec.RecoveryBackupBinding(
        backup_dir=backup_dir,
        backup_device=backup_device,
        backup_inode=backup_inode,
        parent_device=parent_device,
        parent_inode=parent_inode,
        path_chain=_context_directory_chain(chain),
        backup_id=str(manifest.get("backup_id") or ""),
        manifest_sha256=manifest_sha256,
    )


def _verify_prejournal_against_journal(
    construction: prejournal_codec.PersistedPrejournalConstruction,
    journal: recovery_journal.RecoveryJournalContract,
    context: recovery_context_codec.RecoveryContextContract,
) -> tuple[
    prejournal_codec.PersistedPrejournalConstruction,
    recovery_journal.RecoveryJournalContract,
]:
    """Kreuzbindet den kurzlebigen Bauzeugen mit Journal und Context."""

    bound_construction = prejournal_codec.verify_prejournal_construction(
        construction
    )
    bound_journal = recovery_journal.verify_recovery_journal(journal)
    bound_context = recovery_context_codec.read_recovery_context(
        expected_transaction_id=context.context.transaction_id,
        expected_install_root=context.context.install_root,
        expected_full_backup_id=context.context.backup.backup_id,
        path=context.context_path,
    )
    if bound_context != context:
        raise RuntimeError("Recovery-Kontext driftete an der Journalgrenze")
    construction_receipt = bound_construction.receipt
    journal_payload = bound_journal.payload
    context_payload = bound_context.context
    if (
        journal_payload.phase != recovery_journal.PHASE_PREPRODUCT
        or journal_payload.package is not None
        or journal_payload.safety is not None
        or journal_payload.overlay is not None
        or construction_receipt.transaction_id
        != journal_payload.transaction_id
        or construction_receipt.install_root != journal_payload.install_root
        or construction_receipt.install_user != journal_payload.install_user
        or construction_receipt.source != journal_payload.source
        or construction_receipt.target != journal_payload.target
        or construction_receipt.full_backup != journal_payload.full_backup
        or context_payload.transaction_id
        != construction_receipt.transaction_id
        or context_payload.install_root != construction_receipt.install_root
        or context_payload.install_user != construction_receipt.install_user
        or context_payload.target.commit != construction_receipt.target.commit
        or context_payload.target.tag != construction_receipt.target.tag
        or context_payload.target.role != construction_receipt.target.role
        or context_payload.backup.backup_dir
        != construction_receipt.backup_dir
        or context_payload.backup.backup_id
        != construction_receipt.full_backup.backup_id
        or context_payload.backup.manifest_sha256
        != construction_receipt.full_backup.manifest_sha256
        or context_payload.source.old_commit
        != construction_receipt.source.commit
        or context_payload.source.bootstrap_without_git
        == construction_receipt.source.repository_present
        or context_payload.source.bootstrap_rebuild_git
        != construction_receipt.source.repository_rebuild_required
    ):
        raise RuntimeError(
            "Construction-Receipt, Recovery-Kontext und Master-Journal "
            "widersprechen sich"
        )
    return bound_construction, bound_journal


def _persist_preproduct_recovery_bundle(
    *,
    transaction_id: str,
    repo_dir: str,
    install_user: str,
    old_commit: str | None,
    bootstrap_rebuild_git: bool,
    target_commit: str,
    target_tag: str,
    state: TransitionState,
    inventory: frozenset[str],
    recovery_inventory: RecoverySurfaceInventory,
    backup_dir: str,
    full_backup_manifest: dict,
    repo_recovery_contract: RepoRecoveryContract | None,
    backup_receipt: RecoveryBackupReceipt | None,
) -> PersistentRecoveryBundle:
    """Persistiert alle Altstandsbelege vor Bootblock und Paketmutation."""

    surface_binding = None
    systemd_binding = None
    context_binding = None
    journal_contract = None
    construction_binding = None
    try:
        full_backup_id = str(full_backup_manifest.get("backup_id") or "")
        backup_binding = _capture_context_backup_binding(
            backup_dir=backup_dir,
            manifest=full_backup_manifest,
            recovery_receipt=backup_receipt,
        )
        journal_source = recovery_journal.make_source_binding(
            kind=_bootstrap_source_kind(
                _read_version_file(repo_dir),
                old_commit is not None,
            ),
            version=_read_version_file(repo_dir),
            commit=old_commit,
            repository_present=old_commit is not None,
            repository_rebuild_required=bool(bootstrap_rebuild_git),
        )
        journal_target = recovery_journal.make_target_binding(
            commit=target_commit,
            tag=target_tag,
            role=state.ha_role,
        )
        journal_backup = recovery_journal.make_full_backup_binding(
            backup_id=full_backup_id,
            manifest_sha256=backup_binding.manifest_sha256,
        )
        construction_binding = (
            prejournal_codec.write_prejournal_construction(
                prejournal_codec.make_prejournal_construction_receipt(
                    transaction_id=transaction_id,
                    install_root=repo_dir,
                    install_user=install_user,
                    source=journal_source,
                    target=journal_target,
                    backup_dir=backup_dir,
                    full_backup=journal_backup,
                )
            )
        )
        surface_binding = recovery_surface_codec.write_recovery_surface_receipt(
            _surface_receipt_from_inventory(
                recovery_inventory,
                transaction_id=transaction_id,
                install_root=repo_dir,
                full_backup_id=full_backup_id,
            )
        )
        systemd_binding = recovery_surface_codec.write_systemd_recovery_receipt(
            recovery_surface_codec.capture_systemd_recovery_receipt(
                _recovery_bootblock_units(),
                transaction_id=transaction_id,
                install_root=repo_dir,
                full_backup_id=full_backup_id,
            )
        )
        privileged_payloads = (
            backup_receipt.privileged_backup_files
            if backup_receipt is not None
            else _privileged_backup_payload_receipts(
                backup_dir,
                full_backup_manifest,
            )
        )
        install_count, install_digest = (
            recovery_context_codec.inventory_entries_fingerprint(inventory)
        )
        web_count, web_digest = recovery_context_codec.inventory_entries_fingerprint(
            recovery_inventory.web_program_entries
        )
        context_repo = None
        if repo_recovery_contract is not None:
            context_repo = recovery_context_codec.RepoRecoveryBinding(
                expected_commit=repo_recovery_contract.expected_commit,
                tracked_git=tuple(
                    recovery_context_codec.RepoTrackedBinding(
                        relative_path=relative_path,
                        git_mode=git_mode,
                        git_object_id=git_oid,
                    )
                    for (
                        relative_path,
                        git_mode,
                        git_oid,
                        _digest,
                        _size,
                        _mode,
                        _uid,
                        _gid,
                    ) in repo_recovery_contract.tracked_files
                ),
                dirty_paths=repo_recovery_contract.dirty_paths,
            )
        context = recovery_context_codec.RecoveryContext(
            transaction_id=transaction_id,
            install_root=repo_dir,
            install_user=install_user,
            source=recovery_context_codec.RecoverySourceBinding(
                old_commit=old_commit,
                bootstrap_without_git=old_commit is None,
                bootstrap_rebuild_git=bool(bootstrap_rebuild_git),
            ),
            target=recovery_context_codec.RecoveryTargetBinding(
                commit=target_commit,
                tag=target_tag,
                role=state.ha_role,
            ),
            transition=recovery_context_codec.RecoveryTransitionBinding(
                ha_role=state.ha_role,
                config_path=state.config_path,
                config_sha256=state.config_sha256,
                config_source=(
                    recovery_context_codec.CONFIG_SOURCE_SYNTHETIC_MISSING
                    if state.bootstrap_legacy_config
                    else recovery_context_codec.CONFIG_SOURCE_FULL_BACKUP
                ),
                bootstrap_legacy_config=state.bootstrap_legacy_config,
                preinstalled_units=tuple(sorted(state.preinstalled_units)),
                preactive_units=tuple(sorted(state.preactive_units)),
                legacy_e3dc_activity=state.legacy_e3dc_activity,
            ),
            backup=backup_binding,
            repo=context_repo,
            inventory=recovery_context_codec.InventoryFingerprint(
                install_entries_count=install_count,
                install_entries_sha256=install_digest,
                web_entries_count=web_count,
                web_entries_sha256=web_digest,
                watchdog_files=tuple(sorted(recovery_inventory.watchdog_files)),
            ),
            privileged_backup_payloads=_context_privileged_payloads(
                privileged_payloads
            ),
            surface_receipt=_context_receipt_reference(surface_binding),
            systemd_receipt=_context_receipt_reference(systemd_binding),
        )
        try:
            context_binding = recovery_context_codec.write_recovery_context(context)
        except recovery_context_codec.UpdateRecoveryContextPersistenceError as exc:
            if exc.contract is None:
                raise
            context_binding = exc.contract

        immutable_receipts = recovery_journal.make_immutable_receipt_references(
            context=recovery_journal.capture_recovery_receipt_reference(
                "context",
                context_binding.context_path,
            ),
            surface=recovery_journal.capture_recovery_receipt_reference(
                "surface",
                surface_binding.path,
            ),
            systemd=recovery_journal.capture_recovery_receipt_reference(
                "systemd",
                systemd_binding.path,
            ),
        )
        initial_payload = recovery_journal.make_recovery_journal_payload(
            transaction_id=transaction_id,
            install_root=repo_dir,
            install_user=install_user,
            source=journal_source,
            target=journal_target,
            transition_id=context_binding.context_sha256,
            full_backup=journal_backup,
            immutable_receipts=immutable_receipts,
        )
        try:
            journal_contract = recovery_journal.create_recovery_journal(
                initial_payload
            )
        except recovery_journal.UpdateRecoveryJournalPersistenceError as exc:
            if exc.journal is None:
                raise
            journal_contract = recovery_journal.verify_recovery_journal(exc.journal)
        construction_binding, journal_contract = (
            _verify_prejournal_against_journal(
                construction_binding,
                journal_contract,
                context_binding,
            )
        )
        prejournal_codec.remove_prejournal_construction(
            construction_binding
        )
        construction_binding = None
        journal_contract = recovery_journal.verify_recovery_journal(
            journal_contract
        )
        return PersistentRecoveryBundle(
            journal=journal_contract,
            context=context_binding,
            surface=surface_binding,
            systemd=systemd_binding,
        )
    except BaseException as original_error:
        # Ein Fehler oder Signal zwischen Kernel-Commit und Python-Zuweisung
        # beweist niemals, dass der zuletzt geschriebene Name fehlt. Deshalb
        # werden ab dem ersten Construction-Receipt keinerlei Parent-Belege im
        # Fehlerpfad entfernt. Der nächste echte Updateeinstieg liest den
        # persistenten Präfix frisch und räumt ihn nur construction-autorisiert
        # auf beziehungsweise setzt ein bereits vorhandenes Journal fort.
        # Ohne diesen Grundsatz könnte ein tatsächlich durable Journal durch
        # das Entfernen seiner Parents verwaisen.
        raise


def _canonical_recovery_shape_sha256(mapping: dict) -> str:
    try:
        payload = json.dumps(
            mapping,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeError("Recovery-Semantik ist nicht kanonisch hashbar") from exc
    return hashlib.sha256(payload).hexdigest()


def _package_prestate_shape_sha256(
    state: PackageTransactionState,
) -> str:
    return _canonical_recovery_shape_sha256(
        _package_transaction_state_mapping(state)
    )


def _dynamic_safety_shape_sha256(contract: UpdateSafetyContract) -> str:
    ignored = {"state", "receipt_dev", "receipt_ino", "receipt_sha256"}
    return _canonical_recovery_shape_sha256(
        {
            name: getattr(contract, name)
            for name in UpdateSafetyContract.__dataclass_fields__
            if name not in ignored
        }
    )


def _static_bootblock_shape_sha256(
    contract: RecoveryBootblockContract,
) -> str:
    payload = _serialize_recovery_bootblock_contract(contract).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _bind_persistent_recovery_package_safety(
    bundle: PersistentRecoveryBundle,
    *,
    package_receipt: PreparedPackageReceipt | PackageRecoveryReceipt,
    update_safety_contract: UpdateSafetyContract | None,
    static_bootblock_contract: RecoveryBootblockContract | None,
) -> PersistentRecoveryBundle:
    """Bindet Paket- und Gate-Semantik gemeinsam vor dem ersten Paketbefehl."""

    current_journal = recovery_journal.verify_recovery_journal(bundle.journal)
    payload = current_journal.payload
    if payload.phase != recovery_journal.PHASE_PREPRODUCT:
        raise RuntimeError("Paket-/Safety-Bindung sieht keine preproduct-Phase")
    if (update_safety_contract is None) == (static_bootblock_contract is None):
        raise RuntimeError("Recovery benötigt genau einen dynamischen oder statischen Gatevertrag")
    package_transaction = _package_transaction_from_receipt(package_receipt)
    if (
        package_receipt.transaction_id != payload.transaction_id
        or package_receipt.install_root != payload.install_root
        or package_receipt.full_backup_id != payload.full_backup.backup_id
        or package_receipt.target_commit != payload.target.commit
        or package_receipt.target_tag != payload.target.tag
        or package_receipt.role != payload.target.role
    ):
        raise RuntimeError("Paket-Receipt widerspricht dem Master-Journal")

    static_digest = None
    if update_safety_contract is not None:
        current_safety = _validate_update_safety_contract(
            update_safety_contract,
            expected_state="pending",
        )
        safety_binding = recovery_journal.make_dynamic_safety_binding(
            receipt_path=current_safety.receipt_path,
            transaction_id=current_safety.transaction_id,
            install_root=payload.install_root,
            full_backup_id=current_safety.backup_id,
            target_identity_sha256=payload.target.identity_sha256,
            receipt_shape_sha256=_dynamic_safety_shape_sha256(current_safety),
        )
        gate_mode = recovery_journal.GATE_MODE_DYNAMIC
    else:
        _validate_recovery_bootblock_contract(static_bootblock_contract)
        static_digest = _static_bootblock_shape_sha256(static_bootblock_contract)
        safety_binding = recovery_journal.make_static_safety_binding(
            transaction_id=static_bootblock_contract.transaction_id,
            install_root=payload.install_root,
            full_backup_id=payload.full_backup.backup_id,
            target_identity_sha256=payload.target.identity_sha256,
            static_contract_sha256=static_digest,
        )
        gate_mode = recovery_journal.GATE_MODE_STATIC
    package_binding = recovery_journal.make_package_binding(
        path=package_receipt.receipt_path,
        transaction_id=package_receipt.transaction_id,
        install_root=package_receipt.install_root,
        full_backup_id=package_receipt.full_backup_id,
        target_identity_sha256=payload.target.identity_sha256,
        prestate_shape_sha256=_package_prestate_shape_sha256(package_transaction),
        gate_mode=gate_mode,
        static_contract_sha256=static_digest,
    )
    bound_journal = recovery_journal.bind_preproduct_recovery_receipts(
        current_journal,
        package=package_binding,
        safety=safety_binding,
    )
    return replace(bundle, journal=bound_journal)


def _advance_persistent_recovery_product_mutating(
    bundle: PersistentRecoveryBundle,
    overlay_receipt: QuiescedOverlayReceipt,
) -> PersistentRecoveryBundle:
    """Setzt die durable Produktmutationsgrenze vor dem ersten Mutator."""

    if overlay_receipt.transaction_id != bundle.journal.payload.transaction_id:
        raise RuntimeError("Overlay und Master-Journal besitzen verschiedene Transaktionen")
    receipt_reference = recovery_journal.capture_recovery_receipt_reference(
        "overlay",
        os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            QUIESCED_OVERLAY_RECEIPT_NAME,
        ),
    )
    if (
        receipt_reference.device != overlay_receipt.receipt_dev
        or receipt_reference.inode != overlay_receipt.receipt_ino
        or receipt_reference.sha256 != overlay_receipt.receipt_sha256
    ):
        raise RuntimeError("Overlay-Receipt driftete vor der Produktmutation")
    overlay_binding = recovery_journal.make_overlay_binding(
        backup_id=overlay_receipt.backup_id,
        manifest_sha256=overlay_receipt.manifest_sha256,
        receipt=receipt_reference,
    )
    advanced = recovery_journal.advance_recovery_journal(
        bundle.journal,
        recovery_journal.PHASE_PRODUCT_MUTATING,
        overlay=overlay_binding,
    )
    return replace(bundle, journal=advanced)


def _context_reference_matches_journal(
    context_reference: recovery_context_codec.RecoveryReceiptReference,
    journal_reference: recovery_journal.RecoveryReceiptReference,
) -> bool:
    return (
        context_reference.path == journal_reference.path
        and context_reference.device == journal_reference.device
        and context_reference.inode == journal_reference.inode
        and context_reference.sha256 == journal_reference.sha256
    )


def _bind_product_mutating_recovery_journal(
    *,
    expected_device: int,
    expected_inode: int,
    expected_sha256: str,
    expected_phase: str,
    repo_dir: str,
    target_commit: str,
    target_tag: str,
    role: str,
    package_receipt: PreparedPackageReceipt,
    update_safety_contract: UpdateSafetyContract | None,
    static_bootblock_contract: RecoveryBootblockContract | None,
) -> recovery_journal.RecoveryJournalContract:
    """Kreuzbindet Parent-Journal, Context und alle Receipt-Semantiken."""

    if expected_phase != recovery_journal.PHASE_PRODUCT_MUTATING:
        raise RuntimeError(
            "Target-Finalizer erhielt keine zulässige Parent-Journalphase"
        )
    contract = recovery_journal.read_recovery_journal()
    if contract is None:
        raise RuntimeError("Target-Finalizer besitzt kein Master-Journal")
    if (
        contract.journal_device != int(expected_device)
        or contract.journal_inode != int(expected_inode)
        or contract.journal_sha256 != str(expected_sha256)
        or contract.payload.phase != expected_phase
    ):
        raise RuntimeError("Target-Finalizer erhielt nicht das Parent-gebundene Master-Journal")
    payload = contract.payload
    if (
        payload.install_root != repo_dir
        or payload.target.commit != target_commit
        or payload.target.tag != target_tag
        or payload.target.role != role
        or payload.transaction_id != package_receipt.transaction_id
        or payload.full_backup.backup_id != package_receipt.full_backup_id
    ):
        raise RuntimeError("Master-Journal widerspricht dem Target-Finalizer")

    for kind in ("context", "surface", "systemd"):
        recovery_journal.verify_recovery_receipt_reference(
            getattr(payload.immutable_receipts, kind)
        )
    if payload.overlay is None:
        raise RuntimeError("product_mutating-Journal besitzt kein Overlay")
    recovery_journal.verify_recovery_receipt_reference(payload.overlay.receipt)

    context_contract = recovery_context_codec.read_recovery_context(
        expected_transaction_id=payload.transaction_id,
        expected_install_root=payload.install_root,
        expected_full_backup_id=payload.full_backup.backup_id,
    )
    if context_contract is None:
        raise RuntimeError("Master-Journal besitzt keinen Recovery-Kontext")
    context = context_contract.context
    if (
        context_contract.context_sha256 != payload.transition_id
        or context_contract.context_device
        != payload.immutable_receipts.context.device
        or context_contract.context_inode
        != payload.immutable_receipts.context.inode
        or context_contract.context_sha256
        != payload.immutable_receipts.context.sha256
        or context.transaction_id != payload.transaction_id
        or context.install_root != payload.install_root
        or context.install_user != payload.install_user
        or context.target.commit != payload.target.commit
        or context.target.tag != payload.target.tag
        or context.target.role != payload.target.role
        or context.backup.backup_id != payload.full_backup.backup_id
        or context.backup.manifest_sha256
        != payload.full_backup.manifest_sha256
        or not _context_reference_matches_journal(
            context.surface_receipt,
            payload.immutable_receipts.surface,
        )
        or not _context_reference_matches_journal(
            context.systemd_receipt,
            payload.immutable_receipts.systemd,
        )
    ):
        raise RuntimeError("Recovery-Kontext widerspricht dem Master-Journal")

    package = payload.package
    safety = payload.safety
    if package is None or safety is None:
        raise RuntimeError("Master-Journal besitzt keinen Paket-/Safety-Vertrag")
    current_package_transaction = _package_transaction_from_receipt(package_receipt)
    if (
        package.path != package_receipt.receipt_path
        or package.transaction_id != package_receipt.transaction_id
        or package.install_root != package_receipt.install_root
        or package.full_backup_id != package_receipt.full_backup_id
        or package.target_identity_sha256 != payload.target.identity_sha256
        or package.prestate_shape_sha256
        != _package_prestate_shape_sha256(current_package_transaction)
    ):
        raise RuntimeError("Paket-Semantik widerspricht dem Master-Journal")

    if update_safety_contract is not None:
        if static_bootblock_contract is not None:
            raise RuntimeError("Finalizer vermischt dynamischen und statischen Gatevertrag")
        current_safety = _validate_update_safety_contract(
            update_safety_contract,
            expected_state="pending",
        )
        if (
            package.gate_mode != recovery_journal.GATE_MODE_DYNAMIC
            or safety.mode != recovery_journal.GATE_MODE_DYNAMIC
            or safety.receipt_path != current_safety.receipt_path
            or safety.transaction_id != current_safety.transaction_id
            or safety.install_root != payload.install_root
            or safety.full_backup_id != current_safety.backup_id
            or safety.target_identity_sha256 != payload.target.identity_sha256
            or safety.receipt_shape_sha256
            != _dynamic_safety_shape_sha256(current_safety)
        ):
            raise RuntimeError("Dynamische Safety-Semantik widerspricht dem Master-Journal")
    else:
        if static_bootblock_contract is None:
            raise RuntimeError("Finalizer besitzt keinen gebundenen Gatevertrag")
        _validate_recovery_bootblock_contract(static_bootblock_contract)
        static_digest = _static_bootblock_shape_sha256(static_bootblock_contract)
        if (
            package.gate_mode != recovery_journal.GATE_MODE_STATIC
            or safety.mode != recovery_journal.GATE_MODE_STATIC
            or package.static_contract_sha256 != static_digest
            or safety.static_contract_sha256 != static_digest
            or safety.transaction_id != static_bootblock_contract.transaction_id
            or safety.install_root != payload.install_root
            or safety.full_backup_id != payload.full_backup.backup_id
            or safety.target_identity_sha256 != payload.target.identity_sha256
        ):
            raise RuntimeError("Statische Safety-Semantik widerspricht dem Master-Journal")
    return contract


def _advance_persistent_recovery_committed(
    contract: recovery_journal.RecoveryJournalContract,
) -> recovery_journal.RecoveryJournalContract:
    """Macht ausschließlich den durable Journal-Readback irreversibel."""

    try:
        return recovery_journal.advance_recovery_journal(
            contract,
            recovery_journal.PHASE_COMMITTED,
        )
    except BaseException as exc:
        try:
            current = recovery_journal.read_recovery_journal(
                contract.journal_path,
                allow_missing=True,
            )
        except Exception as read_error:
            raise UpdateSafetyManagedServiceUnquiescedError(
                "Master-Journal ist nach dem Commitversuch nicht sicher lesbar; "
                "Altstand-Rollback bleibt fail-closed gesperrt"
            ) from read_error
        if current == contract:
            raise
        if (
            current is not None
            and current.payload.phase == recovery_journal.PHASE_COMMITTED
            and _same_recovery_journal_transaction_shape(current, contract)
        ):
            committed = recovery_journal.verify_recovery_journal(current)
            if isinstance(exc, Exception):
                return committed
            raise UpdateSafetyPostCommitError(
                "Signal oder Prozessabbruch trat nach durable committed "
                "Master-Journal ein; Altstand-Rollback ist verboten"
            ) from exc
        raise UpdateSafetyManagedServiceUnquiescedError(
            "Master-Journal driftete nach dem Commitversuch; Altstand-Rollback "
            "bleibt fail-closed gesperrt"
        ) from exc


def _same_recovery_journal_transaction_shape(
    candidate: recovery_journal.RecoveryJournalContract,
    original: recovery_journal.RecoveryJournalContract,
) -> bool:
    """Vergleicht die phasenunabhängige Identität derselben Transaktion."""

    return (
        isinstance(candidate, recovery_journal.RecoveryJournalContract)
        and isinstance(original, recovery_journal.RecoveryJournalContract)
        and candidate.journal_path == original.journal_path
        and candidate.payload.transaction_id == original.payload.transaction_id
        and candidate.payload.install_root == original.payload.install_root
        and candidate.payload.target == original.payload.target
        and candidate.payload.full_backup == original.payload.full_backup
        and candidate.payload.binding_sha256
        == original.payload.binding_sha256
        and candidate.payload.phase_state_sha256
        == original.payload.phase_state_sha256
    )


def _read_matching_committed_recovery_journal(
    original: recovery_journal.RecoveryJournalContract,
    *,
    allow_missing: bool = False,
) -> recovery_journal.RecoveryJournalContract | None:
    """Liest nur den durable Commit derselben gebundenen Transaktion."""

    current = recovery_journal.read_recovery_journal(
        original.journal_path,
        allow_missing=allow_missing,
    )
    if current is None:
        return None
    if (
        current.payload.phase == recovery_journal.PHASE_COMMITTED
        and _same_recovery_journal_transaction_shape(current, original)
    ):
        return recovery_journal.verify_recovery_journal(current)
    return None


def _advance_persistent_recovery_rolled_back(
    contract: recovery_journal.RecoveryJournalContract,
) -> recovery_journal.RecoveryJournalContract:
    """Entscheidet nach verifiziertem Offline-Preimage dauerhaft den Altstand.

    systemd-Gate-Cleanup, Dienststart und Apache-Wiederherstellung dürfen danach
    noch idempotent offen sein. Die irreversible Richtung bleibt dennoch der
    alte Produktstand; ein neuer Ziel-Commit ist ab dieser Phase verboten.
    """

    if contract.payload.phase not in {
        recovery_journal.PHASE_PREPRODUCT,
        recovery_journal.PHASE_PRODUCT_MUTATING,
    }:
        raise RuntimeError(
            "Nur eine laufende Recovery-Phase darf rolled_back werden"
        )
    try:
        return recovery_journal.advance_recovery_journal(
            contract,
            recovery_journal.PHASE_ROLLED_BACK,
        )
    except BaseException as exc:
        try:
            current = recovery_journal.read_recovery_journal(
                contract.journal_path,
                allow_missing=True,
            )
        except Exception as read_error:
            raise UpdateSafetyManagedServiceUnquiescedError(
                "Master-Journal ist nach dem Rollback-Abschluss nicht sicher "
                "lesbar; terminaler Cleanup bleibt gesperrt"
            ) from read_error
        if current == contract:
            raise
        if (
            current is not None
            and current.payload.phase == recovery_journal.PHASE_ROLLED_BACK
            and _same_recovery_journal_transaction_shape(current, contract)
        ):
            rolled_back = recovery_journal.verify_recovery_journal(current)
            if isinstance(exc, Exception):
                return rolled_back
            raise UpdateSafetyManagedServiceUnquiescedError(
                "Signal trat nach durable rolled_back ein; ausschließlich "
                "terminaler Cleanup ist noch zulässig"
            ) from exc
        raise UpdateSafetyManagedServiceUnquiescedError(
            "Master-Journal driftete am Rollback-Abschluss; terminaler "
            "Cleanup bleibt fail-closed gesperrt"
        ) from exc


def _read_matching_rolled_back_recovery_journal(
    original: recovery_journal.RecoveryJournalContract,
    *,
    allow_missing: bool = False,
) -> recovery_journal.RecoveryJournalContract | None:
    """Liest nur den durable Rücklauf derselben gebundenen Transaktion."""

    current = recovery_journal.read_recovery_journal(
        original.journal_path,
        allow_missing=allow_missing,
    )
    if current is None:
        return None
    if (
        current.payload.phase == recovery_journal.PHASE_ROLLED_BACK
        and _same_recovery_journal_transaction_shape(current, original)
    ):
        return recovery_journal.verify_recovery_journal(current)
    return None


def _cleanup_terminal_recovery_bundle(
    bundle: PersistentRecoveryBundle,
) -> None:
    """Entfernt Begleitbelege idempotent; das Master-Journal immer zuletzt."""

    if not isinstance(bundle, PersistentRecoveryBundle):
        raise TypeError("Terminaler Recovery-Cleanup besitzt kein Bundle")
    terminal = recovery_journal.verify_recovery_journal(bundle.journal)
    if terminal.payload.phase not in {
        recovery_journal.PHASE_COMMITTED,
        recovery_journal.PHASE_ROLLED_BACK,
    }:
        raise RuntimeError("Nichtterminales Master-Journal darf nicht bereinigt werden")

    if bundle.systemd is not None and os.path.lexists(bundle.systemd.path):
        recovery_surface_codec.remove_systemd_recovery_receipt(bundle.systemd)
    if bundle.surface is not None and os.path.lexists(bundle.surface.path):
        recovery_surface_codec.remove_recovery_surface_receipt(bundle.surface)
    if bundle.context is not None and os.path.lexists(bundle.context.context_path):
        recovery_context_codec.remove_recovery_context(bundle.context)

    remaining = tuple(
        path
        for path in (
            terminal.payload.immutable_receipts.systemd.path,
            terminal.payload.immutable_receipts.surface.path,
            terminal.payload.immutable_receipts.context.path,
        )
        if os.path.lexists(path)
    )
    if remaining:
        raise RuntimeError(
            "Recovery-Begleitbelege blieben vor dem Journal-Cleanup stehen: "
            + ", ".join(remaining)
        )
    recovery_journal.remove_recovery_journal(terminal)


def _refresh_terminal_recovery_bundle(
    bundle: PersistentRecoveryBundle,
    *,
    phase: str,
) -> PersistentRecoveryBundle:
    """Bindet den terminalen Journal-Inode derselben Transaktion erneut."""

    if phase == recovery_journal.PHASE_COMMITTED:
        terminal = _read_matching_committed_recovery_journal(
            bundle.journal,
            allow_missing=True,
        )
    elif phase == recovery_journal.PHASE_ROLLED_BACK:
        terminal = _read_matching_rolled_back_recovery_journal(
            bundle.journal,
            allow_missing=True,
        )
    else:
        raise ValueError("Unbekannte terminale Recovery-Phase")
    if terminal is None:
        raise RuntimeError(
            "[E3DC-UPD-TERMINAL-JOURNAL-001] Master-Journal besitzt nicht "
            f"die erwartete terminale Phase {phase}. Lösung: Keine Parent-, "
            "Paket- oder Safety-Datei löschen; führe `sudo stat -c "
            f"'%U:%G %a %h %s %n' {bundle.journal.journal_path}` und danach "
            "`sudo journalctl -b -u 'e3dc-*update*' --no-pager` aus."
        )
    return replace(bundle, journal=terminal)


def _cleanup_terminal_update_artifacts(
    *,
    bundle: PersistentRecoveryBundle,
    offline_receipt: OfflinePackageReceipt | None,
    overlay_receipt: QuiescedOverlayReceipt | None,
    package_receipt: PreparedPackageReceipt | PackageRecoveryReceipt | None,
    update_safety_contract: UpdateSafetyContract | None,
    terminal_label: str,
) -> None:
    """Räumt nur nach terminalem Journal alle Transaktionsartefakte geordnet auf."""

    terminal = recovery_journal.verify_recovery_journal(bundle.journal)
    expected_phase = terminal.payload.phase
    expected_transaction_id = terminal.payload.transaction_id
    if expected_phase not in {
        recovery_journal.PHASE_COMMITTED,
        recovery_journal.PHASE_ROLLED_BACK,
    }:
        raise RuntimeError("Artefakt-Cleanup besitzt kein terminales Journal")

    def assert_terminal_cleanup_authority():
        """Bindet vor jeder Cleanup-Klasse exakt denselben Journal-Inode."""

        current = recovery_journal.verify_recovery_journal(terminal)
        if (
            current.payload.phase != expected_phase
            or current.payload.transaction_id != expected_transaction_id
        ):
            raise RuntimeError(
                "Terminale Cleanup-Autorität wechselte Phase oder Transaktion"
            )
        return current

    assert_terminal_cleanup_authority()
    if not _cleanup_terminal_offline_package_receipt(
        offline_receipt,
        terminal_state=terminal_label,
    ):
        raise RuntimeError("Offline-Paketartefakte blieben unvollständig")
    assert_terminal_cleanup_authority()
    _cleanup_stale_target_execution_snapshots(
        _trusted_same_filesystem_snapshot_parent(
            bundle.journal.payload.install_root
        ),
        prefixes=(TARGET_FINALIZER_SNAPSHOT_PREFIX,),
    )

    if overlay_receipt is not None and os.path.lexists(
        os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            QUIESCED_OVERLAY_RECEIPT_NAME,
        )
    ):
        assert_terminal_cleanup_authority()
        _remove_quiesced_overlay_receipt_and_tree(overlay_receipt)

    current_package = _read_prepared_package_receipt(allow_missing=True)
    if current_package is not None:
        if (
            package_receipt is None
            or current_package.transaction_id
            != package_receipt.transaction_id
            or current_package.install_root != package_receipt.install_root
            or current_package.full_backup_id != package_receipt.full_backup_id
            or current_package.package_transaction
            != package_receipt.package_transaction
        ):
            raise RuntimeError("Terminales Paket-Receipt gehört zu einer fremden Transaktion")
        assert_terminal_cleanup_authority()
        _remove_exact_prepared_package_receipt(current_package)

    current_safety = _read_update_safety_contract(allow_missing=True)
    if current_safety is not None:
        if (
            update_safety_contract is None
            or not _same_update_safety_transaction_shape(
                current_safety,
                update_safety_contract,
            )
        ):
            raise RuntimeError("Terminales Safety-Receipt gehört zu einer fremden Transaktion")
        assert_terminal_cleanup_authority()
        _remove_exact_update_safety_receipt(current_safety)

    current_terminal = assert_terminal_cleanup_authority()
    _cleanup_terminal_recovery_bundle(
        replace(bundle, journal=current_terminal)
    )


def _finish_rolled_back_update_cleanup(
    *,
    bundle: PersistentRecoveryBundle,
    offline_receipt: OfflinePackageReceipt | None,
    overlay_receipt: QuiescedOverlayReceipt | None,
    package_receipt: PreparedPackageReceipt | PackageRecoveryReceipt | None,
    update_safety_contract: UpdateSafetyContract | None,
) -> PersistentRecoveryBundle:
    """Bindet rolled_back erneut und führt ausschließlich terminalen Cleanup aus."""

    terminal = _refresh_terminal_recovery_bundle(
        bundle,
        phase=recovery_journal.PHASE_ROLLED_BACK,
    )
    _cleanup_terminal_update_artifacts(
        bundle=terminal,
        offline_receipt=offline_receipt,
        overlay_receipt=overlay_receipt,
        package_receipt=package_receipt,
        update_safety_contract=update_safety_contract,
        terminal_label="wiederhergestellte Altstand",
    )
    return terminal


def _restore_recovery_surface(
    inventory: RecoverySurfaceInventory,
    state: TransitionState,
    *,
    restore_legacy_systemd_surface: bool = True,
) -> None:
    # Die aktuellen Zustände aller privilegierten Nebenflächen werden als
    # Restore-Guard gebunden, bevor die erste davon verändert wird. Der Guard
    # ist ein interner Fremdänderungsschutz, keine Nutzerhürde.
    root_file_guard = (
        capture_root_file_restore_guard(inventory.root_file_preimages)
        if inventory.root_file_preimages is not None
        else None
    )
    crontab_guard = (
        capture_crontab_restore_guard(inventory.crontab_preimages)
        if inventory.crontab_preimages is not None
        else None
    )
    if root_file_guard is not None:
        restore_root_file_preimages(inventory.root_file_preimages, root_file_guard)
    if crontab_guard is not None:
        restore_crontab_preimages(inventory.crontab_preimages, crontab_guard)
    # Die beiden privilegierten Web-Aktoren werden vom Ziel-Finalizer früh
    # mutiert. Ihr Rücklauf hat deshalb Vorrang vor allen nachfolgenden,
    # voneinander unabhängigen Recovery-Schritten.
    _restore_root_managed_preimages(inventory.root_managed_files)
    _remove_entries_not_in_inventory(
        "/var/www/html",
        inventory.web_program_entries,
        excluded_top=("data", "logs", "ramdisk", "tmp"),
    )
    if restore_legacy_systemd_surface:
        allowed_units = set(_catalog_units_strict()) | {
            "piguard.service",
            "e3dc.service",
        }
        for unit in sorted(allowed_units - set(state.preinstalled_units)):
            path = os.path.join("/etc/systemd/system", unit)
            if os.path.lexists(path):
                os.unlink(path)
    for path in ("/usr/local/bin/boot_notify.sh", "/usr/local/bin/pi_guard.sh"):
        if path not in inventory.watchdog_files and os.path.lexists(path):
            os.unlink(path)
    _restore_apache_security_preimage(
        inventory.apache_security,
        restore_activity=False,
    )
    if restore_legacy_systemd_surface:
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
                raise RuntimeError(
                    f"Enablement von {unit} konnte nicht wiederhergestellt werden"
                )


def _read_commit_text(
    repo_dir: str,
    commit: str,
    path: str,
    install_user: str,
    *,
    root_authority: bool = False,
) -> str:
    verified = _validate_full_commit(commit)
    raw = _read_commit_blob(
        repo_dir,
        verified,
        path,
        install_user,
        maximum=1024 * 1024,
        **_root_git_call_kwargs(root_authority),
    )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{path} ist im verifizierten Ziel-Commit kein UTF-8") from exc


def _fetch_target_commit(
    repo_dir: str,
    install_user: str,
    target_tag: str | None,
    *,
    root_authority: bool = False,
) -> str:
    _require_bound_origin(
        repo_dir,
        install_user,
        **_root_git_call_kwargs(root_authority),
    )
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
            **_root_git_call_kwargs(root_authority),
        )
        if not result["success"]:
            raise RuntimeError("Release-Tag-Fetch fehlgeschlagen: " + result["stderr"].strip())
        object_type = _git_argv(
            repo_dir,
            install_user,
            "cat-file",
            "-t",
            storage_ref,
            timeout=15,
            **_root_git_call_kwargs(root_authority),
        )
        if not object_type["success"] or object_type["stdout"].strip() != "tag":
            raise RuntimeError(f"Release-Tag {target_tag} ist nicht annotiert")
        tag_object = _git_argv(
            repo_dir,
            install_user,
            "rev-parse",
            "--verify",
            storage_ref + "^{tag}",
            timeout=15,
            **_root_git_call_kwargs(root_authority),
        )
        if (
            not tag_object["success"]
            or not _exact_commit_matches(tag_object["stdout"].strip(), official[storage_ref])
        ):
            raise RuntimeError(f"Release-Tag {target_tag} weicht vom offiziellen Tagobjekt ab")
        commit = _resolve_git_commit(
            repo_dir,
            storage_ref,
            install_user,
            **_root_git_call_kwargs(root_authority),
        )
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
            **_root_git_call_kwargs(root_authority),
        )
        if not result["success"]:
            raise RuntimeError("git fetch origin/main fehlgeschlagen: " + result["stderr"].strip())
        commit = _resolve_git_commit(
            repo_dir,
            "refs/remotes/origin/main",
            install_user,
            **_root_git_call_kwargs(root_authority),
        )
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
    *,
    root_authority: bool = False,
) -> str:
    version = _read_commit_text(
        repo_dir,
        target_commit,
        "VERSION",
        install_user,
        **_root_git_call_kwargs(root_authority),
    ).strip().lstrip("v")
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
    stable_commit = _fetch_target_commit(
        repo_dir,
        install_user,
        stable,
        **_root_git_call_kwargs(root_authority),
    )
    if not stable_commit or not _exact_commit_matches(stable_commit, target_commit):
        raise RuntimeError(
            "Stable-Tag der Ziel-Policy verweist nicht exakt auf den Ziel-Commit"
        )
    return stable


def _validate_local_target_release_binding(
    policy: dict,
    repo_dir: str,
    target_commit: str,
    target_tag: str,
    install_user: str,
    *,
    root_authority: bool = False,
) -> str:
    """Prüft nach dem Freeze nur zuvor geladene Git-Objekte, ohne Netzwerk."""

    commit = _validate_full_commit(target_commit)
    tag = _normalize_release_tag(target_tag)
    version = _read_commit_text(
        repo_dir,
        commit,
        "VERSION",
        install_user,
        **_root_git_call_kwargs(root_authority),
    ).strip().lstrip("v")
    stable = _normalize_release_tag(str(policy.get("stable_release") or ""))
    if (
        not version
        or str(policy.get("version") or "").strip().lstrip("v") != version
        or stable != _normalize_release_tag(version)
        or stable != tag
        or policy.get("run_permissions") is not True
    ):
        raise RuntimeError("Lokale Ziel-Policy, VERSION und Stable-Tag widersprechen sich")
    storage_ref = f"refs/tags/{tag}"
    object_type = _git_argv(
        repo_dir,
        install_user,
        "cat-file",
        "-t",
        storage_ref,
        timeout=15,
        **_root_git_call_kwargs(root_authority),
    )
    if not object_type.get("success") or object_type.get("stdout", "").strip() != "tag":
        raise RuntimeError(f"Lokal vorbereitetes Release-Tag {tag} ist nicht annotiert")
    resolved = _resolve_git_commit(
        repo_dir,
        storage_ref,
        install_user,
        **_root_git_call_kwargs(root_authority),
    )
    if not resolved or not _exact_commit_matches(resolved, commit):
        raise RuntimeError("Lokal vorbereitetes Release-Tag verweist nicht auf die Ziel-SHA")
    return stable


def _assert_tree_no_symlinks(root: str) -> None:
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        for name in (*dirnames, *filenames):
            path = os.path.join(directory, name)
            if stat.S_ISLNK(os.lstat(path).st_mode):
                raise RuntimeError(f"Symlink in Release-Baum nicht erlaubt: {path}")


def _ensure_apache_canonical_running() -> None:
    """Aktiviert Apache nach erfolgreicher Projektion eindeutig für den Healthcheck."""

    for command in (
        ["sudo", "systemctl", "unmask", "apache2.service"],
        ["sudo", "systemctl", "enable", "apache2.service"],
        ["sudo", "systemctl", "start", "apache2.service"],
    ):
        result = _run_argv(command, timeout=30)
        if not result.get("success"):
            raise RuntimeError(
                "Apache konnte für den erfolgreichen Release-Endzustand nicht "
                "aktiviert und gestartet werden"
            )
    available, active, unit_file_state = _capture_apache_service_prestate()
    if not available or not active or unit_file_state != "enabled":
        raise RuntimeError(
            "Apache besitzt nach Enable+Start keinen kanonischen aktiven Endzustand"
        )


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
    if not _ensure_rsync_available(allow_install=False):
        raise RuntimeError("rsync ist nicht verfuegbar")
    _prepare_webroot_dirs()
    result = _run_argv(
        [
            "sudo", "rsync", "-a", "--delete", "--delete-delay",
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
        reload_apache=False,
        allow_mutation=True,
    ):
        raise RuntimeError(
            "Apache-Schutz für Daten-, Log-, Ramdisk- und Temp-Pfade "
            "konnte unter gestopptem Apache nicht vorbereitet werden"
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


def _verify_projected_bootstrap_metadata_without_env(projection: dict) -> None:
    """Beweist die dauerhafte Zielbindung in einem vollständig leeren Prozessenv."""

    user, home, root, venv = (
        str(projection.get(key) or "")
        for key in ("install_user", "home_dir", "install_path", "venv_path")
    )
    env_binary = Path("/usr/bin/env")
    _assert_root_controlled_directory_chain(env_binary.parent)
    env_target = env_binary.resolve(strict=True)
    _assert_root_controlled_directory_chain(env_target.parent)
    env_metadata = env_target.stat()
    if (
        not stat.S_ISREG(env_metadata.st_mode)
        or env_metadata.st_uid != 0
        or env_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not env_metadata.st_mode & 0o111
    ):
        raise RuntimeError("Fester env-i-Starter besitzt unsichere Metadaten")

    probe = r"""
import json, os, sys
if any(name.startswith("E3DC_BOOTSTRAP_") for name in os.environ):
    raise RuntimeError("Bootstrap-Umgebung ist in die Zielprobe durchgesickert")
sys.path.insert(0, sys.argv[1])
from Installer.installer_config import get_install_path, get_install_user, get_venv_path
from Installer.transition_context import get_transition_context
user = get_install_user()
context = get_transition_context(require_trusted=True)
print(json.dumps([user, get_install_path(user), context.home_dir, get_venv_path(user),
                  context.install_user, context.install_path, context.venv_path]))
""".strip()
    result = _run_argv(
        [
            str(env_binary),
            "-i",
            "LC_ALL=C.UTF-8",
            "LANG=C.UTF-8",
            "PYTHONNOUSERSITE=1",
            "PYTHONDONTWRITEBYTECODE=1",
            _trusted_system_python(),
            "-I",
            "-B",
            "-c",
            probe,
            root,
        ],
        timeout=30,
    )
    if not result.get("success"):
        raise RuntimeError(
            "Bootstrap-Metadaten sind ohne Prozessumgebung nicht auflösbar: "
            + _combined_process_diagnostics(result, maximum=4096)
        )
    try:
        actual = json.loads(str(result.get("stdout") or "").strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("Bootstrap-Metadatenprobe lieferte kein gültiges JSON") from exc
    expected = [user, root, home, venv or os.path.join(home, ".venv_e3dc"), user, root, venv]
    if actual != expected:
        raise RuntimeError("Bootstrap-Metadatenprobe weicht vom projizierten Sollkontext ab")


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
    expected_package_receipt_sha256: str,
    expected_package_receipt_device: int,
    expected_package_receipt_inode: int,
    expected_package_full_backup_id: str,
    expected_recovery_journal_sha256: str,
    expected_recovery_journal_device: int,
    expected_recovery_journal_inode: int,
    expected_recovery_journal_phase: str,
    expected_apache_available: bool,
    expected_apache_active: bool,
    expected_apache_unit_file_state: str,
    static_recovery_contract_json: str = "",
    update_safety_transaction: str | None = None,
    update_safety_receipt_sha256: str | None = None,
    update_safety_service_unit: str | None = None,
    update_safety_runtime_directory: str | None = None,
    update_safety_token_path: str | None = None,
    explicit_download_bootstrap: bool = False,
    headless: bool = True,
    privileged_preimages=None,
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
            or not safety_contract.apache_completion_required
            or safety_contract.apache_available != expected_apache_available
            or safety_contract.apache_was_active != expected_apache_active
            or safety_contract.apache_unit_file_state
            != str(expected_apache_unit_file_state or "").strip().lower()
        ):
            raise RuntimeError("Target-Finalizer sieht nicht das pending Sicherheitsreceipt")
        _verify_update_safety_marker(safety_contract, expected_present=True)
        _reload_and_verify_update_safety_dropins(safety_contract, expected_present=True)
        _assert_managed_finalizer_service(safety_contract)
        _assert_strict_update_writer_quiescence(
            repo_dir=target_root,
            transaction_id=safety_contract.transaction_id,
        )
    static_bootblock_contract = None
    if static_recovery_contract_json:
        if safety_contract is not None:
            raise RuntimeError(
                "Dynamischer und statischer Update-Bootblock dürfen nicht kombiniert werden"
            )
        static_bootblock_contract = _parse_recovery_bootblock_contract(
            static_recovery_contract_json
        )
        _assert_managed_finalizer_service(static_bootblock_contract)
        _assert_strict_update_writer_quiescence(
            repo_dir=target_root,
            transaction_id=static_bootblock_contract.transaction_id,
        )

    expected_package_transaction = (
        safety_contract.transaction_id
        if safety_contract is not None
        else (
            static_bootblock_contract.transaction_id
            if static_bootblock_contract is not None
            else None
        )
    )
    package_receipt = _require_prepared_package_receipt_binding(
        expected_state="prepared",
        expected_transaction_id=expected_package_transaction,
        expected_install_root=target_root,
        expected_full_backup_id=expected_package_full_backup_id,
        expected_receipt_sha256=expected_package_receipt_sha256,
        expected_receipt_dev=expected_package_receipt_device,
        expected_receipt_ino=expected_package_receipt_inode,
    )
    if (
        safety_contract is not None
        and package_receipt.full_backup_id != safety_contract.backup_id
    ):
        raise RuntimeError("Paket-Receipt widerspricht dem Recovery-Backup")
    if (
        not package_receipt.apache_completion_required
        or package_receipt.target_commit != commit
        or package_receipt.target_tag != tag
        or package_receipt.role != role
        or package_receipt.apache_available != expected_apache_available
        or package_receipt.apache_was_active != expected_apache_active
        or package_receipt.apache_unit_file_state
        != str(expected_apache_unit_file_state or "").strip().lower()
        or package_receipt.static_recovery_contract_json
        != str(static_recovery_contract_json or "")
    ):
        raise RuntimeError("Paket-Receipt widerspricht dem Release-/Apache-Abschluss")

    recovery_journal_contract = _bind_product_mutating_recovery_journal(
        expected_device=expected_recovery_journal_device,
        expected_inode=expected_recovery_journal_inode,
        expected_sha256=expected_recovery_journal_sha256,
        expected_phase=expected_recovery_journal_phase,
        repo_dir=target_root,
        target_commit=commit,
        target_tag=tag,
        role=role,
        package_receipt=package_receipt,
        update_safety_contract=safety_contract,
        static_bootblock_contract=static_bootblock_contract,
    )

    actual_commit = _resolve_git_commit(
        target_root,
        "HEAD",
        get_install_user(),
        **_root_git_call_kwargs(explicit_download_bootstrap),
    )
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
    policy = _read_policy_from_commit(
        target_root,
        commit,
        install_user,
        **_root_git_call_kwargs(explicit_download_bootstrap),
    )
    _validate_local_target_release_binding(
        policy,
        target_root,
        commit,
        tag,
        install_user,
        **_root_git_call_kwargs(explicit_download_bootstrap),
    )
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
    contract_transaction_id = (
        safety_contract.transaction_id
        if safety_contract is not None
        else (
            static_bootblock_contract.transaction_id
            if static_bootblock_contract is not None
            else package_receipt.transaction_id
        )
    )
    if package_receipt.prepared_state is None:
        raise RuntimeError("Paket-Receipt besitzt keinen vorbereiteten Postzustand")
    receipt_venv_state, receipt_venv_path = _finalizer_venv_contract(
        package_receipt.package_transaction
    )
    if (
        package_receipt.package_transaction.install_user != install_user
        or receipt_venv_state != expected_venv_state
        or receipt_venv_path != expected_venv_path
    ):
        raise RuntimeError("Paket-Receipt widerspricht dem Finalizer-Preimage")
    _verify_prepared_package_policy_applied(
        policy,
        install_user,
        expected_transaction_id=contract_transaction_id,
        prepared=package_receipt.prepared_state,
    )
    _validate_watchdog_runtime_venv_contract(
        required=watchdog_runtime_required,
        expected_venv_state=expected_venv_state,
        expected_venv_path=expected_venv_path,
        target_root=target_root,
        install_user=install_user,
    )
    bootstrap_projection = None

    _announce_finalizer_phase(2, phase_total, "Paket- und Repositoryzustand herstellen")
    _secure_repo_permissions(
        target_root,
        install_user,
        expected_commit=commit,
        **(
            {"root_git_authority": True}
            if explicit_download_bootstrap
            else {}
        ),
    )
    _verify_worktree_policy(target_root, policy)
    _migrate_bootstrap_legacy_config(target_root, state)
    if expected_venv_path:
        _harden_existing_release_venv(
            install_user,
            expected_venv_path,
        )
    if explicit_download_bootstrap:
        projection_config, projection_sha256 = state.config, state.config_sha256
        if state.bootstrap_legacy_config:
            projection_config, migrated_raw = _read_json_nofollow(state.config_path)
            if str(projection_config.get("ha_mode") or "").strip().lower() != state.ha_role:
                raise RuntimeError("Migrierte V4-Konfiguration verlor die gebundene Rolle")
            projection_sha256 = hashlib.sha256(migrated_raw).hexdigest()
        bootstrap_projection = project_download_bootstrap_metadata(
            target_root,
            install_user,
            venv_path=expected_venv_path,
            expected_v4_config=projection_config,
            expected_v4_sha256=projection_sha256,
        )
        state = replace(
            state, config=dict(bootstrap_projection["config"]),
            config_sha256=str(bootstrap_projection["config_sha256"]),
        )
        expected_config_sha256 = state.config_sha256

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
    if bootstrap_projection is not None and state.bootstrap_legacy_config:
        legacy_config, legacy_raw = _read_json_nofollow(state.config_path)
        if (
            any(legacy_config.get(key) != value for key, value in state.config.items())
            or set(legacy_config) - set(state.config) - set(WEB_CONFIG_START_DEFAULTS)
        ):
            raise RuntimeError("Legacy-Webprojektion veränderte gebundene Fachwerte")
        state = replace(
            state, config=legacy_config,
            config_sha256=hashlib.sha256(legacy_raw).hexdigest(),
        )
        expected_config_sha256 = state.config_sha256
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
    if static_bootblock_contract is not None:
        _assert_strict_update_writer_quiescence(
            repo_dir=target_root,
            transaction_id=static_bootblock_contract.transaction_id,
        )
        _create_update_safety_start_token(
            static_bootblock_contract,
            repo_dir=target_root,
        )
    if not _restart_v4_services(
        headless=headless,
        services=restart_services,
        transition_state=state,
        prepared_start_only=safety_contract is not None,
        projected_piguard=projected_piguard,
    ):
        raise RuntimeError("Erwartete Dienste konnten nicht vollständig gestartet werden")
    # Der Webserver bleibt bis hinter der durable Commit-Grenze vollständig
    # inaktiv. PHP, Dienste und HA werden daher zunächst ohne HTTP geprüft.
    if not _post_update_healthcheck(
        restart_services,
        transition_state=state,
        projected_piguard=projected_piguard,
        check_http=False,
    ):
        _stop_v4_services(restart_services)
        raise RuntimeError("Dienst-/HA-Gesundheitsgate vor Apache-Freigabe fehlgeschlagen")
    _verify_apache_quiesced_before_commit(
        expected_available=expected_apache_available,
        expected_active=expected_apache_active,
        expected_unit_file_state=expected_apache_unit_file_state,
    )
    if safety_contract is None and not refresh_bound_watchdog():
        _stop_v4_services(restart_services)
        raise RuntimeError("Watchdog-Guard konnte nach dem finalen Dienststart nicht aktualisiert werden")
    _announce_finalizer_phase(6, phase_total, "Boot-, Paket- und Releasevertrag verifizieren")

    try:
        from .boot_sanity import check_boot_sanity
        boot_ok = check_boot_sanity(verbose=True)
    except Exception as exc:
        raise RuntimeError(f"Boot-Sanitycheck konnte nicht ausgeführt werden: {exc}") from exc
    if not boot_ok:
        _stop_v4_services(restart_services)
        raise RuntimeError("Boot-Sanity-Gate fehlgeschlagen")

    if bootstrap_projection is not None:
        _verify_projected_bootstrap_metadata_without_env(bootstrap_projection)
    _verify_transition_state(state)
    if expected_config_state == "present" or bootstrap_projection is not None:
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
        _validate_prepared_package_receipt(
            package_receipt,
            expected_state="prepared",
        )
    else:
        _validate_prepared_package_receipt(
            package_receipt,
            expected_state="prepared",
        )
        if static_bootblock_contract is None:
            raise RuntimeError("Statischer Abschluss besitzt keinen Bootblockvertrag")
        _assert_managed_finalizer_service(static_bootblock_contract)

    recovery_journal_contract = _advance_persistent_recovery_committed(
        recovery_journal_contract
    )

    if safety_contract is not None:
        try:
            committed_contract = _commit_update_safety_receipt(safety_contract)
            package_receipt = _commit_prepared_package_receipt(package_receipt)
            _finish_committed_update_safety_cleanup(
                committed_contract,
                remove_receipt=False,
            )
            _announce_finalizer_phase(
                7,
                phase_total,
                "Apache öffnen und initiale Prognose aktualisieren",
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
            raise UpdateSafetyPostCommitError(
                "Target-Finalizer brach nach durable committed Master-Journal "
                "im PostCommit-Abschluss ab"
            ) from exc
    else:
        try:
            package_receipt = _commit_prepared_package_receipt(package_receipt)
            _clear_recovery_bootblock_marker(static_bootblock_contract)
            _remove_persistent_recovery_bootblock(static_bootblock_contract)
            _complete_bound_apache_after_commit(
                expected_available=expected_apache_available,
                expected_active=expected_apache_active,
                expected_unit_file_state=expected_apache_unit_file_state,
            )
            _announce_finalizer_phase(
                7,
                phase_total,
                "Apache öffnen und initiale Prognose aktualisieren",
            )
            run_initial_forecast(os.path.join(target_root, "Installer"))
            _project_bare_metal_logrotate_config(
                repo_dir=target_root,
                target_commit=commit,
                install_user=install_user,
            )
        except BaseException as exc:
            if isinstance(exc, UpdateSafetyPostCommitError):
                raise
            raise UpdateSafetyPostCommitError(
                "Statischer Target-Finalizer brach nach durable committed "
                "Master-Journal im PostCommit-Abschluss ab"
            ) from exc


def _target_execution_archive_entries(
    *,
    repo_dir: str,
    target_commit: str,
    install_user: str,
    root_authority: bool = False,
) -> dict[str, tuple[bytes, int]]:
    """Liest den vollständigen ausführbaren Installer-Baum direkt aus dem Commit."""

    required = set(TARGET_EXECUTION_SNAPSHOT_ROOT_FILES) | set(TARGET_FINALIZER_RELATIVE_FILES)
    return read_commit_entries(
        repo_dir,
        _validate_full_commit(target_commit),
        (*TARGET_EXECUTION_SNAPSHOT_ROOT_FILES, "Installer"),
        required_paths=required,
        run_as_user=_commit_reader_user(
            install_user,
            root_authority=root_authority,
        ),
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
    package_receipt: PreparedPackageReceipt,
    recovery_journal_contract: recovery_journal.RecoveryJournalContract,
    apache_preimage: ApacheSecurityPreimage,
    static_bootblock_contract: RecoveryBootblockContract | None = None,
    update_safety_contract: UpdateSafetyContract | None = None,
    explicit_download_bootstrap: bool = False,
) -> None:
    """Startet den Zielprozess direkt oder im crash-sicheren transienten Service."""

    lock_fd = _required_update_lock_fd()
    install_user = get_install_user()
    package_receipt = _validate_prepared_package_receipt(
        package_receipt,
        expected_state="prepared",
    )
    recovery_journal_contract = recovery_journal.verify_recovery_journal(
        recovery_journal_contract
    )
    if (
        recovery_journal_contract.payload.phase
        != recovery_journal.PHASE_PRODUCT_MUTATING
        or recovery_journal_contract.payload.install_root != repo_dir
        or recovery_journal_contract.payload.target.commit != target_commit
        or recovery_journal_contract.payload.target.tag != target_tag
        or recovery_journal_contract.payload.target.role != state.ha_role
        or recovery_journal_contract.payload.transaction_id
        != package_receipt.transaction_id
        or recovery_journal_contract.payload.full_backup.backup_id
        != package_receipt.full_backup_id
    ):
        raise RuntimeError(
            "Master-Journal widerspricht dem Finalizer-Handoff"
        )
    if (
        package_receipt.install_root != repo_dir
        or package_receipt.package_transaction.install_user != install_user
    ):
        raise RuntimeError("Paket-Receipt widerspricht dem Finalizer-Handoff")
    if update_safety_contract is not None:
        update_safety_contract = _validate_update_safety_contract(
            update_safety_contract,
            expected_state="pending",
        )
        if (
            update_safety_contract.target_commit != target_commit
            or update_safety_contract.target_tag != _normalize_release_tag(target_tag)
            or update_safety_contract.role != state.ha_role
            or update_safety_contract.transaction_id != package_receipt.transaction_id
            or update_safety_contract.backup_id != package_receipt.full_backup_id
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
    if update_safety_contract is not None and static_bootblock_contract is not None:
        raise RuntimeError(
            "Target-Finalizer erhielt dynamischen und statischen Bootblock zugleich"
        )
    if static_bootblock_contract is not None:
        _validate_recovery_bootblock_contract(static_bootblock_contract)
        _verify_recovery_bootblock_marker(
            static_bootblock_contract,
            expected_present=True,
        )
        _reload_and_verify_recovery_dropins(
            static_bootblock_contract.units,
            expected_present=True,
            transaction_id=static_bootblock_contract.transaction_id,
        )
        _assert_strict_update_writer_quiescence(
            repo_dir=repo_dir,
            transaction_id=static_bootblock_contract.transaction_id,
        )
        if static_bootblock_contract.transaction_id != package_receipt.transaction_id:
            raise RuntimeError("Statischer Bootblock widerspricht dem Paket-Receipt")
    managed_lease_contract = update_safety_contract or static_bootblock_contract
    bound_target_files = {
        relative_path: _bind_target_file_to_commit(
            repo_dir=repo_dir,
            target_commit=target_commit,
            relative_path=relative_path,
            install_user=install_user,
            **_root_git_call_kwargs(explicit_download_bootstrap),
        )
        for relative_path in TARGET_FINALIZER_RELATIVE_FILES
    }
    snapshot_entries = _target_execution_archive_entries(
        repo_dir=repo_dir,
        target_commit=target_commit,
        install_user=install_user,
        **_root_git_call_kwargs(explicit_download_bootstrap),
    )

    for relative_path, expected_identity in bound_target_files.items():
        current = os.lstat(os.path.join(repo_dir, relative_path))
        if _file_identity(current) != expected_identity:
            raise RuntimeError(f"Target-Modul wurde nach der Commit-Bindung ausgetauscht: {relative_path}")

    config_state = "missing" if state.bootstrap_legacy_config else "present"
    venv_state, venv_path = _finalizer_venv_contract(
        package_receipt.package_transaction
    )

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
        "--expected-package-receipt-sha256", package_receipt.receipt_sha256,
        "--expected-package-receipt-device", str(package_receipt.receipt_dev),
        "--expected-package-receipt-inode", str(package_receipt.receipt_ino),
        "--expected-package-full-backup-id", package_receipt.full_backup_id,
        "--expected-recovery-journal-sha256",
        recovery_journal_contract.journal_sha256,
        "--expected-recovery-journal-device",
        str(recovery_journal_contract.journal_device),
        "--expected-recovery-journal-inode",
        str(recovery_journal_contract.journal_inode),
        "--expected-recovery-journal-phase",
        recovery_journal_contract.payload.phase,
        "--expected-apache-available", "1" if apache_preimage.apache_available else "0",
        "--expected-apache-active", "1" if apache_preimage.apache_was_active else "0",
        "--expected-apache-unit-file-state", apache_preimage.apache_unit_file_state,
    ]
    if explicit_download_bootstrap:
        finalizer_args.append("--explicit-download-bootstrap")
    if static_bootblock_contract is not None:
        finalizer_args.extend((
            "--static-recovery-contract-json",
            _serialize_recovery_bootblock_contract(static_bootblock_contract),
        ))
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
    durable_recovery_committed_observed: (
        recovery_journal.RecoveryJournalContract | None
    ) = None
    durable_committed_observed: UpdateSafetyContract | None = None
    durable_package_committed_observed: PreparedPackageReceipt | None = None
    try:
        _verify_target_execution_snapshot(
            snapshot_root,
            snapshot_entries,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )
        if managed_lease_contract is None:
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
                f"--unit={managed_lease_contract.finalizer_unit}",
                "--service-type=exec",
                "--property=ExitType=main",
                "--property=KillMode=control-group",
                "--property=Restart=no",
                "--property=User=root",
                "--property=Group=root",
                "--property=DynamicUser=no",
                "--property=WorkingDirectory=/",
                "--property=UMask=0077",
                f"--property=RuntimeDirectory={managed_lease_contract.runtime_directory}",
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
                "--update-safety-transaction", managed_lease_contract.transaction_id,
                "--update-safety-service-unit", managed_lease_contract.finalizer_unit,
                "--update-safety-runtime-directory", managed_lease_contract.runtime_directory,
                "--update-safety-token-path", managed_lease_contract.token_path,
                "--expected-lock-device", str(lock_metadata.st_dev),
                "--expected-lock-inode", str(lock_metadata.st_ino),
                "--expected-recovery-journal-sha256",
                recovery_journal_contract.journal_sha256,
                "--expected-recovery-journal-device",
                str(recovery_journal_contract.journal_device),
                "--expected-recovery-journal-inode",
                str(recovery_journal_contract.journal_inode),
                "--expected-recovery-journal-phase",
                recovery_journal_contract.payload.phase,
            ]
            if update_safety_contract is not None:
                command.extend((
                    "--update-safety-receipt-sha256",
                    update_safety_contract.receipt_sha256,
                ))
            else:
                command.extend((
                    "--static-recovery-contract-json",
                    _serialize_recovery_bootblock_contract(
                        static_bootblock_contract
                    ),
                ))
            command.extend(("--", *finalizer_args))
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
            _wait_managed_finalizer_inactive(managed_lease_contract)
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
        durable_recovery_committed_observed = (
            _read_matching_committed_recovery_journal(
                recovery_journal_contract
            )
        )
        if durable_recovery_committed_observed is None:
            current_recovery_journal = recovery_journal.read_recovery_journal(
                allow_missing=True
            )
            if current_recovery_journal == recovery_journal_contract:
                raise RuntimeError(
                    "Finalizer-Erfolg besitzt kein durable committed "
                    "Master-Journal"
                )
            raise UpdateSafetyManagedServiceUnquiescedError(
                "Master-Journal driftete nach Finalizer-Erfolg; "
                "Altstand-Rollback bleibt fail-closed gesperrt"
            )
        committed_package_receipt = _read_prepared_package_receipt()
        if (
            committed_package_receipt is None
            or committed_package_receipt.state != "committed"
            or not _same_prepared_package_transaction_shape(
                committed_package_receipt,
                package_receipt,
            )
        ):
            raise RuntimeError(
                "Finalizer-Erfolg besitzt nicht den committed Ersatz seines "
                "Paket-Receipts"
            )
        durable_package_committed_observed = committed_package_receipt
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
            # Safety- und Paket-Receipt bleiben bis hinter Snapshot-, Offline-
            # und Overlay-Cleanup als durable Abschlussbeleg erhalten.
        elif static_bootblock_contract is not None:
            _verify_recovery_bootblock_marker(
                static_bootblock_contract,
                expected_present=False,
            )
            _reload_and_verify_recovery_dropins(
                static_bootblock_contract.units,
                expected_present=False,
                transaction_id=static_bootblock_contract.transaction_id,
            )
            # Das Paket-Receipt bleibt als statischer Abschlussbeleg bis zum
            # vollständig bestätigten äußeren Cleanup erhalten.
        else:
            # Auch der direkte Pfad entfernt seinen Paketbeleg erst außen.
            pass
    except BaseException as original_error:
        if durable_recovery_committed_observed is None:
            try:
                durable_recovery_committed_observed = (
                    _read_matching_committed_recovery_journal(
                        recovery_journal_contract,
                        allow_missing=True,
                    )
                )
                current_recovery_journal = (
                    recovery_journal.read_recovery_journal(allow_missing=True)
                )
            except Exception as journal_error:
                raise UpdateSafetyManagedServiceUnquiescedError(
                    "Master-Journal ist nach Finalizerfehler nicht sicher "
                    "lesbar; Altstand-Rollback bleibt fail-closed gesperrt"
                ) from journal_error
            if (
                durable_recovery_committed_observed is None
                and current_recovery_journal != recovery_journal_contract
            ):
                raise UpdateSafetyManagedServiceUnquiescedError(
                    "Master-Journal driftete nach Finalizerfehler; "
                    "Altstand-Rollback bleibt fail-closed gesperrt"
                ) from original_error
        if managed_lease_contract is not None and managed_service_spawn_attempted:
            if durable_package_committed_observed is None:
                try:
                    current_package_after_failure = _read_prepared_package_receipt(
                        allow_missing=True
                    )
                except Exception as package_receipt_error:
                    update_logger.critical(
                        "Paket-Receipt ist nach Finalizerfehler unlesbar: %s",
                        package_receipt_error,
                    )
                    current_package_after_failure = None
                if (
                    current_package_after_failure is not None
                    and current_package_after_failure.state == "committed"
                    and _same_prepared_package_transaction_shape(
                        current_package_after_failure,
                        package_receipt,
                    )
                ):
                    durable_package_committed_observed = (
                        current_package_after_failure
                    )
            if update_safety_contract is not None and durable_committed_observed is None:
                try:
                    current_after_failure = _read_update_safety_contract(
                        allow_missing=True
                    )
                except Exception as receipt_error:
                    update_logger.critical(
                        "Update-Sicherheitsreceipt ist nach Finalizerfehler unlesbar: %s",
                        receipt_error,
                    )
                    current_after_failure = None
                if (
                    current_after_failure is not None
                    and current_after_failure.state == "committed"
                    and _same_update_safety_transaction_shape(
                        current_after_failure,
                        update_safety_contract,
                    )
                ):
                    durable_committed_observed = current_after_failure
            try:
                _kill_managed_finalizer_and_quiesce(
                    managed_lease_contract,
                    repo_dir=repo_dir,
                    require_pending_contract=(
                        update_safety_contract is not None
                        and durable_committed_observed is None
                    ),
                )
                managed_service_quiesced = True
            except BaseException as quiesce_error:
                managed_service_quiesce_error = quiesce_error
                update_logger.critical(
                    "Verwaltete Finalizer-cgroup/Writer-Ruhe blieb unbewiesen: %s",
                    quiesce_error,
                )
            if (
                durable_recovery_committed_observed is None
                and (
                    durable_committed_observed is not None
                    or durable_package_committed_observed is not None
                )
            ):
                raise UpdateSafetyManagedServiceUnquiescedError(
                    "Safety- oder Paket-Receipt ist committed, obwohl das "
                    "autoritative Master-Journal nicht committed ist; "
                    "Recoverymutation bleibt fail-closed gesperrt"
                ) from original_error
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
            if durable_package_committed_observed is not None:
                if managed_service_quiesced:
                    try:
                        _finish_committed_package_gate_cleanup(
                            durable_package_committed_observed
                        )
                    except Exception as cleanup_error:
                        update_logger.critical(
                            "Committed statischer Apache-Abschluss blieb stehen: %s",
                            cleanup_error,
                        )
            if durable_recovery_committed_observed is not None:
                raise UpdateSafetyPostCommitError(
                    "Apache-/Receipt-Abschluss brach nach durable committed "
                    "Master-Journal ab. Lösung: Keine Receipt-, Overlay- oder "
                    "Backup-Datei manuell löschen; denselben Updatebefehl "
                    "erneut starten."
                ) from original_error
            if not managed_service_quiesced:
                raise UpdateSafetyManagedServiceUnquiescedError(
                    "Verwaltete Finalizer-cgroup/Writer-Ruhe blieb nach "
                    "Kindprozessfehler unbewiesen; Recoverymutation ist gesperrt: "
                    f"{managed_service_quiesce_error}"
                ) from original_error
        if durable_recovery_committed_observed is not None:
            raise UpdateSafetyPostCommitError(
                "Target-Finalizer brach nach durable committed Master-Journal "
                "ab. Lösung: Keine Receipt-, Overlay- oder Backup-Datei "
                "manuell löschen; denselben Updatebefehl erneut starten."
            ) from original_error
        raise
    finally:
        if (
            managed_lease_contract is not None
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
                if (
                    durable_recovery_committed_observed is not None
                    or durable_committed_observed is not None
                    or durable_package_committed_observed is not None
                ):
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


def _commit_reader_user(
    install_user: str,
    *,
    root_authority: bool,
) -> str | None:
    """Wählt die Identität des kryptographischen Commit-Lesers eindeutig."""

    if not isinstance(root_authority, bool):
        raise ValueError("Commit-Leser-Autorität muss boolesch sein")
    if not root_authority:
        return str(install_user)
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError(
            "Bootstrap-Commit-Leser mit Root-Autorität verlangt Root"
        )
    return None


def _read_commit_blob(
    repo_dir: str,
    target_commit: str,
    relative_path: str,
    install_user: str,
    maximum: int = 1024 * 1024,
    *,
    root_authority: bool = False,
) -> bytes:
    commit = _validate_full_commit(target_commit)
    if relative_path.startswith("/") or ".." in Path(relative_path).parts:
        raise RuntimeError("Target-Blobpfad ist nicht relativ und kanonisch")
    entries = read_commit_entries(
        repo_dir,
        commit,
        (relative_path,),
        required_paths=(relative_path,),
        run_as_user=_commit_reader_user(
            install_user,
            root_authority=root_authority,
        ),
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
    *,
    root_authority: bool = False,
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
        run_as_user=_commit_reader_user(
            install_user,
            root_authority=root_authority,
        ),
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
    root_authority: bool = False,
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
            **_root_git_call_kwargs(root_authority),
        )
        expected_mode = _read_commit_file_mode(
            repo_dir,
            target_commit,
            relative_path,
            install_user,
            **_root_git_call_kwargs(root_authority),
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
    root_authority: bool = False,
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
        **_root_git_call_kwargs(root_authority),
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
    post_service_guard=None,
    remove_receipt: bool = True,
) -> bool:
    """Öffnet das Recovery-Gate signalgeschützt und rearmed jeden Fehler."""

    if post_service_guard is not None and not callable(post_service_guard):
        raise ValueError("Dynamischer Recovery-Endguard ist nicht aufrufbar")
    if not isinstance(remove_receipt, bool):
        raise ValueError("Dynamischer Receipt-Cleanup-Vertrag ist nicht boolesch")
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
        if post_service_guard is not None:
            post_service_guard()
        if remove_receipt:
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


def _complete_static_recovery_start(
    contract: RecoveryBootblockContract,
    *,
    recovery_transaction_id: str,
    state: TransitionState,
    post_service_guard=None,
) -> RecoveryTransitionResult:
    """Entfernt BindsTo-Gates vor dem Altstart und rearmed jeden Fehler."""

    if post_service_guard is not None and not callable(post_service_guard):
        raise ValueError("Statischer Recovery-Endguard ist nicht aufrufbar")
    if contract.transaction_id != recovery_transaction_id:
        raise RuntimeError("Statischer Recovery-Vertrag driftete zur Transaktion")
    _validate_recovery_bootblock_contract(contract)
    signal_guard = _TerminalSignalGuard()
    signal_guard.install()
    signal_guard.arm()
    original_error: BaseException | None = None
    latest_contract: RecoveryBootblockContract | RecoveryBootblockPartialContract | None = contract
    try:
        # BindsTo/After auf den beendeten transienten Finalizer darf beim
        # Altstart nicht mehr in der effektiven Unitansicht liegen. Der
        # erhaltene Inodevertrag kann die Startsperre bei jedem Folgefehler
        # exakt neu anlegen.
        _clear_recovery_bootblock_marker(contract)
        _remove_persistent_recovery_bootblock(contract)
        if not _recover_pretransaction_service_state(state):
            raise RuntimeError("Statischer Recovery-Altstart blieb unvollständig")
        if post_service_guard is not None:
            post_service_guard()
    except BaseException as exc:
        original_error = exc
        enforcement = _enforce_fail_closed_after_recovery_failure(
            contract,
            recovery_transaction_id=recovery_transaction_id,
        )
        latest_contract = enforcement.bootblock_contract
    requested_signum = signal_guard.requested_signum
    signal_guard.restore()
    if requested_signum is not None:
        raise _DeferredParentSignal(requested_signum)
    if original_error is None:
        return RecoveryTransitionResult(True, None)
    update_logger.error(
        "Statisches Recovery-Gate/Altstart schlug fehl: %s",
        original_error,
    )
    if not isinstance(original_error, Exception):
        raise original_error
    return RecoveryTransitionResult(False, latest_contract)


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
    quiesced_overlay_receipt: QuiescedOverlayReceipt | None = None,
    recovery_journal_contract: (
        recovery_journal.RecoveryJournalContract | None
    ) = None,
    persistent_recovery_transaction: (
        ReconstructedRecoveryTransaction | None
    ) = None,
) -> RecoveryTransitionResult:
    """Restore old Git/tree/persistent state and verify role/services after any mutation failure."""
    recovery_ok = True
    if (
        not isinstance(
            recovery_journal_contract,
            recovery_journal.RecoveryJournalContract,
        )
        or recovery_journal.verify_recovery_journal(
            recovery_journal_contract
        ).payload.phase
        != recovery_journal.PHASE_PRODUCT_MUTATING
    ):
        update_logger.error(
            "Vollständiger Recovery-Pfad besitzt kein product_mutating-Journal"
        )
        return RecoveryTransitionResult(False, None)
    if persistent_recovery_transaction is not None and (
        not isinstance(
            persistent_recovery_transaction,
            ReconstructedRecoveryTransaction,
        )
        or persistent_recovery_transaction.bundle.journal
        != recovery_journal_contract
    ):
        update_logger.error("Rekonstruierte Recovery-Transaktion driftete")
        return RecoveryTransitionResult(False, None)
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
            arm_kwargs = {"transaction_id": recovery_transaction_id}
            if bootblock_contract is None:
                arm_kwargs["recovery_journal_contract"] = recovery_journal_contract
            bootblock_contract = _arm_persistent_recovery_bootblock(
                bootblock_contract,
                **arm_kwargs,
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

    # Weder Git noch Produkt-/Webdateien dürfen vor dem vollständigen Beweis
    # beider Sicherungsstufen verändert werden. Apache wird erneut gestoppt,
    # weil ein Fehler auch nach seinem späten Health-Start eingetreten sein kann.
    try:
        if quiesced_overlay_receipt is None:
            raise RuntimeError("Quiesced-Overlay-Receipt fehlt")
        for stop_apache in (False, True):
            _revalidate_quiesced_overlay_receipt(
                quiesced_overlay_receipt,
                backup_dir=backup_dir,
                install_root=repo_dir,
                transaction_id=str(recovery_transaction_id),
            )
            if old_commit is not None:
                _revalidate_recovery_backup_payload_receipt(
                    backup_receipt,
                    backup_dir=backup_dir,
                    repo_dir=repo_dir,
                    transaction_id=str(recovery_transaction_id),
                )
            if stop_apache:
                break
            _quiesce_apache_for_cutover(recovery_inventory.apache_security)
    except Exception as exc:
        print(f"[ABBRUCH] E3DC-UPD-RECOVERY-EVIDENCE: {exc}")
        print(f"Vollbackup: {backup_dir}")
        if quiesced_overlay_receipt is not None:
            print(f"Zustands-Overlay: {quiesced_overlay_receipt.overlay_dir}")
        print(
            "Lösung: Dienste nicht manuell starten und keine Sicherung löschen; "
            "prüfe das vollständige Updatejournal und führe erst danach den dort "
            "genannten Recovery-Befehl aus."
        )
        update_logger.critical(
            "Recovery-Sicherungsbeweis scheiterte vor jeder Rückfallmutation: %s",
            exc,
        )
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
        else:
            def restore_manifest_guard(manifest):
                if (
                    not isinstance(manifest, dict)
                    or str(manifest.get("backup_id") or "")
                    != quiesced_overlay_receipt.full_backup_id
                    or str(manifest.get("install_root") or "") != repo_dir
                ):
                    raise RuntimeError(
                        "Vollrestore verwendet nicht das Overlay-gebundene Backupmanifest"
                    )

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
        restore_quiesced_overlay(
            quiesced_overlay_receipt.overlay_dir,
            install_path=repo_dir,
            guard=_overlay_restore_guard_from_receipt(
                quiesced_overlay_receipt
            ),
        )
        # apt-/pip-Rückläufe können über Paket-Maintainer-Skripte Apache,
        # Unitdateien oder Enablement erneut verändern. Sie müssen deshalb
        # zwingend vor dem abschließenden Restore der privilegierten
        # Recovery-Fläche laufen. Erst danach wird deren exaktes Preimage
        # projiziert und gebunden.
        if package_transaction is not None:
            _restore_package_transaction(package_transaction)
            package_transaction = None
        _restore_recovery_surface(
            recovery_inventory,
            state,
            restore_legacy_systemd_surface=(
                persistent_recovery_transaction is None
            ),
        )
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
                    recovery_payload = _render_recovery_bootblock_dropin(
                        bootblock_contract.transaction_id
                    )
                    recovery_path = _recovery_dropin_path(storage_unit)
                    recovery_dev, recovery_ino = recovery_identities[storage_unit]
                    expected_recovery_dropins = {
                        storage_unit: {
                            recovery_path: {
                                "bytes": recovery_payload,
                                "dev": recovery_dev,
                                "ino": recovery_ino,
                                "uid": 0,
                                "gid": 0,
                                "mode": 0o644,
                                "nlink": 1,
                                "size": len(recovery_payload),
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

    def restore_recovery_apache() -> None:
        _restore_apache_after_successful_cutover(
            expected_available=recovery_inventory.apache_security.apache_available,
            expected_active=recovery_inventory.apache_security.apache_was_active,
            expected_unit_file_state=(
                recovery_inventory.apache_security.apache_unit_file_state
            ),
        )

    if persistent_recovery_transaction is not None:
        try:
            rolled_back = _restore_persistent_systemd_prestate(
                persistent_recovery_transaction
            )
            restore_recovery_apache()
        except Exception as exc:
            update_logger.error(
                "Persistenter systemd-/Apache-Altstart blieb unvollständig: %s",
                exc,
            )
            return RecoveryTransitionResult(False, bootblock_contract)
        return RecoveryTransitionResult(True, None, rolled_back)

    if dynamic_safety:
        if not _complete_dynamic_recovery_start(
            update_safety_contract,
            repo_dir=repo_dir,
            state=state,
            post_service_guard=restore_recovery_apache,
            remove_receipt=False,
        ):
            return RecoveryTransitionResult(False, None)
        rolled_back = _advance_persistent_recovery_rolled_back(
            recovery_journal_contract
        )
        return RecoveryTransitionResult(True, None, rolled_back)
    static_result = _complete_static_recovery_start(
        bootblock_contract,
        recovery_transaction_id=str(recovery_transaction_id),
        state=state,
        post_service_guard=restore_recovery_apache,
    )
    if not static_result.recovered:
        return static_result
    rolled_back = _advance_persistent_recovery_rolled_back(
        recovery_journal_contract
    )
    return RecoveryTransitionResult(True, None, rolled_back)


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


def _abort_before_product_mutation(
    *,
    repo_dir: str,
    state: TransitionState,
    apache_preimage: ApacheSecurityPreimage,
    transaction_id: str,
    bootblock_contract: (
        RecoveryBootblockContract | RecoveryBootblockPartialContract | None
    ),
    update_safety_contract: UpdateSafetyContract | None,
    overlay_receipt: QuiescedOverlayReceipt | None,
    package_transaction: PackageTransactionState | None = None,
    packages_mutated: bool = False,
    recovery_inventory: RecoverySurfaceInventory | None = None,
    recovery_journal_contract: (
        recovery_journal.RecoveryJournalContract | None
    ) = None,
    persistent_recovery_transaction: (
        ReconstructedRecoveryTransaction | None
    ) = None,
) -> bool:
    """Öffnet nach reinem Cutoverfehler den unveränderten Altstand kontrolliert."""

    try:
        if (
            not isinstance(
                recovery_journal_contract,
                recovery_journal.RecoveryJournalContract,
            )
            or recovery_journal.verify_recovery_journal(
                recovery_journal_contract
            ).payload.phase
            != recovery_journal.PHASE_PREPRODUCT
        ):
            raise RuntimeError(
                "Vorprodukt-Rücklauf besitzt kein preproduct-Master-Journal"
            )
        if persistent_recovery_transaction is not None and (
            not isinstance(
                persistent_recovery_transaction,
                ReconstructedRecoveryTransaction,
            )
            or persistent_recovery_transaction.bundle.journal
            != recovery_journal_contract
        ):
            raise RuntimeError("Rekonstruierte Vorprodukt-Transaktion driftete")
        if persistent_recovery_transaction is not None:
            if not _stop_v4_services(V4_SERVICES):
                raise RuntimeError(
                    "Aktor-/Writer-Ruhe vor persistentem Vorprodukt-Rücklauf fehlt"
                )
            _quiesce_apache_for_cutover(apache_preimage)
        if packages_mutated:
            if package_transaction is None:
                raise RuntimeError("Paketrücklauf besitzt kein gebundenes Preimage")
            _restore_package_transaction(package_transaction)
        # Paket-Maintainer-Skripte dürfen Apache verändert oder gestartet
        # haben. Zuerst wird sein Dateipreimage bei inaktivem Dienst
        # restauriert; extern geöffnet wird er erst nach gesunden Altdiensten.
        if recovery_inventory is not None:
            if recovery_inventory.apache_security != apache_preimage:
                raise RuntimeError("Apache-Preimage driftete im Recovery-Inventar")
            _restore_recovery_surface(
                recovery_inventory,
                state,
                restore_legacy_systemd_surface=(
                    persistent_recovery_transaction is None
                ),
            )
        else:
            _restore_apache_security_preimage(
                apache_preimage,
                restore_activity=False,
            )

        def restore_bound_apache() -> None:
            _restore_apache_after_successful_cutover(
                expected_available=apache_preimage.apache_available,
                expected_active=apache_preimage.apache_was_active,
                expected_unit_file_state=apache_preimage.apache_unit_file_state,
            )

        if persistent_recovery_transaction is not None:
            _restore_persistent_systemd_prestate(
                persistent_recovery_transaction
            )
            restore_bound_apache()
        elif update_safety_contract is not None:
            if not _complete_dynamic_recovery_start(
                update_safety_contract,
                repo_dir=repo_dir,
                state=state,
                post_service_guard=restore_bound_apache,
                remove_receipt=False,
            ):
                raise RuntimeError("Dynamischer Altstart blieb unvollständig")
        elif isinstance(bootblock_contract, RecoveryBootblockContract):
            static_result = _complete_static_recovery_start(
                bootblock_contract,
                recovery_transaction_id=transaction_id,
                state=state,
                post_service_guard=restore_bound_apache,
            )
            bootblock_contract = static_result.bootblock_contract
            if not static_result.recovered:
                raise RuntimeError("Statischer Altstart blieb unvollständig")
        else:
            if not _recover_pretransaction_service_state(state):
                raise RuntimeError("Altstart ohne Bootblock blieb unvollständig")
            restore_bound_apache()

        if overlay_receipt is not None:
            raise RuntimeError(
                "preproduct-Rücklauf darf kein Produkt-Overlay besitzen"
            )
        if persistent_recovery_transaction is None:
            _advance_persistent_recovery_rolled_back(
                recovery_journal_contract
            )
        return True
    except Exception as exc:
        print(f"[ABBRUCH] E3DC-UPD-PREMUTATION-RECOVERY: {exc}")
        print(
            "Lösung: Produktdateien wurden noch nicht verändert. Dienste und "
            "Sicherungsdateien nicht manuell löschen; prüfe das Updatejournal "
            "und die dort genannten systemctl-Statusbefehle."
        )
        update_logger.critical(
            "Altzustand konnte nach Cutoverfehler nicht vollständig geöffnet werden: %s",
            exc,
        )
        terminal_rollback = None
        if isinstance(
            recovery_journal_contract,
            recovery_journal.RecoveryJournalContract,
        ):
            try:
                terminal_rollback = _read_matching_rolled_back_recovery_journal(
                    recovery_journal_contract,
                    allow_missing=True,
                )
            except Exception as journal_error:
                update_logger.critical(
                    "Rollback-Journal ist nach Altstartfehler unlesbar: %s",
                    journal_error,
                )
        if terminal_rollback is not None:
            print(
                "Lösung: Der Altstand ist dauerhaft gewählt. Starte denselben "
                "Updatebefehl erneut; es werden nur Gate-Cleanup, Altstart und "
                "Apache-Abschluss fortgesetzt."
            )
        elif update_safety_contract is not None:
            try:
                _enforce_update_safety_fail_closed(
                    update_safety_contract,
                    repo_dir=repo_dir,
                )
            except Exception as enforcement_exc:
                update_logger.critical(
                    "Dynamischer Bootblock blieb zusätzlich unbewiesen: %s",
                    enforcement_exc,
                )
        else:
            _enforce_fail_closed_after_recovery_failure(
                bootblock_contract,
                recovery_transaction_id=transaction_id,
                recovery_journal_contract=recovery_journal_contract,
            )
        return False


def _enforce_fail_closed_after_recovery_failure(
    bootblock_contract: (
        RecoveryBootblockContract | RecoveryBootblockPartialContract | None
    ) = None,
    *,
    recovery_transaction_id: str | None = None,
    recovery_journal_contract: (
        recovery_journal.RecoveryJournalContract | None
    ) = None,
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
        arm_kwargs = {"transaction_id": recovery_transaction_id}
        if bootblock_contract is None:
            arm_kwargs["recovery_journal_contract"] = recovery_journal_contract
        latest_contract = _arm_persistent_recovery_bootblock(
            bootblock_contract,
            **arm_kwargs,
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
    *,
    root_authority: bool = False,
) -> str:
    """Bindet HEAD ausschließlich als vollständigen Commit-Hash."""

    result = _git_argv(
        repo_dir,
        install_user,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        timeout=10,
        **_root_git_call_kwargs(root_authority),
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
    root_authority: bool = False,
) -> list[tuple[str, int, str]]:
    """Bindet Pfad, Modus und Blob-ID aus einem exakten Produkt-Commit."""

    commit = (
        _validate_full_commit(target_commit)
        if target_commit is not None
        else _bound_release_head_commit(
            repo_dir,
            install_user,
            **_root_git_call_kwargs(root_authority),
        )
    )

    verified_entries = read_commit_entries(
        os.path.abspath(repo_dir),
        commit,
        (),
        include_all=True,
        run_as_user=_commit_reader_user(
            install_user,
            root_authority=root_authority,
        ),
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
    *,
    root_authority: bool = False,
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
        **_root_git_call_kwargs(root_authority),
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


def _download_bootstrap_entry_mode(target_install_path: str | None) -> str:
    """Bindet den internen Dispatcher-/Rettungsmodus ohne öffentliche CLI."""

    raw = str(os.environ.get("E3DC_BOOTSTRAP_ENTRY_MODE") or "rescue").strip()
    if raw not in {"regular", "rescue"}:
        raise RuntimeError("Update-Einstiegsmodus muss regular oder rescue sein")
    if raw == "regular":
        target = os.path.abspath(str(target_install_path or ""))
        runner = os.path.abspath(os.path.dirname(INSTALLER_DIR))
        if (
            not target_install_path
            or not hasattr(os, "geteuid")
            or os.geteuid() != 0
            or os.path.realpath(os.environ.get("E3DC_BOOTSTRAP_ROOT", ""))
            != os.path.realpath(target)
            or os.path.realpath(os.environ.get("E3DC_BOOTSTRAP_RUNNER_ROOT", ""))
            != os.path.realpath(runner)
            or os.path.realpath(runner) == os.path.realpath(target)
        ):
            raise RuntimeError(
                "Regular-Modus besitzt keinen getrennten root-eigenen Zielcode-Vertrag"
            )
    return raw


def _probe_regular_download_bootstrap_current(
    *,
    repo_dir: str,
    target_commit: str,
    target_tag: str,
    expected_role: str,
) -> tuple[bool, tuple[str, ...]]:
    """Prüft mit Zielcode rein lesend, ob der Release vollständig aktuell ist."""

    _assert_recovery_namespace_read_only_clear()
    root = _validate_bootstrap_install_path(repo_dir)
    commit = _validate_full_commit(target_commit)
    tag = _normalize_release_tag(target_tag)
    role = str(expected_role or "").strip().lower()
    if role not in VALID_HA_ROLES:
        raise RuntimeError("Regular-Probe besitzt keine gültige Rollenbindung")
    install_user = get_install_user()
    current_commit, _rebuild = _bind_bootstrap_git_prestate(
        root,
        explicit_bootstrap=False,
    )
    if not current_commit or not _exact_commit_matches(current_commit, commit):
        return False, (
            f"HEAD {current_commit or 'unlesbar'} weicht vom Zielcommit {commit} ab",
        )
    _require_bound_origin(root, install_user)
    state = _capture_transition_state(
        expected_role=role,
        allow_missing_config=False,
    )

    # Policy, Tag und Commit werden ausschließlich aus dem bereits
    # commitgebunden geladenen Ziel-Checkout interpretiert. Der Altbaum liefert
    # weder Updatebytes noch einen zielversionsspezifischen Dienstvertrag.
    runner_repo = os.path.abspath(os.path.dirname(INSTALLER_DIR))
    policy = _read_policy_from_commit(
        runner_repo,
        commit,
        install_user,
        root_authority=True,
    )
    bound_tag = _validate_target_release(
        policy,
        runner_repo,
        commit,
        tag,
        install_user,
        root_authority=True,
    )
    if bound_tag != tag:
        raise RuntimeError("Regular-Probe verlor ihre Tag-/Commitbindung")
    restart_services = _validated_restart_services(policy, state)
    errors = _same_release_integrity_errors(root, install_user)
    if len(errors) < 12:
        errors.extend(
            _same_release_service_errors(
                restart_services,
                state,
                maximum=12 - len(errors),
            )
        )
    if len(errors) < 12:
        try:
            apache_available, apache_active, apache_unit_state = (
                _capture_apache_service_prestate(settle_timeout_s=0)
            )
        except Exception as exc:
            errors.append(f"Apache-Endzustand ist nicht eindeutig lesbar: {exc}")
        else:
            if (apache_available, apache_active, apache_unit_state) != (
                True,
                True,
                "enabled",
            ):
                errors.append(
                    "apache2.service besitzt keinen kanonischen Endzustand "
                    f"(loaded={apache_available}, active={apache_active}, "
                    f"enabled={apache_unit_state})"
                )
    if len(errors) < 12:
        errors.extend(_local_http_healthcheck()[: 12 - len(errors)])
    return not errors, tuple(errors)


def _logical_commit_tree_space_bytes(
    entries: dict[str, tuple[bytes, int]],
) -> int:
    """Schätzt einen später geschriebenen Commitbaum mit Backup-Blockaufschlag."""

    if not isinstance(entries, dict) or not entries:
        raise RuntimeError("Commitgebundene Speicherfläche ist leer")
    payload_bytes = 0
    directories = {"."}
    for relative_path, entry in entries.items():
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or relative_path.startswith("/")
            or "\\" in relative_path
            or any(char in relative_path for char in "\x00\r\n\t")
        ):
            raise RuntimeError("Commitgebundene Speicherfläche enthält einen ungültigen Pfad")
        parts = relative_path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise RuntimeError("Commitgebundene Speicherfläche verlässt ihren Zielbaum")
        try:
            payload, mode = entry
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Commitgebundener Speichereintrag ist ungültig") from exc
        if not isinstance(payload, bytes) or mode not in {0o444, 0o555}:
            raise RuntimeError("Commitgebundener Speichereintrag besitzt keinen Blobvertrag")
        payload_bytes += len(payload)
        for depth in range(1, len(parts)):
            directories.add("/".join(parts[:depth]))
    metadata_bytes = (
        BACKUP_ESTIMATE_FIXED_OVERHEAD_BYTES
        + BACKUP_ESTIMATE_SOURCE_OVERHEAD_BYTES
        + len(entries) * BACKUP_ESTIMATE_FILE_OVERHEAD_BYTES
        + len(directories) * BACKUP_ESTIMATE_DIRECTORY_OVERHEAD_BYTES
    )
    return payload_bytes + metadata_bytes


def _estimate_target_release_space(
    *,
    source_repo: str,
    target_commit: str,
    install_user: str,
    root_authority: bool,
) -> TargetReleaseSpaceEstimate:
    """Bindet Produkt-, Web- und Finalizerbedarf an genau den Zielcommit."""

    entries = read_commit_entries(
        os.path.abspath(source_repo),
        _validate_full_commit(target_commit),
        (),
        include_all=True,
        run_as_user=_commit_reader_user(
            install_user,
            root_authority=root_authority,
        ),
        maximum_files=TARGET_EXECUTION_SNAPSHOT_MAX_FILES,
        maximum_file_bytes=TARGET_EXECUTION_SNAPSHOT_MAX_FILE_BYTES,
        maximum_total_bytes=TARGET_EXECUTION_SNAPSHOT_MAX_TOTAL_BYTES,
    )
    product_tree_bytes = _logical_commit_tree_space_bytes(entries)

    web_entries: dict[str, tuple[bytes, int]] = {}
    excluded_web_roots = {"data", "logs", "ramdisk", "tmp"}
    for relative_path, entry in entries.items():
        if relative_path in {"VERSION", "CHANGELOG.md", "UPDATE_POLICY.json"}:
            web_entries[relative_path] = entry
            continue
        if not relative_path.startswith("html/"):
            continue
        projected = relative_path[len("html/"):]
        if not projected or projected.split("/", 1)[0] in excluded_web_roots:
            continue
        if projected in web_entries:
            raise RuntimeError("Webprojektion besitzt ein doppeltes Ziel")
        web_entries[projected] = entry
    required_web = {"index.php", "helpers.php", "VERSION", "UPDATE_POLICY.json"}
    if not required_web.issubset(web_entries):
        raise RuntimeError("Zielcommit besitzt keine vollständige Webprojektion")

    snapshot_entries = {
        relative_path: entry
        for relative_path, entry in entries.items()
        if relative_path in TARGET_EXECUTION_SNAPSHOT_ROOT_FILES
        or relative_path == "Installer"
        or relative_path.startswith("Installer/")
    }
    required_snapshot = set(TARGET_EXECUTION_SNAPSHOT_ROOT_FILES) | set(
        TARGET_FINALIZER_RELATIVE_FILES
    )
    if not required_snapshot.issubset(snapshot_entries):
        raise RuntimeError("Zielcommit besitzt keinen vollständigen Finalizer-Snapshot")
    return TargetReleaseSpaceEstimate(
        product_tree_bytes=product_tree_bytes,
        web_projection_bytes=_logical_commit_tree_space_bytes(web_entries),
        finalizer_snapshot_bytes=_logical_commit_tree_space_bytes(snapshot_entries),
    )


def _disk_space_allocation_anchor(path: str) -> tuple[str, int]:
    """Bindet einen Zielpfad an den nächsten vorhandenen no-symlink Datenträger."""

    raw = str(path or "")
    candidate = os.path.abspath(raw)
    if (
        not raw
        or candidate != raw
        or not os.path.isabs(candidate)
        or any(char in candidate for char in "\x00\r\n\t")
    ):
        raise OfflinePreflightError(
            "E3DC-UPD-DISK-001",
            "Ein Zielpfad der Speicherplatzprüfung ist nicht kanonisch.",
            "Prüfe Installations-, Backup- und Webpfad. Korrigiere den genannten "
            "Pfad und starte denselben Updatebefehl erneut; es wurden noch keine "
            "Dienste gestoppt.",
        )
    while not os.path.lexists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    try:
        metadata = os.stat(candidate, follow_symlinks=False)
    except OSError as exc:
        raise OfflinePreflightError(
            "E3DC-UPD-DISK-001",
            f"Der Datenträger für {raw} ist nicht sicher lesbar.",
            f"Prüfe Einhängung und Pfad mit: findmnt -T {shlex.quote(candidate)}. "
            "Korrigiere Pfad oder Mount und starte denselben Updatebefehl erneut; "
            "es wurden noch keine Dienste gestoppt.",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or os.path.realpath(candidate) != candidate:
        raise OfflinePreflightError(
            "E3DC-UPD-DISK-001",
            f"Der Datenträgerpfad {candidate} enthält eine Symlink-Umleitung.",
            f"Prüfe den echten Zielpfad mit: findmnt -T {shlex.quote(candidate)}. "
            "Verwende einen kanonischen Installations-, Backup- oder Webpfad und "
            "starte danach denselben Updatebefehl erneut; es wurden noch keine "
            "Dienste gestoppt.",
        )
    return candidate, int(metadata.st_dev)


def _add_update_filesystem_demand(
    groups: dict[int, UpdateFilesystemDemand],
    *,
    path: str,
    label: str,
    payload_bytes: int = 0,
    backup_bytes: int = 0,
    working_bytes: int = 0,
) -> None:
    """Addiert logische Flächen, ohne Reserve je Pfad mehrfach anzusetzen."""

    values = (payload_bytes, backup_bytes, working_bytes)
    if (
        not isinstance(label, str)
        or not label.strip()
        or any(char in label for char in "\x00\r\n\t")
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values)
    ):
        raise ValueError("Dateisystembedarf ist ungültig")
    anchor, device = _disk_space_allocation_anchor(path)
    group = groups.get(device)
    if group is None:
        group = UpdateFilesystemDemand(
            device=device,
            representative_path=anchor,
            labels=[],
        )
        groups[device] = group
    elif group.total_bytes == 0 and any(values):
        # Ein Nullbeitrag wie der bereits belegte Cache soll nicht zum weniger
        # hilfreichen Repräsentanten einer späteren echten Anforderung werden.
        group.representative_path = anchor
    normalized_label = label.strip()
    if normalized_label not in group.labels:
        group.labels.append(normalized_label)
    group.payload_bytes += payload_bytes
    group.backup_bytes += backup_bytes
    group.working_bytes += working_bytes


def _require_grouped_update_free_space(
    contributions: tuple[tuple[str, str, int, int, int], ...],
    *,
    phase_label: str,
) -> tuple:
    """Prüft jede reale st_dev-Gruppe genau einmal mit einer Reserve."""

    groups: dict[int, UpdateFilesystemDemand] = {}
    for path, label, payload, backup, working in contributions:
        _add_update_filesystem_demand(
            groups,
            path=path,
            label=label,
            payload_bytes=payload,
            backup_bytes=backup,
            working_bytes=working,
        )
    receipts = []
    for device in sorted(groups):
        group = groups[device]
        if group.total_bytes == 0:
            # Bereits materialisierte Cachebytes sind in f_bavail enthalten.
            # Ein dediziertes Cache-Dateisystem erhält deshalb keine zweite,
            # künstliche Nutzlast oder 512-MiB-Reserve.
            continue
        labels = ", ".join(group.labels)
        try:
            receipt = require_conservative_free_space(
                group.representative_path,
                payload_bytes=group.payload_bytes,
                backup_bytes=group.backup_bytes,
                working_bytes=group.working_bytes,
            )
        except OfflinePreflightError as exc:
            extra = (
                f" Betroffene Bereiche: {labels}. Prüfe die Einhängung mit: "
                f"findmnt -T {shlex.quote(group.representative_path)}."
            )
            if "Backup" in labels:
                extra += (
                    " Alte Sicherungen ausschließlich über Installer-Menü 13 "
                    "und „Backup-Limit anwenden“ bereinigen; Backupordner nicht "
                    "manuell löschen."
                )
            raise OfflinePreflightError(
                exc.code,
                f"{phase_label}: {exc.message} Betroffene Bereiche: {labels}.",
                exc.solution + extra,
            ) from exc
        receipts.append(receipt)
    return tuple(receipts)


def _require_update_transaction_space(
    *,
    repo_dir: str,
    backup_collection: str,
    offline_receipt: OfflinePackageReceipt,
    target_space: TargetReleaseSpaceEstimate,
    full_backup_bytes: int,
    overlay_bytes: int,
    bootstrap_without_git: bool,
    phase_label: str,
) -> tuple:
    """Baut die noch ausstehenden Flächen für ein Vor-Stopp-Gate auf."""

    if not isinstance(offline_receipt, OfflinePackageReceipt):
        raise RuntimeError("Offline-Paketreceipt fehlt bei der Speicherplatzprüfung")
    verify_preparation(offline_receipt)
    if offline_receipt.pip_packages:
        if offline_receipt.wheel_mirror is None:
            raise RuntimeError("Wheel-Mirror fehlt bei der Speicherplatzprüfung")
        mirror_path = offline_receipt.wheel_mirror.root
    else:
        mirror_path = None
    if (
        not isinstance(target_space, TargetReleaseSpaceEstimate)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (full_backup_bytes, overlay_bytes)
        )
    ):
        raise RuntimeError("Speicherplatzvertrag ist unvollständig")
    git_rebuild_bytes = (
        target_space.product_tree_bytes if bootstrap_without_git else 0
    )
    contributions = [
        (
            repo_dir,
            "Produktbaum und Finalizer-Snapshot",
            target_space.product_tree_bytes,
            0,
            target_space.finalizer_snapshot_bytes + git_rebuild_bytes,
        ),
        (
            backup_collection,
            "Backup und quiesziertes Zustands-Overlay",
            0,
            full_backup_bytes + overlay_bytes,
            0,
        ),
        (
            "/var/www/html",
            "Webprojektion",
            target_space.web_projection_bytes,
            0,
            0,
        ),
        (
            offline_receipt.cache.root,
            "Offline-Cache (bereits belegt)",
            0,
            0,
            0,
        ),
    ]
    if mirror_path is not None:
        contributions.append(
            (
                mirror_path,
                "Wheel-Mirror (bereits belegt)",
                0,
                0,
                0,
            )
        )
    return _require_grouped_update_free_space(
        tuple(contributions),
        phase_label=phase_label,
    )


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


def _quiesced_overlay_path(
    backup_dir: str,
    recovery_transaction_id: str,
) -> str:
    """Leitet den rebootfest auffindbaren Delta-Sicherungspfad streng ab."""

    transaction_id = str(recovery_transaction_id or "").strip().lower()
    if not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(transaction_id):
        raise RuntimeError("Quiesced-Overlay besitzt keine gültige Transaktions-ID")
    backup = os.path.abspath(str(backup_dir or ""))
    if not backup or backup != str(backup_dir):
        raise RuntimeError("Quiesced-Overlay besitzt keinen kanonischen Backuppfad")
    parent = os.path.dirname(backup)
    name = os.path.basename(backup)
    if not name or name in {".", ".."}:
        raise RuntimeError("Quiesced-Overlay besitzt keinen gültigen Backupnamen")
    target = os.path.join(parent, f".{name}.quiesced-{transaction_id}")
    if os.path.dirname(target) != parent:
        raise RuntimeError("Quiesced-Overlay verlässt den Backup-Root")
    return target


def _read_stable_verified_manifest(
    directory: str,
    *,
    expected_kind: str,
) -> tuple[dict, str]:
    """Bindet die tatsächlich verifizierten Manifestbytes gegen Austausch."""

    root = os.path.abspath(str(directory or ""))
    if not root or root != str(directory) or not os.path.isabs(root):
        raise RuntimeError("Backup-/Overlaypfad ist nicht kanonisch")
    manifest_path = os.path.join(root, MANIFEST_NAME)
    digest_before, size_before = _regular_file_sha256(manifest_path)
    manifest = verify_backup(root, expected_kind=expected_kind)
    digest_after, size_after = _regular_file_sha256(manifest_path)
    if digest_before != digest_after or size_before != size_after:
        raise RuntimeError("Backup-/Overlaymanifest driftete während der Prüfung")
    return manifest, digest_after


def _parse_quiesced_overlay_receipt(
    payload: bytes,
    metadata: os.stat_result,
) -> QuiescedOverlayReceipt:
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Quiesced-Overlay-Receipt ist nicht lesbar") from exc
    if not isinstance(record, dict) or set(record) != {
        "schema",
        "state",
        "transaction_id",
        "install_root",
        "overlay",
        "parent",
        "full_backup",
    }:
        raise RuntimeError("Quiesced-Overlay-Receipt besitzt ein unbekanntes Schema")
    overlay = record.get("overlay")
    parent = record.get("parent")
    full = record.get("full_backup")
    if (
        record.get("schema") != QUIESCED_OVERLAY_RECEIPT_SCHEMA
        or record.get("state") != "pending"
        or not isinstance(overlay, dict)
        or set(overlay) != {"dir", "dev", "ino", "id", "manifest_sha256"}
        or not isinstance(parent, dict)
        or set(parent) != {"dev", "ino"}
        or not isinstance(full, dict)
        or set(full) != {"dir", "dev", "ino", "id", "manifest_sha256"}
    ):
        raise RuntimeError("Quiesced-Overlay-Receipt ist nicht eng typisiert")
    transaction_id = str(record.get("transaction_id") or "")
    install_root = str(record.get("install_root") or "")
    overlay_dir = str(overlay.get("dir") or "")
    full_backup_dir = str(full.get("dir") or "")
    hashes = (
        str(overlay.get("id") or ""),
        str(overlay.get("manifest_sha256") or ""),
        str(full.get("id") or ""),
        str(full.get("manifest_sha256") or ""),
    )
    if (
        not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(transaction_id)
        or not os.path.isabs(install_root)
        or os.path.abspath(install_root) != install_root
        or not os.path.isabs(overlay_dir)
        or os.path.abspath(overlay_dir) != overlay_dir
        or not os.path.isabs(full_backup_dir)
        or os.path.abspath(full_backup_dir) != full_backup_dir
        or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes)
    ):
        raise RuntimeError("Quiesced-Overlay-Receipt besitzt ungültige Bindungswerte")
    try:
        numeric = tuple(
            int(value)
            for value in (
                overlay.get("dev"),
                overlay.get("ino"),
                parent.get("dev"),
                parent.get("ino"),
                full.get("dev"),
                full.get("ino"),
            )
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Quiesced-Overlay-Receipt besitzt ungültige Inodes") from exc
    if any(value <= 0 for value in numeric):
        raise RuntimeError("Quiesced-Overlay-Receipt besitzt ungültige Inodes")
    canonical = _canonical_quiesced_overlay_receipt_bytes(record)
    if canonical != payload:
        raise RuntimeError("Quiesced-Overlay-Receipt ist nicht kanonisch kodiert")
    return QuiescedOverlayReceipt(
        transaction_id=transaction_id,
        overlay_dir=overlay_dir,
        overlay_dev=numeric[0],
        overlay_ino=numeric[1],
        parent_dev=numeric[2],
        parent_ino=numeric[3],
        backup_id=hashes[0],
        manifest_sha256=hashes[1],
        install_root=install_root,
        full_backup_dir=full_backup_dir,
        full_backup_dev=numeric[4],
        full_backup_ino=numeric[5],
        full_backup_id=hashes[2],
        full_backup_manifest_sha256=hashes[3],
        receipt_dev=int(metadata.st_dev),
        receipt_ino=int(metadata.st_ino),
        receipt_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _read_quiesced_overlay_receipt(
    *,
    allow_missing: bool = False,
) -> QuiescedOverlayReceipt | None:
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        readback = _read_bound_root_file_at(
            state_descriptor,
            QUIESCED_OVERLAY_RECEIPT_NAME,
            maximum=64 * 1024,
            mode=0o600,
            allow_missing=allow_missing,
        )
        if readback is None:
            return None
        return _parse_quiesced_overlay_receipt(*readback)
    finally:
        os.close(state_descriptor)


def _capture_quiesced_overlay_receipt(
    *,
    overlay_dir: str,
    backup_dir: str,
    install_root: str,
    transaction_id: str,
    restore_guard: QuiescedOverlayRestoreGuard,
) -> QuiescedOverlayReceipt:
    """Friert Overlay, Vollbackup und beide Inodes vor jeder Produktmutation ein."""

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("Quiesced-Overlay-Receipt darf ausschließlich Root erzeugen")
    transaction_id = str(transaction_id or "").strip().lower()
    root = os.path.abspath(str(install_root or ""))
    backup = os.path.abspath(str(backup_dir or ""))
    overlay = os.path.abspath(str(overlay_dir or ""))
    if (
        not RECOVERY_BOOTBLOCK_TRANSACTION_RE.fullmatch(transaction_id)
        or root != str(install_root)
        or backup != str(backup_dir)
        or overlay != str(overlay_dir)
        or overlay != _quiesced_overlay_path(backup, transaction_id)
    ):
        raise RuntimeError("Quiesced-Overlay-Pfade sind nicht transaktionsgebunden")
    full_manifest, full_manifest_sha256 = _read_stable_verified_manifest(
        backup,
        expected_kind=SYSTEM_BACKUP_KIND,
    )
    overlay_manifest, overlay_manifest_sha256 = _read_stable_verified_manifest(
        overlay,
        expected_kind=QUIESCED_OVERLAY_KIND,
    )
    full_backup_id = str(full_manifest.get("backup_id") or "")
    overlay_id = str(overlay_manifest.get("backup_id") or "")
    if (
        str(full_manifest.get("install_root") or "") != root
        or str(overlay_manifest.get("install_root") or "") != root
        or str(overlay_manifest.get("transaction_id") or "") != transaction_id
        or str(overlay_manifest.get("parent_backup_id") or "") != full_backup_id
        or not re.fullmatch(r"[0-9a-f]{64}", full_backup_id)
        or not re.fullmatch(r"[0-9a-f]{64}", overlay_id)
    ):
        raise RuntimeError("Quiesced-Overlay-Manifest ist nicht an Vollbackup und Transaktion gebunden")

    overlay_descriptor, overlay_chain = _open_root_receipt_directory_chain(overlay)
    backup_descriptor, backup_chain = _open_root_receipt_directory_chain(backup)
    try:
        overlay_metadata = os.fstat(overlay_descriptor)
        backup_metadata = os.fstat(backup_descriptor)
    finally:
        os.close(overlay_descriptor)
        os.close(backup_descriptor)
    if (
        len(overlay_chain) < 2
        or len(backup_chain) < 2
        or overlay_chain[-2][:3] != backup_chain[-2][:3]
        or stat.S_IMODE(overlay_metadata.st_mode) != 0o700
        or stat.S_IMODE(backup_metadata.st_mode) != 0o700
    ):
        raise RuntimeError("Quiesced-Overlay und Vollbackup besitzen keinen gemeinsamen sicheren Parent")
    record = {
        "schema": QUIESCED_OVERLAY_RECEIPT_SCHEMA,
        "state": "pending",
        "transaction_id": transaction_id,
        "install_root": root,
        "overlay": {
            "dir": overlay,
            "dev": int(overlay_metadata.st_dev),
            "ino": int(overlay_metadata.st_ino),
            "id": overlay_id,
            "manifest_sha256": overlay_manifest_sha256,
        },
        "parent": {
            "dev": int(overlay_chain[-2][1]),
            "ino": int(overlay_chain[-2][2]),
        },
        "full_backup": {
            "dir": backup,
            "dev": int(backup_metadata.st_dev),
            "ino": int(backup_metadata.st_ino),
            "id": full_backup_id,
            "manifest_sha256": full_manifest_sha256,
        },
    }
    payload = _canonical_quiesced_overlay_receipt_bytes(record)
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        metadata = _create_owned_exact_root_file_at(
            state_descriptor,
            QUIESCED_OVERLAY_RECEIPT_NAME,
            payload,
            0o600,
        )
        os.fsync(state_descriptor)
    finally:
        os.close(state_descriptor)
    receipt = _parse_quiesced_overlay_receipt(payload, metadata)
    receipt = _revalidate_quiesced_overlay_receipt(
        receipt,
        backup_dir=backup,
        install_root=root,
        transaction_id=transaction_id,
    )
    if not isinstance(restore_guard, QuiescedOverlayRestoreGuard) or (
        _overlay_restore_guard_from_receipt(receipt) != restore_guard
    ):
        raise RuntimeError("Overlay-Restore-Guard driftete vor dem Root-Receipt")
    return receipt


def _revalidate_quiesced_overlay_receipt(
    receipt: QuiescedOverlayReceipt,
    *,
    backup_dir: str,
    install_root: str,
    transaction_id: str,
) -> QuiescedOverlayReceipt:
    """Beweist Receipt, Pfadinodes und beide Manifeste erneut ohne Mutation."""

    if not isinstance(receipt, QuiescedOverlayReceipt):
        raise RuntimeError("Quiesced-Overlay-Receipt fehlt oder besitzt den falschen Typ")
    current = _read_quiesced_overlay_receipt()
    if current != receipt:
        raise RuntimeError("Quiesced-Overlay-Receipt oder sein Inode driftete")
    transaction_id = str(transaction_id or "")
    backup = os.path.abspath(str(backup_dir or ""))
    root = os.path.abspath(str(install_root or ""))
    if (
        receipt.transaction_id != transaction_id
        or receipt.full_backup_dir != backup
        or receipt.install_root != root
        or receipt.overlay_dir != _quiesced_overlay_path(backup, transaction_id)
    ):
        raise RuntimeError("Quiesced-Overlay-Receipt widerspricht der Update-Transaktion")
    overlay_descriptor, overlay_chain = _open_root_receipt_directory_chain(
        receipt.overlay_dir
    )
    backup_descriptor, backup_chain = _open_root_receipt_directory_chain(backup)
    try:
        overlay_metadata = os.fstat(overlay_descriptor)
        backup_metadata = os.fstat(backup_descriptor)
    finally:
        os.close(overlay_descriptor)
        os.close(backup_descriptor)
    if (
        len(overlay_chain) < 2
        or len(backup_chain) < 2
        or (overlay_metadata.st_dev, overlay_metadata.st_ino)
        != (receipt.overlay_dev, receipt.overlay_ino)
        or (backup_metadata.st_dev, backup_metadata.st_ino)
        != (receipt.full_backup_dev, receipt.full_backup_ino)
        or (overlay_chain[-2][1], overlay_chain[-2][2])
        != (receipt.parent_dev, receipt.parent_ino)
        or overlay_chain[-2][:3] != backup_chain[-2][:3]
    ):
        raise RuntimeError("Quiesced-Overlay-, Vollbackup- oder Parent-Inode driftete")
    full_manifest, full_digest = _read_stable_verified_manifest(
        backup,
        expected_kind=SYSTEM_BACKUP_KIND,
    )
    overlay_manifest, overlay_digest = _read_stable_verified_manifest(
        receipt.overlay_dir,
        expected_kind=QUIESCED_OVERLAY_KIND,
    )
    if (
        full_digest != receipt.full_backup_manifest_sha256
        or str(full_manifest.get("backup_id") or "") != receipt.full_backup_id
        or str(full_manifest.get("install_root") or "") != root
        or overlay_digest != receipt.manifest_sha256
        or str(overlay_manifest.get("backup_id") or "") != receipt.backup_id
        or str(overlay_manifest.get("install_root") or "") != root
        or str(overlay_manifest.get("transaction_id") or "") != transaction_id
        or str(overlay_manifest.get("parent_backup_id") or "")
        != receipt.full_backup_id
    ):
        raise RuntimeError("Quiesced-Overlay- oder Vollbackupmanifest driftete")
    return receipt


def _guard_quiesced_overlay_manifest(
    manifest: dict,
    receipt: QuiescedOverlayReceipt,
) -> None:
    if (
        not isinstance(manifest, dict)
        or not isinstance(receipt, QuiescedOverlayReceipt)
        or manifest.get("kind") != QUIESCED_OVERLAY_KIND
        or str(manifest.get("backup_id") or "") != receipt.backup_id
        or str(manifest.get("install_root") or "") != receipt.install_root
        or str(manifest.get("transaction_id") or "") != receipt.transaction_id
        or str(manifest.get("parent_backup_id") or "") != receipt.full_backup_id
    ):
        raise RuntimeError("Overlay-Restore verwendet nicht das transaktionsgebundene Manifest")


def _overlay_restore_guard_from_receipt(
    receipt: QuiescedOverlayReceipt,
) -> QuiescedOverlayRestoreGuard:
    if not isinstance(receipt, QuiescedOverlayReceipt):
        raise RuntimeError("Overlay-Restore besitzt keinen Root-Receipt")
    return QuiescedOverlayRestoreGuard(
        transaction_id=receipt.transaction_id,
        overlay_dir=receipt.overlay_dir,
        overlay_dev=receipt.overlay_dev,
        overlay_ino=receipt.overlay_ino,
        backup_id=receipt.backup_id,
        manifest_sha256=receipt.manifest_sha256,
        install_root=receipt.install_root,
        parent_backup_dir=receipt.full_backup_dir,
        parent_backup_dev=receipt.full_backup_dev,
        parent_backup_ino=receipt.full_backup_ino,
        parent_backup_id=receipt.full_backup_id,
        parent_backup_manifest_sha256=receipt.full_backup_manifest_sha256,
        collection_dir=os.path.dirname(receipt.full_backup_dir),
        collection_dev=receipt.parent_dev,
        collection_ino=receipt.parent_ino,
    )


def _remove_quiesced_overlay_receipt_and_tree(
    receipt: QuiescedOverlayReceipt,
) -> None:
    """Entfernt nach Endgate erst das Blockier-Receipt, dann dessen eigenen Baum.

    Ein Crash darf höchstens einen harmlosen, inodegebundenen Backuprest
    hinterlassen. Umgekehrt wäre ein persistentes Receipt ohne Restorebaum ein
    nicht mehr automatisch auflösbarer Folgeupdate-Blocker.
    """

    _revalidate_quiesced_overlay_receipt(
        receipt,
        backup_dir=receipt.full_backup_dir,
        install_root=receipt.install_root,
        transaction_id=receipt.transaction_id,
    )
    state_descriptor = _open_recovery_bootblock_state_directory()
    try:
        current = _read_bound_root_file_at(
            state_descriptor,
            QUIESCED_OVERLAY_RECEIPT_NAME,
            maximum=64 * 1024,
            mode=0o600,
        )
        if current is None or (current[1].st_dev, current[1].st_ino) != (
            receipt.receipt_dev,
            receipt.receipt_ino,
        ):
            raise RuntimeError("Fremdes Quiesced-Overlay-Receipt wird nicht entfernt")
        os.unlink(QUIESCED_OVERLAY_RECEIPT_NAME, dir_fd=state_descriptor)
        os.fsync(state_descriptor)
    finally:
        os.close(state_descriptor)
    if _read_quiesced_overlay_receipt(allow_missing=True) is not None:
        raise RuntimeError("Quiesced-Overlay-Receipt blieb nach Cleanup vorhanden")
    _remove_tree_nofollow(receipt.overlay_dir)
    if os.path.lexists(receipt.overlay_dir):
        raise RuntimeError("Quiesced-Overlay-Cleanup blieb unvollständig")


def _persistent_receipt_reference_matches(
    reference: recovery_journal.RecoveryReceiptReference,
    *,
    path: str,
    device: int,
    inode: int,
    sha256: str,
) -> bool:
    """Vergleicht Journalreferenz und gebundenen Root-Beleg einschließlich Größe."""

    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return bool(
        reference.path == path
        and reference.device == int(device)
        and reference.inode == int(inode)
        and reference.sha256 == str(sha256)
        and reference.size == int(metadata.st_size)
        and (metadata.st_dev, metadata.st_ino)
        == (int(device), int(inode))
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
    )


def _assert_recovery_context_matches_journal(
    journal_contract: recovery_journal.RecoveryJournalContract,
    context_contract: recovery_context_codec.RecoveryContextContract,
) -> None:
    """Kreuzbindet die unveränderliche fachliche Altstandsautorität."""

    payload = journal_contract.payload
    context = context_contract.context
    if (
        context.transaction_id != payload.transaction_id
        or context.install_root != payload.install_root
        or context.install_user != payload.install_user
        or context_contract.context_sha256 != payload.transition_id
        or context.source.old_commit != payload.source.commit
        or context.source.bootstrap_without_git
        == payload.source.repository_present
        or context.source.bootstrap_rebuild_git
        != payload.source.repository_rebuild_required
        or context.target.commit != payload.target.commit
        or context.target.tag != payload.target.tag
        or context.target.role != payload.target.role
        or context.transition.ha_role != payload.target.role
        or context.backup.backup_id != payload.full_backup.backup_id
        or context.backup.manifest_sha256
        != payload.full_backup.manifest_sha256
        or not _context_reference_matches_journal(
            context.surface_receipt,
            payload.immutable_receipts.surface,
        )
        or not _context_reference_matches_journal(
            context.systemd_receipt,
            payload.immutable_receipts.systemd,
        )
    ):
        raise RuntimeError(
            "Recovery-Kontext widerspricht dem gebundenen Master-Journal"
        )


def _load_persistent_recovery_bundle(
    journal_contract: recovery_journal.RecoveryJournalContract,
) -> PersistentRecoveryBundle:
    """Bindet nach Prozessende alle noch vorhandenen Journalbegleiter neu."""

    journal_contract = recovery_journal.verify_recovery_journal(
        journal_contract
    )
    payload = journal_contract.payload
    terminal = payload.phase in {
        recovery_journal.PHASE_COMMITTED,
        recovery_journal.PHASE_ROLLED_BACK,
    }

    context_reference = payload.immutable_receipts.context
    context_contract = None
    if os.path.lexists(context_reference.path):
        context_contract = recovery_context_codec.read_recovery_context(
            expected_transaction_id=payload.transaction_id,
            expected_install_root=payload.install_root,
            expected_full_backup_id=payload.full_backup.backup_id,
            path=context_reference.path,
        )
        if context_contract is None or not _persistent_receipt_reference_matches(
            context_reference,
            path=context_contract.context_path,
            device=context_contract.context_device,
            inode=context_contract.context_inode,
            sha256=context_contract.context_sha256,
        ):
            raise RuntimeError("Recovery-Kontext driftete vom Master-Journal")
        _assert_recovery_context_matches_journal(
            journal_contract,
            context_contract,
        )
    elif not terminal:
        raise RuntimeError("Nichtterminaler Recovery-Kontext fehlt")

    surface_reference = payload.immutable_receipts.surface
    surface_contract = None
    if os.path.lexists(surface_reference.path):
        surface_contract = recovery_surface_codec.read_recovery_surface_receipt(
            receipt_path=surface_reference.path,
            expected_transaction_id=payload.transaction_id,
            expected_install_root=payload.install_root,
            expected_full_backup_id=payload.full_backup.backup_id,
        )
        if not _persistent_receipt_reference_matches(
            surface_reference,
            path=surface_contract.path,
            device=surface_contract.dev,
            inode=surface_contract.ino,
            sha256=surface_contract.sha256,
        ):
            raise RuntimeError("Recovery-Nebenflächenreceipt driftete vom Journal")
    elif not terminal:
        raise RuntimeError("Nichtterminales Recovery-Nebenflächenreceipt fehlt")

    systemd_reference = payload.immutable_receipts.systemd
    systemd_contract = None
    if os.path.lexists(systemd_reference.path):
        systemd_contract = recovery_surface_codec.read_systemd_recovery_receipt(
            receipt_path=systemd_reference.path,
            expected_transaction_id=payload.transaction_id,
            expected_install_root=payload.install_root,
            expected_full_backup_id=payload.full_backup.backup_id,
            expected_units=_recovery_bootblock_units(),
        )
        if not _persistent_receipt_reference_matches(
            systemd_reference,
            path=systemd_contract.path,
            device=systemd_contract.dev,
            inode=systemd_contract.ino,
            sha256=systemd_contract.sha256,
        ):
            raise RuntimeError("systemd-Recovery-Receipt driftete vom Journal")
    elif not terminal:
        raise RuntimeError("Nichtterminales systemd-Recovery-Receipt fehlt")

    if not terminal and (
        context_contract is None
        or surface_contract is None
        or systemd_contract is None
    ):
        raise RuntimeError("Nichtterminales Recovery-Bundle ist unvollständig")
    if terminal and (
        (systemd_contract is not None, surface_contract is not None, context_contract is not None)
        not in {
            (True, True, True),
            (False, True, True),
            (False, False, True),
            (False, False, False),
        }
    ):
        raise RuntimeError(
            "Terminale Recovery-Begleiter besitzen eine unmögliche Cleanup-Reihenfolge"
        )
    return PersistentRecoveryBundle(
        journal=journal_contract,
        context=context_contract,
        surface=surface_contract,
        systemd=systemd_contract,
    )


def _manifest_tree_inventory(
    manifest: dict,
    *,
    category: str,
    root: str,
) -> frozenset[str]:
    """Rekonstruiert ein bereits gesichertes Baum-Inventar aus dem Manifest."""

    root_path = Path(os.path.abspath(root))
    source_records = [
        item
        for item in manifest.get("sources") or ()
        if isinstance(item, dict)
        and item.get("category") == category
        and item.get("source") == str(root_path)
    ]
    if len(source_records) != 1:
        raise RuntimeError(
            f"Backupmanifest besitzt keinen eindeutigen {category}-Quellbaum"
        )
    source = source_records[0]
    if (
        source.get("present") is not True
        or source.get("source_type") != "directory"
        or not isinstance(source.get("directories"), list)
    ):
        raise RuntimeError(f"Backupmanifest besitzt keinen {category}-Verzeichnisbaum")
    entries: set[str] = set()
    for raw in source["directories"]:
        if not isinstance(raw, dict):
            raise RuntimeError(f"{category}-Verzeichnisinventar ist ungültig")
        relative = str(raw.get("path") or "")
        candidate = Path(relative)
        if (
            not relative
            or candidate.is_absolute()
            or ".." in candidate.parts
            or candidate.as_posix() != relative
        ):
            raise RuntimeError(f"{category}-Verzeichnisinventar enthält Fremdpfade")
        entries.add(relative)
    for raw in manifest.get("files") or ():
        if not isinstance(raw, dict) or raw.get("category") != category:
            continue
        restore_path = str(raw.get("restore_path") or "")
        try:
            relative = Path(restore_path).relative_to(root_path).as_posix()
        except (TypeError, ValueError):
            raise RuntimeError(
                f"{category}-Dateiinventar enthält einen fremden Restorepfad"
            )
        if not relative or relative == "." or ".." in Path(relative).parts:
            raise RuntimeError(f"{category}-Dateiinventar enthält Fremdpfade")
        entries.add(relative)
    return frozenset(entries)


def _read_manifest_json_preimage(
    *,
    backup_dir: str,
    manifest: dict,
    restore_path: str,
    expected_sha256: str,
) -> dict:
    """Liest genau ein nofollow-gebundenes JSON-Preimage aus dem Vollbackup."""

    matches = [
        item
        for item in manifest.get("files") or ()
        if isinstance(item, dict) and item.get("restore_path") == restore_path
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Backup besitzt kein eindeutiges Konfigurationspreimage für {restore_path}"
        )
    entry = matches[0]
    relative = Path(str(entry.get("path") or ""))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RuntimeError("Konfigurationspreimage besitzt einen ungültigen Backuppfad")
    source = os.path.join(backup_dir, *relative.parts)
    descriptor, metadata = _open_regular_file_nofollow(source)
    try:
        if metadata.st_size < 0 or metadata.st_size > 4 * 1024 * 1024:
            raise RuntimeError("Konfigurationspreimage ist unplausibel groß")
        payload = b""
        while len(payload) <= metadata.st_size:
            block = os.read(descriptor, min(65536, metadata.st_size + 1 - len(payload)))
            if not block:
                break
            payload += block
    finally:
        os.close(descriptor)
    digest = hashlib.sha256(payload).hexdigest()
    if (
        len(payload) != metadata.st_size
        or len(payload) != int(entry.get("size", -1))
        or digest != str(entry.get("sha256") or "")
        or digest != expected_sha256
    ):
        raise RuntimeError("Konfigurationspreimage driftete vom Recovery-Kontext")
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Konfigurationspreimage ist kein gültiges UTF-8-JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Konfigurationspreimage ist kein JSON-Objekt")
    return parsed


def _reconstruct_transition_state(
    context: recovery_context_codec.RecoveryContext,
    *,
    backup_dir: str,
    manifest: dict,
) -> TransitionState:
    transition = context.transition
    if transition.config_source == recovery_context_codec.CONFIG_SOURCE_SYNTHETIC_MISSING:
        config = {"ha_mode": transition.ha_role}
        if transition.config_sha256 != hashlib.sha256(b"").hexdigest():
            raise RuntimeError("Synthetischer Konfigurationsvertrag driftete")
    elif transition.config_source == recovery_context_codec.CONFIG_SOURCE_FULL_BACKUP:
        config = _read_manifest_json_preimage(
            backup_dir=backup_dir,
            manifest=manifest,
            restore_path=transition.config_path,
            expected_sha256=transition.config_sha256,
        )
    else:
        raise RuntimeError("Recovery-Kontext besitzt eine unbekannte Konfigurationsquelle")
    if str(config.get("ha_mode") or "").strip().lower() != transition.ha_role:
        raise RuntimeError("Konfigurationspreimage widerspricht der gebundenen Rolle")
    return TransitionState(
        ha_role=transition.ha_role,
        config=dict(config),
        config_sha256=transition.config_sha256,
        config_path=transition.config_path,
        preinstalled_units=frozenset(transition.preinstalled_units),
        preactive_units=frozenset(transition.preactive_units),
        bootstrap_legacy_config=transition.bootstrap_legacy_config,
        legacy_e3dc_activity=transition.legacy_e3dc_activity,
    )


def _runtime_root_managed_preimage(
    item: recovery_surface_codec.RootManagedFileRecoveryPreimage,
) -> RootManagedFilePreimage:
    return RootManagedFilePreimage(
        path=item.path,
        existed=item.existed,
        payload=bytes(item.payload or b""),
        uid=int(item.uid if item.uid is not None else -1),
        gid=int(item.gid if item.gid is not None else -1),
        mode=int(item.mode if item.mode is not None else 0),
        parent_dev=item.parent_dev,
        parent_ino=item.parent_ino,
    )


def _runtime_apache_preimage(
    item: recovery_surface_codec.ApacheSecurityRecoveryPreimage,
) -> ApacheSecurityPreimage:
    return ApacheSecurityPreimage(
        available=item.available,
        payload=bytes(item.payload or b""),
        uid=int(item.uid if item.uid is not None else -1),
        gid=int(item.gid if item.gid is not None else -1),
        mode=int(item.mode if item.mode is not None else 0),
        enabled=item.enabled,
        enabled_target=str(item.enabled_target or ""),
        apache_available=item.apache_available,
        apache_was_active=item.apache_was_active,
        apache_unit_file_state=item.apache_unit_file_state,
    )


def _reconstruct_recovery_inventory(
    context: recovery_context_codec.RecoveryContext,
    surface: recovery_surface_codec.RecoverySurfaceReceipt,
    *,
    manifest: dict,
) -> RecoverySurfaceInventory:
    web_inventory = _manifest_tree_inventory(
        manifest,
        category="web-program",
        root="/var/www/html",
    )
    web_count, web_sha256 = recovery_context_codec.inventory_entries_fingerprint(
        web_inventory
    )
    if (
        web_count != context.inventory.web_entries_count
        or web_sha256 != context.inventory.web_entries_sha256
    ):
        raise RuntimeError("Web-Inventar driftete vom Recovery-Kontext")
    watchdogs = frozenset(
        str(item.get("restore_path"))
        for item in manifest.get("files") or ()
        if isinstance(item, dict)
        and item.get("category") == "watchdog"
        and item.get("restore_path")
    )
    if watchdogs != frozenset(context.inventory.watchdog_files):
        raise RuntimeError("Watchdog-Inventar driftete vom Recovery-Kontext")
    if surface.apache_security is None:
        raise RuntimeError("Recovery-Nebenfläche besitzt kein Apache-Preimage")
    return RecoverySurfaceInventory(
        web_program_entries=web_inventory,
        watchdog_files=watchdogs,
        unit_enablement=(),
        root_managed_files=tuple(
            _runtime_root_managed_preimage(item)
            for item in surface.root_managed_files
        ),
        apache_security=_runtime_apache_preimage(surface.apache_security),
        root_file_preimages=surface.root_files,
        crontab_preimages=surface.crontabs,
    )


def _runtime_directory_chain(
    chain: tuple[recovery_context_codec.DirectoryIdentity, ...],
) -> tuple[tuple[str, int, int, int, int, int], ...]:
    return tuple(
        (
            item.path,
            item.device,
            item.inode,
            item.uid,
            item.gid,
            item.mode,
        )
        for item in chain
    )


def _runtime_privileged_backup_payloads(
    context: recovery_context_codec.RecoveryContext,
) -> tuple[PrivilegedBackupFileReceipt, ...]:
    return tuple(
        PrivilegedBackupFileReceipt(
            restore_path=item.restore_path,
            category=item.category,
            backup_relative_path=item.backup_relative_path,
            parent_path_chain=_runtime_directory_chain(item.parent_path_chain),
            dev=item.device,
            ino=item.inode,
            sha256=item.sha256,
            size=item.size,
            mode=item.mode,
            uid=item.uid,
            gid=item.gid,
            nlink=item.nlink,
            mtime_ns=item.mtime_ns,
            ctime_ns=item.ctime_ns,
        )
        for item in context.privileged_backup_payloads
    )


def _reconstruct_backup_contracts(
    context: recovery_context_codec.RecoveryContext,
) -> tuple[
    dict,
    frozenset[str],
    RepoRecoveryContract | None,
    RecoveryBackupReceipt | None,
]:
    """Bindet Vollbackup, Inventar und optionale Git-Recovery neu."""

    backup_dir = context.backup.backup_dir
    descriptor, path_chain = _open_root_receipt_directory_chain(backup_dir)
    try:
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    expected_chain = _runtime_directory_chain(context.backup.path_chain)
    if (
        path_chain != expected_chain
        or len(path_chain) < 2
        or (metadata.st_dev, metadata.st_ino)
        != (context.backup.backup_device, context.backup.backup_inode)
        or (path_chain[-2][1], path_chain[-2][2])
        != (context.backup.parent_device, context.backup.parent_inode)
    ):
        raise RuntimeError("Vollbackup- oder Parent-Inode driftete vom Kontext")
    manifest, manifest_sha256 = _read_stable_verified_backup_manifest(backup_dir)
    if (
        manifest_sha256 != context.backup.manifest_sha256
        or str(manifest.get("backup_id") or "") != context.backup.backup_id
        or str(manifest.get("install_root") or "") != context.install_root
    ):
        raise RuntimeError("Vollbackupmanifest driftete vom Recovery-Kontext")

    install_inventory = _manifest_tree_inventory(
        manifest,
        category="install-tree",
        root=context.install_root,
    )
    install_count, install_sha256 = (
        recovery_context_codec.inventory_entries_fingerprint(install_inventory)
    )
    if (
        install_count != context.inventory.install_entries_count
        or install_sha256 != context.inventory.install_entries_sha256
    ):
        raise RuntimeError("Installationsinventar driftete vom Recovery-Kontext")

    payload_receipts = _runtime_privileged_backup_payloads(context)
    if payload_receipts != _privileged_backup_payload_receipts(
        backup_dir,
        manifest,
    ):
        raise RuntimeError("Privilegierte Backup-Payloads drifteten vom Kontext")

    repo_binding = context.repo
    if context.source.old_commit is None:
        if repo_binding is not None:
            raise RuntimeError("Bootstrap-Kontext besitzt unerwartete Git-Recovery")
        return manifest, install_inventory, None, None
    if repo_binding is None or repo_binding.expected_commit != context.source.old_commit:
        raise RuntimeError("Git-Recovery-Bindung fehlt für den Alt-Commit")
    tracked_git = tuple(
        (
            item.relative_path,
            item.git_mode,
            item.git_object_id,
        )
        for item in repo_binding.tracked_git
    )
    file_contracts, directory_contracts = _recovery_repo_contracts_from_manifest(
        manifest,
        context.install_root,
        tracked_git,
    )
    git_by_path = {
        relative_path: (git_mode, git_object_id)
        for relative_path, git_mode, git_object_id in tracked_git
    }
    tracked_files = tuple(
        (
            relative_path,
            git_by_path[relative_path][0],
            git_by_path[relative_path][1],
            digest,
            size,
            mode,
            uid,
            gid,
        )
        for relative_path, (digest, size, mode, uid, gid)
        in sorted(file_contracts.items())
    )
    repo_contract = RepoRecoveryContract(
        install_root=context.install_root,
        install_user=context.install_user,
        expected_commit=repo_binding.expected_commit,
        tracked_files=tracked_files,
        dirty_paths=repo_binding.dirty_paths,
    )
    privileged_files = _privileged_restore_contract_from_manifest(
        manifest,
        context.install_user,
        verify_sources=False,
    )
    backup_receipt = RecoveryBackupReceipt(
        backup_dir=backup_dir,
        backup_dev=context.backup.backup_device,
        backup_ino=context.backup.backup_inode,
        parent_dev=context.backup.parent_device,
        parent_ino=context.backup.parent_inode,
        backup_path_chain=expected_chain,
        transaction_id=context.transaction_id,
        backup_id=context.backup.backup_id,
        manifest_sha256=context.backup.manifest_sha256,
        manifest_semantic_sha256=_manifest_semantic_sha256(manifest),
        install_root=context.install_root,
        expected_commit=repo_binding.expected_commit,
        tracked_files=tuple(
            (
                relative_path,
                digest,
                size,
                mode,
                uid,
                gid,
            )
            for relative_path, (digest, size, mode, uid, gid)
            in sorted(file_contracts.items())
        ),
        tracked_directories=tuple(
            (relative_path, mode, uid, gid)
            for relative_path, (mode, uid, gid)
            in sorted(directory_contracts.items())
        ),
        manifest_files=_manifest_file_receipt(manifest),
        privileged_files=privileged_files,
        privileged_backup_files=payload_receipts,
    )
    _revalidate_recovery_backup_payload_receipt(
        backup_receipt,
        backup_dir=backup_dir,
        repo_dir=context.install_root,
        transaction_id=context.transaction_id,
    )
    return manifest, install_inventory, repo_contract, backup_receipt


def _static_bootblock_namespace_present() -> bool:
    if os.path.lexists(RECOVERY_BOOTBLOCK_MARKER):
        return True
    return any(os.path.lexists(_recovery_dropin_path(unit)) for unit in _recovery_bootblock_units())


def _rebind_or_rearm_static_bootblock(
    transaction_id: str,
) -> RecoveryBootblockContract:
    """Bindet einen frühen statischen Gate-Crash und vervollständigt nur ihn."""

    payload = _render_recovery_bootblock_dropin(transaction_id)
    identities = []
    for unit in _recovery_bootblock_units():
        path = _recovery_dropin_path(unit)
        if not os.path.lexists(path):
            continue
        parent_descriptor = _open_directory_nofollow(os.path.dirname(path))
        try:
            metadata = _read_exact_root_file_at(
                parent_descriptor,
                os.path.basename(path),
                payload,
                0o644,
            )
            if metadata is None:
                raise RuntimeError(f"Statisches Recovery-Drop-in fehlt: {unit}")
            identities.append((unit, int(metadata.st_dev), int(metadata.st_ino)))
        finally:
            os.close(parent_descriptor)
    partial = RecoveryBootblockPartialContract(
        units=_recovery_bootblock_units(),
        created_directories=(),
        transaction_id=transaction_id,
        dropin_identities=tuple(identities),
        allow_missing_directories=True,
    )
    return _arm_persistent_recovery_bootblock(
        partial,
        transaction_id=transaction_id,
    )


def _assert_package_binding_matches_journal(
    package_receipt: PreparedPackageReceipt | PackageRecoveryReceipt,
    journal_contract: recovery_journal.RecoveryJournalContract,
) -> None:
    payload = journal_contract.payload
    binding = payload.package
    if binding is None:
        raise RuntimeError("Master-Journal besitzt keine Paketbindung")
    if (
        package_receipt.receipt_path != binding.path
        or package_receipt.transaction_id != binding.transaction_id
        or package_receipt.install_root != binding.install_root
        or package_receipt.full_backup_id != binding.full_backup_id
        or binding.transaction_id != payload.transaction_id
        or binding.install_root != payload.install_root
        or binding.full_backup_id != payload.full_backup.backup_id
        or binding.target_identity_sha256 != payload.target.identity_sha256
        or package_receipt.target_commit != payload.target.commit
        or package_receipt.target_tag != payload.target.tag
        or package_receipt.role != payload.target.role
        or _package_prestate_shape_sha256(package_receipt.package_transaction)
        != binding.prestate_shape_sha256
    ):
        raise RuntimeError("Paket-Receipt widerspricht dem Master-Journal")


def _assert_dynamic_safety_matches_journal(
    safety_contract: UpdateSafetyContract,
    journal_contract: recovery_journal.RecoveryJournalContract,
) -> None:
    payload = journal_contract.payload
    binding = payload.safety
    if binding is None or binding.mode != recovery_journal.GATE_MODE_DYNAMIC:
        raise RuntimeError("Master-Journal besitzt keine dynamische Safety-Bindung")
    if (
        binding.receipt_path != safety_contract.receipt_path
        or binding.transaction_id != safety_contract.transaction_id
        or binding.install_root != payload.install_root
        or binding.full_backup_id != safety_contract.backup_id
        or binding.full_backup_id != payload.full_backup.backup_id
        or binding.target_identity_sha256 != payload.target.identity_sha256
        or binding.receipt_shape_sha256
        != _dynamic_safety_shape_sha256(safety_contract)
        or safety_contract.target_commit != payload.target.commit
        or safety_contract.target_tag != payload.target.tag
        or safety_contract.role != payload.target.role
    ):
        raise RuntimeError("Dynamisches Safety-Receipt widerspricht dem Journal")


def _bind_persisted_runtime_receipts(
    bundle: PersistentRecoveryBundle,
) -> tuple[
    PreparedPackageReceipt | PackageRecoveryReceipt | None,
    PackageTransactionState | None,
    UpdateSafetyContract | None,
    RecoveryBootblockContract | None,
    QuiescedOverlayReceipt | None,
    OfflinePackageReceipt | None,
]:
    """Bindet veränderliche Begleitreceipts ausschließlich über Journalsemantik."""

    journal_contract = recovery_journal.verify_recovery_journal(bundle.journal)
    payload = journal_contract.payload
    terminal = payload.phase in {
        recovery_journal.PHASE_COMMITTED,
        recovery_journal.PHASE_ROLLED_BACK,
    }
    package_receipt = _read_prepared_package_receipt(allow_missing=True)
    package_transaction = None
    if payload.package is not None:
        if package_receipt is None:
            if not terminal:
                raise RuntimeError("Nichtterminales Paket-Receipt fehlt")
        else:
            _assert_package_binding_matches_journal(package_receipt, journal_contract)
            package_transaction = _package_transaction_from_receipt(package_receipt)
            if (
                payload.phase == recovery_journal.PHASE_PRODUCT_MUTATING
                and package_receipt.state != "prepared"
            ):
                raise RuntimeError("Produktmutation besitzt kein prepared Paket-Receipt")
            if (
                payload.phase == recovery_journal.PHASE_COMMITTED
                and package_receipt.state == "applying"
            ):
                raise RuntimeError("Committed Journal besitzt nur applying Paketzustand")
            if (
                payload.phase == recovery_journal.PHASE_ROLLED_BACK
                and package_receipt.state == "committed"
            ):
                raise RuntimeError("Rolled-back Journal besitzt committed Paketzustand")
    elif package_receipt is not None:
        # Crashfenster nach Receipt-Create, aber vor der Journalbindung: Das
        # erste Paketkommando war noch nicht autorisiert. Der Beleg darf daher
        # nur derselben Transaktion zugeordnet und anschließend entfernt werden.
        if (
            payload.phase != recovery_journal.PHASE_PREPRODUCT
            or package_receipt.transaction_id != payload.transaction_id
            or package_receipt.install_root != payload.install_root
            or package_receipt.full_backup_id != payload.full_backup.backup_id
            or package_receipt.target_commit != payload.target.commit
            or package_receipt.target_tag != payload.target.tag
            or package_receipt.role != payload.target.role
        ):
            raise RuntimeError("Ungebundenes Paket-Receipt gehört nicht zur preproduct-Transaktion")

    update_safety_contract = _read_update_safety_contract(allow_missing=True)
    static_bootblock_contract = None
    safety_binding = payload.safety
    if safety_binding is not None:
        if safety_binding.mode == recovery_journal.GATE_MODE_DYNAMIC:
            if update_safety_contract is None:
                if not terminal:
                    raise RuntimeError("Nichtterminales dynamisches Safety-Receipt fehlt")
            else:
                _assert_dynamic_safety_matches_journal(
                    update_safety_contract,
                    journal_contract,
                )
                if (
                    payload.phase == recovery_journal.PHASE_ROLLED_BACK
                    and update_safety_contract.state == "committed"
                ):
                    raise RuntimeError("Rolled-back Journal besitzt committed Safety")
                if not terminal:
                    if update_safety_contract.state != "pending":
                        raise RuntimeError(
                            "Nichtterminales Journal besitzt kein pending Safety-Receipt"
                        )
                    if os.path.lexists(RECOVERY_BOOTBLOCK_MARKER):
                        _verify_update_safety_marker(
                            update_safety_contract,
                            expected_present=True,
                        )
                        _reload_and_verify_update_safety_dropins(
                            update_safety_contract,
                            expected_present=True,
                        )
                    else:
                        # Ein Stromausfall kann exakt zwischen Gateöffnung und
                        # durable rolled_back liegen. Nur die unveränderten,
                        # receiptgebundenen 00-Inodes dürfen den Marker derselben
                        # Transaktion wieder scharf stellen.
                        update_safety_contract = _arm_update_safety_contract(
                            update_safety_contract
                        )
                        _assert_dynamic_safety_matches_journal(
                            update_safety_contract,
                            journal_contract,
                        )
        elif safety_binding.mode == recovery_journal.GATE_MODE_STATIC:
            if update_safety_contract is not None:
                raise RuntimeError("Statisches Journal besitzt dynamisches Safety-Receipt")
            if package_receipt is not None and package_receipt.static_recovery_contract_json:
                static_bootblock_contract = _parse_recovery_bootblock_contract(
                    package_receipt.static_recovery_contract_json,
                    verify_active_gate=False,
                )
                if (
                    safety_binding.static_contract_sha256
                    != _static_bootblock_shape_sha256(static_bootblock_contract)
                ):
                    raise RuntimeError("Statischer Bootblock driftete vom Journal")
                if not terminal:
                    if os.path.lexists(RECOVERY_BOOTBLOCK_MARKER):
                        _verify_recovery_bootblock_marker(
                            static_bootblock_contract,
                            expected_present=True,
                        )
                        _reload_and_verify_recovery_dropins(
                            static_bootblock_contract.units,
                            expected_present=True,
                            transaction_id=static_bootblock_contract.transaction_id,
                        )
                    else:
                        static_bootblock_contract = (
                            _arm_persistent_recovery_bootblock(
                                static_bootblock_contract,
                                transaction_id=payload.transaction_id,
                            )
                        )
                        if (
                            safety_binding.static_contract_sha256
                            != _static_bootblock_shape_sha256(
                                static_bootblock_contract
                            )
                        ):
                            raise RuntimeError(
                                "Reaktivierter statischer Bootblock driftete vom Journal"
                            )
            elif not terminal:
                raise RuntimeError("Nichtterminaler statischer Bootblockvertrag fehlt")
        else:
            raise RuntimeError("Journal besitzt einen unbekannten Gate-Modus")
    else:
        if update_safety_contract is not None:
            if (
                payload.phase != recovery_journal.PHASE_PREPRODUCT
                or update_safety_contract.transaction_id != payload.transaction_id
                or update_safety_contract.backup_id != payload.full_backup.backup_id
                or update_safety_contract.target_commit != payload.target.commit
                or update_safety_contract.target_tag != payload.target.tag
                or update_safety_contract.role != payload.target.role
            ):
                raise RuntimeError("Ungebundenes Safety-Receipt gehört nicht zum Journal")
        elif payload.phase == recovery_journal.PHASE_PREPRODUCT and _static_bootblock_namespace_present():
            static_bootblock_contract = _rebind_or_rearm_static_bootblock(
                payload.transaction_id
            )

    overlay_receipt = _read_quiesced_overlay_receipt(allow_missing=True)
    if payload.overlay is not None:
        if overlay_receipt is None:
            if not terminal:
                raise RuntimeError("Nichtterminales Quiesced-Overlay fehlt")
        else:
            reference = payload.overlay.receipt
            if (
                overlay_receipt.transaction_id != payload.transaction_id
                or overlay_receipt.install_root != payload.install_root
                or overlay_receipt.full_backup_id != payload.full_backup.backup_id
                or overlay_receipt.backup_id != payload.overlay.backup_id
                or overlay_receipt.manifest_sha256 != payload.overlay.manifest_sha256
                or not _persistent_receipt_reference_matches(
                    reference,
                    path=reference.path,
                    device=overlay_receipt.receipt_dev,
                    inode=overlay_receipt.receipt_ino,
                    sha256=overlay_receipt.receipt_sha256,
                )
            ):
                raise RuntimeError("Quiesced-Overlay widerspricht dem Journal")
    elif overlay_receipt is not None:
        raise RuntimeError("Journalphase besitzt ein unerwartetes Quiesced-Overlay")

    offline_receipt = None
    if (
        isinstance(package_receipt, PreparedPackageReceipt)
        and package_receipt.prepared_state is not None
    ):
        offline_receipt = parse_offline_package_receipt(
            package_receipt.prepared_state.offline_receipt_json.encode("utf-8")
        )
    return (
        package_receipt,
        package_transaction,
        update_safety_contract,
        static_bootblock_contract,
        overlay_receipt,
        offline_receipt,
    )


def _reconstruct_persisted_recovery_transaction(
    journal_contract: recovery_journal.RecoveryJournalContract,
) -> ReconstructedRecoveryTransaction:
    bundle = _load_persistent_recovery_bundle(journal_contract)
    (
        package_receipt,
        package_transaction,
        update_safety_contract,
        static_bootblock_contract,
        overlay_receipt,
        offline_receipt,
    ) = _bind_persisted_runtime_receipts(bundle)
    phase = bundle.journal.payload.phase
    rollback_finish_pending = bool(
        phase == recovery_journal.PHASE_ROLLED_BACK
        and (
            update_safety_contract is not None
            or static_bootblock_contract is not None
        )
    )
    if rollback_finish_pending and (
        bundle.context is None
        or bundle.surface is None
        or bundle.systemd is None
    ):
        raise RuntimeError(
            "Rolled-back Altstart verlor vor seinem Abschluss einen Parent-Beleg"
        )
    if phase == recovery_journal.PHASE_COMMITTED or (
        phase == recovery_journal.PHASE_ROLLED_BACK
        and not rollback_finish_pending
    ):
        return ReconstructedRecoveryTransaction(
            bundle=bundle,
            transition_state=None,
            install_inventory=None,
            recovery_inventory=None,
            repo_contract=None,
            backup_receipt=None,
            package_receipt=package_receipt,
            package_transaction=package_transaction,
            update_safety_contract=update_safety_contract,
            static_bootblock_contract=static_bootblock_contract,
            overlay_receipt=overlay_receipt,
            offline_receipt=offline_receipt,
        )
    if bundle.context is None or bundle.surface is None or bundle.systemd is None:
        raise RuntimeError("Nichtterminale Recovery-Transaktion ist unvollständig")
    context = bundle.context.context
    (
        manifest,
        install_inventory,
        repo_contract,
        backup_receipt,
    ) = _reconstruct_backup_contracts(context)
    transition_state = _reconstruct_transition_state(
        context,
        backup_dir=context.backup.backup_dir,
        manifest=manifest,
    )
    recovery_inventory = _reconstruct_recovery_inventory(
        context,
        bundle.surface.receipt,
        manifest=manifest,
    )
    return ReconstructedRecoveryTransaction(
        bundle=bundle,
        transition_state=transition_state,
        install_inventory=install_inventory,
        recovery_inventory=recovery_inventory,
        repo_contract=repo_contract,
        backup_receipt=backup_receipt,
        package_receipt=package_receipt,
        package_transaction=package_transaction,
        update_safety_contract=update_safety_contract,
        static_bootblock_contract=static_bootblock_contract,
        overlay_receipt=overlay_receipt,
        offline_receipt=offline_receipt,
    )


def _persistent_gate_dropin_preimages(
    guard: recovery_surface_codec.SystemdRecoveryRestoreGuard,
    *,
    update_safety_contract: UpdateSafetyContract | None,
    static_bootblock_contract: RecoveryBootblockContract | None,
) -> tuple[recovery_surface_codec.SystemdFilePreimage, ...]:
    if (update_safety_contract is None) == (static_bootblock_contract is None):
        raise RuntimeError("Systemd-Recovery benötigt genau einen Gatevertrag")
    contract = update_safety_contract or static_bootblock_contract
    expected_identities = {
        unit: (device, inode)
        for unit, device, inode in contract.dropin_identities
    }
    expected_payload = (
        _render_update_safety_dropin(contract.transaction_id)
        if update_safety_contract is not None
        else _render_recovery_bootblock_dropin(contract.transaction_id)
    )
    result = []
    for unit_state in guard.current.units:
        path = _recovery_dropin_path(unit_state.unit)
        candidates = tuple(
            item
            for item in unit_state.managed_dropins.entries
            if item.path == path
        )
        if len(candidates) != 1:
            raise RuntimeError(
                f"Startsperr-Drop-in ist nicht eindeutig gebunden: {unit_state.unit}"
            )
        item = candidates[0]
        if (
            item.kind != "regular"
            or bytes(item.payload or b"") != expected_payload
            or item.sha256 != hashlib.sha256(expected_payload).hexdigest()
            or item.uid != 0
            or item.gid != 0
            or item.mode != 0o644
            or item.identity is None
            or tuple(item.identity[:2]) != expected_identities.get(unit_state.unit)
        ):
            raise RuntimeError(
                f"Startsperr-Drop-in driftete vom Gatevertrag: {unit_state.unit}"
            )
        result.append(item)
    if len(result) != len(guard.current.units):
        raise RuntimeError("Startsperr-Drop-in-Menge ist unvollständig")
    return tuple(result)


def _restore_persistent_systemd_prestate(
    transaction: ReconstructedRecoveryTransaction,
) -> recovery_journal.RecoveryJournalContract:
    """Restauriert systemd offline und entscheidet vor Gateöffnung durable Alt."""

    bundle = transaction.bundle
    if bundle.systemd is None:
        raise RuntimeError("Persistentes systemd-Recovery-Receipt fehlt")
    journal_contract = recovery_journal.verify_recovery_journal(bundle.journal)
    if journal_contract.payload.phase not in {
        recovery_journal.PHASE_PREPRODUCT,
        recovery_journal.PHASE_PRODUCT_MUTATING,
    }:
        raise RuntimeError("Systemd-Altstart besitzt keine Recovery-Phase")
    safety = transaction.update_safety_contract
    static_gate = transaction.static_bootblock_contract
    if (safety is None) == (static_gate is None):
        raise RuntimeError("Systemd-Altstart besitzt keinen eindeutigen Gatevertrag")
    receipt = bundle.systemd.receipt

    def closed_gate_verifier(transaction_id: str, units: tuple[str, ...]) -> bool:
        current_journal = recovery_journal.read_recovery_journal()
        if (
            current_journal is None
            or not _same_recovery_journal_transaction_shape(
                current_journal,
                journal_contract,
            )
            or current_journal.payload.phase != journal_contract.payload.phase
            or transaction_id != journal_contract.payload.transaction_id
            or tuple(units) != tuple(item.unit for item in receipt.units)
        ):
            return False
        if safety is not None:
            _validate_update_safety_contract(safety, expected_state="pending")
            _verify_update_safety_marker(safety, expected_present=True)
            _reload_and_verify_update_safety_dropins(safety, expected_present=True)
        else:
            _validate_recovery_bootblock_contract(static_gate)
            _verify_recovery_bootblock_marker(static_gate, expected_present=True)
            _reload_and_verify_recovery_dropins(
                static_gate.units,
                expected_present=True,
                transaction_id=static_gate.transaction_id,
            )
        return True

    guard = recovery_surface_codec.capture_systemd_recovery_restore_guard(
        receipt
    )
    preserved = _persistent_gate_dropin_preimages(
        guard,
        update_safety_contract=safety,
        static_bootblock_contract=static_gate,
    )
    plan = recovery_surface_codec.restore_systemd_files_masks_enablement(
        receipt,
        guard,
        closed_gate_verifier=closed_gate_verifier,
        preserved_gate_dropins=preserved,
    )
    rolled_back_journal = None

    def start_authorizer(transaction_id: str, units: tuple[str, ...]) -> bool:
        nonlocal rolled_back_journal
        if closed_gate_verifier(transaction_id, units) is not True:
            return False
        # Ab hier ist Produkt-, Paket-, Nebenflächen- und systemd-Preimage
        # offline verifiziert. Die Richtung Altstand wird vor dem ersten
        # Marker-/Drop-in-Unlink dauerhaft entschieden, damit ein Stromausfall
        # mitten im per-Unit-Gate-Cleanup nur diesen Altstart fortsetzen darf.
        rolled_back_journal = _advance_persistent_recovery_rolled_back(
            journal_contract
        )
        if safety is not None:
            _clear_update_safety_marker(safety)
        else:
            _clear_recovery_bootblock_marker(static_gate)
        return True

    try:
        recovery_surface_codec.restore_systemd_pre_active_state(
            receipt,
            plan,
            start_authorizer=start_authorizer,
        )
    except BaseException:
        terminal = _read_matching_rolled_back_recovery_journal(
            journal_contract,
            allow_missing=True,
        )
        if terminal is None:
            try:
                if safety is not None:
                    _rearm_pending_update_safety_contract(safety)
                else:
                    _arm_persistent_recovery_bootblock(
                        static_gate,
                        transaction_id=journal_contract.payload.transaction_id,
                    )
            except Exception as gate_error:
                update_logger.critical(
                    "Systemd-Recovery-Gate konnte nach Startfehler nicht erneut gebunden werden: %s",
                    gate_error,
                )
        else:
            update_logger.warning(
                "Altstand ist durable gewählt; ein Folgeaufruf vollendet "
                "ausschließlich Gate-Cleanup und Altstart."
            )
        raise
    if rolled_back_journal is None:
        raise RuntimeError("Systemd-Altstart verlor den durable rolled_back-Beleg")
    return rolled_back_journal


def _settle_terminal_finalizer_lease(
    contract: (
        UpdateSafetyContract
        | RecoveryBootblockContract
        | legacy_safety_codec.LegacyUpdateSafetyReceipt
    ),
) -> None:
    """Akzeptiert failed/PID0 erst nach bestätigtem reset-failed-Readback."""

    try:
        _assert_committed_finalizer_lease_inactive(contract)
        return
    except Exception as original_error:
        properties = ("Id", "ActiveState", "SubState", "MainPID")
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
            if separator == "=" and key in properties and key not in values:
                values[key] = value
        if (
            not result.get("success")
            or result.get("timed_out")
            or int(result.get("returncode", -1)) != 0
            or str(result.get("stderr") or "")
            or values
            != {
                "Id": contract.finalizer_unit,
                "ActiveState": "failed",
                "SubState": "failed",
                "MainPID": "0",
            }
        ):
            raise original_error
        _assert_no_same_transaction_finalizer_processes(contract)
        reset = _run_argv(
            ["systemctl", "reset-failed", "--", contract.finalizer_unit],
            timeout=15,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        if (
            not reset.get("success")
            or reset.get("timed_out")
            or int(reset.get("returncode", -1)) != 0
            or str(reset.get("stderr") or "")
        ):
            raise RuntimeError(
                "Beendete failed Finalizer-Lease konnte nicht zurückgesetzt werden"
            ) from original_error
        _assert_committed_finalizer_lease_inactive(contract)


def _assert_transaction_gate_absent(transaction_id: str) -> None:
    if os.path.lexists(RECOVERY_BOOTBLOCK_MARKER):
        raise RuntimeError("Terminaler Recovery-Marker ist noch vorhanden")
    for unit in _recovery_bootblock_units():
        path = _recovery_dropin_path(unit)
        if os.path.lexists(path):
            raise RuntimeError(
                f"Terminales Recovery-Drop-in ist noch vorhanden: {path}"
            )


def _finish_committed_recovery_transaction(
    transaction: ReconstructedRecoveryTransaction,
) -> None:
    """Vollendet ausschließlich den dauerhaft bestätigten neuen Stand."""

    bundle = transaction.bundle
    payload = bundle.journal.payload
    if payload.phase != recovery_journal.PHASE_COMMITTED:
        raise RuntimeError("PostCommit-Abschluss besitzt kein committed Journal")
    current_head = _bound_release_head_commit(
        payload.install_root,
        payload.install_user,
        root_authority=True,
    )
    if not _exact_commit_matches(current_head, payload.target.commit):
        raise RuntimeError(
            "Committed Journal und aktueller Ziel-HEAD widersprechen sich"
        )

    package_receipt = transaction.package_receipt
    safety = transaction.update_safety_contract
    static_gate = transaction.static_bootblock_contract
    postcommit_health_required = True
    if safety is not None:
        _settle_terminal_finalizer_lease(safety)
        if safety.state == "pending":
            safety = _commit_update_safety_receipt(safety)
        elif safety.state != "committed":
            raise RuntimeError("Committed Journal besitzt einen unbekannten Safety-Zustand")
        if isinstance(package_receipt, PreparedPackageReceipt):
            if package_receipt.state == "prepared":
                package_receipt = _commit_prepared_package_receipt(package_receipt)
            elif package_receipt.state != "committed":
                raise RuntimeError("Committed Journal besitzt keinen commitfähigen Paketzustand")
        _finish_committed_update_safety_cleanup(safety, remove_receipt=False)
    elif static_gate is not None:
        _settle_terminal_finalizer_lease(static_gate)
        if isinstance(package_receipt, PreparedPackageReceipt):
            if package_receipt.state == "prepared":
                package_receipt = _commit_prepared_package_receipt(package_receipt)
            elif package_receipt.state != "committed":
                raise RuntimeError("Statischer PostCommit besitzt falschen Paketzustand")
        if package_receipt is None:
            raise RuntimeError("Statischer PostCommit besitzt kein Paket-Receipt")
        package_receipt = _finish_committed_package_gate_cleanup(
            package_receipt
        )
    else:
        _assert_transaction_gate_absent(payload.transaction_id)
        if package_receipt is not None:
            raise RuntimeError("Committed Journal verlor seinen Gatevertrag")
        # Safety-/Paket-Receipt werden erst entfernt, nachdem Apache und der
        # Zielstand erfolgreich geprüft wurden. Fehlen beide bei weiterhin
        # vorhandenem committed Journal, ist deshalb ausschließlich ein durch
        # Stromausfall unterbrochener Artefakt-Cleanup offen. Ohne den bereits
        # verbrauchten Apache-Prestate dürfen weder Dienste noch HTTP erneut
        # interpretiert oder mutiert werden.
        postcommit_health_required = False

    if postcommit_health_required:
        # Nach einem Reboot wurden die Conditions zwar entfernt, systemd
        # startet zuvor blockierte Units aber nicht rückwirkend. Daher wird
        # ausschließlich der gebundene Ziel-Policy-Satz bei Bedarf aktiviert.
        state = _capture_transition_state(expected_role=payload.target.role)
        policy = _read_policy_from_commit(
            payload.install_root,
            payload.target.commit,
            payload.install_user,
            root_authority=True,
        )
        services = _validated_restart_services(policy, state)
        services_healthy = _post_update_healthcheck(
            services=services,
            transition_state=state,
            check_web=False,
            check_http=False,
        )
        if not services_healthy:
            if not _restart_v4_services(
                headless=True,
                services=services,
                transition_state=state,
            ):
                raise RuntimeError(
                    "Committed Zieldienste konnten nicht vollständig aktiviert werden"
                )
            if not _post_update_healthcheck(
                services=services,
                transition_state=state,
                check_web=False,
                check_http=False,
            ):
                raise RuntimeError(
                    "Committed Zieldienste bestanden das Dienst-/HA-Gesundheitsgate nicht"
                )

        apache_was_active = False
        if safety is not None:
            apache_was_active = bool(
                safety.apache_available and safety.apache_was_active
            )
        elif isinstance(package_receipt, PreparedPackageReceipt):
            apache_was_active = bool(
                package_receipt.apache_available
                and package_receipt.apache_was_active
            )
        if not _post_update_healthcheck(
            services=services,
            transition_state=state,
            check_web=True,
            check_http=apache_was_active,
        ):
            raise RuntimeError(
                "Committed Zielstand bestand das Web-/HTTP-Endgate nicht"
            )

    terminal_bundle = _refresh_terminal_recovery_bundle(
        bundle,
        phase=recovery_journal.PHASE_COMMITTED,
    )
    _cleanup_terminal_update_artifacts(
        bundle=terminal_bundle,
        offline_receipt=transaction.offline_receipt,
        overlay_receipt=transaction.overlay_receipt,
        package_receipt=package_receipt,
        update_safety_contract=safety,
        terminal_label="committed neue Stand",
    )


def _terminal_gate_dropin_contracts(
    transaction: ReconstructedRecoveryTransaction,
) -> dict[str, dict[str, object]]:
    """Projiziert den unveränderlichen Gatevertrag für Teilcleanup-Replays."""

    safety = transaction.update_safety_contract
    static_gate = transaction.static_bootblock_contract
    if (safety is None) == (static_gate is None):
        raise RuntimeError("Terminaler Altstart besitzt keinen eindeutigen Gatevertrag")
    contract = safety or static_gate
    identities = {
        unit: (device, inode)
        for unit, device, inode in contract.dropin_identities
    }
    if set(identities) != set(contract.units):
        raise RuntimeError("Terminaler Gatevertrag besitzt keine vollständigen Inodes")
    payload = (
        _render_update_safety_dropin(contract.transaction_id)
        if safety is not None
        else _render_recovery_bootblock_dropin(contract.transaction_id)
    )
    return {
        _recovery_dropin_path(unit): {
            "device": identities[unit][0],
            "inode": identities[unit][1],
            "payload": payload,
            "uid": 0,
            "gid": 0,
            "mode": 0o644,
            "nlink": 1,
        }
        for unit in contract.units
    }


def _finish_rolled_back_recovery_transaction(
    transaction: ReconstructedRecoveryTransaction,
) -> None:
    """Führt nach durable rolled_back ausschließlich den Cleanup fort."""

    if transaction.bundle.journal.payload.phase != recovery_journal.PHASE_ROLLED_BACK:
        raise RuntimeError("Altstands-Cleanup besitzt kein rolled_back Journal")
    terminal_lease = (
        transaction.update_safety_contract
        or transaction.static_bootblock_contract
    )
    if terminal_lease is not None:
        _settle_terminal_finalizer_lease(terminal_lease)
    if transaction.transition_state is not None:
        if (
            transaction.bundle.systemd is None
            or transaction.recovery_inventory is None
        ):
            raise RuntimeError("Terminaler Altstart besitzt kein vollständiges Preimage")
        if os.path.lexists(RECOVERY_BOOTBLOCK_MARKER):
            if transaction.update_safety_contract is not None:
                _clear_update_safety_marker(
                    transaction.update_safety_contract
                )
            else:
                _clear_recovery_bootblock_marker(
                    transaction.static_bootblock_contract
                )

        terminal_journal = recovery_journal.verify_recovery_journal(
            transaction.bundle.journal
        )
        receipt = transaction.bundle.systemd.receipt

        def terminal_start_authorizer(
            transaction_id: str,
            units: tuple[str, ...],
        ) -> bool:
            try:
                current = recovery_journal.verify_recovery_journal(
                    terminal_journal
                )
            except Exception:
                return False
            return bool(
                current.payload.phase == recovery_journal.PHASE_ROLLED_BACK
                and transaction_id == terminal_journal.payload.transaction_id
                and tuple(units) == tuple(item.unit for item in receipt.units)
            )

        recovery_surface_codec.resume_systemd_pre_active_state_after_gate_open(
            receipt,
            gate_dropins=_terminal_gate_dropin_contracts(transaction),
            start_authorizer=terminal_start_authorizer,
        )
        apache = transaction.recovery_inventory.apache_security
        _restore_apache_after_successful_cutover(
            expected_available=apache.apache_available,
            expected_active=apache.apache_was_active,
            expected_unit_file_state=apache.apache_unit_file_state,
        )
    _assert_transaction_gate_absent(
        transaction.bundle.journal.payload.transaction_id
    )
    _cleanup_terminal_update_artifacts(
        bundle=transaction.bundle,
        offline_receipt=transaction.offline_receipt,
        overlay_receipt=transaction.overlay_receipt,
        package_receipt=transaction.package_receipt,
        update_safety_contract=transaction.update_safety_contract,
        terminal_label="wiederhergestellte Altstand",
    )


def _dispatch_persisted_recovery_transaction(
    transaction: ReconstructedRecoveryTransaction,
) -> None:
    payload = transaction.bundle.journal.payload
    phase = payload.phase
    if phase == recovery_journal.PHASE_COMMITTED:
        _finish_committed_recovery_transaction(transaction)
        return
    if phase == recovery_journal.PHASE_ROLLED_BACK:
        _finish_rolled_back_recovery_transaction(transaction)
        return
    if (
        transaction.transition_state is None
        or transaction.install_inventory is None
        or transaction.recovery_inventory is None
    ):
        raise RuntimeError("Nichtterminale Recovery-Projektion ist unvollständig")
    if phase == recovery_journal.PHASE_PREPRODUCT:
        gate_present = (
            transaction.update_safety_contract is not None
            or transaction.static_bootblock_contract is not None
        )
        package_was_authorized = payload.package is not None
        if not gate_present:
            if package_was_authorized:
                raise RuntimeError("Gebundene Paketmutation besitzt kein Recovery-Gate")
            rolled_back = _advance_persistent_recovery_rolled_back(
                transaction.bundle.journal
            )
            transaction = replace(
                transaction,
                bundle=replace(transaction.bundle, journal=rolled_back),
            )
        else:
            recovered = _abort_before_product_mutation(
                repo_dir=payload.install_root,
                state=transaction.transition_state,
                apache_preimage=transaction.recovery_inventory.apache_security,
                transaction_id=payload.transaction_id,
                bootblock_contract=transaction.static_bootblock_contract,
                update_safety_contract=transaction.update_safety_contract,
                overlay_receipt=None,
                package_transaction=(
                    transaction.package_transaction
                    if package_was_authorized
                    else None
                ),
                packages_mutated=package_was_authorized,
                recovery_inventory=transaction.recovery_inventory,
                recovery_journal_contract=transaction.bundle.journal,
                persistent_recovery_transaction=transaction,
            )
            if not recovered:
                raise RuntimeError("Automatischer preproduct-Rücklauf blieb unvollständig")
        _finish_rolled_back_update_cleanup(
            bundle=transaction.bundle,
            offline_receipt=transaction.offline_receipt,
            overlay_receipt=None,
            package_receipt=transaction.package_receipt,
            update_safety_contract=transaction.update_safety_contract,
        )
        return
    if phase == recovery_journal.PHASE_PRODUCT_MUTATING:
        result = _recover_failed_transition(
            repo_dir=payload.install_root,
            install_user=payload.install_user,
            backup_dir=transaction.bundle.context.context.backup.backup_dir,
            old_commit=payload.source.commit,
            git_created=not payload.source.repository_present,
            inventory=transaction.install_inventory,
            recovery_inventory=transaction.recovery_inventory,
            state=transaction.transition_state,
            package_transaction=transaction.package_transaction,
            repo_recovery_contract=transaction.repo_contract,
            backup_receipt=transaction.backup_receipt,
            bootblock_contract=transaction.static_bootblock_contract,
            update_safety_contract=transaction.update_safety_contract,
            recovery_transaction_id=payload.transaction_id,
            quiesced_overlay_receipt=transaction.overlay_receipt,
            recovery_journal_contract=transaction.bundle.journal,
            persistent_recovery_transaction=transaction,
        )
        if not result.recovered:
            raise RuntimeError("Automatischer Vollrücklauf blieb unvollständig")
        _finish_rolled_back_update_cleanup(
            bundle=transaction.bundle,
            offline_receipt=transaction.offline_receipt,
            overlay_receipt=transaction.overlay_receipt,
            package_receipt=transaction.package_receipt,
            update_safety_contract=transaction.update_safety_contract,
        )
        return
    raise RuntimeError(f"Unbekannte Recovery-Journalphase: {phase}")


def _peek_orphan_recovery_mapping(
    path: str,
    *,
    maximum_bytes: int,
) -> dict:
    """Liest nur die drei Anker eines rootgebundenen Parent-Receipts vor.

    Die Vorablesung erteilt keine Autorität. Unmittelbar danach muss der
    jeweilige öffentliche Codec mit den extrahierten Ankern denselben Inode
    kanonisch, xattr-frei und schemaexakt zurückbinden.
    """

    snapshot = recovery_surface_codec.snapshot_bound_file(
        path,
        allow_missing=False,
        expected_uid=0,
        expected_gid=0,
        max_bytes=maximum_bytes,
    )
    payload = snapshot.get("payload")
    identity = tuple(snapshot.get("identity") or ())
    if (
        not isinstance(payload, bytes)
        or len(identity) != 9
        or identity[2] != 0
        or identity[3] != 0
        or identity[4] != 0o600
        or identity[5] != 1
        or identity[6] != len(payload)
        or snapshot.get("mode") != 0o600
        or snapshot.get("sha256") != hashlib.sha256(payload).hexdigest()
    ):
        raise RuntimeError(
            f"Orphan-Receipt besitzt keinen root:root-0600-Einzelinode: {path}"
        )
    try:
        mapping = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Orphan-Receipt ist kein lesbares JSON: {path}") from exc
    if not isinstance(mapping, dict):
        raise RuntimeError(f"Orphan-Receipt besitzt kein JSON-Objekt: {path}")
    return mapping


def _cleanup_prejournal_construction_prefix(
    *,
    requested_install_root: str,
) -> bool:
    """Bereinigt nur den construction-autorisierten Parent-Aufbau."""

    construction = prejournal_codec.read_prejournal_construction(
        allow_missing=True
    )
    if construction is None:
        return False
    construction = prejournal_codec.verify_prejournal_construction(
        construction
    )
    receipt = construction.receipt
    requested = os.path.realpath(os.path.abspath(requested_install_root))
    if requested != receipt.install_root:
        raise RuntimeError(
            "[E3DC-UPD-PREJOURNAL-INSTANCE-001] Der unterbrochene "
            f"Parent-Aufbau gehört zu {receipt.install_root}, dieser Aufruf "
            f"zu {requested}. Lösung: Starte denselben Updatebefehl für "
            "die zuerst genannte Instanz; bei zwei Instanzen keine "
            "Recovery-Datei löschen"
        )
    stable_manifest, manifest_sha256 = _read_stable_verified_backup_manifest(
        receipt.backup_dir
    )
    if (
        str(stable_manifest.get("backup_id") or "")
        != receipt.full_backup.backup_id
        or manifest_sha256 != receipt.full_backup.manifest_sha256
    ):
        raise RuntimeError(
            "[E3DC-UPD-PREJOURNAL-BACKUP-001] Das verifizierte Vollbackup "
            "widerspricht dem Construction-Receipt. Lösung: Backup nicht "
            "verschieben oder löschen; prüfe dessen Manifest und starte "
            "danach denselben Updatebefehl erneut"
        )

    context_path = recovery_context_codec.RECOVERY_CONTEXT_PATH
    surface_path = recovery_surface_codec.RECOVERY_SURFACE_RECEIPT_PATH
    systemd_path = recovery_surface_codec.SYSTEMD_RECOVERY_RECEIPT_PATH
    has_context = os.path.lexists(context_path)
    has_surface = os.path.lexists(surface_path)
    has_systemd = os.path.lexists(systemd_path)
    if (has_systemd and not has_surface) or (
        has_context and not (has_surface and has_systemd)
    ):
        raise RuntimeError(
            "[E3DC-UPD-PREJOURNAL-PREFIX-001] Parent-Belege verletzen die "
            "Construction-Reihenfolge. Lösung: Keine Datei löschen; prüfe "
            f"sudo stat {context_path} {surface_path} {systemd_path} und "
            "das Updatejournal"
        )

    surface_binding = None
    if has_surface:
        surface_binding = (
            recovery_surface_codec.read_recovery_surface_receipt(
                receipt_path=surface_path,
                expected_transaction_id=receipt.transaction_id,
                expected_install_root=receipt.install_root,
                expected_full_backup_id=receipt.full_backup.backup_id,
            )
        )

    systemd_binding = None
    if has_systemd:
        systemd_mapping = _peek_orphan_recovery_mapping(
            systemd_path,
            maximum_bytes=(
                recovery_surface_codec.MAX_SYSTEMD_RECOVERY_RECEIPT_BYTES
            ),
        )
        raw_units = systemd_mapping.get("units")
        if not isinstance(raw_units, list):
            raise RuntimeError(
                "Construction-systemd-Receipt besitzt keine Unitliste"
            )
        expected_units = tuple(
            str(item.get("unit") or "")
            for item in raw_units
            if isinstance(item, dict)
        )
        if len(expected_units) != len(raw_units):
            raise RuntimeError(
                "Construction-systemd-Receipt besitzt ungültige Uniteinträge"
            )
        systemd_binding = (
            recovery_surface_codec.read_systemd_recovery_receipt(
                receipt_path=systemd_path,
                expected_transaction_id=receipt.transaction_id,
                expected_install_root=receipt.install_root,
                expected_full_backup_id=receipt.full_backup.backup_id,
                expected_units=expected_units,
                expected_unit_root=str(
                    systemd_mapping.get("unit_root") or ""
                ),
            )
        )

    context_binding = None
    if has_context:
        context_binding = recovery_context_codec.read_recovery_context(
            expected_transaction_id=receipt.transaction_id,
            expected_install_root=receipt.install_root,
            expected_full_backup_id=receipt.full_backup.backup_id,
            path=context_path,
        )
        context = context_binding.context
        if (
            surface_binding is None
            or systemd_binding is None
            or context.surface_receipt
            != _context_receipt_reference(surface_binding)
            or context.systemd_receipt
            != _context_receipt_reference(systemd_binding)
            or context.install_user != receipt.install_user
            or context.target.commit != receipt.target.commit
            or context.target.tag != receipt.target.tag
            or context.target.role != receipt.target.role
            or context.backup.backup_dir != receipt.backup_dir
            or context.backup.manifest_sha256
            != receipt.full_backup.manifest_sha256
            or context.source.old_commit != receipt.source.commit
            or context.source.bootstrap_without_git
            == receipt.source.repository_present
            or context.source.bootstrap_rebuild_git
            != receipt.source.repository_rebuild_required
        ):
            raise RuntimeError(
                "Construction-Receipt und Recovery-Kontext widersprechen sich"
            )

    forbidden_paths = (
        os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            PREPARED_PACKAGE_RECEIPT_NAME,
        ),
        os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            UPDATE_SAFETY_RECEIPT_NAME,
        ),
        os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            QUIESCED_OVERLAY_RECEIPT_NAME,
        ),
        RECOVERY_BOOTBLOCK_MARKER,
    )
    finalizer_contract = RecoveryBootblockContract(
        units=(),
        created_directories=(),
        transaction_id=receipt.transaction_id,
        dropin_identities=(),
    )

    def revalidate_before_mutation() -> None:
        """Bestätigt die vollständige Nichtmutationsautorität just in time."""

        prejournal_codec.verify_prejournal_construction(construction)
        forbidden = tuple(
            path for path in forbidden_paths if os.path.lexists(path)
        )
        runtime_path = f"/run/{finalizer_contract.runtime_directory}"
        if os.path.lexists(runtime_path):
            forbidden += (runtime_path,)
        if os.path.lexists(finalizer_contract.token_path):
            forbidden += (finalizer_contract.token_path,)
        if forbidden:
            raise RuntimeError(
                "[E3DC-UPD-PREJOURNAL-MUTATION-001] Der reine Parent-Aufbau "
                "besitzt bereits Mutationsartefakte: "
                + ", ".join(forbidden)
                + ". Lösung: Dateien und Dienste unverändert lassen und das "
                "Updatejournal prüfen"
            )
        _assert_no_same_transaction_finalizer_processes(finalizer_contract)
        _assert_no_recovery_bootblock_dropins()
        if recovery_journal.read_recovery_journal(
            allow_missing=True
        ) is not None:
            raise RuntimeError(
                "Master-Journal erschien während des Construction-Cleanups"
            )

    # Der Construction-Pfad liegt vollständig vor jeder Diensteruhe und
    # Produktmutation. Der Watchdog-Latch darf deshalb gelöst werden.
    revalidate_before_mutation()
    _set_watchdog_update_pause(False, reason="prejournal-construction-cleanup")
    if context_binding is not None:
        revalidate_before_mutation()
        recovery_context_codec.remove_recovery_context(context_binding)
    if systemd_binding is not None:
        revalidate_before_mutation()
        recovery_surface_codec.remove_systemd_recovery_receipt(systemd_binding)
    if surface_binding is not None:
        revalidate_before_mutation()
        recovery_surface_codec.remove_recovery_surface_receipt(surface_binding)
    revalidate_before_mutation()
    prejournal_codec.remove_prejournal_construction(construction)
    remaining = tuple(
        path
        for path in (
            prejournal_codec.PREJOURNAL_CONSTRUCTION_PATH,
            context_path,
            systemd_path,
            surface_path,
        )
        if os.path.lexists(path)
    )
    if remaining:
        raise RuntimeError(
            "Construction-Cleanup blieb unvollständig: "
            + ", ".join(remaining)
        )
    print(
        "[OK] Unterbrochener Parent-Aufbau wurde automatisch bereinigt; "
        "das verifizierte Vollbackup bleibt erhalten."
    )
    return True


def _cleanup_preproduct_orphan_receipt_prefix(
    *,
    requested_install_root: str,
) -> bool:
    """Räumt ausschließlich den mutationsfreien Parent-Präfix vor Journal auf.

    Die Erzeugungsreihenfolge ist Surface -> systemd -> Context -> Journal ->
    Gate. Ohne Journal ist nur Surface bzw. Surface+systemd eindeutig vor der
    Context-/Journal-Grenze unterbrochen. Ein vollständiger Parent-Dreiersatz
    ist ohne Master-Journal phasenambig und bleibt deshalb unverändert.
    """

    context_path = recovery_context_codec.RECOVERY_CONTEXT_PATH
    surface_path = recovery_surface_codec.RECOVERY_SURFACE_RECEIPT_PATH
    systemd_path = recovery_surface_codec.SYSTEMD_RECOVERY_RECEIPT_PATH
    has_context = os.path.lexists(context_path)
    has_surface = os.path.lexists(surface_path)
    has_systemd = os.path.lexists(systemd_path)
    if not (has_context or has_surface or has_systemd):
        return False
    if not has_surface or (has_context and not has_systemd):
        raise RuntimeError(
            "[E3DC-UPD-ORPHAN-CONTEXT-002] Recovery-Begleiter bilden keinen "
            "zulässigen Vorbereitungspräfix. Lösung: Keine Datei löschen; "
            "führe `sudo stat -c '%U:%G %a %h %s %n' "
            f"{context_path} {surface_path} {systemd_path}` aus und prüfe "
            "danach `sudo journalctl -b -u 'e3dc-*update*' --no-pager`"
        )
    if has_context:
        raise RuntimeError(
            "[E3DC-UPD-ORPHAN-JOURNAL-AMBIGUOUS-003] Surface-, systemd- "
            "und Context-Parent sind vollständig, aber das richtungsgebende "
            "Master-Journal fehlt. Dieser Zustand darf weder als Alt- noch "
            "als Neustand geraten werden. Lösung: Keine Recovery-Datei und "
            "keinen systemd-Drop-in löschen; führe `sudo stat -c "
            "'%U:%G %a %h %s %n' "
            f"{context_path} {surface_path} {systemd_path}` und danach "
            "`sudo journalctl -b -u 'e3dc-*update*' --no-pager` aus."
        )

    surface_mapping = _peek_orphan_recovery_mapping(
        surface_path,
        maximum_bytes=recovery_surface_codec.MAX_RECOVERY_SURFACE_RECEIPT_BYTES,
    )
    transaction_id = str(surface_mapping.get("transaction_id") or "")
    install_root = str(surface_mapping.get("install_root") or "")
    full_backup_id = str(surface_mapping.get("full_backup_id") or "")
    surface_binding = recovery_surface_codec.read_recovery_surface_receipt(
        receipt_path=surface_path,
        expected_transaction_id=transaction_id,
        expected_install_root=install_root,
        expected_full_backup_id=full_backup_id,
    )
    requested = os.path.realpath(os.path.abspath(requested_install_root))
    if requested != surface_binding.receipt.install_root:
        raise RuntimeError(
            "[E3DC-UPD-ORPHAN-INSTANCE-001] Der mutationsfreie "
            f"Recovery-Präfix gehört zu {surface_binding.receipt.install_root}, "
            f"dieser Aufruf zu {requested}. Lösung: Starte denselben "
            "Updatebefehl für die zuerst genannte Instanz; bei zwei Instanzen "
            "keine Recovery-Datei löschen"
        )

    systemd_binding = None
    if has_systemd:
        systemd_mapping = _peek_orphan_recovery_mapping(
            systemd_path,
            maximum_bytes=recovery_surface_codec.MAX_SYSTEMD_RECOVERY_RECEIPT_BYTES,
        )
        raw_units = systemd_mapping.get("units")
        if not isinstance(raw_units, list):
            raise RuntimeError("Orphan-systemd-Receipt besitzt keine Unitliste")
        expected_units = tuple(
            str(item.get("unit") or "")
            for item in raw_units
            if isinstance(item, dict)
        )
        if len(expected_units) != len(raw_units):
            raise RuntimeError("Orphan-systemd-Receipt besitzt ungültige Uniteinträge")
        systemd_binding = recovery_surface_codec.read_systemd_recovery_receipt(
            receipt_path=systemd_path,
            expected_transaction_id=transaction_id,
            expected_install_root=install_root,
            expected_full_backup_id=full_backup_id,
            expected_units=expected_units,
            expected_unit_root=str(systemd_mapping.get("unit_root") or ""),
        )

    forbidden_paths = (
        os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            PREPARED_PACKAGE_RECEIPT_NAME,
        ),
        os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            UPDATE_SAFETY_RECEIPT_NAME,
        ),
        os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            QUIESCED_OVERLAY_RECEIPT_NAME,
        ),
        RECOVERY_BOOTBLOCK_MARKER,
    )
    forbidden = tuple(path for path in forbidden_paths if os.path.lexists(path))
    units = set(_recovery_bootblock_units())
    if systemd_binding is not None:
        units.update(item.unit for item in systemd_binding.receipt.units)
    forbidden += tuple(
        _recovery_dropin_path(unit)
        for unit in sorted(units)
        if os.path.lexists(_recovery_dropin_path(unit))
    )
    finalizer_contract = RecoveryBootblockContract(
        units=(),
        created_directories=(),
        transaction_id=transaction_id,
        dropin_identities=(),
    )
    if os.path.lexists(f"/run/{finalizer_contract.runtime_directory}"):
        forbidden += (f"/run/{finalizer_contract.runtime_directory}",)
    if os.path.lexists(finalizer_contract.token_path):
        forbidden += (finalizer_contract.token_path,)
    if forbidden:
        raise RuntimeError(
            "[E3DC-UPD-ORPHAN-MUTATION-001] Parent-Receipts ohne Journal "
            "besitzen bereits Gate-/Paket-/Overlay-Artefakte: "
            + ", ".join(forbidden)
            + ". Lösung: Dienste und Dateien unverändert lassen; führe "
            "`sudo journalctl -b -u 'e3dc-*update*' --no-pager` aus und "
            "verwende nur die dort genannte Recovery-Richtung"
        )
    _assert_no_same_transaction_finalizer_processes(finalizer_contract)
    # Direkt vor der ersten Mutation wird der gesamte systemd-Namensraum
    # descriptorgebunden geprüft. Nicht katalogisierte Alt-/Fremd-Gates dürfen
    # die Parent-Belege niemals erst nach deren Entfernung sichtbar machen.
    _assert_no_recovery_bootblock_dropins()

    # Der alte Produktstand lief während dieses Vorbereitungspräfixes weiter.
    # Der Update-Latch darf daher vor den inodegebundenen Removals fallen; ein
    # weiterer Stromausfall wiederholt lediglich denselben Präfix-Cleanup.
    _set_watchdog_update_pause(False, reason="preproduct-orphan-cleanup")
    if systemd_binding is not None:
        recovery_surface_codec.remove_systemd_recovery_receipt(systemd_binding)
    recovery_surface_codec.remove_recovery_surface_receipt(surface_binding)
    remaining = tuple(
        path
        for path in (context_path, systemd_path, surface_path)
        if os.path.lexists(path)
    )
    if remaining:
        raise RuntimeError(
            "Mutationsfreier Recovery-Präfix blieb nach inodegebundenem Cleanup: "
            + ", ".join(remaining)
        )
    print(
        "[OK] Unterbrochene Update-Vorbereitung ohne Produktänderung wurde "
        "automatisch bereinigt; das verifizierte Backup bleibt erhalten."
    )
    return True


def _assert_legacy_recovery_namespace_exclusive() -> None:
    """Verhindert jede Vermischung alter Safety- und neuer Journalartefakte."""

    new_artifacts = (
        recovery_journal.RECOVERY_JOURNAL_PATH,
        prejournal_codec.PREJOURNAL_CONSTRUCTION_PATH,
        recovery_context_codec.RECOVERY_CONTEXT_PATH,
        recovery_surface_codec.RECOVERY_SURFACE_RECEIPT_PATH,
        recovery_surface_codec.SYSTEMD_RECOVERY_RECEIPT_PATH,
        os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            PREPARED_PACKAGE_RECEIPT_NAME,
        ),
        os.path.join(
            RECOVERY_BOOTBLOCK_STATE_DIR,
            QUIESCED_OVERLAY_RECEIPT_NAME,
        ),
    )
    present = tuple(path for path in new_artifacts if os.path.lexists(path))
    if present:
        command = "sudo stat -c '%U:%G %a %h %s %n' " + " ".join(
            shlex.quote(path) for path in present
        )
        raise ActionableUpdateAbort(
            "E3DC-UPD-LEGACY-MIXED-001",
            "Ein altes 5.4.4/5.4.4a-Safety-Receipt ist mit neuen "
            "Recovery-Artefakten vermischt; eine automatische Richtung wäre unsicher.",
            system_state="UNGEKLÄRT_FAIL_CLOSED",
            target=legacy_safety_codec.LEGACY_UPDATE_SAFETY_RECEIPT_PATH,
            solution=command,
        )


def _resolve_legacy_update_safety_residue(
    *,
    requested_install_root: str,
) -> bool:
    """Löst ausschließlich committed Residuen der direkten Vorgänger."""

    receipt_path = legacy_safety_codec.LEGACY_UPDATE_SAFETY_RECEIPT_PATH
    if not os.path.lexists(receipt_path):
        return False
    _assert_legacy_recovery_namespace_exclusive()
    try:
        bound = _read_bound_legacy_update_safety_receipt()
    except legacy_safety_codec.LegacyUpdateSafetyError as exc:
        raise ActionableUpdateAbort(
            "E3DC-UPD-LEGACY-RECEIPT-001",
            f"Das vorhandene Alt-Receipt ist nicht exakt als veröffentlichter "
            f"5.4.4/5.4.4a-Vertrag beweisbar: {exc.detail}",
            system_state="UNGEKLÄRT_FAIL_CLOSED",
            target=(
                "Alt-Receipt unverändert lassen und dessen genaue Form für "
                "eine geführte Recovery sichern"
            ),
            solution=(
                "sudo stat -c '%U:%G %a %h %s %n' "
                + shlex.quote(receipt_path)
            ),
        ) from exc
    except Exception as exc:
        raise ActionableUpdateAbort(
            "E3DC-UPD-LEGACY-RECEIPT-METADATA-001",
            "Das vorhandene Alt-Receipt konnte nicht mit seinem Root-, "
            f"Datei- und Inodevertrag gelesen werden: {exc}",
            system_state="UNGEKLÄRT_FAIL_CLOSED",
            target=(
                "Alt-Receipt unverändert lassen und seine Metadaten für eine "
                "geführte Recovery sichern"
            ),
            solution=(
                "sudo stat -c '%U:%G %a %h %s %n' "
                + shlex.quote(receipt_path)
            ),
        ) from exc
    if bound is None:
        return False
    receipt, metadata = bound
    try:
        install_root = _bind_legacy_update_backup_instance(
            receipt,
            requested_install_root=requested_install_root,
        )
    except Exception as exc:
        manifest_path = os.path.join(receipt.backup_dir, MANIFEST_NAME)
        raise ActionableUpdateAbort(
            "E3DC-UPD-LEGACY-BACKUP-001",
            f"Das alte Safety-Receipt lässt sich nicht eindeutig seiner "
            f"verifizierten Installation zuordnen: {exc}",
            system_state=(
                "COMMITTED_RECEIPT_INSTANZ_UNGEKLÄRT_FAIL_CLOSED"
                if receipt.state == "committed"
                else "UNGEKLÄRT_FAIL_CLOSED"
            ),
            target=(
                f"Alt-Receipt und Backup unverändert lassen; Zuordnung von "
                f"{receipt.target_tag} zu {requested_install_root} klären"
            ),
            solution=(
                "sudo stat -c '%U:%G %a %h %s %n' "
                + shlex.quote(receipt.backup_dir)
                + " "
                + shlex.quote(manifest_path)
            ),
        ) from exc

    target = f"{receipt.target_tag} ({receipt.target_commit}) in {install_root}"
    if receipt.state == "pending":
        try:
            _verify_update_safety_marker(receipt, expected_present=True)
            _rebind_legacy_update_safety_dropins(
                receipt,
                allow_missing=False,
            )
        except Exception as exc:
            raise ActionableUpdateAbort(
                "E3DC-UPD-LEGACY-PENDING-DRIFT-001",
                f"Ein alter pending Updatevertrag ist vorhanden, sein "
                f"rebootfestes Startgate aber nicht vollständig beweisbar: {exc}",
                system_state="UNGEKLÄRT_FAIL_CLOSED",
                target=(
                    f"Finalizerursache für {target} auslesen; Receipt und "
                    "Backup unverändert lassen"
                ),
                solution=(
                    "sudo journalctl -u "
                    + shlex.quote(receipt.finalizer_unit)
                    + " --no-pager -n 200"
                ),
            ) from exc
        raise ActionableUpdateAbort(
            "E3DC-UPD-LEGACY-PENDING-001",
            "Der Vorgänger hat den Zielstand noch nicht dauerhaft committed. "
            "Ohne dessen Apache- und Dienstpreimage darf der Ziel-Updater "
            "weder Alt- noch Neustand erraten.",
            system_state="UNGEKLÄRT_FAIL_CLOSED",
            target=(
                f"Finalizerursache für {target} auslesen; Receipt und Backup "
                "unverändert lassen"
            ),
            solution=(
                "sudo journalctl -u "
                + shlex.quote(receipt.finalizer_unit)
                + " --no-pager -n 200"
            ),
        )

    try:
        _finish_committed_legacy_update_safety_residue(
            receipt,
            receipt_dev=int(metadata.st_dev),
            receipt_ino=int(metadata.st_ino),
        )
    except ActionableUpdateAbort:
        raise
    except Exception as exc:
        raise ActionableUpdateAbort(
            "E3DC-UPD-LEGACY-CLEANUP-001",
            f"Der alte Zielstand ist committed, aber sein exakter "
            f"Gate-/Finalizer-Cleanup ist noch nicht beweisbar: {exc}",
            system_state="NEUSTAND_COMMITTED",
            target=target,
            solution=(
                "sudo systemctl status --no-pager "
                + shlex.quote(receipt.finalizer_unit)
            ),
        ) from exc
    print(
        f"[OK] Committed Alt-Receipt von {receipt.target_tag} wurde "
        "inodegebunden abgeschlossen."
    )
    return True


def _resume_or_cleanup_recovery_namespace(
    *,
    requested_install_root: str,
) -> bool:
    """Wird genau einmal am echten Update-Einstieg vor jedem neuen Lauf aktiv."""

    journal_contract = recovery_journal.read_recovery_journal(
        allow_missing=True
    )
    if journal_contract is None:
        construction_cleaned = _cleanup_prejournal_construction_prefix(
            requested_install_root=requested_install_root,
        )
        if not construction_cleaned:
            _cleanup_preproduct_orphan_receipt_prefix(
                requested_install_root=requested_install_root,
            )
        # Erst wenn neues Master-Journal und alle neuen Parent-Receipts sicher
        # fehlen, darf der eng begrenzte 5.4.4/5.4.4a-Legacy-Resolver laufen.
        # Er vollendet ausschließlich bereits committed Receipts; pending oder
        # unklare Altzustände bleiben mit konkretem Lösungstext fail-closed.
        _resolve_legacy_update_safety_residue(
            requested_install_root=requested_install_root,
        )
        _assert_no_existing_recovery_bootblock()
        return False
    journal_contract = recovery_journal.verify_recovery_journal(
        journal_contract
    )
    requested = os.path.realpath(os.path.abspath(requested_install_root))
    if requested != journal_contract.payload.install_root:
        raise RuntimeError(
            "[E3DC-UPD-RESIDUE-INSTANCE-001] Die offene Transaktion gehört zu "
            f"{journal_contract.payload.install_root}, dieser Aufruf zu {requested}. "
            "Lösung: Führe denselben Updatebefehl für die zuerst genannte "
            "Instanz zu Ende; bei zwei Instanzen keine Recovery-Datei löschen"
        )
    construction = prejournal_codec.read_prejournal_construction(
        allow_missing=True
    )
    if construction is not None:
        # Construction muss vollständig read-only gegen Journal und Context
        # gebunden werden, bevor die Runtime-Rekonstruktion Gate-Receipts
        # interpretieren oder reaktivieren kann.
        readonly_bundle = _load_persistent_recovery_bundle(journal_contract)
        if readonly_bundle.context is None:
            raise RuntimeError(
                "Master-Journal mit Construction-Receipt besitzt keinen Context"
            )
        construction, journal_contract = _verify_prejournal_against_journal(
            construction,
            journal_contract,
            readonly_bundle.context,
        )
        prejournal_codec.remove_prejournal_construction(construction)
        journal_contract = recovery_journal.verify_recovery_journal(
            journal_contract
        )
        print(
            "[OK] Unterbrochene Journal-Veröffentlichung wurde anhand des "
            "Construction-Receipts automatisch abgeschlossen."
        )
    transaction = _reconstruct_persisted_recovery_transaction(
        journal_contract
    )
    print(
        "[i] Offene Update-Transaktion erkannt; der aktuelle Updater setzt "
        f"Phase {journal_contract.payload.phase} automatisch fort."
    )
    _dispatch_persisted_recovery_transaction(transaction)
    if recovery_journal.read_recovery_journal(allow_missing=True) is not None:
        raise RuntimeError("Master-Journal blieb nach dem automatischen Abschluss erhalten")
    print("[OK] Vorherige Update-Transaktion wurde sicher abgeschlossen.")
    return True


def _prepare_true_update_entry(requested_install_root: str) -> bool:
    """Vollendet Alttransaktionen vor Git-, Policy- und Versionsprüfungen."""

    try:
        # Alte Installationen kennen diesen ausschließlich internen
        # Sicherheitsnamensraum noch nicht. Sein Fehlen ist kein Nutzerfehler:
        # Der aktuelle Root-Updater legt ihn nofollow, root:root und 0700 an,
        # bevor irgendein Receipt gelesen wird. Ein vorhandener unsicherer Pfad
        # bleibt dagegen fail-closed und wird niemals automatisch umgedeutet.
        state_descriptor = _open_recovery_bootblock_state_directory()
        os.close(state_descriptor)
        _resume_or_cleanup_recovery_namespace(
            requested_install_root=requested_install_root,
        )
    except ActionableUpdateAbort as exc:
        _print_actionable_update_abort(exc)
        update_logger.critical(
            "Strukturierter Updateabbruch %s: %s",
            exc.code,
            exc.detail,
        )
        return False
    except Exception as exc:
        print(f"[ABBRUCH] E3DC-UPD-RESIDUE-001: {exc}")
        print(
            "Lösung: Keine Recovery-Datei, kein Overlay und kein Backup "
            "manuell löschen. Behebe ausschließlich die konkret genannte "
            "Pfad-, Rechte-, Speicher- oder Dienstursache und starte danach "
            "denselben Updatebefehl erneut."
        )
        update_logger.critical(
            "Persistente Update-Recovery konnte nicht fortgesetzt werden: %s",
            exc,
        )
        return False
    return True


def _assert_recovery_namespace_read_only_clear() -> None:
    """Reiner Probe-Guard; führt niemals Cleanup oder Dienstaktionen aus."""

    known_paths = (
        recovery_journal.RECOVERY_JOURNAL_PATH,
        prejournal_codec.PREJOURNAL_CONSTRUCTION_PATH,
        recovery_context_codec.RECOVERY_CONTEXT_PATH,
        recovery_surface_codec.RECOVERY_SURFACE_RECEIPT_PATH,
        recovery_surface_codec.SYSTEMD_RECOVERY_RECEIPT_PATH,
        os.path.join(RECOVERY_BOOTBLOCK_STATE_DIR, PREPARED_PACKAGE_RECEIPT_NAME),
        os.path.join(RECOVERY_BOOTBLOCK_STATE_DIR, UPDATE_SAFETY_RECEIPT_NAME),
        os.path.join(RECOVERY_BOOTBLOCK_STATE_DIR, QUIESCED_OVERLAY_RECEIPT_NAME),
        RECOVERY_BOOTBLOCK_MARKER,
    )
    present = tuple(path for path in known_paths if os.path.lexists(path))
    if present or any(
        os.path.lexists(_recovery_dropin_path(unit))
        for unit in _recovery_bootblock_units()
    ):
        raise RuntimeError(
            "Offene Update-Recovery erkannt; der echte Update-Einstieg muss "
            "sie vor der read-only Versionsprobe fortsetzen"
        )


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
        entry_mode = _download_bootstrap_entry_mode(target_install_path)
    except ValueError as exc:
        print(f"[!] {exc}")
        return False
    except RuntimeError as exc:
        print(f"[!] Bootstrap-Einstiegsvertrag ist ungültig: {exc}")
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

    try:
        _assert_recovery_namespace_read_only_clear()
    except Exception as exc:
        print(f"[ABBRUCH] E3DC-UPD-REENTRY-001: {exc}")
        print(
            "Lösung: Starte den Updatebefehl erneut über den normalen Web-, "
            "Bootstrap- oder Konsoleneinstieg. Dort wird die offene "
            "Transaktion vor jedem Git-/Policy-Preflight automatisch fortgesetzt. "
            "Keine Recovery-Datei, kein Overlay und kein Backup manuell löschen."
        )
        update_logger.critical(
            "Transaktionskern wurde mit offenem Recovery-Namensraum betreten: %s",
            exc,
        )
        return False

    if entry_mode == "regular":
        try:
            current, probe_errors = _probe_regular_download_bootstrap_current(
                repo_dir=str(target_install_path),
                target_commit=str(expected_sha),
                target_tag=str(target_tag),
                expected_role=str(expected_ha_role),
            )
        except Exception as exc:
            current = False
            probe_errors = (f"Regular-Probe nicht eindeutig: {exc}",)
        if current:
            print(f"[OK] Du bist auf dem neuesten Stand: {expected_sha}.")
            print(
                "    Ziel-Release, Produkt-/Webprojektion und erwartete Dienste "
                "sind exakt gebunden. Kein Backup und kein Dienststopp erforderlich."
            )
            return UPDATE_ALREADY_CURRENT
        print("[i] Regular-Probe verlangt eine vollständige Rettungstransaktion.")
        for error in probe_errors:
            print(f"    - {error}")
        print(
            "    Derselbe commitgebundene Ziel-Updater fährt mit Backup, "
            "Diensteruhe und vollständiger Projektion fort."
        )
        # Ab hier gelten bewusst die bereits bestehenden vollständigen
        # Rettungssemantiken. Kein nachgelagerter Finalizer darf den
        # read-only Regular-Probe-Modus als eigene Autorität erben.
        os.environ["E3DC_BOOTSTRAP_ENTRY_MODE"] = "rescue"

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
        bootstrap_git_root_authority = bool(
            target_install_path and bootstrap_without_git
        )
        bound_install_user = get_install_user()
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
        recovery_inventory = _capture_recovery_surface(
            state,
            bound_install_user,
        )
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

    # Alle externen Ziel- und Paketentscheidungen werden bei laufendem Altstand
    # abgeschlossen. Nach dem späteren Freeze sind nur noch lokale Readbacks,
    # lokale Paketartefakte und Dateisystemumschaltungen zulässig.
    offline_preparation_plan = None
    offline_package_receipt = None
    try:
        prepared_install_user = get_install_user()
        if prepared_install_user != bound_install_user:
            raise RuntimeError(
                "Installationsbenutzer driftete seit der Instanzbindung"
            )
        prepared_source_repo = (
            os.path.abspath(os.path.dirname(INSTALLER_DIR))
            if target_install_path
            else repo_dir
        )
        prepared_root_authority = bool(target_install_path)
        if target_install_path:
            prepared_target_commit = _validate_full_commit(str(expected_sha or ""))
        elif verified_commit:
            prepared_target_commit = verified_commit
        else:
            prepared_target_commit = _fetch_target_commit(
                repo_dir,
                prepared_install_user,
                target_tag,
            )
        prepared_policy = _read_policy_from_commit(
            prepared_source_repo,
            prepared_target_commit,
            prepared_install_user,
            **_root_git_call_kwargs(prepared_root_authority),
        )
        prepared_requested_tag = target_tag or verified_tag
        prepared_bound_target_tag = _validate_target_release(
            prepared_policy,
            prepared_source_repo,
            prepared_target_commit,
            prepared_requested_tag,
            prepared_install_user,
            **_root_git_call_kwargs(prepared_root_authority),
        )
        if verified_tag and prepared_bound_target_tag != verified_tag:
            raise RuntimeError(
                "Vorbereiteter Ziel-Tag driftete gegenüber dem Ziel-Updater-Handoff"
            )
        if target_tag and not _target_tag_authorized(
            target_tag,
            policy_repo=repo_dir,
            target_commit=prepared_target_commit,
            expected_release_sha=expected_sha,
            install_user=prepared_install_user,
            bootstrap_runner_repo=(
                prepared_source_repo if target_install_path else None
            ),
        ):
            raise RuntimeError(
                "Release-Tag ist nicht durch exakte Policy-/SHA-Bindung autorisiert"
            )
        _validated_restart_services(prepared_policy, state)
        prepared_watchdog_runtime_required = _watchdog_runtime_venv_required(state)
        prepared_package_transaction = _capture_package_transaction(
            prepared_policy,
            prepared_install_user,
            allow_missing_venv=True,
            require_runtime_venv=prepared_watchdog_runtime_required,
        )
        prepared_venv_state, _prepared_venv_path = _finalizer_venv_contract(
            prepared_package_transaction
        )
        offline_preparation_plan = create_preparation_plan(
            recovery_transaction_id,
            apt_packages=prepared_package_transaction.apt_requested,
            pip_packages=prepared_package_transaction.pip_requested,
            expected_venv_state=(
                prepared_venv_state
                if prepared_venv_state in {"present", "missing"}
                else "present"
            ),
            download_python=_trusted_system_python(),
        )
        offline_package_receipt = execute_preparation(
            offline_preparation_plan,
            _run_argv,
        )
        if prepared_package_transaction.pip_requested:
            offline_package_receipt = materialize_wheel_mirror(
                offline_package_receipt
            )
        offline_package_receipt = parse_offline_package_receipt(
            serialize_offline_package_receipt(offline_package_receipt)
        )
        prepared_package_transaction = _bind_package_transaction_to_offline_receipt(
            prepared_package_transaction,
            offline_package_receipt,
        )
    except Exception as exc:
        cleanup_error = None
        try:
            if offline_package_receipt is not None:
                cleanup_offline_package_artifacts(offline_package_receipt)
            elif offline_preparation_plan is not None:
                cleanup_offline_cache(offline_preparation_plan.cache)
        except Exception as cleanup_exc:
            cleanup_error = cleanup_exc
        if isinstance(exc, OfflinePreflightError):
            print(str(exc))
        else:
            print(f"[ABBRUCH] E3DC-UPD-PREPARE: {exc}")
            print(
                "Lösung: Das laufende System wurde nicht gestoppt und keine "
                "Produktdatei verändert. Prüfe Netzwerk, Paketquellen und freien "
                "Speicher; behebe die genannte Ursache und starte denselben "
                "Updatebefehl erneut."
            )
        if cleanup_error is not None and offline_preparation_plan is not None:
            print(
                "[HINWEIS] Der nicht verwendete Offline-Cache blieb zur sicheren "
                f"Prüfung erhalten: {offline_preparation_plan.cache.root}"
            )
        update_logger.error("Release-Vorbereitung vor Dienststopp fehlgeschlagen: %s", exc)
        return False

    repo_recovery_contract = None
    backup_receipt = None
    full_backup_manifest = None
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
            _cleanup_terminal_offline_package_receipt(
                offline_package_receipt,
                terminal_state="unveränderte Altstand",
            )
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
        _cleanup_terminal_offline_package_receipt(
            offline_package_receipt,
            terminal_state="unveränderte Altstand",
        )
        return False

    target_release_space = None
    try:
        if offline_package_receipt is None:
            raise RuntimeError("Offline-Paketreceipt fehlt vor der Speicherplatzprüfung")
        target_release_space = _estimate_target_release_space(
            source_repo=prepared_source_repo,
            target_commit=prepared_target_commit,
            install_user=prepared_install_user,
            root_authority=prepared_root_authority,
        )
        full_backup_estimate = estimate_full_backup_size(repo_dir)
        overlay_estimate = estimate_quiesced_overlay_size(repo_dir)
        _require_update_transaction_space(
            repo_dir=repo_dir,
            backup_collection=backup_collection,
            offline_receipt=offline_package_receipt,
            target_space=target_release_space,
            full_backup_bytes=full_backup_estimate.total_bytes,
            overlay_bytes=overlay_estimate.total_bytes,
            bootstrap_without_git=bootstrap_without_git,
            phase_label="Speicherplatzprüfung vor dem Vollbackup",
        )
        print("  [OK] Dateisystemgruppierter Speicherplatz vor dem Backup bestätigt.")
    except Exception as exc:
        if isinstance(exc, OfflinePreflightError):
            print(str(exc))
        else:
            print(f"[ABBRUCH] E3DC-UPD-DISK-001")
            print(
                "Was ist passiert: Der vollständige Speicherbedarf für Backup, "
                f"Produktbaum und Webprojektion ist nicht sicher ermittelbar: {exc}"
            )
            print(
                "Lösung: Prüfe Installations-, Backup-, Cache- und Webpfad mit "
                "findmnt -T und df -h. Korrigiere fehlende Mounts, Symlinks oder "
                "unsichere Dateitypen und starte denselben Updatebefehl erneut; "
                "es wurden noch keine Dienste gestoppt."
            )
        update_logger.error("Speicherplatzgate vor Vollbackup fehlgeschlagen: %s", exc)
        _cleanup_terminal_offline_package_receipt(
            offline_package_receipt,
            terminal_state="unveränderte Altstand",
        )
        return False

    _enable_watchdog_update_pause(transition_name)
    print("\n[->] Erstelle vollstaendiges externes, verifiziertes Backup...")

    def freeze_backup_receipt(backup_path, verified_manifest):
        nonlocal backup_receipt, full_backup_manifest
        if full_backup_manifest is not None or backup_receipt is not None:
            raise RuntimeError("Backup-Receipt-Callback besitzt keinen eindeutigen Zustand")
        full_backup_manifest = dict(verified_manifest)
        if repo_recovery_contract is not None:
            backup_receipt = _capture_recovery_backup_receipt(
                backup_path,
                verified_manifest,
                repo_recovery_contract,
                recovery_transaction_id,
            )

    try:
        backup_dir = backup_current_version(
            install_path=repo_dir,
            verified_pre_chown_callback=freeze_backup_receipt,
        )
    except Exception as exc:
        backup_dir = None
        update_logger.error(f"Backup vor Release-Wechsel fehlgeschlagen: {exc}")
    if not backup_dir or full_backup_manifest is None or (
        repo_recovery_contract is not None and backup_receipt is None
    ):
        print("[!] Backup fehlgeschlagen; Release-Wechsel hart abgebrochen.")
        _cleanup_terminal_offline_package_receipt(
            offline_package_receipt,
            terminal_state="unveränderte Altstand",
        )
        _set_watchdog_update_pause(False, reason=transition_name)
        return False
    full_backup_id = str(full_backup_manifest.get("backup_id") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", full_backup_id):
        print("[!] Backup-Manifest besitzt keine gebundene Backup-ID; Update abgebrochen.")
        _cleanup_terminal_offline_package_receipt(
            offline_package_receipt,
            terminal_state="unveränderte Altstand",
        )
        _set_watchdog_update_pause(False, reason=transition_name)
        return False

    persistent_recovery_bundle = None
    try:
        persistent_recovery_bundle = _persist_preproduct_recovery_bundle(
            transaction_id=recovery_transaction_id,
            repo_dir=repo_dir,
            install_user=bound_install_user,
            old_commit=old_commit,
            bootstrap_rebuild_git=bootstrap_rebuild_git,
            target_commit=prepared_target_commit,
            target_tag=prepared_bound_target_tag,
            state=state,
            inventory=inventory,
            recovery_inventory=recovery_inventory,
            backup_dir=backup_dir,
            full_backup_manifest=full_backup_manifest,
            repo_recovery_contract=repo_recovery_contract,
            backup_receipt=backup_receipt,
        )
        print("  [OK] Rebootfester Recovery-Kontext und Master-Journal bestätigt.")
    except BaseException as exc:
        print(f"[ABBRUCH] E3DC-UPD-JOURNAL-001: {exc}")
        print(
            "Lösung: Das alte System läuft weiter. Keine Recovery-Datei unter "
            f"{RECOVERY_BOOTBLOCK_STATE_DIR} manuell löschen; starte denselben "
            "Updatebefehl erneut. Der aktuelle Updater wertet den vorhandenen "
            "Transaktionsstand automatisch aus."
        )
        update_logger.error("Recovery-Journal konnte nicht sicher gebunden werden: %s", exc)
        _set_watchdog_update_pause(False, reason=transition_name)
        return False

    install_user = None
    sealed_storage_payloads = None
    sealed_storage_expected_dropins = None
    storage_unit_promoted = False
    storage_promotion_state_uncertain = False
    bootblock_contract = None
    update_safety_contract = None
    quiesced_overlay_dir = None
    quiesced_overlay_receipt = None
    planned_overlay_dir = _quiesced_overlay_path(
        backup_dir,
        recovery_transaction_id,
    )

    # Nach dem verifizierten Vollbackup wird zuerst die rebootfeste
    # Startsperre gebunden. Erst danach darf ein lokales Paketkommando einen
    # globalen Systemzustand verändern; die Alt-Dienste laufen bis zum späteren
    # kurzen Cutover weiter.
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
                apache_preimage=recovery_inventory.apache_security,
                recovery_journal_contract=persistent_recovery_bundle.journal,
            )
            update_safety_contract = _arm_update_safety_contract(
                update_safety_contract
            )
        except BaseException as exc:
            print(f"[ABBRUCH] E3DC-UPD-BOOTBLOCK-001: {exc}")
            print(
                "Lösung: Keine Produktdatei wurde ersetzt. Dienste nicht "
                "manuell starten oder Sicherungen löschen; prüfe das "
                "Updatejournal und starte danach denselben Updatebefehl erneut."
            )
            if update_safety_contract is not None:
                _enforce_update_safety_fail_closed(
                    update_safety_contract,
                    repo_dir=repo_dir,
                )
            _set_watchdog_update_pause(False, reason=transition_name)
            return False
    else:
        try:
            bootblock_contract = _arm_persistent_recovery_bootblock(
                transaction_id=recovery_transaction_id,
                recovery_journal_contract=persistent_recovery_bundle.journal,
            )
        except RecoveryBootblockArmError as exc:
            bootblock_contract = exc.contract
            print(
                "[ABBRUCH] E3DC-UPD-BOOTBLOCK-001: Der rebootfeste "
                "Startsperrvertrag blieb unvollständig."
            )
            print(
                "Lösung: Dienste nicht manuell starten; prüfe "
                "sudo systemctl status --no-pager e3dc-storage-manager.service "
                "und das Updatejournal."
            )
            _enforce_fail_closed_after_recovery_failure(
                bootblock_contract,
                recovery_transaction_id=recovery_transaction_id,
                recovery_journal_contract=persistent_recovery_bundle.journal,
            )
            return False
        except Exception as exc:
            print(f"[ABBRUCH] E3DC-UPD-BOOTBLOCK-001: {exc}")
            print(
                "Lösung: Dienste nicht manuell starten; prüfe das Updatejournal "
                "und sudo systemctl status --no-pager e3dc-storage-manager.service."
            )
            _enforce_fail_closed_after_recovery_failure(
                recovery_transaction_id=recovery_transaction_id,
                recovery_journal_contract=persistent_recovery_bundle.journal,
            )
            return False

    package_transaction = prepared_package_transaction
    prepared_package_state = None
    prepared_package_receipt = None
    packages_mutated = False
    try:
        if offline_package_receipt is None:
            raise RuntimeError("Versiegeltes Offline-Paketreceipt fehlt nach dem Vollbackup")
        prepared_package_receipt = _write_applying_prepared_package_receipt(
            transaction_id=recovery_transaction_id,
            install_root=repo_dir,
            full_backup_id=full_backup_id,
            package_transaction=package_transaction,
            target_commit=prepared_target_commit,
            target_tag=prepared_bound_target_tag,
            role=state.ha_role,
            apache_preimage=recovery_inventory.apache_security,
            static_recovery_contract_json=(
                _serialize_recovery_bootblock_contract(bootblock_contract)
                if isinstance(bootblock_contract, RecoveryBootblockContract)
                else ""
            ),
        )
        if persistent_recovery_bundle is None:
            raise RuntimeError("Master-Journal ging vor der Paketbindung verloren")
        persistent_recovery_bundle = _bind_persistent_recovery_package_safety(
            persistent_recovery_bundle,
            package_receipt=prepared_package_receipt,
            update_safety_contract=update_safety_contract,
            static_bootblock_contract=(
                bootblock_contract
                if isinstance(bootblock_contract, RecoveryBootblockContract)
                else None
            ),
        )
        # Auch ein fehlgeschlagenes APT-Kommando kann bereits Maintainer-
        # Skripte ausgeführt haben. Der Rücklaufvertrag ist deshalb vor dem
        # ersten lokalen Paketkommando rebootfest und inodegebunden aktiv.
        packages_mutated = True
        (
            package_transaction,
            prepared_package_state,
        ) = _apply_prepared_offline_package_policy(
            package_transaction,
            offline_package_receipt,
        )
        prepared_package_receipt = _replace_prepared_package_receipt(
            prepared_package_receipt,
            prepared_package_state,
        )
        print("  [OK] Pakete vollständig offline vorbereitet und verifiziert.")
    except Exception as exc:
        print(f"[ABBRUCH] E3DC-UPD-OFFLINE-APPLY: {exc}")
        try:
            (
                prepared_package_receipt,
                recovery_package_transaction,
            ) = _bind_package_apply_failure_recovery(
                error=exc,
                receipt=prepared_package_receipt,
                packages_mutated=packages_mutated,
                expected_transaction_id=recovery_transaction_id,
                expected_install_root=repo_dir,
                expected_full_backup_id=full_backup_id,
                expected_package_transaction=package_transaction,
            )
            recovered = _abort_before_product_mutation(
                repo_dir=repo_dir,
                state=state,
                apache_preimage=recovery_inventory.apache_security,
                transaction_id=recovery_transaction_id,
                bootblock_contract=bootblock_contract,
                update_safety_contract=update_safety_contract,
                overlay_receipt=None,
                package_transaction=recovery_package_transaction,
                packages_mutated=packages_mutated,
                recovery_inventory=recovery_inventory,
                recovery_journal_contract=persistent_recovery_bundle.journal,
            )
        except Exception as recovery_binding_exc:
            recovered = False
            update_logger.critical(
                "Paket-Receipt konnte für den Rücklauf nicht gebunden werden: %s",
                recovery_binding_exc,
            )
        if recovered:
            try:
                persistent_recovery_bundle = _finish_rolled_back_update_cleanup(
                    bundle=persistent_recovery_bundle,
                    offline_receipt=offline_package_receipt,
                    overlay_receipt=None,
                    package_receipt=prepared_package_receipt,
                    update_safety_contract=update_safety_contract,
                )
            except Exception as cleanup_exc:
                recovered = False
                update_logger.critical(
                    "Terminaler Paket-Rücklauf-Cleanup blieb unvollständig: %s",
                    cleanup_exc,
                )
        if recovered:
            print(
                "Lösung: Der Paket-Ausgangszustand wurde automatisch "
                "wiederhergestellt. Prüfe Paketquellen und freien Speicher und "
                "starte denselben Updatebefehl erneut."
            )
            _set_watchdog_update_pause(False, reason=transition_name)
        else:
            print(
                "Lösung: Dienste nicht manuell neu starten. Der sichere "
                f"Offline-Cache bleibt unter {offline_package_receipt.cache.root}. "
                "Prüfe das vollständige Updatejournal und den Paketstatus mit "
                "sudo dpkg --audit."
            )
            update_logger.critical(
                "Gate-gebundener Offline-Paketrücklauf blieb unvollständig"
            )
        return False

    try:
        if target_release_space is None or offline_package_receipt is None:
            raise RuntimeError("Gebundener Speicherplatzvertrag fehlt vor der Diensteruhe")
        remaining_overlay_estimate = estimate_quiesced_overlay_size(repo_dir)
        _require_update_transaction_space(
            repo_dir=repo_dir,
            backup_collection=backup_collection,
            offline_receipt=offline_package_receipt,
            target_space=target_release_space,
            full_backup_bytes=0,
            overlay_bytes=remaining_overlay_estimate.total_bytes,
            bootstrap_without_git=bootstrap_without_git,
            phase_label="Restbedarf unmittelbar vor der Diensteruhe",
        )
        print("  [OK] Restbedarf unmittelbar vor dem Dienststopp bestätigt.")
    except Exception as exc:
        if isinstance(exc, OfflinePreflightError):
            print(str(exc))
        else:
            print("[ABBRUCH] E3DC-UPD-DISK-001")
            print(
                "Was ist passiert: Der verbleibende Speicherbedarf vor der "
                f"Diensteruhe ist nicht sicher ermittelbar: {exc}"
            )
            print(
                "Lösung: Prüfe die genannten Pfade mit findmnt -T und df -h. "
                "Korrigiere Speicherplatz, Mount oder Dateityp und starte danach "
                "denselben Updatebefehl erneut."
            )
        update_logger.error("Speicherplatz-Restbedarfsgate fehlgeschlagen: %s", exc)
        try:
            recovery_package_transaction = _package_transaction_from_receipt(
                prepared_package_receipt
            )
            recovered = _abort_before_product_mutation(
                repo_dir=repo_dir,
                state=state,
                apache_preimage=recovery_inventory.apache_security,
                transaction_id=recovery_transaction_id,
                bootblock_contract=bootblock_contract,
                update_safety_contract=update_safety_contract,
                overlay_receipt=None,
                package_transaction=recovery_package_transaction,
                packages_mutated=packages_mutated,
                recovery_inventory=recovery_inventory,
                recovery_journal_contract=persistent_recovery_bundle.journal,
            )
        except Exception as recovery_binding_exc:
            recovered = False
            update_logger.critical(
                "Paket-Receipt konnte am Speicherplatz-Rücklauf nicht gebunden werden: %s",
                recovery_binding_exc,
            )
        if recovered:
            try:
                persistent_recovery_bundle = _finish_rolled_back_update_cleanup(
                    bundle=persistent_recovery_bundle,
                    offline_receipt=offline_package_receipt,
                    overlay_receipt=None,
                    package_receipt=prepared_package_receipt,
                    update_safety_contract=update_safety_contract,
                )
            except Exception as cleanup_exc:
                recovered = False
                update_logger.critical(
                    "Terminaler Speicherplatz-Rücklauf-Cleanup blieb "
                    "unvollständig: %s",
                    cleanup_exc,
                )
        if recovered:
            print(
                "Lösung: Der Paket-Ausgangszustand und die Startsperre wurden "
                "automatisch wiederhergestellt. Gib den ausgewiesenen Speicher "
                "frei und starte denselben Updatebefehl erneut; der reguläre "
                "Cutover wurde nicht begonnen."
            )
            _set_watchdog_update_pause(False, reason=transition_name)
        else:
            print(
                "Lösung: Dienste nicht manuell starten. Prüfe das vollständige "
                "Updatejournal und den Paketstatus mit sudo dpkg --audit."
            )
        return False

    try:
        install_user = get_install_user()
        if install_user != bound_install_user:
            raise RuntimeError(
                "Installationsbenutzer driftete seit der Instanzbindung"
            )
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
        else:
            bootblock_contract = _arm_persistent_recovery_bootblock(
                bootblock_contract,
                transaction_id=recovery_transaction_id,
            )
        _verify_transition_state(state)

        if not _stop_v4_services(V4_SERVICES):
            raise RuntimeError("Sichere Aktorruhe konnte unter Bootblock nicht nachgewiesen werden")
        _assert_strict_update_writer_quiescence(
            repo_dir=repo_dir,
            transaction_id=recovery_transaction_id,
        )
        if sealed_target_updater:
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
    except BaseException as exc:
        print(f"[ABBRUCH] E3DC-UPD-QUIESCE-001: {exc}")
        print(
            "Lösung: Prüfe die im Updatejournal genannte aktive Unit oder "
            "den fremden Writer. Nach automatisch bestätigtem Rücklauf kann "
            "derselbe Updatebefehl erneut gestartet werden."
        )
        try:
            recovery_package_transaction = _package_transaction_from_receipt(
                prepared_package_receipt
            )
            recovered = _abort_before_product_mutation(
                repo_dir=repo_dir,
                state=state,
                apache_preimage=recovery_inventory.apache_security,
                transaction_id=recovery_transaction_id,
                bootblock_contract=bootblock_contract,
                update_safety_contract=update_safety_contract,
                overlay_receipt=None,
                package_transaction=recovery_package_transaction,
                packages_mutated=packages_mutated,
                recovery_inventory=recovery_inventory,
                recovery_journal_contract=persistent_recovery_bundle.journal,
            )
        except Exception as recovery_binding_exc:
            recovered = False
            update_logger.critical(
                "Paket-Receipt konnte am Quiesce-Rücklauf nicht gebunden werden: %s",
                recovery_binding_exc,
            )
        if recovered:
            try:
                persistent_recovery_bundle = _finish_rolled_back_update_cleanup(
                    bundle=persistent_recovery_bundle,
                    offline_receipt=offline_package_receipt,
                    overlay_receipt=None,
                    package_receipt=prepared_package_receipt,
                    update_safety_contract=update_safety_contract,
                )
            except Exception as cleanup_exc:
                recovered = False
                update_logger.critical(
                    "Terminaler Quiesce-Rücklauf-Cleanup blieb unvollständig: %s",
                    cleanup_exc,
                )
        if recovered:
            _set_watchdog_update_pause(False, reason=transition_name)
        return False

    git_created = False
    # Erst der vollständig versiegelte Overlay-Receipt autorisiert die erste
    # Produktmutation. Stop und Apache-Cutover allein verändern keine Nutzdaten.
    mutated = False
    role_anchor_created = False
    target_commit = None
    try:
        _quiesce_apache_for_cutover(recovery_inventory.apache_security)
        (
            quiesced_overlay_dir,
            _overlay_manifest,
            overlay_restore_guard,
        ) = create_quiesced_overlay(
            planned_overlay_dir,
            install_path=repo_dir,
            transaction_id=recovery_transaction_id,
            parent_backup_dir=backup_dir,
            parent_backup_id=str(full_backup_manifest.get("backup_id") or ""),
        )
        quiesced_overlay_receipt = _capture_quiesced_overlay_receipt(
            overlay_dir=quiesced_overlay_dir,
            backup_dir=backup_dir,
            install_root=repo_dir,
            transaction_id=recovery_transaction_id,
            restore_guard=overlay_restore_guard,
        )
        if persistent_recovery_bundle is None:
            raise RuntimeError("Master-Journal ging vor der Produktmutation verloren")
        persistent_recovery_bundle = (
            _advance_persistent_recovery_product_mutating(
                persistent_recovery_bundle,
                quiesced_overlay_receipt,
            )
        )
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
            storage_payloads = sealed_storage_payloads
            storage_expected_dropins = sealed_storage_expected_dropins
        else:
            if not isinstance(bootblock_contract, RecoveryBootblockContract):
                raise RuntimeError("Bootstrap verlor seinen persistenten Bootblockvertrag")
            storage_payloads = _approved_storage_manager_unit_payloads()
            storage_expected_dropins = _persistent_recovery_expected_dropins(
                bootblock_contract,
                selected_units=("e3dc-storage-manager.service",),
            )
        try:
            # Ab hier beginnt die erste Produkt-/Systemmutation. Das reine
            # Stoppen sowie das versiegelte Overlay bleiben bis zu diesem
            # Punkt über den schlanken Vor-Mutations-Rückweg reversibel.
            mutated = True
            storage_unit_promoted = bool(
                _migrate_approved_storage_manager_unit_owner(
                    storage_payloads,
                    install_user=install_user,
                    expected_recovery_dropins=storage_expected_dropins[
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
                    "systemd daemon-reload nach quieszierter Storage-Unitmigration fehlgeschlagen: "
                    + _combined_process_diagnostics(reload_result, maximum=800)
                )
            capture_systemd_service_bundle(
                ("e3dc-storage-manager",),
                expected_recovery_dropins=storage_expected_dropins,
            )
        if sealed_target_updater:
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
            # Der vollständige frische Git-Aufbau bleibt bis in den
            # Ziel-Finalizer root-eigen. Erst dessen kanonische
            # Rechteprojektion übergibt .git und den Produktbaum gemeinsam an
            # den gebundenen Installationsbenutzer.
            _initialize_bootstrap_git(repo_dir, install_user)
            mutated = True
            remote_add = _git_argv(
                repo_dir,
                install_user,
                "remote",
                "add",
                "origin",
                SELFUPDATE_REPO,
                timeout=15,
                **_root_git_call_kwargs(bootstrap_git_root_authority),
            )
            if not remote_add["success"]:
                raise RuntimeError("Git-Origin konnte nicht gesetzt werden: " + remote_add["stderr"].strip())

        _require_bound_origin(
            repo_dir,
            install_user,
            **_root_git_call_kwargs(bootstrap_git_root_authority),
        )

        # Beim Download-Bootstrap stammen Tagobjekt und Commit aus dem bereits
        # vor dem Stop vollständig verifizierten Runner-Checkout. Der Fetch ist
        # absichtlich lokal; GitHub wird im Cutover nicht mehr kontaktiert.
        if target_install_path:
            local_fetch = _git_argv(
                repo_dir,
                install_user,
                "fetch",
                "--no-tags",
                prepared_source_repo,
                f"+refs/tags/{prepared_bound_target_tag}:refs/tags/{prepared_bound_target_tag}",
                timeout=120,
                **_root_git_call_kwargs(bootstrap_git_root_authority),
            )
            if not local_fetch.get("success"):
                raise RuntimeError(
                    "Lokal vorbereitetes Release-Tag konnte nicht in die "
                    "Installation übernommen werden: "
                    + str(local_fetch.get("stderr") or "").strip()
                )

        target_commit = prepared_target_commit
        if expected_sha and not _exact_commit_matches(expected_sha, target_commit):
            raise RuntimeError(
                f"Ziel-SHA weicht von der expliziten Freigabe ab: {target_commit} != {expected_sha}"
            )
        policy = _read_policy_from_commit(
            repo_dir,
            target_commit,
            install_user,
            **_root_git_call_kwargs(bootstrap_git_root_authority),
        )
        if policy != prepared_policy:
            raise RuntimeError("Lokal gelesene Ziel-Policy driftete seit der Vorbereitung")
        bound_target_tag = _validate_local_target_release_binding(
            policy,
            repo_dir,
            target_commit,
            prepared_bound_target_tag,
            install_user,
            **_root_git_call_kwargs(bootstrap_git_root_authority),
        )
        _validated_restart_services(policy, state)
        watchdog_runtime_required = prepared_watchdog_runtime_required
        prepared_package_receipt = _validate_prepared_package_receipt(
            prepared_package_receipt,
            expected_state="prepared",
        )
        if prepared_package_receipt.prepared_state is None:
            raise RuntimeError("Vorbereitetes Paket-Receipt besitzt keinen Postzustand")
        _verify_prepared_package_policy_applied(
            policy,
            install_user,
            expected_transaction_id=recovery_transaction_id,
            prepared=prepared_package_receipt.prepared_state,
        )

        _assert_target_worktree_replaceable(
            repo_dir,
            install_user,
            target_commit,
            **_root_git_call_kwargs(bootstrap_git_root_authority),
        )

        mutated = True
        reset = _git_argv(repo_dir, install_user, "reset", "--hard", target_commit, timeout=120, **_root_git_call_kwargs(bootstrap_git_root_authority))
        if not reset["success"]:
            raise RuntimeError("git reset --hard fehlgeschlagen: " + reset["stderr"].strip())
        new_commit = _resolve_git_commit(
            repo_dir,
            "HEAD",
            install_user,
            **_root_git_call_kwargs(bootstrap_git_root_authority),
        )
        if not new_commit or not _exact_commit_matches(target_commit, new_commit):
            raise RuntimeError("HEAD stimmt nicht exakt mit dem freigegebenen Ziel-SHA ueberein")

        mutated = True
        _normalize_target_finalizer_files(
            repo_dir=repo_dir,
            target_commit=target_commit,
            install_user=install_user,
            **_root_git_call_kwargs(bootstrap_git_root_authority),
        )

        mutated = True
        _invoke_target_finalizer(
            repo_dir=repo_dir,
            target_commit=target_commit,
            target_tag=bound_target_tag,
            state=state,
            package_receipt=prepared_package_receipt,
            recovery_journal_contract=persistent_recovery_bundle.journal,
            apache_preimage=recovery_inventory.apache_security,
            static_bootblock_contract=(
                bootblock_contract if not sealed_target_updater else None
            ),
            update_safety_contract=update_safety_contract,
            explicit_download_bootstrap=bool(target_install_path),
        )
        persistent_recovery_bundle = _refresh_terminal_recovery_bundle(
            persistent_recovery_bundle,
            phase=recovery_journal.PHASE_COMMITTED,
        )
    except BaseException as exc:
        print(f"[!] {transition_name} fehlgeschlagen: {exc}")
        update_logger.error(f"{transition_name} fehlgeschlagen: {exc}")
        try:
            current_recovery_journal = recovery_journal.read_recovery_journal()
        except Exception as journal_error:
            update_logger.critical(
                "Master-Journal ist am Transaktionsfehler unlesbar: %s",
                journal_error,
            )
            print(
                "[ABBRUCH] E3DC-UPD-JOURNAL-READ-001: Der Transaktionsstand "
                "ist nicht sicher lesbar; jede Recoverymutation bleibt gesperrt."
            )
            print(
                "Lösung: Dienste und Recovery-Dateien nicht manuell verändern. "
                "Prüfe sudo stat /var/lib/e3dc-update-safety/recovery-journal.json "
                "und starte denselben Updatebefehl erneut."
            )
            return False
        if (
            current_recovery_journal is None
            or not _same_recovery_journal_transaction_shape(
                current_recovery_journal,
                persistent_recovery_bundle.journal,
            )
        ):
            update_logger.critical(
                "Master-Journal gehört am Transaktionsfehler nicht mehr zur "
                "gebundenen Transaktion"
            )
            return False

        journal_phase = current_recovery_journal.payload.phase
        persistent_recovery_bundle = replace(
            persistent_recovery_bundle,
            journal=current_recovery_journal,
        )
        if journal_phase == recovery_journal.PHASE_COMMITTED:
            update_logger.critical(
                "Zielstand ist committed; Altstand-Rollback bleibt ausdrücklich gesperrt"
            )
            print(
                "[ABBRUCH] E3DC-UPD-APACHE-POSTCOMMIT-001: Der neue Stand ist "
                "dauerhaft übernommen, aber der Web-/Cleanup-Abschluss blieb "
                "unvollständig."
            )
            print(
                "Lösung: Keine Receipt-, Overlay- oder Backup-Datei manuell "
                "löschen. Starte denselben Updatebefehl erneut; der aktuelle "
                "Updater wiederholt den gebundenen Apache-/Cleanup-Abschluss."
            )
            return False
        if journal_phase == recovery_journal.PHASE_ROLLED_BACK:
            try:
                _cleanup_terminal_update_artifacts(
                    bundle=persistent_recovery_bundle,
                    offline_receipt=offline_package_receipt,
                    overlay_receipt=quiesced_overlay_receipt,
                    package_receipt=prepared_package_receipt,
                    update_safety_contract=update_safety_contract,
                    terminal_label="wiederhergestellte Altstand",
                )
                _set_watchdog_update_pause(False, reason=transition_name)
            except Exception as cleanup_error:
                update_logger.critical(
                    "Terminaler rolled_back-Cleanup blieb unvollständig: %s",
                    cleanup_error,
                )
            return False
        if isinstance(exc, UpdateSafetyManagedServiceUnquiescedError):
            update_logger.critical(
                "Managed Finalizer-/Writer-Ruhe ist unbewiesen; "
                "jede Recoverymutation bleibt ausdrücklich gesperrt"
            )
            return False
        recovered = False
        try:
            recovery_package_transaction = (
                _package_transaction_from_receipt(prepared_package_receipt)
                if packages_mutated
                else None
            )
            if journal_phase == recovery_journal.PHASE_PRODUCT_MUTATING:
                recovery_result = _recover_failed_transition(
                    repo_dir=repo_dir,
                    install_user=install_user,
                    backup_dir=backup_dir,
                    old_commit=old_commit,
                    git_created=git_created,
                    inventory=inventory,
                    recovery_inventory=recovery_inventory,
                    state=state,
                    package_transaction=recovery_package_transaction,
                    repo_recovery_contract=repo_recovery_contract,
                    backup_receipt=backup_receipt,
                    bootblock_contract=bootblock_contract,
                    update_safety_contract=update_safety_contract,
                    recovery_transaction_id=recovery_transaction_id,
                    quiesced_overlay_receipt=quiesced_overlay_receipt,
                    recovery_journal_contract=current_recovery_journal,
                )
                recovered = bool(recovery_result)
                bootblock_contract = recovery_result.bootblock_contract
                if recovery_result.recovery_journal_contract is not None:
                    persistent_recovery_bundle = replace(
                        persistent_recovery_bundle,
                        journal=recovery_result.recovery_journal_contract,
                    )
            elif journal_phase == recovery_journal.PHASE_PREPRODUCT:
                recovered = _abort_before_product_mutation(
                    repo_dir=repo_dir,
                    state=state,
                    apache_preimage=recovery_inventory.apache_security,
                    transaction_id=recovery_transaction_id,
                    bootblock_contract=bootblock_contract,
                    update_safety_contract=update_safety_contract,
                    overlay_receipt=None,
                    package_transaction=recovery_package_transaction,
                    packages_mutated=packages_mutated,
                    recovery_inventory=recovery_inventory,
                    recovery_journal_contract=current_recovery_journal,
                )
            else:
                raise RuntimeError(
                    f"Unzulässige Recovery-Journalphase: {journal_phase}"
                )
        except Exception as recovery_binding_exc:
            update_logger.critical(
                "Paket-Receipt konnte am Transaktionsrücklauf nicht gebunden werden: %s",
                recovery_binding_exc,
            )
        if recovered:
            try:
                persistent_recovery_bundle = _finish_rolled_back_update_cleanup(
                    bundle=persistent_recovery_bundle,
                    offline_receipt=offline_package_receipt,
                    overlay_receipt=quiesced_overlay_receipt,
                    package_receipt=prepared_package_receipt,
                    update_safety_contract=update_safety_contract,
                )
                print(
                    "[OK] Ausgangszustand wurde automatisch und verifiziert "
                    "wiederhergestellt."
                )
                _set_watchdog_update_pause(False, reason=transition_name)
            except Exception as cleanup_error:
                recovered = False
                update_logger.critical(
                    "Terminaler Rücklauf-Cleanup blieb unvollständig: %s",
                    cleanup_error,
                )
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
                    recovery_journal_contract=persistent_recovery_bundle.journal,
                )
        return False

    try:
        if update_safety_contract is not None:
            committed_contract = _read_update_safety_contract()
            if (
                committed_contract is None
                or committed_contract.state != "committed"
                or not _same_update_safety_transaction_shape(
                    committed_contract,
                    update_safety_contract,
                )
            ):
                raise RuntimeError(
                    "Äußerer Abschluss besitzt nicht den committed Safety-Beleg"
                )
            _finish_committed_update_safety_cleanup(
                committed_contract,
                remove_receipt=False,
            )
        _cleanup_terminal_update_artifacts(
            bundle=persistent_recovery_bundle,
            offline_receipt=offline_package_receipt,
            overlay_receipt=quiesced_overlay_receipt,
            package_receipt=prepared_package_receipt,
            update_safety_contract=update_safety_contract,
            terminal_label="neue Stand",
        )
    except BaseException as exc:
        print(
            f"[ABBRUCH] E3DC-UPD-POSTCOMMIT-CLEANUP-001: {exc}"
        )
        print(
            "Lösung: Der neue Stand ist dauerhaft übernommen. Keine Receipt-, "
            "Overlay- oder Backup-Datei manuell löschen. Starte denselben "
            "Updatebefehl erneut; der aktuelle Updater setzt ausschließlich "
            "den gebundenen terminalen Cleanup fort."
        )
        update_logger.critical(
            "Committed äußerer Updateabschluss blieb unvollständig: %s",
            exc,
        )
        _set_watchdog_update_pause(False, reason=transition_name)
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
    if not _prepare_true_update_entry(product_root):
        return False
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
    if not _is_docker_environment() and not _prepare_true_update_entry(
        os.path.abspath(str(target_install_path or INSTALL_PATH))
    ):
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
    # Ein bereits installiertes System betritt ohne erneute Bewertung seines
    # heterogenen Altzustands direkt den root-eigenen Dispatcher. Nur wenn der
    # Dispatcher noch gar nicht installiert ist, darf der Erstinstallations-
    # pfad die lokale Zustandsklassifikation ausführen.
    if os.path.lexists(UPDATE_DISPATCHER):
        return _start_background_update_dispatcher()
    return start_installation_or_update(allow_first_install=True)


# Im Docker-Container kein Update-Menueintrag
if not _is_docker_environment():
    register_command('11', 'Installation / Update', update_menu, sort_order=11)
