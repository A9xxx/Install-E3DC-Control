"""Persistenter, reiner Handoff-Vertrag für openWB Pro.

Das Modul führt kein Hardware-I/O aus. Der Aufrufer persistiert eine
``pending_intent`` vor dem absoluten Gerätebefehl und dessen Receipt danach.
Eine nach einem Absturz verbliebene Absicht darf nur mit derselben
``operation_id`` wiederholt werden. So sind ``0 A`` und ``Heartbeat aus``
idempotent wiederholbar, ohne aus einer unklaren Generation weiterzulaufen.

Integrationspflicht: Der Manager stellt einen privaten, dem Servicebenutzer
gehörenden State-Ordner mit Modus ``0700`` bereit und verwendet eine eigene
State-Datei je Wallbox. Dieses Modul legt den Ordner bewusst nicht an und
schreibt State sowie CAS-Lock ausschließlich mit Modus ``0600``. Die Wallbox-
ID dieser Datei bleibt unveränderlich; jede neue Generation erhält eine neue,
zufällige 32-Hex-Request-ID. ``timestamp_ns`` stammt dabei immer aus der
serviceeigenen Systemzeit und niemals aus einem Web-Request-Feld.

Schema v2 speichert für pensionierte Request-IDs den Pensionierungszeitpunkt.
Das unveröffentlichte und nie im Feld eingesetzte v1-Stringformat besaß diese
Zeitinformation nicht und wird deshalb bewusst fail-closed verworfen, statt
sein Alter zu erraten oder eine dauerhafte Sperre zu erzeugen. Nur weil v1 nie
Bestandteil eines Releases oder Deployments war, entsteht daraus kein
5.4.5-Migrationsblocker; ein veröffentlichtes Schema dürfte nicht ohne
Migration gewechselt werden.
"""

from __future__ import annotations

from copy import deepcopy
try:
    import fcntl
except ImportError:  # Nicht-POSIX darf den persistenten Vertrag nicht schreiben.
    fcntl = None  # type: ignore
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Dict, Mapping, Optional, Tuple


SCHEMA = "openwb_pro_control_lease_v2"
LEGACY_SCHEMA_V1 = "openwb_pro_control_lease_v1"
DEFAULT_STATE_BASENAME = "wallbox_openwb_pro_control_lease.json"
MAX_STATE_BYTES = 32 * 1024
REQUEST_REPLAY_WINDOW_NS = 24 * 60 * 60 * 1_000_000_000
MAX_RETIRED_REQUESTS_IN_WINDOW = 256

STATE_REQUESTED = "requested"
STATE_ZERO_SENT = "zero_sent"
STATE_ZERO_READBACK_CONFIRMED = "zero_readback_confirmed"
STATE_HEARTBEAT_OFF_SENT = "heartbeat_off_sent"
STATE_RELEASED = "released"

ACTION_SEND_ZERO = "send_zero"
ACTION_CONFIRM_ZERO_READBACK = "confirm_zero_readback"
ACTION_SEND_HEARTBEAT_OFF = "send_heartbeat_off"
ACTION_FINALIZE_RELEASE = "finalize_release"

STATES: Tuple[str, ...] = (
    STATE_REQUESTED,
    STATE_ZERO_SENT,
    STATE_ZERO_READBACK_CONFIRMED,
    STATE_HEARTBEAT_OFF_SENT,
    STATE_RELEASED,
)
_NEXT = {
    STATE_REQUESTED: (ACTION_SEND_ZERO, STATE_ZERO_SENT),
    STATE_ZERO_SENT: (
        ACTION_CONFIRM_ZERO_READBACK,
        STATE_ZERO_READBACK_CONFIRMED,
    ),
    STATE_ZERO_READBACK_CONFIRMED: (
        ACTION_SEND_HEARTBEAT_OFF,
        STATE_HEARTBEAT_OFF_SENT,
    ),
}


class ControlLeaseError(RuntimeError):
    """Basisklasse für einen fail-closed Lease-Fehler."""


class ControlLeaseMissing(ControlLeaseError):
    pass


class ControlLeaseCorrupt(ControlLeaseError):
    pass


class ControlLeaseBindingMismatch(ControlLeaseError):
    pass


class ControlLeaseTransitionError(ControlLeaseError):
    pass


class ControlLeasePersistenceError(ControlLeaseError):
    pass


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ControlLeaseCorrupt("not_canonical_json") from exc


def candidate_config_hash(candidate: Any) -> str:
    """Optionaler Helfer; ein gelieferter PHP-Transaktions-SHA bleibt autoritativ."""

    return "sha256:" + hashlib.sha256(_json_bytes(candidate)).hexdigest()


def device_identity_hash(identity: Any) -> str:
    """Bindet Gerätedaten, ohne Adresse oder Seriennummer im State abzulegen."""

    return "sha256:" + hashlib.sha256(_json_bytes(identity)).hexdigest()


def _text(value: Any, field: str, maximum: int = 160) -> str:
    if not isinstance(value, str):
        raise ControlLeaseCorrupt(field + "_invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ControlLeaseCorrupt(field + "_invalid")
    return normalized


def _request_id(value: Any) -> str:
    normalized = _text(value, "request_id", 32).lower()
    if len(normalized) != 32 or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        raise ControlLeaseCorrupt("request_id_invalid")
    return normalized


def _sha256(value: Any, field: str) -> str:
    normalized = _text(value, field, 71).lower()
    digest = normalized[7:] if normalized.startswith("sha256:") else normalized
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ControlLeaseCorrupt(field + "_invalid")
    return "sha256:" + digest


def _integer(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ControlLeaseCorrupt(field + "_invalid")
    return value


def _retired_requests(value: Any, *, updated_at_ns: int) -> list:
    if (
        not isinstance(value, list)
        or len(value) > MAX_RETIRED_REQUESTS_IN_WINDOW
    ):
        raise ControlLeaseCorrupt("retired_requests_invalid")
    normalized = []
    seen = set()
    previous_retired_at_ns = -1
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "request_id", "retired_at_ns",
        }:
            raise ControlLeaseCorrupt("retired_requests_invalid")
        request_id = _request_id(item.get("request_id"))
        retired_at_ns = _integer(item.get("retired_at_ns"), "retired_at_ns")
        if (
            request_id in seen
            or retired_at_ns < previous_retired_at_ns
            or retired_at_ns > updated_at_ns
        ):
            raise ControlLeaseCorrupt("retired_requests_invalid")
        normalized.append({
            "request_id": request_id,
            "retired_at_ns": retired_at_ns,
        })
        seen.add(request_id)
        previous_retired_at_ns = retired_at_ns
    if normalized != value:
        raise ControlLeaseCorrupt("retired_requests_not_normalized")
    return normalized


def _prune_retired_requests(value: Any, *, now_ns: int) -> list:
    """Behält exakt die IDs, deren 24h-Replayfenster noch nicht abgelaufen ist."""

    cutoff_ns = max(0, now_ns - REQUEST_REPLAY_WINDOW_NS)
    return [
        dict(item)
        for item in value
        # Genau 24 Stunden bleiben noch im akzeptierten Replayfenster.
        if int(item["retired_at_ns"]) >= cutoff_ns
    ]


def make_binding(
    *,
    request_id: Any,
    candidate_hash: Any,
    wb_id: Any,
    device_identity: Any,
    generation: Any,
) -> Dict[str, Any]:
    """Validiert die fünf vollständigen Lease-Bindungen."""

    return {
        "request_id": _request_id(request_id),
        # Dieser Wert darf direkt der revisionsgebundenen PHP-Transaktion
        # entstammen; das Modul parst oder rekonstruiert die Config nicht.
        "candidate_config_hash": _sha256(candidate_hash, "candidate_config_hash"),
        "wb_id": _integer(wb_id, "wb_id", 1),
        # Im State steht ausschließlich ein Digest, kein Klartext-Endpunkt.
        "device_identity": _sha256(device_identity, "device_identity"),
        "generation": _integer(generation, "generation", 1),
    }


def _digest(record: Mapping[str, Any]) -> str:
    material = dict(record)
    material.pop("record_hash", None)
    return "sha256:" + hashlib.sha256(_json_bytes(material)).hexdigest()


def _next(state: str) -> Tuple[str, str]:
    if state == STATE_HEARTBEAT_OFF_SENT:
        return ACTION_FINALIZE_RELEASE, STATE_RELEASED
    if state not in _NEXT:
        raise ControlLeaseTransitionError("no_next_transition")
    return _NEXT[state]


def _validate_intent(intent: Any, state: str, updated_at_ns: int) -> None:
    if intent is None:
        return
    if not isinstance(intent, dict) or set(intent) != {
        "operation_id", "action", "from_state", "to_state", "prepared_at_ns",
    }:
        raise ControlLeaseCorrupt("pending_intent_invalid")
    if state not in _NEXT:
        raise ControlLeaseCorrupt("pending_intent_state_invalid")
    action, target = _NEXT[state]
    if (
        _text(intent.get("operation_id"), "operation_id") != intent["operation_id"]
        or intent.get("action") != action
        or intent.get("from_state") != state
        or intent.get("to_state") != target
        or _integer(intent.get("prepared_at_ns"), "prepared_at_ns") > updated_at_ns
    ):
        raise ControlLeaseCorrupt("pending_intent_binding_invalid")


def _validate_receipt(receipt: Any, updated_at_ns: int) -> None:
    if receipt is None:
        return
    if not isinstance(receipt, dict) or set(receipt) != {
        "operation_id", "receipt_id", "action", "from_state", "to_state",
        "success", "received_at_ns",
    }:
        raise ControlLeaseCorrupt("last_receipt_invalid")
    _text(receipt.get("operation_id"), "operation_id")
    _text(receipt.get("receipt_id"), "receipt_id", 256)
    source = receipt.get("from_state")
    if source not in _NEXT or not isinstance(receipt.get("success"), bool):
        raise ControlLeaseCorrupt("last_receipt_binding_invalid")
    expected_action, expected_target = _NEXT[source]
    result = expected_target if receipt["success"] else source
    if receipt.get("action") != expected_action or receipt.get("to_state") != result:
        raise ControlLeaseCorrupt("last_receipt_transition_invalid")
    if _integer(receipt.get("received_at_ns"), "received_at_ns") > updated_at_ns:
        raise ControlLeaseCorrupt("last_receipt_time_invalid")


def validate_state(record: Any) -> Dict[str, Any]:
    """Prüft Struktur, Semantik und Eigendigest oder verwirft fail-closed."""

    if isinstance(record, dict) and record.get("schema") == LEGACY_SCHEMA_V1:
        raise ControlLeaseCorrupt("legacy_v1_retirement_time_missing")
    if not isinstance(record, dict) or set(record) != {
        "schema", "request_id", "candidate_config_hash", "wb_id",
        "device_identity", "generation", "state", "pending_intent",
        "last_receipt", "updated_at_ns", "superseded",
        "retired_requests", "record_hash",
    }:
        raise ControlLeaseCorrupt("state_shape_invalid")
    if record.get("schema") != SCHEMA or record.get("state") not in STATES:
        raise ControlLeaseCorrupt("state_schema_invalid")
    binding = make_binding(
        request_id=record.get("request_id"),
        candidate_hash=record.get("candidate_config_hash"),
        wb_id=record.get("wb_id"),
        device_identity=record.get("device_identity"),
        generation=record.get("generation"),
    )
    if any(record.get(key) != value for key, value in binding.items()):
        raise ControlLeaseCorrupt("binding_not_normalized")
    updated = _integer(record.get("updated_at_ns"), "updated_at_ns")
    retired_requests = _retired_requests(
        record.get("retired_requests"),
        updated_at_ns=updated,
    )
    if binding["request_id"] in {
        item["request_id"] for item in retired_requests
    }:
        raise ControlLeaseCorrupt("current_request_is_retired")
    _validate_intent(record.get("pending_intent"), record["state"], updated)
    _validate_receipt(record.get("last_receipt"), updated)
    superseded = record.get("superseded")
    if superseded is not None:
        if not isinstance(superseded, dict) or set(superseded) != {
            "request_id", "generation", "state", "record_hash",
        }:
            raise ControlLeaseCorrupt("superseded_invalid")
        if (
            _integer(superseded.get("generation"), "superseded_generation", 1)
            >= binding["generation"]
            or superseded.get("state") not in STATES
        ):
            raise ControlLeaseCorrupt("superseded_generation_invalid")
        _request_id(superseded.get("request_id"))
        _sha256(superseded.get("record_hash"), "superseded_record_hash")
    if record.get("record_hash") != _digest(record):
        raise ControlLeaseCorrupt("record_hash_mismatch")
    return deepcopy(record)


def _seal(record: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(record)
    result["record_hash"] = _digest(result)
    return validate_state(result)


def require_binding(record: Any, expected_binding: Mapping[str, Any]) -> Dict[str, Any]:
    state = validate_state(record)
    try:
        expected = make_binding(
            request_id=expected_binding.get("request_id"),
            candidate_hash=expected_binding.get("candidate_config_hash"),
            wb_id=expected_binding.get("wb_id"),
            device_identity=expected_binding.get("device_identity"),
            generation=expected_binding.get("generation"),
        )
    except (AttributeError, ControlLeaseCorrupt) as exc:
        raise ControlLeaseBindingMismatch("expected_binding_invalid") from exc
    if {key: state[key] for key in expected} != expected:
        raise ControlLeaseBindingMismatch("lease_binding_mismatch")
    return state


def create_request(
    *, request_id: Any, candidate_hash: Any, wb_id: Any,
    device_identity: Any, generation: Any, timestamp_ns: Any,
    superseded: Optional[Mapping[str, Any]] = None,
    retired_requests: Optional[Any] = None,
) -> Dict[str, Any]:
    binding = make_binding(
        request_id=request_id,
        candidate_hash=candidate_hash,
        wb_id=wb_id,
        device_identity=device_identity,
        generation=generation,
    )
    return _seal({
        "schema": SCHEMA,
        **binding,
        "state": STATE_REQUESTED,
        "pending_intent": None,
        "last_receipt": None,
        "updated_at_ns": _integer(timestamp_ns, "timestamp_ns"),
        "superseded": dict(superseded) if superseded is not None else None,
        "retired_requests": deepcopy(retired_requests or []),
        "record_hash": "",
    })


def request_handoff(
    existing: Optional[Mapping[str, Any]],
    *, request_id: Any, candidate_hash: Any, wb_id: Any,
    device_identity: Any, generation: Any, timestamp_ns: Any,
) -> Dict[str, Any]:
    """Idempotiert einen Request oder ersetzt nur eine ältere aktive Generation."""

    requested = make_binding(
        request_id=request_id,
        candidate_hash=candidate_hash,
        wb_id=wb_id,
        device_identity=device_identity,
        generation=generation,
    )
    if existing is None:
        return create_request(
            request_id=requested["request_id"],
            candidate_hash=requested["candidate_config_hash"],
            wb_id=requested["wb_id"],
            device_identity=requested["device_identity"],
            generation=requested["generation"],
            timestamp_ns=timestamp_ns,
        )
    current = validate_state(existing)
    if {key: current[key] for key in requested} == requested:
        return current
    if requested["generation"] <= current["generation"]:
        raise ControlLeaseBindingMismatch("generation_not_newer")
    event_ns = _integer(timestamp_ns, "timestamp_ns")
    if event_ns < current["updated_at_ns"]:
        raise ControlLeaseTransitionError("event_time_reversed")
    active_retired = _prune_retired_requests(
        current["retired_requests"],
        now_ns=event_ns,
    )
    # Eine Datei gehört lebenslang genau zu ihrer Wallbox-ID. Selbst ein
    # terminaler Handoff darf niemals auf eine andere per-WB-Datei umdeuten.
    if requested["wb_id"] != current["wb_id"]:
        raise ControlLeaseBindingMismatch("wallbox_id_is_immutable")
    # request_id bezeichnet genau eine Transaktion samt vollständiger
    # Bindung. Eine neue Generation braucht deshalb immer eine neue ID;
    # Wiederverwendung darf weder Config- noch Gerätewechsel tarnen.
    if requested["request_id"] in (
        [item["request_id"] for item in active_retired]
        + [current["request_id"]]
    ):
        raise ControlLeaseBindingMismatch("request_id_reuse_blocked")
    if (
        current["state"] != STATE_RELEASED
        and current["device_identity"] != requested["device_identity"]
    ):
        raise ControlLeaseBindingMismatch("new_generation_device_mismatch")
    if len(active_retired) >= MAX_RETIRED_REQUESTS_IN_WINDOW:
        raise ControlLeaseTransitionError("request_replay_window_saturated")
    return create_request(
        request_id=requested["request_id"],
        candidate_hash=requested["candidate_config_hash"],
        wb_id=requested["wb_id"],
        device_identity=requested["device_identity"],
        generation=requested["generation"],
        timestamp_ns=event_ns,
        retired_requests=(
            active_retired
            + [{
                "request_id": current["request_id"],
                "retired_at_ns": event_ns,
            }]
        ),
        superseded={
            "request_id": current["request_id"],
            "generation": current["generation"],
            "state": current["state"],
            "record_hash": current["record_hash"],
        },
    )


def expected_next(record: Any) -> Dict[str, str]:
    current = validate_state(record)
    action, target = _next(current["state"])
    return {"action": action, "from_state": current["state"], "to_state": target}


def prepare_transition(
    record: Any, *, action: Any, operation_id: Any, timestamp_ns: Any,
) -> Dict[str, Any]:
    """Erzeugt eine Write-Ahead-Absicht; derselbe absolute Auftrag ist idempotent."""

    current = validate_state(record)
    if current["state"] not in _NEXT:
        raise ControlLeaseTransitionError("transition_requires_local_finalize")
    expected_action, target = _NEXT[current["state"]]
    action_text = _text(action, "action", 80)
    operation_text = _text(operation_id, "operation_id")
    if action_text != expected_action:
        raise ControlLeaseTransitionError("unexpected_action")
    pending = current.get("pending_intent")
    if pending is not None:
        if pending["action"] == action_text and pending["operation_id"] == operation_text:
            return current
        raise ControlLeaseTransitionError("different_intent_already_pending")
    event_ns = _integer(timestamp_ns, "timestamp_ns")
    if event_ns < current["updated_at_ns"]:
        raise ControlLeaseTransitionError("event_time_reversed")
    current["pending_intent"] = {
        "operation_id": operation_text,
        "action": action_text,
        "from_state": current["state"],
        "to_state": target,
        "prepared_at_ns": event_ns,
    }
    current["updated_at_ns"] = event_ns
    return _seal(current)


def record_receipt(
    record: Any, *, operation_id: Any, receipt_id: Any,
    success: Any, timestamp_ns: Any,
) -> Dict[str, Any]:
    current = validate_state(record)
    intent = current.get("pending_intent")
    if not isinstance(intent, dict):
        raise ControlLeaseTransitionError("intent_missing")
    operation_text = _text(operation_id, "operation_id")
    if operation_text != intent["operation_id"]:
        raise ControlLeaseTransitionError("receipt_operation_mismatch")
    if not isinstance(success, bool):
        raise ControlLeaseTransitionError("receipt_success_not_boolean")
    event_ns = _integer(timestamp_ns, "timestamp_ns")
    if event_ns < current["updated_at_ns"]:
        raise ControlLeaseTransitionError("receipt_time_reversed")
    result_state = intent["to_state"] if success else intent["from_state"]
    current["state"] = result_state
    current["pending_intent"] = None
    current["last_receipt"] = {
        "operation_id": operation_text,
        "receipt_id": _text(receipt_id, "receipt_id", 256),
        "action": intent["action"],
        "from_state": intent["from_state"],
        "to_state": result_state,
        "success": success,
        "received_at_ns": event_ns,
    }
    current["updated_at_ns"] = event_ns
    return _seal(current)


def finalize_release(record: Any, *, timestamp_ns: Any) -> Dict[str, Any]:
    """Schließt nach bestätigtem Heartbeat-off rein lokal ab, ohne GET-Hürde."""

    current = validate_state(record)
    receipt = current.get("last_receipt")
    if (
        current["state"] != STATE_HEARTBEAT_OFF_SENT
        or current.get("pending_intent") is not None
        or not isinstance(receipt, dict)
        or receipt.get("action") != ACTION_SEND_HEARTBEAT_OFF
        or receipt.get("success") is not True
    ):
        raise ControlLeaseTransitionError("heartbeat_off_receipt_missing")
    event_ns = _integer(timestamp_ns, "timestamp_ns")
    if event_ns < current["updated_at_ns"]:
        raise ControlLeaseTransitionError("event_time_reversed")
    current["state"] = STATE_RELEASED
    current["updated_at_ns"] = event_ns
    return _seal(current)


def _pairs_without_duplicates(pairs: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ControlLeaseCorrupt("duplicate_json_key")
        result[key] = value
    return result


def _nofollow() -> int:
    value = getattr(os, "O_NOFOLLOW", None)
    if value is None:
        raise ControlLeasePersistenceError("nofollow_not_supported")
    return int(value)


def _open_parent(path: Path) -> int:
    try:
        descriptor = os.open(
            os.fspath(path.parent),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _nofollow(),
        )
    except OSError as exc:
        raise ControlLeasePersistenceError("unsafe_parent") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ControlLeasePersistenceError("parent_not_directory")
    return descriptor


def _target_stat(directory_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ControlLeasePersistenceError("target_stat_failed") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ControlLeasePersistenceError("target_not_single_regular_file")
    return metadata


def _open_exclusive_lock(directory_fd: int, name: str) -> int:
    """Bindet jeden CAS an einen exklusiven, einzelnen regulären Lock-Inode."""

    if fcntl is None:
        raise ControlLeasePersistenceError("exclusive_lock_not_supported")
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | _nofollow(),
            0o600,
            dir_fd=directory_fd,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ControlLeasePersistenceError("lock_not_single_regular_file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        named = _target_stat(directory_fd, name)
        after = os.fstat(descriptor)
        if (
            named is None
            or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
        ):
            raise ControlLeasePersistenceError("lock_changed_during_acquire")
        os.fchmod(descriptor, 0o600)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise ControlLeasePersistenceError("lock_mode_invalid")
        return descriptor
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise ControlLeasePersistenceError("exclusive_lock_failed") from exc
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _read_state_at(
    directory_fd: int,
    name: str,
    *,
    expected_binding: Optional[Mapping[str, Any]] = None,
    allow_missing: bool = False,
) -> Optional[Dict[str, Any]]:
    """Liest Target und Preimage relativ zum bereits gebundenen Ordner-Inode."""

    descriptor = -1
    try:
        before = _target_stat(directory_fd, name)
        if before is None:
            if allow_missing:
                return None
            raise ControlLeaseMissing("state_missing")
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _nofollow(),
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise ControlLeasePersistenceError("state_open_failed") from exc
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > MAX_STATE_BYTES
        ):
            raise ControlLeasePersistenceError("state_changed_or_too_large")
        payload = b""
        while len(payload) <= MAX_STATE_BYTES:
            chunk = os.read(descriptor, 8192)
            if not chunk:
                break
            payload += chunk
        if len(payload) > MAX_STATE_BYTES:
            raise ControlLeaseCorrupt("state_too_large")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise ControlLeasePersistenceError("state_changed_during_read")
        try:
            parsed = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_pairs_without_duplicates,
            )
        except ControlLeaseCorrupt:
            raise
        except (UnicodeError, ValueError, TypeError) as exc:
            raise ControlLeaseCorrupt("state_json_invalid") from exc
        state = validate_state(parsed)
        return require_binding(state, expected_binding) if expected_binding else state
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_state(
    path: Any, *, expected_binding: Optional[Mapping[str, Any]] = None,
    allow_missing: bool = False,
) -> Optional[Dict[str, Any]]:
    target = Path(path)
    directory_fd = _open_parent(target)
    try:
        return _read_state_at(
            directory_fd,
            target.name,
            expected_binding=expected_binding,
            allow_missing=allow_missing,
        )
    finally:
        os.close(directory_fd)


def write_state(
    path: Any,
    record: Any,
    *,
    expected_record_hash: Optional[str],
    mode: int = 0o600,
) -> Dict[str, Any]:
    """Schreibt unter exklusivem Lock mit verbindlichem Preimage-CAS.

    ``expected_record_hash=None`` ist ausschließlich für die Erstanlage bei
    fehlendem Target zulässig. Jedes Folgeschreiben muss den aktuell
    persistierten ``record_hash`` exakt nennen.
    """

    state = validate_state(record)
    if isinstance(mode, bool) or mode != 0o600:
        raise ControlLeasePersistenceError("unsafe_mode")
    expected_hash = (
        None
        if expected_record_hash is None
        else _sha256(expected_record_hash, "expected_record_hash")
    )
    payload = _json_bytes(state) + b"\n"
    if len(payload) > MAX_STATE_BYTES:
        raise ControlLeasePersistenceError("state_payload_too_large")
    target = Path(path)
    directory_fd = _open_parent(target)
    lock_descriptor = -1
    temporary = ".%s.%s.tmp" % (target.name, secrets.token_hex(8))
    descriptor = -1
    try:
        lock_descriptor = _open_exclusive_lock(
            directory_fd,
            target.name + ".lock",
        )
        current = _read_state_at(
            directory_fd,
            target.name,
            allow_missing=True,
        )
        if current is None:
            if expected_hash is not None:
                raise ControlLeasePersistenceError("cas_target_missing")
        else:
            if expected_hash is None:
                raise ControlLeasePersistenceError("cas_target_already_exists")
            if not secrets.compare_digest(current["record_hash"], expected_hash):
                raise ControlLeasePersistenceError("cas_record_hash_mismatch")
            if state["generation"] < current["generation"]:
                raise ControlLeasePersistenceError("generation_rollback_blocked")
            if state["generation"] == current["generation"] and any(
                state[key] != current[key]
                for key in (
                    "request_id",
                    "candidate_config_hash",
                    "wb_id",
                    "device_identity",
                    "generation",
                )
            ):
                raise ControlLeasePersistenceError("same_generation_binding_mismatch")
            if (
                state["generation"] == current["generation"]
                and state["retired_requests"] != current["retired_requests"]
            ):
                raise ControlLeasePersistenceError("same_generation_lineage_mismatch")
            if state["generation"] > current["generation"]:
                superseded = state.get("superseded")
                if (
                    state["state"] != STATE_REQUESTED
                    or state.get("pending_intent") is not None
                    or state.get("last_receipt") is not None
                    or not isinstance(superseded, dict)
                    or superseded.get("request_id") != current["request_id"]
                    or superseded.get("generation") != current["generation"]
                    or superseded.get("state") != current["state"]
                    or superseded.get("record_hash") != current["record_hash"]
                ):
                    raise ControlLeasePersistenceError("generation_supersede_invalid")
                if state["wb_id"] != current["wb_id"]:
                    raise ControlLeasePersistenceError("wallbox_id_is_immutable")
                active_retired = _prune_retired_requests(
                    current["retired_requests"],
                    now_ns=state["updated_at_ns"],
                )
                if state["request_id"] in (
                    [item["request_id"] for item in active_retired]
                    + [current["request_id"]]
                ):
                    raise ControlLeasePersistenceError("request_id_reuse_blocked")
                if len(active_retired) >= MAX_RETIRED_REQUESTS_IN_WINDOW:
                    raise ControlLeasePersistenceError(
                        "request_replay_window_saturated"
                    )
                expected_lineage = active_retired + [{
                    "request_id": current["request_id"],
                    "retired_at_ns": state["updated_at_ns"],
                }]
                if state["retired_requests"] != expected_lineage:
                    raise ControlLeasePersistenceError("request_lineage_mismatch")
                if (
                    current["state"] != STATE_RELEASED
                    and state["device_identity"] != current["device_identity"]
                ):
                    raise ControlLeasePersistenceError("cas_device_binding_mismatch")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0) | _nofollow(),
            mode,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise ControlLeasePersistenceError("short_write")
            offset += written
        os.fsync(descriptor)
        created = os.fstat(descriptor)
        if (
            not stat.S_ISREG(created.st_mode)
            or created.st_nlink != 1
            or stat.S_IMODE(created.st_mode) != 0o600
        ):
            raise ControlLeasePersistenceError("temp_not_regular")
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, target.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        installed = _target_stat(directory_fd, target.name)
        if installed is None or stat.S_IMODE(installed.st_mode) != 0o600:
            raise ControlLeasePersistenceError("installed_mode_invalid")
        os.fsync(directory_fd)
        binding = {key: state[key] for key in (
            "request_id", "candidate_config_hash", "wb_id",
            "device_identity", "generation",
        )}
        loaded = _read_state_at(
            directory_fd,
            target.name,
            expected_binding=binding,
        )
        if loaded is None or loaded["record_hash"] != state["record_hash"]:
            raise ControlLeasePersistenceError("postwrite_verification_failed")
        return loaded
    except OSError as exc:
        raise ControlLeasePersistenceError("atomic_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        if lock_descriptor >= 0:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)
        os.close(directory_fd)


__all__ = [name for name in globals() if name.startswith(("ACTION_", "STATE_"))] + [
    "ControlLeaseBindingMismatch", "ControlLeaseCorrupt", "ControlLeaseError",
    "ControlLeaseMissing", "ControlLeasePersistenceError",
    "ControlLeaseTransitionError", "DEFAULT_STATE_BASENAME", "SCHEMA", "STATES",
    "MAX_RETIRED_REQUESTS_IN_WINDOW", "REQUEST_REPLAY_WINDOW_NS",
    "candidate_config_hash", "create_request", "device_identity_hash", "expected_next",
    "finalize_release", "make_binding", "prepare_transition", "read_state",
    "record_receipt", "request_handoff", "require_binding", "validate_state",
    "write_state",
]
