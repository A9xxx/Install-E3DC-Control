#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import math
import logging
import time
from datetime import datetime

try:
    from hyundai_kia_connect_api import VehicleManager
    IMPORT_ERROR = None
except Exception as e:
    VehicleManager = None
    IMPORT_ERROR = str(e)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BluelinkClient")

def get_install_path():
    try:
        with open('/var/www/html/e3dc_paths.json', 'r') as f:
            return json.load(f).get('install_path', '/home/pi/E3DC-Control')
    except:
        return '/home/pi/E3DC-Control'

CONFIG_FILE = os.path.join(get_install_path(), "e3dc.config.txt")
V4_CONFIG_FILE = "/var/www/html/data/e3dc_v4.json"
VEHICLES_JSON_FILE = "/var/www/html/ramdisk/vehicles.json"
FORCE_FLAG_FILE = "/var/www/html/ramdisk/force_bluelink.flag"

def write_json_atomic(path, payload, indent=None):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    stamp = getattr(time, "time_ns", lambda: int(time.time() * 1000000000))()
    tmp = f"{path}.tmp.{os.getpid()}.{stamp}.{id(payload)}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=indent)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass

def load_bluelink_config():
    """Lädt Token, VIN und Heimat-Koordinaten aus der V4-Konfig mit TXT-Fallback."""
    config = {'refresh_token': None, 'vin': None, 'car_name': None, 'bluelink_interval': '15', 'hoehe': '0', 'laenge': '0'}
    if os.path.exists(V4_CONFIG_FILE):
        try:
            with open(V4_CONFIG_FILE, 'r', encoding='utf-8') as f:
                v4 = json.load(f)
            cfg = v4.get('config', v4) if isinstance(v4, dict) else {}
            if isinstance(cfg, dict):
                config['refresh_token'] = cfg.get('bluelink_refresh_token') or config['refresh_token']
                config['vin'] = cfg.get('bluelink_vin') or config['vin']
                config['car_name'] = cfg.get('bluelink_car_name') or config['car_name']
                config['bluelink_interval'] = str(cfg.get('bluelink_interval') or config['bluelink_interval'])
                config['hoehe'] = str(cfg.get('hoehe') or config['hoehe'])
                config['laenge'] = str(cfg.get('laenge') or config['laenge'])
        except Exception as e:
            logger.warning(f"V4-Konfig konnte nicht gelesen werden: {e}")

    if not os.path.exists(CONFIG_FILE):
        return config

    with open(CONFIG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if '=' in line and not line.strip().startswith('#'):
                key, value = [x.strip() for x in line.split('=', 1)]
                if key.lower() == 'bluelink_refresh_token' and not config['refresh_token']:
                    config['refresh_token'] = value
                elif key.lower() == 'bluelink_vin' and not config['vin']:
                    config['vin'] = value
                elif key.lower() == 'bluelink_car_name' and not config['car_name']:
                    config['car_name'] = value
                elif key.lower() == 'bluelink_interval' and config['bluelink_interval'] == '15':
                    config['bluelink_interval'] = value
                elif key.lower() == 'hoehe' and config['hoehe'] == '0':
                    config['hoehe'] = value
                elif key.lower() == 'laenge' and config['laenge'] == '0':
                    config['laenge'] = value
    return config

def main():
    if VehicleManager is None:
        logger.error(f"Fehler beim Laden der Bluelink-API: {IMPORT_ERROR}")
        logger.error("Bitte pruefe die Installation manuell auf der Konsole.")
        return
        
    last_update = 0

    while True:
        config = load_bluelink_config()
        refresh_token = config.get('refresh_token')
        vin = config.get('vin')
        try: interval = int(config.get('bluelink_interval', 15))
        except ValueError: interval = 15
        
        if interval < 5: interval = 5 # Absicherung für die API

        if not refresh_token:
            logger.error("Kein 'bluelink_refresh_token' gefunden. Warte 60s...")
            time.sleep(60)
            continue
            
        now = time.time()
        force_requested = os.path.exists(FORCE_FLAG_FILE)

        if force_requested or (now - last_update) >= (interval * 60):
            try:
                # Region 1 = Europa, Brand 2 = Hyundai (1 = Kia)
                vm = VehicleManager(region=1, brand=2, username="token-login@example.invalid", password=refresh_token, pin="")
                vm.check_and_refresh_token()
                
                if force_requested:
                    logger.info("Manueller Force-Refresh angefordert. Wecke Fahrzeug auf...")
                    vm.force_refresh_all_vehicles_states()
                    try: os.remove(FORCE_FLAG_FILE)
                    except: pass
                else:
                    vm.update_all_vehicles_with_cached_state()
                    last_update = time.time()

                if not vm.vehicles:
                    logger.error("Keine Fahrzeuge im Bluelink-Konto gefunden.")
                else:
                    vehicles_out = []
                    for v_id, target_vehicle in vm.vehicles.items():
                        if vin and target_vehicle.vin != vin:
                            continue
                            
                        soc = target_vehicle.ev_battery_percentage
                        if soc is None: continue
                        
                        v_data = {"id": v_id, "soc": soc}
                        
                        name = getattr(target_vehicle, 'nickname', None)
                        if not name: name = getattr(target_vehicle, 'model_name', None)
                        if not name: name = getattr(target_vehicle, 'name', f"Fahrzeug {len(vehicles_out)+1}")
                        
                        custom_name = config.get('car_name')
                        if custom_name and (len(vm.vehicles) == 1 or (vin and target_vehicle.vin == vin)):
                            name = custom_name
                            
                        v_data["name"] = name
                        
                        is_plugged_raw = getattr(target_vehicle, 'ev_battery_is_plugged_in', None)
                        if is_plugged_raw is None: v_data["is_plugged_in"] = True
                        elif isinstance(is_plugged_raw, bool): v_data["is_plugged_in"] = is_plugged_raw
                        else:
                            try: v_data["is_plugged_in"] = int(is_plugged_raw) > 0
                            except: v_data["is_plugged_in"] = True
                            
                        try:
                            dt = getattr(target_vehicle, 'last_updated_at', None)
                            if dt: v_data["last_updated_at"] = int(dt.timestamp())
                        except: pass
                        
                        try: v_data["range_km"] = getattr(target_vehicle, 'ev_driving_range', None)
                        except: pass
                        try: 
                            bat = getattr(target_vehicle, 'car_battery_percentage', None)
                            if bat is None: bat = getattr(target_vehicle, 'ev_battery_twelve_volt_percentage', None)
                            if bat is None and hasattr(target_vehicle, 'data'):
                                try: bat = target_vehicle.data['vehicleStatus']['battery']['batSoc']
                                except: pass
                            v_data["bat_12v"] = bat
                        except: pass
                        try: 
                            odo = getattr(target_vehicle, 'odometer', None)
                            if odo is None and hasattr(target_vehicle, 'data'):
                                try: odo = target_vehicle.data['vehicleStatus']['odometer']
                                except: pass
                            if odo is not None: v_data["odometer"] = odo
                        except: pass
                        try: 
                            tsoc = getattr(target_vehicle, 'ev_target_soc_ac', None)
                            if tsoc is None: tsoc = getattr(target_vehicle, 'ev_target_soc', None)
                            if isinstance(tsoc, list) and len(tsoc) > 0: tsoc = tsoc[0]
                            if tsoc is None and hasattr(target_vehicle, 'data'):
                                try: 
                                    tsoc_list = target_vehicle.data['vehicleStatus']['evStatus']['reservChargeInfos']['targetSOClist']
                                    for t in tsoc_list:
                                        if t.get('plugType') == 1:
                                            tsoc = t.get('targetSOClevel')
                                            break
                                    if tsoc is None: tsoc = tsoc_list[0]['targetSOClevel']
                                except: pass
                                if tsoc is None:
                                    try: 
                                        tsoc_list = target_vehicle.data['vehicleStatus']['evStatus']['reservChargeStInfo']['targetSocList']
                                        for t in tsoc_list:
                                            if t.get('plugType') == 1:
                                                tsoc = t.get('targetSocLevel')
                                                break
                                        if tsoc is None: tsoc = tsoc_list[0]['targetSocLevel']
                                    except: pass
                            if tsoc is not None: v_data["target_soc"] = tsoc
                        except: pass
                        try: v_data["is_locked"] = getattr(target_vehicle, 'is_locked', None)
                        except: pass
                        try: v_data["air_ctrl"] = getattr(target_vehicle, 'air_control_is_on', None)
                        except: pass
                        
                        try: 
                            v_stat = target_vehicle.data.get('vehicleStatus') or {}
                            tire = v_stat.get('tirePressureLamp') or {}
                            v_data["tire_warning"] = bool(int(tire.get('tirePressureLampAll', 0)) > 0)
                        except Exception as e: pass
                        
                        try: 
                            v_stat = target_vehicle.data.get('vehicleStatus') or {}
                            doors = v_stat.get('doorOpen') or {}
                            any_door = False
                            for v in doors.values():
                                try:
                                    if int(v) > 0: any_door = True
                                except: pass
                            trunk = bool(v_stat.get('trunkOpen', False))
                            hood = bool(v_stat.get('hoodOpen', False))
                            v_data["doors_open"] = bool(any_door or trunk or hood)
                        except Exception as e: pass
                        
                        loc = getattr(target_vehicle, 'location', None)
                        car_lat = getattr(target_vehicle, 'location_latitude', None)
                        car_lon = getattr(target_vehicle, 'location_longitude', None)
                        if loc and not car_lat:
                            if hasattr(loc, 'latitude'):
                                car_lat = loc.latitude
                                car_lon = loc.longitude
                            elif isinstance(loc, dict):
                                car_lat = loc.get('latitude')
                                car_lon = loc.get('longitude')
                        if not car_lat and hasattr(target_vehicle, 'data'):
                            try:
                                car_lat = target_vehicle.data['vehicleLocation']['coord']['lat']
                                car_lon = target_vehicle.data['vehicleLocation']['coord']['lon']
                            except: pass
                        if car_lat and car_lon:
                            v_data["car_lat"] = car_lat
                            v_data["car_lon"] = car_lon
                            try:
                                home_lat = float(config.get('hoehe', 0))
                                home_lon = float(config.get('laenge', 0))
                                if home_lat and home_lon:
                                    dLat = math.radians(car_lat - home_lat); dLon = math.radians(car_lon - home_lon)
                                    a = math.sin(dLat/2)**2 + math.cos(math.radians(home_lat)) * math.cos(math.radians(car_lat)) * math.sin(dLon/2)**2
                                    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
                                    dist = 6371.0 * c
                                    v_data["is_at_home"] = bool(dist <= 0.5)
                            except Exception as e: pass
                            
                        vehicles_out.append(v_data)

                    # Alte Daten mischen (Odometer etc rüberretten)
                    old_data = {}
                    try:
                        if os.path.exists(VEHICLES_JSON_FILE):
                            with open(VEHICLES_JSON_FILE, "r") as f:
                                old_data = json.load(f)
                    except: pass
                    
                    if 'vehicles' in old_data:
                        for old_v in old_data['vehicles']:
                            for new_v in vehicles_out:
                                if old_v.get('id') == new_v.get('id'):
                                    for k, v in old_v.items():
                                        if k not in new_v and k not in ['soc', 'is_plugged_in']:
                                            new_v[k] = v

                    data = {"ts": int(time.time()), "vehicles": vehicles_out}
                    write_json_atomic(VEHICLES_JSON_FILE, data)
                    logger.info(f"{len(vehicles_out)} Fahrzeuge erfolgreich aktualisiert (Force={force_requested}).")
                    
                    # --- DEBUG: Alle rohen Fahrzeugdaten für den Nutzer speichern ---
            except Exception as e:
                logger.error(f"Ein Fehler ist aufgetreten: {e}")
                
                # SoC erhalten, aber Fehler im JSON hinterlegen
                old_data = {}
                try:
                    if os.path.exists(VEHICLES_JSON_FILE):
                        with open(VEHICLES_JSON_FILE, "r") as f:
                            old_data = json.load(f)
                except: pass
                
                data = {"ts": int(time.time()), "error": "Fahrzeug offline / API Fehler"}
                if 'vehicles' in old_data:
                    data['vehicles'] = old_data['vehicles']
                write_json_atomic(VEHICLES_JSON_FILE, data)
                
                if force_requested:
                    try: os.remove(FORCE_FLAG_FILE)
                    except: pass
                
        time.sleep(5)

if __name__ == "__main__":
    main()
