#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bindet und härtet den persistenten Docker-Matter-Storage fail-closed."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import grp
import os
import pwd
import stat
import sys


DEFAULT_STORAGE_PATH = "/var/www/html/data/matter-storage"
DEFAULT_MAX_ENTRIES = 512
DEFAULT_MAX_DEPTH = 64
IDENTITY_SCHEMA = "e3dc-matter-storage-v1"


class MatterStorageContractError(RuntimeError):
    """Der persistente Matter-Baum erfüllt den privaten Storagevertrag nicht."""


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


def _required_open_flags() -> tuple[int, int, int]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory:
        raise MatterStorageContractError(
            "O_NOFOLLOW/O_DIRECTORY wird für den Matter-Storage benötigt"
        )
    return nofollow, directory, cloexec


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
                        "Matter-Storage überschreitet das Eintragslimit"
                    )
                child_depth = parent.depth + 1
                if child_depth > max_depth:
                    raise MatterStorageContractError(
                        "Matter-Storage überschreitet das Tiefenlimit"
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
                if not is_directory and not is_regular:
                    raise MatterStorageContractError(
                        "Matter-Storage enthält Symlink oder Sonderdatei"
                    )
                if named.st_dev != root_metadata.st_dev:
                    raise MatterStorageContractError(
                        "Matter-Storage überschreitet eine Dateisystemgrenze"
                    )
                if is_regular and named.st_nlink != 1:
                    raise MatterStorageContractError(
                        "Matter-Storage enthält eine reguläre Datei mit mehreren Hardlinks"
                    )
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
                    ):
                        raise MatterStorageContractError(
                            "Matter-Storage-Eintrag wechselte beim Öffnen"
                        )
                    mount_id = _mount_id(descriptor)
                    identity = (int(opened.st_dev), int(opened.st_ino))
                    if opened.st_dev != root_metadata.st_dev or mount_id != root_mount_id:
                        raise MatterStorageContractError(
                            "Matter-Storage überschreitet eine Dateisystem- oder Mountgrenze"
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
) -> None:
    for record in records:
        opened = os.fstat(record.descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (record.device, record.inode)
            or _mount_id(record.descriptor) != record.mount_id
            or opened.st_uid != uid
            or opened.st_gid != gid
            or stat.S_IMODE(opened.st_mode) != _expected_mode(record)
            or (record.is_directory and not stat.S_ISDIR(opened.st_mode))
            or (not record.is_directory and not stat.S_ISREG(opened.st_mode))
            or (not record.is_directory and opened.st_nlink != 1)
        ):
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
            if (
                (named.st_dev, named.st_ino) != (child.device, child.inode)
                or named.st_uid != uid
                or named.st_gid != gid
                or stat.S_IMODE(named.st_mode) != _expected_mode(child)
                or (child.is_directory and not stat.S_ISDIR(named.st_mode))
                or (not child.is_directory and not stat.S_ISREG(named.st_mode))
                or (not child.is_directory and named.st_nlink != 1)
            ):
                raise MatterStorageContractError(
                    f"Matter-Storage-Name driftete: {child.relative_path}"
                )


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
    records: list[_BoundEntry] = []
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
        transferred_root_descriptor = root_descriptor
        root_descriptor = -1
        records = _bind_tree(
            transferred_root_descriptor,
            max_entries=max_entries,
            max_depth=max_depth,
        )
        if harden:
            _harden_bound_tree(records, uid=uid, gid=gid)
        _verify_bound_tree(records, uid=uid, gid=gid)

        named_root = os.stat(
            root_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        bound_root = os.fstat(records[0].descriptor)
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
        for record in reversed(records):
            os.close(record.descriptor)
        if root_descriptor >= 0:
            os.close(root_descriptor)
        os.close(parent_descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Härtet oder verifiziert den privaten Docker-Matter-Storage.",
    )
    parser.add_argument("--mode", choices=("harden", "verify"), required=True)
    parser.add_argument("--path", default=DEFAULT_STORAGE_PATH)
    parser.add_argument("--owner", default="www-data")
    parser.add_argument("--group", default="www-data")
    parser.add_argument("--expected-identity")
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        harden = args.mode == "harden"
        if harden and args.expected_identity is not None:
            raise MatterStorageContractError(
                "Härtung akzeptiert keine vorgegebene Rootidentität"
            )
        if not harden and args.expected_identity is None:
            raise MatterStorageContractError(
                "Verifikation benötigt die gebundene Rootidentität"
            )
        uid = _resolve_uid(args.owner)
        gid = _resolve_gid(args.group)
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
        print(f"Matter-Storage-Vertrag fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    print(identity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
