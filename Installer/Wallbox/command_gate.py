"""Wallbox outbound command gate and compact audit log.

The gate is deliberately close to the driver write paths. If higher-level
control code accidentally calls a write method while a charger is in NGNA/Aus,
the command is blocked before a packet leaves the process.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any, Dict, Iterator, Optional


AUDIT_LOG = os.path.join("/var/www/html/logs", "wallbox_command_audit.log")
MAX_AUDIT_BYTES = 1024 * 1024
ALLOWED_REPEAT_AUDIT_S = 300.0
BLOCKED_REPEAT_AUDIT_S = 300.0
_LAST_ALLOWED_AUDIT: Dict[str, float] = {}
_LAST_BLOCKED_AUDIT: Dict[str, float] = {}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


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
        # The audit must never break charging or safety stops.
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
) -> None:
    if charger is None:
        return
    charger._command_gate_context = {
        "wb_id": _safe_int(wb_id if wb_id is not None else getattr(charger, "wb_id", 0), 0),
        "mode": _safe_int(mode, 0),
        "native_enabled": bool(native_enabled),
        "locked": bool(locked),
        "observe_only": bool(observe_only),
        "driver": driver or charger.__class__.__name__,
        "owner": owner,
        "ts": time.time(),
    }


def is_default_release_allowed(charger: Any) -> bool:
    return bool(getattr(charger, "_command_gate_default_release", False))


@contextlib.contextmanager
def default_release_scope(charger: Any, *, reason: str = "mode0_user_switch") -> Iterator[None]:
    if charger is None:
        yield
        return
    prev_allowed = bool(getattr(charger, "_command_gate_default_release", False))
    prev_reason = getattr(charger, "_command_gate_release_reason", "")
    charger._command_gate_default_release = True
    charger._command_gate_release_reason = str(reason or "mode0_user_switch")
    try:
        yield
    finally:
        charger._command_gate_default_release = prev_allowed
        charger._command_gate_release_reason = prev_reason


def allow_command(
    charger: Any,
    *,
    action: str,
    payload: Any = None,
    owner: str = "wallbox_manager",
    reason: str = "",
    audit_allowed: bool = True,
) -> bool:
    """Return True if a wallbox write may leave the process.

    Missing or invalid context is blocked and audited. The production manager
    sets a complete context every cycle.
    """
    ctx = getattr(charger, "_command_gate_context", None)
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

    mode = _safe_int(ctx.get("mode"), 0)
    native_enabled = bool(ctx.get("native_enabled", True))
    observe_only = bool(ctx.get("observe_only", False))
    default_release = is_default_release_allowed(charger)
    release_reason = getattr(charger, "_command_gate_release_reason", reason or "")

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
