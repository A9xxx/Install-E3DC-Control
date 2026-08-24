#!/usr/bin/env python3
"""Fail-closed backup, manifest, retention and restore primitives.

The module deliberately avoids Git, systemd and hardware access. Source trees
are traversed through directory file descriptors with ``O_NOFOLLOW``. Restore
is transactional across the complete batch: if one replacement fails, every
already changed target is rolled back and verified before an error is returned.
"""

from __future__ import annotations

import datetime as _datetime
import ctypes
import errno
import hashlib
import json
import os
import pwd
import re

try:
    import resource as _resource
except ImportError:  # pragma: no cover - Windows-Testlauf, Produktivbetrieb ist Linux.
    _resource = None

import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

try:
    import fcntl
except ImportError:  # pragma: no cover - Produktivbetrieb erfolgt unter Linux.
    fcntl = None


PathValue = Union[str, os.PathLike]
MANIFEST_NAME = "backup-manifest.json"
MANIFEST_DIGEST_NAME = "backup-manifest.sha256"
ROOT_MARKER_NAME = ".e3dc-backup-root.json"
MANIFEST_SCHEMA = 3
LEGACY_MANIFEST_SCHEMA = 2
ROOT_MARKER_SCHEMA = 1
SYSTEMD_MASK_STATE_SCHEMA = 1
SYSTEMD_ADMIN_UNIT_DIR = Path("/etc/systemd/system")
BACKUP_ROOT_NAMES = {"e3dc-control-backups", ".e3dc-control-backups"}
DEFAULT_BACKUP_ROOT = Path("/srv/e3dc-control-backups")
SYSTEM_BACKUP_KIND = "system-backup"
WEB_SNAPSHOT_KIND = "web-snapshot"
QUIESCED_OVERLAY_KIND = "quiesced-overlay"
_CATEGORY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
PRIVATE_ML_ROOT = Path("/var/lib/e3dc-control/ml")
LEGACY_ML_MODEL = Path("/var/www/html/data/ml_model.pkl")
ML_MODEL_MANIFEST_NAME = "ml_model.manifest.json"
ML_MODEL_LOCK_NAME = ".ml_model.lock"
ML_MODEL_SCHEMA_VERSION = 1
ML_MODEL_FORMAT = "e3dc-sklearn-pickle"
ML_MODEL_MAX_BYTES = 128 * 1024 * 1024
_ML_MODEL_FILE_RE = re.compile(r"ml_model-([0-9a-f]{64})\.pkl\Z")
MAX_CLEANUP_TREE_DEPTH = 512
BACKUP_ESTIMATE_FIXED_OVERHEAD_BYTES = 64 * 1024
BACKUP_ESTIMATE_SOURCE_OVERHEAD_BYTES = 1024
BACKUP_ESTIMATE_DIRECTORY_OVERHEAD_BYTES = 4 * 1024
BACKUP_ESTIMATE_FILE_OVERHEAD_BYTES = 8 * 1024


class BackupIntegrityError(RuntimeError):
    """The backup or restore contract is incomplete or unsafe."""


def _normalized_systemd_mask_entries(entries: Iterable[Dict[str, object]]) -> List[Dict[str, object]]:
    """Prüft und kanonisiert den manifestgebundenen systemd-Maskenumfang."""

    normalized: List[Dict[str, object]] = []
    seen: Set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict) or set(raw) != {"path", "state", "target"}:
            raise BackupIntegrityError("Ungültiger systemd-Maskeneintrag im Manifest")
        path = _lexical_absolute(str(raw.get("path") or ""))
        if path.parent != SYSTEMD_ADMIN_UNIT_DIR or path.name in {"", ".", ".."}:
            raise BackupIntegrityError("Systemd-Maskenpfad liegt außerhalb der Admin-Unitfläche")
        if "/" in path.name or "\\" in path.name:
            raise BackupIntegrityError("Ungültiger systemd-Unitname im Maskenvertrag")
        path_text = str(path)
        if path_text in seen:
            raise BackupIntegrityError("Doppelter systemd-Maskeneintrag im Manifest")
        seen.add(path_text)
        state = str(raw.get("state") or "")
        target = raw.get("target")
        if state == "masked":
            if target != "/dev/null":
                raise BackupIntegrityError("Systemd-Maske zeigt nicht kanonisch auf /dev/null")
        elif state == "unmasked":
            if target is not None:
                raise BackupIntegrityError("Unmaskierter systemd-Eintrag darf kein Ziel besitzen")
        else:
            raise BackupIntegrityError("Ungültiger systemd-Maskenzustand im Manifest")
        normalized.append({"path": path_text, "state": state, "target": target})
    normalized.sort(key=lambda item: str(item["path"]))
    if not normalized:
        raise BackupIntegrityError("Systemd-Maskenumfang darf nicht leer sein")
    return normalized


def build_systemd_mask_state_contract(entries: Iterable[Dict[str, object]]) -> Dict[str, object]:
    """Erzeugt einen deterministischen, eigenständig gehashten Maskenvertrag."""

    normalized = _normalized_systemd_mask_entries(entries)
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return {
        "schema": SYSTEMD_MASK_STATE_SCHEMA,
        "algorithm": "sha256",
        "entries": normalized,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def verify_systemd_mask_state_contract(value: object) -> Dict[str, object]:
    """Prüft inneren Hash und exaktes Schema eines systemd-Maskenvertrags."""

    if not isinstance(value, dict) or set(value) != {"schema", "algorithm", "entries", "sha256"}:
        raise BackupIntegrityError("Ungültiger systemd-Maskenzustandsvertrag")
    if value.get("schema") != SYSTEMD_MASK_STATE_SCHEMA or value.get("algorithm") != "sha256":
        raise BackupIntegrityError("Nicht unterstützter systemd-Maskenzustandsvertrag")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise BackupIntegrityError("Systemd-Maskeneinträge müssen eine Liste sein")
    normalized = _normalized_systemd_mask_entries(entries)
    expected = build_systemd_mask_state_contract(normalized)
    if value.get("sha256") != expected["sha256"] or entries != normalized:
        raise BackupIntegrityError("Systemd-Maskenzustand stimmt nicht mit seiner SHA-256 überein")
    return expected


@dataclass(frozen=True)
class PersistentSource:
    """A source file or directory included in the recovery contract."""

    category: str
    source: Path
    exclude_top_level: Tuple[str, ...] = field(default_factory=tuple)
    exclude_anywhere: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PersistentSourceSizeEstimate:
    """Rein lesende Größenabschätzung für einen PersistentSource-Satz.

    ``payload_bytes`` ist die Summe der logischen ``st_size``-Werte aller
    Dateien, die der Backup-Kopierer tatsächlich berücksichtigen würde.
    ``metadata_bytes`` reserviert zusätzlich einen kleinen konservativen
    Betrag für Zielverzeichnisse, Inodes, Blockrundung und Manifestdateien.
    """

    payload_bytes: int
    metadata_bytes: int
    file_count: int
    directory_count: int
    present_source_count: int
    missing_source_count: int

    @property
    def total_bytes(self) -> int:
        """Gesamtbedarf aus Nutzdaten und konservativem Metadatenaufschlag."""

        return int(self.payload_bytes) + int(self.metadata_bytes)


@dataclass(frozen=True)
class QuiescedOverlayRestoreGuard:
    """Bindet ein Zustands-Overlay an genau eine Update-Transaktion.

    Der Guard wird beim Erzeugen des Overlays aus bereits verifizierten
    Manifesten und nofollow geöffneten Verzeichnissen abgeleitet. Ein Restore
    akzeptiert weder einen frei konstruierten Pfad noch nur eine Backup-ID,
    sondern prüft die komplette Bindung erneut, bevor eine Zieldatei angelegt,
    ersetzt oder entfernt wird.
    """

    transaction_id: str
    overlay_dir: str
    overlay_dev: int
    overlay_ino: int
    backup_id: str
    manifest_sha256: str
    install_root: str
    parent_backup_dir: str
    parent_backup_dev: int
    parent_backup_ino: int
    parent_backup_id: str
    parent_backup_manifest_sha256: str
    collection_dir: str
    collection_dev: int
    collection_ino: int


def _lexical_absolute(path: PathValue) -> Path:
    raw = os.path.expanduser(str(path or ""))
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise BackupIntegrityError("Pfad muss absolut sein: {!r}".format(raw))
    if ".." in candidate.parts:
        raise BackupIntegrityError("Pfad darf keine '..'-Komponente enthalten: {}".format(candidate))
    return Path(os.path.normpath(raw))


def _normalized_transaction_id(value: object) -> str:
    transaction_id = str(value or "")
    if not _SHA256_RE.fullmatch(transaction_id):
        raise BackupIntegrityError(
            "Quiesced-Overlay besitzt keine gültige Transaktions-ID"
        )
    return transaction_id


def _normalized_backup_id(value: object, *, label: str = "Backup-ID") -> str:
    backup_id = str(value or "")
    if (
        not backup_id
        or len(backup_id) > 128
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", backup_id) is None
    ):
        raise BackupIntegrityError("{} ist ungültig".format(label))
    return backup_id


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _is_private_local_entry(name: str) -> bool:
    """Keep every hidden local workspace directory outside backup/restore."""

    return str(name).startswith(".") and str(name).endswith("_local")


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _assert_no_symlink_components(path: PathValue, allow_missing_tail: bool = False) -> Path:
    candidate = _lexical_absolute(path)
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(str(current))
        except FileNotFoundError:
            if allow_missing_tail:
                return candidate
            raise BackupIntegrityError("Pfadkomponente fehlt: {}".format(current))
        if stat.S_ISLNK(metadata.st_mode):
            raise BackupIntegrityError("Symlink-Komponente ist nicht erlaubt: {}".format(current))
    return candidate


def _open_directory_nofollow(path: PathValue) -> int:
    """Open a directory component-by-component without following symlinks."""

    candidate = _lexical_absolute(path)
    flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW
    descriptor = os.open(candidate.anchor, flags)
    try:
        for part in candidate.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise BackupIntegrityError("Kein Verzeichnis: {}".format(candidate))
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_regular_file_nofollow(path: PathValue) -> Tuple[int, os.stat_result]:
    candidate = _lexical_absolute(path)
    parent_descriptor = _open_directory_nofollow(candidate.parent)
    try:
        before = os.stat(candidate.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise BackupIntegrityError("Nur regulaere Dateien sind erlaubt: {}".format(candidate))
        descriptor = os.open(candidate.name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            os.close(descriptor)
            raise BackupIntegrityError("Quelldatei wurde waehrend des Oeffnens ausgetauscht: {}".format(candidate))
        return descriptor, opened
    finally:
        os.close(parent_descriptor)


def _ensure_directory_tree(path: PathValue, mode: int = 0o700) -> Path:
    candidate = _lexical_absolute(path)
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(str(current))
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise BackupIntegrityError("Unsichere Verzeichniskomponente: {}".format(current))
        except FileNotFoundError:
            os.mkdir(str(current), mode)
            metadata = os.lstat(str(current))
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise BackupIntegrityError("Verzeichniskomponente konnte nicht sicher erstellt werden: {}".format(current))
    return candidate


def sha256_file(path: PathValue) -> str:
    descriptor, _metadata = _open_regular_file_nofollow(path)
    digest = hashlib.sha256()
    try:
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _read_private_ml_entry(
    directory_descriptor: int,
    name: str,
    owner_uid: int,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> bytes:
    before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_uid != owner_uid
        or before.st_nlink != 1
        or (before.st_size < 1 and not allow_empty)
        or before.st_size > maximum
    ):
        raise BackupIntegrityError("Unsicherer privater ML-Eintrag: {}".format(name))
    descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=directory_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise BackupIntegrityError("Privater ML-Eintrag wurde beim Oeffnen ausgetauscht: {}".format(name))
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        before_signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if len(payload) > maximum or len(payload) != before.st_size or before_signature != after_signature:
            raise BackupIntegrityError("Privater ML-Eintrag wurde waehrend des Lesens veraendert: {}".format(name))
        return bytes(payload)
    finally:
        os.close(descriptor)


def _private_ml_lock_requires_normalization(
    directory_descriptor: int,
    owner_uid: int,
    owner_gid: int,
) -> bool:
    """Prüft einen reparierbaren Alt-Lock vollständig, ohne ihn zu verändern."""

    before = os.stat(
        ML_MODEL_LOCK_NAME,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > 64 * 1024
        or before.st_uid not in {0, owner_uid}
    ):
        raise BackupIntegrityError(
            "Unsicherer privater ML-Eintrag: {}".format(ML_MODEL_LOCK_NAME)
        )
    descriptor = os.open(
        ML_MODEL_LOCK_NAME,
        os.O_RDONLY | _NOFOLLOW,
        dir_fd=directory_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise BackupIntegrityError(
                "Privater ML-Eintrag wurde beim Öffnen ausgetauscht: {}".format(
                    ML_MODEL_LOCK_NAME
                )
            )
        payload_size = 0
        while payload_size <= 64 * 1024:
            chunk = os.read(descriptor, min(64 * 1024 + 1 - payload_size, 64 * 1024))
            if not chunk:
                break
            payload_size += len(chunk)
        after = os.fstat(descriptor)
        before_signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if payload_size != before.st_size or before_signature != after_signature:
            raise BackupIntegrityError(
                "Privater ML-Eintrag wurde während des Lesens verändert: {}".format(
                    ML_MODEL_LOCK_NAME
                )
            )
        return (
            before.st_uid != owner_uid
            or before.st_gid != owner_gid
            or stat.S_IMODE(before.st_mode) != 0o600
        )
    finally:
        os.close(descriptor)


def normalize_private_ml_lock_metadata(
    model_root: PathValue = PRIVATE_ML_ROOT,
    *,
    expected_uid: int,
    expected_gid: int,
    timeout_s: float = 10.0,
) -> Dict[str, object]:
    """Normalisiert ausschließlich eine vorhandene, eindeutig sichere ML-Sperrdatei."""

    owner_uid = int(expected_uid)
    owner_gid = int(expected_gid)
    if owner_uid < 0 or owner_gid < 0:
        raise BackupIntegrityError("ML-Sperrdatei besitzt keine gültige Zielidentität")
    if fcntl is None:
        raise BackupIntegrityError("ML-Sperrdatei kann auf diesem System nicht verriegelt werden")

    root = _lexical_absolute(model_root)
    if Path(os.path.realpath(str(root))) != root:
        raise BackupIntegrityError("Privater ML-Modellpfad ist nicht kanonisch")

    parent_descriptor = _open_directory_nofollow(root.parent)
    try:
        directory_before = os.stat(root.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            stat.S_ISLNK(directory_before.st_mode)
            or not stat.S_ISDIR(directory_before.st_mode)
            or stat.S_IMODE(directory_before.st_mode) != 0o700
            or directory_before.st_uid != owner_uid
            or directory_before.st_gid != owner_gid
        ):
            raise BackupIntegrityError(
                "Privates ML-Modellverzeichnis ist für die Sperrreparatur nicht eindeutig gebunden"
            )
        directory_descriptor = os.open(
            root.name,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        directory_opened = os.fstat(directory_descriptor)
        if (directory_opened.st_dev, directory_opened.st_ino) != (
            directory_before.st_dev,
            directory_before.st_ino,
        ):
            os.close(directory_descriptor)
            raise BackupIntegrityError(
                "Privates ML-Modellverzeichnis wurde beim Öffnen ausgetauscht"
            )
    finally:
        os.close(parent_descriptor)

    lock_descriptor = None
    locked = False
    try:
        try:
            path_before = os.stat(
                ML_MODEL_LOCK_NAME,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return {"state": "absent", "changed": False, "root": str(root)}
        if (
            not stat.S_ISREG(path_before.st_mode)
            or path_before.st_nlink != 1
            or path_before.st_size > 64 * 1024
            or path_before.st_uid not in {0, owner_uid}
        ):
            raise BackupIntegrityError(
                "ML-Sperrdatei ist kein sicher normalisierbarer Altbestand"
            )
        try:
            lock_descriptor = os.open(
                ML_MODEL_LOCK_NAME,
                os.O_RDWR | _NOFOLLOW,
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            return {"state": "absent", "changed": False, "root": str(root)}
        except OSError as exc:
            raise BackupIntegrityError(
                "ML-Sperrdatei ist keine eindeutig öffnbare reguläre Datei"
            ) from exc

        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise BackupIntegrityError(
                        "ML-Sperrdatei blieb während der Rechteprüfung belegt"
                    ) from exc
                time.sleep(0.1)

        before = os.fstat(lock_descriptor)
        if (path_before.st_dev, path_before.st_ino) != (before.st_dev, before.st_ino):
            raise BackupIntegrityError("ML-Sperrdatei wurde beim Öffnen ausgetauscht")
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > 64 * 1024
            or before.st_uid not in {0, owner_uid}
        ):
            raise BackupIntegrityError(
                "ML-Sperrdatei ist kein sicher normalisierbarer Altbestand"
            )

        changed = (
            before.st_uid != owner_uid
            or before.st_gid != owner_gid
            or stat.S_IMODE(before.st_mode) != 0o600
        )
        if before.st_uid != owner_uid or before.st_gid != owner_gid:
            os.fchown(lock_descriptor, owner_uid, owner_gid)
        if stat.S_IMODE(before.st_mode) != 0o600:
            os.fchmod(lock_descriptor, 0o600)
        if changed:
            os.fsync(lock_descriptor)

        after = os.fstat(lock_descriptor)
        path_after = os.stat(
            ML_MODEL_LOCK_NAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            (after.st_dev, after.st_ino, after.st_size)
            != (before.st_dev, before.st_ino, before.st_size)
            or after.st_nlink != 1
            or after.st_uid != owner_uid
            or after.st_gid != owner_gid
            or stat.S_IMODE(after.st_mode) != 0o600
            or (path_after.st_dev, path_after.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise BackupIntegrityError(
                "ML-Sperrdatei konnte nicht eindeutig normalisiert werden"
            )
        return {"state": "ready", "changed": changed, "root": str(root)}
    finally:
        if lock_descriptor is not None:
            try:
                if locked:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)
        os.close(directory_descriptor)


def validate_private_ml_store(
    model_root: PathValue = PRIVATE_ML_ROOT,
    *,
    expected_uid: Optional[int] = None,
    allow_missing: bool = True,
    allow_repairable_lock: bool = False,
) -> Dict[str, object]:
    """Validate the non-web-writable ML store without deserializing its model."""

    root = _lexical_absolute(model_root)
    try:
        parent_descriptor = _open_directory_nofollow(root.parent)
    except FileNotFoundError:
        if allow_missing:
            return {"state": "missing", "root": str(root), "files": 0}
        raise BackupIntegrityError("Privates ML-Modellverzeichnis fehlt: {}".format(root))
    try:
        try:
            before = os.stat(root.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return {"state": "missing", "root": str(root), "files": 0}
            raise BackupIntegrityError("Privates ML-Modellverzeichnis fehlt: {}".format(root))
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise BackupIntegrityError("Privater ML-Modellpfad ist kein echtes Verzeichnis: {}".format(root))
        if stat.S_IMODE(before.st_mode) != 0o700:
            raise BackupIntegrityError("Privates ML-Modellverzeichnis besitzt nicht Modus 0700")
        owner_uid = int(before.st_uid)
        owner_gid = int(before.st_gid)
        if expected_uid is not None and owner_uid != int(expected_uid):
            raise BackupIntegrityError("Privates ML-Modellverzeichnis besitzt einen falschen Owner")
        try:
            if owner_uid == pwd.getpwnam("www-data").pw_uid:
                raise BackupIntegrityError("Privates ML-Modellverzeichnis darf nicht www-data gehoeren")
        except KeyError:
            pass
        current = root.parent
        while True:
            parent_metadata = os.lstat(str(current))
            if (
                stat.S_ISLNK(parent_metadata.st_mode)
                or not stat.S_ISDIR(parent_metadata.st_mode)
                or parent_metadata.st_uid not in {0, owner_uid}
            ):
                raise BackupIntegrityError("Unsichere Elternkette des privaten ML-Stores")
            if parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                if parent_metadata.st_uid == 0 and parent_metadata.st_mode & stat.S_ISVTX:
                    break
                raise BackupIntegrityError("Beschreibbare Elternkette des privaten ML-Stores")
            if current == Path(current.anchor):
                break
            current = current.parent

        descriptor = os.open(root.name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            os.close(descriptor)
            raise BackupIntegrityError("Privates ML-Modellverzeichnis wurde beim Oeffnen ausgetauscht")
    finally:
        os.close(parent_descriptor)

    try:
        names = sorted(os.listdir(descriptor))
        model_names = []
        repairable_lock = False
        for name in names:
            if name in {ML_MODEL_MANIFEST_NAME, ML_MODEL_LOCK_NAME}:
                maximum = 64 * 1024
            elif _ML_MODEL_FILE_RE.fullmatch(name):
                model_names.append(name)
                maximum = ML_MODEL_MAX_BYTES
            else:
                raise BackupIntegrityError("Nicht manifestgebundener Eintrag im privaten ML-Store: {}".format(name))
            if name == ML_MODEL_LOCK_NAME and allow_repairable_lock:
                repairable_lock = _private_ml_lock_requires_normalization(
                    descriptor,
                    owner_uid,
                    owner_gid,
                )
                continue
            _read_private_ml_entry(
                descriptor,
                name,
                owner_uid,
                maximum,
                allow_empty=(name == ML_MODEL_LOCK_NAME),
            )

        if ML_MODEL_MANIFEST_NAME not in names:
            if model_names:
                raise BackupIntegrityError("ML-Modellartefakt ohne Manifest ist nicht zulaessig")
            return {
                "state": "untrained",
                "root": str(root),
                "files": len(names),
                "uid": owner_uid,
                "repairable_lock": repairable_lock,
            }

        manifest_payload = _read_private_ml_entry(
            descriptor, ML_MODEL_MANIFEST_NAME, owner_uid, 64 * 1024
        )
        try:
            manifest = json.loads(manifest_payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BackupIntegrityError("Privates ML-Manifest ist nicht lesbar") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != ML_MODEL_SCHEMA_VERSION
            or manifest.get("format") != ML_MODEL_FORMAT
        ):
            raise BackupIntegrityError("Privates ML-Manifest besitzt einen unbekannten Vertrag")
        model_name = str(manifest.get("model_file") or "")
        model_hash = str(manifest.get("model_sha256") or "")
        match = _ML_MODEL_FILE_RE.fullmatch(model_name)
        if match is None or match.group(1) != model_hash or model_name not in model_names:
            raise BackupIntegrityError("Privates ML-Manifest verweist nicht exakt auf sein Modell")

        for name in model_names:
            expected_hash = _ML_MODEL_FILE_RE.fullmatch(name).group(1)  # type: ignore[union-attr]
            payload = _read_private_ml_entry(descriptor, name, owner_uid, ML_MODEL_MAX_BYTES)
            if hashlib.sha256(payload).hexdigest() != expected_hash:
                raise BackupIntegrityError("Privates ML-Artefakt stimmt nicht mit seinem Dateihash ueberein")
        return {
            "state": "ready",
            "root": str(root),
            "files": len(names),
            "uid": owner_uid,
            "model_sha256": model_hash,
            "repairable_lock": repairable_lock,
        }
    finally:
        os.close(descriptor)


def _verify_private_ml_backup_contract(backup: Path, manifest: Dict[str, object]) -> None:
    """Bind restored ML files to their inner manifest and reject legacy Pickle."""

    ml_directory_metadata = None
    for record in manifest.get("sources", []):  # type: ignore[union-attr]
        if not isinstance(record, dict) or record.get("source_type") != "directory" or not record.get("present"):
            continue
        source = _lexical_absolute(str(record.get("source") or ""))
        if source == PRIVATE_ML_ROOT:
            ml_directory_metadata = record
            break
        if _is_within(PRIVATE_ML_ROOT, source):
            relative = PRIVATE_ML_ROOT.relative_to(source).as_posix()
            for directory in record.get("directories", []):
                if isinstance(directory, dict) and str(directory.get("path") or "") == relative:
                    ml_directory_metadata = directory
                    break
        if ml_directory_metadata is not None:
            break
    if ml_directory_metadata is not None and int(ml_directory_metadata.get("mode", -1)) != 0o700:
        raise BackupIntegrityError("Privates ML-Verzeichnis im Backup besitzt nicht Modus 0700")
    if ml_directory_metadata is not None:
        directory_uid = int(ml_directory_metadata.get("uid", -1))
        try:
            if directory_uid == pwd.getpwnam("www-data").pw_uid:
                raise BackupIntegrityError("Privates ML-Verzeichnis im Backup darf nicht www-data gehoeren")
        except KeyError:
            pass

    by_restore_path: Dict[str, Tuple[Dict[str, object], Path]] = {}
    for raw in manifest.get("files", []):  # type: ignore[union-attr]
        if not isinstance(raw, dict) or not raw.get("restore_path"):
            continue
        restore_path = _lexical_absolute(str(raw["restore_path"]))
        if restore_path == LEGACY_ML_MODEL:
            raise BackupIntegrityError("Legacy-ML-Pickle darf nicht in ein Systembackup gelangen")
        if restore_path.parent != PRIVATE_ML_ROOT:
            continue
        if str(restore_path) in by_restore_path:
            raise BackupIntegrityError("Doppelter privater ML-Restorepfad")
        by_restore_path[str(restore_path)] = (raw, backup / _safe_relative_path(str(raw["path"])))

    entries_by_name = {Path(path).name: value for path, value in by_restore_path.items()}
    allowed_names = {ML_MODEL_MANIFEST_NAME, ML_MODEL_LOCK_NAME}
    model_names = []
    owner_uid = None
    for name, (entry, _path) in entries_by_name.items():
        if name not in allowed_names and _ML_MODEL_FILE_RE.fullmatch(name) is None:
            raise BackupIntegrityError("Nicht manifestgebundener ML-Eintrag im Backup: {}".format(name))
        if _ML_MODEL_FILE_RE.fullmatch(name):
            model_names.append(name)
        if int(entry.get("mode", -1)) != 0o600:
            raise BackupIntegrityError("Privater ML-Backupeintrag besitzt nicht Modus 0600")
        entry_uid = int(entry.get("uid", -1))
        if owner_uid is None:
            owner_uid = entry_uid
        elif entry_uid != owner_uid:
            raise BackupIntegrityError("Private ML-Backupeintraege besitzen verschiedene Owner")
    if entries_by_name and ml_directory_metadata is None:
        raise BackupIntegrityError("Verzeichnismetadaten des privaten ML-Stores fehlen im Backup")
    if owner_uid is not None and int(ml_directory_metadata.get("uid", -1)) != owner_uid:
        raise BackupIntegrityError("ML-Verzeichnis und ML-Dateien besitzen im Backup verschiedene Owner")

    if model_names and ML_MODEL_MANIFEST_NAME not in entries_by_name:
        raise BackupIntegrityError("ML-Modellartefakt im Backup besitzt kein Manifest")
    if ML_MODEL_MANIFEST_NAME not in entries_by_name:
        return

    manifest_entry, manifest_path = entries_by_name[ML_MODEL_MANIFEST_NAME]
    try:
        ml_manifest = json.loads(_read_small_file_bytes(manifest_path, 64 * 1024).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("ML-Manifest im Backup ist nicht lesbar") from exc
    if (
        not isinstance(ml_manifest, dict)
        or ml_manifest.get("schema_version") != ML_MODEL_SCHEMA_VERSION
        or ml_manifest.get("format") != ML_MODEL_FORMAT
    ):
        raise BackupIntegrityError("ML-Manifest im Backup besitzt einen unbekannten Vertrag")
    model_name = str(ml_manifest.get("model_file") or "")
    expected_hash = str(ml_manifest.get("model_sha256") or "")
    match = _ML_MODEL_FILE_RE.fullmatch(model_name)
    if match is None or match.group(1) != expected_hash or model_name not in entries_by_name:
        raise BackupIntegrityError("ML-Manifest im Backup verweist nicht exakt auf sein Modell")
    model_entry, model_path = entries_by_name[model_name]
    if str(model_entry.get("sha256") or "") != expected_hash or sha256_file(model_path) != expected_hash:
        raise BackupIntegrityError("ML-Modell im Backup stimmt nicht mit seinem inneren Manifest ueberein")

    for name in model_names:
        entry, path = entries_by_name[name]
        file_hash = _ML_MODEL_FILE_RE.fullmatch(name).group(1)  # type: ignore[union-attr]
        if str(entry.get("sha256") or "") != file_hash or sha256_file(path) != file_hash:
            raise BackupIntegrityError("Inhaltsadressiertes ML-Artefakt im Backup besitzt falsche Bytes")


def _protected_backup_locations(install_root: Path) -> Set[Path]:
    protected = {
        Path("/"), Path("/home"), Path("/root"), Path("/etc"), Path("/usr"),
        Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64"), Path("/proc"),
        Path("/sys"), Path("/dev"), Path("/run"), Path("/var"), Path("/var/www"),
        install_root, install_root.parent,
    }
    try:
        protected.add(_lexical_absolute(Path.home()))
    except BackupIntegrityError:
        pass
    return protected


def _account_home_boundaries() -> Set[Path]:
    """Return every absolute account home from the local account database."""

    try:
        accounts = pwd.getpwall()
    except (KeyError, OSError) as exc:
        raise BackupIntegrityError("Konten-Homegrenzen konnten nicht gelesen werden") from exc
    boundaries: Set[Path] = {Path("/home"), Path("/root")}
    for account in accounts:
        raw = str(getattr(account, "pw_dir", "") or "").strip()
        if not raw or raw == "/" or not os.path.isabs(raw):
            continue
        try:
            boundaries.add(_lexical_absolute(raw))
        except BackupIntegrityError as exc:
            raise BackupIntegrityError("Ungueltige Konten-Homegrenze") from exc
    return boundaries


def _validate_backup_root_path(backup_root: PathValue, install_root: PathValue) -> Tuple[Path, Path]:
    root = _lexical_absolute(backup_root)
    install = _lexical_absolute(install_root)
    _assert_no_symlink_components(install)
    if root.name not in BACKUP_ROOT_NAMES:
        raise BackupIntegrityError(
            "Backup-Root muss ein eigener Unterbaum namens {} sein.".format(
                " oder ".join(sorted(BACKUP_ROOT_NAMES))
            )
        )
    if root in _protected_backup_locations(install):
        raise BackupIntegrityError("Backup-Root ist ein geschuetzter Home-/Install-/Systempfad: {}".format(root))
    home_boundaries = _account_home_boundaries()
    try:
        home_boundaries.add(_lexical_absolute(Path.home()))
    except BackupIntegrityError:
        pass
    if any(_is_within(root, boundary) for boundary in home_boundaries):
        raise BackupIntegrityError("Backup-Root darf nicht innerhalb eines Benutzer-Home liegen.")
    if _is_within(root, install) or _is_within(install, root):
        raise BackupIntegrityError("Backup-Root und Installationsbaum duerfen sich nicht ueberlappen.")
    for forbidden_parent in (Path("/etc"), Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"), Path("/proc"), Path("/sys"), Path("/dev"), Path("/run"), Path("/var/www")):
        if _is_within(root, forbidden_parent):
            raise BackupIntegrityError("Backup-Root liegt unter einem geschuetzten Systempfad: {}".format(root))
    _assert_no_symlink_components(root.parent)
    if root.exists() or root.is_symlink():
        _assert_no_symlink_components(root)
    return root, install


def _root_marker_payload(install: Path) -> Dict[str, object]:
    return {
        "schema": ROOT_MARKER_SCHEMA,
        "purpose": "e3dc-control-dedicated-backup-root",
        "install_root": str(install),
    }


def _descriptor_has_unsafe_backup_xattrs(descriptor: int) -> bool:
    """Ein Backup-Authority-Pfad darf keine ungebundenen xattrs tragen."""

    try:
        return bool(os.listxattr(descriptor))
    except AttributeError:
        return True
    except OSError as exc:
        if exc.errno in {
            getattr(errno, "ENODATA", -1),
            getattr(errno, "ENOTSUP", -1),
            getattr(errno, "EOPNOTSUPP", -1),
        }:
            return False
        return True


def _open_root_controlled_backup_directory_chain(
    path: PathValue,
    *,
    leaf_mode: Optional[int] = None,
) -> int:
    """Öffnet jede Pfadkomponente nofollow und bindet Root-Besitz und Inodes."""

    candidate = _lexical_absolute(path)
    if not _NOFOLLOW or not _DIRECTORY:
        raise BackupIntegrityError("Backup-Root benötigt O_NOFOLLOW und O_DIRECTORY")
    flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(candidate.anchor, flags)
    try:
        anchor_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(anchor_metadata.st_mode)
            or anchor_metadata.st_uid != 0
            or anchor_metadata.st_gid != 0
            or anchor_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or _descriptor_has_unsafe_backup_xattrs(descriptor)
        ):
            raise BackupIntegrityError("Backup-Root-Anker ist nicht root-kontrolliert")
        current = Path(candidate.anchor)
        components = candidate.parts[1:]
        for index, component in enumerate(components):
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            current = current / component
            try:
                opened = os.fstat(next_descriptor)
                named_after = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                expected_leaf_mode = (
                    leaf_mode if index == len(components) - 1 else None
                )
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or opened.st_uid != 0
                    or opened.st_gid != 0
                    or opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                    or (
                        expected_leaf_mode is not None
                        and stat.S_IMODE(opened.st_mode) != expected_leaf_mode
                    )
                    or _descriptor_has_unsafe_backup_xattrs(next_descriptor)
                    or (before.st_dev, before.st_ino)
                    != (opened.st_dev, opened.st_ino)
                    or (named_after.st_dev, named_after.st_ino)
                    != (opened.st_dev, opened.st_ino)
                ):
                    raise BackupIntegrityError(
                        "Backup-Root-Komponente ist nicht root-kontrolliert: {}".format(
                            current
                        )
                    )
            except Exception:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        root_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != 0
            or root_metadata.st_gid != 0
            or root_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (leaf_mode is not None and stat.S_IMODE(root_metadata.st_mode) != leaf_mode)
            or _descriptor_has_unsafe_backup_xattrs(descriptor)
        ):
            raise BackupIntegrityError(
                "Backup-Root ist nicht root-kontrolliert: {}".format(candidate)
            )
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_root_marker(root: Path) -> Dict[str, object]:
    marker = root / ROOT_MARKER_NAME
    descriptor, metadata = _open_regular_file_nofollow(marker)
    try:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or _descriptor_has_unsafe_backup_xattrs(descriptor)
        ):
            raise BackupIntegrityError(
                "Backup-Root-Marker ist nicht root:root 0600 gebunden."
            )
        identity = (
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
        data = b""
        while True:
            block = os.read(descriptor, 65536)
            if not block:
                break
            data += block
            if len(data) > 65536:
                raise BackupIntegrityError("Backup-Root-Marker ist unplausibel gross.")
        after = os.fstat(descriptor)
        named_after = os.lstat(str(marker))
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
            after.st_uid,
            after.st_gid,
            stat.S_IMODE(after.st_mode),
        ) or identity != (
            named_after.st_dev,
            named_after.st_ino,
            named_after.st_size,
            named_after.st_mtime_ns,
            named_after.st_ctime_ns,
            named_after.st_nlink,
            named_after.st_uid,
            named_after.st_gid,
            stat.S_IMODE(named_after.st_mode),
        ):
            raise BackupIntegrityError("Backup-Root-Marker driftete beim Readback.")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("Backup-Root-Marker ist unlesbar: {}".format(exc))
    if not isinstance(payload, dict):
        raise BackupIntegrityError("Backup-Root-Marker ist ungueltig.")
    return payload


def configured_backup_root(install_root: PathValue) -> Path:
    install = _lexical_absolute(install_root)
    configured = os.environ.get("E3DC_BACKUP_ROOT", "").strip()
    root = _lexical_absolute(configured) if configured else DEFAULT_BACKUP_ROOT
    root, install = _validate_backup_root_path(root, install)
    if root.exists():
        root_descriptor = _open_root_controlled_backup_directory_chain(
            root,
            leaf_mode=0o700,
        )
        try:
            entries = set(os.listdir(root_descriptor))
        finally:
            os.close(root_descriptor)
        if ROOT_MARKER_NAME not in entries:
            if entries:
                raise BackupIntegrityError("Bestehender Backup-Root ist kein markierter E3DC-Unterbaum.")
        else:
            marker = _read_root_marker(root)
            if marker != _root_marker_payload(install):
                raise BackupIntegrityError("Backup-Root gehoert zu einer anderen Installation.")
    return root


def ensure_external_backup_root(backup_root: PathValue, install_root: PathValue) -> Path:
    root, install = _validate_backup_root_path(backup_root, install_root)
    if not root.exists():
        parent_descriptor = _open_root_controlled_backup_directory_chain(root.parent)
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    root = _assert_no_symlink_components(root)
    root_descriptor = _open_root_controlled_backup_directory_chain(
        root,
        leaf_mode=0o700,
    )
    entries = set(os.listdir(root_descriptor))
    expected = _root_marker_payload(install)
    try:
        if ROOT_MARKER_NAME in entries:
            if _read_root_marker(root) != expected:
                raise BackupIntegrityError("Backup-Root gehört zu einer anderen Installation.")
        else:
            if entries:
                raise BackupIntegrityError("Fremdordner darf nicht als Backup-Root initialisiert werden.")
            descriptor = os.open(
                ROOT_MARKER_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
                dir_fd=root_descriptor,
            )
            try:
                os.write(
                    descriptor,
                    (json.dumps(expected, sort_keys=True) + "\n").encode("utf-8"),
                )
                os.fchown(descriptor, 0, 0)
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(root_descriptor)
            if _read_root_marker(root) != expected:
                raise BackupIntegrityError("Backup-Root-Marker konnte nicht gebunden werden.")
    finally:
        os.close(root_descriptor)
    return root


def default_backup_root(install_root: PathValue) -> Path:
    install = _lexical_absolute(install_root)
    configured = os.environ.get("E3DC_BACKUP_ROOT", "").strip()
    root = _lexical_absolute(configured) if configured else DEFAULT_BACKUP_ROOT
    return ensure_external_backup_root(root, install)


def validate_existing_backup_root(backup_root: PathValue, install_root: PathValue) -> Path:
    """Validate an initialized dedicated root without mutating it."""

    root, install = _validate_backup_root_path(backup_root, install_root)
    if not root.exists():
        raise BackupIntegrityError("Backup-Root existiert nicht: {}".format(root))
    descriptor = _open_root_controlled_backup_directory_chain(root, leaf_mode=0o700)
    os.close(descriptor)
    marker = _read_root_marker(root)
    if marker != _root_marker_payload(install):
        raise BackupIntegrityError("Backup-Root-Marker stimmt nicht mit der Installation ueberein.")
    return root


def _validate_category(category: str) -> str:
    value = str(category or "").strip().lower()
    if not _CATEGORY_RE.fullmatch(value):
        raise BackupIntegrityError("Ungueltige Backup-Kategorie: {!r}".format(category))
    return value


def _copy_fd_to_path(source_descriptor: int, destination: Path, source_mode: int) -> tuple[int, str]:
    _ensure_directory_tree(destination.parent)
    descriptor = os.open(
        str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600
    )
    hasher = hashlib.sha256()
    total_size = 0
    try:
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        while True:
            block = os.read(source_descriptor, 1024 * 1024)
            if not block:
                break
            hasher.update(block)
            total_size += len(block)
            view = memoryview(block)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(str(destination), source_mode & 0o777)
    return total_size, hasher.hexdigest()


def _copy_sqlite_fd(source_descriptor: int, destination: Path, source_mode: int) -> tuple[int, str]:
    _ensure_directory_tree(destination.parent)
    source_uri = "file:/proc/self/fd/{}?mode=ro".format(source_descriptor)
    try:
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source_db, closing(
            sqlite3.connect(str(destination), timeout=30)
        ) as destination_db:
            source_db.backup(destination_db)
    except sqlite3.Error as exc:
        _unlink_if_exists(destination)
        raise BackupIntegrityError("SQLite-Online-Backup fehlgeschlagen: {}".format(exc))
    os.chmod(str(destination), source_mode & 0o777)
    return destination.stat().st_size, sha256_file(destination)


def _archive_path(category: str, source_root: Path, relative: Path, root_is_file: bool) -> Path:
    if root_is_file:
        return Path("recovery") / category / Path(*source_root.parts[1:])
    return Path("recovery") / category / relative


def _persistent_entry_is_excluded(
    name: str,
    relative: Path,
    source: PersistentSource,
) -> bool:
    """Gemeinsamer Ausschlussvertrag für Kopie und Größenabschätzung."""

    if not relative.parts and _is_private_local_entry(name):
        return True
    if name in source.exclude_anywhere:
        return True
    return not relative.parts and name in source.exclude_top_level


def _sqlite_sidecar_is_excluded(directory_descriptor: int, name: str) -> bool:
    """Spiegelt exakt den WAL-/SHM-Ausschluss der SQLite-Online-Kopie."""

    if not name.endswith(("-wal", "-shm")):
        return False
    base_name = name.rsplit("-", 1)[0]
    try:
        base_metadata = os.stat(
            base_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(base_metadata.st_mode)
        and Path(base_name).suffix.lower() in {".db", ".sqlite", ".sqlite3"}
    )


def estimate_persistent_sources_size(
    sources: Sequence[PersistentSource],
) -> PersistentSourceSizeEstimate:
    """Schätzt die tatsächlich kopierte PersistentSource-Fläche read-only.

    Die Traversierung verwendet wie :func:`copy_persistent_sources`
    Verzeichnisdeskriptoren und ``O_NOFOLLOW``. Fehlende Quellen sind erlaubt;
    Symlinks, Spezialdateien, Hardlinks und während des Öffnens ausgetauschte
    Einträge führen fail-closed zu ``BackupIntegrityError``.
    """

    payload_bytes = 0
    file_count = 0
    directory_count = 0
    present_source_count = 0
    missing_source_count = 0
    restore_destinations: Set[str] = set()

    def account_open_file(
        source_path: Path,
        source_descriptor: int,
        metadata: os.stat_result,
    ) -> None:
        nonlocal payload_bytes, file_count
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BackupIntegrityError(
                "Backupquelle ist keine eigenständige reguläre Datei: {}".format(
                    source_path
                )
            )
        restore_text = str(source_path)
        if restore_text in restore_destinations:
            raise BackupIntegrityError(
                "Restore-Ziel ist doppelt definiert: {}".format(source_path)
            )
        restore_destinations.add(restore_text)
        size = int(metadata.st_size)
        if size < 0:
            raise BackupIntegrityError(
                "Backupquelle besitzt eine ungültige Dateigröße: {}".format(
                    source_path
                )
            )
        after = os.fstat(source_descriptor)
        if (
            (after.st_dev, after.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or after.st_size != metadata.st_size
            or after.st_mtime_ns != metadata.st_mtime_ns
            or after.st_ctime_ns != metadata.st_ctime_ns
            or after.st_uid != metadata.st_uid
            or after.st_gid != metadata.st_gid
            or stat.S_IMODE(after.st_mode) != stat.S_IMODE(metadata.st_mode)
        ):
            raise BackupIntegrityError(
                "Quelle driftete während der Backup-Größenabschätzung: {}".format(
                    source_path
                )
            )
        payload_bytes += size
        file_count += 1

    def walk_directory(
        source_root: Path,
        directory_descriptor: int,
        relative: Path,
        item: PersistentSource,
    ) -> None:
        nonlocal directory_count
        for name in sorted(os.listdir(directory_descriptor)):
            if _persistent_entry_is_excluded(name, relative, item):
                continue
            metadata = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            child_relative = relative / name
            child_path = source_root / child_relative
            if stat.S_ISLNK(metadata.st_mode):
                raise BackupIntegrityError(
                    "Symlink in Backupquelle ist nicht erlaubt: {}".format(
                        child_path
                    )
                )
            if stat.S_ISDIR(metadata.st_mode):
                child_descriptor = os.open(
                    name,
                    os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                    dir_fd=directory_descriptor,
                )
                try:
                    opened = os.fstat(child_descriptor)
                    if (
                        not stat.S_ISDIR(opened.st_mode)
                        or (opened.st_dev, opened.st_ino)
                        != (metadata.st_dev, metadata.st_ino)
                    ):
                        raise BackupIntegrityError(
                            "Quellverzeichnis wurde ausgetauscht: {}".format(
                                child_path
                            )
                        )
                    directory_count += 1
                    walk_directory(
                        source_root,
                        child_descriptor,
                        child_relative,
                        item,
                    )
                finally:
                    os.close(child_descriptor)
            elif stat.S_ISREG(metadata.st_mode):
                if _sqlite_sidecar_is_excluded(directory_descriptor, name):
                    continue
                file_descriptor = os.open(
                    name,
                    os.O_RDONLY | _NOFOLLOW,
                    dir_fd=directory_descriptor,
                )
                try:
                    opened = os.fstat(file_descriptor)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise BackupIntegrityError(
                            "Quelldatei wurde ausgetauscht: {}".format(child_path)
                        )
                    account_open_file(child_path, file_descriptor, opened)
                finally:
                    os.close(file_descriptor)
            else:
                raise BackupIntegrityError(
                    "Nicht regulärer Eintrag in Backupquelle: {}".format(
                        child_path
                    )
                )

    for source in sources:
        _validate_category(source.category)
        source_path = _lexical_absolute(source.source)
        try:
            parent_descriptor = _open_directory_nofollow(source_path.parent)
        except FileNotFoundError:
            missing_source_count += 1
            continue
        try:
            try:
                metadata = os.stat(
                    source_path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                missing_source_count += 1
                continue
            present_source_count += 1
            if stat.S_ISLNK(metadata.st_mode):
                raise BackupIntegrityError(
                    "Backupquelle darf kein Symlink sein: {}".format(source_path)
                )
            if stat.S_ISREG(metadata.st_mode):
                descriptor = os.open(
                    source_path.name,
                    os.O_RDONLY | _NOFOLLOW,
                    dir_fd=parent_descriptor,
                )
                try:
                    opened = os.fstat(descriptor)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise BackupIntegrityError(
                            "Backupquelle wurde ausgetauscht: {}".format(
                                source_path
                            )
                        )
                    account_open_file(source_path, descriptor, opened)
                finally:
                    os.close(descriptor)
            elif stat.S_ISDIR(metadata.st_mode):
                descriptor = os.open(
                    source_path.name,
                    os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                    dir_fd=parent_descriptor,
                )
                try:
                    opened = os.fstat(descriptor)
                    if (
                        not stat.S_ISDIR(opened.st_mode)
                        or (opened.st_dev, opened.st_ino)
                        != (metadata.st_dev, metadata.st_ino)
                    ):
                        raise BackupIntegrityError(
                            "Backupquelle wurde ausgetauscht: {}".format(
                                source_path
                            )
                        )
                    directory_count += 1
                    walk_directory(source_path, descriptor, Path(), source)
                finally:
                    os.close(descriptor)
            else:
                raise BackupIntegrityError(
                    "Backupquelle ist kein regulärer Pfad: {}".format(source_path)
                )
        finally:
            os.close(parent_descriptor)

    source_count = present_source_count + missing_source_count
    metadata_bytes = (
        BACKUP_ESTIMATE_FIXED_OVERHEAD_BYTES
        + source_count * BACKUP_ESTIMATE_SOURCE_OVERHEAD_BYTES
        + directory_count * BACKUP_ESTIMATE_DIRECTORY_OVERHEAD_BYTES
        + file_count * BACKUP_ESTIMATE_FILE_OVERHEAD_BYTES
    )
    return PersistentSourceSizeEstimate(
        payload_bytes=payload_bytes,
        metadata_bytes=metadata_bytes,
        file_count=file_count,
        directory_count=directory_count,
        present_source_count=present_source_count,
        missing_source_count=missing_source_count,
    )


def copy_persistent_sources(
    backup_dir: PathValue,
    sources: Sequence[PersistentSource],
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    """Copy recovery sources through no-follow directory descriptors."""

    backup = _assert_no_symlink_components(backup_dir)
    mapped_entries: List[Dict[str, object]] = []
    source_records: List[Dict[str, object]] = []
    restore_destinations: Set[str] = set()

    def copy_open_file(
        category: str,
        archive_root: Path,
        source_path: Path,
        source_descriptor: int,
        metadata: os.stat_result,
        relative: Path,
        root_is_file: bool,
    ) -> None:
        restore_text = str(source_path)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BackupIntegrityError(
                "Backupquelle ist keine eigenständige reguläre Datei: {}".format(
                    source_path
                )
            )
        if restore_text in restore_destinations:
            raise BackupIntegrityError("Restore-Ziel ist doppelt definiert: {}".format(source_path))
        restore_destinations.add(restore_text)
        archive_relative = _archive_path(category, archive_root, relative, root_is_file)
        destination = backup / archive_relative
        suffix = source_path.suffix.lower()
        sqlite_source = suffix in {".db", ".sqlite", ".sqlite3"}
        if sqlite_source:
            size, sha = _copy_sqlite_fd(source_descriptor, destination, stat.S_IMODE(metadata.st_mode))
        else:
            size, sha = _copy_fd_to_path(source_descriptor, destination, stat.S_IMODE(metadata.st_mode))
        after = os.fstat(source_descriptor)
        if (
            (after.st_dev, after.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
        ):
            raise BackupIntegrityError("Quelle wurde waehrend des Backups ausgetauscht: {}".format(source_path))
        if not sqlite_source and (
            after.st_size != metadata.st_size
            or after.st_mtime_ns != metadata.st_mtime_ns
            or after.st_ctime_ns != metadata.st_ctime_ns
            or after.st_uid != metadata.st_uid
            or after.st_gid != metadata.st_gid
            or stat.S_IMODE(after.st_mode) != stat.S_IMODE(metadata.st_mode)
            or size != metadata.st_size
        ):
            raise BackupIntegrityError(
                "Quelle wurde während des Backups in-place verändert: {}. "
                "Das Update hat noch keinen Dienst gestoppt; bitte den erneut "
                "ausgegebenen Updatebefehl nach Abschluss der laufenden Änderung starten.".format(
                    source_path
                )
            )
        mapped_entries.append({
            "backup_path": archive_relative.as_posix(),
            "restore_path": restore_text,
            "category": category,
            "restore_mode": stat.S_IMODE(metadata.st_mode),
            "restore_uid": int(metadata.st_uid),
            "restore_gid": int(metadata.st_gid),
            "size": size,
            "sha256": sha,
        })

    def walk_directory(
        category: str,
        source_root: Path,
        directory_descriptor: int,
        relative: Path,
        item: PersistentSource,
        seen_directories: List[Dict[str, int | str]],
    ) -> int:
        copied = 0
        for name in sorted(os.listdir(directory_descriptor)):
            if _persistent_entry_is_excluded(name, relative, item):
                continue
            metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            child_relative = relative / name
            child_path = source_root / child_relative
            if stat.S_ISLNK(metadata.st_mode):
                raise BackupIntegrityError("Symlink in Backupquelle ist nicht erlaubt: {}".format(child_path))
            if stat.S_ISDIR(metadata.st_mode):
                child_descriptor = os.open(
                    name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=directory_descriptor
                )
                try:
                    opened = os.fstat(child_descriptor)
                    if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                        raise BackupIntegrityError("Quellverzeichnis wurde ausgetauscht: {}".format(child_path))
                    seen_directories.append({
                        "path": child_relative.as_posix(),
                        "mode": stat.S_IMODE(opened.st_mode),
                        "uid": int(opened.st_uid),
                        "gid": int(opened.st_gid),
                    })
                    copied += walk_directory(
                        category,
                        source_root,
                        child_descriptor,
                        child_relative,
                        item,
                        seen_directories,
                    )
                finally:
                    os.close(child_descriptor)
            elif stat.S_ISREG(metadata.st_mode):
                if _sqlite_sidecar_is_excluded(directory_descriptor, name):
                    continue
                file_descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=directory_descriptor)
                try:
                    opened = os.fstat(file_descriptor)
                    if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                        raise BackupIntegrityError("Quelldatei wurde ausgetauscht: {}".format(child_path))
                    copy_open_file(category, source_root, child_path, file_descriptor, opened, child_relative, False)
                    copied += 1
                finally:
                    os.close(file_descriptor)
            else:
                raise BackupIntegrityError("Nicht regulaerer Eintrag in Backupquelle: {}".format(child_path))
        return copied

    for source in sources:
        category = _validate_category(source.category)
        source_path = _lexical_absolute(source.source)
        record: Dict[str, object] = {
            "category": category,
            "source": str(source_path),
            "present": False,
            "files": 0,
            "source_type": "missing",
            "exclude_top_level": list(source.exclude_top_level),
            "exclude_anywhere": list(source.exclude_anywhere),
            "directories": [],
        }
        source_records.append(record)
        try:
            parent_descriptor = _open_directory_nofollow(source_path.parent)
        except FileNotFoundError:
            continue
        try:
            try:
                metadata = os.stat(source_path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            record["present"] = True
            if stat.S_ISLNK(metadata.st_mode):
                raise BackupIntegrityError("Backupquelle darf kein Symlink sein: {}".format(source_path))
            if stat.S_ISREG(metadata.st_mode):
                record["source_type"] = "file"
                descriptor = os.open(source_path.name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_descriptor)
                try:
                    opened = os.fstat(descriptor)
                    if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                        raise BackupIntegrityError("Backupquelle wurde ausgetauscht: {}".format(source_path))
                    copy_open_file(category, source_path, source_path, descriptor, opened, Path(source_path.name), True)
                    record["files"] = 1
                finally:
                    os.close(descriptor)
            elif stat.S_ISDIR(metadata.st_mode):
                record["source_type"] = "directory"
                descriptor = os.open(
                    source_path.name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=parent_descriptor
                )
                try:
                    opened = os.fstat(descriptor)
                    if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                        raise BackupIntegrityError("Backupquelle wurde ausgetauscht: {}".format(source_path))
                    record["mode"] = stat.S_IMODE(opened.st_mode)
                    record["uid"] = int(opened.st_uid)
                    record["gid"] = int(opened.st_gid)
                    directories: List[Dict[str, int | str]] = []
                    record["files"] = walk_directory(
                        category,
                        source_path,
                        descriptor,
                        Path(),
                        source,
                        directories,
                    )
                    record["directories"] = directories
                finally:
                    os.close(descriptor)
            else:
                raise BackupIntegrityError("Backupquelle ist kein regulaerer Pfad: {}".format(source_path))
        finally:
            os.close(parent_descriptor)
    return mapped_entries, source_records


def _safe_relative_path(value: str) -> Path:
    relative = Path(str(value or ""))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise BackupIntegrityError("Ungueltiger relativer Manifestpfad: {!r}".format(value))
    return relative


def _scan_backup_files(backup: Path) -> List[Path]:
    _assert_no_symlink_components(backup)
    files: List[Path] = []
    for root, dirs, names in os.walk(str(backup), followlinks=False):
        root_path = Path(root)
        for dirname in list(dirs):
            candidate = root_path / dirname
            metadata = os.lstat(str(candidate))
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise BackupIntegrityError("Unsicherer Eintrag im Backup: {}".format(candidate))
        for name in names:
            candidate = root_path / name
            if candidate.parent == backup and candidate.name in {MANIFEST_NAME, MANIFEST_DIGEST_NAME}:
                continue
            metadata = os.lstat(str(candidate))
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise BackupIntegrityError("Ungueltiger Dateityp im Backup: {}".format(candidate))
            files.append(candidate)
    return sorted(files)


def secure_backup_tree(backup_dir: PathValue) -> None:
    backup = _assert_no_symlink_components(backup_dir)
    for root, dirs, files in os.walk(str(backup), followlinks=False):
        root_path = Path(root)
        os.chmod(str(root_path), 0o700)
        for dirname in dirs:
            path = root_path / dirname
            metadata = os.lstat(str(path))
            if stat.S_ISLNK(metadata.st_mode):
                raise BackupIntegrityError("Symlink im Backup: {}".format(path))
            os.chmod(str(path), 0o700)
        for name in files:
            path = root_path / name
            metadata = os.lstat(str(path))
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise BackupIntegrityError("Unsichere Backup-Datei: {}".format(path))
            os.chmod(str(path), 0o600)


def finalize_backup(
    backup_dir: PathValue,
    mapped_entries: Iterable[Dict[str, object]],
    source_records: Iterable[Dict[str, object]],
    kind: str = SYSTEM_BACKUP_KIND,
    install_root: Optional[PathValue] = None,
    systemd_mask_state: Optional[Dict[str, object]] = None,
    transaction_id: Optional[str] = None,
    parent_backup_id: Optional[str] = None,
) -> Dict[str, object]:
    backup = _assert_no_symlink_components(backup_dir)
    if kind not in {SYSTEM_BACKUP_KIND, WEB_SNAPSHOT_KIND, QUIESCED_OVERLAY_KIND}:
        raise BackupIntegrityError("Ungueltige Backup-Art: {}".format(kind))
    mapped_entries_list = [dict(item) for item in mapped_entries]
    source_records_list = [dict(item) for item in source_records]
    mapped = {str(item["backup_path"]): item for item in mapped_entries_list}
    if len(mapped) != len(mapped_entries_list):
        raise BackupIntegrityError("Manifestzuordnung enthält doppelte Backuppfade")
    if kind == QUIESCED_OVERLAY_KIND:
        overlay_transaction_id = _normalized_transaction_id(transaction_id)
        overlay_parent_backup_id = _normalized_backup_id(
            parent_backup_id,
            label="Parent-Backup-ID",
        )
        if install_root is None:
            raise BackupIntegrityError(
                "Quiesced-Overlay besitzt keinen gebundenen Installationspfad"
            )
        overlay_install_root = _lexical_absolute(install_root)
        if not source_records_list:
            raise BackupIntegrityError(
                "Quiesced-Overlay besitzt keinen manifestierten Quellumfang"
            )
        if systemd_mask_state is not None:
            raise BackupIntegrityError(
                "Quiesced-Overlay darf keinen systemd-Maskenzustand enthalten"
            )
    else:
        if transaction_id is not None or parent_backup_id is not None:
            raise BackupIntegrityError(
                "Transaktionsbindung ist ausschließlich für Quiesced-Overlays zulässig"
            )
        overlay_transaction_id = None
        overlay_parent_backup_id = None
        overlay_install_root = None
    files = _scan_backup_files(backup)
    if not files and kind != QUIESCED_OVERLAY_KIND:
        raise BackupIntegrityError("Leeres Backup ist nicht zulaessig.")
    manifest_files: List[Dict[str, object]] = []
    for path in files:
        relative = path.relative_to(backup).as_posix()
        mapping = mapped.get(relative, {})
        entry_size = mapping.get("size")
        entry_sha = mapping.get("sha256")
        if entry_size is None or entry_sha is None:
            entry_size = path.stat().st_size
            entry_sha = sha256_file(path)
        manifest_files.append({
            "path": relative,
            "size": int(entry_size),
            "sha256": str(entry_sha),
            "mode": int(mapping.get("restore_mode", stat.S_IMODE(path.stat().st_mode))),
            "uid": int(mapping.get("restore_uid", path.stat().st_uid)),
            "gid": int(mapping.get("restore_gid", path.stat().st_gid)),
            "category": mapping.get("category"),
            "restore_path": mapping.get("restore_path"),
        })
    unknown = sorted(set(mapped) - {str(item["path"]) for item in manifest_files})
    if unknown:
        raise BackupIntegrityError("Manifestzuordnung verweist auf fehlende Dateien: {}".format(unknown[:3]))
    manifest: Dict[str, object] = {
        # Nur Backups mit Maskenvertrag verwenden Schema 3. Alle bestehenden
        # Aufrufer ohne diese Erweiterung schreiben weiterhin Schema 2. Damit
        # lehnt alter Restore-Code maskengebundene Backups sicher ab, statt das
        # neue Feld zu ignorieren und eine Maske als fehlende Datei zu deuten.
        "schema": MANIFEST_SCHEMA if systemd_mask_state is not None else LEGACY_MANIFEST_SCHEMA,
        "kind": kind,
        "state": "complete",
        # Neue Transaktionsreceipts binden die ID als 256-Bit-Hexwert. Bereits
        # vorhandene UUID-Manifeste bleiben in verify_backup kompatibel.
        "backup_id": os.urandom(32).hex(),
        "created_utc": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
        "install_root": str(_lexical_absolute(install_root)) if install_root else None,
        "files": manifest_files,
        "sources": source_records_list,
    }
    if kind == QUIESCED_OVERLAY_KIND:
        manifest.update({
            "transaction_id": overlay_transaction_id,
            "parent_backup_id": overlay_parent_backup_id,
            "install_root": str(overlay_install_root),
        })
    if systemd_mask_state is not None:
        if kind != SYSTEM_BACKUP_KIND:
            raise BackupIntegrityError("Systemd-Maskenzustand ist nur für System-Backups zulässig")
        manifest["systemd_mask_state"] = verify_systemd_mask_state_contract(systemd_mask_state)
    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_path = backup / MANIFEST_NAME
    digest_path = backup / MANIFEST_DIGEST_NAME
    for target, payload in (
        (manifest_path, encoded),
        (digest_path, (hashlib.sha256(encoded).hexdigest() + "  " + MANIFEST_NAME + "\n").encode("ascii")),
    ):
        descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return verify_backup(backup, expected_kind=kind)


def verify_backup(
    backup_dir: PathValue,
    expected_kind: Optional[str] = None,
    expected_manifest_sha256: Optional[str] = None,
) -> Dict[str, object]:
    backup = _assert_no_symlink_components(backup_dir)
    if (
        expected_manifest_sha256 is not None
        and not _SHA256_RE.fullmatch(str(expected_manifest_sha256))
    ):
        raise BackupIntegrityError("Erwartete Manifest-SHA-256 ist ungültig")
    manifest_path = backup / MANIFEST_NAME
    digest_path = backup / MANIFEST_DIGEST_NAME
    try:
        digest_text = _read_small_file(digest_path, 4096, "ascii").strip().split()
        manifest_bytes = _read_small_file_bytes(manifest_path, 16 * 1024 * 1024)
    except (FileNotFoundError, OSError, UnicodeError) as exc:
        raise BackupIntegrityError("Backup, Pflichtmanifest oder Manifest-Pruefsumme fehlt: {}".format(exc))
    if len(digest_text) != 2 or digest_text[1] != MANIFEST_NAME or not _SHA256_RE.fullmatch(digest_text[0]):
        raise BackupIntegrityError("Manifest-Pruefsummendatei ist ungueltig.")
    if hashlib.sha256(manifest_bytes).hexdigest() != digest_text[0]:
        raise BackupIntegrityError("Manifest-Pruefsumme stimmt nicht.")
    if (
        expected_manifest_sha256 is not None
        and digest_text[0] != str(expected_manifest_sha256)
    ):
        raise BackupIntegrityError(
            "Manifest-Prüfsumme stimmt nicht mit dem Restore-Guard überein"
        )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("Manifest ist nicht lesbar: {}".format(exc))
    if not isinstance(manifest, dict):
        raise BackupIntegrityError("Nicht unterstützte Backup-Manifestversion.")
    schema_value = manifest.get("schema")
    if isinstance(schema_value, bool) or not isinstance(schema_value, int) or schema_value not in {
        LEGACY_MANIFEST_SCHEMA,
        MANIFEST_SCHEMA,
    }:
        raise BackupIntegrityError("Nicht unterstützte Backup-Manifestversion.")
    if manifest.get("state") != "complete" or manifest.get("kind") not in {
        SYSTEM_BACKUP_KIND,
        WEB_SNAPSHOT_KIND,
        QUIESCED_OVERLAY_KIND,
    }:
        raise BackupIntegrityError("Backup ist nicht als vollstaendig markiert.")
    if expected_kind and manifest.get("kind") != expected_kind:
        raise BackupIntegrityError("Backup-Art stimmt nicht: erwartet {}, ist {}".format(expected_kind, manifest.get("kind")))
    if not isinstance(manifest.get("backup_id"), str) or not manifest.get("backup_id"):
        raise BackupIntegrityError("Backup-ID fehlt.")
    if manifest.get("kind") == QUIESCED_OVERLAY_KIND:
        _normalized_backup_id(manifest.get("backup_id"))
        _normalized_transaction_id(manifest.get("transaction_id"))
        _normalized_backup_id(
            manifest.get("parent_backup_id"),
            label="Parent-Backup-ID",
        )
        if manifest.get("install_root") is None:
            raise BackupIntegrityError(
                "Quiesced-Overlay besitzt keinen gebundenen Installationspfad"
            )
        _lexical_absolute(str(manifest.get("install_root")))
    elif "transaction_id" in manifest or "parent_backup_id" in manifest:
        raise BackupIntegrityError(
            "Nicht-Overlay-Manifest enthält eine unbeachtete Transaktionsbindung"
        )
    entries = manifest.get("files")
    if not isinstance(entries, list) or (
        not entries and manifest.get("kind") != QUIESCED_OVERLAY_KIND
    ):
        raise BackupIntegrityError("Leeres oder unvollstaendiges Backup-Manifest.")
    expected_paths: Set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupIntegrityError("Ungueltiger Manifesteintrag.")
        relative = _safe_relative_path(str(entry.get("path", "")))
        relative_text = relative.as_posix()
        if relative_text in expected_paths:
            raise BackupIntegrityError("Doppelter Manifestpfad: {}".format(relative_text))
        expected_paths.add(relative_text)
        path = backup / relative
        metadata = os.lstat(str(path))
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise BackupIntegrityError("Manifestdatei fehlt oder ist unsicher: {}".format(relative_text))
        expected_sha = str(entry.get("sha256", ""))
        if not _SHA256_RE.fullmatch(expected_sha):
            raise BackupIntegrityError("Ungueltige SHA-256 fuer {}".format(relative_text))
        if metadata.st_size != int(entry.get("size", -1)) or sha256_file(path) != expected_sha:
            raise BackupIntegrityError("Backup-Datei stimmt nicht mit Manifest ueberein: {}".format(relative_text))
        restore_path = entry.get("restore_path")
        if restore_path is not None:
            _lexical_absolute(str(restore_path))
            try:
                mode = int(entry.get("mode", -1))
                uid = int(entry.get("uid", -1))
                gid = int(entry.get("gid", -1))
            except (TypeError, ValueError) as exc:
                raise BackupIntegrityError("Ungueltige Restore-Metadaten fuer {}".format(relative_text)) from exc
            if mode < 0 or mode > 0o7777 or uid < 0 or gid < 0:
                raise BackupIntegrityError("Ungueltige Restore-Metadaten fuer {}".format(relative_text))
    sources = manifest.get("sources")
    if not isinstance(sources, list) or (
        manifest.get("kind") == QUIESCED_OVERLAY_KIND and not sources
    ):
        raise BackupIntegrityError("Manifest-Sources muessen eine Liste sein")
    for record in sources:
        if not isinstance(record, dict):
            raise BackupIntegrityError("Ungueltiger Source-Eintrag im Manifest")
        source_type = str(record.get("source_type") or "")
        if source_type not in {"file", "directory", "missing"}:
            raise BackupIntegrityError("Ungueltiger Source-Typ im Manifest")
        _lexical_absolute(str(record.get("source") or ""))
        if source_type == "directory" and bool(record.get("present")):
            directory_entries = [record, *list(record.get("directories") or [])]
            for directory in directory_entries:
                if not isinstance(directory, dict):
                    raise BackupIntegrityError("Verzeichnismetadaten fehlen im Manifest")
                try:
                    directory_mode = int(directory.get("mode", -1))
                    directory_uid = int(directory.get("uid", -1))
                    directory_gid = int(directory.get("gid", -1))
                except (TypeError, ValueError) as exc:
                    raise BackupIntegrityError("Ungueltige Verzeichnismetadaten im Manifest") from exc
                if directory_mode < 0 or directory_mode > 0o7777 or directory_uid < 0 or directory_gid < 0:
                    raise BackupIntegrityError("Ungueltige Verzeichnismetadaten im Manifest")
    actual_paths = {path.relative_to(backup).as_posix() for path in _scan_backup_files(backup)}
    if actual_paths != expected_paths:
        raise BackupIntegrityError(
            "Backup-Dateisatz weicht vom Manifest ab (fehlend={}, extra={}).".format(
                sorted(expected_paths - actual_paths)[:3], sorted(actual_paths - expected_paths)[:3]
            )
        )
    schema = schema_value
    if schema == MANIFEST_SCHEMA and manifest.get("kind") != SYSTEM_BACKUP_KIND:
        raise BackupIntegrityError("Manifest-Schema 3 ist ausschließlich für maskengebundene System-Backups zulässig")
    if schema == LEGACY_MANIFEST_SCHEMA and "systemd_mask_state" in manifest:
        raise BackupIntegrityError("Legacy-Manifest darf keinen unbeachteten systemd-Maskenzustand enthalten")
    if schema == MANIFEST_SCHEMA and manifest.get("kind") == SYSTEM_BACKUP_KIND:
        if "systemd_mask_state" not in manifest:
            raise BackupIntegrityError("System-Backup Schema 3 fehlt der systemd-Maskenzustand")
        verify_systemd_mask_state_contract(manifest["systemd_mask_state"])
    if manifest.get("kind") == SYSTEM_BACKUP_KIND:
        # Kompatibilitätsentscheidung: Schema 2 bleibt verifizierbar, sein
        # fehlender Maskenzustand wird beim Restore aber niemals als
        # "unmasked" interpretiert. Nur Schema 3 autorisiert die Mutation.
        _verify_private_ml_backup_contract(backup, manifest)
    elif "systemd_mask_state" in manifest:
        raise BackupIntegrityError("Web-Snapshot darf keinen systemd-Maskenzustand enthalten")
    return manifest


def verified_manifest_sha256(
    backup_dir: PathValue,
    *,
    expected_kind: Optional[str] = None,
) -> str:
    """Liefert die SHA-256 nur für ein zweimal stabil verifiziertes Manifest."""

    backup = _assert_no_symlink_components(backup_dir)
    first = _read_small_file_bytes(backup / MANIFEST_NAME, 16 * 1024 * 1024)
    digest = hashlib.sha256(first).hexdigest()
    verify_backup(
        backup,
        expected_kind=expected_kind,
        expected_manifest_sha256=digest,
    )
    second = _read_small_file_bytes(backup / MANIFEST_NAME, 16 * 1024 * 1024)
    if second != first:
        raise BackupIntegrityError(
            "Backup-Manifest driftete während der Vertragsbindung"
        )
    return digest


def _validate_quiesced_overlay_restore_guard(
    backup: Path,
    manifest: Dict[str, object],
    guard: QuiescedOverlayRestoreGuard,
) -> None:
    """Prüft Pfade, Inodes und beide Manifeste vor jeder Restore-Mutation."""

    if not isinstance(guard, QuiescedOverlayRestoreGuard):
        raise BackupIntegrityError(
            "Quiesced-Overlay-Restore besitzt keinen expliziten Guard"
        )
    transaction_id = _normalized_transaction_id(guard.transaction_id)
    overlay = _lexical_absolute(guard.overlay_dir)
    install = _lexical_absolute(guard.install_root)
    parent_backup = _lexical_absolute(guard.parent_backup_dir)
    collection = _lexical_absolute(guard.collection_dir)
    overlay_backup_id = _normalized_backup_id(guard.backup_id)
    parent_backup_id = _normalized_backup_id(
        guard.parent_backup_id,
        label="Parent-Backup-ID",
    )
    for label, value in (
        ("Overlay-Manifest-SHA-256", guard.manifest_sha256),
        ("Parent-Manifest-SHA-256", guard.parent_backup_manifest_sha256),
    ):
        if not _SHA256_RE.fullmatch(str(value or "")):
            raise BackupIntegrityError("{} ist ungültig".format(label))
    for label, value in (
        ("Overlay-Gerät", guard.overlay_dev),
        ("Overlay-Inode", guard.overlay_ino),
        ("Parent-Backup-Gerät", guard.parent_backup_dev),
        ("Parent-Backup-Inode", guard.parent_backup_ino),
        ("Backup-Root-Gerät", guard.collection_dev),
        ("Backup-Root-Inode", guard.collection_ino),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BackupIntegrityError("{} ist ungültig".format(label))
    expected_name = ".{}.quiesced-{}".format(parent_backup.name, transaction_id)
    if (
        overlay != backup
        or overlay.parent != collection
        or parent_backup.parent != collection
        or overlay.name != expected_name
    ):
        raise BackupIntegrityError(
            "Quiesced-Overlay ist nicht an Parent-Backup und Transaktion gebunden"
        )
    if (
        manifest.get("kind") != QUIESCED_OVERLAY_KIND
        or str(manifest.get("transaction_id") or "") != transaction_id
        or str(manifest.get("parent_backup_id") or "") != parent_backup_id
        or str(manifest.get("backup_id") or "") != overlay_backup_id
        or str(manifest.get("install_root") or "") != str(install)
    ):
        raise BackupIntegrityError(
            "Quiesced-Overlay-Manifest widerspricht dem Restore-Guard"
        )

    validate_existing_backup_root(collection, install)
    collection_descriptor = _open_root_controlled_backup_directory_chain(
        collection,
        leaf_mode=0o700,
    )
    parent_descriptor = None
    overlay_descriptor = None
    try:
        collection_metadata = os.fstat(collection_descriptor)
        if (collection_metadata.st_dev, collection_metadata.st_ino) != (
            guard.collection_dev,
            guard.collection_ino,
        ):
            raise BackupIntegrityError("Backup-Root-Inode driftete vor Restore")
        parent_before = os.stat(
            parent_backup.name,
            dir_fd=collection_descriptor,
            follow_symlinks=False,
        )
        overlay_before = os.stat(
            overlay.name,
            dir_fd=collection_descriptor,
            follow_symlinks=False,
        )
        flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        parent_descriptor = os.open(
            parent_backup.name,
            flags,
            dir_fd=collection_descriptor,
        )
        overlay_descriptor = os.open(
            overlay.name,
            flags,
            dir_fd=collection_descriptor,
        )
        parent_opened = os.fstat(parent_descriptor)
        overlay_opened = os.fstat(overlay_descriptor)
        if (
            not stat.S_ISDIR(parent_opened.st_mode)
            or not stat.S_ISDIR(overlay_opened.st_mode)
            or (parent_before.st_dev, parent_before.st_ino)
            != (parent_opened.st_dev, parent_opened.st_ino)
            or (overlay_before.st_dev, overlay_before.st_ino)
            != (overlay_opened.st_dev, overlay_opened.st_ino)
            or (parent_opened.st_dev, parent_opened.st_ino)
            != (guard.parent_backup_dev, guard.parent_backup_ino)
            or (overlay_opened.st_dev, overlay_opened.st_ino)
            != (guard.overlay_dev, guard.overlay_ino)
        ):
            raise BackupIntegrityError(
                "Quiesced-Overlay- oder Parent-Backup-Inode driftete vor Restore"
            )

        parent_manifest = verify_backup(
            parent_backup,
            expected_kind=SYSTEM_BACKUP_KIND,
            expected_manifest_sha256=guard.parent_backup_manifest_sha256,
        )
        if (
            str(parent_manifest.get("backup_id") or "") != parent_backup_id
            or str(parent_manifest.get("install_root") or "") != str(install)
        ):
            raise BackupIntegrityError(
                "Parent-Backup widerspricht dem Quiesced-Overlay-Guard"
            )
        parent_after = os.stat(
            parent_backup.name,
            dir_fd=collection_descriptor,
            follow_symlinks=False,
        )
        overlay_after = os.stat(
            overlay.name,
            dir_fd=collection_descriptor,
            follow_symlinks=False,
        )
        if (
            (parent_after.st_dev, parent_after.st_ino)
            != (parent_opened.st_dev, parent_opened.st_ino)
            or (overlay_after.st_dev, overlay_after.st_ino)
            != (overlay_opened.st_dev, overlay_opened.st_ino)
        ):
            raise BackupIntegrityError(
                "Quiesced-Overlay- oder Parent-Backup-Pfad driftete während der Prüfung"
            )
    finally:
        if overlay_descriptor is not None:
            os.close(overlay_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        os.close(collection_descriptor)


def _read_small_file_bytes(path: PathValue, maximum: int) -> bytes:
    descriptor, metadata = _open_regular_file_nofollow(path)
    try:
        if metadata.st_size > maximum:
            raise BackupIntegrityError("Datei ist unplausibel gross: {}".format(path))
        chunks: List[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            block = os.read(descriptor, min(65536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise BackupIntegrityError("Datei ist unplausibel gross: {}".format(path))
        return data
    finally:
        os.close(descriptor)


def _read_small_file(path: PathValue, maximum: int, encoding: str) -> str:
    return _read_small_file_bytes(path, maximum).decode(encoding)


def _restore_target_allowed(destination: Path, allowed_roots: Sequence[Path], allowed_files: Sequence[Path]) -> bool:
    if destination in allowed_files:
        return True
    return any(_is_within(destination, root) for root in allowed_roots)


def _copy_open_descriptor_to_temp(source_descriptor: int, parent: Path, prefix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, dir=str(parent))
    temporary = Path(name)
    try:
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        while True:
            block = os.read(source_descriptor, 1024 * 1024)
            if not block:
                break
            view = memoryview(block)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return temporary


def _exact_cleanup_candidates(
    manifest: Dict[str, object],
    roots: Sequence[Path],
    files: Sequence[Path],
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    """Find files and directories created after the manifested backup."""
    expected_paths = {
        _lexical_absolute(str(entry["restore_path"]))
        for entry in manifest.get("files", [])  # type: ignore[union-attr]
        if isinstance(entry, dict) and entry.get("restore_path")
    }
    cleanup_files: Dict[str, Dict[str, object]] = {}
    cleanup_dirs: Dict[str, Dict[str, object]] = {}

    def scan_directory(source: Path, exclude_top: Set[str], exclude_anywhere: Set[str], original_dirs: Set[Path]) -> None:
        if not os.path.lexists(str(source)):
            return
        root_meta = os.lstat(str(source))
        if stat.S_ISLNK(root_meta.st_mode) or not stat.S_ISDIR(root_meta.st_mode):
            raise BackupIntegrityError("Restore-Flaeche ist kein sicheres Verzeichnis: {}".format(source))
        for directory, dirnames, filenames in os.walk(str(source), topdown=True, followlinks=False):
            directory_path = Path(directory)
            relative = directory_path.relative_to(source)
            kept_dirs = []
            for name in dirnames:
                if not relative.parts and _is_private_local_entry(name):
                    continue
                if name in exclude_anywhere or (not relative.parts and name in exclude_top):
                    continue
                candidate = directory_path / name
                metadata = os.lstat(str(candidate))
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise BackupIntegrityError("Unsicherer Eintrag in Restore-Flaeche: {}".format(candidate))
                if candidate not in original_dirs:
                    cleanup_dirs[str(candidate)] = {
                        "path": candidate, "dev": metadata.st_dev, "ino": metadata.st_ino,
                        "mode": stat.S_IMODE(metadata.st_mode), "uid": metadata.st_uid, "gid": metadata.st_gid,
                    }
                    continue
                kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in filenames:
                if not relative.parts and _is_private_local_entry(name):
                    continue
                if name in exclude_anywhere or (not relative.parts and name in exclude_top):
                    continue
                candidate = directory_path / name
                metadata = os.lstat(str(candidate))
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise BackupIntegrityError("Unsicherer Eintrag in Restore-Flaeche: {}".format(candidate))
                if candidate not in expected_paths:
                    cleanup_files[str(candidate)] = {
                        "path": candidate, "dev": metadata.st_dev, "ino": metadata.st_ino,
                        "mode": stat.S_IMODE(metadata.st_mode), "uid": metadata.st_uid, "gid": metadata.st_gid,
                    }

    source_records = manifest.get("sources", [])
    if not isinstance(source_records, list):
        raise BackupIntegrityError("Manifest-Sources muessen eine Liste sein")
    for record in source_records:
        if not isinstance(record, dict):
            raise BackupIntegrityError("Ungueltiger Source-Eintrag im Manifest")
        source = _lexical_absolute(str(record.get("source") or ""))
        if not _restore_target_allowed(source, roots, files):
            raise BackupIntegrityError("Source-Flaeche liegt ausserhalb der Restore-Positivliste: {}".format(source))
        exclude_top = {str(name) for name in record.get("exclude_top_level", [])}
        exclude_anywhere = {str(name) for name in record.get("exclude_anywhere", [])}
        for name in exclude_top | exclude_anywhere:
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise BackupIntegrityError("Ungueltiger Source-Ausschluss im Manifest")
        source_type = str(record.get("source_type") or "")
        present = bool(record.get("present"))
        original_dirs: Set[Path] = set()
        for item in record.get("directories", []):
            relative_value = item.get("path") if isinstance(item, dict) else item
            original_dirs.add(source / _safe_relative_path(str(relative_value)))
        if source_type == "directory" and present:
            scan_directory(source, exclude_top, exclude_anywhere, original_dirs)
        elif source_type == "missing" and not present and os.path.lexists(str(source)):
            metadata = os.lstat(str(source))
            if stat.S_ISREG(metadata.st_mode):
                cleanup_files[str(source)] = {
                    "path": source, "dev": metadata.st_dev, "ino": metadata.st_ino,
                    "mode": stat.S_IMODE(metadata.st_mode), "uid": metadata.st_uid, "gid": metadata.st_gid,
                }
            elif stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                cleanup_dirs[str(source)] = {
                    "path": source, "dev": metadata.st_dev, "ino": metadata.st_ino,
                    "mode": stat.S_IMODE(metadata.st_mode), "uid": metadata.st_uid, "gid": metadata.st_gid,
                }
            else:
                raise BackupIntegrityError("Neu entstandene Source-Flaeche ist unsicher: {}".format(source))
        elif source_type not in {"file", "directory", "missing"}:
            raise BackupIntegrityError("Ungueltiger Source-Typ im Manifest")
    return list(cleanup_files.values()), list(cleanup_dirs.values())


def _open_or_create_directory_nofollow(path: PathValue, mode: int = 0o755) -> int:
    candidate = _lexical_absolute(path)
    flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW
    descriptor = os.open(candidate.anchor, flags)
    try:
        for part in candidate.parts[1:]:
            try:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(part, mode, dir_fd=descriptor)
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _copy_fd_to_temp_at(source_descriptor: int, parent_descriptor: int, prefix: str) -> str:
    for _attempt in range(100):
        name = "{}{}".format(prefix, uuid.uuid4().hex)
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
                dir_fd=parent_descriptor,
            )
            break
        except FileExistsError:
            continue
    else:
        raise BackupIntegrityError("Kein eindeutiger Restore-Stagingname verfuegbar")
    try:
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        while True:
            block = os.read(source_descriptor, 1024 * 1024)
            if not block:
                break
            view = memoryview(block)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return name


def _entry_metadata(parent_descriptor: int, name: str) -> os.stat_result:
    metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BackupIntegrityError("Restore-Eintrag ist keine regulaere Datei: {}".format(name))
    return metadata


def _entry_sha256(parent_descriptor: int, name: str) -> str:
    descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_descriptor)
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BackupIntegrityError("Restore-Eintrag ist keine regulaere Datei: {}".format(name))
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _apply_descriptor_metadata(descriptor: int, mode: int, uid: int, gid: int, label: str) -> None:
    metadata = os.fstat(descriptor)
    if (metadata.st_uid, metadata.st_gid) != (uid, gid):
        os.fchown(descriptor, uid, gid)
    os.fchmod(descriptor, mode & 0o7777)
    metadata = os.fstat(descriptor)
    if (
        stat.S_IMODE(metadata.st_mode) != (mode & 0o7777)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
    ):
        raise BackupIntegrityError("Restore-Metadaten stimmen nicht: {}".format(label))


def _verify_entry(
    parent_descriptor: int,
    name: str,
    expected_sha: str,
    mode: int,
    uid: int,
    gid: int,
    expected_identity: Optional[Tuple[int, int]] = None,
) -> None:
    descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_descriptor)
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BackupIntegrityError("Restore-Ziel ist keine regulaere Datei: {}".format(name))
        if expected_identity is not None and (
            metadata.st_dev,
            metadata.st_ino,
        ) != expected_identity:
            raise BackupIntegrityError(
                "Restore-Ziel-Inode stimmt nicht: {}".format(name)
            )
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        if digest.hexdigest() != expected_sha:
            raise BackupIntegrityError("Restore-Pruefsumme stimmt nicht: {}".format(name))
        _apply_descriptor_metadata(descriptor, mode, uid, gid, name)
        os.fsync(descriptor)
        durable = os.fstat(descriptor)
        live = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(durable.st_mode)
            or (live.st_dev, live.st_ino) != (durable.st_dev, durable.st_ino)
            or stat.S_IMODE(durable.st_mode) != (mode & 0o7777)
            or durable.st_uid != uid
            or durable.st_gid != gid
        ):
            raise BackupIntegrityError(
                "Restore-Ziel driftete beim dauerhaften Schreiben: {}".format(name)
            )
    finally:
        os.close(descriptor)


def _refresh_original_binding_if_changed(
    item: Dict[str, object],
    parent_descriptor: int,
    old_name: str,
) -> bool:
    """Bind a same-inode writer update so rollback can verify the newest bytes."""

    metadata = _entry_metadata(parent_descriptor, old_name)
    if (metadata.st_dev, metadata.st_ino) != (item["original_dev"], item["original_ino"]):
        raise BackupIntegrityError("Restore-Original wurde durch einen anderen Eintrag ersetzt")
    current_sha = _entry_sha256(parent_descriptor, old_name)
    changed = (
        current_sha != item["original_sha"]
        or stat.S_IMODE(metadata.st_mode) != item["original_mode"]
        or metadata.st_uid != item["original_uid"]
        or metadata.st_gid != item["original_gid"]
    )
    if changed:
        item["original_sha"] = current_sha
        item["original_mode"] = stat.S_IMODE(metadata.st_mode)
        item["original_uid"] = int(metadata.st_uid)
        item["original_gid"] = int(metadata.st_gid)
    return changed


def _replace_between(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
    replace_func,
) -> None:
    if replace_func is os.replace:
        os.replace(
            source_name,
            destination_name,
            src_dir_fd=source_descriptor,
            dst_dir_fd=destination_descriptor,
        )
        return
    replace_func(
        "/proc/self/fd/{}/{}".format(source_descriptor, source_name),
        "/proc/self/fd/{}/{}".format(destination_descriptor, destination_name),
    )


def _exchange_entries(
    parent_descriptor: int,
    first_name: str,
    second_name: str,
    exchange_func=None,
) -> None:
    """Atomically exchange two directory entries through renameat2."""

    if exchange_func is not None:
        exchange_func(parent_descriptor, first_name, second_name)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise BackupIntegrityError("Atomarer Restore-Austausch wird vom System nicht unterstuetzt")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_descriptor,
        os.fsencode(first_name),
        parent_descriptor,
        os.fsencode(second_name),
        2,  # RENAME_EXCHANGE
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _unique_missing_name(parent_descriptor: int, prefix: str) -> str:
    for _attempt in range(100):
        name = "{}{}".format(prefix, uuid.uuid4().hex)
        try:
            os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return name
    raise BackupIntegrityError("Kein eindeutiger Quarantaenename verfuegbar")


def _verify_parent_binding(parent_path: Path, parent_descriptor: int, expected: os.stat_result) -> None:
    current_descriptor = _open_directory_nofollow(parent_path)
    try:
        current = os.fstat(current_descriptor)
    finally:
        os.close(current_descriptor)
    opened = os.fstat(parent_descriptor)
    identity = (expected.st_dev, expected.st_ino)
    if (opened.st_dev, opened.st_ino) != identity or (current.st_dev, current.st_ino) != identity:
        raise BackupIntegrityError("Restore-Zielverzeichnis wurde waehrend der Transaktion ausgetauscht: {}".format(parent_path))


def _manifest_directory_entries(
    manifest: Dict[str, object],
    roots: Sequence[Path],
    files: Sequence[Path],
) -> List[Dict[str, object]]:
    """Return every manifested source directory with bound mode/uid/gid."""

    result: Dict[str, Dict[str, object]] = {}
    for record in manifest.get("sources", []):  # type: ignore[union-attr]
        if not isinstance(record, dict) or record.get("source_type") != "directory" or not record.get("present"):
            continue
        source = _lexical_absolute(str(record.get("source") or ""))
        entries = [{
            "path": source,
            "mode": int(record["mode"]),
            "uid": int(record["uid"]),
            "gid": int(record["gid"]),
        }]
        for raw in record.get("directories", []):
            if not isinstance(raw, dict):
                raise BackupIntegrityError("Verzeichnismetadaten fehlen im Manifest")
            entries.append({
                "path": source / _safe_relative_path(str(raw.get("path") or "")),
                "mode": int(raw["mode"]),
                "uid": int(raw["uid"]),
                "gid": int(raw["gid"]),
            })
        for entry in entries:
            path = _lexical_absolute(entry["path"])
            if not _restore_target_allowed(path, roots, files):
                raise BackupIntegrityError("Restore-Verzeichnis liegt ausserhalb der Positivliste: {}".format(path))
            if str(path) in result:
                previous = result[str(path)]
                if any(previous[key] != entry[key] for key in ("mode", "uid", "gid")):
                    raise BackupIntegrityError("Widerspruechliche Verzeichnismetadaten: {}".format(path))
            else:
                result[str(path)] = entry
    return sorted(result.values(), key=lambda item: (len(Path(str(item["path"])).parts), str(item["path"])))


def _open_or_create_exact_directory(path: Path) -> Dict[str, object]:
    parent_descriptor = _open_directory_nofollow(path.parent)
    created = False
    try:
        try:
            before = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            os.mkdir(path.name, 0o700, dir_fd=parent_descriptor)
            created = True
            before = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise BackupIntegrityError("Restore-Verzeichnis ist unsicher: {}".format(path))
        descriptor = os.open(path.name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            os.close(descriptor)
            raise BackupIntegrityError("Restore-Verzeichnis wurde ausgetauscht: {}".format(path))
        return {
            "path": path,
            "parent_fd": parent_descriptor,
            "parent_metadata": os.fstat(parent_descriptor),
            "fd": descriptor,
            "dev": int(opened.st_dev),
            "ino": int(opened.st_ino),
            "created": created,
            "original_mode": stat.S_IMODE(opened.st_mode),
            "original_uid": int(opened.st_uid),
            "original_gid": int(opened.st_gid),
            "metadata_applied": False,
        }
    except Exception:
        if created:
            try:
                os.rmdir(path.name, dir_fd=parent_descriptor)
            except OSError:
                pass
        os.close(parent_descriptor)
        raise


def _current_open_descriptor_count() -> int:
    """Liest den absoluten Linux-FD-Bestand ohne ein neues Dauer-Handle."""

    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError as exc:
        raise BackupIntegrityError(
            "Restore-Dateideskriptorbestand kann nicht sicher ermittelt werden"
        ) from exc


def _cleanup_tree_descriptor_peak(candidate: Dict[str, object]) -> int:
    """Belegt read-only die zusätzlich nötige FD-Tiefe eines Cleanup-Baums."""

    root = _lexical_absolute(str(candidate["path"]))
    root_metadata = os.lstat(str(root))
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or (root_metadata.st_dev, root_metadata.st_ino)
        != (candidate["dev"], candidate["ino"])
    ):
        raise BackupIntegrityError(
            "Post-Backup-Verzeichnis driftete vor FD-Budgetierung: {}".format(root)
        )

    peak = 2

    def walk_error(exc: OSError) -> None:
        raise BackupIntegrityError(
            "Cleanup-Baum kann nicht vollständig gelesen werden: {}".format(root)
        ) from exc

    for directory, dirnames, filenames in os.walk(
        str(root),
        topdown=True,
        followlinks=False,
        onerror=walk_error,
    ):
        directory_path = Path(directory)
        relative_depth = len(directory_path.relative_to(root).parts)
        if relative_depth > MAX_CLEANUP_TREE_DEPTH:
            raise BackupIntegrityError(
                "Cleanup-Baum ist tiefer als {} Ebenen: {}".format(
                    MAX_CLEANUP_TREE_DEPTH,
                    root,
                )
            )
        peak = max(peak, relative_depth + 2)
        for name in dirnames:
            metadata = os.lstat(str(directory_path / name))
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise BackupIntegrityError(
                    "Unsicheres Verzeichnis im Cleanup-Baum: {}".format(
                        directory_path / name
                    )
                )
        for name in filenames:
            metadata = os.lstat(str(directory_path / name))
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise BackupIntegrityError(
                    "Unsichere Datei im Cleanup-Baum: {}".format(
                        directory_path / name
                    )
                )
    return peak


def _reserve_restore_descriptor_budget(required: int):
    """Hebt das weiche Linux-FD-Limit vor der atomaren Batch-Stagingphase an."""

    if _resource is None or not hasattr(_resource, "RLIMIT_NOFILE"):
        raise BackupIntegrityError(
            "Restore-Dateideskriptorprüfung benötigt Linux-RLIMIT_NOFILE"
        )
    requested = max(32, int(required))
    previous = _resource.getrlimit(_resource.RLIMIT_NOFILE)
    soft, hard = previous
    if soft == _resource.RLIM_INFINITY or soft >= requested:
        return previous
    if hard != _resource.RLIM_INFINITY and requested > hard:
        raise BackupIntegrityError(
            "Restore benötigt mindestens {} Dateideskriptoren; Hard-Limit ist {}".format(
                requested, hard
            )
        )
    try:
        _resource.setrlimit(_resource.RLIMIT_NOFILE, (requested, hard))
    except (OSError, ValueError) as exc:
        raise BackupIntegrityError(
            "Restore-Dateideskriptorbudget konnte nicht reserviert werden"
        ) from exc
    return previous


def _restore_descriptor_budget(previous) -> None:
    if _resource is None or previous is None:
        return
    try:
        _resource.setrlimit(_resource.RLIMIT_NOFILE, previous)
    except (OSError, ValueError):
        # Nach geschlossenen Restore-FDs ist ein höheres Soft-Limit sicherer als
        # ein nachträglich fehlgeschlagener, bereits committed Restore.
        try:
            print(
                "[!] Restore-Dateideskriptorbudget konnte nicht auf den Ausgangswert "
                "zurückgesetzt werden."
            )
        except (OSError, ValueError):
            pass


def _directory_tree_digest(descriptor: int) -> str:
    """Hash a tree through held descriptors; reject every non-file/non-directory entry."""

    digest = hashlib.sha256()

    def walk(current: int, prefix: str, depth: int) -> None:
        metadata = os.fstat(current)
        digest.update("D\0{}\0{}\0{}\0{}\n".format(
            prefix, stat.S_IMODE(metadata.st_mode), metadata.st_uid, metadata.st_gid
        ).encode("utf-8"))
        for name in sorted(os.listdir(current)):
            child = os.stat(name, dir_fd=current, follow_symlinks=False)
            relative = "{}/{}".format(prefix, name) if prefix else name
            if stat.S_ISLNK(child.st_mode):
                raise BackupIntegrityError("Symlink in Cleanup-Verzeichnis: {}".format(relative))
            if stat.S_ISDIR(child.st_mode):
                if depth >= MAX_CLEANUP_TREE_DEPTH:
                    raise BackupIntegrityError(
                        "Cleanup-Baum ist tiefer als {} Ebenen".format(
                            MAX_CLEANUP_TREE_DEPTH
                        )
                    )
                child_descriptor = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=current)
                try:
                    opened = os.fstat(child_descriptor)
                    if (opened.st_dev, opened.st_ino) != (child.st_dev, child.st_ino):
                        raise BackupIntegrityError("Cleanup-Verzeichnis wurde ausgetauscht: {}".format(relative))
                    walk(child_descriptor, relative, depth + 1)
                finally:
                    os.close(child_descriptor)
            elif stat.S_ISREG(child.st_mode):
                file_descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=current)
                file_digest = hashlib.sha256()
                try:
                    opened = os.fstat(file_descriptor)
                    if (opened.st_dev, opened.st_ino) != (child.st_dev, child.st_ino):
                        raise BackupIntegrityError("Cleanup-Datei wurde ausgetauscht: {}".format(relative))
                    while True:
                        block = os.read(file_descriptor, 1024 * 1024)
                        if not block:
                            break
                        file_digest.update(block)
                finally:
                    os.close(file_descriptor)
                digest.update("F\0{}\0{}\0{}\0{}\0{}\0{}\n".format(
                    relative,
                    stat.S_IMODE(child.st_mode),
                    child.st_uid,
                    child.st_gid,
                    child.st_size,
                    file_digest.hexdigest(),
                ).encode("utf-8"))
            else:
                raise BackupIntegrityError("Unerlaubter Eintrag in Cleanup-Verzeichnis: {}".format(relative))

    walk(descriptor, "", 0)
    return digest.hexdigest()


def _remove_directory_tree_at(
    parent_descriptor: int,
    name: str,
    expected_dev: int,
    expected_ino: int,
    *,
    _depth: int = 0,
) -> None:
    if _depth > MAX_CLEANUP_TREE_DEPTH:
        raise BackupIntegrityError(
            "Cleanup-Baum ist tiefer als {} Ebenen".format(
                MAX_CLEANUP_TREE_DEPTH
            )
        )
    descriptor = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (expected_dev, expected_ino):
            raise BackupIntegrityError("Cleanup-Quarantaene wurde ausgetauscht")
        for child in sorted(os.listdir(descriptor)):
            metadata = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise BackupIntegrityError("Symlink in Cleanup-Quarantaene: {}".format(child))
            if stat.S_ISDIR(metadata.st_mode):
                _remove_directory_tree_at(
                    descriptor,
                    child,
                    metadata.st_dev,
                    metadata.st_ino,
                    _depth=_depth + 1,
                )
            elif stat.S_ISREG(metadata.st_mode):
                os.unlink(child, dir_fd=descriptor)
            else:
                raise BackupIntegrityError("Unerlaubter Eintrag in Cleanup-Quarantaene: {}".format(child))
    finally:
        os.close(descriptor)
    current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (expected_dev, expected_ino):
        raise BackupIntegrityError("Cleanup-Quarantaene wurde vor rmdir ausgetauscht")
    os.rmdir(name, dir_fd=parent_descriptor)


def restore_persistent_payload(
    backup_dir: PathValue,
    *,
    expected_kind: str = SYSTEM_BACKUP_KIND,
    allowed_roots: Sequence[PathValue],
    allowed_files: Sequence[PathValue] = (),
    exchange_func=None,
    rollback_replace_func=os.replace,
    before_commit=None,
    restored_payload_guard=None,
    restore_metadata_overrides=None,
    verified_manifest_guard=None,
    overlay_restore_guard: Optional[QuiescedOverlayRestoreGuard] = None,
) -> int:
    """Restore one fd-bound transaction in the isolated single-thread updater."""

    backup = _assert_no_symlink_components(backup_dir)
    if expected_kind not in {
        SYSTEM_BACKUP_KIND,
        WEB_SNAPSHOT_KIND,
        QUIESCED_OVERLAY_KIND,
    }:
        raise BackupIntegrityError("Restore verlangt eine bekannte Backup-Art")
    if expected_kind == QUIESCED_OVERLAY_KIND:
        if not isinstance(overlay_restore_guard, QuiescedOverlayRestoreGuard):
            raise BackupIntegrityError(
                "Quiesced-Overlay-Restore besitzt keinen expliziten Guard"
            )
        if not _SHA256_RE.fullmatch(str(overlay_restore_guard.manifest_sha256 or "")):
            raise BackupIntegrityError(
                "Quiesced-Overlay-Restore besitzt keine gültige Manifest-SHA-256"
            )
        expected_manifest_sha256 = overlay_restore_guard.manifest_sha256
    else:
        if overlay_restore_guard is not None:
            raise BackupIntegrityError(
                "Overlay-Restore-Guard ist nur für Quiesced-Overlays zulässig"
            )
        expected_manifest_sha256 = None
    manifest = verify_backup(
        backup,
        expected_kind=expected_kind,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    if expected_kind == QUIESCED_OVERLAY_KIND:
        _validate_quiesced_overlay_restore_guard(
            backup,
            manifest,
            overlay_restore_guard,
        )
    if verified_manifest_guard is not None:
        if not callable(verified_manifest_guard):
            raise BackupIntegrityError("Restore-Manifestguard ist nicht aufrufbar")
        verified_manifest_guard(manifest)
    if restore_metadata_overrides is None:
        metadata_overrides: Dict[str, Tuple[int, int, int]] = {}
    elif not isinstance(restore_metadata_overrides, dict):
        raise BackupIntegrityError("Restore-Metadaten-Overrides sind ungültig")
    else:
        metadata_overrides = {}
        for raw_path, raw_metadata in restore_metadata_overrides.items():
            path = str(_lexical_absolute(str(raw_path)))
            if (
                path != "/etc/systemd/system/e3dc-storage-manager.service"
                or not isinstance(raw_metadata, (tuple, list))
                or tuple(raw_metadata) != (0o644, 0, 0)
            ):
                raise BackupIntegrityError(
                    "Nur die receiptgebundene Storage-Unit darf root-promotet werden"
                )
            metadata_overrides[path] = (0o644, 0, 0)
    if restored_payload_guard is not None and not callable(restored_payload_guard):
        raise BackupIntegrityError("Restore-Payloadguard ist nicht aufrufbar")
    roots = [_lexical_absolute(path) for path in allowed_roots]
    files = [_lexical_absolute(path) for path in allowed_files]
    if expected_kind == QUIESCED_OVERLAY_KIND:
        guarded_install = _lexical_absolute(overlay_restore_guard.install_root)
        expected_roots = {
            guarded_install / "data",
            Path("/var/www/html/data"),
            Path("/var/lib/e3dc-control"),
            Path("/etc/e3dc-control"),
        }
        expected_files = {guarded_install / "e3dc.config.txt"}
        if set(roots) != expected_roots or set(files) != expected_files:
            raise BackupIntegrityError(
                "Quiesced-Overlay-Restore besitzt keine exakte Ziel-Positivliste"
            )
    cleanup_candidates, cleanup_directories = _exact_cleanup_candidates(manifest, roots, files)
    directory_entries = _manifest_directory_entries(manifest, roots, files)
    restorable_files = sum(
        1
        for entry in manifest.get("files", [])  # type: ignore[union-attr]
        if isinstance(entry, dict) and entry.get("restore_path")
    )
    persistent_descriptors = (
        2 * len(directory_entries)
        + restorable_files
        + len(cleanup_candidates)
        + 2 * len(cleanup_directories)
    )
    for candidate in cleanup_directories:
        _cleanup_tree_descriptor_peak(candidate)
    descriptor_budget = (
        _current_open_descriptor_count()
        + persistent_descriptors
        + MAX_CLEANUP_TREE_DEPTH
        + 32
    )
    directory_items: List[Dict[str, object]] = []
    staged: List[Dict[str, object]] = []
    cleanup_items: List[Dict[str, object]] = []
    cleanup_directory_items: List[Dict[str, object]] = []
    applied: List[Dict[str, object]] = []
    cleanup_applied: List[Dict[str, object]] = []
    committed = False
    previous_descriptor_limit = None
    used_metadata_overrides: Set[str] = set()

    try:
        previous_descriptor_limit = _reserve_restore_descriptor_budget(
            descriptor_budget
        )
        for expected in directory_entries:
            directory = _open_or_create_exact_directory(Path(str(expected["path"])))
            directory.update({
                "expected_mode": int(expected["mode"]),
                "expected_uid": int(expected["uid"]),
                "expected_gid": int(expected["gid"]),
            })
            directory_items.append(directory)

        for entry in manifest["files"]:  # type: ignore[index]
            restore_path = entry.get("restore_path")
            if not restore_path:
                continue
            destination = _lexical_absolute(str(restore_path))
            if not _restore_target_allowed(destination, roots, files):
                raise BackupIntegrityError("Restore-Ziel liegt ausserhalb der Positivliste: {}".format(destination))
            parent_descriptor = _open_directory_nofollow(destination.parent)
            parent_metadata = os.fstat(parent_descriptor)
            try:
                original_metadata = _entry_metadata(parent_descriptor, destination.name)
                original_exists = True
                original_sha = _entry_sha256(parent_descriptor, destination.name)
                original_mode = stat.S_IMODE(original_metadata.st_mode)
                original_uid = int(original_metadata.st_uid)
                original_gid = int(original_metadata.st_gid)
            except FileNotFoundError:
                original_metadata = None
                original_exists = False
                original_sha = None
                original_mode = None
                original_uid = None
                original_gid = None

            source = backup / _safe_relative_path(str(entry["path"]))
            source_descriptor, _source_metadata = _open_regular_file_nofollow(source)
            try:
                new_name = _copy_fd_to_temp_at(
                    source_descriptor,
                    parent_descriptor,
                    ".{}.e3dc-new-".format(destination.name),
                )
            finally:
                os.close(source_descriptor)
            new_sha = str(entry["sha256"])
            if _entry_sha256(parent_descriptor, new_name) != new_sha:
                raise BackupIntegrityError("Restore-Staging-Pruefsumme stimmt nicht: {}".format(destination))
            new_metadata = _entry_metadata(parent_descriptor, new_name)
            manifest_mode = int(entry.get("mode", 0o600)) & 0o7777
            manifest_uid = int(entry["uid"])
            manifest_gid = int(entry["gid"])
            override = metadata_overrides.get(str(destination))
            if override is not None:
                if (
                    str(entry.get("category") or "") != "systemd"
                    or manifest_mode != 0o644
                ):
                    raise BackupIntegrityError(
                        "Storage-Unit-Metadatenoverride passt nicht zum Manifest"
                    )
                used_metadata_overrides.add(str(destination))
                new_mode, new_uid, new_gid = override
            else:
                new_mode, new_uid, new_gid = (
                    manifest_mode,
                    manifest_uid,
                    manifest_gid,
                )
            staged.append({
                "destination": destination,
                "parent_fd": parent_descriptor,
                "parent_metadata": parent_metadata,
                "new_name": new_name,
                "new_sha": new_sha,
                "new_mode": int(new_mode),
                "new_uid": int(new_uid),
                "new_gid": int(new_gid),
                "new_dev": int(new_metadata.st_dev),
                "new_ino": int(new_metadata.st_ino),
                "original_exists": original_exists,
                "original_dev": original_metadata.st_dev if original_metadata else None,
                "original_ino": original_metadata.st_ino if original_metadata else None,
                "original_sha": original_sha,
                "original_mode": original_mode,
                "original_uid": original_uid,
                "original_gid": original_gid,
                "old_name": None,
                "superseded_old_name": None,
                "placeholder_dev": None,
                "placeholder_ino": None,
                "placeholder_sha": None,
                "placeholder_mode": None,
                "placeholder_uid": None,
                "placeholder_gid": None,
                "new_installed": False,
            })

        if used_metadata_overrides != set(metadata_overrides):
            raise BackupIntegrityError(
                "Storage-Unit-Metadatenoverride fehlt im verifizierten Manifest"
            )

        for candidate in cleanup_candidates:
            destination = Path(str(candidate["path"]))
            parent_descriptor = _open_directory_nofollow(destination.parent)
            current = _entry_metadata(parent_descriptor, destination.name)
            if (current.st_dev, current.st_ino) != (candidate["dev"], candidate["ino"]):
                os.close(parent_descriptor)
                raise BackupIntegrityError("Post-Backup-Datei wurde vor Staging ersetzt: {}".format(destination))
            cleanup_items.append({
                **candidate,
                "destination": destination,
                "parent_fd": parent_descriptor,
                "parent_metadata": os.fstat(parent_descriptor),
                "sha256": _entry_sha256(parent_descriptor, destination.name),
                "is_directory": False,
                "old_name": None,
                "placeholder_dev": None,
                "placeholder_ino": None,
                "placeholder_sha": None,
                "placeholder_mode": None,
                "placeholder_uid": None,
                "placeholder_gid": None,
                "placeholder_removed": False,
            })

        for candidate in cleanup_directories:
            destination = Path(str(candidate["path"]))
            parent_descriptor = _open_directory_nofollow(destination.parent)
            descriptor = os.open(destination.name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=parent_descriptor)
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (candidate["dev"], candidate["ino"]):
                os.close(descriptor)
                os.close(parent_descriptor)
                raise BackupIntegrityError("Post-Backup-Verzeichnis wurde vor Staging ersetzt: {}".format(destination))
            cleanup_directory_items.append({
                **candidate,
                "destination": destination,
                "parent_fd": parent_descriptor,
                "parent_metadata": os.fstat(parent_descriptor),
                "fd": descriptor,
                "tree_digest": _directory_tree_digest(descriptor),
                "is_directory": True,
                "old_name": None,
                "placeholder_dev": None,
                "placeholder_ino": None,
                "placeholder_sha": None,
                "placeholder_mode": None,
                "placeholder_uid": None,
                "placeholder_gid": None,
                "placeholder_removed": False,
            })

        if not staged and expected_kind != QUIESCED_OVERLAY_KIND:
            raise BackupIntegrityError("Backup enthaelt keine wiederherstellbaren Dateien.")

        try:
            for item in staged:
                parent_descriptor = int(item["parent_fd"])
                destination = Path(str(item["destination"]))
                if item["original_exists"]:
                    current = _entry_metadata(parent_descriptor, destination.name)
                    if (current.st_dev, current.st_ino) != (item["original_dev"], item["original_ino"]):
                        raise BackupIntegrityError("Restore-Ziel wurde vor Austausch ersetzt: {}".format(destination))
                    if (
                        _entry_sha256(parent_descriptor, destination.name) != item["original_sha"]
                        or stat.S_IMODE(current.st_mode) != item["original_mode"]
                        or current.st_uid != item["original_uid"]
                        or current.st_gid != item["original_gid"]
                    ):
                        raise BackupIntegrityError("Restore-Ziel wurde vor Austausch veraendert: {}".format(destination))
                    old_name = _unique_missing_name(parent_descriptor, ".{}.e3dc-old-".format(destination.name))
                    os.replace(destination.name, old_name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
                    item["old_name"] = old_name
                    applied.append(item)
                    placeholder = os.open(
                        destination.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                        0o600,
                        dir_fd=parent_descriptor,
                    )
                    placeholder_metadata = os.fstat(placeholder)
                    os.close(placeholder)
                    item["placeholder_dev"] = placeholder_metadata.st_dev
                    item["placeholder_ino"] = placeholder_metadata.st_ino
                    item["placeholder_sha"] = hashlib.sha256(b"").hexdigest()
                    item["placeholder_mode"] = stat.S_IMODE(placeholder_metadata.st_mode)
                    item["placeholder_uid"] = int(placeholder_metadata.st_uid)
                    item["placeholder_gid"] = int(placeholder_metadata.st_gid)
                    quarantined = _entry_metadata(parent_descriptor, old_name)
                    if (quarantined.st_dev, quarantined.st_ino) != (item["original_dev"], item["original_ino"]):
                        raise BackupIntegrityError("Restore-Ziel-Swap vor Quarantaene erkannt: {}".format(destination))
                    quarantined_sha = _entry_sha256(parent_descriptor, old_name)
                    if (
                        quarantined_sha != item["original_sha"]
                        or stat.S_IMODE(quarantined.st_mode) != item["original_mode"]
                        or quarantined.st_uid != item["original_uid"]
                        or quarantined.st_gid != item["original_gid"]
                    ):
                        # A writer with an already-open descriptor may have changed the
                        # quarantined inode. Preserve that newest state during rollback.
                        item["original_sha"] = quarantined_sha
                        item["original_mode"] = stat.S_IMODE(quarantined.st_mode)
                        item["original_uid"] = int(quarantined.st_uid)
                        item["original_gid"] = int(quarantined.st_gid)
                        raise BackupIntegrityError("Restore-Ziel wurde waehrend Quarantaene veraendert: {}".format(destination))
                else:
                    placeholder = os.open(
                        destination.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                        0o600,
                        dir_fd=parent_descriptor,
                    )
                    placeholder_metadata = os.fstat(placeholder)
                    os.close(placeholder)
                    item["placeholder_dev"] = placeholder_metadata.st_dev
                    item["placeholder_ino"] = placeholder_metadata.st_ino
                    item["placeholder_sha"] = hashlib.sha256(b"").hexdigest()
                    item["placeholder_mode"] = stat.S_IMODE(placeholder_metadata.st_mode)
                    item["placeholder_uid"] = int(placeholder_metadata.st_uid)
                    item["placeholder_gid"] = int(placeholder_metadata.st_gid)
                    applied.append(item)
                displaced_name = str(item["new_name"])
                _exchange_entries(
                    parent_descriptor,
                    displaced_name,
                    destination.name,
                    exchange_func,
                )
                item["new_installed"] = True
                displaced = _entry_metadata(parent_descriptor, displaced_name)
                displaced_sha = _entry_sha256(parent_descriptor, displaced_name)
                placeholder_unchanged = (
                    (displaced.st_dev, displaced.st_ino)
                    == (item["placeholder_dev"], item["placeholder_ino"])
                    and displaced_sha == item["placeholder_sha"]
                    and stat.S_IMODE(displaced.st_mode) == item["placeholder_mode"]
                    and displaced.st_uid == item["placeholder_uid"]
                    and displaced.st_gid == item["placeholder_gid"]
                )
                if not placeholder_unchanged:
                    item["superseded_old_name"] = item.get("old_name")
                    item["old_name"] = displaced_name
                    item["new_name"] = None
                    item["original_exists"] = True
                    item["original_dev"] = int(displaced.st_dev)
                    item["original_ino"] = int(displaced.st_ino)
                    item["original_sha"] = displaced_sha
                    item["original_mode"] = stat.S_IMODE(displaced.st_mode)
                    item["original_uid"] = int(displaced.st_uid)
                    item["original_gid"] = int(displaced.st_gid)
                    raise BackupIntegrityError(
                        "Restore-Platzhalter wurde unmittelbar vor Austausch veraendert: {}".format(destination)
                    )
                if item.get("old_name") and _refresh_original_binding_if_changed(
                    item,
                    parent_descriptor,
                    str(item["old_name"]),
                ):
                    raise BackupIntegrityError(
                        "Restore-Original wurde am atomaren Austauschrand veraendert: {}".format(destination)
                    )
                os.unlink(displaced_name, dir_fd=parent_descriptor)
                item["new_name"] = None
                _verify_entry(
                    parent_descriptor,
                    destination.name,
                    str(item["new_sha"]),
                    int(item["new_mode"]),
                    int(item["new_uid"]),
                    int(item["new_gid"]),
                    (int(item["new_dev"]), int(item["new_ino"])),
                )
                _verify_parent_binding(destination.parent, parent_descriptor, item["parent_metadata"])

            for item in cleanup_items:
                parent_descriptor = int(item["parent_fd"])
                destination = Path(str(item["destination"]))
                current = _entry_metadata(parent_descriptor, destination.name)
                if (
                    (current.st_dev, current.st_ino) != (item["dev"], item["ino"])
                    or _entry_sha256(parent_descriptor, destination.name) != item["sha256"]
                    or stat.S_IMODE(current.st_mode) != item["mode"]
                    or current.st_uid != item["uid"]
                    or current.st_gid != item["gid"]
                ):
                    raise BackupIntegrityError("Post-Backup-Datei wurde vor Cleanup ersetzt: {}".format(destination))
                old_name = _unique_missing_name(parent_descriptor, ".{}.e3dc-extra-".format(destination.name))
                os.replace(destination.name, old_name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
                item["old_name"] = old_name
                cleanup_applied.append(item)
                placeholder = os.open(
                    destination.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                placeholder_metadata = os.fstat(placeholder)
                os.close(placeholder)
                item["placeholder_dev"] = placeholder_metadata.st_dev
                item["placeholder_ino"] = placeholder_metadata.st_ino
                item["placeholder_sha"] = hashlib.sha256(b"").hexdigest()
                item["placeholder_mode"] = stat.S_IMODE(placeholder_metadata.st_mode)
                item["placeholder_uid"] = int(placeholder_metadata.st_uid)
                item["placeholder_gid"] = int(placeholder_metadata.st_gid)
                quarantined = _entry_metadata(parent_descriptor, old_name)
                if (
                    (quarantined.st_dev, quarantined.st_ino) != (item["dev"], item["ino"])
                    or _entry_sha256(parent_descriptor, old_name) != item["sha256"]
                    or stat.S_IMODE(quarantined.st_mode) != item["mode"]
                    or quarantined.st_uid != item["uid"]
                    or quarantined.st_gid != item["gid"]
                ):
                    raise BackupIntegrityError("Post-Backup-Datei-Swap erkannt: {}".format(destination))
                _verify_parent_binding(destination.parent, parent_descriptor, item["parent_metadata"])

            for item in cleanup_directory_items:
                parent_descriptor = int(item["parent_fd"])
                destination = Path(str(item["destination"]))
                descriptor = int(item["fd"])
                current = os.fstat(descriptor)
                if (
                    (current.st_dev, current.st_ino) != (item["dev"], item["ino"])
                    or _directory_tree_digest(descriptor) != item["tree_digest"]
                ):
                    raise BackupIntegrityError("Post-Backup-Verzeichnis wurde vor Cleanup veraendert: {}".format(destination))
                old_name = _unique_missing_name(parent_descriptor, ".{}.e3dc-extra-".format(destination.name))
                os.replace(destination.name, old_name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
                item["old_name"] = old_name
                cleanup_applied.append(item)
                placeholder = os.open(
                    destination.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                placeholder_metadata = os.fstat(placeholder)
                os.close(placeholder)
                item["placeholder_dev"] = placeholder_metadata.st_dev
                item["placeholder_ino"] = placeholder_metadata.st_ino
                item["placeholder_sha"] = hashlib.sha256(b"").hexdigest()
                item["placeholder_mode"] = stat.S_IMODE(placeholder_metadata.st_mode)
                item["placeholder_uid"] = int(placeholder_metadata.st_uid)
                item["placeholder_gid"] = int(placeholder_metadata.st_gid)
                if _directory_tree_digest(descriptor) != item["tree_digest"]:
                    raise BackupIntegrityError("Post-Backup-Verzeichnis-Swap erkannt: {}".format(destination))
                _verify_parent_binding(destination.parent, parent_descriptor, item["parent_metadata"])

            for item in directory_items:
                descriptor = int(item["fd"])
                _apply_descriptor_metadata(
                    descriptor,
                    int(item["expected_mode"]),
                    int(item["expected_uid"]),
                    int(item["expected_gid"]),
                    str(item["path"]),
                )
                item["metadata_applied"] = True
                _verify_parent_binding(
                    Path(str(item["path"])).parent,
                    int(item["parent_fd"]),
                    item["parent_metadata"],
                )

            # Final pre-commit gate: every installed target, displaced original,
            # cleanup quarantine and guard placeholder is still descriptor-bound.
            for item in staged:
                parent_descriptor = int(item["parent_fd"])
                destination = Path(str(item["destination"]))
                current = _entry_metadata(parent_descriptor, destination.name)
                if (
                    (current.st_dev, current.st_ino) != (item["new_dev"], item["new_ino"])
                    or _entry_sha256(parent_descriptor, destination.name) != item["new_sha"]
                    or stat.S_IMODE(current.st_mode) != item["new_mode"]
                    or current.st_uid != item["new_uid"]
                    or current.st_gid != item["new_gid"]
                ):
                    raise BackupIntegrityError("Restore-Ziel driftete vor Commit: {}".format(destination))
                old_name = item.get("old_name")
                if old_name:
                    if _refresh_original_binding_if_changed(item, parent_descriptor, str(old_name)):
                        raise BackupIntegrityError("Restore-Original driftete vor Commit: {}".format(destination))
                _verify_parent_binding(destination.parent, parent_descriptor, item["parent_metadata"])

            for item in cleanup_items:
                parent_descriptor = int(item["parent_fd"])
                destination = Path(str(item["destination"]))
                placeholder = _entry_metadata(parent_descriptor, destination.name)
                if (
                    (placeholder.st_dev, placeholder.st_ino)
                    != (item["placeholder_dev"], item["placeholder_ino"])
                    or _entry_sha256(parent_descriptor, destination.name) != item["placeholder_sha"]
                    or stat.S_IMODE(placeholder.st_mode) != item["placeholder_mode"]
                    or placeholder.st_uid != item["placeholder_uid"]
                    or placeholder.st_gid != item["placeholder_gid"]
                ):
                    raise BackupIntegrityError("Cleanup-Platzhalter driftete vor Commit: {}".format(destination))
                quarantined = _entry_metadata(parent_descriptor, str(item["old_name"]))
                if (
                    (quarantined.st_dev, quarantined.st_ino) != (item["dev"], item["ino"])
                    or _entry_sha256(parent_descriptor, str(item["old_name"])) != item["sha256"]
                ):
                    raise BackupIntegrityError("Cleanup-Quarantaene driftete vor Commit: {}".format(destination))
                os.unlink(destination.name, dir_fd=parent_descriptor)
                item["placeholder_removed"] = True
                _verify_parent_binding(destination.parent, parent_descriptor, item["parent_metadata"])

            for item in cleanup_directory_items:
                parent_descriptor = int(item["parent_fd"])
                destination = Path(str(item["destination"]))
                placeholder = _entry_metadata(parent_descriptor, destination.name)
                if (
                    (placeholder.st_dev, placeholder.st_ino)
                    != (item["placeholder_dev"], item["placeholder_ino"])
                    or _entry_sha256(parent_descriptor, destination.name) != item["placeholder_sha"]
                    or stat.S_IMODE(placeholder.st_mode) != item["placeholder_mode"]
                    or placeholder.st_uid != item["placeholder_uid"]
                    or placeholder.st_gid != item["placeholder_gid"]
                    or _directory_tree_digest(int(item["fd"])) != item["tree_digest"]
                ):
                    raise BackupIntegrityError("Cleanup-Verzeichnis driftete vor Commit: {}".format(destination))
                os.unlink(destination.name, dir_fd=parent_descriptor)
                item["placeholder_removed"] = True
                _verify_parent_binding(destination.parent, parent_descriptor, item["parent_metadata"])

            # Durability-Gate: Der receiptgebundene Guard und insbesondere der
            # nachfolgende systemd-Callback dürfen erst laufen, wenn sowohl die
            # finalen Datei-/Verzeichnismetadaten als auch jeder in dieser
            # Transaktion geänderte Name rebootfest sind. Ein fsync-Fehler
            # läuft in den vollständigen Restore-Rücklauf; das äußere
            # Recovery-Gate bleibt dabei scharf.
            for item in staged:
                destination = Path(str(item["destination"]))
                _verify_entry(
                    int(item["parent_fd"]),
                    destination.name,
                    str(item["new_sha"]),
                    int(item["new_mode"]),
                    int(item["new_uid"]),
                    int(item["new_gid"]),
                    (int(item["new_dev"]), int(item["new_ino"])),
                )

            for item in sorted(
                directory_items,
                key=lambda value: (
                    len(Path(str(value["path"])).parts),
                    str(value["path"]),
                ),
                reverse=True,
            ):
                path = Path(str(item["path"]))
                descriptor = int(item["fd"])
                parent_descriptor = int(item["parent_fd"])
                expected_identity = (int(item["dev"]), int(item["ino"]))
                opened = os.fstat(descriptor)
                live = os.stat(
                    path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(live.st_mode)
                    or (opened.st_dev, opened.st_ino) != expected_identity
                    or (live.st_dev, live.st_ino) != expected_identity
                    or stat.S_IMODE(opened.st_mode)
                    != (int(item["expected_mode"]) & 0o7777)
                    or opened.st_uid != int(item["expected_uid"])
                    or opened.st_gid != int(item["expected_gid"])
                ):
                    raise BackupIntegrityError(
                        "Restore-Verzeichnis driftete vor dauerhaftem Schreiben: {}".format(
                            path
                        )
                    )
                os.fsync(descriptor)
                durable = os.fstat(descriptor)
                live_after = os.stat(
                    path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    (durable.st_dev, durable.st_ino) != expected_identity
                    or (live_after.st_dev, live_after.st_ino) != expected_identity
                ):
                    raise BackupIntegrityError(
                        "Restore-Verzeichnis driftete beim dauerhaften Schreiben: {}".format(
                            path
                        )
                    )
                _verify_parent_binding(
                    path.parent,
                    parent_descriptor,
                    item["parent_metadata"],
                )

            parent_bindings: Dict[
                Tuple[int, int], Tuple[Path, int, os.stat_result]
            ] = {}
            parent_sources = [
                *((Path(str(item["destination"])).parent, item) for item in staged),
                *((Path(str(item["destination"])).parent, item) for item in cleanup_items),
                *((Path(str(item["destination"])).parent, item) for item in cleanup_directory_items),
                *((Path(str(item["path"])).parent, item) for item in directory_items),
            ]
            for parent_path, item in parent_sources:
                descriptor = int(item["parent_fd"])
                expected = item["parent_metadata"]
                opened = os.fstat(descriptor)
                identity = (int(opened.st_dev), int(opened.st_ino))
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or identity != (int(expected.st_dev), int(expected.st_ino))
                ):
                    raise BackupIntegrityError(
                        "Restore-Elternverzeichnis driftete vor dauerhaftem Schreiben: {}".format(
                            parent_path
                        )
                    )
                _verify_parent_binding(parent_path, descriptor, expected)
                previous = parent_bindings.get(identity)
                if previous is not None and previous[0] != parent_path:
                    raise BackupIntegrityError(
                        "Restore-Elternverzeichnis ist mehrdeutig gebunden: {}".format(
                            parent_path
                        )
                    )
                parent_bindings.setdefault(
                    identity,
                    (parent_path, descriptor, expected),
                )

            for parent_path, descriptor, expected in sorted(
                parent_bindings.values(),
                key=lambda value: (len(value[0].parts), str(value[0])),
                reverse=True,
            ):
                os.fsync(descriptor)
                _verify_parent_binding(parent_path, descriptor, expected)

            # Der receiptgebundene Payloadguard läuft nach allen sichtbaren
            # Swaps und Metadaten, aber vor jeder systemd-Maskenprojektion und
            # damit vor dem ersten daemon-reload auf restaurierte Unitnamen.
            if restored_payload_guard is not None:
                restored_payload_guard()

            # Dieser Callback ist die letzte fehlschlagbare Änderung am
            # sichtbaren Zustand. Ein Fehler läuft in den vollständigen
            # Payload-Rücklauf. Nach erfolgreicher Rückkehr wird nur noch das
            # nicht fehlschlagbare Commit-Flag gesetzt; die versteckte
            # Quarantänebereinigung ist reine Nacharbeit nach dem Commit.
            if before_commit is not None:
                before_commit()
            committed = True
        except Exception as restore_exc:
            rollback_errors: List[str] = []
            for item in reversed(cleanup_applied):
                parent_descriptor = int(item["parent_fd"])
                destination = Path(str(item["destination"]))
                try:
                    if item.get("placeholder_removed"):
                        try:
                            os.stat(destination.name, dir_fd=parent_descriptor, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise BackupIntegrityError("Cleanup-Ziel wurde nach Commit-Vorbereitung neu belegt")
                    else:
                        placeholder = os.stat(destination.name, dir_fd=parent_descriptor, follow_symlinks=False)
                        if (
                            (placeholder.st_dev, placeholder.st_ino)
                            != (item["placeholder_dev"], item["placeholder_ino"])
                            or not stat.S_ISREG(placeholder.st_mode)
                        ):
                            raise BackupIntegrityError("Cleanup-Ziel wurde vor Ruecklauf neu belegt")
                        os.unlink(destination.name, dir_fd=parent_descriptor)
                    _replace_between(
                        parent_descriptor,
                        str(item["old_name"]),
                        parent_descriptor,
                        destination.name,
                        rollback_replace_func,
                    )
                    item["old_name"] = None
                    if item.get("is_directory"):
                        restored = os.open(
                            destination.name,
                            os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                            dir_fd=parent_descriptor,
                        )
                        try:
                            metadata = os.fstat(restored)
                            if (
                                (metadata.st_dev, metadata.st_ino) != (item["dev"], item["ino"])
                                or _directory_tree_digest(restored) != item["tree_digest"]
                            ):
                                raise BackupIntegrityError("Cleanup-Verzeichnis wurde nicht exakt restauriert")
                        finally:
                            os.close(restored)
                    else:
                        _verify_entry(
                            parent_descriptor,
                            destination.name,
                            str(item["sha256"]),
                            int(item["mode"]),
                            int(item["uid"]),
                            int(item["gid"]),
                        )
                    _verify_parent_binding(destination.parent, parent_descriptor, item["parent_metadata"])
                except Exception as rollback_exc:
                    rollback_errors.append("{}: {}".format(destination, rollback_exc))

            for item in reversed(applied):
                parent_descriptor = int(item["parent_fd"])
                destination = Path(str(item["destination"]))
                try:
                    try:
                        current = _entry_metadata(parent_descriptor, destination.name)
                    except FileNotFoundError:
                        current = None
                    if current is not None:
                        if item["new_installed"]:
                            if (
                                (current.st_dev, current.st_ino) != (item["new_dev"], item["new_ino"])
                                or _entry_sha256(parent_descriptor, destination.name) != item["new_sha"]
                            ):
                                raise BackupIntegrityError("Restore-Ziel wurde vor Ruecklauf fremd ersetzt")
                        elif (
                            (current.st_dev, current.st_ino)
                            != (item["placeholder_dev"], item["placeholder_ino"])
                        ):
                            raise BackupIntegrityError("Restore-Platzhalter wurde vor Ruecklauf ersetzt")
                        os.unlink(destination.name, dir_fd=parent_descriptor)
                    if item["original_exists"]:
                        _replace_between(
                            parent_descriptor,
                            str(item["old_name"]),
                            parent_descriptor,
                            destination.name,
                            rollback_replace_func,
                        )
                        item["old_name"] = None
                        _verify_entry(
                            parent_descriptor,
                            destination.name,
                            str(item["original_sha"]),
                            int(item["original_mode"]),
                            int(item["original_uid"]),
                            int(item["original_gid"]),
                        )
                        superseded = item.get("superseded_old_name")
                        if superseded:
                            os.unlink(str(superseded), dir_fd=parent_descriptor)
                            item["superseded_old_name"] = None
                    else:
                        try:
                            os.stat(destination.name, dir_fd=parent_descriptor, follow_symlinks=False)
                            raise BackupIntegrityError("Neu angelegtes Ziel blieb nach Ruecklauf bestehen")
                        except FileNotFoundError:
                            pass
                    _verify_parent_binding(destination.parent, parent_descriptor, item["parent_metadata"])
                except Exception as rollback_exc:
                    rollback_errors.append("{}: {}".format(destination, rollback_exc))

            for item in reversed(directory_items):
                path = Path(str(item["path"]))
                try:
                    if item.get("created"):
                        os.rmdir(path.name, dir_fd=int(item["parent_fd"]))
                        item["removed"] = True
                    elif item.get("metadata_applied"):
                        _apply_descriptor_metadata(
                            int(item["fd"]),
                            int(item["original_mode"]),
                            int(item["original_uid"]),
                            int(item["original_gid"]),
                            str(path),
                        )
                    _verify_parent_binding(path.parent, int(item["parent_fd"]), item["parent_metadata"])
                except Exception as rollback_exc:
                    rollback_errors.append("{}: {}".format(path, rollback_exc))

            if rollback_errors:
                raise BackupIntegrityError(
                    "Restore fehlgeschlagen ({}) UND Ruecklauf unvollstaendig: {}".format(
                        restore_exc, "; ".join(rollback_errors)
                    )
                )
            raise BackupIntegrityError(
                "Restore fehlgeschlagen; vollstaendiger Ruecklauf verifiziert: {}".format(restore_exc)
            )

        cleanup_residues: List[str] = []
        for item in staged:
            old_name = item.get("old_name")
            if old_name:
                try:
                    os.unlink(str(old_name), dir_fd=int(item["parent_fd"]))
                    item["old_name"] = None
                except OSError:
                    cleanup_residues.append(str(item["destination"]))
        for item in cleanup_items:
            old_name = item.get("old_name")
            if old_name:
                try:
                    os.unlink(str(old_name), dir_fd=int(item["parent_fd"]))
                    item["old_name"] = None
                except OSError:
                    cleanup_residues.append(str(item["destination"]))
        for item in cleanup_directory_items:
            old_name = item.get("old_name")
            if old_name:
                try:
                    _remove_directory_tree_at(
                        int(item["parent_fd"]),
                        str(old_name),
                        int(item["dev"]),
                        int(item["ino"]),
                    )
                    item["old_name"] = None
                except (OSError, BackupIntegrityError):
                    cleanup_residues.append(str(item["destination"]))
        if cleanup_residues:
            print(
                "[!] Restore ist committed und verifiziert; {} verborgene Quarantaene-Reste "
                "konnten nur fuer spaetere Bereinigung vorgemerkt werden.".format(len(cleanup_residues))
            )
        return len(applied)
    finally:
        try:
            for item in staged:
                parent_descriptor = int(item["parent_fd"])
                new_name = item.get("new_name")
                if new_name:
                    try:
                        os.unlink(str(new_name), dir_fd=parent_descriptor)
                    except OSError:
                        pass
                if committed and item.get("old_name"):
                    try:
                        os.unlink(str(item["old_name"]), dir_fd=parent_descriptor)
                    except OSError:
                        pass
                if committed and item.get("superseded_old_name"):
                    try:
                        os.unlink(str(item["superseded_old_name"]), dir_fd=parent_descriptor)
                    except OSError:
                        pass
                os.close(parent_descriptor)
            for item in cleanup_items:
                parent_descriptor = int(item["parent_fd"])
                if committed and item.get("old_name"):
                    try:
                        os.unlink(str(item["old_name"]), dir_fd=parent_descriptor)
                    except OSError:
                        pass
                os.close(parent_descriptor)
            for item in cleanup_directory_items:
                parent_descriptor = int(item["parent_fd"])
                if committed and item.get("old_name"):
                    try:
                        _remove_directory_tree_at(
                            parent_descriptor,
                            str(item["old_name"]),
                            int(item["dev"]),
                            int(item["ino"]),
                        )
                    except (FileNotFoundError, OSError, BackupIntegrityError):
                        pass
                os.close(int(item["fd"]))
                os.close(parent_descriptor)
            for item in reversed(directory_items):
                try:
                    os.close(int(item["fd"]))
                except OSError:
                    pass
                if not committed and item.get("created") and not item.get("removed"):
                    try:
                        os.rmdir(
                            Path(str(item["path"])).name,
                            dir_fd=int(item["parent_fd"]),
                        )
                    except OSError:
                        pass
                os.close(int(item["parent_fd"]))
        finally:
            _restore_descriptor_budget(previous_descriptor_limit)
