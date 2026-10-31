"""Pure E3DC-native wallbox session state machine.

The E3DC wallbox RSCP API is edge-triggered: current limits are persistent
inputs, but start/stop are toggle impulses.  This module keeps that distinction
visible and testable without touching drivers, files or network state.
"""

from typing import Any, Dict, Optional

from .decision import status_connected, status_real_charging, status_real_power


_NATIVE_E3DC_STOP_EFFECT_WINDOW_S = 30.0

STATE_IDLE = "idle"
STATE_OFFERED = "offered"
STATE_STARTING = "starting"
STATE_CHARGING = "charging"
STATE_STOPPING = "stopping"
STATE_ENDED = "ended"
STATE_RSCP_ERROR = "rscp_error"

_EDGE_METHODS = {
    "set_amp_sonnenmodus",
    "set_amp_autonomous_solar",
    "set_amp_and_state",
    "set_current",
}

_STATE_LABELS = {
    STATE_IDLE: ("Idle", "secondary"),
    STATE_OFFERED: ("Startfreigabe", "warning"),
    STATE_STARTING: ("Start wartet", "warning"),
    STATE_CHARGING: ("Lade", "success"),
    STATE_STOPPING: ("Stoppt", "warning"),
    STATE_ENDED: ("Ladung beendet", "secondary"),
    STATE_RSCP_ERROR: ("RSCP Fehler", "danger"),
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


def _normalize_force_state(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        text = str(value).strip().lower()
        if text in ("", "none", "null"):
            return None
        return int(round(float(text)))
    except (TypeError, ValueError):
        return None


def _state_label(state: str) -> str:
    return _STATE_LABELS.get(state, _STATE_LABELS[STATE_IDLE])[0]


def _state_level(state: str) -> str:
    return _STATE_LABELS.get(state, _STATE_LABELS[STATE_IDLE])[1]


def is_e3dc_native_charger(charger: Any) -> bool:
    """Return true for E3DC-native drivers, including Multi Connect."""

    return bool(
        charger is not None
        and hasattr(charger, "set_amp_sonnenmodus")
        and not hasattr(charger, "set_pv_mode")
    )


def native_edge_contract(sample, state, now, session_id, *, charge_proof=False, power_w=None):
    """Ein Flankentor für Bitregel und den Rückfall ohne Bit.

    Reihenfolge kommt vom Treiber, Fristen sind monoton. Fahrzeugleistung
    bestätigt niemals eine Umschaltflanke. Sie dient nur dem freigegebenen
    Rückfall und der Wirkungsüberwachung nach bestätigtem Abbruch.
    Die Steckepisode wird zur Laufzeit nie geleert, sondern nur rotiert.
    Daher übernimmt die erste Kennung den Zustand des leeren Startfensters.
    """
    if state.get('session_id') != session_id:
        if state.get('session_id') and session_id:
            state.clear()
        state['session_id'] = session_id
    fresh = sample.get('abort_fresh') is True
    value = sample.get('abort_active') if fresh else None
    request = sample.get('abort_request_seq', 0)
    event = state.get('event') or {}
    if request != state.get('request_seq') and fresh:
        previous = state.get('previous_abort')
        state['request_seq'] = request
        state['unknown_samples'] = state.get('unknown_samples', 0) + 1 if value is None else 0
        if previous is True and value is False and state.get('own_stop_confirmed'):
            state['restart_seen'] = True
        if type(value) is bool:
            state.pop('fallback_logged', None)
            if previous is not value:
                state.pop('error', None)
                state.pop('error_logged', None)
            state['previous_abort'] = value
    confirmed = bool(fresh and value is True)
    if confirmed and event.get('action') == 'stop':
        state['own_stop_confirmed'] = True
        state['restart_seen'] = False
        state.setdefault('confirmed_mono', now)
    if confirmed and power_w is not None and power_w > 500:
        state.setdefault('high_power_since', now)
        if now - state['high_power_since'] > _NATIVE_E3DC_STOP_EFFECT_WINDOW_S:
            state['error'] = 'native_disabled_charging'
    else:
        state.pop('high_power_since', None)
        if confirmed and power_w is not None and power_w <= 500 and state.get('error') == 'native_disabled_charging':
            state.pop('error', None)
            state.pop('error_logged', None)
    if (fresh and value is True and event.get('action') == 'start'
            and state.get('error') != 'native_disabled_charging'
            and sample.get('abort_request_mono', now) - event['mono'] >= _NATIVE_E3DC_STOP_EFFECT_WINDOW_S):
        state['error'] = 'native_start_ineffective'
    allowed = False
    fallback = bool(fresh and value is None and state.get('unknown_samples', 0) > 1)
    if fresh and value is False:
        if event.get('action') != 'stop':
            allowed = True
        elif state.get('restart_seen'):
            allowed = now - event['mono'] >= _NATIVE_E3DC_STOP_EFFECT_WINDOW_S
        elif not state.get('own_stop_confirmed'):
            after = sample.get('abort_request_mono', now) - event['mono']
            # Eine verlorene Antwort ist kein Beweis einer verlorenen Flanke.
            # Erst eine danach angefragte Bitprobe nach der Mindestwartezeit
            # erlaubt die einzige Wiederholung.
            allowed = bool(not event.get('fallback') and not event.get('wire_receipt') and event.get('attempts', 1) < 2 and after >= _NATIVE_E3DC_STOP_EFFECT_WINDOW_S)
            if not allowed and after >= _NATIVE_E3DC_STOP_EFFECT_WINDOW_S and (event.get('wire_receipt') or event.get('attempts', 1) >= 2):
                state['error'] = 'native_stop_ineffective'
    elif fallback and charge_proof and event.get('action') != 'stop':
        allowed = True
    return dict(abort_active=value, abort_fresh=fresh, abort_stop_allowed=allowed,
                abort_stop_confirmed=confirmed, abort_fallback=fallback,
                abort_restart_seen=bool(state.get('restart_seen')),
                abort_error=state.get('error'), abort_start_allowed=bool(fresh and value is True))


def note_native_edge(state, *, action, now, wire_receipt, edge_seq, fallback=False):
    """Auch ein mehrdeutiger Ausgang gehört zum laufenden Ereignis."""
    if action == 'stop' and state.get('error') == 'native_stop_ineffective':
        state['user_stop_consumed'] = True
    previous = state.get('event') or {}
    attempts = previous.get('attempts', 0) if action == 'stop' and not state.get('restart_seen') and previous.get('action') == 'stop' else 0
    state['event'] = dict(action=action, mono=now, wire_receipt=bool(wire_receipt),
                          edge_seq=edge_seq, attempts=attempts + 1, fallback=bool(fallback))
    state.pop('confirmed_mono', None)
    state.pop('high_power_since', None)
    state['own_stop_confirmed'] = False
    state['restart_seen'] = False


def guard_edge_command(
    command: Optional[Dict[str, Any]],
    session: Optional[Dict[str, Any]] = None,
    *,
    hard_stop_allowed: bool = False,
    verified_active_charge: bool = False,
    abort_active: Optional[bool] = None,
    abort_fresh: bool = False,
    min_amp: Any = 6,
) -> Dict[str, Any]:
    """Return an E3DC-safe command plus a decision for edge-triggered writes.

    ``force_state`` is not an absolute state on E3DC-native wallboxes.  It is a
    toggle impulse.  This guard makes accidental legacy edges harmless while
    still allowing normal current-limit updates.
    """

    original = dict(command or {})
    guarded = dict(original)
    method = str(guarded.get("method") or guarded.get("kind") or "").strip()
    force_state = _normalize_force_state(guarded.get("force_state"))
    min_current = max(1, _safe_int(min_amp, 6))
    amp = _safe_int(guarded.get("amp", guarded.get("max_amp", min_current)), min_current)
    session_data = session if isinstance(session, dict) else {}
    state = str(session_data.get("state") or "")
    start_blocked = bool(session_data.get("start_blocked", False))
    can_start = bool(session_data.get("can_send_start_toggle", False))
    has_session = bool(session_data)
    decision = {
        "method": method,
        "action": "unchanged",
        "execute": True,
        "reason": "",
        "session_state": state,
        "original_amp": amp,
        "amp": amp,
        "original_force_state": force_state,
        "force_state": force_state,
        "hard_stop_allowed": bool(hard_stop_allowed),
        "verified_active_charge": bool(verified_active_charge),
    }

    if method not in _EDGE_METHODS:
        return {"command": guarded, "decision": decision}

    if force_state == 2:
        if abort_fresh and abort_active is not None:
            if start_blocked:
                guarded['force_state'] = None
                decision.update(action='start_toggle_blocked', execute=False,
                                reason='session_start_blocked', force_state=None)
            elif abort_active is True:
                decision['action'] = 'start_toggle_allowed'
            else:
                # Eine bereits freigegebene Box braucht nur das normale,
                # budgetgeprüfte Stromangebot und keine Umschaltflanke.
                guarded['force_state'] = None
                decision.update(action='start_toggle_downgraded',
                                reason='box_already_released', force_state=None)
            return {"command": guarded, "decision": decision}
        if start_blocked:
            guarded["force_state"] = None
            decision.update({
                "action": "start_toggle_blocked",
                "execute": False,
                "reason": "session_start_blocked",
                "force_state": None,
            })
        elif has_session and not can_start:
            if amp <= 0:
                guarded["amp"] = min_current
            guarded["force_state"] = None
            decision.update({
                "action": "start_toggle_downgraded",
                "reason": "session_not_offered",
                "amp": int(guarded.get("amp", min_current) or min_current),
                "force_state": None,
            })
        else:
            decision["action"] = "start_toggle_allowed"
        return {"command": guarded, "decision": decision}

    if force_state == 1:
        if hard_stop_allowed:
            decision["action"] = "stop_toggle_allowed"
        else:
            guarded["amp"] = min_current
            guarded["force_state"] = None
            decision.update({
                "action": "stop_toggle_downgraded",
                "reason": "native_abort_state_not_released",
                "amp": min_current,
                "force_state": None,
            })
        return {"command": guarded, "decision": decision}

    if force_state is None and amp <= 0:
        guarded["amp"] = min_current
        decision.update({
            "action": "zero_current_downgraded",
            "reason": "zero_current_is_stop_path",
            "amp": min_current,
            "force_state": None,
        })

    return {"command": guarded, "decision": decision}


def evaluate_session(
    status: Optional[Dict[str, Any]],
    *,
    current_set_amp: Any = 0,
    cap_amp: Any = 0,
    min_amp: Any = 6,
    budget_ready: bool = False,
    switch_to_1p_ready: bool = False,
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
) -> Dict[str, Any]:
    """Classify one E3DC-native wallbox session.

    The returned state is diagnostic and control-supporting.  It never turns an
    offered current into measured charging power; only RSCP bits or verified
    phase power may do that through ``status_real_charging``.
    """

    st = status or {}
    now = _safe_float(now_ts, 0.0)
    min_current = max(1, _safe_int(min_amp, 6))
    current_amp = max(0, _safe_int(current_set_amp, 0))
    cap = max(0, _safe_int(cap_amp, 0))
    hw_amp = max(0, _safe_int(st.get("amp", 0), 0))
    offered_amp = max(current_amp, cap, hw_amp if hw_amp >= min_current else 0)
    connected = status_connected(st)
    real_power_w = status_real_power(st)
    real_charging = status_real_charging(st)
    rscp_error = bool(st.get("rscp_error_active", False))
    last_start = _safe_float(last_start_ts, 0.0)
    verify_s = max(0.0, _safe_float(start_verify_s, 180.0))
    last_start_age_s = max(0.0, now - last_start) if now > 0.0 and last_start > 0.0 else None
    start_requested = bool(
        connected
        and not mode_off
        and offered_amp >= min_current
        and (current_amp >= min_current or cap >= min_current)
    )
    physical_budget_ready = bool(
        budget_ready
        or switch_to_1p_ready
        or grid_allowed
        or price_active
        or price_boost_active
        or predump_active
    )
    start_verifying = bool(
        start_requested
        and last_start_age_s is not None
        and last_start_age_s <= verify_s
    )
    stop_active = bool(
        stop_sent_active
        and (
            real_charging
            or real_power_w > 50.0
            or current_amp >= min_current
            or cap >= min_current
        )
    )

    if rscp_error:
        state = STATE_RSCP_ERROR
        reason = "Letzter RSCP-Zugriff fehlgeschlagen."
    elif not connected:
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
        reason = "Stop ist aktiv; es wird kein Start-Toggle gesendet."
    elif start_verifying:
        state = STATE_STARTING
        reason = (
            "%d A freigegeben; E3DC-Control wartet auf ALG-Bit oder "
            "verifizierte Phasenleistung."
        ) % offered_amp
    elif start_requested:
        state = STATE_OFFERED
        reason = "%d A freigegeben; noch keine echte Ladebestätigung." % offered_amp
    else:
        state = STATE_IDLE
        reason = "Fahrzeug verbunden; keine Startfreigabe aktiv." if connected else "Kein Fahrzeug verbunden."

    start_blocked = bool(
        state in (STATE_RSCP_ERROR, STATE_STOPPING, STATE_ENDED)
        or mode_off
        or priority_forced_stop
    )
    can_send_start_toggle = bool(
        state == STATE_OFFERED
        and physical_budget_ready
        and not start_blocked
        and offered_amp >= min_current
    )
    if st.get('abort_fresh') is True and type(st.get('abort_active')) is bool:
        # Ein alter Stromsollwert oder eine nachlaufende Ladeleistung ist
        # kein Startverbot nach einer bestätigten Abbruchflanke.
        start_blocked = bool(rscp_error or ended_latched or mode_off or priority_forced_stop)
        can_send_start_toggle = bool(st['abort_active'] is True and physical_budget_ready
                                     and not start_blocked and offered_amp >= min_current)
    if end_reason:
        reason = "%s (%s)" % (reason, end_reason)

    return {
        "state": state,
        "label": _state_label(state),
        "level": _state_level(state),
        "reason": reason,
        "connected": bool(connected),
        "real_charging": bool(real_charging),
        "real_power_w": float(real_power_w),
        "offered_amp": int(offered_amp),
        "current_set_amp": int(current_amp),
        "cap_amp": int(cap),
        "budget_ready": bool(physical_budget_ready),
        "start_requested": bool(start_requested),
        "start_verifying": bool(start_verifying),
        "stop_active": bool(stop_active),
        "last_start_age_s": last_start_age_s,
        "start_blocked": bool(start_blocked),
        "can_send_start_toggle": bool(can_send_start_toggle),
        "counts_as_real_charge": bool(real_charging),
    }


def apply_session_to_status(status: Optional[Dict[str, Any]], session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Attach session diagnostics to a status dict in-place and return it."""

    if status is None:
        return None
    status["e3dc_session_state"] = session.get("state", STATE_IDLE)
    status["e3dc_session_label"] = session.get("label", _state_label(STATE_IDLE))
    status["e3dc_session_level"] = session.get("level", _state_level(STATE_IDLE))
    status["e3dc_session_reason"] = session.get("reason", "")
    status["e3dc_session_offered_amp"] = int(session.get("offered_amp", 0) or 0)
    status["e3dc_session_budget_ready"] = bool(session.get("budget_ready", False))
    status["e3dc_session_start_requested"] = bool(session.get("start_requested", False))
    status["e3dc_session_start_verifying"] = bool(session.get("start_verifying", False))
    status["e3dc_session_stop_active"] = bool(session.get("stop_active", False))
    status["e3dc_session_start_blocked"] = bool(session.get("start_blocked", False))
    status["e3dc_session_can_send_start_toggle"] = bool(session.get("can_send_start_toggle", False))
    status["e3dc_session_counts_as_real_charge"] = bool(session.get("counts_as_real_charge", False))
    return status
