#!/usr/bin/env python3
"""SHA-gebundener zweiter Prozess für einen Release-Wechsel.

Der Bootstrap-Prozess erzeugt nach dem atomaren Git-Wechsel einen versiegelten
Ausführungssnapshot aus dem verifizierten Zielcommit und lädt den Finalizer
ausschließlich daraus. Erst dieser Prozess darf Zielmodule importieren und
Installation, Rechte, Web-Synchronisation sowie Dienststart finalisieren.
"""

from __future__ import annotations

import time as _bootstrap_time

_PROCESS_ENTRY_MONOTONIC = _bootstrap_time.monotonic()

# Veröffentlichte Updater bis einschließlich v5.4.2 starten diesen Zielcode
# direkt aus dem benutzereigenen Produktverzeichnis und noch ohne ``-I``.
# Entferne deshalb den Skriptpfad, bevor irgendein nicht eingebautes Modul
# importiert wird. Zielmodule werden später ausschließlich aus dem gebundenen
# Ausführungssnapshot wieder explizit in ``sys.path`` aufgenommen.
import sys as _bootstrap_sys

if __name__ == "__main__":
    _bootstrap_script_dir = __file__.rpartition("/")[0]
    _bootstrap_sys.path[:] = [
        item
        for item in _bootstrap_sys.path
        if item not in {"", _bootstrap_script_dir}
    ]

import argparse
import fcntl
import hashlib
import inspect
import json
import os
import pwd
import queue
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, deque
from pathlib import Path


_SNAPSHOT_ROOT_FILES = ("VERSION", "installer_main.py")
_FINALIZER_FILES = (
    "Installer/__init__.py",
    "Installer/git_commit_reader.py",
    "Installer/optional_service_contract.py",
    "Installer/release_finalize.py",
    "Installer/update.py",
)
_SNAPSHOT_MAX_FILES = 4096
_SNAPSHOT_MAX_FILE_BYTES = 8 * 1024 * 1024
_SNAPSHOT_MAX_TOTAL_BYTES = 128 * 1024 * 1024
_COMPAT_SNAPSHOT_PARENT = Path("/run")
_COMPAT_SNAPSHOT_PREFIX = ".e3dc-release-finalizer-compat-"
_FINALIZER_SUCCESS = "E3DC_RELEASE_TARGET_FINALIZER_OK"
_TARGET_UPDATER_SUCCESS = "E3DC_RELEASE_TARGET_UPDATER_OK"
_TARGET_UPDATER_NOOP = "E3DC_RELEASE_TARGET_UPDATER_NOOP"
_LEGACY_BRIDGE_FINALIZER_TIMEOUT_S = 12 * 60
_FINALIZER_HEARTBEAT_S = 30
_FINALIZER_TERMINATE_GRACE_S = 10
_FINALIZER_DIAGNOSTIC_LINES = 512
_UPDATE_LOCK_PATH = Path("/run/lock/e3dc-control/update.lock")
_UPDATE_LOCK_ENV = "E3DC_UPDATE_LOCK_FD"
_UPDATE_SAFETY_RECEIPT_PATH = Path("/var/lib/e3dc-update-safety/transaction.json")
_UPDATE_SAFETY_MARKER_PATH = "/var/lib/e3dc-update-safety/recovery.block"
_UPDATE_SAFETY_SCHEMA = "e3dc_update_safety_v1"
_UPDATE_FINALIZER_INVOCATION_ENV = "E3DC_UPDATE_FINALIZER_INVOCATION_ID"
_COMMIT_READER_PATH = "Installer/git_commit_reader.py"
_FULL_SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
_ACCOUNT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_COMMIT_READER_CACHE: dict[
    tuple[str, str],
    tuple[object, object, object],
] = {}


class _DeferredParentSignal(BaseException):
    """Signalisiert einen Nutzerabbruch erst nach sicherem Kindprozessende."""

    def __init__(self, signum: int):
        self.signum = int(signum)
        super().__init__(f"Elternprozess erhielt Signal {self.signum}")


class _LegacyUpdateSafetyPostCommitError(RuntimeError):
    """Niemals ausgelöster Typ-Sentinel für Updater ohne Safety-Vertrag."""


class _LegacyUpdateSafetyManagedServiceUnquiescedError(RuntimeError):
    """Niemals ausgelöster Typ-Sentinel für Updater ohne Safety-Vertrag."""


def _bind_update_safety_exception_types(
    update_module,
    *,
    require_native: bool,
) -> tuple[type[RuntimeError], type[RuntimeError]]:
    """Bindet neue Safety-Typen, ohne Legacy-Fehler zu breit zu klassifizieren."""

    bindings = []
    for name, fallback in (
        ("UpdateSafetyPostCommitError", _LegacyUpdateSafetyPostCommitError),
        (
            "UpdateSafetyManagedServiceUnquiescedError",
            _LegacyUpdateSafetyManagedServiceUnquiescedError,
        ),
    ):
        candidate = getattr(update_module, name, None)
        if candidate is None:
            if require_native:
                raise RuntimeError(
                    "Versiegelter Target-Updater besitzt für den aktiven "
                    f"Update-Sicherheitsvertrag keinen nativen Fehlertyp {name}"
                )
            candidate = fallback
        elif (
            not inspect.isclass(candidate)
            or candidate is RuntimeError
            or not issubclass(candidate, RuntimeError)
            or getattr(candidate, "__module__", "")
            != getattr(update_module, "__name__", "")
        ):
            raise RuntimeError(
                f"Versiegelter Target-Updater besitzt einen ungültigen Fehlertyp {name}"
            )
        bindings.append(candidate)
    if bindings[0] is bindings[1]:
        raise RuntimeError(
            "Versiegelter Target-Updater vermischt Postcommit- und "
            "Unquiesced-Fehlertyp"
        )
    return bindings[0], bindings[1]


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


class _FailSafeProcessStream:
    """Lässt einen abgerissenen Parent-Pipe niemals den Recoverypfad abbrechen."""

    def __init__(self, stream):
        self._stream = stream
        self._fallback = None

    def _fallback_stream(self):
        if self._fallback is None:
            descriptor = os.open(
                "/dev/null",
                os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
            )
            self._fallback = os.fdopen(
                descriptor,
                "w",
                encoding="utf-8",
                buffering=1,
            )
        self._stream = self._fallback
        return self._fallback

    def write(self, value):
        try:
            return self._stream.write(value)
        except (BrokenPipeError, OSError, ValueError):
            return self._fallback_stream().write(value)

    def flush(self):
        try:
            return self._stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            return self._fallback_stream().flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _install_fail_safe_process_streams() -> None:
    if not isinstance(sys.stdout, _FailSafeProcessStream):
        sys.stdout = _FailSafeProcessStream(sys.stdout)
    if not isinstance(sys.stderr, _FailSafeProcessStream):
        sys.stderr = _FailSafeProcessStream(sys.stderr)


def _assert_root_controlled_directory_chain(path: Path) -> None:
    """Bindet jede kanonische Interpreter-Verzeichniskomponente an Root."""

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
    """Verwendet für Root-Snapshots nie den aufrufenden Benutzer-venv."""

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
        raise RuntimeError("System-Python-Pfad ist weder Link noch reguläre Datei")
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


def _assert_update_lock_directory(path: Path) -> None:
    """Bindet den privaten Lock-Unterbaum samt Sticky-Systemroot."""

    directory = Path(path)
    if not directory.is_absolute() or directory.resolve(strict=True) != directory:
        raise RuntimeError("Update-Lock-Verzeichnis ist nicht kanonisch")
    current = Path(directory.anchor)
    for component in directory.parts[1:]:
        current /= component
        metadata = current.lstat()
        shared_root = current == Path("/run/lock")
        shared_safe = (
            shared_root
            and metadata.st_uid == 0
            and metadata.st_gid == 0
            and bool(metadata.st_mode & stat.S_ISVTX)
        )
        if (
            current.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or (
                not shared_safe
                and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            )
        ):
            raise RuntimeError(
                f"Update-Lock-Pfad besitzt eine unsichere Komponente: {current}"
            )


def _open_update_lock_directory(*, create: bool) -> int:
    shared_root = Path("/run/lock")
    metadata = shared_root.lstat()
    if (
        shared_root.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or (
            metadata.st_mode & stat.S_IWOTH
            and not metadata.st_mode & stat.S_ISVTX
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
    private = os.fstat(private_fd)
    if (
        not stat.S_ISDIR(private.st_mode)
        or private.st_uid != 0
        or private.st_gid != 0
        or stat.S_IMODE(private.st_mode) != 0o700
    ):
        os.close(private_fd)
        raise RuntimeError("Privates Update-Lock-Verzeichnis besitzt unsichere Metadaten")
    return private_fd


def _validate_update_lock_fd(descriptor: int) -> int:
    fd = int(descriptor)
    if fd < 3:
        raise RuntimeError("Update-Lock-FD ist unzulässig")
    _assert_update_lock_directory(_UPDATE_LOCK_PATH.parent)
    path_metadata = _UPDATE_LOCK_PATH.lstat()
    fd_metadata = os.fstat(fd)
    if (
        _UPDATE_LOCK_PATH.is_symlink()
        or not stat.S_ISREG(path_metadata.st_mode)
        or path_metadata.st_nlink != 1
        or path_metadata.st_uid != 0
        or path_metadata.st_gid != 0
        or stat.S_IMODE(path_metadata.st_mode) != 0o600
        or (path_metadata.st_dev, path_metadata.st_ino)
        != (fd_metadata.st_dev, fd_metadata.st_ino)
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
        raise RuntimeError(
            "Ein anderer E3DC-Control-Releasewechsel läuft bereits"
        ) from exc
    return fd


def _acquire_or_inherit_update_lock(*, allow_create: bool) -> tuple[int, bool]:
    inherited = str(os.environ.get(_UPDATE_LOCK_ENV) or "").strip()
    if inherited:
        if not inherited.isdecimal():
            raise RuntimeError("Geerbter Update-Lock-FD ist ungültig")
        return _validate_update_lock_fd(int(inherited)), False
    if not allow_create:
        raise RuntimeError("Versiegelter Finalizer besitzt keinen geerbten Transaktionslock")
    if os.geteuid() != 0:
        raise RuntimeError("Release-Finalizer benötigt Root für den Update-Lock")
    directory_fd = _open_update_lock_directory(create=True)
    try:
        descriptor = os.open(
            _UPDATE_LOCK_PATH.name,
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
    inherited = str(os.environ.get(_UPDATE_LOCK_ENV) or "").strip()
    if not inherited or not inherited.isdecimal():
        raise RuntimeError("Finalizer besitzt keinen geerbten Transaktionslock")
    return _validate_update_lock_fd(int(inherited))


def _run_compat_finalizer(command, *, environment, pass_fds=()) -> dict:
    """Streamt den mutierenden Alt-Updater-Bridgeprozess mit hartem Zeitlimit."""

    deadline = (
        _PROCESS_ENTRY_MONOTONIC + _LEGACY_BRIDGE_FINALIZER_TIMEOUT_S
    )
    if time.monotonic() >= deadline:
        return {
            "returncode": -1,
            "output": "",
            "line_counts": {},
            "timed_out": True,
            "error": (
                "Zeitbudget der Kompatibilitätsbrücke war vor dem "
                "mutierenden Kindprozess erschöpft"
            ),
        }

    signal_guard = _TerminalSignalGuard()
    signal_guard.install()
    try:
        process = subprocess.Popen(
            [str(item) for item in command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
            start_new_session=os.name == "posix",
            pass_fds=tuple(int(item) for item in pass_fds),
        )
    except OSError as exc:
        signal_guard.restore()
        return {
            "returncode": -1,
            "output": "",
            "line_counts": {},
            "timed_out": False,
            "error": str(exc),
        }
    except BaseException:
        signal_guard.restore()
        raise
    signal_guard.arm()

    events: queue.Queue[str | None] = queue.Queue()
    tail = deque(maxlen=_FINALIZER_DIAGNOSTIC_LINES)
    line_counts: Counter[str] = Counter()

    def _drain() -> None:
        try:
            for line in iter(process.stdout.readline, ""):
                events.put(line)
        finally:
            events.put(None)
            process.stdout.close()

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    started = time.monotonic()
    last_heartbeat = started
    stream_done = False
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
        while (
            not stream_done
            or process.poll() is None
            or (timed_out and not force_sent)
        ):
            signal_guard.raise_if_requested()
            now = time.monotonic()
            if (
                not timed_out
                and now >= deadline
            ):
                timed_out = True
                termination_started = now
                _stop_process_tree(force=False)
                print(
                    f"[!] Kompatibilitäts-Finalizer überschreitet "
                    f"{_LEGACY_BRIDGE_FINALIZER_TIMEOUT_S} Sekunden; "
                    "Recovery wird vorbereitet.",
                    flush=True,
                )
            elif (
                timed_out
                and not force_sent
                and now - termination_started >= _FINALIZER_TERMINATE_GRACE_S
            ):
                _stop_process_tree(force=True)
                force_sent = True
            try:
                line = events.get(timeout=0.2)
            except queue.Empty:
                line = ""
            if line is None:
                stream_done = True
            elif line:
                tail.append(line)
                line_counts[line.strip()] += 1
                sys.stdout.write(line)
                sys.stdout.flush()
            now = time.monotonic()
            if (
                not timed_out
                and process.poll() is None
                and now - last_heartbeat >= _FINALIZER_HEARTBEAT_S
            ):
                print(
                    f"[i] Kompatibilitäts-Finalizer läuft weiter "
                    f"({int(now - started)} Sekunden seit Start).",
                    flush=True,
                )
                last_heartbeat = now
    except BaseException:
        # Auch bei abgebrochenem Ausgabeweg bleibt der Snapshot bestehen und
        # der Kindprozess unter demselben Lock, bis sein mutierender Lauf
        # eindeutig beendet ist. Das reguläre Finalizer-Zeitlimit gilt weiter.
        try:
            while (
                not stream_done
                or process.poll() is None
                or (timed_out and not force_sent)
            ):
                now = time.monotonic()
                if (
                    not timed_out
                    and now >= deadline
                ):
                    timed_out = True
                    termination_started = now
                    _stop_process_tree(force=False)
                elif (
                    timed_out
                    and not force_sent
                    and now - termination_started >= _FINALIZER_TERMINATE_GRACE_S
                ):
                    _stop_process_tree(force=True)
                    force_sent = True
                try:
                    line = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    stream_done = True
                elif line:
                    tail.append(line)
                    line_counts[line.strip()] += 1
            process.wait()
            reader.join(timeout=1)
        finally:
            signal_guard.restore()
        raise
    # Erst der nachgewiesene Prozessabschluss darf den unveränderbaren
    # 900-Sekunden-Außenprozess der Alt-Updater in deren Recovery entlassen.
    returncode = process.wait()
    reader.join(timeout=1)
    try:
        signal_guard.raise_if_requested()
        return {
            "returncode": returncode,
            "output": "".join(tail),
            "line_counts": dict(line_counts),
            "timed_out": timed_out,
            "error": (
                "Zeitlimit von "
                f"{_LEGACY_BRIDGE_FINALIZER_TIMEOUT_S} Sekunden überschritten"
                if timed_out
                else ""
            ),
        }
    finally:
        signal_guard.restore()


def _regular_nofollow(path: Path, label: str) -> Path:
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise RuntimeError(f"{label} ist keine eindeutige reguläre Datei")
    return path


def _bound_product_root(install_path: str) -> Path:
    raw = Path(str(install_path or ""))
    if not raw.is_absolute():
        raise RuntimeError("Installationspfad muss absolut sein")
    root = raw.resolve(strict=True)
    if root != raw:
        raise RuntimeError("Installationspfad darf kein Symlinkpfad sein")

    _regular_nofollow(root / "VERSION", "VERSION")
    _regular_nofollow(root / "installer_main.py", "installer_main.py")
    _regular_nofollow(root / "Installer" / "update.py", "Installer/update.py")
    return root


def _bound_execution_root(product_root: Path) -> Path:
    script = _regular_nofollow(Path(os.path.abspath(__file__)), "Target-Finalizer")
    snapshot_root = script.parent.parent
    if snapshot_root == product_root or snapshot_root.resolve(strict=True) != snapshot_root:
        raise RuntimeError("Target-Finalizer besitzt keinen getrennten kanonischen Ausführungssnapshot")
    return snapshot_root


def _read_regular_nofollow(path: Path, maximum: int = 1024 * 1024) -> bytes:
    _regular_nofollow(path, str(path))
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if before.st_size < 1 or before.st_size > maximum:
            raise RuntimeError(f"Target-Modul besitzt eine unzulässige Größe: {path}")
        chunks = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_uid,
            before.st_gid,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_uid,
            after.st_gid,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after:
            raise RuntimeError(f"Target-Modul driftete während des Lesens: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _bootstrap_repository_reader_user(root: Path) -> str | None:
    """Bindet Git-Lesezugriffe vor dem Laden des zentralen Commit-Lesers."""

    git_dir = root / ".git"
    root_metadata = root.lstat()
    git_metadata = git_dir.lstat()
    if (
        not root.is_absolute()
        or root.resolve(strict=True) != root
        or root.is_symlink()
        or not root.is_dir()
        or git_dir.is_symlink()
        or not git_dir.is_dir()
        or root_metadata.st_uid == 0
        or git_metadata.st_uid != root_metadata.st_uid
    ):
        raise RuntimeError(
            "Git-Repository besitzt vor dem Commit-Leser keine gebundene Eigentümerstruktur"
        )
    try:
        account = pwd.getpwuid(root_metadata.st_uid)
    except KeyError as exc:
        raise RuntimeError("Git-Repository-Eigentümer fehlt lokal") from exc
    if not _ACCOUNT_NAME_RE.fullmatch(account.pw_name):
        raise RuntimeError("Git-Repository-Eigentümer besitzt keinen sicheren Namen")
    if os.geteuid() == root_metadata.st_uid:
        return None
    if os.geteuid() != 0:
        raise RuntimeError(
            "Commit-Leser-Bootstrap läuft weder als Root noch als Repository-Eigentümer"
        )
    return account.pw_name


def _bound_legacy_install_user(root: Path) -> str:
    """Bindet den Altübergang an den nicht privilegierten Repo-Eigentümer."""

    install_user = _bootstrap_repository_reader_user(root)
    if (
        not install_user
        or not _ACCOUNT_NAME_RE.fullmatch(install_user)
        or install_user in {"root", "www-data"}
    ):
        raise RuntimeError(
            "Legacy-Zielübergang besitzt keinen gebundenen Installationsbenutzer"
        )
    try:
        account = pwd.getpwnam(install_user)
    except KeyError as exc:
        raise RuntimeError(
            "Legacy-Zielübergang besitzt keinen lokalen Installationsbenutzer"
        ) from exc
    root_metadata = root.lstat()
    git_metadata = (root / ".git").lstat()
    if (
        account.pw_uid == 0
        or root_metadata.st_uid != account.pw_uid
        or git_metadata.st_uid != account.pw_uid
    ):
        raise RuntimeError(
            "Legacy-Zielübergang besitzt keine gebundene Eigentümerstruktur"
        )
    return install_user


def _legacy_repository_identity(root: Path) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Bindet Root und Git-Verzeichnis gegen Austausch vor dem Kindstart."""

    identities = []
    for path in (root, root / ".git"):
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(
                "Legacy-Zielübergang besitzt keine kanonische Repositorystruktur"
            )
        identities.append(
            (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_nlink,
                metadata.st_uid,
                metadata.st_gid,
            )
        )
    return identities[0], identities[1]


def _bound_legacy_snapshot_install_user(
    root: Path,
) -> tuple[str, tuple[tuple[int, ...], tuple[int, ...]]]:
    """Bindet den Nutzer eines älteren Ziel-Snapshotpfads an das Repository."""

    identity_before = _legacy_repository_identity(root)
    install_user = _bound_legacy_install_user(root)
    identity_after = _legacy_repository_identity(root)
    if identity_before != identity_after:
        raise RuntimeError(
            "Repository driftete während der Legacy-Nutzerbindung"
        )
    return install_user, identity_after


def _revalidate_legacy_snapshot_install_user(
    root: Path,
    install_user: str | None,
    repository_identity: tuple[tuple[int, ...], tuple[int, ...]] | None,
    *,
    context: str,
) -> None:
    """Prüft Nutzer und Repository direkt vor dem ersten Zielimport erneut."""

    rebound_user, rebound_identity = _bound_legacy_snapshot_install_user(root)
    if (
        not install_user
        or not repository_identity
        or os.environ.get("E3DC_BOOTSTRAP_USER") != install_user
        or rebound_user != install_user
        or rebound_identity != repository_identity
    ):
        raise RuntimeError(
            "Repository oder Installationsbenutzer driftete vor dem " + context
        )


def _bootstrap_git_command(
    root: Path,
    *arguments: str,
    run_as_user: str | None,
) -> list[str]:
    """Erzeugt den festen, konfigurationsfreien Git-Objektleser."""

    if run_as_user is None:
        prefix = ["/usr/bin/env", "-i"]
    else:
        user = str(run_as_user or "").strip()
        if not _ACCOUNT_NAME_RE.fullmatch(user):
            raise RuntimeError("Commit-Lese-Benutzer ist ungültig")
        try:
            account = pwd.getpwnam(user)
        except KeyError as exc:
            raise RuntimeError("Commit-Lese-Benutzer fehlt lokal") from exc
        if account.pw_uid == 0:
            raise RuntimeError("Commit-Lese-Benutzer darf nicht Root sein")
        prefix = [
            "/usr/bin/sudo",
            "-n",
            "-H",
            "-u",
            user,
            "--",
            "/usr/bin/env",
            "-i",
        ]
    return [
        *prefix,
        "PATH=/usr/bin:/bin",
        "HOME=/nonexistent",
        "XDG_CONFIG_HOME=/nonexistent",
        "LANG=C",
        "LC_ALL=C",
        "GIT_CONFIG_NOSYSTEM=1",
        "GIT_CONFIG_SYSTEM=/dev/null",
        "GIT_CONFIG_GLOBAL=/dev/null",
        "GIT_ATTR_NOSYSTEM=1",
        "GIT_NO_REPLACE_OBJECTS=1",
        "GIT_OPTIONAL_LOCKS=0",
        "GIT_TERMINAL_PROMPT=0",
        "GIT_ASKPASS=/bin/false",
        "SSH_ASKPASS=/bin/false",
        "GIT_ALLOW_PROTOCOL=https",
        "/usr/bin/git",
        "--no-replace-objects",
        "-c",
        f"safe.directory={root}",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=/bin/false",
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "protocol.file.allow=never",
        f"--git-dir={root / '.git'}",
        f"--work-tree={root}",
        *arguments,
    ]


def _bootstrap_run_git(
    root: Path,
    *arguments: str,
    run_as_user: str | None,
    input_bytes: bytes | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            _bootstrap_git_command(
                root,
                *arguments,
                run_as_user=run_as_user,
            ),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Commit-Leser-Bootstrap konnte Git nicht ausführen") from exc


def _bootstrap_read_object(
    root: Path,
    object_id: str,
    expected_type: bytes,
    *,
    run_as_user: str | None,
    maximum_bytes: int,
) -> bytes:
    """Liest genau ein Git-Objekt und prüft seine SHA-1 selbst."""

    if not _FULL_SHA1_RE.fullmatch(object_id):
        raise RuntimeError("Commit-Leser-Bootstrap erhielt keine volle Objekt-ID")
    completed = _bootstrap_run_git(
        root,
        "cat-file",
        "--batch",
        run_as_user=run_as_user,
        input_bytes=object_id.encode("ascii") + b"\n",
        timeout=30,
    )
    if completed.returncode != 0:
        detail = bytes(completed.stderr or b"").decode(
            "utf-8", errors="replace"
        ).strip()
        raise RuntimeError(
            "Commit-Leser-Bootstrap konnte ein Git-Objekt nicht lesen: "
            + detail[-500:]
        )
    output = bytes(completed.stdout or b"")
    header, separator, remainder = output.partition(b"\n")
    try:
        raw_id, object_type, raw_size = header.split(b" ", 2)
        actual_id = raw_id.decode("ascii")
        size = int(raw_size)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(
            "Commit-Leser-Bootstrap erhielt keine eindeutige Objektantwort"
        ) from exc
    if (
        separator != b"\n"
        or actual_id != object_id
        or object_type != expected_type
        or size < 1
        or size > maximum_bytes
        or len(remainder) != size + 1
        or remainder[-1:] != b"\n"
    ):
        raise RuntimeError("Commit-Leser-Bootstrap erhielt ein unzulässiges Git-Objekt")
    payload = remainder[:-1]
    digest = hashlib.new(
        "sha1",
        expected_type + b" " + str(size).encode("ascii") + b"\0" + payload,
        usedforsecurity=False,
    ).hexdigest()
    if digest != object_id:
        raise RuntimeError(
            "Commit-Leser-Bootstrap erkannte eine abweichende Objekt-ID"
        )
    return payload


def _bootstrap_parse_tree(payload: bytes) -> dict[bytes, tuple[str, str]]:
    """Parst einen kryptographisch gebundenen Git-Baum ohne Arbeitsbaumfilter."""

    entries: dict[bytes, tuple[str, str]] = {}
    folded_names: set[str] = set()
    offset = 0
    while offset < len(payload):
        separator = payload.find(b" ", offset)
        terminator = payload.find(b"\0", separator + 1)
        if (
            separator <= offset
            or terminator <= separator + 1
            or terminator + 21 > len(payload)
        ):
            raise RuntimeError("Commit-Leser-Bootstrap erhielt keinen kanonischen Git-Baum")
        raw_mode = payload[offset:separator]
        raw_name = payload[separator + 1 : terminator]
        raw_oid = payload[terminator + 1 : terminator + 21]
        try:
            mode = raw_mode.decode("ascii")
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                "Commit-Leser-Bootstrap erhielt keinen UTF-8-Baumpfad"
            ) from exc
        folded = name.casefold()
        if (
            mode not in {"40000", "100644", "100755"}
            or not raw_name
            or b"/" in raw_name
            or raw_name in {b".", b".."}
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or folded in folded_names
            or raw_name in entries
        ):
            raise RuntimeError(
                "Commit-Leser-Bootstrap erhielt einen mehrdeutigen Git-Baumeintrag"
            )
        entries[raw_name] = (mode, raw_oid.hex())
        folded_names.add(folded)
        offset = terminator + 21
    if offset != len(payload):
        raise RuntimeError("Commit-Leser-Bootstrap erhielt überzählige Baumdaten")
    return entries


def _bootstrap_commit_reader_payload(
    root: Path,
    expected_commit: str,
) -> bytes:
    """Bindet den neuen Helper aus dem Zielcommit, nie aus dem Produktbaum."""

    commit = str(expected_commit or "").strip().lower()
    if not _FULL_SHA1_RE.fullmatch(commit):
        raise RuntimeError("Erwartete Release-SHA ist für den Commit-Leser ungültig")
    reader_user = _bootstrap_repository_reader_user(root)
    object_format = _bootstrap_run_git(
        root,
        "rev-parse",
        "--show-object-format",
        run_as_user=reader_user,
        timeout=15,
    )
    if object_format.returncode != 0 or object_format.stdout.strip() != b"sha1":
        raise RuntimeError("Git-Repository verwendet nicht das gebundene SHA-1-Objektformat")
    replace_refs = _bootstrap_run_git(
        root,
        "for-each-ref",
        "--format=%(refname)",
        "refs/replace/",
        run_as_user=reader_user,
        timeout=15,
    )
    if replace_refs.returncode != 0 or replace_refs.stdout.strip():
        raise RuntimeError("Git-Repository enthält Replace-Refs")
    grafts = root / ".git" / "info" / "grafts"
    if grafts.exists() or grafts.is_symlink():
        raise RuntimeError("Git-Repository enthält eine Legacy-Graft-Datei")

    commit_payload = _bootstrap_read_object(
        root,
        commit,
        b"commit",
        run_as_user=reader_user,
        maximum_bytes=16 * 1024 * 1024,
    )
    first_line = commit_payload.split(b"\n", 1)[0]
    if not first_line.startswith(b"tree "):
        raise RuntimeError("Release-Commit besitzt keinen gebundenen Wurzelbaum")
    try:
        root_tree_id = first_line[5:].decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Release-Wurzelbaum besitzt keine ASCII-Objekt-ID") from exc
    if not _FULL_SHA1_RE.fullmatch(root_tree_id):
        raise RuntimeError("Release-Wurzelbaum besitzt keine volle Objekt-ID")
    root_tree = _bootstrap_parse_tree(
        _bootstrap_read_object(
            root,
            root_tree_id,
            b"tree",
            run_as_user=reader_user,
            maximum_bytes=16 * 1024 * 1024,
        )
    )
    installer_entry = root_tree.get(b"Installer")
    if installer_entry is None or installer_entry[0] != "40000":
        raise RuntimeError("Release-Commit besitzt keinen gebundenen Installer-Baum")
    installer_tree = _bootstrap_parse_tree(
        _bootstrap_read_object(
            root,
            installer_entry[1],
            b"tree",
            run_as_user=reader_user,
            maximum_bytes=16 * 1024 * 1024,
        )
    )
    helper_entry = installer_tree.get(b"git_commit_reader.py")
    if helper_entry is None or helper_entry[0] not in {"100644", "100755"}:
        raise RuntimeError(
            "Release-Commit besitzt keinen regulären gebundenen Git-Commit-Leser"
        )
    return _bootstrap_read_object(
        root,
        helper_entry[1],
        b"blob",
        run_as_user=reader_user,
        maximum_bytes=_SNAPSHOT_MAX_FILE_BYTES,
    )


def _commit_reader_api(
    root: Path,
    expected_commit: str,
) -> tuple[object, object, object]:
    """Lädt Commit-Leser-Code erst nach Objektbindung direkt in den Speicher."""

    commit = str(expected_commit or "").strip().lower()
    cache_key = (str(root), commit)
    cached = _COMMIT_READER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    payload = _bootstrap_commit_reader_payload(root, commit)
    module_name = f"_e3dc_bound_git_commit_reader_{commit}"
    namespace = {
        "__builtins__": __builtins__,
        "__file__": f"<git:{commit}:{_COMMIT_READER_PATH}>",
        "__name__": module_name,
        "__package__": None,
    }
    try:
        code = compile(
            payload,
            namespace["__file__"],
            "exec",
            dont_inherit=True,
            optimize=0,
        )
        exec(code, namespace)
    except Exception as exc:
        raise RuntimeError(
            "Gebundener Git-Commit-Leser konnte nicht geladen werden"
        ) from exc
    api = (
        namespace.get("read_commit_entries"),
        namespace.get("repository_git_reader_user"),
        namespace.get("run_isolated_git"),
    )
    if not all(callable(item) for item in api):
        raise RuntimeError("Gebundener Git-Commit-Leser besitzt keine vollständige API")
    _COMMIT_READER_CACHE[cache_key] = api
    return api


def _commit_execution_entries(root: Path, expected_commit: str) -> dict[str, tuple[bytes, int]]:
    required = set(_SNAPSHOT_ROOT_FILES) | set(_FINALIZER_FILES)
    read_commit_entries, repository_git_reader_user, _run_isolated_git = (
        _commit_reader_api(root, expected_commit)
    )
    reader_user = repository_git_reader_user(root)
    return read_commit_entries(
        root,
        str(expected_commit or "").strip().lower(),
        (*_SNAPSHOT_ROOT_FILES, "Installer"),
        required_paths=required,
        run_as_user=reader_user,
        maximum_files=_SNAPSHOT_MAX_FILES,
        maximum_file_bytes=_SNAPSHOT_MAX_FILE_BYTES,
        maximum_total_bytes=_SNAPSHOT_MAX_TOTAL_BYTES,
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


def _bind_legacy_product_invocation(
    root: Path,
    args: argparse.Namespace,
) -> dict[str, tuple[bytes, int]]:
    """Bindet den direkten Aufruf veröffentlichter Updater vor dem Snapshotwechsel."""

    install_root = str(os.environ.get("E3DC_INSTALL_ROOT") or "")
    if (
        not install_root
        or os.path.abspath(install_root) != str(root)
        or os.path.realpath(install_root) != str(root)
    ):
        raise RuntimeError("Direkter Target-Finalizer besitzt keine gebundene Installationswurzel")
    if any(
        os.environ.get(name)
        for name in (
            "E3DC_BOOTSTRAP_ROOT",
            "E3DC_BOOTSTRAP_RUNNER_ROOT",
            "E3DC_BOOTSTRAP_USER",
            "E3DC_BOOTSTRAP_VENV",
            "PYTHONHOME",
            "PYTHONPATH",
        )
    ):
        raise RuntimeError("Direkter Target-Finalizer besitzt einen widersprüchlichen Bootstrap-Kontext")

    entries = _commit_execution_entries(root, args.expected_release_sha)
    _read_entries, repository_git_reader_user, run_isolated_git = (
        _commit_reader_api(root, args.expected_release_sha)
    )
    version_bytes = _read_regular_nofollow(root / "VERSION", maximum=256)
    if version_bytes != entries["VERSION"][0]:
        raise RuntimeError("Produktversion weicht vom freigegebenen Ziel-Commit ab")
    try:
        version = version_bytes.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("Produktversion ist nicht als UTF-8 gebunden") from exc
    expected_tag = f"v{version}"
    if (
        not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[a-z]?", version)
        or args.expected_release_tag != expected_tag
    ):
        raise RuntimeError("Release-Tag und Produktversion sind nicht kohärent")

    head = run_isolated_git(
        root,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        run_as_user=repository_git_reader_user(root),
        timeout=15,
    )
    actual_head = bytes(head.stdout or b"").decode("ascii", errors="strict").strip().lower()
    if head.returncode != 0 or actual_head != args.expected_release_sha.lower():
        raise RuntimeError("Direkter Target-Finalizer sieht nicht den freigegebenen Ziel-Commit")

    root_owner = root.lstat().st_uid
    for relative_path in _FINALIZER_FILES:
        target = root / relative_path
        metadata = target.lstat()
        if (
            target.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in {0, root_owner}
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError(
                f"Direktes Target-Modul besitzt unzulässige Metadaten: {relative_path}"
            )
        if _read_regular_nofollow(
            target,
            maximum=_SNAPSHOT_MAX_FILE_BYTES,
        ) != entries[relative_path][0]:
            raise RuntimeError(
                f"Direktes Target-Modul weicht vom freigegebenen Commit ab: {relative_path}"
            )
    return entries


def _trusted_snapshot_parent(parent: Path) -> Path:
    if not parent.is_absolute():
        raise RuntimeError("Snapshot-Elternverzeichnis muss absolut sein")
    canonical = parent.resolve(strict=True)
    metadata = parent.lstat()
    if (
        canonical != parent
        or parent.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError("Snapshot-Elternverzeichnis ist nicht vertrauenswürdig")
    descriptor = os.open(
        str(parent),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if _file_identity(os.fstat(descriptor)) != _file_identity(metadata):
            raise RuntimeError("Snapshot-Elternverzeichnis driftete während der Bindung")
    finally:
        os.close(descriptor)
    return parent


def _trusted_same_filesystem_snapshot_parent(product_root: Path) -> Path:
    """Findet oberhalb des Produkts einen root-kontrollierten Ort auf demselben FS."""

    root = Path(os.path.abspath(product_root)).resolve(strict=True)
    product_device = root.lstat().st_dev
    candidate = root.parent
    while True:
        metadata = candidate.lstat()
        if (
            not candidate.is_symlink()
            and stat.S_ISDIR(metadata.st_mode)
            and metadata.st_dev == product_device
            and metadata.st_uid == os.geteuid()
            and metadata.st_gid == os.getegid()
            and not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            and candidate.resolve(strict=True) == candidate
        ):
            return _trusted_snapshot_parent(candidate)
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    raise RuntimeError(
        "Kein root-kontrollierter Snapshot-Ort auf dem Produkt-Dateisystem verfügbar"
    )


def _remove_compat_execution_snapshot(snapshot_root: Path, parent: Path) -> None:
    root = Path(os.path.abspath(snapshot_root))
    bound_parent = Path(os.path.abspath(parent))
    if (
        root.parent != bound_parent
        or not root.name.startswith(_COMPAT_SNAPSHOT_PREFIX)
    ):
        raise RuntimeError("Kompatibilitäts-Snapshot liegt außerhalb der gebundenen Wurzel")
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return
    if (
        root.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
    ):
        raise RuntimeError("Kompatibilitäts-Snapshot ist vor der Bereinigung nicht gebunden")
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in dirnames:
            child = directory_path / name
            child_metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISDIR(child_metadata.st_mode):
                raise RuntimeError("Kompatibilitäts-Snapshot enthält vor der Bereinigung einen Symlink")
        for name in filenames:
            child = directory_path / name
            child_metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISREG(child_metadata.st_mode):
                raise RuntimeError("Kompatibilitäts-Snapshot enthält vor der Bereinigung eine Fremddatei")
        os.chmod(directory_path, 0o700)
    shutil.rmtree(root)


def _cleanup_stale_compat_snapshots(parent: Path) -> int:
    """Bereinigt unter exklusivem Lock ausschließlich sichere Compat-Reste."""

    _required_update_lock_fd()
    bound_parent = _trusted_snapshot_parent(parent)
    stale_roots = []
    with os.scandir(bound_parent) as entries:
        for entry in entries:
            if not entry.name.startswith(_COMPAT_SNAPSHOT_PREFIX):
                continue
            metadata = entry.stat(follow_symlinks=False)
            if (
                entry.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != os.getegid()
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise RuntimeError(
                    f"Kompatibilitäts-Snapshotrest ist unsicher: {entry.name}"
                )
            root = Path(os.path.abspath(entry.path))
            if root.parent != bound_parent:
                raise RuntimeError("Kompatibilitäts-Snapshotrest verlässt den Elternpfad")
            for directory, dirnames, filenames in os.walk(
                root,
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
                        f"Kompatibilitäts-Snapshotrest ist nicht gebunden: {entry.name}"
                    )
                for name in dirnames:
                    child = Path(directory, name)
                    child_metadata = child.lstat()
                    if child.is_symlink() or not stat.S_ISDIR(child_metadata.st_mode):
                        raise RuntimeError(
                            f"Kompatibilitäts-Snapshotrest enthält Fremdpfad: {entry.name}"
                        )
                for name in filenames:
                    child = Path(directory, name)
                    child_metadata = child.lstat()
                    if (
                        child.is_symlink()
                        or not stat.S_ISREG(child_metadata.st_mode)
                        or child_metadata.st_nlink != 1
                        or child_metadata.st_uid != os.geteuid()
                        or child_metadata.st_gid != os.getegid()
                        or child_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                    ):
                        raise RuntimeError(
                            f"Kompatibilitäts-Snapshotrest enthält unsichere Datei: {entry.name}"
                        )
            stale_roots.append(root)
    for stale_root in stale_roots:
        _remove_compat_execution_snapshot(stale_root, bound_parent)
    return len(stale_roots)


def _create_compat_execution_snapshot(
    entries: dict[str, tuple[bytes, int]],
    product_root: Path,
    expected_commit: str,
    *,
    snapshot_parent: Path = _COMPAT_SNAPSHOT_PARENT,
) -> Path:
    """Erzeugt den einmaligen, versiegelten Übergang für veröffentlichte Alt-Updater."""

    parent = _trusted_snapshot_parent(snapshot_parent)
    snapshot_root = Path(
        tempfile.mkdtemp(
            prefix=_COMPAT_SNAPSHOT_PREFIX,
            dir=str(parent),
        )
    )
    directories = {snapshot_root}
    try:
        root_metadata = snapshot_root.lstat()
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.geteuid()
            or root_metadata.st_gid != os.getegid()
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise RuntimeError("Kompatibilitäts-Snapshot wurde nicht privat erzeugt")

        for relative_path, (payload, final_mode) in sorted(entries.items()):
            if final_mode not in {0o444, 0o555}:
                raise RuntimeError("Kompatibilitäts-Snapshot enthält einen beschreibbaren Zielmodus")
            target = snapshot_root / relative_path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            current = target.parent
            while current != snapshot_root:
                directories.add(current)
                current = current.parent
            descriptor = os.open(
                str(target),
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                view = memoryview(payload)
                written = 0
                while written < len(view):
                    count = os.write(descriptor, view[written:])
                    if count <= 0:
                        raise RuntimeError(
                            "Kompatibilitäts-Snapshot konnte einen Blob nicht vollständig schreiben"
                        )
                    written += count
                os.fsync(descriptor)
                os.fchmod(descriptor, final_mode)
                sealed = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(sealed.st_mode)
                    or sealed.st_nlink != 1
                    or sealed.st_uid != os.geteuid()
                    or sealed.st_gid != os.getegid()
                    or stat.S_IMODE(sealed.st_mode) != final_mode
                    or sealed.st_size != len(payload)
                ):
                    raise RuntimeError(
                        "Kompatibilitäts-Snapshot konnte eine Datei nicht versiegeln"
                    )
            finally:
                os.close(descriptor)

        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            os.chmod(directory, 0o555)
        _bind_execution_snapshot(snapshot_root, product_root, expected_commit)
        return snapshot_root
    except Exception:
        _remove_compat_execution_snapshot(snapshot_root, parent)
        raise


def _bind_execution_snapshot(
    snapshot_root: Path,
    product_root: Path,
    expected_commit: str,
    *,
    require_product_target_files: bool = True,
) -> None:
    expected = _commit_execution_entries(product_root, expected_commit)
    expected_directories = {""}
    for relative_path in expected:
        parts = Path(relative_path).parts
        for length in range(1, len(parts)):
            expected_directories.add(Path(*parts[:length]).as_posix())

    actual_directories = {""}
    actual_files = set()
    for directory, dirnames, filenames in os.walk(snapshot_root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        relative_directory = (
            "" if directory_path == snapshot_root
            else directory_path.relative_to(snapshot_root).as_posix()
        )
        metadata = directory_path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o555
        ):
            raise RuntimeError("Ausführungssnapshot besitzt ein fremdes oder beschreibbares Verzeichnis")
        actual_directories.add(relative_directory)
        for name in list(dirnames):
            child = directory_path / name
            child_metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISDIR(child_metadata.st_mode):
                raise RuntimeError("Ausführungssnapshot besitzt eine Symlink-/Nichtverzeichniskomponente")
        for name in filenames:
            child = directory_path / name
            child_metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISREG(child_metadata.st_mode):
                raise RuntimeError("Ausführungssnapshot besitzt eine nicht reguläre Datei")
            actual_files.add(child.relative_to(snapshot_root).as_posix())

    if actual_directories != expected_directories or actual_files != set(expected):
        raise RuntimeError("Ausführungssnapshot besitzt nicht den exakt gebundenen Dateibaum")

    for relative_path, (payload, expected_mode) in expected.items():
        target = snapshot_root / relative_path
        metadata = target.lstat()
        if (
            metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != expected_mode
            or metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError(f"Ausführungssnapshot besitzt unzulässige Metadaten: {relative_path}")
        if _read_regular_nofollow(target, maximum=_SNAPSHOT_MAX_FILE_BYTES) != payload:
            raise RuntimeError(f"Ausführungssnapshot weicht vom Ziel-Commit ab: {relative_path}")

    if require_product_target_files:
        for relative_path in _FINALIZER_FILES:
            if _read_regular_nofollow(product_root / relative_path) != expected[relative_path][0]:
                raise RuntimeError(f"Target-Modul weicht vom freigegebenen Commit ab: {relative_path}")


def _bind_compat_bridge_snapshot(
    snapshot_root: Path,
    expected_sha256: str,
) -> None:
    """Bindet den bewusst minimalen Transport-Runner byte- und modusgenau."""

    expected_digest = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise RuntimeError(
            "Kompatibilitäts-Runner besitzt keine gültige SHA-256-Bindung"
        )
    root = Path(os.path.abspath(snapshot_root))
    if root.resolve(strict=True) != root:
        raise RuntimeError(
            "Kompatibilitäts-Runner besitzt keinen kanonischen Snapshotpfad"
        )

    actual_directories = set()
    actual_files = set()
    for directory, dirnames, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        directory_path = Path(directory)
        relative_directory = (
            ""
            if directory_path == root
            else directory_path.relative_to(root).as_posix()
        )
        metadata = directory_path.lstat()
        if (
            directory_path.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != 0o555
        ):
            raise RuntimeError(
                "Kompatibilitäts-Runner besitzt ein fremdes oder "
                "beschreibbares Verzeichnis"
            )
        actual_directories.add(relative_directory)
        for name in list(dirnames):
            child = directory_path / name
            child_metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISDIR(child_metadata.st_mode):
                raise RuntimeError(
                    "Kompatibilitäts-Runner enthält eine "
                    "Symlink-/Nichtverzeichniskomponente"
                )
        for name in filenames:
            child = directory_path / name
            child_metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISREG(child_metadata.st_mode):
                raise RuntimeError(
                    "Kompatibilitäts-Runner enthält eine nicht reguläre Datei"
                )
            actual_files.add(child.relative_to(root).as_posix())

    relative_path = "Installer/release_finalize.py"
    if (
        actual_directories != {"", "Installer"}
        or actual_files != {relative_path}
    ):
        raise RuntimeError(
            "Kompatibilitäts-Runner besitzt nicht den exakt minimalen Dateibaum"
        )
    target = root / relative_path
    metadata = target.lstat()
    if (
        metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o444
        or metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError(
            "Kompatibilitäts-Runner besitzt unzulässige Dateimetadaten"
        )
    payload = _read_regular_nofollow(
        target,
        maximum=_SNAPSHOT_MAX_FILE_BYTES,
    )
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        raise RuntimeError(
            "Kompatibilitäts-Runner weicht von seiner SHA-256-Bindung ab"
        )
    script = _regular_nofollow(
        Path(os.path.abspath(__file__)),
        "Kompatibilitäts-Runner",
    )
    if (
        script != target
        or _file_identity(script.lstat()) != _file_identity(metadata)
    ):
        raise RuntimeError(
            "Ausgeführter Kompatibilitäts-Runner ist nicht der gebundene Blob"
        )


def _parse_args() -> argparse.Namespace:
    if "--systemd-finalizer-wrapper" in sys.argv[1:]:
        parser = argparse.ArgumentParser(
            description="E3DC-Control systemd-Finalizer-Wrapper"
        )
        parser.add_argument("--systemd-finalizer-wrapper", action="store_true", required=True)
        parser.add_argument("--install-path", required=True)
        parser.add_argument("--execution-root", required=True)
        parser.add_argument("--expected-release-sha", required=True)
        parser.add_argument("--expected-install-user", required=True)
        parser.add_argument("--update-safety-transaction", required=True)
        parser.add_argument("--update-safety-receipt-sha256", required=True)
        parser.add_argument("--update-safety-service-unit", required=True)
        parser.add_argument("--update-safety-runtime-directory", required=True)
        parser.add_argument("--update-safety-token-path", required=True)
        parser.add_argument("--expected-lock-device", required=True, type=int)
        parser.add_argument("--expected-lock-inode", required=True, type=int)
        parser.add_argument("finalizer_argv", nargs=argparse.REMAINDER)
        return parser.parse_args()

    if "--compat-target-updater-handoff" in sys.argv[1:]:
        parser = argparse.ArgumentParser(
            description="E3DC-Control Ziel-Updater-Kompatibilitäts-Handoff"
        )
        parser.add_argument(
            "--compat-target-updater-handoff",
            action="store_true",
            required=True,
        )
        parser.add_argument("--target-execution-root", required=True)
        parser.add_argument("--compat-bridge-sha256", required=True)
        parser.add_argument("--install-path", required=True)
        parser.add_argument("--expected-release-sha", required=True)
        parser.add_argument("--expected-release-tag", required=True)
        parser.add_argument("--requested-release-tag", default="")
        parser.add_argument("--reinstall-current", action="store_true")
        parser.add_argument(
            "--expected-ha-role",
            required=True,
            choices=("off", "master", "slave", "shadow"),
        )
        return parser.parse_args()

    if "--target-updater-handoff" in sys.argv[1:]:
        parser = argparse.ArgumentParser(description="E3DC-Control Ziel-Updater-Handoff")
        parser.add_argument("--target-updater-handoff", action="store_true", required=True)
        parser.add_argument("--install-path", required=True)
        parser.add_argument("--expected-release-sha", required=True)
        parser.add_argument("--expected-release-tag", required=True)
        parser.add_argument("--requested-release-tag", default="")
        parser.add_argument("--reinstall-current", action="store_true")
        parser.add_argument(
            "--expected-ha-role",
            required=True,
            choices=("off", "master", "slave", "shadow"),
        )
        return parser.parse_args()

    parser = argparse.ArgumentParser(description="E3DC-Control Release-Finalizer")
    parser.add_argument("--install-path", required=True)
    parser.add_argument("--expected-release-sha", required=True)
    parser.add_argument("--expected-release-tag", required=True)
    parser.add_argument("--expected-ha-role", required=True)
    parser.add_argument("--expected-config-state", required=True)
    parser.add_argument("--expected-config-sha256", required=True)
    parser.add_argument("--expected-units-sha256", required=True)
    parser.add_argument("--expected-legacy-activity", required=True)
    parser.add_argument(
        "--expected-venv-state",
        required=True,
        choices=("present", "missing", "unused"),
    )
    parser.add_argument("--expected-venv-path", required=True)
    parser.add_argument("--update-safety-transaction", default="")
    parser.add_argument("--update-safety-receipt-sha256", default="")
    parser.add_argument("--update-safety-service-unit", default="")
    parser.add_argument("--update-safety-runtime-directory", default="")
    parser.add_argument("--update-safety-token-path", default="")
    return parser.parse_args()


def _bind_wrapper_receipt(args: argparse.Namespace) -> None:
    path = _UPDATE_SAFETY_RECEIPT_PATH
    if path.parent != Path("/var/lib/e3dc-update-safety"):
        raise RuntimeError("Systemd-Wrapper besitzt keinen kanonischen Receiptpfad")
    _assert_root_controlled_directory_chain(path.parent)
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeError("Systemd-Wrapper-Receipt besitzt unsichere Metadaten")
    payload = _read_regular_nofollow(path, maximum=256 * 1024)
    if hashlib.sha256(payload).hexdigest() != args.update_safety_receipt_sha256:
        raise RuntimeError("Systemd-Wrapper-Receipt driftete vom Parent-Digest")
    try:
        record = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Systemd-Wrapper-Receipt ist nicht lesbar") from exc
    if (
        not isinstance(record, dict)
        or (
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("ascii")
        != payload
        or set(record)
        != {
            "schema",
            "state",
            "transaction_id",
            "target",
            "backup",
            "bootblock",
            "finalizer",
        }
    ):
        raise RuntimeError("Systemd-Wrapper-Receipt besitzt kein kanonisches Schema")
    finalizer = record.get("finalizer") if isinstance(record, dict) else None
    target = record.get("target") if isinstance(record, dict) else None
    backup = record.get("backup") if isinstance(record, dict) else None
    bootblock = record.get("bootblock") if isinstance(record, dict) else None
    units = bootblock.get("units") if isinstance(bootblock, dict) else None
    created = bootblock.get("created_directories") if isinstance(bootblock, dict) else None
    identities = bootblock.get("dropin_identities") if isinstance(bootblock, dict) else None
    expected_dropin = (
        "# E3DC_UPDATE_SAFETY_V1\n"
        "[Unit]\n"
        f"BindsTo={args.update_safety_service_unit}\n"
        f"After={args.update_safety_service_unit}\n"
        f"ConditionPathExists=|!{_UPDATE_SAFETY_MARKER_PATH}\n"
        f"ConditionPathExists=|{args.update_safety_token_path}\n"
    ).encode("utf-8")
    if (
        not isinstance(finalizer, dict)
        or not isinstance(target, dict)
        or not isinstance(backup, dict)
        or not isinstance(bootblock, dict)
        or set(finalizer) != {"unit", "runtime_directory", "token_path"}
        or set(target) != {"commit", "tag", "role"}
        or set(backup) != {"dir", "dev", "ino", "id", "manifest_sha256"}
        or set(bootblock)
        != {
            "units",
            "created_directories",
            "dropin_payload_sha256",
            "dropin_identities",
        }
        or record.get("schema") != _UPDATE_SAFETY_SCHEMA
        or record.get("state") != "pending"
        or record.get("transaction_id") != args.update_safety_transaction
        or not re.fullmatch(r"[0-9a-f]{64}", args.update_safety_transaction)
        or target.get("commit") != args.expected_release_sha
        or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+[a-z]?", str(target.get("tag") or ""))
        or target.get("role") not in {"off", "master", "slave", "shadow"}
        or not os.path.isabs(str(backup.get("dir") or ""))
        or not isinstance(backup.get("dev"), int)
        or int(backup.get("dev", -1)) < 0
        or not isinstance(backup.get("ino"), int)
        or int(backup.get("ino", 0)) <= 0
        or not str(backup.get("id") or "")
        or not re.fullmatch(r"[0-9a-f]{64}", str(backup.get("manifest_sha256") or ""))
        or not isinstance(units, list)
        or not units
        or len(units) != len(set(units))
        or any(not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", str(unit)) for unit in units)
        or not isinstance(created, list)
        or len(created) != len(set(created))
        or not set(created).issubset(set(units))
        or not isinstance(identities, list)
        or len(identities) != len(units)
        or {
            str(item[0])
            for item in identities
            if isinstance(item, list)
            and len(item) == 3
            and isinstance(item[1], int)
            and isinstance(item[2], int)
            and item[1] >= 0
            and item[2] > 0
        }
        != set(units)
        or bootblock.get("dropin_payload_sha256")
        != hashlib.sha256(expected_dropin).hexdigest()
        or finalizer.get("unit") != args.update_safety_service_unit
        or finalizer.get("runtime_directory") != args.update_safety_runtime_directory
        or finalizer.get("token_path") != args.update_safety_token_path
    ):
        raise RuntimeError("Systemd-Wrapper-Receipt widerspricht seinem argv-Vertrag")


def _wrapper_systemd_properties(args: argparse.Namespace) -> tuple[str, str]:
    names = (
        "Id", "LoadState", "ActiveState", "MainPID", "InvocationID",
        "ControlGroup", "FragmentPath", "DropInPaths", "Transient", "Type", "ExitType",
        "KillMode", "Restart", "User", "Group", "DynamicUser",
        "WorkingDirectory", "UMask", "Environment",
        "RuntimeDirectory", "RuntimeDirectoryMode",
        "RuntimeDirectoryPreserve", "RuntimeMaxUSec", "TimeoutStopUSec",
        "SendSIGKILL", "OOMPolicy",
    )
    completed = subprocess.run(
        [
            "/usr/bin/systemctl",
            "show",
            "--no-pager",
            *(f"--property={name}" for name in names),
            args.update_safety_service_unit,
        ],
        cwd="/",
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
        text=True,
    )
    values = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator != "=" or key not in names or key in values or value != value.strip():
            values = {}
            break
        values[key] = value
    invocation = str(os.environ.get("INVOCATION_ID") or "")
    expected = {
        "Id": args.update_safety_service_unit,
        "LoadState": "loaded",
        "ActiveState": "active",
        "MainPID": str(os.getpid()),
        "InvocationID": invocation,
        "FragmentPath": f"/run/systemd/transient/{args.update_safety_service_unit}",
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
        "RuntimeDirectory": args.update_safety_runtime_directory,
        "RuntimeDirectoryMode": "0700",
        "RuntimeDirectoryPreserve": "no",
        "RuntimeMaxUSec": "35min",
        "TimeoutStopUSec": "15s",
        "SendSIGKILL": "yes",
        "OOMPolicy": "stop",
    }
    if (
        completed.returncode != 0
        or completed.stderr
        or set(values) != set(names)
        or not re.fullmatch(r"[0-9a-f]{32}", invocation)
        or any(values.get(key) != value for key, value in expected.items())
    ):
        raise RuntimeError("Systemd-Wrapper-Serviceproperties drifteten")
    expected_environment = (
        f"E3DC_BOOTSTRAP_ROOT={args.install_path}",
        f"E3DC_BOOTSTRAP_RUNNER_ROOT={args.execution_root}",
        f"E3DC_BOOTSTRAP_USER={args.expected_install_user}",
        f"E3DC_INSTALL_ROOT={args.install_path}",
        "PYTHONNOUSERSITE=1",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONUNBUFFERED=1",
        "LC_ALL=C.UTF-8",
        "LANG=C.UTF-8",
    )
    try:
        actual_environment = tuple(shlex.split(values["Environment"]))
    except ValueError as exc:
        raise RuntimeError("Systemd-Wrapper-Environment ist unlesbar") from exc
    if actual_environment != expected_environment:
        raise RuntimeError("Systemd-Wrapper-Environment driftete")
    control_group = values["ControlGroup"]
    if (
        not control_group.startswith("/")
        or not control_group.endswith("/" + args.update_safety_service_unit)
    ):
        raise RuntimeError("Systemd-Wrapper-cgroup ist nicht kanonisch")
    own_cgroups = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    if not any(line.partition("::")[2] == control_group for line in own_cgroups):
        raise RuntimeError("Systemd-Wrapper läuft nicht in der gebundenen cgroup")
    runtime = Path("/run") / args.update_safety_runtime_directory
    runtime_metadata = runtime.lstat()
    if (
        runtime.is_symlink()
        or not stat.S_ISDIR(runtime_metadata.st_mode)
        or runtime_metadata.st_uid != 0
        or runtime_metadata.st_gid != 0
        or stat.S_IMODE(runtime_metadata.st_mode) != 0o700
        or str(runtime / "start.token") != args.update_safety_token_path
    ):
        raise RuntimeError("Systemd-Wrapper-RuntimeDirectory driftete")
    return invocation, control_group


def _run_systemd_finalizer_wrapper(args: argparse.Namespace) -> int:
    """Übernimmt fd0 als dasselbe flock-OFD und exec't den versiegelten Finalizer."""

    if os.geteuid() != 0:
        raise RuntimeError("Systemd-Finalizer-Wrapper benötigt Root")
    root = _bound_product_root(args.install_path)
    execution_root = _bound_execution_root(root)
    if str(execution_root) != os.path.realpath(args.execution_root):
        raise RuntimeError("Systemd-Wrapper-Ausführungssnapshot driftete")
    _bind_execution_snapshot(execution_root, root, args.expected_release_sha)
    system_python = _trusted_system_python()
    _bind_wrapper_receipt(args)
    invocation, _control_group = _wrapper_systemd_properties(args)

    lock_fd = fcntl.fcntl(0, fcntl.F_DUPFD, 10)
    try:
        os.set_inheritable(lock_fd, True)
        _validate_update_lock_fd(lock_fd)
        lock_metadata = os.fstat(lock_fd)
        if (lock_metadata.st_dev, lock_metadata.st_ino) != (
            args.expected_lock_device,
            args.expected_lock_inode,
        ):
            raise RuntimeError("Systemd-Wrapper erhielt nicht das Parent-Lock-OFD")
        # LOCK_NB auf derselben offenen Dateibeschreibung muss erfolgreich
        # bleiben, obwohl der Parent den Exklusivlock weiterhin hält.
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        null_fd = os.open("/dev/null", os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.dup2(null_fd, 0)
        finally:
            os.close(null_fd)
        if os.readlink("/proc/self/fd/0") != "/dev/null":
            raise RuntimeError("Systemd-Wrapper konnte stdin nicht auf /dev/null binden")

        finalizer_argv = list(args.finalizer_argv)
        if finalizer_argv[:1] == ["--"]:
            finalizer_argv = finalizer_argv[1:]
        required_pairs = {
            "--install-path": str(root),
            "--expected-release-sha": args.expected_release_sha,
            "--update-safety-transaction": args.update_safety_transaction,
            "--update-safety-receipt-sha256": args.update_safety_receipt_sha256,
            "--update-safety-service-unit": args.update_safety_service_unit,
            "--update-safety-runtime-directory": args.update_safety_runtime_directory,
            "--update-safety-token-path": args.update_safety_token_path,
        }
        for name, value in required_pairs.items():
            if finalizer_argv.count(name) != 1:
                raise RuntimeError(f"Systemd-Wrapper-Finalizerargv fehlt {name}")
            index = finalizer_argv.index(name)
            if index + 1 >= len(finalizer_argv) or finalizer_argv[index + 1] != value:
                raise RuntimeError(f"Systemd-Wrapper-Finalizerargv driftete bei {name}")
        finalizer_script = _regular_nofollow(
            Path(os.path.abspath(__file__)),
            "Systemd-Target-Finalizer",
        )
        environment = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "E3DC_BOOTSTRAP_ROOT": str(root),
            "E3DC_BOOTSTRAP_RUNNER_ROOT": str(execution_root),
            "E3DC_BOOTSTRAP_USER": args.expected_install_user,
            "E3DC_INSTALL_ROOT": str(root),
            _UPDATE_LOCK_ENV: str(lock_fd),
            _UPDATE_FINALIZER_INVOCATION_ENV: invocation,
        }
        os.execve(
            system_python,
            [
                system_python,
                "-I",
                "-B",
                "-u",
                str(finalizer_script),
                *finalizer_argv,
            ],
            environment,
        )
    finally:
        os.close(lock_fd)
    return 1


def _run_target_updater_handoff(
    root: Path,
    args: argparse.Namespace,
    *,
    legacy_snapshot_install_user: str | None,
    legacy_snapshot_repository_identity: (
        tuple[tuple[int, ...], tuple[int, ...]] | None
    ),
) -> int:
    """Startet die Transaktion aus dem versiegelten Updater des Ziel-Commits."""

    execution_root = _bound_execution_root(root)
    if execution_root.lstat().st_dev != root.lstat().st_dev:
        raise RuntimeError("Ziel-Updater liegt nicht auf dem Produkt-Dateisystem")
    _bind_execution_snapshot(
        execution_root,
        root,
        args.expected_release_sha,
        require_product_target_files=False,
    )
    _revalidate_legacy_snapshot_install_user(
        root,
        legacy_snapshot_install_user,
        legacy_snapshot_repository_identity,
        context="Ziel-Updater-Handoff",
    )

    root_text = str(root)
    execution_text = str(execution_root)
    sys.path[:] = [
        execution_text,
        *[
            item
            for item in sys.path
            if item
            and os.path.realpath(item) not in {
                os.path.realpath(root_text),
                os.path.realpath(str(execution_root / "Installer")),
                os.path.realpath(execution_text),
            }
        ],
    ]
    from Installer.update import (  # pylint: disable=import-outside-toplevel
        TARGET_UPDATER_NOOP,
        TARGET_UPDATER_SUCCESS,
        UPDATE_ALREADY_CURRENT,
        execute_verified_target_update,
    )

    update_module = sys.modules.get("Installer.update")
    if (
        update_module is None
        or Path(os.path.abspath(str(getattr(update_module, "__file__", "")))).parent.parent
        != execution_root
        or TARGET_UPDATER_SUCCESS != _TARGET_UPDATER_SUCCESS
        or TARGET_UPDATER_NOOP != _TARGET_UPDATER_NOOP
    ):
        raise RuntimeError("Installer.update wurde nicht aus dem gebundenen Ziel-Updater geladen")

    updated = execute_verified_target_update(
        repo_dir=root_text,
        target_commit=args.expected_release_sha,
        target_tag=args.expected_release_tag,
        requested_target_tag=args.requested_release_tag or None,
        expected_role=args.expected_ha_role,
        reinstall_current=bool(args.reinstall_current),
    )
    if updated is not True and updated != UPDATE_ALREADY_CURRENT:
        raise RuntimeError("Ziel-Updater hat die Release-Transaktion nicht bestätigt")

    # Nach der Transaktion muss auch der Produktbaum exakt den Commit besitzen,
    # während der ursprüngliche Updater-Snapshot unverändert geblieben ist.
    _bind_execution_snapshot(
        execution_root,
        root,
        args.expected_release_sha,
        require_product_target_files=True,
    )
    outcome_marker = (
        TARGET_UPDATER_NOOP
        if updated == UPDATE_ALREADY_CURRENT
        else TARGET_UPDATER_SUCCESS
    )
    print(
        f"{outcome_marker} "
        f"{args.expected_release_sha} {args.expected_release_tag}"
    )
    return 0


def _run_compat_target_updater_handoff(
    root: Path,
    args: argparse.Namespace,
) -> int:
    """Betritt nur den veröffentlichten Alt-Updater aus dem Ziel-Snapshot."""

    bridge_root = _bound_execution_root(root)
    target_root = Path(os.path.abspath(args.target_execution_root))
    if (
        target_root.resolve(strict=True) != target_root
        or target_root in {root, bridge_root}
        or bridge_root == root
        or target_root.lstat().st_dev != root.lstat().st_dev
        or bridge_root.lstat().st_dev != root.lstat().st_dev
    ):
        raise RuntimeError(
            "Ziel- und Kompatibilitäts-Snapshot sind nicht getrennt an "
            "das Produkt-Dateisystem gebunden"
        )
    if (
        os.path.realpath(os.environ.get("E3DC_BOOTSTRAP_ROOT", "")) != str(root)
        or os.path.realpath(
            os.environ.get("E3DC_BOOTSTRAP_RUNNER_ROOT", "")
        ) != str(bridge_root)
        or os.path.realpath(os.environ.get("E3DC_INSTALL_ROOT", "")) != str(root)
    ):
        raise RuntimeError(
            "Kompatibilitäts-Handoff besitzt keinen eindeutigen Bootstrap-Kontext"
        )
    requested_tag = str(args.requested_release_tag or "").strip()
    if requested_tag and requested_tag != args.expected_release_tag:
        raise RuntimeError(
            "Angeforderter und gebundener Ziel-Release-Tag widersprechen sich"
        )
    if requested_tag and bool(args.reinstall_current):
        raise RuntimeError(
            "Rollback und Neuinstallation dürfen nicht vermischt werden"
        )

    _bind_compat_bridge_snapshot(
        bridge_root,
        args.compat_bridge_sha256,
    )
    _bind_execution_snapshot(
        target_root,
        root,
        args.expected_release_sha,
        require_product_target_files=False,
    )
    # Der Transport-Runner hat sich bis hier selbst gebunden. Für den Import
    # des veröffentlichten Zielcodes muss dessen eigener dualer
    # Bootstrap-Vertrag gelten: Produkt bleibt das Ziel, der ausgeführte
    # Release-Root ist jetzt aber der vollständige Ziel-Snapshot. Diese
    # Prozessumgebung endet zusammen mit dem einmaligen Bridgeprozess.
    os.environ["E3DC_BOOTSTRAP_ROOT"] = str(root)
    os.environ["E3DC_BOOTSTRAP_RUNNER_ROOT"] = str(target_root)
    os.environ["E3DC_INSTALL_ROOT"] = str(root)
    if any(
        name == "Installer" or name.startswith("Installer.")
        for name in sys.modules
    ):
        raise RuntimeError(
            "Kompatibilitäts-Handoff besitzt bereits geladene Produktmodule"
        )

    root_text = str(root)
    target_text = str(target_root)
    bridge_text = str(bridge_root)
    excluded = {
        os.path.realpath(root_text),
        os.path.realpath(str(root / "Installer")),
        os.path.realpath(target_text),
        os.path.realpath(str(target_root / "Installer")),
        os.path.realpath(bridge_text),
        os.path.realpath(str(bridge_root / "Installer")),
    }
    sys.path[:] = [
        target_text,
        *[
            item
            for item in sys.path
            if item and os.path.realpath(item) not in excluded
        ],
    ]

    from Installer import update as target_update  # pylint: disable=import-outside-toplevel

    package_module = sys.modules.get("Installer")
    update_module = sys.modules.get("Installer.update")
    if (
        package_module is None
        or update_module is None
        or Path(
            os.path.abspath(str(getattr(package_module, "__file__", "")))
        ).parent.parent
        != target_root
        or Path(
            os.path.abspath(str(getattr(update_module, "__file__", "")))
        ).parent.parent
        != target_root
    ):
        raise RuntimeError(
            "Alt-Updater wurde nicht vollständig aus dem Ziel-Snapshot geladen"
        )
    required_parameters = {
        "headless",
        "target_ref",
        "target_install_path",
        "expected_release_sha",
        "expected_ha_role",
    }
    if not required_parameters.issubset(
        inspect.signature(target_update.update_e3dc).parameters
    ):
        raise RuntimeError(
            "Alt-Updater besitzt nicht den gebundenen Bootstrap-Vertrag"
        )

    updated = target_update.update_e3dc(
        headless=True,
        target_ref=args.expected_release_tag,
        target_install_path=root_text,
        expected_release_sha=args.expected_release_sha,
        expected_ha_role=args.expected_ha_role,
    )
    os.environ["E3DC_BOOTSTRAP_RUNNER_ROOT"] = str(bridge_root)
    if updated is not True:
        raise RuntimeError(
            "Alt-Updater hat die Release-Transaktion nicht bestätigt"
        )

    # Der alte Zielcode besitzt noch keinen nativen Handoff. Nach seiner
    # abgeschlossenen Transaktion müssen daher der Ziel-Snapshot, der
    # Produktbaum und der reine Transport-Runner jeweils erneut exakt binden.
    _bind_execution_snapshot(
        target_root,
        root,
        args.expected_release_sha,
        require_product_target_files=True,
    )
    _bind_compat_bridge_snapshot(
        bridge_root,
        args.compat_bridge_sha256,
    )
    print(
        f"{_TARGET_UPDATER_SUCCESS} "
        f"{args.expected_release_sha} {args.expected_release_tag}"
    )
    return 0


def _run_legacy_product_bridge(
    root: Path,
    args: argparse.Namespace,
) -> int:
    lock_fd = _required_update_lock_fd()
    entries = _bind_legacy_product_invocation(root, args)
    legacy_install_user = _bound_legacy_install_user(root)
    legacy_repository_identity = _legacy_repository_identity(root)
    snapshot_parent = _trusted_same_filesystem_snapshot_parent(root)
    _cleanup_stale_compat_snapshots(snapshot_parent)
    snapshot_root = _create_compat_execution_snapshot(
        entries,
        root,
        args.expected_release_sha,
        snapshot_parent=snapshot_parent,
    )
    try:
        python = _trusted_system_python()
    except Exception:
        _remove_compat_execution_snapshot(snapshot_root, snapshot_parent)
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
    environment["E3DC_BOOTSTRAP_ROOT"] = str(root)
    environment["E3DC_BOOTSTRAP_RUNNER_ROOT"] = str(snapshot_root)
    environment["E3DC_BOOTSTRAP_USER"] = legacy_install_user
    environment["E3DC_INSTALL_ROOT"] = str(root)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    environment["LC_ALL"] = "C.UTF-8"
    environment["LANG"] = "C.UTF-8"
    environment[_UPDATE_LOCK_ENV] = str(lock_fd)

    finalizer = snapshot_root / "Installer" / "release_finalize.py"
    command = [
        python,
        "-I",
        "-B",
        "-u",
        str(finalizer),
        "--install-path",
        str(root),
        "--expected-release-sha",
        args.expected_release_sha,
        "--expected-release-tag",
        args.expected_release_tag,
        "--expected-ha-role",
        args.expected_ha_role,
        "--expected-config-state",
        args.expected_config_state,
        "--expected-config-sha256",
        args.expected_config_sha256,
        "--expected-units-sha256",
        args.expected_units_sha256,
        "--expected-legacy-activity",
        args.expected_legacy_activity,
        "--expected-venv-state",
        args.expected_venv_state,
        "--expected-venv-path",
        args.expected_venv_path,
    ]
    try:
        if (
            _bound_legacy_install_user(root) != legacy_install_user
            or _legacy_repository_identity(root) != legacy_repository_identity
        ):
            raise RuntimeError(
                "Repository oder Installationsbenutzer driftete vor dem Legacy-Zielübergang"
            )
        result = _run_compat_finalizer(
            command,
            environment=environment,
            pass_fds=(lock_fd,),
        )
    finally:
        try:
            _remove_compat_execution_snapshot(snapshot_root, snapshot_parent)
        except Exception as exc:
            sys.stderr.write(
                "WARNUNG: Kompatibilitäts-Snapshot konnte nach dem "
                f"Finalizerlauf nicht bereinigt werden: {exc}\n"
            )

    marker = (
        f"{_FINALIZER_SUCCESS} "
        f"{args.expected_release_sha} {args.expected_release_tag}"
    )
    marker_count = int((result.get("line_counts") or {}).get(marker, 0))
    if (
        result.get("returncode") != 0
        or bool(result.get("timed_out"))
        or marker_count != 1
    ):
        detail = "\n".join(
            part.strip()
            for part in (
                str(result.get("output") or ""),
                str(result.get("error") or ""),
            )
            if part.strip()
        )
        raise RuntimeError(
            "Kompatibilitäts-Snapshot meldete keinen eindeutigen Erfolg: "
            + detail[-4000:]
        )
    return 0


def _main_with_update_lock(
    root: Path,
    args: argparse.Namespace,
    script: Path,
    *,
    legacy_snapshot_install_user: str | None = None,
    legacy_snapshot_repository_identity: (
        tuple[tuple[int, ...], tuple[int, ...]] | None
    ) = None,
) -> int:
    """Führt genau einen bereits kernelgebundenen Finalizerpfad aus."""

    if getattr(args, "compat_target_updater_handoff", False):
        return _run_compat_target_updater_handoff(root, args)
    if getattr(args, "target_updater_handoff", False):
        return _run_target_updater_handoff(
            root,
            args,
            legacy_snapshot_install_user=legacy_snapshot_install_user,
            legacy_snapshot_repository_identity=(
                legacy_snapshot_repository_identity
            ),
        )
    if script.parent.parent == root:
        return _run_legacy_product_bridge(root, args)
    execution_root = _bound_execution_root(root)
    _bind_execution_snapshot(execution_root, root, args.expected_release_sha)
    if legacy_snapshot_install_user is not None:
        _revalidate_legacy_snapshot_install_user(
            root,
            legacy_snapshot_install_user,
            legacy_snapshot_repository_identity,
            context="Legacy-Snapshot-Finalizer",
        )

    root_text = str(root)
    execution_text = str(execution_root)
    sys.path[:] = [
        execution_text,
        *[
            item
            for item in sys.path
            if item
            and os.path.realpath(item) not in {
                os.path.realpath(root_text),
                os.path.realpath(str(execution_root / "Installer")),
                os.path.realpath(execution_text),
            }
        ],
    ]

    from Installer.update import (  # pylint: disable=import-outside-toplevel
        TARGET_FINALIZER_SUCCESS,
        finalize_release_from_target,
    )
    update_module = sys.modules.get("Installer.update")
    if (
        update_module is None
        or Path(os.path.abspath(str(getattr(update_module, "__file__", "")))).parent.parent
        != execution_root
    ):
        raise RuntimeError("Installer.update wurde nicht aus dem versiegelten Snapshot geladen")
    (
        UpdateSafetyPostCommitError,
        UpdateSafetyManagedServiceUnquiescedError,
    ) = _bind_update_safety_exception_types(
        update_module,
        require_native=bool(getattr(args, "update_safety_transaction", "")),
    )

    expected_pending_contract = None
    if getattr(args, "update_safety_transaction", ""):
        try:
            expected_pending_contract = update_module._read_update_safety_contract()
        except BaseException as exc:
            raise UpdateSafetyManagedServiceUnquiescedError(
                "Target-Finalizer kann sein ursprüngliches Pending-Receipt "
                "vor dem privilegierten Mutationspfad nicht binden"
            ) from exc
        if (
            expected_pending_contract is None
            or expected_pending_contract.state != "pending"
            or expected_pending_contract.transaction_id
            != args.update_safety_transaction
            or expected_pending_contract.receipt_sha256
            != args.update_safety_receipt_sha256
            or expected_pending_contract.target_commit
            != args.expected_release_sha
            or expected_pending_contract.target_tag
            != args.expected_release_tag
            or expected_pending_contract.role != args.expected_ha_role
            or expected_pending_contract.finalizer_unit
            != args.update_safety_service_unit
            or expected_pending_contract.runtime_directory
            != args.update_safety_runtime_directory
            or expected_pending_contract.token_path
            != args.update_safety_token_path
        ):
            raise UpdateSafetyManagedServiceUnquiescedError(
                "Target-Finalizer sieht vor dem privilegierten Mutationspfad "
                "nicht sein exaktes ursprüngliches Pending-Receipt"
            )

    from Installer import web_installer as web_installer_module  # pylint: disable=import-outside-toplevel
    if (
        Path(os.path.abspath(str(getattr(web_installer_module, "__file__", "")))).parent.parent
        != execution_root
    ):
        raise RuntimeError("Installer.web_installer wurde nicht aus dem versiegelten Snapshot geladen")

    # Der Ziel-Finalizer kann zusätzlich zur kanonischen sudoers-Datei alte,
    # eindeutig E3DC-eigene Fragmente bereinigen. Veröffentlichte äußere
    # Updater kennen diese dynamische Zielmenge noch nicht zwingend. Deshalb
    # bindet der versiegelte Zielprozess alle tatsächlich berührbaren
    # privilegierten Webflächen selbst und stellt sie bei jedem späteren
    # Finalizerfehler vollständig wieder her.
    sudoers_findings = web_installer_module.sudoers_file_findings()
    privileged_paths = {
        web_installer_module.SERVICE_WRAPPER,
        web_installer_module.WEB_UPDATE_LAUNCHER,
        web_installer_module.SUDOERS_FILE,
    }
    privileged_paths.update(
        Path(str(item.get("file") or ""))
        for item in sudoers_findings.get("repairable_lines", [])
        if str(item.get("file") or "")
    )
    privileged_preimages = [
        web_installer_module._capture_file_preimage(path)
        for path in sorted(privileged_paths, key=lambda item: str(item))
    ]
    postcommit_state = {"commit_attempted": False}

    try:
        finalize_release_from_target(
            repo_dir=root_text,
            execution_root=execution_text,
            target_commit=args.expected_release_sha,
            target_tag=args.expected_release_tag,
            expected_role=args.expected_ha_role,
            expected_config_state=args.expected_config_state,
            expected_config_sha256=args.expected_config_sha256,
            expected_units_sha256=args.expected_units_sha256,
            expected_legacy_activity=args.expected_legacy_activity,
            expected_venv_state=args.expected_venv_state,
            expected_venv_path=args.expected_venv_path,
            update_safety_transaction=args.update_safety_transaction or None,
            update_safety_receipt_sha256=args.update_safety_receipt_sha256 or None,
            update_safety_service_unit=args.update_safety_service_unit or None,
            update_safety_runtime_directory=args.update_safety_runtime_directory or None,
            update_safety_token_path=args.update_safety_token_path or None,
            headless=True,
            privileged_preimages=privileged_preimages,
            postcommit_state=postcommit_state,
        )
        # Der Berechtigungsdurchlauf darf ausschließlich den gebundenen
        # Produktbaum verändern. Der privilegierte Ausführungssnapshot muss
        # über den gesamten Finalizer-Lauf byte- und modusidentisch bleiben.
        _bind_execution_snapshot(execution_root, root, args.expected_release_sha)
    except BaseException as original_error:
        if isinstance(original_error, UpdateSafetyPostCommitError) or bool(
            postcommit_state.get("commit_attempted")
        ):
            # Ab durable committed ist jeder Altstand-Rollback verboten. Der
            # wartende Ziel-Updater räumt ausschließlich eigene Gate-Reste
            # fertig oder lässt sie bewusst fail-closed stehen.
            if isinstance(original_error, UpdateSafetyPostCommitError):
                raise
            raise UpdateSafetyPostCommitError(
                "Target-Finalizerfehler trat nach Eintritt in die konservative "
                "Commit-Attempt-Grenze auf; Altpreimage-Restore ist gesperrt"
            ) from original_error
        if expected_pending_contract is not None:
            try:
                current_contract = update_module._read_update_safety_contract()
            except BaseException as receipt_error:
                raise UpdateSafetyManagedServiceUnquiescedError(
                    "Update-Sicherheitsreceipt ist vor dem privilegierten "
                    "Altpreimage-Restore nicht mehr lesbar; Restore bleibt gesperrt"
                ) from receipt_error
            if current_contract == expected_pending_contract:
                pass
            elif (
                current_contract is not None
                and current_contract.state == "committed"
                and update_module._same_update_safety_transaction_shape(
                    current_contract,
                    expected_pending_contract,
                )
            ):
                raise UpdateSafetyPostCommitError(
                    "Durable committed Receipt verbietet den privilegierten "
                    "Altpreimage-Restore"
                ) from original_error
            else:
                raise UpdateSafetyManagedServiceUnquiescedError(
                    "Ursprüngliches Pending-Receipt driftete vor dem privilegierten "
                    "Altpreimage-Restore; Restore bleibt fail-closed gesperrt"
                ) from original_error
        try:
            restored = web_installer_module._restore_preimages(privileged_preimages)
            syntax = subprocess.run(
                ["/usr/sbin/visudo", "-cf", "/etc/sudoers"],
                cwd="/",
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            if not bool(restored.get("success")) or syntax.returncode != 0:
                detail = bytes(syntax.stderr or syntax.stdout or b"").decode(
                    "utf-8", errors="replace"
                ).strip()
                raise RuntimeError(
                    "Privilegierte Ziel-Preimages konnten nach dem Finalizerfehler "
                    "nicht vollständig wiederhergestellt werden: " + detail[-1000:]
                )
        except Exception as recovery_error:
            raise RuntimeError(
                "Target-Finalizer und privilegierter Rückweg sind fehlgeschlagen: "
                f"{recovery_error}"
            ) from original_error
        raise
    print(
        f"{TARGET_FINALIZER_SUCCESS} "
        f"{args.expected_release_sha} {args.expected_release_tag}"
    )
    return 0


def main() -> int:
    _install_fail_safe_process_streams()
    args = _parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("Release-Finalizer muss mit Root-Rechten laufen")
    if getattr(args, "systemd_finalizer_wrapper", False):
        return _run_systemd_finalizer_wrapper(args)
    root = _bound_product_root(args.install_path)
    script = _regular_nofollow(
        Path(os.path.abspath(__file__)),
        "Target-Finalizer",
    )
    target_handoff_entry = bool(
        getattr(args, "target_updater_handoff", False)
    )
    compat_handoff_entry = bool(
        getattr(args, "compat_target_updater_handoff", False)
    )
    legacy_product_entry = (
        not target_handoff_entry
        and not compat_handoff_entry
        and script.parent.parent == root
    )
    legacy_target_snapshot_entry = (
        not target_handoff_entry
        and not compat_handoff_entry
        and script.parent.parent != root
    )
    if legacy_target_snapshot_entry:
        # Veröffentlichte Updater vor dem globalen Lock übergeben keinen FD.
        # Nur ihr flagloser, bereits commit- und produktgebundener
        # Target-Finalizer darf den Lock selbst anlegen. Nach dem Lock bindet
        # _main_with_update_lock denselben Snapshot erneut und schließt TOCTOU.
        execution_root = _bound_execution_root(root)
        _bind_execution_snapshot(
            execution_root,
            root,
            args.expected_release_sha,
            require_product_target_files=True,
        )
    lock_fd, lock_owned = _acquire_or_inherit_update_lock(
        allow_create=(
            legacy_product_entry
            or legacy_target_snapshot_entry
        ),
    )
    previous_lock_env = os.environ.get(_UPDATE_LOCK_ENV)
    previous_bootstrap_user = os.environ.get("E3DC_BOOTSTRAP_USER")
    os.environ[_UPDATE_LOCK_ENV] = str(lock_fd)
    legacy_snapshot_install_user = None
    legacy_snapshot_repository_identity = None
    try:
        if target_handoff_entry or legacy_target_snapshot_entry:
            (
                legacy_snapshot_install_user,
                legacy_snapshot_repository_identity,
            ) = _bound_legacy_snapshot_install_user(root)
            # Veröffentlichte und private ältere Aufrufer können diese
            # Bindung vor dem Start ihres versiegelten Target-Snapshots
            # entfernen. Der Root-Finalizer rekonstruiert sie ausschließlich
            # aus der erneut gebundenen Repository-Eigentümerstruktur und
            # vertraut geerbten Prozesswerten nicht ohne exakte Gleichheit.
            if previous_bootstrap_user is None:
                os.environ["E3DC_BOOTSTRAP_USER"] = (
                    legacy_snapshot_install_user
                )
            elif previous_bootstrap_user != legacy_snapshot_install_user:
                raise RuntimeError(
                    "Ziel-Snapshot-Nutzer widerspricht dem "
                    "Repository-Eigentümer"
                )
        return _main_with_update_lock(
            root,
            args,
            script,
            legacy_snapshot_install_user=legacy_snapshot_install_user,
            legacy_snapshot_repository_identity=(
                legacy_snapshot_repository_identity
            ),
        )
    finally:
        if previous_bootstrap_user is None:
            os.environ.pop("E3DC_BOOTSTRAP_USER", None)
        else:
            os.environ["E3DC_BOOTSTRAP_USER"] = previous_bootstrap_user
        if previous_lock_env is None:
            os.environ.pop(_UPDATE_LOCK_ENV, None)
        else:
            os.environ[_UPDATE_LOCK_ENV] = previous_lock_env
        if lock_owned:
            # Auf der mit Kindern geteilten offenen Dateibeschreibung nur den
            # Owner-FD schließen. Der Lock endet erst mit dem letzten FD.
            os.close(lock_fd)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # Der aufrufende Bootstrap übernimmt Recovery und Logausgabe.
        print(f"Release-Finalizer fehlgeschlagen: {exc}", file=sys.stderr)
        raise SystemExit(1)
