"""Reine Reservierungs- und Freigabeverträge pro Wallbox-Phasenübergang.

Das Modul besitzt bewusst kein Hardware- oder Datei-I/O. Es hält die
Energiebindung rund um einen Phasenübergang, während ``phase_sequencer``
die gerätespezifische elektromechanische Sequenz besitzt.
"""

from copy import deepcopy
import math
import uuid

try:
    from Installer import control_time
except ModuleNotFoundError:  # Native Ausführung mit Installer im sys.path
    import control_time  # type: ignore


ACTIVE_STAGES = frozenset({
    "await_budget", "ramp_to_zero", "zero_settle", "set_phase",
    "cp_interrupt", "restart_delay", "confirm_target", "recovery_hold",
})
TERMINAL_STAGES = frozenset({"completed", "aborted", "expired", "fault"})
VISIBLE_STAGES = frozenset(ACTIVE_STAGES | TERMINAL_STAGES)
DEFAULT_LEASE_S = 720.0
DEFAULT_COOLDOWN_S = 480.0
CONFIRM_FRAMES = 3
CONFIRM_S = 10.0
DISCONNECT_CONFIRM_S = 60.0
STATE_KEY = "_wallbox_phase_transition_reservation"
LEASE_TIMEBASE_KEY = "lease_timebase"
DISCONNECT_TIMEBASE_KEY = "disconnect_timebase"
STABLE_TIMEBASE_KEY = "stable_timebase"


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)


def _explicit_inactive(value):
    """Akzeptiert nur typisierte Inaktivwerte des Treibers, nie fehlende Daten."""

    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return math.isfinite(float(value)) and float(value) == 0.0
    return False


def _guard_begin(duration_s, clock_sample, role):
    if not isinstance(clock_sample, dict):
        return None
    guard = control_time.begin_guard(
        max(0.0, _float(duration_s, 0.0)),
        clock_sample,
        minimum_s=0.0,
    )
    guard["guard_role"] = str(role)
    return guard


def _guard_step(previous, duration_s, clock_sample, role):
    if not isinstance(clock_sample, dict):
        return None
    if isinstance(previous, dict):
        if (
            previous.get("schema_version") == control_time.GUARD_SCHEMA
            and previous.get("active") is False
            and _float(previous.get("remaining_s"), -1.0) <= 0.0
            and previous.get("valid") is True
            and previous.get("fail_closed") is not True
        ):
            guard = dict(previous)
            guard.update({
                "active": False,
                "remaining_s": 0.0,
                "sample": dict(clock_sample),
                "rearmed": False,
                "reason": "guard_elapsed",
            })
        else:
            guard = control_time.evaluate_guard(
                previous,
                clock_sample,
                minimum_s=0.0,
            )
    else:
        guard = _guard_begin(duration_s, clock_sample, role)
        guard["fail_closed"] = True
        guard["rearmed"] = True
        guard["reason"] = "phase_transition_timebase_incomplete"
    guard["guard_role"] = str(role)
    return guard


def normalize_current_step(value, default=1.0):
    step = _float(value, default)
    if not math.isfinite(step) or step <= 0.0:
        step = _float(default, 1.0)
    return max(0.1, min(16.0, round(step, 3)))


def status_power_w(status):
    data = status if isinstance(status, dict) else {}
    phase_sum = sum(abs(_float(data.get(key), 0.0)) for key in (
        "phase_power_l1_w", "phase_power_l2_w", "phase_power_l3_w",
    ))
    return max(
        0.0,
        abs(_float(data.get("real_power_w"), 0.0)),
        abs(_float(data.get("phase_power_sum_w"), 0.0)),
        abs(_float(data.get("power_w"), 0.0)),
        phase_sum,
    )


def status_phase_count(status):
    data = status if isinstance(status, dict) else {}
    measured = sum(
        1 for key in ("phase_power_l1_w", "phase_power_l2_w", "phase_power_l3_w")
        if abs(_float(data.get(key), 0.0)) > 100.0
    )
    if measured in (1, 2, 3):
        return measured
    for key in ("phase_actual_phases", "phases_actual", "phases_in_use"):
        phases = _int(data.get(key), 0)
        if phases in (1, 2, 3):
            return phases
    return 0


def planned_reservation_power_w(
    *, observed_before_w=0.0, restart_amp=6.0, target_phases=1,
    effective_w_per_amp=0.0, current_step_amp=1.0, safety_reserve_w=None,
    max_power_w=0.0,
):
    """Liefert die Wiederanlaufleistung einschließlich Mess- und Latenzreserve."""

    phases = _int(target_phases, 0)
    if phases not in (1, 3):
        return 0
    step = normalize_current_step(current_step_amp)
    w_per_amp = _float(effective_w_per_amp, 0.0)
    if w_per_amp <= 0.0:
        w_per_amp = 230.0 * phases
    restart = max(0.0, _float(restart_amp, 0.0)) * w_per_amp
    quantum_w = step * w_per_amp
    reserve = (
        max(150.0, 2.0 * quantum_w)
        if safety_reserve_w is None
        else max(0.0, _float(safety_reserve_w, 0.0))
    )
    requested = max(max(0.0, _float(observed_before_w, 0.0)), restart) + reserve
    maximum = max(0.0, _float(max_power_w, 0.0))
    if maximum > 0.0:
        requested = min(requested, maximum)
    return int(math.ceil(requested))


def begin_reservation(
    state, *, wb_id, from_phases, target_phases, restart_amp,
    current_step_amp=1.0, effective_w_per_amp=0.0, observed_before_w=0.0,
    now_ts=0.0, lease_s=DEFAULT_LEASE_S, cooldown_s=DEFAULT_COOLDOWN_S,
    owner="wallbox_manager", source="phase_command",
    reason_code="phase_transition", safety_reserve_w=None, max_power_w=0.0,
    transition_id=None, clock_sample=None,
):
    data = state if isinstance(state, dict) else {}
    target = _int(target_phases, 0)
    if target not in (1, 3):
        return {}
    now = _float(now_ts, 0.0)
    step = normalize_current_step(current_step_amp)
    w_per_amp = _float(effective_w_per_amp, 0.0) or 230.0 * target
    requested_w = planned_reservation_power_w(
        observed_before_w=observed_before_w,
        restart_amp=restart_amp,
        target_phases=target,
        effective_w_per_amp=w_per_amp,
        current_step_amp=step,
        safety_reserve_w=safety_reserve_w,
        max_power_w=max_power_w,
    )
    rid = str(transition_id or "wb%d-%s" % (max(0, _int(wb_id, 0)), uuid.uuid4().hex[:12]))
    lease_duration_s = max(120.0, _float(lease_s, DEFAULT_LEASE_S))
    lease_guard = _guard_begin(
        lease_duration_s,
        clock_sample,
        "reservation_lease",
    )
    reservation = {
        "schema_version": "wallbox_phase_transition_v2",
        "reservation_id": rid,
        "transition_id": rid,
        "wb_id": max(0, _int(wb_id, 0)),
        "owner": str(owner or "wallbox_manager"),
        "source": str(source or "phase_command"),
        "reason_code": str(reason_code or "phase_transition"),
        "reason": str(reason_code or "phase_transition"),
        "from_phases": _int(from_phases, 0),
        "target_phases": target,
        "restart_amp": round(max(0.0, _float(restart_amp, 0.0)), 3),
        "current_step_amp": step,
        "effective_w_per_amp": round(w_per_amp, 3),
        "observed_before_w": int(round(max(0.0, _float(observed_before_w, 0.0)))),
        "requested_w": requested_w,
        "reserved_w": requested_w,
        "granted_w": 0,
        "committed_w": 0,
        "grant_state": "waiting",
        "stage": "await_budget",
        "active": True,
        "started_ts": now,
        "lease_until_ts": now + lease_duration_s,
        "expires_ts": now + lease_duration_s,
        "cooldown_until_ts": now + max(DEFAULT_COOLDOWN_S, _float(cooldown_s, DEFAULT_COOLDOWN_S)),
        "committed_ts": 0.0,
        "confirmed_ts": 0.0,
        "stable_since_ts": 0.0,
        "valid_frames": 0,
        "disconnected_since_ts": 0.0,
        "data_quality": "pending",
        "last_valid_readback_ts": 0.0,
        "blocker": "await_storage_grant",
    }
    if lease_guard is not None:
        reservation[LEASE_TIMEBASE_KEY] = lease_guard
    data[STATE_KEY] = reservation
    return deepcopy(reservation)


def apply_grant(state, grant, *, now_ts=0.0, clock_sample=None):
    data = state if isinstance(state, dict) else {}
    current = data.get(STATE_KEY)
    reservation = dict(current) if isinstance(current, dict) else {}
    answer = grant if isinstance(grant, dict) else {}
    if not reservation:
        return {}
    if str(answer.get("reservation_id") or "") != str(reservation.get("reservation_id") or ""):
        return deepcopy(reservation)
    requested = max(0, _int(reservation.get("requested_w"), 0))
    granted = max(0, _int(answer.get("granted_w"), 0))
    state_name = str(answer.get("grant_state") or "waiting")
    lease = _float(answer.get("lease_until_ts"), 0.0)
    if lease > 0.0:
        current_lease = _float(
            reservation.get("lease_until_ts", reservation.get("expires_ts")),
            0.0,
        )
        current_guard = reservation.get(LEASE_TIMEBASE_KEY)
        incoming_revision = str(answer.get("lease_revision") or "")
        current_revision = str(reservation.get("lease_revision") or "")
        incoming_duration_s = _float(answer.get("lease_duration_s"), 0.0)
        same_lease = abs(lease - current_lease) <= 1e-6
        explicit_revision = bool(
            incoming_revision
            and incoming_revision != current_revision
            and incoming_duration_s > 0.0
        )
        if not isinstance(current_guard, dict):
            reservation["lease_until_ts"] = lease
            reservation["expires_ts"] = lease
        elif same_lease:
            reservation["lease_projection_reused"] = True
        elif explicit_revision:
            reservation["lease_until_ts"] = lease
            reservation["expires_ts"] = lease
            reservation["lease_revision"] = incoming_revision
        else:
            # Zyklisch neu projizierte Wallclock-Leases dürfen den bereits
            # laufenden monotonic Vertrag weder verlängern noch verkürzen.
            reservation["lease_projection_ignored"] = True
        if (
            isinstance(clock_sample, dict)
            and (
                not isinstance(current_guard, dict)
                or explicit_revision
            )
        ):
            duration_s = (
                incoming_duration_s
                if explicit_revision
                else max(
                    120.0,
                    lease - _float(reservation.get("started_ts"), 0.0),
                )
            )
            lease_guard = _guard_begin(
                duration_s,
                clock_sample,
                "reservation_lease",
            )
            if not isinstance(current_guard, dict):
                lease_guard.update({
                    "fail_closed": True,
                    "rearmed": True,
                    "reason": "legacy_lease_migrated",
                })
            reservation[LEASE_TIMEBASE_KEY] = lease_guard
    reservation["granted_w"] = granted
    reservation["grant_state"] = state_name
    reservation["blocker"] = str(answer.get("blocker") or answer.get("reason_code") or "")
    if granted >= requested and state_name in ("granted", "committed"):
        reservation["grant_state"] = state_name
        reservation["blocker"] = ""
    elif state_name not in ("expired", "rejected"):
        reservation["grant_state"] = "waiting"
    data[STATE_KEY] = reservation
    return deepcopy(reservation)


def grant_is_sufficient(reservation):
    item = reservation if isinstance(reservation, dict) else {}
    return bool(
        item.get("active")
        and max(0, _int(item.get("granted_w"), 0)) >= max(1, _int(item.get("requested_w"), 0))
        and str(item.get("grant_state") or "") in ("granted", "committed")
    )


def mark_committed(state, *, stage="ramp_to_zero", now_ts=0.0):
    data = state if isinstance(state, dict) else {}
    current = data.get(STATE_KEY)
    reservation = dict(current) if isinstance(current, dict) else {}
    if not grant_is_sufficient(reservation):
        return deepcopy(reservation)
    reservation["stage"] = str(stage if stage in VISIBLE_STAGES else "ramp_to_zero")
    reservation["grant_state"] = "committed"
    reservation["committed_w"] = max(
        _int(reservation.get("requested_w"), 0),
        _int(reservation.get("granted_w"), 0),
    )
    if _float(reservation.get("committed_ts"), 0.0) <= 0.0:
        reservation["committed_ts"] = _float(now_ts, 0.0)
    reservation["blocker"] = ""
    data[STATE_KEY] = reservation
    return deepcopy(reservation)


def set_stage(state, stage, *, now_ts=0.0, reason_code=None, deadline_ts=None):
    data = state if isinstance(state, dict) else {}
    current = data.get(STATE_KEY)
    reservation = dict(current) if isinstance(current, dict) else {}
    if not reservation:
        return {}
    stage_name = str(stage or "fault")
    reservation["stage"] = stage_name if stage_name in VISIBLE_STAGES else "fault"
    if reason_code:
        reservation["reason_code"] = str(reason_code)
        reservation["reason"] = str(reason_code)
    if deadline_ts is not None:
        reservation["stage_deadline_ts"] = max(0.0, _float(deadline_ts, 0.0))
    if reservation["stage"] in TERMINAL_STAGES:
        reservation["active"] = False
        reservation["completed_ts"] = _float(now_ts, 0.0)
    data[STATE_KEY] = reservation
    return deepcopy(reservation)


def reservation_lease_contract(reservation, *, now_ts=0.0, clock_sample=None):
    """Wertet die Lease autoritativ monotonic oder explizit als Legacy aus."""

    item = reservation if isinstance(reservation, dict) else {}
    now = _float(now_ts, 0.0)
    lease = _float(item.get("lease_until_ts", item.get("expires_ts")), 0.0)
    raw_guard = item.get(LEASE_TIMEBASE_KEY)
    guard = None
    source = "legacy_wallclock"
    if isinstance(raw_guard, dict):
        source = "monotonic_guard"
        if isinstance(clock_sample, dict):
            guard = _guard_step(
                raw_guard,
                max(120.0, lease - _float(item.get("started_ts"), 0.0)),
                clock_sample,
                "reservation_lease",
            )
        else:
            guard = dict(raw_guard)
            guard.update({
                "active": True,
                "fail_closed": True,
                "reason": "phase_transition_current_timebase_missing",
            })
    expired = bool(
        item
        and (
            guard.get("active") is False
            if isinstance(guard, dict)
            else lease > 0.0 and now >= lease
        )
    )
    return {
        "contract": "wallbox_phase_reservation_lease_v1",
        "expired": expired,
        "active": bool(item and not expired),
        "timebase_unbound": bool(
            isinstance(guard, dict) and guard.get("fail_closed") is True
        ),
        "source": source,
        "guard": guard,
        "lease_until_ts": lease,
        "wall_ts": now,
    }


def update_reservation(
    state,
    *,
    status=None,
    now_ts=0.0,
    connected=None,
    clock_sample=None,
):
    data = state if isinstance(state, dict) else {}
    current = data.get(STATE_KEY)
    reservation = dict(current) if isinstance(current, dict) else {}
    now = _float(now_ts, 0.0)
    if not reservation:
        return inactive_reservation("not_active")

    lease = _float(reservation.get("lease_until_ts", reservation.get("expires_ts")), 0.0)
    lease_duration_s = max(
        120.0,
        lease - _float(reservation.get("started_ts"), 0.0),
    )
    lease_guard = None
    if isinstance(clock_sample, dict):
        lease_guard = _guard_step(
            reservation.get(LEASE_TIMEBASE_KEY),
            lease_duration_s,
            clock_sample,
            "reservation_lease",
        )
        reservation[LEASE_TIMEBASE_KEY] = lease_guard
    elif isinstance(reservation.get(LEASE_TIMEBASE_KEY), dict):
        # Ein gebundener Vertrag darf bei fehlender aktueller Zeitprobe nie auf
        # die Wallclock zurückfallen.
        lease_guard = dict(reservation[LEASE_TIMEBASE_KEY])
        lease_guard.update({
            "active": True,
            "fail_closed": True,
            "reason": "phase_transition_current_timebase_missing",
        })
        reservation[LEASE_TIMEBASE_KEY] = lease_guard

    lease_elapsed = bool(
        lease_guard.get("active") is False
        if isinstance(lease_guard, dict)
        else lease > 0.0 and now >= lease
    )
    timebase_uncertain = bool(
        isinstance(lease_guard, dict)
        and lease_guard.get("fail_closed") is True
    )
    output_bound = bool(
        _int(reservation.get("committed_w"), 0) > 0
        or _float(reservation.get("committed_ts"), 0.0) > 0.0
        or str(reservation.get("stage") or "")
        in ("request_output", "phase_switch", "cooldown", "confirming")
    )
    if timebase_uncertain and output_bound:
        reservation.update({
            "active": True,
            "stage": "recovery_hold",
            "blocker": "phase_transition_timebase_unbound",
        })
        if _int(reservation.get("committed_w"), 0) > 0:
            reservation["grant_state"] = "committed"
        data[STATE_KEY] = reservation
        return public_reservation(reservation, now)
    if lease_elapsed:
        max_recovery_s = max(600.0, _float(reservation.get("cooldown_until_ts", 0.0) - lease, 600.0))
        recovery_expired = bool(lease > 0.0 and now >= lease + max_recovery_s)
        if output_bound and not recovery_expired:
            reservation.update({
                "active": True,
                "stage": "recovery_hold",
                "blocker": "lease_elapsed_output_bound",
            })
            if _int(reservation.get("committed_w"), 0) > 0:
                reservation["grant_state"] = "committed"
        else:
            reservation.update({
                "active": False,
                "stage": "recovery_hold",
                "grant_state": "expired",
                "blocker": "lease_expired",
            })
        data[STATE_KEY] = reservation
        return public_reservation(reservation, now)

    if connected is False:
        since = _float(reservation.get("disconnected_since_ts"), 0.0)
        disconnect_guard = reservation.get(DISCONNECT_TIMEBASE_KEY)
        if isinstance(clock_sample, dict):
            if not isinstance(disconnect_guard, dict):
                disconnect_guard = _guard_begin(
                    DISCONNECT_CONFIRM_S,
                    clock_sample,
                    "disconnect_confirm",
                )
            else:
                disconnect_guard = _guard_step(
                    disconnect_guard,
                    DISCONNECT_CONFIRM_S,
                    clock_sample,
                    "disconnect_confirm",
                )
            reservation[DISCONNECT_TIMEBASE_KEY] = disconnect_guard
        if since <= 0.0:
            reservation["disconnected_since_ts"] = now
        disconnect_confirmed = bool(
            disconnect_guard.get("active") is False
            if isinstance(disconnect_guard, dict)
            else since > 0.0 and now - since >= DISCONNECT_CONFIRM_S
        )
        if disconnect_confirmed and str(reservation.get("stage")) == "await_budget":
            reservation.update({"active": False, "stage": "aborted", "grant_state": "rejected", "blocker": "vehicle_disconnected"})
    elif connected is True:
        reservation["disconnected_since_ts"] = 0.0
        reservation.pop(DISCONNECT_TIMEBASE_KEY, None)

    st = status if isinstance(status, dict) else {}
    valid = bool(st) and st.get("driver_status_valid") is not False and st.get("driver_status_stale") is not True
    if valid:
        reservation["data_quality"] = "valid"
        reservation["last_valid_readback_ts"] = now
    elif st:
        reservation["data_quality"] = "invalid"

    power_w = status_power_w(st)
    phases = status_phase_count(st)
    confirmed = bool(valid and power_w > 500.0 and phases == _int(reservation.get("target_phases"), 0))
    if confirmed:
        reservation["valid_frames"] = _int(reservation.get("valid_frames"), 0) + 1
        if _float(reservation.get("stable_since_ts"), 0.0) <= 0.0:
            reservation["stable_since_ts"] = now
        stable_guard = reservation.get(STABLE_TIMEBASE_KEY)
        if isinstance(clock_sample, dict):
            if not isinstance(stable_guard, dict):
                stable_guard = _guard_begin(
                    CONFIRM_S,
                    clock_sample,
                    "target_stable_confirm",
                )
            else:
                stable_guard = _guard_step(
                    stable_guard,
                    CONFIRM_S,
                    clock_sample,
                    "target_stable_confirm",
                )
            reservation[STABLE_TIMEBASE_KEY] = stable_guard
        stable_confirmed = bool(
            stable_guard.get("active") is False
            if isinstance(stable_guard, dict)
            else now - _float(reservation.get("stable_since_ts"), now) >= CONFIRM_S
        )
        if (
            _int(reservation.get("valid_frames"), 0) >= CONFIRM_FRAMES
            and stable_confirmed
        ):
            reservation.update({
                "active": False, "stage": "completed", "grant_state": "expired",
                "confirmed_ts": now, "blocker": "",
            })
    elif valid:
        reservation["valid_frames"] = 0
        reservation["stable_since_ts"] = 0.0
        reservation.pop(STABLE_TIMEBASE_KEY, None)

    data[STATE_KEY] = reservation
    return public_reservation(reservation, now)


def _evidence_generation_match(evidence_id, reservation_id):
    """Bindet eine persistierte Ausgangs-ID an genau eine Reservierung.

    ``None`` bedeutet, dass ein älteres Schema keine eindeutige ID liefert und
    deshalb weiterhin konservativ als möglicherweise zugehörig gilt.
    """

    evidence = str(evidence_id or "")
    reservation = str(reservation_id or "")
    if not evidence or not reservation:
        return None
    return bool(evidence == reservation or evidence.startswith(reservation + ":"))


def output_evidence_binding_contract(
    reservation,
    *,
    sequence=None,
    output_intent=None,
    output_ack=None,
    recovery_hold=None,
    restart_authorized=None,
):
    """Ordnet persistierte Phasenausgänge ihrer Reservierung zu.

    Vor älteren Managerständen konnten ein bereits bestätigter Ausgang und
    eine spätere, noch uncommittete Reservierung gemeinsam gespeichert werden.
    Beim Neustart durfte daraus keine künstliche Mischgeneration entstehen.
    Nur explizit fremde Evidenz wird verworfen; bei fehlenden IDs bleibt der
    Vertrag bewusst fail-closed.
    """

    item = reservation if isinstance(reservation, dict) else {}
    reservation_id = str(
        item.get("transition_id") or item.get("reservation_id") or ""
    )
    reservation_target = _int(item.get("target_phases"), 0)

    intent = output_intent if isinstance(output_intent, dict) else {}
    intent_id = str(intent.get("intent_id") or "")
    intent_match = _evidence_generation_match(
        intent.get("transition_id") or intent_id,
        reservation_id,
    )
    ack = output_ack if isinstance(output_ack, dict) else {}
    ack_id = str(ack.get("intent_id") or "")
    supported_intent = bool(
        str(intent.get("schema") or "")
        == "openwb_pro_phase_output_intent_v1"
        and (
            (
                str(intent.get("action") or "") == "send_zero"
                and str(intent.get("method") or "") == "set_amp_and_state"
            )
            or (
                str(intent.get("action") or "") == "send_phase"
                and str(intent.get("method") or "") == "set_phases"
            )
        )
    )
    closed_foreign_pair = bool(
        intent
        and ack
        and intent_match is False
        and supported_intent
        and str(ack.get("schema") or "") == "openwb_pro_phase_output_ack_v1"
        and intent_id
        and ack_id == intent_id
        and ack.get("success") is True
    )
    intent_unrelated = bool(closed_foreign_pair)
    ack_unrelated = bool(closed_foreign_pair)
    intent_bound = bool(intent and not closed_foreign_pair)
    ack_bound = bool(ack and not closed_foreign_pair)

    seq = sequence if isinstance(sequence, dict) else {}
    sequence_unrelated = False
    sequence_bound = bool(seq)

    recovery = recovery_hold if isinstance(recovery_hold, dict) else {}
    recovery_unrelated = False
    recovery_bound = bool(recovery)

    restart = restart_authorized if isinstance(restart_authorized, dict) else {}
    restart_unrelated = False
    restart_bound = bool(restart)

    return {
        "contract": "wallbox_phase_output_generation_binding_v1",
        "reservation_id": reservation_id,
        "reservation_target": reservation_target,
        "closed_foreign_intent_ack_pair": closed_foreign_pair,
        "output_intent_bound": intent_bound,
        "output_intent_ignored_unrelated": intent_unrelated,
        "output_ack_bound": ack_bound,
        "output_ack_ignored_unrelated": ack_unrelated,
        "sequence_bound": sequence_bound,
        "sequence_ignored_unrelated": sequence_unrelated,
        "recovery_hold_bound": recovery_bound,
        "recovery_hold_ignored_unrelated": recovery_unrelated,
        "restart_authorized_bound": restart_bound,
        "restart_authorized_ignored_unrelated": restart_unrelated,
    }


def preoutput_supersession_evidence_contract(
    reservation,
    *,
    sequence=None,
    output_intent=None,
    output_ack=None,
    recovery_hold=None,
    restart_authorized=None,
    wakeup_active=False,
):
    """Belegt eine noch vollständig ausgangslose ``await_budget``-Generation."""

    item = reservation if isinstance(reservation, dict) else {}
    binding = output_evidence_binding_contract(
        item,
        sequence=sequence,
        output_intent=output_intent,
        output_ack=output_ack,
        recovery_hold=recovery_hold,
        restart_authorized=restart_authorized,
    )
    reservation_id = str(
        item.get("transition_id") or item.get("reservation_id") or ""
    )
    target = _int(item.get("target_phases"), 0)
    no_output_binding = not any((
        binding.get("sequence_bound") is True,
        binding.get("output_intent_bound") is True,
        binding.get("output_ack_bound") is True,
        binding.get("recovery_hold_bound") is True,
        binding.get("restart_authorized_bound") is True,
        bool(wakeup_active),
    ))
    eligible = bool(
        item.get("active") is True
        and str(item.get("stage") or "") == "await_budget"
        and reservation_id
        and target in (1, 3)
        and max(0, _int(item.get("committed_w"), 0)) == 0
        and _float(item.get("committed_ts"), 0.0) <= 0.0
        and max(0, _int(item.get("valid_frames"), 0)) == 0
        and no_output_binding
    )
    return {
        "schema_version": "wallbox_phase_preoutput_supersession_v1",
        "eligible": eligible,
        "reservation_id": reservation_id,
        "target_phases": target,
        "committed_w": max(0, _int(item.get("committed_w"), 0)),
        "committed_ts": max(0.0, _float(item.get("committed_ts"), 0.0)),
        "valid_frames": max(0, _int(item.get("valid_frames"), 0)),
        "sequence_bound": bool(binding.get("sequence_bound")),
        "output_intent_bound": bool(binding.get("output_intent_bound")),
        "output_ack_bound": bool(binding.get("output_ack_bound")),
        "recovery_hold_bound": bool(binding.get("recovery_hold_bound")),
        "restart_authorized_bound": bool(binding.get("restart_authorized_bound")),
        "wakeup_active": bool(wakeup_active),
    }


def preoutput_supersession_evidence_is_bound(reservation):
    """Validiert die über die Prozessgrenze transportierte Preoutput-Evidenz."""

    item = reservation if isinstance(reservation, dict) else {}
    proof = item.get("preoutput_supersession_evidence")
    proof = proof if isinstance(proof, dict) else {}
    reservation_id = str(
        item.get("transition_id") or item.get("reservation_id") or ""
    )
    return bool(
        proof.get("schema_version") == "wallbox_phase_preoutput_supersession_v1"
        and proof.get("eligible") is True
        and reservation_id
        and item.get("active") is True
        and str(item.get("stage") or "") == "await_budget"
        and str(proof.get("reservation_id") or "") == reservation_id
        and _int(proof.get("target_phases"), 0) == _int(item.get("target_phases"), 0)
        and max(0, _int(item.get("committed_w"), 0)) == 0
        and _float(item.get("committed_ts"), 0.0) <= 0.0
        and max(0, _int(item.get("valid_frames"), 0)) == 0
        and max(0, _int(proof.get("committed_w"), 0)) == 0
        and _float(proof.get("committed_ts"), 0.0) <= 0.0
        and max(0, _int(proof.get("valid_frames"), 0)) == 0
        and not any(
            proof.get(key) is True
            for key in (
                "sequence_bound",
                "output_intent_bound",
                "output_ack_bound",
                "recovery_hold_bound",
                "restart_authorized_bound",
                "wakeup_active",
            )
        )
    )


def expiration_resolution_contract(
    reservation,
    *,
    status=None,
    now_ts=0.0,
    sequence=None,
    output_intent=None,
    output_ack=None,
    recovery_hold=None,
    restart_authorized=None,
    wakeup_active=False,
    clock_sample=None,
):
    """Klassifiziert, ob eine abgelaufene Reservierung ohne I/O entfernt werden kann.

    Nur eine ungebundene Reservierung, die nie einen Geräteausgang besaß, darf
    beendet werden. Eine stale Wiederanlauffreigabe einer älteren, bereits
    bestätigten Phasengeneration ist nur dann harmlos, wenn ein frischer Idle-
    Readback weiterhin dieselbe Zielphase ausweist und CP explizit inaktiv ist.
    Jede mehrdeutige Ausgangsgeneration bleibt in ``recovery_hold`` fail-closed.
    """

    item = dict(reservation) if isinstance(reservation, dict) else {}
    st = status if isinstance(status, dict) else {}
    now = _float(now_ts, 0.0)
    lease = _float(item.get("lease_until_ts", item.get("expires_ts")), 0.0)
    raw_lease_guard = item.get(LEASE_TIMEBASE_KEY)
    lease_guard = None
    if isinstance(raw_lease_guard, dict) and isinstance(clock_sample, dict):
        lease_guard = _guard_step(
            raw_lease_guard,
            max(120.0, lease - _float(item.get("started_ts"), 0.0)),
            clock_sample,
            "reservation_lease",
        )
    elif isinstance(raw_lease_guard, dict):
        lease_guard = dict(raw_lease_guard)
        lease_guard.update({
            "active": True,
            "fail_closed": True,
            "reason": "phase_transition_current_timebase_missing",
        })
    lease_timebase_unbound = bool(
        isinstance(lease_guard, dict)
        and lease_guard.get("fail_closed") is True
    )
    lease_expired = bool(
        item
        and (
            lease_guard.get("active") is False
            if isinstance(lease_guard, dict)
            else lease > 0.0 and now >= lease
        )
    )
    status_valid = bool(
        st
        and st.get("driver_status_valid") is True
        and st.get("driver_status_stale") is not True
        and st.get("driver_status_degraded") is not True
        and st.get("driver_status_glitch") is not True
    )
    cp_inactive = _explicit_inactive(st.get("cp_interrupt_isactive"))
    idle_readback = bool(
        status_valid
        and cp_inactive
        and not bool(st.get("charging", st.get("charge_state", False)))
        and status_power_w(st) <= 50.0
        and max(
            _float(st.get("amp"), 0.0),
            _float(st.get("offered_current_raw"), 0.0),
            _float(st.get("evse_current"), 0.0),
        ) <= 0.0
    )
    # ``granted_w`` bestätigt nur die Wattzuteilung des Storage Managers und
    # ist ausdrücklich noch kein Geräteausgang. Erst ein Commit, bestätigte
    # Leistungsframes oder gebundene Output-Evidenz machen die Generation
    # hardwarewirksam. Andernfalls würde ein nie ausgeführter Grant nach
    # Lease-Ablauf dauerhaft als aktive Transition hängen bleiben.
    uncommitted = bool(
        max(0, _int(item.get("committed_w"), 0)) == 0
        and _float(item.get("committed_ts"), 0.0) <= 0.0
        and _int(item.get("valid_frames"), 0) == 0
    )
    generation_binding = output_evidence_binding_contract(
        item,
        sequence=sequence,
        output_intent=output_intent,
        output_ack=output_ack,
        recovery_hold=recovery_hold,
        restart_authorized=restart_authorized,
    )
    sequence_present = bool(generation_binding["sequence_bound"])
    intent_present = bool(generation_binding["output_intent_bound"])
    ack_present = bool(generation_binding["output_ack_bound"])
    recovery = recovery_hold if isinstance(recovery_hold, dict) else {}
    recovery_present = bool(generation_binding["recovery_hold_bound"])

    restart = restart_authorized if isinstance(restart_authorized, dict) else {}
    restart_present = bool(generation_binding["restart_authorized_bound"])
    restart_target = _int(restart.get("target"), 0)
    readback_target = 0
    for key in ("phases_target", "target_phases", "phase_wallbox_phases"):
        candidate = _int(st.get(key), 0)
        if candidate in (1, 3):
            readback_target = candidate
            break
    if readback_target == 0 and isinstance(st.get("phase_contract"), dict):
        candidate = _int(st["phase_contract"].get("target_phases"), 0)
        if candidate in (1, 3):
            readback_target = candidate
    restart_redundant = bool(
        restart_present
        and restart.get("active") is True
        and restart_target in (1, 3)
        and readback_target == restart_target
        and idle_readback
    )
    recovery_redundant = bool(
        recovery_present
        and str(recovery.get("recovery_class") or "")
        == "expired_uncommitted_no_output"
        and not sequence_present
        and not intent_present
        and not ack_present
        and not bool(wakeup_active)
        and (not restart_present or restart_redundant)
    )
    ambiguous_output = bool(
        sequence_present
        or intent_present
        or ack_present
        or (recovery_present and not recovery_redundant)
        or bool(wakeup_active)
        or (restart_present and not restart_redundant)
    )

    action = "keep"
    reason = "reservation_active"
    if not item:
        action = "none"
        reason = "not_active"
    elif not lease_expired:
        reason = (
            "phase_transition_timebase_unbound"
            if lease_timebase_unbound
            else "lease_not_expired"
        )
    elif not uncommitted:
        reason = "expired_output_committed"
    elif not status_valid:
        reason = "fresh_status_required"
    elif not cp_inactive:
        reason = "cp_state_not_inactive"
    elif ambiguous_output:
        reason = "expired_output_ambiguous"
    elif not idle_readback:
        reason = "idle_zero_readback_required"
    else:
        action = "terminalize"
        reason = "expired_uncommitted_idle_confirmed"

    return {
        "contract": "wallbox_phase_expiration_resolution_v1",
        "action": action,
        "reason": reason,
        "terminal": action == "terminalize",
        "lease_expired": lease_expired,
        "lease_timebase_unbound": lease_timebase_unbound,
        "lease_timebase": lease_guard,
        "uncommitted": uncommitted,
        "status_valid": status_valid,
        "cp_inactive": cp_inactive,
        "idle_readback": idle_readback,
        "ambiguous_output": ambiguous_output,
        "restart_authorized_present": restart_present,
        "restart_authorized_redundant": restart_redundant,
        "recovery_hold_present": recovery_present,
        "recovery_hold_redundant": recovery_redundant,
        "wakeup_active": bool(wakeup_active),
        "restart_target": restart_target,
        "readback_target": readback_target,
        "output_intent_bound": bool(generation_binding["output_intent_bound"]),
        "output_intent_ignored_unrelated": bool(
            generation_binding["output_intent_ignored_unrelated"]
        ),
        "output_ack_bound": bool(generation_binding["output_ack_bound"]),
        "output_ack_ignored_unrelated": bool(
            generation_binding["output_ack_ignored_unrelated"]
        ),
        "sequence_bound": bool(generation_binding["sequence_bound"]),
        "sequence_ignored_unrelated": bool(
            generation_binding["sequence_ignored_unrelated"]
        ),
        "recovery_hold_bound": bool(generation_binding["recovery_hold_bound"]),
        "recovery_hold_ignored_unrelated": bool(
            generation_binding["recovery_hold_ignored_unrelated"]
        ),
        "restart_authorized_bound": bool(
            generation_binding["restart_authorized_bound"]
        ),
        "restart_authorized_ignored_unrelated": bool(
            generation_binding["restart_authorized_ignored_unrelated"]
        ),
        "generation_binding": generation_binding,
        "ts": now,
    }


def inactive_reservation(reason="not_active"):
    return {
        "active": False, "stage": "idle", "reserved_w": 0, "requested_w": 0,
        "granted_w": 0, "committed_w": 0, "target_phases": 0,
        "remaining_s": 0.0, "reason": str(reason), "reason_code": str(reason),
    }


def public_reservation(reservation, now_ts=0.0):
    item = dict(reservation) if isinstance(reservation, dict) else {}
    if not item:
        return inactive_reservation()
    lease = _float(item.get("lease_until_ts", item.get("expires_ts")), 0.0)
    lease_guard = item.get(LEASE_TIMEBASE_KEY)
    if isinstance(lease_guard, dict):
        item["remaining_s"] = max(
            0.0,
            _float(lease_guard.get("remaining_s"), 0.0),
        )
    else:
        item["remaining_s"] = max(0.0, lease - _float(now_ts, 0.0)) if lease > 0.0 else 0.0
    item["reserved_w"] = max(0, _int(item.get("requested_w", item.get("reserved_w")), 0)) if item.get("active") else 0
    return deepcopy(item)


def rehydrate_reservation(raw, *, now_ts=0.0, clock_sample=None):
    item = dict(raw) if isinstance(raw, dict) else {}
    if not item:
        return {}
    now = _float(now_ts, 0.0)
    lease = _float(item.get("lease_until_ts", item.get("expires_ts")), 0.0)
    cooldown = _float(item.get("cooldown_until_ts"), 0.0)
    raw_guard = item.get(LEASE_TIMEBASE_KEY)
    if isinstance(raw_guard, dict):
        lease_contract = reservation_lease_contract(
            item,
            now_ts=now,
            clock_sample=clock_sample,
        )
        guard = lease_contract.get("guard")
        if isinstance(guard, dict):
            item[LEASE_TIMEBASE_KEY] = guard
        if lease_contract.get("expired") is True and cooldown <= now:
            return {}
        item["active"] = True
        item["stage"] = "recovery_hold"
        item["blocker"] = (
            "manager_restart_timebase_recovery"
            if lease_contract.get("timebase_unbound")
            else "manager_restart_recovery"
        )
        return item
    if isinstance(clock_sample, dict) and item.get("active"):
        duration_s = max(120.0, lease - _float(item.get("started_ts"), 0.0))
        guard = _guard_begin(
            duration_s,
            clock_sample,
            "reservation_lease",
        )
        guard.update({
            "fail_closed": True,
            "rearmed": True,
            "reason": "legacy_lease_rehydrated",
        })
        item[LEASE_TIMEBASE_KEY] = guard
        item["stage"] = "recovery_hold"
        item["blocker"] = "manager_restart_timebase_recovery"
        return item
    if lease <= now and cooldown <= now:
        return {}
    if item.get("active") and lease > now:
        item["stage"] = "recovery_hold"
        item["blocker"] = "manager_restart_recovery"
    return item


def aggregate_reservations(chargers, *, now_ts=0.0):
    reservations = []
    for box in chargers if isinstance(chargers, (list, tuple)) else ():
        raw = box.get(STATE_KEY) if isinstance(box, dict) else None
        item = public_reservation(raw, now_ts) if isinstance(raw, dict) else {}
        if item.get("active"):
            reservations.append(item)
    requested_total = sum(max(0, _int(item.get("requested_w"), 0)) for item in reservations)
    granted_total = sum(max(0, _int(item.get("granted_w"), 0)) for item in reservations)
    committed_total = sum(max(0, _int(item.get("committed_w"), 0)) for item in reservations)
    return {
        "schema_version": "wallbox_phase_transition_aggregate_v2",
        "active": bool(reservations),
        "reservations": reservations,
        "requested_w_total": requested_total,
        "granted_w_total": granted_total,
        "committed_w_total": committed_total,
        "reserved_w": requested_total,
        "charger_ids": [max(0, _int(item.get("wb_id"), 0)) for item in reservations],
        "targets": sorted({_int(item.get("target_phases"), 0) for item in reservations if _int(item.get("target_phases"), 0) in (1, 3)}),
        "started_ts": min((_float(item.get("started_ts"), 0.0) for item in reservations), default=0.0),
        "expires_ts": max((_float(item.get("lease_until_ts"), 0.0) for item in reservations), default=0.0),
    }


def arbitrate_grants(
    requests, *, available_w, heatpump_running=False,
    heatpump_running_commitment_w=0, safety_margin_w=0, now_ts=0.0,
):
    """Grant committed transitions first and only fully grant new requests."""

    now = _float(now_ts, 0.0)
    flexible = max(
        0,
        _int(available_w, 0)
        - (max(0, _int(heatpump_running_commitment_w, 0)) if heatpump_running else 0)
        - max(0, _int(safety_margin_w, 0)),
    )
    items = [dict(item) for item in (requests or []) if isinstance(item, dict)]
    items.sort(key=lambda item: (str(item.get("grant_state")) != "committed", _float(item.get("started_ts"), 0.0), _int(item.get("wb_id"), 0)))
    grants = []
    for item in items:
        requested = max(0, _int(item.get("requested_w"), 0))
        committed = str(item.get("grant_state")) == "committed" or max(0, _int(item.get("committed_w"), 0)) > 0
        if committed:
            granted = requested
            grant_state = "committed"
            blocker = ""
            flexible = max(0, flexible - granted)
        elif requested > 0 and flexible >= requested:
            granted = requested
            grant_state = "granted"
            blocker = ""
            flexible -= granted
        else:
            granted = 0
            grant_state = "waiting"
            blocker = "running_heatpump_commitment" if heatpump_running else "insufficient_headroom"
        grants.append({
            "reservation_id": str(item.get("reservation_id") or item.get("transition_id") or ""),
            "wb_id": max(0, _int(item.get("wb_id"), 0)),
            "requested_w": requested,
            "granted_w": granted,
            "grant_state": grant_state,
            "lease_until_ts": _float(item.get("lease_until_ts", item.get("expires_ts")), now),
            "blocker": blocker,
            "reason_code": blocker or "phase_transition_granted",
        })
    return {
        "schema_version": "wallbox_phase_transition_grants_v2",
        "grants": grants,
        "reserved_w_total": sum(item["granted_w"] for item in grants),
        "flexible_budget_after_commitments_w": flexible,
        "heatpump_running": bool(heatpump_running),
        "heatpump_running_commitment_w": max(0, _int(heatpump_running_commitment_w, 0)),
    }


def status_dimensions(reservation, *, charge_truth="unknown", inhibit_owner="none", now_ts=0.0):
    item = reservation if isinstance(reservation, dict) else {}
    now = _float(now_ts, 0.0)
    stage = str(item.get("stage") or "idle")
    visible_stage = stage if stage in VISIBLE_STAGES else "idle"
    lease = _float(item.get("lease_until_ts", item.get("expires_ts")), 0.0)
    stage_deadline = _float(item.get("stage_deadline_ts"), lease)
    cooldown_until = _float(item.get("cooldown_until_ts"), 0.0)
    lease_guard = item.get(LEASE_TIMEBASE_KEY)
    transition_remaining_s = (
        max(0.0, _float(lease_guard.get("remaining_s"), 0.0))
        if isinstance(lease_guard, dict)
        else max(0.0, stage_deadline - now)
        if stage_deadline > 0.0 and item.get("active")
        else 0.0
    )
    return {
        "transition_state": {
            "active": bool(item.get("active")),
            "state": visible_stage,
            "transition_id": str(item.get("transition_id") or item.get("reservation_id") or ""),
            "target_phases": _int(item.get("target_phases"), 0),
            "deadline_ts": stage_deadline,
            "remaining_s": transition_remaining_s,
            "reason_code": str(item.get("reason_code") or ""),
        },
        "phase_cooldown": {
            "active": cooldown_until > now,
            "remaining_s": max(0.0, cooldown_until - now),
            "until_ts": cooldown_until,
        },
        "charge_truth": str(charge_truth or "unknown"),
        "inhibit_owner": str(inhibit_owner or "none"),
    }


def normalize_charge_truth(raw_truth, *, connected=None, offered_amp=0.0, status_valid=True):
    """Ordnet ältere und transiente Labels der unabhängigen öffentlichen Wahrheitsdimension zu."""

    if not status_valid:
        return "unknown"
    truth = str(raw_truth or "").strip().lower()
    if truth in ("charging", "finished", "unknown", "disconnected", "connected", "offered", "not_charging"):
        return truth
    if truth in ("stop_pending", "phase_wait", "temporary_stop", "paused"):
        return "not_charging"
    if connected is False:
        return "disconnected"
    if _float(offered_amp, 0.0) > 0.0:
        return "offered"
    if connected is True:
        return "connected"
    return "unknown"


def explicit_inhibit_owner(evidence, *, status_valid=True):
    """Liefert einen Sperr-Owner nur bei maschinenlesbarem Quellnachweis."""

    if not status_valid:
        return "none"
    data = evidence if isinstance(evidence, dict) else {}
    owner = str(data.get("inhibit_owner") or data.get("owner") or "none").strip().lower()
    allowed = {"none", "user_off", "external_master", "budget", "reserve", "device_fault", "safety"}
    if owner not in allowed:
        return "none"
    if owner == "external_master" and not (
        data.get("explicit_external_command")
        or data.get("external_owner_evidence")
        or data.get("command_audit_id")
    ):
        return "none"
    return owner


__all__ = [
    "ACTIVE_STAGES", "TERMINAL_STAGES", "STATE_KEY", "LEASE_TIMEBASE_KEY",
    "DISCONNECT_TIMEBASE_KEY", "STABLE_TIMEBASE_KEY", "aggregate_reservations",
    "apply_grant", "arbitrate_grants", "begin_reservation", "grant_is_sufficient",
    "expiration_resolution_contract", "mark_committed", "planned_reservation_power_w", "public_reservation",
    "preoutput_supersession_evidence_contract", "preoutput_supersession_evidence_is_bound",
    "rehydrate_reservation", "reservation_lease_contract", "set_stage", "status_dimensions", "update_reservation",
    "normalize_charge_truth", "explicit_inhibit_owner",
]
