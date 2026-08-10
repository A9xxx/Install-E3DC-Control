import ast
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
from .backup_integrity import _open_directory_nofollow, _open_regular_file_nofollow
from .utils import cleanup_pycache, ensure_manager_lock_namespace, run_command
from .installer_config import (
    WEB_CONFIG_START_DEFAULTS,
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
WATCHDOG_PAUSE_FILE = '/var/www/html/ramdisk/watchdog.update_pause'
WATCHDOG_GRACE_FILE = '/var/www/html/ramdisk/watchdog.update_grace'
WATCHDOG_POST_UPDATE_GRACE_S = 300

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
    ("/etc/sudoers.d/020_e3dc_services", 0o440, 256 * 1024),
    ("/etc/apache2/sites-available/000-default.conf", 0o644, 256 * 1024),
    (
        "/etc/apache2/conf-available/e3dc-control-access-log.conf",
        0o644,
        64 * 1024,
    ),
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
TARGET_UPDATER_SUCCESS = "E3DC_RELEASE_TARGET_UPDATER_OK"
TARGET_UPDATER_NOOP = "E3DC_RELEASE_TARGET_UPDATER_NOOP"
TARGET_FINALIZER_RELATIVE_FILES = (
    "Installer/__init__.py",
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


def _run_streaming_argv(
    argv,
    *,
    timeout: int | None,
    env=None,
    pass_fds=(),
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
    return _run_argv(
        [
            "sudo",
            "-H",
            "-u",
            str(install_user),
            "/usr/bin/env",
            "GIT_OPTIONAL_LOCKS=0",
            "git",
            "-c",
            f"safe.directory={repo_dir}",
            "-c",
            "core.fileMode=false",
            "-C",
            str(repo_dir),
            *args,
        ],
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
) -> bool:
    """Migriert einen Legacy-Override atomar mit effektivem Unit-Readback."""

    runner = command_runner or run_command
    target = Path(str(override_file or ""))
    legacy_names = ("storage_manager_next.py", "storage_manager_legacy.py")
    maximum_bytes = 256 * 1024
    parent_descriptor = None
    original_payload = None
    original_metadata = None
    preimage_readback = None
    installed_inode = None

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
        try:
            original_payload, original_metadata = _read_named_payload()
        except FileNotFoundError:
            return True
        try:
            source = original_payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Storage-Override ist nicht UTF-8-lesbar") from exc
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
            and installed_inode is not None
        ):
            try:
                current = os.stat(
                    target.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) != installed_inode:
                    raise RuntimeError(
                        "Installierter Storage-Override driftete vor dem Rollback"
                    )
                _atomic_replace(
                    original_payload,
                    original_metadata,
                    _identity(current),
                )
                _daemon_reload()
                restored_payload, restored_metadata = _read_named_payload()
                if (
                    restored_payload != original_payload
                    or restored_metadata.st_uid != original_metadata.st_uid
                    or restored_metadata.st_gid != original_metadata.st_gid
                    or stat.S_IMODE(restored_metadata.st_mode)
                    != stat.S_IMODE(original_metadata.st_mode)
                    or restored_metadata.st_mtime_ns != original_metadata.st_mtime_ns
                ):
                    raise RuntimeError("Storage-Override-Preimage wurde nicht vollständig restauriert")
                if preimage_readback is not None and _unit_readback() != preimage_readback:
                    raise RuntimeError("Effektiver Storage-Writer wich nach Restore vom Preimage ab")
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        detail = str(exc)
        if rollback_errors:
            detail += "; Restore fehlgeschlagen: " + "; ".join(rollback_errors)
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
    inventory = {
        unit
        for unit in (*_catalog_units_strict(), "piguard.service", "e3dc.service")
        if _service_unit_exists(unit)
    }
    activities: dict[str, str] = {}
    for unit in sorted(inventory):
        status = run_command(f"systemctl is-active {unit}", timeout=10)
        activity = status.get("stdout", "").strip().lower()
        if activity not in {"active", "inactive", "failed"}:
            raise RuntimeError(f"Betriebszustand von {unit} ist nicht lesbar")
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


def _ensure_install_center_core_services() -> bool:
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


def _secure_repo_permissions(
    repo_dir: str,
    install_user: str,
    *,
    expected_commit: str | None = None,
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
            os.fsync(temporary_descriptor)
            os.fchown(temporary_descriptor, account.pw_uid, account.pw_gid)
            os.fchmod(temporary_descriptor, expected_mode)
            os.utime(
                temporary_descriptor,
                ns=(source_metadata.st_atime_ns, source_metadata.st_mtime_ns),
            )
            hardened = os.fstat(temporary_descriptor)
            if (
                not stat.S_ISREG(hardened.st_mode)
                or hardened.st_nlink != 1
                or hardened.st_uid != account.pw_uid
                or hardened.st_gid != account.pw_gid
                or stat.S_IMODE(hardened.st_mode) != expected_mode
                or hardened.st_size != source_metadata.st_size
                or _git_blob_oid_from_descriptor(
                    temporary_descriptor,
                    hardened.st_size,
                    expected_oid,
                )
                != expected_oid
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
                or _git_blob_oid_from_descriptor(
                    temporary_descriptor,
                    hardened_after.st_size,
                    expected_oid,
                )
                != expected_oid
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
                        or _git_blob_oid_from_descriptor(
                            descriptor,
                            opened.st_size,
                            expected_oid,
                        )
                        != expected_oid
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
            if (
                not stat.S_ISDIR(before.st_mode)
                or before.st_uid not in (0, account.pw_uid)
            ):
                raise RuntimeError(
                    "Getracktes Produktverzeichnis besitzt unsichere Metadaten: "
                    + (relative_directory or root)
                )
            if before.st_uid != account.pw_uid or before.st_gid != account.pw_gid:
                os.fchown(descriptor, account.pw_uid, account.pw_gid)
            if stat.S_IMODE(before.st_mode) != 0o755:
                os.fchmod(descriptor, 0o755)
            after = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(after.st_mode)
                or (after.st_dev, after.st_ino)
                != (before.st_dev, before.st_ino)
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
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid not in (0, account.pw_uid)
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
                    or opened.st_uid not in (0, account.pw_uid)
                    or (opened.st_dev, opened.st_ino)
                    != (before.st_dev, before.st_ino)
                    or _git_blob_oid_from_descriptor(
                        descriptor,
                        opened.st_size,
                        expected_oid,
                    )
                    != expected_oid
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
                    or _git_blob_oid_from_descriptor(
                        descriptor,
                        after.st_size,
                        expected_oid,
                    )
                    != expected_oid
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
    result = _git_argv(repo_dir, install_user, "remote", "get-url", "origin", timeout=5)
    return result['stdout'].strip() if result['success'] else None


def check_for_updates(repo_dir: str) -> int | None:
    """
    Prueft ob Updates verfuegbar sind.
    Gibt die Anzahl fehlender Commits zurueck, None bei Fehler.
    """
    install_user = get_install_user()
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
    if ensure_manager_lock_namespace() is not True:
        print("  [!] Root-kontrollierter Manager-Locknamespace ist nicht herstellbar.")
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
                ("restart", res),
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
                    f"restart {_command_result_diagnostic(res)}; "
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
    expected_stable = _normalize_release_tag(version)
    if stable != expected_stable:
        raise RuntimeError(
            "Stable-Tag und VERSION des Ziel-Commits stimmen nicht überein"
        )
    if target_tag and stable != target_tag:
        raise RuntimeError(f"Ziel-Tag {target_tag} ist nicht Stable des Ziel-Commits ({stable})")
    # Auch der bereits explizit angeforderte Tag wird im Zielprozess erneut
    # direkt von origin geladen und als annotiertes Tag auf exakt denselben
    # Commit gebunden. Die Vorprüfung des Alt-Updaters ist kein Ersatz dafür.
    storage_ref = f"refs/tags/{stable}"
    refspec = f"+{storage_ref}:{storage_ref}"
    fetched = _git_argv(
        repo_dir,
        install_user,
        "fetch",
        "--no-tags",
        "origin",
        refspec,
        timeout=120,
    )
    if not fetched["success"]:
        raise RuntimeError("Stable-Tag des Ziel-Commits konnte nicht geladen werden")
    object_type = _git_argv(
        repo_dir,
        install_user,
        "cat-file",
        "-t",
        storage_ref,
        timeout=15,
    )
    if not object_type["success"] or object_type["stdout"].strip() != "tag":
        raise RuntimeError("Stable-Tag des Ziel-Commits ist nicht annotiert")
    stable_commit = _resolve_git_commit(repo_dir, storage_ref, install_user)
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
    headless: bool = True,
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

    _announce_finalizer_phase(3, phase_total, "Webroot und Berechtigungen synchronisieren")
    _sync_release_web(
        target_root,
        policy,
        allow_config_bootstrap=state.bootstrap_legacy_config,
    )
    if policy.get("run_permissions", True):
        from .permissions import run_permissions_wizard
        if run_permissions_wizard(headless=True, release_quiesced=True) is False:
            raise RuntimeError("Berechtigungsreparatur fehlgeschlagen")
        _secure_repo_permissions(
            target_root,
            install_user,
            expected_commit=commit,
        )

    from .permissions import (
        ensure_private_ml_model_store,
        harden_web_program_permissions,
        refresh_watchdog_guard_script,
    )
    if not ensure_private_ml_model_store():
        raise RuntimeError("Privater ML-Modellspeicher konnte nicht sicher vorbereitet werden")
    if not harden_web_program_permissions():
        raise RuntimeError("Web-Programmrechte konnten nicht gehärtet werden")
    _announce_finalizer_phase(4, phase_total, "Kernservices und Migrationen vorbereiten")
    if not _ensure_install_center_core_services():
        raise RuntimeError("Kernservice-Installation ist unvollständig")
    if not migrate_storage_manager_next_override():
        raise RuntimeError("Storage-Service-Migration ist fehlgeschlagen")
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

    _verify_transition_state(state)
    _announce_finalizer_phase(5, phase_total, "Dienste aktivieren und geordnet starten")
    if not _restart_v4_services(
        headless=headless,
        services=restart_services,
        transition_state=state,
    ):
        raise RuntimeError("Erwartete Dienste konnten nicht vollständig gestartet werden")
    if not refresh_watchdog_guard_script():
        _stop_v4_services(restart_services)
        raise RuntimeError("Watchdog-Guard konnte nach dem finalen Dienststart nicht aktualisiert werden")
    _announce_finalizer_phase(6, phase_total, "Gesundheit und Bootvertrag verifizieren")
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
    _announce_finalizer_phase(7, phase_total, "Initiale lokale Prognose aktualisieren")
    run_initial_forecast(os.path.join(target_root, "Installer"))


def _target_execution_archive_entries(
    *,
    repo_dir: str,
    target_commit: str,
    install_user: str,
) -> dict[str, tuple[bytes, int]]:
    """Liest den vollständigen ausführbaren Installer-Baum direkt aus dem Commit."""

    commit = _validate_full_commit(target_commit)
    try:
        completed = subprocess.run(
            [
                "sudo", "-H", "-u", str(install_user),
                "git", "-c", "tar.umask=0022", "-C", str(repo_dir),
                "archive", "--format=tar", commit, "--",
                *TARGET_EXECUTION_SNAPSHOT_ROOT_FILES,
                "Installer",
            ],
            capture_output=True,
            text=False,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Target-Ausführungssnapshot konnte nicht aus Git gelesen werden") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError("Target-Ausführungssnapshot fehlt im freigegebenen Commit: " + detail[-500:])
    archive = bytes(completed.stdout or b"")
    if not archive or len(archive) > TARGET_EXECUTION_SNAPSHOT_MAX_TOTAL_BYTES + (16 * 1024 * 1024):
        raise RuntimeError("Target-Ausführungssnapshot besitzt eine unzulässige Archivgröße")

    entries: dict[str, tuple[bytes, int]] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            for member in bundle.getmembers():
                relative_path = str(member.name or "").rstrip("/")
                if not relative_path:
                    continue
                if (
                    relative_path.startswith("/")
                    or "\\" in relative_path
                    or any(part in {"", ".", ".."} for part in Path(relative_path).parts)
                    or not (
                        relative_path in TARGET_EXECUTION_SNAPSHOT_ROOT_FILES
                        or relative_path == "Installer"
                        or relative_path.startswith("Installer/")
                    )
                ):
                    raise RuntimeError("Target-Ausführungssnapshot enthält einen unzulässigen Pfad")
                if member.isdir():
                    continue
                if not member.isfile() or member.islnk() or member.issym():
                    raise RuntimeError(
                        f"Target-Ausführungssnapshot enthält keinen regulären Blob: {relative_path}"
                    )
                if relative_path in entries:
                    raise RuntimeError("Target-Ausführungssnapshot enthält einen doppelten Pfad")
                mode = stat.S_IMODE(member.mode)
                if mode not in {0o644, 0o755}:
                    raise RuntimeError(
                        f"Target-Ausführungssnapshot besitzt einen unzulässigen Git-Modus: {relative_path}"
                    )
                if member.size < 0 or member.size > TARGET_EXECUTION_SNAPSHOT_MAX_FILE_BYTES:
                    raise RuntimeError(
                        f"Target-Ausführungssnapshot besitzt eine unzulässige Dateigröße: {relative_path}"
                    )
                source = bundle.extractfile(member)
                if source is None:
                    raise RuntimeError(
                        f"Target-Ausführungssnapshot konnte einen Blob nicht lesen: {relative_path}"
                    )
                payload = source.read(TARGET_EXECUTION_SNAPSHOT_MAX_FILE_BYTES + 1)
                if len(payload) != member.size:
                    raise RuntimeError(
                        f"Target-Ausführungssnapshot besitzt eine driftende Blobgröße: {relative_path}"
                    )
                total += len(payload)
                if (
                    len(entries) >= TARGET_EXECUTION_SNAPSHOT_MAX_FILES
                    or total > TARGET_EXECUTION_SNAPSHOT_MAX_TOTAL_BYTES
                ):
                    raise RuntimeError("Target-Ausführungssnapshot überschreitet die feste Größenbindung")
                entries[relative_path] = (
                    payload,
                    0o555 if mode & 0o111 else 0o444,
                )
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError("Target-Ausführungssnapshot besitzt kein gültiges Git-Archiv") from exc

    required = set(TARGET_EXECUTION_SNAPSHOT_ROOT_FILES) | set(TARGET_FINALIZER_RELATIVE_FILES)
    if not required.issubset(entries):
        missing = ", ".join(sorted(required.difference(entries)))
        raise RuntimeError("Target-Ausführungssnapshot ist unvollständig: " + missing)
    return entries


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
            "E3DC_BOOTSTRAP_USER",
            "E3DC_BOOTSTRAP_VENV",
            "PYTHONHOME",
            "PYTHONPATH",
        ):
            environment.pop(name, None)
        environment["E3DC_BOOTSTRAP_ROOT"] = repo_dir
        environment["E3DC_BOOTSTRAP_RUNNER_ROOT"] = runner_root
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
) -> None:
    """Startet den SHA-gebundenen zweiten Prozess mit bereinigtem Importkontext."""

    lock_fd = _required_update_lock_fd()
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
    if not package_transaction.pip_requested:
        venv_state = "unused"
        venv_path = ""
    else:
        venv_state = "present" if package_transaction.venv_existed else "missing"
        venv_path = str(package_transaction.venv_path or "")
        if not os.path.isabs(venv_path):
            raise RuntimeError("Paket-Preimage besitzt keinen absoluten venv-Pfad")

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
        "E3DC_BOOTSTRAP_USER",
        "E3DC_BOOTSTRAP_VENV",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        environment.pop(name, None)
    environment["E3DC_BOOTSTRAP_ROOT"] = repo_dir
    environment["E3DC_BOOTSTRAP_RUNNER_ROOT"] = snapshot_root
    environment["E3DC_INSTALL_ROOT"] = repo_dir
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    environment["LC_ALL"] = "C.UTF-8"
    environment["LANG"] = "C.UTF-8"
    environment[UPDATE_LOCK_ENV] = str(lock_fd)
    try:
        _verify_target_execution_snapshot(
            snapshot_root,
            snapshot_entries,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )
        result = _run_streaming_argv(
            [
                python,
                "-I",
                "-B",
                "-u",
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
            timeout=TARGET_FINALIZER_TIMEOUT_S,
            env=environment,
            pass_fds=(lock_fd,),
            label="Target-Finalizer",
        )
    finally:
        try:
            if os.path.lexists(snapshot_root):
                if os.lstat(snapshot_root).st_dev != os.lstat(repo_dir).st_dev:
                    raise RuntimeError(
                        "Target-Finalizer-Snapshot driftete vom Produkt-Dateisystem"
                    )
            _remove_target_execution_snapshot(snapshot_root)
        except Exception as exc:
            update_logger.warning("Target-Ausführungssnapshot konnte nicht bereinigt werden: %s", exc)
    marker = f"{TARGET_FINALIZER_SUCCESS} {target_commit} {target_tag}"
    marker_count = int(
        (result.get("stdout_line_counts") or {}).get(marker, 0)
    )
    if not result.get("success") or marker_count != 1:
        raise RuntimeError(
            "Target-Finalizer fehlgeschlagen: "
            + _combined_process_diagnostics(result)
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
                raise RuntimeError(
                    "Target-Datei besitzt mehrere Hardlinks: "
                    + _target_metadata_detail(relative_path, before)
                )
            if before.st_uid not in (0, account.pw_uid):
                raise RuntimeError(
                    "Target-Datei besitzt einen nicht vertrauenswürdigen Eigentümer: "
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
        _secure_repo_permissions(
            repo_dir,
            install_user,
            expected_commit=old_commit,
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
    if recovery_ok:
        recovery_ok = _recover_pretransaction_service_state(state)
    return recovery_ok


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
        status = run_command(f"systemctl is-active {unit}", timeout=10)
        activity = status.get("stdout", "").strip().lower()
        end_state_ok = (
            bool(status.get("success")) and activity == "active"
            if should_be_active
            else activity in {"inactive", "failed"}
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
        status = run_command(f"systemctl is-active {unit}", timeout=10)
        activity = status.get("stdout", "").strip().lower()
        end_state_ok = (
            bool(status.get("success")) and activity == "active"
            if should_be_active
            else activity in {"inactive", "failed"}
        )
        if not end_state_ok:
            update_logger.error(
                "Globaler Recovery-Endzustand weicht ab: "
                f"{unit} erwartet={'active' if should_be_active else 'inactive'}, "
                f"gefunden={activity or 'unlesbar'}"
            )
            recovered = False
    return recovered


def _enforce_fail_closed_after_recovery_failure() -> bool:
    """Stoppt nach unvollständiger Recovery erneut alles und beweist die Aktorruhe."""

    quiesced = _stop_v4_services(V4_SERVICES)
    if quiesced:
        print(
            "[!] Recovery blieb unvollständig; die Aktor-/Writer-Ruhe wurde "
            "erneut bewiesen und die Watchdog-Sperre bleibt aktiv."
        )
    else:
        print(
            "[!] Recovery und erneute Aktorruhe sind nicht vollständig "
            "beweisbar; die Watchdog-Sperre bleibt aktiv."
        )
    return quiesced


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

    result = _git_argv(
        repo_dir,
        install_user,
        "ls-tree",
        "-r",
        "--full-tree",
        "-z",
        commit,
        timeout=30,
    )
    if not result.get("success"):
        raise RuntimeError(
            "Git-Dateivertrag ist nicht lesbar: "
            + _combined_process_diagnostics(result, maximum=800)
        )
    root = os.path.abspath(repo_dir)
    entries: list[tuple[str, int, str]] = []
    seen_paths: set[str] = set()
    for raw_entry in str(result.get("stdout") or "").split("\0"):
        if not raw_entry:
            continue
        metadata_text, separator, relative_path = raw_entry.partition("\t")
        fields = metadata_text.split()
        if (
            not separator
            or len(fields) != 3
            or fields[1] != "blob"
            or fields[0] not in {"100644", "100755"}
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[2])
            or not relative_path
            or "\x00" in relative_path
            or relative_path in seen_paths
        ):
            raise RuntimeError(
                "Git-Dateivertrag enthält keinen regulären Commit-Blob"
            )
        target = os.path.abspath(os.path.join(root, relative_path))
        if os.path.commonpath((root, target)) != root or target == root:
            raise RuntimeError("Getrackte Produktdatei verlässt das Repository")
        seen_paths.add(relative_path)
        entries.append(
            (
                relative_path,
                0o755 if fields[0] == "100755" else 0o644,
                fields[2],
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
        repo_dir = (
            _validate_bootstrap_install_path(target_install_path)
            if target_install_path
            else _validate_bootstrap_install_path(
                transaction_repo_dir or INSTALL_PATH
            )
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
        recovered = _recover_pretransaction_service_state(state)
        if recovered:
            print(
                "[OK] Der eingefrorene Ausgangszustand wurde nach dem "
                "partiellen Stop verifiziert wiederhergestellt."
            )
            _set_watchdog_update_pause(False, reason=transition_name)
        else:
            _enforce_fail_closed_after_recovery_failure()
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

        remote = _git_argv(repo_dir, install_user, "remote", "get-url", "origin", timeout=15)
        if not remote["success"] or remote["stdout"].strip() != SELFUPDATE_REPO:
            raise RuntimeError("Git-Origin weicht vom fest freigegebenen Release-Repository ab")

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
        package_transaction = _capture_package_transaction(
            policy,
            install_user,
            # Auch ein normaler Self-Update darf einen wirklich fehlenden,
            # policygebundenen Benutzer-venv erstmals erzeugen. Capture
            # erlaubt dies nur bei freigegebenem python3-venv und exakt
            # absentem kanonischem Zielpfad; jeder belegte Fremdpfad stoppt.
            allow_missing_venv=True,
        )

        mutated = True
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
            recovered = _recover_pretransaction_service_state(state)
        if recovered:
            print("[OK] Ausgangszustand wurde automatisch und verifiziert wiederhergestellt.")
            _set_watchdog_update_pause(False, reason=transition_name)
        else:
            _enforce_fail_closed_after_recovery_failure()
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
        module_root = os.path.dirname(INSTALLER_DIR)
        if os.path.realpath(module_root) != repo_dir:
            raise RuntimeError(
                "Alt-Updater darf den Ziel-Handoff nur aus dem aktiven Produktbaum starten"
            )
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            raise RuntimeError(
                "Installation ohne Git darf nur über den expliziten Erstinstallations-Bootstrap wechseln"
            )
        install_user = get_install_user()
        expected_role = _read_handoff_role()
        old_commit = _resolve_git_commit(repo_dir, "HEAD", install_user)
        if not old_commit:
            raise RuntimeError("Aktueller HEAD konnte nicht als volle Commit-SHA verifiziert werden")
        remote = _git_argv(repo_dir, install_user, "remote", "get-url", "origin", timeout=15)
        if not remote["success"] or remote["stdout"].strip() != SELFUPDATE_REPO:
            raise RuntimeError("Git-Origin weicht vom fest freigegebenen Release-Repository ab")

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
