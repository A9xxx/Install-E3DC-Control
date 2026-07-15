import sys
import os
import json

FLAG_FILE = "/var/www/html/ramdisk/manual_boost.flag"

def _read_e3dc_config_value(key, default=None):
    """Liest einen Wert aus e3dc_v4.json (Single Source of Truth)."""
    try:
        v4_path = "/var/www/html/data/e3dc_v4.json"
        if os.path.exists(v4_path):
            with open(v4_path, 'r', encoding='utf-8-sig') as f:
                v4_data = json.load(f)
                for k, v in v4_data.items():
                    if k.strip().lower() == key.lower():
                        if isinstance(v, bool): return "1" if v else "0"
                        return str(v).strip()
    except Exception: pass
    return default

def get_wp():
    wp_type = int(_read_e3dc_config_value('wp_type', -1))
    if wp_type < 0:
        return None, wp_type
    if wp_type == 1:
        ip = _read_e3dc_config_value('idm_ip', '0.0.0.0')
        sys.path.append(os.path.dirname(os.path.abspath(__file__)))
        try:
            from energy_manager import IDMHeatpump
            return IDMHeatpump(ip), wp_type
        except ImportError:
            return None, wp_type
    if wp_type == 5:
        ip = _read_e3dc_config_value('dimplex_ip', '0.0.0.0')
        sys.path.append(os.path.dirname(os.path.abspath(__file__)))
        try:
            from energy_manager import DimplexHeatpump
            return DimplexHeatpump(
                ip,
                _read_e3dc_config_value('dimplex_port', 502),
                _read_e3dc_config_value('dimplex_unit_id', 1),
                _read_e3dc_config_value('dimplex_sg_register', 5167),
                _read_e3dc_config_value('dimplex_modbus_zero_based', 0),
                _read_e3dc_config_value('dimplex_sg_heartbeat_s', 300),
                _read_e3dc_config_value('dimplex_allow_dark_green', 0),
            ), wp_type
        except ImportError:
            return None, wp_type
    else:
        ip = _read_e3dc_config_value('luxtronik_ip', '0.0.0.0')
        if not ip or ip == '0.0.0.0':
            return None, wp_type
        from luxtronik import LuxtronikModbus
        return LuxtronikModbus(ip), wp_type

def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "off"

    HEIZGRENZE_TEMP = float(_read_e3dc_config_value('heizgrenze_temp', 10.0))
    WWS = float(_read_e3dc_config_value('wws', 55.0))
    WWW = float(_read_e3dc_config_value('www', 48.0))
    HZ = float(_read_e3dc_config_value('hz', 32.0))
    KHL = float(_read_e3dc_config_value('khl', 16.0))
    IDM_COOLING_BOOST_MIN_AT = float(_read_e3dc_config_value('idm_cooling_boost_min_at', 23.0))

    wp, wp_type = get_wp()
    if wp is None:
        print("Fehler: Wärmepumpe konnte nicht initialisiert werden.")
        return

    if action == "on":
        # Sicherer Default: 0.0 = Winter-Modus, wenn Aussentemp nicht lesbar
        at_mittel = 0.0
        connected = True
        
        if wp_type == 0:
            connected = wp.connect()
            if connected:
                try:
                    data = wp.read_all_sensors()
                    at_mittel = data.get('Aussentemp_Mittel', 0.0)
                except Exception: pass
        else:
            live_json_file = "/var/www/html/ramdisk/waermepumpe.json"
            if os.path.exists(live_json_file):
                try:
                    with open(live_json_file, 'r') as f:
                        data = json.load(f)
                        # IDM: 'Aussentemperatur_Mittel' (9.01), Luxtronik: 'Mitteltemperatur'
                        at_mittel = float(data.get('Au\u00dfentemperatur_Mittel',
                                          data.get('Au\u00dfentemperatur',
                                          data.get('Mitteltemperatur',
                                          data.get('Aussentemperatur', 0.0)))))
                except: pass

        if connected:
            if at_mittel > HEIZGRENZE_TEMP:
                # SOMMER-BOOST: Nur Warmwasser, Kühlung nur mit Außen- und Kältespeicherfreigabe.
                khl_aktiv = at_mittel >= IDM_COOLING_BOOST_MIN_AT if wp_type == 1 else at_mittel > HEIZGRENZE_TEMP
                if wp_type == 1 and hasattr(wp, 'force_boost'):
                    boost_ok = wp.force_boost(
                        hz_on=False,
                        ww_on=True,
                        khl_on=khl_aktiv,
                        ww_max=WWS,
                        khl_min=KHL,
                    )
                    khl_aktiv = bool(boost_ok and getattr(wp, 'curr_ext_khl', False))
                elif hasattr(wp, 'set_boost'):
                    wp.set_boost(0, None, 1, WWS, 1 if khl_aktiv else 0, KHL)
                else:
                    wp.write_ww_boost(1, WWS)
                    wp.write_hz_boost(0)
                khl_info = f" | KHL {KHL}\u00b0C" if khl_aktiv else ""
                status_msg = f"Sommer-Boost: WW {WWS}\u00b0C{khl_info}"
            else:
                # WINTER-BOOST: WW + Heizung
                if wp_type == 1 and hasattr(wp, 'force_boost'):
                    wp.force_boost(hz_on=True, ww_on=True, khl_on=False,
                                   ww_max=WWW, hz_max=HZ)  # Kein Boost wenn Temps bereits erfuellt
                elif hasattr(wp, 'set_boost'):
                    wp.set_boost(1, HZ, 1, WWW)
                else:
                    wp.write_ww_boost(1, WWW)
                    wp.write_hz_boost(1, HZ)
                status_msg = f"Winter-Boost: WW {WWW}\u00b0C, HZ {HZ}\u00b0C"
            
            if wp_type == 0: wp.close()
            with open(FLAG_FILE, 'w') as f: f.write(status_msg)
            print(status_msg)
    else:
        if hasattr(wp, 'set_boost'):
            wp.set_boost(0, None, 0, None)
        else:
            if wp_type == 0:
                if wp.connect():
                    wp.write_ww_boost(0, 45.0)
                    wp.write_hz_boost(0)
                    wp.close()
            else:
                wp.write_ww_boost(0, 45.0)
                wp.write_hz_boost(0)

        if os.path.exists(FLAG_FILE): os.remove(FLAG_FILE)
        
        print("Boost deaktiviert")

if __name__ == "__main__":
    main()
