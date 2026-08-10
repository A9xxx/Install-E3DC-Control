#!/usr/bin/env python3
"""Prozessweiter Einzelschreiber-Vertrag für den Wallbox-Manager.

Der Lock wird nicht gelöscht: Seine Exklusivität beruht ausschließlich auf
dem offenen Kernel-Dateideskriptor. Prozessende, SIGTERM und ``os._exit()``
geben ihn deshalb automatisch frei, ohne einen potenziell rennenden Cleanup.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import time


LOCK_NAMESPACE_ROOT = "/run/e3dc-control"
LOCK_DIRECTORY = f"{LOCK_NAMESPACE_ROOT}/locks"
WALLBOX_MANAGER_LOCK_PATH = f"{LOCK_DIRECTORY}/wallbox_manager.owner.lock"
LOCK_DIRECTORY_MODE = 0o755
LOCK_FILE_MODE = 0o660


class WallboxManagerSingletonError(RuntimeError):
    """Der Manager darf vor einem Hardwarezugriff nicht starten."""

    def __init__(self, reason, path, diagnostic=None):
        self.reason = str(reason or "singleton_unavailable")
        self.path = str(path or "")
        self.diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
        super().__init__(f"{self.reason}: {self.path}")


class WallboxManagerSingleton:
    """Hält einen nicht blockierenden POSIX-``flock`` bis zum Prozessende."""

    def __init__(self, descriptor, path, diagnostic):
        self._descriptor = int(descriptor)
        self.path = str(path)
        self.diagnostic = dict(diagnostic)

    @property
    def acquired(self):
        return self._descriptor >= 0

    def release(self):
        """Explizite Test-/Normalfreigabe; produktiv hält ``run()`` die Instanz."""

        descriptor = self._descriptor
        if descriptor < 0:
            return False
        self._descriptor = -1
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass
        return True

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.release()
        return False


def _process_start_diagnostic():
    """Liefert nur Diagnose; Besitz und Freigabe hängen nie von diesem Wert ab."""

    try:
        with open("/proc/self/stat", "r", encoding="ascii") as proc_stat:
            fields = proc_stat.read().split()
        if len(fields) > 21:
            return fields[21]
    except (OSError, UnicodeError):
        pass
    return "unknown"


def _read_diagnostic(descriptor):
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, 4096)
        value = json.loads(raw.decode("utf-8")) if raw else {}
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def _close_quietly(descriptor):
    try:
        os.close(descriptor)
    except OSError:
        pass


def _www_data_gid(path):
    try:
        import grp

        return int(grp.getgrnam("www-data").gr_gid)
    except (ImportError, KeyError) as exc:
        raise WallboxManagerSingletonError("lock_group_unavailable", path) from exc


def _validate_root_directory(path, *, lock_path):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise WallboxManagerSingletonError("lock_parent_unavailable", lock_path) from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise WallboxManagerSingletonError(
            "lock_parent_not_regular_directory",
            lock_path,
        )
    if info.st_uid != 0 or info.st_gid != 0:
        raise WallboxManagerSingletonError("lock_parent_owner_invalid", lock_path)
    if stat.S_IMODE(info.st_mode) != LOCK_DIRECTORY_MODE:
        raise WallboxManagerSingletonError("lock_parent_mode_invalid", lock_path)
    return info


def _lock_file_contract_valid(info, *, www_data_gid):
    return bool(
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == 0
        and info.st_gid == www_data_gid
        and stat.S_IMODE(info.st_mode) == LOCK_FILE_MODE
    )


def acquire_wallbox_manager_singleton(lock_path):
    """Erwirbt den Wallbox-Manager-Lock oder bricht eindeutig fail-closed ab."""

    path = str(lock_path or "")
    if os.name != "posix":
        raise WallboxManagerSingletonError("posix_flock_unavailable", path)
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - auf Linux immer vorhanden
        raise WallboxManagerSingletonError("posix_flock_unavailable", path) from exc

    if not path or not os.path.isabs(path):
        raise WallboxManagerSingletonError("lock_path_not_absolute", path)
    path = os.path.abspath(path)
    if path != WALLBOX_MANAGER_LOCK_PATH:
        raise WallboxManagerSingletonError("lock_path_not_canonical", path)
    parent = os.path.dirname(path)
    _validate_root_directory(LOCK_NAMESPACE_ROOT, lock_path=path)
    _validate_root_directory(parent, lock_path=path)
    www_data_gid = _www_data_gid(path)

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise WallboxManagerSingletonError("lock_nofollow_unavailable", path)
    flags = os.O_RDWR | nofollow | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        reason = "lock_path_symlink" if exc.errno == errno.ELOOP else "lock_open_failed"
        raise WallboxManagerSingletonError(reason, path) from exc

    try:
        opened_stat = os.fstat(descriptor)
        named_stat = os.lstat(path)
        if (
            not _lock_file_contract_valid(opened_stat, www_data_gid=www_data_gid)
            or not _lock_file_contract_valid(named_stat, www_data_gid=www_data_gid)
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (named_stat.st_dev, named_stat.st_ino)
        ):
            raise WallboxManagerSingletonError("lock_path_not_unique_regular_file", path)

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            diagnostic = _read_diagnostic(descriptor)
            raise WallboxManagerSingletonError(
                "wallbox_manager_already_running",
                path,
                diagnostic,
            ) from exc
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                diagnostic = _read_diagnostic(descriptor)
                raise WallboxManagerSingletonError(
                    "wallbox_manager_already_running",
                    path,
                    diagnostic,
                ) from exc
            raise WallboxManagerSingletonError("lock_acquire_failed", path) from exc

        # Nach der Sperre nochmals beweisen, dass der Name weiterhin exakt auf
        # den geöffneten, einzelnen regulären Inode zeigt.
        named_after_lock = os.lstat(path)
        if (
            not _lock_file_contract_valid(
                named_after_lock,
                www_data_gid=www_data_gid,
            )
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (named_after_lock.st_dev, named_after_lock.st_ino)
        ):
            raise WallboxManagerSingletonError("lock_path_changed_during_acquire", path)

        diagnostic = {
            "schema": "e3dc_wallbox_manager_singleton_v1",
            "pid": os.getpid(),
            "process_start": _process_start_diagnostic(),
            "acquired_ts": int(time.time()),
        }
        payload = json.dumps(
            diagnostic,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, payload)
        return WallboxManagerSingleton(descriptor, path, diagnostic)
    except WallboxManagerSingletonError:
        _close_quietly(descriptor)
        raise
    except OSError as exc:
        _close_quietly(descriptor)
        raise WallboxManagerSingletonError("lock_validation_failed", path) from exc
