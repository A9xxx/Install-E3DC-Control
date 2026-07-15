#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E3DC-Control High Availability (HA) Manager
Überwacht den Master/Slave Status, synchronisiert Daten und übernimmt im Notfall.
"""

import os
import time
import subprocess
import json
import glob
import ipaddress
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import pwd
import grp
import socket
from pathlib import Path

from config_secret_permissions import config_secret_dir_mode_text, config_secret_file_mode, config_secret_file_mode_text
from quiet_logging import install_quiet_info_filter

PATHS_FILE = "/var/www/html/e3dc_paths.json"
_paths_warning_logged = False


def read_paths_config():
    """Read e3dc_paths.json defensively; updates can briefly leave it empty."""
    global _paths_warning_logged
    if not os.path.exists(PATHS_FILE):
        return {}
    try:
        with open(PATHS_FILE, "r", encoding="utf-8-sig") as f:
            raw = f.read().strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        if "logger" in globals() and not _paths_warning_logged:
            logger.warning(f"{PATHS_FILE} nicht lesbar, nutze HA-Fallbackwerte: {exc}")
            _paths_warning_logged = True
        return {}


def _validated_install_path(raw_path, default="/home/pi/E3DC-Control"):
    """Normalisiere den Installationspfad aus der Web-Config defensiv."""
    try:
        candidate = Path(str(raw_path or "").strip()).expanduser()
        if not candidate.is_absolute():
            raise ValueError("install_path ist nicht absolut")
        resolved = candidate.resolve(strict=False)
        allowed_roots = [Path("/home"), Path("/opt"), Path("/srv"), Path("/var/www")]
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            raise ValueError(f"install_path außerhalb erlaubter Basis: {resolved}")
        return str(resolved).rstrip("/")
    except Exception as exc:
        if "logger" in globals():
            logger.warning(f"Ungültiger install_path in {PATHS_FILE}: {raw_path!r} ({exc}); nutze {default}")
        return default


def validate_peer_ip(peer_ip):
    """Akzeptiert nur numerische IPv4/IPv6-Adressen für HA-Remote-Ziele."""
    try:
        value = str(peer_ip or "").strip()
        if not value:
            return ""
        return str(ipaddress.ip_address(value))
    except ValueError:
        return ""


def _rsync_remote(user, host_ip, path):
    host = f"[{host_ip}]" if ":" in str(host_ip) else str(host_ip)
    return f"{user}@{host}:{path}"


# Pfade
INSTALL_PATH = "/home/pi/E3DC-Control"
try:
    p_data = read_paths_config()
    if 'install_path' in p_data:
        INSTALL_PATH = _validated_install_path(p_data['install_path'])
except: pass

CONFIG_PATH = "/var/www/html/data/e3dc_v4.json"
LOG_DIR = "/var/www/html/logs"
NOTIFY_SCRIPT = "/usr/local/bin/boot_notify.sh"

LEGACY_E3DC_SERVICE = "e3dc.service"
NATIVE_LIVE_SERVICE = "e3dc-live.service"

HA_LOCAL_CONFIG_KEYS = {
    "ha_mode",
    "ha_peer_ip",
    "telegram_device_name",
    "install_path",
    "install_user",
    "home_dir",
    "venv_name",
    "venv_path",
}
SECRET_CONFIG_KEY_PARTS = (
    "password",
    "passwd",
    "passwort",
    "token",
    "secret",
    "api_key",
    "apikey",
    "aes",
    "private",
)
SECRET_CONFIG_EXACT_KEYS = {
    "rscp_pw",
    "rscp_password",
    "telegram_chat_id",
}


def catalog_managed_services():
    """Liefert alle katalogisierten Dienste, die im HA-Standby exklusiv bleiben."""
    fallback = [
        "e3dc-live.service",
        "e3dc-storage-manager.service",
        "e3dc-storage-simulator.service",
        "e3dc-epex-manager.service",
        "e3dc-weather-manager.service",
        "e3dc-wallbox-manager.service",
        "energy_manager.service",
        "e3dc-idm-live.service",
        "e3dc-lux-live.service",
        "e3dc-stiebel-live.service",
        "e3dc-dimplex-live.service",
        "e3dc-heizstab.service",
        "e3dc-climate-live.service",
        "e3dc-climate-control.service",
        "e3dc-matter-bridge.service",
        "e3dc-bluelink.service",
        "e3dc-mqtt-hub.service",
        "e3dc-notifier.service",
        "e3dc-websocket.service",
        "e3dc-shadow-sync.service",
    ]
    try:
        import sys as _sys
        installer_dir = os.path.dirname(os.path.abspath(__file__))
        if installer_dir not in _sys.path:
            _sys.path.insert(0, installer_dir)
        from service_catalog import allowed_services
        units = list(allowed_services())
    except Exception:
        units = fallback

    services = [LEGACY_E3DC_SERVICE]
    for unit in units:
        unit_name = str(unit or "").strip()
        if not unit_name:
            continue
        if not unit_name.endswith(".service"):
            unit_name += ".service"
        if unit_name in {LEGACY_E3DC_SERVICE, "e3dc-ha.service"}:
            continue
        if unit_name not in services:
            services.append(unit_name)
    return services


# Dienste, die im HA-Standby nicht laufen duerfen.
#
# Ein Slave im Standby muss alle Steuer-, Schreib-, Integrations- und
# Simulationsdienste aus dem zentralen Katalog hart gestoppt halten. Der HA-
# Manager selbst bleibt ausgenommen, damit Failover/Fallback weiter arbeitet.
MANAGED_SERVICES = catalog_managed_services()

def setup_logging():
    """Initialisiert das rotierende Logfile für den HA Manager."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, "ha_manager.log")
    logger = logging.getLogger("HAManager")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(log_file, maxBytes=1024*1024, backupCount=2, encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s', datefmt='%d.%m %H:%M:%S')
    handler.setFormatter(formatter)
    if not logger.handlers:
        logger.addHandler(handler)
    install_quiet_info_filter(
        logger,
        min_interval_s=900.0,
        warning_min_interval_s=30.0,
        warning_max_interval_s=3600.0,
    )
    try:
        os.chmod(log_file, 0o664)
        st = os.stat(LOG_DIR)
        os.chown(log_file, st.st_uid, st.st_gid)
    except: pass
    return logger

logger = setup_logging()

def set_web_permissions(filepath, data=None):
    """Setzt die Dateirechte auf install_user:www-data."""
    try:
        base = os.path.basename(str(filepath))
        mode = config_secret_file_mode(data) if "e3dc_v4.json" in base else 0o664
        os.chmod(filepath, mode)
        install_user = read_paths_config().get('install_user', 'pi')
        uid = pwd.getpwnam(install_user).pw_uid
        gid = grp.getgrnam("www-data").gr_gid
        os.chown(filepath, uid, gid)
    except Exception as e:
        logger.error(f"Konnte Rechte für {filepath} nicht setzen: {e}")

def is_secret_config_key(key):
    """Erkennt Config-Schlüssel, deren Werte nicht zwischen HA-Knoten wandern."""
    normalized = str(key or "").strip().lower()
    if not normalized:
        return False
    if normalized in SECRET_CONFIG_EXACT_KEYS:
        return True
    if normalized.endswith("_pass") or normalized == "pass":
        return True
    return any(part in normalized for part in SECRET_CONFIG_KEY_PARTS)

def ha_sync_config_payload(config_data):
    """Liefert die Master-Config ohne lokale Zugangsdaten für HA-Sync-Artefakte."""
    if not isinstance(config_data, dict):
        return {}
    return {
        key: value
        for key, value in config_data.items()
        if not is_secret_config_key(key)
    }

def load_config():
    """Liest die HA-Parameter aus der e3dc_v4.json (Single Source of Truth)."""
    config = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for k, v in data.items():
                    config[str(k).strip().lower()] = str(v).strip()
        except Exception as e:
            logger.error(f"Fehler beim Laden der Config ({CONFIG_PATH}): {e}")
    return config

def get_local_ips():
    """Liefert lokale IPs, damit HA niemals auf sich selbst synchronisiert."""
    ips = {"127.0.0.1", "::1", "localhost"}
    try:
        hostname = socket.gethostname()
        ips.add(hostname)
        for info in socket.getaddrinfo(hostname, None):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["hostname", "-I"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        for ip in (result.stdout or "").split():
            ips.add(ip.strip())
    except Exception:
        pass
    return {ip for ip in ips if ip}

def peer_points_to_self(peer_ip):
    """True, wenn die HA-Peer-IP auf diesen Pi zeigt."""
    normalized = validate_peer_ip(peer_ip)
    return bool(normalized) and normalized in get_local_ips()

def send_telegram(msg):
    """Sendet Telegram-Nachrichten via boot_notify.sh (Watchdog)."""
    if os.path.exists(NOTIFY_SCRIPT):
        try: subprocess.run([NOTIFY_SCRIPT, msg], timeout=10)
        except Exception: pass
    logger.info(f"Benachrichtigung: {msg}")

def is_host_online(ip):
    """Prüft per Ping, ob der Partner erreichbar ist."""
    ip = validate_peer_ip(ip)
    if not ip:
        return False
    res = subprocess.run(["ping", "-c", "1", "-W", "2", ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return res.returncode == 0

def write_status(mode, state, peer_online, last_sync=0):
    """Schreibt den aktuellen HA-Status für die Web-Oberfläche in die Ramdisk."""
    try:
        status_data = {
            "mode": mode,
            "state": state,
            "peer_online": peer_online,
            "last_sync": last_sync,
            "ts": int(time.time())
        }
        tmp_file = "/var/www/html/ramdisk/ha_status.tmp"
        with open(tmp_file, "w") as f:
            json.dump(status_data, f)
        set_web_permissions(tmp_file)
        os.replace(tmp_file, "/var/www/html/ramdisk/ha_status.json")
    except Exception as e:
        logger.error(f"Fehler beim Schreiben des HA-Status: {e}")

def merge_config(src, dest):
    """
    Übernimmt die Konfiguration vom Master (src) auf den Slave (dest),
    behält aber Cluster-Parameter und Zugangsdaten des Slaves lokal.
    """
    slave_data = {}

    # 1. Lokale HA-Parameter und Secrets des Slaves retten
    if os.path.exists(dest):
        try:
            with open(dest, 'r', encoding='utf-8') as f:
                slave_data = json.load(f)
                if not isinstance(slave_data, dict):
                    slave_data = {}
        except Exception as e:
            logger.error(f"Fehler beim Lesen der Slave-Config: {e}")
    
    # 2. Master-Config einlesen
    try:
        with open(src, 'r', encoding='utf-8') as f:
            master_data = json.load(f)
            if not isinstance(master_data, dict):
                logger.error("Master-Config ist kein JSON-Objekt.")
                return
    except Exception as e:
        logger.error(f"Fehler beim Lesen der Master-Config: {e}")
        return

    # 3. Master-Daten ohne Secrets übernehmen und lokale Werte wieder injizieren
    merged_data = ha_sync_config_payload(master_data)
    protected_count = 0
    for k, v in slave_data.items():
        if k in HA_LOCAL_CONFIG_KEYS or is_secret_config_key(k):
            merged_data[k] = v
            protected_count += 1
            
    # 4. Sicher schreiben
    tmp_dest = dest + ".tmp"
    try:
        with open(tmp_dest, 'w', encoding='utf-8') as f:
            json.dump(merged_data, f, indent=4)
        set_web_permissions(tmp_dest, merged_data)
        os.replace(tmp_dest, dest)
        logger.info(f"Master-Config gemergt, {protected_count} lokale Slave-Werte geschützt.")
    except Exception as e:
        logger.error(f"Fehler beim Speichern der gemergten Config: {e}")

def rsync_data(target_ip, push=True):
    """
    Synchronisiert die Daten zwischen den PIs via rsync.
    push=True:  Dieser Pi sendet an den anderen Pi.
    push=False: Dieser Pi holt sich Daten vom anderen Pi.
    """
    target_ip = validate_peer_ip(target_ip)
    if not target_ip:
        logger.error("Rsync abgebrochen: ungültige HA-Peer-IP.")
        return False

    user = "pi"
    ssh_transport = "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    base_args = [
        "sudo", "-u", user, "rsync", "-au",
        "--exclude", "*.tmp",
        "--exclude", "*.flag",
        "--exclude", "*_status.json",
        "--exclude", "*_cache.json",
        "--exclude", "*_history.json",
        "--exclude", "live_history.txt",
        "--exclude", "diagnose_ack.json",
        "--exclude", "watchdog.heartbeat",
        "-e", ssh_transport,
    ]
    data_args = base_args + [
        "--exclude", "e3dc_v4.json",
        "--exclude", "e3dc_v4.json.tmp",
        "--exclude", "e3dc_v4.json.bak*",
    ]
    optional_args = base_args + ["--ignore-missing-args"]
    
    # Quelle und Ziel für Ramdisk (Historie)
    rd_src = "/var/www/html/ramdisk/" if push else _rsync_remote(user, target_ip, "/var/www/html/ramdisk/")
    rd_dst = _rsync_remote(user, target_ip, "/var/www/html/ramdisk/") if push else "/var/www/html/ramdisk/"
    
    # NEU: Quelle und Ziel für dauerhafte Daten (Datenbank, Wallbox-Logs, Archive)
    data_src = "/var/www/html/data/" if push else _rsync_remote(user, target_ip, "/var/www/html/data/")
    data_dst = _rsync_remote(user, target_ip, "/var/www/html/data/") if push else "/var/www/html/data/"

    # Quelle und Ziel für E3DC Daten (.dat Dateien)
    dat_src = sorted(glob.glob(os.path.join(INSTALL_PATH, "*.dat"))) if push else [_rsync_remote(user, target_ip, f"{INSTALL_PATH}/*.dat")]
    dat_dst = _rsync_remote(user, target_ip, f"{INSTALL_PATH}/") if push else f"{INSTALL_PATH}/"

    # NEU: Quelle und Ziel für E3DC-Strompreise (Ausschluss der wallbox.txt oder anderer lokaler Logs)
    local_txt_src = os.path.join(INSTALL_PATH, "e3dc.strompreise.txt")
    txt_src = [local_txt_src] if push and os.path.exists(local_txt_src) else []
    if not push:
        txt_src = [_rsync_remote(user, target_ip, f"{INSTALL_PATH}/e3dc.strompreise.txt")]
    txt_dst = _rsync_remote(user, target_ip, f"{INSTALL_PATH}/") if push else f"{INSTALL_PATH}/"

    try:
        subprocess.run(base_args + [rd_src, rd_dst], timeout=45, check=True)
        subprocess.run(data_args + [data_src, data_dst], timeout=45, check=True)
        if dat_src:
            subprocess.run(optional_args + dat_src + [dat_dst], timeout=45, check=True)
        if txt_src:
            subprocess.run(optional_args + txt_src + [txt_dst], timeout=45, check=True)
        
        # Rechte der geholten Daten in der Ramdisk und Data anpassen (bei Pull)
        if not push:
            subprocess.run(["sudo", "chown", "-R", "pi:www-data", "/var/www/html/ramdisk", "/var/www/html/data"])
            subprocess.run(["sudo", "find", "/var/www/html/ramdisk", "-type", "f", "-exec", "chmod", "664", "{}", "+"])
            subprocess.run(["sudo", "find", "/var/www/html/data", "-type", "d", "-exec", "chmod", "775", "{}", "+"])
            subprocess.run(["sudo", "find", "/var/www/html/data", "-type", "f", "-exec", "chmod", "664", "{}", "+"])
            subprocess.run(["sudo", "chmod", config_secret_file_mode_text(), "/var/www/html/data/e3dc_v4.json"], stderr=subprocess.DEVNULL)
            subprocess.run(["sudo", "chmod", config_secret_dir_mode_text(), "/var/www/html/data/config_backups"], stderr=subprocess.DEVNULL)
            subprocess.run(["sudo", "find", "/var/www/html/data/config_backups", "-type", "f", "-name", "e3dc_v4*", "-exec", "chmod", config_secret_file_mode_text(), "{}", "+"], stderr=subprocess.DEVNULL)
            
        return True
    except Exception as e:
        logger.error(f"Rsync Fehler: {e}")
        return False

def service_exists(service):
    """Prueft, ob eine systemd-Unit lokal existiert."""
    if not service.endswith(".service"):
        service += ".service"
    return (
        os.path.exists(f"/etc/systemd/system/{service}")
        or os.path.exists(f"/lib/systemd/system/{service}")
        or os.path.exists(f"/usr/lib/systemd/system/{service}")
    )

def service_is_active(service):
    """True, wenn systemd die Unit als aktiv meldet."""
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", service],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0

def service_is_enabled(service):
    """True, wenn die Unit beim HA-Aktivstart mitgestartet werden darf."""
    result = subprocess.run(
        ["systemctl", "is-enabled", service],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    state = (result.stdout or "").strip().lower()
    return state in {"enabled", "static", "generated", "linked", "indirect", "alias"}

def get_managed_services(action="start"):
    """
    Liefert die HA-verwalteten Dienste.

    Bei Start wird der alte C++-Kern nur dann gestartet, wenn kein V4
    `e3dc-live` vorhanden ist. Beim Stop werden beide gestoppt, damit ein
    Standby-Slave sicher stumm bleibt.
    """
    services = list(MANAGED_SERVICES)
    if action == "start" and service_exists(NATIVE_LIVE_SERVICE):
        services = [s for s in services if s != LEGACY_E3DC_SERVICE]
    return services

def manage_services(action="start"):
    """Startet oder stoppt alle Dienste, die im HA-Verbund exklusiv sein muessen."""
    if action not in ("start", "stop"):
        logger.error(f"Ungueltige Service-Aktion: {action}")
        return

    for srv in get_managed_services(action):
        if not service_exists(srv):
            continue

        try:
            active = service_is_active(srv)
            if action == "stop" and not active:
                continue
            if action == "start" and active:
                continue
            if action == "start" and not service_is_enabled(srv):
                logger.info(f"HA start uebersprungen (deaktiviert): {srv}")
                continue

            result = subprocess.run(
                ["sudo", "systemctl", action, srv],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            if result.returncode == 0:
                logger.info(f"HA {action}: {srv}")
            else:
                logger.warning(f"HA {action} fehlgeschlagen: {srv} (rc={result.returncode})")
        except Exception as e:
            logger.error(f"HA {action} Fehler bei {srv}: {e}")



def main_loop():
    logger.info("E3DC-Control High Availability Manager gestartet.")
    
    fail_counter = 0
    last_sync = 0
    was_active_as_slave = False
    master_auto_recovered = False
    last_config_mtime = 0
    last_master_cfg_mtime = 0
    
    while True:
        config = load_config()
        mode = config.get("ha_mode", "off").lower()
        raw_peer_ip = config.get("ha_peer_ip", "")
        peer_ip = validate_peer_ip(raw_peer_ip)
        
        try: fail_timeout = int(config.get("ha_fail_timeout", "15"))
        except ValueError: fail_timeout = 15
        
        try: sync_interval = int(config.get("ha_sync_interval", "60")) * 60
        except ValueError: sync_interval = 3600
        
        auto_recover = config.get("ha_auto_recover", "1") in ["1", "true"]
        auto_failover = config.get("ha_auto_failover", "1") in ["1", "true"]
        
        if mode == "off" or not raw_peer_ip:
            write_status("off", "inactive", False)
            time.sleep(60)
            continue

        if not peer_ip:
            logger.critical(f"HA-Konfiguration ungueltig: ha_peer_ip={raw_peer_ip!r} ist keine IP-Adresse.")
            if mode == "slave":
                manage_services("stop")
            write_status(mode, "config_error_invalid_peer", False, last_sync)
            time.sleep(60)
            continue

        if peer_points_to_self(peer_ip):
            logger.critical(
                f"HA-Konfiguration ungueltig: peer_ip={peer_ip} zeigt auf diesen Pi. "
                "HA-Aktionen werden blockiert."
            )
            if mode == "slave":
                manage_services("stop")
            write_status(mode, "config_error_self_peer", False, last_sync)
            time.sleep(60)
            continue
            
        # ==========================================
        # MASTER LOGIK (Der Haupt-Raspberry Pi)
        # ==========================================
        if mode == "master":
            peer_online = is_host_online(peer_ip)
            # 1. Auto-Recover beim Boot (Einmalig)
            if auto_recover and not master_auto_recovered:
                logger.info("Auto-Recover aktiv: Prüfe ob Slave erreichbar ist für Daten-Rücksicherung...")
                if peer_online:
                    manage_services("stop") # Sicherstellen, dass hier nichts schreibt
                    logger.info("Ziehe letzte Historie & Ramdisk-Daten vom Slave (Pull)...")
                    rsync_data(peer_ip, push=False)
                    logger.info("Auto-Recover abgeschlossen. Fahre System hoch.")
                master_auto_recovered = True
            
            # 2. Stelle sicher, dass die Dienste laufen
            manage_services("start")
            
            # 3. Config-Sync-Artefakt vor dem Datensync frisch und ohne Secrets schreiben
            if os.path.exists(CONFIG_PATH):
                current_config_mtime = os.path.getmtime(CONFIG_PATH)
                if current_config_mtime != last_config_mtime:
                    try:
                        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                            master_data = json.load(f)
                        sync_payload = ha_sync_config_payload(master_data)
                        with open("/var/www/html/ramdisk/master_e3dc_v4.tmp", 'w', encoding='utf-8') as f:
                            json.dump(sync_payload, f, indent=4)
                        set_web_permissions("/var/www/html/ramdisk/master_e3dc_v4.tmp")
                        os.replace("/var/www/html/ramdisk/master_e3dc_v4.tmp", "/var/www/html/ramdisk/master_e3dc_v4.json")
                        last_config_mtime = current_config_mtime
                        logger.info("Konfiguration ohne lokale Secrets für automatischen Sync zum Slave bereitgestellt.")
                    except Exception as e:
                        logger.error(f"Fehler beim Bereitstellen der Config für Sync: {e}")

            # 4. Synchronisiere Daten regelmäßig zum Backup-Pi
            if time.time() - last_sync > sync_interval:
                if peer_online:
                    rsync_data(peer_ip, push=True)
                    last_sync = time.time()
                    logger.debug(f"Sync zu Slave ({peer_ip}) erfolgreich.")
                else:
                    logger.warning(f"Slave ({peer_ip}) ist nicht erreichbar. Backup-Sync übersprungen.")

            write_status("master", "active", peer_online, last_sync)

        # ==========================================
        # SLAVE LOGIK (Der Backup-Raspberry Pi)
        # ==========================================
        elif mode == "slave":
            master_online = is_host_online(peer_ip)
            
            if master_online:
                # Master ist DA -> Alles entspannt
                if fail_counter > 0:
                    logger.info(f"Master ({peer_ip}) ist wieder erreichbar.")
                fail_counter = 0
                
                # Waren wir aktiv? -> FAILBACK einleiten!
                if was_active_as_slave:
                    logger.info("Starte Failback: Synchronisiere gesammelte Daten zurück zum Master...")
                    manage_services("stop") # Unsere Dienste stoppen!
                    rsync_data(peer_ip, push=True) # Daten zum Master pushen
                    logger.info("Starte Dienste auf dem Master neu...")
                    subprocess.run(
                        [
                            "sudo", "-u", "pi",
                            "ssh",
                            "-o", "StrictHostKeyChecking=accept-new",
                            "-o", "BatchMode=yes",
                            f"pi@{peer_ip}",
                            "sudo", "systemctl", "restart", "e3dc-live",
                        ],
                        timeout=30,
                    )
                    send_telegram(f"✅ E3DC FAILBACK: Master ({peer_ip}) ist wieder da! Daten wurden synchronisiert. Backup-Pi geht zurück in Standby.")
                    was_active_as_slave = False
                
                manage_services("stop") # Im Normalbetrieb bleiben wir gestoppt
                
                # Verhindere Endlos-Spinner im Web-UI, falls jemand auf dem Slave auf Update drückt
                force_flag = "/var/www/html/ramdisk/force_bluelink.flag"
                if os.path.exists(force_flag):
                    try: os.remove(force_flag)
                    except: pass
                
            else:
                # Master antwortet NICHT
                fail_counter += 1
                logger.warning(f"Master offline! Fehler-Zähler: {fail_counter}/{fail_timeout} Minuten")
                
                if fail_counter == fail_timeout:
                    if auto_failover:
                        logger.critical(f"FAILOVER: Master seit {fail_timeout} Minuten offline. ÜBERNEHME KONTROLLE!")
                        send_telegram(f"🚨 E3DC FAILOVER: Master ({peer_ip}) ist offline! Backup-Pi übernimmt ab sofort die Steuerung.")
                        manage_services("start")
                        was_active_as_slave = True
                    else:
                        logger.warning(f"Master offline, aber Auto-Failover ist DEAKTIVIERT. Slave bleibt im Standby.")
                        send_telegram(f"⚠️ E3DC Master ({peer_ip}) ist offline! (Manuelles Eingreifen erforderlich, Auto-Failover ist aus)")
                    
                elif fail_counter > fail_timeout:
                    if fail_counter % 60 == 0:
                        if auto_failover:
                            logger.info("System läuft weiterhin im Failover-Modus.")
                            send_telegram(f"⚠️ E3DC FAILOVER: Backup-Pi läuft aktiv. Master weiterhin offline (seit {fail_counter} Min).")

            # Config-Sync vom Master empfangen und sicher mergen
            master_cfg_path = "/var/www/html/ramdisk/master_e3dc_v4.json"
            if os.path.exists(master_cfg_path):
                current_master_cfg_mtime = os.path.getmtime(master_cfg_path)
                if current_master_cfg_mtime != last_master_cfg_mtime:
                    try:
                        merge_config(master_cfg_path, CONFIG_PATH)
                        last_master_cfg_mtime = current_master_cfg_mtime
                        logger.info("Konfiguration vom Master synchronisiert (HA-Parameter geschützt).")
                    except Exception as e:
                        logger.error(f"Fehler beim Mergen der Master-Config: {e}")

            current_state = "failover" if was_active_as_slave else "standby"
            write_status("slave", current_state, master_online)

        time.sleep(60) # Loop läuft einmal pro Minute

if __name__ == "__main__":
    main_loop()
