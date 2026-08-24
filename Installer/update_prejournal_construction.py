"""Kurzlebiger, rebootfester Zeuge vor dem Update-Master-Journal.

Der Receipt autorisiert weder Gate-, Dienst- noch Produktmutationen. Er
beweist ausschließlich, dass eine konkrete Update-Transaktion ihre drei
unveränderlichen Parent-Belege noch aufbaut und das Master-Journal deshalb
noch nie als Richtungsautorität veröffentlicht wurde.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import errno
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
from typing import Mapping

from . import update_recovery_journal as recovery_journal


PREJOURNAL_CONSTRUCTION_SCHEMA = "e3dc_update_prejournal_construction_v1"
PREJOURNAL_CONSTRUCTION_PATH = (
    "/var/lib/e3dc-update-safety/prejournal-construction.json"
)
MAX_PREJOURNAL_CONSTRUCTION_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_CREATE_STAGING_RE = re.compile(
    r"\.e3dc-prejournal-construction-[0-9]+-[0-9a-f]{24}\Z"
)


class UpdatePrejournalConstructionError(RuntimeError):
    """Fail-closed Fehler mit einer direkt ausführbaren Lösung."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        subject: str,
        solution: str | None = None,
    ) -> None:
        self.code = str(code)
        self.subject = str(subject)
        remedy = str(solution or "").strip() or (
            "Keine Recovery-Datei manuell löschen; starte denselben "
            "Updatebefehl erneut. Bleibt der Fehler bestehen, prüfe "
            f"sudo stat -c '%U:%G %a %h %s %n' {shlex.quote(self.subject)} "
            "und sudo journalctl -b -u 'e3dc-*update*' --no-pager."
        )
        super().__init__(
            f"[{self.code}] {message} Betroffen: {self.subject}. Lösung: {remedy}"
        )


@dataclass(frozen=True)
class PrejournalConstructionReceipt:
    transaction_id: str
    install_root: str
    install_user: str
    source: recovery_journal.RecoverySourceBinding
    target: recovery_journal.RecoveryTargetBinding
    backup_dir: str
    full_backup: recovery_journal.RecoveryFullBackupBinding
    binding_sha256: str


@dataclass(frozen=True)
class PersistedPrejournalConstruction:
    receipt: PrejournalConstructionReceipt
    path: str
    device: int
    inode: int
    size: int
    sha256: str
    identity: tuple[int, ...]


def _require_root(path: str) -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise UpdatePrejournalConstructionError(
            "E3DC-UPD-PREJOURNAL-ROOT-001",
            "Der Construction-Receipt darf ausschließlich Root verwalten.",
            subject=path,
            solution=(
                "starte genau denselben Web-, Bootstrap- oder Konsolenbefehl "
                "mit sudo; übergib keine Parent-, SHA- oder Guard-Werte selbst"
            ),
        )


def _canonical_path(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or not os.path.isabs(value)
        or os.path.normpath(value) != value
        or os.path.realpath(value) != value
        or value == "/"
    ):
        raise ValueError(f"{label} ist kein kanonischer absoluter Pfad")
    return value


def _strict_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} ist keine kanonische SHA-256")
    return value


def _strict_mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} ist kein JSON-Objekt")
    return value


def _source_mapping(
    source: recovery_journal.RecoverySourceBinding,
) -> dict[str, object]:
    return {
        "kind": source.kind,
        "version": source.version,
        "commit": source.commit,
        "repository_present": source.repository_present,
        "repository_rebuild_required": source.repository_rebuild_required,
        "identity_sha256": source.identity_sha256,
    }


def _target_mapping(
    target: recovery_journal.RecoveryTargetBinding,
) -> dict[str, object]:
    return {
        "commit": target.commit,
        "tag": target.tag,
        "role": target.role,
        "identity_sha256": target.identity_sha256,
    }


def _backup_mapping(
    backup: recovery_journal.RecoveryFullBackupBinding,
) -> dict[str, object]:
    return {
        "backup_id": backup.backup_id,
        "manifest_sha256": backup.manifest_sha256,
    }


def _core_mapping(
    receipt: PrejournalConstructionReceipt,
) -> dict[str, object]:
    return {
        "transaction_id": receipt.transaction_id,
        "install_root": receipt.install_root,
        "install_user": receipt.install_user,
        "source": _source_mapping(receipt.source),
        "target": _target_mapping(receipt.target),
        "backup_dir": receipt.backup_dir,
        "full_backup": _backup_mapping(receipt.full_backup),
    }


def _canonical_json(mapping: Mapping[str, object]) -> bytes:
    try:
        payload = (
            json.dumps(
                mapping,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("Construction-Receipt ist nicht kanonisch kodierbar") from exc
    if not payload or len(payload) > MAX_PREJOURNAL_CONSTRUCTION_BYTES:
        raise ValueError("Construction-Receipt überschreitet sein Größenlimit")
    return payload


def _binding_sha256(receipt: PrejournalConstructionReceipt) -> str:
    return hashlib.sha256(_canonical_json(_core_mapping(receipt))).hexdigest()


def _validate(
    receipt: PrejournalConstructionReceipt,
) -> PrejournalConstructionReceipt:
    if not isinstance(receipt, PrejournalConstructionReceipt):
        raise ValueError("Construction-Receipt besitzt den falschen Typ")
    if not _SHA256_RE.fullmatch(str(receipt.transaction_id or "")):
        raise ValueError("Transaktions-ID ist nicht kanonisch")
    install_root = _canonical_path(receipt.install_root, label="Installationspfad")
    backup_dir = _canonical_path(receipt.backup_dir, label="Backuppfad")
    if not isinstance(receipt.install_user, str) or not _USER_RE.fullmatch(
        receipt.install_user
    ):
        raise ValueError("Installationsbenutzer ist nicht kanonisch")
    source = recovery_journal.make_source_binding(
        kind=receipt.source.kind,
        version=receipt.source.version,
        commit=receipt.source.commit,
        repository_present=receipt.source.repository_present,
        repository_rebuild_required=receipt.source.repository_rebuild_required,
    )
    target = recovery_journal.make_target_binding(
        commit=receipt.target.commit,
        tag=receipt.target.tag,
        role=receipt.target.role,
    )
    full_backup = recovery_journal.make_full_backup_binding(
        backup_id=receipt.full_backup.backup_id,
        manifest_sha256=receipt.full_backup.manifest_sha256,
    )
    if (
        source != receipt.source
        or target != receipt.target
        or full_backup != receipt.full_backup
    ):
        raise ValueError("Source-, Target- oder Backup-Bindung ist nicht kanonisch")
    canonical = replace(
        receipt,
        install_root=install_root,
        backup_dir=backup_dir,
        binding_sha256="0" * 64,
    )
    expected = _binding_sha256(canonical)
    if receipt.binding_sha256 != expected:
        raise ValueError("Construction-Bindung ist nicht selbstkonsistent")
    return replace(canonical, binding_sha256=expected)


def make_prejournal_construction_receipt(
    *,
    transaction_id: str,
    install_root: str,
    install_user: str,
    source: recovery_journal.RecoverySourceBinding,
    target: recovery_journal.RecoveryTargetBinding,
    backup_dir: str,
    full_backup: recovery_journal.RecoveryFullBackupBinding,
) -> PrejournalConstructionReceipt:
    provisional = PrejournalConstructionReceipt(
        transaction_id=str(transaction_id),
        install_root=str(install_root),
        install_user=str(install_user),
        source=source,
        target=target,
        backup_dir=str(backup_dir),
        full_backup=full_backup,
        binding_sha256="0" * 64,
    )
    return _validate(
        replace(provisional, binding_sha256=_binding_sha256(provisional))
    )


def serialize_prejournal_construction(
    receipt: PrejournalConstructionReceipt,
) -> bytes:
    canonical = _validate(receipt)
    return _canonical_json(
        {
            "schema": PREJOURNAL_CONSTRUCTION_SCHEMA,
            **_core_mapping(canonical),
            "binding_sha256": canonical.binding_sha256,
        }
    )


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Doppelter JSON-Key: {key}")
        result[key] = value
    return result


def _reject_float(value: str):
    raise ValueError(f"Fließkommazahl ist unzulässig: {value}")


def parse_prejournal_construction(
    payload: bytes,
    *,
    subject: str = PREJOURNAL_CONSTRUCTION_PATH,
) -> PrejournalConstructionReceipt:
    try:
        if (
            not isinstance(payload, bytes)
            or not payload
            or len(payload) > MAX_PREJOURNAL_CONSTRUCTION_BYTES
        ):
            raise ValueError("Construction-Payload besitzt eine ungültige Größe")
        top = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_strict_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
        if not isinstance(top, dict) or set(top) != {
            "schema",
            "transaction_id",
            "install_root",
            "install_user",
            "source",
            "target",
            "backup_dir",
            "full_backup",
            "binding_sha256",
        }:
            raise ValueError("Construction-Receipt besitzt unbekannte oder fehlende Felder")
        if top["schema"] != PREJOURNAL_CONSTRUCTION_SCHEMA:
            raise ValueError("Construction-Schema ist unbekannt")
        source_map = _strict_mapping(top["source"], label="Source")
        target_map = _strict_mapping(top["target"], label="Target")
        backup_map = _strict_mapping(top["full_backup"], label="FullBackup")
        if set(source_map) != {
            "kind",
            "version",
            "commit",
            "repository_present",
            "repository_rebuild_required",
            "identity_sha256",
        }:
            raise ValueError("Source besitzt unbekannte oder fehlende Felder")
        if set(target_map) != {
            "commit",
            "tag",
            "role",
            "identity_sha256",
        }:
            raise ValueError("Target besitzt unbekannte oder fehlende Felder")
        if set(backup_map) != {"backup_id", "manifest_sha256"}:
            raise ValueError("FullBackup besitzt unbekannte oder fehlende Felder")
        source = recovery_journal.make_source_binding(
            kind=source_map["kind"],
            version=source_map["version"],
            commit=source_map["commit"],
            repository_present=source_map["repository_present"],
            repository_rebuild_required=source_map[
                "repository_rebuild_required"
            ],
        )
        target = recovery_journal.make_target_binding(
            commit=target_map["commit"],
            tag=target_map["tag"],
            role=target_map["role"],
        )
        full_backup = recovery_journal.make_full_backup_binding(
            backup_id=backup_map["backup_id"],
            manifest_sha256=backup_map["manifest_sha256"],
        )
        if (
            source.identity_sha256 != source_map["identity_sha256"]
            or target.identity_sha256 != target_map["identity_sha256"]
        ):
            raise ValueError("Source-/Target-Identität driftete")
        receipt = PrejournalConstructionReceipt(
            transaction_id=top["transaction_id"],
            install_root=top["install_root"],
            install_user=top["install_user"],
            source=source,
            target=target,
            backup_dir=top["backup_dir"],
            full_backup=full_backup,
            binding_sha256=_strict_sha256(
                top["binding_sha256"],
                label="Construction-Bindung",
            ),
        )
        canonical = _validate(receipt)
        if serialize_prejournal_construction(canonical) != payload:
            raise ValueError("Construction-Receipt ist nicht kanonisch serialisiert")
        return canonical
    except UpdatePrejournalConstructionError:
        raise
    except Exception as exc:
        raise UpdatePrejournalConstructionError(
            "E3DC-UPD-PREJOURNAL-PARSE-001",
            f"Construction-Receipt ist ungültig: {exc}",
            subject=subject,
            solution=(
                "lösche oder bearbeite den Beleg nicht. Prüfe den genannten "
                "Pfad und das Updatejournal; starte danach denselben "
                "Updatebefehl erneut"
            ),
        ) from exc


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_uid),
        int(metadata.st_gid),
        int(stat.S_IMODE(metadata.st_mode)),
    )


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_uid),
        int(metadata.st_gid),
        int(stat.S_IMODE(metadata.st_mode)),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _xattrs(descriptor: int) -> tuple[str, ...]:
    try:
        return tuple(os.listxattr(descriptor))
    except AttributeError as exc:
        raise ValueError("xattr-Prüfung ist nicht verfügbar") from exc
    except OSError as exc:
        if exc.errno in {
            getattr(errno, "ENODATA", -1),
            getattr(errno, "ENOTSUP", -1),
            getattr(errno, "EOPNOTSUPP", -1),
        }:
            return ()
        raise


def _directory_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not directory or not nofollow:
        raise ValueError("O_DIRECTORY/O_NOFOLLOW ist nicht verfügbar")
    return (
        os.O_RDONLY
        | directory
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_secure_parent(path: str) -> tuple[int, tuple[int, ...]]:
    """Bindet jeden Vorfahren und hält den finalen Parent geöffnet."""

    parent = os.path.dirname(path)
    descriptor = os.open("/", _directory_flags())
    try:
        current_path = "/"
        root_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != 0
            or root_metadata.st_gid != 0
            or stat.S_IMODE(root_metadata.st_mode) & 0o022
        ):
            raise ValueError("Root-Verzeichnis ist nicht ausschließlich root-kontrolliert")
        for component in (part for part in parent.split(os.sep) if part):
            named = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            next_descriptor = os.open(
                component,
                _directory_flags(),
                dir_fd=descriptor,
            )
            opened = os.fstat(next_descriptor)
            current_path = os.path.join(current_path, component)
            mode = stat.S_IMODE(opened.st_mode)
            sticky_root = bool(
                opened.st_uid == 0
                and opened.st_gid == 0
                and mode & stat.S_ISVTX
            )
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _directory_identity(named) != _directory_identity(opened)
                or opened.st_uid != 0
                or opened.st_gid != 0
                or (mode & 0o022 and not sticky_root)
            ):
                os.close(next_descriptor)
                raise ValueError(
                    f"Elternpfad ist nicht root-kontrolliert: {current_path}"
                )
            os.close(descriptor)
            descriptor = next_descriptor
        final_metadata = os.fstat(descriptor)
        final_identity = _directory_identity(final_metadata)
        if stat.S_IMODE(final_metadata.st_mode) != 0o700:
            raise ValueError("State-Verzeichnis ist nicht root:root 0700")
        if _xattrs(descriptor):
            raise ValueError("State-Verzeichnis besitzt ACLs oder xattrs")
        named_final = os.stat(parent, follow_symlinks=False)
        if _directory_identity(named_final) != final_identity:
            raise ValueError("State-Verzeichnis driftete während der Bindung")
        return descriptor, final_identity
    except BaseException:
        os.close(descriptor)
        raise


def _read_exact(descriptor: int, size: int) -> bytes:
    if size <= 0 or size > MAX_PREJOURNAL_CONSTRUCTION_BYTES:
        raise ValueError("Construction-Größe liegt außerhalb des Vertrags")
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = int(size)
    chunks: list[bytes] = []
    while remaining:
        block = os.read(descriptor, min(64 * 1024, remaining))
        if not block:
            raise ValueError("Construction-Datei endet vor ihrer Größe")
        chunks.append(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise ValueError("Construction-Datei überschreitet ihre Größe")
    return b"".join(chunks)


def _read_named_at(
    parent_descriptor: int,
    parent_identity: tuple[int, ...],
    path: str,
    *,
    allow_missing: bool,
    allowed_nlinks: frozenset[int] = frozenset({1}),
) -> tuple[bytes, os.stat_result] | None:
    name = os.path.basename(path)
    try:
        before = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        identity = _file_identity(opened)
        if (
            _file_identity(before) != identity
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != 0
            or opened.st_gid != 0
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink not in allowed_nlinks
            or opened.st_size <= 0
            or opened.st_size > MAX_PREJOURNAL_CONSTRUCTION_BYTES
            or _xattrs(descriptor)
        ):
            raise ValueError("Construction-Datei verletzt root:root-0600")
        payload = _read_exact(descriptor, int(opened.st_size))
        after = os.fstat(descriptor)
        named_after = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _file_identity(after) != identity
            or _file_identity(named_after) != identity
            or _directory_identity(os.fstat(parent_descriptor))
            != parent_identity
        ):
            raise ValueError("Construction-Datei driftete beim Lesen")
        return payload, after
    finally:
        os.close(descriptor)


def _binding_from_readback(
    receipt: PrejournalConstructionReceipt,
    path: str,
    payload: bytes,
    metadata: os.stat_result,
) -> PersistedPrejournalConstruction:
    identity = _file_identity(metadata)
    return PersistedPrejournalConstruction(
        receipt=receipt,
        path=path,
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        size=int(metadata.st_size),
        sha256=hashlib.sha256(payload).hexdigest(),
        identity=identity,
    )


def _try_heal_interrupted_create(
    parent_descriptor: int,
    parent_identity: tuple[int, ...],
    path: str,
) -> tuple[bytes, os.stat_result] | None:
    """Heilt nur final+eigenen Stagingnamen auf demselben nlink=2-Inode."""

    try:
        readback = _read_named_at(
            parent_descriptor,
            parent_identity,
            path,
            allow_missing=False,
            allowed_nlinks=frozenset({2}),
        )
        if readback is None:
            return None
        payload, metadata = readback
        parse_prejournal_construction(payload, subject=path)
        candidates = []
        for entry in os.listdir(parent_descriptor):
            if not _CREATE_STAGING_RE.fullmatch(entry):
                continue
            candidate = os.stat(
                entry,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (candidate.st_dev, candidate.st_ino) == (
                metadata.st_dev,
                metadata.st_ino,
            ):
                candidates.append(entry)
        if len(candidates) != 1:
            return None
        candidate = candidates[0]
        final_rebound = os.stat(
            os.path.basename(path),
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        candidate_rebound = os.stat(
            candidate,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _file_identity(final_rebound) != _file_identity(metadata)
            or _file_identity(candidate_rebound) != _file_identity(metadata)
            or final_rebound.st_nlink != 2
        ):
            return None
        os.unlink(candidate, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        return _read_named_at(
            parent_descriptor,
            parent_identity,
            path,
            allow_missing=False,
        )
    except (FileNotFoundError, OSError, ValueError):
        return None


def _read_from_parent(
    parent_descriptor: int,
    parent_identity: tuple[int, ...],
    path: str,
    *,
    allow_missing: bool,
) -> PersistedPrejournalConstruction | None:
    try:
        readback = _read_named_at(
            parent_descriptor,
            parent_identity,
            path,
            allow_missing=allow_missing,
        )
    except ValueError:
        readback = _try_heal_interrupted_create(
            parent_descriptor,
            parent_identity,
            path,
        )
        if readback is None:
            raise
    if readback is None:
        return None
    payload, metadata = readback
    receipt = parse_prejournal_construction(payload, subject=path)
    return _binding_from_readback(receipt, path, payload, metadata)


def read_prejournal_construction(
    *,
    path: str = PREJOURNAL_CONSTRUCTION_PATH,
    allow_missing: bool = False,
) -> PersistedPrejournalConstruction | None:
    _require_root(path)
    parent_descriptor = -1
    try:
        canonical_path = _canonical_path(path, label="Construction-Pfad")
        parent_descriptor, parent_identity = _open_secure_parent(canonical_path)
        return _read_from_parent(
            parent_descriptor,
            parent_identity,
            canonical_path,
            allow_missing=allow_missing,
        )
    except UpdatePrejournalConstructionError:
        raise
    except Exception as exc:
        raise UpdatePrejournalConstructionError(
            "E3DC-UPD-PREJOURNAL-READ-001",
            f"Construction-Receipt ist nicht exakt lesbar: {exc}",
            subject=str(path),
            solution=(
                "prüfe den genannten Pfad sowie jeden Elternordner. Der "
                "State-Ordner muss root:root 0700 sein; starte danach "
                "denselben Updatebefehl erneut und lösche keinen Beleg"
            ),
        ) from exc
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def write_prejournal_construction(
    receipt: PrejournalConstructionReceipt,
    *,
    path: str = PREJOURNAL_CONSTRUCTION_PATH,
) -> PersistedPrejournalConstruction:
    _require_root(path)
    canonical_path = str(path)
    parent_descriptor = -1
    stage_descriptor = -1
    stage_name = ""
    staged_identity: tuple[int, int] | None = None
    linked = False
    try:
        canonical = _validate(receipt)
        payload = serialize_prejournal_construction(canonical)
        canonical_path = _canonical_path(path, label="Construction-Pfad")
        parent_descriptor, parent_identity = _open_secure_parent(canonical_path)
        before = _read_from_parent(
            parent_descriptor,
            parent_identity,
            canonical_path,
            allow_missing=True,
        )
        if before is not None:
            raise ValueError("Ein Construction-Receipt ist bereits vorhanden")
        stage_name = (
            f".e3dc-prejournal-construction-{os.getpid()}-"
            f"{secrets.token_hex(12)}"
        )
        stage_descriptor = os.open(
            stage_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        view = memoryview(payload)
        while view:
            written = os.write(stage_descriptor, view)
            if written <= 0:
                raise ValueError("Construction-Staging blieb unvollständig")
            view = view[written:]
        os.fchown(stage_descriptor, 0, 0)
        os.fchmod(stage_descriptor, 0o600)
        os.fsync(stage_descriptor)
        staged = os.fstat(stage_descriptor)
        if (
            not stat.S_ISREG(staged.st_mode)
            or staged.st_uid != 0
            or staged.st_gid != 0
            or stat.S_IMODE(staged.st_mode) != 0o600
            or staged.st_nlink != 1
            or staged.st_size != len(payload)
            or _xattrs(stage_descriptor)
            or _read_exact(stage_descriptor, int(staged.st_size)) != payload
        ):
            raise ValueError("Construction-Staging verletzt den Bytevertrag")
        staged_identity = (int(staged.st_dev), int(staged.st_ino))
        if _read_from_parent(
            parent_descriptor,
            parent_identity,
            canonical_path,
            allow_missing=True,
        ) is not None:
            raise ValueError("Construction-Ziel erschien vor dem Commit")
        os.link(
            stage_name,
            os.path.basename(canonical_path),
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        linked = True
        os.unlink(stage_name, dir_fd=parent_descriptor)
        stage_name = ""
        os.fsync(parent_descriptor)
        rebound = _read_from_parent(
            parent_descriptor,
            parent_identity,
            canonical_path,
            allow_missing=False,
        )
        if (
            rebound is None
            or (rebound.device, rebound.inode) != staged_identity
            or rebound.receipt != canonical
        ):
            raise ValueError("Construction-Readback wich vom Create-Inode ab")
        return rebound
    except UpdatePrejournalConstructionError:
        raise
    except BaseException as exc:
        persisted = None
        try:
            if parent_descriptor >= 0 and staged_identity is not None:
                rebound = _read_from_parent(
                    parent_descriptor,
                    parent_identity,
                    canonical_path,
                    allow_missing=True,
                )
                if (
                    rebound is not None
                    and (rebound.device, rebound.inode) == staged_identity
                    and rebound.receipt == canonical
                ):
                    persisted = rebound
        except Exception:
            persisted = None
        if not isinstance(exc, Exception):
            raise
        if persisted is not None:
            return persisted
        raise UpdatePrejournalConstructionError(
            "E3DC-UPD-PREJOURNAL-WRITE-001",
            f"Construction-Receipt konnte nicht dauerhaft geschrieben werden: {exc}",
            subject=canonical_path,
            solution=(
                "starte denselben Updatebefehl erneut. Ein vorhandener "
                "Construction-Receipt wird ausschließlich inodegebunden "
                "fortgesetzt; lösche oder überschreibe ihn nicht"
            ),
        ) from exc
    finally:
        if stage_descriptor >= 0:
            os.close(stage_descriptor)
        if stage_name and parent_descriptor >= 0 and not linked:
            try:
                os.unlink(stage_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def verify_prejournal_construction(
    binding: PersistedPrejournalConstruction,
) -> PersistedPrejournalConstruction:
    if not isinstance(binding, PersistedPrejournalConstruction):
        raise TypeError("Construction-Bindung besitzt den falschen Typ")
    current = read_prejournal_construction(path=binding.path)
    if current != binding:
        raise UpdatePrejournalConstructionError(
            "E3DC-UPD-PREJOURNAL-VERIFY-001",
            "Construction-Inode, Bytes oder Metadaten drifteten.",
            subject=binding.path,
        )
    return current


def remove_prejournal_construction(
    binding: PersistedPrejournalConstruction,
) -> None:
    if not isinstance(binding, PersistedPrejournalConstruction):
        raise TypeError("Construction-Bindung besitzt den falschen Typ")
    _require_root(binding.path)
    parent_descriptor = -1
    try:
        parent_descriptor, parent_identity = _open_secure_parent(binding.path)
        current = _read_from_parent(
            parent_descriptor,
            parent_identity,
            binding.path,
            allow_missing=False,
        )
        if current != binding:
            raise ValueError("Construction-Datei driftete vor dem Entfernen")
        named = os.stat(
            os.path.basename(binding.path),
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _file_identity(named) != binding.identity:
            raise ValueError("Construction-Inode driftete vor unlink")
        os.unlink(os.path.basename(binding.path), dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        if _read_from_parent(
            parent_descriptor,
            parent_identity,
            binding.path,
            allow_missing=True,
        ) is not None:
            raise ValueError("Construction-Datei blieb nach unlink vorhanden")
    except UpdatePrejournalConstructionError:
        raise
    except Exception as exc:
        raise UpdatePrejournalConstructionError(
            "E3DC-UPD-PREJOURNAL-REMOVE-001",
            f"Construction-Datei konnte nicht exakt entfernt werden: {exc}",
            subject=binding.path,
            solution=(
                "Datei nicht manuell löschen oder ersetzen; starte denselben "
                "Updatebefehl erneut und prüfe bei erneutem Abbruch den "
                "genannten Inode sowie das Updatejournal"
            ),
        ) from exc
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


__all__ = [
    "MAX_PREJOURNAL_CONSTRUCTION_BYTES",
    "PREJOURNAL_CONSTRUCTION_PATH",
    "PREJOURNAL_CONSTRUCTION_SCHEMA",
    "PersistedPrejournalConstruction",
    "PrejournalConstructionReceipt",
    "UpdatePrejournalConstructionError",
    "make_prejournal_construction_receipt",
    "parse_prejournal_construction",
    "read_prejournal_construction",
    "remove_prejournal_construction",
    "serialize_prejournal_construction",
    "verify_prejournal_construction",
    "write_prejournal_construction",
]
