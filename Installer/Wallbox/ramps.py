"""Reine Stromrampen-Verträge für den Wallbox-Regelkreis."""

import math
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
    current_step_amp: Any = 1.0,
    hard_stop: bool = False,
    bypass: bool = False,
) -> Dict[str, Any]:
    """Limit current-cap changes during an already verified charge."""

    step = max(0.1, min(16.0, _safe_float(current_step_amp, 1.0)))
    minimum = max(step, _safe_float(min_amp, 6.0))
    maximum = max(minimum, _safe_float(max_amp, 32.0))

    def quantize_down(value):
        numeric = max(0.0, _safe_float(value, 0.0))
        if not math.isfinite(numeric):
            return 0.0
        return round(math.floor((numeric + 1e-9) / step) * step, 3)

    def quantize_nearest(value):
        numeric = max(0.0, _safe_float(value, 0.0))
        if not math.isfinite(numeric):
            return 0.0
        return round(round(numeric / step) * step, 3)

    raw_target = min(maximum, quantize_down(target_amp))
    current = min(maximum, max(0.0, quantize_nearest(current_amp), quantize_nearest(current_set_amp)))
    power = max(0.0, _safe_float(hw_power_w, 0.0))
    now = max(0.0, _safe_float(now_ts, 0.0))
    last = max(0.0, _safe_float(last_ramp_ts, 0.0))
    interval = max(0.0, _safe_float(ramp_interval_s, 7.0))
    up_step = max(step, quantize_nearest(up_step_a))
    down_step = max(step, quantize_nearest(down_step_a))
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
    elif abs(raw_target - current) < step * 0.5:
        reason = "stable"
        applied = current
    elif elapsed_s < interval:
        reason = "ramp_interval_hold"
        direction = "up" if raw_target > current else "down"
        applied = current
        limited = True
    elif raw_target > current:
        direction = "up"
        # Nach bestätigter realer Ladung ist ``raw_target`` bereits durch
        # Budget, Fahrzeug-, Treiber- und Infrastrukturgrenzen autorisiert.
        # Eine zusätzliche globale +1-A-Rampe würde nur PV exportieren und
        # konkurriert mit der physischen OBC-Rampe des Fahrzeugs.
        applied = raw_target
        limited = False
        changed = applied != current
        next_ramp_ts = now if changed else last
        reason = "target_reached_direct_up"
    else:
        direction = "down"
        floor = 0 if hard_stop else minimum
        applied = quantize_down(max(raw_target, current - down_step, floor))
        limited = applied != raw_target
        changed = applied != current
        next_ramp_ts = now if changed else last
        reason = "amp_down_step" if limited else "target_reached"

    return {
        "schema_version": "wallbox_running_ramp_v1",
        "raw_target_amp": float(raw_target),
        "applied_amp": float(applied),
        "current_amp": float(current),
        "min_amp": float(minimum),
        "max_amp": float(maximum),
        "current_step_amp": float(step),
        "running": bool(running),
        "limited": bool(limited),
        "changed": bool(changed),
        "direction": direction,
        "reason": reason,
        "elapsed_s": float(elapsed_s),
        "ramp_interval_s": float(interval),
        "up_step_a": float(up_step),
        "down_step_a": float(down_step),
        "next_ramp_ts": float(next_ramp_ts),
    }
