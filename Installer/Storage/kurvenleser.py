"""Gemeinsame Kurveninterpolation mit expliziter Zeit, ohne I/O."""
from typing import Any, Dict, Optional, Tuple
from .leistungsleser import safe_float, safe_int

def current_curve(
    plan: Dict[str, Any],
    now_s: Optional[float] = None,
    lookahead_h: float = 1.0,
    allow_before_start: bool = False,
    timeline_key: str = "target_timeline",
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return current curve SOC, lookahead target SOC and target timestamp."""
    timeline = plan.get(timeline_key) or []
    if not isinstance(timeline, list) or not timeline:
        return None, None, None
    now_ms = (float(now_s)) * 1000.0
    target_ms = now_ms + max(0.05, float(lookahead_h)) * 3600.0 * 1000.0
    points = []
    for item in timeline:
        try:
            ts = float(item.get("ts", 0) or 0)
            soc = float(item.get("soc", item.get("target_soc", 0)) or 0)
            if ts > 0:
                points.append((ts, max(0.0, min(100.0, soc))))
        except Exception:
            continue
    if not points:
        return None, None, None
    points.sort(key=lambda entry: entry[0])

    if now_ms < points[0][0] and not allow_before_start:
        return None, None, None

    def curve_soc_at(anchor_ms: float) -> Tuple[float, float]:
        if anchor_ms <= points[0][0]:
            return points[0][1], points[0][0]
        if anchor_ms >= points[-1][0]:
            return points[-1][1], points[-1][0]
        for idx in range(1, len(points)):
            prev_ts, prev_soc = points[idx - 1]
            next_ts, next_soc = points[idx]
            if anchor_ms <= next_ts:
                span = max(1.0, next_ts - prev_ts)
                ratio = max(0.0, min(1.0, (anchor_ms - prev_ts) / span))
                soc = prev_soc + (next_soc - prev_soc) * ratio
                return max(0.0, min(100.0, soc)), anchor_ms
        return points[-1][1], points[-1][0]

    now_soc, _ = curve_soc_at(now_ms)
    target_soc, target_ts = curve_soc_at(target_ms)
    return now_soc, target_soc, target_ts / 1000.0


def _plan_ts_s(value: Any) -> float:
    raw = safe_float(value, 0.0)
    if raw <= 0.0:
        return 0.0
    return raw / 1000.0 if raw > 10000000000.0 else raw


def adaptive_curve_context(
    plan: Dict[str, Any],
    raw_soc: float,
    now_s: float,
    lookahead_h: float,
    allow_before_start: bool,
) -> Dict[str, Any]:
    """Return the active floor/ceiling curve view for the storage decision."""
    floor_soc, target_soc, target_ts = current_curve(
        plan,
        now_s,
        lookahead_h=lookahead_h,
        allow_before_start=allow_before_start,
        timeline_key="soc_min_curve",
    )
    if floor_soc is None:
        floor_soc, target_soc, target_ts = current_curve(
            plan,
            now_s,
            lookahead_h=lookahead_h,
            allow_before_start=allow_before_start,
        )
    ceiling_soc, ceiling_target_soc, ceiling_target_ts = current_curve(
        plan,
        now_s,
        lookahead_h=lookahead_h,
        allow_before_start=allow_before_start,
        timeline_key="soc_ceiling_curve",
    )
    adaptive_active = bool(floor_soc is not None and ceiling_soc is not None)
    if adaptive_active and ceiling_soc is not None and floor_soc is not None and ceiling_soc < floor_soc:
        ceiling_soc = floor_soc
    if adaptive_active and ceiling_target_soc is not None and target_soc is not None and ceiling_target_soc < target_soc:
        ceiling_target_soc = target_soc

    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    latest_charge_start_s = _plan_ts_s(
        plan.get("latest_charge_start_ts", meta.get("latest_charge_start_ts", 0.0))
    )
    evening_shortfall_wh = max(
        0.0,
        safe_float(plan.get("evening_shortfall_wh", meta.get("evening_shortfall_wh")), 0.0),
    )
    headroom_required_wh = max(
        0.0,
        safe_float(
            plan.get("adaptive_headroom_required_wh", meta.get("adaptive_headroom_required_wh")),
            0.0,
        ),
    )
    latest_charge_due = bool(
        evening_shortfall_wh > 0.0
        or (latest_charge_start_s > 0.0 and float(now_s) >= latest_charge_start_s)
    )

    control_soc = floor_soc
    relation = "no_curve"
    if floor_soc is not None:
        relation = "below_floor"
        control_soc = floor_soc
        if adaptive_active and ceiling_soc is not None:
            if raw_soc > ceiling_soc:
                control_soc = ceiling_soc
                relation = "above_ceiling"
            elif raw_soc >= floor_soc:
                control_soc = raw_soc
                relation = "inside_band"
            else:
                control_soc = floor_soc
                relation = "below_floor"

    return {
        "active": adaptive_active,
        "relation": relation,
        "floor_soc": floor_soc,
        "ceiling_soc": ceiling_soc,
        "control_soc": control_soc,
        "target_soc": target_soc,
        "target_ts": target_ts,
        "ceiling_target_soc": ceiling_target_soc,
        "ceiling_target_ts": ceiling_target_ts,
        "latest_charge_start_ts": latest_charge_start_s,
        "latest_charge_due": latest_charge_due,
        "evening_shortfall_wh": evening_shortfall_wh,
        "headroom_required_wh": headroom_required_wh,
        "headroom_available_wh": max(
            0.0,
            safe_float(
                plan.get("adaptive_headroom_available_wh", meta.get("adaptive_headroom_available_wh")),
                0.0,
            ),
        ),
        "curtailment_pressure_wh": max(
            0.0,
            safe_float(plan.get("curtailment_pressure_wh", meta.get("curtailment_pressure_wh")), 0.0),
        ),
        "curtailment_unavoidable_wh": max(
            0.0,
            safe_float(plan.get("curtailment_unavoidable_wh", meta.get("curtailment_unavoidable_wh")), 0.0),
        ),
        "headroom_reserve_active": bool(
            plan.get("headroom_reserve_active", meta.get("headroom_reserve_active", False))
        ),
        "headroom_reserve_pressure_wh": max(
            0.0,
            safe_float(
                plan.get("headroom_reserve_pressure_wh", meta.get("headroom_reserve_pressure_wh")),
                0.0,
            ),
        ),
        "headroom_reserve_source": str(
            plan.get("headroom_reserve_source", meta.get("headroom_reserve_source", "")) or ""
        ),
    }

