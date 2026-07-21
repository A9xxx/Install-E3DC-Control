#!/usr/bin/env python3
import time
import json
import os
import re
import logging
import urllib.request
import math
import socket
from logging.handlers import RotatingFileHandler
import paho.mqtt.client as mqtt

try:
    from quiet_logging import install_quiet_info_filter
except ImportError:  # pragma: no cover - Paketimport
    from Installer.quiet_logging import install_quiet_info_filter
try:
    from json_cache import read_json_cached as _read_json_cached
except ImportError:  # pragma: no cover - Paketimport
    from Installer.json_cache import read_json_cached as _read_json_cached

# Pfade
LOG_DIR = "/var/www/html/logs"
RAMDISK_DIR = "/var/www/html/ramdisk"
CONFIG_CACHE = "/var/www/html/ramdisk/e3dc_config_cache.json"
VEHICLES_JSON_FILE = "/var/www/html/ramdisk/vehicles.json"
STORAGE_STATE_FILE = "/var/www/html/ramdisk/storage_manager_state.json"
WB_BUDGET_FILE = "/var/www/html/ramdisk/wb_pv_budget.json"
WALLBOX_NATIVE_FILE = "/var/www/html/ramdisk/wallbox_native.json"
HEIZSTAB_DATA_FILE = "/var/www/html/ramdisk/heizstab_data.json"
HA_INBOUND_FILE = "/var/www/html/ramdisk/mqtt_ha_inbound.json"
EXTERNAL_WB_FILE = "/var/www/html/ramdisk/external_wb.json"
PRICE_BOOST_PLAN_FILE = "/var/www/html/ramdisk/price_boost_plan.json"
PREDUMP_CONSUMER_PLAN_FILE = "/var/www/html/ramdisk/predump_consumer_plan.json"
AVAILABILITY_TOPIC_SUFFIX = "status/availability"
INBOUND_MAX_AGE_S = 180

def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(RAMDISK_DIR, exist_ok=True)
    logger = logging.getLogger("MqttHub")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    formatter = logging.Formatter('%(asctime)s - %(message)s', datefmt='%d.%m %H:%M:%S')

    fh = RotatingFileHandler(os.path.join(LOG_DIR, "e3dc_mqtt_hub.log"), maxBytes=1024*1024, backupCount=1)
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    install_quiet_info_filter(
        logger,
        min_interval_s=900.0,
        warning_min_interval_s=30.0,
        warning_max_interval_s=3600.0,
    )
    return logger

logger = setup_logging()


def _mqtt_context_valid(refresh=False):
    """Prüft diesen Dienststamm und mindestens eine kanonische Konfigurationsquelle."""
    try:
        module_file = os.path.abspath(__file__)
        if os.path.islink(module_file) or not os.path.isfile(module_file):
            return False
        install_root = os.path.dirname(os.path.dirname(module_file))
        markers = (
            os.path.join(install_root, "VERSION"),
            os.path.join(install_root, "installer_main.py"),
            os.path.join(install_root, "Installer", "installer_config.py"),
        )
        if not all(os.path.isfile(path) and not os.path.islink(path) for path in markers):
            return False
        config_sources = (
            "/var/www/html/data/e3dc_v4.json",
            "/var/www/html/e3dc_paths.json",
            os.path.join(install_root, "e3dc.config.txt"),
            "/var/www/html/data/e3dc.config.txt",
        )
        return any(os.path.isfile(path) and not os.path.islink(path) for path in config_sources)
    except Exception:
        return False


def create_mqtt_client():
    callback_versions = getattr(mqtt, "CallbackAPIVersion", None)
    version2 = getattr(callback_versions, "VERSION2", None)
    if version2 is not None:
        try:
            return mqtt.Client(callback_api_version=version2)
        except TypeError:
            pass
    return mqtt.Client()

def mqtt_reason_success(reason_code):
    if reason_code == 0:
        return True
    try:
        return int(reason_code) == 0
    except Exception:
        return str(reason_code).strip().lower() in ("0", "success")

def split_mqtt_endpoint(endpoint, default_port=1883):
    raw = str(endpoint or "").strip()
    host, sep, port_text = raw.partition(":")
    if not sep:
        return host, default_port
    try:
        return host, int(port_text)
    except Exception:
        return host, default_port

def log_mqtt_connect_error(label, host, port, exc):
    err_no = getattr(exc, "errno", None)
    if isinstance(exc, ConnectionRefusedError) or err_no in (111, 10061):
        logger.warning(
            f"{label} auf {host}:{port} nicht erreichbar: Verbindung abgelehnt. "
            "Broker (z.B. Mosquitto) starten/installieren oder MQTT-Broker-IP in der WebUI korrigieren."
        )
    else:
        logger.error(f"{label} Verbindungsfehler auf {host}:{port}: {exc}")

def read_json_file(path, max_age_s=None):
    data = _read_json_cached(path, max_age_s=max_age_s)
    return data if isinstance(data, dict) else {}

def write_json_atomic(path, payload, ensure_ascii=False):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    stamp = getattr(time, "time_ns", lambda: int(time.time() * 1000000000))()
    tmp = f"{path}.tmp.{os.getpid()}.{stamp}.{id(payload)}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=ensure_ascii)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _disabled_mqtt_config():
    return {
        "mqtt_hub_enable": "0",
        "mqtt_ha_inbound_enable": "0",
        "mqtt_hub_ip": "",
        "wb_ip": "",
        "wb2_ip": "",
        "context_valid": False,
    }


def _write_disabled_mqtt_state():
    now = int(time.time())
    write_json_atomic(
        HA_INBOUND_FILE,
        {"ts": now, "enabled": False, "context_valid": False, "sources": {}},
        ensure_ascii=False,
    )
    write_json_atomic(
        EXTERNAL_WB_FILE,
        {
            "ts": now,
            "power": None,
            "enabled": False,
            "context_valid": False,
            "state": "unknown",
            "wb1": {"power": None, "ts": now, "context_valid": False, "state": "unknown"},
            "wb2": {"power": None, "ts": now, "context_valid": False, "state": "unknown"},
        },
        ensure_ascii=False,
    )
    return False


def as_float(value, default=0.0):
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return default

def as_int(value, default=0):
    return int(round(as_float(value, default)))

def finite_float(value, default=None):
    try:
        val = float(str(value).strip().replace(",", "."))
        return val if math.isfinite(val) else default
    except Exception:
        return default

def as_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "ja", "on", "ein", "active", "aktiv")

def first_value(data, keys, default=None):
    for key in keys:
        if isinstance(data, dict) and key in data and data[key] is not None:
            return data[key]
    return default

def first_number(data, keys, default=0.0):
    return as_float(first_value(data, keys, default), default)

def publish_json(client, topic, payload, retain=False):
    if not _mqtt_context_valid(refresh=True):
        _write_disabled_mqtt_state()
        return False
    client.publish(topic, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), retain=retain)
    return True


def publish_raw(client, topic, payload, retain=False):
    if not _mqtt_context_valid(refresh=True):
        _write_disabled_mqtt_state()
        return False
    client.publish(topic, payload, retain=retain)
    return True


def publish_retained_offline(client, availability_topic, timeout_s=5.0):
    """Publiziert und bestätigt retained offline auch nach Verlust des Laufzeitkontexts."""
    if client is None:
        return False
    try:
        info = client.publish(availability_topic, "offline", qos=1, retain=True)
        rc = getattr(info, "rc", None)
        if rc is None and isinstance(info, (tuple, list)) and info:
            rc = info[0]
        success_rc = int(getattr(mqtt, "MQTT_ERR_SUCCESS", 0))
        if rc is None or int(rc) != success_rc:
            return False
        wait_for_publish = getattr(info, "wait_for_publish", None)
        is_published = getattr(info, "is_published", None)
        if not callable(wait_for_publish) or not callable(is_published):
            return False
        try:
            wait_for_publish(timeout=float(timeout_s))
        except TypeError:
            wait_for_publish(float(timeout_s))
        return bool(is_published())
    except Exception as exc:
        logger.error(f"Retained MQTT-offline konnte nicht bestätigt werden: {exc}")
        return False


def _abort_transport_for_lwt(client):
    """Schließt ungeordnet, damit der Broker das vorkonfigurierte retained LWT sendet."""
    if client is None:
        return False
    try:
        client.loop_stop()
    except Exception:
        pass
    try:
        mqtt_socket = client.socket()
    except Exception:
        mqtt_socket = None
    if mqtt_socket is None:
        return False
    try:
        mqtt_socket.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        mqtt_socket.close()
        return True
    except Exception:
        return False


def shutdown_mqtt_for_context_loss(client, other_clients, base_topic):
    """Sendet keinen regulären Disconnect, bevor retained offline bestätigt ist."""
    availability_topic = f"{base_topic}/{AVAILABILITY_TOPIC_SUFFIX}"
    offline_confirmed = publish_retained_offline(client, availability_topic) if client is not None else True
    if client is not None:
        if offline_confirmed:
            try:
                client.disconnect()
            except Exception as exc:
                logger.warning(f"MQTT DISCONNECT nach bestätigtem offline fehlgeschlagen: {exc}")
                _abort_transport_for_lwt(client)
            try:
                client.loop_stop()
            except Exception:
                pass
        else:
            # Ein geordneter Disconnect unterdrückt das LWT; daher ungeordnet
            # schließen, damit der Broker das bereits konfigurierte retained offline publiziert.
            _abort_transport_for_lwt(client)
    for mqtt_connection in other_clients or ():
        if mqtt_connection is None:
            continue
        try:
            mqtt_connection.disconnect()
        except Exception:
            pass
        try:
            mqtt_connection.loop_stop()
        except Exception:
            pass
    return offline_confirmed

def topic_safe(name):
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name).strip().lower())
    return safe.strip("_") or "value"

def is_current_window_active(plan):
    try:
        if not isinstance(plan, dict):
            return False
        now_ms = int(time.time() * 1000)
        for win in plan.get("windows", []) or []:
            if int(win.get("start_timestamp", 0) or 0) <= now_ms <= int(win.get("end_timestamp", 0) or 0):
                return True
        active = plan.get("active_window") or {}
        if active:
            return int(active.get("start_timestamp", 0) or 0) <= now_ms <= int(active.get("end_timestamp", 0) or 0)
    except Exception:
        return False
    return False

def update_inbound_telemetry(device, key, value, source_topic):
    if not _mqtt_context_valid(refresh=True):
        return _write_disabled_mqtt_state()

    def normalize_device_name(name):
        normalized = topic_safe(name)
        aliases = {
            "wb": "wallbox",
            "wb1": "wallbox1",
            "wb2": "wallbox2",
            "wallbox_1": "wallbox1",
            "wallbox_2": "wallbox2",
        }
        return aliases.get(normalized, normalized)

    wallbox_keys = {"power_w", "plugged", "charging", "soc", "range_km"}
    allowed = {
        "heatpump": {
            "power_w", "state", "mode", "ww_temp", "ww_target_temp", "flow_temp",
            "return_temp", "outside_temp", "heat_kw", "electric_w", "boost_active"
        },
        "heater": {"power_w", "water_temp", "target_temp", "state", "mode"},
        "wallbox": wallbox_keys,
        "wallbox1": wallbox_keys,
        "wallbox2": wallbox_keys,
        "house": {"extra_power_w"},
    }
    device = normalize_device_name(device)
    key = topic_safe(key)
    if device not in allowed or key not in allowed[device]:
        logger.warning(f"MQTT Eingangs-Topic ignoriert (nicht erlaubt): {source_topic}")
        return False

    text_keys = {"state", "mode"}
    invalid_text_payloads = {"", "unavailable", "none", "null", "nan"}
    invalid_numeric_payloads = invalid_text_payloads | {"unknown"}
    if key in text_keys:
        if value is None:
            logger.info(f"MQTT Eingangs-Topic ohne gueltigen Statuswert ignoriert: {source_topic}")
            return False
        value = str(value).strip()
        if value.lower() in invalid_text_payloads:
            logger.info(f"MQTT Eingangs-Topic ohne gueltigen Statuswert ignoriert: {source_topic}")
            return False
    elif value is None or (isinstance(value, str) and value.strip().lower() in invalid_numeric_payloads):
        logger.info(f"MQTT Eingangs-Topic ohne gueltigen Messwert ignoriert: {source_topic}")
        return False
    if key not in text_keys and isinstance(value, (int, float)) and not math.isfinite(float(value)):
        logger.info(f"MQTT Eingangs-Topic ohne endlichen Messwert ignoriert: {source_topic}")
        return False

    data = read_json_file(HA_INBOUND_FILE)
    if not data:
        data = {"ts": int(time.time()), "sources": {}}
    data["context_valid"] = True
    data["enabled"] = True
    sources = data.setdefault("sources", {})
    now = int(time.time())
    dev = sources.setdefault(device, {})
    dev[key] = value
    dev["ts"] = now
    dev["topic"] = source_topic
    dev.setdefault("_updated", {})[key] = now
    dev.setdefault("_topics", {})[key] = source_topic
    data["ts"] = now

    try:
        os.makedirs(RAMDISK_DIR, exist_ok=True)
        write_json_atomic(HA_INBOUND_FILE, data, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Fehler beim Schreiben von mqtt_ha_inbound.json: {e}")
        return False

def handle_inbound_telemetry(base_topic, topic, payload, enabled=True):
    if not _mqtt_context_valid(refresh=True):
        return _write_disabled_mqtt_state()
    prefix = f"{base_topic}/in/"
    if not topic.startswith(prefix):
        return False
    if not enabled:
        logger.info(f"MQTT Eingangs-Telemetrie deaktiviert, Topic ignoriert: {topic}")
        return True
    parts = topic[len(prefix):].strip("/").split("/")
    if len(parts) < 2:
        return True
    device = parts[0]
    key = parts[-1]
    update_inbound_telemetry(device, key, payload, topic)
    return True

def build_ha_state(live, cfg=None):
    if not _mqtt_context_valid(refresh=True):
        _write_disabled_mqtt_state()
        return {
            "ts": int(time.time()),
            "available": False,
            "context_valid": False,
            "mqtt_inbound_fresh": False,
        }

    def fresh_source(source):
        if not isinstance(source, dict):
            return {}
        ts = as_int(source.get("ts", 0), 0)
        if ts and time.time() - ts > INBOUND_MAX_AGE_S:
            return {}
        return source

    cfg = cfg or {}
    storage_state = read_json_file(STORAGE_STATE_FILE, max_age_s=900)
    wb_budget = read_json_file(WB_BUDGET_FILE, max_age_s=180)
    wallbox_native = read_json_file(WALLBOX_NATIVE_FILE, max_age_s=180)
    heizstab = read_json_file(HEIZSTAB_DATA_FILE, max_age_s=180)
    inbound_enabled = as_bool(cfg.get("mqtt_ha_inbound_enable", "1"))
    inbound = read_json_file(HA_INBOUND_FILE, max_age_s=INBOUND_MAX_AGE_S) if inbound_enabled else {}
    if isinstance(inbound, dict) and inbound.get("context_valid") is False:
        inbound = {}
    price_boost = read_json_file(PRICE_BOOST_PLAN_FILE, max_age_s=1800)
    predump_plan = read_json_file(PREDUMP_CONSUMER_PLAN_FILE, max_age_s=180)
    inbound_sources = inbound.get("sources", {}) if isinstance(inbound.get("sources"), dict) else {}
    inbound_wp = fresh_source(inbound_sources.get("heatpump", {}))
    inbound_heater = fresh_source(inbound_sources.get("heater", {}))
    inbound_wb = fresh_source(inbound_sources.get("wallbox", {}))
    inbound_wb1 = fresh_source(inbound_sources.get("wallbox1", inbound_wb))
    inbound_wb2 = fresh_source(inbound_sources.get("wallbox2", {}))
    inbound_house = fresh_source(inbound_sources.get("house", {}))

    energy_score = {}
    if isinstance(wb_budget.get("energy_score"), dict):
        energy_score = wb_budget.get("energy_score")

    pv_w = first_number(live, ("pv", "PV_Power", "P_PV", "Solar_Power"), 0)
    grid_w = first_number(live, ("grid", "Grid_Power", "P_Grid", "Netz_Power"), 0)
    bat_w = first_number(live, ("bat", "Battery_Power", "P_Battery", "Bat_Power"), 0)
    soc = first_number(live, ("soc", "SOC", "Battery_SOC", "bat_soc"), 0)
    home_w = first_number(live, ("home_raw", "home", "Home_Power", "Hausverbrauch", "P_Home"), 0)
    wb1_w = first_number(live, ("wb", "Wallbox_Power", "Wb_Power", "WB_Power"), 0)
    wb2_w = first_number(live, ("wb2", "Wallbox2_Power", "Wb2_Power", "WB2_Power"), 0)
    wb1_w = max(wb1_w, first_number(inbound_wb1, ("power_w",), 0))
    wb2_w = max(wb2_w, first_number(inbound_wb2, ("power_w",), 0))
    wallbox_w = max(0, wb1_w) + max(0, wb2_w)
    wallbox_w = max(wallbox_w, first_number(inbound_wb, ("power_w",), 0))
    wp_w = max(first_number(live, ("wp", "WP_Power", "wp_electric_w"), 0), first_number(inbound_wp, ("power_w", "electric_w"), 0))
    heater_w = max(first_number(live, ("hs_power", "Heizstab_Power", "heizstab_power"), 0), first_number(inbound_heater, ("power_w",), 0))
    price_ct = first_number(live, ("price_ct", "strompreis_ct", "price"), 0)
    if home_w <= 0 and first_number(inbound_house, ("extra_power_w",), 0) > 0:
        home_w = first_number(inbound_house, ("extra_power_w",), 0)

    free_for_limbs_w = first_number(
        live,
        ("free_for_limbs_w", "wb_budget_w"),
        first_number(energy_score, ("free_for_limbs_w",), first_number(wb_budget, ("budget_w",), 0)),
    )
    grid_export_w = max(0, -grid_w)
    grid_import_w = max(0, grid_w)
    surplus_w = max(0, first_number(energy_score, ("pv_surplus_w",), grid_export_w))

    storage_mode = str(first_value(live, ("storage_state",), first_value(storage_state, ("state",), "")) or "")
    storage_reason = str(first_value(live, ("storage_reason",), first_value(storage_state, ("reason",), "")) or "")
    storage_target_soc = first_number(
        live,
        ("target_soc", "storage_target_soc", "soll_soc_now"),
        first_number(storage_state, ("target_soc", "ziel_soc", "soll_soc"), 0),
    )

    predump_active = (
        storage_mode == "pre_discharge"
        or as_bool(first_value(wb_budget, ("predump_active",), False))
        or as_bool(first_value(predump_plan, ("active",), False))
    )
    abregel_active = storage_mode == "abregelschutz" or "ABREGELSCHUTZ" in storage_reason.upper()
    cheap_price_active = as_bool(first_value(live, ("cheap_grid_boost_active",), False)) or is_current_window_active(price_boost)

    price_allow = price_boost.get("allow", {}) if isinstance(price_boost.get("allow"), dict) else {}
    predump_allow = predump_plan.get("allow", {}) if isinstance(predump_plan.get("allow"), dict) else {}
    wb_state = str(first_value(live, ("wb_budget_state",), first_value(wb_budget, ("state",), "")) or "")

    wallbox_allowed = (
        free_for_limbs_w >= 300
        or as_bool(first_value(price_allow, ("wallbox",), False))
        or (predump_active and as_bool(first_value(predump_allow, ("wallbox",), False)))
        or wb_state in ("run", "ok", "abregelschutz")
    )
    heatpump_allowed = (
        free_for_limbs_w >= 500
        or as_bool(first_value(price_allow, ("heatpump",), False))
        or (predump_active and as_bool(first_value(predump_allow, ("heatpump",), False)))
        or as_bool(first_value(live, ("wp_boost_active",), False))
        or as_bool(first_value(live, ("wp_price_boost",), False))
    )
    heater_allowed = (
        free_for_limbs_w >= 500
        or as_bool(first_value(price_allow, ("heater",), False))
        or (predump_active and as_bool(first_value(predump_allow, ("heater",), False)))
        or as_bool(first_value(heizstab, ("hs_auto_mode", "predump_heater_active"), False))
    )

    wallbox_plugged = (
        as_bool(first_value(wallbox_native, ("plug_state", "car_connected"), False))
        or as_bool(first_value(live, ("wb_plug_state", "wb2_plug_state"), False))
        or as_bool(first_value(inbound_wb1, ("plugged",), False))
        or as_bool(first_value(inbound_wb2, ("plugged",), False))
    )
    wallbox_charging = (
        as_bool(first_value(wallbox_native, ("charge_state", "charging"), False))
        or as_bool(first_value(inbound_wb1, ("charging",), False))
        or as_bool(first_value(inbound_wb2, ("charging",), False))
        or wallbox_w > 100
    )
    v2h_state = live.get("v2h") if isinstance(live.get("v2h"), dict) else {}

    state = {
        "ts": int(time.time()),
        "source": "e3dc_mqtt_hub",
        "pv_w": as_int(pv_w),
        "grid_w": as_int(grid_w),
        "grid_export_w": as_int(grid_export_w),
        "grid_import_w": as_int(grid_import_w),
        "house_w": as_int(home_w),
        "battery_w": as_int(bat_w),
        "battery_soc": round(soc, 1),
        "wallbox_w": as_int(wallbox_w),
        "wallbox1_w": as_int(wb1_w),
        "wallbox2_w": as_int(wb2_w),
        "wallbox_plugged": wallbox_plugged,
        "wallbox_charging": wallbox_charging,
        "wallbox_allowed": wallbox_allowed,
        "wallbox_budget_w": as_int(free_for_limbs_w),
        "heatpump_w": as_int(wp_w),
        "heatpump_allowed": heatpump_allowed,
        "heater_w": as_int(heater_w),
        "heater_allowed": heater_allowed,
        "available_surplus_w": as_int(surplus_w),
        "free_for_consumers_w": as_int(free_for_limbs_w),
        "storage_mode": storage_mode,
        "storage_reason": storage_reason,
        "storage_target_soc": round(storage_target_soc, 1),
        "predump_active": predump_active,
        "abregel_active": abregel_active,
        "cheap_price_active": cheap_price_active,
        "price_ct": round(price_ct, 3),
        "v2h_allowed": as_bool(first_value(v2h_state, ("allowed",), first_value(live, ("v2h_allowed",), False))),
        "v2h_read_only": as_bool(first_value(v2h_state, ("read_only",), True)),
        "v2h_monitoring": as_bool(first_value(v2h_state, ("monitoring",), False)),
        "v2h_active": as_bool(first_value(v2h_state, ("active",), False)),
        "v2h_detected_discharge": as_bool(first_value(v2h_state, ("detected_discharge",), False)),
        "v2h_reason": str(first_value(v2h_state, ("reason",), "") or ""),
        "wp_mode_text": str(first_value(live, ("wp_mode_text",), first_value(inbound_wp, ("mode", "state"), ""))),
        "wp_ww_temp": first_value(live, ("wp_ww_temp",), first_value(inbound_wp, ("ww_temp",), None)),
        "wp_rl_temp": first_value(live, ("wp_rl_temp",), first_value(inbound_wp, ("return_temp",), None)),
        "wp_vl_temp": first_value(live, ("wp_vl_temp",), first_value(inbound_wp, ("flow_temp",), None)),
        "heater_water_temp": first_value(live, ("elwa_water_temp_c", "hs_water_temp_c"), first_value(inbound_heater, ("water_temp",), first_value(heizstab, ("elwa_water_temp_c",), None))),
        "mqtt_inbound_fresh": bool(inbound),
    }
    return state

def read_config():
    if not _mqtt_context_valid(refresh=True):
        _write_disabled_mqtt_state()
        return _disabled_mqtt_config()
    default_v4_file = "/var/www/html/data/e3dc_v4.json"
    try:
        if os.path.exists(CONFIG_CACHE):
            with open(CONFIG_CACHE, 'r') as f:
                cache = json.load(f)
            if isinstance(cache, dict) and isinstance(cache.get('config'), dict):
                cache_mtime = str(cache.get('mtime', ''))
                v4_mtime = str(int(os.path.getmtime(default_v4_file))) if os.path.exists(default_v4_file) else ''
                if cache_mtime and cache_mtime == v4_mtime:
                    return cache.get('config', {})
    except: pass
    # Fallback: direkt aus Dateisystem lesen (Pfade dynamisch!)
    config = {}
    import sys, os as _os
    _sdir = _os.path.dirname(_os.path.abspath(__file__))
    _repo_root = _os.path.dirname(_sdir)
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    from Installer.utils import get_paths as _gp
    _paths = _gp()
    install_path = _paths['install_path']
    data_dir = _paths['data_dir']

    # Erst V4 JSON lesen (neue Quelle), dann Legacy e3dc.config.txt als Fallback
    v4_file = os.path.join(data_dir, 'e3dc_v4.json')
    if os.path.exists(v4_file):
        try:
            with open(v4_file, 'r') as f:
                config = json.load(f)
        except: pass

    if not config:
        # Legacy: e3dc.config.txt (nur wenn V4 leer oder nicht vorhanden)
        cfg_file = os.path.join(install_path, 'e3dc.config.txt')
        if not os.path.exists(cfg_file):
            cfg_file = '/var/www/html/data/e3dc.config.txt'
        try:
            with open(cfg_file, 'r') as f:
                for line in f:
                    if '=' in line and not line.strip().startswith('#'):
                        k, v = line.split('=', 1)
                        config[k.strip().lower()] = v.strip().strip('"').strip("'")
        except: pass
    return config

def send_ha_discovery(client, base_topic):
    """Sendet Auto-Discovery Payloads an Home Assistant."""
    if not _mqtt_context_valid(refresh=True):
        return _write_disabled_mqtt_state()
    logger.info("Sende Home Assistant Auto-Discovery Konfigurationen...")
    state_topic = f"{base_topic}/ha/state"
    availability_topic = f"{base_topic}/{AVAILABILITY_TOPIC_SUFFIX}"
    device = {
        "identifiers": ["e3dc_control_ems"],
        "name": "E3DC-Control EMS",
        "manufacturer": "A9x / E3DC-Control",
        "model": "Native Python EMS"
    }

    sensors = [
        {"id": "pv_power", "name": "PV Leistung", "unit": "W", "class": "power", "key": "pv_w"},
        {"id": "grid_power", "name": "Netz Leistung", "unit": "W", "class": "power", "key": "grid_w"},
        {"id": "grid_export", "name": "Netzeinspeisung", "unit": "W", "class": "power", "key": "grid_export_w"},
        {"id": "grid_import", "name": "Netzbezug", "unit": "W", "class": "power", "key": "grid_import_w"},
        {"id": "home_power", "name": "Hausverbrauch", "unit": "W", "class": "power", "key": "house_w"},
        {"id": "battery_power", "name": "Batterie Leistung", "unit": "W", "class": "power", "key": "battery_w"},
        {"id": "battery_soc", "name": "Batterie SoC", "unit": "%", "class": "battery", "key": "battery_soc"},
        {"id": "wallbox_power", "name": "Wallbox Leistung", "unit": "W", "class": "power", "key": "wallbox_w"},
        {"id": "wallbox_budget", "name": "Wallbox Freigabe-Budget", "unit": "W", "class": "power", "key": "wallbox_budget_w"},
        {"id": "heatpump_power", "name": "Wärmepumpen Leistung", "unit": "W", "class": "power", "key": "heatpump_w"},
        {"id": "heater_power", "name": "Heizstab Leistung", "unit": "W", "class": "power", "key": "heater_w"},
        {"id": "available_surplus", "name": "Verfügbarer Überschuss", "unit": "W", "class": "power", "key": "available_surplus_w"},
        {"id": "free_for_consumers", "name": "Freigabe Verbraucher", "unit": "W", "class": "power", "key": "free_for_consumers_w"},
        {"id": "storage_target_soc", "name": "Speicher Ziel-SoC", "unit": "%", "class": "battery", "key": "storage_target_soc"},
        {"id": "strompreis", "name": "Aktueller Strompreis", "unit": "ct/kWh", "class": "monetary", "key": "price_ct"},
        {"id": "wp_ww_temp", "name": "Warmwasser Ist", "unit": "°C", "class": "temperature", "key": "wp_ww_temp"},
        {"id": "wp_vl_temp", "name": "WP Vorlauf", "unit": "°C", "class": "temperature", "key": "wp_vl_temp"},
        {"id": "wp_rl_temp", "name": "WP Rücklauf", "unit": "°C", "class": "temperature", "key": "wp_rl_temp"},
        {"id": "heater_water_temp", "name": "Heizstab Wasser", "unit": "°C", "class": "temperature", "key": "heater_water_temp"},
        {"id": "storage_mode", "name": "Speicher Modus", "unit": "", "class": None, "key": "storage_mode"},
    ]

    for s in sensors:
        topic = f"homeassistant/sensor/e3dc_control/{s['id']}/config"
        payload = {
            "name": s['name'],
            "state_topic": state_topic,
            "value_template": "{{ value_json.%s | default('unknown') }}" % s["key"],
            "unique_id": f"e3dc_ctrl_{s['id']}",
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device
        }
        if s['unit']:
            payload["unit_of_measurement"] = s['unit']
        if s.get("class"):
            payload["device_class"] = s["class"]
        if s.get("class") in ("power", "battery", "temperature", "monetary"):
            payload["state_class"] = "measurement"
        publish_json(client, topic, payload, retain=True)

    binary_sensors = [
        {"id": "wallbox_allowed", "name": "Wallbox Freigabe", "key": "wallbox_allowed"},
        {"id": "wallbox_plugged", "name": "Wallbox Fahrzeug verbunden", "key": "wallbox_plugged"},
        {"id": "wallbox_charging", "name": "Wallbox lädt", "key": "wallbox_charging"},
        {"id": "heatpump_allowed", "name": "Wärmepumpe Freigabe", "key": "heatpump_allowed"},
        {"id": "heater_allowed", "name": "Heizstab Freigabe", "key": "heater_allowed"},
        {"id": "predump_active", "name": "Pre-Dump aktiv", "key": "predump_active"},
        {"id": "abregel_active", "name": "Abregelschutz aktiv", "key": "abregel_active"},
        {"id": "cheap_price_active", "name": "Preisfenster aktiv", "key": "cheap_price_active"},
        {"id": "v2h_allowed", "name": "V2H/V2G erlaubt (Read-only)", "key": "v2h_allowed"},
        {"id": "v2h_monitoring", "name": "V2H/V2G Beobachtung aktiv", "key": "v2h_monitoring"},
        {"id": "v2h_active", "name": "V2H/V2G Steuerung aktiv (Read-only)", "key": "v2h_active"},
        {"id": "v2h_detected_discharge", "name": "V2H/V2G Entladung erkannt", "key": "v2h_detected_discharge"},
    ]
    for s in binary_sensors:
        topic = f"homeassistant/binary_sensor/e3dc_control/{s['id']}/config"
        payload = {
            "name": s["name"],
            "state_topic": state_topic,
            "value_template": "{{ 'ON' if value_json.%s else 'OFF' }}" % s["key"],
            "payload_on": "ON",
            "payload_off": "OFF",
            "unique_id": f"e3dc_ctrl_{s['id']}",
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
        }
        publish_json(client, topic, payload, retain=True)

def on_connect(client, userdata, flags, reason_code, properties=None):
    if not _mqtt_context_valid(refresh=True):
        _write_disabled_mqtt_state()
        return
    if mqtt_reason_success(reason_code):
        logger.info("MQTT verbunden. Abonniere Fahrzeug- und HA/ioBroker-Topics...")
        base_topic = str(userdata.get('base_topic', 'e3dc') or 'e3dc').strip().strip('/') or 'e3dc'
        # Hört auf alle Fahrzeuge nach dem Muster: e3dc/vehicle/<NAME>/<ATTRIBUT>
        client.subscribe(f"{base_topic}/vehicle/#")
        # Kontrollierte HA/ioBroker-Telemetrie: keine Befehle, nur Allowlist-Werte.
        client.subscribe(f"{base_topic}/in/#")
        logger.info(
            "Abonniere HA/ioBroker Eingangs-Telemetrie: %s/in/# "
            "(u.a. wallbox/power_w, wallbox2/power_w)" % base_topic
        )

        # Fallback für ein spezifisches, in der Config definiertes Topic (z.B. EVCC direkt)
        for key in ['sub_topic', 'sub_topic2']:
            st = userdata.get(key, '')
            if st:
                client.subscribe(st)
                logger.info(f"Abonniere SoC Topic auf Broker: {st}")
    else:
        logger.error(f"MQTT Verbindungsfehler, RC: {reason_code}")

def on_message(client, userdata, msg):
    if not _mqtt_context_valid(refresh=True):
        _write_disabled_mqtt_state()
        return
    topic = msg.topic
    try:
        payload = msg.payload.decode('utf-8').strip()
    except:
        return

    try:
        val = float(payload)
        if not math.isfinite(val):
            logger.info(f"MQTT Topic mit NaN/Inf ignoriert: {topic}")
            return
    except:
        if payload.lower() in ['true', 'on', '1']: val = True
        elif payload.lower() in ['false', 'off', '0']: val = False
        else: val = payload

    base_topic = userdata.get('base_topic', 'e3dc')
    sub_topic = userdata.get('sub_topic', '')
    sub_topic2 = userdata.get('sub_topic2', '')

    if handle_inbound_telemetry(base_topic, topic, val, userdata.get('inbound_enable', True)):
        return

    # Format: e3dc/vehicle/tesla/soc
    match = re.match(rf"^{re.escape(base_topic)}/vehicle/([^/]+)/([^/]+)$", topic)
    if match:
        v_id = match.group(1)
        attr = match.group(2).lower()
    elif sub_topic and topic == sub_topic:
        v_id = userdata.get('sub_topic_name', 'Mqtt_car')
        attr = "soc"
    elif sub_topic2 and topic == sub_topic2:
        v_id = userdata.get('sub_topic_name2', 'Mqtt_car2')
        attr = "soc"
    else:
        return

    attr_map = {"soc": "soc", "plugged": "is_plugged_in", "range": "range_km", "target": "target_soc", "capacity": "capacity"}
    internal_attr = attr_map.get(attr)
    if not internal_attr:
        return

    old_data = {"ts": int(time.time()), "vehicles": []}
    try:
        if os.path.exists(VEHICLES_JSON_FILE):
            with open(VEHICLES_JSON_FILE, "r") as f:
                old_data = json.load(f)
    except: pass

    vehicles = old_data.get("vehicles", [])
    target_v = next((v for v in vehicles if v.get("id") == v_id), None)

    if not target_v:
        target_v = {"id": v_id, "name": v_id.capitalize(), "is_plugged_in": True, "soc": 0}
        vehicles.append(target_v)

    target_v[internal_attr] = val
    target_v["last_updated_at"] = int(time.time())

    # Fallback Name setzen, falls noch nicht vorhanden
    if internal_attr == "soc" and "name" not in target_v:
        target_v["name"] = v_id.capitalize()

    old_data["vehicles"] = vehicles
    old_data["ts"] = int(time.time())
    if "error" in old_data:
        del old_data["error"]

    try:
        os.makedirs(RAMDISK_DIR, exist_ok=True)
        write_json_atomic(VEHICLES_JSON_FILE, old_data)
    except Exception as e:
        logger.error(f"Fehler beim Schreiben der vehicles.json: {e}")

def on_wb_message(client, userdata, msg):
    if not _mqtt_context_valid(refresh=True):
        _write_disabled_mqtt_state()
        return
    try:
        payload = msg.payload.decode('utf-8').strip()
        topic = msg.topic

        # --- NEU: Prüfe ob dieses Topic eigentlich ein SoC-Topic ist ---
        sub_topic = userdata.get('sub_topic', '')
        sub_topic2 = userdata.get('sub_topic2', '')

        if (sub_topic and topic == sub_topic) or (sub_topic2 and topic == sub_topic2):
             on_message(client, userdata, msg)
             return

        wb_id = userdata.get('wb_id', 'wb1')
        power = None

        if payload.startswith('{') and payload.endswith('}'):
            try:
                data = json.loads(payload)
                data_lower = {k.lower(): v for k, v in data.items()}
                for key in ['power', 'p', 'total_power', 'watt', 'w', 'power_w']:
                    if key in data_lower:
                        power = finite_float(data_lower[key])
                        break
            except: pass

        if power is None:
            power = finite_float(payload)

        if power is None: return
        power = max(0.0, power)

        ext_wb_file = EXTERNAL_WB_FILE
        os.makedirs(RAMDISK_DIR, exist_ok=True)
        all_wb_data = {
            "context_valid": True,
            "enabled": True,
            "wb1": {"power": None, "ts": 0, "context_valid": False},
            "wb2": {"power": None, "ts": 0, "context_valid": False},
        }
        try:
            if os.path.exists(ext_wb_file):
                with open(ext_wb_file, 'r') as f:
                    old_data = json.load(f)
                    if "wb1" in old_data: all_wb_data.update(old_data)
                    else: all_wb_data["wb1"] = old_data
        except: pass

        all_wb_data["context_valid"] = True
        all_wb_data["enabled"] = True
        all_wb_data[wb_id] = {
            "power": power,
            "ts": int(time.time()),
            "topic": topic,
            "source": "direct_mqtt",
            "context_valid": True,
        }
        all_wb_data["power"] = all_wb_data["wb1"]["power"]
        all_wb_data["ts"] = all_wb_data["wb1"]["ts"]
        all_wb_data["source"] = "direct_mqtt"

        write_json_atomic(ext_wb_file, all_wb_data)

        inbound_device = "wallbox2" if wb_id == "wb2" else "wallbox1"
        update_inbound_telemetry(inbound_device, "power_w", power, topic)
        if power > 50:
            update_inbound_telemetry(inbound_device, "charging", True, topic)
    except Exception as e:
        logger.error(f"Fehler in on_wb_message: {e}")

def main():
    if not _mqtt_context_valid(refresh=True):
        _write_disabled_mqtt_state()
        logger.error("MQTT-Hub gesperrt: Installationskontext ist nicht vertrauenswuerdig.")
        return
    cfg = read_config()
    broker_ip = cfg.get('mqtt_hub_ip', '127.0.0.1')
    broker_port = int(cfg.get('mqtt_hub_port', 1883))
    user = cfg.get('mqtt_hub_user', '')
    password = cfg.get('mqtt_hub_pass', '')
    base_topic = str(cfg.get('mqtt_hub_topic', 'e3dc') or 'e3dc').strip().strip('/') or 'e3dc'
    sub_topic = cfg.get('mqtt_hub_sub_soc_topic', '')
    sub_topic_name = cfg.get('mqtt_hub_sub_soc_name', 'Mqtt_car')
    sub_topic2 = cfg.get('mqtt_hub_sub_soc_topic_2', '')
    sub_topic_name2 = cfg.get('mqtt_hub_sub_soc_name_2', 'Mqtt_car2')
    inbound_enable = as_bool(cfg.get('mqtt_ha_inbound_enable', '1'))

    wb_ip = cfg.get('wb_ip', '')
    wb_topic = cfg.get('wb_topic', '')
    wb_user = cfg.get('wb_user', '')
    wb_pass = cfg.get('wb_pass', '')

    wb2_ip = cfg.get('wb2_ip', '')
    wb2_topic = cfg.get('wb2_topic', '')
    wb2_user = cfg.get('wb2_user', '')
    wb2_pass = cfg.get('wb2_pass', '')

    has_main_broker = bool(broker_ip and broker_ip != '0.0.0.0')
    has_wb_broker = bool(wb_ip and wb_ip != '0.0.0.0' and wb_topic)
    has_wb2_broker = bool(wb2_ip and wb2_ip != '0.0.0.0' and wb2_topic)

    if not has_main_broker and not has_wb_broker and not has_wb2_broker:
        logger.warning("Kein MQTT Broker in der Config definiert. Beende Dienst.")
        return

    def connect_main_mqtt_client():
        if not _mqtt_context_valid(refresh=True):
            _write_disabled_mqtt_state()
            return None
        logger.info(f"Verbinde zu MQTT Broker auf {broker_ip}:{broker_port}...")
        mqtt_client = create_mqtt_client()
        mqtt_client.user_data_set({'base_topic': base_topic, 'sub_topic': sub_topic, 'sub_topic2': sub_topic2, 'sub_topic_name': sub_topic_name, 'sub_topic_name2': sub_topic_name2, 'inbound_enable': inbound_enable})
        mqtt_client.on_connect = on_connect
        mqtt_client.on_message = on_message
        mqtt_client.will_set(f"{base_topic}/{AVAILABILITY_TOPIC_SUFFIX}", "offline", qos=1, retain=True)
        if user: mqtt_client.username_pw_set(user, password)

        try:
            mqtt_client.connect(broker_ip, broker_port, 60)
            mqtt_client.loop_start()
            publish_raw(mqtt_client, f"{base_topic}/{AVAILABILITY_TOPIC_SUFFIX}", "online", retain=True)
            send_ha_discovery(mqtt_client, base_topic)
            return mqtt_client
        except Exception as e:
            log_mqtt_connect_error("MQTT Broker", broker_ip, broker_port, e)
            try:
                mqtt_client.loop_stop()
            except Exception:
                pass
            return None

    client = connect_main_mqtt_client() if has_main_broker else None
    next_main_retry = time.time() + 60 if has_main_broker and client is None else 0

    wb_client = None
    wb2_client = None

    if has_wb_broker:
        if not _mqtt_context_valid(refresh=True):
            _write_disabled_mqtt_state()
            return
        wb_host, wb_port = split_mqtt_endpoint(wb_ip)
        logger.info(f"Verbinde zu Wallbox 1 MQTT Broker auf {wb_host}:{wb_port} (Topic: {wb_topic})...")
        wb_client = create_mqtt_client()
        wb_client.user_data_set({'wb_id': 'wb1', 'sub_topic': sub_topic, 'sub_topic2': sub_topic2, 'sub_topic_name': sub_topic_name, 'sub_topic_name2': sub_topic_name2, 'base_topic': base_topic})
        wb_client.on_message = on_wb_message
        if wb_user:
            wb_client.username_pw_set(wb_user, wb_pass)
        try:
            wb_client.connect(wb_host, wb_port, 60)
            wb_client.subscribe(wb_topic)
            if sub_topic: wb_client.subscribe(sub_topic)
            if sub_topic2: wb_client.subscribe(sub_topic2)
            wb_client.loop_start()
        except Exception as e:
            log_mqtt_connect_error("Wallbox 1 MQTT Broker", wb_host, wb_port, e)

    if has_wb2_broker:
        if not _mqtt_context_valid(refresh=True):
            _write_disabled_mqtt_state()
            return
        wb2_host, wb2_port = split_mqtt_endpoint(wb2_ip)
        logger.info(f"Verbinde zu Wallbox 2 MQTT Broker auf {wb2_host}:{wb2_port} (Topic: {wb2_topic})...")
        wb2_client = create_mqtt_client()
        wb2_client.user_data_set({'wb_id': 'wb2', 'sub_topic': sub_topic, 'sub_topic2': sub_topic2, 'sub_topic_name': sub_topic_name, 'sub_topic_name2': sub_topic_name2, 'base_topic': base_topic})
        wb2_client.on_message = on_wb_message
        if wb2_user:
            wb2_client.username_pw_set(wb2_user, wb2_pass)
        try:
            wb2_client.connect(wb2_host, wb2_port, 60)
            wb2_client.subscribe(wb2_topic)
            if sub_topic: wb2_client.subscribe(sub_topic)
            if sub_topic2: wb2_client.subscribe(sub_topic2)
            wb2_client.loop_start()
        except Exception as e:
            log_mqtt_connect_error("Wallbox 2 MQTT Broker", wb2_host, wb2_port, e)

    last_data = ""
    last_ha_state = ""
    last_individual = {}
    while True:
        if not _mqtt_context_valid(refresh=True):
            _write_disabled_mqtt_state()
            offline_confirmed = shutdown_mqtt_for_context_loss(
                client,
                (wb_client, wb2_client),
                base_topic,
            )
            if not offline_confirmed:
                logger.critical(
                    "MQTT-Kontextverlust: retained offline nicht direkt bestätigt; "
                    "Transport ungraceful für retained LWT geschlossen."
                )
            logger.error("MQTT-Hub beendet: Installationskontext ist nicht mehr vertrauenswuerdig.")
            return
        if has_main_broker and client is None and time.time() >= next_main_retry:
            client = connect_main_mqtt_client()
            next_main_retry = time.time() + 60 if client is None else 0
        if client:
            try:
                req = urllib.request.Request("http://127.0.0.1/get_live_json.php")
                with urllib.request.urlopen(req, timeout=2) as response:
                    new_data = response.read().decode('utf-8')
                    if new_data != last_data:
                        publish_raw(client, f"{base_topic}/live", new_data, retain=False)
                        live_json = json.loads(new_data)
                        ha_state = build_ha_state(live_json, cfg)
                        ha_state_json = json.dumps(ha_state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        if ha_state_json != last_ha_state:
                            publish_json(client, f"{base_topic}/ha/state", ha_state, retain=True)
                            last_ha_state = ha_state_json

                            for key, value in ha_state.items():
                                if isinstance(value, (dict, list)):
                                    continue
                                if last_individual.get(key) != value:
                                    publish_raw(client, f"{base_topic}/ha/{topic_safe(key)}", str(value), retain=True)
                                    last_individual[key] = value
                        last_data = new_data
            except Exception as e:
                logger.debug(f"MQTT Live-Publish pausiert: {e}")
        time.sleep(2)

if __name__ == "__main__":
    main()
