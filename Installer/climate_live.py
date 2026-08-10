#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Read-only Klimaanlagen-Monitor.

Der Dienst liest einen eigenen lokalen Energiezähler fuer die Klimaanlage und
schreibt die Messwerte als separaten Zusatzverbraucher in die Ramdisk. Er
schaltet bewusst nichts: keine Toshiba-Cloud, kein Shelly-Relais, keine
Wärmepumpen-/Heizstab-Logik.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


CONFIG_FILE = Path("/var/www/html/data/e3dc_v4.json")
RAMDISK_FILE = Path("/var/www/html/ramdisk/climate_load.json")
BASELINE_FILE = Path("/var/www/html/data/climate_energy_baseline.json")
HISTORY_DIR = Path("/var/www/html/data/climate_history")
HISTORY_FLUSH_INTERVAL_S = 5 * 60
HISTORY_RETRY_BASE_S = 5
HISTORY_RETRY_MAX_S = 5 * 60
HISTORY_BUFFER_MAX_AGE_S = 24 * 60 * 60
# 24 h bei der kleinsten erlaubten 15-s-Abtastung ergeben 5.761 Sätze.
HISTORY_BUFFER_MAX_ROWS = 6_000

SHELLY_TIMEOUT_S = 3.0
RUNNING = True

DEFAULT_CONFIG: dict[str, Any] = {
    "climate_enable": "0",
    "climate_name": "Klimaanlage",
    "climate_meter_ip": "0.0.0.0",
    "climate_meter_type": "shelly_pro3em",
    "climate_meter_phase": "c",
    "climate_min_power_w": "50",
    "climate_poll_s": "15",
    "climate_history_enable": "1",
    "climate_history_interval_s": "60",
    "climate_forecast_enable": "0",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any, default: int = 0, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        parsed = default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def cfg_text(cfg: dict[str, Any], key: str, default: str = "") -> str:
    return str(cfg.get(key, default)).strip()


def cfg_bool(cfg: dict[str, Any], key: str, default: bool = False) -> bool:
    raw = cfg_text(cfg, key, "1" if default else "0").lower()
    return raw in {"1", "true", "yes", "on", "ja", "ein"}


def valid_ip_config(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return raw not in {"", "0", "0.0.0.0", "none", "null"}


def load_config(path: Path = CONFIG_FILE) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if not path.exists():
            raise FileNotFoundError(f"Konfigurationsdatei fehlt: {path}")
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("Konfiguration muss ein JSON-Objekt sein")
        for key in DEFAULT_CONFIG:
            if key in data:
                cfg[key] = data[key]
    except Exception as exc:
        cfg["_config_error"] = str(exc)
    return cfg


def normalize_phase(value: Any) -> str:
    raw = str(value or "c").strip().lower()
    aliases = {
        "1": "a",
        "l1": "a",
        "phase_a": "a",
        "phase1": "a",
        "2": "b",
        "l2": "b",
        "phase_b": "b",
        "phase2": "b",
        "3": "c",
        "l3": "c",
        "phase_c": "c",
        "phase3": "c",
        "sum": "total",
        "all": "total",
        "gesamt": "total",
        "1p": "single",
        "einphasig": "single",
        "single": "single",
        "em1": "single",
        "pm1": "single",
        "channel_0": "channel0",
        "ch0": "channel0",
        "emeter0": "channel0",
        "emeter:0": "channel0",
        "emeter/0": "channel0",
        "kanal0": "channel0",
        "channel_1": "channel1",
        "ch1": "channel1",
        "emeter1": "channel1",
        "emeter:1": "channel1",
        "emeter/1": "channel1",
        "kanal1": "channel1",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in {"a", "b", "c", "total", "single", "channel0", "channel1"} else "c"


def normalize_meter_type(value: Any) -> str:
    raw = str(value or "shelly_pro3em").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "auto": "auto",
        "shelly": "auto",
        "shelly_gen2": "shelly_pro3em",
        "shelly_3em": "shelly_pro3em",
        "shelly_pro_3em": "shelly_pro3em",
        "pro3em": "shelly_pro3em",
        "3em": "shelly_pro3em",
        "shelly_em": "shelly_em_gen1",
        "shelly_em_legacy": "shelly_em_gen1",
        "em_gen1": "shelly_em_gen1",
        "gen1_em": "shelly_em_gen1",
        "shelly_mini_em_gen4": "shelly_em_mini_gen4",
        "shelly_em_mini": "shelly_em_mini_gen4",
        "shelly_mini_em": "shelly_em_mini_gen4",
        "em_mini_gen4": "shelly_em_mini_gen4",
        "mini_em_gen4": "shelly_em_mini_gen4",
        "em1": "shelly_em_mini_gen4",
        "shelly_pm_mini_gen3": "shelly_pm_mini",
        "shelly_pm_mini_gen4": "shelly_pm_mini",
        "shelly_pm1": "shelly_pm_mini",
        "pm_mini": "shelly_pm_mini",
        "pm1": "shelly_pm_mini",
    }
    return aliases.get(raw, raw)


def http_json(url: str, timeout_s: float = SHELLY_TIMEOUT_S) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "E3DC-Control climate_live"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        raw = response.read(1024 * 1024)
    data = json.loads(raw.decode("utf-8"))
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        return data["result"]
    if not isinstance(data, dict):
        raise ValueError("HTTP response is not a JSON object")
    return data


def read_shelly_status(ip: str, timeout_s: float = SHELLY_TIMEOUT_S) -> dict[str, Any]:
    return http_json(f"http://{ip}/rpc/Shelly.GetStatus", timeout_s=timeout_s)


def read_shelly_em_gen1(ip: str, timeout_s: float = SHELLY_TIMEOUT_S) -> dict[str, Any]:
    """Liest den Status eines Shelly EM Gen1 ohne einen Steuerbefehl auszulösen."""
    return http_json(f"http://{ip}/status", timeout_s=timeout_s)


def read_shelly_em_mini_gen4(ip: str, timeout_s: float = SHELLY_TIMEOUT_S) -> dict[str, Any]:
    """Read Shelly EM Mini Gen4 through its dedicated EM1 RPC components.

    Some firmware builds do not include the EM1/EM1Data blocks completely in
    Shelly.GetStatus. The official RPC contract exposes the meter through
    EM1.GetStatus and the perpetual energy counter through EM1Data.GetStatus.
    """
    payload: dict[str, Any] = {}
    try:
        payload = read_shelly_status(ip, timeout_s=timeout_s)
    except Exception as exc:
        payload = {"_shelly_getstatus_error": f"{type(exc).__name__}: {exc}"}

    if not isinstance(payload.get("em1:0"), dict):
        em1 = http_json(f"http://{ip}/rpc/EM1.GetStatus?id=0", timeout_s=timeout_s)
        if isinstance(em1, dict):
            payload["em1:0"] = em1
            payload["em1_rpc_source"] = "EM1.GetStatus"

    if not isinstance(payload.get("em1data:0"), dict):
        try:
            em1data = http_json(f"http://{ip}/rpc/EM1Data.GetStatus?id=0", timeout_s=timeout_s)
            if isinstance(em1data, dict):
                payload["em1data:0"] = em1data
                payload["em1data_rpc_source"] = "EM1Data.GetStatus"
        except Exception as exc:
            payload["em1data_rpc_error"] = f"{type(exc).__name__}: {exc}"

    return payload


def read_shelly_pro3em(ip: str, timeout_s: float = SHELLY_TIMEOUT_S) -> dict[str, Any]:
    return read_shelly_status(ip, timeout_s=timeout_s)


def unwrap_shelly_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("result"), dict):
        payload = payload["result"]
    return payload


def _phase_status(em: dict[str, Any], emdata: dict[str, Any], phase: str) -> dict[str, Any]:
    prefix = f"{phase}_"
    energy_wh = _safe_float_or_none(emdata.get(f"{prefix}total_act_energy"))
    return {
        "power_w_raw": round(_safe_float(em.get(f"{prefix}act_power"), 0.0), 3),
        "apparent_power_va": round(_safe_float(em.get(f"{prefix}aprt_power"), 0.0), 3),
        "current_a": round(_safe_float(em.get(f"{prefix}current"), 0.0), 4),
        "voltage_v": round(_safe_float(em.get(f"{prefix}voltage"), 0.0), 2),
        "pf": round(_safe_float(em.get(f"{prefix}pf"), 0.0), 4),
        "freq_hz": round(_safe_float(em.get(f"{prefix}freq"), 0.0), 3),
        "energy_total_wh": round(energy_wh, 3) if energy_wh is not None else None,
        "energy_total_kwh": round(energy_wh / 1000.0, 6) if energy_wh is not None else None,
    }


def _shelly_context(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    cloud = payload.get("cloud") if isinstance(payload.get("cloud"), dict) else {}
    wifi = payload.get("wifi") if isinstance(payload.get("wifi"), dict) else {}
    sys_block = payload.get("sys") if isinstance(payload.get("sys"), dict) else {}
    return cloud, wifi, sys_block


def _build_meter_status(
    payload: dict[str, Any],
    cfg: dict[str, Any],
    *,
    source: str,
    phase: str,
    now_ts: float | None,
    raw_power_w: float,
    apparent_va: float | None,
    current_a: float | None,
    voltage_v: float | None,
    pf: float | None,
    freq_hz: float | None,
    energy_wh: float | None,
    meter_channel: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ts = float(now_ts if now_ts is not None else time.time())
    min_power_w = max(0, _safe_int(cfg.get("climate_min_power_w"), 50))
    power_w = max(0.0, raw_power_w)
    energy_kwh = (float(energy_wh) / 1000.0) if energy_wh is not None else None
    cloud, wifi, sys_block = _shelly_context(payload)
    status = {
        "success": True,
        "enabled": True,
        "online": True,
        "type": "climate_load",
        "source": source,
        "name": cfg_text(cfg, "climate_name", "Klimaanlage") or "Klimaanlage",
        "ip": cfg_text(cfg, "climate_meter_ip", ""),
        "phase": phase,
        "meter_channel": meter_channel,
        "ts": int(ts),
        "ts_iso": datetime.fromtimestamp(ts).isoformat(timespec="seconds"),
        "age_s": 0,
        "power_w": round(power_w, 1),
        "raw_power_w": round(raw_power_w, 3),
        "measurement_valid": True,
        "active": bool(power_w >= min_power_w),
        "min_power_w": min_power_w,
        "apparent_power_va": round(apparent_va, 1) if apparent_va is not None else None,
        "current_a": round(current_a, 4) if current_a is not None else None,
        "voltage_v": round(voltage_v, 2) if voltage_v is not None else None,
        "pf": round(pf, 4) if pf is not None else None,
        "freq_hz": round(freq_hz, 3) if freq_hz is not None else None,
        "energy_total_wh": round(float(energy_wh), 3) if energy_wh is not None else None,
        "energy_total_kwh": round(energy_kwh, 6) if energy_kwh is not None else None,
        "daily_kwh": None,
        "history_enabled": cfg_bool(cfg, "climate_history_enable", True),
        "forecast_enabled": cfg_bool(cfg, "climate_forecast_enable", False),
        "control_enabled": False,
        "control_mode": "none",
        "cloud_connected": bool(cloud.get("connected")) if cloud else None,
        "wifi_ip": wifi.get("sta_ip") if isinstance(wifi.get("sta_ip"), str) else None,
        "shelly_uptime_s": sys_block.get("uptime"),
    }
    if extra:
        status.update(extra)
    return status


def parse_shelly_pro3em_status(payload: dict[str, Any], cfg: dict[str, Any] | None = None, now_ts: float | None = None) -> dict[str, Any]:
    cfg = cfg or DEFAULT_CONFIG
    payload = unwrap_shelly_status_payload(payload)

    em = payload.get("em:0")
    if not isinstance(em, dict):
        em = {}
    emdata = payload.get("emdata:0")
    if not isinstance(emdata, dict):
        emdata = {}

    if not em:
        raise ValueError("Shelly.GetStatus enthaelt keinen em:0 Block")

    phase = normalize_phase(cfg_text(cfg, "climate_meter_phase", "c"))
    if phase not in {"a", "b", "c", "total"}:
        raise ValueError("Shelly Pro3EM benötigt Phase A, B, C oder Summe")
    phases = {p: _phase_status(em, emdata, p) for p in ("a", "b", "c")}

    if phase == "total":
        raw_power_w = _safe_float(em.get("total_act_power"), sum(p["power_w_raw"] for p in phases.values()))
        apparent_va = _safe_float(em.get("total_aprt_power"), sum(p["apparent_power_va"] for p in phases.values()))
        current_a = _safe_float(em.get("total_current"), sum(p["current_a"] for p in phases.values()))
        energy_wh = _safe_float_or_none(emdata.get("total_act"))
        voltage_v = None
        pf = None
        freq_hz = None
    else:
        selected = phases[phase]
        raw_power_w = float(selected["power_w_raw"])
        apparent_va = float(selected["apparent_power_va"])
        current_a = float(selected["current_a"])
        voltage_v = selected["voltage_v"]
        pf = selected["pf"]
        freq_hz = selected["freq_hz"]
        energy_wh = selected["energy_total_wh"]

    return _build_meter_status(
        payload,
        cfg,
        source="shelly_pro3em",
        phase=phase,
        now_ts=now_ts,
        raw_power_w=raw_power_w,
        apparent_va=apparent_va,
        current_a=current_a,
        voltage_v=voltage_v,
        pf=pf,
        freq_hz=freq_hz,
        energy_wh=energy_wh,
        meter_channel="em:0",
        extra={"phases": phases},
    )


def normalize_shelly_em_gen1_channel(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "a": "channel0",
        "l1": "channel0",
        "phase_a": "channel0",
        "phase1": "channel0",
        "single": "channel0",
        "channel_0": "channel0",
        "ch0": "channel0",
        "emeter0": "channel0",
        "emeter:0": "channel0",
        "emeter/0": "channel0",
        "kanal0": "channel0",
        "b": "channel1",
        "l2": "channel1",
        "phase_b": "channel1",
        "phase2": "channel1",
        "channel_1": "channel1",
        "ch1": "channel1",
        "emeter1": "channel1",
        "emeter:1": "channel1",
        "emeter/1": "channel1",
        "kanal1": "channel1",
        "sum": "total",
        "all": "total",
        "gesamt": "total",
    }
    channel = aliases.get(raw, raw)
    if channel not in {"channel0", "channel1", "total"}:
        raise ValueError("Shelly EM Gen1 benötigt Kanal 0, Kanal 1 oder Summe")
    return channel


def _finite_number_or_none(value: Any) -> float | None:
    parsed = _safe_float_or_none(value)
    if parsed is None or not math.isfinite(parsed):
        return None
    return parsed


def _parse_shelly_em_gen1_channel(payload: dict[str, Any], index: int) -> dict[str, Any]:
    emeters = payload.get("emeters")
    if not isinstance(emeters, list) or index >= len(emeters):
        raise ValueError(f"Shelly EM Gen1 enthält keinen emeters[{index}] Kanal")
    emeter = emeters[index]
    if not isinstance(emeter, dict):
        raise ValueError(f"Shelly EM Gen1 emeters[{index}] ist kein Objekt")
    if emeter.get("is_valid") is not True:
        raise ValueError(f"Shelly EM Gen1 emeters[{index}] ist nicht gültig")
    power_w = _finite_number_or_none(emeter.get("power"))
    if power_w is None:
        raise ValueError(f"Shelly EM Gen1 emeters[{index}] enthält keine gültige Leistung")
    return {
        "index": index,
        "power_w_raw": power_w,
        "reactive_power_var": _finite_number_or_none(emeter.get("reactive")),
        "voltage_v": _finite_number_or_none(emeter.get("voltage")),
        "energy_total_wh": _finite_number_or_none(emeter.get("total")),
        "energy_returned_wh": _finite_number_or_none(emeter.get("total_returned")),
        "is_valid": True,
    }


def parse_shelly_em_gen1_status(payload: dict[str, Any], cfg: dict[str, Any] | None = None, now_ts: float | None = None) -> dict[str, Any]:
    cfg = cfg or DEFAULT_CONFIG
    payload = unwrap_shelly_status_payload(payload)
    channel = normalize_shelly_em_gen1_channel(cfg_text(cfg, "climate_meter_phase", ""))

    indices = (0, 1) if channel == "total" else (0 if channel == "channel0" else 1,)
    channels = [_parse_shelly_em_gen1_channel(payload, index) for index in indices]
    raw_power_w = sum(float(item["power_w_raw"]) for item in channels)

    energy_values = [item["energy_total_wh"] for item in channels]
    energy_wh = sum(float(value) for value in energy_values) if all(value is not None for value in energy_values) else None
    reactive_values = [item["reactive_power_var"] for item in channels]
    reactive_var = sum(float(value) for value in reactive_values) if all(value is not None for value in reactive_values) else None
    voltage_v = channels[0]["voltage_v"] if len(channels) == 1 else None

    return _build_meter_status(
        payload,
        cfg,
        source="shelly_em_gen1",
        phase=channel,
        now_ts=now_ts,
        raw_power_w=raw_power_w,
        apparent_va=None,
        current_a=None,
        voltage_v=voltage_v,
        pf=None,
        freq_hz=None,
        energy_wh=energy_wh,
        meter_channel="emeter:sum" if channel == "total" else f"emeter:{indices[0]}",
        extra={
            "channels": {str(item["index"]): item for item in channels},
            "reactive_power_var": round(reactive_var, 3) if reactive_var is not None else None,
        },
    )


def parse_shelly_em_mini_gen4_status(payload: dict[str, Any], cfg: dict[str, Any] | None = None, now_ts: float | None = None) -> dict[str, Any]:
    cfg = cfg or DEFAULT_CONFIG
    payload = unwrap_shelly_status_payload(payload)
    em = payload.get("em1:0")
    if not isinstance(em, dict) or not em:
        raise ValueError("Shelly.GetStatus enthaelt keinen em1:0 Block")
    emdata = payload.get("em1data:0")
    if not isinstance(emdata, dict):
        emdata = {}
    energy_wh = _safe_float_or_none(emdata.get("total_act_energy"))
    return _build_meter_status(
        payload,
        cfg,
        source="shelly_em_mini_gen4",
        phase=normalize_phase(cfg_text(cfg, "climate_meter_phase", "single")),
        now_ts=now_ts,
        raw_power_w=_safe_float(em.get("act_power"), 0.0),
        apparent_va=_safe_float_or_none(em.get("aprt_power")),
        current_a=_safe_float_or_none(em.get("current")),
        voltage_v=_safe_float_or_none(em.get("voltage")),
        pf=_safe_float_or_none(em.get("pf")),
        freq_hz=_safe_float_or_none(em.get("freq")),
        energy_wh=energy_wh,
        meter_channel="em1:0",
        extra={
            "meter_status_source": payload.get("em1_rpc_source") or "Shelly.GetStatus",
            "energy_status_source": payload.get("em1data_rpc_source") or (
                "Shelly.GetStatus" if isinstance(payload.get("em1data:0"), dict) else None
            ),
            "energy_status_error": payload.get("em1data_rpc_error"),
        },
    )


def parse_shelly_pm_mini_status(payload: dict[str, Any], cfg: dict[str, Any] | None = None, now_ts: float | None = None) -> dict[str, Any]:
    cfg = cfg or DEFAULT_CONFIG
    payload = unwrap_shelly_status_payload(payload)
    pm = payload.get("pm1:0")
    if not isinstance(pm, dict) or not pm:
        raise ValueError("Shelly.GetStatus enthaelt keinen pm1:0 Block")
    aenergy = pm.get("aenergy") if isinstance(pm.get("aenergy"), dict) else {}
    energy_wh = _safe_float_or_none(aenergy.get("total"))
    return _build_meter_status(
        payload,
        cfg,
        source="shelly_pm_mini",
        phase=normalize_phase(cfg_text(cfg, "climate_meter_phase", "single")),
        now_ts=now_ts,
        raw_power_w=_safe_float(pm.get("apower"), 0.0),
        apparent_va=None,
        current_a=_safe_float_or_none(pm.get("current")),
        voltage_v=_safe_float_or_none(pm.get("voltage")),
        pf=None,
        freq_hz=_safe_float_or_none(pm.get("freq")),
        energy_wh=energy_wh,
        meter_channel="pm1:0",
        extra={"output": pm.get("output") if isinstance(pm.get("output"), bool) else None},
    )


def parse_shelly_status(payload: dict[str, Any], cfg: dict[str, Any] | None = None, now_ts: float | None = None) -> dict[str, Any]:
    cfg = cfg or DEFAULT_CONFIG
    payload = unwrap_shelly_status_payload(payload)
    meter_type = normalize_meter_type(cfg_text(cfg, "climate_meter_type", "auto"))
    if meter_type == "shelly_pro3em" or (meter_type == "auto" and isinstance(payload.get("em:0"), dict)):
        return parse_shelly_pro3em_status(payload, cfg, now_ts=now_ts)
    if meter_type == "shelly_em_mini_gen4" or (meter_type == "auto" and isinstance(payload.get("em1:0"), dict)):
        return parse_shelly_em_mini_gen4_status(payload, cfg, now_ts=now_ts)
    if meter_type == "shelly_pm_mini" or (meter_type == "auto" and isinstance(payload.get("pm1:0"), dict)):
        return parse_shelly_pm_mini_status(payload, cfg, now_ts=now_ts)
    if meter_type == "shelly_em_gen1" or (meter_type == "auto" and isinstance(payload.get("emeters"), list)):
        return parse_shelly_em_gen1_status(payload, cfg, now_ts=now_ts)
    raise ValueError(f"Nicht unterstuetzter Zaehler-Typ: {meter_type}")


def _has_supported_rpc_meter(payload: dict[str, Any]) -> bool:
    payload = unwrap_shelly_status_payload(payload)
    return any(isinstance(payload.get(key), dict) for key in ("em:0", "em1:0", "pm1:0"))


def disabled_status(cfg: dict[str, Any], reason: str = "disabled") -> dict[str, Any]:
    now_ts = time.time()
    return {
        "success": True,
        "enabled": False,
        "online": False,
        "type": "climate_load",
        "source": "disabled",
        "name": cfg_text(cfg, "climate_name", "Klimaanlage") or "Klimaanlage",
        "ip": cfg_text(cfg, "climate_meter_ip", ""),
        "phase": normalize_phase(cfg_text(cfg, "climate_meter_phase", "c")),
        "ts": int(now_ts),
        "ts_iso": datetime.fromtimestamp(now_ts).isoformat(timespec="seconds"),
        "power_w": 0,
        "measurement_valid": True,
        "daily_kwh": None,
        "active": False,
        "reason": reason,
        "control_enabled": False,
        "control_mode": "none",
    }


def error_status(cfg: dict[str, Any], exc: Exception | str) -> dict[str, Any]:
    now_ts = time.time()
    return {
        "success": False,
        "enabled": cfg_bool(cfg, "climate_enable", False),
        "online": False,
        "type": "climate_load",
        "source": normalize_meter_type(cfg_text(cfg, "climate_meter_type", "shelly_pro3em")),
        "name": cfg_text(cfg, "climate_name", "Klimaanlage") or "Klimaanlage",
        "ip": cfg_text(cfg, "climate_meter_ip", ""),
        "phase": normalize_phase(cfg_text(cfg, "climate_meter_phase", "c")),
        "ts": int(now_ts),
        "ts_iso": datetime.fromtimestamp(now_ts).isoformat(timespec="seconds"),
        "power_w": None,
        "raw_power_w": None,
        "measurement_valid": False,
        "daily_kwh": None,
        "active": False,
        "error": str(exc),
        "control_enabled": False,
        "control_mode": "none",
    }


def read_json_file(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def write_json_atomic(path: Path, payload: dict[str, Any], mode: int = 0o664) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def update_daily_energy(status: dict[str, Any], baseline_file: Path = BASELINE_FILE) -> dict[str, Any]:
    total_kwh = _safe_float_or_none(status.get("energy_total_kwh"))
    if total_kwh is None:
        return status

    today = datetime.fromtimestamp(_safe_float(status.get("ts"), time.time())).strftime("%Y-%m-%d")
    key = f"{status.get('source', 'unknown')}:{status.get('ip', '')}:{status.get('phase', '')}"
    state = read_json_file(baseline_file)
    if not isinstance(state, dict):
        state = {}
    entry = state.get(key)
    if not isinstance(entry, dict):
        entry = {}

    baseline = _safe_float_or_none(entry.get("baseline_total_kwh"))
    baseline_changed = bool(
        entry.get("date") != today
        or baseline is None
        or total_kwh + 0.001 < baseline
    )
    if baseline_changed:
        baseline = total_kwh
        entry = {
            "date": today,
            "baseline_total_kwh": round(baseline, 6),
            "source": status.get("source"),
            "ip": status.get("ip"),
            "phase": status.get("phase"),
        }
        state[key] = entry
        try:
            write_json_atomic(baseline_file, state)
        except Exception as exc:
            status["daily_baseline_error"] = str(exc)

    daily = max(0.0, total_kwh - baseline)
    status["daily_kwh"] = round(daily, 3)
    status["energy_baseline_kwh"] = round(baseline, 6)
    return status


class HistoryAppendIndeterminateError(RuntimeError):
    """Der Dateischwanz ist nach einem fehlgeschlagenen Append nicht bewiesen."""

    def __init__(self, message: str, contract: dict[str, Any]):
        super().__init__(message)
        self.contract = contract


def _discard_history_buffer(runtime_state: dict[str, Any]) -> None:
    for key in (
        "history_pending",
        "history_inflight",
        "history_last_valid_policy",
        "history_retry_attempts",
        "history_retry_delay_s",
        "history_retry_next_monotonic",
        "history_retry_scheduled_monotonic",
        "history_retry_kind",
        "history_last_error",
        "history_overflow_events",
        "history_overflow_dropped_rows",
        "history_overflow_last_error",
        "last_history_monotonic",
        "last_history_ts",
    ):
        runtime_state.pop(key, None)


def _clear_history_retry(runtime_state: dict[str, Any]) -> None:
    for key in (
        "history_retry_attempts",
        "history_retry_delay_s",
        "history_retry_next_monotonic",
        "history_retry_scheduled_monotonic",
        "history_retry_kind",
        "history_last_error",
    ):
        runtime_state.pop(key, None)


def _schedule_history_retry(
    runtime_state: dict[str, Any],
    exc: Exception | str,
    monotonic_ts: float,
    *,
    kind: str = "io",
    attempted: bool = True,
) -> None:
    now_value = float(monotonic_ts)
    next_value = _safe_float(runtime_state.get("history_retry_next_monotonic"), 0.0)
    scheduled_value = _safe_float(runtime_state.get("history_retry_scheduled_monotonic"), now_value)
    same_wait = bool(
        not attempted
        and runtime_state.get("history_retry_kind") == kind
        and now_value >= scheduled_value
        and now_value < next_value
    )
    if same_wait:
        runtime_state["history_last_error"] = str(exc)
        return
    attempts = int(runtime_state.get("history_retry_attempts", 0) or 0) + 1
    previous_delay_s = _safe_float(runtime_state.get("history_retry_delay_s"), 0.0)
    delay_s = float(HISTORY_RETRY_BASE_S) if previous_delay_s <= 0.0 else previous_delay_s * 2.0
    delay_s = min(float(HISTORY_RETRY_MAX_S), max(float(HISTORY_RETRY_BASE_S), delay_s))
    runtime_state["history_retry_attempts"] = attempts
    runtime_state["history_retry_delay_s"] = delay_s
    runtime_state["history_retry_scheduled_monotonic"] = now_value
    runtime_state["history_retry_next_monotonic"] = now_value + delay_s
    runtime_state["history_retry_kind"] = kind
    runtime_state["history_last_error"] = str(exc)


def _history_retry_ready(runtime_state: dict[str, Any], monotonic_ts: float) -> bool:
    next_value = _safe_float(runtime_state.get("history_retry_next_monotonic"), 0.0)
    if next_value <= 0.0:
        return True
    now_value = float(monotonic_ts)
    scheduled_value = _safe_float(runtime_state.get("history_retry_scheduled_monotonic"), now_value)
    return bool(now_value < scheduled_value or now_value >= next_value)


def _history_policy(cfg: dict[str, Any]) -> dict[str, bool]:
    return {
        "climate_enable": cfg_bool(cfg, "climate_enable", False),
        "climate_history_enable": cfg_bool(cfg, "climate_history_enable", True),
    }


def _resolve_history_policy(
    runtime_state: dict[str, Any],
    cfg: dict[str, Any],
    *,
    force: bool,
) -> dict[str, bool]:
    if cfg.get("_config_error"):
        remembered = runtime_state.get("history_last_valid_policy")
        if force and isinstance(remembered, dict):
            return {
                "climate_enable": bool(remembered.get("climate_enable", False)),
                "climate_history_enable": bool(remembered.get("climate_history_enable", False)),
            }
        raise RuntimeError("Klima-Historie: Konfiguration nicht lesbar; Puffer bleibt erhalten")
    policy = _history_policy(cfg)
    if policy["climate_enable"] and policy["climate_history_enable"]:
        runtime_state["history_last_valid_policy"] = dict(policy)
    else:
        _discard_history_buffer(runtime_state)
    return policy


def _history_buffer_due(runtime_state: dict[str, Any], monotonic_ts: float) -> bool:
    pending = runtime_state.get("history_pending")
    if not isinstance(pending, list) or not pending:
        return False
    oldest_monotonic = min(float(item.get("monotonic_ts", monotonic_ts)) for item in pending)
    elapsed_s = float(monotonic_ts) - oldest_monotonic
    return bool(elapsed_s < 0.0 or elapsed_s >= HISTORY_FLUSH_INTERVAL_S)


def _history_flush_ready(runtime_state: dict[str, Any], monotonic_ts: float) -> bool:
    has_inflight = isinstance(runtime_state.get("history_inflight"), dict)
    return bool(
        _history_retry_ready(runtime_state, monotonic_ts)
        and (has_inflight or _history_buffer_due(runtime_state, monotonic_ts))
    )


def _record_history_overflow(runtime_state: dict[str, Any], dropped: int, reason: str) -> None:
    if dropped <= 0:
        return
    runtime_state["history_overflow_events"] = int(runtime_state.get("history_overflow_events", 0) or 0) + 1
    runtime_state["history_overflow_dropped_rows"] = (
        int(runtime_state.get("history_overflow_dropped_rows", 0) or 0) + int(dropped)
    )
    runtime_state["history_overflow_last_error"] = reason


def _enforce_history_buffer_limits(runtime_state: dict[str, Any], monotonic_ts: float) -> None:
    pending = runtime_state.get("history_pending")
    if not isinstance(pending, list) or not pending:
        return
    now_value = float(monotonic_ts)
    kept: list[dict[str, Any]] = []
    age_dropped = 0
    for item in pending:
        item_monotonic = _safe_float(item.get("monotonic_ts"), now_value)
        age_s = now_value - item_monotonic
        if age_s >= 0.0 and age_s > float(HISTORY_BUFFER_MAX_AGE_S):
            age_dropped += 1
        else:
            kept.append(item)
    if age_dropped:
        pending[:] = kept
        _record_history_overflow(
            runtime_state,
            age_dropped,
            "Klima-Historie: RAM-Puffer überschritt 24 h; älteste Sätze wurden verworfen",
        )
    if len(pending) > int(HISTORY_BUFFER_MAX_ROWS):
        row_dropped = len(pending) - int(HISTORY_BUFFER_MAX_ROWS)
        del pending[:row_dropped]
        _record_history_overflow(
            runtime_state,
            row_dropped,
            "Klima-Historie: RAM-Puffer überschritt das Zeilenlimit; älteste Sätze wurden verworfen",
        )
    if not pending:
        runtime_state.pop("history_pending", None)


def _history_append_contract(
    path: Path,
    original_size: int,
    payload: bytes,
    expected_tail: bytes,
    *,
    created: bool,
) -> dict[str, Any]:
    return {
        "path": str(path),
        "original_size": int(original_size),
        "payload": bytes(payload),
        "expected_tail": bytes(expected_tail),
        "created": bool(created),
    }


def _inspect_history_append(contract: dict[str, Any]) -> tuple[str, str]:
    path = Path(str(contract.get("path", "")))
    original_size = int(contract.get("original_size", 0) or 0)
    expected_tail = bytes(contract.get("expected_tail", b""))
    try:
        if not path.exists():
            if original_size == 0:
                return "unchanged", "Zieldatei entspricht dem nicht vorhandenen/leeren Preimage"
            return "foreign", "Zieldatei fehlt trotz gebundenem nichtleerem Preimage"
        current_size = path.stat().st_size
        if current_size < original_size:
            return "foreign", "Zieldatei ist kleiner als das gebundene Preimage"
        delta = current_size - original_size
        if delta == 0:
            return "unchanged", "Dateigröße entspricht dem gebundenen Preimage"
        if delta > len(expected_tail):
            return "foreign", "Dateischwanz ist länger als der erwartete Append"
        with path.open("rb") as handle:
            handle.seek(original_size)
            observed = handle.read(delta)
        if observed != expected_tail[:delta]:
            return "foreign", "Dateischwanz weicht vom erwarteten Append-Präfix ab"
        if delta == len(expected_tail):
            return "committed", "Erwarteter Append ist exakt und vollständig vorhanden"
        return "expected_prefix", "Ein exaktes Präfix des erwarteten Appends ist vorhanden"
    except OSError as exc:
        return "unknown", f"Dateischwanz konnte nicht gelesen werden: {exc}"


def _restore_history_preimage(contract: dict[str, Any]) -> None:
    path = Path(str(contract.get("path", "")))
    original_size = int(contract.get("original_size", 0) or 0)
    if not path.exists() and original_size == 0:
        return
    with path.open("r+b") as handle:
        handle.truncate(original_size)
    state, detail = _inspect_history_append(contract)
    if state != "unchanged":
        raise RuntimeError(f"Klima-Historie: Preimage-Rollback nicht bewiesen ({detail})")


def _append_history_payload(path: Path, payload: bytes, mode: int = 0o664) -> None:
    """Hängt einen Tagespuffer an; unklare Tails werden explizit reconciled."""

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, mode)
        created = True
    except FileExistsError:
        fd = os.open(path, flags | os.O_CREAT, mode)
        created = False
    if created:
        try:
            os.chmod(path, mode)
        except OSError:
            pass

    original_size: int | None = None
    expected_tail: bytes | None = None
    operation_error: Exception | None = None
    close_error: OSError | None = None
    try:
        # Erst diese erfolgreiche Bindung erlaubt irgendeinen Rollback/Unlink.
        original_size = int(os.fstat(fd).st_size)
        prefix = b""
        if original_size > 0:
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b"\n":
                prefix = b"\n"
        expected_tail = prefix + payload
        written = 0
        while written < len(expected_tail):
            count = os.write(fd, expected_tail[written:])
            if count <= 0:
                raise OSError("Klima-Historie: unvollständiger Append")
            written += count
    except Exception as exc:
        operation_error = exc
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            close_error = exc

    primary_error = operation_error or close_error
    if primary_error is None:
        return
    if original_size is None or expected_tail is None:
        # fstat/Preimage war nie gültig: niemals auf den Default 0 zurückrollen.
        raise primary_error

    contract = _history_append_contract(
        path,
        original_size,
        payload,
        expected_tail,
        created=created,
    )
    state, detail = _inspect_history_append(contract)
    if state == "committed":
        return
    if state in {"unchanged", "expected_prefix"}:
        if state == "expected_prefix":
            try:
                _restore_history_preimage(contract)
            except Exception as rollback_exc:
                raise HistoryAppendIndeterminateError(
                    f"Klima-Historie: Teil-Append ist indeterminiert ({rollback_exc})",
                    contract,
                ) from rollback_exc
        if created and original_size == 0:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise primary_error
    raise HistoryAppendIndeterminateError(
        f"Klima-Historie: Append-Zustand ist indeterminiert ({detail})",
        contract,
    ) from primary_error


def _store_history_inflight(
    runtime_state: dict[str, Any],
    contract: dict[str, Any],
    *,
    day: str,
    entries: list[dict[str, Any]],
) -> None:
    stored = dict(contract)
    stored["day"] = str(day)
    stored["row_count"] = len(entries)
    stored["oldest_monotonic"] = min(
        (_safe_float(item.get("monotonic_ts"), 0.0) for item in entries),
        default=0.0,
    )
    runtime_state["history_inflight"] = stored
    pending = runtime_state.get("history_pending")
    if isinstance(pending, list):
        entry_ids = {id(item) for item in entries}
        pending[:] = [item for item in pending if id(item) not in entry_ids]
        if not pending:
            runtime_state.pop("history_pending", None)


def _reconcile_history_inflight(runtime_state: dict[str, Any]) -> bool:
    contract = runtime_state.get("history_inflight")
    if not isinstance(contract, dict):
        return False
    state, detail = _inspect_history_append(contract)
    if state == "committed":
        runtime_state.pop("history_inflight", None)
        return True
    if state not in {"unchanged", "expected_prefix"}:
        raise RuntimeError(f"Klima-Historie: Reconcile bleibt fail-closed ({detail})")
    if state == "expected_prefix":
        _restore_history_preimage(contract)
    try:
        _append_history_payload(
            Path(str(contract["path"])),
            bytes(contract.get("payload", b"")),
        )
    except HistoryAppendIndeterminateError as exc:
        replacement = dict(exc.contract)
        for key in ("day", "row_count", "oldest_monotonic"):
            replacement[key] = contract.get(key)
        runtime_state["history_inflight"] = replacement
        raise
    runtime_state.pop("history_inflight", None)
    return True


def flush_history_buffer(
    runtime_state: dict[str, Any],
    cfg: dict[str, Any],
    history_dir: Path | None = None,
    *,
    monotonic_ts: float | None = None,
    force: bool = False,
) -> bool:
    """Schreibt gepufferte Minutensätze höchstens alle fünf Minuten je Tagesdatei."""

    current_monotonic = float(time.monotonic() if monotonic_ts is None else monotonic_ts)
    try:
        policy = _resolve_history_policy(runtime_state, cfg, force=force)
    except Exception as exc:
        if not force:
            _schedule_history_retry(
                runtime_state,
                exc,
                current_monotonic,
                kind="config",
                attempted=False,
            )
        raise
    if not policy["climate_enable"] or not policy["climate_history_enable"]:
        return False
    if runtime_state.get("history_retry_kind") == "config":
        _clear_history_retry(runtime_state)

    _enforce_history_buffer_limits(runtime_state, current_monotonic)
    pending = runtime_state.get("history_pending")
    has_pending = isinstance(pending, list) and bool(pending)
    has_inflight = isinstance(runtime_state.get("history_inflight"), dict)
    if not has_pending and not has_inflight:
        runtime_state.pop("history_pending", None)
        _clear_history_retry(runtime_state)
        return False
    if not force and not _history_flush_ready(runtime_state, current_monotonic):
        return False

    target_dir = HISTORY_DIR if history_dir is None else Path(history_dir)
    wrote = False
    try:
        if has_inflight:
            wrote = _reconcile_history_inflight(runtime_state) or wrote

        pending = runtime_state.get("history_pending")
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in pending if isinstance(pending, list) else []:
            groups.setdefault(str(item["day"]), []).append(item)

        for day, entries in groups.items():
            payload = "".join(str(item["line"]) + "\n" for item in entries).encode("utf-8")
            try:
                _append_history_payload(target_dir / f"{day}.jsonl", payload)
            except HistoryAppendIndeterminateError as exc:
                _store_history_inflight(
                    runtime_state,
                    exc.contract,
                    day=day,
                    entries=entries,
                )
                raise
            entry_ids = {id(item) for item in entries}
            pending[:] = [item for item in pending if id(item) not in entry_ids]
            wrote = True
    except Exception as exc:
        _schedule_history_retry(runtime_state, exc, current_monotonic, kind="io")
        raise

    pending = runtime_state.get("history_pending")
    if not isinstance(pending, list) or not pending:
        runtime_state.pop("history_pending", None)
    _clear_history_retry(runtime_state)
    return wrote


def append_history(
    status: dict[str, Any],
    runtime_state: dict[str, Any],
    cfg: dict[str, Any],
    history_dir: Path | None = None,
    *,
    monotonic_ts: float | None = None,
) -> None:
    current_monotonic = float(time.monotonic() if monotonic_ts is None else monotonic_ts)
    if cfg.get("_config_error"):
        exc = RuntimeError("Klima-Historie: Konfiguration nicht lesbar; Puffer bleibt erhalten")
        _schedule_history_retry(
            runtime_state,
            exc,
            current_monotonic,
            kind="config",
            attempted=False,
        )
        raise exc
    policy = _resolve_history_policy(runtime_state, cfg, force=False)
    if not policy["climate_enable"] or not policy["climate_history_enable"]:
        return
    if runtime_state.get("history_retry_kind") == "config":
        _clear_history_retry(runtime_state)

    interval_s = _safe_int(cfg.get("climate_history_interval_s"), 60, min_value=15, max_value=3600)
    last_monotonic = runtime_state.get("last_history_monotonic")
    sample_due = bool(
        last_monotonic is None
        or current_monotonic < float(last_monotonic)
        or current_monotonic - float(last_monotonic) >= interval_s
    )
    if sample_due:
        now_ts = _safe_int(status.get("ts"), int(time.time()))
        row = {
            "ts": status.get("ts_iso"),
            "power_w": status.get("power_w"),
            "active": status.get("active"),
            "daily_kwh": status.get("daily_kwh"),
            "energy_total_kwh": status.get("energy_total_kwh"),
            "phase": status.get("phase"),
            "source": status.get("source"),
            "name": status.get("name"),
        }
        runtime_state.setdefault("history_pending", []).append({
            "day": datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d"),
            "line": json.dumps(row, ensure_ascii=False, sort_keys=True),
            "monotonic_ts": current_monotonic,
        })
        runtime_state["last_history_monotonic"] = current_monotonic
        runtime_state["last_history_ts"] = now_ts
        _enforce_history_buffer_limits(runtime_state, current_monotonic)

    flush_history_buffer(
        runtime_state,
        cfg,
        history_dir,
        monotonic_ts=current_monotonic,
    )


def _apply_history_diagnostics(
    status: dict[str, Any],
    runtime_state: dict[str, Any],
    *,
    monotonic_ts: float | None = None,
) -> None:
    status.pop("history_error", None)
    now_value = float(time.monotonic() if monotonic_ts is None else monotonic_ts)
    pending = runtime_state.get("history_pending")
    pending_rows = len(pending) if isinstance(pending, list) else 0
    inflight = runtime_state.get("history_inflight")
    inflight_rows = int(inflight.get("row_count", 0) or 0) if isinstance(inflight, dict) else 0
    status["history_pending_rows"] = pending_rows + inflight_rows
    status["history_retry_attempts"] = int(runtime_state.get("history_retry_attempts", 0) or 0)
    next_retry = _safe_float(runtime_state.get("history_retry_next_monotonic"), 0.0)
    status["history_retry_in_s"] = round(max(0.0, next_retry - now_value), 1) if next_retry else 0.0
    status["history_reconcile_pending"] = isinstance(inflight, dict)
    dropped = int(runtime_state.get("history_overflow_dropped_rows", 0) or 0)
    status["history_overflow_dropped_rows"] = dropped
    status["history_integrity_degraded"] = bool(dropped > 0 or isinstance(inflight, dict))
    active_error = str(runtime_state.get("history_last_error", "") or "")
    overflow_error = str(runtime_state.get("history_overflow_last_error", "") or "")
    if active_error or overflow_error:
        status["history_error"] = active_error or overflow_error


def _history_has_unflushed_data(runtime_state: dict[str, Any]) -> bool:
    pending = runtime_state.get("history_pending")
    return bool(
        (isinstance(pending, list) and pending)
        or isinstance(runtime_state.get("history_inflight"), dict)
    )


def collect_status(cfg: dict[str, Any]) -> dict[str, Any]:
    if not cfg_bool(cfg, "climate_enable", False):
        return disabled_status(cfg)
    ip = cfg_text(cfg, "climate_meter_ip", "")
    if not valid_ip_config(ip):
        return error_status(cfg, "climate_meter_ip fehlt")
    meter_type = normalize_meter_type(cfg_text(cfg, "climate_meter_type", "shelly_pro3em"))
    if meter_type not in {"auto", "shelly_pro3em", "shelly_em_gen1", "shelly_em_mini_gen4", "shelly_pm_mini"}:
        return error_status(cfg, f"Nicht unterstuetzter Zaehler-Typ: {meter_type}")

    if meter_type == "shelly_em_gen1":
        payload = read_shelly_em_gen1(ip)
    elif meter_type == "shelly_em_mini_gen4":
        payload = read_shelly_em_mini_gen4(ip)
    elif meter_type == "auto":
        try:
            payload = read_shelly_status(ip)
        except urllib.error.HTTPError as exc:
            if exc.code not in {400, 404, 405, 501}:
                raise
            payload = read_shelly_em_gen1(ip)
        else:
            if not _has_supported_rpc_meter(payload):
                payload = read_shelly_em_gen1(ip)
    else:
        payload = read_shelly_status(ip)
    status = parse_shelly_status(payload, cfg)
    return update_daily_energy(status)


def run_once(config_path: Path = CONFIG_FILE, output_path: Path = RAMDISK_FILE, runtime_state: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = load_config(config_path)
    runtime_state = runtime_state if runtime_state is not None else {}
    try:
        status = collect_status(cfg)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        status = error_status(cfg, exc)
    write_json_atomic(output_path, status)
    history_error = ""
    try:
        append_history(status, runtime_state, cfg)
    except Exception as exc:
        history_error = str(exc)
    _apply_history_diagnostics(status, runtime_state)
    if history_error:
        status["history_error"] = history_error
    write_json_atomic(output_path, status)
    return status


def _handle_signal(_signum: int, _frame: Any) -> None:
    global RUNNING
    RUNNING = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Klimaanlagen-Verbrauch per lokalem Shelly-Zähler lesen.")
    parser.add_argument("--once", action="store_true", help="Nur einen Zyklus ausführen.")
    parser.add_argument("--config", default=str(CONFIG_FILE), help="Pfad zur e3dc_v4.json.")
    parser.add_argument("--output", default=str(RAMDISK_FILE), help="Zielpfad fuer climate_load.json.")
    parser.add_argument("--print", action="store_true", dest="print_status", help="Status auf stdout ausgeben.")
    args = parser.parse_args(argv)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    config_path = Path(args.config)
    output_path = Path(args.output)
    runtime_state: dict[str, Any] = {}

    status: dict[str, Any] = {}
    exit_code = 0
    try:
        while RUNNING:
            status = run_once(config_path, output_path, runtime_state)
            if args.print_status:
                print(json.dumps(status, ensure_ascii=False, sort_keys=True))
            if args.once:
                exit_code = 0 if status.get("success") else 1
                break
            cfg = load_config(config_path)
            poll_s = _safe_int(cfg.get("climate_poll_s"), 15, min_value=5, max_value=300)
            for _ in range(poll_s):
                if not RUNNING:
                    break
                time.sleep(1)
                if not RUNNING:
                    break
                current_monotonic = time.monotonic()
                if _history_flush_ready(runtime_state, current_monotonic):
                    current_cfg = load_config(config_path)
                    history_error = ""
                    try:
                        flush_history_buffer(
                            runtime_state,
                            current_cfg,
                            monotonic_ts=current_monotonic,
                        )
                    except Exception as exc:
                        history_error = str(exc)
                    _apply_history_diagnostics(
                        status,
                        runtime_state,
                        monotonic_ts=current_monotonic,
                    )
                    if history_error:
                        status["history_error"] = history_error
                    write_json_atomic(output_path, status)
    finally:
        cfg = load_config(config_path)
        shutdown_error = ""
        try:
            flush_history_buffer(runtime_state, cfg, force=True)
        except Exception as exc:
            shutdown_error = str(exc)
        if _history_has_unflushed_data(runtime_state):
            if not shutdown_error:
                shutdown_error = "Klima-Historie: Beim Beenden blieben ungefluschte Sätze zurück"
        if shutdown_error:
            exit_code = 1
            print(f"Klima-Historie konnte beim Beenden nicht geschrieben werden: {shutdown_error}", file=sys.stderr)
        if status:
            _apply_history_diagnostics(status, runtime_state)
            if shutdown_error:
                status["history_error"] = shutdown_error
            try:
                write_json_atomic(output_path, status)
            except Exception:
                pass
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
