#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Begrenzte Offline-Migration der privaten Docker-Laufzeitdaten.

Die CLI bearbeitet ausschließlich feste Produktpfade vor dem Workerstart.
Modelle werden niemals geladen. Beim Rückfall bleiben Runtime-Modelle in
einer privaten Quarantäne; ein älteres Root-Image muss neu trainieren.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import errno
import fcntl
import os
from pathlib import Path
import re
import secrets
import stat
import sys

try:
    from .docker_runtime_identity import (
        CONTROL_STATE_DIRECTORY, CONTROL_STATE_ROOT, PRODUCT_ROOT,
        RUNTIME_GID, RUNTIME_PYTHON_SCRIPTS, RUNTIME_UID, WEB_GID,
        validate_docker_product_binding, validate_runtime_account,
    )
except ImportError:
    from docker_runtime_identity import (
        CONTROL_STATE_DIRECTORY, CONTROL_STATE_ROOT, PRODUCT_ROOT,
        RUNTIME_GID, RUNTIME_PYTHON_SCRIPTS, RUNTIME_UID, WEB_GID,
        validate_docker_product_binding, validate_runtime_account,
    )


ML_DIRECTORY = Path("/var/lib/e3dc-control/ml")
FORECAST_DIRECTORY = Path("/var/lib/e3dc-control/forecast-evidence")
LEGACY_CONTROL_DIRECTORY = Path("/root/.e3dc-control-state")
QUARANTINE_NAME = ".root-rollback-models"
MAX_FILES = 512
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_MODEL_BYTES = 128 * 1024 * 1024
MAX_DATABASE_BYTES = 512 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_ML_NAME = r"(?:ml_model(?:-[0-9a-f]{64})?\.pkl|ml_model\.manifest\.json|\.ml_model\.lock)"
_ML_TEMP = r"\.(?:ml_model-[0-9a-f]{64}\.pkl|ml_model\.manifest\.json)\.tmp\.[1-9][0-9]*\.[0-9a-f]{16}"
_CONTROL_NAME = r"wallbox_openwb_pro_control_lease(?:_wb(?:[1-9]|[1-5][0-9]|6[0-4]))?\.json"


class PrivateRuntimeError(RuntimeError):
    """Ein privater Pfad lässt sich nicht eindeutig und sicher binden."""


def _fail(code: str) -> None:
    raise PrivateRuntimeError(code)


def _mount_id(descriptor: int) -> int:
    with open(f"/proc/self/fdinfo/{descriptor}", encoding="ascii") as handle:
        for line in handle:
            key, separator, value = line.partition(":")
            if separator and key == "mnt_id" and int(value.strip()) > 0:
                return int(value.strip())
    _fail("private_mount_identity_missing")


def _no_extra_permissions(descriptor: int) -> None:
    try:
        names = os.listxattr(descriptor)
    except OSError as exc:
        if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            return
        raise
    if {"system.posix_acl_access", "system.posix_acl_default", "security.capability"} & set(names):
        _fail("private_extra_permissions")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
        info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def _root_parent(path: Path) -> int:
    """Öffnet ausschließlich echte, root-kontrollierte Elternkomponenten."""
    if not path.is_absolute() or str(path) != os.path.abspath(path) or path.name in {"", ".", ".."}:
        _fail("private_path_not_canonical")
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            mode = stat.S_IMODE(info.st_mode)
            if info.st_uid != 0 or info.st_gid != 0 or mode & 0o022:
                _fail("private_parent_not_root_controlled")
            _no_extra_permissions(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _regular_allowed(name: str, kind: str) -> bool:
    if kind == "ml":
        return bool(re.fullmatch(_ML_NAME, name) or re.fullmatch(_ML_TEMP, name))
    if kind == "forecast":
        return name in {
            "pv_forecast_evidence.db", "pv_forecast_evidence.db-wal",
            "pv_forecast_evidence.db-shm", "pv_forecast_evidence.db-journal", "writer.lock",
        }
    if kind == "control":
        return bool(
            re.fullmatch(_CONTROL_NAME + r"(?:\.lock)?", name)
            or re.fullmatch(r"\." + _CONTROL_NAME + r"\.[0-9a-f]{16}\.tmp", name)
        )
    if kind == "quarantine":
        original, separator, token = name.rpartition(".archived-")
        return bool(separator and re.fullmatch(r"[0-9a-f]{32}", token) and _regular_allowed(original, "ml"))
    return False


@dataclass
class BoundFile:
    name: str
    descriptor: int
    metadata: os.stat_result
    mount_id: int

    def close(self) -> None:
        os.close(self.descriptor)


@dataclass
class BoundStore:
    path: Path
    kind: str
    parent: int
    descriptor: int
    metadata: os.stat_result
    mount_id: int
    files: list[BoundFile] = field(default_factory=list)
    archive: "BoundStore | None" = None

    def close(self) -> None:
        if self.archive is not None:
            self.archive.close()
        for item in self.files:
            item.close()
        os.close(self.descriptor)
        os.close(self.parent)


def _assert_store(store: BoundStore) -> None:
    opened = os.fstat(store.descriptor)
    named = os.stat(store.path.name, dir_fd=store.parent, follow_symlinks=False)
    expected = store.metadata
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_gid, opened.st_mode)
        != (expected.st_dev, expected.st_ino, expected.st_uid, expected.st_gid, expected.st_mode)
        or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        or not stat.S_ISDIR(named.st_mode)
        or _mount_id(store.descriptor) != store.mount_id
    ):
        _fail("private_directory_rebound")
    expected_names = {item.name for item in store.files}
    if store.archive is not None:
        expected_names.add(QUARANTINE_NAME)
    if set(os.listdir(store.descriptor)) != expected_names:
        _fail("private_directory_contents_changed")
    _no_extra_permissions(store.descriptor)


def _assert_file(store: BoundStore, item: BoundFile) -> None:
    _assert_store(store)
    opened = os.fstat(item.descriptor)
    named = os.stat(item.name, dir_fd=store.descriptor, follow_symlinks=False)
    if (
        _identity(opened) != _identity(item.metadata)
        or _identity(named) != _identity(opened)
        or _mount_id(item.descriptor) != item.mount_id
        or item.mount_id != store.mount_id
    ):
        _fail("private_file_rebound")
    _no_extra_permissions(item.descriptor)


def _bind_store(path: Path, kind: str, *, parent: int | None = None, require_mount: bool = False) -> BoundStore:
    parent_fd = _root_parent(path) if parent is None else os.dup(parent)
    descriptor = -1
    bound = None
    try:
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
            _fail("private_store_not_directory")
        descriptor = os.open(path.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            _fail("private_store_changed_during_open")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        names = sorted(os.listdir(descriptor))
        if len(names) > MAX_FILES:
            _fail("private_store_file_limit")
        mode = stat.S_IMODE(opened.st_mode)
        empty_root_volume = not names and opened.st_uid == 0 and opened.st_gid == 0 and mode == 0o755
        if (
            opened.st_uid not in {0, RUNTIME_UID}
            or opened.st_gid not in {0, RUNTIME_GID, WEB_GID}
            or (mode != 0o700 and not empty_root_volume)
            or (kind == "quarantine" and (opened.st_uid, opened.st_gid, mode) != (0, 0, 0o700))
        ):
            _fail("private_store_owner_or_mode")
        _no_extra_permissions(descriptor)
        mount_id = _mount_id(descriptor)
        if require_mount and mount_id == _mount_id(parent_fd):
            _fail("private_store_volume_mount_missing")
        if kind == "control" and mount_id != _mount_id(parent_fd):
            _fail("private_control_state_foreign_mount")
        bound = BoundStore(path, kind, parent_fd, descriptor, opened, mount_id)
        total_size = 0
        for name in names:
            if kind == "ml" and name == QUARANTINE_NAME:
                bound.archive = _bind_store(path / name, "quarantine", parent=descriptor)
                if bound.archive.mount_id != mount_id:
                    _fail("private_archive_foreign_mount")
                continue
            if not _regular_allowed(name, kind):
                _fail("private_store_unknown_entry")
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            size_limit = MAX_DATABASE_BYTES if kind == "forecast" else 32 * 1024 if kind == "control" else MAX_MODEL_BYTES
            if (
                not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_uid not in {0, RUNTIME_UID}
                or metadata.st_gid not in {0, RUNTIME_GID, WEB_GID}
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > size_limit
            ):
                _fail("private_file_owner_type_mode_or_size")
            total_size += metadata.st_size
            if total_size > MAX_TOTAL_BYTES:
                _fail("private_store_size_limit")
            child = os.open(name, _FILE_FLAGS, dir_fd=descriptor)
            try:
                if _identity(os.fstat(child)) != _identity(metadata) or _mount_id(child) != mount_id:
                    _fail("private_file_changed_or_foreign_mount")
                _no_extra_permissions(child)
                if name.endswith(".lock"):
                    fcntl.flock(child, fcntl.LOCK_EX | fcntl.LOCK_NB)
                bound.files.append(BoundFile(name, child, metadata, mount_id))
            except BaseException:
                os.close(child)
                raise
        _assert_store(bound)
        return bound
    except BaseException:
        if bound is not None:
            bound.close()
        else:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_fd)
        raise


def _set_directory_owner(store: BoundStore, uid: int, gid: int) -> None:
    _assert_store(store)
    if (store.metadata.st_uid, store.metadata.st_gid) != (uid, gid):
        os.fchown(store.descriptor, uid, gid)
    if stat.S_IMODE(store.metadata.st_mode) != 0o700:
        os.fchmod(store.descriptor, 0o700)
    store.metadata = os.fstat(store.descriptor)
    os.fsync(store.descriptor)
    _assert_store(store)


def _set_file_owner(store: BoundStore, item: BoundFile, uid: int, gid: int) -> None:
    _assert_file(store, item)
    if (item.metadata.st_uid, item.metadata.st_gid) != (uid, gid):
        os.fchown(item.descriptor, uid, gid)
        item.metadata = os.fstat(item.descriptor)
    os.fsync(item.descriptor)
    _assert_file(store, item)


def _move_file(source: BoundStore, destination: BoundStore, item: BoundFile, name: str) -> None:
    _assert_file(source, item)
    _assert_store(destination)
    if (destination.metadata.st_uid, stat.S_IMODE(destination.metadata.st_mode)) != (0, 0o700):
        _fail("private_move_destination_not_root_private")
    try:
        os.stat(name, dir_fd=destination.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        _fail("private_move_destination_exists")
    # Beide Namensräume sind root-privat und über die Store-Locks exklusiv.
    # Deshalb existiert zwischen Leerprüfung und rename kein unprivilegierter
    # Schreiber; es wird weder ein Ziel ersetzt noch ein zweiter Link erzeugt.
    os.rename(item.name, name, src_dir_fd=source.descriptor, dst_dir_fd=destination.descriptor)
    source.files.remove(item)
    destination.files.append(item)
    item.name = name
    item.metadata = os.fstat(item.descriptor)
    _assert_file(destination, item)
    os.fsync(source.descriptor)
    os.fsync(destination.descriptor)


def _ensure_quarantine(store: BoundStore) -> BoundStore:
    if store.archive is None:
        _assert_store(store)
        os.mkdir(QUARANTINE_NAME, mode=0o700, dir_fd=store.descriptor)
        os.fsync(store.descriptor)
        store.archive = _bind_store(store.path / QUARANTINE_NAME, "quarantine", parent=store.descriptor)
    return store.archive


def _apply_store(store: BoundStore, action: str) -> None:
    runtime_target = action in {"migrate", "check-runtime"}
    target_uid, target_gid = (RUNTIME_UID, RUNTIME_GID) if runtime_target else (0, 0)
    checking = action.startswith("check-")
    if checking:
        if (store.metadata.st_uid, store.metadata.st_gid, stat.S_IMODE(store.metadata.st_mode)) != (target_uid, target_gid, 0o700):
            _fail("private_store_target_owner_missing")
        if not runtime_target and store.kind == "ml" and store.files:
            _fail("private_root_model_store_not_empty")
        for item in store.files:
            _assert_file(store, item)
            if (item.metadata.st_uid, item.metadata.st_gid) != (target_uid, target_gid):
                _fail("private_file_target_owner_missing")
        if store.archive is not None:
            for item in store.archive.files:
                _assert_file(store.archive, item)
                if (item.metadata.st_uid, item.metadata.st_gid) != (0, 0):
                    _fail("private_archive_target_owner_missing")
        return

    if store.kind == "ml" and action == "rollback-root":
        archived = store.archive.files if store.archive is not None else []
        if len(archived) + len(store.files) > MAX_FILES:
            _fail("private_archive_file_limit")
        if sum(item.metadata.st_size for item in (*archived, *store.files)) > MAX_TOTAL_BYTES:
            _fail("private_archive_size_limit")

    # Zuerst den bereits vollständig geprüften Namensraum sperren. Ein
    # unterbrochener Lauf hinterlässt ausschließlich 0700/0600 und erlaubte
    # Root-/Runtime-Owner; der nächste Offline-Lauf kann ihn wieder aufnehmen.
    _set_directory_owner(store, 0, 0)
    if store.kind == "ml" and action == "rollback-root" and store.files:
        archive = _ensure_quarantine(store)
        for item in list(store.files):
            archived_name = item.name + ".archived-" + secrets.token_hex(16)
            _move_file(store, archive, item, archived_name)
            _set_file_owner(archive, item, 0, 0)
    else:
        for item in store.files:
            _set_file_owner(store, item, target_uid, target_gid)
    if store.archive is not None:
        for item in store.archive.files:
            _set_file_owner(store.archive, item, 0, 0)
    _set_directory_owner(store, target_uid, target_gid)


def _make_root_directory(path: Path, mode: int, *, must_create: bool = False) -> None:
    parent = _root_parent(path)
    try:
        try:
            os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            os.mkdir(path.name, mode=0o700, dir_fd=parent)
            descriptor = os.open(path.name, _DIRECTORY_FLAGS, dir_fd=parent)
            try:
                info = os.fstat(descriptor)
                if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, 0, 0o700):
                    _fail("private_new_directory_contract")
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent)
        else:
            if must_create:
                _fail("private_new_directory_collision")
        descriptor = os.open(path.name, _DIRECTORY_FLAGS, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, 0, mode):
                _fail("private_control_parent_contract")
            _no_extra_permissions(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


@dataclass
class ControlPlan:
    action: str
    source: BoundStore | None
    destination: BoundStore | None
    destination_path: Path
    runtime_parent_exists: bool

    def close(self) -> None:
        if self.source is not None:
            self.source.close()
        if self.destination is not None:
            self.destination.close()


def _bind_control_plan(action: str) -> ControlPlan:
    """Prüft beide Control-Namensräume vollständig, ohne sie zu verändern."""
    target = Path(CONTROL_STATE_DIRECTORY)
    runtime_parent = Path(CONTROL_STATE_ROOT)
    parent_exists = os.path.lexists(runtime_parent)
    parent = _root_parent(runtime_parent)
    try:
        if parent_exists:
            descriptor = os.open(runtime_parent.name, _DIRECTORY_FLAGS, dir_fd=parent)
            try:
                info = os.fstat(descriptor)
                if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, 0, 0o755):
                    _fail("private_control_parent_contract")
                _no_extra_permissions(descriptor)
            finally:
                os.close(descriptor)
    finally:
        os.close(parent)
    runtime_target = action in {"migrate", "check-runtime"}
    source_path, destination_path = (
        (LEGACY_CONTROL_DIRECTORY, target) if runtime_target else (target, LEGACY_CONTROL_DIRECTORY)
    )
    source = destination = None
    try:
        if os.path.lexists(source_path):
            source = _bind_store(source_path, "control")
        if os.path.lexists(destination_path):
            destination = _bind_store(destination_path, "control")
        else:
            # Der Elternpfad des Altziels existiert immer; der neue Runtime-
            # Parent wurde oben entweder gebunden oder als fehlend belegt.
            if destination_path != target or parent_exists:
                parent = _root_parent(destination_path)
                os.close(parent)
        if source is not None and destination is not None:
            if {item.name for item in source.files} & {item.name for item in destination.files}:
                _fail("private_control_state_collision")
            if source.mount_id != destination.mount_id:
                _fail("private_control_state_cross_mount")
            if len(source.files) + len(destination.files) > MAX_FILES:
                _fail("private_control_state_file_limit")
        return ControlPlan(action, source, destination, destination_path, parent_exists)
    except BaseException:
        if source is not None:
            source.close()
        if destination is not None:
            destination.close()
        raise


def _apply_control_plan(plan: ControlPlan) -> None:
    if plan.action.startswith("check-"):
        if plan.destination is None:
            if plan.action == "check-root" and (plan.source is None or not plan.source.files):
                return
            _fail("private_control_state_missing")
        _apply_store(plan.destination, plan.action)
        if plan.source is not None and plan.source.files:
            _fail("private_control_source_not_empty")
        return
    if plan.action == "rollback-root" and plan.source is None:
        return
    if plan.destination is None:
        if plan.action == "migrate" and not plan.runtime_parent_exists:
            _make_root_directory(Path(CONTROL_STATE_ROOT), 0o755, must_create=True)
        _make_root_directory(plan.destination_path, 0o700, must_create=True)
        plan.destination = _bind_store(plan.destination_path, "control")
    if plan.source is not None:
        if plan.source.mount_id != plan.destination.mount_id:
            _fail("private_control_state_cross_mount")
        _set_directory_owner(plan.source, 0, 0)
    _set_directory_owner(plan.destination, 0, 0)
    if plan.source is not None:
        for item in list(plan.source.files):
            _move_file(plan.source, plan.destination, item, item.name)
    _apply_store(plan.destination, plan.action)


def require_workers_stopped() -> None:
    """Verbietet die Migration im laufenden EMS-Prozessnamensraum."""
    own_pid = os.getpid()
    worker_names = set(RUNTIME_PYTHON_SCRIPTS)
    worker_names.update(str(Path(name).name) for name in RUNTIME_PYTHON_SCRIPTS)
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal() or int(entry.name) == own_pid:
            continue
        try:
            status = (entry / "status").read_text(encoding="ascii")
            uids = next(line for line in status.splitlines() if line.startswith("Uid:")).split()[1:]
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError):
            continue
        if str(RUNTIME_UID) in uids:
            _fail("private_runtime_worker_still_running")
        if arguments and Path(os.fsdecode(arguments[0])).name.startswith("python"):
            for argument in arguments[1:]:
                name = os.fsdecode(argument)
                if name in worker_names or name.startswith(PRODUCT_ROOT + "/Installer/") and name.removeprefix(PRODUCT_ROOT + "/Installer/") in worker_names:
                    _fail("private_legacy_worker_still_running")


def main() -> int:
    parser = argparse.ArgumentParser(description="Private Docker-Laufzeitdaten offline migrieren oder sicher für Root-Rückfall vorbereiten")
    parser.add_argument("action", choices=("migrate", "check-runtime", "rollback-root", "check-root"))
    parser.add_argument("--include-forecast", action="store_true", help="Aktiviertes privates Prognosevolume einschließen")
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            _fail("private_migration_requires_root")
        validate_runtime_account()
        validate_docker_product_binding()
        require_workers_stopped()
        paths = [(ML_DIRECTORY, "ml")]
        if args.include_forecast or args.action in {"rollback-root", "check-root"}:
            if os.path.lexists(FORECAST_DIRECTORY):
                paths.append((FORECAST_DIRECTORY, "forecast"))
            elif args.include_forecast:
                _fail("private_forecast_volume_missing")
        stores = []
        control_plan = None
        try:
            # Sämtliche Volumes und vorhandenen Controlstate-Namensräume vor
            # der ersten Eigentumsänderung prüfen und offen gebunden halten.
            for path, kind in paths:
                stores.append(_bind_store(path, kind, require_mount=True))
            control_plan = _bind_control_plan(args.action)
            for store in stores:
                _apply_store(store, args.action)
            _apply_control_plan(control_plan)
        finally:
            if control_plan is not None:
                control_plan.close()
            for store in stores:
                store.close()
        print("Private Docker-Laufzeitdaten: " + args.action + " bestätigt.")
        return 0
    except PrivateRuntimeError as exc:
        print("Private Docker-Laufzeitdaten: " + str(exc) + "; EMS-Worker bleiben gestoppt.", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError, StopIteration):
        # Keine Dateinamen, Pfade oder privaten Inhalte aus Ausnahmen ausgeben.
        print("Private Docker-Laufzeitdaten konnten nicht sicher gebunden werden; EMS-Worker bleiben gestoppt.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
