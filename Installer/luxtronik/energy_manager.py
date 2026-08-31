import sys
import time
import json
import copy
import os
import sys
import subprocess
import shutil
import math
import stat
from datetime import datetime, timedelta
import logging
from logging.handlers import RotatingFileHandler
import re
import requests
import sqlite3
from luxtronik import LuxtronikModbus
try:
    from quiet_logging import install_quiet_info_filter
    from decision_history import HISTORY_NORMAL_HEARTBEAT_S, write_history_record
    from ems_decision_diagnostics import (
        build_energy_surface_record,
        default_surface_path,
        write_decision_surface_record,
    )
    from consumer_priority import (
        validate_consumer_budget_contract,
        validate_consumer_command_allocations,
    )
except ModuleNotFoundError:
    _INSTALLER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _INSTALLER_DIR not in sys.path:
        sys.path.insert(0, _INSTALLER_DIR)
    from quiet_logging import install_quiet_info_filter
    from decision_history import HISTORY_NORMAL_HEARTBEAT_S, write_history_record
    from ems_decision_diagnostics import (
        build_energy_surface_record,
        default_surface_path,
        write_decision_surface_record,
    )
    from consumer_priority import (
        validate_consumer_budget_contract,
        validate_consumer_command_allocations,
    )

try:
    from Installer.Heat import forecast as heat_forecast
    from Installer.Heat import intent as heat_intent
    from Installer.Heat import policy as heat_policy
    from Installer import control_time
    from Installer.storage_dispatch_contract import (
        revision_hash as storage_contract_revision_hash,
    )
    from Installer.heat_actuator_safety import default_heat_actuator_gate
    from Installer.live_snapshot import (
        read_bound_json_value,
        read_runtime_live_snapshot,
    )
    from Installer.Wallbox.soc_tracker import (
        CONFIRMED_MANUAL_SOC_SOURCES,
        vehicle_soc_age_contract,
        vehicle_soc_max_age_s,
        vehicle_soc_source_contract,
    )
except ModuleNotFoundError:
    _INSTALLER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _INSTALLER_DIR not in sys.path:
        sys.path.insert(0, _INSTALLER_DIR)
    from Heat import forecast as heat_forecast
    from Heat import intent as heat_intent
    from Heat import policy as heat_policy
    import control_time
    from storage_dispatch_contract import (
        revision_hash as storage_contract_revision_hash,
    )
    from heat_actuator_safety import default_heat_actuator_gate
    from live_snapshot import read_bound_json_value, read_runtime_live_snapshot
    from Wallbox.soc_tracker import (
        CONFIRMED_MANUAL_SOC_SOURCES,
        vehicle_soc_age_contract,
        vehicle_soc_max_age_s,
        vehicle_soc_source_contract,
    )

try:
    from tariff_schedule import (
        TARIFF_TIMEZONE_NAME,
        supports_spot_market_prices,
        tariff_type as configured_tariff_type,
    )
except ModuleNotFoundError:
    from Installer.tariff_schedule import (
        TARIFF_TIMEZONE_NAME,
        supports_spot_market_prices,
        tariff_type as configured_tariff_type,
    )

logger = logging.getLogger("EnergyManager")
_SESSION_WRITE_WARNED = set()
UPDATE_DISPATCHER = "/usr/local/sbin/e3dc-web-update-launcher"


def _new_energy_actuator_gate():
    return default_heat_actuator_gate(
        __file__,
        "Installer/luxtronik/energy_manager.py",
        "energy_manager",
    )


def _authorize_heatpump_output(instance, action, driver_key=None):
    """Prüft exakten Tree und Treiber-Lease unmittelbar vor I/O erneut.

    Aus einem ungültigen Kontext ist keine Wärmepumpenfreigabe erlaubt. Das Halten
    des Vorzustands ist beabsichtigt: Ein laufender Verdichter darf nicht allein
    wegen eines verlorenen lokalen Installationskontexts gestoppt oder pausiert werden.
    """
    verdict = instance._actuator_gate.authorize(
        driver_key or instance._actuator_driver_key,
        action,
        allow_release_on_invalid=False,
        preserve_existing=True,
    )
    instance.actor_writes_blocked = not verdict.allowed
    instance.actor_write_block_reason = "" if verdict.allowed else verdict.reason
    if not verdict.allowed:
        logger.error("%s: Aktorausgang blockiert (%s)", instance.__class__.__name__, verdict.reason)
    return verdict.allowed


def write_json_atomic_tolerant(path, payload, mode=0o664, warn_label="Session-Datei"):
    """Write helper-state JSON without letting stale permissions kill the manager."""
    tmp_path = f"{path}.tmp.{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, "w") as f:
            json.dump(payload, f)
        try:
            os.chmod(tmp_path, mode)
        except Exception:
            pass
        os.replace(tmp_path, path)
        try:
            os.chmod(path, mode)
        except Exception:
            pass
        return True
    except Exception as exc:
        warn_key = f"{warn_label}:{path}"
        if warn_key not in _SESSION_WRITE_WARNED:
            suffix = (
                " Virtueller Fahrzeug-SoC wird bis zur Rechte-Reparatur nicht persistiert."
                if warn_label == "Session-Datei"
                else ""
            )
            logger.warning(
                "%s kann nicht geschrieben werden (%s): %s.%s",
                warn_label,
                path,
                exc,
                suffix,
            )
            _SESSION_WRITE_WARNED.add(warn_key)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def write_bounded_json_checkpoint(
    path,
    payload,
    runtime_state,
    *,
    semantic_keys,
    heartbeat_s=120.0,
    now_ts=None,
    force=False,
    warn_label="Checkpoint-Datei",
):
    """Persistiert kompakte Restart-Zustände nur bei Kanten oder Heartbeat."""

    current_ts = time.time() if now_ts is None else float(now_ts)
    heartbeat_s = max(60.0, min(900.0, float(heartbeat_s or 120.0)))
    signature_payload = {
        str(key): payload.get(key)
        for key in semantic_keys
    }
    signature = json.dumps(
        signature_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    last_write_ts = float(runtime_state.get("last_write_ts", 0.0) or 0.0)
    elapsed_s = current_ts - last_write_ts
    semantic_changed = signature != runtime_state.get("signature")
    heartbeat_due = (
        last_write_ts <= 0.0
        or elapsed_s < 0.0
        or elapsed_s >= heartbeat_s
    )
    if not (force or semantic_changed or heartbeat_due):
        return False

    checkpoint = dict(payload)
    checkpoint["checkpoint_ts"] = current_ts
    if not write_json_atomic_tolerant(
        path,
        checkpoint,
        mode=0o664,
        warn_label=warn_label,
    ):
        return False
    runtime_state["signature"] = signature
    runtime_state["last_write_ts"] = current_ts
    return True


class SafeLuxtronik:
    """Stateful Modbus Wrapper. Hält EINE Verbindung dauerhaft offen.
    Verhindert Fehler 816 (Mehrere Quellen) durch Port-Wiederverwendung und
    Fehler 1313 (Timeout) durch regelmäßige Keep-Alive Pings."""
    def __init__(self, ip, safety_gate=None):
        from luxtronik import LuxtronikModbus
        self.wp = LuxtronikModbus(ip)
        self.ip = ip
        self._connected = False
        self.last_activity = 0
        self.curr_ext_ww = 0
        self.curr_ext_hz = 0
        self.last_sent_ww = None
        self.last_sent_hz = None
        self.last_sent_ww_time = 0.0
        self.last_sent_hz_time = 0.0
        self.last_failed_ww = None
        self.last_failed_hz = None
        self.last_failed_ww_time = 0.0
        self.last_failed_hz_time = 0.0
        self._actuator_gate = safety_gate or _new_energy_actuator_gate()
        self._actuator_driver_key = f"transport:luxtronik-shi:{self.ip}:502"
        self.actor_writes_blocked = False
        self.actor_write_block_reason = ""

    def connect(self):
        if self._connected and getattr(self.wp, 'socket', None) is not None:
            return True
        self._connected = False
        if self.wp.connect():
            self._connected = True
            self.last_activity = time.time()
            logger.debug(f"Luxtronik Modbus TCP Verbindung erfolgreich aufgebaut ({self.ip})")
            return True
        return False

    def set_boost(self, hz_mode, hz_temp, ww_mode, ww_temp, kuehl_mode=0, kuehl_soll=None, wp_data=None):
        success = False
        if not _authorize_heatpump_output(self, "luxtronik:set_boost_preflight"):
            return False
        if self.connect():
            try:
                # --- Software Thermostat für Luxtronik ---
                new_hz_mode = hz_mode
                new_ww_mode = ww_mode

                if wp_data:
                    ist_ww = wp_data.get('Warmwasser_Ist')
                    ist_hz = wp_data.get('Ruecklauf_Ist')

                    if ww_mode == 1 and ww_temp and ist_ww is not None:
                        target_ww = float(ww_temp)
                        ww_physically_running = heatpump_ww_cycle_running(
                            {},
                            wp_data,
                            ww_requested=True,
                        )
                        if ist_ww >= target_ww:
                            new_ww_mode = 0
                        elif (
                            ww_physically_running
                            or getattr(self, 'curr_ext_ww', 0) == 1
                        ):
                            # Einen realen Zyklus oder den bestätigt gesetzten
                            # Auftrag bis zur zentralen Rücknahmekante stabil
                            # halten. curr_ext_ww ist nur Auftragskontinuität,
                            # ausdrücklich keine physische WW-Laufevidenz.
                            new_ww_mode = 1
                        elif ist_ww < (target_ww - 8.0):
                            new_ww_mode = 1
                        else:
                            # Ein niedrigerer interner Sollwert ist allein kein
                            # Startsignal. So entsteht am Ziel kein 55/Auto-Flattern.
                            new_ww_mode = 0

                    if hz_mode == 1 and hz_temp and ist_hz is not None:
                        target_hz = float(hz_temp)
                        if ist_hz < (target_hz - 2.0):
                            new_hz_mode = 1
                        elif ist_hz >= target_hz:
                            new_hz_mode = 0
                        else:
                            new_hz_mode = getattr(self, 'curr_ext_hz', 0)

                res1, res2 = True, True
                close_on_failure = False

                # Sende nur, wenn sich der Zustand geändert hat (verhindert ständige Modbus-Writes & Takten!)
                if hz_mode is not None:
                    target_hz_tuple = (new_hz_mode, hz_temp if new_hz_mode else None)
                    if self.last_sent_hz != target_hz_tuple:
                        hz_write_attempted = False
                        if (
                            self.last_failed_hz == target_hz_tuple
                            and time.time() - self.last_failed_hz_time < 60.0
                        ):
                            res1 = False
                        else:
                            if _authorize_heatpump_output(self, "luxtronik:write_hz_boost"):
                                hz_write_attempted = True
                                res1 = self.wp.write_hz_boost(new_hz_mode, target_hz_tuple[1])
                            else:
                                res1 = False
                        if res1 is not False and res1 is not None:
                            self.last_sent_hz = target_hz_tuple
                            self.last_sent_hz_time = time.time()
                            self.last_failed_hz = None
                            self.last_failed_hz_time = 0.0
                            self.curr_ext_hz = new_hz_mode
                            logger.info(f"Luxtronik: HZ Modbus aktualisiert -> Mode={new_hz_mode}, Temp={target_hz_tuple[1]}")
                        elif hz_write_attempted:
                            self.last_failed_hz = target_hz_tuple
                            self.last_failed_hz_time = time.time()
                            close_on_failure = True
                    else:
                        self.curr_ext_hz = new_hz_mode

                if ww_mode is not None:
                    target_ww_tuple = (new_ww_mode, ww_temp if new_ww_mode else None)
                    if self.last_sent_ww != target_ww_tuple:
                        ww_write_attempted = False
                        if (
                            self.last_failed_ww == target_ww_tuple
                            and time.time() - self.last_failed_ww_time < 60.0
                        ):
                            res2 = False
                        else:
                            if _authorize_heatpump_output(self, "luxtronik:write_ww_boost"):
                                ww_write_attempted = True
                                res2 = self.wp.write_ww_boost(new_ww_mode, target_ww_tuple[1])
                            else:
                                res2 = False
                        if res2 is not False and res2 is not None:
                            self.last_sent_ww = target_ww_tuple
                            self.last_sent_ww_time = time.time()
                            self.last_failed_ww = None
                            self.last_failed_ww_time = 0.0
                            self.curr_ext_ww = new_ww_mode
                            logger.info(f"Luxtronik: WW Modbus aktualisiert -> Mode={new_ww_mode}, Temp={target_ww_tuple[1]}")
                        elif ww_write_attempted:
                            self.last_failed_ww = target_ww_tuple
                            self.last_failed_ww_time = time.time()
                            close_on_failure = True
                    else:
                        self.curr_ext_ww = new_ww_mode

                if res1 is None or res1 is False or res2 is None or res2 is False:
                    if close_on_failure:
                        self.close()
                else:
                    success = True
                    self.last_activity = time.time()
            except:
                self.close()
        return success

    def write_hz_boost(self, mode, temp=None):
        return self.set_boost(mode, temp, None, None)

    def write_ww_boost(self, mode, temp=45.0):
        return self.set_boost(None, None, mode, temp)

    def observe_shi_status(self, hz_mode, hz_setpoint, ww_mode, ww_setpoint):
        """Synchronisiert bestätigte SHI-Werte ohne Wiederholung durch Statuslatenz."""
        now_ts = time.time()
        if hz_mode in (0, 1, 2):
            observed_hz = (hz_mode, hz_setpoint if hz_mode else None)
            if (
                self.last_sent_hz is None
                or observed_hz == self.last_sent_hz
                or now_ts - self.last_sent_hz_time >= 60.0
            ):
                self.curr_ext_hz = hz_mode
                self.last_sent_hz = observed_hz
        if ww_mode in (0, 1, 2):
            observed_ww = (ww_mode, ww_setpoint if ww_mode else None)
            if (
                self.last_sent_ww is None
                or observed_ww == self.last_sent_ww
                or now_ts - self.last_sent_ww_time >= 60.0
            ):
                self.curr_ext_ww = ww_mode
                self.last_sent_ww = observed_ww

    def read_runtime_status(self):
        """Liest physische Zustände über die bereits bestehende SHI-Sitzung."""
        if not self.connect():
            return {}
        try:
            result = self.wp.read_runtime_status()
            usable = bool(
                isinstance(result, dict)
                and any(
                    result.get(key) is not None
                    for key in ('Status_Waermepumpe_Bitmask', 'Betriebsart')
                )
            )
            if not usable:
                self.close()
                return {}
            self.last_activity = time.time()
            return result
        except Exception:
            self.close()
            return {}

    def write_zirkulation(self, mode):
        success = False
        if not _authorize_heatpump_output(self, "luxtronik:write_zirkulation_preflight"):
            return False
        if self.connect():
            try:
                if not _authorize_heatpump_output(self, "luxtronik:write_zirkulation"):
                    return False
                res = self.wp.write_zirkulation(mode)
                if res is None or res is False:
                    self.close()
                else:
                    success = True
                    self.last_activity = time.time()
            except:
                self.close()
        return success

    def keep_alive(self, force_open=True):
        """Hält die Modbus-Verbindung offen. Gibt die Modbus-Außentemperatur zurueck
        (oder None bei Fehler) -- wird im Hauptloop mit WebSocket-Wert verglichen."""
        if not force_open:
            self.close()
            return None

        # WICHTIG: Die Wärmepumpe fällt in den Automatik-Modus zurück, wenn
        # die Modbus-Verbindung geschlossen wird oder idled. MUSS offen bleiben!
        # Polling: Lese Außentemperatur (Input-Reg 10108, FC4) als Keep-Alive, max alle 5s
        if self.connect():
            if time.time() - self.last_activity >= 5:
                try:
                    val = self.wp._send_request(4, 10108, 1)  # Input-Reg 10108 = Außentemperatur
                    if val is None:
                        self.close()
                        return None
                    else:
                        self.last_activity = time.time()
                        # Rohwert: signed int16 / 10 = Temperatur in Grad Celsius
                        raw = val[0]
                        signed = raw if raw < 32768 else raw - 65536
                        modbus_at = signed / 10.0
                        self.last_modbus_at = modbus_at
                        return modbus_at
                except:
                    self.close()
                    return None
        return None

    def read_source_temperatures(self):
        """Liest Aussen/Sole-Temperaturen direkt per Modbus als Fallback."""
        if not self.connect():
            return {}
        try:
            vals = self.wp._send_request(4, 10108, 4)
            if not vals or len(vals) < 4:
                self.close()
                return {}

            def to_s(raw):
                signed = raw if raw < 32768 else raw - 65536
                return signed / 10.0

            self.last_activity = time.time()
            return {
                'Aussentemp': to_s(vals[0]),
                'Aussentemp_Mittel': to_s(vals[1]),
                'Sole_Ein': to_s(vals[2]),
                'Sole_Aus': to_s(vals[3]),
            }
        except Exception:
            self.close()
            return {}


    def close(self):
        if self._connected:
            logger.debug("Luxtronik Modbus TCP Verbindung wird geschlossen.")
            try: self.wp.close()
            except: pass
            self._connected = False

    def update_surplus(self, grid_w, **kwargs):
        pass # Für Luxtronik ignorieren wir kontinuierlichen Überschuss

class ShellyHeatpump:
    """Wrapper für Wärmepumpen via Shelly (SG-Ready Boost und/oder EVU Pause)."""
    def __init__(self, sg_ip, pause_ip, state_path="", safety_gate=None):
        self.sg_ip = sg_ip
        self.pause_ip = pause_ip
        self.state_path = state_path
        self._connected = True
        self.sg_state = None
        self.pause_state = None
        self.last_sync_reason = ""
        self.last_live_sg_state = None
        self.last_live_pause_state = None
        self.last_live_sg_ts = 0.0
        self.last_live_pause_ts = 0.0
        self.last_sg_readback_attempt_ts = 0.0
        self._actuator_gate = safety_gate or _new_energy_actuator_gate()
        self._actuator_driver_key = f"heatpump:shelly-controller:{self.sg_ip}:{self.pause_ip}"
        self.actor_writes_blocked = False
        self.actor_write_block_reason = ""
        self._checkpoint_runtime = {}
        self._checkpoint_heartbeat_s = 120.0
        self._load_persisted_state()

    @staticmethod
    def _bool_or_none(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lower = value.strip().lower()
            if lower in ("1", "true", "on", "yes", "ja", "ein"):
                return True
            if lower in ("0", "false", "off", "no", "nein", "aus"):
                return False
        return None

    def _load_persisted_state(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                return
            if state.get("sg_ip") == self.sg_ip:
                self.sg_state = self._bool_or_none(state.get("sg_state"))
            if state.get("pause_ip") == self.pause_ip:
                self.pause_state = self._bool_or_none(state.get("pause_state"))
        except Exception as exc:
            logger.debug("Shelly WP: gespeicherter Relaisstatus nicht lesbar: %s", exc)

    def _persist_state(self, reason="", force=False):
        if not self.state_path:
            return False
        payload = {
            "ts": datetime.now().isoformat(),
            "sg_ip": self.sg_ip,
            "pause_ip": self.pause_ip,
            "sg_state": self.sg_state,
            "pause_state": self.pause_state,
            "live_sg_state": self.last_live_sg_state,
            "live_pause_state": self.last_live_pause_state,
            "live_sg_ts": self.last_live_sg_ts,
            "live_pause_ts": self.last_live_pause_ts,
            "reason": reason or self.last_sync_reason,
        }
        return write_bounded_json_checkpoint(
            self.state_path,
            payload,
            self._checkpoint_runtime,
            semantic_keys=(
                "sg_ip",
                "pause_ip",
                "sg_state",
                "pause_state",
                "live_sg_state",
                "live_pause_state",
            ),
            heartbeat_s=self._checkpoint_heartbeat_s,
            force=force,
            warn_label="Shelly-WP-Statusdatei",
        )

    @staticmethod
    def _response_json(response):
        if response is None:
            return {}
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        if hasattr(response, "json"):
            try:
                data = response.json()
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return {}

    def _read_relay_state(self, ip):
        if not ip or ip == '0.0.0.0':
            return None
        # Gen1: /relay/0, Gen2/Plus/Pro: /rpc/Switch.GetStatus?id=0
        for url in (
            f"http://{ip}/relay/0",
            f"http://{ip}/rpc/Switch.GetStatus?id=0",
        ):
            try:
                data = self._response_json(requests.get(url, timeout=2))
                state = self._bool_or_none(data.get("ison"))
                if state is None:
                    state = self._bool_or_none(data.get("output"))
                if state is not None:
                    return state
            except Exception:
                continue
        return None

    def _record_live_relay_state(self, ip, state, now_ts=None):
        """Materialisiert ausschließlich einen tatsächlich gelesenen Relaiszustand."""

        if not isinstance(state, bool):
            return False
        readback_ts = time.time() if now_ts is None else _safe_float(now_ts, 0.0)
        if readback_ts <= 0.0:
            return False
        recorded = False
        if self._relay_configured(self.sg_ip) and ip == self.sg_ip:
            self.last_live_sg_state = state
            self.last_live_sg_ts = readback_ts
            recorded = True
        if self._relay_configured(self.pause_ip) and ip == self.pause_ip:
            self.last_live_pause_state = state
            self.last_live_pause_ts = readback_ts
            recorded = True
        return recorded

    def refresh_relay_states(self):
        self.last_sg_readback_attempt_ts = time.time()
        sg_live = self._read_relay_state(self.sg_ip)
        pause_live = self._read_relay_state(self.pause_ip)
        readback_ts = time.time()
        if sg_live is not None:
            self.sg_state = sg_live
            self._record_live_relay_state(self.sg_ip, sg_live, readback_ts)
        if pause_live is not None:
            self.pause_state = pause_live
            self._record_live_relay_state(self.pause_ip, pause_live, readback_ts)
        if sg_live is not None or pause_live is not None:
            self._persist_state("refresh")
        return {"sg": sg_live, "pause": pause_live}

    def refresh_sg_readback_if_due(self, now_ts=None, min_interval_s=30.0):
        """Bindet frische Relais-Istwerte, ohne selbst eine Schaltkante auszulösen."""

        if not self._relay_configured(self.sg_ip):
            return None
        current_ts = time.time() if now_ts is None else _safe_float(now_ts, 0.0)
        if current_ts <= 0.0:
            return None
        interval_s = max(5.0, _safe_float(min_interval_s, 30.0))
        if (current_ts - self.last_sg_readback_attempt_ts) < interval_s:
            return self.last_live_sg_state
        self.last_sg_readback_attempt_ts = current_ts
        sg_live = self._read_relay_state(self.sg_ip)
        pause_live = self._read_relay_state(self.pause_ip)
        readback_ts = time.time()
        cache_changed = False
        if isinstance(sg_live, bool):
            cache_changed = self.sg_state != sg_live
            self.sg_state = sg_live
            self._record_live_relay_state(self.sg_ip, sg_live, readback_ts)
        if isinstance(pause_live, bool):
            cache_changed = cache_changed or self.pause_state != pause_live
            self.pause_state = pause_live
            self._record_live_relay_state(self.pause_ip, pause_live, readback_ts)
        if cache_changed:
            # Nur eine bestätigte Soll/Ist-Abweichung ist eine semantische
            # Statuskante. Gleichbleibende 30-s-Readbacks erzeugen keinen
            # zusätzlichen SD-Schreibverkehr.
            self._persist_state("relay_readback_reconciled")
        return sg_live

    @staticmethod
    def _relay_configured(ip):
        return bool(str(ip or "").strip() not in ("", "0.0.0.0"))

    def relay_readback_contract(self, relay_states):
        """Bewertet nur frische Aktorzustände, keine Wärmepumpen-Telemetrie."""

        states = relay_states if isinstance(relay_states, dict) else {}
        configured = {
            "sg": self._relay_configured(self.sg_ip),
            "pause": self._relay_configured(self.pause_ip),
        }
        required = tuple(name for name, is_configured in configured.items() if is_configured)
        valid = bool(required) and all(isinstance(states.get(name), bool) for name in required)
        return {
            "valid": valid,
            "required_contacts": required,
            "confirmed_contacts": tuple(
                name for name in required if isinstance(states.get(name), bool)
            ),
            "source": "shelly_relay_readback",
        }

    def _write_relay_state(self, ip, target_on):
        if not ip or ip == '0.0.0.0':
            return True
        if not _authorize_heatpump_output(
            self,
            f"shelly:relay:{ip}:{'on' if target_on else 'off'}",
            driver_key=f"transport:http-shelly:{ip}:switch:0",
        ):
            return False
        state_str = "on" if target_on else "off"
        write_ok = False
        try:
            response = requests.get(f"http://{ip}/relay/0?turn={state_str}", timeout=2)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            write_ok = True
        except Exception:
            try:
                if not _authorize_heatpump_output(
                    self,
                    f"shelly:relay:{ip}:{'on' if target_on else 'off'}:gen2",
                    driver_key=f"transport:http-shelly:{ip}:switch:0",
                ):
                    return False
                response = requests.post(
                    f"http://{ip}/rpc",
                    json={"id": 1, "method": "Switch.Set", "params": {"id": 0, "on": bool(target_on)}},
                    timeout=2,
                )
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                write_ok = True
            except Exception:
                return False
        if not write_ok:
            return False
        confirmed = self._read_relay_state(ip)
        if confirmed != bool(target_on):
            logger.error(
                "Shelly WP %s: Relais-Readback unbestätigt (Soll=%s Ist=%s)",
                ip,
                bool(target_on),
                confirmed,
            )
            return False
        self._record_live_relay_state(ip, confirmed, time.time())
        return True

    def _rollback_safe_state(self):
        """Bestätigt nach einem Teilschreibvorgang: kein externer Boost und keine EVU-Pause."""
        results = []
        if self.sg_ip and self.sg_ip != '0.0.0.0':
            sg_ok = self._write_relay_state(self.sg_ip, False)
            results.append(sg_ok)
            if sg_ok:
                self.sg_state = False
        if self.pause_ip and self.pause_ip != '0.0.0.0':
            pause_ok = self._write_relay_state(self.pause_ip, True)
            results.append(pause_ok)
            if pause_ok:
                self.pause_state = True
        rollback_ok = bool(results and all(results)) if results else True
        self.last_sync_reason = (
            "partial_write_safe_rollback_confirmed"
            if rollback_ok
            else "partial_write_safe_rollback_incomplete"
        )
        self._persist_state(self.last_sync_reason, force=True)
        return rollback_ok

    def connect(self): return True
    def close(self): pass
    def keep_alive(self, force_open=True): pass

    def set_boost(self, hz_mode, hz_temp, ww_mode, ww_temp, kuehl_mode=0, kuehl_soll=None, wp_data=None, force=False, reason=""):
        if not _authorize_heatpump_output(self, "shelly:set_boost_preflight"):
            return False
        # Luxtronik-Pause (Aushungern) erkennen: hz_mode=1, hz_temp<=20.0
        is_pause = (hz_mode == 1 and hz_temp is not None and hz_temp <= 20.0)
        is_boost = False if is_pause else (hz_mode == 1 or ww_mode == 1)

        success = True
        write_attempted = False
        self.last_sync_reason = reason or ("pause" if is_pause else ("boost" if is_boost else "normal"))

        # 1. EVU / Pause Kontakt (Normally Closed -> On = Heizen, Off = Pause/Gesperrt)
        if self.pause_ip and self.pause_ip != '0.0.0.0':
            target_pause = False if is_pause else True
            if force or target_pause != self.pause_state:
                write_attempted = True
                if self._write_relay_state(self.pause_ip, target_pause):
                    self.pause_state = target_pause
                else:
                    success = False

        # 2. SG-Ready Boost Kontakt
        if success and self.sg_ip and self.sg_ip != '0.0.0.0':
            if force or is_boost != self.sg_state:
                write_attempted = True
                if self._write_relay_state(self.sg_ip, is_boost):
                    self.sg_state = is_boost
                else:
                    success = False

        if not success and write_attempted:
            rollback_ok = self._rollback_safe_state()
            if not rollback_ok:
                logger.error("Shelly WP: bestätigter Safe-State-Rollback unvollständig")
            return False

        self._persist_state(self.last_sync_reason, force=write_attempted)

        return success

    def write_hz_boost(self, mode, temp=None):
        return self.set_boost(mode, temp, 0, None)

    def write_ww_boost(self, mode, temp=45.0):
        return self.set_boost(0, None, mode, temp)
    def write_zirkulation(self, mode): pass

    def update_surplus(self, grid_w, **kwargs):
        pass # Für Luxtronik ignorieren wir kontinuierlichen Überschuss

class IDMHeatpump:
    """Modbus Wrapper für IDM-Wärmepumpen (Native Überschusssteuerung via Reg 74)."""
    COOLING_START_HYSTERESIS_C = 2.0

    def __init__(self, ip, safety_gate=None):
        self.ip = ip
        self._connected = False
        self.client = None
        self.last_activity = 0
        self.last_pw_logged = None
        self.last_sent_surplus_kw = 0.0
        self.last_surplus_write_ts = 0.0
        self.surplus_enabled = True
        self.surplus_max_kw = 2.0
        self.surplus_min_kw = 0.8
        self.surplus_ramp_kw = 0.2
        self.surplus_deadband_kw = 0.1
        self.surplus_heartbeat_s = 60.0
        self.surplus_min_write_interval_s = 10.0
        self.last_surplus_info_bucket = None

        # Interner Status für Thermostat-Hysterese
        self.curr_ext_ww = False
        self.curr_ext_hz = False
        self.curr_ext_khl = False
        self._actuator_gate = safety_gate or _new_energy_actuator_gate()
        self._actuator_driver_key = f"transport:modbus-tcp:{self.ip}:502"
        self.actor_writes_blocked = False
        self.actor_write_block_reason = ""
        self.last_boost_outcome = {
            "status": "idle",
            "attempted": False,
            "command_sent": False,
            "readback_confirmed": False,
            "reason": "",
        }

    def _set_boost_outcome(self, status, *, attempted, command_sent, readback_confirmed, reason=""):
        self.last_boost_outcome = {
            "status": str(status),
            "attempted": bool(attempted),
            "command_sent": bool(command_sent),
            "readback_confirmed": bool(readback_confirmed),
            "reason": str(reason or ""),
        }

    def connect(self):
        # Modbus-TCP Server (speziell IDM) hassen Dauerverbindungen!
        # Daher versuchen wir hier in der Hauptschleife gar nicht erst einen Socket
        # aufzubauen, um den Port nicht für idm_live.py zu blockieren.
        self._connected = True
        self.last_activity = time.time()
        return True

    def close(self):
        self._connected = False

    def keep_alive(self, force_open=True):
        pass # Für IDM verboten: Keine Dauerverbindungen!

    def _read_float(self, client, address):
        """Liest eine 32-Bit Float aus 2 Registern der IDM (High/Low Word order IDM-spezifisch)."""
        import struct, math
        res = client.read_holding_registers(address=address, count=2)
        if res.isError() or len(res.registers) < 2: return None
        packed = struct.pack('>HH', res.registers[1], res.registers[0])
        val = struct.unpack('>f', packed)[0]
        return None if math.isnan(val) or math.isinf(val) else round(val, 1)

    def configure_surplus(self, enabled=True, max_kw=2.0, min_kw=0.8, ramp_kw=0.2, deadband_kw=0.1, heartbeat_s=60.0, min_write_interval_s=10.0):
        self.surplus_enabled = bool(int(float(enabled))) if str(enabled).strip() != "" else True
        self.surplus_max_kw = max(0.0, float(max_kw or 0.0))
        self.surplus_min_kw = max(0.0, float(min_kw or 0.0))
        self.surplus_ramp_kw = max(0.05, float(ramp_kw or 0.2))
        self.surplus_deadband_kw = max(0.02, float(deadband_kw or 0.1))
        self.surplus_heartbeat_s = max(15.0, float(heartbeat_s or 60.0))
        self.surplus_min_write_interval_s = max(2.0, float(min_write_interval_s or 10.0))

    def _write_float(self, client, address, value):
        """Schreibt einen 32-Bit Float in IDM-Wordorder (Low-Word vor High-Word)."""
        import struct
        packed = struct.pack('>f', float(value))
        regs = struct.unpack('>HH', packed)
        return client.write_registers(address, [regs[1], regs[0]])

    @staticmethod
    def _response_ok(result):
        return bool(result is not None and not (hasattr(result, "isError") and result.isError()))

    def _write_register_confirmed(self, client, address, value, action):
        if not _authorize_heatpump_output(self, action):
            return False
        result = client.write_register(address=address, value=int(value))
        if not self._response_ok(result):
            return False
        readback = client.read_holding_registers(address=address, count=1)
        if not self._response_ok(readback) or not getattr(readback, "registers", None):
            return False
        return int(readback.registers[0]) == int(value)

    def _write_float_confirmed(self, client, address, value, action):
        if not _authorize_heatpump_output(self, action):
            return False
        result = self._write_float(client, address, value)
        if not self._response_ok(result):
            return False
        readback = self._read_float(client, address)
        return readback is not None and abs(float(readback) - float(value)) <= 0.11

    def _rollback_safe_state_confirmed(self, client, reason):
        """Entfernt alle schreibbaren externen Anforderungen.

        Register 1006 ist der vom Navigator gemeldete Read-only-Smart-Grid-Status
        und darf nie Teil eines Befehls oder Rollbacks sein.
        """
        steps = (
            (1710, 0, "idm:rollback_heat_request"),
            (1712, 0, "idm:rollback_ww_request"),
            (1711, 0, "idm:rollback_cooling_request"),
        )
        results = []
        for address, value, action in steps:
            try:
                results.append(self._write_register_confirmed(client, address, value, action))
            except Exception:
                results.append(False)
        rollback_ok = bool(results and all(results))
        if rollback_ok:
            self.curr_ext_hz = False
            self.curr_ext_ww = False
            self.curr_ext_khl = False
            self.last_ext_states = (False, False, False)
            logger.warning("IDM Safe-State-Rollback bestätigt (%s)", reason)
        else:
            self.curr_ext_hz = None
            self.curr_ext_ww = None
            self.curr_ext_khl = None
            self.last_ext_states = None
            logger.error("IDM Safe-State-Rollback unvollständig (%s)", reason)
        return rollback_ok

    def _ramp_surplus_kw(self, target_kw):
        current = float(getattr(self, 'last_sent_surplus_kw', 0.0) or 0.0)
        # Ein sinkendes zentrales Wattlimit ist eine Schutzkante und darf nicht
        # durch die Komfort-Rampe verzögert werden. Nur zusätzliche Leistung
        # wird schrittweise angeboten.
        if target_kw < current:
            return target_kw
        if target_kw > current + self.surplus_ramp_kw:
            return current + self.surplus_ramp_kw
        return target_kw

    def set_boost(self, hz_mode, hz_temp, ww_mode, ww_temp, kuehl_mode=0, kuehl_soll=None, wp_data=None):
        """Software-Thermostat: Überwacht Ist-Temperaturen und schaltet nur 1710/1711/1712."""
        if not _authorize_heatpump_output(self, "idm:set_boost_preflight"):
            self._set_boost_outcome(
                "blocked",
                attempted=True,
                command_sent=False,
                readback_confirmed=False,
                reason=self.actor_write_block_reason or "actor_gate_denied",
            )
            return False
        from pymodbus.client import ModbusTcpClient
        client = ModbusTcpClient(self.ip, port=502)

        connected = False
        for _ in range(3):
            if client.connect():
                connected = True
                break
            time.sleep(1)

        if not connected:
            self._set_boost_outcome(
                "failed",
                attempted=True,
                command_sent=False,
                readback_confirmed=False,
                reason="modbus_connect_failed",
            )
            logger.error(f"IDM set_boost: Modbus-Verbindung zu {self.ip} fehlgeschlagen (Port belegt?)")
            return False

        writes_started = False
        try:
            # 1. Ist-Werte auslesen
            ist_ww_zapf = self._read_float(client, 1030) # Warmwasserzapftemperatur
            ist_ww_oben = self._read_float(client, 1014) # Trinkwassererwaermertemp. oben
            ist_ww = ist_ww_oben if ist_ww_oben is not None else ist_ww_zapf
            ist_hz = self._read_float(client, 1008) # Wärmespeichertemperatur
            ist_khl = self._read_float(client, 1010) # Kältespeichertemperatur

            # --- PV-Pause Logik (Aushungern) ---
            is_pause = (hz_mode == 1 and hz_temp is not None and hz_temp <= 20.0)

            new_ext_ww = False
            new_ext_hz = False
            new_ext_khl = False

            if not is_pause:
                # --- Warmwasser Logik ---
                if ww_mode == 1 and ww_temp and ist_ww is not None:
                    target_ww = float(ww_temp)
                    # 8°C Hysterese für Warmwasser, um WP-Takten im Minutentakt zu verhindern!
                    if ist_ww < (target_ww - 8.0):
                        new_ext_ww = True
                    elif ist_ww >= target_ww:
                        new_ext_ww = False
                    else:
                        new_ext_ww = getattr(self, 'curr_ext_ww', False)

                # --- Heizung Logik ---
                if hz_mode == 1 and hz_temp and ist_hz is not None:
                    target_hz = float(hz_temp)
                    if ist_hz < (target_hz - 2.0):
                        new_ext_hz = True
                    elif ist_hz >= target_hz:
                        new_ext_hz = False
                    else:
                        new_ext_hz = getattr(self, 'curr_ext_hz', False)

                # --- Kühlung Logik ---
                if kuehl_mode == 1 and kuehl_soll and ist_khl is not None:
                    target_khl = float(kuehl_soll)
                    if ist_khl > (target_khl + self.COOLING_START_HYSTERESIS_C):
                        new_ext_khl = True
                    elif ist_khl <= target_khl:
                        new_ext_khl = False
                    else:
                        new_ext_khl = getattr(self, 'curr_ext_khl', False)

            # --- 2. Register nur schreiben wenn nötig ---
            val_ww = 1 if new_ext_ww else 0
            val_hz = 1 if new_ext_hz else 0
            val_khl = 1 if new_ext_khl else 0

            status_ww = f"EIN (Speicher oben: {ist_ww}°C < Soll: {ww_temp}°C)" if new_ext_ww else f"AUS (Speicher oben: {ist_ww}°C, Zapf: {ist_ww_zapf}°C)"
            status_hz = f"EIN (Ist: {ist_hz}°C < Soll: {hz_temp}°C)" if new_ext_hz else f"AUS (Ist: {ist_hz}°C)"
            status_khl = f"EIN (Ist: {ist_khl}°C > Soll: {kuehl_soll}°C)" if new_ext_khl else (f"AUS (Ist: {ist_khl}°C)" if ist_khl is not None else "AUS")

            log_str = f"WW: {status_ww} | HZ: {status_hz} | KHL: {status_khl}" if (ww_mode == 1 or hz_mode == 1 or kuehl_mode == 1) else "Alle externen Anforderungen deaktiviert."

            current_states = (new_ext_ww, new_ext_hz, new_ext_khl)
            if not hasattr(self, 'last_ext_states') or self.last_ext_states != current_states:
                writes_started = True
                writes_ok = (
                    self._write_register_confirmed(client, 1712, val_ww, "idm:write_ww_request")
                    and self._write_register_confirmed(client, 1710, val_hz, "idm:write_heat_request")
                    and self._write_register_confirmed(client, 1711, val_khl, "idm:write_cooling_request")
                )
                if not writes_ok:
                    raise RuntimeError("IDM Thermostat-Write/Readback nicht vollständig bestätigt")
                self.curr_ext_ww = new_ext_ww
                self.curr_ext_hz = new_ext_hz
                self.curr_ext_khl = new_ext_khl
                logger.info(f"IDM Thermostat Status-Wechsel -> {log_str}")
                self.last_ext_states = current_states

        except Exception as e:
            if writes_started:
                self._rollback_safe_state_confirmed(client, "set_boost_partial_failure")
            self._set_boost_outcome(
                "failed",
                attempted=True,
                command_sent=writes_started,
                readback_confirmed=False,
                reason=f"write_or_readback_failed:{type(e).__name__}",
            )
            logger.error(f"Fehler im IDM Thermostat (set_boost): {e}")
            return False
        finally:
            client.close()

        self._set_boost_outcome(
            "confirmed",
            attempted=True,
            command_sent=writes_started,
            readback_confirmed=True,
            reason="typed_register_readback" if writes_started else "cached_confirmed_register_state",
        )
        return True

    def write_hz_boost(self, mode, temp=None):
        return self.set_boost(mode, temp, 0, None)

    def write_ww_boost(self, mode, temp=45.0):
        return self.set_boost(0, None, mode, temp)

    def force_boost(self, hz_on, ww_on, khl_on, ww_max=None, hz_max=None, khl_min=None):
        """Manueller Boost: schreibt Register 1710/1711/1712 DIREKT ohne Hysterese-Check.
        Optionale Schutz-Limits verhindern eine Aktivierung bei bereits erfülltem Ziel.
        Externe Kühlung wird ohne gültige Kältespeicher-Mindestgrenze nie freigegeben."""
        if not _authorize_heatpump_output(self, "idm:force_boost_preflight"):
            return False
        from pymodbus.client import ModbusTcpClient
        client = ModbusTcpClient(self.ip, port=502)

        connected = False
        for _ in range(3):
            if client.connect():
                connected = True
                break
            time.sleep(1)

        if not connected:
            logger.error("IDM force_boost: Modbus Verbindung fehlgeschlagen")
            return False
        writes_started = False
        try:
            # Schutz-Check: Ist-Temperaturen direkt vor dem Modbus-Schreiben lesen.
            if ww_max is not None or hz_max is not None:
                ist_ww = self._read_float(client, 1030)  # Warmwasserzapftemperatur
                ist_hz = self._read_float(client, 1008)  # Wärmespeichertemperatur
                if ww_max is not None and ist_ww is not None and ist_ww >= ww_max:
                    logger.info(f"IDM force_boost: WW bereits bei {ist_ww}C >= Soll {ww_max}C - WW-Anforderung deaktiviert")
                    ww_on = False
                if hz_max is not None and ist_hz is not None and ist_hz >= hz_max:
                    logger.info(f"IDM force_boost: HZ bereits bei {ist_hz}C >= Soll {hz_max}C - HZ-Anforderung deaktiviert")
                    hz_on = False

            if khl_on:
                try:
                    target_khl = float(khl_min) if khl_min is not None else None
                except (TypeError, ValueError):
                    target_khl = None
                if target_khl is None or not math.isfinite(target_khl):
                    logger.warning("IDM force_boost: KHL-Anforderung ohne gültige Kältespeicher-Mindestgrenze abgelehnt")
                    khl_on = False
                else:
                    ist_khl = self._read_float(client, 1010)  # Kältespeichertemperatur
                    if ist_khl is None:
                        logger.warning("IDM force_boost: Kältespeicher-Istwert fehlt - KHL-Anforderung sicherheitshalber abgelehnt")
                        khl_on = False
                    elif ist_khl <= target_khl:
                        logger.info(
                            f"IDM force_boost: Kältespeicher bereits bei {ist_khl}C <= Mindestgrenze "
                            f"{target_khl}C - KHL-Anforderung deaktiviert"
                        )
                        khl_on = False
                    elif ist_khl <= target_khl + self.COOLING_START_HYSTERESIS_C:
                        logger.info(
                            f"IDM force_boost: Kältespeicher bei {ist_khl}C unter Startschwelle "
                            f"{target_khl + self.COOLING_START_HYSTERESIS_C}C - KHL-Anforderung deaktiviert"
                        )
                        khl_on = False

            writes_started = True
            writes_ok = (
                self._write_register_confirmed(client, 1710, 1 if hz_on else 0, "idm:force_heat")
                and self._write_register_confirmed(client, 1712, 1 if ww_on else 0, "idm:force_ww")
                and self._write_register_confirmed(client, 1711, 1 if khl_on else 0, "idm:force_cooling")
            )
            if not writes_ok:
                raise RuntimeError("IDM Force-Boost-Write/Readback nicht vollständig bestätigt")
            self.curr_ext_hz  = hz_on
            self.curr_ext_ww  = ww_on
            self.curr_ext_khl = khl_on
            logger.info(f"IDM force_boost: HZ={hz_on} WW={ww_on} KHL={khl_on}")
            return True
        except Exception as e:
            if writes_started:
                self._rollback_safe_state_confirmed(client, "force_boost_partial_failure")
            logger.error(f"IDM force_boost Fehler: {e}")
            return False
        finally:
            client.close()

    def update_surplus(self, grid_w, free_for_limbs_w=None, force_kw=None):
        """Meldet begrenzten PV-Ueberschuss ueber Register 74 (FLOAT, kW).

        Es werden keine Temperatur-Sollwerte geschrieben. Register 74 ist eine
        fluechtige PV-Vorgabe und eignet sich fuer eine ruhige iDM-Grundlast,
        z.B. auf 2 kW begrenzt.
        """
        if not _authorize_heatpump_output(self, "idm:update_surplus_preflight"):
            return False
        from pymodbus.client import ModbusTcpClient

        if not self.surplus_enabled:
            target_kw = 0.0
        elif force_kw is not None:
            target_kw = float(force_kw)
        elif free_for_limbs_w is not None:
            target_kw = max(0.0, float(free_for_limbs_w) / 1000.0)
        else:
            target_kw = max(0.0, -float(grid_w or 0.0) / 1000.0)

        if target_kw < self.surplus_min_kw:
            target_kw = 0.0
        target_kw = min(target_kw, self.surplus_max_kw)
        send_kw = round(max(0.0, self._ramp_surplus_kw(target_kw)), 2)

        now_ts = time.time()
        last_kw = float(getattr(self, 'last_sent_surplus_kw', 0.0) or 0.0)
        cap_reduction = send_kw < (last_kw - 0.001)
        min_interval_due = (now_ts - getattr(self, 'last_surplus_write_ts', 0.0)) >= self.surplus_min_write_interval_s
        heartbeat_due = (now_ts - getattr(self, 'last_surplus_write_ts', 0.0)) >= self.surplus_heartbeat_s
        if not cap_reduction and not min_interval_due and not heartbeat_due:
            return True
        # 0.00 kW ist nur ein Freigabe-Aus-Zustand. Wenn er bereits anliegt,
        # braucht Register 74 keinen Minutentakt und das Log bleibt ruhig.
        if send_kw <= 0.001 and last_kw <= 0.001 and getattr(self, 'last_surplus_write_ts', 0.0) > 0:
            return True
        if (
            not cap_reduction
            and abs(send_kw - last_kw) < self.surplus_deadband_kw
            and not heartbeat_due
        ):
            return True

        client = ModbusTcpClient(self.ip, port=502)
        connected = False
        for _ in range(3):
            if client.connect():
                connected = True
                break
            time.sleep(1)
        if not connected:
            logger.error(f"IDM update_surplus: Modbus-Verbindung zu {self.ip} fehlgeschlagen")
            return False
        try:
            if not self._write_float_confirmed(client, 74, send_kw, "idm:write_surplus"):
                raise RuntimeError("IDM Überschuss-Write/Readback nicht bestätigt")
            self.last_sent_surplus_kw = send_kw
            self.last_surplus_write_ts = now_ts
            source_bucket = "force" if force_kw is not None else ("off" if not self.surplus_enabled else "auto")
            kw_bucket = 0.0 if send_kw <= 0.001 else round(send_kw / 0.5) * 0.5
            info_bucket = (source_bucket, kw_bucket)
            if self.last_surplus_info_bucket != info_bucket:
                logger.info(f"IDM PV-Ueberschuss (Reg 74) -> {send_kw:.2f} kW (Ziel {target_kw:.2f} kW, Limit {self.surplus_max_kw:.2f} kW)")
                self.last_surplus_info_bucket = info_bucket
            else:
                logger.debug(f"IDM PV-Ueberschuss gehalten -> {send_kw:.2f} kW (Ziel {target_kw:.2f} kW)")
            return True
        except Exception as e:
            logger.error(f"IDM update_surplus Fehler: {e}")
            return False
        finally:
            client.close()

class DimplexHeatpump:
    """Modbus wrapper for Dimplex WPM Touch / NWPM SG-Ready control."""

    SG_NORMAL = 0
    SG_GREEN = 11
    SG_RED = 12
    SG_DARK_GREEN = 13

    def __init__(self, ip, port=502, unit_id=1, sg_register=5167, zero_based=False, heartbeat_s=300, allow_dark_green=False, safety_gate=None):
        self.ip = str(ip or "").strip()
        self.port = int(float(port or 502))
        self.unit_id = int(float(unit_id or 1))
        self.sg_register = int(float(sg_register or 5167))
        self.zero_based = str(zero_based or "0").strip().lower() in ("1", "true", "yes", "on", "ja", "ein")
        self.heartbeat_s = max(60.0, float(heartbeat_s or 300))
        self.allow_dark_green = str(allow_dark_green or "0").strip().lower() in ("1", "true", "yes", "on", "ja", "ein")
        self._connected = bool(self.ip and self.ip != "0.0.0.0")
        self.curr_sg_state = None
        self.last_sent_sg_state = None
        self.last_sg_write_ts = 0.0
        self.last_sg_readback_state = None
        self.last_sg_readback_ts = 0.0
        self._actuator_gate = safety_gate or _new_energy_actuator_gate()
        self._actuator_driver_key = f"transport:modbus-tcp:{self.ip}:{self.port}"
        self.actor_writes_blocked = False
        self.actor_write_block_reason = ""

    @property
    def sg_address(self):
        return max(0, self.sg_register if self.zero_based else self.sg_register - 1)

    def connect(self):
        self._connected = bool(self.ip and self.ip != "0.0.0.0")
        return self._connected

    def close(self):
        self._connected = False

    def keep_alive(self, force_open=True):
        return None

    def update_surplus(self, grid_w, **kwargs):
        pass

    def _call_modbus(self, client, method_name, **kwargs):
        method = getattr(client, method_name)
        for unit_kw in ("slave", "unit", None):
            call_kwargs = dict(kwargs)
            if unit_kw:
                call_kwargs[unit_kw] = self.unit_id
            try:
                return method(**call_kwargs)
            except TypeError:
                if unit_kw is None:
                    raise
                continue

    def _target_sg_state(self, hz_mode, hz_temp, ww_mode, ww_temp, kuehl_mode=0):
        is_pause = hz_mode == 1 and hz_temp is not None and _safe_float(hz_temp, 99.0) <= 20.0
        if is_pause:
            return self.SG_RED
        if ww_mode == 1 and self.allow_dark_green:
            return self.SG_DARK_GREEN
        if hz_mode == 1 or ww_mode == 1 or kuehl_mode == 1:
            return self.SG_GREEN
        return self.SG_NORMAL

    def _write_sg_state(self, target_state, reason=""):
        if not _authorize_heatpump_output(self, "dimplex:write_sg_preflight"):
            return False
        if not self.connect():
            logger.error("Dimplex SG: IP nicht konfiguriert")
            return False

        now_ts = time.time()
        if (
            self.last_sent_sg_state == target_state
            and (now_ts - self.last_sg_write_ts) < self.heartbeat_s
        ):
            return True

        from pymodbus.client import ModbusTcpClient
        client = ModbusTcpClient(self.ip, port=self.port)
        connected = False
        for _ in range(3):
            if client.connect():
                connected = True
                break
            time.sleep(1)
        if not connected:
            logger.error(f"Dimplex SG: Modbus-Verbindung zu {self.ip}:{self.port} fehlgeschlagen")
            return False
        try:
            # Verbindungswiederholungen können mehrere Sekunden dauern. Deshalb
            # direkt vor dem physischen Schreibvorgang erneut prüfen.
            if not _authorize_heatpump_output(self, "dimplex:write_sg_state"):
                return False
            result = self._call_modbus(
                client,
                "write_register",
                address=self.sg_address,
                value=int(target_state),
            )
            if result is None or (hasattr(result, "isError") and result.isError()):
                logger.error(f"Dimplex SG: Register {self.sg_register} write error: {result}")
                return False
            if not _authorize_heatpump_output(self, "dimplex:confirm_sg_write"):
                return False
            readback = self._call_modbus(
                client,
                "read_holding_registers",
                address=self.sg_address,
                count=1,
            )
            if (
                readback is None
                or (hasattr(readback, "isError") and readback.isError())
                or not getattr(readback, "registers", None)
                or int(readback.registers[0]) != int(target_state)
            ):
                logger.error(
                    "Dimplex SG: Register %s Readback unbestätigt (Soll=%s)",
                    self.sg_register,
                    target_state,
                )
                return False
            confirmed_state = int(readback.registers[0])
            self.curr_sg_state = confirmed_state
            self.last_sent_sg_state = confirmed_state
            self.last_sg_readback_state = confirmed_state
            self.last_sg_readback_ts = time.time()
            self.last_sg_write_ts = now_ts
            labels = {
                self.SG_NORMAL: "Gelb/Normalbetrieb",
                self.SG_GREEN: "Grün/Smart Grid Anhebung",
                self.SG_RED: "Rot/EVU-Sperre",
                self.SG_DARK_GREEN: "Dunkelgrün/maximale Anhebung",
            }
            logger.info(
                "Dimplex SG-Ready -> %s (%s, Register %s/PDU %s)",
                labels.get(int(target_state), str(target_state)),
                reason or "Energy Manager",
                self.sg_register,
                self.sg_address,
            )
            return True
        except Exception as exc:
            logger.error(f"Dimplex SG Fehler: {exc}")
            return False
        finally:
            try:
                client.close()
            except Exception:
                pass

    def set_boost(self, hz_mode, hz_temp, ww_mode, ww_temp, kuehl_mode=0, kuehl_soll=None, wp_data=None):
        target_state = self._target_sg_state(hz_mode, hz_temp, ww_mode, ww_temp, kuehl_mode)
        if target_state == self.SG_RED:
            reason = "Pause/EVU-Sperre"
        elif target_state == self.SG_DARK_GREEN:
            reason = "PV-/Preis-Boost mit maximaler Anhebung"
        elif target_state == self.SG_GREEN:
            reason = "PV-/Preis-Boost"
        else:
            reason = "Normalbetrieb"
        return self._write_sg_state(target_state, reason=reason)

    def write_hz_boost(self, mode, temp=None):
        return self.set_boost(mode, temp, 0, None)

    def write_ww_boost(self, mode, temp=45.0):
        return self.set_boost(0, None, mode, temp)

    def write_zirkulation(self, mode):
        return True

# Pfade
script_dir = os.path.dirname(os.path.abspath(__file__))
installer_dir = os.path.dirname(script_dir)
if installer_dir not in sys.path:
    sys.path.insert(0, installer_dir)
try:
    from market_economics import current_market_consumer_release
except Exception:
    def current_market_consumer_release(storage_plan, device, config=None, now_ms=None):
        return {"allowed": False, "reason": "market_plan_import_error"}

# NEU: Logging Konfiguration
LOG_DIR = "/var/www/html/logs"

# Bestehende Pfade
RAMDISK_FILE = "/var/www/html/ramdisk/luxtronik.json"
HISTORY_FILE = "/var/www/html/ramdisk/luxtronik_history.json"
BACKUP_DIR = "/var/www/html/data/luxtronik_archive"
LUXTRONIK_RAMDISK_HISTORY_MAX_LINES = 1440
LUXTRONIK_RAMDISK_HISTORY_MAX_BYTES = 8 * 1024 * 1024
LUXTRONIK_RAMDISK_HISTORY_TRIM_INTERVAL_S = 600
LUXTRONIK_ARCHIVE_INTERVAL_S = 5 * 60
LUXTRONIK_ARCHIVE_RETENTION_DAYS = 7
LEGACY_ENERGY_STATE_FILE = "/var/www/html/data/morning_boost_state.json"
ENERGY_STATE_FILE = "/var/www/html/data/energy_manager_state.json"
SHELLY_HEATPUMP_STATE_FILE = "/var/www/html/data/shelly_heatpump_state.json"
ENERGY_STATE_CHECKPOINT_ACTIVE_HEARTBEAT_S = 120.0
ENERGY_STATE_CHECKPOINT_IDLE_HEARTBEAT_S = 900.0
CAR_SESSION_CHECKPOINT_ACTIVE_HEARTBEAT_S = 120.0
CAR_SESSION_CHECKPOINT_IDLE_HEARTBEAT_S = 900.0
ENERGY_STATE_ACTIVE_RESTORE_MAX_AGE_S = 1200.0
HEATPUMP_LIVE_REVALIDATION_MAX_AGE_S = 120.0
STORAGE_PRIMARY_BUDGET_MAX_AGE_S = 30.0
STORAGE_PRIMARY_BUDGET_SCHEMAS = frozenset({"wb_pv_budget_control_v2"})
STORAGE_PRIMARY_BUDGET_IDENTITY_SCHEMA = "storage_consumer_budget_identity_v1"
STORAGE_PRIMARY_BUDGET_IDENTITY_KEYS = frozenset({
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
STORAGE_PRIMARY_BUDGET_BINDING_NOT_APPLICABLE = "not_applicable"
STORAGE_BUDGET_FALLBACK_MAX_AGE_S = 90.0
STORAGE_BUDGET_FALLBACK_SCHEMAS = frozenset({"storage_manager_state_v1"})
STORAGE_BUDGET_IDENTITY_KEYS = (
    "budget_revision",
    "budget_id",
    "plan_id",
    "slot_id",
)
HEATPUMP_SG_READY_START_RESERVATION_MAX_S = 150.0
HEATPUMP_DIRECT_START_RESERVATION_MAX_S = 25.0
# Kompatibilitätsname für bestehende Diagnose-/Testflächen. Neue
# Laufzeitentscheidungen verwenden die typisierte Dauer unten.
HEATPUMP_START_RESERVATION_MAX_S = HEATPUMP_SG_READY_START_RESERVATION_MAX_S
HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S = 600.0
FLAG_FILE = "/var/www/html/ramdisk/manual_boost.flag"
VEHICLES_JSON_FILE = "/var/www/html/ramdisk/vehicles.json"
WS_JSON_FILE = "/var/www/html/ramdisk/waermepumpe.json"
FORCE_FLAG_FILE = "/var/www/html/ramdisk/force_bluelink.flag"
V4_CONFIG_PATH = "/var/www/html/data/e3dc_v4.json"
LEGACY_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "e3dc.config.txt",
)
STORAGE_PLAN_PATH = "/var/www/html/ramdisk/storage_plan.json"
PRICE_BOOST_PLAN_PATH = "/var/www/html/ramdisk/price_boost_plan.json"
PRICE_BOOST_PLAN_MAX_AGE_S = 45 * 60
PRICE_BOOST_CONSUMER_CONFIG = {
    "battery": ("cheap_grid_battery_enable", True),
    "wallbox": ("cheap_grid_wallbox_enable", False),
    "heatpump": ("cheap_grid_heatpump_enable", False),
    "heater": ("cheap_grid_heater_enable", False),
}
PREDUMP_PLAN_PATH = "/var/www/html/ramdisk/predump_consumer_plan.json"
ENERGY_DECISION_LATEST_PATH = "/var/www/html/ramdisk/energy_decision_latest.json"
EMS_DECISION_LATEST_PATH = default_surface_path("/var/www/html/ramdisk")
HEAT_POLICY_LATEST_PATH = "/var/www/html/ramdisk/heat_policy_latest.json"
ENERGY_DECISION_HISTORY_PREFIX = "energy_decision_history_"

_energy_decision_history_state = {}

def read_manual_boost_command(path=FLAG_FILE):
    """Liest einen privaten atomaren Auftrag, ohne Links zu folgen oder Inode-Wechsel zu überholen."""
    absent = {"present": False, "valid": False, "action": "none", "reason": "absent"}
    try:
        lst = os.lstat(path)
    except FileNotFoundError:
        return absent
    except OSError as exc:
        return {**absent, "present": True, "reason": f"lstat_error:{type(exc).__name__}"}

    if not stat.S_ISREG(lst.st_mode) or lst.st_nlink != 1:
        return {**absent, "present": True, "reason": "not_regular_single_link"}
    if lst.st_mode & 0o007:
        return {**absent, "present": True, "reason": "world_accessible"}
    if lst.st_size <= 0 or lst.st_size > 4096:
        return {**absent, "present": True, "reason": "invalid_size"}

    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (
            opened.st_dev != lst.st_dev
            or opened.st_ino != lst.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            return {**absent, "present": True, "reason": "inode_changed"}
        chunks = []
        remaining = 4097
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > 4096:
            return {**absent, "present": True, "reason": "oversize"}
        text = raw.decode("utf-8", errors="strict").strip()
    except (OSError, UnicodeError) as exc:
        return {**absent, "present": True, "reason": f"read_error:{type(exc).__name__}"}
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    action = "on"
    schema = "legacy_manual_boost_flag"
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return {**absent, "present": True, "reason": "invalid_json"}
        if not isinstance(payload, dict) or payload.get("schema") != "manual_heatpump_command_v1":
            return {**absent, "present": True, "reason": "invalid_schema"}
        action = str(payload.get("action") or "").strip().lower()
        if action not in ("on", "off"):
            return {**absent, "present": True, "reason": "invalid_action"}
        schema = "manual_heatpump_command_v1"
    elif not text:
        return {**absent, "present": True, "reason": "empty_legacy_flag"}

    return {
        "present": True,
        "valid": True,
        "action": action,
        "schema": schema,
        "mtime": float(lst.st_mtime),
        "dev": int(lst.st_dev),
        "ino": int(lst.st_ino),
        "reason": "ok",
    }


def manual_boost_command_is_current(command, path=FLAG_FILE):
    """Prüft, ob ein bereits gelesener Auftrag weiterhin den aktuellen Inode bezeichnet."""
    if not isinstance(command, dict) or not command.get("valid"):
        return False
    try:
        current = os.lstat(path)
        return bool(
            stat.S_ISREG(current.st_mode)
            and current.st_nlink == 1
            and int(current.st_dev) == int(command.get("dev", -1))
            and int(current.st_ino) == int(command.get("ino", -1))
        )
    except OSError:
        return False


def _lock_manual_command_directory(path):
    """Sperrt das von Web-Submitter und Owner gemeinsam verwendete Befehlsverzeichnis."""
    import fcntl

    parent = os.path.dirname(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(parent, flags)
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(fd)
        raise OSError("manual command parent is not a directory")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _unlock_manual_command_directory(fd):
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def consume_manual_boost_command(command, path=FLAG_FILE):
    """Übernimmt und verbraucht atomar nur den exakt erfolgreich angewendeten Auftrag."""
    lock_fd = None
    claim_path = ""
    try:
        lock_fd = _lock_manual_command_directory(path)
        if not manual_boost_command_is_current(command, path):
            return False
        claim_path = f"{path}.claimed.{os.getpid()}.{time.time_ns()}"
        os.replace(path, claim_path)
        claimed = os.lstat(claim_path)
        if (
            int(claimed.st_dev) != int(command.get("dev", -1))
            or int(claimed.st_ino) != int(command.get("ino", -1))
            or not stat.S_ISREG(claimed.st_mode)
            or claimed.st_nlink != 1
        ):
            if not os.path.lexists(path):
                os.replace(claim_path, path)
                claim_path = ""
            return False
        os.unlink(claim_path)
        claim_path = ""
        os.fsync(lock_fd)
        return True
    except OSError:
        return False
    finally:
        if lock_fd is not None:
            try:
                _unlock_manual_command_directory(lock_fd)
            except OSError:
                pass

def _safe_float(value, default=0.0):
    try:
        if value is None:
            return float(default)
        result = float(str(value).replace(",", "."))
        return result if math.isfinite(result) else float(default)
    except Exception:
        return float(default)

def _safe_int(value, default=0):
    return int(round(_safe_float(value, default)))


def _single_vehicle_fallback(vehicles):
    """Verhindert eine willkürliche vehicles[0]-Bindung bei mehreren Autos."""

    if not isinstance(vehicles, list) or len(vehicles) != 1:
        return None
    return vehicles[0] if isinstance(vehicles[0], dict) else None


def _strict_storage_budget_ts(value):
    """Liest einen Producer-Zeitstempel ohne Typ-Imputation."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        return None
    return parsed


def _storage_budget_revision(value):
    return bool(
        isinstance(value, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value)
    )


def _storage_primary_authorized_consumer_budgets(primary_payload):
    """Liest die drei versiegelten Verbraucheranteile ohne Typ-Imputation."""

    data = primary_payload if isinstance(primary_payload, dict) else {}
    validation = validate_consumer_budget_contract(
        data.get("consumer_budget_contract")
    )
    command_validation = validate_consumer_command_allocations(
        data.get("consumer_budget_contract")
    )
    if (
        validation.get("valid") is not True
        or command_validation.get("valid") is not True
    ):
        return None
    energy_score = (
        data.get("energy_score")
        if isinstance(data.get("energy_score"), dict)
        else {}
    )
    total_w = data.get("consumer_total_budget_w")
    if (
        type(total_w) is not int
        or total_w != validation.get("total_budget_w")
    ):
        return None
    accounting = dict(validation.get("allocations") or {})
    authorized = dict(command_validation.get("allocations") or {})
    for container in (data, energy_score):
        allocations = container.get("consumer_allocations")
        if not isinstance(allocations, dict):
            return None
        if allocations != authorized:
            return None
        if container.get("consumer_accounting_allocations") != accounting:
            return None
    wallbox_w = data.get("budget_w")
    if type(wallbox_w) is not int or wallbox_w != authorized.get("wallbox"):
        return None
    return authorized


def _storage_primary_budget_provenance_contract(primary_payload):
    """Validiert die vollständige Control-v2-Producer- und Gesamtgeneration."""

    data = primary_payload if isinstance(primary_payload, dict) else {}
    result = {
        "schema_version": "storage_primary_budget_provenance_validation_v1",
        "valid": False,
        "reason_code": "PRODUCER_IDENTITY_MISSING",
    }
    identity = data.get("producer_identity")
    if not isinstance(identity, dict):
        return result
    if frozenset(identity) != STORAGE_PRIMARY_BUDGET_IDENTITY_KEYS:
        return {**result, "reason_code": "PRODUCER_IDENTITY_KEYS_INVALID"}
    if identity.get("schema_version") != STORAGE_PRIMARY_BUDGET_IDENTITY_SCHEMA:
        return {**result, "reason_code": "PRODUCER_IDENTITY_SCHEMA_INVALID"}

    decision_ts = identity.get("decision_ts")
    source_ts = data.get("ts")
    if not bool(
        type(decision_ts) is int
        and decision_ts > 0
        and type(source_ts) is int
        and source_ts == decision_ts
    ):
        return {**result, "reason_code": "PRODUCER_TIMESTAMP_INVALID"}
    if identity.get("decision_owner") != "storage_manager":
        return {**result, "reason_code": "PRODUCER_OWNER_INVALID"}
    if identity.get("decision_effect") != "flexible_consumer_power_budget":
        return {**result, "reason_code": "PRODUCER_EFFECT_INVALID"}
    if not isinstance(identity.get("decision_state"), str) or not str(
        identity.get("decision_state")
    ).strip():
        return {**result, "reason_code": "PRODUCER_STATE_INVALID"}

    authorized_budgets = _storage_primary_authorized_consumer_budgets(data)
    authorized_wallbox_budget_w = identity.get(
        "authorized_wallbox_budget_w"
    )
    authorized_heatpump_budget_w = identity.get(
        "authorized_heatpump_budget_w"
    )
    authorized_heater_budget_w = identity.get(
        "authorized_heater_budget_w"
    )
    if not bool(
        type(identity.get("decision_mode")) is int
        and type(identity.get("decision_value_w")) is int
        and identity.get("decision_value_w") >= 0
        and isinstance(identity.get("decision_protected"), bool)
        and isinstance(authorized_budgets, dict)
        and type(authorized_wallbox_budget_w) is int
        and authorized_wallbox_budget_w
        == authorized_budgets.get("wallbox")
        and type(authorized_heatpump_budget_w) is int
        and authorized_heatpump_budget_w
        == authorized_budgets.get("heatpump")
        and type(authorized_heater_budget_w) is int
        and authorized_heater_budget_w
        == authorized_budgets.get("heater")
    ):
        return {**result, "reason_code": "PRODUCER_WATT_CONTRACT_INVALID"}

    binding_status = identity.get("binding_status")
    binding_values = tuple(
        identity.get(key) for key in ("plan_id", "slot_id", "action_id")
    )
    if binding_status == "bound":
        binding_valid = all(
            _storage_budget_revision(value) for value in binding_values
        )
    elif binding_status == STORAGE_PRIMARY_BUDGET_BINDING_NOT_APPLICABLE:
        binding_valid = all(
            value == STORAGE_PRIMARY_BUDGET_BINDING_NOT_APPLICABLE
            for value in binding_values
        )
    else:
        binding_valid = False
    if not binding_valid:
        return {**result, "reason_code": "PRODUCER_PLAN_BINDING_INVALID"}

    generation = identity.get("decision_generation")
    generation_material = {
        key: value
        for key, value in identity.items()
        if key != "decision_generation"
    }
    if not _storage_budget_revision(generation):
        return {**result, "reason_code": "PRODUCER_GENERATION_INVALID"}
    try:
        expected_generation = storage_contract_revision_hash(
            generation_material
        )
    except (TypeError, ValueError):
        return {**result, "reason_code": "PRODUCER_GENERATION_MATERIAL_INVALID"}
    if generation != expected_generation:
        return {**result, "reason_code": "PRODUCER_GENERATION_MISMATCH"}

    budget_revision = data.get("budget_revision")
    if not _storage_budget_revision(budget_revision):
        return {**result, "reason_code": "BUDGET_REVISION_INVALID"}
    revision_material = {
        key: value
        for key, value in data.items()
        if key != "budget_revision"
    }
    try:
        expected_budget_revision = storage_contract_revision_hash(
            revision_material
        )
    except (TypeError, ValueError):
        return {**result, "reason_code": "BUDGET_REVISION_MATERIAL_INVALID"}
    if budget_revision != expected_budget_revision:
        return {
            **result,
            "reason_code": "BUDGET_REVISION_MISMATCH",
            "observed_budget_revision": budget_revision,
            "expected_budget_revision": expected_budget_revision,
            "budget_timestamp_s": data.get("ts"),
        }

    return {
        **result,
        "valid": True,
        "reason_code": "PRIMARY_PRODUCER_AND_BUDGET_REVISION_BOUND",
        "producer_identity": copy.deepcopy(identity),
        "budget_revision": budget_revision,
        "decision_generation": generation,
        "decision_owner": identity.get("decision_owner"),
        "decision_effect": identity.get("decision_effect"),
        "decision_ts": decision_ts,
        "authorized_wallbox_budget_w": authorized_wallbox_budget_w,
        "authorized_heatpump_budget_w": authorized_heatpump_budget_w,
        "authorized_heater_budget_w": authorized_heater_budget_w,
        "binding_status": binding_status,
    }


def _storage_budget_identity(payload):
    """Bindet nur reale, hashbasierte IDs; fehlende IDs bleiben Evidenzlücke."""

    source = payload if isinstance(payload, dict) else {}
    scopes = [source]
    for scope_key in ("storage_dispatch_runtime", "budget_contract"):
        nested = source.get(scope_key)
        if isinstance(nested, dict):
            scopes.append(nested)

    identity = {}
    invalid = []
    for key in STORAGE_BUDGET_IDENTITY_KEYS:
        values = []
        for scope in scopes:
            if key not in scope:
                continue
            value = scope.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                invalid.append(key)
                continue
            values.append(value)
        if len(set(values)) > 1:
            invalid.append(key)
        elif values:
            identity[key] = values[0]
    return identity, sorted(set(invalid))


def storage_primary_budget_contract(
    primary_payload,
    *,
    now_ts=None,
    file_age_s=None,
    max_age_s=STORAGE_PRIMARY_BUDGET_MAX_AGE_S,
):
    """Bindet das primäre Verbraucherbudget an Schema und Producer-Zeit.

    Die Dateizeit darf als zusätzliche Transportprüfung sperren, ist aber nie
    Ersatz für den im Budget versiegelten Producer-Zeitstempel. Ein bloßes
    erneutes Anfassen einer alten Ramdisk-Datei kann deshalb keine Wärme- oder
    Verbraucherfreigabe erzeugen.
    """

    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    max_age_value = max(0.0, _safe_float(max_age_s, STORAGE_PRIMARY_BUDGET_MAX_AGE_S))
    base = {
        "schema_version": "storage_primary_budget_contract_v1",
        "accepted": False,
        "source": "wb_pv_budget",
        "evidence_status": "INVALID",
        "reason_code": "PRIMARY_PAYLOAD_INVALID",
        "timestamp_s": None,
        "age_s": None,
        "file_age_s": None,
        "max_age_s": max_age_value,
        "revision_status": "EVIDENCE_LIMIT",
        "identity": {},
        "producer_provenance": {},
    }
    if not isinstance(primary_payload, dict):
        return base
    schema = primary_payload.get("schema_version")
    if not isinstance(schema, str) or schema not in STORAGE_PRIMARY_BUDGET_SCHEMAS:
        return {**base, "reason_code": "PRIMARY_SCHEMA_INVALID"}
    source_ts = _strict_storage_budget_ts(primary_payload.get("ts"))
    if source_ts is None:
        return {**base, "reason_code": "PRIMARY_TIMESTAMP_INVALID"}
    age_s = now_value - source_ts
    timed = {**base, "timestamp_s": source_ts, "age_s": age_s}
    if age_s < 0.0:
        return {**timed, "reason_code": "PRIMARY_TIMESTAMP_FUTURE"}
    if age_s > max_age_value:
        return {**timed, "reason_code": "PRIMARY_STALE"}
    if file_age_s is not None:
        if (
            isinstance(file_age_s, bool)
            or not isinstance(file_age_s, (int, float))
            or not math.isfinite(float(file_age_s))
            or float(file_age_s) < 0.0
        ):
            return {**timed, "reason_code": "PRIMARY_FILE_AGE_INVALID"}
        timed["file_age_s"] = float(file_age_s)
        if float(file_age_s) > max_age_value:
            return {**timed, "reason_code": "PRIMARY_FILE_STALE"}
    provenance = _storage_primary_budget_provenance_contract(primary_payload)
    if provenance.get("valid") is not True:
        return {
            **timed,
            "reason_code": str(
                provenance.get("reason_code")
                or "PRIMARY_PROVENANCE_INVALID"
            ),
            "producer_provenance": provenance,
        }
    identity = {
        "budget_revision": provenance.get("budget_revision"),
        "decision_generation": provenance.get("decision_generation"),
        "decision_owner": provenance.get("decision_owner"),
        "decision_effect": provenance.get("decision_effect"),
        "decision_ts": provenance.get("decision_ts"),
        "authorized_wallbox_budget_w": provenance.get(
            "authorized_wallbox_budget_w"
        ),
        "authorized_heatpump_budget_w": provenance.get(
            "authorized_heatpump_budget_w"
        ),
        "authorized_heater_budget_w": provenance.get(
            "authorized_heater_budget_w"
        ),
        "binding_status": provenance.get("binding_status"),
    }
    consumer_contract = (
        primary_payload.get("consumer_budget_contract")
        if isinstance(primary_payload.get("consumer_budget_contract"), dict)
        else {}
    )
    heatpump_boost_permission_active = bool(
        primary_payload.get("heatpump_boost_permission_active") is True
        and consumer_contract.get("heatpump_boost_permission_active") is True
    )
    return {
        **timed,
        "accepted": True,
        "evidence_status": "VALID",
        "reason_code": "PRIMARY_FRESH_REVISION_BOUND",
        "revision_status": "BOUND",
        "identity": dict(identity),
        "producer_provenance": provenance,
        "heatpump_boost_permission_active": (
            heatpump_boost_permission_active
        ),
    }


def select_storage_primary_budget(
    primary_payload,
    *,
    now_ts=None,
    file_age_s=None,
    max_age_s=STORAGE_PRIMARY_BUDGET_MAX_AGE_S,
):
    """Projiziert nur ein frisch producer-gebundenes Primärbudget."""

    contract = storage_primary_budget_contract(
        primary_payload,
        now_ts=now_ts,
        file_age_s=file_age_s,
        max_age_s=max_age_s,
    )
    if (
        not contract.get("accepted")
        or contract.get("evidence_status") != "VALID"
    ):
        return contract, None
    return contract, _storage_budget_consumer_projection(
        primary_payload,
        state_key="storage_state",
    )


def storage_budget_fallback_contract(
    fallback_payload,
    *,
    primary_payload=None,
    previous_guard=None,
    now_ts=None,
    max_age_s=STORAGE_BUDGET_FALLBACK_MAX_AGE_S,
):
    """Validiert den Storage-State ausschließlich als Diagnosequelle.

    Die heutige v1-Fläche enthält weder den vollständigen zentralen
    Verbraucher-Vertrag noch eine Producer- und Hashbindung aller projizierbaren
    Wattfelder. Deshalb bleibt auch eine beobachtete gemeinsame ID
    ``EVIDENCE_LIMIT`` und darf keine Verbraucherfreigabe erzeugen. Eine spätere
    v2-Fläche braucht dafür einen eigenen vollständig validierten Vertrag.
    """

    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    max_age_value = max(0.0, _safe_float(max_age_s, STORAGE_BUDGET_FALLBACK_MAX_AGE_S))
    prior = previous_guard if isinstance(previous_guard, dict) else {}
    base = {
        "schema_version": "storage_budget_fallback_contract_v1",
        "accepted": False,
        "source": "storage_manager_state",
        "evidence_status": "INVALID",
        "reason_code": "FALLBACK_PAYLOAD_INVALID",
        "timestamp_s": None,
        "age_s": None,
        "max_age_s": max_age_value,
        "revision_status": "EVIDENCE_LIMIT",
        "common_identity": {},
        "replay_guard": dict(prior),
    }
    if not isinstance(fallback_payload, dict):
        return base
    if fallback_payload.get("service") != "storage_manager":
        return {**base, "reason_code": "FALLBACK_SOURCE_INVALID"}

    schema = fallback_payload.get("schema_version")
    if schema is not None and (
        not isinstance(schema, str)
        or schema not in STORAGE_BUDGET_FALLBACK_SCHEMAS
    ):
        return {**base, "reason_code": "FALLBACK_SCHEMA_INVALID"}

    source_ts = _strict_storage_budget_ts(fallback_payload.get("ts"))
    if source_ts is None:
        return {**base, "reason_code": "FALLBACK_TIMESTAMP_INVALID"}
    age_s = now_value - source_ts
    timed = {**base, "timestamp_s": source_ts, "age_s": age_s}
    if age_s < 0.0:
        return {**timed, "reason_code": "FALLBACK_TIMESTAMP_FUTURE"}
    if age_s > max_age_value:
        return {**timed, "reason_code": "FALLBACK_STALE"}

    fallback_identity, fallback_identity_invalid = _storage_budget_identity(fallback_payload)
    if fallback_identity_invalid:
        return {
            **timed,
            "reason_code": "FALLBACK_IDENTITY_INVALID",
            "invalid_identity_keys": fallback_identity_invalid,
        }

    primary = primary_payload if isinstance(primary_payload, dict) else {}
    primary_identity, _primary_identity_invalid = _storage_budget_identity(primary)
    common_keys = sorted(set(fallback_identity) & set(primary_identity))
    common_identity = {key: fallback_identity[key] for key in common_keys}
    mismatches = [
        key
        for key in common_keys
        if fallback_identity[key] != primary_identity[key]
    ]
    if mismatches:
        return {
            **timed,
            "reason_code": "FALLBACK_REVISION_MISMATCH",
            "revision_status": "MISMATCH",
            "common_identity": common_identity,
            "mismatched_identity_keys": mismatches,
        }

    primary_ts = None
    if primary.get("schema_version") == "wb_pv_budget_control_v2":
        primary_ts = _strict_storage_budget_ts(primary.get("ts"))
    if primary_ts is not None and source_ts < primary_ts:
        return {
            **timed,
            "reason_code": "FALLBACK_REPLAY_BEHIND_PRIMARY",
            "common_identity": common_identity,
        }

    previous_ts = _strict_storage_budget_ts(prior.get("timestamp_s"))
    if previous_ts is not None and source_ts < previous_ts:
        return {
            **timed,
            "reason_code": "FALLBACK_REPLAY_BEHIND_ACCEPTED",
            "common_identity": common_identity,
        }
    previous_identity = prior.get("identity") if isinstance(prior.get("identity"), dict) else {}
    same_ts_identity_mismatch = sorted(
        key
        for key in set(fallback_identity) & set(previous_identity)
        if source_ts == previous_ts and fallback_identity[key] != previous_identity[key]
    )
    if same_ts_identity_mismatch:
        return {
            **timed,
            "reason_code": "FALLBACK_REPLAY_IDENTITY_CHANGED",
            "revision_status": "MISMATCH",
            "common_identity": common_identity,
            "mismatched_identity_keys": same_ts_identity_mismatch,
        }

    return {
        **timed,
        "accepted": True,
        "evidence_status": "EVIDENCE_LIMIT",
        "reason_code": (
            "FALLBACK_FRESH_IDENTITY_DIAGNOSTIC_ONLY"
            if common_keys
            else "FALLBACK_FRESH_REVISION_UNAVAILABLE"
        ),
        "revision_status": "EVIDENCE_LIMIT",
        "common_identity": common_identity,
        "replay_guard": {
            "timestamp_s": source_ts,
            "identity": dict(fallback_identity),
        },
    }


def _storage_budget_consumer_projection(payload, *, state_key="state"):
    """Projiziert ein bereits validiertes Budget atomar auf sichere Leserwerte."""

    source = payload if isinstance(payload, dict) else {}
    energy_score = source.get("energy_score") if isinstance(source.get("energy_score"), dict) else {}
    allocations = source.get("consumer_allocations") or energy_score.get("consumer_allocations") or {}
    allocations = dict(allocations) if isinstance(allocations, dict) else {}
    accounting_allocations = (
        source.get("consumer_accounting_allocations")
        or energy_score.get("consumer_accounting_allocations")
        or {}
    )
    accounting_allocations = (
        dict(accounting_allocations)
        if isinstance(accounting_allocations, dict)
        else {}
    )
    consumer_total_budget_w = max(
        0,
        _safe_int(
            source.get("consumer_total_budget_w"),
            _safe_int(energy_score.get("free_for_limbs_w"), 0),
        ),
    )
    heatpump_budget_w = None
    if "heatpump" in allocations:
        heatpump_budget_w = max(0, _safe_int(allocations.get("heatpump"), 0))
    # ``free_for_limbs_w`` ist ein ungebundener Legacy-Resttopf und darf eine
    # bereits je Verbraucher versiegelte Zuteilung nicht erneut freigeben.
    # Wärmeentscheidungen verwenden ausschließlich ``heatpump_budget_w``.
    free_w = 0
    heatpump_accounting_budget_w = None
    if "heatpump" in accounting_allocations:
        heatpump_accounting_budget_w = max(
            0,
            _safe_int(accounting_allocations.get("heatpump"), 0),
        )
    pause_request = source.get("heatpump_pause_request")
    consumer_contract = (
        source.get("consumer_budget_contract")
        if isinstance(source.get("consumer_budget_contract"), dict)
        else {}
    )
    heatpump_boost_permission_active = bool(
        source.get("heatpump_boost_permission_active") is True
        and consumer_contract.get("heatpump_boost_permission_active") is True
    )
    return {
        "free_for_limbs_w": free_w,
        "consumer_total_budget_w": consumer_total_budget_w,
        "must_consume_w": _safe_int(energy_score.get("must_consume_w"), 0),
        "consumer_allocations": allocations,
        "heatpump_budget_w": heatpump_budget_w,
        "heatpump_accounting_budget_w": heatpump_accounting_budget_w,
        "heatpump_boost_permission_active": (
            heatpump_boost_permission_active
        ),
        "wallbox_phase_transition_active": wallbox_phase_transition_blocks_heatpump_start(source),
        "wallbox_phase_transition_reserved_w": max(
            0,
            _safe_int(source.get("wallbox_phase_transition_reserved_w"), 0),
            _safe_int(source.get("wallbox_phase_transition_requested_w_total"), 0),
        ),
        "wallbox_phase_transition_until_ts": _safe_float(
            source.get("wallbox_phase_transition_until_ts"),
            0.0,
        ),
        "heatpump_running_commitment_w": max(
            0,
            _safe_int(source.get("heatpump_running_commitment_w"), 0),
        ),
        "heatpump_pause_request": dict(pause_request) if isinstance(pause_request, dict) else {},
        "storage_state_name": str(source.get(state_key) or "unknown"),
    }


def select_storage_budget_fallback(
    fallback_payload,
    *,
    primary_payload=None,
    previous_guard=None,
    now_ts=None,
    max_age_s=STORAGE_BUDGET_FALLBACK_MAX_AGE_S,
):
    """Hält die heutige Storage-State-v1-Fläche strikt diagnose-only."""

    contract = storage_budget_fallback_contract(
        fallback_payload,
        primary_payload=primary_payload,
        previous_guard=previous_guard,
        now_ts=now_ts,
        max_age_s=max_age_s,
    )
    # Eine spätere Projektion braucht eine explizite v2-Implementierung samt
    # vollständigem Consumer-, Producer- und Hashvertrag. Das bloße Hochstufen
    # eines Diagnoseurteils auf VALID darf diesen Hardwarepfad nicht öffnen.
    return contract, None


def heatpump_keepalive_force_open(
    wp_type,
    boost_active=False,
    pv_pause_active=False,
    pre_pause_active=False,
    price_boost_active=False,
):
    """Hält die empfindliche Luxtronik-SHI-Sitzung auch in NORMAL offen."""
    if _safe_int(wp_type, -1) == 0:
        return True
    return bool(
        boost_active
        or pv_pause_active
        or pre_pause_active
        or price_boost_active
    )


def normalize_luxtronik_shi_mode(value):
    """Normalisiert den SHI-Auftragsmodus, nicht den physischen WP-Status."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        mode = int(value)
        return mode if mode in (0, 1, 2) else None

    text = str(value).strip().casefold()
    if not text:
        return None
    try:
        mode = int(float(text.replace(",", ".")))
        return mode if mode in (0, 1, 2) else None
    except (TypeError, ValueError):
        pass

    if text in {
        "aus",
        "auto",
        "normal",
        "---",
        "none",
        "keine beeinflussung",
        "standby",
        "frost",
        "ferien",
        "urlaub",
        "nacht",
        "absenkung",
    }:
        return 0
    if "setpoint" in text or "sollwert" in text:
        return 1
    if "offset" in text:
        return 2
    return None


def normalize_luxtronik_operating_mode(value):
    """Normalisiert die physische Betriebsart aus Modbus oder WebSocket."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        mode = int(value)
        return mode if mode in (0, 1, 2, 3, 4, 5, 6, 7) else None
    text = str(value).strip().casefold()
    if not text:
        return None
    try:
        mode = int(float(text.replace(",", ".")))
        return mode if mode in (0, 1, 2, 3, 4, 5, 6, 7) else None
    except (TypeError, ValueError):
        pass
    if "warmw" in text or "brauchw" in text:
        return 1
    if "schwimm" in text or "pool" in text:
        return 2
    if "evu" in text or "sperr" in text:
        return 3
    if "abtau" in text or "defrost" in text:
        return 4
    if "kühl" in text or "kuehl" in text:
        return 7
    if "heiz" in text:
        return 0
    if text in {"aus", "standby", "keine anforderung", "keine anf."}:
        return 5
    return None


def normalize_native_heatpump_live_payload(raw_payload, wp_type):
    """Bindet die gemeinsame Live-Datei an ihren typisierten Provider.

    Luxtronik und iDM liefern flache Nutzdaten. Dimplex und Stiebel schreiben
    dagegen einen Wrapper mit ``data`` und ``status``. Insbesondere der reale
    Stiebel-Provider heißt ``stiebel_live``; seine vorhandenen Heating-/DHW-
    Statusfelder werden nur für wp_type=4 als Modus-Evidenz übernommen.
    """

    raw = raw_payload if isinstance(raw_payload, dict) else {}
    source = str(raw.get("source") or "").strip().casefold()
    provider_status = (
        raw.get("status") if isinstance(raw.get("status"), dict) else {}
    )
    wrapped_sources = {
        "dimplex_live",
        "stiebel_isg_live",
        "stiebel_live",
    }
    wrapped = bool(source in wrapped_sources and isinstance(raw.get("data"), dict))
    data = dict(raw.get("data") or {}) if wrapped else dict(raw)
    provider_valid = True
    if source == "idm_live" or source in wrapped_sources:
        provider_valid = raw.get("success") is True

    hz_mode_raw = data.get("Modus Heizen")
    ww_mode_raw = data.get("Modus Warmw.")
    if _safe_int(wp_type, -1) == 4:
        provider_valid = bool(
            provider_valid
            and source == "stiebel_live"
            and wrapped
            and provider_status.get("valid") is True
        )
        if hz_mode_raw is None:
            hz_mode_raw = provider_status.get(
                "Heating",
                data.get("stiebel_heating_active"),
            )
        if ww_mode_raw is None:
            ww_mode_raw = provider_status.get(
                "DHW",
                data.get("stiebel_dhw_active"),
            )

    return {
        "data": data,
        "source": source,
        "provider_status": provider_status,
        "provider_valid": bool(provider_valid),
        "wrapped": wrapped,
        "hz_mode_raw": hz_mode_raw,
        "ww_mode_raw": ww_mode_raw,
    }


def heatpump_compressor_running(wp_data=None, wp_status=None):
    """Ermittelt den Verdichterlauf aus offiziellen und kompatiblen Signalen."""
    data = wp_data if isinstance(wp_data, dict) else {}
    status = wp_status if isinstance(wp_status, dict) else {}
    bitmask = data.get("Status_Waermepumpe_Bitmask", status.get("Status_Waermepumpe_Bitmask"))
    if bitmask is not None:
        return bool(_safe_int(bitmask, 0) & 0x03)
    for key in ("Verdichter_Ein", "Verdichter", "Verdichter 1"):
        if key in data or key in status:
            if _safe_int(data.get(key, status.get(key)), 0) > 0:
                return True
    # Rund 150 W können bei Luxtronik ausschließlich Pumpenvorlauf sein. Nur
    # belastbare Leistungsaufnahme oberhalb dieses Vorlaufs ist ein Fallback,
    # wenn kein explizites Verdichtersignal vorliegt.
    if _safe_float(data.get("Leistung_Verdichter_W", status.get("Leistung_Verdichter_W")), 0.0) >= 500.0:
        return True
    for key in ("Leistungsaufnahme", "Leistung_Heiz_kW", "Heizleistung Ist"):
        if _safe_float(data.get(key, status.get(key)), 0.0) >= 0.5:
            return True
    return False

def heatpump_budget_deficit(storage_manager_owns_energy, free_for_limbs_w, grid_start_limit):
    """Return true when a Storage-Manager-owned heat boost has lost its budget."""
    if not storage_manager_owns_energy:
        return False
    required_w = abs(_safe_int(grid_start_limit, -3500))
    if required_w <= 0:
        return False
    return _safe_int(free_for_limbs_w, 0) < required_w


def running_heatpump_budget_underfunded(
    accounting_valid,
    effective_budget_w,
    accounting_w,
    tolerance_w,
):
    """Vergleicht eine laufende WP mit dem wirksamen, nicht nur dem Startbudget."""

    return bool(
        accounting_valid
        and max(0, _safe_int(effective_budget_w, 0))
        + max(0, _safe_int(tolerance_w, 0))
        < max(0, _safe_int(accounting_w, 0))
    )


def heatpump_budget_allows_start(storage_manager_owns_energy, free_for_limbs_w, grid_start_limit, soc, min_soc):
    """Return true when the PV heat boost may start from the current budget."""
    required_w = abs(_safe_int(grid_start_limit, -3500))
    if required_w <= 0:
        return False
    if _safe_int(free_for_limbs_w, 0) < required_w:
        return False
    if storage_manager_owns_energy:
        return True
    return _safe_float(soc, 0.0) >= _safe_float(min_soc, 80.0)


def _legacy_heat_primary_budget_binding(storage_budget_contract):
    """Projiziert ausschließlich einen bereits vollständig validierten Primärvertrag."""

    budget = storage_budget_contract if isinstance(storage_budget_contract, dict) else {}
    provenance = (
        budget.get("producer_provenance")
        if isinstance(budget.get("producer_provenance"), dict)
        else {}
    )
    producer_identity = (
        provenance.get("producer_identity")
        if isinstance(provenance.get("producer_identity"), dict)
        else {}
    )
    normalized_identity = (
        budget.get("identity")
        if isinstance(budget.get("identity"), dict)
        else {}
    )
    source_contract_sha256 = None
    try:
        source_contract_sha256 = heat_intent.revision_hash(budget)
    except (TypeError, ValueError):
        pass
    valid = bool(
        budget.get("schema_version") == "storage_primary_budget_contract_v1"
        and budget.get("source") == "wb_pv_budget"
        and budget.get("accepted") is True
        and budget.get("evidence_status") == "VALID"
        and budget.get("revision_status") == "BOUND"
        and provenance.get("valid") is True
        and provenance.get("reason_code")
        == "PRIMARY_PRODUCER_AND_BUDGET_REVISION_BOUND"
        and producer_identity.get("decision_owner") == "storage_manager"
        and producer_identity.get("decision_effect")
        == "flexible_consumer_power_budget"
        and normalized_identity.get("budget_revision")
        == provenance.get("budget_revision")
        and normalized_identity.get("decision_generation")
        == provenance.get("decision_generation")
        and normalized_identity.get("decision_owner")
        == provenance.get("decision_owner")
        and normalized_identity.get("decision_effect")
        == provenance.get("decision_effect")
        and normalized_identity.get("decision_ts")
        == provenance.get("decision_ts")
        and normalized_identity.get("authorized_wallbox_budget_w")
        == provenance.get("authorized_wallbox_budget_w")
        and normalized_identity.get("authorized_heatpump_budget_w")
        == provenance.get("authorized_heatpump_budget_w")
        and normalized_identity.get("authorized_heater_budget_w")
        == provenance.get("authorized_heater_budget_w")
        and normalized_identity.get("binding_status")
        == provenance.get("binding_status")
        and _storage_budget_revision(provenance.get("budget_revision"))
        and _storage_budget_revision(provenance.get("decision_generation"))
        and _storage_budget_revision(source_contract_sha256)
    )
    return {
        "schema_version": "legacy_heat_primary_budget_binding_v1",
        "valid": valid,
        "source_contract_schema": budget.get("schema_version"),
        "source": budget.get("source"),
        "evidence_status": budget.get("evidence_status"),
        "revision_status": budget.get("revision_status"),
        "source_contract_sha256": source_contract_sha256,
        "budget_revision": provenance.get("budget_revision"),
        "producer_identity": copy.deepcopy(producer_identity),
        "decision_generation": provenance.get("decision_generation"),
        "decision_owner": provenance.get("decision_owner"),
        "decision_effect": provenance.get("decision_effect"),
        "decision_ts": provenance.get("decision_ts"),
        "authorized_wallbox_budget_w": provenance.get(
            "authorized_wallbox_budget_w"
        ),
        "authorized_heatpump_budget_w": provenance.get(
            "authorized_heatpump_budget_w"
        ),
        "authorized_heater_budget_w": provenance.get(
            "authorized_heater_budget_w"
        ),
        "binding_status": provenance.get("binding_status"),
    }


def _legacy_heat_primary_budget_binding_valid(binding):
    """Prüft Owner, Effekt, Watt und Generation des projizierten Primärbudgets."""

    if not isinstance(binding, dict):
        return False
    identity = binding.get("producer_identity")
    if not isinstance(identity, dict):
        return False
    if frozenset(identity) != STORAGE_PRIMARY_BUDGET_IDENTITY_KEYS:
        return False
    if identity.get("schema_version") != STORAGE_PRIMARY_BUDGET_IDENTITY_SCHEMA:
        return False
    generation = identity.get("decision_generation")
    generation_material = {
        key: value
        for key, value in identity.items()
        if key != "decision_generation"
    }
    try:
        generation_valid = bool(
            _storage_budget_revision(generation)
            and generation == storage_contract_revision_hash(
                generation_material
            )
        )
    except (TypeError, ValueError):
        generation_valid = False
    binding_status = identity.get("binding_status")
    binding_values = tuple(
        identity.get(key) for key in ("plan_id", "slot_id", "action_id")
    )
    plan_binding_valid = bool(
        (
            binding_status == "bound"
            and all(_storage_budget_revision(value) for value in binding_values)
        )
        or (
            binding_status == STORAGE_PRIMARY_BUDGET_BINDING_NOT_APPLICABLE
            and all(
                value == STORAGE_PRIMARY_BUDGET_BINDING_NOT_APPLICABLE
                for value in binding_values
            )
        )
    )
    return bool(
        binding.get("schema_version")
        == "legacy_heat_primary_budget_binding_v1"
        and binding.get("valid") is True
        and binding.get("source_contract_schema")
        == "storage_primary_budget_contract_v1"
        and binding.get("source") == "wb_pv_budget"
        and binding.get("evidence_status") == "VALID"
        and binding.get("revision_status") == "BOUND"
        and _storage_budget_revision(binding.get("source_contract_sha256"))
        and _storage_budget_revision(binding.get("budget_revision"))
        and generation_valid
        and binding.get("decision_generation") == generation
        and binding.get("decision_owner") == "storage_manager"
        and binding.get("decision_owner") == identity.get("decision_owner")
        and binding.get("decision_effect")
        == "flexible_consumer_power_budget"
        and binding.get("decision_effect") == identity.get("decision_effect")
        and type(binding.get("decision_ts")) is int
        and binding.get("decision_ts") == identity.get("decision_ts")
        and type(binding.get("authorized_wallbox_budget_w")) is int
        and binding.get("authorized_wallbox_budget_w") >= 0
        and binding.get("authorized_wallbox_budget_w")
        == identity.get("authorized_wallbox_budget_w")
        and type(binding.get("authorized_heatpump_budget_w")) is int
        and binding.get("authorized_heatpump_budget_w") >= 0
        and binding.get("authorized_heatpump_budget_w")
        == identity.get("authorized_heatpump_budget_w")
        and type(binding.get("authorized_heater_budget_w")) is int
        and binding.get("authorized_heater_budget_w") >= 0
        and binding.get("authorized_heater_budget_w")
        == identity.get("authorized_heater_budget_w")
        and binding.get("binding_status") == binding_status
        and plan_binding_valid
    )


def build_central_heatpump_start_budget_gate(
    storage_budget_contract,
    projected_heatpump_budget_w,
    required_start_w,
    *,
    demand_class,
    demand_first_seen_ts,
    projected_boost_permission=None,
    now_ts=None,
):
    """Bindet eine positive Wärmekante an die nächste zentrale Zuteilung.

    Die lokale Nachfrage wird zuerst auf der bestehenden Energy-Decision-Fläche
    publiziert. Ein bereits vor dieser Nachfrage erzeugtes Budget darf deshalb
    keinen Start autorisieren. Erst eine danach erzeugte, producer-, revisions-
    und wattgebundene ``authorized_heatpump_budget_w``-Zuteilung öffnet die
    positive Kante. Stop, Normal und Safety benutzen diesen Helfer nicht.
    """

    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    budget = storage_budget_contract if isinstance(storage_budget_contract, dict) else {}
    demand = str(demand_class or "none").strip().casefold()
    required_w = (
        int(required_start_w)
        if type(required_start_w) is int and required_start_w > 0
        else 0
    )
    first_seen = _strict_storage_budget_ts(demand_first_seen_ts)
    binding = _legacy_heat_primary_budget_binding(budget)
    authorized_w = binding.get("authorized_heatpump_budget_w")
    decision_ts = binding.get("decision_ts")
    timestamp_s = _strict_storage_budget_ts(budget.get("timestamp_s"))
    max_age_s = budget.get("max_age_s")
    max_age_valid = bool(
        not isinstance(max_age_s, bool)
        and isinstance(max_age_s, (int, float))
        and math.isfinite(float(max_age_s))
        and float(max_age_s) >= 0.0
    )
    fresh = bool(
        timestamp_s is not None
        and max_age_valid
        and timestamp_s <= now_value <= timestamp_s + float(max_age_s)
    )
    followup_revision = bool(
        type(decision_ts) is int
        and first_seen is not None
        and decision_ts >= int(math.ceil(first_seen))
    )
    allocation_exact = bool(
        type(projected_heatpump_budget_w) is int
        and type(authorized_w) is int
        and projected_heatpump_budget_w == authorized_w
    )
    permission_contract_present = type(projected_boost_permission) is bool
    boost_permission_active = (
        bound_central_heatpump_boost_permission(
            budget,
            projected_boost_permission,
            now_ts=now_value,
        )
        if permission_contract_present
        else False
    )
    blockers = []
    if demand in ("", "none"):
        blockers.append("HEATPUMP_POSITIVE_DEMAND_MISSING")
    if required_w <= 0:
        blockers.append("HEATPUMP_START_POWER_INVALID")
    if not _legacy_heat_primary_budget_binding_valid(binding) or not fresh:
        blockers.append("HEATPUMP_CENTRAL_BUDGET_NOT_FRESH_BOUND")
    if first_seen is None or not followup_revision:
        blockers.append("HEATPUMP_BUDGET_PREDATES_DEMAND")
    if permission_contract_present:
        if not boost_permission_active:
            blockers.append("HEATPUMP_BOOST_PERMISSION_INACTIVE")
    # Rückwärtskompatible alte Snapshots können das Bool noch nicht liefern;
    # ihre Wattbindung bleibt trotzdem derselben Prüfung unterworfen.
    # Das boolesche Signal ist ein Aktorvertrag, aber keine Energiequelle.
    # Auch v2 darf eine positive Startkante nur öffnen, wenn derselbe frische
    # Vertrag die vollständige Startleistung exakt an diesen Verbraucher
    # projiziert. Ein alter oder fehlerhafter True-Wert kann damit weder einen
    # 1-W- noch einen 0-W-Start autorisieren.
    if not allocation_exact:
        blockers.append("HEATPUMP_ALLOCATION_PROJECTION_MISMATCH")
    if type(authorized_w) is not int or authorized_w < required_w:
        blockers.append("HEATPUMP_AUTHORIZED_WATTS_INSUFFICIENT")
    blockers = list(dict.fromkeys(blockers))
    return {
        "schema_version": "central_heatpump_start_budget_gate_v1",
        "allowed": not blockers,
        "demand_class": demand or "none",
        "demand_first_seen_ts": first_seen,
        "required_start_w": required_w,
        "authorized_heatpump_budget_w": authorized_w if type(authorized_w) is int else None,
        "projected_heatpump_budget_w": (
            projected_heatpump_budget_w
            if type(projected_heatpump_budget_w) is int
            else None
        ),
        "budget_decision_ts": decision_ts if type(decision_ts) is int else None,
        "followup_revision": followup_revision,
        "heatpump_boost_permission_active": boost_permission_active,
        "action_binding_status": "FOLLOWUP_BUDGET_REVISION_ONLY",
        "blockers": blockers,
        "reason_code": blockers[0] if blockers else "HEATPUMP_CENTRAL_START_BUDGET_AUTHORIZED",
    }


def bound_central_heatpump_command_cap_w(
    storage_budget_contract,
    projected_heatpump_budget_w,
    *,
    now_ts=None,
):
    """Liefert ausschließlich eine frisch und exakt gebundene Wärme-Zuteilung.

    Die iDM-Überschussvorgabe darf weder das gesamte Restbudget noch eine
    Messleistung oder eine historische Manager-Zusage als Kommandorahmen
    verwenden. Ein ungültiger, stale oder wattmäßig abweichender Vertrag ist
    deshalb stets ein harter 0-W-Cap.
    """

    budget = (
        storage_budget_contract
        if isinstance(storage_budget_contract, dict)
        else {}
    )
    now_value = time.time() if now_ts is None else _safe_float(now_ts, 0.0)
    timestamp_s = _strict_storage_budget_ts(budget.get("timestamp_s"))
    max_age_s = budget.get("max_age_s")
    max_age_valid = bool(
        not isinstance(max_age_s, bool)
        and isinstance(max_age_s, (int, float))
        and math.isfinite(float(max_age_s))
        and float(max_age_s) >= 0.0
    )
    fresh = bool(
        now_value > 0.0
        and timestamp_s is not None
        and max_age_valid
        and timestamp_s <= now_value <= timestamp_s + float(max_age_s)
    )
    binding = _legacy_heat_primary_budget_binding(budget)
    authorized_w = binding.get("authorized_heatpump_budget_w")
    exact_projection = bool(
        type(projected_heatpump_budget_w) is int
        and type(authorized_w) is int
        and projected_heatpump_budget_w == authorized_w
    )
    if not (
        fresh
        and _legacy_heat_primary_budget_binding_valid(binding)
        and exact_projection
    ):
        return 0
    return max(0, int(authorized_w))


def bound_central_heatpump_boost_permission(
    storage_budget_contract,
    projected_permission,
    *,
    now_ts=None,
):
    """Liest die boolsche Wärmefreigabe nur frisch und revisionsgebunden.

    Die Freigabe ist ein Aktorvertrag, kein wattgenauer Sollwert. Die reale
    Leistungsaufnahme wird weiterhin ausschließlich als Accounting gebunden.
    """

    budget = (
        storage_budget_contract
        if isinstance(storage_budget_contract, dict)
        else {}
    )
    now_value = time.time() if now_ts is None else _safe_float(now_ts, 0.0)
    timestamp_s = _strict_storage_budget_ts(budget.get("timestamp_s"))
    max_age_s = budget.get("max_age_s")
    fresh = bool(
        now_value > 0.0
        and timestamp_s is not None
        and not isinstance(max_age_s, bool)
        and isinstance(max_age_s, (int, float))
        and math.isfinite(float(max_age_s))
        and float(max_age_s) >= 0.0
        and timestamp_s <= now_value <= timestamp_s + float(max_age_s)
    )
    binding = _legacy_heat_primary_budget_binding(budget)
    return bool(
        fresh
        and _legacy_heat_primary_budget_binding_valid(binding)
        and type(projected_permission) is bool
        and projected_permission is True
        and budget.get("heatpump_boost_permission_active") is True
    )


def build_heatpump_positive_signal_window(
    signal_started_ts,
    *,
    compressor_running,
    signal_readback_confirmed=True,
    signal_hold_guard=None,
    clock_sample=None,
    start_reservation_allowed=True,
    start_reservation_max_s=HEATPUMP_SG_READY_START_RESERVATION_MAX_S,
    now_ts=None,
):
    """Trennt die aktortypisierte Startreserve von der Signalhaltezeit."""

    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    reservation_max_s = max(
        0.0,
        _safe_float(
            start_reservation_max_s,
            HEATPUMP_SG_READY_START_RESERVATION_MAX_S,
        ),
    )
    started = _strict_storage_budget_ts(signal_started_ts)
    if started is None:
        return {
            "active": False,
            "bookkeeping_active": False,
            "readback_confirmed": False,
            "elapsed_s": 0.0,
            "wall_elapsed_s": 0.0,
            "full_start_reservation_active": False,
            "full_start_reservation_max_s": reservation_max_s,
            "acceptance_late": False,
            "minimum_signal_hold_active": False,
            "normal_release_allowed": True,
            "hold_guard": {},
        }
    wall_elapsed_s = max(0.0, now_value - started)
    current_clock_sample = (
        copy.deepcopy(clock_sample)
        if isinstance(clock_sample, dict)
        else control_time.sample(wall_ts=now_value)
    )
    if isinstance(signal_hold_guard, dict) and signal_hold_guard:
        evaluated_hold_guard = control_time.evaluate_guard(
            signal_hold_guard,
            current_clock_sample,
            minimum_s=HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S,
        )
    else:
        evaluated_hold_guard = control_time.begin_guard(
            HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S,
            current_clock_sample,
            minimum_s=HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S,
            epoch_mode=control_time.EPOCH_MODE_SAME_BOOT_MONOTONIC,
        )
    hold_remaining_s = max(
        0.0,
        _safe_float(
            evaluated_hold_guard.get("remaining_s"),
            HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S,
        ),
    )
    elapsed_s = max(
        0.0,
        HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S - hold_remaining_s,
    )
    if signal_readback_confirmed is not True:
        # Ein Restart-Checkpoint konserviert ausschließlich Zeit und
        # Demand-Klasse. Ohne frischen positiven Aktor-Readback darf daraus
        # weder eine Haltekante noch ein Refresh oder eine Startreserve
        # entstehen. Eine neue positive Kante benötigt den normalen
        # Demand->Folgebudget-Handshake.
        return {
            "active": False,
            "bookkeeping_active": True,
            "readback_confirmed": False,
            "elapsed_s": elapsed_s,
            "wall_elapsed_s": wall_elapsed_s,
            "full_start_reservation_active": False,
            "full_start_reservation_max_s": reservation_max_s,
            "acceptance_late": False,
            "minimum_signal_hold_active": False,
            "normal_release_allowed": False,
            "hold_guard": copy.deepcopy(evaluated_hold_guard),
        }
    compressor_confirmed = bool(compressor_running)
    full_start_reservation_active = bool(
        start_reservation_allowed is True
        and evaluated_hold_guard.get("fail_closed") is not True
        and evaluated_hold_guard.get("rearmed") is not True
        and not compressor_confirmed
        and elapsed_s < reservation_max_s
    )
    return {
        "active": True,
        "bookkeeping_active": True,
        "readback_confirmed": True,
        "elapsed_s": elapsed_s,
        "wall_elapsed_s": wall_elapsed_s,
        "full_start_reservation_active": full_start_reservation_active,
        "full_start_reservation_max_s": reservation_max_s,
        "acceptance_late": bool(
            not compressor_confirmed
            and not full_start_reservation_active
        ),
        "minimum_signal_hold_active": bool(
            evaluated_hold_guard.get("active") is True
        ),
        "normal_release_allowed": bool(
            evaluated_hold_guard.get("active") is not True
        ),
        "hold_guard": copy.deepcopy(evaluated_hold_guard),
    }


def heatpump_positive_actuator_permission_active(
    signal_window,
    demand_class,
    *,
    safety_stop=False,
):
    """Hält eine bestätigte Aktorfreigabe getrennt vom Watt-Accounting.

    Ein kurzzeitig fehlendes Zuteilungs- oder Messwatt beendet das Modbus-/SG-
    Signal nicht. Während der Mindesthaltezeit überlebt auch eine einzelne
    leere Demand-Projektion; danach braucht es weiterhin fachliche Nachfrage.
    """

    window = signal_window if isinstance(signal_window, dict) else {}
    demand = str(demand_class or "none").strip().casefold()
    return bool(
        safety_stop is not True
        and window.get("active") is True
        and (
            demand not in ("", "none")
            or window.get("minimum_signal_hold_active") is True
        )
    )


def heatpump_positive_actuator_readback(ctx, now_ts=None):
    """Typisiert einen frischen positiven oder nichtpositiven Aktorzustand.

    Der Helfer liest keine Hardware und schreibt nichts. Er bewertet nur die
    im aktuellen Zyklus bereits gebundene SG-/SHI-Rückmeldung. Lokale
    Sollcaches und Restart-Checkpoints sind ausdrücklich keine Evidenz.
    """

    source = ctx if isinstance(ctx, dict) else {}
    config = (
        source.get("current_config")
        if isinstance(source.get("current_config"), dict)
        else {}
    )
    wp_obj = source.get("wp")
    wp_type = source.get("wp_type")
    wp_status = (
        source.get("wp_status")
        if isinstance(source.get("wp_status"), dict)
        else {}
    )
    wp_data = (
        source.get("wp_data")
        if isinstance(source.get("wp_data"), dict)
        else {}
    )
    current_ts = time.time() if now_ts is None else _safe_float(now_ts, 0.0)
    max_age_s = max(
        1.0,
        _safe_float(
            config.get("consumer_acceptance_evidence_max_age_s"),
            45.0,
        ),
    )

    def fresh_timestamp(value):
        timestamp = _strict_storage_budget_ts(value)
        return (
            timestamp
            if timestamp is not None
            and current_ts > 0.0
            and 0.0 <= current_ts - timestamp <= max_age_s
            else None
        )

    def result(
        *,
        state_confirmed=False,
        positive=False,
        provider="none",
        timestamp=0.0,
        state=None,
        reason="no_fresh_positive_actuator_readback",
    ):
        confirmed = bool(state_confirmed)
        positive_confirmed = bool(confirmed and positive)
        return {
            "schema_version": "heatpump_positive_actuator_readback_v1",
            "state_confirmed": confirmed,
            "positive_confirmed": positive_confirmed,
            "nonpositive_confirmed": bool(confirmed and not positive),
            "provider": str(provider or "none"),
            "ts": float(timestamp or 0.0),
            "state": copy.deepcopy(state),
            "reason": str(reason),
        }

    if isinstance(wp_obj, ShellyHeatpump):
        sg_configured = wp_obj._relay_configured(wp_obj.sg_ip)
        pause_configured = wp_obj._relay_configured(wp_obj.pause_ip)
        sg_state = getattr(wp_obj, "last_live_sg_state", None)
        pause_state = getattr(wp_obj, "last_live_pause_state", None)
        sg_ts = fresh_timestamp(getattr(wp_obj, "last_live_sg_ts", 0.0))
        pause_ts = fresh_timestamp(
            getattr(wp_obj, "last_live_pause_ts", 0.0)
        )
        state = {"sg": sg_state, "pause": pause_state}
        if not sg_configured or type(sg_state) is not bool or sg_ts is None:
            return result(
                provider="shelly",
                state=state,
                reason="shelly_sg_readback_missing_or_stale",
            )
        if sg_state is False:
            return result(
                state_confirmed=True,
                positive=False,
                provider="shelly",
                timestamp=sg_ts,
                state=state,
                reason="shelly_sg_normal_confirmed",
            )
        if pause_configured:
            if type(pause_state) is not bool or pause_ts is None:
                return result(
                    provider="shelly",
                    state=state,
                    reason="shelly_pause_readback_missing_or_stale",
                )
            if pause_state is False:
                return result(
                    state_confirmed=True,
                    positive=False,
                    provider="shelly",
                    timestamp=pause_ts,
                    state=state,
                    reason="shelly_pause_confirmed",
                )
            readback_ts = min(sg_ts, pause_ts)
        else:
            readback_ts = sg_ts
        return result(
            state_confirmed=True,
            positive=True,
            provider="shelly",
            timestamp=readback_ts,
            state=state,
            reason="shelly_positive_sg_confirmed",
        )

    if type(wp_type) is int and wp_type == 0:
        source_ts = fresh_timestamp(wp_status.get("source_ts"))
        hz_mode = wp_status.get("SHI_HZ_Mode")
        ww_mode = wp_status.get("SHI_WW_Mode")
        hz_setpoint = wp_status.get("HZ_Setpoint")
        ww_setpoint = wp_status.get("WW_Setpoint")
        status_typed = bool(
            wp_status.get("valid") is True
            and wp_status.get("source_fresh") is True
            and source_ts is not None
            and type(hz_mode) is int
            and type(ww_mode) is int
        )
        state = {
            "SHI_HZ_Mode": hz_mode,
            "HZ_Setpoint": hz_setpoint,
            "SHI_WW_Mode": ww_mode,
            "WW_Setpoint": ww_setpoint,
        }
        if not status_typed:
            return result(
                provider="luxtronik",
                state=state,
                reason="luxtronik_shi_readback_missing_or_stale",
            )
        hz_setpoint_valid = bool(
            not isinstance(hz_setpoint, bool)
            and isinstance(hz_setpoint, (int, float))
            and math.isfinite(float(hz_setpoint))
        )
        luxtronik_pause_setpoint_c = max(
            15.0,
            min(
                22.0,
                _safe_float(
                    config.get("luxtronik_pause_setpoint_c"),
                    20.0,
                ),
            ),
        )
        heating_pause = bool(
            hz_mode == 1
            and hz_setpoint_valid
            and abs(
                float(hz_setpoint) - luxtronik_pause_setpoint_c
            ) < 1.0
        )
        positive = bool(
            ww_mode == 1
            or (
                hz_mode == 1
                and hz_setpoint_valid
                and float(hz_setpoint) > 20.0
                and not heating_pause
            )
        )
        if heating_pause and ww_mode == 1:
            return result(
                provider="luxtronik",
                state=state,
                reason="luxtronik_conflicting_pause_and_positive_readback",
            )
        if positive:
            return result(
                state_confirmed=True,
                positive=True,
                provider="luxtronik",
                timestamp=source_ts,
                state=state,
                reason="luxtronik_positive_shi_confirmed",
            )
        if (hz_mode == 0 and ww_mode == 0) or (
            heating_pause and ww_mode == 0
        ):
            return result(
                state_confirmed=True,
                positive=False,
                provider="luxtronik",
                timestamp=source_ts,
                state=state,
                reason=(
                    "luxtronik_pause_confirmed"
                    if heating_pause
                    else "luxtronik_shi_normal_confirmed"
                ),
            )
        return result(
            provider="luxtronik",
            state=state,
            reason="luxtronik_shi_state_not_positive_typed",
        )

    if type(wp_type) is int and wp_type == 5:
        state = wp_data.get("dimplex_sg_readback_state")
        timestamp = fresh_timestamp(wp_data.get("dimplex_sg_readback_ts"))
        confirmed = bool(
            wp_data.get("dimplex_sg_readback_confirmed") is True
            and type(state) is int
            and timestamp is not None
        )
        object_ts = fresh_timestamp(
            getattr(wp_obj, "last_sg_readback_ts", 0.0) if wp_obj else 0.0
        )
        object_state = (
            getattr(wp_obj, "last_sg_readback_state", None)
            if wp_obj
            else None
        )
        if (
            type(object_state) is int
            and object_ts is not None
            and (timestamp is None or object_ts >= timestamp)
        ):
            state = object_state
            timestamp = object_ts
            confirmed = True
        if not confirmed:
            return result(
                provider="dimplex",
                state=state,
                reason="dimplex_sg_readback_missing_or_stale",
            )
        if state in (DimplexHeatpump.SG_GREEN, DimplexHeatpump.SG_DARK_GREEN):
            return result(
                state_confirmed=True,
                positive=True,
                provider="dimplex",
                timestamp=timestamp,
                state=state,
                reason="dimplex_positive_sg_confirmed",
            )
        if state in (DimplexHeatpump.SG_NORMAL, DimplexHeatpump.SG_RED):
            return result(
                state_confirmed=True,
                positive=False,
                provider="dimplex",
                timestamp=timestamp,
                state=state,
                reason=(
                    "dimplex_sg_normal_confirmed"
                    if state == DimplexHeatpump.SG_NORMAL
                    else "dimplex_pause_confirmed"
                ),
            )
        return result(
            provider="dimplex",
            state=state,
            reason="dimplex_sg_state_unknown",
        )

    # iDM-Sollcaches werden beim Prozessstart initialisiert und sind damit
    # ausdrücklich kein frischer Register-Readback. Bis eine herstellergebundene
    # Rücklesefläche vorliegt, öffnet iDM nach Neustart nur über einen neuen
    # zentralen Demand->Budget-Handshake eine positive Kante.
    return result(
        provider="idm" if type(wp_type) is int and wp_type == 1 else "none",
        reason="positive_actuator_readback_evidence_limit",
    )


def reconcile_heatpump_driver_cache_from_readback(wp_obj, wp_type, readback):
    """Übernimmt ausschließlich einen bestätigten physischen Aktorzustand.

    Dadurch kann ein danach weiterhin verlangtes Positivkommando nicht wegen
    eines veralteten Sollcaches übersprungen werden. Der Helfer schreibt keine
    Hardware und wertet unbestätigte Telemetrie ausdrücklich nicht aus.
    """

    evidence = readback if isinstance(readback, dict) else {}
    if evidence.get("state_confirmed") is not True:
        return False
    state = evidence.get("state")
    if isinstance(wp_obj, ShellyHeatpump) and isinstance(state, dict):
        sg_state = state.get("sg")
        pause_state = state.get("pause")
        if type(sg_state) is bool:
            wp_obj.sg_state = sg_state
        if type(pause_state) is bool:
            wp_obj.pause_state = pause_state
        return type(sg_state) is bool
    if type(wp_type) is int and wp_type == 0 and isinstance(state, dict):
        hz_mode = state.get("SHI_HZ_Mode")
        ww_mode = state.get("SHI_WW_Mode")
        if type(hz_mode) is int:
            wp_obj.curr_ext_hz = hz_mode
            wp_obj.last_sent_hz = (
                hz_mode,
                state.get("HZ_Setpoint") if hz_mode else None,
            )
        if type(ww_mode) is int:
            wp_obj.curr_ext_ww = ww_mode
            wp_obj.last_sent_ww = (
                ww_mode,
                state.get("WW_Setpoint") if ww_mode else None,
            )
        return type(hz_mode) is int and type(ww_mode) is int
    if type(wp_type) is int and wp_type == 5 and type(state) is int:
        wp_obj.curr_sg_state = state
        wp_obj.last_sent_sg_state = state
        return True
    return False


def build_legacy_heat_automation_owner_contract(
    *,
    runtime_enabled,
    auto_mode,
    actuator_write_allowed,
    storage_budget_contract,
    now_ts=None,
):
    """Versiegelt die historische Automatik an ihre reale Budgetquelle."""

    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    budget = storage_budget_contract if isinstance(storage_budget_contract, dict) else {}
    budget_schema = str(budget.get("schema_version") or "")
    budget_source = str(budget.get("source") or "")
    budget_binding = _legacy_heat_primary_budget_binding(budget)
    timestamp_s = _strict_storage_budget_ts(budget.get("timestamp_s"))
    max_age_s = _safe_float(budget.get("max_age_s"), -1.0)
    age_s = (
        now_value - timestamp_s
        if timestamp_s is not None
        else None
    )
    fresh = bool(
        timestamp_s is not None
        and max_age_s >= 0.0
        and age_s is not None
        and 0.0 <= age_s <= max_age_s
    )
    budget_digest = None
    try:
        budget_digest = heat_intent.revision_hash(budget)
    except (TypeError, ValueError):
        pass
    valid = bool(
        runtime_enabled is False
        and auto_mode is True
        and actuator_write_allowed is True
        and budget.get("accepted") is True
        and budget_schema == "storage_primary_budget_contract_v1"
        and budget_source == "wb_pv_budget"
        and budget.get("evidence_status") == "VALID"
        and budget.get("revision_status") == "BOUND"
        and budget_binding.get("valid") is True
        and fresh
        and budget_digest
    )
    contract = {
        "schema_version": "legacy_heat_automation_owner_v1",
        "contract_version": 1,
        "valid": valid,
        "owner": "legacy_heat_automation",
        "effect_scope": "automatic_heat_start_pause_stop",
        "runtime_enabled": runtime_enabled if isinstance(runtime_enabled, bool) else None,
        "auto_mode": auto_mode if isinstance(auto_mode, bool) else None,
        "actuator_write_allowed": (
            actuator_write_allowed
            if isinstance(actuator_write_allowed, bool)
            else None
        ),
        "storage_budget_schema": budget_schema or None,
        "storage_budget_source": budget_source or None,
        "storage_budget_accepted": budget.get("accepted") is True,
        "storage_budget_evidence_status": budget.get("evidence_status"),
        "storage_budget_revision_status": budget.get("revision_status"),
        "storage_budget_binding": budget_binding,
        "storage_budget_timestamp_s": timestamp_s,
        "storage_budget_max_age_s": max_age_s if max_age_s >= 0.0 else None,
        "storage_budget_valid_until_ts": (
            timestamp_s + max_age_s
            if timestamp_s is not None and max_age_s >= 0.0
            else None
        ),
        "storage_budget_contract_sha256": budget_digest,
        "evaluated_ts": now_value,
        "reason_code": (
            "LEGACY_HEAT_AUTOMATION_OWNER_BOUND"
            if valid
            else "LEGACY_HEAT_AUTOMATION_OWNER_INCOMPLETE"
        ),
    }
    contract["owner_id"] = heat_intent.revision_hash(contract)
    return contract


def legacy_heat_automation_owner_contract_valid(contract, *, now_ts=None):
    """Prüft den versiegelten Legacy-Owner ohne Typ- oder Frischeimputation."""

    if not isinstance(contract, dict):
        return False
    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    owner_id = contract.get("owner_id")
    material = copy.deepcopy(contract)
    material.pop("owner_id", None)
    try:
        expected_owner_id = heat_intent.revision_hash(material)
    except (TypeError, ValueError):
        return False
    source_pair_valid = bool(
        contract.get("storage_budget_schema")
        == "storage_primary_budget_contract_v1"
        and contract.get("storage_budget_source") == "wb_pv_budget"
    )
    valid_until_ts = _strict_storage_budget_ts(
        contract.get("storage_budget_valid_until_ts")
    )
    timestamp_s = _strict_storage_budget_ts(
        contract.get("storage_budget_timestamp_s")
    )
    max_age_s = contract.get("storage_budget_max_age_s")
    max_age_valid = bool(
        not isinstance(max_age_s, bool)
        and isinstance(max_age_s, (int, float))
        and math.isfinite(float(max_age_s))
        and float(max_age_s) >= 0.0
    )
    return bool(
        contract.get("schema_version") == "legacy_heat_automation_owner_v1"
        and type(contract.get("contract_version")) is int
        and contract.get("contract_version") == 1
        and contract.get("valid") is True
        and contract.get("owner") == "legacy_heat_automation"
        and contract.get("effect_scope")
        == "automatic_heat_start_pause_stop"
        and contract.get("runtime_enabled") is False
        and contract.get("auto_mode") is True
        and contract.get("actuator_write_allowed") is True
        and contract.get("storage_budget_accepted") is True
        and contract.get("storage_budget_evidence_status") == "VALID"
        and contract.get("storage_budget_revision_status") == "BOUND"
        and _legacy_heat_primary_budget_binding_valid(
            contract.get("storage_budget_binding")
        )
        and contract.get("storage_budget_binding", {}).get(
            "source_contract_sha256"
        ) == contract.get("storage_budget_contract_sha256")
        and source_pair_valid
        and timestamp_s is not None
        and max_age_valid
        and valid_until_ts is not None
        and abs(
            valid_until_ts - (timestamp_s + float(max_age_s))
        ) <= 0.001
        and timestamp_s <= now_value <= valid_until_ts
        and isinstance(contract.get("storage_budget_contract_sha256"), str)
        and re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            contract.get("storage_budget_contract_sha256"),
        )
        is not None
        and isinstance(owner_id, str)
        and owner_id == expected_owner_id
    )


def automatic_heat_demand_actuation_allowed(
    demand_class,
    *,
    policy_actuation_allowed,
    ww_timer_owner_contract,
    now_ts=None,
):
    """Trennt den Nutzer-WW-Timer von der noch gesperrten Heat-Policy.

    Der konfigurierte Warmwasser-Zeitplan ist eine konventionelle Nachfrage
    und keine Freigabe der neuen PV-/Preis-Policy. Er darf deshalb bei einem
    frischen, revisionsgebundenen Storage-Budget weiterarbeiten, auch wenn
    ``heat_intent_v1`` noch ausschließlich im Shadow läuft. Alle anderen
    Nachfrageklassen bleiben an den ausführbaren Policy-Vertrag gebunden.
    """

    if demand_class in {"ww_timer_comfort", "ww_timer_eco"}:
        return legacy_heat_automation_owner_contract_valid(
            ww_timer_owner_contract,
            now_ts=now_ts,
        )
    return policy_actuation_allowed is True


def heat_runtime_actuation_contract(
    runtime_enabled,
    *,
    validated_intent=None,
    runtime_validation=None,
    policy_owner=None,
    legacy_owner_contract=None,
    now_ts=None,
):
    """Bindet einen zentralen Wärmeausgang an Intent, Runtime und Owner.

    ``heat_intent_v1`` ist derzeit ausdrücklich ``shadow_only``. Der Vertrag
    macht deshalb keine implizite Feldfreigabe aus einem Policy-Ziel: Erst ein
    späterer, explizit aktivierter Intent samt passender Runtimevalidierung
    kann ``authorized=True`` erreichen. Der getrennte Legacy-Modus wird bei
    ``runtime_enabled=False`` weder bewertet noch verändert.
    """

    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    runtime_flag_valid = isinstance(runtime_enabled, bool)
    enabled = runtime_enabled is True
    applicable = bool(enabled or not runtime_flag_valid)
    source = validated_intent if isinstance(validated_intent, dict) else {}
    runtime = runtime_validation if isinstance(runtime_validation, dict) else {}
    owner = str(policy_owner or "").strip()
    intent_id = source.get("intent_id")
    owner_binding_id = (
        heat_intent.revision_hash({
            "intent_id": intent_id,
            "policy_owner": owner,
        })
        if isinstance(intent_id, str) and intent_id and owner
        else None
    )
    base = {
        "schema_version": "heat_runtime_actuation_contract_v1",
        "applicable": applicable,
        "authorized": False,
        "runtime_enabled": enabled,
        "runtime_flag_valid": runtime_flag_valid,
        "intent_id": intent_id if isinstance(intent_id, str) else None,
        "policy_owner": owner or None,
        "owner_binding_id": owner_binding_id,
    }
    if runtime_enabled is False:
        legacy_valid = legacy_heat_automation_owner_contract_valid(
            legacy_owner_contract,
            now_ts=now_value,
        )
        return {
            **base,
            "applicable": True,
            "authorized": legacy_valid,
            "legacy_owner_contract": (
                copy.deepcopy(legacy_owner_contract)
                if isinstance(legacy_owner_contract, dict)
                else None
            ),
            "reason_codes": [
                "LEGACY_HEAT_AUTOMATION_OWNER_BOUND"
                if legacy_valid
                else "LEGACY_HEAT_AUTOMATION_OWNER_INVALID"
            ],
        }
    if not runtime_flag_valid:
        return {
            **base,
            "reason_codes": ["HEAT_RUNTIME_ENABLE_FLAG_INVALID"],
        }

    reasons = []
    intent_validation = heat_intent.validate_heat_intent(source)
    if intent_validation.get("valid") is not True:
        reasons.append("HEAT_INTENT_INVALID")
    if source.get("shadow_only") is not False:
        reasons.append("HEAT_INTENT_SHADOW_ONLY")
    if source.get("commands_allowed") is not True:
        reasons.append("HEAT_INTENT_COMMANDS_NOT_ALLOWED")
    if source.get("executable") is not True:
        reasons.append("HEAT_INTENT_NOT_EXECUTABLE")
    if not isinstance(intent_id, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        intent_id,
    ):
        reasons.append("HEAT_INTENT_ID_INVALID")
    if runtime.get("schema_version") != "heat_intent_runtime_validation_v1":
        reasons.append("HEAT_RUNTIME_VALIDATION_SCHEMA_INVALID")
    if runtime.get("validation_valid") is not True:
        reasons.append("HEAT_RUNTIME_VALIDATION_INVALID")
    if runtime.get("binding_valid") is not True:
        reasons.append("HEAT_RUNTIME_BINDING_INVALID")
    if runtime.get("activation_authorized") is not True:
        reasons.append("HEAT_RUNTIME_ACTIVATION_NOT_AUTHORIZED")
    if runtime.get("shadow_only") is not False:
        reasons.append("HEAT_RUNTIME_SHADOW_ONLY")
    if runtime.get("commands_allowed") is not True:
        reasons.append("HEAT_RUNTIME_COMMANDS_NOT_ALLOWED")
    if runtime.get("executable") is not True:
        reasons.append("HEAT_RUNTIME_NOT_EXECUTABLE")
    if runtime.get("intent_id") != intent_id:
        reasons.append("HEAT_RUNTIME_INTENT_ID_MISMATCH")
    if not owner or runtime.get("policy_owner") != owner:
        reasons.append("HEAT_RUNTIME_OWNER_MISMATCH")
    if (
        owner_binding_id is None
        or runtime.get("owner_binding_id") != owner_binding_id
    ):
        reasons.append("HEAT_RUNTIME_OWNER_BINDING_MISMATCH")
    reasons = list(dict.fromkeys(reasons))
    return {
        **base,
        "authorized": not reasons,
        "reason_codes": reasons or ["HEAT_RUNTIME_ACTUATION_AUTHORIZED"],
    }


def automatic_heat_policy_actuation_allowed(runtime_enabled, contract):
    """Jede automatische Wirkung braucht ihren typisierten Ownervertrag."""

    return bool(
        isinstance(contract, dict)
        and isinstance(runtime_enabled, bool)
        and contract.get("applicable") is True
        and contract.get("authorized") is True
    )


def attempt_heatpump_pv_boost_start(
    wp,
    boost_args,
    *,
    now_ts=None,
    retry_not_before_ts=0.0,
    retry_backoff_s=60.0,
    runtime_enabled=False,
    validated_intent=None,
    runtime_validation=None,
    policy_owner=None,
    owner_contract=None,
):
    """Führt ausschließlich einen PV-Boost-Start mit typisiertem Ergebnis aus.

    Der Backoff begrenzt fehlgeschlagene Startversuche. Stop-, Nutzer-Aus- und
    Safety-Pfade rufen diesen Starthelfer bewusst nicht auf und bleiben daher
    jederzeit ausführbar.
    """
    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    runtime_contract = heat_runtime_actuation_contract(
        runtime_enabled,
        validated_intent=validated_intent,
        runtime_validation=runtime_validation,
        policy_owner=policy_owner,
        legacy_owner_contract=owner_contract,
        now_ts=now_value,
    )
    if (
        runtime_contract.get("applicable") is True
        and runtime_contract.get("authorized") is not True
    ):
        return {
            "status": "blocked",
            "pending": False,
            "attempted": False,
            "confirmed": False,
            "failed": False,
            "command_sent": False,
            "readback_confirmed": False,
            "reason": str(
                (runtime_contract.get("reason_codes") or [
                    "heat_runtime_actuation_not_authorized"
                ])[0]
            ),
            "retry_not_before_ts": now_value + max(
                1.0,
                _safe_float(retry_backoff_s, 60.0),
            ),
            "runtime_actuation_contract": runtime_contract,
        }
    retry_at = max(0.0, _safe_float(retry_not_before_ts, 0.0))
    if now_value < retry_at:
        return {
            "status": "backoff",
            "pending": True,
            "attempted": False,
            "confirmed": False,
            "failed": False,
            "command_sent": False,
            "readback_confirmed": False,
            "reason": "retry_backoff_active",
            "retry_not_before_ts": retry_at,
        }

    if wp is None:
        return {
            "status": "failed",
            "pending": False,
            "attempted": False,
            "confirmed": False,
            "failed": True,
            "command_sent": False,
            "readback_confirmed": False,
            "reason": "heatpump_driver_missing",
            "retry_not_before_ts": now_value + max(1.0, _safe_float(retry_backoff_s, 60.0)),
        }

    raw_result = False
    raised_reason = ""
    try:
        raw_result = bool(wp.set_boost(*tuple(boost_args)))
    except Exception as exc:
        raised_reason = f"driver_exception:{type(exc).__name__}"

    driver_outcome = getattr(wp, "last_boost_outcome", None)
    if isinstance(driver_outcome, dict):
        attempted = bool(driver_outcome.get("attempted", True))
        command_sent = bool(driver_outcome.get("command_sent", False))
        readback_confirmed = bool(driver_outcome.get("readback_confirmed", False))
        reason = str(driver_outcome.get("reason", "") or raised_reason)
        driver_status = str(driver_outcome.get("status", "") or "failed")
        confirmed = bool(raw_result and driver_status == "confirmed" and readback_confirmed)
    else:
        # Bestehende Nicht-IDM-Treiber liefern True nur nach ihrem bestätigten
        # Treibervertrag. Der neue IDM-Pfad verwendet immer das typisierte Dict.
        attempted = True
        command_sent = bool(raw_result)
        readback_confirmed = bool(raw_result)
        reason = raised_reason or ("legacy_driver_confirmed" if raw_result else "legacy_driver_failed")
        driver_status = "confirmed" if raw_result else "failed"
        confirmed = bool(raw_result)

    if confirmed:
        return {
            "status": "confirmed",
            "pending": False,
            "attempted": attempted,
            "confirmed": True,
            "failed": False,
            "command_sent": command_sent,
            "readback_confirmed": readback_confirmed,
            "reason": reason,
            "retry_not_before_ts": 0.0,
        }

    return {
        "status": "blocked" if driver_status == "blocked" else "failed",
        "pending": True,
        "attempted": attempted,
        "confirmed": False,
        "failed": True,
        "command_sent": command_sent,
        "readback_confirmed": readback_confirmed,
        "reason": reason or "start_not_confirmed",
        "retry_not_before_ts": now_value + max(1.0, _safe_float(retry_backoff_s, 60.0)),
    }


def wallbox_phase_transition_blocks_heatpump_start(budget_surface, now_ts=None):
    """Blockiere nur neue WP-Starts während einer frischen Wallbox-Umschaltung."""

    data = budget_surface if isinstance(budget_surface, dict) else {}
    nested = data.get("wallbox_phase_transition")
    nested = nested if isinstance(nested, dict) else {}
    active = bool(
        data.get("wallbox_phase_transition_active")
        or nested.get("active")
    )
    reserved_w = max(
        0,
        _safe_int(data.get("wallbox_phase_transition_reserved_w"), 0),
        _safe_int(data.get("wallbox_phase_transition_requested_w_total"), 0),
        _safe_int(nested.get("requested_w", nested.get("reserved_w", 0)), 0),
    )
    expires_ts = _safe_float(
        data.get(
            "wallbox_phase_transition_until_ts",
            nested.get("expires_ts", 0.0),
        ),
        0.0,
    )
    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    return bool(
        active
        and reserved_w > 0
        and (expires_ts <= 0.0 or now_value < expires_ts)
    )


def heatpump_surplus_budget_during_phase_transition(
    free_for_limbs_w,
    wallbox_phase_transition_active,
    *,
    heatpump_running=False,
    heatpump_running_commitment_w=0,
):
    """Sperrt neue Starts, erhält aber eine bereits laufende Wärmepumpenfreigabe."""

    if wallbox_phase_transition_active:
        if heatpump_running:
            return max(
                0,
                _safe_int(heatpump_running_commitment_w, 0),
                _safe_int(free_for_limbs_w, 0),
            )
        return 0
    return max(0, _safe_int(free_for_limbs_w, 0))


def heatpump_takt_start_block(wp_takt_protect, last_stop_ts, restart_block_min, now_ts=None):
    """Return remaining restart-block seconds for a heat-pump boost start."""
    if not wp_takt_protect:
        return 0.0
    last_stop = _safe_float(last_stop_ts, 0.0)
    block_s = max(0.0, _safe_float(restart_block_min, 0.0) * 60.0)
    if last_stop <= 0.0 or block_s <= 0.0:
        return 0.0
    now_s = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    return max(0.0, block_s - max(0.0, now_s - last_stop))

def heatpump_takt_stop_block(wp_takt_protect, last_start_ts, min_runtime_min, now_ts=None, emergency_stop=False):
    """Return remaining minimum-runtime seconds before a non-emergency stop."""
    if emergency_stop or not wp_takt_protect:
        return 0.0
    last_start = _safe_float(last_start_ts, 0.0)
    min_s = max(0.0, _safe_float(min_runtime_min, 0.0) * 60.0)
    if last_start <= 0.0 or min_s <= 0.0:
        return 0.0
    now_s = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    return max(0.0, min_s - max(0.0, now_s - last_start))

def heatpump_ww_price_stop_allowed(
    price_action,
    started_ts,
    *,
    now_ts=None,
    min_runtime_s=None,
):
    """Erlaubt einen rein ökonomischen WW-Stopp erst nach belegter Mindestzeit."""

    if str(price_action or "") != "PAUSE_HIGH_PRICE":
        return False
    started = _safe_float(started_ts, 0.0)
    if started <= 0.0:
        return False
    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    required_s = max(
        float(heat_policy.WW_CYCLE_MIN_RUNTIME_S),
        _safe_float(min_runtime_s, heat_policy.WW_CYCLE_MIN_RUNTIME_S),
    )
    return max(0.0, now_value - started) >= required_s

def heatpump_native_source_timestamp(wp_status):
    """Liefert ausschließlich einen typisierten nativen Messzeitpunkt."""

    status = wp_status if isinstance(wp_status, dict) else {}
    source_ts = status.get("source_ts")
    if (
        isinstance(source_ts, bool)
        or not isinstance(source_ts, (int, float))
        or not math.isfinite(float(source_ts))
        or float(source_ts) <= 0.0
    ):
        return None
    return float(source_ts)


def heatpump_native_source_freshness(source_ts, *, now_ts=None, max_age_s=None):
    """Prüft native Datei-Frische ohne Zukunftszeit zu imputieren."""

    source = heatpump_native_source_timestamp({"source_ts": source_ts})
    now_value = time.time() if now_ts is None else now_ts
    maximum_age_s = (
        HEATPUMP_LIVE_REVALIDATION_MAX_AGE_S
        if max_age_s is None
        else max_age_s
    )
    values_typed = bool(
        source is not None
        and not isinstance(now_value, bool)
        and isinstance(now_value, (int, float))
        and math.isfinite(float(now_value))
        and float(now_value) >= 0.0
        and not isinstance(maximum_age_s, bool)
        and isinstance(maximum_age_s, (int, float))
        and math.isfinite(float(maximum_age_s))
        and float(maximum_age_s) >= 0.0
    )
    if not values_typed:
        return {
            "source_ts": source,
            "source_age_s": None,
            "source_fresh": False,
        }
    source_age_s = float(now_value) - float(source)
    return {
        "source_ts": float(source),
        "source_age_s": source_age_s,
        "source_fresh": bool(
            0.0 <= source_age_s <= float(maximum_age_s)
        ),
    }


def heatpump_power_observation(wp_data, wp_status=None):
    """Bindet WP-Leistung an frische, gültige native Messdaten.

    Ein fehlender, ungültiger oder veralteter Quellstatus darf insbesondere
    nicht als gemessene 0 W erscheinen. Rund 150 W bleiben bei gültiger
    Evidenz als Pumpenvorlauf sichtbar; erst ab 500 W gilt die Aufnahme als
    Verdichterannahme.
    """

    data = wp_data if isinstance(wp_data, dict) else {}
    status = wp_status if isinstance(wp_status, dict) else {}
    source_ts = heatpump_native_source_timestamp(status)
    source_valid = bool(
        status.get("valid") is True
        and status.get("source_fresh") is True
        and source_ts is not None
    )
    raw_power_w = data.get("Leistung_Verdichter_W")
    power_typed = bool(
        not isinstance(raw_power_w, bool)
        and isinstance(raw_power_w, (int, float))
        and math.isfinite(float(raw_power_w))
        and float(raw_power_w) >= 0.0
    )
    if not source_valid or not power_typed:
        return None, False, False
    observed_wp_power_w = int(float(raw_power_w))
    return observed_wp_power_w, True, observed_wp_power_w >= 500


def heatpump_budget_request_readiness(ctx):
    """Bindet eine neue Budgetanfrage an frische Geräte- und Temperaturgates."""

    source = ctx if isinstance(ctx, dict) else {}
    wp_data = source.get("wp_data") if isinstance(source.get("wp_data"), dict) else {}
    wp_status = source.get("wp_status") if isinstance(source.get("wp_status"), dict) else {}
    config = source.get("current_config") if isinstance(source.get("current_config"), dict) else {}
    request_w = max(0, abs(_safe_int(source.get("GRID_START_LIMIT"), -3500)))
    demand_class = str(
        source.get("heatpump_budget_demand_class") or "none"
    ).strip().casefold()
    demand_target_c = source.get("heatpump_budget_demand_target_c")
    signal_window = (
        source.get("heatpump_positive_signal_window")
        if isinstance(source.get("heatpump_positive_signal_window"), dict)
        else {}
    )
    blockers = []
    if demand_class in ("", "none"):
        blockers.append("heatpump_positive_demand_missing")
    # Die aktortypisierte Startreserve (direkt 25 s, SG-Ready 150 s) ist nur
    # ein Accounting-Fenster. Ihr Ablauf beendet weder den fachlichen
    # Wärmebedarf noch eine bereits erteilte Aktorfreigabe. Sonst würde die
    # Wärmepumpe nach der Pumpenvorlaufzeit ihren eigenen Demand verlieren.
    if not bool(source.get("wp")) or not bool(source.get("wp_connected")):
        blockers.append("heatpump_not_connected")
    if _safe_int(source.get("AUTO_MODE"), 0) != 1:
        blockers.append("heatpump_auto_off")
    if wp_status.get("valid") is not True or wp_status.get("source_fresh") is not True:
        blockers.append("heatpump_status_invalid")
    if bool(source.get("heatpump_pause_blocks_boost")):
        blockers.append("heatpump_pause_active")
    if bool(source.get("car_blocks_boost_applied")):
        blockers.append("heatpump_blocked_by_vehicle_priority")
    if bool(source.get("wallbox_phase_transition_active")):
        blockers.append("wallbox_phase_transition_active")
    if bool(source.get("predump_heatpump_targets_reached")):
        blockers.append("heatpump_target_reached")
    if bool(source.get("predump_heatpump_protect_block")):
        blockers.append("heatpump_source_protection")
    if _safe_float(source.get("price_heatpump_start_block_remaining_s"), 0.0) > 0.0:
        blockers.append("heatpump_restart_block")
    wp_obj = source.get("wp")
    if bool(getattr(wp_obj, "actor_writes_blocked", False)):
        blockers.append("heatpump_actuator_blocked")

    deadband_c = max(
        0.1,
        _safe_float(config.get("heat_policy_temperature_deadband_c"), 0.2),
    )
    if demand_class.startswith("ww_"):
        ww_actual_c = wp_data.get(
            "Warmwasser_Ist",
            wp_data.get("Warmwasser-Ist"),
        )
        target_valid = bool(
            not isinstance(demand_target_c, bool)
            and isinstance(demand_target_c, (int, float))
            and math.isfinite(float(demand_target_c))
        )
        actual_valid = bool(
            not isinstance(ww_actual_c, bool)
            and isinstance(ww_actual_c, (int, float))
            and math.isfinite(float(ww_actual_c))
        )
        temperature = {
            "temperature_valid": bool(target_valid and actual_valid),
            "temperature_c": float(ww_actual_c) if actual_valid else None,
            "temperature_max_c": float(demand_target_c) if target_valid else None,
            "temperature_source": "warmwater_demand",
        }
    else:
        temperature = _heat_policy_temperature_context(
            config,
            wp_data,
            source.get("at_mittel", 20.0),
            source.get("HEIZGRENZE_TEMP", 10.0),
            source.get("CONF_WWS", 50.0),
            source.get("CONF_HZ", 32.0),
        )
    if not temperature.get("temperature_valid"):
        blockers.append("heatpump_temperature_invalid")
    elif (
        temperature.get("temperature_max_c") is not None
        and _safe_float(temperature.get("temperature_c"), 0.0)
        >= _safe_float(temperature.get("temperature_max_c"), 0.0) - deadband_c
    ):
        blockers.append("heatpump_temperature_satisfied")
    if request_w <= 0:
        blockers.append("heatpump_start_request_invalid")
    return {
        "schema_version": "heatpump_budget_request_readiness_v1",
        "ready": not blockers,
        "request_w": request_w if not blockers else 0,
        "signal_semantics": "start_recommendation_not_acceptance",
        "demand_class": demand_class or "none",
        "demand_target_c": (
            float(demand_target_c)
            if not isinstance(demand_target_c, bool)
            and isinstance(demand_target_c, (int, float))
            and math.isfinite(float(demand_target_c))
            else None
        ),
        "full_start_reservation_max_s": _safe_float(
            signal_window.get("full_start_reservation_max_s"),
            heatpump_start_reservation_duration_s(
                source.get("wp_type", -1),
                config.get("shelly_sg_ip", ""),
            ),
        ),
        "positive_signal_min_hold_s": HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S,
        "blockers": blockers,
        "temperature": temperature,
        "deadband_c": deadband_c,
    }

_last_luxtronik_history_trim_ts = 0.0
_last_luxtronik_archive_bucket_by_path = {}

def _warn_once(key, message, *args):
    if key in _SESSION_WRITE_WARNED:
        return
    logger.warning(message, *args)
    _SESSION_WRITE_WARNED.add(key)

def _luxtronik_history_date(payload, now_obj=None):
    ts_raw = payload.get("ts") if isinstance(payload, dict) else None
    try:
        if ts_raw:
            return datetime.fromisoformat(str(ts_raw)).strftime("%Y-%m-%d")
    except Exception:
        pass
    if isinstance(now_obj, datetime):
        return now_obj.strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")

def luxtronik_archive_path_for_payload(payload, now_obj=None):
    return os.path.join(BACKUP_DIR, f"luxtronik_{_luxtronik_history_date(payload, now_obj)}.json")

def _luxtronik_payload_timestamp(payload, now_obj=None):
    if isinstance(now_obj, datetime):
        return now_obj.timestamp()
    ts_raw = payload.get("ts") if isinstance(payload, dict) else None
    try:
        if isinstance(ts_raw, (int, float)):
            return float(ts_raw)
        if ts_raw:
            return datetime.fromisoformat(str(ts_raw)).timestamp()
    except Exception:
        pass
    return time.time()

def _luxtronik_archive_bucket(payload, now_obj=None):
    return int(
        _luxtronik_payload_timestamp(payload, now_obj)
        // max(60, int(LUXTRONIK_ARCHIVE_INTERVAL_S))
    )

def _luxtronik_compact_archive_payload(payload):
    source = payload if isinstance(payload, dict) else {}
    return {
        "schema_version": "luxtronik_archive_v2",
        "ts": source.get("ts"),
        "data": source.get("data") if isinstance(source.get("data"), dict) else {},
        "status": source.get("status") if isinstance(source.get("status"), dict) else {},
    }

def _last_jsonl_record(path, tail_bytes=64 * 1024):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max(1024, int(tail_bytes))))
            raw = handle.read()
        for line in reversed(raw.splitlines()):
            if not line.strip():
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # Nach einem Stromausfall kann ausschließlich die letzte
                # Append-Zeile unvollständig sein. Für den Bucket-Neustart
                # zählt dann weiterhin der vorherige vollständig lesbare Satz.
                continue
            if isinstance(record, dict):
                return record
    except Exception:
        return None
    return None

def _luxtronik_archive_due(archive_path, payload, now_obj=None):
    bucket = _luxtronik_archive_bucket(payload, now_obj)
    if archive_path not in _last_luxtronik_archive_bucket_by_path:
        previous = _last_jsonl_record(archive_path)
        if isinstance(previous, dict):
            _last_luxtronik_archive_bucket_by_path[archive_path] = (
                _luxtronik_archive_bucket(previous)
            )
    return (
        _last_luxtronik_archive_bucket_by_path.get(archive_path) != bucket,
        bucket,
    )

def _append_history_line(path, line, mode=0o664):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existed = os.path.exists(path)
    encoded = line.encode("utf-8")
    with open(path, "ab+") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                # Eine partielle Crash-Zeile bleibt forensisch erhalten, wird
                # aber klar vom ersten gültigen Satz nach Neustart getrennt.
                f.seek(0, os.SEEK_END)
                f.write(b"\n")
        f.seek(0, os.SEEK_END)
        f.write(encoded)
    if not existed:
        try:
            os.chmod(path, mode)
        except Exception:
            pass

def trim_luxtronik_ramdisk_history(
    path=HISTORY_FILE,
    max_lines=LUXTRONIK_RAMDISK_HISTORY_MAX_LINES,
    max_bytes=LUXTRONIK_RAMDISK_HISTORY_MAX_BYTES,
    force=False,
):
    """Keep only a bounded live Luxtronik window in tmpfs.

    Das persistente Archiv enthält kompakte Fünf-Minuten-Stützstellen. Diese
    Ramdisk-Datei bleibt der vollständige Minutenpuffer für Live-Charts.
    """
    try:
        if not os.path.exists(path):
            return True
        current_size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8") as f:
            lines = [line for line in f.readlines() if line.strip()]
        if (
            not force
            and current_size <= max_bytes
            and len(lines) <= max(1, int(max_lines))
        ):
            return True
        keep_lines = lines[-max(1, int(max_lines)):]
        while keep_lines and sum(len(line.encode("utf-8")) for line in keep_lines) > max_bytes:
            keep_lines = keep_lines[max(1, len(keep_lines) // 10):]
        tmp_path = f"{path}.trim.{os.getpid()}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.writelines(keep_lines)
            try:
                os.chmod(tmp_path, 0o664)
            except Exception:
                pass
            os.replace(tmp_path, path)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
        try:
            os.chmod(path, 0o664)
        except Exception:
            pass
        if len(keep_lines) != len(lines) or current_size > max_bytes:
            logger.info(
                "Luxtronik-Ramdisk-Historie begrenzt: %s -> %s Zeilen, %.1f MB -> %.1f MB",
                len(lines),
                len(keep_lines),
                current_size / (1024 * 1024),
                os.path.getsize(path) / (1024 * 1024) if os.path.exists(path) else 0.0,
            )
        return True
    except Exception as exc:
        _warn_once(
            f"lux_history_trim:{path}",
            "Luxtronik-Ramdisk-Historie konnte nicht begrenzt werden (%s): %s",
            path,
            exc,
        )
        return False

def maybe_trim_luxtronik_ramdisk_history(now_ts=None, path=HISTORY_FILE):
    global _last_luxtronik_history_trim_ts
    now_ts = time.time() if now_ts is None else float(now_ts)
    if now_ts - _last_luxtronik_history_trim_ts < LUXTRONIK_RAMDISK_HISTORY_TRIM_INTERVAL_S:
        return True
    _last_luxtronik_history_trim_ts = now_ts
    return trim_luxtronik_ramdisk_history(path=path)

def append_luxtronik_history(payload, now_obj=None, history_path=HISTORY_FILE):
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    archive_path = luxtronik_archive_path_for_payload(payload, now_obj)
    archive_ok = True
    archive_due, archive_bucket = _luxtronik_archive_due(
        archive_path,
        payload,
        now_obj,
    )
    if archive_due:
        archive_payload = _luxtronik_compact_archive_payload(payload)
        archive_line = (
            json.dumps(
                archive_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        try:
            _append_history_line(archive_path, archive_line)
            _last_luxtronik_archive_bucket_by_path[archive_path] = archive_bucket
        except Exception as exc:
            archive_ok = False
            _warn_once(
                f"lux_history_archive:{archive_path}",
                "Luxtronik-Archiv kann nicht geschrieben werden (%s): %s",
                archive_path,
                exc,
            )

    ramdisk_ok = True
    try:
        _append_history_line(history_path, line)
        maybe_trim_luxtronik_ramdisk_history(path=history_path)
    except Exception as exc:
        trim_luxtronik_ramdisk_history(path=history_path, force=True)
        try:
            _append_history_line(history_path, line)
            maybe_trim_luxtronik_ramdisk_history(path=history_path)
        except Exception as retry_exc:
            ramdisk_ok = False
            _warn_once(
                f"lux_history_ramdisk:{history_path}",
                "Luxtronik-Ramdisk-Historie kann nicht geschrieben werden (%s): %s; nach Begrenzung: %s",
                history_path,
                exc,
                retry_exc,
            )
    return archive_ok and ramdisk_ok

def cleanup_luxtronik_archives(
    *,
    now_ts=None,
    archive_dir=None,
    retention_days=LUXTRONIK_ARCHIVE_RETENTION_DAYS,
):
    """Entfernt ausschließlich Luxtronik-Tagesarchive außerhalb von sieben Tagen."""

    archive_dir = str(archive_dir or BACKUP_DIR)
    cutoff_ts = (
        time.time() if now_ts is None else float(now_ts)
    ) - max(1, int(retention_days)) * 86400
    removed = []
    for file_name in os.listdir(archive_dir):
        if not (
            file_name.startswith("luxtronik_")
            and file_name.endswith(".json")
        ):
            continue
        file_path = os.path.join(archive_dir, file_name)
        if (
            os.path.isfile(file_path)
            and os.path.getmtime(file_path) < cutoff_ts
        ):
            os.remove(file_path)
            removed.append(file_path)
    return removed

def write_manager_json_atomic(path, payload, mode=0o664):
    tmp_path = f"{path}.tmp.{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        try:
            os.chmod(tmp_path, mode)
        except Exception:
            pass
        os.replace(tmp_path, path)
        try:
            os.chmod(path, mode)
        except Exception:
            pass
        return True
    except Exception as exc:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        logger.debug("Manager-JSON konnte nicht geschrieben werden (%s): %s", path, exc)
        return False

def _cleanup_energy_decision_history(retention_days):
    global _energy_decision_history_state
    today = datetime.now().date().isoformat()
    if _energy_decision_history_state.get("legacy_cleanup_day") == today:
        return
    _energy_decision_history_state["legacy_cleanup_day"] = today
    cutoff = time.time() - max(1, int(retention_days or 14)) * 86400
    try:
        for name in os.listdir(LOG_DIR):
            if not name.startswith(ENERGY_DECISION_HISTORY_PREFIX):
                continue
            if not (name.endswith(".jsonl") or name.endswith(".jsonl.gz")):
                continue
            path = os.path.join(LOG_DIR, name)
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except Exception as exc:
        logger.debug("Energy-Decision Cleanup uebersprungen: %s", exc)

def energy_history_critical_events(record):
    """Verdichtet nur sicherheits- und aktorrelevante Energy-Kanten."""

    source = record if isinstance(record, dict) else {}
    decision = source.get("decision") if isinstance(source.get("decision"), dict) else {}
    heatpump = source.get("heatpump") if isinstance(source.get("heatpump"), dict) else {}
    events = []

    actions = []
    for raw_action in decision.get("actions") or []:
        if isinstance(raw_action, dict):
            if not (
                raw_action.get("confirmed") is True
                or raw_action.get("attempted") is True
                or raw_action.get("command_sent") is True
            ):
                continue
            action = {
                key: raw_action.get(key)
                for key in (
                    "action",
                    "owner",
                    "reason",
                    "confirmed",
                    "attempted",
                    "command_sent",
                    "readback_confirmed",
                    "min_runtime_s",
                )
                if raw_action.get(key) is not None
            }
        else:
            continue
        if action:
            actions.append(action)
    if actions:
        events.append({"event": "actuator_actions", "actions": actions})

    outcome = (
        heatpump.get("pv_boost_start_outcome")
        if isinstance(heatpump.get("pv_boost_start_outcome"), dict)
        else {}
    )
    if outcome and (
        outcome.get("attempted")
        or outcome.get("pending")
        or outcome.get("failed")
        or outcome.get("confirmed")
    ):
        events.append({
            "event": "actuator_outcome",
            **{
                key: outcome.get(key)
                for key in (
                    "status",
                    "attempted",
                    "pending",
                    "failed",
                    "confirmed",
                    "command_sent",
                    "readback_confirmed",
                    "reason",
                )
                if outcome.get(key) is not None
            },
        })
    if heatpump.get("actor_writes_blocked"):
        events.append({
            "event": "actuator_veto",
            "reason": str(heatpump.get("actor_write_block_reason") or "blocked"),
        })

    if heatpump.get("configured"):
        readback = {
            "event": "heatpump_readback",
            "connected": bool(heatpump.get("connected")),
            "power_known": bool(heatpump.get("power_known")),
        }
        for key in (
            "status_valid",
            "source_fresh",
            "restart_state_valid",
            "restart_actuator_state_valid",
            "dimplex_sg_readback_confirmed",
            "dimplex_sg_readback_state",
            "shelly_sg_readback_confirmed",
            "shelly_sg_readback_state",
            "shelly_pause_readback_state",
        ):
            if heatpump.get(key) is not None:
                readback[key] = heatpump.get(key)
        events.append(readback)

    stale_reasons = []
    if heatpump.get("configured") and not heatpump.get("connected"):
        stale_reasons.append("connection_missing")
    if heatpump.get("source_fresh") is False and heatpump.get("source_age_s") is not None:
        stale_reasons.append("telemetry_stale")
    if heatpump.get("status_valid") is False and heatpump.get("source_fresh") is not None:
        stale_reasons.append("status_invalid")
    if heatpump.get("restart_safe_stop_required"):
        stale_reasons.append("restart_revalidation_required")
    if stale_reasons:
        events.append({"event": "stale", "reasons": sorted(set(stale_reasons))})

    if heatpump.get("protect_block"):
        events.append({"event": "protect", "reason": "heat_source_protection"})

    minimum_runtime = {
        "predump_hold": bool(heatpump.get("predump_hold_active")),
        "price_start_blocked": bool(heatpump.get("price_takt_start_blocked")),
        "price_stop_held": bool(heatpump.get("price_takt_stop_held")),
        "ww_cycle_hold": bool(
            _safe_float(heatpump.get("ww_cycle_min_runtime_remaining_s"), 0.0) > 0.0
        ),
    }
    if any(minimum_runtime.values()):
        events.append({"event": "minimum_runtime", **minimum_runtime})

    holds = {
        "pv_pause": bool(decision.get("pv_pause_active")),
        "pre_pause": bool(decision.get("pre_pause_active")),
        "source_recovery": bool(decision.get("source_recovery_pause_latched")),
        "predump": bool(heatpump.get("predump_hold_active")),
    }
    if any(holds.values()):
        events.append({"event": "hold", **holds})
    return events


def write_energy_decision_history(record, config):
    enabled_value = get_cfg_value(config, "energy_decision_history_enable", 1)
    enabled = str(enabled_value).strip().lower()
    disabled = bool(
        enabled in ("0", "0.0", "false", "no", "off", "nein", "aus")
        or (
            isinstance(enabled_value, (int, float))
            and not isinstance(enabled_value, bool)
            and float(enabled_value) == 0.0
        )
        or enabled_value is False
    )
    if disabled:
        # Diese Datei ist die volatile IPC-Steuerfläche zum Storage Manager,
        # keine persistente Diagnosehistorie. SD-Schonung darf nur JSONL/GZIP
        # abschalten und niemals die Wärmeanforderung stale werden lassen.
        write_json_atomic_tolerant(
            ENERGY_DECISION_LATEST_PATH,
            record,
            warn_label="Energy-Decision Steuerfläche",
        )
        return
    try:
        write_history_record(
            record,
            config=config or {},
            log_dir=LOG_DIR,
            latest_path=ENERGY_DECISION_LATEST_PATH,
            prefix=ENERGY_DECISION_HISTORY_PREFIX,
            enable_key="energy_decision_history_enable",
            max_bytes_key="energy_decision_history_max_bytes",
            retention_key="energy_decision_history_retention_days",
            interval_key="energy_decision_history_interval_s",
            state=_energy_decision_history_state,
            signature_paths=(
                "decision.state",
                "decision.reason",
                "heatpump.accepting_power",
                "heatpump.targets_reached",
            ),
            critical_signature_paths=(
                "decision.critical_events",
            ),
            summary_paths=(
                "inputs.grid_w",
                "inputs.bat_w",
                "inputs.soc",
                "inputs.free_for_limbs_w",
                "inputs.heatpump_budget_w",
                "heatpump.wp_power_w",
                "heatpump.ww_ist_c",
                "heatpump.rl_ist_c",
            ),
            summary_state_path="decision.state",
            default_interval_s=HISTORY_NORMAL_HEARTBEAT_S,
            minimum_interval_s=HISTORY_NORMAL_HEARTBEAT_S,
            default_max_bytes=8 * 1024 * 1024,
            default_retention_days=2,
            logger=logger,
            config_get=get_cfg_value,
        )
    except Exception as exc:
        logger.debug("Energy-Decision History konnte nicht geschrieben werden: %s", exc)

def heatpump_boost_targets_reached(wp_data, at_mittel, heizgrenze_temp, conf_wws, conf_www, conf_hz):
    """Return True only when the active boost target is clearly satisfied."""
    data = wp_data or {}
    if _safe_float(at_mittel, 20.0) > _safe_float(heizgrenze_temp, 10.0):
        ww_target = _safe_float(conf_wws, 50.0)
        ww_ist = data.get("Warmwasser_Ist", data.get("Warmwasser-Ist"))
        return ww_ist is not None and _safe_float(ww_ist, -99.0) >= (ww_target - 0.2)
    ww_ist = data.get("Warmwasser_Ist", data.get("Warmwasser-Ist"))
    hz_ist = data.get("Ruecklauf_Ist", data.get("Ruecklauf"))
    ww_ok = ww_ist is not None and _safe_float(ww_ist, -99.0) >= (_safe_float(conf_www, 48.0) - 0.2)
    hz_ok = hz_ist is not None and _safe_float(hz_ist, -99.0) >= (_safe_float(conf_hz, 32.0) - 0.2)
    return bool(ww_ok and hz_ok)

def idm_cooling_boost_allowed(wp_type, at_mittel, cooling_min_at):
    """Gate iDM cooling boost separately from the general heating boundary."""
    try:
        if int(wp_type) != 1:
            return True
    except (TypeError, ValueError):
        return True
    return _safe_float(at_mittel, -99.0) >= _safe_float(cooling_min_at, 23.0)

def idm_cooling_diagnostics(wp_type, wp_data, external_cooling_request=False):
    """Trennt iDM-interne Kühlung von einer externen EMS-Anforderung."""
    try:
        is_idm = int(wp_type) == 1
    except (TypeError, ValueError):
        is_idm = False

    data = wp_data if isinstance(wp_data, dict) else {}
    state_text = str(data.get("Betriebszustand") or "").strip().casefold()
    cooling_active = bool(is_idm and ("kühl" in state_text or "kuehl" in state_text))
    if isinstance(external_cooling_request, bool):
        external_request = bool(is_idm and external_cooling_request)
    else:
        external_request = bool(is_idm and _safe_int(external_cooling_request, 0))
    internal_cooling = bool(cooling_active and not external_request)
    if not is_idm:
        origin = "not_idm"
    elif external_request:
        origin = "external_request"
    elif internal_cooling:
        origin = "internal_control"
    else:
        origin = "off"
    return {
        "idm_cooling_active": cooling_active,
        "idm_external_cooling_request": external_request,
        "idm_internal_cooling": internal_cooling,
        "idm_cooling_origin": origin,
    }

def heatpump_live_float(wp_data, keys, default=0.0):
    """Return the first numeric heat-pump live value, ignoring missing/null sensors."""
    data = wp_data if isinstance(wp_data, dict) else {}
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if value is None or value == "":
            continue
        return _safe_float(value, default)
    return _safe_float(default, default)

def luxtronik_ww_command_request(
    target_ww_mode,
    target_ww_temp,
    wp_status,
    wp_data,
    last_ww_mode,
    last_ww_temp,
    time_since_last_ww_cmd,
    ww_boost_owner_recent,
    cooldown_s,
    blind_heartbeat_s,
):
    """Return the WW command that should be sent, or (None, None, None).

    Live SHI status is the primary source. Identical WW timer targets are not
    resent while the heat pump already reports the desired state.
    """
    if target_ww_mode is None or ww_boost_owner_recent:
        return None, None, None

    status = wp_status or {}
    data = wp_data or {}
    send_mode = target_ww_mode
    send_temp = target_ww_temp if target_ww_mode == 1 else None

    ww_satisfied_by_temp = False
    if target_ww_mode == 1 and target_ww_temp is not None:
        ww_ist = data.get("Warmwasser_Ist", data.get("Warmwasser-Ist"))
        if ww_ist is not None:
            ww_satisfied_by_temp = _safe_float(ww_ist, -99.0) >= _safe_float(target_ww_temp, 0.0)
            if ww_satisfied_by_temp:
                send_mode = 0
                send_temp = None

    last_mode_matches = last_ww_mode == send_mode
    last_temp_matches = (
        send_mode == 0
        or (
            last_ww_temp is not None
            and send_temp is not None
            and abs(_safe_float(last_ww_temp, -999.0) - _safe_float(send_temp, 999.0)) <= 0.05
        )
    )
    last_matches = bool(last_mode_matches and last_temp_matches)

    if time_since_last_ww_cmd < cooldown_s:
        if not last_matches:
            return send_mode, send_temp, "target_changed"
        return None, None, None

    if status.get("valid"):
        live_ww_mode = status.get("WW_Mode")
        live_ww_temp = status.get("WW_Setpoint")
        if send_mode == 0:
            if live_ww_mode != 0:
                if last_ww_mode == 0 and _safe_float(time_since_last_ww_cmd, 0.0) < 1800.0:
                    return None, None, None
                return send_mode, send_temp, "target_reached" if ww_satisfied_by_temp else "live_mismatch"
            return None, None, None

        if send_mode == 1 and live_ww_mode == 1 and live_ww_temp is not None and send_temp is not None:
            if _safe_float(send_temp, 0.0) < (_safe_float(live_ww_temp, 0.0) - 0.5):
                ww_ist = data.get("Warmwasser_Ist", data.get("Warmwasser-Ist"))
                if ww_ist is not None and _safe_float(ww_ist, -99.0) < (_safe_float(live_ww_temp, 0.0) - 0.5):
                    return None, None, None

        temp_mismatch = (
            live_ww_temp is None
            or abs(_safe_float(live_ww_temp, -999.0) - _safe_float(send_temp, 999.0)) > 0.5
        )
        if live_ww_mode != send_mode or temp_mismatch:
            return send_mode, send_temp, "live_mismatch"
        return None, None, None

    if not last_matches:
        return send_mode, send_temp, "target_changed"
    if time_since_last_ww_cmd >= blind_heartbeat_s:
        return send_mode, send_temp, "blind_heartbeat"
    return None, None, None


def luxtronik_ww_target_readback_confirmed(
    target_ww_mode,
    target_ww_temp,
    wp_status=None,
):
    """Bestätigt einen bereits ohne neuen Write erreichten WW-Rückfall."""

    status = wp_status if isinstance(wp_status, dict) else {}
    if status.get("valid") is not True or target_ww_mode is None:
        return False
    live_mode = normalize_luxtronik_shi_mode(
        status.get("SHI_WW_Mode", status.get("WW_Mode"))
    )
    target_mode = normalize_luxtronik_shi_mode(target_ww_mode)
    if target_mode == 0:
        return live_mode == 0
    if target_mode != 1 or live_mode != 1 or target_ww_temp is None:
        return False
    live_temp = status.get("SHI_WW_Setpoint", status.get("WW_Setpoint"))
    return bool(
        live_temp is not None
        and abs(
            _safe_float(live_temp, -999.0)
            - _safe_float(target_ww_temp, 999.0)
        )
        <= 0.5
    )


def heatpump_ww_timer_target_allowed(
    wp_type,
    automatic_heat_start_allowed,
    positive_signal_active,
):
    """Trennt direkten Luxtronik-Zeitplan von SG-Ready-Budgetfreigaben."""

    return bool(
        _safe_int(wp_type, -1) == 0
        or automatic_heat_start_allowed
        or positive_signal_active
    )


def heatpump_start_reservation_duration_s(wp_type, shelly_sg_ip=""):
    """Gibt 150 s nur für SG-Ready-/Relaispfade zurück.

    Direkte Modbus-Sollwerte bleiben am Aktor bestehen und dürfen deshalb
    nicht die verzögerte Verdichterannahme eines SG-Ready-Kontakts erben.
    """

    normalized_shelly_ip = str(shelly_sg_ip or "").strip()
    if (
        _safe_int(wp_type, -1) in (3, 5)
        or normalized_shelly_ip not in ("", "0.0.0.0")
    ):
        return HEATPUMP_SG_READY_START_RESERVATION_MAX_S
    return HEATPUMP_DIRECT_START_RESERVATION_MAX_S


_LUXTRONIK_TIMER_BUDGET_WITHDRAWAL_REASONS = frozenset({
    "typed_demand_ended",
    "price_or_predump_stop",
    "pv_budget_deficit_stop",
    "luxtronik_ww_start_budget_expired",
})


def luxtronik_timer_hard_blocked(
    positive_output_blocked,
    block_reasons,
    *,
    force_pause=False,
):
    """Trennt Safety-/Nutzer-Vetos vom reinen Ende eines Wärmebudgets.

    Nur bekannte Budget-Rücknahmekanten dürfen den normalen Luxtronik-Timer
    weiterlaufen lassen. Unbekannte Gründe bleiben fail-closed.
    """

    if force_pause:
        return True
    if not positive_output_blocked:
        return False
    reasons = {
        str(reason or "").strip()
        for reason in (block_reasons or [])
        if str(reason or "").strip()
    }
    return bool(
        not reasons
        or not reasons.issubset(_LUXTRONIK_TIMER_BUDGET_WITHDRAWAL_REASONS)
    )


def luxtronik_ww_budget_target(
    *,
    timer_enabled,
    timer_target_c,
    boost_requested,
    authorized_heatpump_budget_w,
    boost_permission_active=False,
    boost_target_c,
    fallback_target_c,
    hard_blocked=False,
):
    """Projiziert Budget auf Luxtronik-Solltemperatur, niemals auf Leistung.

    Der Timer ist die normale, budgetunabhängige Solltemperatur. Ausschließlich
    ein bereits zentral autorisiertes positives Wattbudget darf eine neue
    Anhebung starten. Danach hält die bestätigte boolsche Aktorfreigabe den
    Sollwert unabhängig vom zyklischen Leistungsaccounting stabil.
    """

    if hard_blocked:
        return {
            "mode": 0,
            "target_c": fallback_target_c,
            "budget_boost_active": False,
            "boost_target_active": False,
            "boost_permission_active": False,
            "timer_active": False,
        }
    budget_boost_active = bool(
        boost_requested
        and type(authorized_heatpump_budget_w) is int
        and authorized_heatpump_budget_w > 0
    )
    boost_target_active = bool(
        boost_requested
        and (budget_boost_active or boost_permission_active is True)
    )
    if boost_target_active:
        return {
            "mode": 1,
            "target_c": boost_target_c,
            "budget_boost_active": budget_boost_active,
            "boost_target_active": True,
            "boost_permission_active": boost_permission_active is True,
            "timer_active": bool(timer_enabled),
        }
    if timer_enabled and timer_target_c is not None:
        return {
            "mode": 1,
            "target_c": timer_target_c,
            "budget_boost_active": False,
            "boost_target_active": False,
            "boost_permission_active": False,
            "timer_active": True,
        }
    return {
        "mode": None,
        "target_c": None,
        "budget_boost_active": False,
        "boost_target_active": False,
        "boost_permission_active": False,
        "timer_active": False,
    }


def luxtronik_direct_setpoint_permission(
    wp_type,
    central_permission,
    held_permission,
    demand_class,
    *,
    force_pause=False,
    hard_blocked=False,
):
    """Bindet den direkten 55-°C-Setpoint an Grant oder sicheren Hold.

    Der erste Modbus-Sollwert darf unmittelbar aus dem frischen boolschen
    Storage-Grant entstehen. Nach dem Write hält ausschließlich der lokale,
    safety-geprüfte Signalzustand die Freigabe; ein Prozessneustart kann ihn
    daher nicht blind wiederherstellen.
    """

    boost_demand = str(demand_class or "none").strip().casefold() in {
        "pv_surplus",
        "pre_dump",
        "market_price",
        "price",
        "ww_immediate_manual",
    }
    return bool(
        _safe_int(wp_type, -1) == 0
        and boost_demand
        and not force_pause
        and not hard_blocked
        and (
            central_permission is True
            or held_permission is True
        )
    )


def heatpump_budget_withdrawal_readback(ctx, now_ts=None):
    """Normalisiert ausschließlich frisch bestätigte SG-Normal-Readbacks.

    Ein Sollzustand, eine fehlende Rückmeldung oder eine veraltete Rückmeldung
    gilt ausdrücklich nicht als Rücknahme einer zuvor angebotenen
    Verbraucher-Budgetlease. Der Storage Manager darf damit nur Budget
    freigeben; Eigentümer des SG-/Boost-Aktors bleibt der Energy Manager.
    """

    source = ctx if isinstance(ctx, dict) else {}
    config = (
        source.get("current_config")
        if isinstance(source.get("current_config"), dict)
        else {}
    )
    current_ts = time.time() if now_ts is None else _safe_float(now_ts, 0.0)
    max_age_s = max(
        1.0,
        _safe_float(config.get("consumer_acceptance_evidence_max_age_s"), 45.0),
    )
    wp_obj = source.get("wp")
    candidates = []

    def append_candidate(provider, state, readback_ts, confirmed, normal, origin):
        timestamp = _safe_float(readback_ts, 0.0)
        fresh = bool(
            current_ts > 0.0
            and timestamp > 0.0
            and 0.0 <= current_ts - timestamp <= max_age_s
        )
        if confirmed is True and fresh:
            candidates.append({
                "provider": provider,
                "state": state,
                "ts": timestamp,
                "normal": bool(normal),
                "source": str(origin or provider),
            })

    wp_status = (
        source.get("wp_status")
        if isinstance(source.get("wp_status"), dict)
        else {}
    )
    wp_type = source.get("wp_type")
    luxtronik_hz_mode = wp_status.get("SHI_HZ_Mode")
    luxtronik_ww_mode = wp_status.get("SHI_WW_Mode")
    luxtronik_source_age_s = _safe_float(
        wp_status.get("source_age_s"),
        -1.0,
    )
    luxtronik_status_typed = bool(
        type(wp_type) is int
        and wp_type == 0
        and wp_status.get("valid") is True
        and wp_status.get("source_fresh") is True
        and type(luxtronik_hz_mode) is int
        and type(luxtronik_ww_mode) is int
        and 0.0 <= luxtronik_source_age_s <= max_age_s
    )
    if luxtronik_status_typed:
        append_candidate(
            "luxtronik",
            {
                "SHI_HZ_Mode": luxtronik_hz_mode,
                "SHI_WW_Mode": luxtronik_ww_mode,
            },
            wp_status.get("source_ts"),
            True,
            bool(
                # Luxtronik-SHI: 0 bedeutet in beiden Auftragskanälen
                # „keine Beeinflussung“. Das ist nicht die generische
                # vierstufige SG-Ready-Codierung aus Heat/policy.py.
                luxtronik_hz_mode == 0
                and luxtronik_ww_mode == 0
            ),
            "luxtronik_shi_confirmed_readback",
        )

    dimplex_state = source.get("dimplex_sg_readback_state")
    append_candidate(
        "dimplex",
        dimplex_state,
        source.get("dimplex_sg_readback_ts"),
        source.get("dimplex_sg_readback_confirmed"),
        type(dimplex_state) is int and dimplex_state == heat_policy.SG_READY_NORMAL,
        source.get("dimplex_sg_readback_source"),
    )

    shelly_state = source.get(
        "shelly_sg_readback_state",
        getattr(wp_obj, "last_live_sg_state", None) if wp_obj else None,
    )
    shelly_ts = source.get(
        "shelly_sg_readback_ts",
        getattr(wp_obj, "last_live_sg_ts", 0.0) if wp_obj else 0.0,
    )
    shelly_confirmed = source.get("shelly_sg_readback_confirmed")
    if type(shelly_confirmed) is not bool:
        shelly_confirmed = bool(type(shelly_state) is bool and _safe_float(shelly_ts, 0.0) > 0.0)
    append_candidate(
        "shelly",
        shelly_state,
        shelly_ts,
        shelly_confirmed,
        shelly_state is False,
        source.get("shelly_sg_readback_source")
        or "shelly_relay_confirmed_readback",
    )

    if not candidates:
        return {
            "schema_version": "heatpump_budget_withdrawal_readback_v1",
            "confirmed": False,
            "state_confirmed": False,
            "active": False,
            "ts": 0.0,
            "source": "",
            "provider": "none",
            "state": None,
            "reason": "no_fresh_confirmed_readback",
        }
    latest = max(candidates, key=lambda item: item["ts"])
    return {
        "schema_version": "heatpump_budget_withdrawal_readback_v1",
        "confirmed": bool(latest["normal"]),
        "state_confirmed": True,
        "active": not bool(latest["normal"]),
        "ts": float(latest["ts"]),
        "source": str(latest["source"]),
        "provider": str(latest["provider"]),
        "state": latest["state"],
        "reason": "confirmed_sg_normal" if latest["normal"] else "confirmed_non_normal_state",
    }


def build_energy_decision_record(ctx):
    """Build one Energy-R5 record with safe defaults for missing heat pumps."""
    ctx = ctx or {}
    now_obj = ctx.get("now")
    record_ts = _safe_int(ctx.get("record_ts", time.time()), int(time.time()))
    record_time = ctx.get("record_time")
    if not record_time:
        if isinstance(now_obj, datetime):
            record_time = now_obj.isoformat(timespec="seconds")
        else:
            record_time = datetime.fromtimestamp(record_ts).isoformat(timespec="seconds")

    wp_data = ctx.get("wp_data") if isinstance(ctx.get("wp_data"), dict) else {}
    wp_status = ctx.get("wp_status") if isinstance(ctx.get("wp_status"), dict) else {}
    heatpump_pause_request = ctx.get("heatpump_pause_request") if isinstance(ctx.get("heatpump_pause_request"), dict) else {}
    consumer_allocations = ctx.get("consumer_allocations") if isinstance(ctx.get("consumer_allocations"), dict) else {}
    cycle_actions = ctx.get("cycle_actions") if isinstance(ctx.get("cycle_actions"), list) else []
    local_autonomy_blocked = ctx.get("local_autonomy_blocked")
    if not isinstance(local_autonomy_blocked, list):
        local_autonomy_blocked = []

    predump_heatpump_active = bool(ctx.get("predump_heatpump_active", False))
    predump_heatpump_hold_active = bool(ctx.get("predump_heatpump_hold_active", False))
    price_boost_active = bool(ctx.get("price_boost_active", False))
    market_plan_heatpump_active = bool(
        ctx.get("market_plan_heatpump_active", ctx.get("market_heatpump_active", False))
    )
    market_plan_release = ctx.get("market_heatpump_release") if isinstance(ctx.get("market_heatpump_release"), dict) else {}
    market_plan_action = ctx.get("market_plan_action", market_plan_release.get("action"))
    market_plan_reason = ctx.get("market_plan_reason", market_plan_release.get("reason"))
    legacy_price_heatpump_active = bool(ctx.get("legacy_price_heatpump_active", False))
    pv_pause_active = bool(ctx.get("pv_pause_active", False))
    pv_pause_owner = str(ctx.get("pv_pause_owner") or "none")
    manual_heatpump_active = bool(ctx.get("manual_heatpump_active", False))
    manual_ww_boost_active_export = bool(ctx.get("manual_ww_boost_active_export", False))
    boost_active = bool(ctx.get("boost_active", False))
    source_recovery_pause_requested = bool(ctx.get("source_recovery_pause_requested", False))
    source_recovery_pause_latched = bool(ctx.get("source_recovery_pause_latched", False))
    source_recovery_pause_blocks_boost = bool(ctx.get("source_recovery_pause_blocks_boost", False))
    source_recovery_heat_budget_override = bool(ctx.get("source_recovery_heat_budget_override", False))
    predump_heatpump_targets_reached = bool(ctx.get("predump_heatpump_targets_reached", False))
    predump_heatpump_protect_block = bool(ctx.get("predump_heatpump_protect_block", False))
    heat_policy_record = ctx.get("heat_policy_export") if isinstance(ctx.get("heat_policy_export"), dict) else {}
    heatpump_budget_withdrawal = heatpump_budget_withdrawal_readback(
        ctx,
        # Für Frischeprüfungen die Producer-Zeit nicht auf ganze Sekunden
        # kürzen: Ein bestätigter Readback aus demselben Wandzeit-Sekundenfeld
        # darf dadurch nicht fälschlich als „aus der Zukunft“ verworfen werden.
        now_ts=_safe_float(ctx.get("record_ts"), time.time()),
    )
    wp_obj = ctx.get("wp")
    idm_cooling_diag = idm_cooling_diagnostics(
        ctx.get("wp_type", -1),
        wp_data,
        getattr(wp_obj, "curr_ext_khl", False) if wp_obj else False,
    )

    decision_state = "beobachtet"
    decision_reason = "Keine aktive Waermefreigabe"
    observed_wp_power_w, heatpump_power_known, heatpump_accepting_power = heatpump_power_observation(
        wp_data,
        wp_status,
    )
    heatpump_source_ts = heatpump_native_source_timestamp(wp_status)
    heatpump_source_fresh = bool(
        wp_status.get("valid") is True
        and wp_status.get("source_fresh") is True
        and heatpump_source_ts is not None
    )
    heatpump_budget_readiness = heatpump_budget_request_readiness(ctx)
    if predump_heatpump_active:
        decision_state = "predump_waerme_hold" if predump_heatpump_hold_active else "predump_waerme_start"
        decision_reason = "Pre-Dump gibt Waermepumpe frei; Mindestlaufzeit schuetzt vor kurzem Takten"
    elif price_boost_active:
        decision_state = "preis_waerme_aktiv" if heatpump_accepting_power else "preis_waerme_budget_frei"
        if market_plan_heatpump_active:
            decision_reason = (
                "Marktvertrag aktiv; Wärmepumpe nimmt Leistung an"
                if heatpump_accepting_power
                else (
                    "Marktvertrag bietet Wärmebudget an; Wärmepumpe nimmt aktuell keine Leistung auf"
                    if heatpump_power_known
                    else "Marktvertrag bietet Wärmebudget an; Leistungsaufnahme wird für diesen Freigabepfad nicht gemessen"
                )
            )
        else:
            decision_reason = (
                "Preis-/Tariffenster aktiv; Wärmepumpe nimmt Leistung an"
                if heatpump_accepting_power
                else (
                    "Preis-/Tariffenster bietet Wärmebudget an; Wärmepumpe nimmt aktuell keine Leistung auf"
                    if heatpump_power_known
                    else "Preis-/Tariffenster bietet Wärmebudget an; Leistungsaufnahme wird für diesen Freigabepfad nicht gemessen"
                )
            )
    elif pv_pause_active:
        decision_state = "waerme_pause"
        decision_reason = (
            str(heatpump_pause_request.get("reason") or "Quell-Erholung aktiv; Waermepumpe wird wegen Preis/PV-Strategie gehalten")
            if source_recovery_pause_requested or pv_pause_owner == "source_recovery_heatpump"
            else "Quell-Erholung aktiv; Waermepumpe wird wegen Preis/PV-Strategie gehalten"
        )
    elif manual_heatpump_active or manual_ww_boost_active_export:
        decision_state = "manuelle_waermefreigabe"
        decision_reason = "Manuelle Waermefreigabe aktiv"
    elif boost_active:
        decision_state = "pv_waerme_aktiv" if heatpump_accepting_power else "pv_waerme_budget_frei"
        decision_reason = (
            "PV-/Budget-Freigabe aktiv; Waermepumpe nimmt Leistung an"
            if heatpump_accepting_power
            else (
                "PV-/Budget-Freigabe angeboten; Wärmepumpe nimmt aktuell keine Leistung auf"
                if heatpump_power_known
                else "PV-/Budget-Freigabe aktiv; SG-Ready-/Freigabekontakt gesetzt, Leistungsaufnahme wird nicht gemessen"
            )
        )
    elif predump_heatpump_targets_reached:
        decision_state = "zieltemperatur_erreicht"
        decision_reason = "Pre-Dump-Waermefreigabe blockiert: Zieltemperaturen erreicht"
    elif predump_heatpump_protect_block:
        decision_state = "wq_schutz"
        decision_reason = "Waermefreigabe blockiert: Waermequelle zu kalt"

    predump_heatpump_hold_until = _safe_float(ctx.get("predump_heatpump_hold_until", 0.0), 0.0)
    heatpump_budget_w = ctx.get("heatpump_budget_w")
    PREDUMP_HEATPUMP_MIN_RUNTIME_MIN = _safe_float(ctx.get("PREDUMP_HEATPUMP_MIN_RUNTIME_MIN", 60.0), 60.0)

    record = {
        "ts": record_ts,
        "time": record_time,
        "service": "energy_manager",
        "decision": {
            "state": decision_state,
            "reason": decision_reason,
            "price_action": ctx.get("price_action", "NONE"),
            "boost_active": bool(boost_active),
            "price_boost_active": bool(price_boost_active),
            "market_plan_heatpump_active": bool(market_plan_heatpump_active),
            "market_plan_action": market_plan_action,
            "market_plan_reason": market_plan_reason,
            "legacy_price_heatpump_active": bool(legacy_price_heatpump_active),
            "pv_pause_active": bool(pv_pause_active),
            "pv_pause_owner": pv_pause_owner,
            "pre_pause_active": bool(ctx.get("pre_pause_active", False)),
            "heatpump_boost_owner": ctx.get("heatpump_boost_owner", "none"),
            "source_recovery_pause_requested": bool(source_recovery_pause_requested),
            "source_recovery_pause_allowed": bool(ctx.get("source_recovery_pause_allowed", False)),
            "source_recovery_pause_latched": bool(source_recovery_pause_latched),
            "source_recovery_pause_blocks_boost": bool(source_recovery_pause_blocks_boost),
            "source_recovery_heat_budget_override": bool(source_recovery_heat_budget_override),
            "source_recovery_release_reason": ctx.get("source_recovery_release_reason", ""),
            "source_recovery_history_allowed": bool(ctx.get("source_recovery_history_allowed", False)),
            "source_recovery_history_reason": ctx.get("source_recovery_history_reason", ""),
            "source_recovery_compressor_off_before_s": ctx.get("source_recovery_compressor_off_before_s"),
            "source_recovery_planned_pause_s": _safe_float(ctx.get("source_recovery_planned_pause_s", 0.0), 0.0),
            "storage_manager_owns_energy": bool(ctx.get("storage_manager_owns_energy", True)),
            "energy_autonomy_allowed": bool(ctx.get("energy_autonomy_allowed", False)),
            "local_autonomy_blocked": local_autonomy_blocked,
            "actions": cycle_actions[-8:],
            "heat_policy": {
                "target_state": heat_policy_record.get("target_state"),
                "sg_ready_state": heat_policy_record.get("sg_ready_state"),
                "available_budget_w": heat_policy_record.get("available_budget_w"),
                "block_reason": heat_policy_record.get("block_reason"),
                "owner": heat_policy_record.get("owner"),
                "runtime_enabled": heat_policy_record.get("runtime_enabled"),
            },
        },
        "inputs": {
            "grid_w": _safe_int(ctx.get("grid", 0)),
            "bat_w": _safe_int(ctx.get("bat", 0)),
            "soc": _safe_float(ctx.get("soc", 0.0)),
            "current_price_ct": _safe_float(ctx.get("current_price", 99.9), 99.9),
            "storage_state": ctx.get("storage_state_name", ""),
            "free_for_limbs_w": _safe_int(ctx.get("free_for_limbs_w", 0)),
            "heatpump_budget_w": _safe_int(heatpump_budget_w, 0) if heatpump_budget_w is not None else None,
            "consumer_allocations": consumer_allocations,
            "heatpump_pause_request": heatpump_pause_request,
        },
        "heatpump": {
            "configured": bool(ctx.get("wp")),
            "connected": bool(ctx.get("wp_connected", False)),
            "type": _safe_int(ctx.get("wp_type", -1), -1),
            "wp_power_w": observed_wp_power_w,
            "power_known": bool(heatpump_power_known),
            "accepting_power": bool(heatpump_accepting_power),
            "source_ts": heatpump_source_ts,
            "budget_start_ready": bool(heatpump_budget_readiness.get("ready")),
            "budget_start_request_w": _safe_int(
                heatpump_budget_readiness.get("request_w"),
                0,
            ),
            "budget_start_demand_class": str(
                heatpump_budget_readiness.get("demand_class") or "none"
            ),
            "budget_start_demand_target_c": heatpump_budget_readiness.get(
                "demand_target_c"
            ),
            "budget_start_blockers": list(
                heatpump_budget_readiness.get("blockers") or []
            ),
            "central_start_budget_gate": copy.deepcopy(
                ctx.get("central_heatpump_start_budget_gate")
                if isinstance(ctx.get("central_heatpump_start_budget_gate"), dict)
                else {}
            ),
            "positive_signal_window": copy.deepcopy(
                ctx.get("heatpump_positive_signal_window")
                if isinstance(ctx.get("heatpump_positive_signal_window"), dict)
                else {}
            ),
            "positive_signal_restored_unconfirmed": bool(
                ctx.get("heatpump_positive_signal_restored_unconfirmed")
            ),
            "positive_signal_restart_readback": copy.deepcopy(
                ctx.get("heatpump_positive_signal_restart_readback")
                if isinstance(
                    ctx.get("heatpump_positive_signal_restart_readback"),
                    dict,
                )
                else {}
            ),
            "budget_offered": bool(
                boost_active
                or price_boost_active
                or predump_heatpump_active
                or (
                    isinstance(ctx.get("heatpump_positive_signal_window"), dict)
                    and ctx.get("heatpump_positive_signal_window", {}).get(
                        "active"
                    )
                    is True
                )
            ),
            "budget_signal_active_confirmed": bool(
                heatpump_budget_withdrawal.get("active") is True
                and heatpump_budget_withdrawal.get("state_confirmed") is True
            ),
            "budget_signal_readback_ts": _safe_float(
                heatpump_budget_withdrawal.get("ts"),
                0.0,
            ),
            "budget_signal_readback_source": str(
                heatpump_budget_withdrawal.get("source") or ""
            ),
            "budget_signal_readback_state": heatpump_budget_withdrawal.get(
                "state"
            ),
            "budget_withdrawal_confirmed": bool(
                heatpump_budget_withdrawal.get("confirmed") is True
            ),
            "budget_withdrawal_ts": _safe_float(
                heatpump_budget_withdrawal.get("ts"),
                0.0,
            ),
            "budget_withdrawal_source": str(
                heatpump_budget_withdrawal.get("source") or ""
            ),
            "budget_withdrawal_reason": str(
                heatpump_budget_withdrawal.get("reason")
                or "no_fresh_confirmed_readback"
            ),
            "at_c": _safe_float(ctx.get("at", 0.0), 0.0),
            "at_mittel_c": _safe_float(ctx.get("at_mittel", ctx.get("at", 0.0)), 0.0),
            "wq_aus_c": _safe_float(ctx.get("wq_aus", 0.0), 0.0),
            "hz_mode": wp_status.get("HZ_Mode"),
            "ww_mode": wp_status.get("WW_Mode"),
            "compressor_running": bool(ctx.get("wp_compressor_running_now", False)),
            "compressor_observation_valid": bool(ctx.get("wp_compressor_observation_valid", False)),
            "compressor_history_valid": bool(ctx.get("wp_compressor_history_valid", False)),
            "compressor_last_start_ts": _safe_float(ctx.get("wp_compressor_last_start_ts", 0.0), 0.0),
            "compressor_last_stop_ts": _safe_float(ctx.get("wp_compressor_last_stop_ts", 0.0), 0.0),
            "compressor_last_run_s": _safe_float(ctx.get("wp_compressor_last_run_s", 0.0), 0.0),
            "ww_ist_c": _safe_float(wp_data.get("Warmwasser_Ist"), 0.0) if wp_data.get("Warmwasser_Ist") is not None else None,
            "rl_ist_c": _safe_float(wp_data.get("Ruecklauf_Ist"), 0.0) if wp_data.get("Ruecklauf_Ist") is not None else None,
            "predump_raw_active": bool(ctx.get("predump_heatpump_raw_active", False)),
            "predump_active": bool(predump_heatpump_active),
            "predump_hold_active": bool(predump_heatpump_hold_active),
            "predump_hold_until": int(predump_heatpump_hold_until or 0),
            "predump_hold_remaining_s": max(0, int(predump_heatpump_hold_until - time.time())) if predump_heatpump_hold_until else 0,
            "predump_min_runtime_s": int(PREDUMP_HEATPUMP_MIN_RUNTIME_MIN * 60),
            "targets_reached": bool(predump_heatpump_targets_reached),
            "protect_block": bool(predump_heatpump_protect_block),
            "market_plan_active": bool(market_plan_heatpump_active),
            "market_plan_action": market_plan_action,
            "market_plan_reason": market_plan_reason,
            "price_takt_start_blocked": bool(ctx.get("price_heatpump_takt_start_blocked", False)),
            "price_takt_stop_held": bool(ctx.get("price_heatpump_takt_stop_held", False)),
            "ww_cycle_min_runtime_remaining_s": max(
                0.0,
                _safe_float(ctx.get("ww_cycle_min_runtime_remaining_s", 0.0), 0.0),
            ),
            "status_valid": (
                bool(wp_status.get("valid")) if "valid" in wp_status else None
            ),
            "source_fresh": (
                heatpump_source_fresh if "source_fresh" in wp_status else None
            ),
            "source_age_s": wp_status.get("source_age_s"),
            "restart_state_valid": (
                bool((ctx.get("restart_revalidation") or {}).get("state_fresh"))
                if isinstance(ctx.get("restart_revalidation"), dict)
                and "state_fresh" in ctx.get("restart_revalidation")
                else None
            ),
            "restart_actuator_state_valid": (
                bool((ctx.get("restart_revalidation") or {}).get("actuator_state_valid"))
                if isinstance(ctx.get("restart_revalidation"), dict)
                and "actuator_state_valid" in ctx.get("restart_revalidation")
                else None
            ),
            "restart_safe_stop_required": bool(
                (ctx.get("restart_revalidation") or {}).get("safe_stop_required", False)
            ) if isinstance(ctx.get("restart_revalidation"), dict) else False,
            "actor_writes_blocked": bool(getattr(wp_obj, "actor_writes_blocked", False)) if wp_obj else False,
            "actor_write_block_reason": str(getattr(wp_obj, "actor_write_block_reason", "") or "") if wp_obj else "",
            "pv_boost_start_outcome": dict(ctx.get("pv_boost_last_outcome") or {}),
            "dimplex_sg_readback_state": ctx.get("dimplex_sg_readback_state"),
            "dimplex_sg_readback_ts": _safe_float(
                ctx.get("dimplex_sg_readback_ts"),
                0.0,
            ),
            "dimplex_sg_readback_source": str(
                ctx.get("dimplex_sg_readback_source") or ""
            ),
            "dimplex_sg_readback_confirmed": (
                bool(ctx.get("dimplex_sg_readback_confirmed"))
                if "dimplex_sg_readback_confirmed" in ctx
                else None
            ),
            "shelly_sg_readback_state": getattr(wp_obj, "last_live_sg_state", None) if wp_obj else None,
            "shelly_sg_readback_ts": _safe_float(
                ctx.get(
                    "shelly_sg_readback_ts",
                    getattr(wp_obj, "last_live_sg_ts", 0.0) if wp_obj else 0.0,
                ),
                0.0,
            ),
            "shelly_sg_readback_source": str(
                ctx.get("shelly_sg_readback_source")
                or ("shelly_relay_confirmed_readback" if wp_obj else "")
            ),
            "shelly_sg_readback_confirmed": (
                isinstance(getattr(wp_obj, "last_live_sg_state", None), bool)
                and _safe_float(getattr(wp_obj, "last_live_sg_ts", 0.0), 0.0) > 0.0
            ) if wp_obj else None,
            "shelly_pause_readback_state": getattr(wp_obj, "last_live_pause_state", None) if wp_obj else None,
            **idm_cooling_diag,
        },
        "wallbox_context": {
            "wb1_locked": bool(ctx.get("wb1_locked", False)),
            "wb2_locked": bool(ctx.get("wb2_locked", False)),
            "car_blocks_boost": bool(ctx.get("car_blocks_boost", False)),
            "car_blocks_boost_applied": bool(ctx.get("car_blocks_boost_applied", False)),
            "car_blocks_pause": bool(ctx.get("car_blocks_pause", False)),
        },
    }
    record["decision"]["critical_events"] = energy_history_critical_events(record)
    return record

def source_recovery_history_gate(
    heatpump_pause_request,
    *,
    pause_active,
    compressor_running,
    compressor_history_valid,
    compressor_last_stop_ts,
    now_ts=None,
):
    """Verhindert eine Quell-Erholung ohne vorherige reale Quellenbelastung."""
    request = heatpump_pause_request if isinstance(heatpump_pause_request, dict) else {}
    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    planned_pause_s = max(
        0.0,
        _safe_float(
            request.get("planned_pause_s"),
            request.get("min_runtime_s", 15.0 * 60.0),
        ),
    )
    last_stop = _safe_float(compressor_last_stop_ts, 0.0)
    off_before_s = max(0.0, now_value - last_stop) if last_stop > 0.0 else None

    if pause_active:
        return {
            "allowed": True,
            "reason": "Laufende Quell-Erholung bleibt bis zu ihrem Endzustand verriegelt",
            "planned_pause_s": planned_pause_s,
            "compressor_off_before_s": off_before_s,
        }
    if not request.get("active") or request.get("owner") != "source_recovery_heatpump":
        return {
            "allowed": False,
            "reason": "Keine aktive Quell-Erholungsanforderung",
            "planned_pause_s": planned_pause_s,
            "compressor_off_before_s": off_before_s,
        }
    if compressor_running:
        return {
            "allowed": False,
            "reason": "Verdichter läuft; Quell-Erholung darf keinen laufenden Zyklus unterbrechen",
            "planned_pause_s": planned_pause_s,
            "compressor_off_before_s": 0.0,
        }
    if not compressor_history_valid or off_before_s is None:
        return {
            "allowed": False,
            "reason": "Verdichterhistorie unbekannt; Quell-Erholung bleibt sicher gesperrt",
            "planned_pause_s": planned_pause_s,
            "compressor_off_before_s": off_before_s,
        }
    if planned_pause_s <= 0.0 or off_before_s >= planned_pause_s:
        return {
            "allowed": False,
            "reason": "Quelle bereits erholt: Verdichter war vor Pausenstart lange genug aus",
            "planned_pause_s": planned_pause_s,
            "compressor_off_before_s": off_before_s,
        }
    return {
        "allowed": True,
        "reason": "Vorherige Verdichterlast rechtfertigt Quell-Erholung",
        "planned_pause_s": planned_pause_s,
        "compressor_off_before_s": off_before_s,
    }


def source_recovery_pause_latch(heatpump_pause_request, pause_active, context, current_price, now_ts=None, grace_s=120.0):
    """Hält eine gestartete Quell-Erholung bis zum geplanten PV-Zeitpunkt stabil."""
    now_ts = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    request = heatpump_pause_request if isinstance(heatpump_pause_request, dict) else {}
    cached = context if isinstance(context, dict) else {}
    owner_is_source_recovery = request.get("owner") == "source_recovery_heatpump"
    expires_ts = _safe_float(request.get("expires_ts", 0), 0.0)
    price_ok = _safe_float(current_price, 0.0) > 0
    requested_now = bool(
        request.get("active")
        and owner_is_source_recovery
        and expires_ts >= (now_ts - 5)
        and price_ok
    )
    grace_s = max(0.0, _safe_float(grace_s, 0.0))
    last_seen_ts = _safe_float(cached.get("_last_seen_ts", 0.0), 0.0)
    short_gap_fresh = bool(
        pause_active
        and cached
        and price_ok
        and last_seen_ts > 0
        and (now_ts - last_seen_ts) <= grace_s
    )

    started_ts = _safe_float(cached.get("_started_ts", cached.get("ts", 0.0)), 0.0)
    pause_until_ts = _safe_float(cached.get("pause_until_ts", 0.0), 0.0)
    timeout_s = max(0.0, _safe_float(cached.get("timeout_s", 0.0), 0.0))
    timeout_deadline = started_ts + timeout_s if started_ts > 0.0 and timeout_s > 0.0 else 0.0
    deadlines = [value for value in (pause_until_ts, timeout_deadline) if value > 0.0]
    hold_until_ts = min(deadlines) if deadlines else 0.0
    planned_hold_fresh = bool(
        pause_active
        and cached
        and price_ok
        and hold_until_ts > now_ts
    )
    cached_fresh = bool(short_gap_fresh or planned_hold_fresh)

    if requested_now:
        next_context = dict(request)
        next_context["_last_seen_ts"] = now_ts
        next_context["_started_ts"] = _safe_float(cached.get("_started_ts", now_ts), now_ts)
        active_request = dict(request)
    elif cached_fresh:
        next_context = dict(cached)
        active_request = {key: value for key, value in cached.items() if not str(key).startswith("_")}
    else:
        next_context = {}
        active_request = dict(request)

    return {
        "allowed": bool(requested_now or cached_fresh),
        "requested": requested_now,
        "seen": bool(owner_is_source_recovery),
        "cached_fresh": cached_fresh,
        "planned_hold_fresh": planned_hold_fresh,
        "hold_until_ts": hold_until_ts,
        "request": active_request,
        "context": next_context,
    }

def source_recovery_heat_override_state(
    *,
    source_recovery_pause_allowed,
    source_recovery_pause_active,
    free_for_limbs_w,
    grid_start_limit,
    storage_manager_owns_energy,
    soc,
    min_soc,
    heat_policy_decision=None,
    ww_cycle_started_ts=0.0,
    now_ts=None,
):
    """Entscheidet, ob nutzbares Wärmebudget eine Quellen-Erholungspause beendet."""

    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    relevant = bool(source_recovery_pause_allowed or source_recovery_pause_active)
    budget_ready = heatpump_budget_allows_start(
        storage_manager_owns_energy,
        free_for_limbs_w,
        grid_start_limit,
        soc,
        min_soc,
    )
    started = _safe_float(ww_cycle_started_ts, 0.0)
    ww_remaining_s = 0.0
    if started > 0.0:
        ww_remaining_s = max(
            0.0,
            heat_policy.WW_CYCLE_MIN_RUNTIME_S - max(0.0, now_value - started),
        )

    target_state = getattr(heat_policy_decision, "target_state", None)
    owner = getattr(heat_policy_decision, "owner", None)
    policy_pv_budget = target_state == heat_policy.TARGET_PV_SURPLUS and budget_ready
    policy_forced_heat = target_state in (heat_policy.TARGET_BOOST, heat_policy.TARGET_PRE_DUMP)
    policy_ww_hold = target_state == heat_policy.TARGET_PROTECTED and owner == "ww_cycle"
    policy_allows_heat = bool(policy_pv_budget or policy_forced_heat or policy_ww_hold)

    override = False
    release_reason = ""
    if relevant:
        if ww_remaining_s > 0.0 or policy_ww_hold:
            override = True
            release_reason = "WW-Mindestlaufzeit hat Vorrang vor Quell-Erholung"
        elif policy_forced_heat:
            override = True
            release_reason = "Heat Policy fordert Preis-/Pre-Dump-Wärmefreigabe"
        elif policy_pv_budget:
            override = True
            release_reason = "PV-Wärmebudget ist durch die Heat Policy freigegeben"
        elif heat_policy_decision is None and budget_ready:
            override = True
            release_reason = "PV-Wärmebudget ist vor Policy-Bewertung verfügbar"

    return {
        "active_or_allowed": relevant,
        "budget_ready": bool(budget_ready),
        "policy_allows_heat": policy_allows_heat,
        "override": bool(override),
        "blocks_boost": bool(relevant and not override),
        "release_reason": release_reason,
        "ww_cycle_min_runtime_remaining_s": ww_remaining_s,
    }

def source_recovery_release_cooldown_s(heatpump_pause_request, pv_boost_delay_s, ww_remaining_s=0.0):
    """Liefert nach der Quellen-Erholung eine begrenzte Wiedereinschaltsperre."""

    request = heatpump_pause_request if isinstance(heatpump_pause_request, dict) else {}
    requested_min_s = _safe_float(request.get("min_runtime_s", 0.0), 0.0)
    base_s = max(
        _safe_float(pv_boost_delay_s, 30.0) * 2.0,
        requested_min_s,
        heat_policy.WW_CYCLE_MIN_RUNTIME_S,
        _safe_float(ww_remaining_s, 0.0),
    )
    return max(60.0, min(3600.0, base_s))


def release_heatpump_pause(wp, wp_type, pause_owner, ww_temp):
    """Löst eine Luxtronik-Quell-Erholung ohne Eingriff in den WW-Kanal."""
    if wp is None:
        return False
    if pause_owner == "source_recovery_heatpump" and _safe_int(wp_type, -1) == 0:
        return bool(wp.write_hz_boost(0, None))
    return bool(wp.set_boost(0, None, 0, ww_temp))

def _heat_config_value(config, keys, default=None):
    for key in keys:
        value = get_cfg_value(config, key, None)
        if value is not None and value != "":
            return value
    return default

def _heat_policy_temperature_context(config, wp_data, at_mittel, heizgrenze_temp, conf_wws, conf_hz):
    data = wp_data if isinstance(wp_data, dict) else {}
    summer_mode = _safe_float(at_mittel, 20.0) > _safe_float(heizgrenze_temp, 10.0)
    if summer_mode:
        raw_temp = data.get("Warmwasser_Ist", data.get("Warmwasser-Ist"))
        min_temp = _heat_config_value(config, ("heat_policy_ww_min_c",), conf_wws)
        max_temp = _heat_config_value(config, ("heat_policy_ww_max_c",), _safe_float(conf_wws, 50.0) + 3.0)
    else:
        raw_temp = data.get("Ruecklauf_Ist", data.get("Rücklauf", data.get("Vorlauf_Ist")))
        min_temp = _heat_config_value(config, ("heat_policy_hz_min_c",), conf_hz)
        max_temp = _heat_config_value(config, ("heat_policy_hz_max_c",), _safe_float(conf_hz, 32.0) + 3.0)
    if raw_temp is None or raw_temp == "":
        return {
            "temperature_valid": False,
            "temperature_c": None,
            "temperature_min_c": _safe_float(min_temp, 0.0),
            "temperature_max_c": _safe_float(max_temp, 0.0),
        }
    return {
        "temperature_valid": True,
        "temperature_c": _safe_float(raw_temp, 0.0),
        "temperature_min_c": _safe_float(min_temp, 0.0),
        "temperature_max_c": _safe_float(max_temp, 0.0),
    }

def _heat_policy_user_fallback_kwh(config):
    value = _heat_config_value(
        config,
        (
            "heat_wp_daily_kwh",
            "wp_forecast_daily_kwh",
            "wp_daily_need_kwh",
            "wp_energy_need_kwh",
        ),
        None,
    )
    if value is None:
        return None
    return max(0.0, _safe_float(value, 0.0))

def _market_release_end_ts_s(release):
    contract = release.get("contract") if isinstance(release, dict) else None
    if not isinstance(contract, dict):
        return None
    end_ms = _safe_float(contract.get("end_ts"), 0.0)
    return end_ms / 1000.0 if end_ms > 0 else None

def luxtronik_ww_runtime_state(wp_status=None, wp_data=None):
    """Klassifiziert einen Verdichterlauf domänenscharf als Warmwasser.

    SHI-WW-Modus und Sollwert sind nur unser Auftrag. Sie dürfen weder einen
    Heiztakt noch einen stehenden Verdichter in einen laufenden WW-Zyklus
    umdeuten. Als WW-Evidenz gelten ausschließlich der laufende Verdichter plus
    dokumentierter Betriebs-/WW-Status oder die frische vorhandene BUP-Kante.
    Bei widersprüchlicher Heiz- und WW-Evidenz gewinnt der WW-Schutz; der
    Konflikt bleibt als Diagnosegrund sichtbar und wird nicht blind beendet.
    """

    status = wp_status if isinstance(wp_status, dict) else {}
    data = wp_data if isinstance(wp_data, dict) else {}
    compressor_running = heatpump_compressor_running(data, status)
    if not compressor_running:
        return {
            "state": "not_running",
            "ww_running": False,
            "compressor_running": False,
            "reason": "compressor_off",
        }

    operating_raw = data.get("Betriebsart", status.get("Betriebsart"))
    ww_status_raw = data.get(
        "Status_Warmwasser",
        status.get("Status_Warmwasser"),
    )
    heating_status_raw = data.get(
        "Status_Heizen",
        status.get("Status_Heizen"),
    )
    operating_mode = (
        _safe_int(operating_raw, -1)
        if operating_raw is not None
        else None
    )
    ww_status = (
        _safe_int(ww_status_raw, -1)
        if ww_status_raw is not None
        else None
    )
    heating_status = (
        _safe_int(heating_status_raw, -1)
        if heating_status_raw is not None
        else None
    )
    bup_raw = data.get("BUP", status.get("BUP"))
    bup_fresh = bool(status.get("source_fresh") is True)

    ww_evidence = []
    other_domain_evidence = []
    if operating_mode == 1:
        ww_evidence.append("operating_mode_ww")
    elif operating_mode is not None and operating_mode >= 0:
        other_domain_evidence.append("operating_mode_other")
    if ww_status == 3:
        ww_evidence.append("warmwater_status_active")
    if heating_status == 3:
        other_domain_evidence.append("heating_status_active")
    if bup_fresh and _safe_int(bup_raw, 0) > 0:
        ww_evidence.append("bup_active")

    if ww_evidence:
        return {
            "state": "ww_running",
            "ww_running": True,
            "compressor_running": True,
            "reason": (
                "ww_running_with_conflict"
                if other_domain_evidence
                else ww_evidence[0]
            ),
            "ww_evidence": ww_evidence,
            "other_domain_evidence": other_domain_evidence,
        }
    if other_domain_evidence:
        return {
            "state": "other_domain",
            "ww_running": False,
            "compressor_running": True,
            "reason": other_domain_evidence[0],
            "ww_evidence": [],
            "other_domain_evidence": other_domain_evidence,
        }
    return {
        "state": "unknown",
        "ww_running": False,
        "compressor_running": True,
        "reason": "compressor_domain_unbound",
        "ww_evidence": [],
        "other_domain_evidence": [],
    }


def heatpump_ww_cycle_running(wp_status=None, wp_data=None, ww_requested=False):
    """Kompatibler herstellerübergreifender WW-Laufvertrag.

    Luxtronik-spezifische Entscheidungen verwenden zusätzlich
    ``luxtronik_ww_runtime_state``. Andere Treiber besitzen nicht zwingend die
    Luxtronik-Domänenfelder und behalten deshalb ihre bisherige Kombination
    aus WW-Anforderung und physischer Verdichterevidenz.
    """

    status = wp_status if isinstance(wp_status, dict) else {}
    data = wp_data if isinstance(wp_data, dict) else {}
    physical_ww_status = data.get(
        "Status_Warmwasser",
        status.get("Status_Warmwasser"),
    )
    if (
        physical_ww_status is not None
        and _safe_int(physical_ww_status, -1) == 3
    ):
        return True

    operating_mode = data.get("Betriebsart", status.get("Betriebsart"))
    compressor_running = heatpump_compressor_running(data, status)
    if (
        operating_mode is not None
        and _safe_int(operating_mode, -1) == 1
        and compressor_running
    ):
        return True
    if not ww_requested:
        return False
    ww_mode = normalize_luxtronik_shi_mode(
        status.get(
            "SHI_WW_Mode",
            status.get(
                "WW_Mode",
                data.get("WW_Mode", data.get("Modus Warmw.")),
            ),
        )
    )
    if ww_mode != 1:
        return False
    if compressor_running:
        return True
    for key in ("Verdichter", "Verdichter_Ein", "BUP", "SLP"):
        if _safe_int(data.get(key, status.get(key)), 0) > 0:
            return True
    return False


def luxtronik_ww_fresh_start_budget_allowed(
    demand_active,
    command_cap_w,
    required_start_w,
    boost_permission_active,
    start_budget_gate=None,
):
    """Akzeptiert nur ein zur aktuellen Demand-Generation gehörendes Budget."""

    gate = start_budget_gate if isinstance(start_budget_gate, dict) else {}
    return bool(
        demand_active
        and gate.get("allowed") is True
        and _safe_int(command_cap_w, 0) >= max(
            1,
            _safe_int(required_start_w, 1),
        )
        and boost_permission_active is True
    )


def luxtronik_ww_budget_loss_guard(
    state=None,
    *,
    demand_active,
    signal_active,
    fresh_start_budget,
    runtime_state,
    duration_s,
    signal_release_allowed,
    clock_sample=None,
):
    """Begrenzt eine nicht angenommene direkte WW-Startfreigabe.

    Die globale 600-s-Signalhaltezeit bleibt unangetastet. Erst nach Ablauf
    dieser Hardwarekante *und* der konfigurierten Defizitfrist wird der
    WW-Sollwert blockiert. Ein echter WW-Lauf bleibt geschützt; ein Heiztakt
    darf die WW-Lease dagegen nicht verlängern. Nach Ablauf kann ausschließlich
    ein neues frisches startfähiges Command-Budget die Lease wieder öffnen.
    """

    source = copy.deepcopy(state) if isinstance(state, dict) else {}
    runtime = str(runtime_state or "unknown").strip().casefold()
    current_sample = (
        copy.deepcopy(clock_sample)
        if isinstance(clock_sample, dict)
        else control_time.sample()
    )
    duration = max(1.0, _safe_float(duration_s, 600.0))

    if not demand_active:
        return {
            "blocked": False,
            "effective_block": False,
            "timer_guard": {},
            "runtime_state": runtime,
            "reason": "demand_ended",
            "evidence_limit": False,
        }
    if fresh_start_budget:
        return {
            "blocked": False,
            "effective_block": False,
            "timer_guard": {},
            "runtime_state": runtime,
            "reason": "fresh_start_budget",
            "evidence_limit": False,
        }
    if source.get("blocked") is True:
        return {
            "blocked": True,
            "effective_block": bool(
                signal_release_allowed
                and runtime in ("not_running", "other_domain")
            ),
            "timer_guard": {},
            "runtime_state": runtime,
            "reason": (
                "latched_but_physical_ww_protected"
                if runtime == "ww_running"
                else str(source.get("reason") or "budget_loss_latched")
            ),
            "evidence_limit": runtime == "unknown",
        }
    if runtime == "ww_running":
        return {
            "blocked": False,
            "effective_block": False,
            "timer_guard": {},
            "runtime_state": runtime,
            "reason": "physical_ww_cycle_running",
            "evidence_limit": False,
        }
    if runtime == "unknown":
        return {
            "blocked": False,
            "effective_block": False,
            "timer_guard": copy.deepcopy(source.get("timer_guard") or {}),
            "runtime_state": runtime,
            "reason": "ww_runtime_evidence_limit",
            "evidence_limit": True,
        }
    if not signal_active:
        return {
            "blocked": False,
            "effective_block": False,
            "timer_guard": {},
            "runtime_state": runtime,
            "reason": "waiting_for_positive_signal",
            "evidence_limit": False,
        }

    previous_guard = source.get("timer_guard")
    if isinstance(previous_guard, dict) and previous_guard:
        timer_guard = control_time.evaluate_guard(
            previous_guard,
            current_sample,
            minimum_s=duration,
        )
    else:
        timer_guard = control_time.begin_guard(
            duration,
            current_sample,
            minimum_s=duration,
            epoch_mode=control_time.EPOCH_MODE_SAME_BOOT_MONOTONIC,
        )
    blocked = bool(
        timer_guard.get("active") is not True
        or timer_guard.get("fail_closed") is True
    )
    return {
        "blocked": blocked,
        "effective_block": bool(blocked and signal_release_allowed),
        "timer_guard": {} if blocked else copy.deepcopy(timer_guard),
        "runtime_state": runtime,
        "reason": (
            "budget_loss_expired"
            if blocked
            else "budget_loss_grace_active"
        ),
        "evidence_limit": bool(timer_guard.get("fail_closed") is True),
    }

def heatpump_ww_temperature_below_target(wp_status=None, wp_data=None, target_temp_c=None, fallback_target_c=0.0):
    """Return (below_target, actual, target) for warm-water stop protection."""

    status = wp_status if isinstance(wp_status, dict) else {}
    data = wp_data if isinstance(wp_data, dict) else {}
    actual_raw = data.get("Warmwasser_Ist", data.get("Warmwasser-Ist"))
    if actual_raw is None:
        return False, None, None
    target_raw = target_temp_c
    if target_raw is None:
        target_raw = status.get("WW_Setpoint", data.get("Warmwasser_Soll", data.get("Warmwasser-Soll", fallback_target_c)))
    actual = _safe_float(actual_raw, -999.0)
    target = _safe_float(target_raw, _safe_float(fallback_target_c, 0.0))
    if actual < -100.0 or target <= 0.0:
        return False, actual, target
    return actual < (target - 0.05), actual, target

def heatpump_ww_cycle_min_runtime_guard(
    target_ww_mode,
    target_ww_temp,
    wp_status=None,
    wp_data=None,
    *,
    started_ts=0.0,
    latched_target_c=0.0,
    now_ts=None,
    min_runtime_s=None,
    abort_allowed=False,
    physical_running=None,
):
    """Schützt WW ab physischem Verdichterstart, nicht ab Sollwertauftrag."""

    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    min_s = max(float(heat_policy.WW_CYCLE_MIN_RUNTIME_S), _safe_float(min_runtime_s, heat_policy.WW_CYCLE_MIN_RUNTIME_S))
    start_value = _safe_float(started_ts, 0.0)
    requested = _safe_int(target_ww_mode, 0) == 1
    target_value = _safe_float(
        target_ww_temp if target_ww_temp is not None else latched_target_c,
        _safe_float(latched_target_c, 0.0),
    )
    below_target, actual_c, resolved_target_c = heatpump_ww_temperature_below_target(
        wp_status,
        wp_data,
        target_value if target_value > 0.0 else None,
        _safe_float(latched_target_c, 0.0),
    )
    target_known = resolved_target_c is not None and _safe_float(resolved_target_c, 0.0) > 0.0
    if target_known:
        target_value = _safe_float(resolved_target_c, target_value)
    if physical_running is None:
        physical_running = heatpump_ww_cycle_running(
            wp_status,
            wp_data,
            ww_requested=True,
        )
    physical_running = bool(physical_running)

    if requested:
        if actual_c is not None and not below_target:
            return {
                "target_ww_mode": target_ww_mode,
                "target_ww_temp": target_ww_temp,
                "started_ts": 0.0,
                "target_c": 0.0,
                "hold_active": False,
                "remaining_s": 0.0,
                "reason": "target_reached",
            }
        if start_value <= 0.0 and physical_running:
            start_value = now_value
        return {
            "target_ww_mode": 1,
            "target_ww_temp": target_ww_temp if target_ww_temp is not None else target_value,
            "started_ts": start_value,
            "target_c": target_value,
            "hold_active": False,
            "remaining_s": (
                max(0.0, min_s - max(0.0, now_value - start_value))
                if start_value > 0.0
                else 0.0
            ),
            "reason": "physical_running" if physical_running else "requested_waiting_for_physical_start",
        }

    if (
        start_value <= 0.0
        and physical_running
        and target_value > 0.0
        and below_target
        and not abort_allowed
    ):
        # Der Verdichter kann zwischen Sollwertauftrag und Folgetakt anlaufen,
        # während das Budget bereits entfällt. Das vorgemerkte Ziel ordnet
        # diesen physischen Start noch dem begonnenen WW-Zyklus zu.
        start_value = now_value

    if start_value <= 0.0:
        return {
            "target_ww_mode": target_ww_mode,
            "target_ww_temp": target_ww_temp,
            "started_ts": 0.0,
            "target_c": 0.0,
            "hold_active": False,
            "remaining_s": 0.0,
            "reason": "idle",
        }

    elapsed_s = max(0.0, now_value - start_value)
    remaining_s = max(0.0, min_s - elapsed_s)
    target_reached = bool(actual_c is not None and not below_target)
    if abort_allowed or target_reached or remaining_s <= 0.0:
        return {
            "target_ww_mode": target_ww_mode,
            "target_ww_temp": target_ww_temp,
            "started_ts": 0.0,
            "target_c": 0.0,
            "hold_active": False,
            "remaining_s": 0.0,
            "reason": "abort_allowed" if abort_allowed else ("target_reached" if target_reached else "min_runtime_done"),
        }

    hold_target = target_value if target_value > 0.0 else _safe_float(target_ww_temp, 0.0)
    return {
        "target_ww_mode": 1,
        "target_ww_temp": hold_target if hold_target > 0.0 else target_ww_temp,
        "started_ts": start_value,
        "target_c": hold_target,
        "hold_active": True,
        "remaining_s": remaining_s,
        "reason": "min_runtime_hold",
    }

def _heat_contract_ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        if parsed > 10_000_000_000:
            parsed /= 1000.0
        return parsed if math.isfinite(parsed) and parsed > 0.0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
        if parsed > 10_000_000_000:
            parsed /= 1000.0
        return parsed if math.isfinite(parsed) and parsed > 0.0 else None
    except ValueError:
        pass
    try:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        return datetime.fromisoformat(normalized).timestamp()
    except Exception:
        return None


def _heat_contract_revision(value):
    text = str(value or "")
    return bool(re.fullmatch(r"sha256:[0-9a-f]{64}", text))


def heat_price_boost_evidence_contract(
    live_data,
    forecast_result,
    *,
    now_ts=None,
    horizon_h=24.0,
):
    """Bewertet die kanonische Wärme-/PV-Projektion rein diagnostisch.

    Der aktuelle Speicherplan liefert für Haus und Wärme nur P50. Deshalb kann
    dieser Slice eine lückenlose P50-Deckung berechnen, aber noch keine aktive
    Preisverschiebung autorisieren. `evidence_status=EVIDENCE_LIMIT` ist bis zu
    getrennten konservativen PV-/Nichtwärmelast-Quantilen beabsichtigt.
    """

    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    required_h = max(0.25, _safe_float(horizon_h, 24.0))
    required_s = required_h * 3600.0
    base = {
        "schema_version": "heat_price_boost_evidence_v1",
        "evidence_status": "EVIDENCE_LIMIT",
        "eligible": False,
        "selected": False,
        "executable": False,
        "confirmed": False,
        "shadow_only": True,
        "commands_allowed": False,
        "heat_forecast_valid": False,
        "heat_forecast_need_kwh": None,
        "pv_coverage_valid": False,
        "pv_coverage_kwh": None,
        "conservative_pv_coverage_valid": False,
        "horizon_h": round(required_h, 3),
        "plan_id": None,
        "slot_id": None,
        "input_revisions": None,
    }
    heat_need = _safe_float(getattr(forecast_result, "need_kwh", None), -1.0)
    heat_coverage_h = _safe_float(getattr(forecast_result, "coverage_h", None), 0.0)
    heat_horizon_h = _safe_float(getattr(forecast_result, "horizon_h", None), 0.0)
    heat_valid = bool(
        forecast_result is not None
        and getattr(forecast_result, "valid", False)
        and not getattr(forecast_result, "stale", False)
        and str(getattr(forecast_result, "quality", "")) == "ml_prediction"
        and heat_need >= 0.0
        and heat_horizon_h + 1e-6 >= required_h
        and heat_coverage_h + 1e-6 >= required_h
    )
    if not heat_valid:
        reason = (
            "heat_forecast_horizon_incomplete"
            if forecast_result is not None
            and heat_horizon_h + 1e-6 >= required_h
            and heat_coverage_h + 1e-6 < required_h
            else "heat_forecast_not_authoritative"
        )
        return {
            **base,
            "reason": reason,
            "heat_forecast_need_kwh": heat_need if heat_need >= 0.0 else None,
        }

    payload = live_data if isinstance(live_data, dict) else {}
    plan_meta = payload.get("storage_plan_meta") if isinstance(payload.get("storage_plan_meta"), dict) else {}
    projection = (
        payload.get("heat_price_boost_forecast")
        if isinstance(payload.get("heat_price_boost_forecast"), dict)
        else {}
    )
    plan_id = str(plan_meta.get("plan_id") or "")
    projection_plan_id = str(projection.get("plan_id") or "")
    input_revisions = (
        plan_meta.get("input_revisions")
        if isinstance(plan_meta.get("input_revisions"), dict)
        else {}
    )
    required_revisions = ("pv_ensemble", "load_ensemble", "config")
    binding_valid = bool(
        _heat_contract_revision(plan_id)
        and projection.get("schema_version") == "heat_price_boost_forecast_v1"
        and projection_plan_id == plan_id
        and all(_heat_contract_revision(input_revisions.get(key)) for key in required_revisions)
    )
    if not binding_valid:
        return {
            **base,
            "reason": "storage_plan_binding_invalid",
            "heat_forecast_valid": True,
            "heat_forecast_need_kwh": heat_need,
            "plan_id": plan_id or None,
            "input_revisions": input_revisions or None,
        }

    valid_from = _heat_contract_ts(plan_meta.get("valid_from"))
    valid_until = _heat_contract_ts(plan_meta.get("valid_until"))
    horizon_end = _heat_contract_ts(plan_meta.get("horizon_end"))
    generated_at = _heat_contract_ts(plan_meta.get("generated_at"))
    plan_active = bool(
        valid_from is not None
        and valid_until is not None
        and generated_at is not None
        and valid_from <= now_value < valid_until
        and generated_at <= now_value + 60.0
        and now_value - generated_at <= 30 * 60
        and horizon_end is not None
        and horizon_end + 1e-6 >= now_value + required_s
    )
    if not plan_active:
        return {
            **base,
            "reason": "storage_plan_stale_or_inactive",
            "heat_forecast_valid": True,
            "heat_forecast_need_kwh": heat_need,
            "plan_id": plan_id,
            "input_revisions": input_revisions,
        }

    slots = [
        item
        for item in projection.get("slots") or []
        if isinstance(item, dict)
    ]
    slots.sort(key=lambda item: _safe_int(item.get("start_ts_ms"), 0))
    interval_start_ms = int(round(now_value * 1000.0))
    interval_end_ms = int(round((now_value + required_s) * 1000.0))
    cursor_ms = interval_start_ms
    p50_surplus_wh = 0.0
    first_slot_id = None
    for slot in slots:
        start_ms = _safe_int(slot.get("start_ts_ms"), 0)
        end_ms = _safe_int(slot.get("end_ts_ms"), 0)
        if end_ms <= interval_start_ms or start_ms >= interval_end_ms:
            continue
        overlap_start_ms = max(interval_start_ms, start_ms)
        overlap_end_ms = min(interval_end_ms, end_ms)
        if end_ms <= start_ms or overlap_start_ms > cursor_ms:
            return {
                **base,
                "reason": "pv_coverage_slot_gap",
                "heat_forecast_valid": True,
                "heat_forecast_need_kwh": heat_need,
                "plan_id": plan_id,
                "input_revisions": input_revisions,
            }
        if slot.get("pv_forecast_fresh") is not True or slot.get("forecast_fresh") is not True:
            return {
                **base,
                "reason": "pv_coverage_forecast_stale",
                "heat_forecast_valid": True,
                "heat_forecast_need_kwh": heat_need,
                "plan_id": plan_id,
                "input_revisions": input_revisions,
            }
        pv_p50_w = _safe_float(slot.get("pv_p50_w"), -1.0)
        house_p50_w = _safe_float(slot.get("house_p50_w"), -1.0)
        wallbox_p50_w = _safe_float(slot.get("wallbox_p50_w"), 0.0)
        if pv_p50_w < 0.0 or house_p50_w < 0.0 or wallbox_p50_w < 0.0:
            return {
                **base,
                "reason": "pv_coverage_projection_incomplete",
                "heat_forecast_valid": True,
                "heat_forecast_need_kwh": heat_need,
                "plan_id": plan_id,
                "input_revisions": input_revisions,
            }
        if first_slot_id is None:
            first_slot_id = slot.get("slot_id")
        p50_surplus_wh += max(0.0, pv_p50_w - house_p50_w - wallbox_p50_w) * (
            overlap_end_ms - overlap_start_ms
        ) / 3_600_000.0
        cursor_ms = max(cursor_ms, overlap_end_ms)
        if cursor_ms >= interval_end_ms:
            break
    if cursor_ms < interval_end_ms:
        return {
            **base,
            "reason": "pv_coverage_slot_gap",
            "heat_forecast_valid": True,
            "heat_forecast_need_kwh": heat_need,
            "plan_id": plan_id,
            "input_revisions": input_revisions,
        }

    return {
        **base,
        "reason": "p50_only_evidence_limit",
        "heat_forecast_valid": True,
        "heat_forecast_need_kwh": round(max(0.0, heat_need), 4),
        "pv_coverage_valid": True,
        "pv_coverage_kwh": round(max(0.0, p50_surplus_wh) / 1000.0, 4),
        "plan_id": plan_id,
        "slot_id": first_slot_id,
        "input_revisions": input_revisions,
    }


def build_heat_intent_shadow_projection(
    live_data,
    *,
    now_ts=None,
    heat_policy_decision=None,
):
    """Validiert den Plan-Candidate quer gegen die aktuelle Laufzeitprojektion.

    Das Ergebnis ist ausschließlich Diagnose. Selbst ein vollständig
    konsistenter Shadow-Intent bleibt ``commands_allowed=False`` und wird von
    keinem Preis-, Ziel- oder Treiberpfad gelesen.
    """

    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    payload = live_data if isinstance(live_data, dict) else {}
    projection = (
        payload.get("heat_price_boost_forecast")
        if isinstance(payload.get("heat_price_boost_forecast"), dict)
        else {}
    )
    plan_meta = (
        payload.get("storage_plan_meta")
        if isinstance(payload.get("storage_plan_meta"), dict)
        else {}
    )
    runtime = (
        payload.get("storage_dispatch_runtime")
        if isinstance(payload.get("storage_dispatch_runtime"), dict)
        else {}
    )
    candidate = (
        projection.get("candidate")
        if isinstance(projection.get("candidate"), dict)
        else None
    )
    reasons = []
    plan_id = str(plan_meta.get("plan_id") or "")
    projection_plan_id = str(projection.get("plan_id") or "")
    runtime_plan_id = str(runtime.get("plan_id") or "")
    runtime_slot_id = str(runtime.get("slot_id") or "")
    input_revisions = (
        plan_meta.get("input_revisions")
        if isinstance(plan_meta.get("input_revisions"), dict)
        else {}
    )

    if projection.get("schema_version") != "heat_price_boost_forecast_v1":
        reasons.append("HEAT_PROJECTION_SCHEMA_INVALID")
    if not _heat_contract_revision(plan_id):
        reasons.append("PLAN_ID_INVALID")
    if projection_plan_id != plan_id:
        reasons.append("PROJECTION_PLAN_ID_MISMATCH")
    if runtime.get("schema_version") != "storage_dispatch_runtime_v1":
        reasons.append("STORAGE_RUNTIME_SCHEMA_INVALID")
    if runtime.get("plan_valid") is not True:
        reasons.append("STORAGE_RUNTIME_PLAN_NOT_VALID")
    if runtime_plan_id != plan_id:
        reasons.append("STORAGE_RUNTIME_PLAN_ID_MISMATCH")
    if not _heat_contract_revision(runtime_slot_id):
        reasons.append("STORAGE_RUNTIME_SLOT_ID_INVALID")

    generated_at = _heat_contract_ts(plan_meta.get("generated_at"))
    valid_from = _heat_contract_ts(plan_meta.get("valid_from"))
    valid_until = _heat_contract_ts(plan_meta.get("valid_until"))
    if (
        generated_at is None
        or valid_from is None
        or valid_until is None
        or not (valid_from <= now_value < valid_until)
        or generated_at > now_value + 60.0
        or now_value - generated_at > 30 * 60
    ):
        reasons.append("PLAN_NOT_CURRENT")

    if candidate is None:
        reasons.append("HEAT_INTENT_CANDIDATE_MISSING")
        draft_intent = heat_intent.validate_heat_intent_candidate(None)
    else:
        binding = (
            candidate.get("binding")
            if isinstance(candidate.get("binding"), dict)
            else {}
        )
        if binding.get("plan_id") != plan_id:
            reasons.append("CANDIDATE_PLAN_ID_MISMATCH")
        if binding.get("plan_revision") != plan_id:
            reasons.append("CANDIDATE_PLAN_REVISION_MISMATCH")
        if binding.get("slot_id") != runtime_slot_id:
            reasons.append("CANDIDATE_SLOT_ID_MISMATCH")
        if binding.get("slot_revision") != runtime_slot_id:
            reasons.append("CANDIDATE_SLOT_REVISION_MISMATCH")

        inputs = (
            candidate.get("inputs")
            if isinstance(candidate.get("inputs"), dict)
            else {}
        )
        user = inputs.get("user") if isinstance(inputs.get("user"), dict) else {}
        capability = (
            inputs.get("capability")
            if isinstance(inputs.get("capability"), dict)
            else {}
        )
        forecast_evidence = (
            inputs.get("forecast_evidence")
            if isinstance(inputs.get("forecast_evidence"), dict)
            else {}
        )
        heat_evidence = (
            forecast_evidence.get("heat_need")
            if isinstance(forecast_evidence.get("heat_need"), dict)
            else {}
        )
        pv_evidence = (
            forecast_evidence.get("pv_coverage")
            if isinstance(forecast_evidence.get("pv_coverage"), dict)
            else {}
        )
        config_revision = input_revisions.get("config")
        if (
            not _heat_contract_revision(config_revision)
            or user.get("revision") != config_revision
            or capability.get("revision") != config_revision
        ):
            reasons.append("CANDIDATE_CONFIG_REVISION_MISMATCH")
        if heat_evidence.get("source_revision") != input_revisions.get("load_ensemble"):
            reasons.append("CANDIDATE_HEAT_REVISION_MISMATCH")
        method_revision = forecast_evidence.get("method_revision")
        expected_pv_revision = heat_intent.revision_hash({
            "pv_ensemble": input_revisions.get("pv_ensemble"),
            "load_ensemble": input_revisions.get("load_ensemble"),
            "method_revision": method_revision,
        })
        if pv_evidence.get("source_revision") != expected_pv_revision:
            reasons.append("CANDIDATE_PV_REVISION_MISMATCH")
        if candidate.get("selected") is True:
            # Preisfenster, Storage-Budget, Policy-/Root-Generation und Ack
            # sind in v1 noch nicht vollständig gebunden. Eine Auswahl wäre
            # deshalb selbst bei struktureller Konsistenz unzulässig.
            reasons.append("CANDIDATE_SELECTION_NOT_SUPPORTED_IN_V1")
        draft_intent = heat_intent.validate_heat_intent_candidate(candidate)

    draft_validation = heat_intent.validate_heat_intent(draft_intent)
    if draft_validation.get("valid") is not True:
        reasons.append("HEAT_INTENT_CONTRACT_INVALID")
    reasons = list(dict.fromkeys(reasons))
    if reasons:
        exported_intent = heat_intent.validate_heat_intent_candidate(None)
    else:
        exported_intent = draft_intent

    policy_owner = str(
        getattr(heat_policy_decision, "owner", None) or ""
    ).strip()
    intent_id = exported_intent.get("intent_id")
    owner_binding_id = (
        heat_intent.revision_hash({
            "intent_id": intent_id,
            "policy_owner": policy_owner,
        })
        if isinstance(intent_id, str) and intent_id and policy_owner
        else None
    )
    return exported_intent, {
        "schema_version": "heat_intent_runtime_validation_v1",
        "validation_valid": bool(
            draft_validation.get("valid") is True and not reasons
        ),
        "binding_valid": not reasons,
        "activation_authorized": False,
        "shadow_only": True,
        "commands_allowed": False,
        "executable": False,
        "confirmed": False,
        "plan_id": plan_id or None,
        "slot_id": runtime_slot_id or None,
        "candidate_id": (
            candidate.get("candidate_id")
            if isinstance(candidate, dict)
            else None
        ),
        "intent_id": exported_intent.get("intent_id"),
        "policy_owner": policy_owner or None,
        "owner_binding_id": owner_binding_id,
        "evidence_status": exported_intent.get("status"),
        "policy_target_state": getattr(
            heat_policy_decision,
            "target_state",
            None,
        ),
        "reason_codes": reasons or ["VALIDATED_EFFECTLESS_SHADOW"],
    }


def build_heatpump_policy_decision(
    *,
    config,
    now_ts,
    auto_mode,
    wp,
    wp_data,
    wp_status,
    at_mittel,
    heizgrenze_temp,
    conf_wws,
    conf_hz,
    free_for_limbs_w,
    grid_start_limit,
    soc,
    min_soc,
    current_price,
    price_action,
    price_pause_limit,
    price_hard_limit,
    market_heatpump_release,
    legacy_price_heatpump_active,
    predump_heatpump_active,
    boost_active,
    price_boost_active,
    is_ww_timer_running,
    ww_cycle_started_ts=0.0,
    source_protection_active,
    restart_block_remaining_s,
    price_block_started_ts,
    boost_delivered_kwh,
    price_block_control_authorized=False,
    previous_target_state=heat_policy.TARGET_NORMAL,
    previous_sg_ready_state=heat_policy.SG_READY_NORMAL,
    previous_available_budget_w=0,
    forecast_result=None,
    wp_type=None,
):
    """Translate Energy-Manager state into the central heat policy contract."""

    temp_ctx = _heat_policy_temperature_context(config, wp_data, at_mittel, heizgrenze_temp, conf_wws, conf_hz)
    if forecast_result is None:
        forecast_result = heat_forecast.predict_wp_energy_need_kwh(
            forecast_temp_c=_safe_float(at_mittel, 8.0),
            now_ts=now_ts,
            user_fallback_kwh=_heat_policy_user_fallback_kwh(config),
        )
    forecast_deficit_kwh = heat_forecast.calculate_heat_deficit_kwh(
        forecast_result.need_kwh,
        delivered_kwh=boost_delivered_kwh,
    )
    price_value = _safe_float(current_price, 99.9)
    price_valid = current_price is not None and price_value < 99.0
    expensive_price_window_active = bool(price_valid and price_value >= _safe_float(price_pause_limit, 35.0))
    low_price_window_active = bool(
        price_action == "BOOST"
        or bool(legacy_price_heatpump_active)
        or heatpump_negative_price_release_requested(
            market_heatpump_release
        )
    )
    market_end_ts = _market_release_end_ts_s(market_heatpump_release if isinstance(market_heatpump_release, dict) else {})
    window_end_ts = market_end_ts
    required_w = abs(_safe_int(grid_start_limit, -3500))
    pv_budget_w = max(0, _safe_int(free_for_limbs_w, 0))
    predump_budget_w = pv_budget_w if predump_heatpump_active else 0
    battery_empty_soc = _safe_float(_heat_config_value(config, ("heat_price_block_empty_soc",), max(5.0, min(15.0, _safe_float(min_soc, 80.0) - 20.0))), 10.0)
    summer_mode = _safe_float(at_mittel, 20.0) > _safe_float(heizgrenze_temp, 10.0)
    ww_requested = bool(boost_active or price_boost_active or predump_heatpump_active or is_ww_timer_running)
    ww_cycle_running = bool(
        luxtronik_ww_runtime_state(wp_status, wp_data).get("ww_running")
        is True
        if _safe_int(wp_type, -1) == 0
        else heatpump_ww_cycle_running(
            wp_status,
            wp_data,
            ww_requested=ww_requested,
        )
    )
    operating_mode = normalize_luxtronik_operating_mode(
        wp_data.get("Betriebsart") if isinstance(wp_data, dict) else None
    )
    operating_text = str(
        wp_data.get("Betriebszustand", "") if isinstance(wp_data, dict) else ""
    ).casefold()
    defrost_active = bool(
        operating_mode == 4
        or "abtau" in operating_text
        or "defrost" in operating_text
    )
    legionella_active = bool(
        "legion" in operating_text
        or "desinfektion" in operating_text
    )
    ctx = heat_policy.HeatPolicyInput(
        now_ts=now_ts,
        auto_enabled=bool(auto_mode),
        heat_enabled=bool(wp),
        heatpump_configured=bool(wp),
        heater_configured=False,
        pv_available_budget_w=pv_budget_w,
        pv_start_w=required_w,
        pv_stop_w=max(0, min(required_w, 500)),
        pv_hysteresis_active=bool(previous_target_state == heat_policy.TARGET_PV_SURPLUS),
        predump_available_budget_w=predump_budget_w,
        low_price_window_active=low_price_window_active,
        expensive_price_window_active=expensive_price_window_active,
        price_quality_valid=price_valid,
        current_price_ct=price_value if price_valid else None,
        price_window_end_ts=window_end_ts,
        price_pain_limit_ct=_safe_float(price_pause_limit, 45.0),
        battery_empty=bool(_safe_float(soc, 0.0) <= battery_empty_soc),
        price_block_started_ts=price_block_started_ts if price_block_started_ts and price_block_started_ts > 0 else None,
        price_block_control_authorized=bool(
            price_block_control_authorized
        ),
        forecast_need_kwh=forecast_result.need_kwh,
        forecast_deficit_kwh=forecast_deficit_kwh,
        forecast_valid=bool(forecast_result.valid and not forecast_result.stale),
        forecast_source=forecast_result.source,
        forecast_quality=forecast_result.quality,
        boost_delivered_kwh=boost_delivered_kwh,
        control_cycle_s=60.0,
        heatpump_grid_boost_enable=True,
        heatpump_grid_boost_max_w=max(0, required_w),
        temperature_valid=bool(temp_ctx["temperature_valid"]),
        temperature_c=temp_ctx["temperature_c"],
        temperature_min_c=temp_ctx["temperature_min_c"],
        temperature_max_c=temp_ctx["temperature_max_c"],
        ww_cycle_requested=bool(price_action == "BOOST" and summer_mode),
        ww_cycle_running=ww_cycle_running,
        ww_cycle_started_ts=ww_cycle_started_ts,
        defrost_active=defrost_active,
        legionella_active=legionella_active,
        source_protection_active=bool(source_protection_active),
        restart_block_remaining_s=max(0.0, _safe_float(restart_block_remaining_s, 0.0)),
        previous_target_state=previous_target_state,
        previous_sg_ready_state=previous_sg_ready_state,
        previous_available_budget_w=previous_available_budget_w,
    )
    return heat_policy.decide_heat_policy(ctx), forecast_result

def send_webpush(config_key, title, body, url="/", actions=None):
    """
    Hilfsfunktion: Prüft, ob der Nutzer in e3dc_v4.json die Benachrichtigung
    (z.B. push_notify_soc = 1) aktiviert hat und verschickt dann die Push-Nachricht über send_push.py.
    """
    push_enabled = False
    try:
        if os.path.exists(V4_CONFIG_PATH):
            with open(V4_CONFIG_PATH, 'r', encoding='utf-8') as f:
                v4_data = json.load(f)
                val = v4_data.get(config_key)
                if val in [1, '1', True, 'true']:
                    push_enabled = True
    except Exception as e:
        logger.error(f"Fehler beim Web-Push Config-Lesen ({config_key}): {e}")

    if push_enabled:
        logger.info(f"Sende Web-Push '{title}': {body}")
        try:
            install_root = os.path.dirname(os.path.dirname(os.path.abspath(script_dir)))
            push_script = os.path.join(install_root, "Installer", "send_push.py")
            cmd_python = sys.executable if os.path.isabs(sys.executable) and os.path.isfile(sys.executable) else ""

            if cmd_python and os.path.exists(push_script):
                # Wir rufen es asynchron (fire and forget) per subprocess auf,
                # damit der energy_manager nicht auf eine Antwort der Apple/Google Server warten muss!
                cmd_args = [cmd_python, push_script, title, body, "--url", url]
                if actions:
                    cmd_args.extend(["--actions", actions])

                subprocess.Popen(cmd_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                logger.error("Push-Aufruf blockiert: Release-Skript oder aktueller Interpreter fehlt")
        except Exception as e:
             logger.error(f"Fehler beim Web-Push Aufruf: {e}")

def setup_logging():
    """Initialisiert ein rotierendes Logfile für den Energy Manager."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, "energy_manager.log")

    logger.setLevel(logging.INFO)

    formatter = logging.Formatter('%(asctime)s - %(message)s', datefmt='%d.%m %H:%M:%S')

    if not logger.handlers:
        # 1. Dateiausgabe (für das Web-Dashboard)
        file_handler = RotatingFileHandler(log_file, maxBytes=1024*1024, backupCount=1, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # 2. Konsolenausgabe (für journalctl)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    # Berechtigungen für das Logfile setzen, damit es vom Webserver gelesen werden kann
    install_quiet_info_filter(
        logger,
        min_interval_s=300,
        normalize_numbers=True,
        always_keywords=(
            "not-aus",
            "fehler",
            "fehlgeschlagen",
            "timeout",
            "dienst",
            "sigterm",
            "update-prozess",
            "konfiguration aktualisiert",
            "start pv-boost",
            "stop pv-boost",
            "preis-steuerung",
            "superintelligence beendet",
            "morning-boost beendet",
        ),
    )
    try:
        os.chmod(log_file, 0o664)
        # Owner/Group vom Verzeichnis erben
        st = os.stat(LOG_DIR)
        os.chown(log_file, st.st_uid, st.st_gid)
    except Exception: pass
    return logger

# Verzeichnis für Archiv erstellen
if not os.path.exists(BACKUP_DIR):
    os.makedirs(BACKUP_DIR)

def read_e3dc_config_value_raw(key):
    """Liest einen Rohwert aus der e3dc_v4.json."""
    try:
        if os.path.exists(V4_CONFIG_PATH):
            with open(V4_CONFIG_PATH, 'r', encoding='utf-8') as f:
                v4_data = json.load(f)
                val = v4_data.get(key)
                if val is not None:
                    if isinstance(val, bool): return "1" if val else "0"
                    return str(val).strip()
    except Exception: pass
    return None

def read_e3dc_config_value(key, default=None):
    """Liest einen Wert aus e3dc_v4.json und gibt einen Default zurueck, falls nicht gefunden."""
    value = read_e3dc_config_value_raw(key)
    if value is None:
        return default
    # Konvertiere 'true'/'false' Strings zu Booleans, Zahlen zu Zahlen etc.
    if value.lower() in ['true', '1']: return 1
    if value.lower() in ['false', '0']: return 0
    try: return float(value)
    except ValueError: return value

def load_e3dc_config_dict():
    """Liest die gesamte V4 JSON Config in ein Dictionary (Performance)."""
    config = {}
    try:
        if os.path.exists(V4_CONFIG_PATH):
            with open(V4_CONFIG_PATH, 'r', encoding='utf-8') as f:
                v4_data = json.load(f)
                for k, v in v4_data.items():
                    val = v
                    if isinstance(v, str):
                        v_lower = v.strip().lower()
                        if v_lower in ['true', '1']: val = 1
                        elif v_lower in ['false', '0']: val = 0
                    elif isinstance(v, bool):
                        val = 1 if v else 0
                    config[k.strip().lower()] = val
    except Exception as e:
        logger = logging.getLogger("EnergyManager")
        logger.error(f"Fehler beim Laden von e3dc_v4.json: {e}")

    return config

def get_cfg_value(config_dict, key, default=None):
    """Holt Wert aus dem Cache-Dict."""
    if config_dict is None: return default
    val = config_dict.get(key.lower(), default)
    if val == '': return default
    try: return float(val)
    except (ValueError, TypeError): return val

def get_cfg_int(config_dict, key, default=0):
    val = get_cfg_value(config_dict, key, default)
    try: return int(float(val))
    except: return default

def cleanup_legacy_energy_state_file(path=LEGACY_ENERGY_STATE_FILE):
    """Remove stale Morning-Boost/SI state left by pre-V5 Energy Manager runs."""
    if not os.path.exists(path):
        return False
    mode = "unknown"
    try:
        with open(path, "r", encoding="utf-8") as f:
            state_data = json.load(f)
            mode = str(state_data.get("mode") or mode)
    except Exception:
        pass
    try:
        os.remove(path)
        logger.info("Entferne alten Energy-Manager-Status %s: V5 nutzt Pre-Dump, Ladekurve und Storage-Manager-Auftraege.", mode)
        return True
    except Exception as exc:
        logger.debug("Alter Energy-Manager-Status konnte nicht entfernt werden: %s", exc)
        return False


ENERGY_RESTART_CHECKPOINT_KEYS = (
    "daily_boost_counter",
    "last_pv_boost_time",
    "last_wp_command_time",
    "last_notstrom_status",
    "boost_active",
    "price_boost_active",
    "pre_pause_active",
    "pv_pause_active",
    "pv_pause_blocked_until",
    "wp_last_pv_boost_start_ts",
    "wp_last_pv_boost_stop_ts",
    "heatpump_positive_signal_started_ts",
    "heatpump_positive_signal_demand_class",
    "heatpump_positive_signal_hold_guard",
    "luxtronik_ww_budget_loss_guard_state",
    "luxtronik_ww_budget_loss_blocked_until_fresh_budget",
    "heatpump_budget_demand_first_seen_ts",
    "wp_last_ww_cycle_start_ts",
    "wp_last_ww_cycle_target_c",
    "wp_curr_ext_ww",
    "wp_curr_ext_hz",
    "wp_curr_ext_khl",
    "idm_ext_ww",
    "idm_ext_hz",
    "idm_ext_khl",
    "predump_heatpump_active",
    "predump_heatpump_started_ts",
    "predump_heatpump_hold_until",
    "heat_price_block_started_ts",
    "heat_policy_boost_delivered_kwh",
)
ENERGY_RESTART_SEMANTIC_KEYS = (
    "daily_boost_counter",
    "last_notstrom_status",
    "boost_active",
    "price_boost_active",
    "pre_pause_active",
    "pv_pause_active",
    "heatpump_positive_signal_started_ts",
    "heatpump_positive_signal_demand_class",
    "luxtronik_ww_budget_loss_blocked_until_fresh_budget",
    "wp_last_ww_cycle_start_ts",
    "wp_last_ww_cycle_target_c",
    "wp_curr_ext_ww",
    "wp_curr_ext_hz",
    "wp_curr_ext_khl",
    "idm_ext_ww",
    "idm_ext_hz",
    "idm_ext_khl",
    "predump_heatpump_active",
    "pv_pause_hold_active",
    "predump_heatpump_hold_active",
)
CAR_SESSION_SEMANTIC_KEYS = (
    "start_soc",
    "start_kwh",
    "last_car_ts",
    "last_manual_ts",
    "soc_source_ts",
    "car_id",
    "car_capacity",
    "is_manual",
    "soc_source",
    "soc_rule_confirmed",
    "target_soc",
    "charging",
)


def build_energy_restart_checkpoint(live_state, now_ts=None):
    """Reduziert den großen Live-Status auf den Restart-/Safety-Vertrag."""

    current_ts = time.time() if now_ts is None else float(now_ts)
    checkpoint = {
        "schema_version": "energy_manager_restart_checkpoint_v1",
        "ts": datetime.fromtimestamp(current_ts).isoformat(),
    }
    if isinstance(live_state, dict):
        for key in ENERGY_RESTART_CHECKPOINT_KEYS:
            if key in live_state:
                checkpoint[key] = live_state.get(key)
    checkpoint["pv_pause_hold_active"] = bool(
        _safe_float(checkpoint.get("pv_pause_blocked_until"), 0.0) > current_ts
    )
    checkpoint["predump_heatpump_hold_active"] = bool(
        _safe_float(checkpoint.get("predump_heatpump_hold_until"), 0.0) > current_ts
    )
    return checkpoint


def persist_energy_restart_checkpoint(path, live_state, runtime_state, *, now_ts=None, force=False):
    checkpoint = build_energy_restart_checkpoint(live_state, now_ts=now_ts)
    active_runtime_state = any(
        bool(checkpoint.get(key))
        for key in (
            "boost_active",
            "price_boost_active",
            "pre_pause_active",
            "pv_pause_active",
            "predump_heatpump_active",
            "pv_pause_hold_active",
            "predump_heatpump_hold_active",
            "heatpump_positive_signal_started_ts",
            "wp_curr_ext_ww",
            "wp_curr_ext_hz",
            "wp_curr_ext_khl",
            "idm_ext_ww",
            "idm_ext_hz",
            "idm_ext_khl",
        )
    )
    return write_bounded_json_checkpoint(
        path,
        checkpoint,
        runtime_state,
        semantic_keys=ENERGY_RESTART_SEMANTIC_KEYS,
        heartbeat_s=(
            ENERGY_STATE_CHECKPOINT_ACTIVE_HEARTBEAT_S
            if active_runtime_state
            else ENERGY_STATE_CHECKPOINT_IDLE_HEARTBEAT_S
        ),
        now_ts=now_ts,
        force=force,
        warn_label="Energy-Manager-Restart-Checkpoint",
    )


def car_session_paths(wb_idx):
    wb_id = int(wb_idx)
    live_name = "car_charge_session.json" if wb_id == 1 else f"car_charge_session_wb{wb_id}.json"
    legacy_name = live_name
    return {
        "live": os.path.join("/var/www/html/ramdisk", live_name),
        "checkpoint": os.path.join(
            "/var/www/html/data",
            f"car_charge_session_checkpoint_wb{wb_id}.json",
        ),
        "legacy": os.path.join("/var/www/html/tmp", legacy_name),
    }


def load_car_session_state(wb_idx, paths=None, now_ts=None):
    """Liest RAM zuerst, danach neuen Checkpoint und zuletzt den Altpfad."""

    candidates = paths or car_session_paths(wb_idx)
    current_ts = time.time() if now_ts is None else float(now_ts)
    for key, max_age_s in (
        ("live", 300.0),
        ("checkpoint", 36 * 3600.0),
        ("legacy", 36 * 3600.0),
    ):
        path = candidates.get(key)
        if not path or not os.path.exists(path):
            continue
        try:
            if max(0.0, current_ts - os.path.getmtime(path)) > max_age_s:
                continue
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                return payload, path
        except Exception:
            continue
    return {}, ""


def discard_invalid_car_session_state(
    paths,
    *,
    wb_idx=None,
    checkpoint_runtime=None,
    latest_sessions=None,
):
    """Entfernt einen ungültigen Altanker samt persistenten Wiederladepfaden."""

    candidates = paths if isinstance(paths, dict) else {}
    for path in {
        str(candidates.get(key) or "")
        for key in ("live", "checkpoint", "legacy")
    }:
        if not path:
            continue
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    if isinstance(checkpoint_runtime, dict):
        checkpoint_runtime.clear()
    if isinstance(latest_sessions, dict) and wb_idx is not None:
        latest_sessions.pop(int(wb_idx), None)


def persist_car_session_checkpoint(
    path,
    session,
    runtime_state,
    *,
    charging=None,
    now_ts=None,
    force=False,
):
    checkpoint = dict(session or {})
    checkpoint["schema_version"] = "car_charge_session_checkpoint_v1"
    if charging is None:
        charging = bool(checkpoint.get("charging", False))
    checkpoint["charging"] = bool(charging)
    return write_bounded_json_checkpoint(
        path,
        checkpoint,
        runtime_state,
        semantic_keys=CAR_SESSION_SEMANTIC_KEYS,
        heartbeat_s=(
            CAR_SESSION_CHECKPOINT_ACTIVE_HEARTBEAT_S
            if checkpoint["charging"]
            else CAR_SESSION_CHECKPOINT_IDLE_HEARTBEAT_S
        ),
        now_ts=now_ts,
        force=force,
        warn_label="Fahrzeug-Session-Checkpoint",
    )


def load_previous_energy_state(paths=None, now_obj=None):
    """Load the newest Energy-Manager state from ramdisk or persistent data."""
    now_obj = now_obj or datetime.now()
    candidates = []
    for path in paths or (RAMDISK_FILE, ENERGY_STATE_FILE):
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            ts_obj = None
            raw_ts = data.get("ts")
            if raw_ts:
                try:
                    ts_obj = datetime.fromisoformat(str(raw_ts))
                except Exception:
                    ts_obj = None
            if ts_obj is None:
                ts_obj = datetime.fromtimestamp(os.path.getmtime(path))
            age_s = max(0.0, (now_obj - ts_obj).total_seconds())
            candidates.append((ts_obj.timestamp(), path, data, age_s))
        except Exception as exc:
            logger.debug("Energy-Manager-Status %s nicht lesbar: %s", path, exc)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, path, data, age_s = candidates[0]
    return {"path": path, "data": data, "age_s": age_s}


def restored_heatpump_state_contract(
    saved,
    state_age_s,
    *,
    auto_mode_enabled,
    now_ts=None,
    max_state_age_s=ENERGY_STATE_ACTIVE_RESTORE_MAX_AGE_S,
):
    """Übernimmt nach Neustart nur konservative Holds, niemals Aktorzustände."""

    saved = saved if isinstance(saved, dict) else {}
    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
    age_s = max(0.0, _safe_float(state_age_s, max_state_age_s + 1.0))
    max_age_s = max(0.0, _safe_float(max_state_age_s, ENERGY_STATE_ACTIVE_RESTORE_MAX_AGE_S))
    state_fresh = bool(age_s < max_age_s)
    auto_enabled = bool(auto_mode_enabled)

    started_ts = _safe_float(saved.get("wp_last_ww_cycle_start_ts"), 0.0)
    target_c = _safe_float(saved.get("wp_last_ww_cycle_target_c"), 0.0)
    elapsed_s = now_value - started_ts if started_ts > 0.0 else float("inf")
    ww_hold_valid = bool(
        state_fresh
        and auto_enabled
        and started_ts > 0.0
        and 0.0 <= elapsed_s < float(heat_policy.WW_CYCLE_MIN_RUNTIME_S)
        and 20.0 <= target_c <= 70.0
    )

    blocked_until = _safe_float(saved.get("pv_pause_blocked_until"), 0.0)
    pause_hold_valid = bool(state_fresh and auto_enabled and blocked_until > now_value)
    active_command_saved = bool(
        saved.get("boost_active")
        or saved.get("pv_pause_active")
        or saved.get("price_boost_active")
        or saved.get("pre_pause_active")
        or _safe_int(saved.get("wp_curr_ext_ww", saved.get("idm_ext_ww", 0)), 0)
        or _safe_int(saved.get("wp_curr_ext_hz", saved.get("idm_ext_hz", 0)), 0)
        or _safe_int(saved.get("wp_curr_ext_khl", saved.get("idm_ext_khl", 0)), 0)
    )
    saved_signal_started_ts = _strict_storage_budget_ts(
        saved.get("heatpump_positive_signal_started_ts")
    )
    saved_signal_hold_guard = (
        copy.deepcopy(saved.get("heatpump_positive_signal_hold_guard"))
        if isinstance(
            saved.get("heatpump_positive_signal_hold_guard"),
            dict,
        )
        else {}
    )
    saved_signal_demand_class = str(
        saved.get("heatpump_positive_signal_demand_class") or "none"
    ).strip().casefold()
    saved_signal_demand_valid = bool(
        saved_signal_demand_class in {
            "ww_immediate_manual",
            "ww_timer_comfort",
            "ww_timer_eco",
            "pre_dump",
            "market_price",
            "price",
            "pv_surplus",
        }
    )
    saved_checkpoint_ts = _strict_storage_budget_ts(
        saved.get("checkpoint_ts")
    )
    checkpoint_age_s = (
        now_value - saved_checkpoint_ts
        if saved_checkpoint_ts is not None
        else None
    )
    signal_timebase_valid = bool(
        checkpoint_age_s is not None
        and checkpoint_age_s >= 0.0
        and abs(checkpoint_age_s - age_s) <= 5.0
    )
    positive_signal_bookkeeping_valid = bool(
        state_fresh
        and auto_enabled
        and saved_signal_started_ts is not None
        and saved_signal_demand_valid
    )
    if positive_signal_bookkeeping_valid and (
        not signal_timebase_valid
        or saved_signal_started_ts > now_value
    ):
        # Eine unklare oder rückwärts gesprungene Wandzeit darf die
        # Mindesthaltezeit nicht verkürzen. Nur die lokale 600-s-Signalkante
        # beginnt konservativ neu; die monotone 150-s-Leistungslease bleibt
        # ausschließlich beim Storage Manager gebunden und wird nicht remintet.
        saved_signal_started_ts = now_value

    if not auto_enabled:
        reason = "Nutzer-Aus: gespeicherte Aktorzustände und Holds verworfen"
    elif not state_fresh:
        reason = "Statusdatei stale: gespeicherte Aktorzustände und Holds verworfen"
    elif positive_signal_bookkeeping_valid or active_command_saved:
        reason = (
            "Physischer Zustand unbekannt: aktive Kommandos verworfen; "
            "nur Zeit-/Demand-Buchhaltung und gültige Schutz-Holds übernommen"
        )
    else:
        reason = "Keine aktiven Aktorkommandos wiederhergestellt"

    return {
        "diagnostic_signature": "HEATPUMP_RESTART_REVALIDATION",
        "state_fresh": state_fresh,
        "state_age_s": age_s,
        "auto_mode_enabled": auto_enabled,
        "physical_state_valid": False,
        "actuator_state_valid": False,
        "restore_active_commands": False,
        "active_command_saved": active_command_saved,
        "positive_signal_bookkeeping_valid": positive_signal_bookkeeping_valid,
        "positive_signal_timebase_status": (
            "wall_time_consistent"
            if signal_timebase_valid
            else "EVIDENCE_LIMIT_REARMED"
        ),
        "heatpump_positive_signal_started_ts": (
            saved_signal_started_ts
            if positive_signal_bookkeeping_valid
            else 0.0
        ),
        "heatpump_positive_signal_demand_class": (
            saved_signal_demand_class
            if positive_signal_bookkeeping_valid
            else "none"
        ),
        "heatpump_positive_signal_hold_guard": (
            saved_signal_hold_guard
            if positive_signal_bookkeeping_valid
            else {}
        ),
        # Vor der ersten frischen Live-Validierung bleibt jeder Neustartzustand
        # sicher gesperrt. Auch ein leerer Cache beweist keinen physischen Stillstand.
        "safe_stop_required": True,
        "ww_hold_restored": ww_hold_valid,
        "ww_cycle_started_ts": started_ts if ww_hold_valid else 0.0,
        "ww_cycle_target_c": target_c if ww_hold_valid else 0.0,
        "pause_hold_restored": pause_hold_valid,
        "pv_pause_blocked_until": blocked_until if pause_hold_valid else 0.0,
        "reason": reason,
    }


def revalidate_restored_heatpump_state(
    contract,
    *,
    live_state_valid,
    auto_mode_enabled,
    actuator_state_valid=False,
    validation_source="heatpump_live",
    now_ts=None,
):
    """Bestätigt nur den frischen physischen Zustand, nie den alten Aktor-Cache."""

    result = dict(contract) if isinstance(contract, dict) else {}
    live_valid = bool(live_state_valid)
    actuator_valid = bool(actuator_state_valid)
    auto_enabled = bool(auto_mode_enabled)
    result["auto_mode_enabled"] = auto_enabled
    result["physical_state_valid"] = bool(live_valid and auto_enabled)
    result["actuator_state_valid"] = bool(actuator_valid and auto_enabled)
    result["validation_source"] = str(validation_source or "unknown")
    result["restore_active_commands"] = False
    if not auto_enabled:
        result["safe_stop_required"] = True
        result["reason"] = "Nutzer-Aus: Wärmepumpe bleibt ohne EMS-Aktorbefehl"
    elif not (live_valid or actuator_valid):
        result["safe_stop_required"] = True
        result["reason"] = "Physischer Zustand weiterhin unbekannt oder stale: sicherer Stillstand"
    else:
        result["safe_stop_required"] = False
        result["revalidated_at"] = time.time() if now_ts is None else _safe_float(now_ts, time.time())
        if actuator_valid and not live_valid:
            result["reason"] = (
                "Frischer Shelly-Relaiszustand validiert; "
                "Policy entscheidet ohne Verdichter- oder Bedarfsannahme neu"
            )
        else:
            result["reason"] = "Frischer physischer Zustand validiert; Policy entscheidet ohne alten Aktor-Cache neu"
    return result


def revalidate_shelly_restart_state(contract, wp, *, auto_mode_enabled, now_ts=None):
    """Liest konfigurierte Shelly-Kontakte einmal frisch und öffnet nur den Aktorpfad."""

    if not isinstance(wp, ShellyHeatpump):
        raise TypeError("ShellyHeatpump erforderlich")
    if not bool(auto_mode_enabled):
        return revalidate_restored_heatpump_state(
            contract,
            live_state_valid=False,
            actuator_state_valid=False,
            auto_mode_enabled=False,
            validation_source="shelly_relay_readback",
            now_ts=now_ts,
        )
    relay_states = wp.refresh_relay_states()
    relay_contract = wp.relay_readback_contract(relay_states)
    result = revalidate_restored_heatpump_state(
        contract,
        live_state_valid=False,
        actuator_state_valid=relay_contract["valid"],
        auto_mode_enabled=True,
        validation_source=relay_contract["source"],
        now_ts=now_ts,
    )
    result["relay_required_contacts"] = relay_contract["required_contacts"]
    result["relay_confirmed_contacts"] = relay_contract["confirmed_contacts"]
    return result


def get_v4_eco_score():
    """Liest den aktuellen Optimization-Score und Billing-Price aus der Ramdisk"""
    try:
        path = "/var/www/html/ramdisk/eco_score.json"
        if not os.path.exists(path):
            return None, None

        with open(path, "r") as f:
            scores = json.load(f)

        now_ms = time.time() * 1000
        for s in scores:
            if s["start_timestamp"] <= now_ms < s["end_timestamp"]:
                return s.get("optimization_score", 50.0), s.get("billing_price", 99.9)

    except Exception as e:
        logger.error(f"Fehler beim Lesen der V4 Eco-Score JSON: {e}")
    return None, None


def _config_flag(value, default=False):
    if value is None or str(value).strip() == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def current_price_boost_consumer_allowed(config, device):
    """Aktuelle Nutzerfreigaben sind ein hartes Veto gegen alte Pläne."""
    if not isinstance(config, dict):
        return False
    # Der alte Preisplan darf seine Verbraucherfreigaben nur aus echten
    # Börsenpreisen ableiten. Wiederkehrende Tarifzeiten werden im
    # Wärmepumpen-Preis-Boost separat und bedarfsgeführt behandelt.
    if not supports_spot_market_prices(config):
        return False
    if not _config_flag(config.get("cheap_grid_boost_enable"), False):
        return False
    consumer_contract = PRICE_BOOST_CONSUMER_CONFIG.get(str(device or "").strip().lower())
    if consumer_contract is None:
        return False
    config_key, default = consumer_contract
    return _config_flag(config.get(config_key), default)


def _price_boost_plan_allows_device(plan, device):
    """Lässt nur einen expliziten booleschen Verbraucher-Vertrag passieren."""
    if not isinstance(plan, dict):
        return False
    allow = plan.get("allow")
    if not isinstance(allow, dict):
        return False
    return allow.get(str(device or "").strip().lower()) is True


def _read_price_boost_context(device):
    """Liest aktuelle Freigabe und exakt dazugehörige Plan-Dateievidenz."""
    try:
        with open(V4_CONFIG_PATH, "r", encoding="utf-8") as handle:
            config = json.load(handle)
        if not current_price_boost_consumer_allowed(config, device):
            return None, None

        now_s = time.time()
        with open(PRICE_BOOST_PLAN_PATH, "r", encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            plan = json.load(handle)
        if str((plan or {}).get("tariff_axis") or "") == "configured_recurring":
            if configured_tariff_type(config) != "octopus_heat":
                return None, None
            file_age_s = now_s - float(metadata.st_mtime)
            if file_age_s < 0.0 or file_age_s > PRICE_BOOST_PLAN_MAX_AGE_S:
                return None, None
        return plan, int(now_s * 1000)
    except Exception:
        return None, None


def active_price_boost_window(plan, now_ms=None):
    """Wertet nur frische konfigurierte Tariffenster an der Slotgrenze aus."""
    if not isinstance(plan, dict) or not plan.get("enabled"):
        return None
    if plan.get("context_valid", True) is False or plan.get("release_valid", True) is False:
        return None

    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    recurring_axis = str(plan.get("tariff_axis") or "") == "configured_recurring"
    if recurring_axis:
        try:
            generated_ms = int(plan.get("ts", 0) or 0)
            valid_until_ms = int(plan.get("valid_until_ts_ms", 0) or 0)
        except (TypeError, ValueError):
            return None
        if (
            str(plan.get("timezone") or "") != TARIFF_TIMEZONE_NAME
            or generated_ms <= 0
            or now_ms < generated_ms
            or now_ms - generated_ms > PRICE_BOOST_PLAN_MAX_AGE_S * 1000
            or valid_until_ms < now_ms
            or valid_until_ms > generated_ms + PRICE_BOOST_PLAN_MAX_AGE_S * 1000
        ):
            return None
        candidates = plan.get("windows") if isinstance(plan.get("windows"), list) else []
    else:
        if not plan.get("active"):
            return None
        candidates = [plan.get("active_window") or {}]

    for raw_window in candidates:
        if not isinstance(raw_window, dict):
            continue
        try:
            start_ms = int(raw_window.get("start_timestamp", 0) or 0)
            end_ms = int(raw_window.get("end_timestamp", 0) or 0)
        except (TypeError, ValueError):
            continue
        if start_ms > 0 and start_ms <= now_ms < end_ms:
            return raw_window
    return None


def price_boost_allows(device):
    """Zentraler EPEX-Preisboost: Geraete nur einschalten, wenn explizit erlaubt."""
    plan, now_ms = _read_price_boost_context(device)
    return bool(
        active_price_boost_window(plan, now_ms=now_ms) is not None
        and _price_boost_plan_allows_device(plan, device)
    ) if isinstance(plan, dict) else False

def price_boost_window_end_ts(device):
    """Return active cheap-grid window end timestamp in seconds for diagnostics/guards."""
    plan, now_ms = _read_price_boost_context(device)
    if not isinstance(plan, dict):
        return None
    try:
        win = active_price_boost_window(plan, now_ms=now_ms)
        if win is None or not _price_boost_plan_allows_device(plan, device):
            return None
        end_ms = int(win.get("end_timestamp", 0) or 0)
        return end_ms / 1000.0 if end_ms > 0 else None
    except Exception:
        return None

def read_storage_market_plan():
    try:
        if not os.path.exists(STORAGE_PLAN_PATH):
            return {}
        if time.time() - os.path.getmtime(STORAGE_PLAN_PATH) > 1800:
            return {}
        with open(STORAGE_PLAN_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def market_plan_allows(device, config=None, storage_plan=None):
    """Storage-Manager-Vertrag für externe Verbraucher lesen."""
    try:
        plan = storage_plan if isinstance(storage_plan, dict) else read_storage_market_plan()
        ctx = current_market_consumer_release(plan, device, config)
        return ctx if isinstance(ctx, dict) else {"allowed": False}
    except Exception:
        return {"allowed": False, "reason": "market_plan_error"}


def heatpump_negative_price_release_requested(release):
    """Akzeptiert nur die explizite Negativpreisfreigabe des Storage-Vertrags."""
    return bool(
        isinstance(release, dict)
        and release.get("allowed") is True
        and release.get("negative_price") is True
    )


def predump_allows(device):
    """Pre-Dump-Verbraucherfreigabe: Speicherplatz schaffen, ohne zuerst einzuspeisen."""
    try:
        if os.path.exists(V4_CONFIG_PATH):
            with open(V4_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            key = "predump_%s_enable" % device
            if str(cfg.get(key, 0)).strip().lower() not in ("1", "true", "yes", "on"):
                return False
        if not os.path.exists(PREDUMP_PLAN_PATH):
            return False
        with open(PREDUMP_PLAN_PATH, "r", encoding="utf-8") as f:
            plan = json.load(f)
        return bool(plan.get("enabled") and plan.get("active")
                    and int(time.time()) <= int(plan.get("expires_ts", 0) or 0)
                    and plan.get("allow", {}).get(device, False))
    except Exception:
        return False

def get_ml_prediction():
    """Liest die aktuelle KI-Prognose aus der RAM-Disk."""
    pred_file = "/var/www/html/ramdisk/ml_prediction.json"
    try:
        if os.path.exists(pred_file):
            with open(pred_file, "r") as f:
                return json.load(f)
    except Exception: pass
    return None

def _energy_live_from_ramdisk(data):
    """Mappt den validierten nativen Livevertrag für die Wärmeentscheidung."""
    if not isinstance(data, dict):
        return {}
    mapped = dict(data)

    def first_value(*keys):
        for key in keys:
            value = data.get(key)
            if value is not None:
                return value
        return None

    mapped.setdefault("grid", first_value("grid", "Grid_Power"))
    mapped.setdefault("grid_filtered", first_value("grid_filtered", "Grid_Power_Filtered"))
    mapped.setdefault("bat", first_value("bat", "Battery_Power"))
    mapped.setdefault("soc", first_value("soc", "SOC"))
    mapped.setdefault(
        "notstrom_status",
        first_value(
            "notstrom_status",
            "Notstrom_Status",
            "ems_emergency_power_status",
        ),
    )
    mapped.setdefault("wb_power", first_value("wb_power", "Wallbox_Power"))
    mapped.setdefault("wb", first_value("wb", "Wallbox_Power", "wb_power"))
    mapped.setdefault("wb2", first_value("wb2", "Wallbox2_Power", "Wallbox_2_Power", "wb2_power"))
    mapped.setdefault("wb_session_kwh", first_value("wb_session_kwh", "Wallbox_Session_kWh"))
    mapped.setdefault("wb2_session_kwh", first_value("wb2_session_kwh", "Wallbox2_Session_kWh", "Wallbox_2_Session_kWh"))
    mapped.setdefault("wb_locked", first_value("wb_locked", "Wallbox_Locked"))
    mapped.setdefault("wb2_locked", first_value("wb2_locked", "Wallbox2_Locked", "Wallbox_2_Locked"))
    mapped.setdefault("prices", [])
    mapped.setdefault("forecast", [])
    return mapped


def _energy_forecast_from_ramdisk(
    forecast_path="/var/www/html/ramdisk/pv_forecast.json",
    max_age_s=3 * 3600,
    now_s=None,
    lookahead_s=90 * 60,
):
    """Projiziert ausschließlich bestätigte Slots im absoluten Nahhorizont."""
    try:
        slots, metadata = read_bound_json_value(
            forecast_path,
            max_age_s=max(60.0, float(max_age_s)),
            max_bytes=8 * 1024 * 1024,
            copy_data=False,
        )
        if not metadata.get("valid") or not isinstance(slots, list):
            return []
        current_s = time.time() if now_s is None else float(now_s)
        earliest_s = current_s - 15 * 60
        latest_s = current_s + max(15 * 60, float(lookahead_s))
        projected = []
        for slot in slots[:512]:
            if (
                not isinstance(slot, dict)
                or slot.get("forecast_fresh") is not True
                or slot.get("pv_forecast_fresh") is not True
            ):
                continue
            start_ms = _safe_float(slot.get("start_timestamp"), 0.0)
            predicted_kw = _safe_float(slot.get("predicted_kwh"), 0.0)
            if start_ms <= 0 or predicted_kw < 0:
                continue
            start_s = start_ms / 1000.0 if start_ms > 20_000_000_000 else start_ms
            if start_s < earliest_s or start_s > latest_s:
                continue
            utc = time.gmtime(start_s)
            projected.append({
                "h": utc.tm_hour + utc.tm_min / 60.0,
                "w": max(0.0, predicted_kw * 1000.0),
                "start_ts": start_s,
            })
        return projected
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []


def read_e3dc_live_for_energy_manager(
    timeout=10,
    live_path="/var/www/html/ramdisk/live_data_py.json",
    forecast_path="/var/www/html/ramdisk/pv_forecast.json",
    wallbox_path="/var/www/html/ramdisk/wallbox_native.json",
):
    """Liest den kompakten nativen Zustand direkt aus der RAM-Disk.

    ``timeout`` bleibt für Aufruferkompatibilität erhalten. Ein zyklischer
    HTTP-Aufruf des vollständigen Web-Samplers findet bewusst nicht mehr statt.
    """
    del timeout

    data = read_runtime_live_snapshot(
        live_path=live_path,
        wallbox_path=wallbox_path,
        live_max_age_s=15.0,
        wallbox_max_age_s=30.0,
        require_control_valid=True,
        include_web_projection=False,
    )
    if not data:
        raise RuntimeError(
            "live_data_py.json fehlt, ist veraltet oder besitzt keinen "
            "gültigen RSCP-/Netzpunktvertrag"
        )

    mapped = _energy_live_from_ramdisk(data)
    if not mapped:
        raise RuntimeError("live_data_py.json lieferte kein JSON-Objekt")
    mapped["forecast"] = _energy_forecast_from_ramdisk(forecast_path)
    return mapped

def main():
    setup_logging()

    # Initiales Lesen (einmalig beim Start)
    luxtronik_enabled = read_e3dc_config_value('luxtronik', 0) == 1
    wp_ip = read_e3dc_config_value('luxtronik_ip')
    shelly_sg_ip = read_e3dc_config_value('shelly_sg_ip', '')
    shelly_pause_ip = read_e3dc_config_value('shelly_pause_ip', '')

    logger.info("Dienst wird gestartet...")
    wp = None

    # SIGTERM-Handler wird NACH WP-Initialisierung registriert (weiter unten),
    # damit wp im Closure der Handler-Funktion verfuegbar ist.
    # Kein fruehzeitiger Handler noetig — systemd wartet auf TimeoutStopSec.

    # WP-Steuerung initialisieren (Prio: 1. IDM, 2. Luxtronik, 3. Shelly)
    wp_type = int(read_e3dc_config_value('wp_type', -1))
    idm_ip = read_e3dc_config_value('idm_ip', '')
    dimplex_ip = read_e3dc_config_value('dimplex_ip', '')
    has_shelly_heatpump = (
        (shelly_sg_ip and shelly_sg_ip != '0.0.0.0')
        or (shelly_pause_ip and shelly_pause_ip != '0.0.0.0')
    )
    heatpump_start_reservation_max_s = heatpump_start_reservation_duration_s(
        wp_type,
        shelly_sg_ip,
    )

    if wp_type == 1 and idm_ip and luxtronik_enabled:
        try:
            wp = IDMHeatpump(idm_ip)
            logger.info(f"IDM WP-Steuerung aktiv (Modbus-TCP: {idm_ip}).")
        except Exception as e:
            logger.error(f"Fehler bei IDM-Initialisierung: {e}")
    elif wp_type == 5 and dimplex_ip and luxtronik_enabled:
        try:
            wp = DimplexHeatpump(
                dimplex_ip,
                read_e3dc_config_value('dimplex_port', 502),
                read_e3dc_config_value('dimplex_unit_id', 1),
                read_e3dc_config_value('dimplex_sg_register', 5167),
                read_e3dc_config_value('dimplex_modbus_zero_based', 0),
                read_e3dc_config_value('dimplex_sg_heartbeat_s', 300),
                read_e3dc_config_value('dimplex_allow_dark_green', 0),
            )
            logger.info(f"Dimplex WPM Touch SG-Steuerung aktiv (Modbus-TCP: {dimplex_ip}).")
        except Exception as e:
            logger.error(f"Fehler bei Dimplex-Initialisierung: {e}")
    elif wp_type == 0 and luxtronik_enabled and wp_ip and wp_ip != '0.0.0.0':
        try:
            wp = SafeLuxtronik(wp_ip)
            logger.info("Luxtronik-Modul aktiv und verbunden.")
        except Exception as e:
            logger.error(f"Fehler bei Luxtronik-Initialisierung: {e}")
    elif has_shelly_heatpump:
        wp = ShellyHeatpump(shelly_sg_ip, shelly_pause_ip, SHELLY_HEATPUMP_STATE_FILE)
        logger.info(f"Shelly SG-Ready WP-Steuerung aktiv (SG-Ready: {shelly_sg_ip}, EVU-Pause: {shelly_pause_ip}).")
    elif wp_type < 0:
        logger.info("Keine native Waermepumpe aktiv; Energy Manager laeuft nur fuer Smart Charging/Heizstab.")

    # Prüfen, ob der Neustart durch ein Update ausgelöst wurde, um eine Endlosschleife zu verhindern.
    restarted_by_update = False
    update_flag_path = "/var/www/html/ramdisk/em_restarted_by_update.flag"
    if os.path.exists(update_flag_path):
        try:
            restarted_by_update = True
            os.remove(update_flag_path)
            logger.info("Neustart durch Update erkannt. Erster Auto-Update-Check wird übersprungen.")
        except Exception as e:
            logger.warning(f"Konnte Update-Flag nicht entfernen: {e}")

    # Altes Flag im /tmp Verzeichnis sicherheitshalber ignorieren/löschen
    if os.path.exists("/tmp/em_restarted_by_update.flag"):
        try: os.remove("/tmp/em_restarted_by_update.flag")
        except: pass

    # Vorab-Deklaration für send_telegram (Closure Scope)
    TELEGRAM_TOKEN = ''
    TELEGRAM_CHAT_ID = ''

    # Wenn durch Update neugestartet, den ersten Check überspringen.
    update_checked_today = restarted_by_update

    boost_active = False
    price_boost_active = False
    pv_pause_active = False
    pv_pause_start_time = None
    pv_pause_owner = "none"
    source_recovery_pause_context = {}
    pre_pause_active = False
    deficit_start_time = None
    last_day = datetime.now().day
    last_debug_archive_hour = datetime.now().hour
    first_run = True
    daily_boost_counter = 0
    last_pv_boost_time = 0
    last_price_warning_time = 0
    # 0 statt time.time(): Beim Start/Crash-Restart wird der erste Safety-Log
    # sofort ausgegeben. Die 1800s-Bedingung throttelt NUR den Log, nicht den Reset.
    last_safety_check_time = 0
    last_ww_off_guard_log_time = 0.0
    last_wp_command_time = time.time()
    last_e3dc_error_log_time = 0.0
    wp_last_pv_boost_start_ts = 0.0
    wp_last_pv_boost_stop_ts = 0.0
    wp_last_ww_cycle_start_ts = 0.0
    wp_last_ww_cycle_target_c = 0.0
    wp_compressor_was_running = None
    wp_compressor_running_now = False
    wp_compressor_observation_valid = False
    wp_compressor_last_start_ts = 0.0
    wp_compressor_last_stop_ts = 0.0
    wp_compressor_last_run_s = 0.0
    wp_compressor_history_valid = False
    heatpump_positive_signal_started_ts = 0.0
    heatpump_boost_permission_active = False
    heatpump_positive_signal_demand_class = "none"
    heatpump_positive_signal_restored_unconfirmed = False
    heatpump_positive_signal_restart_budget_rearm_pending = False
    heatpump_positive_signal_hold_guard = {}
    heatpump_positive_signal_start_reservation_allowed = False
    luxtronik_ww_budget_loss_guard_state = {}
    luxtronik_ww_budget_loss_blocked_until_fresh_budget = False
    heatpump_budget_demand_active_class = "none"
    heatpump_budget_demand_first_seen_ts = 0.0
    predump_heatpump_started_ts = 0.0
    predump_heatpump_hold_until = 0.0
    heat_price_block_started_ts = 0.0
    heat_policy_boost_delivered_kwh = 0.0
    heat_policy_last_energy_ts = 0.0
    last_wp_takt_log_time = 0.0
    last_source_recovery_release_error_log_time = 0.0
    last_idm_cooling_gate_log_time = 0.0
    pv_boost_pending_start = None
    pv_boost_retry_not_before_ts = 0.0
    heatpump_positive_signal_retry_not_before_ts = 0.0
    pv_boost_last_outcome = {
        "status": "idle",
        "pending": False,
        "attempted": False,
        "confirmed": False,
        "failed": False,
        "command_sent": False,
        "readback_confirmed": False,
        "reason": "",
        "retry_not_before_ts": 0.0,
    }
    pv_pause_pending_end = None
    pv_pause_blocked_until = 0
    legacy_discharge_warned = False
    legacy_autonomy_warned = False
    smart_wbhour_handoff_logged = False

    last_wb_locked = False
    last_wakeup_trigger_time = 0
    e3dc_initialized = False
    guest_car_active = False
    last_history_write = 0
    energy_checkpoint_runtime = {}
    latest_energy_live_state = {}
    car_session_checkpoint_runtime = {1: {}, 2: {}}
    latest_car_sessions = {}
    storage_budget_fallback_replay_guard = {}

    # Web-Push State Trackers
    last_push_soc_sent = False
    last_push_unplugged_sent = False
    last_notstrom_status = 0

    def send_telegram(msg):
        notify_script = "/usr/local/bin/boot_notify.sh"
        if os.path.exists(notify_script):
            try:
                subprocess.run([notify_script, msg], timeout=10)
                return
            except Exception: pass

        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
                requests.post(url, data=data, timeout=5)
            except: pass

    # Restore Status: Docker nutzt /var/www/html/ramdisk als tmpfs. Nach Watchtower
    # Recreate ist luxtronik.json weg; energy_manager_state.json im data-Volume bleibt.
    startup_config = load_e3dc_config_dict()
    startup_auto_mode_enabled = get_cfg_int(startup_config, 'auto_mode', 1) != 0
    restart_revalidation = restored_heatpump_state_contract(
        {},
        ENERGY_STATE_ACTIVE_RESTORE_MAX_AGE_S + 1.0,
        auto_mode_enabled=startup_auto_mode_enabled,
    )
    previous_state = load_previous_energy_state()
    restored_state_source = ""
    if previous_state:
        try:
            saved = previous_state.get("data") or {}
            restored_state_source = previous_state.get("path") or ""
            saved_ts = datetime.fromisoformat(saved["ts"]) if saved.get("ts") else datetime.now()
            if saved_ts.date() == datetime.now().date():
                daily_boost_counter = saved.get('daily_boost_counter', 0)
            last_pv_boost_time = saved.get('last_pv_boost_time', 0)
            last_wp_command_time = _safe_float(saved.get('last_wp_command_time', last_wp_command_time), last_wp_command_time)
            wp_last_pv_boost_start_ts = saved.get('wp_last_pv_boost_start_ts', 0.0)
            wp_last_pv_boost_stop_ts = saved.get('wp_last_pv_boost_stop_ts', 0.0)
            restart_revalidation = restored_heatpump_state_contract(
                saved,
                previous_state.get("age_s", 999999),
                auto_mode_enabled=startup_auto_mode_enabled,
            )
            if restart_revalidation.get(
                "positive_signal_bookkeeping_valid"
            ):
                heatpump_positive_signal_started_ts = _safe_float(
                    restart_revalidation.get(
                        "heatpump_positive_signal_started_ts"
                    ),
                    0.0,
                )
                heatpump_positive_signal_demand_class = str(
                    restart_revalidation.get(
                        "heatpump_positive_signal_demand_class"
                    )
                    or "none"
                )
                heatpump_positive_signal_hold_guard = copy.deepcopy(
                    restart_revalidation.get(
                        "heatpump_positive_signal_hold_guard"
                    )
                    if isinstance(
                        restart_revalidation.get(
                            "heatpump_positive_signal_hold_guard"
                        ),
                        dict,
                    )
                    else {}
                )
                # Eine persistierte Signalkante darf nie die 150-s-Vollreserve
                # neu bewaffnen. Diese Lease gehört ausschließlich dem
                # Storage Manager.
                heatpump_positive_signal_start_reservation_allowed = False
                heatpump_positive_signal_restored_unconfirmed = True
                heatpump_positive_signal_restart_budget_rearm_pending = True
                heatpump_budget_demand_active_class = (
                    heatpump_positive_signal_demand_class
                )
                restored_demand_first_seen = _strict_storage_budget_ts(
                    saved.get("heatpump_budget_demand_first_seen_ts")
                )
                heatpump_budget_demand_first_seen_ts = (
                    restored_demand_first_seen
                    if restored_demand_first_seen is not None
                    and restored_demand_first_seen <= time.time()
                    else time.time()
                )
            saved_ww_budget_loss_guard = saved.get(
                "luxtronik_ww_budget_loss_guard_state"
            )
            saved_ww_budget_loss_pending = bool(
                saved.get(
                    "luxtronik_ww_budget_loss_blocked_until_fresh_budget"
                )
                is True
                or (
                    isinstance(saved_ww_budget_loss_guard, dict)
                    and (
                        saved_ww_budget_loss_guard.get("blocked") is True
                        or bool(saved_ww_budget_loss_guard.get("timer_guard"))
                    )
                )
            )
            if saved_ww_budget_loss_pending:
                # Ein Neustart darf die Ablaufzeit einer bereits budgetlosen
                # 55-°C-Lease nicht neu minten. Bis zu einem frischen
                # startfähigen Folgebudget bleibt sie daher konservativ
                # abgelaufen; ein physisch belegter WW-Lauf wird unten dennoch
                # geschützt.
                luxtronik_ww_budget_loss_guard_state = {
                    "blocked": True,
                    "effective_block": False,
                    "timer_guard": {},
                    "runtime_state": "unknown",
                    "reason": "restart_budget_loss_conservative",
                    "evidence_limit": True,
                }
                luxtronik_ww_budget_loss_blocked_until_fresh_budget = True
            wp_last_ww_cycle_start_ts = restart_revalidation["ww_cycle_started_ts"]
            wp_last_ww_cycle_target_c = restart_revalidation["ww_cycle_target_c"]
            pv_pause_blocked_until = restart_revalidation["pv_pause_blocked_until"]
            predump_heatpump_started_ts = saved.get('predump_heatpump_started_ts', 0.0)
            predump_heatpump_hold_until = saved.get('predump_heatpump_hold_until', 0.0)
            heat_price_block_started_ts = saved.get('heat_price_block_started_ts', 0.0)
            heat_policy_boost_delivered_kwh = saved.get('heat_policy_boost_delivered_kwh', 0.0)
            if restart_revalidation["active_command_saved"]:
                logger.warning(
                    "%s: %s (%s)",
                    restart_revalidation["diagnostic_signature"],
                    restart_revalidation["reason"],
                    os.path.basename(restored_state_source),
                )
            elif restart_revalidation["ww_hold_restored"] or restart_revalidation["pause_hold_restored"]:
                logger.info(
                    "%s: Nur konservative Sicherheits-Holds wiederhergestellt (%s)",
                    restart_revalidation["diagnostic_signature"],
                    os.path.basename(restored_state_source),
                )

            last_notstrom_status = saved.get('last_notstrom_status', 0)
        except Exception as exc:
            logger.debug("Energy-Manager-Status konnte nicht wiederhergestellt werden: %s", exc)

    cleanup_legacy_energy_state_file()

    # Init Ramdisk
    init_json = {"ts": datetime.now().isoformat(), "success": False, "error": "Dienst startet...", "data": {}, "status": {}}
    write_json_atomic_tolerant(RAMDISK_FILE, init_json, mode=0o664, warn_label="Luxtronik-Live-Datei")
    shelly_startup_sync_pending = bool(has_shelly_heatpump and wp)
    shelly_startup_sync_source = os.path.basename(restored_state_source) if restored_state_source else "cold_start"
    last_shelly_startup_sync_log_time = 0.0

    # Config Cache Init
    config_cache = startup_config
    if os.path.exists(V4_CONFIG_PATH):
        config_mtime = os.path.getmtime(V4_CONFIG_PATH)
    else:
        config_mtime = 0

    auto_mode_init = get_cfg_int(config_cache, 'auto_mode', 1)
    grid_limit_init = get_cfg_value(config_cache, 'GRID_START_LIMIT', -3500)

    if auto_mode_init == 0: logger.info("Automatik-Regelung ist DEAKTIVIERT (Nur Monitoring).")
    elif not wp: logger.info("Nur Lademanagement-Regeln sind aktiv.")
    else: logger.info(f"Start bei > {abs(grid_limit_init)}W Einspeisung.")

    script_start_time = time.time()
    last_wakeup_trigger_time = 0
    last_wb_locked = False
    last_wb2_locked = False
    guest_car_active = False
    last_history_write = 0
    last_push_awattar_sent = False

    import signal
    import sys
    def handle_sigterm(sig, frame):
        logger.info("Dienst wird beendet (SIGTERM). Schliesse Verbindung sauber ab...")
        try:
            if latest_energy_live_state:
                persist_energy_restart_checkpoint(
                    ENERGY_STATE_FILE,
                    latest_energy_live_state,
                    energy_checkpoint_runtime,
                    force=True,
                )
            for wb_id, session in tuple(latest_car_sessions.items()):
                if session:
                    persist_car_session_checkpoint(
                        car_session_paths(wb_id)["checkpoint"],
                        session,
                        car_session_checkpoint_runtime.setdefault(int(wb_id), {}),
                        force=True,
                    )
        except Exception as checkpoint_exc:
            logger.error("Restart-Checkpoint beim Beenden fehlgeschlagen: %s", checkpoint_exc)
        # wp ist direkt aus dem aeusseren Scope (Closure) zugaenglich — kein globals() noetig!
        if wp is not None and hasattr(wp, 'close'):
            try:
                wp.close()
                logger.info("WP-Verbindung geschlossen.")
            except Exception as e:
                logger.error(f"Fehler beim Schliessen der WP-Verbindung: {e}")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    while True:
        now = datetime.now()
        cycle_actions = []
        price_action = "NONE"
        predump_heatpump_raw_active = False
        predump_heatpump_active = False
        predump_heatpump_targets_reached = False
        predump_heatpump_protect_block = False
        predump_heatpump_hold_active = bool(predump_heatpump_hold_until and time.time() < predump_heatpump_hold_until)
        market_heatpump_release = {"allowed": False}
        market_heatpump_active = False
        market_heatpump_requested = False
        legacy_price_heatpump_active = False
        price_heatpump_start_block_remaining_s = 0.0
        price_heatpump_stop_block_remaining_s = 0.0
        price_heatpump_takt_start_blocked = False
        price_heatpump_takt_stop_held = False
        storage_manager_owns_energy = True
        energy_autonomy_allowed = False
        local_autonomy_blocked = []
        car_blocks_boost_applied = False
        heat_policy_decision = None
        heat_forecast_result = None
        heat_intent_export, heat_intent_runtime_validation = (
            build_heat_intent_shadow_projection({}, now_ts=time.time())
        )
        legacy_heat_automation_owner = (
            build_legacy_heat_automation_owner_contract(
                runtime_enabled=False,
                auto_mode=False,
                actuator_write_allowed=False,
                storage_budget_contract={},
                now_ts=time.time(),
            )
        )
        ww_timer_automation_owner = (
            build_legacy_heat_automation_owner_contract(
                runtime_enabled=False,
                auto_mode=False,
                actuator_write_allowed=False,
                storage_budget_contract={},
                now_ts=time.time(),
            )
        )
        heat_runtime_actuation = heat_runtime_actuation_contract(
            False,
            validated_intent=heat_intent_export,
            runtime_validation=heat_intent_runtime_validation,
            policy_owner=None,
            legacy_owner_contract=legacy_heat_automation_owner,
            now_ts=time.time(),
        )
        automatic_heat_actuation_allowed = False
        heat_policy_price_gate_reason = ""
        heat_policy_runtime_enabled = False
        manual_ww_active = False
        ww_timer_window_active = False
        ww_timer_target_c = None
        heatpump_budget_demand_class = "none"
        heatpump_budget_demand_target_c = None
        heatpump_positive_output_blocked_this_cycle = False
        heatpump_positive_output_block_reasons = []
        heatpump_positive_signal_window = build_heatpump_positive_signal_window(
            heatpump_positive_signal_started_ts,
            compressor_running=wp_compressor_running_now,
            signal_readback_confirmed=(
                not heatpump_positive_signal_restored_unconfirmed
            ),
            signal_hold_guard=heatpump_positive_signal_hold_guard,
            clock_sample=control_time.sample(),
            start_reservation_allowed=(
                heatpump_positive_signal_start_reservation_allowed
            ),
            start_reservation_max_s=heatpump_start_reservation_max_s,
            now_ts=time.time(),
        )
        heatpump_positive_signal_hold_guard = copy.deepcopy(
            heatpump_positive_signal_window.get("hold_guard") or {}
        )
        if heatpump_positive_signal_started_ts <= 0.0:
            heatpump_boost_permission_active = False
        heatpump_positive_signal_restart_readback = {
            "schema_version": "heatpump_positive_actuator_readback_v1",
            "state_confirmed": False,
            "positive_confirmed": False,
            "nonpositive_confirmed": False,
            "provider": "none",
            "ts": 0.0,
            "state": None,
            "reason": "restart_positive_signal_not_pending",
        }
        heatpump_positive_signal_actuator_readback = {
            "schema_version": "heatpump_positive_actuator_readback_v1",
            "state_confirmed": False,
            "positive_confirmed": False,
            "nonpositive_confirmed": False,
            "provider": "none",
            "ts": 0.0,
            "state": None,
            "reason": "positive_signal_not_active",
        }
        central_heatpump_start_budget_gate = {
            "schema_version": "central_heatpump_start_budget_gate_v1",
            "allowed": False,
            "reason_code": "HEATPUMP_POSITIVE_DEMAND_MISSING",
            "blockers": ["HEATPUMP_POSITIVE_DEMAND_MISSING"],
        }
        central_heatpump_command_cap_w = 0
        central_heatpump_effective_budget_w = 0
        central_heatpump_effective_budget_source = "none"
        automatic_heat_start_allowed = False
        running_heatpump_observed_w = 0
        running_heatpump_accounting_w = 0
        running_heatpump_accounting_valid = False
        running_heatpump_budget_tolerance_w = 250
        running_heatpump_underfunded = False

        # ML-Prognose: Jeden Tag um 00:06 Uhr laden (Notifier erstellt sie um 00:05)
        if now.hour == 0 and now.minute == 6 and now.second < 5:
            ml_data = get_ml_prediction()
            if ml_data:
                logger.info(f"🧠 KI Tages-Prognose geladen: Haus ~{ml_data.get('home_kwh', 0):.1f} kWh | WP ~{ml_data.get('wp_kwh', 0):.1f} kWh")
            time.sleep(5) # Verhindert Mehrfach-Ausführung in der gleichen Minute

        # HA Standby Check: Wenn dieser Pi der Slave ist, darf er nicht auf Modbus zugreifen!
        is_standby = False
        try:
            if os.path.exists("/var/www/html/ramdisk/ha_status.json"):
                with open("/var/www/html/ramdisk/ha_status.json", "r") as f:
                    ha_data = json.load(f)
                    is_standby = (ha_data.get('mode') == 'slave' and ha_data.get('state') != 'failover')
        except: pass

        if is_standby:
            if wp: wp.close() # Modbus-Schnittstelle sofort für den Master freigeben!
            time.sleep(10)
            continue # Schleife abbrechen, nichts regel

        wp_write_allowed = (time.time() - script_start_time) > 65

        # 1. Konfiguration nur bei Dateiänderung neu laden (Smart Caching)
        try:
            if os.path.exists(V4_CONFIG_PATH):
                current_mtime = os.path.getmtime(V4_CONFIG_PATH)
                if current_mtime != config_mtime:
                    config_cache = load_e3dc_config_dict()
                    config_mtime = current_mtime
                    logger.info("Konfiguration aktualisiert (Dateiaenderung erkannt).")
        except Exception as e:
            logger.error(f"Fehler beim Konfigurations-Check: {e}")

        current_config = config_cache

        try:
            # Dynamisches Nachladen wichtiger Parameter sicher im Try-Block
            AUTO_UPDATE_ENABLE = get_cfg_int(current_config, 'auto_update_enable', 0)
            update_time_str = str(get_cfg_value(current_config, 'auto_update_time', "23:00"))
            try: update_hour, update_minute = map(int, update_time_str.split(':'))
            except: update_hour, update_minute = 23, 0

            GRID_START_LIMIT = get_cfg_value(current_config, 'GRID_START_LIMIT', -3500)
            PV_BOOST_DELAY = get_cfg_int(current_config, 'pv_boost_delay', 30)
            STOP_DELAY_MINUTES = get_cfg_int(current_config, 'stop_delay_minutes', 10)
            WP_MIN_RUNTIME_MIN = get_cfg_value(current_config, 'wp_min_runtime_min', 30.0)
            WP_RESTART_BLOCK_MIN = get_cfg_value(current_config, 'wp_restart_block_min', 20.0)
            PREDUMP_HEATPUMP_MIN_RUNTIME_MIN = max(0.0, get_cfg_value(current_config, 'predump_heatpump_min_runtime_min', 60.0))
            WP_TAKT_PROTECT = (int(wp_type) in (0, 1, 3, 5) or has_shelly_heatpump) and (WP_MIN_RUNTIME_MIN > 0 or WP_RESTART_BLOCK_MIN > 0)
            MIN_SOC = get_cfg_value(current_config, 'MIN_SOC', 80)
            AUTO_MODE = get_cfg_int(current_config, 'auto_mode', 1)
            HEIZGRENZE_TEMP = get_cfg_value(current_config, 'HEIZGRENZE_TEMP', 10.0)
            storage_manager_owns_energy = get_cfg_int(current_config, 'storage_manager_owns_energy_distribution', 1) == 1
            energy_autonomy_allowed = (
                not storage_manager_owns_energy
                or get_cfg_int(current_config, 'energy_manager_legacy_autonomy_enable', 0) == 1
            )
            # Gecachte Sollwerte für die Schleife
            CONF_WWS = get_cfg_value(current_config, 'WWS', 50.0)
            CONF_WWW = get_cfg_value(current_config, 'WWW', 48.0)
            CONF_HZ  = get_cfg_value(current_config, 'HZ', 32.0)
            CONF_KHL = get_cfg_value(current_config, 'KHL', 16.0)
            IDM_COOLING_BOOST_MIN_AT = get_cfg_value(current_config, 'idm_cooling_boost_min_at', 23.0)
            IDM_PV_SURPLUS_ENABLE = get_cfg_int(current_config, 'idm_pv_surplus_enable', 1)
            IDM_PV_SURPLUS_MAX_KW = get_cfg_value(current_config, 'idm_pv_surplus_max_kw', 2.0)
            IDM_PV_SURPLUS_MIN_KW = get_cfg_value(current_config, 'idm_pv_surplus_min_kw', 0.8)
            IDM_PV_SURPLUS_RAMP_KW = get_cfg_value(current_config, 'idm_pv_surplus_ramp_kw', 0.2)
            IDM_PV_SURPLUS_DEADBAND_KW = get_cfg_value(current_config, 'idm_pv_surplus_deadband_kw', 0.1)
            IDM_PV_SURPLUS_HEARTBEAT_S = get_cfg_value(current_config, 'idm_pv_surplus_heartbeat_s', 60.0)
            IDM_PV_SURPLUS_MIN_WRITE_INTERVAL_S = get_cfg_value(current_config, 'idm_pv_surplus_min_write_interval_s', 10.0)
            if wp_type == 1 and wp and hasattr(wp, 'configure_surplus'):
                wp.configure_surplus(
                    IDM_PV_SURPLUS_ENABLE,
                    IDM_PV_SURPLUS_MAX_KW,
                    IDM_PV_SURPLUS_MIN_KW,
                    IDM_PV_SURPLUS_RAMP_KW,
                    IDM_PV_SURPLUS_DEADBAND_KW,
                    IDM_PV_SURPLUS_HEARTBEAT_S,
                    IDM_PV_SURPLUS_MIN_WRITE_INTERVAL_S
                )

            # WW Software-Timer
            WW_TIMER_ENABLE = get_cfg_int(current_config, 'ww_timer_enable', 0)
            WW_VON = get_cfg_value(current_config, 'wwvon', 0.0)
            WW_BIS = get_cfg_value(current_config, 'wwbis', 24.0)
            WW_NORMAL = get_cfg_value(current_config, 'ww_normal', 45.0)
            WW_ECO = get_cfg_value(current_config, 'ww_eco', 35.0)
            WW_CIRC_VON = get_cfg_value(current_config, 'ww_circ_von', 5.5)
            WW_CIRC_BIS = get_cfg_value(current_config, 'ww_circ_bis', 20.0)
            WW_CIRC_ON = get_cfg_int(current_config, 'ww_circ_on', 5)
            WW_CIRC_OFF = get_cfg_int(current_config, 'ww_circ_off', 25)
            WW_CIRC_BOOST = get_cfg_int(current_config, 'ww_circ_boost', 0)
            WW_SOFORT_DURATION = get_cfg_int(current_config, 'ww_sofort_duration', 120)

            # Der allgemeine Wärme-Preis-Boost ist in dieser Migrationsphase
            # ausschließlich ein Candidate/Shadow. Dieser Nutzerwunsch darf
            # weder durch die historische Energy-Manager-Autonomie noch durch
            # einen NT-Zeitpfad direkt in einen Aktorbefehl übersetzt werden.
            heat_price_boost_candidate_requested = (
                get_cfg_int(current_config, 'price_boost_enable', 0) == 1
            )
            PRICE_LIMIT = get_cfg_value(current_config, 'price_limit', 20.0)
            PRICE_MIN_DURATION = get_cfg_value(current_config, 'price_min_duration', 60)
            PRICE_MAX_DAILY = get_cfg_value(current_config, 'price_max_daily', 180)
            PRICE_HARD_LIMIT = get_cfg_value(current_config, 'price_hard_limit', -99.0)
            PRICE_PAUSE_LIMIT = get_cfg_value(current_config, 'price_pause_limit', 35.0)
            HEAT_POLICY_RUNTIME_ENABLE = get_cfg_int(current_config, 'heat_policy_runtime_enable', 0)
            heat_policy_runtime_enabled = HEAT_POLICY_RUNTIME_ENABLE == 1
            heat_runtime_actuation = heat_runtime_actuation_contract(
                heat_policy_runtime_enabled,
                validated_intent=heat_intent_export,
                runtime_validation=heat_intent_runtime_validation,
                policy_owner=None,
                legacy_owner_contract=legacy_heat_automation_owner,
                now_ts=time.time(),
            )
            automatic_heat_actuation_allowed = (
                automatic_heat_policy_actuation_allowed(
                    heat_policy_runtime_enabled,
                    heat_runtime_actuation,
                )
            )
            WQ_MIN_TEMP = get_cfg_value(current_config, 'wq_min_temp', 1.0)
            RL_SOURCE = str(get_cfg_value(current_config, 'rl_source', 'internal'))
            MANUAL_BOOST_MIN_SOC = get_cfg_value(current_config, 'manual_boost_min_soc', 25)
            MANUAL_BOOST_MAX_DURATION = get_cfg_value(current_config, 'manual_boost_max_duration', 180)

            legacy_mb_enable = get_cfg_int(current_config, 'morning_boost_enable', 0)
            legacy_si_enable = get_cfg_int(current_config, 'super_intelligence_enable', 0)
            if not legacy_discharge_warned and (legacy_mb_enable == 1 or legacy_si_enable == 1):
                logger.info("Legacy-Speicherentladung im Energy Manager ist deaktiviert; Storage Manager/Simulator steuern Pre-Dump und Ladekurve.")
                legacy_discharge_warned = True

            SMART_WBHOUR_ENABLE_RAW = get_cfg_int(current_config, 'smart_wbhour_enable', 0)
            SMART_WBHOUR_ENABLE = SMART_WBHOUR_ENABLE_RAW if energy_autonomy_allowed else 0
            try: CAR_CAPACITY = float(get_cfg_value(current_config, 'car_capacity', 72.0))
            except: CAR_CAPACITY = 72.0
            try: CAR_TARGET_SOC = float(get_cfg_value(current_config, 'car_target_soc', 80.0))
            except: CAR_TARGET_SOC = 80.0
            try: CAR_CHARGE_POWER = float(get_cfg_value(current_config, 'car_charge_power', 11.0))
            except: CAR_CHARGE_POWER = 11.0

            V2H_ENABLE = get_cfg_int(current_config, 'v2h_enable', 0)
            V2H_MIN_SOC = float(get_cfg_value(current_config, 'v2h_min_soc', 40.0))
            V2H_BAT_SOC_LIMIT = float(get_cfg_value(current_config, 'v2h_bat_soc_limit', 10.0))

            BL_IGNORE_PLUG = get_cfg_int(current_config, 'bluelink_ignore_plug_status', 0)

            TELEGRAM_TOKEN = str(get_cfg_value(current_config, 'telegram_token', ''))
            TELEGRAM_CHAT_ID = str(get_cfg_value(current_config, 'telegram_chat_id', ''))

            PV_PAUSE_ENABLE_RAW = get_cfg_int(current_config, 'pv_pause_enable', 0)
            PV_PAUSE_ENABLE = PV_PAUSE_ENABLE_RAW if energy_autonomy_allowed else 0
            PV_PAUSE_SOC = get_cfg_value(current_config, 'pv_pause_soc', 80)
            PV_PAUSE_WATT = get_cfg_value(current_config, 'pv_pause_watt', 3000.0)
            PV_PAUSE_TIMEOUT_MINUTES = get_cfg_value(current_config, 'pv_pause_timeout_minutes', 120)
            PV_PAUSE_MIN_AT = get_cfg_value(current_config, 'pv_pause_min_at', 0.0)
            PV_PAUSE_MAX_TEMP_DROP = get_cfg_value(current_config, 'pv_pause_max_temp_drop', 4.0)
            LUXTRONIK_PAUSE_SETPOINT_C = max(
                15.0,
                min(22.0, get_cfg_value(current_config, 'luxtronik_pause_setpoint_c', 20.0)),
            )
            PAUSE_SETPOINT_C = LUXTRONIK_PAUSE_SETPOINT_C if wp_type == 0 else 20.0
            SOURCE_RECOVERY_REQUEST_GRACE_S = max(90.0, min(300.0, float(PV_BOOST_DELAY) * 3.0))
            local_autonomy_blocked = []
            if heat_price_boost_candidate_requested:
                local_autonomy_blocked.append(
                    "allgemeine Wärmepumpen-Preisverschiebung (nur Candidate/Shadow)"
                )
            if not energy_autonomy_allowed:
                if PV_PAUSE_ENABLE_RAW == 1:
                    local_autonomy_blocked.append("PV-Pause")
                if SMART_WBHOUR_ENABLE_RAW == 1:
                    local_autonomy_blocked.append("Smart-wbhour")
                if legacy_mb_enable == 1 or legacy_si_enable == 1:
                    local_autonomy_blocked.append("Legacy-Morning/SI")
            if local_autonomy_blocked and not legacy_autonomy_warned:
                logger.info(
                    "Zentrale Besitzerregel aktiv: Storage Manager entscheidet Energieverteilung; "
                    "Energy Manager führt nur gebundene Freigaben aus. Blockierte lokale Autonomie: %s. "
                    "Pre-Dump-, Negativpreisplan- und manuelle Freigaben bleiben getrennt erlaubt.",
                    ", ".join(local_autonomy_blocked),
                )
                legacy_autonomy_warned = True

            # Auto-Update Check oder Update-Benachrichtigung
            push_notify_updates = str(get_cfg_value(current_config, 'push_notify_updates', '0')).lower() in ['1', 'true']

            if AUTO_UPDATE_ENABLE == 1 or push_notify_updates:
                if now.hour == update_hour and now.minute == update_minute:
                    if not update_checked_today:
                        logger.info(f"Starte tägliche Update-Prüfung ({update_hour:02d}:{update_minute:02d} Uhr)...")
                        install_root = os.path.abspath(os.path.join(script_dir, "../../"))

                        if AUTO_UPDATE_ENABLE == 1:
                            if os.path.isfile("/.dockerenv"):
                                logger.error(
                                    "Auto-Update im Container ist gesperrt; "
                                    "der Release-Wechsel muss auf dem Docker-Host erfolgen."
                                )
                            elif os.path.isfile(UPDATE_DISPATCHER) and os.access(UPDATE_DISPATCHER, os.X_OK):
                                try:
                                    log_file = os.path.join(LOG_DIR, "auto_self_update.log")
                                    cmd = (
                                        [UPDATE_DISPATCHER]
                                        if os.geteuid() == 0
                                        else ["/usr/bin/sudo", "-n", "--", UPDATE_DISPATCHER]
                                    )

                                    with open(log_file, "w") as f:
                                        f.write(f"=== Starting Auto-Update at {datetime.now()} ===\n")
                                        f.write("Command: " + " ".join(cmd) + "\n---\n")
                                    os.chmod(log_file, 0o664)

                                    logger.info("Übergebe Auto-Update an den root-eigenen Hintergrund-Dispatcher.")
                                    with open(log_file, "a") as output:
                                        result = subprocess.run(
                                            cmd,
                                            stdin=subprocess.DEVNULL,
                                            stdout=output,
                                            stderr=subprocess.STDOUT,
                                            timeout=30,
                                            check=False,
                                        )
                                    if result.returncode == 0:
                                        logger.info(
                                            "Update gestartet: e3dc-web-update.service; "
                                            "Status: systemctl status --no-pager e3dc-web-update.service; "
                                            "Protokoll: journalctl -fu e3dc-web-update.service; "
                                            "Dateilog: tail -f /var/log/e3dc-control/web-update.log"
                                        )
                                    else:
                                        logger.error(
                                            "Update-Dispatcher lehnte den Auftrag ab (Exit %s); "
                                            "Details: %s",
                                            result.returncode,
                                            log_file,
                                        )
                                except Exception as e:
                                    logger.error(f"Fehler beim Starten des Auto-Updates: {e}")
                            else:
                                logger.error(
                                    "Auto-Update fehlgeschlagen: root-eigener Update-Dispatcher "
                                    "fehlt oder ist nicht ausführbar (%s).",
                                    UPDATE_DISPATCHER,
                                )

                        # Nur Benachrichtigung (kein Auto-Update)
                        elif push_notify_updates:
                            try:
                                # 1. Fetch remote (kurzer Prozess)
                                subprocess.run(["git", "-C", install_root, "fetch"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                                # 2. Count new commits
                                res = subprocess.run(["git", "-C", install_root, "rev-list", "--count", "HEAD..@{u}"], capture_output=True, text=True, timeout=5)
                                if res.returncode == 0:
                                    commits_behind = int(res.stdout.strip())
                                    if commits_behind > 0:
                                        # "Nur 1x benachrichtigen, auch wenn weitere Commits hinzukommen" (vergleiche lokalen HEAD Hash)
                                        local_hash_res = subprocess.run(["git", "-C", install_root, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
                                        if local_hash_res.returncode == 0:
                                            local_hash = local_hash_res.stdout.strip()
                                            notified_file = "/var/www/html/ramdisk/last_notified_update_hash.txt"

                                            already_notified = False
                                            if os.path.exists(notified_file):
                                                with open(notified_file, "r") as nf:
                                                    if nf.read().strip() == local_hash:
                                                        already_notified = True

                                            if not already_notified:
                                                actions = json.dumps([
                                                    {"action": "update_now", "title": "Jetzt Aktualisieren"}
                                                ])
                                                send_webpush('push_notify_updates', 'Neues Update verfügbar!', f"Es gibt {commits_behind} neue Änderung(en) für E3DC-Control. Bitte im System-Menü aktualisieren.", url="/logs.php", actions=actions)
                                                with open(notified_file, "w") as nf:
                                                    nf.write(local_hash)
                            except Exception as e:
                                logger.error(f"Fehler bei Update-Prüfung für Web-Push: {e}")

                        update_checked_today = True
                else:
                    update_checked_today = False

            wp_data = {}
            wp_status = {}
            wp_compressor_running_now = False
            wp_compressor_observation_valid = False
            luxtronik_ww_runtime_contract = {
                "state": "unknown",
                "ww_running": False,
                "compressor_running": False,
                "reason": "runtime_not_sampled",
            }
            luxtronik_ww_budget_loss_effective_block = False
            wp_live_ts = None
            wp_live_age_s = None
            wp_live_fresh = False
            at = 20.0
            at_mittel = at
            success = False
            wq_aus = 10.0
            wp_error_msg = ""
            if not wp: success = True

            # 1. Daten von WP holen
            try:
                with open(WS_JSON_FILE, "r", encoding="utf-8") as f:
                    wp_live_ts = os.path.getmtime(WS_JSON_FILE)
                    wp_live_freshness = heatpump_native_source_freshness(
                        wp_live_ts,
                        now_ts=time.time(),
                        max_age_s=HEATPUMP_LIVE_REVALIDATION_MAX_AGE_S,
                    )
                    wp_live_age_s = wp_live_freshness["source_age_s"]
                    wp_live_fresh = wp_live_freshness["source_fresh"]
                    raw_ws_data = json.load(f)
                    native_live = normalize_native_heatpump_live_payload(
                        raw_ws_data,
                        wp_type,
                    )
                    ws_data = native_live["data"]
                    # Mappe das WebSocket-JSON auf die alten Modbus-Keys für Kompatibilität, aber behalte Originale!
                    wp_data = dict(ws_data)



                    # Helper Funktion um nur echte Werte zu mappen ohne Fake-Defaults wie 30.0 / 10.0 zu erzeugen
                    def _map(target, sources, transform=None):
                        for s in sources:
                            if s in ws_data and ws_data[s] is not None:
                                wp_data[target] = transform(ws_data[s]) if transform else ws_data[s]
                                return

                    _map('Aussentemp', ['Außentemperatur'])
                    _map('Aussentemp_Mittel', ['Außentemperatur_Mittel', 'Gemittelte Außentemperatur', 'Mitteltemperatur'])
                    _map('Sole_Ein', ['Wärmequelle-Ein', 'Zuluft'])
                    _map('Sole_Aus', ['Wärmequelle-Aus'])
                    _map('Vorlauf_Ist', ['Vorlauf_Ist', 'Vorlauf'])
                    _map('Ruecklauf_Ist', ['Ruecklauf_Ist', 'Rücklauf'])
                    _map('Ruecklauf_Soll', ['Ruecklauf_Soll', 'Rückl.-Soll'])
                    _map('Ruecklauf_Extern', ['Ruecklauf_Extern', 'Rückl.-Extern'])
                    _map('Warmwasser_Ist', ['Warmwasser-Ist', 'Warmwasser_Ist'])
                    _map('Warmwasser_Soll', ['Warmwasser-Soll', 'Warmwasser_Soll'])
                    _map('Leistung_Heiz_kW', ['Heizleistung Ist', 'Leistung_Heiz_kW'])
                    _map('Leistung_Verdichter_W', ['Leistungsaufnahme'], lambda x: x * 1000.0)
                    _map('Verdichter_Ein', ['Verdichter', 'Verdichter 1'])
                    _map('Energie_Waerme_kWh', ['Wärmemenge Gesamt', 'Wärmemenge_Gesamt'])
                    _map('Energie_Elek_kWh', ['Leistungsaufnahme_Gesamt', 'Leistungsaufnahme Gesamt'])
                    _map('Freq_Ist', ['Freq_Ist', 'Freq. aktuell'])
                    _map('Freq_Soll', ['Freq_Soll', 'Freq. Sollwert'])

                    # Der WebSocket liefert auf manchen Luxtronik-Versionen nur
                    # Waermequelle-Ein. Sole_Aus kommt dann direkt aus Modbus 10111.
                    if hasattr(wp, 'read_source_temperatures'):
                        _src_temps = wp.read_source_temperatures()
                        for _k, _v in _src_temps.items():
                            if _v is not None and (wp_data.get(_k) is None or wp_data.get(_k) == ''):
                                wp_data[_k] = _v

                    operating_mode_ws = normalize_luxtronik_operating_mode(ws_data.get('Betriebszustand'))
                    if operating_mode_ws is not None:
                        wp_data['Betriebsart'] = operating_mode_ws

                    hz_mode_raw = native_live["hz_mode_raw"]
                    ww_mode_raw = native_live["ww_mode_raw"]
                    shi_hz_mode = normalize_luxtronik_shi_mode(hz_mode_raw)
                    shi_ww_mode = normalize_luxtronik_shi_mode(ww_mode_raw)
                    has_valid_status_data = bool(
                        wp_live_fresh
                        and native_live["provider_valid"]
                        and (shi_hz_mode is not None or shi_ww_mode is not None)
                    )

                    # Pausiere Steuerung, wenn WP manuell in den Ferien- oder Frostschutzmodus versetzt wurde
                    wp_is_vacation = False
                    for m_str in [str(hz_mode_raw).lower(), str(ww_mode_raw).lower()]:
                        if any(x in m_str for x in ['ferien', 'urlaub', 'frost']):
                            wp_is_vacation = True

                    if wp_is_vacation:
                        wp_write_allowed = False

                    # Luxtronik hat den statischen "Sollwert Warmw." und das aktuelle Live-Ziel "Warmwasser-Soll"
                    ww_soll = ws_data.get('Warmwasser-Soll', ws_data.get('Sollwert Warmw.', 45.0))
                    if not isinstance(ww_soll, (int, float)): ww_soll = 45.0

                    hz_soll = ws_data.get('Sollwert Heizen', 20.0)
                    if not isinstance(hz_soll, (int, float)): hz_soll = 20.0

                    wp_status = {
                        'valid': has_valid_status_data,
                        'source_fresh': wp_live_fresh,
                        'source_ts': wp_live_ts,
                        'source_age_s': wp_live_age_s,
                        'source': native_live["source"] or None,
                        'provider_valid': native_live["provider_valid"],
                        'power_source': ws_data.get('stiebel_power_source'),
                        'SHI_HZ_Mode': shi_hz_mode,
                        'SHI_HZ_Mode_Raw': hz_mode_raw,
                        'HZ_Mode': shi_hz_mode,
                        'HZ_Setpoint': float(hz_soll),
                        'SHI_WW_Mode': shi_ww_mode,
                        'SHI_WW_Mode_Raw': ww_mode_raw,
                        'WW_Mode': shi_ww_mode,
                        'WW_Setpoint': float(ww_soll),
                        # Zirkulationsstatus aus WebSocket: "Ein"->1, "Aus"->0, "---"->None
                        'CIRC_Mode': (1 if 'ein' in str(ws_data.get('Zirkulation', '---')).lower()
                                      else (0 if 'aus' in str(ws_data.get('Zirkulation', '---')).lower()
                                            else None))
                    }
            except Exception as e:
                logger.debug(f"Konnte waermepumpe.json nicht lesen: {e}")

            restart_was_valid = not bool(restart_revalidation.get("safe_stop_required", True))
            if isinstance(wp, ShellyHeatpump):
                if bool(restart_revalidation.get("actuator_state_valid")) and bool(AUTO_MODE):
                    restart_revalidation = revalidate_restored_heatpump_state(
                        restart_revalidation,
                        live_state_valid=False,
                        actuator_state_valid=True,
                        auto_mode_enabled=True,
                        validation_source="shelly_relay_readback",
                    )
                else:
                    restart_revalidation = revalidate_shelly_restart_state(
                        restart_revalidation,
                        wp,
                        auto_mode_enabled=bool(AUTO_MODE),
                    )
            else:
                restart_revalidation = revalidate_restored_heatpump_state(
                    restart_revalidation,
                    live_state_valid=bool(wp_status.get("valid")),
                    auto_mode_enabled=bool(AUTO_MODE),
                )
            if isinstance(wp, ShellyHeatpump):
                # Reine Anzeigeevidenz: Der zyklische Readback verändert weder
                # den Sollcache noch die Policy und löst niemals einen Write aus.
                wp.refresh_sg_readback_if_due()
            if heatpump_positive_signal_restored_unconfirmed:
                heatpump_positive_signal_restart_readback = (
                    heatpump_positive_actuator_readback(
                        {
                            "wp": wp,
                            "wp_type": wp_type,
                            "wp_status": wp_status,
                            "wp_data": wp_data,
                            "current_config": current_config,
                        },
                        now_ts=time.time(),
                    )
                )
                restart_revalidation["positive_signal_readback"] = copy.deepcopy(
                    heatpump_positive_signal_restart_readback
                )
                reconcile_heatpump_driver_cache_from_readback(
                    wp,
                    wp_type,
                    heatpump_positive_signal_restart_readback,
                )
                if heatpump_positive_signal_restart_readback.get(
                    "positive_confirmed"
                ) is True:
                    # Nur der frische non-normale Aktor-Readback macht aus der
                    # restaurierten Zeit-/Demand-Buchhaltung wieder eine
                    # laufende Signalhaltezeit. Es wird kein Write ausgelöst.
                    # Die lokale Freigabelatch wird dabei aus genau diesem
                    # Readback rehydriert. Die zentrale Startfreigabe bleibt
                    # getrennt: Sie darf bei 0 W nicht als neue Startquelle
                    # dienen, während der bestehende Aktorzustand nach einem
                    # Prozessneustart weiterhin bis zum Safety-/Hold-Entscheid
                    # beobachtet werden muss.
                    heatpump_boost_permission_active = True
                    heatpump_positive_signal_restored_unconfirmed = False
                    heatpump_positive_signal_restart_budget_rearm_pending = (
                        False
                    )
                    if heatpump_positive_signal_demand_class in {
                        "pre_dump",
                        "market_price",
                        "price",
                        "pv_surplus",
                    }:
                        boost_active = True
                    if heatpump_positive_signal_demand_class in {
                        "market_price",
                        "price",
                    }:
                        price_boost_active = True
                    restart_revalidation[
                        "positive_signal_restore_status"
                    ] = "fresh_positive_readback_confirmed"
                    logger.info(
                        "WP-Restart: positive %s-Freigabe frisch bestätigt; "
                        "bestehende Signalzeit wird weiter beobachtet.",
                        heatpump_positive_signal_restart_readback.get(
                            "provider",
                            "Aktor",
                        ),
                    )
                elif heatpump_positive_signal_restart_readback.get(
                    "nonpositive_confirmed"
                ) is True:
                    # Ein frischer Normal-/Pause-Readback widerlegt das alte
                    # positive Signal. Ein neuer Start muss wieder Nachfrage
                    # publizieren und auf das Folgezyklus-Budget warten.
                    heatpump_positive_signal_started_ts = 0.0
                    heatpump_positive_signal_demand_class = "none"
                    heatpump_positive_signal_restored_unconfirmed = False
                    heatpump_positive_signal_hold_guard = {}
                    heatpump_positive_signal_start_reservation_allowed = False
                    heatpump_positive_signal_restart_budget_rearm_pending = (
                        False
                    )
                    heatpump_budget_demand_active_class = "none"
                    heatpump_budget_demand_first_seen_ts = 0.0
                    boost_active = False
                    price_boost_active = False
                    restart_revalidation[
                        "positive_signal_restore_status"
                    ] = "fresh_nonpositive_readback_discarded"
                    logger.info(
                        "WP-Restart: %s-Aktor ist frisch nichtpositiv; "
                        "altes Signal-Bookkeeping verworfen.",
                        heatpump_positive_signal_restart_readback.get(
                            "provider",
                            "Aktor",
                        ),
                    )
                else:
                    restart_revalidation[
                        "positive_signal_restore_status"
                    ] = "EVIDENCE_LIMIT"
                    if heatpump_positive_signal_restart_budget_rearm_pending:
                        # Das alte Demand-Wasserzeichen darf kein neues
                        # Positivkommando freigeben. Die nächste reale Nachfrage
                        # erhält daher eine neue First-seen-Kante.
                        heatpump_budget_demand_active_class = "none"
                        heatpump_budget_demand_first_seen_ts = 0.0
                        heatpump_positive_signal_restart_budget_rearm_pending = (
                            False
                        )
                        logger.warning(
                            "WP-Restart: positive Aktorrückmeldung fehlt; "
                            "keine Signalhalte-/Refresh-Kante, neuer Start nur "
                            "nach frischem Folgezyklus-Budget."
                        )
                heatpump_positive_signal_window = (
                    build_heatpump_positive_signal_window(
                        heatpump_positive_signal_started_ts,
                        compressor_running=wp_compressor_running_now,
                        signal_readback_confirmed=(
                            not heatpump_positive_signal_restored_unconfirmed
                        ),
                        signal_hold_guard=heatpump_positive_signal_hold_guard,
                        clock_sample=control_time.sample(),
                        start_reservation_allowed=(
                            heatpump_positive_signal_start_reservation_allowed
                        ),
                        start_reservation_max_s=heatpump_start_reservation_max_s,
                        now_ts=time.time(),
                    )
                )
                heatpump_positive_signal_hold_guard = copy.deepcopy(
                    heatpump_positive_signal_window.get("hold_guard") or {}
                )
            if restart_revalidation.get("safe_stop_required"):
                # Stale oder unbekannte Messwerte dürfen nach der Anlaufwartezeit
                # keinen neuen Aktorbefehl freigeben. Nutzer-Aus bleibt ebenfalls Aus.
                wp_write_allowed = False
            if not restart_revalidation.get("safe_stop_required") and not restart_was_valid:
                logger.info(
                    "%s: Frischer %s-Zustand bestätigt; Policy entscheidet neu.",
                    restart_revalidation.get("diagnostic_signature", "HEATPUMP_RESTART_REVALIDATION"),
                    restart_revalidation.get("validation_source", "Wärmepumpen-Live"),
                )

            wp_connected = False
            if wp:
                wp_connected = wp.connect()
                if wp_connected:

                    if hasattr(wp, 'read_runtime_status'):
                        runtime_status = wp.read_runtime_status()
                        if isinstance(runtime_status, dict) and runtime_status:
                            for key, value in runtime_status.items():
                                if value is not None:
                                    wp_data[key] = value
                            wp_status['physical_valid'] = bool(runtime_status.get('Runtime_Status_Valid'))
                            wp_status['Status_Heizen'] = runtime_status.get('Status_Heizen')
                            wp_status['Status_Warmwasser'] = runtime_status.get('Status_Warmwasser')
                            wp_status['Betriebsart'] = runtime_status.get('Betriebsart')
                            wp_status['Status_Waermepumpe_Bitmask'] = runtime_status.get('Status_Waermepumpe_Bitmask')

                    if hasattr(wp, 'observe_shi_status') and wp_status.get('source_fresh'):
                        wp.observe_shi_status(
                            wp_status.get('SHI_HZ_Mode'),
                            wp_status.get('HZ_Setpoint'),
                            wp_status.get('SHI_WW_Mode'),
                            wp_status.get('WW_Setpoint'),
                        )

                    wp_compressor_observation_valid = bool(
                        wp_data.get('Status_Waermepumpe_Bitmask') is not None
                        or (
                            wp_live_fresh
                            and any(
                                key in wp_data
                                for key in ('Verdichter_Ein', 'Verdichter', 'Verdichter 1', 'Leistung_Verdichter_W')
                            )
                        )
                    )
                    if wp_compressor_observation_valid:
                        wp_compressor_running_now = heatpump_compressor_running(wp_data, wp_status)
                        compressor_transition_ts = time.time()
                        if wp_compressor_running_now and wp_compressor_was_running is not True:
                            wp_compressor_last_start_ts = compressor_transition_ts
                        elif not wp_compressor_running_now and wp_compressor_was_running is True:
                            wp_compressor_last_stop_ts = compressor_transition_ts
                            wp_compressor_last_run_s = max(
                                0.0,
                                compressor_transition_ts - wp_compressor_last_start_ts,
                            )
                            wp_compressor_history_valid = wp_compressor_last_start_ts > 0.0
                        wp_compressor_was_running = wp_compressor_running_now
                        if (
                            wp_compressor_running_now
                            and heatpump_positive_signal_started_ts > 0.0
                            and wp_last_pv_boost_start_ts <= 0.0
                        ):
                            # Erst der physisch bestätigte Verdichterstart, nie
                            # das SG-/Boost-Kommando oder rund 150 W Pumpenvorlauf,
                            # eröffnet Mindestlaufzeit und geschützte Accountinglast.
                            wp_last_pv_boost_start_ts = max(
                                heatpump_positive_signal_started_ts,
                                wp_compressor_last_start_ts
                                or compressor_transition_ts,
                            )
                            if heatpump_positive_signal_demand_class == "pre_dump":
                                predump_heatpump_started_ts = wp_last_pv_boost_start_ts
                                predump_heatpump_hold_until = max(
                                    predump_heatpump_hold_until,
                                    wp_last_pv_boost_start_ts
                                    + (PREDUMP_HEATPUMP_MIN_RUNTIME_MIN * 60.0),
                                )

                    wq_aus = heatpump_live_float(wp_data, ('Sole_Aus', 'WQ_Austritt'), 10.0)
                    at = heatpump_live_float(wp_data, ('Aussentemp',), 20.0)
                    at_mittel = heatpump_live_float(wp_data, ('Aussentemp_Mittel',), at)
                    cooling_boost_allowed = idm_cooling_boost_allowed(wp_type, at_mittel, IDM_COOLING_BOOST_MIN_AT)
                    cooling_boost_mode = 1 if cooling_boost_allowed else 0

                    if first_run: first_run = False

                    # Prüfe vorab auf WW-Timer und WW-Sofort, damit der Sicherheitscheck WW nicht fälschlicherweise abwürgt
                    is_ww_timer_running = False
                    if WW_TIMER_ENABLE:
                        cur_h = now.hour + (now.minute / 60.0)
                        if WW_VON <= WW_BIS:
                            ww_timer_window_active = WW_VON <= cur_h < WW_BIS
                        else:
                            ww_timer_window_active = cur_h >= WW_VON or cur_h < WW_BIS
                        is_ww_timer_running = ww_timer_window_active
                        ww_timer_target_c = WW_NORMAL if ww_timer_window_active else WW_ECO

                    man_flag = "/var/www/html/ramdisk/manual_ww_boost.flag"
                    if os.path.exists(man_flag) and (time.time() - os.path.getmtime(man_flag)) < (WW_SOFORT_DURATION * 60):
                        manual_ww_active = True
                        is_ww_timer_running = True

                    # Sicherheits-Check: Prüfen ob WP in einem fremden Boost-Modus ist
                    # (nicht für IDM, da diese ihre eigene Hysterese-Regelung hat)
                    # Wenn der WW_TIMER_ENABLE aktiv ist, wird WW absichtlich auf Mode=1 gehalten.
                    # Wenn HZ_Mode=1 aber HZ_Setpoint deutlich über 20°C: das ist die WP-eigene
                    # Zeitschaltuhr (Komfort) -- NICHT unser Boost! Soll still akzeptiert werden.
                    illegal_ww = wp_status.get('WW_Mode') == 1 and not is_ww_timer_running and not WW_TIMER_ENABLE
                    hz_set = wp_status.get('HZ_Setpoint', 0)
                    hz_mode_on = wp_status.get('HZ_Mode') == 1
                    # HZ ist nur "illegal" wenn wir selbst keinen Boost gesetzt haben UND
                    # der Setpoint nicht nach WP-internem Komfort-Timer aussieht (> 22°C)
                    hz_is_wp_own_schedule = hz_mode_on and hz_set > 22.0  # WP-eigener Komfort-Timer
                    illegal_hz = hz_mode_on and not hz_is_wp_own_schedule

                    if wp_type not in (1, 5) and wp_status.get('valid') and not boost_active and not os.path.exists(FLAG_FILE) and (illegal_ww or illegal_hz):
                        if hz_is_wp_own_schedule:
                            # WP laeuft nach eigener Zeitschaltuhr (Komfort) -- nur debug, nicht eingreifen
                            logger.debug(f"WP HZ Komfort-Timer aktiv (Setpoint={hz_set}C) -- WP-eigene Zeitschaltuhr, kein Eingriff.")
                        elif wp_status.get('HZ_Mode') == 1 and abs(hz_set - PAUSE_SETPOINT_C) < 1.0:
                            if PV_PAUSE_ENABLE == 1:
                                logger.info(
                                    "Erkenne aktive weiche Sollwertsperre (%.1f°C). Übernehme.",
                                    PAUSE_SETPOINT_C,
                                )
                                boost_active = True
                                pv_pause_active = True
                                if not pv_pause_start_time: pv_pause_start_time = time.time()
                        else:
                            # Gnadenfrist von 60s nach Befehlen, da WP Zeit braucht den Status zu aktualisieren
                            if (time.time() - last_wp_command_time) > 60:
                                # Nicht resetten wenn ein aktueller Boost-/Pause-Pfad aktiv ist.
                                boost_possible = bool(
                                    price_boost_active
                                    or pre_pause_active
                                    or pv_pause_active
                                    or heatpump_positive_signal_window.get(
                                        "active"
                                    )
                                    is True
                                )

                                if not boost_possible:
                                    # Log-Throttling: Debug-Ausgabe max. 1x alle 30 Min (nicht bei jedem Zyklus)
                                    # WICHTIG: Der Reset SELBST (unten) ist NICHT von dieser Bedingung abhaengig!
                                    # Er laeuft bei jedem Zyklus, sobald wp_write_allowed=True gilt.
                                    # D.h. nach einem Crash wird der Boost spaetestens nach 65s zurueckgesetzt.
                                    if (time.time() - last_safety_check_time) > 1800:
                                        logger.info(f"Sicherheits-Reset: WP Boost (WW={wp_status.get('WW_Mode')}, HZ={wp_status.get('HZ_Mode')}, HZ-Set={hz_set}C) ohne SW-Anforderung - setze zurueck.")
                                        last_safety_check_time = time.time()

                                    if wp_write_allowed:
                                        heatpump_positive_output_blocked_this_cycle = True
                                        heatpump_positive_output_block_reasons.append(
                                            "unowned_positive_output_reset"
                                        )
                                        reset_ok = True
                                        if illegal_hz:
                                            if not wp.write_hz_boost(0, None):
                                                logger.warning(f"HZ-Reset fehlgeschlagen! WP laeuft im Boost (HZ_Mode=1, Set={hz_set}C) ohne SW-Anforderung.")
                                                reset_ok = False
                                        if illegal_ww:
                                            if not wp.write_ww_boost(0, CONF_WWW):
                                                logger.warning(f"WW-Reset fehlgeschlagen! WP laeuft im Boost (WW_Mode=1) ohne SW-Anforderung.")
                                                reset_ok = False
                                        if reset_ok:
                                            logger.debug("WP Boost Reset erfolgreich.")
                                        last_wp_command_time = time.time()

                    if wp_type == 5 and wp_status.get('valid') and wp_write_allowed and wp and not boost_active:
                        dimplex_sg_live = int(_safe_float(wp_data.get('dimplex_sg_value'), 0))
                        manual_ww_flag = "/var/www/html/ramdisk/manual_ww_boost.flag"
                        manual_ww_fresh = False
                        try:
                            manual_ww_fresh = (
                                os.path.exists(manual_ww_flag)
                                and (time.time() - os.path.getmtime(manual_ww_flag)) < (WW_SOFORT_DURATION * 60)
                            )
                        except Exception:
                            manual_ww_fresh = False
                        dimplex_owner_active = bool(
                            price_boost_active
                            or pre_pause_active
                            or pv_pause_active
                            or os.path.exists(FLAG_FILE)
                            or manual_ww_fresh
                            or heatpump_positive_signal_window.get("active")
                            is True
                        )
                        if (
                            dimplex_sg_live in (11, 13)
                            and not dimplex_owner_active
                            and (time.time() - last_wp_command_time) > 60
                        ):
                            heatpump_positive_output_blocked_this_cycle = True
                            heatpump_positive_output_block_reasons.append(
                                "dimplex_unowned_positive_output_reset"
                            )
                            logger.info(
                                "Dimplex SG-Freigabe ohne aktiven Besitzer erkannt "
                                "(SG=%s) - setze auf Gelb zurück.",
                                dimplex_sg_live,
                            )
                            if wp.set_boost(0, None, 0, None):
                                last_wp_command_time = time.time()
                                wp_last_pv_boost_stop_ts = time.time()
                                heatpump_positive_signal_started_ts = 0.0
                                heatpump_positive_signal_demand_class = "none"
                                heatpump_positive_signal_restored_unconfirmed = False
                                heatpump_positive_signal_start_reservation_allowed = False
                                heatpump_positive_signal_hold_guard = {}

                        success = True

                    # Externer Reset Check (Gilt nicht für IDM/Dimplex, da dort nur Freigaben vorgegeben werden)
                    if wp_type not in (1, 5) and wp_status.get('valid') and boost_active and wp_status and (time.time() - last_wp_command_time) > 120:
                        if wp_status.get('WW_Mode') != 1 and wp_status.get('HZ_Mode') != 1:
                            if not is_ww_timer_running:
                                heatpump_positive_output_blocked_this_cycle = True
                                heatpump_positive_output_block_reasons.append(
                                    "external_positive_output_reset"
                                )
                                logger.info("Boost-Modus extern deaktiviert. Reset.")
                                boost_active = False
                                deficit_start_time = None
                                price_boost_active = False
                                pre_pause_active = False
                                pv_pause_active = False
                                heatpump_positive_signal_started_ts = 0.0
                                heatpump_positive_signal_demand_class = "none"
                                heatpump_positive_signal_restored_unconfirmed = False
                                heatpump_positive_signal_start_reservation_allowed = False
                                heatpump_positive_signal_hold_guard = {}

                    success = True
                else:
                    wp_error_msg = "Verbindung zur Wärmepumpe im Modbus-Netzwerk fehlgeschlagen."
                    logger.warning("Verbindung zur WP fehlgeschlagen")

            # 2. E3DC Daten holen
            e3dc = {}
            grid = 0
            bat = 0
            soc = 0
            wb_locked = False
            current_price = 99.9
            prices = []
            forecast = []
            price_start_hour = 0
            price_interval = 1.0
            e3dc_valid = False
            is_tiered_nt = False
            v2h_allowed = False
            v2h_reason = "V2H deaktiviert"

            # --- KI 3.0: Gehirn lesen (Storage Manager) ---
            # Primär: wb_pv_budget.json (wird alle 5s frisch geschrieben, inkl. tl_brake)
            # Fallback: storage_manager_state.json energy_score
            free_for_limbs_w = 0
            heat_policy_budget_w = 0
            must_consume_w = 0
            consumer_allocations = {}
            wallbox_phase_transition_active = False
            wallbox_phase_transition_reserved_w = 0
            wallbox_phase_transition_until_ts = 0.0
            heatpump_running_commitment_w = 0
            heatpump_pause_request = {}
            source_recovery_request_seen = False
            source_recovery_pause_requested = False
            source_recovery_pause_allowed = False
            source_recovery_pause_latched = False
            source_recovery_pause_blocks_boost = False
            source_recovery_heat_budget_override = False
            source_recovery_budget_ready = False
            source_recovery_release_reason = ""
            source_recovery_history_allowed = False
            source_recovery_history_reason = ""
            source_recovery_compressor_off_before_s = None
            source_recovery_planned_pause_s = 0.0
            heatpump_budget_w = None
            heatpump_accounting_budget_w = None
            central_heatpump_boost_permission_active = False
            storage_state_name = 'unknown'
            budget_is_fresh = False
            storage_budget_source_contract = {
                "schema_version": "storage_budget_fallback_contract_v1",
                "accepted": False,
                "source": "none",
                "evidence_status": "INVALID",
                "reason_code": "NO_FRESH_BUDGET_SOURCE",
            }
            storage_primary_budget_diagnostic = {
                "schema_version": "storage_primary_budget_cycle_diagnostic_v1",
                "status": "not_evaluated",
                "reason_code": "PRIMARY_BUDGET_NOT_EVALUATED",
                "exception_type": "",
            }
            storage_loaded_observed_revision = None
            storage_loaded_expected_revision = None
            try:
                wb_budget_path = "/var/www/html/ramdisk/wb_pv_budget.json"
                sm_path = "/var/www/html/ramdisk/storage_manager_state.json"
                wb_budget_data = {}
                sm_data = {}

                # wb_pv_budget.json: primäre Budget-Quelle (alle 5s aktualisiert)
                if os.path.exists(wb_budget_path):
                    _age = time.time() - os.path.getmtime(wb_budget_path)
                    try:
                        with open(
                            wb_budget_path,
                            'r',
                            encoding='utf-8',
                            errors='strict',
                        ) as f:
                            observed_wb_budget = json.load(f)
                        if isinstance(observed_wb_budget, dict):
                            wb_budget_data = observed_wb_budget
                            storage_loaded_observed_revision = (
                                wb_budget_data.get("budget_revision")
                            )
                            storage_loaded_expected_revision = (
                                storage_contract_revision_hash({
                                    key: value
                                    for key, value in wb_budget_data.items()
                                    if key != "budget_revision"
                                })
                            )
                    except Exception as exc:
                        wb_budget_data = {}
                        storage_primary_budget_diagnostic = {
                            "schema_version": "storage_primary_budget_cycle_diagnostic_v1",
                            "status": "read_error",
                            "reason_code": "PRIMARY_BUDGET_JSON_READ_FAILED",
                            "exception_type": type(exc).__name__,
                        }
                    storage_budget_source_contract, primary_projection = select_storage_primary_budget(
                        wb_budget_data,
                        now_ts=time.time(),
                        file_age_s=_age,
                    )
                    storage_primary_budget_diagnostic = {
                        "schema_version": "storage_primary_budget_cycle_diagnostic_v1",
                        "status": (
                            "valid"
                            if primary_projection is not None
                            else "rejected"
                        ),
                        "reason_code": str(
                            storage_budget_source_contract.get("reason_code")
                            or "PRIMARY_BUDGET_REASON_MISSING"
                        ),
                        "exception_type": "",
                        "evidence_status": storage_budget_source_contract.get(
                            "evidence_status"
                        ),
                        "revision_status": storage_budget_source_contract.get(
                            "revision_status"
                        ),
                        "timestamp_s": storage_budget_source_contract.get(
                            "timestamp_s"
                        ),
                    }
                    primary_provenance_diagnostic = (
                        storage_budget_source_contract.get(
                            "producer_provenance"
                        )
                        if isinstance(
                            storage_budget_source_contract.get(
                                "producer_provenance"
                            ),
                            dict,
                        )
                        else {}
                    )
                    for diagnostic_key in (
                        "observed_budget_revision",
                        "expected_budget_revision",
                        "budget_timestamp_s",
                    ):
                        if diagnostic_key in primary_provenance_diagnostic:
                            storage_primary_budget_diagnostic[
                                diagnostic_key
                            ] = primary_provenance_diagnostic[diagnostic_key]
                    storage_primary_budget_diagnostic[
                        "loaded_observed_budget_revision"
                    ] = storage_loaded_observed_revision
                    storage_primary_budget_diagnostic[
                        "loaded_expected_budget_revision"
                    ] = storage_loaded_expected_revision
                    if primary_projection is not None:
                        free_for_limbs_w = primary_projection["free_for_limbs_w"]
                        must_consume_w = primary_projection["must_consume_w"]
                        consumer_allocations = primary_projection["consumer_allocations"]
                        heatpump_budget_w = primary_projection["heatpump_budget_w"]
                        heat_policy_budget_w = max(
                            0,
                            _safe_int(heatpump_budget_w, 0),
                        )
                        heatpump_accounting_budget_w = primary_projection[
                            "heatpump_accounting_budget_w"
                        ]
                        central_heatpump_boost_permission_active = bool(
                            primary_projection.get(
                                "heatpump_boost_permission_active"
                            )
                        )
                        wallbox_phase_transition_active = primary_projection[
                            "wallbox_phase_transition_active"
                        ]
                        wallbox_phase_transition_reserved_w = primary_projection[
                            "wallbox_phase_transition_reserved_w"
                        ]
                        wallbox_phase_transition_until_ts = primary_projection[
                            "wallbox_phase_transition_until_ts"
                        ]
                        heatpump_running_commitment_w = primary_projection[
                            "heatpump_running_commitment_w"
                        ]
                        heatpump_pause_request = primary_projection["heatpump_pause_request"]
                        storage_state_name = primary_projection["storage_state_name"]
                        budget_is_fresh = True

                # Fallback: storage_manager_state.json (wird seltener geschrieben)
                # Ein frisches 0W-Budget ist ein echtes Stop-Signal und darf
                # nicht durch ältere storage_manager_state-Werte überschrieben werden.
                if (not budget_is_fresh) and os.path.exists(sm_path):
                    with open(
                        sm_path,
                        'r',
                        encoding='utf-8',
                        errors='strict',
                    ) as f:
                        sm_data = json.load(f)
                    storage_budget_source_contract, fallback_projection = select_storage_budget_fallback(
                        sm_data,
                        primary_payload=wb_budget_data,
                        previous_guard=storage_budget_fallback_replay_guard,
                        now_ts=time.time(),
                    )
                    if fallback_projection is not None:
                        free_for_limbs_w = fallback_projection["free_for_limbs_w"]
                        must_consume_w = fallback_projection["must_consume_w"]
                        consumer_allocations = fallback_projection["consumer_allocations"]
                        heatpump_budget_w = fallback_projection["heatpump_budget_w"]
                        heat_policy_budget_w = max(
                            0,
                            _safe_int(heatpump_budget_w, 0),
                        )
                        heatpump_accounting_budget_w = fallback_projection.get(
                            "heatpump_accounting_budget_w"
                        )
                        wallbox_phase_transition_active = fallback_projection[
                            "wallbox_phase_transition_active"
                        ]
                        wallbox_phase_transition_reserved_w = fallback_projection[
                            "wallbox_phase_transition_reserved_w"
                        ]
                        wallbox_phase_transition_until_ts = fallback_projection[
                            "wallbox_phase_transition_until_ts"
                        ]
                        heatpump_running_commitment_w = fallback_projection[
                            "heatpump_running_commitment_w"
                        ]
                        heatpump_pause_request = fallback_projection["heatpump_pause_request"]
                        storage_state_name = fallback_projection["storage_state_name"]
                        storage_budget_fallback_replay_guard = dict(
                            storage_budget_source_contract.get("replay_guard") or {}
                        )

                if isinstance(heatpump_pause_request, dict):
                    expires_ts = _safe_float(heatpump_pause_request.get("expires_ts", 0), 0.0)
                    source_recovery_request_seen = bool(
                        heatpump_pause_request.get("owner") == "source_recovery_heatpump"
                    )
                    source_recovery_pause_requested = bool(
                        heatpump_pause_request.get("active")
                        and heatpump_pause_request.get("owner") == "source_recovery_heatpump"
                        and expires_ts >= (time.time() - 5)
                    )

            except Exception as exc:
                storage_primary_budget_diagnostic = {
                    "schema_version": "storage_primary_budget_cycle_diagnostic_v1",
                    "status": "evaluation_error",
                    "reason_code": "PRIMARY_BUDGET_EVALUATION_FAILED",
                    "exception_type": type(exc).__name__,
                }

            legacy_heat_automation_owner = (
                build_legacy_heat_automation_owner_contract(
                    runtime_enabled=heat_policy_runtime_enabled,
                    auto_mode=AUTO_MODE == 1,
                    actuator_write_allowed=bool(wp_write_allowed),
                    storage_budget_contract=storage_budget_source_contract,
                    now_ts=time.time(),
                )
            )
            ww_timer_automation_owner = (
                build_legacy_heat_automation_owner_contract(
                    runtime_enabled=False,
                    auto_mode=AUTO_MODE == 1,
                    actuator_write_allowed=bool(wp_write_allowed),
                    storage_budget_contract=storage_budget_source_contract,
                    now_ts=time.time(),
                )
            )
            heat_runtime_actuation = heat_runtime_actuation_contract(
                heat_policy_runtime_enabled,
                validated_intent=heat_intent_export,
                runtime_validation=heat_intent_runtime_validation,
                policy_owner=None,
                legacy_owner_contract=legacy_heat_automation_owner,
                now_ts=time.time(),
            )
            automatic_heat_actuation_allowed = (
                automatic_heat_policy_actuation_allowed(
                    heat_policy_runtime_enabled,
                    heat_runtime_actuation,
                )
            )

            try:
                e3dc = read_e3dc_live_for_energy_manager(timeout=10)
                grid = e3dc.get('grid')
                if grid is None: grid = 0
                bat = e3dc.get('bat')
                if bat is None: bat = 0
                soc = e3dc.get('soc')
                if soc is None: soc = 0
                wb_locked_live = e3dc.get('wb_locked', False)
                current_price = e3dc.get('price_ct')

                if current_price is None or current_price == 99.9:
                    v4_opt_score, v4_billing_price = get_v4_eco_score()
                    if v4_billing_price is not None:
                        current_price = v4_billing_price
                    else:
                        current_price = 99.9  # Fallback

                prices = e3dc.get('prices', [])
                forecast = e3dc.get('forecast', [])
                e3dc_valid = True

                # Notstrom / Inselbetrieb Überwachung
                notstrom_status_live = e3dc.get('notstrom_status', 0)
                if notstrom_status_live != last_notstrom_status:
                    if notstrom_status_live in [1, 4]:
                        n_mode = "Inselbetrieb" if notstrom_status_live == 4 else "Notstrom"
                        actions = json.dumps([
                            {"action": "emergency_stop", "title": "Großverbraucher ABSCHALTEN"}
                        ])
                        send_webpush('push_notify_notstrom', f'⚠️ ACHTUNG: {n_mode}!', f'Das System läuft aktuell im {n_mode} Modus! Netzstrom-Ausfall erkannt.', actions=actions)
                    elif last_notstrom_status in [1, 4] and notstrom_status_live == 0:
                        send_webpush('push_notify_notstrom', '✅ Netz ist zurück', 'Der E3DC Notstrom / Inselbetrieb wurde beendet. Das Stromnetz ist verfügbar.')
                    last_notstrom_status = notstrom_status_live

            except Exception as e:
                if AUTO_MODE == 1 or os.path.exists(FLAG_FILE) or has_shelly_heatpump:
                    now_err = time.time()
                    if now_err - last_e3dc_error_log_time >= 60:
                        logger.error(f"Fehler bei E3DC Abfrage: {e}")
                        last_e3dc_error_log_time = now_err

            # --- Fahrzeug & SoC Management (Dual Wallbox Support) ---
            wb1_session_kwh = e3dc.get('wb_session_kwh')
            wb2_session_kwh = e3dc.get('wb2_session_kwh')
            wb1_locked = bool(e3dc.get('wb_locked', False))
            wb2_locked = bool(e3dc.get('wb2_locked', False))

            # RSCP-Glitch-Schutz: Bei nativer E3DC Wallbox (wb_native_type=e3dc) lese session_kwh und
            # wb_locked DIREKT aus wb_live_session.json (vom wallbox_manager per RSCP befuellt).
            # So wird den C++ Polling-Glitches (session_kwh springt auf 0) aus dem Weg gegangen.
            wb_native_type = get_cfg_value(current_config, 'wb_native_type', '').strip().lower()
            wb_native_enable = str(get_cfg_value(current_config, 'wb_native_enable', '0')).strip().lower()
            if wb_native_enable in ('1', 'true') and wb_native_type in ('e3dc', 'native'):
                for wb_live_session_path in (
                    '/var/www/html/ramdisk/wb_live_session.json',
                    '/var/www/html/logs/wb_live_session.json',
                ):
                    try:
                        if (
                            not os.path.exists(wb_live_session_path)
                            or (time.time() - os.path.getmtime(wb_live_session_path)) >= 30
                        ):
                            continue
                        with open(wb_live_session_path, 'r') as f:
                            _wls = json.load(f)
                        if _wls.get('source') != 'rscp':
                            continue
                        _rscp_kwh = _wls.get('session_kwh')
                        if _rscp_kwh is not None and _rscp_kwh >= 0:
                            wb1_session_kwh = _rscp_kwh
                        # car_connected aus RSCP ist zuverlaessiger als wb_locked aus C++ Polling
                        wb1_locked = bool(_wls.get('car_connected', wb1_locked))
                        break
                    except Exception:
                        continue  # Nächster Read-Fallback, danach C++-Wert

            wb1_car_id = get_cfg_value(current_config, 'wb1_car_id', 'car1')
            wb2_car_id = get_cfg_value(current_config, 'wb2_car_id', '')

            all_vehicles = []
            try:
                if os.path.exists(VEHICLES_JSON_FILE):
                    with open(VEHICLES_JSON_FILE, "r") as f:
                        v_data = json.load(f)
                        all_vehicles = v_data.get('vehicles', [])
            except: pass

            _cloud_interval_bound = 'bluelink_interval' in current_config
            _cloud_interval_raw = current_config.get('bluelink_interval', 15)
            if (not _cloud_interval_bound
                and isinstance(current_config.get('config'), dict)
                and 'bluelink_interval' in current_config['config']):
                _cloud_interval_raw = current_config['config']['bluelink_interval']
                _cloud_interval_bound = True
            if not _cloud_interval_bound and os.path.isfile(LEGACY_CONFIG_PATH):
                try:
                    with open(
                        LEGACY_CONFIG_PATH,
                        'r',
                        encoding='utf-8',
                        errors='ignore',
                    ) as _legacy_config:
                        for _legacy_line in _legacy_config:
                            if '=' not in _legacy_line or _legacy_line.lstrip().startswith('#'):
                                continue
                            _legacy_key, _legacy_value = _legacy_line.split('=', 1)
                            if _legacy_key.strip().lower() == 'bluelink_interval':
                                _cloud_interval_raw = _legacy_value.strip()
                                break
                except OSError:
                    pass
            _soc_age_config = dict(current_config)
            _soc_age_config['bluelink_interval'] = _cloud_interval_raw
            cloud_soc_freshness_s = vehicle_soc_max_age_s(
                'bluelink',
                _soc_age_config,
            )

            def soc_timestamp(value):
                if value in (None, ''):
                    return 0.0
                if isinstance(value, bool):
                    return 0.0
                try:
                    parsed = float(value)
                    if parsed > 100000000000.0:
                        parsed /= 1000.0
                    return parsed if math.isfinite(parsed) and parsed > 0.0 else 0.0
                except (TypeError, ValueError):
                    try:
                        return datetime.fromisoformat(
                            str(value).replace('Z', '+00:00')
                        ).timestamp()
                    except (TypeError, ValueError):
                        return 0.0

            def soc_source_uses_openwb_pro_anchor(source):
                contract = vehicle_soc_source_contract(source)
                return bool(
                    contract
                    and contract['base_source'] in (
                        'openwb_pro_raw', 'openwb_pro_estimated',
                    )
                )

            def soc_source_is_cloud(source):
                contract = vehicle_soc_source_contract(source)
                return bool(contract and contract['kind'] == 'cloud')

            def soc_source_is_untrusted(source):
                return vehicle_soc_source_contract(source) is None

            def soc_source_is_mqtt(source):
                contract = vehicle_soc_source_contract(source)
                return bool(contract and contract['base_source'] in ('mqtt', 'openwb_mqtt'))

            def soc_contract_flag_active(value):
                if value is True:
                    return True
                if isinstance(value, bool) or value is None:
                    return False
                if isinstance(value, (int, float)):
                    numeric = float(value)
                    return math.isfinite(numeric) and numeric == 1.0
                return str(value).strip().lower() in (
                    '1', 'true', 'yes', 'ja', 'on', 'active',
                    'stale', 'expired', 'invalid', 'degraded',
                )

            def soc_percent_value(value):
                if isinstance(value, bool) or value in (None, ''):
                    return None
                try:
                    normalized = float(value)
                except (TypeError, ValueError):
                    return None
                if (
                    not math.isfinite(normalized)
                    or normalized < 0.0
                    or normalized > 100.0
                ):
                    return None
                return normalized

            def soc_record_source_ts(record):
                item = record if isinstance(record, dict) else {}
                source = str(
                    item.get('soc_source', item.get('source', ''))
                ).strip().lower()
                if soc_source_is_untrusted(source):
                    return 0.0
                if 'soc_source_ts' in item:
                    source_ts = soc_timestamp(item.get('soc_source_ts'))
                    if source_ts > 0.0:
                        return source_ts
                    raw_ts = soc_timestamp(item.get('raw_soc_ts'))
                    return raw_ts if raw_ts > 0.0 else 0.0
                raw_ts = soc_timestamp(item.get('raw_soc_ts'))
                if raw_ts > 0.0:
                    return raw_ts
                # Ausschließlich bekannte manuelle Nutzeraktionen dürfen den
                # historischen Aktionszeitpunkt aus ``ts`` übernehmen. Ein
                # Maschinen-Heartbeat ist kein SoC-Quellanker.
                if source in CONFIRMED_MANUAL_SOC_SOURCES:
                    return soc_timestamp(item.get('ts'))
                return 0.0

            def soc_source_rule_confirmed(
                source,
                rule_confirmed=None,
                source_ts=None,
                now_ts=None,
                derived_context=False,
            ):
                contract = vehicle_soc_source_contract(source)
                if contract is None:
                    return False
                exact_true = rule_confirmed is True
                explicit = None if rule_confirmed is None else exact_true
                if explicit is False:
                    return False
                manual_direct = bool(
                    not contract['derived']
                    and contract['base_source'] in CONFIRMED_MANUAL_SOC_SOURCES
                )
                # Jede Maschinenquelle bleibt auch in einer abgeleiteten
                # Session nur mit dem exakt typisierten Producer-Vertrag
                # regelwirksam. ``"true"``, ``1`` oder ein fehlendes Feld
                # dürfen dabei nie nachträglich aufgewertet werden.
                if not manual_direct and not exact_true:
                    return False
                anchor_ts = soc_timestamp(source_ts)
                now_value = time.time() if now_ts is None else float(now_ts)
                max_age_s = vehicle_soc_max_age_s(source, _soc_age_config)
                return bool(
                    anchor_ts > 0.0
                    and anchor_ts <= now_value + 300.0
                    and max_age_s > 0.0
                    and now_value - anchor_ts <= max_age_s
                )

            def soc_record_rule_contract(record, source):
                item = record if isinstance(record, dict) else {}
                source_text = str(source or '').strip().lower()
                return bool(
                    item.get('soc_rule_confirmed') is True
                    or (
                        source_text in CONFIRMED_MANUAL_SOC_SOURCES
                        and 'soc_rule_confirmed' not in item
                    )
                )

            def soc_record_vetoed(record):
                item = record if isinstance(record, dict) else {}
                if any(
                    soc_contract_flag_active(item.get(key))
                    for key in (
                        'soc_stale', 'car_soc_stale', 'stale',
                        'estimate_expired', 'soc_expired', 'car_soc_expired',
                        'expired', 'soc_profile_binding_invalid',
                        'car_soc_profile_binding_invalid',
                        'profile_binding_invalid', 'driver_status_stale',
                        'driver_status_degraded',
                    )
                ):
                    return True

                def field_true(value):
                    if value is True:
                        return True
                    if isinstance(value, bool) or value is None:
                        return False
                    if isinstance(value, (int, float)):
                        numeric = float(value)
                        return math.isfinite(numeric) and numeric == 1.0
                    return str(value).strip().lower() in (
                        '1', 'true', 'yes', 'ja', 'on',
                    )

                return any(
                    key in item and not field_true(item.get(key))
                    for key in (
                        'driver_status_valid', 'soc_profile_bound',
                        'car_soc_profile_bound', 'plug_state', 'plugged',
                        'is_plugged_in',
                    )
                )

            def soc_record_binding_valid(
                record,
                wb_idx=None,
                car_id='',
                require_plugged=False,
                expected_vehicle=None,
                allow_legacy_missing_slot=False,
            ):
                item = record if isinstance(record, dict) else {}
                if not item:
                    return False
                if require_plugged:
                    plugged = (
                        item.get('is_plugged_in')
                        if 'is_plugged_in' in item
                        else item.get('plugged')
                    )
                    if plugged is not True:
                        return False
                if wb_idx is not None:
                    slot_key = (
                        'wb_slot' if 'wb_slot' in item
                        else 'wb' if 'wb' in item
                        else ''
                    )
                    if not slot_key or item.get(slot_key) in (None, ''):
                        if not allow_legacy_missing_slot:
                            return False
                    else:
                        raw_slot = item.get(slot_key)
                        if isinstance(raw_slot, bool):
                            return False
                        try:
                            slot = int(raw_slot)
                            if slot <= 0:
                                if not (allow_legacy_missing_slot and slot == 0):
                                    return False
                            elif slot != int(wb_idx):
                                return False
                        except (TypeError, ValueError):
                            return False

                def compact(value):
                    return ''.join(
                        char for char in str(value or '').strip().lower()
                        if char.isalnum()
                    )

                expected_aliases = {compact(car_id)} if compact(car_id) else set()
                if isinstance(expected_vehicle, dict):
                    expected_aliases.update(
                        compact(expected_vehicle.get(key))
                        for key in (
                            'id', 'profile_id', 'car_id', 'vehicle_id',
                            'cloud_vehicle_id', 'rfid_tag',
                        )
                        if compact(expected_vehicle.get(key))
                    )
                if expected_aliases:
                    record_aliases = {
                        compact(item.get(key))
                        for key in (
                            'id', 'profile_id', 'car_id', 'vehicle_id',
                            'cloud_vehicle_id', 'rfid_tag',
                        )
                        if compact(item.get(key))
                    }
                    if not record_aliases.intersection(expected_aliases):
                        return False
                return True

            def soc_session_anchor(
                record,
                wb_idx=None,
                car_id='',
                allow_legacy_missing_slot=False,
            ):
                item = record if isinstance(record, dict) else {}
                source = item.get('soc_source', '')
                start_soc = soc_percent_value(item.get('start_soc'))
                source_ts = soc_timestamp(item.get('soc_source_ts'))
                if source_ts <= 0.0:
                    source_ts = soc_timestamp(
                        item.get('last_manual_ts')
                        if item.get('is_manual')
                        else item.get('last_car_ts')
                    )
                if (
                    start_soc is None
                    or soc_record_vetoed(item)
                    or not soc_record_binding_valid(
                        item,
                        wb_idx,
                        car_id,
                        require_plugged=False,
                        allow_legacy_missing_slot=allow_legacy_missing_slot,
                    )
                    or not soc_record_rule_contract(item, source)
                    or not soc_source_rule_confirmed(
                        source,
                        item.get('soc_rule_confirmed'),
                        source_ts,
                        derived_context=True,
                    )
                ):
                    return None
                return start_soc, source_ts

            def configured_vehicle_binding_unique(wb_idx, car_id):
                def compact(value):
                    return ''.join(
                        char for char in str(value or '').strip().lower()
                        if char.isalnum()
                    )

                selected = compact(car_id)
                if not selected:
                    return False
                assignments = [compact(wb1_car_id), compact(wb2_car_id)]
                try:
                    current = assignments[int(wb_idx) - 1]
                except (IndexError, TypeError, ValueError):
                    return False
                return current == selected and assignments.count(selected) == 1

            def process_car_session(wb_idx, car_id, session_kwh, is_locked, wb_power_w):
                session_files = car_session_paths(wb_idx)
                session_file = session_files["live"]
                session_checkpoint_file = session_files["checkpoint"]

                manual_soc_file = f"/var/www/html/ramdisk/manual_soc_wb{wb_idx}.json"
                if wb_idx == 1 and not os.path.exists(manual_soc_file): manual_soc_file = "/var/www/html/tmp/manual_soc.json"

                # Temporäre RSCP Kollisionen beim Regeln können kurzzeitig is_locked=False erzeugen!
                # Daher löschen wir die Session-/Fahrzeugdaten NICHT mehr blindlings weg ("Auto bleibt gesetzt").
                # Wenn das Auto wirklich abgesteckt wurde, fällt session_kwh beim nächsten Anstecken auf 0,
                # was unsere Logik weiter unten ohnehin als neue Session erkennt.
                # Der persistente Checkpoint behält dabei konservativ die letzte bestätigte
                # verbundene Session. Nach diesem Rücksprung gibt es weder Disconnect-Kante
                # noch Idle-Heartbeat; so wird ein einzelner RSCP-Glitch nicht zum Crash-Stand.
                if not is_locked:
                    return None

                if not car_id or car_id == '': return None

                target_v = next((v for v in all_vehicles if v.get('id') == car_id), None)
                if not target_v and car_id == 'car1':
                    target_v = _single_vehicle_fallback(all_vehicles)
                soc_raw = target_v.get('soc') if target_v else None
                car_soc = soc_percent_value(soc_raw)
                if not target_v:
                    car_soc = None
                car_ts = soc_timestamp(
                    target_v.get('soc_source_ts')
                    if isinstance(target_v, dict) else None
                )
                car_soc_confirmed = bool(
                    target_v
                    and car_soc is not None
                    and target_v.get('soc_rule_confirmed') is True
                    and 'soc_source_ts' in target_v
                    and not soc_record_vetoed(target_v)
                    and soc_record_binding_valid(
                        target_v,
                        wb_idx,
                        car_id,
                        require_plugged=True,
                        expected_vehicle=target_v,
                        allow_legacy_missing_slot=(
                            configured_vehicle_binding_unique(wb_idx, car_id)
                        ),
                    )
                    and soc_source_rule_confirmed(
                        target_v.get('soc_source', target_v.get('source', '')),
                        target_v.get('soc_rule_confirmed'),
                        car_ts,
                    )
                )
                if not car_soc_confirmed:
                    car_soc = None


                manual_data = None
                if os.path.exists(manual_soc_file):
                    try:
                        with open(manual_soc_file, "r") as f: manual_data = json.load(f)
                    except: pass
                manual_source = str(
                    (manual_data or {}).get('source', '')
                ).strip()
                manual_user_source = (
                    manual_source.lower() in CONFIRMED_MANUAL_SOC_SOURCES
                )
                manual_soc_raw = (manual_data or {}).get('soc')
                manual_soc = soc_percent_value(manual_soc_raw)
                manual_source_ts = soc_record_source_ts(manual_data)
                manual_soc_confirmed = bool(
                    manual_data
                    and manual_soc is not None
                    and soc_record_rule_contract(manual_data, manual_source)
                    and not soc_record_vetoed(manual_data)
                    and soc_record_binding_valid(
                        manual_data,
                        wb_idx,
                        car_id,
                        require_plugged=True,
                        expected_vehicle=target_v,
                        # manual_soc_wbX.json ist über den Dateipfad bereits
                        # eindeutig an diese Wallbox gebunden.
                        allow_legacy_missing_slot=True,
                    )
                    and soc_source_rule_confirmed(
                        manual_source,
                        manual_data.get('soc_rule_confirmed'),
                        manual_source_ts,
                    )
                )

                sess, _session_source = load_car_session_state(wb_idx, session_files)
                if manual_data and not manual_soc_confirmed and sess.get('is_manual'):
                    discard_invalid_car_session_state(
                        session_files,
                        wb_idx=wb_idx,
                        checkpoint_runtime=(
                            car_session_checkpoint_runtime.setdefault(
                                int(wb_idx),
                                {},
                            )
                        ),
                        latest_sessions=latest_car_sessions,
                    )
                    sess = {}
                    if not car_soc_confirmed:
                        return None

                if sess:
                    stored_anchor = soc_session_anchor(
                        sess,
                        wb_idx=wb_idx,
                        car_id=car_id,
                        # Sessiondateien sind per WB getrennt. Ein vorhandenes
                        # Feld muss dennoch exakt passen.
                        allow_legacy_missing_slot=True,
                    )
                    if stored_anchor is not None:
                        sess['start_soc'], sess['soc_source_ts'] = stored_anchor
                        sess['soc_rule_confirmed'] = True
                    else:
                        sess = {}

                try:
                    session_kwh = round(float(session_kwh), 2)
                except (ValueError, TypeError):
                    session_kwh = None

                # RSCP Glitch Protection: Wenn der session_kwh Wert plötzlich auf 0 fällt (während das Auto noch angesteckt ist),
                # ist das ein Polling-Glitch des C++ Kerns aufgrund von RSCP-Kollisionen.
                # Wir stellen den alten Wert aus der Session wieder her.
                if session_kwh in [0, 0.0, None] and sess.get('start_kwh', 0) > 0:
                    session_kwh = sess.get('last_valid_session_kwh', sess.get('start_kwh', 0))
                else:
                    sess['last_valid_session_kwh'] = session_kwh

                manual_ts = manual_source_ts if manual_soc_confirmed else 0
                if manual_ts > sess.get('last_manual_ts', 0):
                    sess = {
                        'wb': wb_idx,
                        'car_id': car_id,
                        'start_soc': manual_soc,
                        'start_kwh': session_kwh or 0,
                        'last_car_ts': car_ts,
                        'last_manual_ts': manual_ts,
                        'car_name': manual_data.get('name', ''),
                        'car_capacity': manual_data.get('capacity', 0.0),
                        'is_manual': True,
                        'soc_source': manual_source,
                        'soc_source_ts': manual_source_ts,
                        'soc_rule_confirmed': True,
                        'last_valid_session_kwh': session_kwh or 0
                    }
                elif not sess or sess.get('start_kwh', -1) > (session_kwh or 0) or (car_ts > sess.get('last_car_ts', 0) and not sess.get('is_manual')):
                    if car_soc is None:
                        return None
                    # Neuer Cloud-Sync erkannt: SoC und kwh-Zähler synchronisieren, um Sprünge zu vermeiden
                    # Wir behalten die Session bei, setzen aber den Nullpunkt neu auf den Cloud-Wert
                    sess['start_soc'] = car_soc or 0
                    sess['start_kwh'] = session_kwh or 0
                    sess['last_car_ts'] = car_ts
                    sess['is_manual'] = False
                    sess['soc_source'] = (target_v or {}).get(
                        'soc_source',
                        (target_v or {}).get('source', ''),
                    )
                    sess['soc_source_ts'] = car_ts
                    sess['soc_rule_confirmed'] = True
                    sess['last_valid_session_kwh'] = session_kwh or 0

                added_kwh = max(0, (session_kwh or 0) - sess.get('start_kwh', 0))
                net_added_kwh = added_kwh * 0.92

                if net_added_kwh > 0.02:
                    raw_session_source = str(sess.get('soc_source', '')).strip().lower()
                    if not raw_session_source.startswith('wallbox_estimated_from_'):
                        raw_contract = vehicle_soc_source_contract(raw_session_source)
                        if raw_contract is not None:
                            sess['soc_source'] = (
                                'wallbox_estimated_from_' + raw_contract['base_source']
                            )

                conf_cap = get_cfg_value(current_config, f'wb{wb_idx}_capacity', CAR_CAPACITY)
                if sess.get('car_capacity', 0) > 0: cap = sess['car_capacity']
                elif target_v and target_v.get('capacity'): cap = float(target_v['capacity'])
                else: cap = float(conf_cap)
                if cap <= 0: cap = 72.0

                virtual_soc = sess.get('start_soc', 0) + ((net_added_kwh / cap) * 100.0)
                current_soc = min(100.0, virtual_soc)
                if car_soc is not None and current_soc < car_soc and not sess.get('is_manual'): current_soc = car_soc

                sess['current_virtual_soc'] = round(current_soc, 2)
                sess['wb'] = wb_idx
                sess['car_id'] = car_id
                sess['car_capacity'] = cap
                session_source_ts = soc_timestamp(sess.get('soc_source_ts'))
                if session_source_ts <= 0.0:
                    session_source_ts = soc_timestamp(
                        sess.get('last_manual_ts')
                        if sess.get('is_manual')
                        else sess.get('last_car_ts')
                    )
                sess['soc_source_ts'] = session_source_ts or None
                sess['soc_rule_confirmed'] = soc_source_rule_confirmed(
                    sess.get('soc_source', ''),
                    sess.get('soc_rule_confirmed'),
                    session_source_ts,
                    derived_context=True,
                )
                session_age_contract = vehicle_soc_age_contract(
                    sess.get('soc_source', ''),
                    _soc_age_config,
                )
                if session_age_contract is None:
                    return None
                sess['soc_age_contract'] = session_age_contract['schema_version']
                sess['soc_age_contract_source'] = session_age_contract['source']
                sess['soc_max_age_s'] = session_age_contract['max_age_s']
                sess['ts'] = time.time()

                target_soc = float(get_cfg_value(current_config, f'wb{wb_idx}_target_soc', CAR_TARGET_SOC))
                if target_v and target_v.get('target_soc'):
                    car_limit = float(target_v['target_soc'])
                    if car_limit > 0: target_soc = min(target_soc, car_limit)
                sess['target_soc'] = target_soc
                sess['charging'] = bool(wb_power_w > 500)

                if wb_power_w > 500:
                    needed_kwh = max(0, (target_soc - current_soc) * cap / 100.0)
                    kw = wb_power_w / 1000.0
                    sess['time_to_target_mins'] = int((needed_kwh / kw) * 60) if kw > 0.5 else None
                else:
                    sess['time_to_target_mins'] = None

                write_json_atomic_tolerant(session_file, sess)
                persist_car_session_checkpoint(
                    session_checkpoint_file,
                    sess,
                    car_session_checkpoint_runtime.setdefault(int(wb_idx), {}),
                    charging=sess['charging'],
                )
                latest_car_sessions[int(wb_idx)] = dict(sess)
                return sess

            processed_wb1 = process_car_session(1, wb1_car_id, wb1_session_kwh, wb1_locked, abs(e3dc.get('wb', 0)))
            processed_wb2 = process_car_session(2, wb2_car_id, wb2_session_kwh, wb2_locked, abs(e3dc.get('wb2', 0)))

            primary_sess = processed_wb1 or processed_wb2
            primary_sess_usable = bool(
                primary_sess
                and primary_sess.get('soc_rule_confirmed') is True
            )
            fallback_vehicle = _single_vehicle_fallback(all_vehicles)
            fallback_wb_idx = (
                1 if wb1_locked and not wb2_locked
                else 2 if wb2_locked and not wb1_locked
                else None
            )
            fallback_vehicle_ts = soc_timestamp(
                fallback_vehicle.get('soc_source_ts')
                if isinstance(fallback_vehicle, dict) else None
            )
            fallback_vehicle_soc = soc_percent_value(
                fallback_vehicle.get('soc')
                if isinstance(fallback_vehicle, dict) else None
            )
            fallback_vehicle_usable = bool(
                fallback_vehicle
                and fallback_vehicle_soc is not None
                and fallback_vehicle.get('soc_rule_confirmed') is True
                and 'soc_source_ts' in fallback_vehicle
                and fallback_wb_idx is not None
                and not soc_record_vetoed(fallback_vehicle)
                and soc_record_binding_valid(
                    fallback_vehicle,
                    fallback_wb_idx,
                    fallback_vehicle.get('id', ''),
                    require_plugged=True,
                    expected_vehicle=fallback_vehicle,
                    # Genau ein Fahrzeug und genau eine verriegelte Wallbox
                    # bilden den einzigen slotlosen Legacy-Fallback.
                    allow_legacy_missing_slot=True,
                )
                and soc_source_rule_confirmed(
                    fallback_vehicle.get(
                        'soc_source',
                        fallback_vehicle.get('source', ''),
                    ),
                    fallback_vehicle.get('soc_rule_confirmed'),
                    fallback_vehicle_ts,
                )
            )
            car_soc = (
                primary_sess['current_virtual_soc']
                if primary_sess_usable
                else fallback_vehicle_soc
                if fallback_vehicle_usable
                else None
            )

            car_needs_energy = True
            if car_soc is not None:
                p_target = primary_sess['target_soc'] if primary_sess else CAR_TARGET_SOC
                if car_soc >= (p_target - 1): car_needs_energy = False

            if V2H_ENABLE == 1:
                v2h_allowed = True
                v2h_reason = ""
                if not (wb1_locked or wb2_locked):
                    v2h_allowed = False
                    v2h_reason = "Kein Fahrzeug"
                elif car_soc is None:
                    v2h_allowed = False
                    v2h_reason = "SoC unbekannt"
                elif soc > V2H_BAT_SOC_LIMIT:
                    v2h_allowed = False
                    v2h_reason = "Hausakku > Limit"
                elif car_soc <= V2H_MIN_SOC:
                    v2h_allowed = False
                    v2h_reason = "Autoakku am Limit"
                if not v2h_allowed and e3dc.get('wb', 0) < -50:
                    # V2H/V2G bleibt beobachtend. Das Ergebnis wird als Telemetrie
                    # exportiert, aber dieser Dienst darf nie zweiter Wallbox-Schreiber
                    # werden oder CP, Phasen beziehungsweise Leistung ändern.
                    logger.warning(
                        "[V2H] Read-only monitoring reports a limit violation; "
                        "no wallbox command is issued."
                    )

            car_blocks_pause = False
            car_blocks_boost = False
            if wb1_locked or wb2_locked:
                pwr = abs(e3dc.get('wb', 0)) + abs(e3dc.get('wb2', 0))
                if pwr < 200:
                    car_blocks_pause = True
                    car_blocks_boost = True
                    if grid <= -4150 or (car_soc is not None and not car_needs_energy):
                        car_blocks_pause = False
                        car_blocks_boost = False
                else:
                    car_blocks_pause = True
                    car_blocks_boost = False
            car_blocks_boost_applied = bool(car_blocks_boost and energy_autonomy_allowed)

            if SMART_WBHOUR_ENABLE == 1 and car_soc is not None:
                current_wbh = get_cfg_int(current_config, 'wbhour', -1)
                if not smart_wbhour_handoff_logged:
                    logger.info(
                        "Smart-wbhour wird vom Wallbox-Planer gefuehrt; "
                        "Energy Manager schreibt keine Wallbox-Ladeplanung mehr."
                    )
                    smart_wbhour_handoff_logged = True
                if not car_needs_energy:
                    if current_wbh > 0:
                        logger.debug("Smart-wbhour Ziel erreicht; Plan-Reset liegt beim Wallbox-Planer.")
                else:
                    max_h = 0
                    for s in [processed_wb1, processed_wb2]:
                        if s:
                            m_s = max(0, s['target_soc'] - s['current_virtual_soc'])
                            m_k = (m_s / 100.0) * s['car_capacity'] * 1.12
                            p_kw = get_cfg_value(current_config, f'wb{1 if s==processed_wb1 else 2}_charge_power', CAR_CHARGE_POWER)
                            if p_kw <= 0: p_kw = 11.0
                            h = int(math.ceil(m_k / float(p_kw)))
                            if h > max_h: max_h = h

                    # --- Wakeup Trigger & Guest Car Logic ---
                    if e3dc_valid:
                        # Wakeup Trigger (Force Refresh)
                        if (wb1_locked and not last_wb_locked) or (wb2_locked and not last_wb2_locked):
                            if (time.time() - last_wakeup_trigger_time) > 900:
                                logger.info("Fahrzeug angesteckt. Fordere SoC-Refresh an.")
                                try:
                                    with open("/var/www/html/ramdisk/force_bluelink.flag", 'w') as f: f.write("1")
                                    last_wakeup_trigger_time = time.time()
                                except: pass

                            # Action-Push
                            try:
                                actions_json = json.dumps([
                                    {"action": "wb_direct_99", "title": "Maximal (99h)"},
                                    {"action": "wb_pv_50", "title": "Auf 50%, dann PV"},
                                    {"action": "wb_stop_direct", "title": "Klar! Nur smart / PV"}
                                ])
                                send_webpush('push_notify_plugged', 'Fahrzeug angesteckt 🔌', 'Soll sofort ein Ladevorgang gestartet werden?', url="/mobile.php?seite=charging", actions=actions_json)
                            except Exception as e:
                                logger.error(f"Fehler bei Action-Push: {e}")
                        last_wb_locked = wb1_locked
                        last_wb2_locked = wb2_locked

                        # Push: Ziel SoC erreicht
                        if car_soc is not None:
                            if not car_needs_energy and (wb1_locked or wb2_locked):
                                if not last_push_soc_sent:
                                    send_webpush('push_notify_soc', 'Ladeziel erreicht', f"Fahrzeug hat das gesetzte Ziel von {p_target}% erreicht.")
                                    last_push_soc_sent = True
                            elif car_needs_energy and (wb1_locked or wb2_locked):
                                last_push_soc_sent = False

                        # Push: Fahrzeug nicht angesteckt bei Ladeplanung (nur abends zwischen 18 und 23 Uhr zur Erinnerung max 1x)
                        if not wb1_locked and not wb2_locked:
                            active_plan = False
                            if current_wbh > 0: active_plan = True

                            if active_plan:
                                h = now.hour
                                if (18 <= h <= 23) and not last_push_unplugged_sent:
                                    send_webpush('push_notify_unplugged', 'Fahrzeug nicht angesteckt!', "Eine intelligente Ladeplanung für die Wallbox steht an, aber es ist kein Fahrzeug verbunden.")
                                    last_push_unplugged_sent = True
                        else:
                            last_push_unplugged_sent = False

                        # Gast-Auto Erkennung (WB belegt aber Cloud meldet abgesteckt)
                        # Hinweis: Wir prüfen hier nur das primäre Fahrzeug (wb1) für die Einfachheit
                        if wb1_locked and all_vehicles:
                            target_v = next(
                                (v for v in all_vehicles if v.get('id') == wb1_car_id),
                                None,
                            )
                            if target_v is None and wb1_car_id == 'car1':
                                target_v = _single_vehicle_fallback(all_vehicles)
                            if (target_v is not None
                                and not target_v.get('is_plugged_in', True)
                                and (time.time() - last_wakeup_trigger_time) > 60):
                                # Wenn nach Wakeup immer noch abgesteckt gemeldet wird -> Gast
                                if not guest_car_active:
                                    logger.info("Gast-Auto an WB1 erkannt (Cloud meldet 'abgesteckt').")
                                    guest_car_active = True
                            else:
                                guest_car_active = False
                        else:
                            guest_car_active = False
                    if current_wbh > 0 and 0 < max_h < current_wbh and (wb1_locked or wb2_locked): max_h = current_wbh
                    if current_wbh != max_h:
                        logger.debug(
                            "Smart-wbhour Bedarf waere %sh; Wallbox-Planer berechnet und schreibt den Plan.",
                            max_h,
                        )
            # --- Ende Fahrzeug & SoC management ---

            if (
                (
                    e3dc_valid
                    and _safe_float(soc, 0.0) > 0.0
                    and _safe_float(soc, 0.0)
                    < max(5.0, _safe_float(MIN_SOC, 80.0) - 5.0)
                )
                or _safe_float(grid, 0.0) > 2500.0
            ):
                heatpump_positive_output_blocked_this_cycle = True
                heatpump_positive_output_block_reasons.append(
                    "pre_control_independent_safety_stop"
                )

            # Manueller Boost Check
            manual_boost_command = read_manual_boost_command()
            if manual_boost_command.get("present") and not manual_boost_command.get("valid"):
                _warn_once(
                    f"manual_boost_command:{manual_boost_command.get('reason')}",
                    "Manueller Wärmepumpenauftrag wird nicht ausgeführt (%s).",
                    manual_boost_command.get("reason"),
                )
            if (
                manual_boost_command.get("valid")
                and wp
                and manual_boost_command_is_current(manual_boost_command)
            ):
                try:
                    if (
                        manual_boost_command.get("action") == "on"
                        and manual_boost_command.get("schema")
                        != "manual_heatpump_command_v1"
                    ):
                        _warn_once(
                            "legacy_manual_heatpump_on_blocked",
                            "Historischer manueller Wärme-Boost bleibt gesperrt; "
                            "für eine positive Komfortkante ist der typisierte "
                            "manual_heatpump_command_v1-Auftrag erforderlich.",
                        )
                    elif manual_boost_command.get("action") == "off":
                        heatpump_positive_output_blocked_this_cycle = True
                        heatpump_positive_output_block_reasons.append(
                            "manual_user_off"
                        )
                        if wp_write_allowed:
                            if wp.set_boost(0, None, 0, CONF_WWW):
                                last_wp_command_time = time.time()
                                heatpump_positive_signal_started_ts = 0.0
                                heatpump_positive_signal_demand_class = "none"
                                heatpump_positive_signal_restored_unconfirmed = False
                                heatpump_positive_signal_start_reservation_allowed = False
                                heatpump_positive_signal_hold_guard = {}
                                if not consume_manual_boost_command(manual_boost_command):
                                    logger.info(
                                        "Manueller Boost wurde beendet; ein neuerer Auftrag bleibt zur Verarbeitung liegen."
                                    )
                            else:
                                logger.error(
                                    "Manueller Boost-OFF-Auftrag bleibt offen: Treiber-Release nicht bestätigt."
                                )
                    elif wq_aus < WQ_MIN_TEMP:
                        heatpump_positive_output_blocked_this_cycle = True
                        heatpump_positive_output_block_reasons.append(
                            "manual_source_temperature_stop"
                        )
                        if wp_write_allowed:
                            logger.warning(f"NOT-AUS (Manuell): WQ Aus zu kalt ({wq_aus}°C).")
                            if wp.set_boost(0, None, 0, CONF_WWW):
                                last_wp_command_time = time.time()
                                heatpump_positive_signal_started_ts = 0.0
                                heatpump_positive_signal_demand_class = "none"
                                heatpump_positive_signal_restored_unconfirmed = False
                                heatpump_positive_signal_start_reservation_allowed = False
                                heatpump_positive_signal_hold_guard = {}
                                consume_manual_boost_command(manual_boost_command)
                    elif soc < MANUAL_BOOST_MIN_SOC:
                        heatpump_positive_output_blocked_this_cycle = True
                        heatpump_positive_output_block_reasons.append(
                            "manual_low_soc_stop"
                        )
                        if wp_write_allowed:
                            logger.info(f"Manueller Boost gestoppt: SoC niedrig ({soc}%).")
                            if wp.set_boost(0, None, 0, CONF_WWW):
                                last_wp_command_time = time.time()
                                heatpump_positive_signal_started_ts = 0.0
                                heatpump_positive_signal_demand_class = "none"
                                heatpump_positive_signal_restored_unconfirmed = False
                                heatpump_positive_signal_start_reservation_allowed = False
                                heatpump_positive_signal_hold_guard = {}
                                consume_manual_boost_command(manual_boost_command)
                    elif (time.time() - manual_boost_command.get("mtime", time.time())) > (MANUAL_BOOST_MAX_DURATION * 60):
                        heatpump_positive_output_blocked_this_cycle = True
                        heatpump_positive_output_block_reasons.append(
                            "manual_command_expired"
                        )
                        if wp_write_allowed:
                            logger.info("Manueller Boost abgelaufen.")
                            if wp.set_boost(0, None, 0, CONF_WWW):
                                last_wp_command_time = time.time()
                                heatpump_positive_signal_started_ts = 0.0
                                heatpump_positive_signal_demand_class = "none"
                                heatpump_positive_signal_restored_unconfirmed = False
                                heatpump_positive_signal_start_reservation_allowed = False
                                heatpump_positive_signal_hold_guard = {}
                                consume_manual_boost_command(manual_boost_command)
                    else:
                        # Keep-Alive fuer manuellen Boost: Temperaturwerte alle 30s nachschreiben
                        # damit Config-Aenderungen (z.B. www=55) sofort wirken
                        if (
                            wp_write_allowed
                            and not heatpump_positive_output_blocked_this_cycle
                        ):
                            if (time.time() - last_wp_command_time) > 30:
                                if at_mittel > HEIZGRENZE_TEMP:
                                    if wp_type == 1:
                                        wp.set_boost(
                                            0,
                                            None,
                                            1,
                                            CONF_WWS,
                                            cooling_boost_mode,
                                            CONF_KHL,
                                            wp_data=wp_data,
                                        )
                                    else:
                                        wp.set_boost(0, None, 1, CONF_WWS, wp_data=wp_data)
                                else:
                                    wp.set_boost(1, CONF_HZ, 1, CONF_WWW, wp_data=wp_data)
                                last_wp_command_time = time.time()
                            else:
                                wp.keep_alive(force_open=True)
                except Exception as e: logger.error(f"Fehler Manual-Boost: {e}")

            # --- HAUPT REGELUNG (Wärmepumpe) ---
            if not os.path.exists(FLAG_FILE) and AUTO_MODE == 0 and boost_active and wp:
                # PV-Automatik wurde ausgeschaltet, aber ein Boost ist noch aktiv -> Hart beenden!
                # Wir ignorieren hier wp_write_allowed, da ein globaler Ausschaltbefehl sofort wirken muss.
                logger.info("PV-Automatik deaktiviert: Beende aktiven PV-Boost.")
                heatpump_positive_output_blocked_this_cycle = True
                heatpump_positive_output_block_reasons.append(
                    "automatic_mode_user_off"
                )
                if wp.set_boost(0, None, 0, CONF_WWW):
                    last_wp_command_time = time.time()
                    heatpump_positive_signal_started_ts = 0.0
                    heatpump_positive_signal_demand_class = "none"
                    heatpump_positive_signal_restored_unconfirmed = False
                    heatpump_positive_signal_start_reservation_allowed = False
                    heatpump_positive_signal_hold_guard = {}
                    boost_active = False; pv_pause_active = False; pre_pause_active = False
                    pv_pause_owner = "none"; source_recovery_pause_context = {}
                    pv_boost_pending_start = None

            if not os.path.exists(FLAG_FILE) and AUTO_MODE == 1 and wp:
                try:
                    # Ein echtes Defizit liegt nur vor, wenn wir Strom aus dem Netz beziehen UND die Batterie nicht massiv lädt,
                    # ODER wenn wir die Batterie entladen (bat < -50) und nicht massiv einspeisen (grid > -100)
                    is_deficit = (grid > 50 and bat < 100) or (bat < -50 and grid > -100)

                    # PV PAUSE
                    source_recovery_pause_active = bool(
                        pv_pause_active
                        and pv_pause_owner == "source_recovery_heatpump"
                    )
                    source_recovery_history = source_recovery_history_gate(
                        heatpump_pause_request,
                        pause_active=source_recovery_pause_active,
                        compressor_running=wp_compressor_running_now,
                        compressor_history_valid=bool(
                            wp_compressor_history_valid
                            and wp_compressor_observation_valid
                        ),
                        compressor_last_stop_ts=wp_compressor_last_stop_ts,
                        now_ts=time.time(),
                    )
                    source_recovery_history_allowed = bool(source_recovery_history.get("allowed"))
                    source_recovery_history_reason = str(source_recovery_history.get("reason") or "")
                    source_recovery_compressor_off_before_s = source_recovery_history.get("compressor_off_before_s")
                    source_recovery_planned_pause_s = _safe_float(
                        source_recovery_history.get("planned_pause_s"),
                        0.0,
                    )
                    if (
                        isinstance(heatpump_pause_request, dict)
                        and heatpump_pause_request.get("active")
                        and heatpump_pause_request.get("owner") == "source_recovery_heatpump"
                        and not source_recovery_pause_active
                        and not source_recovery_history_allowed
                    ):
                        blocked_request = dict(heatpump_pause_request)
                        blocked_request["active"] = False
                        blocked_request["history_gate_allowed"] = False
                        blocked_request["history_gate_reason"] = source_recovery_history_reason
                        heatpump_pause_request = blocked_request
                    source_recovery_latch = source_recovery_pause_latch(
                        heatpump_pause_request,
                        source_recovery_pause_active,
                        source_recovery_pause_context,
                        current_price,
                        now_ts=time.time(),
                        grace_s=SOURCE_RECOVERY_REQUEST_GRACE_S,
                    )
                    source_recovery_pause_requested = bool(source_recovery_latch.get("requested"))
                    source_recovery_request_seen = bool(source_recovery_latch.get("seen"))
                    source_recovery_pause_allowed = bool(source_recovery_latch.get("allowed"))
                    source_recovery_pause_latched = bool(source_recovery_latch.get("cached_fresh"))
                    heatpump_pause_request = source_recovery_latch.get("request") or {}
                    source_recovery_pause_context = source_recovery_latch.get("context") or {}
                    if heat_policy_runtime_enabled:
                        source_recovery_pre_policy_state = source_recovery_heat_override_state(
                            source_recovery_pause_allowed=source_recovery_pause_allowed,
                            source_recovery_pause_active=source_recovery_pause_active,
                            free_for_limbs_w=heat_policy_budget_w,
                            grid_start_limit=GRID_START_LIMIT,
                            storage_manager_owns_energy=storage_manager_owns_energy,
                            soc=soc,
                            min_soc=MIN_SOC,
                            ww_cycle_started_ts=wp_last_ww_cycle_start_ts,
                            now_ts=time.time(),
                        )
                    else:
                        source_recovery_pre_policy_state = {
                            "override": False,
                            "budget_ready": False,
                            "release_reason": "",
                            "blocks_boost": bool(source_recovery_pause_allowed or source_recovery_pause_active),
                            "ww_cycle_min_runtime_remaining_s": 0.0,
                        }
                    source_recovery_heat_budget_override = bool(source_recovery_pre_policy_state.get("override"))
                    source_recovery_budget_ready = bool(source_recovery_pre_policy_state.get("budget_ready"))
                    source_recovery_release_reason = str(source_recovery_pre_policy_state.get("release_reason") or "")
                    heatpump_pause_blocks_boost = bool(source_recovery_pre_policy_state.get("blocks_boost"))
                    source_recovery_pause_blocks_boost = heatpump_pause_blocks_boost
                    source_recovery_pause_eligible = bool(source_recovery_pause_allowed and heatpump_pause_blocks_boost)
                    effective_pv_pause_enable = 1 if source_recovery_pause_eligible else PV_PAUSE_ENABLE
                    pause_label = "Quell-Erholung" if source_recovery_pause_eligible else "PV-Pause"
                    pause_min_at = (
                        _safe_float(heatpump_pause_request.get("min_outdoor_temp_c"), PV_PAUSE_MIN_AT)
                        if source_recovery_pause_eligible
                        else PV_PAUSE_MIN_AT
                    )
                    pause_timeout_s = (
                        max(60, _safe_int(heatpump_pause_request.get("timeout_s"), int(PV_PAUSE_TIMEOUT_MINUTES * 60)))
                        if source_recovery_pause_eligible
                        else int(PV_PAUSE_TIMEOUT_MINUTES * 60)
                    )
                    pause_restart_block_s = (
                        max(0, _safe_int(heatpump_pause_request.get("restart_block_s"), 3 * 3600))
                        if source_recovery_pause_eligible
                        else 3 * 3600
                    )
                    pause_max_drop_k = (
                        _safe_float(heatpump_pause_request.get("max_temp_drop_k"), PV_PAUSE_MAX_TEMP_DROP)
                        if source_recovery_pause_eligible
                        else PV_PAUSE_MAX_TEMP_DROP
                    )
                    source_recovery_active_before_pause_logic = bool(
                        pv_pause_active
                        and pv_pause_owner == "source_recovery_heatpump"
                    )
                    source_recovery_release_pending = bool(
                        source_recovery_active_before_pause_logic
                        and not source_recovery_pause_eligible
                    )
                    if (
                        effective_pv_pause_enable == 1
                        and current_price > 0
                        and not source_recovery_release_pending
                    ):
                        # Auskühlschutz: Keine Pause bei tiefen Temperaturen
                        if at < pause_min_at:
                            if pv_pause_active:
                                if wp_write_allowed:
                                    logger.info(f"PV-Pause beendet (Auskühlschutz, AT {at}°C < Limit {PV_PAUSE_MIN_AT}°C).")
                                    if release_heatpump_pause(wp, wp_type, pv_pause_owner, CONF_WWW):
                                        last_wp_command_time = time.time()
                                        pv_pause_active = False; boost_active = False; pv_pause_start_time = None

                        elif car_blocks_pause:
                            if pv_pause_active:
                                if wp_write_allowed:
                                    logger.info("PV-Pause beendet (Auto an Wallbox hat Priorität).")
                                    if release_heatpump_pause(wp, wp_type, pv_pause_owner, CONF_WWW):
                                        last_wp_command_time = time.time()
                                        pv_pause_active = False; boost_active = False; pv_pause_start_time = None
                                        pv_pause_pending_end = None

                        elif pv_pause_active:
                            # Temperatur-Hysterese prüfen (Auskühlschutz)
                            is_idm = (wp_type == 1)
                            if is_idm:
                                # IDM ist Vorlaufgeregelt
                                curr_ist = wp_data.get('Vorlauf_Ist', 30.0)
                                curr_soll = wp_data.get('Vorlauf_Soll', 30.0)
                                temp_drop = curr_soll - curr_ist
                                max_drop = 4.0
                                log_var = f"VL {curr_ist}°C"
                            else:
                                curr_ist = wp_data.get('Ruecklauf_Extern', 30.0) if RL_SOURCE == 'external' else wp_data.get('Ruecklauf_Ist', 30.0)
                                curr_soll = wp_data.get('Ruecklauf_Soll', 30.0)
                                temp_drop = curr_soll - curr_ist
                                max_drop = pause_max_drop_k
                                log_var = f"RL {curr_ist}°C"

                            if temp_drop >= max_drop:
                                if wp_write_allowed:
                                    logger.warning(f"PV-Pause Notabbruch! Haus kühlt aus ({log_var} ist {temp_drop:.1f}K unter Soll {curr_soll}°C).")
                                    if release_heatpump_pause(wp, wp_type, pv_pause_owner, CONF_WWW):
                                        last_wp_command_time = time.time()
                                        pv_pause_active = False; boost_active = False; pv_pause_start_time = None
                                        pv_pause_pending_end = None
                                        pv_pause_blocked_until = time.time() + (4 * 3600)  # 4 Stunden Sperre!

                            elif (
                                (not source_recovery_pause_eligible)
                                and not wallbox_phase_transition_active
                                and heat_policy_budget_w >= abs(GRID_START_LIMIT)
                            ):
                                if pv_pause_pending_end is None:
                                    pv_pause_pending_end = now
                                elif (now - pv_pause_pending_end).total_seconds() > PV_BOOST_DELAY:
                                    if wp_write_allowed and automatic_heat_actuation_allowed:
                                        logger.info(f"PV-Pause beendet -> Gehirn-Budget bestätigt ({heat_policy_budget_w}W über {PV_BOOST_DELAY}s).")
                                        if release_heatpump_pause(wp, wp_type, pv_pause_owner, CONF_WWW):
                                            last_wp_command_time = time.time()
                                            pv_pause_active = False; boost_active = False; pv_pause_start_time = None
                                            pv_pause_pending_end = None
                                            if pv_pause_owner == "source_recovery_heatpump":
                                                cooldown_s = source_recovery_release_cooldown_s(
                                                    heatpump_pause_request,
                                                    PV_BOOST_DELAY,
                                                    source_recovery_pre_policy_state.get("ww_cycle_min_runtime_remaining_s", 0.0),
                                                )
                                                pv_pause_blocked_until = max(pv_pause_blocked_until, time.time() + cooldown_s)

                            elif e3dc_valid and soc > 0 and soc < (PV_PAUSE_SOC - 5):
                                if wp_write_allowed:
                                    logger.warning("PV-Pause abgebrochen (SoC tief).")
                                    if release_heatpump_pause(wp, wp_type, pv_pause_owner, CONF_WWW):
                                        last_wp_command_time = time.time()
                                        pv_pause_active = False; boost_active = False; pv_pause_start_time = None
                            elif pv_pause_start_time and (time.time() - pv_pause_start_time) > pause_timeout_s:
                                if wp_write_allowed:
                                    logger.warning("PV-Pause Timeout. Sperre erneute Pause für 3 Stunden.")
                                    if release_heatpump_pause(wp, wp_type, pv_pause_owner, CONF_WWW):
                                        last_wp_command_time = time.time()
                                        pv_pause_active = False; boost_active = False; pv_pause_start_time = None
                                        pv_pause_blocked_until = time.time() + pause_restart_block_s
                            elif forecast and not source_recovery_pause_eligible:
                                gmt = time.gmtime(); now_gmt = gmt.tm_hour + gmt.tm_min / 60.0
                                peak_still_valid = False; max_future_w = 0.0; current_w = 0.0
                                for entry in forecast:
                                    h = entry['h'];
                                    if h < (now_gmt - 12): h += 24
                                    if abs(h - now_gmt) < 0.25: current_w = entry['w']
                                    if now_gmt < h <= (now_gmt + 1.5) and entry['w'] > max_future_w: max_future_w = entry['w']
                                if max_future_w >= PV_PAUSE_WATT and max_future_w > (current_w * 1.1): peak_still_valid = True

                                if not peak_still_valid:
                                    if wp_write_allowed and automatic_heat_actuation_allowed:
                                        logger.info("PV-Pause beendet (Trend entfallen).")
                                        if release_heatpump_pause(wp, wp_type, pv_pause_owner, CONF_WWW):
                                            last_wp_command_time = time.time()
                                            pv_pause_active = False; boost_active = False; pv_pause_start_time = None

                            # Hysterese: Timer abbrechen, wenn Netzbezug um 500W über Limit steigt
                            if pv_pause_active and pv_pause_pending_end is not None and grid > (GRID_START_LIMIT + 500):
                                pv_pause_pending_end = None
                            elif not boost_active and soc >= PV_PAUSE_SOC and not car_blocks_pause and time.time() > pv_pause_blocked_until:
                                peak_found = False
                                if forecast:
                                    gmt = time.gmtime(); now_gmt = gmt.tm_hour + gmt.tm_min / 60.0
                                    max_future_w = 0.0; current_w = 0.0
                                    for entry in forecast:
                                        h = entry['h'];
                                        if h < (now_gmt - 12): h += 24
                                        if abs(h - now_gmt) < 0.25: current_w = entry['w']
                                        if now_gmt < h <= (now_gmt + 1.5) and entry['w'] > max_future_w: max_future_w = entry['w']
                                    if max_future_w >= PV_PAUSE_WATT and max_future_w > (current_w * 1.1): peak_found = True

                                    if peak_found:
                                        if wp_write_allowed and automatic_heat_actuation_allowed:
                                            logger.info(f"Starte PV-Pause (Prognose > {PV_PAUSE_WATT}W).")
                                            heatpump_positive_output_blocked_this_cycle = True
                                            heatpump_positive_output_block_reasons.append(
                                                "legacy_pv_pause"
                                            )
                                            if wp.set_boost(1, PAUSE_SETPOINT_C, 0, CONF_WWW):
                                                last_wp_command_time = time.time()
                                                heatpump_positive_signal_started_ts = 0.0
                                                heatpump_positive_signal_demand_class = "none"
                                                heatpump_positive_signal_restored_unconfirmed = False
                                                heatpump_positive_signal_start_reservation_allowed = False
                                                heatpump_positive_signal_hold_guard = {}
                                                pv_pause_active = True; boost_active = True; pv_pause_start_time = time.time()
                                                pv_pause_owner = "legacy_pv_pause"
                        elif not boost_active and not car_blocks_pause and time.time() > pv_pause_blocked_until:
                            peak_found = bool(source_recovery_pause_eligible)
                            if not peak_found and soc >= PV_PAUSE_SOC and forecast:
                                gmt = time.gmtime(); now_gmt = gmt.tm_hour + gmt.tm_min / 60.0
                                max_future_w = 0.0; current_w = 0.0
                                for entry in forecast:
                                    h = entry['h'];
                                    if h < (now_gmt - 12): h += 24
                                    if abs(h - now_gmt) < 0.25: current_w = entry['w']
                                    if now_gmt < h <= (now_gmt + 1.5) and entry['w'] > max_future_w: max_future_w = entry['w']
                                if max_future_w >= PV_PAUSE_WATT and max_future_w > (current_w * 1.1): peak_found = True

                            if (
                                peak_found
                                and wp_write_allowed
                                and automatic_heat_actuation_allowed
                            ):
                                pause_reason = str(heatpump_pause_request.get("reason") or f"Prognose > {PV_PAUSE_WATT}W")
                                logger.info(f"Starte {pause_label} ({pause_reason}).")
                                heatpump_positive_output_blocked_this_cycle = True
                                heatpump_positive_output_block_reasons.append(
                                    "source_recovery_pause"
                                    if source_recovery_pause_eligible
                                    else "legacy_pv_pause"
                                )
                                if wp.set_boost(1, PAUSE_SETPOINT_C, 0, CONF_WWW):
                                    last_wp_command_time = time.time()
                                    heatpump_positive_signal_started_ts = 0.0
                                    heatpump_positive_signal_demand_class = "none"
                                    heatpump_positive_signal_restored_unconfirmed = False
                                    heatpump_positive_signal_start_reservation_allowed = False
                                    heatpump_positive_signal_hold_guard = {}
                                    pv_pause_active = True; boost_active = True; pv_pause_start_time = time.time()
                                    pv_pause_owner = "source_recovery_heatpump" if source_recovery_pause_eligible else "legacy_pv_pause"
                    elif (
                        pv_pause_active
                        and not source_recovery_pause_eligible
                        and (
                            PV_PAUSE_ENABLE != 1
                            or pv_pause_owner == "source_recovery_heatpump"
                        )
                    ):
                        source_recovery_was_owner = pv_pause_owner == "source_recovery_heatpump"
                        pause_release_succeeded = False
                        if wp_write_allowed and automatic_heat_actuation_allowed:
                            pause_release_succeeded = release_heatpump_pause(
                                wp,
                                wp_type,
                                pv_pause_owner,
                                CONF_WWW,
                            )
                            if pause_release_succeeded:
                                last_wp_command_time = time.time()
                                if source_recovery_was_owner and source_recovery_heat_budget_override:
                                    logger.info(
                                        "Beende Quell-Erholung: %s.",
                                        source_recovery_release_reason or "Wärmebudget hat Vorrang",
                                    )
                                elif source_recovery_was_owner:
                                    logger.info(
                                        "Beende Quell-Erholung: geplanter Pausenendpunkt erreicht "
                                        "oder Quell-Erholungsauftrag beendet."
                                    )
                                else:
                                    logger.info("Beende alte PV-Pause: PV-Pause ist deaktiviert oder Storage Manager ist Besitzer der Energieverteilung.")
                            elif (time.time() - last_source_recovery_release_error_log_time) >= 60.0:
                                logger.warning("Pausenfreigabe konnte nicht geschrieben werden; Zustand bleibt aktiv.")
                                last_source_recovery_release_error_log_time = time.time()
                        if pause_release_succeeded:
                            pv_pause_active = False
                            source_recovery_pause_active = False
                            pv_pause_start_time = None
                            pv_pause_pending_end = None
                            boost_active = bool(price_boost_active or pre_pause_active)
                    if source_recovery_active_before_pause_logic and not pv_pause_active:
                        cooldown_s = source_recovery_release_cooldown_s(
                            source_recovery_pause_context or heatpump_pause_request,
                            PV_BOOST_DELAY,
                            source_recovery_pre_policy_state.get("ww_cycle_min_runtime_remaining_s", 0.0),
                        )
                        pv_pause_blocked_until = max(
                            pv_pause_blocked_until,
                            time.time() + cooldown_s,
                        )
                    if not pv_pause_active:
                        pv_pause_owner = "none"
                        source_recovery_pause_context = {}

                    # PREIS BOOST
                    heat_energy_now_ts = time.time()
                    observed_price_wp_power_w, observed_price_power_known, _observed_price_accepting = heatpump_power_observation(
                        wp_data,
                        wp_status,
                    )
                    if price_boost_active and heat_policy_last_energy_ts > 0 and observed_price_power_known:
                        elapsed_h = max(0.0, heat_energy_now_ts - heat_policy_last_energy_ts) / 3600.0
                        heat_policy_boost_delivered_kwh += max(0, observed_price_wp_power_w) * elapsed_h / 1000.0
                    heat_policy_last_energy_ts = heat_energy_now_ts if price_boost_active else 0.0

                    price_action = "NONE"
                    effective_price_limit = PRICE_LIMIT
                    estimated_cop = 3.5
                    predump_heatpump_raw_active = predump_allows("heatpump")
                    predump_heatpump_targets_reached = heatpump_boost_targets_reached(
                        wp_data, at_mittel, HEIZGRENZE_TEMP, CONF_WWS, CONF_WWW, CONF_HZ
                    )
                    predump_heatpump_protect_block = bool(wq_aus < WQ_MIN_TEMP)
                    predump_heatpump_hold_active = bool(predump_heatpump_hold_until and time.time() < predump_heatpump_hold_until)
                    predump_heatpump_active = bool(
                        (predump_heatpump_raw_active or predump_heatpump_hold_active)
                        and not predump_heatpump_targets_reached
                        and not predump_heatpump_protect_block
                    )

                    # `price_boost_enable` erzeugt hier absichtlich keine
                    # aktive Preisentscheidung mehr. Allgemeine günstige oder
                    # teure Fenster werden nur im heat_intent-Shadowvertrag
                    # bewertet. Aktive BOOST-Werte dürfen weiterhin nur aus
                    # den getrennten, zentral gebundenen Negativpreis- oder
                    # Pre-Dump-Verträgen stammen.

                    market_heatpump_release = market_plan_allows("heatpump", current_config)
                    market_heatpump_requested = (
                        heatpump_negative_price_release_requested(
                            market_heatpump_release
                        )
                    )
                    # Die Wärmepumpe akzeptiert den Negativpreis-Sonderpfad
                    # ausschließlich aus dem Storage-Manager-Vertrag und nur
                    # unter der zentralen Heat Policy. Der historische
                    # price_boost_plan bleibt für diesen Aktor wirkungslos.
                    market_heatpump_active = bool(
                        market_heatpump_requested
                        and heat_policy_runtime_enabled
                    )
                    legacy_price_heatpump_active = False
                    if market_heatpump_active or predump_heatpump_active:
                        price_action = "BOOST"
                    if (
                        (not heat_policy_runtime_enabled)
                        and source_recovery_pause_allowed
                        and price_action == "BOOST"
                        and not predump_heatpump_active
                    ):
                        price_action = "NONE"
                    price_candidate_requested = bool(
                        price_action == "BOOST"
                        or price_action == "PAUSE_HIGH_PRICE"
                    )
                    if price_candidate_requested and not price_boost_active and not predump_heatpump_active:
                        price_heatpump_start_block_remaining_s = heatpump_takt_start_block(
                            WP_TAKT_PROTECT,
                            wp_last_pv_boost_stop_ts,
                            WP_RESTART_BLOCK_MIN,
                        )
                        price_heatpump_takt_start_blocked = price_heatpump_start_block_remaining_s > 0.0
                    price_value_for_policy = _safe_float(current_price, 99.9)
                    price_valid_for_policy = current_price is not None and price_value_for_policy < 99.0
                    battery_empty_soc = _safe_float(
                        _heat_config_value(
                            current_config,
                            ("heat_price_block_empty_soc",),
                            max(5.0, min(15.0, _safe_float(MIN_SOC, 80.0) - 20.0)),
                        ),
                        10.0,
                    )
                    # Allgemeine Hochpreisfenster bleiben bis zu einem eigenen
                    # Nutzer- und Laufzeitvertrag reine Shadow-Evidenz. Der
                    # zentrale Heat-Runtime-Schalter allein autorisiert weder
                    # eine Preis-Pause noch den Abbruch eines WW-Zyklus.
                    heat_price_block_control_authorized = False
                    if (
                        heat_price_block_control_authorized
                        and price_valid_for_policy
                        and price_value_for_policy
                        >= _safe_float(PRICE_PAUSE_LIMIT, 35.0)
                        and _safe_float(soc, 0.0) <= battery_empty_soc
                    ):
                        if heat_price_block_started_ts <= 0:
                            heat_price_block_started_ts = time.time()
                    else:
                        heat_price_block_started_ts = 0.0
                    heat_policy_decision, heat_forecast_result = build_heatpump_policy_decision(
                        config=current_config,
                        now_ts=time.time(),
                        auto_mode=AUTO_MODE == 1,
                        wp=wp,
                        wp_data=wp_data,
                        wp_status=wp_status,
                        at_mittel=at_mittel,
                        heizgrenze_temp=HEIZGRENZE_TEMP,
                        conf_wws=CONF_WWS,
                        conf_hz=CONF_HZ,
                        free_for_limbs_w=heat_policy_budget_w,
                        grid_start_limit=GRID_START_LIMIT,
                        soc=soc,
                        min_soc=MIN_SOC,
                        current_price=current_price,
                        price_action=price_action,
                        price_pause_limit=PRICE_PAUSE_LIMIT,
                        price_hard_limit=PRICE_HARD_LIMIT,
                        market_heatpump_release=market_heatpump_release,
                        legacy_price_heatpump_active=legacy_price_heatpump_active,
                        predump_heatpump_active=predump_heatpump_active,
                        boost_active=boost_active,
                        price_boost_active=price_boost_active,
                        is_ww_timer_running=is_ww_timer_running,
                        ww_cycle_started_ts=wp_last_ww_cycle_start_ts,
                        source_protection_active=predump_heatpump_protect_block,
                        restart_block_remaining_s=price_heatpump_start_block_remaining_s,
                        price_block_started_ts=heat_price_block_started_ts,
                        boost_delivered_kwh=heat_policy_boost_delivered_kwh,
                        price_block_control_authorized=(
                            heat_price_block_control_authorized
                        ),
                        previous_target_state=(
                            heat_policy.TARGET_BOOST
                            if (price_boost_active or predump_heatpump_active)
                            else (
                                heat_policy.TARGET_PV_SURPLUS
                                if (boost_active and not pv_pause_active and not pre_pause_active)
                                else heat_policy.TARGET_NORMAL
                            )
                        ),
                        previous_sg_ready_state=(
                            heat_policy.SG_READY_BOOST
                            if (price_boost_active or predump_heatpump_active)
                            else (
                                heat_policy.SG_READY_PV
                                if (boost_active and not pv_pause_active and not pre_pause_active)
                                else heat_policy.SG_READY_NORMAL
                            )
                        ),
                        previous_available_budget_w=heat_policy_budget_w,
                        wp_type=wp_type,
                    )
                    heat_intent_export, heat_intent_runtime_validation = (
                        build_heat_intent_shadow_projection(
                            e3dc,
                            now_ts=time.time(),
                            heat_policy_decision=heat_policy_decision,
                        )
                    )
                    heat_runtime_actuation = heat_runtime_actuation_contract(
                        heat_policy_runtime_enabled,
                        validated_intent=heat_intent_export,
                        runtime_validation=heat_intent_runtime_validation,
                        policy_owner=heat_policy_decision.owner,
                        legacy_owner_contract=legacy_heat_automation_owner,
                        now_ts=time.time(),
                    )
                    automatic_heat_actuation_allowed = (
                        automatic_heat_policy_actuation_allowed(
                            heat_policy_runtime_enabled,
                            heat_runtime_actuation,
                        )
                    )
                    heat_policy_target = heat_policy_decision.target_state
                    heat_policy_protected_hold = bool(
                        heat_policy_target == heat_policy.TARGET_PROTECTED
                        and heat_policy_decision.owner in ("ww_cycle", "defrost", "legionella", "hardware_protection")
                    )
                    heat_policy_allows_active_heatpump = bool(
                        heat_policy_target in (
                            heat_policy.TARGET_PV_SURPLUS,
                            heat_policy.TARGET_BOOST,
                            heat_policy.TARGET_PRE_DUMP,
                        )
                        or heat_policy_protected_hold
                    )
                    source_recovery_policy_state = source_recovery_pre_policy_state
                    if heat_policy_runtime_enabled:
                        source_recovery_policy_state = source_recovery_heat_override_state(
                            source_recovery_pause_allowed=source_recovery_pause_allowed,
                            source_recovery_pause_active=source_recovery_pause_active,
                            free_for_limbs_w=heat_policy_budget_w,
                            grid_start_limit=GRID_START_LIMIT,
                            storage_manager_owns_energy=storage_manager_owns_energy,
                            soc=soc,
                            min_soc=MIN_SOC,
                            heat_policy_decision=heat_policy_decision,
                            ww_cycle_started_ts=wp_last_ww_cycle_start_ts,
                            now_ts=time.time(),
                        )
                        source_recovery_heat_budget_override = bool(source_recovery_policy_state.get("override"))
                        source_recovery_budget_ready = bool(source_recovery_policy_state.get("budget_ready"))
                        if source_recovery_policy_state.get("release_reason"):
                            source_recovery_release_reason = str(source_recovery_policy_state.get("release_reason"))
                        heatpump_pause_blocks_boost = bool(source_recovery_policy_state.get("blocks_boost"))
                        source_recovery_pause_blocks_boost = heatpump_pause_blocks_boost
                        if (
                            source_recovery_heat_budget_override
                            and not wallbox_phase_transition_active
                            and pv_pause_active
                            and pv_pause_owner == "source_recovery_heatpump"
                        ):
                            pause_release_succeeded = False
                            if wp_write_allowed and automatic_heat_actuation_allowed:
                                pause_release_succeeded = release_heatpump_pause(
                                    wp,
                                    wp_type,
                                    pv_pause_owner,
                                    CONF_WWW,
                                )
                                if pause_release_succeeded:
                                    last_wp_command_time = time.time()
                                    logger.info(
                                        "Beende Quell-Erholung: %s.",
                                        source_recovery_release_reason or "Heat Policy gibt Wärme frei",
                                    )
                                elif (time.time() - last_source_recovery_release_error_log_time) >= 60.0:
                                    logger.warning("Quell-Erholung konnte nicht freigegeben werden; Wärmefreigabe wartet.")
                                    last_source_recovery_release_error_log_time = time.time()
                            if pause_release_succeeded:
                                cooldown_s = source_recovery_release_cooldown_s(
                                    heatpump_pause_request,
                                    PV_BOOST_DELAY,
                                    source_recovery_policy_state.get("ww_cycle_min_runtime_remaining_s", 0.0),
                                )
                                pv_pause_blocked_until = max(pv_pause_blocked_until, time.time() + cooldown_s)
                                pv_pause_active = False
                                source_recovery_pause_active = False
                                pv_pause_start_time = None
                                pv_pause_pending_end = None
                                pv_pause_owner = "none"
                                source_recovery_pause_context = {}
                                boost_active = bool(price_boost_active or pre_pause_active)
                            else:
                                heatpump_pause_blocks_boost = True
                                source_recovery_pause_blocks_boost = True
                    if heat_policy_runtime_enabled:
                        if heat_policy_decision.owner == "price_block":
                            price_action = "PAUSE_HIGH_PRICE"
                        elif price_action == "PAUSE_HIGH_PRICE":
                            price_action = "NONE"
                            heat_policy_price_gate_reason = heat_policy_decision.block_reason
                        elif price_action == "BOOST" and heat_policy_target not in (heat_policy.TARGET_BOOST, heat_policy.TARGET_PRE_DUMP):
                            if heat_policy_protected_hold and (price_boost_active or predump_heatpump_active):
                                heat_policy_price_gate_reason = heat_policy_decision.block_reason
                            else:
                                price_action = "NONE"
                                heat_policy_price_gate_reason = heat_policy_decision.block_reason
                        price_heatpump_start_requested = bool(
                            (
                                price_action == "BOOST"
                                and heat_policy_target in (heat_policy.TARGET_BOOST, heat_policy.TARGET_PRE_DUMP)
                            )
                            or (
                                price_action == "BOOST"
                                and heat_policy_protected_hold
                                and price_boost_active
                            )
                        )
                    else:
                        heat_policy_price_gate_reason = "Shadow only - heat_policy_runtime_enable=0"
                        price_heatpump_start_requested = bool(price_action == "BOOST")

                    if (
                        heat_policy_runtime_enabled
                        and not automatic_heat_actuation_allowed
                    ):
                        price_action = "NONE"
                        price_heatpump_start_requested = False
                        pv_boost_pending_start = None
                        heat_policy_price_gate_reason = str(
                            (heat_runtime_actuation.get("reason_codes") or [
                                "HEAT_RUNTIME_ACTUATION_NOT_AUTHORIZED"
                            ])[0]
                        )

                    if (
                        wp
                        and heatpump_positive_signal_started_ts > 0.0
                        and not heatpump_positive_signal_restored_unconfirmed
                    ):
                        heatpump_positive_signal_actuator_readback = (
                            heatpump_positive_actuator_readback(
                                {
                                    "wp": wp,
                                    "wp_type": wp_type,
                                    "wp_status": wp_status,
                                    "wp_data": wp_data,
                                    "current_config": current_config,
                                },
                                now_ts=time.time(),
                            )
                        )
                        readback_postdates_positive_edge = bool(
                            _safe_float(
                                heatpump_positive_signal_actuator_readback.get(
                                    "ts"
                                ),
                                0.0,
                            )
                            > heatpump_positive_signal_started_ts
                        )
                        if readback_postdates_positive_edge:
                            reconcile_heatpump_driver_cache_from_readback(
                                wp,
                                wp_type,
                                heatpump_positive_signal_actuator_readback,
                            )
                        if (
                            heatpump_positive_signal_actuator_readback.get(
                                "nonpositive_confirmed"
                            )
                            is True
                            and readback_postdates_positive_edge
                        ):
                            previous_signal_demand_class = (
                                heatpump_positive_signal_demand_class
                            )
                            logger.warning(
                                "Positive Wärmefreigabe physisch zurückgenommen "
                                "(%s: %s); alter Signalvertrag verfällt.",
                                heatpump_positive_signal_actuator_readback.get(
                                    "provider",
                                    "Aktor",
                                ),
                                heatpump_positive_signal_actuator_readback.get(
                                    "reason",
                                    "fresh_nonpositive_readback",
                                ),
                            )
                            heatpump_positive_signal_started_ts = 0.0
                            heatpump_positive_signal_demand_class = "none"
                            heatpump_positive_signal_restored_unconfirmed = False
                            heatpump_positive_signal_restart_budget_rearm_pending = (
                                False
                            )
                            heatpump_positive_signal_hold_guard = {}
                            heatpump_positive_signal_start_reservation_allowed = (
                                False
                            )
                            heatpump_budget_demand_active_class = "none"
                            heatpump_budget_demand_first_seen_ts = 0.0
                            heatpump_positive_signal_retry_not_before_ts = max(
                                heatpump_positive_signal_retry_not_before_ts,
                                time.time() + 60.0,
                            )
                            heatpump_positive_output_blocked_this_cycle = True
                            heatpump_positive_output_block_reasons.append(
                                "fresh_nonpositive_actuator_readback"
                            )
                            boost_active = False
                            price_boost_active = False
                            deficit_start_time = None
                            pv_boost_pending_start = None
                            cycle_actions.append({
                                "action": "positive_signal_readback_withdrawn",
                                "owner": previous_signal_demand_class,
                                "provider": (
                                    heatpump_positive_signal_actuator_readback.get(
                                        "provider",
                                        "none",
                                    )
                                ),
                                "new_start_requires_followup_budget": True,
                                "start_reservation_rearmed": False,
                            })
                            restart_revalidation[
                                "positive_signal_runtime_status"
                            ] = "fresh_nonpositive_readback_discarded"

                    # Positive Wärme-Nachfrage wird zuerst publiziert und erst
                    # mit einer danach erzeugten zentralen Wattzuteilung zur
                    # Aktorkante. Die Demand-Klasse bleibt Diagnose/Handshake;
                    # der Storage Manager ist weiterhin alleiniger Budget-Owner.
                    heatpump_positive_signal_window = (
                        build_heatpump_positive_signal_window(
                            heatpump_positive_signal_started_ts,
                            compressor_running=wp_compressor_running_now,
                            signal_readback_confirmed=(
                                not heatpump_positive_signal_restored_unconfirmed
                            ),
                            signal_hold_guard=(
                                heatpump_positive_signal_hold_guard
                            ),
                            clock_sample=control_time.sample(),
                            start_reservation_allowed=(
                                heatpump_positive_signal_start_reservation_allowed
                            ),
                            start_reservation_max_s=heatpump_start_reservation_max_s,
                            now_ts=time.time(),
                        )
                    )
                    heatpump_positive_signal_hold_guard = copy.deepcopy(
                        heatpump_positive_signal_window.get("hold_guard") or {}
                    )
                    ww_actual_raw = wp_data.get(
                        "Warmwasser_Ist",
                        wp_data.get("Warmwasser-Ist"),
                    )
                    wp_temperature_evidence_fresh = bool(
                        wp_status.get("valid") is True
                        and wp_status.get("source_fresh") is True
                    )
                    ww_actual_valid = bool(
                        wp_temperature_evidence_fresh
                        and not isinstance(ww_actual_raw, bool)
                        and isinstance(ww_actual_raw, (int, float))
                        and math.isfinite(float(ww_actual_raw))
                    )
                    ww_deadband_c = max(
                        0.1,
                        _safe_float(
                            current_config.get(
                                "heat_policy_temperature_deadband_c"
                            ),
                            0.2,
                        ),
                    )
                    manual_ww_target_c = (
                        CONF_WWS if at_mittel > HEIZGRENZE_TEMP else CONF_WWW
                    )
                    existing_ww_signal = bool(
                        heatpump_positive_signal_window.get("active") is True
                        and heatpump_positive_signal_demand_class.startswith(
                            "ww_"
                        )
                    )
                    manual_ww_demand = bool(
                        manual_ww_active
                        and (
                            (
                                ww_actual_valid
                                and float(ww_actual_raw)
                                < float(manual_ww_target_c) - ww_deadband_c
                            )
                            or (
                                not ww_actual_valid
                                and existing_ww_signal
                                and heatpump_positive_signal_window.get(
                                    "minimum_signal_hold_active"
                                )
                                is True
                            )
                        )
                    )
                    timer_ww_demand = bool(
                        WW_TIMER_ENABLE
                        and ww_timer_target_c is not None
                        and (
                            (
                                ww_actual_valid
                                and float(ww_actual_raw)
                                < float(ww_timer_target_c) - ww_deadband_c
                            )
                            or (
                                not ww_actual_valid
                                and existing_ww_signal
                                and heatpump_positive_signal_window.get(
                                    "minimum_signal_hold_active"
                                )
                                is True
                            )
                        )
                    )
                    pv_temperature = _heat_policy_temperature_context(
                        current_config,
                        wp_data,
                        at_mittel,
                        HEIZGRENZE_TEMP,
                        CONF_WWS,
                        CONF_HZ,
                    )
                    pv_temperature_demand = bool(
                        wp_temperature_evidence_fresh
                        and pv_temperature.get("temperature_valid")
                        and pv_temperature.get("temperature_max_c") is not None
                        and _safe_float(
                            pv_temperature.get("temperature_c"),
                            0.0,
                        )
                        < _safe_float(
                            pv_temperature.get("temperature_max_c"),
                            0.0,
                        )
                        - ww_deadband_c
                    )
                    if manual_ww_demand:
                        heatpump_budget_demand_class = "ww_immediate_manual"
                        heatpump_budget_demand_target_c = manual_ww_target_c
                    elif (
                        predump_heatpump_active
                        and not predump_heatpump_targets_reached
                        and not predump_heatpump_protect_block
                        and pv_temperature_demand
                    ):
                        heatpump_budget_demand_class = "pre_dump"
                    elif price_heatpump_start_requested and pv_temperature_demand:
                        heatpump_budget_demand_class = (
                            "market_price"
                            if market_heatpump_active
                            else "price"
                        )
                    elif (
                        AUTO_MODE == 1
                        and pv_temperature_demand
                    ):
                        heatpump_budget_demand_class = "pv_surplus"
                        heatpump_budget_demand_target_c = (
                            CONF_WWS if at_mittel > HEIZGRENZE_TEMP else CONF_HZ
                        )
                    elif timer_ww_demand:
                        heatpump_budget_demand_class = (
                            "ww_timer_comfort"
                            if ww_timer_window_active
                            else "ww_timer_eco"
                        )
                        heatpump_budget_demand_target_c = ww_timer_target_c


                    if (
                        heatpump_positive_signal_window.get("active") is True
                        and heatpump_budget_demand_class != "none"
                        and heatpump_budget_demand_class
                        != heatpump_positive_signal_demand_class
                    ):
                        previous_signal_demand_class = (
                            heatpump_positive_signal_demand_class
                        )
                        heatpump_positive_signal_demand_class = (
                            heatpump_budget_demand_class
                        )
                        signal_owner_is_ww = (
                            heatpump_positive_signal_demand_class.startswith(
                                "ww_"
                            )
                        )
                        boost_active = not signal_owner_is_ww
                        price_boost_active = bool(
                            heatpump_positive_signal_demand_class
                            in {"pre_dump", "market_price", "price"}
                        )
                        cycle_actions.append({
                            "action": "positive_signal_owner_transition",
                            "from": previous_signal_demand_class,
                            "to": heatpump_positive_signal_demand_class,
                            "signal_restarted": False,
                            "start_reservation_rearmed": False,
                        })

                    if (
                        heatpump_budget_demand_class
                        != heatpump_budget_demand_active_class
                    ):
                        heatpump_budget_demand_active_class = (
                            heatpump_budget_demand_class
                        )
                        heatpump_budget_demand_first_seen_ts = (
                            time.time()
                            if heatpump_budget_demand_class != "none"
                            else 0.0
                        )

                    central_heatpump_start_budget_gate = (
                        build_central_heatpump_start_budget_gate(
                            storage_budget_source_contract,
                            heatpump_budget_w,
                            max(0, abs(_safe_int(GRID_START_LIMIT, -3500))),
                            demand_class=heatpump_budget_demand_class,
                            demand_first_seen_ts=(
                                heatpump_budget_demand_first_seen_ts
                            ),
                            projected_boost_permission=(
                                central_heatpump_boost_permission_active
                                if wp_type == 0
                                else None
                            ),
                            now_ts=time.time(),
                        )
                    )
                    heat_demand_owner_allowed = (
                        automatic_heat_demand_actuation_allowed(
                            heatpump_budget_demand_class,
                            policy_actuation_allowed=(
                                automatic_heat_actuation_allowed
                            ),
                            ww_timer_owner_contract=(
                                ww_timer_automation_owner
                            ),
                            now_ts=time.time(),
                        )
                    )
                    automatic_heat_start_allowed = bool(
                        heat_demand_owner_allowed
                        and central_heatpump_start_budget_gate.get("allowed")
                        is True
                        and not heatpump_positive_output_blocked_this_cycle
                        and time.time()
                        >= heatpump_positive_signal_retry_not_before_ts
                    )
                    central_heatpump_start_budget_gate[
                        "retry_not_before_ts"
                    ] = heatpump_positive_signal_retry_not_before_ts
                    central_heatpump_start_budget_gate["retry_blocked"] = bool(
                        time.time()
                        < heatpump_positive_signal_retry_not_before_ts
                    )
                    central_heatpump_command_cap_w = (
                        bound_central_heatpump_command_cap_w(
                            storage_budget_source_contract,
                            heatpump_budget_w,
                            now_ts=time.time(),
                        )
                    )
                    if heatpump_positive_output_blocked_this_cycle:
                        automatic_heat_start_allowed = False
                        central_heatpump_command_cap_w = 0
                        central_heatpump_start_budget_gate[
                            "positive_output_cycle_blocked"
                        ] = True

                    (
                        running_heatpump_observed_w,
                        running_heatpump_power_known,
                        _running_heatpump_accepting,
                    ) = heatpump_power_observation(wp_data, wp_status)
                    running_heatpump_accounting_valid = bool(
                        wp_compressor_running_now
                        and running_heatpump_power_known
                        and wp_temperature_evidence_fresh
                    )
                    running_heatpump_accounting_w = max(
                        0,
                        _safe_int(running_heatpump_observed_w, 0),
                        max(0, _safe_int(heatpump_running_commitment_w, 0)),
                    )
                    running_heatpump_budget_tolerance_w = max(
                        50,
                        min(
                            500,
                            _safe_int(
                                current_config.get(
                                    "consumer_offer_acceptance_tolerance_w"
                                ),
                                250,
                            ),
                        ),
                    )
                    central_heatpump_effective_budget_w = max(
                        0,
                        _safe_int(central_heatpump_command_cap_w, 0),
                    )
                    central_heatpump_effective_budget_source = (
                        "command_allocation"
                        if central_heatpump_effective_budget_w > 0
                        else "none"
                    )
                    if (
                        wp_type == 0
                        and budget_is_fresh
                        and running_heatpump_accounting_valid
                        and type(heatpump_accounting_budget_w) is int
                        and heatpump_accounting_budget_w > 0
                        and abs(
                            heatpump_accounting_budget_w
                            - running_heatpump_accounting_w
                        )
                        <= running_heatpump_budget_tolerance_w
                    ):
                        # Eine laufende Modbus-Wärmepumpe benötigt keinen
                        # erneuten Start-Command. Ihre frisch gemessene und im
                        # zentralen Vertrag bestätigte Accounting-Leistung bleibt
                        # dennoch ein autorisiertes Wärme-Teilbudget.
                        central_heatpump_effective_budget_w = (
                            heatpump_accounting_budget_w
                        )
                        central_heatpump_effective_budget_source = (
                            "accepted_running_accounting"
                        )

                    luxtronik_ww_runtime_contract = (
                        luxtronik_ww_runtime_state(wp_status, wp_data)
                    )
                    luxtronik_ww_budget_demand_active = bool(
                        wp_type == 0
                        and at_mittel > HEIZGRENZE_TEMP
                        and str(
                            heatpump_budget_demand_class or "none"
                        ).strip().casefold()
                        in {
                            "pv_surplus",
                            "pre_dump",
                            "market_price",
                            "price",
                        }
                    )
                    luxtronik_ww_required_start_w = max(
                        1,
                        _safe_int(
                            central_heatpump_start_budget_gate.get(
                                "required_start_w"
                            ),
                            abs(_safe_int(GRID_START_LIMIT, -3500)),
                        ),
                    )
                    luxtronik_ww_fresh_start_budget = (
                        luxtronik_ww_fresh_start_budget_allowed(
                            luxtronik_ww_budget_demand_active,
                            central_heatpump_command_cap_w,
                            luxtronik_ww_required_start_w,
                            central_heatpump_boost_permission_active,
                            central_heatpump_start_budget_gate,
                        )
                    )
                    luxtronik_ww_budget_loss_guard_state = (
                        luxtronik_ww_budget_loss_guard(
                            luxtronik_ww_budget_loss_guard_state,
                            demand_active=(
                                luxtronik_ww_budget_demand_active
                            ),
                            signal_active=bool(
                                heatpump_positive_signal_started_ts > 0.0
                                or boost_active
                            ),
                            fresh_start_budget=(
                                luxtronik_ww_fresh_start_budget
                            ),
                            runtime_state=(
                                luxtronik_ww_runtime_contract.get("state")
                            ),
                            duration_s=max(
                                1.0,
                                _safe_float(STOP_DELAY_MINUTES, 10.0)
                                * 60.0,
                            ),
                            signal_release_allowed=bool(
                                heatpump_positive_signal_window.get(
                                    "normal_release_allowed"
                                )
                                is True
                            ),
                            clock_sample=control_time.sample(),
                        )
                    )
                    luxtronik_ww_budget_loss_blocked_until_fresh_budget = bool(
                        luxtronik_ww_budget_loss_guard_state.get("blocked")
                        is True
                    )
                    luxtronik_ww_budget_loss_effective_block = bool(
                        luxtronik_ww_budget_loss_guard_state.get(
                            "effective_block"
                        )
                        is True
                    )
                    if luxtronik_ww_budget_loss_effective_block:
                        heatpump_positive_output_blocked_this_cycle = True
                        if (
                            "luxtronik_ww_start_budget_expired"
                            not in heatpump_positive_output_block_reasons
                        ):
                            heatpump_positive_output_block_reasons.append(
                                "luxtronik_ww_start_budget_expired"
                            )
                        automatic_heat_start_allowed = False
                        central_heatpump_command_cap_w = 0
                        central_heatpump_start_budget_gate[
                            "luxtronik_ww_start_budget_expired"
                        ] = True

                    # Nach der aktortypisierten Startlease endet nur die volle
                    # ungenutzte Reserve (SG-Ready 150 s, Modbus direkt 25 s).
                    # Das positive Signal bleibt bis zum Ende des Bedarfs und
                    # mindestens 600 s bestehen. Ein später Verdichterstart
                    # wird über seine frische Istleistung zentral abgerechnet.
                    heatpump_signal_typed_protection_stop = bool(
                        heat_policy_decision is not None
                        and heat_policy_decision.owner
                        in ("hardware_protection", "source_protection")
                    )
                    heatpump_signal_independent_safety_stop = bool(
                        (
                            e3dc_valid
                            and _safe_float(soc, 0.0) > 0.0
                            and _safe_float(soc, 0.0)
                            < max(
                                5.0,
                                _safe_float(MIN_SOC, 80.0) - 5.0,
                            )
                        )
                        or _safe_float(grid, 0.0) > 2500.0
                        or heatpump_signal_typed_protection_stop
                    )
                    heatpump_positive_actuator_permission = (
                        bool(
                            heatpump_boost_permission_active
                            and heatpump_positive_signal_started_ts > 0.0
                            and heatpump_positive_actuator_permission_active(
                                heatpump_positive_signal_window,
                                heatpump_budget_demand_class,
                                safety_stop=(
                                    heatpump_signal_independent_safety_stop
                                ),
                            )
                        )
                    )
                    if heatpump_signal_independent_safety_stop:
                        heatpump_positive_output_blocked_this_cycle = True
                        heatpump_positive_output_block_reasons.append(
                            "independent_safety_or_manufacturer_stop"
                        )
                        # Ein im Safety-Zyklus gebildetes Budget darf nach dem
                        # OFF weder einen zweiten Positivbefehl noch iDM-Reg-74
                        # wieder öffnen. Nach Freigabe beginnt der reguläre
                        # Demand->Folgebudget-Handshake neu.
                        automatic_heat_start_allowed = False
                        central_heatpump_command_cap_w = 0
                        heatpump_budget_demand_active_class = "none"
                        heatpump_budget_demand_first_seen_ts = 0.0
                        central_heatpump_start_budget_gate[
                            "independent_safety_blocked"
                        ] = True
                    heatpump_signal_manufacturer_cycle_hold = bool(
                        heat_policy_protected_hold
                        and heat_policy_decision is not None
                        and heat_policy_decision.owner
                        in ("ww_cycle", "defrost", "legionella")
                    )
                    heatpump_signal_demand_ended = bool(
                        heatpump_budget_demand_class == "none"
                        and not heatpump_signal_manufacturer_cycle_hold
                    )
                    if (
                        heatpump_positive_signal_window.get("active") is True
                        and (
                            heatpump_signal_independent_safety_stop
                            or (
                                heatpump_signal_demand_ended
                                and heatpump_positive_signal_window.get(
                                    "normal_release_allowed"
                                )
                                is True
                            )
                        )
                        and wp_write_allowed
                        and wp
                    ):
                        heatpump_positive_output_blocked_this_cycle = True
                        heatpump_positive_output_block_reasons.append(
                            "independent_safety_stop"
                            if heatpump_signal_independent_safety_stop
                            else "typed_demand_ended"
                        )
                        automatic_heat_start_allowed = False
                        central_heatpump_command_cap_w = 0
                        if wp.set_boost(0, None, 0, CONF_WWW):
                            logger.info(
                                "Beende positive Wärmefreigabe: %s.",
                                "unabhängige Safety-/Herstellerschranke"
                                if heatpump_signal_independent_safety_stop
                                else "fachliche Nachfrage im aktuellen Zyklus beendet",
                            )
                            last_wp_command_time = time.time()
                            wp_last_pv_boost_stop_ts = time.time()
                            heatpump_positive_signal_started_ts = 0.0
                            heatpump_positive_signal_demand_class = "none"
                            heatpump_positive_signal_restored_unconfirmed = False
                            heatpump_positive_signal_start_reservation_allowed = (
                                False
                            )
                            heatpump_positive_signal_hold_guard = {}
                            heatpump_positive_signal_window = (
                                build_heatpump_positive_signal_window(
                                    0.0,
                                    compressor_running=wp_compressor_running_now,
                                    start_reservation_max_s=heatpump_start_reservation_max_s,
                                    now_ts=time.time(),
                                )
                            )
                            boost_active = False
                            price_boost_active = False
                            predump_heatpump_started_ts = 0.0
                            predump_heatpump_hold_until = 0.0
                            deficit_start_time = None

                    if shelly_startup_sync_pending and isinstance(wp, ShellyHeatpump):
                        if restart_revalidation.get("actuator_state_valid"):
                            # Der frische Readback ist nur Aktor-Evidence. Es wird hier
                            # weder ein alter Sollzustand wiederholt noch ein sicherer
                            # Gleichzustands-Write erzwungen. Erst die reguläre Policy
                            # darf in einem späteren Schritt eine Schaltkante erzeugen.
                            shelly_startup_sync_pending = False
                            logger.info(
                                "Shelly Recreate-Readback abgeschlossen: SG=%s Pause=%s Quelle=%s",
                                getattr(wp, "last_live_sg_state", None),
                                getattr(wp, "last_live_pause_state", None),
                                shelly_startup_sync_source,
                            )
                        elif (time.time() - last_shelly_startup_sync_log_time) > 60:
                            logger.warning(
                                "Shelly Recreate-Readback unvollständig; konfigurierte Kontakte bleiben schreibgesperrt."
                            )
                            last_shelly_startup_sync_log_time = time.time()

                    if price_action == "PAUSE" or price_action == "PAUSE_HIGH_PRICE":
                        if not pre_pause_active:
                            if (
                                wp_write_allowed
                                and automatic_heat_actuation_allowed
                                and heatpump_positive_signal_window.get(
                                    "minimum_signal_hold_active"
                                )
                                is not True
                            ):
                                reason = "Preis-Pause (Vorlauf)" if price_action == "PAUSE" else f"Hochpreis-Pause (> {PRICE_PAUSE_LIMIT}ct)"
                                logger.info(f"Start {reason}.")
                                heatpump_positive_output_blocked_this_cycle = True
                                heatpump_positive_output_block_reasons.append(
                                    "price_pause"
                                )
                                automatic_heat_start_allowed = False
                                central_heatpump_command_cap_w = 0
                                if wp.set_boost(1, PAUSE_SETPOINT_C, 0, CONF_WWW):
                                    last_wp_command_time = time.time()
                                    heatpump_positive_signal_started_ts = 0.0
                                    heatpump_positive_signal_demand_class = "none"
                                    heatpump_positive_signal_restored_unconfirmed = False
                                    heatpump_positive_signal_start_reservation_allowed = False
                                    heatpump_positive_signal_hold_guard = {}
                                    pre_pause_active = True; price_boost_active = False; boost_active = True

                    elif (
                        price_heatpump_start_requested
                        and wallbox_phase_transition_active
                        and not boost_active
                    ):
                        if (time.time() - last_wp_takt_log_time) > 300:
                            logger.info(
                                "WP-Start wartet auf abgeschlossenen Wallbox-Phasenwechsel "
                                "(Reservierung %dW).",
                                wallbox_phase_transition_reserved_w,
                            )
                            last_wp_takt_log_time = time.time()

                    elif price_heatpump_start_requested and price_heatpump_takt_start_blocked:
                        if (time.time() - last_wp_takt_log_time) > 300:
                            logger.info(
                                f"WP-Taktschutz: Start Preis-/Markt-Boost noch {price_heatpump_start_block_remaining_s/60:.1f} Min gesperrt "
                                f"(Wiedereinschaltsperre {WP_RESTART_BLOCK_MIN:.0f} Min)."
                            )
                            last_wp_takt_log_time = time.time()

                    elif price_heatpump_start_requested:
                        # NT-Fenster oder dynamischer Boost
                        if not price_boost_active:
                            if wp_write_allowed and automatic_heat_start_allowed:
                                if predump_heatpump_active:
                                    msg = "Start Pre-Dump-Verbraucherfreigabe (Waermepumpe)."
                                else:
                                    msg = f"Start Preis-Boost ({current_price} ct)" if price_action == "BOOST" else f"Start Nachtstrom-Boost (NT: {current_price} ct)"
                                logger.info(msg)

                                # Web-Push für extrem billigen Strom
                                if price_action == "BOOST" and current_price <= 0:
                                    if not last_push_awattar_sent:
                                        actions = json.dumps([
                                            {"action": "wb_direct_max", "title": "Auto voll Laden"},
                                        ])
                                        send_webpush('push_notify_warnings', '🤑 Börsenstrom negativ!', f"Der aktuelle Strompreis liegt bei {current_price} ct/kWh. Booster wurden gestartet. Auto auch laden?", url="/mobile.php?seite=charging", actions=actions)
                                        last_push_awattar_sent = True
                                elif current_price > 0:
                                    last_push_awattar_sent = False

                                if at_mittel > HEIZGRENZE_TEMP: success_w = wp.set_boost(0, None, 1, CONF_WWS, wp_data=wp_data)
                                else: success_w = wp.set_boost(1, CONF_HZ, 1, CONF_WWW, wp_data=wp_data)
                                if success_w:
                                    heatpump_positive_signal_retry_not_before_ts = 0.0
                                    heatpump_boost_permission_active = True
                                    last_wp_command_time = time.time()
                                    if (
                                        heatpump_positive_signal_restored_unconfirmed
                                        or heatpump_positive_signal_started_ts <= 0.0
                                    ):
                                        heatpump_positive_signal_started_ts = time.time()
                                        heatpump_positive_signal_demand_class = (
                                            heatpump_budget_demand_class
                                        )
                                        heatpump_positive_signal_hold_guard = (
                                            control_time.begin_guard(
                                                HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S,
                                                control_time.sample(),
                                                minimum_s=(
                                                    HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S
                                                ),
                                                epoch_mode=(
                                                    control_time.EPOCH_MODE_SAME_BOOT_MONOTONIC
                                                ),
                                            )
                                        )
                                        heatpump_positive_signal_start_reservation_allowed = (
                                            True
                                        )
                                    if heatpump_positive_signal_restored_unconfirmed:
                                        restart_revalidation[
                                            "positive_signal_restore_status"
                                        ] = "new_positive_command_confirmed"
                                    heatpump_positive_signal_restored_unconfirmed = (
                                        False
                                    )
                                    heatpump_positive_signal_window = (
                                        build_heatpump_positive_signal_window(
                                            heatpump_positive_signal_started_ts,
                                            compressor_running=(
                                                wp_compressor_running_now
                                            ),
                                            signal_hold_guard=(
                                                heatpump_positive_signal_hold_guard
                                            ),
                                            clock_sample=control_time.sample(),
                                            start_reservation_allowed=(
                                                heatpump_positive_signal_start_reservation_allowed
                                            ),
                                            start_reservation_max_s=heatpump_start_reservation_max_s,
                                            now_ts=time.time(),
                                        )
                                    )
                                    heatpump_positive_signal_hold_guard = copy.deepcopy(
                                        heatpump_positive_signal_window.get(
                                            "hold_guard"
                                        )
                                        or {}
                                    )
                                    # Das bestätigte SG-/Boost-Signal ist noch
                                    # kein Verdichterstart und eröffnet daher
                                    # keine Mindestlaufzeit.
                                    wp_last_pv_boost_start_ts = 0.0
                                    if predump_heatpump_active:
                                        cycle_actions.append({
                                            "action": "boost_start",
                                            "owner": "predump_heatpump",
                                            "confirmed": True,
                                            "signal_confirmed": True,
                                            "compressor_confirmed": False,
                                            "min_runtime_s": int(PREDUMP_HEATPUMP_MIN_RUNTIME_MIN * 60),
                                            "budget_w": int(
                                                central_heatpump_command_cap_w
                                            ),
                                        })
                                    else:
                                        boost_owner = (
                                            "market_plan_heatpump"
                                            if market_heatpump_active
                                            else ("price_plan_heatpump" if legacy_price_heatpump_active else "price_heatpump")
                                        )
                                        cycle_actions.append({
                                            "action": "boost_start",
                                            "owner": boost_owner,
                                            "confirmed": True,
                                            "signal_confirmed": True,
                                            "compressor_confirmed": False,
                                            "price_ct": current_price,
                                            "market_plan_action": market_heatpump_release.get("action"),
                                        })
                                    price_boost_active = True; pre_pause_active = False; boost_active = True
                                else:
                                    heatpump_positive_signal_retry_not_before_ts = max(
                                        heatpump_positive_signal_retry_not_before_ts,
                                        time.time() + 60.0,
                                    )
                        if price_action == "BOOST" and not predump_heatpump_active:
                             daily_boost_counter += 0.5 # Nur bei dynamischem Boost das tägliche Limit zählen

                    elif (
                        (price_boost_active or pre_pause_active)
                        and price_action == "NONE"
                        and heatpump_budget_demand_class == "none"
                        and not heatpump_signal_manufacturer_cycle_hold
                    ):
                        emergency_stop = (
                            e3dc_valid
                            and soc > 0
                            and soc < max(5, MIN_SOC - 5)
                        ) or grid > 2500 or heatpump_signal_typed_protection_stop
                        if price_boost_active and not predump_heatpump_active:
                            price_heatpump_stop_block_remaining_s = heatpump_takt_stop_block(
                                WP_TAKT_PROTECT,
                                wp_last_pv_boost_start_ts,
                                WP_MIN_RUNTIME_MIN,
                                emergency_stop=emergency_stop,
                            )
                            if (
                                not emergency_stop
                                and heatpump_positive_signal_window.get("active")
                                is True
                            ):
                                price_heatpump_stop_block_remaining_s = max(
                                    price_heatpump_stop_block_remaining_s,
                                    max(
                                        0.0,
                                        HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S
                                        - _safe_float(
                                            heatpump_positive_signal_window.get(
                                                "elapsed_s"
                                            ),
                                            0.0,
                                        ),
                                    ),
                                )
                            price_heatpump_takt_stop_held = price_heatpump_stop_block_remaining_s > 0.0
                        if price_heatpump_takt_stop_held:
                            if (time.time() - last_wp_takt_log_time) > 300:
                                logger.info(
                                    f"WP-Taktschutz: Ende Preis-/Markt-Boost noch {price_heatpump_stop_block_remaining_s/60:.1f} Min verzögert "
                                    f"(Mindestlaufzeit {WP_MIN_RUNTIME_MIN:.0f} Min)."
                                )
                                last_wp_takt_log_time = time.time()
                        elif wp_write_allowed:
                            heatpump_positive_output_blocked_this_cycle = True
                            heatpump_positive_output_block_reasons.append(
                                "price_or_predump_stop"
                            )
                            automatic_heat_start_allowed = False
                            central_heatpump_command_cap_w = 0
                            logger.info("Ende Preis-Steuerung.")
                            if wp.set_boost(0, None, 0, CONF_WWW):
                                last_wp_command_time = time.time()
                                wp_last_pv_boost_stop_ts = time.time()
                                heatpump_positive_signal_started_ts = 0.0
                                heatpump_positive_signal_demand_class = "none"
                                heatpump_positive_signal_restored_unconfirmed = False
                                heatpump_positive_signal_start_reservation_allowed = False
                                heatpump_positive_signal_hold_guard = {}
                                if predump_heatpump_started_ts:
                                    cycle_actions.append({
                                        "action": "boost_stop",
                                        "owner": "predump_heatpump",
                                        "confirmed": True,
                                        "reason": (
                                            "temperatur_erreicht" if predump_heatpump_targets_reached
                                            else ("wq_schutz" if predump_heatpump_protect_block else "haltezeit_abgelaufen")
                                        ),
                                    })
                                else:
                                    cycle_actions.append({
                                        "action": "boost_stop",
                                        "owner": "price_heatpump",
                                        "confirmed": True,
                                    })
                                predump_heatpump_started_ts = 0.0
                                predump_heatpump_hold_until = 0.0
                                price_boost_active = False; pre_pause_active = False; boost_active = False

                    # LAUFENDE ÜBERWACHUNG (PV-Boost)
                    if (
                        boost_active
                        and heatpump_positive_signal_demand_class == "pv_surplus"
                        and not price_boost_active
                        and not pre_pause_active
                        and not pv_pause_active
                    ):
                        # PV-Boost aktiv: Wir haben den Speicherstart bereits passiert
                        last_pv_boost_time = time.time()

                        # Dynamische Parameteranpassung Sommer/Winter während laufendem Boost
                        if (
                            wp_write_allowed
                            and wp
                            and heatpump_positive_signal_window.get("active")
                            is True
                            and not heatpump_positive_output_blocked_this_cycle
                            and (time.time() - last_wp_command_time) > 25
                        ):
                            if at_mittel > HEIZGRENZE_TEMP: wp.set_boost(0, None, 1, CONF_WWS, cooling_boost_mode, CONF_KHL, wp_data=wp_data)
                            else: wp.set_boost(1, CONF_HZ, 1, CONF_WWW, wp_data=wp_data)
                            last_wp_command_time = time.time()

                        # DEFIZIT-Check (Strenge Version):
                        # Wir stoppen, wenn wir massiv importieren (grid > 100) UND
                        # entweder der Akku nicht mehr massiv lädt (bat < 500)
                        # ODER das Gehirn sagt, dass kein Budget mehr da ist.
                        transition_protects_running_heatpump = bool(
                            (
                                wallbox_phase_transition_active
                                and (boost_active or wp_compressor_running_now)
                            )
                            or heat_policy_protected_hold
                        )
                        independent_safety_stop = bool(
                            (e3dc_valid and soc > 0 and soc < max(5, MIN_SOC - 5))
                            or grid > 2500
                            or heatpump_signal_typed_protection_stop
                        )
                        running_heatpump_underfunded = (
                            running_heatpump_budget_underfunded(
                                running_heatpump_accounting_valid,
                                central_heatpump_effective_budget_w,
                                running_heatpump_accounting_w,
                                running_heatpump_budget_tolerance_w,
                            )
                        )
                        pending_positive_signal = bool(
                            heatpump_positive_signal_window.get("active")
                            is True
                            and not wp_compressor_running_now
                        )
                        pending_signal_demand_active = bool(
                            pending_positive_signal
                            and heatpump_budget_demand_class != "none"
                        )
                        is_deficit = bool(independent_safety_stop)
                        is_strict_deficit = bool(
                            not pending_signal_demand_active
                            and (grid > 200)
                            and (
                                running_heatpump_underfunded
                                if running_heatpump_accounting_valid
                                else central_heatpump_effective_budget_w < 500
                            )
                            and (
                                not transition_protects_running_heatpump
                                or independent_safety_stop
                            )
                        )
                        is_budget_deficit = bool(
                            not pending_signal_demand_active
                            and not transition_protects_running_heatpump
                            and (
                                running_heatpump_underfunded
                                if running_heatpump_accounting_valid
                                else heatpump_budget_deficit(
                                    storage_manager_owns_energy,
                                    central_heatpump_effective_budget_w,
                                    GRID_START_LIMIT,
                                )
                            )
                        )
                        if (
                            heatpump_positive_actuator_permission
                            and not independent_safety_stop
                        ):
                            # Der zyklische Wattrahmen ist nur Accounting. Eine
                            # bereits bestätigte boolsche Aktorfreigabe bleibt
                            # bis zum fachlichen Ziel-/Safety-Ende stabil.
                            is_deficit = False
                            is_strict_deficit = False
                            is_budget_deficit = False
                            deficit_start_time = None
                        elif (
                            pending_signal_demand_active
                            and not independent_safety_stop
                        ):
                            # Nach der aktortypisierten Startlease endet nur die
                            # Vollreserve. Das noch
                            # nicht angenommene SG-/Boost-Signal bleibt trotz
                            # 0-W-Command-Cap bestehen, solange der Bedarf gilt.
                            is_deficit = False
                            deficit_start_time = None
                        elif transition_protects_running_heatpump and not independent_safety_stop:
                            is_deficit = False
                            deficit_start_time = None

                        if is_deficit or is_strict_deficit or is_budget_deficit:
                            if deficit_start_time is None:
                                deficit_start_time = now
                                reason_str = "Gehirn-Budget" if is_budget_deficit else "Standard"
                                logger.info(
                                    "Defizit erkannt (%s | Netz: %.0fW, "
                                    "Heat-Cap: %sW, Quelle: %s). Timer start.",
                                    reason_str,
                                    grid,
                                    central_heatpump_effective_budget_w,
                                    central_heatpump_effective_budget_source,
                                )
                            elif (now - deficit_start_time).total_seconds() > (STOP_DELAY_MINUTES * 60):
                                min_runtime_left_s = 0.0
                                if WP_TAKT_PROTECT and wp_last_pv_boost_start_ts and WP_MIN_RUNTIME_MIN > 0:
                                    min_runtime_left_s = (WP_MIN_RUNTIME_MIN * 60) - (time.time() - wp_last_pv_boost_start_ts)
                                emergency_stop = (
                                    (e3dc_valid and soc > 0 and soc < max(5, MIN_SOC - 5))
                                    or grid > 2500
                                    or heatpump_signal_typed_protection_stop
                                )
                                if (
                                    not emergency_stop
                                    and heatpump_positive_signal_window.get("active")
                                    is True
                                ):
                                    min_runtime_left_s = max(
                                        min_runtime_left_s,
                                        HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S
                                        - _safe_float(
                                            heatpump_positive_signal_window.get(
                                                "elapsed_s"
                                            ),
                                            0.0,
                                        ),
                                    )
                                if min_runtime_left_s > 0 and not emergency_stop:
                                    if (time.time() - last_wp_takt_log_time) > 300:
                                        logger.info(f"WP-Taktschutz: Stop PV-Boost noch {min_runtime_left_s/60:.1f} Min verzögert (Mindestlaufzeit {WP_MIN_RUNTIME_MIN:.0f} Min).")
                                        last_wp_takt_log_time = time.time()
                                elif wp_write_allowed:
                                    heatpump_positive_output_blocked_this_cycle = True
                                    heatpump_positive_output_block_reasons.append(
                                        "pv_budget_deficit_stop"
                                    )
                                    automatic_heat_start_allowed = False
                                    central_heatpump_command_cap_w = 0
                                    logger.info(f"Stop PV-Boost (Defizit | Netz: {grid:.0f}W, Heat-Cap: {central_heatpump_command_cap_w}W).")
                                    if wp.set_boost(0, None, 0, CONF_WWW):
                                        last_wp_command_time = time.time()
                                        wp_last_pv_boost_stop_ts = time.time()
                                        heatpump_positive_signal_started_ts = 0.0
                                        heatpump_positive_signal_demand_class = "none"
                                        heatpump_positive_signal_restored_unconfirmed = False
                                        heatpump_positive_signal_start_reservation_allowed = False
                                        heatpump_positive_signal_hold_guard = {}
                                        boost_active = False; deficit_start_time = None
                        else:
                            deficit_start_time = None

                    # PV BOOST START-SEQUENZ
                    if heatpump_pause_blocks_boost or wallbox_phase_transition_active:
                        pv_boost_pending_start = None
                    if (
                        not boost_active and not heatpump_pause_blocks_boost and not car_blocks_boost_applied
                        and not wallbox_phase_transition_active
                    ):
                        # KI 3.0: Wir verzichten auf die eigenmächtige Grid-Prüfung (grid <= GRID_START_LIMIT)
                        # und vertrauen voll auf den Storage Manager (Gehirn).
                        # Der GRID_START_LIMIT dient hier als Schwellwert für den vom Gehirn
                        # ausgewiesenen 'echten' Überschuss nach Batterieladung.
                        restart_block_left_s = 0.0
                        if WP_TAKT_PROTECT and wp_last_pv_boost_stop_ts and WP_RESTART_BLOCK_MIN > 0:
                            restart_block_left_s = (WP_RESTART_BLOCK_MIN * 60) - (time.time() - wp_last_pv_boost_stop_ts)
                        if restart_block_left_s > 0:
                            pv_boost_pending_start = None
                            if (time.time() - last_wp_takt_log_time) > 300:
                                logger.info(f"WP-Taktschutz: Start PV-Boost noch {restart_block_left_s/60:.1f} Min gesperrt (Wiedereinschaltsperre {WP_RESTART_BLOCK_MIN:.0f} Min).")
                                last_wp_takt_log_time = time.time()
                        else:
                            policy_allows_pv_start = bool(
                                heat_policy_runtime_enabled
                                and automatic_heat_start_allowed
                                and heat_policy_decision is not None
                                and heat_policy_decision.target_state == heat_policy.TARGET_PV_SURPLUS
                            )
                            legacy_allows_pv_start = bool(
                                (not heat_policy_runtime_enabled)
                                and automatic_heat_start_allowed
                                and heatpump_budget_allows_start(
                                    storage_manager_owns_energy,
                                    heat_policy_budget_w,
                                    GRID_START_LIMIT,
                                    soc,
                                    MIN_SOC,
                                )
                            )
                        if restart_block_left_s <= 0 and (policy_allows_pv_start or legacy_allows_pv_start) and wp:
                            if pv_boost_pending_start is None:
                                pv_boost_pending_start = now
                            elif (now - pv_boost_pending_start).total_seconds() > PV_BOOST_DELAY:
                                if wp_write_allowed:
                                    if at_mittel > HEIZGRENZE_TEMP:
                                        if wp_type == 1 and not cooling_boost_allowed and (time.time() - last_idm_cooling_gate_log_time) > 300:
                                            logger.info(f"iDM Kühl-Boost bleibt aus: Außentemperatur {_safe_float(at_mittel, 0.0):.1f}°C < Kühlgrenze {_safe_float(IDM_COOLING_BOOST_MIN_AT, 23.0):.1f}°C.")
                                            last_idm_cooling_gate_log_time = time.time()
                                        boost_args = (0, None, 1, CONF_WWS, cooling_boost_mode, CONF_KHL, wp_data)
                                    else:
                                        boost_args = (1, CONF_HZ, 1, CONF_WWW, 0, None, wp_data)
                                    pv_boost_last_outcome = attempt_heatpump_pv_boost_start(
                                        wp,
                                        boost_args,
                                        now_ts=time.time(),
                                        retry_not_before_ts=pv_boost_retry_not_before_ts,
                                        retry_backoff_s=60.0,
                                        runtime_enabled=heat_policy_runtime_enabled,
                                        validated_intent=heat_intent_export,
                                        runtime_validation=(
                                            heat_intent_runtime_validation
                                        ),
                                        policy_owner=(
                                            heat_policy_decision.owner
                                            if heat_policy_decision is not None
                                            else None
                                        ),
                                        owner_contract=legacy_heat_automation_owner,
                                    )
                                    pv_boost_retry_not_before_ts = _safe_float(
                                        pv_boost_last_outcome.get("retry_not_before_ts"),
                                        pv_boost_retry_not_before_ts,
                                    )
                                    if pv_boost_last_outcome.get("confirmed"):
                                        heatpump_positive_signal_retry_not_before_ts = 0.0
                                        heatpump_boost_permission_active = True
                                        logger.info(
                                            "Start PV-Boost bestätigt (Gehirn-Budget: %sW >= %sW; "
                                            "command_sent=%s, readback_confirmed=%s).",
                                            central_heatpump_command_cap_w,
                                            abs(GRID_START_LIMIT),
                                            pv_boost_last_outcome.get("command_sent"),
                                            pv_boost_last_outcome.get("readback_confirmed"),
                                        )
                                        last_wp_command_time = time.time()
                                        if (
                                            heatpump_positive_signal_restored_unconfirmed
                                            or heatpump_positive_signal_started_ts <= 0.0
                                        ):
                                            heatpump_positive_signal_started_ts = time.time()
                                            heatpump_positive_signal_demand_class = (
                                                heatpump_budget_demand_class
                                            )
                                            heatpump_positive_signal_hold_guard = (
                                                control_time.begin_guard(
                                                    HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S,
                                                    control_time.sample(),
                                                    minimum_s=(
                                                        HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S
                                                    ),
                                                    epoch_mode=(
                                                        control_time.EPOCH_MODE_SAME_BOOT_MONOTONIC
                                                    ),
                                                )
                                            )
                                            heatpump_positive_signal_start_reservation_allowed = (
                                                True
                                            )
                                        if heatpump_positive_signal_restored_unconfirmed:
                                            restart_revalidation[
                                                "positive_signal_restore_status"
                                            ] = "new_positive_command_confirmed"
                                        heatpump_positive_signal_restored_unconfirmed = (
                                            False
                                        )
                                        heatpump_positive_signal_window = (
                                            build_heatpump_positive_signal_window(
                                                heatpump_positive_signal_started_ts,
                                                compressor_running=(
                                                    wp_compressor_running_now
                                                ),
                                                signal_hold_guard=(
                                                    heatpump_positive_signal_hold_guard
                                                ),
                                                clock_sample=control_time.sample(),
                                                start_reservation_allowed=(
                                                    heatpump_positive_signal_start_reservation_allowed
                                                ),
                                                start_reservation_max_s=heatpump_start_reservation_max_s,
                                                now_ts=time.time(),
                                            )
                                        )
                                        heatpump_positive_signal_hold_guard = copy.deepcopy(
                                            heatpump_positive_signal_window.get(
                                                "hold_guard"
                                            )
                                            or {}
                                        )
                                        wp_last_pv_boost_start_ts = 0.0
                                        boost_active = True; deficit_start_time = None
                                        pv_boost_pending_start = None
                                    elif pv_boost_last_outcome.get("status") != "backoff":
                                        logger.warning(
                                            "Start PV-Boost nicht bestätigt (status=%s, command_sent=%s, "
                                            "readback_confirmed=%s, reason=%s); neuer Startversuch frühestens in 60s.",
                                            pv_boost_last_outcome.get("status"),
                                            pv_boost_last_outcome.get("command_sent"),
                                            pv_boost_last_outcome.get("readback_confirmed"),
                                            pv_boost_last_outcome.get("reason"),
                                        )
                        elif restart_block_left_s <= 0:
                            pv_boost_pending_start = None

                    # Native iDM Überschusssteuerung
                    if wp_write_allowed and wp:
                        wp.update_surplus(
                            grid,
                            free_for_limbs_w=(
                                0
                                if heatpump_positive_output_blocked_this_cycle
                                else central_heatpump_command_cap_w
                            ),
                        )

                    # Modbus Keep-Alive + Dual-Channel Health-Check
                    # Vergleich Modbus-Außentemperatur (Register 10108) mit WebSocket-Außentemperatur.
                    if wp_write_allowed:
                        force_socket_open = heatpump_keepalive_force_open(
                            wp_type,
                            boost_active,
                            pv_pause_active,
                            pre_pause_active,
                            price_boost_active,
                        )
                        modbus_at = wp.keep_alive(force_open=force_socket_open)

                        # Health-Check: Nur wenn beide Quellen frische Werte haben
                        ws_at = wp_data.get('Aussentemp') if wp_data else None
                        if modbus_at is not None and ws_at is not None:
                            diff = abs(modbus_at - float(ws_at))
                            if diff > 3.0:
                                logger.warning(f"Modbus Health-Check: Aussentemp Divergenz! Modbus={modbus_at:.1f}C vs WebSocket={ws_at:.1f}C (Diff={diff:.1f}K) -> Reconnect")
                                wp.close()  # Erzwingt Reconnect beim naechsten Zyklus

                    # ---------------------------------------------------------
                    # WARM WATER SOFTWARE TIMER ENFORCEMENT & WW SOFORT
                    # ---------------------------------------------------------
                    manual_ww_flag = "/var/www/html/ramdisk/manual_ww_boost.flag"
                    if os.path.exists(manual_ww_flag):
                        try:
                            if (time.time() - os.path.getmtime(manual_ww_flag)) < (WW_SOFORT_DURATION * 60):
                                manual_ww_active = True
                            else: os.remove(manual_ww_flag)
                        except: pass

                    if wp_write_allowed and wp:
                        current_hour_decimal = now.hour + (now.minute / 60.0)

                        target_ww_mode = None
                        target_ww_temp = None
                        target_circ = None

                        boost_ww_temp = CONF_WWS if at_mittel > HEIZGRENZE_TEMP else CONF_WWW
                        force_pause = (pre_pause_active or pv_pause_active)
                        ww_positive_output_hard_blocked = bool(
                            heatpump_positive_output_blocked_this_cycle
                            if wp_type != 0
                            else luxtronik_timer_hard_blocked(
                                heatpump_positive_output_blocked_this_cycle,
                                heatpump_positive_output_block_reasons,
                            )
                        )
                        positive_signal_active = bool(
                            heatpump_positive_signal_window.get("active")
                            is True
                        )
                        positive_signal_min_hold = bool(
                            positive_signal_active
                            and heatpump_positive_signal_window.get(
                                "minimum_signal_hold_active"
                            )
                            is True
                        )
                        luxtronik_boost_permission_active = (
                            not luxtronik_ww_budget_loss_effective_block
                            and luxtronik_direct_setpoint_permission(
                                wp_type,
                                central_heatpump_boost_permission_active,
                                bool(
                                    heatpump_positive_actuator_permission
                                    and heatpump_boost_permission_active
                                    and heatpump_positive_signal_started_ts
                                    > 0.0
                                ),
                                heatpump_budget_demand_class,
                                force_pause=force_pause,
                                hard_blocked=(
                                    ww_positive_output_hard_blocked
                                ),
                            )
                        )
                        luxtronik_ww_target = luxtronik_ww_budget_target(
                            timer_enabled=bool(WW_TIMER_ENABLE),
                            timer_target_c=ww_timer_target_c,
                            boost_requested=bool(
                                not force_pause
                                and not luxtronik_ww_budget_loss_effective_block
                                and (
                                    boost_active
                                    or luxtronik_boost_permission_active
                                    or (
                                        WW_TIMER_ENABLE
                                        and central_heatpump_effective_budget_w > 0
                                    )
                                )
                            ),
                            authorized_heatpump_budget_w=(
                                central_heatpump_effective_budget_w
                            ),
                            boost_permission_active=(
                                luxtronik_boost_permission_active
                            ),
                            boost_target_c=boost_ww_temp,
                            fallback_target_c=CONF_WWW,
                            hard_blocked=ww_positive_output_hard_blocked,
                        )
                        luxtronik_boost_target_active = bool(
                            wp_type == 0
                            and luxtronik_ww_target.get("boost_target_active")
                        )
                        active_boost_type = bool(
                            not force_pause
                            and (
                                luxtronik_boost_target_active
                                if wp_type == 0
                                else boost_active
                            )
                        )
                        force_ww = bool(
                            active_boost_type
                            or (
                                manual_ww_active
                                and (
                                    automatic_heat_start_allowed
                                    or positive_signal_active
                                )
                            )
                            or (
                                positive_signal_min_hold
                                and (
                                    wp_type != 0
                                    or luxtronik_boost_permission_active
                                )
                                and heatpump_positive_signal_demand_class.startswith(
                                    "ww_"
                                )
                            )
                        )
                        source_recovery_owns_pause = bool(
                            pv_pause_active
                            and pv_pause_owner == "source_recovery_heatpump"
                        )

                        if ww_positive_output_hard_blocked:
                            target_ww_mode = 0
                            target_ww_temp = CONF_WWW
                        elif force_pause and source_recovery_owns_pause:
                            target_ww_mode = None
                            target_ww_temp = None
                        elif force_pause:
                            target_ww_mode = 0  # Externe SHI-Beeinflussung zurückgeben
                            target_ww_temp = CONF_WWW
                        elif force_ww:
                            if wp_type == 0 and luxtronik_boost_target_active:
                                target_ww_mode = luxtronik_ww_target.get("mode")
                                target_ww_temp = luxtronik_ww_target.get("target_c")
                            else:
                                target_ww_mode = 1
                                target_ww_temp = (
                                    heatpump_budget_demand_target_c
                                    if heatpump_budget_demand_target_c is not None
                                    else boost_ww_temp
                                )
                        elif (
                            wp_type == 0
                            and luxtronik_ww_budget_loss_effective_block
                        ):
                            # Nur den WW-Auftrag zurücknehmen. Ein gleichzeitig
                            # laufender Heizverdichter und dessen HZ-SHI bleiben
                            # unangetastet.
                            target_ww_mode = luxtronik_ww_target.get("mode")
                            target_ww_temp = luxtronik_ww_target.get("target_c")
                            if target_ww_mode is None:
                                target_ww_mode = 0
                                target_ww_temp = CONF_WWW
                        elif manual_ww_active:
                            target_ww_mode = 0
                            target_ww_temp = CONF_WWW
                        elif WW_TIMER_ENABLE:
                            # Der Luxtronik-Zeitplan ist ein direkter
                            # Komfort-Setpoint und keine SG-Ready-Startreserve.
                            # Er bleibt unter den harten Pause-/Safety-Gates,
                            # darf aber nicht von einem aktuellen
                            # Überschussbudget abhängen. Andere Aktorpfade
                            # behalten ihre budgetgebundene Startsemantik.
                            if wp_type == 0:
                                target_ww_mode = luxtronik_ww_target.get("mode")
                                target_ww_temp = luxtronik_ww_target.get("target_c")
                            elif heatpump_ww_timer_target_allowed(
                                wp_type,
                                automatic_heat_start_allowed,
                                positive_signal_active,
                            ):
                                target_ww_mode = 1
                                target_ww_temp = ww_timer_target_c
                            else:
                                target_ww_mode = 0
                                target_ww_temp = CONF_WWW

                        # Zirkulationstimer (laeuft IMMER, unabhaengig von force_ww/force_pause)
                        # WW_CIRC_BOOST kann target_circ auf 1 erzwingen, aber nicht loeschen.
                        if WW_TIMER_ENABLE and not force_pause:
                            if WW_CIRC_VON <= WW_CIRC_BIS:
                                in_circ = (WW_CIRC_VON <= current_hour_decimal < WW_CIRC_BIS)
                            else:
                                in_circ = (current_hour_decimal >= WW_CIRC_VON or current_hour_decimal < WW_CIRC_BIS)

                            if in_circ and (WW_CIRC_ON + WW_CIRC_OFF) > 0:
                                cycle_time_minutes = (now.hour * 60 + now.minute) % (WW_CIRC_ON + WW_CIRC_OFF)
                                target_circ = 1 if cycle_time_minutes < WW_CIRC_ON else 0
                            else:
                                target_circ = 0
                            # WW_CIRC_BOOST: PV-Boost erzwingt Zirkulation EIN (ueberschreibt 0 auf 1)
                            if force_ww and WW_CIRC_BOOST:
                                target_circ = 1

                        ww_cycle_abort_allowed = bool(
                            (e3dc_valid and _safe_float(soc, 0.0) > 0 and _safe_float(soc, 0.0) < max(5.0, _safe_float(MIN_SOC, 80.0) - 5.0))
                            or (_safe_float(grid, 0.0) > 2500.0 and _safe_float(soc, 0.0) <= _safe_float(MIN_SOC, 80.0))
                            or heatpump_ww_price_stop_allowed(
                                price_action,
                                wp_last_ww_cycle_start_ts,
                            )
                            or not AUTO_MODE
                            or not wp_write_allowed
                            or ww_positive_output_hard_blocked
                            or bool(
                                restart_revalidation.get("ww_hold_restored")
                                and not wp_status.get("valid")
                            )
                        )
                        ww_cycle_guard = heatpump_ww_cycle_min_runtime_guard(
                            target_ww_mode,
                            target_ww_temp,
                            wp_status,
                            wp_data,
                            started_ts=wp_last_ww_cycle_start_ts,
                            latched_target_c=wp_last_ww_cycle_target_c,
                            min_runtime_s=heat_policy.WW_CYCLE_MIN_RUNTIME_S,
                            abort_allowed=ww_cycle_abort_allowed,
                            physical_running=(
                                luxtronik_ww_runtime_contract.get("state")
                                == "ww_running"
                                if wp_type == 0
                                else None
                            ),
                        )
                        if ww_cycle_guard.get("hold_active") and (time.time() - last_ww_off_guard_log_time) > 300.0:
                            logger.info(
                                "WW-Zyklus gehalten: Mindestlaufzeit noch %.1f Min, Ziel %.1f°C.",
                                _safe_float(ww_cycle_guard.get("remaining_s"), 0.0) / 60.0,
                                _safe_float(ww_cycle_guard.get("target_c"), 0.0),
                            )
                            last_ww_off_guard_log_time = time.time()
                        target_ww_mode = ww_cycle_guard.get("target_ww_mode")
                        target_ww_temp = ww_cycle_guard.get("target_ww_temp")
                        if ww_positive_output_hard_blocked:
                            target_ww_mode = 0
                            target_ww_temp = CONF_WWW
                        wp_last_ww_cycle_start_ts = _safe_float(ww_cycle_guard.get("started_ts"), 0.0)
                        wp_last_ww_cycle_target_c = _safe_float(ww_cycle_guard.get("target_c"), 0.0)

                        # ── WW Setpoint Refresh-Logik ──────────────────────────────────────
                        # Die Luxtronik SHI kann externe Register zuruecksetzen. Deshalb ist
                        # der Live-Status die fuehrende Referenz: passt er bereits, wird kein
                        # identischer Timerwert erneut auf den Bus geschrieben. Nur ohne
                        # Live-Status bleibt ein gedehnter Blind-Heartbeat als Sicherheitsnetz.
                        WW_HEARTBEAT_SECS = 900  # Blind-Heartbeat nur ohne Live-Status
                        WW_COOLDOWN_SECS  = 15   # Cooldown nach Write (Luxtronik Verarbeitungszeit)
                        ww_boost_owner_recent = bool(
                            (active_boost_type or force_pause)
                            and (time.time() - float(last_wp_command_time or 0.0)) < max(WW_COOLDOWN_SECS, 30.0)
                        )

                        time_since_last_ww_cmd = time.time() - getattr(wp, 'last_ww_cmd_time', 0)
                        send_ww_mode, send_ww_temp, ww_update_reason = luxtronik_ww_command_request(
                            target_ww_mode,
                            target_ww_temp,
                            wp_status,
                            wp_data,
                            getattr(wp, 'last_ww_mode', None),
                            getattr(wp, 'last_ww_temp', None),
                            time_since_last_ww_cmd,
                            ww_boost_owner_recent,
                            WW_COOLDOWN_SECS,
                            WW_HEARTBEAT_SECS,
                        )

                        ww_positive_bookkeeping_active = bool(
                            heatpump_positive_signal_started_ts > 0.0
                            or boost_active
                            or price_boost_active
                        )
                        ww_budget_release_readback_confirmed = bool(
                            luxtronik_ww_budget_loss_effective_block
                            and ww_positive_bookkeeping_active
                            and send_ww_mode is None
                            and luxtronik_ww_target_readback_confirmed(
                                target_ww_mode,
                                target_ww_temp,
                                wp_status,
                            )
                        )
                        if ww_budget_release_readback_confirmed:
                            heatpump_positive_signal_started_ts = 0.0
                            heatpump_positive_signal_demand_class = "none"
                            heatpump_positive_signal_restored_unconfirmed = False
                            heatpump_positive_signal_start_reservation_allowed = False
                            heatpump_positive_signal_hold_guard = {}
                            heatpump_boost_permission_active = False
                            boost_active = False
                            price_boost_active = False
                            wp_last_pv_boost_stop_ts = time.time()
                            heatpump_budget_demand_active_class = "none"
                            heatpump_budget_demand_first_seen_ts = 0.0
                            deficit_start_time = None
                            pv_boost_pending_start = None
                            cycle_actions.append({
                                "action": "ww_boost_budget_release",
                                "owner": "luxtronik_ww",
                                "confirmed": True,
                                "confirmation_source": "fresh_shi_readback",
                                "mode": target_ww_mode,
                                "target_c": target_ww_temp,
                                "new_start_requires_followup_budget": True,
                            })

                        if ww_update_reason is not None and send_ww_mode == 0:
                            ww_below_target, ww_actual_c, ww_target_c = heatpump_ww_temperature_below_target(
                                wp_status,
                                wp_data,
                                target_ww_temp,
                                CONF_WWW,
                            )
                            ww_off_abort_allowed = bool(
                                (e3dc_valid and _safe_float(soc, 0.0) > 0 and _safe_float(soc, 0.0) < max(5.0, _safe_float(MIN_SOC, 80.0) - 5.0))
                                or (_safe_float(grid, 0.0) > 2500.0 and _safe_float(soc, 0.0) <= _safe_float(MIN_SOC, 80.0))
                                or heatpump_ww_price_stop_allowed(
                                    price_action,
                                    wp_last_ww_cycle_start_ts,
                                )
                                or not AUTO_MODE
                                or not wp_write_allowed
                                or ww_positive_output_hard_blocked
                            )
                            if (
                                (
                                    luxtronik_ww_runtime_contract.get("state")
                                    in ("ww_running", "unknown")
                                    if wp_type == 0
                                    else heatpump_ww_cycle_running(
                                        wp_status,
                                        wp_data,
                                        ww_requested=True,
                                    )
                                )
                                and ww_below_target
                                and not ww_off_abort_allowed
                            ):
                                if (time.time() - last_ww_off_guard_log_time) > 300.0:
                                    logger.warning(
                                        "WP-Schutz: Blockiere unplausibles WW-Off-Kommando "
                                        "(Verdichter läuft, Temp %s°C < Ziel %s°C, keine harte Abbruchfreigabe).",
                                        ww_actual_c,
                                        ww_target_c,
                                    )
                                    last_ww_off_guard_log_time = time.time()
                                send_ww_mode = None
                                ww_update_reason = None

                        if (
                            ww_positive_output_hard_blocked
                            and send_ww_mode == 1
                        ):
                            send_ww_mode = 0
                            send_ww_temp = CONF_WWW
                            ww_update_reason = "positive_output_cycle_blocked"

                        if ww_update_reason is not None and send_ww_mode is not None:
                            ww_positive_start_attempt = bool(
                                send_ww_mode == 1
                                and heatpump_positive_signal_started_ts <= 0.0
                            )
                            ww_positive_stop_attempt = bool(
                                (
                                    send_ww_mode == 0
                                    or luxtronik_ww_budget_loss_effective_block
                                )
                                and ww_positive_bookkeeping_active
                            )
                            if ww_positive_stop_attempt:
                                heatpump_positive_output_blocked_this_cycle = True
                                heatpump_positive_output_block_reasons.append(
                                    "ww_positive_output_stop"
                                )
                                automatic_heat_start_allowed = False
                                central_heatpump_command_cap_w = 0
                            if wp.write_ww_boost(send_ww_mode, send_ww_temp):
                                if ww_positive_start_attempt:
                                    heatpump_positive_signal_retry_not_before_ts = 0.0
                                wp.last_ww_mode = send_ww_mode
                                wp.last_ww_temp = send_ww_temp
                                wp.last_ww_cmd_time = time.time()
                                if ww_positive_stop_attempt:
                                    heatpump_positive_signal_started_ts = 0.0
                                    heatpump_positive_signal_demand_class = "none"
                                    heatpump_positive_signal_restored_unconfirmed = False
                                    heatpump_positive_signal_start_reservation_allowed = False
                                    heatpump_positive_signal_hold_guard = {}
                                    heatpump_boost_permission_active = False
                                    boost_active = False
                                    price_boost_active = False
                                    wp_last_pv_boost_stop_ts = time.time()
                                    heatpump_budget_demand_active_class = "none"
                                    heatpump_budget_demand_first_seen_ts = 0.0
                                    deficit_start_time = None
                                    pv_boost_pending_start = None
                                    cycle_actions.append({
                                        "action": "ww_boost_budget_release",
                                        "owner": "luxtronik_ww",
                                        "confirmed": True,
                                        "mode": send_ww_mode,
                                        "target_c": send_ww_temp,
                                        "new_start_requires_followup_budget": True,
                                    })
                                elif (
                                    send_ww_mode == 1
                                    and luxtronik_boost_target_active
                                ):
                                    heatpump_boost_permission_active = True
                                if (
                                    not ww_positive_stop_attempt
                                    and
                                    send_ww_mode == 1
                                    and (
                                        heatpump_positive_signal_restored_unconfirmed
                                        or heatpump_positive_signal_started_ts <= 0.0
                                    )
                                ):
                                    heatpump_positive_signal_started_ts = time.time()
                                    heatpump_positive_signal_demand_class = (
                                        heatpump_budget_demand_class
                                    )
                                    heatpump_positive_signal_hold_guard = (
                                        control_time.begin_guard(
                                            HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S,
                                            control_time.sample(),
                                            minimum_s=(
                                                HEATPUMP_POSITIVE_SIGNAL_MIN_HOLD_S
                                            ),
                                            epoch_mode=(
                                                control_time.EPOCH_MODE_SAME_BOOT_MONOTONIC
                                            ),
                                        )
                                    )
                                    heatpump_positive_signal_start_reservation_allowed = (
                                        True
                                    )
                                    if heatpump_positive_signal_restored_unconfirmed:
                                        restart_revalidation[
                                            "positive_signal_restore_status"
                                        ] = "new_positive_command_confirmed"
                                    heatpump_positive_signal_restored_unconfirmed = (
                                        False
                                    )
                                    wp_last_pv_boost_start_ts = 0.0
                                    heatpump_positive_signal_window = (
                                        build_heatpump_positive_signal_window(
                                            heatpump_positive_signal_started_ts,
                                            compressor_running=(
                                                wp_compressor_running_now
                                            ),
                                            signal_hold_guard=(
                                                heatpump_positive_signal_hold_guard
                                            ),
                                            clock_sample=control_time.sample(),
                                            start_reservation_allowed=(
                                                heatpump_positive_signal_start_reservation_allowed
                                            ),
                                            start_reservation_max_s=heatpump_start_reservation_max_s,
                                            now_ts=time.time(),
                                        )
                                    )
                                    heatpump_positive_signal_hold_guard = copy.deepcopy(
                                        heatpump_positive_signal_window.get(
                                            "hold_guard"
                                        )
                                        or {}
                                    )
                                elif (
                                    not ww_positive_stop_attempt
                                    and send_ww_mode == 0
                                ):
                                    heatpump_positive_signal_started_ts = 0.0
                                    heatpump_positive_signal_demand_class = "none"
                                    heatpump_positive_signal_restored_unconfirmed = False
                                    heatpump_positive_signal_start_reservation_allowed = False
                                    heatpump_positive_signal_hold_guard = {}
                                if ww_update_reason == "blind_heartbeat":
                                    logger.debug(f"WW Blind-Heartbeat: Mode={send_ww_mode}, Temp={send_ww_temp}")
                                else:
                                    logger.info(f"WW Timer/Boost Set: Mode={send_ww_mode}, Temp={send_ww_temp} ({ww_update_reason})")
                            else:
                                if ww_positive_start_attempt:
                                    heatpump_positive_signal_retry_not_before_ts = max(
                                        heatpump_positive_signal_retry_not_before_ts,
                                        time.time() + 60.0,
                                    )
                                logger.warning(
                                    "WW Timer/Boost konnte nicht geschrieben werden: Mode=%s, Temp=%s (%s).",
                                    send_ww_mode,
                                    send_ww_temp,
                                    ww_update_reason,
                                )

                        if target_circ is not None:
                            # Zirkulation: Statuswechsel sofort schreiben, plus 90s-Heartbeat
                            # (SHI-Register werden intern periodic zurueckgesetzt wie WW/HZ)
                            time_since_last_circ_cmd = time.time() - getattr(wp, 'last_circ_cmd_time', 0)
                            circ_mode_changed = getattr(wp, 'last_circ_mode', None) != target_circ
                            circ_heartbeat    = time_since_last_circ_cmd >= WW_HEARTBEAT_SECS

                            if circ_mode_changed or circ_heartbeat:
                                success_circ = wp.write_zirkulation(target_circ)
                                if success_circ:
                                    wp.last_circ_mode = target_circ
                                    wp.last_circ_cmd_time = time.time()
                                    if circ_heartbeat and not circ_mode_changed:
                                        logger.debug(f"Zirkulation Heartbeat: {target_circ}")
                                    else:
                                        logger.info(f"Zirkulation Set: {target_circ}")
                                else:
                                    # GANZ WICHTIG: mode auf target_circ setzen, auch bei Fehler!
                                    # Wenn es auf 'None' bleibt, versucht die nächste Iteration (2 Sek später)
                                    # wieder zu senden, weil "circ_mode_changed" dann True ist -> DEADLOCK / SPAMMING!
                                    wp.last_circ_mode = target_circ
                                    wp.last_circ_cmd_time = time.time()
                                    logger.error(f"Zirkulation Set {target_circ} via Modbus abgewiesen! (Sperre für Heartbeat-Zeit)")

                except Exception as req_err: logger.error(f"Fehler Logik: {req_err}")

            # 3. Daten schreiben
            manual_heatpump_active = bool(
                manual_boost_command.get("valid")
                and manual_boost_command.get("action") == "on"
            )
            manual_ww_boost_active_export = os.path.exists("/var/www/html/ramdisk/manual_ww_boost.flag")
            heatpump_boost_owner = "none"
            if predump_heatpump_active:
                heatpump_boost_owner = "predump_heatpump"
            elif price_boost_active:
                if market_heatpump_active:
                    heatpump_boost_owner = "market_plan_heatpump"
                elif legacy_price_heatpump_active:
                    heatpump_boost_owner = "price_plan_heatpump"
                else:
                    heatpump_boost_owner = "legacy_price_heatpump"
            elif pv_pause_active:
                heatpump_boost_owner = "source_recovery_heatpump" if pv_pause_owner == "source_recovery_heatpump" else "legacy_pv_pause"
            elif manual_heatpump_active:
                heatpump_boost_owner = "manual_heatpump"
            elif manual_ww_boost_active_export:
                heatpump_boost_owner = "manual_ww_heatpump"
            elif boost_active:
                heatpump_boost_owner = "storage_budget_heatpump"

            if heat_policy_decision is None:
                heat_policy_decision = heat_policy.decide_heat_policy(
                    heat_policy.HeatPolicyInput(
                        now_ts=time.time(),
                        auto_enabled=bool(AUTO_MODE == 1),
                        heat_enabled=bool(wp),
                        heatpump_configured=bool(wp),
                        heater_configured=False,
                    )
                )
            heat_policy_export = heat_policy_decision.as_dict()
            heat_policy_export.update({
                "ts": int(time.time()),
                "time": now.isoformat(timespec="seconds"),
                "service": "energy_manager",
                "domain": "heatpump",
                "hal_output": True,
                "runtime_enabled": bool(heat_policy_runtime_enabled),
                "boost_delivered_kwh": round(max(0.0, _safe_float(heat_policy_boost_delivered_kwh, 0.0)), 4),
                "price_block_started_ts": int(heat_price_block_started_ts or 0),
                "price_block_control_authorized": False,
                "price_gate_reason": heat_policy_price_gate_reason,
            })
            if heat_forecast_result is not None:
                heat_policy_export["forecast_input"] = heat_forecast_result.as_dict()

            idm_cooling_diag = idm_cooling_diagnostics(
                wp_type,
                wp_data,
                getattr(wp, "curr_ext_khl", False) if wp else False,
            )
            dimplex_sg_readback_state = wp_data.get("dimplex_sg_readback_state")
            dimplex_sg_readback_ts = _safe_float(
                wp_data.get("dimplex_sg_readback_ts"),
                0.0,
            )
            dimplex_sg_readback_source = str(
                wp_data.get("dimplex_sg_readback_source") or ""
            )
            dimplex_sg_readback_confirmed = bool(
                wp_data.get("dimplex_sg_readback_confirmed") is True
                and dimplex_sg_readback_state is not None
                and dimplex_sg_readback_ts > 0.0
            )
            dimplex_write_readback_ts = _safe_float(
                getattr(wp, "last_sg_readback_ts", 0.0) if wp else 0.0,
                0.0,
            )
            if (
                wp_type == 5
                and wp
                and getattr(wp, "last_sg_readback_state", None) is not None
                and dimplex_write_readback_ts > dimplex_sg_readback_ts
            ):
                dimplex_sg_readback_state = int(wp.last_sg_readback_state)
                dimplex_sg_readback_ts = dimplex_write_readback_ts
                dimplex_sg_readback_source = "dimplex_modbus_confirmed_readback"
                dimplex_sg_readback_confirmed = True

            json_export = {
                "ts": now.isoformat(), "data": wp_data, "status": wp_status,
                "boost_active": boost_active, "auto_mode": AUTO_MODE,
                "daily_boost_counter": daily_boost_counter,
                "last_pv_boost_time": last_pv_boost_time,
                "last_wp_command_time": last_wp_command_time,
                "last_notstrom_status": last_notstrom_status,
                "price_boost_active": price_boost_active, "pre_pause_active": pre_pause_active,
                "market_plan_heatpump_active": bool(market_heatpump_active),
                "market_plan_heatpump_requested": bool(market_heatpump_requested),
                "market_plan_heatpump_policy_gate": (
                    "allowed"
                    if market_heatpump_active
                    else "heat_policy_runtime_required"
                    if market_heatpump_requested
                    else str(market_heatpump_release.get("reason") or "not_requested")
                ),
                "market_plan_action": market_heatpump_release.get("action"),
                "market_plan_reason": market_heatpump_release.get("reason"),
                "legacy_price_heatpump_active": bool(legacy_price_heatpump_active),
                "pv_pause_active": pv_pause_active, "success": success, "error": wp_error_msg,
                "pv_pause_owner": pv_pause_owner,
                "pv_pause_start_time": pv_pause_start_time,
                "free_for_limbs_w": free_for_limbs_w, "storage_state": storage_state_name,
                "heatpump_budget_w": heatpump_budget_w,
                "heatpump_accounting_budget_w": heatpump_accounting_budget_w,
                "central_heatpump_command_cap_w": central_heatpump_command_cap_w,
                "central_heatpump_effective_budget_w": central_heatpump_effective_budget_w,
                "central_heatpump_effective_budget_source": (
                    central_heatpump_effective_budget_source
                ),
                "heatpump_budget_demand_class": heatpump_budget_demand_class,
                "heatpump_budget_demand_target_c": heatpump_budget_demand_target_c,
                "heatpump_budget_demand_first_seen_ts": heatpump_budget_demand_first_seen_ts,
                "central_heatpump_start_budget_gate": dict(
                    central_heatpump_start_budget_gate
                ),
                "heatpump_positive_signal_window": dict(
                    heatpump_positive_signal_window
                ),
                "heatpump_positive_signal_hold_guard": copy.deepcopy(
                    heatpump_positive_signal_hold_guard
                ),
                "heatpump_positive_signal_restored_unconfirmed": bool(
                    heatpump_positive_signal_restored_unconfirmed
                ),
                "heatpump_positive_signal_restart_readback": dict(
                    heatpump_positive_signal_restart_readback
                ),
                "heatpump_positive_signal_actuator_readback": dict(
                    heatpump_positive_signal_actuator_readback
                ),
                "heatpump_positive_signal_demand_class": (
                    heatpump_positive_signal_demand_class
                ),
                "heatpump_boost_permission_active": bool(
                    heatpump_boost_permission_active
                ),
                "central_heatpump_boost_permission_active": bool(
                    central_heatpump_boost_permission_active
                ),
                "heatpump_positive_signal_retry_not_before_ts": (
                    heatpump_positive_signal_retry_not_before_ts
                ),
                "heatpump_positive_output_blocked_this_cycle": bool(
                    heatpump_positive_output_blocked_this_cycle
                ),
                "heatpump_positive_output_block_reasons": list(
                    dict.fromkeys(heatpump_positive_output_block_reasons)
                ),
                "storage_budget_source_contract": storage_budget_source_contract,
                "storage_primary_budget_diagnostic": (
                    storage_primary_budget_diagnostic
                ),
                "consumer_allocations": consumer_allocations,
                "wallbox_phase_transition_active": wallbox_phase_transition_active,
                "wallbox_phase_transition_reserved_w": wallbox_phase_transition_reserved_w,
                "wallbox_phase_transition_until_ts": wallbox_phase_transition_until_ts,
                "heatpump_running_commitment_w": heatpump_running_commitment_w,
                "heatpump_running_observed_w": running_heatpump_observed_w,
                "heatpump_running_accounting_w": running_heatpump_accounting_w,
                "heatpump_running_accounting_valid": bool(
                    running_heatpump_accounting_valid
                ),
                "heatpump_running_budget_tolerance_w": (
                    running_heatpump_budget_tolerance_w
                ),
                "heatpump_running_underfunded": bool(
                    running_heatpump_underfunded
                ),
                "heatpump_new_start_allowed": not wallbox_phase_transition_active,
                "heatpump_pause_request": heatpump_pause_request,
                "source_recovery_pause_context": source_recovery_pause_context,
                "source_recovery_pause_requested": source_recovery_pause_requested,
                "source_recovery_pause_allowed": source_recovery_pause_allowed,
                "source_recovery_pause_latched": source_recovery_pause_latched,
                "source_recovery_pause_blocks_boost": source_recovery_pause_blocks_boost,
                "source_recovery_heat_budget_override": source_recovery_heat_budget_override,
                "source_recovery_budget_ready": source_recovery_budget_ready,
                "source_recovery_release_reason": source_recovery_release_reason,
                "source_recovery_history_allowed": source_recovery_history_allowed,
                "source_recovery_history_reason": source_recovery_history_reason,
                "source_recovery_compressor_off_before_s": source_recovery_compressor_off_before_s,
                "source_recovery_planned_pause_s": source_recovery_planned_pause_s,
                "storage_manager_owns_energy": storage_manager_owns_energy,
                "energy_autonomy_allowed": energy_autonomy_allowed,
                "local_autonomy_blocked": local_autonomy_blocked,
                "car_blocks_boost": car_blocks_boost,
                "car_blocks_boost_applied": car_blocks_boost_applied,
                "pv_pause_blocked_until": pv_pause_blocked_until,
                "manual_heatpump_active": manual_heatpump_active,
                "manual_ww_boost_active": manual_ww_boost_active_export,
                "wp_last_pv_boost_start_ts": wp_last_pv_boost_start_ts,
                "wp_last_pv_boost_stop_ts": wp_last_pv_boost_stop_ts,
                "luxtronik_ww_runtime": dict(
                    luxtronik_ww_runtime_contract
                ),
                "luxtronik_ww_budget_loss_guard_state": copy.deepcopy(
                    luxtronik_ww_budget_loss_guard_state
                ),
                "luxtronik_ww_budget_loss_blocked_until_fresh_budget": bool(
                    luxtronik_ww_budget_loss_blocked_until_fresh_budget
                ),
                "luxtronik_ww_budget_loss_effective_block": bool(
                    luxtronik_ww_budget_loss_effective_block
                ),
                "pv_boost_start_outcome": dict(pv_boost_last_outcome),
                "pv_boost_retry_not_before_ts": pv_boost_retry_not_before_ts,
                "wp_last_ww_cycle_start_ts": wp_last_ww_cycle_start_ts,
                "wp_last_ww_cycle_target_c": wp_last_ww_cycle_target_c,
                "wp_compressor_running": bool(wp_compressor_running_now),
                "wp_compressor_observation_valid": bool(wp_compressor_observation_valid),
                "wp_compressor_history_valid": bool(wp_compressor_history_valid),
                "wp_compressor_last_start_ts": wp_compressor_last_start_ts,
                "wp_compressor_last_stop_ts": wp_compressor_last_stop_ts,
                "wp_compressor_last_run_s": wp_compressor_last_run_s,
                "restart_revalidation": restart_revalidation,
                "wp_live_revalidation": {
                    "valid": bool(wp_status.get("valid")),
                    "source_fresh": bool(wp_status.get("source_fresh")),
                    "source_age_s": wp_status.get("source_age_s"),
                },
                "ww_cycle_min_runtime_remaining_s": max(
                    0,
                    int(
                        heat_policy.WW_CYCLE_MIN_RUNTIME_S
                        - max(0.0, time.time() - _safe_float(wp_last_ww_cycle_start_ts, 0.0))
                    ),
                ) if wp_last_ww_cycle_start_ts else 0,
                "wp_last_ww_mode": getattr(wp, 'last_ww_mode', None) if wp else None,
                "wp_last_ww_temp": getattr(wp, 'last_ww_temp', None) if wp else None,
                "wp_last_ww_cmd_time": getattr(wp, 'last_ww_cmd_time', 0.0) if wp else 0.0,
                "wp_curr_ext_ww": int(getattr(wp, 'curr_ext_ww', 0) or 0) if wp else 0,
                "wp_curr_ext_hz": int(getattr(wp, 'curr_ext_hz', 0) or 0) if wp else 0,
                "predump_heatpump_active": predump_heatpump_active,
                "predump_heatpump_raw_active": predump_heatpump_raw_active,
                "predump_heatpump_started_ts": predump_heatpump_started_ts,
                "predump_heatpump_hold_until": predump_heatpump_hold_until,
                "predump_heatpump_hold_remaining_s": max(0, int(predump_heatpump_hold_until - time.time())) if predump_heatpump_hold_until else 0,
                "heatpump_boost_owner": heatpump_boost_owner,
                "heat_policy": heat_policy_export,
                "heat_intent": heat_intent_export,
                "heat_intent_runtime_validation": heat_intent_runtime_validation,
                "heat_runtime_actuation": dict(heat_runtime_actuation),
                "legacy_heat_automation_owner": dict(
                    legacy_heat_automation_owner
                ),
                "ww_timer_automation_owner": dict(
                    ww_timer_automation_owner
                ),
                "heat_price_block_started_ts": heat_price_block_started_ts,
                "heat_policy_boost_delivered_kwh": heat_policy_boost_delivered_kwh,
                "predump_heatpump_targets_reached": predump_heatpump_targets_reached,
                "predump_heatpump_protect_block": predump_heatpump_protect_block,
                "wp_takt_protect": {
                    "active": WP_TAKT_PROTECT,
                    "min_runtime_min": WP_MIN_RUNTIME_MIN,
                    "restart_block_min": WP_RESTART_BLOCK_MIN,
                    "price_start_blocked": bool(price_heatpump_takt_start_blocked),
                    "price_start_remaining_s": max(0, int(price_heatpump_start_block_remaining_s)),
                    "price_stop_held": bool(price_heatpump_takt_stop_held),
                    "price_stop_remaining_s": max(0, int(price_heatpump_stop_block_remaining_s))
                },
                "idm_surplus_kw": getattr(wp, 'last_sent_surplus_kw', 0.0) if wp else 0.0,
                "idm_ext_ww": int(getattr(wp, 'curr_ext_ww', False) or 0) if wp else 0,
                "idm_ext_hz": int(getattr(wp, 'curr_ext_hz', False) or 0) if wp else 0,
                "idm_ext_khl": int(getattr(wp, 'curr_ext_khl', False) or 0) if wp else 0,
                **idm_cooling_diag,
                "dimplex_sg_state": getattr(wp, 'curr_sg_state', None) if wp_type == 5 and wp else None,
                "dimplex_sg_readback_state": dimplex_sg_readback_state if wp_type == 5 else None,
                "dimplex_sg_readback_ts": dimplex_sg_readback_ts if wp_type == 5 else 0.0,
                "dimplex_sg_readback_source": dimplex_sg_readback_source if wp_type == 5 else "",
                "dimplex_sg_readback_confirmed": bool(dimplex_sg_readback_confirmed) if wp_type == 5 else False,
                "dimplex_sg_register": getattr(wp, 'sg_register', None) if wp_type == 5 and wp else None,
                "dimplex_sg_address": getattr(wp, 'sg_address', None) if wp_type == 5 and wp else None,
                "dimplex_allow_dark_green": bool(getattr(wp, 'allow_dark_green', False)) if wp_type == 5 and wp else False,
                "shelly_sg_state": getattr(wp, 'sg_state', None) if has_shelly_heatpump and wp else None,
                "shelly_pause_state": getattr(wp, 'pause_state', None) if has_shelly_heatpump and wp else None,
                "shelly_live_sg_state": getattr(wp, 'last_live_sg_state', None) if has_shelly_heatpump and wp else None,
                "shelly_live_pause_state": getattr(wp, 'last_live_pause_state', None) if has_shelly_heatpump and wp else None,
                "shelly_sg_readback_state": getattr(wp, 'last_live_sg_state', None) if has_shelly_heatpump and wp else None,
                "shelly_sg_readback_ts": _safe_float(getattr(wp, 'last_live_sg_ts', 0.0), 0.0) if has_shelly_heatpump and wp else 0.0,
                "shelly_sg_readback_source": "shelly_relay_confirmed_readback" if has_shelly_heatpump and wp else "",
                "shelly_sg_readback_confirmed": bool(
                    has_shelly_heatpump
                    and wp
                    and isinstance(getattr(wp, 'last_live_sg_state', None), bool)
                    and _safe_float(getattr(wp, 'last_live_sg_ts', 0.0), 0.0) > 0.0
                ),
                "shelly_startup_sync_pending": bool(shelly_startup_sync_pending),
                "actor_writes_blocked": bool(getattr(wp, "actor_writes_blocked", False)) if wp else False,
                "actor_write_block_reason": str(getattr(wp, "actor_write_block_reason", "") or "") if wp else "",
                "manual_heatpump_command": {
                    "valid": bool(manual_boost_command.get("valid")),
                    "action": str(manual_boost_command.get("action") or "none"),
                    "schema": str(manual_boost_command.get("schema") or "none"),
                    "reason": str(manual_boost_command.get("reason") or "absent"),
                },
                "v2h": {
                    "active": False,
                    "monitoring": V2H_ENABLE == 1,
                    "read_only": True,
                    "allowed": v2h_allowed,
                    "detected_discharge": bool(e3dc.get('wb', 0) < -50),
                    "reason": v2h_reason,
                }
            }
            write_json_atomic_tolerant(RAMDISK_FILE, json_export, mode=0o664, warn_label="Luxtronik-Live-Datei")
            latest_energy_live_state.clear()
            latest_energy_live_state.update(json_export)
            persist_energy_restart_checkpoint(
                ENERGY_STATE_FILE,
                json_export,
                energy_checkpoint_runtime,
            )
            write_json_atomic_tolerant(HEAT_POLICY_LATEST_PATH, heat_policy_export, mode=0o664, warn_label="Heat-Policy-Latest")

            if time.time() - last_history_write >= 60:
                append_luxtronik_history(json_export, now)
                last_history_write = time.time()

            decision_state = "beobachtet"
            decision_reason = "Keine aktive Waermefreigabe"
            observed_wp_power_w, heatpump_power_known, heatpump_accepting_power = heatpump_power_observation(
                wp_data,
                wp_status,
            )
            heatpump_source_ts = heatpump_native_source_timestamp(wp_status)
            if predump_heatpump_active:
                decision_state = "predump_waerme_hold" if predump_heatpump_hold_active else "predump_waerme_start"
                decision_reason = "Pre-Dump gibt Waermepumpe frei; Mindestlaufzeit schuetzt vor kurzem Takten"
            elif price_boost_active:
                decision_state = "preis_waerme_aktiv" if heatpump_accepting_power else "preis_waerme_budget_frei"
                if market_heatpump_active:
                    decision_reason = (
                        "Marktvertrag aktiv; Wärmepumpe nimmt Leistung an"
                        if heatpump_accepting_power
                        else (
                            "Marktvertrag bietet Wärmebudget an; Wärmepumpe nimmt aktuell keine Leistung auf"
                            if heatpump_power_known
                            else "Marktvertrag bietet Wärmebudget an; Leistungsaufnahme wird für diesen Freigabepfad nicht gemessen"
                        )
                    )
                else:
                    decision_reason = (
                        "Preis-/Tariffenster aktiv; Wärmepumpe nimmt Leistung an"
                        if heatpump_accepting_power
                        else (
                            "Preis-/Tariffenster bietet Wärmebudget an; Wärmepumpe nimmt aktuell keine Leistung auf"
                            if heatpump_power_known
                            else "Preis-/Tariffenster bietet Wärmebudget an; Leistungsaufnahme wird für diesen Freigabepfad nicht gemessen"
                        )
                    )
            elif pv_pause_active:
                decision_state = "waerme_pause"
                decision_reason = (
                    str(heatpump_pause_request.get("reason") or "Quell-Erholung aktiv; Waermepumpe wird wegen Preis/PV-Strategie gehalten")
                    if pv_pause_owner == "source_recovery_heatpump"
                    else "Quell-Erholung aktiv; Waermepumpe wird wegen Preis/PV-Strategie gehalten"
                )
            elif manual_heatpump_active or manual_ww_boost_active_export:
                decision_state = "manuelle_waermefreigabe"
                decision_reason = "Manuelle Waermefreigabe aktiv"
            elif boost_active:
                decision_state = "pv_waerme_aktiv" if heatpump_accepting_power else "pv_waerme_budget_frei"
                decision_reason = (
                    "PV-/Budget-Freigabe aktiv; Waermepumpe nimmt Leistung an"
                    if heatpump_accepting_power
                    else (
                        "PV-/Budget-Freigabe angeboten; Wärmepumpe nimmt aktuell keine Leistung auf"
                        if heatpump_power_known
                        else "PV-/Budget-Freigabe aktiv; SG-Ready-/Freigabekontakt gesetzt, Leistungsaufnahme wird nicht gemessen"
                    )
                )
            elif predump_heatpump_targets_reached:
                decision_state = "zieltemperatur_erreicht"
                decision_reason = "Pre-Dump-Waermefreigabe blockiert: Zieltemperaturen erreicht"
            elif predump_heatpump_protect_block:
                decision_state = "wq_schutz"
                decision_reason = "Waermefreigabe blockiert: Waermequelle zu kalt"

            energy_record = {
                "ts": int(time.time()),
                "time": datetime.now().isoformat(timespec="seconds"),
                "service": "energy_manager",
                "decision": {
                    "state": decision_state,
                    "reason": decision_reason,
                    "price_action": price_action,
                    "boost_active": bool(boost_active),
                    "price_boost_active": bool(price_boost_active),
                    "market_plan_heatpump_active": bool(market_heatpump_active),
                    "market_plan_action": market_heatpump_release.get("action"),
                    "market_plan_reason": market_heatpump_release.get("reason"),
                    "legacy_price_heatpump_active": bool(legacy_price_heatpump_active),
                    "pv_pause_active": bool(pv_pause_active),
                    "pv_pause_owner": pv_pause_owner,
                    "pre_pause_active": bool(pre_pause_active),
                    "heatpump_boost_owner": heatpump_boost_owner,
                    "source_recovery_pause_requested": bool(source_recovery_pause_requested),
                    "source_recovery_pause_allowed": bool(source_recovery_pause_allowed),
                    "source_recovery_pause_latched": bool(source_recovery_pause_latched),
                    "source_recovery_pause_blocks_boost": bool(source_recovery_pause_blocks_boost),
                    "source_recovery_heat_budget_override": bool(source_recovery_heat_budget_override),
                    "source_recovery_release_reason": source_recovery_release_reason,
                    "storage_manager_owns_energy": bool(storage_manager_owns_energy),
                    "energy_autonomy_allowed": bool(energy_autonomy_allowed),
                    "local_autonomy_blocked": local_autonomy_blocked,
                    "actions": cycle_actions[-8:],
                },
                "inputs": {
                    "grid_w": _safe_int(grid),
                    "bat_w": _safe_int(bat),
                    "soc": _safe_float(soc),
                    "current_price_ct": _safe_float(current_price, 99.9),
                    "storage_state": storage_state_name,
                    "free_for_limbs_w": _safe_int(free_for_limbs_w),
                    "heatpump_budget_w": _safe_int(heatpump_budget_w, 0) if heatpump_budget_w is not None else None,
                    "storage_budget_source_contract": storage_budget_source_contract,
                    "storage_primary_budget_diagnostic": (
                        storage_primary_budget_diagnostic
                    ),
                    "consumer_allocations": consumer_allocations,
                    "wallbox_phase_transition_active": bool(wallbox_phase_transition_active),
                    "wallbox_phase_transition_reserved_w": _safe_int(wallbox_phase_transition_reserved_w),
                    "wallbox_phase_transition_until_ts": _safe_float(wallbox_phase_transition_until_ts),
                    "heatpump_pause_request": heatpump_pause_request,
                },
                "heatpump": {
                    "configured": bool(wp),
                    "connected": bool(wp_connected),
                    "type": int(wp_type),
                    "wp_power_w": observed_wp_power_w,
                    "power_known": bool(heatpump_power_known),
                    "accepting_power": bool(heatpump_accepting_power),
                    "source_ts": heatpump_source_ts,
                    "budget_offered": bool(
                        boost_active
                        or price_boost_active
                        or predump_heatpump_active
                        or heatpump_positive_signal_window.get("active") is True
                    ),
                    "at_c": _safe_float(at, 0.0),
                    "at_mittel_c": _safe_float(at_mittel, 0.0),
                    "wq_aus_c": _safe_float(wq_aus, 0.0),
                    "hz_mode": wp_status.get("HZ_Mode") if isinstance(wp_status, dict) else None,
                    "ww_mode": wp_status.get("WW_Mode") if isinstance(wp_status, dict) else None,
                    "ww_ist_c": _safe_float(wp_data.get("Warmwasser_Ist"), 0.0) if isinstance(wp_data, dict) and wp_data.get("Warmwasser_Ist") is not None else None,
                    "rl_ist_c": _safe_float(wp_data.get("Ruecklauf_Ist"), 0.0) if isinstance(wp_data, dict) and wp_data.get("Ruecklauf_Ist") is not None else None,
                    "predump_raw_active": bool(predump_heatpump_raw_active),
                    "predump_active": bool(predump_heatpump_active),
                    "predump_hold_until": int(predump_heatpump_hold_until or 0),
                    "predump_hold_remaining_s": max(0, int(predump_heatpump_hold_until - time.time())) if predump_heatpump_hold_until else 0,
                    "predump_min_runtime_s": int(PREDUMP_HEATPUMP_MIN_RUNTIME_MIN * 60),
                    "targets_reached": bool(predump_heatpump_targets_reached),
                    "protect_block": bool(predump_heatpump_protect_block),
                    "market_plan_active": bool(market_heatpump_active),
                    "market_plan_action": market_heatpump_release.get("action"),
                    "market_plan_reason": market_heatpump_release.get("reason"),
                },
                "wallbox_context": {
                    "wb1_locked": bool(wb1_locked),
                    "wb2_locked": bool(wb2_locked),
                    "car_blocks_boost": bool(car_blocks_boost),
                    "car_blocks_boost_applied": bool(car_blocks_boost_applied),
                    "car_blocks_pause": bool(car_blocks_pause),
                    "phase_transition_active": bool(wallbox_phase_transition_active),
                    "phase_transition_reserved_w": _safe_int(wallbox_phase_transition_reserved_w),
                },
            }
            energy_record = build_energy_decision_record(locals())
            write_energy_decision_history(energy_record, current_config)
            try:
                write_decision_surface_record(
                    build_energy_surface_record(energy_record),
                    path=EMS_DECISION_LATEST_PATH,
                )
            except Exception as exc:
                logger.debug("EMS-Decision-Surface fuer Energy konnte nicht geschrieben werden: %s", exc)

            # --- Stündliches Datei-Backup (Ersatz für blockierte C++ system() Aufrufe) ---
            if now.minute == 2 and last_debug_archive_hour != now.hour:
                try:
                    if os.path.exists(AWATTAR_DEBUG_PATH):
                        dst = os.path.join(os.path.dirname(AWATTAR_DEBUG_PATH), f"awattardebug.{now.hour}.txt")
                        shutil.copy2(AWATTAR_DEBUG_PATH, dst)

                    if now.hour == 0:
                        dv_src = os.path.join(os.path.dirname(AWATTAR_DEBUG_PATH), "dv.txt")
                        if os.path.exists(dv_src):
                            dv_dst = os.path.join(os.path.dirname(dv_src), f"dv.{now.day}.txt")
                            shutil.copy2(dv_src, dv_dst)
                except Exception: pass
                last_debug_archive_hour = now.hour

            # Tageswechsel
            if now.day != last_day:
                try:
                    trim_luxtronik_ramdisk_history(force=True)
                except Exception as e:
                    logger.warning(f"Fehler beim Archivieren der Luxtronik-Historie: {e}")

                daily_boost_counter = 0
                heat_policy_boost_delivered_kwh = 0.0
                heat_policy_last_energy_ts = 0.0
                last_day = now.day

                # Das kompakte Betriebsarchiv besitzt genau einen Vertrag:
                # sieben Tage Retention, unabhängig von Minutenpuffer und ML.
                try:
                    cleanup_luxtronik_archives()
                except Exception as e:
                    logger.warning(f"Fehler beim Bereinigen des Luxtronik-Archivs: {e}")

        except Exception as e:
            logger.critical(f"Kritischer Fehler: {e}", exc_info=True)
            write_json_atomic_tolerant(
                RAMDISK_FILE,
                {"success": False, "error": str(e), "ts": now.isoformat()},
                mode=0o664,
                warn_label="Luxtronik-Live-Datei",
            )

        # --- Freeze Detection / Auto-Restart Hook (ENTFERNT) ---
        # Wurde für das alte C++ System benutzt. Da das V6 System rein Python-basiert ist,
        # und e3dc.service (Eba-M) ausgemustert wurde, wurde dieser Watchdog-Restart deaktiviert.
        stale_file = '/var/www/html/ramdisk/live_stale_hash.json'
        if os.path.exists(stale_file):
            try:
                os.remove(stale_file)
            except Exception:
                pass


        time.sleep(2)

if __name__ == "__main__":
    main()
