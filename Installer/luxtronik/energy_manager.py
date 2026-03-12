import time
import json
import os
import subprocess
import shutil
import math
from datetime import datetime, timedelta
import logging
from logging.handlers import RotatingFileHandler
import re
import requests
from luxtronik import LuxtronikModbus

# Pfade
script_dir = os.path.dirname(os.path.abspath(__file__))

# NEU: Logging Konfiguration
LOG_DIR = "/var/www/html/logs"

# Bestehende Pfade
E3DC_CONFIG_PATH = "/home/pi/E3DC-Control/e3dc.config.txt"
AWATTAR_DEBUG_PATH = "/home/pi/E3DC-Control/awattardebug.txt"
RAMDISK_FILE = "/var/www/html/ramdisk/luxtronik.json"
HISTORY_FILE = "/var/www/html/ramdisk/luxtronik_history.json"
BACKUP_DIR = "/var/www/html/tmp/luxtronik_archive"
STATE_FILE = "/var/www/html/tmp/morning_boost_state.json"
FLAG_FILE = "/var/www/html/ramdisk/manual_boost.flag"

# Pfade aus e3dc_paths.json laden falls vorhanden
PATHS_FILE = "/var/www/html/e3dc_paths.json"
if os.path.exists(PATHS_FILE):
    try:
        with open(PATHS_FILE, 'r') as f:
            pdata = json.load(f)
            install_path = pdata.get('install_path', '/home/pi/E3DC-Control')
            if not install_path.endswith('/'): install_path += '/'
            E3DC_CONFIG_PATH = install_path + "e3dc.config.txt"
            AWATTAR_DEBUG_PATH = install_path + "awattardebug.txt"
    except: pass

def setup_logging():
    """Initialisiert ein rotierendes Logfile für den Energy Manager."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, "energy_manager.log")
    
    logger = logging.getLogger("EnergyManager")
    logger.setLevel(logging.INFO)
    
    # Rotierendes Log: 1MB groß, 1 Backup-Datei
    handler = RotatingFileHandler(log_file, maxBytes=1024*1024, backupCount=1, encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s - %(message)s', datefmt='%d.%m %H:%M:%S')
    handler.setFormatter(formatter)
    
    if not logger.handlers:
        logger.addHandler(handler)
        
    # Berechtigungen für das Logfile setzen, damit es vom Webserver gelesen werden kann
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
    """Liest einen Rohwert aus der e3dc.config.txt"""
    try:
        with open(E3DC_CONFIG_PATH, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or '=' not in line: continue
                k, v = line.split('=', 1)
                if k.strip().lower() == key.lower():
                    return v.strip()
    except Exception: pass
    return None

def read_e3dc_config_value(key, default=None):
    """Liest einen Wert aus der e3dc.config.txt und gibt einen Default zurück, falls nicht gefunden."""
    value = read_e3dc_config_value_raw(key)
    if value is None:
        return default
    # Konvertiere 'true'/'false' Strings zu Booleans, Zahlen zu Zahlen etc.
    if value.lower() in ['true', '1']: return 1
    if value.lower() in ['false', '0']: return 0
    try: return float(value)
    except ValueError: return value

def load_e3dc_config_dict():
    """Liest die gesamte Config einmalig in ein Dictionary (Performance)."""
    config = {}
    try:
        with open(E3DC_CONFIG_PATH, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or '=' not in line: continue
                k, v = line.split('=', 1)
                val = v.strip()
                # Einfache Typkonvertierung
                if val.lower() in ['true', '1']: val = 1
                elif val.lower() in ['false', '0']: val = 0
                config[k.strip().lower()] = val
    except Exception: pass
    return config

def get_cfg_value(config_dict, key, default=None):
    """Holt Wert aus dem Cache-Dict."""
    if config_dict is None: return default
    val = config_dict.get(key.lower(), default)
    try: return float(val)
    except (ValueError, TypeError): return val

def write_e3dc_config_value(key, value):
    """Schreibt einen Wert in die e3dc.config.txt"""
    try:
        with open(E3DC_CONFIG_PATH, 'r') as f:
            content = f.read()
        
        pattern = re.compile(r'^(\s*' + re.escape(key) + r'\s*=\s*)(.*)$', re.MULTILINE | re.IGNORECASE)
        
        if pattern.search(content):
            new_content = pattern.sub(r'\g<1>' + str(value), content)
        else:
            # Key existiert nicht, anhängen
            new_content = content + f"\n{key} = {value}\n"
            
        with open(E3DC_CONFIG_PATH, 'w') as f:
            f.write(new_content)
        return True
    except Exception as e:
        return False

def get_forecast_data():
    """Liest awattardebug.txt und analysiert die Prognose für die nächsten 18h"""
    high_soc_hours = 0
    total_pv_pct = 0.0
    
    if not os.path.exists(AWATTAR_DEBUG_PATH):
        return 0, 0.0

    try:
        with open(AWATTAR_DEBUG_PATH, 'r') as f:
            lines = f.readlines()
        
        in_data = False
        
        for line in lines:
            if "Data" in line: in_data = True; continue
            if not in_data: continue
            
            parts = line.split()
            if len(parts) >= 5:
                soc = float(parts[2])
                pv = float(parts[4])
                
                if soc >= 98.0:
                    high_soc_hours += 0.25 
                    total_pv_pct += pv
    except: pass
    return high_soc_hours, total_pv_pct

def get_price_action(prices, start_hour, interval, limit, min_duration_min, current_idx_float):
    """Ermittelt, ob wir boosten (BOOST), pausieren (PAUSE) oder nichts tun (NONE)."""
    if not prices:
        return "NONE"

    min_slots = int(math.ceil(min_duration_min / 60.0 / interval))
    
    # 1. Günstige Blöcke finden
    cheap_blocks = []
    current_block = []
    
    for i, p in enumerate(prices):
        if p <= limit:
            current_block.append(i)
        else:
            if len(current_block) >= min_slots:
                cheap_blocks.append((current_block[0], current_block[-1]))
            current_block = []
    if len(current_block) >= min_slots:
        cheap_blocks.append((current_block[0], current_block[-1]))
        
    # 2. Prüfen wo wir sind
    for start_idx, end_idx in cheap_blocks:
        if start_idx <= current_idx_float < (end_idx + 1):
            return "BOOST"
        
        slots_1h = 1.0 / interval
        if (start_idx - slots_1h) <= current_idx_float < start_idx:
            return "PAUSE"
            
    return "NONE"

def main():
    logger = setup_logging()

    # Initiales Lesen (einmalig beim Start)
    luxtronik_enabled = read_e3dc_config_value('luxtronik', 0) == 1
    wp_ip = read_e3dc_config_value('luxtronik_ip')
    
    logger.info("Dienst wird gestartet...")
    wp = None
    if luxtronik_enabled and wp_ip:
        try:
            wp = LuxtronikModbus(wp_ip)
            logger.info("Luxtronik-Modul aktiv und verbunden.")
        except Exception as e:
            logger.error(f"Fehler bei Luxtronik-Initialisierung: {e}")
            wp = None

    # Initialwerte lesen
    GRID_START_LIMIT = read_e3dc_config_value('GRID_START_LIMIT', -3500)
    MIN_SOC = read_e3dc_config_value('MIN_SOC', 80)
    AT_LIMIT = read_e3dc_config_value('AT_LIMIT', 10.0)
    AUTO_MODE = read_e3dc_config_value('auto_mode', 1)
    
    PRICE_BOOST_ENABLE = read_e3dc_config_value('price_boost_enable', 0)
    PRICE_LIMIT = read_e3dc_config_value('price_limit', 20.0)
    PRICE_MIN_DURATION = read_e3dc_config_value('price_min_duration', 60)
    PRICE_MAX_DAILY = read_e3dc_config_value('price_max_daily', 180)
    PRICE_HARD_LIMIT = read_e3dc_config_value('price_hard_limit', -99.0)
    WQ_MIN_TEMP = read_e3dc_config_value('wq_min_temp', 1.0)
    RL_SOURCE = read_e3dc_config_value('rl_source', 'internal')
    MANUAL_BOOST_MIN_SOC = read_e3dc_config_value('manual_boost_min_soc', 25)

    MB_ENABLE = read_e3dc_config_value('morning_boost_enable', 0)
    MB_PRIO = read_e3dc_config_value('morning_boost_prio', 'wallbox')
    MB_WB_POWER = read_e3dc_config_value('morning_boost_wb_power', 7.0)
    MB_MIN_HOURS = read_e3dc_config_value('morning_boost_min_hours', 3)
    MB_MIN_PV_PCT = read_e3dc_config_value('morning_boost_min_pv_pct', 50.0)
    MB_TARGET_SOC = read_e3dc_config_value('morning_boost_target_soc', 20)
    try: MB_DEADLINE = int(read_e3dc_config_value('morning_boost_deadline', 8))
    except: MB_DEADLINE = 8

    SI_ENABLE = read_e3dc_config_value('super_intelligence_enable', 0)
    try: SI_DEADLINE = int(read_e3dc_config_value('super_intelligence_deadline', 8))
    except: SI_DEADLINE = 8

    TELEGRAM_TOKEN = read_e3dc_config_value('telegram_token', '')
    TELEGRAM_CHAT_ID = read_e3dc_config_value('telegram_chat_id', '')

    PV_PAUSE_ENABLE = read_e3dc_config_value('pv_pause_enable', 0)
    PV_PAUSE_SOC = read_e3dc_config_value('pv_pause_soc', 80)
    PV_PAUSE_WATT = read_e3dc_config_value('pv_pause_watt', 3000.0)
    PV_PAUSE_TIMEOUT_MINUTES = read_e3dc_config_value('pv_pause_timeout_minutes', 120)
    
    STOP_DELAY_MINUTES = read_e3dc_config_value('stop_delay_minutes', 10)
    MANUAL_BOOST_MAX_DURATION = read_e3dc_config_value('manual_boost_max_duration', 180)
    
    update_checked_today = False

    boost_active = False
    price_boost_active = False
    pv_pause_active = False
    pv_pause_start_time = None
    pre_pause_active = False
    deficit_start_time = None
    last_day = datetime.now().day
    first_run = True
    daily_boost_counter = 0
    last_pv_boost_time = 0
    last_price_warning_time = 0
    last_safety_check_time = 0
    mb_state = "IDLE"
    mb_running_prio = ""
    
    si_state = "IDLE"
    si_calibrated_power_kw = None
    si_pause_end_ts = None
    si_power_check_counter = 0

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

    # Restore Status
    if os.path.exists(RAMDISK_FILE):
        try:
            with open(RAMDISK_FILE, 'r') as f:
                saved = json.load(f)
                if 'ts' in saved:
                    saved_ts = datetime.fromisoformat(saved['ts'])
                    if saved_ts.date() == datetime.now().date():
                        daily_boost_counter = saved.get('daily_boost_counter', 0)
                last_pv_boost_time = saved.get('last_pv_boost_time', 0)                
                
                if (datetime.now() - saved_ts).total_seconds() < 1200:
                    if saved.get('boost_active'):
                        boost_active = True
                        pv_pause_active = saved.get('pv_pause_active', False)
                        price_boost_active = saved.get('price_boost_active', False)
                        pre_pause_active = saved.get('pre_pause_active', False)
                        if pv_pause_active: pv_pause_start_time = saved.get('pv_pause_start_time', time.time())
                        logger.info(f"Aktiven Status wiederhergestellt: Boost={boost_active}")
        except: pass

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                state_data = json.load(f)
                if state_data.get('mode') == 'morning_boost' and state_data.get('status') == 'RUNNING':
                    mb_state = 'RUNNING'
                    mb_running_prio = state_data.get('prio', '')
                    logger.warning(f"Morning-Boost Status wiederhergestellt: RUNNING")
                elif state_data.get('mode') == 'super_intelligence':
                    si_state = state_data.get('status', 'IDLE')
                    if si_state != 'IDLE' and si_state != 'DONE':
                        si_calibrated_power_kw = state_data.get('calibrated_power')                        
                        si_pause_end_ts = state_data.get('pause_end_ts')
                        logger.warning(f"Superintelligence Status wiederhergestellt: {si_state}")
        except: pass

    # Init Ramdisk
    init_json = {"ts": datetime.now().isoformat(), "success": False, "error": "Dienst startet...", "data": {}, "status": {}}
    with open(RAMDISK_FILE, 'w') as f: json.dump(init_json, f)
    try: os.chmod(RAMDISK_FILE, 0o664)
    except: pass

    # Config Cache Init
    config_cache = {}
    config_mtime = 0

    if AUTO_MODE == 0: logger.info("Automatik-Regelung ist DEAKTIVIERT (Nur Monitoring).")
    elif not wp: logger.info("Nur Lademanagement-Regeln sind aktiv.")
    else: logger.info(f"Start bei > {abs(GRID_START_LIMIT)}W Einspeisung.")

    while True:
        now = datetime.now()
        
        # 1. Konfiguration nur bei Dateiänderung neu laden (Smart Caching)
        try:
            if os.path.exists(E3DC_CONFIG_PATH):
                current_mtime = os.path.getmtime(E3DC_CONFIG_PATH)
                if current_mtime != config_mtime:
                    config_cache = load_e3dc_config_dict()
                    config_mtime = current_mtime
                    logger.info("Konfiguration aktualisiert (Dateiänderung erkannt).")
        except Exception as e:
            logger.error(f"Fehler beim Konfigurations-Check: {e}")
        
        current_config = config_cache
        
        # Dynamisches Nachladen wichtiger Parameter
        AUTO_UPDATE_ENABLE = int(get_cfg_value(current_config, 'auto_update_enable', 0))
        update_time_str = str(get_cfg_value(current_config, 'auto_update_time', "23:00"))
        try: update_hour, update_minute = map(int, update_time_str.split(':'))
        except: update_hour, update_minute = 23, 0
            
        GRID_START_LIMIT = get_cfg_value(current_config, 'GRID_START_LIMIT', -3500)
        STOP_DELAY_MINUTES = get_cfg_value(current_config, 'stop_delay_minutes', 10)
        MIN_SOC = get_cfg_value(current_config, 'MIN_SOC', 80)
        AUTO_MODE = int(get_cfg_value(current_config, 'auto_mode', 1))
        AT_LIMIT = get_cfg_value(current_config, 'AT_LIMIT', 10.0)
        # Gecachte Sollwerte für die Schleife
        CONF_WWS = get_cfg_value(current_config, 'WWS', 50.0)
        CONF_WWW = get_cfg_value(current_config, 'WWW', 48.0)
        CONF_HZ  = get_cfg_value(current_config, 'HZ', 32.0)
        
        # Auto-Update Check
        if AUTO_UPDATE_ENABLE == 1:
            if now.hour == update_hour and now.minute == update_minute:
                if not update_checked_today:
                    logger.info(f"Starte tägliche Update-Prüfung ({update_hour:02d}:{update_minute:02d} Uhr)...")
                    # Pfad zum Install-Verzeichnis (z.B. /home/pi/Install)
                    install_root = os.path.abspath(os.path.join(script_dir, "../../"))
                    self_update_script = os.path.join(install_root, "Installer", "self_update.py")
                    
                    if os.path.exists(self_update_script):
                        try:
                            # Vereinfachter und robusterer Aufruf.
                            # Erfordert eine sudoers-Regel für den ausführenden Benutzer.
                            log_file = os.path.join(LOG_DIR, "auto_self_update.log")
                            cmd = f"sudo /usr/bin/python3 {self_update_script} --silent"
                            
                            # Log-Datei vorbereiten
                            with open(log_file, "w") as f:
                                f.write(f"=== Starting Auto-Update at {datetime.now()} ===\n")
                                f.write(f"Command: {cmd}\n---\n")
                            os.chmod(log_file, 0o664)

                            logger.info(f"Führe Update-Kommando aus: {cmd}")
                            # Prozess starten und Ausgabe in Log-Datei umleiten
                            subprocess.Popen(f"nohup {cmd} >> {log_file} 2>&1 &", shell=True)
                            logger.info("Update-Prozess im Hintergrund gestartet.")
                        except Exception as e:
                            logger.error(f"Fehler beim Starten des Auto-Updates: {e}")
                    else:
                        logger.error("Auto-Update fehlgeschlagen: self_update.py nicht gefunden.")
                    update_checked_today = True
            else:
                update_checked_today = False

        wp_data = {}   
        wp_status = {} 
        at = 20.0 
        success = False 
        wq_aus = 10.0 
        if not wp: success = True

        try:
            # 1. Daten von WP holen
            if wp:
                if wp.connect():
                    wp_data = wp.read_all_sensors()
                    time.sleep(0.5)
                    wp_status = wp.read_shi_status()
                    
                    wq_aus = wp_data.get('Sole_Aus', wp_data.get('WQ_Austritt', 10.0))
                    
                    if first_run: first_run = False
                    
                    # Sicherheits-Check
                    if not boost_active and not os.path.exists(FLAG_FILE) and (wp_status.get('WW_Mode') == 1 or wp_status.get('HZ_Mode') == 1):
                        hz_set = wp_status.get('HZ_Setpoint', 0)
                        if wp_status.get('HZ_Mode') == 1 and abs(hz_set - 20.0) < 1.0:
                             if PV_PAUSE_ENABLE == 1:
                                 logger.info("Erkenne aktiven Pause-Status (20°C). Übernehme.")
                                 boost_active = True
                                 pv_pause_active = True
                                 if not pv_pause_start_time: pv_pause_start_time = time.time()
                        else:
                            if (time.time() - last_safety_check_time) > 900:
                                msg = f"SICHERHEIT: Unerwarteter Boost-Status (WW={wp_status.get('WW_Mode')}, HZ={wp_status.get('HZ_Mode')}). Reset."
                                logger.warning(msg)
                                send_telegram(f"⚠️ {msg}")
                                last_safety_check_time = time.time()
                            
                            wp.write_hz_boost(0, 32.0)
                            wp.write_ww_boost(0, CONF_WWW)
                        
                        wp.close()
                        success = True
                        time.sleep(15)
                        continue

                    # Externer Reset Check
                    if boost_active and wp_status:
                        if wp_status.get('WW_Mode') != 1 and wp_status.get('HZ_Mode') != 1:
                            logger.info("Boost-Modus extern deaktiviert. Reset.")
                            boost_active = False
                            deficit_start_time = None
                            price_boost_active = False
                            pre_pause_active = False
                            pv_pause_active = False
                    
                    wp.close()
                    success = True 
                else:
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
            
            try:
                r = requests.get("http://localhost/get_live_json.php", timeout=5)
                if r.status_code == 200:
                    e3dc = r.json()
                    grid = e3dc.get('grid', 0)
                    bat = e3dc.get('bat', 0)
                    soc = e3dc.get('soc', 0)
                    wb_locked = e3dc.get('wb_locked', False)
                    current_price = e3dc.get('current_price', 99.9)
                    prices = e3dc.get('prices', [])
                    forecast = e3dc.get('forecast', [])
                    price_start_hour = e3dc.get('price_start_hour', 0)
                    price_interval = e3dc.get('price_interval', 1.0)
                    e3dc_valid = True
            except Exception as e:
                if AUTO_MODE == 1 or os.path.exists(FLAG_FILE):
                    logger.error(f"Fehler bei E3DC Abfrage: {e}")

            # Manueller Boost Check
            if os.path.exists(FLAG_FILE) and wp:
                try:
                    if wq_aus < WQ_MIN_TEMP:
                        logger.warning(f"NOT-AUS (Manuell): WQ Aus zu kalt ({wq_aus}°C).")
                        if wp and wp.connect():
                            wp.write_hz_boost(0); wp.write_ww_boost(0, CONF_WWW); wp.close()
                        os.remove(FLAG_FILE)
                    elif soc < MANUAL_BOOST_MIN_SOC:                        
                        logger.info(f"Manueller Boost gestoppt: SoC niedrig ({soc}%).")
                        if wp and wp.connect():
                            wp.write_hz_boost(0); wp.write_ww_boost(0, CONF_WWW); wp.close()
                        os.remove(FLAG_FILE)
                    elif (time.time() - os.path.getmtime(FLAG_FILE)) > (MANUAL_BOOST_MAX_DURATION * 60):
                        logger.info("Manueller Boost abgelaufen.")
                        if wp and wp.connect():
                            wp.write_hz_boost(0); wp.write_ww_boost(0, CONF_WWW); wp.close()
                        os.remove(FLAG_FILE)
                except Exception as e: logger.error(f"Fehler Manual-Boost: {e}")

            # --- SUPERINTELLIGENCE LOGIK ---
            if SI_ENABLE == 1:
                si_target_soc = MANUAL_BOOST_MIN_SOC
                si_deadline_ts = datetime(now.year, now.month, now.day, SI_DEADLINE, 0, 0)

                if si_state == "PAUSING" and si_pause_end_ts and now >= datetime.fromtimestamp(si_pause_end_ts):
                    logger.info("Superintelligence: Pause beendet.")
                    write_e3dc_config_value('wbmode', 10)
                    si_state = "RUNNING"

                if si_state == "IDLE" and now < si_deadline_ts:
                    high_hours, pv_sum = get_forecast_data()
                    if high_hours >= 8.0 and pv_sum >= 1.5 * (100 - si_target_soc):                        
                        logger.info(f"Superintelligence geplant! (Prog: {high_hours}h, {pv_sum:.1f}% PV)")
                        si_state = "PLANNED"
                
                if si_state == "PLANNED":
                    cap_kwh = float(get_cfg_value(current_config, 'speichergroesse', 10.0))
                    energy_needed_kwh = (max(0, soc - si_target_soc) / 100.0) * cap_kwh
                    assumed_power_kw = MB_WB_POWER + 0.5
                    duration_h = energy_needed_kwh / assumed_power_kw if assumed_power_kw > 0 else 0
                    start_ts = si_deadline_ts - timedelta(hours=duration_h)
                    
                    if now >= start_ts:
                        logger.info(f"Starte Superintelligence. Ziel: {si_target_soc}%.")
                        orig_mode = get_cfg_value(current_config, 'wbmode')
                        orig_min = get_cfg_value(current_config, 'wbminsoc')
                        with open(STATE_FILE, 'w') as f:
                            json.dump({'mode': 'super_intelligence', 'status': 'RUNNING', 'orig_wbmode': orig_mode, 'orig_wbminsoc': orig_min}, f)
                        try: os.chmod(STATE_FILE, 0o666)
                        except: pass
                        write_e3dc_config_value('wbminsoc', si_target_soc)
                        write_e3dc_config_value('wbmode', 10)
                        si_state = "RUNNING"
                        si_power_check_counter = 0
                        si_calibrated_power_kw = None

                if si_state == "RUNNING":
                    if (soc <= (si_target_soc + 1)) or (now >= si_deadline_ts):
                        logger.info("Superintelligence beendet.")
                        if os.path.exists(STATE_FILE):
                            with open(STATE_FILE, 'r') as f: saved = json.load(f)
                            write_e3dc_config_value('wbmode', saved.get('orig_wbmode', 4))
                            write_e3dc_config_value('wbminsoc', saved.get('orig_wbminsoc', 50))
                            os.remove(STATE_FILE)
                        si_state = "DONE"
                    else:
                        actual_wb_power_w = e3dc.get('wb', 0)
                        if si_calibrated_power_kw is None and actual_wb_power_w > 1000:
                            si_power_check_counter += 1
                            if si_power_check_counter >= 3:
                                si_calibrated_power_kw = actual_wb_power_w / 1000.0
                                logger.info(f"SI: Kalibriert auf {si_calibrated_power_kw:.2f} kW.")
                                with open(STATE_FILE, 'r+') as f:
                                    d = json.load(f)
                                    d['calibrated_power'] = si_calibrated_power_kw
                                    f.seek(0); f.truncate(); json.dump(d, f)
                        elif actual_wb_power_w < 1000:
                            si_power_check_counter = 0

                        if si_calibrated_power_kw is not None:
                            cap_kwh = float(get_cfg_value(current_config, 'speichergroesse', 10.0))
                            remaining_energy = ((soc - si_target_soc) / 100.0) * cap_kwh
                            power_kw = si_calibrated_power_kw + 0.5
                            needed_h = remaining_energy / power_kw if power_kw > 0 else 99
                            req_start = si_deadline_ts - timedelta(hours=needed_h)

                            if now < req_start:
                                pause_s = (req_start - now).total_seconds()
                                if pause_s > 120:                                    
                                    logger.info(f"SI: Pause für {pause_s/60:.1f} Min.")
                                    si_pause_end_ts = time.time() + pause_s
                                    si_state = "PAUSING"
                                    with open(STATE_FILE, 'r+') as f:
                                        d = json.load(f)
                                        d['status'] = 'PAUSING'; d['pause_end_ts'] = si_pause_end_ts
                                        f.seek(0); f.truncate(); json.dump(d, f)
                                        write_e3dc_config_value('wbmode', d.get('orig_wbmode', 4))

            if now.hour == 0 and now.minute < 2 and (si_state == "DONE" or si_state == "IDLE"):
                si_state = "IDLE"; si_calibrated_power_kw = None

            # --- MORNING BOOST ---
            elif MB_ENABLE == 1:
                if mb_state == "IDLE" and now.hour < MB_DEADLINE:
                    high_hours, pv_sum = get_forecast_data()
                    if high_hours >= MB_MIN_HOURS and pv_sum >= MB_MIN_PV_PCT:                            
                        logger.info(f"Morning-Boost geplant! ({high_hours}h voll)")
                        mb_state = "PLANNED"
                
                if mb_state == "PLANNED":
                    eff_prio = None
                    if MB_PRIO == 'wallbox': eff_prio = 'wallbox' if wb_locked else ('heatpump' if wp else None)
                    elif MB_PRIO == 'wallbox_only': eff_prio = 'wallbox' if wb_locked else None
                    elif MB_PRIO == 'heatpump' and wp: eff_prio = 'heatpump'

                    if eff_prio:
                        cap_kwh = float(get_cfg_value(current_config, 'speichergroesse', 10.0))
                        en_need = (max(0, soc - MB_TARGET_SOC) / 100.0) * cap_kwh
                        p_kw = 0.5
                        if eff_prio == 'wallbox': p_kw += MB_WB_POWER
                        else: p_kw += float(get_cfg_value(current_config, 'wpmax', 4.0))
                        
                        dur_h = en_need / p_kw if p_kw > 0 else 0
                        deadline_ts = datetime(now.year, now.month, now.day, MB_DEADLINE, 0, 0)
                        start_ts = deadline_ts - timedelta(hours=dur_h)
                        
                        if now >= start_ts:
                            logger.info(f"Starte Morning-Boost ({eff_prio}). Ziel: {MB_TARGET_SOC}%.")
                            if eff_prio == 'wallbox':
                                orig_mode = get_cfg_value(current_config, 'wbmode')
                                orig_min = get_cfg_value(current_config, 'wbminsoc')
                                with open(STATE_FILE, 'w') as f:
                                    json.dump({'mode': 'morning_boost', 'status': 'RUNNING', 'prio': 'wallbox', 'orig_wbmode': orig_mode, 'orig_wbminsoc': orig_min}, f)
                                try: os.chmod(STATE_FILE, 0o666)
                                except: pass
                                write_e3dc_config_value('wbminsoc', MB_TARGET_SOC)
                                write_e3dc_config_value('wbmode', 10)
                            else:
                                with open(STATE_FILE, 'w') as f:
                                    json.dump({'mode': 'morning_boost', 'status': 'RUNNING', 'prio': 'heatpump'}, f)
                                try: os.chmod(STATE_FILE, 0o666)
                                except: pass
                                with open(FLAG_FILE, 'w') as f: f.write("1")
                            mb_running_prio = eff_prio
                            mb_state = "RUNNING"

                if mb_state == "RUNNING":
                    if soc <= (MB_TARGET_SOC + 1) or now.hour >= MB_DEADLINE:
                        logger.info("Morning-Boost beendet.")
                        if os.path.exists(STATE_FILE):
                            with open(STATE_FILE, 'r') as f: saved = json.load(f)
                            if saved.get('mode') == 'morning_boost' and saved.get('prio') == 'wallbox':
                                write_e3dc_config_value('wbmode', saved.get('orig_wbmode', 4))
                                write_e3dc_config_value('wbminsoc', saved.get('orig_wbminsoc', 50))
                            else:
                                if os.path.exists(FLAG_FILE) and wp: os.remove(FLAG_FILE)
                                if wp and wp.connect():
                                    wp.write_hz_boost(0); wp.write_ww_boost(0)
                                    wp.close()
                            os.remove(STATE_FILE)
                        mb_state = "DONE"
                        mb_running_prio = ""

            # --- HAUPT REGELUNG (Wärmepumpe) ---
            if not os.path.exists(FLAG_FILE) and AUTO_MODE == 1 and wp:
                try:
                    is_deficit = (grid > 50) or (bat < -50)

                    # PV PAUSE
                    if PV_PAUSE_ENABLE == 1 and current_price > 0 and mb_state != "RUNNING" and si_state != "RUNNING" and si_state != "PAUSING":
                        if pv_pause_active:
                            if grid <= GRID_START_LIMIT:                                    
                                logger.info(f"PV-Pause beendet -> Überschuss ({grid}W).")
                                pv_pause_active = False; boost_active = False
                            elif e3dc_valid and soc > 0 and soc < (PV_PAUSE_SOC - 5):                                    
                                logger.warning("PV-Pause abgebrochen (SoC tief).")
                                if wp and wp.connect(): wp.write_hz_boost(0); wp.write_ww_boost(0, CONF_WWW); wp.close()
                                pv_pause_active = False; boost_active = False; pv_pause_start_time = None
                            elif pv_pause_start_time and (time.time() - pv_pause_start_time) > (PV_PAUSE_TIMEOUT_MINUTES * 60):                                    
                                logger.warning("PV-Pause Timeout.")
                                if wp and wp.connect(): wp.write_hz_boost(0); wp.write_ww_boost(0, CONF_WWW); wp.close()
                                pv_pause_active = False; boost_active = False; pv_pause_start_time = None
                            elif forecast:
                                gmt = time.gmtime(); now_gmt = gmt.tm_hour + gmt.tm_min / 60.0
                                peak_still_valid = False; max_future_w = 0.0; current_w = 0.0
                                for entry in forecast:
                                    h = entry['h']; 
                                    if h < (now_gmt - 12): h += 24
                                    if abs(h - now_gmt) < 0.25: current_w = entry['w']
                                    if now_gmt < h <= (now_gmt + 1.5) and entry['w'] > max_future_w: max_future_w = entry['w']
                                if max_future_w >= PV_PAUSE_WATT and max_future_w > (current_w * 1.1): peak_still_valid = True
                                    
                                if not peak_still_valid:
                                    logger.info("PV-Pause beendet (Trend entfallen).")
                                    if wp and wp.connect(): wp.write_hz_boost(0); wp.write_ww_boost(0, CONF_WWW); wp.close()
                                    pv_pause_active = False; boost_active = False; pv_pause_start_time = None

                        elif not boost_active and soc >= PV_PAUSE_SOC:
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
                                logger.info(f"Starte PV-Pause (Prognose > {PV_PAUSE_WATT}W).")
                                if wp and wp.connect():
                                    wp.write_hz_boost(1, 20.0); wp.write_ww_boost(0, CONF_WWW); wp.close()
                                pv_pause_active = True; boost_active = True; pv_pause_start_time = time.time()

                    # PREIS BOOST
                    price_action = "NONE"
                    if PRICE_BOOST_ENABLE == 1 and mb_state != "RUNNING" and si_state != "RUNNING" and si_state != "PAUSING":
                        if current_price <= 0: price_action = "BOOST"
                        elif prices:
                            gmt = time.gmtime(); now_gmt = gmt.tm_hour + gmt.tm_min / 60.0
                            h_diff = now_gmt - price_start_hour
                            if h_diff < -12: h_diff += 24
                            if h_diff > 36: h_diff -= 24
                            price_action = get_price_action(prices, price_start_hour, price_interval, PRICE_LIMIT, PRICE_MIN_DURATION, h_diff / price_interval)
                        
                        is_hard = (current_price <= PRICE_HARD_LIMIT)
                        if wq_aus < WQ_MIN_TEMP: price_action = "NONE"
                        elif daily_boost_counter >= PRICE_MAX_DAILY and price_action == "BOOST" and not is_hard: price_action = "NONE"
                        elif (time.time() - last_pv_boost_time) <= (18 * 3600) and price_action != "NONE" and not is_hard: price_action = "NONE"

                    if price_action == "PAUSE":
                        if not pre_pause_active:
                            logger.info("Start Preis-Pause.")
                            if wp and wp.connect(): wp.write_hz_boost(1, 20.0); wp.write_ww_boost(0, CONF_WWW); wp.close()
                            pre_pause_active = True; price_boost_active = False; boost_active = True
                    elif price_action == "BOOST":
                        if not price_boost_active:
                            logger.info(f"Start Preis-Boost ({current_price} ct).")
                            if wp and wp.connect():
                                if at > AT_LIMIT: wp.write_ww_boost(1, CONF_WWS); wp.write_hz_boost(0)
                                else: wp.write_ww_boost(1, CONF_WWW); wp.write_hz_boost(1, CONF_HZ)
                                wp.close()
                            price_boost_active = True; pre_pause_active = False; boost_active = True
                        daily_boost_counter += 0.5
                    elif (price_boost_active or pre_pause_active) and price_action == "NONE":
                        logger.info("Ende Preis-Steuerung.")
                        if wp and wp.connect(): wp.write_hz_boost(0); wp.write_ww_boost(0, CONF_WWW); wp.close()
                        price_boost_active = False; pre_pause_active = False; boost_active = False

                    # PV BOOST
                    if not boost_active and mb_state != "RUNNING" and si_state != "RUNNING" and si_state != "PAUSING":
                        if grid <= GRID_START_LIMIT and soc >= MIN_SOC and wp:
                                logger.info(f"Start PV-Boost (Grid: {grid}W).")
                                if wp.connect():
                                    if at > AT_LIMIT: wp.write_ww_boost(1, CONF_WWS); wp.write_hz_boost(0)
                                    else: wp.write_ww_boost(1, CONF_WWW); wp.write_hz_boost(1, CONF_HZ)
                                    wp.close()
                                boost_active = True; deficit_start_time = None

                    # LAUFENDE ÜBERWACHUNG
                    if boost_active:
                        is_pv = (not price_boost_active) and (not pre_pause_active) and (not pv_pause_active)
                        if is_pv:
                            last_pv_boost_time = time.time()
                            # Sync Check
                            t_ww = CONF_WWS if at > AT_LIMIT else CONF_WWW
                            if abs(wp_status.get('WW_Setpoint', 0) - t_ww) > 0.5:
                                logger.info("Sync Check: Werte korrigiert.")
                                if wp.connect():
                                    if at > AT_LIMIT: wp.write_ww_boost(1, CONF_WWS); wp.write_hz_boost(0)
                                    else: wp.write_ww_boost(1, CONF_WWW); wp.write_hz_boost(1, CONF_HZ)
                                    wp.close()

                        # Defizit Abschaltung (nur PV)
                        if is_deficit and is_pv:
                            if deficit_start_time is None:
                                deficit_start_time = now; logger.info("Defizit erkannt. Timer start.")
                            elif (now - deficit_start_time).total_seconds() > (STOP_DELAY_MINUTES * 60):
                                logger.info("Stop PV-Boost (Defizit).")
                                if wp and wp.connect(): wp.write_ww_boost(0, 45.0); wp.write_hz_boost(0); wp.close()
                                boost_active = False; deficit_start_time = None
                        else:
                            if deficit_start_time is not None:
                                logger.info("Defizit beendet."); deficit_start_time = None

                except Exception as req_err: logger.error(f"Fehler Logik: {req_err}")

            # 3. Daten schreiben
            json_export = {
                "ts": now.isoformat(), "data": wp_data, "status": wp_status,
                "boost_active": boost_active, "auto_mode": AUTO_MODE, "wq_aus": wq_aus,
                "daily_boost_counter": daily_boost_counter, "last_pv_boost_time": last_pv_boost_time,
                "price_boost_active": price_boost_active, "pre_pause_active": pre_pause_active,
                "pv_pause_active": pv_pause_active, "mb_state": mb_state, "mb_prio": mb_running_prio,
                "si_state": si_state, "success": success
            }
            tmp_file = RAMDISK_FILE + ".tmp"
            with open(tmp_file, 'w') as f: json.dump(json_export, f)
            try: 
                os.chmod(tmp_file, 0o664)
                st = os.stat(os.path.dirname(RAMDISK_FILE))
                os.chown(tmp_file, st.st_uid, st.st_gid)
            except: pass
            os.replace(tmp_file, RAMDISK_FILE)
            with open(HISTORY_FILE, 'a') as f: f.write(json.dumps(json_export) + "\n")

            # Tageswechsel
            if now.day != last_day:
                shutil.copy(HISTORY_FILE, os.path.join(BACKUP_DIR, f"luxtronik_{(now - timedelta(days=1)).strftime('%Y-%m-%d')}.json"))
                open(HISTORY_FILE, 'w').close()
                if mb_state == "DONE": mb_state = "IDLE"
                if si_state == "DONE": si_state = "IDLE"
                daily_boost_counter = 0; last_day = now.day

        except Exception as e:
            logger.critical(f"Kritischer Fehler: {e}", exc_info=True)
            with open(RAMDISK_FILE, 'w') as f: json.dump({"success": False, "error": str(e), "ts": now.isoformat()}, f)
        
        time.sleep(15)

if __name__ == "__main__":
    main()
