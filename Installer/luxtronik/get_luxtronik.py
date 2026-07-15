import json
import time
import sys
import os
from luxtronik import LuxtronikModbus

def _read_e3dc_config_value(key, default=None):
    """Liest einen Wert aus e3dc_v4.json (Single Source of Truth)."""
    try:
        v4_path = "/var/www/html/data/e3dc_v4.json"
        if os.path.exists(v4_path):
            with open(v4_path, 'r', encoding='utf-8') as f:
                v4_data = json.load(f)
                for k, v in v4_data.items():
                    if k.strip().lower() == key.lower():
                        if isinstance(v, bool): return "1" if v else "0"
                        return str(v).strip()
    except: pass
    return default

def main():
    ip = _read_e3dc_config_value('luxtronik_ip', '0.0.0.0')
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
