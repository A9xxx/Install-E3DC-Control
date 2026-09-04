"""Ausgangssperre für Wallbox-Befehle mit kompaktem Auditprotokoll.

Die Sperre liegt bewusst nahe an den schreibenden Treiberpfaden. Ruft eine
übergeordnete Regelung versehentlich eine Schreibmethode auf, während eine
Wallbox auf NGNA/Aus steht, wird der Befehl blockiert, bevor ein Paket den
Prozess verlässt.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any, Callable, Dict, Iterator, Optional
from urllib.parse import parse_qs


AUDIT_LOG = os.path.join("/var/www/html/logs", "wallbox_command_audit.log")
MAX_AUDIT_BYTES = 1024 * 1024
ALLOWED_REPEAT_AUDIT_S = 300.0
BLOCKED_REPEAT_AUDIT_S = 300.0
_LAST_ALLOWED_AUDIT: Dict[str, float] = {}
_LAST_BLOCKED_AUDIT: Dict[str, float] = {}

_STORAGE_HARD_BLOCK_ATTR = "_storage_power_budget_hard_block"
_STORAGE_HARD_BLOCK_REASON_ATTR = "_storage_power_budget_hard_block_reason"
_GROUP_DEFICIT_DOWNWARD_SCOPE_ATTR = "_command_gate_group_deficit_downward_scope"
_GROUP_DEFICIT_DOWNWARD_SCHEMA = "wallbox_group_deficit_downward_authority_v1"
_GROUP_DEFICIT_DOWNWARD_KEYS = frozenset({
    "schema",
    "active",
    "owner_id",
    "wb_id",
    "plug_session_id",
    "binding_generation",
    "cycle_token",
    "snapshot_id",
    "action_latch_id",
    "target_amp",
    "observed_amp",
    "min_amp",
})
_GROUP_DEFICIT_DOWNWARD_SCOPE_SEAL = object()
_USER_OFF_RELEASE_TYPE = "user_off_handoff"
_OPENWB_PRO_MODE0_BINDING_SCHEMA = "openwb_pro_mode0_output_binding_v1"
_OPENWB_PRO_MODE0_BINDING_KEYS = frozenset({
    "schema",
    "request_id",
    "candidate_config_hash",
    "wb_id",
    "device_identity",
    "generation",
    "action",
    "operation_id",
    "lease_record_hash",
    "cycle_token",
})


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def set_storage_power_budget_hard_block(
    charger: Any,
    active: bool,
    *,
    reason: str = "storage_power_budget_readback_blocked",
) -> None:
    """Bindet das Storage-Wattbudget-Veto direkt an den Wallboxtreiber."""
    if charger is None:
        return
    blocked = bool(active)
    setattr(charger, _STORAGE_HARD_BLOCK_ATTR, blocked)
    setattr(
        charger,
        _STORAGE_HARD_BLOCK_REASON_ATTR,
        str(reason or "storage_power_budget_readback_blocked") if blocked else "",
    )
    if blocked:
        _silence_stale_charge_output(charger)


def _silence_stale_charge_output(charger: Any) -> None:
    """Verhindert, dass ein alter E3DC-Sollstrom nach dem Veto wieder anläuft."""
    if hasattr(charger, "_control_generation"):
        charger._control_generation = int(charger._control_generation or 0) + 1
    if hasattr(charger, "external_suspended"):
        charger.external_suspended = True
    if hasattr(charger, "last_amp"):
        charger.last_amp = None
    if hasattr(charger, "last_force_state"):
        charger.last_force_state = None


def _number_is_zero(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and abs(number) <= 1e-9


_EMERGENCY_STOP_ACTIONS = frozenset({
    "e3dc_emergency_stop",
    "e3dc_multi_emergency_stop",
})
_EMERGENCY_STOP_NESTED_ACTIONS = frozenset({
    "e3dc_set_extern",
    "e3dc_set_extern_wire",
    "e3dc_multi_send_command",
})


def _typed_emergency_stop_payload(payload: Any) -> bool:
    """Erkennt ausschließlich den flüchtigen E3/DC-Stop 0 A/Abort=1."""

    if not isinstance(payload, dict):
        return False
    if not _number_is_zero(payload.get("target_amp")):
        return False
    try:
        force_state = float(payload.get("force_state"))
    except (TypeError, ValueError):
        return False
    return force_state == 1.0 and payload.get("heartbeat") is not True


def _is_typed_emergency_output(charger: Any, action: str, payload: Any) -> bool:
    name = str(action or "").strip().lower()
    if name in _EMERGENCY_STOP_ACTIONS and _typed_emergency_stop_payload(payload):
        return True
    scoped = int(
        getattr(charger, "_command_gate_emergency_scope_depth", 0) or 0
    ) > 0
    if not scoped:
        return False
    if name in _EMERGENCY_STOP_NESTED_ACTIONS:
        return _typed_emergency_stop_payload(payload)

    data = payload if isinstance(payload, dict) else {}
    try:
        force_state = float(data.get("force_state"))
    except (TypeError, ValueError):
        force_state = None
    if name in {
        "goe_set_amp_and_state",
        "goe_set_amp_and_state_wire",
        "dummy_set_amp_and_state",
        "openwb_set_amp_and_state",
        "openwb_pro_set_amp_and_state",
    }:
        return force_state == 1.0
    if name == "openwb_set_direct_current":
        return _number_is_zero(data.get("target_amp"))
    if name.startswith("openwb_http_post"):
        try:
            values = parse_qs(
                str(data.get("post_data") or ""),
                keep_blank_values=True,
            )
        except (TypeError, ValueError):
            return False
        return bool(
            set(values) == {"set_chargemode", "chargepoint_nr"}
            and values.get("set_chargemode") == ["stop"]
        )
    if name.startswith("openwb_http_v1_post"):
        topic = str(data.get("topic") or "").strip().lower()
        return bool(
            topic.endswith("/data/set_current")
            and _number_is_zero(data.get("message"))
        )
    if name.startswith("openwb_modbus_write"):
        connector = max(1, _safe_int(getattr(charger, "modbus_connector", 1), 1))
        expected = 10171 + (connector - 1) * 100 + _safe_int(
            getattr(charger, "modbus_offset", 0), 0
        )
        return bool(
            _safe_int(data.get("address"), -1) == expected
            and _number_is_zero(data.get("value"))
        )
    if name.startswith("openwb_pro_post_control"):
        return bool(
            set(data) == {"ampere"}
            and _number_is_zero(data.get("ampere"))
        )
    return False


@contextlib.contextmanager
def emergency_stop_scope(charger: Any) -> Iterator[None]:
    """Bindet die verschachtelten SET_EXTERN-Rahmen an genau einen NOT-AUS-Aufruf."""

    if charger is None:
        yield
        return
    previous = int(getattr(charger, "_command_gate_emergency_scope_depth", 0) or 0)
    charger._command_gate_emergency_scope_depth = previous + 1
    try:
        yield
    finally:
        charger._command_gate_emergency_scope_depth = previous


def emergency_stop_scope_active(charger: Any) -> bool:
    """Meldet ausschließlich den aktuell gebundenen NOT-AUS-Aufruf."""

    return bool(
        charger is not None
        and int(
            getattr(charger, "_command_gate_emergency_scope_depth", 0) or 0
        ) > 0
    )


def _normalized_group_deficit_downward_authority(
    value: Any,
) -> Optional[Dict[str, Any]]:
    """Validiert den vollständigen kurzlebigen PCC-Abwärtsvertrag."""

    if not isinstance(value, dict) or set(value) != _GROUP_DEFICIT_DOWNWARD_KEYS:
        return None
    try:
        owner_id = int(value.get("owner_id"))
        wb_id = int(value.get("wb_id"))
        generation = int(value.get("binding_generation"))
        target_amp = float(value.get("target_amp"))
        observed_amp = float(value.get("observed_amp"))
        min_amp = float(value.get("min_amp"))
    except (TypeError, ValueError, OverflowError):
        return None
    plug_session_id = str(value.get("plug_session_id") or "")
    cycle_token = str(value.get("cycle_token") or "")
    snapshot_id = str(value.get("snapshot_id") or "")
    action_latch_id = str(value.get("action_latch_id") or "").lower()
    if (
        value.get("schema") != _GROUP_DEFICIT_DOWNWARD_SCHEMA
        or value.get("active") is not True
        or owner_id < 1
        or wb_id != owner_id
        or generation < 1
        or not plug_session_id
        or len(plug_session_id) > 256
        or not cycle_token
        or len(cycle_token) > 160
        or not snapshot_id
        or len(snapshot_id) > 200
        or len(action_latch_id) != 71
        or not action_latch_id.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in action_latch_id[7:])
        or not all(
            number == number and abs(number) != float("inf")
            for number in (target_amp, observed_amp, min_amp)
        )
        or min_amp <= 0.0
        or target_amp < min_amp
        or target_amp <= 0.0
        or target_amp >= observed_amp
    ):
        return None
    return {
        "schema": _GROUP_DEFICIT_DOWNWARD_SCHEMA,
        "active": True,
        "owner_id": owner_id,
        "wb_id": wb_id,
        "plug_session_id": plug_session_id,
        "binding_generation": generation,
        "cycle_token": cycle_token,
        "snapshot_id": snapshot_id,
        "action_latch_id": action_latch_id,
        "target_amp": target_amp,
        "observed_amp": observed_amp,
        "min_amp": min_amp,
    }


@contextlib.contextmanager
def group_deficit_downward_scope(
    charger: Any,
    authority: Dict[str, Any],
    *,
    expected_binding: Dict[str, Any],
) -> Iterator[None]:
    """Öffnet genau den gebundenen positiven Strom-Abwärtsbefehl.

    Der Scope verändert das Storage-Veto nicht. Die unabhängig aus der
    Managerbindung projizierte Erwartung muss bytegenau zum Aktionsvertrag
    passen; verschachtelte Treiberaufrufe sehen nur die versiegelte Kopie.
    """

    if charger is None:
        raise ValueError("group_deficit_downward_charger_missing")
    normalized = _normalized_group_deficit_downward_authority(authority)
    expected = _normalized_group_deficit_downward_authority(expected_binding)
    if normalized is None or expected is None or normalized != expected:
        raise ValueError("group_deficit_downward_authority_mismatch")
    context = getattr(charger, "_command_gate_context", None)
    if not isinstance(context, dict):
        raise ValueError("group_deficit_downward_command_context_missing")
    try:
        context_wb_id = int(context.get("wb_id", 0))
    except (TypeError, ValueError, OverflowError):
        context_wb_id = 0
    if context_wb_id != normalized["owner_id"]:
        raise ValueError("group_deficit_downward_owner_mismatch")
    previous = getattr(charger, _GROUP_DEFICIT_DOWNWARD_SCOPE_ATTR, None)
    setattr(
        charger,
        _GROUP_DEFICIT_DOWNWARD_SCOPE_ATTR,
        (_GROUP_DEFICIT_DOWNWARD_SCOPE_SEAL, dict(normalized)),
    )
    try:
        yield
    finally:
        setattr(charger, _GROUP_DEFICIT_DOWNWARD_SCOPE_ATTR, previous)


def _group_deficit_downward_target_from_action(
    charger: Any,
    action: str,
    payload: Any,
) -> Optional[float]:
    """Dekodiert ausschließlich bekannte Strompfade, nie Mode/Phase/Stop."""

    name = str(action or "").strip().lower()
    data = payload if isinstance(payload, dict) else {}
    target_actions = {
        "goe_set_amp_and_state",
        "goe_set_amp_and_state_wire",
        "dummy_set_amp_and_state",
        "openwb_set_amp_and_state",
        "openwb_set_direct_current",
        "openwb_pro_set_amp_and_state",
        "e3dc_set_amp_sonnenmodus",
        "e3dc_set_amp_and_state",
        "e3dc_set_extern",
        "e3dc_set_extern_wire",
        "e3dc_multi_set_amp_sonnenmodus",
        "e3dc_multi_set_amp_autonomous_solar",
        "e3dc_multi_set_amp_and_state",
        "e3dc_multi_send_command",
    }
    if name in target_actions:
        if data.get("heartbeat") is True or data.get("force_state") is not None:
            return None
        try:
            return float(data.get("target_amp"))
        except (TypeError, ValueError, OverflowError):
            return None
    if name in {"openwb_http_post", "openwb_http_post_wire"}:
        try:
            values = parse_qs(
                str(data.get("post_data") or ""),
                keep_blank_values=True,
            )
            if set(values) != {"chargecurrent", "chargepoint_nr"}:
                return None
            if len(values.get("chargecurrent", ())) != 1:
                return None
            return float(values["chargecurrent"][0])
        except (TypeError, ValueError, OverflowError):
            return None
    if name in {"openwb_http_v1_post", "openwb_http_v1_post_wire"}:
        topic = str(data.get("topic") or "").strip().lower()
        if not topic.endswith("/data/set_current"):
            return None
        try:
            return float(data.get("message"))
        except (TypeError, ValueError, OverflowError):
            return None
    if name in {"openwb_modbus_write_connect", "openwb_modbus_write_wire"}:
        connector = max(1, _safe_int(getattr(charger, "modbus_connector", 1), 1))
        expected_address = 10171 + (connector - 1) * 100 + _safe_int(
            getattr(charger, "modbus_offset", 0),
            0,
        )
        if _safe_int(data.get("function_code"), -1) != 6:
            return None
        if _safe_int(data.get("address"), -1) != expected_address:
            return None
        try:
            return float(data.get("value")) / 100.0
        except (TypeError, ValueError, OverflowError):
            return None
    if name in {"openwb_pro_post_control", "openwb_pro_post_control_wire"}:
        if set(data) != {"ampere"}:
            return None
        try:
            return float(data.get("ampere"))
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _storage_hard_block_allows_group_deficit_downward(
    *,
    charger: Any,
    action: str,
    payload: Any,
) -> bool:
    scoped = getattr(charger, _GROUP_DEFICIT_DOWNWARD_SCOPE_ATTR, None)
    if not (
        isinstance(scoped, tuple)
        and len(scoped) == 2
        and scoped[0] is _GROUP_DEFICIT_DOWNWARD_SCOPE_SEAL
    ):
        return False
    authority = _normalized_group_deficit_downward_authority(scoped[1])
    if authority is None:
        return False
    context = getattr(charger, "_command_gate_context", None)
    try:
        context_wb_id = int((context or {}).get("wb_id", 0))
    except (TypeError, ValueError, OverflowError):
        return False
    if context_wb_id != authority["owner_id"]:
        return False
    target = _group_deficit_downward_target_from_action(
        charger,
        action,
        payload,
    )
    if target is None or target != target or abs(target) == float("inf"):
        return False
    return bool(
        target > 0.0
        and target >= authority["min_amp"]
        and target < authority["observed_amp"]
        and abs(target - authority["target_amp"]) <= 0.051
    )


def _storage_hard_block_allows_output(
    *,
    charger: Any,
    action: str,
    payload: Any,
    default_release: bool,
    release_type: str,
) -> bool:
    """Erlaubt unter Storage-Veto ausschließlich sicher typisierte Ausgänge."""

    name = str(action or "").strip().lower()
    data = payload if isinstance(payload, dict) else {}
    user_off_handoff = bool(
        default_release and release_type == _USER_OFF_RELEASE_TYPE
    )

    if _is_typed_emergency_output(charger, name, data):
        return True
    if _storage_hard_block_allows_group_deficit_downward(
        charger=charger,
        action=name,
        payload=data,
    ):
        return True
    try:
        force_state = float(data.get("force_state"))
    except (TypeError, ValueError):
        force_state = None

    e3dc_current_actions = {
        "e3dc_set_amp_sonnenmodus",
        "e3dc_set_amp_and_state",
        "e3dc_set_extern",
        "e3dc_set_extern_wire",
        "e3dc_multi_set_amp_sonnenmodus",
        "e3dc_multi_set_amp_autonomous_solar",
        "e3dc_multi_set_amp_and_state",
        "e3dc_multi_send_command",
    }
    if name in e3dc_current_actions:
        if data.get("heartbeat") is True:
            return False
        return bool(
            force_state == 1.0
            and getattr(charger, "real_charging", None) is True
        )

    absolute_current_actions = {
        "goe_set_amp_and_state",
        "goe_set_amp_and_state_wire",
        "dummy_set_amp_and_state",
        "openwb_set_amp_and_state",
        "openwb_set_direct_current",
        "openwb_pro_set_amp_and_state",
    }
    if name in absolute_current_actions:
        if force_state == 1.0:
            return True
        if name.startswith(("openwb_", "openwb_pro_")):
            return _number_is_zero(data.get("target_amp"))
        return bool(user_off_handoff and force_state == 0.0)
    if user_off_handoff and name in {
        "dummy_release_to_default",
        "openwb_set_pv_mode",
    }:
        return True
    if name.startswith("openwb_http_post"):
        try:
            values = parse_qs(str(data.get("post_data") or ""), keep_blank_values=True)
        except (TypeError, ValueError):
            return False
        if set(values) != {"set_chargemode", "chargepoint_nr"}:
            return False
        mode = values.get("set_chargemode")
        return bool(mode == ["stop"] or (user_off_handoff and mode == ["pv"]))
    if name.startswith("openwb_http_v1_post"):
        topic = str(data.get("topic") or "").strip().lower()
        return topic.endswith("/data/set_current") and _number_is_zero(data.get("message"))
    if name.startswith("openwb_modbus_write"):
        connector = max(1, _safe_int(getattr(charger, "modbus_connector", 1), 1))
        expected = 10171 + (connector - 1) * 100 + _safe_int(
            getattr(charger, "modbus_offset", 0), 0
        )
        return bool(
            _safe_int(data.get("address"), -1) == expected
            and _number_is_zero(data.get("value"))
        )
    if name == "openwb_pro_set_heartbeat":
        return data.get("enabled") is False
    if name.startswith("openwb_pro_post_control"):
        if set(data) == {"ampere"}:
            return _number_is_zero(data.get("ampere"))
        if set(data) == {"heartbeatenabled"}:
            return str(data.get("heartbeatenabled") or "").lower() in {"0", "false", "off"}
    return False


def _compact_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {str(k): _compact_payload(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_compact_payload(v) for v in payload]
    if isinstance(payload, (str, int, float, bool)) or payload is None:
        text = str(payload)
        if isinstance(payload, str) and len(text) > 180:
            return text[:177] + "..."
        return payload
    return str(payload)


def _rotate_audit_if_needed(path: str) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_AUDIT_BYTES:
            old_path = path + ".1"
            try:
                if os.path.exists(old_path):
                    os.remove(old_path)
            except OSError:
                pass
            os.replace(path, old_path)
    except OSError:
        pass


def audit_event(charger: Any, *, action: str, decision: str, reason: str = "", payload: Any = None, owner: str = "") -> None:
    ctx = getattr(charger, "_command_gate_context", {}) or {}
    if not isinstance(ctx, dict):
        ctx = {}
    entry = {
        "ts": round(time.time(), 3),
        "wb": _safe_int(ctx.get("wb_id", getattr(charger, "wb_id", 0)), 0),
        "driver": ctx.get("driver") or charger.__class__.__name__,
        "mode": ctx.get("mode"),
        "native_enabled": ctx.get("native_enabled"),
        "storage_power_budget_hard_block": bool(
            getattr(charger, _STORAGE_HARD_BLOCK_ATTR, False)
            or ctx.get("storage_power_budget_hard_block") is True
        ),
        "owner": owner or ctx.get("owner") or "wallbox_manager",
        "action": str(action or ""),
        "decision": str(decision or ""),
        "reason": str(reason or ctx.get("reason") or ""),
        "payload": _compact_payload(payload),
    }
    if entry["decision"] in {"allowed", "blocked"}:
        signature = json.dumps(
            {
                "wb": entry["wb"],
                "driver": entry["driver"],
                "mode": entry["mode"],
                "action": entry["action"],
                "decision": entry["decision"],
                "reason": entry["reason"],
                "payload": entry["payload"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = float(entry["ts"])
        if entry["decision"] == "allowed":
            repeat_s = ALLOWED_REPEAT_AUDIT_S
            audit_cache = _LAST_ALLOWED_AUDIT
        else:
            repeat_s = BLOCKED_REPEAT_AUDIT_S
            audit_cache = _LAST_BLOCKED_AUDIT
        last = float(audit_cache.get(signature, 0.0) or 0.0)
        if now - last < repeat_s:
            return
        audit_cache[signature] = now
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        _rotate_audit_if_needed(AUDIT_LOG)
        with open(AUDIT_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        # Das Audit darf weder das Laden noch sicherheitsbedingte Stopps stören.
        pass


def configure_charger(
    charger: Any,
    *,
    wb_id: Optional[int] = None,
    mode: Any = None,
    native_enabled: bool = True,
    locked: bool = False,
    driver: str = "",
    owner: str = "wallbox_manager",
    observe_only: bool = False,
    runtime_validation_required: bool = False,
    runtime_validator: Optional[Callable[[], Any]] = None,
    safe_handoff: Optional[Callable[[Any, str], Any]] = None,
    owner_lease_token: Any = None,
    owner_lease_expires_ts: Optional[float] = None,
) -> None:
    if charger is None:
        return
    previous = getattr(charger, "_command_gate_context", {}) or {}
    if not isinstance(previous, dict):
        previous = {}
    charger._command_gate_context = {
        "wb_id": _safe_int(wb_id if wb_id is not None else getattr(charger, "wb_id", 0), 0),
        "mode": _safe_int(mode, 0),
        "native_enabled": bool(native_enabled),
        "locked": bool(locked),
        "observe_only": bool(observe_only),
        "driver": driver or charger.__class__.__name__,
        "owner": owner,
        "ts": time.time(),
        "runtime_validation_required": bool(runtime_validation_required),
        "runtime_validator": runtime_validator,
        "safe_handoff": safe_handoff,
        "owner_lease_token": owner_lease_token,
        "owner_lease_expires_ts": owner_lease_expires_ts,
        "_runtime_last_valid": bool(previous.get("_runtime_last_valid", True)),
        "_runtime_handoff_done": bool(previous.get("_runtime_handoff_done", False)),
        "_runtime_handoff_result": previous.get("_runtime_handoff_result"),
    }


def _normalize_runtime_validation(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        result = dict(value)
        result["valid"] = bool(result.get("valid", False))
        result["reason"] = str(result.get("reason") or ("runtime_valid" if result["valid"] else "runtime_invalid"))
        return result
    if isinstance(value, (tuple, list)) and value:
        valid = bool(value[0])
        reason = str(value[1] if len(value) > 1 else ("runtime_valid" if valid else "runtime_invalid"))
        return {"valid": valid, "reason": reason}
    valid = bool(value)
    return {"valid": valid, "reason": "runtime_valid" if valid else "runtime_invalid"}


def _validate_runtime_context(charger: Any, ctx: Dict[str, Any], owner: str) -> Dict[str, Any]:
    """Prüft Kontext und Owner unmittelbar vor einem ausgehenden Schreibzug erneut."""
    if not bool(ctx.get("runtime_validation_required", False)):
        return {
            "valid": True,
            "reason": "legacy_local_gate",
            "authority_confirmed": False,
        }

    # Die Anlagenrollen-/HA-Autorität muss vor allen kurzlebigen lokalen
    # Owner-/TTL-Prüfungen ausgewertet werden. Andernfalls könnte ein Ablauf
    # der 30-s-Lease den harten Shadow-/HA-Block im Emergency-Zweig verdecken.
    validator = ctx.get("runtime_validator")
    if not callable(validator):
        return {
            "valid": False,
            "reason": "runtime_validator_missing",
            "authority_confirmed": False,
            "hard_authority_block": True,
        }
    try:
        runtime = _normalize_runtime_validation(validator())
    except Exception:
        return {
            "valid": False,
            "reason": "runtime_validator_exception",
            "authority_confirmed": False,
            "hard_authority_block": True,
        }
    if runtime.get("hard_authority_block") is True:
        runtime["valid"] = False
        runtime["authority_confirmed"] = False
        return runtime

    expected_owner = str(ctx.get("owner") or "")
    if not expected_owner or expected_owner != str(owner or ""):
        runtime.update({"valid": False, "reason": "owner_mismatch"})
        return runtime

    lease_expires = ctx.get("owner_lease_expires_ts")
    try:
        lease_valid = lease_expires is not None and float(lease_expires) > time.time()
    except (TypeError, ValueError):
        lease_valid = False
    if not lease_valid or ctx.get("owner_lease_token") is None:
        runtime.update({"valid": False, "reason": "owner_lease_expired_or_missing"})
        return runtime
    return runtime


def _perform_safe_handoff(charger: Any, ctx: Dict[str, Any], reason: str) -> Dict[str, Any]:
    handoff = ctx.get("safe_handoff")
    if not callable(handoff):
        return {
            "status": "unconfirmed",
            "confirmed": False,
            "strategy": "command_silence_only",
            "reason": "safe_handoff_missing",
        }
    try:
        raw = handoff(charger, reason)
        if isinstance(raw, dict):
            result = dict(raw)
            result.setdefault("confirmed", bool(result.get("status") == "confirmed"))
            result.setdefault("status", "confirmed" if result["confirmed"] else "unconfirmed")
            return result
        confirmed = bool(raw)
        return {
            "status": "confirmed" if confirmed else "unconfirmed",
            "confirmed": confirmed,
            "strategy": "driver_callback",
        }
    except Exception:
        return {
            "status": "unconfirmed",
            "confirmed": False,
            "strategy": "driver_callback",
            "reason": "safe_handoff_exception",
        }


def _block_invalid_runtime(
    charger: Any,
    ctx: Dict[str, Any],
    *,
    action: str,
    payload: Any,
    owner: str,
    reason: str,
) -> bool:
    was_valid = bool(ctx.get("_runtime_last_valid", True))
    handoff_done = bool(ctx.get("_runtime_handoff_done", False))
    handoff_result = ctx.get("_runtime_handoff_result")
    if was_valid and not handoff_done:
        handoff_result = _perform_safe_handoff(charger, ctx, reason)
        ctx["_runtime_handoff_done"] = True
        ctx["_runtime_handoff_result"] = handoff_result
    ctx["_runtime_last_valid"] = False
    charger._command_gate_context = ctx
    charger._command_gate_runtime_status = {
        "valid": False,
        "reason": reason,
        "action": str(action or ""),
        "handoff": handoff_result,
        "ts": time.time(),
    }
    audit_event(
        charger,
        action=action,
        decision="blocked",
        reason=reason,
        payload={"command": payload, "safe_handoff": handoff_result},
        owner=owner,
    )
    return False


def is_default_release_allowed(charger: Any) -> bool:
    return bool(getattr(charger, "_command_gate_default_release", False))


def _normalized_openwb_pro_mode0_binding(value: Any) -> Optional[Dict[str, Any]]:
    """Validiert die kurzlebige Managerbindung für genau eine sichere Kante."""

    if not isinstance(value, dict) or set(value) != _OPENWB_PRO_MODE0_BINDING_KEYS:
        return None
    request_id = str(value.get("request_id") or "").lower()
    candidate_hash = str(value.get("candidate_config_hash") or "").lower()
    device_identity = str(value.get("device_identity") or "").lower()
    record_hash = str(value.get("lease_record_hash") or "").lower()
    operation_id = str(value.get("operation_id") or "")
    cycle_token = str(value.get("cycle_token") or "")
    action = str(value.get("action") or "")
    try:
        wb_id = int(value.get("wb_id"))
        generation = int(value.get("generation"))
    except (TypeError, ValueError):
        return None
    sha_values = (candidate_hash, device_identity, record_hash)
    if (
        value.get("schema") != _OPENWB_PRO_MODE0_BINDING_SCHEMA
        or len(request_id) != 32
        or any(char not in "0123456789abcdef" for char in request_id)
        or any(
            len(digest) != 71
            or not digest.startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in digest[7:])
            for digest in sha_values
        )
        or wb_id < 1
        or generation < 1
        or action not in {"send_zero", "send_heartbeat_off"}
        or not operation_id
        or len(operation_id) > 160
        or not cycle_token
        or len(cycle_token) > 160
    ):
        return None
    return {
        "schema": _OPENWB_PRO_MODE0_BINDING_SCHEMA,
        "request_id": request_id,
        "candidate_config_hash": candidate_hash,
        "wb_id": wb_id,
        "device_identity": device_identity,
        "generation": generation,
        "action": action,
        "operation_id": operation_id,
        "lease_record_hash": record_hash,
        "cycle_token": cycle_token,
    }


def default_release_binding(charger: Any) -> Optional[Dict[str, Any]]:
    binding = _normalized_openwb_pro_mode0_binding(
        getattr(charger, "_command_gate_release_binding", None)
    )
    return dict(binding) if binding is not None else None


def _openwb_pro_mode0_action_allowed(
    charger: Any,
    *,
    action: str,
    payload: Any,
) -> bool:
    """Begrenzt den Pro-Übergabescopedown auf 0 A oder Heartbeat aus."""

    if charger.__class__.__name__ != "OpenWBProCharger":
        return True
    binding = default_release_binding(charger)
    if binding is None:
        return False
    name = str(action or "").strip().lower()
    data = payload if isinstance(payload, dict) else {}
    if binding["action"] == "send_zero":
        if name == "openwb_pro_set_amp_and_state":
            try:
                force_state = float(data.get("force_state"))
            except (TypeError, ValueError):
                return False
            return bool(
                _number_is_zero(data.get("target_amp"))
                and force_state == 1.0
            )
        if name in {"openwb_pro_post_control", "openwb_pro_post_control_wire"}:
            return bool(
                set(data) == {"ampere"}
                and _number_is_zero(data.get("ampere"))
            )
        return False
    if binding["action"] == "send_heartbeat_off":
        if name == "openwb_pro_set_heartbeat":
            return data.get("enabled") is False
        if name in {"openwb_pro_post_control", "openwb_pro_post_control_wire"}:
            return bool(
                set(data) == {"heartbeatenabled"}
                and str(data.get("heartbeatenabled") or "").strip().lower()
                in {"0", "false", "off"}
            )
    return False


@contextlib.contextmanager
def default_release_scope(
    charger: Any,
    *,
    reason: str = "mode0_user_switch",
    binding: Optional[Dict[str, Any]] = None,
) -> Iterator[None]:
    if charger is None:
        yield
        return
    prev_allowed = bool(getattr(charger, "_command_gate_default_release", False))
    prev_reason = getattr(charger, "_command_gate_release_reason", "")
    prev_release_type = getattr(charger, "_command_gate_release_type", "")
    prev_binding = getattr(charger, "_command_gate_release_binding", None)
    release_reason = str(reason or "mode0_user_switch")
    normalized_binding = _normalized_openwb_pro_mode0_binding(binding)
    if (
        charger.__class__.__name__ == "OpenWBProCharger"
        and release_reason == "mode0_user_switch"
        and normalized_binding is None
    ):
        raise ValueError("openwb_pro_mode0_release_binding_invalid")
    try:
        charger._command_gate_default_release = True
        charger._command_gate_release_reason = release_reason
        charger._command_gate_release_type = (
            _USER_OFF_RELEASE_TYPE
            if release_reason == "mode0_user_switch"
            else "other_default_release"
        )
        charger._command_gate_release_binding = normalized_binding
    except AttributeError:
        pass
    try:
        yield
    finally:
        try:
            charger._command_gate_default_release = prev_allowed
            charger._command_gate_release_reason = prev_reason
            charger._command_gate_release_type = prev_release_type
            charger._command_gate_release_binding = prev_binding
        except AttributeError:
            pass


def allow_command(
    charger: Any,
    *,
    action: str,
    payload: Any = None,
    owner: str = "wallbox_manager",
    reason: str = "",
    audit_allowed: bool = True,
) -> bool:
    """Liefert ``True``, wenn ein Wallbox-Schreibzug den Prozess verlassen darf.

    Ein fehlender oder ungültiger Kontext wird blockiert und protokolliert. Der
    produktive Manager setzt in jedem Zyklus einen vollständigen Kontext.
    """
    ctx = getattr(charger, "_command_gate_context", None)
    emergency_output = bool(
        str(owner or "") == "wallbox_manager"
        and _is_typed_emergency_output(charger, action, payload)
    )
    if emergency_output:
        runtime_reason = "missing_or_invalid_command_context"
        runtime_valid = False
        runtime = {"valid": False, "reason": runtime_reason}
        if isinstance(ctx, dict) and ctx:
            runtime = _validate_runtime_context(charger, ctx, owner)
            runtime_valid = bool(runtime.get("valid", False))
            runtime_reason = str(
                runtime.get("reason")
                or ("runtime_valid" if runtime_valid else "runtime_context_invalid")
            )
        if (
            runtime.get("hard_authority_block") is True
            or runtime.get("authority_confirmed") is not True
        ):
            return _block_invalid_runtime(
                charger,
                ctx if isinstance(ctx, dict) else {},
                action=action,
                payload=payload,
                owner=owner,
                reason=(
                    runtime_reason
                    if runtime.get("hard_authority_block") is True
                    else "runtime_authority_unconfirmed"
                ),
            )
        storage_blocked = getattr(charger, _STORAGE_HARD_BLOCK_ATTR, False) is True
        charger._command_gate_runtime_status = {
            "valid": runtime_valid,
            "reason": runtime_reason,
            "action": str(action or ""),
            "emergency_override": True,
            "ts": time.time(),
        }
        charger._command_gate_storage_status = {
            "active": storage_blocked,
            "allowed": True,
            "reason": str(
                getattr(charger, _STORAGE_HARD_BLOCK_REASON_ATTR, "")
                or "typed_emergency_stop"
            ),
            "action": str(action or ""),
            "ts": time.time(),
        }
        if audit_allowed:
            audit_event(
                charger,
                action=action,
                decision="emergency_allowed",
                reason="typed_emergency_stop:%s" % runtime_reason,
                payload=payload,
                owner=owner,
            )
        return True
    if not isinstance(ctx, dict) or not ctx:
        audit_event(
            charger,
            action=action,
            decision="blocked",
            reason=reason or "missing_or_invalid_command_context",
            payload=payload,
            owner=owner,
        )
        return False

    runtime = _validate_runtime_context(charger, ctx, owner)
    if not bool(runtime.get("valid", False)):
        return _block_invalid_runtime(
            charger,
            ctx,
            action=action,
            payload=payload,
            owner=owner,
            reason=str(runtime.get("reason") or "runtime_context_invalid"),
        )
    if not bool(ctx.get("_runtime_last_valid", True)):
        ctx["_runtime_handoff_done"] = False
        ctx["_runtime_handoff_result"] = None
    ctx["_runtime_last_valid"] = True
    charger._command_gate_context = ctx
    charger._command_gate_runtime_status = {
        "valid": True,
        "reason": str(runtime.get("reason") or "runtime_valid"),
        "action": str(action or ""),
        "ts": time.time(),
    }

    mode = _safe_int(ctx.get("mode"), 0)
    native_enabled = bool(ctx.get("native_enabled", True))
    observe_only = bool(ctx.get("observe_only", False))
    default_release = is_default_release_allowed(charger)
    release_reason = getattr(charger, "_command_gate_release_reason", reason or "")
    release_type = str(getattr(charger, "_command_gate_release_type", "") or "")

    if (
        default_release
        and release_type == _USER_OFF_RELEASE_TYPE
        and not _openwb_pro_mode0_action_allowed(
            charger,
            action=action,
            payload=payload,
        )
    ):
        audit_event(
            charger,
            action=action,
            decision="blocked",
            reason="openwb_pro_mode0_release_binding_mismatch",
            payload=payload,
            owner=owner,
        )
        return False

    if observe_only and not default_release:
        audit_event(
            charger,
            action=action,
            decision="blocked",
            reason=reason or "openwb_primary_observe_only",
            payload=payload,
            owner=owner,
        )
        return False

    if (mode == 0 or not native_enabled) and not default_release:
        audit_event(
            charger,
            action=action,
            decision="blocked",
            reason=reason or "ngna_observe_only",
            payload=payload,
            owner=owner,
        )
        return False

    storage_blocked = getattr(charger, _STORAGE_HARD_BLOCK_ATTR, False) is True
    storage_reason = str(
        getattr(charger, _STORAGE_HARD_BLOCK_REASON_ATTR, "")
        or "storage_power_budget_readback_blocked"
    )
    storage_output_allowed = bool(
        not storage_blocked
        or _storage_hard_block_allows_output(
            charger=charger,
            action=action,
            payload=payload,
            default_release=default_release,
            release_type=release_type,
        )
    )
    charger._command_gate_storage_status = {
        "active": storage_blocked,
        "allowed": storage_output_allowed,
        "reason": storage_reason,
        "action": str(action or ""),
        "ts": time.time(),
    }
    if not storage_output_allowed:
        _silence_stale_charge_output(charger)
        audit_event(
            charger,
            action=action,
            decision="blocked",
            reason=storage_reason,
            payload=payload,
            owner=owner,
        )
        return False

    if default_release:
        if audit_allowed:
            audit_event(
                charger,
                action=action,
                decision="one_shot_allowed",
                reason=release_reason or reason or "mode0_user_switch",
                payload=payload,
                owner=owner,
            )
        return True

    if audit_allowed:
        audit_event(
            charger,
            action=action,
            decision="allowed",
            reason=reason or "active_control",
            payload=payload,
            owner=owner,
        )
    return True
