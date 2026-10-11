#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemeinsame Leistungsgrenzen ohne Signal-, Datei-, Log- oder Netzwerkwirkung beim Import."""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional


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


def _configured_power_limit_w(cfg: Dict[str, Any], key: str) -> int:
    if key not in cfg:
        return 0
    value = safe_int(cfg.get(key), 0)
    return value if value >= 300 else 0

def _rscp_power_limit_w(live: Dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = abs(safe_int(live.get(key), 0))
        if value >= 300:
            return value
    return 0

def _fresh_rscp_charge_limits_w(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    *,
    now_s: Optional[float] = None,
) -> List[int]:
    """Liefert ausschließlich frische, typisierte RSCP-Ladegrenzen.

    ``EMS_USER_CHARGE_LIMIT`` und ``EMS_BAT_CHARGE_LIMIT`` sind Obergrenzen.
    Sie dürfen einen konfigurierten Sollwert deshalb nur absenken, niemals
    anheben. Bei aktiven ``EMS_POWER_SETTINGS`` kann E3/DC den flüchtigen
    Lade-Cap jedoch auch in ``EMS_USER_CHARGE_LIMIT`` spiegeln. Nur wenn
    frischer USER- und MAX_CHARGE-Readback innerhalb des etablierten
    50-W-Fensters übereinstimmen und eine explizite Konfigurationsgrenze
    existiert, wird dieser rückgekoppelte Wert verworfen. Jede abweichende
    USER-Grenze sowie die typisierte Batteriegrenze bleiben wirksam.
    """

    if (
        live.get("RSCP_Sample_Valid") is not True
        or live.get("Power_Decision_Usable") is not True
    ):
        return []
    raw_ts = live.get("_ts")
    if (
        not isinstance(raw_ts, (int, float))
        or isinstance(raw_ts, bool)
        or not math.isfinite(float(raw_ts))
        or float(raw_ts) <= 0.0
    ):
        return []
    now_value = time.time() if now_s is None else float(now_s)
    max_age_s = max(
        1.0,
        min(30.0, safe_float(cfg.get("storage_live_stale_guard_s"), 10.0)),
    )
    age_s = now_value - float(raw_ts)
    if not math.isfinite(age_s) or age_s < -5.0 or age_s > max_age_s:
        return []

    configured_charge_limit_w = _configured_power_limit_w(
        cfg,
        "maximumladeleistung",
    )
    user_charge_limit_w = live.get("user_charge_limit_w")
    ems_max_charge_power_w = live.get("ems_max_charge_power_w")
    user_limit_reflects_dynamic_cap = bool(
        configured_charge_limit_w >= 300
        and live.get("ems_power_settings_read") is True
        and live.get("ems_power_settings_valid") is True
        and live.get("power_limits_active") is True
        and isinstance(user_charge_limit_w, int)
        and not isinstance(user_charge_limit_w, bool)
        and user_charge_limit_w >= 300
        and isinstance(ems_max_charge_power_w, int)
        and not isinstance(ems_max_charge_power_w, bool)
        and ems_max_charge_power_w >= 0
        and abs(user_charge_limit_w - ems_max_charge_power_w) < 50
    )
    limits: List[int] = []
    for key in ("user_charge_limit_w", "bat_charge_limit_w"):
        if user_limit_reflects_dynamic_cap and key == "user_charge_limit_w":
            continue
        value = live.get(key)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 300
        ):
            limits.append(int(value))
    return limits

def _legacy_configured_charge_limit_w(
    cfg: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    *,
    now_s: Optional[float] = None,
) -> int:
    live = live or {}
    configured_limit_w = _configured_power_limit_w(cfg, "maximumladeleistung")
    fresh_rscp_limits_w = _fresh_rscp_charge_limits_w(
        cfg,
        live,
        now_s=now_s,
    )
    if fresh_rscp_limits_w:
        hardware_limit_w = min(fresh_rscp_limits_w)
        return min(configured_limit_w, hardware_limit_w) if configured_limit_w else hardware_limit_w
    return configured_limit_w or 12000

def _legacy_configured_discharge_limit_w(
    cfg: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    fallback_charge_w: Optional[int] = None,
) -> int:
    live = live or {}
    user_limit_w = _configured_power_limit_w(cfg, "maximaleentladeleistung")
    if user_limit_w:
        return user_limit_w
    rscp_limit_w = _rscp_power_limit_w(
        live,
        "user_discharge_limit_w",
        "bat_discharge_limit_w",
    )
    if rscp_limit_w:
        return rscp_limit_w
    return max(300, int(fallback_charge_w or _legacy_configured_charge_limit_w(cfg, live)))

def effective_power_limit_cfg(cfg: Dict[str, Any], live: Optional[Dict[str, Any]] = None,
                              *, now_s: Optional[float] = None,
                              fallback_charge_w: Optional[int] = None) -> Dict[str, Any]:
    """Eine Grenze je Richtung; eigene dynamische Sollwerte bleiben außerhalb."""
    live = live or {}
    result = dict(cfg or {})
    original = dict(result.get("_power_limit_user_cfg", result))
    now = time.time() if now_s is None else float(now_s)
    diagnostics = {}
    legacy_charge = _legacy_configured_charge_limit_w(original, live, now_s=now)
    for direction, config_key, fallback in (
        ("charge", "maximumladeleistung", legacy_charge),
        ("discharge", "maximaleentladeleistung", None),
    ):
        if fallback is None:
            fallback = _legacy_configured_discharge_limit_w(original, live, fallback_charge_w or result["maximumladeleistung"])
        device_key = "device_max_" + direction + "_power_w"
        device = live.get(device_key)
        observed = live.get(device_key + "_observed_at_s")
        source = live.get(device_key + "_source")
        age = now - observed if type(observed) in (int, float) and math.isfinite(observed) else None
        valid = (live.get(device_key + "_valid") is True and type(device) is int and device >= 300
                 and source == "rscp_sys_specs_" + ("maxChargePower" if direction == "charge" else "maxDischargePower")
                 and age is not None and -5 <= age <= 1800)
        if not valid:
            device = None
            raw_ts = live.get("_ts")
            age = now - raw_ts if type(raw_ts) in (int, float) and math.isfinite(raw_ts) else None
            value = live.get("ems_max_" + direction + "_power_w")
            if (live.get("ems_power_settings_read") is True and live.get("ems_power_settings_valid") is True
                    and live.get("power_limits_active", live.get("ems_power_limits_active")) is False
                    and type(value) is int and value >= 300 and age is not None and -5 <= age <= 30):
                device, source, valid = value, "rscp_get_power_settings_limits_off", True
        configured = _configured_power_limit_w(original, config_key)
        effective = fallback
        selected = "user" if configured else "default"
        hint = "device_limit_unknown" if not valid else ""
        if valid:
            if not configured:
                effective, selected = device, "device_fallback"
                hint = "Keine plausible Eingabe; verwendet werden %d W aus dem Speicher." % device
            elif configured > device:
                effective, selected = min(device, fallback), "device_clamped"
                hint = "Eingetragen sind %d W, der Speicher erlaubt höchstens %d W. Verwendet werden %d W." % (configured, device, effective)
            else:
                effective = min(configured, fallback)
            recommended_key = "device_recommended_min_" + direction + "_power_w"
            recommended = live.get(recommended_key)
            rec_ts = live.get(recommended_key + "_observed_at_s")
            if (live.get(recommended_key + "_valid") is True and type(recommended) is int and recommended > 0
                    and type(rec_ts) in (int, float) and math.isfinite(rec_ts) and -5 <= now - rec_ts <= 1800
                    and configured and configured < recommended):
                hint = (hint + " " if hint else "") + "E3DC empfiehlt mindestens %d W. Die bewusste Drosselung bleibt erhalten." % recommended
        if valid and direction == "charge" and selected == "device_fallback":
            # Eine unabhängige frische Batteriegrenze bleibt eine harte Schranke.
            # Ein eigener USER-Sollwert ist bei fehlender Eingabe kein Gerätemaximum.
            battery_only = dict(live, user_charge_limit_w=None)
            battery_limits = _fresh_rscp_charge_limits_w(original, battery_only, now_s=now)
            if battery_limits:
                effective = min(effective, min(battery_limits))
                hint = "Keine plausible Eingabe; verwendet werden %d W innerhalb der Gerätegrenzen." % effective
        prefix = "power_limit_" + direction
        diagnostics.update({prefix + "_effective_w": effective, prefix + "_source": selected,
                            prefix + "_hint": hint, device_key: device, device_key + "_valid": valid,
                            device_key + "_source": source if valid else "device_limit_unknown",
                            device_key + "_age_s": max(0.0, age) if valid else None})
        result[config_key] = effective
    result["_power_limit_user_cfg"] = {k: v for k, v in original.items() if not k.startswith("_power_limit")}
    result["_power_limit_diagnostics"] = diagnostics
    return result
