"""
E3DC-Control Wallbox Manager - Wallbox Treiber.
Enthaelt alle Treiber-Klassen:
  - WallboxDriver   (abstrakte Basisklasse)
  - GoECharger      (go-eCharger V2/V3 API)
  - DummyCharger    (Simulator für Tests)
  - OpenWBCharger   (openWB 2.x HTTP SimpleAPI)
  - E3DCCharger     (E3DC native Wallbox über RSCP)
  - create_charger() Factory-Funktion
"""
import os
import json
import time
import logging
import re
import base64
import math
import hashlib

import requests as _requests

from .config import logger, RAMDISK_DIR
from . import command_gate
from .soc_tracker import vehicle_soc_source_trusted

# paho-mqtt ergänzt bei openWB den strikt lesenden Fahrzeug-SoC-Pfad.
# Leistung, Steckzustand und Steuerung bleiben standardmäßig beim HTTP-Pfad.
_MQTT_LAZY = object()
mqtt = _MQTT_LAZY


def _get_mqtt_module():
    """Lade paho-mqtt erst bei Bedarf.

    Auf manchen Test-/Desktop-Umgebungen kann bereits der optionale Import
    haengen. Der HTTP-Pfad der openWB darf davon nie blockiert werden.
    """
    global mqtt
    if mqtt is _MQTT_LAZY:
        if str(os.environ.get("E3DC_DISABLE_PAHO_MQTT", "")).strip().lower() in ("1", "true", "yes", "on"):
            mqtt = None
            return None
        try:
            import importlib
            mqtt = importlib.import_module("paho.mqtt.client")
        except Exception as e:
            mqtt = None
            logger.debug(f"paho-mqtt nicht verfuegbar: {e}")
    return mqtt


OPENWB_AUTONOMOUS_CHARGEMODES = {
    "pv_charging",
    "instant_charging",
    "stop",
    "scheduled_charging",
    "eco_charging",
}

OPENWB_PRIMARY_DIRECT_LIMIT_NOTICE = (
    "Primary-Direktpfad: Stromvorgaben laufen über openWB-Sofortladen "
    "(chargecurrent); SoC-/Energiemengenlimits aus openWB bleiben wirksam."
)

E3DC_TRANSPORT = "e3dc_rscp_via_home_power_station"
E3DC_BACKEND_WBCHAR6 = "wbchar6_compat"
E3DC_BACKEND_STATUS_ONLY = "status_only"
E3DC_EASY_CONNECT_START_ATTEMPT_LIMIT = 3
E3DC_EASY_CONNECT_START_RETRY_MIN_S = 60.0
E3DC_DEVICE_FAMILIES = {
    "efy",
    "easy_connect",
    "multi_connect",
    "multi_connect_ii",
    "unknown",
}


def _config_bool(config, *keys, default=False):
    cfg = config or {}
    for key in keys:
        if key not in cfg or cfg.get(key) in (None, ""):
            continue
        value = cfg.get(key)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(default)


def _normalize_e3dc_device_family(value, default="unknown"):
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "easy": "easy_connect",
        "easyconnect": "easy_connect",
        "multi": "multi_connect",
        "multiconnect": "multi_connect",
        "multi_connect_i": "multi_connect",
        "multi_connect_2": "multi_connect_ii",
        "multiconnectii": "multi_connect_ii",
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in E3DC_DEVICE_FAMILIES else str(default or "unknown")


def _device_family_from_name(value):
    name = str(value or "").strip().lower()
    if not name:
        return "unknown"
    if "efy" in name:
        return "efy"
    if "easy" in name:
        return "easy_connect"
    if "multi connect ii" in name or "multi-connect ii" in name:
        return "multi_connect_ii"
    if "multi" in name and "connect" in name:
        return "multi_connect"
    return "unknown"


def _current_step_from_config(config, wb_id=1, default=1.0):
    """Return the driver-owned current granularity in ampere.

    The central manager may transport float setpoints, but the driver decides
    which step is safe for the concrete API. Unknown/empty/"auto" stays on the
    conservative default.
    """
    cfg = config or {}
    keys = (
        f"wb{wb_id}_current_step_amp",
        "wb_current_step_amp",
        "openwb_current_step_amp",
        "wallbox_current_step_amp",
    )
    raw = None
    for key in keys:
        value = cfg.get(key)
        if value not in (None, ""):
            raw = value
            break
    if raw is None:
        return float(default)
    text = str(raw).strip().lower().replace(",", ".")
    if text in ("auto", "default"):
        return float(default)
    try:
        step = float(text)
    except (TypeError, ValueError):
        return float(default)
    if step <= 0.11:
        return 0.1
    if step <= 0.51:
        return 0.5
    return 1.0


def _configured_max_current_amp(config, wb_id=1, default=16.0):
    """Binde die physische Stromgrenze je Ladepunkt konservativ.

    32 A sind nur zulässig, wenn sie für diesen Ladepunkt oder global
    ausdrücklich konfiguriert wurden. Ein fehlender oder ungültiger Wert darf
    an der letzten Treiberkante niemals stillschweigend aus 16 A 32 A machen.
    """

    cfg = config or {}
    try:
        cid = int(wb_id or 1)
    except (TypeError, ValueError):
        cid = 1
    raw = None
    for key in (
        f"wb{cid}_max_amp",
        f"wb{cid}_maxladestrom",
        "wbmaxladestrom",
        "wb_max_amp",
    ):
        value = cfg.get(key)
        if value is not None and str(value).strip() != "":
            raw = value
            break
    try:
        maximum = float(raw if raw is not None else default)
    except (TypeError, ValueError):
        maximum = float(default)
    if not math.isfinite(maximum):
        maximum = float(default)
    return max(6.0, min(32.0, maximum))


def _bind_configured_driver_limits(charger, config, wb_id):
    """Versiegle die Infrastrukturgrenze direkt am Treiberobjekt."""

    if charger is not None:
        charger.max_amp = _configured_max_current_amp(
            config,
            wb_id,
            default=16.0,
        )
    return charger


def _quantize_current_amp(target_amp, *, step=1.0, min_amp=6.0, max_amp=16.0):
    """Quantize a current setpoint at the driver/API boundary."""
    try:
        raw = float(target_amp or 0.0)
    except (TypeError, ValueError):
        raw = 0.0
    if raw < 0.5:
        return 0.0
    step = max(0.1, min(1.0, float(step or 1.0)))
    amp = round(raw / step) * step
    amp = max(float(min_amp), min(float(max_amp), amp))
    if step >= 0.99:
        return float(int(round(amp)))
    return round(amp, 1)


def _amp_api_value(amp):
    return int(round(amp)) if abs(float(amp) - round(float(amp))) < 0.01 else round(float(amp), 1)


def _amp_label(amp):
    amp = float(amp or 0.0)
    return str(int(round(amp))) if abs(amp - round(amp)) < 0.01 else f"{amp:.1f}"


MAX_PHASE_CURRENT_A = 32.0
MAX_PHASE_POWER_W = MAX_PHASE_CURRENT_A * 260.0
ZERO_SAMPLE_GLITCH_HOLD_S = 3.0


def _driver_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _validated_e3dc_phase_power(sub, tag_name, validator):
    """Liefert ausschließlich typbestätigte Double64-Phasenleistung."""

    value, valid = validator(sub, tag_name)
    if not valid or type(value) is not float:
        return None, False
    if not math.isfinite(value) or abs(value) > MAX_PHASE_POWER_W:
        return None, False
    return float(value), True


def discover_openwb_chargepoints(ip, timeout=3.0):
    """Read available openWB Software chargepoints without changing settings."""
    import urllib.request

    ip = str(ip or "").strip()
    if not ip:
        return []
    url = f"http://{ip}/openWB/simpleAPI/simpleapi.php?get_chargepoint_all"
    try:
        with urllib.request.urlopen(url, timeout=float(timeout or 3.0)) as ctx:
            raw = ctx.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except Exception as exc:
        logger.debug(f"openWB Discovery fehlgeschlagen ({ip}): {exc}")
        return []
    if not isinstance(payload, dict):
        return []

    found = []
    seen_ids = set()
    for key, value in payload.items():
        match = re.match(r"^chargepoint_(\d+)$", str(key))
        if not match or not isinstance(value, dict):
            continue
        cp_id = int(match.group(1))
        if cp_id <= 0 or cp_id in seen_ids:
            continue
        seen_ids.add(cp_id)
        name = str(value.get("config_name") or value.get("name") or f"Ladepunkt {cp_id}").strip()
        found.append({
            "id": cp_id,
            "name": name,
            "chargemode": str(value.get("chargemode") or "").strip(),
            "plug_state": value.get("plug_state"),
            "charge_state": value.get("charge_state"),
        })
    found.sort(key=lambda item: int(item.get("id", 0)))
    return found


# ===========================================================================
# Basisklasse
# ===========================================================================
class WallboxDriver:
    """Abstrakte Basisklasse für alle Wallbox-Treiber."""

    def __init__(self, ip, wb_id=1):
        self.ip    = ip
        self.wb_id = wb_id
        self._last_plausible_status = None
        self._last_plausible_status_ts = 0.0

    def _remember_plausible_status(self, status, now_ts=None):
        if isinstance(status, dict):
            now_value = time.time() if now_ts is None else float(now_ts)
            self._last_plausible_status = dict(status)
            self._last_plausible_status_ts = now_value
            status["driver_status_plausible"] = True
            if "driver_status_glitch" not in status:
                status["driver_status_glitch"] = False
            status["driver_status_last_good_ts"] = int(now_value)
        return status

    def _hold_previous_measurement(self, status, reason, now_ts=None):
        previous = self._last_plausible_status if isinstance(self._last_plausible_status, dict) else {}
        if not previous:
            return status
        for key in (
            "amp", "evse_current", "offered_current_raw",
            "real_power_w", "phase_power_l1_w", "phase_power_l2_w",
            "phase_power_l3_w", "phase_power_sum_w",
            "phase_power_verified", "phase_apparent_l1_va",
            "phase_apparent_l2_va", "phase_apparent_l3_va",
            "phase_current_l1_a", "phase_current_l2_a", "phase_current_l3_a",
            "apparent_power_va", "power_factor",
            "phases_in_use", "phases_actual", "pha",
            "charging", "charge_state",
        ):
            if key in previous:
                status[key] = previous[key]
        status["driver_status_plausible"] = False
        status["driver_status_glitch"] = True
        status["driver_status_glitch_reason"] = reason
        status["driver_status_last_good_ts"] = int(float(self._last_plausible_status_ts or 0.0))
        return status

    def _sanitize_measurement_status(self, status, now_ts=None):
        if not isinstance(status, dict):
            return status
        now_value = time.time() if now_ts is None else float(now_ts)
        previous = self._last_plausible_status if isinstance(self._last_plausible_status, dict) else {}
        previous_age_s = now_value - float(self._last_plausible_status_ts or 0.0)
        recent_previous = bool(previous and 0.0 <= previous_age_s <= ZERO_SAMPLE_GLITCH_HOLD_S)

        # OpenWB-Treiber verwenden ihr Status-Dictionary über mehrere Abfragen.
        # Eine alte Glitch-Markierung darf deshalb eine neue gültige Messung nicht
        # dauerhaft als unplausibel festhalten.
        status["driver_status_glitch"] = False
        status["driver_status_glitch_reason"] = ""
        if status.get("wb_status_valid") is False:
            status["driver_status_plausible"] = False
            status["driver_status_last_good_ts"] = int(float(self._last_plausible_status_ts or 0.0))
            return status

        current_fields = (
            "amp", "evse_current", "offered_current_raw",
            "phase_current_l1_a", "phase_current_l2_a", "phase_current_l3_a",
        )
        current_over_limit = any(_driver_float(status.get(key), 0.0) > MAX_PHASE_CURRENT_A + 0.1 for key in current_fields)
        phase_power_over_limit = any(
            abs(_driver_float(status.get(key), 0.0)) > MAX_PHASE_POWER_W
            for key in ("phase_power_l1_w", "phase_power_l2_w", "phase_power_l3_w")
        )
        if (current_over_limit or phase_power_over_limit) and recent_previous:
            return self._hold_previous_measurement(
                status,
                "current_over_32a" if current_over_limit else "phase_power_over_32a_equivalent",
                now_ts=now_value,
            )
        if current_over_limit:
            for key in current_fields:
                if _driver_float(status.get(key), 0.0) > MAX_PHASE_CURRENT_A + 0.1:
                    status[key] = MAX_PHASE_CURRENT_A
            status["amp"] = int(round(min(MAX_PHASE_CURRENT_A, _driver_float(status.get("amp"), 0.0))))
            status["driver_status_glitch"] = True
            status["driver_status_glitch_reason"] = "current_clamped_32a"
        if phase_power_over_limit:
            for key in ("phase_power_l1_w", "phase_power_l2_w", "phase_power_l3_w"):
                if abs(_driver_float(status.get(key), 0.0)) > MAX_PHASE_POWER_W:
                    status[key] = 0.0
            status["phase_power_sum_w"] = sum(_driver_float(status.get(key), 0.0) for key in ("phase_power_l1_w", "phase_power_l2_w", "phase_power_l3_w"))
            status["real_power_w"] = min(_driver_float(status.get("real_power_w"), 0.0), _driver_float(status.get("phase_power_sum_w"), 0.0))
            status["phase_power_verified"] = False
            status["driver_status_glitch"] = True
            status["driver_status_glitch_reason"] = "phase_power_discarded_32a_equivalent"

        current_power_w = _driver_float(status.get("real_power_w"), 0.0)
        current_amp_a = max(_driver_float(status.get("amp"), 0.0), _driver_float(status.get("evse_current"), 0.0))
        previous_power_w = _driver_float(previous.get("real_power_w"), 0.0) if previous else 0.0
        previous_amp_a = max(_driver_float(previous.get("amp"), 0.0), _driver_float(previous.get("evse_current"), 0.0)) if previous else 0.0
        previous_charging = bool(previous.get("charging") or previous.get("charge_state") or previous_power_w > 500.0)
        current_charging_flag = bool(status.get("charging") or status.get("charge_state"))
        if (
            recent_previous
            and previous_charging
            and previous_power_w > 500.0
            and previous_amp_a >= 5.5
            and current_charging_flag
            and current_power_w <= 50.0
            and current_amp_a <= 0.2
        ):
            return self._hold_previous_measurement(status, "single_zero_sample_during_charge", now_ts=now_value)

        if bool(status.get("driver_status_glitch", False)):
            status["driver_status_plausible"] = False
            status["driver_status_last_good_ts"] = int(float(self._last_plausible_status_ts or 0.0))
            return status
        return self._remember_plausible_status(status, now_value)


    def get_status(self):
        """Muss ueberschrieben werden.
        Erwartet: dict { 'car': 1..4, 'amp': int, 'pha': int, 'charging': bool }"""
        raise NotImplementedError()

    def set_amp_and_state(self, target_amp, force_state=None):
        """Setzt Ampere und optional State (1=Aus, 2=An)."""
        raise NotImplementedError()

    def release_to_default(self, max_amp=16):
        """Gibt die Wallbox in einen nutzbaren lokalen Default zurück."""
        return False


# ===========================================================================
# go-eCharger (V2 / V3 API)
# ===========================================================================
class GoECharger(WallboxDriver):
    """Treiber für go-eCharger über HTTP V2/V3 API."""

    def get_status(self):
        url          = f"http://{self.ip}/api/status?filter=car,amp,pnp,pha,frc,nrg"
        url_fallback = f"http://{self.ip}/api/status"
        try:
            try:
                response = _requests.get(url, timeout=5)
            except _requests.exceptions.ConnectionError:
                logger.warning(f"[WB{self.wb_id}] Filter-Request fehlgeschlagen, Fallback...")
                response = _requests.get(url_fallback, timeout=5)
            response.raise_for_status()
            data = response.json()

            # Echte Leistung aus nrg-Array (Index 11 = Total Watts)
            nrg        = data.get('nrg', [])
            real_power = 0.0
            if len(nrg) > 11:
                val = float(nrg[11])
                real_power = val
                if val < 3200 and data.get('amp', 0) > 6 and val > 0:
                    expected = float(data.get('amp', 6)) * 230.0
                    if val < (expected / 5.0):
                        real_power = val * 10.0  # Korrektur Dekawatt -> Watt

            def _int(key, default):
                v = data.get(key, default)
                if isinstance(v, list):
                    v = v[0] if v else default
                try:
                    return int(v) if v is not None else default
                except (ValueError, TypeError):
                    return default

            car_status = _int('car', 1)
            legacy_pha = _int('pha', 56)
            reported_phases = _int('pnp', 0)
            measured_phases = 0
            phase_currents = []
            if len(nrg) > 6:
                for phase_current in nrg[4:7]:
                    try:
                        current_value = abs(float(phase_current or 0.0))
                        phase_currents.append(current_value)
                        if current_value > 0.5:
                            measured_phases += 1
                    except (TypeError, ValueError):
                        phase_currents.append(0.0)
            while len(phase_currents) < 3:
                phase_currents.append(0.0)
            if measured_phases in (1, 2, 3) and (car_status == 2 or real_power > 500.0):
                phases_in_use = measured_phases
                phase_count_source = 'nrg_current'
            elif reported_phases in (1, 2, 3):
                phases_in_use = reported_phases
                phase_count_source = 'pnp'
            else:
                phases_in_use = (
                    3 if legacy_pha == 56
                    else (2 if legacy_pha == 24 else (1 if legacy_pha in (8, 16, 32) else 0))
                )
                phase_count_source = 'pha_legacy'
            status = {
                'car':         car_status,
                'amp':         _int('amp', 6),
                'pha':         legacy_pha,
                'phases_in_use': phases_in_use,
                'phase_actual_source': phase_count_source,
                'phase_current_l1_a': phase_currents[0],
                'phase_current_l2_a': phase_currents[1],
                'phase_current_l3_a': phase_currents[2],
                'frc':         _int('frc', 0),
                'charging':    car_status == 2,
                'real_power_w': real_power,
            }
            return self._sanitize_measurement_status(status)
        except Exception as e:
            logger.error(f"[WB{self.wb_id}] Fehler beim Lesen des go-eChargers ({self.ip}): {e}")
            return None

    def set_amp_and_state(self, target_amp, force_state=None):
        if not command_gate.allow_command(
            self,
            action="goe_set_amp_and_state",
            payload={"target_amp": target_amp, "force_state": force_state},
        ):
            return False
        amp = max(
            6,
            min(int(getattr(self, "max_amp", 16.0)), int(target_amp or 6)),
        )
        params = f"amp={amp}"
        if force_state is not None:
            params += f"&frc={int(force_state)}"
        url = f"http://{self.ip}/api/set?{params}"
        try:
            if not command_gate.allow_command(
                self,
                action="goe_set_amp_and_state_wire",
                payload={"target_amp": amp, "force_state": force_state},
                audit_allowed=False,
            ):
                return False
            response = _requests.get(url, timeout=5)
            response.raise_for_status()
            result = response.json()
            expected_keys = ["amp"]
            if force_state is not None:
                expected_keys.append("frc")
            if not isinstance(result, dict):
                logger.error(
                    f"[WB{self.wb_id}] go-eCharger lieferte keine JSON-Objekt-Antwort auf /api/set"
                )
                return False
            rejected = {
                key: result.get(key)
                for key in expected_keys
                if result.get(key) is not True
            }
            if rejected:
                logger.error(
                    f"[WB{self.wb_id}] go-eCharger hat Schreibwerte abgelehnt: {rejected}"
                )
                return False
            return True
        except Exception as e:
            logger.error(f"[WB{self.wb_id}] Fehler beim Schreiben auf go-eCharger ({self.ip}): {e}")
            return False

    def release_to_default(self, max_amp=16):
        """go-e: neutralen FRC-Modus mit vollem konfiguriertem Strom herstellen."""
        return self.set_amp_and_state(max_amp, force_state=0)


# ===========================================================================
# DummyCharger (Test/Simulation)
# ===========================================================================
class DummyCharger(WallboxDriver):
    """Simulator für Testumgebungen."""

    def __init__(self, ip, wb_id=1):
        super().__init__(ip, wb_id)
        self.state = {'car': 2, 'amp': 6, 'pha': 56, 'charging': False}

    def get_status(self):
        return self.state

    def set_amp_and_state(self, target_amp, force_state=None):
        if not command_gate.allow_command(
            self,
            action="dummy_set_amp_and_state",
            payload={"target_amp": target_amp, "force_state": force_state},
        ):
            return False
        self.state['amp'] = target_amp
        if force_state is not None:
            self.state['frc']      = force_state
            self.state['charging'] = (force_state == 2)
        logger.info(f"[WB{self.wb_id} DUMMY] set amp={target_amp}, frc={force_state}")
        return True

    def release_to_default(self, max_amp=16):
        if not command_gate.allow_command(
            self,
            action="dummy_release_to_default",
            payload={"max_amp": max_amp},
        ):
            return False
        self.state['amp'] = max_amp
        self.state['frc'] = 0
        self.state['charging'] = False
        logger.info(f"[WB{self.wb_id} DUMMY] Default-Freigabe amp={max_amp}, frc=0")
        return True


# ===========================================================================
# OpenWBCharger (openWB 2.x: HTTP SimpleAPI + Modbus Secondary)
# ===========================================================================
class OpenWBCharger(WallboxDriver):
    """Treiber für openWB Series 2.

    Die normale openWB ist ein eigener Energiemanager. Standard bleibt der
    evcc/openWB-Secondary-Pfad über Sollstrom plus Heartbeat. Wird
    wb_openwb_primary_enable gesetzt, bleibt openWB Primary und E3DC-Control
    schaltet nur die openWB-Modi PV/Sofort/Stop per simpleAPI. Aktive
    Stromvorgaben sind dabei ein Primary-Direktpfad über openWB-Sofortladen
    (chargecurrent); openWB-SoC- und Energiemengenlimits bleiben wirksam.

    Status lesen: GET  simpleapi.php?get_chargepoint_all=<ID>; Fahrzeug-SoC
                  zusätzlich über einen strikt lesenden MQTT-Abonnenten.
    Steuern:      HTTP-V1-Secondary-Topics; optional Modbus Secondary als
                  Rückfall, wenn der HTTP-Pfad nicht erreichbar ist.
    """

    def __init__(self, ip, wb_id=1, config=None):
        super().__init__(ip, wb_id)
        config = config or {}
        self.config = config
        observed_profile_id = str(config.get(f"wb{wb_id}_car_id", "") or "").strip()
        if observed_profile_id.lower() in ("__none", "none", "no_vehicle", "kein_fahrzeug", "0", "false"):
            observed_profile_id = ""
        self.current_step_amp = _current_step_from_config(config, wb_id, default=1.0)
        self.state = {
            'car':               1,
            'amp':               6,
            'pha':               56,
            'charging':          False,
            'frc':               0,
            'plug_state':        False,
            'locked':            False,
            'charge_state':      False,
            'real_power_w':      0.0,
            'phase_power_l1_w':  0.0,
            'phase_power_l2_w':  0.0,
            'phase_power_l3_w':  0.0,
            'phase_power_sum_w': 0.0,
            'phase_power_verified': False,
            'phase_apparent_l1_va': 0.0,
            'phase_apparent_l2_va': 0.0,
            'phase_apparent_l3_va': 0.0,
            'apparent_power_va': 0.0,
            'power_factor': 0.0,
            'evse_current':      0.0,
            'phases_in_use':     0,
            'can_switch_phases': False,
            'phase_switch_capability': 'secondary_current_only',
            'phase_switch_source': 'disabled_by_design',
            'api_surface':        'openwb_secondary_set_current_heartbeat',
            'control_status':     'self_regulated',
            'control_label':      'openWB regelt selbst',
            'control_detail':     'E3DC-Control beobachtet die Leistung und führt den Speicher; openWB führt Ladepunkt, PV-Logik und Phasen.',
            'control_level':      'info',
            'control_ts':         0,
            'last_command_ok':    None,
            'last_command_amp':   None,
            'last_command_ts':    0,
            'current_step_amp':    self.current_step_amp,
            'fractional_current_supported': self.current_step_amp < 1.0,
            'last_heartbeat_ok':  None,
            'last_heartbeat_ts':  0.0,
            'http_auth_configured': False,
            'configured_role':    'secondary_http',
            'detected_role':      '',
            'effective_role':     'secondary_http',
            'role_detection_source': '',
            'role_detection_detail': '',
            'role_mismatch':      False,
            'command_failure_count': 0,
            'command_failure_limit': 3,
            'command_blocked':    False,
            'command_blocked_until': 0,
            'connected_phases':   0,
            'daily_imported_wh': 0.0,
            'imported_total_wh': 0.0,
            'chargemode_str':    'stop',
            'chargepoint_name':  '',
            'charge_template_name': '',
            'vehicle_identity_current': False,
            'stable_vehicle_identity_current': False,
            'state_text':        '',
            'fault_text':        '',
            'fault_state':       0,
            'manual_lock':       False,
            'min_current':       0.0,
            'pv_charging_min_current': 0.0,
            'instant_charging_current': 0.0,
            'primary_start_stage': '',
            'primary_start_target_amp': None,
            'primary_start_current_sent_ts': 0.0,
            'instant_charging_limit': '',
            'instant_charging_soc': 0.0,
            'session_kwh':       0.0,
            '_session_start_wh': None,
            '_session_start_ts': None,
            '_plug_state_observed': False,
            '_daily_imported_observed': False,
            'cp_id':             '',
            'chargepoint_detection_source': '',
            # Fahrzeug-Info aus connected_vehicle/info
            'car_name':          '',
            'car_id':            None,
            'vehicle_id':        None,
            'rfid_tag':          None,
            'car_soc_source':    '',
            'car_soc_source_ts': None,
            'car_soc_raw_ts':    None,
            'car_soc_rule_confirmed': False,
            # Reine Anzeige-Beobachtung. Diese Felder sind bewusst vom
            # Regelvertrag getrennt: Retains und HTTP-Werte ohne echten
            # Produzentenzeitpunkt dürfen sichtbar sein, aber keine
            # Ladeentscheidung bestätigen.
            'car_soc_observed': None,
            'car_soc_observed_source': '',
            'car_soc_observed_source_ts': None,
            'car_soc_observed_received_ts': 0,
            'car_soc_observed_retained': False,
            'car_soc_display_usable': False,
            'car_soc_observed_session_start_ts': None,
            'car_soc_observed_vehicle_id': '',
            # Reine Konfigurationszuordnung für die Beschriftung. Sie ist
            # weder Live-Fahrzeugidentität noch Regelautorität.
            'car_soc_observed_profile_id': observed_profile_id,
            '_simpleapi_soc_pending': {},
            'car_capacity_kwh':  0.0,
            'car_consumption_kwh_100km': 0.0,
            'car_range':         0.0,
            'car_range_source':  '',
            'car_range_valid':   False,
            'car_range_observed_ts': 0,
            'car_range_source_ts': None,
            'car_range_source_ts_explicit': False,
            'car_range_vehicle_key': '',
            'car_charged_range': 0.0,
            'car_charged_range_source': '',
            'car_charged_range_valid': False,
            'car_charged_range_observed_ts': 0,
            'car_charged_range_source_ts': None,
            'car_charged_range_source_ts_explicit': False,
            'car_charged_range_vehicle_key': '',
        }

        # CP-ID aus Topic-Prefix extrahieren
        import re
        cfg_prefix = config.get(f"wb{wb_id}_topic_prefix", "")
        self.cp_id = ""
        cp_source = ""
        if cfg_prefix:
            m = re.search(r'(?:chargepoint|lp)[/\\](\d+)', cfg_prefix)
            if m:
                prefix_cp = int(m.group(1))
                if prefix_cp > 0:
                    self.cp_id = prefix_cp
                    cp_source = "config_topic_prefix"
        # Der historische globale CP-Key gehört ausschließlich zu WB1. Ein
        # per-Slot-Prefix hat immer Vorrang; sonst würden WB1 und WB2 trotz
        # unterschiedlicher Prefixe denselben physischen Ladepunkt ansteuern.
        if self.cp_id == "" and int(wb_id or 1) == 1:
            cp_cfg = config.get("wb_native_cp_id", None)
            if cp_cfg is not None and str(cp_cfg).strip().isdigit():
                cp_int = int(cp_cfg)
                if cp_int > 0:
                    self.cp_id = cp_int
                    cp_source = "config_global_cp_id"

        self.cp_suffix = f"/{self.cp_id}" if self.cp_id != "" else ""
        self.native_prefix   = f"openWB/chargepoint{self.cp_suffix}/get"
        self.simpleapi_prefix = f"openWB/simpleAPI/chargepoint{self.cp_suffix}"
        self.http_api_url    = f"http://{self.ip}/openWB/simpleAPI/simpleapi.php"
        self.http_v1_url     = f"https://{self.ip}:8443/v1/"
        self.http_user       = self._config_first_nonempty(f"wb{wb_id}_user", "wb_user")
        self.http_pass       = self._config_first_nonempty(f"wb{wb_id}_pass", "wb_pass", strip=False)
        self.state["http_auth_configured"] = bool(self.http_user)
        self.api_duo_num     = int(self.config.get("openwb_api_duo_num", self.config.get("openwb_duo_num", 0)) or 0)
        self.auto_role_enabled = self._bool_value(
            self.config.get(
                f"wb{wb_id}_openwb_auto_role_enable",
                self.config.get("wb_openwb_auto_role_enable", "1"),
            ),
            True,
        )
        self.configured_primary_mode_enabled = self._bool_value(
            self.config.get(
                f"wb{wb_id}_openwb_primary_enable",
                self.config.get("wb_openwb_primary_enable", "0"),
            )
        )
        self.configured_modbus_enabled = self._bool_value(
            self.config.get(
                f"wb{wb_id}_openwb_modbus_secondary_enable",
                self.config.get("wb_openwb_modbus_secondary_enable", "0"),
            )
        )
        self.primary_mode_enabled = self.configured_primary_mode_enabled
        self.modbus_enabled = self.configured_modbus_enabled
        self.command_failure_limit = max(1, int(float(
            self.config.get(
                f"wb{wb_id}_openwb_command_fail_limit",
                self.config.get("wb_openwb_command_fail_limit", 3),
            ) or 3
        )))
        self.command_block_s = max(30.0, float(
            self.config.get(
                f"wb{wb_id}_openwb_command_block_s",
                self.config.get("wb_openwb_command_block_s", 300),
            ) or 300
        ))
        self._command_failure_count = 0
        self._command_blocked_until = 0.0
        self._cp_config_cache_until = 0.0
        self._cp_config_cache = None
        self.modbus_port = int(float(
            self.config.get(
                f"wb{wb_id}_openwb_modbus_port",
                self.config.get("wb_openwb_modbus_port", 1502),
            ) or 1502
        ))
        self.modbus_unit = int(float(
            self.config.get(
                f"wb{wb_id}_openwb_modbus_unit",
                self.config.get("wb_openwb_modbus_unit", 1),
            ) or 1
        ))
        self.modbus_offset = int(float(
            self.config.get(
                f"wb{wb_id}_openwb_modbus_offset",
                self.config.get("wb_openwb_modbus_offset", 0),
            ) or 0
        ))
        connector_cfg = self.config.get(
            f"wb{wb_id}_openwb_modbus_connector",
            self.config.get("wb_openwb_modbus_connector", ""),
        )
        self._modbus_connector_configured = connector_cfg not in (None, "")
        if connector_cfg in (None, ""):
            connector_cfg = self.cp_id if self.cp_id != "" else 1
        self.modbus_connector = max(1, int(float(connector_cfg or 1)))
        if self.primary_mode_enabled:
            self.state["phase_switch_capability"] = "openwb_primary_mode_only"
            self.state["api_surface"] = "openwb_primary_simpleapi"
        elif self.modbus_enabled:
            self.state["phase_switch_capability"] = "secondary_modbus_current_only"
            self.state["api_surface"] = "openwb_secondary_modbus"
        self._apply_effective_role(
            self._configured_role(),
            detected_role="",
            source="config",
            detail="Gespeicherte openWB-Konfiguration.",
        )
        self.secondary_parent_ip = str(
            self.config.get("wb_openwb_parent_ip", self.config.get("openwb_parent_ip", ""))
            or ""
        ).strip()
        self.topic_prefix    = cfg_prefix or self.native_prefix
        discovery_contract = config.get(
            f"_wb{int(wb_id or 1)}_openwb_discovery_contract",
            {},
        )
        discovery_contract = (
            dict(discovery_contract)
            if isinstance(discovery_contract, dict)
            else {}
        )
        discovery_cp = discovery_contract.get("cp_id")
        try:
            discovery_cp = int(discovery_cp)
        except (TypeError, ValueError):
            discovery_cp = 0
        discovery_valid = bool(
            discovery_contract.get("valid") is True
            and discovery_cp > 0
            and self.cp_id == discovery_cp
            and float(discovery_contract.get("detected_at", 0.0) or 0.0) > 0.0
        )
        self._auto_discovery_bound = discovery_valid
        self._auto_discovery_status_confirmed = False
        if discovery_valid:
            cp_source = str(
                discovery_contract.get("source")
                or "manager_simpleapi_discovery"
            )
            discovery_contract = {
                "schema_version": "openwb_chargepoint_discovery_v1",
                "valid": True,
                "source": cp_source,
                "detected_at": int(
                    float(discovery_contract.get("detected_at", 0.0) or 0.0)
                ),
                "controller_identity": str(
                    discovery_contract.get("controller_identity") or ""
                ),
                "cp_id": discovery_cp,
                "peer_cp_id": int(
                    float(discovery_contract.get("peer_cp_id", 0) or 0)
                ),
                "status_confirmed": False,
                "status_confirmed_ts": 0,
            }
        else:
            discovery_contract = {}
        self.state["cp_id"] = self.cp_id
        self.state["chargepoint_detection_source"] = cp_source
        self.state["chargepoint_discovery_contract"] = discovery_contract
        # Ein automatisch hinzugefügter WB2 beginnt ohne Schreibautorität. Der
        # Manager hebt diese Sperre erst nach frischem Status und nachweislich
        # eindeutiger Befehlsidentität auf.
        self._physical_output_blocked = bool(
            discovery_valid and int(wb_id or 1) == 2
        )
        self._physical_output_block_reason = (
            "auto_discovery_waits_for_fresh_distinct_actuator"
            if self._physical_output_blocked
            else ""
        )
        self._physical_output_last_log_ts = 0.0
        self._last_command_key = None
        self._last_command_ts = 0.0
        self._primary_pending_current_amp = None
        self._primary_pending_current_sent_ts = 0.0
        self._primary_pending_current_timeout_s = 30.0

        logger.info(f"[WB{self.wb_id}] openWB 2.x HTTP SimpleAPI: IP={self.ip}, CP={self.cp_id if self.cp_id != '' else 'AUTO'}")
        logger.info(f"[WB{self.wb_id}]   Status-GET   : get_chargepoint_all={self.cp_id if self.cp_id != '' else 'auto'}")
        logger.info(f"[WB{self.wb_id}]   HTTP-API     : {self.http_api_url}")
        logger.info(f"[WB{self.wb_id}]   HTTP-Auth    : {'Basic konfiguriert' if self.http_user else 'aus'}")
        control_path = "Primary simpleAPI (PV/Sofortladen/Stop)" if self.primary_mode_enabled else (
            "Modbus Secondary" if self.modbus_enabled else "HTTP V1 set_current + Heartbeat"
        )
        logger.info(
            f"[WB{self.wb_id}]   Steuerpfad   : "
            f"{control_path}"
        )
        self.mqtt_client = None
        self._mqtt_subscribed_topics = set()
        legacy_mqtt = str(
            self.config.get("openwb_mqtt_legacy_enable", self.config.get("wb_openwb_mqtt_legacy_enable", "0"))
        ).strip().lower() in ("1", "true", "yes", "on")
        self._mqtt_legacy_enabled = legacy_mqtt
        # Der Standardpfad ist ein strikt lesender SoC-Abonnent. Retained
        # Leistung, Plug- und Schaltzustände bleiben hinter dem ausdrücklichen
        # Legacy-Schalter, damit sie die HTTP-Wahrheit nicht überlagern.
        mqtt_module = _get_mqtt_module()
        if mqtt_module is not None:
            try:
                self.mqtt_client = mqtt_module.Client()
                self.mqtt_client.on_connect = self.on_connect
                self.mqtt_client.on_disconnect = self.on_disconnect
                self.mqtt_client.on_message = self.on_message
                if hasattr(self.mqtt_client, "reconnect_delay_set"):
                    self.mqtt_client.reconnect_delay_set(min_delay=2, max_delay=60)
                self.mqtt_client.connect_async(self.ip, 1883, 60)
                self.mqtt_client.loop_start()
                logger.info(f"[WB{self.wb_id}] MQTT-Lesepfad für Fahrzeugdaten gestartet.")
            except Exception as e:
                self.mqtt_client = None
                logger.debug(f"[WB{self.wb_id}] MQTT-Lesepfad nicht verfuegbar: {e}")

    def _config_first_nonempty(self, *keys, strip=True):
        """Ermittelt den ersten gesetzten Konfigwert ohne leere Fallbacks."""
        for key in keys:
            if key not in self.config:
                continue
            value = self.config.get(key)
            if value is None:
                continue
            text = str(value)
            if strip:
                text = text.strip()
            if text != "":
                return text
        return ""

    def _set_runtime_chargepoint_id(self, cp_id, source="simpleapi_auto"):
        """Übernimmt eine per openWB-Autoerkennung gelesene Ladepunktnummer."""
        try:
            cp_int = int(cp_id)
        except (TypeError, ValueError):
            return False
        if cp_int <= 0:
            return False
        if self.cp_id == cp_int:
            return False
        old_cp = self.cp_id
        self.cp_id = cp_int
        self.cp_suffix = f"/{self.cp_id}"
        self.native_prefix = f"openWB/chargepoint{self.cp_suffix}/get"
        self.simpleapi_prefix = f"openWB/simpleAPI/chargepoint{self.cp_suffix}"
        if not getattr(self, "_modbus_connector_configured", False):
            self.modbus_connector = cp_int
        # Ein SoC-Halbpaar oder eine Beobachtung aus dem bisherigen
        # Ladepunkt-Namespace darf nicht unter der neu gebundenen CP-ID
        # weiterleben. Der aktuelle HTTP-Snapshot wird anschließend normal
        # unter der erkannten ID neu ausgewertet.
        self.state.update({
            "_simpleapi_soc_pending": {},
            "car_soc_observed": None,
            "car_soc_observed_source": "",
            "car_soc_observed_source_ts": None,
            "car_soc_observed_received_ts": 0,
            "car_soc_observed_retained": False,
            "car_soc_display_usable": False,
            "car_soc_observed_session_start_ts": None,
            "car_soc_observed_vehicle_id": "",
        })
        self.state["cp_id"] = self.cp_id
        self.state["chargepoint_detection_source"] = str(source or "simpleapi_auto")
        logger.info(
            f"[WB{self.wb_id}] openWB Auto-Ladepunkt übernommen: "
            f"CP={self.cp_id} (vorher {old_cp if old_cp != '' else 'AUTO'})"
        )
        if (
            self.mqtt_client is not None
            and self.state.get("mqtt_connected") is True
        ):
            self._sync_mqtt_subscriptions(self.mqtt_client)
        return True

    @staticmethod
    def _identity_digest(material):
        return "sha256:" + hashlib.sha256(
            str(material or "").encode("utf-8")
        ).hexdigest()

    def physical_output_identity_contract(self):
        """Beschreibt den tatsächlich verwendeten openWB-Schreibausgang.

        Der Vertrag leitet sich ausschließlich aus den bereits implementierten
        SimpleAPI-, HTTP-V1- und Modbus-Pfaden ab. Er erfindet keine Discovery
        und keinen Geräteendpunkt.
        """
        host = str(self.ip or "").strip().lower()
        controller_identity = (
            self._identity_digest("openwb-controller|" + host)
            if host
            else ""
        )
        try:
            cp_id = int(self.cp_id)
        except (TypeError, ValueError):
            cp_id = 0
        role = str(
            self.state.get("effective_role")
            or self._configured_role()
            or ""
        ).strip().lower()
        endpoint_kind = ""
        material = ""
        endpoint_detail = ""
        if self.primary_mode_enabled:
            endpoint_kind = "primary_simpleapi"
            if host and cp_id > 0:
                material = f"{host}|simpleapi|chargepoint|{cp_id}"
                endpoint_detail = f"chargepoint:{cp_id}"
        elif self.modbus_enabled:
            endpoint_kind = "secondary_modbus"
            try:
                port = int(self.modbus_port)
                unit = int(self.modbus_unit)
                current_register = int(self._modbus_register(10171))
            except (TypeError, ValueError, OverflowError):
                port = unit = current_register = -1
            if host and 0 < port <= 65535 and 0 <= unit <= 247 and current_register >= 0:
                material = (
                    f"{host}|modbus|{port}|{unit}|register|"
                    f"{current_register}"
                )
                endpoint_detail = (
                    f"unit:{unit}:register:{current_register}"
                )
        else:
            endpoint_kind = "secondary_http_v1"
            try:
                duo_num = int(self.api_duo_num)
            except (TypeError, ValueError, OverflowError):
                duo_num = -1
            if host and duo_num >= 0:
                material = (
                    f"{host}|http_v1|internal_chargepoint|{duo_num}|"
                    "set_current"
                )
                endpoint_detail = f"internal_chargepoint:{duo_num}"

        discovery = self.state.get("chargepoint_discovery_contract")
        discovery = discovery if isinstance(discovery, dict) else {}
        binding_valid = bool(
            not self._auto_discovery_bound
            or self._auto_discovery_status_confirmed
        )
        contract = {
            "schema_version": "openwb_physical_output_identity_v1",
            "valid": bool(material and binding_valid),
            "identity": self._identity_digest(material) if material else "",
            "controller_identity": controller_identity,
            "endpoint_kind": endpoint_kind,
            "endpoint_detail": endpoint_detail,
            "effective_role": role,
            "cp_id": cp_id,
            "chargepoint_detection_source": str(
                self.state.get("chargepoint_detection_source") or ""
            ),
            "auto_discovery_bound": bool(self._auto_discovery_bound),
            "auto_discovery_status_confirmed": bool(
                self._auto_discovery_status_confirmed
            ),
            "discovery_source": str(discovery.get("source") or ""),
        }
        self.state["physical_output_identity"] = dict(contract)
        return contract

    def _physical_output_write_allowed(self, action):
        if not bool(getattr(self, "_physical_output_blocked", False)):
            return True
        now = time.time()
        if now - float(self._physical_output_last_log_ts or 0.0) >= 30.0:
            self._physical_output_last_log_ts = now
            logger.warning(
                "[WB%d] openWB-Schreibausgang %s blockiert: %s",
                int(self.wb_id or 0),
                str(action or "command"),
                str(
                    self._physical_output_block_reason
                    or "physical_output_identity_not_unique"
                ),
            )
        self.state["physical_output_blocked"] = True
        self.state["physical_output_block_reason"] = str(
            self._physical_output_block_reason
            or "physical_output_identity_not_unique"
        )
        return False

    def _http_headers(self, extra=None):
        """Gemeinsame HTTP-Header für openWB simpleAPI/V1, optional mit Basic Auth."""
        headers = dict(extra or {})
        if self.http_user:
            token = base64.b64encode(f"{self.http_user}:{self.http_pass}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        return headers

    def _current_range_vehicle_key(self):
        if not bool(self.state.get("stable_vehicle_identity_current", False)):
            return ""
        return str(
            self.state.get("vehicle_id")
            or self.state.get("rfid_tag")
            or self.state.get("car_id")
            or ""
        ).strip()

    def _set_total_range(self, value, source, *, observed_ts=None, source_ts=None, vehicle_key=""):
        value = self._float_value(value, 0.0)
        if value <= 0.0:
            self._clear_total_range(source=source)
            return False
        now_ts = time.time() if observed_ts is None else float(observed_ts)
        source_timestamp_missing = source_ts is None or (
            isinstance(source_ts, str)
            and source_ts.strip().lower() in ("", "null")
        )
        source_timestamp = None if source_timestamp_missing else source_ts
        self.state.update({
            "car_range": value,
            "car_range_source": str(source or ""),
            "car_range_valid": True,
            "car_range_observed_ts": int(now_ts),
            "car_range_source_ts": source_timestamp,
            "car_range_source_ts_explicit": not source_timestamp_missing,
            "car_range_vehicle_key": str(vehicle_key or "").strip(),
        })
        return True

    def _clear_total_range(self, source=None):
        if source and str(self.state.get("car_range_source") or "") != str(source):
            return
        self.state.update({
            "car_range": 0.0,
            "car_range_source": "",
            "car_range_valid": False,
            "car_range_observed_ts": 0,
            "car_range_source_ts": None,
            "car_range_source_ts_explicit": False,
            "car_range_vehicle_key": "",
        })

    def _set_charged_range(self, value, source, *, observed_ts=None, source_ts=None, vehicle_key=""):
        value = self._float_value(value, -1.0)
        if value < 0.0:
            self._clear_charged_range(source=source)
            return False
        now_ts = time.time() if observed_ts is None else float(observed_ts)
        source_timestamp_missing = source_ts is None or (
            isinstance(source_ts, str)
            and source_ts.strip().lower() in ("", "null")
        )
        source_timestamp = None if source_timestamp_missing else source_ts
        self.state.update({
            "car_charged_range": value,
            "car_charged_range_source": str(source or ""),
            "car_charged_range_valid": True,
            "car_charged_range_observed_ts": int(now_ts),
            "car_charged_range_source_ts": source_timestamp,
            "car_charged_range_source_ts_explicit": not source_timestamp_missing,
            "car_charged_range_vehicle_key": str(vehicle_key or "").strip(),
        })
        return True

    def _clear_charged_range(self, source=None):
        if source and str(self.state.get("car_charged_range_source") or "") != str(source):
            return
        self.state.update({
            "car_charged_range": 0.0,
            "car_charged_range_source": "",
            "car_charged_range_valid": False,
            "car_charged_range_observed_ts": 0,
            "car_charged_range_source_ts": None,
            "car_charged_range_source_ts_explicit": False,
            "car_charged_range_vehicle_key": "",
        })

    @classmethod
    def _extract_total_range(cls, payload):
        """Lese ausschließlich die openWB-Gesamtreichweite, nie range_charged."""

        if not isinstance(payload, dict) or "range" not in payload:
            return 0.0, False
        value = cls._float_value(payload.get("range"), 0.0)
        return value, True

    def _mqtt_subscription_topics(self):
        topics = [
            f"{self.native_prefix}/connected_vehicle/soc",
            f"{self.simpleapi_prefix}/soc/soc",
            f"{self.simpleapi_prefix}/soc/timestamp",
        ]
        if bool(getattr(self, "_mqtt_legacy_enabled", False)):
            topics.extend([
                f"{self.native_prefix}/plug_state",
                f"{self.native_prefix}/charge_state",
                f"{self.native_prefix}/power",
                f"{self.native_prefix}/powers",
                f"{self.native_prefix}/evse_current",
                f"{self.native_prefix}/phases_in_use",
                f"{self.native_prefix}/daily_imported",
                f"{self.native_prefix}/imported",
                f"{self.native_prefix}/fault_str",
                f"{self.native_prefix}/state_str",
                f"{self.native_prefix}/connected_vehicle/range",
                f"{self.native_prefix}/connected_vehicle/info",
                f"{self.simpleapi_prefix}/soc/range",
                f"{self.simpleapi_prefix}/chargemode",
                f"openWB/chargepoint{self.cp_suffix}/config",
            ])
        return topics

    def _sync_mqtt_subscriptions(self, client, *, reconnect=False):
        """Bindet den reinen Lesepfad an die aktuell erkannte Ladepunkt-ID."""

        desired = set(self._mqtt_subscription_topics())
        previous = set() if reconnect else set(self._mqtt_subscribed_topics)
        if not reconnect and hasattr(client, "unsubscribe"):
            for topic in sorted(previous - desired):
                try:
                    client.unsubscribe(topic)
                except Exception as exc:
                    logger.debug(
                        f"[WB{self.wb_id}] MQTT-Leseabonnement konnte nicht "
                        f"abgemeldet werden ({topic}): {exc}"
                    )
        subscribed = set(previous & desired)
        for topic in sorted(desired - subscribed):
            try:
                client.subscribe(topic)
                subscribed.add(topic)
            except Exception as exc:
                logger.warning(
                    f"[WB{self.wb_id}] MQTT-Leseabonnement konnte nicht "
                    f"gebunden werden ({topic}): {exc}"
                )
        self._mqtt_subscribed_topics = subscribed
        return len(subscribed)

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        rc = reason_code if isinstance(reason_code, int) else (0 if str(reason_code) == 'Success' else 1)
        if rc == 0:
            self.state["mqtt_reconnect_backoff_s"] = 0
            self.state["mqtt_reconnect_backoff_max_s"] = 60
            self.state["mqtt_connected"] = True
            logger.info(f"[WB{self.wb_id}] MQTT: Verbunden mit openWB ({self.ip}), CP={self.cp_id}")
            topic_count = self._sync_mqtt_subscriptions(client, reconnect=True)
            logger.info(f"[WB{self.wb_id}] MQTT: {topic_count} Topics abonniert (incl. Auto-SoC)")
        else:
            self.state["mqtt_connected"] = False
            logger.error(f"[WB{self.wb_id}] MQTT: Connection failed (rc={rc})")

    def on_disconnect(self, client, userdata, *args):
        reason_code = args[-2] if len(args) >= 2 else (args[0] if args else None)
        rc_text = str(reason_code if reason_code is not None else "")
        self.state["mqtt_connected"] = False
        self.state["mqtt_reconnect_backoff_s"] = 2
        self.state["mqtt_reconnect_backoff_max_s"] = 60
        logger.warning(
            f"[WB{self.wb_id}] MQTT: Verbindung getrennt (rc={rc_text}); "
            "Reconnect läuft mit Backoff 2s..60s."
        )

    def on_message(self, client, userdata, msg):
        try:
            topic       = msg.topic
            payload_str = msg.payload.decode('utf-8').strip()
            if not payload_str:
                return

            # --- Skalare SoC-Projektion der openWB simpleAPI ---
            # openWB liefert Wert und echten Produzentenzeitpunkt in zwei
            # Topics. Sie werden nur innerhalb eines engen Empfangsfensters
            # gepaart. Der Lesepfad publiziert selbst keinerlei MQTT-Befehle.
            if topic in (
                f"{self.simpleapi_prefix}/soc/soc",
                f"{self.simpleapi_prefix}/soc/timestamp",
            ):
                observed_ts = time.time()
                pending = self.state.setdefault('_simpleapi_soc_pending', {})
                if topic == f"{self.simpleapi_prefix}/soc/soc":
                    car_soc = self._soc_percent_value(payload_str.strip('"'))
                    if car_soc is None:
                        return
                    pending.update({
                        "value": car_soc,
                        "value_received_ts": observed_ts,
                    })
                else:
                    source_ts = self._soc_source_timestamp(
                        payload_str.strip('"'),
                        now_ts=observed_ts,
                    )
                    if source_ts is None:
                        return
                    pending.update({
                        "source_ts": source_ts,
                        "timestamp_received_ts": observed_ts,
                    })
                self._apply_simpleapi_soc_pair(observed_ts=observed_ts)
                return

            # --- Auto-SoC aus openWB (MQTT JSON) ---
            if topic == f"{self.native_prefix}/connected_vehicle/soc":
                try:
                    soc_data = json.loads(payload_str)
                    if not isinstance(soc_data, dict):
                        return
                    car_soc = self._soc_percent_value(soc_data.get("soc"))
                    if car_soc is not None:
                        observed_ts = time.time()
                        openwb_ts = soc_data.get("timestamp", None)
                        explicit_soc_ts = self._soc_source_timestamp(
                            openwb_ts,
                            now_ts=observed_ts,
                        )
                        retained = bool(getattr(msg, "retain", False))
                        previous_soc_ts = self._soc_source_timestamp(
                            self.state.get("car_soc_source_ts"),
                            now_ts=observed_ts,
                        )
                        previous_confirmed = bool(
                            self.state.get("car_soc_rule_confirmed") is True
                            and previous_soc_ts is not None
                        )
                        soc_ts = (
                            explicit_soc_ts
                            if explicit_soc_ts is not None
                            else (int(observed_ts) if not retained else None)
                        )
                        source = "openwb_mqtt_retained" if retained else "openwb_mqtt"
                        self._record_soc_observation(
                            car_soc,
                            source="openwb_mqtt",
                            observed_ts=observed_ts,
                            # Für die reine Anzeige niemals Empfangszeit als
                            # Produzentenzeit ausgeben. Der Legacy-Regelpfad
                            # darf sein bisheriges Verhalten separat behalten.
                            source_ts=explicit_soc_ts,
                            retained=retained,
                        )
                        if not bool(getattr(self, "_mqtt_legacy_enabled", False)):
                            # Der neue Standard-Abonnent ist reine Anzeige.
                            # Nur der explizit aktivierte alte MQTT-Pfad darf
                            # seinen bisherigen Regelvertrag weiterführen.
                            return
                        if (
                            retained
                            and previous_confirmed
                        ):
                            # Ein Retain ist kein neuer Fahrzeugmesswert. Eine
                            # bereits bestätigte Live-Wahrheit bleibt erhalten.
                            self.state["mqtt_retained_received_at"] = int(observed_ts)
                            return
                        rule_confirmed = bool(not retained and soc_ts is not None)
                        if (
                            rule_confirmed
                            and previous_confirmed
                            and soc_ts < previous_soc_ts
                        ):
                            # Ein später empfangenes, aber älter gemessenes
                            # Ereignis darf den frischeren SoC nicht ersetzen.
                            self.state["car_soc_unconfirmed_observed"] = car_soc
                            self.state["car_soc_unconfirmed_source"] = source
                            self.state["car_soc_unconfirmed_observed_ts"] = int(observed_ts)
                            return
                        vehicle_key = self._current_range_vehicle_key()
                        self.state['car_soc'] = car_soc
                        self.state['car_soc_source'] = source
                        self.state['car_soc_source_ts'] = soc_ts
                        self.state['car_soc_raw_ts'] = soc_ts
                        self.state['car_soc_rule_confirmed'] = rule_confirmed
                        if "range_charged" in soc_data:
                            self._set_charged_range(
                                soc_data.get("range_charged"),
                                "mqtt_charged",
                                observed_ts=observed_ts,
                                source_ts=openwb_ts,
                                vehicle_key=vehicle_key,
                            )
                        range_val, range_present = self._extract_total_range(soc_data)
                        if range_present:
                            self._set_total_range(
                                range_val,
                                "mqtt_total",
                                observed_ts=observed_ts,
                                source_ts=openwb_ts,
                                vehicle_key=vehicle_key,
                            )
                        is_plugged = self.state.get('plug_state', False)
                        soc_age_h = max(
                            0.0,
                            (observed_ts - float(soc_ts or observed_ts)) / 3600.0,
                        )

                        self._write_manual_soc(
                            car_soc,
                            is_plugged,
                            soc_ts,
                            source=source,
                            soc_source_ts=soc_ts,
                            raw_soc_ts=soc_ts,
                            rule_confirmed=rule_confirmed,
                        )
                        state_str = "eingesteckt" if is_plugged else f"nicht eingesteckt (letzter Wert vor {soc_age_h:.1f}h)"
                        last_soc = self.state.get('_last_logged_soc', -100)
                        if abs(car_soc - last_soc) >= 5.0:
                            logger.info(f"[WB{self.wb_id}] Auto-SoC aus openWB: {car_soc:.1f}% ({state_str})")
                            self.state['_last_logged_soc'] = car_soc
                except Exception:
                    pass
                return

            # --- Auto-Range aus openWB ---
            if topic in (f"{self.native_prefix}/connected_vehicle/range", f"{self.simpleapi_prefix}/soc/range"):
                try:
                    range_val = float(payload_str)
                    if range_val > 0:
                        observed_ts = time.time()
                        self._set_total_range(
                            range_val,
                            "mqtt_total",
                            observed_ts=observed_ts,
                            source_ts=None,
                            vehicle_key=self._current_range_vehicle_key(),
                        )
                        last_range = self.state.get('_last_logged_range', -1)
                        if abs(range_val - last_range) >= 10.0:
                            logger.info(f"[WB{self.wb_id}] Auto-Range aus openWB: {int(range_val)} km")
                            self.state['_last_logged_range'] = range_val
                except Exception:
                    pass
                return

            # --- Fahrzeug-Info aus openWB (Name, ID, Kapazitaet) ---
            if topic == f"{self.native_prefix}/connected_vehicle/info":
                try:
                    info = json.loads(payload_str)
                    name = info.get('name', '') or info.get('vehicle_name', '')
                    vid  = info.get('id', None)
                    cap  = float(info.get('capacity', 0) or info.get('bat_capacity', 0) or 0)
                    if name:
                        old_name = self.state.get('car_name', '')
                        self.state['car_name'] = name
                        self.state['car_id']   = vid
                        if cap > 0:
                            self.state['car_capacity_kwh'] = cap
                        if name != old_name:
                            logger.info(f"[WB{self.wb_id}] Fahrzeug: '{name}' (ID={vid}, {cap:.0f}kWh)")
                except Exception:
                    pass
                return

            # --- openWB Config (Phasen-Capability) ---
            if topic == f"openWB/chargepoint{self.cp_suffix}/config":
                try:
                    cfg = json.loads(payload_str)
                    connected_phases = int(cfg.get('connected_phases', 1) or 1)
                    # Normale openWB wird als Secondary-Energiemanager gefuehrt:
                    # Wir senden nur Sollstrom + Heartbeat, keine internen
                    # Phasenbefehle. Die Capability bleibt nur Diagnose.
                    self.state['can_switch_phases'] = False
                    self.state['phase_switch_capability'] = 'secondary_current_only'
                    self.state['phase_switch_source'] = 'openwb_config_mqtt'
                    self.state['connected_phases'] = connected_phases
                except Exception:
                    pass
                return

            # --- Boolean-Felder ---
            if topic == f"{self.native_prefix}/plug_state":
                val      = payload_str.lower() in ('true', '1')
                prev_plug = self.state['plug_state']
                plug_observed = bool(self.state.get('_plug_state_observed', False))
                self.state['plug_state'] = val
                self.state['car']        = 2 if val else 1
                if val and plug_observed and not prev_plug:
                    self.state['_session_start_wh'] = (
                        self.state['daily_imported_wh']
                        if self.state.get('_daily_imported_observed', False)
                        else None
                    )
                    self.state['_session_start_ts'] = int(time.time())
                    self.state['session_kwh']        = 0.0
                    logger.info(f"[WB{self.wb_id}] Auto eingesteckt! Session-Zaehler gestartet.")
                elif not val:
                    self.state['_session_start_wh'] = None
                    self.state['_session_start_ts'] = None
                    self._clear_total_range()
                    self._clear_charged_range()
                self.state['_plug_state_observed'] = True
                return

            if topic == f"{self.native_prefix}/charge_state":
                self.state['charge_state'] = payload_str.lower() in ('true', '1')
                self.state['charging']     = self.state['charge_state']
                if not self.state['charge_state']:
                    self.state['real_power_w'] = 0.0
                    self.state['phase_power_l1_w'] = 0.0
                    self.state['phase_power_l2_w'] = 0.0
                    self.state['phase_power_l3_w'] = 0.0
                    self.state['phase_power_sum_w'] = 0.0
                    self.state['phase_power_verified'] = False
                return

            if topic == f"{self.native_prefix}/powers":
                try:
                    powers = json.loads(payload_str)
                    if not isinstance(powers, list):
                        return
                    p1 = float(powers[0]) if len(powers) > 0 and powers[0] is not None else 0.0
                    p2 = float(powers[1]) if len(powers) > 1 and powers[1] is not None else 0.0
                    p3 = float(powers[2]) if len(powers) > 2 and powers[2] is not None else 0.0
                    total = p1 + p2 + p3
                    self.state['phase_power_l1_w'] = p1
                    self.state['phase_power_l2_w'] = p2
                    self.state['phase_power_l3_w'] = p3
                    self.state['phase_power_sum_w'] = total
                    self.state['_phase_power_ts'] = time.time()
                    self.state['phase_power_verified'] = bool(total > 50.0)
                    self.state['real_power_w'] = total if total > 50.0 else 0.0
                    if total > 50.0:
                        self.state['charging'] = True
                    elif not self.state.get('charge_state', False):
                        self.state['charging'] = False
                except Exception:
                    pass
                return

            # --- Lademodus (simpleAPI) ---
            if topic == f"{self.simpleapi_prefix}/chargemode":
                self.state['chargemode_str'] = payload_str.strip('"')
                self.state['frc'] = 0 if payload_str.lower() in ('stop', '0') else 2
                return

            # --- Numerische Felder ---
            try:
                num_val = float(payload_str)
            except (ValueError, TypeError):
                return

            if topic == f"{self.native_prefix}/power":
                if self.state.get('charge_state', False) or self.state.get('phase_power_verified', False):
                    self.state['real_power_w'] = num_val if num_val > 50.0 else 0.0
                else:
                    self.state['real_power_w'] = 0.0
            elif topic == f"{self.native_prefix}/evse_current":
                self.state['evse_current'] = num_val
                self.state['amp']          = int(num_val)
            elif topic == f"{self.native_prefix}/phases_in_use":
                self.state['phases_in_use'] = int(num_val)
                self.state['pha']           = 56 if int(num_val) >= 3 else 8
            elif topic == f"{self.native_prefix}/daily_imported":
                self.state['daily_imported_wh'] = num_val
                self.state['_daily_imported_observed'] = True
                start = self.state.get('_session_start_wh')
                if (
                    start is None
                    and self.state.get('plug_state', False)
                    and self.state.get('_session_start_ts') is not None
                ):
                    self.state['_session_start_wh'] = num_val
                    start = num_val
                if start is not None and self.state['plug_state']:
                    self.state['session_kwh'] = max(0.0, (num_val - start) / 1000.0)
            elif topic == f"{self.native_prefix}/imported":
                self.state['imported_total_wh'] = num_val

        except Exception:
            pass  # Stille Fehlerbehandlung in High-Volume MQTT Threads

    def _configured_role(self):
        if self.configured_primary_mode_enabled:
            return "primary"
        if self.configured_modbus_enabled:
            return "secondary_modbus"
        return "secondary_http"

    @staticmethod
    def _role_group(role):
        role = str(role or "").strip().lower()
        if role.startswith("primary"):
            return "primary"
        if role.startswith("secondary"):
            return "secondary"
        return role

    def _openwb_chargepoint_config_snapshot(self):
        now = time.time()
        if now < float(self._cp_config_cache_until or 0.0):
            return self._cp_config_cache
        self._cp_config_cache_until = now + 60.0
        self._cp_config_cache = None
        if self.cp_id == "":
            return None
        result = self._http_v1_read(f"openWB/chargepoint/{self.cp_id}/config")
        message = result.get("message") if isinstance(result, dict) else None
        if isinstance(message, dict):
            self._cp_config_cache = message
        return self._cp_config_cache

    def _detect_openwb_role(self, cp_data):
        """Infer the openWB role from read-only status/config data."""
        cp_data = cp_data if isinstance(cp_data, dict) else {}
        cp_config = self._openwb_chargepoint_config_snapshot()
        config = cp_config.get("configuration") if isinstance(cp_config, dict) and isinstance(cp_config.get("configuration"), dict) else {}
        cp_type = str((cp_config or {}).get("type") or "").strip().lower()
        cp_mode = str(config.get("mode") or config.get("control_mode") or "").strip().lower()
        connected_phases = self._float_value((cp_config or {}).get("connected_phases"), 0.0)
        if connected_phases > 0:
            self.state["connected_phases"] = int(connected_phases)
        all_values = " ".join(str(v).strip().lower() for v in list(config.values()) + list((cp_config or {}).values()) if not isinstance(v, (dict, list)))

        if "secondary" in cp_mode or "secondary" in cp_type or "secondary" in all_values:
            return "secondary", "openwb_v1_config", f"type={cp_type or '-'}, mode={cp_mode or '-'}"
        if cp_type in ("internal_openwb", "internal", "series", "openwb_series") or cp_mode in ("series", "primary", "local"):
            return "primary", "openwb_v1_config", f"type={cp_type or '-'}, mode={cp_mode or '-'}"

        chargemode = str(cp_data.get("chargemode") or "").strip().lower()
        if chargemode in OPENWB_AUTONOMOUS_CHARGEMODES:
            return "primary", "simpleapi_chargemode", f"chargemode={chargemode}"
        return "", "", ""

    def _apply_effective_role(self, role, detected_role="", source="", detail=""):
        configured = self._configured_role()
        role = str(role or configured).strip().lower()
        detected_role = str(detected_role or "").strip().lower()
        self.primary_mode_enabled = role.startswith("primary")
        self.modbus_enabled = role.startswith("secondary_modbus")
        if self.primary_mode_enabled:
            self.state["phase_switch_capability"] = "openwb_primary_mode_only"
            self.state["phase_switch_source"] = source or "openwb_role"
            self.state["api_surface"] = "openwb_primary_simpleapi_autodetected" if "auto" in role else "openwb_primary_simpleapi"
        elif self.modbus_enabled:
            self.state["phase_switch_capability"] = "secondary_modbus_current_only"
            self.state["phase_switch_source"] = source or "openwb_role"
            self.state["api_surface"] = "openwb_secondary_modbus"
        else:
            self.state["phase_switch_capability"] = "secondary_current_only"
            self.state["phase_switch_source"] = source or "disabled_by_design"
            self.state["api_surface"] = "openwb_secondary_set_current_heartbeat"

        self.state["configured_role"] = configured
        self.state["detected_role"] = detected_role
        self.state["effective_role"] = role
        self.state["role_detection_source"] = source or ""
        self.state["role_detection_detail"] = detail or ""
        self.state["role_mismatch"] = bool(detected_role and self._role_group(detected_role) != self._role_group(configured))

    def _apply_role_detection(self, cp_data):
        detected, source, detail = self._detect_openwb_role(cp_data)
        configured = self._configured_role()
        effective = configured
        # The historical default was "Secondary over HTTP". If openWB itself
        # clearly reports an internal/series chargepoint, follow that detected
        # Primary/autonomous role. Explicit Primary or Modbus settings remain
        # explicit operator intent.
        if self.auto_role_enabled and configured == "secondary_http" and detected == "primary":
            effective = "primary_autodetected"
        self._apply_effective_role(effective, detected_role=detected, source=source, detail=detail)

    def _http_post(self, post_data: str) -> bool:
        """Sendet Steuerbefehl per HTTP POST an openWB simpleapi.php."""
        import urllib.request
        import urllib.error
        if not self._physical_output_write_allowed("simpleapi"):
            return False
        if not command_gate.allow_command(
            self,
            action="openwb_http_post",
            payload={"post_data": post_data},
            audit_allowed=False,
        ):
            return False
        try:
            req = urllib.request.Request(
                self.http_api_url,
                data=post_data.encode('ascii'),
                headers=self._http_headers({'Content-Type': 'application/x-www-form-urlencoded'}),
                method='POST',
            )
            if not command_gate.allow_command(
                self,
                action="openwb_http_post_wire",
                payload={"post_data": post_data},
                audit_allowed=False,
            ):
                return False
            with urllib.request.urlopen(req, timeout=5) as ctx:
                resp = ctx.read().decode('utf-8', errors='replace')
            if '"success":true' in resp:
                logger.debug(f"[WB{self.wb_id}] HTTP OK: {post_data} -> {resp[:80]}")
                return True
            logger.warning(f"[WB{self.wb_id}] HTTP Fehler: {post_data} -> {resp[:120]}")
            return False
        except Exception as e:
            logger.error(f"[WB{self.wb_id}] HTTP POST fehlgeschlagen: {e}")
            return False

    def _http_get_json(self, query: str):
        """Liest JSON aus der openWB HTTP simpleAPI."""
        import urllib.parse
        import urllib.request
        sep = "&" if "?" in self.http_api_url else "?"
        url = self.http_api_url + sep + query
        try:
            req = urllib.request.Request(
                url,
                headers=self._http_headers(),
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as ctx:
                raw = ctx.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except Exception as e:
            logger.error(f"[WB{self.wb_id}] HTTP GET fehlgeschlagen ({query}): {e}")
            return None

    def _openwb_chargepoint_nr(self):
        """Ladepunktnummer für die openWB simpleAPI."""
        return str(self.cp_id) if self.cp_id != "" else "auto"

    def _primary_set_chargemode(self, mode: str) -> bool:
        """Schaltet openWB Software im Primary-Betrieb auf PV/Sofort/Stop."""
        import urllib.parse

        mode = str(mode or "").strip().lower()
        if mode not in ("instant", "pv", "stop"):
            logger.warning(f"[WB{self.wb_id}] openWB Primary: ungueltiger Modus {mode!r}")
            return False
        if mode in ("pv", "stop"):
            # Eine neue Nutzer-/Policyentscheidung widerruft einen eventuell
            # vorbereiteten Sofortladestart bereits vor dem Modusschreiben.
            # Ein späterer Zyklus darf den alten Start sonst nicht nachholen.
            self._clear_primary_start_transition()
        post_data = urllib.parse.urlencode({
            "set_chargemode": mode,
            "chargepoint_nr": self._openwb_chargepoint_nr(),
        })
        ok = self._http_post(post_data)
        if ok:
            self.state["chargemode_str"] = mode
            self.state["api_surface"] = "openwb_primary_simpleapi"
            self.state["frc"] = 0 if mode == "stop" else 2
            if mode == "stop":
                self.state["amp"] = 0
                self.state["charging"] = False
            if mode == "pv":
                self._set_control_state(
                    "self_regulated",
                    "openWB regelt selbst",
                    "openWB Primary-PV-Modus wurde gesetzt; E3DC-Control beobachtet die Leistung und führt den Speicher.",
                    "info",
                    ok=True,
                )
            elif mode == "stop":
                self._set_control_state(
                    "stop_accepted",
                    "Stop übernommen",
                    "openWB Primary hat den Stop-Modus angenommen.",
                    "secondary",
                    amp=0,
                    ok=True,
                )
            else:
                self._set_control_state(
                    "primary_mode_accepted",
                    "Primary-Modus übernommen",
                    "openWB Primary hat den Sofortlade-Modus angenommen; openWB führt Ladepunktdetails weiter selbst. "
                    + OPENWB_PRIMARY_DIRECT_LIMIT_NOTICE,
                    "success",
                    ok=True,
                )
            logger.info(f"[WB{self.wb_id}] openWB Primary Modus: {mode}")
        else:
            self._set_control_state(
                "primary_mode_failed",
                "Primary-Modus nicht angenommen",
                f"openWB Primary hat den Modus {mode} nicht bestaetigt.",
                "warning",
                ok=False,
            )
        return ok

    def _clear_primary_start_transition(self):
        self._primary_pending_current_amp = None
        self._primary_pending_current_sent_ts = 0.0
        self.state["primary_start_stage"] = ""
        self.state["primary_start_target_amp"] = None
        self.state["primary_start_current_sent_ts"] = 0.0

    def _primary_current_readback_confirmed(self, target_amp) -> bool:
        """Bestätigt den vorbereiteten Primary-Strom nur aus frischem Status."""

        try:
            target = float(target_amp)
            reported = float(self.state.get("instant_charging_current"))
            readback_ts = float(self.state.get("driver_status_last_ok_ts") or 0.0)
        except (TypeError, ValueError):
            return False
        sent_ts = float(self._primary_pending_current_sent_ts or 0.0)
        if (
            sent_ts <= 0.0
            or readback_ts <= sent_ts
            or self.state.get("driver_status_valid") is not True
            or bool(self.state.get("driver_status_stale", False))
            or bool(self.state.get("driver_status_degraded", False))
        ):
            return False
        tolerance = max(0.05, float(self.current_step_amp or 1.0) / 2.0)
        return math.isfinite(reported) and abs(reported - target) <= tolerance

    def _primary_set_chargecurrent(self, target_amp) -> bool:
        """Setzt den Sofortlade-Strom für openWB Primary."""
        import urllib.parse

        amp = _quantize_current_amp(
            target_amp,
            step=self.current_step_amp,
            max_amp=getattr(self, "max_amp", 16.0),
        )
        amp_value = _amp_label(amp)
        post_data = urllib.parse.urlencode({
            "chargecurrent": amp_value,
            "chargepoint_nr": self._openwb_chargepoint_nr(),
        })
        ok = self._http_post(post_data)
        if ok:
            self.state["amp"] = int(round(amp))
            self.state["evse_current"] = amp
            self.state["last_command_amp"] = amp
            self.state["current_step_amp"] = self.current_step_amp
            self.state["api_surface"] = "openwb_primary_simpleapi"
            self._set_control_state(
                "primary_current_accepted",
                "Primary-Strom übernommen",
                f"openWB Primary hat {amp_value} A als Sofortladestrom angenommen. "
                + OPENWB_PRIMARY_DIRECT_LIMIT_NOTICE,
                "success",
                amp=amp_value,
                ok=True,
            )
            logger.debug(f"[WB{self.wb_id}] openWB Primary Sofortladestrom: {amp_value}A")
        else:
            self._set_control_state(
                "primary_current_failed",
                "Primary-Strom nicht angenommen",
                f"openWB Primary hat {amp_value} A nicht bestaetigt.",
                "warning",
                amp=amp_value,
                ok=False,
            )
        return ok

    def _primary_set_instant_current(self, target_amp) -> bool:
        """Bereitet Strom vor und aktiviert Sofortladen erst nach Readback."""
        raw = float(target_amp or 0)
        if raw < 0.5:
            self._clear_primary_start_transition()
            return self._primary_set_chargemode("stop")
        amp = _quantize_current_amp(
            max(6.0, min(float(getattr(self, "max_amp", 16.0)), raw)),
            step=self.current_step_amp,
            max_amp=getattr(self, "max_amp", 16.0),
        )
        pending_amp = self._primary_pending_current_amp
        if pending_amp is not None and abs(float(pending_amp) - amp) <= 1e-6:
            if self._primary_current_readback_confirmed(amp):
                mode_ok = self._primary_set_chargemode("instant")
                if mode_ok:
                    self._clear_primary_start_transition()
                    self.state["chargemode_str"] = "instant"
                return bool(mode_ok)

            age_s = max(
                0.0,
                time.time() - float(self._primary_pending_current_sent_ts or 0.0),
            )
            if age_s < self._primary_pending_current_timeout_s:
                self.state["primary_start_stage"] = "await_current_readback"
                self._set_control_state(
                    "primary_current_readback_pending",
                    "Warte auf Strombestätigung",
                    f"openWB Primary muss {amp:g} A erst frisch zurückmelden; "
                    "Sofortladen bleibt bis dahin aus.",
                    "info",
                    amp=amp,
                    ok=None,
                    count_failure=False,
                )
                return False

        # Neues Ziel oder abgelaufener Readback: nur den Stromwert schreiben.
        # Der Moduswechsel ist eine getrennte Transaktion in einem späteren
        # Managerzyklus und darf nie auf einem alten openWB-Stromwert starten.
        self._clear_primary_start_transition()
        current_ok = self._primary_set_chargecurrent(amp)
        if current_ok:
            sent_ts = time.time()
            self._primary_pending_current_amp = amp
            self._primary_pending_current_sent_ts = sent_ts
            self.state["primary_start_stage"] = "await_current_readback"
            self.state["primary_start_target_amp"] = amp
            self.state["primary_start_current_sent_ts"] = sent_ts
            self._set_control_state(
                "primary_current_readback_pending",
                "Warte auf Strombestätigung",
                f"openWB Primary hat {amp:g} A angenommen; Sofortladen folgt "
                "erst nach einem frischen passenden Readback.",
                "info",
                amp=amp,
                ok=True,
                count_failure=False,
            )
        else:
            self._clear_primary_start_transition()
        return bool(current_ok)

    def _http_v1_post(self, topic: str, message=None):
        """Schreibt/liest openWB HTTP-API V1 Topics (Port 8443)."""
        import ssl
        import urllib.request
        if not self._physical_output_write_allowed("http_v1"):
            return None
        if not command_gate.allow_command(
            self,
            action="openwb_http_v1_post",
            payload={"topic": topic, "message": message},
            audit_allowed=False,
        ):
            return None
        payload = {"topic": topic}
        if message is not None:
            payload["message"] = json.dumps(message)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.http_v1_url,
            data=data,
            headers=self._http_headers({"Content-Type": "application/json"}),
            method="POST",
        )
        try:
            ctx = ssl._create_unverified_context()
            if not command_gate.allow_command(
                self,
                action="openwb_http_v1_post_wire",
                payload={"topic": topic, "message": message},
                audit_allowed=False,
            ):
                return None
            with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except Exception as e:
            logger.error(f"[WB{self.wb_id}] HTTP V1 fehlgeschlagen ({topic}): {e}")
            return None

    def _http_v1_read(self, topic: str):
        """Liest openWB HTTP-API V1 Topics ohne Command-Gate."""
        import ssl
        import urllib.request
        payload = {"topic": topic}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.http_v1_url,
            data=data,
            headers=self._http_headers({"Content-Type": "application/json"}),
            method="POST",
        )
        try:
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=3, context=ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except Exception as e:
            logger.debug(f"[WB{self.wb_id}] HTTP V1 Read fehlgeschlagen ({topic}): {e}")
            return None

    def _openwb_backend_pro_ip(self):
        """Erkennt eine hinter openWB Software haengende openWB Pro."""
        now = time.time()
        cached_until = float(self.state.get("_backend_pro_cache_until", 0.0) or 0.0)
        if now < cached_until:
            return self.state.get("_backend_pro_ip") or ""
        self.state["_backend_pro_cache_until"] = now + 60.0
        self.state["_backend_pro_ip"] = ""
        if self.cp_id == "":
            return ""
        message = self._openwb_chargepoint_config_snapshot()
        if not isinstance(message, dict):
            return ""
        cp_type = str(message.get("type") or "").strip().lower()
        config = message.get("configuration") if isinstance(message.get("configuration"), dict) else {}
        ip = str(config.get("ip_address") or "").strip()
        if cp_type == "openwb_pro" and ip and ip != "0.0.0.0":
            self.state["_backend_pro_ip"] = ip
            return ip
        return ""

    def _openwb_pro_backend_snapshot(self):
        """Direktwert einer hinter openWB Software angebundenen Pro.

        Einige openWB-Software-2.x-Staende behalten nach externer Unterbrechung
        stale Ladepunktwerte. Wenn die Software den Pro-Backend-IP kennt, ist
        connect.php die robustere Wahrheit für Leistung, Stecker und Ladung.
        """
        import urllib.request
        ip = self._openwb_backend_pro_ip()
        if not ip:
            return None
        try:
            with urllib.request.urlopen(f"http://{ip}/connect.php", timeout=3) as ctx:
                raw = ctx.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            if isinstance(data, dict):
                data["_backend_ip"] = ip
                return data
        except Exception as e:
            logger.debug(f"[WB{self.wb_id}] openWB-Pro Backend nicht lesbar ({ip}): {e}")
        return None

    def _secondary_parent(self):
        """IP-Adresse des steuernden Systems für openWB-Secondary-Heartbeat."""
        if self.secondary_parent_ip:
            return self.secondary_parent_ip
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect((self.ip, 9))
                return sock.getsockname()[0]
            finally:
                sock.close()
        except Exception:
            return "0.0.0.0"

    def _remember_command_result(self, ok):
        if ok is None:
            return
        if bool(ok):
            self._command_failure_count = 0
            self._command_blocked_until = 0.0
            self.state["command_blocked"] = False
            self.state["command_blocked_until"] = 0
            return
        self._command_failure_count += 1
        if self._command_failure_count >= self.command_failure_limit:
            self._command_blocked_until = time.time() + self.command_block_s
            self.state["command_blocked"] = True
            self.state["command_blocked_until"] = int(self._command_blocked_until)

    def _commands_temporarily_blocked(self):
        now = time.time()
        if self._command_blocked_until and now < self._command_blocked_until:
            remaining = int(max(1, self._command_blocked_until - now))
            self._set_control_state(
                "command_blocked",
                "openWB-Befehle pausiert",
                f"openWB hat {self._command_failure_count} Befehle nicht bestaetigt. E3DC-Control pausiert weitere openWB-Schreibbefehle noch {remaining}s und zeigt diesen Zustand im Frontend.",
                "danger",
                ok=None,
                count_failure=False,
            )
            return True
        if self._command_blocked_until and now >= self._command_blocked_until:
            self._command_blocked_until = 0.0
            self._command_failure_count = 0
            self.state["command_blocked"] = False
            self.state["command_blocked_until"] = 0
        return False

    def _set_control_state(self, status, label, detail="", level="info", amp=None, ok=None, count_failure=True):
        """Merkt den echten Steuerzustand für Diagnose und Frontend."""
        if count_failure:
            self._remember_command_result(ok)
            if ok is False and self._command_failure_count >= self.command_failure_limit:
                status = "command_rejected_limit"
                label = "openWB-Befehle nicht angenommen"
                detail = (
                    f"openWB hat {self._command_failure_count} Steuerbefehle in Folge nicht bestaetigt. "
                    "Bitte openWB-Rolle/API pruefen; E3DC-Control zeigt den Fehler im Frontend und pausiert kurz weitere Schreibbefehle."
                )
                level = "danger"
        self.state["control_status"] = str(status or "")
        self.state["control_label"] = str(label or "")
        self.state["control_detail"] = str(detail or "")
        self.state["control_level"] = str(level or "info")
        self.state["control_ts"] = int(time.time())
        if amp is not None:
            self.state["last_command_amp"] = amp
            self.state["last_command_ts"] = self.state["control_ts"]
        if ok is not None:
            self.state["last_command_ok"] = bool(ok)
        self.state["command_failure_count"] = int(self._command_failure_count)
        self.state["command_failure_limit"] = int(self.command_failure_limit)
        self.state["command_blocked"] = bool(self._command_blocked_until and time.time() < self._command_blocked_until)
        self.state["command_blocked_until"] = int(self._command_blocked_until or 0)
        if ok is False or self.state["command_blocked"]:
            try:
                self.write_openwb_status()
            except Exception:
                pass

    def _secondary_heartbeat(self):
        """Haelt openWB im Secondary-Pfad wach, ohne Lademodus umzuschalten."""
        if self.modbus_enabled:
            # Der konfigurierte Transport ist eine Owner-Entscheidung. Nach
            # einem unklaren HTTP-Ergebnis darf derselbe Heartbeat nicht über
            # einen zweiten Transport erneut ausgelöst werden.
            return self._modbus_secondary_heartbeat()
        now_ts = time.time()
        payload = {
            "heartbeat": int(now_ts),
            "parent_ip": self._secondary_parent(),
        }
        result = self._http_v1_post("openWB/set/internal_chargepoint/global_data", payload)
        if result and result.get("status") == "success":
            self.state["last_heartbeat_ok"] = True
            self.state["last_heartbeat_ts"] = now_ts
            return True
        self.state["last_heartbeat_ok"] = False
        return False

    def _modbus_register(self, lp_register: int) -> int:
        """Mappt LP1-Register 101xx auf den konfigurierten openWB-Ladepunkt."""
        return int(lp_register) + max(0, self.modbus_connector - 1) * 100 + int(self.modbus_offset)

    def _modbus_request(self, function_code: int, address: int, value_or_count: int):
        import socket
        import struct

        transaction_id = int(time.time() * 1000) & 0xFFFF
        pdu = struct.pack(">BHH", int(function_code), int(address), int(value_or_count))
        mbap = struct.pack(">HHHB", transaction_id, 0, len(pdu) + 1, int(self.modbus_unit))
        is_write = int(function_code) == 6
        write_payload = {"function_code": int(function_code), "address": int(address), "value": int(value_or_count)}
        if is_write and not self._physical_output_write_allowed("modbus"):
            return b""
        if is_write and not command_gate.allow_command(
            self,
            action="openwb_modbus_write_connect",
            payload=write_payload,
            audit_allowed=False,
        ):
            return b""
        with socket.create_connection((self.ip, int(self.modbus_port)), timeout=2.0) as sock:
            sock.settimeout(2.0)
            if is_write and not command_gate.allow_command(
                self,
                action="openwb_modbus_write_wire",
                payload=write_payload,
                audit_allowed=False,
            ):
                return b""
            sock.sendall(mbap + pdu)
            header = sock.recv(7)
            if len(header) < 7:
                raise RuntimeError("kurze Modbus-Antwort")
            _, _, length, _ = struct.unpack(">HHHB", header)
            body = b""
            while len(body) < max(0, length - 1):
                chunk = sock.recv(length - 1 - len(body))
                if not chunk:
                    break
                body += chunk
        if not body:
            raise RuntimeError("leere Modbus-Antwort")
        if body[0] & 0x80:
            code = body[1] if len(body) > 1 else 0
            raise RuntimeError(f"Modbus Exception {code}")
        return body

    def _modbus_read_input(self, lp_register: int, count: int = 1):
        body = self._modbus_request(4, self._modbus_register(lp_register), count)
        import struct

        byte_count = body[1] if len(body) > 1 else 0
        if byte_count < count * 2:
            raise RuntimeError("kurze Modbus-Registerantwort")
        return list(struct.unpack(">" + "H" * count, body[2:2 + count * 2]))

    def _modbus_write_register(self, lp_register: int, value: int) -> bool:
        body = self._modbus_request(6, self._modbus_register(lp_register), int(value))
        return bool(body and body[0] == 6)

    def _modbus_secondary_heartbeat(self) -> bool:
        try:
            hb_ok = self._modbus_write_register(10190, 1)
            # Laut openWB Modbus Rev2.0 setzt jeder Modbus-Lesezugriff den
            # Heartbeat-Zaehler zurück, solange Heartbeat aktiv ist.
            self._modbus_read_input(10115, 1)
            self.state["api_surface"] = "openwb_secondary_modbus"
            self.state["last_heartbeat_ok"] = bool(hb_ok)
            if hb_ok:
                self.state["last_heartbeat_ts"] = time.time()
            return hb_ok
        except Exception as e:
            logger.warning(f"[WB{self.wb_id}] openWB Modbus-Heartbeat fehlgeschlagen: {e}")
            self.state["last_heartbeat_ok"] = False
            return False

    def _secondary_set_current(self, target_amp):
        """Setzt den openWB-Secondary-Sollstrom.

        0A ist ein echter Freigabeentzug. Werte dazwischen werden auf 6..32A
        begrenzt. Die openWB entscheidet selbst über PV-Logik, Schaltpause und
        Phasen; E3DC-Control gibt nur das verfuegbare Budget weiter.
        """
        amp = _quantize_current_amp(
            target_amp,
            step=self.current_step_amp,
            max_amp=getattr(self, "max_amp", 16.0),
        )
        amp_payload = _amp_api_value(amp)
        amp_text = _amp_label(amp)
        self.state["last_command_amp"] = amp
        self.state["last_command_ts"] = int(time.time())
        self.state["current_step_amp"] = self.current_step_amp
        self.state["fractional_current_supported"] = self.current_step_amp < 1.0
        result = None
        transport = "modbus" if self.modbus_enabled else "http_v1"
        if self.modbus_enabled:
            try:
                ok = self._modbus_write_register(10171, int(round(amp * 100)))
            except Exception as e:
                logger.warning(
                    f"[WB{self.wb_id}] openWB Modbus-Strom {amp_text}A fehlgeschlagen: {e}"
                )
                ok = False
        else:
            result = self._http_v1_post(
                f"openWB/set/internal_chargepoint/{self.api_duo_num}/data/set_current",
                amp_payload,
            )
            ok = bool(result and result.get("status") == "success")
        if ok:
            self.state["amp"] = int(round(amp))
            self.state["evse_current"] = amp
            self.state["chargemode_str"] = "secondary_current"
            self.state["api_surface"] = (
                "openwb_secondary_modbus"
                if self.modbus_enabled
                else "openwb_secondary_set_current_heartbeat"
            )
            if amp <= 0:
                self.state["charging"] = False
            self._set_control_state(
                "set_current_accepted",
                "Sollstrom übernommen",
                f"openWB Secondary hat {amp_text} A per "
                f"{'Modbus' if self.modbus_enabled else 'HTTP V1'} angenommen; "
                "der Heartbeat bleibt eine getrennte Managertransaktion.",
                "success",
                amp=amp,
                ok=True,
            )
            logger.debug(
                f"[WB{self.wb_id}] openWB Secondary {transport}: {amp_text}A"
            )
            return True
        self._set_control_state(
            "set_current_failed",
            "Sollstrom nicht angenommen",
            f"openWB Secondary hat {amp_text} A über {transport} nicht bestätigt. "
            "Kein zweiter Transport wird nach einem mehrdeutigen Ergebnis versucht; "
            "E3DC-Control nutzt die gemessene Wallboxleistung nur als Last.",
            "warning",
            amp=amp,
            ok=False,
        )
        logger.warning(
            f"[WB{self.wb_id}] openWB Secondary set_current={amp_text}A fehlgeschlagen: "
            f"transport={transport}, result={result}"
        )
        return False

    @staticmethod
    def _bool_value(value, default=False):
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().strip('"').lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _float_value(value, default=0.0):
        if value in (None, "null"):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _soc_percent_value(value):
        """Normalisiert ausschließlich echte Prozentwerte; ``0`` ist gültig."""
        if isinstance(value, bool) or value in (None, ""):
            return None
        try:
            soc = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(soc) or soc < 0.0 or soc > 100.0:
            return None
        return soc

    @staticmethod
    def _soc_source_timestamp(value, now_ts=None):
        """Liest vorhandene openWB-Zeitstempel in Sekunden, ohne sie zu erfinden."""
        if isinstance(value, bool) or value in (None, "", "null"):
            return None
        try:
            source_ts = float(value)
            now_value = time.time() if now_ts is None else float(now_ts)
        except (TypeError, ValueError):
            return None
        if source_ts > 100000000000.0:
            source_ts /= 1000.0
        if (
            not math.isfinite(source_ts)
            or not math.isfinite(now_value)
            or source_ts <= 0.0
            or now_value <= 0.0
            or source_ts > now_value + 300.0
        ):
            return None
        return int(source_ts)

    @staticmethod
    def _soc_source_rule_confirmed(source):
        return vehicle_soc_source_trusted(source)

    def _record_soc_observation(
        self,
        value,
        *,
        source,
        observed_ts=None,
        source_ts=None,
        retained=False,
        vehicle_key=None,
    ):
        """Merkt den besten sichtbaren SoC getrennt vom Regelvertrag."""
        car_soc = self._soc_percent_value(value)
        if car_soc is None:
            return False
        now_ts = time.time() if observed_ts is None else float(observed_ts)
        candidate_source_ts = self._soc_source_timestamp(source_ts, now_ts=now_ts)
        previous_source_ts = self._soc_source_timestamp(
            self.state.get("car_soc_observed_source_ts"),
            now_ts=now_ts,
        )
        previous_soc = self._soc_percent_value(self.state.get("car_soc_observed"))
        previous_retained = bool(self.state.get("car_soc_observed_retained", False))

        if previous_source_ts is not None:
            # Derselbe SoC darf seinen echten MQTT-Zeitanker nicht durch einen
            # zeitlosen HTTP-Poll verlieren. Ein abweichender HTTP-Wert bleibt
            # dagegen als neue, ausdrücklich zeitlose Beobachtung sichtbar.
            if candidate_source_ts is None and previous_soc == car_soc:
                if vehicle_key:
                    self.state["car_soc_observed_vehicle_id"] = str(vehicle_key).strip()
                return False
            if candidate_source_ts is not None and candidate_source_ts < previous_source_ts:
                return False
            if (
                candidate_source_ts == previous_source_ts
                and retained
                and not previous_retained
            ):
                return False

        self.state.update({
            "car_soc_observed": car_soc,
            "car_soc_observed_source": str(source or "").strip(),
            "car_soc_observed_source_ts": candidate_source_ts,
            "car_soc_observed_received_ts": int(now_ts),
            "car_soc_observed_retained": bool(retained),
            "car_soc_display_usable": True,
            "car_soc_observed_session_start_ts": (
                self.state.get("_session_start_ts")
                if not retained else None
            ),
            "car_soc_observed_vehicle_id": str(
                self._current_range_vehicle_key()
                if vehicle_key is None else vehicle_key
            ).strip(),
        })
        return True

    def _apply_simpleapi_soc_pair(self, *, observed_ts=None):
        """Paart skalaren simpleAPI-SoC und dessen Produzentenzeitpunkt."""
        now_ts = time.time() if observed_ts is None else float(observed_ts)
        pending = self.state.get("_simpleapi_soc_pending", {})
        car_soc = self._soc_percent_value(pending.get("value"))
        source_ts = self._soc_source_timestamp(pending.get("source_ts"), now_ts=now_ts)
        value_received_ts = self._float_value(pending.get("value_received_ts"), 0.0)
        timestamp_received_ts = self._float_value(pending.get("timestamp_received_ts"), 0.0)
        if (
            car_soc is None
            or source_ts is None
            or value_received_ts <= 0.0
            or timestamp_received_ts <= 0.0
            or abs(value_received_ts - timestamp_received_ts) > 10.0
        ):
            return False

        self._record_soc_observation(
            car_soc,
            source="openwb_mqtt",
            observed_ts=now_ts,
            source_ts=source_ts,
            retained=True,
        )
        # Das Paar ist verbraucht. So kann ein unmittelbar folgender neuer
        # SoC nicht versehentlich mit dem Timestamp des vorigen Werts gepaart
        # werden, selbst wenn openWB beide Änderungen sehr schnell sendet.
        pending.pop("value_received_ts", None)
        pending.pop("timestamp_received_ts", None)

        # Die openWB simpleAPI publiziert diese Projektion retained und nur
        # bei Wertänderung. Selbst ein synthetisch non-retained zugestelltes
        # Scalar-Paar bleibt daher ausschließlich Anzeige-Beobachtung.
        return True

    @staticmethod
    def _text_value(value, default=""):
        if value in (None, "null"):
            return default
        return str(value).strip().strip('"')

    @staticmethod
    def _simpleapi_status_payload_valid(payload):
        if not isinstance(payload, dict):
            return False
        status_keys = {
            "power", "charging_power", "plug_state", "charge_state",
            "evse_current", "charging_current", "powers", "currents",
        }
        if not status_keys.intersection(payload):
            return False
        bool_values = {"0", "1", "false", "true", "no", "yes", "off", "on"}
        for key in ("plug_state", "charge_state"):
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
                continue
            if isinstance(value, str) and value.strip().strip('"').lower() in bool_values:
                continue
            return False
        for key in ("power", "charging_power", "evse_current", "charging_current"):
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, bool):
                return False
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(parsed):
                return False
        for key in ("powers", "currents"):
            if key not in payload:
                continue
            values = payload[key]
            if not isinstance(values, list):
                return False
            try:
                if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in values):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _write_manual_soc(
        self,
        car_soc,
        plugged,
        soc_ts=None,
        source="openwb_http",
        soc_source_ts=None,
        raw_soc_ts=None,
        rule_confirmed=False,
    ):
        car_soc = self._soc_percent_value(car_soc)
        if car_soc is None:
            return False
        now_ts = time.time()
        source_ts = self._soc_source_timestamp(
            soc_source_ts if soc_source_ts is not None else soc_ts,
            now_ts=now_ts,
        )
        raw_ts = self._soc_source_timestamp(
            raw_soc_ts if raw_soc_ts is not None else source_ts,
            now_ts=now_ts,
        )
        source = str(source or "").strip()
        confirmed = bool(
            rule_confirmed is True
            and source_ts is not None
            and raw_ts is not None
            and self._soc_source_rule_confirmed(source)
        )
        file_ts = int(source_ts or now_ts)
        soc_age_h = max(0.0, (now_ts - float(source_ts or now_ts)) / 3600.0)
        soc_file = os.path.join(RAMDISK_DIR, f"manual_soc_wb{self.wb_id}.json")
        payload_out = {
            "soc": car_soc,
            "ts": file_ts,
            "source": source,
            "soc_source_ts": source_ts,
            "raw_soc_ts": raw_ts,
            "soc_rule_confirmed": confirmed,
            "plugged": bool(plugged),
            "age_h": round(soc_age_h, 1),
            "wb": self.wb_id,
            "profile_id": self.state.get("profile_id"),
            "car_id": self.state.get("car_id"),
            "vehicle_id": self.state.get("vehicle_id"),
            "rfid_tag": self.state.get("rfid_tag"),
        }
        tmp = f"{soc_file}.tmp.{os.getpid()}.{time.monotonic_ns()}"
        try:
            os.makedirs(os.path.dirname(soc_file), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as sf:
                json.dump(payload_out, sf, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, soc_file)
            try:
                os.chmod(soc_file, 0o664)
            except OSError:
                pass
            return True
        except Exception as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            logger.debug(f"[WB{self.wb_id}] Auto-SoC schreiben fehlgeschlagen: {e}")
            return False

    def _update_from_simpleapi(self, payload):
        """Normalisiert get_chargepoint_all=<ID> auf das interne Statusformat."""
        if not isinstance(payload, dict):
            return False

        cp_data = payload
        detected_cp_id = None
        if not self._simpleapi_status_payload_valid(payload):
            cp_key = None
            if self.cp_id != "":
                cp_key = f"chargepoint_{self.cp_id}"
            if cp_key and isinstance(payload.get(cp_key), dict):
                cp_data = payload[cp_key]
            else:
                chargepoints = []
                for key, value in payload.items():
                    match = re.match(r"^chargepoint_(\d+)$", str(key))
                    if match and isinstance(value, dict):
                        chargepoints.append((int(match.group(1)), value))
                chargepoints.sort(key=lambda item: item[0])
                if chargepoints:
                    index = max(0, min(len(chargepoints) - 1, int(self.wb_id or 1) - 1))
                    detected_cp_id, cp_data = chargepoints[index]
        if not self._simpleapi_status_payload_valid(cp_data):
            return False
        if self.cp_id == "" and detected_cp_id is not None:
            self._set_runtime_chargepoint_id(detected_cp_id)
        if (
            self._auto_discovery_bound
            and self.cp_id != ""
            and int(self.cp_id) == int(
                (self.state.get("chargepoint_discovery_contract") or {}).get(
                    "cp_id", 0
                )
                or 0
            )
        ):
            confirmed_ts = int(time.time())
            self._auto_discovery_status_confirmed = True
            discovery = dict(
                self.state.get("chargepoint_discovery_contract") or {}
            )
            discovery["status_confirmed"] = True
            discovery["status_confirmed_ts"] = confirmed_ts
            self.state["chargepoint_discovery_contract"] = discovery
        self._apply_role_detection(cp_data)
        self.physical_output_identity_contract()

        plug_state = self._bool_value(cp_data.get("plug_state"), self.state.get("plug_state", False))
        charge_state = self._bool_value(cp_data.get("charge_state"), self.state.get("charge_state", False))
        locked = self._bool_value(
            cp_data.get("locked", cp_data.get("lock_state", cp_data.get("plug_locked"))),
            plug_state,
        )
        live_vehicle_id = cp_data.get("vehicle_id", cp_data.get("connected_vehicle_id"))
        live_rfid_tag = cp_data.get("rfid_tag", cp_data.get("rfid"))
        raw_power_w = self._float_value(cp_data.get("power", cp_data.get("charging_power")), 0.0)
        powers = cp_data.get("powers") if isinstance(cp_data.get("powers"), list) else []
        if powers:
            p1 = self._float_value(powers[0], 0.0) if len(powers) > 0 else 0.0
            p2 = self._float_value(powers[1], 0.0) if len(powers) > 1 else 0.0
            p3 = self._float_value(powers[2], 0.0) if len(powers) > 2 else 0.0
            self.state["_phase_power_ts"] = time.time()
        elif time.time() - float(self.state.get("_phase_power_ts", 0.0) or 0.0) < 15.0:
            p1 = self.state.get("phase_power_l1_w", 0.0)
            p2 = self.state.get("phase_power_l2_w", 0.0)
            p3 = self.state.get("phase_power_l3_w", 0.0)
        else:
            p1 = p2 = p3 = 0.0
        phase_power_sum = p1 + p2 + p3
        phase_power_verified = phase_power_sum > 50.0
        if phase_power_verified:
            power_w = phase_power_sum
        elif charge_state and raw_power_w > 50.0:
            power_w = raw_power_w
        else:
            power_w = 0.0
        evse_current = self._float_value(cp_data.get("evse_current", cp_data.get("charging_current")), 0.0)
        currents = cp_data.get("currents") if isinstance(cp_data.get("currents"), list) else []
        voltages = cp_data.get("voltages") if isinstance(cp_data.get("voltages"), list) else []
        phase_apparent = []
        for idx in range(3):
            if idx < len(currents) and idx < len(voltages):
                phase_apparent.append(abs(self._float_value(currents[idx], 0.0) * self._float_value(voltages[idx], 0.0)))
            else:
                phase_apparent.append(0.0)
        apparent_power_va = sum(phase_apparent)
        power_factor = (phase_power_sum / apparent_power_va) if apparent_power_va > 50.0 and phase_power_sum > 0 else 0.0
        if evse_current <= 0 and currents:
            evse_current = max(self._float_value(v, 0.0) for v in currents)

        phases = int(self._float_value(cp_data.get("phases_in_use"), 0.0))
        if phases <= 0 and currents:
            phases = sum(1 for v in currents if self._float_value(v, 0.0) > 0.2)
        if phases <= 0:
            phases = self.state.get("phases_in_use", 0)

        pro_backend = self._openwb_pro_backend_snapshot()
        if pro_backend is not None:
            pro_powers = pro_backend.get("powers") if isinstance(pro_backend.get("powers"), list) else []
            p1 = self._float_value(pro_powers[0], 0.0) if len(pro_powers) > 0 else 0.0
            p2 = self._float_value(pro_powers[1], 0.0) if len(pro_powers) > 1 else 0.0
            p3 = self._float_value(pro_powers[2], 0.0) if len(pro_powers) > 2 else 0.0
            phase_power_sum = p1 + p2 + p3
            raw_power_w = self._float_value(pro_backend.get("power_all"), phase_power_sum)
            power_w = phase_power_sum if abs(phase_power_sum) > 50.0 else (raw_power_w if abs(raw_power_w) > 50.0 else 0.0)
            phase_power_verified = abs(power_w) > 50.0
            plug_state = self._bool_value(pro_backend.get("plug_state"), False)
            charge_state = self._bool_value(pro_backend.get("charge_state"), False)
            locked = plug_state
            evse_current = self._float_value(pro_backend.get("offered_current"), 0.0)
            currents = pro_backend.get("currents") if isinstance(pro_backend.get("currents"), list) else []
            voltages = pro_backend.get("voltages") if isinstance(pro_backend.get("voltages"), list) else []
            phase_apparent = []
            for idx in range(3):
                if idx < len(currents) and idx < len(voltages):
                    phase_apparent.append(abs(self._float_value(currents[idx], 0.0) * self._float_value(voltages[idx], 0.0)))
                else:
                    phase_apparent.append(0.0)
            apparent_power_va = sum(phase_apparent)
            power_factor = (abs(power_w) / apparent_power_va) if apparent_power_va > 50.0 and abs(power_w) > 0 else 0.0
            phases = int(self._float_value(pro_backend.get("phases_in_use", pro_backend.get("phases_actual")), 0.0))
            if phases <= 0 and currents:
                phases = sum(1 for v in currents if self._float_value(v, 0.0) > 0.2)
            live_vehicle_id = pro_backend.get("vehicle_id") or live_vehicle_id
            live_rfid_tag = pro_backend.get("rfid_tag") or live_rfid_tag
            if pro_backend.get("soc_value") not in (None, "null"):
                cp_data["soc"] = pro_backend.get("soc_value")
                cp_data["soc_timestamp"] = pro_backend.get("soc_timestamp")
            if pro_backend.get("imported") not in (None, "null"):
                cp_data["imported"] = pro_backend.get("imported")
            if self.primary_mode_enabled:
                base_surface = "openwb_primary_simpleapi"
            else:
                base_surface = "openwb_secondary_modbus" if self.modbus_enabled else "openwb_secondary_set_current_heartbeat"
            self.state["api_surface"] = f"{base_surface}+pro_backend"
            self.state["backend_pro_ip"] = pro_backend.get("_backend_ip", "")

        stable_vehicle_identity_current = bool(
            str(live_vehicle_id or "").strip()
            or str(live_rfid_tag or "").strip()
        )

        prev_plug = self.state.get("plug_state", False)
        plug_observed = bool(self.state.get("_plug_state_observed", False))
        daily_imported = self._float_value(cp_data.get("daily_imported"), self.state.get("daily_imported_wh", 0.0))
        effective_plug_state = bool(plug_state or locked or charge_state or power_w > 50.0)
        if not effective_plug_state:
            evse_current = 0.0
            phases = 0
        if effective_plug_state and plug_observed and not prev_plug:
            self.state["_session_start_wh"] = daily_imported
            self.state["_session_start_ts"] = int(time.time())
            self.state["session_kwh"] = 0.0
            logger.info(f"[WB{self.wb_id}] Auto eingesteckt! Session-Zaehler gestartet.")
        elif not effective_plug_state:
            self.state["_session_start_wh"] = None
            self.state["_session_start_ts"] = None
            self.state["_session_vehicle_id"] = None
            self.state["_session_rfid_tag"] = None
        self.state["_plug_state_observed"] = True

        if effective_plug_state:
            if live_vehicle_id:
                self.state["_session_vehicle_id"] = live_vehicle_id
            else:
                live_vehicle_id = self.state.get("_session_vehicle_id")
            if live_rfid_tag:
                self.state["_session_rfid_tag"] = live_rfid_tag
            else:
                live_rfid_tag = self.state.get("_session_rfid_tag")

        start = self.state.get("_session_start_wh")
        if start is not None and effective_plug_state:
            self.state["session_kwh"] = max(0.0, (daily_imported - start) / 1000.0)

        car_soc = self._soc_percent_value(cp_data.get("soc", cp_data.get("pro_soc")))
        if car_soc is not None:
            observed_ts = time.time()
            car_soc_upstream_source = self._text_value(
                cp_data.get("car_soc_source", cp_data.get("soc_source")),
                "",
            )
            # Der Transportweg ist die belegte Quelle. Ein frei gelieferter
            # Gerätetext bleibt Diagnose und darf den Regelvertrag nicht
            # erweitern.
            car_soc_source = "openwb_http"
            car_soc_source_ts = self._soc_source_timestamp(
                cp_data.get("soc_timestamp"),
                now_ts=observed_ts,
            )
            self._record_soc_observation(
                car_soc,
                source=car_soc_source,
                observed_ts=observed_ts,
                source_ts=car_soc_source_ts,
                retained=False,
                vehicle_key=str(live_vehicle_id or live_rfid_tag or "").strip(),
            )
            car_soc_rule_confirmed = bool(
                car_soc_source_ts is not None
                and self._soc_source_rule_confirmed(car_soc_source)
            )
            previous_soc_ts = self._soc_source_timestamp(
                self.state.get("car_soc_source_ts"),
                now_ts=observed_ts,
            )
            previous_confirmed = bool(
                self.state.get("car_soc_rule_confirmed") is True
                and previous_soc_ts is not None
            )
            candidate_is_freshest = bool(
                car_soc_rule_confirmed
                and (
                    not previous_confirmed
                    or car_soc_source_ts >= previous_soc_ts
                )
            )
            if (
                candidate_is_freshest
                or (
                    not previous_confirmed
                    and (
                        previous_soc_ts is None
                        or car_soc_source_ts is not None
                    )
                )
            ):
                self.state["car_soc"] = car_soc
                self.state["car_soc_source"] = car_soc_source
                self.state["car_soc_upstream_source"] = car_soc_upstream_source
                self.state["car_soc_source_ts"] = car_soc_source_ts
                self.state["car_soc_raw_ts"] = car_soc_source_ts
                self.state["car_soc_rule_confirmed"] = car_soc_rule_confirmed
                self._write_manual_soc(
                    car_soc,
                    effective_plug_state,
                    car_soc_source_ts,
                    source=car_soc_source,
                    soc_source_ts=car_soc_source_ts,
                    raw_soc_ts=car_soc_source_ts,
                    rule_confirmed=car_soc_rule_confirmed,
                )
            else:
                # Eine unbestätigte HTTP-Projektion darf einen vorhandenen
                # echten MQTT-/SimpleAPI-Anker nicht entwerten.
                self.state["car_soc_unconfirmed_observed"] = car_soc
                self.state["car_soc_unconfirmed_source"] = car_soc_source
                self.state["car_soc_unconfirmed_observed_ts"] = int(observed_ts)
        range_observed_ts = time.time()
        range_source_ts = cp_data.get("soc_timestamp")
        if range_source_ts in (None, "", "null"):
            range_source_ts = None
        range_vehicle_key = (
            str(live_vehicle_id or live_rfid_tag or "").strip()
            if stable_vehicle_identity_current
            else ""
        )
        if effective_plug_state and "range_charged" in cp_data:
            self._set_charged_range(
                cp_data.get("range_charged"),
                "http_charged",
                observed_ts=range_observed_ts,
                source_ts=range_source_ts,
                vehicle_key=range_vehicle_key,
            )
        elif not effective_plug_state:
            self._clear_charged_range()
        else:
            self._clear_charged_range(source="http_charged")

        car_range, car_range_present = self._extract_total_range(cp_data)
        if effective_plug_state and car_range_present and car_range > 0.0:
            self._set_total_range(
                car_range,
                "http_total",
                observed_ts=range_observed_ts,
                source_ts=range_source_ts,
                vehicle_key=range_vehicle_key,
            )
        elif not effective_plug_state:
            self._clear_total_range()
        else:
            self._clear_total_range(source="http_total")

        self.state.update({
            "plug_state": effective_plug_state,
            "plug_state_raw": plug_state,
            "locked": locked,
            "charge_state": charge_state,
            "car": 2 if effective_plug_state else 1,
            "charging": bool(charge_state or phase_power_verified),
            "real_power_w": power_w,
            "phase_power_l1_w": p1,
            "phase_power_l2_w": p2,
            "phase_power_l3_w": p3,
            "phase_power_sum_w": phase_power_sum,
            "phase_power_verified": phase_power_verified,
            "phase_apparent_l1_va": phase_apparent[0],
            "phase_apparent_l2_va": phase_apparent[1],
            "phase_apparent_l3_va": phase_apparent[2],
            "apparent_power_va": apparent_power_va,
            "power_factor": power_factor,
            "evse_current": evse_current,
            "amp": int(round(evse_current)) if evse_current > 0 else 0,
            "phases_in_use": int(phases),
            "pha": 56 if int(phases) >= 3 else (8 if int(phases) >= 1 else 0),
            "can_switch_phases": bool(self.state.get("can_switch_phases", False)),
            "phase_switch_capability": self.state.get("phase_switch_capability", "secondary_current_only"),
            "phase_switch_source": self.state.get("phase_switch_source", "disabled_by_design"),
            "api_surface": self.state.get("api_surface", "openwb_primary_simpleapi" if self.primary_mode_enabled else "openwb_secondary_set_current_heartbeat"),
            "control_status": self.state.get("control_status", ""),
            "control_label": self.state.get("control_label", ""),
            "control_detail": self.state.get("control_detail", ""),
            "control_level": self.state.get("control_level", "info"),
            "control_ts": self.state.get("control_ts", 0),
            "last_command_ok": self.state.get("last_command_ok", None),
            "last_command_amp": self.state.get("last_command_amp", None),
            "last_command_ts": self.state.get("last_command_ts", 0),
            "last_heartbeat_ok": self.state.get("last_heartbeat_ok", None),
            "last_heartbeat_ts": self.state.get("last_heartbeat_ts", 0.0),
            "daily_imported_wh": daily_imported,
            "imported_total_wh": self._float_value(cp_data.get("imported"), self.state.get("imported_total_wh", 0.0)),
            "chargemode_str": self._text_value(cp_data.get("chargemode"), self.state.get("chargemode_str", "")),
            "chargepoint_name": self._text_value(cp_data.get("config_name", cp_data.get("name")), self.state.get("chargepoint_name", "")),
            "charge_template_name": self._text_value(cp_data.get("charge_template_name"), self.state.get("charge_template_name", "")),
            # Die Session-ID darf kurze openWB-Aussetzer überleben. Für die
            # Anzeige muss aber getrennt erkennbar bleiben, ob die aktuelle
            # Antwort wirklich eine Fahrzeugidentität geliefert hat.
            # Kompatibilitätsfeld und expliziter Wahrheitsvertrag meinen beide
            # ausschließlich eine aktuell gelieferte stabile ID/RFID. Name und
            # erhaltene Session-ID reichen dafür nicht.
            "vehicle_identity_current": bool(stable_vehicle_identity_current),
            "stable_vehicle_identity_current": bool(stable_vehicle_identity_current),
            "state_text": self._text_value(cp_data.get("state_str"), self.state.get("state_text", "")),
            "fault_text": self._text_value(cp_data.get("fault_str"), self.state.get("fault_text", "")),
            "fault_state": int(self._float_value(cp_data.get("fault_state"), self.state.get("fault_state", 0))),
            "manual_lock": self._bool_value(cp_data.get("manual_lock"), self.state.get("manual_lock", False)),
            "min_current": self._float_value(cp_data.get("min_current"), self.state.get("min_current", 0.0)),
            "pv_charging_min_current": self._float_value(cp_data.get("pv_charging_min_current"), self.state.get("pv_charging_min_current", 0.0)),
            "instant_charging_current": self._float_value(cp_data.get("instant_charging_current"), self.state.get("instant_charging_current", 0.0)),
            "instant_charging_limit": self._text_value(cp_data.get("instant_charging_limit"), self.state.get("instant_charging_limit", "")),
            "instant_charging_soc": self._float_value(cp_data.get("instant_charging_soc"), self.state.get("instant_charging_soc", 0.0)),
            "car_name": self._text_value(cp_data.get("connected_vehicle_name"), self.state.get("car_name", "")),
            "car_id": (live_vehicle_id or self.state.get("car_id")) if effective_plug_state else None,
            "vehicle_id": (live_vehicle_id or self.state.get("vehicle_id")) if effective_plug_state else None,
            "rfid_tag": (live_rfid_tag or self.state.get("rfid_tag")) if effective_plug_state else None,
            "car_range": self.state.get("car_range", 0.0),
        })
        self.write_openwb_status()
        return True

    def write_openwb_status(self):
        """Schreibt aktuellen openWB-Status als JSON in die Ramdisk."""
        out_file  = os.path.join(RAMDISK_DIR, f"openwb_data_wb{self.wb_id}.json")
        out_alias = os.path.join(RAMDISK_DIR, "openwb_data.json") if self.wb_id == 1 else None
        payload = {
            'plug_state':        self.state['plug_state'],
            'plug_state_raw':    bool(self.state.get('plug_state_raw', self.state['plug_state'])),
            'locked':            bool(self.state.get('locked', False)),
            'charge_state':      self.state['charge_state'],
            'power_w':           round(self.state['real_power_w'], 1),
            'phase_power_l1_w':  round(self.state.get('phase_power_l1_w', 0.0), 1),
            'phase_power_l2_w':  round(self.state.get('phase_power_l2_w', 0.0), 1),
            'phase_power_l3_w':  round(self.state.get('phase_power_l3_w', 0.0), 1),
            'phase_power_sum_w': round(self.state.get('phase_power_sum_w', self.state['real_power_w']), 1),
            'phase_power_verified': bool(self.state.get('phase_power_verified', False)),
            'phase_apparent_l1_va': round(self.state.get('phase_apparent_l1_va', 0.0), 1),
            'phase_apparent_l2_va': round(self.state.get('phase_apparent_l2_va', 0.0), 1),
            'phase_apparent_l3_va': round(self.state.get('phase_apparent_l3_va', 0.0), 1),
            'apparent_power_va': round(self.state.get('apparent_power_va', 0.0), 1),
            'apparent_power_kva': round(self.state.get('apparent_power_va', 0.0) / 1000.0, 2),
            'power_factor': round(self.state.get('power_factor', 0.0), 2),
            'evse_current':      self.state['evse_current'],
            'phases_in_use':     self.state['phases_in_use'],
            'can_switch_phases': self.state['can_switch_phases'],
            'phase_switch_capability': self.state.get('phase_switch_capability', 'secondary_current_only'),
            'phase_switch_source': self.state.get('phase_switch_source', 'disabled_by_design'),
            'api_surface':        self.state.get('api_surface', 'openwb_primary_simpleapi' if self.primary_mode_enabled else 'openwb_secondary_set_current_heartbeat'),
            'control_status':     self.state.get('control_status', ''),
            'control_label':      self.state.get('control_label', ''),
            'control_detail':     self.state.get('control_detail', ''),
            'control_level':      self.state.get('control_level', 'info'),
            'control_ts':         self.state.get('control_ts', 0),
            'last_command_ok':    self.state.get('last_command_ok', None),
            'last_command_amp':   self.state.get('last_command_amp', None),
            'last_command_ts':    self.state.get('last_command_ts', 0),
            'last_heartbeat_ok':  self.state.get('last_heartbeat_ok', None),
            'last_heartbeat_ts':  self.state.get('last_heartbeat_ts', 0.0),
            'configured_role':    self.state.get('configured_role', ''),
            'detected_role':      self.state.get('detected_role', ''),
            'effective_role':     self.state.get('effective_role', ''),
            'role_detection_source': self.state.get('role_detection_source', ''),
            'role_detection_detail': self.state.get('role_detection_detail', ''),
            'role_mismatch':      bool(self.state.get('role_mismatch', False)),
            'command_failure_count': int(self.state.get('command_failure_count', 0) or 0),
            'command_failure_limit': int(self.state.get('command_failure_limit', self.command_failure_limit) or self.command_failure_limit),
            'command_blocked':    bool(self.state.get('command_blocked', False)),
            'command_blocked_until': int(self.state.get('command_blocked_until', 0) or 0),
            'connected_phases':   self.state.get('connected_phases', 0),
            'chargepoint_detection_source': self.state.get('chargepoint_detection_source', ''),
            'chargepoint_discovery_contract': self.state.get('chargepoint_discovery_contract', {}),
            'physical_output_identity': self.physical_output_identity_contract(),
            'physical_output_blocked': bool(getattr(self, '_physical_output_blocked', False)),
            'physical_output_block_reason': str(getattr(self, '_physical_output_block_reason', '') or ''),
            'daily_imported_wh': round(self.state['daily_imported_wh'], 0),
            'imported_total_wh': round(self.state['imported_total_wh'], 0),
            'chargemode':        self.state['chargemode_str'],
            'chargepoint_name':  self.state.get('chargepoint_name', ''),
            'charge_template_name': self.state.get('charge_template_name', ''),
            'vehicle_identity_current': bool(self.state.get('vehicle_identity_current', False)),
            'stable_vehicle_identity_current': bool(self.state.get('stable_vehicle_identity_current', False)),
            'state_text':        self.state.get('state_text', ''),
            'fault_text':        self.state.get('fault_text', ''),
            'fault_state':       self.state.get('fault_state', 0),
            'manual_lock':       bool(self.state.get('manual_lock', False)),
            'min_current':       self.state.get('min_current', 0.0),
            'pv_charging_min_current': self.state.get('pv_charging_min_current', 0.0),
            'instant_charging_current': self.state.get('instant_charging_current', 0.0),
            'instant_charging_limit': self.state.get('instant_charging_limit', ''),
            'instant_charging_soc': self.state.get('instant_charging_soc', 0.0),
            'session_kwh':       round(self.state['session_kwh'], 3),
            'session_start_ts':  self.state.get('_session_start_ts') if self.state.get('plug_state') else None,
            'cp_id':             self.cp_id,
            'wb_id':             self.wb_id,
            'ts':                int(time.time()),
            'car_soc':           self.state.get('car_soc', 0),
            'car_soc_source':    self.state.get('car_soc_source', ''),
            'car_soc_source_ts': self.state.get('car_soc_source_ts'),
            'car_soc_raw_ts':    self.state.get('car_soc_raw_ts'),
            'car_soc_rule_confirmed': self.state.get('car_soc_rule_confirmed') is True,
            'car_soc_observed': self.state.get('car_soc_observed'),
            'car_soc_observed_source': self.state.get('car_soc_observed_source', ''),
            'car_soc_observed_source_ts': self.state.get('car_soc_observed_source_ts'),
            'car_soc_observed_received_ts': self.state.get('car_soc_observed_received_ts', 0),
            'car_soc_observed_retained': bool(self.state.get('car_soc_observed_retained', False)),
            'car_soc_display_usable': bool(self.state.get('car_soc_display_usable', False)),
            'car_soc_observed_session_start_ts': self.state.get('car_soc_observed_session_start_ts'),
            'car_soc_observed_vehicle_id': self.state.get('car_soc_observed_vehicle_id', ''),
            'car_soc_observed_profile_id': self.state.get('car_soc_observed_profile_id', ''),
            'car_range':         self.state.get('car_range', 0),
            'range_km':          self.state.get('car_range', 0),
            'car_range_source':  self.state.get('car_range_source', ''),
            'car_range_valid':   bool(self.state.get('car_range_valid', False)),
            'car_range_observed_ts': self.state.get('car_range_observed_ts', 0),
            'car_range_source_ts': self.state.get('car_range_source_ts'),
            'car_range_source_ts_explicit': bool(self.state.get('car_range_source_ts_explicit', False)),
            'car_range_vehicle_key': self.state.get('car_range_vehicle_key', ''),
            'car_charged_range': self.state.get('car_charged_range', 0),
            'charged_range_km':  self.state.get('car_charged_range', 0),
            'car_charged_range_source': self.state.get('car_charged_range_source', ''),
            'car_charged_range_valid': bool(self.state.get('car_charged_range_valid', False)),
            'car_charged_range_observed_ts': self.state.get('car_charged_range_observed_ts', 0),
            'car_charged_range_source_ts': self.state.get('car_charged_range_source_ts'),
            'car_charged_range_source_ts_explicit': bool(self.state.get('car_charged_range_source_ts_explicit', False)),
            'car_charged_range_vehicle_key': self.state.get('car_charged_range_vehicle_key', ''),
            # Fahrzeug-Identitaet (aus connected_vehicle/info)
            'car_name':          self.state.get('car_name', ''),
            'car_id':            self.state.get('car_id', None),
            'vehicle_id':        self.state.get('vehicle_id', None),
            'rfid_tag':          self.state.get('rfid_tag', None),
            'car_capacity_kwh':  self.state.get('car_capacity_kwh', 0.0),
            'car_consumption_kwh_100km': self.state.get('car_consumption_kwh_100km', 0.0),
        }
        try:
            tmp = out_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(payload, f)
            os.replace(tmp, out_file)
            if out_alias:
                alias_tmp = out_alias + ".tmp"
                with open(alias_tmp, 'w') as alias_handle:
                    json.dump(payload, alias_handle)
                os.replace(alias_tmp, out_alias)
        except Exception as e:
            logger.debug(f"[WB{self.wb_id}] openwb_data.json schreiben fehlgeschlagen: {e}")

    def get_status(self):
        cp = self.cp_id if self.cp_id != "" else "auto"
        payload = self._http_get_json(f"get_chargepoint_all={cp}")
        if payload is not None and self._update_from_simpleapi(payload):
            now_ts = int(time.time())
            self.state.update({
                "driver_status_valid": True,
                "driver_status_stale": False,
                "driver_status_degraded": False,
                "driver_status_age_s": 0.0,
                "driver_status_reason": "fresh",
                "driver_status_last_ok_ts": now_ts,
                "driver_status_last_sample_ts": now_ts,
            })
            return self._sanitize_measurement_status(self.state)
        self.state.update({
            "driver_status_valid": False,
            "driver_status_stale": True,
            "driver_status_degraded": True,
            "driver_status_reason": (
                "openwb_http_status_unavailable"
                if payload is None
                else "openwb_http_status_invalid"
            ),
            "driver_status_last_sample_ts": int(time.time()),
        })
        return None

    def set_amp_and_state(self, target_amp, force_state=None):
        """Steuert openWB 2.x je nach Konfigurationsrolle.

        Standard: Secondary-Sollstrom; der Manager sendet den Heartbeat in
        einem getrennten freien Ausgangszyklus.
        openWB Primary Opt-in: PV/Sofort/Stop per simpleAPI, openWB regelt den
        Ladepunkt weiter selbst.
        """
        if not command_gate.allow_command(
            self,
            action="openwb_set_amp_and_state",
            payload={"target_amp": target_amp, "force_state": force_state},
        ):
            return False
        if self._commands_temporarily_blocked():
            return False
        try:
            amp = 0 if force_state == 1 else target_amp
            if self.primary_mode_enabled:
                if force_state == 1 or float(amp or 0) < 0.5:
                    return self._primary_set_chargemode("stop")
                return self._primary_set_instant_current(amp)
            return self._secondary_set_current(amp)
        except Exception as e:
            logger.error(f"[WB{self.wb_id}] set_amp_and_state Fehler: {e}")
            return False

    def set_pv_mode(self):
        """Gibt openWB in ihren PV-Modus oder haelt den Secondary-Heartbeat."""
        if not command_gate.allow_command(
            self,
            action="openwb_set_pv_mode",
            payload={"cp_id": self.cp_id},
        ):
            return False
        if self._commands_temporarily_blocked():
            return False
        if self.primary_mode_enabled:
            return self._primary_set_chargemode("pv")
        logger.debug(f"[WB{self.wb_id}] PV-Modus liegt bei openWB; E3DC-Control setzt nur Strom/Heartbeat.")
        ok = self._secondary_heartbeat()
        self._set_control_state(
            "self_regulated",
            "openWB regelt selbst",
            f"E3DC-Control hält nur den Secondary-Heartbeat; openWB führt PV-/Ziel-/Phasenlogik. Heartbeat {'ok' if ok else 'nicht bestätigt'}.",
            "info" if ok else "warning",
            ok=ok,
        )
        return ok

    def release_to_default(self, max_amp=16):
        """openWB sauber loslassen."""
        if self._commands_temporarily_blocked():
            return False
        if self.primary_mode_enabled:
            ok = self._primary_set_chargemode("pv")
            logger.info(f"[WB{self.wb_id}] Default-Freigabe: openWB Primary zurück auf PV (ok={ok})")
            return ok
        ok = self._secondary_set_current(0)
        logger.info(
            f"[WB{self.wb_id}] Default-Freigabe: Secondary-Sollstrom 0A, "
            f"kein openWB-Lademoduswechsel (ok={ok})"
        )
        return ok

    def set_direct_current(self, target_amp):
        """Setzt Secondary-Sollstrom oder Primary-Sofortladen."""
        if not command_gate.allow_command(
            self,
            action="openwb_set_direct_current",
            payload={"target_amp": target_amp},
        ):
            return False
        if self._commands_temporarily_blocked():
            return False
        if self.primary_mode_enabled:
            return self._primary_set_instant_current(target_amp)
        return self._secondary_set_current(target_amp)

    def set_phases(self, phases):
        """Normale openWB bekommt keine Phasenbefehle von E3DC-Control."""
        if not command_gate.allow_command(
            self,
            action="openwb_set_phases",
            payload={"phases": phases},
        ):
            return False
        logger.warning(
            f"[WB{self.wb_id}] Phasenumschaltung ignoriert: normale openWB "
            f"wird nur über Sollstrom und Heartbeat geführt."
        )
        return False


# ===========================================================================
# E3DCCharger (native E3DC Wallbox per RSCP)
# ===========================================================================
class E3DCCharger(WallboxDriver):
    """Treiber für native E3DC Wallbox über RSCP."""

    def __init__(self, ip, wb_id, config):
        super().__init__(ip, wb_id)
        self.config = config or {}
        configured_type = str(self.config.get("_e3dc_configured_type") or "e3dc").strip().lower()
        configured_family = None
        for key in (
            f"wb{int(wb_id or 1)}_e3dc_device_family",
            "e3dc_device_family",
        ):
            if self.config.get(key) not in (None, ""):
                configured_family = self.config.get(key)
                break
        type_family = {
            "e3dc_efy": "efy",
            "e3dc_easy": "easy_connect",
            "e3dc_easy_connect": "easy_connect",
            # Der generische Legacy-Typ belegt keine Easy-Connect-Hardware.
            # Ein falscher Easy-Retry könnte eine inzwischen laufende Ladung
            # mit einem zweiten Toggle wieder beenden.
            "e3dc_legacy": "unknown",
            "e3dc_multi": "multi_connect",
            "e3dc_multi_connect": "multi_connect",
            "e3dc_multi_connect_ii": "multi_connect_ii",
        }.get(configured_type, "unknown")
        self.transport = E3DC_TRANSPORT
        self.device_family = _normalize_e3dc_device_family(configured_family, type_family)
        self.device_family_source = "configured" if configured_family not in (None, "") else (
            "configured_type" if type_family != "unknown" else "unknown"
        )
        self.rscp_wallbox_type = None
        self.wbchar6_compat_explicit = _config_bool(
            self.config,
            f"wb{int(wb_id or 1)}_e3dc_wbchar6_compat_enable",
            "e3dc_wbchar6_compat_enable",
            default=configured_type in {
                "native", "e3dc", "e3dc_easy", "e3dc_easy_connect",
                "e3dc_legacy", "e3dc_efy", "e3dc_multi", "e3dc_multi_connect",
                "e3dc_multi_connect_ii",
            },
        )
        self.efy_autonomous_wbchar6_verified = _config_bool(
            self.config,
            f"wb{int(wb_id or 1)}_e3dc_efy_autonomous_wbchar6_verified",
            "e3dc_efy_autonomous_wbchar6_verified",
            default=False,
        )
        self.direct_transition_capable = False
        self.direct_transition_readback_complete = False
        self.direct_transition_readback_ts = 0.0
        self.control_backend = (
            E3DC_BACKEND_WBCHAR6
            if self.wbchar6_compat_explicit
            else E3DC_BACKEND_STATUS_ONLY
        )
        self.capability_state = (
            "wbchar6_compat_explicit"
            if self.wbchar6_compat_explicit
            else "readback_unverified"
        )
        self.server_ip = str(self.config.get("server_ip") or "").strip()
        try:
            self.server_port = int(self.config.get("server_port", 5033))
        except (TypeError, ValueError):
            self.server_port = 0
        self.user = str(self.config.get("e3dc_user") or "").strip()
        self.password = str(self.config.get("e3dc_password") or "").strip()
        self.aes_password = str(self.config.get("aes_password") or "").strip()
        self.wb_index    = int(wb_id) - 1
        self.conn        = None
        self.last_connect_time = 0
        import threading
        # Das letzte Gate kann die Übergabe ohne Schreibzugriff aufrufen, während
        # unter dieser Sperre ein Befehlsrahmen vorbereitet wird. Reentranz
        # verhindert einen Deadlock; der Ausschluss zwischen Threads bleibt bestehen.
        self.lock = threading.RLock()
        # Default ist echte Funkstille. Erst set_amp_* aktiviert SET_EXTERN-Heartbeat.
        self.last_amp = None
        self.last_force_state = None
        self.external_suspended = True
        self._control_generation = 0
        self.real_charging = False
        self.sonnenmodus = False  # True = E3DC steuert autonom (Mode=1), False = Python-Kontrolle (Mode=2)
        self.last_stop_toggle_ts = 0.0
        self._wbchar6_output_seq = 0
        self._wbchar6_last_wire_receipt = {}
        self._wbchar6_dispatch_seq = 0
        self._wbchar6_last_dispatch_outcome = {}
        self._native_status_sample_seq = 0
        self._last_alg_flags = 0
        self._wbchar6_readback_ts = 0.0
        self._wbchar6_last_plugged = None
        self._wbchar6_stop_confirmed = False
        self._wbchar6_stop_episode = 0
        self._wbchar6_start_sent_key = None
        self._wbchar6_start_attempt_count = 0
        self._wbchar6_last_start_toggle_ts = 0.0
        self._efy_autonomous_handoff_blocker = ""
        self._efy_autonomous_handoff_ts = 0.0
        self.rscp_error_count = 0
        self.rscp_last_error = ""
        self.rscp_last_error_context = ""
        self.rscp_last_error_ts = 0.0
        self.rscp_last_ok_ts = 0.0
        self.rscp_last_ok_context = ""
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb_thread.start()

    def _backend_contract_fields(self):
        if self.control_backend == E3DC_BACKEND_WBCHAR6:
            label = "WBchar6-Kompatibilität"
        elif self.capability_state == "efy_sleeping_state_inconclusive":
            label = "Direkte Übergänge unbestätigt – Wallbox ohne Fahrzeug möglicherweise inaktiv"
        elif self.direct_transition_readback_complete:
            label = "Direkte Übergänge gesperrt – Readback-Semantik unbestätigt"
        else:
            label = "Direkte Übergänge gesperrt"
        return {
            "e3dc_transport": self.transport,
            "e3dc_device_family": self.device_family,
            "e3dc_device_family_source": self.device_family_source,
            "e3dc_rscp_wallbox_type": self.rscp_wallbox_type,
            "e3dc_rscp_wallbox_type_semantics": (
                "raw_unclassified_numeric_readback"
                if type(self.rscp_wallbox_type) is int
                else "missing"
            ),
            "e3dc_device_family_inferred_from_wallbox_type": False,
            "e3dc_control_backend": self.control_backend,
            "e3dc_backend_label": label,
            "e3dc_capability_state": self.capability_state,
            "e3dc_direct_transition_capable": bool(self.direct_transition_capable),
            "e3dc_direct_readback_complete": bool(self.direct_transition_readback_complete),
            "e3dc_direct_transition_write_allowed": False,
            "e3dc_direct_readback_ts": float(self.direct_transition_readback_ts or 0.0),
            "e3dc_wbchar6_compat_explicit": bool(self.wbchar6_compat_explicit),
            "e3dc_efy_autonomous_wbchar6_verified": bool(
                self.efy_autonomous_wbchar6_verified
            ),
            "e3dc_efy_autonomous_wbchar6_provenance": (
                "field_verified_legacy"
                if self.efy_autonomous_wbchar6_verified
                else "unverified"
            ),
            "e3dc_wbchar6_start_attempt_count": int(self._wbchar6_start_attempt_count),
            "e3dc_wbchar6_start_attempt_limit": E3DC_EASY_CONNECT_START_ATTEMPT_LIMIT,
            "e3dc_wbchar6_last_start_toggle_ts": float(self._wbchar6_last_start_toggle_ts or 0.0),
            "e3dc_wbchar6_bounded_retry_eligible": self.device_family == "easy_connect",
            "e3dc_wbchar6_bounded_retry_provenance": (
                "explicit_family_or_device_name"
                if self.device_family == "easy_connect"
                and self.device_family_source in {
                    "configured",
                    "configured_type",
                    "device_name_readback",
                }
                else "not_eligible"
            ),
            "e3dc_efy_autonomous_handoff_blocker": str(
                self._efy_autonomous_handoff_blocker or ""
            ),
            "e3dc_efy_autonomous_handoff_ts": float(
                self._efy_autonomous_handoff_ts or 0.0
            ),
        }

    def _e3dc_readback_contract(
        self,
        status,
        *,
        sample_ts,
        sample_fresh,
        phase_power_sample_valid,
    ):
        """Beschreibt RSCP-Readback und Grenzen seiner Aussagekraft.

        Der Vertrag ist rein diagnostisch. Insbesondere werden numerischer
        Wallbox-Typ, aktuelle Phasenkonfiguration und PARAM_1-Bytes niemals in
        eine Hardwarefähigkeit oder einen bestätigten Soll-/Ist-Strom
        umgedeutet.
        """

        st = status if isinstance(status, dict) else {}
        raw_type = st.get("wallbox_type", self.rscp_wallbox_type)
        raw_type = raw_type if type(raw_type) is int and raw_type >= 0 else None
        raw_phases = st.get("number_phases")
        raw_phases = (
            int(raw_phases)
            if type(raw_phases) is int and raw_phases in (1, 3)
            else None
        )
        raw_param_byte2 = st.get("param1_byte2_raw")
        raw_param_byte2 = (
            int(raw_param_byte2)
            if type(raw_param_byte2) is int and 0 <= raw_param_byte2 <= 255
            else None
        )
        phase_type_codes = st.get("phase_power_rscp_type_codes")
        phase_type_codes = (
            dict(phase_type_codes)
            if isinstance(phase_type_codes, dict)
            else {}
        )
        try:
            timestamp = float(sample_ts)
        except (TypeError, ValueError, OverflowError):
            timestamp = 0.0
        if not math.isfinite(timestamp) or timestamp <= 0.0:
            timestamp = 0.0

        return {
            "schema": "e3dc_wallbox_readback_v1",
            "read_only": True,
            "sample": {
                "ts": timestamp,
                "age_s": 0.0 if timestamp > 0.0 else None,
                "fresh": bool(sample_fresh and timestamp > 0.0),
                "source": "rscp_same_response",
            },
            "identity": {
                "device_name": str(st.get("device_name") or ""),
                "device_name_source": (
                    str(st.get("device_name_source") or "rscp_wb_device_name")
                    if st.get("device_name")
                    else "missing"
                ),
                "firmware_version": str(st.get("firmware_version") or ""),
                "firmware_version_source": (
                    str(
                        st.get("firmware_version_source")
                        or "rscp_wb_firmware_version"
                    )
                    if st.get("firmware_version")
                    else "missing"
                ),
                "raw_wallbox_type": raw_type,
                "raw_wallbox_type_source": (
                    "rscp_wb_wallbox_type" if raw_type is not None else "missing"
                ),
                "raw_wallbox_type_semantics": "unclassified_numeric_readback",
                "configured_family": str(self.device_family or "unknown"),
                "family_source": str(self.device_family_source or "unknown"),
                "family_inferred_from_raw_wallbox_type": False,
            },
            "phase_configuration": {
                "number_phases": raw_phases,
                "source": (
                    "rscp_wb_number_phases" if raw_phases is not None else "missing"
                ),
                "semantics": (
                    "current_configuration_not_hardware_wiring_or_switch_capability"
                ),
                "hardware_phase_capability": None,
                "phase_switch_capability_inferred": False,
            },
            "operating_state": {
                "auto_phase_switch_enabled": (
                    st.get("auto_phase_switch_enabled")
                    if type(st.get("auto_phase_switch_enabled")) is bool
                    else None
                ),
                "auto_phase_source": (
                    str(
                        st.get("auto_phase_switch_source")
                        or "rscp_wb_auto_phase_switch_enabled"
                    )
                    if type(st.get("auto_phase_switch_enabled")) is bool
                    else "missing"
                ),
                "sun_mode_active": (
                    st.get("sun_mode_active")
                    if type(st.get("sun_mode_active")) is bool
                    else None
                ),
                "sun_mode_source": (
                    str(st.get("sun_mode_source") or "rscp_wb_sun_mode_active")
                    if type(st.get("sun_mode_active")) is bool
                    else "missing"
                ),
                "abort_charging": (
                    st.get("abort_charging")
                    if type(st.get("abort_charging")) is bool
                    else None
                ),
                "abort_source": (
                    str(
                        st.get("abort_charging_source")
                        or "rscp_wb_abort_charging"
                    )
                    if type(st.get("abort_charging")) is bool
                    else "missing"
                ),
                "alg_flags": (
                    int(st.get("alg_flags"))
                    if type(st.get("alg_flags")) is int
                    else None
                ),
                "alg_abort_or_disabled": (
                    st.get("alg_abort_or_disabled")
                    if type(st.get("alg_abort_or_disabled")) is bool
                    else None
                ),
                "alg_bit_0x40_semantics": "abort_or_disabled_not_overload_protection",
            },
            "param_1": {
                "raw_hex": str(st.get("param1_hex") or ""),
                "byte2_raw": raw_param_byte2,
                "byte2_semantics": "unconfirmed_raw_byte_not_current_authority",
                "current_confirmed": False,
            },
            "phase_power": {
                "values_w": {
                    "l1": st.get("phase_power_l1_w"),
                    "l2": st.get("phase_power_l2_w"),
                    "l3": st.get("phase_power_l3_w"),
                },
                "expected_rscp_type": "Double64",
                "rscp_type_codes": phase_type_codes,
                "type_complete": bool(
                    st.get("phase_power_rscp_type_complete") is True
                ),
                "sample_valid": bool(phase_power_sample_valid),
                "source": "rscp_wb_pm_power_l1_l2_l3",
            },
            "capability_provenance": {
                "phase_switch": str(st.get("phase_switch_capability") or "unknown"),
                "phase_switch_source": str(st.get("phase_switch_source") or ""),
                "direct_transition_write_allowed": False,
                "raw_type_used_for_capability": False,
                "number_phases_used_for_hardware_capability": False,
            },
        }

    def _set_control_backend(self, *, status_valid, transition_capable=False, readback_ts=0.0):
        self.direct_transition_readback_complete = bool(transition_capable)
        family_eligible = self.device_family in {"multi_connect", "multi_connect_ii"}
        self.direct_transition_capable = bool(self.direct_transition_readback_complete and family_eligible)
        self.direct_transition_readback_ts = float(readback_ts or 0.0) if self.direct_transition_readback_complete else 0.0
        if self.direct_transition_capable:
            self.capability_state = "direct_readback_diagnostic_only"
            self.control_backend = (
                E3DC_BACKEND_WBCHAR6
                if self.wbchar6_compat_explicit
                else E3DC_BACKEND_STATUS_ONLY
            )
        else:
            if self.direct_transition_readback_complete:
                self.capability_state = (
                    "efy_sleeping_state_inconclusive"
                    if self.device_family == "efy"
                    else "direct_readback_semantics_unverified"
                )
            elif status_valid:
                self.capability_state = "direct_readback_incomplete"
            else:
                self.capability_state = "status_unavailable"
            if self.wbchar6_compat_explicit:
                self.control_backend = E3DC_BACKEND_WBCHAR6
            else:
                self.control_backend = E3DC_BACKEND_STATUS_ONLY

    def _update_device_identity(self, *, device_name=None, wallbox_type=None):
        if wallbox_type is not None:
            self.rscp_wallbox_type = wallbox_type
        # Eine Firmware-Nummer, ein Wallbox-Index oder ein numerischer Wallbox-Typ
        # belegt niemals eine Produktfamilie. Nur eine explizite Installationseinstellung
        # oder ein eindeutiger Gerätename im Readback darf eine unbekannte Familie verfeinern.
        detected = _device_family_from_name(device_name)
        if self.device_family == "unknown" and detected != "unknown":
            self.device_family = detected
            self.device_family_source = "device_name_readback"

    def _observe_wbchar6_status(self, decoded):
        if not isinstance(decoded, dict) or not decoded.get("valid"):
            self._wbchar6_readback_ts = 0.0
            self._wbchar6_stop_confirmed = False
            return
        now = time.time()
        flags = int(decoded.get("flags") or 0)
        plugged = decoded.get("plugged") is True
        charging = decoded.get("charging") is True
        # Bit 0x10 ist laut kanonischem Decoder die Steckerverriegelung, nicht
        # das Ladebit. Easy Connect meldet im regulären, startbereiten
        # Stillstand daher typischerweise 0x58 (verbunden + verriegelt +
        # externer Status), während allein 0x20 eine aktive Ladung bezeichnet.
        # Eine verriegelte Kupplung darf die sichere STOP-Postcondition nicht
        # in einen permanenten Nicht-Start-Zustand verwandeln.
        stopped = bool(plugged and (flags & 0x40) and not charging)
        previous_plugged = self._wbchar6_last_plugged
        previous_stopped = self._wbchar6_stop_confirmed
        self._last_alg_flags = flags
        self._wbchar6_readback_ts = now
        self._wbchar6_last_plugged = plugged
        self._wbchar6_stop_confirmed = stopped
        if stopped and (not previous_stopped or previous_plugged is False):
            self._wbchar6_stop_episode += 1
            self._wbchar6_start_sent_key = None
            self._wbchar6_start_attempt_count = 0
            self._wbchar6_last_start_toggle_ts = 0.0
        elif not plugged or charging:
            self._wbchar6_stop_confirmed = False

    def _wbchar6_start_toggle_allowed(self, force_state, *, is_heartbeat=False):
        if force_state != 2 or is_heartbeat or not self._wbchar6_stop_confirmed:
            return False
        max_age_s = 15.0
        try:
            max_age_s = max(1.0, float(self.config.get("e3dc_wbchar6_status_max_age_s", 15.0)))
        except (TypeError, ValueError):
            pass
        if self._wbchar6_readback_ts <= 0.0 or time.time() - self._wbchar6_readback_ts > max_age_s:
            return False
        episode = int(self._wbchar6_stop_episode)
        if episode != self._wbchar6_start_sent_key:
            return True

        # Easy Connect benötigt bei einzelnen Fahrzeugen einen echten zweiten
        # Startimpuls. Der Manager darf ihn nur als expliziten, zeitlich
        # begrenzten Startretry anfordern. Andere E3/DC-Familien bleiben beim
        # bewiesenen Ein-Impuls-Vertrag.
        if self.device_family != "easy_connect":
            return False
        if self._wbchar6_start_attempt_count >= E3DC_EASY_CONNECT_START_ATTEMPT_LIMIT:
            return False
        retry_s = E3DC_EASY_CONNECT_START_RETRY_MIN_S
        try:
            retry_s = max(
                E3DC_EASY_CONNECT_START_RETRY_MIN_S,
                float(self.config.get("e3dc_native_start_retry_s", 60.0)),
            )
        except (TypeError, ValueError):
            pass
        return bool(
            self._wbchar6_last_start_toggle_ts > 0.0
            and time.time() - self._wbchar6_last_start_toggle_ts >= retry_s
        )

    def e3dc_bounded_start_retry_ready(self):
        """Stellt die exakte read-only Freigabe der Easy-Connect-Startkante bereit.

        Der Manager darf sie nur verwenden, um eine vom Treiber bereits als
        sicher belegte Startkante nicht herabzustufen. Die endgültige
        Versuchsanzahl, ein frischer STOP-Readback und der 60-Sekunden-Abstand
        werden im tatsächlichen WBchar6-Sendepfad des Treibers erneut erzwungen.
        """

        return bool(
            self.device_family == "easy_connect"
            and self._wbchar6_start_toggle_allowed(2, is_heartbeat=False)
        )

    def _mark_wbchar6_start_toggle_sent(self):
        episode = int(self._wbchar6_stop_episode)
        if self._wbchar6_start_sent_key != episode:
            self._wbchar6_start_attempt_count = 0
        self._wbchar6_start_sent_key = episode
        self._wbchar6_start_attempt_count += 1
        self._wbchar6_last_start_toggle_ts = time.time()

    def _record_rscp_ok(self, context):
        self.rscp_last_ok_ts = time.time()
        self.rscp_last_ok_context = str(context or "")

    def _record_rscp_error(self, context, error):
        self.rscp_error_count = int(getattr(self, "rscp_error_count", 0) or 0) + 1
        self.rscp_last_error_context = str(context or "")
        self.rscp_last_error_ts = time.time()
        message = str(error or "unknown")
        if self.rscp_last_error_context:
            message = f"{self.rscp_last_error_context}: {message}"
        self.rscp_last_error = message[:240]

    def _rscp_diag_status(self):
        last_error_ts = float(getattr(self, "rscp_last_error_ts", 0.0) or 0.0)
        last_ok_ts = float(getattr(self, "rscp_last_ok_ts", 0.0) or 0.0)
        error_active = bool(last_error_ts > last_ok_ts)
        return {
            "rscp_status": "error" if error_active else ("ok" if last_ok_ts > 0 else "unknown"),
            "rscp_error_active": error_active,
            "rscp_error_count": int(getattr(self, "rscp_error_count", 0) or 0),
            "rscp_last_error": str(getattr(self, "rscp_last_error", "") or ""),
            "rscp_last_error_context": str(getattr(self, "rscp_last_error_context", "") or ""),
            "rscp_last_error_ts": int(last_error_ts) if last_error_ts > 0 else 0,
            "rscp_last_ok_ts": int(last_ok_ts) if last_ok_ts > 0 else 0,
            "rscp_last_ok_context": str(getattr(self, "rscp_last_ok_context", "") or ""),
        }

    def _minimal_rscp_status(self):
        self._set_control_backend(status_valid=False)
        driver_variant = getattr(self, "driver_variant", "e3dc_native")
        is_multi_connect = driver_variant == "e3dc_multi_connect"
        phase_capability = (
            "e3dc_multi_connect_cp_480_unverified"
            if is_multi_connect
            else "e3dc_native_fixed"
        )
        status = {
            'car': 1,
            'amp': 0,
            'pha': 56,
            'charging': None,
            'plug_locked': None,
            'alg_seen': False,
            'alg_flags': 0,
            'alg_abort_or_disabled': None,
            'alg_charging': None,
            'alg_connected': None,
            'device_working': None,
            'real_power_w': 0.0,
            'car_connected_rscp': None,
            'wb_status_valid': False,
            'wb_status_source': 'rscp_wb_extern_data_alg',
            'wb_status_reason': 'rscp_status_unavailable',
            'driver_variant': driver_variant,
            'rscp_wb_index': self.wb_index,
            'phase_power_l1_w': 0.0,
            'phase_power_l2_w': 0.0,
            'phase_power_l3_w': 0.0,
            'phase_power_sum_w': 0.0,
            'phase_power_verified': False,
            'phase_power_sample_valid': False,
            'native_fixed_phases': 0,
            'native_fixed_phases_valid': False,
            'native_fixed_phases_source': '',
            'native_status_sample_seq': int(
                getattr(self, '_native_status_sample_seq', 0) or 0
            ),
            'native_status_sample_ts': 0.0,
            'phases_in_use': 0,
            'phases_actual': 0,
            'phases_target': 0,
            'number_phases': 0,
            'connected_phases': 0,
            'can_switch_phases': False,
            'phase_switch_capability': phase_capability,
            'phase_switch_source': (
                'disabled_by_hardware_protection'
                if is_multi_connect
                else 'rscp_status'
            ),
            'api_surface': '',
            'charge_contract': {},
            'charge_truth': 'unknown',
            'charge_source': 'rscp_status_unavailable',
            'driver_status_valid': False,
            'driver_status_stale': True,
            'driver_status_degraded': True,
            'driver_status_reason': 'rscp_status_unavailable',
            'e3dc_readback_sample_ts': 0.0,
            'e3dc_readback_age_s': None,
            'e3dc_readback_fresh': False,
            'e3dc_readback_source': 'rscp_same_response',
        }
        status.update(self._backend_contract_fields())
        status.update(self._rscp_diag_status())
        status["e3dc_readback_contract"] = self._e3dc_readback_contract(
            status,
            sample_ts=0.0,
            sample_fresh=False,
            phase_power_sample_valid=False,
        )
        return status

    def _finalize_native_status_sample(
        self,
        status,
        *,
        phase_values,
        fixed_phases,
    ):
        """Versiegelt PM-Werte und feste Phase nur aus derselben RSCP-Probe."""

        sanitized = self._sanitize_measurement_status(status)
        sanitized = sanitized if isinstance(sanitized, dict) else {}
        raw_values = (
            tuple(phase_values)
            if isinstance(phase_values, (tuple, list))
            else ()
        )
        raw_complete = bool(
            len(raw_values) == 3
            and all(
                type(value) in (int, float)
                and math.isfinite(float(value))
                and abs(float(value)) <= MAX_PHASE_POWER_W
                for value in raw_values
            )
        )
        sanitized_values = tuple(
            sanitized.get(key)
            for key in (
                'phase_power_l1_w',
                'phase_power_l2_w',
                'phase_power_l3_w',
            )
        )
        sanitized_complete = bool(
            len(sanitized_values) == 3
            and all(
                type(value) in (int, float)
                and math.isfinite(float(value))
                and abs(float(value)) <= MAX_PHASE_POWER_W
                for value in sanitized_values
            )
        )
        try:
            fixed_value = int(fixed_phases)
        except (TypeError, ValueError, OverflowError):
            fixed_value = 0
        sample_fresh = bool(
            sanitized.get('wb_status_valid') is True
            and sanitized.get('driver_status_glitch') is False
            and sanitized.get('driver_status_plausible') is not False
            and sanitized.get('rscp_error_active') is False
            and sanitized.get('driver_status_stale') is not True
            and sanitized.get('driver_status_degraded') is not True
        )
        sample_valid = bool(
            raw_complete
            and sanitized_complete
            and fixed_value in (1, 3)
            and sample_fresh
        )
        sample_ts = time.time()
        with self.lock:
            self._native_status_sample_seq = int(
                getattr(self, '_native_status_sample_seq', 0) or 0
            ) + 1
            sample_seq = self._native_status_sample_seq

        sanitized.update({
            'phase_power_sample_valid': sample_valid,
            'native_fixed_phases': fixed_value if sample_valid else 0,
            'native_fixed_phases_valid': sample_valid,
            'native_fixed_phases_source': (
                'rscp_wb_number_phases_same_response'
                if sample_valid
                else ''
            ),
            'native_status_sample_seq': int(sample_seq),
            'native_status_sample_ts': float(sample_ts),
            'driver_status_valid': bool(sample_fresh),
            'driver_status_stale': False,
            'driver_status_degraded': not bool(sample_fresh),
            'driver_status_age_s': 0.0,
            'driver_status_reason': (
                'fresh'
                if sample_fresh
                else str(
                    sanitized.get('driver_status_glitch_reason')
                    or sanitized.get('wb_status_reason')
                    or sanitized.get('rscp_last_error')
                    or 'native_status_not_fresh'
                )
            ),
            'driver_status_last_sample_ts': float(sample_ts),
            'driver_status_last_ok_ts': (
                float(sample_ts)
                if sample_fresh
                else 0.0
            ),
            'driver_status_source': 'rscp_same_response',
            'e3dc_readback_sample_ts': float(sample_ts),
            'e3dc_readback_age_s': 0.0,
            'e3dc_readback_fresh': bool(sample_fresh),
            'e3dc_readback_source': 'rscp_same_response',
        })
        sanitized['e3dc_readback_contract'] = self._e3dc_readback_contract(
            sanitized,
            sample_ts=sample_ts,
            sample_fresh=sample_fresh,
            phase_power_sample_valid=sample_valid,
        )
        return sanitized

    def _heartbeat_loop(self):
        # Feldverifizierter Legacy-Pfad: Der externe RSCP-Rahmen muss zyklisch
        # erneuert werden. Eine belastbare öffentliche Herstellerangabe zur
        # exakten Frist liegt nicht vor; deshalb bleibt das 2-s-Intervall eine
        # konservative Betriebserfahrung und keine behauptete Protokollnorm.
        while True:
            time.sleep(2.0)
            self._heartbeat_once()

    def _heartbeat_preflight_locked(self):
        return True

    def _heartbeat_once(self):
        # Read, Lease-Prüfung und Wire bleiben unter derselben reentranten
        # Sperre wie STOP/Aus/Nullautorität.
        with self.lock:
            if self.external_suspended or self.last_amp is None:
                return False
            if not self._heartbeat_preflight_locked():
                return False
            heartbeat_amp = self.last_amp
            return bool(self._send_command_internal(
                heartbeat_amp,
                None,
                is_heartbeat=True,
            ))

    def _ensure_connected(self):
        if not (
            self.server_ip
            and 1 <= int(self.server_port) <= 65535
            and self.user
            and self.password
            and self.aes_password
        ):
            self._record_rscp_error("connect", "unvollständige lokale RSCP-Konfiguration")
            return False
        now = time.time()
        if self.conn is None or not getattr(self.conn, 'connected', False) or (now - self.last_connect_time > 300):
            try:
                from rscp_client import RscpConnection
                if self.conn and getattr(self.conn, 'connected', False):
                    self.conn.close()
                self.conn = RscpConnection(self.server_ip, self.server_port, self.aes_password)
                self.conn.connect()
                self.conn.authenticate(self.user, self.password)
                self.last_connect_time = now
                self._record_rscp_ok("connect")
            except Exception as e:
                self._record_rscp_error("connect", e)
                logger.error(f"[WB{self.wb_id}] RSCP Verbindungsfehler: {e}")
                return False
        return True

    def get_status(self):
        with self.lock:
            if not self._ensure_connected():
                return self._minimal_rscp_status()
        from rscp_client import (
            RscpTag,
            RscpType,
            decode_wb_extern_data_alg,
            validate_mirror_read_item,
        )

        # --- Anfrage 1: Leistung + Verbindungsstatus ---
        reqs = [
            {'tag': RscpTag.WB_INDEX,               'type': RscpType.UChar8, 'value': self.wb_index},
            {'tag': RscpTag.WB_REQ_PM_POWER_L1,     'type': RscpType.Nil,    'value': None},
            {'tag': RscpTag.WB_REQ_PM_POWER_L2,     'type': RscpType.Nil,    'value': None},
            {'tag': RscpTag.WB_REQ_PM_POWER_L3,     'type': RscpType.Nil,    'value': None},
            {'tag': RscpTag.WB_REQ_EXTERN_DATA_ALG, 'type': RscpType.Nil,    'value': None},
            {'tag': RscpTag.WB_REQ_NUMBER_PHASES,   'type': RscpType.Nil,    'value': None},
        ]

        # --- Anfrage 2: Session-Energie direkt aus E3DC-Firmware ---
        session_reqs = [
            {'tag': RscpTag.WB_INDEX,        'type': RscpType.UChar8, 'value': self.wb_index},
            {'tag': RscpTag.WB_REQ_SESSION,  'type': RscpType.UChar8, 'value': self.wb_index},
        ]

        try:
            req_frame = [
                {'tag': RscpTag.WB_REQ_DATA, 'type': RscpType.Container, 'value': reqs},
                {'tag': RscpTag.WB_REQ_DATA, 'type': RscpType.Container, 'value': session_reqs},
            ]
            with self.lock:
                if not self._ensure_connected():
                    return self._minimal_rscp_status()
                response = self.conn.request(req_frame)
            if not response:
                self._record_rscp_error("status", "empty response")
                return self._minimal_rscp_status()
            self._record_rscp_ok("status")

            status = {
                'car': 1, 'amp': 6, 'pha': 56, 'charging': None, 'real_power_w': 0,
                'car_connected_rscp': None,
                'plug_locked': None,
                'wb_status_valid': False,
                'wb_status_source': 'rscp_wb_extern_data_alg',
                'wb_status_reason': 'missing',
                'alg_seen': False,
                'alg_flags': 0,
                'alg_abort_or_disabled': None,
                'alg_charging': None,
                'alg_connected': None,
                'device_working': None,
                'extern_alg_hex': '',
                'session_kwh': None,
                'session_start_ts': None,
                'phase_power_rscp_type_codes': {},
                'phase_power_rscp_type_complete': False,
                'driver_variant': getattr(self, "driver_variant", "e3dc_native"),
                'rscp_wb_index': self.wb_index,
                'number_phases': 0,
                'connected_phases': 0,
                'phases_actual': 0,
                'phases_target': 0,
                'can_switch_phases': False,
                'phase_switch_capability': 'e3dc_native_fixed',
                'phase_switch_source': 'rscp_status',
                'api_surface': '',
            }
            p1 = p2 = p3 = 0.0
            phase_probe_values = ()
            fixed_phase_probe = 0

            for item in response:
                if item['tag'] == RscpTag.WB_DATA:
                    sub_list = item.get('value', [])
                    container_phase_values = {}
                    container_fixed_phases = 0
                    for sub in sub_list:
                        phase_name = {
                            RscpTag.WB_PM_POWER_L1: ('l1', 'WB_PM_POWER_L1'),
                            RscpTag.WB_PM_POWER_L2: ('l2', 'WB_PM_POWER_L2'),
                            RscpTag.WB_PM_POWER_L3: ('l3', 'WB_PM_POWER_L3'),
                        }.get(sub.get('tag'))
                        if phase_name is not None:
                            key, tag_name = phase_name
                            status['phase_power_rscp_type_codes'][key] = sub.get('type')
                            value, valid = _validated_e3dc_phase_power(
                                sub,
                                tag_name,
                                validate_mirror_read_item,
                            )
                            if valid:
                                container_phase_values[key] = value
                                if key == 'l1':
                                    p1 = value
                                elif key == 'l2':
                                    p2 = value
                                else:
                                    p3 = value
                        elif sub['tag'] == RscpTag.WB_NUMBER_PHASES and sub.get('value') is not None:
                            number_phases, valid = validate_mirror_read_item(sub, 'WB_NUMBER_PHASES')
                            if valid and number_phases in (1, 3):
                                container_fixed_phases = int(number_phases)
                                status['number_phases'] = number_phases
                                status['connected_phases'] = number_phases
                                status['phases_actual'] = number_phases
                                status['pha'] = 56 if number_phases == 3 else 8
                        elif sub['tag'] == RscpTag.WB_EXTERN_DATA_ALG:
                            decoded = decode_wb_extern_data_alg(sub, age_s=0.0)
                            self._observe_wbchar6_status(decoded)
                            status['wb_status_valid'] = decoded['valid']
                            status['wb_status_reason'] = decoded['reason']
                            status['plug_locked'] = decoded['plug_locked']
                            status['car_connected_rscp'] = decoded['plugged']
                            status['charging'] = decoded['charging']
                            status['alg_seen'] = decoded['valid']
                            status['alg_flags'] = decoded['flags'] or 0
                            status['alg_abort_or_disabled'] = decoded.get(
                                'abort_or_disabled'
                            )
                            status['extern_alg_hex'] = decoded.get('raw_hex', '')
                            status['alg_charging'] = decoded['charging']
                            status['alg_connected'] = decoded['plugged']
                            self.real_charging = bool(decoded['charging']) if decoded['valid'] else False
                            if decoded['plugged'] is True:
                                status['car'] = 2

                    if (
                        not phase_probe_values
                        and container_fixed_phases in (1, 3)
                        and set(container_phase_values) == {'l1', 'l2', 'l3'}
                    ):
                        phase_probe_values = (
                            container_phase_values['l1'],
                            container_phase_values['l2'],
                            container_phase_values['l3'],
                        )
                        fixed_phase_probe = container_fixed_phases

                    status['phase_power_rscp_type_complete'] = bool(
                        set(container_phase_values) == {'l1', 'l2', 'l3'}
                    )

                    # Session-Daten aus dem zweiten WB_DATA Container lesen
                    for sub in sub_list:
                        if sub['tag'] == RscpTag.WB_SESSION:
                            session_container, session_valid = validate_mirror_read_item(sub, 'WB_SESSION')
                            if session_valid:
                                for s in session_container:
                                    # WB_SESSION_CHARGED_ENERGY = 0x0E74102A (Wh)
                                    if s.get('tag') == RscpTag.WB_SESSION_CHARGED_ENERGY and s.get('value') is not None:
                                        value, valid = validate_mirror_read_item(s, 'WB_SESSION_CHARGED_ENERGY')
                                        if valid and value >= 0:
                                            status['session_kwh'] = round(value / 1000.0, 3)
                                    # WB_SESSION_START_TIME = 0x0E741026 (Unix Timestamp)
                                    elif s.get('tag') == RscpTag.WB_SESSION_START_TIME and s.get('value') is not None:
                                        value, valid = validate_mirror_read_item(s, 'WB_SESSION_START_TIME')
                                        if valid:
                                            status['session_start_ts'] = value

            raw_power_w = p1 + p2 + p3
            status['real_power_w'] = raw_power_w
            status['phase_power_l1_w'] = round(p1, 1)
            status['phase_power_l2_w'] = round(p2, 1)
            status['phase_power_l3_w'] = round(p3, 1)
            status['phase_power_sum_w'] = round(raw_power_w, 1)
            # E3DC/Multi Connect can report small residual or stale PM values
            # after an abort. Treat only meaningful load as charging; the
            # authoritative state still comes from WB_EXTERN_DATA_ALG.
            if status['charging'] is not True:
                status['real_power_w'] = 0.0
            self.real_charging = bool(status['charging'])
            active_phases = sum(1 for p in [p1, p2, p3] if p > 10)
            status['phases_in_use'] = int(active_phases)
            status['phase_power_verified'] = bool(status['charging'] and raw_power_w > 500 and active_phases >= 1)
            if active_phases == 1:
                status['pha'] = 8
            elif active_phases == 2:
                status['pha'] = 24
            elif active_phases >= 3:
                status['pha'] = 56
            if active_phases:
                status['phases_actual'] = int(active_phases)
            self._set_control_backend(status_valid=bool(status.get('wb_status_valid')))
            status.update(self._backend_contract_fields())
            status.update(self._rscp_diag_status())
            return self._finalize_native_status_sample(
                status,
                phase_values=phase_probe_values,
                fixed_phases=fixed_phase_probe,
            )
        except Exception as e:
            self._record_rscp_error("status", e)
            logger.error(f"[WB{self.wb_id}] getData Fehler: {e}")
            with self.lock:
                failed_conn = self.conn
                self.conn = None
                if failed_conn is not None:
                    try:
                        failed_conn.close()
                    except Exception:
                        pass
            return self._minimal_rscp_status()

    def _send_command_internal(self, target_amp, force_state, is_heartbeat=False):
        if not command_gate.allow_command(
            self,
            action="e3dc_set_extern",
            payload={"target_amp": target_amp, "force_state": force_state, "heartbeat": bool(is_heartbeat)},
            audit_allowed=not bool(is_heartbeat),
        ):
            return False
        if self.control_backend == E3DC_BACKEND_STATUS_ONLY and force_state != 1:
            self._record_rscp_error("set_extern", "status-only backend blocks normal command")
            return False
        with self.lock:
            self._wbchar6_dispatch_seq += 1
            dispatch_seq = int(self._wbchar6_dispatch_seq)
            self._wbchar6_last_dispatch_outcome = {
                'contract': 'e3dc_wbchar6_dispatch_outcome_v1',
                'seq': dispatch_seq,
                'state': 'not_attempted',
                'ts': time.time(),
                'target_amp': int(target_amp),
                'force_state': force_state,
                'heartbeat': bool(is_heartbeat),
            }
            if self.external_suspended:
                return False
            if not self._ensure_connected():
                return False
            from rscp_client import RscpTag, RscpType
            target_amp = int(max(
                0,
                min(target_amp, float(getattr(self, "max_amp", 16.0))),
            ))

            # E3DC-WBchar6-Mode-Semantik (feldverifizierter Legacy-Vertrag,
            # nicht öffentlich als Hersteller-API spezifiziert):
            # - Mode=1 übergibt einen Stromdeckel an den Sonnenmodus. Ein
            #   garantiert exakter Strom oder netzbezugsfreier Betrieb wird
            #   daraus nicht abgeleitet; der zentrale PCC-/Wh-Wächter bleibt
            #   autoritativ.
            # - Mode=2 übergibt den Stromdeckel an die externe Regelung.
            #   Python muss unerwünschten Netzbezug selbst verhindern.
            # self.sonnenmodus=True -> Mode=1 (PV-Only, Python setzt Ceiling)
            # self.sonnenmodus=False -> Mode=2 (Python steuert exakt, alle Quellen)
            mode = 1 if self.sonnenmodus else 2
            amp = max(6, target_amp)
            abort_flag = 0

            # Das E3DC abort_flag ist ein TOGGLE-Befehl! (Umschalter)
            # Wir feuern den Toggle NUR ab, wenn wir den physischen Zustand wirklich aendern muessen.
            if force_state == 1 or target_amp == 0:
                # Wir wollen STOPPEN.
                # E3DC nutzt hier einen Toggle, keinen absoluten Stop-Befehl.
                # Deshalb nie dauerhaft force_state=1 feuern. Bei unbekanntem
                # Zustand ist ein erzwungener Stop erlaubt, danach erst wieder
                # wenn der Status/die Leistung wirklich weiter Laden zeigt.
                amp = 6
                if self.real_charging and not is_heartbeat:
                    abort_flag = 1
                elif force_state == 1 and not is_heartbeat:
                    logger.debug(f"[WB{self.wb_id}] Stop-Toggle unterdrueckt: Wallbox meldet keine aktive Ladung.")
            elif target_amp > 0:
                # Wir wollen STARTEN/LADEN. Toggle (1) nur bei explizitem
                # Startimpuls senden. force_state=None ist ein reiner
                # Stromdeckel/Keepalive und darf nach einem weichen Stop
                # keine schlafende E3DC-Wallbox wieder anstossen.
                if self._wbchar6_start_toggle_allowed(force_state, is_heartbeat=is_heartbeat):
                    abort_flag = 1

            # Heartbeat-Calls haben force_state=None und dürfen nie toggeln.

            wbchar6 = bytearray(6)
            wbchar6[0] = mode
            wbchar6[1] = amp
            wbchar6[4] = abort_flag
            reqs = [
                {'tag': RscpTag.WB_EXTERN_DATA_LEN, 'type': RscpType.UChar8,    'value': 6},
                {'tag': RscpTag.WB_EXTERN_DATA,      'type': RscpType.ByteArray, 'value': bytes(wbchar6)},
            ]
            try:
                # Toggle nur anwenden, wenn es Sinn macht. Den realen Zustand
                # nicht optimistisch kippen: C++/Eba wertet den naechsten
                # WB_EXTERN_DATA_ALG-Status aus. Ein vorweggenommenes Toggle
                # erzeugt bei Multi Connect Phantom-"laedt"-Zustaende.
                if abort_flag == 1:
                    logger.debug(f"[WB{self.wb_id}] Sende E3DC Toggle (Mode={mode}, Amp={amp}) um neuen Status zu erzwingen.")

                req_frame = [{'tag': RscpTag.WB_REQ_DATA, 'type': RscpType.Container, 'value': [
                    {'tag': RscpTag.WB_INDEX,       'type': RscpType.UChar8,    'value': self.wb_index},
                    {'tag': RscpTag.WB_REQ_SET_EXTERN, 'type': RscpType.Container, 'value': reqs},
                ]}]
                if not command_gate.allow_command(
                    self,
                    action="e3dc_set_extern_wire",
                    payload={"target_amp": target_amp, "force_state": force_state, "heartbeat": bool(is_heartbeat)},
                    audit_allowed=False,
                ):
                    return False
                if self.external_suspended:
                    return False
                self._wbchar6_last_dispatch_outcome.update({
                    'state': 'attempted_ambiguous',
                    'wire_attempt_ts': time.time(),
                })
                self.conn.request(req_frame)
                receipt_ts = time.time()
                self._wbchar6_output_seq += 1
                self._wbchar6_last_wire_receipt = {
                    'contract': 'e3dc_wbchar6_wire_receipt_v1',
                    'seq': int(self._wbchar6_output_seq),
                    'ts': receipt_ts,
                    'requested_amp': int(target_amp),
                    'wire_amp': int(amp),
                    'mode': int(mode),
                    'post_guard_force_state': force_state,
                    'abort_flag': int(abort_flag),
                    'heartbeat': bool(is_heartbeat),
                    'stop_edge_issued': bool(
                        abort_flag == 1
                        and force_state == 1
                        and not is_heartbeat
                    ),
                }
                self._wbchar6_last_dispatch_outcome.update({
                    'state': 'confirmed',
                    'wire_receipt_seq': int(self._wbchar6_output_seq),
                    'wire_receipt_ts': receipt_ts,
                })
                if self._wbchar6_last_wire_receipt['stop_edge_issued']:
                    self.last_stop_toggle_ts = receipt_ts
                if abort_flag == 1 and force_state == 2:
                    self._mark_wbchar6_start_toggle_sent()
                self._record_rscp_ok("set_extern")
                return True
            except Exception as e:
                self._record_rscp_error("set_extern", e)
                logger.error(f"[WB{self.wb_id}] setAmp Fehler: {e}")
                failed_conn = self.conn
                self.conn = None
                if failed_conn is not None:
                    try:
                        failed_conn.close()
                    except Exception as close_error:
                        logger.debug(
                            f"[WB{self.wb_id}] RSCP close nach SET_EXTERN-Fehler "
                            f"fehlgeschlagen: {close_error}"
                        )
                return False

    def release_to_e3dc(self, max_amp=16):
        """
        Echte Freigabe für wb_mode=0: keine SET_EXTERN-Kommandos mehr senden.
        Der bisherige "Sonnenmodus-Heartbeat" hielt die E3DC-Wallbox weiter in
        externer RSCP-Kontrolle. Einige Anlagen laden dann erst wieder, wenn der
        Pi offline ist. Mode 0 bedeutet deshalb ab jetzt wirklich Python stumm.
        """
        self.suspend_external_control("Mode 0 / E3DC autonom")
        return True

    def release_to_default(self, max_amp=16):
        return self.release_to_e3dc(max_amp=max_amp)

    def _set_e3dc_max_charge_current(self, max_amp=16):
        """Sperrt den persistenten, typinkonsistenten Maximalstrom-Setter hart."""
        logger.error(
            f"[WB{self.wb_id}] Direkter E3DC-Maximalstrom ist typ-/EEPROM-gesperrt; kein RSCP-Write."
        )
        return False

    def suspend_external_control(self, reason="Python stumm"):
        """Stoppt den E3DC-SET_EXTERN-Heartbeat und schliesst die RSCP-Verbindung."""
        with self.lock:
            if not self.external_suspended:
                logger.info(f"[WB{self.wb_id}] SET_EXTERN-Heartbeat gestoppt ({reason}).")
            self._control_generation += 1
            self.external_suspended = True
            self.sonnenmodus = False
            self.last_amp = None
            self.last_force_state = None
            failed_conn = self.conn
            self.conn = None
            if failed_conn is not None:
                try:
                    failed_conn.close()
                except Exception:
                    pass

    def set_amp_sonnenmodus(self, target_amp, force_state=None):
        """
        Setzt einen feldverifizierten Stromdeckel in Mode=1 (Sonnenmodus).
        Verwendet für Python-gesteuerte PV-Ladung (wb_mode 1-10 mit Budget-Signal).

        Unterschied zu release_to_e3dc():
        - Python bleibt aktiv und setzt den Ceiling-Amp regelmäßig (alle 2-4s)
        - E3DC erhält target_amp als Legacy-Stromdeckel; der Manager überwacht
          den Netzpunkt weiterhin selbst
        - Start/Stop via force_state=2 (Start-Toggle) / force_state=1 (Stop-Toggle)
        - force_state=None setzt nur den Stromdeckel und sendet keinen Toggle

        Unterschied zu set_amp_and_state() (Mode=2):
        - Mode=1 ersetzt keinen zentralen PCC-/Wh-Schutz
        """
        request_generation = self._control_generation
        if not command_gate.allow_command(
            self,
            action="e3dc_set_amp_sonnenmodus",
            payload={"target_amp": target_amp, "force_state": force_state},
            audit_allowed=False,
        ):
            return False
        with self.lock:
            if request_generation != self._control_generation:
                return False
            previous_control_state = (
                self.external_suspended,
                self.sonnenmodus,
                self.last_amp,
                self.last_force_state,
            )
            bounded_amp = (
                0
                if float(target_amp or 0.0) < 0.5
                else max(
                    6,
                    min(
                        float(getattr(self, "max_amp", 16.0)),
                        float(target_amp),
                    ),
                )
            )
            self.external_suspended = False
            self.sonnenmodus = True
            ok = False
            try:
                ok = bool(
                    self._send_command_internal(
                        bounded_amp,
                        force_state,
                        is_heartbeat=False,
                    )
                )
            finally:
                if ok:
                    # Erst ein angenommener RSCP-Rahmen darf den freien
                    # Heartbeat auf diesen Sollwert scharf schalten.
                    self.last_amp = bounded_amp
                    self.last_force_state = force_state
                else:
                    (
                        self.external_suspended,
                        self.sonnenmodus,
                        self.last_amp,
                        self.last_force_state,
                    ) = previous_control_state
            return ok

    def take_control(self):
        """Python uebernimmt aktive Steuerung (Netzmodus Mode=2)."""
        request_generation = self._control_generation
        with self.lock:
            if request_generation != self._control_generation:
                return False
            self.external_suspended = False
            if self.sonnenmodus:
                logger.info(f"[WB{self.wb_id}] Python uebernimmt Steuerung (Netzmodus Mode=2)")
                self.sonnenmodus = False

    def emergency_stop(self):
        """
        Notabschaltung: Python uebernimmt sofort die Kontrolle (Mode=2)
        und sendet genau einen harten Stopimpuls.
        Unterschied zu set_amp_and_state(0, 1):
        - Bricht auch den Sonnenmodus (Mode=1) auf
        - Der Heartbeat erneuert danach nur den angenommenen 0-A-Intent ohne
          weiteren Toggle; die physische Stopbestätigung kommt aus dem Readback
        """
        if not command_gate.allow_command(
            self,
            action="e3dc_emergency_stop",
            payload={"target_amp": 0, "force_state": 1},
        ):
            return False
        logger.warning(f"[WB{self.wb_id}] emergency_stop(): Unterbreche Sonnenmodus, erzwinge Mode=2 STOP.")
        with self.lock:
            if self.real_charging is not True:
                return False
            self.external_suspended = False
            self.sonnenmodus = False       # Sonnenmodus beenden - Python übernimmt
            ok = False
            try:
                with command_gate.emergency_stop_scope(self):
                    ok = bool(
                        self._send_command_internal(
                            0,
                            1,
                            is_heartbeat=False,
                        )
                    )
            finally:
                if not ok:
                    # Ein fehlgeschlagener oder ambiger Not-Stopp darf keinen
                    # nachlaufenden Heartbeat mit einem unbestätigten Zustand
                    # scharf schalten.
                    self.suspend_external_control(
                        "Emergency-STOP ohne bestätigten RSCP-Ausgang"
                    )
            if not ok:
                return False
            self.last_amp = 0
            self.last_force_state = None
            return True

    def set_amp_and_state(self, target_amp, force_state=None):
        request_generation = self._control_generation
        if not command_gate.allow_command(
            self,
            action="e3dc_set_amp_and_state",
            payload={"target_amp": target_amp, "force_state": force_state},
            audit_allowed=False,
        ):
            return False
        with self.lock:
            if request_generation != self._control_generation:
                return False
            previous_control_state = (
                self.external_suspended,
                self.sonnenmodus,
                self.last_amp,
                self.last_force_state,
            )
            bounded_amp = (
                0
                if float(target_amp or 0.0) < 0.5
                else max(
                    6,
                    min(
                        float(getattr(self, "max_amp", 16.0)),
                        float(target_amp),
                    ),
                )
            )
            self.external_suspended = False
            ok = False
            try:
                ok = bool(
                    self._send_command_internal(
                        bounded_amp,
                        force_state,
                        is_heartbeat=False,
                    )
                )
            finally:
                if ok:
                    self.last_amp = bounded_amp
                    self.last_force_state = force_state
                else:
                    (
                        self.external_suspended,
                        self.sonnenmodus,
                        self.last_amp,
                        self.last_force_state,
                    ) = previous_control_state
            return ok


# ===========================================================================
# E3DCMultiConnectCharger (Multi Connect I/II per direkten RSCP-Tags)
# ===========================================================================
class E3DCMultiConnectCharger(E3DCCharger):
    """Gemeinsamer E3/DC-Wallboxtransport mit fähigkeitsgebundenen Übergängen.

    Eine gültige ALG-/Index-Antwort belegt den gemeinsamen E3/DC-Transport,
    aber keine Produktfamilie. Direkte Sun-/Auto-/Abort-Schreibzugriffe sind
    nicht freigegeben; die zugehörigen Felder dienen nur dem Readback. Dynamik
    und abgesicherter Start bleiben im flüchtigen WBchar6-/SET_EXTERN-Rahmen.
    """

    def __init__(self, ip, wb_id, config):
        super().__init__(ip, wb_id, config)
        self.driver_variant = (
            "e3dc_multi_connect"
            if self.device_family in {"multi_connect", "multi_connect_ii"}
            else "e3dc_rscp"
        )
        self.device_name = ""
        self.firmware_version = ""
        self._direct_checked = False
        self._direct_supported = False
        self._last_alg_flags = 0
        self._last_start_toggle_ts = 0.0
        self._last_param_current = None
        self._last_param_current_ts = 0.0
        self._last_extern_mode = None
        self._last_extern_amp = None
        self._last_extern_mode_ts = 0.0
        self._last_abort = None
        self._last_abort_ts = 0.0
        self._last_sun_mode = None
        self._last_sun_mode_ts = 0.0
        self._last_auto_phase = None
        self._last_auto_phase_ts = 0.0
        self._keepalive_interval_s = 10.0
        self._direct_index_checked = False
        self._detected_wb_index = self.wb_index
        self._transition_baseline = None
        self._transition_confirmed = {}
        self._transition_changed_by_us = set()
        self._transition_last_requested = {}
        self._transition_readback_ts = 0.0
        self._transition_write_performed = False
        self._release_attempted = False
        self.release_incomplete = False
        self.release_incomplete_reason = ""

    def _configured_wb_index(self):
        for key in (
            f"wb{self.wb_id}_rscp_index",
            f"wb{self.wb_id}_multi_index",
            "wb_native_rscp_index" if int(self.wb_id or 1) == 1 else "wb_native_rscp_index2",
        ):
            raw = self.config.get(key)
            if raw in (None, ""):
                continue
            try:
                return max(0, min(15, int(raw)))
            except (TypeError, ValueError):
                continue
        return None

    def _candidate_wb_indices(self):
        candidates = []
        configured = self._configured_wb_index()
        for candidate in (
            configured,
            self.wb_index,
            int(self.wb_id or 1),
            0,
            1,
            2,
            3,
        ):
            if candidate is None:
                continue
            try:
                idx = int(candidate)
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx in candidates:
                continue
            candidates.append(idx)
        return candidates

    def _wb_request(self, reqs, wb_index=None, *, write_action=None, write_payload=None):
        with self.lock:
            if not self._ensure_connected():
                return None
            from rscp_client import RscpTag, RscpType
            index = self.wb_index if wb_index is None else int(wb_index)
            frame = [{'tag': RscpTag.WB_REQ_DATA, 'type': RscpType.Container, 'value': [
                {'tag': RscpTag.WB_INDEX, 'type': RscpType.UChar8, 'value': index},
                *reqs,
            ]}]
            try:
                if write_action and not command_gate.allow_command(
                    self,
                    action=str(write_action),
                    payload=write_payload,
                    audit_allowed=False,
                ):
                    return None
                response = self.conn.request(frame)
                self._record_rscp_ok("wb_request")
                return response
            except Exception as e:
                self._record_rscp_error("wb_request", e)
                logger.error(f"[WB{self.wb_id}] Multi Connect RSCP Fehler: {e}")
                failed_conn = self.conn
                self.conn = None
                if failed_conn is not None:
                    try:
                        failed_conn.close()
                    except Exception as close_error:
                        logger.debug(
                            f"[WB{self.wb_id}] Multi Connect RSCP close nach "
                            f"Requestfehler fehlgeschlagen: {close_error}"
                        )
                return None

    @staticmethod
    def _iter_wb_items(response):
        from rscp_client import RscpTag
        if not response:
            return
        for item in response:
            if item.get('tag') != RscpTag.WB_DATA:
                continue
            for sub in item.get('value', []) or []:
                yield sub

    @staticmethod
    def _extract_extern_bytes(sub, expected_tag=None):
        from rscp_client import RscpTag
        if expected_tag is not None and sub.get('tag') != expected_tag:
            return None
        val = sub.get('value')
        if isinstance(val, (bytes, bytearray)):
            return bytes(val)
        if isinstance(val, list):
            for data_sub in val:
                if data_sub.get('tag') == RscpTag.WB_EXTERN_DATA:
                    b = data_sub.get('value')
                    if isinstance(b, (bytes, bytearray)):
                        return bytes(b)
        return None

    @staticmethod
    def _extract_alg_bytes(sub):
        from rscp_client import RscpTag
        return E3DCMultiConnectCharger._extract_extern_bytes(sub, RscpTag.WB_EXTERN_DATA_ALG)

    def _read_direct_info(self, wb_index=None):
        from rscp_client import RscpTag, RscpType, validate_mirror_read_item
        index = self.wb_index if wb_index is None else int(wb_index)
        response = self._wb_request([
            {'tag': RscpTag.WB_REQ_DEVICE_NAME,              'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_FIRMWARE_VERSION,         'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_PARAM_1,                  'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_PARAM_2,                  'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_EXTERN_DATA_ALG,          'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_NUMBER_PHASES,            'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_SUN_MODE_ACTIVE,          'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_AUTO_PHASE_SWITCH_ENABLED,'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_ABORT_CHARGING,            'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_WALLBOX_TYPE,             'type': RscpType.Nil, 'value': None},
        ], wb_index=index)
        info = {}
        for sub in self._iter_wb_items(response):
            tag = sub.get('tag')
            if tag == RscpTag.WB_DEVICE_NAME:
                val, valid = validate_mirror_read_item(sub, 'WB_DEVICE_NAME')
                if valid:
                    info['device_name'] = val
            elif tag == RscpTag.WB_FIRMWARE_VERSION:
                val, valid = validate_mirror_read_item(sub, 'WB_FIRMWARE_VERSION')
                if valid:
                    info['firmware_version'] = val
            elif tag == RscpTag.WB_NUMBER_PHASES:
                val, valid = validate_mirror_read_item(sub, 'WB_NUMBER_PHASES')
                if valid and val in (1, 3):
                    info['number_phases'] = val
            elif tag == RscpTag.WB_SUN_MODE_ACTIVE:
                val, valid = validate_mirror_read_item(sub, 'WB_SUN_MODE_ACTIVE')
                if valid:
                    info['sun_mode_active'] = val
            elif tag == RscpTag.WB_AUTO_PHASE_SWITCH_ENABLED:
                val, valid = validate_mirror_read_item(sub, 'WB_AUTO_PHASE_SWITCH_ENABLED')
                if valid:
                    info['auto_phase_switch_enabled'] = val
            elif tag == RscpTag.WB_ABORT_CHARGING:
                val, valid = validate_mirror_read_item(sub, 'WB_ABORT_CHARGING')
                if valid:
                    info['abort_charging'] = val
            elif tag == RscpTag.WB_WALLBOX_TYPE:
                val, valid = validate_mirror_read_item(sub, 'WB_WALLBOX_TYPE')
                if valid:
                    info['wallbox_type'] = val
            elif tag == RscpTag.WB_RSP_PARAM_1:
                param = self._extract_extern_bytes(sub, RscpTag.WB_RSP_PARAM_1)
                if param is not None:
                    info['param1'] = param
                    if len(param) >= 3:
                        info['param_current'] = int(param[2])
                        info['param1_byte2_raw'] = int(param[2])
                        info['param1_byte2_semantics'] = (
                            'unconfirmed_raw_byte_not_current_authority'
                        )
            elif tag == RscpTag.WB_RSP_PARAM_2:
                param = self._extract_extern_bytes(sub, RscpTag.WB_RSP_PARAM_2)
                if param is not None:
                    info['param2'] = param
            else:
                alg = self._extract_alg_bytes(sub)
                if alg is not None:
                    info['extern_alg'] = alg
        self.device_name = info.get('device_name', self.device_name)
        self.firmware_version = info.get('firmware_version', self.firmware_version)
        info['readback_ts'] = time.time()
        return info

    def _read_transition_state(self):
        info = self._read_direct_info()
        state = {
            'sun_mode': info.get('sun_mode_active'),
            'auto_phase': info.get('auto_phase_switch_enabled'),
            'abort': info.get('abort_charging'),
            'phases': info.get('number_phases'),
        }
        self._transition_readback_ts = float(info.get('readback_ts') or 0.0)
        self._transition_confirmed.update({k: v for k, v in state.items() if v is not None})
        complete = all(type(state.get(name)) is bool for name in ('sun_mode', 'auto_phase', 'abort'))
        self._set_control_backend(
            status_valid=bool(info.get('extern_alg')),
            transition_capable=complete,
            readback_ts=self._transition_readback_ts if complete else 0.0,
        )
        return state

    def _capture_transition_baseline(self):
        """Direct Sun/Auto/Abort ownership is not released in Stable."""
        return False

    def _clear_transition_ownership_episode(self):
        self._transition_baseline = None
        self._transition_confirmed.clear()
        self._transition_changed_by_us.clear()
        self._transition_last_requested.clear()
        self._transition_write_performed = False

    def _transition_write_one(self, field, desired):
        """Sperrt fehlersicher: Direkte E3/DC-Transitionsschreibvorgänge sind nicht freigegeben."""
        _ = (field, desired)
        return False

    def _restore_transition_state_once(self):
        """Bleibt wirkungslos, da Stable nie direkten Übergangszustand besitzt."""
        self._release_attempted = True
        self._clear_transition_ownership_episode()
        return True

    @staticmethod
    def _direct_info_score(info):
        if not isinstance(info, dict):
            return 0
        score = 0
        alg = info.get('extern_alg')
        if alg and len(alg) >= 3:
            score += 80
        if info.get('wallbox_type') is not None:
            score += 40
        if info.get('param_current', 0) > 0:
            score += 20
        if info.get('number_phases') in (1, 3):
            score += 15
        if info.get('device_name'):
            score += 5
        return score

    @staticmethod
    def _direct_info_supported(info):
        """Meldet die direkte Übergangs-Readback-Fähigkeit, niemals ein Gerätemodell."""
        if not isinstance(info, dict):
            return False
        return bool(
            info.get('extern_alg')
            and all(type(info.get(name)) is bool for name in (
                'sun_mode_active', 'auto_phase_switch_enabled', 'abort_charging'
            ))
        )

    def _resolve_wb_index(self, force=False):
        if self._direct_index_checked and not force:
            return self.wb_index
        self._direct_index_checked = True
        best_idx = self.wb_index
        best_info = None
        best_score = -1
        try:
            for idx in self._candidate_wb_indices():
                info = self._read_direct_info(wb_index=idx)
                score = self._direct_info_score(info)
                if score > best_score:
                    best_idx = idx
                    best_info = info
                    best_score = score
                if self._direct_info_supported(info) and score >= 80:
                    break
            if best_score > 0:
                previous = self.wb_index
                self.wb_index = best_idx
                self._detected_wb_index = best_idx
                self._direct_supported = self._direct_info_supported(best_info)
                if isinstance(best_info, dict):
                    self._update_device_identity(
                        device_name=best_info.get('device_name'),
                        wallbox_type=best_info.get('wallbox_type'),
                    )
                if self.wb_index != previous:
                    logger.info(
                        f"[WB{self.wb_id}] Multi Connect RSCP-Index erkannt: "
                        f"{self.wb_index} (statt {previous})"
                    )
            else:
                self._direct_supported = False
        except Exception as e:
            logger.debug(f"[WB{self.wb_id}] E3DC Multi-Connect Index-Probe fehlgeschlagen: {e}")
            self._direct_supported = False
        return self.wb_index

    def is_direct_supported(self):
        """Read-only-Prüfung für die automatische Erkennung."""
        if not self._direct_checked:
            self._direct_checked = True
            self._resolve_wb_index(force=True)
        return self._direct_supported

    def _set_extern_mode(self, mode, amp=None, force=False, toggle=False):
        mode = 1 if int(mode) == 1 else 2
        amp = int(max(
            6,
            min(
                float(getattr(self, "max_amp", 16.0)),
                amp or self.last_amp or 6,
            ),
        ))
        now = time.time()
        if (
            not force
            and self._last_extern_mode == mode
            and self._last_extern_amp == amp
            and not toggle
            and now - self._last_extern_mode_ts < self._keepalive_interval_s
        ):
            return True
        ok = self._set_extern_cxx(mode, amp, toggle=toggle)
        if ok:
            self._last_extern_mode = mode
            self._last_extern_amp = amp
            self._last_extern_mode_ts = now
        return ok

    def _set_sun_mode(self, active, amp=None, force=False):
        active = bool(active)
        ok = self._transition_write_one('sun_mode', active)
        if ok:
            self.sonnenmodus = active
            self._last_sun_mode = active
        return ok

    def _set_auto_phase_switch(self, active, force=False):
        active = bool(active)
        ok = self._transition_write_one('auto_phase', active)
        if ok:
            self._last_auto_phase = active
        return ok

    def _efy_autonomous_solar_handoff_contract(self):
        """Prüft nur Identität, Protokollfähigkeit und ALG-Freshness.

        Diese Freigabe erteilt ausdrücklich keine Berechtigung für direkte
        Phasen-/Transition-Schreibzugriffe. Sie wählt nur den vorhandenen
        WBchar6-Sonnenmodus, in dem die efy ihre herstellereigene 1p-/3p-
        Automatik selbst ausführt.
        """

        max_age_s = 15.0
        try:
            max_age_s = max(
                1.0,
                float(
                    self.config.get(
                        "e3dc_wbchar6_status_max_age_s",
                        15.0,
                    )
                ),
            )
        except (TypeError, ValueError):
            pass
        if not math.isfinite(max_age_s):
            max_age_s = 15.0
        now = time.time()
        try:
            readback_ts = float(self._wbchar6_readback_ts or 0.0)
        except (TypeError, ValueError):
            readback_ts = 0.0
        readback_finite = math.isfinite(readback_ts)
        age_s = (
            max(0.0, now - readback_ts)
            if readback_finite and readback_ts > 0.0
            else None
        )
        blocker = ""
        if self.device_family != "efy":
            blocker = "device_family_not_efy"
        elif self.device_family_source not in {"configured", "configured_type"}:
            blocker = "device_family_not_explicitly_configured"
        elif not self.efy_autonomous_wbchar6_verified:
            blocker = "field_verified_wbchar6_capability_missing"
        elif self.control_backend != E3DC_BACKEND_WBCHAR6:
            blocker = "wbchar6_compat_not_bound"
        elif not readback_finite:
            blocker = "fresh_alg_status_invalid"
        elif readback_ts <= 0.0:
            blocker = "fresh_alg_status_missing"
        elif age_s is None or age_s > max_age_s:
            blocker = "fresh_alg_status_expired"
        return {
            "contract": "e3dc_efy_autonomous_solar_driver_v1",
            "allowed": not blocker,
            "blocker": blocker,
            "family_source": str(self.device_family_source or ""),
            "provenance": (
                "field_verified_legacy"
                if self.efy_autonomous_wbchar6_verified
                else "unverified"
            ),
            "backend": str(self.control_backend or ""),
            "readback_age_s": age_s,
            "max_age_s": float(max_age_s),
            "direct_phase_write_allowed": False,
        }

    def _efy_autonomous_solar_handoff_ready(self):
        return bool(
            self._efy_autonomous_solar_handoff_contract().get("allowed")
        )

    def _heartbeat_preflight_locked(self):
        if not self.sonnenmodus:
            return True
        contract = self._efy_autonomous_solar_handoff_contract()
        if contract.get("allowed") is True:
            return True
        blocker = str(
            contract.get("blocker")
            or "autonomous_heartbeat_contract_not_ready"
        )
        self.external_suspended = True
        self.last_amp = None
        self.last_force_state = None
        self._control_generation += 1
        self._efy_autonomous_handoff_blocker = "heartbeat_%s" % blocker
        self._efy_autonomous_handoff_ts = time.time()
        logger.warning(
            "[WB%s] efy-Mode-1-Heartbeat ohne Ausgang entwaffnet: %s",
            self.wb_id,
            blocker,
        )
        return False

    def _ensure_control_defaults(self, force=False):
        # evcc-Referenz für Multi Connect: externe Regelung arbeitet nur
        # sauber, wenn Sonnenmodus und automatische Phasenumschaltung aus sind.
        ok = self._set_sun_mode(False, force=force)
        if self._transition_write_performed:
            return ok
        ok = self._set_auto_phase_switch(False, force=force) and ok
        return ok

    def _set_abort(self, abort, force=False):
        abort = bool(abort)
        ok = self._transition_write_one('abort', abort)
        if ok:
            self._last_abort = abort
        return ok

    def _set_max_current(self, amp, force=False):
        logger.error(f"[WB{self.wb_id}] Direkter Maximalstrom ist typ-/EEPROM-gesperrt.")
        return False

    def _clear_phantom_state(self):
        """Multi Connect beruhigen, ohne Start/Stop-Toggle.

        Nach einem abgewiesenen Start kann WB_EXTERN_DATA_ALG z.B. 0x88
        melden: Fahrzeug verbunden + Sonnenmodus, aber keine echte Ladung.
        Ein weiterer Toggle wuerde diesen Schattenzustand wieder anfachen.
        0x48 ist dagegen laut Multi-Connect-Diagnose ein normaler Zustand:
        verbunden, aber laedt nicht. Den lassen wir unangetastet.
        """
        flags = int(self._last_alg_flags or 0)
        if not (flags & 0x80):
            return True
        ok = self._set_sun_mode(False)
        self.sonnenmodus = False
        self._last_extern_mode = None
        self._last_extern_amp = None
        return ok

    def _send_charge_toggle(self):
        mode = 1 if self.sonnenmodus else 2
        amp = int(max(
            6,
            min(
                float(getattr(self, "max_amp", 16.0)),
                self.last_amp or self._last_param_current or 6,
            ),
        ))
        logger.debug(f"[WB{self.wb_id}] Multi Connect SET_EXTERN Toggle=1")
        return self._set_extern_cxx(mode, amp, toggle=True)

    def _set_extern_cxx(self, mode, amp, toggle=False):
        """C++-konforme Multi-Connect-Freigabe über WBchar6/SET_EXTERN.

        Die direkten Multi-Tags liefern saubere Statuswerte und setzen den
        Stromdeckel, starten bestimmte Multi-Connect-Installationen aber nicht
        zuverlässig. Das alte C++-Programm nutzt für Start/Stop WBchar6:
        Byte 0 = Modus (1 Sonnenmodus, 2 Misch/Netz), Byte 1 = Ampere,
        Byte 4 = Toggle. Der Toggle darf nur als Impuls gesendet werden.
        """
        if not command_gate.allow_command(
            self,
            action="e3dc_multi_set_extern_cxx",
            payload={"mode": mode, "amp": amp, "toggle": bool(toggle)},
            audit_allowed=False,
        ):
            return False
        from rscp_client import RscpTag, RscpType
        mode = 1 if int(mode) == 1 else 2
        amp = int(max(
            6,
            min(float(getattr(self, "max_amp", 16.0)), amp or 6),
        ))
        wbchar6 = bytearray(6)
        wbchar6[0] = mode
        wbchar6[1] = amp
        wbchar6[4] = 1 if toggle else 0
        reqs = [
            {'tag': RscpTag.WB_REQ_SET_EXTERN, 'type': RscpType.Container, 'value': [
                {'tag': RscpTag.WB_EXTERN_DATA_LEN, 'type': RscpType.UChar8, 'value': 6},
                {'tag': RscpTag.WB_EXTERN_DATA, 'type': RscpType.ByteArray, 'value': bytes(wbchar6)},
            ]},
        ]
        logger.debug(
            f"[WB{self.wb_id}] Multi Connect SET_EXTERN Mode={mode} Amp={amp} Toggle={int(toggle)}"
        )
        return self._wb_request(
            reqs,
            write_action="e3dc_multi_set_extern_cxx_wire",
            write_payload={"mode": mode, "amp": amp, "toggle": bool(toggle)},
        ) is not None

    def _needs_start_toggle(self, force_state=None):
        return self._wbchar6_start_toggle_allowed(force_state, is_heartbeat=False)

    def _needs_stop_toggle(self):
        flags = int(self._last_alg_flags or 0)
        return self.real_charging or bool(flags & (0x10 | 0x20))

    def _send_command_internal(self, target_amp, force_state=None, is_heartbeat=False):
        """Führt exakt ein ausgewähltes Backend aus; versucht nie ein anderes."""
        if not command_gate.allow_command(
            self,
            action="e3dc_multi_send_command",
            payload={"target_amp": target_amp, "force_state": force_state, "heartbeat": bool(is_heartbeat)},
            audit_allowed=False,
        ):
            return False
        with self.lock:
            if self.external_suspended:
                return False
            backend = self.control_backend
            if backend == E3DC_BACKEND_WBCHAR6:
                return super()._send_command_internal(target_amp, force_state, is_heartbeat=is_heartbeat)
            self._record_rscp_error(
                "e3dc_backend",
                "status-only backend blocks normal command; direct transitions are no-send",
            )
            return False

    def get_status(self):
        from rscp_client import (
            RscpTag,
            RscpType,
            decode_wb_extern_data_alg,
            validate_mirror_read_item,
        )
        self._resolve_wb_index()
        response = self._wb_request([
            {'tag': RscpTag.WB_REQ_PM_POWER_L1,       'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_PM_POWER_L2,       'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_PM_POWER_L3,       'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_PARAM_1,           'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_PARAM_2,           'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_EXTERN_DATA_ALG,   'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_DEVICE_NAME,       'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_FIRMWARE_VERSION,  'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_NUMBER_PHASES,     'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_AUTO_PHASE_SWITCH_ENABLED,'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_SUN_MODE_ACTIVE,   'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_ABORT_CHARGING,    'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_WALLBOX_TYPE,      'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_SESSION,           'type': RscpType.UChar8, 'value': self.wb_index},
        ])
        if not response:
            self._record_rscp_error("status", "empty response")
            return self._minimal_rscp_status()

        status = {
            'car': 1, 'amp': 6, 'pha': 56, 'charging': None, 'real_power_w': 0.0,
            'car_connected_rscp': None,
            'plug_locked': None,
            'wb_status_valid': False,
            'wb_status_source': 'rscp_wb_extern_data_alg',
            'wb_status_reason': 'missing',
            'alg_seen': False,
            'alg_flags': 0,
            'alg_charging': None,
            'alg_connected': None,
            'device_working': None,
            'driver_variant': self.driver_variant,
            'rscp_wb_index': self.wb_index,
            'device_name': self.device_name,
            'device_name_source': '',
            'firmware_version': self.firmware_version,
            'firmware_version_source': '',
            'enabled': None,
            'extern_alg_hex': '',
            'param1_hex': '',
            'param2_hex': '',
            'param_current': None,
            'param_current_confirmed': False,
            'param1_byte2_raw': None,
            'param1_byte2_semantics': 'unconfirmed_raw_byte_not_current_authority',
            'amp_readback_confirmed': False,
            'amp_source': 'rscp_param1_byte2_unconfirmed_legacy_projection',
            'wallbox_type': None,
            'wallbox_type_semantics': 'raw_unclassified_numeric_readback',
            'wallbox_type_family_inferred': False,
            'abort_charging': None,
            'abort_charging_source': '',
            'session_kwh': None,
            'session_start_ts': None,
            'phase_power_l1_w': 0.0,
            'phase_power_l2_w': 0.0,
            'phase_power_l3_w': 0.0,
            'phase_power_sum_w': 0.0,
            'phase_power_verified': False,
            'phase_power_rscp_type_codes': {},
            'phase_power_rscp_type_complete': False,
            'phases_in_use': 0,
            'phases_actual': 0,
            'phases_target': 0,
            'number_phases': 0,
            'number_phases_source': '',
            'number_phases_semantics': (
                'current_configuration_not_hardware_wiring_or_switch_capability'
            ),
            'number_phases_hardware_capability_inferred': False,
            'connected_phases': 0,
            'auto_phase_switch_enabled': None,
            'auto_phase_switch_source': '',
            'sun_mode_active': None,
            'sun_mode_source': '',
            'can_switch_phases': False,
            'phase_switch_capability': 'e3dc_multi_connect_cp_480_unverified',
            'phase_switch_source': 'disabled_by_hardware_protection',
            'api_surface': '',
        }
        status.update(self._rscp_diag_status())
        p1 = p2 = p3 = 0.0
        alg = None
        param_current = None
        phase_probe_values = ()
        fixed_phase_probe = 0

        for item in response:
            if item.get('tag') != RscpTag.WB_DATA:
                continue
            container_phase_values = {}
            container_fixed_phases = 0
            for sub in item.get('value', []) or []:
                tag = sub.get('tag')
                value = sub.get('value')
                phase_key = {
                    RscpTag.WB_PM_POWER_L1: 'l1',
                    RscpTag.WB_PM_POWER_L2: 'l2',
                    RscpTag.WB_PM_POWER_L3: 'l3',
                }.get(tag)
                if phase_key is not None:
                    tag_name = 'WB_PM_POWER_L%s' % phase_key[-1]
                    status['phase_power_rscp_type_codes'][phase_key] = sub.get('type')
                    phase_value, valid = _validated_e3dc_phase_power(
                        sub,
                        tag_name,
                        validate_mirror_read_item,
                    )
                    if valid:
                        container_phase_values[phase_key] = phase_value
                elif tag == RscpTag.WB_NUMBER_PHASES and value is not None:
                    number_phases, valid = validate_mirror_read_item(
                        sub,
                        'WB_NUMBER_PHASES',
                    )
                    if valid and number_phases in (1, 3):
                        container_fixed_phases = int(number_phases)
            if (
                not phase_probe_values
                and container_fixed_phases in (1, 3)
                and set(container_phase_values) == {'l1', 'l2', 'l3'}
            ):
                phase_probe_values = (
                    container_phase_values['l1'],
                    container_phase_values['l2'],
                    container_phase_values['l3'],
                )
                fixed_phase_probe = container_fixed_phases
                status['phase_power_rscp_type_complete'] = True

        for sub in self._iter_wb_items(response):
            tag = sub.get('tag')
            val = sub.get('value')
            if tag in {
                RscpTag.WB_PM_POWER_L1,
                RscpTag.WB_PM_POWER_L2,
                RscpTag.WB_PM_POWER_L3,
            }:
                phase_name = {
                    RscpTag.WB_PM_POWER_L1: ('l1', 'WB_PM_POWER_L1'),
                    RscpTag.WB_PM_POWER_L2: ('l2', 'WB_PM_POWER_L2'),
                    RscpTag.WB_PM_POWER_L3: ('l3', 'WB_PM_POWER_L3'),
                }[tag]
                phase_key, tag_name = phase_name
                phase_value, valid = _validated_e3dc_phase_power(
                    sub,
                    tag_name,
                    validate_mirror_read_item,
                )
                if valid:
                    if phase_key == 'l1':
                        p1 = phase_value
                    elif phase_key == 'l2':
                        p2 = phase_value
                    else:
                        p3 = phase_value
            elif tag == RscpTag.WB_DEVICE_NAME:
                value, valid = validate_mirror_read_item(sub, 'WB_DEVICE_NAME')
                if valid:
                    self.device_name = value
                    status['device_name'] = value
                    status['device_name_source'] = 'rscp_wb_device_name'
            elif tag == RscpTag.WB_FIRMWARE_VERSION:
                value, valid = validate_mirror_read_item(sub, 'WB_FIRMWARE_VERSION')
                if valid:
                    self.firmware_version = value
                    status['firmware_version'] = value
                    status['firmware_version_source'] = 'rscp_wb_firmware_version'
            elif tag == RscpTag.WB_NUMBER_PHASES and val is not None:
                number_phases, valid = validate_mirror_read_item(sub, 'WB_NUMBER_PHASES')
                if valid and number_phases in (1, 3):
                    status['number_phases'] = number_phases
                    status['number_phases_source'] = 'rscp_wb_number_phases'
                    status['connected_phases'] = number_phases
                    status['phases_actual'] = number_phases
                    status['phases_target'] = number_phases
                    status['pha'] = 56 if number_phases >= 3 else (24 if number_phases == 2 else 8)
            elif tag == RscpTag.WB_AUTO_PHASE_SWITCH_ENABLED and val is not None:
                value, valid = validate_mirror_read_item(sub, 'WB_AUTO_PHASE_SWITCH_ENABLED')
                if valid:
                    status['auto_phase_switch_enabled'] = value
                    status['auto_phase_switch_source'] = (
                        'rscp_wb_auto_phase_switch_enabled'
                    )
            elif tag == RscpTag.WB_SUN_MODE_ACTIVE and val is not None:
                value, valid = validate_mirror_read_item(sub, 'WB_SUN_MODE_ACTIVE')
                if valid:
                    status['sun_mode_active'] = value
                    status['sun_mode_source'] = 'rscp_wb_sun_mode_active'
            elif tag == RscpTag.WB_ABORT_CHARGING and val is not None:
                value, valid = validate_mirror_read_item(sub, 'WB_ABORT_CHARGING')
                if valid:
                    status['abort_charging'] = value
                    status['abort_charging_source'] = 'rscp_wb_abort_charging'
                    status['enabled'] = not value
            elif tag == RscpTag.WB_WALLBOX_TYPE and val is not None:
                value, valid = validate_mirror_read_item(sub, 'WB_WALLBOX_TYPE')
                if valid:
                    status['wallbox_type'] = value
            elif tag == RscpTag.WB_RSP_PARAM_1:
                param = self._extract_extern_bytes(sub, RscpTag.WB_RSP_PARAM_1)
                if param is not None:
                    status['param1_hex'] = param.hex()
                    # C++ Referenz: iWBIst = WBchar[2] aus TAG_WB_RSP_PARAM_1.
                    if len(param) >= 3:
                        param_current = int(param[2])
                        status['param_current'] = param_current
                        status['param1_byte2_raw'] = param_current
            elif tag == RscpTag.WB_RSP_PARAM_2:
                param = self._extract_extern_bytes(sub, RscpTag.WB_RSP_PARAM_2)
                if param is not None:
                    status['param2_hex'] = param.hex()
            elif tag == RscpTag.WB_SESSION:
                session, valid = validate_mirror_read_item(sub, 'WB_SESSION')
                for s in (session if valid else []):
                    if s.get('tag') == RscpTag.WB_SESSION_CHARGED_ENERGY and s.get('value') is not None:
                        value, item_valid = validate_mirror_read_item(s, 'WB_SESSION_CHARGED_ENERGY')
                        if item_valid and value >= 0:
                            status['session_kwh'] = round(value / 1000.0, 3)
                    elif s.get('tag') == RscpTag.WB_SESSION_START_TIME and s.get('value') is not None:
                        value, item_valid = validate_mirror_read_item(s, 'WB_SESSION_START_TIME')
                        if item_valid:
                            status['session_start_ts'] = value
                    elif s.get('tag') == RscpTag.WB_SESSION_AUTH_DATA and s.get('value') is not None:
                        value, item_valid = validate_mirror_read_item(s, 'WB_SESSION_AUTH_DATA')
                        if item_valid:
                            status['rfid_tag'] = value
            else:
                alg_candidate = self._extract_alg_bytes(sub)
                if alg_candidate is not None:
                    alg = alg_candidate

        decoded = decode_wb_extern_data_alg(alg, age_s=0.0)
        self._observe_wbchar6_status(decoded)
        if decoded['valid']:
            flags = decoded['flags']
            self._last_alg_flags = int(flags)
            status['alg_seen'] = True
            status['alg_flags'] = int(flags)
            status['alg_abort_or_disabled'] = decoded.get('abort_or_disabled')
            status['alg_bit_0x40_semantics'] = (
                'abort_or_disabled_not_overload_protection'
            )
            status['extern_alg_hex'] = alg.hex()
            status['alg_charging'] = decoded['charging']
            status['alg_connected'] = decoded['plugged']
            status['charging'] = decoded['charging']
            status['car_connected_rscp'] = decoded['plugged']
            status['plug_locked'] = decoded['plug_locked']
            status['wb_status_valid'] = True
            status['wb_status_reason'] = 'ok'
            status['enabled'] = (flags & 0b01000000) == 0
            if len(alg) >= 2:
                status['pha'] = 8 if int(alg[1]) == 1 else 56

        if param_current is not None:
            status['amp'] = param_current

        if status.get('enabled') is False:
            status['amp'] = 0
            # 0x68 kann kurz nach einem Stop auftreten: Fahrzeug/PM meldet
            # noch Leistung, aber Abort ist bereits aktiv. Für die Regelung
            # ist das kein freigegebenes Laden mehr.
            status['charging'] = False

        if status['car_connected_rscp']:
            status['car'] = 2
        raw_power_w = p1 + p2 + p3
        active_phases = sum(1 for p in (p1, p2, p3) if p > 250)
        status['phase_power_l1_w'] = round(p1, 1)
        status['phase_power_l2_w'] = round(p2, 1)
        status['phase_power_l3_w'] = round(p3, 1)
        status['phase_power_sum_w'] = round(raw_power_w, 1)
        status['phases_in_use'] = int(active_phases)
        status['phase_power_verified'] = bool(
            status.get('enabled') is True
            and status.get('car_connected_rscp')
            and status.get('charging')
            and raw_power_w > 500
            and active_phases >= 1
        )
        alg_seen = bool(decoded['valid'])
        if not alg_seen:
            # Multi Connect kann beim Startversuch PM-Leistung melden, ohne
            # dass der ALG-Status echte Ladung bestaetigt. In diesem Fall
            # ist der Messwert für UI und Regelung ein Glitch.
            status['charging'] = None
        # Multi Connect kann kurz Phantomwerte aus dem internen PM liefern,
        # ohne dass WB_EXTERN_DATA_ALG/DEVICE_WORKING echte Ladung melden.
        # Für die Regelung bleibt deshalb der E3DC-Status führend.
        status['real_power_w'] = raw_power_w if status['charging'] else 0.0
        if status['charging']:
            if active_phases == 1:
                status['pha'] = 8
            elif active_phases == 2:
                status['pha'] = 24
            elif active_phases >= 3:
                status['pha'] = 56
            if active_phases:
                status['phases_actual'] = int(active_phases)
        status['can_switch_phases'] = False
        status['phase_switch_capability'] = 'e3dc_multi_connect_cp_480_unverified'
        status['phase_switch_source'] = 'disabled_by_hardware_protection'
        status['api_surface'] = ''
        transition_complete = all(type(status.get(name)) is bool for name in (
            'sun_mode_active', 'auto_phase_switch_enabled', 'abort_charging'
        ))
        self._update_device_identity(
            device_name=status.get('device_name'),
            wallbox_type=status.get('wallbox_type'),
        )
        self._set_control_backend(
            status_valid=bool(decoded.get('valid')),
            transition_capable=transition_complete,
            readback_ts=time.time() if transition_complete else 0.0,
        )
        status.update(self._backend_contract_fields())
        status.update(self._rscp_diag_status())
        self.real_charging = bool(status['charging'])
        return self._finalize_native_status_sample(
            status,
            phase_values=phase_probe_values,
            fixed_phases=fixed_phase_probe,
        )

    def release_to_e3dc(self, max_amp=16):
        with self.lock:
            if not self.external_suspended:
                logger.info(f"[WB{self.wb_id}] Multi Connect Direktsteuerung pausiert (Mode 0 / E3DC autonom).")
            # Die Freigabekante ist vor jedem Lesen/Wiederherstellen sichtbar.
            # Ein bereits an dieser Sperre wartender Heartbeat oder Direktbefehl
            # muss den Pausenzustand daher erneut prüfen und ohne Rahmen enden.
            self._control_generation += 1
            self.external_suspended = True
            self.last_amp = None
            self.last_force_state = None
            ok_default = self._restore_transition_state_once()
            failed_conn = self.conn
            self.conn = None
            if failed_conn is not None:
                try:
                    failed_conn.close()
                except Exception as close_error:
                    logger.debug(
                        f"[WB{self.wb_id}] RSCP close nach Release fehlgeschlagen: {close_error}"
                    )
            return ok_default

    def release_to_default(self, max_amp=16):
        return self.release_to_e3dc(max_amp=max_amp)

    def suspend_external_control(self, reason="Python stumm"):
        with self.lock:
            if not self.external_suspended:
                logger.info(f"[WB{self.wb_id}] Multi Connect Direktsteuerung pausiert ({reason}).")
            self._control_generation += 1
            self.external_suspended = True
            self.last_amp = None
            self.last_force_state = None
            failed_conn = self.conn
            self.conn = None
            if failed_conn is not None:
                try:
                    failed_conn.close()
                except Exception:
                    pass

    def set_amp_sonnenmodus(self, target_amp, force_state=None):
        """PV-only/Fuzzy: Manager gibt Budget vor; Multi bleibt im externen Modus."""
        request_generation = self._control_generation
        if not command_gate.allow_command(
            self,
            action="e3dc_multi_set_amp_sonnenmodus",
            payload={"target_amp": target_amp, "force_state": force_state},
        ):
            return False
        with self.lock:
            if request_generation != self._control_generation:
                return False
            bounded_amp = (
                0
                if float(target_amp or 0.0) < 0.5
                else max(
                    6,
                    min(
                        float(getattr(self, "max_amp", 16.0)),
                        float(target_amp),
                    ),
                )
            )
            self.external_suspended = False
            self.sonnenmodus = False
            ok = bool(
                self._send_command_internal(
                    bounded_amp,
                    force_state,
                    is_heartbeat=False,
                )
            )
            if ok:
                # Die Multi-Connect-Klasse überschreibt die Basismethode. Ohne
                # diesen bestätigten Sollwert bleibt ``last_amp`` auf ``None``
                # und der feldverifiziert zyklische SET_EXTERN-Heartbeat schweigt
                # nach genau einem Managerbefehl. Ein fehlgeschlagener Ausgang
                # darf dagegen keinen neuen Heartbeat-Sollwert vortäuschen.
                self.last_amp = bounded_amp
                self.last_force_state = force_state
            return ok

    def set_amp_autonomous_solar(self, target_amp, force_state=None):
        """Übersetzt einen expliziten Managerauftrag atomar in WBchar6-Mode=1.

        Wattbudget, Amperewert und Start-/Halteentscheidung kommen vollständig
        aus dem Manager. Der Treiber prüft unter derselben Sperre unmittelbar
        vor dem Ausgang ausschließlich, ob die explizite efy-Identität, der
        WBchar6-Kompatibilitätspfad und ein frischer ALG-Status noch gelten.
        Bei Verlust dieser Evidenz bleibt die Leitung unverändert; insbesondere
        gibt es keinen stillen Rückfall auf Mode=2 oder einen Phasenbefehl.
        """

        request_generation = self._control_generation
        if not command_gate.allow_command(
            self,
            action="e3dc_multi_set_amp_autonomous_solar",
            payload={"target_amp": target_amp, "force_state": force_state},
        ):
            return False
        with self.lock:
            if request_generation != self._control_generation:
                self._efy_autonomous_handoff_blocker = (
                    "control_generation_changed"
                )
                self._efy_autonomous_handoff_ts = time.time()
                return False
            contract = self._efy_autonomous_solar_handoff_contract()
            if contract.get("allowed") is not True:
                blocker = str(
                    contract.get("blocker")
                    or "autonomous_solar_contract_not_ready"
                )
                self._efy_autonomous_handoff_blocker = blocker
                self._efy_autonomous_handoff_ts = time.time()
                logger.warning(
                    "[WB%s] efy-Solarübergabe ohne Ausgang blockiert: %s",
                    self.wb_id,
                    blocker,
                )
                return False
            self._efy_autonomous_handoff_blocker = ""
            self._efy_autonomous_handoff_ts = time.time()
            bounded_amp = (
                0
                if float(target_amp or 0.0) < 0.5
                else max(
                    6,
                    min(
                        float(getattr(self, "max_amp", 16.0)),
                        float(target_amp),
                    ),
                )
            )
            previous_sonnenmodus = self.sonnenmodus
            self.external_suspended = False
            self.sonnenmodus = True
            ok = bool(
                self._send_command_internal(
                    bounded_amp,
                    force_state,
                    is_heartbeat=False,
                )
            )
            if ok:
                self.last_amp = bounded_amp
                self.last_force_state = force_state
            else:
                self.sonnenmodus = previous_sonnenmodus
                self.external_suspended = True
                self.last_amp = None
                self.last_force_state = None
                self._control_generation += 1
                self._efy_autonomous_handoff_blocker = (
                    "wire_command_failed_fail_silent"
                )
                self._efy_autonomous_handoff_ts = time.time()
            return ok

    def take_control(self):
        request_generation = self._control_generation
        with self.lock:
            if request_generation != self._control_generation:
                return False
            self.external_suspended = False
            if self.sonnenmodus:
                logger.info(f"[WB{self.wb_id}] Multi Connect: Mischbetrieb/Python-Kontrolle")
            self._capture_transition_baseline()
            self.sonnenmodus = False

    def emergency_stop(self):
        if not command_gate.allow_command(
            self,
            action="e3dc_multi_emergency_stop",
            payload={"target_amp": 0, "force_state": 1},
        ):
            return False
        logger.warning(f"[WB{self.wb_id}] Multi Connect emergency_stop(): Abort aktiv")
        with self.lock:
            if self.real_charging is not True:
                return False
            self.external_suspended = False
            self.last_amp = 0
            self.last_force_state = 1
            with command_gate.emergency_stop_scope(self):
                return bool(self._send_command_internal(0, 1, is_heartbeat=False))

    def set_amp_and_state(self, target_amp, force_state=None):
        """Mischbetrieb: PV+Speicher/Netz je nach Manager-Modus."""
        request_generation = self._control_generation
        if not command_gate.allow_command(
            self,
            action="e3dc_multi_set_amp_and_state",
            payload={"target_amp": target_amp, "force_state": force_state},
        ):
            return False
        with self.lock:
            if request_generation != self._control_generation:
                return False
            bounded_amp = (
                0
                if float(target_amp or 0.0) < 0.5
                else max(
                    6,
                    min(
                        float(getattr(self, "max_amp", 16.0)),
                        float(target_amp),
                    ),
                )
            )
            self.external_suspended = False
            self.sonnenmodus = False
            ok = bool(
                self._send_command_internal(
                    bounded_amp,
                    force_state,
                    is_heartbeat=False,
                )
            )
            if ok:
                self.last_amp = bounded_amp
                self.last_force_state = force_state
            return ok


# ===========================================================================
# OpenWBProCharger (openWB Pro: connect.php Status + Steuerung)
# ===========================================================================
class OpenWBProCharger(WallboxDriver):
    """Treiber für openWB Pro.

    Die openWB Pro ist kein openWB-Controller und stellt die Controller-
    simpleAPI unter /openWB/simpleAPI/simpleapi.php nicht bereit. Status und
    Steuerung laufen im Standalone-Betrieb über die dokumentierte
    /connect.php API.
    """

    # openWB verlangt bei aktivem Heartbeat regelmäßige connect.php-Abfragen
    # im Bereich 20–30 s. 25 s bleibt innerhalb dieses Herstellerfensters.
    HEARTBEAT_LEASE_REFRESH_MAX_S = 25.0

    def __init__(self, ip, wb_id=1, config=None):
        super().__init__(ip, wb_id)
        self.config = config or {}
        self.current_step_amp = 0.1
        self.status_url = f"http://{self.ip}/connect.php"
        self.fallback_status_url = f"http://{self.ip}/api/secc"
        self._last_control_key = None
        self._last_control_ts = 0.0
        self._last_phase_key = None
        self._last_phase_ts = 0.0
        self._heartbeat_enabled_assumed = False
        self._last_heartbeat_lease_refresh_ts = 0.0
        self._driver_instance_token = "%d:%d:%d" % (
            os.getpid(),
            self.wb_id,
            time.time_ns(),
        )
        self.state = {
            "driver_instance_token": self._driver_instance_token,
            "car": 1,
            "amp": 0,
            "pha": 0,
            "charging": False,
            "frc": 0,
            "plug_state": False,
            "locked": False,
            "charge_state": False,
            "real_power_w": 0.0,
            "phase_power_l1_w": 0.0,
            "phase_power_l2_w": 0.0,
            "phase_power_l3_w": 0.0,
            "phase_power_sum_w": 0.0,
            "phase_power_verified": False,
            "phase_apparent_l1_va": 0.0,
            "phase_apparent_l2_va": 0.0,
            "phase_apparent_l3_va": 0.0,
            "phase_current_l1_a": 0.0,
            "phase_current_l2_a": 0.0,
            "phase_current_l3_a": 0.0,
            "apparent_power_va": 0.0,
            "power_factor": 0.0,
            "evse_current": 0.0,
            "phases_in_use": 0,
            "phases_actual": 0,
            "phases_target": 0,
            # Vor dem ersten erfolgreichen connect.php-Readback ist nur die
            # konfigurierte Treiberklasse bekannt, nicht die verfügbare
            # Geräteschnittstelle. Unbekannt darf keinen Phasenbefehl öffnen.
            "can_switch_phases": False,
            "phase_switch_capability": "unknown_until_connect_php_readback",
            "phase_switch_source": "fail_closed",
            "phase_connect_payload_valid": False,
            "phase_connect_payload_error": "not_read_yet",
            "api_surface": "unknown",
            "daily_imported_wh": 0.0,
            "imported_total_wh": 0.0,
            "chargemode_str": "stop",
            "session_kwh": 0.0,
            "_session_start_wh": None,
            "_session_start_ts": None,
            "_plug_state_observed": False,
            "car_soc": 0.0,
            "car_soc_source": "",
            "car_soc_source_ts": None,
            "car_soc_raw_ts": None,
            "car_soc_rule_confirmed": False,
            "car_range": 0.0,
            "car_range_source": "",
            "car_charged_range": 0.0,
            "car_name": "openWB Pro",
            "car_id": None,
            "car_capacity_kwh": 0.0,
            "car_efficiency": 0.90,
            "car_consumption_kwh_100km": 0.0,
            "vehicle_id": None,
            "rfid_tag": None,
            "vehicle_identity_current": False,
            "stable_vehicle_identity_current": False,
            "rfid_timestamp": None,
            "_soc_anchor_soc": None,
            "_soc_anchor_imported_wh": None,
            "_soc_anchor_vehicle_id": None,
            "_soc_raw_timestamp": None,
            "_soc_raw_value": None,
            "serial": None,
            "version": None,
            "openwb_pro_api_version": 0,
            "cp_interrupt_supported": False,
            "cp_interrupt_capability_source": "unknown_until_connect_php_readback",
            "automatic_start_cp_supported": False,
            "automatic_start_cp_capability_source": "unknown_until_connect_php_readback",
            "connect_php_payload_valid": False,
            "connect_php_payload_error": "not_read_yet",
            "v2g_ready": 0,
            "evse_signaling": "",
            "offered_current_raw": 0.0,
            "offered_current_confirmed": False,
            "offered_current_readback_ts": 0.0,
            "current_step_amp": self.current_step_amp,
            "fractional_current_supported": True,
            "max_charge_power": 0,
            "max_discharge_power": 0,
            "temp_c": None,
            "cp_interrupt_isactive": 0,
            "cp_interrupt_duration": 0,
            "cp_interrupt_version": "",
        }
        logger.info(f"[WB{self.wb_id}] openWB Pro: IP={self.ip}, API=/connect.php")

    @staticmethod
    def _dig(data, path, default=None):
        cur = data
        for key in path.split("/"):
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur[key]
        return cur

    @staticmethod
    def _float(value, default=0.0):
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _soc_percent_value(value):
        if isinstance(value, bool) or value in (None, ""):
            return None
        try:
            soc = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(soc) or soc < 0.0 or soc > 100.0:
            return None
        return soc

    @staticmethod
    def _soc_source_timestamp(value, now_ts=None):
        if isinstance(value, bool) or value in (None, "", "null"):
            return None
        try:
            source_ts = float(value)
            now_value = time.time() if now_ts is None else float(now_ts)
        except (TypeError, ValueError):
            return None
        if source_ts > 100000000000.0:
            source_ts /= 1000.0
        if (
            not math.isfinite(source_ts)
            or not math.isfinite(now_value)
            or source_ts <= 0.0
            or now_value <= 0.0
            or source_ts > now_value + 300.0
        ):
            return None
        return int(source_ts)

    @staticmethod
    def _contract_flag_active(value):
        if value is True:
            return True
        if isinstance(value, bool) or value is None:
            return False
        if isinstance(value, (int, float)):
            try:
                return math.isfinite(float(value)) and float(value) != 0.0
            except (TypeError, ValueError):
                return False
        return str(value).strip().lower() in {
            "1", "true", "yes", "ja", "on", "active", "aktiv",
            "stale", "expired", "invalid",
        }

    @staticmethod
    def _plugged_explicitly_false(value):
        if value is False:
            return True
        if isinstance(value, bool) or value is None:
            return False
        if isinstance(value, (int, float)):
            try:
                return math.isfinite(float(value)) and float(value) == 0.0
            except (TypeError, ValueError):
                return False
        return str(value).strip().lower() in {
            "0", "false", "no", "nein", "off", "aus",
        }

    @staticmethod
    def _boolish(value):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _compact_id(value):
        return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

    @staticmethod
    def _clamp_percent(value, default=0.0):
        try:
            return max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            return default

    def _saved_car_profiles(self):
        path = "/var/www/html/data/saved_cars.json"
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return []
        if isinstance(data, dict):
            data = list(data.values())
        return data if isinstance(data, list) else []

    def _merge_vehicle_profile(self, profile, car):
        if not isinstance(car, dict):
            return profile
        profile = dict(profile)
        profile["id"] = str(car.get("id") or profile.get("id") or "").strip() or None
        profile["name"] = str(car.get("name") or profile.get("name") or "").strip()
        profile["vehicle_id"] = str(
            car.get("vehicle_id")
            or car.get("vehicle_mac")
            or car.get("mac")
            or car.get("rfid")
            or car.get("rfid_tag")
            or profile.get("vehicle_id")
            or ""
        ).strip()
        capacity = self._float(car.get("capacity", car.get("capacity_kwh")), profile.get("capacity_kwh", 0.0))
        if capacity > 0:
            profile["capacity_kwh"] = capacity
        efficiency = self._float(
            car.get("efficiency", car.get("charge_efficiency", car.get("charging_efficiency"))),
            profile.get("efficiency", 0.90),
        )
        if efficiency > 1.0:
            efficiency = efficiency / 100.0
        profile["efficiency"] = max(0.50, min(1.00, efficiency or 0.90))
        consumption = self._float(
            car.get("consumption", car.get("consumption_kwh_100km", car.get("avg_consumption"))),
            profile.get("consumption_kwh_100km", 0.0),
        )
        if consumption > 0:
            profile["consumption_kwh_100km"] = consumption
        return profile

    def _vehicle_profile(self, vehicle_id=None, fallback_name=None, allow_selected_fallback=True):
        selected_id = str(self.config.get(f"wb{self.wb_id}_car_id") or "").strip()
        if selected_id.lower() in ("__none", "none", "0", "false"):
            selected_id = ""
        selected_profile_id = selected_id if allow_selected_fallback else ""
        capacity = self._float(self.config.get(f"wb{self.wb_id}_capacity"), 0.0)
        profile = {
            "id": selected_profile_id or None,
            "name": str(fallback_name or "").strip(),
            "vehicle_id": str(vehicle_id or "").strip(),
            "capacity_kwh": capacity,
            "efficiency": 0.90,
            "consumption_kwh_100km": 0.0,
        }
        cars = self._saved_car_profiles()
        probe = self._compact_id(vehicle_id)
        if probe:
            for car in cars:
                for key in ("vehicle_id", "vehicle_mac", "mac", "rfid", "rfid_tag"):
                    if self._compact_id(car.get(key)) == probe:
                        return self._merge_vehicle_profile(profile, car)
        if allow_selected_fallback:
            for car in cars:
                if not selected_id:
                    continue
                for key in ("id", "cloud_vehicle_id", "vehicle_id", "vehicle_mac", "mac", "rfid", "rfid_tag"):
                    if str(car.get(key) or "").strip() == selected_id:
                        return self._merge_vehicle_profile(profile, car)
        fallback_norm = str(fallback_name or "").strip().lower()
        if fallback_norm:
            for car in cars:
                if str(car.get("name") or "").strip().lower() == fallback_norm:
                    return self._merge_vehicle_profile(profile, car)
        return profile

    def _write_openwb_pro_manual_soc(self, source):
        soc = self._soc_percent_value(self.state.get("car_soc"))
        if soc is None:
            self.state["car_soc_rule_confirmed"] = False
            return False
        now_ts = time.time()
        source = str(source or "").strip()
        source_ts = self._soc_source_timestamp(
            self.state.get("car_soc_source_ts"),
            now_ts=now_ts,
        )
        raw_soc_ts = self._soc_source_timestamp(
            self.state.get("car_soc_raw_ts"),
            now_ts=now_ts,
        )
        confirmed = bool(
            self.state.get("car_soc_rule_confirmed") is True
            and source in {"openwb_pro_raw", "openwb_pro_estimated", "manual_start_soc"}
            and source_ts is not None
            and raw_soc_ts is not None
        )
        self.state["car_soc_rule_confirmed"] = confirmed
        file_ts = int(now_ts)
        soc_file = os.path.join(RAMDISK_DIR, f"manual_soc_wb{self.wb_id}.json")
        payload = {
            "soc": round(soc, 1),
            "ts": file_ts,
            "source": source,
            "soc_source_ts": source_ts,
            "raw_soc_ts": raw_soc_ts,
            "soc_rule_confirmed": confirmed,
            "plugged": bool(self.state.get("plug_state", False)),
            "age_h": round(max(0.0, (now_ts - float(source_ts or now_ts)) / 3600.0), 1),
            "wb": self.wb_id,
            "name": self.state.get("car_name"),
            "car_id": self.state.get("car_id"),
            "vehicle_id": self.state.get("vehicle_id") or self.state.get("rfid_tag"),
            "capacity": self.state.get("car_capacity_kwh", 0.0),
            "session_kwh": round(self.state.get("session_kwh", 0.0), 3),
        }
        tmp = f"{soc_file}.tmp.{os.getpid()}.{time.monotonic_ns()}"
        try:
            os.makedirs(os.path.dirname(soc_file), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, soc_file)
            try:
                os.chmod(soc_file, 0o664)
            except OSError:
                pass
            return True
        except Exception as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            logger.debug(f"[WB{self.wb_id}] openWB Pro manual_soc schreiben fehlgeschlagen: {e}")
            return False

    def _openwb_pro_manual_start_sample(self, active_id=None):
        soc_file = os.path.join(RAMDISK_DIR, f"manual_soc_wb{self.wb_id}.json")
        try:
            with open(soc_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if str(data.get("source") or "").strip() != "manual_start_soc":
            return None
        if "plugged" in data and self._plugged_explicitly_false(data.get("plugged")):
            return None
        if "soc_rule_confirmed" in data and data.get("soc_rule_confirmed") is not True:
            return None
        if any(
            self._contract_flag_active(data.get(key))
            for key in (
                "soc_stale", "estimate_expired", "soc_profile_binding_invalid",
            )
        ):
            return None
        now_ts = time.time()
        source_ts = self._soc_source_timestamp(
            data.get("soc_source_ts", data.get("raw_soc_ts", data.get("ts"))),
            now_ts=now_ts,
        )
        raw_soc_ts = self._soc_source_timestamp(
            data.get("raw_soc_ts", data.get("soc_source_ts", data.get("ts"))),
            now_ts=now_ts,
        )
        if (
            source_ts is None
            or raw_soc_ts is None
            or now_ts - source_ts > 12 * 3600
            or now_ts - raw_soc_ts > 12 * 3600
        ):
            return None
        soc = self._soc_percent_value(data.get("soc"))
        if soc is None:
            return None
        manual_vehicle_id = str(data.get("vehicle_id") or "").strip()
        manual_car_id = str(data.get("car_id") or "").strip()
        active_compact = self._compact_id(active_id)
        if active_compact and manual_vehicle_id and self._compact_id(manual_vehicle_id) != active_compact:
            return None
        return {
            "soc": soc,
            "ts": source_ts,
            "soc_source_ts": source_ts,
            "raw_soc_ts": raw_soc_ts,
            "soc_rule_confirmed": True,
            "car_id": manual_car_id,
            "vehicle_id": manual_vehicle_id,
            "name": str(data.get("name") or "").strip(),
            "capacity_kwh": self._float(data.get("capacity"), 0.0),
        }

    def _openwb_pro_parse_session_ts(self, value):
        if isinstance(value, bool) or value is None:
            return 0
        try:
            numeric_value = float(value)
            if math.isfinite(numeric_value):
                numeric = int(numeric_value)
                if numeric > 0:
                    return numeric
        except (TypeError, ValueError, OverflowError):
            pass
        text = str(value or "").strip()
        if not text:
            return 0
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return int(time.mktime(time.strptime(text[:19], fmt)))
            except Exception:
                pass
        return 0

    def _openwb_pro_persisted_session_sample(self, active_id=None):
        now_ts = time.time()
        best = {"kwh": 0.0, "start_ts": 0, "source": ""}

        def accept(kwh, start_ts=0, source=""):
            if isinstance(kwh, bool):
                return
            try:
                kwh = float(kwh)
                start_ts = int(start_ts)
            except (TypeError, ValueError, OverflowError):
                return
            if (
                not math.isfinite(kwh)
                or kwh <= 0.02
                or start_ts <= 0
                or start_ts > now_ts + 300.0
            ):
                return
            if kwh > self._float(best.get("kwh"), 0.0):
                best.update({"kwh": kwh, "start_ts": start_ts, "source": source})

        session_file = os.path.join(RAMDISK_DIR, f"wb{self.wb_id}_live_session.json")
        try:
            with open(session_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            data = None
        if (
            isinstance(data, dict)
            and not (
                "is_locked" in data
                and self._plugged_explicitly_false(data.get("is_locked"))
            )
        ):
            last_ts = self._openwb_pro_parse_session_ts(data.get("last_ts") or data.get("ts"))
            start_ts = self._openwb_pro_parse_session_ts(data.get("start_ts"))
            if (
                last_ts > 0
                and start_ts > 0
                and -300.0 <= now_ts - last_ts <= 24 * 3600
                and start_ts <= last_ts + 300
            ):
                accept(
                    data.get("kwh", data.get("session_kwh", 0.0)),
                    start_ts,
                    "live_session",
                )

        soc_file = os.path.join(RAMDISK_DIR, f"manual_soc_wb{self.wb_id}.json")
        try:
            with open(soc_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            data = None
        if isinstance(data, dict):
            source = str(data.get("source") or "").strip()
            # Derived openWB-Pro SoC estimates are display/cache values, not
            # authoritative session anchors. Feeding their session_kwh back
            # into the estimator after a restart can add old charge energy to
            # the raw vehicle SoC again and make the car SoC jump.
            if source != "manual_start_soc":
                return best
            if "plugged" in data and self._plugged_explicitly_false(data.get("plugged")):
                return best
            if "soc_rule_confirmed" in data and data.get("soc_rule_confirmed") is not True:
                return best
            if any(
                self._contract_flag_active(data.get(key))
                for key in (
                    "soc_stale", "estimate_expired", "soc_profile_binding_invalid",
                )
            ):
                return best
            if self._soc_percent_value(data.get("soc")) is None:
                return best
            source_ts = self._soc_source_timestamp(
                data.get("soc_source_ts", data.get("raw_soc_ts", data.get("ts"))),
                now_ts=now_ts,
            )
            raw_soc_ts = self._soc_source_timestamp(
                data.get("raw_soc_ts", data.get("soc_source_ts", data.get("ts"))),
                now_ts=now_ts,
            )
            if (
                source_ts is None
                or raw_soc_ts is None
                or now_ts - source_ts > 12 * 3600
                or now_ts - raw_soc_ts > 12 * 3600
            ):
                return best
            active_compact = self._compact_id(active_id)
            manual_vehicle_id = str(data.get("vehicle_id") or "").strip()
            if not active_compact or not manual_vehicle_id or self._compact_id(manual_vehicle_id) == active_compact:
                accept(
                    data.get("session_kwh", 0.0),
                    raw_soc_ts,
                    source,
                )
        return best

    def _update_vehicle_soc_estimate(self, raw_soc, imported_wh, plug_state, charging, vehicle_id=None, rfid_tag=None, prev_plug=False, raw_soc_ts=None, restored_session_wh=0.0, restored_session_start_ts=0):
        now_ts = time.time()
        active_id = vehicle_id or rfid_tag
        manual_sample = self._openwb_pro_manual_start_sample(active_id)
        fallback_name = self.state.get("car_name", "openWB Pro")
        profile = self._vehicle_profile(active_id, fallback_name=fallback_name, allow_selected_fallback=bool(active_id))
        capacity = self._float(profile.get("capacity_kwh"), 0.0)
        if manual_sample and manual_sample.get("capacity_kwh", 0.0) > 0:
            capacity = self._float(manual_sample.get("capacity_kwh"), capacity)
        efficiency = self._float(profile.get("efficiency"), 0.90)
        consumption = self._float(profile.get("consumption_kwh_100km"), 0.0)
        if capacity > 0:
            self.state["car_capacity_kwh"] = capacity
        self.state["car_efficiency"] = max(0.50, min(1.00, efficiency or 0.90))
        self.state["car_consumption_kwh_100km"] = consumption
        if profile.get("name"):
            self.state["car_name"] = profile["name"]
        if profile.get("id"):
            self.state["car_id"] = profile["id"]
        elif active_id:
            self.state["car_id"] = active_id
        if profile.get("vehicle_id") and not self.state.get("vehicle_id"):
            self.state["vehicle_id"] = profile["vehicle_id"]
        if manual_sample:
            if manual_sample.get("name"):
                self.state["car_name"] = manual_sample["name"]
            if manual_sample.get("car_id"):
                self.state["car_id"] = manual_sample["car_id"]
            if manual_sample.get("vehicle_id") and not self.state.get("vehicle_id"):
                self.state["vehicle_id"] = manual_sample["vehicle_id"]

        normalized_raw_soc = self._soc_percent_value(raw_soc)
        raw_valid = normalized_raw_soc is not None
        if raw_valid:
            raw_soc = normalized_raw_soc
        raw_source_ts = self._soc_source_timestamp(raw_soc_ts, now_ts=now_ts)
        raw_ts = int(raw_source_ts or 0)
        raw_rule_confirmed = bool(raw_valid and raw_source_ts is not None)
        last_raw_ts = int(self.state.get("_soc_raw_timestamp") or 0)
        last_raw_soc = self._float(self.state.get("_soc_raw_value"), -1.0)
        new_raw_sample = False
        if raw_valid:
            if raw_ts > 0:
                new_raw_sample = raw_ts != last_raw_ts and raw_ts > last_raw_ts
            if last_raw_soc >= 0:
                new_raw_sample = new_raw_sample or abs(raw_soc - last_raw_soc) >= 0.3
        ident_compact = self._compact_id(active_id)
        anchor_ident = self.state.get("_soc_anchor_vehicle_id")
        anchor_imported = self.state.get("_soc_anchor_imported_wh")
        meter_reset = anchor_imported is not None and imported_wh + 100.0 < anchor_imported
        id_changed = bool(ident_compact and anchor_ident and ident_compact != anchor_ident)
        anchor_missing = self.state.get("_soc_anchor_soc") is None or anchor_imported is None

        if not plug_state:
            if raw_valid:
                self.state["_soc_raw_timestamp"] = raw_ts or int(now_ts)
                self.state["_soc_raw_value"] = raw_soc
                self.state["car_soc_raw_ts"] = raw_ts or None
                self.state["car_soc"] = round(self._clamp_percent(raw_soc), 1)
                self.state["car_soc_source"] = "openwb_pro_raw"
                self.state["car_soc_source_ts"] = raw_ts or int(now_ts)
                self.state["car_soc_rule_confirmed"] = raw_rule_confirmed
                self._write_openwb_pro_manual_soc("openwb_pro_raw")
            self.state["_soc_anchor_soc"] = None
            self.state["_soc_anchor_imported_wh"] = None
            self.state["_soc_anchor_vehicle_id"] = None
            self.state["_soc_power_integrated_wh"] = 0.0
            self.state["_soc_delivered_wh"] = 0.0
            self.state["_soc_last_update_ts"] = None
            return

        last_update_ts = self._float(self.state.get("_soc_last_update_ts"), 0.0)
        if charging and last_update_ts > 0 and now_ts > last_update_ts:
            dt_s = min(max(0.0, now_ts - last_update_ts), 300.0)
            power_w = self._float(self.state.get("real_power_w"), 0.0)
            if power_w > 50.0 and dt_s > 0:
                self.state["_soc_power_integrated_wh"] = (
                    self._float(self.state.get("_soc_power_integrated_wh"), 0.0)
                    + power_w * dt_s / 3600.0
                )
        self.state["_soc_last_update_ts"] = now_ts

        current_soc = self._float(self.state.get("car_soc"), -1.0)
        reanchor = anchor_missing or (not prev_plug) or id_changed
        restored_session_wh = max(0.0, self._float(restored_session_wh, 0.0))
        restored_session_start_ts = int(self._float(restored_session_start_ts, 0.0))
        raw_supports_restored_session = False
        if restored_session_wh > 20.0 and raw_valid and raw_ts > 0 and (anchor_missing or not prev_plug):
            if restored_session_start_ts > 0:
                raw_supports_restored_session = raw_ts <= restored_session_start_ts + 900
            else:
                raw_supports_restored_session = now_ts - raw_ts > 900
        manual_ts = int(self._float((manual_sample or {}).get("ts"), 0.0))
        anchor_sample_ts = int(self._float(self.state.get("_soc_anchor_sample_ts"), 0.0))
        manual_anchor_already = self.state.get("_soc_anchor_source") == "manual_start_soc"
        manual_soc_changed = abs(self._float((manual_sample or {}).get("soc"), -1.0) - self._float(self.state.get("_soc_anchor_soc"), -1.0)) >= 0.2
        manual_reanchor = bool(
            manual_sample
            and (
                reanchor
                or (manual_ts > anchor_sample_ts + 1 and (not manual_anchor_already or manual_soc_changed))
            )
        )
        if manual_reanchor:
            self.state["_soc_anchor_soc"] = self._clamp_percent(manual_sample["soc"])
            manual_anchor_imported = imported_wh
            if restored_session_wh > 20.0 and (anchor_missing or not prev_plug):
                manual_anchor_imported = max(0.0, imported_wh - restored_session_wh)
            self.state["_soc_anchor_imported_wh"] = manual_anchor_imported
            self.state["_soc_anchor_vehicle_id"] = ident_compact or self._compact_id(manual_sample.get("vehicle_id") or manual_sample.get("car_id")) or None
            self.state["_soc_anchor_source"] = "manual_start_soc"
            self.state["_soc_anchor_sample_ts"] = manual_ts or int(now_ts)
            self.state["_soc_anchor_raw_ts"] = manual_sample.get("raw_soc_ts")
            self.state["_soc_anchor_rule_confirmed"] = (
                manual_sample.get("soc_rule_confirmed") is True
            )
            self.state["car_soc"] = round(self.state["_soc_anchor_soc"], 1)
            self.state["car_soc_source"] = "manual_start_soc"
            self.state["car_soc_source_ts"] = manual_ts or int(now_ts)
            self.state["car_soc_raw_ts"] = manual_sample.get("raw_soc_ts")
            self.state["car_soc_rule_confirmed"] = (
                manual_sample.get("soc_rule_confirmed") is True
            )
            self.state["_soc_power_integrated_wh"] = 0.0
            self.state["_soc_delivered_wh"] = 0.0
            self.state["_soc_last_update_ts"] = now_ts
        manual_anchor_active = self.state.get("_soc_anchor_source") == "manual_start_soc"
        if raw_valid and not manual_anchor_active and (reanchor or new_raw_sample):
            self.state["_soc_anchor_soc"] = self._clamp_percent(raw_soc)
            raw_anchor_imported = imported_wh
            if raw_supports_restored_session:
                raw_anchor_imported = max(0.0, imported_wh - restored_session_wh)
            self.state["_soc_anchor_imported_wh"] = raw_anchor_imported
            self.state["_soc_anchor_vehicle_id"] = ident_compact or None
            self.state["_soc_anchor_source"] = "openwb_pro_raw"
            self.state["_soc_anchor_sample_ts"] = raw_ts or int(now_ts)
            self.state["_soc_anchor_raw_ts"] = raw_ts or None
            self.state["_soc_anchor_rule_confirmed"] = raw_rule_confirmed
            self.state["_soc_raw_timestamp"] = raw_ts or int(now_ts)
            self.state["_soc_raw_value"] = raw_soc
            self.state["car_soc_raw_ts"] = raw_ts or None
            self.state["car_soc"] = round(self.state["_soc_anchor_soc"], 1)
            self.state["car_soc_source"] = "openwb_pro_raw"
            self.state["car_soc_source_ts"] = raw_ts or int(now_ts)
            self.state["car_soc_rule_confirmed"] = raw_rule_confirmed
            self.state["_soc_power_integrated_wh"] = 0.0
            self.state["_soc_delivered_wh"] = 0.0
            self.state["_soc_last_update_ts"] = now_ts

        anchor_soc = self.state.get("_soc_anchor_soc")
        anchor_imported = self.state.get("_soc_anchor_imported_wh")
        if anchor_soc is not None and anchor_imported is not None and capacity > 0:
            meter_delta_wh = 0.0
            if imported_wh >= anchor_imported:
                meter_delta_wh = max(0.0, imported_wh - anchor_imported)
            delivered_wh = max(
                meter_delta_wh,
                self._float(self.state.get("_soc_power_integrated_wh"), 0.0),
                self._float(self.state.get("_soc_delivered_wh"), 0.0),
            )
            self.state["_soc_delivered_wh"] = delivered_wh
            self.state["_soc_power_integrated_wh"] = max(
                self._float(self.state.get("_soc_power_integrated_wh"), 0.0),
                delivered_wh,
            )
            self.state["session_kwh"] = max(self._float(self.state.get("session_kwh"), 0.0), delivered_wh / 1000.0)
            delta_kwh = delivered_wh / 1000.0
            estimated_soc = self._clamp_percent(anchor_soc + (delta_kwh * self.state["car_efficiency"] / capacity * 100.0))
            self.state["car_soc"] = round(estimated_soc, 1)
            anchor_source = str(self.state.get("_soc_anchor_source") or "openwb_pro_raw")
            self.state["car_soc_source"] = "openwb_pro_estimated" if delta_kwh > 0.02 else anchor_source
            self.state["car_soc_source_ts"] = int(
                self._float(self.state.get("_soc_anchor_sample_ts"), now_ts)
            )
            self.state["car_soc_raw_ts"] = self.state.get("_soc_anchor_raw_ts")
            self.state["car_soc_rule_confirmed"] = (
                self.state.get("_soc_anchor_rule_confirmed") is True
            )
            if consumption > 0:
                self.state["car_range"] = round((capacity * estimated_soc / 100.0) / consumption * 100.0, 0)
                self.state["car_range_source"] = "openwb_pro_estimated"
                charged_range = (delta_kwh * self.state["car_efficiency"] / consumption * 100.0) if consumption > 0 else 0.0
                self.state["car_charged_range"] = round(max(0.0, charged_range), 1)
            self._write_openwb_pro_manual_soc(self.state["car_soc_source"])
        elif raw_valid:
            self.state["_soc_raw_timestamp"] = raw_ts or int(now_ts)
            self.state["_soc_raw_value"] = raw_soc
            self.state["car_soc_raw_ts"] = raw_ts or None
            self.state["car_soc"] = round(self._clamp_percent(raw_soc), 1)
            self.state["car_soc_source"] = "openwb_pro_raw"
            self.state["car_soc_source_ts"] = raw_ts or int(now_ts)
            self.state["car_soc_rule_confirmed"] = raw_rule_confirmed
            self._write_openwb_pro_manual_soc("openwb_pro_raw")

    def _get_json(self, url):
        try:
            response = _requests.get(url, timeout=5, headers={"Accept": "application/json"})
            response.raise_for_status()
            text = response.text.strip()
            if not text.startswith("{"):
                return None
            return response.json()
        except Exception as e:
            logger.debug(f"[WB{self.wb_id}] openWB Pro GET fehlgeschlagen ({url}): {e}")
            return None

    def _post_control(self, payload):
        if not command_gate.allow_command(
            self,
            action="openwb_pro_post_control",
            payload=payload,
            audit_allowed=False,
        ):
            return False
        try:
            if not command_gate.allow_command(
                self,
                action="openwb_pro_post_control_wire",
                payload=payload,
                audit_allowed=False,
            ):
                return False
            response = _requests.post(self.status_url, data=payload, timeout=5)
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"[WB{self.wb_id}] openWB Pro POST fehlgeschlagen ({payload}): {e}")
            return False

    def _write_openwb_pro_status(self):
        out_file = os.path.join(RAMDISK_DIR, f"openwb_data_wb{self.wb_id}.json")
        out_alias = os.path.join(RAMDISK_DIR, "openwb_data.json") if self.wb_id == 1 else None
        payload = {
            "plug_state": self.state["plug_state"],
            "plug_state_raw": bool(self.state.get("plug_state_raw", self.state["plug_state"])),
            "locked": bool(self.state.get("locked", self.state["plug_state"])),
            "charge_state": self.state["charge_state"],
            "power_w": round(self.state["real_power_w"], 1),
            "phase_power_l1_w": round(self.state.get("phase_power_l1_w", 0.0), 1),
            "phase_power_l2_w": round(self.state.get("phase_power_l2_w", 0.0), 1),
            "phase_power_l3_w": round(self.state.get("phase_power_l3_w", 0.0), 1),
            "phase_power_sum_w": round(self.state.get("phase_power_sum_w", self.state["real_power_w"]), 1),
            "phase_power_verified": bool(self.state.get("phase_power_verified", False)),
            "phase_apparent_l1_va": round(self.state.get("phase_apparent_l1_va", 0.0), 1),
            "phase_apparent_l2_va": round(self.state.get("phase_apparent_l2_va", 0.0), 1),
            "phase_apparent_l3_va": round(self.state.get("phase_apparent_l3_va", 0.0), 1),
            "phase_current_l1_a": round(self.state.get("phase_current_l1_a", 0.0), 2),
            "phase_current_l2_a": round(self.state.get("phase_current_l2_a", 0.0), 2),
            "phase_current_l3_a": round(self.state.get("phase_current_l3_a", 0.0), 2),
            "apparent_power_va": round(self.state.get("apparent_power_va", 0.0), 1),
            "apparent_power_kva": round(self.state.get("apparent_power_va", 0.0) / 1000.0, 2),
            "power_factor": round(self.state.get("power_factor", 0.0), 2),
            "evse_current": self.state["evse_current"],
            "phases_in_use": self.state["phases_in_use"],
            "phases_actual": self.state.get("phases_actual", 0),
            "phases_target": self.state.get("phases_target", 0),
            "can_switch_phases": self.state["can_switch_phases"],
            "phase_switch_capability": self.state.get("phase_switch_capability", "official_connect_php"),
            "phase_switch_source": self.state.get("phase_switch_source", "openwb_pro_connect_php"),
            "phase_connect_payload_valid": bool(
                self.state.get("phase_connect_payload_valid", False)
            ),
            "phase_connect_payload_error": str(
                self.state.get("phase_connect_payload_error", "") or ""
            ),
            "api_surface": self.state.get("api_surface", "openwb_pro_connect_php"),
            "daily_imported_wh": round(self.state["daily_imported_wh"], 0),
            "imported_total_wh": round(self.state["imported_total_wh"], 0),
            "chargemode": self.state["chargemode_str"],
            "session_kwh": round(self.state["session_kwh"], 3),
            "session_start_ts": self.state.get("_session_start_ts") if self.state.get("plug_state") else None,
            "driver_instance_token": self._driver_instance_token,
            "cp_id": "pro",
            "wb_id": self.wb_id,
            "ts": int(time.time()),
            "source": "openwb_pro",
            "car_soc": self.state.get("car_soc", 0),
            "car_soc_source": self.state.get("car_soc_source", ""),
            "car_soc_source_ts": self.state.get("car_soc_source_ts"),
            "car_soc_raw_ts": self.state.get("car_soc_raw_ts"),
            "car_soc_rule_confirmed": self.state.get("car_soc_rule_confirmed") is True,
            "car_range": self.state.get("car_range", 0),
            "range_km": self.state.get("car_range", 0),
            "car_range_source": self.state.get("car_range_source", ""),
            "car_charged_range": self.state.get("car_charged_range", 0),
            "charged_range_km": self.state.get("car_charged_range", 0),
            "car_name": self.state.get("car_name", "openWB Pro"),
            "car_id": self.state.get("car_id", None),
            "car_capacity_kwh": self.state.get("car_capacity_kwh", 0.0),
            "car_efficiency": self.state.get("car_efficiency", 0.90),
            "car_consumption_kwh_100km": self.state.get("car_consumption_kwh_100km", 0.0),
            "vehicle_id": self.state.get("vehicle_id"),
            "rfid_tag": self.state.get("rfid_tag"),
            "vehicle_identity_current": bool(self.state.get("vehicle_identity_current", False)),
            "stable_vehicle_identity_current": bool(self.state.get("stable_vehicle_identity_current", False)),
            "rfid_timestamp": self.state.get("rfid_timestamp"),
            "serial": self.state.get("serial"),
            "version": self.state.get("version"),
            "openwb_pro_api_version": self.state.get("openwb_pro_api_version", 0),
            "cp_interrupt_supported": bool(
                self.state.get("cp_interrupt_supported", False)
            ),
            "cp_interrupt_capability_source": str(
                self.state.get("cp_interrupt_capability_source", "") or ""
            ),
            "automatic_start_cp_supported": bool(
                self.state.get("automatic_start_cp_supported", False)
            ),
            "automatic_start_cp_capability_source": self.state.get(
                "automatic_start_cp_capability_source",
                "explicit_vehicle_profile_required",
            ),
            "connect_php_payload_valid": bool(
                self.state.get("connect_php_payload_valid", False)
            ),
            "connect_php_payload_error": str(
                self.state.get("connect_php_payload_error", "") or ""
            ),
            "v2g_ready": self.state.get("v2g_ready", 0),
            "evse_signaling": self.state.get("evse_signaling", ""),
            "offered_current_raw": self.state.get("offered_current_raw", 0.0),
            "offered_current_confirmed": bool(
                self.state.get("offered_current_confirmed", False)
            ),
            "offered_current_readback_ts": self.state.get(
                "offered_current_readback_ts", 0.0
            ),
            "max_charge_power": self.state.get("max_charge_power", 0),
            "max_discharge_power": self.state.get("max_discharge_power", 0),
            "temp_c": self.state.get("temp_c"),
            "cp_interrupt_isactive": self.state.get("cp_interrupt_isactive", 0),
            "cp_interrupt_duration": self.state.get("cp_interrupt_duration", 0),
            "cp_interrupt_version": self.state.get("cp_interrupt_version", ""),
        }
        try:
            tmp = out_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, out_file)
            if out_alias:
                alias_tmp = out_alias + ".tmp"
                with open(alias_tmp, "w") as alias_handle:
                    json.dump(payload, alias_handle)
                os.replace(alias_tmp, out_alias)
        except Exception as e:
            logger.debug(f"[WB{self.wb_id}] openWB Pro Status schreiben fehlgeschlagen: {e}")

    @staticmethod
    def _valid_connect_status_payload(data):
        """Prüft nur die offizielle Basisfläche für Status und Heartbeat.

        Ein HTTP-200 mit ``{}`` oder einem Teilobjekt belegt weder den
        Hersteller-Statusvertrag noch eine erneuerte lokale Heartbeat-Lease.
        Optionale Spannungen und zusätzliche Phasen-/CP-Felder dürfen dagegen
        die Stromregelung und Heartbeat-Lease nicht sperren.
        """

        if not isinstance(data, dict) or not data:
            return False, "empty_or_non_object"
        required = {
            "power_all",
            "powers",
            "currents",
            "imported",
            "exported",
            "plug_state",
            "charge_state",
            "phases_in_use",
            "vehicle_id",
            "offered_current",
            "serial",
            "evse_signaling",
        }
        missing = sorted(required.difference(data))
        if missing:
            return False, "missing:" + ",".join(missing)

        for key in ("powers", "currents"):
            values = data.get(key)
            if not isinstance(values, list) or len(values) < 3:
                return False, f"invalid_{key}"
            try:
                if not all(math.isfinite(float(value)) for value in values[:3]):
                    return False, f"non_finite_{key}"
            except (TypeError, ValueError):
                return False, f"non_numeric_{key}"

        voltages = data.get("voltages")
        if voltages:
            if not isinstance(voltages, list) or len(voltages) < 3:
                return False, "invalid_optional_voltages"
            try:
                if not all(math.isfinite(float(value)) for value in voltages[:3]):
                    return False, "non_finite_optional_voltages"
            except (TypeError, ValueError):
                return False, "non_numeric_optional_voltages"

        for key in (
            "power_all",
            "imported",
            "exported",
            "offered_current",
            "phases_in_use",
        ):
            try:
                value = float(data.get(key))
            except (TypeError, ValueError):
                return False, f"non_numeric_{key}"
            if not math.isfinite(value):
                return False, f"non_finite_{key}"
        if int(float(data.get("phases_in_use"))) not in (0, 1, 2, 3):
            return False, "invalid_phases_in_use"
        if not str(data.get("evse_signaling") or "").strip():
            return False, "missing_evse_signaling_value"
        return True, ""

    @staticmethod
    def _valid_connect_phase_payload(data):
        """Prüft die zusätzliche Phasenfläche ohne den Basisstatus zu entwerten."""

        if not isinstance(data, dict):
            return False, "non_object"
        missing = sorted({"phases_actual", "phases_target"}.difference(data))
        if missing:
            return False, "missing:" + ",".join(missing)
        for key in ("phases_actual", "phases_target"):
            try:
                value = float(data.get(key))
            except (TypeError, ValueError):
                return False, f"non_numeric_{key}"
            if not math.isfinite(value):
                return False, f"non_finite_{key}"
        if int(float(data.get("phases_actual"))) not in (0, 1, 2, 3):
            return False, "invalid_phases_actual"
        if int(float(data.get("phases_target"))) not in (0, 1, 3):
            return False, "invalid_phases_target"
        return True, ""

    def _cp_interrupt_capability(self, data):
        """Binde den CP-Wirevertrag an Readback oder explizite Installation."""

        explicit = _config_bool(
            self.config,
            f"wb{int(self.wb_id or 1)}_openwb_pro_cp_interrupt_supported",
            "openwb_pro_cp_interrupt_supported",
            default=False,
        )
        if explicit:
            return True, "explicit_installation_capability"
        required = {
            "cp_interrupt_isactive",
            "cp_interrupt_duration",
            "cp_interrupt_version",
        }
        if not isinstance(data, dict) or not required.issubset(data):
            return False, "cp_readback_fields_missing"
        try:
            duration = float(data.get("cp_interrupt_duration"))
        except (TypeError, ValueError):
            return False, "cp_duration_invalid"
        if not math.isfinite(duration) or duration < 0.0:
            return False, "cp_duration_invalid"
        active = data.get("cp_interrupt_isactive")
        if isinstance(active, bool):
            active_valid = True
        elif isinstance(active, (int, float)) and not isinstance(active, bool):
            active_valid = math.isfinite(float(active)) and float(active) in (0.0, 1.0)
        else:
            active_valid = str(active or "").strip().lower() in {
                "0", "1", "false", "true", "off", "on",
            }
        if not active_valid:
            return False, "cp_active_state_invalid"
        version = str(data.get("cp_interrupt_version") or "").strip()
        if version not in ("0V", "-12V"):
            return False, "cp_version_unconfirmed"
        return True, "connect_php_cp_readback"

    def _update_from_connect_status(self, data):
        phase_payload_valid, phase_payload_error = (
            self._valid_connect_phase_payload(data)
        )
        cp_interrupt_supported, cp_capability_source = (
            self._cp_interrupt_capability(data)
        )
        plug_state = self._boolish(data.get("plug_state"))
        charge_state = self._boolish(data.get("charge_state"))
        locked = bool(plug_state or self._boolish(data.get("locked")) or self._boolish(data.get("plug_locked")))
        live_vehicle_id = data.get("vehicle_id")
        live_rfid_tag = data.get("rfid_tag")
        stable_vehicle_identity_current = bool(
            str(live_vehicle_id or "").strip()
            or str(live_rfid_tag or "").strip()
        )
        powers = data.get("powers") if isinstance(data.get("powers"), list) else []
        p1 = self._float(powers[0], 0.0) if len(powers) > 0 else 0.0
        p2 = self._float(powers[1], 0.0) if len(powers) > 1 else 0.0
        p3 = self._float(powers[2], 0.0) if len(powers) > 2 else 0.0
        raw_power_w = self._float(data.get("power_all"), None)
        if raw_power_w is None:
            raw_power_w = p1 + p2 + p3
        phase_power_sum = p1 + p2 + p3
        phase_power_verified = phase_power_sum > 50.0
        if phase_power_verified:
            power_w = phase_power_sum
        elif charge_state and raw_power_w > 50.0:
            power_w = raw_power_w
        else:
            power_w = 0.0

        currents = data.get("currents") if isinstance(data.get("currents"), list) else []
        voltages = data.get("voltages") if isinstance(data.get("voltages"), list) else []
        phase_apparent = []
        phase_currents = []
        for idx in range(3):
            if idx < len(currents) and idx < len(voltages):
                current_a = abs(self._float(currents[idx], 0.0))
                phase_currents.append(current_a)
                phase_apparent.append(abs(current_a * self._float(voltages[idx], 0.0)))
            else:
                phase_currents.append(0.0)
                phase_apparent.append(0.0)
        apparent_power_va = sum(phase_apparent)
        power_factor = (phase_power_sum / apparent_power_va) if apparent_power_va > 50.0 and phase_power_sum > 0 else 0.0
        offered_current = self._float(data.get("offered_current"), 0.0)
        phases_actual = int(self._float(data.get("phases_actual"), 0.0))
        phases_target = int(self._float(data.get("phases_target"), 0.0))
        phases_in_use = int(self._float(data.get("phases_in_use"), 0.0))
        measured_phases = sum(1 for cur in currents if self._float(cur, 0.0) > 0.2)
        if measured_phases > 0:
            phases_in_use = measured_phases
        elif not charge_state and not phase_power_verified and apparent_power_va <= 50.0:
            # connect.php may report the configured/available phase set while
            # the EV is only offered current. Treat idle 0A/0W as no active
            # phases so the manager can fall back to a 1p start.
            phases_in_use = 0
        elif phases_in_use <= 0 and currents:
            phases_in_use = measured_phases
        display_phases = phases_in_use or phases_actual or phases_target

        imported_wh = self._float(data.get("imported"), self.state.get("imported_total_wh", 0.0))
        prev_plug = self.state.get("plug_state", False)
        plug_observed = bool(self.state.get("_plug_state_observed", False))
        effective_plug_state = bool(plug_state or locked or charge_state or power_w > 50.0 or live_vehicle_id or live_rfid_tag)
        restored_session = (
            self._openwb_pro_persisted_session_sample(live_vehicle_id or live_rfid_tag)
            if effective_plug_state
            else {"kwh": 0.0, "start_ts": 0}
        )
        restored_session_kwh = max(0.0, self._float(restored_session.get("kwh"), 0.0))
        restored_session_wh = restored_session_kwh * 1000.0
        restored_start_ts = self._openwb_pro_parse_session_ts(
            restored_session.get("start_ts")
        )
        restored_session_bound = restored_session_wh > 20.0 and restored_start_ts > 0
        if effective_plug_state and (
            not prev_plug
            or self.state.get("_session_start_wh") is None
            or self.state.get("_session_start_ts") is None
        ):
            if restored_session_bound:
                self.state["_session_start_wh"] = max(0.0, imported_wh - restored_session_wh)
                self.state["_session_start_ts"] = restored_start_ts
                self.state["session_kwh"] = restored_session_kwh
                logger.info(
                    f"[WB{self.wb_id}] openWB Pro: laufende Session mit "
                    f"{restored_session_kwh:.3f} kWh fortgefuehrt."
                )
            elif plug_observed and not prev_plug:
                self.state["_session_start_wh"] = imported_wh
                self.state["_session_start_ts"] = int(time.time())
                self.state["session_kwh"] = 0.0
                logger.info(f"[WB{self.wb_id}] openWB Pro: Auto eingesteckt, Session-Zaehler gestartet.")
        elif not effective_plug_state:
            self.state["_session_start_wh"] = None
            self.state["_session_start_ts"] = None
            self.state["_session_vehicle_id"] = None
            self.state["_session_rfid_tag"] = None
        self.state["_plug_state_observed"] = True

        if effective_plug_state:
            if live_vehicle_id:
                self.state["_session_vehicle_id"] = live_vehicle_id
            else:
                live_vehicle_id = self.state.get("_session_vehicle_id")
            if live_rfid_tag:
                self.state["_session_rfid_tag"] = live_rfid_tag
            else:
                live_rfid_tag = self.state.get("_session_rfid_tag")

        start = self.state.get("_session_start_wh")
        if start is not None and effective_plug_state:
            meter_session_kwh = max(0.0, (imported_wh - start) / 1000.0)
            self.state["session_kwh"] = max(meter_session_kwh, restored_session_kwh)
            if restored_session_kwh > meter_session_kwh + 0.02:
                self.state["_session_start_wh"] = max(0.0, imported_wh - restored_session_wh)
        session_wh_for_soc = max(0.0, self._float(self.state.get("session_kwh"), 0.0) * 1000.0, restored_session_wh)

        soc = data.get("soc_value")
        soc_ts = data.get("soc_timestamp")

        has_current_vehicle = bool(effective_plug_state)
        vehicle_id = live_vehicle_id if has_current_vehicle else None
        rfid_tag = live_rfid_tag if has_current_vehicle else None
        car_id = (vehicle_id or rfid_tag) if has_current_vehicle else None
        if not has_current_vehicle:
            car_name = ""
        elif rfid_tag:
            car_name = f"RFID {rfid_tag}"
        elif vehicle_id:
            car_name = str(vehicle_id)
        else:
            car_name = "openWB Pro"

        temp_raw = self._float(data.get("temp"), None)
        temp_c = None
        if temp_raw is not None:
            temp_c = round(temp_raw / 1000.0, 1) if temp_raw > 200 else round(temp_raw, 1)

        visible_current = offered_current if effective_plug_state else 0.0

        raw_api_version = data.get("version")
        try:
            api_version = max(0, int(float(raw_api_version)))
        except (TypeError, ValueError):
            api_version = 0
        self.state.update({
            "plug_state": effective_plug_state,
            "plug_state_raw": plug_state,
            "locked": locked,
            "charge_state": charge_state,
            "car": 2 if effective_plug_state else 1,
            "charging": bool(charge_state or phase_power_verified),
            "real_power_w": power_w,
            "phase_power_l1_w": p1,
            "phase_power_l2_w": p2,
            "phase_power_l3_w": p3,
            "phase_power_sum_w": phase_power_sum,
            "phase_power_verified": phase_power_verified,
            "phase_apparent_l1_va": phase_apparent[0],
            "phase_apparent_l2_va": phase_apparent[1],
            "phase_apparent_l3_va": phase_apparent[2],
            "phase_current_l1_a": phase_currents[0],
            "phase_current_l2_a": phase_currents[1],
            "phase_current_l3_a": phase_currents[2],
            "apparent_power_va": apparent_power_va,
            "power_factor": power_factor,
            "evse_current": visible_current,
            "amp": int(round(visible_current)) if visible_current > 0 else 0,
            "phases_in_use": int(phases_in_use),
            "phases_actual": int(phases_actual),
            "phases_target": int(phases_target),
            "can_switch_phases": bool(phase_payload_valid),
            "phase_switch_capability": (
                "official_connect_php"
                if phase_payload_valid
                else "connect_php_phase_contract_incomplete"
            ),
            "phase_switch_source": (
                "openwb_pro_connect_php"
                if phase_payload_valid
                else "fail_closed"
            ),
            "phase_connect_payload_valid": bool(phase_payload_valid),
            "phase_connect_payload_error": str(phase_payload_error or ""),
            "api_surface": "openwb_pro_connect_php",
            "pha": 56 if int(display_phases) >= 3 else (8 if int(display_phases) >= 1 else 0),
            "daily_imported_wh": imported_wh,
            "imported_total_wh": imported_wh,
            "chargemode_str": "instant" if visible_current >= 6 else "stop",
            "frc": 2 if visible_current >= 6 else 0,
            "car_name": car_name,
            "car_id": car_id,
            "vehicle_id": vehicle_id,
            "rfid_tag": rfid_tag,
            "vehicle_identity_current": bool(stable_vehicle_identity_current),
            "stable_vehicle_identity_current": bool(stable_vehicle_identity_current),
            "rfid_timestamp": data.get("rfid_timestamp"),
            "serial": data.get("serial"),
            "version": raw_api_version,
            "openwb_pro_api_version": api_version,
            "cp_interrupt_supported": bool(cp_interrupt_supported),
            "cp_interrupt_capability_source": str(cp_capability_source or ""),
            "automatic_start_cp_supported": False,
            "automatic_start_cp_capability_source": "explicit_vehicle_profile_required",
            "connect_php_payload_valid": True,
            "connect_php_payload_error": "",
            "v2g_ready": data.get("v2g_ready", 0),
            "evse_signaling": data.get("evse_signaling", ""),
            "offered_current_raw": offered_current,
            # Ausschließlich der erfolgreiche connect.php-GET bestätigt
            # die vom EVSE aktuell angebotene Stellgröße. Ein späterer
            # lokaler POST darf diesen Readback nicht optimistisch erneuern.
            "offered_current_confirmed": True,
            "offered_current_readback_ts": time.time(),
            "max_charge_power": data.get("max_charge_power", 0),
            "max_discharge_power": data.get("max_discharge_power", 0),
            "temp_c": temp_c,
            "cp_interrupt_isactive": data.get("cp_interrupt_isactive", 0),
            "cp_interrupt_duration": data.get("cp_interrupt_duration", 0),
            "cp_interrupt_version": data.get("cp_interrupt_version", ""),
        })
        self._update_vehicle_soc_estimate(
            soc,
            imported_wh,
            effective_plug_state,
            bool(charge_state or phase_power_verified),
            vehicle_id=vehicle_id,
            rfid_tag=rfid_tag,
            prev_plug=prev_plug,
            raw_soc_ts=soc_ts,
            restored_session_wh=session_wh_for_soc,
            restored_session_start_ts=restored_session.get("start_ts", 0),
        )
        self._write_openwb_pro_status()

    def _update_from_legacy_secc_status(self, data):
        port = data.get("port0", data)
        ev_present = self._boolish(self._dig(port, "ev_present", "0"))
        pluggable = bool(port.get("pluggable", False))
        charging = self._boolish(port.get("charging", "0"))
        plug_status = str(self._dig(port, "ci/charge/plug/status", "") or "").strip().lower()
        plug_lock_actual = self._dig(port, "plug_lock/state/actual", None)
        power_w = self._float(self._dig(port, "metering/power/active_total/actual"), 0.0) / 10.0
        offered_current = self._float(self._dig(port, "ci/evse/basic/offered_current_limit"), 0.0)
        evse_current = self._float(port.get("evse_current_limit"), offered_current)
        phase_actual = int(self._float(self._dig(port, "ci/evse/phase/actual"), 0.0))
        phase_target = int(self._float(self._dig(port, "ci/evse/phase/target"), 0.0))
        daily_imported = self._float(self._dig(port, "metering/energy/active_import/actual"), 0.0)

        phase_currents = [
            self._float(self._dig(port, f"metering/current/ac/l{i}/actual"), 0.0) / 1000.0
            for i in (1, 2, 3)
        ]
        measured_phases = sum(1 for cur in phase_currents if cur > 0.2)
        phases = measured_phases or phase_actual
        phase_power_verified = bool(power_w > 50.0 and measured_phases >= 1)
        real_power_w = power_w if (charging or phase_power_verified) else 0.0

        prev_plug = self.state.get("plug_state", False)
        plug_observed = bool(self.state.get("_plug_state_observed", False))
        plug_state = bool(ev_present or pluggable)
        locked = bool(plug_state and (
            plug_status == "locked"
            or self._boolish(plug_lock_actual)
            or str(plug_lock_actual).strip() == "1"
        ))
        if plug_state and plug_observed and not prev_plug:
            self.state["_session_start_wh"] = daily_imported
            self.state["_session_start_ts"] = int(time.time())
            self.state["session_kwh"] = 0.0
            logger.info(f"[WB{self.wb_id}] openWB Pro: Auto eingesteckt, Session-Zaehler gestartet.")
        elif not plug_state:
            self.state["_session_start_wh"] = None
            self.state["_session_start_ts"] = None
        self.state["_plug_state_observed"] = True

        start = self.state.get("_session_start_wh")
        if start is not None and plug_state:
            self.state["session_kwh"] = max(0.0, (daily_imported - start) / 1000.0)

        car_soc = self._dig(port, "ci/ev/soc/actual")
        car_soc_ts = self._dig(port, "ci/ev/soc/timestamp")

        visible_current = offered_current
        if visible_current <= 0 and (charging or power_w > 50.0):
            visible_current = evse_current
        self.state.update({
            "car": 2 if plug_state else 1,
            "amp": int(round(visible_current)),
            "pha": 56 if phases >= 3 else (8 if phases >= 1 else 0),
            "charging": bool(charging or phase_power_verified),
            "frc": 2 if visible_current >= 6 else 0,
            "plug_state": plug_state,
            "locked": locked,
            "charge_state": bool(charging or phase_power_verified),
            "real_power_w": real_power_w,
            "phase_power_sum_w": real_power_w,
            "phase_power_verified": phase_power_verified,
            "phase_current_l1_a": phase_currents[0],
            "phase_current_l2_a": phase_currents[1],
            "phase_current_l3_a": phase_currents[2],
            "evse_current": visible_current,
            "phases_in_use": int(phases),
            "phases_actual": int(phase_actual),
            "phases_target": int(phase_target or phases),
            "can_switch_phases": False,
            "phase_switch_capability": "legacy_secc_diagnostic",
            "phase_switch_source": "legacy_secc_diagnostic_only",
            "phase_connect_payload_valid": False,
            "phase_connect_payload_error": "legacy_secc_fallback",
            "api_surface": "legacy_secc_diagnostic",
            "cp_interrupt_supported": False,
            "cp_interrupt_capability_source": "legacy_secc_diagnostic_only",
            "automatic_start_cp_supported": False,
            "automatic_start_cp_capability_source": "legacy_secc_diagnostic_only",
            "connect_php_payload_valid": False,
            "connect_php_payload_error": "legacy_secc_fallback",
            "offered_current_confirmed": False,
            "offered_current_readback_ts": 0.0,
            "daily_imported_wh": daily_imported,
            "imported_total_wh": daily_imported,
            "chargemode_str": "instant" if visible_current >= 6 else "stop",
        })
        self._update_vehicle_soc_estimate(
            car_soc,
            daily_imported,
            plug_state,
            bool(charging or phase_power_verified),
            vehicle_id=None,
            rfid_tag=None,
            prev_plug=prev_plug,
            raw_soc_ts=car_soc_ts,
        )
        self._write_openwb_pro_status()

    def get_control_handoff_status(self):
        """Liest genau einmal die offizielle Pro-Fläche, ohne Legacy-Fallback.

        Der Modus-0-Handoff verwendet diesen schmalen Readback, weil ein
        ``get_status()`` bei ungültigem ``connect.php`` sonst noch einen
        zweiten HTTP-GET auf ``/api/secc`` auslösen würde. Eine unvollständige
        Antwort bleibt deshalb für die Übergabe strikt unbestätigt.
        """

        data = self._get_json(self.status_url)
        connect_valid, connect_error = self._valid_connect_status_payload(data)
        if connect_valid:
            if self._heartbeat_enabled_assumed:
                # Laut Hersteller verlängert ausschließlich die erfolgreiche
                # connect.php-Abfrage die aktive Heartbeat-Lease. Fallback-
                # Diagnose, Fehler und Nicht-JSON-Antworten zählen nicht.
                self._last_heartbeat_lease_refresh_ts = time.time()
            self._update_from_connect_status(data)
            return self._sanitize_measurement_status(self.state)
        if isinstance(data, dict):
            self.state.update({
                "can_switch_phases": False,
                "phase_switch_capability": "invalid_connect_php_payload",
                "phase_switch_source": "fail_closed",
                "phase_connect_payload_valid": False,
                "phase_connect_payload_error": str(connect_error or "invalid_payload"),
                "api_surface": "invalid_connect_php_payload",
                "cp_interrupt_supported": False,
                "cp_interrupt_capability_source": "invalid_connect_php_payload",
                "automatic_start_cp_supported": False,
                "automatic_start_cp_capability_source": "invalid_connect_php_payload",
                "connect_php_payload_valid": False,
                "connect_php_payload_error": str(connect_error or "invalid_payload"),
                "offered_current_confirmed": False,
                "offered_current_readback_ts": 0.0,
            })
            logger.warning(
                f"[WB{self.wb_id}] openWB Pro connect.php unvollständig; "
                f"Capability und Heartbeat-Lease bleiben gesperrt ({connect_error})."
            )

        return None

    def get_status(self):
        connect_status = self.get_control_handoff_status()
        if connect_status is not None:
            return connect_status

        data = self._get_json(self.fallback_status_url)
        if isinstance(data, dict):
            self._update_from_legacy_secc_status(data)
            return self._sanitize_measurement_status(self.state)

        logger.error(f"[WB{self.wb_id}] openWB Pro Status nicht lesbar (/connect.php, /api/secc)")
        return None

    def set_amp_and_state(self, target_amp, force_state=None, *args, **kwargs):
        if not command_gate.allow_command(
            self,
            action="openwb_pro_set_amp_and_state",
            payload={"target_amp": target_amp, "force_state": force_state},
        ):
            # Ein vom zentralen Ausgangsgate blockierter Schreibzug hat das
            # Gerät nicht erreicht. Er darf deshalb weder als erfolgreicher
            # STOP noch als übernommener Sollstrom an den Manager zurücklaufen.
            return False
        try:
            raw_amp = float(target_amp or 0.0)
        except (TypeError, ValueError):
            raw_amp = 0.0
        if force_state == 1 or raw_amp < 0.5:
            amp = 0.0
        else:
            amp = _quantize_current_amp(
                raw_amp,
                step=self.current_step_amp,
                max_amp=getattr(self, "max_amp", 16.0),
            )
        now = time.time()
        control_key = ("ampere", amp)
        is_keepalive = self._last_control_key == control_key
        repeat_after_s = 5.0 if amp >= 6.0 else 20.0
        emergency_zero = bool(
            amp == 0.0
            and force_state == 1
            and command_gate.emergency_stop_scope_active(self)
        )
        if (
            is_keepalive
            and now - self._last_control_ts < repeat_after_s
            and not emergency_zero
        ):
            logger.debug(f"[WB{self.wb_id}] openWB Pro Sollstrom gedrosselt: {amp:.1f}A")
            # Kein POST, also auch kein neuer Ausgangsbeleg. Der Manager darf
            # den alten Gerätezustand nur aus frischem Readback übernehmen.
            return False

        if amp >= 6.0:
            if not self._ensure_heartbeat_enabled(now):
                return False

        # Match openWB's openWB-Pro module: set_current writes only ampere.
        # Start verification, wakeup and CP retries are manager policy and
        # must be sent explicitly via trigger_cp_interrupt().
        ok = self._post_control({"ampere": f"{amp:.1f}" if amp else "0"})
        if ok:
            self.state["amp"] = int(round(amp))
            self.state["evse_current"] = amp
            self.state["offered_current_raw"] = amp
            self.state["offered_current_confirmed"] = False
            self.state["current_step_amp"] = self.current_step_amp
            self.state["fractional_current_supported"] = True
            self.state["chargemode_str"] = "instant" if amp >= 6 else "stop"
            self.state["frc"] = 2 if amp >= 6 else 0
            self._last_control_key = control_key
            self._last_control_ts = now
            if is_keepalive and amp >= 6.0:
                logger.debug(f"[WB{self.wb_id}] openWB Pro Keepalive: {amp:.1f}A")
            else:
                logger.info(f"[WB{self.wb_id}] openWB Pro Sollstrom: {amp:.1f}A")
        return bool(ok)

    def set_phases(self, phases, require_wire_receipt=False, *args, **kwargs):
        if not command_gate.allow_command(
            self,
            action="openwb_pro_set_phases",
            payload={"phases": phases},
        ):
            return False
        phases = 1 if int(phases) == 1 else 3
        now = time.time()
        phase_key = ("phase", phases)
        reported_target = 0
        try:
            reported_target = int(float(self.state.get("phases_target", 0) or 0))
        except (TypeError, ValueError):
            reported_target = 0
        fresh_target_readback = bool(
            self.state.get("driver_status_valid") is True
            and self.state.get("driver_status_stale") is not True
            and self.state.get("driver_status_degraded") is not True
            and reported_target == phases
        )
        if fresh_target_readback and not bool(require_wire_receipt):
            # Ein frisches identisches Ziel ist bereits vom Gerät angenommen.
            # Besonders nach einem Manager-Neustart darf ein laufender
            # Pro-eigener CP-/Phasenwechsel nicht durch denselben POST erneut
            # gestartet oder verlängert werden.
            first_readback_adoption = self._last_phase_key != phase_key
            self._last_phase_key = phase_key
            self._last_phase_ts = now
            self.state["pha"] = 56 if phases == 3 else 8
            if first_readback_adoption:
                logger.info(
                    f"[WB{self.wb_id}] openWB Pro Phasenziel bereits frisch bestätigt: "
                    f"{phases}p, kein erneuter Wire-POST"
                )
            return False
        if (
            not bool(require_wire_receipt)
            and self._last_phase_key == phase_key
            and now - self._last_phase_ts < 20.0
        ):
            logger.debug(f"[WB{self.wb_id}] openWB Pro Phasenziel gedrosselt: {phases}p")
            return False
        # Laut openWB-Pro-Standalone-API ist phasetarget der offizielle
        # Schalter. Die Pro uebernimmt die Pause/Signalisierung zum Fahrzeug
        # selbst; ein manueller CP-Interrupt waere hier doppelt.
        ok = self._post_control({"phasetarget": str(phases)})
        if ok:
            self._last_phase_key = phase_key
            self._last_phase_ts = now
            self._last_control_key = None
            # Der erfolgreiche POST ist nur ein Wire-Beleg. phases_target,
            # pha und Istphasen dürfen ausschließlich ein späterer echter
            # connect.php-GET aktualisieren.
            self._last_commanded_phase_target = phases
            logger.info(f"[WB{self.wb_id}] openWB Pro Phasenziel: {phases}p")
        return ok

    def set_heartbeat(self, enabled=True, now=None):
        if not command_gate.allow_command(
            self,
            action="openwb_pro_set_heartbeat",
            payload={"enabled": bool(enabled)},
        ):
            return False
        ok = self._post_control({"heartbeatenabled": "1" if enabled else "0"})
        if ok:
            self._heartbeat_enabled_assumed = bool(enabled)
            refresh_ts = float(now) if enabled and now is not None else (time.time() if enabled else 0.0)
            self._last_heartbeat_lease_refresh_ts = refresh_ts
        return ok

    def _ensure_heartbeat_enabled(self, now=None):
        now = time.time() if now is None else float(now)
        if (
            self._heartbeat_enabled_assumed
            and 0.0 <= now - self._last_heartbeat_lease_refresh_ts
            <= self.HEARTBEAT_LEASE_REFRESH_MAX_S
        ):
            return True
        ok = self.set_heartbeat(True, now=now)
        return bool(ok)

    def trigger_cp_interrupt(self, duration=None, version=None):
        if not command_gate.allow_command(
            self,
            action="openwb_pro_cp_interrupt",
            payload={"duration": duration, "version": version},
        ):
            return False
        payload = {"cp_interrupt": "true"}
        if duration is not None:
            try:
                payload["cp_interrupt_duration"] = str(max(1, int(float(duration))))
            except (TypeError, ValueError):
                pass
        if version in ("0V", "-12V"):
            payload["cp_interrupt_version"] = version
        return self._post_control(payload)

    def set_pv_mode(self):
        # Die Pro kennt keinen PV-Modus wie der openWB-Controller. PV-Logik bleibt im Manager.
        return True

    def release_to_default(self, max_amp=16):
        """openWB Pro Standalone sicher freigeben, ohne blind 32A anzubieten."""
        ok_stop = self.set_amp_and_state(0, force_state=1)
        logger.info(
            f"[WB{self.wb_id}] openWB Pro Default-Freigabe: Standalone sicher gestoppt "
            f"(stop={ok_stop})"
        )
        # Ein fehlgeschlagener 0-A-Wirebeleg darf weder durch einen separaten
        # Heartbeat kaschiert noch als erfolgreiche Freigabe gelatcht werden.
        return bool(ok_stop)


# ===========================================================================
# Factory
# ===========================================================================
DISABLED_WALLBOX_TYPES = {
    "none",
    "disabled",
    "deaktiviert",
    "aus",
    "keine",
    "keine_wallbox",
    "no_wallbox",
    "off",
    "false",
    "no",
    "0",
    "-1",
}

def create_charger(wb_type, ip, wb_id, config=None):
    """Erstellt den passenden Treiber für den konfigurierten Wallbox-Typ."""
    wb_type = str(wb_type).strip().lower()
    if not wb_type or wb_type in DISABLED_WALLBOX_TYPES:
        return None
    native_types = (
        "native", "e3dc", "e3dc_easy", "e3dc_easy_connect", "e3dc_legacy",
        "e3dc_efy", "e3dc_auto", "e3dc_multi", "e3dc_multi_connect",
        "e3dc_multi_connect_ii",
    )
    if not ip and wb_type not in ("dummy", *native_types):
        return None
    config = dict(config or {})
    config["_e3dc_configured_type"] = wb_type
    if wb_type == "go-e":
        return _bind_configured_driver_limits(GoECharger(ip, wb_id), config, wb_id)
    if wb_type == "openwb":
        return _bind_configured_driver_limits(OpenWBCharger(ip, wb_id, config), config, wb_id)
    if wb_type in ("openwb_pro", "openwb-pro", "openwbpro"):
        return _bind_configured_driver_limits(OpenWBProCharger(ip, wb_id, config), config, wb_id)
    if wb_type in (
        "e3dc_auto", "e3dc_efy", "e3dc_easy", "e3dc_easy_connect",
        "e3dc_multi", "e3dc_multi_connect", "e3dc_multi_connect_ii",
    ):
        return _bind_configured_driver_limits(
            E3DCMultiConnectCharger(ip, wb_id, config),
            config,
            wb_id,
        )
    if wb_type in ("native", "e3dc", "e3dc_legacy"):
        return _bind_configured_driver_limits(E3DCCharger(ip, wb_id, config), config, wb_id)
    if wb_type == "dummy":
        return _bind_configured_driver_limits(DummyCharger(ip, wb_id), config, wb_id)
    logger.warning(f"Unbekannter Wallbox-Typ: '{wb_type}'")
    return None
