#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Helpers for E3DC emergency-power reserve normalisation."""

from __future__ import annotations

import math
from typing import Any, Dict, Optional


def safe_float(value: Any, default: float = 0.0) -> float:
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


def _first_number(data: Dict[str, Any], *keys: str) -> tuple[Optional[float], Optional[str]]:
    for key in keys:
        value = safe_float(data.get(key), float("nan"))
        if math.isfinite(value) and abs(value) > 0.0001:
            return value, key
    return None, None


def effective_capacity_kwh(cfg: Dict[str, Any] | None, live: Dict[str, Any] | None = None) -> tuple[float, str]:
    """Return the system-level usable battery capacity in kWh."""
    cfg = cfg or {}
    live = live or {}
    cfg_capacity = safe_float(cfg.get("speichergroesse"), 0.0)
    if cfg_capacity > 0.1:
        return cfg_capacity, "config:speichergroesse"

    for key in (
        "bat_total_usable_kwh",
        "bat_total_full_cap_kwh",
        "bat_total_specified_kwh",
    ):
        value = safe_float(live.get(key), 0.0)
        if value > 0.1:
            if key == "bat_total_specified_kwh":
                return value * 0.9, f"{key}*0.9"
            return value, key

    cabinets: list[tuple[float, str]] = []
    for prefix in ("bat", "bat1", "bat2", "bat3"):
        usable = safe_float(live.get(f"{prefix}_usable_kwh"), 0.0)
        full = safe_float(live.get(f"{prefix}_full_cap_kwh"), 0.0)
        specified = safe_float(live.get(f"{prefix}_specified_kwh"), 0.0)
        voltage = safe_float(live.get(f"{prefix}_v"), 0.0)
        active = voltage > 5.0 or specified > 0.1 or usable > 0.1 or full > 0.1
        if not active:
            continue
        value = usable if usable > 0.1 else full
        if specified > 0.1 and (value <= 0.1 or value > specified * 1.15 or value < specified * 0.45):
            value = specified * 0.9
        if value > 0.1:
            cabinets.append((value, f"{prefix}_usable_kwh"))
    if cabinets:
        return sum(value for value, _source in cabinets), "+".join(source for _value, source in cabinets)

    for key in (
        "real_usable_capacity_kwh",
        "usable_capacity_kwh",
        "bat_total_usable_kwh",
        "bat_usable_kwh",
        "bat_capacity_kwh",
        "bat_total_full_cap_kwh",
        "bat_full_cap_kwh",
    ):
        value = safe_float(live.get(key), 0.0)
        if value > 0.1:
            pack_count = max(1.0, safe_float(live.get("bat_dcb_count"), 1.0))
            if pack_count > 1.0 and 0.1 < value < 5.0:
                return value * pack_count, f"{key}*bat_dcb_count"
            return value, key
    for key in (
        "real_usable_capacity_wh",
        "usable_capacity_wh",
        "installed_capacity_wh",
        "battery_capacity_wh",
    ):
        value = safe_float(live.get(key), 0.0)
        if value > 100.0:
            return value / 1000.0, key
    return 0.0, ""


def live_ep_reserve_details(cfg: Dict[str, Any] | None, live: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Normalise E3DC EP reserve values to the whole system capacity."""
    cfg = cfg or {}
    live = live or {}
    capacity_kwh, capacity_source = effective_capacity_kwh(cfg, live)
    capacity_wh = capacity_kwh * 1000.0 if capacity_kwh > 0.1 else 0.0
    raw_pct, raw_key = _first_number(
        live,
        "ep_reserve_raw_pct",
        "ep_reserve_pct",
        "notstrom_reserve",
        "emergency_reserve_pct",
        "reserve_percent",
    )
    energy_wh, energy_key = _first_number(
        live,
        "ep_reserve_energy_wh",
        "reserve_energy_wh",
        "reserve_energy",
    )
    max_energy_wh, max_energy_key = _first_number(
        live,
        "ep_reserve_max_energy_wh",
        "reserve_max_energy_wh",
        "reserve_max_energy",
        "reserve_max",
    )

    effective_pct: Optional[float] = None
    source = ""
    normalised = False
    if energy_wh is not None and capacity_wh > 100.0:
        effective_pct = max(0.0, min(100.0, energy_wh / capacity_wh * 100.0))
        source = "rscp_energy_wh"
        normalised = raw_pct is not None and abs(effective_pct - raw_pct) >= 0.2
    elif raw_pct is not None and max_energy_wh is not None and capacity_wh > 100.0:
        reserve_energy = max_energy_wh * raw_pct / 100.0
        effective_pct = max(0.0, min(100.0, reserve_energy / capacity_wh * 100.0))
        source = "rscp_percent_scaled_by_reserve_max"
        normalised = abs(effective_pct - raw_pct) >= 0.2
    elif raw_pct is not None:
        effective_pct = max(0.0, min(100.0, raw_pct))
        source = "rscp_percent"

    return {
        "effective_pct": effective_pct,
        "raw_pct": raw_pct,
        "raw_key": raw_key,
        "energy_wh": energy_wh,
        "energy_key": energy_key,
        "max_energy_wh": max_energy_wh,
        "max_energy_key": max_energy_key,
        "capacity_kwh": capacity_kwh,
        "capacity_source": capacity_source,
        "source": source,
        "normalised": normalised,
    }


def effective_ep_reserve_pct(
    cfg: Dict[str, Any] | None,
    live: Dict[str, Any] | None = None,
    default: float = 8.0,
) -> float:
    """Return the protected reserve floor, with config as fallback/conservative floor."""
    cfg = cfg or {}
    live = live or {}
    cfg_pct = safe_float(cfg.get("ep_reserve_pct"), default)
    details = live_ep_reserve_details(cfg, live)
    live_pct = details.get("effective_pct")
    return max(0.0, cfg_pct, safe_float(live_pct, 0.0))


def normalise_live_ep_reserve(live: Dict[str, Any], cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Mutate live data so ep_reserve_pct is always whole-system based."""
    details = live_ep_reserve_details(cfg, live)
    raw_pct = details.get("raw_pct")
    effective_pct = details.get("effective_pct")
    if raw_pct is not None:
        live["ep_reserve_raw_pct"] = round(float(raw_pct), 2)
    if effective_pct is not None:
        live["ep_reserve_pct"] = round(float(effective_pct), 2)
        live["ep_reserve_effective_pct"] = round(float(effective_pct), 2)
    if details.get("energy_wh") is not None:
        live["ep_reserve_energy_wh"] = round(float(details["energy_wh"]), 1)
    if details.get("max_energy_wh") is not None:
        live["ep_reserve_max_energy_wh"] = round(float(details["max_energy_wh"]), 1)
    if details.get("capacity_kwh"):
        live["ep_reserve_capacity_kwh"] = round(float(details["capacity_kwh"]), 3)
    live["ep_reserve_source"] = details.get("source") or "unavailable"
    live["ep_reserve_normalized"] = bool(details.get("normalised"))
    return live
