"""Pure go-e Charger session contract.

The go-e HTTP API is level-triggered: ``amp`` and ``frc`` describe the desired
device state, while real charging must still be proven by measured power.  This
module keeps the manager-owned start/stop offer visible without doing network or
filesystem work.
"""

from typing import Any, Dict, Optional

from .decision import status_connected, status_real_charging, status_real_power


STATE_IDLE = "idle"
STATE_OFFERED = "offered"
STATE_STARTING = "starting"
STATE_CHARGING = "charging"
STATE_STOPPING = "stopping"
STATE_ENDED = "ended"

CONTRACT_NAME = "goe_http_level_state"

_STATE_LABELS = {
    STATE_IDLE: ("Idle", "secondary"),
    STATE_OFFERED: ("Startfreigabe", "warning"),
    STATE_STARTING: ("Start wartet", "warning"),
    STATE_CHARGING: ("Lade", "success"),
    STATE_STOPPING: ("Stoppt", "warning"),
    STATE_ENDED: ("Ladung beendet", "secondary"),
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).strip() if value is not None else ""
        return float(text) if text else float(default)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_safe_float(value, default)))
    except (TypeError, ValueError):
        return int(default)


def _state_label(state: str) -> str:
    return _STATE_LABELS.get(state, _STATE_LABELS[STATE_IDLE])[0]


def _state_level(state: str) -> str:
    return _STATE_LABELS.get(state, _STATE_LABELS[STATE_IDLE])[1]


def is_goe_charger(charger: Any) -> bool:
    """Return true for the official go-e HTTP driver."""

    return bool(charger is not None and charger.__class__.__name__ == "GoECharger")


def evaluate_session(
    status: Optional[Dict[str, Any]],
    *,
    current_set_amp: Any = 0,
    cap_amp: Any = 0,
    min_amp: Any = 6,
    budget_ready: bool = False,
    grid_allowed: bool = False,
    price_active: bool = False,
    price_boost_active: bool = False,
    predump_active: bool = False,
    mode_off: bool = False,
    priority_forced_stop: bool = False,
    stop_sent_active: bool = False,
    ended_latched: bool = False,
    end_reason: str = "",
    last_start_ts: Any = 0,
    now_ts: Any = 0,
    start_verify_s: Any = 180,
    stable_hw_power_w: Any = 0.0,
) -> Dict[str, Any]:
    """Classify one manager-owned go-e session."""

    st = status or {}
    now = _safe_float(now_ts, 0.0)
    min_current = max(1, _safe_int(min_amp, 6))
    current_amp = max(0, _safe_int(current_set_amp, 0))
    cap = max(0, _safe_int(cap_amp, 0))
    hw_amp = max(0, _safe_int(st.get("amp", 0), 0))
    frc = _safe_int(st.get("frc", 0), 0)
    offered_amp = max(current_amp, cap, hw_amp if hw_amp >= min_current and frc != 1 else 0)
    connected = status_connected(st)
    real_power_w = max(status_real_power(st), _safe_float(stable_hw_power_w, 0.0))
    real_charging = bool(status_real_charging(st) or real_power_w > 500.0)
    last_start = _safe_float(last_start_ts, 0.0)
    verify_s = max(0.0, _safe_float(start_verify_s, 180.0))
    last_start_age_s = max(0.0, now - last_start) if now > 0.0 and last_start > 0.0 else None
    start_requested = bool(
        connected
        and not mode_off
        and frc != 1
        and offered_amp >= min_current
        and (current_amp >= min_current or cap >= min_current or frc == 2)
    )
    physical_budget_ready = bool(
        budget_ready
        or grid_allowed
        or price_active
        or price_boost_active
        or predump_active
    )
    start_verifying = bool(
        start_requested
        and (
            frc == 2
            or (
                last_start_age_s is not None
                and last_start_age_s <= verify_s
            )
        )
    )
    frc_stop = bool(frc == 1)
    stop_active = bool(
        stop_sent_active
        or (
            frc_stop
            and (
                real_charging
                or real_power_w > 50.0
                or current_amp >= min_current
                or cap >= min_current
            )
        )
    )

    if not connected:
        state = STATE_IDLE
        reason = "Kein Fahrzeug verbunden."
    elif real_charging:
        state = STATE_CHARGING
        reason = "Echte Ladung mit %.0f W bestätigt." % real_power_w
    elif ended_latched:
        state = STATE_ENDED
        reason = (
            "Ladeende ist gelatcht; Neustart erst nach Umstecken, Moduswechsel "
            "oder neuer Nutzerfreigabe."
        )
    elif stop_active or priority_forced_stop:
        state = STATE_STOPPING
        reason = "go-e Stop ist aktiv; es wird keine neue Freigabe gesendet."
    elif start_verifying:
        state = STATE_STARTING
        reason = "%d A/FRC=%d freigegeben; go-e wartet auf echte Ladeleistung." % (offered_amp, frc)
    elif start_requested:
        state = STATE_OFFERED
        reason = "%d A freigegeben; noch keine echte Ladebestätigung." % offered_amp
    else:
        state = STATE_IDLE
        reason = "Fahrzeug verbunden; keine Startfreigabe aktiv."

    start_blocked = bool(state in (STATE_STOPPING, STATE_ENDED) or mode_off or priority_forced_stop or frc_stop)
    can_send_start_command = bool(
        state in (STATE_OFFERED, STATE_STARTING)
        and physical_budget_ready
        and not start_blocked
        and offered_amp >= min_current
    )
    if end_reason:
        reason = "%s (%s)" % (reason, end_reason)

    return {
        "contract": CONTRACT_NAME,
        "state": state,
        "label": _state_label(state),
        "level": _state_level(state),
        "reason": reason,
        "connected": bool(connected),
        "real_charging": bool(real_charging),
        "real_power_w": float(real_power_w if real_charging else 0.0),
        "offered_amp": int(offered_amp),
        "current_set_amp": int(current_amp),
        "cap_amp": int(cap),
        "hardware_amp": int(hw_amp),
        "frc": int(frc),
        "budget_ready": bool(physical_budget_ready),
        "start_requested": bool(start_requested),
        "start_verifying": bool(start_verifying),
        "stop_active": bool(stop_active),
        "last_start_age_s": last_start_age_s,
        "start_blocked": bool(start_blocked),
        "can_send_start_command": bool(can_send_start_command),
        "counts_as_real_charge": bool(real_charging),
    }


def apply_session_to_status(status: Optional[Dict[str, Any]], session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Attach go-e session diagnostics to a status dict in-place."""

    if status is None:
        return None
    status["goe_contract"] = session.get("contract", CONTRACT_NAME)
    status["goe_runtime_path"] = "python_wallbox_manager"
    status["goe_session_guard_required"] = True
    status["goe_charge_verification_required"] = True
    status["goe_session_state"] = session.get("state", STATE_IDLE)
    status["goe_session_label"] = session.get("label", _state_label(STATE_IDLE))
    status["goe_session_level"] = session.get("level", _state_level(STATE_IDLE))
    status["goe_session_reason"] = session.get("reason", "")
    status["goe_session_offered_amp"] = int(session.get("offered_amp", 0) or 0)
    status["goe_session_current_set_amp"] = int(session.get("current_set_amp", 0) or 0)
    status["goe_session_cap_amp"] = int(session.get("cap_amp", 0) or 0)
    status["goe_session_hardware_amp"] = int(session.get("hardware_amp", 0) or 0)
    status["goe_session_frc"] = int(session.get("frc", 0) or 0)
    status["goe_session_budget_ready"] = bool(session.get("budget_ready", False))
    status["goe_session_start_requested"] = bool(session.get("start_requested", False))
    status["goe_session_start_verifying"] = bool(session.get("start_verifying", False))
    status["goe_session_stop_active"] = bool(session.get("stop_active", False))
    status["goe_session_start_blocked"] = bool(session.get("start_blocked", False))
    status["goe_session_can_send_start_command"] = bool(session.get("can_send_start_command", False))
    status["goe_session_counts_as_real_charge"] = bool(session.get("counts_as_real_charge", False))
    return status
