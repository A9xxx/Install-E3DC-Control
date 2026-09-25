#!/usr/bin/env python3
"""Zusatzwechselrichter per Modbus TCP (Sungrow SG-Serie), ausschließlich lesend.

Reiner Treiber ohne Fremdbibliothek: baut Modbus-TCP-Rahmen (FC04, Eingangsregister)
selbst, liest einen Registerblock, skaliert ihn nach der öffentlichen Sungrow-
Protokollbeschreibung für netzgekoppelte String-Wechselrichter und liefert ein
normalisiertes Ergebnis samt Gültigkeit. Er entscheidet nichts und regelt nichts;
die Regelung nutzt weiterhin den E3DC-Messwert (`Ext_PV_Power`). Jede Störung
(Zeitüberschreitung, Modbus-Ausnahme, falsche Unit-ID, unvollständige Antwort)
ergibt `valid=False` mit Fehlertext und ohne Teildaten (fail-closed).

Registerbelegung (Registernummern wie in der Sungrow-Beschreibung, 1-basiert; auf
dem Draht wird die Adresse Register − 1 gesendet). Belegt über SunGather
(registers-sungrow.yaml), das evcc-Template `sungrow-inverter` (Wirkleistung
Adresse 5030 = Register 5031, U32 mit vertauschten Wörtern) und die
Home-Assistant-Modbus-YAML für Sungrow (Adresse = Register − 1, `swap: word`):

    5000  Gerätetypcode           U16
    5001  Nennleistung            U16  × 0,1 kW
    5002  Ausgangstyp             U16  0 = einphasig, 1 = 3P4L, 2 = 3P3L
    5003  Tagesertrag             U16  × 0,1 kWh
    5004  Gesamtertrag            U32  kWh
    5006  Betriebsstunden         U32  h
    5008  Innentemperatur         S16  × 0,1 °C
    5009  Scheinleistung          U32  VA
    5011  MPPT1 Spannung / 5012 Strom   U16 × 0,1 V / × 0,1 A
    5013  MPPT2 Spannung / 5014 Strom
    5015  MPPT3 Spannung / 5016 Strom  (nur bei Geräten mit drei Trackern belegt)
    5017  DC-Gesamtleistung       U32  W
    5019  Spannung A / 5020 B / 5021 C         U16 × 0,1 V  (Ausgangstyp 1: Phasenspannung;
                                                Ausgangstyp 2: Leiterspannung A-B/B-C/C-A;
                                                Ausgangstyp 0: nur 5019 belegt)
    5022  Strom Phase A / 5023 B / 5024 C      S16 × 0,1 A
    5031  Wirkleistung gesamt     U32  W
    5033  Blindleistung gesamt    S32  var
    5035  Leistungsfaktor         S16  × 0,001
    5036  Netzfrequenz            U16  × 0,1 Hz
    5038  Betriebszustand         U16  (0 = Betrieb, 0x8000 = Stopp, …)

U32/S32 stehen mit niederwertigem Wort zuerst (Wort-Tausch). Die Phasenleistungen
`ac_p1_w..ac_p3_w` sind eine Näherung aus U × I je Phase ohne Leistungsfaktor
(Scheinleistung je Phase); der gemessene Leistungsfaktor wird getrennt ausgegeben.
Welche Spannung 5019–5021 tragen, bestimmt der Ausgangstyp (5002): bei 1 (3P4L)
Phasenspannungen, bei 2 (3P3L) Leiterspannungen A-B/B-C/C-A – dann rechnet die
Näherung mit U_Leiter/√3 (symmetrisches Netz vorausgesetzt); `ac_voltage_kind`
(phase|line|unknown) benennt die Basis der Rohwerte `ac_v1..ac_v3`. Bei
Ausgangstyp 0 (einphasig) sind nur 5019/5022 belegt, Phase 2/3 bleiben None;
bei unbekanntem Ausgangstyp bleiben die Phasenleistungen None (kein Schätzwert).
Hybridgeräte (SH-Serie) haben eine andere Belegung und sind hier nicht abgedeckt.
"""

from __future__ import annotations

import ipaddress
import math
import socket
import struct
import threading
import time
from typing import Any, Callable, Dict, Optional, Sequence

SCHEMA_VERSION = "ext_inverter_v1"
EXT_INVERTER_TYPES = ("none", "sungrow_modbus")
DEFAULT_PORT = 502
DEFAULT_UNIT_ID = 1
DEFAULT_POLL_S = 10
MIN_POLL_S = 5
MAX_POLL_S = 60
DEFAULT_TIMEOUT_S = 3.0
MODBUS_FC_READ_INPUT_REGISTERS = 4
MODBUS_PROTOCOL_ID = 0
MODBUS_MAX_REGISTERS = 125
MODBUS_EXCEPTION_NAMES = {
    1: "illegal_function",
    2: "illegal_data_address",
    3: "illegal_data_value",
    4: "slave_device_failure",
    5: "acknowledge",
    6: "slave_device_busy",
    8: "memory_parity_error",
    10: "gateway_path_unavailable",
    11: "gateway_target_device_failed",
}

# Sungrow-Register (1-basiert) – ein zusammenhängender Block 5000..5038.
SUNGROW_BLOCK_START_REGISTER = 5000
SUNGROW_BLOCK_COUNT = 39
SUNGROW_REGISTERS = {
    "device_type_code": 5000,
    "nominal_power": 5001,
    "output_type": 5002,
    "daily_yield": 5003,
    "total_yield": 5004,
    "running_hours": 5006,
    "internal_temperature": 5008,
    "apparent_power": 5009,
    "mppt1_voltage": 5011,
    "mppt1_current": 5012,
    "mppt2_voltage": 5013,
    "mppt2_current": 5014,
    "mppt3_voltage": 5015,
    "mppt3_current": 5016,
    "dc_power": 5017,
    "phase_a_voltage": 5019,
    "phase_b_voltage": 5020,
    "phase_c_voltage": 5021,
    "phase_a_current": 5022,
    "phase_b_current": 5023,
    "phase_c_current": 5024,
    "active_power": 5031,
    "reactive_power": 5033,
    "power_factor": 5035,
    "grid_frequency": 5036,
    "work_state": 5038,
}
SUNGROW_OUTPUT_TYPES = {0: "single_phase", 1: "three_phase_4l", 2: "three_phase_3l"}
# Spannungsbasis der Register 5019–5021 je Ausgangstyp (Sungrow-Protokollbeschreibung, Zeilen 22–24):
# 0/1 = Phasenspannung, 2 = Leiterspannung A-B/B-C/C-A; sonst unbekannt (Phasenleistung dann None).
SUNGROW_AC_VOLTAGE_KINDS = {0: "phase", 1: "phase", 2: "line"}
SQRT3 = math.sqrt(3.0)
SUNGROW_WORK_STATES = {
    0x0000: "run",
    0x8000: "stop",
    0x1300: "key_stop",
    0x1500: "emergency_stop",
    0x1400: "standby",
    0x1200: "initial_standby",
    0x1600: "starting",
    0x9100: "alarm_run",
    0x8100: "derating_run",
    0x8200: "dispatch_run",
    0x5500: "fault",
    0x2500: "communication_fault",
}
# Plausibilitätsschranke: mehr als das 1,5-fache der Nennleistung ist kein Messwert.
AC_POWER_NOMINAL_FACTOR_MAX = 1.5


class ExtInverterError(RuntimeError):
    """Modbus-Transport- oder Protokollfehler (Antwort unbrauchbar)."""


# ---------------------------------------------------------------------------
# Modbus-TCP-Rahmen (FC04), ohne Bibliothek
# ---------------------------------------------------------------------------

def build_read_input_registers_request(transaction_id: int, unit_id: int, address: int, count: int) -> bytes:
    if not 0 <= int(address) <= 0xFFFF:
        raise ExtInverterError("modbus_address_out_of_range")
    if not 1 <= int(count) <= MODBUS_MAX_REGISTERS:
        raise ExtInverterError("modbus_count_out_of_range")
    pdu = struct.pack(">BHH", MODBUS_FC_READ_INPUT_REGISTERS, int(address), int(count))
    return struct.pack(">HHHB", int(transaction_id) & 0xFFFF, MODBUS_PROTOCOL_ID, len(pdu) + 1, int(unit_id) & 0xFF) + pdu


def parse_read_input_registers_response(frame: bytes, transaction_id: int, unit_id: int, count: int) -> Sequence[int]:
    """Prüft MBAP-Kopf, Unit-ID, Funktionscode und Bytezahl; Modbus-Ausnahmen werden zu ExtInverterError."""
    if len(frame) < 9:
        raise ExtInverterError("modbus_response_too_short")
    tid, proto, length, unit = struct.unpack(">HHHB", frame[:7])
    if tid != (int(transaction_id) & 0xFFFF):
        raise ExtInverterError("modbus_transaction_id_mismatch")
    if proto != MODBUS_PROTOCOL_ID:
        raise ExtInverterError("modbus_protocol_id_invalid")
    if unit != (int(unit_id) & 0xFF):
        raise ExtInverterError("modbus_unit_id_mismatch (%d != %d)" % (unit, int(unit_id) & 0xFF))
    body = frame[7:]
    if len(body) != length - 1:
        raise ExtInverterError("modbus_length_mismatch")
    function = body[0]
    if function & 0x80:
        code = body[1] if len(body) > 1 else 0
        raise ExtInverterError("modbus_exception_%d_%s" % (code, MODBUS_EXCEPTION_NAMES.get(code, "unknown")))
    if function != MODBUS_FC_READ_INPUT_REGISTERS:
        raise ExtInverterError("modbus_function_mismatch")
    byte_count = body[1]
    if byte_count != 2 * int(count) or len(body) != 2 + byte_count:
        raise ExtInverterError("modbus_byte_count_mismatch")
    return struct.unpack(">%dH" % int(count), body[2:2 + byte_count])


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ExtInverterError("modbus_connection_closed")
        data += chunk
    return data


def modbus_read_input_registers(
    ip: str,
    port: int,
    unit_id: int,
    address: int,
    count: int,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    transaction_id: int = 1,
) -> Sequence[int]:
    """Eine FC04-Lesung (0-basierte Adresse) über eine kurzlebige TCP-Verbindung."""
    request = build_read_input_registers_request(transaction_id, unit_id, address, count)
    try:
        with socket.create_connection((str(ip), int(port)), timeout=float(timeout_s)) as sock:
            sock.settimeout(float(timeout_s))
            sock.sendall(request)
            header = _recv_exact(sock, 7)
            length = struct.unpack(">H", header[4:6])[0]
            if length < 2 or length > 2 + 2 * MODBUS_MAX_REGISTERS + 1:
                raise ExtInverterError("modbus_length_invalid")
            body = _recv_exact(sock, length - 1)
    except ExtInverterError:
        raise
    except socket.timeout as exc:
        raise ExtInverterError("timeout: %s" % exc) from exc
    except OSError as exc:
        raise ExtInverterError("socket: %s" % (exc.strerror or exc)) from exc
    return parse_read_input_registers_response(header + body, transaction_id, unit_id, count)


# ---------------------------------------------------------------------------
# Sungrow-Dekodierung
# ---------------------------------------------------------------------------

def _u16(regs: Sequence[int], register: int) -> int:
    return int(regs[register - SUNGROW_BLOCK_START_REGISTER]) & 0xFFFF


def _s16(regs: Sequence[int], register: int) -> int:
    value = _u16(regs, register)
    return value - 0x10000 if value & 0x8000 else value


def _u32_low_first(regs: Sequence[int], register: int) -> int:
    return _u16(regs, register) | (_u16(regs, register + 1) << 16)


def _s32_low_first(regs: Sequence[int], register: int) -> int:
    value = _u32_low_first(regs, register)
    return value - 0x1_0000_0000 if value & 0x8000_0000 else value


def decode_sungrow_block(regs: Sequence[int]) -> Dict[str, Any]:
    """Skaliert den Block 5000..5038 (39 Register) in normalisierte Größen."""
    if len(regs) != SUNGROW_BLOCK_COUNT:
        raise ExtInverterError("sungrow_block_length_%d" % len(regs))
    r = SUNGROW_REGISTERS
    mppt = []
    for index in (1, 2, 3):
        volt = _u16(regs, r["mppt%d_voltage" % index]) * 0.1
        amp = _u16(regs, r["mppt%d_current" % index]) * 0.1
        mppt.append({"index": index, "v": round(volt, 1), "i": round(amp, 1), "w": int(round(volt * amp))})
    ac_v = [round(_u16(regs, r[key]) * 0.1, 1) for key in ("phase_a_voltage", "phase_b_voltage", "phase_c_voltage")]
    ac_i = [round(_s16(regs, r[key]) * 0.1, 1) for key in ("phase_a_current", "phase_b_current", "phase_c_current")]
    output_type = _u16(regs, r["output_type"])
    # Phasenleistung je Ausgangstyp – 1 (3P4L): U_Phase × I; 2 (3P3L): 5019–5021 sind Leiterspannungen,
    # also U_Leiter/√3 × I (symmetrisch); unbekannter Typ: None. Ausgangstyp 0 (einphasig): nur 5019/5022 belegt.
    voltage_kind = SUNGROW_AC_VOLTAGE_KINDS.get(output_type, "unknown")
    if voltage_kind == "phase":
        ac_p = [int(round(volt * amp)) for volt, amp in zip(ac_v, ac_i)]
    elif voltage_kind == "line":
        ac_p = [int(round(volt / SQRT3 * amp)) for volt, amp in zip(ac_v, ac_i)]
    else:
        ac_p = [None, None, None]
    if output_type == 0:
        ac_v = [ac_v[0], None, None]
        ac_i = [ac_i[0], None, None]
        ac_p = [ac_p[0], None, None]
    state_code = _u16(regs, r["work_state"])
    return {
        "device_type_code": _u16(regs, r["device_type_code"]),
        "nominal_kw": round(_u16(regs, r["nominal_power"]) * 0.1, 1),
        "output_type": output_type,
        "output_type_name": SUNGROW_OUTPUT_TYPES.get(output_type, "unknown"),
        "ac_w": _u32_low_first(regs, r["active_power"]),
        # Näherung ohne Leistungsfaktor: U × I je Phase (Scheinleistung je Phase), Spannungsbasis je Ausgangstyp s. o.
        "ac_voltage_kind": voltage_kind,
        "ac_p1_w": ac_p[0],
        "ac_p2_w": ac_p[1],
        "ac_p3_w": ac_p[2],
        "ac_v1": ac_v[0],
        "ac_v2": ac_v[1],
        "ac_v3": ac_v[2],
        "ac_i1": ac_i[0],
        "ac_i2": ac_i[1],
        "ac_i3": ac_i[2],
        "apparent_va": _u32_low_first(regs, r["apparent_power"]),
        "reactive_var": _s32_low_first(regs, r["reactive_power"]),
        "power_factor": round(_s16(regs, r["power_factor"]) * 0.001, 3),
        "dc_w": _u32_low_first(regs, r["dc_power"]),
        "mppt": mppt,
        "daily_kwh": round(_u16(regs, r["daily_yield"]) * 0.1, 1),
        "total_kwh": _u32_low_first(regs, r["total_yield"]),
        "running_h": _u32_low_first(regs, r["running_hours"]),
        "temp_c": round(_s16(regs, r["internal_temperature"]) * 0.1, 1),
        "freq_hz": round(_u16(regs, r["grid_frequency"]) * 0.1, 1),
        "state_code": state_code,
        "state_name": SUNGROW_WORK_STATES.get(state_code, "unknown"),
    }


def _invalid_result(error: str, ts: float) -> Dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "valid": False,
        "ts": float(ts),
        "error": str(error),
        "device_type_code": None,
        "nominal_kw": None,
        "output_type": None,
        "output_type_name": None,
        "ac_w": None,
        "ac_voltage_kind": None,
        "ac_p1_w": None,
        "ac_p2_w": None,
        "ac_p3_w": None,
        "ac_v1": None,
        "ac_v2": None,
        "ac_v3": None,
        "ac_i1": None,
        "ac_i2": None,
        "ac_i3": None,
        "apparent_va": None,
        "reactive_var": None,
        "power_factor": None,
        "dc_w": None,
        "mppt": [],
        "daily_kwh": None,
        "total_kwh": None,
        "running_h": None,
        "temp_c": None,
        "freq_hz": None,
        "state_code": None,
        "state_name": None,
    }


def read_sungrow(
    ip: str,
    port: int = DEFAULT_PORT,
    unit: int = DEFAULT_UNIT_ID,
    timeout: float = DEFAULT_TIMEOUT_S,
    *,
    reader: Optional[Callable[..., Sequence[int]]] = None,
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Eine Lesung des Sungrow-Blocks; jede Ausnahme ergibt valid=False ohne Teildaten."""
    ts = float(clock())
    read = reader or modbus_read_input_registers
    try:
        regs = read(ip, int(port), int(unit), SUNGROW_BLOCK_START_REGISTER - 1, SUNGROW_BLOCK_COUNT, float(timeout))
        decoded = decode_sungrow_block(list(regs))
    except ExtInverterError as exc:
        return _invalid_result(str(exc), ts)
    except Exception as exc:  # fail-closed: keine Teildaten, Text statt Absturz
        return _invalid_result("%s: %s" % (type(exc).__name__, exc), ts)
    nominal_w = float(decoded["nominal_kw"] or 0.0) * 1000.0
    if nominal_w > 0.0 and float(decoded["ac_w"]) > nominal_w * AC_POWER_NOMINAL_FACTOR_MAX:
        return _invalid_result("implausible_ac_power_%d_w" % int(decoded["ac_w"]), ts)
    result = {"schema": SCHEMA_VERSION, "valid": True, "ts": ts, "error": ""}
    result.update(decoded)
    return result


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

def _int_in_range(value: Any, default: int, minimum: int, maximum: int) -> Optional[int]:
    try:
        number = int(float(str(value).strip().replace(",", ".")))
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def ext_inverter_settings(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalisiert die Konfiguration (Schlüssel klein, Werte als Text) fail-closed.

    Ergebnis: type, enabled, ip, port, unit_id, poll_s, timeout_s, error. Ungültige
    Angaben deaktivieren die Lesung (enabled False) und benennen den Grund.
    """
    cfg = {str(k).lower(): v for k, v in (cfg or {}).items()}
    kind = str(cfg.get("ext_inverter_type", "none") or "none").strip().lower()
    ip_raw = str(cfg.get("ext_inverter_ip", "") or "").strip()
    port = _int_in_range(cfg.get("ext_inverter_port", DEFAULT_PORT) or DEFAULT_PORT, DEFAULT_PORT, 1, 65535)
    unit_id = _int_in_range(cfg.get("ext_inverter_unit_id", DEFAULT_UNIT_ID) or DEFAULT_UNIT_ID, DEFAULT_UNIT_ID, 0, 247)
    poll_s = _int_in_range(cfg.get("ext_inverter_poll_s", DEFAULT_POLL_S) or DEFAULT_POLL_S, DEFAULT_POLL_S, MIN_POLL_S, MAX_POLL_S)
    error = ""
    if kind not in EXT_INVERTER_TYPES:
        error = "unknown_type"
        kind = "none"
    elif kind != "none":
        try:
            ipaddress.IPv4Address(ip_raw)
        except (ipaddress.AddressValueError, ValueError):
            error = "invalid_ip"
        if not error and port is None:
            error = "invalid_port"
        if not error and unit_id is None:
            error = "invalid_unit_id"
        if not error and poll_s is None:
            error = "invalid_poll_s"
    enabled = bool(kind != "none" and not error)
    return {
        "type": kind,
        "enabled": enabled,
        "ip": ip_raw if enabled else "",
        "port": port if port is not None else DEFAULT_PORT,
        "unit_id": unit_id if unit_id is not None else DEFAULT_UNIT_ID,
        "poll_s": poll_s if poll_s is not None else DEFAULT_POLL_S,
        "timeout_s": DEFAULT_TIMEOUT_S,
        "error": error if kind != "none" or error else "disabled",
    }


# ---------------------------------------------------------------------------
# Hintergrund-Abfrage und Live-Felder
# ---------------------------------------------------------------------------

class ExtInverterPoller:
    """Liest alle poll_s im eigenen Thread; der Live-Zyklus holt nur den letzten Stand (nie blockierend)."""

    def __init__(
        self,
        settings: Dict[str, Any],
        *,
        reader: Callable[..., Dict[str, Any]] = read_sungrow,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = dict(settings)
        self._reader = reader
        self._clock = clock
        self._lock = threading.Lock()
        self._last: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.poll_count = 0

    def poll_once(self) -> Dict[str, Any]:
        """Eine Lesung (synchron); Ausnahmen des Lesers werden zu valid=False."""
        settings = self.settings
        try:
            result = self._reader(settings["ip"], settings["port"], settings["unit_id"], settings["timeout_s"])
            if not isinstance(result, dict) or "valid" not in result:
                result = _invalid_result("reader_returned_no_result", self._clock())
        except Exception as exc:
            result = _invalid_result("%s: %s" % (type(exc).__name__, exc), self._clock())
        with self._lock:
            self._last = result
            self.poll_count += 1
        return result

    def _run(self) -> None:
        interval = max(float(MIN_POLL_S), float(self.settings.get("poll_s", DEFAULT_POLL_S)))
        while not self._stop.is_set():
            started = self._clock()
            self.poll_once()
            elapsed = max(0.0, self._clock() - started)
            if self._stop.wait(max(0.5, interval - elapsed)):
                break

    def start(self) -> None:
        if self._thread is not None or not self.settings.get("enabled"):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ext-inverter-poll", daemon=True)
        self._thread.start()

    def stop(self, join_timeout_s: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(join_timeout_s)
        self._thread = None

    def snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._last) if self._last is not None else None


def max_sample_age_s(settings: Dict[str, Any]) -> float:
    return 2.0 * float(settings.get("poll_s", DEFAULT_POLL_S)) + float(settings.get("timeout_s", DEFAULT_TIMEOUT_S))


def live_fields(settings: Dict[str, Any], sample: Optional[Dict[str, Any]], now: float) -> Dict[str, Any]:
    """Felder für live_data_py.json: Block `ext_inverter` plus flache `ext_pv_*`-Diagnosefelder.

    Gültig nur mit aktivem Typ, erfolgreicher Lesung und Alter ≤ 2 × poll_s + Timeout.
    """
    now = float(now)
    block: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "type": settings.get("type", "none"),
        "enabled": bool(settings.get("enabled")),
        "host": settings.get("ip", ""),
        "port": settings.get("port", DEFAULT_PORT),
        "unit_id": settings.get("unit_id", DEFAULT_UNIT_ID),
        "poll_s": settings.get("poll_s", DEFAULT_POLL_S),
        "max_age_s": round(max_sample_age_s(settings), 1),
        "valid": False,
        "age_s": None,
        "error": "",
    }
    if not block["enabled"]:
        block["error"] = settings.get("error") or "disabled"
        block.update({k: v for k, v in _invalid_result(block["error"], now).items() if k not in ("schema", "valid", "ts", "error")})
    elif not isinstance(sample, dict):
        block["error"] = "no_sample_yet"
        block.update({k: v for k, v in _invalid_result(block["error"], now).items() if k not in ("schema", "valid", "ts", "error")})
    else:
        age = now - float(sample.get("ts") or 0.0)
        block["age_s"] = round(age, 1) if age >= 0.0 else None
        block.update({k: v for k, v in sample.items() if k not in ("schema", "valid", "ts", "error")})
        block["sample_ts"] = sample.get("ts")
        if not sample.get("valid"):
            block["error"] = str(sample.get("error") or "invalid")
        elif age < 0.0 or age > max_sample_age_s(settings):
            block["error"] = "stale_%.0fs" % age
        else:
            block["valid"] = True
    valid = bool(block["valid"])
    return {
        "ext_inverter": block,
        "ext_pv_valid": valid,
        "ext_pv_p1_w": block.get("ac_p1_w") if valid else None,
        "ext_pv_p2_w": block.get("ac_p2_w") if valid else None,
        "ext_pv_p3_w": block.get("ac_p3_w") if valid else None,
        "ext_pv_dc_w": block.get("dc_w") if valid else None,
        "ext_pv_age_s": block.get("age_s") if valid else None,
    }


if __name__ == "__main__":  # pragma: no cover - manuelle Probe: python3 ext_inverter_modbus.py <ip> [port] [unit]
    import json
    import sys

    argv = sys.argv[1:]
    if not argv:
        print("Aufruf: ext_inverter_modbus.py <ip> [port] [unit]")
        sys.exit(2)
    probe = read_sungrow(argv[0], int(argv[1]) if len(argv) > 1 else DEFAULT_PORT, int(argv[2]) if len(argv) > 2 else DEFAULT_UNIT_ID)
    print(json.dumps(probe, ensure_ascii=False, indent=2))
    sys.exit(0 if probe.get("valid") else 1)
