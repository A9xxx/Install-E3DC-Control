import sys
import os
import json
from luxtronik import LuxtronikModbus

FLAG_FILE = "/var/www/html/ramdisk/manual_boost.flag"

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
    action = sys.argv[1] if len(sys.argv) > 1 else "off"

    WP_IP = _read_e3dc_config_value('luxtronik_ip', '192.168.178.88')
    AT_LIMIT = float(_read_e3dc_config_value('at_limit', 10.0))
    WWS = float(_read_e3dc_config_value('wws', 50.0))
    WWW = float(_read_e3dc_config_value('www', 48.0))
    HZ = float(_read_e3dc_config_value('hz', 32.0))

    wp = LuxtronikModbus(WP_IP)

    if action == "on":
        if wp.connect():
            data = wp.read_all_sensors()
            at_mittel = data.get('Aussentemp_Mittel', 20.0) #
            
            if at_mittel > AT_LIMIT:
                # SOMMER-BOOST: Nur Warmwasser auf WWS (55°C)
                wp.write_ww_boost(1, WWS)
                wp.write_hz_boost(0) # Heizung bleibt Automatik
                status_msg = f"Sommer-Boost: WW {WWS}°C"
            else:
                # WINTER-BOOST: WW auf WWW (45°C) + Heizung auf HZ (50°C)
                wp.write_ww_boost(1, WWW)
                wp.write_hz_boost(1, HZ)
                status_msg = f"Winter-Boost: WW {WWW}°C, HZ {HZ}°C"
            
            wp.close()
            with open(FLAG_FILE, 'w') as f: f.write(status_msg)
            print(status_msg)
    else:
        if wp.connect():
            wp.write_ww_boost(0, 45.0)
            wp.write_hz_boost(0)
            wp.close()
            if os.path.exists(FLAG_FILE): os.remove(FLAG_FILE)
            print("Boost deaktiviert")

if __name__ == "__main__":
    main()