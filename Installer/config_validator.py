#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Config plausibility checks against live E3DC/RSCP values.

The validator is intentionally advisory: user inputs keep priority where the
manager already supports them, but strong deviations from E3DC live values are
made visible in the WebUI.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from typing import Any, Dict, Iterable, Optional, Tuple

try:
    from reserve import live_ep_reserve_details
except Exception:  # pragma: no cover - package import fallback
    from .reserve import live_ep_reserve_details  # type: ignore

try:
    from .config_secret_permissions import apply_config_secret_permissions
except Exception:  # pragma: no cover - direct script execution fallback
    from config_secret_permissions import apply_config_secret_permissions  # type: ignore
try:
    from .json_cache import atomic_write_on_change as _atomic_write_on_change
except Exception:  # pragma: no cover - direct script execution fallback
    from json_cache import atomic_write_on_change as _atomic_write_on_change  # type: ignore


RAMDISK = "/var/www/html/ramdisk"
CONFIG_VALIDATION_F = os.path.join(RAMDISK, "config_validation.json")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
            if value == "":
                return float(default)
        result = float(value)
        return result if math.isfinite(result) else float(default)
    except Exception:
        return float(default)


def _has_user_value(cfg: Dict[str, Any], key: str) -> bool:
    if key not in cfg:
        return False
    raw = cfg.get(key)
    return raw is not None and str(raw).strip() != ""


def _is_enabled(cfg: Dict[str, Any], key: str) -> bool:
    return str(cfg.get(key, "0")).strip().lower() in {"1", "true", "yes", "on", "ein"}


def _first_live_value(live: Dict[str, Any], keys: Iterable[str]) -> Tuple[Optional[float], Optional[str]]:
    for key in keys:
        value = safe_float(live.get(key), float("nan"))
        if math.isfinite(value) and abs(value) > 0.0001:
            return value, key
    return None, None


def _power_live_value(live: Dict[str, Any], keys: Iterable[str]) -> Tuple[Optional[float], Optional[str]]:
    dynamic_limit_keys = {
        "user_charge_limit_w",
        "used_charge_limit_w",
        "remaining_charge_w",
        "ems_max_charge_power_w",
        "user_discharge_limit_w",
        "used_discharge_limit_w",
        "remaining_discharge_w",
        "ems_max_discharge_power_w",
    }
    limits_active = str(live.get("power_limits_active", "")).strip().lower() in {"1", "true", "yes", "on"}
    for key in keys:
        if limits_active and key in dynamic_limit_keys:
            continue
        value = safe_float(live.get(key), float("nan"))
        if not math.isfinite(value) or abs(value) <= 0.0001:
            continue
        value = abs(value)
        if value >= 300.0:
            return value, key
    return None, None


def _battery_pack_count(live: Dict[str, Any]) -> int:
    for key in (
        "bat_total_dcb_count",
        "bat_dcb_count",
        "bat_pack_count",
        "battery_pack_count",
        "pack_count",
        "dcb_count",
    ):
        value = int(round(safe_float(live.get(key), 0.0)))
        if value > 1:
            return value
    return 1


def _normalise_capacity_kwh(live: Dict[str, Any], value: float, key: str) -> Tuple[float, str]:
    """Normalise E3DC capacity values that are sometimes reported per pack."""
    pack_count = _battery_pack_count(live)
    if key in {"bat_usable_kwh", "bat_full_cap_kwh", "bat_capacity_kwh"}:
        if pack_count > 1 and 0.1 < value < 5.0:
            return round(value * pack_count, 3), f"{key}*bat_dcb_count"
    return value, key


def _capacity_from_specification(usable_kwh: float, full_kwh: float, specified_kwh: float, key: str) -> Tuple[Optional[float], Optional[str]]:
    if specified_kwh <= 0.1:
        return None, None
    usable = usable_kwh
    if usable <= 0.1:
        usable = full_kwh
    if usable <= 0.1 or usable > specified_kwh * 1.15 or usable < specified_kwh * 0.45:
        return round(specified_kwh * 0.9, 3), f"{key}_specified_kwh*0.9"
    return round(usable, 3), key


def _cabinet_capacity_live_value(live: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    cabinets = []
    for prefix in ("bat", "bat1", "bat2", "bat3"):
        usable = safe_float(live.get(f"{prefix}_usable_kwh"), float("nan"))
        full = safe_float(live.get(f"{prefix}_full_cap_kwh"), float("nan"))
        specified = safe_float(live.get(f"{prefix}_specified_kwh"), float("nan"))
        voltage = safe_float(live.get(f"{prefix}_v"), 0.0)
        active = voltage > 5.0 or (math.isfinite(specified) and specified > 0.1)
        if not active:
            continue
        if not math.isfinite(usable):
            usable = 0.0
        if not math.isfinite(full):
            full = 0.0
        if not math.isfinite(specified):
            specified = 0.0
        value, source = _capacity_from_specification(usable, full, specified, prefix)
        if value is None and usable > 0.1:
            value, source = _normalise_capacity_kwh(live, usable, f"{prefix}_usable_kwh")
        if value is not None and value > 0.1:
            cabinets.append((value, source or f"{prefix}_usable_kwh"))
    if not cabinets:
        return None, None
    total = round(sum(value for value, _source in cabinets), 3)
    sources = "+".join(source for _value, source in cabinets)
    return total, sources


def _capacity_live_value(live: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    for key in (
        "bat_total_usable_kwh",
        "bat_total_full_cap_kwh",
        "bat_total_specified_kwh",
    ):
        value = safe_float(live.get(key), float("nan"))
        if math.isfinite(value) and value > 0.1:
            if key == "bat_total_specified_kwh":
                return round(value * 0.9, 3), f"{key}*0.9"
            return value, key
    cabinet_value, cabinet_key = _cabinet_capacity_live_value(live)
    if cabinet_value is not None:
        return cabinet_value, cabinet_key
    for key in (
        "real_usable_capacity_kwh",
        "usable_capacity_kwh",
        "bat_usable_kwh",
        "bat_capacity_kwh",
        "bat_full_cap_kwh",
    ):
        value = safe_float(live.get(key), float("nan"))
        if math.isfinite(value) and value > 0.1:
            return _normalise_capacity_kwh(live, value, key)
    for key in (
        "real_usable_capacity_wh",
        "usable_capacity_wh",
        "installed_capacity_wh",
        "battery_capacity_wh",
    ):
        value = safe_float(live.get(key), float("nan"))
        if math.isfinite(value) and value > 100.0:
            return value / 1000.0, key
    return None, None


def _deviation(configured: float, live_value: float) -> Tuple[float, float]:
    delta = abs(configured - live_value)
    rel = delta / max(abs(live_value), 1.0)
    return delta, rel


def _entry(
    *,
    key: str,
    label: str,
    unit: str,
    configured: Any,
    live_value: Optional[float],
    live_key: Optional[str],
    effective: Any,
    source: str,
    severity: str,
    message: str,
) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "configured": configured,
        "rscp": live_value,
        "rscp_key": live_key,
        "effective": effective,
        "source": source,
        "severity": severity,
        "message": message,
    }


def _warning_count(*groups: Dict[str, Dict[str, Any]]) -> int:
    return sum(1 for group in groups for item in group.values() if item.get("severity") == "warning")


def validate_storage_config(cfg: Optional[Dict[str, Any]], live: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return advisory storage config validation.

    Source order:
    - charge/discharge/capacity: valid user input -> RSCP live -> project default
    - emergency reserve: E3DC live reserve is the safety floor; configured value
      is only a fallback/additional floor and must not undercut RSCP.
    """

    cfg = {str(k).lower(): v for k, v in (cfg or {}).items()}
    live = live or {}
    now = int(time.time())
    storage: Dict[str, Dict[str, Any]] = {}
    wallbox: Dict[str, Dict[str, Any]] = {}
    consumer: Dict[str, Dict[str, Any]] = {}
    price: Dict[str, Dict[str, Any]] = {}

    power_rules = {
        "maximumladeleistung": {
            "label": "Max. Ladeleistung",
            "default": 12000.0,
            "live_keys": (
                "bat_charge_limit_w",
                "user_charge_limit_w",
            ),
        },
        "maximaleentladeleistung": {
            "label": "Max. Entladeleistung",
            "default": 11000.0,
            "live_keys": (
                "user_discharge_limit_w",
                "bat_discharge_limit_w",
            ),
        },
    }
    for key, rule in power_rules.items():
        configured = safe_float(cfg.get(key), float("nan")) if _has_user_value(cfg, key) else None
        live_value, live_key = _power_live_value(live, rule["live_keys"])
        source = "default"
        effective = float(rule["default"])
        severity = "info"
        message = "Kein RSCP-Wert verfuegbar; Projekt-Default wirkt."
        if configured is not None and math.isfinite(configured) and configured >= 300.0:
            source = "user"
            effective = configured
            severity = "ok"
            message = "Nutzereingabe ist aktiv."
            if live_value is not None:
                delta, rel = _deviation(configured, live_value)
                if delta >= 2000.0 and rel >= 0.30:
                    severity = "warning"
                    message = (
                        "Nutzereingabe weicht deutlich vom E3DC-RSCP-Wert ab. "
                        "Die Eingabe bleibt aktiv; bitte bewusst pruefen."
                    )
        elif live_value is not None:
            source = "rscp"
            effective = live_value
            severity = "ok"
            message = "RSCP-Wert wird als Default/Fallback genutzt."
        storage[key] = _entry(
            key=key,
            label=str(rule["label"]),
            unit="W",
            configured=configured if configured is not None and math.isfinite(configured) else None,
            live_value=live_value,
            live_key=live_key,
            effective=effective,
            source=source,
            severity=severity,
            message=message,
        )

    capacity_cfg = safe_float(cfg.get("speichergroesse"), float("nan")) if _has_user_value(cfg, "speichergroesse") else None
    capacity_live, capacity_live_key = _capacity_live_value(live)
    capacity_effective = 15.0
    capacity_source = "default"
    capacity_severity = "info"
    capacity_message = "Kein RSCP-Kapazitaetswert verfuegbar; Fallback-Wert wirkt."
    if capacity_cfg is not None and math.isfinite(capacity_cfg) and capacity_cfg > 0.1:
        capacity_effective = capacity_cfg
        capacity_source = "user"
        capacity_severity = "ok"
        capacity_message = "Nutzereingabe ist aktiv."
        if capacity_live is not None:
            delta, rel = _deviation(capacity_cfg, capacity_live)
            if delta >= 2.0 and rel >= 0.20:
                capacity_severity = "warning"
                capacity_message = (
                    "Nutzbare Speichergroesse weicht deutlich vom E3DC-Wert ab. "
                    "Die Eingabe bleibt aktiv; bitte Datenblatt/RSCP pruefen."
                )
    elif capacity_live is not None:
        capacity_effective = capacity_live
        capacity_source = "rscp"
        capacity_severity = "ok"
        capacity_message = "RSCP-Wert wird als Default/Fallback genutzt."
    storage["speichergroesse"] = _entry(
        key="speichergroesse",
        label="Speichergroesse",
        unit="kWh",
        configured=capacity_cfg if capacity_cfg is not None and math.isfinite(capacity_cfg) else None,
        live_value=capacity_live,
        live_key=capacity_live_key,
        effective=capacity_effective,
        source=capacity_source,
        severity=capacity_severity,
        message=capacity_message,
    )

    reserve_cfg = safe_float(cfg.get("ep_reserve_pct"), float("nan")) if _has_user_value(cfg, "ep_reserve_pct") else None
    reserve_details = live_ep_reserve_details(cfg, live)
    reserve_live = reserve_details.get("effective_pct")
    reserve_live_key = reserve_details.get("source") or reserve_details.get("raw_key")
    reserve_default = 8.0
    reserve_effective = max(
        0.0,
        reserve_cfg if reserve_cfg is not None and math.isfinite(reserve_cfg) else reserve_default,
        safe_float(reserve_live, 0.0),
    )
    reserve_source = "rscp" if reserve_live is not None else ("user" if reserve_cfg is not None else "default")
    reserve_severity = "ok" if reserve_live is not None else "info"
    reserve_message = "E3DC-Notstromreserve ist Sicherheits-Fuehrung; Fallback greift nur, wenn RSCP fehlt."
    if reserve_details.get("normalised"):
        reserve_message = (
            "E3DC-Notstromreserve wurde aus Wh auf die Gesamt-Speicherkapazitaet normalisiert; "
            "der Prozent-Rohwert bezieht sich nur auf den Reserve-Kreis."
        )
    if reserve_live is None:
        reserve_message = "Kein RSCP-Reservewert verfuegbar; Fallback-Reserve wirkt."
    elif reserve_cfg is not None and math.isfinite(reserve_cfg):
        delta, _rel = _deviation(reserve_cfg, safe_float(reserve_live, 0.0))
        if delta >= 2.0 and not reserve_details.get("normalised"):
            reserve_severity = "warning"
            reserve_message = (
                "Fallback-Reserve weicht vom E3DC-Wert ab. "
                "Die Regelung nutzt sicherheitshalber den hoeheren Wert."
            )
    storage["ep_reserve_pct"] = _entry(
        key="ep_reserve_pct",
        label="Notstromreserve Fallback",
        unit="%",
        configured=reserve_cfg if reserve_cfg is not None and math.isfinite(reserve_cfg) else None,
        live_value=safe_float(reserve_live, float("nan")) if reserve_live is not None else None,
        live_key=reserve_live_key,
        effective=reserve_effective,
        source=reserve_source,
        severity=reserve_severity,
        message=reserve_message,
    )

    grid_amps = safe_float(cfg.get("grid_max_amps"), 63.0)
    grid_severity = "ok"
    grid_message = "Hausanschlusslimit ist plausibel."
    if grid_amps < 16.0 or grid_amps > 100.0:
        grid_severity = "warning"
        grid_message = "Hausanschlusslimit wirkt unplausibel; typische Werte liegen etwa zwischen 16 A und 100 A je Phase."
    wallbox["grid_max_amps"] = _entry(
        key="grid_max_amps",
        label="Hausanschluss",
        unit="A",
        configured=grid_amps if _has_user_value(cfg, "grid_max_amps") else None,
        live_value=None,
        live_key=None,
        effective=grid_amps,
        source="user" if _has_user_value(cfg, "grid_max_amps") else "default",
        severity=grid_severity,
        message=grid_message,
    )

    global_wb_max = safe_float(cfg.get("wbmaxladestrom"), 16.0)
    for key, label, fallback in (
        ("wbmaxladestrom", "Wallbox global max.", 16.0),
        ("wb1_max_amp", "WB1 max. Ladestrom", global_wb_max),
        ("wb2_max_amp", "WB2 max. Ladestrom", global_wb_max),
    ):
        configured = safe_float(cfg.get(key), float("nan")) if _has_user_value(cfg, key) else None
        effective = configured if configured is not None and math.isfinite(configured) else float(fallback)
        severity = "ok"
        message = "Ladestrom-Grenze ist plausibel."
        if effective < 6.0 or effective > 32.0:
            severity = "warning"
            message = "Wallbox-Ladestrom sollte zwischen 6 A und 32 A liegen."
        elif effective > grid_amps:
            severity = "warning"
            message = "Wallbox-Ladestrom liegt ueber der Hausabsicherung je Phase."
        wallbox[key] = _entry(
            key=key,
            label=label,
            unit="A",
            configured=configured if configured is not None and math.isfinite(configured) else None,
            live_value=None,
            live_key=None,
            effective=effective,
            source="user" if configured is not None and math.isfinite(configured) else "fallback",
            severity=severity,
            message=message,
        )

    total_3p_wallbox_amps = max(0.0, wallbox["wb1_max_amp"]["effective"]) + max(0.0, wallbox["wb2_max_amp"]["effective"])
    wallbox_sum_exceeds_grid = total_3p_wallbox_amps > grid_amps
    native_wallbox_active = _is_enabled(cfg, "wb_native_enable")
    wallbox_sum_severity = "warning" if wallbox_sum_exceeds_grid and not native_wallbox_active else "ok"
    if wallbox_sum_exceeds_grid and native_wallbox_active:
        wallbox_sum_message = (
            "Summe der Wallbox-Maximalströme liegt über der Hausabsicherung; "
            "die native Wallbox-Regelung begrenzt den gleichzeitigen Sollstrom dynamisch."
        )
    elif wallbox_sum_exceeds_grid:
        wallbox_sum_message = "Summe der 3-phasigen Wallbox-Maximalströme kann die Hausabsicherung übersteigen."
    else:
        wallbox_sum_message = "Wallbox-Summe liegt innerhalb der Hausabsicherung."
    wallbox["wallbox_grid_sum"] = _entry(
        key="wallbox_grid_sum",
        label="WB-Summe 3p",
        unit="A",
        configured=total_3p_wallbox_amps,
        live_value=None,
        live_key=None,
        effective=total_3p_wallbox_amps,
        source="derived",
        severity=wallbox_sum_severity,
        message=wallbox_sum_message,
    )

    wbminsoc_val = safe_float(cfg.get("wbminsoc"), 70.0)
    if wbminsoc_val < reserve_effective:
        raise ValueError(
            f"Wallbox-Mindest-SoC (wbminsoc={wbminsoc_val}%) darf nicht kleiner als "
            f"die effektive Notstromreserve ({reserve_effective}%) sein."
        )
    wbminsoc_severity = "ok"
    wbminsoc_message = "Wallbox-Mindest-SoC schützt die Hausreserve plausibel."
    if wbminsoc_val < 20.0:
        wbminsoc_severity = "warning"
        wbminsoc_message = (
            "Wallbox-Mindest-SoC ist sehr niedrig; nachts kann der Hausspeicher durch Fahrzeugladung "
            "stärker entladen werden."
        )
    elif wbminsoc_val > 90.0:
        wbminsoc_severity = "warning"
        wbminsoc_message = "Wallbox-Mindest-SoC ist sehr hoch; die Wallbox wird selten freigegeben."
    wallbox["wbminsoc"] = _entry(
        key="wbminsoc",
        label="Wallbox Mindest-SoC",
        unit="%",
        configured=wbminsoc_val if _has_user_value(cfg, "wbminsoc") else None,
        live_value=None,
        live_key=None,
        effective=wbminsoc_val,
        source="user" if _has_user_value(cfg, "wbminsoc") else "default",
        severity=wbminsoc_severity,
        message=wbminsoc_message,
    )

    wb_bat_target_soc_val = safe_float(cfg.get("wb_bat_target_soc"), 90.0)
    wb_bat_target_soc_severity = "ok"
    wb_bat_target_soc_message = "Ziel-SoC am Abend liegt plausibel zur Wallbox-Reserve."
    if wb_bat_target_soc_val < wbminsoc_val:
        wb_bat_target_soc_severity = "warning"
        wb_bat_target_soc_message = "Ziel-SoC am Abend liegt unter dem Wallbox-Mindest-SoC."
    wallbox["wb_bat_target_soc"] = _entry(
        key="wb_bat_target_soc",
        label="Wallbox Ziel-SoC Abend",
        unit="%",
        configured=wb_bat_target_soc_val if _has_user_value(cfg, "wb_bat_target_soc") else None,
        live_value=None,
        live_key=None,
        effective=wb_bat_target_soc_val,
        source="user" if _has_user_value(cfg, "wb_bat_target_soc") else "default",
        severity=wb_bat_target_soc_severity,
        message=wb_bat_target_soc_message,
    )

    openwb_pro_phase_wait_configured = (
        safe_float(cfg.get("openwb_pro_phase_wait_s"), 480.0)
        if _has_user_value(cfg, "openwb_pro_phase_wait_s")
        else None
    )
    openwb_pro_phase_wait_effective = max(
        480.0,
        openwb_pro_phase_wait_configured if openwb_pro_phase_wait_configured is not None else 480.0,
    )
    openwb_pro_phase_wait_severity = "ok"
    openwb_pro_phase_wait_message = "Phasenwechsel-Wartezeit schützt die openWB Pro plausibel."
    if openwb_pro_phase_wait_configured is not None and openwb_pro_phase_wait_configured < 480.0:
        openwb_pro_phase_wait_severity = "warning"
        openwb_pro_phase_wait_message = (
            "Phasenwechsel-Wartezeit ist kleiner als 480s; die Regelung hebt diesen Wert "
            "zum Hardwareschutz automatisch an."
        )
    wallbox["openwb_pro_phase_wait_s"] = _entry(
        key="openwb_pro_phase_wait_s",
        label="openWB Pro Phasenwartezeit",
        unit="s",
        configured=openwb_pro_phase_wait_configured,
        live_value=None,
        live_key=None,
        effective=openwb_pro_phase_wait_effective,
        source="user" if openwb_pro_phase_wait_configured is not None else "default",
        severity=openwb_pro_phase_wait_severity,
        message=openwb_pro_phase_wait_message,
    )

    valid_priority = ["heatpump", "wallbox", "heater"]
    order_raw = str(cfg.get("consumer_priority_order", "heatpump,wallbox,heater")).strip()
    order = [part.strip().lower() for part in order_raw.split(",") if part.strip()]
    active_consumers = []
    if _is_enabled(cfg, "luxtronik") or safe_float(cfg.get("wp_type"), -1.0) >= 0:
        active_consumers.append("heatpump")
    if _is_enabled(cfg, "wb_native_enable"):
        active_consumers.append("wallbox")
    if _is_enabled(cfg, "heizstab") or _has_user_value(cfg, "heizstab_ip") or _has_user_value(cfg, "shelly_heiz_ip"):
        active_consumers.append("heater")
    priority_severity = "ok"
    priority_message = "Verbraucherprioritaet ist plausibel."
    if sorted(order) != sorted(valid_priority) or len(set(order)) != len(order):
        priority_severity = "warning"
        priority_message = "Verbraucherprioritaet muss heatpump, wallbox und heater genau einmal enthalten."
    elif "heatpump" in active_consumers and order[0] != "heatpump":
        priority_severity = "warning"
        priority_message = "Waermepumpe ist aktiv, steht aber nicht an erster Stelle; Nachlauf/Komfort bewusst pruefen."
    consumer["consumer_priority_order"] = _entry(
        key="consumer_priority_order",
        label="Verbraucherprioritaet",
        unit="",
        configured=order_raw,
        live_value=None,
        live_key=None,
        effective=",".join(order),
        source="user" if _has_user_value(cfg, "consumer_priority_order") else "default",
        severity=priority_severity,
        message=priority_message,
    )

    wp_runon = safe_float(cfg.get("consumer_priority_wp_runon_s"), 600.0)
    consumer["consumer_priority_wp_runon_s"] = _entry(
        key="consumer_priority_wp_runon_s",
        label="WP-Nachlaufzeit",
        unit="s",
        configured=wp_runon if _has_user_value(cfg, "consumer_priority_wp_runon_s") else None,
        live_value=None,
        live_key=None,
        effective=wp_runon,
        source="user" if _has_user_value(cfg, "consumer_priority_wp_runon_s") else "default",
        severity="warning" if wp_runon < 60.0 or wp_runon > 3600.0 else "ok",
        message=(
            "WP-Nachlaufzeit wirkt unplausibel; empfohlen sind grob 60 bis 3600 Sekunden."
            if wp_runon < 60.0 or wp_runon > 3600.0 else "WP-Nachlaufzeit ist plausibel."
        ),
    )

    luxtronik_pause_configured = (
        safe_float(cfg.get("luxtronik_pause_setpoint_c"), 20.0)
        if _has_user_value(cfg, "luxtronik_pause_setpoint_c")
        else None
    )
    luxtronik_pause_raw = (
        luxtronik_pause_configured
        if luxtronik_pause_configured is not None
        else 20.0
    )
    luxtronik_pause_effective = max(15.0, min(22.0, luxtronik_pause_raw))
    luxtronik_pause_in_range = 15.0 <= luxtronik_pause_raw <= 22.0
    consumer["luxtronik_pause_setpoint_c"] = _entry(
        key="luxtronik_pause_setpoint_c",
        label="Luxtronik Absenk-Sollwert",
        unit="°C",
        configured=luxtronik_pause_configured,
        live_value=None,
        live_key=None,
        effective=luxtronik_pause_effective,
        source="user" if luxtronik_pause_configured is not None else "default",
        severity="ok" if luxtronik_pause_in_range else "warning",
        message=(
            "Weiche SHI-Sollwertsperre liegt im EMS-Sicherheitsbereich von 15 bis 22 °C."
            if luxtronik_pause_in_range
            else "Wert außerhalb 15 bis 22 °C; die Laufzeitregelung begrenzt ihn auf den Sicherheitsbereich."
        ),
    )

    tariff = str(cfg.get("stromtarif_typ", "static")).strip().lower()
    basis = safe_float(cfg.get("strompreis_basis"), 25.0)
    cheap = safe_float(cfg.get("strompreis_cheap"), 18.0)
    uht = safe_float(cfg.get("strompreis_uht"), 32.0)
    price_limit = safe_float(cfg.get("price_limit"), 20.0)
    price_pause = safe_float(cfg.get("price_pause_limit"), 35.0)
    price_hard = safe_float(cfg.get("price_hard_limit"), -99.0)
    cheap_grid_limit = safe_float(cfg.get("cheap_grid_price_limit_ct"), 0.0)
    cheap_grid_enabled = _is_enabled(cfg, "cheap_grid_boost_enable")
    cheap_grid_supported = tariff in {"tibber", "awattar", "dynamic", "epex", "octopus_heat"}
    market_min_margin = safe_float(
        cfg.get("market_min_margin_pct"),
        safe_float(cfg.get("direct_marketing_min_margin_pct"), 10.0),
    )
    market_safety_correction = safe_float(
        cfg.get("market_safety_correction_ct_per_kwh"),
        safe_float(cfg.get("direct_marketing_safety_margin_ct_per_kwh"), 0.0),
    )
    market_autarky_first = (
        _is_enabled(cfg, "market_autarky_first_enable")
        if _has_user_value(cfg, "market_autarky_first_enable")
        else True
    )
    market_autarky_low_soc = safe_float(cfg.get("market_autarky_low_soc_pct"), 20.0)
    market_autarky_buffer_wh = safe_float(cfg.get("market_autarky_horizon_buffer_wh"), 500.0)
    octopus_bad_order = tariff == "octopus_heat" and not (cheap < basis < uht)
    price["strompreis_cheap"] = _entry(
        key="strompreis_cheap",
        label="Guensigpreis",
        unit="ct/kWh",
        configured=cheap if _has_user_value(cfg, "strompreis_cheap") else None,
        live_value=None,
        live_key=None,
        effective=cheap,
        source="user" if _has_user_value(cfg, "strompreis_cheap") else "default",
        severity="warning" if octopus_bad_order else "ok",
        message=(
            "Octopus-Heat-Preise sollten guenstig < normal < Hochpreis sein."
            if octopus_bad_order else "Preisfeld ist plausibel."
        ),
    )
    price["strompreis_basis"] = _entry(
        key="strompreis_basis",
        label="Normalpreis",
        unit="ct/kWh",
        configured=basis if _has_user_value(cfg, "strompreis_basis") else None,
        live_value=None,
        live_key=None,
        effective=basis,
        source="user" if _has_user_value(cfg, "strompreis_basis") else "default",
        severity="warning" if basis <= 0.0 or octopus_bad_order else "ok",
        message="Normalpreis wirkt unplausibel." if basis <= 0.0 else ("Octopus-Heat-Preise sollten guenstig < normal < Hochpreis sein." if octopus_bad_order else "Preisfeld ist plausibel."),
    )
    price["strompreis_uht"] = _entry(
        key="strompreis_uht",
        label="Hochpreis",
        unit="ct/kWh",
        configured=uht if _has_user_value(cfg, "strompreis_uht") else None,
        live_value=None,
        live_key=None,
        effective=uht,
        source="user" if _has_user_value(cfg, "strompreis_uht") else "default",
        severity="warning" if octopus_bad_order else "ok",
        message=(
            "Octopus-Heat-Preise sollten guenstig < normal < Hochpreis sein."
            if octopus_bad_order else "Preisfeld ist plausibel."
        ),
    )
    price_order_warning = price_limit > price_pause
    for key, label, value in (
        ("price_limit", "Boost-Preislimit", price_limit),
        ("price_pause_limit", "Sperr-Preislimit", price_pause),
        ("cheap_grid_price_limit_ct", "Speicher-Netzladen Preislimit", cheap_grid_limit),
    ):
        enabled = key != "cheap_grid_price_limit_ct" or cheap_grid_enabled
        severity = "ok"
        message = "Preislimit ist plausibel."
        if enabled and value < 0.0:
            severity = "warning"
            message = "Preislimit sollte nicht negativ sein."
        elif key in {"price_limit", "price_pause_limit"} and price_order_warning:
            severity = "warning"
            message = "Boost-Preislimit liegt ueber dem Sperr-Preislimit; Reihenfolge pruefen."
        elif key == "cheap_grid_price_limit_ct" and cheap_grid_enabled and not cheap_grid_supported:
            severity = "warning"
            message = "Preis-Boost ist nur für dynamische EPEX-/Börsentarife und Octopus Heat gedacht."
        elif key == "cheap_grid_price_limit_ct" and enabled and value <= 0.0 and tariff == "octopus_heat":
            message = "Octopus Heat nutzt LT-Fenster automatisch; Preislimit ist optional."
        elif key == "cheap_grid_price_limit_ct" and enabled and value <= 0.0:
            severity = "warning"
            message = "Speicher-Netzladen ist aktiv, aber kein positives Preislimit gesetzt."
        price[key] = _entry(
            key=key,
            label=label,
            unit="ct/kWh",
            configured=value if _has_user_value(cfg, key) else None,
            live_value=None,
            live_key=None,
            effective=value,
            source="user" if _has_user_value(cfg, key) else "default",
            severity=severity,
            message=message,
        )

    for key, label, value, unit, low, high in (
        ("market_min_margin_pct", "Preis-Mindestmarge", market_min_margin, "%", 0.0, None),
        ("market_safety_correction_ct_per_kwh", "Preis-Sicherheitskorrektur", market_safety_correction, "ct/kWh", -10.0, 50.0),
        ("market_autarky_low_soc_pct", "PV-autark Low-SOC-Ausnahme", market_autarky_low_soc, "%", 0.0, 100.0),
        ("market_autarky_horizon_buffer_wh", "PV-autark Horizontpuffer", market_autarky_buffer_wh, "Wh", 0.0, None),
    ):
        configured = cfg.get(key) if _has_user_value(cfg, key) else None
        source = (
            "user"
            if _has_user_value(cfg, key)
            else (
                "legacy_direct_marketing"
                if (
                    key == "market_min_margin_pct"
                    and _has_user_value(cfg, "direct_marketing_min_margin_pct")
                ) or (
                    key == "market_safety_correction_ct_per_kwh"
                    and _has_user_value(cfg, "direct_marketing_safety_margin_ct_per_kwh")
                )
                else "default"
            )
        )
        severity = "ok"
        message = "Wert ist plausibel."
        if low is not None and value < low:
            severity = "warning"
            message = "Wert liegt unter der erlaubten Untergrenze."
        elif high is not None and value > high:
            severity = "warning"
            message = "Wert liegt über der erlaubten Obergrenze."
        elif key == "market_min_margin_pct" and value < 10.0:
            severity = "warning"
            message = "Mindestmarge unter 10 Prozent; preisbasierte Speicherladung wird aggressiver."
        elif key == "market_safety_correction_ct_per_kwh" and value < 0.0:
            message = "Negative Korrektur macht die Preisregelung aggressiver; Mindestmarge bleibt als Schutz aktiv."
        elif key == "market_autarky_low_soc_pct" and value < 15.0:
            severity = "warning"
            message = "Low-SOC-Ausnahme unter 15 Prozent lässt Markt-Netzladen erst sehr spät wieder zu."
        elif key == "market_autarky_horizon_buffer_wh" and value > 5000.0:
            severity = "warning"
            message = "Sehr hoher Autarkie-Puffer kann Netzladen trotz eigentlich ausreichender Tagesprognose wieder erlauben."
        price[key] = _entry(
            key=key,
            label=label,
            unit=unit,
            configured=configured,
            live_value=None,
            live_key=None,
            effective=value,
            source=source,
            severity=severity,
            message=message,
        )

    price["market_autarky_first_enable"] = _entry(
        key="market_autarky_first_enable",
        label="PV-autark zuerst",
        unit="-",
        configured=(
            cfg.get("market_autarky_first_enable")
            if _has_user_value(cfg, "market_autarky_first_enable")
            else None
        ),
        live_value=None,
        live_key=None,
        effective=market_autarky_first,
        source="user" if _has_user_value(cfg, "market_autarky_first_enable") else "default",
        severity="ok" if market_autarky_first else "warning",
        message=(
            "Normales Markt-Netzladen und Speicher-Halten werden bei ausreichender Horizontprognose blockiert."
            if market_autarky_first
            else "PV-autark zuerst ist ausgeschaltet; der normale Marktpfad darf gute Preisfenster aggressiver vorziehen."
        ),
    )

    for key, label, default_enabled in (
        ("market_battery_grid_charge_enable", "Marktpfad Speicher-Netzladen", False),
        ("market_battery_hold_enable", "Marktpfad Speicher-Entladesperre", False),
        ("market_wallbox_enable", "Marktpfad Wallbox", False),
        ("market_heatpump_enable", "Marktpfad Wärmepumpe", False),
        ("market_heater_enable", "Marktpfad Heizstab", False),
    ):
        configured = cfg.get(key) if _has_user_value(cfg, key) else None
        if key == "market_heatpump_enable":
            user_enabled = _is_enabled(cfg, key) if _has_user_value(cfg, key) else False
            price[key] = _entry(
                key=key,
                label=label,
                unit="-",
                configured=configured,
                live_value=None,
                live_key=None,
                effective=False,
                source="legacy_ignored" if _has_user_value(cfg, key) else "default",
                severity="warning" if user_enabled else "ok",
                message=(
                    "Der normale Marktpfad steuert Wärmepumpen nicht mehr. "
                    "Wärmepumpen laufen über PV-/Forecast-Budget, Pre-Dump oder den separaten Negativpreis-Boost."
                    if user_enabled
                    else "Normale Marktpfad-Freigabe für Wärmepumpen ist aus."
                ),
            )
            continue
        if _has_user_value(cfg, key):
            effective = _is_enabled(cfg, key)
            source = "user"
        else:
            effective = bool(default_enabled)
            source = "default"
        price[key] = _entry(
            key=key,
            label=label,
            unit="-",
            configured=configured,
            live_value=None,
            live_key=None,
            effective=effective,
            source=source,
            severity="ok",
            message=(
                "Marktpfad-Freigabe ist aktiv; Speicherpfade sind bewusst separat zu prüfen."
                if effective and key.startswith("market_battery_")
                else ("Marktpfad-Freigabe ist aktiv." if effective else "Marktpfad-Freigabe ist aus.")
            ),
        )
    if _has_user_value(cfg, "market_battery_enable"):
        price["market_battery_enable"] = _entry(
            key="market_battery_enable",
            label="Marktpfad Speicher (Alt-Schalter)",
            unit="-",
            configured=cfg.get("market_battery_enable"),
            live_value=None,
            live_key=None,
            effective=False,
            source="legacy_ignored",
            severity="warning",
            message="Alt-Schalter wird für normales Prognose-Netzladen und Speicher-Halten nicht mehr ausgewertet.",
        )

    heat_policy_runtime = _is_enabled(cfg, "heat_policy_runtime_enable")
    ems_budget_runtime = _is_enabled(cfg, "ems_budget_runtime_enable")
    heater_grid_boost = _is_enabled(cfg, "heat_heater_grid_boost_enable")
    heater_grid_ack = _is_enabled(cfg, "heat_heater_grid_boost_ack")
    heater_requires_deficit = (
        _is_enabled(cfg, "heat_heater_grid_boost_requires_deficit")
        if _has_user_value(cfg, "heat_heater_grid_boost_requires_deficit")
        else True
    )
    heater_price_limit = safe_float(cfg.get("heat_heater_grid_boost_price_limit_ct"), 0.0)
    heater_grid_max_w = safe_float(cfg.get("heat_heater_grid_boost_max_w"), 3000.0)
    heater_min_temp = safe_float(cfg.get("heat_heater_min_temp_c"), 45.0)
    heater_max_temp = safe_float(cfg.get("heat_heater_max_temp_c"), 60.0)
    heat_daily_fallback = safe_float(cfg.get("heat_wp_daily_kwh"), 0.0)

    price["heat_policy_runtime_enable"] = _entry(
        key="heat_policy_runtime_enable",
        label="Central Heat Policy Runtime",
        unit="-",
        configured=cfg.get("heat_policy_runtime_enable") if _has_user_value(cfg, "heat_policy_runtime_enable") else None,
        live_value=None,
        live_key=None,
        effective=heat_policy_runtime,
        source="user" if _has_user_value(cfg, "heat_policy_runtime_enable") else "default",
        severity="warning" if heat_policy_runtime else "ok",
        message=(
            "Aktiv: Zentrale Wärme-Policy darf WP-Starts und Heizstab-Netzboost begrenzen."
            if heat_policy_runtime
            else "Diagnosemodus: Zentrale Wärme-Policy schreibt Status, die bestehende Regelung bleibt führend."
        ),
    )
    price["ems_budget_runtime_enable"] = _entry(
        key="ems_budget_runtime_enable",
        label="EMS-Budget-Runtime",
        unit="-",
        configured=cfg.get("ems_budget_runtime_enable") if _has_user_value(cfg, "ems_budget_runtime_enable") else None,
        live_value=None,
        live_key=None,
        effective=ems_budget_runtime,
        source="user" if _has_user_value(cfg, "ems_budget_runtime_enable") else "default",
        severity="warning" if ems_budget_runtime else "ok",
        message=(
            "Aktiv: Verbraucherbudgets werden nur nach zentralem Budget-Latch und gültigem Ack freigegeben; bei Datenverlust fällt das System auf AUTO-Freilauf zurück."
            if ems_budget_runtime
            else "Diagnosemodus: Budget-Arbitration wird ausgewertet, greift aber nicht in Verbraucherbudgets ein."
        ),
    )
    price["heat_heater_grid_boost_enable"] = _entry(
        key="heat_heater_grid_boost_enable",
        label="Heizstab-Netzboost",
        unit="-",
        configured=cfg.get("heat_heater_grid_boost_enable") if _has_user_value(cfg, "heat_heater_grid_boost_enable") else None,
        live_value=None,
        live_key=None,
        effective=heater_grid_boost,
        source="user" if _has_user_value(cfg, "heat_heater_grid_boost_enable") else "default",
        severity="warning" if heater_grid_boost else "ok",
        message=(
            "Heizstab-Netzboost ist freigegeben; COP=1 nur mit Preislimit, Defizit und Temperaturgrenzen sinnvoll."
            if heater_grid_boost
            else "Heizstab-Netzboost ist aus."
        ),
    )
    price["heat_heater_grid_boost_ack"] = _entry(
        key="heat_heater_grid_boost_ack",
        label="Heizstab COP=1 bestätigt",
        unit="-",
        configured=cfg.get("heat_heater_grid_boost_ack") if _has_user_value(cfg, "heat_heater_grid_boost_ack") else None,
        live_value=None,
        live_key=None,
        effective=heater_grid_ack,
        source="user" if _has_user_value(cfg, "heat_heater_grid_boost_ack") else "default",
        severity="warning" if heater_grid_boost and not heater_grid_ack else "ok",
        message=(
            "COP=1-Warnung nicht bestätigt; die Policy blockiert Heizstab-Netzboost."
            if heater_grid_boost and not heater_grid_ack
            else "COP=1-Warnung bestätigt." if heater_grid_ack else "Keine Bestätigung nötig, solange Heizstab-Netzboost aus ist."
        ),
    )
    price["heat_heater_grid_boost_requires_deficit"] = _entry(
        key="heat_heater_grid_boost_requires_deficit",
        label="Heizstab nur Prognose-Defizit",
        unit="-",
        configured=cfg.get("heat_heater_grid_boost_requires_deficit") if _has_user_value(cfg, "heat_heater_grid_boost_requires_deficit") else None,
        live_value=None,
        live_key=None,
        effective=heater_requires_deficit,
        source="user" if _has_user_value(cfg, "heat_heater_grid_boost_requires_deficit") else "default",
        severity="ok" if heater_requires_deficit else "warning",
        message=(
            "Heizstab-Netzboost wird auf die berechnete Wärme-Defizitdeckung begrenzt."
            if heater_requires_deficit
            else "Defizitbindung ist ausgeschaltet; das ist nur für bewusst freigegebene Sonderfälle sinnvoll."
        ),
    )
    for key, label, value, unit, low, high in (
        ("heat_heater_grid_boost_price_limit_ct", "Heizstab Preislimit", heater_price_limit, "ct/kWh", None, None),
        ("heat_heater_grid_boost_max_w", "Heizstab Netzlimit", heater_grid_max_w, "W", 0.0, None),
        ("heat_heater_min_temp_c", "Heizstab Mindesttemperatur", heater_min_temp, "°C", 0.0, 95.0),
        ("heat_heater_max_temp_c", "Heizstab Maximaltemperatur", heater_max_temp, "°C", 0.0, 95.0),
        ("heat_wp_daily_kwh", "Fallback Wärmebedarf", heat_daily_fallback, "kWh/24h", 0.0, 120.0),
    ):
        severity = "ok"
        message = "Wert ist plausibel."
        if low is not None and value < low:
            severity = "warning"
            message = "Wert liegt unter der erlaubten Untergrenze."
        elif high is not None and value > high:
            severity = "warning"
            message = "Wert liegt über der erwarteten Obergrenze."
        elif key == "heat_heater_grid_boost_price_limit_ct" and heater_grid_boost and value > 0.0:
            severity = "warning"
            message = "Positives Heizstab-Preislimit: COP=1 kann teurer sein als Wärmepumpe oder späterer PV-Betrieb."
        elif key == "heat_heater_grid_boost_max_w" and heater_grid_boost and value <= 0.0:
            severity = "warning"
            message = "Heizstab-Netzboost ist aktiv, aber das Netzlimit ist 0 W."
        elif key == "heat_heater_max_temp_c" and heater_max_temp <= heater_min_temp:
            severity = "warning"
            message = "Maximaltemperatur muss über der Mindesttemperatur liegen."
        price[key] = _entry(
            key=key,
            label=label,
            unit=unit,
            configured=cfg.get(key) if _has_user_value(cfg, key) else None,
            live_value=None,
            live_key=None,
            effective=value,
            source="user" if _has_user_value(cfg, key) else "default",
            severity=severity,
            message=message,
        )

    price["price_hard_limit"] = _entry(
        key="price_hard_limit",
        label="Zwangs-Preislimit",
        unit="ct/kWh",
        configured=price_hard if _has_user_value(cfg, "price_hard_limit") else None,
        live_value=None,
        live_key=None,
        effective=price_hard,
        source="user" if _has_user_value(cfg, "price_hard_limit") else "default",
        severity="warning" if price_hard != -99.0 and price_hard > price_limit else "ok",
        message=(
            "Zwangs-Limit sollte unter oder gleich dem normalen Preislimit liegen; -99 deaktiviert es."
            if price_hard != -99.0 and price_hard > price_limit else "Zwangs-Preislimit ist plausibel oder deaktiviert."
        ),
    )

    dm_enabled = _is_enabled(cfg, "direct_marketing_enable")
    dm_settlement_basis = str(
        cfg.get("direct_marketing_settlement_basis", "day_ahead_15min") or "day_ahead_15min"
    ).strip().lower().replace("-", "_")
    dm_settlement_active_supported = dm_settlement_basis == "day_ahead_15min"
    dm_mode_raw = str(cfg.get("direct_marketing_mode", "safe")).strip().lower().replace("-", "_")
    if dm_mode_raw not in {"safe", "eco", "eco_plus", "arbitrage"}:
        dm_mode = "safe"
        dm_mode_warning = True
    else:
        dm_mode = dm_mode_raw
        dm_mode_warning = False
    dm_profit_profile_raw = str(cfg.get("direct_marketing_profit_profile", "standard")).strip().lower().replace("-", "_")
    if dm_profit_profile_raw not in {"standard", "aggressive", "expert"}:
        dm_profit_profile = "standard"
        dm_profit_profile_warning = True
    else:
        dm_profit_profile = dm_profit_profile_raw
        dm_profit_profile_warning = False
    dm_export_enabled = _is_enabled(cfg, "direct_marketing_export_enable")
    dm_grid_charge_enabled = _is_enabled(cfg, "direct_marketing_grid_charge_enable")
    dm_arbitrage = _is_enabled(cfg, "direct_marketing_arbitrage_enable")
    dm_pv_store_enabled = str(cfg.get("direct_marketing_pv_store_enable", "1")).strip().lower() in {"1", "true", "yes", "on", "ein"}
    dm_min_margin = safe_float(cfg.get("direct_marketing_min_margin_pct"), 10.0)
    dm_min_profit = safe_float(cfg.get("direct_marketing_min_profit_ct_per_kwh"), 0.0)
    dm_min_window_profit_eur = safe_float(cfg.get("direct_marketing_min_window_profit_eur"), 0.10)
    dm_min_export_energy_kwh = safe_float(cfg.get("direct_marketing_min_export_energy_kwh"), 1.0)
    dm_min_export_window_min = safe_float(cfg.get("direct_marketing_min_export_window_min"), 60.0)
    dm_profit_hold = safe_float(cfg.get("direct_marketing_profit_hold_ct_per_kwh"), 0.5)
    dm_margin_hold = safe_float(cfg.get("direct_marketing_margin_hold_pct"), 5.0)
    dm_degradation = safe_float(cfg.get("direct_marketing_degradation_ct_per_kwh"), 4.0)
    dm_efficiency = safe_float(cfg.get("direct_marketing_roundtrip_efficiency_pct"), 85.0)
    dm_fee_ct = safe_float(cfg.get("direct_marketing_fee_ct_per_kwh"), 0.0)
    dm_fee_pct = safe_float(cfg.get("direct_marketing_fee_pct"), 0.0)
    dm_revenue_offset = safe_float(cfg.get("direct_marketing_revenue_offset_ct"), 0.0)
    dm_safety_margin = safe_float(cfg.get("direct_marketing_safety_margin_ct_per_kwh"), 0.0)
    dm_max_export_w = safe_float(cfg.get("direct_marketing_max_export_w"), 0.0)
    dm_min_grid_export_w = safe_float(cfg.get("direct_marketing_min_grid_export_w"), 100.0)
    dm_max_grid_charge_w = safe_float(cfg.get("direct_marketing_max_grid_charge_w"), 0.0)
    dm_pv_store_threshold = safe_float(cfg.get("direct_marketing_pv_store_threshold_ct"), 0.0)
    dm_pv_store_max_w = safe_float(cfg.get("direct_marketing_pv_store_max_w"), 0.0)
    dm_pv_store_min_surplus_w = safe_float(cfg.get("direct_marketing_pv_store_min_surplus_w"), 300.0)
    dm_pv_store_import_guard_w = safe_float(cfg.get("direct_marketing_pv_store_import_guard_w"), 80.0)
    dm_pv_store_min_hold_s = safe_float(cfg.get("direct_marketing_pv_store_min_hold_s"), 600.0)
    dm_pv_store_ramp_step_w = safe_float(cfg.get("direct_marketing_pv_store_ramp_step_w"), 300.0)
    dm_pv_store_dc_only = _is_enabled(cfg, "direct_marketing_pv_store_dc_only_enable")
    dm_pv_store_external_ac_guard_w = safe_float(cfg.get("direct_marketing_pv_store_external_ac_guard_w"), 100.0)
    dm_pv_store_export_limit_guard_w = safe_float(cfg.get("direct_marketing_pv_store_export_limit_guard_w"), 100.0)
    dm_pv_store_export_limit_ramp_bypass_w = safe_float(cfg.get("direct_marketing_pv_store_export_limit_ramp_bypass_w"), 300.0)
    dm_price_max_age_s = safe_float(cfg.get("direct_marketing_price_max_age_s"), 0.0)
    dm_cycles = safe_float(cfg.get("direct_marketing_max_cycles_per_day"), 1.0)
    dm_headroom = safe_float(cfg.get("direct_marketing_keep_headroom_pct"), 20.0)
    dm_negative_headroom_enabled = _is_enabled(cfg, "direct_marketing_negative_headroom_enable")
    dm_negative_headroom_lookahead_min = safe_float(cfg.get("direct_marketing_negative_headroom_lookahead_min"), 240.0)
    dm_negative_headroom_min_window_min = safe_float(cfg.get("direct_marketing_negative_headroom_min_window_min"), 30.0)
    dm_negative_headroom_min_surplus_wh = safe_float(cfg.get("direct_marketing_negative_headroom_min_surplus_wh"), 1000.0)
    dm_negative_headroom_buffer_pct = safe_float(cfg.get("direct_marketing_negative_headroom_buffer_pct"), 3.0)
    dm_low_price_headroom_enabled = _is_enabled(cfg, "direct_marketing_low_price_headroom_enable")
    dm_curtail_enabled = _is_enabled(cfg, "direct_marketing_low_price_curtail_enable")
    dm_curtail_limit_w = safe_float(cfg.get("direct_marketing_low_price_curtail_limit_w"), 0.0)
    dm_market_value_solar_enabled = _is_enabled(cfg, "direct_marketing_market_value_solar_enable")
    dm_market_value_solar_source = str(cfg.get("direct_marketing_market_value_solar_source", "netztransparenz_hochrechnung_solar") or "netztransparenz_hochrechnung_solar").strip()
    nt_client_id = str(cfg.get("netztransparenz_client_id", "") or "").strip()
    nt_client_secret = str(cfg.get("netztransparenz_client_secret", "") or "").strip()
    dm_aux_inverter_shelly_override_raw = str(cfg.get("direct_marketing_aux_inverter_shelly_override", "0") or "0").strip().lower()
    dm_aux_inverter_shelly_override = dm_aux_inverter_shelly_override_raw in {"1", "true", "yes", "on", "ein", "central", "zentral"}
    dm_aux_inverter_shelly_override_known = dm_aux_inverter_shelly_override_raw in {
        "0",
        "false",
        "no",
        "off",
        "aus",
        "local",
        "lokal",
        "1",
        "true",
        "yes",
        "on",
        "ein",
        "central",
        "zentral",
    }
    dm_aux_inverter_shelly_ip = str(cfg.get("direct_marketing_aux_inverter_shelly_ip", "") or "").strip()
    dm_aux_inverter_shelly_ip_valid = bool(
        dm_aux_inverter_shelly_ip
        and re.match(r"^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$", dm_aux_inverter_shelly_ip)
    )
    dm_aux_inverter_shelly_invert = _is_enabled(cfg, "direct_marketing_aux_inverter_shelly_invert")
    dm_aux_inverter_shelly_dynamic_unblock = str(
        cfg.get("direct_marketing_aux_inverter_shelly_dynamic_unblock_enable", "0") or "0"
    ).strip().lower() in {"1", "true", "yes", "on", "ein"}
    dm_aux_inverter_shelly_unblock_threshold_w = safe_float(
        cfg.get("direct_marketing_aux_inverter_shelly_unblock_threshold_w"),
        3000.0,
    )
    dm_negative_no_export = _is_enabled(cfg, "direct_marketing_negative_price_no_export")
    dm_low_no_export = _is_enabled(cfg, "direct_marketing_low_price_no_export")
    dm_eeg_enabled = _is_enabled(cfg, "direct_marketing_eeg_enable")
    dm_eeg_commissioning = str(cfg.get("direct_marketing_eeg_commissioning_date", "") or "").strip()
    dm_eeg_support_years = safe_float(cfg.get("direct_marketing_eeg_support_years"), 20.0)
    dm_eeg_tiers = str(cfg.get("direct_marketing_eeg_tariff_tiers", "") or "").strip()
    dm_eeg_rate_source = str(cfg.get("direct_marketing_eeg_rate_source", "manual") or "manual").strip()
    dm_eeg_system_type = str(cfg.get("direct_marketing_eeg_system_type", "building") or "building").strip()
    dm_eeg_feed_type = str(cfg.get("direct_marketing_eeg_feed_type", "partial") or "partial").strip()
    dm_eeg_compensation_basis = str(cfg.get("direct_marketing_eeg_compensation_basis", "feed_in_tariff") or "feed_in_tariff").strip()
    dm_eeg_grid_export_ack = _is_enabled(cfg, "direct_marketing_eeg_grid_export_risk_ack")

    price["direct_marketing_enable"] = _entry(
        key="direct_marketing_enable",
        label="Direktvermarktung",
        unit="-",
        configured=cfg.get("direct_marketing_enable") if _has_user_value(cfg, "direct_marketing_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_enable") else "default",
        severity="ok",
        message="Direktvermarktung ist aktiv." if dm_enabled else "Direktvermarktung ist hart aus und beeinflusst die Regelung nicht.",
    )
    price["direct_marketing_mode"] = _entry(
        key="direct_marketing_mode",
        label="Direktvermarktungsmodus",
        unit="-",
        configured=cfg.get("direct_marketing_mode") if _has_user_value(cfg, "direct_marketing_mode") else None,
        live_value=None,
        live_key=None,
        effective=dm_mode,
        source="user" if _has_user_value(cfg, "direct_marketing_mode") else "default",
        severity="warning" if dm_enabled and dm_mode_warning else "ok",
        message="Unbekannter Modus; Safe wird als konservativer Fallback genutzt." if dm_enabled and dm_mode_warning else "Direktvermarktungsmodus ist plausibel.",
    )
    dm_profit_profile_severity = "ok"
    dm_profit_profile_message = "Standardprofil berücksichtigt Wirkungsgrad, LCOS und alle Mindestgates."
    if dm_profit_profile_warning:
        dm_profit_profile_severity = "warning"
        dm_profit_profile_message = "Unbekanntes Profitprofil; Standard wird als wirtschaftlicher Fallback genutzt."
    elif dm_profit_profile == "aggressive":
        dm_profit_profile_severity = "warning" if dm_enabled and dm_export_enabled else "ok"
        dm_profit_profile_message = "Aggressiv erlaubt positive Kleinstgewinne nach Wirkungsgrad und kann zusätzliche Batteriezyklen verursachen."
    elif dm_profit_profile == "expert":
        dm_profit_profile_severity = "warning" if dm_enabled else "ok"
        dm_profit_profile_message = "Experte umgeht Wirtschaftlichkeitsgates; Notstrom-, Reserve- und Datenqualitätsgrenzen bleiben hart."
    price["direct_marketing_profit_profile"] = _entry(
        key="direct_marketing_profit_profile",
        label="DV-Profitprofil",
        unit="-",
        configured=cfg.get("direct_marketing_profit_profile") if _has_user_value(cfg, "direct_marketing_profit_profile") else None,
        live_value=None,
        live_key=None,
        effective=dm_profit_profile,
        source="user" if _has_user_value(cfg, "direct_marketing_profit_profile") else "default",
        severity=dm_profit_profile_severity,
        message=dm_profit_profile_message,
    )
    price["direct_marketing_settlement_basis"] = _entry(
        key="direct_marketing_settlement_basis",
        label="DV-Abrechnungsbasis",
        unit="-",
        configured=(
            cfg.get("direct_marketing_settlement_basis")
            if _has_user_value(cfg, "direct_marketing_settlement_basis")
            else None
        ),
        live_value=None,
        live_key=None,
        effective=dm_settlement_basis,
        source="user" if _has_user_value(cfg, "direct_marketing_settlement_basis") else "default",
        severity="warning" if dm_enabled and not dm_settlement_active_supported else "ok",
        message=(
            "Aktive DV-Regelung ist derzeit nur mit Day-Ahead-Viertelstundenpreisen freigegeben. "
            "Die gewählte Basis bleibt reine Analyse und erzeugt keine Steuerbefehle."
            if dm_enabled and not dm_settlement_active_supported
            else "Day-Ahead-Viertelstundenpreise sind für die aktive DV-Regelung freigegeben."
        ),
    )
    price["direct_marketing_pv_store_enable"] = _entry(
        key="direct_marketing_pv_store_enable",
        label="DV-PV-Speichern",
        unit="-",
        configured=cfg.get("direct_marketing_pv_store_enable") if _has_user_value(cfg, "direct_marketing_pv_store_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_pv_store_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_pv_store_enable") else "default",
        severity="ok",
        message=(
            "Eco+ darf PV-Überschuss in niedrigen Direktvermarktungsfenstern speichern; Netzladen und Batterieeinspeisung bleiben separat gesperrt."
            if dm_pv_store_enabled else
            "PV-Speichern im Direktvermarktungszweig ist aus; es gibt keinen aktiven PV-Ladeowner."
        ),
    )
    price["direct_marketing_eeg_enable"] = _entry(
        key="direct_marketing_eeg_enable",
        label="DV-EEG-Anlage",
        unit="-",
        configured=cfg.get("direct_marketing_eeg_enable") if _has_user_value(cfg, "direct_marketing_eeg_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_enable") else "default",
        severity="ok",
        message="EEG-/Marktpraemienbewertung ist aktiv." if dm_eeg_enabled else "EEG-/Marktpraemienbewertung ist aus.",
    )
    eeg_rate_sources = {"manual", "bnetza_archive", "bnetza_current_2026_02"}
    eeg_auto_source = dm_eeg_rate_source in {"bnetza_archive", "bnetza_current_2026_02"}
    eeg_source_invalid = dm_eeg_rate_source not in eeg_rate_sources
    eeg_auto_date_outside = bool(
        dm_eeg_rate_source == "bnetza_current_2026_02"
        and re.match(r"^\d{4}-\d{2}-\d{2}$", dm_eeg_commissioning)
        and (dm_eeg_commissioning < "2026-02-01" or dm_eeg_commissioning > "2026-07-31")
    )
    eeg_archive_date_missing = bool(dm_eeg_rate_source == "bnetza_archive" and not dm_eeg_commissioning)
    eeg_archive_date_outside = bool(
        dm_eeg_rate_source == "bnetza_archive"
        and re.match(r"^\d{4}-\d{2}-\d{2}$", dm_eeg_commissioning)
        and (dm_eeg_commissioning < "2018-01-01" or dm_eeg_commissioning > "2026-07-31")
    )
    price["direct_marketing_eeg_rate_source"] = _entry(
        key="direct_marketing_eeg_rate_source",
        label="DV-EEG-Verguetungsquelle",
        unit="-",
        configured=dm_eeg_rate_source if _has_user_value(cfg, "direct_marketing_eeg_rate_source") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_rate_source,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_rate_source") else "default",
        severity="warning" if dm_eeg_enabled and (eeg_source_invalid or eeg_auto_date_outside or eeg_archive_date_missing or eeg_archive_date_outside) else "ok",
        message=(
            "Unbekannte EEG-Verguetungsquelle; bitte manuelle Stufen verwenden."
            if eeg_source_invalid
            else (
                "Aktuelle BNetzA-Tabelle gilt nur fuer Inbetriebnahmen vom 2026-02-01 bis 2026-07-31."
                if eeg_auto_date_outside
                else (
                    "BNetzA-Archiv automatisch braucht ein Inbetriebnahmedatum."
                    if eeg_archive_date_missing
                    else (
                        "Eingebettetes BNetzA-Archiv deckt aktuell nur 2018-01-01 bis 2026-07-31 ab."
                        if eeg_archive_date_outside
                        else "EEG-Verguetungsquelle ist plausibel."
                    )
                )
            )
        ),
    )
    price["direct_marketing_eeg_system_type"] = _entry(
        key="direct_marketing_eeg_system_type",
        label="DV-EEG-Anlagenart",
        unit="-",
        configured=dm_eeg_system_type if _has_user_value(cfg, "direct_marketing_eeg_system_type") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_system_type,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_system_type") else "default",
        severity="warning" if dm_eeg_enabled and dm_eeg_system_type not in {"building", "other"} else "ok",
        message="Anlagenart ist plausibel." if dm_eeg_system_type in {"building", "other"} else "Unbekannte Anlagenart.",
    )
    price["direct_marketing_eeg_feed_type"] = _entry(
        key="direct_marketing_eeg_feed_type",
        label="DV-EEG-Einspeiseart",
        unit="-",
        configured=dm_eeg_feed_type if _has_user_value(cfg, "direct_marketing_eeg_feed_type") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_feed_type,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_feed_type") else "default",
        severity="warning" if dm_eeg_enabled and dm_eeg_feed_type not in {"partial", "full"} else "ok",
        message="Einspeiseart ist plausibel." if dm_eeg_feed_type in {"partial", "full"} else "Unbekannte Einspeiseart.",
    )
    price["direct_marketing_eeg_compensation_basis"] = _entry(
        key="direct_marketing_eeg_compensation_basis",
        label="DV-EEG-Rechengrundlage",
        unit="-",
        configured=dm_eeg_compensation_basis if _has_user_value(cfg, "direct_marketing_eeg_compensation_basis") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_compensation_basis,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_compensation_basis") else "default",
        severity="warning" if dm_eeg_enabled and dm_eeg_compensation_basis not in {"feed_in_tariff", "market_premium"} else "ok",
        message=(
            "Rechengrundlage ist plausibel."
            if dm_eeg_compensation_basis in {"feed_in_tariff", "market_premium"}
            else "Unbekannte Rechengrundlage."
        ),
    )
    eeg_date_invalid = bool(dm_eeg_commissioning and not re.match(r"^\d{4}-\d{2}-\d{2}$", dm_eeg_commissioning))
    price["direct_marketing_eeg_commissioning_date"] = _entry(
        key="direct_marketing_eeg_commissioning_date",
        label="DV-EEG-Inbetriebnahme",
        unit="-",
        configured=dm_eeg_commissioning if _has_user_value(cfg, "direct_marketing_eeg_commissioning_date") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_commissioning,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_commissioning_date") else "default",
        severity="warning" if dm_eeg_enabled and eeg_date_invalid else "ok",
        message="Datum bitte als YYYY-MM-DD hinterlegen." if dm_eeg_enabled and eeg_date_invalid else "Inbetriebnahmedatum ist plausibel oder leer.",
    )
    price["direct_marketing_eeg_tariff_tiers"] = _entry(
        key="direct_marketing_eeg_tariff_tiers",
        label="DV-EEG-Verguetungsstufen",
        unit="ct/kWh",
        configured=dm_eeg_tiers if _has_user_value(cfg, "direct_marketing_eeg_tariff_tiers") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_tiers,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_tariff_tiers") else "default",
        severity="warning" if dm_eeg_enabled and not eeg_auto_source and not dm_eeg_tiers else "ok",
        message=(
            "EEG-Anlage ist aktiv, aber es sind keine Verguetungsstufen hinterlegt."
            if dm_eeg_enabled and not eeg_auto_source and not dm_eeg_tiers else "Verguetungsstufen sind hinterlegt, automatisch ableitbar oder EEG ist aus."
        ),
    )
    eeg_grid_export_risk = bool(dm_enabled and dm_eeg_enabled and dm_grid_charge_enabled and dm_export_enabled)
    price["direct_marketing_eeg_grid_export_risk_ack"] = _entry(
        key="direct_marketing_eeg_grid_export_risk_ack",
        label="DV-EEG-Netzstrom-Risiko",
        unit="-",
        configured=cfg.get("direct_marketing_eeg_grid_export_risk_ack") if _has_user_value(cfg, "direct_marketing_eeg_grid_export_risk_ack") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_grid_export_ack,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_grid_export_risk_ack") else "default",
        severity="warning" if eeg_grid_export_risk and not dm_eeg_grid_export_ack else "ok",
        message=(
            "EEG-Anlage mit Netzladen und Einspeisung braucht bestaetigtes Mess-/Vertragskonzept; sonst bleiben Arbitrage-Befehle blockiert."
            if eeg_grid_export_risk and not dm_eeg_grid_export_ack
            else "Kein unbestaetigter EEG-Netzstrom-Export-Risikobetrieb."
        ),
    )

    threshold_contract_warning = bool(
        dm_enabled
        and dm_pv_store_enabled
        and dm_eeg_enabled
        and dm_pv_store_threshold <= 0.0
        and not eeg_auto_source
        and not dm_eeg_tiers
    )
    price["direct_marketing_pv_store_contract_threshold"] = _entry(
        key="direct_marketing_pv_store_contract_threshold",
        label="DV-PV-Speicher-Vertragsschwelle",
        unit="-",
        configured=None,
        live_value=None,
        live_key=None,
        effective=not threshold_contract_warning,
        source="derived",
        severity="warning" if threshold_contract_warning else "ok",
        message=(
            "PV-Speichern nach EEG-/Direktvermarktungsschwelle braucht eine manuelle Schwelle oder gültige Vergütungsstufen; sonst bleibt nur die Preis-Score-Logik."
            if threshold_contract_warning else
            "Schwelle ist manuell, automatisch ableitbar oder PV-Speichern nutzt bewusst die Score-Logik."
        ),
    )
    price["direct_marketing_pv_store_dc_only_enable"] = _entry(
        key="direct_marketing_pv_store_dc_only_enable",
        label="DV-PV-Speichern nur E3DC-DC",
        unit="-",
        configured=cfg.get("direct_marketing_pv_store_dc_only_enable") if _has_user_value(cfg, "direct_marketing_pv_store_dc_only_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_pv_store_dc_only,
        source="user" if _has_user_value(cfg, "direct_marketing_pv_store_dc_only_enable") else "default",
        severity="ok",
        message=(
            "Externe AC-Zusatzwechselrichter werden beim DV-PV-Speichern nicht als Batterie-Ladequelle genutzt."
            if dm_pv_store_dc_only else
            "Externe AC-PV darf den allgemeinen PV-Überschuss für DV-PV-Speichern mitbestimmen."
        ),
    )
    market_value_source_ok = dm_market_value_solar_source in {"netztransparenz_hochrechnung_solar"}
    price["direct_marketing_market_value_solar_enable"] = _entry(
        key="direct_marketing_market_value_solar_enable",
        label="DV-Marktwert Solar Monitor",
        unit="-",
        configured=cfg.get("direct_marketing_market_value_solar_enable") if _has_user_value(cfg, "direct_marketing_market_value_solar_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_market_value_solar_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_market_value_solar_enable") else "default",
        severity="warning" if dm_market_value_solar_enabled and (not nt_client_id or not nt_client_secret or not market_value_source_ok) else "ok",
        message=(
            "Marktwert-Solar-Monitor ist aktiv, aber Netztransparenz-Zugangsdaten oder Quelle sind unvollständig."
            if dm_market_value_solar_enabled and (not nt_client_id or not nt_client_secret or not market_value_source_ok)
            else (
                "Marktwert-Solar-Monitor ist aktiv und bleibt read-only ohne Regelwirkung."
                if dm_market_value_solar_enabled else
                "Marktwert-Solar-Monitor ist aus."
            )
        ),
    )
    price["direct_marketing_market_value_solar_source"] = _entry(
        key="direct_marketing_market_value_solar_source",
        label="DV-Marktwert Solar Quelle",
        unit="-",
        configured=dm_market_value_solar_source if _has_user_value(cfg, "direct_marketing_market_value_solar_source") else None,
        live_value=None,
        live_key=None,
        effective=dm_market_value_solar_source,
        source="user" if _has_user_value(cfg, "direct_marketing_market_value_solar_source") else "default",
        severity="warning" if dm_market_value_solar_enabled and not market_value_source_ok else "ok",
        message="Quelle ist plausibel." if market_value_source_ok else "Unbekannte Marktwert-Solar-Quelle.",
    )
    price["netztransparenz_client_id"] = _entry(
        key="netztransparenz_client_id",
        label="Netztransparenz Client-ID",
        unit="-",
        configured="gesetzt" if _has_user_value(cfg, "netztransparenz_client_id") and nt_client_id else None,
        live_value=None,
        live_key=None,
        effective=bool(nt_client_id),
        source="user" if _has_user_value(cfg, "netztransparenz_client_id") else "default",
        severity="warning" if dm_market_value_solar_enabled and not nt_client_id else "ok",
        message="Client-ID ist gesetzt." if nt_client_id else "Client-ID fehlt für den Marktwert-Solar-Monitor.",
    )
    price["netztransparenz_client_secret"] = _entry(
        key="netztransparenz_client_secret",
        label="Netztransparenz Client-Secret",
        unit="-",
        configured="***" if _has_user_value(cfg, "netztransparenz_client_secret") and nt_client_secret else None,
        live_value=None,
        live_key=None,
        effective=bool(nt_client_secret),
        source="user" if _has_user_value(cfg, "netztransparenz_client_secret") else "default",
        severity="warning" if dm_market_value_solar_enabled and not nt_client_secret else "ok",
        message="Client-Secret ist gesetzt und wird nicht ausgegeben." if nt_client_secret else "Client-Secret fehlt für den Marktwert-Solar-Monitor.",
    )
    price["direct_marketing_aux_inverter_shelly_override"] = _entry(
        key="direct_marketing_aux_inverter_shelly_override",
        label="DV-Zusatz-WR Shelly-Steuerung",
        unit="-",
        configured=cfg.get("direct_marketing_aux_inverter_shelly_override") if _has_user_value(cfg, "direct_marketing_aux_inverter_shelly_override") else None,
        live_value=None,
        live_key=None,
        effective="central" if dm_aux_inverter_shelly_override else "local",
        source="user" if _has_user_value(cfg, "direct_marketing_aux_inverter_shelly_override") else "default",
        severity="warning" if dm_enabled and not dm_aux_inverter_shelly_override_known else "ok",
        message=(
            "Unbekannter Wert; lokales Shelly-Skript bleibt als sicherer Fallback maßgeblich."
            if dm_enabled and not dm_aux_inverter_shelly_override_known
            else (
                "E3DC-Control übernimmt die Shelly-Relaissteuerung für den ungeregelten Zusatzwechselrichter."
                if dm_aux_inverter_shelly_override
                else "Shelly-Zusatzwechselrichter bleibt lokal/fallback-gesteuert; E3DC-Control sendet keine Relaisbefehle."
            )
        ),
    )
    price["direct_marketing_aux_inverter_shelly_ip"] = _entry(
        key="direct_marketing_aux_inverter_shelly_ip",
        label="DV-Zusatz-WR Shelly-IP",
        unit="-",
        configured=dm_aux_inverter_shelly_ip if _has_user_value(cfg, "direct_marketing_aux_inverter_shelly_ip") else None,
        live_value=None,
        live_key=None,
        effective=dm_aux_inverter_shelly_ip,
        source="user" if _has_user_value(cfg, "direct_marketing_aux_inverter_shelly_ip") else "default",
        severity="warning" if dm_aux_inverter_shelly_override and not dm_aux_inverter_shelly_ip_valid else "ok",
        message=(
            "Zentrale Shelly-Steuerung ist aktiv, aber es ist keine gültige IPv4-Adresse hinterlegt."
            if dm_aux_inverter_shelly_override and not dm_aux_inverter_shelly_ip_valid
            else (
                "Shelly-IP ist plausibel."
                if dm_aux_inverter_shelly_ip
                else "Keine Shelly-IP hinterlegt; ohne zentrale Freigabe ist das in Ordnung."
            )
        ),
    )
    price["direct_marketing_aux_inverter_shelly_invert"] = _entry(
        key="direct_marketing_aux_inverter_shelly_invert",
        label="DV-Zusatz-WR Shelly-Schütz invertiert",
        unit="-",
        configured=cfg.get("direct_marketing_aux_inverter_shelly_invert") if _has_user_value(cfg, "direct_marketing_aux_inverter_shelly_invert") else None,
        live_value=None,
        live_key=None,
        effective=bool(dm_aux_inverter_shelly_invert),
        source="user" if _has_user_value(cfg, "direct_marketing_aux_inverter_shelly_invert") else "default",
        severity="warning" if dm_aux_inverter_shelly_override and dm_aux_inverter_shelly_invert else "ok",
        message=(
            "Invertiert aktiv: Shelly-Relais EIN bedeutet Zusatzwechselrichter AUS (NC-Schütz/stromlos geschlossen)."
            if dm_aux_inverter_shelly_invert
            else "Normale Logik: Shelly-Relais EIN bedeutet Zusatzwechselrichter EIN."
        ),
    )
    price["direct_marketing_aux_inverter_shelly_dynamic_unblock_enable"] = _entry(
        key="direct_marketing_aux_inverter_shelly_dynamic_unblock_enable",
        label="DV-Zusatz-WR dynamische Last-Freigabe",
        unit="-",
        configured=cfg.get("direct_marketing_aux_inverter_shelly_dynamic_unblock_enable") if _has_user_value(cfg, "direct_marketing_aux_inverter_shelly_dynamic_unblock_enable") else None,
        live_value=None,
        live_key=None,
        effective=bool(dm_aux_inverter_shelly_dynamic_unblock),
        source="user" if _has_user_value(cfg, "direct_marketing_aux_inverter_shelly_dynamic_unblock_enable") else "default",
        severity="warning" if dm_aux_inverter_shelly_override and dm_aux_inverter_shelly_dynamic_unblock else "ok",
        message=(
            "Dynamische Freigabe ist bewusst aktiviert. Sie nutzt gültige Netz-/Lastwerte und eine 600-s-Schaltsperre; der gewählte Lastwert garantiert allein keinen exportfreien Betrieb."
            if dm_aux_inverter_shelly_dynamic_unblock
            else "Sicherer Standard: Bei Negativpreis bleibt der Zusatzwechselrichter statisch gesperrt."
        ),
    )
    price["direct_marketing_aux_inverter_shelly_unblock_threshold_w"] = _entry(
        key="direct_marketing_aux_inverter_shelly_unblock_threshold_w",
        label="DV-Zusatz-WR Last-Freigabeschwelle",
        unit="W",
        configured=cfg.get("direct_marketing_aux_inverter_shelly_unblock_threshold_w") if _has_user_value(cfg, "direct_marketing_aux_inverter_shelly_unblock_threshold_w") else None,
        live_value=None,
        live_key=None,
        effective=dm_aux_inverter_shelly_unblock_threshold_w,
        source="user" if _has_user_value(cfg, "direct_marketing_aux_inverter_shelly_unblock_threshold_w") else "default",
        severity="warning" if dm_aux_inverter_shelly_dynamic_unblock and not (100.0 <= dm_aux_inverter_shelly_unblock_threshold_w <= 100000.0) else "ok",
        message=(
            "Last-Freigabeschwelle muss zwischen 100 W und 100.000 W liegen; wirksam werden mindestens 100 W angesetzt."
            if dm_aux_inverter_shelly_dynamic_unblock and not (100.0 <= dm_aux_inverter_shelly_unblock_threshold_w <= 100000.0)
            else "Last-Freigabeschwelle ist plausibel; sie muss zur Leistung des Zusatzwechselrichters und der schaltbaren Last passen."
        ),
    )

    dm_numeric_checks = (
        ("direct_marketing_fee_ct_per_kwh", "DV-Gebuehr fix", dm_fee_ct, "ct/kWh", 0.0, None),
        ("direct_marketing_fee_pct", "DV-Gebuehr variabel", dm_fee_pct, "%", 0.0, 100.0),
        ("direct_marketing_revenue_offset_ct", "DV-Erloes-Korrektur", dm_revenue_offset, "ct/kWh", None, None),
        ("direct_marketing_min_margin_pct", "DV-Mindestmarge", dm_min_margin, "%", 0.0, None),
        ("direct_marketing_min_profit_ct_per_kwh", "DV-Mindestgewinn", dm_min_profit, "ct/kWh", 0.0, None),
        ("direct_marketing_min_window_profit_eur", "DV-Mindestfenstergewinn", dm_min_window_profit_eur, "EUR", 0.0, None),
        ("direct_marketing_min_export_energy_kwh", "DV-Mindestexportenergie", dm_min_export_energy_kwh, "kWh", 0.0, None),
        ("direct_marketing_min_export_window_min", "DV-Mindestexportdauer", dm_min_export_window_min, "min", 0.0, 1440.0),
        ("direct_marketing_profit_hold_ct_per_kwh", "DV-Gewinn-Halteband", dm_profit_hold, "ct/kWh", 0.0, None),
        ("direct_marketing_margin_hold_pct", "DV-Margen-Halteband", dm_margin_hold, "%", 0.0, 50.0),
        ("direct_marketing_degradation_ct_per_kwh", "DV-Degeneration", dm_degradation, "ct/kWh", 0.0, None),
        ("direct_marketing_roundtrip_efficiency_pct", "DV-Wirkungsgrad", dm_efficiency, "%", 50.0, 100.0),
        ("direct_marketing_safety_margin_ct_per_kwh", "DV-Sicherheitskorrektur", dm_safety_margin, "ct/kWh", -10.0, 50.0),
        ("direct_marketing_max_export_w", "DV-Basis-Entladung", dm_max_export_w, "W", 0.0, None),
        ("direct_marketing_min_grid_export_w", "DV-Mindest-Netzexport", dm_min_grid_export_w, "W", 0.0, None),
        ("direct_marketing_max_grid_charge_w", "DV-Max-Netzladen", dm_max_grid_charge_w, "W", 0.0, None),
        ("direct_marketing_pv_store_threshold_ct", "DV-PV-Speicher-Schwelle", dm_pv_store_threshold, "ct/kWh", -50.0, 200.0),
        ("direct_marketing_pv_store_max_w", "DV-Max-PV-Speichern", dm_pv_store_max_w, "W", 0.0, None),
        ("direct_marketing_pv_store_min_surplus_w", "DV-PV-Mindestüberschuss", dm_pv_store_min_surplus_w, "W", 0.0, None),
        ("direct_marketing_pv_store_import_guard_w", "DV-PV-Importwächter", dm_pv_store_import_guard_w, "W", 0.0, None),
        ("direct_marketing_pv_store_min_hold_s", "DV-PV-Mindesthaltezeit", dm_pv_store_min_hold_s, "s", 0.0, 3600.0),
        ("direct_marketing_pv_store_ramp_step_w", "DV-PV-Laderampe", dm_pv_store_ramp_step_w, "W/Zyklus", 100.0, 5000.0),
        ("direct_marketing_pv_store_external_ac_guard_w", "DV-PV-AC-Zusatz-Wächter", dm_pv_store_external_ac_guard_w, "W", 0.0, None),
        ("direct_marketing_pv_store_export_limit_guard_w", "DV-PV-Exportlimit-Toleranz", dm_pv_store_export_limit_guard_w, "W", 0.0, None),
        ("direct_marketing_pv_store_export_limit_ramp_bypass_w", "DV-PV-Exportlimit-Rampenbypass", dm_pv_store_export_limit_ramp_bypass_w, "W", 0.0, 5000.0),
        ("direct_marketing_price_max_age_s", "DV-Preis-Maxalter", dm_price_max_age_s, "s", 0.0, None),
        ("direct_marketing_max_cycles_per_day", "DV-Zyklenlimit", dm_cycles, "Zyklen/Tag", 0.0, 3.0),
        ("direct_marketing_keep_headroom_pct", "DV-Headroom", dm_headroom, "%", 0.0, 100.0),
        ("direct_marketing_negative_headroom_lookahead_min", "DV-Preisfenster-Headroom-Vorlauf", dm_negative_headroom_lookahead_min, "min", 0.0, 1440.0),
        ("direct_marketing_negative_headroom_min_window_min", "DV-Preisfenster-Mindestfenster", dm_negative_headroom_min_window_min, "min", 0.0, 720.0),
        ("direct_marketing_negative_headroom_min_surplus_wh", "DV-Preisfenster-Mindestüberschuss", dm_negative_headroom_min_surplus_wh, "Wh", 0.0, None),
        ("direct_marketing_negative_headroom_buffer_pct", "DV-Preisfenster-Headroom-Puffer", dm_negative_headroom_buffer_pct, "%", 0.0, 50.0),
        ("direct_marketing_low_price_curtail_limit_w", "DV-Billigpreis-Restexport", dm_curtail_limit_w, "W", 0.0, None),
        ("direct_marketing_eeg_support_years", "DV-EEG-Foerderjahre", dm_eeg_support_years, "Jahre", 0.0, 30.0),
    )
    for key, label, value, unit, minimum, maximum in dm_numeric_checks:
        severity = "ok"
        message = "Wert ist plausibel."
        check_active = dm_eeg_enabled if key == "direct_marketing_eeg_support_years" else dm_enabled
        if check_active:
            if minimum is not None and value < minimum:
                severity = "warning"
                message = "Wert darf nicht negativ oder unter der Mindestgrenze liegen."
            elif maximum is not None and value > maximum:
                severity = "warning"
                message = "Wert liegt ueber der plausiblen Obergrenze."
            elif key == "direct_marketing_safety_margin_ct_per_kwh" and value < 0.0:
                severity = "warning"
                message = "Negative Korrektur macht die Regelung aggressiver; Mindestmarge bleibt als Schutz aktiv."
            elif key == "direct_marketing_min_margin_pct" and value < 10.0:
                severity = "warning"
                message = "Mindestmarge unter 10 Prozent; Wirtschaftlichkeit wird zu aggressiv."
            elif key == "direct_marketing_max_export_w" and dm_export_enabled and value <= 0.0:
                severity = "warning"
                message = "Export ist freigegeben, aber die Basis-Entladung steht auf 0 W."
            elif key == "direct_marketing_max_grid_charge_w" and dm_grid_charge_enabled and value <= 0.0:
                severity = "warning"
                message = "Netzladen ist freigegeben, aber die maximale Netzladeleistung steht auf 0 W."
            elif key == "direct_marketing_pv_store_min_surplus_w" and dm_pv_store_enabled and value < 300.0:
                severity = "warning"
                message = "PV-Speichern unter 300 W kann takten und Messrauschen folgen."
            elif key == "direct_marketing_pv_store_import_guard_w" and dm_pv_store_enabled and value > 300.0:
                severity = "warning"
                message = "Hohe Importwächter-Grenze kann Netzbezug beim PV-Speichern zulassen."
            elif key == "direct_marketing_pv_store_max_w" and dm_pv_store_enabled and value <= 0.0:
                message = "0 W bedeutet Automatik: der Storage Manager nutzt das System-Ladelimit und den realen PV-Überschuss."
            elif key == "direct_marketing_pv_store_min_hold_s" and dm_pv_store_enabled and value < 300.0:
                severity = "warning"
                message = "Mindesthaltezeit unter 5 Minuten kann PV-Speichern nervös takten lassen."
            elif key == "direct_marketing_pv_store_ramp_step_w" and dm_pv_store_enabled and value > 3000.0:
                severity = "warning"
                message = "Sehr große Laderampen können die DV-PV-Ladeleistung trotz Preisfenster sprunghaft machen."
            elif key == "direct_marketing_pv_store_export_limit_guard_w" and dm_pv_store_enabled and value > 500.0:
                severity = "warning"
                message = "Hohe Exportlimit-Toleranz kann bei Negativpreisfenstern unnötigen Restexport zulassen."
            elif key == "direct_marketing_pv_store_export_limit_ramp_bypass_w" and dm_pv_store_enabled and value > 1000.0:
                severity = "warning"
                message = "Hoher Rampenbypass reagiert spät auf Exportlimit-0-Fenster."
            elif key == "direct_marketing_negative_headroom_lookahead_min" and dm_negative_headroom_enabled and value < 30.0:
                severity = "warning"
                message = "Sehr kurzer Vorlauf kann die Ladekurve vor Negativpreisfenstern zu spät beruhigen."
            elif key == "direct_marketing_negative_headroom_min_window_min" and dm_negative_headroom_enabled and value < 15.0:
                severity = "warning"
                message = "Sehr kurze Negativpreisfenster sind oft Prognose- und Rampenrauschen; mindestens 15 Minuten sind empfohlen."
            elif key == "direct_marketing_negative_headroom_min_surplus_wh" and dm_negative_headroom_enabled and value < 300.0:
                severity = "warning"
                message = "Sehr kleiner Mindestüberschuss kann Headroom-Holds aus Messrauschen erzeugen."
            elif key == "direct_marketing_price_max_age_s" and dm_enabled and value == 0.0:
                message = "0 bedeutet: Preisalter wird nicht pauschal begrenzt; explizite Stale-Flags blockieren weiterhin aktiv."
        price[key] = _entry(
            key=key,
            label=label,
            unit=unit,
            configured=value if _has_user_value(cfg, key) else None,
            live_value=None,
            live_key=None,
            effective=value,
            source="user" if _has_user_value(cfg, key) else "default",
            severity=severity,
            message=message,
        )

    price["direct_marketing_arbitrage_enable"] = _entry(
        key="direct_marketing_arbitrage_enable",
        label="DV-Arbitrage-Scharfschalter",
        unit="-",
        configured=cfg.get("direct_marketing_arbitrage_enable") if _has_user_value(cfg, "direct_marketing_arbitrage_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_arbitrage,
        source="user" if _has_user_value(cfg, "direct_marketing_arbitrage_enable") else "default",
        severity="warning" if dm_enabled and dm_mode == "arbitrage" and not dm_arbitrage else "ok",
        message=(
            "Arbitrage ist gewaehlt, aber die zusaetzliche Sicherheitsfreigabe ist aus."
            if dm_enabled and dm_mode == "arbitrage" and not dm_arbitrage else "Arbitrage-Sicherheitsfreigabe ist plausibel."
        ),
    )
    price["direct_marketing_export_enable"] = _entry(
        key="direct_marketing_export_enable",
        label="DV-Batterieeinspeisung",
        unit="-",
        configured=cfg.get("direct_marketing_export_enable") if _has_user_value(cfg, "direct_marketing_export_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_export_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_export_enable") else "default",
        severity="ok",
        message="Batterieeinspeisung ist separat freigegeben." if dm_export_enabled else "Batterieeinspeisung ist gesperrt.",
    )
    price["direct_marketing_grid_charge_enable"] = _entry(
        key="direct_marketing_grid_charge_enable",
        label="DV-Netzladen",
        unit="-",
        configured=cfg.get("direct_marketing_grid_charge_enable") if _has_user_value(cfg, "direct_marketing_grid_charge_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_grid_charge_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_grid_charge_enable") else "default",
        severity="ok",
        message="Netzladen ist separat freigegeben." if dm_grid_charge_enabled else "Netzladen ist gesperrt.",
    )
    for key, label, value in (
        ("direct_marketing_negative_price_no_export", "DV-kein Verkauf bei Negativpreis", dm_negative_no_export),
        ("direct_marketing_negative_headroom_enable", "DV-Headroom vor Preisfenstern", dm_negative_headroom_enabled),
        ("direct_marketing_low_price_headroom_enable", "DV-Headroom vor Billigpreis", dm_low_price_headroom_enabled),
        ("direct_marketing_low_price_no_export", "DV-kein Verkauf bei Billigpreis", dm_low_no_export),
    ):
        price[key] = _entry(
            key=key,
            label=label,
            unit="-",
            configured=cfg.get(key) if _has_user_value(cfg, key) else None,
            live_value=None,
            live_key=None,
            effective=value,
            source="user" if _has_user_value(cfg, key) else "default",
            severity="warning" if dm_enabled and not value else "ok",
            message="Schutz ist aktiv." if value else "Schutz ist aus; Verkauf in unguenstigen Preisfenstern waere spaeter moeglich.",
        )
    price["direct_marketing_low_price_curtail_enable"] = _entry(
        key="direct_marketing_low_price_curtail_enable",
        label="DV-Negativpreis-Einspeisebegrenzung",
        unit="-",
        configured=cfg.get("direct_marketing_low_price_curtail_enable") if _has_user_value(cfg, "direct_marketing_low_price_curtail_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_curtail_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_low_price_curtail_enable") else "default",
        severity="ok",
        message=(
            "Harte Gesamt-Einspeisebegrenzung ist ausschließlich für Negativpreise freigegeben; positive EEG-/Billigpreisfenster bleiben weich."
            if dm_curtail_enabled else "Zusätzliche harte Negativpreis-Einspeisebegrenzung ist aus."
        ),
    )

    warnings = _warning_count(storage, wallbox, consumer, price)
    return {
        "ts": now,
        "version": 2,
        "summary": {
            "warnings": warnings,
            "live_available": bool(live),
            "source_order": "user_then_rscp_then_default",
        },
        "storage": storage,
        "wallbox": wallbox,
        "consumer": consumer,
        "price": price,
    }


def atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
        try:
            if os.path.basename(path) == "e3dc_v4.json":
                apply_config_secret_permissions(path, data=payload if isinstance(payload, dict) else None)
            else:
                os.chmod(path, 0o664)
        except Exception:
            pass
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def write_config_validation(
    cfg: Optional[Dict[str, Any]],
    live: Optional[Dict[str, Any]],
    path: str = CONFIG_VALIDATION_F,
) -> Dict[str, Any]:
    payload = validate_storage_config(cfg, live)
    _atomic_write_on_change(
        path,
        payload,
        force_interval_s=60.0,
        noise_keys={"ts"},
        indent=2,
    )
    return payload
