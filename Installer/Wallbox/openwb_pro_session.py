"""Pure openWB Pro session contract.

openWB Pro connect.php is level-triggered: ``ampere`` and ``phasetarget`` are
absolute device state, not E3DC RSCP toggle edges.  This module keeps the
manager-owned start offer, phase settle window and real-charge confirmation
visible and testable without touching drivers, files or network state.
"""

from typing import Any, Dict, Optional

from .decision import status_connected, status_real_charging, status_real_power


STATE_IDLE = "idle"
STATE_OFFERED = "offered"
STATE_STARTING = "starting"
STATE_WAKEUP = "wakeup"
STATE_CHARGING = "charging"
STATE_PHASE_WAIT = "phase_wait"
STATE_STOPPING = "stopping"
STATE_ENDED = "ended"

CONTRACT_NAME = "openwb_pro_connect_php_level_state"

_STATE_LABELS = {
    STATE_IDLE: ("Idle", "secondary"),
    STATE_OFFERED: ("Startfreigabe", "warning"),
    STATE_WAKEUP: ("Wake-up", "warning"),
    STATE_STARTING: ("Start wartet", "warning"),
    STATE_CHARGING: ("Lade", "success"),
    STATE_PHASE_WAIT: ("Phasenwechsel", "warning"),
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


def _current_step(value: Any, default: float = 1.0) -> float:
    step = _safe_float(value, default)
    if step <= 0.11:
        return 0.1
    if step <= 0.51:
        return 0.5
    return 1.0


def _round_to_step(value: float, step: float) -> float:
    step = _current_step(step)
    rounded = round(float(value or 0.0) / step) * step
    return float(int(round(rounded))) if step >= 0.99 else round(rounded, 1)


def _state_label(state: str) -> str:
    return _STATE_LABELS.get(state, _STATE_LABELS[STATE_IDLE])[0]


def _state_level(state: str) -> str:
    return _STATE_LABELS.get(state, _STATE_LABELS[STATE_IDLE])[1]


def _valid_phase_count(value: Any, default: int = 0) -> int:
    try:
        phases = int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)
    return phases if phases in (1, 2, 3) else int(default)


def is_openwb_pro_charger(charger: Any) -> bool:
    """Return true for the official openWB Pro connect.php driver."""

    return bool(charger is not None and charger.__class__.__name__ == "OpenWBProCharger")


def start_hold_s(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config or {}
    return max(60.0, _safe_float(cfg.get("openwb_pro_start_hold_s", 180), 180.0))


def mark_start_offer(
    state: Dict[str, Any],
    amp: Any = 6,
    *,
    now_ts: Any = None,
    config: Optional[Dict[str, Any]] = None,
    charger_max_amp: Any = 32,
    refresh: bool = False,
) -> None:
    """Remember that an openWB Pro start offer is standing."""

    if not isinstance(state, dict):
        return
    now_value = _safe_float(now_ts, 0.0)
    if now_value <= 0.0:
        import time

        now_value = time.time()
    max_amp = max(6, _safe_int(charger_max_amp, 32))
    amp_value = max(0, min(max_amp, _safe_int(amp, 0)))
    if amp_value < 6:
        return
    hold_s = start_hold_s(config)
    last_start_ts = _safe_float(state.get("last_start_ts", 0.0), 0.0)
    hold_until = _safe_float(state.get("_openwb_pro_start_hold_until", 0.0), 0.0)
    if refresh or last_start_ts <= 0.0 or hold_until <= now_value:
        state["last_start_ts"] = now_value
        hold_until = now_value + hold_s
    state["_openwb_pro_start_hold_until"] = hold_until
    state["_openwb_pro_start_hold_amp"] = max(
        _safe_int(state.get("_openwb_pro_start_hold_amp", 0), 0),
        amp_value,
    )
    if refresh and str(state.get("_bev_full_block_reason") or "") == "start_rejected_soft":
        state["_bev_full_block_reason"] = ""
        state["_openwb_start_reject_soft_until"] = 0.0


def start_hold_active(
    state: Dict[str, Any],
    now_ts: Any = None,
    *,
    hw_charging: bool = False,
    stable_hw_power_w: Any = 0.0,
) -> bool:
    if not isinstance(state, dict):
        return False
    now_value = _safe_float(now_ts, 0.0)
    if now_value <= 0.0:
        import time

        now_value = time.time()
    hold_until = _safe_float(state.get("_openwb_pro_start_hold_until", 0.0), 0.0)
    hold_amp = _safe_int(state.get("_openwb_pro_start_hold_amp", 0), 0)
    power_w = _safe_float(stable_hw_power_w, 0.0)
    return bool(
        hold_amp >= 6
        and now_value < hold_until
        and not hw_charging
        and power_w <= 500.0
    )


def phase_wait_s(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config or {}
    value = _safe_float(cfg.get("openwb_pro_phase_wait_s", 480), 480.0)
    return max(480.0, value)


def phase_zero_settle_s(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config or {}
    value = _safe_float(cfg.get("openwb_pro_phase_zero_settle_s", 3), 3.0)
    return min(30.0, max(2.0, value))


def phase_restart_delay_s(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config or {}
    return max(0.0, _safe_float(cfg.get("openwb_pro_phase_restart_delay_s", 30), 30.0))


def start_wakeup_delay_s(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config or {}
    return max(0.0, _safe_float(cfg.get("openwb_pro_start_wakeup_delay_s", 5), 5.0))


def cp_interrupt_payload(
    config: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return official connect.php CP-interrupt options.

    The driver translates this neutral payload to ``cp_interrupt=true`` plus
    optional duration/version fields.  Keeping the extraction here prevents the
    manager from owning protocol defaults while still allowing the manager to
    decide when a CP interrupt is safe.
    """

    cfg = config if isinstance(config, dict) else {}
    st = status if isinstance(status, dict) else {}
    payload: Dict[str, Any] = {}
    duration = cfg.get(
        "openwb_pro_cp_interrupt_s",
        cfg.get("openwb_pro_cp_interrupt_duration_s", cfg.get("openwb_pro_cp_interrupt_duration", None)),
    )
    if duration is None:
        duration = st.get("cp_interrupt_duration", None)
    try:
        if duration is not None and str(duration).strip() != "":
            duration_value = int(float(duration))
            if duration_value > 0:
                payload["duration"] = duration_value
    except (TypeError, ValueError):
        pass
    if "duration" not in payload:
        payload["duration"] = 10

    version = str(
        cfg.get(
            "openwb_pro_cp_interrupt_version",
            st.get("cp_interrupt_version", ""),
        )
        or ""
    ).strip()
    if version in ("0V", "-12V"):
        payload["version"] = version
    return payload


def phase_sequence_step_contract(
    target_phases: Any,
    sequence: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
    hold_s: Any = 480,
    restart_delay_s: Any = 30,
    current_set_amp: Any = 0,
    status: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    reason: str = "phase_switch",
    cp_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the next safe openWB Pro phase-sequence step.

    The caller still owns state mutation and driver IO.  This contract only
    encodes the hard sequence required around connect.php: 0 A, short settle,
    ``phasetarget``, CP interrupt, restart delay, then current release.  The
    long protection window is a post-switch cooldown against the next phase
    change, not an 8 minute delay before ``phasetarget``.
    """

    target = _valid_phase_count(target_phases, 0)
    now_value = _safe_float(now_ts, 0.0)
    restart_seconds = max(0.0, _safe_float(restart_delay_s, 30.0))
    seq = sequence if isinstance(sequence, dict) else {}
    st = status if isinstance(status, dict) else {}
    cfg = config if isinstance(config, dict) else {}
    phase_cooldown_seconds = max(30.0, _safe_float(hold_s, 480.0))
    zero_settle_seconds = phase_zero_settle_s(cfg)
    base = {
        "contract": "openwb_pro_phase_sequence_step_v1",
        "target": int(target),
        "action": "invalid",
        "stage": "",
        "ready": False,
        "command": None,
        "sequence": None,
        "sequence_patch": {},
        "phase_wait_config": {},
        "phase_cooldown_s": float(phase_cooldown_seconds),
        "zero_settle_s": float(zero_settle_seconds),
        "reason": str(reason or "phase_switch"),
    }
    if target not in (1, 3):
        base["reason"] = "invalid_phase_target"
        return base

    if not seq or _safe_int(seq.get("target", 0), 0) != target:
        amp_before = max(
            0,
            _safe_int(current_set_amp, 0),
            _safe_int(st.get("amp", 0), 0),
        )
        hold_amp = max(6, amp_before) if amp_before >= 6 else 0
        next_sequence = {
            "target": target,
            "stage": "zero_wait",
            "reason": str(reason or "phase_switch"),
            "hold_amp": hold_amp,
            "started_ts": now_value,
            "zero_until": now_value + zero_settle_seconds,
            "phase_cooldown_s": phase_cooldown_seconds,
            "zero_settle_s": zero_settle_seconds,
            "phase_sent_ts": 0.0,
            "cp_sent_ts": 0.0,
            "current_allowed_after": 0.0,
        }
        return {
            **base,
            "action": "send_zero",
            "stage": "zero_wait",
            "sequence": next_sequence,
            "phase_wait_config": {
                "openwb_pro_phase_wait_s": phase_cooldown_seconds,
            },
            "command": {
                "method": "set_amp_and_state",
                "amp": 0,
                "force_state": 1,
                "reason": "openwb_pro_phase_zero",
                "_openwb_pro_sequence_internal": True,
            },
        }

    stage = str(seq.get("stage") or "zero_wait")
    if stage == "zero_wait":
        zero_until = _safe_float(seq.get("zero_until"), now_value + zero_settle_seconds)
        if now_value < zero_until:
            return {**base, "action": "wait_zero", "stage": "zero_wait"}
        return {
            **base,
            "action": "send_phase",
            "stage": "cp_after_phase",
            "phase_wait_config": {
                "openwb_pro_phase_wait_s": _safe_float(
                    seq.get("phase_cooldown_s"),
                    phase_cooldown_seconds,
                ),
            },
            "sequence_patch": {
                "stage": "cp_after_phase",
                "phase_sent_ts": now_value,
            },
            "command": {
                "method": "set_phases",
                "phases": target,
                "reason": "openwb_pro_phase_target",
                "_openwb_pro_sequence_internal": True,
            },
        }

    if stage == "cp_after_phase":
        payload = cp_payload if isinstance(cp_payload, dict) else {}
        return {
            **base,
            "action": "send_cp",
            "stage": "restart_delay",
            "sequence_patch": {
                "stage": "restart_delay",
                "cp_sent_ts": now_value,
                "current_allowed_after": now_value + restart_seconds,
            },
            "command": {
                "method": "trigger_cp_interrupt",
                "reason": "openwb_pro_phase_cp_interrupt",
                "_openwb_pro_sequence_internal": True,
                **payload,
            },
        }

    if stage == "restart_delay":
        allowed_after = _safe_float(seq.get("current_allowed_after"), now_value + restart_seconds)
        if now_value < allowed_after:
            return {
                **base,
                "action": "wait_restart",
                "stage": "restart_delay",
                "sequence_patch": {"current_allowed_after": allowed_after},
            }
        return {
            **base,
            "action": "ready",
            "stage": "ready",
            "ready": True,
            "sequence": dict(seq),
        }

    return {**base, "action": "unknown_stage", "stage": stage, "reason": "unknown_phase_sequence_stage"}


def start_wakeup_step_contract(
    method: Any,
    amp: Any,
    state_data: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
    cp_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the next safe openWB Pro start wake-up step.

    The manager decides when a start command is desired.  This contract only
    gates that command around sleeping vehicles: optionally send CP interrupt,
    wait the configured delay, and then allow the current command through.
    """

    data = state_data if isinstance(state_data, dict) else {}
    st = status if isinstance(status, dict) else {}
    cfg = config if isinstance(config, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    method_text = str(method or "").strip()
    amp_value = _safe_float(amp, 0.0)
    delay_s = start_wakeup_delay_s(cfg)
    retry_s = max(30.0, _safe_float(cfg.get("wb_openwb_start_retry_s"), 45.0))
    max_retries = max(1, min(5, _safe_int(cfg.get("wb_openwb_start_cp_retries"), 2)))
    sent_count = max(0, _safe_int(data.get("_openwb_pro_start_wakeup_count", 0), 0))
    base = {
        "contract": "openwb_pro_start_wakeup_step_v1",
        "action": "allow",
        "allow": True,
        "reason": "allow",
        "command": None,
        "command_patch": {},
        "state_patch": {},
        "success_state_patch": {},
        "delay_s": float(delay_s),
        "retry_s": float(retry_s),
        "max_retries": int(max_retries),
        "sent_count": int(sent_count),
        "phase_wait_active": False,
    }

    if method_text not in ("set_amp_and_state", "set_current", "set_direct_current"):
        return {**base, "action": "non_start_command", "reason": "not_current_command"}
    if amp_value < 6.0:
        return {
            **base,
            "action": "clear_below_min_current",
            "reason": "below_min_current",
            "state_patch": {
                "_openwb_pro_start_wakeup_allowed_after": 0.0,
                "_openwb_pro_start_wakeup_pending": False,
            },
        }
    if bool(data.get("_bev_full_blocked", False)):
        return {**base, "action": "blocked_vehicle_finished", "allow": False, "reason": "bev_full_blocked"}

    sequence = data.get("_openwb_pro_phase_sequence")
    if isinstance(sequence, dict) and sequence and str(sequence.get("stage") or "") != "ready":
        return {**base, "action": "blocked_phase_sequence", "allow": False, "reason": "phase_sequence_active"}

    command_patch: Dict[str, Any] = {}
    last_sequence = data.get("_openwb_pro_phase_sequence_last")
    if isinstance(last_sequence, dict):
        allowed_after = _safe_float(last_sequence.get("current_allowed_after"), 0.0)
        phase_restart_grace_s = max(60.0, phase_restart_delay_s(cfg) + 30.0)
        if allowed_after > 0.0 and allowed_after <= now_value <= allowed_after + phase_restart_grace_s:
            command_patch["_guard_allow_restart_after_stop"] = True

    connected = bool(st.get("plug_state") or st.get("car") == 2)
    real_power_w = max(
        _safe_float(st.get("real_power_w"), 0.0),
        _safe_float(st.get("phase_power_sum_w"), 0.0),
        _safe_float(st.get("power_w"), 0.0),
    )
    charging = bool(st.get("charging") or st.get("charge_state") or real_power_w > 500.0)
    if not connected:
        return {
            **base,
            "action": "clear_not_connected",
            "reason": "not_connected",
            "command_patch": command_patch,
            "state_patch": {
                "_openwb_pro_start_wakeup_allowed_after": 0.0,
                "_openwb_pro_start_wakeup_pending": False,
                "_openwb_pro_start_wakeup_count": 0,
            },
        }

    allowed_after = _safe_float(data.get("_openwb_pro_start_wakeup_allowed_after"), 0.0)
    if allowed_after > now_value and real_power_w <= 500.0:
        return {
            **base,
            "action": "wait_wakeup_delay",
            "allow": False,
            "reason": "wakeup_delay_active",
            "command_patch": command_patch,
            "state_patch": {"_openwb_pro_start_wakeup_pending": True},
        }
    if charging or real_power_w > 500.0:
        return {
            **base,
            "action": "clear_connected_or_charging",
            "reason": "not_connected_or_already_charging",
            "command_patch": command_patch,
            "state_patch": {
                "_openwb_pro_start_wakeup_allowed_after": 0.0,
                "_openwb_pro_start_wakeup_pending": False,
                "_openwb_pro_start_wakeup_count": 0,
            },
        }

    phase_wait_until = _safe_float(data.get("_openwb_pro_phase_wait_until"), 0.0)
    phase_wait_target = _valid_phase_count(data.get("_openwb_pro_phase_wait_target"), 0)
    if phase_wait_target in (1, 3) and phase_wait_until > now_value:
        return {
            **base,
            "action": "allow_without_cp",
            "reason": "phase_wait_no_cp",
            "command_patch": command_patch,
            "phase_wait_active": True,
        }

    last_cp_ts = _safe_float(data.get("_openwb_pro_start_wakeup_cp_ts"), 0.0)
    if last_cp_ts > 0.0 and now_value - last_cp_ts < retry_s:
        return {
            **base,
            "action": "allow_recent_cp_retry_window",
            "reason": "recent_cp_retry_window",
            "command_patch": {**command_patch, "_guard_allow_restart_after_stop": True},
            "state_patch": {"_openwb_pro_start_wakeup_pending": False},
        }
    if sent_count >= max_retries:
        return {
            **base,
            "action": "allow_after_max_retries",
            "reason": "max_cp_retries_reached",
            "command_patch": {**command_patch, "_guard_allow_restart_after_stop": True},
            "state_patch": {"_openwb_pro_start_wakeup_pending": False},
        }

    if bool(st.get("cp_interrupt_isactive", 0)):
        return {
            **base,
            "action": "wait_active_cp_interrupt",
            "allow": False,
            "reason": "cp_interrupt_active",
            "command_patch": command_patch,
            "state_patch": {
                "_openwb_pro_start_wakeup_allowed_after": now_value + delay_s,
                "_openwb_pro_start_wakeup_pending": True,
            },
        }

    payload = cp_payload if isinstance(cp_payload, dict) else {}
    return {
        **base,
        "action": "send_cp_interrupt",
        "allow": False,
        "reason": "send_cp_interrupt",
        "command_patch": command_patch,
        "command": {
            "method": "trigger_cp_interrupt",
            "reason": "openwb_pro_start_wakeup",
            "_openwb_pro_sequence_internal": True,
            **payload,
        },
        "success_state_patch": {
            "_openwb_pro_start_wakeup_cp_ts": now_value,
            "_openwb_pro_start_wakeup_allowed_after": now_value + delay_s,
            "_openwb_pro_start_wakeup_pending": True,
            "_openwb_pro_start_wakeup_count": sent_count + 1,
            "_openwb_cp_start_sent": True,
            "_openwb_last_cp_start_ts": now_value,
        },
    }


def vehicle_finished_drop_contract(
    status: Optional[Dict[str, Any]] = None,
    state_data: Optional[Dict[str, Any]] = None,
    *,
    is_manager_charging: bool = False,
    current_set_amp: Any = 0,
    time_since_start_s: Any = 0,
    startup_grace_s: Any = 90,
    min_amp: Any = 6,
    had_confirmed_charge: Optional[bool] = None,
    observe_only: bool = False,
    openwb_mode9_monitor: bool = False,
    stop_sent_active: bool = False,
    manager_zero_anchor_active: bool = False,
    recent_manager_stop: bool = False,
    recent_manager_start_retry: bool = False,
    phase_transition_active: bool = False,
    openwb_pro_phase_transition_active: bool = False,
    now_ts: Any = 0,
) -> Dict[str, Any]:
    """Classify an openWB Pro power drop before the central charge-end latch.

    This contract does not decide or send a stop.  It only says whether the
    live drop is a safe candidate for ``charge_end_latch_contract``.  Manager
    stops, phase pauses, wake-up retries and unconfirmed start attempts remain
    visible blockers instead of being mistaken for a finished vehicle.
    """

    st = status if isinstance(status, dict) else {}
    data = state_data if isinstance(state_data, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    min_current = max(1, _safe_int(min_amp, 6))
    current_amp = max(0, _safe_int(current_set_amp, 0))
    time_since_start = max(0.0, _safe_float(time_since_start_s, 0.0))
    grace_s = max(0.0, _safe_float(startup_grace_s, 90.0))
    connected = status_connected(st)
    real_power_w = max(
        status_real_power(st),
        _safe_float(st.get("real_power_w"), 0.0) if bool(st.get("charging") or st.get("charge_state")) else 0.0,
    )
    real_charging = bool(status_real_charging(st) or real_power_w > 500.0)
    confirmed = (
        bool(data.get("_aha_real_charge_confirmed", False))
        if had_confirmed_charge is None
        else bool(had_confirmed_charge)
    )
    hardware_amp = max(
        _safe_int(st.get("amp", 0), 0),
        _safe_int(st.get("offered_current_raw", 0), 0),
        _safe_int(st.get("evse_current", 0), 0),
    )

    blockers = []
    if not bool(is_manager_charging):
        blockers.append("manager_not_in_charge_session")
    if not connected:
        blockers.append("vehicle_not_connected")
    if real_charging:
        blockers.append("real_charge_still_active")
    if time_since_start <= grace_s:
        blockers.append("startup_grace_active")
    if current_amp <= 0:
        blockers.append("no_manager_current_offer")
    if bool(observe_only):
        blockers.append("observe_only")
    if bool(openwb_mode9_monitor):
        blockers.append("openwb_mode9_monitor")
    if bool(stop_sent_active):
        blockers.append("manager_stop_already_sent")
    if bool(manager_zero_anchor_active):
        blockers.append("manager_zero_anchor_active")
    if bool(recent_manager_stop):
        blockers.append("recent_manager_stop")
    if bool(recent_manager_start_retry):
        blockers.append("recent_manager_start_retry")
    if bool(phase_transition_active):
        blockers.append("phase_transition_active")
    if bool(openwb_pro_phase_transition_active):
        blockers.append("openwb_pro_phase_transition_active")
    if not confirmed:
        blockers.append("no_confirmed_real_charge")

    allow = bool(not blockers)
    action = "candidate" if allow else "ignore"
    reason = "vehicle_finished_candidate" if allow else blockers[0]
    return {
        "contract": "openwb_pro_vehicle_finished_drop_v1",
        "action": action,
        "allow_new_latch": bool(allow),
        "reason": reason,
        "blockers": blockers,
        "connected": bool(connected),
        "real_charging": bool(real_charging),
        "real_power_w": float(real_power_w if real_charging else 0.0),
        "manager_charging": bool(is_manager_charging),
        "current_set_amp": int(current_amp),
        "hardware_amp": int(hardware_amp),
        "min_amp": int(min_current),
        "time_since_start_s": float(time_since_start),
        "startup_grace_s": float(grace_s),
        "had_confirmed_charge": bool(confirmed),
        "ts": float(now_value),
    }


def apply_vehicle_finished_drop_to_status(
    status: Optional[Dict[str, Any]],
    contract: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Attach vehicle-finished pre-latch diagnostics to a status dict."""

    if status is None or not isinstance(contract, dict):
        return status
    status["openwb_pro_vehicle_finished_contract"] = contract
    status["openwb_pro_vehicle_finished_action"] = str(contract.get("action", "") or "")
    status["openwb_pro_vehicle_finished_reason"] = str(contract.get("reason", "") or "")
    status["openwb_pro_vehicle_finished_allow_new_latch"] = bool(contract.get("allow_new_latch", False))
    status["openwb_pro_vehicle_finished_blockers"] = list(contract.get("blockers") or [])
    status["openwb_pro_vehicle_finished_had_confirmed_charge"] = bool(
        contract.get("had_confirmed_charge", False)
    )
    status["openwb_pro_vehicle_finished_time_since_start_s"] = float(
        contract.get("time_since_start_s", 0.0) or 0.0
    )
    return status


def temporary_ems_stop_contract(
    status: Optional[Dict[str, Any]] = None,
    state_data: Optional[Dict[str, Any]] = None,
    *,
    current_set_amp: Any = 0,
    cap_amp: Any = 0,
    min_amp: Any = 6,
    mode_off: bool = False,
    priority_forced_stop: bool = False,
    stop_sent_active: bool = False,
    manager_stop_pending: bool = False,
    manager_stop_reason: str = "",
    manager_zero_anchor_active: bool = False,
    ended_latched: bool = False,
    now_ts: Any = 0,
    stable_hw_power_w: Any = 0.0,
) -> Dict[str, Any]:
    """Classify temporary EMS stops separately from vehicle-finished latches."""

    st = status if isinstance(status, dict) else {}
    data = state_data if isinstance(state_data, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    min_current = max(1, _safe_int(min_amp, 6))
    current_amp = max(0, _safe_int(current_set_amp, 0))
    cap = max(0, _safe_int(cap_amp, 0))
    hw_amp = max(
        _safe_int(st.get("amp", 0), 0),
        _safe_int(st.get("offered_current_raw", 0), 0),
        _safe_int(st.get("evse_current", 0), 0),
    )
    connected = status_connected(st)
    real_power_w = max(status_real_power(st), _safe_float(stable_hw_power_w, 0.0))
    real_charging = bool(status_real_charging(st) or real_power_w > 500.0)
    zero_anchor = bool(manager_zero_anchor_active or data.get("_manager_zero_anchor_active", False))
    stop_pending = bool(manager_stop_pending or st.get("manager_stop_pending", False))
    last_stop_known = bool(
        _safe_float(data.get("_last_manager_stop_request_ts", 0.0), 0.0) > 0.0
        or _safe_float(data.get("_last_manager_zero_anchor_ts", 0.0), 0.0) > 0.0
    )
    stop_has_effect = bool(
        real_charging
        or real_power_w > 50.0
        or current_amp >= min_current
        or cap >= min_current
        or hw_amp >= min_current
    )

    state_hint = "none"
    reason = ""
    active = False
    stopping = False
    temporary = False
    start_blocked = False

    if ended_latched:
        state_hint = "vehicle_finished"
        reason = "vehicle_finished_latched"
        start_blocked = True
    elif bool(mode_off):
        state_hint = "off"
        reason = "mode_off"
        active = True
        start_blocked = True
    elif stop_pending:
        state_hint = "stopping"
        reason = str(manager_stop_reason or st.get("manager_stop_reason") or "manager_stop_pending")
        active = True
        stopping = True
        temporary = True
        start_blocked = True
    elif bool(stop_sent_active) and stop_has_effect:
        state_hint = "stopping"
        reason = "stop_command_active"
        active = True
        stopping = True
        temporary = True
        start_blocked = True
    elif bool(stop_sent_active) and connected:
        state_hint = "waiting_start_release"
        reason = "stop_command_settled"
        active = True
        temporary = True
        start_blocked = False
    elif zero_anchor:
        state_hint = "waiting_start_release"
        reason = str(data.get("_last_manager_zero_anchor_reason") or "manager_zero_anchor_active")
        active = True
        temporary = True
        start_blocked = True
    elif bool(priority_forced_stop):
        state_hint = "waiting_start_release"
        reason = "priority_forced_stop"
        active = True
        temporary = True
        start_blocked = True
    elif last_stop_known and connected and current_amp <= 0 and cap <= 0 and hw_amp <= 0 and not real_charging:
        state_hint = "waiting_start_release"
        reason = "zero_current_policy_hold"
        active = True
        temporary = True
        start_blocked = False

    return {
        "contract": "openwb_pro_temporary_ems_stop_v1",
        "active": bool(active),
        "temporary": bool(temporary),
        "stopping": bool(stopping),
        "state_hint": state_hint,
        "reason": reason,
        "start_blocked": bool(start_blocked),
        "connected": bool(connected),
        "real_charging": bool(real_charging),
        "real_power_w": float(real_power_w if real_charging else 0.0),
        "current_set_amp": int(current_amp),
        "cap_amp": int(cap),
        "hardware_amp": int(hw_amp),
        "manager_stop_pending": bool(stop_pending),
        "manager_zero_anchor_active": bool(zero_anchor),
        "mode_off": bool(mode_off),
        "priority_forced_stop": bool(priority_forced_stop),
        "ended_latched": bool(ended_latched),
        "ts": float(now_value),
    }


def start_retry_guard_contract(
    command: Optional[Dict[str, Any]] = None,
    state_data: Optional[Dict[str, Any]] = None,
    *,
    session: Optional[Dict[str, Any]] = None,
    now_ts: Any = 0,
    reason: str = "",
) -> Dict[str, Any]:
    """Decide whether a start/retry command may bypass the chatter guard.

    The command guard is still the single executor-side protection.  This pure
    contract only identifies the narrow openWB-Pro retry cases that belong to
    the current session state, and blocks retries during EMS stop states or a
    latched vehicle-finished state.
    """

    cmd = command if isinstance(command, dict) else {}
    data = state_data if isinstance(state_data, dict) else {}
    sess = session if isinstance(session, dict) else (
        data.get("_openwb_pro_session") if isinstance(data.get("_openwb_pro_session"), dict) else {}
    )
    now_value = _safe_float(now_ts, 0.0)
    method = str(cmd.get("method") or cmd.get("kind") or "").strip().lower()
    kind = str(cmd.get("kind") or method or "").strip().lower()
    amp_value = _safe_float(cmd.get("amp"), 0.0)
    session_state = str(sess.get("state") or data.get("_openwb_pro_session_state") or "").strip().lower()
    hold_until = _safe_float(data.get("_openwb_pro_start_hold_until"), 0.0)
    hold_amp = _safe_float(data.get("_openwb_pro_start_hold_amp"), 0.0)
    hold_active = bool(sess.get("start_hold_active", False)) or (
        hold_until > now_value and hold_amp >= 6.0
    )
    phase_wait_until = _safe_float(data.get("_openwb_pro_phase_wait_until"), 0.0)
    phase_wait_target = _valid_phase_count(data.get("_openwb_pro_phase_wait_target"), 0)
    phase_wait_active_guard = bool(
        phase_wait_target in (1, 3)
        and phase_wait_until > now_value
    )
    start_verifying = bool(sess.get("start_verifying", False)) or session_state == STATE_STARTING
    budget_ready = bool(sess.get("budget_ready", False))
    can_send = bool(sess.get("can_send_start_command", False))
    stop_active = bool(sess.get("stop_active", False))
    start_blocked = bool(sess.get("start_blocked", False))
    temp_stop = sess.get("temporary_stop") if isinstance(sess.get("temporary_stop"), dict) else {}
    temporary_stop_active = bool(
        sess.get("temporary_stop_active", False)
        or temp_stop.get("active", False)
    )
    temporary_stop_hint = str(
        sess.get("temporary_stop_state_hint")
        or temp_stop.get("state_hint")
        or ""
    )
    vehicle_finished = bool(
        data.get("_bev_full_blocked", False)
        or session_state == STATE_ENDED
        or temporary_stop_hint == "vehicle_finished"
    )
    reason_text = " ".join(
        str(part or "")
        for part in (cmd.get("reason"), cmd.get("source"), reason)
    ).lower()
    retry_command = bool(
        "openwb_start_retry" in reason_text
        or "openwb_pro_keepalive" in reason_text
        or "phase_decision_apply_current" in reason_text
        or "openwb_pro_curve_direct" in reason_text
        or "set_current" in reason_text
    )
    soft_retry_due = bool(
        str(data.get("_bev_full_block_reason") or "") == "start_rejected_soft"
        and _safe_float(data.get("_openwb_start_reject_soft_until"), 0.0) > 0.0
        and now_value >= _safe_float(data.get("_openwb_start_reject_soft_until"), 0.0)
        and not bool(data.get("_bev_full_blocked", False))
    )
    command_valid = bool(
        (
            method in ("set_amp_and_state", "set_current", "set_direct_current")
            or kind in ("set_current", "hold_current")
        )
        and amp_value >= 6.0
    )
    try:
        if int(float(cmd.get("force_state"))) == 1:
            command_valid = False
    except Exception:
        pass

    allow = False
    block_reason = ""
    if not command_valid:
        block_reason = "not_start_current_command"
    elif not retry_command:
        block_reason = "not_retry_command"
    elif vehicle_finished:
        block_reason = "vehicle_finished"
    elif temporary_stop_active:
        block_reason = "temporary_stop_active"
    elif stop_active:
        block_reason = "stop_active"
    elif start_blocked:
        block_reason = "start_blocked"
    elif phase_wait_active_guard:
        allow = True
        block_reason = "phase_wait_no_cp"
    elif (
        start_verifying
        or hold_active
        or session_state in (STATE_OFFERED, STATE_PHASE_WAIT)
    ):
        if budget_ready and can_send:
            allow = True
            block_reason = "start_verification_retry"
        elif not budget_ready:
            block_reason = "budget_not_ready"
        else:
            block_reason = "cannot_send_start_command"
    elif soft_retry_due:
        allow = True
        block_reason = "soft_reject_retry_due"
    else:
        block_reason = "no_retry_scope"

    return {
        "contract": "openwb_pro_start_retry_guard_v1",
        "allow_override": bool(allow),
        "reason": block_reason,
        "command_valid": bool(command_valid),
        "retry_command": bool(retry_command),
        "soft_retry_due": bool(soft_retry_due),
        "session_state": session_state,
        "start_verifying": bool(start_verifying),
        "hold_active": bool(hold_active),
        "phase_wait_active": bool(phase_wait_active_guard),
        "budget_ready": bool(budget_ready),
        "can_send_start_command": bool(can_send),
        "stop_active": bool(stop_active),
        "temporary_stop_active": bool(temporary_stop_active),
        "temporary_stop_state_hint": temporary_stop_hint,
        "vehicle_finished": bool(vehicle_finished),
        "start_blocked": bool(start_blocked),
        "amp": float(amp_value),
        "ts": float(now_value),
    }


def phase_min_settle_s(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config or {}
    return max(0.0, _safe_float(cfg.get("openwb_pro_phase_min_settle_s", 30), 30.0))


def _record_phase_wait_measurement(
    state: Dict[str, Any],
    *,
    now_ts: float,
    target: int,
    actual: int,
    in_use: int,
    measured_power_w: float,
    result: str,
) -> None:
    if not isinstance(state, dict):
        return
    since = _safe_float(state.get("_openwb_pro_phase_wait_since", 0.0), 0.0)
    duration_s = max(0.0, float(now_ts or 0.0) - since) if since > 0.0 and now_ts > 0.0 else 0.0
    samples = max(0, _safe_int(state.get("_openwb_pro_phase_wait_samples", 0), 0))
    previous_ema = _safe_float(state.get("_openwb_pro_phase_wait_ema_s", 0.0), 0.0)
    previous_max = _safe_float(state.get("_openwb_pro_phase_wait_max_s", 0.0), 0.0)
    if duration_s > 0.0:
        samples += 1
        ema = duration_s if previous_ema <= 0.0 else previous_ema * 0.75 + duration_s * 0.25
    else:
        ema = previous_ema
    state["_openwb_pro_phase_wait_last_duration_s"] = round(duration_s, 1)
    state["_openwb_pro_phase_wait_last_result"] = str(result or "")
    state["_openwb_pro_phase_wait_last_target"] = int(target or 0)
    state["_openwb_pro_phase_wait_last_actual"] = int(actual or 0)
    state["_openwb_pro_phase_wait_last_in_use"] = int(in_use or 0)
    state["_openwb_pro_phase_wait_last_power_w"] = round(max(0.0, float(measured_power_w or 0.0)), 1)
    state["_openwb_pro_phase_wait_last_ts"] = float(now_ts or 0.0)
    state["_openwb_pro_phase_wait_samples"] = samples
    state["_openwb_pro_phase_wait_ema_s"] = round(ema, 1)
    state["_openwb_pro_phase_wait_max_s"] = round(max(previous_max, duration_s), 1)


def mark_phase_wait(
    state: Dict[str, Any],
    phases: Any,
    *,
    current_amp: Any = 0,
    now_ts: Any = None,
    config: Optional[Dict[str, Any]] = None,
    charger_max_amp: Any = 32,
) -> None:
    """Remember that openWB Pro is settling after a phasetarget command."""

    if not isinstance(state, dict):
        return
    target = _valid_phase_count(phases, 0)
    if target not in (1, 3):
        return
    now_value = _safe_float(now_ts, 0.0)
    if now_value <= 0.0:
        import time

        now_value = time.time()
    max_amp = max(6, _safe_int(charger_max_amp, 32))
    amp_value = _safe_int(current_amp, 0)
    if amp_value >= 6:
        amp_value = max(6, min(max_amp, amp_value))
    else:
        amp_value = 0
    wait_s = phase_wait_s(config)
    min_settle_s = min(wait_s, phase_min_settle_s(config))
    state["_openwb_pro_phase_wait_target"] = target
    state["_openwb_pro_phase_wait_until"] = now_value + wait_s
    state["_openwb_pro_phase_wait_min_until"] = now_value + min_settle_s
    state["_openwb_pro_phase_wait_amp"] = amp_value
    state["_openwb_pro_phase_wait_since"] = now_value


def clear_phase_wait(state: Dict[str, Any]) -> None:
    if not isinstance(state, dict):
        return
    state["_openwb_pro_phase_wait_target"] = 0
    state["_openwb_pro_phase_wait_until"] = 0.0
    state["_openwb_pro_phase_wait_min_until"] = 0.0
    state["_openwb_pro_phase_wait_amp"] = 0
    state["_openwb_pro_phase_wait_since"] = 0.0


def phase_wait_active(
    state: Dict[str, Any],
    status: Optional[Dict[str, Any]] = None,
    now_ts: Any = None,
    *,
    stable_hw_power_w: Any = 0.0,
) -> bool:
    if not isinstance(state, dict):
        return False
    now_value = _safe_float(now_ts, 0.0)
    if now_value <= 0.0:
        import time

        now_value = time.time()
    hold_until = _safe_float(state.get("_openwb_pro_phase_wait_until", 0.0), 0.0)
    if hold_until <= 0.0:
        return False
    min_until = _safe_float(state.get("_openwb_pro_phase_wait_min_until", 0.0), 0.0)
    min_settle_active = bool(min_until > 0.0 and now_value < min_until)
    target = _valid_phase_count(state.get("_openwb_pro_phase_wait_target"), 0)
    if target not in (1, 3):
        clear_phase_wait(state)
        return False
    st = status or {}
    status_target = _valid_phase_count(st.get("phases_target"), target)
    actual = _valid_phase_count(st.get("phases_actual"), 0)
    in_use = _valid_phase_count(st.get("phases_in_use"), 0)
    measured_power_w = max(
        _safe_float(st.get("real_power_w", st.get("power_w", 0.0)), 0.0),
        _safe_float(st.get("phase_power_sum_w", 0.0), 0.0),
        _safe_float(stable_hw_power_w, 0.0),
    )
    if now_value >= hold_until:
        confirmed_after_hold = bool(
            measured_power_w > 500.0
            and (
                actual == target
                or in_use == target
            )
        )
        _record_phase_wait_measurement(
            state,
            now_ts=now_value,
            target=target,
            actual=actual,
            in_use=in_use,
            measured_power_w=measured_power_w,
            result="confirmed_after_hold" if confirmed_after_hold else "timeout",
        )
        clear_phase_wait(state)
        state["_last_phase_switch_ts"] = now_value
        return False
    if status_target not in (0, target):
        return True
    # openWB Pro may report the target phase before the vehicle has finished
    # renegotiating CP.  Keep the post-switch protection window alive until
    # hold_until so no wake-up CP interrupts can disturb sensitive onboard
    # chargers that briefly expose an apparent completed session.
    if actual == target or in_use == target or measured_power_w > 500.0 or min_settle_active:
        return True
    return True


def direct_bulk_ready(
    status: Optional[Dict[str, Any]] = None,
    *,
    hw_charging: bool = False,
    stable_hw_power_w: Any = 0.0,
) -> bool:
    """Return True once openWB Pro/BEV confirmed that a start or phase switch is real."""

    if hw_charging:
        return True
    st = status or {}
    measured_power_w = max(
        _safe_float(st.get("real_power_w", st.get("power_w", 0.0)), 0.0),
        _safe_float(st.get("phase_power_sum_w", 0.0), 0.0),
        _safe_float(stable_hw_power_w, 0.0),
    )
    return bool(measured_power_w > 500.0)


def direct_target_amp(
    current_amp: Any,
    direct_amp: Any,
    direct_direction: Any,
    *,
    bulk_ready: bool = False,
    start_amp: Any = 6,
    down_step_a: Any = 2,
    current_step_amp: Any = 1.0,
) -> float:
    """Calculate openWB Pro PV-curve setpoint without slow +1A ramps after confirmation."""

    step = _current_step(current_step_amp, 1.0)
    current_value = max(0.0, _safe_float(current_amp, 0.0))
    direct_value = max(0.0, _safe_float(direct_amp, 0.0))
    start_value = max(6.0, _safe_float(start_amp, 6.0))
    down_step = max(step, _safe_float(down_step_a, 2.0))

    if _safe_int(direct_direction, 0) < 0:
        if direct_value <= 0:
            return 0.0
        return _round_to_step(max(direct_value, current_value - down_step, 6.0), step)
    if _safe_int(direct_direction, 0) > 0:
        if direct_value <= 0:
            return 0.0
        if current_value <= 0:
            return _round_to_step(min(direct_value, start_value), step)
        if bulk_ready:
            return _round_to_step(direct_value, step)
        return _round_to_step(min(direct_value, current_value + max(1.0, step)), step)
    return _round_to_step(current_value, step)


def evaluate_session(
    status: Optional[Dict[str, Any]],
    *,
    state_data: Optional[Dict[str, Any]] = None,
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
    manager_stop_pending: bool = False,
    manager_stop_reason: str = "",
    ended_latched: bool = False,
    end_reason: str = "",
    last_start_ts: Any = 0,
    now_ts: Any = 0,
    start_verify_s: Any = 180,
    stable_hw_power_w: Any = 0.0,
) -> Dict[str, Any]:
    """Classify one manager-owned openWB Pro session."""

    st = status or {}
    data = state_data if isinstance(state_data, dict) else {}
    now = _safe_float(now_ts, 0.0)
    min_current = max(1, _safe_int(min_amp, 6))
    current_amp = max(0, _safe_int(current_set_amp, 0))
    cap = max(0, _safe_int(cap_amp, 0))
    hw_amp = max(0, _safe_int(st.get("amp", 0), 0))
    offered_amp = max(current_amp, cap, hw_amp if hw_amp >= min_current else 0)
    connected = status_connected(st)
    real_power_w = max(status_real_power(st), _safe_float(stable_hw_power_w, 0.0))
    real_charging = bool(status_real_charging(st) or real_power_w > 500.0)
    last_start = _safe_float(last_start_ts, 0.0)
    verify_s = max(0.0, _safe_float(start_verify_s, 180.0))
    last_start_age_s = max(0.0, now - last_start) if now > 0.0 and last_start > 0.0 else None
    start_hold = start_hold_active(
        data,
        now,
        hw_charging=real_charging,
        stable_hw_power_w=real_power_w,
    )
    wakeup_wait_until = _safe_float(data.get("_openwb_pro_start_wakeup_allowed_after", 0.0), 0.0)
    wakeup_pending = bool(wakeup_wait_until > 0.0 and now > 0.0 and now < wakeup_wait_until)
    phase_wait = phase_wait_active(
        data,
        st,
        now,
        stable_hw_power_w=real_power_w,
    )
    start_requested = bool(
        connected
        and not mode_off
        and offered_amp >= min_current
        and (current_amp >= min_current or cap >= min_current or start_hold)
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
        and (
            start_hold
            or (
                last_start_age_s is not None
                and last_start_age_s <= verify_s
            )
        )
    )
    temporary_stop = temporary_ems_stop_contract(
        st,
        data,
        current_set_amp=current_amp,
        cap_amp=cap,
        min_amp=min_current,
        mode_off=mode_off,
        priority_forced_stop=priority_forced_stop,
        stop_sent_active=stop_sent_active,
        manager_stop_pending=manager_stop_pending,
        manager_stop_reason=manager_stop_reason,
        manager_zero_anchor_active=bool(data.get("_manager_zero_anchor_active", False)),
        ended_latched=ended_latched,
        now_ts=now,
        stable_hw_power_w=real_power_w,
    )
    stop_active = bool(temporary_stop.get("stopping", False))
    stop_hint = str(temporary_stop.get("state_hint") or "none")
    temporary_waiting = bool(stop_hint == "waiting_start_release")

    if not connected:
        state = STATE_IDLE
        reason = "Kein Fahrzeug verbunden."
    elif real_charging:
        state = STATE_CHARGING
        reason = "Echte Ladung mit %.0f W bestaetigt." % real_power_w
    elif ended_latched:
        state = STATE_ENDED
        reason = (
            "Ladeende ist gelatcht; Neustart erst nach Umstecken, Moduswechsel "
            "oder neuer Nutzerfreigabe."
        )
    elif stop_active:
        state = STATE_STOPPING
        stop_reason = str(temporary_stop.get("reason") or "stop")
        reason = "Temporärer EMS-Stopp (%s); es wird keine neue openWB-Pro-Freigabe gesendet." % stop_reason
    elif priority_forced_stop:
        state = STATE_IDLE
        reason = "Startfreigabe ist durch die Regelung blockiert; es wird auf neue Freigabe gewartet."
    elif mode_off:
        state = STATE_IDLE
        reason = "Wallbox-Regelung ist aus; openWB Pro wird nur beobachtet."
    elif phase_wait:
        state = STATE_PHASE_WAIT
        reason = "Phasenwechsel läuft; Stromrampe wartet auf plausiblen Status."
    elif wakeup_pending:
        state = STATE_WAKEUP
        reason = "CP-Wake-up gesendet; Stromfreigabe wartet auf Einschaltverzoegerung."
    elif start_verifying:
        state = STATE_STARTING
        reason = "%d A freigegeben; openWB Pro wartet auf echte Ladeleistung." % offered_amp
    elif start_requested:
        state = STATE_OFFERED
        reason = "%d A freigegeben; noch keine echte Ladebestaetigung." % offered_amp
    elif temporary_waiting:
        state = STATE_IDLE
        reason = "Temporärer EMS-Stopp; wartet auf neue Startfreigabe oder Mindestleistung."
    else:
        state = STATE_IDLE
        reason = "Fahrzeug verbunden; keine Startfreigabe aktiv."

    start_blocked = bool(
        state in (STATE_STOPPING, STATE_ENDED)
        or mode_off
        or priority_forced_stop
        or temporary_stop.get("start_blocked", False)
    )
    can_send_start_command = bool(
        state in (STATE_OFFERED, STATE_STARTING, STATE_PHASE_WAIT)
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
        "budget_ready": bool(physical_budget_ready),
        "start_requested": bool(start_requested),
        "start_verifying": bool(start_verifying),
        "wakeup_pending": bool(wakeup_pending),
        "wakeup_remaining_s": (
            max(0.0, wakeup_wait_until - now)
            if wakeup_pending and now > 0.0
            else 0.0
        ),
        "start_hold_active": bool(start_hold),
        "phase_wait_active": bool(phase_wait),
        "phase_wait_target": int(data.get("_openwb_pro_phase_wait_target", 0) or 0),
        "phase_wait_since_s": (
            max(0.0, now - _safe_float(data.get("_openwb_pro_phase_wait_since", 0.0), 0.0))
            if phase_wait and now > 0.0 and _safe_float(data.get("_openwb_pro_phase_wait_since", 0.0), 0.0) > 0.0
            else 0.0
        ),
        "phase_wait_last_duration_s": _safe_float(data.get("_openwb_pro_phase_wait_last_duration_s", 0.0), 0.0),
        "phase_wait_last_result": str(data.get("_openwb_pro_phase_wait_last_result", "") or ""),
        "phase_wait_last_target": int(data.get("_openwb_pro_phase_wait_last_target", 0) or 0),
        "phase_wait_samples": int(data.get("_openwb_pro_phase_wait_samples", 0) or 0),
        "phase_wait_ema_s": _safe_float(data.get("_openwb_pro_phase_wait_ema_s", 0.0), 0.0),
        "phase_wait_max_s": _safe_float(data.get("_openwb_pro_phase_wait_max_s", 0.0), 0.0),
        "stop_active": bool(stop_active),
        "temporary_stop": temporary_stop,
        "temporary_stop_active": bool(temporary_stop.get("active", False)),
        "temporary_stop_reason": str(temporary_stop.get("reason") or ""),
        "temporary_stop_state_hint": stop_hint,
        "temporary_stop_waiting": bool(temporary_waiting),
        "last_start_age_s": last_start_age_s,
        "start_blocked": bool(start_blocked),
        "can_send_start_command": bool(can_send_start_command),
        "counts_as_real_charge": bool(real_charging),
    }


def apply_session_to_status(status: Optional[Dict[str, Any]], session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Attach openWB Pro session diagnostics to a status dict in-place."""

    if status is None:
        return None
    status["openwb_pro_contract"] = session.get("contract", CONTRACT_NAME)
    status["openwb_pro_runtime_path"] = "python_wallbox_manager"
    status["openwb_pro_session_guard_required"] = True
    status["openwb_pro_charge_verification_required"] = True
    status["openwb_pro_session_state"] = session.get("state", STATE_IDLE)
    status["openwb_pro_session_label"] = session.get("label", _state_label(STATE_IDLE))
    status["openwb_pro_session_level"] = session.get("level", _state_level(STATE_IDLE))
    status["openwb_pro_session_reason"] = session.get("reason", "")
    status["openwb_pro_session_offered_amp"] = int(session.get("offered_amp", 0) or 0)
    status["openwb_pro_session_budget_ready"] = bool(session.get("budget_ready", False))
    status["openwb_pro_session_start_requested"] = bool(session.get("start_requested", False))
    status["openwb_pro_session_start_verifying"] = bool(session.get("start_verifying", False))
    status["openwb_pro_session_wakeup_pending"] = bool(session.get("wakeup_pending", False))
    status["openwb_pro_session_wakeup_remaining_s"] = float(session.get("wakeup_remaining_s", 0.0) or 0.0)
    status["openwb_pro_session_start_hold_active"] = bool(session.get("start_hold_active", False))
    status["openwb_pro_session_phase_wait_active"] = bool(session.get("phase_wait_active", False))
    status["openwb_pro_session_phase_wait_target"] = int(session.get("phase_wait_target", 0) or 0)
    status["openwb_pro_session_phase_wait_since_s"] = float(session.get("phase_wait_since_s", 0.0) or 0.0)
    status["openwb_pro_session_phase_wait_last_duration_s"] = float(session.get("phase_wait_last_duration_s", 0.0) or 0.0)
    status["openwb_pro_session_phase_wait_last_result"] = str(session.get("phase_wait_last_result", "") or "")
    status["openwb_pro_session_phase_wait_last_target"] = int(session.get("phase_wait_last_target", 0) or 0)
    status["openwb_pro_session_phase_wait_samples"] = int(session.get("phase_wait_samples", 0) or 0)
    status["openwb_pro_session_phase_wait_ema_s"] = float(session.get("phase_wait_ema_s", 0.0) or 0.0)
    status["openwb_pro_session_phase_wait_max_s"] = float(session.get("phase_wait_max_s", 0.0) or 0.0)
    status["openwb_pro_session_stop_active"] = bool(session.get("stop_active", False))
    status["openwb_pro_temporary_stop_contract"] = session.get("temporary_stop", {})
    status["openwb_pro_temporary_stop_active"] = bool(session.get("temporary_stop_active", False))
    status["openwb_pro_temporary_stop_reason"] = str(session.get("temporary_stop_reason", "") or "")
    status["openwb_pro_temporary_stop_state_hint"] = str(session.get("temporary_stop_state_hint", "") or "")
    status["openwb_pro_temporary_stop_waiting"] = bool(session.get("temporary_stop_waiting", False))
    status["openwb_pro_session_start_blocked"] = bool(session.get("start_blocked", False))
    status["openwb_pro_session_can_send_start_command"] = bool(session.get("can_send_start_command", False))
    status["openwb_pro_session_counts_as_real_charge"] = bool(session.get("counts_as_real_charge", False))
    return status
