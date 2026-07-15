"""Shared safety checks for closed-loop controller command streams."""

import re
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple


DEFAULT_PROTECTION_REASON_MARKERS = (
    "emergency",
    "not_aus",
    "not-aus",
    "manual",
    "user",
    "hard",
    "protection",
    "schutz",
    "grid",
    "netz",
    "import",
    "house_fuse",
    "budget_timeout",
    "stale",
    "fault",
    "error",
    "no_vehicle",
    "kein fahrzeug",
    "disconnected",
    "vehicle_done",
    "charge_done",
    "target_reached",
    "zieltemperatur",
    "high_pressure",
    "hochdruck",
    "compressor",
    "verdichter",
    "target_unreachable",
    "owner_override",
    "price",
    "slot_end",
    "planned_end",
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).strip() if value is not None else ""
        return float(text) if text else float(default)
    except (TypeError, ValueError):
        return float(default)


def _event_actor(event: Dict[str, Any], actor_key: str) -> str:
    return str(event.get(actor_key, event.get("id", "default")) or "default")


def _event_ts(event: Dict[str, Any], ts_key: str) -> float:
    return _safe_float(event.get(ts_key, event.get("time", event.get("timestamp", 0.0))), 0.0)


def _event_action(event: Dict[str, Any], action_key: str) -> str:
    return str(event.get(action_key, "") or "").strip().upper()


def _event_reason(event: Dict[str, Any], reason_keys: Sequence[str]) -> str:
    return " ".join(str(event.get(key, "") or "").lower() for key in reason_keys)


def _event_target_reachable(event: Dict[str, Any], reachable_key: str) -> bool:
    if reachable_key not in event:
        return True
    return str(event.get(reachable_key, "")).strip().lower() not in ("0", "false", "no", "nein")


def _has_protection_reason(
    event: Dict[str, Any],
    reason_keys: Sequence[str],
    markers: Iterable[str],
) -> bool:
    reason = _event_reason(event, reason_keys)
    reason_words = re.sub(r"\s+", " ", reason).strip()
    for marker in markers:
        marker_text = str(marker).strip().lower()
        if not marker_text:
            continue
        if " " in marker_text:
            if marker_text in reason_words:
                return True
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(marker_text)}(?![a-z0-9])", reason):
            return True
    return False


def detect_command_chatter(
    events: Iterable[Dict[str, Any]],
    *,
    unsafe_patterns: Iterable[Tuple[str, str, str]],
    min_gap_s: int,
    actor_key: str = "actor",
    action_key: str = "action",
    ts_key: str = "ts",
    reason_keys: Sequence[str] = ("reason", "owner", "protection_reason"),
    protection_reason_markers: Optional[Iterable[str]] = None,
    reachable_key: str = "target_reachable",
) -> Dict[str, Any]:
    """Detect three-step ping-pong patterns without a hard protection reason."""

    markers = tuple(protection_reason_markers or DEFAULT_PROTECTION_REASON_MARKERS)
    normalized_patterns = {
        tuple(str(part).strip().upper() for part in pattern)
        for pattern in unsafe_patterns
    }
    pattern_names = {"_".join(pattern).lower(): 0 for pattern in normalized_patterns}
    timeline = sorted(
        [event for event in events if isinstance(event, dict)],
        key=lambda event: (_event_actor(event, actor_key), _event_ts(event, ts_key)),
    )
    recent: Dict[str, list] = {}
    violations = []

    def protected_or_unreachable(window: Iterable[Dict[str, Any]]) -> bool:
        return any(_has_protection_reason(event, reason_keys, markers) for event in window) or not all(
            _event_target_reachable(event, reachable_key) for event in window
        )

    for event in timeline:
        action = _event_action(event, action_key)
        if not action:
            continue
        actor = _event_actor(event, actor_key)
        history = recent.setdefault(actor, [])
        history.append({"event": event, "action": action, "ts": _event_ts(event, ts_key)})
        del history[:-3]
        if len(history) != 3:
            continue

        pattern = tuple(item["action"] for item in history)
        if pattern not in normalized_patterns:
            continue
        age_s = history[-1]["ts"] - history[0]["ts"]
        window = [item["event"] for item in history]
        if age_s >= min_gap_s or protected_or_unreachable(window):
            continue

        name = "_".join(pattern).lower()
        pattern_names[name] += 1
        violations.append({
            "type": name,
            "actor": actor,
            "age_s": int(round(age_s)),
            "events": window,
        })

    return {"ok": not violations, "violations": violations, "counts": pattern_names}


def detect_state_chatter(
    events: Iterable[Dict[str, Any]],
    *,
    min_gap_s: int,
    unsafe_patterns: Optional[Iterable[Sequence[str]]] = None,
    required_actions_any: Optional[Iterable[str]] = None,
    actor_key: str = "actor",
    action_key: str = "action",
    ts_key: str = "ts",
    reason_keys: Sequence[str] = ("reason", "owner", "protection_reason"),
    protection_reason_markers: Optional[Iterable[str]] = None,
    reachable_key: str = "target_reachable",
) -> Dict[str, Any]:
    """Detect A-B-A owner/state chatter even when hardware commands stay stable."""

    markers = tuple(protection_reason_markers or DEFAULT_PROTECTION_REASON_MARKERS)
    normalized_patterns = {
        tuple(str(part).upper() for part in pattern)
        for pattern in (unsafe_patterns or [])
    }
    required_actions = {str(action).upper() for action in (required_actions_any or [])}
    timeline = sorted(
        [event for event in events if isinstance(event, dict)],
        key=lambda event: (_event_actor(event, actor_key), _event_ts(event, ts_key)),
    )
    recent: Dict[str, list] = {}
    violations = []
    counts: Dict[str, int] = {}

    def protected_or_unreachable(window: Iterable[Dict[str, Any]]) -> bool:
        return any(_has_protection_reason(event, reason_keys, markers) for event in window) or not all(
            _event_target_reachable(event, reachable_key) for event in window
        )

    for event in timeline:
        action = _event_action(event, action_key)
        if not action:
            continue
        actor = _event_actor(event, actor_key)
        history = recent.setdefault(actor, [])
        history.append({"event": event, "action": action, "ts": _event_ts(event, ts_key)})
        del history[:-3]
        if len(history) != 3:
            continue

        pattern = tuple(item["action"] for item in history)
        if not (pattern[0] == pattern[2] and pattern[0] != pattern[1]):
            continue
        if normalized_patterns and pattern not in normalized_patterns:
            continue
        if required_actions and not required_actions.intersection(pattern):
            continue
        age_s = history[-1]["ts"] - history[0]["ts"]
        window = [item["event"] for item in history]
        if age_s >= min_gap_s or protected_or_unreachable(window):
            continue

        name = "_".join(pattern).lower()
        counts[name] = counts.get(name, 0) + 1
        violations.append({
            "type": name,
            "actor": actor,
            "age_s": int(round(age_s)),
            "events": window,
        })

    return {"ok": not violations, "violations": violations, "counts": counts}
