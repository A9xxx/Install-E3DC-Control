#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bindet und härtet den persistenten Docker-Matter-Storage fail-closed."""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass, field
import errno
import grp
import json
import os
import pwd
import stat
import sys
import time


DEFAULT_STORAGE_PATH = "/var/www/html/data/matter-storage"
DEFAULT_RESET_REQUEST_PATH = "/var/www/html/data/matter_pairing_reset.request"
DEFAULT_RESET_REPAIR_REQUEST_PATH = "/var/www/html/data/matter_pairing_reset_repair.request"
DEFAULT_PAIRING_PATH = "/var/www/html/ramdisk/matter_pairing.json"
RESET_QUARANTINE_NAME = ".matter-storage-reset-quarantine"
RESET_QUARANTINE_PREPARE_NAME = ".matter-storage-reset-quarantine.prepare"
RESET_TRANSACTION_STAGE_PREFIX = ".matter-storage-reset-stage-"
RESET_TRANSACTION_MARKER_NAME = ".e3dc-matter-reset-transaction.json"
RESET_TRANSACTION_SCHEMA = "e3dc_matter_reset_transaction_v1"
RESET_TRANSACTION_MARKER_MAX_BYTES = 1024
DEFAULT_MAX_ENTRIES = 768
DEFAULT_MAX_DEPTH = 64
IDENTITY_SCHEMA = "e3dc-matter-storage-v1"
RESET_CAPABILITY = "e3dc-matter-pairing-reset-v2"
RESET_REQUEST_PAYLOAD = b"e3dc-matter-pairing-reset-v1\n"
RESET_REPAIR_REQUEST_PAYLOAD = b"e3dc-matter-pairing-reset-repair-v1\n"
RESET_TRANSACTION_SECONDS = 75.0


class MatterStorageContractError(RuntimeError):
    """Der persistente Matter-Baum erfüllt den privaten Storagevertrag nicht."""

    def __init__(self, message: str, code: str = "CONTRACT") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _BoundEntry:
    descriptor: int
    parent_index: int | None
    name: str
    relative_path: str
    is_directory: bool
    device: int
    inode: int
    mount_id: int
    depth: int
    children: dict[str, int] = field(default_factory=dict)
    child_names: tuple[str, ...] = ()
    repair_leaf: bool = False
    file_type: int = 0
    nlink: int = 1
    uid: int = 0
    gid: int = 0
    mode: int = 0


@dataclass
class _BoundRegular:
    parent_descriptor: int
    parent_device: int
    parent_inode: int
    parent_mount_id: int
    descriptor: int
    name: str
    device: int
    inode: int
    mount_id: int
    uid: int
    gid: int
    mode: int
    size: int
    payload: bytes | None


@dataclass
class _BoundNode:
    parent_descriptor: int
    parent_device: int
    parent_inode: int
    parent_mount_id: int
    descriptor: int
    name: str
    device: int
    inode: int
    mount_id: int
    file_type: int
    nlink: int
    uid: int
    gid: int
    mode: int
    size: int
    empty_directory: bool


def _required_open_flags() -> tuple[int, int, int]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory:
        raise MatterStorageContractError(
            "O_NOFOLLOW/O_DIRECTORY wird für den Matter-Storage benötigt"
        )
    return nofollow, directory, cloexec


def _path_open_flags() -> int:
    nofollow, _, cloexec = _required_open_flags()
    path_only = getattr(os, "O_PATH", 0)
    if not path_only:
        raise MatterStorageContractError(
            "O_PATH wird für die sichere Matter-Reparatur benötigt",
            "BINDING",
        )
    return path_only | nofollow | cloexec


def _mount_id(descriptor: int) -> int:
    try:
        with open(
            f"/proc/self/fdinfo/{int(descriptor)}",
            "r",
            encoding="ascii",
            errors="strict",
        ) as handle:
            for line in handle:
                key, separator, value = line.partition(":")
                if separator and key.strip() == "mnt_id":
                    parsed = int(value.strip(), 10)
                    if parsed <= 0:
                        break
                    return parsed
    except (OSError, UnicodeError, ValueError) as exc:
        raise MatterStorageContractError(
            "Mount-Identität des Matter-Storage ist nicht bindbar"
        ) from exc
    raise MatterStorageContractError(
        "Mount-Identität des Matter-Storage fehlt"
    )


def _open_absolute_directory(path: str) -> int:
    normalized = os.path.normpath(str(path))
    if (
        not normalized.startswith("/")
        or normalized != str(path)
        or normalized == "/"
    ):
        raise MatterStorageContractError(
            "Matter-Storage-Elternpfad ist nicht kanonisch absolut"
        )

    nofollow, directory, cloexec = _required_open_flags()
    flags = os.O_RDONLY | nofollow | directory | cloexec
    descriptor = os.open("/", flags)
    try:
        for component in normalized.split("/")[1:]:
            if not component or component in {".", ".."}:
                raise MatterStorageContractError(
                    "Matter-Storage-Elternpfad enthält eine ungültige Komponente"
                )
            named = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(named.st_mode) or not stat.S_ISDIR(named.st_mode):
                raise MatterStorageContractError(
                    "Matter-Storage-Elternkette enthält keinen sicheren Ordner"
                )
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            try:
                opened = os.fstat(next_descriptor)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (named.st_dev, named.st_ino)
                ):
                    raise MatterStorageContractError(
                        "Matter-Storage-Elternkette wechselte beim Öffnen"
                    )
            except Exception:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _resolve_uid(raw: str) -> int:
    token = str(raw).strip()
    try:
        account = pwd.getpwuid(int(token, 10)) if token.isdecimal() else pwd.getpwnam(token)
    except (KeyError, ValueError) as exc:
        raise MatterStorageContractError(
            "Matter-Storage-Benutzer ist nicht lokal gebunden"
        ) from exc
    if account.pw_uid <= 0:
        raise MatterStorageContractError(
            "Matter-Storage darf nicht root gehören"
        )
    return int(account.pw_uid)


def _resolve_gid(raw: str) -> int:
    token = str(raw).strip()
    try:
        group = grp.getgrgid(int(token, 10)) if token.isdecimal() else grp.getgrnam(token)
    except (KeyError, ValueError) as exc:
        raise MatterStorageContractError(
            "Matter-Storage-Gruppe ist nicht lokal gebunden"
        ) from exc
    if group.gr_gid <= 0:
        raise MatterStorageContractError(
            "Matter-Storage darf nicht der Root-Gruppe gehören"
        )
    return int(group.gr_gid)


def _identity_token(
    parent_metadata: os.stat_result,
    parent_mount_id: int,
    root_metadata: os.stat_result,
    root_mount_id: int,
) -> str:
    return ":".join(
        (
            IDENTITY_SCHEMA,
            str(int(parent_metadata.st_dev)),
            str(int(parent_metadata.st_ino)),
            str(int(parent_mount_id)),
            str(int(root_metadata.st_dev)),
            str(int(root_metadata.st_ino)),
            str(int(root_mount_id)),
        )
    )


def _validate_expected_identity(raw: str) -> str:
    token = str(raw or "").strip()
    parts = token.split(":")
    if len(parts) != 7 or parts[0] != IDENTITY_SCHEMA:
        raise MatterStorageContractError(
            "Gebundene Matter-Storage-Identität ist ungültig"
        )
    try:
        numeric = tuple(int(value, 10) for value in parts[1:])
    except ValueError as exc:
        raise MatterStorageContractError(
            "Gebundene Matter-Storage-Identität ist ungültig"
        ) from exc
    if any(value <= 0 for value in numeric):
        raise MatterStorageContractError(
            "Gebundene Matter-Storage-Identität ist ungültig"
        )
    return token


def _open_storage_root(
    parent_descriptor: int,
    root_name: str,
    *,
    create: bool,
) -> int:
    nofollow, directory, cloexec = _required_open_flags()
    flags = os.O_RDONLY | nofollow | directory | cloexec
    try:
        named = os.stat(
            root_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if not create:
            raise MatterStorageContractError(
                "Matter-Storage fehlt vor dem Workerstart"
            ) from None
        try:
            os.mkdir(root_name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        named = os.stat(
            root_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    if stat.S_ISLNK(named.st_mode) or not stat.S_ISDIR(named.st_mode):
        raise MatterStorageContractError(
            "Matter-Storage-Root ist kein sicheres Verzeichnis"
        )
    descriptor = os.open(root_name, flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise MatterStorageContractError(
                "Matter-Storage-Root wechselte beim Öffnen"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _bind_tree(
    root_descriptor: int,
    *,
    max_entries: int,
    max_depth: int,
    reset_mode: bool = False,
) -> list[_BoundEntry]:
    records: list[_BoundEntry] = []
    try:
        nofollow, directory, cloexec = _required_open_flags()
        root_metadata = os.fstat(root_descriptor)
        root_mount_id = _mount_id(root_descriptor)
        records.append(
            _BoundEntry(
                descriptor=root_descriptor,
                parent_index=None,
                name="matter-storage",
                relative_path=".",
                is_directory=True,
                device=int(root_metadata.st_dev),
                inode=int(root_metadata.st_ino),
                mount_id=root_mount_id,
                depth=0,
                repair_leaf=False,
                file_type=stat.S_IFDIR,
                nlink=int(root_metadata.st_nlink),
                uid=int(root_metadata.st_uid),
                gid=int(root_metadata.st_gid),
                mode=stat.S_IMODE(root_metadata.st_mode),
            )
        )
        seen_identities = {(int(root_metadata.st_dev), int(root_metadata.st_ino))}
        pending = [0]
        while pending:
            parent_index = pending.pop()
            parent = records[parent_index]
            try:
                names = tuple(sorted(os.listdir(parent.descriptor)))
            except OSError as exc:
                raise MatterStorageContractError(
                    f"Matter-Storage-Verzeichnis ist nicht lesbar: {parent.relative_path}"
                ) from exc
            parent.child_names = names
            for name in names:
                if len(records) >= max_entries:
                    raise MatterStorageContractError(
                        "Matter-Storage überschreitet das Eintragslimit",
                        "LIMIT",
                    )
                child_depth = parent.depth + 1
                if child_depth > max_depth:
                    raise MatterStorageContractError(
                        "Matter-Storage überschreitet das Tiefenlimit",
                        "LIMIT",
                    )
                try:
                    named = os.stat(
                        name,
                        dir_fd=parent.descriptor,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise MatterStorageContractError(
                        "Matter-Storage-Eintrag driftete beim Lesen"
                    ) from exc

                is_directory = stat.S_ISDIR(named.st_mode) and not stat.S_ISLNK(named.st_mode)
                is_regular = stat.S_ISREG(named.st_mode) and not stat.S_ISLNK(named.st_mode)
                repair_leaf = not is_directory and not is_regular
                if repair_leaf and not reset_mode:
                    raise MatterStorageContractError(
                        "Matter-Storage enthält Symlink oder Sonderdatei"
                    )
                if named.st_dev != root_metadata.st_dev:
                    raise MatterStorageContractError(
                        "Matter-Storage überschreitet eine Dateisystemgrenze"
                    )
                if is_regular and named.st_nlink != 1:
                    raise MatterStorageContractError(
                        "Matter-Storage enthält eine reguläre Datei mit mehreren Hardlinks",
                        "HARDLINK",
                    )
                if repair_leaf:
                    flags = _path_open_flags()
                else:
                    flags = os.O_RDONLY | nofollow | cloexec
                    if is_directory:
                        flags |= directory
                    else:
                        flags |= getattr(os, "O_NONBLOCK", 0)
                try:
                    descriptor = os.open(name, flags, dir_fd=parent.descriptor)
                except OSError as exc:
                    raise MatterStorageContractError(
                        "Matter-Storage-Eintrag konnte nicht sicher geöffnet werden"
                    ) from exc
                try:
                    opened = os.fstat(descriptor)
                    opened_is_directory = stat.S_ISDIR(opened.st_mode)
                    opened_is_regular = stat.S_ISREG(opened.st_mode)
                    if (
                        (is_directory and not opened_is_directory)
                        or (is_regular and not opened_is_regular)
                        or (opened.st_dev, opened.st_ino)
                        != (named.st_dev, named.st_ino)
                        or (is_regular and opened.st_nlink != 1)
                        or (repair_leaf and stat.S_IFMT(opened.st_mode)
                            != stat.S_IFMT(named.st_mode))
                    ):
                        raise MatterStorageContractError(
                            "Matter-Storage-Eintrag wechselte beim Öffnen"
                        )
                    mount_id = _mount_id(descriptor)
                    identity = (int(opened.st_dev), int(opened.st_ino))
                    if opened.st_dev != root_metadata.st_dev or mount_id != root_mount_id:
                        raise MatterStorageContractError(
                            "Matter-Storage überschreitet eine Dateisystem- oder Mountgrenze",
                            "FOREIGN_MOUNT",
                        )
                    if identity in seen_identities:
                        raise MatterStorageContractError(
                            "Matter-Storage enthält eine mehrfach erreichbare Identität"
                        )
                    seen_identities.add(identity)
                    relative_path = (
                        name
                        if parent.relative_path == "."
                        else f"{parent.relative_path}/{name}"
                    )
                    child_index = len(records)
                    records.append(
                        _BoundEntry(
                            descriptor=descriptor,
                            parent_index=parent_index,
                            name=name,
                            relative_path=relative_path,
                            is_directory=is_directory,
                            device=identity[0],
                            inode=identity[1],
                            mount_id=mount_id,
                            depth=child_depth,
                            repair_leaf=repair_leaf,
                            file_type=stat.S_IFMT(opened.st_mode),
                            nlink=int(opened.st_nlink),
                            uid=int(opened.st_uid),
                            gid=int(opened.st_gid),
                            mode=stat.S_IMODE(opened.st_mode),
                        )
                    )
                    descriptor = -1
                    parent.children[name] = child_index
                    if is_directory:
                        pending.append(child_index)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
        return records
    except Exception:
        if records:
            for record in reversed(records):
                os.close(record.descriptor)
        else:
            os.close(root_descriptor)
        raise


def _expected_mode(record: _BoundEntry) -> int:
    return 0o700 if record.is_directory else 0o600


def _harden_bound_tree(
    records: list[_BoundEntry],
    *,
    uid: int,
    gid: int,
) -> None:
    privileged = os.geteuid() == 0
    for record in records:
        before = os.fstat(record.descriptor)
        if privileged:
            os.fchown(record.descriptor, uid, gid)
        elif before.st_uid != uid or before.st_gid != gid:
            raise MatterStorageContractError(
                "Matter-Storage-Härtung benötigt root für den Eigentümerwechsel"
            )
        os.fchmod(record.descriptor, _expected_mode(record))


def _verify_bound_tree(
    records: list[_BoundEntry],
    *,
    uid: int,
    gid: int,
    reset_mode: bool = False,
) -> None:
    for record in records:
        opened = os.fstat(record.descriptor)
        identity_invalid = (
            (opened.st_dev, opened.st_ino) != (record.device, record.inode)
            or _mount_id(record.descriptor) != record.mount_id
        )
        if reset_mode:
            contract_invalid = (
                stat.S_IFMT(opened.st_mode) != record.file_type
                or opened.st_nlink != record.nlink
                or opened.st_uid != record.uid
                or opened.st_gid != record.gid
                or stat.S_IMODE(opened.st_mode) != record.mode
            )
        else:
            contract_invalid = (
                opened.st_uid != uid
                or opened.st_gid != gid
                or stat.S_IMODE(opened.st_mode) != _expected_mode(record)
                or (record.is_directory and not stat.S_ISDIR(opened.st_mode))
                or (not record.is_directory and not stat.S_ISREG(opened.st_mode))
                or (not record.is_directory and opened.st_nlink != 1)
            )
        if identity_invalid or contract_invalid:
            raise MatterStorageContractError(
                f"Matter-Storage-Metadaten sind nicht gebunden: {record.relative_path}"
            )
        if not record.is_directory:
            continue
        try:
            current_names = tuple(sorted(os.listdir(record.descriptor)))
        except OSError as exc:
            raise MatterStorageContractError(
                f"Matter-Storage-Verzeichnis driftete: {record.relative_path}"
            ) from exc
        if current_names != record.child_names:
            raise MatterStorageContractError(
                f"Matter-Storage-Namenssatz driftete: {record.relative_path}"
            )
        for name, child_index in record.children.items():
            child = records[child_index]
            named = os.stat(
                name,
                dir_fd=record.descriptor,
                follow_symlinks=False,
            )
            identity_invalid = (
                (named.st_dev, named.st_ino) != (child.device, child.inode)
            )
            if reset_mode:
                contract_invalid = (
                    stat.S_IFMT(named.st_mode) != child.file_type
                    or named.st_nlink != child.nlink
                    or named.st_uid != child.uid
                    or named.st_gid != child.gid
                    or stat.S_IMODE(named.st_mode) != child.mode
                )
            else:
                contract_invalid = (
                    named.st_uid != uid
                    or named.st_gid != gid
                    or stat.S_IMODE(named.st_mode) != _expected_mode(child)
                    or (child.is_directory and not stat.S_ISDIR(named.st_mode))
                    or (not child.is_directory and not stat.S_ISREG(named.st_mode))
                    or (not child.is_directory and named.st_nlink != 1)
                )
            if identity_invalid or contract_invalid:
                raise MatterStorageContractError(
                    f"Matter-Storage-Name driftete: {child.relative_path}"
                )


def _clear_bound_tree(
    records: list[_BoundEntry],
    *,
    uid: int,
    gid: int,
    root_parent_descriptor: int,
    root_name: str,
    reset_mode: bool = False,
) -> None:
    """Leert einen vollständig gebundenen Baum ausschließlich relativ zu FDs."""

    for record in reversed(records[1:]):
        rebound_root = os.stat(
            root_name,
            dir_fd=root_parent_descriptor,
            follow_symlinks=False,
        )
        root_opened = os.fstat(records[0].descriptor)
        if (
            not stat.S_ISDIR(rebound_root.st_mode)
            or (rebound_root.st_dev, rebound_root.st_ino)
            != (root_opened.st_dev, root_opened.st_ino)
        ):
            raise MatterStorageContractError(
                "Matter-Storage-Rootname driftete während des Leeren"
            )
        if record.parent_index is None:
            raise MatterStorageContractError(
                "Matter-Storage-Baum besitzt keinen gebundenen Elternknoten"
            )
        parent = records[record.parent_index]
        named = os.stat(
            record.name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        opened = os.fstat(record.descriptor)
        identity_invalid = (
            (named.st_dev, named.st_ino) != (record.device, record.inode)
            or (opened.st_dev, opened.st_ino) != (record.device, record.inode)
            or _mount_id(record.descriptor) != record.mount_id
        )
        if reset_mode:
            contract_invalid = (
                stat.S_IFMT(named.st_mode) != record.file_type
                or stat.S_IFMT(opened.st_mode) != record.file_type
                or named.st_nlink != record.nlink
                or opened.st_nlink != record.nlink
                or named.st_uid != record.uid
                or named.st_gid != record.gid
                or opened.st_uid != record.uid
                or opened.st_gid != record.gid
                or stat.S_IMODE(named.st_mode) != record.mode
                or stat.S_IMODE(opened.st_mode) != record.mode
            )
        else:
            contract_invalid = (
                named.st_uid != uid
                or named.st_gid != gid
                or opened.st_uid != uid
                or opened.st_gid != gid
                or stat.S_IMODE(named.st_mode) != _expected_mode(record)
                or stat.S_IMODE(opened.st_mode) != _expected_mode(record)
                or (record.is_directory and not stat.S_ISDIR(named.st_mode))
                or (not record.is_directory and not stat.S_ISREG(named.st_mode))
                or (not record.is_directory and named.st_nlink != 1)
                or (not record.is_directory and opened.st_nlink != 1)
            )
        if identity_invalid or contract_invalid:
            raise MatterStorageContractError(
                "Matter-Storage-Eintrag driftete vor dem Leeren",
                "CLEAR",
            )
        if record.is_directory:
            os.rmdir(record.name, dir_fd=parent.descriptor)
        else:
            os.unlink(record.name, dir_fd=parent.descriptor)
        try:
            os.stat(
                record.name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise MatterStorageContractError(
                "Matter-Storage-Eintrag blieb nach dem Leeren erreichbar",
                "CLEAR",
            )
        os.fsync(parent.descriptor)

    if os.listdir(records[0].descriptor):
        raise MatterStorageContractError(
            "Matter-Storage ist nach dem Leeren nicht leer",
            "CLEAR",
        )
    os.fsync(records[0].descriptor)


def _bind_regular(
    path: str,
    *,
    uid: int,
    gid: int,
    mode: int | None,
    payload: bytes | None,
    required: bool,
    allowed_nlinks: frozenset[int] = frozenset({1}),
) -> _BoundRegular:
    if not allowed_nlinks or not allowed_nlinks.issubset({1, 2}):
        raise MatterStorageContractError(
            "Matter-Reset-Datei besitzt keinen zulässigen Linkvertrag"
        )
    normalized = os.path.normpath(str(path))
    if (
        not normalized.startswith("/")
        or normalized != str(path)
        or normalized == "/"
    ):
        raise MatterStorageContractError(
            "Matter-Reset-Dateipfad ist nicht kanonisch absolut"
        )
    parent_path, name = os.path.split(normalized)
    if not name or name in {".", ".."}:
        raise MatterStorageContractError("Matter-Reset-Dateiname ist ungültig")

    parent_descriptor = _open_absolute_directory(parent_path)
    descriptor = -1
    try:
        parent_metadata = os.fstat(parent_descriptor)
        parent_mount_id = _mount_id(parent_descriptor)
        try:
            named = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if required:
                raise MatterStorageContractError(
                    "Matter-Reset-Anforderung fehlt"
                ) from None
            bound = _BoundRegular(
                parent_descriptor=parent_descriptor,
                parent_device=int(parent_metadata.st_dev),
                parent_inode=int(parent_metadata.st_ino),
                parent_mount_id=parent_mount_id,
                descriptor=-1,
                name=name,
                device=0,
                inode=0,
                mount_id=0,
                uid=int(uid),
                gid=int(gid),
                mode=-1,
                size=0,
                payload=payload,
            )
            parent_descriptor = -1
            return bound
        if (
            stat.S_ISLNK(named.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or named.st_nlink not in allowed_nlinks
            or named.st_uid != uid
            or named.st_gid != gid
            or (mode is not None and stat.S_IMODE(named.st_mode) != mode)
            or named.st_dev != parent_metadata.st_dev
        ):
            raise MatterStorageContractError(
                "Matter-Reset-Datei besitzt keinen sicheren Vertrag"
            )
        if payload is not None and named.st_size != len(payload):
            raise MatterStorageContractError(
                "Matter-Reset-Anforderung besitzt eine ungültige Größe"
            )

        nofollow, _, cloexec = _required_open_flags()
        descriptor = os.open(
            name,
            os.O_RDONLY | nofollow | cloexec | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        mount_id = _mount_id(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != named.st_nlink
            or opened.st_nlink not in allowed_nlinks
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or opened.st_uid != uid
            or opened.st_gid != gid
            or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(named.st_mode)
            or opened.st_size != named.st_size
            or mount_id != parent_mount_id
        ):
            raise MatterStorageContractError(
                "Matter-Reset-Datei wechselte beim Öffnen"
            )
        if payload is not None:
            content = os.read(descriptor, len(payload) + 1)
            if content != payload:
                raise MatterStorageContractError(
                    "Matter-Reset-Anforderung besitzt einen ungültigen Inhalt"
                )
        rebound = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            (rebound.st_dev, rebound.st_ino) != (opened.st_dev, opened.st_ino)
            or rebound.st_nlink != opened.st_nlink
            or rebound.st_nlink not in allowed_nlinks
        ):
            raise MatterStorageContractError(
                "Matter-Reset-Dateiname driftete nach dem Öffnen"
            )
        bound = _BoundRegular(
            parent_descriptor=parent_descriptor,
            parent_device=int(parent_metadata.st_dev),
            parent_inode=int(parent_metadata.st_ino),
            parent_mount_id=parent_mount_id,
            descriptor=descriptor,
            name=name,
            device=int(opened.st_dev),
            inode=int(opened.st_ino),
            mount_id=mount_id,
            uid=int(uid),
            gid=int(gid),
            mode=stat.S_IMODE(opened.st_mode),
            size=int(opened.st_size),
            payload=payload,
        )
        parent_descriptor = -1
        descriptor = -1
        return bound
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _verify_bound_regular(
    bound: _BoundRegular,
    *,
    allowed_nlinks: frozenset[int] = frozenset({1}),
) -> None:
    if not allowed_nlinks or not allowed_nlinks.issubset({1, 2}):
        raise MatterStorageContractError(
            "Matter-Reset-Datei besitzt keinen zulässigen Linkvertrag"
        )
    parent = os.fstat(bound.parent_descriptor)
    if (
        not stat.S_ISDIR(parent.st_mode)
        or (parent.st_dev, parent.st_ino)
        != (bound.parent_device, bound.parent_inode)
        or _mount_id(bound.parent_descriptor) != bound.parent_mount_id
    ):
        raise MatterStorageContractError(
            "Matter-Reset-Elternverzeichnis driftete vor der Transaktion"
        )
    if bound.descriptor < 0:
        try:
            os.stat(
                bound.name,
                dir_fd=bound.parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        raise MatterStorageContractError(
            "Matter-Reset-Datei erschien während der Transaktion"
        )
    opened = os.fstat(bound.descriptor)
    named = os.stat(
        bound.name,
        dir_fd=bound.parent_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or opened.st_nlink != named.st_nlink
        or opened.st_nlink not in allowed_nlinks
        or (opened.st_dev, opened.st_ino) != (bound.device, bound.inode)
        or (named.st_dev, named.st_ino) != (bound.device, bound.inode)
        or _mount_id(bound.descriptor) != bound.mount_id
        or opened.st_uid != bound.uid
        or opened.st_gid != bound.gid
        or stat.S_IMODE(opened.st_mode) != bound.mode
        or opened.st_size != bound.size
    ):
        raise MatterStorageContractError(
            "Matter-Reset-Datei driftete vor der Transaktion"
        )
    if bound.payload is not None:
        os.lseek(bound.descriptor, 0, os.SEEK_SET)
        if os.read(bound.descriptor, len(bound.payload) + 1) != bound.payload:
            raise MatterStorageContractError(
                "Matter-Reset-Anforderung driftete vor der Transaktion"
            )


def _unlink_bound_regular(bound: _BoundRegular) -> None:
    if bound.descriptor < 0:
        raise MatterStorageContractError(
            "Eine fehlende Matter-Reset-Datei darf nicht entfernt werden"
        )
    _verify_bound_regular(bound)
    os.unlink(bound.name, dir_fd=bound.parent_descriptor)
    try:
        os.stat(
            bound.name,
            dir_fd=bound.parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        raise MatterStorageContractError(
            "Matter-Reset-Datei blieb nach dem Entfernen erreichbar"
        )
    os.fsync(bound.parent_descriptor)


def _close_bound_regular(bound: _BoundRegular | None) -> None:
    if bound is None:
        return
    if bound.descriptor >= 0:
        os.close(bound.descriptor)
        bound.descriptor = -1
    if bound.parent_descriptor >= 0:
        os.close(bound.parent_descriptor)
        bound.parent_descriptor = -1


def _bind_untrusted_node(
    path: str,
    *,
    code: str,
    required: bool = True,
    allow_regular_hardlink: bool = False,
    allow_nonempty_directory: bool = False,
) -> _BoundNode:
    """Bindet ausschließlich den exakt benannten reservierten Reparaturknoten."""

    normalized = os.path.normpath(str(path))
    if not normalized.startswith("/") or normalized != str(path) or normalized == "/":
        raise MatterStorageContractError("Reservierter Matter-Pfad ist ungültig", code)
    parent_path, name = os.path.split(normalized)
    if not name or name in {".", ".."}:
        raise MatterStorageContractError("Reservierter Matter-Name ist ungültig", code)
    parent_descriptor = _open_absolute_directory(parent_path)
    descriptor = -1
    try:
        parent = os.fstat(parent_descriptor)
        parent_mount = _mount_id(parent_descriptor)
        try:
            named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if required:
                raise MatterStorageContractError("Reservierter Matter-Knoten fehlt", code) from None
            result = _BoundNode(
                parent_descriptor=parent_descriptor,
                parent_device=int(parent.st_dev),
                parent_inode=int(parent.st_ino),
                parent_mount_id=parent_mount,
                descriptor=-1,
                name=name,
                device=0,
                inode=0,
                mount_id=0,
                file_type=0,
                nlink=0,
                uid=0,
                gid=0,
                mode=0,
                size=0,
                empty_directory=False,
            )
            parent_descriptor = -1
            return result
        if stat.S_ISREG(named.st_mode) and named.st_nlink != 1 \
                and not allow_regular_hardlink:
            hardlink_code = "PAIRING_HARDLINK" if code == "PAIRING_BINDING" else "HARDLINK"
            raise MatterStorageContractError(
                "Reservierter Matter-Knoten besitzt Hardlinks",
                hardlink_code,
            )
        if stat.S_ISDIR(named.st_mode):
            nofollow, directory, cloexec = _required_open_flags()
            flags = os.O_RDONLY | nofollow | directory | cloexec
        else:
            flags = _path_open_flags()
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        node_mount = _mount_id(descriptor)
        if node_mount != parent_mount or opened.st_dev != parent.st_dev:
            raise MatterStorageContractError(
                "Reservierter Matter-Knoten überschreitet eine Mountgrenze",
                "FOREIGN_MOUNT",
            )
        if (
            (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(named.st_mode)
            or opened.st_nlink != named.st_nlink
        ):
            raise MatterStorageContractError("Reservierter Matter-Knoten driftete", code)
        directory_not_empty = stat.S_ISDIR(opened.st_mode) and bool(tuple(os.listdir(descriptor)))
        if directory_not_empty and not allow_nonempty_directory:
            raise MatterStorageContractError("Reservierter Matter-Ordner ist nicht leer", code)
        result = _BoundNode(
            parent_descriptor=parent_descriptor,
            parent_device=int(parent.st_dev),
            parent_inode=int(parent.st_ino),
            parent_mount_id=parent_mount,
            descriptor=descriptor,
            name=name,
            device=int(opened.st_dev),
            inode=int(opened.st_ino),
            mount_id=node_mount,
            file_type=stat.S_IFMT(opened.st_mode),
            nlink=int(opened.st_nlink),
            uid=int(opened.st_uid),
            gid=int(opened.st_gid),
            mode=stat.S_IMODE(opened.st_mode),
            size=int(opened.st_size),
            empty_directory=stat.S_ISDIR(opened.st_mode) and not directory_not_empty,
        )
        parent_descriptor = -1
        descriptor = -1
        return result
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _verify_bound_node(bound: _BoundNode, *, code: str) -> None:
    parent = os.fstat(bound.parent_descriptor)
    if bound.descriptor < 0:
        if (
            not stat.S_ISDIR(parent.st_mode)
            or (parent.st_dev, parent.st_ino) != (bound.parent_device, bound.parent_inode)
            or _mount_id(bound.parent_descriptor) != bound.parent_mount_id
        ):
            raise MatterStorageContractError("Reservierter Matter-Elternpfad driftete", code)
        try:
            os.stat(bound.name, dir_fd=bound.parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise MatterStorageContractError("Reservierter Matter-Knoten erschien", code)
    opened = os.fstat(bound.descriptor)
    named = os.stat(bound.name, dir_fd=bound.parent_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISDIR(parent.st_mode)
        or (parent.st_dev, parent.st_ino) != (bound.parent_device, bound.parent_inode)
        or _mount_id(bound.parent_descriptor) != bound.parent_mount_id
        or (opened.st_dev, opened.st_ino) != (bound.device, bound.inode)
        or (named.st_dev, named.st_ino) != (bound.device, bound.inode)
        or _mount_id(bound.descriptor) != bound.mount_id
        or stat.S_IFMT(opened.st_mode) != bound.file_type
        or stat.S_IFMT(named.st_mode) != bound.file_type
        or opened.st_nlink != bound.nlink
        or named.st_nlink != bound.nlink
        or opened.st_uid != bound.uid
        or opened.st_gid != bound.gid
        or stat.S_IMODE(opened.st_mode) != bound.mode
        or opened.st_size != bound.size
    ):
        raise MatterStorageContractError("Reservierter Matter-Knoten driftete", code)
    if bound.empty_directory and tuple(os.listdir(bound.descriptor)):
        raise MatterStorageContractError("Reservierter Matter-Ordner driftete", code)


def _unlink_bound_node(
    bound: _BoundNode,
    *,
    code: str,
    deadline: float | None,
) -> None:
    if deadline is not None:
        _check_reset_deadline(deadline)
    _verify_bound_node(bound, code=code)
    if bound.descriptor < 0:
        raise MatterStorageContractError("Fehlender Matter-Knoten darf nicht entfernt werden", code)
    if bound.empty_directory:
        if deadline is not None:
            _check_reset_deadline(deadline)
        os.rmdir(bound.name, dir_fd=bound.parent_descriptor)
    else:
        if deadline is not None:
            _check_reset_deadline(deadline)
        os.unlink(bound.name, dir_fd=bound.parent_descriptor)
    if deadline is not None:
        _check_reset_deadline(deadline)
    try:
        os.stat(bound.name, dir_fd=bound.parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise MatterStorageContractError("Reservierter Matter-Knoten blieb erreichbar", code)
    os.fsync(bound.parent_descriptor)
    if deadline is not None:
        _check_reset_deadline(deadline)


def _close_bound_node(bound: _BoundNode | None) -> None:
    if bound is None:
        return
    if bound.descriptor >= 0:
        os.close(bound.descriptor)
    os.close(bound.parent_descriptor)


def _create_storage_root(
    parent_descriptor: int,
    root_name: str,
    *,
    uid: int,
    gid: int,
    deadline: float,
) -> int:
    _check_reset_deadline(deadline)
    os.mkdir(root_name, 0o700, dir_fd=parent_descriptor)
    _check_reset_deadline(deadline)
    nofollow, directory, cloexec = _required_open_flags()
    descriptor = os.open(
        root_name,
        os.O_RDONLY | nofollow | directory | cloexec,
        dir_fd=parent_descriptor,
    )
    try:
        parent = os.fstat(parent_descriptor)
        opened = os.fstat(descriptor)
        if opened.st_dev != parent.st_dev or _mount_id(descriptor) != _mount_id(parent_descriptor):
            raise MatterStorageContractError(
                "Matter-Storage-Reparatur überschreitet eine Mountgrenze",
                "FOREIGN_MOUNT",
            )
        _check_reset_deadline(deadline)
        os.fchown(descriptor, uid, gid)
        _check_reset_deadline(deadline)
        os.fchmod(descriptor, 0o700)
        _check_reset_deadline(deadline)
        rebound = os.stat(root_name, dir_fd=parent_descriptor, follow_symlinks=False)
        final = os.fstat(descriptor)
        if (
            (rebound.st_dev, rebound.st_ino) != (final.st_dev, final.st_ino)
            or final.st_uid != uid
            or final.st_gid != gid
            or stat.S_IMODE(final.st_mode) != 0o700
            or tuple(os.listdir(descriptor))
        ):
            raise MatterStorageContractError("Matter-Storage-Reparatur driftete", "CLEAR")
        os.fsync(descriptor)
        os.fsync(parent_descriptor)
        _check_reset_deadline(deadline)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _check_reset_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise MatterStorageContractError(
            "Matter-Reset überschritt die gebundene Gesamtdeadline",
            "TIMEOUT",
        )


def _private_binding(
    metadata: os.stat_result,
    mount: int,
    *,
    depth: int = 0,
) -> dict[str, object]:
    return {
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "file_type": stat.S_IFMT(metadata.st_mode),
        "mount": int(mount),
        "names": (),
        "children": {},
        "parent": None,
        "name": "",
        "depth": int(depth),
        "nlink": int(metadata.st_nlink),
        "uid": int(metadata.st_uid),
        "gid": int(metadata.st_gid),
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _verify_private_identity(
    metadata: os.stat_result,
    binding: dict[str, object],
    mount: int,
) -> None:
    if binding["file_type"] == stat.S_IFREG and (
        binding["nlink"] != 1 or metadata.st_nlink != 1
    ):
        raise MatterStorageContractError(
            "Private Matter-Quarantäne enthält oder erhielt einen Hardlink",
            "HARDLINK",
        )
    if (
        (metadata.st_dev, metadata.st_ino)
        != (binding["device"], binding["inode"])
        or stat.S_IFMT(metadata.st_mode) != binding["file_type"]
        or mount != binding["mount"]
    ):
        raise MatterStorageContractError(
            "Private Matter-Quarantäne driftete",
            "BINDING",
        )


def _scan_mount_tree(
    root_descriptor: int,
    deadline: float,
    *,
    max_entries: int | None = None,
    max_depth: int | None = None,
    regular_tree_only: bool = True,
) -> list[dict[str, object]]:
    """Bindet den vollständigen Baum iterativ mit höchstens wenigen offenen FDs."""

    _check_reset_deadline(deadline)
    nofollow, directory, cloexec = _required_open_flags()
    directory_flags = os.O_RDONLY | nofollow | directory | cloexec
    root_metadata = os.fstat(root_descriptor)
    root_mount = _mount_id(root_descriptor)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise MatterStorageContractError(
            "Matter-Storage-Root ist kein Verzeichnis",
            "BINDING",
        )
    root_binding = _private_binding(root_metadata, root_mount)
    records: list[dict[str, object]] = [root_binding]
    seen_directories = {(int(root_metadata.st_dev), int(root_metadata.st_ino))}
    current = os.dup(root_descriptor)
    frames: list[dict[str, int]] = []
    try:
        root_names = tuple(sorted(os.listdir(current)))
        root_binding["names"] = root_names
        frames.append({"record": 0, "offset": 0})
        while frames:
            _check_reset_deadline(deadline)
            frame = frames[-1]
            record = records[frame["record"]]
            names = record["names"]
            if not isinstance(names, tuple):
                raise MatterStorageContractError(
                    "Private Matter-Quarantäne besitzt keinen gebundenen Namenssatz",
                    "BINDING",
                )
            if frame["offset"] >= len(names):
                if tuple(sorted(os.listdir(current))) != names:
                    raise MatterStorageContractError(
                        "Private Matter-Quarantäne driftete beim Scan",
                        "BINDING",
                    )
                if len(frames) == 1:
                    break
                child_record = record
                parent_record = records[frames[-2]["record"]]
                parent_descriptor = os.open("..", directory_flags, dir_fd=current)
                try:
                    _verify_private_identity(
                        os.fstat(parent_descriptor),
                        parent_record,
                        _mount_id(parent_descriptor),
                    )
                    child_name = child_record["name"]
                    if not isinstance(child_name, str) or not child_name:
                        raise MatterStorageContractError(
                            "Private Matter-Quarantäne besitzt einen ungültigen Namen",
                            "BINDING",
                        )
                    rebound = os.stat(
                        child_name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    _verify_private_identity(rebound, child_record, root_mount)
                except Exception:
                    os.close(parent_descriptor)
                    raise
                os.close(current)
                current = parent_descriptor
                frames.pop()
                continue

            name = names[frame["offset"]]
            frame["offset"] += 1
            if max_entries is not None and len(records) >= max_entries:
                raise MatterStorageContractError(
                    "Matter-Storage überschreitet das Eintragslimit",
                    "LIMIT",
                )
            parent_depth = record["depth"]
            if not isinstance(parent_depth, int):
                raise MatterStorageContractError(
                    "Matter-Storage besitzt keine gebundene Tiefe",
                    "BINDING",
                )
            child_depth = parent_depth + 1
            if max_depth is not None and child_depth > max_depth:
                raise MatterStorageContractError(
                    "Matter-Storage überschreitet das Tiefenlimit",
                    "LIMIT",
                )
            named = os.stat(name, dir_fd=current, follow_symlinks=False)
            path_descriptor = os.open(name, _path_open_flags(), dir_fd=current)
            try:
                opened = os.fstat(path_descriptor)
                entry_mount = _mount_id(path_descriptor)
                if (
                    (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                    or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(named.st_mode)
                    or entry_mount != root_mount
                ):
                    code = "FOREIGN_MOUNT" if entry_mount != root_mount else "BINDING"
                    raise MatterStorageContractError(
                        "Private Matter-Quarantäne driftete oder enthält einen Fremdmount",
                        code,
                    )
                is_directory = stat.S_ISDIR(named.st_mode) and not stat.S_ISLNK(named.st_mode)
                is_regular = stat.S_ISREG(named.st_mode) and not stat.S_ISLNK(named.st_mode)
                if regular_tree_only and not (is_directory or is_regular):
                    raise MatterStorageContractError(
                        "Matter-Storage enthält Symlink oder Sonderdatei",
                        "CONTRACT",
                    )
                if regular_tree_only and is_regular and opened.st_nlink != 1:
                    raise MatterStorageContractError(
                        "Matter-Storage enthält eine reguläre Datei mit mehreren Hardlinks",
                        "HARDLINK",
                    )
                child = _private_binding(
                    opened,
                    entry_mount,
                    depth=child_depth,
                )
                child["parent"] = frame["record"]
                child["name"] = name
                child_index = len(records)
                records.append(child)
                children = record["children"]
                if not isinstance(children, dict):
                    raise MatterStorageContractError(
                        "Private Matter-Quarantäne besitzt keine gebundene Kindtabelle",
                        "BINDING",
                    )
                children[name] = child_index
                if not is_directory:
                    continue
                identity = (int(opened.st_dev), int(opened.st_ino))
                if identity in seen_directories:
                    raise MatterStorageContractError(
                        "Private Matter-Quarantäne enthält eine zyklische Verzeichnisidentität",
                        "BINDING",
                    )
                seen_directories.add(identity)
                child_descriptor = os.open(name, directory_flags, dir_fd=current)
                try:
                    _verify_private_identity(
                        os.fstat(child_descriptor),
                        child,
                        _mount_id(child_descriptor),
                    )
                    child["names"] = tuple(sorted(os.listdir(child_descriptor)))
                except Exception:
                    os.close(child_descriptor)
                    raise
            finally:
                os.close(path_descriptor)
            os.close(current)
            current = child_descriptor
            frames.append({"record": child_index, "offset": 0})
        return records
    finally:
        os.close(current)


def _apply_private_tree_contract(
    root_descriptor: int,
    records: list[dict[str, object]],
    *,
    uid: int,
    gid: int,
    harden: bool,
    deadline: float = float("inf"),
) -> None:
    """Härtet/verifiziert einen Snapshot mit konstant kleinem FD-Bestand."""

    if not records:
        raise MatterStorageContractError(
            "Matter-Storage besitzt keinen gebundenen Snapshot",
            "BINDING",
        )
    root_mount = _mount_id(root_descriptor)
    nofollow, directory, cloexec = _required_open_flags()
    directory_flags = os.O_RDONLY | nofollow | directory | cloexec
    file_flags = (
        os.O_RDONLY
        | nofollow
        | cloexec
        | getattr(os, "O_NONBLOCK", 0)
    )
    privileged = os.geteuid() == 0

    def apply_contract(descriptor: int, record: dict[str, object]) -> None:
        _check_reset_deadline(deadline)
        opened = os.fstat(descriptor)
        _verify_private_identity(opened, record, _mount_id(descriptor))
        file_type = record["file_type"]
        is_directory = file_type == stat.S_IFDIR
        if file_type not in {stat.S_IFDIR, stat.S_IFREG}:
            raise MatterStorageContractError(
                "Matter-Storage enthält Symlink oder Sonderdatei",
                "CONTRACT",
            )
        if not is_directory and opened.st_nlink != 1:
            raise MatterStorageContractError(
                "Matter-Storage enthält eine reguläre Datei mit mehreren Hardlinks",
                "HARDLINK",
            )
        expected_mode = 0o700 if is_directory else 0o600
        if harden:
            if privileged:
                if opened.st_uid != uid or opened.st_gid != gid:
                    _check_reset_deadline(deadline)
                    os.fchown(descriptor, uid, gid)
                    _check_reset_deadline(deadline)
            elif opened.st_uid != uid or opened.st_gid != gid:
                raise MatterStorageContractError(
                    "Matter-Storage-Härtung benötigt root für den Eigentümerwechsel"
                )
            opened = os.fstat(descriptor)
            if stat.S_IMODE(opened.st_mode) != expected_mode:
                _check_reset_deadline(deadline)
                os.fchmod(descriptor, expected_mode)
                _check_reset_deadline(deadline)
        final = os.fstat(descriptor)
        if (
            (final.st_dev, final.st_ino)
            != (record["device"], record["inode"])
            or _mount_id(descriptor) != root_mount
            or stat.S_IFMT(final.st_mode) != file_type
            or final.st_uid != uid
            or final.st_gid != gid
            or stat.S_IMODE(final.st_mode) != expected_mode
            or (not is_directory and final.st_nlink != 1)
        ):
            raise MatterStorageContractError(
                "Matter-Storage-Metadaten drifteten während der Prüfung",
                "BINDING",
            )
        _check_reset_deadline(deadline)

    apply_contract(root_descriptor, records[0])
    root_names = records[0]["names"]
    if not isinstance(root_names, tuple) \
            or tuple(sorted(os.listdir(root_descriptor))) != root_names:
        raise MatterStorageContractError(
            "Matter-Storage-Rootnamenssatz driftete vor der Prüfung",
            "BINDING",
        )

    current = os.dup(root_descriptor)
    frames: list[dict[str, int]] = [{"record": 0, "offset": 0}]
    try:
        while frames:
            _check_reset_deadline(deadline)
            frame = frames[-1]
            record = records[frame["record"]]
            names = record["names"]
            children = record["children"]
            if not isinstance(names, tuple) or not isinstance(children, dict):
                raise MatterStorageContractError(
                    "Matter-Storage-Snapshot ist unvollständig",
                    "BINDING",
                )
            if frame["offset"] >= len(names):
                if tuple(sorted(os.listdir(current))) != names:
                    raise MatterStorageContractError(
                        "Matter-Storage-Namenssatz driftete während der Prüfung",
                        "BINDING",
                    )
                if len(frames) == 1:
                    break
                child_record = record
                parent_record = records[frames[-2]["record"]]
                parent_descriptor = os.open(
                    "..",
                    directory_flags,
                    dir_fd=current,
                )
                try:
                    _verify_private_identity(
                        os.fstat(parent_descriptor),
                        parent_record,
                        _mount_id(parent_descriptor),
                    )
                    child_name = child_record["name"]
                    if not isinstance(child_name, str) or not child_name:
                        raise MatterStorageContractError(
                            "Matter-Storage-Snapshot besitzt einen ungültigen Namen",
                            "BINDING",
                        )
                    rebound = os.stat(
                        child_name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    _verify_private_identity(rebound, child_record, root_mount)
                except Exception:
                    os.close(parent_descriptor)
                    raise
                os.close(current)
                current = parent_descriptor
                frames.pop()
                continue

            name = names[frame["offset"]]
            frame["offset"] += 1
            child_index = children.get(name)
            if not isinstance(child_index, int):
                raise MatterStorageContractError(
                    "Matter-Storage-Snapshot verlor einen gebundenen Knoten",
                    "BINDING",
                )
            child = records[child_index]
            child_type = child["file_type"]
            child_is_directory = child_type == stat.S_IFDIR
            open_flags = directory_flags if child_is_directory else file_flags
            named = os.stat(name, dir_fd=current, follow_symlinks=False)
            child_descriptor = os.open(name, open_flags, dir_fd=current)
            try:
                opened = os.fstat(child_descriptor)
                _verify_private_identity(named, child, root_mount)
                _verify_private_identity(
                    opened,
                    child,
                    _mount_id(child_descriptor),
                )
                if not child_is_directory and (
                    named.st_nlink != 1 or opened.st_nlink != 1
                ):
                    raise MatterStorageContractError(
                        "Matter-Storage-Datei driftete zu einem Hardlink",
                        "HARDLINK",
                    )
                apply_contract(child_descriptor, child)
                if child_is_directory:
                    child_names = child["names"]
                    if not isinstance(child_names, tuple) \
                            or tuple(sorted(os.listdir(child_descriptor))) != child_names:
                        raise MatterStorageContractError(
                            "Matter-Storage-Verzeichnis driftete vor dem Abstieg",
                            "BINDING",
                        )
            except Exception:
                os.close(child_descriptor)
                raise
            if child_is_directory:
                os.close(current)
                current = child_descriptor
                frames.append({"record": child_index, "offset": 0})
            else:
                os.close(child_descriptor)
    finally:
        os.close(current)


def _clear_private_tree(
    root_descriptor: int,
    root_mount: int,
    deadline: float,
) -> None:
    """Leert einen vollständig vorgebundenen Baum iterativ und postorder."""

    records = _scan_mount_tree(root_descriptor, deadline)
    if records[0]["mount"] != root_mount:
        raise MatterStorageContractError(
            "Private Matter-Quarantäne wechselte die Mountidentität",
            "FOREIGN_MOUNT",
        )
    nofollow, directory, cloexec = _required_open_flags()
    directory_flags = os.O_RDONLY | nofollow | directory | cloexec
    current = os.dup(root_descriptor)
    frames: list[dict[str, int]] = [{"record": 0, "offset": 0}]
    try:
        while frames:
            _check_reset_deadline(deadline)
            frame = frames[-1]
            record = records[frame["record"]]
            names = record["names"]
            children = record["children"]
            if not isinstance(names, tuple) or not isinstance(children, dict):
                raise MatterStorageContractError(
                    "Private Matter-Quarantäne besitzt keinen Löschsnapshot",
                    "BINDING",
                )
            remaining = names[frame["offset"]:]
            if tuple(sorted(os.listdir(current))) != remaining:
                raise MatterStorageContractError(
                    "Private Matter-Quarantäne driftete vor dem Leeren",
                    "BINDING",
                )
            if not remaining:
                if len(frames) == 1:
                    break
                child_record = record
                parent_record = records[frames[-2]["record"]]
                parent_descriptor = os.open("..", directory_flags, dir_fd=current)
                try:
                    _verify_private_identity(
                        os.fstat(parent_descriptor),
                        parent_record,
                        _mount_id(parent_descriptor),
                    )
                    child_name = child_record["name"]
                    if not isinstance(child_name, str) or not child_name:
                        raise MatterStorageContractError(
                            "Private Matter-Quarantäne besitzt einen ungültigen Löschknoten",
                            "BINDING",
                        )
                    rebound = os.stat(
                        child_name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    _verify_private_identity(rebound, child_record, root_mount)
                    _check_reset_deadline(deadline)
                    os.rmdir(child_name, dir_fd=parent_descriptor)
                    _check_reset_deadline(deadline)
                    try:
                        os.stat(
                            child_name,
                            dir_fd=parent_descriptor,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        raise MatterStorageContractError(
                            "Private Matter-Quarantäne-Verzeichnis blieb erreichbar",
                            "CLEAR",
                        )
                    os.fsync(parent_descriptor)
                    _check_reset_deadline(deadline)
                except Exception:
                    os.close(parent_descriptor)
                    raise
                os.close(current)
                current = parent_descriptor
                frames.pop()
                continue

            name = remaining[0]
            child_index = children.get(name)
            if not isinstance(child_index, int):
                raise MatterStorageContractError(
                    "Private Matter-Quarantäne verlor einen gebundenen Knoten",
                    "BINDING",
                )
            child = records[child_index]
            frame["offset"] += 1
            named = os.stat(name, dir_fd=current, follow_symlinks=False)
            path_descriptor = os.open(name, _path_open_flags(), dir_fd=current)
            try:
                opened = os.fstat(path_descriptor)
                _verify_private_identity(opened, child, _mount_id(path_descriptor))
                _verify_private_identity(named, child, root_mount)
                is_directory = stat.S_ISDIR(named.st_mode) and not stat.S_ISLNK(named.st_mode)
                if is_directory:
                    child_descriptor = os.open(name, directory_flags, dir_fd=current)
                    try:
                        _verify_private_identity(
                            os.fstat(child_descriptor),
                            child,
                            _mount_id(child_descriptor),
                        )
                    except Exception:
                        os.close(child_descriptor)
                        raise
                else:
                    rebound = os.stat(name, dir_fd=current, follow_symlinks=False)
                    _verify_private_identity(rebound, child, root_mount)
                    _check_reset_deadline(deadline)
                    os.unlink(name, dir_fd=current)
                    _check_reset_deadline(deadline)
                    try:
                        os.stat(name, dir_fd=current, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise MatterStorageContractError(
                            "Private Matter-Quarantäne-Knoten blieb erreichbar",
                            "CLEAR",
                        )
                    os.fsync(current)
                    _check_reset_deadline(deadline)
            finally:
                os.close(path_descriptor)
            if is_directory:
                os.close(current)
                current = child_descriptor
                frames.append({"record": child_index, "offset": 0})
    finally:
        os.close(current)


@dataclass
class _ResetTransaction:
    marker: _BoundRegular
    document: dict[str, object]
    quarantine_descriptor: int
    receipt: bool


def _rename_noreplace(
    source_name: str,
    target_name: str,
    *,
    source_parent: int,
    target_parent: int,
    collision_code: str,
) -> None:
    """Publiziert reservierte Resetnamen ohne einen vorhandenen Knoten zu ersetzen."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise MatterStorageContractError(
            "renameat2(RENAME_NOREPLACE) fehlt für die Matter-Resettransaktion",
            "CONTRACT",
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent,
        os.fsencode(source_name),
        target_parent,
        os.fsencode(target_name),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise MatterStorageContractError(
            "Reservierter Matter-Resetname ist bereits fremd belegt",
            collision_code,
        )
    raise MatterStorageContractError(
        "Matter-Resetname konnte nicht atomar veröffentlicht werden",
        "FOREIGN_MOUNT" if error_number == errno.EXDEV else "BINDING",
    )


def _transaction_identity(metadata: os.stat_result, mount: int) -> dict[str, int]:
    return {
        "dev": int(metadata.st_dev),
        "ino": int(metadata.st_ino),
        "mount_id": int(mount),
    }


def _transaction_document(
    parent: os.stat_result,
    parent_mount: int,
    quarantine: os.stat_result,
    quarantine_mount: int,
    source: os.stat_result,
    source_mount: int,
    source_name: str,
) -> dict[str, object]:
    return {
        "schema": RESET_TRANSACTION_SCHEMA,
        "parent": _transaction_identity(parent, parent_mount),
        "quarantine": {
            "name": RESET_QUARANTINE_NAME,
            **_transaction_identity(quarantine, quarantine_mount),
        },
        "source": {
            "name": source_name,
            "quarantine_name": "storage",
            **_transaction_identity(source, source_mount),
        },
    }


def _transaction_payload(document: dict[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def _valid_transaction_identity(raw: object) -> bool:
    return isinstance(raw, dict) \
        and set(raw) == {"dev", "ino", "mount_id"} \
        and all(type(raw[key]) is int and raw[key] > 0 for key in raw)


def _validate_transaction_document(
    raw: object,
    *,
    source_name: str,
) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {
        "schema", "parent", "quarantine", "source"
    }:
        raise MatterStorageContractError(
            "Matter-Resetmarker besitzt kein geschlossenes Schema",
            "QUARANTINE_COLLISION",
        )
    parent = raw.get("parent")
    quarantine = raw.get("quarantine")
    source = raw.get("source")
    if raw.get("schema") != RESET_TRANSACTION_SCHEMA \
            or not _valid_transaction_identity(parent) \
            or not isinstance(quarantine, dict) \
            or set(quarantine) != {"name", "dev", "ino", "mount_id"} \
            or quarantine.get("name") != RESET_QUARANTINE_NAME \
            or not _valid_transaction_identity({
                key: quarantine.get(key) for key in ("dev", "ino", "mount_id")
            }) \
            or not isinstance(source, dict) \
            or set(source) != {
                "name", "quarantine_name", "dev", "ino", "mount_id"
            } \
            or source.get("name") != source_name \
            or source.get("quarantine_name") != "storage" \
            or not _valid_transaction_identity({
                key: source.get(key) for key in ("dev", "ino", "mount_id")
            }):
        raise MatterStorageContractError(
            "Matter-Resetmarker besitzt ungültige Identitätsfelder",
            "QUARANTINE_COLLISION",
        )
    return raw


def _bind_transaction_marker(
    path: str,
    *,
    source_name: str,
    required: bool,
    allowed_nlinks: frozenset[int] = frozenset({1}),
) -> tuple[_BoundRegular, dict[str, object] | None]:
    try:
        marker = _bind_regular(
            path,
            uid=0,
            gid=0,
            mode=0o600,
            payload=None,
            required=required,
            allowed_nlinks=allowed_nlinks,
        )
    except (MatterStorageContractError, OSError) as exc:
        raise MatterStorageContractError(
            "Reservierter Matter-Resetmarker kollidiert mit fremdem Bestand",
            "QUARANTINE_COLLISION",
        ) from exc
    if marker.descriptor < 0:
        return marker, None
    try:
        if marker.size < 2 or marker.size > RESET_TRANSACTION_MARKER_MAX_BYTES:
            raise MatterStorageContractError(
                "Matter-Resetmarker besitzt eine ungültige Größe",
                "QUARANTINE_COLLISION",
            )
        content = os.read(marker.descriptor, marker.size + 1)
        try:
            decoded = json.loads(content.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MatterStorageContractError(
                "Matter-Resetmarker ist nicht kanonisch lesbar",
                "QUARANTINE_COLLISION",
            ) from exc
        document = _validate_transaction_document(decoded, source_name=source_name)
        if content != _transaction_payload(document):
            raise MatterStorageContractError(
                "Matter-Resetmarker ist nicht kanonisch serialisiert",
                "QUARANTINE_COLLISION",
            )
        marker.payload = content
        _verify_bound_regular(marker, allowed_nlinks=allowed_nlinks)
        return marker, document
    except Exception:
        _close_bound_regular(marker)
        raise


def _bind_transaction_directory(path: str, *, required: bool) -> _BoundNode:
    try:
        bound = _bind_untrusted_node(
            path,
            code="QUARANTINE_COLLISION",
            required=required,
            allow_nonempty_directory=True,
        )
        if bound.descriptor >= 0 and (
            bound.file_type != stat.S_IFDIR
            or bound.uid != 0
            or bound.gid != 0
            or bound.mode != 0o700
        ):
            raise MatterStorageContractError(
                "Reservierte Matter-Quarantäne ist nicht root-privat",
                "QUARANTINE_COLLISION",
            )
        _verify_bound_node(bound, code="QUARANTINE_COLLISION")
        return bound
    except (MatterStorageContractError, OSError) as exc:
        if isinstance(exc, MatterStorageContractError) \
                and exc.code == "QUARANTINE_COLLISION":
            raise
        raise MatterStorageContractError(
            "Reservierte Matter-Quarantäne kollidiert mit fremdem Bestand",
            "QUARANTINE_COLLISION",
        ) from exc


def _transaction_identity_matches(
    metadata: os.stat_result,
    mount: int,
    identity: object,
) -> bool:
    return isinstance(identity, dict) \
        and (int(metadata.st_dev), int(metadata.st_ino), int(mount)) == (
            identity.get("dev"), identity.get("ino"), identity.get("mount_id")
        )


def _load_reset_transaction(
    parent_path: str,
    storage_parent: int,
    source_name: str,
    deadline: float,
) -> _ResetTransaction | None:
    """Bindet genau eine intern markierte Quarantäne oder ein End-Receipt."""

    _check_reset_deadline(deadline)
    prepare_path = os.path.join(parent_path, RESET_QUARANTINE_PREPARE_NAME)
    quarantine_path = os.path.join(parent_path, RESET_QUARANTINE_NAME)
    receipt_path = os.path.join(parent_path, RESET_TRANSACTION_MARKER_NAME)
    prepare: _BoundNode | None = None
    quarantine: _BoundNode | None = None
    receipt: _BoundRegular | None = None
    receipt_document: dict[str, object] | None = None
    active_marker: _BoundRegular | None = None
    try:
        prepare = _bind_transaction_directory(prepare_path, required=False)
        quarantine = _bind_transaction_directory(quarantine_path, required=False)
        receipt, receipt_document = _bind_transaction_marker(
            receipt_path,
            source_name=source_name,
            required=False,
            allowed_nlinks=frozenset({1, 2}),
        )
        present_directories = [
            bound for bound in (prepare, quarantine) if bound.descriptor >= 0
        ]
        if receipt.descriptor >= 0:
            if prepare.descriptor >= 0 or len(present_directories) > 1:
                raise MatterStorageContractError(
                    "Matter-Reset-Receipt kollidiert mit einer Prepare-Quarantäne",
                    "QUARANTINE_COLLISION",
                )
            if receipt_document is None:
                raise MatterStorageContractError(
                    "Matter-Reset-Receipt ist unvollständig",
                    "QUARANTINE_COLLISION",
                )
            receipt_links = os.fstat(receipt.descriptor).st_nlink
            parent = os.fstat(storage_parent)
            if not _transaction_identity_matches(
                parent,
                _mount_id(storage_parent),
                receipt_document["parent"],
            ):
                raise MatterStorageContractError(
                    "Matter-Reset-Receipt bindet einen fremden Elternpfad",
                    "QUARANTINE_COLLISION",
                )
            quarantine_descriptor = -1
            if quarantine.descriptor >= 0:
                quarantine_identity = receipt_document["quarantine"]
                quarantine_entries = set(os.listdir(quarantine.descriptor))
                if not _transaction_identity_matches(
                    os.fstat(quarantine.descriptor),
                    _mount_id(quarantine.descriptor),
                    quarantine_identity,
                ):
                    raise MatterStorageContractError(
                        "Matter-Reset-Receipt bindet eine fremde Endquarantäne",
                        "QUARANTINE_COLLISION",
                    )
                if receipt_links == 2:
                    if quarantine_entries != {RESET_TRANSACTION_MARKER_NAME}:
                        raise MatterStorageContractError(
                            "Matter-Dual-Receipt besitzt keinen exklusiven internen Marker",
                            "QUARANTINE_COLLISION",
                        )
                    internal_marker, internal_document = _bind_transaction_marker(
                        os.path.join(
                            quarantine_path,
                            RESET_TRANSACTION_MARKER_NAME,
                        ),
                        source_name=source_name,
                        required=True,
                        allowed_nlinks=frozenset({2}),
                    )
                    active_marker = internal_marker
                    if internal_document != receipt_document \
                            or (internal_marker.device, internal_marker.inode) != (
                                receipt.device,
                                receipt.inode,
                            ):
                        raise MatterStorageContractError(
                            "Matter-Dual-Receipt bindet nicht denselben Marker-Inode",
                            "QUARANTINE_COLLISION",
                        )
                    _verify_bound_regular(
                        receipt,
                        allowed_nlinks=frozenset({2}),
                    )
                    _verify_bound_regular(
                        internal_marker,
                        allowed_nlinks=frozenset({2}),
                    )
                    _check_reset_deadline(deadline)
                    os.fsync(storage_parent)
                    _check_reset_deadline(deadline)
                    os.unlink(
                        RESET_TRANSACTION_MARKER_NAME,
                        dir_fd=quarantine.descriptor,
                    )
                    os.fsync(quarantine.descriptor)
                    _check_reset_deadline(deadline)
                    _verify_bound_regular(receipt)
                    if tuple(os.listdir(quarantine.descriptor)):
                        raise MatterStorageContractError(
                            "Matter-Dual-Receipt ließ einen Quarantänenrest zurück",
                            "QUARANTINE_COLLISION",
                        )
                elif receipt_links == 1:
                    if quarantine_entries:
                        raise MatterStorageContractError(
                            "Matter-Reset-Receipt bindet keine leere Endquarantäne",
                            "QUARANTINE_COLLISION",
                        )
                    _verify_bound_regular(receipt)
                else:
                    raise MatterStorageContractError(
                        "Matter-Reset-Receipt besitzt fremde Hardlinks",
                        "QUARANTINE_COLLISION",
                    )
                quarantine_descriptor = quarantine.descriptor
                quarantine.descriptor = -1
            elif receipt_links != 1:
                raise MatterStorageContractError(
                    "Matter-Dual-Receipt verlor seine gebundene Quarantäne",
                    "QUARANTINE_COLLISION",
                )
            else:
                _verify_bound_regular(receipt)
            transaction = _ResetTransaction(
                marker=receipt,
                document=receipt_document,
                quarantine_descriptor=quarantine_descriptor,
                receipt=True,
            )
            receipt = None
            return transaction

        _close_bound_regular(receipt)
        receipt = None
        if not present_directories:
            return None
        if len(present_directories) != 1:
            raise MatterStorageContractError(
                "Prepare- und Endquarantäne sind gleichzeitig belegt",
                "QUARANTINE_COLLISION",
            )
        active = present_directories[0]
        active_name = (
            RESET_QUARANTINE_PREPARE_NAME
            if active is prepare
            else RESET_QUARANTINE_NAME
        )
        marker_path = os.path.join(
            parent_path,
            active_name,
            RESET_TRANSACTION_MARKER_NAME,
        )
        marker, document = _bind_transaction_marker(
            marker_path,
            source_name=source_name,
            required=True,
        )
        active_marker = marker
        if document is None:
            raise MatterStorageContractError(
                "Matter-Quarantäne besitzt keinen gültigen Transaktionsmarker",
                "QUARANTINE_COLLISION",
            )
        parent = os.fstat(storage_parent)
        if not _transaction_identity_matches(
            parent,
            _mount_id(storage_parent),
            document["parent"],
        ) or not _transaction_identity_matches(
            os.fstat(active.descriptor),
            _mount_id(active.descriptor),
            document["quarantine"],
        ):
            raise MatterStorageContractError(
                "Matter-Quarantäne bindet fremde Parent- oder Quarantäne-Inodes",
                "QUARANTINE_COLLISION",
            )
        allowed_names = {RESET_TRANSACTION_MARKER_NAME, "storage"}
        if not set(os.listdir(active.descriptor)).issubset(allowed_names):
            raise MatterStorageContractError(
                "Matter-Quarantäne enthält nicht markierten Bestand",
                "QUARANTINE_COLLISION",
            )
        if active is prepare:
            if "storage" in os.listdir(active.descriptor):
                raise MatterStorageContractError(
                    "Prepare-Quarantäne enthält bereits Storage",
                    "QUARANTINE_COLLISION",
                )
            _check_reset_deadline(deadline)
            _rename_noreplace(
                RESET_QUARANTINE_PREPARE_NAME,
                RESET_QUARANTINE_NAME,
                source_parent=storage_parent,
                target_parent=storage_parent,
                collision_code="QUARANTINE_COLLISION",
            )
            os.fsync(storage_parent)
            _check_reset_deadline(deadline)
            named = os.stat(
                RESET_QUARANTINE_NAME,
                dir_fd=storage_parent,
                follow_symlinks=False,
            )
            if (named.st_dev, named.st_ino) != (active.device, active.inode):
                raise MatterStorageContractError(
                    "Matter-Quarantäne driftete beim atomaren Publish",
                    "QUARANTINE_COLLISION",
                )
        quarantine_descriptor = active.descriptor
        active.descriptor = -1
        transaction = _ResetTransaction(
            marker=marker,
            document=document,
            quarantine_descriptor=quarantine_descriptor,
            receipt=False,
        )
        active_marker = None
        return transaction
    finally:
        _close_bound_node(prepare)
        _close_bound_node(quarantine)
        _close_bound_regular(receipt)
        _close_bound_regular(active_marker)


def _create_reset_transaction(
    parent_path: str,
    storage_parent: int,
    source_name: str,
    source_descriptor: int,
    deadline: float,
) -> _ResetTransaction:
    """Erzeugt Marker+Quarantäne privat und publiziert nur markierte feste Namen."""

    _check_reset_deadline(deadline)
    stage_name = ""
    for _ in range(32):
        candidate = RESET_TRANSACTION_STAGE_PREFIX + os.urandom(16).hex()
        try:
            os.mkdir(candidate, 0o700, dir_fd=storage_parent)
        except FileExistsError:
            continue
        stage_name = candidate
        break
    if not stage_name:
        raise MatterStorageContractError(
            "Private Matter-Resetstage konnte nicht exklusiv angelegt werden",
            "QUARANTINE_COLLISION",
        )
    nofollow, directory, cloexec = _required_open_flags()
    prepare_descriptor = os.open(
        stage_name,
        os.O_RDONLY | nofollow | directory | cloexec,
        dir_fd=storage_parent,
    )
    try:
        _check_reset_deadline(deadline)
        os.fchown(prepare_descriptor, 0, 0)
        os.fchmod(prepare_descriptor, 0o700)
        prepare = os.fstat(prepare_descriptor)
        stage_named = os.stat(
            stage_name,
            dir_fd=storage_parent,
            follow_symlinks=False,
        )
        parent = os.fstat(storage_parent)
        source = os.fstat(source_descriptor)
        prepare_mount = _mount_id(prepare_descriptor)
        parent_mount = _mount_id(storage_parent)
        source_mount = _mount_id(source_descriptor)
        if (
            prepare.st_uid != 0
            or prepare.st_gid != 0
            or stat.S_IMODE(prepare.st_mode) != 0o700
            or not stat.S_ISDIR(stage_named.st_mode)
            or (stage_named.st_dev, stage_named.st_ino)
            != (prepare.st_dev, prepare.st_ino)
            or prepare.st_dev != parent.st_dev
            or prepare_mount != parent_mount
            or tuple(os.listdir(prepare_descriptor))
        ):
            raise MatterStorageContractError(
                "Private Matter-Prepare-Quarantäne ist nicht sicher gebunden",
                "QUARANTINE_COLLISION",
            )
        document = _transaction_document(
            parent,
            parent_mount,
            prepare,
            prepare_mount,
            source,
            source_mount,
            source_name,
        )
        payload = _transaction_payload(document)
        if len(payload) > RESET_TRANSACTION_MARKER_MAX_BYTES:
            raise MatterStorageContractError(
                "Matter-Resetmarker überschreitet sein Limit",
                "CONTRACT",
            )
        marker_descriptor = os.open(
            RESET_TRANSACTION_MARKER_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec,
            0o600,
            dir_fd=prepare_descriptor,
        )
        try:
            os.fchown(marker_descriptor, 0, 0)
            os.fchmod(marker_descriptor, 0o600)
            offset = 0
            while offset < len(payload):
                written = os.write(marker_descriptor, payload[offset:])
                if written <= 0:
                    raise MatterStorageContractError(
                        "Matter-Resetmarker konnte nicht vollständig geschrieben werden",
                        "QUARANTINE_COLLISION",
                    )
                offset += written
            os.fsync(marker_descriptor)
            marker_meta = os.fstat(marker_descriptor)
            if (
                not stat.S_ISREG(marker_meta.st_mode)
                or marker_meta.st_nlink != 1
                or marker_meta.st_uid != 0
                or marker_meta.st_gid != 0
                or stat.S_IMODE(marker_meta.st_mode) != 0o600
                or marker_meta.st_size != len(payload)
            ):
                raise MatterStorageContractError(
                    "Matter-Resetmarker besitzt keinen root-privaten Vertrag",
                    "QUARANTINE_COLLISION",
                )
        finally:
            os.close(marker_descriptor)
        os.fsync(prepare_descriptor)
        os.fsync(storage_parent)
        rebound_stage = os.stat(
            stage_name,
            dir_fd=storage_parent,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(rebound_stage.st_mode)
            or (rebound_stage.st_dev, rebound_stage.st_ino)
            != (prepare.st_dev, prepare.st_ino)
            or set(os.listdir(prepare_descriptor)) != {
                RESET_TRANSACTION_MARKER_NAME
            }
        ):
            raise MatterStorageContractError(
                "Private Matter-Resetstage driftete vor dem atomaren Publish",
                "QUARANTINE_COLLISION",
            )
        _check_reset_deadline(deadline)
        _rename_noreplace(
            stage_name,
            RESET_QUARANTINE_PREPARE_NAME,
            source_parent=storage_parent,
            target_parent=storage_parent,
            collision_code="QUARANTINE_COLLISION",
        )
        os.fsync(storage_parent)
        published_prepare = os.stat(
            RESET_QUARANTINE_PREPARE_NAME,
            dir_fd=storage_parent,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(published_prepare.st_mode)
            or (published_prepare.st_dev, published_prepare.st_ino)
            != (prepare.st_dev, prepare.st_ino)
            or set(os.listdir(prepare_descriptor)) != {
                RESET_TRANSACTION_MARKER_NAME
            }
        ):
            raise MatterStorageContractError(
                "Markierte Matter-Prepare-Quarantäne driftete nach dem Publish",
                "QUARANTINE_COLLISION",
            )
        _check_reset_deadline(deadline)
        _rename_noreplace(
            RESET_QUARANTINE_PREPARE_NAME,
            RESET_QUARANTINE_NAME,
            source_parent=storage_parent,
            target_parent=storage_parent,
            collision_code="QUARANTINE_COLLISION",
        )
        os.fsync(storage_parent)
        _check_reset_deadline(deadline)
    finally:
        os.close(prepare_descriptor)
    transaction = _load_reset_transaction(
        parent_path,
        storage_parent,
        source_name,
        deadline,
    )
    if transaction is None or transaction.receipt:
        raise MatterStorageContractError(
            "Matter-Resettransaktion wurde nicht atomar sichtbar",
            "QUARANTINE_COLLISION",
        )
    return transaction


def _close_reset_transaction(transaction: _ResetTransaction | None) -> None:
    if transaction is None:
        return
    _close_bound_regular(transaction.marker)
    if transaction.quarantine_descriptor >= 0:
        os.close(transaction.quarantine_descriptor)


def _open_transaction_source(
    transaction: _ResetTransaction,
    storage_parent: int,
    root_name: str,
    deadline: float,
) -> int:
    """Bindet die Source an ihrem alten oder bereits quarantänisierten Namen."""

    source_identity = transaction.document["source"]
    if not isinstance(source_identity, dict):
        raise MatterStorageContractError(
            "Matter-Resetmarker verlor seine Source-Identität",
            "QUARANTINE_COLLISION",
        )
    nofollow, directory, cloexec = _required_open_flags()
    flags = os.O_RDONLY | nofollow | directory | cloexec
    parent_source = -1
    quarantine_source = -1
    try:
        try:
            parent_named = os.stat(
                root_name,
                dir_fd=storage_parent,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            parent_named = None
        if parent_named is not None and _transaction_identity_matches(
            parent_named,
            _mount_id(storage_parent),
            source_identity,
        ):
            if not stat.S_ISDIR(parent_named.st_mode):
                raise MatterStorageContractError(
                    "Matter-Reset-Source wechselte den Dateityp",
                    "QUARANTINE_COLLISION",
                )
            parent_source = os.open(root_name, flags, dir_fd=storage_parent)
            opened = os.fstat(parent_source)
            if not _transaction_identity_matches(
                opened,
                _mount_id(parent_source),
                source_identity,
            ):
                raise MatterStorageContractError(
                    "Matter-Reset-Source driftete am Ursprungsnamen",
                    "QUARANTINE_COLLISION",
                )

        if transaction.quarantine_descriptor >= 0:
            try:
                quarantine_named = os.stat(
                    "storage",
                    dir_fd=transaction.quarantine_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                quarantine_named = None
            if quarantine_named is not None:
                if not stat.S_ISDIR(quarantine_named.st_mode) \
                        or not _transaction_identity_matches(
                            quarantine_named,
                            _mount_id(transaction.quarantine_descriptor),
                            source_identity,
                        ):
                    raise MatterStorageContractError(
                        "Matter-Quarantäne enthält eine fremde Source",
                        "QUARANTINE_COLLISION",
                    )
                quarantine_source = os.open(
                    "storage",
                    flags,
                    dir_fd=transaction.quarantine_descriptor,
                )
                if not _transaction_identity_matches(
                    os.fstat(quarantine_source),
                    _mount_id(quarantine_source),
                    source_identity,
                ):
                    raise MatterStorageContractError(
                        "Matter-Source driftete innerhalb der Quarantäne",
                        "QUARANTINE_COLLISION",
                    )
        if parent_source >= 0 and quarantine_source >= 0:
            raise MatterStorageContractError(
                "Matter-Reset-Source ist unter zwei Namen sichtbar",
                "QUARANTINE_COLLISION",
            )
        if transaction.receipt and (parent_source >= 0 or quarantine_source >= 0):
            raise MatterStorageContractError(
                "Terminales Matter-Receipt besitzt weiterhin eine Source",
                "QUARANTINE_COLLISION",
            )
        if parent_source >= 0:
            if transaction.quarantine_descriptor < 0 \
                    or set(os.listdir(transaction.quarantine_descriptor)) != {
                        RESET_TRANSACTION_MARKER_NAME
                    }:
                raise MatterStorageContractError(
                    "Matter-Quarantäne ist vor dem Source-Publish nicht leer markiert",
                    "QUARANTINE_COLLISION",
                )
            _scan_mount_tree(parent_source, deadline)
            _check_reset_deadline(deadline)
            _rename_noreplace(
                root_name,
                "storage",
                source_parent=storage_parent,
                target_parent=transaction.quarantine_descriptor,
                collision_code="QUARANTINE_COLLISION",
            )
            os.fsync(transaction.quarantine_descriptor)
            os.fsync(storage_parent)
            _check_reset_deadline(deadline)
            moved = os.stat(
                "storage",
                dir_fd=transaction.quarantine_descriptor,
                follow_symlinks=False,
            )
            if not _transaction_identity_matches(
                moved,
                _mount_id(transaction.quarantine_descriptor),
                source_identity,
            ):
                raise MatterStorageContractError(
                    "Matter-Source driftete beim atomaren Quarantäne-Publish",
                    "QUARANTINE_COLLISION",
                )
            result = parent_source
            parent_source = -1
            return result
        if quarantine_source >= 0:
            _scan_mount_tree(quarantine_source, deadline)
            result = quarantine_source
            quarantine_source = -1
            return result
        return -1
    finally:
        if parent_source >= 0:
            os.close(parent_source)
        if quarantine_source >= 0:
            os.close(quarantine_source)


def _ensure_transaction_replacement_root(
    storage_parent: int,
    root_name: str,
    *,
    uid: int,
    gid: int,
    source_identity: object,
    deadline: float,
) -> int:
    """Erzeugt oder vervollständigt ausschließlich den leeren Ersatzroot."""

    try:
        named = os.stat(root_name, dir_fd=storage_parent, follow_symlinks=False)
    except FileNotFoundError:
        return _create_storage_root(
            storage_parent,
            root_name,
            uid=uid,
            gid=gid,
            deadline=deadline,
        )
    if _transaction_identity_matches(
        named,
        _mount_id(storage_parent),
        source_identity,
    ):
        raise MatterStorageContractError(
            "Matter-Source blieb am Storage-Namen erreichbar",
            "QUARANTINE_COLLISION",
        )
    if not stat.S_ISDIR(named.st_mode) or stat.S_ISLNK(named.st_mode):
        raise MatterStorageContractError(
            "Matter-Ersatzroot kollidiert mit einem fremden Knoten",
            "BINDING",
        )
    nofollow, directory, cloexec = _required_open_flags()
    descriptor = os.open(
        root_name,
        os.O_RDONLY | nofollow | directory | cloexec,
        dir_fd=storage_parent,
    )
    try:
        opened = os.fstat(descriptor)
        parent = os.fstat(storage_parent)
        if (
            (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or opened.st_dev != parent.st_dev
            or _mount_id(descriptor) != _mount_id(storage_parent)
            or tuple(os.listdir(descriptor))
        ):
            raise MatterStorageContractError(
                "Matter-Ersatzroot ist nicht leer mountgebunden",
                "BINDING",
            )
        _check_reset_deadline(deadline)
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, 0o700)
        _check_reset_deadline(deadline)
        final = os.fstat(descriptor)
        rebound = os.stat(root_name, dir_fd=storage_parent, follow_symlinks=False)
        if (
            (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino)
            or (rebound.st_dev, rebound.st_ino) != (opened.st_dev, opened.st_ino)
            or final.st_uid != uid
            or final.st_gid != gid
            or stat.S_IMODE(final.st_mode) != 0o700
            or tuple(os.listdir(descriptor))
        ):
            raise MatterStorageContractError(
                "Matter-Ersatzroot driftete während der Reparatur",
                "BINDING",
            )
        os.fsync(descriptor)
        os.fsync(storage_parent)
        _check_reset_deadline(deadline)
        result = descriptor
        descriptor = -1
        return result
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _clear_transaction_source(
    transaction: _ResetTransaction,
    source_descriptor: int,
    deadline: float,
) -> None:
    if source_descriptor < 0:
        if transaction.receipt:
            return
        if transaction.quarantine_descriptor < 0 \
                or set(os.listdir(transaction.quarantine_descriptor)) != {
                    RESET_TRANSACTION_MARKER_NAME
                }:
            raise MatterStorageContractError(
                "Matter-Quarantäne verlor ihre Source ohne Endzustand",
                "QUARANTINE_COLLISION",
            )
        return
    if transaction.receipt or transaction.quarantine_descriptor < 0:
        raise MatterStorageContractError(
            "Matter-Source ist ohne aktive Quarantäne sichtbar",
            "QUARANTINE_COLLISION",
        )
    _clear_private_tree(source_descriptor, _mount_id(source_descriptor), deadline)
    if tuple(os.listdir(source_descriptor)):
        raise MatterStorageContractError(
            "Matter-Source blieb nach dem Leeren befüllt",
            "CLEAR",
        )
    source_identity = transaction.document["source"]
    moved = os.stat(
        "storage",
        dir_fd=transaction.quarantine_descriptor,
        follow_symlinks=False,
    )
    if not _transaction_identity_matches(
        moved,
        _mount_id(transaction.quarantine_descriptor),
        source_identity,
    ):
        raise MatterStorageContractError(
            "Matter-Source driftete vor dem terminalen Entfernen",
            "QUARANTINE_COLLISION",
        )
    _check_reset_deadline(deadline)
    os.rmdir("storage", dir_fd=transaction.quarantine_descriptor)
    _check_reset_deadline(deadline)
    os.fsync(transaction.quarantine_descriptor)
    if set(os.listdir(transaction.quarantine_descriptor)) != {
        RESET_TRANSACTION_MARKER_NAME
    }:
        raise MatterStorageContractError(
            "Matter-Quarantäne besitzt keinen exklusiven Endmarker",
            "QUARANTINE_COLLISION",
        )


def _finish_reset_transaction(
    transaction: _ResetTransaction,
    parent_path: str,
    storage_parent: int,
    root_name: str,
    deadline: float | None,
) -> None:
    """Entfernt Quarantäne und danach das gebundene Parent-Receipt als Letztes."""

    if not transaction.receipt:
        if transaction.quarantine_descriptor < 0 \
                or set(os.listdir(transaction.quarantine_descriptor)) != {
                    RESET_TRANSACTION_MARKER_NAME
                }:
            raise MatterStorageContractError(
                "Matter-Quarantäne ist nicht terminal markiert",
                "QUARANTINE_COLLISION",
            )
        _verify_bound_regular(transaction.marker)
        marker_identity = (
            transaction.marker.device,
            transaction.marker.inode,
        )
        if deadline is not None:
            _check_reset_deadline(deadline)
        try:
            os.link(
                RESET_TRANSACTION_MARKER_NAME,
                RESET_TRANSACTION_MARKER_NAME,
                src_dir_fd=transaction.quarantine_descriptor,
                dst_dir_fd=storage_parent,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise MatterStorageContractError(
                "Matter-Endreceipt kollidiert mit einem vorhandenen Knoten",
                "QUARANTINE_COLLISION",
            ) from exc
        except OSError as exc:
            raise MatterStorageContractError(
                "Matter-Endreceipt konnte nicht dauerhaft verlinkt werden",
                "FOREIGN_MOUNT" if exc.errno == errno.EXDEV else "BINDING",
            ) from exc
        _verify_bound_regular(
            transaction.marker,
            allowed_nlinks=frozenset({2}),
        )
        receipt_named = os.stat(
            RESET_TRANSACTION_MARKER_NAME,
            dir_fd=storage_parent,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(receipt_named.st_mode)
            or receipt_named.st_nlink != 2
            or (receipt_named.st_dev, receipt_named.st_ino) != marker_identity
            or receipt_named.st_uid != transaction.marker.uid
            or receipt_named.st_gid != transaction.marker.gid
            or stat.S_IMODE(receipt_named.st_mode) != transaction.marker.mode
            or receipt_named.st_size != transaction.marker.size
        ):
            raise MatterStorageContractError(
                "Matter-Dual-Receipt bindet nicht exakt denselben Marker",
                "QUARANTINE_COLLISION",
            )
        os.fsync(storage_parent)
        if deadline is not None:
            _check_reset_deadline(deadline)
        _verify_bound_regular(
            transaction.marker,
            allowed_nlinks=frozenset({2}),
        )
        os.unlink(
            RESET_TRANSACTION_MARKER_NAME,
            dir_fd=transaction.quarantine_descriptor,
        )
        os.fsync(transaction.quarantine_descriptor)
        if deadline is not None:
            _check_reset_deadline(deadline)
        try:
            os.stat(
                RESET_TRANSACTION_MARKER_NAME,
                dir_fd=transaction.quarantine_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise MatterStorageContractError(
                "Interner Matter-Resetmarker blieb nach dem WAL-Commit erreichbar",
                "QUARANTINE_COLLISION",
            )
        receipt_named = os.stat(
            RESET_TRANSACTION_MARKER_NAME,
            dir_fd=storage_parent,
            follow_symlinks=False,
        )
        if receipt_named.st_nlink != 1 \
                or (receipt_named.st_dev, receipt_named.st_ino) != marker_identity:
            raise MatterStorageContractError(
                "Matter-Endreceipt driftete nach dem WAL-Commit",
                "QUARANTINE_COLLISION",
            )
        _close_bound_regular(transaction.marker)
        marker, document = _bind_transaction_marker(
            os.path.join(parent_path, RESET_TRANSACTION_MARKER_NAME),
            source_name=root_name,
            required=True,
        )
        if document != transaction.document \
                or (marker.device, marker.inode) != marker_identity:
            _close_bound_regular(marker)
            raise MatterStorageContractError(
                "Matter-Endreceipt driftete beim atomaren Publish",
                "QUARANTINE_COLLISION",
            )
        transaction.marker = marker
        transaction.receipt = True

    if transaction.quarantine_descriptor >= 0:
        quarantine = os.fstat(transaction.quarantine_descriptor)
        named = os.stat(
            RESET_QUARANTINE_NAME,
            dir_fd=storage_parent,
            follow_symlinks=False,
        )
        if (
            (named.st_dev, named.st_ino) != (quarantine.st_dev, quarantine.st_ino)
            or tuple(os.listdir(transaction.quarantine_descriptor))
        ):
            raise MatterStorageContractError(
                "Matter-Endquarantäne driftete vor dem Entfernen",
                "QUARANTINE_COLLISION",
            )
        if deadline is not None:
            _check_reset_deadline(deadline)
        os.rmdir(RESET_QUARANTINE_NAME, dir_fd=storage_parent)
        if deadline is not None:
            _check_reset_deadline(deadline)
        os.fsync(storage_parent)
        os.close(transaction.quarantine_descriptor)
        transaction.quarantine_descriptor = -1

    # Das Receipt ist der letzte Knoten der privaten Quarantänetransaktion.
    _verify_bound_regular(transaction.marker)
    _unlink_bound_regular(transaction.marker)


def consume_reset_request(
    storage_path: str,
    request_path: str,
    pairing_path: str,
    *,
    repair_request_path: str | None = None,
    uid: int,
    gid: int,
    expected_identity: str | None,
    max_entries: int,
    max_depth: int,
) -> bool | str:
    """Verbraucht feste Resetanforderungen erst nach vollständigem Preflight."""

    if max_entries < 1 or max_entries > 4096:
        raise MatterStorageContractError("Matter-Storage-Eintragslimit ist ungültig")
    if max_depth < 1 or max_depth > 128:
        raise MatterStorageContractError("Matter-Storage-Tiefenlimit ist ungültig")
    deadline = time.monotonic() + RESET_TRANSACTION_SECONDS

    request: _BoundRegular | None = None
    repair_request: _BoundRegular | None = None
    storage_parent = -1
    replacement_root = -1
    source_descriptor = -1
    transaction: _ResetTransaction | None = None
    quarantined = False
    pairing: _BoundNode | None = None
    try:
        if repair_request_path is not None:
            try:
                repair_request = _bind_regular(
                    repair_request_path,
                    uid=uid,
                    gid=gid,
                    mode=0o660,
                    payload=RESET_REPAIR_REQUEST_PAYLOAD,
                    required=False,
                )
            except (MatterStorageContractError, OSError) as exc:
                raise MatterStorageContractError(
                    "Matter-Reset-Reparaturanforderung kollidiert mit fremdem Bestand",
                    "REQUEST_COLLISION",
                ) from exc
            if repair_request.descriptor < 0:
                _close_bound_regular(repair_request)
                repair_request = None
        try:
            request = _bind_regular(
                request_path,
                uid=uid,
                gid=gid,
                mode=0o660,
                payload=RESET_REQUEST_PAYLOAD,
                required=False,
            )
        except (MatterStorageContractError, OSError) as exc:
            raise MatterStorageContractError(
                "Matter-Reset-Anforderung kollidiert mit fremdem Bestand",
                "REQUEST_COLLISION",
            ) from exc
        if request is not None and request.descriptor < 0:
            _close_bound_regular(request)
            request = None
        if request is None and repair_request is None:
            return False

        path = os.path.normpath(str(storage_path))
        if not path.startswith("/") or path != str(storage_path):
            raise MatterStorageContractError(
                "Matter-Storage-Pfad ist nicht kanonisch absolut",
                "BINDING",
            )
        parent_path, root_name = os.path.split(path)
        storage_parent = _open_absolute_directory(parent_path)
        parent_metadata = os.fstat(storage_parent)
        parent_mount_id = _mount_id(storage_parent)

        pairing = _bind_untrusted_node(
            pairing_path,
            code="PAIRING_BINDING",
            required=False,
            allow_regular_hardlink=True,
        )
        _verify_bound_node(pairing, code="PAIRING_BINDING")
        if request is not None:
            _verify_bound_regular(request)
        if repair_request is not None:
            _verify_bound_regular(repair_request)
        _check_reset_deadline(deadline)

        transaction = _load_reset_transaction(
            parent_path,
            storage_parent,
            root_name,
            deadline,
        )
        try:
            root_named = os.stat(root_name, dir_fd=storage_parent, follow_symlinks=False)
        except FileNotFoundError:
            root_named = None
        if transaction is not None and expected_identity is not None:
            if root_named is None or not stat.S_ISDIR(root_named.st_mode) \
                    or stat.S_ISLNK(root_named.st_mode):
                raise MatterStorageContractError(
                    "Matter-Storage-Rootidentität fehlt vor der Wiederaufnahme",
                    "BINDING",
                )
            nofollow, directory, cloexec = _required_open_flags()
            current_root = os.open(
                root_name,
                os.O_RDONLY | nofollow | directory | cloexec,
                dir_fd=storage_parent,
            )
            try:
                current_meta = os.fstat(current_root)
                current_mount_id = _mount_id(current_root)
                if (
                    (current_meta.st_dev, current_meta.st_ino)
                    != (root_named.st_dev, root_named.st_ino)
                    or current_meta.st_dev != parent_metadata.st_dev
                    or current_mount_id != parent_mount_id
                    or current_meta.st_uid != uid
                    or current_meta.st_gid != gid
                    or stat.S_IMODE(current_meta.st_mode) != 0o700
                    or _identity_token(
                        parent_metadata,
                        parent_mount_id,
                        current_meta,
                        current_mount_id,
                    ) != _validate_expected_identity(expected_identity)
                ):
                    raise MatterStorageContractError(
                        "Matter-Storage-Rootidentität driftete vor der Wiederaufnahme",
                        "BINDING",
                    )
            finally:
                os.close(current_root)
        if transaction is None and root_named is not None \
                and stat.S_ISDIR(root_named.st_mode) \
                and not stat.S_ISLNK(root_named.st_mode):
            nofollow, directory, cloexec = _required_open_flags()
            source_descriptor = os.open(
                root_name,
                os.O_RDONLY | nofollow | directory | cloexec,
                dir_fd=storage_parent,
            )
            root_metadata = os.fstat(source_descriptor)
            root_mount_id = _mount_id(source_descriptor)
            if (
                (root_metadata.st_dev, root_metadata.st_ino)
                != (root_named.st_dev, root_named.st_ino)
                or root_metadata.st_dev != parent_metadata.st_dev
                or root_mount_id != parent_mount_id
                or root_metadata.st_uid != uid
                or root_metadata.st_gid != gid
                or stat.S_IMODE(root_metadata.st_mode) != 0o700
            ):
                raise MatterStorageContractError(
                    "Matter-Storage-Root besitzt keinen exakt gebundenen Eigentümer- und Modusvertrag",
                    (
                        "FOREIGN_MOUNT"
                        if root_metadata.st_dev != parent_metadata.st_dev
                        or root_mount_id != parent_mount_id
                        else "BINDING"
                    ),
                )
            identity = _identity_token(
                parent_metadata,
                parent_mount_id,
                root_metadata,
                root_mount_id,
            )
            if expected_identity is not None \
                    and identity != _validate_expected_identity(expected_identity):
                raise MatterStorageContractError(
                    "Matter-Storage-Rootidentität driftete vor dem Reset",
                    "BINDING",
                )
            # Jeder Verzeichnisreset wechselt vor der ersten Inhaltsmutation
            # zusammen mit seinem root-privaten Marker atomar in den festen
            # Quarantänenamespace. Vorher wird kein Storage-Inhalt verändert.
            records = _scan_mount_tree(
                source_descriptor,
                deadline,
                max_entries=max_entries,
                max_depth=max_depth,
                regular_tree_only=True,
            )
            _apply_private_tree_contract(
                source_descriptor,
                records,
                uid=uid,
                gid=gid,
                harden=False,
                deadline=deadline,
            )
        elif transaction is None and root_named is not None:
            raise MatterStorageContractError(
                "Matter-Storage-Root ist kein echtes Verzeichnis",
                "BINDING",
            )

        # Der vollständige Baum, die exakte Ramdisk-Datei und die persistente
        # Anforderung sind gebunden, bevor die erste Löschung erfolgt.
        _verify_bound_node(pairing, code="PAIRING_BINDING")
        if request is not None:
            _verify_bound_regular(request)
        if repair_request is not None:
            _verify_bound_regular(repair_request)
        _check_reset_deadline(deadline)

        if transaction is None and source_descriptor >= 0:
            transaction = _create_reset_transaction(
                parent_path,
                storage_parent,
                root_name,
                source_descriptor,
                deadline,
            )
            quarantined = True
            os.close(source_descriptor)
            source_descriptor = -1
        elif transaction is not None:
            quarantined = True

        if transaction is not None:
            source_descriptor = _open_transaction_source(
                transaction,
                storage_parent,
                root_name,
                deadline,
            )
            replacement_root = _ensure_transaction_replacement_root(
                storage_parent,
                root_name,
                uid=uid,
                gid=gid,
                source_identity=transaction.document["source"],
                deadline=deadline,
            )
            _clear_transaction_source(transaction, source_descriptor, deadline)
            if source_descriptor >= 0:
                os.close(source_descriptor)
                source_descriptor = -1
        elif root_named is None:
            replacement_root = _create_storage_root(
                storage_parent,
                root_name,
                uid=uid,
                gid=gid,
                deadline=deadline,
            )
        else:
            raise MatterStorageContractError(
                "Matter-Storage besitzt keinen gebundenen Resetpfad",
                "BINDING",
            )
        final_root_descriptor = replacement_root

        final_root = os.stat(root_name, dir_fd=storage_parent, follow_symlinks=False)
        opened_root = os.fstat(final_root_descriptor)
        if (
            (final_root.st_dev, final_root.st_ino)
            != (opened_root.st_dev, opened_root.st_ino)
            or not stat.S_ISDIR(final_root.st_mode)
            or opened_root.st_uid != uid
            or opened_root.st_gid != gid
            or stat.S_IMODE(opened_root.st_mode) != 0o700
            or tuple(os.listdir(final_root_descriptor))
        ):
            raise MatterStorageContractError(
                "Matter-Storage-Root driftete nach dem Reset",
                "CLEAR",
            )

        # Pairing und persistente Anforderungen bleiben bis zum vollständig
        # geleerten Storage und der letzten Pre-Commit-Deadline erhalten.
        _check_reset_deadline(deadline)
        if pairing.descriptor >= 0:
            _unlink_bound_node(
                pairing,
                code="PAIRING_CLEAR",
                deadline=None,
            )
        else:
            _verify_bound_node(pairing, code="PAIRING_BINDING")

        # Nach der letzten Pre-Commit-Deadline darf kein neuer Timeout mehr
        # entstehen: Ein begonnener Pairing-Commit konvergiert deterministisch.
        if transaction is not None:
            _finish_reset_transaction(
                transaction,
                parent_path,
                storage_parent,
                root_name,
                None,
            )

        # Commitpunkt: Mindestens eine persistente Anforderung bleibt bis zum
        # bestätigten Storage-, Marker- und Ramdisk-Reset erhalten. Ausschließlich
        # zuvor exakt gebundene E3DC-Anforderungen werden entfernt.
        try:
            if request is not None:
                _unlink_bound_regular(request)
            if repair_request is not None:
                _unlink_bound_regular(repair_request)
        except (MatterStorageContractError, OSError) as exc:
            raise MatterStorageContractError(
                "Matter-Reset-Commit konnte nicht bestätigt werden",
                "COMMIT",
            ) from exc
        return "quarantined" if quarantined else True
    finally:
        _close_bound_node(pairing)
        _close_reset_transaction(transaction)
        if replacement_root >= 0:
            os.close(replacement_root)
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if storage_parent >= 0:
            os.close(storage_parent)
        _close_bound_regular(request)
        _close_bound_regular(repair_request)


def secure_matter_storage(
    storage_path: str,
    *,
    uid: int,
    gid: int,
    harden: bool,
    expected_identity: str | None,
    max_entries: int,
    max_depth: int,
) -> str:
    path = os.path.normpath(str(storage_path))
    if not path.startswith("/") or path != str(storage_path):
        raise MatterStorageContractError(
            "Matter-Storage-Pfad ist nicht kanonisch absolut"
        )
    parent_path, root_name = os.path.split(path)
    if not root_name or root_name in {".", ".."}:
        raise MatterStorageContractError("Matter-Storage-Name ist ungültig")
    if max_entries < 1 or max_entries > 4096:
        raise MatterStorageContractError("Matter-Storage-Eintragslimit ist ungültig")
    if max_depth < 1 or max_depth > 128:
        raise MatterStorageContractError("Matter-Storage-Tiefenlimit ist ungültig")

    parent_descriptor = _open_absolute_directory(parent_path)
    records: list[dict[str, object]] = []
    reopened_parent = -1
    root_descriptor = -1
    try:
        parent_metadata = os.fstat(parent_descriptor)
        parent_mount_id = _mount_id(parent_descriptor)
        root_descriptor = _open_storage_root(
            parent_descriptor,
            root_name,
            create=harden,
        )
        root_metadata = os.fstat(root_descriptor)
        root_mount_id = _mount_id(root_descriptor)
        if (
            root_metadata.st_dev != parent_metadata.st_dev
            or root_mount_id != parent_mount_id
        ):
            raise MatterStorageContractError(
                "Matter-Storage-Root liegt außerhalb des Daten-Volumes"
            )
        identity = _identity_token(
            parent_metadata,
            parent_mount_id,
            root_metadata,
            root_mount_id,
        )
        if expected_identity is not None:
            expected = _validate_expected_identity(expected_identity)
            if identity != expected:
                raise MatterStorageContractError(
                    "Matter-Storage-Rootidentität driftete vor dem Workerstart"
                )
        records = _scan_mount_tree(
            root_descriptor,
            float("inf"),
            max_entries=max_entries,
            max_depth=max_depth,
            regular_tree_only=True,
        )
        _apply_private_tree_contract(
            root_descriptor,
            records,
            uid=uid,
            gid=gid,
            harden=harden,
        )
        # Zweiter vollständiger Pass ist der Commitbeweis nach allen möglichen
        # chown/chmod-Mutationen und hält weiterhin nur wenige FDs gleichzeitig.
        _apply_private_tree_contract(
            root_descriptor,
            records,
            uid=uid,
            gid=gid,
            harden=False,
        )

        named_root = os.stat(
            root_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        bound_root = os.fstat(root_descriptor)
        if (
            (named_root.st_dev, named_root.st_ino)
            != (bound_root.st_dev, bound_root.st_ino)
            or not stat.S_ISDIR(named_root.st_mode)
        ):
            raise MatterStorageContractError(
                "Matter-Storage-Rootname driftete nach der Prüfung"
            )
        reopened_parent = _open_absolute_directory(parent_path)
        rebound_parent = os.fstat(reopened_parent)
        if (
            (rebound_parent.st_dev, rebound_parent.st_ino)
            != (parent_metadata.st_dev, parent_metadata.st_ino)
            or _mount_id(reopened_parent) != parent_mount_id
        ):
            raise MatterStorageContractError(
                "Matter-Storage-Elternpfad driftete nach der Prüfung"
            )
        return _identity_token(
            rebound_parent,
            parent_mount_id,
            bound_root,
            root_mount_id,
        )
    finally:
        if reopened_parent >= 0:
            os.close(reopened_parent)
        if root_descriptor >= 0:
            os.close(root_descriptor)
        os.close(parent_descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Härtet oder verifiziert den privaten Docker-Matter-Storage.",
    )
    parser.add_argument(
        "--mode",
        choices=("harden", "verify", "capabilities", "consume-reset"),
        required=True,
    )
    parser.add_argument("--path", default=DEFAULT_STORAGE_PATH)
    parser.add_argument("--request-path", default=DEFAULT_RESET_REQUEST_PATH)
    parser.add_argument(
        "--repair-request-path",
        default=DEFAULT_RESET_REPAIR_REQUEST_PATH,
    )
    parser.add_argument("--pairing-path", default=DEFAULT_PAIRING_PATH)
    parser.add_argument("--owner", default="www-data")
    parser.add_argument("--group", default="www-data")
    parser.add_argument("--expected-identity")
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.mode == "capabilities":
        print(RESET_CAPABILITY)
        return 0
    try:
        harden = args.mode == "harden"
        if harden and args.expected_identity is not None:
            raise MatterStorageContractError(
                "Härtung akzeptiert keine vorgegebene Rootidentität"
            )
        if args.mode in {"verify", "consume-reset"} \
                and args.expected_identity is None:
            raise MatterStorageContractError(
                "Verifikation und Resetkonsum benötigen die gebundene Rootidentität"
            )
        uid = _resolve_uid(args.owner)
        gid = _resolve_gid(args.group)
        if args.mode == "consume-reset":
            consumed = consume_reset_request(
                args.path,
                args.request_path,
                args.pairing_path,
                repair_request_path=args.repair_request_path,
                uid=uid,
                gid=gid,
                expected_identity=args.expected_identity,
                max_entries=args.max_entries,
                max_depth=args.max_depth,
            )
            if consumed == "quarantined":
                identity = "reset-complete-quarantined"
            else:
                identity = "reset-complete" if consumed else "no-request"
        else:
            identity = secure_matter_storage(
                args.path,
                uid=uid,
                gid=gid,
                harden=harden,
                expected_identity=args.expected_identity,
                max_entries=args.max_entries,
                max_depth=args.max_depth,
            )
    except (MatterStorageContractError, OSError) as exc:
        if args.mode == "consume-reset":
            code = getattr(exc, "code", "CONTRACT")
            if code not in {
                "BINDING",
                "CLEAR",
                "COMMIT",
                "CONTRACT",
                "FOREIGN_MOUNT",
                "HARDLINK",
                "LIMIT",
                "PAIRING_BINDING",
                "PAIRING_CLEAR",
                "PAIRING_HARDLINK",
                "QUARANTINE_COLLISION",
                "REQUEST_COLLISION",
                "REQUEST_INVALID",
                "REQUEST_REPAIR_BLOCKED",
                "TIMEOUT",
            }:
                code = "CONTRACT"
            print(f"MATTER_RESET_ERROR_DOCKER_{code}", file=sys.stderr)
        else:
            print(f"Matter-Storage-Vertrag fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    print(identity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
