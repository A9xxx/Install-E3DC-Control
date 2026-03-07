import sys
import os
import json
from luxtronik import LuxtronikModbus

CONFIG_PATH = "/home/pi/Install/Installer/luxtronik/config.lux.json"
FLAG_FILE = "/var/www/html/ramdisk/manual_boost.flag"
WP_IP = "192.168.178.88"

def load_config():
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)

def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "off"
    cfg = load_config()
    wp = LuxtronikModbus(WP_IP)

    if action == "on":
        if wp.connect():
            data = wp.read_all_sensors()
            at_mittel = data.get('Aussentemp_Mittel', 20.0) #
            
            if at_mittel > cfg['AT_LIMIT']:
                # SOMMER-BOOST: Nur Warmwasser auf WWS (55°C)
                wp.write_ww_boost(1, cfg['WWS'])
                wp.write_hz_boost(0) # Heizung bleibt Automatik
                status_msg = f"Sommer-Boost: WW {cfg['WWS']}°C"
            else:
                # WINTER-BOOST: WW auf WWW (45°C) + Heizung auf HZ (50°C)
                wp.write_ww_boost(1, cfg['WWW'])
                wp.write_hz_boost(1, cfg['HZ'])
                status_msg = f"Winter-Boost: WW {cfg['WWW']}°C, HZ {cfg['HZ']}°C"
            
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