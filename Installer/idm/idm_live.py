#!/usr/bin/env python3
from pymodbus.client import ModbusTcpClient
import struct
import time
import json
import os
import math  # WICHTIG: Für den NaN-Check!
import signal
import sys

INSTALLER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if INSTALLER_DIR not in sys.path:
    sys.path.insert(0, INSTALLER_DIR)
from runtime_logging import configure_service_logger

_stop = False
def _sig(sig, _):
    global _stop
    _stop = True
    log_event("[!] SIGTERM empfangen, beende idm_live sauber...", key="sigterm", interval_s=0)
    sys.exit(0)

signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT,  _sig)

# Konfiguration
RAMDISK_FILE = "/var/www/html/ramdisk/waermepumpe.json"
LOG_FILE = "/var/www/html/logs/idm_live.log"
CONFIG_PATHS = [
    "/var/www/html/data/e3dc.config.txt",
    os.path.join(os.path.dirname(INSTALLER_DIR), "e3dc.config.txt"),
    os.path.join(INSTALLER_DIR, "luxtronik", "e3dc.config.txt"),
]
_last_log_by_key = {}
_event_logger = configure_service_logger(
    "IDMLive",
    log_path=LOG_FILE,
    max_bytes=1024 * 1024,
    backup_count=2,
    quiet_interval_s=0.0,
    warning_min_interval_s=0.0,
)

def log_event(message, key=None, interval_s=300):
    """Log to systemd stdout and to the module log without flooding."""
    now = time.time()
    if key:
        last = _last_log_by_key.get(key, 0.0)
        if now - last < interval_s:
            return
        _last_log_by_key[key] = now
    _event_logger.info(str(message), extra={"e3dc_no_throttle": True})

def _safe_config_int(value, default):
    try:
        if value in (None, "", "none", "null"):
            return int(default)
        return int(float(str(value).strip()))
    except Exception:
        return int(default)

def _valid_ip(value):
    return str(value or "").strip() not in ("", "0", "0.0.0.0", "None", "none", "null")

def _resolve_idm_target(ip, port, wp_type):
    ip = str(ip or "0.0.0.0").strip()
    port = _safe_config_int(port, 502)
    wp_type = _safe_config_int(wp_type, 0)
    if _valid_ip(ip) and wp_type == 1:
        return ip, port
    return "0.0.0.0", 502

# IDM Register (Bestätigt durch Parameterliste)
REGISTER_MAP = {
    1000: ("Außentemperatur", "FLOAT"),
    1002: ("Außentemperatur_Mittel", "FLOAT"),
    1010: ("Kaeltespeicher_Ist", "FLOAT"),
    1012: ("Warmwasser unten", "FLOAT"),
    1014: ("Warmwasser-Ist", "FLOAT"),
    1032: ("Warmwasser-Soll", "UCHAR"),
    1050: ("Vorlauf_Ist", "FLOAT"),
    1052: ("Ruecklauf_Ist", "FLOAT"),
    1060: ("Zuluft", "FLOAT"),
    1378: ("Vorlauf_Soll", "FLOAT"),
    1090: ("Betriebszustand_ID", "UCHAR"),
    1100: ("Verdichter", "UCHAR"),
    1750: ("Wärmemenge Heizen", "FLOAT"),
    1752: ("Wärmemenge Warmwasser", "FLOAT"),
    1790: ("Wärmemenge Gesamt", "FLOAT"),
    4122: ("Leistungsaufnahme", "FLOAT"),
    4126: ("Heizleistung Ist", "FLOAT"),
    4128: ("Waermemenge_Gesamt_Kum", "FLOAT"),  # Bestaetigt: Kumulierte Gesamtwaermemenge
}

def load_wp_config():
    """Lädt die IDM IP und den Port aus der V4 JSON oder Legacy Config (nur aktiv wenn wp_type=1)."""
    ip = "0.0.0.0"
    port = 502
    wp_type = 0

    # 1. V4 JSON check
    v4_path = "/var/www/html/data/e3dc_v4.json"
    if os.path.exists(v4_path):
        try:
            with open(v4_path, "r", encoding="utf-8") as f:
                v4_cfg = json.load(f)
                ip = str(v4_cfg.get("idm_ip") or ip).strip()
                port = _safe_config_int(v4_cfg.get("idm_port"), port)
                wp_type = _safe_config_int(v4_cfg.get("wp_type"), wp_type)
        except Exception as exc:
            log_event(f"V4-Konfiguration konnte nicht vollständig gelesen werden: {exc}", key="config_v4_error")

    # 2. Legacy Fallback (txt)
    for p in CONFIG_PATHS:
        if os.path.exists(p) and not _valid_ip(ip):
            try:
                with open(p, "r") as f:
                    for line in f:
                        stripped = line.strip().lower()
                        if stripped.startswith("idm_ip"):
                            ip = line.split("=")[1].strip()
                        elif stripped.startswith("idm_port"):
                            port = _safe_config_int(line.split("=")[1].strip(), port)
                        elif stripped.startswith("wp_type"):
                            wp_type = _safe_config_int(line.split("=")[1].strip(), wp_type)
            except Exception as exc:
                log_event(f"Legacy-Konfiguration {p} konnte nicht gelesen werden: {exc}", key=f"config_legacy_error:{p}")

    return _resolve_idm_target(ip, port, wp_type)

def get_status_text(id_val):
    """Mappt IDM Betriebsart auf Luxtronik-ähnliche Texte für den Energy Manager."""
    if id_val == 1:   return "Heizen"
    if id_val == 4:   return "Warmwasser"
    if id_val == 2:   return "Kühlen"
    if id_val == 8:   return "Abtauen"
    if id_val == 0:   return "Aus"
    return "Standby"

def read_idm(client):
    """Liest alle definierten Register aus der IDM Wärmepumpe."""
    data = {}
    success_count = 0
    last_error = ""
    for addr, (label, dtype) in REGISTER_MAP.items():
        reg_count = 2 if dtype == "FLOAT" else 1

        try:
            result = client.read_holding_registers(address=addr, count=reg_count)

            if result.isError():
                err_msg = f"Modbus-Fehler an Adr {addr} ({label}): {result}"
                log_event(err_msg, key=f"modbus_error:{addr}", interval_s=60)
                last_error = err_msg
                break # Wenn ein Fehler auftritt, sofort abbrechen (Socket wahrscheinlich tot)

            regs = result.registers
            if dtype == "FLOAT" and len(regs) == 2:
                # IDM Magic: Low-Word (regs[0]) VOR High-Word (regs[1])
                packed = struct.pack('>HH', regs[1], regs[0])
                val = struct.unpack('>f', packed)[0]

                # Check ob die IDM einen ungültigen Sensor-Wert (NaN / Inf) sendet
                if math.isnan(val) or math.isinf(val):
                    log_event(f"Warnung: Sensor {label} (Adr {addr}) liefert NaN/Inf.", key=f"nan:{addr}", interval_s=300)
                    data[label] = None # JSON-kompatibles null einfügen!
                else:
                    data[label] = round(val, 2)
                    success_count += 1

            elif dtype == "UCHAR" and len(regs) >= 1:
                val = regs[0]
                if label == "Betriebszustand_ID":
                    data["Betriebszustand"] = get_status_text(val)
                    data["Modus Heizen"] = "Ein" if val == 1 else "Aus"
                    data["Modus Warmw."] = "Ein" if val == 4 else "Aus"
                else:
                    data[label] = val
                success_count += 1

        except Exception as e:
            err_msg = f"Python Fehler an Adr {addr} ({label}): {e}"
            log_event(err_msg, key=f"python_error:{addr}", interval_s=60)
            last_error = err_msg
            break # Bei Exception (Broken Pipe etc.) den aktuellen Zyklus komplett abbrechen!

    data["last_error"] = last_error

    # IDM liefert Wärmemenge Gesamt (1790) oft als 0.0, wir rechnen es zur Sicherheit einfach zusammen
    wm_hz = data.get("Wärmemenge Heizen") or 0.0
    wm_ww = data.get("Wärmemenge Warmwasser") or 0.0
    wm_ges = data.get("Wärmemenge Gesamt") or 0.0
    if wm_ges == 0.0 and (wm_hz > 0 or wm_ww > 0):
        data["Wärmemenge Gesamt"] = round(wm_hz + wm_ww, 2)

    return data, success_count

def save_to_ramdisk(data):
    """Speichert die Daten threadsicher und mit Rechten in der Ramdisk."""
    if not isinstance(data, dict) or not data:
        log_event("Leerer iDM-Datensatz verworfen; waermepumpe.json bleibt unverändert.", key="empty_payload")
        return False
    data["source"] = "idm_live"
    data['ts'] = time.strftime('%Y-%m-%d %H:%M:%S')

    tmp_file = RAMDISK_FILE + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        os.chmod(tmp_file, 0o664)
        try:
            import grp
            gid = grp.getgrnam("www-data").gr_gid
            os.chown(tmp_file, -1, gid)
        except: pass

        os.replace(tmp_file, RAMDISK_FILE)
        return True
    except Exception as e:
        log_event(f"Fehler beim Schreiben der Ramdisk: {e}", key="write_error", interval_s=60)
        return False

def main():
    ip, port = load_wp_config()
    if ip == "0.0.0.0":
        log_event("[!] IDM IP nicht in e3dc_v4.json gefunden (idm_ip=...).", key="ip_missing", interval_s=60)
        save_to_ramdisk({"success": False, "error": "IP nicht konfiguriert", "idm_ip": "???"})
        return

    log_event(f"Starte IDM Modbus-Live-Stream zu {ip}:{port} (Slave 1)...", key="startup", interval_s=60)

    while True:
        try:
            # WICHTIG: Erzeuge das ModbusTcpClient Objekt jede Runde neu!
            # IDM schließt Verbindungen sofort, und pymodbus cacht sonst tote Sockets.
            client = ModbusTcpClient(ip, port=port)

            connected = False
            for _ in range(3):
                if _stop: break
                if client.connect():
                    connected = True
                    break
                time.sleep(1)

            if _stop or not connected:
                log_event("Verbindung fehlgeschlagen...", key="connect_failed", interval_s=60)
                save_to_ramdisk({"success": False, "error": "Verbindung fehlgeschlagen", "idm_ip": ip})
                time.sleep(15)
                continue

            wp_data, count = read_idm(client)
            client.close() # IMMER schließen, damit andere Clients (IDM App, energy_manager) dran kommen!

            if count > 0:
                wp_data["success"] = True
                wp_data["idm_ip"] = ip
                save_to_ramdisk(wp_data)
                log_event(f"iDM Livewerte aktualisiert ({count} Register).", key="success", interval_s=900)
            else:
                err_msg = wp_data.get("last_error", "Ungültige Register-Adressen")
                log_event(f"Keine gültigen Register: {err_msg}", key="no_registers", interval_s=60)
                save_to_ramdisk({"success": False, "error": f"Keine gültigen Register ({err_msg})", "idm_ip": ip})

        except Exception as e:
            log_event(f"Fehler im Loop: {e}", key="loop_error", interval_s=60)
            save_to_ramdisk({"success": False, "error": str(e), "idm_ip": ip})
            try: client.close()
            except: pass
            time.sleep(15)
            continue

        # V4 Sleep blockiert nicht durchgehend (bessere SIGTERM Reaktion)
        for _ in range(15):
            if _stop: break
            time.sleep(1)

        if _stop:
            break

if __name__ == "__main__":
    main()
