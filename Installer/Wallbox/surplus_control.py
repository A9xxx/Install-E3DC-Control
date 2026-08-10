"""Reiner PV-Überschussregler unter Beachtung der Treiberauflösung."""

import math


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _quantize_down(value, step):
    return round(math.floor((max(0.0, value) + 1e-9) / step) * step, 3)


def surplus_control_step(
    *, grid_w, grant_w, current_amp, actual_amp=None, phases=1,
    current_step_amp=1.0, effective_w_per_amp=0.0, min_amp=6.0,
    max_amp=32.0, data_valid=True, coherent=True, command_confirmed=True,
    transition_active=False, cp_active=False, heatpump_starting=False,
    marginal_actor=True, now_ts=0.0, previous_state=None,
    target_grid_w=-125.0, noise_w=100.0, reaction_noise_w=0.0,
    max_up_step_a=1.0, cycle_s=2.0,
):
    """Liefert einen deterministischen Regelschritt und ein Zustandsdelta.

    Positive Netzwerte bedeuten Bezug, negative Werte Einspeisung. Eine
    Erhöhung ist nach zwei kohärenten Frames mit höchstens einem Ampere pro
    aktivem Zyklus erlaubt; die Bezugskorrektur ist asymmetrisch und darf den
    vollständig erforderlichen Abwärtsschritt verwenden.
    """

    state = dict(previous_state) if isinstance(previous_state, dict) else {}
    step = max(0.1, min(16.0, _float(current_step_amp, 1.0)))
    phase_count = max(1, min(3, int(round(_float(phases, 1.0)))))
    w_per_amp = _float(effective_w_per_amp, 0.0) or 230.0 * phase_count
    w_per_amp = max(23.0 * phase_count, w_per_amp)
    minimum = max(step, _float(min_amp, 6.0))
    maximum = max(minimum, _float(max_amp, 32.0))
    current = max(0.0, _float(current_amp, 0.0), _float(actual_amp, 0.0))
    grant = max(0.0, _float(grant_w, 0.0))
    grant_target = _quantize_down(grant / w_per_amp, step) if grant >= minimum * w_per_amp else 0.0
    grant_target = min(maximum, grant_target)
    quantum_w = step * w_per_amp
    corridor_w = max(2.0 * quantum_w, _float(noise_w, 0.0), _float(reaction_noise_w, 0.0))
    target_grid = min(0.0, _float(target_grid_w, -125.0))
    lower = target_grid - corridor_w * 0.5
    upper = target_grid + corridor_w * 0.5
    grid = _float(grid_w, 0.0)
    coherent_frames = int(state.get("coherent_frames", 0) or 0)
    coherent_frames = coherent_frames + 1 if data_valid and coherent else 0
    blocked_up = bool(
        not data_valid or coherent_frames < 2 or not command_confirmed
        or transition_active or cp_active or heatpump_starting or not marginal_actor
    )

    if current < minimum and grant_target >= minimum:
        blocked_up = bool(not data_valid or not marginal_actor or transition_active or cp_active or heatpump_starting)

    raw_target = min(grant_target, current)
    send_amp = current
    direction = "hold"
    reason = "target_corridor"
    next_ts = _float(now_ts, 0.0) + max(0.1, _float(cycle_s, 2.0))

    if grant_target + step * 0.5 < current:
        raw_target = grant_target
        send_amp = grant_target
        direction = "down"
        reason = "grant_fast_down"
    elif grid > max(250.0, upper):
        correction_a = max(step, (grid - target_grid + quantum_w) / w_per_amp)
        raw_target = max(0.0, current - correction_a)
        if 0.0 < raw_target < minimum:
            raw_target = minimum if grant_target >= minimum else 0.0
        send_amp = min(grant_target, _quantize_down(raw_target, step))
        direction = "down"
        reason = "grid_import_fast_down"
        next_ts = _float(now_ts, 0.0)
    elif grid < lower and grant_target > current + step * 0.5:
        raw_target = min(grant_target, current + max(0.0, (target_grid - grid) / w_per_amp))
        if current < minimum and grant_target >= minimum:
            raw_target = grant_target
        if blocked_up:
            send_amp = current
            direction = "hold"
            reason = "non_marginal_actor_hold" if not marginal_actor else "up_frozen"
        else:
            if current < minimum and grant_target >= minimum:
                up_limit = max(minimum, grant_target)
            else:
                up_limit = max(step, _float(max_up_step_a, 1.0))
            send_amp = _quantize_down(min(raw_target, current + up_limit), step)
            direction = "up" if send_amp > current + step * 0.5 else "hold"
            reason = "surplus_coarse_up" if raw_target - current > 2.0 * step else "surplus_fine_trim"
    elif not data_valid:
        reason = "invalid_data_hold"
    elif not marginal_actor:
        reason = "non_marginal_actor_hold"

    changed = abs(send_amp - current) >= step * 0.5
    patch = {
        "coherent_frames": coherent_frames,
        "last_direction": direction,
        "last_reason": reason,
        "last_target_amp": round(send_amp, 3),
        "last_ts": _float(now_ts, 0.0),
    }
    return {
        "schema_version": "wallbox_surplus_control_v1",
        "raw_target_amp": round(raw_target, 3),
        "quantized_target_amp": round(grant_target, 3),
        "send_amp": round(send_amp, 3),
        "current_amp": round(current, 3),
        "current_step_amp": round(step, 3),
        "effective_w_per_amp": round(w_per_amp, 3),
        "physical_quantum_w": round(quantum_w, 1),
        "corridor_w": round(corridor_w, 1),
        "target_grid_w": round(target_grid, 1),
        "direction": direction,
        "reason_code": reason,
        "changed": changed,
        "next_eligible_ts": next_ts,
        "state_patch": patch,
    }


__all__ = ["surplus_control_step"]
