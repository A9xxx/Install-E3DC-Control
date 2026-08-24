"""Kanonischer, rebootfester Master-Journal-Vertrag für Updates.

Der Helfer persistiert nur Identität und Fortschritt einer Transaktion. Er
entscheidet nicht über Vorwärtsabschluss oder Rücklauf. Hashes und Inodes
werden ausschließlich vom Updater abgeleitet und sind keine Nutzereingaben.

Die Trennung ist absichtlich streng:

* Kontext-, Nebenflächen- und systemd-Preimage bleiben echte, unveränderliche
  Inode-/Hash-Receipts.
* Package und Safety wechseln beim Commit ihren Inode. Das Journal bindet
  deshalb deren unveränderliche Recovery-Semantik statt eines Dateiinodes.
* Das Quiesced-Overlay existiert erst unmittelbar vor der Produktmutation und
  wird genau beim Wechsel nach ``product_mutating`` gebunden.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import errno
import hashlib
import json
import os
import re
import secrets
import stat
from typing import Mapping


RECOVERY_JOURNAL_SCHEMA = "e3dc_update_recovery_journal_v1"
RECOVERY_JOURNAL_NAME = "recovery-journal.json"
RECOVERY_JOURNAL_PATH = os.path.join(
    "/var/lib/e3dc-update-safety",
    RECOVERY_JOURNAL_NAME,
)
MAX_RECOVERY_JOURNAL_BYTES = 512 * 1024
MAX_COMPANION_RECEIPT_BYTES = 32 * 1024 * 1024

PHASE_PREPRODUCT = "preproduct"
PHASE_PRODUCT_MUTATING = "product_mutating"
PHASE_COMMITTED = "committed"
PHASE_ROLLED_BACK = "rolled_back"
RECOVERY_JOURNAL_PHASES = (
    PHASE_PREPRODUCT,
    PHASE_PRODUCT_MUTATING,
    PHASE_COMMITTED,
    PHASE_ROLLED_BACK,
)

GATE_MODE_DYNAMIC = "dynamic"
GATE_MODE_STATIC = "static"
GATE_MODES = frozenset({GATE_MODE_DYNAMIC, GATE_MODE_STATIC})

_IMMUTABLE_RECEIPT_KINDS = ("context", "surface", "systemd")
_CAPTURABLE_RECEIPT_KINDS = frozenset((*_IMMUTABLE_RECEIPT_KINDS, "overlay"))
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_RELEASE_TAG_RE = re.compile(r"v\d+\.\d+\.\d+[A-Za-z0-9._-]*\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_CREATE_STAGING_RE = re.compile(
    r"\.e3dc-recovery-journal-[0-9]+-[0-9a-f]{24}\Z"
)
_VALID_ROLES = frozenset({"off", "master", "slave", "shadow"})


class UpdateRecoveryJournalError(RuntimeError):
    """Fail-closed Fehler mit Code und konkreter Handlungsanweisung."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        solution: str,
        subject: str = RECOVERY_JOURNAL_PATH,
    ) -> None:
        self.code = str(code)
        self.solution = str(solution)
        self.subject = str(subject)
        super().__init__(
            f"[{self.code}] {message} Betroffen: {self.subject}. "
            f"Lösung: {self.solution}"
        )


class UpdateRecoveryJournalPersistenceError(UpdateRecoveryJournalError):
    """Atomarer Namenswechsel wirkte eventuell, der fsync-Abschluss blieb offen."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        solution: str,
        journal: "RecoveryJournalContract | None",
        subject: str = RECOVERY_JOURNAL_PATH,
    ) -> None:
        self.journal = journal
        super().__init__(code, message, solution=solution, subject=subject)


@dataclass(frozen=True)
class RecoverySourceBinding:
    kind: str
    version: str | None
    commit: str | None
    repository_present: bool
    repository_rebuild_required: bool
    identity_sha256: str


@dataclass(frozen=True)
class RecoveryTargetBinding:
    commit: str
    tag: str
    role: str
    identity_sha256: str


@dataclass(frozen=True)
class RecoveryFullBackupBinding:
    backup_id: str
    manifest_sha256: str


@dataclass(frozen=True)
class RecoveryReceiptReference:
    """Echte unveränderliche Root-Datei mit gebundenem Inode und Inhalt."""

    kind: str
    path: str
    device: int
    inode: int
    size: int
    sha256: str


@dataclass(frozen=True)
class RecoveryImmutableReceiptReferences:
    context: RecoveryReceiptReference
    surface: RecoveryReceiptReference
    systemd: RecoveryReceiptReference


@dataclass(frozen=True)
class RecoveryPackageBinding:
    """Inode-unabhängige Paket-Recovery-Semantik über alle Receipt-Zustände."""

    path: str
    transaction_id: str
    install_root: str
    full_backup_id: str
    target_identity_sha256: str
    prestate_shape_sha256: str
    gate_mode: str
    static_contract_sha256: str | None


@dataclass(frozen=True)
class RecoverySafetyBinding:
    """Dynamisches Safety-Receipt oder statischer RecoveryBootblockContract."""

    mode: str
    transaction_id: str
    install_root: str
    full_backup_id: str
    target_identity_sha256: str
    receipt_path: str | None
    receipt_shape_sha256: str | None
    static_contract_sha256: str | None


@dataclass(frozen=True)
class RecoveryOverlayBinding:
    backup_id: str
    manifest_sha256: str
    receipt: RecoveryReceiptReference


@dataclass(frozen=True)
class RecoveryJournalPayload:
    phase: str
    transaction_id: str
    install_root: str
    install_user: str
    source: RecoverySourceBinding
    target: RecoveryTargetBinding
    transition_id: str
    full_backup: RecoveryFullBackupBinding
    immutable_receipts: RecoveryImmutableReceiptReferences
    package: RecoveryPackageBinding | None
    safety: RecoverySafetyBinding | None
    overlay: RecoveryOverlayBinding | None
    binding_sha256: str
    phase_state_sha256: str


@dataclass(frozen=True)
class RecoveryJournalContract:
    payload: RecoveryJournalPayload
    journal_path: str
    journal_device: int
    journal_inode: int
    journal_size: int
    journal_sha256: str


def _solution_relaunch() -> str:
    return (
        "starte denselben Updatebefehl erneut. Der aktuelle Updater wertet "
        "das vorhandene Journal automatisch aus; die Datei nicht manuell "
        "bearbeiten oder löschen"
    )


def _solution_state_directory(path: str) -> str:
    parent = os.path.dirname(path)
    return (
        f"prüfe `sudo ls -ld {parent}`. Fehlt ausschließlich dieses "
        "Updater-Verzeichnis, lege es mit "
        f"`sudo install -d -o root -g root -m 0700 {parent}` an und starte "
        "danach denselben Updatebefehl erneut"
    )


def _contract_error(message: str, *, subject: str = RECOVERY_JOURNAL_PATH):
    raise UpdateRecoveryJournalError(
        "E3DC-UPD-JOURNAL-001",
        message,
        solution=(
            "erzeuge den Vertrag ausschließlich durch den aktuellen Updater "
            "neu; vorhandene Journal- oder Receipt-Dateien nicht manuell anpassen"
        ),
        subject=subject,
    )


def _strict_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} ist keine kanonische SHA-256")
    return value


def _strict_commit(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
        raise ValueError("Commit ist keine vollständige kleingeschriebene SHA-1")
    return value


def _strict_int(value: object, *, label: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{label} ist keine zulässige Ganzzahl")
    return value


def _strict_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} ist kein boolescher Wert")
    return value


def _optional_text(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value.encode("utf-8")) > 128
        or any(character in value for character in "\x00\r\n\t")
    ):
        raise ValueError(f"{label} ist nicht kanonisch")
    return value


def _canonical_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} fehlt")
    if (
        not os.path.isabs(value)
        or os.path.normpath(value) != value
        or os.path.abspath(value) != value
        or os.path.realpath(value) != value
        or value == "/"
    ):
        raise ValueError(f"{label} ist kein kanonischer absoluter Pfad")
    return value


def _canonical_json(mapping: Mapping[str, object], *, maximum: int) -> bytes:
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
        raise ValueError("Journal ist nicht kanonisch JSON-kodierbar") from exc
    if not payload or len(payload) > maximum:
        raise ValueError("Journal überschreitet sein festes Größenlimit")
    return payload


def _digest_mapping(mapping: Mapping[str, object]) -> str:
    return hashlib.sha256(
        _canonical_json(mapping, maximum=MAX_RECOVERY_JOURNAL_BYTES)
    ).hexdigest()


def _source_core(source: RecoverySourceBinding) -> dict[str, object]:
    return {
        "kind": source.kind,
        "version": source.version,
        "commit": source.commit,
        "repository_present": source.repository_present,
        "repository_rebuild_required": source.repository_rebuild_required,
    }


def _source_mapping(source: RecoverySourceBinding) -> dict[str, object]:
    return {**_source_core(source), "identity_sha256": source.identity_sha256}


def make_source_binding(
    *,
    kind: str,
    version: str | None,
    commit: str | None,
    repository_present: bool,
    repository_rebuild_required: bool,
) -> RecoverySourceBinding:
    try:
        if not isinstance(kind, str) or not _TOKEN_RE.fullmatch(kind):
            raise ValueError("Quellart ist kein kanonisches Token")
        bound_commit = _strict_commit(commit, optional=True)
        present = _strict_bool(repository_present, label="repository_present")
        rebuild = _strict_bool(
            repository_rebuild_required,
            label="repository_rebuild_required",
        )
        if bound_commit is not None and not present:
            raise ValueError("Quellcommit ohne vorhandenes Repository ist widersprüchlich")
        provisional = RecoverySourceBinding(
            kind=kind,
            version=_optional_text(version, label="Quellversion"),
            commit=bound_commit,
            repository_present=present,
            repository_rebuild_required=rebuild,
            identity_sha256="0" * 64,
        )
        return replace(
            provisional,
            identity_sha256=_digest_mapping(_source_core(provisional)),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        _contract_error(f"Quellbindung ist ungültig: {exc}")


def _target_core(target: RecoveryTargetBinding) -> dict[str, object]:
    return {"commit": target.commit, "tag": target.tag, "role": target.role}


def _target_mapping(target: RecoveryTargetBinding) -> dict[str, object]:
    return {**_target_core(target), "identity_sha256": target.identity_sha256}


def make_target_binding(
    *,
    commit: str,
    tag: str,
    role: str,
) -> RecoveryTargetBinding:
    try:
        bound_commit = _strict_commit(commit)
        if not isinstance(tag, str) or not _RELEASE_TAG_RE.fullmatch(tag):
            raise ValueError("Ziel-Tag ist nicht kanonisch")
        if role not in _VALID_ROLES:
            raise ValueError("Zielrolle ist nicht unterstützt")
        provisional = RecoveryTargetBinding(
            commit=bound_commit or "",
            tag=tag,
            role=role,
            identity_sha256="0" * 64,
        )
        return replace(
            provisional,
            identity_sha256=_digest_mapping(_target_core(provisional)),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        _contract_error(f"Zielbindung ist ungültig: {exc}")


def make_full_backup_binding(
    *,
    backup_id: str,
    manifest_sha256: str,
) -> RecoveryFullBackupBinding:
    try:
        return RecoveryFullBackupBinding(
            backup_id=_strict_sha256(backup_id, label="Vollbackup-ID"),
            manifest_sha256=_strict_sha256(
                manifest_sha256,
                label="Vollbackup-Manifest",
            ),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        _contract_error(f"Vollbackup-Bindung ist ungültig: {exc}")


def _receipt_mapping(reference: RecoveryReceiptReference) -> dict[str, object]:
    return {
        "kind": reference.kind,
        "path": reference.path,
        "device": reference.device,
        "inode": reference.inode,
        "size": reference.size,
        "sha256": reference.sha256,
    }


def _validate_receipt(
    reference: RecoveryReceiptReference,
    *,
    expected_kind: str,
) -> RecoveryReceiptReference:
    if not isinstance(reference, RecoveryReceiptReference):
        raise ValueError(f"Receipt {expected_kind} besitzt den falschen Typ")
    if expected_kind not in _CAPTURABLE_RECEIPT_KINDS or reference.kind != expected_kind:
        raise ValueError(f"Receipt {expected_kind} ist vertauscht")
    _canonical_path(reference.path, label=f"Receipt-Pfad {expected_kind}")
    _strict_int(reference.device, label="Receipt-Gerät")
    _strict_int(reference.inode, label="Receipt-Inode", minimum=1)
    size = _strict_int(reference.size, label="Receipt-Größe", minimum=1)
    if size > MAX_COMPANION_RECEIPT_BYTES:
        raise ValueError("Receipt überschreitet das Größenlimit")
    _strict_sha256(reference.sha256, label="Receipt-SHA256")
    return reference


def make_immutable_receipt_references(
    *,
    context: RecoveryReceiptReference,
    surface: RecoveryReceiptReference,
    systemd: RecoveryReceiptReference,
) -> RecoveryImmutableReceiptReferences:
    try:
        references = RecoveryImmutableReceiptReferences(
            context=_validate_receipt(context, expected_kind="context"),
            surface=_validate_receipt(surface, expected_kind="surface"),
            systemd=_validate_receipt(systemd, expected_kind="systemd"),
        )
        values = tuple(getattr(references, kind) for kind in _IMMUTABLE_RECEIPT_KINDS)
        if len({item.path for item in values}) != len(values):
            raise ValueError("Unveränderliche Receipts teilen einen Pfad")
        if len({(item.device, item.inode) for item in values}) != len(values):
            raise ValueError("Unveränderliche Receipts teilen einen Inode")
        return references
    except (AttributeError, TypeError, ValueError) as exc:
        _contract_error(f"Unveränderlicher Receipt-Satz ist ungültig: {exc}")


def _immutable_receipts_mapping(
    references: RecoveryImmutableReceiptReferences,
) -> dict[str, object]:
    return {
        kind: _receipt_mapping(getattr(references, kind))
        for kind in _IMMUTABLE_RECEIPT_KINDS
    }


def make_package_binding(
    *,
    path: str,
    transaction_id: str,
    install_root: str,
    full_backup_id: str,
    target_identity_sha256: str,
    prestate_shape_sha256: str,
    gate_mode: str,
    static_contract_sha256: str | None = None,
) -> RecoveryPackageBinding:
    try:
        if gate_mode not in GATE_MODES:
            raise ValueError("Package-Gate-Modus ist unbekannt")
        static_digest = (
            _strict_sha256(static_contract_sha256, label="Static-Contract")
            if static_contract_sha256 is not None
            else None
        )
        if (gate_mode == GATE_MODE_STATIC) != (static_digest is not None):
            raise ValueError("Package-Gate-Modus und Static-Contract widersprechen sich")
        return RecoveryPackageBinding(
            path=_canonical_path(path, label="Package-Receipt-Pfad"),
            transaction_id=_strict_sha256(transaction_id, label="Package-Transaktion"),
            install_root=_canonical_path(install_root, label="Package-Installationspfad"),
            full_backup_id=_strict_sha256(full_backup_id, label="Package-Vollbackup"),
            target_identity_sha256=_strict_sha256(
                target_identity_sha256,
                label="Package-Zielidentität",
            ),
            prestate_shape_sha256=_strict_sha256(
                prestate_shape_sha256,
                label="Package-Prestate-Shape",
            ),
            gate_mode=gate_mode,
            static_contract_sha256=static_digest,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        _contract_error(f"Package-Semantik ist ungültig: {exc}")


def _package_mapping(package: RecoveryPackageBinding | None) -> object:
    if package is None:
        return None
    return {
        "path": package.path,
        "transaction_id": package.transaction_id,
        "install_root": package.install_root,
        "full_backup_id": package.full_backup_id,
        "target_identity_sha256": package.target_identity_sha256,
        "prestate_shape_sha256": package.prestate_shape_sha256,
        "gate_mode": package.gate_mode,
        "static_contract_sha256": package.static_contract_sha256,
    }


def make_dynamic_safety_binding(
    *,
    receipt_path: str,
    transaction_id: str,
    install_root: str,
    full_backup_id: str,
    target_identity_sha256: str,
    receipt_shape_sha256: str,
) -> RecoverySafetyBinding:
    try:
        return RecoverySafetyBinding(
            mode=GATE_MODE_DYNAMIC,
            transaction_id=_strict_sha256(transaction_id, label="Safety-Transaktion"),
            install_root=_canonical_path(install_root, label="Safety-Installationspfad"),
            full_backup_id=_strict_sha256(full_backup_id, label="Safety-Vollbackup"),
            target_identity_sha256=_strict_sha256(
                target_identity_sha256,
                label="Safety-Zielidentität",
            ),
            receipt_path=_canonical_path(receipt_path, label="Safety-Receipt-Pfad"),
            receipt_shape_sha256=_strict_sha256(
                receipt_shape_sha256,
                label="Safety-Receipt-Shape",
            ),
            static_contract_sha256=None,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        _contract_error(f"Dynamische Safety-Semantik ist ungültig: {exc}")


def make_static_safety_binding(
    *,
    transaction_id: str,
    install_root: str,
    full_backup_id: str,
    target_identity_sha256: str,
    static_contract_sha256: str,
) -> RecoverySafetyBinding:
    try:
        return RecoverySafetyBinding(
            mode=GATE_MODE_STATIC,
            transaction_id=_strict_sha256(transaction_id, label="Safety-Transaktion"),
            install_root=_canonical_path(install_root, label="Safety-Installationspfad"),
            full_backup_id=_strict_sha256(full_backup_id, label="Safety-Vollbackup"),
            target_identity_sha256=_strict_sha256(
                target_identity_sha256,
                label="Safety-Zielidentität",
            ),
            receipt_path=None,
            receipt_shape_sha256=None,
            static_contract_sha256=_strict_sha256(
                static_contract_sha256,
                label="Static-RecoveryBootblockContract",
            ),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        _contract_error(f"Statische Safety-Semantik ist ungültig: {exc}")


def _safety_mapping(safety: RecoverySafetyBinding | None) -> object:
    if safety is None:
        return None
    return {
        "mode": safety.mode,
        "transaction_id": safety.transaction_id,
        "install_root": safety.install_root,
        "full_backup_id": safety.full_backup_id,
        "target_identity_sha256": safety.target_identity_sha256,
        "receipt_path": safety.receipt_path,
        "receipt_shape_sha256": safety.receipt_shape_sha256,
        "static_contract_sha256": safety.static_contract_sha256,
    }


def make_overlay_binding(
    *,
    backup_id: str,
    manifest_sha256: str,
    receipt: RecoveryReceiptReference,
) -> RecoveryOverlayBinding:
    try:
        return RecoveryOverlayBinding(
            backup_id=_strict_sha256(backup_id, label="Overlay-Backup-ID"),
            manifest_sha256=_strict_sha256(
                manifest_sha256,
                label="Overlay-Manifest",
            ),
            receipt=_validate_receipt(receipt, expected_kind="overlay"),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        _contract_error(f"Overlay-Bindung ist ungültig: {exc}")


def _overlay_mapping(overlay: RecoveryOverlayBinding | None) -> object:
    if overlay is None:
        return None
    return {
        "backup_id": overlay.backup_id,
        "manifest_sha256": overlay.manifest_sha256,
        "receipt": _receipt_mapping(overlay.receipt),
    }


def _full_backup_mapping(backup: RecoveryFullBackupBinding) -> dict[str, object]:
    return {"backup_id": backup.backup_id, "manifest_sha256": backup.manifest_sha256}


def _immutable_mapping(payload: RecoveryJournalPayload) -> dict[str, object]:
    return {
        "transaction_id": payload.transaction_id,
        "install_root": payload.install_root,
        "install_user": payload.install_user,
        "source": _source_mapping(payload.source),
        "target": _target_mapping(payload.target),
        "transition_id": payload.transition_id,
        "full_backup": _full_backup_mapping(payload.full_backup),
        "receipts": _immutable_receipts_mapping(payload.immutable_receipts),
    }


def _phase_mapping(payload: RecoveryJournalPayload) -> dict[str, object]:
    return {
        "package": _package_mapping(payload.package),
        "safety": _safety_mapping(payload.safety),
        "overlay": _overlay_mapping(payload.overlay),
    }


def _assert_semantic_anchor(payload: RecoveryJournalPayload) -> None:
    package = payload.package
    safety = payload.safety
    if (package is None) != (safety is None):
        raise ValueError("Package und Safety müssen gemeinsam gebunden sein")
    if package is None or safety is None:
        return
    expected = (
        payload.transaction_id,
        payload.install_root,
        payload.full_backup.backup_id,
        payload.target.identity_sha256,
    )
    if (
        (
            package.transaction_id,
            package.install_root,
            package.full_backup_id,
            package.target_identity_sha256,
        )
        != expected
        or (
            safety.transaction_id,
            safety.install_root,
            safety.full_backup_id,
            safety.target_identity_sha256,
        )
        != expected
        or package.gate_mode != safety.mode
        or package.static_contract_sha256 != safety.static_contract_sha256
    ):
        raise ValueError("Package-, Safety- und Master-Journal-Anker widersprechen sich")
    if safety.mode == GATE_MODE_DYNAMIC:
        if (
            safety.receipt_path is None
            or safety.receipt_shape_sha256 is None
            or safety.static_contract_sha256 is not None
        ):
            raise ValueError("Dynamische Safety-Bindung ist unvollständig")
    elif safety.mode == GATE_MODE_STATIC:
        if (
            safety.receipt_path is not None
            or safety.receipt_shape_sha256 is not None
            or safety.static_contract_sha256 is None
        ):
            raise ValueError("Statische Safety-Bindung darf keinen Receiptpfad erfinden")
    else:
        raise ValueError("Safety-Gate-Modus ist unbekannt")


def _validate_payload(payload: RecoveryJournalPayload) -> RecoveryJournalPayload:
    if not isinstance(payload, RecoveryJournalPayload):
        raise ValueError("Journal-Payload besitzt den falschen Typ")
    if payload.phase not in RECOVERY_JOURNAL_PHASES:
        raise ValueError("Journalphase ist unbekannt")
    _strict_sha256(payload.transaction_id, label="Transaktions-ID")
    _canonical_path(payload.install_root, label="Installationspfad")
    if not isinstance(payload.install_user, str) or not _USER_RE.fullmatch(
        payload.install_user
    ):
        raise ValueError("Installationsbenutzer ist nicht kanonisch")
    expected_source = make_source_binding(
        kind=payload.source.kind,
        version=payload.source.version,
        commit=payload.source.commit,
        repository_present=payload.source.repository_present,
        repository_rebuild_required=payload.source.repository_rebuild_required,
    )
    expected_target = make_target_binding(
        commit=payload.target.commit,
        tag=payload.target.tag,
        role=payload.target.role,
    )
    if expected_source != payload.source or expected_target != payload.target:
        raise ValueError("Quell- oder Zielidentität ist nicht selbstkonsistent")
    _strict_sha256(payload.transition_id, label="Transition-ID")
    expected_backup = make_full_backup_binding(**_full_backup_mapping(payload.full_backup))
    expected_receipts = make_immutable_receipt_references(
        **{
            kind: getattr(payload.immutable_receipts, kind)
            for kind in _IMMUTABLE_RECEIPT_KINDS
        }
    )
    if expected_backup != payload.full_backup or expected_receipts != payload.immutable_receipts:
        raise ValueError("Vollbackup- oder Preimage-Receipt-Bindung driftete")
    _assert_semantic_anchor(payload)
    if payload.phase == PHASE_PREPRODUCT:
        if payload.overlay is not None:
            raise ValueError("preproduct darf noch kein Overlay binden")
    elif payload.phase in {PHASE_PRODUCT_MUTATING, PHASE_COMMITTED} and (
        payload.package is None
        or payload.safety is None
        or payload.overlay is None
    ):
        raise ValueError("Produktmutation/Commit benötigt Package, Safety und Overlay")
    elif payload.phase == PHASE_ROLLED_BACK and (
        payload.overlay is not None
        and (payload.package is None or payload.safety is None)
    ):
        raise ValueError("rolled_back besitzt ein Overlay ohne Package-/Safety-Bindung")
    if payload.overlay is not None:
        expected_overlay = make_overlay_binding(
            backup_id=payload.overlay.backup_id,
            manifest_sha256=payload.overlay.manifest_sha256,
            receipt=payload.overlay.receipt,
        )
        if expected_overlay != payload.overlay:
            raise ValueError("Overlay-Bindung driftete")
        occupied_paths = {
            getattr(payload.immutable_receipts, kind).path
            for kind in _IMMUTABLE_RECEIPT_KINDS
        }
        occupied_inodes = {
            (
                getattr(payload.immutable_receipts, kind).device,
                getattr(payload.immutable_receipts, kind).inode,
            )
            for kind in _IMMUTABLE_RECEIPT_KINDS
        }
        if (
            payload.overlay.receipt.path in occupied_paths
            or (payload.overlay.receipt.device, payload.overlay.receipt.inode)
            in occupied_inodes
            or payload.overlay.backup_id == payload.full_backup.backup_id
        ):
            raise ValueError("Overlay wurde mit Vollbackup oder Preimage-Receipt vertauscht")
    semantic_paths = set()
    if payload.package is not None:
        semantic_paths.add(payload.package.path)
    if payload.safety is not None and payload.safety.receipt_path is not None:
        semantic_paths.add(payload.safety.receipt_path)
    immutable_paths = {
        getattr(payload.immutable_receipts, kind).path
        for kind in _IMMUTABLE_RECEIPT_KINDS
    }
    if payload.overlay is not None:
        immutable_paths.add(payload.overlay.receipt.path)
    expected_semantic_count = 0
    if payload.package is not None:
        expected_semantic_count += 1
    if payload.safety is not None and payload.safety.receipt_path is not None:
        expected_semantic_count += 1
    if len(semantic_paths) != expected_semantic_count or semantic_paths & immutable_paths:
        raise ValueError("Receiptpfade sind nicht eindeutig getrennt")
    if payload.binding_sha256 != _digest_mapping(_immutable_mapping(payload)):
        raise ValueError("Unveränderliche Journalbindung besitzt einen falschen Hash")
    if payload.phase_state_sha256 != _digest_mapping(_phase_mapping(payload)):
        raise ValueError("Phasenzustand besitzt einen falschen Hash")
    return payload


def _rehash_payload(payload: RecoveryJournalPayload) -> RecoveryJournalPayload:
    payload = replace(
        payload,
        binding_sha256=_digest_mapping(_immutable_mapping(payload)),
        phase_state_sha256=_digest_mapping(_phase_mapping(payload)),
    )
    return _validate_payload(payload)


def make_recovery_journal_payload(
    *,
    transaction_id: str,
    install_root: str,
    install_user: str,
    source: RecoverySourceBinding,
    target: RecoveryTargetBinding,
    transition_id: str,
    full_backup: RecoveryFullBackupBinding,
    immutable_receipts: RecoveryImmutableReceiptReferences,
) -> RecoveryJournalPayload:
    """Erzeugt den ersten ``preproduct``-Stand vor Bootblock und Paketreceipt."""

    try:
        if not isinstance(install_user, str) or not _USER_RE.fullmatch(install_user):
            raise ValueError("Installationsbenutzer ist nicht kanonisch")
        provisional = RecoveryJournalPayload(
            phase=PHASE_PREPRODUCT,
            transaction_id=_strict_sha256(transaction_id, label="Transaktions-ID"),
            install_root=_canonical_path(install_root, label="Installationspfad"),
            install_user=install_user,
            source=source,
            target=target,
            transition_id=_strict_sha256(transition_id, label="Transition-ID"),
            full_backup=full_backup,
            immutable_receipts=immutable_receipts,
            package=None,
            safety=None,
            overlay=None,
            binding_sha256="0" * 64,
            phase_state_sha256="0" * 64,
        )
        return _rehash_payload(provisional)
    except UpdateRecoveryJournalError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        _contract_error(f"Master-Journal-Bindung ist ungültig: {exc}")


def _payload_mapping(payload: RecoveryJournalPayload) -> dict[str, object]:
    _validate_payload(payload)
    return {
        "schema": RECOVERY_JOURNAL_SCHEMA,
        "phase": payload.phase,
        "binding_sha256": payload.binding_sha256,
        "phase_state_sha256": payload.phase_state_sha256,
        "binding": _immutable_mapping(payload),
        "phase_state": _phase_mapping(payload),
    }


def serialize_recovery_journal_payload(payload: RecoveryJournalPayload) -> bytes:
    try:
        return _canonical_json(
            _payload_mapping(payload),
            maximum=MAX_RECOVERY_JOURNAL_BYTES,
        )
    except UpdateRecoveryJournalError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        _contract_error(f"Master-Journal kann nicht serialisiert werden: {exc}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Doppelter JSON-Schlüssel: {key}")
        result[key] = value
    return result


def _reject_noninteger(raw: str) -> object:
    raise ValueError(f"Nicht-ganzzahliger JSON-Wert ist unzulässig: {raw}")


def _exact_mapping(
    raw: object,
    *,
    keys: frozenset[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ValueError(f"{label} besitzt kein exaktes Schema")
    return raw


def _source_from_mapping(raw: object) -> RecoverySourceBinding:
    mapping = _exact_mapping(
        raw,
        keys=frozenset(
            {
                "kind",
                "version",
                "commit",
                "repository_present",
                "repository_rebuild_required",
                "identity_sha256",
            }
        ),
        label="Quellbindung",
    )
    source = make_source_binding(
        kind=mapping["kind"],
        version=mapping["version"],
        commit=mapping["commit"],
        repository_present=mapping["repository_present"],
        repository_rebuild_required=mapping["repository_rebuild_required"],
    )
    if source.identity_sha256 != mapping["identity_sha256"]:
        raise ValueError("Quellidentität driftete")
    return source


def _target_from_mapping(raw: object) -> RecoveryTargetBinding:
    mapping = _exact_mapping(
        raw,
        keys=frozenset({"commit", "tag", "role", "identity_sha256"}),
        label="Zielbindung",
    )
    target = make_target_binding(
        commit=mapping["commit"],
        tag=mapping["tag"],
        role=mapping["role"],
    )
    if target.identity_sha256 != mapping["identity_sha256"]:
        raise ValueError("Zielidentität driftete")
    return target


def _receipt_from_mapping(raw: object, *, expected_kind: str) -> RecoveryReceiptReference:
    mapping = _exact_mapping(
        raw,
        keys=frozenset({"kind", "path", "device", "inode", "size", "sha256"}),
        label=f"Receipt {expected_kind}",
    )
    return _validate_receipt(
        RecoveryReceiptReference(
            kind=mapping["kind"],
            path=mapping["path"],
            device=mapping["device"],
            inode=mapping["inode"],
            size=mapping["size"],
            sha256=mapping["sha256"],
        ),
        expected_kind=expected_kind,
    )


def _package_from_mapping(raw: object) -> RecoveryPackageBinding | None:
    if raw is None:
        return None
    mapping = _exact_mapping(
        raw,
        keys=frozenset(
            {
                "path",
                "transaction_id",
                "install_root",
                "full_backup_id",
                "target_identity_sha256",
                "prestate_shape_sha256",
                "gate_mode",
                "static_contract_sha256",
            }
        ),
        label="Package-Semantik",
    )
    return make_package_binding(**mapping)


def _safety_from_mapping(raw: object) -> RecoverySafetyBinding | None:
    if raw is None:
        return None
    mapping = _exact_mapping(
        raw,
        keys=frozenset(
            {
                "mode",
                "transaction_id",
                "install_root",
                "full_backup_id",
                "target_identity_sha256",
                "receipt_path",
                "receipt_shape_sha256",
                "static_contract_sha256",
            }
        ),
        label="Safety-Semantik",
    )
    if mapping["mode"] == GATE_MODE_DYNAMIC:
        if mapping["static_contract_sha256"] is not None:
            raise ValueError("Dynamische Safety-Semantik enthält Static-Contract")
        return make_dynamic_safety_binding(
            receipt_path=mapping["receipt_path"],
            transaction_id=mapping["transaction_id"],
            install_root=mapping["install_root"],
            full_backup_id=mapping["full_backup_id"],
            target_identity_sha256=mapping["target_identity_sha256"],
            receipt_shape_sha256=mapping["receipt_shape_sha256"],
        )
    if mapping["mode"] == GATE_MODE_STATIC:
        if mapping["receipt_path"] is not None or mapping["receipt_shape_sha256"] is not None:
            raise ValueError("Statische Safety-Semantik enthält Receiptpfad")
        return make_static_safety_binding(
            transaction_id=mapping["transaction_id"],
            install_root=mapping["install_root"],
            full_backup_id=mapping["full_backup_id"],
            target_identity_sha256=mapping["target_identity_sha256"],
            static_contract_sha256=mapping["static_contract_sha256"],
        )
    raise ValueError("Safety-Gate-Modus ist unbekannt")


def _overlay_from_mapping(raw: object) -> RecoveryOverlayBinding | None:
    if raw is None:
        return None
    mapping = _exact_mapping(
        raw,
        keys=frozenset({"backup_id", "manifest_sha256", "receipt"}),
        label="Overlay-Bindung",
    )
    return make_overlay_binding(
        backup_id=mapping["backup_id"],
        manifest_sha256=mapping["manifest_sha256"],
        receipt=_receipt_from_mapping(mapping["receipt"], expected_kind="overlay"),
    )


def parse_recovery_journal_payload(raw_payload: bytes) -> RecoveryJournalPayload:
    try:
        if (
            not isinstance(raw_payload, bytes)
            or not raw_payload
            or len(raw_payload) > MAX_RECOVERY_JOURNAL_BYTES
        ):
            raise ValueError("Journal besitzt eine ungültige Größe")
        record = json.loads(
            raw_payload.decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_float=_reject_noninteger,
            parse_constant=_reject_noninteger,
        )
        top = _exact_mapping(
            record,
            keys=frozenset(
                {
                    "schema",
                    "phase",
                    "binding_sha256",
                    "phase_state_sha256",
                    "binding",
                    "phase_state",
                }
            ),
            label="Master-Journal",
        )
        if top["schema"] != RECOVERY_JOURNAL_SCHEMA:
            raise ValueError("Master-Journal besitzt ein unbekanntes Schema")
        if _canonical_json(top, maximum=MAX_RECOVERY_JOURNAL_BYTES) != raw_payload:
            raise ValueError("Master-Journal ist nicht kanonisch kodiert")
        binding = _exact_mapping(
            top["binding"],
            keys=frozenset(
                {
                    "transaction_id",
                    "install_root",
                    "install_user",
                    "source",
                    "target",
                    "transition_id",
                    "full_backup",
                    "receipts",
                }
            ),
            label="Unveränderliche Bindung",
        )
        full = _exact_mapping(
            binding["full_backup"],
            keys=frozenset({"backup_id", "manifest_sha256"}),
            label="Vollbackup-Bindung",
        )
        receipt_mapping = _exact_mapping(
            binding["receipts"],
            keys=frozenset(_IMMUTABLE_RECEIPT_KINDS),
            label="Preimage-Receipt-Satz",
        )
        immutable_receipts = make_immutable_receipt_references(
            **{
                kind: _receipt_from_mapping(receipt_mapping[kind], expected_kind=kind)
                for kind in _IMMUTABLE_RECEIPT_KINDS
            }
        )
        phase = _exact_mapping(
            top["phase_state"],
            keys=frozenset({"package", "safety", "overlay"}),
            label="Phasenzustand",
        )
        parsed = RecoveryJournalPayload(
            phase=top["phase"],
            transaction_id=binding["transaction_id"],
            install_root=binding["install_root"],
            install_user=binding["install_user"],
            source=_source_from_mapping(binding["source"]),
            target=_target_from_mapping(binding["target"]),
            transition_id=binding["transition_id"],
            full_backup=make_full_backup_binding(**full),
            immutable_receipts=immutable_receipts,
            package=_package_from_mapping(phase["package"]),
            safety=_safety_from_mapping(phase["safety"]),
            overlay=_overlay_from_mapping(phase["overlay"]),
            binding_sha256=top["binding_sha256"],
            phase_state_sha256=top["phase_state_sha256"],
        )
        return _validate_payload(parsed)
    except UpdateRecoveryJournalError:
        raise
    except (AttributeError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        _contract_error(f"Master-Journal ist ungültig: {exc}")


def _has_unsafe_xattrs(descriptor: int) -> bool:
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


def _read_exact(descriptor: int, size: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = int(size)
    chunks: list[bytes] = []
    while remaining:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            raise ValueError("Datei endet vor ihrer gebundenen Größe")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise ValueError("Datei überschreitet ihre gebundene Größe")
    return b"".join(chunks)


def _validate_root_file(
    descriptor: int,
    *,
    maximum: int,
    expected_payload: bytes | None = None,
) -> tuple[bytes, os.stat_result]:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size <= 0
        or metadata.st_size > maximum
        or _has_unsafe_xattrs(descriptor)
    ):
        raise ValueError("Datei verletzt den Root-Receipt-Vertrag")
    payload = _read_exact(descriptor, metadata.st_size)
    if expected_payload is not None and payload != expected_payload:
        raise ValueError("Datei besitzt nicht die erwarteten Bytes")
    return payload, metadata


def _require_root(subject: str) -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise UpdateRecoveryJournalError(
            "E3DC-UPD-JOURNAL-002",
            "Master-Journal darf nur mit Root-Rechten verwaltet werden",
            solution="starte denselben Updatebefehl erneut mit `sudo`",
            subject=subject,
        )


def _open_parent(journal_path: str) -> tuple[int, str, str]:
    _require_root(str(journal_path))
    try:
        path = _canonical_path(journal_path, label="Journalpfad")
    except ValueError as exc:
        _contract_error(str(exc), subject=str(journal_path))
    parent = os.path.dirname(path)
    name = os.path.basename(path)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    if not directory_flag or not nofollow_flag:
        raise UpdateRecoveryJournalError(
            "E3DC-UPD-JOURNAL-003",
            "Kernel/Python stellt O_DIRECTORY oder O_NOFOLLOW nicht bereit",
            solution=(
                "aktualisiere Raspberry Pi OS beziehungsweise Python und starte "
                "danach denselben Updatebefehl erneut"
            ),
            subject=path,
        )
    try:
        descriptor = os.open(
            parent,
            os.O_RDONLY | directory_flag | nofollow_flag | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError as exc:
        raise UpdateRecoveryJournalError(
            "E3DC-UPD-JOURNAL-004",
            "Sicheres Journalverzeichnis fehlt",
            solution=_solution_state_directory(path),
            subject=parent,
        ) from exc
    except OSError as exc:
        raise UpdateRecoveryJournalError(
            "E3DC-UPD-JOURNAL-005",
            f"Journalverzeichnis kann nicht sicher geöffnet werden: {exc}",
            solution=_solution_state_directory(path),
            subject=parent,
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        named = os.stat(parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ValueError("Verzeichnis ist nicht rootgebunden oder fremdbeschreibbar")
    except Exception as exc:
        os.close(descriptor)
        raise UpdateRecoveryJournalError(
            "E3DC-UPD-JOURNAL-006",
            f"Journalverzeichnis verletzt den Sicherheitsvertrag: {exc}",
            solution=_solution_state_directory(path),
            subject=parent,
        ) from exc
    return descriptor, name, path


def _read_root_file_at(
    parent_descriptor: int,
    name: str,
    *,
    maximum: int,
    allow_missing: bool = False,
    expected_payload: bytes | None = None,
) -> tuple[bytes, os.stat_result] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    try:
        payload, opened = _validate_root_file(
            descriptor,
            maximum=maximum,
            expected_payload=expected_payload,
        )
        after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if len(
            {
                (before.st_dev, before.st_ino),
                (opened.st_dev, opened.st_ino),
                (after.st_dev, after.st_ino),
            }
        ) != 1:
            raise ValueError("Dateiname und geöffneter Inode drifteten")
        return payload, opened
    finally:
        os.close(descriptor)


def _try_heal_interrupted_create_link(
    parent_descriptor: int,
    name: str,
    path: str,
) -> tuple[bytes, os.stat_result] | None:
    """Heilt nur ``link(final)`` vor ``unlink(staging)`` aus dem Create-Pfad.

    Ein nlink=2 allein genügt ausdrücklich nicht. Der zweite Name muss genau ein
    transaktionseigener Stagingname im selben root:root-0700-Verzeichnis sein,
    auf denselben Inode zeigen und ein vollständiges initiales preproduct-
    Journal enthalten. Andere Hardlinks bleiben unangetastet und fail-closed.
    """

    if name != RECOVERY_JOURNAL_NAME:
        return None
    parent_metadata = os.fstat(parent_descriptor)
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != 0
        or parent_metadata.st_gid != 0
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        return None

    descriptor = None
    payload = None
    metadata = None
    candidate = None
    try:
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(descriptor)
        after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 2
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 0
            or metadata.st_size > MAX_RECOVERY_JOURNAL_BYTES
            or _has_unsafe_xattrs(descriptor)
            or len(
                {
                    (before.st_dev, before.st_ino),
                    (metadata.st_dev, metadata.st_ino),
                    (after.st_dev, after.st_ino),
                }
            )
            != 1
        ):
            return None
        payload = _read_exact(descriptor, int(metadata.st_size))
        try:
            parsed = parse_recovery_journal_payload(payload)
        except UpdateRecoveryJournalError:
            return None
        if (
            parsed.phase != PHASE_PREPRODUCT
            or parsed.package is not None
            or parsed.safety is not None
            or parsed.overlay is not None
        ):
            return None

        matches: list[str] = []
        for entry in os.listdir(parent_descriptor):
            if not _CREATE_STAGING_RE.fullmatch(entry):
                continue
            try:
                candidate_metadata = os.stat(
                    entry,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if (candidate_metadata.st_dev, candidate_metadata.st_ino) == (
                metadata.st_dev,
                metadata.st_ino,
            ):
                matches.append(entry)
        if len(matches) != 1:
            return None
        candidate = matches[0]
        final_rebound = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        candidate_rebound = os.stat(
            candidate,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            (final_rebound.st_dev, final_rebound.st_ino)
            != (metadata.st_dev, metadata.st_ino)
            or (candidate_rebound.st_dev, candidate_rebound.st_ino)
            != (metadata.st_dev, metadata.st_ino)
            or final_rebound.st_nlink != 2
            or candidate_rebound.st_nlink != 2
        ):
            return None
    except (FileNotFoundError, OSError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)

    if payload is None or metadata is None or candidate is None:
        return None
    unlinked = False
    try:
        os.unlink(candidate, dir_fd=parent_descriptor)
        unlinked = True
        os.fsync(parent_descriptor)
        rebound = _read_root_file_at(
            parent_descriptor,
            name,
            maximum=MAX_RECOVERY_JOURNAL_BYTES,
            expected_payload=payload,
        )
        if rebound is None or (
            rebound[1].st_dev,
            rebound[1].st_ino,
        ) != (metadata.st_dev, metadata.st_ino):
            raise ValueError("Geheiltes Create-Journal verlor seinen gebundenen Inode")
        return rebound
    except BaseException as exc:
        persisted = None
        if unlinked:
            try:
                rebound = _read_root_file_at(
                    parent_descriptor,
                    name,
                    maximum=MAX_RECOVERY_JOURNAL_BYTES,
                    expected_payload=payload,
                )
                if rebound is not None and (
                    rebound[1].st_dev,
                    rebound[1].st_ino,
                ) == (metadata.st_dev, metadata.st_ino):
                    persisted = _contract(*rebound, path)
            except Exception:
                persisted = None
        raise UpdateRecoveryJournalPersistenceError(
            "E3DC-UPD-JOURNAL-023",
            f"Create-Crashheilung blieb ohne bestätigten fsync-Abschluss: {exc}",
            solution=_solution_relaunch(),
            journal=persisted,
            subject=path,
        ) from exc


def capture_recovery_receipt_reference(
    kind: str,
    path: str,
) -> RecoveryReceiptReference:
    """Bindet Context/Surface/Systemd/Overlay ohne vom Nutzer gelieferte Guards."""

    _require_root(str(path))
    if kind not in _CAPTURABLE_RECEIPT_KINDS:
        _contract_error("Receipt-Art ist unbekannt", subject=str(path))
    try:
        canonical = _canonical_path(path, label="Receipt-Pfad")
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
        if not directory_flag or not nofollow_flag:
            raise ValueError("O_DIRECTORY/O_NOFOLLOW fehlt")
        parent_descriptor = os.open(
            os.path.dirname(canonical),
            os.O_RDONLY | directory_flag | nofollow_flag | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            parent_metadata = os.fstat(parent_descriptor)
            named_parent = os.stat(
                os.path.dirname(canonical),
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(parent_metadata.st_mode)
                or stat.S_ISLNK(named_parent.st_mode)
                or (parent_metadata.st_dev, parent_metadata.st_ino)
                != (named_parent.st_dev, named_parent.st_ino)
                or parent_metadata.st_uid != 0
                or parent_metadata.st_gid != 0
                or stat.S_IMODE(parent_metadata.st_mode) & 0o022
            ):
                raise ValueError(
                    "Receipt-Elternverzeichnis ist nicht rootgebunden oder "
                    "fremdbeschreibbar"
                )
            readback = _read_root_file_at(
                parent_descriptor,
                os.path.basename(canonical),
                maximum=MAX_COMPANION_RECEIPT_BYTES,
            )
        finally:
            os.close(parent_descriptor)
        if readback is None:
            raise FileNotFoundError(canonical)
        payload, metadata = readback
        return RecoveryReceiptReference(
            kind=kind,
            path=canonical,
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
            size=int(metadata.st_size),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
    except UpdateRecoveryJournalError:
        raise
    except Exception as exc:
        raise UpdateRecoveryJournalError(
            "E3DC-UPD-JOURNAL-007",
            f"Begleit-Receipt kann nicht sicher gebunden werden: {exc}",
            solution=(
                "starte denselben Updatebefehl erneut. Bleibt der Fehler, "
                "prüfe den genannten Pfad mit `sudo ls -l` und verändere "
                "keine Receipt-Datei manuell"
            ),
            subject=str(path),
        ) from exc


def verify_recovery_receipt_reference(
    reference: RecoveryReceiptReference,
) -> RecoveryReceiptReference:
    _require_root(getattr(reference, "path", RECOVERY_JOURNAL_PATH))
    try:
        if not isinstance(reference, RecoveryReceiptReference):
            raise ValueError("Receipt-Referenz besitzt den falschen Typ")
        expected = _validate_receipt(reference, expected_kind=reference.kind)
        current = capture_recovery_receipt_reference(expected.kind, expected.path)
        if current != expected:
            raise ValueError("Receipt-Bytes oder Inode drifteten")
        return current
    except UpdateRecoveryJournalError:
        raise
    except Exception as exc:
        raise UpdateRecoveryJournalError(
            "E3DC-UPD-JOURNAL-008",
            f"Begleit-Receipt driftete: {exc}",
            solution=_solution_relaunch(),
            subject=getattr(reference, "path", RECOVERY_JOURNAL_PATH),
        ) from exc


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Datei blieb unvollständig")
        view = view[written:]


def _stage(parent_descriptor: int, payload: bytes) -> tuple[int, str, os.stat_result]:
    name = f".e3dc-recovery-journal-{os.getpid()}-{secrets.token_hex(12)}"
    descriptor = os.open(
        name,
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=parent_descriptor,
    )
    try:
        _write_all(descriptor, payload)
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        rebound, metadata = _validate_root_file(
            descriptor,
            maximum=MAX_RECOVERY_JOURNAL_BYTES,
            expected_payload=payload,
        )
        if rebound != payload:
            raise ValueError("Gestagtes Journal driftete")
        return descriptor, name, metadata
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        raise


def _contract(
    payload: bytes,
    metadata: os.stat_result,
    path: str,
) -> RecoveryJournalContract:
    return RecoveryJournalContract(
        payload=parse_recovery_journal_payload(payload),
        journal_path=path,
        journal_device=int(metadata.st_dev),
        journal_inode=int(metadata.st_ino),
        journal_size=int(metadata.st_size),
        journal_sha256=hashlib.sha256(payload).hexdigest(),
    )


def read_recovery_journal(
    journal_path: str = RECOVERY_JOURNAL_PATH,
    *,
    allow_missing: bool = False,
) -> RecoveryJournalContract | None:
    parent, name, path = _open_parent(journal_path)
    try:
        try:
            try:
                readback = _read_root_file_at(
                    parent,
                    name,
                    maximum=MAX_RECOVERY_JOURNAL_BYTES,
                    allow_missing=allow_missing,
                )
            except ValueError:
                readback = _try_heal_interrupted_create_link(parent, name, path)
                if readback is None:
                    raise
        except FileNotFoundError as exc:
            raise UpdateRecoveryJournalError(
                "E3DC-UPD-JOURNAL-009",
                "Master-Journal fehlt",
                solution=_solution_relaunch(),
                subject=path,
            ) from exc
        if readback is None:
            return None
        return _contract(*readback, path)
    except UpdateRecoveryJournalError:
        raise
    except Exception as exc:
        raise UpdateRecoveryJournalError(
            "E3DC-UPD-JOURNAL-010",
            f"Master-Journal kann nicht sicher gelesen werden: {exc}",
            solution=_solution_relaunch(),
            subject=path,
        ) from exc
    finally:
        os.close(parent)


def create_recovery_journal(
    payload: RecoveryJournalPayload,
    journal_path: str = RECOVERY_JOURNAL_PATH,
) -> RecoveryJournalContract:
    try:
        validated = _validate_payload(payload)
    except UpdateRecoveryJournalError:
        raise
    except Exception as exc:
        _contract_error(f"Master-Journal-Payload ist ungültig: {exc}")
    if (
        validated.phase != PHASE_PREPRODUCT
        or validated.package is not None
        or validated.safety is not None
        or validated.overlay is not None
    ):
        _contract_error("Ein neues Journal muss vor Bootblock und Paketreceipt beginnen")
    serialized = serialize_recovery_journal_payload(validated)
    parent, name, path = _open_parent(journal_path)
    descriptor = None
    temporary = None
    linked = False
    staged = None
    try:
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise UpdateRecoveryJournalError(
                "E3DC-UPD-JOURNAL-011",
                "Ein Master-Journal ist bereits vorhanden",
                solution=_solution_relaunch(),
                subject=path,
            )
        descriptor, temporary, staged = _stage(parent, serialized)
        os.link(
            temporary,
            name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
            follow_symlinks=False,
        )
        linked = True
        os.unlink(temporary, dir_fd=parent)
        temporary = None
        os.fsync(parent)
        readback = _read_root_file_at(
            parent,
            name,
            maximum=MAX_RECOVERY_JOURNAL_BYTES,
            expected_payload=serialized,
        )
        if readback is None or (readback[1].st_dev, readback[1].st_ino) != (
            staged.st_dev,
            staged.st_ino,
        ):
            raise ValueError("Master-Journal driftete nach atomarem create")
        return _contract(*readback, path)
    except UpdateRecoveryJournalError:
        raise
    except BaseException as exc:
        persisted = None
        if linked:
            try:
                rebound = _read_root_file_at(
                    parent,
                    name,
                    maximum=MAX_RECOVERY_JOURNAL_BYTES,
                    expected_payload=serialized,
                )
                if rebound is not None and staged is not None and (
                    rebound[1].st_dev,
                    rebound[1].st_ino,
                ) == (staged.st_dev, staged.st_ino):
                    persisted = _contract(*rebound, path)
            except Exception:
                persisted = None
        raise UpdateRecoveryJournalPersistenceError(
            "E3DC-UPD-JOURNAL-012",
            f"Atomare Journalerzeugung blieb ohne bestätigten Abschluss: {exc}",
            solution=_solution_relaunch(),
            journal=persisted,
            subject=path,
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def verify_recovery_journal(contract: RecoveryJournalContract) -> RecoveryJournalContract:
    if not isinstance(contract, RecoveryJournalContract):
        _contract_error("Master-Journal-Vertrag besitzt den falschen Typ")
    current = read_recovery_journal(contract.journal_path)
    if current != contract:
        raise UpdateRecoveryJournalError(
            "E3DC-UPD-JOURNAL-013",
            "Master-Journal-Bytes, Phase oder Inode drifteten",
            solution=_solution_relaunch(),
            subject=contract.journal_path,
        )
    return current


def _replace_exact(
    current: RecoveryJournalContract,
    next_payload: RecoveryJournalPayload,
) -> RecoveryJournalContract:
    serialized = serialize_recovery_journal_payload(next_payload)
    parent, name, path = _open_parent(current.journal_path)
    descriptor = None
    temporary = None
    replaced_name = False
    staged = None
    try:
        rebound = _read_root_file_at(parent, name, maximum=MAX_RECOVERY_JOURNAL_BYTES)
        if rebound is None or _contract(*rebound, path) != current:
            raise ValueError("Master-Journal driftete vor dem Ersatz")
        descriptor, temporary, staged = _stage(parent, serialized)
        os.replace(
            temporary,
            name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        replaced_name = True
        temporary = None
        os.fsync(parent)
        readback = _read_root_file_at(
            parent,
            name,
            maximum=MAX_RECOVERY_JOURNAL_BYTES,
            expected_payload=serialized,
        )
        if readback is None or (readback[1].st_dev, readback[1].st_ino) != (
            staged.st_dev,
            staged.st_ino,
        ):
            raise ValueError("Master-Journal driftete nach dem Ersatz")
        return _contract(*readback, path)
    except BaseException as exc:
        persisted = None
        try:
            rebound = _read_root_file_at(parent, name, maximum=MAX_RECOVERY_JOURNAL_BYTES)
            if rebound is not None:
                candidate = _contract(*rebound, path)
                if candidate == current or (
                    candidate.payload == next_payload
                    and staged is not None
                    and (candidate.journal_device, candidate.journal_inode)
                    == (staged.st_dev, staged.st_ino)
                ):
                    persisted = candidate
        except Exception:
            persisted = None
        raise UpdateRecoveryJournalPersistenceError(
            "E3DC-UPD-JOURNAL-014",
            f"Journalersatz blieb ohne bestätigten Abschluss: {exc}",
            solution=_solution_relaunch(),
            journal=persisted,
            subject=path,
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None and not replaced_name:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def bind_preproduct_recovery_receipts(
    contract: RecoveryJournalContract,
    *,
    package: RecoveryPackageBinding,
    safety: RecoverySafetyBinding,
) -> RecoveryJournalContract:
    """Einmaliger SAME-PHASE-Rebind nach Bootblock und applying-Receipt."""

    current = verify_recovery_journal(contract)
    if (
        current.payload.phase != PHASE_PREPRODUCT
        or current.payload.package is not None
        or current.payload.safety is not None
        or current.payload.overlay is not None
    ):
        raise UpdateRecoveryJournalError(
            "E3DC-UPD-JOURNAL-015",
            "preproduct-Semantik darf nur einmal von null auf gebunden wechseln",
            solution=_solution_relaunch(),
            subject=current.journal_path,
        )
    try:
        next_payload = _rehash_payload(
            replace(current.payload, package=package, safety=safety)
        )
    except UpdateRecoveryJournalError:
        raise
    except Exception as exc:
        raise UpdateRecoveryJournalError(
            "E3DC-UPD-JOURNAL-020",
            f"Package-/Safety-Semantik widerspricht der Transaktion: {exc}",
            solution=_solution_relaunch(),
            subject=current.journal_path,
        ) from exc
    if (
        next_payload.phase != current.payload.phase
        or _immutable_mapping(next_payload) != _immutable_mapping(current.payload)
        or next_payload.overlay is not None
    ):
        _contract_error("preproduct-Rebind veränderte mehr als Package/Safety")
    return _replace_exact(current, next_payload)


def advance_recovery_journal(
    contract: RecoveryJournalContract,
    target_phase: str,
    *,
    overlay: RecoveryOverlayBinding | None = None,
) -> RecoveryJournalContract:
    """Erlaubt nur linearen Commit oder terminalen, zustandstreuen Rücklauf."""

    current = verify_recovery_journal(contract)
    if current.payload.phase == PHASE_PREPRODUCT:
        if target_phase == PHASE_ROLLED_BACK and overlay is None:
            try:
                next_payload = _rehash_payload(
                    replace(current.payload, phase=PHASE_ROLLED_BACK)
                )
            except UpdateRecoveryJournalError:
                raise
            except Exception as exc:
                raise UpdateRecoveryJournalError(
                    "E3DC-UPD-JOURNAL-024",
                    f"Früher Rücklauf ist nicht selbstkonsistent: {exc}",
                    solution=_solution_relaunch(),
                    subject=current.journal_path,
                ) from exc
        elif (
            target_phase == PHASE_PRODUCT_MUTATING
            and current.payload.package is not None
            and current.payload.safety is not None
            and current.payload.overlay is None
            and overlay is not None
        ):
            try:
                next_payload = _rehash_payload(
                    replace(
                        current.payload,
                        phase=PHASE_PRODUCT_MUTATING,
                        overlay=overlay,
                    )
                )
            except UpdateRecoveryJournalError:
                raise
            except Exception as exc:
                raise UpdateRecoveryJournalError(
                    "E3DC-UPD-JOURNAL-021",
                    f"Overlay widerspricht der gebundenen Transaktion: {exc}",
                    solution=_solution_relaunch(),
                    subject=current.journal_path,
                ) from exc
        else:
            raise UpdateRecoveryJournalError(
                "E3DC-UPD-JOURNAL-016",
                "preproduct erlaubt nur product_mutating mit vollständiger "
                "Bindung oder rolled_back ohne neues Overlay",
                solution=_solution_relaunch(),
                subject=current.journal_path,
            )
    elif current.payload.phase == PHASE_PRODUCT_MUTATING:
        if target_phase not in {PHASE_COMMITTED, PHASE_ROLLED_BACK} or overlay is not None:
            raise UpdateRecoveryJournalError(
                "E3DC-UPD-JOURNAL-017",
                "product_mutating darf nur committed oder rolled_back werden; "
                "beide ändern ausschließlich die Journalphase",
                solution=_solution_relaunch(),
                subject=current.journal_path,
            )
        try:
            next_payload = _rehash_payload(
                replace(current.payload, phase=target_phase)
            )
        except UpdateRecoveryJournalError:
            raise
        except Exception as exc:
            raise UpdateRecoveryJournalError(
                "E3DC-UPD-JOURNAL-022",
                f"Terminale Phase ist nicht selbstkonsistent: {exc}",
                solution=_solution_relaunch(),
                subject=current.journal_path,
            ) from exc
        if _phase_mapping(next_payload) != _phase_mapping(current.payload):
            _contract_error(
                "Terminale Phase veränderte Package-, Safety- oder Overlay-Bindung"
            )
    else:
        raise UpdateRecoveryJournalError(
            "E3DC-UPD-JOURNAL-018",
            f"Ein {current.payload.phase} Journal besitzt keine weitere Phase",
            solution=_solution_relaunch(),
            subject=current.journal_path,
        )
    if (
        _immutable_mapping(next_payload) != _immutable_mapping(current.payload)
        or next_payload.binding_sha256 != current.payload.binding_sha256
    ):
        _contract_error("Phasenwechsel veränderte die unveränderliche Bindung")
    if target_phase == PHASE_ROLLED_BACK and (
        _phase_mapping(next_payload) != _phase_mapping(current.payload)
        or next_payload.phase_state_sha256 != current.payload.phase_state_sha256
    ):
        _contract_error("rolled_back veränderte den zuvor gebundenen Phasenzustand")
    return _replace_exact(current, next_payload)


def remove_recovery_journal(contract: RecoveryJournalContract) -> None:
    """Entfernt nur den bereits gebundenen Inode mit exakt gebundenem Hash."""

    current = verify_recovery_journal(contract)
    parent, name, path = _open_parent(current.journal_path)
    unlinked = False
    try:
        rebound = _read_root_file_at(parent, name, maximum=MAX_RECOVERY_JOURNAL_BYTES)
        if rebound is None:
            raise ValueError("Master-Journal fehlt vor dem Entfernen")
        payload, metadata = rebound
        if (
            (metadata.st_dev, metadata.st_ino)
            != (current.journal_device, current.journal_inode)
            or len(payload) != current.journal_size
            or hashlib.sha256(payload).hexdigest() != current.journal_sha256
        ):
            raise ValueError("Master-Journal verlor Inode- oder Hashbindung")
        os.unlink(name, dir_fd=parent)
        unlinked = True
        os.fsync(parent)
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise ValueError("Master-Journal blieb nach unlink vorhanden")
    except BaseException as exc:
        raise UpdateRecoveryJournalPersistenceError(
            "E3DC-UPD-JOURNAL-019",
            f"Gebundenes Journal konnte nicht eindeutig entfernt werden: {exc}",
            solution=_solution_relaunch(),
            journal=None if unlinked else current,
            subject=path,
        ) from exc
    finally:
        os.close(parent)


__all__ = [
    "GATE_MODE_DYNAMIC",
    "GATE_MODE_STATIC",
    "MAX_COMPANION_RECEIPT_BYTES",
    "MAX_RECOVERY_JOURNAL_BYTES",
    "PHASE_COMMITTED",
    "PHASE_PREPRODUCT",
    "PHASE_PRODUCT_MUTATING",
    "PHASE_ROLLED_BACK",
    "RECOVERY_JOURNAL_NAME",
    "RECOVERY_JOURNAL_PATH",
    "RECOVERY_JOURNAL_PHASES",
    "RecoveryFullBackupBinding",
    "RecoveryImmutableReceiptReferences",
    "RecoveryJournalContract",
    "RecoveryJournalPayload",
    "RecoveryOverlayBinding",
    "RecoveryPackageBinding",
    "RecoveryReceiptReference",
    "RecoverySafetyBinding",
    "RecoverySourceBinding",
    "RecoveryTargetBinding",
    "UpdateRecoveryJournalError",
    "UpdateRecoveryJournalPersistenceError",
    "advance_recovery_journal",
    "bind_preproduct_recovery_receipts",
    "capture_recovery_receipt_reference",
    "create_recovery_journal",
    "make_dynamic_safety_binding",
    "make_full_backup_binding",
    "make_immutable_receipt_references",
    "make_overlay_binding",
    "make_package_binding",
    "make_recovery_journal_payload",
    "make_source_binding",
    "make_static_safety_binding",
    "make_target_binding",
    "parse_recovery_journal_payload",
    "read_recovery_journal",
    "remove_recovery_journal",
    "serialize_recovery_journal_payload",
    "verify_recovery_journal",
    "verify_recovery_receipt_reference",
]
