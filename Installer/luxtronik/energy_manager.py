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
CONFIG_PATH = os.path.join(script_dir, "config.lux.json")
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
        # Owner/Group vom Verzeichnis erben (damit Check zufrieden ist)
        st = os.stat(LOG_DIR)
        os.chown(log_file, st.st_uid, st.st_gid)
    except Exception: pass
    return logger

# Verzeichnis für Archiv erstellen
if not os.path.exists(BACKUP_DIR):
    os.makedirs(BACKUP_DIR)

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    return {}


def read_e3dc_config_value(key):
    """Liest einen Wert aus der e3dc.config.txt"""
    try:
        with open(E3DC_CONFIG_PATH, 'r') as f:
            for line in f:
                if line.strip().startswith('#'): continue
                if '=' in line:
                    k, v = line.split('=', 1)
                    if k.strip().lower() == key.lower():
                        return v.strip()
    except Exception as e:
        # Hier kann nicht geloggt werden, da der Logger noch nicht initialisiert ist
        pass
    return None


def write_e3dc_config_value(key, value):
    """Schreibt einen Wert in die e3dc.config.txt (Regex-basiert, um Kommentare zu erhalten)"""
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
        # Siehe oben
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
        now_gmt = time.gmtime().tm_hour + time.gmtime().tm_min / 60.0
        
        for line in lines:
            if "Data" in line: in_data = True; continue
            if not in_data: continue
            
            parts = line.split()
            if len(parts) >= 5:
                # Format: Time(0) Price(1) SoC(2) ... PV%(4)
                # Achtung: Indizes im Python Split: 0=Time, 2=SoC, 4=PV%
                t = float(parts[0])
                soc = float(parts[2])
                pv = float(parts[4])
                
                # Wir schauen uns den Tag an (einfache Logik: alles was in der Datei steht)
                if soc >= 98.0: # Toleranz für 99%
                    high_soc_hours += 0.25 # Viertelstunden-Werte
                    total_pv_pct += pv
    except: pass
    return high_soc_hours, total_pv_pct


def get_price_action(prices, start_hour, interval, limit, min_duration_min, current_idx_float):
    """
    Ermittelt, ob wir boosten (BOOST), pausieren (PAUSE) oder nichts tun (NONE).
    """
    if not prices:
        return "NONE"

    min_slots = int(math.ceil(min_duration_min / 60.0 / interval))
    action = "NONE"
    
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
    # Letzten Block prüfen
    if len(current_block) >= min_slots:
        cheap_blocks.append((current_block[0], current_block[-1]))
        
    # 2. Prüfen wo wir sind
    for start_idx, end_idx in cheap_blocks:
        # Sind wir IM Block? (Start <= Jetzt <= Ende)
        # Wir geben etwas Toleranz am Ende (-0.1), damit wir nicht genau auf der Kante flackern
        if start_idx <= current_idx_float < (end_idx + 1):
            return "BOOST"
            
        # Sind wir in der PAUSE davor? (Start - 1h <= Jetzt < Start)
        # 1 Stunde in Slots umrechnen
        slots_1h = 1.0 / interval
        if (start_idx - slots_1h) <= current_idx_float < start_idx:
            return "PAUSE"
            
    return "NONE"


def main():
    logger = setup_logging()
    cfg = load_config()

    # Prüfen ob Luxtronik aktiviert ist
    luxtronik_enabled = int(cfg.get('luxtronik', 0)) == 1
    wp_ip = cfg.get('luxtronik_ip')
    
    logger.info("Dienst wird gestartet...")
    # Nur initialisieren, wenn auch aktiv und IP vorhanden
    wp = None
    if luxtronik_enabled and wp_ip:
        try:
            wp = LuxtronikModbus(wp_ip)
            logger.info("Luxtronik-Modul aktiv und verbunden.")
        except Exception as e:
            logger.error(f"Fehler bei Luxtronik-Initialisierung: {e}")
            wp = None # Sicherstellen, dass es None ist bei Fehler

    # Konfiguration
    # Start-Grenze: -3500 bedeutet 3500W Einspeisung nötig zum Starten
    GRID_START_LIMIT = cfg.get('GRID_START_LIMIT', -3500) 
    MIN_SOC = cfg.get('MIN_SOC', 80)
    AT_LIMIT = cfg.get('AT_LIMIT', 10.0)
    AUTO_MODE = int(cfg.get('auto_mode', 1))
    
    # Preis-Boost Konfiguration
    PRICE_BOOST_ENABLE = int(cfg.get('price_boost_enable', 0))
    PRICE_LIMIT = float(cfg.get('price_limit', 20.0)) # ct/kWh
    PRICE_MIN_DURATION = int(cfg.get('price_min_duration', 60)) # Minuten
    PRICE_MAX_DAILY = int(cfg.get('price_max_daily', 180)) # Minuten pro Tag
    PRICE_HARD_LIMIT = float(cfg.get('price_hard_limit', -99.0)) # Zwangs-Boost unter diesem Preis
    WQ_MIN_TEMP = float(cfg.get('wq_min_temp', 1.0)) # WQ Aus Schutz
    RL_SOURCE = cfg.get('rl_source', 'internal') # 'internal' or 'external'
    MANUAL_BOOST_MIN_SOC = int(cfg.get('manual_boost_min_soc', 25)) # SoC Schutz für manuellen Boost

    # Morning Boost Konfiguration
    MB_ENABLE = int(cfg.get('morning_boost_enable', 0))
    MB_PRIO = cfg.get('morning_boost_prio', 'wallbox')
    MB_WB_POWER = float(cfg.get('morning_boost_wb_power', 7.0))
    MB_MIN_HOURS = int(cfg.get('morning_boost_min_hours', 3))
    MB_MIN_PV_PCT = float(cfg.get('morning_boost_min_pv_pct', 50.0))
    MB_TARGET_SOC = int(cfg.get('morning_boost_target_soc', 20))
    MB_DEADLINE = int(cfg.get('morning_boost_deadline', 8)) # Stunde

    # Superintelligence Konfiguration
    SI_ENABLE = int(cfg.get('super_intelligence_enable', 0))
    SI_DEADLINE = int(cfg.get('super_intelligence_deadline', 8))

    # Telegram Konfiguration
    TELEGRAM_TOKEN = cfg.get('telegram_token', '')
    TELEGRAM_CHAT_ID = cfg.get('telegram_chat_id', '')

    # PV-Pause (Prognose-basiert)
    PV_PAUSE_ENABLE = int(cfg.get('pv_pause_enable', 0))
    PV_PAUSE_SOC = int(cfg.get('pv_pause_soc', 80)) # Mindest-SoC für Pause
    PV_PAUSE_WATT = float(cfg.get('pv_pause_watt', 3000.0)) # Erwartete Leistung
    PV_PAUSE_TIMEOUT_MINUTES = int(cfg.get('pv_pause_timeout_minutes', 120)) # Max. Dauer der Pause
    
    # Stop-Verzögerung: Wie lange darf Strom aus Netz/Akku gezogen werden?
    STOP_DELAY_MINUTES = int(cfg.get('stop_delay_minutes', 10))
    MANUAL_BOOST_MAX_DURATION = int(cfg.get('manual_boost_max_duration', 180))
    
    # IP Adresse
    boost_active = False
    price_boost_active = False
    pv_pause_active = False
    pv_pause_start_time = None
    pre_pause_active = False
    deficit_start_time = None # Timer für Abschaltung
    last_day = datetime.now().day
    first_run = True # Initial-Check Flag
    daily_boost_counter = 0 # Zähler für Preis-Boost Minuten
    last_pv_boost_time = 0 # Timestamp des letzten PV-Boosts
    last_price_warning_time = 0 # Für Log-Drosselung
    last_safety_check_time = 0 # Für Sicherheits-Log-Drosselung
    mb_state = "IDLE" # IDLE, PLANNED, RUNNING, DONE
    mb_running_prio = ""
    
    si_state = "IDLE" # IDLE, PLANNED, RUNNING, PAUSING, DONE
    si_calibrated_power_kw = None
    si_pause_end_ts = None
    si_power_check_counter = 0

    # Helper für Telegram
    def send_telegram(msg):
        # 1. Versuch: Zentrales Skript nutzen (installiert via Watchdog)
        notify_script = "/usr/local/bin/boot_notify.sh"
        if os.path.exists(notify_script):
            try:
                subprocess.run([notify_script, msg], timeout=10)
                return # Erfolgreich an Skript übergeben
            except Exception as e:
                print(f"Fehler bei boot_notify.sh: {e}")

        # 2. Versuch: Interne Config nutzen (Fallback)
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
                requests.post(url, data=data, timeout=5)
            except: pass

    # Versuche Zustand aus Ramdisk wiederherzustellen
    if os.path.exists(RAMDISK_FILE):
        try:
            with open(RAMDISK_FILE, 'r') as f:
                saved = json.load(f)
                if 'ts' in saved:
                    saved_ts = datetime.fromisoformat(saved['ts'])
                    if saved_ts.date() == datetime.now().date():
                        daily_boost_counter = saved.get('daily_boost_counter', 0)
                last_pv_boost_time = saved.get('last_pv_boost_time', 0)                
                if daily_boost_counter > 0 or last_pv_boost_time > 0:
                    logger.info(f"Status wiederhergestellt: Tageszähler ({daily_boost_counter} Min), letzter PV-Boost vor {(time.time() - last_pv_boost_time)/3600:.1f}h.")
                
                # NEU: Aktive Zustände wiederherstellen (Crash/Reboot Recovery)
                # Nur wenn Daten frisch sind (< 20 min)
                if (datetime.now() - saved_ts).total_seconds() < 1200:
                    if saved.get('boost_active'):
                        boost_active = True
                        pv_pause_active = saved.get('pv_pause_active', False)
                        price_boost_active = saved.get('price_boost_active', False)
                        pre_pause_active = saved.get('pre_pause_active', False)
                        if pv_pause_active: pv_pause_start_time = saved.get('pv_pause_start_time', time.time())
                        logger.info(f"Aktiven Status wiederhergestellt: Boost={boost_active}, Pause={pv_pause_active}")
        except: pass

    # Morning Boost State Recovery (Sicherheit bei Neustart)
    mb_restore_needed = False
    mb_original_wbmode = None
    mb_original_wbminsoc = None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                state_data = json.load(f)
                if state_data.get('mode') == 'morning_boost' and state_data.get('status') == 'RUNNING':
                    mb_state = 'RUNNING'
                    mb_running_prio = state_data.get('prio', '')
                    mb_restore_needed = True                    
                    logger.warning(f"Morning-Boost Status wiederhergestellt: RUNNING ({mb_running_prio})")
                elif state_data.get('mode') == 'super_intelligence':
                    si_state = state_data.get('status', 'IDLE')
                    if si_state != 'IDLE' and si_state != 'DONE':
                        si_calibrated_power_kw = state_data.get('calibrated_power')                        
                        si_pause_end_ts = state_data.get('pause_end_ts')
                        mb_restore_needed = True # nutzt den gleichen Restore-Mechanismus
                        logger.warning(f"Superintelligence Status wiederhergestellt: {si_state}")

        except: pass

    # Initiale Ramdisk-Datei
    init_json = {
        "ts": datetime.now().isoformat(),
        "success": False,
        "error": "Dienst startet gerade...",
        "data": {},
        "status": {}
    }
    with open(RAMDISK_FILE, 'w') as f:
        json.dump(init_json, f)
    try:
        os.chmod(RAMDISK_FILE, 0o664)
                # os.chown(RAMDISK_FILE, os.getuid(), os.getgid()) # Optional: Owner auf den ausführenden User setzen
    except: pass

    if AUTO_MODE == 0:
        logger.info("Automatik-Regelung ist DEAKTIVIERT (Nur Monitoring).")
    elif not wp:
        logger.info("Nur Lademanagement-Regeln sind aktiv.")
        # Deaktiviere die Haupt-Boost-Logik für die WP, wenn keine WP konfiguriert ist
        AUTO_MODE = 0
    else:
        logger.info(f"Start bei > {abs(GRID_START_LIMIT)}W Einspeisung.")
        logger.info(f"Stop nach {STOP_DELAY_MINUTES} min Bezug (Netz oder Batterie).")

    while True:
        now = datetime.now()
        wp_data = {}   
        wp_status = {} 
        at = 20.0 # Default
        success = False 
        wq_aus = 10.0 # Default sicher

        # Wenn keine WP konfiguriert ist, ist der Durchlauf erstmal erfolgreich
        if not wp:
            success = True

        try:
            # 1. Daten von Wärmepumpe holen (nur wenn aktiv)
            if wp:
                if wp.connect():
                    wp_data = wp.read_all_sensors()
                    time.sleep(0.5)
                    wp_status = wp.read_shi_status()
                    
                    # Versuchen WQ Aus zu lesen (Register muss in LuxtronikModbus definiert sein!)
                    wq_aus = wp_data.get('Sole_Aus', wp_data.get('Wärmequelle_Austritt', wp_data.get('WQ_Austritt', 10.0)))
                    
                    # Initial-Status beim Start prüfen
                    if first_run:
                        if os.path.exists(FLAG_FILE):                            
                            logger.info("Manueller Boost erkannt (Flag). Warte im Standby.")
                        # Die Logik zum Übernehmen eines Boosts wird entfernt.
                        # Stattdessen wird der Sicherheits-Check unten den Zustand korrigieren.
                        first_run = False
                    
                    # Sicherheits-Check: In jedem Durchlauf prüfen, ob die WP einen aktiven Setpoint hat,
                    # obwohl das Skript keinen Boost-Zustand verwaltet.
                    if not boost_active and not os.path.exists(FLAG_FILE) and (wp_status.get('WW_Mode') == 1 or wp_status.get('HZ_Mode') == 1):
                        # Nur alle 15 Minuten benachrichtigen, um Spam zu vermeiden
                        
                        # NEU: Smart-Check - Ist es vielleicht eine noch aktive Pause (20°C)?
                        hz_set = wp_status.get('HZ_Setpoint', 0)
                        if wp_status.get('HZ_Mode') == 1 and abs(hz_set - 20.0) < 1.0:
                             # Nur übernehmen, wenn Feature auch aktiviert ist
                             if PV_PAUSE_ENABLE == 1:
                                 logger.info("Erkenne aktiven Pause-Status an der WP (20°C). Übernehme Status.")
                                 boost_active = True
                                 pv_pause_active = True
                                 if not pv_pause_start_time: pv_pause_start_time = time.time()
                             else:
                                 # Feature ist aus -> Reset erzwingen (fällt in den else-Block unten)
                                 pass
                        else:
                            # Echter Fehler -> Reset
                            if (time.time() - last_safety_check_time) > 900:
                                msg = f"SICHERHEIT: Unerwarteter Boost-Status erkannt (WW-Mode={wp_status.get('WW_Mode')}, HZ-Mode={wp_status.get('HZ_Mode')}). Setze WP zurück."
                                logger.warning(msg)
                                send_telegram(f"⚠️ {msg}")
                                last_safety_check_time = time.time()
                            
                            # Reset mit validen Temperaturen
                            wp.write_hz_boost(0, 32.0)
                            wp.write_ww_boost(0, cfg.get('WWW', 45.0))
                        
                        # WICHTIG: Verbindung sauber schließen, bevor wir den Zyklus abbrechen!
                        wp.close()
                        success = True # Wir haben erfolgreich kommuniziert und gehandelt
                        
                        # Nach einem Sicherheits-Reset den Zyklus neu starten, um mit sauberen Werten zu arbeiten.
                        # Dies verhindert, dass die Pause-Logik im selben Zyklus sofort wieder zuschlägt.
                        time.sleep(15) # Warten auf nächsten Zyklus
                        continue

                    # Status-Korrektur: Falls WP extern (Display/App) auf "Auto" (0) gestellt wurde
                    if boost_active and wp_status:
                        if wp_status.get('WW_Mode') != 1 and wp_status.get('HZ_Mode') != 1:
                            logger.info("Boost-Modus wurde extern deaktiviert (Register=0). Reset Status.")
                            boost_active = False
                            deficit_start_time = None
                            price_boost_active = False
                            pre_pause_active = False
                            pv_pause_active = False # WICHTIG: Auch PV-Pause zurücksetzen
                    
                    wp.close()
                    success = True 
                else:
                    logger.warning("Verbindung zur WP fehlgeschlagen")

            # 2. Logik (Überschuss-Prüfung)
            
            # E3DC Daten holen (immer, da für Manual Boost SoC und Auto Mode benötigt)
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
            
            try:
                r = requests.get("http://localhost/get_live_json.php", timeout=5)
                if r.status_code == 200:
                    e3dc = r.json()
                    grid = e3dc.get('grid', 0) # + Import, - Export
                    bat = e3dc.get('bat', 0)   # + Laden, - Entladen
                    soc = e3dc.get('soc', 0)
                    wb_locked = e3dc.get('wb_locked', False)
                    current_price = e3dc.get('current_price', 99.9)
                    prices = e3dc.get('prices', [])
                    forecast = e3dc.get('forecast', [])
                    price_start_hour = e3dc.get('price_start_hour', 0)
                    price_interval = e3dc.get('price_interval', 1.0)
            except Exception as e:
                if AUTO_MODE == 1 or os.path.exists(FLAG_FILE):
                    logger.error(f"Fehler bei E3DC Abfrage: {e}")

            # Zuerst prüfen wir den manuellen Boost auf Limits (Zeit & WQ & SoC)
            if os.path.exists(FLAG_FILE) and wp:
                try:
                    # 1. WQ Schutz
                    if wq_aus < WQ_MIN_TEMP:
                        logger.warning(f"NOT-AUS (Manuell): WQ Aus zu kalt ({wq_aus}°C).")
                        if wp and wp.connect():
                            wp.write_hz_boost(0)
                            wp.write_ww_boost(0, cfg.get('WWW', 45.0))
                            wp.close()
                        os.remove(FLAG_FILE)
                    # 2. SoC Schutz (NEU)
                    elif soc < MANUAL_BOOST_MIN_SOC:                        
                        logger.info(f"Manueller Boost gestoppt: SoC zu niedrig ({soc}% < {MANUAL_BOOST_MIN_SOC}%).")
                        if wp and wp.connect():
                            wp.write_hz_boost(0)
                            wp.write_ww_boost(0, cfg.get('WWW', 45.0))
                            wp.close()
                        os.remove(FLAG_FILE)
                    # 2. Zeit-Limit
                    elif (time.time() - os.path.getmtime(FLAG_FILE)) > (MANUAL_BOOST_MAX_DURATION * 60):
                        logger.info(f"Manueller Boost abgelaufen (> {MANUAL_BOOST_MAX_DURATION/60:.1f}h).")
                        if wp and wp.connect():
                            wp.write_hz_boost(0)
                            wp.write_ww_boost(0, cfg.get('WWW', 45.0))
                            wp.close()
                        os.remove(FLAG_FILE)
                except Exception as e:
                    logger.error(f"Fehler bei Manual-Boost Check: {e}")

            # --- SUPERINTELLIGENCE LOGIK ---
            if SI_ENABLE == 1:
                si_target_soc = MANUAL_BOOST_MIN_SOC
                si_deadline_ts = datetime(now.year, now.month, now.day, SI_DEADLINE, 0, 0)

                # 0. Pausen-Management
                if si_state == "PAUSING" and si_pause_end_ts and now >= datetime.fromtimestamp(si_pause_end_ts):
                    logger.info("Superintelligence: Pause beendet, setze Ladung fort.")
                    write_e3dc_config_value('wbmode', 10)
                    si_state = "RUNNING"

                # 1. Planung
                if si_state == "IDLE" and now < si_deadline_ts:
                    high_hours, pv_sum = get_forecast_data()
                    condition_hours = high_hours >= 8.0
                    condition_yield = pv_sum >= 1.5 * (100 - si_target_soc)
                    if condition_hours and condition_yield:                        
                        logger.info(f"Superintelligence geplant! (Prognose: {high_hours}h voll, {pv_sum:.1f}% PV, Ziel-SoC {si_target_soc}%)")
                        si_state = "PLANNED"
                
                # 2. Start-Berechnung & Ausführung
                if si_state == "PLANNED":
                    cap_kwh = float(read_e3dc_config_value('speichergroesse') or 10.0)
                    delta_soc = max(0, soc - si_target_soc)
                    energy_needed_kwh = (delta_soc / 100.0) * cap_kwh
                    
                    # Annahme: 7kW + Hauslast
                    assumed_power_kw = MB_WB_POWER + 0.5
                    duration_h = energy_needed_kwh / assumed_power_kw if assumed_power_kw > 0 else 0
                    start_ts = si_deadline_ts - timedelta(hours=duration_h)
                    
                    if now >= start_ts:
                        logger.info(f"Starte Superintelligence. Ziel: {si_target_soc}% SoC bis {SI_DEADLINE}:00.")
                        orig_mode = read_e3dc_config_value('wbmode')
                        orig_min = read_e3dc_config_value('wbminsoc')
                        with open(STATE_FILE, 'w') as f:
                            json.dump({
                                'mode': 'super_intelligence', 'status': 'RUNNING',
                                'orig_wbmode': orig_mode, 'orig_wbminsoc': orig_min
                            }, f)
                        
                        # Berechtigungen für die State-Datei setzen
                        try: os.chmod(STATE_FILE, 0o666)
                        except: pass

                        write_e3dc_config_value('wbminsoc', si_target_soc)
                        write_e3dc_config_value('wbmode', 10)
                        si_state = "RUNNING"
                        si_power_check_counter = 0
                        si_calibrated_power_kw = None

                # 3. Laufende Überwachung, Kalibrierung & Stopp
                if si_state == "RUNNING":
                    stop_boost = (soc <= (si_target_soc + 1)) or (now >= si_deadline_ts)
                    
                    if stop_boost:
                        logger.info("Superintelligence beendet.")
                        if os.path.exists(STATE_FILE):
                            with open(STATE_FILE, 'r') as f: saved_state = json.load(f)
                            write_e3dc_config_value('wbmode', saved_state.get('orig_wbmode', 4))
                            write_e3dc_config_value('wbminsoc', saved_state.get('orig_wbminsoc', 50))
                            os.remove(STATE_FILE)
                        si_state = "DONE"
                    else:
                        # Power-Kalibrierung
                        actual_wb_power_w = e3dc.get('wb', 0)
                        if si_calibrated_power_kw is None and actual_wb_power_w > 1000:
                            si_power_check_counter += 1
                            if si_power_check_counter >= 3: # Stabil nach ca. 45s
                                si_calibrated_power_kw = actual_wb_power_w / 1000.0
                                logger.info(f"Superintelligence: Ladeleistung auf {si_calibrated_power_kw:.2f} kW kalibriert.")
                                with open(STATE_FILE, 'r+') as f:
                                    d = json.load(f)
                                    d['calibrated_power'] = si_calibrated_power_kw
                                    f.seek(0); f.truncate(); json.dump(d, f)
                        elif actual_wb_power_w < 1000:
                            si_power_check_counter = 0

                        # Zeitfenster-Anpassung
                        if si_calibrated_power_kw is not None:
                            cap_kwh = float(read_e3dc_config_value('speichergroesse') or 10.0)
                            remaining_energy = ((soc - si_target_soc) / 100.0) * cap_kwh
                            power_kw = si_calibrated_power_kw + 0.5 # inkl. Hauslast
                            needed_h = remaining_energy / power_kw if power_kw > 0 else 99
                            
                            required_start_ts = si_deadline_ts - timedelta(hours=needed_h)

                            if now < required_start_ts:
                                pause_seconds = (required_start_ts - now).total_seconds()
                                if pause_seconds > 120: # Nur bei >2min Pause                                    
                                    logger.info(f"Superintelligence: Ladeplan angepasst. Pausiere für {pause_seconds/60:.1f} Min.")
                                    si_pause_end_ts = time.time() + pause_seconds
                                    si_state = "PAUSING"
                                    
                                    with open(STATE_FILE, 'r+') as f:
                                        d = json.load(f)
                                        d['status'] = 'PAUSING'
                                        d['pause_end_ts'] = si_pause_end_ts
                                        f.seek(0); f.truncate(); json.dump(d, f)
                                        # Original-Modus wiederherstellen zum Pausieren
                                        # Berechtigungen für die State-Datei setzen
                                        try: os.chmod(STATE_FILE, 0o666)
                                        except: pass

                                        write_e3dc_config_value('wbmode', d.get('orig_wbmode', 4))

            # Tages-Reset
            if now.hour == 0 and now.minute < 2 and (si_state == "DONE" or si_state == "IDLE"):
                si_state = "IDLE"
                si_calibrated_power_kw = None

            # --- MORNING BOOST LOGIK (Nur wenn Superintelligence nicht aktiv ist) ---
            elif MB_ENABLE == 1:
                # 1. Planung (Nur wenn IDLE und wir Daten haben)
                if mb_state == "IDLE":
                    # Prüfen ob wir schon fertig sind für heute (Reset um Mitternacht nötig, hier einfach Timer-Check)
                    # Wir prüfen einfach: Ist es vor der Deadline?
                    if now.hour < MB_DEADLINE:
                        high_hours, pv_sum = get_forecast_data()
                        if high_hours >= MB_MIN_HOURS and pv_sum >= MB_MIN_PV_PCT:                            
                            logger.info(f"Morning-Boost geplant! (Prognose: {high_hours}h voll, {pv_sum:.1f}% PV)")
                            mb_state = "PLANNED"
                
                # 2. Start-Berechnung & Ausführung
                if mb_state == "PLANNED":
                    # Priorität prüfen und ob gestartet werden kann
                    effective_prio = None
                    if MB_PRIO == 'wallbox':                        
                        if wb_locked:
                            effective_prio = 'wallbox'
                        elif wp: # Fallback nur wenn WP existiert
                            logger.info("Morning-Boost: Wallbox priorisiert, aber nicht verbunden. Wechsle zu Wärmepumpe.")
                            effective_prio = 'heatpump'
                    elif MB_PRIO == 'wallbox_only':
                        if wb_locked:
                            effective_prio = 'wallbox'
                        else:
                            logger.info("Morning-Boost (Nur Wallbox): Auto nicht verbunden. Warte...")
                    elif MB_PRIO == 'heatpump' and wp:
                        effective_prio = 'heatpump'

                    # Nur fortfahren, wenn eine gültige Priorität ermittelt wurde
                    if effective_prio:
                        # Kapazität lesen
                        cap_kwh = float(read_e3dc_config_value('speichergroesse') or 10.0)
                        
                        # Energiebedarf berechnen
                        delta_soc = max(0, soc - MB_TARGET_SOC)
                        energy_needed_kwh = (delta_soc / 100.0) * cap_kwh
                        
                        # Leistungsschätzung
                        power_kw = 0.5 # Grundlast Haus
                        
                        if effective_prio == 'wallbox':
                            power_kw += MB_WB_POWER
                        else: # heatpump
                            # WP Max Leistung aus Config lesen
                            wp_max = float(read_e3dc_config_value('wpmax') or 4.0)
                            power_kw += wp_max
                        
                        duration_h = energy_needed_kwh / power_kw if power_kw > 0 else 0
                        
                        # Startzeitpunkt
                        deadline_ts = datetime(now.year, now.month, now.day, MB_DEADLINE, 0, 0)
                        start_ts = deadline_ts - timedelta(hours=duration_h)
                        
                        # Starten?
                        if now >= start_ts:
                            logger.info(f"Starte Morning-Boost ({effective_prio}). Ziel: {MB_TARGET_SOC}% SoC bis {MB_DEADLINE}:00.")
                            
                            if effective_prio == 'wallbox':
                                # Aktuelle Werte sichern
                                orig_mode = read_e3dc_config_value('wbmode')
                                orig_min = read_e3dc_config_value('wbminsoc')
                                
                                # State sichern
                                with open(STATE_FILE, 'w') as f: # Überschreibt, da neuer Vorgang
                                    json.dump({
                                        'mode': 'morning_boost', 'status': 'RUNNING',
                                        'prio': 'wallbox',
                                        'orig_wbmode': orig_mode,
                                        'orig_wbminsoc': orig_min
                                    }, f)
                                
                                # Berechtigungen für die State-Datei setzen
                                try: os.chmod(STATE_FILE, 0o666)
                                except: pass

                                # E3DC Config ändern
                                write_e3dc_config_value('wbminsoc', MB_TARGET_SOC)
                                write_e3dc_config_value('wbmode', 10) # Entladen
                            
                            else: # heatpump
                                # State sichern
                                with open(STATE_FILE, 'w') as f: # Überschreibt
                                    json.dump({'mode': 'morning_boost', 'status': 'RUNNING', 'prio': 'heatpump'}, f)
                                
                                # Berechtigungen für die State-Datei setzen
                                try: os.chmod(STATE_FILE, 0o666)
                                except: pass

                                # Manuellen Boost Flag setzen (wird oben verarbeitet)
                                with open(FLAG_FILE, 'w') as f: f.write("1")
                                
                            mb_running_prio = effective_prio
                            mb_state = "RUNNING"

                # 3. Überwachung & Stop
                if mb_state == "RUNNING":
                    # Abbruchbedingungen: SoC erreicht ODER Zeit abgelaufen
                    stop_boost = False
                    if soc <= (MB_TARGET_SOC + 1): stop_boost = True
                    if now.hour >= MB_DEADLINE: stop_boost = True
                    
                    if stop_boost:
                        logger.info("Morning-Boost beendet.")
                        
                        # Wiederherstellen
                        if os.path.exists(STATE_FILE):
                            with open(STATE_FILE, 'r') as f:
                                saved_state = json.load(f)
                            
                            if saved_state.get('mode') == 'morning_boost' and saved_state.get('prio') == 'wallbox':
                                write_e3dc_config_value('wbmode', saved_state.get('orig_wbmode', 4))
                                write_e3dc_config_value('wbminsoc', saved_state.get('orig_wbminsoc', 50))
                            else:
                                # WP Boost stoppen
                                if os.path.exists(FLAG_FILE) and wp: os.remove(FLAG_FILE)
                                if wp and wp.connect():
                                    wp.write_hz_boost(0)
                                    wp.write_ww_boost(0)
                                    wp.close()
                            
                            os.remove(STATE_FILE)
                        mb_state = "DONE"
                        mb_running_prio = ""

            if not os.path.exists(FLAG_FILE) and AUTO_MODE == 1 and wp:
                try:
                    r = requests.get("http://localhost/get_live_json.php", timeout=5)
                    if r.status_code == 200:
                        e3dc = r.json()
                        grid = e3dc.get('grid', 0) # + Import, - Export
                        bat = e3dc.get('bat', 0)   # + Laden, - Entladen
                        soc = e3dc.get('soc', 0)
                        at = wp_data.get('Aussentemp_Mittel', 20.0) # Wert aktualisieren
                        
                        # Strompreis (aktuell)
                        current_price = e3dc.get('current_price', 99.9) # Muss von get_live_json kommen
                        # Falls get_live_json keinen Preis liefert, müsste man ihn hier anders holen.
                        # Wir nehmen an, E3DC stellt ihn bereit oder wir lesen ihn aus der awattardebug.

                        # Defizit-Erkennung:
                        # Wir verbrauchen Reserven, wenn wir aus dem Netz beziehen (>50W Puffer)
                        # ODER wenn wir die Batterie entladen (bat < -50W)
                        is_deficit = (grid > 50) or (bat < -50)

                        # --- PV PAUSE LOGIK (Prognose) ---
                        # Diese Logik hat Vorrang vor Preis-Boost, um kostenlose Energie zu priorisieren.
                        # Wird bei negativen Preisen übersprungen, da Netzbezug dann gewünscht ist.
                        if PV_PAUSE_ENABLE == 1 and current_price > 0 and mb_state != "RUNNING" and si_state != "RUNNING" and si_state != "PAUSING":

                            # Fall A: Wir sind bereits in der PV-Pause
                            if pv_pause_active:
                                # Abbruchbedingung 1: Überschuss ist JETZT da -> Sofort Boost starten
                                if grid <= GRID_START_LIMIT:                                    
                                    logger.info(f"PV-Pause beendet -> Überschuss da ({grid}W). Übergebe an Boost-Logik.")
                                    pv_pause_active = False
                                    boost_active = False # Damit die PV-Boost Logik unten greift
                                
                                # Abbruchbedingung 2: SoC fällt zu tief (Sicherheitsnetz)
                                elif soc < (PV_PAUSE_SOC - 5):                                    
                                    logger.warning(f"PV-Pause abgebrochen (SoC {soc}% < {PV_PAUSE_SOC-5}%).")
                                    if wp and wp.connect(): wp.write_hz_boost(0); wp.write_ww_boost(0, cfg.get('WWW', 45.0)); wp.close()
                                    pv_pause_active = False; boost_active = False; pv_pause_start_time = None

                                # Abbruchbedingung 3: Timeout (Sonne kam nicht)
                                elif pv_pause_start_time and (time.time() - pv_pause_start_time) > (PV_PAUSE_TIMEOUT_MINUTES * 60):                                    
                                    logger.warning(f"PV-Pause abgebrochen (Timeout > {PV_PAUSE_TIMEOUT_MINUTES} Min).")
                                    if wp and wp.connect(): wp.write_hz_boost(0); wp.write_ww_boost(0, cfg.get('WWW', 45.0)); wp.close()
                                    pv_pause_active = False; boost_active = False; pv_pause_start_time = None
                                
                                # Abbruchbedingung 4: Prognose-Check (Ist der Grund für die Pause noch da?)
                                elif forecast:
                                    gmt = time.gmtime()
                                    now_gmt = gmt.tm_hour + gmt.tm_min / 60.0
                                    peak_still_valid = False
                                    for entry in forecast:
                                        h = entry['h']
                                        if h < (now_gmt - 12): h += 24
                                        # Prüfe ob Peak in den nächsten 1.5h (oder aktuell) liegt
                                        if now_gmt < h <= (now_gmt + 1.5):
                                            if entry['w'] >= PV_PAUSE_WATT:
                                                peak_still_valid = True
                                                break
                                    if not peak_still_valid:
                                        logger.info(f"PV-Pause beendet (Prognose-Grund entfallen).")
                                        if wp and wp.connect(): wp.write_hz_boost(0); wp.write_ww_boost(0, cfg.get('WWW', 45.0)); wp.close()
                                        pv_pause_active = False; boost_active = False; pv_pause_start_time = None

                            # Fall B: Wir prüfen, ob wir pausieren sollten (noch kein Boost aktiv)
                            elif not boost_active and soc >= PV_PAUSE_SOC:
                                # Prognose prüfen: Kommt in den nächsten 90 Min viel Sonne?
                                peak_found = False
                                if forecast:
                                    gmt = time.gmtime()
                                    now_gmt = gmt.tm_hour + gmt.tm_min / 60.0
                                    
                                    for entry in forecast:
                                        h = entry['h']
                                        if h < (now_gmt - 12): h += 24 
                                        
                                        if now_gmt < h <= (now_gmt + 1.5):
                                            if entry['w'] >= PV_PAUSE_WATT:
                                                peak_found = True
                                                break
                                
                                if peak_found:
                                    logger.info(f"Starte PV-Pause (Prognose > {PV_PAUSE_WATT}W erwartet).")
                                    if wp and wp.connect():
                                        wp.write_hz_boost(1, 20.0) # Heizung unterdrücken
                                        wp.write_ww_boost(0, cfg.get('WWW', 45.0))
                                        wp.close()
                                    pv_pause_active = True
                                    boost_active = True
                                    pv_pause_start_time = time.time()

                        # --- PREIS BOOST LOGIK ---
                        # Diese Logik hat Vorrang vor PV-Boost, wenn aktiviert
                        price_action = "NONE"

                        if PRICE_BOOST_ENABLE == 1 and mb_state != "RUNNING" and si_state != "RUNNING" and si_state != "PAUSING":
                            # Sonderfall: Bei negativen Preisen immer "BOOST" erzwingen
                            if current_price <= 0:
                                price_action = "BOOST"
                                logger.info(f"Negativer Strompreis erkannt ({current_price} ct). Erzwinge Boost.")
                            else:
                                # Aktuelle Zeit im Preis-Raster ermitteln
                                if prices:
                                    gmt = time.gmtime()
                                    now_gmt_dec = gmt.tm_hour + gmt.tm_min / 60.0
                                    hour_diff = now_gmt_dec - price_start_hour
                                    if hour_diff < -12: hour_diff += 24
                                    if hour_diff > 36: hour_diff -= 24

                                    current_idx_float = hour_diff / price_interval

                                    # Aktion ermitteln (BOOST, PAUSE oder NONE)
                                    price_action = get_price_action(prices, price_start_hour, price_interval, PRICE_LIMIT, PRICE_MIN_DURATION, current_idx_float)
                                elif (time.time() - last_price_warning_time) > 3600:
                                    # Warnung nur 1x pro Stunde                                    
                                    logger.warning("Preis-Boost aktiv, aber keine Preisdaten (prices=[]) empfangen.")
                                    last_price_warning_time = time.time()

                            # Constraints prüfen (WQ, Tageslimit, 18h Sperre, Hard-Limit)
                            # Hard-Limit: Bei extrem tiefen Preisen (z.B. negativ) Limits ignorieren
                            is_hard_boost = (current_price <= PRICE_HARD_LIMIT)

                            if wq_aus < WQ_MIN_TEMP:
                                if price_boost_active or pre_pause_active:
                                    logger.warning(f"NOT-AUS: WQ Aus zu kalt ({wq_aus}°C).")
                                price_action = "NONE"
                            elif daily_boost_counter >= PRICE_MAX_DAILY and price_action == "BOOST" and not is_hard_boost:
                                price_action = "NONE"
                            elif (time.time() - last_pv_boost_time) <= (18 * 3600) and price_action != "NONE" and not is_hard_boost:
                                # Sperre aktiv (PV-Boost war erst kürzlich) -> Keine Aktion
                                price_action = "NONE"

                        # --- AUSFÜHRUNG PREIS LOGIK ---
                        if price_action == "PAUSE":
                            if not pre_pause_active:
                                logger.info(f"Start Preis-Pause (Erholung vor Boost). WQ: {wq_aus}°C")
                                if wp and wp.connect():
                                    wp.write_hz_boost(1, 20.0) # Heizung auf 20°C zwingen (Pause)
                                    wp.write_ww_boost(0, cfg.get('WWW', 45.0))       # WW Normal
                                    wp.close()
                                pre_pause_active = True
                                price_boost_active = False
                                boost_active = True # Wir kontrollieren die WP
                                
                        elif price_action == "BOOST":
                            if not price_boost_active:
                                logger.info(f"Start Preis-Boost (Preis: {current_price} ct). WQ: {wq_aus}°C")
                                if wp and wp.connect():
                                    if at > AT_LIMIT:
                                        # Sommer-Settings
                                        wp.write_ww_boost(1, cfg.get('WWS', 50.0))
                                        wp.write_hz_boost(0) # Heizung aus
                                    else:
                                        # Winter-Settings
                                        wp.write_ww_boost(1, cfg.get('WWW', 48.0))
                                        wp.write_hz_boost(1, cfg.get('HZ', 50.0))
                                    wp.close()
                                price_boost_active = True
                                pre_pause_active = False
                                boost_active = True
                            
                            daily_boost_counter += 0.5 # +30 Sekunden
                            
                        elif (price_boost_active or pre_pause_active) and price_action == "NONE":
                            # Ende von Preis-Aktionen (oder Feature deaktiviert)
                            logger.info("Ende Preis-Steuerung.")
                            if wp and wp.connect():
                                wp.write_hz_boost(0)
                                wp.write_ww_boost(0, cfg.get('WWW', 45.0))
                                wp.close()
                            price_boost_active = False
                            pre_pause_active = False
                            boost_active = False

                        # --- PV BOOST LOGIK (Nur wenn kein Preis-Boost aktiv) ---
                        if not boost_active and mb_state != "RUNNING" and si_state != "RUNNING" and si_state != "PAUSING":
                            # EINSCHALTEN: Genug Überschuss und Batterie voll genug
                            if grid <= GRID_START_LIMIT and soc >= MIN_SOC and wp:
                                    logger.info(f"Start PV-Boost (Grid: {grid}W, SoC: {soc}%)")
                                    if wp.connect():
                                        if at > AT_LIMIT:
                                            wp.write_ww_boost(1, cfg.get('WWS', 50.0))
                                            wp.write_hz_boost(0) # Heizung im Sommer sicherheitshalber aus
                                        else:
                                            wp.write_ww_boost(1, cfg.get('WWW', 48.0))
                                            wp.write_hz_boost(1, cfg.get('HZ', 50.0))
                                        wp.close()
                                    boost_active = True
                                    deficit_start_time = None # Timer sicherheitshalber nullen

                        # LAUFENDE ÜBERWACHUNG (Wenn Boost aktiv ist)
                        if boost_active:
                            # Identifiziere reinen PV-Boost
                            is_standard_pv_boost = (not price_boost_active) and (not pre_pause_active) and (not pv_pause_active)
                            
                            if is_standard_pv_boost:
                                # Timestamp für 18h-Sperre aktualisieren
                                last_pv_boost_time = time.time()

                                # SYNC-CHECK: Prüfen ob Werte mit Config übereinstimmen
                                # (Wichtig nach Neustart oder Config-Änderung)
                                target_ww = cfg.get('WWS', 50.0) if at > AT_LIMIT else cfg.get('WWW', 48.0)
                                current_ww = wp_status.get('WW_Setpoint', 0)
                                
                                sync_needed = abs(current_ww - target_ww) > 0.5

                                # Im Winter auch Heizung prüfen
                                if at <= AT_LIMIT:
                                    target_hz = cfg.get('HZ', 50.0)
                                    current_hz = wp_status.get('HZ_Setpoint', 0)
                                    if abs(current_hz - target_hz) > 0.5:
                                        sync_needed = True
                                
                                if sync_needed:
                                    logger.info("Korrigiere Boost-Werte (Sync Check)")
                                    if wp.connect():
                                        if at > AT_LIMIT:
                                            wp.write_ww_boost(1, cfg.get('WWS', 50.0))
                                            wp.write_hz_boost(0)
                                        else:
                                            wp.write_ww_boost(1, cfg.get('WWW', 48.0))
                                            wp.write_hz_boost(1, cfg.get('HZ', 50.0))
                                        wp.close()

                            # AUSSCHALTEN PRÜFEN (Nur bei reinem PV-Boost relevant)
                            # Preis-Boost, Pausen etc. dürfen Netzbezug haben.
                            # Wir prüfen auch boost_active, um unnötige Logik im Standby zu vermeiden.
                            if is_deficit and not price_boost_active and not pre_pause_active and not pv_pause_active:
                                # Wenn wir gerade erst ins Defizit rutschen -> Timer starten
                                if deficit_start_time is None:
                                    deficit_start_time = now                                    
                                    logger.info(f"Defizit erkannt (Grid: {grid}W, Bat: {bat}W). Timer gestartet.")
                                
                                # Wenn Timer läuft -> Prüfen ob Zeit abgelaufen
                                elif (now - deficit_start_time).total_seconds() > (STOP_DELAY_MINUTES * 60):                                    
                                    logger.info(f"Stop PV-Boost nach {STOP_DELAY_MINUTES} min Defizit.")
                                    if wp and wp.connect():                                        
                                        wp.write_ww_boost(0, 45.0) # Reset auf Standard
                                        wp.write_hz_boost(0)
                                        wp.close()
                                        boost_active = False
                                        deficit_start_time = None
                            else:
                                # Kein Defizit (wir speisen ein oder laden Batterie) -> Timer reset
                                if deficit_start_time is not None:
                                    logger.info("Defizit beendet (Sonne ist zurück). Timer reset.")
                                deficit_start_time = None

                except Exception as req_err:
                    logger.error(f"Fehler bei E3DC Abfrage: {req_err}")

            # 3. Daten schreiben (Ramdisk & History)
            json_export = {
                "ts": now.isoformat(),
                "data": wp_data,
                "status": wp_status,
                "boost_active": boost_active,
                "auto_mode": AUTO_MODE,
                "wq_aus": wq_aus,
                "daily_boost_counter": daily_boost_counter,
                "last_pv_boost_time": last_pv_boost_time,
                "price_boost_active": price_boost_active,
                "pre_pause_active": pre_pause_active,
                "pv_pause_start_time": pv_pause_start_time,
                "pv_pause_active": pv_pause_active,
                "mb_state": mb_state,
                "mb_prio": mb_running_prio,
                "si_state": si_state,
                "success": success
            }

            # Atomares Schreiben
            tmp_file = RAMDISK_FILE + ".tmp"
            with open(tmp_file, 'w') as f:
                json.dump(json_export, f)
            try:
                os.chmod(tmp_file, 0o664)
                # Owner/Group erben (für Permission-Check Konsistenz)
                st = os.stat(os.path.dirname(RAMDISK_FILE))
                os.chown(tmp_file, st.st_uid, st.st_gid)
            except: pass
            os.replace(tmp_file, RAMDISK_FILE)

            with open(HISTORY_FILE, 'a') as f:
                f.write(json.dumps(json_export) + "\n")

            # 4. Tageswechsel & Backup
            if now.day != last_day:
                yesterday = now - timedelta(days=1)
                backup_path = os.path.join(BACKUP_DIR, f"luxtronik_{yesterday.strftime('%Y-%m-%d')}.json")
                if os.path.exists(HISTORY_FILE):
                    shutil.copy(HISTORY_FILE, backup_path)
                    open(HISTORY_FILE, 'w').close()
                
                # Reset Morning Boost State für den neuen Tag
                if mb_state == "DONE":
                    mb_state = "IDLE"
                if si_state == "DONE":
                    si_state = "IDLE"
                daily_boost_counter = 0 # Reset des Tageszählers
                last_day = now.day

        except Exception as e:
            logger.critical(f"Kritischer Fehler im Haupt-Loop: {e}", exc_info=True)
            error_json = {"success": False, "error": str(e), "ts": now.isoformat()}
            with open(RAMDISK_FILE, 'w') as f:
                json.dump(error_json, f)
            try:
                os.chmod(RAMDISK_FILE, 0o664)
            except: pass
        
        time.sleep(15)

if __name__ == "__main__":
    main()
