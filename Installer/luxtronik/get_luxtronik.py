import json
import time
import sys
import os
from luxtronik import LuxtronikModbus

def _read_e3dc_config_value(key, default=None):
    """Liest einen Wert aus der zentralen e3dc.config.txt."""
    try:
        with open('/var/www/html/e3dc_paths.json', 'r') as f:
            paths = json.load(f)
            install_path = paths.get('install_path', '/home/pi/E3DC-Control/')
    except:
        install_path = '/home/pi/E3DC-Control/'
    
    config_path = os.path.join(install_path, 'e3dc.config.txt')
    if not os.path.exists(config_path): return default

    try:
        with open(config_path, 'r') as f:
            for line in f:
                if line.strip().startswith('#') or '=' not in line: continue
                k, v = line.split('=', 1)
                if k.strip().lower() == key.lower():
                    return v.strip()
    except Exception: pass
    return default

def main():
    ip = _read_e3dc_config_value('luxtronik_ip', '192.168.178.88')
    luxtronik_enabled_str = _read_e3dc_config_value('luxtronik', '0')
    luxtronik_enabled = luxtronik_enabled_str.lower() in ['1', 'true']

    # Vorbereiten des Ergebnis-Objekts
    result = {
        'success': False, 
        'data': {}, 
        'status': {}, 
        'error': ''
    }

    # Abbruch, wenn deaktiviert
    if luxtronik_enabled != 1:
        result['error'] = "Luxtronik ist in der Konfiguration deaktiviert."
        print(json.dumps(result))
        return

    wp = LuxtronikModbus(ip)

    try:
        # DURCHGANG 1: SENSORDATEN (INPUT REGISTER)
        if wp.connect():
            # Liest Temperaturen, Leistung und Energie
            result['data'] = wp.read_all_sensors()
            wp.close() # VERBINDUNG SOFORT SCHLIESSEN
        else:
            result['error'] = "Verbindung fuer Sensordaten fehlgeschlagen."
            print(json.dumps(result))
            return

        # VERSCHNAUFPAUSE: Damit der Modbus-Stack der WP resetten kann
        time.sleep(1.0) 

        # DURCHGANG 2: SHI-STATUS (HOLDING REGISTER)
        if wp.connect():
            # Liest Heizungs-/WW-Modus und SHI-Sollwerte
            result['status'] = wp.read_shi_status()
            result['success'] = True # Wenn wir hier ankommen, war alles erfolgreich
            wp.close()
        else:
            # Falls nur der Status fehlschlaegt, senden wir trotzdem die Sensordaten
            result['error'] = "Sensordaten OK, aber Status-Abfrage fehlgeschlagen."
            result['success'] = True 

    except Exception as e:
        result['error'] = f"Skript-Fehler: {str(e)}"
        result['success'] = False

    # JSON-Ausgabe fuer dein PHP-Dashboard
    print(json.dumps(result))

if __name__ == "__main__":
    main()
