import time
import json
import os
import shutil
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
    
    # Stop-Verzögerung: Wie lange darf Strom aus Netz/Akku gezogen werden?
    STOP_DELAY_MINUTES = 10 
    
    # IP Adresse
    wp_ip = cfg.get('luxtronik_ip', "192.168.178.88")
    wp = LuxtronikModbus(wp_ip)
    
    boost_active = False
    deficit_start_time = None # Timer für Abschaltung
    last_day = datetime.now().day
    first_run = True # Initial-Check Flag

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
    print(f"Start bei > {abs(GRID_START_LIMIT)}W Einspeisung.")
    print(f"Stop nach {STOP_DELAY_MINUTES} min Bezug (Netz oder Batterie).")

    while True:
        now = datetime.now()
        wp_data = {}   
        wp_status = {} 
        at = 20.0 # Default
        success = False 

        try:
            # 1. Daten von Wärmepumpe holen
            if wp.connect():
                wp_data = wp.read_all_sensors()
                time.sleep(0.5)
                wp_status = wp.read_shi_status()
                
                # Initial-Status beim Start prüfen
                if first_run:
                    if os.path.exists(FLAG_FILE):
                        print(f"[{now.strftime('%H:%M:%S')}] Manueller Boost erkannt (Flag). Warte im Standby.")
                        boost_active = False
                    elif wp_status.get('WW_Mode') == 1 or wp_status.get('HZ_Mode') == 1:
                        print(f"[{now.strftime('%H:%M:%S')}] WP ist bereits im Boost-Modus. Übernehme Status.")
                        boost_active = True
                    first_run = False
                
                # Status-Korrektur: Falls WP extern (Display/App) auf "Auto" (0) gestellt wurde
                if boost_active and wp_status:
                    if wp_status.get('WW_Mode') != 1 and wp_status.get('HZ_Mode') != 1:
                        print(f"[{now.strftime('%H:%M:%S')}] Boost-Modus wurde extern deaktiviert (Register=0). Reset Status.")
                        boost_active = False
                        deficit_start_time = None
                
                wp.close()
                success = True 
            else:
                print(f"[{now.strftime('%H:%M:%S')}] Verbindung zur WP fehlgeschlagen")

            # 2. Logik (Überschuss-Prüfung)
            if not os.path.exists(FLAG_FILE):
                try:
                    r = requests.get("http://localhost/get_live_json.php", timeout=5)
                    if r.status_code == 200:
                        e3dc = r.json()
                        grid = e3dc.get('grid', 0) # + Import, - Export
                        bat = e3dc.get('bat', 0)   # + Laden, - Entladen
                        soc = e3dc.get('soc', 0)
                        at = wp_data.get('Aussentemp_Mittel', 20.0) # Wert aktualisieren

                        # Defizit-Erkennung:
                        # Wir verbrauchen Reserven, wenn wir aus dem Netz beziehen (>50W Puffer)
                        # ODER wenn wir die Batterie entladen (bat < -50W)
                        is_deficit = (grid > 50) or (bat < -50)

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
                last_day = now.day

        except Exception as e:
            print(f"Fehler im Loop: {e}")
            error_json = {"success": False, "error": str(e), "ts": now.isoformat()}
            with open(RAMDISK_FILE, 'w') as f:
                json.dump(error_json, f)
            try:
                os.chmod(RAMDISK_FILE, 0o664)
            except: pass
        
        time.sleep(30)

if __name__ == "__main__":
    main()
