#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ladekurven- und Prognosehelfer für den Storage Manager."""

from __future__ import annotations

import datetime
import time
from typing import Any, Dict, Optional, Tuple

try:
    from Installer.Storage.common import safe_float, safe_int
except ModuleNotFoundError:
    from Storage.common import safe_float, safe_int  # type: ignore


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
    now_ms = (time.time() if now_s is None else float(now_s)) * 1000.0
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


def latest_charge_start_clamp_context(
    cfg: Dict[str, Any],
    previous_state: Optional[Dict[str, Any]],
    current_latest_s: float,
    now_s: float,
) -> Dict[str, Any]:
    previous_state = previous_state or {}
    current_latest_s = _plan_ts_s(current_latest_s)
    previous_latest_s = _plan_ts_s(previous_state.get("latest_charge_start_ts", 0.0))
    if current_latest_s <= 0.0 or previous_latest_s <= 0.0:
        return {"active": False, "latest_charge_start_ts": current_latest_s}
    if current_latest_s <= previous_latest_s:
        return {"active": False, "latest_charge_start_ts": current_latest_s}

    try:
        current_day = datetime.datetime.fromtimestamp(current_latest_s).date()
        previous_day = datetime.datetime.fromtimestamp(previous_latest_s).date()
        now_day = datetime.datetime.fromtimestamp(now_s).date()
    except Exception:
        current_day = previous_day = now_day = None
    if current_day is not None and (current_day != previous_day or current_day != now_day):
        return {"active": False, "latest_charge_start_ts": current_latest_s}

    freeze_s = max(
        300.0,
        safe_float(
            cfg.get("storage_curve_latest_charge_freeze_s"),
            safe_float(cfg.get("storage_curve_sliding_horizon_min_open_s"), 3600.0),
        ),
    )
    replan_margin_s = max(0.0, safe_float(cfg.get("storage_curve_latest_charge_replan_margin_s"), 60.0))
    previous_reason = str(previous_state.get("sliding_horizon_reason") or "")
    previous_due = bool(previous_state.get("adaptive_latest_charge_due"))
    previous_risk = bool(
        previous_due
        or safe_float(previous_state.get("evening_shortfall_wh"), 0.0) > 0.0
        or previous_reason in ("latest_charge_start_near", "evening_shortfall", "target_not_reachable")
    )
    previous_near = bool(now_s + freeze_s >= previous_latest_s)
    moved_later = current_latest_s > previous_latest_s + replan_margin_s
    if not (moved_later and (previous_near or previous_risk)):
        return {"active": False, "latest_charge_start_ts": current_latest_s}

    return {
        "active": True,
        "latest_charge_start_ts": previous_latest_s,
        "raw_latest_charge_start_ts": current_latest_s,
        "previous_latest_charge_start_ts": previous_latest_s,
        "freeze_s": freeze_s,
        "replan_margin_s": replan_margin_s,
        "previous_reason": previous_reason,
        "previous_due": previous_due,
    }


def sliding_forecast_horizon_context(
    cfg: Dict[str, Any],
    plan: Dict[str, Any],
    now_s: float,
    curve_control_soc: float,
    effective_target_soc: float,
    release_ts_s: float,
    latest_charge_start_s: float,
    evening_shortfall_wh: float,
    forecast_only_target_active: bool,
    can_reach_target: bool,
    headroom_required_wh: float,
    headroom_available_wh: float,
    curtailment_pressure_wh: float,
    curtailment_unavoidable_wh: float,
    headroom_reserve_active: bool,
    hard_anchor: Dict[str, Any],
    previous_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Relax Forecast-100 curve following when future PV can still cover the target."""

    previous_state = previous_state or {}
    enabled = safe_int(cfg.get("storage_curve_sliding_horizon_enable"), 0) != 0
    min_confidence = max(0.0, min(1.0, safe_float(cfg.get("storage_curve_sliding_horizon_min_confidence"), 0.65)))
    exit_margin = max(0.0, safe_float(cfg.get("storage_curve_sliding_horizon_exit_margin"), 0.08))
    exit_confidence = max(0.0, min_confidence - exit_margin)
    min_hold_s = max(0.0, safe_float(cfg.get("storage_curve_sliding_horizon_min_hold_s"), 600.0))
    min_open_s = max(300.0, safe_float(cfg.get("storage_curve_sliding_horizon_min_open_s"), 3600.0))
    max_shortfall_wh = max(0.0, safe_float(cfg.get("storage_curve_sliding_horizon_shortfall_wh"), 200.0))
    max_pressure_wh = max(0.0, safe_float(cfg.get("storage_curve_sliding_horizon_pressure_wh"), 200.0))
    min_soc = max(0.0, min(99.0, safe_float(cfg.get("storage_curve_sliding_horizon_min_soc"), 80.0)))
    max_gap_pct = max(0.1, safe_float(cfg.get("storage_curve_sliding_horizon_max_gap_pct"), 12.0))
    headroom_available_wh = max(0.0, safe_float(headroom_available_wh, 0.0))
    uncovered_curtailment_pressure_wh = max(0.0, safe_float(curtailment_pressure_wh, 0.0) - headroom_available_wh)
    uncovered_pressure_wh = max(0.0, safe_float(headroom_required_wh, 0.0), uncovered_curtailment_pressure_wh)

    target_gap_pct = max(0.0, safe_float(effective_target_soc, 0.0) - safe_float(curve_control_soc, 0.0))
    minutes_until_latest = None
    if latest_charge_start_s > 0.0:
        minutes_until_latest = round(max(0.0, latest_charge_start_s - now_s) / 60.0, 1)

    try:
        month = datetime.datetime.fromtimestamp(now_s).month
    except Exception:
        month = 6
    if month in (4, 5, 6, 7, 8):
        season = "Sommer"
        season_factor = safe_float(cfg.get("storage_curve_sliding_horizon_summer_factor"), 1.0)
    elif month in (3, 9):
        season = "Übergang"
        season_factor = safe_float(cfg.get("storage_curve_sliding_horizon_transition_factor"), 0.82)
    else:
        season = "Winter"
        season_factor = safe_float(cfg.get("storage_curve_sliding_horizon_winter_factor"), 0.58)
    season_factor = max(0.0, min(1.0, season_factor))

    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    forecast_confidence_raw = max(
        safe_float(plan.get("forecast_confidence"), -1.0),
        safe_float(meta.get("forecast_confidence"), -1.0),
        safe_float(plan.get("forecast_confidence_pct"), -1.0) / 100.0,
        safe_float(meta.get("forecast_confidence_pct"), -1.0) / 100.0,
    )
    forecast_confidence = 1.0 if forecast_confidence_raw < 0.0 else max(0.0, min(1.0, forecast_confidence_raw))
    if effective_target_soc > min_soc:
        soc_factor = max(0.0, min(1.0, (curve_control_soc - min_soc) / (effective_target_soc - min_soc)))
    else:
        soc_factor = 1.0 if curve_control_soc >= effective_target_soc else 0.0
    if release_ts_s > now_s and latest_charge_start_s > now_s:
        horizon_factor = max(0.0, min(1.0, (latest_charge_start_s - now_s) / (release_ts_s - now_s)))
    else:
        horizon_factor = 0.0
    confidence = max(
        0.0,
        min(1.0, (0.45 * soc_factor + 0.35 * horizon_factor + 0.20 * forecast_confidence) * season_factor),
    )
    previous_sliding_active = bool(previous_state.get("sliding_horizon_active")) and str(
        previous_state.get("state") or previous_state.get("parallel_state") or ""
    ) == "parallel_curve_auto_hold"
    previous_ts = safe_float(previous_state.get("ts"), 0.0)
    previous_age_s = max(0.0, now_s - previous_ts) if previous_ts > 0.0 else 999999.0

    def blocked(reason: str, **extra: Any) -> Dict[str, Any]:
        payload = {
            "active": False,
            "enabled": enabled,
            "reason": reason,
            "confidence": round(confidence, 4),
            "min_confidence": round(min_confidence, 4),
            "exit_confidence": round(exit_confidence, 4),
            "season": season,
            "season_factor": round(season_factor, 4),
            "soc_factor": round(soc_factor, 4),
            "horizon_factor": round(horizon_factor, 4),
            "forecast_confidence": round(forecast_confidence, 4),
            "target_gap_pct": round(target_gap_pct, 3),
            "latest_charge_start_ts": latest_charge_start_s,
            "minutes_until_latest_charge": minutes_until_latest,
            "headroom_available_wh": round(headroom_available_wh, 1),
            "uncovered_pressure_wh": round(uncovered_pressure_wh, 1),
            "uncovered_curtailment_pressure_wh": round(uncovered_curtailment_pressure_wh, 1),
            "previous_active": previous_sliding_active,
            "previous_age_s": round(previous_age_s, 1) if previous_ts > 0.0 else None,
        }
        payload.update(extra)
        return payload

    if not enabled:
        return blocked("disabled")
    if not forecast_only_target_active:
        return blocked("not_forecast_100")
    if not can_reach_target:
        return blocked("target_not_reachable")
    if effective_target_soc <= 0.0:
        return blocked("missing_target")
    if target_gap_pct <= max(0.05, safe_float(cfg.get("storage_curve_sliding_horizon_target_reached_margin_pct"), 0.2)):
        return blocked("target_already_reached")
    if target_gap_pct > max_gap_pct:
        return blocked("target_gap_too_large")
    if curve_control_soc < min_soc:
        return blocked("soc_below_sliding_floor")
    if evening_shortfall_wh > max_shortfall_wh:
        return blocked("evening_shortfall", evening_shortfall_wh=round(evening_shortfall_wh, 1))
    if release_ts_s <= 0.0 or now_s >= release_ts_s:
        return blocked("release_due_or_missing")
    if latest_charge_start_s <= 0.0:
        return blocked("latest_charge_start_missing")
    if now_s + min_open_s >= latest_charge_start_s:
        return blocked("latest_charge_start_near")
    if uncovered_pressure_wh >= max_pressure_wh:
        return blocked(
            "headroom_required" if headroom_required_wh >= max_pressure_wh else "curtailment_pressure_uncovered",
            headroom_required_wh=round(headroom_required_wh, 1),
            headroom_available_wh=round(headroom_available_wh, 1),
            curtailment_pressure_wh=round(curtailment_pressure_wh, 1),
            uncovered_pressure_wh=round(uncovered_pressure_wh, 1),
        )
    if curtailment_unavoidable_wh >= max_pressure_wh:
        return blocked("curtailment_unavoidable", curtailment_unavoidable_wh=round(curtailment_unavoidable_wh, 1))
    if headroom_reserve_active:
        return blocked("headroom_reserve_active")
    hard_anchor_ts = safe_float(hard_anchor.get("ts_s"), 0.0) if isinstance(hard_anchor, dict) else 0.0
    if (
        isinstance(hard_anchor, dict)
        and bool(hard_anchor.get("active"))
        and bool(hard_anchor.get("locked", True))
        and hard_anchor_ts > now_s
    ):
        return blocked("hard_anchor_pending", hard_anchor_ts=hard_anchor_ts, hard_anchor_soc=hard_anchor.get("soc"))
    if confidence < min_confidence:
        if previous_sliding_active and previous_age_s < min_hold_s and confidence >= exit_confidence:
            return {
                "active": True,
                "enabled": enabled,
                "reason": "hysteresis_hold",
                "confidence": round(confidence, 4),
                "min_confidence": round(min_confidence, 4),
                "exit_confidence": round(exit_confidence, 4),
                "season": season,
                "season_factor": round(season_factor, 4),
                "soc_factor": round(soc_factor, 4),
                "horizon_factor": round(horizon_factor, 4),
                "forecast_confidence": round(forecast_confidence, 4),
                "target_gap_pct": round(target_gap_pct, 3),
                "latest_charge_start_ts": latest_charge_start_s,
                "minutes_until_latest_charge": minutes_until_latest,
                "headroom_available_wh": round(headroom_available_wh, 1),
                "uncovered_pressure_wh": round(uncovered_pressure_wh, 1),
                "uncovered_curtailment_pressure_wh": round(uncovered_curtailment_pressure_wh, 1),
                "min_open_s": round(min_open_s, 1),
                "min_hold_s": round(min_hold_s, 1),
                "previous_active": True,
                "previous_age_s": round(previous_age_s, 1),
                "min_soc": round(min_soc, 2),
                "max_gap_pct": round(max_gap_pct, 2),
            }
        return blocked("confidence_too_low")

    return {
        "active": True,
        "enabled": enabled,
        "reason": "future_pv_covers_target_before_latest_charge",
        "confidence": round(confidence, 4),
        "min_confidence": round(min_confidence, 4),
        "exit_confidence": round(exit_confidence, 4),
        "season": season,
        "season_factor": round(season_factor, 4),
        "soc_factor": round(soc_factor, 4),
        "horizon_factor": round(horizon_factor, 4),
        "forecast_confidence": round(forecast_confidence, 4),
        "target_gap_pct": round(target_gap_pct, 3),
        "latest_charge_start_ts": latest_charge_start_s,
        "minutes_until_latest_charge": minutes_until_latest,
        "headroom_available_wh": round(headroom_available_wh, 1),
        "uncovered_pressure_wh": round(uncovered_pressure_wh, 1),
        "uncovered_curtailment_pressure_wh": round(uncovered_curtailment_pressure_wh, 1),
        "min_open_s": round(min_open_s, 1),
        "min_hold_s": round(min_hold_s, 1),
        "previous_active": previous_sliding_active,
        "previous_age_s": round(previous_age_s, 1) if previous_ts > 0.0 else None,
        "min_soc": round(min_soc, 2),
        "max_gap_pct": round(max_gap_pct, 2),
    }


def hard_noon_anchor(plan: Dict[str, Any], now_s: Optional[float] = None) -> Dict[str, Any]:
    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    anchors = plan.get("curve_anchors") if isinstance(plan.get("curve_anchors"), list) else []
    hard_anchors = []
    for anchor in anchors:
        if not isinstance(anchor, dict) or str(anchor.get("kind") or "") not in {"intermediate", "noon"}:
            continue
        ts = safe_float(anchor.get("ts"), 0.0)
        soc = safe_float(anchor.get("soc"), -1.0)
        if ts > 0 and soc >= 0.0:
            ts_s = ts / 1000.0 if ts > 10000000000.0 else ts
            hard_anchors.append({
                "active": True,
                "locked": bool(meta.get("intermediate_anchors_locked", meta.get("noon_anchor_locked", meta.get("noon_anchor_active", True)))),
                "ts_s": ts_s,
                "soc": max(0.0, min(100.0, soc)),
                "t": str(anchor.get("t") or meta.get("noon_anchor_t") or ""),
                "kind": str(anchor.get("kind") or ""),
                "label": str(anchor.get("label") or ""),
            })
    if hard_anchors:
        hard_anchors.sort(key=lambda item: safe_float(item.get("ts_s"), 0.0))
        if now_s is not None:
            now_f = safe_float(now_s, 0.0)
            for anchor in hard_anchors:
                if safe_float(anchor.get("ts_s"), 0.0) >= now_f:
                    return anchor
            return hard_anchors[-1]
        return hard_anchors[0]

    meta_anchors = meta.get("intermediate_anchors") if isinstance(meta.get("intermediate_anchors"), list) else []
    hard_anchors = []
    for anchor in meta_anchors:
        if not isinstance(anchor, dict):
            continue
        ts = safe_float(anchor.get("ts"), 0.0)
        soc = safe_float(anchor.get("soc"), -1.0)
        if ts > 0 and soc >= 0.0:
            hard_anchors.append({
                "active": True,
                "locked": bool(meta.get("intermediate_anchors_locked", True)),
                "ts_s": ts / 1000.0 if ts > 10000000000.0 else ts,
                "soc": max(0.0, min(100.0, soc)),
                "t": str(anchor.get("t") or ""),
                "kind": str(anchor.get("kind") or ""),
                "label": str(anchor.get("label") or ""),
            })
    if hard_anchors:
        hard_anchors.sort(key=lambda item: safe_float(item.get("ts_s"), 0.0))
        if now_s is not None:
            now_f = safe_float(now_s, 0.0)
            for anchor in hard_anchors:
                if safe_float(anchor.get("ts_s"), 0.0) >= now_f:
                    return anchor
            return hard_anchors[-1]
        return hard_anchors[0]

    if not bool(meta.get("noon_anchor_active") or meta.get("noon_anchor_locked")):
        return {"active": False}
    soc = safe_float(meta.get("noon_anchor_soc"), -1.0)
    day_start = safe_float(meta.get("curve_day_start_ts"), 0.0)
    label = str(meta.get("noon_anchor_t") or "").strip()
    if soc < 0.0 or day_start <= 0.0 or ":" not in label:
        return {"active": False}
    try:
        hour_s, minute_s = label.split(":", 1)
        hour = max(0, min(23, int(hour_s)))
        minute = max(0, min(59, int(minute_s[:2])))
    except Exception:
        return {"active": False}
    day_start_s = day_start / 1000.0 if day_start > 10000000000.0 else day_start
    return {
        "active": True,
        "locked": bool(meta.get("noon_anchor_locked", True)),
        "ts_s": day_start_s + hour * 3600.0 + minute * 60.0,
        "soc": max(0.0, min(100.0, soc)),
        "t": label,
        "kind": "noon",
        "label": "Z2",
    }


def build_ladekurve_meta(plan: Dict[str, Any]) -> Dict[str, Any]:
    now_ms = time.time() * 1000.0
    target_tl = plan.get("target_timeline") or []
    sim_tl = plan.get("timeline") or []
    if not isinstance(target_tl, list):
        target_tl = []
    if not isinstance(sim_tl, list):
        sim_tl = []
    if not target_tl and not sim_tl:
        return {}

    day_ms = 86400000.0
    today0_dt = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today0_ms = today0_dt.timestamp() * 1000.0
    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}

    display_start_ms: Optional[float] = None
    display_label = "Heute"
    if meta.get("curve_day_start_ts"):
        try:
            raw_start_ms = float(meta.get("curve_day_start_ts") or 0.0)
            # curve_day_start_ts is already the simulator's display-day
            # boundary. Re-normalizing it to the host timezone can move the
            # window back by a day on UTC bare-metal systems.
            display_start_ms = raw_start_ms
            display_label = str(meta.get("curve_day_label") or display_label)
        except Exception:
            display_start_ms = None

    if display_start_ms is None:
        days = []
        all_points = sim_tl or target_tl
        for offset in (0, 1):
            start = today0_ms + offset * day_ms
            end = start + day_ms
            slots = []
            for point in all_points:
                try:
                    ts = float(point.get("ts", 0) or 0)
                except Exception:
                    continue
                if start <= ts < end:
                    slots.append(point)
            if not slots:
                continue
            future_slots = [s for s in slots if safe_float(s.get("ts"), 0.0) >= now_ms - 15 * 60000]
            max_pv = max((safe_float(s.get("pv_w"), 0.0) for s in slots), default=0.0)
            max_future_pv = max((safe_float(s.get("pv_w"), 0.0) for s in future_slots), default=0.0)
            last_pv_ts = max(
                (safe_float(s.get("ts"), 0.0) for s in slots if safe_float(s.get("pv_w"), 0.0) > 500.0),
                default=0.0,
            )
            days.append({
                "start": start,
                "offset": offset,
                "max_pv": max_pv,
                "max_future_pv": max_future_pv,
                "last_pv_ts": last_pv_ts,
            })
        display = next((d for d in days if d["offset"] == 0), None)
        today_done = bool(display) and display["max_future_pv"] < 500.0 and now_ms > (display["last_pv_ts"] + 30 * 60000)
        if today_done:
            display = next((d for d in days if d["offset"] == 1 and d["max_pv"] > 500.0), display)
        if not display and days:
            display = days[0]
        if display:
            display_start_ms = float(display["start"])
            display_label = "Morgen" if int(display["offset"]) == 1 else "Heute"

    if display_start_ms is None and target_tl:
        first_ts = safe_float(target_tl[0].get("ts"), 0.0)
        display_start_ms = datetime.datetime.fromtimestamp(first_ts / 1000.0).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp() * 1000.0
        display_label = "Morgen" if display_start_ms > today0_ms + 3600000.0 else "Heute"
    if display_start_ms is None:
        return {}

    display_end_ms = display_start_ms + day_ms
    display_date = datetime.datetime.fromtimestamp(display_start_ms / 1000.0).date()
    day_offset = round((display_start_ms - today0_ms) / day_ms)

    target_points = []
    for point in target_tl:
        ts = safe_float(point.get("ts"), 0.0)
        if display_start_ms <= ts < display_end_ms:
            target_points.append((ts, safe_float(point.get("soc"), 0.0)))
    target_points.sort(key=lambda item: item[0])

    sim_points = []
    for point in sim_tl:
        ts = safe_float(point.get("ts"), 0.0)
        if display_start_ms <= ts < display_end_ms:
            sim_points.append(point)
    sim_points.sort(key=lambda item: safe_float(item.get("ts"), 0.0))

    if not target_points and not sim_points:
        return {}

    def soc_near(ts: float, fallback: float) -> float:
        if not target_points:
            return fallback
        return min(target_points, key=lambda item: abs(item[0] - ts))[1]

    first_ts, first_soc = target_points[0] if target_points else (
        safe_float(sim_points[0].get("ts"), 0.0),
        safe_float(sim_points[0].get("soc"), 0.0),
    )
    last_ts, last_soc = target_points[-1] if target_points else (
        safe_float(sim_points[-1].get("ts"), 0.0),
        safe_float(sim_points[-1].get("soc"), 0.0),
    )
    peak = None
    if sim_points:
        peak_slot = max(sim_points, key=lambda item: safe_float(item.get("pv_w"), 0.0))
        peak_ts = safe_float(peak_slot.get("ts"), first_ts)
        peak_pv_w = safe_float(peak_slot.get("pv_w"), 0.0)
        if peak_pv_w > 500.0:
            peak = {
                "t": datetime.datetime.fromtimestamp(peak_ts / 1000.0).strftime("%H:%M"),
                "soc": round(soc_near(peak_ts, safe_float(peak_slot.get("soc"), first_soc)), 1),
                "pv_kw": round(peak_pv_w / 1000.0, 1),
                "past": peak_ts < now_ms,
                "source": "storage_plan",
            }
    if peak is None:
        nearest_ts, nearest_soc = min(target_points or [(first_ts, first_soc)], key=lambda item: abs(item[0] - now_ms))
        peak = {
            "t": datetime.datetime.fromtimestamp(nearest_ts / 1000.0).strftime("%H:%M"),
            "soc": round(nearest_soc, 1),
            "past": nearest_ts < now_ms,
            "source": "target_timeline",
        }

    freilauf_ts = safe_float(plan.get("ladeende_ts"), 0.0) or safe_float(meta.get("curve_end_ts"), 0.0) or last_ts
    if not (display_start_ms <= freilauf_ts < display_end_ms):
        freilauf_ts = last_ts
    freilauf_soc = safe_float(plan.get("ladeende_soc"), safe_float(plan.get("effective_target_soc"), safe_float(plan.get("target_soc"), last_soc)))
    return {
        "day_label": display_label,
        "day_offset": int(day_offset),
        "day_start_ts": int(display_start_ms),
        "date": display_date.isoformat(),
        "has_target_curve": bool(target_points),
        "ladestart": {
            "t": datetime.datetime.fromtimestamp(first_ts / 1000.0).strftime("%H:%M"),
            "soc": round(first_soc, 1),
            "past": first_ts < now_ms,
            "forecast": display_start_ms > today0_ms,
        },
        "peak": peak,
        "freilauf": {
            "t": datetime.datetime.fromtimestamp(freilauf_ts / 1000.0).strftime("%H:%M"),
            "soc": round(freilauf_soc, 1),
            "past": freilauf_ts < now_ms,
        },
    }
