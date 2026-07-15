#!/usr/bin/env python3
"""Read-only Stiebel Eltron ISG/WPM live service.

The service writes the existing heatpump live contract to waermepumpe.json and
keeps a Stiebel-specific copy in stiebel_isg.json. It never writes SG-Ready or
temperature registers; active control stays in the Energy Manager behind its
own opt-in.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import signal
import socket
import struct
import sys
import time
import urllib.parse
import urllib.request
from typing import Any


CONFIG_FILE = "/var/www/html/data/e3dc_v4.json"
RAMDISK_DIR = "/var/www/html/ramdisk"
WP_FILE = os.path.join(RAMDISK_DIR, "waermepumpe.json")
STIEBEL_FILE = os.path.join(RAMDISK_DIR, "stiebel_isg.json")
LOG_PREFIX = "Stiebel ISG Live"
POLL_INTERVAL_S = 30
HZ_BACKOFF_AFTER_ERRORS = 3
HZ_BACKOFF_S = 1800
HZ_ERROR_LOG_INTERVAL_S = 900

WPM_OPERATING_MODE_TEXT = {
    0: "Notbetrieb",
    1: "Bereitschaft",
    2: "Programmbetrieb",
    3: "Komfortbetrieb",
    4: "Eco-Betrieb",
    5: "Warmwasserbetrieb",
}

WPM_STATUS_HEATING = 1 << 3
WPM_STATUS_DHW = 1 << 4
WPM_STATUS_COMPRESSOR = 1 << 5
WPM_STATUS_SUMMER = 1 << 6
WPM_STATUS_COOLING = 1 << 7

_stop = False
_web_opener: urllib.request.OpenerDirector | None = None
_web_login_until = 0.0
_last_hz_error_log = 0.0
_last_hz_backoff_log = 0.0
_hz_error_count = 0
_hz_backoff_until = 0.0


def _sigterm(_signum, _frame):
    global _stop
    _stop = True
    print(f"[{time.strftime('%H:%M:%S')}] SIGTERM empfangen, beende {LOG_PREFIX} sauber...", flush=True)


signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT, _sigterm)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "---"):
            return default
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, "", "---"):
            return default
        return int(float(str(value).strip().replace(",", ".")))
    except Exception:
        return default


def cfg_enabled(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on", "ein")


def status_bit(value: int, mask: int) -> bool:
    return int(value or 0) & int(mask) != 0


def has_address(value: Any) -> bool:
    return str(value or "").strip().lower() not in ("", "0", "0.0.0.0", "none", "null")


def signed16(value: int) -> int:
    return value if value < 32768 else value - 65536


def none_marker(value: int | None) -> bool:
    return value is None or value in (0x8000, 32768)


def plausible_temp(value: float) -> float | None:
    if value < -100.0 or value > 200.0:
        return None
    return round(value, 1)


def temp_01k(value: int | None) -> float | None:
    if none_marker(value):
        return None
    return plausible_temp(signed16(int(value)) / 10.0)


def temp_degc_or_01k(value: int | None) -> float | None:
    if none_marker(value):
        return None
    signed = signed16(int(value))
    if -50 <= signed <= 120:
        return plausible_temp(float(signed))
    return plausible_temp(signed / 10.0)


def uint_or_none(value: int | None) -> int | None:
    if none_marker(value):
        return None
    return int(value)


def read_config() -> dict[str, Any]:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_power_map(spec: Any) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for part in re.split(r"[;,]", str(spec or "")):
        item = part.strip()
        if not item:
            continue
        match = re.match(r"^\s*([0-9]+(?:[,.][0-9]+)?)\s*[:=]\s*([0-9]+(?:[,.][0-9]+)?)\s*$", item)
        if not match:
            continue
        points.append((safe_float(match.group(1)), safe_float(match.group(2))))
    return sorted(dict(points).items())


def interpolate(points: list[tuple[float, float]], x_value: float) -> float | None:
    if not points:
        return None
    if x_value <= points[0][0]:
        return points[0][1]
    if x_value >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x_value <= x1:
            if x1 == x0:
                return y1
            return y0 + ((x_value - x0) / (x1 - x0)) * (y1 - y0)
    return None


def estimate_from_percent(percent: float, mode_max_w: int, standby_w: int) -> int:
    percent = max(0.0, min(100.0, float(percent)))
    dynamic_w = max(0.0, float(mode_max_w) - float(standby_w))
    return int(round(float(standby_w) + (percent / 100.0) * dynamic_w))


def estimate_from_hz(hz: float, mode_max_w: int, standby_w: int, max_hz: float, power_map: list[tuple[float, float]]) -> int:
    mapped = interpolate(power_map, max(0.0, hz))
    if mapped is not None:
        return max(0, int(round(mapped)))
    percent = min(100.0, (max(0.0, hz) / max(1.0, max_hz)) * 100.0)
    return estimate_from_percent(percent, mode_max_w, standby_w)


def value_at(registers: list[int], base_address: int, address: int) -> int | None:
    index = int(address) - int(base_address)
    if 0 <= index < len(registers):
        return registers[index]
    return None


def code_address(doc_address: int) -> int:
    """Convert the 1-based Stiebel documentation address to the Modbus PDU address."""
    return int(doc_address) - 1


def value_at_doc(registers: list[int], base_doc_address: int, doc_address: int) -> int | None:
    return value_at(registers, code_address(base_doc_address), code_address(doc_address))


def temp_from_doc(registers: list[int], base_doc_address: int, *doc_addresses: int) -> float | None:
    for doc_address in doc_addresses:
        value = temp_01k(value_at_doc(registers, base_doc_address, doc_address))
        if value is not None:
            return value
    return None


def temp_from(registers: list[int], base_address: int, *addresses: int) -> float | None:
    for address in addresses:
        value = temp_01k(value_at(registers, base_address, address))
        if value is not None:
            return value
    return None


def read_regs(ip: str, port: int, unit_id: int, function: int, address: int, count: int) -> list[int]:
    tid = int(time.time() * 1000) & 0xFFFF
    pdu = struct.pack(">BHH", function, int(address), int(count))
    msg = struct.pack(">HHHB", tid, 0, len(pdu) + 1, int(unit_id)) + pdu
    with socket.create_connection((ip, int(port)), timeout=3.0) as sock:
        sock.settimeout(3.0)
        sock.sendall(msg)
        header = sock.recv(7)
        if len(header) != 7:
            raise RuntimeError("kurzer Modbus-Header")
        _, _, length, _ = struct.unpack(">HHHB", header)
        body = b""
        while len(body) < length - 1:
            chunk = sock.recv(length - 1 - len(body))
            if not chunk:
                break
            body += chunk
    if not body:
        raise RuntimeError("leere Modbus-Antwort")
    if body[0] & 0x80:
        code = body[1] if len(body) > 1 else "?"
        raise RuntimeError(f"Modbus Exception {code} an Adresse {address}")
    byte_count = body[1]
    return list(struct.unpack(">" + "H" * (byte_count // 2), body[2 : 2 + byte_count]))


def read_http_json(url: str, timeout: float = 3.0) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "E3DC-Control/5"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="ignore")
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def valid_power_meter_type(value: Any) -> str:
    meter_type = str(value or "auto").strip().lower().replace("-", "_")
    aliases = {
        "": "auto",
        "shelly": "auto",
        "shelly3em": "shelly_3em",
        "shelly_pro3em": "shelly_3em",
        "pro3em": "shelly_3em",
        "3em": "shelly_3em",
        "plug": "shelly_plug",
        "shelly_plus": "shelly_plug",
        "pm": "shelly_pm",
    }
    return aliases.get(meter_type, meter_type)


def normalise_meter_power(value: Any) -> float:
    power = safe_float(value, 0.0)
    return round(abs(power), 1)


def phase_power_value(value: int | None) -> float | None:
    if none_marker(value):
        return None
    power = signed16(int(value))
    if power < -1000 or power > 50000:
        return None
    return round(max(0.0, float(power)), 1)


def read_wpmg_phase_power(ip: str, port: int, unit_id: int) -> dict[str, Any]:
    """Try optional WPMG direct electric phase-power registers, read-only."""
    blocks = [
        ("wpmg_phase_power_primary", 6118),
        ("wpmg_phase_power_secondary_1", 6268),
        ("wpmg_phase_power_secondary_2", 6418),
        ("wpmg_phase_power_secondary_3", 6568),
        ("wpmg_phase_power_secondary_4", 6718),
        ("wpmg_phase_power_secondary_5", 6868),
        ("wpmg_phase_power_system", 36118),
    ]
    for source, doc_l1 in blocks:
        try:
            regs = read_regs(ip, port, unit_id, 4, code_address(doc_l1), 3)
        except Exception:
            continue
        phases = [phase_power_value(regs[idx] if idx < len(regs) else None) for idx in range(3)]
        if any(value is None for value in phases):
            continue
        total = round(sum(float(value) for value in phases), 1)
        return {
            "power_w": total,
            "source": source,
            "phase_a_w": phases[0],
            "phase_b_w": phases[1],
            "phase_c_w": phases[2],
            "doc_l1": doc_l1,
            "code_l1": code_address(doc_l1),
        }
    return {}


def read_shelly_power_meter(ip: str, meter_type: str = "auto") -> dict[str, Any] | None:
    """Read a Shelly/Shelly Pro meter as external heat-pump power input."""
    meter_type = valid_power_meter_type(meter_type)
    try_3em = meter_type in ("auto", "shelly_3em")
    try_plug = meter_type in ("auto", "shelly_plug", "shelly_pm")

    if try_3em:
        try:
            data = read_http_json(f"http://{ip}/rpc/EM.GetStatus?id=0")
            total = data.get("total_act_power")
            if total is None:
                total = (
                    safe_float(data.get("a_act_power"), 0.0)
                    + safe_float(data.get("b_act_power"), 0.0)
                    + safe_float(data.get("c_act_power"), 0.0)
                )
            return {
                "power_w": normalise_meter_power(total),
                "raw_power_w": round(safe_float(total, 0.0), 1),
                "source": "shelly_3em_rpc",
                "phase_a_w": round(safe_float(data.get("a_act_power"), 0.0), 1),
                "phase_b_w": round(safe_float(data.get("b_act_power"), 0.0), 1),
                "phase_c_w": round(safe_float(data.get("c_act_power"), 0.0), 1),
                "voltage_a_v": round(safe_float(data.get("a_voltage"), 0.0), 1),
                "voltage_b_v": round(safe_float(data.get("b_voltage"), 0.0), 1),
                "voltage_c_v": round(safe_float(data.get("c_voltage"), 0.0), 1),
            }
        except Exception:
            pass

        try:
            data = read_http_json(f"http://{ip}/status")
            emeters = data.get("emeters")
            if isinstance(emeters, list) and emeters:
                phase_powers = [safe_float(item.get("power"), 0.0) for item in emeters if isinstance(item, dict)]
                total = sum(phase_powers)
                result = {
                    "power_w": normalise_meter_power(total),
                    "raw_power_w": round(total, 1),
                    "source": "shelly_3em_status",
                }
                for idx, phase in enumerate(("a", "b", "c")):
                    if idx < len(phase_powers):
                        result[f"phase_{phase}_w"] = round(phase_powers[idx], 1)
                return result
        except Exception:
            pass

    if try_plug:
        for path, source in (
            ("/rpc/Switch.GetStatus?id=0", "shelly_switch_rpc"),
            ("/rpc/PM1.GetStatus?id=0", "shelly_pm_rpc"),
            ("/meter/0", "shelly_meter"),
        ):
            try:
                data = read_http_json(f"http://{ip}{path}")
                power = data.get("apower", data.get("power", 0.0))
                return {
                    "power_w": normalise_meter_power(power),
                    "raw_power_w": round(safe_float(power, 0.0), 1),
                    "source": source,
                    "output": data.get("output", data.get("ison")),
                }
            except Exception:
                pass

    return None


def read_external_power_meter(cfg: dict[str, Any]) -> dict[str, Any]:
    if not cfg_enabled(cfg.get("stiebel_isg_power_meter_enable", "0")):
        return {}
    ip = str(cfg.get("stiebel_isg_power_meter_ip", "") or "").strip()
    if not has_address(ip):
        ip = str(cfg.get("shelly_3em_ip", "") or "").strip()
    if not has_address(ip):
        return {"error": "kein externer Stiebel-Leistungsmesser konfiguriert"}
    meter_type = str(cfg.get("stiebel_isg_power_meter_type", "auto") or "auto")
    try:
        data = read_shelly_power_meter(ip, meter_type)
        if data:
            data["ip"] = ip
            data["type"] = valid_power_meter_type(meter_type)
            return data
        return {"ip": ip, "type": valid_power_meter_type(meter_type), "error": "Shelly-Leistung nicht lesbar"}
    except Exception as exc:
        return {"ip": ip, "type": valid_power_meter_type(meter_type), "error": str(exc)}


def web_opener(ip: str, user: str, password: str) -> urllib.request.OpenerDirector:
    global _web_opener, _web_login_until
    if _web_opener is not None and time.time() < _web_login_until:
        return _web_opener

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    if user and password:
        login_data = urllib.parse.urlencode({"make": "send", "user": user, "pass": password}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{ip}/",
            data=login_data,
            headers={"User-Agent": "E3DC-Control/5", "Content-Type": "application/x-www-form-urlencoded"},
        )
        with opener.open(req, timeout=4.0) as response:
            response.read()

    _web_opener = opener
    _web_login_until = time.time() + 900.0
    return opener


def scrape_compressor_hz(cfg: dict[str, Any], ip: str) -> float | None:
    global _last_hz_error_log, _last_hz_backoff_log, _hz_error_count, _hz_backoff_until
    if not cfg_enabled(cfg.get("stiebel_isg_scrape_hz_enable", "0")):
        _hz_error_count = 0
        _hz_backoff_until = 0.0
        return None
    now = time.time()
    if now < _hz_backoff_until:
        if now - _last_hz_backoff_log >= HZ_ERROR_LOG_INTERVAL_S:
            _last_hz_backoff_log = now
            remaining_min = max(1, int(round((_hz_backoff_until - now) / 60.0)))
            print(
                f"[{time.strftime('%H:%M:%S')}] Prozessdaten-Hz pausiert noch ca. {remaining_min} min "
                "nach wiederholten Timeouts; Modbus/Shelly laufen weiter.",
                flush=True,
            )
        return None
    try:
        opener = web_opener(
            ip,
            str(cfg.get("stiebel_isg_web_user", "") or ""),
            str(cfg.get("stiebel_isg_web_password", "") or ""),
        )
        req = urllib.request.Request(f"http://{ip}/?s=1,1", headers={"User-Agent": "E3DC-Control/5"})
        with opener.open(req, timeout=4.0) as response:
            raw = response.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="ignore")
        for pattern in (
            r"ISTDREHZAHL\s+VERDICHTER.{0,700}?<td[^>]*>\s*([0-9]+(?:[,.][0-9]+)?)\s*Hz",
            r"Ist(?:drehzahl)?\s*Verdichter.{0,700}?([0-9]+(?:[,.][0-9]+)?)\s*Hz",
        ):
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                _hz_error_count = 0
                _hz_backoff_until = 0.0
                return max(0.0, safe_float(match.group(1), 0.0))
    except Exception as exc:
        now = time.time()
        _hz_error_count += 1
        if _hz_error_count >= HZ_BACKOFF_AFTER_ERRORS:
            _hz_backoff_until = now + HZ_BACKOFF_S
            _last_hz_backoff_log = now
            print(
                f"[{time.strftime('%H:%M:%S')}] Prozessdaten-Hz nicht lesbar: {exc}; "
                f"pausiere Web-Hz nach {_hz_error_count} Fehlern fuer {int(HZ_BACKOFF_S / 60)} min. "
                "Modbus und externer Leistungsmesser bleiben aktiv.",
                flush=True,
            )
            _hz_error_count = 0
            return None
        if now - _last_hz_error_log >= HZ_ERROR_LOG_INTERVAL_S:
            _last_hz_error_log = now
            print(
                f"[{time.strftime('%H:%M:%S')}] Prozessdaten-Hz nicht lesbar: {exc} "
                f"(Fehler {_hz_error_count}/{HZ_BACKOFF_AFTER_ERRORS}; optional, "
                "Leistung kommt weiter aus Modbus/Shelly)",
                flush=True,
            )
    return None


def read_stiebel_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    ip = str(cfg.get("stiebel_isg_ip", "") or "").strip()
    port = safe_int(cfg.get("stiebel_isg_port", 502), 502)
    unit_id = safe_int(cfg.get("stiebel_isg_device_id", 1), 1)
    if not has_address(ip):
        raise RuntimeError("stiebel_isg_ip nicht konfiguriert")

    system = read_regs(ip, port, unit_id, 4, code_address(501), 40)
    state = read_regs(ip, port, unit_id, 4, code_address(2501), 47)
    energy = read_regs(ip, port, unit_id, 4, code_address(3501), 40)
    info = read_regs(ip, port, unit_id, 4, code_address(5001), 2)
    sg = []
    operating_mode = None
    compressor_percent = None
    param_regs = []
    try:
        sg = read_regs(ip, port, unit_id, 3, code_address(4001), 3)
    except Exception:
        pass
    try:
        param_regs = read_regs(ip, port, unit_id, 3, code_address(1501), 11)
        operating_mode = uint_or_none(value_at_doc(param_regs, 1501, 1501))
    except Exception:
        pass
    try:
        dyn = read_regs(ip, port, unit_id, 4, code_address(6128), 1)
        raw_percent = uint_or_none(dyn[0] if dyn else None)
        if raw_percent is not None and 0 <= raw_percent <= 100:
            compressor_percent = raw_percent
    except Exception:
        pass
    compressor_hz = scrape_compressor_hz(cfg, ip)
    direct_power = read_wpmg_phase_power(ip, port, unit_id)
    external_power = read_external_power_meter(cfg)

    return {
        "system": {
            "outside_c": temp_from_doc(system, 501, 507),
            "heating_circuit_1_c": temp_from_doc(system, 501, 508),
            "heating_circuit_1_set_c": temp_from_doc(system, 501, 510, 509),
            "heating_circuit_2_set_c": temp_from_doc(system, 501, 512),
            "flow_c": temp_from_doc(system, 501, 515, 513, 517),
            "return_c": temp_from_doc(system, 501, 516),
            "buffer_c": temp_from_doc(system, 501, 518),
            "buffer_set_c": temp_from_doc(system, 501, 519),
            "dhw_c": temp_from_doc(system, 501, 522),
            "dhw_set_c": temp_from_doc(system, 501, 523),
            "source_c": temp_from_doc(system, 501, 536, 537),
        },
        "state": {
            "operating_status": uint_or_none(value_at_doc(state, 2501, 2501)),
            "fault_status": uint_or_none(value_at_doc(state, 2501, 2504)),
            "dhw_pump": uint_or_none(value_at_doc(state, 2501, 2514)),
            "cooling_mode": uint_or_none(value_at_doc(state, 2501, 2520)),
            "compressor_1": uint_or_none(value_at_doc(state, 2501, 2542)),
            "compressor_2": uint_or_none(value_at_doc(state, 2501, 2543)),
        },
        "energy": {
            "heat_day_kwh": uint_or_none(value_at_doc(energy, 3501, 3501)),
            "dhw_day_kwh": uint_or_none(value_at_doc(energy, 3501, 3504)),
            "heat_consumed_day_kwh": uint_or_none(value_at_doc(energy, 3501, 3511)),
            "dhw_consumed_day_kwh": uint_or_none(value_at_doc(energy, 3501, 3514)),
        },
        "sg": {
            "state": uint_or_none(value_at_doc(info, 5001, 5001)),
            "controller_id": uint_or_none(value_at_doc(info, 5001, 5002)),
            "switch": uint_or_none(value_at_doc(sg, 4001, 4001)),
            "input1": uint_or_none(value_at_doc(sg, 4001, 4002)),
            "input2": uint_or_none(value_at_doc(sg, 4001, 4003)),
        },
        "dynamic": {
            "compressor_percent": compressor_percent,
            "compressor_hz": compressor_hz,
        },
        "params": {
            "comfort_temp_hk1_c": temp_01k(value_at_doc(param_regs, 1501, 1502)),
            "eco_temp_hk1_c": temp_01k(value_at_doc(param_regs, 1501, 1503)),
            "comfort_temp_hk2_c": temp_01k(value_at_doc(param_regs, 1501, 1505)),
            "eco_temp_hk2_c": temp_01k(value_at_doc(param_regs, 1501, 1506)),
            "comfort_temp_dhw_c": temp_degc_or_01k(value_at_doc(param_regs, 1501, 1510)),
            "eco_temp_dhw_c": temp_01k(value_at_doc(param_regs, 1501, 1511)),
        },
        "direct_power": direct_power,
        "external_power": external_power,
        "operating_mode": operating_mode,
    }


def normalise_payload(payload: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    system = payload.get("system", {})
    state = payload.get("state", {})
    energy = payload.get("energy", {})
    sg = payload.get("sg", {})
    dynamic = payload.get("dynamic", {})
    params = payload.get("params", {})
    direct_power = payload.get("direct_power", {})
    external_power = payload.get("external_power", {})

    heating_w = max(0, safe_int(cfg.get("stiebel_isg_power_heating_w", 1500), 1500))
    dhw_w = max(0, safe_int(cfg.get("stiebel_isg_power_dhw_w", 2500), 2500))
    cop_estimate = max(0.0, safe_float(cfg.get("stiebel_isg_cop_estimate", 3.0), 3.0))
    standby_w = max(0, safe_int(cfg.get("stiebel_isg_standby_w", 35), 35))
    max_hz = max(1.0, safe_float(cfg.get("stiebel_isg_max_hz", 60), 60.0))
    hz_power_map = parse_power_map(cfg.get("stiebel_isg_hz_power_map", ""))

    operating = safe_int(state.get("operating_status"), 0)
    operating_mode = safe_int(payload.get("operating_mode", -1), -1)
    compressor_percent = dynamic.get("compressor_percent")
    compressor_hz = dynamic.get("compressor_hz")
    compressor_percent_f = safe_float(compressor_percent, -1.0)
    compressor_hz_f = safe_float(compressor_hz, -1.0)
    direct_power_w = safe_float(direct_power.get("power_w") if isinstance(direct_power, dict) else None, -1.0)
    direct_power_ok = isinstance(direct_power, dict) and direct_power_w >= 0.0 and not direct_power.get("error")
    external_power_w = safe_float(external_power.get("power_w") if isinstance(external_power, dict) else None, -1.0)
    external_power_ok = isinstance(external_power, dict) and external_power_w >= 0.0 and not external_power.get("error")
    status_heating = status_bit(operating, WPM_STATUS_HEATING)
    status_dhw = status_bit(operating, WPM_STATUS_DHW)
    status_compressor = status_bit(operating, WPM_STATUS_COMPRESSOR)
    status_summer = status_bit(operating, WPM_STATUS_SUMMER)
    status_cooling = status_bit(operating, WPM_STATUS_COOLING)
    compressor_on = bool(
        status_compressor
        or safe_int(state.get("compressor_1"), 0) == 1
        or safe_int(state.get("compressor_2"), 0) == 1
        or compressor_percent_f > 0.0
        or compressor_hz_f > 0.0
    )
    dhw_requested = bool(
        status_dhw
        or operating_mode == 5
        or safe_int(state.get("dhw_pump"), 0) == 1
        or (
            compressor_on
            and safe_float(system.get("dhw_set_c"), -99.0) >= 35.0
            and safe_float(system.get("dhw_c"), -99.0) < safe_float(system.get("dhw_set_c"), -99.0) - 0.3
            and (safe_float(system.get("flow_c"), -99.0) >= 48.0 or safe_float(system.get("return_c"), -99.0) >= 45.0)
        )
    )
    cooling_requested = bool(status_cooling or safe_int(state.get("cooling_mode"), 0) == 1)
    dhw_active = bool(dhw_requested and compressor_on)
    passive_cooling_active = bool(cooling_requested and not compressor_on)
    cooling_active = bool(cooling_requested and not dhw_active)
    heating_active = bool((status_heating or compressor_on) and compressor_on and not dhw_active and not cooling_active)
    mode_max_w = dhw_w if dhw_active else (heating_w if heating_active else max(heating_w, dhw_w))

    if external_power_ok:
        power_w = max(0, int(round(external_power_w)))
        power_source = str(external_power.get("source") or "external_power_meter")
    elif direct_power_ok:
        power_w = max(0, int(round(direct_power_w)))
        power_source = str(direct_power.get("source") or "wpmg_phase_power")
    elif compressor_percent_f >= 0.0:
        power_w = estimate_from_percent(compressor_percent_f, mode_max_w, standby_w)
        power_source = "compressor_percent"
    elif compressor_hz_f >= 0.0:
        power_w = estimate_from_hz(compressor_hz_f, mode_max_w, standby_w, max_hz, hz_power_map)
        power_source = "compressor_hz"
    elif passive_cooling_active:
        power_w = standby_w
        power_source = "passive_cooling_standby"
    elif not compressor_on:
        power_w = standby_w
        power_source = "standby"
    elif dhw_active:
        power_w = dhw_w
        power_source = "status_nominal_dhw"
    else:
        power_w = heating_w
        power_source = "status_nominal_heating"

    estimated_thermal_kw = 0.0
    thermal_power_estimated = False
    if compressor_on and power_w > standby_w and cop_estimate > 0.0:
        estimated_thermal_kw = round((power_w * cop_estimate) / 1000.0, 3)
        thermal_power_estimated = True

    mode_text = (
        "WW + passive Kühlung"
        if dhw_requested and passive_cooling_active
        else (
            "Passive Kühlung"
            if passive_cooling_active
            else ("WW" if dhw_active else ("Kühlen" if cooling_active else ("Heizen" if heating_active else "Standby")))
        )
    )
    heat_day = safe_float(energy.get("heat_day_kwh"), 0.0) + safe_float(energy.get("dhw_day_kwh"), 0.0)
    consumed_day = safe_float(energy.get("heat_consumed_day_kwh"), 0.0) + safe_float(energy.get("dhw_consumed_day_kwh"), 0.0)

    data = {
        "Hersteller": "Stiebel Eltron",
        "Quelle": "stiebel_live",
        "Aussentemp": system.get("outside_c"),
        "Aussentemperatur": system.get("outside_c"),
        "Heizgrenze_Temperatur": safe_float(cfg.get("heizgrenze_temp", 16.0), 16.0),
        "Heizkreis1_Ist": system.get("heating_circuit_1_c"),
        "Heizkreis1_Soll": system.get("heating_circuit_1_set_c"),
        "Heizkreis2_Soll": system.get("heating_circuit_2_set_c"),
        "Vorlauf_Ist": system.get("flow_c"),
        "Ruecklauf_Ist": system.get("return_c"),
        "Puffer_Ist": system.get("buffer_c"),
        "Puffer_Soll": system.get("buffer_set_c"),
        "Kaeltespeicher_Ist": system.get("buffer_c"),
        "Warmwasser_Ist": system.get("dhw_c"),
        "Warmwasser_Soll": system.get("dhw_set_c") if system.get("dhw_set_c") is not None else params.get("comfort_temp_dhw_c"),
        "Quellentemperatur": system.get("source_c"),
        "Waermequelle_Temperatur": system.get("source_c"),
        "Betriebszustand": mode_text,
        "Verdichter_Ein": 1 if compressor_on else 0,
        "Verdichter": 1 if compressor_on else 0,
        "Leistung_Verdichter_W": power_w,
        "Leistungsaufnahme": round(power_w / 1000.0, 3),
        "Leistung_Heiz_kW": estimated_thermal_kw if (heating_active or dhw_active) else 0.0,
        "Leistung_Kuehl_kW": estimated_thermal_kw if cooling_active else 0.0,
        "Kuehlung_Aktiv": 1 if cooling_active else 0,
        "Passive_Kuehlung_Aktiv": 1 if passive_cooling_active else 0,
        "Waerme_Tag_kWh": heat_day,
        "Strom_Tag_kWh": consumed_day,
        "wp_mode_text": mode_text,
        "stiebel_power_source": power_source,
        "stiebel_heat_power_estimated": 1 if thermal_power_estimated else 0,
        "stiebel_heat_power_source": "estimated_from_electric_cop" if thermal_power_estimated else None,
        "stiebel_cop_estimate": cop_estimate,
        "stiebel_operating_status": operating,
        "stiebel_heating_active": 1 if heating_active else 0,
        "stiebel_dhw_active": 1 if dhw_active else 0,
        "stiebel_cooling_active": 1 if cooling_active else 0,
        "stiebel_passive_cooling_active": 1 if passive_cooling_active else 0,
        "stiebel_dhw_requested": 1 if dhw_requested else 0,
        "stiebel_cooling_requested": 1 if cooling_requested else 0,
        "stiebel_compressor_running": 1 if compressor_on else 0,
        "stiebel_summer_mode_active": 1 if status_summer else 0,
        "stiebel_operating_mode": operating_mode if operating_mode >= 0 else None,
        "stiebel_operating_mode_text": WPM_OPERATING_MODE_TEXT.get(operating_mode),
        "stiebel_fault_status": state.get("fault_status"),
        "stiebel_cooling_mode_status": state.get("cooling_mode"),
        "stiebel_sg_ready_state": sg.get("state"),
        "stiebel_sg_ready_switch": sg.get("switch"),
        "stiebel_sg_ready_input1": sg.get("input1"),
        "stiebel_sg_ready_input2": sg.get("input2"),
        "stiebel_controller_id": sg.get("controller_id"),
        "stiebel_compressor_percent": compressor_percent if compressor_percent_f >= 0.0 else None,
        "stiebel_compressor_hz": compressor_hz if compressor_hz_f >= 0.0 else None,
        "stiebel_direct_power_w": round(direct_power_w, 1) if direct_power_w >= 0.0 else None,
        "stiebel_direct_power_source": direct_power.get("source") if isinstance(direct_power, dict) else None,
        "stiebel_direct_power_phase_a_w": direct_power.get("phase_a_w") if isinstance(direct_power, dict) else None,
        "stiebel_direct_power_phase_b_w": direct_power.get("phase_b_w") if isinstance(direct_power, dict) else None,
        "stiebel_direct_power_phase_c_w": direct_power.get("phase_c_w") if isinstance(direct_power, dict) else None,
        "stiebel_comfort_temp_hk1_c": params.get("comfort_temp_hk1_c"),
        "stiebel_eco_temp_hk1_c": params.get("eco_temp_hk1_c"),
        "stiebel_comfort_temp_hk2_c": params.get("comfort_temp_hk2_c"),
        "stiebel_eco_temp_hk2_c": params.get("eco_temp_hk2_c"),
        "stiebel_comfort_temp_dhw_c": params.get("comfort_temp_dhw_c"),
        "stiebel_eco_temp_dhw_c": params.get("eco_temp_dhw_c"),
        "stiebel_external_power_w": round(external_power_w, 1) if external_power_w >= 0.0 else None,
        "stiebel_external_power_source": external_power.get("source") if isinstance(external_power, dict) else None,
        "stiebel_external_power_error": external_power.get("error") if isinstance(external_power, dict) else None,
        "stiebel_external_power_raw_w": external_power.get("raw_power_w") if isinstance(external_power, dict) else None,
        "stiebel_external_power_phase_a_w": external_power.get("phase_a_w") if isinstance(external_power, dict) else None,
        "stiebel_external_power_phase_b_w": external_power.get("phase_b_w") if isinstance(external_power, dict) else None,
        "stiebel_external_power_phase_c_w": external_power.get("phase_c_w") if isinstance(external_power, dict) else None,
    }
    data = {key: value for key, value in data.items() if value is not None}
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "success": True,
        "error": "",
        "source": "stiebel_live",
        "data": data,
        "status": {
            "valid": True,
            "Mode": mode_text,
            "Compressor": 1 if compressor_on else 0,
            "Heating": 1 if heating_active else 0,
            "DHW": 1 if dhw_active else 0,
            "Cooling": 1 if cooling_active else 0,
            "Passive_Cooling": 1 if passive_cooling_active else 0,
            "DHW_Request": 1 if dhw_requested else 0,
            "Cooling_Request": 1 if cooling_requested else 0,
            "Summer_Mode": 1 if status_summer else 0,
            "SG_State": sg.get("state"),
            "Operating_Mode": operating_mode if operating_mode >= 0 else None,
            "Operating_Mode_Text": WPM_OPERATING_MODE_TEXT.get(operating_mode),
        },
        "stiebel": {
            "estimated_power_w": power_w,
            "power_source": power_source,
            "direct_power_w": round(direct_power_w, 1) if direct_power_w >= 0.0 else None,
            "direct_power_source": direct_power.get("source") if isinstance(direct_power, dict) else None,
            "external_power_w": round(external_power_w, 1) if external_power_w >= 0.0 else None,
            "external_power_source": external_power.get("source") if isinstance(external_power, dict) else None,
            "external_power_error": external_power.get("error") if isinstance(external_power, dict) else None,
            "compressor_percent": compressor_percent if compressor_percent_f >= 0.0 else None,
            "compressor_hz": compressor_hz if compressor_hz_f >= 0.0 else None,
            "compressor_running": bool(compressor_on),
            "heating_active": bool(heating_active),
            "dhw_active": bool(dhw_active),
            "cooling_active": bool(cooling_active),
            "passive_cooling_active": bool(passive_cooling_active),
            "dhw_requested": bool(dhw_requested),
            "cooling_requested": bool(cooling_requested),
            "summer_mode_active": bool(status_summer),
            "sg_ready_state": sg.get("state"),
            "write_enabled": False,
        },
    }


def write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    try:
        os.chmod(tmp_path, 0o664)
        import grp

        os.chown(tmp_path, -1, grp.getgrnam("www-data").gr_gid)
    except Exception:
        pass
    os.replace(tmp_path, path)


def save_payload(payload: dict[str, Any]) -> None:
    write_json(WP_FILE, payload)
    write_json(STIEBEL_FILE, payload)


def save_error(message: str, cfg: dict[str, Any], write_wp: bool = True) -> None:
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "success": False,
        "error": message,
        "source": "stiebel_live",
        "data": {"Hersteller": "Stiebel Eltron", "Quelle": "stiebel_live"},
        "stiebel": {"ip": cfg.get("stiebel_isg_ip", ""), "write_enabled": False},
    }
    if write_wp:
        write_json(WP_FILE, payload)
    write_json(STIEBEL_FILE, payload)


def configured_for_stiebel(cfg: dict[str, Any]) -> bool:
    return str(cfg.get("wp_type", "")).strip() == "4" and cfg_enabled(cfg.get("luxtronik", "0"))


def main() -> None:
    print(f"[{time.strftime('%H:%M:%S')}] Starte {LOG_PREFIX} (read-only)...", flush=True)
    while not _stop:
        cfg = read_config()
        try:
            if not configured_for_stiebel(cfg):
                save_error("Stiebel ist nicht aktiviert (Wärmepumpen-Typ Stiebel und WP-/Verbrauchslogging aktiv erforderlich).", cfg, write_wp=False)
                time.sleep(POLL_INTERVAL_S)
                continue
            payload = normalise_payload(read_stiebel_payload(cfg), cfg)
            save_payload(payload)
        except Exception as exc:
            print(f"[{time.strftime('%H:%M:%S')}] Stiebel ISG Fehler: {exc}", flush=True)
            save_error(str(exc), cfg)

        for _ in range(POLL_INTERVAL_S):
            if _stop:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
