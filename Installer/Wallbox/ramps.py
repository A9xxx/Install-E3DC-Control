"""Reine Stromrampen-Verträge für den Wallbox-Regelkreis."""

from typing import Any, Dict


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).strip() if value is not None else ""
        return float(text) if text else float(default)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_safe_float(value, default)))
    except Exception:
        return int(default)


def running_charge_ramp_contract(
    *,
    target_amp: Any,
    current_amp: Any = 0,
    current_set_amp: Any = 0,
    charger_connected: bool = False,
    hw_charging: bool = False,
    hw_power_w: Any = 0.0,
    now_ts: Any = 0.0,
    last_ramp_ts: Any = 0.0,
    min_amp: Any = 6,
    max_amp: Any = 32,
    ramp_interval_s: Any = 7.0,
    up_step_a: Any = 1,
    down_step_a: Any = 1,
    hard_stop: bool = False,
    bypass: bool = False,
) -> Dict[str, Any]:
    """Limit current-cap changes during an already verified charge."""

    minimum = max(1, _safe_int(min_amp, 6))
    maximum = max(minimum, _safe_int(max_amp, 32))
    raw_target = max(0, min(maximum, _safe_int(target_amp, 0)))
    current = max(0, min(maximum, max(_safe_int(current_amp, 0), _safe_int(current_set_amp, 0))))
    power = max(0.0, _safe_float(hw_power_w, 0.0))
    now = max(0.0, _safe_float(now_ts, 0.0))
    last = max(0.0, _safe_float(last_ramp_ts, 0.0))
    interval = max(0.0, _safe_float(ramp_interval_s, 7.0))
    up_step = max(1, _safe_int(up_step_a, 1))
    down_step = max(1, _safe_int(down_step_a, 1))
    running = bool(charger_connected and (hw_charging or power > 500.0) and current >= minimum)
    elapsed_s = max(0.0, now - last) if now > 0.0 and last > 0.0 else 999999.0

    applied = raw_target
    reason = "not_running"
    direction = "flat"
    limited = False
    changed = False
    next_ramp_ts = last

    if bypass:
        reason = "bypass"
    elif raw_target <= 0:
        reason = "hard_stop" if hard_stop else "target_zero"
    elif not running:
        reason = "not_running"
    elif raw_target == current:
        reason = "stable"
        applied = current
    elif elapsed_s < interval:
        reason = "ramp_interval_hold"
        direction = "up" if raw_target > current else "down"
        applied = current
        limited = True
    elif raw_target > current:
        direction = "up"
        applied = min(raw_target, current + up_step)
        limited = applied != raw_target
        changed = applied != current
        next_ramp_ts = now if changed else last
        reason = "amp_up_step" if limited else "target_reached"
    else:
        direction = "down"
        floor = 0 if hard_stop else minimum
        applied = max(raw_target, current - down_step, floor)
        limited = applied != raw_target
        changed = applied != current
        next_ramp_ts = now if changed else last
        reason = "amp_down_step" if limited else "target_reached"

    return {
        "schema_version": "wallbox_running_ramp_v1",
        "raw_target_amp": int(raw_target),
        "applied_amp": int(applied),
        "current_amp": int(current),
        "min_amp": int(minimum),
        "max_amp": int(maximum),
        "running": bool(running),
        "limited": bool(limited),
        "changed": bool(changed),
        "direction": direction,
        "reason": reason,
        "elapsed_s": float(elapsed_s),
        "ramp_interval_s": float(interval),
        "up_step_a": int(up_step),
        "down_step_a": int(down_step),
        "next_ramp_ts": float(next_ramp_ts),
    }
