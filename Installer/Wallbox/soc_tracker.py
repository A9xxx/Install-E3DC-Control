"""
Central per-wallbox vehicle SoC tracker.

The tracker keeps one estimation path for all wallbox types:
- use a fresh confirmed wallbox/vehicle/manual SoC as anchor
- add measured wallbox energy while the car is connected
- derive vehicle range from the saved capacity/consumption profile
- write manual_soc_wbX.json so scheduler and UI see the same value

openWB Pro keeps its own CCS/import counter estimator in the driver. Those
values are treated as authoritative and are not overwritten here.
"""
import json
import math
import os
import re
import time
from datetime import datetime

from .config import RAMDISK_DIR, logger

SAVED_CARS_FILE = "/var/www/html/data/saved_cars.json"
TMP_DIR = "/var/www/html/tmp"
TRACKER_CHECKPOINT_ACTIVE_HEARTBEAT_S = 120.0
TRACKER_CHECKPOINT_IDLE_HEARTBEAT_S = 900.0
TRACKER_CHECKPOINT_SEMANTIC_KEYS = (
    "vehicle_key",
    "car_id",
    "vehicle_id",
    "anchor_soc",
    "anchor_sample_ts",
    "anchor_source",
    "anchor_meter_wh",
    "meter_source",
    "connected",
    "charging",
    "plug_session_id",
    "session_closed",
    "estimate_expired",
)

ESTIMATED_PREFIX = "wallbox_estimated"
OPENWB_PRO_SOURCES = ("openwb_pro_raw", "openwb_pro_estimated")
NO_VEHICLE_IDS = ("", "__none", "none", "no_vehicle", "kein_fahrzeug", "0", "false")
PROFILE_ALIAS_KEYS = (
    "id",
    "profile_id",
    "cloud_vehicle_id",
    "vehicle_id",
    "vehicle_mac",
    "mac",
    "rfid",
    "rfid_tag",
)
LIVE_STATUS_ID_KEYS = ("car_id", "vehicle_id", "rfid_tag")
SESSION_SCOPED_ANCHOR_SOURCES = ("manual_start_soc", "simple_view_start_soc", "config_start_soc")
UNMETERED_SESSION_ANCHOR_MAX_AGE_S = 36 * 3600
CONFIRMED_MANUAL_SOC_SOURCES = ("manual_start_soc", "manual_soc", "manual", "openwb_profile_link")
CONFIRMED_VEHICLE_SOC_KEYWORDS = ("mqtt", "bluelink", "wallbox", "openwb", "vehicle", "car_soc", "hyundai", "kia")
UNCONFIRMED_SOC_SOURCES = ("simple_view_start_soc", "config_start_soc")
OPENWB_PRO_STATUS_MAX_AGE_S = 60
OPENWB_PRO_VEHICLE_SOC_MAX_AGE_S = 8 * 3600
OPENWB_EXPLICIT_TOTAL_RANGE_SOURCES = frozenset(("http_total", "mqtt_total"))
OPENWB_EXPLICIT_CHARGED_RANGE_SOURCES = frozenset(("http_charged", "mqtt_charged"))
OPENWB_EXPLICIT_RANGE_MAX_AGE_S = 120.0
OPENWB_EXPLICIT_RANGE_SOURCE_MAX_AGE_S = OPENWB_PRO_VEHICLE_SOC_MAX_AGE_S


def _compact_id(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        text = str(value).strip()
        if text == "":
            return default
        return float(text.replace(",", "."))
    except (TypeError, ValueError):
        return default


def _clamp_percent(value, default=0.0):
    return max(0.0, min(100.0, _safe_float(value, default)))


def _is_confirmed_soc_source(source):
    text = str(source or "").strip().lower()
    if not text or text in UNCONFIRMED_SOC_SOURCES or text.startswith(ESTIMATED_PREFIX):
        return False
    if text in CONFIRMED_MANUAL_SOC_SOURCES or text in OPENWB_PRO_SOURCES:
        return True
    return any(token in text for token in CONFIRMED_VEHICLE_SOC_KEYWORDS)


def _timestamp(value, default=0.0):
    if value in (None, ""):
        return default
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts / 1000.0 if ts > 100000000000.0 else ts
    text = str(value).strip()
    if not text:
        return default
    try:
        ts = float(text)
        return ts / 1000.0 if ts > 100000000000.0 else ts
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return default


def _status_has_connected_vehicle(status):
    if not isinstance(status, dict):
        return False
    if bool(status.get("plug_state") or status.get("locked") or status.get("charging") or status.get("charge_state")):
        return True
    try:
        return int(status.get("car", 1) or 1) >= 2
    except (TypeError, ValueError):
        return False


def _current_explicit_openwb_range(
    status,
    *,
    value_keys,
    source_key,
    valid_key,
    observed_ts_key,
    source_ts_key,
    source_ts_explicit_key,
    vehicle_key_key,
    allowed_sources,
    now_ts=None,
):
    """Liefere nur eine frisch beobachtete, sitzungsplausible openWB-Reichweite."""

    if not isinstance(status, dict) or not _status_has_connected_vehicle(status):
        return None
    if status.get("driver_status_valid") is False or bool(status.get("driver_status_stale", False)):
        return None
    if status.get(valid_key) is not True:
        return None

    source = str(status.get(source_key) or "").strip().lower()
    if source not in allowed_sources:
        return None
    value = None
    for key in value_keys:
        if key not in status:
            continue
        candidate = _safe_float(status.get(key), -1.0)
        if candidate >= 0.0:
            value = candidate
            break
    if value is None:
        return None

    now = time.time() if now_ts is None else float(now_ts)
    observed_ts = _timestamp(status.get(observed_ts_key), 0.0)
    age_s = now - observed_ts
    if (
        not math.isfinite(now)
        or not math.isfinite(observed_ts)
        or observed_ts <= 0.0
        or age_s < -5.0
        or age_s > OPENWB_EXPLICIT_RANGE_MAX_AGE_S
    ):
        return None

    # Die HTTP-Beobachtung beweist nur, wann E3DC-Control denselben Wert erneut
    # gelesen hat. Liefert openWB eine eigene Quellenzeit, muss auch diese frisch
    # sein; wiederholtes Polling darf einen alten Fahrzeugwert nie verjüngen.
    # Alte openWB-Versionen ohne Quellenzeit bleiben über die frische lokale
    # Beobachtung kompatibel.
    source_ts_raw = status.get(source_ts_key)
    # Nur der vom Treiber ausdrücklich gesetzte Marker beweist, dass das Feld
    # eine eigene openWB-Quellenzeit enthält. Ältere Statusstände konnten dort
    # noch eine SoC-Zeit ohne diese Semantik führen; sie bleiben allein über
    # die maximal 120 Sekunden alte lokale Reichweitenbeobachtung gültig.
    source_ts_explicit = status.get(source_ts_explicit_key) is True
    if not source_ts_explicit:
        source_ts = observed_ts
    else:
        if source_ts_raw is None or (
            isinstance(source_ts_raw, str)
            and source_ts_raw.strip().lower() in ("", "null")
        ):
            return None
        source_ts = _timestamp(source_ts_raw, 0.0)
        source_age_s = now - source_ts
        if (
            not math.isfinite(source_ts)
            or source_ts <= 0.0
            or source_age_s < -5.0
            or source_age_s > OPENWB_EXPLICIT_RANGE_SOURCE_MAX_AGE_S
        ):
            return None

    bound_vehicle_key = _compact_id(status.get(vehicle_key_key))
    if bound_vehicle_key:
        if not bool(status.get("stable_vehicle_identity_current", False)):
            return None
        current_keys = {
            key
            for key in (
                _compact_id(status.get("vehicle_id")),
                _compact_id(status.get("rfid_tag")),
                _compact_id(status.get("car_id")),
            )
            if key
        }
        if not current_keys or bound_vehicle_key not in current_keys:
            return None

    return {
        "range_km": value,
        "range_source": source,
        "range_observed_ts": int(observed_ts),
        "range_source_ts": int(source_ts),
        "range_source_ts_explicit": source_ts_explicit,
        "range_vehicle_key": str(status.get(vehicle_key_key) or ""),
        "range_explicit": True,
    }


def current_explicit_openwb_total_range(status, now_ts=None):
    """Aktuelle openWB-Gesamtreichweite; Profilrechnungen sind hier nie autoritativ."""

    return _current_explicit_openwb_range(
        status,
        value_keys=("car_range", "range_km"),
        source_key="car_range_source",
        valid_key="car_range_valid",
        observed_ts_key="car_range_observed_ts",
        source_ts_key="car_range_source_ts",
        source_ts_explicit_key="car_range_source_ts_explicit",
        vehicle_key_key="car_range_vehicle_key",
        allowed_sources=OPENWB_EXPLICIT_TOTAL_RANGE_SOURCES,
        now_ts=now_ts,
    )


def current_explicit_openwb_charged_range(status, now_ts=None):
    """Aktuelle geladene Reichweite als eigener, niemals totaler Wert."""

    return _current_explicit_openwb_range(
        status,
        value_keys=("car_charged_range", "charged_range_km"),
        source_key="car_charged_range_source",
        valid_key="car_charged_range_valid",
        observed_ts_key="car_charged_range_observed_ts",
        source_ts_key="car_charged_range_source_ts",
        source_ts_explicit_key="car_charged_range_source_ts_explicit",
        vehicle_key_key="car_charged_range_vehicle_key",
        allowed_sources=OPENWB_EXPLICIT_CHARGED_RANGE_SOURCES,
        now_ts=now_ts,
    )


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data
    except Exception:
        return default


def _write_json_atomic(path, payload):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o664)
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.debug("SoC-Tracker: Schreiben fehlgeschlagen (%s): %s", path, exc)
        return False


def _vehicle_aliases(vehicle):
    aliases = []
    if not isinstance(vehicle, dict):
        return aliases
    for key in ("id", "profile_id", "cloud_vehicle_id", "vehicle_id", "vehicle_mac", "mac", "rfid", "rfid_tag"):
        raw = str(vehicle.get(key) or "").strip()
        if raw:
            aliases.append(raw)
    return aliases


def _compact_aliases(vehicle, keys=PROFILE_ALIAS_KEYS):
    if not isinstance(vehicle, dict):
        return set()
    return {
        compact
        for compact in (_compact_id(vehicle.get(key)) for key in keys)
        if compact
    }


def _matches_vehicle(vehicle, selected_id):
    selected = str(selected_id or "").strip()
    if not selected or selected.lower() in NO_VEHICLE_IDS:
        return False
    if str(vehicle.get("id") or "").strip() == selected:
        return True
    selected_compact = _compact_id(selected)
    for alias in _vehicle_aliases(vehicle):
        if alias == selected or (_compact_id(alias) and _compact_id(alias) == selected_compact):
            return True
    return False


def _load_saved_cars():
    data = _read_json(SAVED_CARS_FILE, [])
    if isinstance(data, dict):
        data = list(data.values())
    return data if isinstance(data, list) else []


def _load_live_vehicles():
    data = _read_json(os.path.join(RAMDISK_DIR, "vehicles.json"), [])
    if isinstance(data, dict):
        data = data.get("vehicles", [])
    return data if isinstance(data, list) else []


def _unique_saved_profile(selected_id):
    """Löse die konfigurierte Auswahl auf genau ein gespeichertes Profil auf."""

    selected = str(selected_id or "").strip()
    if not selected or selected.lower() in NO_VEHICLE_IDS:
        return None
    matches = [
        car
        for car in _load_saved_cars()
        if isinstance(car, dict) and _matches_vehicle(car, selected)
    ]
    return dict(matches[0]) if len(matches) == 1 else None


def _truthy(value):
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    if isinstance(value, (int, float)):
        return float(value) != 0.0
    return str(value).strip().lower() in ("1", "true", "yes", "on", "connected", "plugged")


def _fresh_timestamp(value, now, max_age_s):
    sample_ts = _timestamp(value, 0.0)
    if sample_ts <= 0.0:
        return 0.0
    age_s = float(now) - sample_ts
    if age_s < -300.0 or age_s > float(max_age_s):
        return 0.0
    return sample_ts


def _plug_session_started_ts(plug_session_id, now):
    """Löse die epochbasierte Startzeit einer openWB-Pro-Stecksession auf."""

    raw = str(plug_session_id or "").rsplit(":", 1)[-1].strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value > 1.0e17:
        value /= 1.0e9
    elif value > 1.0e14:
        value /= 1.0e6
    elif value > 1.0e11:
        value /= 1.0e3
    # Monotone Prozessuhren oder beliebige Tokens dürfen nicht wie eine
    # belastbare Steckzeit behandelt werden.
    if value < 1577836800.0 or value > float(now) + 300.0:
        return 0.0
    return value


def _openwb_pro_session_meter_wh(status):
    """Liefere ausschließlich den zur Stecksession gehörenden Energiezähler."""

    if not isinstance(status, dict) or status.get("session_kwh") is None:
        return None
    value = _safe_float(status.get("session_kwh"), -1.0)
    return value * 1000.0 if value >= 0.0 else None


def _openwb_pro_same_session_sample(
    wb_id,
    profile,
    profile_aliases,
    plug_session_id,
    plug_session_started_ts,
    meter_wh,
    now,
):
    """Binde eine bestätigte Pro-Schätzung an dieselbe Stecksession."""

    data = _read_manual_soc(wb_id, None)
    if not isinstance(data, dict):
        return None
    source = str(data.get("source") or "").strip()
    raw_source = str(data.get("raw_source") or "").strip()
    is_raw_pro_sample = source in OPENWB_PRO_SOURCES
    is_bound_pro_estimate = bool(
        source.startswith(ESTIMATED_PREFIX)
        and raw_source in OPENWB_PRO_SOURCES
        and data.get("soc_profile_bound") is True
        and data.get("soc_profile_binding_invalid") is not True
        and data.get("estimate_expired") is not True
        and str(data.get("plug_session_id") or "").strip() == plug_session_id
    )
    if not (is_raw_pro_sample or is_bound_pro_estimate):
        return None
    sample_source = source if is_raw_pro_sample else raw_source
    rule_confirmed = data.get("soc_rule_confirmed")
    if (
        not _is_confirmed_soc_source(sample_source)
        or (rule_confirmed is not None and not _truthy(rule_confirmed))
    ):
        return None

    soc = _safe_float(
        data.get("soc") if is_raw_pro_sample else data.get("raw_soc"),
        -1.0,
    )
    sample_ts = _timestamp(data.get("raw_soc_ts", data.get("ts")), 0.0)
    if (
        soc <= 0.0
        or sample_ts <= 0.0
        or sample_ts + 1.0 < plug_session_started_ts
        or sample_ts > float(now) + 300.0
    ):
        return None

    aliases = {
        compact
        for compact in (
            _compact_id(data.get(key))
            for key in ("profile_id", "car_id", "vehicle_id", "rfid_tag")
        )
        if compact
    }
    if not aliases or not aliases.issubset(profile_aliases):
        return None

    anchor_kwh = _safe_float(
        data.get("session_kwh")
        if is_raw_pro_sample
        else data.get("anchor_session_kwh"),
        -1.0,
    )
    anchor_meter_wh = anchor_kwh * 1000.0
    if anchor_kwh < 0.0 or meter_wh is None or meter_wh + 0.1 < anchor_meter_wh:
        return None

    return {
        "soc": _clamp_percent(soc),
        "ts": sample_ts,
        "source": sample_source,
        "car_id": profile.get("id") or "",
        "vehicle_id": "",
        "name": profile.get("name") or str(data.get("name") or "").strip(),
        "capacity_kwh": _safe_float(profile.get("capacity_kwh"), 0.0),
        "profile_id": profile.get("id") or "",
        "soc_profile_bound": True,
        "anchor_meter_wh": anchor_meter_wh,
    }


def _openwb_pro_profile_binding(config, wb_id, status, selected_id, now=None):
    """Liefere eine fail-closed Profil-/Live-SoC-Bindung für openWB Pro.

    Die openWB Pro liefert nicht auf jeder Anlage eine nutzbare Fahrzeug-ID.
    Dann darf das explizit konfigurierte Wallboxprofil nur verwendet werden,
    wenn es eindeutig ist und ein bestätigter SoC-Anker aus derselben
    Stecksession oder genau ein frischer Live-Datensatz über einen Profilalias
    gebunden ist. Das Ergebnis bleibt bewusst nur profilgebunden und behauptet
    keine von der Pro gemeldete stabile Identität.
    """

    now = time.time() if now is None else float(now)
    status = status or {}
    if (
        status.get("driver_status_valid") is not True
        or status.get("driver_status_stale") is True
        or status.get("driver_status_degraded") is True
        or not _truthy(status.get("plug_state"))
    ):
        return None
    if not _fresh_timestamp(
        status.get("driver_status_last_sample_ts"),
        now,
        OPENWB_PRO_STATUS_MAX_AGE_S,
    ):
        return None
    plug_session_id = str(status.get("plug_session_id") or "").strip()
    if not plug_session_id:
        return None
    plug_session_started_ts = _plug_session_started_ts(plug_session_id, now)
    meter_wh = _openwb_pro_session_meter_wh(status)
    if not plug_session_started_ts or meter_wh is None:
        return None

    saved_car = _unique_saved_profile(selected_id)
    if not saved_car:
        return None
    profile_aliases = _compact_aliases(saved_car)
    if not profile_aliases:
        return None

    # Eine aktuelle oder erhaltene explizite Pro-ID darf dem Profil nie
    # widersprechen. Eine leere ID ist erlaubt und begründet diesen Fallback.
    for key in LIVE_STATUS_ID_KEYS:
        live_id = _compact_id(status.get(key))
        if live_id and live_id not in profile_aliases:
            return None

    profile = _profile_for(config, wb_id, selected_id)
    session_sample = _openwb_pro_same_session_sample(
        wb_id,
        profile,
        profile_aliases,
        plug_session_id,
        plug_session_started_ts,
        meter_wh,
        now,
    )
    if session_sample:
        return {
            "sample": session_sample,
            "profile": profile,
            "plug_session_id": plug_session_id,
            "meter_wh": meter_wh,
            "meter_source": "session_kwh",
        }

    candidates = []
    for vehicle in _load_live_vehicles():
        if not isinstance(vehicle, dict):
            continue
        vehicle_aliases = _compact_aliases(vehicle)
        if not vehicle_aliases.intersection(profile_aliases):
            continue
        if not _truthy(vehicle.get("is_plugged_in", vehicle.get("plugged"))):
            continue
        try:
            vehicle_slot = int(vehicle.get("wb_slot") or 0)
        except (TypeError, ValueError):
            vehicle_slot = 0
        if vehicle_slot not in (0, int(wb_id)):
            continue

        # Zusätzliche typisierte Live-IDs müssen ebenfalls zum Profil gehören.
        strong_live_aliases = _compact_aliases(
            vehicle,
            ("cloud_vehicle_id", "vehicle_id", "vehicle_mac", "mac", "rfid", "rfid_tag"),
        )
        if strong_live_aliases and not strong_live_aliases.issubset(profile_aliases):
            continue
        soc = _safe_float(vehicle.get("soc", vehicle.get("battery_soc")), -1.0)
        source = str(vehicle.get("soc_source") or vehicle.get("source") or "").strip()
        sample_ts = _fresh_timestamp(
            vehicle.get(
                "last_updated_at",
                vehicle.get("updated_at", vehicle.get("ts", vehicle.get("timestamp"))),
            ),
            now,
            OPENWB_PRO_VEHICLE_SOC_MAX_AGE_S,
        )
        if soc <= 0.0 or not sample_ts or not _is_confirmed_soc_source(source):
            continue
        candidates.append((vehicle, soc, source, sample_ts))

    if len(candidates) != 1:
        return None

    vehicle, soc, source, sample_ts = candidates[0]
    live_vehicle_id = ""
    for key in ("vehicle_id", "cloud_vehicle_id", "id", "rfid", "rfid_tag"):
        probe = str(vehicle.get(key) or "").strip()
        if probe and _compact_id(probe) in profile_aliases:
            live_vehicle_id = probe
            break
    sample = {
        "soc": _clamp_percent(soc),
        "ts": sample_ts,
        "source": source,
        # Die Profil-ID wird separat gespeichert, ohne sie als Pro-Live-ID
        # auszugeben.
        "car_id": profile.get("id") or str(selected_id or "").strip(),
        "vehicle_id": live_vehicle_id,
        "name": profile.get("name") or str(vehicle.get("name") or "").strip(),
        "capacity_kwh": _safe_float(profile.get("capacity_kwh"), 0.0),
        "profile_id": profile.get("id") or str(selected_id or "").strip(),
        "soc_profile_bound": True,
        # Ein Cloud-SoC ist eine aktuelle Verankerung. Frühere Energie aus
        # derselben Stecksession darf nicht nachträglich addiert werden.
        "anchor_meter_wh": meter_wh,
    }
    return {
        "sample": sample,
        "profile": profile,
        "plug_session_id": plug_session_id,
        "meter_wh": meter_wh,
        "meter_source": "session_kwh",
    }


def _profile_for(config, wb_id, selected_id, fallback_name=""):
    profile = {
        "id": selected_id or "",
        "name": "",
        "capacity_kwh": _safe_float(config.get(f"wb{wb_id}_capacity"), 0.0),
        "efficiency": 0.90,
        "consumption_kwh_100km": 0.0,
    }
    saved_cars = _load_saved_cars()
    for car in saved_cars:
        if not isinstance(car, dict) or not _matches_vehicle(car, selected_id):
            continue
        profile["id"] = str(car.get("id") or selected_id or "").strip()
        profile["name"] = str(car.get("name") or "").strip()
        capacity = _safe_float(car.get("capacity", car.get("capacity_kwh")), profile["capacity_kwh"])
        if capacity > 0:
            profile["capacity_kwh"] = capacity
        efficiency = _safe_float(
            car.get("efficiency", car.get("charge_efficiency", car.get("charging_efficiency"))),
            profile["efficiency"],
        )
        if efficiency > 1.0:
            efficiency = efficiency / 100.0
        profile["efficiency"] = max(0.50, min(1.00, efficiency or 0.90))
        consumption = _safe_float(
            car.get("consumption", car.get("consumption_kwh_100km", car.get("avg_consumption"))),
            profile["consumption_kwh_100km"],
        )
        if consumption > 0:
            profile["consumption_kwh_100km"] = consumption
        for key in ("cloud_vehicle_id", "vehicle_id", "vehicle_mac", "mac", "rfid", "rfid_tag"):
            if car.get(key):
                profile[key] = car.get(key)
        break
    else:
        fallback_norm = str(fallback_name or "").strip().lower()
        if fallback_norm:
            for car in saved_cars:
                if not isinstance(car, dict):
                    continue
                car_name = str(car.get("name") or "").strip()
                car_norm = car_name.lower()
                if not car_norm:
                    continue
                if car_norm == fallback_norm or car_norm in fallback_norm or fallback_norm in car_norm:
                    profile["id"] = str(car.get("id") or selected_id or "").strip()
                    profile["name"] = car_name
                    capacity = _safe_float(car.get("capacity", car.get("capacity_kwh")), profile["capacity_kwh"])
                    if capacity > 0:
                        profile["capacity_kwh"] = capacity
                    efficiency = _safe_float(
                        car.get("efficiency", car.get("charge_efficiency", car.get("charging_efficiency"))),
                        profile["efficiency"],
                    )
                    if efficiency > 1.0:
                        efficiency = efficiency / 100.0
                    profile["efficiency"] = max(0.50, min(1.00, efficiency or 0.90))
                    consumption = _safe_float(
                        car.get("consumption", car.get("consumption_kwh_100km", car.get("avg_consumption"))),
                        profile["consumption_kwh_100km"],
                    )
                    if consumption > 0:
                        profile["consumption_kwh_100km"] = consumption
                    for key in ("cloud_vehicle_id", "vehicle_id", "vehicle_mac", "mac", "rfid", "rfid_tag"):
                        if car.get(key):
                            profile[key] = car.get(key)
                    break
    return profile


def _status_connected(status):
    if not isinstance(status, dict):
        return False
    if bool(status.get("plug_state", False)):
        return True
    if bool(status.get("car_connected_rscp", False)):
        return True
    try:
        return int(status.get("car", 1) or 1) >= 2
    except Exception:
        return False


def _status_power_w(status):
    if not isinstance(status, dict):
        return 0.0
    phase_power = _safe_float(status.get("phase_power_sum_w"), 0.0)
    if bool(status.get("phase_power_verified", False)) and phase_power > 50.0:
        return phase_power
    if not bool(status.get("charging", False) or status.get("charge_state", False)):
        return 0.0
    return max(_safe_float(status.get("real_power_w"), 0.0), _safe_float(status.get("power_w"), 0.0))


def _status_meter_wh(status):
    if not isinstance(status, dict):
        return None, ""
    for key in ("imported_total_wh", "daily_imported_wh"):
        value = _safe_float(status.get(key), -1.0)
        if value >= 0.0:
            return value, key
    if status.get("session_kwh") is not None:
        value = _safe_float(status.get("session_kwh"), -1.0)
        if value >= 0.0:
            return value * 1000.0, "session_kwh"
    return None, ""


def _manual_soc_path(wb_id):
    return os.path.join(RAMDISK_DIR, f"manual_soc_wb{int(wb_id)}.json")


def _legacy_manual_soc_path(wb_id):
    if int(wb_id) != 1:
        return ""
    return os.path.join(TMP_DIR, "manual_soc.json")


def _read_manual_soc(wb_id, default=None):
    data = _read_json(_manual_soc_path(wb_id), None)
    if isinstance(data, dict):
        return data
    legacy_path = _legacy_manual_soc_path(wb_id)
    if legacy_path:
        legacy = _read_json(legacy_path, None)
        if isinstance(legacy, dict):
            return legacy
    return default


def _tracker_state_path(wb_id):
    return os.path.join(RAMDISK_DIR, f"vehicle_soc_tracker_wb{int(wb_id)}.json")


def _tracker_checkpoint_path(wb_id):
    data_dir = os.path.dirname(SAVED_CARS_FILE) or "/var/www/html/data"
    return os.path.join(data_dir, f"vehicle_soc_tracker_checkpoint_wb{int(wb_id)}.json")


def _persist_tracker_checkpoint(wb_id, state, runtime_state, *, now_ts=None, force=False):
    current_ts = time.time() if now_ts is None else float(now_ts)
    connected = bool((state or {}).get("connected", False))
    charging = bool((state or {}).get("charging", False))
    session_closed = bool((state or {}).get("session_closed", False))
    heartbeat_s = (
        TRACKER_CHECKPOINT_ACTIVE_HEARTBEAT_S
        if connected and charging
        else TRACKER_CHECKPOINT_IDLE_HEARTBEAT_S
        if connected and not session_closed
        else None
    )
    signature = json.dumps(
        {
            key: (state or {}).get(key)
            for key in TRACKER_CHECKPOINT_SEMANTIC_KEYS
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    last_write_ts = float(runtime_state.get("last_write_ts", 0.0) or 0.0)
    elapsed_s = current_ts - last_write_ts
    if not (
        force
        or signature != runtime_state.get("signature")
        or last_write_ts <= 0.0
        or elapsed_s < 0.0
        or (heartbeat_s is not None and elapsed_s >= heartbeat_s)
    ):
        return False
    checkpoint = dict(state or {})
    checkpoint["schema_version"] = "vehicle_soc_tracker_checkpoint_v1"
    checkpoint["checkpoint_ts"] = current_ts
    if not _write_json_atomic(_tracker_checkpoint_path(wb_id), checkpoint):
        return False
    runtime_state["signature"] = signature
    runtime_state["last_write_ts"] = current_ts
    return True


class VehicleSocTracker:
    def __init__(self):
        self._states = {}
        self._checkpoint_runtime = {}

    def _load_state(self, wb_id):
        key = int(wb_id)
        if key not in self._states:
            state = _read_json(_tracker_state_path(key), None)
            if not isinstance(state, dict):
                state = _read_json(_tracker_checkpoint_path(key), {})
            if not isinstance(state, dict):
                state = {}
            state.pop("schema_version", None)
            state.pop("checkpoint_ts", None)
            self._states[key] = state
        return self._states[key]

    def _save_state(self, wb_id, state):
        key = int(wb_id)
        self._states[key] = dict(state or {})
        _write_json_atomic(_tracker_state_path(key), self._states[key])
        _persist_tracker_checkpoint(
            key,
            self._states[key],
            self._checkpoint_runtime.setdefault(key, {}),
        )

    def _session_anchor_expired(self, state, now, meter_wh):
        source = str((state or {}).get("anchor_source") or "").strip()
        if source not in SESSION_SCOPED_ANCHOR_SOURCES:
            return False
        if (state or {}).get("anchor_meter_wh") is not None or meter_wh is not None:
            return False
        anchor_ts = _timestamp((state or {}).get("anchor_sample_ts"), 0.0)
        return anchor_ts > 0.0 and now - anchor_ts > UNMETERED_SESSION_ANCHOR_MAX_AGE_S

    def _expire_session_anchor(self, wb_id, state, now, connected, charging):
        expired_soc = _clamp_percent(_safe_float((state or {}).get("anchor_soc"), 0.0))
        expired_payload = {
            "soc": expired_soc,
            "source": f"{ESTIMATED_PREFIX}_expired",
            "soc_rule_confirmed": False,
            "raw_soc": expired_soc,
            "raw_source": str((state or {}).get("anchor_source") or "start_soc"),
            "raw_soc_ts": int(_timestamp((state or {}).get("anchor_sample_ts"), now)),
            "car_id": (state or {}).get("car_id") or "",
            "vehicle_id": (state or {}).get("vehicle_id") or "",
            "name": (state or {}).get("name") or "",
            "capacity": _safe_float((state or {}).get("capacity_kwh"), 0.0),
            "wb": int(wb_id),
            "plugged": connected,
            "charging": charging,
            "is_interpolated": False,
            "estimate_expired": True,
            "expired_session_kwh": round(_safe_float((state or {}).get("power_integrated_wh"), 0.0) / 1000.0, 3),
            "ts": int(now),
        }
        self._write_manual_soc(wb_id, expired_payload)
        self._save_state(wb_id, {
            "wb": int(wb_id),
            "vehicle_key": (state or {}).get("vehicle_key") or "",
            "car_id": (state or {}).get("car_id") or "",
            "vehicle_id": (state or {}).get("vehicle_id") or "",
            "name": (state or {}).get("name") or "",
            "connected": connected,
            "charging": charging,
            "last_update_ts": now,
            "estimate_expired": True,
            "expired_anchor_source": expired_payload["raw_source"],
            "expired_anchor_ts": expired_payload["raw_soc_ts"],
            "expired_session_kwh": expired_payload["expired_session_kwh"],
        })

    def _manual_sample(self, wb_id, selected_id):
        data = _read_manual_soc(wb_id, None)
        if not isinstance(data, dict):
            return None
        source = str(data.get("source") or "manual_start_soc").strip()
        if source.startswith(ESTIMATED_PREFIX) or source in OPENWB_PRO_SOURCES:
            return None
        if not _is_confirmed_soc_source(source):
            return None
        sample_ts = _timestamp(data.get("raw_soc_ts", data.get("ts")), time.time())
        if source in CONFIRMED_MANUAL_SOC_SOURCES and sample_ts > 0.0:
            if time.time() - sample_ts > UNMETERED_SESSION_ANCHOR_MAX_AGE_S:
                return None
        soc = _safe_float(data.get("soc"), -1.0)
        if soc <= 0:
            return None
        car_id = str(data.get("car_id") or data.get("name") or "").strip()
        vehicle_id = str(data.get("vehicle_id") or "").strip()
        if selected_id and selected_id.lower() not in NO_VEHICLE_IDS:
            probes = [car_id, vehicle_id]
            if car_id and car_id != selected_id and _compact_id(car_id) != _compact_id(selected_id):
                if vehicle_id and _compact_id(vehicle_id) != _compact_id(selected_id):
                    return None
                if not vehicle_id and car_id not in ("none", "Gast-Fahrzeug"):
                    return None
        return {
            "soc": _clamp_percent(soc),
            "ts": sample_ts,
            "source": source,
            "car_id": car_id or selected_id,
            "vehicle_id": vehicle_id,
            "name": str(data.get("name") or "").strip(),
            "capacity_kwh": _safe_float(data.get("capacity"), 0.0),
        }

    def _vehicle_sample(self, selected_id):
        if not selected_id or selected_id.lower() in NO_VEHICLE_IDS:
            return None
        now = time.time()
        for vehicle in _load_live_vehicles():
            if not isinstance(vehicle, dict) or not _matches_vehicle(vehicle, selected_id):
                continue
            soc = _safe_float(vehicle.get("soc", vehicle.get("battery_soc")), -1.0)
            ts = _timestamp(
                vehicle.get("last_updated_at", vehicle.get("updated_at", vehicle.get("ts", vehicle.get("timestamp")))),
                0.0,
            )
            if soc <= 0 or (ts > 0 and now - ts > 8 * 3600):
                return None
            source = str(vehicle.get("soc_source") or vehicle.get("source") or "vehicle_soc").strip()
            if not _is_confirmed_soc_source(source):
                return None
            return {
                "soc": _clamp_percent(soc),
                "ts": ts or now,
                "source": source,
                "car_id": str(vehicle.get("id") or selected_id).strip(),
                "vehicle_id": str(vehicle.get("vehicle_id") or vehicle.get("cloud_vehicle_id") or "").strip(),
                "name": str(vehicle.get("name") or "").strip(),
                "capacity_kwh": _safe_float(vehicle.get("capacity_kwh", vehicle.get("capacity")), 0.0),
            }
        return None

    def _config_sample(self, config, wb_id, selected_id):
        return None

    def _best_anchor_sample(self, wb_id, config, status, selected_id):
        for sample in (
            self._manual_sample(wb_id, selected_id),
            self._vehicle_sample(selected_id),
            self._config_sample(config, wb_id, selected_id),
        ):
            if sample:
                return sample
        status_soc = _safe_float((status or {}).get("car_soc"), -1.0)
        if status_soc > 0:
            source = str((status or {}).get("car_soc_source") or "wallbox_status").strip()
            if (
                source not in OPENWB_PRO_SOURCES
                and not source.startswith(ESTIMATED_PREFIX)
                and _is_confirmed_soc_source(source)
            ):
                return {
                    "soc": _clamp_percent(status_soc),
                    "ts": _timestamp((status or {}).get("car_soc_raw_ts"), time.time()),
                    "source": source,
                    "car_id": str((status or {}).get("car_id") or selected_id or "").strip(),
                    "vehicle_id": str((status or {}).get("vehicle_id") or "").strip(),
                    "name": str((status or {}).get("car_name") or "").strip(),
                    "capacity_kwh": _safe_float((status or {}).get("car_capacity_kwh"), 0.0),
                }
        return None

    def update(self, wb_id, config, status, charger_class=""):
        now = time.time()
        wb_id = int(wb_id)
        config = config or {}
        status = status or {}
        source_status = str(status.get("car_soc_source") or "").strip()
        is_openwb_pro = str(charger_class or "") == "OpenWBProCharger"
        status_soc = _safe_float(status.get("car_soc"), -1.0)
        # Ein bestätigter Roh-/Treiberschätzwert bleibt autoritativ. Der
        # generische Fallback überschreibt weder Status noch Manual-SoC-Datei.
        if (
            source_status in OPENWB_PRO_SOURCES
            and status_soc > 0.0
            and _is_confirmed_soc_source(source_status)
        ):
            return None

        selected_id = str(config.get(f"wb{wb_id}_car_id") or "").strip()
        if selected_id.lower() in NO_VEHICLE_IDS:
            selected_id = ""
        profile_binding = (
            _openwb_pro_profile_binding(
                config,
                wb_id,
                status,
                selected_id,
                now=now,
            )
            if is_openwb_pro
            else None
        )
        if is_openwb_pro:
            if not profile_binding:
                self._invalidate_profile_fallback(
                    wb_id,
                    connected=_status_connected(status),
                    reason="profile_binding_invalid",
                )
                return None
            sample = profile_binding["sample"]
            profile = profile_binding["profile"]
        else:
            sample = self._best_anchor_sample(wb_id, config, status, selected_id)
            fallback_name = str(
                (sample or {}).get("name")
                or status.get("car_name")
                or status.get("charge_template_name")
                or ""
            ).strip()
            profile = _profile_for(
                config,
                wb_id,
                selected_id,
                fallback_name=fallback_name,
            )
        capacity = _safe_float(profile.get("capacity_kwh"), 0.0)
        efficiency = _safe_float(profile.get("efficiency"), 0.90)
        efficiency = max(0.50, min(1.00, efficiency or 0.90))
        consumption = _safe_float(profile.get("consumption_kwh_100km"), 0.0)
        if sample and _safe_float(sample.get("capacity_kwh"), 0.0) > 0:
            capacity = _safe_float(sample.get("capacity_kwh"), capacity)

        state = self._load_state(wb_id)
        connected = _status_connected(status)
        charging = _status_power_w(status) > 500.0
        if profile_binding:
            meter_wh = profile_binding.get("meter_wh")
            meter_source = str(profile_binding.get("meter_source") or "")
        else:
            meter_wh, meter_source = _status_meter_wh(status)
        plug_session_id = (
            str(profile_binding.get("plug_session_id") or "").strip()
            if profile_binding
            else ""
        )
        active_car_id = str((sample or {}).get("car_id") or profile.get("id") or selected_id or "").strip()
        vehicle_key = _compact_id(active_car_id or (sample or {}).get("vehicle_id") or selected_id or f"wb{wb_id}")
        meter_reset = (
            meter_wh is not None
            and state.get("anchor_meter_wh") is not None
            and meter_wh + 100.0 < _safe_float(state.get("anchor_meter_wh"), 0.0)
        )
        car_changed = bool(state.get("vehicle_key") and vehicle_key and state.get("vehicle_key") != vehicle_key)
        plug_session_changed = bool(
            is_openwb_pro
            and state.get("plug_session_id")
            and plug_session_id
            and state.get("plug_session_id") != plug_session_id
        )
        source_newer = False
        if sample:
            source_newer = _timestamp(sample.get("ts"), 0.0) > _timestamp(state.get("anchor_sample_ts"), 0.0) + 1.0
        needs_anchor = (
            not state.get("anchor_soc")
            or (is_openwb_pro and (not state.get("soc_profile_bound") or not state.get("plug_session_id")))
            or not connected
            or car_changed
            or plug_session_changed
            or meter_reset
            or (sample is not None and source_newer)
        )

        if not connected:
            if state:
                self._mark_manual_unplugged(wb_id, state)
                self._save_state(wb_id, {
                    "wb": wb_id,
                    "vehicle_key": state.get("vehicle_key") or "",
                    "car_id": state.get("car_id") or "",
                    "vehicle_id": state.get("vehicle_id") or "",
                    "name": state.get("name") or "",
                    "connected": False,
                    "charging": False,
                    "last_update_ts": now,
                    "session_closed": True,
                    "closed_anchor_source": state.get("anchor_source") or "",
                    "closed_anchor_ts": int(_timestamp(state.get("anchor_sample_ts"), now)),
                    "closed_session_kwh": round(_safe_float(state.get("power_integrated_wh"), 0.0) / 1000.0, 3),
                })
            return None

        if needs_anchor and sample:
            state = {
                "wb": wb_id,
                "vehicle_key": vehicle_key,
                "car_id": active_car_id,
                "vehicle_id": str(sample.get("vehicle_id") or profile.get("vehicle_id") or "").strip(),
                "name": str(sample.get("name") or profile.get("name") or active_car_id or "").strip(),
                "anchor_soc": _clamp_percent(sample.get("soc")),
                "anchor_sample_ts": _timestamp(sample.get("ts"), now),
                "anchor_source": str(sample.get("source") or "start_soc"),
                "anchor_meter_wh": (
                    _safe_float(sample.get("anchor_meter_wh"), meter_wh)
                    if sample.get("anchor_meter_wh") is not None
                    else meter_wh
                ),
                "last_meter_wh": meter_wh,
                "meter_source": meter_source,
                "power_integrated_wh": 0.0,
                "last_update_ts": now,
                "capacity_kwh": capacity,
                "efficiency": efficiency,
                "consumption_kwh_100km": consumption,
                "profile_id": str(sample.get("profile_id") or profile.get("id") or "").strip(),
                "soc_profile_bound": bool(sample.get("soc_profile_bound", False)),
                "plug_session_id": plug_session_id,
            }
        elif not state.get("anchor_soc"):
            return None

        if self._session_anchor_expired(state, now, meter_wh):
            self._expire_session_anchor(wb_id, state, now, connected, charging)
            return None

        last_update = _timestamp(state.get("last_update_ts"), now)
        if charging and now > last_update:
            dt_s = min(max(0.0, now - last_update), 300.0)
            power_w = _status_power_w(status)
            if power_w > 50.0 and dt_s > 0.0:
                state["power_integrated_wh"] = _safe_float(state.get("power_integrated_wh"), 0.0) + power_w * dt_s / 3600.0

        meter_delta_wh = 0.0
        anchor_meter = state.get("anchor_meter_wh")
        if meter_wh is not None and anchor_meter is not None and meter_wh >= _safe_float(anchor_meter, 0.0):
            meter_delta_wh = max(0.0, meter_wh - _safe_float(anchor_meter, 0.0))
            state["last_meter_wh"] = meter_wh
            state["meter_source"] = meter_source
        delivered_wh = max(meter_delta_wh, _safe_float(state.get("power_integrated_wh"), 0.0))

        state["connected"] = connected
        state["charging"] = charging
        state["last_update_ts"] = now
        state["capacity_kwh"] = capacity
        state["efficiency"] = efficiency
        state["consumption_kwh_100km"] = consumption
        self._save_state(wb_id, state)

        if capacity <= 0:
            return None

        estimated_soc = _clamp_percent(_safe_float(state.get("anchor_soc"), 0.0) + (delivered_wh / 1000.0) * efficiency / capacity * 100.0)
        raw_source = str(state.get("anchor_source") or "start_soc")
        source = f"{ESTIMATED_PREFIX}_from_{raw_source}" if delivered_wh > 20.0 else raw_source
        result = {
            "soc": round(estimated_soc, 1),
            "source": source,
            "soc_rule_confirmed": _is_confirmed_soc_source(raw_source),
            "raw_soc": round(_safe_float(state.get("anchor_soc"), estimated_soc), 1),
            "raw_source": raw_source,
            "raw_soc_ts": int(_timestamp(state.get("anchor_sample_ts"), now)),
            # Bei einer Profilbindung ohne Pro-Live-ID bleibt die Runtime-ID
            # leer. Das Profil wird separat transportiert und ist keine
            # Behauptung einer stabilen, von der Wallbox gelesenen Identität.
            "car_id": (
                str(status.get("car_id") or "").strip()
                if bool(state.get("soc_profile_bound", False))
                else state.get("car_id") or active_car_id
            ),
            "vehicle_id": (
                str(status.get("vehicle_id") or "").strip()
                if bool(state.get("soc_profile_bound", False))
                else state.get("vehicle_id") or ""
            ),
            "name": state.get("name") or profile.get("name") or active_car_id,
            "capacity": capacity,
            "wb": wb_id,
            "plugged": connected,
            "charging": charging,
            "is_interpolated": delivered_wh > 20.0,
            "profile_id": state.get("profile_id") or "",
            "soc_profile_bound": bool(state.get("soc_profile_bound", False)),
            # Diese Zuordnung dient ausschließlich SoC/Anzeige/Planung. Sie
            # beweist weder die aktuelle Stecksession noch eine OBC-Grenze.
            "identity_scope": "soc_profile_only",
            "stable_vehicle_identity_current": False,
            "plug_session_id": state.get("plug_session_id") or "",
            "session_kwh": round(
                meter_wh / 1000.0
                if state.get("meter_source") == "session_kwh"
                and meter_wh is not None
                else delivered_wh / 1000.0,
                3,
            ),
            "ts": int(now),
        }
        if (
            state.get("meter_source") == "session_kwh"
            and state.get("anchor_meter_wh") is not None
        ):
            result["anchor_session_kwh"] = round(
                _safe_float(state.get("anchor_meter_wh"), 0.0) / 1000.0,
                3,
            )
        explicit_total_range = current_explicit_openwb_total_range(status, now_ts=now)
        explicit_charged_range = current_explicit_openwb_charged_range(status, now_ts=now)
        if consumption > 0:
            result["consumption_kwh_100km"] = consumption
        if explicit_total_range:
            result.update(explicit_total_range)
        elif consumption > 0:
            result["range_km"] = round((capacity * estimated_soc / 100.0) / consumption * 100.0, 0)
            result["range_source"] = "wallbox_estimated_consumption"
            result["range_explicit"] = False
        if explicit_charged_range:
            result["charged_range_km"] = explicit_charged_range["range_km"]
            result["charged_range_source"] = explicit_charged_range["range_source"]
            result["charged_range_observed_ts"] = explicit_charged_range["range_observed_ts"]
            result["charged_range_source_ts"] = explicit_charged_range["range_source_ts"]
            result["charged_range_source_ts_explicit"] = explicit_charged_range[
                "range_source_ts_explicit"
            ]
            result["charged_range_vehicle_key"] = explicit_charged_range["range_vehicle_key"]
            result["charged_range_explicit"] = True
        elif consumption > 0:
            result["charged_range_km"] = round(max(0.0, (delivered_wh / 1000.0) * efficiency / consumption * 100.0), 1)
            result["charged_range_source"] = "wallbox_estimated_consumption"
            result["charged_range_explicit"] = False
        self._write_manual_soc(wb_id, result)
        return result

    def _write_manual_soc(self, wb_id, payload):
        path = _manual_soc_path(wb_id)
        _write_json_atomic(path, payload)

    def _invalidate_profile_fallback(self, wb_id, connected, reason):
        """Sperre eine nicht mehr belastbar gebundene Pro-/Profil-Schätzung."""

        data = _read_manual_soc(wb_id, None)
        if not isinstance(data, dict):
            return
        source = str(data.get("source") or "").strip()
        if (
            source not in OPENWB_PRO_SOURCES
            and not bool(data.get("soc_profile_bound", False))
        ):
            return
        data.update({
            "source": f"{ESTIMATED_PREFIX}_{str(reason or 'invalid')}",
            "soc_rule_confirmed": False,
            "plugged": bool(connected),
            "charging": False,
            "is_interpolated": False,
            "estimate_expired": True,
            "soc_profile_binding_invalid": True,
            "ts": int(time.time()),
        })
        self._write_manual_soc(wb_id, data)

    def _mark_manual_unplugged(self, wb_id, state):
        data = _read_manual_soc(wb_id, None)
        if not isinstance(data, dict):
            return
        source = str(data.get("source") or "")
        data_ts = _timestamp(data.get("raw_soc_ts", data.get("ts")), 0.0)
        state_ts = _timestamp((state or {}).get("anchor_sample_ts"), 0.0)
        if not source.startswith(ESTIMATED_PREFIX) and data_ts > state_ts + 1.0:
            return
        raw_source = str(data.get("raw_source") or (state or {}).get("anchor_source") or source or "")
        raw_soc = _clamp_percent(_safe_float(data.get("raw_soc", (state or {}).get("anchor_soc", data.get("soc"))), 0.0))
        data.update({
            "soc": raw_soc,
            "source": f"{ESTIMATED_PREFIX}_unplugged",
            "soc_rule_confirmed": False,
            "raw_soc": raw_soc,
            "raw_source": raw_source,
            "raw_soc_ts": int(_timestamp((state or {}).get("anchor_sample_ts"), data_ts or time.time())),
            "plugged": False,
            "charging": False,
            "session_closed": True,
            "ts": int(time.time()),
        })
        self._write_manual_soc(wb_id, data)
