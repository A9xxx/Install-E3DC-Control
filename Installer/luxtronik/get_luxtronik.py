import json
import time
import sys
import os
from luxtronik import LuxtronikModbus

def main():
    # Standardwerte
    ip = '192.168.178.88' 
    luxtronik_enabled = 0

    # Pfad zur Konfigurationsdatei ermitteln
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_file = os.path.join(script_dir, "config.lux.json")

    # Config laden
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                data = json.load(f)
                ip = data.get('luxtronik_ip', ip)
                luxtronik_enabled = int(data.get('luxtronik', 0))
        except Exception as e:
            # Fehler auf stderr ausgeben, damit der JSON-Output (stdout) nicht zerstört wird
            sys.stderr.write(f"Config-Fehler: {e}\n")

    # Vorbereiten des Ergebnis-Objekts
    result = {
        'success': False, 
        'data': {}, 
        'status': {}, 
        'error': ''
    }

    # Abbruch, wenn deaktiviert
    if luxtronik_enabled != 1:
        result['error'] = "Luxtronik ist deaktiviert (config.lux.json)."
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
