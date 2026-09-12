"""Reine Phasenbeobachtung einer festen, stromgeregelten Stecksession.

Der Aufrufer besitzt Steckerkennung, Stromauftrag und Hardwareausgang. Dieses
Modul sendet nichts und öffnet kein Startbudget. Eine bestätigte Sitzung darf
bei einer Pause weniger Leistung abnehmen, ohne dadurch Phasen zu verlieren.
"""

import copy
import math


STATE_SCHEMA = "fixed_phase_session_state_v1"
CONTRACT_SCHEMA = "fixed_phase_session_v1"
ACTIVE_PHASE_POWER_W = 250.0
DEFAULT_SETTLE_S = 12.0
DEFAULT_CONFIRM_S = 8.0
DEFAULT_CONFIRM_SAMPLES = 3
DEFAULT_DISCONNECT_S = 3.0
DEFAULT_LEARNING_LIMIT_S = 30.0


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


def _clear_candidate(state):
    state["stable_6a_since_s"] = None
    state["candidate_since_s"] = None
    state["candidate_mask"] = []
    state["candidate_samples"] = 0
    state["candidate_reference_w"] = []


def update_fixed_phase_session(
    previous,
    *,
    status,
    now_s,
    session_id,
    enabled,
    connected,
    commanded_amp,
    evse_max_phases=3,
    vehicle_max_phases=0,
    sample_id=None,
    settle_s=DEFAULT_SETTLE_S,
    confirm_s=DEFAULT_CONFIRM_S,
    confirm_samples=DEFAULT_CONFIRM_SAMPLES,
    disconnect_s=DEFAULT_DISCONNECT_S,
    max_sample_gap_s=10.0,
    learning_limit_s=DEFAULT_LEARNING_LIMIT_S,
):
    """Beobachte höchstens eine neue vollständige Probe je Aufruf.

    ``now_s`` verwendet eine monotone Uhr. ``sample_id`` muss eine echte neue
    Geräteprobe kennzeichnen, niemals nur einen Managerzyklus. Ohne explizite
    ID werden nativer Probenzähler und dessen Zeitstempel verwendet.
    ``connected=None`` bedeutet unbekannt und niemals Abstecken. ``enabled``
    darf nur der feste Python-Strompfad setzen, kein autonomer oder sekundärer
    Ladepunkt. Der Manager bindet den zurückgegebenen Vertrag anschließend an
    seinen Zyklustoken und dieselbe Stecksession.
    """
    st = status if isinstance(status, dict) else {}
    sid = str(session_id or "")
    old = previous if isinstance(previous, dict) else {}
    if old.get("schema") == STATE_SCHEMA and old.get("session_id") == sid and sid:
        state = copy.deepcopy(old)
    else:
        state = {
            "schema": STATE_SCHEMA,
            "session_id": sid,
            "confirmed_phases": 0,
            "confirmed_mask": [],
            "last_sample_id": None,
            "last_sample_s": None,
            "disconnect_since_s": None,
            "disconnect_samples": 0,
            "disconnected": False,
            "measurement_conflict": False,
            "learning_started_s": None,
        }
        _clear_candidate(state)
    state["confirmed_phases"] = _phases(state.get("confirmed_phases"))
    now = _number(now_s)
    supply = _phases(evse_max_phases, 3)
    vehicle = _phases(vehicle_max_phases)
    fallback = min(supply, vehicle) if vehicle else supply
    reason = "waiting_for_complete_6a_samples"
    observed = 0

    def result():
        confirmed = _phases(state.get("confirmed_phases"))
        active = bool(enabled is True and sid and state.get("disconnected") is not True)
        command_phases = max(confirmed, observed) if confirmed else max(fallback, observed)
        learning_started = _number(state.get("learning_started_s"))
        learning_limit_active = bool(
            active and not confirmed and not vehicle and supply > 1
            and (learning_started is None or now is None or now < learning_started
                 or now - learning_started < max(0.0, float(learning_limit_s)))
        )
        contract = {
            "contract": CONTRACT_SCHEMA,
            "active": active,
            "session_id": sid,
            "confirmed": bool(confirmed),
            "phase_count": confirmed,
            "command_phase_count": command_phases,
            "evse_supply_phases": supply,
            "observed_phases": observed,
            "confirmed_mask": list(state.get("confirmed_mask") or []),
            "measurement_conflict": state.get("measurement_conflict") is True,
            "learning_current_limit_active": learning_limit_active,
            "learning_started_s": learning_started,
            "sample_id": state.get("last_sample_id"),
            "sample_s": state.get("last_sample_s"),
            "reason": reason,
        }
        return {
            "state": state,
            "phase_contract": contract,
            "reason": reason,
            "probe_required": bool(active and not confirmed and not vehicle and supply > 1),
            "command_phase_count": command_phases,
            "disconnect_confirmed": state.get("disconnected") is True,
        }

    if enabled is not True or not sid:
        _clear_candidate(state)
        reason = "fixed_current_owner_missing"
        return result()
    if now is None or now < 0.0:
        _clear_candidate(state)
        reason = "monotonic_time_invalid"
        return result()
    last_time = _number(state.get("last_sample_s"))
    if last_time is not None and now < last_time:
        _clear_candidate(state)
        state["disconnect_since_s"] = None
        state["disconnect_samples"] = 0
        state["last_sample_s"] = None
        state["last_sample_id"] = None
        reason = "monotonic_clock_changed_confirmation_preserved"
        return result()
    if sample_id is None:
        seq = _number(st.get("native_status_sample_seq"))
        ts = _number(st.get("native_status_sample_ts"))
        if seq is not None and seq > 0 and ts is not None and ts > 0:
            sample_id = "%s:%s" % (seq, ts)
    key = str(sample_id) if sample_id is not None and not isinstance(sample_id, bool) else ""
    if not _fresh(st) or not key:
        _clear_candidate(state)
        reason = "sample_missing_or_stale_confirmation_preserved"
        return result()
    if key == state.get("last_sample_id"):
        reason = "duplicate_sample_confirmation_preserved"
        return result()
    if last_time is not None and now - last_time > max(0.0, float(max_sample_gap_s)):
        # Eine Lücke belegt keine ununterbrochene Einschwing- oder Lernzeit.
        # Bereits bestätigte Phasen bleiben davon unberührt.
        _clear_candidate(state)
        state["disconnect_since_s"] = None
        state["disconnect_samples"] = 0
    state["last_sample_id"] = key
    state["last_sample_s"] = now

    if connected is False:
        _clear_candidate(state)
        if state.get("disconnect_since_s") is None:
            state["disconnect_since_s"] = now
            state["disconnect_samples"] = 0
        state["disconnect_samples"] = int(state.get("disconnect_samples", 0)) + 1
        elapsed = now - float(state["disconnect_since_s"])
        if state["disconnect_samples"] >= 2 and elapsed >= max(0.0, float(disconnect_s)):
            state["confirmed_phases"] = 0
            state["confirmed_mask"] = []
            state["measurement_conflict"] = False
            state["disconnected"] = True
            reason = "disconnect_confirmed"
        else:
            reason = "disconnect_debounce_confirmation_preserved"
        return result()
    if connected is not True:
        _clear_candidate(state)
        state["disconnect_since_s"] = None
        state["disconnect_samples"] = 0
        reason = "connection_unknown_confirmation_preserved"
        return result()
    state["disconnect_since_s"] = None
    state["disconnect_samples"] = 0
    state["disconnected"] = False

    amp = _number(commanded_amp)
    if amp is not None and abs(amp - 6.0) <= 0.25 and state.get("learning_started_s") is None:
        # Die zeitlich begrenzte Lerndrossel beginnt mit dem frischen
        # 6-A-Angebot, auch wenn der phasenweise Zähler noch nichts liefert.
        # Pause oder fehlende Einzelphasen starten keine neue Wartezeit.
        state["learning_started_s"] = now

    powers = _phase_powers(st)
    if powers is None or st.get("phase_power_verified") is not True:
        _clear_candidate(state)
        reason = "phase_measurement_incomplete_confirmation_preserved"
        return result()
    mask = [index + 1 for index, power in enumerate(powers) if power > ACTIVE_PHASE_POWER_W]
    observed = len(mask)
    confirmed = state["confirmed_phases"]
    if confirmed:
        if observed > confirmed:
            # Eine größere reale Last wird sofort konservativ berücksichtigt.
            # Eine kurze kleinere Last kann diese Grenze später nicht senken.
            state["confirmed_phases"] = observed
            state["confirmed_mask"] = mask
            state["measurement_conflict"] = True
            reason = "additional_phase_observed_raise_immediately"
        elif observed and mask != list(state.get("confirmed_mask") or []) and observed == confirmed:
            state["measurement_conflict"] = True
            reason = "phase_layout_changed_confirmation_preserved"
        else:
            reason = "session_phase_confirmation_preserved"
        return result()

    stable_power = bool(mask and all(900.0 <= powers[index - 1] <= 1800.0 for index in mask))
    if amp is None or abs(amp - 6.0) > 0.25 or not stable_power:
        _clear_candidate(state)
        reason = "waiting_for_stable_6a_load"
        return result()
    if state.get("stable_6a_since_s") is None:
        state["stable_6a_since_s"] = now
    if now - float(state["stable_6a_since_s"]) < max(0.0, float(settle_s)):
        reason = "six_amp_start_settling"
        return result()
    reference = state.get("candidate_reference_w") or []
    comparable = bool(
        state.get("candidate_mask") == mask
        and len(reference) == 3
        and all(abs(powers[index - 1] - reference[index - 1]) <= max(100.0, 0.15 * reference[index - 1]) for index in mask)
    )
    if not comparable:
        state["candidate_mask"] = mask
        state["candidate_reference_w"] = list(powers)
        state["candidate_since_s"] = now
        state["candidate_samples"] = 1
    else:
        state["candidate_samples"] = int(state.get("candidate_samples", 0)) + 1
    if (
        state["candidate_samples"] >= max(2, int(confirm_samples))
        and now - float(state["candidate_since_s"]) >= max(0.0, float(confirm_s))
    ):
        state["confirmed_phases"] = observed
        state["confirmed_mask"] = mask
        reason = "stable_6a_phase_confirmation"
    else:
        reason = "collecting_stable_6a_samples"
    return result()
