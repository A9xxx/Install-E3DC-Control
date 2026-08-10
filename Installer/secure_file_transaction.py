"""Enger, nofollow-gebundener Dateivertrag für Installer-Transaktionen.

Die Helfer in diesem Modul arbeiten ausschließlich mit absoluten Pfaden. Jede
Elternkette wird komponentenweise per ``openat``/``O_NOFOLLOW`` gebunden. Neue
Bytes entstehen in einem privaten root-kontrollierten Staging-Verzeichnis auf
demselben Dateisystem und werden anschließend per dirfd-gebundenem Rename
projiziert. Damit existiert kein austauschbarer Tempname im beschreibbaren
Zielverzeichnis.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
import secrets
import stat
from typing import Iterable, Mapping


DEFAULT_MAX_FILE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_TREE_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_TREE_ENTRIES = 10000
_STAGING_DIRECTORY_NAME = ".e3dc-control-transactions"
_LOCK_ROOT = "/run/lock"


class SecureFileTransactionError(RuntimeError):
    """Ein Datei- oder Namensvertrag konnte nicht eindeutig bewiesen werden."""


def _normalise_absolute(path: str | os.PathLike[str]) -> str:
    value = os.fspath(path)
    if not value or "\x00" in value or not os.path.isabs(value):
        raise SecureFileTransactionError("Transaktionspfad ist nicht absolut")
    normalised = os.path.normpath(value)
    if normalised != value or normalised == "/":
        raise SecureFileTransactionError("Transaktionspfad ist nicht kanonisch")
    return normalised


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        stat.S_IMODE(metadata.st_mode),
    )


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise SecureFileTransactionError(
            "O_NOFOLLOW/O_DIRECTORY wird für sichere Installerpfade benötigt"
        )
    return (
        os.O_RDONLY
        | nofollow
        | directory
        | getattr(os, "O_CLOEXEC", 0)
    )


def open_bound_directory(path: str | os.PathLike[str]) -> tuple[int, tuple[int, ...]]:
    """Öffnet eine absolute Verzeichniskette ohne einen Symlink zu verfolgen."""

    value = os.fspath(path)
    if not value or "\x00" in value or not os.path.isabs(value):
        raise SecureFileTransactionError("Verzeichnispfad ist nicht absolut")
    normalised = os.path.normpath(value)
    if normalised != value:
        raise SecureFileTransactionError("Verzeichnispfad ist nicht kanonisch")

    flags = _directory_flags()
    descriptor = os.open("/", flags)
    try:
        for component in (part for part in normalised.split(os.sep) if part):
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise SecureFileTransactionError("Gebundener Pfad ist kein Verzeichnis")
        return descriptor, _directory_identity(metadata)
    except Exception:
        os.close(descriptor)
        raise


def _assert_directory_path_bound(path: str, expected: tuple[int, ...]) -> None:
    descriptor, current = open_bound_directory(path)
    try:
        if current[:2] != expected[:2]:
            raise SecureFileTransactionError(
                "Gebundenes Elternverzeichnis wurde ausgetauscht"
            )
    finally:
        os.close(descriptor)


def _read_descriptor(
    descriptor: int,
    *,
    size: int,
    max_bytes: int,
) -> bytes:
    if size < 0 or size > int(max_bytes):
        raise SecureFileTransactionError("Dateigröße liegt außerhalb des Vertrags")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = int(max_bytes) + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) != size or len(payload) > int(max_bytes):
        raise SecureFileTransactionError("Datei änderte ihre Größe beim Lesen")
    return payload


def _missing_snapshot(path: str, parent_identity: tuple[int, ...]) -> dict[str, object]:
    return {
        "schema": "e3dc_bound_file_v1",
        "path": path,
        "parent_path": os.path.dirname(path),
        "parent_identity": parent_identity,
        "exists": False,
        "kind": "missing",
        "identity": None,
        "payload": None,
        "sha256": None,
        "uid": None,
        "gid": None,
        "mode": None,
    }


def _snapshot_regular_from_parent(
    path: str,
    parent_descriptor: int,
    parent_identity: tuple[int, ...],
    *,
    expected_uid: int | None,
    expected_gid: int | None,
    max_bytes: int,
) -> dict[str, object]:
    name = os.path.basename(path)
    named_before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(named_before.st_mode)
        or named_before.st_nlink != 1
        or named_before.st_size < 0
        or named_before.st_size > int(max_bytes)
        or (expected_uid is not None and named_before.st_uid != int(expected_uid))
        or (expected_gid is not None and named_before.st_gid != int(expected_gid))
    ):
        raise SecureFileTransactionError(
            "Datei ist keine eindeutige reguläre Datei des erwarteten Eigentümers"
        )

    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_descriptor,
    )
    try:
        opened_before = os.fstat(descriptor)
        identity = _file_identity(opened_before)
        if identity != _file_identity(named_before):
            raise SecureFileTransactionError("Datei wechselte beim sicheren Öffnen")
        payload = _read_descriptor(
            descriptor,
            size=opened_before.st_size,
            max_bytes=max_bytes,
        )
        opened_after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            _file_identity(opened_after) != identity
            or _file_identity(named_after) != identity
        ):
            raise SecureFileTransactionError("Datei driftete während des Lesens")
    finally:
        os.close(descriptor)

    if _directory_identity(os.fstat(parent_descriptor))[:2] != parent_identity[:2]:
        raise SecureFileTransactionError("Elternverzeichnis driftete während des Lesens")
    return {
        "schema": "e3dc_bound_file_v1",
        "path": path,
        "parent_path": os.path.dirname(path),
        "parent_identity": parent_identity,
        "exists": True,
        "kind": "regular",
        "identity": identity,
        "payload": payload,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "uid": opened_after.st_uid,
        "gid": opened_after.st_gid,
        "mode": stat.S_IMODE(opened_after.st_mode),
    }


def snapshot_bound_file(
    path: str | os.PathLike[str],
    *,
    allow_missing: bool = False,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> dict[str, object]:
    """Bindet Elterninode, Datei-Inode, Metadaten, Bytes und SHA-256."""

    target = _normalise_absolute(path)
    parent_path = os.path.dirname(target)
    parent_descriptor, parent_identity = open_bound_directory(parent_path)
    try:
        try:
            result = _snapshot_regular_from_parent(
                target,
                parent_descriptor,
                parent_identity,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                max_bytes=max_bytes,
            )
        except FileNotFoundError:
            if not allow_missing:
                raise
            result = _missing_snapshot(target, parent_identity)
    finally:
        os.close(parent_descriptor)
    _assert_directory_path_bound(parent_path, parent_identity)
    return result


def read_bound_regular_file(
    path: str | os.PathLike[str],
    *,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> dict[str, object]:
    return snapshot_bound_file(
        path,
        allow_missing=False,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        max_bytes=max_bytes,
    )


def _entry_token_from_parent(
    path: str,
    parent_descriptor: int,
    parent_identity: tuple[int, ...],
    *,
    allow_symlink: bool,
    max_bytes: int,
) -> dict[str, object]:
    name = os.path.basename(path)
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return _missing_snapshot(path, parent_identity)
    if stat.S_ISLNK(metadata.st_mode):
        if not allow_symlink:
            raise SecureFileTransactionError("Zieldatei ist ein Symlink")
        link_target = os.readlink(name, dir_fd=parent_descriptor)
        return {
            "schema": "e3dc_bound_file_v1",
            "path": path,
            "parent_path": os.path.dirname(path),
            "parent_identity": parent_identity,
            "exists": True,
            "kind": "symlink",
            "identity": _file_identity(metadata),
            "payload": link_target.encode("utf-8", "surrogateescape"),
            "sha256": hashlib.sha256(
                link_target.encode("utf-8", "surrogateescape")
            ).hexdigest(),
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
            "mode": stat.S_IMODE(metadata.st_mode),
        }
    return _snapshot_regular_from_parent(
        path,
        parent_descriptor,
        parent_identity,
        expected_uid=None,
        expected_gid=None,
        max_bytes=max_bytes,
    )


def _same_snapshot(
    actual: Mapping[str, object],
    expected: Mapping[str, object],
    *,
    exact_metadata: bool = True,
) -> bool:
    if (
        actual.get("path") != expected.get("path")
        or bool(actual.get("exists")) != bool(expected.get("exists"))
        or actual.get("kind") != expected.get("kind")
    ):
        return False
    actual_parent = tuple(actual.get("parent_identity") or ())
    expected_parent = tuple(expected.get("parent_identity") or ())
    if actual_parent[:2] != expected_parent[:2]:
        return False
    if not actual.get("exists"):
        return True
    actual_identity = tuple(actual.get("identity") or ())
    expected_identity = tuple(expected.get("identity") or ())
    if actual_identity[:2] != expected_identity[:2]:
        return False
    if actual.get("sha256") != expected.get("sha256"):
        return False
    return not exact_metadata or actual_identity == expected_identity


def snapshots_match(
    actual: Mapping[str, object],
    expected: Mapping[str, object],
    *,
    exact_metadata: bool = True,
) -> bool:
    """Öffentlicher Vergleich für gebundene Pre-/Postimages."""

    return _same_snapshot(actual, expected, exact_metadata=exact_metadata)


def _open_staging_directory(
    target_parent_path: str,
    target_parent_identity: tuple[int, ...],
    *,
    staging_root: str | os.PathLike[str] | None,
) -> int:
    target_device = target_parent_identity[0]
    candidates: list[str] = []
    if staging_root is not None:
        candidates.append(os.path.normpath(os.fspath(staging_root)))
    else:
        current = target_parent_path
        while True:
            candidates.append(current)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

    selected_descriptor = -1
    for candidate in candidates:
        if not os.path.isabs(candidate):
            raise SecureFileTransactionError("Staging-Root ist nicht absolut")
        descriptor, identity = open_bound_directory(candidate)
        metadata = os.fstat(descriptor)
        if metadata.st_dev != target_device:
            os.close(descriptor)
            if staging_root is not None:
                raise SecureFileTransactionError(
                    "Staging-Root liegt nicht auf dem Zieldateisystem"
                )
            continue
        if metadata.st_uid == 0 and not (stat.S_IMODE(metadata.st_mode) & 0o022):
            selected_descriptor = descriptor
            break
        os.close(descriptor)

    if selected_descriptor < 0:
        raise SecureFileTransactionError(
            "Kein root-kontrolliertes Staging-Verzeichnis auf dem Zieldateisystem"
        )

    try:
        try:
            os.mkdir(_STAGING_DIRECTORY_NAME, 0o700, dir_fd=selected_descriptor)
            os.fsync(selected_descriptor)
        except FileExistsError:
            pass
        staging_named = os.stat(
            _STAGING_DIRECTORY_NAME,
            dir_fd=selected_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(staging_named.st_mode)
            or staging_named.st_uid != 0
            or staging_named.st_gid != 0
            or stat.S_IMODE(staging_named.st_mode) != 0o700
        ):
            raise SecureFileTransactionError(
                "Privates Staging-Verzeichnis besitzt unsichere Metadaten"
            )
        staging_descriptor = os.open(
            _STAGING_DIRECTORY_NAME,
            _directory_flags(),
            dir_fd=selected_descriptor,
        )
        staging_opened = os.fstat(staging_descriptor)
        if (
            _directory_identity(staging_opened) != _directory_identity(staging_named)
            or staging_opened.st_dev != target_device
        ):
            os.close(staging_descriptor)
            raise SecureFileTransactionError("Staging-Verzeichnis wechselte beim Öffnen")
        return staging_descriptor
    finally:
        os.close(selected_descriptor)


def atomic_write_bound_file(
    path: str | os.PathLike[str],
    payload: bytes,
    *,
    uid: int,
    gid: int,
    mode: int,
    expected_snapshot: Mapping[str, object] | None = None,
    allow_existing_symlink: bool = False,
    max_existing_bytes: int = DEFAULT_MAX_FILE_BYTES,
    staging_root: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Projiziert exakt gebundene Bytes atomar und ohne Zieldereferenzierung."""

    if not isinstance(payload, bytes):
        raise TypeError("Dateinutzdaten müssen bytes sein")
    target = _normalise_absolute(path)
    parent_path = os.path.dirname(target)
    target_name = os.path.basename(target)
    parent_descriptor, parent_identity = open_bound_directory(parent_path)
    staging_descriptor = -1
    stage_descriptor = -1
    stage_name = f"payload-{os.getpid()}-{secrets.token_hex(16)}"
    try:
        if expected_snapshot is not None:
            if str(expected_snapshot.get("path") or "") != target:
                raise SecureFileTransactionError("Preimage gehört zu einem anderen Ziel")
            expected_parent = tuple(expected_snapshot.get("parent_identity") or ())
            if expected_parent[:2] != parent_identity[:2]:
                raise SecureFileTransactionError("Zielelterninode wich vom Preimage ab")

        current = _entry_token_from_parent(
            target,
            parent_descriptor,
            parent_identity,
            allow_symlink=allow_existing_symlink,
            max_bytes=max_existing_bytes,
        )
        if expected_snapshot is not None and not _same_snapshot(
            current,
            expected_snapshot,
            exact_metadata=True,
        ):
            raise SecureFileTransactionError("Zieldatei driftete seit dem Preimage")

        staging_descriptor = _open_staging_directory(
            parent_path,
            parent_identity,
            staging_root=staging_root,
        )
        stage_descriptor = os.open(
            stage_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=staging_descriptor,
        )
        offset = 0
        while offset < len(payload):
            written = os.write(stage_descriptor, payload[offset:])
            if written <= 0:
                raise SecureFileTransactionError("Staging-Datei konnte nicht geschrieben werden")
            offset += written
        os.fsync(stage_descriptor)
        os.fchown(stage_descriptor, int(uid), int(gid))
        os.fchmod(stage_descriptor, int(mode))
        os.fsync(stage_descriptor)
        staged = os.fstat(stage_descriptor)
        named_stage = os.stat(
            stage_name,
            dir_fd=staging_descriptor,
            follow_symlinks=False,
        )
        staged_identity = _file_identity(staged)
        if (
            not stat.S_ISREG(staged.st_mode)
            or staged.st_nlink != 1
            or staged_identity != _file_identity(named_stage)
            or staged.st_uid != int(uid)
            or staged.st_gid != int(gid)
            or stat.S_IMODE(staged.st_mode) != int(mode)
            or staged.st_size != len(payload)
            or hashlib.sha256(
                _read_descriptor(
                    stage_descriptor,
                    size=staged.st_size,
                    max_bytes=max(len(payload), 1),
                )
            ).digest()
            != hashlib.sha256(payload).digest()
        ):
            raise SecureFileTransactionError("Staging-Datei erfüllt den Bytevertrag nicht")

        current_before_commit = _entry_token_from_parent(
            target,
            parent_descriptor,
            parent_identity,
            allow_symlink=allow_existing_symlink,
            max_bytes=max_existing_bytes,
        )
        if not _same_snapshot(current_before_commit, current, exact_metadata=True):
            raise SecureFileTransactionError("Zieldatei driftete vor dem atomaren Commit")
        _assert_directory_path_bound(parent_path, parent_identity)
        os.replace(
            stage_name,
            target_name,
            src_dir_fd=staging_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)

        installed = _snapshot_regular_from_parent(
            target,
            parent_descriptor,
            parent_identity,
            expected_uid=int(uid),
            expected_gid=int(gid),
            max_bytes=max(len(payload), 1),
        )
        if (
            tuple(installed.get("identity") or ())[:2] != staged_identity[:2]
            or installed.get("sha256") != hashlib.sha256(payload).hexdigest()
            or installed.get("mode") != int(mode)
        ):
            raise SecureFileTransactionError("Installierte Datei weicht vom Staging-Inode ab")
        _assert_directory_path_bound(parent_path, parent_identity)
        return installed
    finally:
        if stage_descriptor >= 0:
            os.close(stage_descriptor)
        if staging_descriptor >= 0:
            try:
                os.unlink(stage_name, dir_fd=staging_descriptor)
            except FileNotFoundError:
                pass
            os.close(staging_descriptor)
        os.close(parent_descriptor)


def remove_bound_file(
    snapshot: Mapping[str, object],
    *,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> dict[str, object]:
    """Entfernt nur genau den weiterhin gebundenen regulären Inode."""

    target = _normalise_absolute(str(snapshot.get("path") or ""))
    parent_path = os.path.dirname(target)
    parent_descriptor, parent_identity = open_bound_directory(parent_path)
    try:
        current = _entry_token_from_parent(
            target,
            parent_descriptor,
            parent_identity,
            allow_symlink=False,
            max_bytes=max_bytes,
        )
        if not _same_snapshot(current, snapshot, exact_metadata=False):
            raise SecureFileTransactionError("Datei driftete vor dem Entfernen")
        if current.get("exists"):
            os.unlink(os.path.basename(target), dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        _assert_directory_path_bound(parent_path, parent_identity)
        return _missing_snapshot(target, parent_identity)
    finally:
        os.close(parent_descriptor)


def restore_bound_file(
    previous: Mapping[str, object],
    *,
    expected_current: Mapping[str, object] | None = None,
    staging_root: str | os.PathLike[str] | None = None,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> dict[str, object]:
    """Stellt ein gebundenes reguläres Preimage oder dessen Abwesenheit wieder her."""

    target = str(previous.get("path") or "")
    current = snapshot_bound_file(target, allow_missing=True, max_bytes=max_bytes)
    previous_parent = tuple(previous.get("parent_identity") or ())
    current_parent = tuple(current.get("parent_identity") or ())
    if previous_parent[:2] != current_parent[:2]:
        raise SecureFileTransactionError(
            "Rollback-Zielelternverzeichnis driftete seit dem Preimage"
        )
    if expected_current is not None and not _same_snapshot(
        current,
        expected_current,
        exact_metadata=False,
    ):
        raise SecureFileTransactionError("Rollback-Ziel driftete seit dem Commit")
    if not previous.get("exists"):
        if current.get("exists"):
            return remove_bound_file(current, max_bytes=max_bytes)
        return current
    if previous.get("kind") != "regular" or not isinstance(previous.get("payload"), bytes):
        raise SecureFileTransactionError("Rollback-Preimage ist keine reguläre Datei")
    return atomic_write_bound_file(
        target,
        previous["payload"],
        uid=int(previous["uid"]),
        gid=int(previous["gid"]),
        mode=int(previous["mode"]),
        expected_snapshot=current,
        max_existing_bytes=max_bytes,
        staging_root=staging_root,
    )


def set_bound_file_metadata(
    path: str | os.PathLike[str],
    *,
    uid: int,
    gid: int,
    mode: int,
    expected_snapshot: Mapping[str, object] | None = None,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> dict[str, object]:
    """Ändert Metadaten ausschließlich am fd-gebundenen regulären Zielinode."""

    target = _normalise_absolute(path)
    parent_path = os.path.dirname(target)
    parent_descriptor, parent_identity = open_bound_directory(parent_path)
    descriptor = -1
    try:
        before = _snapshot_regular_from_parent(
            target,
            parent_descriptor,
            parent_identity,
            expected_uid=None,
            expected_gid=None,
            max_bytes=max_bytes,
        )
        if expected_snapshot is not None:
            if str(expected_snapshot.get("path") or "") != target or not _same_snapshot(
                before,
                expected_snapshot,
                exact_metadata=True,
            ):
                raise SecureFileTransactionError(
                    "Metadatenziel driftete seit dem gebundenen Preimage"
                )
        descriptor = os.open(
            os.path.basename(target),
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        if tuple(before.get("identity") or ()) != _file_identity(opened):
            raise SecureFileTransactionError("Metadatenziel wechselte beim Öffnen")
        try:
            os.fchown(descriptor, int(uid), int(gid))
            os.fchmod(descriptor, int(mode))
            os.fsync(descriptor)
            changed = os.fstat(descriptor)
            named = os.stat(
                os.path.basename(target),
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                _file_identity(changed) != _file_identity(named)
                or changed.st_uid != int(uid)
                or changed.st_gid != int(gid)
                or stat.S_IMODE(changed.st_mode) != int(mode)
            ):
                raise SecureFileTransactionError("Metadaten-Readback ist nicht eindeutig")
            result = _snapshot_regular_from_parent(
                target,
                parent_descriptor,
                parent_identity,
                expected_uid=int(uid),
                expected_gid=int(gid),
                max_bytes=max_bytes,
            )
            if result.get("sha256") != before.get("sha256"):
                raise SecureFileTransactionError(
                    "Dateiinhalt driftete während der Metadatenoperation"
                )
            _assert_directory_path_bound(parent_path, parent_identity)
            return result
        except Exception as exc:
            try:
                named = os.stat(
                    os.path.basename(target),
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                opened_now = os.fstat(descriptor)
                if (
                    (opened_now.st_dev, opened_now.st_ino)
                    != tuple(before.get("identity") or ())[:2]
                    or (named.st_dev, named.st_ino)
                    != tuple(before.get("identity") or ())[:2]
                ):
                    raise SecureFileTransactionError(
                        "Metadatenziel driftete während des Rollbacks"
                    )
                os.fchown(descriptor, int(before["uid"]), int(before["gid"]))
                os.fchmod(descriptor, int(before["mode"]))
                os.fsync(descriptor)
                restored = os.fstat(descriptor)
                if (
                    restored.st_uid != int(before["uid"])
                    or restored.st_gid != int(before["gid"])
                    or stat.S_IMODE(restored.st_mode) != int(before["mode"])
                ):
                    raise SecureFileTransactionError(
                        "Metadaten-Rollback blieb unvollständig"
                    )
            except Exception as rollback_exc:
                raise SecureFileTransactionError(
                    f"Metadatenoperation fehlgeschlagen ({exc}); "
                    f"Rollback unvollständig: {rollback_exc}"
                ) from exc
            raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def ensure_bound_directory(
    path: str | os.PathLike[str],
    *,
    uid: int,
    gid: int,
    mode: int,
    expected_identity: tuple[int, ...] | None = None,
    expected_parent_identity: tuple[int, ...] | None = None,
    expected_missing: bool = False,
) -> dict[str, object]:
    """Erstellt oder bindet genau ein Verzeichnis unter einem gebundenen Parent."""

    target = _normalise_absolute(path)
    parent_path = os.path.dirname(target)
    name = os.path.basename(target)
    parent_descriptor, parent_identity = open_bound_directory(parent_path)
    descriptor = -1
    created = False
    created_identity = None
    previous_metadata = None
    try:
        if (
            expected_parent_identity is not None
            and parent_identity[:2] != tuple(expected_parent_identity)[:2]
        ):
            raise SecureFileTransactionError(
                "Verzeichnisparent driftete seit dem gebundenen Preimage"
            )
        try:
            os.mkdir(name, int(mode), dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            if expected_missing:
                raise SecureFileTransactionError(
                    "Fehlendes Verzeichnisziel entstand vor dem Commit"
                )
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(named.st_mode):
            raise SecureFileTransactionError("Verzeichnisziel ist kein echtes Verzeichnis")
        if expected_identity is not None and _directory_identity(named) != tuple(
            expected_identity
        ):
            raise SecureFileTransactionError(
                "Verzeichnisziel driftete seit dem gebundenen Preimage"
            )
        if created:
            created_identity = _directory_identity(named)
        previous_metadata = (named.st_uid, named.st_gid, stat.S_IMODE(named.st_mode))
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if _directory_identity(opened) != _directory_identity(named):
            raise SecureFileTransactionError("Verzeichnisziel wechselte beim Öffnen")
        os.fchown(descriptor, int(uid), int(gid))
        os.fchmod(descriptor, int(mode))
        os.fsync(descriptor)
        changed = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            _directory_identity(changed) != _directory_identity(named_after)
            or changed.st_uid != int(uid)
            or changed.st_gid != int(gid)
            or stat.S_IMODE(changed.st_mode) != int(mode)
        ):
            raise SecureFileTransactionError("Verzeichnisvertrag ist nicht wirksam")
        os.fsync(parent_descriptor)
        _assert_directory_path_bound(parent_path, parent_identity)
        return {
            "path": target,
            "parent_path": parent_path,
            "parent_identity": parent_identity,
            "identity": _directory_identity(changed),
            "created": created,
            "uid": changed.st_uid,
            "gid": changed.st_gid,
            "mode": stat.S_IMODE(changed.st_mode),
        }
    except Exception as exc:
        rollback_error = None
        try:
            if descriptor < 0 and created and created_identity is not None:
                descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
                reopened = os.fstat(descriptor)
                named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
                if (
                    _directory_identity(reopened) != tuple(created_identity)
                    or _directory_identity(named) != tuple(created_identity)
                ):
                    raise SecureFileTransactionError(
                        "Neu erzeugtes Verzeichnis driftete vor dem lokalen Rollback"
                    )
            if descriptor >= 0:
                opened = os.fstat(descriptor)
                named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                    raise SecureFileTransactionError(
                        "Verzeichnis wechselte während des lokalen Rollbacks"
                    )
                if created:
                    if os.listdir(descriptor):
                        raise SecureFileTransactionError(
                            "Neu erzeugtes Verzeichnis ist beim Rollback nicht leer"
                        )
                    os.close(descriptor)
                    descriptor = -1
                    os.rmdir(name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
                elif previous_metadata is not None:
                    os.fchown(
                        descriptor,
                        int(previous_metadata[0]),
                        int(previous_metadata[1]),
                    )
                    os.fchmod(descriptor, int(previous_metadata[2]))
                    os.fsync(descriptor)
                    restored = os.fstat(descriptor)
                    named_restored = os.stat(
                        name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        _directory_identity(restored)
                        != _directory_identity(named_restored)
                        or restored.st_uid != int(previous_metadata[0])
                        or restored.st_gid != int(previous_metadata[1])
                        or stat.S_IMODE(restored.st_mode)
                        != int(previous_metadata[2])
                    ):
                        raise SecureFileTransactionError(
                            "Verzeichnis-Metadatenrollback blieb unvollständig"
                        )
            _assert_directory_path_bound(parent_path, parent_identity)
        except Exception as rollback_exc:
            rollback_error = rollback_exc
        if rollback_error is not None:
            raise SecureFileTransactionError(
                f"Verzeichnisoperation fehlgeschlagen ({exc}); "
                f"lokaler Rollback unvollständig: {rollback_error}"
            ) from exc
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def render_assignment_updates(
    payload: bytes,
    replacements: Mapping[str, str],
) -> bytes:
    """Ersetzt Legacy-``key = value``-Zeilen ohne weitere Dateizugriffe."""

    if not isinstance(payload, bytes):
        raise TypeError("Legacy-Konfiguration muss als bytes gebunden sein")
    bom = payload.startswith(b"\xef\xbb\xbf")
    text = payload.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    pending = {str(key): str(value) for key, value in replacements.items()}
    rendered: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        replacement_key = next(
            (
                key
                for key in pending
                if stripped.startswith(key + " ") or stripped.startswith(key + "=")
            ),
            None,
        )
        if replacement_key is None:
            rendered.append(line)
            continue
        line_ending = "\r\n" if line.endswith("\r\n") else "\n"
        rendered.append(pending[replacement_key] + line_ending)
        seen.add(replacement_key)
    if rendered and not rendered[-1].endswith(("\n", "\r")):
        rendered[-1] += newline
    for key, replacement in pending.items():
        if key not in seen:
            rendered.append(replacement + newline)
    result = "".join(rendered).encode("utf-8")
    return (b"\xef\xbb\xbf" + result) if bom else result


def _missing_tree_snapshot(
    path: str,
    parent_identity: tuple[int, ...] = (),
) -> dict[str, object]:
    return {
        "schema": "e3dc_bound_tree_v1",
        "path": path,
        "parent_path": os.path.dirname(path),
        "parent_identity": parent_identity,
        "exists": False,
        "root_identity": None,
        "root_uid": None,
        "root_gid": None,
        "root_mode": None,
        "directories": {},
        "files": {},
        "total_bytes": 0,
    }


def snapshot_bound_regular_tree(
    path: str | os.PathLike[str],
    *,
    allow_missing: bool = False,
    exclude_top_level: Iterable[str] = (),
    expected_uid: int | None = None,
    require_owner_only_write: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TREE_BYTES,
    max_entries: int = DEFAULT_MAX_TREE_ENTRIES,
) -> dict[str, object]:
    """Bindet einen reinen Verzeichnis-/Dateibaum ohne Symlinks oder Mounts."""

    root = _normalise_absolute(path)
    excluded = frozenset(str(name) for name in exclude_top_level)
    if any(not name or name in {".", ".."} or os.sep in name for name in excluded):
        raise SecureFileTransactionError("Ungültiger Top-Level-Ausschluss")
    try:
        root_descriptor, root_identity = open_bound_directory(root)
    except FileNotFoundError:
        if allow_missing:
            parent_path = os.path.dirname(root)
            parent_descriptor, parent_identity = open_bound_directory(parent_path)
            try:
                try:
                    os.stat(
                        os.path.basename(root),
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise SecureFileTransactionError(
                        "Fehlender Baum entstand während der Bindung"
                    )
            finally:
                os.close(parent_descriptor)
            _assert_directory_path_bound(parent_path, parent_identity)
            return _missing_tree_snapshot(root, parent_identity)
        raise

    directories: dict[str, dict[str, object]] = {}
    files: dict[str, dict[str, object]] = {}
    total_bytes = 0
    entries = 0
    root_info = os.fstat(root_descriptor)
    root_device = root_info.st_dev

    def _check_directory(metadata: os.stat_result, label: str) -> None:
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != root_device
            or (expected_uid is not None and metadata.st_uid != int(expected_uid))
            or (
                require_owner_only_write
                and stat.S_IMODE(metadata.st_mode) & 0o022
            )
        ):
            raise SecureFileTransactionError(
                f"Verzeichnis verletzt den gebundenen Baumvertrag: {label}"
            )

    def _scan(directory_descriptor: int, relative_parent: str, *, top_level: bool) -> None:
        nonlocal entries, total_bytes
        directory_before = os.fstat(directory_descriptor)
        _check_directory(directory_before, relative_parent or ".")
        directory_identity = _directory_identity(directory_before)
        for name in sorted(os.listdir(directory_descriptor)):
            if top_level and name in excluded:
                continue
            if not name or name in {".", ".."} or os.sep in name:
                raise SecureFileTransactionError("Ungültiger Verzeichniseintrag")
            entries += 1
            if entries > int(max_entries):
                raise SecureFileTransactionError("Dateibaum überschreitet das Eintragslimit")
            relative = os.path.join(relative_parent, name) if relative_parent else name
            metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                _check_directory(metadata, relative)
                child_descriptor = os.open(name, _directory_flags(), dir_fd=directory_descriptor)
                try:
                    opened = os.fstat(child_descriptor)
                    child_identity = _directory_identity(opened)
                    if child_identity != _directory_identity(metadata):
                        raise SecureFileTransactionError(
                            f"Verzeichnis wechselte beim Öffnen: {relative}"
                        )
                    directories[relative] = {
                        "identity": child_identity,
                        "uid": opened.st_uid,
                        "gid": opened.st_gid,
                        "mode": stat.S_IMODE(opened.st_mode),
                    }
                    _scan(child_descriptor, relative, top_level=False)
                    named_after = os.stat(
                        name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                    if _directory_identity(named_after) != child_identity:
                        raise SecureFileTransactionError(
                            f"Verzeichnis driftete während des Scans: {relative}"
                        )
                finally:
                    os.close(child_descriptor)
            elif stat.S_ISREG(metadata.st_mode):
                absolute = os.path.join(root, relative)
                snapshot = _snapshot_regular_from_parent(
                    absolute,
                    directory_descriptor,
                    directory_identity,
                    expected_uid=expected_uid,
                    expected_gid=None,
                    max_bytes=max_file_bytes,
                )
                if (
                    require_owner_only_write
                    and int(snapshot["mode"]) & 0o022
                ):
                    raise SecureFileTransactionError(
                        f"Datei ist gruppen- oder weltbeschreibbar: {relative}"
                    )
                total_bytes += len(snapshot["payload"])
                if total_bytes > int(max_total_bytes):
                    raise SecureFileTransactionError("Dateibaum überschreitet das Bytelimit")
                files[relative] = snapshot
            else:
                raise SecureFileTransactionError(
                    f"Symlink oder Special-Datei im gebundenen Baum: {relative}"
                )
        if _directory_identity(os.fstat(directory_descriptor)) != directory_identity:
            raise SecureFileTransactionError(
                f"Verzeichnis driftete beim Scannen: {relative_parent or '.'}"
            )

    try:
        _check_directory(root_info, ".")
        _scan(root_descriptor, "", top_level=True)
        if _directory_identity(os.fstat(root_descriptor)) != root_identity:
            raise SecureFileTransactionError("Baumroot driftete während des Scans")
    finally:
        os.close(root_descriptor)
    _assert_directory_path_bound(root, root_identity)
    parent_path = os.path.dirname(root)
    parent_descriptor, parent_identity = open_bound_directory(parent_path)
    try:
        named_root = os.stat(
            os.path.basename(root),
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _directory_identity(named_root) != root_identity:
            raise SecureFileTransactionError("Baumroot driftete gegenüber seinem Parent")
    finally:
        os.close(parent_descriptor)
    _assert_directory_path_bound(parent_path, parent_identity)
    return {
        "schema": "e3dc_bound_tree_v1",
        "path": root,
        "parent_path": parent_path,
        "parent_identity": parent_identity,
        "exists": True,
        "root_identity": root_identity,
        "root_uid": root_info.st_uid,
        "root_gid": root_info.st_gid,
        "root_mode": stat.S_IMODE(root_info.st_mode),
        "directories": directories,
        "files": files,
        "total_bytes": total_bytes,
    }


def tree_snapshots_match(
    actual: Mapping[str, object],
    expected: Mapping[str, object],
    *,
    exact_identities: bool = True,
) -> bool:
    if (
        actual.get("path") != expected.get("path")
        or bool(actual.get("exists")) != bool(expected.get("exists"))
    ):
        return False
    actual_parent = tuple(actual.get("parent_identity") or ())
    expected_parent = tuple(expected.get("parent_identity") or ())
    if actual_parent[:2] != expected_parent[:2]:
        return False
    if not actual.get("exists"):
        return True
    actual_directories = dict(actual.get("directories") or {})
    expected_directories = dict(expected.get("directories") or {})
    actual_files = dict(actual.get("files") or {})
    expected_files = dict(expected.get("files") or {})
    if actual_directories.keys() != expected_directories.keys() or actual_files.keys() != expected_files.keys():
        return False
    actual_root = tuple(actual.get("root_identity") or ())
    expected_root = tuple(expected.get("root_identity") or ())
    if exact_identities:
        if actual_root != expected_root:
            return False
    elif (
        actual.get("root_uid") != expected.get("root_uid")
        or actual.get("root_gid") != expected.get("root_gid")
        or actual.get("root_mode") != expected.get("root_mode")
    ):
        return False
    for relative, expected_directory in expected_directories.items():
        actual_directory = actual_directories[relative]
        actual_identity = tuple(actual_directory.get("identity") or ())
        expected_identity = tuple(expected_directory.get("identity") or ())
        if exact_identities:
            if actual_identity != expected_identity:
                return False
        elif (
            actual_directory.get("uid") != expected_directory.get("uid")
            or actual_directory.get("gid") != expected_directory.get("gid")
            or actual_directory.get("mode") != expected_directory.get("mode")
        ):
            return False
    for relative, expected_file in expected_files.items():
        actual_file = actual_files[relative]
        if exact_identities:
            if not _same_snapshot(actual_file, expected_file, exact_metadata=True):
                return False
        elif (
            actual_file.get("kind") != "regular"
            or actual_file.get("sha256") != expected_file.get("sha256")
            or actual_file.get("uid") != expected_file.get("uid")
            or actual_file.get("gid") != expected_file.get("gid")
            or actual_file.get("mode") != expected_file.get("mode")
        ):
            return False
    return True


def remove_bound_regular_tree(
    snapshot: Mapping[str, object],
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TREE_BYTES,
) -> dict[str, object]:
    """Entfernt nur einen vollständig unveränderten, gebundenen regulären Baum."""

    root = _normalise_absolute(str(snapshot.get("path") or ""))
    if not snapshot.get("exists"):
        return dict(snapshot)
    current = snapshot_bound_regular_tree(
        root,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    if not tree_snapshots_match(current, snapshot, exact_identities=True):
        raise SecureFileTransactionError("Dateibaum driftete vor dem Entfernen")

    files = dict(snapshot.get("files") or {})
    directories = dict(snapshot.get("directories") or {})
    parent_path = os.path.dirname(root)
    root_name = os.path.basename(root)
    parent_descriptor, parent_identity = open_bound_directory(parent_path)
    root_descriptor = -1
    if parent_identity[:2] != tuple(snapshot.get("parent_identity") or ())[:2]:
        os.close(parent_descriptor)
        raise SecureFileTransactionError("Baumparent driftete vor dem Entfernen")

    def _remove_children(directory_descriptor: int, relative_parent: str) -> None:
        for name in sorted(os.listdir(directory_descriptor)):
            relative = os.path.join(relative_parent, name) if relative_parent else name
            if relative in files:
                expected_file = files[relative]
                parent_identity = _directory_identity(os.fstat(directory_descriptor))
                actual_file = _entry_token_from_parent(
                    os.path.join(root, relative),
                    directory_descriptor,
                    parent_identity,
                    allow_symlink=False,
                    max_bytes=max_file_bytes,
                )
                if not _same_snapshot(actual_file, expected_file, exact_metadata=True):
                    raise SecureFileTransactionError(
                        f"Datei driftete beim Entfernen: {relative}"
                    )
                os.unlink(name, dir_fd=directory_descriptor)
                continue
            if relative not in directories:
                raise SecureFileTransactionError(
                    f"Unbekannter Eintrag beim Entfernen: {relative}"
                )
            expected_directory = directories[relative]
            named = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if _directory_identity(named) != tuple(expected_directory["identity"]):
                raise SecureFileTransactionError(
                    f"Verzeichnis driftete beim Entfernen: {relative}"
                )
            child_descriptor = os.open(name, _directory_flags(), dir_fd=directory_descriptor)
            try:
                if _directory_identity(os.fstat(child_descriptor)) != tuple(expected_directory["identity"]):
                    raise SecureFileTransactionError(
                        f"Verzeichnis wechselte beim Entfernen: {relative}"
                    )
                _remove_children(child_descriptor, relative)
                os.fsync(child_descriptor)
            finally:
                os.close(child_descriptor)
            named_after = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if (named_after.st_dev, named_after.st_ino) != tuple(expected_directory["identity"])[:2]:
                raise SecureFileTransactionError(
                    f"Verzeichnis wechselte vor rmdir: {relative}"
                )
            os.rmdir(name, dir_fd=directory_descriptor)

    try:
        named_root = os.stat(root_name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _directory_identity(named_root) != tuple(snapshot.get("root_identity") or ()):
            raise SecureFileTransactionError("Baumroot driftete vor dem Entfernen")
        root_descriptor = os.open(root_name, _directory_flags(), dir_fd=parent_descriptor)
        if _directory_identity(os.fstat(root_descriptor)) != tuple(snapshot.get("root_identity") or ()):
            raise SecureFileTransactionError("Baumroot wechselte beim Entfernen")
        _remove_children(root_descriptor, "")
        os.fsync(root_descriptor)
        named_after = os.stat(root_name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (named_after.st_dev, named_after.st_ino) != tuple(snapshot.get("root_identity") or ())[:2]:
            raise SecureFileTransactionError("Baumroot wechselte vor rmdir")
        os.rmdir(root_name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        _assert_directory_path_bound(parent_path, parent_identity)
        return _missing_tree_snapshot(root, parent_identity)
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)
        os.close(parent_descriptor)


def restore_bound_regular_tree(
    previous: Mapping[str, object],
    *,
    staging_root: str | os.PathLike[str] | None = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TREE_BYTES,
) -> dict[str, object]:
    """Restauriert einen teilweise oder vollständig entfernten Baum fail-closed."""

    root = _normalise_absolute(str(previous.get("path") or ""))
    current = snapshot_bound_regular_tree(
        root,
        allow_missing=True,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    previous_parent = tuple(previous.get("parent_identity") or ())
    current_parent = tuple(current.get("parent_identity") or ())
    if previous_parent[:2] != current_parent[:2]:
        raise SecureFileTransactionError(
            "Rollback-Baumparent driftete seit dem Preimage"
        )
    if not previous.get("exists"):
        if current.get("exists"):
            raise SecureFileTransactionError("Rollback-Baum entstand unerwartet")
        return current

    expected_directories = dict(previous.get("directories") or {})
    expected_files = dict(previous.get("files") or {})
    directory_bindings: dict[str, tuple[int, ...]] = {}
    current_directories = {}
    if current.get("exists"):
        if tuple(current.get("root_identity") or ()) != tuple(previous.get("root_identity") or ()):
            raise SecureFileTransactionError("Rollback-Baumroot driftete fremd")
        current_directories = dict(current.get("directories") or {})
        current_files = dict(current.get("files") or {})
        if not current_directories.keys() <= expected_directories.keys() or not current_files.keys() <= expected_files.keys():
            raise SecureFileTransactionError("Rollback-Baum enthält fremde Einträge")
        for relative, current_directory in current_directories.items():
            if tuple(current_directory.get("identity") or ()) != tuple(expected_directories[relative].get("identity") or ()):
                raise SecureFileTransactionError(
                    f"Rollback-Verzeichnis driftete fremd: {relative}"
                )
        for relative, current_file in current_files.items():
            if not _same_snapshot(
                current_file,
                expected_files[relative],
                exact_metadata=True,
            ):
                raise SecureFileTransactionError(f"Rollback-Datei driftete fremd: {relative}")
        directory_bindings[""] = tuple(current.get("root_identity") or ())
    else:
        root_binding = ensure_bound_directory(
            root,
            uid=int(previous["root_uid"]),
            gid=int(previous["root_gid"]),
            mode=int(previous["root_mode"]),
            expected_parent_identity=previous_parent,
            expected_missing=True,
        )
        directory_bindings[""] = tuple(root_binding["identity"])

    for relative in sorted(expected_directories, key=lambda value: (value.count(os.sep), value)):
        metadata = expected_directories[relative]
        relative_parent = os.path.dirname(relative)
        if relative_parent == ".":
            relative_parent = ""
        expected_parent = directory_bindings.get(relative_parent)
        if not expected_parent:
            raise SecureFileTransactionError(
                f"Rollback-Verzeichnisparent ist nicht gebunden: {relative}"
            )
        existing = current_directories.get(relative)
        binding = ensure_bound_directory(
            os.path.join(root, relative),
            uid=int(metadata["uid"]),
            gid=int(metadata["gid"]),
            mode=int(metadata["mode"]),
            expected_identity=(
                tuple(metadata["identity"])
                if existing is not None
                else None
            ),
            expected_parent_identity=expected_parent,
            expected_missing=existing is None,
        )
        directory_bindings[relative] = tuple(binding["identity"])
    for relative, expected_file in sorted(expected_files.items()):
        target = os.path.join(root, relative)
        target_current = snapshot_bound_file(
            target,
            allow_missing=True,
            max_bytes=max_file_bytes,
        )
        relative_parent = os.path.dirname(relative)
        if relative_parent == ".":
            relative_parent = ""
        if tuple(target_current.get("parent_identity") or ())[:2] != tuple(
            directory_bindings.get(relative_parent) or ()
        )[:2]:
            raise SecureFileTransactionError(
                f"Rollback-Dateiparent driftete fremd: {relative}"
            )
        if target_current.get("exists"):
            if not _same_snapshot(target_current, expected_file, exact_metadata=True):
                raise SecureFileTransactionError(
                    f"Rollback-Datei entstand fremd: {relative}"
                )
            continue
        atomic_write_bound_file(
            target,
            expected_file["payload"],
            uid=int(expected_file["uid"]),
            gid=int(expected_file["gid"]),
            mode=int(expected_file["mode"]),
            expected_snapshot=target_current,
            staging_root=staging_root,
        )

    restored = snapshot_bound_regular_tree(
        root,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    if not tree_snapshots_match(restored, previous, exact_identities=False):
        raise SecureFileTransactionError("Restaurierter Baum weicht vom Preimage ab")
    return restored


def remove_bound_directory_if_empty(snapshot: Mapping[str, object]) -> None:
    """Entfernt nur ein weiterhin gebundenes, leeres, von uns erzeugtes Verzeichnis."""

    target = _normalise_absolute(str(snapshot.get("path") or ""))
    if not snapshot.get("created"):
        return
    parent_path = os.path.dirname(target)
    parent_descriptor, parent_identity = open_bound_directory(parent_path)
    descriptor = -1
    try:
        if parent_identity[:2] != tuple(snapshot.get("parent_identity") or ())[:2]:
            raise SecureFileTransactionError(
                "Erzeugter Verzeichnisparent driftete vor rmdir"
            )
        name = os.path.basename(target)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _directory_identity(named) != tuple(snapshot.get("identity") or ()):
            raise SecureFileTransactionError("Erzeugtes Verzeichnis driftete vor rmdir")
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
        if _directory_identity(os.fstat(descriptor)) != tuple(snapshot.get("identity") or ()):
            raise SecureFileTransactionError("Erzeugtes Verzeichnis wechselte vor rmdir")
        if os.listdir(descriptor):
            raise SecureFileTransactionError("Erzeugtes Verzeichnis ist nicht leer")
        os.close(descriptor)
        descriptor = -1
        os.rmdir(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        _assert_directory_path_bound(parent_path, parent_identity)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


@contextmanager
def exclusive_transaction_lock(name: str):
    """Serialisiert eine mehrteilige Installertransaktion in `/run/lock`."""

    normalised = str(name or "")
    if not normalised or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        for character in normalised
    ):
        raise SecureFileTransactionError("Ungültiger Transaktions-Lockname")
    root_descriptor, root_identity = open_bound_directory(_LOCK_ROOT)
    descriptor = -1
    try:
        root_info = os.fstat(root_descriptor)
        root_mode = stat.S_IMODE(root_info.st_mode)
        if (
            root_info.st_uid != 0
            or root_mode & stat.S_IWOTH
            or (root_mode & stat.S_IWGRP and root_info.st_gid != 0)
        ):
            raise SecureFileTransactionError("/run/lock ist nicht root-kontrolliert")
        descriptor = os.open(
            normalised,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=root_descriptor,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
        ):
            raise SecureFileTransactionError("Transaktions-Lock ist nicht vertrauenswürdig")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SecureFileTransactionError(
                "Eine gleichartige Installertransaktion läuft bereits"
            ) from exc
        _assert_directory_path_bound(_LOCK_ROOT, root_identity)
        yield
    finally:
        if descriptor >= 0:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)
        os.close(root_descriptor)


__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_TREE_BYTES",
    "DEFAULT_MAX_TREE_ENTRIES",
    "SecureFileTransactionError",
    "atomic_write_bound_file",
    "ensure_bound_directory",
    "exclusive_transaction_lock",
    "open_bound_directory",
    "read_bound_regular_file",
    "remove_bound_directory_if_empty",
    "remove_bound_file",
    "remove_bound_regular_tree",
    "render_assignment_updates",
    "restore_bound_file",
    "restore_bound_regular_tree",
    "set_bound_file_metadata",
    "snapshot_bound_file",
    "snapshot_bound_regular_tree",
    "snapshots_match",
    "tree_snapshots_match",
]
