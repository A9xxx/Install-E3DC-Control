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
_USER_OFF_RELEASE_TYPE = "user_off_handoff"


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


@contextlib.contextmanager
def default_release_scope(charger: Any, *, reason: str = "mode0_user_switch") -> Iterator[None]:
    if charger is None:
        yield
        return
    prev_allowed = bool(getattr(charger, "_command_gate_default_release", False))
    prev_reason = getattr(charger, "_command_gate_release_reason", "")
    prev_release_type = getattr(charger, "_command_gate_release_type", "")
    release_reason = str(reason or "mode0_user_switch")
    try:
        charger._command_gate_default_release = True
        charger._command_gate_release_reason = release_reason
        charger._command_gate_release_type = (
            _USER_OFF_RELEASE_TYPE
            if release_reason == "mode0_user_switch"
            else "other_default_release"
        )
    except AttributeError:
        pass
    try:
        yield
    finally:
        try:
            charger._command_gate_default_release = prev_allowed
            charger._command_gate_release_reason = prev_reason
            charger._command_gate_release_type = prev_release_type
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
