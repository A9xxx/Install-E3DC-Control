#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only Dimplex WPM Touch / NWPM Modbus live service.

The active SG-Ready write path lives in luxtronik/energy_manager.py. This
service only reads the WPM Touch registers and publishes the common
waermepumpe.json contract for dashboard, history and diagnostics.
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import sys
import time
from typing import Any

from pymodbus.client import ModbusTcpClient

INSTALLER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if INSTALLER_DIR not in sys.path:
    sys.path.insert(0, INSTALLER_DIR)
from runtime_logging import configure_service_logger


RAMDISK_FILE = "/var/www/html/ramdisk/waermepumpe.json"
DIMPLEX_FILE = "/var/www/html/ramdisk/dimplex_wpm.json"
LOG_FILE = "/var/www/html/logs/dimplex_live.log"
V4_CONFIG_PATH = "/var/www/html/data/e3dc_v4.json"
DEFAULT_PORT = 502
DEFAULT_UNIT_ID = 1
DEFAULT_SG_REGISTER = 5167
DEFAULT_OUTDOOR_REGISTER = 1
DEFAULT_DHW_REGISTER = 3
DEFAULT_RETURN_REGISTER = 2
DEFAULT_FLOW_REGISTER = 5
DEFAULT_HEAT_SOURCE_IN_REGISTER = 6
DEFAULT_HEAT_SOURCE_OUT_REGISTER = 7
DEFAULT_COOLING_FLOW_REGISTER = 19
DEFAULT_COOLING_RETURN_REGISTER = 20
DEFAULT_COOLING_PRIMARY_RETURN_REGISTER = 21
DEFAULT_RETURN_SETPOINT_REGISTER = 53
DEFAULT_DHW_SETPOINT_REGISTER = 58
DEFAULT_OPERATING_MODE_REGISTER = 5015
DEFAULT_HEAT_POWER_REGISTER = 5168
DEFAULT_ELECTRIC_POWER_REGISTER = 5170
DEFAULT_HEARTBEAT_OUT_REGISTER = 5064
DEFAULT_SW_VERSION_REGISTER = 65
DEFAULT_SW_NUMBER_REGISTER = 66
DEFAULT_SW_INDEX_REGISTER = 67
DEFAULT_COP_ESTIMATE = 3.0

SG_STATES = {
    0: ("Gelb", "Hardwareeingang / Zustand gelb"),
    10: ("Gelb", "Normalbetrieb"),
    11: ("Grün", "Smart Grid Anhebung"),
    12: ("Rot", "EVU-Sperre"),
    13: ("Dunkelgrün", "Maximale Anhebung inkl. elektrischer Wärmeerzeuger"),
}

OPERATING_MODES = {
    0: "Sommer",
    1: "Winter",
    2: "Urlaub",
    3: "Party",
    4: "2. Wärmeerzeuger",
    5: "Kühlen",
}

_stop = False
_last_log_by_key: dict[str, float] = {}
_event_logger = configure_service_logger(
    "DimplexLive",
    log_path=LOG_FILE,
    max_bytes=1024 * 1024,
    backup_count=2,
    quiet_interval_s=0.0,
    warning_min_interval_s=0.0,
)


def _sig(_sig_num, _frame):
    global _stop
    _stop = True
    log_event("SIGTERM empfangen, beende Dimplex Live sauber.", key="sigterm", interval_s=0)
    sys.exit(0)


signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)


def log_event(message: str, key: str | None = None, interval_s: float = 300.0) -> None:
    now = time.time()
    if key:
        last = _last_log_by_key.get(key, 0.0)
        if now - last < interval_s:
            return
        _last_log_by_key[key] = now
    _event_logger.info(str(message), extra={"e3dc_no_throttle": True})


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, "", "none", "null"):
            return int(default)
        return int(float(str(value).strip().replace(",", ".")))
    except Exception:
        return int(default)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "none", "null"):
            return float(default)
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return float(default)


def valid_ip(value: Any) -> bool:
    return str(value or "").strip().lower() not in ("", "0", "0.0.0.0", "none", "null")


def cfg_enabled(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "ja", "ein")


def signed16(value: int) -> int:
    value = int(value) & 0xFFFF
    return value if value < 32768 else value - 65536


def scale_temperature(raw_value: int | None, scale: Any = "auto") -> float | None:
    if raw_value is None:
        return None
    signed = signed16(raw_value)
    scale_text = str(scale or "auto").strip().lower().replace(",", ".")
    try:
        if scale_text not in ("", "auto"):
            factor = float(scale_text)
            value = signed * factor
            return round(value, 1) if math.isfinite(value) else None
    except Exception:
        pass

    direct = float(signed)
    if -60.0 <= direct <= 100.0:
        return round(direct, 1)
    tenth = signed / 10.0
    if -60.0 <= tenth <= 100.0:
        return round(tenth, 1)
    return round(direct, 1)


def scale_dimplex_register_temperature(raw_value: int | None, scale: Any = "auto") -> float | None:
    scale_text = str(scale or "auto").strip().lower().replace(",", ".")
    if scale_text not in ("", "auto"):
        return scale_temperature(raw_value, scale)
    if raw_value is None:
        return None
    signed = signed16(raw_value)
    tenth = signed / 10.0
    if -60.0 <= tenth <= 100.0:
        return round(tenth, 1)
    return scale_temperature(raw_value, scale)


def scale_power_w(raw_value: int | None) -> int | None:
    if raw_value is None:
        return None
    return int(signed16(raw_value) * 10)


def pdu_address(doc_address: Any, zero_based: Any = False) -> int:
    address = safe_int(doc_address, 0)
    if cfg_enabled(zero_based):
        return max(0, address)
    return max(0, address - 1)


def parse_wpm_software(value: Any) -> tuple[str, int, int] | None:
    text = str(value or "").strip().upper().replace(" ", "")
    if not text:
        return None
    match = re.match(r"^([A-Z])?(\d+)(?:[.,](\d+))?$", text)
    if not match:
        return None
    family = match.group(1) or "M"
    return family, int(match.group(2)), int(match.group(3) or 0)


def dimplex_power_registers_supported(software_text: Any) -> bool | None:
    """Return False only when the known M-version is older than M3.5."""
    parsed = parse_wpm_software(software_text)
    if not parsed:
        return None
    family, number, index = parsed
    if family != "M":
        return None
    return (number, index) >= (3, 5)


def decode_wpm_software(version_raw: int | None, number_raw: int | None, index_raw: int | None) -> tuple[str | None, dict[str, Any]]:
    raw = {
        "version": version_raw,
        "number": number_raw,
        "index": index_raw,
        "version_register": DEFAULT_SW_VERSION_REGISTER,
        "number_register": DEFAULT_SW_NUMBER_REGISTER,
        "index_register": DEFAULT_SW_INDEX_REGISTER,
    }
    if version_raw is None or number_raw is None or index_raw is None:
        return None, raw
    version = safe_int(version_raw, -1)
    number = safe_int(number_raw, -1)
    index = safe_int(index_raw, -1)
    if not (1 <= version <= 26 and number >= 0 and index >= 0):
        return None, raw
    family = chr(ord("A") + version - 1)
    raw.update({"family": family, "decoded": f"{family}{number}.{index}"})
    return raw["decoded"], raw


def modbus_call(client: Any, method_name: str, unit_id: int, **kwargs):
    method = getattr(client, method_name)
    for unit_kw in ("slave", "unit", None):
        call_kwargs = dict(kwargs)
        if unit_kw:
            call_kwargs[unit_kw] = unit_id
        try:
            return method(**call_kwargs)
        except TypeError:
            if unit_kw is None:
                raise
            continue


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if os.path.exists(V4_CONFIG_PATH):
        try:
            with open(V4_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as exc:
            log_event(f"V4-Konfiguration konnte nicht gelesen werden: {exc}", key="config_error", interval_s=60)
    return cfg if isinstance(cfg, dict) else {}


def resolve_target(cfg: dict[str, Any]) -> tuple[str, int, int, dict[str, Any]]:
    wp_type = safe_int(cfg.get("wp_type"), -1)
    ip = str(cfg.get("dimplex_ip") or "0.0.0.0").strip()
    port = safe_int(cfg.get("dimplex_port"), DEFAULT_PORT)
    unit_id = safe_int(cfg.get("dimplex_unit_id"), DEFAULT_UNIT_ID)
    if wp_type != 5 or not cfg_enabled(cfg.get("luxtronik", "0")) or not valid_ip(ip):
        return "0.0.0.0", DEFAULT_PORT, DEFAULT_UNIT_ID, cfg
    return ip, port, unit_id, cfg


def read_one_register(client: Any, address: int, unit_id: int) -> int | None:
    result = modbus_call(client, "read_holding_registers", unit_id, address=address, count=1)
    if result is None or (hasattr(result, "isError") and result.isError()):
        return None
    regs = getattr(result, "registers", None) or []
    return int(regs[0]) if regs else None


def read_optional_register(client: Any, address: int, unit_id: int) -> int | None:
    try:
        return read_one_register(client, address, unit_id)
    except Exception:
        return None


def read_wpm_software(client: Any, unit_id: int) -> tuple[str | None, dict[str, Any]]:
    version_raw = read_optional_register(client, pdu_address(DEFAULT_SW_VERSION_REGISTER, False), unit_id)
    number_raw = read_optional_register(client, pdu_address(DEFAULT_SW_NUMBER_REGISTER, False), unit_id)
    index_raw = read_optional_register(client, pdu_address(DEFAULT_SW_INDEX_REGISTER, False), unit_id)
    return decode_wpm_software(version_raw, number_raw, index_raw)


def read_temperature_register(
    client: Any,
    doc_register: Any,
    zero_based: Any,
    unit_id: int,
    temp_scale: Any,
) -> tuple[int | None, float | None]:
    raw = read_optional_register(client, pdu_address(doc_register, zero_based), unit_id)
    return raw, scale_dimplex_register_temperature(raw, temp_scale)


def plausible_temperature(value: float | None, *, allow_zero: bool = False, min_c: float = -30.0, max_c: float = 95.0) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    numeric = float(value)
    if not allow_zero and abs(numeric) < 0.05:
        return None
    if numeric < min_c or numeric > max_c:
        return None
    return round(numeric, 1)


def first_plausible_temperature(*values: float | None, allow_zero: bool = False) -> float | None:
    for value in values:
        candidate = plausible_temperature(value, allow_zero=allow_zero)
        if candidate is not None:
            return candidate
    return None


def sg_text(value: int | None) -> tuple[str, str]:
    if value is None:
        return "--", "Unbekannt"
    return SG_STATES.get(int(value), ("Rohwert", f"Dimplex-Rohwert {value} ist nicht als SG-Ready-Zustand gemappt"))


def using_default_power_registers(heat_register: Any, electric_register: Any, zero_based: Any) -> bool:
    heat = safe_int(heat_register, DEFAULT_HEAT_POWER_REGISTER)
    electric = safe_int(electric_register, DEFAULT_ELECTRIC_POWER_REGISTER)
    if cfg_enabled(zero_based):
        return heat == pdu_address(DEFAULT_HEAT_POWER_REGISTER, False) and electric == pdu_address(DEFAULT_ELECTRIC_POWER_REGISTER, False)
    return heat == DEFAULT_HEAT_POWER_REGISTER and electric == DEFAULT_ELECTRIC_POWER_REGISTER


def read_dimplex(client: Any, cfg: dict[str, Any], unit_id: int) -> dict[str, Any]:
    zero_based = cfg.get("dimplex_modbus_zero_based", "0")
    temp_scale = cfg.get("dimplex_temp_scale", "auto")
    sg_register = cfg.get("dimplex_sg_register", DEFAULT_SG_REGISTER)
    outdoor_register = cfg.get("dimplex_outdoor_register", DEFAULT_OUTDOOR_REGISTER)
    dhw_register = cfg.get("dimplex_dhw_register", DEFAULT_DHW_REGISTER)
    return_register = cfg.get("dimplex_return_register", DEFAULT_RETURN_REGISTER)
    flow_register = cfg.get("dimplex_flow_register", DEFAULT_FLOW_REGISTER)
    heat_source_in_register = cfg.get("dimplex_heat_source_in_register", DEFAULT_HEAT_SOURCE_IN_REGISTER)
    heat_source_out_register = cfg.get("dimplex_heat_source_out_register", DEFAULT_HEAT_SOURCE_OUT_REGISTER)
    cooling_flow_register = cfg.get("dimplex_cooling_flow_register", DEFAULT_COOLING_FLOW_REGISTER)
    cooling_return_register = cfg.get("dimplex_cooling_return_register", DEFAULT_COOLING_RETURN_REGISTER)
    cooling_primary_return_register = cfg.get("dimplex_cooling_primary_return_register", DEFAULT_COOLING_PRIMARY_RETURN_REGISTER)
    return_setpoint_register = cfg.get("dimplex_return_setpoint_register", DEFAULT_RETURN_SETPOINT_REGISTER)
    dhw_setpoint_register = cfg.get("dimplex_dhw_setpoint_register", DEFAULT_DHW_SETPOINT_REGISTER)
    operating_mode_register = cfg.get("dimplex_operating_mode_register", DEFAULT_OPERATING_MODE_REGISTER)
    heat_power_register = cfg.get("dimplex_heat_power_register", DEFAULT_HEAT_POWER_REGISTER)
    electric_power_register = cfg.get("dimplex_electric_power_register", DEFAULT_ELECTRIC_POWER_REGISTER)
    heartbeat_out_register = cfg.get("dimplex_heartbeat_out_register", DEFAULT_HEARTBEAT_OUT_REGISTER)
    cop_estimate = max(0.0, safe_float(cfg.get("dimplex_cop_estimate"), DEFAULT_COP_ESTIMATE))

    outdoor_raw = read_one_register(client, pdu_address(outdoor_register, zero_based), unit_id)
    dhw_raw = read_one_register(client, pdu_address(dhw_register, zero_based), unit_id)
    return_raw, return_c = read_temperature_register(client, return_register, zero_based, unit_id, temp_scale)
    flow_raw, flow_c = read_temperature_register(client, flow_register, zero_based, unit_id, temp_scale)
    heat_source_in_raw, heat_source_in_c = read_temperature_register(client, heat_source_in_register, zero_based, unit_id, temp_scale)
    heat_source_out_raw, heat_source_out_c = read_temperature_register(client, heat_source_out_register, zero_based, unit_id, temp_scale)
    cooling_flow_raw, cooling_flow_c = read_temperature_register(client, cooling_flow_register, zero_based, unit_id, temp_scale)
    cooling_return_raw, cooling_return_c = read_temperature_register(client, cooling_return_register, zero_based, unit_id, temp_scale)
    cooling_primary_return_raw, cooling_primary_return_c = read_temperature_register(client, cooling_primary_return_register, zero_based, unit_id, temp_scale)
    return_setpoint_raw, return_setpoint_c = read_temperature_register(client, return_setpoint_register, zero_based, unit_id, temp_scale)
    dhw_setpoint_raw, dhw_setpoint_c = read_temperature_register(client, dhw_setpoint_register, zero_based, unit_id, temp_scale)
    sg_raw = read_one_register(client, pdu_address(sg_register, zero_based), unit_id)
    operating_mode_raw = read_one_register(client, pdu_address(operating_mode_register, zero_based), unit_id)
    heat_power_raw = read_one_register(client, pdu_address(heat_power_register, zero_based), unit_id)
    electric_power_raw = read_one_register(client, pdu_address(electric_power_register, zero_based), unit_id)
    heartbeat_out_raw = read_one_register(client, pdu_address(heartbeat_out_register, zero_based), unit_id)
    detected_software, software_raw = read_wpm_software(client, unit_id)

    outdoor_c = scale_temperature(outdoor_raw, temp_scale)
    dhw_c = scale_temperature(dhw_raw, temp_scale)
    return_c = plausible_temperature(return_c, min_c=-5.0)
    flow_c = plausible_temperature(flow_c, min_c=-5.0)
    return_setpoint_c = plausible_temperature(return_setpoint_c, min_c=-5.0)
    dhw_setpoint_c = plausible_temperature(dhw_setpoint_c, min_c=0.0)
    heat_source_in_c = plausible_temperature(heat_source_in_c)
    heat_source_out_c = plausible_temperature(heat_source_out_c)
    cooling_flow_c = plausible_temperature(cooling_flow_c)
    cooling_return_c = plausible_temperature(cooling_return_c)
    cooling_primary_return_c = plausible_temperature(cooling_primary_return_c)
    cooling_storage_c = first_plausible_temperature(cooling_primary_return_c, cooling_return_c, cooling_flow_c)
    configured_software = str(cfg.get("dimplex_wpm_software") or "").strip()
    software_text = configured_software or detected_software
    power_supported = dimplex_power_registers_supported(software_text)
    heat_power_w = scale_power_w(heat_power_raw)
    electric_power_w = scale_power_w(electric_power_raw)
    raw_heat_power_w = heat_power_w
    heat_power_estimated = False
    heat_power_source = "modbus_register"
    heat_power_standby_suppressed = False
    power_note = None
    if power_supported is False and using_default_power_registers(heat_power_register, electric_power_register, zero_based):
        heat_power_w = None
        electric_power_w = None
        raw_heat_power_w = None
        power_note = f"Leistungsregister {DEFAULT_HEAT_POWER_REGISTER}/{DEFAULT_ELECTRIC_POWER_REGISTER} sind erst ab Dimplex M3.5 nutzbar; erkannt wurde {software_text}."
    elif electric_power_w is not None and electric_power_w >= 500 and cop_estimate > 0:
        raw_heat = float(raw_heat_power_w or 0)
        if heat_power_w is None or raw_heat < max(500.0, float(electric_power_w) * 0.25):
            heat_power_w = int(round(float(electric_power_w) * cop_estimate))
            heat_power_estimated = True
            heat_power_source = "estimated_from_electric_cop"
            power_note = (
                "Wärmeleistung aus elektrischer Leistung und Dimplex-COP-Schätzung "
                f"{cop_estimate:.1f} berechnet; Rohregister {safe_int(heat_power_register, DEFAULT_HEAT_POWER_REGISTER)} "
                f"liefert {int(raw_heat)} W."
            )
    elif electric_power_w is not None and electric_power_w < 150 and heat_power_w is not None and heat_power_w <= 500:
        heat_power_w = 0
        heat_power_source = "standby_raw_suppressed"
        heat_power_standby_suppressed = True
    sg_color, sg_state = sg_text(sg_raw)
    sg_known = sg_raw is not None and int(sg_raw) in SG_STATES
    sg_readback_ts = time.time() if sg_known else 0.0
    sg_note = None if sg_known or sg_raw is None else f"Rohwert {sg_raw} liegt außerhalb der gemappten SG-Ready-Werte 0/10/11/12/13."
    sg_value = safe_int(sg_raw, 0)
    sg_boost_active = sg_value in (11, 13)
    operating_mode = safe_int(operating_mode_raw, -1)
    operating_mode_text = OPERATING_MODES.get(operating_mode, f"Unbekannt {operating_mode}" if operating_mode >= 0 else "--")
    compressor_on = 1 if float(electric_power_w or 0) >= 150.0 else 0

    data: dict[str, Any] = {
        "Hersteller": "Dimplex",
        "Quelle": "dimplex_live",
        "success": True,
        "source": "dimplex_live",
        "Außentemperatur": outdoor_c,
        "Aussentemp": outdoor_c,
        "Warmwasser-Ist": dhw_c,
        "Warmwasser_Ist": dhw_c,
        "Warmwasser-Soll": dhw_setpoint_c,
        "Warmwasser_Soll": dhw_setpoint_c,
        "Ruecklauf_Ist": return_c,
        "Ruecklauf": return_c,
        "Ruecklauf_Soll": return_setpoint_c,
        "Ruecklauf-Soll": return_setpoint_c,
        "Vorlauf_Ist": flow_c,
        "Vorlauf": flow_c,
        "Sole_Ein": heat_source_in_c,
        "Waermequelle_Ein": heat_source_in_c,
        "Sole_Aus": heat_source_out_c,
        "Waermequelle_Aus": heat_source_out_c,
        "Kaeltespeicher_Ist": cooling_storage_c,
        "Kaeltespeicher_Temp": cooling_storage_c,
        "Kuehlkreis_Vorlauf": cooling_flow_c,
        "Kuehlkreis_Ruecklauf": cooling_return_c,
        "Kuehlkreis_Primaer_Ruecklauf": cooling_primary_return_c,
        "Betriebszustand": sg_state,
        "Betriebsmodus": operating_mode_text,
        "Betriebsmodus_Code": operating_mode if operating_mode >= 0 else None,
        "Leistung_Heiz_kW": round(heat_power_w / 1000.0, 3) if heat_power_w is not None else None,
        "Leistung_Verdichter_W": max(0, electric_power_w) if electric_power_w is not None else None,
        "Leistungsaufnahme": round(max(0, electric_power_w) / 1000.0, 3) if electric_power_w is not None else None,
        "Verdichter_Ein": compressor_on,
        "Modus Heizen": "Aus",
        "Modus Warmw.": "Aus",
        "dimplex_sg_register": safe_int(sg_register, DEFAULT_SG_REGISTER),
        "dimplex_sg_address": pdu_address(sg_register, zero_based),
        "dimplex_sg_value": sg_raw,
        "dimplex_sg_color": sg_color,
        "dimplex_sg_state": sg_state,
        "dimplex_sg_active": sg_boost_active,
        "dimplex_sg_readback_state": int(sg_raw) if sg_known else None,
        "dimplex_sg_readback_ts": sg_readback_ts,
        "dimplex_sg_readback_source": "dimplex_modbus_live_readback" if sg_known else "",
        "dimplex_sg_readback_confirmed": bool(sg_known),
        "dimplex_operating_mode": operating_mode if operating_mode >= 0 else None,
        "dimplex_operating_mode_text": operating_mode_text,
        "dimplex_heat_power_w": heat_power_w,
        "dimplex_electric_power_w": electric_power_w,
        "dimplex_heat_power_raw_w": raw_heat_power_w,
        "dimplex_heat_power_estimated": heat_power_estimated,
        "dimplex_heat_power_source": heat_power_source,
        "dimplex_heat_power_standby_suppressed": heat_power_standby_suppressed,
        "dimplex_cop_estimate": cop_estimate,
        "dimplex_heat_power_register": safe_int(heat_power_register, DEFAULT_HEAT_POWER_REGISTER),
        "dimplex_heat_power_address": pdu_address(heat_power_register, zero_based),
        "dimplex_electric_power_register": safe_int(electric_power_register, DEFAULT_ELECTRIC_POWER_REGISTER),
        "dimplex_electric_power_address": pdu_address(electric_power_register, zero_based),
        "dimplex_power_registers_supported": power_supported,
        "dimplex_power_note": power_note,
        "dimplex_return_register": safe_int(return_register, DEFAULT_RETURN_REGISTER),
        "dimplex_return_address": pdu_address(return_register, zero_based),
        "dimplex_return_raw": return_raw,
        "dimplex_flow_register": safe_int(flow_register, DEFAULT_FLOW_REGISTER),
        "dimplex_flow_address": pdu_address(flow_register, zero_based),
        "dimplex_flow_raw": flow_raw,
        "dimplex_return_setpoint_register": safe_int(return_setpoint_register, DEFAULT_RETURN_SETPOINT_REGISTER),
        "dimplex_return_setpoint_address": pdu_address(return_setpoint_register, zero_based),
        "dimplex_return_setpoint_raw": return_setpoint_raw,
        "dimplex_dhw_setpoint_register": safe_int(dhw_setpoint_register, DEFAULT_DHW_SETPOINT_REGISTER),
        "dimplex_dhw_setpoint_address": pdu_address(dhw_setpoint_register, zero_based),
        "dimplex_dhw_setpoint_raw": dhw_setpoint_raw,
        "dimplex_heat_source_in_register": safe_int(heat_source_in_register, DEFAULT_HEAT_SOURCE_IN_REGISTER),
        "dimplex_heat_source_in_address": pdu_address(heat_source_in_register, zero_based),
        "dimplex_heat_source_in_raw": heat_source_in_raw,
        "dimplex_heat_source_out_register": safe_int(heat_source_out_register, DEFAULT_HEAT_SOURCE_OUT_REGISTER),
        "dimplex_heat_source_out_address": pdu_address(heat_source_out_register, zero_based),
        "dimplex_heat_source_out_raw": heat_source_out_raw,
        "dimplex_cooling_flow_register": safe_int(cooling_flow_register, DEFAULT_COOLING_FLOW_REGISTER),
        "dimplex_cooling_flow_address": pdu_address(cooling_flow_register, zero_based),
        "dimplex_cooling_flow_raw": cooling_flow_raw,
        "dimplex_cooling_return_register": safe_int(cooling_return_register, DEFAULT_COOLING_RETURN_REGISTER),
        "dimplex_cooling_return_address": pdu_address(cooling_return_register, zero_based),
        "dimplex_cooling_return_raw": cooling_return_raw,
        "dimplex_cooling_primary_return_register": safe_int(cooling_primary_return_register, DEFAULT_COOLING_PRIMARY_RETURN_REGISTER),
        "dimplex_cooling_primary_return_address": pdu_address(cooling_primary_return_register, zero_based),
        "dimplex_cooling_primary_return_raw": cooling_primary_return_raw,
        "dimplex_heartbeat_out": heartbeat_out_raw,
        "dimplex_wpm_software": software_text,
        "dimplex_wpm_software_auto": detected_software,
        "dimplex_wpm_software_raw": software_raw,
        "dimplex_sg_known": sg_known,
        "dimplex_sg_note": sg_note,
        "dimplex_outdoor_raw": outdoor_raw,
        "dimplex_dhw_raw": dhw_raw,
        "dimplex_heat_power_raw": heat_power_raw,
        "dimplex_electric_power_raw": electric_power_raw,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return data


def write_json(path: str, payload: dict[str, Any]) -> bool:
    tmp_file = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.chmod(tmp_file, 0o664)
        try:
            import grp

            os.chown(tmp_file, -1, grp.getgrnam("www-data").gr_gid)
        except Exception:
            pass
        os.replace(tmp_file, path)
        return True
    except Exception as exc:
        log_event(f"Fehler beim Schreiben von {path}: {exc}", key=f"write_error:{path}", interval_s=60)
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except Exception:
            pass
        return False


def save_payload(payload: dict[str, Any], write_wp: bool = True) -> None:
    wrapped = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "success": bool(payload.get("success", True)),
        "source": "dimplex_live",
        "data": payload,
        "status": {
            "SG_State": payload.get("dimplex_sg_state"),
            "SG_Color": payload.get("dimplex_sg_color"),
            "SG_Value": payload.get("dimplex_sg_value"),
            "SG_Active": payload.get("dimplex_sg_active"),
            "HZ_Mode": 1 if payload.get("Modus Heizen") == "Ein" else 0,
            "WW_Mode": 1 if payload.get("Modus Warmw.") == "Ein" else 0,
        },
        "dimplex": {
            "write_enabled": False,
            "sg_register": payload.get("dimplex_sg_register"),
            "sg_address": payload.get("dimplex_sg_address"),
            "sg_value": payload.get("dimplex_sg_value"),
            "sg_known": payload.get("dimplex_sg_known"),
            "sg_active": payload.get("dimplex_sg_active"),
            "wpm_software": payload.get("dimplex_wpm_software"),
            "power_registers_supported": payload.get("dimplex_power_registers_supported"),
        },
    }
    write_json(DIMPLEX_FILE, wrapped)
    if write_wp:
        write_json(RAMDISK_FILE, wrapped)


def save_error(message: str, cfg: dict[str, Any] | None = None, write_wp: bool = False) -> None:
    cfg = cfg or {}
    payload = {
        "success": False,
        "error": message,
        "source": "dimplex_live",
        "Hersteller": "Dimplex",
        "Quelle": "dimplex_live",
        "dimplex_ip": cfg.get("dimplex_ip", ""),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_payload(payload, write_wp=write_wp)


def main() -> None:
    cfg = load_config()
    ip, port, unit_id, cfg = resolve_target(cfg)
    if ip == "0.0.0.0":
        log_event("Dimplex ist nicht aktiviert oder dimplex_ip fehlt.", key="inactive", interval_s=60)
        save_error("Dimplex ist nicht aktiviert (wp_type=5, WP-/Verbrauchslogging und dimplex_ip erforderlich).", cfg)
        return

    log_event(f"Starte Dimplex WPM Touch Live zu {ip}:{port} (Unit {unit_id}).", key="startup", interval_s=60)
    while True:
        client = ModbusTcpClient(ip, port=port)
        try:
            connected = False
            for _ in range(3):
                if _stop:
                    break
                if client.connect():
                    connected = True
                    break
                time.sleep(1)
            if _stop:
                break
            if not connected:
                log_event("Dimplex Modbus-Verbindung fehlgeschlagen.", key="connect_failed", interval_s=60)
                save_error("Verbindung fehlgeschlagen", cfg, write_wp=True)
            else:
                payload = read_dimplex(client, cfg, unit_id)
                save_payload(payload)
                log_event(
                    f"Dimplex Livewerte aktualisiert (SG {payload.get('dimplex_sg_value')} {payload.get('dimplex_sg_color')}).",
                    key="success",
                    interval_s=900,
                )
        except Exception as exc:
            log_event(f"Fehler im Dimplex Loop: {exc}", key="loop_error", interval_s=60)
            save_error(str(exc), cfg, write_wp=True)
        finally:
            try:
                client.close()
            except Exception:
                pass

        for _ in range(15):
            if _stop:
                return
            time.sleep(1)


if __name__ == "__main__":
    main()
