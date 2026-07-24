import sys
import time
import json
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
    from decision_history import write_history_record
    from ems_decision_diagnostics import (
        build_energy_surface_record,
        default_surface_path,
        write_decision_surface_record,
    )
except ModuleNotFoundError:
    _INSTALLER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _INSTALLER_DIR not in sys.path:
        sys.path.insert(0, _INSTALLER_DIR)
    from quiet_logging import install_quiet_info_filter
    from decision_history import write_history_record
    from ems_decision_diagnostics import (
        build_energy_surface_record,
        default_surface_path,
        write_decision_surface_record,
    )

try:
    from Installer.Heat import forecast as heat_forecast
    from Installer.Heat import policy as heat_policy
    from Installer.heat_actuator_safety import default_heat_actuator_gate
except ModuleNotFoundError:
    _INSTALLER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _INSTALLER_DIR not in sys.path:
        sys.path.insert(0, _INSTALLER_DIR)
    from Heat import forecast as heat_forecast
    from Heat import policy as heat_policy
    from heat_actuator_safety import default_heat_actuator_gate

logger = logging.getLogger("EnergyManager")
_SESSION_WRITE_WARNED = set()


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
                        ww_physically_running = heatpump_compressor_running(wp_data)
                        if ist_ww >= target_ww:
                            new_ww_mode = 0
                        elif ww_physically_running or getattr(self, 'curr_ext_ww', 0) == 1:
                            # Einen realen Zyklus beziehungsweise einen bereits
                            # übernommenen Sollwert bis zur Zieltemperatur halten.
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

    def _persist_state(self, reason=""):
        if not self.state_path:
            return
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
        write_json_atomic_tolerant(
            self.state_path,
            payload,
            mode=0o664,
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
        """Aktualisiert nur die Anzeigeevidenz, nie Sollzustand oder Regelentscheidung."""

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
        if isinstance(sg_live, bool):
            self._record_live_relay_state(self.sg_ip, sg_live, time.time())
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
        self._persist_state(self.last_sync_reason)
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

        self._persist_state(self.last_sync_reason)

        return success

    def write_hz_boost(self, mode, temp=None): self.set_boost(mode, temp, 0, None)
    def write_ww_boost(self, mode, temp=45.0): self.set_boost(0, None, mode, temp)
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
        if target_kw > current + self.surplus_ramp_kw:
            return current + self.surplus_ramp_kw
        if target_kw < current - self.surplus_ramp_kw:
            return current - self.surplus_ramp_kw
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
        min_interval_due = (now_ts - getattr(self, 'last_surplus_write_ts', 0.0)) >= self.surplus_min_write_interval_s
        heartbeat_due = (now_ts - getattr(self, 'last_surplus_write_ts', 0.0)) >= self.surplus_heartbeat_s
        if not min_interval_due and not heartbeat_due:
            return True
        # 0.00 kW ist nur ein Freigabe-Aus-Zustand. Wenn er bereits anliegt,
        # braucht Register 74 keinen Minutentakt und das Log bleibt ruhig.
        if send_kw <= 0.001 and last_kw <= 0.001 and getattr(self, 'last_surplus_write_ts', 0.0) > 0:
            return True
        if abs(send_kw - last_kw) < self.surplus_deadband_kw and not heartbeat_due:
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
LEGACY_ENERGY_STATE_FILE = "/var/www/html/data/morning_boost_state.json"
ENERGY_STATE_FILE = "/var/www/html/data/energy_manager_state.json"
SHELLY_HEATPUMP_STATE_FILE = "/var/www/html/data/shelly_heatpump_state.json"
ENERGY_STATE_ACTIVE_RESTORE_MAX_AGE_S = 1200.0
HEATPUMP_LIVE_REVALIDATION_MAX_AGE_S = 120.0
FLAG_FILE = "/var/www/html/ramdisk/manual_boost.flag"
VEHICLES_JSON_FILE = "/var/www/html/ramdisk/vehicles.json"
WS_JSON_FILE = "/var/www/html/ramdisk/waermepumpe.json"
FORCE_FLAG_FILE = "/var/www/html/ramdisk/force_bluelink.flag"
V4_CONFIG_PATH = "/var/www/html/data/e3dc_v4.json"
STORAGE_PLAN_PATH = "/var/www/html/ramdisk/storage_plan.json"
PRICE_BOOST_PLAN_PATH = "/var/www/html/ramdisk/price_boost_plan.json"
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
    if _safe_float(data.get("Leistung_Verdichter_W", status.get("Leistung_Verdichter_W")), 0.0) > 100.0:
        return True
    for key in ("Leistungsaufnahme", "Leistung_Heiz_kW", "Heizleistung Ist"):
        if _safe_float(data.get(key, status.get(key)), 0.0) > 0.1:
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


def attempt_heatpump_pv_boost_start(
    wp,
    boost_args,
    *,
    now_ts=None,
    retry_not_before_ts=0.0,
    retry_backoff_s=60.0,
):
    """Führt ausschließlich einen PV-Boost-Start mit typisiertem Ergebnis aus.

    Der Backoff begrenzt fehlgeschlagene Startversuche. Stop-, Nutzer-Aus- und
    Safety-Pfade rufen diesen Starthelfer bewusst nicht auf und bleiben daher
    jederzeit ausführbar.
    """
    now_value = time.time() if now_ts is None else _safe_float(now_ts, time.time())
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

def heatpump_power_observation(wp_data):
    """Return measured heat-pump power and whether that measurement exists."""
    if not isinstance(wp_data, dict):
        return 0, False, False
    power_known = wp_data.get("Leistung_Verdichter_W") is not None
    observed_wp_power_w = _safe_int(wp_data.get("Leistung_Verdichter_W", 0))
    return observed_wp_power_w, power_known, bool(power_known and observed_wp_power_w > 100)

_last_luxtronik_history_trim_ts = 0.0

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

def _append_history_line(path, line, mode=0o664):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
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

    The complete day archive is written to BACKUP_DIR on every history append.
    The ramdisk file is only a fast live buffer for charts and diagnostics.
    """
    try:
        if not os.path.exists(path):
            return True
        current_size = os.path.getsize(path)
        if not force and current_size <= max_bytes:
            return True
        with open(path, "r", encoding="utf-8") as f:
            lines = [line for line in f.readlines() if line.strip()]
        keep_lines = lines[-max(1, int(max_lines)):]
        while keep_lines and sum(len(line.encode("utf-8")) for line in keep_lines) > max_bytes:
            keep_lines = keep_lines[max(1, len(keep_lines) // 10):]
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(keep_lines)
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
    try:
        _append_history_line(archive_path, line)
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

def write_energy_decision_history(record, config):
    enabled = str(get_cfg_value(config, "energy_decision_history_enable", 1)).strip().lower()
    if enabled in ("0", "false", "no", "off", "nein", "aus"):
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
                "decision.actions",
                "heatpump.predump_active",
                "heatpump.protect_block",
                "heatpump.targets_reached",
            ),
            default_interval_s=60,
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
    wp_obj = ctx.get("wp")
    idm_cooling_diag = idm_cooling_diagnostics(
        ctx.get("wp_type", -1),
        wp_data,
        getattr(wp_obj, "curr_ext_khl", False) if wp_obj else False,
    )

    decision_state = "beobachtet"
    decision_reason = "Keine aktive Waermefreigabe"
    observed_wp_power_w, heatpump_power_known, heatpump_accepting_power = heatpump_power_observation(wp_data)
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

    return {
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
            "budget_offered": bool(boost_active or price_boost_active or predump_heatpump_active),
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

def heatpump_ww_cycle_running(wp_status=None, wp_data=None, ww_requested=False):
    """Return true only when warm-water mode is physically active.

    Some Luxtronik values expose ``WW_Mode=1`` while the compressor and pumps are
    idle. Treating that as a running WW cycle would keep the productive heat
    policy in a protected hold without a real cycle to protect.
    """

    status = wp_status if isinstance(wp_status, dict) else {}
    data = wp_data if isinstance(wp_data, dict) else {}
    physical_ww_status = data.get("Status_Warmwasser", status.get("Status_Warmwasser"))
    if physical_ww_status is not None and _safe_int(physical_ww_status, -1) == 3:
        return True

    operating_mode = data.get("Betriebsart", status.get("Betriebsart"))
    compressor_running = heatpump_compressor_running(data, status)
    if operating_mode is not None and _safe_int(operating_mode, -1) == 1 and compressor_running:
        return True

    if not ww_requested:
        return False
    ww_mode = normalize_luxtronik_shi_mode(
        status.get("SHI_WW_Mode", status.get("WW_Mode", data.get("WW_Mode", data.get("Modus Warmw."))))
    )
    if ww_mode != 1:
        return False
    if compressor_running:
        return True
    for key in ("Verdichter", "Verdichter_Ein", "BUP", "SLP"):
        if _safe_int(data.get(key, status.get(key)), 0) > 0:
            return True
    return False

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
    previous_target_state=heat_policy.TARGET_NORMAL,
    previous_sg_ready_state=heat_policy.SG_READY_NORMAL,
    previous_available_budget_w=0,
):
    """Translate Energy-Manager state into the central heat policy contract."""

    temp_ctx = _heat_policy_temperature_context(config, wp_data, at_mittel, heizgrenze_temp, conf_wws, conf_hz)
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
        or bool(market_heatpump_release.get("allowed") if isinstance(market_heatpump_release, dict) else False)
        or price_value <= _safe_float(price_hard_limit, -99.0)
    )
    market_end_ts = _market_release_end_ts_s(market_heatpump_release if isinstance(market_heatpump_release, dict) else {})
    window_end_ts = market_end_ts or price_boost_window_end_ts("heatpump")
    required_w = abs(_safe_int(grid_start_limit, -3500))
    pv_budget_w = max(0, _safe_int(free_for_limbs_w, 0))
    predump_budget_w = pv_budget_w if predump_heatpump_active else 0
    battery_empty_soc = _safe_float(_heat_config_value(config, ("heat_price_block_empty_soc",), max(5.0, min(15.0, _safe_float(min_soc, 80.0) - 20.0))), 10.0)
    summer_mode = _safe_float(at_mittel, 20.0) > _safe_float(heizgrenze_temp, 10.0)
    ww_requested = bool(boost_active or price_boost_active or predump_heatpump_active or is_ww_timer_running)
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
        ww_cycle_running=heatpump_ww_cycle_running(wp_status, wp_data, ww_requested=ww_requested),
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

    if not auto_enabled:
        reason = "Nutzer-Aus: gespeicherte Aktorzustände und Holds verworfen"
    elif not state_fresh:
        reason = "Statusdatei stale: gespeicherte Aktorzustände und Holds verworfen"
    elif active_command_saved:
        reason = "Physischer Zustand unbekannt: aktive Kommandos verworfen, nur Sicherheits-Holds übernommen"
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

def price_boost_allows(device):
    """Zentraler EPEX-Preisboost: Geraete nur einschalten, wenn explizit erlaubt."""
    try:
        if os.path.exists(V4_CONFIG_PATH):
            with open(V4_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if str(cfg.get("cheap_grid_boost_enable", 0)).strip().lower() not in ("1", "true", "yes", "on"):
                return False
        if not os.path.exists(PRICE_BOOST_PLAN_PATH):
            return False
        with open(PRICE_BOOST_PLAN_PATH, "r", encoding="utf-8") as f:
            plan = json.load(f)
        win = plan.get("active_window") or {}
        now_ms = int(time.time() * 1000)
        start_ms = int(win.get("start_timestamp", 0) or 0)
        end_ms = int(win.get("end_timestamp", 0) or 0)
        return bool(plan.get("enabled") and plan.get("active")
                    and start_ms <= now_ms < end_ms
                    and plan.get("allow", {}).get(device, False))
    except Exception:
        return False

def price_boost_window_end_ts(device):
    """Return active cheap-grid window end timestamp in seconds for diagnostics/guards."""
    try:
        if not os.path.exists(PRICE_BOOST_PLAN_PATH):
            return None
        with open(PRICE_BOOST_PLAN_PATH, "r", encoding="utf-8") as f:
            plan = json.load(f)
        win = plan.get("active_window") or {}
        now_ms = int(time.time() * 1000)
        start_ms = int(win.get("start_timestamp", 0) or 0)
        end_ms = int(win.get("end_timestamp", 0) or 0)
        if not (
            plan.get("enabled")
            and plan.get("active")
            and start_ms <= now_ms < end_ms
            and plan.get("allow", {}).get(device, False)
        ):
            return None
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
    """Map native live_data_py.json keys to the compact dashboard contract."""
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
    mapped.setdefault("bat", first_value("bat", "Battery_Power"))
    mapped.setdefault("soc", first_value("soc", "SOC"))
    mapped.setdefault("notstrom_status", first_value("notstrom_status", "Notstrom_Status", "Emergency_Power_Status"))
    mapped.setdefault("wb_power", first_value("wb_power", "Wallbox_Power"))
    mapped.setdefault("wb_session_kwh", first_value("wb_session_kwh", "Wallbox_Session_kWh"))
    mapped.setdefault("wb_locked", first_value("wb_locked", "Wallbox_Locked"))
    mapped.setdefault("prices", [])
    mapped.setdefault("forecast", [])
    return mapped

def read_e3dc_live_for_energy_manager(timeout=10, live_path="/var/www/html/ramdisk/live_data_py.json"):
    """Read dashboard live JSON, falling back to native ramdisk JSON in Docker."""
    errors = []
    try:
        response = requests.get("http://localhost/get_live_json.php", timeout=timeout)
        if response.status_code == 200:
            text = response.text.strip()
            if text:
                data = json.loads(text)
                if isinstance(data, dict):
                    return data
                errors.append("get_live_json.php lieferte kein JSON-Objekt")
            else:
                errors.append("get_live_json.php lieferte eine leere Antwort")
        else:
            errors.append(f"get_live_json.php HTTP {response.status_code}")
    except Exception as exc:
        errors.append(f"get_live_json.php: {exc}")

    try:
        if os.path.exists(live_path):
            with open(live_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            mapped = _energy_live_from_ramdisk(data)
            if mapped:
                return mapped
            errors.append("live_data_py.json lieferte kein JSON-Objekt")
        else:
            errors.append("live_data_py.json fehlt")
    except Exception as exc:
        errors.append(f"live_data_py.json: {exc}")

    raise RuntimeError("; ".join(errors))

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
        heat_policy_price_gate_reason = ""
        heat_policy_runtime_enabled = False

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

            PRICE_BOOST_ENABLE = get_cfg_value(current_config, 'price_boost_enable', 0)
            PRICE_LIMIT = get_cfg_value(current_config, 'price_limit', 20.0)
            PRICE_MIN_DURATION = get_cfg_value(current_config, 'price_min_duration', 60)
            PRICE_MAX_DAILY = get_cfg_value(current_config, 'price_max_daily', 180)
            PRICE_HARD_LIMIT = get_cfg_value(current_config, 'price_hard_limit', -99.0)
            PRICE_PAUSE_LIMIT = get_cfg_value(current_config, 'price_pause_limit', 35.0)
            HEAT_POLICY_RUNTIME_ENABLE = get_cfg_int(current_config, 'heat_policy_runtime_enable', 0)
            heat_policy_runtime_enabled = HEAT_POLICY_RUNTIME_ENABLE == 1
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
            if not energy_autonomy_allowed:
                if get_cfg_int(current_config, 'price_boost_enable', 0) == 1:
                    local_autonomy_blocked.append("lokale Preisentscheidung")
                if PV_PAUSE_ENABLE_RAW == 1:
                    local_autonomy_blocked.append("PV-Pause")
                if SMART_WBHOUR_ENABLE_RAW == 1:
                    local_autonomy_blocked.append("Smart-wbhour")
                if legacy_mb_enable == 1 or legacy_si_enable == 1:
                    local_autonomy_blocked.append("Legacy-Morning/SI")
                if local_autonomy_blocked and not legacy_autonomy_warned:
                    logger.info(
                        "V5-Besitzerregel aktiv: Storage Manager entscheidet Energieverteilung; "
                        "Energy Manager führt nur Aktoren aus. Blockierte lokale Autonomie: %s. "
                        "Pre-Dump-, Preisplan- und manuelle Freigaben bleiben erlaubt.",
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
                            installer_main = os.path.join(install_root, "installer_main.py")
                            if os.path.exists(installer_main):
                                try:
                                    log_file = os.path.join(LOG_DIR, "auto_self_update.log")
                                    cmd = f"sudo /usr/bin/python3 {installer_main} --update-e3dc --unattended"

                                    with open(log_file, "w") as f:
                                        f.write(f"=== Starting Auto-Update at {datetime.now()} ===\n")
                                        f.write(f"Command: {cmd}\n---\n")
                                    os.chmod(log_file, 0o664)

                                    logger.info(f"Führe Update-Kommando aus: {cmd}")
                                    subprocess.Popen(f"nohup {cmd} >> {log_file} 2>&1 &", shell=True)
                                    logger.info("Update-Prozess im Hintergrund gestartet.")
                                except Exception as e:
                                    logger.error(f"Fehler beim Starten des Auto-Updates: {e}")
                            else:
                                logger.error("Auto-Update fehlgeschlagen: self_update.py nicht gefunden.")

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
                    wp_live_age_s = max(0.0, time.time() - os.path.getmtime(WS_JSON_FILE))
                    wp_live_fresh = wp_live_age_s <= HEATPUMP_LIVE_REVALIDATION_MAX_AGE_S
                    raw_ws_data = json.load(f)
                    ws_data = raw_ws_data
                    if (
                        isinstance(raw_ws_data, dict)
                        and isinstance(raw_ws_data.get("data"), dict)
                        and str(raw_ws_data.get("source") or "").strip().lower() in ("dimplex_live", "stiebel_isg_live")
                    ):
                        ws_data = dict(raw_ws_data.get("data") or {})
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

                    hz_mode_raw = ws_data.get('Modus Heizen')
                    ww_mode_raw = ws_data.get('Modus Warmw.')
                    shi_hz_mode = normalize_luxtronik_shi_mode(hz_mode_raw)
                    shi_ww_mode = normalize_luxtronik_shi_mode(ww_mode_raw)
                    has_valid_status_data = bool(
                        wp_live_fresh
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
                        'source_age_s': wp_live_age_s,
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
                        if WW_VON <= WW_BIS: is_ww_timer_running = (WW_VON <= cur_h < WW_BIS)
                        else: is_ww_timer_running = (cur_h >= WW_VON or cur_h < WW_BIS)

                    man_flag = "/var/www/html/ramdisk/manual_ww_boost.flag"
                    if os.path.exists(man_flag) and (time.time() - os.path.getmtime(man_flag)) < (WW_SOFORT_DURATION * 60):
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
                                boost_possible = bool(price_boost_active or pre_pause_active or pv_pause_active)

                                if not boost_possible:
                                    # Log-Throttling: Debug-Ausgabe max. 1x alle 30 Min (nicht bei jedem Zyklus)
                                    # WICHTIG: Der Reset SELBST (unten) ist NICHT von dieser Bedingung abhaengig!
                                    # Er laeuft bei jedem Zyklus, sobald wp_write_allowed=True gilt.
                                    # D.h. nach einem Crash wird der Boost spaetestens nach 65s zurueckgesetzt.
                                    if (time.time() - last_safety_check_time) > 1800:
                                        logger.info(f"Sicherheits-Reset: WP Boost (WW={wp_status.get('WW_Mode')}, HZ={wp_status.get('HZ_Mode')}, HZ-Set={hz_set}C) ohne SW-Anforderung - setze zurueck.")
                                        last_safety_check_time = time.time()

                                    if wp_write_allowed:
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
                        )
                        if (
                            dimplex_sg_live in (11, 13)
                            and not dimplex_owner_active
                            and (time.time() - last_wp_command_time) > 60
                        ):
                            logger.info(
                                "Dimplex SG-Freigabe ohne aktiven Besitzer erkannt "
                                "(SG=%s) - setze auf Gelb zurück.",
                                dimplex_sg_live,
                            )
                            if wp.set_boost(0, None, 0, None):
                                last_wp_command_time = time.time()
                                wp_last_pv_boost_stop_ts = time.time()

                        success = True

                    # Externer Reset Check (Gilt nicht für IDM/Dimplex, da dort nur Freigaben vorgegeben werden)
                    if wp_type not in (1, 5) and wp_status.get('valid') and boost_active and wp_status and (time.time() - last_wp_command_time) > 120:
                        if wp_status.get('WW_Mode') != 1 and wp_status.get('HZ_Mode') != 1:
                            if not is_ww_timer_running:
                                logger.info("Boost-Modus extern deaktiviert. Reset.")
                                boost_active = False
                                deficit_start_time = None
                                price_boost_active = False
                                pre_pause_active = False
                                pv_pause_active = False

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
            # Primaer: wb_pv_budget.json (wird alle 5s frisch geschrieben, inkl. tl_brake)
            # Fallback: storage_manager_state.json energy_score
            free_for_limbs_w = 0
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
            storage_state_name = 'unknown'
            budget_is_fresh = False
            try:
                wb_budget_path = "/var/www/html/ramdisk/wb_pv_budget.json"
                sm_path = "/var/www/html/ramdisk/storage_manager_state.json"
                wb_budget_data = {}
                sm_data = {}

                # wb_pv_budget.json: primaere Budget-Quelle (alle 5s aktualisiert)
                if os.path.exists(wb_budget_path):
                    _age = time.time() - os.path.getmtime(wb_budget_path)
                    if _age < 30:  # Nur wenn frisch (< 30s)
                        with open(wb_budget_path, 'r') as f:
                            wb_budget_data = json.load(f)
                        storage_state_name = wb_budget_data.get('storage_state', 'unknown')
                        es = wb_budget_data.get('energy_score', {})
                        free_for_limbs_w = int(es.get('free_for_limbs_w', 0))
                        must_consume_w   = int(es.get('must_consume_w', 0))
                        consumer_allocations = wb_budget_data.get('consumer_allocations') or es.get('consumer_allocations') or {}
                        if isinstance(consumer_allocations, dict) and 'heatpump' in consumer_allocations:
                            heatpump_budget_w = max(0, int(float(consumer_allocations.get('heatpump', 0) or 0)))
                            free_for_limbs_w = heatpump_budget_w
                        wallbox_phase_transition_active = wallbox_phase_transition_blocks_heatpump_start(
                            wb_budget_data
                        )
                        wallbox_phase_transition_reserved_w = max(
                            0,
                            _safe_int(wb_budget_data.get('wallbox_phase_transition_reserved_w'), 0),
                            _safe_int(wb_budget_data.get('wallbox_phase_transition_requested_w_total'), 0),
                        )
                        wallbox_phase_transition_until_ts = _safe_float(
                            wb_budget_data.get('wallbox_phase_transition_until_ts'),
                            0.0,
                        )
                        heatpump_running_commitment_w = max(
                            0,
                            _safe_int(wb_budget_data.get('heatpump_running_commitment_w'), 0),
                        )
                        if isinstance(wb_budget_data.get('heatpump_pause_request'), dict):
                            heatpump_pause_request = wb_budget_data.get('heatpump_pause_request') or {}
                        budget_is_fresh = True

                # Fallback: storage_manager_state.json (wird seltener geschrieben)
                # Ein frisches 0W-Budget ist ein echtes Stop-Signal und darf
                # nicht durch aeltere storage_manager_state-Werte ueberschrieben werden.
                if (not budget_is_fresh) and os.path.exists(sm_path):
                    with open(sm_path, 'r') as f:
                        sm_data = json.load(f)
                    es = sm_data.get('energy_score', {})
                    free_for_limbs_w = int(es.get('free_for_limbs_w', 0))
                    must_consume_w = int(es.get('must_consume_w', 0))
                    consumer_allocations = sm_data.get('consumer_allocations') or es.get('consumer_allocations') or {}
                    if isinstance(consumer_allocations, dict) and 'heatpump' in consumer_allocations:
                        heatpump_budget_w = max(0, int(float(consumer_allocations.get('heatpump', 0) or 0)))
                        free_for_limbs_w = heatpump_budget_w
                    wallbox_phase_transition_active = wallbox_phase_transition_blocks_heatpump_start(
                        sm_data
                    )
                    wallbox_phase_transition_reserved_w = max(
                        0,
                        _safe_int(sm_data.get('wallbox_phase_transition_reserved_w'), 0),
                        _safe_int(sm_data.get('wallbox_phase_transition_requested_w_total'), 0),
                    )
                    wallbox_phase_transition_until_ts = _safe_float(
                        sm_data.get('wallbox_phase_transition_until_ts'),
                        0.0,
                    )
                    heatpump_running_commitment_w = max(
                        0,
                        _safe_int(sm_data.get('heatpump_running_commitment_w'), 0),
                    )
                    if isinstance(sm_data.get('heatpump_pause_request'), dict):
                        heatpump_pause_request = sm_data.get('heatpump_pause_request') or {}
                    storage_state_name = sm_data.get('state', 'unknown')

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

            except Exception:
                pass

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
                wb_live_session_path = '/var/www/html/logs/wb_live_session.json'
                try:
                    if os.path.exists(wb_live_session_path) and (time.time() - os.path.getmtime(wb_live_session_path)) < 30:
                        with open(wb_live_session_path, 'r') as f:
                            _wls = json.load(f)
                        if _wls.get('source') == 'rscp':
                            _rscp_kwh = _wls.get('session_kwh')
                            if _rscp_kwh is not None and _rscp_kwh >= 0:
                                wb1_session_kwh = _rscp_kwh
                            # car_connected aus RSCP ist zuverlaessiger als wb_locked aus C++ Polling
                            wb1_locked = bool(_wls.get('car_connected', wb1_locked))
                except Exception:
                    pass  # Fallback auf C++ Wert

            wb1_car_id = get_cfg_value(current_config, 'wb1_car_id', 'car1')
            wb2_car_id = get_cfg_value(current_config, 'wb2_car_id', '')

            all_vehicles = []
            try:
                if os.path.exists(VEHICLES_JSON_FILE):
                    with open(VEHICLES_JSON_FILE, "r") as f:
                        v_data = json.load(f)
                        all_vehicles = v_data.get('vehicles', [])
            except: pass

            def soc_source_rule_confirmed(source, rule_confirmed=None):
                if rule_confirmed is True or rule_confirmed == 1 or str(rule_confirmed).lower() == 'true':
                    return True
                text = str(source or '').strip().lower()
                if not text or text in ['simple_view_start_soc', 'config_start_soc', 'configured_wallbox']:
                    return False
                if text.startswith('wallbox_estimated_from_'):
                    return soc_source_rule_confirmed(text[len('wallbox_estimated_from_'):], None)
                if text.startswith('wallbox_estimated'):
                    return False
                if text in ['manual_start_soc', 'manual_soc', 'manual', 'openwb_profile_link', 'openwb_pro_raw', 'openwb_pro_estimated']:
                    return True
                return any(token in text for token in ['mqtt', 'bluelink', 'wallbox', 'openwb', 'vehicle', 'car_soc', 'hyundai', 'kia'])

            def process_car_session(wb_idx, car_id, session_kwh, is_locked, wb_power_w):
                session_file = f"/var/www/html/tmp/car_charge_session_wb{wb_idx}.json"
                if wb_idx == 1: session_file = "/var/www/html/tmp/car_charge_session.json"

                manual_soc_file = f"/var/www/html/ramdisk/manual_soc_wb{wb_idx}.json"
                if wb_idx == 1 and not os.path.exists(manual_soc_file): manual_soc_file = "/var/www/html/tmp/manual_soc.json"

                # Temporäre RSCP Kollisionen beim Regeln können kurzzeitig is_locked=False erzeugen!
                # Daher löschen wir die Session-/Fahrzeugdaten NICHT mehr blindlings weg ("Auto bleibt gesetzt").
                # Wenn das Auto wirklich abgesteckt wurde, fällt session_kwh beim nächsten Anstecken auf 0,
                # was unsere Logik weiter unten ohnehin als neue Session erkennt.
                if not is_locked:
                    return None

                if not car_id or car_id == '': return None

                target_v = next((v for v in all_vehicles if v.get('id') == car_id), None)
                if not target_v and car_id == 'car1' and all_vehicles: target_v = all_vehicles[0]
                soc_raw = target_v.get('soc', 0) if target_v else None
                try:
                    car_soc = float(soc_raw) if soc_raw not in [None, ''] else 0.0
                except ValueError:
                    car_soc = 0.0
                if not target_v:
                    car_soc = None
                car_ts = target_v.get('last_updated_at', 0) if target_v else 0
                car_soc_confirmed = bool(
                    target_v
                    and car_soc is not None
                    and car_soc > 0
                    and soc_source_rule_confirmed(
                        target_v.get('soc_source', target_v.get('source', 'vehicle_soc')),
                        target_v.get('soc_rule_confirmed')
                    )
                )
                if not car_soc_confirmed:
                    car_soc = None


                manual_data = None
                if os.path.exists(manual_soc_file):
                    try:
                        with open(manual_soc_file, "r") as f: manual_data = json.load(f)
                    except: pass
                manual_soc_confirmed = bool(
                    manual_data
                    and soc_source_rule_confirmed(
                        manual_data.get('source', 'manual_start_soc'),
                        manual_data.get('soc_rule_confirmed')
                    )
                )

                sess = {}
                if os.path.exists(session_file):
                    try:
                        with open(session_file, "r") as f: sess = json.load(f)
                    except: pass
                if manual_data and not manual_soc_confirmed and sess.get('is_manual'):
                    try:
                        os.remove(session_file)
                    except Exception:
                        pass
                    return None

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

                manual_ts = manual_data.get('ts', 0) if manual_soc_confirmed else 0
                if manual_ts > sess.get('last_manual_ts', 0):
                    sess = {
                        'start_soc': manual_data.get('soc', 0),
                        'start_kwh': session_kwh or 0,
                        'last_car_ts': car_ts,
                        'last_manual_ts': manual_ts,
                        'car_name': manual_data.get('name', ''),
                        'car_capacity': manual_data.get('capacity', 0.0),
                        'is_manual': True,
                        'soc_source': manual_data.get('source', 'manual_start_soc'),
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
                    sess['soc_source'] = (target_v or {}).get('soc_source', (target_v or {}).get('source', 'vehicle_soc'))
                    sess['soc_rule_confirmed'] = True
                    sess['last_valid_session_kwh'] = session_kwh or 0

                added_kwh = max(0, (session_kwh or 0) - sess.get('start_kwh', 0))
                net_added_kwh = added_kwh * 0.92

                conf_cap = get_cfg_value(current_config, f'wb{wb_idx}_capacity', CAR_CAPACITY)
                if sess.get('car_capacity', 0) > 0: cap = sess['car_capacity']
                elif target_v and target_v.get('capacity'): cap = float(target_v['capacity'])
                else: cap = float(conf_cap)
                if cap <= 0: cap = 72.0

                virtual_soc = sess.get('start_soc', 0) + ((net_added_kwh / cap) * 100.0)
                current_soc = min(100.0, virtual_soc)
                if car_soc is not None and current_soc < car_soc and not sess.get('is_manual'): current_soc = car_soc

                sess['current_virtual_soc'] = round(current_soc, 2)
                sess['car_id'] = car_id
                sess['car_capacity'] = cap
                sess['ts'] = time.time()

                target_soc = float(get_cfg_value(current_config, f'wb{wb_idx}_target_soc', CAR_TARGET_SOC))
                if target_v and target_v.get('target_soc'):
                    car_limit = float(target_v['target_soc'])
                    if car_limit > 0: target_soc = min(target_soc, car_limit)
                sess['target_soc'] = target_soc

                if wb_power_w > 500:
                    needed_kwh = max(0, (target_soc - current_soc) * cap / 100.0)
                    kw = wb_power_w / 1000.0
                    sess['time_to_target_mins'] = int((needed_kwh / kw) * 60) if kw > 0.5 else None
                else:
                    sess['time_to_target_mins'] = None

                write_json_atomic_tolerant(session_file, sess)
                return sess

            processed_wb1 = process_car_session(1, wb1_car_id, wb1_session_kwh, wb1_locked, abs(e3dc.get('wb', 0)))
            processed_wb2 = process_car_session(2, wb2_car_id, wb2_session_kwh, wb2_locked, abs(e3dc.get('wb2', 0)))

            primary_sess = processed_wb1 or processed_wb2
            car_soc = primary_sess['current_virtual_soc'] if primary_sess else (all_vehicles[0].get('soc') if all_vehicles else None)

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
                            target_v = next((v for v in all_vehicles if v.get('id') == wb1_car_id), all_vehicles[0])
                            if not target_v.get('is_plugged_in', True) and (time.time() - last_wakeup_trigger_time) > 60:
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
                    if manual_boost_command.get("action") == "off":
                        if wp_write_allowed:
                            if wp.set_boost(0, None, 0, CONF_WWW):
                                last_wp_command_time = time.time()
                                if not consume_manual_boost_command(manual_boost_command):
                                    logger.info(
                                        "Manueller Boost wurde beendet; ein neuerer Auftrag bleibt zur Verarbeitung liegen."
                                    )
                            else:
                                logger.error(
                                    "Manueller Boost-OFF-Auftrag bleibt offen: Treiber-Release nicht bestätigt."
                                )
                    elif wq_aus < WQ_MIN_TEMP:
                        if wp_write_allowed:
                            logger.warning(f"NOT-AUS (Manuell): WQ Aus zu kalt ({wq_aus}°C).")
                            if wp.set_boost(0, None, 0, CONF_WWW):
                                last_wp_command_time = time.time()
                                consume_manual_boost_command(manual_boost_command)
                    elif soc < MANUAL_BOOST_MIN_SOC:
                        if wp_write_allowed:
                            logger.info(f"Manueller Boost gestoppt: SoC niedrig ({soc}%).")
                            if wp.set_boost(0, None, 0, CONF_WWW):
                                last_wp_command_time = time.time()
                                consume_manual_boost_command(manual_boost_command)
                    elif (time.time() - manual_boost_command.get("mtime", time.time())) > (MANUAL_BOOST_MAX_DURATION * 60):
                        if wp_write_allowed:
                            logger.info("Manueller Boost abgelaufen.")
                            if wp.set_boost(0, None, 0, CONF_WWW):
                                last_wp_command_time = time.time()
                                consume_manual_boost_command(manual_boost_command)
                    else:
                        # Keep-Alive fuer manuellen Boost: Temperaturwerte alle 30s nachschreiben
                        # damit Config-Aenderungen (z.B. www=55) sofort wirken
                        if wp_write_allowed:
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
                if wp.set_boost(0, None, 0, CONF_WWW):
                    last_wp_command_time = time.time()
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
                            free_for_limbs_w=free_for_limbs_w,
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
                                and free_for_limbs_w >= abs(GRID_START_LIMIT)
                            ):
                                if pv_pause_pending_end is None:
                                    pv_pause_pending_end = now
                                elif (now - pv_pause_pending_end).total_seconds() > PV_BOOST_DELAY:
                                    if wp_write_allowed:
                                        logger.info(f"PV-Pause beendet -> Gehirn-Budget bestätigt ({free_for_limbs_w}W über {PV_BOOST_DELAY}s).")
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
                                    if wp_write_allowed:
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
                                        if wp_write_allowed:
                                            logger.info(f"Starte PV-Pause (Prognose > {PV_PAUSE_WATT}W).")
                                            if wp.set_boost(1, PAUSE_SETPOINT_C, 0, CONF_WWW):
                                                last_wp_command_time = time.time()
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

                            if peak_found and wp_write_allowed:
                                pause_reason = str(heatpump_pause_request.get("reason") or f"Prognose > {PV_PAUSE_WATT}W")
                                logger.info(f"Starte {pause_label} ({pause_reason}).")
                                if wp.set_boost(1, PAUSE_SETPOINT_C, 0, CONF_WWW):
                                    last_wp_command_time = time.time()
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
                        if wp_write_allowed:
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
                    observed_price_wp_power_w, observed_price_power_known, _observed_price_accepting = heatpump_power_observation(wp_data)
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

                    if energy_autonomy_allowed and PRICE_BOOST_ENABLE == 1:
                        # COP-Schätzung & thermische Preisanpassung (Sole vs. Luft)
                        wq_ein = wp_data.get('Sole_Ein', wp_data.get('WQ_Eintritt', at))
                        estimated_cop = max(2.0, min(6.0, 3.5 + (wq_ein - 5.0) * 0.1))
                        effective_price_limit = PRICE_LIMIT * (estimated_cop / 3.5)

                        v4_opt_score, v4_billing_price = get_v4_eco_score()

                        if v4_opt_score is not None:
                            # V4 System ist aktiv. Logik erfolgt ausschließlich über den intelligenten Score
                            if current_price <= PRICE_HARD_LIMIT: price_action = "BOOST"
                            elif v4_opt_score >= 80.0: price_action = "BOOST"
                            elif v4_opt_score <= 20.0: price_action = "PAUSE_HIGH_PRICE"
                        elif current_price <= 0:
                            price_action = "BOOST"
                        elif current_price >= PRICE_PAUSE_LIMIT:
                            price_action = "PAUSE_HIGH_PRICE"
                        elif prices: # Legacy PHP Logic
                            gmt = time.gmtime(); now_gmt = gmt.tm_hour + gmt.tm_min / 60.0
                            h_diff = now_gmt - price_start_hour
                            if h_diff < -12: h_diff += 24
                            if h_diff > 36: h_diff -= 24
                            # Placeholder für Nutzer, deren Skript die alte PHP Prices liefert
                            pass
                        is_hard = (current_price <= PRICE_HARD_LIMIT)
                        if wq_aus < WQ_MIN_TEMP: price_action = "NONE"
                        elif daily_boost_counter >= PRICE_MAX_DAILY and price_action == "BOOST" and not is_hard: price_action = "NONE"
                        elif (time.time() - last_pv_boost_time) <= (18 * 3600) and price_action != "NONE" and not is_hard: price_action = "NONE"

                    market_heatpump_release = market_plan_allows("heatpump", current_config)
                    market_heatpump_active = bool(market_heatpump_release.get("allowed"))
                    legacy_price_heatpump_active = price_boost_allows("heatpump")
                    if market_heatpump_active or legacy_price_heatpump_active or predump_heatpump_active:
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
                        or (energy_autonomy_allowed and is_tiered_nt and not boost_active and PRICE_BOOST_ENABLE == 1)
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
                    if price_valid_for_policy and price_value_for_policy >= _safe_float(PRICE_PAUSE_LIMIT, 35.0) and _safe_float(soc, 0.0) <= battery_empty_soc:
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
                        free_for_limbs_w=free_for_limbs_w,
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
                        previous_available_budget_w=free_for_limbs_w,
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
                            free_for_limbs_w=free_for_limbs_w,
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
                            if wp_write_allowed:
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
                            or (
                                energy_autonomy_allowed
                                and is_tiered_nt
                                and not boost_active
                                and PRICE_BOOST_ENABLE == 1
                                and heat_policy_target == heat_policy.TARGET_BOOST
                            )
                        )
                        if (
                            boost_active
                            and not heat_policy_allows_active_heatpump
                            and not (price_boost_active or pre_pause_active or pv_pause_active or predump_heatpump_active)
                            and not os.path.exists(FLAG_FILE)
                        ):
                            ww_below_target, ww_actual_c, ww_target_c = heatpump_ww_temperature_below_target(
                                wp_status,
                                wp_data,
                                None,
                                CONF_WWW,
                            )
                            ww_stop_would_interrupt = bool(
                                wp_write_allowed
                                and wp
                                and heatpump_ww_cycle_running(wp_status, wp_data, ww_requested=True)
                                and ww_below_target
                            )
                            ww_stop_abort_allowed = bool(
                                (e3dc_valid and _safe_float(soc, 0.0) > 0 and _safe_float(soc, 0.0) < max(5.0, _safe_float(MIN_SOC, 80.0) - 5.0))
                                or (_safe_float(grid, 0.0) > 2500.0 and _safe_float(soc, 0.0) <= _safe_float(MIN_SOC, 80.0))
                                or price_action == "PAUSE_HIGH_PRICE"
                                or not AUTO_MODE
                                or not wp_write_allowed
                            )
                            if ww_stop_would_interrupt and not ww_stop_abort_allowed:
                                if (time.time() - last_ww_off_guard_log_time) > 300.0:
                                    logger.warning(
                                        "WP-Schutz: Blockiere unplausibles WW-Off-Kommando "
                                        "(Verdichter läuft, Temp %s°C < Ziel %s°C, kein Notstop/Preis-Schmerzgrenze).",
                                        ww_actual_c,
                                        ww_target_c,
                                    )
                                    last_ww_off_guard_log_time = time.time()
                                heat_policy_price_gate_reason = "WW-Off durch Verdichterschutz blockiert"
                            else:
                                if wp_write_allowed and wp:
                                    logger.info(
                                        "Heat Policy Runtime stoppt PV-/Budget-Boost: Ziel=%s Grund=%s",
                                        heat_policy_target,
                                        heat_policy_decision.block_reason,
                                    )
                                    if wp.set_boost(0, None, 0, CONF_WWW):
                                        last_wp_command_time = time.time()
                                        wp_last_pv_boost_stop_ts = time.time()
                                boost_active = False
                                deficit_start_time = None
                                pv_boost_pending_start = None
                    else:
                        heat_policy_price_gate_reason = "Shadow only - heat_policy_runtime_enable=0"
                        price_heatpump_start_requested = bool(
                            price_action == "BOOST"
                            or (
                                energy_autonomy_allowed
                                and is_tiered_nt
                                and not boost_active
                                and PRICE_BOOST_ENABLE == 1
                            )
                        )

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
                            if wp_write_allowed:
                                reason = "Preis-Pause (Vorlauf)" if price_action == "PAUSE" else f"Hochpreis-Pause (> {PRICE_PAUSE_LIMIT}ct)"
                                logger.info(f"Start {reason}.")
                                if wp.set_boost(1, PAUSE_SETPOINT_C, 0, CONF_WWW):
                                    last_wp_command_time = time.time()
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
                            if wp_write_allowed:
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
                                    last_wp_command_time = time.time()
                                    wp_last_pv_boost_start_ts = time.time()
                                    if predump_heatpump_active:
                                        if predump_heatpump_started_ts <= 0 or not predump_heatpump_hold_active:
                                            predump_heatpump_started_ts = time.time()
                                        predump_heatpump_hold_until = max(
                                            predump_heatpump_hold_until,
                                            time.time() + (PREDUMP_HEATPUMP_MIN_RUNTIME_MIN * 60.0),
                                        )
                                        cycle_actions.append({
                                            "action": "boost_start",
                                            "owner": "predump_heatpump",
                                            "min_runtime_s": int(PREDUMP_HEATPUMP_MIN_RUNTIME_MIN * 60),
                                            "budget_w": int(free_for_limbs_w),
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
                                            "price_ct": current_price,
                                            "market_plan_action": market_heatpump_release.get("action"),
                                        })
                                    price_boost_active = True; pre_pause_active = False; boost_active = True
                        if price_action == "BOOST" and not predump_heatpump_active:
                             daily_boost_counter += 0.5 # Nur bei dynamischem Boost das tägliche Limit zählen

                    elif (price_boost_active or pre_pause_active) and price_action == "NONE" and not is_tiered_nt:
                        emergency_stop = (e3dc_valid and soc > 0 and soc < max(5, MIN_SOC - 5)) or (grid > 2500 and free_for_limbs_w < 0)
                        if price_boost_active and not predump_heatpump_active:
                            price_heatpump_stop_block_remaining_s = heatpump_takt_stop_block(
                                WP_TAKT_PROTECT,
                                wp_last_pv_boost_start_ts,
                                WP_MIN_RUNTIME_MIN,
                                emergency_stop=emergency_stop,
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
                            logger.info("Ende Preis-Steuerung.")
                            if wp.set_boost(0, None, 0, CONF_WWW):
                                last_wp_command_time = time.time()
                                wp_last_pv_boost_stop_ts = time.time()
                                if predump_heatpump_started_ts:
                                    cycle_actions.append({
                                        "action": "boost_stop",
                                        "owner": "predump_heatpump",
                                        "reason": (
                                            "temperatur_erreicht" if predump_heatpump_targets_reached
                                            else ("wq_schutz" if predump_heatpump_protect_block else "haltezeit_abgelaufen")
                                        ),
                                    })
                                else:
                                    cycle_actions.append({"action": "boost_stop", "owner": "price_heatpump"})
                                predump_heatpump_started_ts = 0.0
                                predump_heatpump_hold_until = 0.0
                                price_boost_active = False; pre_pause_active = False; boost_active = False

                    # LAUFENDE ÜBERWACHUNG (PV-Boost)
                    if boost_active and (not price_boost_active) and (not pre_pause_active) and (not pv_pause_active):
                        # PV-Boost aktiv: Wir haben den Speicherstart bereits passiert
                        last_pv_boost_time = time.time()

                        # Dynamische Parameteranpassung Sommer/Winter während laufendem Boost
                        if wp_write_allowed and wp and (time.time() - last_wp_command_time) > 25:
                            if at_mittel > HEIZGRENZE_TEMP: wp.set_boost(0, None, 1, CONF_WWS, cooling_boost_mode, CONF_KHL, wp_data=wp_data)
                            else: wp.set_boost(1, CONF_HZ, 1, CONF_WWW, wp_data=wp_data)
                            last_wp_command_time = time.time()

                        # DEFIZIT-Check (Strenge Version):
                        # Wir stoppen, wenn wir massiv importieren (grid > 100) UND
                        # entweder der Akku nicht mehr massiv lädt (bat < 500)
                        # ODER das Gehirn sagt, dass kein Budget mehr da ist.
                        transition_protects_running_heatpump = bool(
                            wallbox_phase_transition_active
                            and (boost_active or wp_compressor_running_now)
                        )
                        independent_safety_stop = bool(
                            (e3dc_valid and soc > 0 and soc < max(5, MIN_SOC - 5))
                            or grid > 2500
                        )
                        is_strict_deficit = bool(
                            (grid > 200)
                            and (free_for_limbs_w < 500)
                            and (not transition_protects_running_heatpump or independent_safety_stop)
                        )
                        is_budget_deficit = (not transition_protects_running_heatpump) and heatpump_budget_deficit(
                            storage_manager_owns_energy,
                            free_for_limbs_w,
                            GRID_START_LIMIT,
                        )
                        if transition_protects_running_heatpump and not independent_safety_stop:
                            is_deficit = False
                            deficit_start_time = None

                        if is_deficit or is_strict_deficit or is_budget_deficit:
                            if deficit_start_time is None:
                                deficit_start_time = now
                                reason_str = "Gehirn-Budget" if is_budget_deficit else "Standard"
                                logger.info(f"Defizit erkannt ({reason_str} | Netz: {grid:.0f}W, Free: {free_for_limbs_w}W). Timer start.")
                            elif (now - deficit_start_time).total_seconds() > (STOP_DELAY_MINUTES * 60):
                                min_runtime_left_s = 0.0
                                if WP_TAKT_PROTECT and wp_last_pv_boost_start_ts and WP_MIN_RUNTIME_MIN > 0:
                                    min_runtime_left_s = (WP_MIN_RUNTIME_MIN * 60) - (time.time() - wp_last_pv_boost_start_ts)
                                emergency_stop = (e3dc_valid and soc > 0 and soc < max(5, MIN_SOC - 5)) or (grid > 2500 and free_for_limbs_w < 0)
                                if min_runtime_left_s > 0 and not emergency_stop:
                                    if (time.time() - last_wp_takt_log_time) > 300:
                                        logger.info(f"WP-Taktschutz: Stop PV-Boost noch {min_runtime_left_s/60:.1f} Min verzögert (Mindestlaufzeit {WP_MIN_RUNTIME_MIN:.0f} Min).")
                                        last_wp_takt_log_time = time.time()
                                elif wp_write_allowed:
                                    logger.info(f"Stop PV-Boost (Defizit | Netz: {grid:.0f}W, Free: {free_for_limbs_w}W).")
                                    if wp.set_boost(0, None, 0, CONF_WWW):
                                        last_wp_command_time = time.time()
                                        wp_last_pv_boost_stop_ts = time.time()
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
                                and heat_policy_decision is not None
                                and heat_policy_decision.target_state == heat_policy.TARGET_PV_SURPLUS
                            )
                            legacy_allows_pv_start = bool(
                                (not heat_policy_runtime_enabled)
                                and heatpump_budget_allows_start(
                                    storage_manager_owns_energy,
                                    free_for_limbs_w,
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
                                    )
                                    pv_boost_retry_not_before_ts = _safe_float(
                                        pv_boost_last_outcome.get("retry_not_before_ts"),
                                        pv_boost_retry_not_before_ts,
                                    )
                                    if pv_boost_last_outcome.get("confirmed"):
                                        logger.info(
                                            "Start PV-Boost bestätigt (Gehirn-Budget: %sW >= %sW; "
                                            "command_sent=%s, readback_confirmed=%s).",
                                            free_for_limbs_w,
                                            abs(GRID_START_LIMIT),
                                            pv_boost_last_outcome.get("command_sent"),
                                            pv_boost_last_outcome.get("readback_confirmed"),
                                        )
                                        last_wp_command_time = time.time()
                                        wp_last_pv_boost_start_ts = time.time()
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
                            free_for_limbs_w=heatpump_surplus_budget_during_phase_transition(
                                free_for_limbs_w,
                                wallbox_phase_transition_active,
                                heatpump_running=bool(boost_active or wp_compressor_running_now),
                                heatpump_running_commitment_w=max(
                                    heatpump_running_commitment_w,
                                    observed_wp_power_w,
                                ),
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
                    manual_ww_active = False
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

                        active_boost_type = (boost_active and not (pre_pause_active or pv_pause_active))
                        boost_ww_temp = CONF_WWS if at_mittel > HEIZGRENZE_TEMP else CONF_WWW
                        force_ww = (active_boost_type or manual_ww_active)
                        force_pause = (pre_pause_active or pv_pause_active)
                        source_recovery_owns_pause = bool(
                            pv_pause_active
                            and pv_pause_owner == "source_recovery_heatpump"
                        )

                        if force_pause and source_recovery_owns_pause:
                            target_ww_mode = None
                            target_ww_temp = None
                        elif force_pause:
                            target_ww_mode = 0  # Externe SHI-Beeinflussung zurückgeben
                            target_ww_temp = CONF_WWW
                        elif force_ww:
                            target_ww_mode = 1
                            target_ww_temp = boost_ww_temp
                        elif WW_TIMER_ENABLE:
                            target_ww_mode = 1
                            if WW_VON <= WW_BIS:
                                in_ww = (WW_VON <= current_hour_decimal < WW_BIS)
                            else:
                                in_ww = (current_hour_decimal >= WW_VON or current_hour_decimal < WW_BIS)
                            target_ww_temp = WW_NORMAL if in_ww else WW_ECO

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
                            or price_action == "PAUSE_HIGH_PRICE"
                            or not AUTO_MODE
                            or not wp_write_allowed
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
                                or price_action == "PAUSE_HIGH_PRICE"
                                or not AUTO_MODE
                                or not wp_write_allowed
                            )
                            if (
                                heatpump_ww_cycle_running(wp_status, wp_data, ww_requested=True)
                                and ww_below_target
                                and not ww_off_abort_allowed
                            ):
                                if (time.time() - last_ww_off_guard_log_time) > 300.0:
                                    logger.warning(
                                        "WP-Schutz: Blockiere unplausibles WW-Off-Kommando "
                                        "(Verdichter läuft, Temp %s°C < Ziel %s°C, kein Notstop/Preis-Schmerzgrenze).",
                                        ww_actual_c,
                                        ww_target_c,
                                    )
                                    last_ww_off_guard_log_time = time.time()
                                send_ww_mode = None
                                ww_update_reason = None

                        if ww_update_reason is not None and send_ww_mode is not None:
                            if wp.write_ww_boost(send_ww_mode, send_ww_temp):
                                wp.last_ww_mode = send_ww_mode
                                wp.last_ww_temp = send_ww_temp
                                wp.last_ww_cmd_time = time.time()
                                if ww_update_reason == "blind_heartbeat":
                                    logger.debug(f"WW Blind-Heartbeat: Mode={send_ww_mode}, Temp={send_ww_temp}")
                                else:
                                    logger.info(f"WW Timer/Boost Set: Mode={send_ww_mode}, Temp={send_ww_temp} ({ww_update_reason})")
                            else:
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
                "market_plan_action": market_heatpump_release.get("action"),
                "market_plan_reason": market_heatpump_release.get("reason"),
                "legacy_price_heatpump_active": bool(legacy_price_heatpump_active),
                "pv_pause_active": pv_pause_active, "success": success, "error": wp_error_msg,
                "pv_pause_owner": pv_pause_owner,
                "pv_pause_start_time": pv_pause_start_time,
                "free_for_limbs_w": free_for_limbs_w, "storage_state": storage_state_name,
                "heatpump_budget_w": heatpump_budget_w,
                "consumer_allocations": consumer_allocations,
                "wallbox_phase_transition_active": wallbox_phase_transition_active,
                "wallbox_phase_transition_reserved_w": wallbox_phase_transition_reserved_w,
                "wallbox_phase_transition_until_ts": wallbox_phase_transition_until_ts,
                "heatpump_running_commitment_w": heatpump_running_commitment_w,
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
            write_json_atomic_tolerant(ENERGY_STATE_FILE, json_export, mode=0o664, warn_label="Energy-Manager-Statusdatei")
            write_json_atomic_tolerant(HEAT_POLICY_LATEST_PATH, heat_policy_export, mode=0o664, warn_label="Heat-Policy-Latest")

            if time.time() - last_history_write >= 60:
                append_luxtronik_history(json_export, now)
                last_history_write = time.time()

            decision_state = "beobachtet"
            decision_reason = "Keine aktive Waermefreigabe"
            observed_wp_power_w, heatpump_power_known, heatpump_accepting_power = heatpump_power_observation(wp_data)
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
                    "budget_offered": bool(boost_active or price_boost_active or predump_heatpump_active),
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

                # --- Bereinige alte RAW-Dateien nach 30 Tagen (Speicherplatz sparen) ---
                try:
                    cutoff_30d = time.time() - (30 * 86400)
                    for file_name in os.listdir(BACKUP_DIR):
                        if file_name.startswith("luxtronik_") and file_name.endswith(".json"):
                            full_path = os.path.join(BACKUP_DIR, file_name)
                            if os.path.isfile(full_path) and os.path.getmtime(full_path) < cutoff_30d:
                                os.remove(full_path)
                except Exception as cleanup_err:
                    logger.warning(f"Fehler beim Aufräumen alter Luxtronik-Archive: {cleanup_err}")

                daily_boost_counter = 0
                heat_policy_boost_delivered_kwh = 0.0
                heat_policy_last_energy_ts = 0.0
                last_day = now.day

                # Cleanup: Lösche Archiv-Dateien älter als 7 Tage
                try:
                    cutoff_time = time.time() - (7 * 86400)
                    for f in os.listdir(BACKUP_DIR):
                        if f.startswith("luxtronik_") and f.endswith(".json"):
                            f_path = os.path.join(BACKUP_DIR, f)
                            if os.path.getmtime(f_path) < cutoff_time:
                                os.remove(f_path)
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
