#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import time
import json
import logging
import inspect
import hashlib
import re
import stat
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# Pfade dynamisch ermitteln (niemals hardcoded!) -- Reihenfolge: e3dc_v4.json -> e3dc_paths.json -> Defaults
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
try:
    from Installer.utils import get_paths as _get_paths
except ImportError:  # pragma: no cover - package-less compatibility
    from utils import get_paths as _get_paths
try:
    from Installer.tariff_schedule import (
        configured_billing_price_for_timestamp,
        parse_special_tariff_schedule as _parse_special_tariff_schedule,
        recurring_tariff_slots,
        special_tariff_price_for_datetime,
        supports_spot_market_prices,
        TARIFF_TIMEZONE_NAME,
        tariff_type as configured_tariff_type,
    )
except ImportError:  # pragma: no cover - package-less compatibility
    from tariff_schedule import (
        configured_billing_price_for_timestamp,
        parse_special_tariff_schedule as _parse_special_tariff_schedule,
        recurring_tariff_slots,
        special_tariff_price_for_datetime,
        supports_spot_market_prices,
        TARIFF_TIMEZONE_NAME,
        tariff_type as configured_tariff_type,
    )
_p = _get_paths()
try:
    from runtime_logging import configure_service_logger
except ImportError:  # pragma: no cover - Paketimport
    from Installer.runtime_logging import configure_service_logger

INSTALL_DIR  = _p['install_path']
RAMDISK_DIR  = _p['ramdisk_dir']
DATA_DIR     = _p['data_dir']
V4_CONFIG_FILE = os.path.join(DATA_DIR, 'e3dc_v4.json')

EPEX_OUTPUT_FILE = os.path.join(RAMDISK_DIR, "epex_daten.json")
ECO_SCORE_FILE   = os.path.join(RAMDISK_DIR, "eco_score.json")
PRICE_BOOST_PLAN_FILE = os.path.join(RAMDISK_DIR, "price_boost_plan.json")
EPEX_CACHE_FILE = os.path.join(DATA_DIR, "epex_daten_last.json")
MARKET_VALUE_SOLAR_FILE = os.path.join(RAMDISK_DIR, "market_value_solar.json")
MARKET_VALUE_SOLAR_CACHE_FILE = os.path.join(DATA_DIR, "market_value_solar_last.json")
TIBBER_API_URL = "https://api.tibber.com/v1-beta/gql"
ENTSOE_API_ENDPOINTS = (
    "https://web-api.tp.entsoe.eu/api",
    "https://external-api.tp.entsoe.eu/api",
)
ENTSOE_DE_LU_DOMAIN = "10Y1001A1001A82H"
PRICE_BOOST_PUBLISH_INTERVAL_S = 30 * 60
PRICE_BOOST_PLAN_MAX_AGE_S = PRICE_BOOST_PUBLISH_INTERVAL_S + 15 * 60

# Importieren des Installers darf weder Ramdisk-Verzeichnisse noch Logdateien
# erzeugen. Die Laufzeit initialisiert beides erst im tatsächlichen Dienstprozess.
logger = logging.getLogger("EpexManager")
_runtime_initialized = False
_market_value_solar_loaded = False
update_market_value_solar_report = None


def _initialize_runtime():
    global logger, _runtime_initialized
    if _runtime_initialized:
        return
    os.makedirs(RAMDISK_DIR, exist_ok=True)
    log_dir = "/var/www/html/logs"
    if not os.path.exists(log_dir):
        log_dir = os.path.join(INSTALL_DIR, "logs")
    if not os.path.exists(log_dir):
        log_dir = RAMDISK_DIR
    logger = configure_service_logger(
        "EpexManager",
        log_path=os.path.join(log_dir, "epex_manager.log"),
        max_bytes=2 * 1024 * 1024,
        backup_count=3,
        quiet_interval_s=900.0,
    )
    _runtime_initialized = True


def _load_market_value_solar_report():
    global _market_value_solar_loaded, update_market_value_solar_report
    if _market_value_solar_loaded:
        return update_market_value_solar_report
    _market_value_solar_loaded = True
    try:
        from market_value_solar import update_market_value_solar_report as reporter

        update_market_value_solar_report = reporter
    except Exception as exc:
        logger.warning("Marktwert-Solar-Monitor nicht geladen: %s", exc)
        update_market_value_solar_report = None
    return update_market_value_solar_report

def update_market_value_solar_monitor(config, price_data):
    """Update the read-only Marktwert-Solar diagnostics file."""
    reporter = _load_market_value_solar_report()
    if reporter is None:
        return None
    try:
        report = reporter(
            config,
            price_data,
            MARKET_VALUE_SOLAR_FILE,
            MARKET_VALUE_SOLAR_CACHE_FILE,
            write_json_atomic=write_json_atomic,
            logger=logger,
        )
        if report and report.get("enabled"):
            logger.info(
                "Marktwert Solar: Status %s, Wert %s ct/kWh, %s/%s Slots.",
                report.get("status"),
                report.get("solar_weighted_market_value_ct"),
                (report.get("slots") or {}).get("matched"),
                (report.get("slots") or {}).get("solar"),
            )
        return report
    except Exception as exc:
        logger.warning("Marktwert-Solar-Monitor konnte nicht aktualisiert werden: %s", exc)
    return None

def parse_special_tariff_schedule(raw):
    """Kompatibilitätswrapper für die neutrale Tarifauflösung."""
    return _parse_special_tariff_schedule(raw)

def special_tariff_price_for_dt(raw, dt, default_price):
    """Kompatibilitätswrapper für die neutrale Tarifauflösung."""
    return special_tariff_price_for_datetime(raw, dt, default_price)

def write_json_atomic(path, payload, indent=None):
    temp_file = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=indent)
    os.replace(temp_file, path)


MARKET_CONSUMERS = ("battery", "wallbox", "heatpump", "heater")


def _path_is_within(path, base_dir):
    """Liefert nur dann True, wenn der Pfad unterhalb des erwarteten Laufzeitverzeichnisses liegt."""
    try:
        path_real = os.path.realpath(os.path.abspath(path))
        base_real = os.path.realpath(os.path.abspath(base_dir))
        return os.path.commonpath((path_real, base_real)) == base_real
    except (OSError, TypeError, ValueError):
        return False


def market_safety_context(config):
    """Enges Fail-closed-Gate für Preisartefakte ohne Installationsresolver."""
    if not isinstance(config, dict) or not config:
        return False, "config_unavailable"
    if not os.path.isabs(RAMDISK_DIR) or not os.path.isabs(DATA_DIR):
        return False, "runtime_path_not_absolute"
    if not os.path.isdir(RAMDISK_DIR) or not os.path.isdir(DATA_DIR):
        return False, "runtime_directory_missing"
    if not os.path.isfile(V4_CONFIG_FILE):
        return False, "config_file_missing"
    try:
        config_info = os.lstat(V4_CONFIG_FILE)
    except OSError:
        return False, "config_file_unreadable"
    if not stat.S_ISREG(config_info.st_mode) or config_info.st_nlink != 1:
        return False, "config_file_not_regular"

    for path in (EPEX_OUTPUT_FILE, ECO_SCORE_FILE, PRICE_BOOST_PLAN_FILE, MARKET_VALUE_SOLAR_FILE):
        if not _path_is_within(path, RAMDISK_DIR):
            return False, "ramdisk_path_outside_context"
    for path in (V4_CONFIG_FILE, EPEX_CACHE_FILE, MARKET_VALUE_SOLAR_CACHE_FILE):
        if not _path_is_within(path, DATA_DIR):
            return False, "data_path_outside_context"
    if not os.access(RAMDISK_DIR, os.W_OK) or not os.access(DATA_DIR, os.W_OK):
        return False, "runtime_directory_not_writable"
    return True, ""


def _evaluate_market_safety_gate(config, safety_gate=None):
    gate = safety_gate or market_safety_context
    try:
        result = gate(config)
    except Exception:
        return False, "safety_gate_error"
    if isinstance(result, tuple) and len(result) >= 2:
        return bool(result[0]), str(result[1] or "")
    if result is True:
        return True, ""
    return False, "safety_context_invalid"


def disabled_market_plan(config, reason, now_ms=None):
    """Erstellt bei ungültigen Eingangsdaten den einzig zulässigen Marktvertrag."""
    supported = supports_spot_market_prices(config)
    generated_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return {
        "ts": generated_ms,
        "valid_until_ts_ms": generated_ms,
        "enabled": False,
        "supported": supported,
        "unsupported_reason": "" if supported else "Negativpreis-Boost ist nur für echte Börsenpreistarife verfügbar",
        "active": False,
        "context_valid": False,
        "release_valid": False,
        "disabled_reason": str(reason or "market_input_invalid"),
        "price_limit_ct": None,
        "min_duration_min": None,
        "active_window": None,
        "windows": [],
        "allow": {consumer: False for consumer in MARKET_CONSUMERS},
        "consumer_releases": {consumer: False for consumer in MARKET_CONSUMERS},
        "limits": {},
    }


def publish_disabled_market_state(config, reason):
    """Ersetzt alle verbraucherseitigen Marktartefakte atomar durch einen gesperrten Zustand."""
    if (
        not os.path.isabs(RAMDISK_DIR)
        or not os.path.isdir(RAMDISK_DIR)
        or not os.access(RAMDISK_DIR, os.W_OK)
        or any(not _path_is_within(path, RAMDISK_DIR) for path in (
            EPEX_OUTPUT_FILE,
            ECO_SCORE_FILE,
            PRICE_BOOST_PLAN_FILE,
        ))
    ):
        raise OSError("consumer-facing market paths are not safe for closure")
    plan = disabled_market_plan(config, reason)
    # Der Plan wird zuerst gesperrt, damit ein Teilschreibvorgang nie eine alte Freigabe behält.
    write_json_atomic(PRICE_BOOST_PLAN_FILE, plan, indent=2)
    write_json_atomic(ECO_SCORE_FILE, [], indent=2)
    write_json_atomic(EPEX_OUTPUT_FILE, [], indent=2)
    update_market_value_solar_monitor(config or {}, [])
    return plan


def publish_market_state(config, data, safety_gate=None, cache_write=True, source="provider"):
    """Prüft und veröffentlicht einen vollständigen Marktstatus und erhält den gültigen Normalpfad."""
    context_valid, context_reason = _evaluate_market_safety_gate(config, safety_gate)
    if not context_valid:
        return False, [], publish_disabled_market_state(config, context_reason), context_reason
    if not price_data_has_future_slots(data, min_horizon_s=900):
        if configured_tariff_type(config) == "octopus_heat":
            return publish_configured_tariff_state(config, safety_gate=safety_gate)
        reason = "price_data_missing_or_stale"
        return False, [], publish_disabled_market_state(config, reason), reason

    scores = generate_eco_score(data, config)
    if not isinstance(scores, list) or not scores:
        reason = "eco_score_empty"
        return False, [], publish_disabled_market_state(config, reason), reason

    boost_plan = generate_price_boost_plan(data, scores, config)
    if not isinstance(boost_plan, dict):
        reason = "price_plan_invalid"
        return False, [], publish_disabled_market_state(config, reason), reason

    # Schließt eine ältere Freigabe, bevor die zugehörigen Datendateien ersetzt
    # werden. Der endgültige Plan wird bewusst zuletzt veröffentlicht.
    write_json_atomic(PRICE_BOOST_PLAN_FILE, disabled_market_plan(config, "publishing"), indent=2)
    try:
        write_json_atomic(EPEX_OUTPUT_FILE, data)
        write_json_atomic(ECO_SCORE_FILE, scores, indent=2)
        if cache_write:
            persist_price_cache(data)
        boost_plan = dict(boost_plan)
        boost_plan["context_valid"] = True
        boost_plan["release_valid"] = True
        boost_plan["source_status"] = str(source or "provider")
        boost_plan["consumer_releases"] = {
            consumer: bool(boost_plan.get("enabled") and boost_plan.get("active")
                           and (boost_plan.get("allow") or {}).get(consumer, False))
            for consumer in MARKET_CONSUMERS
        }
        write_json_atomic(PRICE_BOOST_PLAN_FILE, boost_plan, indent=2)
        update_market_value_solar_monitor(config, data)
    except Exception:
        publish_disabled_market_state(config, "market_publish_error")
        raise
    return True, scores, boost_plan, ""


def publish_configured_tariff_state(config, safety_gate=None):
    """Veröffentlicht die lokale Octopus-Heat-Tarifachse ohne Börsenpreis."""
    context_valid, context_reason = _evaluate_market_safety_gate(config, safety_gate)
    if not context_valid:
        return False, [], publish_disabled_market_state(config, context_reason), context_reason
    if configured_tariff_type(config) != "octopus_heat":
        reason = "configured_tariff_axis_unavailable"
        return False, [], publish_disabled_market_state(config, reason), reason

    now_ms = int(time.time() * 1000)
    tariff_slots = recurring_tariff_slots(config, now_ms=now_ms)
    scores = generate_configured_tariff_score(tariff_slots)
    if not scores:
        reason = "configured_tariff_axis_empty"
        return False, [], publish_disabled_market_state(config, reason), reason
    boost_plan = generate_price_boost_plan([], scores, config)

    write_json_atomic(PRICE_BOOST_PLAN_FILE, disabled_market_plan(config, "publishing"), indent=2)
    try:
        # Ohne externe Quelle existiert bewusst kein Marktpreisvertrag.
        write_json_atomic(EPEX_OUTPUT_FILE, [], indent=2)
        write_json_atomic(ECO_SCORE_FILE, scores, indent=2)
        boost_plan = dict(boost_plan)
        boost_plan["context_valid"] = True
        boost_plan["release_valid"] = True
        boost_plan["source_status"] = "configured_tariff"
        boost_plan["consumer_releases"] = {
            consumer: bool(
                boost_plan.get("enabled")
                and boost_plan.get("active")
                and (boost_plan.get("allow") or {}).get(consumer, False)
            )
            for consumer in MARKET_CONSUMERS
        }
        write_json_atomic(PRICE_BOOST_PLAN_FILE, boost_plan, indent=2)
        update_market_value_solar_monitor(config, [])
    except Exception:
        publish_disabled_market_state(config, "configured_tariff_publish_error")
        raise
    return True, scores, boost_plan, ""

def price_data_has_future_slots(data, min_horizon_s=3600):
    if not isinstance(data, list):
        return False
    now_ms = time.time() * 1000
    min_end = now_ms + (float(min_horizon_s) * 1000)
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            end_ts = float(entry.get("end_timestamp", 0) or 0)
        except Exception:
            continue
        if end_ts >= min_end:
            return True
    return False

def price_data_latest_end_ts(data):
    latest = 0.0
    if not isinstance(data, list):
        return latest
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            latest = max(latest, float(entry.get("end_timestamp", 0) or 0))
        except Exception:
            continue
    return latest

def price_data_window_text(data):
    latest = price_data_latest_end_ts(data)
    if latest <= 0.0:
        return "kein gueltiges Zeitfenster"
    try:
        from datetime import datetime
        return "letzter Slot endet %s" % datetime.fromtimestamp(latest / 1000.0).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "letzter Slot endet %.0f" % latest

def price_output_is_fresh(max_age_s=3300):
    try:
        return os.path.exists(EPEX_OUTPUT_FILE) and (time.time() - os.path.getmtime(EPEX_OUTPUT_FILE)) <= max_age_s
    except Exception:
        return False

def persist_price_cache(data):
    if not price_data_has_future_slots(data, min_horizon_s=900):
        return
    try:
        write_json_atomic(EPEX_CACHE_FILE, data)
    except Exception as e:
        logger.warning(f"EPEX-Cache konnte nicht geschrieben werden: {e}")

def restore_cached_prices(config, safety_gate=None):
    context_valid, context_reason = _evaluate_market_safety_gate(config, safety_gate)
    if not context_valid:
        publish_disabled_market_state(config, context_reason)
        return False
    if price_output_is_fresh():
        return False
    if not os.path.exists(EPEX_CACHE_FILE):
        return False
    try:
        with open(EPEX_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"EPEX-Cache konnte nicht gelesen werden: {e}")
        return False
    if not price_data_has_future_slots(data):
        logger.warning("EPEX-Cache ist zu alt und wird nicht wiederhergestellt.")
        return False

    published, _scores, _boost_plan, reason = publish_market_state(
        config,
        data,
        safety_gate=safety_gate,
        cache_write=False,
        source="cache",
    )
    if not published:
        logger.warning("EPEX-Cache wurde aus Sicherheitsgruenden verworfen: %s", reason)
        return False
    logger.warning(
        "Nutze letzten gueltigen EPEX-Cache (%d Eintraege), weil Live-Abruf fehlgeschlagen ist.",
        len(data),
    )
    return True


def recover_or_disable_market(config, safety_gate=None):
    """Nutzt einen gültigen Cache oder schließt alle veralteten Marktfreigaben explizit."""
    if safety_gate is None:
        if restore_cached_prices(config):
            return True
    elif restore_cached_prices(config, safety_gate=safety_gate):
        return True
    if configured_tariff_type(config) == "octopus_heat":
        published, _scores, _plan, _reason = publish_configured_tariff_state(
            config,
            safety_gate=safety_gate,
        )
        if published:
            return True
    context_valid, context_reason = _evaluate_market_safety_gate(config, safety_gate)
    reason = "provider_and_cache_unavailable" if context_valid else context_reason
    publish_disabled_market_state(config, reason)
    return False

def load_v4_config():
    config = {}
    if os.path.exists(V4_CONFIG_FILE):
        try:
            with open(V4_CONFIG_FILE, "r") as f:
                config = json.load(f)
        except Exception as e:
            logger.error(f"Fehler beim Lesen der V4 Config: {e}")

    # Legacy Fallback für Benutzer, die das V4 UI noch nicht gespeichert haben
    legacy_stromtarif_typ = None
    if os.path.exists(os.path.join(INSTALL_DIR, "e3dc.config.txt")):
        try:
            with open(os.path.join(INSTALL_DIR, "e3dc.config.txt"), "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("awattar") and "=" in line:
                        val = line.split("=")[1].strip().lower()
                        if val in ["1", "2", "true"]:
                            legacy_stromtarif_typ = "tibber"
        except Exception as e:
            logger.warning(f"Feature Fallback: e3dc.config.txt konnte nicht gelesen werden: {e}")

    if legacy_stromtarif_typ and "stromtarif_typ" not in config:
        config["stromtarif_typ"] = legacy_stromtarif_typ

    return config


def _forecast_evidence_enabled_for_install(path=V4_CONFIG_FILE):
    """Liest nur den optionalen Service-Schalter über einen nofollow-Vertrag."""

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or not 2 <= before.st_size <= 4 * 1024 * 1024:
                raise ValueError("config_file_invalid")
            chunks = []
            remaining = before.st_size
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if (
                remaining != 0
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise ValueError("config_file_changed")
        finally:
            os.close(descriptor)
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("config_root_invalid")
    except Exception as exc:
        logger.warning(
            "PV-Prognosediagnose-Schalter nicht sicher lesbar; Dienst bleibt aus: %s",
            exc,
        )
        return False
    return str(payload.get("forecast_diagnostics_enable", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "ein",
    }


def fetch_awattar_data():
    """Fetch data from Awattar (DE) Public API"""
    logger.info("Frage aWATTar API nach aktuellen Spot-Preisen...")
    now_ms = int(time.time() * 1000)
    start_time = now_ms - (3600 * 1000 * 24) # -24h
    end_time = now_ms + (3600 * 1000 * 36)   # +36h
    url = f"https://api.awattar.de/v1/marketdata?start={start_time}&end={end_time}"
    req = urllib.request.Request(url, headers={'User-Agent': 'E3DC-Control-V4/1.0'})
    with urllib.request.urlopen(req, timeout=10) as response:
        content = response.read().decode('utf-8')
        data = json.loads(content)
        if "data" in data and isinstance(data["data"], list):
            normalized = []
            for entry in data["data"]:
                if not isinstance(entry, dict):
                    continue
                item = dict(entry)
                try:
                    duration_min = int(round((float(item.get("end_timestamp", 0)) - float(item.get("start_timestamp", 0))) / 60000.0))
                except Exception:
                    duration_min = 60
                item.setdefault("price_source", "awattar")
                item.setdefault("source_resolution_min", duration_min if duration_min > 0 else 60)
                item.setdefault("price_resolution_min", duration_min if duration_min > 0 else 60)
                normalized.append(item)
            return normalized
    return None

def fetch_awattar_with_log(reason=""):
    try:
        data = fetch_awattar_data()
        if data:
            suffix = f" ({reason})" if reason else ""
            logger.info(f"aWATTar-Fallback geladen ({len(data)} Eintraege){suffix}.")
            return data
    except Exception as e:
        logger.warning(f"aWATTar Fehler: {e}.")
    return None

def _parse_tibber_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None

def _tibber_price_to_slot(entry):
    if not isinstance(entry, dict):
        return None
    starts_at = _parse_tibber_time(entry.get("startsAt"))
    if starts_at is None:
        return None
    try:
        total = float(entry.get("total"))
    except (TypeError, ValueError):
        return None
    start_ms = int(starts_at.timestamp() * 1000)
    billing_price_ct = total * 100.0
    return {
        "start_timestamp": start_ms,
        "end_timestamp": start_ms,
        "marketprice": total * 1000.0,
        "billing_price_ct": billing_price_ct,
        "price_source": "tibber",
        "tariff_provider": "tibber",
        "currency": str(entry.get("currency") or "EUR"),
        "level": entry.get("level"),
        "unit": "Eur/MWh",
    }

def _finalize_tibber_slots(slots):
    clean = {}
    for slot in slots or []:
        if isinstance(slot, dict):
            clean[int(slot["start_timestamp"])] = slot
    ordered = [clean[key] for key in sorted(clean)]
    deltas = [
        ordered[idx + 1]["start_timestamp"] - ordered[idx]["start_timestamp"]
        for idx in range(len(ordered) - 1)
        if ordered[idx + 1]["start_timestamp"] > ordered[idx]["start_timestamp"]
    ]
    positive_deltas = [delta for delta in deltas if 0 < delta <= 2 * 3600 * 1000]
    default_delta = min(positive_deltas) if positive_deltas else 15 * 60 * 1000
    resolution_min = int(round(default_delta / 60000.0))
    for idx, slot in enumerate(ordered):
        if idx + 1 < len(ordered):
            delta = ordered[idx + 1]["start_timestamp"] - slot["start_timestamp"]
            if delta <= 0 or delta > 2 * 3600 * 1000:
                delta = default_delta
        else:
            delta = default_delta
        slot["end_timestamp"] = slot["start_timestamp"] + delta
        slot["price_resolution_min"] = int(round(delta / 60000.0))
        slot["source_resolution_min"] = resolution_min
    return ordered

def fetch_tibber_data(config):
    token = str((config or {}).get("tibber_api_token") or "").strip()
    if not token:
        raise ValueError("kein Tibber API-Token konfiguriert")
    home_id = str((config or {}).get("tibber_home_id") or "").strip()
    query = """
    query E3dcTibberPrices {
      viewer {
        homes {
          id
          currentSubscription {
            priceInfo(resolution: QUARTER_HOURLY) {
              current { total energy tax startsAt level currency }
              today { total energy tax startsAt level currency }
              tomorrow { total energy tax startsAt level currency }
            }
          }
        }
      }
    }
    """
    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        TIBBER_API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "E3DC-Control-V4/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("errors"):
        raise RuntimeError("Tibber API Fehler: %s" % payload.get("errors"))
    homes = (((payload.get("data") or {}).get("viewer") or {}).get("homes") or [])
    selected = None
    for home in homes:
        if not isinstance(home, dict):
            continue
        if home_id and str(home.get("id") or "") != home_id:
            continue
        subscription = home.get("currentSubscription") or {}
        if subscription.get("priceInfo"):
            selected = home
            break
    if selected is None:
        raise ValueError("kein Tibber Home mit Preisinfo gefunden")
    price_info = ((selected.get("currentSubscription") or {}).get("priceInfo") or {})
    raw_prices = []
    for key in ("today", "tomorrow"):
        values = price_info.get(key) or []
        if isinstance(values, list):
            raw_prices.extend(values)
    if not raw_prices and isinstance(price_info.get("current"), dict):
        raw_prices.append(price_info["current"])
    slots = [_tibber_price_to_slot(entry) for entry in raw_prices]
    return _finalize_tibber_slots([slot for slot in slots if slot])

def fetch_tibber_with_log(config, reason=""):
    try:
        data = fetch_tibber_data(config)
        if data:
            suffix = f" ({reason})" if reason else ""
            logger.info(f"Tibber-Preise geladen ({len(data)} Eintraege){suffix}.")
            return data
    except Exception as e:
        logger.warning(f"Tibber Fehler: {e}.")
    return None

def _entsoe_xml_name(tag):
    return str(tag).rsplit("}", 1)[-1]

def _entsoe_child_text(node, name):
    for child in list(node):
        if _entsoe_xml_name(child.tag) == name:
            return (child.text or "").strip()
    return None

def _entsoe_desc_text(node, name):
    for child in node.iter():
        if _entsoe_xml_name(child.tag) == name:
            return (child.text or "").strip()
    return None

def _entsoe_parse_time(value):
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(timezone.utc)

def _entsoe_parse_resolution(value):
    text = str(value or "").strip().upper()
    if text in ("PT60M", "PT1H"):
        return timedelta(hours=1)
    if text == "PT30M":
        return timedelta(minutes=30)
    if text == "PT15M":
        return timedelta(minutes=15)
    match = re.match(r"^PT(\d+(?:\.\d+)?)([HM])$", text)
    if not match:
        return None
    amount = float(match.group(1))
    return timedelta(hours=amount) if match.group(2) == "H" else timedelta(minutes=amount)

def _entsoe_point_price(point):
    value = _entsoe_child_text(point, "price.amount")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _entsoe_error_reasons(root):
    reasons = []
    for reason in root.iter():
        if _entsoe_xml_name(reason.tag) != "Reason":
            continue
        code = _entsoe_desc_text(reason, "code")
        text = _entsoe_desc_text(reason, "text")
        if code or text:
            reasons.append(f"{code or ''} {text or ''}".strip())
    return "; ".join(reasons)

def _parse_entsoe_day_ahead_xml(raw):
    root = ET.fromstring(raw)
    reasons = _entsoe_error_reasons(root)
    if reasons and not any(_entsoe_xml_name(node.tag) == "TimeSeries" for node in root.iter()):
        raise RuntimeError(reasons)

    merged = {}
    for time_series in root.iter():
        if _entsoe_xml_name(time_series.tag) != "TimeSeries":
            continue
        currency = _entsoe_desc_text(time_series, "currency_Unit.name") or "EUR"
        unit = _entsoe_desc_text(time_series, "price_Measure_Unit.name") or "MWH"
        curve_type = (_entsoe_desc_text(time_series, "curveType") or "").strip().upper()
        for period in time_series.iter():
            if _entsoe_xml_name(period.tag) != "Period":
                continue
            interval = None
            for child in list(period):
                if _entsoe_xml_name(child.tag) == "timeInterval":
                    interval = child
                    break
            if interval is None:
                continue
            try:
                period_start = _entsoe_parse_time(_entsoe_child_text(interval, "start"))
                period_end = _entsoe_parse_time(_entsoe_child_text(interval, "end"))
            except Exception:
                continue
            resolution = _entsoe_parse_resolution(_entsoe_child_text(period, "resolution"))
            if resolution is None or resolution.total_seconds() <= 0:
                continue
            resolution_ms = int(round(resolution.total_seconds() * 1000))
            resolution_min = int(round(resolution.total_seconds() / 60.0))
            point_values = []
            for point in list(period):
                if _entsoe_xml_name(point.tag) != "Point":
                    continue
                try:
                    position = int(_entsoe_child_text(point, "position") or "0")
                except (TypeError, ValueError):
                    continue
                price = _entsoe_point_price(point)
                if position <= 0 or price is None:
                    continue
                point_values.append((position, price))

            point_values.sort(key=lambda item: item[0])
            period_slots = int(
                max(0.0, (period_end - period_start).total_seconds())
                // resolution.total_seconds()
            )
            for index, (position, price) in enumerate(point_values):
                # A03 ist eine ENTSO-E-Blockkurve: Ein Punkt gilt bis zum
                # nächsten Positionspunkt. Ohne diese Expansion entstehen an
                # ausgelassenen Positionen Inseln aus der älteren TimeSeries.
                if curve_type == "A03":
                    next_position = (
                        point_values[index + 1][0]
                        if index + 1 < len(point_values)
                        else period_slots + 1
                    )
                    end_position = min(period_slots + 1, max(position + 1, next_position))
                else:
                    end_position = position + 1
                for expanded_position in range(position, end_position):
                    start = period_start + ((expanded_position - 1) * resolution)
                    start_ms = int(start.timestamp() * 1000)
                    # ENTSO-E kann überlappende TimeSeries liefern; die spätere
                    # Revision ersetzt den vollständigen Block positionsgenau.
                    merged[start_ms] = {
                        "start_timestamp": start_ms,
                        "end_timestamp": start_ms + resolution_ms,
                        "marketprice": price,
                        "price_source": "entsoe",
                        "tariff_provider": "entsoe",
                        "currency": currency,
                        "unit": "Eur/MWh" if unit.upper() == "MWH" else unit,
                        "source_resolution_min": resolution_min,
                        "price_resolution_min": resolution_min,
                    }

    return [merged[key] for key in sorted(merged)]

def fetch_entsoe_day_ahead_prices(config):
    token = str((config or {}).get("entsoe_api_token") or "").strip()
    if not token:
        raise ValueError("kein ENTSO-E API-Token konfiguriert")

    now_utc = datetime.now(timezone.utc)
    period_start = (now_utc - timedelta(hours=24)).replace(minute=0, second=0, microsecond=0)
    period_end = (now_utc + timedelta(hours=48)).replace(minute=0, second=0, microsecond=0)
    params = {
        "securityToken": token,
        "documentType": "A44",
        "in_Domain": ENTSOE_DE_LU_DOMAIN,
        "out_Domain": ENTSOE_DE_LU_DOMAIN,
        "periodStart": period_start.strftime("%Y%m%d%H%M"),
        "periodEnd": period_end.strftime("%Y%m%d%H%M"),
    }

    last_error = None
    for endpoint in ENTSOE_API_ENDPOINTS:
        url = endpoint + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "E3DC-Control-V4/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                raw = response.read()
            data = _parse_entsoe_day_ahead_xml(raw)
            if data:
                return data
            last_error = ValueError("ENTSO-E lieferte keine Preisslots")
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    return None

def fetch_entsoe_with_log(config, reason=""):
    try:
        data = fetch_entsoe_day_ahead_prices(config)
        if data:
            suffix = f" ({reason})" if reason else ""
            logger.info(f"ENTSO-E-Preise geladen ({len(data)} Eintraege){suffix}.")
            return data
    except Exception as e:
        logger.warning(f"ENTSO-E Fehler: {e}.")
    return None

def _safe_bool(val, default=False):
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    raw = str(val).strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on", "ja", "ein")

def _direct_marketing_wants_market_overlay(config):
    if not _safe_bool((config or {}).get("direct_marketing_enable"), False):
        return False
    basis = str((config or {}).get("direct_marketing_settlement_basis", "day_ahead_15min")).strip().lower()
    return basis in ("day_ahead_15min", "day-ahead-15min", "day_ahead", "day-ahead", "market")

def _slot_resolution_min(slot, default=15):
    for key in ("price_resolution_min", "source_resolution_min"):
        raw = (slot or {}).get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return int(round(float(str(raw).replace(",", "."))))
        except Exception:
            continue
    return int(default)

def _fetch_direct_marketing_market_data(config, entsoe_data=None, smard_data=None):
    if not _direct_marketing_wants_market_overlay(config):
        return None

    candidates = []
    if entsoe_data:
        candidates.append(("ENTSO-E", entsoe_data))
    else:
        data = fetch_entsoe_with_log(config, "Direktvermarktung-Marktpreis")
        if data:
            candidates.append(("ENTSO-E", data))

    if smard_data:
        candidates.append(("SMARD", smard_data))
    else:
        data = fetch_smard_combined_data()
        if data:
            candidates.append(("SMARD", data))

    merged = {}
    source_counts = {}
    for label, data in candidates:
        if not price_data_has_future_slots(data, min_horizon_s=900):
            logger.warning(
                "%s-DV-Marktpreise ohne ausreichenden Zukunftshorizont (%s).",
                label,
                price_data_window_text(data),
            )
            continue
        valid = [
            slot for slot in data
            if isinstance(slot, dict)
            and _slot_resolution_min(slot, 15) <= 15
            and slot.get("marketprice") is not None
        ]
        if valid:
            added = 0
            for slot in sorted(valid, key=lambda item: int(item.get("start_timestamp", 0) or 0)):
                try:
                    start = int(slot.get("start_timestamp", 0) or 0)
                    end = int(slot.get("end_timestamp", 0) or 0)
                except Exception:
                    continue
                if start <= 0 or end <= start or start in merged:
                    continue
                merged[start] = slot
                added += 1
            source_counts[label] = added
        else:
            logger.warning("%s-DV-Marktpreise haben keine nutzbaren 15-Minuten-Slots.", label)

    if merged:
        source_text = ", ".join(
            "%s=%d" % (label, source_counts.get(label, 0))
            for label, _data in candidates
            if source_counts.get(label, 0) > 0
        )
        logger.info(
            "Direktvermarktung nutzt ein lückenfüllendes 15-Minuten-Marktpreis-Overlay (%s, gesamt=%d).",
            source_text,
            len(merged),
        )
        return [merged[start] for start in sorted(merged)]

    return None

def apply_direct_marketing_market_overlay(price_data, market_data, config=None):
    """Add DV market-price fields without changing the user's tariff price data."""
    if not price_data or not market_data or not _direct_marketing_wants_market_overlay(config or {}):
        return price_data

    market_slots = []
    for raw in market_data:
        if not isinstance(raw, dict):
            continue
        try:
            start = int(raw.get("start_timestamp", 0))
            end = int(raw.get("end_timestamp", 0))
            price = float(raw.get("marketprice"))
        except Exception:
            continue
        if start <= 0 or end <= start:
            continue
        resolution = _slot_resolution_min(raw, max(1, int(round((end - start) / 60000.0))))
        market_slots.append({
            "start_timestamp": start,
            "end_timestamp": end,
            "marketprice": price,
            "price_source": raw.get("price_source") or raw.get("tariff_provider") or "market",
            "price_resolution_min": resolution,
            "source_resolution_min": raw.get("source_resolution_min", resolution),
        })
    market_slots.sort(key=lambda item: item["start_timestamp"])
    if not market_slots:
        return price_data

    # Das Providerformat enthält nicht zwingend eine externe Revisions-ID.
    # Für die DV-Verschiebungsentscheidung binden wir deshalb keine erfundene
    # Providerrevision, sondern einen lokalen Inhaltsnachweis über exakt die
    # Rohslots, die dieses Overlay verwendet. Jede Preis-, Quellen-, Raster-
    # oder Horizontänderung erzeugt eine andere Revision.
    revision_material = [
        {
            "start_timestamp": int(item["start_timestamp"]),
            "end_timestamp": int(item["end_timestamp"]),
            "marketprice": round(float(item["marketprice"]), 6),
            "price_source": str(item.get("price_source") or ""),
            "price_resolution_min": int(item.get("price_resolution_min") or 0),
            "source_resolution_min": int(item.get("source_resolution_min") or 0),
        }
        for item in market_slots
    ]
    market_revision = "sha256:" + hashlib.sha256(
        json.dumps(
            revision_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    matched = 0
    min_ct = None
    max_ct = None
    market_idx = 0
    for slot in price_data:
        if not isinstance(slot, dict):
            continue
        try:
            start = int(slot.get("start_timestamp", 0))
        except Exception:
            continue
        while market_idx + 1 < len(market_slots) and market_slots[market_idx]["end_timestamp"] <= start:
            market_idx += 1
        market = market_slots[market_idx]
        if not (market["start_timestamp"] <= start < market["end_timestamp"]):
            continue

        market_ct = market["marketprice"] / 10.0
        slot["direct_marketing_marketprice"] = market["marketprice"]
        slot["direct_marketing_market_price_ct"] = round(market_ct, 5)
        slot["direct_marketing_price_source"] = market["price_source"]
        slot["direct_marketing_price_resolution_min"] = market["price_resolution_min"]
        slot["direct_marketing_source_resolution_min"] = market["source_resolution_min"]
        slot["direct_marketing_price_revision"] = market_revision
        slot["direct_marketing_price_revision_source"] = (
            "local_market_overlay_content_v1"
        )
        slot["direct_marketing_price_available"] = True
        matched += 1
        min_ct = market_ct if min_ct is None else min(min_ct, market_ct)
        max_ct = market_ct if max_ct is None else max(max_ct, market_ct)

    if matched:
        logger.info(
            "Direktvermarktung-Marktpreis-Overlay gesetzt: %d/%d Slots, %.3f bis %.3f ct/kWh.",
            matched,
            len(price_data),
            min_ct,
            max_ct,
        )
    else:
        logger.warning("Direktvermarktung-Marktpreis-Overlay fand keine passenden Zeitfenster.")
    return price_data

def _fetch_smard_series(resolution):
    """Interne Hilfsfunktion: Laedt SMARD Zeitreihe fuer 'hour' oder 'quarterhour'."""
    index_url = f"https://www.smard.de/app/chart_data/4169/DE/index_{resolution}.json"
    req = urllib.request.Request(index_url, headers={'User-Agent': 'E3DC-Control-V4/1.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as response:
        index_data = json.loads(response.read().decode('utf-8'))
    timestamps = index_data.get("timestamps", [])
    if not timestamps:
        raise ValueError(f"SMARD lieferte keine Timestamps fuer {resolution}!")
    series = []
    for ts in timestamps[-2:]:
        data_url = f"https://www.smard.de/app/chart_data/4169/DE/4169_DE_{resolution}_{ts}.json"
        req_data = urllib.request.Request(data_url, headers={'User-Agent': 'E3DC-Control-V4/1.0', 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req_data, timeout=10) as response:
                raw_series = json.loads(response.read().decode('utf-8'))
                series.extend(raw_series.get("series", []))
        except Exception as e:
            logger.warning(f"Fehler beim Abrufen der SMARD {resolution} Woche {ts}: {e}")
    return series

def fetch_smard_quarterhour_data():
    """Laedt 15-Minuten EPEX Intraday Preise von SMARD (primaerer Endpunkt)."""
    logger.info("Frage SMARD nach 15-Minuten Intraday-Preisen (quarterhour)...")
    series = _fetch_smard_series("quarterhour")
    current_time_ms = time.time() * 1000 - (24 * 3600 * 1000)
    standardized_data = []
    for entry in series:
        if len(entry) >= 2 and entry[1] is not None:
            ts = entry[0]
            if ts >= current_time_ms:
                standardized_data.append({
                    "start_timestamp": ts,
                    "end_timestamp": ts + (900 * 1000),  # 15 Minuten in ms
                    "marketprice": entry[1]  # EUR/MWh
                })
    return standardized_data if standardized_data else None

def fetch_smard_data():
    """Laedt stundenweise EPEX Day-Ahead Preise von SMARD (Fallback)."""
    logger.info("Frage SMARD nach stundenweisen Spot-Preisen (hour)...")
    series = _fetch_smard_series("hour")
    current_time_ms = time.time() * 1000 - (24 * 3600 * 1000)
    standardized_data = []
    for entry in series:
        if len(entry) >= 2 and entry[1] is not None:
            ts = entry[0]
            if ts >= current_time_ms:
                standardized_data.append({
                    "start_timestamp": ts,
                    "end_timestamp": ts + (3600 * 1000),  # 1 Stunde in ms
                    "marketprice": entry[1]  # EUR/MWh
                })
    return standardized_data if standardized_data else None

def fetch_smard_combined_data():
    qh_data = None
    hour_data = None
    try:
        qh_data = fetch_smard_quarterhour_data()
        if qh_data:
            logger.info(f"15-Min SMARD Intraday geladen ({len(qh_data)} Eintraege).")
    except Exception as e:
        logger.warning(f"SMARD quarterhour Fehler: {e}.")

    try:
        hour_data = fetch_smard_data()
        if hour_data:
            logger.info(f"Stundenweise SMARD Day-Ahead geladen ({len(hour_data)} Eintraege).")
    except Exception as e:
        logger.warning(f"SMARD hour Fehler: {e}.")

    if not (qh_data or hour_data):
        return None

    merged = {}
    for entry in (hour_data or []):
        st = entry["start_timestamp"]
        price = entry["marketprice"]
        for minute_offset in [0, 15, 30, 45]:
            slot_start = st + (minute_offset * 60 * 1000)
            merged[slot_start] = {
                "start_timestamp": slot_start,
                "end_timestamp": slot_start + (15 * 60 * 1000),
                "marketprice": price,
                "price_source": "smard_hour",
                "source_resolution_min": 60,
                "price_resolution_min": 15,
            }
    for entry in (qh_data or []):
        item = dict(entry)
        item.setdefault("price_source", "smard_quarterhour")
        item.setdefault("source_resolution_min", 15)
        item.setdefault("price_resolution_min", 15)
        merged[item["start_timestamp"]] = item

    data = sorted(merged.values(), key=lambda x: x["start_timestamp"])
    logger.info(f"Kombinierte Preisdaten: {len(data)} Eintraege (15-Min + Day-Ahead).")
    return data

def generate_eco_score(price_data, config):
    """Generates market-based Eco-Score data plus the user's billing price."""
    if not price_data:
        return {}

    now_ms = time.time() * 1000

    # Extract prices for the next 48 hours to fully cover day-ahead auctions
    future_prices = []
    for d in price_data:
        if d["start_timestamp"] >= (now_ms - 3600*1000) and d["start_timestamp"] < (now_ms + 48*3600*1000):
            future_prices.append(d)

    if not future_prices:
        return {}

    prices = [p["marketprice"] for p in future_prices]
    min_p = min(prices)
    max_p = max(prices)
    span = max_p - min_p
    if span == 0: span = 1

    def safe_float(val, default_val):
        try:
            if val is None or str(val).strip() == "":
                return default_val
            if isinstance(val, str):
                val = val.replace(',', '.')
            return float(val)
        except (ValueError, TypeError):
            return default_val

    stromtarif_typ = str(config.get("stromtarif_typ", "static")).strip().lower()
    strompreis_basis = safe_float(config.get("strompreis_basis", 25.0), 25.0)
    grid_friendly_mode = str(config.get("grid_friendly_mode", "1")).lower() in ["1", "true"]

    eco_scores = []

    for d in future_prices:
        ts = int(d["start_timestamp"] / 1000)

        # 1. Pure Eco Score (Wann ist der Netz-Strom dreckig / sauber?)
        pure_eco_score = 100.0 - (((d["marketprice"] - min_p) / span) * 100.0)
        if d["marketprice"] < 0: pure_eco_score = 100.0

        # 2. Billing Price (Was zahlt der Nutzer?)
        direct_billing_raw = d.get("billing_price_ct")
        if direct_billing_raw is not None and str(direct_billing_raw).strip() != "":
            billing_price = safe_float(direct_billing_raw, d["marketprice"] / 10.0)
        elif stromtarif_typ in ("tibber", "awattar", "dynamic", "epex"):
            # Grobe Schätzung: (Spot-Preis in EUR/MWh / 10) + Steuern + Netz für ct/kWh
            # Hier reichen uns relative Verhältnisse für den Score. Im V4 UI werden wir
            # demnächst die exakten Steueranteile definieren. Für jetzt:
            taxes = safe_float(config.get("awnebenkosten", 15.915), 15.915)
            vat = safe_float(config.get("awmwst", 19), 19.0) / 100.0
            billing_price = (d["marketprice"] / 10.0) * (1.0 + vat) + taxes

        else:
            # Vollständig konfigurierte Tarife verwenden dieselbe neutrale
            # Tagesachse wie Wallbox- und Heat-Planung.
            configured_price = configured_billing_price_for_timestamp(config, now_ts=ts)
            billing_price = strompreis_basis if configured_price is None else configured_price

        # 3. Eco-Score fuer netzdienliche Regelung
        # Kein Cheap-Score: Netzdienlichkeit kommt aus dem Marktpreis,
        # Nutzerkosten bleiben separat in billing_price.
        optimization_score = pure_eco_score
        if stromtarif_typ in ("static", "fix", "fixed", "flat") and not grid_friendly_mode:
            optimization_score = 50.0

        # Override rules
        if d["marketprice"] < -50.0:  # Extrem negativ
            optimization_score = 100.0

        score_entry = {
            "start_timestamp": d["start_timestamp"],
            "end_timestamp": d["end_timestamp"],
            "market_price": round(d["marketprice"], 2),
            "billing_price": round(billing_price, 2),
            "price_source": d.get("price_source") or d.get("tariff_provider") or "",
            "price_resolution_min": d.get("price_resolution_min"),
            "source_resolution_min": d.get("source_resolution_min"),
            "pure_eco_score": round(pure_eco_score, 1),
            "optimization_score": round(optimization_score, 1)
        }
        direct_market_ct = d.get("direct_marketing_market_price_ct")
        direct_market_eur_mwh = d.get("direct_marketing_marketprice")
        if direct_market_ct is not None and str(direct_market_ct).strip() != "":
            score_entry["direct_marketing_market_price_ct"] = round(
                safe_float(direct_market_ct, 0.0),
                5,
            )
        if direct_market_eur_mwh is not None and str(direct_market_eur_mwh).strip() != "":
            score_entry["direct_marketing_marketprice"] = round(
                safe_float(direct_market_eur_mwh, 0.0),
                5,
            )
        if "direct_marketing_market_price_ct" in score_entry or "direct_marketing_marketprice" in score_entry:
            score_entry["direct_marketing_price_source"] = d.get("direct_marketing_price_source") or ""
            score_entry["direct_marketing_price_resolution_min"] = d.get("direct_marketing_price_resolution_min")
            score_entry["direct_marketing_source_resolution_min"] = d.get("direct_marketing_source_resolution_min")
            score_entry["direct_marketing_price_revision"] = d.get("direct_marketing_price_revision")
            score_entry["direct_marketing_price_revision_source"] = d.get(
                "direct_marketing_price_revision_source"
            )
            score_entry["direct_marketing_price_available"] = bool(d.get("direct_marketing_price_available", True))
        eco_scores.append(score_entry)

    return eco_scores


def generate_configured_tariff_score(tariff_slots):
    """Erzeugt eine neutrale Preisprojektion ohne erfundenen Markt-Eco-Score."""
    scores = []
    for slot in tariff_slots or []:
        try:
            start_ms = int(slot.get("start_timestamp", 0))
            end_ms = int(slot.get("end_timestamp", start_ms + 900000))
            billing_price = float(slot.get("billing_price_ct"))
        except (TypeError, ValueError):
            continue
        if start_ms <= 0 or end_ms <= start_ms:
            continue
        scores.append({
            "start_timestamp": start_ms,
            "end_timestamp": end_ms,
            "market_price": None,
            "billing_price": round(billing_price, 2),
            "price_source": "configured_tariff",
            "price_resolution_min": slot.get("price_resolution_min", 15),
            "source_resolution_min": slot.get("source_resolution_min", 15),
            "pure_eco_score": None,
            "optimization_score": 50.0,
        })
    return scores


def generate_price_boost_plan(price_data, eco_scores, config):
    """
    Berechnet günstige Preisfenster für explizit freigegebene Verbraucher.
    Die Freigabe ist ausschließlich für echte Börsenpreisslots zulässig.
    Wiederkehrende Tarifzeiten wie Octopus Heat sind kein Negativpreisvertrag.
    """
    def safe_float(val, default_val):
        try:
            if val is None or str(val).strip() == "":
                return default_val
            if isinstance(val, str):
                val = val.replace(',', '.')
            return float(val)
        except (ValueError, TypeError):
            return default_val

    def safe_bool(val, default=False):
        if isinstance(val, bool):
            return val
        if val is None:
            return default
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    stromtarif_typ = str(config.get("stromtarif_typ", "static")).strip().lower()
    supported_tariff = supports_spot_market_prices(config)
    enabled = supported_tariff and safe_bool(config.get("cheap_grid_boost_enable", 0), False)
    price_limit_ct = safe_float(config.get("cheap_grid_price_limit_ct", 0.0), 0.0)
    min_duration_min = int(safe_float(config.get("cheap_grid_min_duration_min", 15), 15))
    now_ms = int(time.time() * 1000)

    score_by_ts = {}
    for score in eco_scores or []:
        try:
            score_by_ts[int(score.get("start_timestamp", 0))] = score
        except Exception:
            continue

    market_intervals = []
    for raw in price_data or []:
        try:
            interval_start = int(raw.get("start_timestamp", 0))
            interval_end = int(raw.get("end_timestamp", interval_start + 900000))
            market_value = float(raw.get("marketprice"))
        except (TypeError, ValueError):
            continue
        if interval_start > 0 and interval_end > interval_start:
            market_intervals.append((interval_start, interval_end, market_value))

    if stromtarif_typ == "octopus_heat":
        plan_slots = recurring_tariff_slots(config, now_ms=now_ms)
    else:
        plan_slots = list(price_data or [])

    slots = []
    for raw in plan_slots:
        try:
            st = int(raw.get("start_timestamp", 0))
            en = int(raw.get("end_timestamp", st + 900000))
            score = score_by_ts.get(st, {})
        except (TypeError, ValueError):
            continue

        if st < now_ms - 3600000 or st > now_ms + 48 * 3600000:
            continue

        market_price = None
        for interval_start, interval_end, market_value in market_intervals:
            if interval_start <= st < interval_end:
                market_price = market_value
                break

        billing_raw = score.get("billing_price")
        if billing_raw is None:
            billing_raw = raw.get("billing_price_ct")
        if billing_raw is None and stromtarif_typ == "octopus_heat":
            billing_raw = configured_billing_price_for_timestamp(config, now_ts=st / 1000.0)
        if billing_raw is None and market_price is not None:
            billing_raw = market_price / 10.0
        try:
            billing_price = float(billing_raw)
        except (TypeError, ValueError):
            continue

        cheap_slot = False
        if enabled:
            # Der Sonderpfad ist kein allgemeines Günstigpreisfenster. 0
            # bedeutet strikt negativer Abrechnungspreis; ein negativerer
            # Nutzerwert darf die Freigabe weiter verschärfen.
            effective_negative_limit = min(0.0, price_limit_ct)
            cheap_slot = billing_price < 0.0 and billing_price <= effective_negative_limit

        slots.append({
            "start_timestamp": st,
            "end_timestamp": en,
            "market_price": None if market_price is None else round(market_price, 2),
            "billing_price": round(billing_price, 2),
            "price_source": str(raw.get("price_source") or score.get("price_source") or ""),
            "cheap": bool(cheap_slot),
        })

    slots.sort(key=lambda s: s["start_timestamp"])
    windows = []
    current = None
    for slot in slots:
        if slot["cheap"]:
            if current and slot["start_timestamp"] <= current["end_timestamp"] + 1000:
                current["end_timestamp"] = max(current["end_timestamp"], slot["end_timestamp"])
                current["slots"].append(slot)
            else:
                current = {
                    "start_timestamp": slot["start_timestamp"],
                    "end_timestamp": slot["end_timestamp"],
                    "slots": [slot],
                }
                windows.append(current)
        else:
            current = None

    min_duration_ms = max(1, min_duration_min) * 60000
    clean_windows = []
    for win in windows:
        duration_ms = win["end_timestamp"] - win["start_timestamp"]
        if duration_ms < min_duration_ms:
            continue
        prices = [s["billing_price"] for s in win["slots"]]
        market_prices = [s["market_price"] for s in win["slots"] if s["market_price"] is not None]
        clean_windows.append({
            "start_timestamp": win["start_timestamp"],
            "end_timestamp": win["end_timestamp"],
            "duration_min": int(round(duration_ms / 60000.0)),
            "slot_count": len(win["slots"]),
            "min_billing_price": round(min(prices), 2),
            "avg_billing_price": round(sum(prices) / len(prices), 2),
            "min_market_price": round(min(market_prices), 2) if market_prices else None,
        })

    active_window = None
    for win in clean_windows:
        if win["start_timestamp"] <= now_ms < win["end_timestamp"]:
            active_window = win
            break

    return {
        "ts": now_ms,
        "valid_until_ts_ms": now_ms + PRICE_BOOST_PLAN_MAX_AGE_S * 1000,
        "publish_interval_s": PRICE_BOOST_PUBLISH_INTERVAL_S,
        "enabled": enabled,
        "supported": supported_tariff,
        "unsupported_reason": "" if supported_tariff else "Negativpreis-Boost ist nur für echte Börsenpreistarife verfügbar",
        "tariff_axis": "configured_recurring" if stromtarif_typ == "octopus_heat" else "market",
        "timezone": TARIFF_TIMEZONE_NAME if stromtarif_typ == "octopus_heat" else None,
        "active": active_window is not None,
        "price_limit_ct": price_limit_ct,
        "min_duration_min": min_duration_min,
        "active_window": active_window,
        "windows": clean_windows,
        "allow": {
            "battery": safe_bool(config.get("cheap_grid_battery_enable", 1), True),
            "wallbox": safe_bool(config.get("cheap_grid_wallbox_enable", 0), False),
            "heatpump": safe_bool(config.get("cheap_grid_heatpump_enable", 0), False),
            "heater": safe_bool(config.get("cheap_grid_heater_enable", 0), False),
        },
        "limits": {
            "battery_max_soc": safe_float(config.get("cheap_grid_battery_max_soc", 80.0), 80.0),
            "battery_max_w": safe_float(config.get("cheap_grid_battery_max_w", 0.0), 0.0),
            "pv_buffer_pct": safe_float(config.get("cheap_grid_pv_buffer_pct", 2.0), 2.0),
            "soc_hysteresis_pct": safe_float(config.get("cheap_grid_soc_hysteresis_pct", 0.5), 0.5),
        }
    }

def run():
    _initialize_runtime()
    logger.info("Starte EPEX Manager (V4)...")

    while True:
        try:
            config = load_v4_config()
            tariff_type = str(config.get("stromtarif_typ", "static")).strip().lower()
            provider = str(config.get("tariff_provider", "smard")).strip().lower()
            if tariff_type == "tibber":
                provider = "tibber"

            data = None
            smard_data = None
            entsoe_data = None
            awattar_data = None

            if provider == "tibber":
                data = fetch_tibber_with_log(config, "konfigurierter Provider")
                if data and not price_data_has_future_slots(data, min_horizon_s=900):
                    logger.warning(
                        "Tibber-Preisdaten ohne ausreichenden Zukunftshorizont (%s); nutze SMARD-Fallback.",
                        price_data_window_text(data),
                    )
                    data = None
                if data is None:
                    smard_data = fetch_smard_combined_data()
                    if smard_data and price_data_has_future_slots(smard_data, min_horizon_s=900):
                        logger.warning("Tibber nicht verfügbar; nutze SMARD-Fallback.")
                        data = smard_data
                    elif smard_data:
                        logger.warning(
                            "SMARD-Preisdaten ohne ausreichenden Zukunftshorizont (%s); "
                            "sie werden nicht in die Ramdisk geschrieben.",
                            price_data_window_text(smard_data),
                        )
                    if data is None:
                        entsoe_data = fetch_entsoe_with_log(config, "Tibber/SMARD-Fallback")
                        if entsoe_data and price_data_has_future_slots(entsoe_data, min_horizon_s=900):
                            data = entsoe_data
                        elif entsoe_data:
                            logger.warning(
                                "ENTSO-E-Fallback ohne ausreichenden Zukunftshorizont (%s).",
                                price_data_window_text(entsoe_data),
                            )
                    if data is None:
                        awattar_data = fetch_awattar_with_log("Tibber/SMARD-Fallback")
                        if awattar_data and price_data_has_future_slots(awattar_data, min_horizon_s=900):
                            data = awattar_data

            elif provider == "entsoe":
                data = fetch_entsoe_with_log(config, "konfigurierter Provider")
                if data and not price_data_has_future_slots(data, min_horizon_s=900):
                    logger.warning(
                        "ENTSO-E-Preisdaten ohne ausreichenden Zukunftshorizont (%s); nutze SMARD-Fallback.",
                        price_data_window_text(data),
                    )
                    data = None
                if data is None:
                    smard_data = fetch_smard_combined_data()
                    if smard_data and price_data_has_future_slots(smard_data, min_horizon_s=900):
                        data = smard_data
                if data is None:
                    awattar_data = fetch_awattar_with_log("ENTSO-E/SMARD-Fallback")
                    if awattar_data and price_data_has_future_slots(awattar_data, min_horizon_s=900):
                        data = awattar_data

            elif provider == "awattar":
                data = fetch_awattar_with_log("konfigurierter Provider")
                if data and not price_data_has_future_slots(data, min_horizon_s=900):
                    logger.warning(
                        "aWATTar-Preisdaten ohne ausreichenden Zukunftshorizont (%s); nutze SMARD-Fallback.",
                        price_data_window_text(data),
                    )
                    data = None
                if data is None:
                    smard_data = fetch_smard_combined_data()
                    if smard_data and price_data_has_future_slots(smard_data, min_horizon_s=900):
                        data = smard_data
                if data is None:
                    entsoe_data = fetch_entsoe_with_log(config, "aWATTar/SMARD-Fallback")
                    if entsoe_data and price_data_has_future_slots(entsoe_data, min_horizon_s=900):
                        data = entsoe_data
                    elif entsoe_data:
                        logger.warning(
                            "ENTSO-E-Fallback ohne ausreichenden Zukunftshorizont (%s).",
                            price_data_window_text(entsoe_data),
                        )

            else:
                smard_data = fetch_smard_combined_data()
                if smard_data and price_data_has_future_slots(smard_data, min_horizon_s=900):
                    data = smard_data
                elif smard_data:
                    logger.warning(
                        "SMARD-Preisdaten ohne ausreichenden Zukunftshorizont (%s); "
                        "sie werden nicht in die Ramdisk geschrieben.",
                        price_data_window_text(smard_data),
                    )
                if data is None:
                    entsoe_data = fetch_entsoe_with_log(config, "SMARD ohne Zukunftsslots")
                    if entsoe_data and price_data_has_future_slots(entsoe_data, min_horizon_s=900):
                        data = entsoe_data
                    elif entsoe_data:
                        logger.warning(
                            "ENTSO-E-Fallback ohne ausreichenden Zukunftshorizont (%s).",
                            price_data_window_text(entsoe_data),
                        )
                if data is None:
                    awattar_data = fetch_awattar_with_log("SMARD ohne Zukunftsslots")
                    if awattar_data and price_data_has_future_slots(awattar_data, min_horizon_s=900):
                        data = awattar_data
                    elif awattar_data:
                        logger.warning(
                            "aWATTar-Fallback ebenfalls ohne ausreichenden Zukunftshorizont (%s).",
                            price_data_window_text(awattar_data),
                        )

            if data and not price_data_has_future_slots(data, min_horizon_s=900):
                logger.warning(
                    "Preisdaten ohne ausreichenden Zukunftshorizont (%s); nutze Cache/Fallback.",
                    price_data_window_text(data),
                )
                data = None

            if data:
                direct_market_data = _fetch_direct_marketing_market_data(
                    config,
                    entsoe_data=entsoe_data,
                    smard_data=smard_data,
                )
                if direct_market_data:
                    data = apply_direct_marketing_market_overlay(data, direct_market_data, config)

                published, scores, boost_plan, publish_reason = publish_market_state(config, data)

                # 1. Speichere Rohdaten (für C++ Engine & Wallbox Kompatibilität)
                if not published:
                    logger.error("Marktfreigabe sicher deaktiviert: %s", publish_reason)
                    time.sleep(PRICE_BOOST_PUBLISH_INTERVAL_S)
                    continue
                logger.info(f"Spot-Preise gesichert ({len(data)} Einträge).")

                # 2. Generiere den neuen Netzdienlichkeits-Eco-Score (V4 Modul)
                scores = scores if published else []
                if scores:
                    # Already published atomically by publish_market_state().

                    # Logge aktuellen Score
                    now_ms = time.time() * 1000
                    for s in scores:
                        if s["start_timestamp"] <= now_ms < s["end_timestamp"]:
                            logger.info(f"Aktueller Optimization-Score: {s['optimization_score']}/100 (Billing: {s['billing_price']} ct/kWh)")
                            break

                if published and boost_plan.get("enabled"):
                    logger.info(
                        "Preis-Boost: %d Fenster unter %.2f ct/kWh%s" % (
                            len(boost_plan.get("windows", [])),
                            boost_plan.get("price_limit_ct", 0.0),
                            " (jetzt aktiv)" if boost_plan.get("active") else ""
                        )
                    )

            else:
                if recover_or_disable_market(config):
                    logger.warning(
                        "Konnte Spot-Preise nicht live laden; Ramdisk aus EPEX-Cache wiederhergestellt "
                        "oder lokale Tarifachse veröffentlicht."
                    )
                else:
                    # recover_or_disable_market hat bereits einen gesperrten Zustand publiziert.
                    logger.error("Konnte Spot-Preise von keinem Provider laden! Nächster Versuch in 5 Minuten.")

        except Exception as e:
            logger.error(f"Unerwarteter Fehler im EPEX Manager Loop: {e}")
            try:
                publish_disabled_market_state(locals().get("config", {}), "market_loop_error")
            except Exception as close_error:
                logger.critical("Marktfreigabe konnte nicht geschlossen werden: %s", close_error)

        # Aktualisiere stündlich (oder falls unglücklich gestartet, alle 30 min probieren, um keine Sprünge zu verpassen)
        # Die Strombörse wird täglich um 14:00 für den nächsten Tag veröffentlicht. Wir polen einfach alle 30 Mins.
        time.sleep(PRICE_BOOST_PUBLISH_INTERVAL_S)

if __name__ == "__main__":
    run()


def _install_forecast_evidence_service_compat(
    create_service_file,
    *,
    evidence_enabled,
    start_services,
    defer_activation=False,
    bundle_snapshot=None,
):
    """Installiert den optionalen Sidecar nur mit vollständig passendem Helper.

    Bei Release-Wechseln aus älteren Python-Prozessen kann ``Installer.utils``
    noch die frühere, schmalere Helper-Signatur tragen. Der rein diagnostische
    Sidecar darf dann weder den Gesamtwechsel blockieren noch mit impliziten
    Enable-/Restart-Defaults angelegt werden.
    """

    optional_kwargs = {
        "enable_service": bool(evidence_enabled),
        "restart_policy": "on-failure",
        "nice": 10,
        "io_scheduling_class": "idle",
        "after_services": (
            "e3dc-live.service",
            "e3dc-weather-manager.service",
        ),
        "defer_activation": bool(defer_activation),
        "bundle_snapshot": bundle_snapshot,
    }
    try:
        parameters = inspect.signature(create_service_file).parameters
    except (TypeError, ValueError):
        parameters = {}
    missing = sorted(key for key in optional_kwargs if key not in parameters)
    if missing:
        if not evidence_enabled:
            return True
        print(
            "  [!] Alter Service-Helper kann das transaktionale Dienstbundle "
            "nicht sicher abbilden "
            f"(fehlender Vertrag: {', '.join(missing)})."
        )
        return False

    result = create_service_file(
        "e3dc-forecast-evidence",
        "E3DC PV-Prognosediagnose (read-only)",
        "forecast_evidence_sidecar.py",
        "python3",
        restart_sec=300,
        start_service=bool(start_services and evidence_enabled),
        **optional_kwargs,
    )
    return result is True

def install_epex_service(
    start_services=True,
    include_websocket=False,
    expected_recovery_dropins=None,
):
    print("Installiere E3DC-Control Kern-Manager Services...")
    from .utils import (
        _approved_storage_manager_unit_payloads,
        _create_service_file,
        _migrate_approved_storage_manager_unit_owner,
        activate_systemd_service_bundle,
        capture_systemd_service_bundle,
        install_e3dc_live_service,
        rollback_systemd_service_bundle,
        setup_websocket_service,
    )
    installer_dir = os.path.join(INSTALL_DIR, "Installer")

    required_scripts = [
        ("E3DC Live Daten Service", "e3dc_live.py"),
        ("EPEX & Strompreis Manager", "epex_manager.py"),
        ("Wetter & PV-Forecast Service", "Forecast/pv_forecast_service.py"),
        ("Storage Simulator", "storage_simulator.py"),
        ("Storage Manager", "storage_manager.py"),
    ]
    if include_websocket:
        required_scripts.append(("WebSocket Service", "e3dc_websocket.py"))
    missing_scripts = [
        (label, os.path.join(installer_dir, relative_path))
        for label, relative_path in required_scripts
        if not os.path.isfile(os.path.join(installer_dir, relative_path))
    ]
    if missing_scripts:
        for label, path in missing_scripts:
            print(f"  [!] Pflichtskript für {label} fehlt: {path}")
        return False

    evidence_path = os.path.join(
        installer_dir,
        "forecast_evidence_sidecar.py",
    )
    evidence_present = os.path.isfile(evidence_path)
    evidence_enabled = (
        _forecast_evidence_enabled_for_install() if evidence_present else False
    )

    service_names = [
        "e3dc-live",
        "e3dc-epex-manager",
        "e3dc-weather-manager",
        "e3dc-storage-simulator",
        "e3dc-storage-manager",
    ]
    if evidence_present:
        service_names.append("e3dc-forecast-evidence")
    if include_websocket:
        service_names.append("e3dc-websocket")
    expected_recovery_dropins = {
        unit: contract
        for unit, contract in dict(expected_recovery_dropins or {}).items()
        if unit.removesuffix(".service") in service_names
    }

    try:
        storage_unit_migrated = _migrate_approved_storage_manager_unit_owner(
            _approved_storage_manager_unit_payloads(),
            expected_recovery_dropins=dict(expected_recovery_dropins or {}).get(
                "e3dc-storage-manager.service"
            ),
        )
        if storage_unit_migrated:
            print(
                "  [OK] Bytegenaue Storage-Manager-Altunit sicher auf "
                "root:root 0644 migriert."
            )
    except Exception as exc:
        print(
            "  [!] Bestehende Storage-Manager-Unit ist nicht für die enge "
            f"Altbesitz-Migration freigegeben: {exc}"
        )
        return False

    try:
        service_snapshot = capture_systemd_service_bundle(
            service_names,
            expected_recovery_dropins=expected_recovery_dropins,
        )
    except Exception as exc:
        print(f"  [!] Bestehender Kerndienstzustand ist nicht sicher gebunden: {exc}")
        return False

    def _require_prepared(result, label, service_name):
        if result is not True:
            raise RuntimeError(f"{label} konnte nicht transaktional vorbereitet werden")

    try:
        total_steps = 7 if include_websocket else 6

        # 1. E3DC Live Daten Service (RSCP Python) - Basis für alle Kerndienste
        print(f"\n[1/{total_steps}] E3DC Live Daten Service...")
        _require_prepared(
            install_e3dc_live_service(
                start_service=False,
                defer_activation=True,
                bundle_snapshot=service_snapshot,
            ),
            "E3DC Live Daten Service",
            "e3dc-live",
        )

        # 2. EPEX Manager
        print(f"\n[2/{total_steps}] EPEX & Strompreis Manager...")
        _require_prepared(
            _create_service_file(
                "e3dc-epex-manager",
                "E3DC EPEX Manager",
                "epex_manager.py",
                "python3",
                start_service=False,
                defer_activation=True,
                bundle_snapshot=service_snapshot,
            ),
            "EPEX & Strompreis Manager",
            "e3dc-epex-manager",
        )

        # 3. Wetter-/PV-Forecast Service
        print(f"\n[3/{total_steps}] Wetter & PV-Forecast Service...")
        _require_prepared(
            _create_service_file(
                "e3dc-weather-manager",
                "E3DC Wetter & PV Forecast",
                "Forecast/pv_forecast_service.py",
                "python3",
                start_service=False,
                defer_activation=True,
                bundle_snapshot=service_snapshot,
            ),
            "Wetter & PV-Forecast Service",
            "e3dc-weather-manager",
        )

        # 4. Storage Simulator
        print(f"\n[4/{total_steps}] Storage Simulator...")
        _require_prepared(
            _create_service_file(
                "e3dc-storage-simulator",
                "E3DC Storage Simulator",
                "storage_simulator.py",
                "python3",
                start_service=False,
                defer_activation=True,
                bundle_snapshot=service_snapshot,
            ),
            "Storage Simulator",
            "e3dc-storage-simulator",
        )

        # 5. Storage Manager. Der aktuelle Regler ist kanonisch storage_manager.py;
        # der alte Regler bleibt nur als storage_manager_legacy.py im Repository.
        print(f"\n[5/{total_steps}] Storage Manager...")
        _require_prepared(
            _create_service_file(
                "e3dc-storage-manager",
                "E3DC Storage Manager",
                "storage_manager.py",
                "python3",
                restart_sec=5,
                start_service=False,
                after_services=("e3dc-live.service",),
                start_limit_interval_sec=300,
                start_limit_burst=3,
                defer_activation=True,
                bundle_snapshot=service_snapshot,
            ),
            "Storage Manager",
            "e3dc-storage-manager",
        )

        # 6. Rein diagnostischer Prognose-Sidecar. Die Unit wird immer
        # installiert, bleibt ohne ausdrückliche Nutzerfreigabe jedoch gestoppt.
        print(f"\n[6/{total_steps}] Optionale PV-Prognosediagnose...")
        if evidence_present:
            _require_prepared(
                _install_forecast_evidence_service_compat(
                    _create_service_file,
                    evidence_enabled=evidence_enabled,
                    start_services=False,
                    defer_activation=True,
                    bundle_snapshot=service_snapshot,
                ),
                "Optionale PV-Prognosediagnose",
                "e3dc-forecast-evidence",
            )

        if include_websocket:
            print(f"\n[7/{total_steps}] WebSocket Service...")
            _require_prepared(
                setup_websocket_service(
                    start_service=False,
                    defer_activation=True,
                    bundle_snapshot=service_snapshot,
                ),
                "WebSocket Service",
                "e3dc-websocket",
            )

        enabled_services = [
            "e3dc-live",
            "e3dc-epex-manager",
            "e3dc-weather-manager",
            "e3dc-storage-simulator",
            "e3dc-storage-manager",
        ]
        # Der einzige RSCP-Hardwarewriter im Kernbundle startet erst, nachdem
        # alle rein lesenden/diagnostischen Pflichtdienste bestätigt laufen.
        start_order = [
            "e3dc-live",
            "e3dc-epex-manager",
            "e3dc-weather-manager",
            "e3dc-storage-simulator",
        ]
        if evidence_present and evidence_enabled:
            enabled_services.append("e3dc-forecast-evidence")
            start_order.append("e3dc-forecast-evidence")
        if include_websocket:
            enabled_services.append("e3dc-websocket")
            start_order.append("e3dc-websocket")
        start_order.append("e3dc-storage-manager")

        if activate_systemd_service_bundle(
            service_snapshot,
            enabled_units=enabled_services,
            start_order=start_order,
            start_services=bool(start_services),
        ) is not True:
            raise RuntimeError("Kerndienstbundle konnte nicht vollständig aktiviert werden")
    except Exception as exc:
        print(f"  [!] Kerndienstbundle fehlgeschlagen: {exc}")
        if rollback_systemd_service_bundle(service_snapshot) is True:
            print("  [i] Vorheriger Unit-, Enablement- und Aktivzustand wiederhergestellt.")
        else:
            print("  [!] Rollback nicht vollständig bestätigt; alle Bundle-Dienste bleiben gestoppt.")
        return False

    print("\n[OK] Alle Kern-Dienste gemeinsam installiert und bestätigt.")
    return True


# Registriere die Installation der Manager global, damit core.py sie beim Start laden kann.
if __name__ != "__main__":
    from .core import register_command

    register_command(
        "400",
        "Kern-Dienste & Manager installieren",
        install_epex_service,
        sort_order=400,
        category="Kernsystem & Update",
    )
