"""Minimal F040/F041 wallbox transition budget contract.

This R0 module is deliberately pure: it performs no file, network or device
I/O and exists only to keep a confirmed running heat-pump commitment ahead of
a new wallbox start or phase change.
"""

import math


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
    current_step_amp=1.0, safety_reserve_w=None, max_power_w=0.0,
):
    phases = _int(target_phases, 0)
    if phases not in (1, 3):
        return 0
    step = max(0.1, min(16.0, _float(current_step_amp, 1.0)))
    w_per_amp = 230.0 * phases
    reserve = max(150.0, 2.0 * step * w_per_amp) if safety_reserve_w is None else max(0.0, _float(safety_reserve_w, 0.0))
    requested = max(max(0.0, _float(observed_before_w, 0.0)), max(0.0, _float(restart_amp, 0.0)) * w_per_amp) + reserve
    maximum = max(0.0, _float(max_power_w, 0.0))
    if maximum > 0.0:
        requested = min(requested, maximum)
    return int(math.ceil(requested))


def build_request(
    *, wb_id, from_phases, target_phases, restart_amp,
    current_step_amp=1.0, observed_before_w=0.0, max_power_w=0.0,
):
    requested_w = planned_reservation_power_w(
        observed_before_w=observed_before_w,
        restart_amp=restart_amp,
        target_phases=target_phases,
        current_step_amp=current_step_amp,
        max_power_w=max_power_w,
    )
    return {
        "reservation_id": "wb%d-f040" % max(0, _int(wb_id, 0)),
        "wb_id": max(0, _int(wb_id, 0)),
        "from_phases": _int(from_phases, 0),
        "target_phases": _int(target_phases, 0),
        "requested_w": requested_w,
    }


def arbitrate_grants(
    requests, *, available_w, heatpump_running=False,
    heatpump_running_commitment_w=0, safety_margin_w=0,
):
    flexible = max(
        0,
        _int(available_w, 0)
        - (max(0, _int(heatpump_running_commitment_w, 0)) if heatpump_running else 0)
        - max(0, _int(safety_margin_w, 0)),
    )
    grants = []
    for item in (requests or []):
        request = item if isinstance(item, dict) else {}
        requested = max(0, _int(request.get("requested_w"), 0))
        if requested > 0 and flexible >= requested:
            granted, state, blocker = requested, "granted", ""
            flexible -= requested
        else:
            granted, state = 0, "waiting"
            blocker = "running_heatpump_commitment" if heatpump_running else "insufficient_headroom"
        grants.append({
            "reservation_id": str(request.get("reservation_id") or ""),
            "requested_w": requested,
            "granted_w": granted,
            "grant_state": state,
            "blocker": blocker,
        })
    return {
        "grants": grants,
        "flexible_budget_after_commitments_w": flexible,
        "heatpump_running": bool(heatpump_running),
        "heatpump_running_commitment_w": max(0, _int(heatpump_running_commitment_w, 0)),
    }


__all__ = [
    "arbitrate_grants", "build_request", "planned_reservation_power_w",
    "status_phase_count", "status_power_w",
]
