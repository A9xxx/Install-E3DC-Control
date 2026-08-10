#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Exklusiver Prozessbesitz und Neustartübergabe des Storage Managers.

Der Kernel-Lock ist der einzige Besitzbeleg. Er wird nie gelöscht und beim
Prozessende automatisch freigegeben. Die Zustandsmaschine dient ausschließlich
der fail-closed Ausgangssperre und Diagnose; PID oder Lockdateiinhalt verleihen
keinen Besitz.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import time
from typing import Any, Dict


OWNER_ACQUIRED = "owner_acquired"
SUCCESSOR_CONFIRMED = "successor_confirmed"
TERMINATING = "terminating"
OWNER_RELEASED = "owner_released"

LOCK_NAMESPACE_ROOT = "/run/e3dc-control"
LOCK_DIRECTORY = f"{LOCK_NAMESPACE_ROOT}/locks"
STORAGE_MANAGER_LOCK_PATH = f"{LOCK_DIRECTORY}/storage_manager.owner.lock"
LOCK_DIRECTORY_MODE = 0o755
LOCK_FILE_MODE = 0o660


class StorageManagerOwnershipError(RuntimeError):
    """Der Storage Manager darf vor dem RSCP-Ausgang nicht starten."""

    def __init__(
        self,
        reason: str,
        path: str,
        diagnostic: Dict[str, Any] | None = None,
    ) -> None:
        self.reason = str(reason or "storage_owner_unavailable")
        self.path = str(path or "")
        self.diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
        super().__init__(f"{self.reason}: {self.path}")


class StorageManagerOwnership:
    """Hält den exklusiven POSIX-Lock bis zum bestätigten Prozessende."""

    def __init__(self, descriptor: int, path: str, diagnostic: Dict[str, Any]):
        self._descriptor = int(descriptor)
        self.path = str(path)
        self._diagnostic = dict(diagnostic)

    @property
    def acquired(self) -> bool:
        return self._descriptor >= 0 and self.state != OWNER_RELEASED

    @property
    def state(self) -> str:
        return str(self._diagnostic.get("state") or "")

    @property
    def successor_confirmed(self) -> bool:
        return self.state == SUCCESSOR_CONFIRMED

    def diagnostic(self) -> Dict[str, Any]:
        return dict(self._diagnostic)

    def _publish_state(self, state: str, *, evidence: str = "") -> None:
        if self._descriptor < 0:
            raise StorageManagerOwnershipError("storage_owner_already_released", self.path)
        if state not in {
            OWNER_ACQUIRED,
            SUCCESSOR_CONFIRMED,
            TERMINATING,
            OWNER_RELEASED,
        }:
            raise StorageManagerOwnershipError("storage_owner_state_invalid", self.path)
        self._diagnostic["state"] = state
        self._diagnostic["updated_ts"] = int(time.time())
        if evidence:
            self._diagnostic["confirmation_evidence"] = str(evidence)
        payload = json.dumps(
            self._diagnostic,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        os.ftruncate(self._descriptor, 0)
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        os.write(self._descriptor, payload)

    def confirm_successor(self, evidence: str) -> bool:
        """Bestätigt den Nachfolger erst nach frischem Geräte-Readback/ACK."""

        if self.state == SUCCESSOR_CONFIRMED:
            return False
        if self.state != OWNER_ACQUIRED:
            raise StorageManagerOwnershipError(
                "storage_successor_confirmation_out_of_order",
                self.path,
                self.diagnostic(),
            )
        evidence_text = str(evidence or "").strip()
        if not evidence_text:
            raise StorageManagerOwnershipError(
                "storage_successor_confirmation_missing_evidence",
                self.path,
                self.diagnostic(),
            )
        self._publish_state(SUCCESSOR_CONFIRMED, evidence=evidence_text)
        return True

    def begin_termination(self) -> bool:
        if self.state == TERMINATING:
            return False
        if self.state not in {OWNER_ACQUIRED, SUCCESSOR_CONFIRMED}:
            raise StorageManagerOwnershipError(
                "storage_termination_out_of_order",
                self.path,
                self.diagnostic(),
            )
        self._publish_state(TERMINATING)
        return True

    def release(self) -> bool:
        """Gibt nur den Prozesslock frei; diese Methode sendet niemals RSCP."""

        descriptor = self._descriptor
        if descriptor < 0:
            return False
        try:
            if self.state != TERMINATING:
                self.begin_termination()
            self._publish_state(OWNER_RELEASED)
        finally:
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

    def __enter__(self) -> "StorageManagerOwnership":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> bool:
        self.release()
        return False


def _process_start_diagnostic() -> str:
    try:
        with open("/proc/self/stat", "r", encoding="ascii") as proc_stat:
            fields = proc_stat.read().split()
        if len(fields) > 21:
            return fields[21]
    except (OSError, UnicodeError):
        pass
    return "unknown"


def _read_diagnostic(descriptor: int) -> Dict[str, Any]:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, 4096)
        value = json.loads(raw.decode("utf-8")) if raw else {}
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _www_data_gid(path: str) -> int:
    try:
        import grp

        return int(grp.getgrnam("www-data").gr_gid)
    except (ImportError, KeyError) as exc:
        raise StorageManagerOwnershipError("lock_group_unavailable", path) from exc


def _validate_root_directory(path: str, *, lock_path: str) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise StorageManagerOwnershipError("lock_parent_unavailable", lock_path) from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise StorageManagerOwnershipError(
            "lock_parent_not_regular_directory",
            lock_path,
        )
    if info.st_uid != 0 or info.st_gid != 0:
        raise StorageManagerOwnershipError("lock_parent_owner_invalid", lock_path)
    if stat.S_IMODE(info.st_mode) != LOCK_DIRECTORY_MODE:
        raise StorageManagerOwnershipError("lock_parent_mode_invalid", lock_path)
    return info


def _lock_file_contract_valid(info: os.stat_result, *, www_data_gid: int) -> bool:
    return bool(
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == 0
        and info.st_gid == www_data_gid
        and stat.S_IMODE(info.st_mode) == LOCK_FILE_MODE
    )


def acquire_storage_manager_ownership(
    lock_path: str,
    *,
    wait_s: float = 0.0,
) -> StorageManagerOwnership:
    """Erwirbt genau einen Storage-RSCP-Owner oder bricht fail-closed ab."""

    path = str(lock_path or "")
    if os.name != "posix":
        raise StorageManagerOwnershipError("posix_flock_unavailable", path)
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - auf Linux immer vorhanden
        raise StorageManagerOwnershipError("posix_flock_unavailable", path) from exc

    if not path or not os.path.isabs(path):
        raise StorageManagerOwnershipError("lock_path_not_absolute", path)
    path = os.path.abspath(path)
    if path != STORAGE_MANAGER_LOCK_PATH:
        raise StorageManagerOwnershipError("lock_path_not_canonical", path)
    parent = os.path.dirname(path)
    _validate_root_directory(LOCK_NAMESPACE_ROOT, lock_path=path)
    _validate_root_directory(parent, lock_path=path)
    www_data_gid = _www_data_gid(path)

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise StorageManagerOwnershipError("lock_nofollow_unavailable", path)
    flags = os.O_RDWR | nofollow | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        reason = "lock_path_symlink" if exc.errno == errno.ELOOP else "lock_open_failed"
        raise StorageManagerOwnershipError(reason, path) from exc

    try:
        opened_stat = os.fstat(descriptor)
        named_stat = os.lstat(path)
        if (
            not _lock_file_contract_valid(opened_stat, www_data_gid=www_data_gid)
            or not _lock_file_contract_valid(named_stat, www_data_gid=www_data_gid)
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (named_stat.st_dev, named_stat.st_ino)
        ):
            raise StorageManagerOwnershipError(
                "lock_path_not_unique_regular_file",
                path,
            )

        deadline = time.monotonic() + max(0.0, float(wait_s))
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise StorageManagerOwnershipError(
                        "storage_manager_already_running",
                        path,
                        _read_diagnostic(descriptor),
                    ) from exc
                time.sleep(0.05)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    if time.monotonic() < deadline:
                        time.sleep(0.05)
                        continue
                    raise StorageManagerOwnershipError(
                        "storage_manager_already_running",
                        path,
                        _read_diagnostic(descriptor),
                    ) from exc
                raise StorageManagerOwnershipError("lock_acquire_failed", path) from exc

        named_after_lock = os.lstat(path)
        if (
            not _lock_file_contract_valid(named_after_lock, www_data_gid=www_data_gid)
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (named_after_lock.st_dev, named_after_lock.st_ino)
        ):
            raise StorageManagerOwnershipError(
                "lock_path_changed_during_acquire",
                path,
            )
        diagnostic = {
            "schema": "e3dc_storage_manager_ownership_v1",
            "contract_version": 1,
            "pid": os.getpid(),
            "process_start": _process_start_diagnostic(),
            "acquired_ts": int(time.time()),
            "state": OWNER_ACQUIRED,
        }
        owner = StorageManagerOwnership(descriptor, path, diagnostic)
        owner._publish_state(OWNER_ACQUIRED)
        return owner
    except StorageManagerOwnershipError:
        _close_quietly(descriptor)
        raise
    except OSError as exc:
        _close_quietly(descriptor)
        raise StorageManagerOwnershipError("lock_validation_failed", path) from exc
