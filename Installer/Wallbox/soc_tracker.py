"""
Central per-wallbox vehicle SoC tracker.

The tracker keeps one estimation path for all wallbox types:
- use a fresh confirmed wallbox/vehicle/manual SoC as anchor
- add measured wallbox energy while the car is connected
- derive vehicle range from the saved capacity/consumption profile
- write manual_soc_wbX.json so scheduler and UI see the same value

openWB Pro keeps its own CCS/import counter estimator in the driver. Fresh
values stay authoritative; an expired raw anchor may be replaced by a newer,
unambiguously profile-bound vehicle value.
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
VEHICLE_SOC_AGE_CONTRACT = "vehicle_soc_source_age_v1"
CONFIRMED_MANUAL_SOC_MAX_AGE_S = 36 * 3600
CONFIRMED_MANUAL_SOC_SOURCES = frozenset({
    "manual_start_soc", "manual_soc", "manual", "openwb_profile_link",
})
UNCONFIRMED_SOC_SOURCES = ("simple_view_start_soc", "config_start_soc")
UNTRUSTED_SOC_SOURCE_TOKENS = (
    "retained",
    "legacy",
    "unknown",
    "cached",
    "expired",
    "invalid",
)
OPENWB_PRO_STATUS_MAX_AGE_S = 60
OPENWB_PRO_VEHICLE_SOC_MAX_AGE_S = 8 * 3600
VEHICLE_CLOUD_SOC_SOURCES = frozenset({
    "bluelink", "hyundai", "kia", "cloud", "vehicle_cloud",
})
VEHICLE_MACHINE_SOC_SOURCES = frozenset({
    "mqtt",
    "openwb_mqtt",
    "openwb_http",
    "openwb_pro_raw",
    "openwb_pro_estimated",
})
VEHICLE_DIRECT_SOC_SOURCES = frozenset(
    set(CONFIRMED_MANUAL_SOC_SOURCES)
    | set(VEHICLE_CLOUD_SOC_SOURCES)
    | set(VEHICLE_MACHINE_SOC_SOURCES)
)
VEHICLE_ESTIMATED_SOURCE_PREFIX = "wallbox_estimated_from_"
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


def vehicle_soc_source_contract(source):
    """Löst ausschließlich bekannte Fahrzeug-SoC-Quellen fail-closed auf.

    Freie Präfix- oder Teilstringfreigaben sind absichtlich unzulässig. Eine
    meterbasierte Schätzung darf genau einen bekannten Rohanker benennen; sie
    ist selbst immer eine Maschinenquelle mit höchstens acht Stunden Alter.
    """

    text = str(source or "").strip().lower()
    if (
        not text
        or text in UNCONFIRMED_SOC_SOURCES
        or any(token in text for token in UNTRUSTED_SOC_SOURCE_TOKENS)
    ):
        return None

    derived = text.startswith(VEHICLE_ESTIMATED_SOURCE_PREFIX)
    base_source = (
        text[len(VEHICLE_ESTIMATED_SOURCE_PREFIX):]
        if derived
        else text
    )
    if (
        not base_source
        or base_source.startswith(VEHICLE_ESTIMATED_SOURCE_PREFIX)
        or base_source not in VEHICLE_DIRECT_SOC_SOURCES
        or (text.startswith(ESTIMATED_PREFIX) and not derived)
    ):
        return None

    if derived:
        kind = "estimated"
        max_age_class = "machine"
    elif base_source in CONFIRMED_MANUAL_SOC_SOURCES:
        kind = "manual"
        max_age_class = "manual"
    elif base_source in VEHICLE_CLOUD_SOC_SOURCES:
        kind = "cloud"
        max_age_class = "cloud"
    elif base_source in ("mqtt", "openwb_mqtt"):
        kind = "mqtt"
        max_age_class = "machine"
    else:
        kind = "wallbox"
        max_age_class = "machine"

    return {
        "source": text,
        "base_source": base_source,
        "kind": kind,
        "derived": derived,
        "legacy": base_source in {
            "manual_soc", "manual", "hyundai", "kia", "cloud",
            "vehicle_cloud",
        },
        "max_age_class": max_age_class,
    }


def vehicle_soc_source_trusted(source):
    return vehicle_soc_source_contract(source) is not None


def _is_confirmed_soc_source(source):
    """Kompatibilitätsname für den zentralen exakten Quellenvertrag."""

    return vehicle_soc_source_trusted(source)


def vehicle_soc_max_age_s(source, config=None):
    """Kanonischer Altersvertrag für bestätigte Fahrzeug-SoC-Quellen."""

    contract = vehicle_soc_source_contract(source)
    if contract is None:
        return 0.0
    if contract["max_age_class"] == "manual":
        return float(CONFIRMED_MANUAL_SOC_MAX_AGE_S)
    if contract["max_age_class"] != "cloud":
        return float(OPENWB_PRO_VEHICLE_SOC_MAX_AGE_S)
    config = config if isinstance(config, dict) else {}
    try:
        interval_min = max(
            5.0,
            float(config.get("bluelink_interval", 15) or 15),
        )
    except (TypeError, ValueError):
        interval_min = 15.0
    return float(min(
        OPENWB_PRO_VEHICLE_SOC_MAX_AGE_S,
        max(15 * 60, int(round(interval_min * 60.0)) + 300),
    ))


def vehicle_soc_age_contract(source, config=None):
    """Liefert den sprachübergreifend transportierten SoC-Altersvertrag."""

    source_contract = vehicle_soc_source_contract(source)
    if source_contract is None:
        return None
    source_text = source_contract["source"]
    return {
        "schema_version": VEHICLE_SOC_AGE_CONTRACT,
        "source": source_text,
        "max_age_s": int(vehicle_soc_max_age_s(source_text, config)),
    }


def _vehicle_soc_rule_sample(vehicle, config=None, now=None):
    """Akzeptiert nur explizite Fahrzeugquelle, Quellzeit und Freigabe."""
    if (
        not isinstance(vehicle, dict)
        or vehicle.get("soc_rule_confirmed") is not True
        or _soc_record_vetoed(vehicle)
    ):
        return None
    raw_soc = vehicle.get("soc", vehicle.get("battery_soc"))
    if isinstance(raw_soc, bool):
        return None
    source = str(vehicle.get("soc_source") or vehicle.get("source") or "").strip()
    if not source or not _is_confirmed_soc_source(source) or "soc_source_ts" not in vehicle:
        return None
    soc = _safe_float(raw_soc, -1.0)
    source_ts = _timestamp(vehicle.get("soc_source_ts"), 0.0)
    now = time.time() if now is None else _safe_float(now, 0.0)
    max_age_s = vehicle_soc_max_age_s(source, config)
    if (
        not math.isfinite(soc)
        or not math.isfinite(source_ts)
        or not math.isfinite(now)
        or soc < 0.0
        or soc > 100.0
        or source_ts <= 0.0
        or now <= 0.0
        or source_ts > now + 300.0
        or now - source_ts > max_age_s
    ):
        return None
    return {
        "soc": _clamp_percent(soc),
        "source": source,
        "source_ts": source_ts,
        "age_s": max(0.0, now - source_ts),
    }


def _timestamp(value, default=0.0):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        ts = float(value)
        if not math.isfinite(ts):
            return default
        return ts / 1000.0 if ts > 100000000000.0 else ts
    text = str(value).strip()
    if not text:
        return default
    try:
        ts = float(text)
        if not math.isfinite(ts):
            return default
        return ts / 1000.0 if ts > 100000000000.0 else ts
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        return parsed if math.isfinite(parsed) else default
    except Exception:
        return default


def _contract_flag_active(value):
    if value is True:
        return True
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        numeric = float(value)
        return math.isfinite(numeric) and numeric == 1.0
    return str(value).strip().lower() in {
        "1", "true", "yes", "ja", "on", "active", "stale",
        "expired", "invalid", "degraded",
    }


def _contract_field_true(value):
    """Normalisiert ausschließlich dokumentierte True-Repräsentationen."""

    if value is True:
        return True
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        numeric = float(value)
        return math.isfinite(numeric) and numeric == 1.0
    return str(value).strip().lower() in {
        "1", "true", "yes", "ja", "on",
    }


def _soc_record_vetoed(
    record,
    *,
    include_profile_binding=True,
    include_plug_state=True,
):
    item = record if isinstance(record, dict) else {}
    if any(
        _contract_flag_active(item.get(key))
        for key in (
            "soc_stale", "car_soc_stale", "stale",
            "estimate_expired", "soc_expired", "car_soc_expired", "expired",
            "soc_profile_binding_invalid", "car_soc_profile_binding_invalid",
            "profile_binding_invalid", "driver_status_stale",
            "driver_status_degraded",
        )
    ):
        return True
    required_true_fields = ["driver_status_valid"]
    if include_profile_binding:
        required_true_fields.extend(("soc_profile_bound", "car_soc_profile_bound"))
    if include_plug_state:
        required_true_fields.extend(("plug_state", "plugged", "is_plugged_in"))
    return any(
        key in item and not _contract_field_true(item.get(key))
        for key in required_true_fields
    )


def _tracker_anchor_state_valid(
    state,
    now=None,
    *,
    config=None,
    wb_id=None,
    vehicle_key=None,
    meter_estimation=False,
):
    """Prüft einen gespeicherten Trackeranker ohne Zeit-/Wertimputation."""

    if not isinstance(state, dict):
        return False
    raw_soc = state.get("anchor_soc")
    if isinstance(raw_soc, bool):
        return False
    soc = _safe_float(raw_soc, -1.0)
    source = str(state.get("anchor_source") or "").strip()
    source_text = source.lower()
    source_ts = _timestamp(state.get("anchor_sample_ts"), 0.0)
    now_value = time.time() if now is None else _safe_float(now, 0.0)
    max_age_s = (
        float(CONFIRMED_MANUAL_SOC_MAX_AGE_S)
        if source_text in CONFIRMED_MANUAL_SOC_SOURCES
        else vehicle_soc_max_age_s(
            f"{VEHICLE_ESTIMATED_SOURCE_PREFIX}{source}"
            if meter_estimation and source_text in VEHICLE_CLOUD_SOC_SOURCES
            else source,
            config,
        )
    )
    if wb_id is not None and state.get("wb") not in (None, ""):
        try:
            if int(state.get("wb")) != int(wb_id):
                return False
        except (TypeError, ValueError):
            return False
    expected_vehicle_key = _compact_id(vehicle_key)
    stored_vehicle_key = _compact_id(state.get("vehicle_key"))
    if expected_vehicle_key and stored_vehicle_key != expected_vehicle_key:
        return False
    return bool(
        math.isfinite(soc)
        and 0.0 <= soc <= 100.0
        and _is_confirmed_soc_source(source)
        and math.isfinite(source_ts)
        and source_ts > 0.0
        and math.isfinite(now_value)
        and now_value > 0.0
        and source_ts <= now_value + 300.0
        and now_value - source_ts <= max_age_s
        and (
            state.get("soc_rule_confirmed") is True
            or (
                source_text in CONFIRMED_MANUAL_SOC_SOURCES
                and "soc_rule_confirmed" not in state
            )
        )
        and not _soc_record_vetoed(
            state,
            include_profile_binding=False,
            include_plug_state=False,
        )
    )


def _openwb_pro_direct_soc_fresh(status, now=None):
    """Bindet einen direkten Pro-SoC an den Zeitstempel seines Rohankers."""

    item = status if isinstance(status, dict) else {}
    source = str(item.get("car_soc_source") or "").strip().lower()
    raw_soc = item.get("car_soc")
    if isinstance(raw_soc, bool):
        return False
    soc = _safe_float(raw_soc, -1.0)
    if (
        source not in OPENWB_PRO_SOURCES
        or not math.isfinite(soc)
        or soc < 0.0
        or soc > 100.0
        or not _is_confirmed_soc_source(source)
        or item.get("car_soc_rule_confirmed") is not True
        or item.get("plug_state") is not True
        or _soc_record_vetoed(item)
    ):
        return False
    now_value = time.time() if now is None else _safe_float(now, 0.0)
    source_ts = _timestamp(item.get("car_soc_source_ts"), 0.0)
    if source_ts <= 0.0:
        source_ts = _timestamp(item.get("car_soc_raw_ts"), 0.0)
    return bool(
        math.isfinite(now_value)
        and math.isfinite(source_ts)
        and now_value > 0.0
        and source_ts > 0.0
        and source_ts <= now_value
        and now_value - source_ts <= OPENWB_PRO_VEHICLE_SOC_MAX_AGE_S
    )


def _status_has_connected_vehicle(status):
    if not isinstance(status, dict):
        return False
    if any(
        _truthy(status.get(key))
        for key in ("plug_state", "locked", "charging", "charge_state")
    ):
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
    if (
        status.get("driver_status_valid") is False
        or _contract_flag_active(status.get("driver_status_stale"))
    ):
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
        if status.get("stable_vehicle_identity_current") is not True:
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


def _record_wallbox_binding_valid(
    record,
    wb_id,
    *,
    require_plugged=False,
    allow_legacy_missing_slot=False,
):
    """Binde einen Datensatz an Stecker und Wallbox ohne Truthiness."""

    if not isinstance(record, dict):
        return False
    if require_plugged:
        plugged = (
            record.get("is_plugged_in")
            if "is_plugged_in" in record
            else record.get("plugged")
        )
        if plugged is not True:
            return False
    slot_key = "wb_slot" if "wb_slot" in record else "wb" if "wb" in record else ""
    if not slot_key or record.get(slot_key) in (None, ""):
        return bool(allow_legacy_missing_slot)
    raw_slot = record.get(slot_key)
    if isinstance(raw_slot, bool):
        return False
    try:
        slot = int(raw_slot)
    except (TypeError, ValueError):
        return False
    if slot <= 0:
        return bool(allow_legacy_missing_slot and slot == 0)
    return slot == int(wb_id)


def _record_matches_selected_vehicle(record, selected_id):
    if not isinstance(record, dict):
        return False
    selected = _compact_id(selected_id)
    if not selected:
        return True
    aliases = {
        _compact_id(record.get(key))
        for key in (
            "id", "profile_id", "car_id", "vehicle_id",
            "cloud_vehicle_id", "rfid_tag",
        )
        if _compact_id(record.get(key))
    }
    return selected in aliases


def _configured_vehicle_binding_unique(config, wb_id, selected_id):
    """Erlaubt slotlose Shared-Daten nur für genau ein konfiguriertes WB-Profil."""

    selected = _compact_id(selected_id)
    if not selected:
        return False
    cfg = config if isinstance(config, dict) else {}
    current = _compact_id(cfg.get(f"wb{int(wb_id)}_car_id"))
    if current != selected:
        return False
    assignments = [
        _compact_id(cfg.get(f"wb{slot}_car_id"))
        for slot in (1, 2)
    ]
    return assignments.count(selected) == 1


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


def _manual_profile_binding(record, selected_id):
    """Typisiere einen Nutzeranker nur gegen genau ein gespeichertes Profil.

    Die Wallbox muss dafür keine stabile Fahrzeug-ID liefern. Die Bindung gilt
    ausschließlich für SoC, Anzeige und Planung; widersprüchliche zusätzliche
    Kennungen oder explizite Vetos bleiben fail-closed.
    """

    if (
        not isinstance(record, dict)
        or _soc_record_vetoed(
            record,
            include_profile_binding=False,
            include_plug_state=False,
        )
        or not _record_matches_selected_vehicle(record, selected_id)
    ):
        return None
    profile = _unique_saved_profile(selected_id)
    if not profile:
        return None
    profile_aliases = _compact_aliases(profile)
    record_aliases = _compact_aliases(
        record,
        (
            "id", "profile_id", "car_id", "vehicle_id",
            "cloud_vehicle_id", "rfid_tag", "vehicle_key",
        ),
    )
    canonical_profile_id = str(profile.get("id") or selected_id or "").strip()
    if (
        not canonical_profile_id
        or not profile_aliases
        or not record_aliases
        or not record_aliases.issubset(profile_aliases)
        or _compact_id(canonical_profile_id) not in profile_aliases
    ):
        return None
    return {
        "profile": profile,
        "profile_id": canonical_profile_id,
    }


def _repair_confirmed_manual_profile_binding(state, selected_id, wb_id):
    """Repariere ausschließlich den bekannten alten False-Producerzustand."""

    if (
        not isinstance(state, dict)
        or state.get("soc_profile_bound") is not False
        or state.get("soc_rule_confirmed") is not True
        or str(state.get("anchor_source") or "").strip().lower()
        not in CONFIRMED_MANUAL_SOC_SOURCES
    ):
        return False
    projection = _read_manual_soc(wb_id, None)
    projection_source = str((projection or {}).get("source") or "").strip().lower()
    projection_raw_source = str(
        (projection or {}).get("raw_source") or projection_source
    ).strip().lower()
    projection_anchor_ts = _timestamp(
        (projection or {}).get(
            "raw_soc_ts",
            (projection or {}).get("soc_source_ts"),
        ),
        0.0,
    )
    state_anchor_ts = _timestamp(state.get("anchor_sample_ts"), 0.0)
    projection_raw_soc = (projection or {}).get("raw_soc")
    projection_raw_soc_value = (
        _safe_float(projection_raw_soc, -1.0)
        if not isinstance(projection_raw_soc, bool)
        else -1.0
    )
    state_anchor_soc = _safe_float(state.get("anchor_soc"), -1.0)
    state_binding = _manual_profile_binding(state, selected_id)
    projection_binding = _manual_profile_binding(projection, selected_id)
    if (
        not isinstance(projection, dict)
        or projection.get("soc_profile_bound") is not False
        or projection.get("soc_rule_confirmed") is not True
        or not _record_wallbox_binding_valid(
            projection,
            wb_id,
            require_plugged=True,
            allow_legacy_missing_slot=False,
        )
        or projection_source not in {
            projection_raw_source,
            f"{ESTIMATED_PREFIX}_from_{projection_raw_source}",
        }
        or projection_raw_source
        != str(state.get("anchor_source") or "").strip().lower()
        or projection_raw_source not in CONFIRMED_MANUAL_SOC_SOURCES
        or projection_anchor_ts <= 0.0
        or abs(projection_anchor_ts - state_anchor_ts) > 1.0
        or not math.isfinite(projection_raw_soc_value)
        or not math.isfinite(state_anchor_soc)
        or projection_raw_soc_value < 0.0
        or state_anchor_soc < 0.0
        or abs(projection_raw_soc_value - state_anchor_soc) > 0.05
        or not state_binding
        or not projection_binding
        or state_binding["profile_id"] != projection_binding["profile_id"]
    ):
        return False
    state["profile_id"] = state_binding["profile_id"]
    state["soc_profile_bound"] = True
    return True


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
    if not math.isfinite(value):
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
        and not _contract_flag_active(data.get("soc_profile_binding_invalid"))
        and not _contract_flag_active(data.get("estimate_expired"))
        and str(data.get("plug_session_id") or "").strip() == plug_session_id
    )
    if not (is_raw_pro_sample or is_bound_pro_estimate):
        return None
    if _soc_record_vetoed(data) or data.get("plugged") is not True:
        return None
    if data.get("wb") not in (None, ""):
        try:
            if int(data.get("wb")) != int(wb_id):
                return None
        except (TypeError, ValueError):
            return None
    sample_source = source if is_raw_pro_sample else raw_source
    if (
        not _is_confirmed_soc_source(sample_source)
        or data.get("soc_rule_confirmed") is not True
    ):
        return None

    soc = _safe_float(
        data.get("soc") if is_raw_pro_sample else data.get("raw_soc"),
        -1.0,
    )
    sample_ts = _timestamp(data.get("soc_source_ts"), 0.0)
    if sample_ts <= 0.0:
        sample_ts = _timestamp(data.get("raw_soc_ts"), 0.0)
    if (
        not math.isfinite(soc)
        or soc < 0.0
        or soc > 100.0
        or sample_ts <= 0.0
        or sample_ts + 1.0 < plug_session_started_ts
        or sample_ts > float(now) + 300.0
        or float(now) - sample_ts > OPENWB_PRO_VEHICLE_SOC_MAX_AGE_S
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
    if (
        not math.isfinite(anchor_kwh)
        or not math.isfinite(anchor_meter_wh)
        or anchor_kwh < 0.0
        or meter_wh is None
        or not math.isfinite(float(meter_wh))
        or meter_wh + 0.1 < anchor_meter_wh
    ):
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
        "soc_rule_confirmed": True,
        "anchor_meter_wh": anchor_meter_wh,
    }


def _openwb_pro_tracker_session_sample(state, wb_id, profile, profile_aliases,
                                     plug_session_id, meter_wh, now, config):
    """Setze nur einen bereits bestätigten Cloud-Zähleranker derselben Session fort."""
    if not isinstance(state, dict):
        return None
    if (
        state.get("anchor_source") not in VEHICLE_CLOUD_SOC_SOURCES
        or state.get("soc_profile_bound") is not True
        or state.get("connected") is not True
        or state.get("meter_source") != "session_kwh"
        or state.get("plug_session_id") != plug_session_id
        or _compact_id(state.get("profile_id")) != _compact_id(profile.get("id"))
        or not _tracker_anchor_state_valid(
            state, now=now, config=config, wb_id=wb_id,
            vehicle_key=profile.get("id"), meter_estimation=True,
        )
    ):
        return None
    aliases = _compact_aliases(state, ("car_id", "vehicle_id", "profile_id"))
    anchor_wh = _safe_float(state.get("anchor_meter_wh"), -1.0)
    last_wh = _safe_float(state.get("last_meter_wh"), -1.0)
    if (
        not aliases or not aliases.issubset(profile_aliases)
        or not math.isfinite(anchor_wh) or anchor_wh < 0.0
        or not math.isfinite(last_wh) or last_wh < anchor_wh
        or meter_wh + 0.1 < last_wh
    ):
        return None
    return {
        "soc": state["anchor_soc"],
        "ts": state["anchor_sample_ts"],
        "source": state["anchor_source"],
        "car_id": profile.get("id") or "",
        "vehicle_id": state.get("vehicle_id") or "",
        "name": profile.get("name") or "",
        "capacity_kwh": profile.get("capacity_kwh"),
        "profile_id": profile.get("id") or "",
        "soc_profile_bound": True,
        "soc_rule_confirmed": True,
        "anchor_meter_wh": anchor_wh,
    }


def _openwb_pro_profile_binding(config, wb_id, status, selected_id, now=None,
                               tracker_state=None):
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
        or _contract_flag_active(status.get("driver_status_stale"))
        or _contract_flag_active(status.get("driver_status_degraded"))
        or status.get("plug_state") is not True
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
        plugged_value = (
            vehicle.get("is_plugged_in")
            if "is_plugged_in" in vehicle
            else vehicle.get("plugged")
        )
        if plugged_value is not True:
            continue
        raw_vehicle_slot = vehicle.get("wb_slot")
        if isinstance(raw_vehicle_slot, bool):
            continue
        try:
            vehicle_slot = int(raw_vehicle_slot or 0)
        except (TypeError, ValueError):
            vehicle_slot = 0
        if vehicle_slot <= 0 and not _configured_vehicle_binding_unique(
            config,
            wb_id,
            selected_id,
        ):
            continue
        if vehicle_slot > 0 and vehicle_slot != int(wb_id):
            continue

        # Zusätzliche typisierte Live-IDs müssen ebenfalls zum Profil gehören.
        strong_live_aliases = _compact_aliases(
            vehicle,
            ("cloud_vehicle_id", "vehicle_id", "vehicle_mac", "mac", "rfid", "rfid_tag"),
        )
        if strong_live_aliases and not strong_live_aliases.issubset(profile_aliases):
            continue
        truth_sample = _vehicle_soc_rule_sample(vehicle, config=config, now=now)
        if truth_sample is None:
            continue
        candidates.append((
            vehicle,
            truth_sample["soc"],
            truth_sample["source"],
            truth_sample["source_ts"],
        ))

    if not candidates:
        # Ein beim Empfang frischer Cloud-Anker bleibt über den gemessenen
        # Energiezuwachs nutzbar. Seine Quellzeit wird dabei nie verjüngt;
        # neue oder rückgesetzte Sessions dürfen ihn nicht übernehmen.
        retained_sample = _openwb_pro_tracker_session_sample(
            tracker_state, wb_id, profile, profile_aliases,
            plug_session_id, meter_wh, now, config,
        )
        if retained_sample:
            return {
                "sample": retained_sample, "profile": profile,
                "plug_session_id": plug_session_id,
                "meter_wh": meter_wh, "meter_source": "session_kwh",
            }
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
        "soc_rule_confirmed": True,
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
        partial_matches = []
        if fallback_norm:
            for car in saved_cars:
                if not isinstance(car, dict):
                    continue
                car_name = str(car.get("name") or "").strip()
                car_norm = car_name.lower()
                if (
                    car_norm
                    and (
                        car_norm == fallback_norm
                        or car_norm in fallback_norm
                        or fallback_norm in car_norm
                    )
                ):
                    partial_matches.append((car, car_name))
        if len(partial_matches) == 1:
            car, car_name = partial_matches[0]
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
    return profile


def _status_connected(status):
    if not isinstance(status, dict):
        return False
    if _truthy(status.get("plug_state")):
        return True
    if _truthy(status.get("car_connected_rscp")):
        return True
    try:
        return int(status.get("car", 1) or 1) >= 2
    except Exception:
        return False


def _status_power_w(status):
    if not isinstance(status, dict):
        return 0.0
    phase_power = _safe_float(status.get("phase_power_sum_w"), 0.0)
    if _truthy(status.get("phase_power_verified")) and phase_power > 50.0:
        return phase_power
    if not (
        _truthy(status.get("charging"))
        or _truthy(status.get("charge_state"))
    ):
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
        return anchor_ts > 0.0 and now - anchor_ts > CONFIRMED_MANUAL_SOC_MAX_AGE_S

    def _expire_session_anchor(self, wb_id, state, now, connected, charging):
        expired_soc = _clamp_percent(_safe_float((state or {}).get("anchor_soc"), 0.0))
        expired_payload = {
            "soc": expired_soc,
            "source": f"{ESTIMATED_PREFIX}_expired",
            "soc_rule_confirmed": False,
            "raw_soc": expired_soc,
            "raw_source": str((state or {}).get("anchor_source") or ""),
            "raw_soc_ts": int(_timestamp((state or {}).get("anchor_sample_ts"), 0.0)),
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

    def _manual_sample(self, wb_id, selected_id, config=None):
        data = _read_manual_soc(wb_id, None)
        if not isinstance(data, dict):
            return None
        source = str(data.get("source") or "").strip()
        source_text = source.lower()
        if (
            not source_text
            or source_text.startswith(ESTIMATED_PREFIX)
            or source_text in OPENWB_PRO_SOURCES
            or any(token in source_text for token in UNTRUSTED_SOC_SOURCE_TOKENS)
            or (
                "soc_rule_confirmed" in data
                and data.get("soc_rule_confirmed") is not True
            )
            or _soc_record_vetoed(data)
            or not _record_wallbox_binding_valid(
                data,
                wb_id,
                require_plugged=True,
                # Der per-Wallbox-Dateipfad ist bei alten manuellen
                # Nutzerankern bereits die eindeutige WB-Bindung.
                allow_legacy_missing_slot=True,
            )
        ):
            return None
        if not _is_confirmed_soc_source(source):
            return None
        now = time.time()
        if source_text in CONFIRMED_MANUAL_SOC_SOURCES:
            sample_ts = _timestamp(data.get("soc_source_ts"), 0.0)
            if sample_ts <= 0.0:
                sample_ts = _timestamp(data.get("raw_soc_ts"), 0.0)
            if sample_ts <= 0.0:
                sample_ts = _timestamp(data.get("ts"), 0.0)
            max_age_s = CONFIRMED_MANUAL_SOC_MAX_AGE_S
        else:
            if data.get("soc_rule_confirmed") is not True:
                return None
            sample_ts = _timestamp(data.get("soc_source_ts"), 0.0)
            if sample_ts <= 0.0:
                sample_ts = _timestamp(data.get("raw_soc_ts"), 0.0)
            max_age_s = vehicle_soc_max_age_s(source, config)
        if (
            not math.isfinite(sample_ts)
            or sample_ts <= 0.0
            or sample_ts > now + 300.0
            or now - sample_ts > max_age_s
        ):
            return None
        raw_soc = data.get("soc")
        if isinstance(raw_soc, bool):
            return None
        soc = _safe_float(raw_soc, -1.0)
        if not math.isfinite(soc) or soc < 0.0 or soc > 100.0:
            return None
        car_id = str(data.get("car_id") or data.get("profile_id") or "").strip()
        vehicle_id = str(data.get("vehicle_id") or "").strip()
        if (
            selected_id
            and selected_id.lower() not in NO_VEHICLE_IDS
            and not _record_matches_selected_vehicle(data, selected_id)
        ):
            return None
        profile_binding = _manual_profile_binding(data, selected_id)
        canonical_profile_id = (
            profile_binding["profile_id"]
            if profile_binding
            else ""
        )
        bound_profile = profile_binding["profile"] if profile_binding else {}
        return {
            "soc": _clamp_percent(soc),
            "ts": sample_ts,
            "source": source,
            "soc_rule_confirmed": True,
            "car_id": canonical_profile_id or car_id or selected_id,
            "vehicle_id": vehicle_id,
            "name": str(
                data.get("name") or bound_profile.get("name") or ""
            ).strip(),
            "capacity_kwh": _safe_float(
                data.get("capacity"),
                _safe_float(
                    bound_profile.get("capacity", bound_profile.get("capacity_kwh")),
                    0.0,
                ),
            ),
            "profile_id": canonical_profile_id,
            "soc_profile_bound": profile_binding is not None,
        }

    def _vehicle_sample(self, wb_id, selected_id, config=None):
        if not selected_id or selected_id.lower() in NO_VEHICLE_IDS:
            return None
        now = time.time()
        for vehicle in _load_live_vehicles():
            legacy_slot_binding = bool(
                _record_matches_selected_vehicle(vehicle, selected_id)
                and _configured_vehicle_binding_unique(
                    config,
                    wb_id,
                    selected_id,
                )
            )
            if (
                not isinstance(vehicle, dict)
                or not _matches_vehicle(vehicle, selected_id)
                or not _record_wallbox_binding_valid(
                    vehicle,
                    wb_id,
                    require_plugged=True,
                    allow_legacy_missing_slot=legacy_slot_binding,
                )
            ):
                continue
            sample = _vehicle_soc_rule_sample(vehicle, config=config, now=now)
            if sample is None:
                return None
            return {
                "soc": sample["soc"],
                "ts": sample["source_ts"],
                "source": sample["source"],
                "soc_rule_confirmed": True,
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
            self._manual_sample(wb_id, selected_id, config=config),
            self._vehicle_sample(wb_id, selected_id, config=config),
            self._config_sample(config, wb_id, selected_id),
        ):
            if sample:
                return sample
        raw_status_soc = (status or {}).get("car_soc")
        status_soc = _safe_float(raw_status_soc, -1.0)
        if (
            not isinstance(raw_status_soc, bool)
            and math.isfinite(status_soc)
            and 0.0 <= status_soc <= 100.0
        ):
            source = str((status or {}).get("car_soc_source") or "").strip()
            source_ts = (
                (status or {}).get("car_soc_source_ts")
                if "car_soc_source_ts" in (status or {})
                else (status or {}).get("car_soc_raw_ts")
            )
            status_sample_record = {
                "soc": status_soc,
                "soc_source": source,
                "soc_source_ts": source_ts,
                "soc_rule_confirmed": (status or {}).get("car_soc_rule_confirmed") is True,
            }
            for veto_key in (
                "soc_stale", "car_soc_stale", "stale",
                "estimate_expired", "soc_expired", "car_soc_expired", "expired",
                "soc_profile_binding_invalid", "car_soc_profile_binding_invalid",
                "profile_binding_invalid", "driver_status_stale",
                "driver_status_degraded", "driver_status_valid",
                "soc_profile_bound", "car_soc_profile_bound",
                "plug_state", "plugged", "is_plugged_in",
            ):
                if veto_key in (status or {}):
                    status_sample_record[veto_key] = (status or {}).get(veto_key)
            status_sample = _vehicle_soc_rule_sample(
                status_sample_record,
                config=config,
            )
            if (source not in OPENWB_PRO_SOURCES
                and not source.startswith(ESTIMATED_PREFIX)
                and _status_connected(status)
                and status_sample is not None):
                return {
                    "soc": status_sample["soc"],
                    "ts": status_sample["source_ts"],
                    "source": status_sample["source"],
                    "soc_rule_confirmed": True,
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
        # Ein frischer bestätigter Roh-/Treiberschätzwert bleibt autoritativ.
        # Der Dateizeitpunkt der zyklisch geschriebenen Manual-Datei darf den
        # tatsächlichen Rohanker nicht verjüngen. Nach acht Stunden wird die
        # direkte Quelle lokal entwertet und ein eindeutig profilgebundener,
        # neuerer Fahrzeugwert darf übernehmen.
        if (
            source_status in OPENWB_PRO_SOURCES
            and status_soc >= 0.0
            and _is_confirmed_soc_source(source_status)
        ):
            if _openwb_pro_direct_soc_fresh(status, now=now):
                return None
            status["car_soc_rule_confirmed"] = False

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
                tracker_state=self._load_state(wb_id),
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
        state_anchor_valid = _tracker_anchor_state_valid(
            state,
            now=now,
            config=config,
            wb_id=wb_id,
            vehicle_key=vehicle_key,
            meter_estimation=is_openwb_pro and profile_binding is not None,
        )
        if state_anchor_valid and connected and not is_openwb_pro:
            _repair_confirmed_manual_profile_binding(state, selected_id, wb_id)
        needs_anchor = (
            not state_anchor_valid
            or (is_openwb_pro and (state.get("soc_profile_bound") is not True or not state.get("plug_session_id")))
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
                "anchor_sample_ts": _timestamp(sample.get("ts"), 0.0),
                "anchor_source": str(sample.get("source") or ""),
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
                "soc_profile_bound": sample.get("soc_profile_bound") is True,
                "soc_rule_confirmed": sample.get("soc_rule_confirmed") is True,
                "plug_session_id": plug_session_id,
            }
        elif not state_anchor_valid:
            return None

        if not _tracker_anchor_state_valid(
            state,
            now=now,
            config=config,
            wb_id=wb_id,
            vehicle_key=vehicle_key,
            meter_estimation=is_openwb_pro and profile_binding is not None,
        ):
            return None

        # Ein kompatibler Legacy-Manuellanker ohne Feld wurde oben nur nach
        # vollständiger Quellen-, Zeit-, Werte- und Veto-Prüfung akzeptiert.
        # Ab hier wird der interne Zustand auf den typisierten Vertrag gehoben;
        # Maschinenquellen gelangen ohne ``is True`` nie an diese Stelle.
        state["soc_rule_confirmed"] = True

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

        state["wb"] = wb_id
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
        raw_source = str(state.get("anchor_source") or "")
        source = f"{ESTIMATED_PREFIX}_from_{raw_source}" if delivered_wh > 20.0 else raw_source
        anchor_sample_ts = _timestamp(state.get("anchor_sample_ts"), 0.0)
        age_contract = vehicle_soc_age_contract(source, config)
        if age_contract is None:
            return None
        result = {
            "soc": round(estimated_soc, 1),
            "source": source,
            "soc_rule_confirmed": (
                state.get("soc_rule_confirmed") is True
                and _is_confirmed_soc_source(raw_source)
            ),
            "raw_soc": round(_safe_float(state.get("anchor_soc"), estimated_soc), 1),
            "raw_source": raw_source,
            "raw_soc_ts": int(anchor_sample_ts),
            "soc_source_ts": int(anchor_sample_ts),
            "soc_age_contract": age_contract["schema_version"],
            "soc_age_contract_source": age_contract["source"],
            "soc_max_age_s": age_contract["max_age_s"],
            # Bei einer Profilbindung ohne Pro-Live-ID bleibt die Runtime-ID
            # leer. Das Profil wird separat transportiert und ist keine
            # Behauptung einer stabilen, von der Wallbox gelesenen Identität.
            "car_id": (
                str(status.get("car_id") or "").strip()
                if state.get("soc_profile_bound") is True
                else state.get("car_id") or active_car_id
            ),
            "vehicle_id": (
                str(status.get("vehicle_id") or "").strip()
                if state.get("soc_profile_bound") is True
                else state.get("vehicle_id") or ""
            ),
            "name": state.get("name") or profile.get("name") or active_car_id,
            "capacity": capacity,
            "wb": wb_id,
            "plugged": connected,
            "charging": charging,
            "is_interpolated": delivered_wh > 20.0,
            "profile_id": state.get("profile_id") or "",
            "soc_profile_bound": state.get("soc_profile_bound") is True,
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
            and data.get("soc_profile_bound") is not True
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
