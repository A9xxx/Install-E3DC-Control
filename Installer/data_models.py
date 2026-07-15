#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Typed snapshots for the JSON edges used by the control managers.

The productive files stay plain JSON/dict for compatibility.  These models sit
just inside the edge and give tests and review code stable field names.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return float(default)
        text = str(value).strip().replace(",", ".")
        if text == "" or text.lower() in ("none", "null", "nan", "inf", "infinity"):
            return float(default)
        result = float(text)
        return result if math.isfinite(result) else float(default)
    except Exception:
        return float(default)


def safe_int(value: Any, default: int = 0) -> int:
    return int(round(safe_float(value, default)))


def _first_number(data: Dict[str, Any], keys: Iterable[str], default: float = 0.0) -> float:
    for key in keys:
        if key in data:
            return safe_float(data.get(key), default)
    return float(default)


def _copy_aliases(data: Dict[str, Any], pairs: Iterable[tuple[str, str]]) -> Dict[str, Any]:
    normalized = dict(data)
    for canonical, legacy in pairs:
        if canonical in normalized and legacy not in normalized:
            normalized[legacy] = normalized[canonical]
        if legacy in normalized and canonical not in normalized:
            normalized[canonical] = normalized[legacy]
    return normalized


LIVE_ALIASES = (
    ("Grid_Power", "Grid"),
    ("Battery_Power", "Battery"),
    ("PV_Power", "PV"),
    ("Home_Power", "Home"),
    ("Wallbox_Power", "WB"),
    ("Battery_SoC", "SOC"),
)


def _bool_field(data: Dict[str, Any], key: str, default: bool = True) -> bool:
    if key not in data:
        return bool(default)
    value = data.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "ok", "valid"):
        return True
    if text in ("0", "false", "no", "off", "invalid"):
        return False
    return bool(default)


def live_power_plausibility(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize e3dc-live plausibility metadata for control consumers."""
    raw = data if isinstance(data, dict) else {}
    power_meta = raw.get("Power_Plausibility") if isinstance(raw.get("Power_Plausibility"), dict) else {}
    reasons_raw = raw.get("RSCP_Glitch_Reasons", power_meta.get("reasons", []))
    if isinstance(reasons_raw, (list, tuple, set)):
        reasons = sorted({str(item) for item in reasons_raw if str(item or "").strip()})
    elif str(reasons_raw or "").strip():
        reasons = [str(reasons_raw).strip()]
    else:
        reasons = []

    home_source = str(raw.get("Home_Power_Source", power_meta.get("home_source", "")) or "").strip()
    home_valid = _bool_field(raw, "Home_Power_Valid", _bool_field(power_meta, "home_valid", True))
    grid_valid = _bool_field(raw, "Grid_Power_Valid", _bool_field(power_meta, "grid_valid", True))
    sample_valid = _bool_field(raw, "RSCP_Sample_Valid", _bool_field(power_meta, "sample_valid", True))
    if home_source.startswith("invalid_"):
        home_valid = False
    if reasons:
        sample_valid = False
    if not home_valid or not grid_valid:
        sample_valid = False

    return {
        "sample_valid": bool(sample_valid),
        "home_valid": bool(home_valid),
        "grid_valid": bool(grid_valid),
        "home_independent": _bool_field(
            raw,
            "Home_Power_Independent",
            _bool_field(power_meta, "home_independent", True),
        ),
        "home_source": home_source or "legacy_unmarked",
        "home_balance_w": safe_int(raw.get("Home_Power_Balance", power_meta.get("home_balance_w")), 0),
        "home_delta_w": safe_int(raw.get("Home_Power_Delta", power_meta.get("home_delta_w")), 0),
        "grid_pm_delta_w": safe_int(raw.get("Grid_PM_Delta", power_meta.get("grid_pm_delta_w")), 0),
        "reasons": reasons,
    }


def control_home_power_w(data: Optional[Dict[str, Any]], default: int = 0) -> int:
    """Return a conservative home-load value for control paths."""
    raw = data if isinstance(data, dict) else {}
    meta = live_power_plausibility(raw)
    if meta["home_valid"]:
        return max(0, safe_int(_first_number(raw, ("Home_Power", "Home"), default), default))
    if meta["home_balance_w"] > 0 and meta["grid_valid"]:
        return max(0, safe_int(meta["home_balance_w"], default))
    return max(0, safe_int(default, 0))


@dataclass(frozen=True)
class LiveDataSnapshot:
    raw: Dict[str, Any] = field(default_factory=dict)
    soc: float = 0.0
    pv_w: int = 0
    grid_w: int = 0
    home_w: int = 0
    battery_w: int = 0
    wallbox_w: int = 0
    heatpump_w: int = 0
    ts: float = 0.0
    age_s: float = 0.0
    plausibility: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]], *, now_s: Optional[float] = None) -> "LiveDataSnapshot":
        normalized = normalize_live_data_dict(data or {})
        now = time.time() if now_s is None else float(now_s)
        ts = safe_float(normalized.get("_ts"), 0.0)
        age_s = max(0.0, now - ts) if ts > 0.0 else 0.0
        plausibility = live_power_plausibility(normalized)
        return cls(
            raw=normalized,
            soc=_first_number(normalized, ("SOC", "Battery_SoC"), 0.0),
            pv_w=safe_int(_first_number(normalized, ("PV_Power", "PV"), 0.0)),
            grid_w=safe_int(_first_number(normalized, ("Grid_Power", "Grid"), 0.0)),
            home_w=control_home_power_w(normalized, 0),
            battery_w=safe_int(_first_number(normalized, ("Battery_Power", "Battery"), 0.0)),
            wallbox_w=safe_int(_first_number(normalized, ("Wallbox_Power", "WB"), 0.0)),
            heatpump_w=safe_int(
                _first_number(
                    normalized,
                    ("WP_Power", "heizstab_power", "Heizstab_Power"),
                    0.0,
                )
            ),
            ts=ts,
            age_s=age_s,
            plausibility=plausibility,
        )

    def to_legacy_dict(self) -> Dict[str, Any]:
        return dict(self.raw)


@dataclass(frozen=True)
class WallboxDetailSnapshot:
    raw: Dict[str, Any] = field(default_factory=dict)
    charger_id: int = 0
    power_w: float = 0.0
    connected: bool = False
    charging: bool = False
    phases: int = 0

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "WallboxDetailSnapshot":
        raw = dict(data or {})
        power_w = max(
            0.0,
            safe_float(
                raw.get(
                    "power_w",
                    raw.get("real_power_w", raw.get("phase_power_sum_w", 0.0)),
                ),
                0.0,
            ),
        )
        connected = bool(raw.get("plug", raw.get("plug_state", raw.get("connected", False))))
        charging = bool(raw.get("charging", raw.get("charge_state", False))) or power_w > 250.0
        return cls(
            raw=raw,
            charger_id=safe_int(raw.get("id", raw.get("charger_id", 0)), 0),
            power_w=power_w,
            connected=connected,
            charging=charging,
            phases=safe_int(raw.get("phases_in_use", raw.get("physical_phases", 0)), 0),
        )


@dataclass(frozen=True)
class WallboxStatusSnapshot:
    raw: Dict[str, Any] = field(default_factory=dict)
    details: List[WallboxDetailSnapshot] = field(default_factory=list)
    total_power_w: float = 0.0
    charging_active: bool = False
    connected_count: int = 0

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "WallboxStatusSnapshot":
        raw = dict(data or {})
        raw_details = raw.get("wb_details") if isinstance(raw.get("wb_details"), list) else []
        details = [
            WallboxDetailSnapshot.from_dict(item)
            for item in raw_details
            if isinstance(item, dict)
        ]
        detail_power_w = sum(item.power_w for item in details if item.charging or item.power_w > 50.0)
        total_power_w = max(0.0, safe_float(raw.get("total_power_w"), detail_power_w))
        if detail_power_w > 50.0:
            total_power_w = detail_power_w
        charging_active = bool(raw.get("charging_active", False)) or any(item.charging for item in details)
        connected_count = sum(1 for item in details if item.connected)
        return cls(
            raw=raw,
            details=details,
            total_power_w=total_power_w,
            charging_active=charging_active,
            connected_count=connected_count,
        )

    def to_legacy_dict(self) -> Dict[str, Any]:
        return dict(self.raw)


@dataclass(frozen=True)
class StorageCurvePoint:
    ts: float
    soc: float
    pv_w: float = 0.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StorageCurvePoint":
        return cls(
            ts=safe_float(data.get("ts"), 0.0),
            soc=safe_float(data.get("soc"), 0.0),
            pv_w=safe_float(data.get("pv_w"), 0.0),
        )


@dataclass(frozen=True)
class StoragePlanSnapshot:
    raw: Dict[str, Any] = field(default_factory=dict)
    target_timeline: List[StorageCurvePoint] = field(default_factory=list)
    storage_target_curve: List[StorageCurvePoint] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    curve_end_ts: float = 0.0

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "StoragePlanSnapshot":
        raw = dict(data or {})
        target_raw = raw.get("target_timeline") if isinstance(raw.get("target_timeline"), list) else []
        storage_raw = raw.get("storage_target_curve") if isinstance(raw.get("storage_target_curve"), list) else []
        meta = raw.get("target_curve_meta") if isinstance(raw.get("target_curve_meta"), dict) else {}
        curve_end_ts = safe_float(raw.get("ladeende_ts"), safe_float(meta.get("curve_end_ts"), 0.0))
        return cls(
            raw=raw,
            target_timeline=[
                StorageCurvePoint.from_dict(item)
                for item in target_raw
                if isinstance(item, dict)
            ],
            storage_target_curve=[
                StorageCurvePoint.from_dict(item)
                for item in storage_raw
                if isinstance(item, dict)
            ],
            meta=dict(meta),
            curve_end_ts=curve_end_ts,
        )

    @property
    def has_target_curve(self) -> bool:
        return bool(self.target_timeline)

    def to_legacy_dict(self) -> Dict[str, Any]:
        return dict(self.raw)


def normalize_live_data_dict(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return _copy_aliases(dict(data or {}), LIVE_ALIASES)


def normalize_storage_plan_dict(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = dict(data or {})
    if not isinstance(raw.get("target_curve_meta"), dict):
        raw["target_curve_meta"] = {}
    if not isinstance(raw.get("target_timeline"), list):
        raw["target_timeline"] = []
    if not isinstance(raw.get("storage_target_curve"), list):
        raw["storage_target_curve"] = []
    return raw
