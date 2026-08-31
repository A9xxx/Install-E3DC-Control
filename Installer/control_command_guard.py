"""Inline command guard and shadow history for closed-loop controller outputs.

The guard is intentionally close to hardware write paths. It observes the
command stream that would leave the process, blocks unsafe chatter patterns and
writes a compact shadow status for diagnostics. It must never talk to hardware.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import control_safety
except Exception:  # pragma: no cover - package import fallback
    from . import control_safety  # type: ignore


RAMDISK_DIR = "/var/www/html/ramdisk"
DEFAULT_STATUS_FILE = os.path.join(RAMDISK_DIR, "control_shadow_status.json")
DEFAULT_HISTORY_FILE = os.path.join(RAMDISK_DIR, "control_shadow_history.jsonl")
MAX_HISTORY_BYTES = 1024 * 1024
MAX_EVENTS_PER_ACTOR = 16
MAX_EVENT_CLOCK_SKEW_S = 5.0

WALLBOX_START_STOP_PATTERNS: Tuple[Tuple[str, str, str], ...] = (
    ("START", "STOP", "START"),
    ("STOP", "START", "STOP"),
)
WALLBOX_PHASE_PATTERNS: Tuple[Tuple[str, str, str], ...] = (
    ("1P", "3P", "1P"),
    ("3P", "1P", "3P"),
)
RESTART_OVERRIDE_REASON_MARKERS: Tuple[str, ...] = (
    "manual",
    "user",
    "owner_override",
    "planned_start",
    "slot_start",
    "scheduled_slot",
    "grid_allowed",
    "price_slot",
    "boost",
    "phase_start_hold",
    "start_retry",
    "surplus_recovery",
)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _default_status_path() -> str:
    return os.environ.get("E3DC_CONTROL_SHADOW_STATUS_FILE", DEFAULT_STATUS_FILE)


def _default_history_path() -> str:
    return os.environ.get("E3DC_CONTROL_SHADOW_HISTORY_FILE", DEFAULT_HISTORY_FILE)


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path: str, data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o664)
        except Exception:
            pass
    except Exception:
        pass


def _rotate_history_if_needed(path: str) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_HISTORY_BYTES:
            old_path = path + ".1"
            try:
                if os.path.exists(old_path):
                    os.remove(old_path)
            except OSError:
                pass
            os.replace(path, old_path)
    except OSError:
        pass


def _append_jsonl(path: str, record: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _rotate_history_if_needed(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    except Exception:
        pass


def _compact_command(command: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key in (
        "schema_version", "kind", "method", "amp", "target_phases", "phases",
        "force_state", "reason", "source", "_guard_allow_restart_after_stop", "_guard_actor_active",
    ):
        if key in command:
            result[key] = command.get(key)
    return result


def _wallbox_actor(wb_id: Any) -> str:
    return "wallbox:%d" % max(0, _safe_int(wb_id, 0))


def _wallbox_command_action(command: Dict[str, Any], actor_state: Dict[str, Any]) -> str:
    method = str(command.get("method") or command.get("kind") or "").strip().lower()
    kind = str(command.get("kind") or method or "").strip().lower()
    amp = _safe_int(command.get("amp"), 0)
    target_phases = _safe_int(command.get("target_phases", command.get("phases")), 0)
    active = bool(command.get("_guard_actor_active", False) or actor_state.get("active", False))

    if method in {"stop", "emergency_stop"} or kind in {"stop", "emergency_stop"}:
        return "STOP"
    if method in {"set_phases", "phase_switch"} or kind in {"set_phases", "phase_switch"}:
        if target_phases >= 3:
            return "3P"
        if target_phases == 1:
            return "1P"
        return ""
    if method in {
        "set_current",
        "set_amp_and_state",
        "set_amp_sonnenmodus",
        "set_amp_autonomous_solar",
        "set_direct_current",
    } or kind in {
        "set_current",
        "hold_current",
    }:
        if amp <= 0:
            return "STOP" if active else ""
        return "START" if not active else "CURRENT"
    if method in {"release_to_default", "release_to_e3dc"} or kind in {"release_to_default", "release_to_e3dc"}:
        return "START" if not active else "CURRENT"
    return ""


def _candidate_event(
    *,
    wb_id: int,
    actor: str,
    action: str,
    command: Dict[str, Any],
    reason: str,
    now_ts: float,
    target_reachable: bool,
) -> Dict[str, Any]:
    return {
        "actor": actor,
        "wb_id": wb_id,
        "action": action,
        "ts": round(float(now_ts), 3),
        "reason": str(reason or command.get("reason") or action.lower()),
        "target_reachable": bool(target_reachable),
        "command": _compact_command(command),
    }


def _trim_events(
    events: Iterable[Dict[str, Any]],
    *,
    now_ts: Optional[float] = None,
    max_age_s: Optional[float] = None,
) -> List[Dict[str, Any]]:
    now_value = _safe_float(now_ts, 0.0)
    age_limit = None if max_age_s is None else max(0.0, _safe_float(max_age_s, 0.0))
    timeline = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_ts = _safe_float(event.get("ts"), 0.0)
        if now_value > 0.0 and age_limit is not None:
            age_s = now_value - event_ts
            # A chatter window is relevant only while it can still combine
            # with the current candidate. Stale or future-restored events
            # remain in the append-only history, but must never deadlock the
            # real actuator after their configured protection window.
            if age_s >= age_limit or age_s < -MAX_EVENT_CLOCK_SKEW_S:
                continue
        timeline.append(event)
    timeline.sort(key=lambda event: _safe_float(event.get("ts"), 0.0))
    return timeline[-MAX_EVENTS_PER_ACTOR:]


def _candidate_violations(result: Dict[str, Any], candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return only violations newly completed by the current candidate."""

    violations = []
    for violation in result.get("violations") or []:
        window = violation.get("events") if isinstance(violation, dict) else None
        if isinstance(window, list) and window and window[-1] is candidate:
            violations.append(violation)
    return violations


def _evaluate_wallbox_events(
    events: List[Dict[str, Any]],
    *,
    candidate: Dict[str, Any],
    start_stop_gap_s: int,
    phase_gap_s: int,
) -> Dict[str, Any]:
    start_stop = control_safety.detect_command_chatter(
        events,
        unsafe_patterns=WALLBOX_START_STOP_PATTERNS,
        min_gap_s=start_stop_gap_s,
    )
    phases = control_safety.detect_command_chatter(
        events,
        unsafe_patterns=WALLBOX_PHASE_PATTERNS,
        min_gap_s=phase_gap_s,
    )
    violations = []
    violations.extend(_candidate_violations(start_stop, candidate))
    violations.extend(_candidate_violations(phases, candidate))
    return {
        "ok": not violations,
        "violations": violations,
        "checks": {
            "wallbox_start_stop": start_stop,
            "wallbox_phase": phases,
        },
    }


def _reason_has_protection_marker(reason: Any) -> bool:
    reason_text = str(reason or "").lower()
    reason_words = re.sub(r"\s+", " ", reason_text).strip()
    for marker in control_safety.DEFAULT_PROTECTION_REASON_MARKERS:
        marker_text = str(marker).strip().lower()
        if not marker_text:
            continue
        if " " in marker_text:
            if marker_text in reason_words:
                return True
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(marker_text)}(?![a-z0-9])", reason_text):
            return True
    return False


def _window_has_protection(events: Iterable[Dict[str, Any]]) -> bool:
    for event in events:
        reason = " ".join(
            str(event.get(key, "") or "")
            for key in ("reason", "owner", "protection_reason")
        )
        if _reason_has_protection_marker(reason):
            return True
        if str(event.get("target_reachable", "true")).strip().lower() in ("0", "false", "no", "nein"):
            return True
    return False


def _event_has_restart_override(event: Dict[str, Any]) -> bool:
    reason = str(event.get("reason", "") or "").lower()
    command = event.get("command") if isinstance(event.get("command"), dict) else {}
    if bool(command.get("_guard_allow_restart_after_stop", False)):
        return True
    reason = " ".join((reason, str(command.get("reason", "") or "").lower()))
    for marker in RESTART_OVERRIDE_REASON_MARKERS:
        marker_text = str(marker).strip().lower()
        if not marker_text:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(marker_text)}(?![a-z0-9])", reason):
            return True
    return False


def _openwb_pro_phase_zero_restart_override(candidate: Dict[str, Any], violations: Iterable[Dict[str, Any]]) -> bool:
    """Allow the protected openWB Pro restart after its own 0 A phase step."""

    if str(candidate.get("action", "")).upper() != "START":
        return False
    if not _event_has_restart_override(candidate):
        return False
    command = candidate.get("command") if isinstance(candidate.get("command"), dict) else {}
    candidate_text = " ".join(
        str(value or "").lower()
        for value in (
            candidate.get("reason"),
            command.get("reason"),
            command.get("method"),
            command.get("kind"),
        )
    )
    if "openwb_pro" not in candidate_text:
        return False
    allowed_types = {"restart_after_stop", "stop_start_stop", "start_stop_start"}
    saw_phase_zero = False
    for violation in violations:
        if not isinstance(violation, dict):
            return False
        violation_type = str(violation.get("type", "") or "")
        if violation_type not in allowed_types:
            return False
        for event in violation.get("events") or []:
            if not isinstance(event, dict):
                continue
            event_command = event.get("command") if isinstance(event.get("command"), dict) else {}
            event_action = str(event.get("action", "") or "").upper()
            event_text = " ".join(
                str(value or "").lower()
                for value in (
                    event.get("reason"),
                    event_command.get("reason"),
                    event_command.get("method"),
                    event_command.get("kind"),
                )
            )
            event_is_phase_zero = "openwb_pro_phase_zero" in event_text
            event_is_openwb_pro_start = (
                event_action == "START"
                and "openwb_pro" in event_text
                and _event_has_restart_override(event)
            )
            if event_action == "STOP" and not event_is_phase_zero:
                return False
            if event_action == "START" and not event_is_openwb_pro_start:
                return False
            if event_is_phase_zero:
                saw_phase_zero = True
    return saw_phase_zero


def _recent_restart_violation(
    events: List[Dict[str, Any]],
    candidate: Dict[str, Any],
    *,
    min_gap_s: int,
) -> Optional[Dict[str, Any]]:
    if str(candidate.get("action", "")).upper() != "START":
        return None
    recent = [event for event in events if str(event.get("action", "")).upper() in {"START", "STOP"}]
    if not recent:
        return None
    previous = recent[-1]
    if str(previous.get("action", "")).upper() != "STOP":
        return None
    age_s = _safe_float(candidate.get("ts"), 0.0) - _safe_float(previous.get("ts"), 0.0)
    window = [previous, candidate]
    if age_s >= min_gap_s or _event_has_restart_override(candidate):
        return None
    return {
        "type": "restart_after_stop",
        "actor": candidate.get("actor", ""),
        "age_s": int(round(max(0.0, age_s))),
        "events": window,
    }


def commit_wallbox_command_decision(
    decision: Dict[str, Any],
    *,
    status_path: Optional[str] = None,
    history_path: Optional[str] = None,
    start_stop_gap_s: int = 180,
    phase_gap_s: int = 300,
) -> Dict[str, Any]:
    """Persistiere eine bereits bewertete Entscheidung erst nach Ausgangsbeleg."""

    record = decision if isinstance(decision, dict) else {}
    if not record:
        return {}
    status_file = status_path or _default_status_path()
    history_file = history_path or _default_history_path()
    now_value = _safe_float(record.get("ts"), time.time())
    status = _read_json(status_file)
    actors = status.get("actors") if isinstance(status.get("actors"), dict) else {}
    actor = str(record.get("actor") or _wallbox_actor(record.get("wb_id", 0)))
    actor_state = actors.get(actor) if isinstance(actors.get(actor), dict) else {}
    event_max_age_s = max(1.0, float(start_stop_gap_s), float(phase_gap_s))
    events = _trim_events(
        actor_state.get("events") or [],
        now_ts=now_value,
        max_age_s=event_max_age_s,
    )
    action = str(record.get("action") or "NOOP").upper()
    allowed = record.get("allowed") is True
    candidate = record.get("candidate_event")
    if allowed:
        if isinstance(candidate, dict):
            events = _trim_events(
                events + [candidate],
                now_ts=now_value,
                max_age_s=event_max_age_s,
            )
        if action in {"START", "CURRENT"}:
            actor_state["active"] = True
        elif action == "STOP":
            actor_state["active"] = False
        if action in {"1P", "3P"}:
            actor_state["phase"] = 3 if action == "3P" else 1
    else:
        actor_state["last_blocked"] = record

    actor_state["events"] = events
    actor_state["last_decision"] = record
    actor_state["last_ts"] = round(now_value, 3)
    actors[actor] = actor_state
    status.update({
        "ts": round(now_value, 3),
        "service": "control_command_guard",
        "status": "OK" if allowed else "WARN",
        "last_decision": record,
        "actors": actors,
    })
    _write_json_atomic(status_file, status)
    _append_jsonl(history_file, record)
    return record

def evaluate_wallbox_command(
    command: Dict[str, Any],
    *,
    wb_id: int,
    reason: str = "",
    target_reachable: bool = True,
    now_ts: Optional[float] = None,
    status_path: Optional[str] = None,
    history_path: Optional[str] = None,
    start_stop_gap_s: int = 180,
    phase_gap_s: int = 300,
    commit: bool = True,
) -> Dict[str, Any]:
    """Return whether a wallbox command may be sent to the real driver."""

    now_value = float(now_ts if now_ts is not None else time.time())
    status_file = status_path or _default_status_path()
    history_file = history_path or _default_history_path()
    status = _read_json(status_file)
    actors = status.get("actors") if isinstance(status.get("actors"), dict) else {}
    actor = _wallbox_actor(wb_id)
    actor_state = actors.get(actor) if isinstance(actors.get(actor), dict) else {}
    event_max_age_s = max(1.0, float(start_stop_gap_s), float(phase_gap_s))
    events = _trim_events(
        actor_state.get("events") or [],
        now_ts=now_value,
        max_age_s=event_max_age_s,
    )
    cmd = command if isinstance(command, dict) else {}
    action = _wallbox_command_action(cmd, actor_state)
    candidate = None
    check = {"ok": True, "violations": [], "checks": {}}
    allowed = True
    block_reason = ""

    if action in {"START", "STOP", "1P", "3P"}:
        candidate = _candidate_event(
            wb_id=wb_id,
            actor=actor,
            action=action,
            command=cmd,
            reason=reason or str(cmd.get("reason", "")),
            now_ts=now_value,
            target_reachable=target_reachable,
        )
        check = _evaluate_wallbox_events(
            events + [candidate],
            candidate=candidate,
            start_stop_gap_s=start_stop_gap_s,
            phase_gap_s=phase_gap_s,
        )
        restart_violation = _recent_restart_violation(events, candidate, min_gap_s=start_stop_gap_s)
        if restart_violation:
            check.setdefault("violations", []).append(restart_violation)
            check["ok"] = False
        # A STOP is a safety edge. It may create a suspicious stream in the
        # shadow history, but it must never be blocked in a way that keeps a
        # real car charging.
        if action == "STOP":
            check["ok"] = True
            restart_violation = None
        if not check.get("ok", True) and _openwb_pro_phase_zero_restart_override(
            candidate,
            check.get("violations") or [],
        ):
            check["ok"] = True
            check["violations"] = []
        if not check.get("ok", True):
            allowed = False
            first = (check.get("violations") or [{}])[0]
            block_reason = "command_chatter_guard:%s" % str(first.get("type", "unknown"))

    decision = {
        "ts": round(now_value, 3),
        "service": "control_command_guard",
        "domain": "wallbox",
        "actor": actor,
        "wb_id": int(wb_id),
        "action": action or "NOOP",
        "allowed": bool(allowed),
        "decision": "allowed" if allowed else "blocked",
        "reason": str(reason or cmd.get("reason") or action or ""),
        "block_reason": block_reason,
        "target_reachable": bool(target_reachable),
        "command": _compact_command(cmd),
        "candidate_event": candidate,
        "violations": check.get("violations") or [],
    }

    if not commit:
        return decision
    return commit_wallbox_command_decision(
        decision,
        status_path=status_file,
        history_path=history_file,
        start_stop_gap_s=start_stop_gap_s,
        phase_gap_s=phase_gap_s,
    )
