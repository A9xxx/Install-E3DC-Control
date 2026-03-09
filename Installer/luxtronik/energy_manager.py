import time
import json
import os
import shutil
import math
from datetime import datetime, timedelta
import requests
from luxtronik import LuxtronikModbus

# Pfade
script_dir = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(script_dir, "config.lux.json")
RAMDISK_FILE = "/var/www/html/ramdisk/luxtronik.json"
HISTORY_FILE = "/var/www/html/ramdisk/luxtronik_history.json"
BACKUP_DIR = "/var/www/html/tmp/luxtronik_archive"
FLAG_FILE = "/var/www/html/ramdisk/manual_boost.flag"

# Verzeichnis für Archiv erstellen
if not os.path.exists(BACKUP_DIR):
    os.makedirs(BACKUP_DIR)

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    return {}

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
    cfg = load_config()

    # Prüfen ob Luxtronik aktiviert ist (Standard: 0/Aus)
    if int(cfg.get('luxtronik', 0)) != 1:
        print(f"Luxtronik ist deaktiviert (luxtronik!=1 in {CONFIG_PATH}).")
        return

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

    # PV-Pause (Prognose-basiert)
    PV_PAUSE_ENABLE = int(cfg.get('pv_pause_enable', 0))
    PV_PAUSE_SOC = int(cfg.get('pv_pause_soc', 80)) # Mindest-SoC für Pause
    PV_PAUSE_WATT = float(cfg.get('pv_pause_watt', 3000.0)) # Erwartete Leistung
    PV_PAUSE_TIMEOUT_MINUTES = int(cfg.get('pv_pause_timeout_minutes', 120)) # Max. Dauer der Pause
    
    # Stop-Verzögerung: Wie lange darf Strom aus Netz/Akku gezogen werden?
    STOP_DELAY_MINUTES = int(cfg.get('stop_delay_minutes', 10))
    MANUAL_BOOST_MAX_DURATION = int(cfg.get('manual_boost_max_duration', 180))
    
    # IP Adresse
    wp_ip = cfg.get('luxtronik_ip', "192.168.178.88")
    wp = LuxtronikModbus(wp_ip)
    
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
                
                if saved.get('price_boost_active', False):
                    price_boost_active = True
                    boost_active = True
                    print(f"Status wiederhergestellt: Preis-Boost aktiv.")
                if saved.get('pre_pause_active', False):
                    pre_pause_active = True
                    boost_active = True
                    print(f"Status wiederhergestellt: Pause aktiv.")
                if saved.get('pv_pause_active', False):
                    pv_pause_active = True
                    boost_active = True
                    pv_pause_start_time = saved.get('pv_pause_start_time', time.time()) # Fallback auf jetzt
                    print(f"Status wiederhergestellt: PV-Pause (Prognose) aktiv.")
                print(f"Status wiederhergestellt: PV-Boost vor {(time.time() - last_pv_boost_time)/3600:.1f}h")
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
    except: pass

    print(f"Energy-Manager gestartet...")
    if AUTO_MODE == 0:
        print("Automatik-Regelung ist DEAKTIVIERT (Nur Monitoring).")
    else:
        print(f"Start bei > {abs(GRID_START_LIMIT)}W Einspeisung.")
        print(f"Stop nach {STOP_DELAY_MINUTES} min Bezug (Netz oder Batterie).")

    while True:
        now = datetime.now()
        wp_data = {}   
        wp_status = {} 
        at = 20.0 # Default
        success = False 
        wq_aus = 10.0 # Default sicher

        try:
            # 1. Daten von Wärmepumpe holen
            if wp.connect():
                wp_data = wp.read_all_sensors()
                time.sleep(0.5)
                wp_status = wp.read_shi_status()
                
                # Versuchen WQ Aus zu lesen (Register muss in LuxtronikModbus definiert sein!)
                wq_aus = wp_data.get('Sole_Aus', wp_data.get('Wärmequelle_Austritt', wp_data.get('WQ_Austritt', 10.0)))
                
                # Initial-Status beim Start prüfen
                if first_run:
                    if os.path.exists(FLAG_FILE):
                        print(f"[{now.strftime('%H:%M:%S')}] Manueller Boost erkannt (Flag). Warte im Standby.")
                        boost_active = False
                    elif AUTO_MODE == 1 and (wp_status.get('WW_Mode') == 1 or wp_status.get('HZ_Mode') == 1):
                        print(f"[{now.strftime('%H:%M:%S')}] WP ist bereits im Boost-Modus. Übernehme Status.")
                        boost_active = True
                    first_run = False
                
                # Status-Korrektur: Falls WP extern (Display/App) auf "Auto" (0) gestellt wurde
                if boost_active and wp_status:
                    if wp_status.get('WW_Mode') != 1 and wp_status.get('HZ_Mode') != 1:
                        print(f"[{now.strftime('%H:%M:%S')}] Boost-Modus wurde extern deaktiviert (Register=0). Reset Status.")
                        boost_active = False
                        deficit_start_time = None
                        price_boost_active = False
                        pre_pause_active = False
                
                wp.close()
                success = True 
            else:
                print(f"[{now.strftime('%H:%M:%S')}] Verbindung zur WP fehlgeschlagen")

            # 2. Logik (Überschuss-Prüfung)
            
            # E3DC Daten holen (immer, da für Manual Boost SoC und Auto Mode benötigt)
            grid = 0
            bat = 0
            soc = 0
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
                    current_price = e3dc.get('current_price', 99.9)
                    prices = e3dc.get('prices', [])
                    forecast = e3dc.get('forecast', [])
                    price_start_hour = e3dc.get('price_start_hour', 0)
                    price_interval = e3dc.get('price_interval', 1.0)
            except Exception as e:
                if AUTO_MODE == 1 or os.path.exists(FLAG_FILE):
                    print(f"Fehler bei E3DC Abfrage: {e}")

            # Zuerst prüfen wir den manuellen Boost auf Limits (Zeit & WQ & SoC)
            if os.path.exists(FLAG_FILE):
                try:
                    # 1. WQ Schutz
                    if wq_aus < WQ_MIN_TEMP:
                        print(f"[{now.strftime('%H:%M:%S')}] NOT-AUS (Manuell): WQ Aus zu kalt ({wq_aus}°C).")
                        if wp.connect():
                            wp.write_hz_boost(0)
                            wp.write_ww_boost(0)
                            wp.close()
                        os.remove(FLAG_FILE)
                    # 2. SoC Schutz (NEU)
                    elif soc < MANUAL_BOOST_MIN_SOC:
                        print(f"[{now.strftime('%H:%M:%S')}] Manueller Boost gestoppt: SoC zu niedrig ({soc}% < {MANUAL_BOOST_MIN_SOC}%).")
                        if wp.connect():
                            wp.write_hz_boost(0)
                            wp.write_ww_boost(0)
                            wp.close()
                        os.remove(FLAG_FILE)
                    # 2. Zeit-Limit
                    elif (time.time() - os.path.getmtime(FLAG_FILE)) > (MANUAL_BOOST_MAX_DURATION * 60):
                        print(f"[{now.strftime('%H:%M:%S')}] Manueller Boost abgelaufen (> {MANUAL_BOOST_MAX_DURATION/60:.1f}h).")
                        if wp.connect():
                            wp.write_hz_boost(0)
                            wp.write_ww_boost(0)
                            wp.close()
                        os.remove(FLAG_FILE)
                except Exception as e:
                    print(f"Fehler bei Manual-Boost Check: {e}")

            if not os.path.exists(FLAG_FILE) and AUTO_MODE == 1:
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
                        if PV_PAUSE_ENABLE == 1 and current_price > 0:

                            # Fall A: Wir sind bereits in der PV-Pause
                            if pv_pause_active:
                                # Abbruchbedingung 1: Überschuss ist JETZT da -> Sofort Boost starten
                                if grid <= GRID_START_LIMIT:
                                    print(f"[{now.strftime('%H:%M:%S')}] PV-Pause beendet -> Überschuss da ({grid}W). Übergebe an Boost-Logik.")
                                    pv_pause_active = False
                                    boost_active = False # Damit die PV-Boost Logik unten greift
                                
                                # Abbruchbedingung 2: SoC fällt zu tief (Sicherheitsnetz)
                                elif soc < (PV_PAUSE_SOC - 5):
                                    print(f"[{now.strftime('%H:%M:%S')}] PV-Pause abgebrochen (SoC {soc}% < {PV_PAUSE_SOC-5}%).")
                                    if wp.connect(): wp.write_hz_boost(0); wp.write_ww_boost(0); wp.close()
                                    pv_pause_active = False; boost_active = False; pv_pause_start_time = None

                                # Abbruchbedingung 3: Timeout (Sonne kam nicht)
                                elif pv_pause_start_time and (time.time() - pv_pause_start_time) > (PV_PAUSE_TIMEOUT_MINUTES * 60):
                                    print(f"[{now.strftime('%H:%M:%S')}] PV-Pause abgebrochen (Timeout > {PV_PAUSE_TIMEOUT_MINUTES} Min).")
                                    if wp.connect(): wp.write_hz_boost(0); wp.write_ww_boost(0); wp.close()
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
                                    print(f"[{now.strftime('%H:%M:%S')}] Starte PV-Pause (Prognose > {PV_PAUSE_WATT}W erwartet).")
                                    if wp.connect():
                                        wp.write_hz_boost(1, 20.0) # Heizung unterdrücken
                                        wp.write_ww_boost(0)
                                        wp.close()
                                    pv_pause_active = True
                                    boost_active = True
                                    pv_pause_start_time = time.time()

                        # --- PREIS BOOST LOGIK ---
                        # Diese Logik hat Vorrang vor PV-Boost, wenn aktiviert
                        price_action = "NONE"

                        if PRICE_BOOST_ENABLE == 1:
                            # Sonderfall: Bei negativen Preisen immer "BOOST" erzwingen
                            if current_price <= 0:
                                price_action = "BOOST"
                                print(f"[{now.strftime('%H:%M:%S')}] Negativer Strompreis erkannt ({current_price} ct). Erzwinge Boost.")
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
                                    print(f"[{now.strftime('%H:%M:%S')}] WARNUNG: Preis-Boost aktiv, aber keine Preisdaten (prices=[]) empfangen.")
                                    last_price_warning_time = time.time()

                            # Constraints prüfen (WQ, Tageslimit, 18h Sperre, Hard-Limit)
                            # Hard-Limit: Bei extrem tiefen Preisen (z.B. negativ) Limits ignorieren
                            is_hard_boost = (current_price <= PRICE_HARD_LIMIT)

                            if wq_aus < WQ_MIN_TEMP:
                                if price_boost_active or pre_pause_active:
                                    print(f"[{now.strftime('%H:%M:%S')}] NOT-AUS: WQ Aus zu kalt ({wq_aus}°C).")
                                price_action = "NONE"
                            elif daily_boost_counter >= PRICE_MAX_DAILY and price_action == "BOOST" and not is_hard_boost:
                                price_action = "NONE"
                            elif (time.time() - last_pv_boost_time) <= (18 * 3600) and price_action != "NONE" and not is_hard_boost:
                                # Sperre aktiv (PV-Boost war erst kürzlich) -> Keine Aktion
                                price_action = "NONE"

                        # --- AUSFÜHRUNG PREIS LOGIK ---
                        if price_action == "PAUSE":
                            if not pre_pause_active:
                                print(f"[{now.strftime('%H:%M:%S')}] Start Preis-Pause (Erholung vor Boost). WQ: {wq_aus}°C")
                                if wp.connect():
                                    wp.write_hz_boost(1, 20.0) # Heizung auf 20°C zwingen (Pause)
                                    wp.write_ww_boost(0)       # WW Normal
                                    wp.close()
                                pre_pause_active = True
                                price_boost_active = False
                                boost_active = True # Wir kontrollieren die WP
                                
                        elif price_action == "BOOST":
                            if not price_boost_active:
                                print(f"[{now.strftime('%H:%M:%S')}] Start Preis-Boost (Preis: {current_price} ct). WQ: {wq_aus}°C")
                                if wp.connect():
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
                            print(f"[{now.strftime('%H:%M:%S')}] Ende Preis-Steuerung.")
                            if wp.connect():
                                wp.write_hz_boost(0)
                                wp.write_ww_boost(0)
                                wp.close()
                            price_boost_active = False
                            pre_pause_active = False
                            boost_active = False

                        # --- PV BOOST LOGIK (Nur wenn kein Preis-Boost aktiv) ---
                        if not boost_active:
                            # EINSCHALTEN: Genug Überschuss und Batterie voll genug
                            if grid <= GRID_START_LIMIT and soc >= MIN_SOC:
                                if wp.connect():
                                    print(f"[{now.strftime('%H:%M:%S')}] Start Boost (Grid: {grid}W, SoC: {soc}%)")
                                    if at > AT_LIMIT:
                                        wp.write_ww_boost(1, cfg.get('WWS', 50.0))
                                        wp.write_hz_boost(0) # Heizung im Sommer sicherheitshalber aus
                                    else:
                                        wp.write_ww_boost(1, cfg.get('WWW', 48.0))
                                        wp.write_hz_boost(1, cfg.get('HZ', 50.0))
                                    wp.close()
                                    boost_active = True
                                    deficit_start_time = None # Timer sicherheitshalber nullen

                            elif boost_active:
                                # Wenn PV-Boost aktiv (kein Preis-Boost), Timestamp aktualisieren
                                if not price_boost_active:
                                    last_pv_boost_time = time.time()
                                    deficit_start_time = None # Timer zurücksetzen

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
                                print(f"[{now.strftime('%H:%M:%S')}] Korrigiere Boost-Werte (Sync Check)")
                                if wp.connect():
                                    if at > AT_LIMIT:
                                        wp.write_ww_boost(1, cfg.get('WWS', 50.0))
                                        wp.write_hz_boost(0)
                                    else:
                                        wp.write_ww_boost(1, cfg.get('WWW', 48.0))
                                        wp.write_hz_boost(1, cfg.get('HZ', 50.0))
                                    wp.close()

                            # AUSSCHALTEN PRÜFEN
                            if is_deficit:
                                # Wenn wir gerade erst ins Defizit rutschen -> Timer starten
                                if deficit_start_time is None:
                                    deficit_start_time = now
                                    print(f"[{now.strftime('%H:%M:%S')}] Defizit erkannt (Grid: {grid}W, Bat: {bat}W). Timer gestartet.")
                                
                                # Wenn Timer läuft -> Prüfen ob Zeit abgelaufen
                                elif (now - deficit_start_time).total_seconds() > (STOP_DELAY_MINUTES * 60):
                                    if wp.connect():
                                        print(f"[{now.strftime('%H:%M:%S')}] Stop Boost (10 min Defizit)")
                                        wp.write_ww_boost(0, 45.0) # Reset auf Standard
                                        wp.write_hz_boost(0)
                                        wp.close()
                                        boost_active = False
                                        deficit_start_time = None
                            else:
                                # Kein Defizit (wir speisen ein oder laden Batterie) -> Timer reset
                                if deficit_start_time is not None:
                                    print(f"[{now.strftime('%H:%M:%S')}] Defizit beendet (Sonne ist zurück). Timer reset.")
                                deficit_start_time = None

                except Exception as req_err:
                    print(f"Fehler bei E3DC Abfrage: {req_err}")

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
                "success": success
            }

            # Atomares Schreiben
            tmp_file = RAMDISK_FILE + ".tmp"
            with open(tmp_file, 'w') as f:
                json.dump(json_export, f)
            try:
                os.chmod(tmp_file, 0o664)
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
                daily_boost_counter = 0 # Reset des Tageszählers
                last_day = now.day

        except Exception as e:
            print(f"Fehler im Loop: {e}")
            error_json = {"success": False, "error": str(e), "ts": now.isoformat()}
            with open(RAMDISK_FILE, 'w') as f:
                json.dump(error_json, f)
            try:
                os.chmod(RAMDISK_FILE, 0o664)
            except: pass
        
        time.sleep(15)

if __name__ == "__main__":
    main()
