"""Heat demand forecasting helpers for the central heat policy.

The policy needs a bounded 24h heat deficit, not a vague "cheap now" signal.
This module reads the already generated ML heat-pump forecast first, falls
back to empirical daily heat-pump consumption, then to explicit user input and
finally to a conservative temperature-based estimate.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_ML_PREDICTION_PATH = "/var/www/html/ramdisk/ml_prediction.json"
DEFAULT_DB_PATH = "/var/www/html/data/e3dc_stats.db"
DEFAULT_HORIZON_H = 24.0
DEFAULT_MAX_PREDICTION_AGE_S = 36 * 3600


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        result = float(str(value).replace(",", "."))
        return result if math.isfinite(result) else float(default)
    except Exception:
        return float(default)


def _safe_ts(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:
            ts /= 1000.0
        return ts if math.isfinite(ts) and ts > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = float(text)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return numeric if numeric > 0 else None
    except ValueError:
        pass
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        if not path or not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


@dataclass(frozen=True)
class HeatForecastResult:
    need_kwh: float
    source: str
    quality: str
    reason: str
    horizon_h: float = DEFAULT_HORIZON_H
    coverage_h: float = 0.0
    forecast_temp_c: Optional[float] = None
    stale: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.quality in ("empirical", "ml_prediction", "user_fallback", "scientific_fallback") and self.need_kwh >= 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "need_kwh": round(max(0.0, self.need_kwh), 4),
            "source": self.source,
            "quality": self.quality,
            "reason": self.reason,
            "horizon_h": round(max(0.0, self.horizon_h), 3),
            "coverage_h": round(max(0.0, self.coverage_h), 3),
            "forecast_temp_c": self.forecast_temp_c,
            "stale": bool(self.stale),
            "valid": bool(self.valid),
            "details": dict(self.details),
        }


def _timeline_window_energy_kwh(
    timeline: Iterable[Dict[str, Any]],
    *,
    now_ts: float,
    horizon_h: float,
) -> Optional[HeatForecastResult]:
    start_s = float(now_ts)
    end_s = start_s + max(0.25, float(horizon_h)) * 3600.0
    energy_kwh = 0.0
    coverage_s = 0.0
    temp_values: List[float] = []
    slot_count = 0

    for slot in timeline or []:
        if not isinstance(slot, dict):
            continue
        slot_start = _safe_ts(slot.get("start_timestamp"))
        if slot_start is None:
            continue
        slot_end = _safe_ts(slot.get("end_timestamp"))
        if slot_end is None or slot_end <= slot_start:
            slot_end = slot_start + 15 * 60
        overlap_s = min(slot_end, end_s) - max(slot_start, start_s)
        if overlap_s <= 0:
            continue
        # Existing ml_prediction.timeline keeps wp_kwh as average kW per slot.
        avg_kw = max(0.0, _safe_float(slot.get("wp_kwh"), 0.0))
        energy_kwh += avg_kw * overlap_s / 3600.0
        coverage_s += overlap_s
        slot_count += 1
        if slot.get("forecast_temp_c") is not None:
            temp_values.append(_safe_float(slot.get("forecast_temp_c"), 0.0))

    if coverage_s <= 0:
        return None
    temp = round(sum(temp_values) / len(temp_values), 2) if temp_values else None
    return HeatForecastResult(
        need_kwh=max(0.0, energy_kwh),
        source="ml_prediction_timeline",
        quality="ml_prediction",
        reason="fresh_ml_prediction_timeline",
        horizon_h=horizon_h,
        coverage_h=coverage_s / 3600.0,
        forecast_temp_c=temp,
        details={"slots": slot_count},
    )


def _prediction_result(
    *,
    prediction_path: str,
    now_ts: float,
    horizon_h: float,
    max_age_s: float,
) -> Optional[HeatForecastResult]:
    payload = _read_json(prediction_path)
    if not payload:
        return None

    ts = _safe_ts(payload.get("ts"))
    if ts is None:
        try:
            ts = os.path.getmtime(prediction_path)
        except Exception:
            ts = None
    if ts is None:
        return None
    age_s = max(0.0, float(now_ts) - float(ts))
    if age_s > max(0.0, float(max_age_s)):
        return HeatForecastResult(
            need_kwh=0.0,
            source="ml_prediction_stale",
            quality="stale",
            reason="ml_prediction_too_old",
            horizon_h=horizon_h,
            stale=True,
            details={"age_s": int(age_s), "max_age_s": int(max_age_s)},
        )

    timeline_result = _timeline_window_energy_kwh(
        payload.get("timeline") if isinstance(payload.get("timeline"), list) else [],
        now_ts=now_ts,
        horizon_h=horizon_h,
    )
    if timeline_result is not None:
        return timeline_result

    if payload.get("wp_kwh") is not None:
        return HeatForecastResult(
            need_kwh=max(0.0, _safe_float(payload.get("wp_kwh"), 0.0)),
            source="ml_prediction_daily",
            quality="ml_prediction",
            reason="fresh_ml_prediction_daily",
            horizon_h=horizon_h,
            coverage_h=min(float(horizon_h), 24.0),
            forecast_temp_c=None,
            details={"age_s": int(age_s)},
        )
    return None


def _percentile(values: List[float], quantile: float) -> Optional[float]:
    clean = sorted(v for v in values if math.isfinite(v) and v >= 0.0)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    pos = (len(clean) - 1) * max(0.0, min(1.0, quantile))
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return clean[low]
    return clean[low] + (clean[high] - clean[low]) * (pos - low)


def _daily_stats_result(db_path: str, *, horizon_h: float, days: int = 21) -> Optional[HeatForecastResult]:
    try:
        if not db_path or not os.path.exists(db_path):
            return None
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                """
                SELECT wp_consumption
                FROM daily_stats
                WHERE date < date('now') AND wp_consumption IS NOT NULL
                ORDER BY date DESC
                LIMIT ?
                """,
                (int(days),),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None

    values = []
    for (raw,) in rows:
        val = _safe_float(raw, 0.0)
        if 0.05 <= val <= 80.0:
            values.append(val)
    if len(values) < 3:
        return None
    baseline = _percentile(values, 0.75)
    if baseline is None:
        return None
    scale = max(0.0, float(horizon_h)) / 24.0
    return HeatForecastResult(
        need_kwh=baseline * scale,
        source="daily_stats_wp_consumption",
        quality="empirical",
        reason="daily_stats_75th_percentile",
        horizon_h=horizon_h,
        coverage_h=min(float(horizon_h), 24.0),
        details={"samples": len(values), "percentile": 0.75},
    )


def _scientific_fallback_kwh(forecast_temp_c: Optional[float], horizon_h: float) -> float:
    """Conservative fallback if no site data exists.

    This is intentionally coarse. It is only used after ML, measured history and
    explicit user input are unavailable, so it must avoid aggressive boosts.
    """

    if forecast_temp_c is None:
        daily_kwh = 5.0
    else:
        temp = _safe_float(forecast_temp_c, 8.0)
        if temp <= -10.0:
            daily_kwh = 18.0
        elif temp <= 0.0:
            daily_kwh = 12.0
        elif temp <= 7.0:
            daily_kwh = 8.0
        elif temp <= 15.0:
            daily_kwh = 4.0
        else:
            daily_kwh = 1.2
    return max(0.0, daily_kwh * max(0.0, float(horizon_h)) / 24.0)


def predict_wp_energy_need_kwh(
    forecast_temp_c: Optional[float] = None,
    *,
    horizon_h: float = DEFAULT_HORIZON_H,
    now_ts: Optional[float] = None,
    prediction_path: str = DEFAULT_ML_PREDICTION_PATH,
    db_path: str = DEFAULT_DB_PATH,
    user_fallback_kwh: Optional[float] = None,
    scientific_fallback_kwh: Optional[float] = None,
    max_prediction_age_s: float = DEFAULT_MAX_PREDICTION_AGE_S,
) -> HeatForecastResult:
    """Return the best 24h heat-pump energy need estimate.

    Source priority:
    1. Fresh ML timeline from ml_prediction.json.
    2. Empirical daily_stats.wp_consumption percentile.
    3. Explicit user fallback from config.
    4. Conservative temperature fallback.
    """

    now = float(now_ts if now_ts is not None else time.time())
    pred = _prediction_result(
        prediction_path=prediction_path,
        now_ts=now,
        horizon_h=horizon_h,
        max_age_s=max_prediction_age_s,
    )
    if pred is not None and pred.quality != "stale":
        return pred

    stats = _daily_stats_result(db_path, horizon_h=horizon_h)
    if stats is not None:
        if pred is not None and pred.stale:
            details = dict(stats.details)
            details["stale_prediction"] = pred.as_dict()
            return HeatForecastResult(
                need_kwh=stats.need_kwh,
                source=stats.source,
                quality=stats.quality,
                reason=stats.reason,
                horizon_h=stats.horizon_h,
                coverage_h=stats.coverage_h,
                forecast_temp_c=stats.forecast_temp_c,
                details=details,
            )
        return stats

    if user_fallback_kwh is not None:
        fallback = max(0.0, _safe_float(user_fallback_kwh, 0.0))
        return HeatForecastResult(
            need_kwh=fallback,
            source="user_fallback",
            quality="user_fallback",
            reason="configured_wp_daily_need",
            horizon_h=horizon_h,
            coverage_h=min(float(horizon_h), 24.0),
            forecast_temp_c=forecast_temp_c,
            details={"stale_prediction": pred.as_dict() if pred is not None and pred.stale else None},
        )

    fallback_value = (
        max(0.0, _safe_float(scientific_fallback_kwh, 0.0))
        if scientific_fallback_kwh is not None
        else _scientific_fallback_kwh(forecast_temp_c, horizon_h)
    )
    return HeatForecastResult(
        need_kwh=fallback_value,
        source="scientific_fallback",
        quality="scientific_fallback",
        reason="temperature_degree_day_fallback",
        horizon_h=horizon_h,
        coverage_h=min(float(horizon_h), 24.0),
        forecast_temp_c=forecast_temp_c,
        details={"stale_prediction": pred.as_dict() if pred is not None and pred.stale else None},
    )


def calculate_heat_deficit_kwh(
    need_kwh: Any,
    *,
    pv_coverage_kwh: Any = 0.0,
    storage_coverage_kwh: Any = 0.0,
    delivered_kwh: Any = 0.0,
) -> float:
    """Return the uncovered heat energy for the policy boost horizon."""

    return max(
        0.0,
        _safe_float(need_kwh, 0.0)
        - _safe_float(pv_coverage_kwh, 0.0)
        - _safe_float(storage_coverage_kwh, 0.0)
        - _safe_float(delivered_kwh, 0.0),
    )
