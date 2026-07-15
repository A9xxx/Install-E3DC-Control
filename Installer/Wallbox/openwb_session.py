"""Pure normal openWB Secondary session contract.

The regular openWB path is intentionally narrow: E3DC-Control publishes a
Secondary set-current value plus heartbeat, while openWB keeps its own PV,
target and phase logic.  Real charging is still proven by measured power, not
by the offered current.
"""

from typing import Any, Dict, Optional

from .decision import status_connected, status_real_charging, status_real_power


STATE_IDLE = "idle"
STATE_OFFERED = "offered"
STATE_STARTING = "starting"
STATE_CHARGING = "charging"
STATE_STOPPING = "stopping"
STATE_ENDED = "ended"

CONTRACT_NAME = "openwb_secondary_current_heartbeat"
SECONDARY_HEARTBEAT_INTERVAL_S = 30.0
SECONDARY_HEARTBEAT_RETRY_S = 5.0

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


def is_openwb_charger(charger: Any) -> bool:
    """Return true for the regular openWB driver."""

    return bool(charger is not None and charger.__class__.__name__ == "OpenWBCharger")


def secondary_heartbeat_refresh_due(
    *,
    last_success_ts: Any = 0,
    last_attempt_ts: Any = 0,
    now_ts: Any = 0,
    interval_s: Any = SECONDARY_HEARTBEAT_INTERVAL_S,
    retry_s: Any = SECONDARY_HEARTBEAT_RETRY_S,
) -> bool:
    """Return whether the official Secondary heartbeat must be refreshed.

    openWB requires a current Unix timestamp at least every 60 seconds. The
    manager deliberately uses a 30-second success window and retries failed
    attempts after five seconds. A backwards clock jump is treated as due so
    that a stale future timestamp cannot suppress the safety heartbeat.
    """

    now = _safe_float(now_ts, 0.0)
    last_success = _safe_float(last_success_ts, 0.0)
    last_attempt = _safe_float(last_attempt_ts, 0.0)
    interval = max(1.0, min(30.0, _safe_float(interval_s, SECONDARY_HEARTBEAT_INTERVAL_S)))
    retry = max(1.0, min(interval, _safe_float(retry_s, SECONDARY_HEARTBEAT_RETRY_S)))
    success_age = now - last_success if last_success > 0.0 else None
    attempt_age = now - last_attempt if last_attempt > 0.0 else None
    success_due = bool(
        success_age is None
        or success_age < 0.0
        or success_age >= interval
    )
    retry_due = bool(
        attempt_age is None
        or attempt_age < 0.0
        or attempt_age >= retry
    )
    return bool(success_due and retry_due)


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
    primary_delegate: bool = False,
    last_start_ts: Any = 0,
    now_ts: Any = 0,
    start_verify_s: Any = 180,
    stable_hw_power_w: Any = 0.0,
) -> Dict[str, Any]:
    """Classify one manager-owned regular openWB Secondary session."""

    st = status or {}
    api_surface = str(st.get("api_surface") or "")
    primary_mode = bool(primary_delegate or api_surface.startswith("openwb_primary"))
    secondary_active = not primary_mode
    now = _safe_float(now_ts, 0.0)
    min_current = max(1, _safe_int(min_amp, 6))
    current_amp = max(0, _safe_int(current_set_amp, 0))
    cap = max(0, _safe_int(cap_amp, 0))
    hw_amp = max(
        0,
        _safe_int(
            st.get("amp", st.get("evse_current", 0)),
            0,
        ),
    )
    command_amp = max(0, _safe_int(st.get("last_command_amp", 0), 0))
    offered_amp = max(current_amp, cap, command_amp, hw_amp if secondary_active else 0)
    connected = status_connected(st)
    real_power_w = max(status_real_power(st), _safe_float(stable_hw_power_w, 0.0))
    real_charging = bool(status_real_charging(st) or real_power_w > 500.0)
    last_start = _safe_float(last_start_ts, 0.0)
    verify_s = max(0.0, _safe_float(start_verify_s, 180.0))
    last_start_age_s = max(0.0, now - last_start) if now > 0.0 and last_start > 0.0 else None
    heartbeat_ok = st.get("last_heartbeat_ok")
    command_ok = st.get("last_command_ok")
    command_blocked = bool(st.get("command_blocked", False))
    start_requested = bool(
        connected
        and secondary_active
        and not mode_off
        and offered_amp >= min_current
        and (current_amp >= min_current or cap >= min_current or command_amp >= min_current or hw_amp >= min_current)
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
            (
                last_start_age_s is not None
                and last_start_age_s <= verify_s
            )
            or command_amp >= min_current
        )
    )
    zero_command_active = bool(
        secondary_active
        and command_ok is True
        and command_amp <= 0
        and current_amp <= 0
        and cap <= 0
    )
    stop_active = bool(stop_sent_active or zero_command_active)

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
    elif primary_mode:
        state = STATE_IDLE
        reason = "openWB Primary führt Ladepunkt und Phasen; E3DC-Control greift nicht per Secondary-Sollstrom ein."
    elif stop_active or priority_forced_stop:
        state = STATE_STOPPING
        reason = "openWB Secondary Stop/0A ist aktiv; es wird keine neue Freigabe gesendet."
    elif command_blocked:
        state = STATE_IDLE
        reason = "openWB Secondary blockiert weitere Befehle nach wiederholten Fehlern."
    elif start_verifying:
        state = STATE_STARTING
        reason = "%d A plus Heartbeat freigegeben; openWB Secondary wartet auf echte Ladeleistung." % offered_amp
    elif start_requested:
        state = STATE_OFFERED
        reason = "%d A plus Heartbeat freigegeben; noch keine echte Ladebestätigung." % offered_amp
    else:
        state = STATE_IDLE
        reason = "Fahrzeug verbunden; keine Secondary-Startfreigabe aktiv."

    if heartbeat_ok is False and state in (STATE_OFFERED, STATE_STARTING):
        reason = "%s Heartbeat wurde zuletzt nicht bestätigt." % reason
    if command_ok is False and state in (STATE_OFFERED, STATE_STARTING):
        reason = "%s Sollstrom wurde zuletzt nicht bestätigt." % reason

    start_blocked = bool(
        state in (STATE_STOPPING, STATE_ENDED)
        or mode_off
        or priority_forced_stop
        or primary_mode
        or command_blocked
    )
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
        "last_command_amp": int(command_amp),
        "heartbeat_ok": None if heartbeat_ok is None else bool(heartbeat_ok),
        "command_ok": None if command_ok is None else bool(command_ok),
        "command_blocked": bool(command_blocked),
        "api_surface": api_surface,
        "secondary_active": bool(secondary_active),
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
    """Attach regular openWB session diagnostics to a status dict in-place."""

    if status is None:
        return None
    status["openwb_secondary_contract"] = session.get("contract", CONTRACT_NAME)
    status["openwb_secondary_runtime_path"] = "python_wallbox_manager"
    status["openwb_secondary_session_guard_required"] = True
    status["openwb_secondary_charge_verification_required"] = True
    status["openwb_secondary_session_state"] = session.get("state", STATE_IDLE)
    status["openwb_secondary_session_label"] = session.get("label", _state_label(STATE_IDLE))
    status["openwb_secondary_session_level"] = session.get("level", _state_level(STATE_IDLE))
    status["openwb_secondary_session_reason"] = session.get("reason", "")
    status["openwb_secondary_session_offered_amp"] = int(session.get("offered_amp", 0) or 0)
    status["openwb_secondary_session_current_set_amp"] = int(session.get("current_set_amp", 0) or 0)
    status["openwb_secondary_session_cap_amp"] = int(session.get("cap_amp", 0) or 0)
    status["openwb_secondary_session_hardware_amp"] = int(session.get("hardware_amp", 0) or 0)
    status["openwb_secondary_session_last_command_amp"] = int(session.get("last_command_amp", 0) or 0)
    status["openwb_secondary_session_heartbeat_ok"] = session.get("heartbeat_ok")
    status["openwb_secondary_session_command_ok"] = session.get("command_ok")
    status["openwb_secondary_session_command_blocked"] = bool(session.get("command_blocked", False))
    status["openwb_secondary_session_api_surface"] = session.get("api_surface", "")
    status["openwb_secondary_session_secondary_active"] = bool(session.get("secondary_active", False))
    status["openwb_secondary_session_budget_ready"] = bool(session.get("budget_ready", False))
    status["openwb_secondary_session_start_requested"] = bool(session.get("start_requested", False))
    status["openwb_secondary_session_start_verifying"] = bool(session.get("start_verifying", False))
    status["openwb_secondary_session_stop_active"] = bool(session.get("stop_active", False))
    status["openwb_secondary_session_start_blocked"] = bool(session.get("start_blocked", False))
    status["openwb_secondary_session_can_send_start_command"] = bool(session.get("can_send_start_command", False))
    status["openwb_secondary_session_counts_as_real_charge"] = bool(session.get("counts_as_real_charge", False))
    return status
