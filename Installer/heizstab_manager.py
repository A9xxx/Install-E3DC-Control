#!/usr/bin/env python3
"""
heizstab_manager.py - Autonomer Heizstab / Shelly-Heizlüfter / Shelly-3EM-WP-Manager

Heizstab/BWWP läuft als Zusatzverbraucher parallel zu Luxtronik/IDM.
wp_type bleibt der echte Wärmepumpentyp:
    0: Luxtronik
    1: IDM
    3: Shelly Pro3EM WP-Messung + optionale Relaissteuerung (keine native WP-Anbindung)
Legacy wp_type=2 wird noch akzeptiert, aber nicht mehr neu gesetzt.

Architektur:
- Liest PV-Überschuss aus live_data_py.json (Python RSCP)
- Steuert Heizstab via Modbus-TCP (Zusatzverbraucher):
    Register 1000 = Sollleistung setzen (W, wie C++ Kern)
    Register 1014 = Istleistung lesen  (W, wie C++ Kern)
- Steuert Shelly-Heizlüfter über die HTTP-API (Shelly Plug S / Pro)
- Liest WP-Leistung via Shelly Pro3EM (wp_type=3, Gen2 RPC)
- Schaltet WP-Relais via Shelly Pro3EM (wp_type=3, optional)
- Schreibt Status in /var/www/html/ramdisk/heizstab_data.json
- Keine Dauerverbindungen! Modbus-Socket nach jedem Zugriff SOFORT schließen.

Konfiguration in e3dc_v4.json:
    wp_type         = 0/1/3             (Luxtronik/IDM/Shelly-3EM-WP)
    heizstab        = 1                 (Zusatzverbraucher Heizstab/BWWP aktiv)
    heizstab_type   = generic           (generic | mypv_elwa)
    heizstab_ip     = 192.0.2.81        (0.0.0.0 = deaktiviert)
    heizstab_port   = 502
    heizstab_max_w  = 3000
    shelly_heiz_ip  = 192.0.2.82        (0.0.0.0 = deaktiviert)
    shelly_heiz_w   = 1500              (Nennleistung des Shelly-Geräts für Berechnung)
    hs_auto_mode    = 1                 (0=Manuell, 1=PV-Auto)
    hs_min_surplus_w = 500              (Mindest-PV-Überschuss zum Einschalten)
    hs_min_soc      = 20                (Mindest-Batterie-SOC für Betrieb)

    Shelly Pro3EM WP-Integration (wp_type=3):
    shelly_3em_ip        = 192.0.2.90       (IP des Shelly Pro3EM)
    shelly_3em_relay_id  = 0                (Relay-ID 0-2, -1 = kein Schalten)
    shelly_3em_wp_min_w  = 1000            (WP-Mindestleistung: Einschaltschwelle)
    shelly_3em_wp_max_w  = 3000            (WP-Nennleistung: für Budget-Berechnung)
    shelly_3em_enable    = 1               (1=Relaissteuerung aktiv, 0=nur messen)
    wp_min_runtime_min   = 30              (Taktschutz: Mindestlaufzeit für WP-Relais)
    wp_restart_block_min = 20              (Taktschutz: Wiedereinschaltsperre für WP-Relais)

myPV AC ELWA-E Modbus Register (heizstab_type = mypv_elwa):
    Register 1000 R/W  Sollleistung (W, 0-3000)
    Register 1001 R    Wassertemperatur (x0.1 degC)
    Register 1002 R    Ziel-Temperatur  (x0.1 degC)
    Register 1003 R    Status (2=Heizen, 3=Standby, 4=Boost, 5=Fertig, 201+=Fehler)
    Register 1004 R/W  Modbus-Timeout (60s empfohlen)
    HINWEIS: Register 1014 = Sicherungsgröße (13/16A) - NICHT Istleistung!
"""

import os
import sys
import json
import math
import time
import urllib.request
import urllib.error

_INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))
_INSTALL_ROOT = os.path.dirname(_INSTALLER_DIR)

try:
    from consumer_priority import (
        CONSUMER_MIN_W,
        validate_consumer_budget_contract,
        validate_consumer_command_allocations,
    )
except Exception:
    CONSUMER_MIN_W = {"heatpump": 1500, "heater": 500}
    def validate_consumer_budget_contract(_contract):
        return {"valid": False, "reason_code": "consumer_budget_validator_unavailable"}

    def validate_consumer_command_allocations(_contract):
        return {"valid": False, "reason_code": "consumer_budget_validator_unavailable"}

try:
    from Installer.storage_dispatch_contract import revision_hash
except ModuleNotFoundError:
    from storage_dispatch_contract import revision_hash  # type: ignore

try:
    from market_economics import current_market_consumer_release
except Exception:
    def current_market_consumer_release(*_args, **_kwargs):
        return {"allowed": False, "reason": "market_plan_unavailable"}

try:
    from Installer.Heat import forecast as heat_forecast
    from Installer.Heat import policy as heat_policy
    from Installer.heat_actuator_safety import default_heat_actuator_gate
except ModuleNotFoundError:
    if _INSTALLER_DIR not in sys.path:
        sys.path.insert(0, _INSTALLER_DIR)
    from Heat import forecast as heat_forecast
    from Heat import policy as heat_policy
    from heat_actuator_safety import default_heat_actuator_gate

# Modbus: nur importieren wenn verfuegbar
try:
    from pymodbus.client import ModbusTcpClient
    MODBUS_OK = True
except ImportError:
    try:
        # Fallback fuer pymodbus < 3.0 (aeltere Installationen)
        from pymodbus.client.sync import ModbusTcpClient
        MODBUS_OK = True
    except ImportError:
        MODBUS_OK = False
        print("[!] pymodbus nicht installiert - Heizstab Modbus deaktiviert")

# pymodbus API-Versionen:
#   2.x  -> unit=1
#   3.0-3.12 -> slave=1
#   3.13+    -> device_id=1
# Auto-Detect via inspect schlaegt fehl (*args/**kwargs),
# daher dreistufiges try/except in den Modbus-Funktionen.

# ---------------------------------------------------------------------------
# Pfade
# ---------------------------------------------------------------------------

RAMDISK_FILE   = "/var/www/html/ramdisk/heizstab_data.json"
LIVE_PY_FILE   = "/var/www/html/ramdisk/live_data_py.json"

# V4 JSON Config (neue Single-Source-of-Truth)
V4_CONFIG_FILE = "/var/www/html/data/e3dc_v4.json"
PREDUMP_PLAN_FILE = "/var/www/html/ramdisk/predump_consumer_plan.json"
STORAGE_PLAN_FILE = "/var/www/html/ramdisk/storage_plan.json"
WB_BUDGET_FILE = "/var/www/html/ramdisk/wb_pv_budget.json"
HS_MANUAL_FILE = "/var/www/html/ramdisk/heizstab_manual_override.json"

# Legacy Fallback: e3dc.config.txt (C++ Kern)
CONFIG_PATHS = [
    "/var/www/html/data/e3dc.config.txt",
    os.path.join(_INSTALL_ROOT, "e3dc.config.txt"),
    os.path.join(_INSTALLER_DIR, "luxtronik", "e3dc.config.txt"),
]

POLL_INTERVAL      = 15   # Sekunden zwischen Zyklen (war 10 - hoeher = weniger Modbus-Stress)
STARTUP_DELAY      = 30   # Sekunden warten nach (Re-)Start bevor erster Modbus-Zugriff
CONN_ERR_BACKOFF   = 60   # Sekunden Pause nach Connection refused
HS_HYSTERESIS_W    = 500  # Deadband: Abschalten erst wenn Ueberschuss < min - 500W
SHELLY_TIMEOUT     = 3    # HTTP Timeout
HEATER_READBACK_MAX_AGE_S = 45.0
HEATER_CONSUMER_BUDGET_MAX_AGE_S = 20.0
_CONSUMER_BUDGET_KEYS = ("heatpump", "wallbox", "heater")
_CONSUMER_BUDGET_IDENTITY_SCHEMA = "storage_consumer_budget_identity_v1"
_CONSUMER_BUDGET_IDENTITY_NOT_APPLICABLE = "not_applicable"
_CONSUMER_BUDGET_IDENTITY_KEYS = frozenset({
    "schema_version",
    "decision_generation",
    "decision_ts",
    "decision_owner",
    "decision_effect",
    "decision_state",
    "decision_mode",
    "decision_value_w",
    "decision_protected",
    "authorized_wallbox_budget_w",
    "authorized_heatpump_budget_w",
    "authorized_heater_budget_w",
    "binding_status",
    "plan_id",
    "slot_id",
    "action_id",
})


_HEATER_ACTUATOR_GATE = None


def _heater_actuator_gate():
    global _HEATER_ACTUATOR_GATE
    if _HEATER_ACTUATOR_GATE is None:
        _HEATER_ACTUATOR_GATE = default_heat_actuator_gate(
            __file__,
            "Installer/heizstab_manager.py",
            "heizstab_manager",
        )
    return _HEATER_ACTUATOR_GATE


def _authorize_heater_output(
    driver_key,
    action,
    *,
    safety_gate=None,
    safe_release=False,
    preserve_existing=False,
):
    gate = safety_gate or _heater_actuator_gate()
    verdict = gate.authorize(
        driver_key,
        action,
        allow_release_on_invalid=bool(safe_release),
        preserve_existing=bool(preserve_existing),
    )
    if not verdict.allowed:
        print(f"  [!] Aktorausgang blockiert: {verdict.reason}")
    return verdict.allowed


def _invoke_actuator(func, *args, safety_gate=None):
    """Erhält bestehende Aufrufsignaturen und erlaubt die explizite Gate-Übergabe."""
    if safety_gate is None:
        return func(*args)
    return func(*args, safety_gate=safety_gate)


def _with_actuator_safety_status(status, safety_gate=None):
    _finalize_heater_measurement_projection(status)
    gate = safety_gate or _HEATER_ACTUATOR_GATE
    authorization = getattr(gate, "last_authorization", None) if gate is not None else None
    status["actor_writes_blocked"] = bool(
        authorization is not None and not getattr(authorization, "allowed", False)
    )
    status["actor_write_block_reason"] = (
        str(getattr(authorization, "reason", ""))
        if status["actor_writes_blocked"]
        else ""
    )
    return status


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

def load_config():
    """Liest Konfiguration: Primar aus e3dc_v4.json (V4), Fallback auf e3dc.config.txt (Legacy)."""
    cfg = {
        "wp_type":              "0",
        "heizstab":             "0",
        "heizstab_type":        "generic",
        "heizstab_ip":          "0.0.0.0",
        "heizstab_port":        "502",
        "heizstab_max_w":       "3000",
        "shelly_heiz_ip":       "0.0.0.0",
        "shelly_heiz_w":        "1500",
        "hs_auto_mode":         "1",
        "hs_min_surplus_w":     "500",
        "hs_min_soc":           "20",
        "hs_grid_guard_w":      "100",
        "hs_min_change_w":      "100",
        # Shelly Pro3EM WP-Integration (wp_type=3)
        "shelly_3em_ip":        "0.0.0.0",
        "shelly_3em_relay_id":  "-1",
        "shelly_3em_wp_min_w":  "1000",
        "shelly_3em_wp_max_w":  "3000",
        "shelly_3em_enable":    "0",
        "wp_min_runtime_min":    "30",
        "wp_restart_block_min":  "20",
        "heat_policy_runtime_enable": "0",
        "heat_heater_grid_boost_enable": "0",
        "heat_heater_grid_boost_ack": "0",
        "heat_heater_grid_boost_requires_deficit": "1",
        "heat_heater_grid_boost_price_limit_ct": "0",
        "heat_heater_grid_boost_max_w": "3000",
        "heat_heater_min_temp_c": "45",
        "heat_heater_max_temp_c": "60",
        "heat_wp_daily_kwh": "",
    }

    # 1. Primaer: e3dc_v4.json laden (V4-Standard)
    if os.path.exists(V4_CONFIG_FILE):
        try:
            with open(V4_CONFIG_FILE, "r", encoding="utf-8") as f:
                v4 = json.load(f)
            for k in cfg:
                if k in v4:
                    cfg[k] = str(v4[k])
            return cfg  # V4 hat Vorrang - fertig
        except Exception as e:
            print(f"[!] e3dc_v4.json Lesefehler: {e} - versuche Fallback")

    # 2. Fallback: e3dc.config.txt (C++ Legacy oder Hybrid)
    for path in CONFIG_PATHS:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, _, v = line.partition("=")
                        k = k.strip().lower()
                        v = v.strip().strip('"').strip("'")
                        if k in cfg:
                            cfg[k] = v
            except Exception as e:
                print(f"[!] Config-Fehler {path}: {e}")
            break  # Erste gefundene Datei reicht
    return cfg


def cfg_float(cfg, key, default, min_value=None):
    raw = cfg.get(key, default)
    try:
        text = str(raw).strip()
        value = float(text if text != "" else default)
    except Exception:
        value = float(default)
    if min_value is not None:
        value = max(float(min_value), value)
    return value


def cfg_int(cfg, key, default):
    raw = cfg.get(key, default)
    try:
        text = str(raw).strip()
        return int(float(text if text != "" else default))
    except Exception:
        return int(default)


def optional_live_float(data, *keys):
    for key in keys:
        value = data.get(key) if isinstance(data, dict) else None
        if value is None or value == "":
            continue
        try:
            return float(str(value).replace(",", "."))
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Live-Daten lesen (Python RSCP)
# ---------------------------------------------------------------------------

def read_live():
    """
    Liest aktuelle Leistungswerte aus live_data_py.json (Python RSCP).
    Gibt Nullwerte zurueck wenn Datei fehlt oder aelter als 30s (Dienst tot).
    """
    if os.path.exists(LIVE_PY_FILE):
        if time.time() - os.path.getmtime(LIVE_PY_FILE) <= 30:
            try:
                with open(LIVE_PY_FILE, "r") as f:
                    data = json.load(f)
                    return {
                        "pv_w":   float(data.get("PV_Power", 0)),
                        "grid_w": float(data.get("Grid_Power", 0)),
                        "soc":    float(data.get("SOC", 0)),
                        "price_ct": optional_live_float(data, "price_ct", "billing_price_ct"),
                        "heater_temp_c": optional_live_float(data, "heater_temp_c"),
                        "source": "live_data_py.json",
                    }
            except Exception:
                pass
    return {"pv_w": 0, "grid_w": 0, "soc": 0, "price_ct": None, "heater_temp_c": None, "source": "none"}


# ---------------------------------------------------------------------------
# Heizstab Modbus-TCP (generic + myPV AC ELWA-E)
# ---------------------------------------------------------------------------

# myPV AC ELWA-E Status-Codes (Register 1003)
ELWA_STATUS = {
    0: 'Standby',
    1: 'Bereit',
    2: 'Heizen',
    3: 'Standby',
    4: 'Boost',
    5: 'Fertig',
    9: 'Setup',
    201: 'Fehler: Uebertemperatur/Sicherung',
    202: 'Fehler: Uebertemperatur',
    203: 'Fehler: Elektronik zu warm',
    204: 'Fehler: Hardware',
    205: 'Fehler: Temperatursensor',
}
ELWA_HEATING_CODES = {1, 2, 4}


def _modbus_read_regs(ip, port, start, count=1):
    """Liest count Register ab start. Gibt Liste oder None zurueck. Socket wird IMMER geschlossen."""
    if not MODBUS_OK:
        return None
    client = ModbusTcpClient(ip, port=int(port), timeout=4)
    try:
        if not client.connect():
            print(f"  [!] Modbus Verbindung zu {ip}:{port} fehlgeschlagen")
            return None
        # pymodbus 3.13+: device_id=  |  3.0-3.12: slave=  |  2.x: unit=
        try:
            result = client.read_holding_registers(start, count=count, device_id=1)
        except TypeError:
            try:
                result = client.read_holding_registers(start, count=count, slave=1)
            except TypeError:
                result = client.read_holding_registers(start, count=count, unit=1)
        if hasattr(result, 'registers') and result.registers:
            return result.registers
        return None
    except Exception as e:
        print(f"  [!] Modbus Lesen ({ip} Reg {start}): {e}")
        return None
    finally:
        client.close()


def _modbus_write_reg_raw(ip, port, register, value):
    """Schreibt einen Wert in ein Holding-Register. Socket wird IMMER geschlossen."""
    if not MODBUS_OK:
        return False
    client = ModbusTcpClient(ip, port=int(port), timeout=4)
    try:
        if not client.connect():
            return False
        # pymodbus 3.13+: device_id=  |  3.0-3.12: slave=  |  2.x: unit=
        try:
            result = client.write_register(register, int(max(0, value)), device_id=1)
        except TypeError:
            try:
                result = client.write_register(register, int(max(0, value)), slave=1)
            except TypeError:
                result = client.write_register(register, int(max(0, value)), unit=1)
        return bool(result is not None and not (hasattr(result, "isError") and result.isError()))
    except Exception as e:
        print(f"  [!] Modbus Schreiben ({ip} Reg {register}={value}): {e}")
        return False
    finally:
        client.close()


# --- Generic Heizstab (bisheriges Verhalten) ---

def heizstab_read_power(ip, port):
    """
    Generic: Liest Istleistung aus Register 1014 (W).
    ACHTUNG: Beim myPV AC ELWA-E ist Reg 1014 die Sicherungsgroesse!
    Fuer ELWA-E stattdessen elwa_read_status() verwenden.
    """
    regs = _modbus_read_regs(ip, port, 1014, count=1)
    if regs:
        return int(regs[0])
    return None


def _modbus_write_confirmed(
    ip,
    port,
    register,
    value,
    *,
    driver_key,
    safety_gate=None,
    safe_release=False,
):
    if not _authorize_heater_output(
        driver_key,
        f"modbus:{register}:{value}",
        safety_gate=safety_gate,
        safe_release=safe_release,
    ):
        return False
    if not _modbus_write_reg_raw(ip, port, register, value):
        return False
    readback = _modbus_read_regs(ip, port, register, count=1)
    return bool(readback and int(readback[0]) == int(max(0, value)))


def heizstab_set_power(ip, port, power_w, safety_gate=None):
    """Generic: Setzt Sollleistung in Register 1000 (W)."""
    value = int(max(0, power_w))
    return _modbus_write_confirmed(
        ip,
        port,
        1000,
        value,
        driver_key=f"transport:modbus-tcp:{ip}:{port}",
        safety_gate=safety_gate,
        safe_release=value == 0,
    )


# --- myPV AC ELWA-E ---

_elwa_timeout_set = {}  # {ip: True} - Timeout einmalig gesetzt


def elwa_ensure_timeout(ip, port, timeout_s=60, safety_gate=None):
    """
    Setzt Modbus-Timeout einmalig auf 60s (Register 1004).
    Schreibt selten um EEPROM-Verschleiss zu vermeiden (nur beim ersten Zyklus).
    """
    if _elwa_timeout_set.get(ip):
        return
    ok = _modbus_write_confirmed(
        ip,
        port,
        1004,
        timeout_s,
        driver_key=f"transport:modbus-tcp:{ip}:{port}",
        safety_gate=safety_gate,
        safe_release=False,
    )
    if ok:
        print(f"  [ELWA] Modbus Timeout auf {timeout_s}s gesetzt (Register 1004)")
        _elwa_timeout_set[ip] = True


def elwa_read_status(ip, port):
    """
    Liest AC ELWA-E Status-Block: Reg 1000-1003 (4 Register in einem Aufruf).
    Gibt dict zurueck:
      setpoint_w   - aktueller Sollwert (W)
      water_temp_c - Wassertemperatur (degC)
      target_temp_c- Zieltemperatur (degC)
      status_code  - 2=Heizen, 3=Standby, 4=Boost, 5=Fertig, 201+=Fehler
      status_text  - lesbare Beschreibung
      actual_w     - geschaetzte Istleistung (=setpoint wenn status=1, sonst 0)
    """
    regs = _modbus_read_regs(ip, port, 1000, count=4)
    if not regs or len(regs) < 4:
        return None
    setpoint_w    = int(regs[0])
    water_temp_c  = round(regs[1] * 0.1, 1)
    target_temp_c = round(regs[2] * 0.1, 1)
    status_code   = int(regs[3])
    status_text   = ELWA_STATUS.get(status_code, f'Fehler: Code {status_code}' if status_code >= 200 else f'Unbekannt({status_code})')
    # Istleistung: nur wenn das Geraet aktiv heizt. Status 5 ist "Fertig",
    # kein Fehler und keine laufende Leistung.
    actual_w = setpoint_w if status_code in ELWA_HEATING_CODES else 0
    return {
        'setpoint_w':    setpoint_w,
        'water_temp_c':  water_temp_c,
        'target_temp_c': target_temp_c,
        'status_code':   status_code,
        'status_text':   status_text,
        'actual_w':      actual_w,
    }


def elwa_set_power(ip, port, power_w, safety_gate=None):
    """Setzt Sollleistung am AC ELWA-E (Register 1000, 0-3000W)."""
    value = int(max(0, power_w))
    return _modbus_write_confirmed(
        ip,
        port,
        1000,
        value,
        driver_key=f"transport:modbus-tcp:{ip}:{port}",
        safety_gate=safety_gate,
        safe_release=value == 0,
    )


def elwa_can_accept_power(status_code):
    """
    True, wenn die ELWA-E eine Leistungsanforderung grundsaetzlich annehmen darf.
    Der Status wird vor dem Schreiben gelesen und kann deshalb noch Standby sein.
    Nur echte Fehler und "Fertig" sollen nicht als laufende Leistung in die
    Bilanz eingehen.
    """
    if status_code is None:
        return True
    try:
        code = int(status_code)
    except Exception:
        return True
    if code == 5 or code >= 200:
        return False
    return True


# ---------------------------------------------------------------------------
# Shelly Pro3EM HTTP API (Gen2 RPC, wp_type=3)
# ---------------------------------------------------------------------------

def shelly_3em_read(ip):
    """
    Liest alle 3 Phasen vom Shelly Pro3EM via Gen2 RPC.
    Gibt dict zurueck:
      phase_a_w, phase_b_w, phase_c_w - Wirkleistung je Phase (W)
      total_w                          - Gesamtleistung (W)
      phase_a_v, phase_b_v, phase_c_v - Spannung (V)
      phase_a_a, phase_b_a, phase_c_a - Strom (A)
      freq_hz                          - Netzfrequenz (Hz)
    Gibt None bei Fehler.
    """
    try:
        url = f"http://{ip}/rpc/EM.GetStatus?id=0"
        with urllib.request.urlopen(url, timeout=SHELLY_TIMEOUT) as r:
            d = json.loads(r.read().decode())
        if not isinstance(d, dict):
            return None

        def measured(field, digits):
            value = _strict_finite_number(d.get(field))
            return round(value, digits) if value is not None else None

        total_w = measured("total_act_power", 1)
        if total_w is None:
            return None
        return {
            "phase_a_w": measured("a_act_power", 1),
            "phase_b_w": measured("b_act_power", 1),
            "phase_c_w": measured("c_act_power", 1),
            "total_w": total_w,
            "phase_a_v": measured("a_voltage", 1),
            "phase_b_v": measured("b_voltage", 1),
            "phase_c_v": measured("c_voltage", 1),
            "phase_a_a": measured("a_current", 3),
            "phase_b_a": measured("b_current", 3),
            "phase_c_a": measured("c_current", 3),
            "freq_hz": measured("a_freq", 2),
        }
    except Exception as e:
        print(f"  [!] Shelly Pro3EM {ip} Lesefehler: {e}")
        return None


def shelly_3em_get_relay(ip, relay_id):
    """Liest den exakten Gen2-Relaiskanal und liefert bool oder None."""
    try:
        url = f"http://{ip}/rpc/Switch.GetStatus?id={int(relay_id)}"
        with urllib.request.urlopen(url, timeout=SHELLY_TIMEOUT) as response:
            data = json.loads(response.read().decode())
        if (
            not isinstance(data, dict)
            or type(data.get("output")) is not bool
        ):
            return None
        return data["output"]
    except Exception:
        return None


def shelly_3em_set_relay(ip, relay_id, on: bool, safety_gate=None):
    """
    Schaltet ein Relais des Shelly Pro3EM (Gen2 RPC Switch.Set).
    relay_id: 0, 1 oder 2 (je nach Shelly-Modell und Verdrahtung).
    Gibt True bei Erfolg, False bei Fehler.
    """
    driver_key = f"transport:http-shelly:{ip}:switch:{int(relay_id)}"
    if not _authorize_heater_output(
        driver_key,
        f"shelly-3em:{'on' if on else 'off'}",
        safety_gate=safety_gate,
        safe_release=False,
        preserve_existing=True,
    ):
        return False
    try:
        body = json.dumps({"id": int(relay_id), "on": bool(on)}).encode()
        req = urllib.request.Request(
            f"http://{ip}/rpc/Switch.Set",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=SHELLY_TIMEOUT) as r:
            resp = json.loads(r.read().decode())
        # Gen2 antwortet mit {"was_on": bool} oder {"error": ...}
        if "error" in resp:
            print(f"  [!] Shelly 3EM Relay {relay_id}: {resp['error']}")
            return False
        confirmed = shelly_3em_get_relay(ip, relay_id)
        if confirmed != bool(on):
            print(
                f"  [!] Shelly 3EM Relay {relay_id}: Readback unbestätigt "
                f"(Soll={bool(on)} Ist={confirmed})"
            )
            return False
        return True
    except Exception as e:
        print(f"  [!] Shelly Pro3EM {ip} Relay-Fehler: {e}")
        return False


def _strict_finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _heater_power_sample(value, observed_ts, now_ts, max_age_s):
    """Validiert genau einen physischen Leistungs-Readback ohne 0-W-Imputation."""
    power_w = _strict_finite_number(value)
    source_ts = _strict_finite_number(observed_ts)
    fresh = bool(
        source_ts is not None
        and source_ts > 0.0
        and 0.0 <= now_ts - source_ts <= max_age_s
    )
    valid = bool(power_w is not None and power_w >= 0.0 and fresh)
    if power_w is None or power_w < 0.0:
        reason = "power_missing_or_invalid"
    elif source_ts is None or source_ts <= 0.0:
        reason = "timestamp_missing_or_invalid"
    elif now_ts - source_ts < 0.0:
        reason = "timestamp_future"
    elif not fresh:
        reason = "readback_stale"
    else:
        reason = "ok"
    return {
        "valid": valid,
        "source_fresh": fresh,
        "power_w": int(round(power_w)) if valid else None,
        "source_ts": float(source_ts) if source_ts is not None else None,
        "reason": reason,
    }


def update_heater_power_measurement(
    status,
    *,
    modbus_configured,
    modbus_power_w=None,
    modbus_observed_ts=None,
    modbus_source="modbus_actual_power",
    shelly_configured,
    shelly_state=None,
    shelly_observed_ts=None,
    now_ts=None,
    max_age_s=HEATER_READBACK_MAX_AGE_S,
):
    """Bindet die gemeinsame Heizstab-Istleistung an echte Provider-Readbacks.

    Bei zwei konfigurierten, physisch unabhängigen Verbrauchern wird nur dann
    summiert, wenn beide Readbacks gültig und frisch sind. Fehlt einer, bleibt
    die gemeinsame Istleistung unbekannt; dadurch kann kein Budget doppelt als
    frei behandelt werden.
    """
    current_ts = time.time() if now_ts is None else float(now_ts)
    evidence_max_age_s = max(1.0, float(max_age_s))
    topology = (
        "modbus+shelly"
        if modbus_configured and shelly_configured
        else "modbus"
        if modbus_configured
        else "shelly"
        if shelly_configured
        else "none"
    )
    provider_measurements = {}

    if modbus_configured:
        provider_measurements["modbus"] = _heater_power_sample(
            modbus_power_w,
            modbus_observed_ts,
            current_ts,
            evidence_max_age_s,
        )
        provider_measurements["modbus"]["source"] = str(modbus_source)

    if shelly_configured:
        shelly_on = (
            shelly_state.get("on")
            if isinstance(shelly_state, dict)
            and type(shelly_state.get("on")) is bool
            else None
        )
        shelly_power_w = (
            shelly_state.get("power_w")
            if isinstance(shelly_state, dict)
            else None
        )
        shelly_sample = _heater_power_sample(
            shelly_power_w,
            shelly_observed_ts,
            current_ts,
            evidence_max_age_s,
        )
        if shelly_on is None:
            shelly_sample["valid"] = False
            shelly_sample["reason"] = "relay_state_missing_or_invalid"
        shelly_sample["source"] = "shelly_switch_power"
        shelly_sample["relay_on"] = shelly_on
        provider_measurements["shelly"] = shelly_sample
        status["shelly_heiz_on"] = shelly_on
        status["shelly_heiz_w"] = (
            shelly_sample["power_w"] if shelly_sample["valid"] else None
        )

    measurement_required = bool(modbus_configured or shelly_configured)
    measurement_valid = bool(
        measurement_required
        and provider_measurements
        and all(
            sample.get("valid") is True
            for sample in provider_measurements.values()
        )
    )
    actual_w = (
        sum(int(sample["power_w"]) for sample in provider_measurements.values())
        if measurement_valid
        else None
    )
    source_ts = (
        min(float(sample["source_ts"]) for sample in provider_measurements.values())
        if measurement_valid
        else None
    )
    source = (
        "+".join(
            str(sample["source"])
            for sample in provider_measurements.values()
        )
        if measurement_valid
        else ""
    )
    reasons = [
        f"{provider}:{sample.get('reason', 'invalid')}"
        for provider, sample in provider_measurements.items()
        if sample.get("valid") is not True
    ]

    status["hs_measurement_required"] = measurement_required
    status["hs_measurement_valid"] = measurement_valid
    status["hs_measurement_ts"] = source_ts
    status["hs_measurement_source"] = source
    status["hs_measurement_topology"] = topology
    status["hs_measurement_reason"] = (
        "ok" if measurement_valid else ",".join(reasons) or "not_configured"
    )
    status["hs_provider_measurements"] = provider_measurements
    status["source_fresh"] = measurement_valid
    status["hs_source_fresh"] = measurement_valid
    status["Heizstab_Power"] = actual_w
    status["hs_actual_w"] = actual_w
    status["hs_active"] = bool(actual_w > 0) if actual_w is not None else None
    if measurement_required and not measurement_valid:
        # `success` bleibt aus Kompatibilitätsgründen der aggregierte
        # Statusvertrag; `process_success` unterscheidet davon den intakten Loop.
        status["success"] = False
        status["budget_start_ready"] = False
        status["budget_start_request_w"] = 0
    return measurement_valid


def _finalize_heater_measurement_projection(status):
    """Verhindert, dass Sollwerte später als physische Istleistung erscheinen."""
    if not isinstance(status, dict) or not status.get("hs_measurement_required"):
        return status
    valid = bool(
        status.get("hs_measurement_valid") is True
        and status.get("source_fresh") is True
    )
    measurements = (
        status.get("hs_provider_measurements")
        if isinstance(status.get("hs_provider_measurements"), dict)
        else {}
    )
    actual_w = (
        sum(int(sample["power_w"]) for sample in measurements.values())
        if valid
        and measurements
        and all(
            isinstance(sample, dict)
            and sample.get("valid") is True
            and type(sample.get("power_w")) is int
            for sample in measurements.values()
        )
        else None
    )
    if actual_w is None:
        valid = False
    status["hs_measurement_valid"] = valid
    status["source_fresh"] = valid
    status["hs_source_fresh"] = valid
    status["Heizstab_Power"] = actual_w if valid else None
    status["hs_actual_w"] = actual_w if valid else None
    status["hs_active"] = bool(actual_w > 0) if valid else None
    if not valid:
        status["success"] = False
        status["budget_start_ready"] = False
        status["budget_start_request_w"] = 0
    return status


def update_shelly_3em_measurement(status, s3_ip, s3_min_w, now_ts=None):
    """Projiziert eine Pro3EM-Messung ohne 0-W-Imputation.

    Das Pro3EM misst die gesamte Wärmepumpe. Es beweist deshalb eine frische
    Leistungsannahme, aber keinen separaten Verdichterzustand. Ein kleiner
    Pumpenvorlauf bleibt so von einer echten Annahme unterscheidbar.
    """
    measured_ts = time.time() if now_ts is None else float(now_ts)
    status["wp_evidence_schema"] = "shelly_3em_heatpump_evidence_v1"
    status["wp_evidence_provider"] = "shelly_3em"
    status["wp_evidence_valid"] = False
    status["wp_evidence_ts"] = None
    status["wp_measurement_source"] = "shelly_3em_rpc"
    status["wp_power_known"] = False
    status["wp_power_w"] = None
    status["wp_start_progress_observed"] = False
    status["wp_acceptance_stage"] = "measurement_unavailable"
    status["wp_accepting_power"] = False
    status["wp_compressor_observation_valid"] = False
    status["wp_compressor_running"] = False
    status["wp_is_running"] = False

    s3_data = shelly_3em_read(s3_ip)
    if not isinstance(s3_data, dict):
        return False
    total_w = _strict_finite_number(s3_data.get("total_w"))
    if total_w is None or total_w < 0.0:
        return False

    actual_w = int(round(total_w))
    accepting_power = bool(actual_w >= max(1, int(math.ceil(s3_min_w))))
    start_progress = bool(0 < actual_w < max(1, int(math.ceil(s3_min_w))))
    status["wp_evidence_valid"] = True
    status["wp_evidence_ts"] = measured_ts
    status["wp_power_known"] = True
    status["wp_power_w"] = actual_w
    status["wp_start_progress_observed"] = start_progress
    status["wp_acceptance_stage"] = (
        "configured_load_floor_reached"
        if accepting_power
        else ("pump_prelude_or_standby" if start_progress else "no_power")
    )
    status["wp_accepting_power"] = accepting_power
    status["wp_is_running"] = accepting_power
    phase_a_w = _strict_finite_number(s3_data.get("phase_a_w"))
    phase_b_w = _strict_finite_number(s3_data.get("phase_b_w"))
    phase_c_w = _strict_finite_number(s3_data.get("phase_c_w"))
    status["wp_phase_a_w"] = phase_a_w
    status["wp_phase_b_w"] = phase_b_w
    status["wp_phase_c_w"] = phase_c_w
    status["wp_phase_a_v"] = _strict_finite_number(s3_data.get("phase_a_v"))
    status["wp_phase_b_v"] = _strict_finite_number(s3_data.get("phase_b_v"))
    status["wp_phase_c_v"] = _strict_finite_number(s3_data.get("phase_c_v"))
    status["wp_freq_hz"] = _strict_finite_number(s3_data.get("freq_hz"))
    status["shelly_3em_ip"] = s3_ip
    print(f"  [3EM] WP={actual_w:.0f}W "
          f"(A:{float(phase_a_w or 0):.0f}W "
          f"B:{float(phase_b_w or 0):.0f}W "
          f"C:{float(phase_c_w or 0):.0f}W) "
          f"{'[LASTANNAHME]' if accepting_power else '[STAND/PUMPENVORLAUF]'}")
    return True


def update_shelly_3em_relay_evidence(
    status,
    relay_state,
    *,
    now_ts=None,
    source="shelly_3em_switch_get_status",
):
    """Bindet ausschließlich einen echten Switch-Readback als Relaiszustand."""
    read_ts = time.time() if now_ts is None else float(now_ts)
    valid = type(relay_state) is bool
    status["wp_relay_readback_valid"] = valid
    status["wp_relay_readback_on"] = relay_state if valid else None
    status["wp_relay_readback_ts"] = read_ts if valid else None
    status["wp_relay_readback_source"] = str(source) if valid else ""
    status["wp_budget_signal_readback_ts"] = read_ts if valid else None
    status["wp_budget_signal_readback_source"] = str(source) if valid else ""
    status["wp_budget_signal_active_confirmed"] = bool(
        valid and relay_state is True
    )
    status["wp_budget_withdrawal_confirmed"] = bool(
        valid and relay_state is False
    )
    status["wp_budget_withdrawal_ts"] = (
        read_ts if valid and relay_state is False else None
    )
    status["wp_budget_withdrawal_source"] = (
        str(source) if valid and relay_state is False else ""
    )
    if valid:
        status["wp_relay_on"] = bool(relay_state)
    return valid


def reconcile_shelly_3em_relay_state(
    status,
    hs_state,
    relay_state,
    *,
    now_ts=None,
):
    """Synchronisiert Prozesszustand konservativ aus frischem Hardware-Readback.

    Ist das Relais beim Prozessstart bereits EIN, ist sein echtes Einschaltalter
    unbekannt. Dann beginnt genau für diesen Prozesszustand eine neue lokale
    Mindestlaufzeit, statt das Relais sofort oder doppelt zu schalten.
    """
    if type(relay_state) is not bool:
        status["wp_relay_state_reconciled"] = False
        status["wp_relay_runtime_evidence"] = "readback_unavailable"
        return False

    observed_ts = time.time() if now_ts is None else float(now_ts)
    prior_known = type(hs_state.get("s3em_on")) is bool
    prior_on = bool(hs_state.get("s3em_on", False))
    changed = bool(not prior_known or prior_on != relay_state)
    status["wp_relay_state_reconciled"] = changed

    if relay_state:
        if not prior_known or not prior_on:
            hs_state["s3em_last_on_ts"] = observed_ts
            hs_state["s3em_restart_rearm_count"] = 1
            status["wp_relay_runtime_evidence"] = (
                "restart_conservative_min_runtime_rearm"
                if not prior_known
                else "external_on_readback_rearm"
            )
        else:
            status["wp_relay_runtime_evidence"] = "confirmed_on_readback"
        hs_state["s3em_on"] = True
        hs_state["s3em_auto_off_confirmed"] = False
    else:
        if not prior_known:
            # Der physische AUS-Zustand ist belegt, sein Alter nach einem
            # Prozessneustart aber nicht. Deshalb beginnt die lokale
            # Wiedereinschaltsperre konservativ neu.
            hs_state["s3em_last_off_ts"] = observed_ts
            hs_state["s3em_restart_rearm_count"] = 1
            status["wp_relay_runtime_evidence"] = (
                "restart_conservative_restart_block_rearm"
            )
        elif prior_on:
            hs_state["s3em_last_off_ts"] = observed_ts
            status["wp_relay_runtime_evidence"] = "external_off_readback"
        else:
            status["wp_relay_runtime_evidence"] = "confirmed_off_readback"
        hs_state["s3em_on"] = False
        if prior_known:
            hs_state["s3em_restart_rearm_count"] = 0
    return True


def heat_actuator_context_readiness(safety_gate=None):
    """Prüft den Aktorkontext read-only, ohne eine Writer-Lease zu erwerben."""
    try:
        gate = safety_gate or _heater_actuator_gate()
        verdict = gate.context_validator()
        return bool(getattr(verdict, "valid", False)), str(
            getattr(verdict, "reason", "context_invalid")
        )
    except Exception as exc:
        return False, f"context_error:{type(exc).__name__}"


def update_shelly_3em_provider_contract(
    status,
    hs_state,
    *,
    global_auto,
    s3_enable,
    s3_relay,
    s3_min_w,
    s3_max_w,
    soc_ok,
    restart_left_s,
    wallbox_transition,
    actuator_context_valid,
    actuator_context_reason,
    authorized_budget_w,
):
    """Publiziert den typisierten Pro3EM-Start- und Readbackvertrag."""
    start_floor_w = max(
        int(CONSUMER_MIN_W.get("heatpump", 1500)),
        int(math.ceil(max(0.0, float(s3_min_w)))),
    )
    max_w = int(math.ceil(max(0.0, float(s3_max_w))))
    config_valid = bool(s3_min_w > 0 and max_w >= start_floor_w)
    command_eligible = bool(
        global_auto
        and s3_enable
        and s3_relay >= 0
        and config_valid
        and actuator_context_valid
    )
    relay_valid = status.get("wp_relay_readback_valid") is True
    relay_on = status.get("wp_relay_readback_on") is True
    blockers = []
    if not global_auto:
        blockers.append("heatpump_auto_off")
    if not s3_enable:
        blockers.append("shelly_3em_relay_control_disabled")
    if s3_relay < 0:
        blockers.append("shelly_3em_relay_id_invalid")
    if not config_valid:
        blockers.append("shelly_3em_power_contract_invalid")
    if status.get("wp_evidence_valid") is not True:
        blockers.append("shelly_3em_power_evidence_invalid")
    if not relay_valid:
        blockers.append("shelly_3em_relay_readback_invalid")
    elif relay_on:
        blockers.append("shelly_3em_relay_already_on")
    if status.get("wp_accepting_power") is True:
        blockers.append("heatpump_already_accepting_power")
    if not soc_ok:
        blockers.append("heatpump_soc_protection")
    if restart_left_s > 0.0:
        blockers.append("heatpump_restart_block")
    if wallbox_transition:
        blockers.append("wallbox_phase_transition_active")
    if not actuator_context_valid:
        blockers.append(
            "heatpump_actuator_context_invalid:"
            + str(actuator_context_reason or "unknown")
        )

    start_ready = not blockers
    status["wp_budget_actuator_owner"] = "heizstab_manager:shelly_3em_relay"
    status["wp_budget_signal_semantics"] = "start_recommendation_not_acceptance"
    # Bis 120 s reale Luxtronik-Anlaufzeit plus 30 s Diagnose-/Zyklusreserve.
    status["wp_budget_startup_grace_s"] = 150
    status["wp_budget_command_eligible"] = command_eligible
    status["wp_budget_start_floor_w"] = start_floor_w
    status["wp_budget_device_max_w"] = max_w
    status["wp_budget_authorized_w"] = max(0, int(authorized_budget_w or 0))
    status["wp_budget_offered"] = status["wp_budget_authorized_w"] > 0
    status["wp_budget_start_ready"] = start_ready
    status["wp_budget_start_request_w"] = max_w if start_ready else 0
    status["wp_budget_start_blockers"] = blockers
    return status


def shelly_get_state(ip):
    """
    Liest Status und gemessene Leistung des Shelly Plug.
    Gibt dict zurueck: {'on': bool, 'power_w': float} oder None bei Fehler.
    Unterstuetzt Shelly Gen1 (/relay/0 + /meter/0) und Gen2 (/rpc/Switch.GetStatus).
    """
    # Gen1 Versuch
    try:
        url = f"http://{ip}/relay/0"
        with urllib.request.urlopen(url, timeout=SHELLY_TIMEOUT) as r:
            relay = json.loads(r.read().decode())
        if (
            not isinstance(relay, dict)
            or type(relay.get("ison")) is not bool
        ):
            raise ValueError("Gen1 relay response has no ison field")
        is_on = relay["ison"]

        # Leistung (meter)
        power = None
        try:
            url2 = f"http://{ip}/meter/0"
            with urllib.request.urlopen(url2, timeout=SHELLY_TIMEOUT) as r2:
                meter = json.loads(r2.read().decode())
            power = _strict_finite_number(meter.get("power"))
        except Exception:
            pass
        return {"on": is_on, "power_w": power, "gen": 1}
    except Exception:
        pass

    # Gen2 Versuch (Shelly Plus/Pro)
    try:
        url = f"http://{ip}/rpc/Switch.GetStatus?id=0"
        with urllib.request.urlopen(url, timeout=SHELLY_TIMEOUT) as r:
            data = json.loads(r.read().decode())
        if (
            not isinstance(data, dict)
            or type(data.get("output")) is not bool
        ):
            return None
        return {
            "on":      data["output"],
            "power_w": _strict_finite_number(data.get("apower")),
            "gen":     2,
        }
    except Exception as e:
        print(f"  [!] Shelly {ip} nicht erreichbar: {e}")
        return None


def shelly_set_state(ip, on: bool, safety_gate=None):
    """Schaltet Shelly Plug ein oder aus (Gen1 und Gen2)."""
    turn = "on" if on else "off"
    driver_key = f"transport:http-shelly:{ip}:switch:0"

    # Gen1
    if _authorize_heater_output(
        driver_key,
        f"shelly-gen1:{turn}",
        safety_gate=safety_gate,
        safe_release=not on,
    ):
        try:
            url = f"http://{ip}/relay/0?turn={turn}"
            with urllib.request.urlopen(url, timeout=SHELLY_TIMEOUT):
                pass
            confirmed = shelly_get_state(ip)
            if confirmed is not None and confirmed.get("on") == bool(on):
                return True
        except Exception:
            pass

    # Gen2
    if not _authorize_heater_output(
        driver_key,
        f"shelly-gen2:{turn}",
        safety_gate=safety_gate,
        safe_release=not on,
    ):
        return False
    try:
        body = json.dumps({"id": 0, "on": on}).encode()
        req = urllib.request.Request(
            f"http://{ip}/rpc/Switch.Set",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=SHELLY_TIMEOUT):
            pass
        confirmed = shelly_get_state(ip)
        if confirmed is None or confirmed.get("on") != bool(on):
            print(
                f"  [!] Shelly {ip}: Readback unbestätigt "
                f"(Soll={bool(on)} Ist={None if confirmed is None else confirmed.get('on')})"
            )
            return False
        return True
    except Exception as e:
        print(f"  [!] Shelly {ip} Schalten fehlgeschlagen: {e}")
        return False


# ---------------------------------------------------------------------------
# PV-Ueberschuss-Regelung
# ---------------------------------------------------------------------------

def calc_netpoint_surplus(live, current_hs_power_w=0, cfg=None):
    """Berechnet echten Ueberschuss am Netzpunkt mit kleiner Einspeise-Reserve."""
    cfg = cfg or {}
    try:
        grid_guard_w = max(0, int(float(cfg.get("hs_grid_guard_w", 100) or 0)))
    except Exception:
        grid_guard_w = 100

    try:
        return max(
            0,
            -int(float(live.get("grid_w", 0) or 0)) + int(float(current_hs_power_w or 0)) - grid_guard_w
        )
    except Exception:
        return 0


def _strict_consumer_budget_map(value):
    if not isinstance(value, dict) or set(value) != set(_CONSUMER_BUDGET_KEYS):
        return None
    if any(type(value[key]) is not int or value[key] < 0 for key in _CONSUMER_BUDGET_KEYS):
        return None
    return {key: value[key] for key in _CONSUMER_BUDGET_KEYS}


def _consumer_budget_sha256_id(value):
    return bool(
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _heater_consumer_budget_envelope_contract(
    budget,
    *,
    consumer=None,
    now_ts=None,
    file_age_s=None,
):
    """Liest einen Heizverbraucheranteil nur aus dem vollständigen v2-Vertrag.

    Die Dateizeit ist lediglich eine zusätzliche Transportprüfung. Freigaben
    bleiben an den producerseitigen Zeitstempel, die versiegelte Identity und
    den vollständigen Payload-Hash gebunden; ein berührtes oder teilweise
    manipuliertes Ramdisk-JSON kann damit keine Heizleistung freigeben.
    """

    result = {
        "valid": False,
        "reason": "consumer_budget_missing",
        "allocation_w": 0,
    }
    consumer_key = str(consumer or "").strip().lower()
    if consumer_key and consumer_key not in ("heatpump", "heater"):
        return {**result, "reason": "consumer_budget_consumer_invalid"}
    data = budget if isinstance(budget, dict) else {}
    if data.get("schema_version") != "wb_pv_budget_control_v2":
        return {**result, "reason": "consumer_budget_schema_invalid"}
    now_value = time.time() if now_ts is None else now_ts
    if (
        isinstance(now_value, bool)
        or not isinstance(now_value, (int, float))
        or not math.isfinite(float(now_value))
    ):
        return {**result, "reason": "consumer_budget_now_invalid"}
    source_ts = data.get("ts")
    if type(source_ts) is not int or source_ts <= 0:
        return {**result, "reason": "consumer_budget_timestamp_invalid"}
    age_s = float(now_value) - float(source_ts)
    if age_s < 0.0:
        return {**result, "reason": "consumer_budget_timestamp_future"}
    if age_s > HEATER_CONSUMER_BUDGET_MAX_AGE_S:
        return {**result, "reason": "consumer_budget_timestamp_stale"}
    if file_age_s is not None:
        if (
            isinstance(file_age_s, bool)
            or not isinstance(file_age_s, (int, float))
            or not math.isfinite(float(file_age_s))
            or float(file_age_s) < 0.0
            or float(file_age_s) > HEATER_CONSUMER_BUDGET_MAX_AGE_S
        ):
            return {**result, "reason": "consumer_budget_file_age_invalid"}

    validation = validate_consumer_budget_contract(
        data.get("consumer_budget_contract")
    )
    command_validation = validate_consumer_command_allocations(
        data.get("consumer_budget_contract")
    )
    if validation.get("valid") is not True:
        return {**result, "reason": "consumer_budget_contract_invalid"}
    if command_validation.get("valid") is not True:
        return {**result, "reason": "consumer_budget_command_invalid"}
    accounting = _strict_consumer_budget_map(validation.get("allocations"))
    allocations = _strict_consumer_budget_map(command_validation.get("allocations"))
    if accounting is None or allocations is None:
        return {**result, "reason": "consumer_budget_allocations_invalid"}
    energy_score = data.get("energy_score") if isinstance(data.get("energy_score"), dict) else {}
    if type(data.get("consumer_total_budget_w")) is not int or data.get(
        "consumer_total_budget_w"
    ) != validation.get("total_budget_w"):
        return {**result, "reason": "consumer_budget_total_mismatch"}
    for container in (data, energy_score):
        if _strict_consumer_budget_map(container.get("consumer_allocations")) != allocations:
            return {**result, "reason": "consumer_budget_command_scope_mismatch"}
        if _strict_consumer_budget_map(container.get("consumer_accounting_allocations")) != accounting:
            return {**result, "reason": "consumer_budget_accounting_scope_mismatch"}

    identity = data.get("producer_identity")
    if not isinstance(identity, dict) or frozenset(identity) != _CONSUMER_BUDGET_IDENTITY_KEYS:
        return {**result, "reason": "consumer_budget_identity_keys_invalid"}
    if identity.get("schema_version") != _CONSUMER_BUDGET_IDENTITY_SCHEMA:
        return {**result, "reason": "consumer_budget_identity_schema_invalid"}
    if identity.get("decision_ts") != source_ts:
        return {**result, "reason": "consumer_budget_identity_timestamp_invalid"}
    if (
        identity.get("decision_owner") != "storage_manager"
        or identity.get("decision_effect") != "flexible_consumer_power_budget"
        or not isinstance(identity.get("decision_state"), str)
        or not str(identity.get("decision_state")).strip()
        or type(identity.get("decision_mode")) is not int
        or type(identity.get("decision_value_w")) is not int
        or identity.get("decision_value_w") < 0
        or type(identity.get("decision_protected")) is not bool
    ):
        return {**result, "reason": "consumer_budget_identity_contract_invalid"}
    for key, allocation_key in (
        ("authorized_wallbox_budget_w", "wallbox"),
        ("authorized_heatpump_budget_w", "heatpump"),
        ("authorized_heater_budget_w", "heater"),
    ):
        if type(identity.get(key)) is not int or identity.get(key) != allocations[allocation_key]:
            return {**result, "reason": "consumer_budget_identity_allocation_mismatch"}
    if type(data.get("budget_w")) is not int or data.get("budget_w") != allocations["wallbox"]:
        return {**result, "reason": "consumer_budget_wallbox_scope_mismatch"}
    binding_values = tuple(identity.get(key) for key in ("plan_id", "slot_id", "action_id"))
    if identity.get("binding_status") == "bound":
        binding_valid = all(_consumer_budget_sha256_id(value) for value in binding_values)
    elif identity.get("binding_status") == _CONSUMER_BUDGET_IDENTITY_NOT_APPLICABLE:
        binding_valid = all(value == _CONSUMER_BUDGET_IDENTITY_NOT_APPLICABLE for value in binding_values)
    else:
        binding_valid = False
    if not binding_valid:
        return {**result, "reason": "consumer_budget_plan_binding_invalid"}
    generation = identity.get("decision_generation")
    if not _consumer_budget_sha256_id(generation):
        return {**result, "reason": "consumer_budget_generation_invalid"}
    if generation != revision_hash({key: value for key, value in identity.items() if key != "decision_generation"}):
        return {**result, "reason": "consumer_budget_generation_mismatch"}
    revision = data.get("budget_revision")
    if not _consumer_budget_sha256_id(revision):
        return {**result, "reason": "consumer_budget_revision_invalid"}
    if revision != revision_hash({key: value for key, value in data.items() if key != "budget_revision"}):
        return {**result, "reason": "consumer_budget_revision_mismatch"}
    return {
        "valid": True,
        "reason": "consumer_budget_fresh_bound",
        "allocations": dict(allocations),
        "allocation_w": allocations[consumer_key] if consumer_key else None,
        "budget_revision": revision,
    }


def _read_consumer_budget_envelope_snapshot(*, now_ts=None):
    """Liest und validiert beide Heizanteile aus genau einer Dateigeneration.

    ``wb_pv_budget.json`` wird atomar ersetzt. Ändert sich der Dateiknoten
    während des Lesens, wird nicht auf einen zweiten Stand nachgelesen,
    sondern der gesamte Heizzyklus bleibt fail-closed. So können Heizstab und
    Shelly-3EM-Wärmepumpe niemals Anteile unterschiedlicher Revisionen sehen.
    """

    result = {
        "valid": False,
        "reason": "consumer_budget_snapshot_missing",
        "allocations": {key: 0 for key in _CONSUMER_BUDGET_KEYS},
        "budget_revision": None,
    }
    now_value = time.time() if now_ts is None else now_ts
    try:
        before = os.stat(WB_BUDGET_FILE)
        with open(WB_BUDGET_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        after = os.stat(WB_BUDGET_FILE)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return result
    before_generation = (before.st_ino, before.st_size, before.st_mtime_ns)
    after_generation = (after.st_ino, after.st_size, after.st_mtime_ns)
    if before_generation != after_generation:
        return {**result, "reason": "consumer_budget_snapshot_changed_during_read"}
    envelope = _heater_consumer_budget_envelope_contract(
        payload,
        now_ts=now_value,
        file_age_s=float(now_value) - float(before.st_mtime),
    )
    if envelope.get("valid") is not True:
        return {**result, "reason": str(envelope.get("reason") or "consumer_budget_snapshot_invalid")}
    allocations = _strict_consumer_budget_map(envelope.get("allocations"))
    if allocations is None:
        return {**result, "reason": "consumer_budget_snapshot_allocations_invalid"}
    return {
        "valid": True,
        "reason": "consumer_budget_snapshot_fresh_bound",
        "allocations": allocations,
        "budget_revision": envelope.get("budget_revision"),
        "wallbox_phase_transition_active": payload.get(
            "wallbox_phase_transition_active"
        ) is True,
        "wallbox_phase_transition_until_ts": payload.get(
            "wallbox_phase_transition_until_ts",
            0,
        ),
    }


def calc_surplus(live, current_hs_power_w=0, cfg=None, *, consumer="heater"):
    """
    Liest genau einen absoluten Anteil des gemeinsamen Verbraucherbudgets.

    Heizstab und Pro3EM-Wärmepumpe sind getrennte Verbraucher. Weder eine
    Netzmessung noch der jeweils andere Teilrahmen darf hier ein zweites Budget
    erzeugen.
    """
    consumer_key = str(consumer or "").strip().lower()
    if consumer_key not in ("heatpump", "heater"):
        return 0
    snapshot = _read_consumer_budget_envelope_snapshot(now_ts=time.time())
    if snapshot.get("valid") is not True:
        return 0
    allocations = _strict_consumer_budget_map(snapshot.get("allocations"))
    if allocations is None:
        return 0
    # Der zentrale Vertrag hat Startminima und laufende, gegebenenfalls
    # darunter modulierende Istlasten bereits typisiert. Ein zweiter lokaler
    # Min-Filter würde eine gültige 1-kW-Run-on-Allokation verfälschen.
    return max(0, allocations[consumer_key])

def wallbox_phase_transition_active(
    now_ts=None,
    max_age_s=30.0,
    budget_snapshot=None,
):
    """Liefert nur bei einer frischen, explizit aktiven Wallboxtransition True."""
    now_value = time.time() if now_ts is None else float(now_ts)
    snapshot = budget_snapshot if isinstance(budget_snapshot, dict) else None
    if snapshot is not None:
        if snapshot.get("valid") is not True:
            return False
        active = snapshot.get("wallbox_phase_transition_active") is True
        expires_raw = snapshot.get("wallbox_phase_transition_until_ts", 0)
        if isinstance(expires_raw, bool) or not isinstance(expires_raw, (int, float)):
            return False
        expires_ts = float(expires_raw)
        if not math.isfinite(expires_ts):
            return False
        return bool(active and (expires_ts <= 0.0 or now_value <= expires_ts))
    try:
        if not os.path.exists(WB_BUDGET_FILE):
            return False
        if now_value - os.path.getmtime(WB_BUDGET_FILE) > max(1.0, float(max_age_s)):
            return False
        with open(WB_BUDGET_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return False
        nested = data.get("wallbox_phase_transition")
        nested = nested if isinstance(nested, dict) else {}
        active = bool(data.get("wallbox_phase_transition_active") or nested.get("active"))
        expires_ts = float(
            data.get(
                "wallbox_phase_transition_until_ts",
                nested.get("expires_ts", 0),
            )
            or 0
        )
        return bool(active and (expires_ts <= 0 or now_value <= expires_ts))
    except Exception:
        return False


def shelly_wp_relay_should_run(
    *,
    wp_is_on,
    soc_ok,
    surplus_w,
    threshold_on_w,
    threshold_off_w,
    wp_min_w,
    wallbox_transition,
):
    """Erhält laufende Teilallokationen exakt und blockiert Unterflurstarts."""
    if wp_is_on:
        # Der zentrale Vertrag hat die laufende Last und ihre Hysterese bereits
        # bewertet. Jede positive gültige Teilallokation bleibt wirksam; ein
        # zweiter lokaler Mindestfilter würde sie verfälschen.
        return bool(soc_ok and (wallbox_transition or surplus_w > 0))
    return bool(
        not wallbox_transition
        and soc_ok
        and surplus_w >= max(wp_min_w, threshold_on_w)
    )


def read_predump_heater_budget(cfg):
    """Liest die Pre-Dump-Freigabe fuer Heizstab/Shelly."""
    try:
        if str(cfg.get("predump_heater_enable", 0)).strip().lower() not in ("1", "true", "yes", "on"):
            return 0
        if not os.path.exists(PREDUMP_PLAN_FILE):
            return 0
        with open(PREDUMP_PLAN_FILE, "r", encoding="utf-8") as f:
            plan = json.load(f)
        if not (plan.get("enabled") and plan.get("active")):
            return 0
        if int(time.time()) > int(plan.get("expires_ts", 0) or 0):
            return 0
        if not (plan.get("allow") or {}).get("heater", False):
            return 0
        return max(0, int(float(plan.get("budget_w", 0) or 0)))
    except Exception:
        return 0


def read_storage_market_plan(max_age_s=1800):
    """Liest den Storage-Manager-Marktvertrag fuer externe Verbraucher."""
    try:
        if not os.path.exists(STORAGE_PLAN_FILE):
            return {}
        if time.time() - os.path.getmtime(STORAGE_PLAN_FILE) > max_age_s:
            return {}
        with open(STORAGE_PLAN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_market_heater_release(cfg):
    """Freigabe fuer Heizstab/Shelly aus dem Storage-Manager-Marktvertrag."""
    try:
        ctx = current_market_consumer_release(read_storage_market_plan(), "heater", cfg)
        return ctx if isinstance(ctx, dict) else {"allowed": False, "reason": "market_plan_error"}
    except Exception:
        return {"allowed": False, "reason": "market_plan_error"}


def market_heater_budget(cfg, has_hs, has_sh, hs_max_w, sh_nominal_w):
    """Berechnet das maximal nutzbare Heizstab-/Shelly-Budget im Marktfenster."""
    ctx = read_market_heater_release(cfg)
    if not bool(ctx.get("allowed")):
        return 0, ctx
    budget_w = 0
    if has_hs:
        budget_w += max(0, int(round(hs_max_w)))
    if has_sh:
        budget_w += max(0, int(round(sh_nominal_w)))
    if budget_w <= 0:
        ctx = dict(ctx)
        ctx["allowed"] = False
        ctx["reason"] = "heater_not_configured"
        return 0, ctx
    return budget_w, ctx


def cfg_bool(cfg, key, default=False):
    raw = cfg.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "ja", "ein")


def _market_release_end_ts_s(release):
    contract = release.get("contract") if isinstance(release, dict) else None
    if not isinstance(contract, dict):
        return None
    try:
        end_ms = float(contract.get("end_ts", 0) or 0)
        return end_ms / 1000.0 if end_ms > 0 else None
    except Exception:
        return None


def _heater_temperature_context(cfg, live, status):
    temp = None
    if isinstance(status, dict) and status.get("elwa_water_temp_c") is not None:
        temp = status.get("elwa_water_temp_c")
    elif isinstance(live, dict) and live.get("heater_temp_c") is not None:
        temp = live.get("heater_temp_c")
    elif str(cfg.get("heat_heater_temperature_c", "")).strip() != "":
        temp = cfg.get("heat_heater_temperature_c")
    try:
        if temp is None or temp == "":
            raise ValueError
        temp_c = float(str(temp).replace(",", "."))
        return True, temp_c
    except Exception:
        return False, None


def _heat_user_fallback_kwh(cfg):
    for key in ("heat_wp_daily_kwh", "wp_forecast_daily_kwh", "wp_daily_need_kwh", "wp_energy_need_kwh"):
        raw = str(cfg.get(key, "")).strip()
        if raw:
            try:
                return max(0.0, float(raw.replace(",", ".")))
            except Exception:
                return None
    return None


def build_heater_policy_decision(
    cfg,
    live,
    status,
    *,
    auto_mode,
    has_heat_device,
    pv_budget_w,
    predump_budget_w,
    raw_market_budget_w,
    market_ctx,
    hs_max_w,
    is_currently_on,
    min_surplus_w,
    threshold_off_w,
    expensive_price_window_active=False,
    battery_empty=False,
    price_block_started_ts=0.0,
    price_block_limit_ct=45.0,
):
    """Build the central heat policy decision used by the heater manager."""

    now_ts = time.time()
    temp_valid, temp_c = _heater_temperature_context(cfg, live, status)
    forecast_result = heat_forecast.predict_wp_energy_need_kwh(
        forecast_temp_c=None,
        now_ts=now_ts,
        user_fallback_kwh=_heat_user_fallback_kwh(cfg),
    )
    deficit_kwh = heat_forecast.calculate_heat_deficit_kwh(forecast_result.need_kwh)
    price_ct = live.get("price_ct") if isinstance(live, dict) else None
    if price_ct is None and isinstance(market_ctx, dict) and market_ctx.get("negative_price"):
        price_ct = -1.0
    low_price_window_active = bool(raw_market_budget_w > 0 or (isinstance(market_ctx, dict) and market_ctx.get("active")))
    max_grid_boost_w = min(
        max(0.0, float(raw_market_budget_w or 0)),
        cfg_float(cfg, "heat_heater_grid_boost_max_w", hs_max_w, min_value=0),
    )
    if max_grid_boost_w <= 0:
        max_grid_boost_w = cfg_float(cfg, "heat_heater_grid_boost_max_w", hs_max_w, min_value=0)
    ctx = heat_policy.HeatPolicyInput(
        now_ts=now_ts,
        auto_enabled=bool(auto_mode),
        heat_enabled=bool(has_heat_device),
        heatpump_configured=False,
        heater_configured=bool(has_heat_device),
        pv_available_budget_w=max(0.0, float(pv_budget_w or 0)),
        pv_start_w=max(0.0, float(min_surplus_w or 0)),
        pv_stop_w=max(0.0, float(threshold_off_w or 0)),
        pv_hysteresis_active=bool(is_currently_on),
        predump_available_budget_w=max(0.0, float(predump_budget_w or 0)),
        low_price_window_active=low_price_window_active,
        expensive_price_window_active=bool(expensive_price_window_active),
        price_quality_valid=price_ct is not None,
        current_price_ct=price_ct,
        price_window_end_ts=_market_release_end_ts_s(market_ctx if isinstance(market_ctx, dict) else {}),
        price_pain_limit_ct=price_block_limit_ct,
        battery_empty=bool(battery_empty),
        price_block_started_ts=price_block_started_ts if price_block_started_ts and price_block_started_ts > 0 else None,
        # Beim Heizstab ist der bestehende Preisblock weiterhin Teil des
        # ausdrücklich separat durch `heat_policy_runtime_enable`
        # aktivierten Aktorpfads. Die Hardwarewirkung wird im Regelzyklus
        # zusätzlich an genau diesen Schalter gebunden.
        price_block_control_authorized=True,
        forecast_need_kwh=forecast_result.need_kwh,
        forecast_deficit_kwh=deficit_kwh,
        forecast_valid=bool(forecast_result.valid and not forecast_result.stale),
        forecast_source=forecast_result.source,
        forecast_quality=forecast_result.quality,
        heater_grid_boost_enable=cfg_bool(cfg, "heat_heater_grid_boost_enable", False),
        heater_grid_boost_ack=cfg_bool(cfg, "heat_heater_grid_boost_ack", False),
        heater_grid_boost_requires_deficit=cfg_bool(cfg, "heat_heater_grid_boost_requires_deficit", True),
        heater_grid_boost_max_w=max_grid_boost_w,
        heater_grid_boost_price_limit_ct=cfg_float(cfg, "heat_heater_grid_boost_price_limit_ct", 0.0),
        temperature_valid=temp_valid,
        temperature_c=temp_c,
        temperature_min_c=cfg_float(cfg, "heat_heater_min_temp_c", 45.0),
        temperature_max_c=cfg_float(cfg, "heat_heater_max_temp_c", 60.0),
    )
    return heat_policy.decide_heat_policy(ctx), forecast_result


def read_manual_override():
    """Liest eine explizite Heizstab-Handfreigabe aus der WebUI."""
    try:
        if not os.path.exists(HS_MANUAL_FILE):
            return None
        with open(HS_MANUAL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        mode = str(data.get("mode", "")).strip().lower()
        if mode not in ("full", "off"):
            return None
        expires_ts = int(float(data.get("expires_ts", 0) or 0))
        if expires_ts > 0 and int(time.time()) > expires_ts:
            try:
                os.unlink(HS_MANUAL_FILE)
            except Exception:
                pass
            return None
        return data
    except Exception:
        return None


def control_cycle(cfg, live, hs_state, safety_gate=None):
    """
    Haupt-Regelzyklus: entscheidet ob und mit wieviel Leistung aktiviert wird.
    Unterstuetzt heizstab_type = generic (Standard) und mypv_elwa (myPV AC ELWA-E).
    hs_state: persistenter Zustand zwischen Zyklen {'is_on': bool, 'current_w': float}
    Gibt ein Status-Dict zurueck das in heizstab_data.json landet.
    """
    global_auto   = str(cfg.get("auto_mode", "1")).lower() in ("1", "true", "yes")
    local_auto    = str(cfg.get("hs_auto_mode", "1")) == "1"
    auto_mode     = global_auto and local_auto
    heat_policy_runtime_enabled = cfg_bool(cfg, "heat_policy_runtime_enable", False)
    min_surplus_w = cfg_float(cfg, "hs_min_surplus_w", 500, min_value=0)
    min_soc       = cfg_float(cfg, "hs_min_soc", 20, min_value=0)
    hs_ip         = str(cfg.get("heizstab_ip", "0.0.0.0")).strip()
    hs_port       = cfg.get("heizstab_port", "502")
    hs_max_w      = cfg_float(cfg, "heizstab_max_w", 3000, min_value=0)
    hs_type       = str(cfg.get("heizstab_type", "generic")).strip().lower()
    sh_ip         = str(cfg.get("shelly_heiz_ip", "0.0.0.0")).strip()
    sh_nominal_w  = cfg_float(cfg, "shelly_heiz_w", 1500, min_value=0)
    s3_ip         = str(cfg.get("shelly_3em_ip", "0.0.0.0")).strip()
    s3_relay      = cfg_int(cfg, "shelly_3em_relay_id", "-1")
    s3_enable     = str(cfg.get("shelly_3em_enable", "0")).strip().lower() in ("1", "true", "yes", "on")
    s3_min_w      = cfg_float(cfg, "shelly_3em_wp_min_w", "1000", min_value=0)
    s3_max_w      = cfg_float(cfg, "shelly_3em_wp_max_w", "3000", min_value=0)
    s3_min_runtime_s = cfg_float(cfg, "wp_min_runtime_min", 30, min_value=0) * 60.0
    s3_restart_block_s = cfg_float(cfg, "wp_restart_block_min", 20, min_value=0) * 60.0
    wp_type       = str(cfg.get("wp_type", "0")).strip()
    heater_module_enabled = wp_type == "2" or cfg_bool(cfg, "heizstab", False)
    has_s3em      = wp_type == "3" and s3_ip not in ("", "0.0.0.0")
    s3_relay_auto_mode = global_auto and s3_enable and s3_relay >= 0

    hs_configured = hs_ip not in ("", "0.0.0.0")
    sh_configured = sh_ip not in ("", "0.0.0.0")
    has_hs  = heater_module_enabled and hs_configured
    has_sh  = heater_module_enabled and sh_configured
    is_elwa = hs_type == "mypv_elwa"
    elwa_status_code = None

    # Der Netzpunkt bleibt Diagnose. Die produktive Leistung kommt ausschließlich
    # aus der versiegelten Command-Allokation des zentralen Verbraucherbudgets.
    # Ein im vorherigen Zyklus laufender Heizstab darf nach heizstab=0 nicht
    # als vermeintlich frei werdender Überschuss eine separate WP starten.
    current_hs_w = hs_state.get('current_w', 0) if heater_module_enabled and auto_mode else 0
    netpoint_surplus_w = calc_netpoint_surplus(live, current_hs_power_w=current_hs_w, cfg=cfg)
    consumer_budget_snapshot = _read_consumer_budget_envelope_snapshot(
        now_ts=time.time()
    )
    consumer_allocations = _strict_consumer_budget_map(
        consumer_budget_snapshot.get("allocations")
    )
    if consumer_budget_snapshot.get("valid") is not True or consumer_allocations is None:
        consumer_allocations = {key: 0 for key in _CONSUMER_BUDGET_KEYS}
    surplus_w = consumer_allocations["heater"]
    s3_surplus_w = consumer_allocations["heatpump"] if has_s3em else 0
    predump_heater_budget_w = read_predump_heater_budget(cfg)
    raw_market_heater_budget_w, market_heater_ctx = market_heater_budget(
        cfg,
        has_hs,
        has_sh,
        hs_max_w,
        sh_nominal_w,
    )
    market_heater_budget_w = 0
    soc       = live["soc"]
    pv_w      = live["pv_w"]
    soc_ok = soc >= min_soc

    # Hysterese-Schwellen: Einschalten >= min_surplus, Ausschalten < (min_surplus - deadband)
    is_currently_on     = hs_state.get('is_on', False)
    threshold_on_w      = min_surplus_w
    threshold_off_w     = max(0, min_surplus_w - HS_HYSTERESIS_W)

    status = {
        "Heizstab_Power":   None,
        "hs_actual_w":       None,
        "hs_target_w":       0,
        "hs_requested_w":    0,
        "hs_active":         None,
        "budget_start_ready": False,
        "budget_start_request_w": 0,
        "heizstab_type":    hs_type,
        "shelly_heiz_on":   None,
        "shelly_heiz_w":    None,
        "wp_power_w":       None,
        "wp_is_running":    False,
        "wp_relay_on":      False,
        "wp_takt_protect_active": False,
        "wp_relay_auto_mode": s3_relay_auto_mode,
        "wp_surplus_w":     round(s3_surplus_w),
        "heizstab_enabled": heater_module_enabled,
        "hs_auto_mode":     auto_mode,
        "hs_global_auto":   global_auto,
        "hs_manual_override": None,
        "hs_mode":          "manual" if not auto_mode else "pv_auto",
        "hs_reason":        "Auto-Modus deaktiviert",
        "predump_heater_active": predump_heater_budget_w > 0 and surplus_w > 0,
        "predump_heater_requested_w": round(predump_heater_budget_w),
        "market_heater_active": market_heater_budget_w > 0,
        "market_heater_requested_w": round(raw_market_heater_budget_w),
        "market_plan_action": market_heater_ctx.get("action"),
        "market_plan_reason": market_heater_ctx.get("reason"),
        "market_plan_negative_price": bool(market_heater_ctx.get("negative_price")),
        "surplus_w":        round(surplus_w),
        "grid_surplus_w":   round(netpoint_surplus_w),
        "pv_w":             round(pv_w),
        "soc":              soc,
        "live_source":      live.get("source", "none"),
        "process_success":  True,
        "success":          True,
        "hs_measurement_required": False,
        "hs_measurement_valid": False,
        "hs_measurement_ts": None,
        "hs_measurement_source": "",
        "hs_measurement_topology": "none",
        "hs_measurement_reason": "not_configured",
        "hs_provider_measurements": {},
        "source_fresh": False,
        "hs_source_fresh": False,
        "consumer_budget_valid": bool(consumer_budget_snapshot.get("valid") is True),
        "consumer_budget_reason": str(consumer_budget_snapshot.get("reason") or ""),
        "consumer_budget_revision": consumer_budget_snapshot.get("budget_revision"),
        "consumer_budget_allocations": dict(consumer_allocations),
    }
    s3_wallbox_transition = False
    s3_actuator_context_valid = False
    s3_actuator_context_reason = "heatpump_not_configured"

    def refresh_shelly_3em_provider(now_ts=None):
        """Aktualisiert den Providervertrag aus dem bereits gelesenen Zustand."""
        if not has_s3em:
            return 0.0
        current_ts = time.time() if now_ts is None else float(now_ts)
        last_off_ts = float(hs_state.get("s3em_last_off_ts", 0) or 0)
        restart_left_s = (
            max(0.0, s3_restart_block_s - (current_ts - last_off_ts))
            if last_off_ts > 0
            else 0.0
        )
        update_shelly_3em_provider_contract(
            status,
            hs_state,
            global_auto=global_auto,
            s3_enable=s3_enable,
            s3_relay=s3_relay,
            s3_min_w=s3_min_w,
            s3_max_w=s3_max_w,
            soc_ok=soc_ok,
            restart_left_s=restart_left_s,
            wallbox_transition=s3_wallbox_transition,
            actuator_context_valid=s3_actuator_context_valid,
            actuator_context_reason=s3_actuator_context_reason,
            authorized_budget_w=s3_surplus_w,
        )
        if restart_left_s > 0:
            status["wp_restart_block_remaining_s"] = round(restart_left_s)
        else:
            status.pop("wp_restart_block_remaining_s", None)
        return restart_left_s

    elwa_modbus_available = True
    if is_elwa:
        _backoff_until = float(hs_state.get("elwa_backoff_until", 0) or 0)
        if _backoff_until > time.time():
            elwa_modbus_available = False
            _retry_s = max(1, int(_backoff_until - time.time()))
            status["success"] = False
            status["modbus_offline"] = True
            status["elwa_status"] = "Offline / Modbus nicht erreichbar"
            status["hs_reason"] = f"ELWA Modbus nicht erreichbar - neuer Versuch in {_retry_s}s"

    modbus_power_w = None
    modbus_observed_ts = None
    modbus_source = "elwa_status_derived_power" if is_elwa else "modbus_register_1014"
    sh_state = None
    sh_observed_ts = None

    # --- Heizstab Status auslesen ---
    if has_hs and MODBUS_OK and elwa_modbus_available:
        if is_elwa:
            # myPV AC ELWA-E: Timeout einmalig setzen + Status-Block lesen
            _invoke_actuator(elwa_ensure_timeout, hs_ip, hs_port, safety_gate=safety_gate)
            elwa_data = elwa_read_status(hs_ip, hs_port)
            modbus_observed_ts = time.time()
            if elwa_data:
                hs_state["elwa_fail_count"] = 0
                hs_state["elwa_backoff_until"] = 0
                modbus_power_w              = elwa_data["actual_w"]
                status["hs_target_w"]       = elwa_data["setpoint_w"]
                status["elwa_setpoint_w"]   = elwa_data["setpoint_w"]
                status["elwa_water_temp_c"] = elwa_data["water_temp_c"]
                status["elwa_target_temp_c"]= elwa_data["target_temp_c"]
                status["elwa_status_code"]  = elwa_data["status_code"]
                status["elwa_status"]       = elwa_data["status_text"]
                elwa_status_code = elwa_data["status_code"]
                print(f"  [ELWA] Status={elwa_data['status_text']} "
                      f"Soll={elwa_data['setpoint_w']}W "
                      f"Ist={elwa_data['actual_w']}W "
                      f"Wasser={elwa_data['water_temp_c']}degC")
            else:
                _fail_count = int(hs_state.get("elwa_fail_count", 0) or 0) + 1
                _backoff_s = min(300, max(30, 30 * _fail_count))
                hs_state["elwa_fail_count"] = _fail_count
                hs_state["elwa_backoff_until"] = time.time() + _backoff_s
                hs_state["is_on"] = False
                hs_state["current_w"] = 0
                elwa_modbus_available = False
                status["success"] = False
                status["modbus_offline"] = True
                status["elwa_status"] = "Offline / Modbus nicht erreichbar"
                status["hs_reason"] = f"ELWA Modbus keine Antwort - Retry in {_backoff_s}s"
                print(f"  [ELWA] Keine Daten von {hs_ip}:{hs_port}")
        else:
            # Generic: Register 1014 = Istleistung
            ist = heizstab_read_power(hs_ip, hs_port)
            modbus_observed_ts = time.time()
            if ist is not None:
                modbus_power_w = ist
                status["hs_target_w"] = ist
                print(f"  Heizstab Istleistung: {ist}W")

    # --- Shelly Istleistung auslesen ---
    if has_sh:
        sh_state = shelly_get_state(sh_ip)
        sh_observed_ts = time.time()
        if sh_state:
            sh_power_text = (
                f"{sh_state['power_w']:.0f}W"
                if _strict_finite_number(sh_state.get("power_w")) is not None
                else "Istleistung unbekannt"
            )
            print(f"  Shelly: {'EIN' if sh_state['on'] else 'AUS'} / {sh_power_text}")

    update_heater_power_measurement(
        status,
        modbus_configured=has_hs,
        modbus_power_w=modbus_power_w,
        modbus_observed_ts=modbus_observed_ts,
        modbus_source=modbus_source,
        shelly_configured=has_sh,
        shelly_state=sh_state,
        shelly_observed_ts=sh_observed_ts,
        max_age_s=cfg_float(
            cfg,
            "consumer_acceptance_evidence_max_age_s",
            HEATER_READBACK_MAX_AGE_S,
            min_value=1,
        ),
    )

    if has_s3em:
        status["wp_nominal_w"] = round(s3_max_w)
        s3_observed_ts = time.time()
        update_shelly_3em_measurement(
            status,
            s3_ip,
            s3_min_w,
            now_ts=s3_observed_ts,
        )
        s3_relay_state = (
            shelly_3em_get_relay(s3_ip, s3_relay)
            if s3_relay >= 0
            else None
        )
        update_shelly_3em_relay_evidence(
            status,
            s3_relay_state,
            now_ts=s3_observed_ts,
        )
        reconcile_shelly_3em_relay_state(
            status,
            hs_state,
            s3_relay_state,
            now_ts=s3_observed_ts,
        )
        s3_wallbox_transition = wallbox_phase_transition_active(
            now_ts=s3_observed_ts,
            budget_snapshot=consumer_budget_snapshot,
        )
        (
            s3_actuator_context_valid,
            s3_actuator_context_reason,
        ) = heat_actuator_context_readiness(safety_gate)
        refresh_shelly_3em_provider(s3_observed_ts)

    if not heater_module_enabled:
        status["hs_mode"] = "disabled"
        status["hs_reason"] = "Heizstab/BWWP deaktiviert"
        release_ok = True
        release_needed = not bool(hs_state.get("heater_module_off_confirmed", False))
        if release_needed and hs_configured:
            if not MODBUS_OK or not elwa_modbus_available:
                release_ok = False
            elif is_elwa:
                release_ok = _invoke_actuator(
                    elwa_set_power,
                    hs_ip,
                    hs_port,
                    0,
                    safety_gate=safety_gate,
                ) and release_ok
            else:
                release_ok = _invoke_actuator(
                    heizstab_set_power,
                    hs_ip,
                    hs_port,
                    0,
                    safety_gate=safety_gate,
                ) and release_ok
        if release_needed and sh_configured:
            release_ok = _invoke_actuator(
                shelly_set_state,
                sh_ip,
                False,
                safety_gate=safety_gate,
            ) and release_ok
        if release_ok:
            status["Heizstab_Power"] = 0
            status["hs_actual_w"] = 0
            status["hs_target_w"] = 0
            status["hs_requested_w"] = 0
            status["hs_active"] = False
            status["shelly_heiz_on"] = False
            hs_state["is_on"] = False
            hs_state["current_w"] = 0
            hs_state["heater_module_off_confirmed"] = True
        else:
            status["success"] = False
            status["hs_reason"] = "Heizstab/BWWP AUS nicht sicher bestätigt"
            hs_state["heater_module_off_confirmed"] = False
        # Pro3EM-Messung und eine separat freigegebene WP-Relaissteuerung
        # bleiben unabhängig vom ausgeschalteten Zusatz-Heizstab erhalten.
        if not has_s3em:
            return _with_actuator_safety_status(status, safety_gate)
    else:
        hs_state["heater_module_off_confirmed"] = False

    if is_elwa and has_hs and not elwa_modbus_available and not has_sh:
        # ELWA ist das einzige Verbraucher-Modul und Modbus ist gerade offline.
        # Keine weiteren 0W-/Sollwert-Schreibversuche bis zum Backoff-Ende.
        return _with_actuator_safety_status(status, safety_gate)

    heater_price_block_limit_ct = cfg_float(cfg, "heat_price_block_limit_ct", 35.0)
    heater_empty_soc = cfg_float(cfg, "heat_price_block_empty_soc", 10.0)
    live_price_ct = live.get("price_ct") if isinstance(live, dict) else None
    expensive_price_window_active = bool(live_price_ct is not None and float(live_price_ct) >= heater_price_block_limit_ct)
    battery_empty_for_price_block = bool(soc <= heater_empty_soc)
    if expensive_price_window_active and battery_empty_for_price_block:
        if float(hs_state.get("heat_price_block_started_ts", 0) or 0) <= 0:
            hs_state["heat_price_block_started_ts"] = time.time()
    else:
        hs_state["heat_price_block_started_ts"] = 0.0

    heat_policy_decision, heat_forecast_result = build_heater_policy_decision(
        cfg,
        live,
        status,
        auto_mode=auto_mode,
        has_heat_device=bool(has_hs or has_sh),
        pv_budget_w=surplus_w,
        predump_budget_w=predump_heater_budget_w,
        raw_market_budget_w=raw_market_heater_budget_w,
        market_ctx=market_heater_ctx,
        hs_max_w=hs_max_w,
        is_currently_on=is_currently_on,
        min_surplus_w=min_surplus_w,
        threshold_off_w=threshold_off_w,
        expensive_price_window_active=expensive_price_window_active,
        battery_empty=battery_empty_for_price_block,
        price_block_started_ts=float(hs_state.get("heat_price_block_started_ts", 0) or 0),
        price_block_limit_ct=heater_price_block_limit_ct,
    )
    if (
        heat_policy_runtime_enabled
        and raw_market_heater_budget_w > 0
        and heat_policy_decision.target_state == heat_policy.TARGET_BOOST
        and heat_policy_decision.owner == "heater_grid_boost"
    ):
        market_heater_budget_w = min(
            surplus_w,
            raw_market_heater_budget_w,
            heat_policy_decision.available_budget_w,
        )
    elif heat_policy_runtime_enabled and raw_market_heater_budget_w > 0:
        market_heater_ctx = dict(market_heater_ctx)
        market_heater_ctx["allowed"] = False
        market_heater_ctx["reason"] = heat_policy_decision.block_reason
    elif raw_market_heater_budget_w > 0:
        market_heater_budget_w = min(surplus_w, raw_market_heater_budget_w)

    heat_policy_export = heat_policy_decision.as_dict()
    heat_policy_export.update({
        "ts": int(time.time()),
        "service": "heizstab_manager",
        "domain": "heater",
        "hal_output": True,
        "runtime_enabled": bool(heat_policy_runtime_enabled),
        "price_block_control_authorized": True,
        "forecast_input": heat_forecast_result.as_dict(),
        "raw_market_budget_w": int(raw_market_heater_budget_w),
    })
    status["heat_policy"] = heat_policy_export
    status["market_heater_active"] = market_heater_budget_w > 0
    status["market_plan_action"] = market_heater_ctx.get("action")
    status["market_plan_reason"] = market_heater_ctx.get("reason")
    status["market_plan_negative_price"] = bool(market_heater_ctx.get("negative_price"))
    status["surplus_w"] = round(surplus_w)

    manual_override = read_manual_override() if heater_module_enabled else None
    if manual_override:
        manual_mode = str(manual_override.get("mode", "")).strip().lower()
        status["hs_manual_override"] = manual_mode
        if manual_mode == "full":
            target_w = hs_max_w
            status["hs_mode"] = "manual_full"
            status["hs_reason"] = f"Manuell Vollgas: {target_w:.0f}W angefordert"
            if has_hs and MODBUS_OK and elwa_modbus_available:
                if is_elwa:
                    ok = _invoke_actuator(elwa_set_power, hs_ip, hs_port, target_w, safety_gate=safety_gate)
                    if ok:
                        status["hs_requested_w"] = target_w
                        status["hs_target_w"] = target_w
                        if elwa_can_accept_power(elwa_status_code):
                            status["Heizstab_Power"] = target_w
                            status["hs_actual_w"] = target_w
                            status["hs_active"] = target_w > 0
                            hs_state["is_on"] = True
                            hs_state["current_w"] = target_w
                        else:
                            status["Heizstab_Power"] = 0
                            status["hs_actual_w"] = 0
                            status["hs_active"] = False
                            hs_state["is_on"] = False
                            hs_state["current_w"] = 0
                    else:
                        status["success"] = False
                        status["hs_reason"] = "ELWA Sollwert-Write/Readback nicht bestätigt"
                else:
                    ok = _invoke_actuator(heizstab_set_power, hs_ip, hs_port, target_w, safety_gate=safety_gate)
                    if ok:
                        status["Heizstab_Power"] = target_w
                        status["hs_actual_w"] = target_w
                        status["hs_target_w"] = target_w
                        status["hs_requested_w"] = target_w
                        status["hs_active"] = target_w > 0
                        hs_state["is_on"] = True
                        hs_state["current_w"] = target_w
                    else:
                        status["success"] = False
                        status["hs_reason"] = "Heizstab Sollwert-Write/Readback nicht bestätigt"
            if has_sh:
                shelly_ok = _invoke_actuator(shelly_set_state, sh_ip, True, safety_gate=safety_gate)
                if shelly_ok:
                    status["shelly_heiz_on"] = True
                    status["hs_active"] = True
                    status["Heizstab_Power"] = max(float(status.get("Heizstab_Power", 0) or 0), sh_nominal_w)
                    status["hs_actual_w"] = max(float(status.get("hs_actual_w", 0) or 0), sh_nominal_w)
                    hs_state["is_on"] = True
                    hs_state["current_w"] = max(float(hs_state.get("current_w", 0) or 0), sh_nominal_w)
                else:
                    status["success"] = False
                    status["hs_reason"] = "Shelly EIN nicht bestätigt"
            return _with_actuator_safety_status(status, safety_gate)

        if manual_mode == "off":
            status["hs_mode"] = "manual_off"
            status["hs_reason"] = "Manuell AUS: Heizstab gestoppt"
            release_ok = True
            if has_hs and MODBUS_OK and elwa_modbus_available:
                if is_elwa:
                    release_ok = _invoke_actuator(
                        elwa_set_power,
                        hs_ip,
                        hs_port,
                        0,
                        safety_gate=safety_gate,
                    ) and release_ok
                else:
                    release_ok = _invoke_actuator(
                        heizstab_set_power,
                        hs_ip,
                        hs_port,
                        0,
                        safety_gate=safety_gate,
                    ) and release_ok
            if has_sh and status.get("shelly_heiz_on"):
                shelly_off_ok = _invoke_actuator(shelly_set_state, sh_ip, False, safety_gate=safety_gate)
                release_ok = shelly_off_ok and release_ok
                if shelly_off_ok:
                    status["shelly_heiz_on"] = False
            if release_ok:
                status["Heizstab_Power"] = 0
                status["hs_actual_w"] = 0
                status["hs_target_w"] = 0
                status["hs_requested_w"] = 0
                status["hs_active"] = False
                hs_state["is_on"] = False
                hs_state["current_w"] = 0
            else:
                status["success"] = False
                status["hs_reason"] = "Manuell AUS fehlgeschlagen: Safe-Readback nicht bestätigt"
            return _with_actuator_safety_status(status, safety_gate)

    if auto_mode:
        hs_state["heater_auto_off_confirmed"] = False
    else:
        # Der lokale Heizstab-Schalter stoppt ausschließlich Heizstab/BWWP.
        # Eine separat freigegebene Pro3EM-WP bleibt davon unabhängig.
        status["hs_reason"] = (
            "Globale Automatik deaktiviert - Geräte auf Idle"
            if not global_auto
            else "Auto-Modus deaktiviert - Heizstab gestoppt"
        )
        heater_drifted_on = bool(
            float(status.get("hs_actual_w", 0) or 0) > 0
            or float(status.get("hs_target_w", 0) or 0) > 0
            or status.get("shelly_heiz_on")
            or hs_state.get("is_on")
            or float(hs_state.get("current_w", 0) or 0) > 0
        )
        if heater_drifted_on:
            hs_state["heater_auto_off_confirmed"] = False
        release_needed = not bool(hs_state.get("heater_auto_off_confirmed", False))
        release_ok = True
        if release_needed and has_hs:
            if not MODBUS_OK or not elwa_modbus_available:
                release_ok = False
            elif is_elwa:
                release_ok = _invoke_actuator(
                    elwa_set_power,
                    hs_ip,
                    hs_port,
                    0,
                    safety_gate=safety_gate,
                ) and release_ok
            else:
                release_ok = _invoke_actuator(
                    heizstab_set_power,
                    hs_ip,
                    hs_port,
                    0,
                    safety_gate=safety_gate,
                ) and release_ok
        if release_needed and has_sh:
            release_ok = _invoke_actuator(
                shelly_set_state,
                sh_ip,
                False,
                safety_gate=safety_gate,
            ) and release_ok
        if release_ok:
            status["Heizstab_Power"] = 0
            status["hs_actual_w"] = 0
            status["hs_target_w"] = 0
            status["hs_requested_w"] = 0
            status["hs_active"] = False
            status["shelly_heiz_on"] = False
            hs_state["is_on"] = False
            hs_state["current_w"] = 0
            hs_state["heater_auto_off_confirmed"] = True
            if release_needed and (has_hs or has_sh):
                print("  -> Heizstab/BWWP AUS gesetzt und bestätigt")
        else:
            status["success"] = False
            status["hs_reason"] = "Auto AUS fehlgeschlagen: Safe-Readback nicht bestätigt"
            hs_state["heater_auto_off_confirmed"] = False

        if not global_auto and has_s3em and s3_enable and s3_relay >= 0:
            s3_confirmed_off = bool(
                status.get("wp_relay_readback_valid") is True
                and status.get("wp_relay_readback_on") is False
            )
            s3_release_needed = bool(
                not s3_confirmed_off
                and (
                    hs_state.get("s3em_on")
                    or not hs_state.get("s3em_auto_off_confirmed", False)
                )
            )
            s3_release_ok = True
            if s3_release_needed:
                s3_release_ok = _invoke_actuator(
                    shelly_3em_set_relay,
                    s3_ip,
                    s3_relay,
                    False,
                    safety_gate=safety_gate,
                )
            if s3_release_ok:
                hs_state["s3em_on"] = False
                hs_state["s3em_auto_off_confirmed"] = True
                status["wp_relay_on"] = False
                if s3_release_needed:
                    s3_release_ts = time.time()
                    hs_state["s3em_last_off_ts"] = s3_release_ts
                    update_shelly_3em_relay_evidence(
                        status,
                        False,
                        now_ts=s3_release_ts,
                        source="shelly_3em_switch_set_confirmed_readback",
                    )
                    status["wp_relay_runtime_evidence"] = (
                        "commanded_off_confirmed_readback"
                    )
            else:
                status["success"] = False
                status["hs_reason"] = "Globale Automatik AUS: WP-Relais nicht sicher bestätigt"
                hs_state["s3em_auto_off_confirmed"] = False
            refresh_shelly_3em_provider()
        elif s3_relay_auto_mode:
            hs_state["s3em_auto_off_confirmed"] = False

        # Nutzer-Aus bleibt für den Heizstab ein hartes Veto. Nur die explizit
        # freigegebene Pro3EM-WP darf bei lokalem hs_auto_mode=0 weiterlaufen.
        if not (has_s3em and s3_relay_auto_mode):
            if has_s3em and not s3_enable:
                status["hs_reason"] = "WP nur Messung - keine Relaissteuerung"
            return _with_actuator_safety_status(status, safety_gate)

    # --- PV-Ueberschuss-Regelung mit Hysterese ---
    # Einschalten: Ueberschuss muss >= threshold_on_w sein.
    # Ausschalten: bei SOC-Freigabe unter/bei threshold_off_w, unter SOC-Schutz wieder erst ab threshold_on_w.
    if is_currently_on:
        should_run = (soc_ok and surplus_w > threshold_off_w) or ((not soc_ok) and surplus_w >= threshold_on_w)
    else:
        should_run = surplus_w >= threshold_on_w
    if not auto_mode:
        should_run = False
    if heat_policy_runtime_enabled and heat_policy_decision.target_state == heat_policy.TARGET_BLOCKED:
        should_run = False
    measurement_blocked = bool(
        status.get("hs_measurement_required") is True
        and (
            status.get("hs_measurement_valid") is not True
            or status.get("source_fresh") is not True
        )
    )
    if measurement_blocked:
        should_run = False

    hyst_info = f"(Hyst: Ein>={threshold_on_w:.0f}W, Aus<{threshold_off_w:.0f}W, akt={'EIN' if is_currently_on else 'AUS'})"
    print(f"  PV-Ueberschuss: {surplus_w:.0f}W | SOC: {soc:.0f}% (Min: {min_soc}%) | "
          f"Regelung: {'AKTIV' if should_run else 'STOP'} {hyst_info}")

    if should_run:
        target_w = min(surplus_w, hs_max_w)
        try:
            min_change_w = max(0, float(cfg.get("hs_min_change_w", 100) or 0))
        except Exception:
            min_change_w = 100
        current_w = float(hs_state.get('current_w', 0) or 0)
        if is_currently_on and min_change_w > 0 and abs(target_w - current_w) < min_change_w:
            target_w = current_w
        if market_heater_budget_w > 0:
            if bool(market_heater_ctx.get("negative_price")):
                status["hs_reason"] = (
                    f"Negativpreis-Boost: Heizstab/Shelly mit {surplus_w/1000:.1f}kW freigegeben"
                )
                status["hs_mode"] = "negative_price_boost"
            else:
                status["hs_reason"] = (
                    f"Marktfenster: Heizstab/Shelly mit {surplus_w/1000:.1f}kW freigegeben "
                    f"({market_heater_ctx.get('reason') or 'market_plan'})"
                )
                status["hs_mode"] = "market_plan"
        elif predump_heater_budget_w > 0:
            status["hs_reason"] = f"Pre-Dump-Freigabe {surplus_w/1000:.1f}kW (SOC {soc:.0f}%)"
            status["hs_mode"] = "pre_dump"
        elif not soc_ok:
            status["hs_reason"] = f"Zentralbudget {surplus_w/1000:.1f}kW trotz SOC-Schutz (SOC {soc:.0f}%)"
            status["hs_mode"] = "grid_follow"
        else:
            status["hs_reason"] = f"Zentralbudget {surplus_w/1000:.1f}kW (SOC {soc:.0f}%)"

        if has_hs and MODBUS_OK and elwa_modbus_available:
            if is_elwa:
                ok = _invoke_actuator(elwa_set_power, hs_ip, hs_port, target_w, safety_gate=safety_gate)
                if ok:
                    status["hs_requested_w"]   = target_w
                    status["hs_target_w"]     = target_w
                    # Der ELWA-Status wurde vor dem Schreiben gelesen. Direkt
                    # nach Standby darf die Anforderung trotzdem als laufende
                    # Last fuer die naechste Netzpunktrechnung gelten, sonst
                    # rechnet sich der Regler seine eigene Last weg und taktet.
                    # Nur "Fertig" und echte Fehler zaehlen nicht als Last.
                    if elwa_can_accept_power(elwa_status_code):
                        status["Heizstab_Power"] = target_w
                        status["hs_actual_w"] = target_w
                        status["hs_active"] = target_w > 0
                        hs_state['is_on'] = True
                        hs_state['current_w'] = target_w
                    else:
                        status["Heizstab_Power"] = 0
                        status["hs_actual_w"] = 0
                        status["hs_active"] = False
                        hs_state['is_on'] = False
                        hs_state['current_w'] = 0
                    print(f"  [ELWA] -> Sollleistung {target_w:.0f}W gesetzt")
                else:
                    status["success"] = False
                    status["hs_reason"] = "ELWA Sollwert-Write/Readback nicht bestätigt"
            else:
                ok = _invoke_actuator(heizstab_set_power, hs_ip, hs_port, target_w, safety_gate=safety_gate)
                if ok:
                    status["hs_requested_w"] = target_w
                    status["Heizstab_Power"] = target_w
                    status["hs_actual_w"] = target_w
                    status["hs_target_w"] = target_w
                    status["hs_active"] = target_w > 0
                    hs_state['is_on']    = True
                    hs_state['current_w'] = target_w
                    print(f"  -> Heizstab auf {target_w:.0f}W gesetzt")
                else:
                    status["success"] = False
                    status["hs_reason"] = "Heizstab Sollwert-Write/Readback nicht bestätigt"

        if has_sh and surplus_w >= sh_nominal_w:
            shelly_on_ok = _invoke_actuator(shelly_set_state, sh_ip, True, safety_gate=safety_gate)
            if shelly_on_ok:
                status["shelly_heiz_on"] = True
                status["Heizstab_Power"] = max(float(status.get("Heizstab_Power", 0) or 0), sh_nominal_w)
                status["hs_actual_w"] = max(float(status.get("hs_actual_w", 0) or 0), sh_nominal_w)
                status["hs_target_w"] = max(float(status.get("hs_target_w", 0) or 0), sh_nominal_w)
                status["hs_active"] = True
                hs_state['is_on'] = True
                hs_state['current_w'] = max(target_w, sh_nominal_w)
                print(f"  -> Shelly EINgeschaltet")
            else:
                status["success"] = False
                status["hs_reason"] = "Shelly EIN nicht bestätigt"

    else:
        if not auto_mode:
            pass
        elif measurement_blocked:
            status["hs_reason"] = (
                "Heizstab-Istwert unbekannt/ungültig; Start bleibt fail-closed"
            )
            status["hs_mode"] = "measurement_blocked"
        elif heat_policy_runtime_enabled and heat_policy_decision.target_state == heat_policy.TARGET_BLOCKED:
            status["hs_reason"] = heat_policy_decision.block_reason
            status["hs_mode"] = "blocked"
        elif heat_policy_runtime_enabled and raw_market_heater_budget_w > 0 and market_heater_budget_w <= 0:
            status["hs_reason"] = f"Marktfenster blockiert: {heat_policy_decision.block_reason}"
            status["hs_mode"] = "market_blocked"
        elif not soc_ok:
            status["hs_reason"] = f"SOC zu niedrig ({soc:.0f}% < {min_soc}%)"
        else:
            status["hs_reason"] = f"PV-Ueberschuss zu gering ({surplus_w:.0f}W < {threshold_off_w:.0f}W Ausschalt-Schwelle)"

        aux_release_ok = True
        if auto_mode and has_hs and MODBUS_OK and elwa_modbus_available:
            heater_off_ok = True
            if is_elwa:
                if is_currently_on or float(hs_state.get('current_w', 0) or 0) > 0:
                    heater_off_ok = _invoke_actuator(elwa_set_power, hs_ip, hs_port, 0, safety_gate=safety_gate)
                    if heater_off_ok:
                        print(f"  [ELWA] -> Sollleistung 0W (deaktiviert)")
                if heater_off_ok:
                    status["elwa_setpoint_w"] = 0
                    hs_state['is_on']    = False
                    hs_state['current_w'] = 0
            else:
                heater_off_ok = _invoke_actuator(heizstab_set_power, hs_ip, hs_port, 0, safety_gate=safety_gate)
                if heater_off_ok:
                    hs_state['is_on']    = False
                    hs_state['current_w'] = 0
            if heater_off_ok:
                status["Heizstab_Power"] = 0
                status["hs_actual_w"] = 0
                status["hs_target_w"] = 0
                status["hs_active"] = False
            else:
                status["success"] = False
                status["hs_reason"] = "Heizstab AUS fehlgeschlagen: Register-Readback nicht bestätigt"
                aux_release_ok = False

        if auto_mode and has_sh and status.get("shelly_heiz_on"):
            shelly_off_ok = _invoke_actuator(shelly_set_state, sh_ip, False, safety_gate=safety_gate)
            if shelly_off_ok:
                status["shelly_heiz_on"] = False
                print(f"  -> Shelly AUSgeschaltet")
            else:
                status["success"] = False
                status["hs_reason"] = "Shelly AUS fehlgeschlagen: Relais-Readback nicht bestätigt"
                aux_release_ok = False
        if aux_release_ok and not status.get("hs_active") and not status.get("shelly_heiz_on"):
            hs_state['is_on'] = False
            hs_state['current_w'] = 0

    # ══ SHELLY PRO3EM WÄRMEPUMPE (wp_type=3) ══
    if has_s3em:
        # Relais-Steuerung (nur wenn s3_enable=1 und relay_id >= 0)
        if s3_relay_auto_mode:
            relay_readback_valid = status.get("wp_relay_readback_valid") is True
            relay_readback_on = status.get("wp_relay_readback_on") is True
            managed_relay_on = bool(
                relay_readback_on or hs_state.get("s3em_on", False)
            )
            wp_is_on = bool(
                managed_relay_on or status.get("wp_accepting_power") is True
            )
            start_floor_w = max(
                int(CONSUMER_MIN_W.get("heatpump", 1500)),
                int(math.ceil(s3_min_w)),
            )
            # Ein Wallbox-Phasen-/Stromübergang darf das gemeinsame Budget reservieren,
            # aber einen bereits laufenden Verdichter nie stoppen. Der unabhängige
            # SoC-Schutz bleibt unverändert.
            should_wp = shelly_wp_relay_should_run(
                wp_is_on=wp_is_on,
                soc_ok=soc_ok,
                surplus_w=s3_surplus_w,
                threshold_on_w=threshold_on_w,
                threshold_off_w=threshold_off_w,
                wp_min_w=start_floor_w,
                wallbox_transition=s3_wallbox_transition,
            )

            now_ts = time.time()
            last_on_ts = float(hs_state.get("s3em_last_on_ts", 0) or 0)
            last_off_ts = float(hs_state.get("s3em_last_off_ts", 0) or 0)
            runtime_left_s = max(0.0, s3_min_runtime_s - (now_ts - last_on_ts)) if last_on_ts > 0 else 0.0
            restart_left_s = max(0.0, s3_restart_block_s - (now_ts - last_off_ts)) if last_off_ts > 0 else 0.0
            # SoC-Schutz und manuelles/Auto-Aus bleiben harte Stops. Reine Wolkenkanten
            # halten wir bis zur Mindestlaufzeit, damit das WP-Relais nicht taktet.
            relay_emergency_stop = not soc_ok

            if s3_wallbox_transition and wp_is_on and not relay_emergency_stop:
                status["wp_relay_on"] = bool(managed_relay_on)
                status["wp_takt_protect_active"] = True
                status["hs_reason"] = "WP läuft weiter: Wallbox-Übergang darf sie nicht stoppen"
                refresh_shelly_3em_provider(now_ts)
                return _with_actuator_safety_status(status, safety_gate)

            if should_wp and not wp_is_on:
                if not relay_readback_valid:
                    status["success"] = False
                    status["wp_relay_on"] = bool(hs_state.get("s3em_on", False))
                    status["hs_reason"] = (
                        "WP EIN gesperrt: frischer Relais-Readback fehlt"
                    )
                    refresh_shelly_3em_provider(now_ts)
                    return _with_actuator_safety_status(status, safety_gate)
                if restart_left_s > 0:
                    status["wp_relay_on"] = False
                    status["wp_takt_protect_active"] = True
                    status["wp_restart_block_remaining_s"] = round(restart_left_s)
                    status["hs_reason"] = (
                        f"WP Wiedereinschaltsperre aktiv: noch {restart_left_s/60:.1f} Min "
                        f"(Überschuss {s3_surplus_w:.0f}W)"
                    )
                    print(f"  [3EM] WP Relais bleibt AUS (Wiedereinschaltsperre {restart_left_s/60:.1f} Min)")
                    refresh_shelly_3em_provider(now_ts)
                    return _with_actuator_safety_status(status, safety_gate)
                if status.get("wp_budget_start_ready") is not True:
                    status["success"] = False
                    status["wp_relay_on"] = False
                    status["hs_reason"] = (
                        "WP EIN gesperrt: Provider-Readiness nicht vollständig"
                    )
                    refresh_shelly_3em_provider(now_ts)
                    return _with_actuator_safety_status(status, safety_gate)
                ok = _invoke_actuator(
                    shelly_3em_set_relay,
                    s3_ip,
                    s3_relay,
                    True,
                    safety_gate=safety_gate,
                )
                if ok:
                    hs_state["s3em_on"] = True
                    hs_state["s3em_last_on_ts"] = now_ts
                    hs_state["s3em_auto_off_confirmed"] = False
                    update_shelly_3em_relay_evidence(
                        status,
                        True,
                        now_ts=now_ts,
                        source="shelly_3em_switch_set_confirmed_readback",
                    )
                    status["wp_relay_runtime_evidence"] = (
                        "commanded_on_confirmed_readback"
                    )
                    status["wp_relay_on"] = True
                    status["wp_takt_protect_active"] = False
                    status["wp_min_runtime_remaining_s"] = round(s3_min_runtime_s)
                    status["hs_reason"] = (
                        f"WP EIN: Budget {s3_surplus_w:.0f}W >= "
                        f"{start_floor_w:.0f}W Startminimum (SOC {soc:.0f}%)"
                    )
                    print(f"  [3EM] -> WP Relais {s3_relay} EINgeschaltet")
                else:
                    status["success"] = False
                    status["wp_relay_on"] = bool(managed_relay_on)
                    status["hs_reason"] = "WP EIN blockiert oder Readback nicht bestätigt"
            elif not should_wp and managed_relay_on:
                if runtime_left_s > 0 and not relay_emergency_stop:
                    hs_state["s3em_on"] = True
                    status["wp_relay_on"] = True
                    status["wp_takt_protect_active"] = True
                    status["wp_min_runtime_remaining_s"] = round(runtime_left_s)
                    status["hs_reason"] = (
                        f"WP Mindestlaufzeit aktiv: noch {runtime_left_s/60:.1f} Min "
                        f"(zugewiesenes Budget {s3_surplus_w:.0f}W)"
                    )
                    print(f"  [3EM] WP Relais bleibt EIN (Mindestlaufzeit {runtime_left_s/60:.1f} Min)")
                    refresh_shelly_3em_provider(now_ts)
                    return _with_actuator_safety_status(status, safety_gate)
                ok = _invoke_actuator(
                    shelly_3em_set_relay,
                    s3_ip,
                    s3_relay,
                    False,
                    safety_gate=safety_gate,
                )
                if ok:
                    hs_state["s3em_on"] = False
                    hs_state["s3em_last_off_ts"] = now_ts
                    update_shelly_3em_relay_evidence(
                        status,
                        False,
                        now_ts=now_ts,
                        source="shelly_3em_switch_set_confirmed_readback",
                    )
                    status["wp_relay_runtime_evidence"] = (
                        "commanded_off_confirmed_readback"
                    )
                    status["wp_relay_on"] = False
                    status["wp_takt_protect_active"] = False
                    status["wp_restart_block_remaining_s"] = round(s3_restart_block_s)
                    reason_off = (f"SOC zu niedrig ({soc:.0f}% < {min_soc}%)" if not soc_ok
                                  else f"kein zugewiesenes WP-Budget ({s3_surplus_w:.0f}W)")
                    status["hs_reason"] = f"WP AUS: {reason_off}"
                    print(f"  [3EM] -> WP Relais {s3_relay} AUSgeschaltet ({reason_off})")
                else:
                    status["success"] = False
                    status["wp_relay_on"] = True
                    status["hs_reason"] = (
                        "WP AUS blockiert oder Readback nicht bestätigt; "
                        "laufender Zustand wird nicht verändert"
                    )
            else:
                status["wp_relay_on"] = bool(managed_relay_on)
                status["wp_takt_protect_active"] = False
                if status["wp_relay_on"] and s3_wallbox_transition:
                    status["hs_reason"] = "WP läuft weiter: Wallbox-Übergang darf sie nicht stoppen"
                if status["wp_relay_on"] and runtime_left_s > 0:
                    status["wp_min_runtime_remaining_s"] = round(runtime_left_s)
                elif (not status["wp_relay_on"]) and restart_left_s > 0:
                    status["wp_restart_block_remaining_s"] = round(restart_left_s)
            refresh_shelly_3em_provider(now_ts)
        else:
            status["wp_relay_on"] = bool(hs_state.get("s3em_on", False))
            status["wp_takt_protect_active"] = False
            if (not s3_enable or s3_relay < 0) and not (has_hs or has_sh):
                status["hs_reason"] = "WP nur Messung - keine Relaissteuerung"
            refresh_shelly_3em_provider()

    heater_acceptance_ready = bool(
        auto_mode
        and heater_module_enabled
        and (has_hs or has_sh)
        and status.get("success") is True
        and status.get("hs_measurement_valid") is True
        and status.get("source_fresh") is True
        and not bool(status.get("actor_writes_blocked"))
        and (not is_elwa or elwa_can_accept_power(elwa_status_code))
    )
    status["budget_start_ready"] = heater_acceptance_ready
    status["budget_start_request_w"] = (
        int(max(hs_max_w if has_hs else 0, sh_nominal_w if has_sh else 0))
        if heater_acceptance_ready
        else 0
    )
    return _with_actuator_safety_status(status, safety_gate)


# ---------------------------------------------------------------------------
# Ramdisk schreiben (atomar, wie idm_live.py)
# ---------------------------------------------------------------------------

def save_to_ramdisk(data):
    """Schreibt heizstab_data.json atomar via os.replace."""
    data["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = RAMDISK_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.chmod(tmp, 0o664)
        try:
            import grp
            gid = grp.getgrnam("www-data").gr_gid
            os.chown(tmp, -1, gid)
        except Exception:
            pass
        os.replace(tmp, RAMDISK_FILE)
    except Exception as e:
        print(f"[!] Ramdisk-Fehler: {e}")


# ---------------------------------------------------------------------------
# Hauptschleife
# ---------------------------------------------------------------------------

def main():
    print(f"[{time.strftime('%H:%M:%S')}] heizstab_manager.py gestartet")

    cfg = load_config()

    wp_type = str(cfg.get("wp_type", "0")).strip()
    hs_ip = cfg.get("heizstab_ip", "0.0.0.0").strip()
    sh_ip = cfg.get("shelly_heiz_ip", "0.0.0.0").strip()
    s3_ip = cfg.get("shelly_3em_ip", "0.0.0.0").strip()
    has_aux_heater = hs_ip not in ("", "0.0.0.0") or sh_ip not in ("", "0.0.0.0")
    has_s3em_wp = wp_type == "3" and s3_ip not in ("", "0.0.0.0")
    legacy_wp_type_2 = wp_type == "2" and has_aux_heater

    if not (has_aux_heater or has_s3em_wp or legacy_wp_type_2):
        print("[!] Kein Heizstab/Shelly/BWWP-Zusatzverbraucher konfiguriert.")
        print("    wp_type bleibt Luxtronik/IDM. Fuer Heizstab/BWWP heizstab=1 und IP setzen.")
        save_to_ramdisk({
            "success": False,
            "process_success": False,
            "error": "Kein Heizstab/Shelly konfiguriert",
            "Heizstab_Power": None,
            "hs_actual_w": None,
            "hs_measurement_valid": False,
            "source_fresh": False,
        })
        return
    if wp_type == "3" and s3_ip in ("", "0.0.0.0") and not has_aux_heater:
        print("[!] wp_type=3 aber shelly_3em_ip nicht konfiguriert.")
        save_to_ramdisk({
            "success": False,
            "process_success": False,
            "error": "wp_type=3: shelly_3em_ip fehlt",
            "Heizstab_Power": None,
            "hs_actual_w": None,
            "hs_measurement_valid": False,
            "source_fresh": False,
        })
        return

    # Derselbe Endpunkt darf nicht gleichzeitig Luxtronik und Heizstab sein.
    luxtronik_ip = str(cfg.get("luxtronik_ip", "")).strip()
    if hs_ip not in ("", "0.0.0.0") and luxtronik_ip and hs_ip == luxtronik_ip:
        print("[!] WARNUNG: heizstab_ip entspricht dem konfigurierten Luxtronik-Endpunkt!")
        print("    Heizstab Modbus wird auf dieser IP NICHT funktionieren.")
        print("    Setze heizstab_ip = 0.0.0.0 wenn kein Modbus-Heizstab vorhanden ist.")

    print(f"  WP-Typ: {wp_type} | Heizstab/BWWP-Zusatz: {'an' if has_aux_heater else 'aus'}")
    print(f"  Heizstab Modbus: {hs_ip}:{cfg.get('heizstab_port','502')} | "
          f"Shelly: {sh_ip} | "
          f"Auto-Modus: {'an' if cfg.get('hs_auto_mode','1')=='1' else 'aus'}")

    # SIGTERM-Handler: Heizstab und Shelly beim Dienst-Stop auf 0/AUS setzen.
    # WICHTIG fuer Generic-Heizstab (Register 1000): hat keinen Modbus-Timeout!
    # Der myPV ELWA-E schaltet nach 60s (Register 1004) selbst ab, aber Generic nicht.
    import signal as _signal
    _hs_stop = False
    def _handle_stop(sig, frame):
        nonlocal _hs_stop
        _hs_stop = True
        print(f"[{time.strftime('%H:%M:%S')}] SIGTERM: Heizstab und Shelly werden abgeschaltet...")
        release_ok = True
        _hs = cfg.get("heizstab_ip", "0.0.0.0").strip()
        _sh = cfg.get("shelly_heiz_ip", "0.0.0.0").strip()
        _ht = str(cfg.get("heizstab_type", "generic")).strip().lower()
        if _hs not in ("", "0.0.0.0") and MODBUS_OK:
            try:
                if _ht == "mypv_elwa":
                    heater_off_ok = elwa_set_power(_hs, cfg.get("heizstab_port", "502"), 0)
                else:
                    heater_off_ok = heizstab_set_power(_hs, cfg.get("heizstab_port", "502"), 0)
                release_ok = heater_off_ok and release_ok
                if heater_off_ok:
                    print(f"  Heizstab {_hs} -> 0W gesetzt und bestätigt")
                else:
                    print(f"  [!] Heizstab {_hs}: 0W-Readback nicht bestätigt")
            except Exception as e:
                release_ok = False
                print(f"  [!] Heizstab Stop-Fehler: {e}")
        if _sh not in ("", "0.0.0.0"):
            try:
                shelly_off_ok = shelly_set_state(_sh, False)
                release_ok = shelly_off_ok and release_ok
                if shelly_off_ok:
                    print(f"  Shelly {_sh} -> AUS gesetzt und bestätigt")
                else:
                    print(f"  [!] Shelly {_sh}: AUS-Readback nicht bestätigt")
            except Exception as e:
                release_ok = False
                print(f"  [!] Shelly Stop-Fehler: {e}")
        save_to_ramdisk({
            "success": False,
            "process_success": False,
            "error": "Dienst beendet" if release_ok else "Dienst beendet; Safe-Release unvollständig",
            "Heizstab_Power": None,
            "hs_actual_w": None,
            "hs_measurement_valid": False,
            "source_fresh": False,
            "safe_release_confirmed": bool(release_ok),
        })
    _signal.signal(_signal.SIGTERM, _handle_stop)
    _signal.signal(_signal.SIGINT,  _handle_stop)

    # Startup-Delay: ELWA-Modbus braucht Zeit zur Erholung nach Neustart
    print(f"  Warte {STARTUP_DELAY}s (ELWA Modbus Erholung nach Neustart)...")
    for _ in range(STARTUP_DELAY):
        if _hs_stop:
            return
        time.sleep(1)

    # Persistenter Zustand fuer Hysterese (lebt nur im Arbeitsspeicher, kein JSON)
    hs_state = {'is_on': False, 'current_w': 0}
    conn_err_count = 0  # Zaehlt aufeinanderfolgende Connection-Fehler

    loop = 0
    while not _hs_stop:
        loop += 1
        try:
            # Config jede 6. Runde neu laden (ca. jede 90s bei POLL_INTERVAL=15)
            if loop % 6 == 1:
                cfg = load_config()

            live = read_live()
            print(f"\n[{time.strftime('%H:%M:%S')}] PV={live['pv_w']:.0f}W "
                  f"Grid={live['grid_w']:+.0f}W SOC={live['soc']:.0f}% "
                  f"[{live['source']}]")

            status = control_cycle(cfg, live, hs_state)
            save_to_ramdisk(status)
            conn_err_count = 0  # Reset bei erfolgreichem Zyklus

        except ConnectionRefusedError:
            conn_err_count += 1
            wait = min(CONN_ERR_BACKOFF * conn_err_count, 300)
            print(f"[!] Connection refused (#{conn_err_count}) - warte {wait}s")
            save_to_ramdisk({
                "success": False,
                "process_success": False,
                "error": f"Connection refused (#{conn_err_count})",
                "Heizstab_Power": None,
                "hs_actual_w": None,
                "hs_measurement_valid": False,
                "source_fresh": False,
            })
            hs_state['is_on'] = False
            hs_state['current_w'] = 0
            for _ in range(wait):
                if _hs_stop:
                    return
                time.sleep(1)
            continue

        except Exception as e:
            print(f"[!] Fehler im Loop: {e}")
            save_to_ramdisk({
                "success": False,
                "process_success": False,
                "error": str(e),
                "Heizstab_Power": None,
                "hs_actual_w": None,
                "hs_measurement_valid": False,
                "source_fresh": False,
            })

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
