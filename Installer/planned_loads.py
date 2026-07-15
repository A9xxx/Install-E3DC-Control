#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Planned external load windows for storage planning and live control.

The module is intentionally strict: it only accepts explicit, static user
windows. It does not try to infer wallboxes, heaters, or workshop loads from a
high house consumption signal.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from reserve import effective_ep_reserve_pct
except Exception:  # pragma: no cover - package import fallback
    from .reserve import effective_ep_reserve_pct  # type: ignore


SLOT_MS = 15 * 60 * 1000
DAY_MINUTES = 24 * 60


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return float(default)
        text = str(value).strip().replace(",", ".")
        if not text or text.lower() in ("none", "null", "nan", "inf", "infinity"):
            return float(default)
        result = float(text)
        return result if math.isfinite(result) else float(default)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    return int(round(_safe_float(value, default)))


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text == "":
        return bool(default)
    return text in ("1", "true", "yes", "on", "ja", "ein", "aktiv")


def _normalize_mode(value: Any) -> str:
    text = str(value or "protect_storage").strip().lower()
    text = (
        text.replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
        .replace("-", "_")
        .replace(" ", "_")
    )
    if text in (
        "price_support",
        "price_guided_support",
        "preisgefuehrt_stuetzen",
        "preisgefuehrt_stuetz",
        "preisgefuehrt",
        "stuetzen",
    ):
        return "price_support"
    return "protect_storage"


def _cfg_get(cfg: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    if not isinstance(cfg, dict):
        return default
    lower = {str(k).lower(): v for k, v in cfg.items()}
    for key in keys:
        if key in cfg:
            return cfg[key]
        lk = str(key).lower()
        if lk in lower:
            return lower[lk]
    return default


def _parse_minute(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minute = int(round(float(value)))
        return minute % DAY_MINUTES
    text = str(value).strip()
    if ":" in text:
        parts = text.split(":", 2)
        try:
            hour = int(float(parts[0]))
            minute = int(float(parts[1]))
            return (hour * 60 + minute) % DAY_MINUTES
        except Exception:
            return default
    minute = _safe_int(text, -1)
    if minute < 0:
        return default
    return minute % DAY_MINUTES


def _parse_weekdays(value: Any) -> Tuple[int, ...]:
    if value is None or value == "":
        return ()
    aliases = {
        "mo": 0, "mon": 0, "montag": 0,
        "di": 1, "die": 1, "tue": 1, "dienstag": 1,
        "mi": 2, "mit": 2, "wed": 2, "mittwoch": 2,
        "do": 3, "don": 3, "thu": 3, "donnerstag": 3,
        "fr": 4, "fre": 4, "fri": 4, "freitag": 4,
        "sa": 5, "sam": 5, "sat": 5, "samstag": 5,
        "so": 6, "son": 6, "sun": 6, "sonntag": 6,
    }
    if isinstance(value, str):
        raw_items: Iterable[Any] = value.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(value, Sequence):
        raw_items = value
    else:
        raw_items = [value]
    days = set()
    for item in raw_items:
        text = str(item).strip().lower()
        if not text:
            continue
        if text in ("all", "alle", "daily", "taeglich", "täglich", "*"):
            return ()
        if text in aliases:
            days.add(aliases[text])
            continue
        day = _safe_int(text, -1)
        if 0 <= day <= 6:
            days.add(day)
        elif 1 <= day <= 7:
            days.add((day - 1) % 7)
    return tuple(sorted(days))


def _current_price_ct_from_plan(plan: Dict[str, Any]) -> Optional[float]:
    if not isinstance(plan, dict):
        return None
    for key in ("current_price_ct", "price_ct", "awattar_price_ct"):
        price = _safe_float(plan.get(key), -1.0)
        if price > 0:
            return price
    price = _safe_float(plan.get("awattar_price"), -1.0)
    if price <= 0:
        return None
    return price / 10.0 if price > 80.0 else price


def _future_recovery_wh(plan: Dict[str, Any], now_s: float) -> float:
    timeline = plan.get("timeline") if isinstance(plan, dict) else []
    if not isinstance(timeline, list):
        return 0.0
    now_ms = float(now_s) * 1000.0
    horizon_ms = now_ms + 24.0 * 3600.0 * 1000.0
    recovery_wh = 0.0
    for slot in timeline:
        if not isinstance(slot, dict):
            continue
        ts = _safe_float(slot.get("ts", slot.get("start_timestamp")), 0.0)
        if ts < now_ms or ts > horizon_ms:
            continue
        end_ts = _safe_float(slot.get("end_timestamp"), ts + SLOT_MS)
        hours = max(0.0, min(SLOT_MS, max(0.0, end_ts - ts)) / 3600000.0)
        pv_w = _safe_float(slot.get("pv_w"), 0.0)
        home_w = _safe_float(slot.get("home_w"), 0.0)
        wp_w = _safe_float(slot.get("wp_w"), 0.0)
        planned_w = _safe_float(slot.get("planned_load_w"), 0.0)
        recovery_wh += max(0.0, pv_w - home_w - wp_w - planned_w) * hours
    return recovery_wh


def _raw_windows_from_config(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = _cfg_get(
        cfg,
        "planned_load_windows",
        "planned_loads",
        "geplante_lasten",
        default=[],
    )
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
            raw = decoded
        except Exception:
            return []
    if isinstance(raw, dict):
        raw = raw.get("windows", raw.get("items", []))
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


@dataclass(frozen=True)
class PlannedLoadWindow:
    name: str
    kind: str
    start_min: int
    end_min: int
    duration_min: int
    power_w: int
    min_power_w: int
    min_duration_min: int
    tolerance_w: int
    confirm_grace_min: int
    late_grace_min: int
    early_grace_min: int
    mode: str
    weekdays: Tuple[int, ...]

    def public_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["weekdays"] = list(self.weekdays)
        data["start"] = minutes_to_hhmm(self.start_min)
        data["end"] = minutes_to_hhmm(self.end_min)
        return data


def minutes_to_hhmm(minute: int) -> str:
    minute = int(minute) % DAY_MINUTES
    return "%02d:%02d" % (minute // 60, minute % 60)


def planned_windows_from_config(cfg: Dict[str, Any]) -> Tuple[List[PlannedLoadWindow], List[Dict[str, Any]]]:
    """Return valid planned load windows and rejected entries with reasons."""
    if not _truthy(_cfg_get(cfg, "planned_load_enable", "planned_loads_enable", default=True), True):
        return [], []

    windows: List[PlannedLoadWindow] = []
    rejected: List[Dict[str, Any]] = []
    default_min_power = max(1000, _safe_int(_cfg_get(cfg, "planned_load_min_power_w", default=1500), 1500))
    default_min_duration = max(15, _safe_int(_cfg_get(cfg, "planned_load_min_duration_min", default=30), 30))
    default_grace = max(0, _safe_int(_cfg_get(cfg, "planned_load_confirm_grace_min", default=15), 15))
    default_late = max(0, _safe_int(_cfg_get(cfg, "planned_load_late_grace_min", default=30), 30))
    default_early = max(0, _safe_int(_cfg_get(cfg, "planned_load_early_grace_min", default=0), 0))

    for idx, item in enumerate(_raw_windows_from_config(cfg)):
        name = str(item.get("name") or item.get("label") or ("Last %d" % (idx + 1))).strip()
        if not _truthy(item.get("enabled", item.get("active", True)), True):
            continue
        start_min = _parse_minute(item.get("start", item.get("start_time", item.get("start_minute"))))
        end_min = _parse_minute(item.get("end", item.get("end_time", item.get("end_minute"))))
        if start_min is None or end_min is None:
            rejected.append({"name": name, "reason": "start_end_invalid"})
            continue
        duration_min = (end_min - start_min) % DAY_MINUTES
        if duration_min <= 0:
            duration_min = DAY_MINUTES
        power_w = max(0, _safe_int(item.get("power_w", item.get("leistung_w", item.get("load_w"))), 0))
        min_power_w = max(300, _safe_int(item.get("min_power_w"), default_min_power))
        min_duration_min = max(1, _safe_int(item.get("min_duration_min"), default_min_duration))
        if power_w < min_power_w:
            rejected.append({"name": name, "reason": "power_below_min", "power_w": power_w, "min_power_w": min_power_w})
            continue
        if duration_min < min_duration_min:
            rejected.append({"name": name, "reason": "duration_below_min", "duration_min": duration_min, "min_duration_min": min_duration_min})
            continue
        tolerance_w = _safe_int(item.get("tolerance_w"), 0)
        if tolerance_w <= 0:
            tolerance_w = max(500, int(round(power_w * 0.20)))
        windows.append(
            PlannedLoadWindow(
                name=name,
                kind=str(item.get("type", item.get("kind", "external_load")) or "external_load").strip().lower(),
                start_min=int(start_min),
                end_min=int(end_min),
                duration_min=int(duration_min),
                power_w=int(power_w),
                min_power_w=int(min_power_w),
                min_duration_min=int(min_duration_min),
                tolerance_w=int(tolerance_w),
                confirm_grace_min=max(0, _safe_int(item.get("confirm_grace_min"), default_grace)),
                late_grace_min=max(0, _safe_int(item.get("late_grace_min"), default_late)),
                early_grace_min=max(0, _safe_int(item.get("early_grace_min"), default_early)),
                mode=_normalize_mode(item.get("mode", "protect_storage")),
                weekdays=_parse_weekdays(item.get("weekdays", item.get("days"))),
            )
        )
    return windows, rejected


def _candidate_bounds(window: PlannedLoadWindow, dt: datetime, include_grace: bool) -> Iterable[Tuple[datetime, datetime, datetime]]:
    base_midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset_days in (-1, 0, 1):
        start_day = base_midnight + timedelta(days=offset_days)
        if window.weekdays and start_day.weekday() not in window.weekdays:
            continue
        start = start_day + timedelta(minutes=window.start_min)
        end = start + timedelta(minutes=window.duration_min)
        if include_grace:
            match_start = start - timedelta(minutes=window.early_grace_min)
            match_end = end + timedelta(minutes=window.late_grace_min)
        else:
            match_start = start
            match_end = end
        yield start, end, match_start, match_end


def active_windows_at(
    windows: Sequence[PlannedLoadWindow],
    dt: datetime,
    *,
    include_grace: bool = False,
) -> List[Tuple[PlannedLoadWindow, datetime, datetime]]:
    active: List[Tuple[PlannedLoadWindow, datetime, datetime]] = []
    for window in windows:
        for start, end, match_start, match_end in _candidate_bounds(window, dt, include_grace):
            if match_start <= dt < match_end:
                active.append((window, start, end))
                break
    return active


def planned_load_w_at(windows: Sequence[PlannedLoadWindow], dt: datetime) -> Tuple[int, List[str]]:
    active = active_windows_at(windows, dt, include_grace=False)
    watts = int(sum(window.power_w for window, _start, _end in active))
    return watts, [window.name for window, _start, _end in active]


def apply_planned_loads_to_timeline(
    timeline: List[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    windows, rejected = planned_windows_from_config(cfg)
    if not windows:
        return {"enabled": False, "windows": [], "rejected": rejected, "total_wh": 0}
    total_wh = 0.0
    active_slots = 0
    for slot in timeline or []:
        try:
            dt = datetime.fromtimestamp(_safe_float(slot.get("ts"), 0.0) / 1000.0)
        except Exception:
            continue
        planned_w, names = planned_load_w_at(windows, dt)
        slot["planned_load_w"] = int(planned_w)
        if names:
            slot["planned_load_names"] = names
        pv_w = _safe_float(slot.get("pv_w"), 0.0)
        home_w = _safe_float(slot.get("home_w"), 0.0)
        wp_w = _safe_float(slot.get("wp_w"), 0.0)
        slot["surplus_w"] = pv_w - home_w - wp_w - planned_w
        charge_w = _safe_float(slot.get("charge_w"), 0.0)
        if planned_w > 0 or abs(charge_w) <= 1:
            max_charge_w = max(0.0, _safe_float(slot.get("max_charge_w"), 0.0))
            if slot["surplus_w"] < 0:
                slot["charge_w"] = slot["surplus_w"]
            elif max_charge_w > 0:
                slot["charge_w"] = min(slot["surplus_w"], max_charge_w)
            else:
                slot["charge_w"] = slot["surplus_w"]
        if planned_w > 0:
            active_slots += 1
            total_wh += planned_w * (SLOT_MS / 3600000.0)
    return {
        "enabled": True,
        "windows": [window.public_dict() for window in windows],
        "rejected": rejected,
        "total_wh": round(total_wh, 0),
        "active_slots": active_slots,
    }


def current_plan_slot(plan: Dict[str, Any], now_s: float) -> Dict[str, Any]:
    timeline = plan.get("timeline") if isinstance(plan, dict) else []
    if not isinstance(timeline, list) or not timeline:
        return {}
    now_ms = float(now_s) * 1000.0
    best = {}
    best_dist = float("inf")
    for slot in timeline:
        if not isinstance(slot, dict):
            continue
        ts = _safe_float(slot.get("ts"), 0.0)
        if ts <= 0:
            continue
        end_ts = _safe_float(slot.get("end_timestamp"), ts + SLOT_MS)
        if ts <= now_ms < end_ts:
            return slot
        dist = abs(ts - now_ms)
        if dist < best_dist:
            best = slot
            best_dist = dist
    return best if best_dist <= SLOT_MS else {}


def price_support_guard(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    active: Sequence[Tuple[PlannedLoadWindow, datetime, datetime]],
    now_s: float,
    expected_w: int,
    observed_extra_w: int,
) -> Dict[str, Any]:
    """Return whether a planned load may be supported from the battery.

    This is intentionally separate from the hard protect-storage mode. A
    support window only opens if every guard is plausible; otherwise the caller
    can fall back to the existing zero-discharge storage protection.
    """
    now_dt = datetime.fromtimestamp(float(now_s))
    support_windows = [
        (window, start, end)
        for window, start, end in active
        if window.mode == "price_support"
    ]
    if not support_windows:
        return {"allowed": False, "reason": "not_price_support_window"}

    price_ct = _current_price_ct_from_plan(plan)
    min_price_ct = _safe_float(
        _cfg_get(cfg, "planned_load_support_min_price_ct", default=None),
        _safe_float(_cfg_get(cfg, "price_pause_limit", default=35.0), 35.0),
    )
    if price_ct is None:
        return {"allowed": False, "reason": "price_missing", "min_price_ct": round(min_price_ct, 3)}
    if price_ct < min_price_ct:
        return {
            "allowed": False,
            "reason": "price_below_threshold",
            "price_ct": round(price_ct, 3),
            "min_price_ct": round(min_price_ct, 3),
        }

    soc = _safe_float(live.get("SOC", live.get("Battery_SoC")), 0.0)
    ep_reserve = effective_ep_reserve_pct(cfg, live, default=0.0)
    morning_reserve = max(0.0, _safe_float(_cfg_get(cfg, "storage_morning_soc", default=0.0), 0.0))
    min_soc = max(
        ep_reserve + 8.0,
        morning_reserve,
        _safe_float(_cfg_get(cfg, "planned_load_support_min_soc", default=35.0), 35.0),
    )
    if soc <= min_soc:
        return {
            "allowed": False,
            "reason": "soc_below_support_floor",
            "soc": round(soc, 2),
            "min_soc": round(min_soc, 2),
            "ep_reserve_pct": round(ep_reserve, 2),
            "morning_reserve_soc": round(morning_reserve, 2),
        }

    max_energy_wh = max(0.0, _safe_float(_cfg_get(cfg, "planned_load_support_max_kwh", default=2.0), 2.0) * 1000.0)
    if max_energy_wh < 100.0:
        return {"allowed": False, "reason": "max_energy_zero"}

    end = max((end for _window, _start, end in support_windows), default=now_dt)
    remaining_h = max(0.25, (end - now_dt).total_seconds() / 3600.0)
    observed_w = max(0, int(observed_extra_w))
    base_support_w = max(0, min(int(expected_w), observed_w if observed_w > 0 else int(expected_w)))
    max_energy_power_w = int(max(0.0, max_energy_wh / remaining_h))
    max_discharge_w = max(0, _safe_int(_cfg_get(cfg, "maximaleentladeleistung", default=0), 0))
    if max_discharge_w <= 0:
        max_discharge_w = max(0, _safe_int(_cfg_get(cfg, "maximumladeleistung", default=0), 0))
    support_w = min(
        power
        for power in (base_support_w, max_energy_power_w, max_discharge_w or base_support_w)
        if power >= 0
    )
    if support_w < 300:
        return {
            "allowed": False,
            "reason": "support_power_below_min",
            "support_w": int(support_w),
            "max_energy_wh": int(max_energy_wh),
            "remaining_h": round(remaining_h, 2),
        }

    planned_energy_wh = support_w * remaining_h
    live_capacity = _safe_float(
        live.get("bat_total_usable_kwh"),
        _safe_float(
            live.get("bat_usable_kwh"),
            _safe_float(
                live.get("bat_total_full_cap_kwh"),
                _safe_float(live.get("bat_full_cap_kwh"), 10.0),
            ),
        ),
    )
    capacity_kwh = max(1.0, _safe_float(_cfg_get(cfg, "speichergroesse", default=0.0), live_capacity))
    soc_after_support = soc - (planned_energy_wh / (capacity_kwh * 1000.0) * 100.0)
    if soc_after_support < min_soc:
        return {
            "allowed": False,
            "reason": "energy_would_cross_support_floor",
            "soc": round(soc, 2),
            "soc_after_support": round(soc_after_support, 2),
            "min_soc": round(min_soc, 2),
            "support_energy_wh": int(planned_energy_wh),
        }

    recovery_wh = _future_recovery_wh(plan, now_s)
    recovery_factor = max(1.0, _safe_float(_cfg_get(cfg, "planned_load_support_pv_recovery_factor", default=1.2), 1.2))
    required_recovery_wh = planned_energy_wh * recovery_factor
    if recovery_wh < required_recovery_wh:
        return {
            "allowed": False,
            "reason": "pv_recovery_too_low",
            "future_recovery_wh": int(recovery_wh),
            "required_recovery_wh": int(required_recovery_wh),
        }

    return {
        "allowed": True,
        "reason": "price_support_allowed",
        "mode": "price_support",
        "price_ct": round(price_ct, 3),
        "min_price_ct": round(min_price_ct, 3),
        "soc": round(soc, 2),
        "min_soc": round(min_soc, 2),
        "soc_after_support": round(soc_after_support, 2),
        "ep_reserve_pct": round(ep_reserve, 2),
        "morning_reserve_soc": round(morning_reserve, 2),
        "support_max_discharge_w": int(support_w),
        "support_energy_wh": int(planned_energy_wh),
        "support_max_energy_wh": int(max_energy_wh),
        "future_recovery_wh": int(recovery_wh),
        "required_recovery_wh": int(required_recovery_wh),
        "remaining_h": round(remaining_h, 2),
    }


def current_planned_load_status(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    now_s: float,
) -> Dict[str, Any]:
    windows, rejected = planned_windows_from_config(cfg)
    if not windows:
        return {"active": False, "confirmed": False, "reason": "no_windows", "rejected": rejected}
    now_dt = datetime.fromtimestamp(float(now_s))
    active = active_windows_at(windows, now_dt, include_grace=True)
    if not active:
        return {"active": False, "confirmed": False, "reason": "outside_window", "rejected": rejected}
    protect_active = any(
        window.mode in ("protect_storage", "hold", "grid", "grid_preferred", "netz")
        for window, _start, _end in active
    )
    support_active = any(window.mode == "price_support" for window, _start, _end in active)
    if not protect_active and not support_active:
        return {"active": False, "confirmed": False, "reason": "plan_only_window", "rejected": rejected}

    plan_slot = current_plan_slot(plan, now_s)
    if not plan_slot:
        return {"active": True, "confirmed": False, "reason": "no_plan_slot", "rejected": rejected}
    planned_w = _safe_int(plan_slot.get("planned_load_w"), 0) if plan_slot else 0
    if planned_w <= 0:
        planned_w = int(sum(window.power_w for window, _start, _end in active))
    planned_w = max(0, planned_w)
    if planned_w <= 0:
        return {"active": False, "confirmed": False, "reason": "no_planned_power", "rejected": rejected}

    baseline_home_w = max(0, _safe_int(plan_slot.get("home_w"), 0)) if plan_slot else 0
    observed_home_w = max(0, _safe_int(live.get("Home_Power"), 0))
    if _truthy(live.get("Wallbox_Home_Includes"), False):
        observed_home_w = max(0, observed_home_w - max(0, _safe_int(live.get("Wallbox_Power"), 0)))
    observed_extra_w = max(0, observed_home_w - baseline_home_w)

    tolerance_w = max(window.tolerance_w for window, _start, _end in active)
    confirmed = observed_extra_w >= max(300, planned_w - tolerance_w)
    mode = "protect_storage" if protect_active else "price_support"
    support = {}
    if support_active and confirmed:
        support = price_support_guard(
            cfg,
            live,
            plan,
            active,
            now_s,
            int(planned_w),
            int(observed_extra_w),
        )
    starts = [start for _window, start, _end in active]
    ends = [end for _window, _start, end in active]
    start = min(starts)
    end = max(ends)
    if now_dt < start:
        phase = "early_grace"
    elif now_dt >= end:
        phase = "late_grace"
    else:
        phase = "window"
    return {
        "active": True,
        "confirmed": bool(confirmed),
        "phase": phase,
        "mode": mode,
        "expected_w": int(planned_w),
        "observed_extra_w": int(observed_extra_w),
        "observed_home_w": int(observed_home_w),
        "baseline_home_w": int(baseline_home_w),
        "tolerance_w": int(tolerance_w),
        "windows": [window.public_dict() for window, _start, _end in active],
        "start_ts": int(start.timestamp()),
        "end_ts": int(end.timestamp()),
        "support_allowed": bool(support.get("allowed", False)),
        "support_reason": support.get("reason", ""),
        "support": support,
        "reason": (
            "price_support_allowed"
            if bool(support.get("allowed", False))
            else (support.get("reason") if support else ("confirmed" if confirmed else "waiting_for_static_load"))
        ),
        "rejected": rejected,
    }
