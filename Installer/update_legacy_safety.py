#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reiner Decoder für veröffentlichte Update-Safety-Receipts.

Das Modul bildet ausschließlich die in ``v5.4.4`` und ``v5.4.4a``
veröffentlichte ``e3dc_update_safety_v1``-Semantik ab. Es importiert keine
Installer-, Apache- oder systemd-Komponente und führt keinerlei Dateisystem-
oder Prozessmutation aus. Dadurch kann ein neuer Bootstrap einen liegen
gebliebenen Altvertrag lesen, bevor der restliche Altstand vertrauenswürdig
importierbar ist.

Wichtig: Die Unit-Reihenfolge, Finalizer-Namen und Drop-in-Bytes sind bewusst
eingefroren. Der aktuelle Dienstkatalog darf einen veröffentlichten
Altvertrag nicht nachträglich umdeuten.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, NoReturn


LEGACY_UPDATE_SAFETY_SCHEMA = "e3dc_update_safety_v1"
LEGACY_UPDATE_SAFETY_RECEIPT_PATH = (
    "/var/lib/e3dc-update-safety/transaction.json"
)
LEGACY_UPDATE_SAFETY_MARKER_PATH = (
    "/var/lib/e3dc-update-safety/recovery.block"
)
LEGACY_UPDATE_SAFETY_DROPIN_NAME = "00-e3dc-recovery-bootblock.conf"
LEGACY_UPDATE_SAFETY_MAX_BYTES = 256 * 1024

LEGACY_UPDATE_SAFETY_TARGETS: Mapping[str, str] = MappingProxyType(
    {
        "v5.4.4": "0bb993ef9111ba4a4375fdafb80711a7af061300",
        "v5.4.4a": "7d3635b66ad090dd6e20793330aaf60a5f61cfe7",
    }
)

# Diese vier Dateien liegen im verifizierten Vollbackup des alten Parents.
# Ihre gemeinsamen SHA-256-Werte identifizieren den tatsächlich laufenden
# veröffentlichten Writer, ohne einem frei gesetzten Versionsstring oder dem
# bereits auf den Zielstand gewechselten Produkt-HEAD zu vertrauen.
LEGACY_FORWARD_SOURCE_BLOBS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "v5.4.4": MappingProxyType(
            {
                "VERSION": "51e62c6f47653bdc2cb33ffba0ece9a0150d6d7edf124261c89cf82d049f970d",
                "Installer/update.py": "f1bebc2de42b921c03597bea9456c3ef351c7302c8d122e985fe838886e76cad",
                "Installer/release_finalize.py": "62c18cfe3083649fdac31eeaab81ea966e90e5d87054b38b7d4a11c075fedaf7",
                "UPDATE_POLICY.json": "4e6961ab2d0982cbbe0b37df28f4bd896da63d34dc561f32894967dde383503e",
            }
        ),
        "v5.4.4a": MappingProxyType(
            {
                "VERSION": "c72ec2431e1932b5d2f84ed7de7978f3c85dbf537e790b152cb181f8597dcb40",
                "Installer/update.py": "d60c6189dc6d216c9ba5b743da2cb9ee53fa3c0b4e0a5e67a6c25fdc2b13ea2a",
                "Installer/release_finalize.py": "62c18cfe3083649fdac31eeaab81ea966e90e5d87054b38b7d4a11c075fedaf7",
                "UPDATE_POLICY.json": "43c45036de72a44e0b193c6a9604512c1c5159b1d135e9370f7df4bca40bb589",
            }
        ),
    }
)

# Exakte Reihenfolge aus service_catalog.py der beiden veröffentlichten Tags:
# _catalog_service_names(include_legacy=True) plus piguard.service.
LEGACY_UPDATE_SAFETY_UNITS = (
    "e3dc.service",
    "e3dc-live.service",
    "e3dc-epex-manager.service",
    "e3dc-weather-manager.service",
    "e3dc-forecast-evidence.service",
    "e3dc-storage-simulator.service",
    "e3dc-storage-manager.service",
    "e3dc-websocket.service",
    "e3dc-wallbox-manager.service",
    "energy_manager.service",
    "e3dc-lux-live.service",
    "e3dc-idm-live.service",
    "e3dc-stiebel-live.service",
    "e3dc-dimplex-live.service",
    "e3dc-heizstab.service",
    "e3dc-climate-live.service",
    "e3dc-climate-control.service",
    "e3dc-ha.service",
    "e3dc-shadow-sync.service",
    "e3dc-matter-bridge.service",
    "e3dc-bluelink.service",
    "e3dc-notifier.service",
    "e3dc-mqtt-hub.service",
    "piguard.service",
)

_VALID_STATES = frozenset(("pending", "committed"))
_VALID_ROLES = frozenset(("off", "master", "slave", "shadow"))
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

_OUTER_KEYS = frozenset(
    ("schema", "state", "transaction_id", "target", "backup", "bootblock", "finalizer")
)
_TARGET_KEYS = frozenset(("commit", "tag", "role"))
_BACKUP_KEYS = frozenset(("dir", "dev", "ino", "id", "manifest_sha256"))
_BOOTBLOCK_KEYS = frozenset(
    ("units", "created_directories", "dropin_payload_sha256", "dropin_identities")
)
_FINALIZER_KEYS = frozenset(("unit", "runtime_directory", "token_path"))

_FORMAT_SOLUTION = (
    "Receipt nicht bearbeiten oder neu erzeugen. Die Originaldatei unverändert "
    "als Diagnose sichern und das Update mit dem aktuellen Updater erneut starten."
)
_TARGET_SOLUTION = (
    "Nur ein unverändertes Receipt von v5.4.4 oder v5.4.4a übernehmen. Bei "
    "einem anderen Ursprung den dazugehörigen veröffentlichten Updater verwenden."
)
_CONTRACT_SOLUTION = (
    "Update nicht fortsetzen und den Safety-Vertrag nicht manuell entfernen. "
    "Receipt, Updateprotokoll und betroffene Drop-ins für die geführte Recovery sichern."
)


class LegacyUpdateSafetyError(ValueError):
    """Basistyp mit maschinenlesbarem Fehlercode und konkreter Lösung."""

    def __init__(self, code: str, detail: str, solution: str):
        self.code = str(code)
        self.detail = str(detail)
        self.solution = str(solution)
        super().__init__(
            f"[{self.code}] {self.detail} Lösung: {self.solution}"
        )


class LegacyUpdateSafetyFormatError(LegacyUpdateSafetyError):
    """Die Receipt-Bytes sind nicht exakt im veröffentlichten Format."""


class LegacyUpdateSafetyTargetError(LegacyUpdateSafetyError):
    """Tag und Commit bilden kein veröffentlichtes, unterstütztes Paar."""


class LegacyUpdateSafetyContractError(LegacyUpdateSafetyError):
    """Das Receipt widerspricht der eingefrorenen Legacy-Semantik."""


@dataclass(frozen=True, slots=True)
class LegacyUpdateSafetyReceipt:
    """Vollständig validierter, unveränderlicher Legacy-Safety-Vertrag."""

    schema: str
    state: str
    transaction_id: str
    target_commit: str
    target_tag: str
    role: str
    backup_dir: str
    backup_dev: int
    backup_ino: int
    backup_id: str
    backup_manifest_sha256: str
    units: tuple[str, ...]
    created_directories: tuple[str, ...]
    dropin_identities: tuple[tuple[str, int, int], ...]
    dropin_payload_sha256: str
    finalizer_unit: str
    runtime_directory: str
    token_path: str
    receipt_path: str
    receipt_sha256: str

    @property
    def target_identity(self) -> tuple[str, str]:
        """Liefert die gebundene Identität als ``(Tag, Commit)``."""

        return self.target_tag, self.target_commit

    @property
    def dropin_payload(self) -> bytes:
        """Liefert die eingefrorenen Drop-in-Bytes dieser Transaktion."""

        return render_legacy_update_safety_dropin(self.transaction_id)


def _raise_format(code: str, detail: str) -> NoReturn:
    raise LegacyUpdateSafetyFormatError(code, detail, _FORMAT_SOLUTION)


def _raise_target(code: str, detail: str) -> NoReturn:
    raise LegacyUpdateSafetyTargetError(code, detail, _TARGET_SOLUTION)


def _raise_contract(code: str, detail: str) -> NoReturn:
    raise LegacyUpdateSafetyContractError(code, detail, _CONTRACT_SOLUTION)


def _canonical_receipt_bytes(record: object) -> bytes:
    try:
        encoded = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        _raise_format(
            "LEGACY_SAFETY_NOT_CANONICAL",
            f"Receipt lässt sich nicht kanonisch serialisieren: {exc}",
        )
    return (encoded + "\n").encode("ascii")


def _require_mapping(
    value: object,
    *,
    keys: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _raise_format(
            "LEGACY_SAFETY_KEYSET_MISMATCH",
            f"{label} besitzt nicht den veröffentlichten, exakten Schlüsselsatz.",
        )
    return value


def _require_string(value: object, *, label: str) -> str:
    if type(value) is not str:
        _raise_format(
            "LEGACY_SAFETY_TYPE_MISMATCH",
            f"{label} muss eine Zeichenkette sein.",
        )
    return value


def _require_integer(value: object, *, label: str) -> int:
    if type(value) is not int:
        _raise_format(
            "LEGACY_SAFETY_TYPE_MISMATCH",
            f"{label} muss eine Ganzzahl sein.",
        )
    return value


def legacy_update_safety_names(transaction_id: str) -> tuple[str, str, str]:
    """Leitet die in v5.4.4/v5.4.4a veröffentlichten Namen ab."""

    if type(transaction_id) is not str or not _SHA256_RE.fullmatch(transaction_id):
        _raise_contract(
            "LEGACY_SAFETY_TRANSACTION_INVALID",
            "Transaktions-ID ist kein kleingeschriebener SHA-256-Wert.",
        )
    unit = f"e3dc-update-finalizer-{transaction_id}.service"
    runtime = f"e3dc-update-finalizer-{transaction_id}-runtime"
    token = f"/run/{runtime}/start.token"
    return unit, runtime, token


def render_legacy_update_safety_dropin(transaction_id: str) -> bytes:
    """Erzeugt bytegenau das veröffentlichte dynamische Startgate."""

    unit, _runtime, token = legacy_update_safety_names(transaction_id)
    return (
        "# E3DC_UPDATE_SAFETY_V1\n"
        "[Unit]\n"
        f"BindsTo={unit}\n"
        f"After={unit}\n"
        f"ConditionPathExists=|!{LEGACY_UPDATE_SAFETY_MARKER_PATH}\n"
        f"ConditionPathExists=|{token}\n"
    ).encode("utf-8")


def _parse_string_list(value: object, *, label: str) -> tuple[str, ...]:
    if type(value) is not list:
        _raise_format(
            "LEGACY_SAFETY_TYPE_MISMATCH",
            f"{label} muss eine Liste sein.",
        )
    parsed: list[str] = []
    for index, item in enumerate(value):
        parsed.append(_require_string(item, label=f"{label}[{index}]"))
    return tuple(parsed)


def _parse_dropin_identities(
    value: object,
) -> tuple[tuple[str, int, int], ...]:
    if type(value) is not list:
        _raise_format(
            "LEGACY_SAFETY_TYPE_MISMATCH",
            "bootblock.dropin_identities muss eine Liste sein.",
        )
    parsed: list[tuple[str, int, int]] = []
    for index, item in enumerate(value):
        if type(item) is not list or len(item) != 3:
            _raise_format(
                "LEGACY_SAFETY_TYPE_MISMATCH",
                f"bootblock.dropin_identities[{index}] muss [Unit, Dev, Ino] enthalten.",
            )
        unit = _require_string(item[0], label=f"dropin_identities[{index}][0]")
        device = _require_integer(item[1], label=f"dropin_identities[{index}][1]")
        inode = _require_integer(item[2], label=f"dropin_identities[{index}][2]")
        parsed.append((unit, device, inode))
    return tuple(parsed)


def _validate_backup_id(value: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        _raise_contract(
            "LEGACY_SAFETY_BACKUP_ID_INVALID",
            "backup.id ist keine kanonische UUID.",
        )
    if (
        str(parsed) != value
        or parsed.version != 4
        or parsed.variant != uuid.RFC_4122
    ):
        _raise_contract(
            "LEGACY_SAFETY_BACKUP_ID_INVALID",
            "backup.id ist keine kanonische, veröffentlichte UUIDv4.",
        )


def _parse_legacy_update_safety_receipt(
    payload: bytes,
    *,
    expected_target_commit: str | None,
    expected_target_tag: str | None,
) -> LegacyUpdateSafetyReceipt:
    """Validiert ausschließlich unveränderte v5.4.4/v5.4.4a-Receipt-Bytes.

    Die Funktion liest keine Datei. Der Aufrufer muss die Bytes selbst mit
    seiner aktuellen inode-/rechtegebundenen Leselogik beschaffen. Bei Erfolg
    ist jedes Feld typisiert, die Zielidentität fest gebunden und die
    veröffentlichte Unit-/Finalizer-/Drop-in-Semantik bewiesen.
    """

    if type(payload) is not bytes:
        _raise_format(
            "LEGACY_SAFETY_BYTES_REQUIRED",
            "Receipt muss als unverändertes bytes-Objekt übergeben werden.",
        )
    if not payload or len(payload) > LEGACY_UPDATE_SAFETY_MAX_BYTES:
        _raise_format(
            "LEGACY_SAFETY_SIZE_INVALID",
            "Receipt ist leer oder größer als die veröffentlichte 256-KiB-Grenze.",
        )
    try:
        decoded = payload.decode("ascii")
    except UnicodeDecodeError:
        _raise_format(
            "LEGACY_SAFETY_ASCII_REQUIRED",
            "Receipt enthält Bytes außerhalb des veröffentlichten ASCII-Formats.",
        )
    try:
        record = json.loads(decoded)
    except json.JSONDecodeError as exc:
        _raise_format(
            "LEGACY_SAFETY_JSON_INVALID",
            f"Receipt enthält kein gültiges JSON: {exc.msg}.",
        )
    if _canonical_receipt_bytes(record) != payload:
        _raise_format(
            "LEGACY_SAFETY_NOT_CANONICAL",
            "Sortierung, Trennzeichen oder genau ein abschließender LF-Zeilenumbruch weichen ab.",
        )

    outer = _require_mapping(record, keys=_OUTER_KEYS, label="Receipt")
    target = _require_mapping(outer["target"], keys=_TARGET_KEYS, label="target")
    backup = _require_mapping(outer["backup"], keys=_BACKUP_KEYS, label="backup")
    bootblock = _require_mapping(
        outer["bootblock"], keys=_BOOTBLOCK_KEYS, label="bootblock"
    )
    finalizer = _require_mapping(
        outer["finalizer"], keys=_FINALIZER_KEYS, label="finalizer"
    )

    schema = _require_string(outer["schema"], label="schema")
    state = _require_string(outer["state"], label="state")
    transaction_id = _require_string(
        outer["transaction_id"], label="transaction_id"
    )
    if schema != LEGACY_UPDATE_SAFETY_SCHEMA:
        _raise_contract(
            "LEGACY_SAFETY_SCHEMA_UNSUPPORTED",
            f"Schema {schema!r} ist kein veröffentlichter Legacy-Safety-Vertrag.",
        )
    if state not in _VALID_STATES:
        _raise_contract(
            "LEGACY_SAFETY_STATE_INVALID",
            f"Safety-Zustand {state!r} ist weder pending noch committed.",
        )

    expected_unit, expected_runtime, expected_token = legacy_update_safety_names(
        transaction_id
    )

    target_commit = _require_string(target["commit"], label="target.commit")
    target_tag = _require_string(target["tag"], label="target.tag")
    role = _require_string(target["role"], label="target.role")
    if expected_target_commit is None and expected_target_tag is None:
        expected_commit = LEGACY_UPDATE_SAFETY_TARGETS.get(target_tag)
        if expected_commit is None or target_commit != expected_commit:
            _raise_target(
                "LEGACY_SAFETY_TARGET_PAIR_UNSUPPORTED",
                f"Zielpaar {target_tag!r}/{target_commit!r} ist nicht veröffentlicht gebunden.",
            )
    else:
        if (
            type(expected_target_commit) is not str
            or type(expected_target_tag) is not str
            or not re.fullmatch(r"[0-9a-f]{40}", expected_target_commit)
            or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+[a-z]?", expected_target_tag)
            or target_commit != expected_target_commit
            or target_tag != expected_target_tag
        ):
            _raise_target(
                "LEGACY_FORWARD_TARGET_MISMATCH",
                "Aktives Legacy-Receipt widerspricht dem commitgebundenen Ziel-Snapshot.",
            )
    if role not in _VALID_ROLES:
        _raise_contract(
            "LEGACY_SAFETY_ROLE_INVALID",
            f"HA-Rolle {role!r} gehört nicht zum veröffentlichten Rollenvertrag.",
        )

    backup_dir = _require_string(backup["dir"], label="backup.dir")
    backup_dev = _require_integer(backup["dev"], label="backup.dev")
    backup_ino = _require_integer(backup["ino"], label="backup.ino")
    backup_id = _require_string(backup["id"], label="backup.id")
    backup_manifest_sha256 = _require_string(
        backup["manifest_sha256"], label="backup.manifest_sha256"
    )
    if (
        not posixpath.isabs(backup_dir)
        or backup_dir == "/"
        or "\x00" in backup_dir
        or posixpath.normpath(backup_dir) != backup_dir
        or backup_dev < 0
        or backup_ino <= 0
        or not _SHA256_RE.fullmatch(backup_manifest_sha256)
    ):
        _raise_contract(
            "LEGACY_SAFETY_BACKUP_BINDING_INVALID",
            "Backup-Pfad, Device, Inode oder Manifest-Hash ist nicht eindeutig gebunden.",
        )
    _validate_backup_id(backup_id)

    units = _parse_string_list(bootblock["units"], label="bootblock.units")
    created_directories = _parse_string_list(
        bootblock["created_directories"], label="bootblock.created_directories"
    )
    dropin_identities = _parse_dropin_identities(
        bootblock["dropin_identities"]
    )
    dropin_payload_sha256 = _require_string(
        bootblock["dropin_payload_sha256"],
        label="bootblock.dropin_payload_sha256",
    )
    expected_dropin_hash = hashlib.sha256(
        render_legacy_update_safety_dropin(transaction_id)
    ).hexdigest()
    expected_created_order = tuple(
        unit for unit in LEGACY_UPDATE_SAFETY_UNITS if unit in created_directories
    )
    if (
        units != LEGACY_UPDATE_SAFETY_UNITS
        or len(set(created_directories)) != len(created_directories)
        or expected_created_order != created_directories
        or tuple(item[0] for item in dropin_identities)
        != LEGACY_UPDATE_SAFETY_UNITS
        or any(device < 0 or inode <= 0 for _, device, inode in dropin_identities)
        or dropin_payload_sha256 != expected_dropin_hash
    ):
        _raise_contract(
            "LEGACY_SAFETY_BOOTBLOCK_DRIFT",
            "Unit-Reihenfolge, erzeugte Verzeichnisse, Inodes oder Drop-in-Hash sind gedriftet.",
        )

    finalizer_unit = _require_string(finalizer["unit"], label="finalizer.unit")
    runtime_directory = _require_string(
        finalizer["runtime_directory"], label="finalizer.runtime_directory"
    )
    token_path = _require_string(
        finalizer["token_path"], label="finalizer.token_path"
    )
    if (
        finalizer_unit != expected_unit
        or runtime_directory != expected_runtime
        or token_path != expected_token
    ):
        _raise_contract(
            "LEGACY_SAFETY_FINALIZER_DRIFT",
            "Finalizer-Unit, RuntimeDirectory oder Starttoken passen nicht zur Transaktion.",
        )

    return LegacyUpdateSafetyReceipt(
        schema=schema,
        state=state,
        transaction_id=transaction_id,
        target_commit=target_commit,
        target_tag=target_tag,
        role=role,
        backup_dir=backup_dir,
        backup_dev=backup_dev,
        backup_ino=backup_ino,
        backup_id=backup_id,
        backup_manifest_sha256=backup_manifest_sha256,
        units=units,
        created_directories=created_directories,
        dropin_identities=dropin_identities,
        dropin_payload_sha256=dropin_payload_sha256,
        finalizer_unit=finalizer_unit,
        runtime_directory=runtime_directory,
        token_path=token_path,
        receipt_path=LEGACY_UPDATE_SAFETY_RECEIPT_PATH,
        receipt_sha256=hashlib.sha256(payload).hexdigest(),
    )


def parse_legacy_update_safety_receipt(
    payload: bytes,
) -> LegacyUpdateSafetyReceipt:
    """Liest ausschließlich ein zurückgelassenes v5.4.4/v5.4.4a-Receipt."""

    return _parse_legacy_update_safety_receipt(
        payload,
        expected_target_commit=None,
        expected_target_tag=None,
    )


def parse_active_legacy_forward_receipt(
    payload: bytes,
    *,
    expected_target_commit: str,
    expected_target_tag: str,
) -> LegacyUpdateSafetyReceipt:
    """Bindet ein laufendes v1-Receipt an den bereits geladenen Ziel-Snapshot.

    Anders als die Residuum-Lesefunktion erlaubt dieser Pfad kein beliebiges
    altes Ziel und auch keine Ziel-Ableitung aus dem Receipt. Commit und Tag
    stammen zwingend aus dem bereits commitgebundenen Wrapper-/Snapshotvertrag.
    Die Identität des alten Writers wird davon getrennt über die vier
    Vollbackup-Blobs aus :data:`LEGACY_FORWARD_SOURCE_BLOBS` bewiesen.
    """

    return _parse_legacy_update_safety_receipt(
        payload,
        expected_target_commit=expected_target_commit,
        expected_target_tag=expected_target_tag,
    )


__all__ = (
    "LEGACY_UPDATE_SAFETY_DROPIN_NAME",
    "LEGACY_FORWARD_SOURCE_BLOBS",
    "LEGACY_UPDATE_SAFETY_MARKER_PATH",
    "LEGACY_UPDATE_SAFETY_MAX_BYTES",
    "LEGACY_UPDATE_SAFETY_RECEIPT_PATH",
    "LEGACY_UPDATE_SAFETY_SCHEMA",
    "LEGACY_UPDATE_SAFETY_TARGETS",
    "LEGACY_UPDATE_SAFETY_UNITS",
    "LegacyUpdateSafetyContractError",
    "LegacyUpdateSafetyError",
    "LegacyUpdateSafetyFormatError",
    "LegacyUpdateSafetyReceipt",
    "LegacyUpdateSafetyTargetError",
    "legacy_update_safety_names",
    "parse_legacy_update_safety_receipt",
    "parse_active_legacy_forward_receipt",
    "render_legacy_update_safety_dropin",
)
