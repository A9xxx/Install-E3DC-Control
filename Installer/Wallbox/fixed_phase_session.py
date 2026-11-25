"""Flüchtige Phasenbeobachtung einer festen Stecksession ohne Hardwareausgang."""

import math

STATE_SCHEMA = "fixed_phase_session_state_v1"
CONTRACT_SCHEMA = "fixed_phase_session_v1"
ACTIVE_PHASE_POWER_W = 250.0

def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None

def _phases(value, default=0):
    number = _number(value)
    return int(number) if number in (1, 2, 3) else default

def _fresh(status):
    return bool(
        status.get("driver_status_valid") is True
        and status.get("driver_status_stale") is not True
        and status.get("driver_status_degraded") is not True
        and status.get("driver_status_glitch") is not True
        and status.get("driver_status_plausible") is not False
        and status.get("valid") is not False
        and status.get("stale") is not True
    )

def _phase_powers(status):
    values = tuple(_number(status.get("phase_power_l%d_w" % phase)) for phase in (1, 2, 3))
    if any(value is None or value < 0.0 or value > 50000.0 for value in values):
        return None
    if status.get("phase_power_sample_valid") is False:
        return None
    if status.get("phase_power_rscp_type_complete") is False:
        return None
    return values


def update_fixed_phase_session(
    previous, *, status, now_s, session_id,
    vehicle_max_phases=0, sample_id=None,
):
    """Halte das Maximum vollständiger frischer Messungen bis zum Abstecken.

    Der bestehende Steckepisoden-Beobachter bestätigt die physische Trennung.
    Nullleistung, fehlender Status und Ladepausen löschen keinen Messbeleg.
    """
    st = status if isinstance(status, dict) else {}
    sid = str(session_id or "")
    old = previous if isinstance(previous, dict) else {}
    state = dict(old) if old.get("schema") == STATE_SCHEMA and old.get("session_id") == sid else {
        "schema": STATE_SCHEMA, "session_id": sid, "confirmed_phases": 0,
        "last_sample_id": None,
    }
    reason = "waiting_for_phase_measurement"
    if sid and _fresh(st):
        now = _number(now_s)
        powers = _phase_powers(st)
        if (now is not None and now >= 0 and sample_id is not None
                and str(sample_id) != state.get("last_sample_id")
                and powers is not None and st.get("phase_power_verified") is True):
            state.update(last_sample_id=str(sample_id))
            mask = [i + 1 for i, power in enumerate(powers) if power > ACTIVE_PHASE_POWER_W]
            if len(mask) > _phases(state.get("confirmed_phases")):
                state.update(confirmed_phases=len(mask))
                reason = "session_phase_measurement"
            else:
                reason = "session_phase_confirmation_preserved"
    confirmed = _phases(state.get("confirmed_phases"))
    vehicle = _phases(vehicle_max_phases)
    phases = confirmed or vehicle or 3
    contract = {
        "contract": CONTRACT_SCHEMA, "active": bool(sid), "session_id": sid,
        "confirmed": bool(confirmed), "phase_count": confirmed,
        "command_phase_count": phases,
        "reason": reason,
    }
    return {"state": state, "phase_contract": contract, "reason": reason,
            "command_phase_count": phases}
