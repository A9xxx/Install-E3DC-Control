"""
E3DC-Control Wallbox Manager - Wallbox Treiber.
Enthaelt alle Treiber-Klassen:
  - WallboxDriver   (abstrakte Basisklasse)
  - GoECharger      (go-eCharger V2/V3 API)
  - OpenWBCharger   (openWB 2.x HTTP SimpleAPI)
  - E3DCCharger     (E3DC native Wallbox ueber RSCP)
  - create_charger() Factory-Funktion
"""
import os
import json
import time
import logging
import re
import base64

import requests as _requests

from .config import logger, RAMDISK_DIR
from . import command_gate

# paho-mqtt ist nur noch fuer alte Installationen relevant.
# Der openWB-2.x-Treiber nutzt bewusst ausschliesslich HTTP simpleAPI.
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


def _quantize_current_amp(target_amp, *, step=1.0, min_amp=6.0, max_amp=32.0):
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
    for key, value in payload.items():
        match = re.match(r"^chargepoint_(\d+)$", str(key))
        if not match or not isinstance(value, dict):
            continue
        cp_id = int(match.group(1))
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
    """Abstrakte Basisklasse fuer alle Wallbox-Treiber."""

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

    def release_to_default(self, max_amp=32):
        """Gibt die Wallbox in einen nutzbaren lokalen Default zurueck."""
        return False


# ===========================================================================
# go-eCharger (V2 / V3 API)
# ===========================================================================
class GoECharger(WallboxDriver):
    """Treiber fuer go-eCharger ueber HTTP V2/V3 API."""

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
            return True
        amp = max(6, min(32, int(target_amp or 6)))
        params = f"amp={amp}"
        if force_state is not None:
            params += f"&frc={int(force_state)}"
        url = f"http://{self.ip}/api/set?{params}"
        try:
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

    def release_to_default(self, max_amp=32):
        """go-e: neutralen FRC-Modus mit vollem konfiguriertem Strom herstellen."""
        return self.set_amp_and_state(max_amp, force_state=0)


# ===========================================================================
# OpenWBCharger (openWB 2.x: HTTP SimpleAPI + Modbus Secondary)
# ===========================================================================
class OpenWBCharger(WallboxDriver):
    """Treiber fuer openWB Series 2.

    Die normale openWB ist ein eigener Energiemanager. Standard bleibt der
    evcc/openWB-Secondary-Pfad ueber Sollstrom plus Heartbeat. Wird
    wb_openwb_primary_enable gesetzt, bleibt openWB Primary und E3DC-Control
    schaltet nur die openWB-Modi PV/Sofort/Stop per simpleAPI. Aktive
    Stromvorgaben sind dabei ein Primary-Direktpfad über openWB-Sofortladen
    (chargecurrent); openWB-SoC- und Energiemengenlimits bleiben wirksam.

    Status lesen: GET  simpleapi.php?get_chargepoint_all=<ID>
    Steuern:      HTTP-V1-Secondary-Topics; optional Modbus Secondary als
                  Rueckfall, wenn der HTTP-Pfad nicht erreichbar ist.
    """

    def __init__(self, ip, wb_id=1, config=None):
        super().__init__(ip, wb_id)
        config = config or {}
        self.config = config
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
            'state_text':        '',
            'fault_text':        '',
            'fault_state':       0,
            'manual_lock':       False,
            'min_current':       0.0,
            'pv_charging_min_current': 0.0,
            'instant_charging_current': 0.0,
            'instant_charging_limit': '',
            'instant_charging_soc': 0.0,
            'session_kwh':       0.0,
            '_session_start_wh': None,
            'cp_id':             '',
            'chargepoint_detection_source': '',
            # Fahrzeug-Info aus connected_vehicle/info
            'car_name':          '',
            'car_id':            None,
            'vehicle_id':        None,
            'rfid_tag':          None,
            'car_capacity_kwh':  0.0,
            'car_consumption_kwh_100km': 0.0,
            'car_range':         0.0,
            'car_range_source':  '',
            'car_charged_range': 0.0,
        }

        # CP-ID aus Topic-Prefix extrahieren
        import re
        cfg_prefix = config.get(f"wb{wb_id}_topic_prefix", "")
        self.cp_id = ""
        if cfg_prefix:
            m = re.search(r'(?:chargepoint|lp)[/\\](\d+)', cfg_prefix)
            if m:
                self.cp_id = int(m.group(1))
        # Alternativ direkt aus Config
        cp_cfg = config.get(f"wb_native_cp_id", None)
        if cp_cfg is not None and str(cp_cfg).strip().isdigit():
            self.cp_id = int(cp_cfg)
        elif cp_cfg == "":
            self.cp_id = ""

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
        self._last_command_key = None
        self._last_command_ts = 0.0

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
        legacy_mqtt = str(
            self.config.get("openwb_mqtt_legacy_enable", self.config.get("wb_openwb_mqtt_legacy_enable", "0"))
        ).strip().lower() in ("1", "true", "yes", "on")
        mqtt_module = _get_mqtt_module() if legacy_mqtt else None
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
        self.state["cp_id"] = self.cp_id
        self.state["chargepoint_detection_source"] = str(source or "simpleapi_auto")
        logger.info(
            f"[WB{self.wb_id}] openWB Auto-Ladepunkt übernommen: "
            f"CP={self.cp_id} (vorher {old_cp if old_cp != '' else 'AUTO'})"
        )
        return True

    def _http_headers(self, extra=None):
        """Gemeinsame HTTP-Header fuer openWB simpleAPI/V1, optional mit Basic Auth."""
        headers = dict(extra or {})
        if self.http_user:
            token = base64.b64encode(f"{self.http_user}:{self.http_pass}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        return headers

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        rc = reason_code if isinstance(reason_code, int) else (0 if str(reason_code) == 'Success' else 1)
        if rc == 0:
            self.state["mqtt_reconnect_backoff_s"] = 0
            self.state["mqtt_reconnect_backoff_max_s"] = 60
            self.state["mqtt_connected"] = True
            logger.info(f"[WB{self.wb_id}] MQTT: Verbunden mit openWB ({self.ip}), CP={self.cp_id}")
            topics = [
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
                f"{self.native_prefix}/connected_vehicle/soc",   # Auto-SoC
                f"{self.native_prefix}/connected_vehicle/range",  # Auto-Reichweite
                f"{self.native_prefix}/connected_vehicle/info",   # Auto-Name, ID, Kapazitaet
                f"{self.simpleapi_prefix}/soc/range",
                f"{self.simpleapi_prefix}/chargemode",
                f"openWB/chargepoint{self.cp_suffix}/config",
            ]
            for t in topics:
                client.subscribe(t)
            logger.info(f"[WB{self.wb_id}] MQTT: {len(topics)} Topics abonniert (incl. Auto-SoC)")
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

            # --- Auto-SoC aus openWB (MQTT JSON) ---
            if topic == f"{self.native_prefix}/connected_vehicle/soc":
                try:
                    soc_data  = json.loads(payload_str)
                    car_soc   = float(soc_data.get("soc", 0))
                    if car_soc > 0:
                        self.state['car_soc'] = car_soc
                        charged_range = float(soc_data.get("range_charged", 0) or 0)
                        if charged_range > 0:
                            self.state['car_charged_range'] = charged_range
                        range_val = float(soc_data.get("range", 0) or 0)
                        if range_val > 0:
                            self.state['car_range'] = range_val
                            self.state['car_range_source'] = 'mqtt_total'
                        openwb_ts = soc_data.get("timestamp", None)
                        soc_ts    = int(openwb_ts) if openwb_ts else int(time.time())
                        is_plugged = self.state.get('plug_state', False)
                        soc_age_h  = (time.time() - soc_ts) / 3600.0

                        self._write_manual_soc(car_soc, is_plugged, soc_ts, source="openwb_mqtt")
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
                        self.state['car_range'] = range_val
                        self.state['car_range_source'] = 'mqtt_total'
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
                self.state['plug_state'] = val
                self.state['car']        = 2 if val else 1
                if val and not prev_plug:
                    self.state['_session_start_wh'] = self.state['daily_imported_wh']
                    self.state['session_kwh']        = 0.0
                    logger.info(f"[WB{self.wb_id}] Auto eingesteckt! Session-Zaehler gestartet.")
                elif not val:
                    self.state['_session_start_wh'] = None
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
                start = self.state.get('_session_start_wh')
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
        """Ladepunktnummer fuer die openWB simpleAPI."""
        return str(self.cp_id) if self.cp_id != "" else "auto"

    def _primary_set_chargemode(self, mode: str) -> bool:
        """Schaltet openWB Software im Primary-Betrieb auf PV/Sofort/Stop."""
        import urllib.parse

        mode = str(mode or "").strip().lower()
        if mode not in ("instant", "pv", "stop"):
            logger.warning(f"[WB{self.wb_id}] openWB Primary: ungueltiger Modus {mode!r}")
            return False
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

    def _primary_set_chargecurrent(self, target_amp) -> bool:
        """Setzt den Sofortlade-Strom fuer openWB Primary."""
        import urllib.parse

        amp = _quantize_current_amp(target_amp, step=self.current_step_amp)
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
        """Aktiver openWB-Primary-Eingriff: Sofortladen mit Stromvorgabe."""
        raw = float(target_amp or 0)
        if raw < 0.5:
            return self._primary_set_chargemode("stop")
        amp = max(6.0, min(32.0, raw))
        current_ok = self._primary_set_chargecurrent(amp)
        mode_ok = self._primary_set_chargemode("instant")
        if current_ok and mode_ok:
            self.state["chargemode_str"] = "instant"
        return bool(current_ok and mode_ok)

    def _http_v1_post(self, topic: str, message=None):
        """Schreibt/liest openWB HTTP-API V1 Topics (Port 8443)."""
        import ssl
        import urllib.request
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
        connect.php die robustere Wahrheit fuer Leistung, Stecker und Ladung.
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
        """IP-Adresse des steuernden Systems fuer openWB-Secondary-Heartbeat."""
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
        if self.modbus_enabled:
            logger.info(f"[WB{self.wb_id}] openWB HTTP-V1-Heartbeat nicht verfuegbar, nutze Modbus als Rueckfall.")
            ok = self._modbus_secondary_heartbeat()
            self.state["last_heartbeat_ok"] = bool(ok)
            return ok
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
        with socket.create_connection((self.ip, int(self.modbus_port)), timeout=2.0) as sock:
            sock.settimeout(2.0)
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
            # Heartbeat-Zaehler zurueck, solange Heartbeat aktiv ist.
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
        begrenzt. Die openWB entscheidet selbst ueber PV-Logik, Schaltpause und
        Phasen; E3DC-Control gibt nur das verfuegbare Budget weiter.
        """
        amp = _quantize_current_amp(target_amp, step=self.current_step_amp)
        amp_payload = _amp_api_value(amp)
        amp_text = _amp_label(amp)
        self.state["last_command_amp"] = amp
        self.state["last_command_ts"] = int(time.time())
        self.state["current_step_amp"] = self.current_step_amp
        self.state["fractional_current_supported"] = self.current_step_amp < 1.0
        hb_ok = self._secondary_heartbeat()
        result = self._http_v1_post(
            f"openWB/set/internal_chargepoint/{self.api_duo_num}/data/set_current",
            amp_payload,
        )
        ok = bool(result and result.get("status") == "success")
        if ok:
            self.state["amp"] = int(round(amp))
            self.state["evse_current"] = amp
            self.state["chargemode_str"] = "secondary_current"
            self.state["api_surface"] = "openwb_secondary_set_current_heartbeat"
            if amp <= 0:
                self.state["charging"] = False
            self._set_control_state(
                "set_current_accepted",
                "Sollstrom übernommen",
                f"openWB Secondary hat {amp_text} A per HTTP V1 angenommen; Heartbeat {'ok' if hb_ok else 'nicht bestätigt'}.",
                "success",
                amp=amp,
                ok=True,
            )
            logger.debug(f"[WB{self.wb_id}] openWB Secondary HTTP-V1: {amp_text}A (heartbeat={hb_ok})")
            return True

        if self.modbus_enabled:
            logger.info(f"[WB{self.wb_id}] openWB HTTP-V1-set_current fehlgeschlagen, versuche Modbus Secondary.")
            hb_ok = hb_ok or self._modbus_secondary_heartbeat()
            try:
                ok = self._modbus_write_register(10171, int(round(amp * 100)))
            except Exception as e:
                logger.warning(f"[WB{self.wb_id}] openWB Modbus-Strom {amp_text}A fehlgeschlagen: {e}")
                ok = False
            if ok:
                self.state["amp"] = int(round(amp))
                self.state["evse_current"] = amp
                self.state["chargemode_str"] = "secondary_current"
                self.state["api_surface"] = "openwb_secondary_modbus"
                if amp <= 0:
                    self.state["charging"] = False
                self._set_control_state(
                    "set_current_accepted",
                    "Sollstrom übernommen",
                    f"openWB Secondary hat {amp_text} A per Modbus angenommen; Heartbeat {'ok' if hb_ok else 'nicht bestätigt'}.",
                    "success",
                    amp=amp,
                    ok=True,
                )
                logger.debug(f"[WB{self.wb_id}] openWB Modbus Secondary: {amp_text}A (heartbeat={hb_ok})")
                return True
        self._set_control_state(
            "set_current_failed",
            "Sollstrom nicht angenommen",
            f"openWB Secondary hat {amp_text} A nicht bestätigt; Heartbeat {'ok' if hb_ok else 'nicht bestätigt'}. E3DC-Control nutzt die gemessene Wallboxleistung nur als Last.",
            "warning",
            amp=amp,
            ok=False,
        )
        logger.warning(
            f"[WB{self.wb_id}] openWB Secondary set_current={amp_text}A fehlgeschlagen: "
            f"current={result}, heartbeat={hb_ok}"
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
    def _text_value(value, default=""):
        if value in (None, "null"):
            return default
        return str(value).strip().strip('"')

    def _write_manual_soc(self, car_soc, plugged, soc_ts=None, source="openwb_http"):
        if car_soc <= 0:
            return
        soc_ts = int(soc_ts or time.time())
        soc_age_h = max(0.0, (time.time() - soc_ts) / 3600.0)
        soc_file = os.path.join(RAMDISK_DIR, f"manual_soc_wb{self.wb_id}.json")
        payload_out = {
            "soc": car_soc,
            "ts": soc_ts,
            "source": source,
            "plugged": bool(plugged),
            "age_h": round(soc_age_h, 1),
            "wb": self.wb_id,
        }
        try:
            tmp = soc_file + ".tmp"
            with open(tmp, "w") as sf:
                json.dump(payload_out, sf)
            os.replace(tmp, soc_file)
        except Exception as e:
            logger.debug(f"[WB{self.wb_id}] Auto-SoC schreiben fehlgeschlagen: {e}")

    def _update_from_simpleapi(self, payload):
        """Normalisiert get_chargepoint_all=<ID> auf das interne Statusformat."""
        if not isinstance(payload, dict):
            return False

        cp_data = payload
        detected_cp_id = None
        if not any(k in payload for k in ("power", "plug_state", "charge_state", "evse_current")):
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
        if not isinstance(cp_data, dict):
            return False
        if self.cp_id == "" and detected_cp_id is not None:
            self._set_runtime_chargepoint_id(detected_cp_id)
        self._apply_role_detection(cp_data)

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

        prev_plug = self.state.get("plug_state", False)
        daily_imported = self._float_value(cp_data.get("daily_imported"), self.state.get("daily_imported_wh", 0.0))
        effective_plug_state = bool(plug_state or locked or charge_state or power_w > 50.0)
        if not effective_plug_state:
            evse_current = 0.0
            phases = 0
        if effective_plug_state and not prev_plug:
            self.state["_session_start_wh"] = daily_imported
            self.state["session_kwh"] = 0.0
            logger.info(f"[WB{self.wb_id}] Auto eingesteckt! Session-Zaehler gestartet.")
        elif not effective_plug_state:
            self.state["_session_start_wh"] = None
            self.state["_session_vehicle_id"] = None
            self.state["_session_rfid_tag"] = None

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

        car_soc = self._float_value(cp_data.get("soc", cp_data.get("pro_soc")), 0.0)
        if car_soc > 0:
            self.state["car_soc"] = car_soc
            self._write_manual_soc(car_soc, effective_plug_state)
        charged_range = self._float_value(cp_data.get("range_charged"), 0.0)
        if charged_range > 0:
            self.state["car_charged_range"] = charged_range
        car_range = self._float_value(
            cp_data.get("range", cp_data.get("vehicle_range", cp_data.get("remaining_range"))),
            0.0
        )
        if car_range > 0:
            self.state["car_range"] = car_range
            self.state["car_range_source"] = "http_total"

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
            'locked':            bool(self.state.get('locked', self.state['plug_state'])),
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
            'daily_imported_wh': round(self.state['daily_imported_wh'], 0),
            'imported_total_wh': round(self.state['imported_total_wh'], 0),
            'chargemode':        self.state['chargemode_str'],
            'chargepoint_name':  self.state.get('chargepoint_name', ''),
            'charge_template_name': self.state.get('charge_template_name', ''),
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
            'cp_id':             self.cp_id,
            'wb_id':             self.wb_id,
            'ts':                int(time.time()),
            'car_soc':           self.state.get('car_soc', 0),
            'car_range':         self.state.get('car_range', 0),
            'range_km':          self.state.get('car_range', 0),
            'car_range_source':  self.state.get('car_range_source', ''),
            'car_charged_range': self.state.get('car_charged_range', 0),
            'charged_range_km':  self.state.get('car_charged_range', 0),
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
        if payload is not None:
            self._update_from_simpleapi(payload)
            return self._sanitize_measurement_status(self.state)
        self.state["driver_status_valid"] = False
        self.state["driver_status_stale"] = True
        self.state["driver_status_reason"] = "openwb_http_status_unavailable"
        return None

    def set_amp_and_state(self, target_amp, force_state=None):
        """Steuert openWB 2.x je nach Konfigurationsrolle.

        Standard: Secondary-Sollstrom + Heartbeat.
        openWB Primary Opt-in: PV/Sofort/Stop per simpleAPI, openWB regelt den
        Ladepunkt weiter selbst.
        """
        if not command_gate.allow_command(
            self,
            action="openwb_set_amp_and_state",
            payload={"target_amp": target_amp, "force_state": force_state},
        ):
            return True
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
            return True
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

    def release_to_default(self, max_amp=32):
        """openWB sauber loslassen."""
        if self._commands_temporarily_blocked():
            return False
        if self.primary_mode_enabled:
            ok = self._primary_set_chargemode("pv")
            logger.info(f"[WB{self.wb_id}] Default-Freigabe: openWB Primary zurueck auf PV (ok={ok})")
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
            return True
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
            return True
        logger.warning(
            f"[WB{self.wb_id}] Phasenumschaltung ignoriert: normale openWB "
            f"wird nur ueber Sollstrom+Heartbeat gefuehrt."
        )
        return False


# ===========================================================================
# E3DCCharger (native E3DC Wallbox per RSCP)
# ===========================================================================
class E3DCCharger(WallboxDriver):
    """Treiber fuer native E3DC Wallbox ueber RSCP."""

    def __init__(self, ip, wb_id, config):
        super().__init__(ip, wb_id)
        self.config = config or {}
        self.server_ip   = self.config.get("server_ip",    "127.0.0.1")
        self.server_port = int(self.config.get("server_port", 5033))
        self.user        = self.config.get("e3dc_user",    "")
        self.password    = self.config.get("e3dc_password", "")
        self.aes_password = self.config.get("aes_password", "")
        self.wb_index    = int(wb_id) - 1
        self.conn        = None
        self.last_connect_time = 0
        import threading
        self.lock = threading.Lock()
        # Default ist echte Funkstille. Erst set_amp_* aktiviert SET_EXTERN-Heartbeat.
        self.last_amp = None
        self.last_force_state = None
        self.external_suspended = True
        self.real_charging = False
        self.sonnenmodus = False  # True = E3DC steuert autonom (Mode=1), False = Python-Kontrolle (Mode=2)
        self.last_stop_toggle_ts = 0.0
        self.rscp_error_count = 0
        self.rscp_last_error = ""
        self.rscp_last_error_context = ""
        self.rscp_last_error_ts = 0.0
        self.rscp_last_ok_ts = 0.0
        self.rscp_last_ok_context = ""
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb_thread.start()

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
        driver_variant = getattr(self, "driver_variant", "e3dc_native")
        phase_capability = "e3dc_multi_connect_unknown" if driver_variant == "e3dc_multi_connect" else "e3dc_native_fixed"
        status = {
            'car': 1,
            'amp': 0,
            'pha': 56,
            'charging': False,
            'alg_seen': False,
            'alg_flags': 0,
            'alg_charging': False,
            'alg_connected': False,
            'device_working': False,
            'real_power_w': 0.0,
            'car_connected_rscp': False,
            'driver_variant': driver_variant,
            'rscp_wb_index': self.wb_index,
            'phase_power_l1_w': 0.0,
            'phase_power_l2_w': 0.0,
            'phase_power_l3_w': 0.0,
            'phase_power_sum_w': 0.0,
            'phase_power_verified': False,
            'phases_in_use': 0,
            'phases_actual': 0,
            'phases_target': 0,
            'number_phases': 0,
            'connected_phases': 0,
            'can_switch_phases': False,
            'phase_switch_capability': phase_capability,
            'phase_switch_source': 'rscp_status',
            'api_surface': '',
            'charge_contract': {},
            'charge_truth': 'unknown',
            'charge_source': 'rscp_status_unavailable',
            'driver_status_valid': False,
            'driver_status_stale': True,
            'driver_status_degraded': True,
            'driver_status_reason': 'rscp_status_unavailable',
        }
        status.update(self._rscp_diag_status())
        return status

    def _heartbeat_loop(self):
        # E3DC requires a continuous heartbeat every <3 seconds to stay in external control mode
        while True:
            time.sleep(2.0)
            if self.external_suspended:
                continue
            if hasattr(self, 'last_amp') and hasattr(self, 'last_force_state'):
                if self.last_amp is not None:
                    # Heartbeat MUSS force_state=None senden. 
                    # sendet man dauerhaft last_force_state=1, feuert man im 2s Takt Toggle=1 ab,
                    # wodurch die Wallbox im Ping-Pong an und aus schaltet (Tick-Tack Bug)!
                    self._send_command_internal(self.last_amp, None, is_heartbeat=True)

    def _ensure_connected(self):
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
        from rscp_client import RscpTag, RscpType

        # --- Anfrage 1: Leistung + Verbindungsstatus ---
        reqs = [
            {'tag': RscpTag.WB_INDEX,               'type': RscpType.UChar8, 'value': self.wb_index},
            {'tag': RscpTag.WB_REQ_PM_POWER_L1,     'type': RscpType.Nil,    'value': None},
            {'tag': RscpTag.WB_REQ_PM_POWER_L2,     'type': RscpType.Nil,    'value': None},
            {'tag': RscpTag.WB_REQ_PM_POWER_L3,     'type': RscpType.Nil,    'value': None},
            {'tag': RscpTag.WB_REQ_MAX_CHARGE_CURRENT, 'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_EXTERN_DATA_ALG, 'type': RscpType.Nil,    'value': None},
            {'tag': RscpTag.WB_REQ_NUMBER_PHASES,   'type': RscpType.Nil,    'value': None},
            # Steckerzustand direkt vom RSCP (nicht vom C++ Polling)
            {'tag': RscpTag.WB_REQ_DEVICE_CONNECTED,'type': RscpType.Nil,    'value': None},
            {'tag': RscpTag.WB_REQ_DEVICE_WORKING,  'type': RscpType.Nil,    'value': None},
        ]

        # --- Anfrage 2: Session-Energie direkt aus E3DC-Firmware ---
        session_reqs = [
            {'tag': RscpTag.WB_INDEX,        'type': RscpType.UChar8, 'value': self.wb_index},
            {'tag': RscpTag.WB_REQ_SESSION,  'type': RscpType.Nil,    'value': None},
        ]

        try:
            req_frame = [
                {'tag': RscpTag.WB_REQ_DATA, 'type': RscpType.Container, 'value': reqs},
                {'tag': RscpTag.WB_REQ_DATA, 'type': RscpType.Container, 'value': session_reqs},
            ]
            response = self.conn.request(req_frame)
            if not response:
                self._record_rscp_error("status", "empty response")
                return self._minimal_rscp_status()
            self._record_rscp_ok("status")

            status = {
                'car': 1, 'amp': 6, 'pha': 56, 'charging': False, 'real_power_w': 0,
                'car_connected_rscp': False,  # Direktes RSCP Signal - kein C++ Glitch-Risiko
                'alg_seen': False,
                'alg_flags': 0,
                'alg_charging': False,
                'alg_connected': False,
                'device_working': False,
                'extern_alg_hex': '',
                'session_kwh': None,
                'session_start_ts': None,
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

            for item in response:
                if item['tag'] == RscpTag.WB_DATA:
                    sub_list = item.get('value', [])
                    for sub in sub_list:
                        if sub['tag'] == RscpTag.WB_PM_POWER_L1 and sub.get('value') is not None:
                            p1 = float(sub['value'])
                        elif sub['tag'] == RscpTag.WB_PM_POWER_L2 and sub.get('value') is not None:
                            p2 = float(sub['value'])
                        elif sub['tag'] == RscpTag.WB_PM_POWER_L3 and sub.get('value') is not None:
                            p3 = float(sub['value'])
                        elif sub['tag'] == RscpTag.WB_MAX_CHARGE_CURRENT and sub.get('value') is not None:
                            try:
                                status['amp'] = int(float(sub['value']))
                            except (TypeError, ValueError):
                                pass
                        elif sub['tag'] == RscpTag.WB_NUMBER_PHASES and sub.get('value') is not None:
                            try:
                                number_phases = int(float(sub['value']))
                                if number_phases in (1, 2, 3):
                                    status['number_phases'] = number_phases
                                    status['connected_phases'] = number_phases
                                    status['phases_actual'] = number_phases
                                    status['pha'] = 56 if number_phases >= 3 else (24 if number_phases == 2 else 8)
                            except (TypeError, ValueError):
                                pass
                        elif sub['tag'] == RscpTag.WB_DEVICE_CONNECTED and sub.get('value') is not None:
                            status['car_connected_rscp'] = bool(sub['value'])
                            if status['car_connected_rscp']:
                                status['car'] = 2
                        elif sub['tag'] == RscpTag.WB_DEVICE_WORKING and sub.get('value') is not None:
                            status['device_working'] = bool(sub['value'])
                            status['charging'] = bool(sub['value'])
                        elif sub['tag'] == RscpTag.WB_EXTERN_DATA_ALG:
                            if isinstance(sub.get('value'), list):
                                for alg_sub in sub['value']:
                                    if alg_sub['tag'] == RscpTag.WB_EXTERN_DATA:
                                        b = alg_sub.get('value')
                                        if b and len(b) >= 3:
                                            cWBALG = b[2]
                                            status['alg_seen'] = True
                                            status['alg_flags'] = int(cWBALG)
                                            status['extern_alg_hex'] = b.hex()
                                            status['alg_charging'] = bool(cWBALG & 32)
                                            status['alg_connected'] = bool(cWBALG & 8)
                                            self.real_charging = bool(status['alg_charging'])
                                            status['charging'] = bool(status['alg_charging'] or status['device_working'])
                                            if bool(cWBALG & 8):
                                                status['car'] = 2
                                                status['car_connected_rscp'] = True

                    # Session-Daten aus dem zweiten WB_DATA Container lesen
                    for sub in sub_list:
                        if sub['tag'] == RscpTag.WB_SESSION:
                            session_container = sub.get('value', [])
                            if isinstance(session_container, list):
                                for s in session_container:
                                    # WB_SESSION_CHARGED_ENERGY = 0x0E74102A (Wh)
                                    if s.get('tag') == 0x0E74102A and s.get('value') is not None:
                                        try:
                                            status['session_kwh'] = round(float(s['value']) / 1000.0, 3)
                                        except (ValueError, TypeError):
                                            pass
                                    # WB_SESSION_START_TIME = 0x0E741026 (Unix Timestamp)
                                    elif s.get('tag') == 0x0E741026 and s.get('value') is not None:
                                        try:
                                            status['session_start_ts'] = int(s['value'])
                                        except (ValueError, TypeError):
                                            pass

            raw_power_w = p1 + p2 + p3
            status['real_power_w'] = raw_power_w
            status['phase_power_l1_w'] = round(p1, 1)
            status['phase_power_l2_w'] = round(p2, 1)
            status['phase_power_l3_w'] = round(p3, 1)
            status['phase_power_sum_w'] = round(raw_power_w, 1)
            # E3DC/Multi Connect can report small residual or stale PM values
            # after an abort. Treat only meaningful load as charging; the
            # authoritative state still comes from WB_EXTERN_DATA_ALG.
            if raw_power_w > 500:
                status['charging'] = True
            if not status['charging']:
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
            status.update(self._rscp_diag_status())
            return self._sanitize_measurement_status(status)
        except Exception as e:
            self._record_rscp_error("status", e)
            logger.error(f"[WB{self.wb_id}] getData Fehler: {e}")
            if self.conn:
                self.conn.disconnect()
            return self._minimal_rscp_status()

    def _send_command_internal(self, target_amp, force_state, is_heartbeat=False):
        if not command_gate.allow_command(
            self,
            action="e3dc_set_extern",
            payload={"target_amp": target_amp, "force_state": force_state, "heartbeat": bool(is_heartbeat)},
            audit_allowed=not bool(is_heartbeat),
        ):
            return True
        with self.lock:
            if not self._ensure_connected():
                return False
            from rscp_client import RscpTag, RscpType
            target_amp = int(max(0, min(target_amp, 32)))
            
            # E3DC WBchar6 Mode-Semantik (empirisch durch Eba-Algorithmus verifiziert):
            # - Mode=1 (Sonnenmodus): WBchar6[1] = echter Sollstrom, aber NUR aus PV.
            #   E3DC laedt exakt WBchar6[1] Ampere, aber begrenzt auf verfuegbare PV.
            #   Kein Netzbezug moeglich in Mode=1. E3DC haelt Netz-Schutz intern!
            # - Mode=2 (Netzmodus): WBchar6[1] = exakter Sollstrom aus PV+Netz+Batterie.
            #   Python muss selber sicherstellen, dass kein unerwuenschter Netzbezug entsteht.
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
                now = time.time()
                if self.real_charging:
                    abort_flag = 1
                    self.last_stop_toggle_ts = now
                elif force_state == 1 and not is_heartbeat:
                    logger.debug(f"[WB{self.wb_id}] Stop-Toggle unterdrueckt: Wallbox meldet keine aktive Ladung.")
            elif target_amp > 0:
                # Wir wollen STARTEN/LADEN. Toggle (1) nur bei explizitem
                # Startimpuls senden. force_state=None ist ein reiner
                # Stromdeckel/Keepalive und darf nach einem weichen Stop
                # keine schlafende E3DC-Wallbox wieder anstossen.
                if force_state == 2 and not self.real_charging and not is_heartbeat:
                    abort_flag = 1

            # Heartbeat-Calls haben force_state=None und duerfen nie toggeln.

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
                self.conn.request(req_frame)
                self._record_rscp_ok("set_extern")
                return True
            except Exception as e:
                self._record_rscp_error("set_extern", e)
                logger.error(f"[WB{self.wb_id}] setAmp Fehler: {e}")
                if self.conn:
                    self.conn.disconnect()
                return False

    def release_to_e3dc(self, max_amp=32):
        """
        Echte Freigabe fuer wb_mode=0: keine SET_EXTERN-Kommandos mehr senden.
        Der bisherige "Sonnenmodus-Heartbeat" hielt die E3DC-Wallbox weiter in
        externer RSCP-Kontrolle. Einige Anlagen laden dann erst wieder, wenn der
        Pi offline ist. Mode 0 bedeutet deshalb ab jetzt wirklich Python stumm.
        """
        self.suspend_external_control("Mode 0 / E3DC autonom")
        ok_default = True
        try:
            ok_default = bool(self._set_e3dc_max_charge_current(max_amp))
        except Exception as e:
            ok_default = False
            logger.debug(f"[WB{self.wb_id}] E3DC Max-Strom Default-Freigabe fehlgeschlagen: {e}")
        self.suspend_external_control("Mode 0 / E3DC autonom")
        return ok_default

    def release_to_default(self, max_amp=32):
        return self.release_to_e3dc(max_amp=max_amp)

    def _set_e3dc_max_charge_current(self, max_amp=32):
        """Setzt den E3DC-Hardwaredeckel ohne SET_EXTERN-Heartbeat."""
        if not command_gate.allow_command(
            self,
            action="e3dc_set_max_charge_current",
            payload={"max_amp": max_amp},
        ):
            return True
        amp = int(max(6, min(32, max_amp or 32)))
        if not self._ensure_connected():
            return False
        from rscp_client import RscpTag, RscpType
        frame = [{'tag': RscpTag.WB_REQ_DATA, 'type': RscpType.Container, 'value': [
            {'tag': RscpTag.WB_INDEX, 'type': RscpType.UChar8, 'value': self.wb_index},
            {'tag': RscpTag.WB_REQ_SET_MAX_CHARGE_CURRENT, 'type': RscpType.UChar8, 'value': amp},
        ]}]
        try:
            self.conn.request(frame)
            self._record_rscp_ok("set_max_charge_current")
        except Exception as e:
            self._record_rscp_error("set_max_charge_current", e)
            logger.error(f"[WB{self.wb_id}] E3DC Max-Ladestrom Fehler: {e}")
            if self.conn:
                self.conn.disconnect()
            return False
        logger.info(f"[WB{self.wb_id}] E3DC Max-Ladestrom fuer Autonom-Modus auf {amp}A gesetzt.")
        return True

    def suspend_external_control(self, reason="Python stumm"):
        """Stoppt den E3DC-SET_EXTERN-Heartbeat und schliesst die RSCP-Verbindung."""
        with self.lock:
            if not self.external_suspended:
                logger.info(f"[WB{self.wb_id}] SET_EXTERN-Heartbeat gestoppt ({reason}).")
            self.external_suspended = True
            self.sonnenmodus = False
            self.last_amp = None
            self.last_force_state = None
            try:
                if self.conn:
                    self.conn.close()
            except Exception:
                pass
            self.conn = None

    def set_amp_sonnenmodus(self, target_amp, force_state=None):
        """
        Setzt Sollstrom in Mode=1 (Sonnenmodus / PV-Only).
        Verwendet fuer Python-gesteuerte PV-Ladung (wb_mode 1-10 mit Budget-Signal).

        Unterschied zu release_to_e3dc():
        - Python bleibt aktiv und setzt den Ceiling-Amp regelmaessig (alle 2-4s)
        - E3DC laedt mit target_amp, aber nur aus PV (kein Netzbezug)
        - Start/Stop via force_state=2 (Start-Toggle) / force_state=1 (Stop-Toggle)
        - force_state=None setzt nur den Stromdeckel und sendet keinen Toggle

        Unterschied zu set_amp_and_state() (Mode=2):
        - Mode=1 schuetzt gegen Netzbezug ohne Fast-Grid-Correction
        """
        if not command_gate.allow_command(
            self,
            action="e3dc_set_amp_sonnenmodus",
            payload={"target_amp": target_amp, "force_state": force_state},
            audit_allowed=False,
        ):
            return True
        self.external_suspended = False
        self.sonnenmodus = True
        self.last_amp = target_amp
        self.last_force_state = force_state
        return self._send_command_internal(target_amp, force_state, is_heartbeat=False)

    def take_control(self):
        """Python uebernimmt aktive Steuerung (Netzmodus Mode=2)."""
        self.external_suspended = False
        if self.sonnenmodus:
            logger.info(f"[WB{self.wb_id}] Python uebernimmt Steuerung (Netzmodus Mode=2)")
            self.sonnenmodus = False

    def emergency_stop(self):
        """
        Notabschaltung: Python uebernimmt sofort die Kontrolle (Mode=2)
        und stoppt die WB hart. Heartbeat haelt den Stopp-Zustand.
        Unterschied zu set_amp_and_state(0, 1):
        - Bricht auch den Sonnenmodus (Mode=1) auf
        - Setzt last_force_state=1 damit Heartbeat nicht wieder startet
        """
        if not command_gate.allow_command(
            self,
            action="e3dc_emergency_stop",
            payload={"target_amp": 0, "force_state": 1},
        ):
            return True
        logger.warning(f"[WB{self.wb_id}] emergency_stop(): Unterbreche Sonnenmodus, erzwinge Mode=2 STOP.")
        self.external_suspended = False
        self.sonnenmodus = False       # Sonnenmodus beenden - Python uebernimmt
        self.real_charging = True      # Erzwinge Toggle auf Stop (auch wenn State unbekannt)
        self.last_amp = 6              # Minimaler Amp fuer Heartbeat
        self.last_force_state = 1      # Heartbeat haelt STOP dauerhaft!
        self._send_command_internal(0, 1, is_heartbeat=False)  # Sofortiger Stop-Toggle an E3DC

    def set_amp_and_state(self, target_amp, force_state=None):
        if not command_gate.allow_command(
            self,
            action="e3dc_set_amp_and_state",
            payload={"target_amp": target_amp, "force_state": force_state},
            audit_allowed=False,
        ):
            return True
        self.external_suspended = False
        self.last_amp = target_amp
        self.last_force_state = force_state # Heartbeat soll den aktuellen Status uebernehmen (None fuer PV, 2 fuer Zwang)
        return self._send_command_internal(target_amp, force_state, is_heartbeat=False)


# ===========================================================================
# E3DCMultiConnectCharger (Multi Connect I/II per direkten RSCP-Tags)
# ===========================================================================
class E3DCMultiConnectCharger(E3DCCharger):
    """E3DC Multi Connect per direkten RSCP-Wallbox-Tags.

    Die Multi Connect arbeitet stabiler ueber die neueren direkten Tags:
    Sonnenmodus aus, Auto-Phasenwechsel aus, Stromlimit setzen und per
    Abort-Flag freigeben/sperren. SET_EXTERN bleibt nur als historische
    Diagnose-/Fallback-Referenz im Code, wird aber nicht fuer normale
    Regelung genutzt.
    """

    def __init__(self, ip, wb_id, config):
        super().__init__(ip, wb_id, config)
        self.driver_variant = "e3dc_multi_connect"
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

    def _wb_request(self, reqs, wb_index=None):
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
                response = self.conn.request(frame)
                self._record_rscp_ok("wb_request")
                return response
            except Exception as e:
                self._record_rscp_error("wb_request", e)
                logger.error(f"[WB{self.wb_id}] Multi Connect RSCP Fehler: {e}")
                if self.conn:
                    self.conn.disconnect()
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
        from rscp_client import RscpTag, RscpType
        response = self._wb_request([
            {'tag': RscpTag.WB_REQ_DEVICE_NAME,              'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_FIRMWARE_VERSION,         'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_PARAM_1,                  'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_PARAM_2,                  'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_EXTERN_DATA_ALG,          'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_NUMBER_PHASES,            'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_MAX_CHARGE_CURRENT,       'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_SUN_MODE_ACTIVE,          'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_AUTO_PHASE_SWITCH_ENABLED,'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_WALLBOX_TYPE,             'type': RscpType.Nil, 'value': None},
        ], wb_index=wb_index)
        info = {}
        for sub in self._iter_wb_items(response):
            tag = sub.get('tag')
            val = sub.get('value')
            if tag == RscpTag.WB_DEVICE_NAME:
                info['device_name'] = str(val or '')
            elif tag == RscpTag.WB_FIRMWARE_VERSION:
                info['firmware_version'] = str(val or '')
            elif tag == RscpTag.WB_NUMBER_PHASES:
                info['number_phases'] = int(val or 0)
            elif tag == RscpTag.WB_MAX_CHARGE_CURRENT:
                info['max_charge_current'] = int(val or 0)
            elif tag == RscpTag.WB_SUN_MODE_ACTIVE:
                info['sun_mode_active'] = bool(val)
            elif tag == RscpTag.WB_AUTO_PHASE_SWITCH_ENABLED:
                info['auto_phase_switch_enabled'] = bool(val)
            elif tag == RscpTag.WB_WALLBOX_TYPE:
                info['wallbox_type'] = int(val or 0)
            elif tag == RscpTag.WB_RSP_PARAM_1:
                param = self._extract_extern_bytes(sub, RscpTag.WB_RSP_PARAM_1)
                if param is not None:
                    info['param1'] = param
                    if len(param) >= 3:
                        info['param_current'] = int(param[2])
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
        return info

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
        if info.get('max_charge_current', 0) > 0:
            score += 30
        if info.get('param_current', 0) > 0:
            score += 20
        if info.get('number_phases') in (1, 3):
            score += 15
        if info.get('device_name'):
            score += 5
        return score

    @staticmethod
    def _direct_info_supported(info):
        if not isinstance(info, dict):
            return False
        name = (info.get('device_name') or '').lower()
        alg = info.get('extern_alg')
        return bool(
            info.get('wallbox_type') == 6
            or (alg and len(alg) >= 3)
            and (
                'multi' in name
                or info.get('wallbox_type') == 6
                or info.get('number_phases') in (1, 3)
                or info.get('max_charge_current', 0) > 0
            )
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
        """Read-only Probe fuer Auto-Erkennung."""
        if not self._direct_checked:
            self._direct_checked = True
            self._resolve_wb_index(force=True)
        return self._direct_supported

    def _set_direct(self, tag, value, value_type):
        if not command_gate.allow_command(
            self,
            action="e3dc_multi_set_direct",
            payload={"tag": str(tag), "value": value},
            audit_allowed=False,
        ):
            return True
        from rscp_client import RscpTag
        req = {'tag': tag, 'type': value_type, 'value': value}
        response = self._wb_request([req])
        return response is not None

    def _send_wallbox_data_index(self, request_tag, data_index, value, length=6):
        """pye3dc-kompatibler Low-Level-Setter fuer WB_EXTERN_DATA.

        Multi Connect verarbeitet Max-Strom stabil ueber SET_PARAM_1 Byte 2.
        SET_EXTERN bleibt fuer Modus (Byte 0) und Start/Stop-Toggle (Byte 4).
        """
        from rscp_client import RscpTag, RscpType
        if data_index < 0 or data_index >= length:
            return False
        data = bytearray(length)
        data[data_index] = int(max(0, min(255, value)))
        reqs = [
            {'tag': request_tag, 'type': RscpType.Container, 'value': [
                {'tag': RscpTag.WB_EXTERN_DATA_LEN, 'type': RscpType.UChar8, 'value': length},
                {'tag': RscpTag.WB_EXTERN_DATA, 'type': RscpType.ByteArray, 'value': bytes(data)},
            ]},
        ]
        return self._wb_request(reqs) is not None

    def _set_extern_mode(self, mode, amp=None, force=False):
        mode = 1 if int(mode) == 1 else 2
        amp = int(max(6, min(32, amp or self.last_amp or 6)))
        now = time.time()
        if (
            not force
            and self._last_extern_mode == mode
            and self._last_extern_amp == amp
            and now - self._last_extern_mode_ts < self._keepalive_interval_s
        ):
            return True
        ok = self._set_extern_cxx(mode, amp, toggle=False)
        if ok:
            self._last_extern_mode = mode
            self._last_extern_amp = amp
            self._last_extern_mode_ts = now
        return ok

    def _set_sun_mode(self, active, amp=None, force=False):
        from rscp_client import RscpTag, RscpType
        active = bool(active)
        now = time.time()
        if (
            not force
            and self._last_sun_mode is active
            and now - self._last_sun_mode_ts < self._keepalive_interval_s
        ):
            return True
        ok = self._set_direct(RscpTag.WB_REQ_SET_SUN_MODE_ACTIVE, active, RscpType.Bool)
        if ok:
            self.sonnenmodus = active
            self._last_sun_mode = active
            self._last_sun_mode_ts = now
        return ok

    def _set_auto_phase_switch(self, active, force=False):
        from rscp_client import RscpTag, RscpType
        active = bool(active)
        now = time.time()
        if (
            not force
            and self._last_auto_phase is active
            and now - self._last_auto_phase_ts < self._keepalive_interval_s
        ):
            return True
        ok = self._set_direct(RscpTag.WB_REQ_SET_AUTO_PHASE_SWITCH_ENABLED, active, RscpType.Bool)
        if ok:
            self._last_auto_phase = active
            self._last_auto_phase_ts = now
        return ok

    def _ensure_control_defaults(self, force=False):
        # evcc-Referenz fuer Multi Connect: externe Regelung arbeitet nur
        # sauber, wenn Sonnenmodus und automatische Phasenumschaltung aus sind.
        ok = self._set_sun_mode(False, force=force)
        ok = self._set_auto_phase_switch(False, force=force) and ok
        return ok

    def _set_abort(self, abort, force=False):
        from rscp_client import RscpTag, RscpType
        abort = bool(abort)
        now = time.time()
        if (
            not force
            and self._last_abort is abort
            and now - self._last_abort_ts < self._keepalive_interval_s
        ):
            return True
        ok = self._set_direct(RscpTag.WB_REQ_SET_ABORT_CHARGING, abort, RscpType.Bool)
        if ok:
            self._last_abort = abort
            self._last_abort_ts = now
        return ok

    def _set_max_current(self, amp, force=False):
        from rscp_client import RscpTag, RscpType
        amp = int(max(6, min(32, amp)))
        now = time.time()
        if (
            not force
            and self._last_param_current == amp
            and now - self._last_param_current_ts < self._keepalive_interval_s
        ):
            return True
        ok = self._set_direct(RscpTag.WB_REQ_SET_MAX_CHARGE_CURRENT, amp, RscpType.UChar8)
        if ok:
            self._last_param_current = amp
            self._last_param_current_ts = now
        return ok

    def _clear_phantom_state(self):
        """Multi Connect beruhigen, ohne Start/Stop-Toggle.

        Nach einem abgewiesenen Start kann WB_EXTERN_DATA_ALG z.B. 0x88
        melden: Fahrzeug verbunden + Sonnenmodus, aber keine echte Ladung.
        Ein weiterer Toggle wuerde diesen Schattenzustand wieder anfachen.
        0x48 ist dagegen laut Multi-Connect-Diagnose ein normaler Zustand:
        verbunden, aber laedt nicht. Den lassen wir unangetastet.
        """
        from rscp_client import RscpTag, RscpType
        flags = int(self._last_alg_flags or 0)
        if not (flags & 0x80):
            return True
        ok = True
        ok = self._set_direct(RscpTag.WB_REQ_SET_SUN_MODE_ACTIVE, False, RscpType.Bool) and ok
        self.sonnenmodus = False
        self._last_extern_mode = None
        self._last_extern_amp = None
        return ok

    def _send_charge_toggle(self):
        mode = 1 if self.sonnenmodus else 2
        amp = int(max(6, min(32, self.last_amp or self._last_param_current or 6)))
        logger.debug(f"[WB{self.wb_id}] Multi Connect SET_EXTERN Toggle=1")
        return self._set_extern_cxx(mode, amp, toggle=True)

    def _set_extern_cxx(self, mode, amp, toggle=False):
        """C++-konforme Multi-Connect-Freigabe ueber WBchar6/SET_EXTERN.

        Die direkten Multi-Tags liefern saubere Statuswerte und setzen den
        Stromdeckel, starten die Multi Connect auf einem Referenzsystem aber nicht
        zuverlaessig. Das alte C++-Programm nutzt fuer Start/Stop WBchar6:
        Byte 0 = Modus (1 Sonnenmodus, 2 Misch/Netz), Byte 1 = Ampere,
        Byte 4 = Toggle. Der Toggle darf nur als Impuls gesendet werden.
        """
        if not command_gate.allow_command(
            self,
            action="e3dc_multi_set_extern_cxx",
            payload={"mode": mode, "amp": amp, "toggle": bool(toggle)},
            audit_allowed=False,
        ):
            return True
        from rscp_client import RscpTag, RscpType
        mode = 1 if int(mode) == 1 else 2
        amp = int(max(6, min(32, amp or 6)))
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
        return self._wb_request(reqs) is not None

    def _needs_start_toggle(self, force_state=None):
        flags = int(self._last_alg_flags or 0)
        if force_state != 2:
            return False
        # C++/Eba sendet den Start-Toggle nur, wenn die Wallbox den
        # Stopp-Zustand (0x40) meldet. In einem Referenzfall zeigte sich: ein Toggle aus
        # dem reinen "angesteckt"-Zustand (0x08) schiebt die Multi Connect
        # erst in STOP und erzeugt PM-Phantomleistung.
        want_toggle = bool(flags & 0x40) and not bool(flags & (0x10 | 0x20))
        if not want_toggle:
            return False
        now = time.time()
        if now - self._last_start_toggle_ts < 8:
            return False
        self._last_start_toggle_ts = now
        return True

    def _needs_stop_toggle(self):
        flags = int(self._last_alg_flags or 0)
        return self.real_charging or bool(flags & (0x10 | 0x20))

    def _send_command_internal(self, target_amp, force_state=None, is_heartbeat=False):
        """evcc-nahe Multi-Connect-Steuerung ohne SET_EXTERN-Toggle."""
        if not command_gate.allow_command(
            self,
            action="e3dc_multi_send_command",
            payload={"target_amp": target_amp, "force_state": force_state, "heartbeat": bool(is_heartbeat)},
            audit_allowed=False,
        ):
            return True
        if self.external_suspended:
            return True
        amp = int(max(0, min(target_amp or 0, 32)))
        if amp <= 0 or force_state == 1:
            self.last_amp = 0
            self.last_force_state = 1
            ok = self._ensure_control_defaults(force=not is_heartbeat)
            ok = self._set_max_current(6, force=not is_heartbeat) and ok
            ok = self._set_abort(True, force=not is_heartbeat) and ok
            return ok
        ok = self._ensure_control_defaults(force=not is_heartbeat)
        ok = self._set_max_current(amp, force=not is_heartbeat) and ok
        ok = self._set_abort(False, force=not is_heartbeat) and ok
        if ok and not is_heartbeat and self._needs_start_toggle(force_state):
            # Die direkten Tags setzen den Deckel und loesen Abort. Einige
            # Multi-Connect-Anlagen bleiben danach im echten Stoppzustand
            # (ALG Bit 6). Dann braucht es genau einen alten C++-Startimpuls.
            # Kein Toggle aus dem reinen "verbunden"-Zustand, sonst entstehen
            # Phantom-Starts ohne echte Energie.
            ok = self._set_extern_cxx(2, amp, toggle=True) and ok
            self._last_extern_mode = None
            self._last_extern_amp = None
            logger.info(f"[WB{self.wb_id}] Multi Connect Startimpuls: Stopp-Flag geloest ({amp}A)")
        self.last_amp = amp
        self.last_force_state = force_state
        return ok

    def set_phases(self, phases):
        if not command_gate.allow_command(
            self,
            action="e3dc_multi_set_phases",
            payload={"phases": phases},
        ):
            return True
        from rscp_client import RscpTag, RscpType
        phases = 1 if int(phases) == 1 else 3
        return self._set_direct(RscpTag.WB_REQ_SET_NUMBER_PHASES, phases, RscpType.UChar8)

    def get_status(self):
        from rscp_client import RscpTag, RscpType
        self._resolve_wb_index()
        response = self._wb_request([
            {'tag': RscpTag.WB_REQ_PM_POWER_L1,       'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_PM_POWER_L2,       'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_PM_POWER_L3,       'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_PARAM_1,           'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_PARAM_2,           'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_EXTERN_DATA_ALG,   'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_DEVICE_CONNECTED,  'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_DEVICE_WORKING,    'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_DEVICE_NAME,       'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_FIRMWARE_VERSION,  'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_NUMBER_PHASES,     'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_AUTO_PHASE_SWITCH_ENABLED,'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_MAX_CHARGE_CURRENT,'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_ABORT_CHARGING,    'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_WALLBOX_TYPE,      'type': RscpType.Nil, 'value': None},
            {'tag': RscpTag.WB_REQ_SESSION,           'type': RscpType.Nil, 'value': None},
        ])
        if not response:
            self._record_rscp_error("status", "empty response")
            return self._minimal_rscp_status()

        status = {
            'car': 1, 'amp': 6, 'pha': 56, 'charging': False, 'real_power_w': 0.0,
            'car_connected_rscp': False,
            'alg_seen': False,
            'alg_flags': 0,
            'alg_charging': False,
            'alg_connected': False,
            'device_working': False,
            'driver_variant': self.driver_variant,
            'rscp_wb_index': self.wb_index,
            'device_name': self.device_name,
            'firmware_version': self.firmware_version,
            'enabled': None,
            'extern_alg_hex': '',
            'param1_hex': '',
            'param2_hex': '',
            'param_current': None,
            'wallbox_type': None,
            'session_kwh': None,
            'session_start_ts': None,
            'phase_power_l1_w': 0.0,
            'phase_power_l2_w': 0.0,
            'phase_power_l3_w': 0.0,
            'phase_power_sum_w': 0.0,
            'phase_power_verified': False,
            'phases_in_use': 0,
            'phases_actual': 0,
            'phases_target': 0,
            'number_phases': 0,
            'connected_phases': 0,
            'auto_phase_switch_enabled': None,
            'can_switch_phases': False,
            'phase_switch_capability': 'e3dc_multi_connect_unknown',
            'phase_switch_source': 'rscp_status',
            'api_surface': '',
        }
        status.update(self._rscp_diag_status())
        p1 = p2 = p3 = 0.0
        alg = None
        param_current = None

        for sub in self._iter_wb_items(response):
            tag = sub.get('tag')
            val = sub.get('value')
            if tag == RscpTag.WB_PM_POWER_L1 and val is not None:
                p1 = float(val)
            elif tag == RscpTag.WB_PM_POWER_L2 and val is not None:
                p2 = float(val)
            elif tag == RscpTag.WB_PM_POWER_L3 and val is not None:
                p3 = float(val)
            elif tag == RscpTag.WB_DEVICE_CONNECTED and val is not None:
                status['car_connected_rscp'] = bool(val)
            elif tag == RscpTag.WB_DEVICE_WORKING and val is not None:
                status['device_working'] = bool(val)
                status['charging'] = bool(val)
            elif tag == RscpTag.WB_DEVICE_NAME:
                self.device_name = str(val or '')
                status['device_name'] = self.device_name
            elif tag == RscpTag.WB_FIRMWARE_VERSION:
                self.firmware_version = str(val or '')
                status['firmware_version'] = self.firmware_version
            elif tag == RscpTag.WB_NUMBER_PHASES and val is not None:
                try:
                    number_phases = int(val or 0)
                except (TypeError, ValueError):
                    number_phases = 0
                if number_phases in (1, 2, 3):
                    status['number_phases'] = number_phases
                    status['connected_phases'] = number_phases
                    status['phases_actual'] = number_phases
                    status['phases_target'] = number_phases
                    status['pha'] = 56 if number_phases >= 3 else (24 if number_phases == 2 else 8)
            elif tag == RscpTag.WB_AUTO_PHASE_SWITCH_ENABLED and val is not None:
                status['auto_phase_switch_enabled'] = bool(val)
            elif tag == RscpTag.WB_MAX_CHARGE_CURRENT and val is not None:
                status['amp'] = int(val)
            elif tag == RscpTag.WB_ABORT_CHARGING and val is not None:
                status['enabled'] = not bool(val)
            elif tag == RscpTag.WB_WALLBOX_TYPE and val is not None:
                status['wallbox_type'] = int(val or 0)
            elif tag == RscpTag.WB_RSP_PARAM_1:
                param = self._extract_extern_bytes(sub, RscpTag.WB_RSP_PARAM_1)
                if param is not None:
                    status['param1_hex'] = param.hex()
                    # C++ Referenz: iWBIst = WBchar[2] aus TAG_WB_RSP_PARAM_1.
                    if len(param) >= 3:
                        param_current = int(param[2])
                        status['param_current'] = param_current
            elif tag == RscpTag.WB_RSP_PARAM_2:
                param = self._extract_extern_bytes(sub, RscpTag.WB_RSP_PARAM_2)
                if param is not None:
                    status['param2_hex'] = param.hex()
            elif tag == RscpTag.WB_SESSION:
                for s in (val or []):
                    if s.get('tag') == RscpTag.WB_SESSION_CHARGED_ENERGY and s.get('value') is not None:
                        status['session_kwh'] = round(float(s['value']) / 1000.0, 3)
                    elif s.get('tag') == RscpTag.WB_SESSION_START_TIME and s.get('value') is not None:
                        status['session_start_ts'] = int(s['value'])
                    elif s.get('tag') == RscpTag.WB_SESSION_AUTH_DATA and s.get('value') is not None:
                        status['rfid_tag'] = str(s['value'])
            else:
                alg_candidate = self._extract_alg_bytes(sub)
                if alg_candidate is not None:
                    alg = alg_candidate

        if alg and len(alg) >= 3:
            flags = alg[2]
            self._last_alg_flags = int(flags)
            status['alg_seen'] = True
            status['alg_flags'] = int(flags)
            status['extern_alg_hex'] = alg.hex()
            status['alg_charging'] = bool(flags & 0b00100000)
            status['alg_connected'] = bool(flags & 0b00001000)
            status['charging'] = bool(status['alg_charging'] or status['device_working'])
            status['car_connected_rscp'] = bool(status['alg_connected'])
            status['enabled'] = (flags & 0b01000000) == 0
            if len(alg) >= 2:
                status['pha'] = 8 if int(alg[1]) == 1 else 56

        if param_current is not None:
            status['amp'] = param_current

        if status.get('enabled') is False:
            status['amp'] = 0
            # 0x68 kann kurz nach einem Stop auftreten: Fahrzeug/PM meldet
            # noch Leistung, aber Abort ist bereits aktiv. Fuer die Regelung
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
        alg_seen = bool(alg and len(alg) >= 3)
        if not alg_seen:
            # Multi Connect kann beim Startversuch PM-Leistung melden, ohne
            # dass der ALG-Status echte Ladung bestaetigt. In diesem Fall
            # ist der Messwert fuer UI und Regelung ein Glitch.
            status['charging'] = False
        # Multi Connect kann kurz Phantomwerte aus dem internen PM liefern,
        # ohne dass WB_EXTERN_DATA_ALG/DEVICE_WORKING echte Ladung melden.
        # Fuer die Regelung bleibt deshalb der E3DC-Status fuehrend.
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
        can_switch_phases = bool(
            status.get('wallbox_type') == 6
            or int(status.get('number_phases', 0) or 0) in (1, 3)
        )
        status['can_switch_phases'] = can_switch_phases
        status['phase_switch_capability'] = (
            'e3dc_multi_connect_direct'
            if can_switch_phases
            else 'e3dc_multi_connect_unknown'
        )
        status['phase_switch_source'] = (
            'rscp_wb_set_number_phases'
            if can_switch_phases
            else 'rscp_status'
        )
        status['api_surface'] = (
            'rscp_wb_req_set_number_phases'
            if can_switch_phases
            else ''
        )
        self.real_charging = bool(status['charging'])
        return self._sanitize_measurement_status(status)

    def release_to_e3dc(self, max_amp=32):
        # Freigabe fuer E3DC/autonome Bedienung: sichere Multi-Grundeinstellung,
        # dann Python stumm.
        self.suspend_external_control("Mode 0 / E3DC autonom")
        ok_default = True
        try:
            ok_default = self._ensure_control_defaults(force=True)
            ok_default = self._set_max_current(max_amp, force=True) and ok_default
            ok_default = self._set_abort(False, force=True) and ok_default
        except Exception as e:
            ok_default = False
            logger.debug(f"[WB{self.wb_id}] Multi Connect Freigabe fehlgeschlagen: {e}")
        self.suspend_external_control("Mode 0 / E3DC autonom")
        return ok_default

    def release_to_default(self, max_amp=32):
        return self.release_to_e3dc(max_amp=max_amp)

    def suspend_external_control(self, reason="Python stumm"):
        if not self.external_suspended:
            logger.info(f"[WB{self.wb_id}] Multi Connect Direktsteuerung pausiert ({reason}).")
        self.external_suspended = True
        self.last_amp = None
        self.last_force_state = None
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass
        self.conn = None

    def set_amp_sonnenmodus(self, target_amp, force_state=None):
        """PV-only/Fuzzy: Manager gibt Budget vor; Multi bleibt im externen Modus."""
        if not command_gate.allow_command(
            self,
            action="e3dc_multi_set_amp_sonnenmodus",
            payload={"target_amp": target_amp, "force_state": force_state},
        ):
            return True
        self.external_suspended = False
        self.sonnenmodus = False
        return self._send_command_internal(target_amp, force_state, is_heartbeat=False)

    def take_control(self):
        self.external_suspended = False
        if self.sonnenmodus:
            logger.info(f"[WB{self.wb_id}] Multi Connect: Mischbetrieb/Python-Kontrolle")
        self._ensure_control_defaults(force=True)
        self.sonnenmodus = False

    def emergency_stop(self):
        if not command_gate.allow_command(
            self,
            action="e3dc_multi_emergency_stop",
            payload={"target_amp": 0, "force_state": 1},
        ):
            return True
        logger.warning(f"[WB{self.wb_id}] Multi Connect emergency_stop(): Abort aktiv")
        self.external_suspended = False
        self.last_amp = 0
        self.last_force_state = 1
        self._send_command_internal(0, 1, is_heartbeat=False)

    def set_amp_and_state(self, target_amp, force_state=None):
        """Mischbetrieb: PV+Speicher/Netz je nach Manager-Modus."""
        if not command_gate.allow_command(
            self,
            action="e3dc_multi_set_amp_and_state",
            payload={"target_amp": target_amp, "force_state": force_state},
        ):
            return True
        self.external_suspended = False
        self.sonnenmodus = False
        return self._send_command_internal(target_amp, force_state, is_heartbeat=False)


# ===========================================================================
# OpenWBProCharger (openWB Pro: connect.php Status + Steuerung)
# ===========================================================================
class OpenWBProCharger(WallboxDriver):
    """Treiber fuer openWB Pro.

    Die openWB Pro ist kein openWB-Controller und stellt die Controller-
    simpleAPI unter /openWB/simpleAPI/simpleapi.php nicht bereit. Status und
    Steuerung laufen im Standalone-Betrieb ueber die dokumentierte
    /connect.php API.
    """

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
        self._last_heartbeat_enable_ts = 0.0
        self.state = {
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
            "can_switch_phases": True,
            "phase_switch_capability": "official_connect_php",
            "phase_switch_source": "openwb_pro_connect_php",
            "api_surface": "openwb_pro_connect_php",
            "daily_imported_wh": 0.0,
            "imported_total_wh": 0.0,
            "chargemode_str": "stop",
            "session_kwh": 0.0,
            "_session_start_wh": None,
            "car_soc": 0.0,
            "car_soc_source": "",
            "car_soc_raw_ts": None,
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
            "rfid_timestamp": None,
            "_soc_anchor_soc": None,
            "_soc_anchor_imported_wh": None,
            "_soc_anchor_vehicle_id": None,
            "_soc_raw_timestamp": None,
            "_soc_raw_value": None,
            "serial": None,
            "version": None,
            "v2g_ready": 0,
            "evse_signaling": "",
            "offered_current_raw": 0.0,
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
        soc = self._float(self.state.get("car_soc"), -1.0)
        if soc <= 0:
            return
        soc_ts = int(time.time())
        soc_file = os.path.join(RAMDISK_DIR, f"manual_soc_wb{self.wb_id}.json")
        payload = {
            "soc": round(soc, 1),
            "ts": soc_ts,
            "source": source,
            "plugged": bool(self.state.get("plug_state", False)),
            "age_h": 0.0,
            "wb": self.wb_id,
            "name": self.state.get("car_name"),
            "car_id": self.state.get("car_id"),
            "vehicle_id": self.state.get("vehicle_id") or self.state.get("rfid_tag"),
            "capacity": self.state.get("car_capacity_kwh", 0.0),
            "session_kwh": round(self.state.get("session_kwh", 0.0), 3),
            "raw_soc_ts": self.state.get("car_soc_raw_ts"),
        }
        try:
            os.makedirs(os.path.dirname(soc_file), exist_ok=True)
            tmp = soc_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, soc_file)
        except Exception as e:
            logger.debug(f"[WB{self.wb_id}] openWB Pro manual_soc schreiben fehlgeschlagen: {e}")

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
        if data.get("plugged") is False:
            return None
        ts = int(self._float(data.get("ts"), 0.0))
        if ts > 0 and time.time() - ts > 12 * 3600:
            return None
        soc = self._float(data.get("soc"), -1.0)
        if soc <= 0:
            return None
        manual_vehicle_id = str(data.get("vehicle_id") or "").strip()
        manual_car_id = str(data.get("car_id") or "").strip()
        active_compact = self._compact_id(active_id)
        if active_compact and manual_vehicle_id and self._compact_id(manual_vehicle_id) != active_compact:
            return None
        return {
            "soc": self._clamp_percent(soc),
            "ts": ts or int(time.time()),
            "car_id": manual_car_id,
            "vehicle_id": manual_vehicle_id,
            "name": str(data.get("name") or "").strip(),
            "capacity_kwh": self._float(data.get("capacity"), 0.0),
        }

    def _openwb_pro_parse_session_ts(self, value):
        if value is None:
            return 0
        numeric = int(self._float(value, 0.0))
        if numeric > 0:
            return numeric
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
            kwh = self._float(kwh, 0.0)
            if kwh <= 0.02:
                return
            if kwh > self._float(best.get("kwh"), 0.0):
                best.update({"kwh": kwh, "start_ts": int(start_ts or 0), "source": source})

        session_file = os.path.join(RAMDISK_DIR, f"wb{self.wb_id}_live_session.json")
        try:
            with open(session_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("is_locked") is not False:
            last_ts = self._openwb_pro_parse_session_ts(data.get("last_ts") or data.get("ts"))
            if not last_ts or now_ts - last_ts <= 24 * 3600 or last_ts > now_ts:
                accept(
                    data.get("kwh", data.get("session_kwh", 0.0)),
                    self._openwb_pro_parse_session_ts(data.get("start_ts")),
                    "live_session",
                )

        soc_file = os.path.join(RAMDISK_DIR, f"manual_soc_wb{self.wb_id}.json")
        try:
            with open(soc_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("plugged") is not False:
            source = str(data.get("source") or "").strip()
            # Derived openWB-Pro SoC estimates are display/cache values, not
            # authoritative session anchors. Feeding their session_kwh back
            # into the estimator after a restart can add old charge energy to
            # the raw vehicle SoC again and make the car SoC jump.
            if source != "manual_start_soc":
                return best
            ts = int(self._float(data.get("ts"), 0.0))
            active_compact = self._compact_id(active_id)
            manual_vehicle_id = str(data.get("vehicle_id") or "").strip()
            if not active_compact or not manual_vehicle_id or self._compact_id(manual_vehicle_id) == active_compact:
                if not ts or now_ts - ts <= 12 * 3600 or ts > now_ts:
                    accept(
                        data.get("session_kwh", 0.0),
                        int(self._float(data.get("raw_soc_ts"), 0.0)) or ts,
                        source or "manual_soc",
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

        raw_valid = raw_soc is not None and raw_soc > 0
        raw_ts = int(raw_soc_ts or 0)
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
            self.state["car_soc"] = round(self.state["_soc_anchor_soc"], 1)
            self.state["car_soc_source"] = "manual_start_soc"
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
            self.state["_soc_raw_timestamp"] = raw_ts or int(now_ts)
            self.state["_soc_raw_value"] = raw_soc
            self.state["car_soc_raw_ts"] = raw_ts or None
            self.state["car_soc"] = round(self.state["_soc_anchor_soc"], 1)
            self.state["car_soc_source"] = "openwb_pro_raw"
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
            "api_surface": self.state.get("api_surface", "openwb_pro_connect_php"),
            "daily_imported_wh": round(self.state["daily_imported_wh"], 0),
            "imported_total_wh": round(self.state["imported_total_wh"], 0),
            "chargemode": self.state["chargemode_str"],
            "session_kwh": round(self.state["session_kwh"], 3),
            "cp_id": "pro",
            "wb_id": self.wb_id,
            "ts": int(time.time()),
            "source": "openwb_pro",
            "car_soc": self.state.get("car_soc", 0),
            "car_soc_source": self.state.get("car_soc_source", ""),
            "car_soc_raw_ts": self.state.get("car_soc_raw_ts"),
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
            "rfid_timestamp": self.state.get("rfid_timestamp"),
            "serial": self.state.get("serial"),
            "version": self.state.get("version"),
            "v2g_ready": self.state.get("v2g_ready", 0),
            "evse_signaling": self.state.get("evse_signaling", ""),
            "offered_current_raw": self.state.get("offered_current_raw", 0.0),
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

    def _update_from_connect_status(self, data):
        plug_state = self._boolish(data.get("plug_state"))
        charge_state = self._boolish(data.get("charge_state"))
        locked = bool(plug_state or self._boolish(data.get("locked")) or self._boolish(data.get("plug_locked")))
        live_vehicle_id = data.get("vehicle_id")
        live_rfid_tag = data.get("rfid_tag")
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
        effective_plug_state = bool(plug_state or locked or charge_state or power_w > 50.0 or live_vehicle_id or live_rfid_tag)
        restored_session = (
            self._openwb_pro_persisted_session_sample(live_vehicle_id or live_rfid_tag)
            if effective_plug_state
            else {"kwh": 0.0, "start_ts": 0}
        )
        restored_session_kwh = max(0.0, self._float(restored_session.get("kwh"), 0.0))
        restored_session_wh = restored_session_kwh * 1000.0
        if effective_plug_state and not prev_plug:
            if restored_session_wh > 20.0:
                self.state["_session_start_wh"] = max(0.0, imported_wh - restored_session_wh)
                self.state["session_kwh"] = restored_session_kwh
                logger.info(
                    f"[WB{self.wb_id}] openWB Pro: laufende Session mit "
                    f"{restored_session_kwh:.3f} kWh fortgefuehrt."
                )
            else:
                self.state["_session_start_wh"] = imported_wh
                self.state["session_kwh"] = 0.0
                logger.info(f"[WB{self.wb_id}] openWB Pro: Auto eingesteckt, Session-Zaehler gestartet.")
        elif not effective_plug_state:
            self.state["_session_start_wh"] = None
            self.state["_session_vehicle_id"] = None
            self.state["_session_rfid_tag"] = None

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

        soc = self._float(data.get("soc_value"), -1.0)
        soc_ts = int(self._float(data.get("soc_timestamp"), 0.0)) or None

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
            "pha": 56 if int(display_phases) >= 3 else (8 if int(display_phases) >= 1 else 0),
            "daily_imported_wh": imported_wh,
            "imported_total_wh": imported_wh,
            "chargemode_str": "instant" if visible_current >= 6 else "stop",
            "frc": 2 if visible_current >= 6 else 0,
            "car_name": car_name,
            "car_id": car_id,
            "vehicle_id": vehicle_id,
            "rfid_tag": rfid_tag,
            "rfid_timestamp": data.get("rfid_timestamp"),
            "serial": data.get("serial"),
            "version": data.get("version"),
            "v2g_ready": data.get("v2g_ready", 0),
            "evse_signaling": data.get("evse_signaling", ""),
            "offered_current_raw": offered_current,
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
        plug_state = bool(ev_present or pluggable)
        locked = bool(plug_state and (
            plug_status == "locked"
            or self._boolish(plug_lock_actual)
            or str(plug_lock_actual).strip() == "1"
        ))
        if plug_state and not prev_plug:
            self.state["_session_start_wh"] = daily_imported
            self.state["session_kwh"] = 0.0
            logger.info(f"[WB{self.wb_id}] openWB Pro: Auto eingesteckt, Session-Zaehler gestartet.")
        elif not plug_state:
            self.state["_session_start_wh"] = None

        start = self.state.get("_session_start_wh")
        if start is not None and plug_state:
            self.state["session_kwh"] = max(0.0, (daily_imported - start) / 1000.0)

        car_soc = self._float(self._dig(port, "ci/ev/soc/actual"), -1.0)
        car_soc_ts = int(self._float(self._dig(port, "ci/ev/soc/timestamp"), 0.0)) or None

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

    def get_status(self):
        data = self._get_json(self.status_url)
        if isinstance(data, dict):
            self._update_from_connect_status(data)
            return self._sanitize_measurement_status(self.state)

        data = self._get_json(self.fallback_status_url)
        if isinstance(data, dict):
            self._update_from_legacy_secc_status(data)
            return self._sanitize_measurement_status(self.state)

        logger.error(f"[WB{self.wb_id}] openWB Pro Status nicht lesbar (/connect.php, /api/secc)")
        return None

    def set_amp_and_state(self, target_amp, force_state=None):
        if not command_gate.allow_command(
            self,
            action="openwb_pro_set_amp_and_state",
            payload={"target_amp": target_amp, "force_state": force_state},
        ):
            return True
        try:
            raw_amp = float(target_amp or 0.0)
        except (TypeError, ValueError):
            raw_amp = 0.0
        if force_state == 1 or raw_amp < 0.5:
            amp = 0.0
        else:
            amp = _quantize_current_amp(raw_amp, step=self.current_step_amp)
        now = time.time()
        control_key = ("ampere", amp)
        is_keepalive = self._last_control_key == control_key
        repeat_after_s = 5.0 if amp >= 6.0 else 20.0
        if is_keepalive and now - self._last_control_ts < repeat_after_s:
            logger.debug(f"[WB{self.wb_id}] openWB Pro Sollstrom gedrosselt: {amp:.1f}A")
            return True

        heartbeat_ok = True
        if amp >= 6.0:
            heartbeat_ok = self._ensure_heartbeat_enabled(now)

        # Match openWB's openWB-Pro module: set_current writes only ampere.
        # Start verification, wakeup and CP retries are manager policy and
        # must be sent explicitly via trigger_cp_interrupt().
        ok = self._post_control({"ampere": f"{amp:.1f}" if amp else "0"})
        if ok:
            self.state["amp"] = int(round(amp))
            self.state["evse_current"] = amp
            self.state["offered_current_raw"] = amp
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
        return bool(ok and heartbeat_ok)

    def set_phases(self, phases):
        if not command_gate.allow_command(
            self,
            action="openwb_pro_set_phases",
            payload={"phases": phases},
        ):
            return True
        phases = 1 if int(phases) == 1 else 3
        now = time.time()
        phase_key = ("phase", phases)
        if self._last_phase_key == phase_key and now - self._last_phase_ts < 20.0:
            logger.debug(f"[WB{self.wb_id}] openWB Pro Phasenziel gedrosselt: {phases}p")
            return True
        # Laut openWB-Pro-Standalone-API ist phasetarget der offizielle
        # Schalter. Die Pro uebernimmt die Pause/Signalisierung zum Fahrzeug
        # selbst; ein manueller CP-Interrupt waere hier doppelt.
        ok = self._post_control({"phasetarget": str(phases)})
        if ok:
            self._last_phase_key = phase_key
            self._last_phase_ts = now
            self._last_control_key = None
            self.state["phases_target"] = phases
            self.state["pha"] = 56 if phases == 3 else 8
            logger.info(f"[WB{self.wb_id}] openWB Pro Phasenziel: {phases}p")
        return ok

    def set_heartbeat(self, enabled=True, now=None):
        if not command_gate.allow_command(
            self,
            action="openwb_pro_set_heartbeat",
            payload={"enabled": bool(enabled)},
        ):
            return True
        ok = self._post_control({"heartbeatenabled": "1" if enabled else "0"})
        if ok:
            self._heartbeat_enabled_assumed = bool(enabled)
            self._last_heartbeat_enable_ts = float(now) if enabled and now is not None else (time.time() if enabled else 0.0)
        return ok

    def _ensure_heartbeat_enabled(self, now=None):
        now = time.time() if now is None else float(now)
        if self._heartbeat_enabled_assumed and now - self._last_heartbeat_enable_ts < 300.0:
            return True
        return self.set_heartbeat(True, now=now)

    def trigger_cp_interrupt(self, duration=None, version=None):
        if not command_gate.allow_command(
            self,
            action="openwb_pro_cp_interrupt",
            payload={"duration": duration, "version": version},
        ):
            return True
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

    def release_to_default(self, max_amp=32):
        """openWB Pro Standalone sicher freigeben, ohne blind 32A anzubieten."""
        ok_heartbeat = self.set_heartbeat(True)
        ok_stop = self.set_amp_and_state(0, force_state=1)
        logger.info(
            f"[WB{self.wb_id}] openWB Pro Default-Freigabe: Standalone sicher gestoppt "
            f"(heartbeat={ok_heartbeat}, stop={ok_stop})"
        )
        return ok_heartbeat or ok_stop


# ===========================================================================
# Factory
# ===========================================================================
DISABLED_WALLBOX_TYPES = {
    "none",
    "disabled",
    "deaktiviert",
    "keine",
    "keine_wallbox",
    "no_wallbox",
    "off",
}

def create_charger(wb_type, ip, wb_id, config=None):
    """Erstellt den passenden Treiber fuer den konfigurierten Wallbox-Typ."""
    wb_type = str(wb_type).strip().lower()
    if not wb_type or wb_type in DISABLED_WALLBOX_TYPES:
        return None
    native_types = ("native", "e3dc", "e3dc_easy", "e3dc_legacy", "e3dc_auto", "e3dc_multi", "e3dc_multi_connect")
    if not ip and wb_type not in native_types:
        return None
    config = config or {}
    if wb_type == "go-e":
        return GoECharger(ip, wb_id)
    if wb_type == "openwb":
        return OpenWBCharger(ip, wb_id, config)
    if wb_type in ("openwb_pro", "openwb-pro", "openwbpro"):
        return OpenWBProCharger(ip, wb_id, config)
    if wb_type in ("e3dc_multi", "e3dc_multi_connect"):
        return E3DCMultiConnectCharger(ip, wb_id, config)
    if wb_type == "e3dc_auto":
        probe = E3DCMultiConnectCharger(ip, wb_id, config)
        if probe.is_direct_supported():
            logger.info(f"[WB{wb_id}] E3DC Auto-Erkennung: Multi Connect Direkt-RSCP aktiv.")
            return probe
        probe.suspend_external_control("Auto-Erkennung: kein Multi-Connect-Direktpfad")
        logger.info(f"[WB{wb_id}] E3DC Auto-Erkennung: Legacy/Easy-Connect Pfad aktiv.")
        return E3DCCharger(ip, wb_id, config)
    if wb_type in ("native", "e3dc", "e3dc_easy", "e3dc_legacy"):
        return E3DCCharger(ip, wb_id, config)
    logger.warning(f"Unbekannter Wallbox-Typ: '{wb_type}'")
    return None
