#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import math
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Nur nicht-POSIX Testumgebungen
    fcntl = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 bleibt fail-closed
    ZoneInfo = None

try:
    from hyundai_kia_connect_api import VehicleManager
    IMPORT_ERROR = None
except Exception as e:
    VehicleManager = None
    IMPORT_ERROR = str(e)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BluelinkClient")

def get_install_path():
    root = Path(__file__).resolve().parent.parent
    markers = (root / "VERSION", root / "installer_main.py", root / "Installer")
    if not all(marker.exists() for marker in markers):
        raise RuntimeError("Bluelink: Release-Root ist nicht eindeutig aufloesbar")
    return str(root)

CONFIG_FILE = os.path.join(get_install_path(), "e3dc.config.txt")
V4_CONFIG_FILE = "/var/www/html/data/e3dc_v4.json"
VEHICLES_JSON_FILE = "/var/www/html/ramdisk/vehicles.json"
FORCE_FLAG_FILE = "/var/www/html/ramdisk/force_bluelink.flag"
BLUELINK_REFRESH_SCHEMA = "bluelink_refresh_status_v1"
BLUELINK_SOC_SOURCES = {"bluelink", "hyundai", "kia", "cloud", "vehicle_cloud"}


class BluelinkVehicleDataMissing(RuntimeError):
    """Die Cloudantwort enthält kein zum konfigurierten Fahrzeug nutzbares Ergebnis."""

    refresh_error_code = "vehicle_data_missing"


def _safe_timestamp(value):
    """Normalisiert ausschließlich einen echten Fahrzeug-Quellzeitpunkt."""
    if value in (None, ""):
        return None
    if hasattr(value, "timestamp"):
        try:
            value = value.timestamp()
        except Exception:
            return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timestamp) or timestamp <= 0.0:
        return None
    if timestamp > 100000000000.0:
        timestamp /= 1000.0
    return int(timestamp)


def _safe_persisted_soc_timestamp(value, now=None):
    """Verwirft persistierte Zukunftsanker, die gültige Antworten blockieren."""
    timestamp = _safe_timestamp(value)
    if timestamp is None:
        return None
    now_value = int(time.time()) if now is None else int(now)
    if timestamp > now_value + 300:
        return None
    return timestamp


def _normalize_soc_percent(value):
    """Akzeptiert nur endliche, physikalisch mögliche Fahrzeug-Prozentwerte."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        soc = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(soc) or soc < 0.0 or soc > 100.0:
        return None
    return soc


def _parse_raw_vehicle_timestamp(value, source_timezone):
    """Parst nur einen tatsächlich im Hyundai/Kia-Rohstatus vorhandenen Zeitwert."""
    if value in (None, ""):
        return None
    if hasattr(value, "timestamp"):
        timestamp = _safe_timestamp(value)
        return timestamp if timestamp is not None and timestamp <= int(time.time()) + 300 else None

    text = str(value).strip()
    if not text:
        return None
    parsed = None
    try:
        parsed = datetime.strptime(text, "%a, %d %b %Y %H:%M:%S GMT").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        pass

    if parsed is None:
        compact = text.replace("-", "").replace("T", "").replace(":", "").replace("Z", "")
        if len(compact) >= 14 and compact[:14].isdigit():
            try:
                parsed = datetime.strptime(compact[:14], "%Y%m%d%H%M%S")
            except ValueError:
                parsed = None

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        if source_timezone == "UTC":
            parsed = parsed.replace(tzinfo=timezone.utc)
        elif ZoneInfo is not None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Berlin"))
        else:
            return None
    timestamp = _safe_timestamp(parsed)
    if timestamp is None or timestamp > int(time.time()) + 300:
        return None
    return timestamp


def _raw_vehicle_soc_timestamp(target_vehicle):
    """Liest ausschließlich die beiden von der EU-API belegten Rohanker."""
    raw = getattr(target_vehicle, "data", None)
    if not isinstance(raw, dict):
        return None

    vehicle_status = raw.get("vehicleStatus")
    if isinstance(vehicle_status, dict) and vehicle_status.get("time") not in (None, ""):
        return _parse_raw_vehicle_timestamp(vehicle_status.get("time"), "Europe/Berlin")
    if raw.get("Date") not in (None, ""):
        return _parse_raw_vehicle_timestamp(raw.get("Date"), "UTC")
    return None


def _preserve_vehicles_fail_closed(old_data):
    """Erhält fremde Quellen und entwertet nur ungebundene Legacywerte."""
    old_vehicles = old_data.get("vehicles") if isinstance(old_data, dict) else None
    preserved = []
    for vehicle in old_vehicles if isinstance(old_vehicles, list) else []:
        if not isinstance(vehicle, dict):
            continue
        item = dict(vehicle)
        source = str(item.get("soc_source") or item.get("source") or "").strip().lower()
        if source in BLUELINK_SOC_SOURCES:
            source_ts = (
                _safe_persisted_soc_timestamp(item.get("soc_source_ts"))
                if "soc_source_ts" in item else None
            )
            rule_confirmed = (
                item.get("soc_rule_confirmed") is True
                and item.get("soc_stale") is not True
                and item.get("soc_rule_usable") is not False
                and source_ts is not None
            )
            item["soc_source"] = "bluelink"
            item["soc_source_ts"] = source_ts
            item["last_updated_at"] = source_ts
            item["soc_rule_confirmed"] = rule_confirmed
            if not rule_confirmed:
                item["soc_stale"] = True
        elif not source:
            # 4f schrieb Cloud- und MQTT-Werte noch ohne Quellvertrag in
            # dieselbe Datei. Diese Migration ist absichtlich unbekannt statt
            # eine der beiden Quellen zu erfinden.
            item["soc_source"] = "legacy_vehicle_unknown"
            item["soc_source_ts"] = None
            item["last_updated_at"] = None
            item["soc_rule_confirmed"] = False
            item["soc_stale"] = True
        preserved.append(item)
    return preserved


def _is_bluelink_vehicle_record(vehicle):
    if not isinstance(vehicle, dict):
        return False
    source = str(vehicle.get("soc_source") or vehicle.get("source") or "").strip().lower()
    return source in BLUELINK_SOC_SOURCES


def _vehicle_source_timestamp(vehicle):
    if not isinstance(vehicle, dict):
        return None
    if not _is_bluelink_vehicle_record(vehicle):
        return None
    # Der Abruf-/Dateizeitpunkt ist kein Fahrzeugmesswert. Sobald das neue
    # Feld vorhanden ist, darf ein fehlender Rohanker nicht auf einen anderen
    # Zeitstempel zurückfallen.
    if "soc_source_ts" in vehicle:
        return _safe_persisted_soc_timestamp(vehicle.get("soc_source_ts"))
    return None


def _latest_vehicle_source_timestamp(vehicles):
    source_timestamps = [
        timestamp
        for timestamp in (
            _vehicle_source_timestamp(vehicle)
            for vehicle in (vehicles if isinstance(vehicles, list) else [])
        )
        if timestamp is not None
    ]
    return max(source_timestamps) if source_timestamps else None


def _refresh_error_code(error):
    typed_code = getattr(error, "refresh_error_code", None)
    if typed_code == "vehicle_data_missing":
        return typed_code
    text = str(error or "").strip().lower()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "too many requests" in text or "rate limit" in text or "429" in text:
        return "rate_limited"
    if "unauthorized" in text or "authentication" in text or "invalid token" in text:
        return "authentication_failed"
    return "api_error"


def _refresh_error_message(code):
    return {
        "timeout": "Hyundai/Kia-Cloud antwortet nicht rechtzeitig.",
        "rate_limited": "Hyundai/Kia-Cloud begrenzt weitere Abfragen.",
        "authentication_failed": "Hyundai/Kia-Anmeldung wurde abgewiesen.",
        "vehicle_data_missing": "Hyundai/Kia-Cloud lieferte keine verwertbaren Fahrzeugdaten.",
        "api_error": "Hyundai/Kia-Fahrzeugdaten konnten nicht aktualisiert werden.",
    }.get(str(code or ""), "Hyundai/Kia-Fahrzeugdaten konnten nicht aktualisiert werden.")


def build_refresh_status(
    old_data,
    vehicles,
    mode,
    attempt_ts,
    completed_ts,
    error=None,
    response_vehicles=None,
):
    """Erzeugt Diagnose ohne einen SoC-Quellanker künstlich zu verjüngen."""
    old_data = old_data if isinstance(old_data, dict) else {}
    old_vehicles = old_data.get("vehicles") if isinstance(old_data.get("vehicles"), list) else []
    old_source_ts = _latest_vehicle_source_timestamp(old_vehicles)
    source_ts = _latest_vehicle_source_timestamp(vehicles)
    response_items = (
        response_vehicles
        if isinstance(response_vehicles, list)
        else vehicles if isinstance(vehicles, list) else []
    )
    response_source_ts = _latest_vehicle_source_timestamp(response_items)
    response_vehicle_count = len([
        item for item in response_items if isinstance(item, dict)
    ])
    response_missing_source_count = len([
        item
        for item in response_items
        if isinstance(item, dict) and _vehicle_source_timestamp(item) is None
    ])
    response_source_complete = bool(
        response_vehicle_count > 0 and response_missing_source_count == 0
    )
    previous = old_data.get("refresh") if isinstance(old_data.get("refresh"), dict) else {}
    last_error = previous.get("last_error") if isinstance(previous.get("last_error"), dict) else None

    error_code = _refresh_error_code(error) if error is not None else None
    if error_code is not None:
        last_error = {
            "ts": int(completed_ts),
            "mode": str(mode),
            "code": error_code,
            "message": _refresh_error_message(error_code),
        }
        status = "failed"
        source_advanced = False
    else:
        source_advanced = response_source_ts is not None and (
            old_source_ts is None or response_source_ts > old_source_ts
        )
        if response_source_ts is None:
            status = "success_source_unknown"
        elif not response_source_complete:
            status = "success_source_partial"
        elif source_advanced:
            status = "success_source_advanced"
        else:
            status = "success_source_unchanged"

    last_error_ts = _safe_timestamp((last_error or {}).get("ts"))
    last_error_active = bool(
        last_error_ts is not None
        and (
            not response_source_complete
            or response_source_ts is None
            or response_source_ts <= last_error_ts
        )
    )
    return {
        "schema": BLUELINK_REFRESH_SCHEMA,
        "status": status,
        "mode": str(mode),
        "attempt_ts": int(attempt_ts),
        "completed_ts": int(completed_ts),
        "success": error is None,
        "source_ts": source_ts,
        "source_age_s": max(0, int(completed_ts) - source_ts) if source_ts is not None else None,
        "source_advanced": source_advanced,
        "response_source_ts": response_source_ts,
        "response_source_complete": response_source_complete,
        "response_vehicle_count": response_vehicle_count,
        "response_missing_source_count": response_missing_source_count,
        "error_code": error_code,
        "message": _refresh_error_message(error_code) if error_code is not None else None,
        "last_error": last_error,
        "last_error_active": last_error_active,
    }


def _read_existing_vehicle_data():
    try:
        if os.path.exists(VEHICLES_JSON_FILE):
            with open(VEHICLES_JSON_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


@contextmanager
def _vehicle_store_lock():
    """Serialisiert alle read-modify-write-Zyklen der gemeinsamen Fahrzeugdatei."""
    lock_path = f"{VEHICLES_JSON_FILE}.lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o664)
    try:
        try:
            os.chmod(lock_path, 0o664)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


_SOC_RUNTIME_FIELDS = frozenset({
    "soc", "source", "soc_source",
    "soc_source_ts", "last_updated_at", "soc_rule_confirmed",
    "soc_rule_usable", "soc_stale", "soc_age_s", "soc_meta",
    "soc_source_previous", "soc_source_original", "soc_source_class",
    "soc_cache_ts", "raw_soc_ts", "is_interpolated",
})


def _soc_truth_rank(vehicle):
    """Ordnet konkurrierende Producer nach Regelwahrheit, Anker und Quelle."""
    if not isinstance(vehicle, dict):
        return (0, -1, 0)
    source = str(
        vehicle.get("soc_source") or vehicle.get("source") or ""
    ).strip().lower()
    source_ts = (
        _safe_persisted_soc_timestamp(vehicle.get("soc_source_ts"))
        if "soc_source_ts" in vehicle else None
    )
    confirmed = bool(
        vehicle.get("soc_rule_confirmed") is True
        and vehicle.get("soc_stale") is not True
        and vehicle.get("soc_rule_usable") is not False
        and source_ts is not None
    )
    if source in BLUELINK_SOC_SOURCES:
        source_priority = 300
    elif source == "mqtt":
        source_priority = 250
    elif source.startswith("openwb") or source.startswith("wallbox"):
        source_priority = 200
    elif source:
        source_priority = 100
    else:
        source_priority = 0
    return (
        1 if confirmed else 0,
        int(source_ts) if source_ts is not None else -1,
        source_priority,
    )


def _merge_cloud_vehicles_with_current(
    current_data,
    cloud_vehicles,
    preserve_missing_cloud=False,
):
    """Erhält statische Felder und fremde Producer ohne alte SoC-Wahrheit."""
    merged = [dict(vehicle) for vehicle in cloud_vehicles if isinstance(vehicle, dict)]
    current_vehicles = (
        current_data.get("vehicles")
        if isinstance(current_data, dict) else None
    )
    current_vehicles = current_vehicles if isinstance(current_vehicles, list) else []

    by_id = {str(vehicle.get("id")): vehicle for vehicle in merged}
    for current_vehicle in current_vehicles:
        if not isinstance(current_vehicle, dict):
            continue
        target = by_id.get(str(current_vehicle.get("id")))
        if target is None:
            continue
        current_rank = _soc_truth_rank(current_vehicle)
        incoming_rank = _soc_truth_rank(target)
        if current_rank[0] > incoming_rank[0] or (
            current_rank[0] == incoming_rank[0] == 1
            and current_rank >= incoming_rank
        ):
            # Eine schwächere, ankerlose oder ältere Cloudantwort darf weder
            # einen neueren Cloudwert noch einen parallel empfangenen,
            # bestätigten MQTT-Wert derselben Fahrzeug-ID zurückrollen.
            # Nicht-SoC-Stammdaten des aktuellen API-Objekts bleiben nutzbar.
            for key in _SOC_RUNTIME_FIELDS:
                if key in current_vehicle:
                    target[key] = current_vehicle[key]
                else:
                    target.pop(key, None)
        for key, value in current_vehicle.items():
            if key not in target and key not in _SOC_RUNTIME_FIELDS:
                target[key] = value

    known_ids = set(by_id)
    for current_vehicle in _preserve_vehicles_fail_closed(current_data):
        current_id = str(current_vehicle.get("id"))
        if ((not _is_bluelink_vehicle_record(current_vehicle)
             or preserve_missing_cloud)
            and current_id not in known_ids):
            merged.append(current_vehicle)
            known_ids.add(current_id)
    return merged


def _commit_bluelink_result(
    cloud_vehicles,
    mode,
    attempt_ts,
    completed_ts,
    error=None,
    preserve_missing_cloud=False,
):
    """Liest unter derselben Sperre neu ein und schreibt genau einen Merge."""
    with _vehicle_store_lock():
        current_data = _read_existing_vehicle_data()
        if error is None:
            vehicles = _merge_cloud_vehicles_with_current(
                current_data,
                cloud_vehicles,
                preserve_missing_cloud=preserve_missing_cloud,
            )
        else:
            vehicles = _preserve_vehicles_fail_closed(current_data)
        refresh = build_refresh_status(
            current_data,
            vehicles,
            mode,
            attempt_ts,
            completed_ts,
            error=error,
            response_vehicles=cloud_vehicles if error is None else None,
        )
        data = {
            "ts": int(completed_ts),
            "source": "bluelink",
            "refresh": refresh,
            "vehicles": vehicles,
        }
        if error is not None:
            data["error"] = refresh["message"]
        write_json_atomic(VEHICLES_JSON_FILE, data)
        return data

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
    v4_bound_keys = set()
    if os.path.exists(V4_CONFIG_FILE):
        try:
            with open(V4_CONFIG_FILE, 'r', encoding='utf-8') as f:
                v4 = json.load(f)
            if isinstance(v4, dict):
                sub_cfg = v4.get('config') if isinstance(v4.get('config'), dict) else {}
                for source_key, target_key, stringify in (
                    ('bluelink_refresh_token', 'refresh_token', False),
                    ('bluelink_vin', 'vin', False),
                    ('bluelink_car_name', 'car_name', False),
                    ('bluelink_interval', 'bluelink_interval', True),
                    ('hoehe', 'hoehe', True),
                    ('laenge', 'laenge', True),
                ):
                    if source_key in v4:
                        value = v4.get(source_key)
                        v4_bound_keys.add(source_key)
                        config[target_key] = (
                            '' if value is None else str(value)
                        ) if stringify else value
                    elif source_key in sub_cfg:
                        value = sub_cfg.get(source_key)
                        if value is not None and str(value).strip() != '':
                            config[target_key] = str(value) if stringify else value
                    else:
                        continue
        except Exception as e:
            logger.warning(f"V4-Konfig konnte nicht gelesen werden: {e}")

    if not os.path.exists(CONFIG_FILE):
        return config

    with open(CONFIG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if '=' in line and not line.strip().startswith('#'):
                key, value = [x.strip() for x in line.split('=', 1)]
                key = key.lower()
                if key == 'bluelink_refresh_token' and key not in v4_bound_keys and not config['refresh_token']:
                    config['refresh_token'] = value
                elif key == 'bluelink_vin' and key not in v4_bound_keys and not config['vin']:
                    config['vin'] = value
                elif key == 'bluelink_car_name' and key not in v4_bound_keys and not config['car_name']:
                    config['car_name'] = value
                elif key == 'bluelink_interval' and key not in v4_bound_keys and config['bluelink_interval'] == '15':
                    config['bluelink_interval'] = value
                elif key == 'hoehe' and key not in v4_bound_keys and config['hoehe'] == '0':
                    config['hoehe'] = value
                elif key == 'laenge' and key not in v4_bound_keys and config['laenge'] == '0':
                    config['laenge'] = value
    return config

def main():
    if VehicleManager is None:
        logger.error(f"Fehler beim Laden der Bluelink-API: {IMPORT_ERROR}")
        logger.error("Bitte pruefe die Installation manuell auf der Konsole.")
        return
        
    last_update = 0
    missing_token_logged = False

    while True:
        config = load_bluelink_config()
        refresh_token = config.get('refresh_token')
        vin = config.get('vin')
        try: interval = int(config.get('bluelink_interval', 15))
        except ValueError: interval = 15
        
        if interval < 5: interval = 5 # Absicherung für die API

        if not refresh_token:
            if not missing_token_logged:
                logger.info("Bluelink ist ohne refresh_token deaktiviert; warte auf Konfiguration.")
                missing_token_logged = True
            time.sleep(60)
            continue
        missing_token_logged = False
            
        now = time.time()
        force_requested = os.path.exists(FORCE_FLAG_FILE)

        if force_requested or (now - last_update) >= (interval * 60):
            attempt_ts = int(time.time())
            refresh_mode = "force" if force_requested else "cached"
            # Jeder Cloudversuch setzt den nächsten regulären Termin. Ein
            # fehlgeschlagener Cached-Abruf darf sonst alle fünf Sekunden neu
            # anlaufen und API-Limit sowie Fahrzeugbatterie belasten.
            last_update = float(attempt_ts)
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

                if not vm.vehicles:
                    raise BluelinkVehicleDataMissing("Kein Fahrzeug im Konto")
                else:
                    vehicles_out = []
                    for v_id, target_vehicle in vm.vehicles.items():
                        if vin and target_vehicle.vin != vin:
                            continue
                            
                        soc = _normalize_soc_percent(
                            target_vehicle.ev_battery_percentage
                        )
                        if soc is None:
                            continue
                        
                        v_data = {
                            "id": v_id,
                            "soc": soc,
                            "soc_source": "bluelink",
                        }
                        
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
                            
                        source_ts = _raw_vehicle_soc_timestamp(target_vehicle)
                        v_data["soc_source_ts"] = source_ts
                        v_data["last_updated_at"] = source_ts
                        v_data["soc_rule_confirmed"] = source_ts is not None
                        
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

                    if not vehicles_out:
                        raise BluelinkVehicleDataMissing(
                            "Konfiguriertes Fahrzeug oder dessen SoC fehlt"
                        )

                    completed_ts = int(time.time())
                    data = _commit_bluelink_result(
                        vehicles_out,
                        refresh_mode,
                        attempt_ts,
                        completed_ts,
                        preserve_missing_cloud=not bool(vin),
                    )
                    logger.info(f"{len(data['vehicles'])} Fahrzeuge erfolgreich aktualisiert (Force={force_requested}).")
                    
                    # --- DEBUG: Alle rohen Fahrzeugdaten für den Nutzer speichern ---
            except Exception as e:
                logger.error(f"Ein Fehler ist aufgetreten: {e}")
                
                # Letzten bestätigten SoC erhalten, aber Versuch und Fehler
                # getrennt davon publizieren. Der neue Datei-Zeitpunkt ist
                # ausdrücklich kein neuer Fahrzeugstand.
                completed_ts = int(time.time())
                _commit_bluelink_result(
                    [],
                    refresh_mode,
                    attempt_ts,
                    completed_ts,
                    error=e,
                )
                
                if force_requested:
                    try: os.remove(FORCE_FLAG_FILE)
                    except: pass
            finally:
                # Das reguläre Intervall beginnt nach Abschluss des Abrufs.
                # Auch ein ungewöhnlich langer Timeout löst daher nicht fünf
                # Sekunden später bereits den nächsten Cloudversuch aus.
                last_update = time.time()

        time.sleep(5)

if __name__ == "__main__":
    main()
