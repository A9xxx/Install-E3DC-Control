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

# Gemeinsame Messfrist der Speicherregelung und ihrer Konfigurationsprüfung.
LIVE_SAMPLE_MAX_AGE_S = 10.0

# Karenz nur für den Stale-Wächter des Marktplans (plan_expired). valid_until_ts ist
# created_ts + 15 min, der Folgeplan steht aber erst nach dem Ende des Simulator-Laufs in der Datei (gemessen
# ~0,5 s + Jitter; auf langsamen Hosts bis ~9 s). Ohne Karenz sah jeder Regelzyklus in dieser Lücke einen
# abgelaufenen Plan (Entladefreigabe für einen Zyklus, GRID -> AUTO -> GRID). 20 s = mehr als das Doppelte der
# längsten beobachteten Laufzeit plus ein Regelzyklus, weit unter dem Slot; Vertragsende bleibt unverändert.
# Steht hier, damit Marktökonomie, Speicherdienst und Speicherkern dieselbe Definition lesen.
PLAN_STALE_GRACE_MS = 20 * 1000


def parse_mqtt_vehicle_allowlist(raw):
    """None = offen, leere Menge = ungültig/geschlossen; Segmente bleiben exakt."""
    if raw is None or raw == "":
        return None, True
    if not isinstance(raw, str):
        return frozenset(), False
    if not raw.strip(" \t\r\n\v\f"):
        return None, True
    names = [name.strip(" \t\r\n\v\f") for name in raw.split(",")]
    try:
        valid = all(name and not any(c in name for c in "/#+")
                    and not any(ord(c) < 32 or ord(c) == 127 for c in name)
                    and len(name.encode("utf-8")) <= 65535 for name in names)
    except UnicodeError:
        valid = False
    return (frozenset(names), True) if valid else (frozenset(), False)


def mqtt_vehicle_record_allowed(record, config=None):
    """Entwertet nur generische MQTT-Werte, einschließlich alter Fortschreibungen."""
    config = config if isinstance(config, dict) else {}
    allowed, valid = parse_mqtt_vehicle_allowlist(config.get("mqtt_vehicle_allowlist"))
    if allowed is None:
        return True
    record = record if isinstance(record, dict) else {}
    # Dieselben Leerzeichen wie im Listenparser und im PHP-Leser (mqttVehicleRecordAllowed).
    def first_text(*keys):
        return next((record[key].strip(" \t\r\n\v\f") for key in keys
                     if isinstance(record.get(key), str) and record[key].strip(" \t\r\n\v\f")), "")

    source = first_text("soc_source", "source", "anchor_source").lower()
    if source == "vehicle_cached_last_confirmed":
        source = str(record.get("soc_source_previous") or "").strip(" \t\r\n\v\f").lower()
    mqtt_source = source in ("mqtt", "mqtt_retained", "wallbox_estimated_from_mqtt",
                             "wallbox_estimated_from_mqtt_retained")
    origin = record.get("mqtt_vehicle_origin")
    if not mqtt_source and not (not source and origin == "generic"):
        return True
    if origin == "dedicated":
        return True
    name = first_text("mqtt_vehicle_id", "id", "car_id")
    if origin != "generic":
        # Der Altproduzent speicherte weder Topic noch Eingangsart.
        # Erst eine neue Nachricht belegt die dedizierte oder generische Herkunft.
        return False
    return bool(valid and name and name in allowed)


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


def live_number(data, keys, valid_key=None, *, soc=False):
    """Gemessener endlicher Wert oder None; keine Ersatzbilanz."""
    if valid_key and valid_key in data and not _bool_field(data, valid_key, False):
        return None
    for key in keys:
        if key not in data:
            continue
        raw = data[key]
        if isinstance(raw, bool) or raw is None:
            return None
        try:
            number = float(raw)
        except (ValueError, TypeError, OverflowError):
            return None
        if not math.isfinite(number) or (soc and not 0 <= number <= 100):
            return None
        return number
    return None


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
    home_valid = live_number(raw, ("Home_Power", "Home"), "Home_Power_Valid") is not None
    grid_valid = live_number(raw, ("Grid_Power", "Grid"), "Grid_Power_Valid") is not None
    sample_valid = _bool_field(raw, "RSCP_Sample_Valid", _bool_field(power_meta, "sample_valid", True))
    if home_source.startswith("invalid_"):
        home_valid = False
    if reasons:
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
        "home_balance_w": live_number(raw, ("Home_Power_Balance",)),
        "home_delta_w": live_number(raw, ("Home_Power_Delta",)),
        "grid_pm_delta_w": live_number(raw, ("Grid_PM_Delta",)),
        "reasons": reasons,
    }


def control_home_power_w(data: Optional[Dict[str, Any]], default=None):
    """Gemessener Hauswert; der Standard bleibt nur zur Aufrufkompatibilität."""
    value = live_number(data or {}, ("Home_Power", "Home"), "Home_Power_Valid")
    return value if value is not None and value >= -100 else None


@dataclass(frozen=True)
class LiveDataSnapshot:
    raw: Dict[str, Any] = field(default_factory=dict)
    soc: Optional[float] = None
    pv_w: Optional[float] = None
    grid_w: Optional[float] = None
    home_w: Optional[float] = None
    battery_w: Optional[float] = None
    wallbox_w: Optional[float] = None
    heatpump_w: int = 0
    ts: float = 0.0
    age_s: float = float("inf")
    plausibility: Dict[str, Any] = field(default_factory=dict)
    soc_valid: bool = False
    pv_valid: bool = False
    grid_valid: bool = False
    home_valid: bool = False
    battery_valid: bool = False

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]], *, now_s: Optional[float] = None) -> "LiveDataSnapshot":
        normalized = normalize_live_data_dict(data or {})
        now = time.time() if now_s is None else float(now_s)
        ts = safe_float(normalized.get("_ts"), 0.0)
        age_s = max(0.0, now - ts) if ts > 0.0 else float("inf")
        plausibility = live_power_plausibility(normalized)
        values = {
            "soc": live_number(normalized, ("SOC", "Battery_SoC"), "SOC_Valid", soc=True),
            "pv": live_number(normalized, ("PV_Power", "PV"), "PV_Power_Valid"),
            "grid": live_number(normalized, ("Grid_Power", "Grid"), "Grid_Power_Valid"),
            "home": control_home_power_w(normalized),
            "battery": live_number(normalized, ("Battery_Power", "Battery"), "Battery_Power_Valid"),
        }
        return cls(
            **{key + "_valid": value is not None for key, value in values.items()},
            raw=normalized,
            soc=values["soc"],
            pv_w=values["pv"],
            grid_w=values["grid"],
            home_w=values["home"],
            battery_w=values["battery"],
            wallbox_w=live_number(normalized, ("Wallbox_Power", "WB"), "Wallbox_Power_Valid"),
            heatpump_w=safe_int(
                _first_number(
                    normalized,
                    ("WP_Power", "Heatpump_Power"),
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


# Gemeinsame reine Wallbox-Messverträge, ohne Speicher- oder Treiberabhängigkeit.
STATUS_STALE_DEFAULT_S = 45.0

E3DC_WALLBOX_TYPES = frozenset((
    "native", "e3dc", "e3dc_easy", "e3dc_easy_connect", "e3dc_legacy",
    "e3dc_efy", "e3dc_auto", "e3dc_multi", "e3dc_multi_connect",
    "e3dc_multi_connect_ii",
))


def published_measurement_valid(status, now_s, max_age_s=STATUS_STALE_DEFAULT_S):
    """Veröffentlichter Ablauf und engere Leserfrist müssen beide gültig sein."""
    observation = status.get("observation") if isinstance(status, dict) else None
    if not isinstance(observation, dict) or "valid_until_ts" not in observation:
        return None  # Bestandsformat ohne veröffentlichten Ablauf.
    deadline = observation.get("valid_until_ts")
    sample = observation.get("sample_ts")
    guard = observation.get("valid_for_s", STATUS_STALE_DEFAULT_S)
    return bool(observation.get("valid") is True
                and type(deadline) in (int, float) and math.isfinite(deadline)
                and type(sample) in (int, float) and math.isfinite(sample) and sample > 0
                and math.isfinite(now_s) and math.isfinite(max_age_s) and max_age_s > 0
                and -2 <= now_s - sample <= max_age_s
                and type(guard) in (int, float) and math.isfinite(guard) and 0 < guard <= 300
                and now_s <= deadline <= sample + guard + 1
                and deadline - now_s <= guard + 2)


def wallbox_type_is_e3dc(value):
    """Gemeinsame Messpunktzuordnung einschließlich der Konfigurationsaliase."""
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_") in E3DC_WALLBOX_TYPES

