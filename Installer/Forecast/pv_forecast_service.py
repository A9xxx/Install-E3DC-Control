import json
import os
import logging
import subprocess
import sys
import urllib.request
import urllib.error
import hashlib
import io
import math
import re
import socket
import time
import zipfile
import xml.etree.ElementTree as ET
import tempfile
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INSTALLER_DIR = os.path.dirname(SCRIPT_DIR)
REPO_ROOT = os.path.dirname(INSTALLER_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if INSTALLER_DIR not in sys.path:
    sys.path.insert(0, INSTALLER_DIR)

from Forecast.pv_forecast_diagnostic_details import build_stage_metadata, provider_resource_samples

from pv_forecast_topology import (
    RESOURCE_PROJECTION_ABS_TOLERANCE_W,
    RESOURCE_PROJECTION_REL_TOLERANCE,
    build_pv_forecast_topology,
    configured_generator_groups,
    configured_provider_bindings,
    has_explicit_topology_config,
    legacy_provider_resource_duplicate_keys,
    project_slot_topology,
    topology_resource_keys,
)
def _resource_projection_state_for_models(
    *,
    timestamp,
    model_values,
    model_weights,
    resource_models,
    resource_keys,
):
    """Prüft neutrale Ressourcenbeiträge vor dem Ensemble-Mischen.

    Alte Modellcaches ohne ``resource_data`` bleiben damit ausdrücklich ohne
    DC-/AC-Splitwirkung. Nur Modelle mit einem wirksamen Gewicht und positiver
    Gesamtprognose benötigen einen vollständigen, summenkohärenten Beitrag.
    """

    required = tuple(str(key) for key in resource_keys if str(key))
    if not required:
        return {"status": "complete", "reason": "OK"}

    incomplete = False
    mismatch = False
    for model_id in ("m1", "m2", "m3"):
        try:
            weight = float(model_weights.get(model_id) or 0.0)
            model_total = max(0.0, float(model_values.get(model_id) or 0.0))
        except (TypeError, ValueError):
            incomplete = True
            continue
        if weight <= 0.0 or model_total <= 0.005:
            continue
        model_resources = resource_models.get(model_id) or {}
        if any(
            key not in model_resources
            or timestamp not in (model_resources.get(key) or {})
            or (model_resources.get(key) or {}).get(timestamp) is None
            for key in required
        ):
            incomplete = True
            continue
        try:
            resource_total = sum(
                max(0.0, float(model_resources[key][timestamp] or 0.0))
                for key in required
            )
        except (TypeError, ValueError):
            incomplete = True
            continue
        tolerance_kw = max(
            RESOURCE_PROJECTION_ABS_TOLERANCE_W / 1000.0,
            model_total * RESOURCE_PROJECTION_REL_TOLERANCE,
        )
        if abs(model_total - resource_total) > tolerance_kw:
            mismatch = True

    if mismatch:
        return {"status": "unbound", "reason": "RESOURCE_PROJECTION_TOTAL_MISMATCH"}
    if incomplete:
        return {"status": "unbound", "reason": "RESOURCE_PROJECTION_INCOMPLETE"}
    return {"status": "complete", "reason": "OK"}


def _merge_resource_projection_states(*states):
    reasons = {
        str((state or {}).get("reason") or "RESOURCE_PROJECTION_INCOMPLETE")
        for state in states
        if str((state or {}).get("status") or "") != "complete"
    }
    if "RESOURCE_PROJECTION_TOTAL_MISMATCH" in reasons:
        return {"status": "unbound", "reason": "RESOURCE_PROJECTION_TOTAL_MISMATCH"}
    if reasons:
        return {"status": "unbound", "reason": "RESOURCE_PROJECTION_INCOMPLETE"}
    return {"status": "complete", "reason": "OK"}


def _weighted_split_freshness(*, model_values, model_weights, model_freshness, projection_state):
    """Belegt Frische nur für tatsächlich gewichtete Split-Modelle.

    Eine aktuelle Ensemble-Berechnung ersetzt keinen frischen Provider- oder
    Modellcache.  Fehlende Provenienz und der bewusst zugelassene
    ``stale_complete_fallback`` bleiben daher hart negativ.
    """

    if str((projection_state or {}).get("status") or "") != "complete":
        return {
            "fresh": False,
            "source": "resource_projection_" + str((projection_state or {}).get("reason") or "incomplete").lower(),
        }
    required = []
    for model_id in ("m1", "m2", "m3"):
        try:
            weight = float((model_weights or {}).get(model_id) or 0.0)
        except (TypeError, ValueError):
            return {"fresh": False, "source": "model_provenance_unknown"}
        if weight > 0.0:
            required.append(model_id)
    if not required:
        return {"fresh": False, "source": "no_weighted_source_model"}
    for model_id in required:
        state = dict((model_freshness or {}).get(model_id) or {})
        if state.get("fresh") is not True:
            return {
                "fresh": False,
                "source": str(state.get("source") or "model_provenance_unknown"),
            }
    return {"fresh": True, "source": "weighted_models_within_ttl"}


def _merge_split_freshness(*states):
    for state in states:
        if not isinstance(state, dict) or state.get("fresh") is not True:
            return {
                "fresh": False,
                "source": str((state or {}).get("source") or "model_provenance_unknown"),
            }
    return {"fresh": True, "source": "weighted_models_within_ttl"}


def _weighted_resource_contribution(*, resource_models, resource_key, timestamp, model_values, model_weights):
    """Summiert nur tatsächlich wirksame, zuvor vollständig geprüfte Modelle.

    Ein Modell mit Gewicht 0 oder einem nichtpositiven Slotwert trägt exakt
    null bei und wird nicht indexiert. Für jedes wirksame Modell bleibt der
    direkte Indexzugriff absichtlich hart: fehlende Ressourcendaten dürfen
    weder als 0 noch als vollständiger Quellsplit erscheinen.
    """

    total = 0.0
    for model_id in ("m1", "m2", "m3"):
        weight = float((model_weights or {}).get(model_id) or 0.0)
        value = float((model_values or {}).get(model_id) or 0.0)
        if weight <= 0.0 or value <= 0.005:
            continue
        total += float(resource_models[model_id][resource_key][timestamp]) * weight
    return total

def _legacy_config_files():
    """Nutzt nur die modullokale und kanonische Webkonfiguration; Home-Pfade werden nie geraten."""
    result = []
    for candidate in (
        os.path.join(REPO_ROOT, "e3dc.config.txt"),
        "/var/www/html/data/e3dc.config.txt",
        "/var/www/html/e3dc.config.txt",
    ):
        lexical = os.path.abspath(candidate)
        if lexical != os.path.realpath(lexical) or not os.path.isfile(lexical):
            continue
        result.append(lexical)
    return tuple(result)


_INITIAL_LEGACY_CONFIG_FILES = _legacy_config_files()
CONFIG_FILE = _INITIAL_LEGACY_CONFIG_FILES[0] if _INITIAL_LEGACY_CONFIG_FILES else ""

RAMDISK_DIR = "/var/www/html/ramdisk"
if not os.path.exists(RAMDISK_DIR):
    os.makedirs(RAMDISK_DIR, exist_ok=True)
FORECAST_OUTPUT = os.path.join(RAMDISK_DIR, "pv_forecast.json")
WEATHER_ALERTS_OUTPUT = os.path.join(RAMDISK_DIR, "weather_alerts.json")
ML_PREDICTION_OUTPUT = os.path.join(RAMDISK_DIR, "ml_prediction.json")
LEGACY_ML_MODEL_FILE = "/var/www/html/data/ml_model.pkl"  # Nur ein Signal; wird weder gelesen noch geladen.
MODEL_CACHE_FILE = "/var/www/html/logs/forecast_model_cache.json"
FORECAST_EVAL_FILE = "/var/www/html/logs/pv_forecast_eval.json"
DAILY_STATS_DB_PATH = "/var/www/html/data/e3dc_stats.db"
DWD_CAP_WARNINGS_URL = (
    "https://opendata.dwd.de/weather/alerts/cap/COMMUNEUNION_EVENT_STAT/"
    "Z_CAP_C_EDZW_LATEST_PVW_STATUS_PREMIUMEVENT_COMMUNEUNION_DE.zip"
)

# --- Abruf-Intervalle pro Modell (in Sekunden) ---
# Forecast.Solar: kostenlos, ~1000 Calls/Tag -> 60 Min reichen
# Open-Meteo:    kostenlos, unlimitiert      -> 60 Min reichen
# Solcast:       Home-PV default 10 Requests/Tag, exakt per Konto budgetiert
MODEL_TTL = {
    "m1": 60 * 60,     # Forecast.Solar: 1 Stunde
    "m2": 60 * 60,     # Open-Meteo:     1 Stunde
    "m3": 4 * 60 * 60  # Solcast:        Fallback ohne Sites
}
WEATHER_ALERT_TTL_S = 5 * 60
ML_PREDICTION_MAX_AGE_S = 90 * 60
SOLCAST_DEFAULT_CALLS_PER_DAY = 10
SOLCAST_MIN_TTL_S = 60 * 60
SOLCAST_CACHE_SCHEMA_VERSION = 3
SOLCAST_SIGNATURE_SCHEMA = "solcast_sites_v3"
MODEL_RESOURCE_SCHEMA = "neutral_fc_contributions_v1"
PV_PHYSICAL_PEAK_MARGIN = 1.03
PV_CLOUD_EDGE_PEAK_MARGIN = 1.15
PV_DAILY_SANITY_HISTORY_DAYS = 730
PV_DAILY_SANITY_MIN_DAYS = 7
PV_DAILY_SANITY_MARGIN = 1.08
PV_DAILY_SANITY_DOY_WINDOW_DAYS = 45
PV_BIAS_MIN_QUARTER_DAYS = 7
PV_BIAS_CONFIRMATION_WINDOW_DAYS = 45
PV_BIAS_CONFIRMATION_TOLERANCE = 0.06
PV_BIAS_MIN_CONFIRMATIONS = 1
PV_BIAS_SCHEMA3_GUARD_WINDOW_DAYS = 14
PV_BIAS_SCHEMA3_VISIBLE_OVERSHOOT = 0.97
PV_BIAS_SCHEMA3_GUARD_MARGIN = 1.06
FORECAST_ISSUE_SCHEMA = "pv_forecast_issue_v1"
FORECAST_SOURCE_COMPOSITION_SCHEMA = "pv_forecast_source_composition_v1"
FORECAST_VALUE_STAGE = "displayed_postprocessed"
FORECAST_DISTRIBUTION_TYPE = "deterministic_point"
FORECAST_PRODUCER_TIME_BASIS = "producer_output_generation_utc_v1"
PV_ZERO_EVIDENCE_SCHEMA = "pv_zero_evidence_v1"
PV_ZERO_EVIDENCE_REASON = "ASTRONOMICAL_NIGHT"
PV_ZERO_SOLAR_WINDOW_SCHEMA = "pv_forecast_solar_window_v1"
# Die bestehende Sonnenfensterformel arbeitet in lokaler Standardzeit. Der
# konservative Abstand deckt Sommerzeit, Gleichung der Zeit, Refraktion und
# Rundung ab; Grenzslots bleiben bewusst ohne Nacht-Null-Beleg.
PV_ZERO_SOLAR_GUARD_S = 90 * 60

logger = logging.getLogger("EnsemblePVForecaster")

def _roof_float(value):
    return float(str(value).strip().replace(',', '.'))

def _parse_roof_config(value):
    text = str(value or "").strip()
    if not text:
        return []

    roofs = []
    if "/" in text:
        pattern = r"(-?\d+(?:[.,]\d+)?)\s*/\s*(-?\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)"
        for match in re.finditer(pattern, text):
            try:
                roofs.append({
                    "tilt": _roof_float(match.group(1)),
                    "azimuth": _roof_float(match.group(2)),
                    "kwp": _roof_float(match.group(3)),
                })
            except ValueError:
                continue
        if roofs:
            return roofs

    parts = [part.strip() for part in re.split(r"[,;/\s]+", text) if part.strip()]
    if len(parts) >= 3:
        try:
            return [{
                "tilt": _roof_float(parts[0]),
                "azimuth": _roof_float(parts[1]),
                "kwp": _roof_float(parts[2]),
            }]
        except ValueError:
            return []
    return []


def _forecast_history_path():
    return os.path.join(RAMDISK_DIR, "pv_forecast_history.json")


def _forecast_site_descriptor(roofs, installed_kwp=None):
    normalized_roofs = []
    for roof in roofs or []:
        try:
            normalized_roofs.append({
                "tilt": round(float(roof.get("tilt", 0.0)), 1),
                "azimuth": round(float(roof.get("azimuth", 0.0)), 1),
                "kwp": round(float(roof.get("kwp", 0.0)), 3),
            })
        except Exception:
            continue
    normalized_roofs.sort(key=lambda r: (r["azimuth"], r["tilt"], r["kwp"]))
    try:
        kwp = round(float(installed_kwp or 0.0), 3)
    except Exception:
        kwp = 0.0
    return {
        "roofs": normalized_roofs,
        "configured_kwp": round(sum(r["kwp"] for r in normalized_roofs), 3),
        "installed_kwp": kwp,
    }


def _forecast_site_signature(roofs, installed_kwp=None):
    descriptor = _forecast_site_descriptor(roofs, installed_kwp)
    payload = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16], descriptor


def _quarter_for_date(day):
    if isinstance(day, datetime):
        month = day.month
    else:
        month = day.month
    return f"Q{(month - 1) // 3 + 1}"


def _day_of_year_distance(a, b):
    da = int(a.strftime("%j"))
    db = int(b.strftime("%j"))
    diff = abs(da - db)
    return min(diff, 366 - diff)


def _quarter_training_counts(daily_log, site_signature=None):
    counts = {}
    for entry in daily_log or []:
        entry_signature = str(entry.get("site_signature") or "")
        if site_signature and entry_signature and entry_signature != str(site_signature):
            continue
        day = _parse_iso_date(entry.get("date"))
        quarter = str(entry.get("quarter") or (_quarter_for_date(day) if day else ""))
        if not quarter:
            continue
        counts.setdefault(quarter, {"total": 0, "eligible": 0})
        counts[quarter]["total"] += 1
        if str(entry.get("clearsky_class") or "sunny") != "cloudy":
            counts[quarter]["eligible"] += 1
    return counts


def _daily_log_entry_schema(entry):
    if not isinstance(entry, dict):
        return "unknown"
    try:
        schema_version = int(entry.get("schema_version") or 0)
    except (TypeError, ValueError):
        schema_version = 0
    if (
        schema_version >= 3
        or (
            "raw_forecast_kwh" in entry
            and "bias_corrected_kwh" in entry
            and "visible_forecast_kwh" in entry
        )
    ):
        return "raw_bias_visible"
    if "raw_forecast_kwh" in entry or "visible_forecast_kwh" in entry:
        return "partial_raw_visible"
    if "forecast_kwh" in entry and "actual_kwh" in entry:
        return "legacy_forecast_only"
    return "unknown"


def _daily_log_schema_counts(daily_log, site_signature=None):
    counts = {
        "raw_bias_visible": 0,
        "partial_raw_visible": 0,
        "legacy_forecast_only": 0,
        "unknown": 0,
    }
    for entry in daily_log or []:
        entry_signature = str(entry.get("site_signature") or "")
        if site_signature and entry_signature and entry_signature != str(site_signature):
            continue
        schema = _daily_log_entry_schema(entry)
        counts[schema] = counts.get(schema, 0) + 1
    return counts


def _entry_bias_value(entry):
    if not isinstance(entry, dict):
        return None
    for key in ("bias_raw", "bias_target", "bias_new"):
        try:
            value = float(entry.get(key))
            if 0.2 <= value <= 3.0:
                return value
        except (TypeError, ValueError):
            pass
    try:
        actual = float(entry.get("actual_kwh") or 0.0)
        raw = float(entry.get("raw_forecast_kwh") or 0.0)
        if actual > 0.5 and raw > 1.0:
            value = actual / raw
            if 0.2 <= value <= 3.0:
                return value
    except (TypeError, ValueError):
        pass
    try:
        actual = float(entry.get("actual_kwh") or 0.0)
        forecast = float(entry.get("forecast_kwh") or 0.0)
        if actual > 0.5 and forecast > 1.0:
            value = actual / forecast
            if 0.2 <= value <= 3.0:
                return value
    except (TypeError, ValueError):
        pass
    return None


def _bias_confirmation_status(
    daily_log,
    quarter,
    bias,
    now=None,
    site_signature=None,
    window_days=PV_BIAS_CONFIRMATION_WINDOW_DAYS,
    tolerance=PV_BIAS_CONFIRMATION_TOLERANCE,
):
    now = now or datetime.now()
    try:
        now_day = now.date()
    except AttributeError:
        now_day = now
    try:
        bias_value = float(bias or 1.0)
    except (TypeError, ValueError):
        bias_value = 1.0
    threshold = max(0.04, abs(bias_value) * float(tolerance))
    confirmation_count = 0
    recent_samples = 0
    latest_confirmed_date = None
    latest_confirmed_bias = None
    latest_sample_date = None
    latest_sample_bias = None

    for entry in daily_log or []:
        if not isinstance(entry, dict):
            continue
        entry_signature = str(entry.get("site_signature") or "")
        if site_signature and entry_signature and entry_signature != str(site_signature):
            continue
        day = _parse_iso_date(entry.get("date"))
        if not day:
            continue
        age_days = (now_day - day).days
        if age_days < 0 or age_days > int(window_days):
            continue
        entry_quarter = str(entry.get("quarter") or _quarter_for_date(day))
        if entry_quarter != str(quarter):
            continue
        if str(entry.get("clearsky_class") or "sunny") == "cloudy":
            continue
        value = _entry_bias_value(entry)
        if value is None:
            continue
        recent_samples += 1
        latest_sample_date = day.isoformat()
        latest_sample_bias = value
        if abs(value - bias_value) <= threshold:
            confirmation_count += 1
            latest_confirmed_date = day.isoformat()
            latest_confirmed_bias = value

    return {
        "confirmed": confirmation_count >= PV_BIAS_MIN_CONFIRMATIONS,
        "confirmation_count": confirmation_count,
        "min_confirmations": PV_BIAS_MIN_CONFIRMATIONS,
        "recent_samples": recent_samples,
        "window_days": int(window_days),
        "tolerance": round(float(tolerance), 4),
        "threshold": round(threshold, 4),
        "latest_confirmed_date": latest_confirmed_date,
        "latest_confirmed_bias": round(latest_confirmed_bias, 4) if latest_confirmed_bias is not None else None,
        "latest_sample_date": latest_sample_date,
        "latest_sample_bias": round(latest_sample_bias, 4) if latest_sample_bias is not None else None,
    }


def _recent_schema3_visible_overshoot_guard(
    daily_log,
    quarter,
    bias,
    now=None,
    site_signature=None,
    window_days=PV_BIAS_SCHEMA3_GUARD_WINDOW_DAYS,
    visible_ratio_threshold=PV_BIAS_SCHEMA3_VISIBLE_OVERSHOOT,
    margin=PV_BIAS_SCHEMA3_GUARD_MARGIN,
):
    """Begrenzt alten Bias, wenn neue raw/visible-Tage sichtbare Überprognose zeigen."""
    now = now or datetime.now()
    try:
        now_day = now.date()
    except AttributeError:
        now_day = now
    try:
        bias_value = float(bias or 1.0)
    except (TypeError, ValueError):
        bias_value = 1.0

    recent_schema3 = []
    for entry in daily_log or []:
        if not isinstance(entry, dict):
            continue
        entry_signature = str(entry.get("site_signature") or "")
        if site_signature and entry_signature and entry_signature != str(site_signature):
            continue
        if _daily_log_entry_schema(entry) != "raw_bias_visible":
            continue
        day = _parse_iso_date(entry.get("date"))
        if not day:
            continue
        age_days = (now_day - day).days
        if age_days < 0 or age_days > int(window_days):
            continue
        entry_quarter = str(entry.get("quarter") or _quarter_for_date(day))
        if entry_quarter != str(quarter):
            continue
        if str(entry.get("clearsky_class") or "sunny") == "cloudy":
            continue
        try:
            visible_ratio = float(entry.get("visible_ratio"))
        except (TypeError, ValueError):
            continue
        target = None
        for key in ("bias_target", "bias_raw", "bias_new"):
            try:
                candidate = float(entry.get(key))
                if 0.2 <= candidate <= 3.0:
                    target = candidate
                    break
            except (TypeError, ValueError):
                continue
        if target is None:
            continue
        recent_schema3.append({
            "date": day.isoformat(),
            "visible_ratio": round(visible_ratio, 4),
            "bias_target": round(target, 4),
            "bias_raw": entry.get("bias_raw"),
            "bias_new": entry.get("bias_new"),
        })

    overshoots = [
        item for item in recent_schema3
        if item["visible_ratio"] < float(visible_ratio_threshold)
        and item["bias_target"] < bias_value - 0.005
    ]
    latest = recent_schema3[-1] if recent_schema3 else None
    latest_overshoot = overshoots[-1] if overshoots else None
    result = {
        "active": False,
        "reason": "no_recent_schema3_overshoot",
        "window_days": int(window_days),
        "visible_ratio_threshold": round(float(visible_ratio_threshold), 4),
        "guard_margin": round(float(margin), 4),
        "schema3_recent_samples": len(recent_schema3),
        "schema3_overshoot_samples": len(overshoots),
        "latest_schema3": latest,
        "latest_overshoot": latest_overshoot,
    }
    if not latest_overshoot:
        return result

    guarded_bias = max(0.75, min(bias_value, latest_overshoot["bias_target"] * float(margin)))
    result["effective_bias_cap"] = round(guarded_bias, 4)
    if guarded_bias < bias_value - 0.005:
        result["active"] = True
        result["reason"] = "recent_schema3_visible_overshoot"
    return result


def _load_forecast_eval(eval_path=FORECAST_EVAL_FILE):
    if os.path.exists(eval_path):
        with open(eval_path, 'r') as f:
            data = json.load(f)
    else:
        data = {"version": 3, "daily_log": [], "seasonal_bias": {}, "last_update": ""}

    if "daily_errors" in data and "daily_log" not in data:
        data["daily_log"] = []
        data["seasonal_bias"] = {}
        data["version"] = 2
        logger.info("pv_forecast_eval.json: Migration auf Version 2 (EWMA-Bias).")

    data.setdefault("daily_log", [])
    data.setdefault("seasonal_bias", {})
    data.setdefault("last_update", "")
    data["version"] = max(int(data.get("version", 1) or 1), 3)
    return data


def _ensure_eval_site_signature(eval_data, site_signature=None, site_descriptor=None, now=None):
    if not site_signature:
        return eval_data, False
    now = now or datetime.now()
    current = str(site_signature)
    stored = str(eval_data.get("site_signature") or "")
    changed = bool(stored and stored != current)
    if changed:
        eval_data["seasonal_bias"] = {}
        eval_data["daily_log"] = []
        eval_data["last_update"] = ""
        eval_data["site_signature_changed_at"] = now.isoformat(timespec='seconds')
        eval_data["site_signature_seen_at"] = now.isoformat(timespec='seconds')
        eval_data["signature_reset_reason"] = "roof_config_changed"
        logger.warning(
            "PV-Prognose-Kalibrierung zurückgesetzt: Dach-/Anlagen-Signatur hat sich geändert "
            f"({stored} -> {current})."
        )
    elif not stored:
        eval_data["site_signature_seen_at"] = eval_data.get("site_signature_seen_at") or ""

    eval_data["site_signature"] = current
    if site_descriptor is not None:
        eval_data["site_descriptor"] = site_descriptor
    return eval_data, changed


def _slot_kw(slot, *keys):
    for key in keys:
        if key in slot and slot.get(key) is not None:
            try:
                return float(slot.get(key) or 0.0)
            except Exception:
                continue
    return 0.0


def _canonical_json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_revision(value):
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _source_file_revision(path):
    """Bindet die Forecast-Methode an den tatsächlich laufenden Quelltext."""

    try:
        with open(path, "rb") as handle:
            return "sha256:" + hashlib.sha256(handle.read()).hexdigest()
    except (OSError, ValueError):
        return None


FORECAST_METHOD_REVISION = _source_file_revision(__file__)


def _atomic_write_json(path, payload):
    """Publiziert JSON im Zielverzeichnis atomar und erhält den Dateimodus."""

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    if os.path.lexists(path) and os.path.islink(path):
        raise OSError("forecast_target_symlink")
    try:
        target_mode = os.stat(path, follow_symlinks=False).st_mode & 0o777
    except FileNotFoundError:
        target_mode = 0o644
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ).encode("utf-8")
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".pv_forecast.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        os.fchmod(descriptor, target_mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, target_mode, follow_symlinks=False)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _forecast_target_slot_material(slots):
    material = []
    for slot in slots or []:
        try:
            start_ms = int(slot.get("start_timestamp"))
            end_ms = int(slot.get("end_timestamp"))
        except (TypeError, ValueError):
            continue
        material.append({
            "slot_start_utc_s": int(round(start_ms / 1000.0)),
            "slot_end_utc_s": int(round(end_ms / 1000.0)),
        })
    material.sort(key=lambda item: (item["slot_start_utc_s"], item["slot_end_utc_s"]))
    return material


def _forecast_source_composition(
    *,
    models,
    resource_models,
    model_freshness,
    configured,
):
    providers = {
        "m1": "forecast_solar",
        "m2": "open_meteo_icon_ecmwf_ensemble",
        "m3": "solcast",
    }
    sources = []
    for model_id in ("m1", "m2", "m3"):
        model_values = dict((models or {}).get(model_id) or {})
        resource_values = dict((resource_models or {}).get(model_id) or {})
        freshness = dict((model_freshness or {}).get(model_id) or {})
        sources.append({
            "model_id": model_id,
            "provider": providers[model_id],
            "configured": bool((configured or {}).get(model_id)),
            "available": bool(model_values),
            "fresh": freshness.get("fresh") is True,
            "freshness_source": str(
                freshness.get("source") or "model_provenance_unknown"
            )[:96],
            "model_input_revision": _sha256_revision(model_values),
            "resource_input_revision": _sha256_revision(resource_values),
        })
    return {
        "schema_version": FORECAST_SOURCE_COMPOSITION_SCHEMA,
        "sources": sources,
    }


def build_forecast_issue_contract(
    slots,
    *,
    issued_at_utc_s,
    models,
    resource_models,
    model_freshness,
    configured,
):
    """Erzeugt den unveränderlichen Producer-Vertrag der sichtbaren Ausgabe.

    Unbekannte Revisionen bleiben ausdrücklich ``None`` mit
    ``EVIDENCE_LIMIT``. Weder ein Punktwert noch die Dateizeit wird dabei als
    Quantil beziehungsweise Producer-Ausgabezeit umgedeutet.
    """

    issued_at = int(issued_at_utc_s or 0)
    target_slots = _forecast_target_slot_material(slots)
    topology_revisions = {
        str(slot.get("pv_topology_revision") or "").strip()
        for slot in (slots or [])
        if str(slot.get("pv_topology_status") or "") == "bound"
    }
    topology_revisions.discard("")
    all_slots_bound = bool(slots) and all(
        str(slot.get("pv_topology_status") or "") == "bound"
        for slot in slots
    )
    topology_revision = (
        next(iter(topology_revisions))
        if all_slots_bound and len(topology_revisions) == 1
        else None
    )
    source_composition = _forecast_source_composition(
        models=models,
        resource_models=resource_models,
        model_freshness=model_freshness,
        configured=configured,
    )
    source_composition_revision = _sha256_revision(source_composition)
    # Die Anbieter liefern hier keine belastbar ausgewiesene externe
    # Modell-/Ensemble-Version. Der Hash der Eingabedaten bleibt deshalb
    # ausschließlich in source_composition/model_input_revision gebunden und
    # wird nicht zur Modellrevision umgedeutet.
    model_revision = None
    method_revision = FORECAST_METHOD_REVISION
    postprocessing_revision = _sha256_revision({
        "schema_version": "pv_forecast_postprocessed_payload_v1",
        "value_stage": FORECAST_VALUE_STAGE,
        "slots": slots,
    }) if slots else None
    target_slots_revision = _sha256_revision(target_slots) if target_slots else None

    producer_time_status = "complete" if issued_at > 0 else "EVIDENCE_LIMIT"
    topology_status = (
        "complete"
        if isinstance(topology_revision, str)
        and topology_revision.startswith("sha256:")
        and len(topology_revision) == 71
        else "EVIDENCE_LIMIT"
    )
    model_status = "EVIDENCE_LIMIT"
    method_status = "complete" if method_revision else "EVIDENCE_LIMIT"
    postprocessing_status = (
        "complete" if postprocessing_revision else "EVIDENCE_LIMIT"
    )
    source_status = (
        "complete" if source_composition_revision else "EVIDENCE_LIMIT"
    )
    target_status = "complete" if target_slots_revision else "EVIDENCE_LIMIT"
    status = (
        "complete"
        if all(
            item == "complete"
            for item in (
                producer_time_status,
                topology_status,
                model_status,
                method_status,
                postprocessing_status,
                source_status,
                target_status,
            )
        )
        else "EVIDENCE_LIMIT"
    )
    material = {
        "schema_version": FORECAST_ISSUE_SCHEMA,
        "status": status,
        "producer": "pv_forecast_service",
        "producer_issued_at_utc_s": issued_at if issued_at > 0 else None,
        "producer_issue_time_basis": FORECAST_PRODUCER_TIME_BASIS,
        "producer_issue_time_status": producer_time_status,
        "model_revision": model_revision,
        "model_revision_status": model_status,
        "method_revision": method_revision,
        "method_revision_status": method_status,
        "postprocessing_revision": postprocessing_revision,
        "postprocessing_revision_status": postprocessing_status,
        "topology_revision": topology_revision,
        "topology_revision_status": topology_status,
        "source_composition": source_composition,
        "source_composition_revision": source_composition_revision,
        "source_composition_status": source_status,
        "value_stage": FORECAST_VALUE_STAGE,
        "distribution_type": FORECAST_DISTRIBUTION_TYPE,
        "declared_quantile": None,
        "quantile_convention": None,
        "target_slot_count": len(target_slots),
        "target_slot_start_utc_s": (
            target_slots[0]["slot_start_utc_s"] if target_slots else None
        ),
        "target_slot_end_utc_s": (
            target_slots[-1]["slot_end_utc_s"] if target_slots else None
        ),
        "target_slots_revision": target_slots_revision,
        "target_slots_status": target_status,
        "control_effect": False,
        "configuration_writes": False,
        "automatic_model_selection": False,
        "decision_use_allowed": False,
    }
    return {
        **material,
        "issue_id": _sha256_revision(material),
    }


def _slots_energy_kwh(slots, field="predicted_kwh"):
    total = 0.0
    for slot in slots or []:
        total += _slot_kw(slot, field) * 0.25
    return total


def _daily_forecast_totals(slots):
    """Summiert Tages-/Restprognosen getrennt nach Roh-, Bias- und Anzeige-Wert."""
    by_day = {}
    for slot in slots or []:
        try:
            day = datetime.fromtimestamp(slot["start_timestamp"] / 1000).strftime("%Y-%m-%d")
        except Exception:
            continue
        entry = by_day.setdefault(day, {
            "day": day,
            "slots": 0,
            "raw_kwh": 0.0,
            "bias_corrected_kwh": 0.0,
            "displayed_kwh": 0.0,
            "predicted_kwh": 0.0,
        })
        entry["slots"] += 1
        entry["raw_kwh"] += _slot_kw(slot, "raw_predicted_kwh", "predicted_kwh") * 0.25
        entry["bias_corrected_kwh"] += _slot_kw(slot, "bias_corrected_kwh", "predicted_kwh") * 0.25
        entry["displayed_kwh"] += _slot_kw(slot, "displayed_predicted_kwh", "predicted_kwh") * 0.25
        entry["predicted_kwh"] += _slot_kw(slot, "predicted_kwh") * 0.25

    result = []
    for day in sorted(by_day):
        entry = by_day[day]
        for key in ("raw_kwh", "bias_corrected_kwh", "displayed_kwh", "predicted_kwh"):
            entry[key] = round(entry[key], 2)
        result.append(entry)
    return result


def _mark_raw_forecast_slots(slots):
    marked = []
    for slot in slots or []:
        slot = dict(slot)
        raw_kw = _slot_kw(slot, "raw_predicted_kwh", "predicted_kwh")
        slot["raw_predicted_kwh"] = round(raw_kw, 4)
        slot.setdefault("bias_corrected_kwh", round(_slot_kw(slot, "predicted_kwh"), 4))
        marked.append(slot)
    return marked


def _mark_displayed_forecast_slots(slots):
    marked = []
    for slot in slots or []:
        slot = dict(slot)
        slot["displayed_predicted_kwh"] = round(_slot_kw(slot, "predicted_kwh"), 4)
        marked.append(slot)
    return marked

class EnsemblePVForecaster:
    def __init__(self, *, force_resource_refresh=False):
        # Ausschließlich der explizite Einmallauf darf einen bereits belegten
        # M1/M2-Refresh-Backoff übergehen. Solcast-TTL und -Tagesbudget bleiben
        # davon vollständig unberührt.
        self.force_resource_refresh = bool(force_resource_refresh)
        self.config = self._load_config()
        self.v4_config = self._load_v4_config()
        self._model_cache = self._load_model_cache()
        self._model_resource_data = {}
        self._local_model_failure_classes = {}
        self.pv_topology_contract = build_pv_forecast_topology(self.v4_config)
        self.pv_topology_explicit = has_explicit_topology_config(self.v4_config)

        # Favorisiere v4_config für Standortdaten
        self.lat = float(self.v4_config.get('hoehe', self.config.get('hoehe', 48.6)))
        self.lon = float(self.v4_config.get('laenge', self.config.get('laenge', 13.4)))
        def configured_coordinate_present(source, key):
            return bool(
                key in source
                and source.get(key) is not None
                and str(source.get(key)).strip()
            )
        location_explicit = bool(
            configured_coordinate_present(self.v4_config, "hoehe")
            or configured_coordinate_present(self.config, "hoehe")
        ) and bool(
            configured_coordinate_present(self.v4_config, "laenge")
            or configured_coordinate_present(self.config, "laenge")
        )
        self._solar_location_valid = bool(
            location_explicit
            and math.isfinite(self.lat)
            and math.isfinite(self.lon)
            and -66.0 <= self.lat <= 66.0
            and -180.0 <= self.lon <= 180.0
        )
        self._solar_site_revision = (
            _sha256_revision({
                "schema_version": "pv_forecast_site_location_v1",
                "latitude_deg": round(self.lat, 8),
                "longitude_deg": round(self.lon, 8),
            })
            if self._solar_location_valid
            else None
        )
        self._solar_astronomy_revision = (
            _sha256_revision({
                "schema_version": PV_ZERO_SOLAR_WINDOW_SCHEMA,
                "formula": "declination_hour_angle_local_standard_time_v1",
                "day_of_year_denominator": 364.0,
                "timezone_center_longitude_deg": 15.0,
                "solar_guard_s": PV_ZERO_SOLAR_GUARD_S,
                "method_revision": FORECAST_METHOD_REVISION,
                "site_revision": self._solar_site_revision,
            })
            if self._solar_location_valid and FORECAST_METHOD_REVISION
            else None
        )

        # Provider-unabhängige lokale Geometrie.  Explizite Generatorgruppen
        # lösen FC1..3 als Datenmodell ab; die alte Syntax bleibt Lesefallback.
        self.roofs = []
        explicit_groups = configured_generator_groups(self.v4_config)
        if explicit_groups:
            for group in explicit_groups:
                if group.get("invalid"):
                    logger.warning("PV-Generatorgruppe ist ungültig; lokale Teilprognose bleibt Missing.")
                    continue
                for surface in group.get("surfaces", []):
                    roof = dict(surface)
                    roof["resource_key"] = str(group.get("group_id"))
                    self.roofs.append(roof)
        elif not self.pv_topology_explicit:
            for resource_index, key in enumerate(['forecast1', 'forecast2', 'forecast3'], start=1):
                # V4-Config hat Vorrang, Fallback auf alte e3dc.config.txt
                val = self.v4_config.get(key) or self.config.get(key)
                roofs = _parse_roof_config(val)
                if roofs:
                    for roof in roofs:
                        roof["resource_key"] = f"FC{resource_index}"
                    self.roofs.extend(roofs)
                elif val:
                    logger.error(f"Ungültiges Format für Dach: {val}")
        # Die 10-kWp-Notgeometrie ist ausschließlich alter, vertragsloser
        # Fallback. Ein vorhandener, aber ungültiger V4-Vertrag bleibt
        # absichtlich ohne lokale Teilressourcen und damit fail-closed.
        if not self.roofs and not self.pv_topology_explicit:
            self.roofs.append({"tilt": 35, "azimuth": 0, "kwp": 10.0})
        elif not self.roofs:
            logger.warning("Explizite PV-Topologie enthält keine gültigen Flächen; lokale Teilprognose bleibt Missing.")

        # Für Open-Meteo (welches keine Ausrichtung kennt) berechnen wir die Gesamt-kWp
        self.configured_total_kwp = sum(r['kwp'] for r in self.roofs)
        self.total_kwp = self.configured_total_kwp
        self._update_kwp_from_live()

    def _update_kwp_from_live(self):
        live_path = os.path.join(RAMDISK_DIR, "live_data_py.json")
        if os.path.exists(live_path):
            try:
                with open(live_path, 'r') as f:
                    d = json.load(f)
                    installed_kwp_values = []
                    peak_valid = d.get("installed_peak_power_valid")
                    peak_source = str(d.get("installed_peak_power_source") or "rscp").strip()
                    if peak_valid is False:
                        installed_kwp = None
                        logger.warning(
                            "E3DC-PV-Leistung nicht vertrauenswuerdig "
                            f"({peak_source}); behalte konfigurierte "
                            f"{float(getattr(self, 'configured_total_kwp', self.total_kwp) or 0.0):.2f} kWp."
                        )
                    elif "installed_peak_power_w" in d:
                        installed_kwp_values.append(float(d.get("installed_peak_power_w") or 0.0) / 1000.0)
                    if peak_valid is not False and "installed_peak_power_kwp" in d:
                        installed_kwp_values.append(float(d.get("installed_peak_power_kwp") or 0.0))
                    if peak_valid is not False:
                        installed_kwp = max(installed_kwp_values) if installed_kwp_values else None

                    if installed_kwp is not None:
                        live_pv_kw = max(
                            0.0,
                            float(d.get("PV_Power") or 0.0) / 1000.0,
                            float(d.get("Ext_PV_Power") or 0.0) / 1000.0,
                        )
                        configured_kwp = float(getattr(self, "configured_total_kwp", self.total_kwp) or 0.0)
                        if installed_kwp <= 0.0:
                            logger.warning(
                                "E3DC meldet installierte PV-Leistung 0.00 kWp; "
                                f"behalte konfigurierte {configured_kwp:.2f} kWp."
                            )
                        elif live_pv_kw > 0.5 and installed_kwp < live_pv_kw * 0.85:
                            logger.warning(
                                f"E3DC-PV-Leistung {installed_kwp:.2f} kWp ist kleiner als aktuelle "
                                f"PV-Leistung {live_pv_kw:.2f} kW; behalte konfigurierte {configured_kwp:.2f} kWp."
                            )
                        else:
                            self.total_kwp = installed_kwp
                            logger.info(f"PV-Leistung auf reale {self.total_kwp:.2f} kWp angepasst (aus E3DC).")
            except: pass

        # Die Gewichte aus der Zukunft (können später dynamisch vom Evaluator überschrieben werden)
        self.weight_m1 = float(self.config.get('pv_score_m1', 0.4))
        self.weight_m2 = float(self.config.get('pv_score_m2', 0.2))
        self.weight_m3 = float(self.config.get('pv_score_m3', 0.4)) # Solcast

        # Solcast Rooftop Sites (bevorzugt aus v4.json). Der Accountslot ist
        # pro Site explizit; bei fehlendem Mapping bleibt der Legacy-Vertrag erhalten.
        self.solcast_api_key = str(self.v4_config.get('solcast_api_key', self.config.get('solcast_api_key', '')) or '').strip()
        self.solcast_api_key_2 = str(self.v4_config.get('solcast_api_key_2', self.config.get('solcast_api_key_2', '')) or '').strip()
        self.solcast_calls_per_day = self._solcast_calls_per_day("solcast_calls_per_day", SOLCAST_DEFAULT_CALLS_PER_DAY)
        self.solcast_calls_per_day_2 = self._solcast_calls_per_day("solcast_calls_per_day_2", self.solcast_calls_per_day)
        self.solcast_resource_id = str(self.v4_config.get('solcast_resource_id', self.config.get('solcast_resource_id', '')) or '').strip()
        self.solcast_resource_id_2 = str(self.v4_config.get('solcast_resource_id_2', self.config.get('solcast_resource_id_2', '')) or '').strip()
        self.solcast_resource_id_3 = str(self.v4_config.get('solcast_resource_id_3', self.config.get('solcast_resource_id_3', '')) or '').strip()
        self.solcast_resource_id_4 = str(self.v4_config.get('solcast_resource_id_4', self.config.get('solcast_resource_id_4', '')) or '').strip()
        self.solcast_api_keys = {1: self.solcast_api_key, 2: self.solcast_api_key_2}
        self.solcast_slot_limits = {1: self.solcast_calls_per_day, 2: self.solcast_calls_per_day_2}
        self.solcast_configured_sites = []
        self.solcast_sites = []
        self.solcast_mapping_blocked = False
        self.solcast_mapping_reason = ""
        topology_explicit = bool(
            getattr(
                self,
                "pv_topology_explicit",
                has_explicit_topology_config(self.v4_config),
            )
        )
        self.pv_topology_explicit = topology_explicit
        legacy_secondary_slot = 2 if self.solcast_api_key_2 else 1
        explicit_bindings = configured_provider_bindings(self.v4_config)
        pv_topology_contract = self._pv_topology_contract_or_unbound()
        provider_diagnostics = {
            str(item.get("binding_id") or ""): item
            for item in pv_topology_contract.get("provider_bindings", [])
            if isinstance(item, dict) and str(item.get("binding_id") or "")
        }
        if (
            topology_explicit
            and pv_topology_contract.get("provider_status") == "invalid"
        ):
            self.solcast_mapping_blocked = True
            self.solcast_mapping_reason = str(
                pv_topology_contract.get("provider_reason")
                or "BINDING_INVALID"
            )

        def provider_binding_error(binding):
            diagnostic = provider_diagnostics.get(
                str(binding.get("binding_id") or ""),
                {},
            )
            if diagnostic.get("state") != "bound":
                return str(
                    binding.get("invalid")
                    or diagnostic.get("reason")
                    or "BINDING_INVALID"
                )
            return str(binding.get("invalid") or "")

        legacy_duplicate_labels = (
            set()
            if topology_explicit
            else set(legacy_provider_resource_duplicate_keys(self.v4_config))
        )
        if topology_explicit:
            site_specs = [
                {
                    "label": "binding:" + str(binding.get("binding_id") or "missing"),
                    "resource_id": str(binding.get("resource_id") or ""),
                    "account_slot": binding.get("account_slot"),
                    "legacy_slot": legacy_secondary_slot,
                    "resource_keys": list(binding.get("group_ids") or []),
                    "allocations": list(binding.get("allocations") or []),
                    "mapping_source": "explicit_binding",
                    "binding_invalid": provider_binding_error(binding),
                }
                for binding in explicit_bindings
                if str(binding.get("provider") or "") == "solcast"
            ]
        else:
            site_specs = [
                {"label": "FC1", "resource_id": self.solcast_resource_id, "mapping_key": "solcast_api_slot_fc1", "legacy_slot": 1, "resource_keys": ["FC1"], "allocations": [{"group_id": "FC1", "share": 1.0}], "mapping_source": "legacy_default", "binding_invalid": "provider_resource_duplicate" if legacy_duplicate_labels else ""},
                {"label": "FC2", "resource_id": self.solcast_resource_id_2, "mapping_key": "solcast_api_slot_fc2", "legacy_slot": legacy_secondary_slot, "resource_keys": ["FC2"], "allocations": [{"group_id": "FC2", "share": 1.0}], "mapping_source": "legacy_default", "binding_invalid": "provider_resource_duplicate" if legacy_duplicate_labels else ""},
                {"label": "FC3", "resource_id": self.solcast_resource_id_3, "mapping_key": "solcast_api_slot_fc3", "legacy_slot": legacy_secondary_slot, "resource_keys": ["FC3"], "allocations": [{"group_id": "FC3", "share": 1.0}], "mapping_source": "legacy_default", "binding_invalid": "provider_resource_duplicate" if legacy_duplicate_labels else ""},
                {"label": "FC4", "resource_id": self.solcast_resource_id_4, "mapping_key": "solcast_api_slot_fc4", "legacy_slot": legacy_secondary_slot, "resource_keys": ["FC4"], "allocations": [{"group_id": "FC4", "share": 1.0}], "mapping_source": "legacy_default", "binding_invalid": "provider_resource_duplicate" if legacy_duplicate_labels else ""},
            ]
        for spec in site_specs:
            label = str(spec.get("label") or "")
            resource_id = str(spec.get("resource_id") or "")
            if not resource_id:
                continue
            mapping_key = spec.get("mapping_key")
            raw_slot = (
                spec.get("account_slot")
                if topology_explicit
                else self.v4_config.get(mapping_key, self.config.get(mapping_key))
            )
            slot_text = str(raw_slot).strip() if raw_slot is not None else ""
            explicit = bool(slot_text)
            if explicit and slot_text not in {"1", "2"}:
                account_slot = None
                reason = "invalid_account_slot"
            else:
                account_slot = int(slot_text) if explicit else int(spec.get("legacy_slot") or 1)
                reason = str(spec.get("binding_invalid") or "")
            site = {
                "label": label,
                "resource_id": resource_id,
                "account_slot": account_slot,
                "credential_ref": f"solcast_api_key{'_2' if account_slot == 2 else ''}" if account_slot else "",
                "mapping_source": spec.get("mapping_source") if topology_explicit else ("explicit" if explicit else "legacy_default"),
                # Only explicit bindings establish a group association.  A
                # later aggregation refuses multi-group sites as Missing.
                "resource_keys": [str(item) for item in spec.get("resource_keys", []) if str(item)],
                "allocations": [dict(item) for item in spec.get("allocations", []) if isinstance(item, dict)],
            }
            self.solcast_configured_sites.append(site)
            if reason:
                self.solcast_mapping_blocked = True
                self.solcast_mapping_reason = reason
                continue
            if not self.solcast_api_keys.get(account_slot, ""):
                self.solcast_mapping_blocked = True
                self.solcast_mapping_reason = "missing_selected_key"
                continue
            self.solcast_sites.append(site)
        sig_src = json.dumps(
            {
                "schema": SOLCAST_SIGNATURE_SCHEMA,
                "sites": [
                    {
                        "label": site["label"],
                        "resource": site["resource_id"],
                        "account_slot": site["account_slot"],
                        "resource_keys": site.get("resource_keys", []),
                        "allocations": site.get("allocations", []),
                    }
                    for site in self.solcast_configured_sites
                ],
                "topology_revision": self._pv_topology_contract_or_unbound().get("revision"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self.solcast_signature = hashlib.sha256(sig_src.encode("utf-8")).hexdigest() if sig_src else ""
        if not self.solcast_configured_sites:
            state = "disabled_no_sites"
        elif self.solcast_mapping_blocked or len(self.solcast_sites) != len(self.solcast_configured_sites):
            state = "blocked_missing_selected_key" if self.solcast_mapping_reason == "missing_selected_key" else "incomplete_refresh"
        else:
            state = "incomplete_refresh"
        self.solcast_status = self._solcast_status(
            state,
            successful=0,
            failed_labels=[site["label"] for site in self.solcast_configured_sites] if self.solcast_mapping_blocked else [],
        )
        self.solcast_ttl_s = self._solcast_dynamic_ttl_s()

    def _strict_solar_window_hours(self, day_date):
        """Liefert das Producer-Sonnenfenster ohne Default-/Fehlerfallback."""

        if not self._solar_location_valid:
            return None
        try:
            doy = day_date.timetuple().tm_yday
            decl = math.radians(23.45 * math.sin(2.0 * math.pi * (doy - 81) / 364.0))
            lat_r = math.radians(float(self.lat))
            cos_ha = -math.tan(lat_r) * math.tan(decl)
            cos_ha = max(-0.999, min(0.999, cos_ha))
            ha_h = math.degrees(math.acos(cos_ha)) / 15.0
            timezone_center_lon = 15.0
            solar_noon = 12.0 - ((float(self.lon) - timezone_center_lon) / 15.0)
            window = (
                max(3.0, solar_noon - ha_h),
                solar_noon,
                min(22.5, solar_noon + ha_h),
            )
            if not all(math.isfinite(float(value)) for value in window):
                return None
            if not window[0] < window[1] < window[2]:
                return None
            return window
        except Exception:
            return None

    def _solar_window_hours(self, day_date):
        window = self._strict_solar_window_hours(day_date)
        return window if window is not None else (6.0, 12.5, 19.0)

    def _pv_daylight_factor(self, slot_dt):
        sunrise, _solar_noon, sunset = self._solar_window_hours(slot_dt.date())
        hour = slot_dt.hour + slot_dt.minute / 60.0 + slot_dt.second / 3600.0
        if sunrise >= sunset or hour <= sunrise or hour >= sunset:
            return 0.0
        progress = (hour - sunrise) / max(0.1, sunset - sunrise)
        return max(0.0, math.sin(math.pi * progress))

    def _pv_night_zero_evidence(self, slot, topology_slot):
        """Belegt ausschließlich eine vollständig dunkle, echte Nullprojektion."""

        if not (
            self._solar_location_valid
            and isinstance(self._solar_site_revision, str)
            and self._solar_site_revision.startswith("sha256:")
            and isinstance(self._solar_astronomy_revision, str)
            and self._solar_astronomy_revision.startswith("sha256:")
            and isinstance(FORECAST_METHOD_REVISION, str)
            and FORECAST_METHOD_REVISION.startswith("sha256:")
            and isinstance(slot, dict)
            and isinstance(topology_slot, dict)
        ):
            return None
        try:
            start_ms = int(slot.get("start_timestamp"))
            end_ms = int(slot.get("end_timestamp"))
        except (TypeError, ValueError):
            return None
        if not (
            end_ms - start_ms == 900_000
            and start_ms % 900_000 == 0
            and end_ms % 900_000 == 0
        ):
            return None
        midpoint_ms = start_ms + 450_000
        probe_ms = (start_ms, midpoint_ms, end_ms - 1)
        guard_h = PV_ZERO_SOLAR_GUARD_S / 3600.0
        for timestamp_ms in probe_ms:
            local_dt = datetime.fromtimestamp(timestamp_ms / 1000.0)
            window = self._strict_solar_window_hours(local_dt.date())
            if window is None:
                return None
            sunrise, _solar_noon, sunset = window
            local_hour = (
                local_dt.hour
                + local_dt.minute / 60.0
                + local_dt.second / 3600.0
                + local_dt.microsecond / 3_600_000_000.0
            )
            if not (
                local_hour <= sunrise - guard_h
                or local_hour >= sunset + guard_h
            ):
                return None
        midpoint_dt = datetime.fromtimestamp(midpoint_ms / 1000.0)
        if self._pv_daylight_factor(midpoint_dt) != 0.0:
            return None

        def exact_zero(value):
            if isinstance(value, bool) or value is None:
                return False
            try:
                number = float(value)
            except (TypeError, ValueError):
                return False
            return math.isfinite(number) and number == 0.0

        resources = topology_slot.get("resources")
        if not isinstance(resources, list) or not resources:
            return None
        resource_keys = []
        resource_total_w = 0.0
        for resource in resources:
            if not isinstance(resource, dict):
                return None
            resource_key = str(resource.get("resource_key") or "").strip()
            if not resource_key or resource_key in resource_keys:
                return None
            if not exact_zero(resource.get("forecast_w")):
                return None
            resource_keys.append(resource_key)
            resource_total_w += float(resource["forecast_w"])

        topology_revision = str(
            topology_slot.get("topology_revision") or ""
        )
        if not (
            str(topology_slot.get("status") or "") == "bound"
            and topology_slot.get("split_usable") is True
            and str(topology_slot.get("resource_projection_status") or "")
            == "complete"
            and topology_revision.startswith("sha256:")
            and exact_zero(slot.get("predicted_kwh"))
            and exact_zero(topology_slot.get("total_pv_w"))
            and exact_zero(topology_slot.get("e3dc_dc_pv_w"))
            and exact_zero(topology_slot.get("external_ac_pv_w"))
            and exact_zero(topology_slot.get("external_ac_capped_w"))
            and resource_total_w == 0.0
        ):
            return None

        material = {
            "schema_version": PV_ZERO_EVIDENCE_SCHEMA,
            "status": "COMPLETE",
            "reason": PV_ZERO_EVIDENCE_REASON,
            "slot_start_ts_ms": start_ms,
            "slot_end_ts_ms": end_ms,
            "slot_midpoint_ts_ms": midpoint_ms,
            "full_slot_night": True,
            "daylight_factor_midpoint": 0.0,
            "solar_guard_s": PV_ZERO_SOLAR_GUARD_S,
            "method_revision": FORECAST_METHOD_REVISION,
            "site_revision": self._solar_site_revision,
            "astronomy_revision": self._solar_astronomy_revision,
            "topology_revision": topology_revision,
            "pv_total_w": 0.0,
            "e3dc_dc_pv_w": 0.0,
            "external_ac_pv_w": 0.0,
            "resource_count": len(resource_keys),
            "resource_total_w": 0.0,
        }
        return {
            **material,
            "evidence_revision": _sha256_revision(material),
        }

    def _solcast_calls_per_day(self, key, default):
        raw = self.v4_config.get(key, self.config.get(key, default))
        try:
            calls = int(float(raw))
        except (TypeError, ValueError):
            calls = int(default)
        return max(1, min(500, calls))

    @staticmethod
    def _provider_failure_class(error):
        if isinstance(error, urllib.error.HTTPError):
            return "http_429" if int(error.code) == 429 else "http_error"
        error_text = str(error).lower()
        if isinstance(error, (TimeoutError, socket.timeout)) or "timed out" in error_text:
            return "timeout"
        if isinstance(error, urllib.error.URLError):
            return "transport_error"
        if isinstance(error, (ValueError, KeyError, TypeError)):
            return "parse_error"
        return "fetch_error"

    @staticmethod
    def _dominant_failure_class(failure_classes):
        priority = (
            "http_429",
            "timeout",
            "http_error",
            "transport_error",
            "parse_error",
            "fetch_error",
        )
        present = {str(item) for item in (failure_classes or [])}
        return next((item for item in priority if item in present), "fetch_failed")

    def _solcast_status(
        self,
        state,
        successful=0,
        failed_labels=None,
        failure_classes=None,
    ):
        configured = list(getattr(self, "solcast_configured_sites", []) or [])
        raw_failed_labels = {
            str(label)
            for label in (failed_labels or [])
            if str(label)
        }
        allowed_failure_classes = {
            "fetch_error",
            "http_429",
            "http_error",
            "mapping_blocked",
            "parse_error",
            "timeout",
            "transport_error",
        }
        return {
            "schema": "solcast_status_v2",
            "state": str(state),
            "configured_sites": len(configured),
            "successful_sites": int(successful),
            # Legacy-Labels bleiben kompatibel. Explizite Binding-IDs werden
            # nicht veröffentlicht; ihr Fehlerumfang bleibt als Anzahl und
            # nichtgeheime Klasse vollständig diagnostizierbar.
            "failed_labels": sorted(
                label
                for label in raw_failed_labels
                if label in {"FC1", "FC2", "FC3", "FC4"}
            ),
            "failed_site_count": len(raw_failed_labels),
            "failure_classes": sorted(
                {
                    str(item)
                    for item in (failure_classes or [])
                    if str(item) in allowed_failure_classes
                }
            ),
            "site_accounts": {
                str(site.get("label")): int(site.get("account_slot"))
                for site in configured
                if site.get("label") in {"FC1", "FC2", "FC3", "FC4"} and site.get("account_slot") in {1, 2}
            },
            "signature": str(getattr(self, "solcast_signature", "")),
        }

    @staticmethod
    def _solcast_credential_id(api_key):
        return hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()

    def _solcast_limit_for_api_key(self, api_key):
        limits = []
        if api_key and api_key == self.solcast_api_key:
            limits.append(self.solcast_calls_per_day)
        if api_key and api_key == self.solcast_api_key_2:
            limits.append(self.solcast_calls_per_day_2)
        return min(limits) if limits else self.solcast_calls_per_day

    def _solcast_credential_groups(self):
        groups = {}
        api_keys = getattr(self, "solcast_api_keys", {1: self.solcast_api_key, 2: self.solcast_api_key_2})
        slot_limits = getattr(
            self,
            "solcast_slot_limits",
            {1: self.solcast_calls_per_day, 2: self.solcast_calls_per_day_2},
        )
        for site in self.solcast_sites:
            account_slot = int(site["account_slot"])
            api_key = api_keys.get(account_slot, "")
            if not api_key:
                continue
            credential_id = self._solcast_credential_id(api_key)
            group = groups.setdefault(
                credential_id,
                {"api_key": api_key, "site_count": 0, "limits": []},
            )
            group["site_count"] += 1
            group["limits"].append(int(slot_limits.get(account_slot, SOLCAST_DEFAULT_CALLS_PER_DAY)))
        for group in groups.values():
            group["calls_per_day"] = min(group.pop("limits") or [SOLCAST_DEFAULT_CALLS_PER_DAY])
        return groups

    def _solcast_dynamic_ttl_s(self):
        """Hält jede reale Credentialidentität unter ihrem Tagesbudget."""
        if not self.solcast_sites:
            return MODEL_TTL["m3"]
        credential_groups = self._solcast_credential_groups()
        ttl_requirements = []
        for group in credential_groups.values():
            site_count = int(group["site_count"])
            calls_per_day = int(group["calls_per_day"])
            ttl_requirements.append(math.ceil((24 * 60 * 60 * site_count) / calls_per_day))
        min_ttl_for_quota = max(ttl_requirements or [MODEL_TTL["m3"]])
        # Auf 15-Minuten-Grenzen aufrunden, damit Restarts die Tagesquote nicht langsam überziehen.
        quota_safe_ttl = int(math.ceil(min_ttl_for_quota / 900.0) * 900)
        ttl = max(SOLCAST_MIN_TTL_S, quota_safe_ttl)
        if ttl != MODEL_TTL["m3"] or len(credential_groups) > 1:
            logger.info(
                "Model 3 (Solcast): %d Site(s) auf %d Credential(s), Tagesbudget %s -> TTL %.2fh."
                % (
                    len(self.solcast_sites),
                    len(credential_groups),
                    ",".join(str(group["calls_per_day"]) for group in credential_groups.values()),
                    ttl / 3600.0,
                )
            )
        return ttl

    def _load_model_cache(self):
        """Laedt zwischengespeicherte Modell-Daten (Zeitstempel + Daten pro Modell)."""
        if os.path.exists(MODEL_CACHE_FILE):
            try:
                with open(MODEL_CACHE_FILE, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_model_cache(self):
        """Persistiert den Model-Cache atomar auf Disk (überlebt Neustarts)."""
        directory = os.path.dirname(MODEL_CACHE_FILE) or "."
        tmp_path = ""
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix="forecast_model_cache.", suffix=".tmp", dir=directory)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self._model_cache, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, MODEL_CACHE_FILE)
            tmp_path = ""
            try:
                dir_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception as e:
            logger.warning(f"Konnte Model-Cache nicht speichern: {e}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _trusted_solcast_cache_entry(self):
        cached = self._model_cache.get("m3", {})
        if not isinstance(cached, dict):
            return None
        if int(cached.get("schema_version") or 0) != SOLCAST_CACHE_SCHEMA_VERSION:
            return None
        if cached.get("completeness") != "complete":
            return None
        if cached.get("sig") != getattr(self, "solcast_signature", ""):
            return None
        data = cached.get("data")
        if not isinstance(data, dict) or not data:
            return None
        return cached

    def _model_resource_payload_state(self, resource_data, model_data=None):
        """Prüft, ob neutrale Ressourcenwerte exakt zur lokalen Topologie passen."""

        required = tuple(topology_resource_keys(self._pv_topology_contract_or_unbound()))
        if not required:
            return False, "topology_resources_missing"
        if not isinstance(resource_data, dict) or not resource_data:
            return False, "resource_data_missing"
        actual = {str(key) for key in resource_data}
        if actual != set(required):
            return False, "resource_keys_mismatch"
        if any(
            not isinstance(resource_data.get(key), dict)
            or not resource_data.get(key)
            for key in required
        ):
            return False, "resource_values_missing"
        if model_data is not None:
            if not isinstance(model_data, dict) or not model_data:
                return False, "total_data_missing"
            total_timestamps = {str(timestamp) for timestamp in model_data}
            if any(
                not total_timestamps.issubset(
                    {str(timestamp) for timestamp in resource_data[key]}
                )
                for key in required
            ):
                return False, "resource_timestamps_mismatch"
        return True, "ok"

    def _model_resource_cache_state(self, model_id: str, cached=None):
        """Bindet Ressourcendaten an Revision, Schema und erwartete Gruppen."""

        if cached is None:
            cached = (
                self._trusted_solcast_cache_entry()
                if model_id == "m3"
                else self._model_cache.get(model_id, {})
            )
        if not isinstance(cached, dict):
            return False, f"{model_id}_cache_missing"
        current_revision = self._pv_topology_contract_or_unbound().get("revision")
        if not current_revision:
            return False, f"{model_id}_topology_revision_missing"
        if cached.get("topology_revision") != current_revision:
            return False, f"{model_id}_topology_revision_mismatch"
        if cached.get("resource_schema") != MODEL_RESOURCE_SCHEMA:
            return False, f"{model_id}_resource_schema_mismatch"
        compatible, reason = self._model_resource_payload_state(
            cached.get("resource_data"),
            cached.get("data"),
        )
        if not compatible:
            return False, f"{model_id}_{reason}"
        return True, f"{model_id}_resource_cache_compatible"

    def _local_model_refresh_backoff_active(self, model_id: str, cached) -> bool:
        """Verhindert API-Loops nach einem belegten Fehlversuch mit Totalfallback."""

        import time as _t

        if model_id not in {"m1", "m2"} or not isinstance(cached, dict):
            return False
        if not isinstance(cached.get("data"), dict) or not cached.get("data"):
            return False
        current_revision = self._pv_topology_contract_or_unbound().get("revision")
        if cached.get("resource_refresh_attempt_revision") != current_revision:
            return False
        if cached.get("resource_refresh_attempt_schema") != MODEL_RESOURCE_SCHEMA:
            return False
        try:
            age = _t.time() - float(cached.get("resource_refresh_attempt_ts"))
        except (TypeError, ValueError):
            return False
        return 0.0 <= age < float(MODEL_TTL.get(model_id, 3600))

    def _mark_local_model_refresh_failure(self, model_id: str, reason: str):
        """Behält nur den alten Totalcache und entzieht jedem Split die Frische."""

        import time as _t

        if model_id not in {"m1", "m2"}:
            return
        cached = self._model_cache.get(model_id, {})
        if not isinstance(cached, dict):
            return
        data = cached.get("data")
        if not isinstance(data, dict) or not data:
            return
        entry = dict(cached)
        for key in ("resource_data", "resource_schema", "topology_revision"):
            entry.pop(key, None)
        entry["resource_refresh_attempt_ts"] = _t.time()
        entry["resource_refresh_attempt_revision"] = (
            self._pv_topology_contract_or_unbound().get("revision")
        )
        entry["resource_refresh_attempt_schema"] = MODEL_RESOURCE_SCHEMA
        entry["resource_refresh_attempt_result"] = str(reason or "fetch_failed")
        self._model_cache[model_id] = entry
        self._save_model_cache()

    def _model_is_stale(self, model_id: str) -> bool:
        """Gibt True zurueck wenn das Modell neu abgerufen werden muss."""
        import time as _t
        cached = self._model_cache.get(model_id, {})
        refresh_reason = ""
        if model_id == "m3":
            if not getattr(self, "solcast_configured_sites", []):
                self.solcast_status = self._solcast_status("disabled_no_sites")
                return False
            if getattr(self, "solcast_mapping_blocked", False):
                return True
            cached = self._trusted_solcast_cache_entry()
            if cached is None:
                logger.info("Modell m3: Kein vollständig gebundener Solcast-Cache; Abruf erforderlich.")
                return True
        elif model_id in {"m1", "m2"}:
            compatible, resource_reason = self._model_resource_cache_state(
                model_id,
                cached,
            )
            if not compatible:
                refresh_reason = resource_reason
        last_ts = cached.get('ts', 0)
        ttl = getattr(self, "solcast_ttl_s", MODEL_TTL.get(model_id, 3600)) if model_id == "m3" else MODEL_TTL.get(model_id, 3600)
        try:
            age = _t.time() - float(last_ts)
        except (TypeError, ValueError):
            age = float(ttl)
        if age < 0.0 or age >= ttl:
            refresh_reason = refresh_reason or "cache_ttl_expired"
        if refresh_reason:
            force_local_refresh = bool(
                model_id in {"m1", "m2"}
                and getattr(self, "force_resource_refresh", False)
            )
            if (
                self._local_model_refresh_backoff_active(model_id, cached)
                and not force_local_refresh
            ):
                logger.warning(
                    "Modell %s: Refresh nach %s zuletzt fehlgeschlagen; "
                    "behalte Totalfallback bis zum nächsten Retryfenster.",
                    model_id,
                    refresh_reason,
                )
                return False
            if force_local_refresh:
                logger.info(
                    "Modell %s: expliziter Einmallauf übergeht den lokalen "
                    "Ressourcen-Refresh-Backoff (%s).",
                    model_id,
                    refresh_reason,
                )
            logger.info(f"Modell {model_id}: Abruf noetig (Alter {age/60:.0f}min, TTL {ttl/60:.0f}min).")
            return True
        logger.info(f"Modell {model_id}: Cache gültig ({age/60:.0f}min alt, TTL {ttl/60:.0f}min) - überspringe API Call.")
        if model_id == "m3":
            self.solcast_status = self._solcast_status(
                "complete_cached",
                successful=int(cached.get("successful_sites") or len(self.solcast_configured_sites)),
            )
        return False

    def _model_split_freshness(self, model_id: str):
        """Liefert eine nach TTL und Provenienz belegte Modellfrische."""

        import time as _t
        if self._pv_topology_contract_or_unbound().get("split_usable") is not True:
            return {"fresh": False, "source": f"{model_id}_topology_split_unusable"}
        cached = self._model_cache.get(model_id, {})
        ttl = MODEL_TTL.get(model_id, 3600)
        if model_id == "m3":
            state = str((getattr(self, "solcast_status", {}) or {}).get("state") or "")
            if state == "stale_complete_fallback":
                return {"fresh": False, "source": "m3_stale_complete_fallback"}
            cached = self._trusted_solcast_cache_entry()
            ttl = getattr(self, "solcast_ttl_s", MODEL_TTL.get(model_id, 3600))
            if state not in {"complete_fresh", "complete_cached"}:
                return {"fresh": False, "source": "m3_provenance_" + (state or "unknown")}
        compatible, resource_reason = self._model_resource_cache_state(
            model_id,
            cached,
        )
        if not compatible:
            return {"fresh": False, "source": resource_reason}
        if not isinstance(cached, dict) or not cached.get("data"):
            return {"fresh": False, "source": f"{model_id}_cache_missing"}
        try:
            age = _t.time() - float(cached.get("ts"))
        except (TypeError, ValueError):
            return {"fresh": False, "source": f"{model_id}_cache_timestamp_unknown"}
        if age < 0.0 or age >= float(ttl):
            return {"fresh": False, "source": f"{model_id}_cache_stale"}
        return {"fresh": True, "source": f"{model_id}_cache_within_ttl"}

    def _get_model_data(self, model_id: str):
        """Gibt gecachte Modell-Daten zurueck (dict ts->kwh), oder leeres dict."""
        if model_id == "m3":
            cached = self._trusted_solcast_cache_entry()
            return dict(cached.get("data", {})) if cached else {}
        return self._model_cache.get(model_id, {}).get('data', {})

    def _pv_topology_contract_or_unbound(self):
        """Liefert den Vertrag ohne ihn an Fallback-Teilobjekten zu erfinden."""

        contract = getattr(self, "pv_topology_contract", None)
        if isinstance(contract, dict):
            return contract
        return {
            "schema_version": "pv_forecast_topology_v1",
            "status": "topology_unbound",
            "reason": "TOPOLOGY_CONTRACT_MISSING",
            "revision": None,
            "resources": [],
            "split_usable": False,
        }

    def _record_model_resource_data(self, model_id, resource_data):
        if not isinstance(getattr(self, "_model_resource_data", None), dict):
            self._model_resource_data = {}
        self._model_resource_data[str(model_id)] = dict(resource_data or {})

    def _get_model_resource_data(self, model_id: str):
        """Liefert neutrale FC-Beiträge; alte Caches bleiben ohne Splitwirkung."""
        cached = self._trusted_solcast_cache_entry() if model_id == "m3" else self._model_cache.get(model_id, {})
        compatible, _reason = self._model_resource_cache_state(model_id, cached)
        if not compatible:
            return {}
        data = cached.get("resource_data", {}) if isinstance(cached, dict) else {}
        allowed_resource_keys = tuple(topology_resource_keys(self._pv_topology_contract_or_unbound()))
        return {
            label: dict(data[label])
            for label in allowed_resource_keys
        }

    def _set_model_data(self, model_id: str, data: dict):
        """Speichert Modell-Daten in den Cache."""
        import time as _t
        if data:  # Nur bei echten Daten cachen, nicht bei leeren Ergebnissen
            entry = {'ts': _t.time(), 'data': data}
            resource_data = getattr(self, "_model_resource_data", {}).get(model_id, {})
            resource_compatible, resource_reason = (
                self._model_resource_payload_state(resource_data, data)
            )
            if resource_compatible:
                entry["resource_data"] = resource_data
                entry["resource_schema"] = MODEL_RESOURCE_SCHEMA
                entry["topology_revision"] = self._pv_topology_contract_or_unbound().get("revision")
            elif model_id in {"m1", "m2"}:
                entry["resource_refresh_attempt_ts"] = _t.time()
                entry["resource_refresh_attempt_revision"] = (
                    self._pv_topology_contract_or_unbound().get("revision")
                )
                entry["resource_refresh_attempt_schema"] = MODEL_RESOURCE_SCHEMA
                entry["resource_refresh_attempt_result"] = resource_reason
            if model_id == "m3":
                if self.solcast_status.get("state") != "complete_fresh":
                    return
                if self.solcast_status.get("successful_sites") != len(self.solcast_configured_sites):
                    return
                entry["schema_version"] = SOLCAST_CACHE_SCHEMA_VERSION
                entry["completeness"] = "complete"
                entry["sig"] = getattr(self, "solcast_signature", "")
                entry["configured_sites"] = len(self.solcast_configured_sites)
                entry["successful_sites"] = len(self.solcast_configured_sites)
            self._model_cache[model_id] = entry
            self._save_model_cache()

    def _load_v4_config(self):
        v4_path = "/var/www/html/data/e3dc_v4.json"
        if os.path.exists(v4_path):
            try:
                with open(v4_path, 'r') as f:
                    return json.load(f)
            except: pass
        return {}

    def _load_config(self):
        conf = {}
        candidates = list(_legacy_config_files())
        if CONFIG_FILE and CONFIG_FILE not in candidates:
            candidates.insert(0, CONFIG_FILE)
        for config_file in candidates:
            if not os.path.isfile(config_file):
                continue
            with open(config_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('#') or '=' not in line: continue
                    k, v = line.split('=', 1)
                    conf[k.strip().lower()] = v.strip()
            break
        return conf

    def _write_weather_alerts(self, payload):
        tmp_path = None
        try:
            payload["fetched_at"] = datetime.now().isoformat(timespec='seconds')
            payload["lat"] = round(float(self.lat), 6)
            payload["lon"] = round(float(self.lon), 6)
            directory = os.path.dirname(WEATHER_ALERTS_OUTPUT) or "."
            fd, tmp_path = tempfile.mkstemp(
                prefix="weather_alerts.",
                suffix=".tmp",
                dir=directory,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
            try:
                os.chmod(tmp_path, 0o664)
            except OSError:
                pass
            os.replace(tmp_path, WEATHER_ALERTS_OUTPUT)
            tmp_path = None
            logger.info(
                "Wetterwarnungen gespeichert: active=%s alerts=%s risk=%s"
                % (payload.get("active"), len(payload.get("alerts") or []), (payload.get("risk") or {}).get("level"))
            )
        except Exception as e:
            logger.warning(f"Wetterwarnungen konnten nicht gespeichert werden: {e}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _parse_cap_datetime(self, value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    def _dt_epoch(self, dt_obj):
        try:
            return int(dt_obj.timestamp()) if dt_obj else None
        except Exception:
            return None

    def _severity_level(self, severity):
        return {
            "minor": 1,
            "moderate": 2,
            "severe": 3,
            "extreme": 4,
        }.get(str(severity or "").strip().lower(), 0)

    def _point_in_polygon(self, lat, lon, polygon_text):
        points = []
        for token in str(polygon_text or "").replace("\n", " ").split():
            if "," not in token:
                continue
            try:
                p_lat, p_lon = token.split(",", 1)
                points.append((float(p_lon), float(p_lat)))
            except Exception:
                continue
        if len(points) < 3:
            return False

        x, y = float(lon), float(lat)
        inside = False
        j = len(points) - 1
        for i in range(len(points)):
            xi, yi = points[i]
            xj, yj = points[j]
            crosses = ((yi > y) != (yj > y))
            if crosses:
                x_at_y = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
                if x < x_at_y:
                    inside = not inside
            j = i
        return inside

    def _cap_child_text(self, node, tag, ns):
        value = node.findtext(f"cap:{tag}", default="", namespaces=ns)
        return value.strip() if isinstance(value, str) else value

    def _cap_pairs(self, parent, pair_tag, ns):
        pairs = {}
        for item in parent.findall(f"cap:{pair_tag}", ns):
            name = self._cap_child_text(item, "valueName", ns)
            value = self._cap_child_text(item, "value", ns)
            if name:
                pairs[name] = value
        return pairs

    def _fetch_dwd_cap_alerts_for_location(self):
        req = urllib.request.Request(DWD_CAP_WARNINGS_URL, headers={'User-Agent': 'E3DC-Control-V4'})
        with urllib.request.urlopen(req, timeout=15) as response:
            zip_bytes = response.read()

        alerts = []
        now_ts = datetime.now().timestamp()
        ns = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            for entry in archive.infolist():
                if not entry.filename.lower().endswith(".xml"):
                    continue
                try:
                    root = ET.fromstring(archive.read(entry))
                except Exception:
                    continue

                infos = root.findall("cap:info", ns)
                if not infos:
                    continue
                info = None
                for candidate in infos:
                    lang = self._cap_child_text(candidate, "language", ns).lower()
                    if lang.startswith("de"):
                        info = candidate
                        break
                if info is None:
                    info = infos[0]

                polygons = []
                region = ""
                for area in info.findall("cap:area", ns):
                    region = region or self._cap_child_text(area, "areaDesc", ns)
                    polygons.extend([
                        p.text or "" for p in area.findall("cap:polygon", ns)
                        if (p.text or "").strip()
                    ])

                if not polygons or not any(self._point_in_polygon(self.lat, self.lon, poly) for poly in polygons):
                    continue

                event = self._cap_child_text(info, "event", ns)
                headline = self._cap_child_text(info, "headline", ns)
                description = self._cap_child_text(info, "description", ns)
                instruction = self._cap_child_text(info, "instruction", ns)
                severity = self._cap_child_text(info, "severity", ns)
                certainty = self._cap_child_text(info, "certainty", ns)
                urgency = self._cap_child_text(info, "urgency", ns)
                onset = self._parse_cap_datetime(self._cap_child_text(info, "onset", ns))
                expires = self._parse_cap_datetime(self._cap_child_text(info, "expires", ns))
                effective = self._parse_cap_datetime(self._cap_child_text(info, "effective", ns))
                start = onset or effective
                start_ts = self._dt_epoch(start)
                end_ts = self._dt_epoch(expires)

                if end_ts is not None and end_ts < now_ts:
                    continue
                if start_ts is not None and start_ts - now_ts > 36 * 3600:
                    continue

                event_codes = self._cap_pairs(info, "eventCode", ns)
                parameters = self._cap_pairs(info, "parameter", ns)
                search_text = " ".join([
                    event or "", headline or "", description or "",
                    event_codes.get("GROUP", ""), event_codes.get("II", "")
                ]).upper()
                thunderstorm = "GEWITTER" in search_text or event_codes.get("GROUP", "").upper() in ("THUNDERSTORM", "CONVECTIVE")
                active_now = (start_ts is None or start_ts <= now_ts) and (end_ts is None or now_ts <= end_ts)

                alerts.append({
                    "source": "dwd_cap",
                    "event": event,
                    "headline": headline or event,
                    "description": description,
                    "instruction": instruction,
                    "severity": severity,
                    "level": self._severity_level(severity),
                    "certainty": certainty,
                    "urgency": urgency,
                    "region": region,
                    "start": start.isoformat(timespec='seconds') if start else None,
                    "end": expires.isoformat(timespec='seconds') if expires else None,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "active_now": active_now,
                    "upcoming": bool(start_ts is not None and start_ts > now_ts),
                    "thunderstorm": thunderstorm,
                    "event_codes": event_codes,
                    "parameters": parameters,
                })

        alerts.sort(key=lambda a: (
            0 if a.get("active_now") else 1,
            0 if a.get("thunderstorm") else 1,
            -int(a.get("level") or 0),
            int(a.get("start_ts") or 0),
        ))
        return alerts

    def _fetch_open_meteo_storm_risk(self):
        url = (
            f"https://api.open-meteo.com/v1/dwd-icon"
            f"?latitude={self.lat}&longitude={self.lon}"
            f"&hourly=weather_code,lightning_potential,precipitation,showers,wind_gusts_10m"
            f"&timezone=Europe%2FBerlin&forecast_days=2"
        )
        req = urllib.request.Request(url, headers={'User-Agent': 'E3DC-Control-V4'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())

        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        codes = hourly.get("weather_code", [])
        lpi_values = hourly.get("lightning_potential", [])
        precip_values = hourly.get("precipitation", [])
        shower_values = hourly.get("showers", [])
        gust_values = hourly.get("wind_gusts_10m", [])

        now_ts = datetime.now().timestamp()
        horizon_ts = now_ts + 18 * 3600
        peak = {
            "level": 0,
            "reason": "Keine erhoehte Gewittertendenz im ICON-Kurzfristmodell.",
            "weather_code": None,
            "lpi": 0.0,
            "precip_mm": 0.0,
            "showers_mm": 0.0,
            "wind_gust_kmh": 0.0,
            "time": None,
            "ts": None,
        }
        current_weather = None

        for i, time_str in enumerate(times):
            try:
                ts = datetime.strptime(time_str, "%Y-%m-%dT%H:%M").timestamp()
            except Exception:
                continue
            if ts < now_ts - 3600 or ts > horizon_ts:
                continue

            code = int(codes[i]) if i < len(codes) and codes[i] is not None else None
            lpi = float(lpi_values[i]) if i < len(lpi_values) and lpi_values[i] is not None else 0.0
            precip = float(precip_values[i]) if i < len(precip_values) and precip_values[i] is not None else 0.0
            showers = float(shower_values[i]) if i < len(shower_values) and shower_values[i] is not None else 0.0
            gust = float(gust_values[i]) if i < len(gust_values) and gust_values[i] is not None else 0.0

            level = 0
            reason = "Keine erhoehte Gewittertendenz im ICON-Kurzfristmodell."
            if code in (96, 99) or lpi >= 2000:
                level = 3
                reason = "ICON meldet starkes Gewitterrisiko."
            elif code == 95 or lpi >= 500:
                level = 2
                reason = "ICON meldet Gewittertendenz."
            elif lpi >= 50 or (showers >= 1.0 and gust >= 50):
                level = 1
                reason = "ICON zeigt konvektive Schauer/Boen, PV-Einbruch moeglich."

            sample = {
                "weather_code": code,
                "lpi": round(lpi, 1),
                "precip_mm": round(precip, 2),
                "showers_mm": round(showers, 2),
                "wind_gust_kmh": round(gust, 1),
                "time": time_str,
                "ts": int(ts),
            }
            if current_weather is None or abs(ts - now_ts) < abs(float(current_weather.get("ts", 0)) - now_ts):
                current_weather = sample

            better = level > peak["level"] or (level == peak["level"] and lpi > peak["lpi"])
            if better:
                peak = {
                    "level": level,
                    "active": level > 0,
                    "reason": reason,
                    **sample,
                }

        if int(peak.get("level") or 0) <= 0 and current_weather:
            peak.update(current_weather)
            peak["reason"] = "Keine erhoehte Gewittertendenz; aktueller ICON-Wetterzustand."
            peak["active"] = False

        peak["source"] = "open-meteo/dwd-icon"
        peak["active"] = bool(peak.get("level", 0) > 0)
        return peak

    def update_weather_alerts(self):
        payload = {
            "source": "dwd_cap+open_meteo_dwd_icon",
            "active": False,
            "thunderstorm_active": False,
            "title": "Keine Wetterwarnung",
            "summary": "Keine aktive DWD-Warnung am Standort.",
            "alerts": [],
            "risk": {"source": "open-meteo/dwd-icon", "active": False, "level": 0},
            "errors": [],
        }

        try:
            payload["alerts"] = self._fetch_dwd_cap_alerts_for_location()
        except Exception as e:
            msg = f"DWD CAP Warnungen nicht abrufbar: {e}"
            logger.warning(msg)
            payload["errors"].append(msg)

        try:
            payload["risk"] = self._fetch_open_meteo_storm_risk()
        except Exception as e:
            msg = f"Open-Meteo Gewitterrisiko nicht abrufbar: {e}"
            logger.warning(msg)
            payload["errors"].append(msg)

        alerts = payload.get("alerts") or []
        top_alert = alerts[0] if alerts else None
        risk = payload.get("risk") or {}
        has_dwd = bool(top_alert)
        has_risk = bool(risk.get("active"))
        thunder = any(bool(a.get("thunderstorm")) for a in alerts) or (int(risk.get("level") or 0) >= 2)

        payload["active"] = has_dwd or has_risk
        payload["thunderstorm_active"] = thunder
        payload["highest_level"] = max([int(a.get("level") or 0) for a in alerts] + [int(risk.get("level") or 0)])

        if top_alert:
            label = "Gewitterwarnung" if top_alert.get("thunderstorm") else "Wetterwarnung"
            payload["title"] = f"{label}: {top_alert.get('event') or top_alert.get('headline')}"
            payload["summary"] = top_alert.get("headline") or top_alert.get("description") or payload["title"]
            payload["start"] = top_alert.get("start")
            payload["end"] = top_alert.get("end")
        elif has_risk:
            payload["title"] = "Gewitterrisiko"
            payload["summary"] = risk.get("reason") or "ICON meldet erhoehtes Gewitterrisiko."
            payload["start"] = risk.get("time")
            payload["end"] = None

        self._write_weather_alerts(payload)

    @staticmethod
    def _urlopen_with_bounded_transport_retry(
        url,
        *,
        timeout_s=15,
        attempts=2,
    ):
        """Wiederholt genau einmal nur einen Transportfehler.

        HTTP-Antworten, Provider-Quoten und JSON-/Schemafehler werden nie
        wiederholt. Damit bleibt der Abruf begrenzt und ein Providerfehler
        wird nicht durch eine unkontrollierte Retry-Schleife verstärkt.
        """

        attempts = max(1, min(2, int(attempts)))
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "E3DC-Control-V4"},
                )
                return urllib.request.urlopen(req, timeout=float(timeout_s))
            except urllib.error.HTTPError:
                raise
            except (TimeoutError, socket.timeout, urllib.error.URLError):
                if attempt + 1 >= attempts:
                    raise
                logger.warning(
                    "Provider-Transportfehler; genau ein begrenzter "
                    "Wiederholungsabruf folgt."
                )
        raise RuntimeError("provider_transport_retry_exhausted")

    def fetch_model_1_forecast_solar(self):
        """Holt Prognose von Forecast.Solar (Standard in der PV Community)"""
        parsed_model_total = {}
        parsed_resource_total = {}
        successful_fetches = 0
        failure_classes = []

        for idx, roof in enumerate(self.roofs):
            resource_key = str(roof.get("resource_key") or f"FC{idx + 1}")
            parsed_resource = parsed_resource_total.setdefault(resource_key, {})
            # API Erwartet Azimuth: 0=Süd, -90=Ost
            url = f"https://api.forecast.solar/estimate/{self.lat}/{self.lon}/{roof['tilt']}/{roof['azimuth']}/{roof['kwp']}"
            logger.info(f"Hole Model 1 (Forecast.Solar) Dach {idx+1}")

            try:
                with self._urlopen_with_bounded_transport_retry(url) as response:
                    data = json.loads(response.read().decode())

                    # Format: {"result": {"watts": {"2024-10-10 08:00:00": 1500, ...}}}
                    hourly_watts = data.get('result', {}).get('watts', {})

                    for time_str, watts in hourly_watts.items():
                        # Parse "YYYY-MM-DD HH:MM:SS" -> datetime object
                        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                        unix_h = int(dt.replace(minute=0, second=0).timestamp())
                        # Speichere als kWh fuer diese Stunde (Key IMMER int!)
                        kwh = float(watts) / 1000.0
                        if unix_h in parsed_model_total:
                            parsed_model_total[unix_h] += kwh
                        else:
                            parsed_model_total[unix_h] = kwh
                        parsed_resource[unix_h] = parsed_resource.get(unix_h, 0.0) + kwh
                    successful_fetches += 1
            except Exception as e:
                failure_classes.append(self._provider_failure_class(e))
                logger.error(f"Fehler bei Model 1 Dach {idx+1}: {e}")

        if successful_fetches == len(self.roofs) and parsed_model_total:
            self._local_model_failure_classes.pop("m1", None)
            self._record_model_resource_data("m1", parsed_resource_total)
            return parsed_model_total
        self._local_model_failure_classes["m1"] = self._dominant_failure_class(
            failure_classes
        )
        if successful_fetches > 0:
            logger.warning(
                "Model 1 (Forecast.Solar): unvollständig (%d/%d); "
                "Teilsumme wird verworfen.",
                successful_fetches,
                len(self.roofs),
            )
        return {}

    def fetch_model_2_open_meteo(self):
        """Holt Strahlung UND Temperatur von Open-Meteo (Zweit-Modell + Wetter-Prognose)"""
        # Wir berechnen den PV-Ertrag auf Basis der exakten Dach-Geometrie (global_tilted_irradiance)
        # und nutzen dafuer direkt das Ensemble aus: best_match, icon_d2, ecmwf_ifs025

        parsed_model_total = {}
        parsed_resource_total = {}
        weather_by_ts = {}
        successful_fetches = 0
        failure_classes = []

        for idx, roof in enumerate(self.roofs):
            resource_key = str(roof.get("resource_key") or f"FC{idx + 1}")
            parsed_resource = parsed_resource_total.setdefault(resource_key, {})
            # Open-Meteo nutzt dieselbe Solar-Konvention wie Forecast.Solar:
            # Sued=0, West=90, Ost=-90
            om_azimuth = roof['azimuth']

            url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={self.lat}&longitude={self.lon}"
                f"&hourly=global_tilted_irradiance,temperature_2m"
                f"&tilt={roof['tilt']}&azimuth={om_azimuth}"
                f"&timezone=Europe%2FBerlin&models=best_match,icon_d2,ecmwf_ifs025&forecast_days=4"
            )
            logger.info(f"Hole Model 2 Dach {idx+1} (Open-Meteo Ensemble)")

            try:
                with self._urlopen_with_bounded_transport_retry(url) as response:
                    data = json.loads(response.read().decode())

                    times = data['hourly']['time']

                    # Modelle abrufen (Falls ein Modell fehlt, z.B. icon_d2 ausserhalb Europas, Fallback)
                    rad_best  = data['hourly'].get('global_tilted_irradiance_best_match', [])
                    rad_icon  = data['hourly'].get('global_tilted_irradiance_icon_d2', [])
                    rad_ecmwf = data['hourly'].get('global_tilted_irradiance_ecmwf_ifs025', [])

                    if not rad_best:
                        rad_best = data['hourly'].get('global_tilted_irradiance', [])

                    if not rad_icon: rad_icon = rad_best
                    if not rad_ecmwf: rad_ecmwf = rad_best

                    # Temp fuer ML (nur 1x pro Zeitslot noetig, egal welches Dach)
                    temp_2m = data['hourly'].get('temperature_2m_best_match', data['hourly'].get('temperature_2m', []))

                    for i, time_str in enumerate(times):
                        dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M")
                        unix_h = int(dt.timestamp())

                        # Kombiniere die Modelle zu einem internen Ensemble (50% ICON, 30% ECMWF, 20% Best)
                        # ICON-D2 reicht nur 48h weit
                        v_icon = rad_icon[i] if i < len(rad_icon) and rad_icon[i] is not None else None
                        v_ecmwf = rad_ecmwf[i] if i < len(rad_ecmwf) and rad_ecmwf[i] is not None else None
                        v_best = rad_best[i] if i < len(rad_best) and rad_best[i] is not None else 0.0

                        if v_icon is not None and v_ecmwf is not None:
                            blended_rad = (v_icon * 0.5) + (v_ecmwf * 0.3) + (v_best * 0.2)
                        elif v_ecmwf is not None:
                            blended_rad = (v_ecmwf * 0.6) + (v_best * 0.4)
                        else:
                            blended_rad = v_best

                        # kWp * (GlobalTiltedIrradiance / 1000) * Performance Ratio (0.85 für geneigte Flächen realistischer)
                        est_kwh = (blended_rad / 1000.0) * roof['kwp'] * 0.85

                        if unix_h in parsed_model_total:
                            parsed_model_total[unix_h] += est_kwh
                        else:
                            parsed_model_total[unix_h] = est_kwh
                        parsed_resource[unix_h] = parsed_resource.get(unix_h, 0.0) + est_kwh

                        # Temperatur im ML Cache speichern
                        if idx == 0 and i < len(temp_2m) and temp_2m[i] is not None:
                            weather_by_ts[unix_h] = {
                                "temp_c": round(float(temp_2m[i]), 1),
                                "radiation_wm2": round(float(blended_rad), 1)
                            }

                    successful_fetches += 1

            except Exception as e:
                failure_classes.append(self._provider_failure_class(e))
                logger.error(f"Fehler bei Model 2 (Open-Meteo) Dach {idx+1}: {e}")

        # weather_forecast.json in Ramdisk speichern (fuer ML)
        if weather_by_ts and successful_fetches > 0:
            try:
                weather_forecast_path = os.path.join(RAMDISK_DIR, "weather_forecast.json")
                weather_output = {
                    "fetched_at": datetime.now().isoformat(timespec='seconds'),
                    "source": "open-meteo/icon_ecmwf_ensemble",
                    "slots": len(weather_by_ts),
                    "hourly": weather_by_ts
                }
                with open(weather_forecast_path, 'w') as wf:
                    json.dump(weather_output, wf)
                logger.info(f"Wetter-Prognose (Temp) gespeichert: {len(weather_by_ts)} Stunden-Slots.")
            except Exception: pass

        if successful_fetches == len(self.roofs) and parsed_model_total:
            self._local_model_failure_classes.pop("m2", None)
            self._record_model_resource_data("m2", parsed_resource_total)
            return parsed_model_total
        self._local_model_failure_classes["m2"] = self._dominant_failure_class(
            failure_classes
        )
        if successful_fetches > 0:
            logger.warning(
                "Model 2 (Open-Meteo): unvollständig (%d/%d); "
                "Teilsumme wird verworfen.",
                successful_fetches,
                len(self.roofs),
            )
        return {}


    def fetch_model_3_solcast(self):
        """Holt M3 nur als vollständige Summe aller konfigurierten Sites."""
        if not self.solcast_configured_sites:
            self.solcast_status = self._solcast_status("disabled_no_sites")
            logger.info("Model 3 (Solcast): Übersprungen (keine Site konfiguriert)")
            return {}
        if self.solcast_mapping_blocked or len(self.solcast_sites) != len(self.solcast_configured_sites):
            state = "blocked_missing_selected_key" if self.solcast_mapping_reason == "missing_selected_key" else "incomplete_refresh"
            labels = [site["label"] for site in self.solcast_configured_sites]
            self.solcast_status = self._solcast_status(
                state,
                failed_labels=labels,
                failure_classes=["mapping_blocked"],
            )
            logger.error("Model 3 (Solcast): Accountzuordnung ist nicht ausführbar; kein Abruf.")
            return {}

        parsed_model_total = {}
        parsed_resource_total = {}
        successful_fetches = 0
        failed_labels = []
        failure_classes = []

        for site in self.solcast_sites:
            label = site["label"]
            resource_id = site["resource_id"]
            api_key = self.solcast_api_keys[int(site["account_slot"])]
            url = f"https://api.solcast.com.au/rooftop_sites/{resource_id}/forecasts?format=json"
            logger.info(f"Hole Model 3 (Solcast {label})")

            try:
                req = urllib.request.Request(
                    url,
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    data = json.loads(response.read().decode())

                    forecasts = data.get('forecasts', [])
                    if not isinstance(forecasts, list) or not forecasts:
                        raise ValueError("forecast_list_missing")
                    parsed_site = {}
                    for entry in forecasts:
                        if not isinstance(entry, dict) or 'period_end' not in entry or 'pv_estimate' not in entry:
                            raise ValueError("forecast_entry_invalid")
                        time_str = entry['period_end'].replace('Z', '+00:00')
                        dt = datetime.fromisoformat(time_str)
                        unix_h = int(dt.replace(minute=0, second=0).timestamp())

                        est_kw = float(entry['pv_estimate'])
                        if not math.isfinite(est_kw) or est_kw < 0.0:
                            raise ValueError("forecast_value_invalid")
                        parsed_site[unix_h] = parsed_site.get(unix_h, 0.0) + est_kw * 0.5
                    if not parsed_site:
                        raise ValueError("forecast_empty")
                    for unix_h, value in parsed_site.items():
                        parsed_model_total[unix_h] = parsed_model_total.get(unix_h, 0.0) + value
                    allocations = [
                        (str(item.get("group_id") or ""), float(item.get("share") or 0.0))
                        for item in site.get("allocations", []) if isinstance(item, dict)
                    ]
                    if allocations and all(group_id and share > 0.0 for group_id, share in allocations):
                        # Many provider resources may feed one group.  A
                        # one-to-many resource is accepted only with explicit
                        # shares, never with positional or inferred routing.
                        for group_key, share in allocations:
                            target = parsed_resource_total.setdefault(group_key, {})
                            for unix_h, value in parsed_site.items():
                                target[unix_h] = target.get(unix_h, 0.0) + value * share
                    else:
                        # Keep the total, but preserve Missingness for the
                        # split instead of fabricating a zero or a mapping.
                        logger.warning("Solcast-Site besitzt keine explizite Gruppenaufteilung; Quellsplit bleibt Missing.")
                    successful_fetches += 1
            except urllib.error.HTTPError as e:
                failed_labels.append(label)
                failure_class = self._provider_failure_class(e)
                failure_classes.append(failure_class)
                if failure_class == "http_429":
                    logger.error(f"Fehler bei Model 3 ({label}): Tagesbudget erschöpft (HTTP 429)")
                else:
                    logger.error(f"Fehler bei Model 3 ({label}): HTTP {int(e.code)}")
            except Exception as error:
                failed_labels.append(label)
                failure_classes.append(self._provider_failure_class(error))
                logger.error(f"Fehler bei Model 3 ({label}): {type(error).__name__}")

        if successful_fetches == len(self.solcast_configured_sites):
            self._record_model_resource_data("m3", parsed_resource_total)
            self.solcast_status = self._solcast_status("complete_fresh", successful=successful_fetches)
            logger.info(
                f"Model 3 (Solcast): {successful_fetches}/{len(self.solcast_sites)} "
                "Site(s) vollständig summiert.",
                # Auch eine schnelle Erholung nach HTTP 429/Providerfehler
                # muss trotz ruhiger Normalprotokollierung sichtbar bleiben.
                extra={"e3dc_no_throttle": True},
            )
            return parsed_model_total
        self.solcast_status = self._solcast_status(
            "incomplete_refresh",
            successful=successful_fetches,
            failed_labels=failed_labels,
            failure_classes=failure_classes,
        )
        logger.warning(
            f"Model 3 (Solcast): unvollständig ({successful_fetches}/{len(self.solcast_sites)}); Teilsumme wird verworfen."
        )
        return {}

    def _resolve_solcast_model(self):
        """Liefert ausschließlich vollständiges frisches oder vertrauenswürdig gecachtes M3."""
        if not self.solcast_configured_sites:
            self.solcast_status = self._solcast_status("disabled_no_sites")
            return {}
        if self.solcast_mapping_blocked:
            return self.fetch_model_3_solcast()
        if not self._model_is_stale("m3"):
            return self._get_model_data("m3")

        stale_complete = self._get_model_data("m3")
        fresh = self.fetch_model_3_solcast()
        if self.solcast_status.get("state") == "complete_fresh":
            self._set_model_data("m3", fresh)
            return fresh
        if stale_complete:
            refresh_status = dict(self.solcast_status)
            self.solcast_status = self._solcast_status(
                "stale_complete_fallback",
                successful=int(refresh_status.get("successful_sites") or 0),
                failed_labels=refresh_status.get("failed_labels", []),
                failure_classes=refresh_status.get("failure_classes", []),
            )
            return stale_complete
        return {}

    def _resolve_local_model(self, model_id: str, fetch_model):
        """Aktualisiert M1/M2, ohne einen brauchbaren Totalcache zu verlieren."""

        cached_total = dict(self._get_model_data(model_id) or {})
        if not self._model_is_stale(model_id):
            return cached_total
        self._record_model_resource_data(model_id, {})
        self._local_model_failure_classes.pop(model_id, None)
        fresh = fetch_model()
        if isinstance(fresh, dict) and fresh:
            self._set_model_data(model_id, fresh)
            return fresh
        failure_class = self._local_model_failure_classes.pop(
            model_id,
            "fetch_failed",
        )
        self._mark_local_model_refresh_failure(model_id, failure_class)
        if cached_total:
            logger.warning(
                "Modell %s: Refresh fehlgeschlagen; alter Totalforecast bleibt "
                "ohne Ressourcen-/Splitwirkung im Ensemble.",
                model_id,
            )
        return cached_total

    def _check_connectivity(self):
        """Schneller Netz-Ping vor API-Calls um sinnlose Fehler-Loops zu vermeiden."""
        import socket
        try:
            socket.setdefaulttimeout(3)
            socket.getaddrinfo("api.open-meteo.com", 80)
            return True
        except Exception:
            return False

    def generate_ensemble(self):
        # Konnektivitaet pruefen bevor alle APIs einzeln scheitern
        if not self._check_connectivity():
            logger.warning("Kein Internetzugang (DNS-Fehler). Behalte bestehende Prognose.")
            return  # Alte pv_forecast.json bleibt unangetastet!

        self.update_weather_alerts()

        # Config-Flags: Modelle koennen per v4.json (Web-UI) deaktiviert werden.
        # WICHTIG: 'openmeteo' aus der ALTEN e3dc.config.txt wird hier IGNORIERT!
        # Der Key dort ist fuer den C++ Kern (verhindert Eba-M Fallback) und hat
        # mit unserem Python-Service NICHTS zu tun.
        # Fuer unseren Service gilt: v4.json 'openmeteo' (default: true = immer aktiv)
        use_openmeteo     = str(self.v4_config.get('openmeteo',     'true')).lower() not in ('false', '0', 'no')
        use_forecastsolar = str(self.v4_config.get('forecastsolar', 'true')).lower() not in ('false', '0', 'no')
        # forecast1/2/3 keys muessen vorhanden (und nicht kommentiert) sein; sonst haben wir keine roofs -> skip
        use_forecastsolar = use_forecastsolar and len(self.roofs) > 0

        # --- Per-Modell TTL Cache: nur abfragen wenn TTL abgelaufen ---
        if use_forecastsolar:
            m1 = self._resolve_local_model(
                "m1",
                self.fetch_model_1_forecast_solar,
            )
        else:
            m1 = self._get_model_data('m1')
            logger.info("Model 1 (Forecast.Solar): Deaktiviert via Config.")

        if use_openmeteo:
            m2 = self._resolve_local_model(
                "m2",
                self.fetch_model_2_open_meteo,
            )
        else:
            m2 = self._get_model_data('m2')
            logger.info("Model 2 (Open-Meteo): Deaktiviert via Config.")

        m3 = self._resolve_solcast_model()

        ensemble_forecast = []

        # Vereine alle vorkommenden Zeitstempel
        # KRITISCH: JSON-Cache-Keys sind STRINGS! -> immer int() casten!
        all_timestamps = set(
            [int(k) for k in m1.keys()] +
            [int(k) for k in m2.keys()] +
            [int(k) for k in m3.keys()]
        )

        # Normalisierte int-key Dicts (verhindert str+int TypeError beim Interpolieren)
        m1 = {int(k): v for k, v in m1.items()}
        m2 = {int(k): v for k, v in m2.items()}
        m3 = {int(k): v for k, v in m3.items()}
        resource_models = {}
        for model_id in ("m1", "m2", "m3"):
            resource_models[model_id] = {
                label: {int(ts): value for ts, value in values.items()}
                for label, values in self._get_model_resource_data(model_id).items()
            }
        model_freshness = {
            model_id: self._model_split_freshness(model_id)
            for model_id in ("m1", "m2", "m3")
        }
        pv_topology_contract = self._pv_topology_contract_or_unbound()
        resource_keys = tuple(topology_resource_keys(pv_topology_contract))

        # Nur Zeitstempel der naechsten 72h behalten (Prognose-Horizont)
        import time as _t2
        now_h = int(_t2.time())
        horizon_96h = now_h + 96 * 3600
        all_timestamps = {ts for ts in all_timestamps if ts <= horizon_96h}

        # Zwischenspeicher fuer stuendliche Werte
        hourly_values = []

        for ts in sorted(all_timestamps):
            val1 = m1.get(ts)
            val2 = m2.get(ts)
            val3 = m3.get(ts)

            # BUG-FIX: Basis-Gewichte aus Config, normiert auf verfuegbare Modelle.
            w1 = self.weight_m1 if val1 is not None else 0.0
            w2 = self.weight_m2 if val2 is not None else 0.0
            w3 = self.weight_m3 if val3 is not None else 0.0
            total_weight = w1 + w2 + w3

            if total_weight == 0:
                continue

            # Basis-Normierung (Summe immer 1.0)
            norm_w1 = w1 / total_weight
            norm_w2 = w2 / total_weight
            norm_w3 = w3 / total_weight

            # --- PHASE A: Horizont-abhaengige Gewichtung ---
            # Grundprinzip (IEA Task 16 / WMO):
            #   0-24h:  M2 (NWP-Ensemble) dominiert - aktuelles Wetter wird korrekt erfasst
            #   24-48h: M1+M2 gleichgewichtig - beide Modelle in ihrem Optimum
            #   48-72h: M1 (Geometrie/Jahreszeit) leicht bevorzugt - NWP verliert Praezision,
            #           M1's astronomische Genauigkeit zahlt sich aus
            # M3 (Solcast): linearer Abfall ab 24h, ab 36h=0 (Free-Tier Horizont)
            hours_ahead = (ts - now_h) / 3600.0
            if hours_ahead <= 24:
                # Kurzfrist: M2 (NWP) dominiert, M1 reduziert
                h_factor_1 = 0.60   # M1 auf 60% seines Basis-Gewichts
                h_factor_2 = 1.40   # M2 auf 140% seines Basis-Gewichts
                h_factor_3 = 1.00   # M3 (Solcast): voll aktiv wenn verfuegbar
            elif hours_ahead <= 48:
                # Mittelfrist: gleichgewichtig, M3 faellt linear ab
                t = (hours_ahead - 24) / 24.0  # 0..1
                h_factor_1 = 0.60 + t * 0.50   # 0.60 -> 1.10
                h_factor_2 = 1.40 - t * 0.40   # 1.40 -> 1.00
                h_factor_3 = max(0.0, 1.0 - (hours_ahead - 24) / 12.0)  # linear 0 ab 36h
            else:
                # Langfrist (48-72h): M1 Geometrie-Vorteil, M2 normal, M3=0
                t = min(1.0, (hours_ahead - 48) / 24.0)  # 0..1
                h_factor_1 = 1.10 + t * 0.20   # 1.10 -> 1.30
                h_factor_2 = 1.00 - t * 0.20   # 1.00 -> 0.80
                h_factor_3 = 0.0

            # Horizont-Faktoren anwenden und renormieren
            hw1 = norm_w1 * h_factor_1
            hw2 = norm_w2 * h_factor_2
            hw3 = norm_w3 * h_factor_3
            h_total = hw1 + hw2 + hw3
            if h_total > 0:
                norm_w1 = hw1 / h_total
                norm_w2 = hw2 / h_total
                norm_w3 = hw3 / h_total

            # --- PHASE B: Wetterklassen-Gewichtung (basiert auf M2-Strahlungswert) ---
            # M2 liefert Global Tilted Irradiance (GTI) als Rohwert in kWh/15min
            # Daraus leiten wir den Wettertyp ab:
            #   Klar  (val2 > 0.30 kWh/h = ~300W/m2): M1 Geometrie-Prazision bevorzugt
            #   Misch (val2 0.08-0.30):                ausgeglichen
            #   Bedeckt (val2 < 0.08 kWh/h):           M2 NWP-Diffusmodell bevorzugt
            # Faktor: Multiplikator auf norm_w1 (Anteil M1), Rest geht an M2
            if val2 is not None and val2 > 0:
                if val2 >= 0.30:     # Klar: M1 bekommt Bonus (geometrische Dachgenauigkeit)
                    wc_factor = 1.25
                elif val2 >= 0.08:   # Mischbewoelkt: neutral
                    wc_factor = 1.00
                else:               # Bedeckt/Nebel: M2 NWP ist besser fuer Diffusstrahlung
                    wc_factor = 0.70
                ww1 = norm_w1 * wc_factor
                ww2 = norm_w2 * (2.0 - wc_factor)  # Spiegelsymmetrisch
                ww3 = norm_w3
                ww_total = ww1 + ww2 + ww3
                if ww_total > 0:
                    norm_w1 = ww1 / ww_total
                    norm_w2 = ww2 / ww_total
                    norm_w3 = ww3 / ww_total

            final_kwh = ((val1 or 0) * norm_w1 + (val2 or 0) * norm_w2 + (val3 or 0) * norm_w3)
            resource_projection_state = _resource_projection_state_for_models(
                timestamp=ts,
                model_values={"m1": val1, "m2": val2, "m3": val3},
                model_weights={"m1": norm_w1, "m2": norm_w2, "m3": norm_w3},
                resource_models=resource_models,
                resource_keys=resource_keys,
            )
            split_freshness = _weighted_split_freshness(
                model_values={"m1": val1, "m2": val2, "m3": val3},
                model_weights={"m1": norm_w1, "m2": norm_w2, "m3": norm_w3},
                model_freshness=model_freshness,
                projection_state=resource_projection_state,
            )
            if not resource_keys:
                split_freshness = {"fresh": False, "source": "source_split_resources_missing"}
            resource_kwh = {}
            if resource_projection_state["status"] == "complete":
                for resource_key in resource_keys:
                    resource_kwh[resource_key] = _weighted_resource_contribution(
                        resource_models=resource_models,
                        resource_key=resource_key,
                        timestamp=ts,
                        model_values={"m1": val1, "m2": val2, "m3": val3},
                        model_weights={"m1": norm_w1, "m2": norm_w2, "m3": norm_w3},
                    )
            hourly_values.append({
                "ts": ts, "kwh": final_kwh, "m1": val1, "m2": val2, "m3": val3,
                "w1_eff": round(norm_w1, 3), "w2_eff": round(norm_w2, 3),
                "hours_ahead": round(hours_ahead, 1),
                "resource_kwh": resource_kwh,
                "resource_projection_state": resource_projection_state,
                "split_freshness": split_freshness,
                "diagnostic_weights": {"m1": norm_w1, "m2": norm_w2, "m3": norm_w3},
            })

        # Weiche Interpolation auf 15-Minuten Blöcke
        for i, hr in enumerate(hourly_values):
            curr_kw = hr["kwh"]
            prev_kw = hourly_values[i-1]["kwh"] if i > 0 else curr_kw
            next_kw = hourly_values[i+1]["kwh"] if i < len(hourly_values)-1 else curr_kw

            for q in range(4):
                if q == 0: slotted_kw = (prev_kw * 0.25) + (curr_kw * 0.75)
                elif q == 1: slotted_kw = (prev_kw * 0.10) + (curr_kw * 0.90)
                elif q == 2: slotted_kw = (next_kw * 0.10) + (curr_kw * 0.90)
                else: slotted_kw = (next_kw * 0.25) + (curr_kw * 0.75)
                uncapped_slotted_kw = max(0.0, slotted_kw)

                start_s = int(hr["ts"]) + q * 900
                daylight_factor = self._pv_daylight_factor(datetime.fromtimestamp(start_s + 450))
                if daylight_factor <= 0.0:
                    slotted_kw = 0.0
                else:
                    slotted_kw = min(
                        max(0.0, slotted_kw),
                        self.total_kwp * PV_CLOUD_EDGE_PEAK_MARGIN * daylight_factor,
                    )

                slotted_resources_w = {}
                for resource_key in resource_keys:
                    if resource_key not in (hr.get("resource_kwh") or {}):
                        continue
                    current_resource = (hr.get("resource_kwh") or {}).get(resource_key)
                    previous_resource = (
                        (hourly_values[i - 1].get("resource_kwh") or {}).get(resource_key, current_resource)
                        if i > 0 else current_resource
                    )
                    next_resource = (
                        (hourly_values[i + 1].get("resource_kwh") or {}).get(resource_key, current_resource)
                        if i < len(hourly_values) - 1 else current_resource
                    )
                    if q == 0:
                        resource_kw = previous_resource * 0.25 + current_resource * 0.75
                    elif q == 1:
                        resource_kw = previous_resource * 0.10 + current_resource * 0.90
                    elif q == 2:
                        resource_kw = next_resource * 0.10 + current_resource * 0.90
                    else:
                        resource_kw = next_resource * 0.25 + current_resource * 0.75
                    if resource_kw is not None:
                        slotted_resources_w[resource_key] = max(0.0, resource_kw * 1000.0)

                adjacent_hr = (
                    hourly_values[i - 1] if q < 2 and i > 0
                    else hourly_values[i + 1] if q >= 2 and i < len(hourly_values) - 1
                    else hr
                )
                slot_projection_state = _merge_resource_projection_states(
                    hr.get("resource_projection_state"),
                    adjacent_hr.get("resource_projection_state"),
                )
                slot_freshness = _merge_split_freshness(
                    hr.get("split_freshness"),
                    adjacent_hr.get("split_freshness"),
                )
                if slot_projection_state["status"] == "complete" and resource_keys:
                    source_total_w = uncapped_slotted_kw * 1000.0
                    resource_total_w = sum(slotted_resources_w.values())
                    source_tolerance_w = max(
                        RESOURCE_PROJECTION_ABS_TOLERANCE_W,
                        source_total_w * RESOURCE_PROJECTION_REL_TOLERANCE,
                    )
                    if abs(source_total_w - resource_total_w) > source_tolerance_w:
                        slot_projection_state = {
                            "status": "unbound",
                            "reason": "RESOURCE_PROJECTION_TOTAL_MISMATCH",
                        }
                    elif resource_total_w > 0.0:
                        # Tageslicht-/physikalische Site-Caps sind explizite
                        # Gesamttransformationen und werden proportional auf
                        # vollständig belegte Ressourcenbeiträge angewandt.
                        site_scale = (slotted_kw * 1000.0) / resource_total_w
                        slotted_resources_w = {
                            key: value * site_scale
                            for key, value in slotted_resources_w.items()
                        }

                start_ms = start_s * 1000
                try:
                    diagnostic_provider_resources = provider_resource_samples(
                        resource_models, resource_keys, hr["ts"], adjacent_hr["ts"],
                        .25 if q in (0, 3) else .10,
                    )
                except Exception:
                    diagnostic_provider_resources = {}
                ensemble_forecast.append({
                    "start_timestamp": start_ms,
                    "end_timestamp": start_ms + (900 * 1000),
                    "predicted_kwh": round(slotted_kw, 4),
                    "m1_raw": hr.get("m1"),
                    "m2_raw": hr.get("m2"),
                    "m3_raw": hr.get("m3"),
                    "pv_diagnostic_daylight_expected": (
                        daylight_factor > 0 if getattr(self, "_solar_location_valid", False) else None
                    ),
                    "pv_diagnostic_provider_resources": diagnostic_provider_resources,
                    "pv_diagnostic_parameters": {
                        "method_revision": FORECAST_METHOD_REVISION,
                        "current_weights": hr["diagnostic_weights"],
                        "adjacent_weights": adjacent_hr["diagnostic_weights"],
                        "interpolation_adjacent_weight": .25 if q in (0, 3) else .10,
                        "solar_cap_factor": daylight_factor,
                        "installed_kwp": self.total_kwp,
                        "provider_freshness": {
                            key: isinstance(value, dict) and value.get("fresh") is True
                            for key, value in model_freshness.items()
                        },
                    },
                    "pv_resource_raw_w": slotted_resources_w,
                    "pv_resource_reference_w": max(0.0, slotted_kw * 1000.0),
                    "pv_resource_projection_status": slot_projection_state["status"],
                    "pv_resource_projection_reason": slot_projection_state["reason"],
                    "pv_forecast_fresh": bool(slot_freshness.get("fresh") is True),
                    "forecast_fresh": bool(slot_freshness.get("fresh") is True),
                    "pv_forecast_freshness_source": str(slot_freshness.get("source") or "model_provenance_unknown"),
                })

        site_signature, site_descriptor = _forecast_site_signature(self.roofs, self.total_kwp)

        # Tages-Bias-Update: einmal pro Tag mit Ist-Ertrag aus DB vs. History-Summe.
        # Wichtig: History enthaelt ab jetzt Rohwert UND sichtbaren Prognosewert.
        _update_daily_bias_from_db(
            site_signature=site_signature,
            site_descriptor=site_descriptor,
        )

        # EWMA-Bias anwenden: Korrigiert systematische Modell-Unterschaetzung/-Ueberschaetzung.
        # Rohwert, bias-korrigierter Wert und spaeter sichtbarer Wert bleiben getrennt.
        ensemble_forecast = _apply_daily_bias_to_forecast(
            ensemble_forecast,
            site_signature=site_signature,
        )
        ensemble_forecast, capped_slots, cap_kw = _cap_physical_pv_peak(
            ensemble_forecast,
            self.total_kwp,
        )
        if capped_slots:
            logger.warning(
                "PV-Prognose physikalisch begrenzt: %d Slot(s) auf %.2f kW "
                "(%.2f kWp + %.0f%% Puffer)."
                % (
                    capped_slots,
                    cap_kw,
                    float(self.total_kwp),
                    (PV_PHYSICAL_PEAK_MARGIN - 1.0) * 100.0,
                )
            )

        ensemble_forecast, daily_caps = _cap_daily_forecast_totals(
            ensemble_forecast,
            installed_kwp=self.total_kwp,
            site_signature=site_signature,
            config=self.v4_config,
        )
        topology_status_counts = {}
        topology_slots = []
        for slot in ensemble_forecast:
            slot = dict(slot)
            slot_total_w = _slot_kw(slot, "predicted_kwh") * 1000.0
            resource_raw_w = slot.pop("pv_resource_raw_w", {})
            diagnostic_raw_resources = dict(resource_raw_w)
            diagnostic_provider_resources = slot.pop("pv_diagnostic_provider_resources", {})
            diagnostic_parameters = slot.pop("pv_diagnostic_parameters", {})
            projection_status = slot.pop("pv_resource_projection_status", "unbound")
            projection_reason = slot.pop(
                "pv_resource_projection_reason",
                "RESOURCE_PROJECTION_INCOMPLETE",
            )
            reference_w = max(0.0, _slot_kw(slot, "pv_resource_reference_w"))
            slot.pop("pv_resource_reference_w", None)
            if projection_status == "complete" and resource_keys:
                resource_total_w = sum(
                    max(0.0, float(resource_raw_w.get(key) or 0.0))
                    for key in resource_keys
                )
                reference_tolerance_w = max(
                    RESOURCE_PROJECTION_ABS_TOLERANCE_W,
                    reference_w * RESOURCE_PROJECTION_REL_TOLERANCE,
                )
                if abs(reference_w - resource_total_w) > reference_tolerance_w:
                    projection_status = "unbound"
                    projection_reason = "RESOURCE_PROJECTION_TOTAL_MISMATCH"
                elif resource_total_w > 0.0:
                    # Bias und Tagescap sind bereits fachlich auf den
                    # Gesamtforecast angewandt. Bei belegter Projektion folgt
                    # der Split proportional; fehlende Beiträge werden nie so
                    # kaschiert.
                    aggregate_scale = slot_total_w / resource_total_w
                    resource_raw_w = {
                        key: max(0.0, float(value or 0.0)) * aggregate_scale
                        for key, value in resource_raw_w.items()
                    }
                elif slot_total_w > reference_tolerance_w:
                    projection_status = "unbound"
                    projection_reason = "RESOURCE_PROJECTION_TOTAL_MISMATCH"
            topology_slot = project_slot_topology(
                slot_total_w,
                resource_raw_w,
                pv_topology_contract,
                projection_status=projection_status,
                projection_reason=projection_reason,
            )
            if topology_slot.get("status") != "bound":
                slot["pv_forecast_fresh"] = False
                slot["forecast_fresh"] = False
                slot["pv_forecast_freshness_source"] = (
                    "topology_" + str(topology_slot.get("reason") or "unbound").lower()
                )
            slot["predicted_kwh"] = round(float(topology_slot.get("total_pv_w") or 0.0) / 1000.0, 4)
            slot["pv_topology_status"] = topology_slot.get("status")
            slot["pv_topology_reason"] = topology_slot.get("reason")
            slot["pv_topology_revision"] = topology_slot.get("topology_revision")
            slot["e3dc_dc_pv_w"] = topology_slot.get("e3dc_dc_pv_w")
            slot["external_ac_pv_w"] = topology_slot.get("external_ac_pv_w")
            slot["pv_resources"] = topology_slot.get("resources", [])
            slot["pv_external_ac_capped_w"] = topology_slot.get("external_ac_capped_w", 0.0)
            slot["pv_resource_projection_status"] = topology_slot.get("resource_projection_status")
            slot["pv_resource_projection_reason"] = topology_slot.get("resource_projection_reason")
            slot["pv_topology_source"] = "resource_forecast_ensemble_v1"
            slot["pv_topology_quality"] = (
                "complete"
                if topology_slot.get("status") == "bound"
                else "missing_or_incoherent_resource_projection"
            )
            try:
                slot["pv_diagnostic_stages"] = build_stage_metadata(
                    slot, pv_topology_contract, diagnostic_raw_resources,
                    diagnostic_provider_resources, diagnostic_parameters,
                )
            except Exception:
                slot["pv_diagnostic_stages"] = None
                slot["pv_diagnostic_stage_status"] = "EVIDENCE_LIMIT"
            night_zero_evidence = self._pv_night_zero_evidence(
                slot,
                topology_slot,
            )
            if night_zero_evidence is not None:
                slot["pv_zero_evidence"] = night_zero_evidence
            status_key = str(topology_slot.get("status") or "unknown")
            topology_status_counts[status_key] = topology_status_counts.get(status_key, 0) + 1
            topology_slots.append(slot)
        ensemble_forecast = topology_slots
        ensemble_forecast = _mark_displayed_forecast_slots(ensemble_forecast)
        full_day_forecast_totals = _daily_forecast_totals(ensemble_forecast)

        # --- History-Buffer: Vergangene Slots fuer Chart-Overlay speichern ---
        # Bevor wir filtern: vergangene Slots in pv_forecast_history.json persistieren.
        # (Rolling 24h Buffer - PHP liest sie fuer die "Prognose vs Realitaet" Linie)
        import time as _t
        now_ms = int(_t.time() * 1000)
        cutoff_24h_ms = now_ms - (24 * 3600 * 1000)
        past_slots = [s for s in ensemble_forecast if s['end_timestamp'] <= now_ms]
        _save_forecast_history(past_slots, cutoff_24h_ms)

        # Im Live-Forecast behalten wir alle Slots des aktuellen Kalendertags (ab 00:00 Uhr)
        # bis zum vollen Mehrtages-Horizont, damit Simulator und Trajektorienberechnung
        # zu jedem Zeitpunkt des Tages eine lückenlose, konsistente Datenbasis vorfinden.
        start_of_today_ms = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        future_forecast = [s for s in ensemble_forecast if s['end_timestamp'] > start_of_today_ms]
        removed = len(ensemble_forecast) - len(future_forecast)
        if removed > 0:
            logger.info(f"Gefiltert: {removed} vergangene Vortages-Slots gesichert in History + entfernt aus Live-Forecast.")

        ensemble_forecast = future_forecast
        remaining_forecast_totals = _daily_forecast_totals(ensemble_forecast)

        # KRITISCH: Niemals eine leere Prognose speichern (wuerde gueltige Daten loeschen!)
        if len(ensemble_forecast) == 0:
            logger.warning(
                f"Alle APIs haben leere Antworten geliefert (M1={len(m1)} M2={len(m2)} M3={len(m3)} Slots). "
                f"Bestehende pv_forecast.json wird NICHT ueberschrieben."
            )
            return

        typed_forecast = []
        for slot in ensemble_forecast:
            slot = dict(slot)
            slot["forecast_value_stage"] = FORECAST_VALUE_STAGE
            slot["forecast_distribution_type"] = FORECAST_DISTRIBUTION_TYPE
            slot["forecast_quantile_level"] = None
            slot["forecast_quantile_convention"] = None
            typed_forecast.append(slot)
        ensemble_forecast = typed_forecast
        producer_issued_at_utc_s = int(time.time())
        forecast_issue_contract = build_forecast_issue_contract(
            ensemble_forecast,
            issued_at_utc_s=producer_issued_at_utc_s,
            models={"m1": m1, "m2": m2, "m3": m3},
            resource_models=resource_models,
            model_freshness=model_freshness,
            configured={
                "m1": use_forecastsolar,
                "m2": use_openmeteo,
                "m3": bool(self.solcast_configured_sites),
            },
        )
        issue_id = forecast_issue_contract["issue_id"]
        ensemble_forecast = [
            {
                **slot,
                "forecast_issue_id": issue_id,
                **(
                    {"forecast_issue_contract": forecast_issue_contract}
                    if index == 0
                    else {}
                ),
            }
            for index, slot in enumerate(ensemble_forecast)
        ]

        # Speichern in die Ramdisk (nur bei echten Daten)
        try:
            fetched_at = datetime.now().isoformat(timespec='seconds')  # datetime = Klasse
            output = {
                "fetched_at": fetched_at,
                "slots": len(ensemble_forecast),
                "models_ok": {"m1": len(m1) > 0, "m2": len(m2) > 0, "m3": len(m3) > 0},
            }
            # Rueckwaertskompatibel: Als flache Liste speichern (wie bisher gelesen)
            _atomic_write_json(FORECAST_OUTPUT, ensemble_forecast)
            # Zusaetzlich Metadaten in separate Datei (fuer Dashboard/Debug)
            meta_path = FORECAST_OUTPUT.replace('.json', '_meta.json')
            # Lade aktuellen Bias-Status fuer Meta-Ausgabe
            bias_info = _get_current_bias_info(
                site_signature=site_signature,
                site_descriptor=site_descriptor,
            )
            with open(meta_path, 'w') as f:
                json.dump({"fetched_at": output["fetched_at"], "slots": output["slots"],
                           "models_ok": output["models_ok"],
                           "solcast": dict(self.solcast_status),
                           "pv_topology": dict(pv_topology_contract),
                           "pv_topology_slot_status_counts": topology_status_counts,
                           "calibration": bias_info,
                           "daily_caps": daily_caps,
                           "forecast_issue_contract": forecast_issue_contract,
                           "forecast_totals": {
                               "schema": "slot_kw_times_0_25h",
                               "full_days": full_day_forecast_totals,
                               "remaining_days": remaining_forecast_totals,
                           },
                           "site_signature": site_signature}, f)

            # Log: effektive Gewichte fuer ersten SONNIGEN Slot und letzten sonnigen Slot
            sunny_hrs = [h for h in hourly_values if (h.get("kwh") or 0) > 0.3]
            first_hr = sunny_hrs[0] if sunny_hrs else hourly_values[0] if hourly_values else {}
            last_hr  = sunny_hrs[-1] if sunny_hrs else hourly_values[-1] if hourly_values else {}
            logger.info(
                f"Ensemble-Prognose: {len(ensemble_forecast)} Slots gespeichert. "
                f"Modelle: M1={'OK' if len(m1)>0 else 'FEHLER'} "
                f"M2={'OK' if len(m2)>0 else 'FEHLER'} "
                f"M3={'OK' if len(m3)>0 else 'Uebersprungen'}"
            )
            logger.info(
                f"Gewichtung (Phase A+B): "
                f"Kurzfrist (0h): M1={first_hr.get('w1_eff','?'):.2f} M2={first_hr.get('w2_eff','?'):.2f} | "
                f"Langfrist ({last_hr.get('hours_ahead','?')}h): M1={last_hr.get('w1_eff','?'):.2f} M2={last_hr.get('w2_eff','?'):.2f}"
            )
        except Exception as e:
            logger.error(f"Konnte Prognose nicht speichern: {e}")




def _save_forecast_history(past_slots: list, cutoff_ms: int):
    """
    Fuegt vergangene Forecast-Slots in pv_forecast_history.json ein (Rolling 24h Buffer).
    PHP liest diese Datei fuer den 'Prognose vs Realitaet' Chart-Overlay.
    """
    history_path = _forecast_history_path()
    try:
        # Vorhandene History laden
        if os.path.exists(history_path):
            with open(history_path, 'r') as f:
                history = json.load(f)
        else:
            history = []

        # Neue Slots mergen. Bestehende Slots werden ergaenzt, damit neue Diagnosefelder
        # wie raw_predicted_kwh/displayed_predicted_kwh nicht einen Tag lang fehlen.
        by_ts = {s['start_timestamp']: s for s in history if 'start_timestamp' in s}
        added = 0
        updated = 0
        for slot in past_slots:
            ts = slot.get('start_timestamp')
            if ts is None:
                continue
            if ts in by_ts:
                merged = dict(by_ts[ts])
                merged.update(slot)
                by_ts[ts] = merged
                updated += 1
            else:
                by_ts[ts] = slot
                added += 1
        history = list(by_ts.values())

        # Alles aelter als 24h entfernen (Rolling Buffer)
        history = [s for s in history if s['end_timestamp'] > cutoff_ms]
        history.sort(key=lambda s: s['start_timestamp'])

        with open(history_path, 'w') as f:
            json.dump(history, f)
        if added > 0 or updated > 0:
            logger.debug(
                f"Forecast-History: {added} neue, {updated} aktualisierte Slots, "
                f"{len(history)} gesamt im 24h-Buffer."
            )
    except Exception as e:
        logger.warning(f"Forecast-History konnte nicht gespeichert werden: {e}")


def _update_daily_bias_from_db(
    site_signature=None,
    site_descriptor=None,
    eval_path=FORECAST_EVAL_FILE,
    db_path=DAILY_STATS_DB_PATH,
    history_path=None,
    now=None,
):
    """
    Selbstlernendes EWMA-Bias-System: Vergleicht den gestrigen Ist-Ertrag (aus e3dc_stats.db)
    mit der gestrigen Prognose (aus pv_forecast_history.json) und aktualisiert
    einen gleitenden Korrekturfaktor per Exponentially Weighted Moving Average (EWMA).

    Wissenschaftlicher Hintergrund:
    - EWMA-Bias-Korrektur ist Standard in der PV-Prognoseliteratur (IEA Task 16, SolarAnywhere)
    - Separate Faktoren pro Jahreszeit verhindern, dass Sommer-Sonnentage den Winter-Bias ueberschreiben
    - Clearsky-Index-Klassifizierung (sunny/mixed/cloudy) trennt systematische Modell-Fehler
      von zufaelligen Wolken-Events (nur sunny-Tage fliessen in den Bias ein)

    Speichert den Bias in pv_forecast_eval.json und wendet ihn beim naechsten
    Forecast-Zyklus via _apply_daily_bias_to_forecast() an.
    """
    try:
        # Nur einmal pro Tag ausfuehren (nach 21 Uhr oder wenn kein Eintrag fuer heute existiert)
        import time as _t
        now = now or datetime.now()
        history_path = history_path or _forecast_history_path()
        today_str = now.strftime('%Y-%m-%d')
        yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')

        previous_signature = None
        if os.path.exists(eval_path):
            try:
                with open(eval_path, 'r') as f:
                    previous_signature = (json.load(f) or {}).get("site_signature")
            except Exception:
                previous_signature = None

        eval_data = _load_forecast_eval(eval_path)
        eval_data, signature_changed = _ensure_eval_site_signature(
            eval_data,
            site_signature=site_signature,
            site_descriptor=site_descriptor,
            now=now,
        )
        signature_initialized = bool(site_signature and previous_signature != site_signature)

        daily_log = eval_data.get("daily_log", [])
        existing_dates = {e.get('date') for e in daily_log}
        if yesterday_str in existing_dates:
            if signature_initialized:
                with open(eval_path, 'w') as f:
                    json.dump(eval_data, f, indent=2)
            return

        # Nicht mehr als einmal pro Stunde laufen
        last_update = eval_data.get("last_update", "")
        if last_update and last_update[:13] == now.strftime('%Y-%m-%dT%H'):
            return

        # Schritt 1: Gestrigen Ist-Ertrag aus DB laden
        import sqlite3
        if not os.path.exists(db_path):
            if signature_initialized or signature_changed:
                with open(eval_path, 'w') as f:
                    json.dump(eval_data, f, indent=2)
            return
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT pv_yield FROM daily_stats WHERE date=? AND pv_yield > 5", (yesterday_str,))
        row = c.fetchone()
        conn.close()
        if not row:
            if signature_initialized or signature_changed:
                with open(eval_path, 'w') as f:
                    json.dump(eval_data, f, indent=2)
            return  # Kein Ertrag oder bewolkter Tag mit < 5 kWh
        actual_kwh = float(row[0])

        # Schritt 2: Gestrige Prognose aus History-Buffer rekonstruieren
        if not os.path.exists(history_path):
            return
        with open(history_path, 'r') as f:
            history = json.load(f)

        # Summiere History-Slots des gestrigen Tages (x0.25 fuer kWh aus kW-Werten pro 15min-Slot)
        yest_slots = [
            s for s in history
            if datetime.fromtimestamp(s['start_timestamp'] / 1000).strftime('%Y-%m-%d') == yesterday_str
        ]
        if not yest_slots:
            return  # Noch keine gestrigen Slots im 24h-Buffer
        raw_forecast_kwh = sum(
            _slot_kw(s, "raw_predicted_kwh", "predicted_kwh") * 0.25
            for s in yest_slots
        )
        bias_corrected_kwh = sum(
            _slot_kw(s, "bias_corrected_kwh", "predicted_kwh") * 0.25
            for s in yest_slots
        )
        visible_forecast_kwh = sum(
            _slot_kw(s, "displayed_predicted_kwh", "predicted_kwh") * 0.25
            for s in yest_slots
        )
        if raw_forecast_kwh < 2.0:
            return  # Zu wenig Prognose-Daten
        if visible_forecast_kwh < 2.0:
            visible_forecast_kwh = bias_corrected_kwh if bias_corrected_kwh >= 2.0 else raw_forecast_kwh

        # Schritt 3: Clearsky-Index berechnen (Klassifizierung des Tages)
        # Theoretischer Max-Ertrag: kWp * Sonnenstunden (Sommertag Mitteleuropa max ~8.5h)
        # Einfache Naeherung: wenn Ist-Ertrag > 85% des Forecast -> sonnig
        bias_raw = actual_kwh / raw_forecast_kwh
        visible_ratio = actual_kwh / visible_forecast_kwh if visible_forecast_kwh >= 2.0 else None
        clearsky_class = "sunny" if bias_raw > 1.10 else ("mixed" if bias_raw > 0.80 else "cloudy")

        # Schritt 4: EWMA-Update pro Jahreszeit
        # Jahreszeiten: Q1=Winter, Q2=Fruehling, Q3=Sommer, Q4=Herbst
        quarter = _quarter_for_date(now)
        season_bias = eval_data.get("seasonal_bias", {})
        old_bias = float(season_bias.get(quarter, 1.0) or 1.0)
        bias_clamped = max(0.70, min(1.50, bias_raw))
        target_bias = bias_clamped
        if visible_ratio is not None and visible_ratio < 0.98:
            # Wenn die sichtbare, bereits korrigierte Prognose zu hoch war, muss der
            # gespeicherte Bias schneller nach unten. Das verhindert einen alten Q2-Bias,
            # der trotz realer Tagesmessung weiter 120+ kWh auf die Anzeige hebt.
            visible_target = max(0.70, min(1.50, old_bias * visible_ratio))
            target_bias = min(target_bias, visible_target)
        alpha = 0.45 if target_bias < old_bias - 0.005 else 0.15

        # Nur Sunny-Tage beeinflussen den Bias (bei Wolkentagen ist die Abweichung zufaellig!)
        if clearsky_class != "cloudy":
            new_bias = alpha * target_bias + (1.0 - alpha) * old_bias
            season_bias[quarter] = round(new_bias, 4)
            logger.info(
                f"EWMA-Bias Update {yesterday_str}: Ist={actual_kwh:.1f} kWh, "
                f"Raw={raw_forecast_kwh:.1f} kWh, Sichtbar={visible_forecast_kwh:.1f} kWh, "
                f"Bias={bias_raw:.3f}, sichtbar={visible_ratio:.3f} ({clearsky_class}), "
                f"Neu={quarter}: {old_bias:.3f} -> {new_bias:.3f} (alpha={alpha})"
            )
        else:
            logger.info(
                f"EWMA-Bias: {yesterday_str} cloudy (bias={bias_raw:.2f}) - kein Update."
            )

        # Schritt 5: Tages-Log aktualisieren (max 90 Tage)
        if yesterday_str not in existing_dates:
            daily_log.append({
                "schema_version": 3,
                "forecast_value_schema": "raw_bias_visible",
                "date": yesterday_str,
                "actual_kwh": round(actual_kwh, 2),
                "forecast_kwh": round(visible_forecast_kwh, 2),
                "raw_forecast_kwh": round(raw_forecast_kwh, 2),
                "bias_corrected_kwh": round(bias_corrected_kwh, 2),
                "visible_forecast_kwh": round(visible_forecast_kwh, 2),
                "bias_raw": round(bias_raw, 4),
                "visible_ratio": round(visible_ratio, 4) if visible_ratio is not None else None,
                "bias_old": round(old_bias, 4),
                "bias_target": round(target_bias, 4),
                "bias_new": round(season_bias.get(quarter, old_bias), 4),
                "alpha": round(alpha, 3) if clearsky_class != "cloudy" else 0.0,
                "clearsky_class": clearsky_class,
                "quarter": quarter,
                "site_signature": site_signature,
                "slots_used": len(yest_slots),
                "ts": int(_t.time())
            })
        eval_data["daily_log"] = daily_log[-90:]
        eval_data["quarter_samples"] = _quarter_training_counts(
            eval_data["daily_log"],
            site_signature=site_signature,
        )
        eval_data["daily_log_schema_counts"] = _daily_log_schema_counts(
            eval_data["daily_log"],
            site_signature=site_signature,
        )
        eval_data["seasonal_bias"] = season_bias
        eval_data["last_update"] = now.isoformat(timespec='seconds')
        eval_data["version"] = 3

        with open(eval_path, 'w') as f:
            json.dump(eval_data, f, indent=2)

    except Exception as e:
        logger.warning(f"EWMA-Bias Update fehlgeschlagen (unkritisch): {e}")


def _get_current_bias_info(site_signature=None, site_descriptor=None, eval_path=FORECAST_EVAL_FILE, now=None) -> dict:
    """
    Gibt den aktuellen EWMA-Bias-Status fuer die Metadaten-Datei zurueck.
    Wird in pv_forecast_meta.json gespeichert (Dashboard-Anzeige).
    """
    try:
        if os.path.exists(eval_path):
            with open(eval_path, 'r') as f:
                ev = json.load(f)
            now = now or datetime.now()
            quarter = _quarter_for_date(now)
            stored_signature = str(ev.get("site_signature") or "")
            if site_signature and stored_signature and stored_signature != str(site_signature):
                return {
                    "current_quarter": quarter,
                    "current_bias": 1.0,
                    "bias_active": False,
                    "reason": "site_signature_changed",
                    "site_signature": site_signature,
                    "stored_site_signature": stored_signature,
                    "site_descriptor": site_descriptor,
                    "days_tracked": 0,
            }
            bias = float(ev.get("seasonal_bias", {}).get(quarter, 1.0) or 1.0)
            log = ev.get("daily_log", [])
            quarter_samples = ev.get("quarter_samples") or _quarter_training_counts(
                log,
                site_signature=site_signature or stored_signature,
            )
            quarter_eligible_days = int((quarter_samples.get(quarter) or {}).get("eligible", 0) or 0)
            schema_counts = _daily_log_schema_counts(
                log,
                site_signature=site_signature or stored_signature,
            )
            confirmation = _bias_confirmation_status(
                log,
                quarter,
                bias,
                now=now,
                site_signature=site_signature or stored_signature,
            )
            bias_guard = _recent_schema3_visible_overshoot_guard(
                log,
                quarter,
                bias,
                now=now,
                site_signature=site_signature or stored_signature,
            )
            has_non_neutral_bias = abs(float(bias or 1.0) - 1.0) >= 0.02
            has_training = quarter_eligible_days >= PV_BIAS_MIN_QUARTER_DAYS
            bias_active = has_non_neutral_bias and has_training and bool(confirmation.get("confirmed"))
            effective_bias = bias if bias_active else 1.0
            if bias_active and bias_guard.get("active"):
                try:
                    effective_bias = min(effective_bias, float(bias_guard.get("effective_bias_cap")))
                except (TypeError, ValueError):
                    pass
            if not has_non_neutral_bias:
                reason = "neutral_bias"
            elif not has_training:
                reason = "insufficient_quarter_samples"
            elif not confirmation.get("confirmed"):
                reason = "missing_recent_confirmation"
            elif bias_guard.get("active"):
                reason = "active_guarded_by_recent_visible_overshoot"
            else:
                reason = "active"
            return {
                "current_quarter": quarter,
                "current_bias": round(bias, 4),
                "effective_bias": round(effective_bias, 4),
                "bias_active": bias_active,
                "bias_confirmed": bool(confirmation.get("confirmed")),
                "bias_confirmation": confirmation,
                "bias_guard": bias_guard,
                "reason": reason,
                "seasonal_bias": ev.get("seasonal_bias", {}),
                "quarter_samples": quarter_samples,
                "quarter_days_tracked": quarter_eligible_days,
                "min_quarter_days": PV_BIAS_MIN_QUARTER_DAYS,
                "daily_log_schema_counts": schema_counts,
                "legacy_log_entries": int(schema_counts.get("legacy_forecast_only", 0) or 0),
                "days_tracked": len(log),
                "last_update": ev.get("last_update", ""),
                "last_entry": log[-1] if log else None,
                "last_entry_schema": _daily_log_entry_schema(log[-1]) if log else None,
                "site_signature": site_signature or stored_signature,
                "stored_site_signature": stored_signature,
                "site_descriptor": site_descriptor or ev.get("site_descriptor"),
                "site_signature_seen_at": ev.get("site_signature_seen_at", ""),
                "site_signature_changed_at": ev.get("site_signature_changed_at", ""),
            }
    except Exception:
        pass
    return {"current_bias": 1.0, "days_tracked": 0, "bias_active": False}


def _apply_daily_bias_to_forecast(
    ensemble_forecast: list,
    site_signature=None,
    eval_path=FORECAST_EVAL_FILE,
    now=None,
) -> list:
    """
    Wendet den EWMA-Saisonal-Bias auf die Ensemble-Prognose an.
    Wird im generate_ensemble() NACH der Ensemble-Berechnung aufgerufen.

    Der Bias ist ein multiplikativer Korrekturfaktor aus historischen Ist-vs-Prognose-Vergleichen.
    Beispiel: bias=1.18 bedeutet, das Modell prognostiziert 18% zu wenig -> Slots x1.18.

    Wichtige Einschraenkungen:
    - Max-Faktor: 1.40 (40% Erhoehung), Min-Faktor: 0.75 (25% Reduktion)
    - Nur tagsaktive Slots werden angepasst (predicted_kwh > 0.01 kW)
    - Bias wird als 'bias_applied' im Slot gespeichert fuer Transparenz
    """
    try:
        ensemble_forecast = _mark_raw_forecast_slots(ensemble_forecast)
        if not os.path.exists(eval_path):
            return ensemble_forecast
        with open(eval_path, 'r') as f:
            ev = json.load(f)
        if ev.get("version", 1) < 2:
            return ensemble_forecast  # Altes Format, noch kein Bias berechnet

        stored_signature = str(ev.get("site_signature") or "")
        if site_signature and stored_signature and stored_signature != str(site_signature):
            logger.warning(
                "EWMA-Bias ignoriert: gespeicherte Dach-/Anlagen-Signatur passt nicht zur aktuellen Konfiguration."
            )
            return ensemble_forecast

        now = now or datetime.now()
        quarter = _quarter_for_date(now)
        bias = float(ev.get("seasonal_bias", {}).get(quarter, 1.0) or 1.0)
        daily_log = ev.get("daily_log", [])
        quarter_samples = ev.get("quarter_samples") or _quarter_training_counts(
            daily_log,
            site_signature=site_signature or stored_signature,
        )
        quarter_eligible_days = int((quarter_samples.get(quarter) or {}).get("eligible", 0) or 0)

        # Mindestens 7 passende Tage im aktuellen Quartal benoetigt bevor Bias angewendet wird.
        if quarter_eligible_days < PV_BIAS_MIN_QUARTER_DAYS or abs(bias - 1.0) < 0.02:
            if quarter_eligible_days < PV_BIAS_MIN_QUARTER_DAYS:
                logger.debug(
                    f"EWMA-Bias: Noch nicht genug {quarter}-Daten "
                    f"({quarter_eligible_days}/{PV_BIAS_MIN_QUARTER_DAYS} Tage). Kein Bias angewendet."
                )
            return ensemble_forecast

        confirmation = _bias_confirmation_status(
            daily_log,
            quarter,
            bias,
            now=now,
            site_signature=site_signature or stored_signature,
        )
        if not confirmation.get("confirmed"):
            logger.info(
                f"EWMA-Bias: {quarter} bias={bias:.3f} ist nicht frisch bestätigt "
                f"({confirmation.get('confirmation_count', 0)}/{PV_BIAS_MIN_CONFIRMATIONS} ähnliche Werte "
                f"in {PV_BIAS_CONFIRMATION_WINDOW_DAYS} Tagen, "
                f"{quarter_eligible_days} Trainingstage). Kein Bias angewendet."
            )
            return ensemble_forecast

        # Bias sicher begrenzen
        bias_nominal = max(0.75, min(1.40, bias))
        bias_safe = bias_nominal
        bias_guard = _recent_schema3_visible_overshoot_guard(
            daily_log,
            quarter,
            bias,
            now=now,
            site_signature=site_signature or stored_signature,
        )
        if bias_guard.get("active"):
            try:
                bias_safe = min(bias_safe, float(bias_guard.get("effective_bias_cap")))
            except (TypeError, ValueError):
                pass
            logger.info(
                f"EWMA-Bias Guard aktiv: {quarter} nominal {bias_nominal:.3f} "
                f"-> effektiv {bias_safe:.3f} ({bias_guard.get('reason')})."
            )
        corrected = []
        for slot in ensemble_forecast:
            slot = dict(slot)
            raw_kw = _slot_kw(slot, "raw_predicted_kwh", "predicted_kwh")
            if raw_kw > 0.01:
                corrected_kw = raw_kw * bias_safe
                slot['predicted_kwh'] = round(corrected_kw, 4)
                slot['bias_corrected_kwh'] = round(corrected_kw, 4)
                slot['bias_applied'] = round(bias_safe, 4)
                if bias_guard.get("active"):
                    slot['bias_nominal'] = round(bias_nominal, 4)
                    slot['bias_guard_applied'] = True
                    slot['bias_guard_reason'] = str(bias_guard.get("reason") or "")
            else:
                slot['bias_corrected_kwh'] = round(raw_kw, 4)
            corrected.append(slot)

        logger.info(
            f"EWMA-Bias angewendet: {quarter} bias={bias_safe:.3f} "
            f"auf {len([s for s in corrected if s.get('bias_applied')])} aktive Slots "
            f"({quarter_eligible_days} {quarter}-Trainingstage, "
            f"{confirmation.get('confirmation_count', 0)} frisch bestätigt)."
        )
        return corrected
    except Exception as e:
        logger.warning(f"EWMA-Bias konnte nicht angewendet werden: {e}")
        return ensemble_forecast


def _cap_physical_pv_peak(ensemble_forecast: list, installed_kwp, margin: float = PV_PHYSICAL_PEAK_MARGIN):
    """
    Begrenze die Momentanleistung auf einen physikalisch plausiblen Anlagenpeak.

    predicted_kwh ist historisch falsch benannt und enthaelt die mittlere kW-Leistung
    des 15-Minuten-Slots. Der Tages-Bias darf die Energie korrigieren, aber keine
    unmoeglichen 15-Minuten-Spitzen oberhalb der Anlagenleistung erzeugen.

    Kurze Cloud-Edge-/Reflexionsspitzen bei wechselnder Bewoelkung sind realistisch.
    Deshalb gilt der enge Deckel nur fuer breite Plateaus; volatile Einzelspitzen
    duerfen bis zum Cloud-Edge-Puffer stehen bleiben.
    """
    try:
        peak_kw = float(installed_kwp or 0.0)
    except Exception:
        peak_kw = 0.0
    if peak_kw <= 0.0:
        return ensemble_forecast, 0, 0.0

    try:
        margin_safe = max(1.0, min(1.10, float(margin)))
    except Exception:
        margin_safe = PV_PHYSICAL_PEAK_MARGIN
    base_cap_kw = peak_kw * margin_safe
    cloud_edge_cap_kw = peak_kw * PV_CLOUD_EDGE_PEAK_MARGIN

    slots = list(ensemble_forecast or [])

    def _slot_kw(index):
        try:
            return float(slots[index].get("predicted_kwh", 0.0) or 0.0)
        except Exception:
            return 0.0

    def _cloud_edge_like(index, slot_kw):
        neighbors = []
        for offset in (-2, -1, 1, 2):
            j = index + offset
            if 0 <= j < len(slots):
                val = _slot_kw(j)
                if val > 0.01:
                    neighbors.append(val)
        if not neighbors:
            return False
        local_min = min(neighbors)
        local_avg = sum(neighbors) / len(neighbors)
        # Einzelne Reflexionsspitze: deutlich hoeher als die direkte Umgebung.
        return (
            slot_kw >= base_cap_kw
            and local_min <= slot_kw * 0.86
            and local_avg <= slot_kw * 0.93
        )

    corrected = []
    capped = 0
    for idx, slot in enumerate(slots):
        try:
            slot_kw = float(slot.get("predicted_kwh", 0.0) or 0.0)
        except Exception:
            corrected.append(slot)
            continue

        cap_kw = cloud_edge_cap_kw if _cloud_edge_like(idx, slot_kw) else base_cap_kw
        if slot_kw > cap_kw:
            slot = dict(slot)
            slot["physical_cap_before_kw"] = round(slot_kw, 4)
            slot["physical_cap_kw"] = round(cap_kw, 4)
            slot["predicted_kwh"] = round(cap_kw, 4)
            capped += 1
        corrected.append(slot)
    return corrected, capped, round(base_cap_kw, 4)


def _config_flag_enabled(config, *keys, default=True):
    config = config or {}
    for key in keys:
        if key in config:
            value = str(config.get(key)).strip().lower()
            return value not in ("0", "false", "no", "off", "nein", "aus")
    return default


def _parse_iso_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19]).date()
    except Exception:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def _select_seasonal_daily_cap_samples(rows, forecast_day, min_days, doy_window_days):
    samples = []
    for date_str, pv_yield in rows or []:
        sample_day = _parse_iso_date(date_str)
        if not sample_day:
            continue
        try:
            value = float(pv_yield)
        except Exception:
            continue
        samples.append({"date": sample_day, "pv_yield": value})

    if not samples:
        return [], {
            "seasonal_mode": "no_history",
            "total_days": 0,
            "doy_days": 0,
            "quarter_days": 0,
        }

    doy_samples = [
        s for s in samples
        if _day_of_year_distance(s["date"], forecast_day) <= int(doy_window_days)
    ]
    quarter = _quarter_for_date(forecast_day)
    quarter_samples = [s for s in samples if _quarter_for_date(s["date"]) == quarter]

    if len(doy_samples) >= int(min_days):
        selected = doy_samples
        mode = "day_of_year_window"
    elif len(quarter_samples) >= int(min_days):
        selected = quarter_samples
        mode = "quarter"
    else:
        return [], {
            "seasonal_mode": "insufficient_seasonal_history",
            "total_days": len(samples),
            "doy_days": len(doy_samples),
            "quarter_days": len(quarter_samples),
            "quarter": quarter,
            "doy_window_days": int(doy_window_days),
        }

    return [s["pv_yield"] for s in selected], {
        "seasonal_mode": mode,
        "total_days": len(samples),
        "doy_days": len(doy_samples),
        "quarter_days": len(quarter_samples),
        "seasonal_days": len(selected),
        "quarter": quarter,
        "doy_window_days": int(doy_window_days),
    }


def _recent_daily_pv_cap_kwh(
    forecast_day,
    site_signature=None,
    db_path=DAILY_STATS_DB_PATH,
    eval_path=FORECAST_EVAL_FILE,
    history_days=PV_DAILY_SANITY_HISTORY_DAYS,
    min_days=PV_DAILY_SANITY_MIN_DAYS,
    margin=PV_DAILY_SANITY_MARGIN,
    doy_window_days=PV_DAILY_SANITY_DOY_WINDOW_DAYS,
):
    """
    Liefert einen realitätsnahen Tagesenergie-Deckel aus echten Tageserträgen.

    Der Deckel ist bewusst an die Dach-/Anlagen-Signatur gekoppelt. Nach einer
    Dachänderung sind alte SO-only Tagesmaxima für die neue Geometrie nicht mehr
    belastbar und werden erst wieder nach neuen Beobachtungstagen genutzt.
    """
    try:
        day = datetime.strptime(str(forecast_day), "%Y-%m-%d").date()
    except Exception:
        return None, {"status": "invalid_day", "day": str(forecast_day)}

    eval_data = {}
    if os.path.exists(eval_path):
        try:
            eval_data = _load_forecast_eval(eval_path)
        except Exception:
            eval_data = {}

    stored_signature = str(eval_data.get("site_signature") or "")
    if site_signature and stored_signature and stored_signature != str(site_signature):
        return None, {
            "status": "site_signature_mismatch",
            "day": str(forecast_day),
            "site_signature": site_signature,
            "stored_site_signature": stored_signature,
        }

    changed_at = _parse_iso_date(eval_data.get("site_signature_changed_at"))
    seen_at = _parse_iso_date(eval_data.get("site_signature_seen_at")) if changed_at else None
    lower_bound = day - timedelta(days=int(history_days))
    if seen_at and seen_at > lower_bound:
        lower_bound = seen_at

    if not os.path.exists(db_path):
        return None, {"status": "no_db", "day": str(forecast_day)}

    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute(
            """
            SELECT date, pv_yield
              FROM daily_stats
             WHERE date >= ? AND date < ? AND pv_yield > 5
             ORDER BY date ASC
            """,
            (lower_bound.strftime("%Y-%m-%d"), day.strftime("%Y-%m-%d")),
        )
        rows = c.fetchall()
        conn.close()
    except Exception as exc:
        return None, {"status": "db_error", "day": str(forecast_day), "error": str(exc)}

    values, seasonal_info = _select_seasonal_daily_cap_samples(
        rows,
        day,
        min_days=min_days,
        doy_window_days=doy_window_days,
    )
    if len(values) < int(min_days):
        return None, {
            "status": seasonal_info.get("seasonal_mode", "insufficient_seasonal_history"),
            "day": str(forecast_day),
            "days": len(values),
            "min_days": int(min_days),
            "history_from": lower_bound.strftime("%Y-%m-%d"),
            **seasonal_info,
        }

    values.sort(reverse=True)
    top_n = min(7, max(5, len(values) // 10))
    top_values = values[:top_n]
    observed_max = values[0]
    robust_top_avg = sum(top_values) / len(top_values)
    cap_kwh = max(observed_max * 1.03, robust_top_avg * float(margin))
    return round(cap_kwh, 2), {
        "status": "ok",
        "day": str(forecast_day),
        "cap_kwh": round(cap_kwh, 2),
        "observed_max_kwh": round(observed_max, 2),
        "robust_top_avg_kwh": round(robust_top_avg, 2),
        "top_days": len(top_values),
        "days": len(values),
        "history_from": lower_bound.strftime("%Y-%m-%d"),
        "margin": float(margin),
        **seasonal_info,
    }


def _cap_daily_forecast_totals(
    ensemble_forecast: list,
    installed_kwp=None,
    site_signature=None,
    config=None,
    db_path=DAILY_STATS_DB_PATH,
    eval_path=FORECAST_EVAL_FILE,
):
    if not _config_flag_enabled(
        config,
        "pv_forecast_daily_cap_enabled",
        "pv_daily_sanity_cap_enabled",
        default=True,
    ):
        return ensemble_forecast, [{"status": "disabled"}]

    by_day = {}
    for slot in ensemble_forecast or []:
        try:
            day = datetime.fromtimestamp(slot["start_timestamp"] / 1000).strftime("%Y-%m-%d")
        except Exception:
            continue
        by_day.setdefault(day, []).append(slot)

    summaries = []
    corrected_by_id = {}
    for day, day_slots in by_day.items():
        before_kwh = _slots_energy_kwh(day_slots, "predicted_kwh")
        cap_kwh, info = _recent_daily_pv_cap_kwh(
            day,
            site_signature=site_signature,
            db_path=db_path,
            eval_path=eval_path,
        )
        info["forecast_before_kwh"] = round(before_kwh, 2)
        try:
            info["installed_kwp"] = round(float(installed_kwp or 0.0), 3)
        except Exception:
            info["installed_kwp"] = 0.0
        if cap_kwh is None or before_kwh <= cap_kwh or before_kwh <= 0.01:
            summaries.append(info)
            continue

        scale = cap_kwh / before_kwh
        capped_slots = 0
        for slot in day_slots:
            original_id = id(slot)
            slot = dict(slot)
            old_kw = _slot_kw(slot, "predicted_kwh")
            if old_kw > 0.01:
                slot["daily_cap_before_kw"] = round(old_kw, 4)
                slot["daily_cap_kwh"] = round(cap_kwh, 2)
                slot["daily_cap_scale"] = round(scale, 4)
                slot["predicted_kwh"] = round(old_kw * scale, 4)
                capped_slots += 1
            corrected_by_id[original_id] = slot
        info.update({
            "status": "applied",
            "forecast_after_kwh": round(cap_kwh, 2),
            "scale": round(scale, 4),
            "slots": capped_slots,
        })
        summaries.append(info)

    corrected = []
    for slot in ensemble_forecast or []:
        replacement = corrected_by_id.get(id(slot))
        corrected.append(replacement if replacement is not None else slot)

    applied = [s for s in summaries if s.get("status") == "applied"]
    for item in applied:
        logger.warning(
            "PV-Tagesprognose plausibilisiert: %s %.1f -> %.1f kWh "
            "(reale Spitzenhistorie %.1f kWh, %d Tage)."
            % (
                item.get("day"),
                item.get("forecast_before_kwh", 0.0),
                item.get("forecast_after_kwh", 0.0),
                item.get("observed_max_kwh", 0.0),
                item.get("days", 0),
            )
        )
    return corrected, summaries


def _update_model_weights(accuracy_info: dict):
    """
    ML Weight Evaluator: Speichert taeglich die Prognose-Abweichung pro Modell
    in pv_forecast_eval.json (Disk-persistent).
    Nach 7+ Tagen mit genuegend Tagesertrag (>= 5 kWh) werden die Gewichte
    (pv_score_m1/m2/m3) in e3dc_v4.json proportional zur inversen MAE angepasst.
    """
    if not accuracy_info or accuracy_info.get('error_pct') is None:
        return

    import time as _t
    EVAL_FILE = "/var/www/html/logs/pv_forecast_eval.json"
    V4_CONFIG = "/var/www/html/data/e3dc_v4.json"

    try:
        # Evaluations-History laden
        if os.path.exists(EVAL_FILE):
            with open(EVAL_FILE, 'r') as f:
                eval_data = json.load(f)
        else:
            eval_data = {"daily_errors": [], "last_weight_update": 0, "current_weights": {}}

        # Nur bei relevantem Tagesertrag (nicht um Mitternacht mit 0 kWh)
        today_kwh = accuracy_info.get('today_actual_total_kwh', 0)
        if today_kwh < 2.0:  # Kein Ertrag / Nacht -> ueberspringe
            return

        today_str = datetime.now().strftime('%Y-%m-%d')
        error_pct = accuracy_info.get('error_pct', 0)
        slot_pred = accuracy_info.get('slot_predicted_kwh', 0)
        slot_actual = accuracy_info.get('slot_actual_kwh_est', 0)

        # Tages-Eintrag (nur einmal pro Tag, letzten Eintrag ggf. ueberschreiben)
        daily = eval_data.get('daily_errors', [])
        if daily and daily[-1].get('date') == today_str:
            daily[-1].update({'error_pct': error_pct, 'today_kwh': today_kwh,
                              'pred_kwh': slot_pred, 'actual_kwh': slot_actual})
        else:
            daily.append({'date': today_str, 'error_pct': error_pct,
                          'today_kwh': today_kwh, 'pred_kwh': slot_pred,
                          'actual_kwh': slot_actual, 'ts': int(_t.time())})
        # Nur letzten 90 Tage behalten
        eval_data['daily_errors'] = daily[-90:]

        # Prüfe, ob genügend Daten für eine Gewichtsanpassung vorhanden sind
        eligible_days = [d for d in daily if d.get('today_kwh', 0) >= 5.0]
        days_since_update = (_t.time() - eval_data.get('last_weight_update', 0)) / 86400

        if len(eligible_days) >= 7 and days_since_update >= 7:
            # -------------------------------------------------------------------
            # Einfaches MAE-basiertes Gewichts-Update:
            # Wir haben pro Slot nur den Ensemble-Fehler, nicht pro Modell.
            # Da wir m1_raw, m2_raw einzeln gespeichert haben, berechnen wir
            # die MAE pro Modell aus der History (pv_forecast_history.json).
            # -------------------------------------------------------------------
            history_path = os.path.join(RAMDISK_DIR, "pv_forecast_history.json")
            live_path = os.path.join(RAMDISK_DIR, "live_data_py.json")

            if os.path.exists(history_path) and os.path.exists(live_path):
                with open(history_path, 'r') as f:
                    hist = json.load(f)

                # Pro Modell: absolute Fehler aufsammeln
                m1_errs, m2_errs, m3_errs = [], [], []
                for slot in hist:
                    actual = slot.get('actual_kwh')  # Wird erst unten geschrieben
                    pred_m1 = slot.get('m1_raw')
                    pred_m2 = slot.get('m2_raw')
                    pred_m3 = slot.get('m3_raw')
                    ens = slot.get('predicted_kwh', 0)
                    if actual is None or ens == 0:
                        continue
                    # Skaliere Modell-Rohwerte auf Ensemble-Ebene
                    if pred_m1 is not None: m1_errs.append(abs(pred_m1 - actual))
                    if pred_m2 is not None: m2_errs.append(abs(pred_m2 - actual))
                    if pred_m3 is not None: m3_errs.append(abs(pred_m3 - actual))

                if m1_errs and m2_errs:
                    mae_m1 = sum(m1_errs) / len(m1_errs)
                    mae_m2 = sum(m2_errs) / len(m2_errs)
                    mae_m3 = sum(m3_errs) / len(m3_errs) if m3_errs else mae_m2

                    # Inverser Fehler -> besseres Modell bekommt hoehere Gewichtung
                    # Epsilon vermeidet Division durch Null bei perfektem Modell
                    inv_m1 = 1.0 / (mae_m1 + 0.001)
                    inv_m2 = 1.0 / (mae_m2 + 0.001)
                    inv_m3 = 1.0 / (mae_m3 + 0.001)
                    total_inv = inv_m1 + inv_m2 + inv_m3

                    # Neue Gewichte (auf 2 Dezimalen gerundet, Summe = 1.0)
                    new_w1 = round(inv_m1 / total_inv, 2)
                    new_w2 = round(inv_m2 / total_inv, 2)
                    new_w3 = round(1.0 - new_w1 - new_w2, 2)  # Rest-Gewicht (Summe = 1.0)

                    # Plausibilitätsprüfung: kein Modell unter 5 % oder über 70 %
                    new_w1 = max(0.05, min(0.70, new_w1))
                    new_w2 = max(0.05, min(0.70, new_w2))
                    new_w3 = max(0.05, min(0.70, new_w3))
                    total_new = new_w1 + new_w2 + new_w3
                    new_w1 /= total_new; new_w2 /= total_new; new_w3 /= total_new

                    # In e3dc_v4.json schreiben (nur wenn Datei schreibbar)
                    if os.path.exists(V4_CONFIG):
                        with open(V4_CONFIG, 'r') as f:
                            v4 = json.load(f)
                        old_w = (v4.get('pv_score_m1', '?'), v4.get('pv_score_m2', '?'), v4.get('pv_score_m3', '?'))
                        v4['pv_score_m1'] = round(new_w1, 4)
                        v4['pv_score_m2'] = round(new_w2, 4)
                        v4['pv_score_m3'] = round(new_w3, 4)
                        with open(V4_CONFIG, 'w') as f:
                            json.dump(v4, f, indent=2)
                        logger.info(
                            f"ML Weight-Update: M1 {old_w[0]}->{new_w1:.3f} "
                            f"(MAE {mae_m1:.4f}), "
                            f"M2 {old_w[1]}->{new_w2:.3f} (MAE {mae_m2:.4f}), "
                            f"M3 {old_w[2]}->{new_w3:.3f} (MAE {mae_m3:.4f}). "
                            f"Basis: {len(m1_errs)} Vergleichsslots."
                        )
                        eval_data['last_weight_update'] = int(_t.time())
                        eval_data['current_weights'] = {
                            'pv_score_m1': round(new_w1, 4),
                            'pv_score_m2': round(new_w2, 4),
                            'pv_score_m3': round(new_w3, 4),
                            'mae_m1': round(mae_m1, 4),
                            'mae_m2': round(mae_m2, 4),
                            'mae_m3': round(mae_m3, 4),
                            'sample_count': len(m1_errs),
                            'updated_at': datetime.now().isoformat(timespec='seconds')
                        }

        with open(EVAL_FILE, 'w') as f:
            json.dump(eval_data, f, indent=2)

    except Exception as e:
        logger.warning(f"Weight-Evaluator Fehler (unkritisch): {e}")

def _compute_forecast_accuracy(current_forecast):
    """
    Vergleicht den heutigen Ist-Ertrag (aus live_data_py.json) gegen die Prognose
    für diese Stunde und gibt ein Dict mit Abweichungsinformationen zurück.
    Wird in pv_forecast_meta.json gespeichert (Diagnose / zukuenftige Gewichtsanpassung).
    """
    try:
        import time as _t
        live_path = os.path.join(RAMDISK_DIR, "live_data_py.json")
        if not os.path.exists(live_path):
            return {}
        with open(live_path, 'r') as f:
            live = json.load(f)
        actual_pv_w = float(live.get('PV_Power', live.get('pv_power', 0)))
        actual_kwh_today = float(live.get('PV_Energy_kWh', live.get('pv_energy_kwh', 0)))

        # Welcher Forecast-Slot entspricht der aktuellen Stunde?
        now_ms = int(_t.time() * 1000)
        matching = [s for s in current_forecast
                    if s['start_timestamp'] <= now_ms < s['end_timestamp']]
        if not matching:
            return {"status": "kein passender Slot"}
        predicted_kwh = matching[0]['predicted_kwh']
        actual_slot_kwh = actual_pv_w / 1000.0 * 0.25  # 15-Min-Slot -> kWh Schaetzung

        error_pct = 0.0
        if predicted_kwh > 0:
            error_pct = round((actual_slot_kwh - predicted_kwh) / predicted_kwh * 100, 1)

        result = {
            "slot_predicted_kwh": round(predicted_kwh, 3),
            "slot_actual_kwh_est": round(actual_slot_kwh, 3),
            "error_pct": error_pct,
            "today_actual_total_kwh": round(actual_kwh_today, 2),
            "ts": int(_t.time())
        }
        if abs(error_pct) > 25:
            logger.warning(
                f"Prognose-Abweichung: {error_pct:+.1f}% "
                f"(Prognose {predicted_kwh:.3f} kWh vs Ist-Schaetzung {actual_slot_kwh:.3f} kWh). "
                f"Tages-Ist bisher: {actual_kwh_today:.1f} kWh."
            )
        return result
    except Exception as e:
        logger.debug(f"Accuracy-Check fehlgeschlagen: {e}")
        return {}


# HINWEIS: 'datetime' ist bereits oben mit 'from datetime import datetime, timedelta' importiert.
# KEIN 'import datetime' hier - das wuerde den Klassennamen im globalen Scope ueberschreiben!

# ---------------------------------------------------------------------------
# Logging: begrenztes Dateilog und Journal-Ausgabe
# ---------------------------------------------------------------------------
LOG_DIR_SVC = "/var/www/html/logs"
if not os.path.exists(LOG_DIR_SVC):
    LOG_DIR_SVC = RAMDISK_DIR  # Fallback falls logs-Dir noch nicht existiert

_log_file = os.path.join(LOG_DIR_SVC, "pv_forecast.log")
from runtime_logging import configure_service_logger
logger = configure_service_logger(
    "EnsemblePVForecaster",
    log_path=_log_file,
    max_bytes=2 * 1024 * 1024,
    backup_count=3,
    # Das begrenzte Dateilog ist die kanonische persistente Senke. Eine
    # parallele stderr-/Journal-Kopie würde jeden Provider- und Modellhinweis
    # doppelt auf den Datenträger schreiben. Ungefangene Prozessfehler bleiben
    # unabhängig davon über systemd sichtbar.
    stream=False,
    quiet_interval_s=1800.0,
)


def _get_sleep_seconds():
    """
    Haupt-Loop-Intervall (wie oft der Loop laeuft und prft ob ein Modell ablaeuft):
    - Tagzeit (07-21 Uhr): 60 Minuten
    - Nacht (21-07 Uhr):  120 Minuten
    Die echten API-Calls werden durch Modell-TTLs und Solcast-Tagesbudget gesteuert.
    """
    hour = datetime.now().hour  # datetime = Klasse aus 'from datetime import datetime'
    if 7 <= hour < 21:
        return 60 * 60   # 60 Minuten tags\u00fcber
    return 120 * 60      # 2 Stunden nachts



def _forecast_is_stale():
    """
    Gibt True zurueck wenn pv_forecast.json aelter als 3 Stunden ist
    und wir uns in der Tagzeit befinden (dann sofort neu abrufen).
    """
    if not os.path.exists(FORECAST_OUTPUT):
        return True
    age_secs = time.time() - os.path.getmtime(FORECAST_OUTPUT)
    hour = datetime.now().hour  # datetime = Klasse aus 'from datetime import datetime'
    if 7 <= hour < 21 and age_secs > 3 * 3600:
        logger.warning(f"pv_forecast.json ist {age_secs/3600:.1f}h alt - sofortiger Re-Fetch!")
        return True
    return False


def _weather_alerts_are_stale():
    """
    DWD-Warnungen koennen sehr kurzfristig kommen. Darum laeuft dieser Check
    unabhaengig vom teuren PV-Forecast-Zyklus.
    """
    if not os.path.exists(WEATHER_ALERTS_OUTPUT):
        return True
    try:
        return (time.time() - os.path.getmtime(WEATHER_ALERTS_OUTPUT)) > WEATHER_ALERT_TTL_S
    except Exception:
        return True


def _ml_prediction_is_stale(max_age_s=ML_PREDICTION_MAX_AGE_S):
    if not os.path.exists(ML_PREDICTION_OUTPUT):
        return True
    try:
        return (time.time() - os.path.getmtime(ML_PREDICTION_OUTPUT)) > max_age_s
    except Exception:
        return True


def _run_ml_predict_if_ready(force=False):
    """
    Die PV-Prognose liegt in der Ramdisk und wird nach Reboot neu aufgebaut.
    Die Verbrauchs-/WP-Prognose muss denselben Lifecycle haben, sonst fällt der
    Simulator nach jedem Neustart auf feste 500W/300W-Ersatzwerte zurück.
    """
    if not force and not _ml_prediction_is_stale():
        return False

    ml_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ml_predictor.py"))
    if not os.path.exists(ml_script):
        logger.warning("ML-Predictor fehlt: %s", ml_script)
        return False

    python_bin = sys.executable or "python3"
    try:
        ready_result = subprocess.run(
            [python_bin, ml_script, "--model-ready"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        result = subprocess.run(
            [python_bin, ml_script, "--predict"],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except Exception as exc:
        logger.warning("ML-Verbrauchsprognose konnte nicht gestartet werden: %s", exc)
        return False

    stdout_tail = (result.stdout or "").strip().splitlines()[-6:]
    stderr_tail = (result.stderr or "").strip().splitlines()[-6:]
    for line in stdout_tail:
        logger.info("ml_predictor: %s", line)
    for line in stderr_tail:
        logger.warning("ml_predictor stderr: %s", line)

    if result.returncode == 0 and os.path.exists(ML_PREDICTION_OUTPUT):
        forecast_mode = "unknown"
        try:
            with open(ML_PREDICTION_OUTPUT, "r", encoding="utf-8") as prediction_file:
                prediction = json.load(prediction_file)
            if isinstance(prediction, dict):
                forecast_mode = str(prediction.get("forecast_mode") or "unknown")
        except Exception:
            pass
        if ready_result.returncode != 0 and forecast_mode == "historical_profile":
            logger.info(
                "Kein manifest- und hashgeprüftes ML-Modell; "
                "variables lokales Historienprofil wurde veröffentlicht."
            )
        logger.info(
            "Verbrauchsprognose aktualisiert: %s (mode=%s)",
            ML_PREDICTION_OUTPUT,
            forecast_mode,
        )
        return True

    logger.warning(
        "ML-Verbrauchsprognose fehlgeschlagen (rc=%s, output_exists=%s).",
        result.returncode,
        os.path.exists(ML_PREDICTION_OUTPUT),
    )
    return False


def _refresh_weather_alerts_only():
    try:
        app = EnsemblePVForecaster()
        app.update_weather_alerts()
        return True
    except Exception as exc:
        logger.warning("Wetterwarnungen konnten nicht separat aktualisiert werden: %s", exc)
        return False


if __name__ == "__main__":
    import sys as _sys
    _once = "--once" in _sys.argv  # Einzel-Run: genau ein Zyklus, dann exit (fuer entrypoint)
    _force_resource_refresh = "--force-resource-refresh" in _sys.argv

    logger.info(
        "Starte PV Wetter & Forecast Manager Service (V4)%s%s...",
        " [ONCE]" if _once else "",
        " [FORCE-RESOURCE-REFRESH]" if _force_resource_refresh else "",
    )
    logger.info(f"Log-Datei: {_log_file}")

    while True:
        try:
            # Konfiguration bei jedem Zyklus neu laden (UI-Aenderungen wirken ohne Neustart)
            app = EnsemblePVForecaster(
                force_resource_refresh=_force_resource_refresh,
            )
            app.generate_ensemble()
            _run_ml_predict_if_ready(force=_once)
        except Exception as e:
            logger.error(f"Unerwarteter Fehler im Forecast-Loop: {e}", exc_info=True)

        if _once:
            logger.info("--once: Einzel-Fetch abgeschlossen. Beende Prozess.")
            break  # Kein Daemon - sofort beenden (fuer entrypoint.sh / update.py)

        sleep_secs = _get_sleep_seconds()
        logger.info(f"Naechster API-Fetch in {sleep_secs // 60} Minuten (Tagzeit={7 <= datetime.now().hour < 21}).")

        # Adaptiv schlafen: alle 60s pruefen ob Prognose veraltet ist
        slept = 0
        while slept < sleep_secs:
            time.sleep(60)
            slept += 60
            if _weather_alerts_are_stale():
                logger.info("Wetterwarnungen veraltet - aktualisiere DWD/ICON ohne PV-Forecast.")
                _refresh_weather_alerts_only()
            if _forecast_is_stale():
                logger.info("Prognose veraltet - breche Schlaf ab und aktualisiere sofort.")
                break
