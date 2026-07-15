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
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
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
    }
    raw = aliases.get(raw, raw)
    return raw if raw in {"a", "b", "c", "total", "single"} else "c"


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
    raise ValueError(f"Nicht unterstuetzter Zaehler-Typ: {meter_type}")


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
        "power_w": 0,
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
    if entry.get("date") != today or baseline is None or total_kwh + 0.001 < baseline:
        baseline = total_kwh
        entry = {
            "date": today,
            "baseline_total_kwh": round(baseline, 6),
            "source": status.get("source"),
            "ip": status.get("ip"),
            "phase": status.get("phase"),
        }

    daily = max(0.0, total_kwh - baseline)
    entry["last_total_kwh"] = round(total_kwh, 6)
    entry["last_seen_ts"] = status.get("ts")
    state[key] = entry

    try:
        write_json_atomic(baseline_file, state)
    except Exception as exc:
        status["daily_baseline_error"] = str(exc)

    status["daily_kwh"] = round(daily, 3)
    status["energy_baseline_kwh"] = round(baseline, 6)
    return status


def append_history(status: dict[str, Any], runtime_state: dict[str, Any], cfg: dict[str, Any], history_dir: Path = HISTORY_DIR) -> None:
    if not cfg_bool(cfg, "climate_history_enable", True):
        return
    interval_s = _safe_int(cfg.get("climate_history_interval_s"), 60, min_value=15, max_value=3600)
    now_ts = _safe_int(status.get("ts"), int(time.time()))
    if now_ts - int(runtime_state.get("last_history_ts", 0)) < interval_s:
        return

    day = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d")
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / f"{day}.jsonl"
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
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    try:
        os.chmod(path, 0o664)
    except OSError:
        pass
    runtime_state["last_history_ts"] = now_ts


def collect_status(cfg: dict[str, Any]) -> dict[str, Any]:
    if not cfg_bool(cfg, "climate_enable", False):
        return disabled_status(cfg)
    ip = cfg_text(cfg, "climate_meter_ip", "")
    if not valid_ip_config(ip):
        return error_status(cfg, "climate_meter_ip fehlt")
    meter_type = normalize_meter_type(cfg_text(cfg, "climate_meter_type", "shelly_pro3em"))
    if meter_type not in {"auto", "shelly_pro3em", "shelly_em_mini_gen4", "shelly_pm_mini"}:
        return error_status(cfg, f"Nicht unterstuetzter Zaehler-Typ: {meter_type}")

    if meter_type == "shelly_em_mini_gen4":
        payload = read_shelly_em_mini_gen4(ip)
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
    try:
        append_history(status, runtime_state, cfg)
    except Exception as exc:
        status["history_error"] = str(exc)
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

    while RUNNING:
        status = run_once(config_path, output_path, runtime_state)
        if args.print_status:
            print(json.dumps(status, ensure_ascii=False, sort_keys=True))
        if args.once:
            return 0 if status.get("success") else 1
        cfg = load_config(config_path)
        poll_s = _safe_int(cfg.get("climate_poll_s"), 15, min_value=5, max_value=300)
        for _ in range(poll_s):
            if not RUNNING:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
