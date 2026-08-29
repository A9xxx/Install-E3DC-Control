#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manifestgebundene Aufbewahrung für eigene E3DC-Sicherungssammlungen."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple, Union

try:
    import fcntl
except ImportError:  # pragma: no cover - Produktivbetrieb erfolgt unter Linux.
    fcntl = None

try:
    from .backup_integrity import (
        BackupIntegrityError,
        QuiescedOverlayRestoreGuard,
        QUIESCED_OVERLAY_KIND,
        ROOT_MARKER_NAME,
        SYSTEM_BACKUP_KIND,
        WEB_SNAPSHOT_KIND,
        _assert_no_symlink_components,
        _lexical_absolute,
        _normalized_backup_id,
        _normalized_transaction_id,
        validate_existing_backup_root,
        validate_quiesced_overlay_guard,
        verify_backup,
        verified_manifest_sha256,
    )
except ImportError:  # pragma: no cover - Rückfall für direkte Skriptausführung
    from backup_integrity import (
        BackupIntegrityError,
        QuiescedOverlayRestoreGuard,
        QUIESCED_OVERLAY_KIND,
        ROOT_MARKER_NAME,
        SYSTEM_BACKUP_KIND,
        WEB_SNAPSHOT_KIND,
        _assert_no_symlink_components,
        _lexical_absolute,
        _normalized_backup_id,
        _normalized_transaction_id,
        validate_existing_backup_root,
        validate_quiesced_overlay_guard,
        verify_backup,
        verified_manifest_sha256,
    )


PathValue = Union[str, os.PathLike]
UPDATE_LOCK_PATH = Path("/run/lock/e3dc-control/update.lock")
UPDATE_LOCK_ENV = "E3DC_UPDATE_LOCK_FD"
UPDATE_STATE_DIR = Path("/var/lib/e3dc-update-safety")
# Muss mit dem read-only Recovery-Namensraum aus update.py synchron bleiben.
UPDATE_STATE_BLOCKERS = (
    "recovery-journal.json",
    "prejournal-construction.json",
    "recovery-context.json",
    "recovery-surface.json",
    "systemd-recovery-surface.json",
    "transaction.json",
    "quiesced-overlay.json",
    "prepared-packages.json",
    "recovery.block",
)
UPDATE_RECOVERY_DROPIN_NAME = "00-e3dc-recovery-bootblock.conf"
SYSTEMD_ADMIN_ROOT = Path("/etc/systemd/system")
_QUIESCED_OVERLAY_NAME_RE = re.compile(
    r"\.([A-Za-z0-9][A-Za-z0-9._-]{0,254})\.quiesced-([0-9a-f]{64})\Z"
)
_PRUNE_QUARANTINE_NAME_RE = re.compile(r"\.e3dc-prune-[0-9a-f]{32}\Z")
_PRUNE_RESUME_RECEIPT_NAME_RE = re.compile(
    r"(\.e3dc-prune-[0-9a-f]{32})\.resume\.json\Z"
)
_PRUNE_RESUME_STAGE_NAME_RE = re.compile(
    r"(\.e3dc-prune-[0-9a-f]{32})\.resume-stage-[0-9a-f]{32}\.json\Z"
)
_PRUNE_RESUME_SCHEMA = 1
_PRUNE_RESUME_PURPOSE = "e3dc-control-prune-resume"
_PRUNE_RESUME_MAX_BYTES = 64 * 1024 * 1024
_PRUNE_RESUME_MAX_ENTRIES = 500_000
_QUIESCED_OVERLAY_KEYS = frozenset(
    {
        "schema",
        "kind",
        "state",
        "backup_id",
        "created_utc",
        "install_root",
        "files",
        "sources",
        "transaction_id",
        "parent_backup_id",
    }
)


@dataclass(frozen=True)
class _SystemBackupContract:
    path: Path
    backup_id: str
    install_root: str
    manifest_sha256: str
    dev: int
    ino: int


@dataclass(frozen=True)
class _QuiescedOverlayContract:
    path: Path
    parent_name: str
    transaction_id: str
    backup_id: str
    parent_backup_id: str
    install_root: str
    manifest_sha256: str
    dev: int
    ino: int


@dataclass(frozen=True)
class _PruneQuarantineContract:
    path: Path
    kind: str
    install_root: str
    manifest_sha256: str
    parent_backup_id: str
    dev: int
    ino: int
    root_dev: int
    root_ino: int
    root_mount_id: int
    mount_id: int


@dataclass(frozen=True)
class _DirectoryMountContract:
    dev: int
    ino: int
    parent_mount_id: int
    entry_mount_id: int


@dataclass(frozen=True)
class _RemovalTreeContract:
    name: str
    dev: int
    ino: int
    mount_id: int
    is_directory: bool
    children: Tuple["_RemovalTreeContract", ...] = ()


@dataclass(frozen=True)
class _RemovalTreeReceiptEntry:
    path: str
    dev: int
    ino: int
    is_directory: bool


@dataclass(frozen=True)
class _PruneResumeContract:
    receipt_name: str
    quarantine_name: str
    original_name: str
    kind: str
    install_root: str
    manifest_sha256: str
    parent_backup_id: str
    root_dev: int
    root_ino: int
    target_dev: int
    target_ino: int
    tree: Tuple[_RemovalTreeReceiptEntry, ...]
    receipt_dev: int
    receipt_ino: int
    root_mount_id: int
    receipt_mount_id: int


@dataclass
class _RemovalMutationState:
    started: bool = False


class BackupMaintenanceBusy(BackupIntegrityError):
    """Eine andere Update- oder Recovery-Transaktion besitzt die Mutation."""


def _int_from_env(name: str, default: int, minimum: int = 1, maximum: int = 200) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


MAX_BACKUP_FAMILY_COUNT = 3
UPDATE_BACKUP_KEEP_COUNT = _int_from_env(
    "E3DC_BACKUP_KEEP_COUNT",
    MAX_BACKUP_FAMILY_COUNT,
    maximum=MAX_BACKUP_FAMILY_COUNT,
)
WEB_INSTALLER_BACKUP_KEEP_COUNT = _int_from_env(
    "E3DC_WEB_BACKUP_KEEP_COUNT",
    MAX_BACKUP_FAMILY_COUNT,
    maximum=MAX_BACKUP_FAMILY_COUNT,
)
UPDATE_BACKUP_MIN_KEEP_COUNT = min(
    UPDATE_BACKUP_KEEP_COUNT,
    _int_from_env(
        "E3DC_BACKUP_MIN_KEEP_COUNT",
        MAX_BACKUP_FAMILY_COUNT,
        maximum=MAX_BACKUP_FAMILY_COUNT,
    ),
)
WEB_INSTALLER_BACKUP_MIN_KEEP_COUNT = min(
    WEB_INSTALLER_BACKUP_KEEP_COUNT,
    _int_from_env(
        "E3DC_WEB_BACKUP_MIN_KEEP_COUNT",
        MAX_BACKUP_FAMILY_COUNT,
        maximum=MAX_BACKUP_FAMILY_COUNT,
    ),
)
# Drei verifizierte Generationen sind der maximale Standardbestand. Eine
# zusätzliche Altersrotation würde diesen Vertrag nur verdecken und ist daher
# standardmäßig deaktiviert; explizite Aufrufer können sie weiterhin setzen.
UPDATE_BACKUP_MAX_AGE_DAYS = _int_from_env(
    "E3DC_BACKUP_MAX_AGE_DAYS", 0, minimum=0, maximum=0
)
WEB_INSTALLER_BACKUP_MAX_AGE_DAYS = _int_from_env(
    "E3DC_WEB_BACKUP_MAX_AGE_DAYS", 0, minimum=0, maximum=0
)


def _log(logger: Any, level: str, message: str) -> None:
    if logger is None:
        return
    try:
        getattr(logger, level)(message)
    except Exception:
        pass


def _assert_trusted_update_lock_directory(path: Path) -> None:
    directory = Path(path)
    if not directory.is_absolute() or directory.resolve(strict=True) != directory:
        raise BackupIntegrityError("Update-Lock-Verzeichnis ist nicht kanonisch")
    current = Path(directory.anchor)
    for component in directory.parts[1:]:
        current /= component
        metadata = current.lstat()
        shared_lock_root = current == Path("/run/lock")
        shared_lock_root_safe = bool(
            shared_lock_root
            and metadata.st_uid == 0
            and metadata.st_gid == 0
            and metadata.st_mode & stat.S_ISVTX
        )
        if (
            current.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or (
                not shared_lock_root_safe
                and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            )
        ):
            raise BackupIntegrityError(
                "Update-Lock-Pfad besitzt eine unsichere Komponente: {}".format(
                    current
                )
            )


def _open_update_lock_directory() -> int:
    if fcntl is None or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise BackupIntegrityError(
            "Backup-Bereinigung benötigt Root und einen Linux-Dateilock"
        )
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
        raise BackupIntegrityError(
            "Systemweites Lock-Verzeichnis ist nicht vertrauenswürdig"
        )
    shared_descriptor = os.open(
        str(shared_root),
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        try:
            os.mkdir("e3dc-control", 0o700, dir_fd=shared_descriptor)
        except FileExistsError:
            pass
        private_descriptor = os.open(
            "e3dc-control",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=shared_descriptor,
        )
    finally:
        os.close(shared_descriptor)
    metadata = os.fstat(private_descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        os.close(private_descriptor)
        raise BackupIntegrityError(
            "Privates Update-Lock-Verzeichnis besitzt unsichere Metadaten"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        os.fchmod(private_descriptor, 0o700)
        os.fsync(private_descriptor)
    return private_descriptor


def _validate_update_lock_fd(descriptor: int) -> int:
    if fcntl is None:
        raise BackupIntegrityError("Linux-Dateilock ist nicht verfügbar")
    fd = int(descriptor)
    if fd < 3:
        raise BackupIntegrityError("Update-Lock-FD ist unzulässig")
    _assert_trusted_update_lock_directory(UPDATE_LOCK_PATH.parent)
    path_metadata = UPDATE_LOCK_PATH.lstat()
    fd_metadata = os.fstat(fd)
    if (
        UPDATE_LOCK_PATH.is_symlink()
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
        raise BackupIntegrityError(
            "Update-Lock besitzt unsichere Datei- oder FD-Metadaten"
        )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise BackupMaintenanceBusy(
            "Ein anderer E3DC-Control Update- oder Installationslauf ist aktiv"
        ) from exc
    return fd


def _acquire_or_bind_retention_lock() -> Tuple[int, bool]:
    inherited = str(os.environ.get(UPDATE_LOCK_ENV) or "").strip()
    if inherited:
        if not inherited.isdecimal():
            raise BackupIntegrityError("Geerbter Update-Lock-FD ist ungültig")
        return _validate_update_lock_fd(int(inherited)), False

    directory_descriptor = _open_update_lock_directory()
    try:
        descriptor = os.open(
            UPDATE_LOCK_PATH.name,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
    finally:
        os.close(directory_descriptor)
    try:
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        return _validate_update_lock_fd(descriptor), True
    except Exception:
        os.close(descriptor)
        raise


def _update_state_blockers() -> List[str]:
    blockers = [
        str(UPDATE_STATE_DIR / name)
        for name in UPDATE_STATE_BLOCKERS
        if os.path.lexists(str(UPDATE_STATE_DIR / name))
    ]
    try:
        unit_directories = tuple(SYSTEMD_ADMIN_ROOT.iterdir())
    except FileNotFoundError:
        unit_directories = ()
    except OSError:
        blockers.append(str(SYSTEMD_ADMIN_ROOT) + " (nicht lesbar)")
        unit_directories = ()
    blockers.extend(
        str(directory / UPDATE_RECOVERY_DROPIN_NAME)
        for directory in unit_directories
        if directory.name.endswith(".service.d")
        and os.path.lexists(str(directory / UPDATE_RECOVERY_DROPIN_NAME))
    )
    return sorted(set(blockers))


def _assert_no_update_state_blockers() -> None:
    blockers = _update_state_blockers()
    if blockers:
        raise BackupMaintenanceBusy(
            "Ein Updateabschluss oder Recovery-Zustand ist noch offen: {}".format(
                ", ".join(blockers)
            )
        )


@contextmanager
def backup_maintenance_lock(
    *,
    require_no_update_state: bool = True,
) -> Iterator[int]:
    """Serialisiert generische Backup-Mutationen mit Releasewechseln."""

    descriptor, owned = _acquire_or_bind_retention_lock()
    previous = os.environ.get(UPDATE_LOCK_ENV)
    os.environ[UPDATE_LOCK_ENV] = str(descriptor)
    try:
        if require_no_update_state:
            _assert_no_update_state_blockers()
        yield descriptor
        if require_no_update_state:
            _assert_no_update_state_blockers()
    finally:
        if previous is None:
            os.environ.pop(UPDATE_LOCK_ENV, None)
        else:
            os.environ[UPDATE_LOCK_ENV] = previous
        if owned:
            os.close(descriptor)


def _require_bound_update_lock() -> int:
    inherited = str(os.environ.get(UPDATE_LOCK_ENV) or "").strip()
    if not inherited or not inherited.isdecimal():
        raise BackupIntegrityError(
            "Overlay-Cleanup besitzt keinen gebundenen Update-Lock"
        )
    return _validate_update_lock_fd(int(inherited))


def _fd_mount_id(descriptor: int) -> int:
    """Liest die Kernel-Mount-ID eines bereits gehaltenen Deskriptors."""

    try:
        payload = Path("/proc/self/fdinfo/{}".format(int(descriptor))).read_text(
            encoding="ascii",
            errors="strict",
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise BackupIntegrityError(
            "Mount-ID des Backup-Deskriptors ist nicht sicher lesbar"
        ) from exc
    for line in payload.splitlines():
        if line.startswith("mnt_id:"):
            value = line.partition(":")[2].strip()
            if value.isdecimal() and int(value) > 0:
                return int(value)
    raise BackupIntegrityError("Mount-ID fehlt im Backup-Deskriptorvertrag")


def _bind_directory_mount_contract(
    parent_descriptor: int,
    name: str,
    *,
    expected_dev: Optional[int] = None,
    expected_ino: Optional[int] = None,
    expected_parent_mount_id: Optional[int] = None,
    expected_entry_mount_id: Optional[int] = None,
) -> _DirectoryMountContract:
    """Bindet ein direktes echtes Verzeichnis an Parent- und Eintrags-Mount."""

    parent_mount_id = _fd_mount_id(parent_descriptor)
    if (
        expected_parent_mount_id is not None
        and parent_mount_id != int(expected_parent_mount_id)
    ):
        raise BackupIntegrityError("Backup-Root-Mount driftete")
    before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise BackupIntegrityError("Backup-Eintrag ist kein echtes Verzeichnis")
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        entry_mount_id = _fd_mount_id(descriptor)
        named_after = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    finally:
        os.close(descriptor)
    identity = (int(before.st_dev), int(before.st_ino))
    if (
        identity != (int(opened.st_dev), int(opened.st_ino))
        or identity != (int(named_after.st_dev), int(named_after.st_ino))
        or (
            expected_dev is not None
            and identity != (int(expected_dev), int(expected_ino))
        )
    ):
        raise BackupIntegrityError("Backup-Verzeichnis driftete beim nofollow-Öffnen")
    if entry_mount_id != parent_mount_id:
        raise BackupIntegrityError(
            "Backup-Verzeichnis überschreitet eine fremde oder gebundene Mountgrenze"
        )
    if (
        expected_entry_mount_id is not None
        and entry_mount_id != int(expected_entry_mount_id)
    ):
        raise BackupIntegrityError("Backup-Eintrags-Mount driftete")
    return _DirectoryMountContract(
        dev=identity[0],
        ino=identity[1],
        parent_mount_id=parent_mount_id,
        entry_mount_id=entry_mount_id,
    )


def _bind_removal_tree(
    parent_descriptor: int,
    name: str,
    *,
    expected_dev: Optional[int] = None,
    expected_ino: Optional[int] = None,
    expected_parent_mount_id: Optional[int] = None,
    expected_entry_mount_id: Optional[int] = None,
) -> _RemovalTreeContract:
    """Prüft den vollständigen Löschbaum vor dem ersten Unlink oder Rmdir."""

    parent_mount_id = _fd_mount_id(parent_descriptor)
    if (
        expected_parent_mount_id is not None
        and parent_mount_id != int(expected_parent_mount_id)
    ):
        raise BackupIntegrityError("Löschbaum-Parent-Mount driftete")
    before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    is_directory = stat.S_ISDIR(before.st_mode)
    if stat.S_ISLNK(before.st_mode) or not (
        is_directory or stat.S_ISREG(before.st_mode)
    ):
        raise BackupIntegrityError(
            "Unerlaubter Dateityp im zu entfernenden Backup: {}".format(name)
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if is_directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    else:
        flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        entry_mount_id = _fd_mount_id(descriptor)
        named_after = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        identity = (int(before.st_dev), int(before.st_ino))
        if (
            identity != (int(opened.st_dev), int(opened.st_ino))
            or identity != (int(named_after.st_dev), int(named_after.st_ino))
            or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(before.st_mode)
            or stat.S_IFMT(named_after.st_mode) != stat.S_IFMT(before.st_mode)
            or (
                expected_dev is not None
                and identity != (int(expected_dev), int(expected_ino))
            )
        ):
            raise BackupIntegrityError("Löschbaum-Eintrag driftete beim Öffnen")
        if entry_mount_id != parent_mount_id:
            raise BackupIntegrityError(
                "Löschbaum enthält einen fremden oder bind-gemounteten Unterbaum"
            )
        if (
            expected_entry_mount_id is not None
            and entry_mount_id != int(expected_entry_mount_id)
        ):
            raise BackupIntegrityError("Löschbaum-Eintrags-Mount driftete")
        children: Tuple[_RemovalTreeContract, ...] = ()
        if is_directory:
            children = tuple(
                _bind_removal_tree(
                    descriptor,
                    child,
                    expected_parent_mount_id=entry_mount_id,
                )
                for child in sorted(os.listdir(descriptor))
            )
        if (
            _fd_mount_id(descriptor) != entry_mount_id
            or (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
            != identity
        ):
            raise BackupIntegrityError("Löschbaum-Eintrag driftete nach dem Preflight")
    finally:
        os.close(descriptor)
    return _RemovalTreeContract(
        name=name,
        dev=identity[0],
        ino=identity[1],
        mount_id=entry_mount_id,
        is_directory=is_directory,
        children=children,
    )


def _delete_bound_removal_tree(
    parent_descriptor: int,
    contract: _RemovalTreeContract,
    *,
    expected_parent_mount_id: int,
    mutation_state: Optional[_RemovalMutationState] = None,
) -> None:
    """Löscht ausschließlich den zuvor vollständig gebundenen Löschbaum."""

    if _fd_mount_id(parent_descriptor) != int(expected_parent_mount_id):
        raise BackupIntegrityError("Löschbaum-Parent-Mount driftete vor Mutation")
    before = os.stat(
        contract.name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if contract.is_directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    else:
        flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(contract.name, flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        entry_mount_id = _fd_mount_id(descriptor)
        named_after = os.stat(
            contract.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        expected_identity = (contract.dev, contract.ino)
        if (
            (int(before.st_dev), int(before.st_ino)) != expected_identity
            or (int(opened.st_dev), int(opened.st_ino)) != expected_identity
            or (int(named_after.st_dev), int(named_after.st_ino))
            != expected_identity
            or entry_mount_id != contract.mount_id
            or entry_mount_id != int(expected_parent_mount_id)
            or stat.S_ISDIR(opened.st_mode) != contract.is_directory
        ):
            raise BackupIntegrityError("Löschbaum driftete vor Mutation")
        if contract.is_directory:
            current_names = tuple(sorted(os.listdir(descriptor)))
            if current_names != tuple(child.name for child in contract.children):
                raise BackupIntegrityError("Löschbaum-Inhalt driftete vor Mutation")
            for child in contract.children:
                _delete_bound_removal_tree(
                    descriptor,
                    child,
                    expected_parent_mount_id=contract.mount_id,
                    mutation_state=mutation_state,
                )
            if os.listdir(descriptor):
                raise BackupIntegrityError("Löschbaum erhielt während der Mutation neue Einträge")
            # Ein Directory-fsync bündelt die darin vorgenommenen Unlinks. So
            # ist der Baum vor dem Receipt-Commit dauerhaft entfernt, ohne pro
            # Datei zusätzliche Schreiblast zu erzeugen.
            os.fsync(descriptor)
            if (
                _fd_mount_id(descriptor) != contract.mount_id
                or _fd_mount_id(parent_descriptor) != int(expected_parent_mount_id)
            ):
                raise BackupIntegrityError("Löschbaum-Mount driftete vor Rmdir")
            final_named = os.stat(
                contract.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (int(final_named.st_dev), int(final_named.st_ino)) != expected_identity:
                raise BackupIntegrityError("Löschbaum-Verzeichnis driftete vor Rmdir")
            if mutation_state is not None:
                mutation_state.started = True
            os.rmdir(contract.name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        else:
            if _fd_mount_id(parent_descriptor) != int(expected_parent_mount_id):
                raise BackupIntegrityError("Löschbaum-Mount driftete vor Unlink")
            if mutation_state is not None:
                mutation_state.started = True
            os.unlink(contract.name, dir_fd=parent_descriptor)
    finally:
        os.close(descriptor)


def _remove_directory_entry(
    parent_descriptor: int,
    name: str,
    expected_dev: Optional[int] = None,
    expected_ino: Optional[int] = None,
    *,
    expected_parent_mount_id: Optional[int] = None,
    expected_entry_mount_id: Optional[int] = None,
    mutation_state: Optional[_RemovalMutationState] = None,
) -> None:
    """Entfernt erst nach vollständigem nofollow- und Mount-Preflight."""

    parent_mount_id = _fd_mount_id(parent_descriptor)
    if (
        expected_parent_mount_id is not None
        and parent_mount_id != int(expected_parent_mount_id)
    ):
        raise BackupIntegrityError("Quarantäne-Root-Mount driftete")
    contract = _bind_removal_tree(
        parent_descriptor,
        name,
        expected_dev=expected_dev,
        expected_ino=expected_ino,
        expected_parent_mount_id=parent_mount_id,
        expected_entry_mount_id=expected_entry_mount_id,
    )
    if _fd_mount_id(parent_descriptor) != parent_mount_id:
        raise BackupIntegrityError("Quarantäne-Root-Mount driftete nach dem Preflight")
    _delete_bound_removal_tree(
        parent_descriptor,
        contract,
        expected_parent_mount_id=parent_mount_id,
        mutation_state=mutation_state,
    )


def _retarget_removal_tree(
    contract: _RemovalTreeContract,
    name: str,
) -> _RemovalTreeContract:
    return _RemovalTreeContract(
        name=name,
        dev=contract.dev,
        ino=contract.ino,
        mount_id=contract.mount_id,
        is_directory=contract.is_directory,
        children=contract.children,
    )


def _prune_resume_path_sort_key(path: str) -> Tuple[bool, str]:
    """Hält die versiegelte Baumwurzel vor global sortierten relativen Pfaden."""

    value = str(path)
    return value != ".", value


def _removal_tree_receipt_entries(
    contract: _RemovalTreeContract,
) -> Tuple[_RemovalTreeReceiptEntry, ...]:
    entries: List[_RemovalTreeReceiptEntry] = []

    def walk(node: _RemovalTreeContract, parts: Tuple[str, ...]) -> None:
        if len(entries) >= _PRUNE_RESUME_MAX_ENTRIES:
            raise BackupIntegrityError(
                "Löschfortsetzungsvertrag überschreitet die sichere Eintragsgrenze"
            )
        entries.append(
            _RemovalTreeReceiptEntry(
                path="." if not parts else "/".join(parts),
                dev=int(node.dev),
                ino=int(node.ino),
                is_directory=bool(node.is_directory),
            )
        )
        for child in node.children:
            walk(child, parts + (child.name,))

    walk(contract, ())
    # Writer und Reader verwenden exakt dieselbe globale Pfadordnung. Ein
    # Depth-first-Lauf allein ist nicht kanonisch, sobald etwa ``a/`` und
    # ``a-`` Geschwister sind: ``a-`` liegt lexikalisch vor ``a/x``.
    return tuple(
        sorted(
            entries,
            key=lambda entry: _prune_resume_path_sort_key(entry.path),
        )
    )


def _prune_resume_receipt_name(quarantine_name: str) -> str:
    if _PRUNE_QUARANTINE_NAME_RE.fullmatch(quarantine_name) is None:
        raise BackupIntegrityError("Quarantäne-Name ist nicht kanonisch")
    return quarantine_name + ".resume.json"


def _prune_resume_receipt_metadata_secure(metadata: os.stat_result) -> bool:
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and metadata.st_uid == 0
        and metadata.st_gid == 0
        and stat.S_IMODE(metadata.st_mode) == 0o600
    )


def _canonical_resume_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _prune_resume_envelope_bytes(
    root: Path,
    root_descriptor: int,
    quarantine_name: str,
    original_name: str,
    *,
    kind: str,
    install_root: str,
    manifest_sha256: str,
    parent_backup_id: str,
    tree_contract: _RemovalTreeContract,
) -> bytes:
    if kind not in {SYSTEM_BACKUP_KIND, WEB_SNAPSHOT_KIND, QUIESCED_OVERLAY_KIND}:
        raise BackupIntegrityError("Löschfortsetzungsvertrag besitzt keine Backup-Art")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest_sha256 or "")):
        raise BackupIntegrityError(
            "Löschfortsetzungsvertrag besitzt keine Manifest-SHA-256"
        )
    if original_name and (
        "/" in original_name
        or original_name in {".", ".."}
        or original_name.startswith(".e3dc-prune-")
    ):
        raise BackupIntegrityError("Ursprungsname im Löschfortsetzungsvertrag ist unsicher")
    _prune_resume_receipt_name(quarantine_name)
    root_metadata = os.fstat(root_descriptor)
    root_mount_id = _fd_mount_id(root_descriptor)
    if tree_contract.mount_id != root_mount_id:
        raise BackupIntegrityError(
            "Löschfortsetzungsvertrag überschreitet vor dem Versiegeln eine Mountgrenze"
        )
    entries = _removal_tree_receipt_entries(tree_contract)
    payload = {
        "schema": _PRUNE_RESUME_SCHEMA,
        "purpose": _PRUNE_RESUME_PURPOSE,
        "root": str(root),
        "root_dev": int(root_metadata.st_dev),
        "root_ino": int(root_metadata.st_ino),
        "quarantine_name": quarantine_name,
        "original_name": original_name,
        "kind": kind,
        "install_root": str(install_root),
        "manifest_sha256": str(manifest_sha256),
        "parent_backup_id": str(parent_backup_id),
        "target_dev": int(tree_contract.dev),
        "target_ino": int(tree_contract.ino),
        "tree": [
            {
                "path": entry.path,
                "dev": entry.dev,
                "ino": entry.ino,
                "type": "directory" if entry.is_directory else "file",
            }
            for entry in entries
        ],
    }
    payload_bytes = _canonical_resume_json(payload)
    envelope = {
        "payload": payload,
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
    }
    encoded = _canonical_resume_json(envelope) + b"\n"
    if len(encoded) > _PRUNE_RESUME_MAX_BYTES:
        raise BackupIntegrityError(
            "Löschfortsetzungsvertrag überschreitet die sichere Größenbegrenzung"
        )
    return encoded


def _open_bound_resume_receipt(
    root_descriptor: int,
    receipt_name: str,
) -> Tuple[int, os.stat_result, int, int]:
    if _PRUNE_RESUME_RECEIPT_NAME_RE.fullmatch(receipt_name) is None:
        raise BackupIntegrityError("Receipt-Name ist nicht kanonisch")
    root_mount_id = _fd_mount_id(root_descriptor)
    before = os.stat(receipt_name, dir_fd=root_descriptor, follow_symlinks=False)
    descriptor = os.open(
        receipt_name,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=root_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        receipt_mount_id = _fd_mount_id(descriptor)
        named_after = os.stat(
            receipt_name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        identity = (int(opened.st_dev), int(opened.st_ino))
        if (
            identity != (int(before.st_dev), int(before.st_ino))
            or identity != (int(named_after.st_dev), int(named_after.st_ino))
            or not _prune_resume_receipt_metadata_secure(opened)
            or receipt_mount_id != root_mount_id
        ):
            raise BackupIntegrityError(
                "Löschfortsetzungs-Receipt ist nicht root-privat und mountgebunden"
            )
        return descriptor, opened, root_mount_id, receipt_mount_id
    except Exception:
        os.close(descriptor)
        raise


def _read_prune_resume_contract(
    root: Path,
    root_descriptor: int,
    receipt_name: str,
    *,
    expected: Optional[_PruneResumeContract] = None,
    expected_kind: Optional[str] = None,
    expected_install_root: Optional[str] = None,
) -> _PruneResumeContract:
    match = _PRUNE_RESUME_RECEIPT_NAME_RE.fullmatch(receipt_name)
    if match is None:
        raise BackupIntegrityError("Receipt-Name ist nicht kanonisch")
    descriptor, metadata, root_mount_id, receipt_mount_id = (
        _open_bound_resume_receipt(root_descriptor, receipt_name)
    )
    try:
        chunks: List[bytes] = []
        remaining = _PRUNE_RESUME_MAX_BYTES + 1
        while remaining > 0:
            block = os.read(descriptor, min(65536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        encoded = b"".join(chunks)
        if len(encoded) > _PRUNE_RESUME_MAX_BYTES:
            raise BackupIntegrityError("Löschfortsetzungs-Receipt ist unplausibel groß")
        after = os.fstat(descriptor)
        named_after = os.stat(
            receipt_name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            (after.st_dev, after.st_ino) != (metadata.st_dev, metadata.st_ino)
            or (named_after.st_dev, named_after.st_ino)
            != (metadata.st_dev, metadata.st_ino)
            or after.st_size != metadata.st_size
            or after.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise BackupIntegrityError("Löschfortsetzungs-Receipt driftete beim Lesen")
    finally:
        os.close(descriptor)
    try:
        envelope = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("Löschfortsetzungs-Receipt ist unlesbar") from exc
    if not isinstance(envelope, dict) or set(envelope) != {
        "payload",
        "payload_sha256",
    }:
        raise BackupIntegrityError("Löschfortsetzungs-Receipt besitzt keinen exakten Vertrag")
    payload = envelope.get("payload")
    payload_sha256 = str(envelope.get("payload_sha256") or "")
    if (
        not isinstance(payload, dict)
        or not re.fullmatch(r"[0-9a-f]{64}", payload_sha256)
        or hashlib.sha256(_canonical_resume_json(payload)).hexdigest()
        != payload_sha256
    ):
        raise BackupIntegrityError("Löschfortsetzungs-Receipt besitzt keine gültige Prüfsumme")
    expected_keys = {
        "schema",
        "purpose",
        "root",
        "root_dev",
        "root_ino",
        "quarantine_name",
        "original_name",
        "kind",
        "install_root",
        "manifest_sha256",
        "parent_backup_id",
        "target_dev",
        "target_ino",
        "tree",
    }
    if set(payload) != expected_keys:
        raise BackupIntegrityError("Löschfortsetzungs-Payload besitzt unbekannte Felder")
    integer_names = ("schema", "root_dev", "root_ino", "target_dev", "target_ino")
    if any(
        isinstance(payload.get(name), bool)
        or not isinstance(payload.get(name), int)
        or int(payload[name]) < 0
        for name in integer_names
    ):
        raise BackupIntegrityError("Löschfortsetzungs-Payload besitzt ungültige Ganzzahlen")
    root_metadata = os.fstat(root_descriptor)
    quarantine_name = str(payload.get("quarantine_name") or "")
    original_name = str(payload.get("original_name") or "")
    kind = str(payload.get("kind") or "")
    install_root = str(payload.get("install_root") or "")
    manifest_sha256 = str(payload.get("manifest_sha256") or "")
    parent_backup_id = str(payload.get("parent_backup_id") or "")
    if (
        payload.get("schema") != _PRUNE_RESUME_SCHEMA
        or payload.get("purpose") != _PRUNE_RESUME_PURPOSE
        or str(payload.get("root") or "") != str(root)
        or (int(payload["root_dev"]), int(payload["root_ino"]))
        != (int(root_metadata.st_dev), int(root_metadata.st_ino))
        or quarantine_name != match.group(1)
        or kind not in {SYSTEM_BACKUP_KIND, WEB_SNAPSHOT_KIND, QUIESCED_OVERLAY_KIND}
        or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256)
        or (
            expected_kind is not None
            and kind != str(expected_kind)
        )
        or (
            expected_install_root is not None
            and install_root != str(expected_install_root)
        )
    ):
        raise BackupIntegrityError("Löschfortsetzungs-Payload widerspricht dem Rootvertrag")
    if original_name and (
        "/" in original_name
        or original_name in {".", ".."}
        or original_name.startswith(".e3dc-prune-")
    ):
        raise BackupIntegrityError("Löschfortsetzungs-Payload besitzt unsicheren Ursprungsnamen")
    raw_tree = payload.get("tree")
    if (
        not isinstance(raw_tree, list)
        or not raw_tree
        or len(raw_tree) > _PRUNE_RESUME_MAX_ENTRIES
    ):
        raise BackupIntegrityError("Löschfortsetzungs-Payload besitzt keinen Löschbaum")
    entries: List[_RemovalTreeReceiptEntry] = []
    seen_paths: Set[str] = set()
    for raw in raw_tree:
        if not isinstance(raw, dict) or set(raw) != {"path", "dev", "ino", "type"}:
            raise BackupIntegrityError("Löschfortsetzungs-Payload besitzt ungültigen Baumeintrag")
        path_text = str(raw.get("path") or "")
        if path_text != ".":
            relative = Path(path_text)
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
                or relative.as_posix() != path_text
            ):
                raise BackupIntegrityError("Löschfortsetzungs-Payload besitzt unsicheren Baumpfad")
        if path_text in seen_paths:
            raise BackupIntegrityError("Löschfortsetzungs-Payload besitzt doppelte Baumpfade")
        seen_paths.add(path_text)
        raw_dev = raw.get("dev")
        raw_ino = raw.get("ino")
        if (
            isinstance(raw_dev, bool)
            or not isinstance(raw_dev, int)
            or raw_dev < 0
            or isinstance(raw_ino, bool)
            or not isinstance(raw_ino, int)
            or raw_ino <= 0
            or raw.get("type") not in {"directory", "file"}
        ):
            raise BackupIntegrityError("Löschfortsetzungs-Payload besitzt ungültige Baumidentität")
        entries.append(
            _RemovalTreeReceiptEntry(
                path=path_text,
                dev=int(raw_dev),
                ino=int(raw_ino),
                is_directory=raw.get("type") == "directory",
            )
        )
    if entries[0].path != "." or not entries[0].is_directory:
        raise BackupIntegrityError("Löschfortsetzungs-Payload besitzt keine Verzeichniswurzel")
    if tuple(entry.path for entry in entries) != tuple(
        sorted(
            (entry.path for entry in entries),
            key=_prune_resume_path_sort_key,
        )
    ):
        raise BackupIntegrityError("Löschfortsetzungs-Payload ist nicht kanonisch sortiert")
    directory_paths = {entry.path for entry in entries if entry.is_directory}
    for entry in entries[1:]:
        parent_text = Path(entry.path).parent.as_posix()
        if parent_text == ".":
            parent_text = "."
        if parent_text not in directory_paths:
            raise BackupIntegrityError("Löschfortsetzungs-Payload besitzt verwaisten Baumpfad")
    if (
        (entries[0].dev, entries[0].ino)
        != (int(payload["target_dev"]), int(payload["target_ino"]))
    ):
        raise BackupIntegrityError("Löschfortsetzungs-Payload widerspricht der Zielidentität")
    contract = _PruneResumeContract(
        receipt_name=receipt_name,
        quarantine_name=quarantine_name,
        original_name=original_name,
        kind=kind,
        install_root=install_root,
        manifest_sha256=manifest_sha256,
        parent_backup_id=parent_backup_id,
        root_dev=int(payload["root_dev"]),
        root_ino=int(payload["root_ino"]),
        target_dev=int(payload["target_dev"]),
        target_ino=int(payload["target_ino"]),
        tree=tuple(entries),
        receipt_dev=int(metadata.st_dev),
        receipt_ino=int(metadata.st_ino),
        root_mount_id=root_mount_id,
        receipt_mount_id=receipt_mount_id,
    )
    if expected is not None and contract != expected:
        raise BackupIntegrityError("Löschfortsetzungs-Receipt driftete gegenüber der Bindung")
    return contract


def _write_prune_resume_receipt(
    root: Path,
    root_descriptor: int,
    quarantine_name: str,
    original_name: str,
    *,
    kind: str,
    install_root: str,
    manifest_sha256: str,
    parent_backup_id: str,
    tree_contract: _RemovalTreeContract,
) -> _PruneResumeContract:
    receipt_name = _prune_resume_receipt_name(quarantine_name)
    encoded = _prune_resume_envelope_bytes(
        root,
        root_descriptor,
        quarantine_name,
        original_name,
        kind=kind,
        install_root=install_root,
        manifest_sha256=manifest_sha256,
        parent_backup_id=parent_backup_id,
        tree_contract=tree_contract,
    )
    try:
        os.stat(receipt_name, dir_fd=root_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise BackupIntegrityError("Löschfortsetzungs-Receipt existiert bereits")
    stage_name = "{}.resume-stage-{}.json".format(
        quarantine_name,
        uuid.uuid4().hex,
    )
    descriptor = -1
    stage_identity: Optional[Tuple[int, int]] = None
    final_created = False
    try:
        descriptor = os.open(
            stage_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=root_descriptor,
        )
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        stage_identity = (int(metadata.st_dev), int(metadata.st_ino))
        written = 0
        while written < len(encoded):
            count = os.write(descriptor, encoded[written:])
            if count <= 0:
                raise OSError("Receipt-Write lieferte keinen Fortschritt")
            written += count
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            metadata.st_size != len(encoded)
            or not _prune_resume_receipt_metadata_secure(metadata)
            or _fd_mount_id(descriptor) != _fd_mount_id(root_descriptor)
        ):
            raise BackupIntegrityError("Receipt-Stage ist nicht sicher gebunden")
        os.close(descriptor)
        descriptor = -1
        stage_contract = _bind_removal_tree(
            root_descriptor,
            stage_name,
            expected_dev=stage_identity[0],
            expected_ino=stage_identity[1],
            expected_parent_mount_id=_fd_mount_id(root_descriptor),
            expected_entry_mount_id=_fd_mount_id(root_descriptor),
        )
        if stage_contract.is_directory:
            raise BackupIntegrityError("Receipt-Stage ist kein Dateieintrag")
        os.rename(
            stage_name,
            receipt_name,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
        )
        final_created = True
        os.fsync(root_descriptor)
        return _read_prune_resume_contract(root, root_descriptor, receipt_name)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        cleanup_name = receipt_name if final_created else stage_name
        try:
            current = os.stat(
                cleanup_name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if stage_identity is not None and (
                int(current.st_dev),
                int(current.st_ino),
            ) == stage_identity:
                _remove_directory_entry(
                    root_descriptor,
                    cleanup_name,
                    stage_identity[0],
                    stage_identity[1],
                    expected_parent_mount_id=_fd_mount_id(root_descriptor),
                    expected_entry_mount_id=_fd_mount_id(root_descriptor),
                )
                os.fsync(root_descriptor)
        except Exception:
            pass
        raise


def _bind_remaining_resume_tree(
    root_descriptor: int,
    contract: _PruneResumeContract,
) -> Optional[_RemovalTreeContract]:
    root_metadata = os.fstat(root_descriptor)
    root_mount_id = _fd_mount_id(root_descriptor)
    if (
        (int(root_metadata.st_dev), int(root_metadata.st_ino))
        != (contract.root_dev, contract.root_ino)
    ):
        raise BackupIntegrityError("Löschfortsetzungs-Root driftete")
    try:
        current = _bind_removal_tree(
            root_descriptor,
            contract.quarantine_name,
            expected_dev=contract.target_dev,
            expected_ino=contract.target_ino,
            expected_parent_mount_id=root_mount_id,
            expected_entry_mount_id=root_mount_id,
        )
    except FileNotFoundError:
        return None
    expected_entries = {
        entry.path: (entry.dev, entry.ino, entry.is_directory)
        for entry in contract.tree
    }
    current_entries = _removal_tree_receipt_entries(current)
    for entry in current_entries:
        if expected_entries.get(entry.path) != (
            entry.dev,
            entry.ino,
            entry.is_directory,
        ):
            raise BackupIntegrityError(
                "Verbliebener Quarantänebaum widerspricht dem versiegelten Receipt"
            )
    return current


def _resume_tree_is_complete_original(
    root_descriptor: int,
    contract: _PruneResumeContract,
) -> bool:
    if not contract.original_name:
        return False
    try:
        original = _bind_removal_tree(
            root_descriptor,
            contract.original_name,
            expected_dev=contract.target_dev,
            expected_ino=contract.target_ino,
            expected_parent_mount_id=_fd_mount_id(root_descriptor),
            expected_entry_mount_id=_fd_mount_id(root_descriptor),
        )
    except FileNotFoundError:
        return False
    return _removal_tree_receipt_entries(original) == contract.tree


def _discard_prune_resume_receipt(
    root: Path,
    root_descriptor: int,
    contract: _PruneResumeContract,
) -> None:
    rebound = _read_prune_resume_contract(
        root,
        root_descriptor,
        contract.receipt_name,
        expected=contract,
    )
    _remove_directory_entry(
        root_descriptor,
        rebound.receipt_name,
        rebound.receipt_dev,
        rebound.receipt_ino,
        expected_parent_mount_id=rebound.root_mount_id,
        expected_entry_mount_id=rebound.receipt_mount_id,
    )
    os.fsync(root_descriptor)


def _continue_prune_resume(
    root: Path,
    root_descriptor: int,
    contract: _PruneResumeContract,
    *,
    mutation_state: Optional[_RemovalMutationState] = None,
) -> None:
    rebound = _read_prune_resume_contract(
        root,
        root_descriptor,
        contract.receipt_name,
        expected=contract,
    )
    current = _bind_remaining_resume_tree(root_descriptor, rebound)
    if current is not None:
        _delete_bound_removal_tree(
            root_descriptor,
            current,
            expected_parent_mount_id=_fd_mount_id(root_descriptor),
            mutation_state=mutation_state,
        )
        os.fsync(root_descriptor)
    _discard_prune_resume_receipt(root, root_descriptor, rebound)


def _discard_secure_prune_resume_stage(
    root_descriptor: int,
    stage_name: str,
) -> None:
    if _PRUNE_RESUME_STAGE_NAME_RE.fullmatch(stage_name) is None:
        raise BackupIntegrityError("Receipt-Stage-Name ist nicht kanonisch")
    metadata = os.stat(stage_name, dir_fd=root_descriptor, follow_symlinks=False)
    if not _prune_resume_receipt_metadata_secure(metadata):
        raise BackupIntegrityError("Receipt-Stage ist nicht root-privat")
    _remove_directory_entry(
        root_descriptor,
        stage_name,
        int(metadata.st_dev),
        int(metadata.st_ino),
        expected_parent_mount_id=_fd_mount_id(root_descriptor),
        expected_entry_mount_id=_fd_mount_id(root_descriptor),
    )
    os.fsync(root_descriptor)


def _quiesced_name_parts(name: str) -> Optional[Tuple[str, str]]:
    match = _QUIESCED_OVERLAY_NAME_RE.fullmatch(str(name or ""))
    if match is None:
        return None
    parent_name, transaction_id = match.groups()
    if parent_name in {ROOT_MARKER_NAME, "web_installer"} or parent_name.startswith(
        ".e3dc-prune-"
    ):
        return None
    return parent_name, transaction_id


def _overlay_directory_is_secure(metadata: os.stat_result) -> bool:
    return bool(
        not stat.S_ISLNK(metadata.st_mode)
        and stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_gid == 0
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )


def _verify_system_backup_contract(
    root: Path,
    root_descriptor: int,
    name: str,
    install: Path,
) -> _SystemBackupContract:
    path = root / name
    before = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise BackupIntegrityError("Parent-Backup ist kein echtes Verzeichnis")
    manifest = verify_backup(path, expected_kind=SYSTEM_BACKUP_KIND)
    backup_id = _normalized_backup_id(manifest.get("backup_id"))
    if str(manifest.get("install_root") or "") != str(install):
        raise BackupIntegrityError(
            "Parent-Backup gehört nicht zur aktuellen Installation"
        )
    manifest_sha256 = verified_manifest_sha256(
        path,
        expected_kind=SYSTEM_BACKUP_KIND,
    )
    after = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise BackupIntegrityError(
            "Parent-Backup driftete während der Vertragsbindung"
        )
    return _SystemBackupContract(
        path=path,
        backup_id=backup_id,
        install_root=str(install),
        manifest_sha256=manifest_sha256,
        dev=int(before.st_dev),
        ino=int(before.st_ino),
    )


def _verify_quiesced_overlay_contract(
    root: Path,
    root_descriptor: int,
    name: str,
    install: Path,
) -> _QuiescedOverlayContract:
    parsed = _quiesced_name_parts(name)
    if parsed is None:
        raise BackupIntegrityError(
            "Name der ruhenden Daten-Nachsicherung ist nicht kanonisch"
        )
    parent_name, name_transaction_id = parsed
    path = root / name
    before = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
    if not _overlay_directory_is_secure(before):
        raise BackupIntegrityError(
            "Ruhende Daten-Nachsicherung ist nicht root:root 0700 gebunden"
        )
    manifest = verify_backup(path, expected_kind=QUIESCED_OVERLAY_KIND)
    if set(manifest) != _QUIESCED_OVERLAY_KEYS:
        raise BackupIntegrityError(
            "Overlay-Manifest besitzt einen unbekannten oder unvollständigen Vertrag"
        )
    transaction_id = _normalized_transaction_id(manifest.get("transaction_id"))
    backup_id = _normalized_backup_id(manifest.get("backup_id"))
    parent_backup_id = _normalized_backup_id(
        manifest.get("parent_backup_id"),
        label="Parent-Backup-ID",
    )
    if transaction_id != name_transaction_id:
        raise BackupIntegrityError(
            "Transaktions-ID widerspricht dem Namen der ruhenden Daten-Nachsicherung"
        )
    if str(manifest.get("install_root") or "") != str(install):
        raise BackupIntegrityError(
            "Ruhende Daten-Nachsicherung gehört nicht zur aktuellen Installation"
        )
    manifest_sha256 = verified_manifest_sha256(
        path,
        expected_kind=QUIESCED_OVERLAY_KIND,
    )
    after = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise BackupIntegrityError(
            "Ruhende Daten-Nachsicherung driftete während der Vertragsbindung"
        )
    return _QuiescedOverlayContract(
        path=path,
        parent_name=parent_name,
        transaction_id=transaction_id,
        backup_id=backup_id,
        parent_backup_id=parent_backup_id,
        install_root=str(install),
        manifest_sha256=manifest_sha256,
        dev=int(before.st_dev),
        ino=int(before.st_ino),
    )


def _validate_overlay_manifest_against_contract(
    manifest: Dict[str, object],
    contract: _QuiescedOverlayContract,
) -> None:
    if (
        set(manifest) != _QUIESCED_OVERLAY_KEYS
        or manifest.get("kind") != QUIESCED_OVERLAY_KIND
        or str(manifest.get("state") or "") != "complete"
        or _normalized_transaction_id(manifest.get("transaction_id"))
        != contract.transaction_id
        or _normalized_backup_id(manifest.get("backup_id")) != contract.backup_id
        or _normalized_backup_id(
            manifest.get("parent_backup_id"),
            label="Parent-Backup-ID",
        )
        != contract.parent_backup_id
        or str(manifest.get("install_root") or "") != contract.install_root
    ):
        raise BackupIntegrityError(
            "Overlay-Manifest driftete gegenüber dem gebundenen Löschvertrag"
        )


def _system_backup_index(
    root: Path,
    root_descriptor: int,
    install: Path,
) -> Tuple[Dict[str, _SystemBackupContract], Dict[str, List[_SystemBackupContract]]]:
    by_name: Dict[str, _SystemBackupContract] = {}
    by_id: Dict[str, List[_SystemBackupContract]] = {}
    for name in sorted(os.listdir(root_descriptor)):
        if (
            name in {ROOT_MARKER_NAME, "web_installer"}
            or name.startswith(".")
        ):
            continue
        try:
            metadata = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                continue
            contract = _verify_system_backup_contract(
                root,
                root_descriptor,
                name,
                install,
            )
        except Exception:
            continue
        by_name[name] = contract
        by_id.setdefault(contract.backup_id, []).append(contract)
    return by_name, by_id


def _verify_prune_quarantine_contract(
    root: Path,
    root_descriptor: int,
    name: str,
    install: Path,
    *,
    expected: Optional[_PruneQuarantineContract] = None,
) -> _PruneQuarantineContract:
    """Bindet nur einen vollständig verifizierten eigenen Quarantäne-Rest."""

    if _PRUNE_QUARANTINE_NAME_RE.fullmatch(name) is None:
        raise BackupIntegrityError("Quarantäne-Name gehört nicht zum eigenen Vertrag")

    opened_root = os.fstat(root_descriptor)
    opened_root_mount_id = _fd_mount_id(root_descriptor)
    named_root = os.stat(root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(named_root.st_mode)
        or (named_root.st_dev, named_root.st_ino)
        != (opened_root.st_dev, opened_root.st_ino)
    ):
        raise BackupIntegrityError("Backup-Root driftete vor der Quarantäne-Prüfung")

    before = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise BackupIntegrityError("Quarantäne-Rest ist kein echtes Verzeichnis")
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=root_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        entry_mount_id = _fd_mount_id(descriptor)
    finally:
        os.close(descriptor)
    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
        raise BackupIntegrityError("Quarantäne-Rest driftete beim nofollow-Öffnen")
    if entry_mount_id != opened_root_mount_id:
        raise BackupIntegrityError(
            "Quarantäne-Rest überschreitet eine fremde oder gebundene Mountgrenze"
        )

    path = root / name
    manifest = verify_backup(path)
    kind = str(manifest.get("kind") or "")
    if kind not in {SYSTEM_BACKUP_KIND, QUIESCED_OVERLAY_KIND}:
        raise BackupIntegrityError("Quarantäne-Rest besitzt keine freigegebene Backup-Art")
    manifest_install_root = str(manifest.get("install_root") or "")
    if manifest_install_root != str(install):
        raise BackupIntegrityError(
            "Quarantäne-Rest gehört nicht zur aktuellen Installation"
        )
    parent_backup_id = ""
    if kind == QUIESCED_OVERLAY_KIND:
        if not _overlay_directory_is_secure(before):
            raise BackupIntegrityError(
                "Quarantäne-Rest besitzt nicht den privaten Overlay-Modus"
            )
        if set(manifest) != _QUIESCED_OVERLAY_KEYS:
            raise BackupIntegrityError(
                "Quarantäne-Rest besitzt keinen vollständigen Overlay-Vertrag"
            )
        _normalized_backup_id(manifest.get("backup_id"))
        _normalized_transaction_id(manifest.get("transaction_id"))
        parent_backup_id = _normalized_backup_id(
            manifest.get("parent_backup_id"),
            label="Parent-Backup-ID",
        )
    manifest_sha256 = verified_manifest_sha256(path, expected_kind=kind)

    after = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
    final_root = os.fstat(root_descriptor)
    final_root_mount_id = _fd_mount_id(root_descriptor)
    final_named_root = os.stat(root, follow_symlinks=False)
    if (
        (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or (final_root.st_dev, final_root.st_ino)
        != (opened_root.st_dev, opened_root.st_ino)
        or final_root_mount_id != opened_root_mount_id
        or not stat.S_ISDIR(final_named_root.st_mode)
        or (final_named_root.st_dev, final_named_root.st_ino)
        != (final_root.st_dev, final_root.st_ino)
    ):
        raise BackupIntegrityError(
            "Backup-Root oder Quarantäne-Rest driftete während der Verifikation"
        )

    contract = _PruneQuarantineContract(
        path=path,
        kind=kind,
        install_root=manifest_install_root,
        manifest_sha256=manifest_sha256,
        parent_backup_id=parent_backup_id,
        dev=int(after.st_dev),
        ino=int(after.st_ino),
        root_dev=int(final_root.st_dev),
        root_ino=int(final_root.st_ino),
        root_mount_id=final_root_mount_id,
        mount_id=entry_mount_id,
    )
    if expected is not None and contract != expected:
        raise BackupIntegrityError(
            "Quarantäne-Rest driftete gegenüber dem gebundenen Löschvertrag"
        )
    return contract


def _overlay_restore_guard(
    root: Path,
    root_descriptor: int,
    overlay: _QuiescedOverlayContract,
    parent: _SystemBackupContract,
) -> QuiescedOverlayRestoreGuard:
    root_metadata = os.fstat(root_descriptor)
    return QuiescedOverlayRestoreGuard(
        transaction_id=overlay.transaction_id,
        overlay_dir=str(overlay.path),
        overlay_dev=overlay.dev,
        overlay_ino=overlay.ino,
        backup_id=overlay.backup_id,
        manifest_sha256=overlay.manifest_sha256,
        install_root=overlay.install_root,
        parent_backup_dir=str(parent.path),
        parent_backup_dev=parent.dev,
        parent_backup_ino=parent.ino,
        parent_backup_id=parent.backup_id,
        parent_backup_manifest_sha256=parent.manifest_sha256,
        collection_dir=str(root),
        collection_dev=int(root_metadata.st_dev),
        collection_ino=int(root_metadata.st_ino),
    )


def _restore_quarantine_name(
    root_descriptor: int,
    quarantine: str,
    original: str,
    *,
    expected_dev: Optional[int] = None,
    expected_ino: Optional[int] = None,
    expected_parent_mount_id: Optional[int] = None,
    expected_entry_mount_id: Optional[int] = None,
    expected_tree_contract: Optional[_RemovalTreeContract] = None,
) -> bool:
    restored = False
    try:
        os.stat(original, dir_fd=root_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        try:
            rebound = _bind_removal_tree(
                root_descriptor,
                quarantine,
                expected_dev=expected_dev,
                expected_ino=expected_ino,
                expected_parent_mount_id=expected_parent_mount_id,
                expected_entry_mount_id=expected_entry_mount_id,
            )
            if expected_tree_contract is not None and rebound != _retarget_removal_tree(
                expected_tree_contract,
                quarantine,
            ):
                raise BackupIntegrityError(
                    "Quarantänebaum driftete vor der Namenswiederherstellung"
                )
            os.rename(
                quarantine,
                original,
                src_dir_fd=root_descriptor,
                dst_dir_fd=root_descriptor,
            )
            restored = True
            os.fsync(root_descriptor)
        except Exception:
            return restored
    except Exception:
        return False
    return restored


def _delete_via_resumable_quarantine(
    root: Path,
    root_descriptor: int,
    original_name: str,
    quarantine_name: str,
    *,
    expected_dev: int,
    expected_ino: int,
    expected_parent_mount_id: int,
    expected_entry_mount_id: int,
    kind: str,
    install_root: str,
    manifest_sha256: str,
    parent_backup_id: str = "",
) -> None:
    """Versiegelt vollständig, benennt um und löscht crashfest fortsetzbar."""

    preflight = _bind_removal_tree(
        root_descriptor,
        original_name,
        expected_dev=expected_dev,
        expected_ino=expected_ino,
        expected_parent_mount_id=expected_parent_mount_id,
        expected_entry_mount_id=expected_entry_mount_id,
    )
    receipt = _write_prune_resume_receipt(
        root,
        root_descriptor,
        quarantine_name,
        original_name,
        kind=kind,
        install_root=install_root,
        manifest_sha256=manifest_sha256,
        parent_backup_id=parent_backup_id,
        tree_contract=preflight,
    )
    renamed = False
    mutation_state = _RemovalMutationState()
    try:
        rebound = _bind_removal_tree(
            root_descriptor,
            original_name,
            expected_dev=expected_dev,
            expected_ino=expected_ino,
            expected_parent_mount_id=expected_parent_mount_id,
            expected_entry_mount_id=expected_entry_mount_id,
        )
        if rebound != preflight:
            raise BackupIntegrityError(
                "Löschbaum driftete zwischen Receipt und Quarantäne-Rename"
            )
        os.rename(
            original_name,
            quarantine_name,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
        )
        renamed = True
        os.fsync(root_descriptor)
        quarantined = _bind_removal_tree(
            root_descriptor,
            quarantine_name,
            expected_dev=expected_dev,
            expected_ino=expected_ino,
            expected_parent_mount_id=expected_parent_mount_id,
            expected_entry_mount_id=expected_entry_mount_id,
        )
        if quarantined != _retarget_removal_tree(preflight, quarantine_name):
            raise BackupIntegrityError(
                "Quarantänebaum driftete nach dem atomaren Rename"
            )
        _continue_prune_resume(
            root,
            root_descriptor,
            receipt,
            mutation_state=mutation_state,
        )
    except Exception:
        if renamed and not mutation_state.started:
            restored = _restore_quarantine_name(
                root_descriptor,
                quarantine_name,
                original_name,
                expected_dev=expected_dev,
                expected_ino=expected_ino,
                expected_parent_mount_id=expected_parent_mount_id,
                expected_entry_mount_id=expected_entry_mount_id,
                expected_tree_contract=preflight,
            )
            if restored:
                try:
                    _discard_prune_resume_receipt(
                        root,
                        root_descriptor,
                        receipt,
                    )
                except Exception:
                    pass
        elif not renamed:
            try:
                _discard_prune_resume_receipt(root, root_descriptor, receipt)
            except Exception:
                pass
        raise


def _delete_existing_quarantine_with_resume(
    root: Path,
    root_descriptor: int,
    quarantine: _PruneQuarantineContract,
) -> None:
    """Versiegelt eine valide Altquarantäne vor ihrem ersten rekursiven Unlink."""

    preflight = _bind_removal_tree(
        root_descriptor,
        quarantine.path.name,
        expected_dev=quarantine.dev,
        expected_ino=quarantine.ino,
        expected_parent_mount_id=quarantine.root_mount_id,
        expected_entry_mount_id=quarantine.mount_id,
    )
    receipt = _write_prune_resume_receipt(
        root,
        root_descriptor,
        quarantine.path.name,
        "",
        kind=quarantine.kind,
        install_root=quarantine.install_root,
        manifest_sha256=quarantine.manifest_sha256,
        parent_backup_id=quarantine.parent_backup_id,
        tree_contract=preflight,
    )
    mutation_state = _RemovalMutationState()
    try:
        rebound = _bind_removal_tree(
            root_descriptor,
            quarantine.path.name,
            expected_dev=quarantine.dev,
            expected_ino=quarantine.ino,
            expected_parent_mount_id=quarantine.root_mount_id,
            expected_entry_mount_id=quarantine.mount_id,
        )
        if rebound != preflight:
            raise BackupIntegrityError(
                "Altquarantäne driftete zwischen Receipt und Löschbeginn"
            )
        _continue_prune_resume(
            root,
            root_descriptor,
            receipt,
            mutation_state=mutation_state,
        )
    except Exception:
        if not mutation_state.started:
            try:
                _discard_prune_resume_receipt(root, root_descriptor, receipt)
            except Exception:
                pass
        raise


def _load_prune_resume_receipts(
    root: Path,
    root_descriptor: int,
    *,
    expected_kind: Optional[str] = None,
    expected_install_root: Optional[str] = None,
) -> Tuple[
    Dict[str, _PruneResumeContract],
    Dict[str, str],
    Tuple[str, ...],
]:
    receipts: Dict[str, _PruneResumeContract] = {}
    invalid: Dict[str, str] = {}
    stages: List[str] = []
    for name in sorted(os.listdir(root_descriptor)):
        receipt_match = _PRUNE_RESUME_RECEIPT_NAME_RE.fullmatch(name)
        if receipt_match is not None:
            quarantine_name = receipt_match.group(1)
            try:
                receipt = _read_prune_resume_contract(
                    root,
                    root_descriptor,
                    name,
                    expected_kind=expected_kind,
                    expected_install_root=expected_install_root,
                )
            except Exception as exc:
                invalid[quarantine_name] = str(exc)
            else:
                receipts[quarantine_name] = receipt
            continue
        if _PRUNE_RESUME_STAGE_NAME_RE.fullmatch(name) is not None:
            stages.append(name)
    return receipts, invalid, tuple(stages)


def _reconcile_absent_prune_resume(
    root: Path,
    root_descriptor: int,
    contract: _PruneResumeContract,
    *,
    dry_run: bool,
) -> bool:
    try:
        os.stat(
            contract.quarantine_name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        return False
    if contract.original_name:
        try:
            original_metadata = os.stat(
                contract.original_name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            if (
                int(original_metadata.st_dev),
                int(original_metadata.st_ino),
            ) != (contract.target_dev, contract.target_ino):
                raise BackupIntegrityError(
                    "Ursprungsname widerspricht einem verwaisten Lösch-Receipt"
                )
            if not _resume_tree_is_complete_original(root_descriptor, contract):
                raise BackupIntegrityError(
                    "Ursprungsbaum ist gegenüber dem Lösch-Receipt unvollständig"
                )
    if not dry_run:
        _discard_prune_resume_receipt(root, root_descriptor, contract)
    return True


def _generic_verified_quarantine_contract(
    root: Path,
    root_descriptor: int,
    name: str,
    *,
    expected_kind: str,
) -> _PruneQuarantineContract:
    before = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
    mount_contract = _bind_directory_mount_contract(
        root_descriptor,
        name,
        expected_dev=int(before.st_dev),
        expected_ino=int(before.st_ino),
    )
    manifest = verify_backup(root / name, expected_kind=expected_kind)
    manifest_sha256 = verified_manifest_sha256(
        root / name,
        expected_kind=expected_kind,
    )
    rebound = _bind_directory_mount_contract(
        root_descriptor,
        name,
        expected_dev=mount_contract.dev,
        expected_ino=mount_contract.ino,
        expected_parent_mount_id=mount_contract.parent_mount_id,
        expected_entry_mount_id=mount_contract.entry_mount_id,
    )
    return _PruneQuarantineContract(
        path=root / name,
        kind=expected_kind,
        install_root=str(manifest.get("install_root") or ""),
        manifest_sha256=manifest_sha256,
        parent_backup_id=str(manifest.get("parent_backup_id") or ""),
        dev=rebound.dev,
        ino=rebound.ino,
        root_dev=int(os.fstat(root_descriptor).st_dev),
        root_ino=int(os.fstat(root_descriptor).st_ino),
        root_mount_id=rebound.parent_mount_id,
        mount_id=rebound.entry_mount_id,
    )


def delete_bound_quiesced_overlay(
    overlay_path: PathValue,
    *,
    guard: QuiescedOverlayRestoreGuard,
) -> None:
    """Entfernt exakt das vom besitzenden Updater versiegelte Overlay."""

    _require_bound_update_lock()
    target = _lexical_absolute(overlay_path)
    if not isinstance(guard, QuiescedOverlayRestoreGuard) or target != _lexical_absolute(
        guard.overlay_dir
    ):
        raise BackupIntegrityError(
            "Overlay-Cleanup widerspricht dem gebundenen Update-Guard"
        )
    manifest = validate_quiesced_overlay_guard(guard)
    parsed = _quiesced_name_parts(target.name)
    if parsed is None:
        raise BackupIntegrityError("Overlay-Cleanup besitzt keinen kanonischen Namen")
    parent_name, transaction_id = parsed
    contract = _QuiescedOverlayContract(
        path=target,
        parent_name=parent_name,
        transaction_id=transaction_id,
        backup_id=_normalized_backup_id(manifest.get("backup_id")),
        parent_backup_id=_normalized_backup_id(
            manifest.get("parent_backup_id"),
            label="Parent-Backup-ID",
        ),
        install_root=str(_lexical_absolute(guard.install_root)),
        manifest_sha256=str(guard.manifest_sha256),
        dev=int(guard.overlay_dev),
        ino=int(guard.overlay_ino),
    )
    _validate_overlay_manifest_against_contract(manifest, contract)
    root = validate_existing_backup_root(
        _lexical_absolute(guard.collection_dir),
        _lexical_absolute(guard.install_root),
    )
    parent = _lexical_absolute(guard.parent_backup_dir)
    if target.parent != root or parent.parent != root or parent.name != parent_name:
        raise BackupIntegrityError(
            "Overlay-Cleanup ist nicht an Collection und Parent gebunden"
        )

    root_descriptor = os.open(
        str(root),
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    quarantine = ".e3dc-prune-{}".format(uuid.uuid4().hex)
    try:
        root_metadata = os.fstat(root_descriptor)
        before = os.stat(target.name, dir_fd=root_descriptor, follow_symlinks=False)
        parent_before = os.stat(
            parent.name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            (root_metadata.st_dev, root_metadata.st_ino)
            != (guard.collection_dev, guard.collection_ino)
            or (before.st_dev, before.st_ino) != (guard.overlay_dev, guard.overlay_ino)
            or (parent_before.st_dev, parent_before.st_ino)
            != (guard.parent_backup_dev, guard.parent_backup_ino)
        ):
            raise BackupIntegrityError(
                "Overlay-, Parent- oder Collection-Inode driftete vor Cleanup"
            )
        parent_manifest = verify_backup(
            parent,
            expected_kind=SYSTEM_BACKUP_KIND,
            expected_manifest_sha256=guard.parent_backup_manifest_sha256,
        )
        if (
            _normalized_backup_id(parent_manifest.get("backup_id"))
            != contract.parent_backup_id
            or str(parent_manifest.get("install_root") or "")
            != contract.install_root
        ):
            raise BackupIntegrityError(
                "Parent-Manifest driftete vor dem Overlay-Cleanup"
            )
        rebound = verify_backup(
            target,
            expected_kind=QUIESCED_OVERLAY_KIND,
            expected_manifest_sha256=guard.manifest_sha256,
        )
        _validate_overlay_manifest_against_contract(rebound, contract)
        mount_contract = _bind_directory_mount_contract(
            root_descriptor,
            target.name,
            expected_dev=guard.overlay_dev,
            expected_ino=guard.overlay_ino,
        )
        _delete_via_resumable_quarantine(
            root,
            root_descriptor,
            target.name,
            quarantine,
            expected_dev=int(guard.overlay_dev),
            expected_ino=int(guard.overlay_ino),
            expected_parent_mount_id=mount_contract.parent_mount_id,
            expected_entry_mount_id=mount_contract.entry_mount_id,
            kind=QUIESCED_OVERLAY_KIND,
            install_root=contract.install_root,
            manifest_sha256=str(guard.manifest_sha256),
            parent_backup_id=contract.parent_backup_id,
        )
    finally:
        os.close(root_descriptor)


def _delete_orphan_quiesced_overlay(
    root: Path,
    install: Path,
    contract: _QuiescedOverlayContract,
    *,
    root_dev: int,
    root_ino: int,
) -> None:
    root_descriptor = os.open(
        str(root),
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    quarantine = ".e3dc-prune-{}".format(uuid.uuid4().hex)
    try:
        root_metadata = os.fstat(root_descriptor)
        if (root_metadata.st_dev, root_metadata.st_ino) != (root_dev, root_ino):
            raise BackupIntegrityError("Backup-Root driftete vor verwaistem Cleanup")
        before = os.stat(
            contract.path.name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (before.st_dev, before.st_ino) != (contract.dev, contract.ino):
            raise BackupIntegrityError("Verwaistes Overlay driftete vor Cleanup")
        try:
            os.stat(
                contract.parent_name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise BackupIntegrityError(
                "Benannter Parent entstand vor dem verwaisten Overlay-Cleanup"
            )
        rebound = verify_backup(
            contract.path,
            expected_kind=QUIESCED_OVERLAY_KIND,
            expected_manifest_sha256=contract.manifest_sha256,
        )
        _validate_overlay_manifest_against_contract(rebound, contract)
        if str(rebound.get("install_root") or "") != str(install):
            raise BackupIntegrityError("Overlay-Installationsbindung driftete")
        mount_contract = _bind_directory_mount_contract(
            root_descriptor,
            contract.path.name,
            expected_dev=contract.dev,
            expected_ino=contract.ino,
        )
        _delete_via_resumable_quarantine(
            root,
            root_descriptor,
            contract.path.name,
            quarantine,
            expected_dev=contract.dev,
            expected_ino=contract.ino,
            expected_parent_mount_id=mount_contract.parent_mount_id,
            expected_entry_mount_id=mount_contract.entry_mount_id,
            kind=QUIESCED_OVERLAY_KIND,
            install_root=contract.install_root,
            manifest_sha256=contract.manifest_sha256,
            parent_backup_id=contract.parent_backup_id,
        )
    finally:
        os.close(root_descriptor)


def _blocked_backup_prune_result(
    backup_root: PathValue,
    *,
    keep_count: int,
    min_keep_count: int,
    max_age_days: Optional[int],
    expected_kind: str,
    blocker: str,
    reason: str,
    dry_run: bool,
    success: bool = True,
) -> Dict[str, Any]:
    """Beschreibt einen absichtlichen, vollständig mutationsfreien Retention-No-op."""

    normalized_keep = min(
        MAX_BACKUP_FAMILY_COUNT,
        max(1, int(keep_count or 1)),
    )
    normalized_minimum = max(
        1,
        min(int(min_keep_count or 1), normalized_keep),
    )
    return {
        "success": bool(success),
        "blocked": True,
        "blocker": blocker,
        "limit_satisfied": False,
        "root": str(_lexical_absolute(backup_root)),
        "expected_kind": expected_kind,
        "keep_count": normalized_keep,
        "min_keep_count": normalized_minimum,
        "max_age_days": (
            None if max_age_days is None else max(0, int(max_age_days))
        ),
        "removed": [],
        "kept": [],
        "skipped": [
            {
                "path": str(_lexical_absolute(backup_root)),
                "reason": reason,
            }
        ],
        "dry_run": bool(dry_run),
    }


def _prune_backup_dir_locked(
    backup_root: PathValue,
    keep_count: int,
    preserve_names: Optional[Iterable[str]] = None,
    logger: Any = None,
    dry_run: bool = False,
    max_age_days: Optional[int] = None,
    min_keep_count: int = 1,
    now: Optional[float] = None,
    expected_kind: str = SYSTEM_BACKUP_KIND,
    preserve_paths: Optional[Iterable[PathValue]] = None,
    recognized_paths: Optional[Iterable[PathValue]] = None,
) -> Dict[str, Any]:
    """Rotiert unter gebundenem Update-Lock nur verifizierte Kind-Manifeste.

    Unbekannte Verzeichnisse, Symlinks, unvollständige Sicherungen und fremde
    Artefakte sind niemals Kandidaten. Zum Löschen wird das ausgewählte
    Verzeichnis zunächst innerhalb seiner vertrauenswürdigen Sammlung atomar
    umbenannt und danach Deskriptor für Deskriptor entfernt.
    """

    _require_bound_update_lock()
    _assert_no_update_state_blockers()
    root = _lexical_absolute(backup_root)
    keep_count = min(
        MAX_BACKUP_FAMILY_COUNT,
        max(1, int(keep_count or 1)),
    )
    min_keep_count = max(1, min(int(min_keep_count or 1), keep_count))
    max_age_days = None if max_age_days is None else max(0, int(max_age_days))
    now_ts = time.time() if now is None else float(now)
    preserved_names = {str(name) for name in (preserve_names or ())}
    preserved_paths = {_lexical_absolute(path) for path in (preserve_paths or ())}
    recognized_paths = {
        _lexical_absolute(path) for path in (recognized_paths or ())
    }
    result: Dict[str, Any] = {
        "success": True,
        "blocked": False,
        "blocker": "",
        "limit_satisfied": False,
        "root": str(root),
        "expected_kind": expected_kind,
        "keep_count": keep_count,
        "min_keep_count": min_keep_count,
        "max_age_days": max_age_days,
        "removed": [],
        "kept": [],
        "skipped": [],
        "unclassified": [],
        "dry_run": bool(dry_run),
    }

    def record_unclassified(
        path: Path,
        reason: str,
        detail: Optional[str] = None,
    ) -> None:
        entry = {"path": str(path), "reason": reason}
        if detail:
            entry["detail"] = detail
        result["skipped"].append(dict(entry))
        result["unclassified"].append(dict(entry))

    out_of_scope_protections = sorted(
        {
            path
            for path in preserved_paths | recognized_paths
            if path.parent != root
        },
        key=str,
    )
    if out_of_scope_protections:
        message = "Schutzpfad liegt nicht direkt im gebundenen Backup-Root"
        result.update(
            {
                "success": False,
                "blocked": True,
                "blocker": message,
                "skipped": [
                    {"path": str(path), "reason": "protected_path_out_of_scope"}
                    for path in out_of_scope_protections
                ],
            }
        )
        return result
    if not root.exists():
        if preserved_paths or recognized_paths:
            result.update(
                {
                    "success": False,
                    "blocked": True,
                    "blocker": "Geschützter Backup-Root oder Schutzpfad fehlt",
                    "skipped": [
                        {"path": str(path), "reason": "protected_backup_missing"}
                        for path in sorted(
                            preserved_paths | recognized_paths,
                            key=str,
                        )
                    ],
                }
            )
            return result
        result["missing"] = True
        result["limit_satisfied"] = True
        return result
    try:
        _assert_no_symlink_components(root)
        root_descriptor = os.open(
            str(root),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except Exception as exc:
        result["success"] = False
        result["error"] = str(exc)
        return result

    # Exakte Schutzpfade sind Teil der Quote. Zuvor vollständig verifizierte
    # Familien-Nachsicherungen sind bekannte Hilfseinträge, aber keine eigenen
    # Systembackup-Kandidaten.
    direct_protected_paths = {
        path
        for path in preserved_paths
        if path.parent == root
        and path not in recognized_paths
    }
    verified_protected_paths: Set[Path] = set()
    candidates: List[Tuple[float, str, Path, int, int, int, int, bool]] = []
    try:
        resume_receipts, invalid_resume_receipts, resume_stages = (
            _load_prune_resume_receipts(
                root,
                root_descriptor,
                expected_kind=expected_kind,
            )
        )
        for quarantine_name, detail in sorted(invalid_resume_receipts.items()):
            record_unclassified(
                root / _prune_resume_receipt_name(quarantine_name),
                "resume_receipt_invalid",
                detail,
            )
        for stage_name in resume_stages:
            try:
                if not dry_run:
                    _assert_no_update_state_blockers()
                    _discard_secure_prune_resume_stage(root_descriptor, stage_name)
                result.setdefault("maintenance_removed", []).append(
                    {
                        "path": str(root / stage_name),
                        "reason": "resume_stage_residue",
                        "dry_run": bool(dry_run),
                    }
                )
            except Exception as exc:
                record_unclassified(
                    root / stage_name,
                    "resume_stage_invalid",
                    str(exc),
                )
        for quarantine_name, receipt in sorted(resume_receipts.items()):
            try:
                reconciled = _reconcile_absent_prune_resume(
                    root,
                    root_descriptor,
                    receipt,
                    dry_run=dry_run,
                )
            except Exception as exc:
                record_unclassified(
                    root / receipt.receipt_name,
                    "resume_receipt_conflict",
                    str(exc),
                )
            else:
                if reconciled:
                    result.setdefault("maintenance_removed", []).append(
                        {
                            "path": str(root / receipt.receipt_name),
                            "reason": "resume_receipt_reconciled",
                            "dry_run": bool(dry_run),
                        }
                    )
        for name in sorted(os.listdir(root_descriptor)):
            candidate = root / name
            if (
                _PRUNE_RESUME_RECEIPT_NAME_RE.fullmatch(name) is not None
                or _PRUNE_RESUME_STAGE_NAME_RE.fullmatch(name) is not None
            ):
                continue
            if name == ROOT_MARKER_NAME:
                try:
                    metadata = os.stat(
                        name,
                        dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(
                        metadata.st_mode
                    ):
                        raise BackupIntegrityError(
                            "Backup-Root-Marker ist keine echte reguläre Datei"
                        )
                    marker_contract = _bind_removal_tree(
                        root_descriptor,
                        name,
                        expected_dev=metadata.st_dev,
                        expected_ino=metadata.st_ino,
                    )
                    if marker_contract.is_directory:
                        raise BackupIntegrityError(
                            "Backup-Root-Marker ist kein Dateieintrag"
                        )
                except Exception as exc:
                    record_unclassified(
                        candidate,
                        "root_marker_invalid",
                        str(exc),
                    )
                else:
                    result["skipped"].append(
                        {"path": str(candidate), "reason": "geschützt"}
                    )
                continue
            if (
                expected_kind == SYSTEM_BACKUP_KIND
                and name == "web_installer"
            ):
                try:
                    _bind_directory_mount_contract(root_descriptor, name)
                except Exception as exc:
                    record_unclassified(
                        candidate,
                        "web_collection_invalid",
                        str(exc),
                    )
                else:
                    result["skipped"].append(
                        {"path": str(candidate), "reason": "Web-Sammlung"}
                    )
                continue
            protected = candidate in direct_protected_paths or name in preserved_names
            if (
                name.startswith(".e3dc-prune-")
                and _PRUNE_QUARANTINE_NAME_RE.fullmatch(name) is None
            ):
                record_unclassified(candidate, "quarantine_name_invalid")
                continue
            if _PRUNE_QUARANTINE_NAME_RE.fullmatch(name) is not None:
                if name in invalid_resume_receipts:
                    continue
                resume = resume_receipts.get(name)
                if resume is not None and candidate not in recognized_paths and not protected:
                    try:
                        if _bind_remaining_resume_tree(
                            root_descriptor,
                            resume,
                        ) is None:
                            raise BackupIntegrityError(
                                "Quarantäne fehlt zum Löschfortsetzungs-Receipt"
                            )
                        if not dry_run:
                            _assert_no_update_state_blockers()
                            _continue_prune_resume(
                                root,
                                root_descriptor,
                                resume,
                                mutation_state=_RemovalMutationState(),
                            )
                        result.setdefault("maintenance_removed", []).append(
                            {
                                "path": str(candidate),
                                "reason": "resumed_partial_quarantine",
                                "dry_run": bool(dry_run),
                            }
                        )
                    except Exception as exc:
                        result["success"] = False
                        record_unclassified(
                            candidate,
                            "resume_delete_failed",
                            str(exc),
                        )
                    continue
                if resume is None and candidate not in recognized_paths and not protected:
                    try:
                        quarantine = _generic_verified_quarantine_contract(
                            root,
                            root_descriptor,
                            name,
                            expected_kind=expected_kind,
                        )
                        if not dry_run:
                            _assert_no_update_state_blockers()
                            _delete_existing_quarantine_with_resume(
                                root,
                                root_descriptor,
                                quarantine,
                            )
                        result.setdefault("maintenance_removed", []).append(
                            {
                                "path": str(candidate),
                                "reason": "verified_quarantine_residue",
                                "dry_run": bool(dry_run),
                            }
                        )
                    except Exception as exc:
                        record_unclassified(
                            candidate,
                            "quarantine_residue",
                            str(exc),
                        )
                    continue
            if candidate in recognized_paths:
                try:
                    metadata = os.stat(
                        name,
                        dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                    mount_contract = _bind_directory_mount_contract(
                        root_descriptor,
                        name,
                        expected_dev=metadata.st_dev,
                        expected_ino=metadata.st_ino,
                    )
                    if (
                        _quiesced_name_parts(name) is None
                        and _PRUNE_QUARANTINE_NAME_RE.fullmatch(name) is None
                    ):
                        raise BackupIntegrityError(
                            "Geschützter Hilfseintrag besitzt keinen bekannten Namen"
                        )
                    verify_backup(
                        candidate,
                        expected_kind=QUIESCED_OVERLAY_KIND,
                    )
                    _bind_directory_mount_contract(
                        root_descriptor,
                        name,
                        expected_dev=mount_contract.dev,
                        expected_ino=mount_contract.ino,
                        expected_parent_mount_id=mount_contract.parent_mount_id,
                        expected_entry_mount_id=mount_contract.entry_mount_id,
                    )
                except Exception as exc:
                    record_unclassified(
                        candidate,
                        "protected_auxiliary_invalid",
                        str(exc),
                    )
                else:
                    verified_protected_paths.add(candidate)
                    result["skipped"].append(
                        {
                            "path": str(candidate),
                            "reason": "geschützte Familien-Nachsicherung",
                        }
                    )
                continue
            try:
                metadata = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise BackupIntegrityError("Symlink")
                if not stat.S_ISDIR(metadata.st_mode):
                    raise BackupIntegrityError("kein Backup-Verzeichnis")
                mount_contract = _bind_directory_mount_contract(
                    root_descriptor,
                    name,
                    expected_dev=metadata.st_dev,
                    expected_ino=metadata.st_ino,
                )
                verify_backup(candidate, expected_kind=expected_kind)
                verified_metadata = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
                if (metadata.st_dev, metadata.st_ino) != (verified_metadata.st_dev, verified_metadata.st_ino):
                    raise BackupIntegrityError("Backup wurde während der Verifikation ausgetauscht.")
                _bind_directory_mount_contract(
                    root_descriptor,
                    name,
                    expected_dev=mount_contract.dev,
                    expected_ino=mount_contract.ino,
                    expected_parent_mount_id=mount_contract.parent_mount_id,
                    expected_entry_mount_id=mount_contract.entry_mount_id,
                )
                candidates.append(
                    (
                        metadata.st_mtime,
                        name,
                        candidate,
                        metadata.st_dev,
                        metadata.st_ino,
                        mount_contract.parent_mount_id,
                        mount_contract.entry_mount_id,
                        protected,
                    )
                )
                if protected:
                    verified_protected_paths.add(candidate)
            except Exception as exc:
                record_unclassified(candidate, "nicht verifiziert", str(exc))

        missing_protected_paths = sorted(
            (direct_protected_paths | recognized_paths)
            - verified_protected_paths,
            key=str,
        )
        if missing_protected_paths:
            message = (
                "Mindestens ein geschütztes Backup konnte nicht erneut verifiziert werden"
            )
            result.update(
                {
                    "success": False,
                    "blocked": True,
                    "blocker": message,
                    "verified_count_before": len(candidates),
                    "verified_count_after": len(candidates),
                    "kept": [str(item[2]) for item in candidates],
                }
            )
            for path in missing_protected_paths:
                if not any(
                    entry.get("path") == str(path)
                    for entry in result["skipped"]
                ):
                    result["skipped"].append(
                        {"path": str(path), "reason": "geschütztes Backup fehlt"}
                    )
            _log(logger, "warning", "Backup-Retention blockiert: " + message)
            return result

        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        protected_candidates = [item for item in candidates if item[7]]
        limit_deferred_reason = ""
        if len(protected_candidates) > keep_count:
            limit_deferred_reason = (
                "{} geschützte Backup-Familien überschreiten das Limit von {}"
            ).format(len(protected_candidates), keep_count)
            result["limit_deferred"] = True
            result["limit_deferred_reason"] = limit_deferred_reason
            result["skipped"].append(
                {"path": str(root), "reason": "protected_limit_exceeded"}
            )
            _log(
                logger,
                "warning",
                "Backup-Limit wird nach Ende der Schutzbindung erneut angewendet: "
                + limit_deferred_reason,
            )

        protected_keep = {item[2] for item in protected_candidates}
        unprotected_candidates = [item for item in candidates if not item[7]]
        count_slots = max(0, keep_count - len(protected_keep))
        minimum_slots = max(0, min_keep_count - len(protected_keep))
        count_keep = protected_keep | {
            item[2] for item in unprotected_candidates[:count_slots]
        }
        min_keep = protected_keep | {
            item[2] for item in unprotected_candidates[:minimum_slots]
        }
        cutoff = None
        if max_age_days:
            cutoff = now_ts - max_age_days * 86400

        for (
            mtime,
            name,
            path,
            verified_dev,
            verified_ino,
            root_mount_id,
            entry_mount_id,
            protected,
        ) in candidates:
            if protected:
                result["kept"].append(str(path))
                continue
            too_many = path not in count_keep
            too_old = cutoff is not None and mtime < cutoff and path not in min_keep
            if not too_many and not too_old:
                result["kept"].append(str(path))
                continue
            reason = "max_count" if too_many else "max_age"
            if dry_run:
                result["removed"].append({"path": str(path), "reason": reason, "dry_run": True})
                continue
            quarantine = ".e3dc-prune-{}".format(uuid.uuid4().hex)
            try:
                _assert_no_update_state_blockers()
                _bind_directory_mount_contract(
                    root_descriptor,
                    name,
                    expected_dev=verified_dev,
                    expected_ino=verified_ino,
                    expected_parent_mount_id=root_mount_id,
                    expected_entry_mount_id=entry_mount_id,
                )
                rebound_manifest = verify_backup(path, expected_kind=expected_kind)
                rebound_manifest_sha256 = verified_manifest_sha256(
                    path,
                    expected_kind=expected_kind,
                )
                _delete_via_resumable_quarantine(
                    root,
                    root_descriptor,
                    name,
                    quarantine,
                    expected_dev=verified_dev,
                    expected_ino=verified_ino,
                    expected_parent_mount_id=root_mount_id,
                    expected_entry_mount_id=entry_mount_id,
                    kind=expected_kind,
                    install_root=str(rebound_manifest.get("install_root") or ""),
                    manifest_sha256=rebound_manifest_sha256,
                )
                result["removed"].append({"path": str(path), "reason": reason})
                _log(logger, "info", "Backup-Retention entfernt verifiziertes Backup: {}".format(path))
            except Exception as exc:
                result["success"] = False
                result["skipped"].append({"path": str(path), "reason": "Löschen fehlgeschlagen: {}".format(exc)})
        result["verified_count_before"] = len(candidates)
        clean_inventory = not result["unclassified"] and result["success"]
        if dry_run:
            result["verified_count_after"] = len(candidates)
            result["limit_satisfied"] = (
                len(candidates) <= keep_count and clean_inventory
            )
            result["projected_limit_satisfied"] = (
                len(candidates) - len(result["removed"]) <= keep_count
                and clean_inventory
            )
        else:
            remaining_count = len(candidates) - len(result["removed"])
            result["verified_count_after"] = remaining_count
            result["limit_satisfied"] = (
                remaining_count <= keep_count and clean_inventory
            )
    finally:
        os.close(root_descriptor)
    return result


def _delete_verified_backup_locked(
    backup_path: PathValue,
    collection_root: PathValue,
    expected_kind: str = SYSTEM_BACKUP_KIND,
    *,
    expected_manifest_sha256: Optional[str] = None,
    expected_dev: Optional[int] = None,
    expected_ino: Optional[int] = None,
) -> None:
    """Löscht unter bereits gebundenem Lock ein verifiziertes direktes Kind."""

    _require_bound_update_lock()
    root = _assert_no_symlink_components(collection_root)
    target = _assert_no_symlink_components(backup_path)
    if target.parent != root or target.name.startswith(".e3dc-prune-"):
        raise BackupIntegrityError("Backup liegt nicht direkt im freigegebenen Collection-Root.")
    root_descriptor = os.open(
        str(root),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    quarantine = ".e3dc-prune-{}".format(uuid.uuid4().hex)
    try:
        before = os.stat(target.name, dir_fd=root_descriptor, follow_symlinks=False)
        if expected_dev is not None and (
            before.st_dev,
            before.st_ino,
        ) != (expected_dev, expected_ino):
            raise BackupIntegrityError("Backup-Inode widerspricht dem Löschvertrag.")
        manifest = verify_backup(
            target,
            expected_kind=expected_kind,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        bound_manifest_sha256 = expected_manifest_sha256 or verified_manifest_sha256(
            target,
            expected_kind=expected_kind,
        )
        after = os.stat(target.name, dir_fd=root_descriptor, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise BackupIntegrityError("Backup wurde während der Verifikation ausgetauscht.")
        mount_contract = _bind_directory_mount_contract(
            root_descriptor,
            target.name,
            expected_dev=before.st_dev,
            expected_ino=before.st_ino,
        )
        _assert_no_update_state_blockers()
        _delete_via_resumable_quarantine(
            root,
            root_descriptor,
            target.name,
            quarantine,
            expected_dev=int(before.st_dev),
            expected_ino=int(before.st_ino),
            expected_parent_mount_id=mount_contract.parent_mount_id,
            expected_entry_mount_id=mount_contract.entry_mount_id,
            kind=expected_kind,
            install_root=str(manifest.get("install_root") or ""),
            manifest_sha256=str(bound_manifest_sha256),
        )
    finally:
        os.close(root_descriptor)


def delete_verified_backup(
    backup_path: PathValue,
    collection_root: PathValue,
    expected_kind: str = SYSTEM_BACKUP_KIND,
    *,
    expected_manifest_sha256: Optional[str] = None,
    expected_dev: Optional[int] = None,
    expected_ino: Optional[int] = None,
) -> None:
    """Löscht ein verifiziertes direktes Kind über eine atomare Quarantäne.

    Der öffentliche Altvertrag bleibt unverändert aufrufbar, bindet seine
    Mutation nun aber selbst an denselben Update-Lock wie Familienlöschungen.
    Ein bereits vom Familiencaller geerbter Lock wird reentrant weiterverwendet.
    """

    with backup_maintenance_lock(require_no_update_state=True):
        _delete_verified_backup_locked(
            backup_path,
            collection_root,
            expected_kind=expected_kind,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_dev=expected_dev,
            expected_ino=expected_ino,
        )


def _verify_quarantine_or_resume_contract(
    root: Path,
    root_descriptor: int,
    quarantine_name: str,
    install: Path,
) -> Union[_PruneQuarantineContract, _PruneResumeContract]:
    receipt_name = _prune_resume_receipt_name(quarantine_name)
    try:
        os.stat(receipt_name, dir_fd=root_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return _verify_prune_quarantine_contract(
            root,
            root_descriptor,
            quarantine_name,
            install,
        )
    receipt = _read_prune_resume_contract(
        root,
        root_descriptor,
        receipt_name,
        expected_install_root=str(install),
    )
    if _bind_remaining_resume_tree(root_descriptor, receipt) is None:
        raise BackupIntegrityError("Geschützte Quarantäne fehlt zum Receipt")
    return receipt


def _resolve_preserved_backup_families(
    install: Path,
    root: Path,
    preserved_paths: Iterable[Path],
) -> Tuple[Set[Path], Set[Path]]:
    """Erweitert geschützte Nachsicherungen um ihr eindeutiges Parent-Backup."""

    direct_paths = {
        path for path in preserved_paths if path.parent == root
    }
    out_of_scope = sorted(
        {path for path in preserved_paths if path.parent != root},
        key=str,
    )
    if out_of_scope:
        raise BackupIntegrityError(
            "Schutzpfad liegt nicht direkt im gebundenen Backup-Root: {}".format(
                ", ".join(str(path) for path in out_of_scope)
            )
        )
    expanded: Set[Path] = set()
    recognized_auxiliary: Set[Path] = set()
    if not direct_paths:
        return expanded, recognized_auxiliary
    root_descriptor = os.open(
        str(root),
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        by_name, by_id = _system_backup_index(root, root_descriptor, install)
        for path in sorted(direct_paths, key=str):
            if _PRUNE_QUARANTINE_NAME_RE.fullmatch(path.name) is not None:
                quarantine = _verify_quarantine_or_resume_contract(
                    root,
                    root_descriptor,
                    path.name,
                    install,
                )
                if quarantine.kind == QUIESCED_OVERLAY_KIND:
                    parents = by_id.get(quarantine.parent_backup_id, ())
                    if len(parents) != 1:
                        raise BackupIntegrityError(
                            "Geschützte Quarantäne-Nachsicherung besitzt kein "
                            "eindeutiges Parent-Backup"
                        )
                    expanded.add(parents[0].path)
                    recognized_auxiliary.add(path)
                continue
            if path.name.startswith(".e3dc-prune-"):
                raise BackupIntegrityError(
                    "Geschützter Quarantänepfad besitzt keinen kanonischen Namen"
                )
            parsed = _quiesced_name_parts(path.name)
            if parsed is None:
                continue
            parent_name, _transaction_id = parsed
            overlay = _verify_quiesced_overlay_contract(
                root,
                root_descriptor,
                path.name,
                install,
            )
            parent = by_name.get(parent_name)
            if (
                parent is None
                or parent.backup_id != overlay.parent_backup_id
                or len(by_id.get(parent.backup_id, ())) != 1
            ):
                raise BackupIntegrityError(
                    "Geschützte Daten-Nachsicherung besitzt kein eindeutiges "
                    "Parent-Backup"
                )
            expanded.add(parent.path)
            recognized_auxiliary.add(path)

        protected_parent_paths = direct_paths | expanded
        protected_parent_ids = {
            contract.backup_id
            for contract in by_name.values()
            if contract.path in protected_parent_paths
        }
        for name in sorted(os.listdir(root_descriptor)):
            path = root / name
            if path in recognized_auxiliary:
                continue
            if _PRUNE_QUARANTINE_NAME_RE.fullmatch(name) is not None:
                try:
                    quarantine = _verify_quarantine_or_resume_contract(
                        root,
                        root_descriptor,
                        name,
                        install,
                    )
                except Exception:
                    continue
                parents = by_id.get(quarantine.parent_backup_id, ())
                if (
                    quarantine.kind == QUIESCED_OVERLAY_KIND
                    and quarantine.parent_backup_id in protected_parent_ids
                    and len(parents) == 1
                    and parents[0].path in protected_parent_paths
                ):
                    recognized_auxiliary.add(path)
                continue
            if _quiesced_name_parts(name) is None:
                continue
            try:
                overlay = _verify_quiesced_overlay_contract(
                    root,
                    root_descriptor,
                    name,
                    install,
                )
            except Exception:
                continue
            parent = by_name.get(overlay.parent_name)
            if (
                parent is not None
                and parent.path in protected_parent_paths
                and parent.backup_id == overlay.parent_backup_id
                and len(by_id.get(parent.backup_id, ())) == 1
            ):
                recognized_auxiliary.add(path)
    finally:
        os.close(root_descriptor)
    return expanded, recognized_auxiliary


def _verify_preserved_backup_paths(
    install: Path,
    root: Path,
    preserved_paths: Iterable[Path],
) -> None:
    """Verifiziert alle direkt geschützten Familien vor der ersten Mutation."""

    out_of_scope = sorted(
        {path for path in preserved_paths if path.parent != root},
        key=str,
    )
    if out_of_scope:
        raise BackupIntegrityError(
            "Schutzpfad liegt nicht direkt im gebundenen Backup-Root: {}".format(
                ", ".join(str(path) for path in out_of_scope)
            )
        )
    direct_paths = sorted(
        {path for path in preserved_paths if path.parent == root},
        key=str,
    )
    if not direct_paths:
        return
    root_descriptor = os.open(
        str(root),
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        by_name, by_id = _system_backup_index(root, root_descriptor, install)
        for path in direct_paths:
            if _PRUNE_QUARANTINE_NAME_RE.fullmatch(path.name) is not None:
                quarantine = _verify_quarantine_or_resume_contract(
                    root,
                    root_descriptor,
                    path.name,
                    install,
                )
                if quarantine.kind == QUIESCED_OVERLAY_KIND:
                    parents = by_id.get(quarantine.parent_backup_id, ())
                    if (
                        len(parents) != 1
                        or parents[0].path not in direct_paths
                    ):
                        raise BackupIntegrityError(
                            "Geschützte Quarantäne-Nachsicherung widerspricht "
                            "der Parent-Schutzbindung"
                        )
                continue
            if path.name.startswith(".e3dc-prune-"):
                raise BackupIntegrityError(
                    "Geschützter Quarantänepfad besitzt keinen kanonischen Namen"
                )
            parsed = _quiesced_name_parts(path.name)
            if parsed is None:
                _verify_system_backup_contract(
                    root,
                    root_descriptor,
                    path.name,
                    install,
                )
                continue

            parent_name, _transaction_id = parsed
            overlay = _verify_quiesced_overlay_contract(
                root,
                root_descriptor,
                path.name,
                install,
            )
            parent = by_name.get(parent_name)
            if parent is None:
                raise BackupIntegrityError(
                    "Geschützte Daten-Nachsicherung besitzt kein verifiziertes Parent-Backup"
                )
            if (
                parent.backup_id != overlay.parent_backup_id
                or len(by_id.get(parent.backup_id, ())) != 1
                or parent.path not in direct_paths
            ):
                raise BackupIntegrityError(
                    "Geschützte Daten-Nachsicherung widerspricht der Parent-Bindung"
                )
            _overlay_restore_guard(
                root,
                root_descriptor,
                overlay,
                parent,
            )
    finally:
        os.close(root_descriptor)


def _prune_quiesced_overlays_locked(
    install: Path,
    root: Path,
    *,
    logger: Any = None,
    dry_run: bool = False,
    preserve_paths: Optional[Iterable[PathValue]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": True,
        "blocked": False,
        "blocker": "",
        "root": str(root),
        "removed": [],
        "kept": [],
        "skipped": [],
        "dry_run": bool(dry_run),
    }
    preserved = {_lexical_absolute(path) for path in (preserve_paths or ())}
    root_descriptor = os.open(
        str(root),
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        root_metadata = os.fstat(root_descriptor)
        resume_receipts, invalid_resume_receipts, resume_stages = (
            _load_prune_resume_receipts(
                root,
                root_descriptor,
                expected_install_root=str(install),
            )
        )
        for quarantine_name, detail in sorted(invalid_resume_receipts.items()):
            result["skipped"].append(
                {
                    "path": str(root / _prune_resume_receipt_name(quarantine_name)),
                    "reason": "resume_receipt_invalid",
                    "detail": detail,
                }
            )
        for stage_name in resume_stages:
            try:
                if not dry_run:
                    _assert_no_update_state_blockers()
                    _discard_secure_prune_resume_stage(root_descriptor, stage_name)
                result["removed"].append(
                    {
                        "path": str(root / stage_name),
                        "reason": "resume_stage_residue",
                        "dry_run": bool(dry_run),
                    }
                )
            except Exception as exc:
                result["skipped"].append(
                    {
                        "path": str(root / stage_name),
                        "reason": "resume_stage_invalid",
                        "detail": str(exc),
                    }
                )
        for quarantine_name, receipt in sorted(resume_receipts.items()):
            try:
                reconciled = _reconcile_absent_prune_resume(
                    root,
                    root_descriptor,
                    receipt,
                    dry_run=dry_run,
                )
            except Exception as exc:
                result["skipped"].append(
                    {
                        "path": str(root / receipt.receipt_name),
                        "reason": "resume_receipt_conflict",
                        "detail": str(exc),
                    }
                )
            else:
                if reconciled:
                    result["removed"].append(
                        {
                            "path": str(root / receipt.receipt_name),
                            "reason": "resume_receipt_reconciled",
                            "dry_run": bool(dry_run),
                        }
                    )
        by_name, by_id = _system_backup_index(root, root_descriptor, install)
        for name in sorted(os.listdir(root_descriptor)):
            path = root / name
            if (
                _PRUNE_RESUME_RECEIPT_NAME_RE.fullmatch(name) is not None
                or _PRUNE_RESUME_STAGE_NAME_RE.fullmatch(name) is not None
            ):
                continue
            if name.startswith(".e3dc-prune-"):
                if name in invalid_resume_receipts:
                    continue
                resume = resume_receipts.get(name)
                if path in preserved:
                    try:
                        if resume is not None:
                            if _bind_remaining_resume_tree(
                                root_descriptor,
                                resume,
                            ) is None:
                                raise BackupIntegrityError(
                                    "Geschützte Quarantäne fehlt zum Receipt"
                                )
                            kept_kind = resume.kind
                        else:
                            quarantine = _verify_prune_quarantine_contract(
                                root,
                                root_descriptor,
                                name,
                                install,
                            )
                            kept_kind = quarantine.kind
                    except Exception as exc:
                        result["skipped"].append(
                            {
                                "path": str(path),
                                "reason": "protected_quarantine_invalid",
                                "detail": str(exc),
                            }
                        )
                    else:
                        result["kept"].append(
                            {
                                "path": str(path),
                                "reason": "protected",
                                "kind": kept_kind,
                            }
                        )
                    continue
                if resume is None:
                    try:
                        quarantine = _verify_prune_quarantine_contract(
                            root,
                            root_descriptor,
                            name,
                            install,
                        )
                    except Exception as exc:
                        result["skipped"].append(
                            {
                                "path": str(path),
                                "reason": "quarantine_residue",
                                "detail": str(exc),
                            }
                        )
                        continue
                    quarantine_kind = quarantine.kind
                    quarantine_parent_backup_id = quarantine.parent_backup_id
                else:
                    try:
                        if _bind_remaining_resume_tree(
                            root_descriptor,
                            resume,
                        ) is None:
                            raise BackupIntegrityError(
                                "Quarantäne fehlt zum Löschfortsetzungs-Receipt"
                            )
                    except Exception as exc:
                        result["skipped"].append(
                            {
                                "path": str(path),
                                "reason": "resume_tree_invalid",
                                "detail": str(exc),
                            }
                        )
                        continue
                    quarantine_kind = resume.kind
                    quarantine_parent_backup_id = resume.parent_backup_id
                if (
                    quarantine_kind == QUIESCED_OVERLAY_KIND
                    and any(
                        parent.path in preserved
                        for parent in by_id.get(
                            quarantine_parent_backup_id,
                            (),
                        )
                    )
                ):
                    result["kept"].append(
                        {
                            "path": str(path),
                            "reason": "protected_parent_family",
                            "kind": quarantine_kind,
                        }
                    )
                    continue
                if dry_run:
                    result["removed"].append(
                        {
                            "path": str(path),
                            "reason": (
                                "resumable_quarantine_residue"
                                if resume is not None
                                else "verified_quarantine_residue"
                            ),
                            "kind": quarantine_kind,
                            "dry_run": True,
                        }
                    )
                    continue
                try:
                    _assert_no_update_state_blockers()
                    if resume is not None:
                        _continue_prune_resume(
                            root,
                            root_descriptor,
                            resume,
                            mutation_state=_RemovalMutationState(),
                        )
                        removed_kind = resume.kind
                        removed_reason = "resumed_partial_quarantine"
                    else:
                        rebound = _verify_prune_quarantine_contract(
                            root,
                            root_descriptor,
                            name,
                            install,
                            expected=quarantine,
                        )
                        _delete_existing_quarantine_with_resume(
                            root,
                            root_descriptor,
                            rebound,
                        )
                        removed_kind = rebound.kind
                        removed_reason = "verified_quarantine_residue"
                    result["removed"].append(
                        {
                            "path": str(path),
                            "reason": removed_reason,
                            "kind": removed_kind,
                        }
                    )
                    _log(
                        logger,
                        "info",
                        "Backup-Retention schließt verifizierten Quarantäne-Rest ab: {}".format(
                            path
                        ),
                    )
                except BackupMaintenanceBusy as exc:
                    result["success"] = False
                    result["blocked"] = True
                    result["blocker"] = str(exc)
                    result["skipped"].append(
                        {
                            "path": str(path),
                            "reason": "update_receipt_present",
                            "detail": str(exc),
                        }
                    )
                    break
                except Exception as exc:
                    result["success"] = False
                    result["skipped"].append(
                        {
                            "path": str(path),
                            "reason": "quarantine_delete_failed",
                            "detail": str(exc),
                        }
                    )
                continue
            parsed = _quiesced_name_parts(name)
            looks_like_overlay = ".quiesced-" in name
            if parsed is None:
                if looks_like_overlay:
                    result["skipped"].append(
                        {"path": str(path), "reason": "transaction_name_mismatch"}
                    )
                continue
            parent_name, _transaction_id = parsed
            parent_path = root / parent_name
            protected = path in preserved or parent_path in preserved
            try:
                overlay = _verify_quiesced_overlay_contract(
                    root,
                    root_descriptor,
                    name,
                    install,
                )
            except Exception as exc:
                result["skipped"].append(
                    {
                        "path": str(path),
                        "reason": "manifest_invalid",
                        "detail": str(exc),
                    }
                )
                continue

            parent = by_name.get(parent_name)
            guard: Optional[QuiescedOverlayRestoreGuard] = None
            if parent is not None:
                if parent.backup_id != overlay.parent_backup_id:
                    result["skipped"].append(
                        {"path": str(path), "reason": "parent_id_mismatch"}
                    )
                    continue
                if len(by_id.get(parent.backup_id, ())) != 1:
                    result["skipped"].append(
                        {"path": str(path), "reason": "parent_id_duplicate"}
                    )
                    continue
                guard = _overlay_restore_guard(
                    root,
                    root_descriptor,
                    overlay,
                    parent,
                )
                reason = "inactive_valid_overlay"
            else:
                try:
                    os.stat(
                        parent_name,
                        dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    if overlay.parent_backup_id in by_id:
                        result["skipped"].append(
                            {"path": str(path), "reason": "parent_id_mismatch"}
                        )
                        continue
                    reason = "orphan_parent_missing"
                except Exception as exc:
                    result["skipped"].append(
                        {
                            "path": str(path),
                            "reason": "parent_invalid",
                            "detail": str(exc),
                        }
                    )
                    continue
                else:
                    result["skipped"].append(
                        {"path": str(path), "reason": "parent_invalid"}
                    )
                    continue

            if protected:
                if parent is None:
                    result["skipped"].append(
                        {"path": str(path), "reason": "protected_parent_missing"}
                    )
                    continue
                result["kept"].append(
                    {"path": str(path), "reason": "protected"}
                )
                continue

            if dry_run:
                result["removed"].append(
                    {"path": str(path), "reason": reason, "dry_run": True}
                )
                continue
            try:
                _assert_no_update_state_blockers()
                if guard is not None:
                    delete_bound_quiesced_overlay(path, guard=guard)
                else:
                    _delete_orphan_quiesced_overlay(
                        root,
                        install,
                        overlay,
                        root_dev=int(root_metadata.st_dev),
                        root_ino=int(root_metadata.st_ino),
                    )
                result["removed"].append(
                    {"path": str(path), "reason": reason}
                )
                _log(
                    logger,
                    "info",
                    "Backup-Retention entfernt verifizierte ruhende Daten-Nachsicherung: {}".format(
                        path
                    ),
                )
            except BackupMaintenanceBusy as exc:
                result["success"] = False
                result["blocked"] = True
                result["blocker"] = str(exc)
                result["skipped"].append(
                    {
                        "path": str(path),
                        "reason": "update_receipt_present",
                        "detail": str(exc),
                    }
                )
                break
            except Exception as exc:
                result["success"] = False
                result["skipped"].append(
                    {
                        "path": str(path),
                        "reason": "delete_failed",
                        "detail": str(exc),
                    }
                )
    finally:
        os.close(root_descriptor)
    return result


def prune_backup_dir(
    backup_root: PathValue,
    keep_count: int,
    preserve_names: Optional[Iterable[str]] = None,
    logger: Any = None,
    dry_run: bool = False,
    max_age_days: Optional[int] = None,
    min_keep_count: int = 1,
    now: Optional[float] = None,
    expected_kind: str = SYSTEM_BACKUP_KIND,
    preserve_paths: Optional[Iterable[PathValue]] = None,
) -> Dict[str, Any]:
    """Gatet die Einzel-Retention gegen Update-Lock und Recovery-Belege."""

    try:
        with backup_maintenance_lock(require_no_update_state=False):
            blockers = _update_state_blockers()
            if blockers:
                message = (
                    "Ein Updateabschluss oder Recovery-Zustand ist noch offen: "
                    + ", ".join(blockers)
                )
                _log(logger, "warning", "Backup-Retention bleibt ohne Mutation: " + message)
                return _blocked_backup_prune_result(
                    backup_root,
                    keep_count=keep_count,
                    min_keep_count=min_keep_count,
                    max_age_days=max_age_days,
                    expected_kind=expected_kind,
                    blocker=message,
                    reason="update_receipt_present",
                    dry_run=dry_run,
                )
            return _prune_backup_dir_locked(
                backup_root,
                keep_count=keep_count,
                preserve_names=preserve_names,
                logger=logger,
                dry_run=dry_run,
                max_age_days=max_age_days,
                min_keep_count=min_keep_count,
                now=now,
                expected_kind=expected_kind,
                preserve_paths=preserve_paths,
            )
    except BackupMaintenanceBusy as exc:
        _log(logger, "warning", "Backup-Retention bleibt ohne Mutation: " + str(exc))
        return _blocked_backup_prune_result(
            backup_root,
            keep_count=keep_count,
            min_keep_count=min_keep_count,
            max_age_days=max_age_days,
            expected_kind=expected_kind,
            blocker=str(exc),
            reason=(
                "update_receipt_present"
                if _update_state_blockers()
                else "update_lock_active"
            ),
            dry_run=dry_run,
        )
    except (BackupIntegrityError, OSError) as exc:
        message = "Backup-Maintenance-Lock konnte nicht sicher gebunden werden: {}".format(
            exc
        )
        _log(logger, "error", message)
        return _blocked_backup_prune_result(
            backup_root,
            keep_count=keep_count,
            min_keep_count=min_keep_count,
            max_age_days=max_age_days,
            expected_kind=expected_kind,
            blocker=message,
            reason="maintenance_lock_invalid",
            dry_run=dry_run,
            success=False,
        )


def prune_quiesced_overlays(
    install_path: PathValue,
    backup_root: PathValue,
    *,
    logger: Any = None,
    dry_run: bool = False,
    preserve_paths: Optional[Iterable[PathValue]] = None,
) -> Dict[str, Any]:
    """Bereinigt nur inaktive, vollständig klassifizierte Transaktions-Overlays."""

    install = _lexical_absolute(install_path)
    root = validate_existing_backup_root(backup_root, install)
    try:
        with backup_maintenance_lock(require_no_update_state=True):
            return _prune_quiesced_overlays_locked(
                install,
                root,
                logger=logger,
                dry_run=dry_run,
                preserve_paths=preserve_paths,
            )
    except BackupMaintenanceBusy as exc:
        reason = (
            "update_receipt_present"
            if _update_state_blockers()
            else "update_lock_active"
        )
        return {
            "success": False,
            "blocked": True,
            "blocker": str(exc),
            "root": str(root),
            "removed": [],
            "kept": [],
            "skipped": [{"path": str(root), "reason": reason}],
            "dry_run": bool(dry_run),
        }


def delete_verified_backup_family(
    backup_path: PathValue,
    collection_root: PathValue,
    install_path: PathValue,
) -> Dict[str, Any]:
    """Löscht nach Gesamt-Preflight Overlays und zuletzt ihr Vollbackup."""

    install = _lexical_absolute(install_path)
    root = validate_existing_backup_root(collection_root, install)
    target = _lexical_absolute(backup_path)
    if target.parent != root:
        raise BackupIntegrityError(
            "Backup liegt nicht direkt im freigegebenen Collection-Root"
        )

    with backup_maintenance_lock(require_no_update_state=True):
        root_descriptor = os.open(
            str(root),
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        guards: List[Tuple[Path, QuiescedOverlayRestoreGuard]] = []
        try:
            by_name, by_id = _system_backup_index(root, root_descriptor, install)
            parent = by_name.get(target.name)
            if parent is None or parent.path != target:
                raise BackupIntegrityError(
                    "Ausgewähltes Vollbackup ist nicht eindeutig verifiziert"
                )
            if len(by_id.get(parent.backup_id, ())) != 1:
                raise BackupIntegrityError(
                    "Ausgewähltes Vollbackup besitzt keine eindeutige Backup-ID"
                )
            prefix = ".{}.quiesced-".format(parent.path.name)
            for name in sorted(os.listdir(root_descriptor)):
                if name.startswith(".e3dc-prune-"):
                    raise BackupIntegrityError(
                        "Backup-Sammlung enthält einen unbestätigten "
                        "Quarantäne-Rest: {}".format(name)
                    )
                selected_prefix = name.startswith(prefix)
                if not selected_prefix and _quiesced_name_parts(name) is None:
                    continue
                try:
                    overlay = _verify_quiesced_overlay_contract(
                        root,
                        root_descriptor,
                        name,
                        install,
                    )
                except Exception as exc:
                    if not selected_prefix:
                        continue
                    raise BackupIntegrityError(
                        "Backup-Familie enthält eine nicht verifizierbare ruhende "
                        "Daten-Nachsicherung: {} ({})".format(name, exc)
                    ) from exc
                if not selected_prefix:
                    if overlay.parent_backup_id == parent.backup_id:
                        raise BackupIntegrityError(
                            "Backup-Familie besitzt eine widersprüchliche "
                            "Parent-ID unter fremdem Namen: {}".format(name)
                        )
                    continue
                if (
                    overlay.parent_name != parent.path.name
                    or overlay.parent_backup_id != parent.backup_id
                ):
                    raise BackupIntegrityError(
                        "Backup-Familie enthält eine fremde Parent-Bindung: {}".format(
                            name
                        )
                    )
                guard = _overlay_restore_guard(
                    root,
                    root_descriptor,
                    overlay,
                    parent,
                )
                validate_quiesced_overlay_guard(guard)
                guards.append((overlay.path, guard))
        finally:
            os.close(root_descriptor)

        _assert_no_update_state_blockers()
        for overlay_path, guard in guards:
            delete_bound_quiesced_overlay(overlay_path, guard=guard)
        _assert_no_update_state_blockers()
        parent_manifest = verify_backup(
            target,
            expected_kind=SYSTEM_BACKUP_KIND,
            expected_manifest_sha256=parent.manifest_sha256,
        )
        if (
            _normalized_backup_id(parent_manifest.get("backup_id"))
            != parent.backup_id
            or str(parent_manifest.get("install_root") or "") != str(install)
        ):
            raise BackupIntegrityError(
                "Vollbackup driftete nach dem Overlay-Cleanup"
            )
        _delete_verified_backup_locked(
            target,
            root,
            expected_kind=SYSTEM_BACKUP_KIND,
            expected_manifest_sha256=parent.manifest_sha256,
            expected_dev=parent.dev,
            expected_ino=parent.ino,
        )
        return {
            "success": True,
            "backup": str(target),
            "removed_quiesced_overlays": len(guards),
        }


def prune_heavy_backup_payloads(
    backup_root: PathValue,
    logger: Any = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Veraltete wirkungslose Funktion: Manifestinhalte bleiben nach Abschluss unveränderlich."""

    return {
        "success": True,
        "root": str(_lexical_absolute(backup_root)),
        "removed": [],
        "skipped": [{"reason": "Manifest-Backups werden niemals nachträglich verändert."}],
        "dry_run": bool(dry_run),
    }


def prune_install_backups(
    install_path: PathValue,
    logger: Any = None,
    backup_root: Optional[PathValue] = None,
    preserve_paths: Optional[Iterable[PathValue]] = None,
    *,
    explicit_maintenance: bool = False,
) -> Dict[str, Any]:
    """Wendet die Aufbewahrung nur im validierten eigenen Sicherungsnamensraum an.

    Bei automatischer Ausführung bleibt ein frisch erstelltes Backup trotz
    offenem Recovery-Beleg gültig; die Retention wird dann als sicherer No-op
    gemeldet. Explizite Wartung meldet denselben Blocker als Fehlschlag.
    """

    install = _lexical_absolute(install_path)
    if backup_root is None:
        configured = os.environ.get("E3DC_BACKUP_ROOT", "").strip()
        root = _lexical_absolute(configured) if configured else install.parent / "e3dc-control-backups"
    else:
        root = _lexical_absolute(backup_root)
    root = validate_existing_backup_root(root, install)
    preserved_paths = {
        _lexical_absolute(path) for path in (preserve_paths or ())
    }
    try:
        with backup_maintenance_lock(require_no_update_state=False):
            blockers = _update_state_blockers()
            if blockers:
                message = (
                    "Ein Updateabschluss oder Recovery-Zustand ist noch offen: {}".format(
                        ", ".join(blockers)
                    )
                )
                blocked_result = {
                    "success": not explicit_maintenance,
                    "blocked": True,
                    "blocker": message,
                    "limit_satisfied": False,
                    "root": str(root),
                    "removed": [],
                    "kept": [],
                    "skipped": [
                        {"path": str(root), "reason": "update_receipt_present"}
                    ],
                    "dry_run": False,
                }
                _log(logger, "warning", "Backup-Retention bleibt ohne Mutation: " + message)
                return {
                    "success": not explicit_maintenance,
                    "blocked": True,
                    "blocker": message,
                    "limit_satisfied": False,
                    "payload_cleanup": prune_heavy_backup_payloads(root, logger=logger),
                    "quiesced_overlays": dict(blocked_result),
                    "update_backups": dict(blocked_result),
                    "web_installer_backups": dict(blocked_result),
                }

            recognized_auxiliary_paths: Set[Path] = set()
            try:
                expanded_paths, recognized_auxiliary_paths = (
                    _resolve_preserved_backup_families(
                        install,
                        root,
                        preserved_paths,
                    )
                )
                preserved_paths.update(expanded_paths)
                preserved_paths.update(recognized_auxiliary_paths)
                _verify_preserved_backup_paths(
                    install,
                    root,
                    preserved_paths,
                )
            except Exception as exc:
                message = (
                    "Geschützte Backup-Familie konnte vor der Bereinigung nicht "
                    "vollständig verifiziert werden: {}"
                ).format(exc)
                blocked_result = {
                    "success": False,
                    "blocked": True,
                    "blocker": message,
                    "limit_satisfied": False,
                    "root": str(root),
                    "removed": [],
                    "kept": [],
                    "skipped": [
                        {"path": str(root), "reason": "protected_preflight_invalid"}
                    ],
                    "dry_run": False,
                }
                _log(
                    logger,
                    "error",
                    "Backup-Retention bleibt ohne Mutation: " + message,
                )
                return {
                    "success": False,
                    "blocked": True,
                    "blocker": message,
                    "limit_satisfied": False,
                    "payload_cleanup": prune_heavy_backup_payloads(
                        root,
                        logger=logger,
                    ),
                    "quiesced_overlays": dict(blocked_result),
                    "update_backups": dict(blocked_result),
                    "web_installer_backups": dict(blocked_result),
                }

            quiesced_result = _prune_quiesced_overlays_locked(
                install,
                root,
                logger=logger,
                preserve_paths=preserved_paths,
            )
            if quiesced_result.get("blocked"):
                raise BackupMaintenanceBusy(
                    str(quiesced_result.get("blocker") or "Overlay-Cleanup blockiert")
                )
            _assert_no_update_state_blockers()
            if (
                not quiesced_result.get("success")
                or quiesced_result.get("skipped")
            ):
                update_result = {
                    "success": True,
                    "blocked": False,
                    "blocker": "",
                    "limit_satisfied": False,
                    "root": str(root),
                    "expected_kind": SYSTEM_BACKUP_KIND,
                    "keep_count": UPDATE_BACKUP_KEEP_COUNT,
                    "min_keep_count": UPDATE_BACKUP_MIN_KEEP_COUNT,
                    "max_age_days": UPDATE_BACKUP_MAX_AGE_DAYS,
                    "removed": [],
                    "kept": [],
                    "skipped": [
                        {
                            "path": str(root),
                            "reason": "overlay_preflight_incomplete",
                        }
                    ],
                    "dry_run": False,
                }
            else:
                update_result = _prune_backup_dir_locked(
                    root,
                    keep_count=UPDATE_BACKUP_KEEP_COUNT,
                    min_keep_count=UPDATE_BACKUP_MIN_KEEP_COUNT,
                    max_age_days=UPDATE_BACKUP_MAX_AGE_DAYS,
                    expected_kind=SYSTEM_BACKUP_KIND,
                    preserve_names={"web_installer"},
                    preserve_paths=preserved_paths,
                    recognized_paths=recognized_auxiliary_paths,
                    logger=logger,
                )
            if (
                quiesced_result.get("success")
                and update_result.get("success")
                and not update_result.get("blocked")
            ):
                web_result = _prune_backup_dir_locked(
                    root / "web_installer",
                    keep_count=WEB_INSTALLER_BACKUP_KEEP_COUNT,
                    min_keep_count=WEB_INSTALLER_BACKUP_MIN_KEEP_COUNT,
                    max_age_days=WEB_INSTALLER_BACKUP_MAX_AGE_DAYS,
                    expected_kind=WEB_SNAPSHOT_KIND,
                    logger=logger,
                )
            else:
                web_result = {
                    "success": True,
                    "blocked": False,
                    "blocker": "",
                    "limit_satisfied": False,
                    "root": str(root / "web_installer"),
                    "expected_kind": WEB_SNAPSHOT_KIND,
                    "removed": [],
                    "kept": [],
                    "skipped": [
                        {
                            "path": str(root / "web_installer"),
                            "reason": "system_retention_incomplete",
                        }
                    ],
                    "dry_run": False,
                }
            result_blocker = str(
                update_result.get("blocker")
                or web_result.get("blocker")
                or ""
            )
            result_blocked = bool(
                update_result.get("blocked") or web_result.get("blocked")
            )
            limit_satisfied = bool(
                not quiesced_result.get("skipped")
                and update_result.get("limit_satisfied")
                and web_result.get("limit_satisfied")
            )
            return {
                "success": bool(
                    quiesced_result.get("success")
                    and update_result.get("success")
                    and web_result.get("success")
                ),
                "blocked": result_blocked,
                "blocker": result_blocker,
                "limit_satisfied": limit_satisfied,
                "payload_cleanup": prune_heavy_backup_payloads(root, logger=logger),
                "quiesced_overlays": quiesced_result,
                "update_backups": update_result,
                "web_installer_backups": web_result,
            }
    except BackupMaintenanceBusy as exc:
        reason = (
            "update_receipt_present"
            if _update_state_blockers()
            else "update_lock_active"
        )
        automatic_noop = not explicit_maintenance
        blocked_result = {
            "success": automatic_noop,
            "blocked": True,
            "blocker": str(exc),
            "limit_satisfied": False,
            "root": str(root),
            "removed": [],
            "kept": [],
            "skipped": [{"path": str(root), "reason": reason}],
            "dry_run": False,
        }
        return {
            "success": automatic_noop,
            "blocked": True,
            "blocker": str(exc),
            "limit_satisfied": False,
            "payload_cleanup": prune_heavy_backup_payloads(root, logger=logger),
            "quiesced_overlays": blocked_result,
            "update_backups": dict(blocked_result),
            "web_installer_backups": dict(blocked_result),
        }
