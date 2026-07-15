#!/usr/bin/env python3
"""
E3DC-Control Multi Wallbox Manager - Entry Point (v3.9.4+)

Alle Logik liegt in den Submodulen unter Installer/Wallbox/:
  config.py      - Pfade, Logging, get_config(), read_live_data(), ...
  drivers.py     - WallboxDriver, GoECharger, OpenWBCharger, E3DCCharger
  scheduler.py   - Service-Fassade zum kanonischen wallbox_planer.py
  vehicle_manager.py - Fahrzeugidentitaet, Profile und SoC-Status
  phase_sequencer.py - bestaetigte openWB-Pro-Phasensequenz
  ramps.py       - reine Stromrampen-Vertraege
  controller.py  - allocate_power()

Dieses Skript startet via run() den Haupt-Regelkreis.
"""
import os
import sys
import time
import json
import logging
import math

# Installer-Verzeichnis in Pfad aufnehmen (fuer rscp_client u.a.)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Modul-Importe
# ---------------------------------------------------------------------------
from Wallbox.config import (
    get_config,
    read_live_data,
    live_data_age_s,
    write_status,
    compute_charge_score,
    read_current_epex_price,
    CONFIG_FILE,
    V4_CONFIG_FILE,
    RAMDISK_DIR,
    INSTALL_DIR,
    STATUS_OUTPUT_FILE,
    LIVE_DATA_FILE_PY,
    LOG_DIR,
    logger,
)
from Wallbox.drivers import (
    GoECharger,
    OpenWBCharger,
    E3DCCharger,
    DISABLED_WALLBOX_TYPES,
    create_charger,
    discover_openwb_chargepoints,
)
from Wallbox.scheduler import ScheduleService
from Wallbox.vehicle_manager import VehicleManager
from Wallbox.controller import allocate_power
from Wallbox import command_gate
from Wallbox import decision as wallbox_decision
from Wallbox import e3dc_session
from Wallbox import goe_session
from Wallbox import openwb_session
from Wallbox import openwb_pro_session
from Wallbox.phase_sequencer import PhaseSwitchSequencer
from Wallbox.phase_sequencer import begin_phase_transition_reservation, phase_transition_reservation
from Wallbox import phase_transition as wallbox_phase_transition_guard
from Wallbox import policy as wallbox_policy
import control_command_guard
from decision_history import write_history_record
from json_cache import read_json_cached
from config_secret_permissions import apply_config_secret_permissions
from Wallbox.modes import (
    MODE_BATTERY_DEPARTURE,
    MODE_BASE,
    MODE_CURVE,
    MODE_OFF,
    MODE_PRICE,
    MODE_TARGET,
    controller_mode,
    mode_label,
    normalize_distribution_mode,
    normalize_wb_mode,
    price_allows_grid,
    price_limit_ct,
    storage_floor_mode,
)
from Wallbox.runtime_state import WallboxRuntimeState
from market_economics import current_market_consumer_release
from data_models import live_power_plausibility
from ems_decision_diagnostics import (
    build_wallbox_decision_records,
    default_surface_path,
    write_decision_surface_records,
)

PRICE_BOOST_PLAN_FILE = os.path.join(RAMDISK_DIR, "price_boost_plan.json")
PREDUMP_PLAN_FILE = os.path.join(RAMDISK_DIR, "predump_consumer_plan.json")
WB_STORAGE_INTENT_FILE = os.path.join(RAMDISK_DIR, "wallbox_storage_intent.json")
WB_ABORT_STATE_FILE = os.path.join(RAMDISK_DIR, "wallbox_abort_state.json")
WB_PHASE_STATE_FILE = os.path.join(RAMDISK_DIR, "wallbox_phase_state.json")
WB_DEFAULT_RELEASE_REQUEST_FILE = os.path.join(RAMDISK_DIR, "wallbox_default_release_request.json")
WB_USER_MODE_REQUEST_FILE = os.path.join(RAMDISK_DIR, "wallbox_user_mode_request.json")
EMS_DECISION_FILE = default_surface_path(RAMDISK_DIR)
ENERGY_DECISION_LATEST_FILE = os.path.join(RAMDISK_DIR, "energy_decision_latest.json")

CONFIRMED_CAR_SOC_MANUAL_SOURCES = {"manual_start_soc", "manual_soc", "manual", "openwb_profile_link"}
CONFIRMED_CAR_SOC_DIRECT_SOURCES = {"openwb_pro_raw", "openwb_pro_estimated"}
UNCONFIRMED_CAR_SOC_SOURCES = {"simple_view_start_soc", "config_start_soc"}
CONFIRMED_CAR_SOC_KEYWORDS = ("mqtt", "bluelink", "wallbox", "openwb", "vehicle", "car_soc", "hyundai", "kia")
WALLBOX_STATUS_STALE_DEFAULT_S = 45.0
WALLBOX_STATUS_DIAG_KEYS = (
    "driver_status_valid",
    "driver_status_stale",
    "driver_status_degraded",
    "driver_status_age_s",
    "driver_status_reason",
    "driver_status_last_ok_ts",
    "driver_status_last_sample_ts",
    "driver_status_source",
    "driver_status_plausible",
    "driver_status_glitch",
    "driver_status_glitch_reason",
    "driver_status_last_good_ts",
)


def _car_soc_source_rule_confirmed(source):
    text = str(source or "").strip().lower()
    if not text or text in UNCONFIRMED_CAR_SOC_SOURCES:
        return False
    if text.startswith("wallbox_estimated_from_"):
        return _car_soc_source_rule_confirmed(text[len("wallbox_estimated_from_"):])
    if text.startswith("wallbox_estimated"):
        return False
    if text in CONFIRMED_CAR_SOC_MANUAL_SOURCES or text in CONFIRMED_CAR_SOC_DIRECT_SOURCES:
        return True
    return any(token in text for token in CONFIRMED_CAR_SOC_KEYWORDS)


def _car_soc_rule_confirmed(status):
    if not isinstance(status, dict):
        return False
    if bool(status.get("car_soc_rule_confirmed", False)):
        return True
    return _car_soc_source_rule_confirmed(status.get("car_soc_source"))


def _openwb_primary_chargemode_key(value):
    text = str(value or "").strip().lower()
    if text in ("instant", "instant_charging"):
        return "instant"
    if text in ("pv", "pv_charging"):
        return "pv"
    if text in ("stop", "stopped"):
        return "stop"
    return text


def _openwb_primary_grid_phase_warning(
    status,
    c_data=None,
    *,
    public_mode=0,
    cap_amp=0,
    scheduled_slot_active=False,
    mode5_grid_allowed=False,
    price_boost_active=False,
    vehicle_max_phases=0,
):
    """Diagnostic only: openWB Primary grid charging may be stuck at 1p."""
    st = status if isinstance(status, dict) else {}
    box = c_data if isinstance(c_data, dict) else {}
    charger = box.get("charger")
    charger_class = charger.__class__.__name__ if charger is not None else str(box.get("_charger_class_name", "") or "")
    primary_openwb = bool(
        charger_class == "OpenWBCharger"
        and (
            getattr(charger, "primary_mode_enabled", False)
            or str(st.get("effective_role") or st.get("configured_role") or "").lower().startswith("primary")
            or str(st.get("api_surface") or "").lower().startswith("openwb_primary")
        )
    )
    if not primary_openwb:
        return None

    mode = normalize_wb_mode(public_mode)
    grid_intent = bool(
        mode == MODE_PRICE
        or scheduled_slot_active
        or mode5_grid_allowed
        or price_boost_active
    )
    if not grid_intent or not _wb_status_real_charging(st):
        return None

    target_amp = max(
        _cfg_float(box.get("current_set_amp"), 0.0),
        _cfg_float(st.get("last_command_amp"), 0.0),
        _cfg_float(st.get("instant_charging_current"), 0.0),
        _cfg_float(st.get("amp"), 0.0),
        _cfg_float(cap_amp, 0.0),
    )
    if target_amp < 10.0:
        return None

    real_power_w = max(0.0, abs(_wb_status_real_power(st)))
    if real_power_w < 500.0:
        return None

    actual_phases = _valid_phase_count(st.get("phases_in_use"), 0)
    if not actual_phases:
        actual_phases = _valid_phase_count(st.get("phase_actual_phases"), 0)
    if not actual_phases:
        phase_powers = [
            abs(_cfg_float(st.get("phase_power_l1_w"), 0.0)),
            abs(_cfg_float(st.get("phase_power_l2_w"), 0.0)),
            abs(_cfg_float(st.get("phase_power_l3_w"), 0.0)),
        ]
        active_phase_count = sum(1 for value in phase_powers if value > 100.0)
        actual_phases = active_phase_count if active_phase_count in (1, 2, 3) else 0

    expected_3p_w = target_amp * 230.0 * 3.0
    low_vs_3p = bool(expected_3p_w > 0.0 and real_power_w < expected_3p_w * 0.55)
    one_phase_hint = bool(actual_phases == 1 or (actual_phases <= 0 and low_vs_3p))
    if not one_phase_hint:
        return None

    vehicle_phases = _valid_phase_count(vehicle_max_phases, 0) or _valid_phase_count(st.get("phase_vehicle_max_phases"), 0)
    connected_phases = _valid_phase_count(st.get("connected_phases"), 0) or _valid_phase_count(st.get("phase_cable_phases"), 0)
    capable_hint = "Fahrzeug/Anschluss wirkt 3p-fähig." if max(vehicle_phases, connected_phases) >= 3 else "Wenn Fahrzeug und Installation 3p können,"
    phase_text = "%dp" % actual_phases if actual_phases > 0 else "Phasen unklar"
    reason = (
        "openWB Primary Netzladen: %.1f A angefordert, real %.0f W (%s). %s "
        "E3DC-Control gibt Sofortladen/Strom vor, erzwingt aber keine Phasenumschaltung; "
        "openWB-Sofortladen, Phasenautomatik und Fahrzeugprofil prüfen."
    ) % (target_amp, real_power_w, phase_text, capable_hint)
    return {
        "state": "Primary 1p",
        "state_level": "warning",
        "state_reason": reason,
        "control_status": "openwb_primary_grid_phase_warning",
        "control_label": "Primary lädt 1-phasig",
        "control_detail": reason,
        "control_level": "warning",
        "openwb_primary_grid_phase_warning": True,
        "openwb_primary_grid_phase_warning_reason": reason,
        "openwb_primary_grid_target_amp": round(target_amp, 1),
        "openwb_primary_grid_actual_power_w": round(real_power_w, 1),
        "openwb_primary_grid_actual_phases": int(actual_phases or 0),
        "openwb_primary_grid_expected_3p_w": round(expected_3p_w, 1),
        "openwb_primary_grid_vehicle_phases": int(vehicle_phases or 0),
        "openwb_primary_grid_connected_phases": int(connected_phases or 0),
    }


def _wallbox_vehicle_under_acceptance_warning(
    status,
    c_data=None,
    *,
    cap_amp=0,
    detected_phases=0,
    min_amp=6,
):
    """Diagnostic only: EVSE offers a clear current, vehicle takes much less."""
    st = status if isinstance(status, dict) else {}
    box = c_data if isinstance(c_data, dict) else {}
    if not _wb_status_real_charging(st):
        return None

    target_amp = max(
        _cfg_float(box.get("current_set_amp"), 0.0),
        _cfg_float(st.get("last_command_amp"), 0.0),
        _cfg_float(st.get("offered_current_raw"), 0.0),
        _cfg_float(st.get("offered_current"), 0.0),
        _cfg_float(st.get("evse_current"), 0.0),
        _cfg_float(st.get("amp"), 0.0),
        _cfg_float(cap_amp, 0.0),
    )
    min_offer_amp = max(10.0, _cfg_float(min_amp, 6.0) + 3.0)
    if target_amp < min_offer_amp:
        return None

    target_phases = _valid_phase_count(st.get("phases_target"), 0)
    if not target_phases:
        target_phases = _valid_phase_count(st.get("phase_effective_phases"), 0)
    if not target_phases:
        target_phases = _valid_phase_count(st.get("wallbox_phases"), 0)
    if not target_phases:
        target_phases = _valid_phase_count(detected_phases, 0)
    if not target_phases:
        target_phases = _valid_phase_count(st.get("number_phases"), 0)
    target_phases = max(1, target_phases or 1)

    real_power_w = max(0.0, abs(_wb_status_real_power(st)))
    expected_power_w = target_amp * 230.0 * target_phases
    if real_power_w < 500.0 or expected_power_w < 4500.0:
        return None

    shortfall_w = expected_power_w - real_power_w
    accepted_ratio = real_power_w / expected_power_w if expected_power_w > 0.0 else 1.0
    if not (accepted_ratio < 0.65 and shortfall_w >= max(1500.0, expected_power_w * 0.25)):
        return None

    actual_phases = _valid_phase_count(st.get("phases_in_use"), 0)
    if not actual_phases:
        actual_phases = _valid_phase_count(st.get("phase_actual_phases"), 0)
    if not actual_phases:
        actual_phases = _valid_phase_count(st.get("phases_actual"), 0)
    if not actual_phases:
        phase_powers = [
            abs(_cfg_float(st.get("phase_power_l1_w"), 0.0)),
            abs(_cfg_float(st.get("phase_power_l2_w"), 0.0)),
            abs(_cfg_float(st.get("phase_power_l3_w"), 0.0)),
        ]
        active_phase_count = sum(1 for value in phase_powers if value > 100.0)
        actual_phases = active_phase_count if active_phase_count in (1, 2, 3) else 0

    charger = box.get("charger")
    charger_class = charger.__class__.__name__ if charger is not None else str(box.get("_charger_class_name", "") or "")
    phase_text = "%dp" % actual_phases if actual_phases > 0 else "Phasen unklar"
    reason = (
        "Wallbox bietet %.1f A/%dp an, Fahrzeug nimmt aber nur %.0f W "
        "(%.0f%% von ca. %.0f W, %s). E3DC-Control kann nur freigeben; "
        "Fahrzeuglimit, Fahrzeug-SoC/Temperatur, Kabel/Phasen, Wallbox-Profil und App-Limit prüfen."
    ) % (
        target_amp,
        target_phases,
        real_power_w,
        accepted_ratio * 100.0,
        expected_power_w,
        phase_text,
    )
    return {
        "state": "Fahrzeug begrenzt",
        "state_level": "warning",
        "state_reason": reason,
        "control_status": "vehicle_under_acceptance",
        "control_label": "Fahrzeug nimmt weniger",
        "control_detail": reason,
        "control_level": "warning",
        "vehicle_under_acceptance_warning": True,
        "vehicle_under_acceptance_reason": reason,
        "vehicle_under_acceptance_offered_amp": round(target_amp, 1),
        "vehicle_under_acceptance_target_phases": int(target_phases),
        "vehicle_under_acceptance_actual_power_w": round(real_power_w, 1),
        "vehicle_under_acceptance_expected_power_w": round(expected_power_w, 1),
        "vehicle_under_acceptance_accepted_ratio": round(accepted_ratio, 3),
        "vehicle_under_acceptance_actual_phases": int(actual_phases or 0),
        "vehicle_under_acceptance_charger_class": charger_class,
    }


WB_DECISION_LATEST_FILE = os.path.join(RAMDISK_DIR, "wallbox_decision_latest.json")
WB_DECISION_HISTORY_PREFIX = "wallbox_decision_history_"
WB_ABORT_RETRY_LIMIT = 1
_wallbox_decision_history_state = {}


def wallbox_loop_sleep_s(
    config,
    *,
    made_changes=False,
    statuses=None,
    wb_details=None,
    scheduled_slot_active=False,
    price_boost_active=False,
    predump_wallbox_active=False,
    budget_stale=False,
    budget_timeout=False,
    storage_state="",
):
    """Return the next manager-loop sleep time without changing control rules."""

    statuses = statuses or []
    wb_details = wb_details or []
    active_storage_states = {
        "parallel_wb_auto",
        "parallel_curve_charge",
        "parallel_curve_charge_cap",
        "parallel_grid_relief_auto",
        "pre_discharge",
        "pre_discharge_wait",
        "pre_discharge_consumer_auto",
        "price_boost_grid",
        "storm_guard_grid",
    }
    any_connected = any(_wb_status_connected(item.get("status")) for item in statuses if isinstance(item, dict))
    any_charging = any(_wb_status_real_charging(item.get("status")) for item in statuses if isinstance(item, dict))
    any_detail_edge = any(
        bool(detail.get("plug") or detail.get("charging"))
        for detail in wb_details
        if isinstance(detail, dict)
    )
    active_edge = bool(
        made_changes
        or any_connected
        or any_charging
        or any_detail_edge
        or scheduled_slot_active
        or price_boost_active
        or predump_wallbox_active
        or budget_stale
        or budget_timeout
        or str(storage_state or "") in active_storage_states
    )
    if active_edge:
        return 2.0
    idle_s = _cfg_float((config or {}).get("wb_idle_poll_s"), 6.0)
    return max(2.0, min(10.0, idle_s))


def _cfg_float(value, default=0.0):
    try:
        s = str(value).strip() if value is not None else ""
        return float(s) if s else float(default)
    except (TypeError, ValueError):
        return float(default)

def _first_cfg_float(*values, default=0.0):
    for value in values:
        try:
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            result = float(text)
            if math.isfinite(result):
                return result
        except (TypeError, ValueError):
            continue
    return float(default)

def _first_present(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None

def _ts_seconds(value, default=0.0):
    try:
        if value is None:
            return float(default)
        ts = float(value)
        if not math.isfinite(ts) or ts <= 0:
            return float(default)
        if ts > 10_000_000_000:
            ts /= 1000.0
        return ts
    except (TypeError, ValueError):
        return float(default)

def _amp_limit(value, fallback=16):
    amp = int(round(_cfg_float(value, fallback)))
    return max(6, min(32, amp))

BATTERY_DEPARTURE_DEFAULT_TIME = "06:30"
BATTERY_DEPARTURE_DEFAULT_WINDOW_H = 3.0


def _parse_hhmm(value, default=BATTERY_DEPARTURE_DEFAULT_TIME):
    text = str(value if value not in (None, "") else default).strip()
    if len(text) >= 5 and ":" in text:
        parts = text.split(":", 1)
        try:
            hour = int(parts[0])
            minute = int(parts[1][:2])
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute, f"{hour:02d}:{minute:02d}"
        except (TypeError, ValueError):
            pass
    return 6, 30, BATTERY_DEPARTURE_DEFAULT_TIME


def _battery_departure_window_s(config, charger_id=None):
    keys = []
    if charger_id is not None:
        keys.append(f"wb{int(charger_id)}_battery_departure_window_h")
    keys.append("wb_battery_departure_window_h")
    raw = None
    for key in keys:
        if key in config and str(config.get(key, "")).strip() != "":
            raw = config.get(key)
            break
    hours = _cfg_float(raw, BATTERY_DEPARTURE_DEFAULT_WINDOW_H)
    return max(1.0, min(36.0, hours)) * 3600.0


def _battery_departure_state(config, charger_id, public_mode, now_ts=None):
    """Return the active/blocked state for the battery-only departure mode."""
    if normalize_wb_mode(public_mode) != MODE_BATTERY_DEPARTURE:
        return {"configured": False, "active": False, "blocked": False}
    now_ts = time.time() if now_ts is None else float(now_ts)
    key = f"wb{int(charger_id)}_battery_departure_time"
    hour, minute, label = _parse_hhmm(
        config.get(key, config.get("wb_battery_departure_time", BATTERY_DEPARTURE_DEFAULT_TIME))
    )
    now_local = time.localtime(now_ts)
    deadline_tuple = (
        now_local.tm_year,
        now_local.tm_mon,
        now_local.tm_mday,
        hour,
        minute,
        0,
        now_local.tm_wday,
        now_local.tm_yday,
        now_local.tm_isdst,
    )
    deadline_ts = time.mktime(deadline_tuple)
    if now_ts > deadline_ts and now_local.tm_hour >= 12:
        deadline_ts += 86400.0
    remaining_s = deadline_ts - now_ts
    window_s = _battery_departure_window_s(config, charger_id)
    start_ts = deadline_ts - window_s
    active = 0.0 <= remaining_s <= window_s
    expired = remaining_s < 0.0
    blocked = not active
    reason = "active" if active else ("departure_reached" if expired else "outside_window")
    return {
        "configured": True,
        "active": active,
        "blocked": blocked,
        "expired": expired,
        "reason": reason,
        "deadline_ts": deadline_ts,
        "start_ts": start_ts,
        "departure_time": label,
        "start_time": time.strftime("%H:%M", time.localtime(start_ts)),
        "remaining_s": remaining_s,
        "window_s": window_s,
        "window_h": round(window_s / 3600.0, 2),
    }


def _battery_departure_target_reached(config, charger_id, status):
    """Return whether Akku-bis-Abfahrt should be considered finished by SoC."""
    if not _wb_status_connected(status):
        return False, -1.0, 100.0
    if not _car_soc_rule_confirmed(status):
        return False, -1.0, 100.0
    try:
        car_soc = float((status or {}).get("car_soc"))
    except (TypeError, ValueError):
        return False, -1.0, 100.0
    if car_soc < 0.0:
        return False, car_soc, 100.0

    target_soc = 100.0
    cfg = config or {}
    key = f"wb{int(charger_id or 1)}_target_soc"
    raw_target = cfg.get(key)
    if raw_target is not None and str(raw_target).strip() != "":
        target_soc = _cfg_float(raw_target, 100.0)
    target_soc = max(5.0, min(100.0, target_soc))
    threshold = 99.5 if target_soc >= 99.5 else max(0.0, target_soc - 0.5)
    return car_soc >= threshold, car_soc, target_soc


def _wallbox_target_soc_not_reached(config, charger_id, status):
    """Return true when a confirmed vehicle SoC proves that a charge-end latch is stale."""
    reached, car_soc, target_soc = _battery_departure_target_reached(config, charger_id, status)
    if car_soc < 0.0:
        return False, car_soc, target_soc
    return bool(not reached), car_soc, target_soc


def _wallbox_target_kwh_reached(config, charger_id, status):
    """Return whether the current wallbox session reached its kWh target."""
    cfg = config or {}
    cid = int(charger_id or 1)
    target_unit = str(cfg.get(f"wb{cid}_target_unit", cfg.get("car_target_unit", "soc"))).strip().lower()
    if target_unit != "kwh" or not _wb_status_connected(status):
        return False, -1.0, 0.0
    target_kwh = _cfg_float(cfg.get(f"wb{cid}_target_kwh", cfg.get("car_target_kwh", 0.0)), 0.0)
    if target_kwh <= 0.05:
        return False, -1.0, target_kwh
    session_kwh = _first_cfg_float(
        (status or {}).get("session_kwh"),
        (status or {}).get("kwh"),
        (status or {}).get("session_energy_kwh"),
        default=-1.0,
    )
    if session_kwh < 0.0:
        return False, session_kwh, target_kwh
    return session_kwh + 0.02 >= target_kwh, session_kwh, target_kwh


def _mode_priority(public_mode):
    mode = normalize_wb_mode(public_mode)
    if mode == MODE_OFF:
        return 0
    if mode == MODE_CURVE:
        return 20
    if mode == MODE_BASE:
        return 30
    if mode in (MODE_TARGET, MODE_BATTERY_DEPARTURE):
        return 40
    if mode == MODE_PRICE:
        return 50
    return 20


def _select_effective_public_wb_mode(wb_charge_mode, charger_ids, blocked_ids=None):
    blocked_ids = set(blocked_ids or [])
    best_mode = MODE_OFF
    best_rank = -1
    for cid in charger_ids or []:
        mode = MODE_OFF if int(cid) in blocked_ids else normalize_wb_mode(wb_charge_mode.get(int(cid), MODE_OFF))
        rank = _mode_priority(mode)
        if rank > best_rank:
            best_rank = rank
            best_mode = mode
    return best_mode

def _write_json_atomic(path, payload, mode=0o664):
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        try:
            os.chmod(tmp, mode)
        except Exception:
            pass
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except Exception:
            pass
        return True
    except Exception as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        logger.debug("JSON-Schreibvorgang uebersprungen (%s): %s", path, exc)
        return False

def _cleanup_wallbox_decision_history(retention_days=14):
    global _wallbox_decision_history_state
    today = time.strftime("%Y-%m-%d")
    if _wallbox_decision_history_state.get("legacy_cleanup_day") == today:
        return
    _wallbox_decision_history_state["legacy_cleanup_day"] = today
    cutoff = time.time() - max(1, int(retention_days or 14)) * 86400
    try:
        for name in os.listdir(LOG_DIR):
            if not name.startswith(WB_DECISION_HISTORY_PREFIX):
                continue
            if not (name.endswith(".jsonl") or name.endswith(".jsonl.gz")):
                continue
            path = os.path.join(LOG_DIR, name)
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except Exception as exc:
        logger.debug("Wallbox-Decision Cleanup uebersprungen: %s", exc)

def write_wallbox_decision_history(record, config):
    enabled = str((config or {}).get("wallbox_decision_history_enable", 1)).strip().lower()
    if enabled in ("0", "false", "no", "off", "nein", "aus"):
        return
    try:
        write_history_record(
            record,
            config=config or {},
            log_dir=LOG_DIR,
            latest_path=WB_DECISION_LATEST_FILE,
            prefix=WB_DECISION_HISTORY_PREFIX,
            enable_key="wallbox_decision_history_enable",
            max_bytes_key="wallbox_decision_history_max_bytes",
            retention_key="wallbox_decision_history_retention_days",
            interval_key="wallbox_decision_history_interval_s",
            state=_wallbox_decision_history_state,
            signature_paths=(
                "decision.state",
                "decision.mode_public",
                "decision.mode_control",
                "decision.battery_request",
                "decision.reason",
                "decision.made_changes",
                "decision.scheduled_slot_active",
                "decision.price_boost_active",
                "decision.predump_wallbox_active",
                "inputs.cap_amp",
                "inputs.set_amp",
                "inputs.budget_stale",
                "inputs.budget_timeout",
                "storage_context.storage_state",
                "storage_context.curve_wb_relief_active",
                "storage_context.forecast_auto_relief_active",
                "storage_context.curve_forecast_wallbox_stop_active",
            ),
            default_interval_s=60,
            default_max_bytes=8 * 1024 * 1024,
            default_retention_days=2,
            logger=logger,
        )
    except Exception as exc:
        logger.debug("Wallbox-Decision History konnte nicht geschrieben werden: %s", exc)

def write_wallbox_decision_snapshot(ui_state, config, context=None):
    state = ui_state if isinstance(ui_state, dict) else {}
    context = context if isinstance(context, dict) else {}
    record = {
        "ts": int(time.time()),
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "service": "wallbox_manager",
        "decision": {
            "state": state.get("status_msg", "unknown"),
            "mode_public": state.get("wb_mode_active"),
            "mode_control": state.get("wb_control_mode"),
            "battery_request": state.get("battery_request", context.get("battery_request")),
            "reason": context.get("intent_reason", state.get("operator_hint_text", "")),
            "made_changes": bool(context.get("made_changes", False)),
            "scheduled_slot_active": bool(state.get("scheduled_slot_active", False)),
            "price_boost_active": bool(state.get("price_boost_active", False)),
            "predump_wallbox_active": bool(state.get("predump_wallbox_active", False)),
            "predump_wallbox_gate_open": bool(state.get("predump_wallbox_gate_open", False)),
        },
        "inputs": {
            "grid_w": int(round(_cfg_float(state.get("grid_w_raw"), 0.0))),
            "bat_w": int(round(_cfg_float(state.get("bat_w_raw"), 0.0))),
            "budget_raw_w": int(round(_cfg_float(state.get("wb_budget_raw_w"), 0.0))),
            "effective_budget_w": int(round(_cfg_float(state.get("wb_effective_budget_w"), 0.0))),
            "allowed_w": int(round(_cfg_float(state.get("avail_wb_w"), 0.0))),
            "cap_amp": int(round(_cfg_float(state.get("cap_amp"), 0.0))),
            "set_amp": int(round(_cfg_float(state.get("set_amp"), 0.0))),
            "detected_phases": int(round(_cfg_float(state.get("detected_phases"), 0.0))),
            "budget_stale": bool(context.get("budget_stale", False)),
            "budget_timeout": bool(context.get("budget_timeout", False)),
        },
        "wallboxes": state.get("wb_details", []),
        "storage_context": {
            "storage_state": context.get("storage_state"),
            "wbminsoc_gate_open": bool(state.get("wbminsoc_gate_open", False)),
            "curve_wb_relief_active": bool(state.get("curve_wb_relief_active", False)),
            "forecast_auto_relief_active": bool(state.get("forecast_auto_relief_active", False)),
            "curve_forecast_wallbox_stop_active": bool(state.get("curve_forecast_wallbox_stop_active", False)),
            "curve_forecast_wallbox_reason": state.get("curve_forecast_wallbox_reason"),
            "price_plan_storage_protect": bool(state.get("price_plan_storage_protect", False)),
        },
    }
    write_wallbox_decision_history(record, config)
    try:
        write_decision_surface_records(build_wallbox_decision_records(record), path=EMS_DECISION_FILE)
    except Exception as exc:
        logger.debug("EMS-Decision-Surface fuer Wallbox konnte nicht geschrieben werden: %s", exc)

def _remember_wallbox_decision_payload(c_data, payload):
    if not isinstance(c_data, dict) or not isinstance(payload, dict):
        return {}
    driver_command = wallbox_decision.driver_command_from_decision_payload(payload)
    payload["driver_command"] = driver_command
    c_data["_decision_payload"] = payload
    c_data["_driver_command"] = driver_command
    return driver_command

def _wallbox_decision_payload_or_default(
    c_data,
    status,
    *,
    public_mode=0,
    control_mode=None,
    cap_amp=0,
    allowed_w=0,
    detected_phases=1,
    max_amp=0,
    mode_label_text="",
    storage_state="",
    budget_timeout=False,
):
    if not isinstance(c_data, dict):
        c_data = {}
    try:
        c_id = int(c_data.get("id", 0) or 0)
    except Exception:
        c_id = 0
    existing = c_data.get("_decision_payload")
    if isinstance(existing, dict) and int(existing.get("wb_id", c_id) or 0) == c_id:
        command = c_data.get("_driver_command")
        if not isinstance(command, dict):
            command = wallbox_decision.driver_command_from_decision_payload(existing)
            existing["driver_command"] = command
        return existing, command

    public = normalize_wb_mode(public_mode)
    control = controller_mode(public) if control_mode is None else int(control_mode or 0)
    connected = _wb_status_connected(status)
    real_state = _wb_status_real_charging(status)
    real_power_w = _wb_status_real_power(status) if real_state else 0.0
    try:
        status_amp = int(round(float((status or {}).get("amp", 0) or 0)))
    except Exception:
        status_amp = 0
    charger = c_data.get("charger")
    driver_class_name = charger.__class__.__name__ if charger is not None else ""
    openwb_pro = bool(driver_class_name == "OpenWBProCharger")
    openwb_like = bool(driver_class_name == "OpenWBCharger")
    e3dc_native_toggle = bool(
        charger is not None
        and hasattr(charger, "set_amp_sonnenmodus")
        and not hasattr(charger, "set_pv_mode")
    )
    target_amp = int(cap_amp or 0)
    start_action = "NOOP"
    start_reason = "mode_off" if public == 0 else ("no_vehicle_connected" if not connected else "observe_only")
    if public > 0 and connected and target_amp > 0:
        start_action = "START" if status_amp <= 0 and int(c_data.get("current_set_amp", 0) or 0) <= 0 else "SET_CURRENT"
        start_reason = "target_current_available"
    payload = wallbox_decision.build_wallbox_decision_payload(
        wb_id=c_id,
        public_mode=public,
        control_mode=control,
        current_decision={
            "target_amp": target_amp,
            "raw_amp": target_amp,
            "physically_chargeable": bool(target_amp > 0 and connected),
            "house_fuse_limited": False,
            "limiting_reason": start_reason,
        },
        start_stop_decision={
            "action": start_action,
            "target_amp": target_amp if start_action in ("START", "SET_CURRENT") else 0,
            "hold_amp": target_amp if start_action in ("START", "SET_CURRENT") else 0,
            "reason": start_reason,
        },
        phase_recommendation={
            "action": "KEEP_PHASES",
            "target_phases": 0,
            "reason": "stable",
            "wait_s": 0,
            "remaining_s": 0,
        },
        allowed_w=allowed_w,
        detected_phases=detected_phases,
        current_amp=status_amp,
        current_set_amp=int(c_data.get("current_set_amp", 0) or 0),
        cap_amp=int(cap_amp or 0),
        max_amp=int(max_amp or 0),
        charger_connected=connected,
        hw_charging=real_state,
        hw_power_w=real_power_w,
        mode_label=mode_label_text or mode_label(public),
        storage_state=storage_state,
        driver_class_name=driver_class_name,
        openwb_like_charger=openwb_like,
        openwb_pro=openwb_pro,
        e3dc_native_toggle=e3dc_native_toggle,
        observe_only=bool(public == 0 or not connected),
        budget_timeout=budget_timeout,
    )
    command = wallbox_decision.driver_command_from_decision_payload(payload)
    payload["driver_command"] = command
    return payload, command


def _wallbox_e3dc_native_production_contract(
    c_data,
    status=None,
    config=None,
    *,
    charger_class_name="",
):
    box = c_data if isinstance(c_data, dict) else {}
    charger = box.get("charger")
    class_name = str(charger_class_name or "")
    if not class_name:
        class_name = charger.__class__.__name__ if charger is not None else str(box.get("_charger_class_name", "") or "")
    driver_variant = str((status or {}).get("driver_variant") or box.get("driver_variant") or "")
    return wallbox_decision.e3dc_native_production_contract(
        class_name,
        status,
        config,
        driver_variant=driver_variant,
        has_sonnenmodus_surface=bool(charger is not None and hasattr(charger, "set_amp_sonnenmodus")),
    )


def _apply_e3dc_native_production_contract_to_status(status, contract):
    if status is None or not isinstance(contract, dict):
        return status
    status["e3dc_native_contract"] = contract
    status["e3dc_native_runtime_path"] = str(contract.get("runtime_path", "not_e3dc") or "not_e3dc")
    status["e3dc_native_legacy_cpp_runtime_allowed"] = bool(contract.get("legacy_cpp_runtime_allowed", False))
    status["e3dc_native_fallback_role"] = str(contract.get("fallback_role", "") or "")
    status["e3dc_native_session_guard_required"] = bool(contract.get("session_guard_required", False))
    status["e3dc_native_charge_verification_required"] = bool(contract.get("charge_verification_required", False))
    return status


def _storage_floor_reachable(plan, battery_soc, floor_soc, hysteresis_pct=0.0):
    """Return whether the house battery floor is still reachable.

    ``can_reach_target`` may refer to the higher day target. Grundladung stabil
    only needs the wbminSoC floor: it should hold 6A until that floor is no
    longer reachable.
    """
    try:
        floor = float(floor_soc)
    except (TypeError, ValueError):
        return True
    if floor <= 0:
        return True

    try:
        current_soc = float(battery_soc)
    except (TypeError, ValueError):
        current_soc = None
    try:
        hyst = max(0.0, float(hysteresis_pct or 0.0))
    except (TypeError, ValueError):
        hyst = 0.0

    if current_soc is not None and current_soc >= floor - hyst:
        return True

    plan = plan if isinstance(plan, dict) else {}
    if not plan:
        return True
    if plan.get("can_reach_target") is True:
        return True

    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    for source in (plan, meta):
        raw = source.get("max_reachable_soc")
        if raw is None:
            continue
        try:
            return float(raw) >= floor - hyst
        except (TypeError, ValueError):
            continue

    if plan.get("can_reach_target") is False:
        return False
    return True


def _wbminsoc_discharge_taper_factor(soc_diff_pct, taper_above_pct):
    try:
        diff = max(0.0, float(soc_diff_pct or 0.0))
    except (TypeError, ValueError):
        return 1.0
    try:
        band = max(0.0, float(taper_above_pct or 0.0))
    except (TypeError, ValueError):
        band = 0.0
    if band <= 0.0 or diff >= band:
        return 1.0
    if diff <= 0.0:
        return 0.0
    linear = max(0.0, min(1.0, diff / band))
    return linear * linear


def _wbminsoc_floor_pv_start_ready(
    *,
    control_mode,
    wbminsoc_gate_open,
    cap_amp,
    real_surplus_w,
    min_power_w,
    startable_connected,
    manual_pause,
    grid_allowed,
):
    try:
        mode = int(control_mode or 0)
    except (TypeError, ValueError):
        mode = 0
    try:
        cap = int(round(float(cap_amp or 0)))
    except (TypeError, ValueError):
        cap = 0
    try:
        surplus = float(real_surplus_w or 0.0)
    except (TypeError, ValueError):
        surplus = 0.0
    try:
        minimum = float(min_power_w or 0.0)
    except (TypeError, ValueError):
        minimum = 0.0

    return bool(
        mode in (9, 10, 11)
        and not bool(wbminsoc_gate_open)
        and bool(startable_connected)
        and not bool(manual_pause)
        and not bool(grid_allowed)
        and cap > 0
        and surplus >= max(750.0, minimum - 250.0)
    )


def _wbminsoc_floor_battery_hard_stop_due(
    *,
    floor_battery_support_active,
    floor_pv_buffer_ready,
    floor_battery_age_s,
    floor_battery_stop_delay_s,
    floor_effective_surplus_w,
    floor_min_hold_w,
    floor_gross_surplus_w=0.0,
):
    if not bool(floor_battery_support_active):
        return False
    if bool(floor_pv_buffer_ready):
        return False
    try:
        age_s = float(floor_battery_age_s or 0.0)
    except (TypeError, ValueError):
        age_s = 0.0
    try:
        delay_s = max(0.0, float(floor_battery_stop_delay_s or 0.0))
    except (TypeError, ValueError):
        delay_s = 0.0
    try:
        surplus_w = float(floor_effective_surplus_w or 0.0)
    except (TypeError, ValueError):
        surplus_w = 0.0
    try:
        gross_surplus_w = float(floor_gross_surplus_w or 0.0)
    except (TypeError, ValueError):
        gross_surplus_w = 0.0
    try:
        min_hold_w = float(floor_min_hold_w or 0.0)
    except (TypeError, ValueError):
        min_hold_w = 0.0
    hold_threshold_w = max(750.0, min_hold_w - 250.0)
    return bool(
        age_s >= delay_s
        and surplus_w < hold_threshold_w
        and gross_surplus_w < hold_threshold_w
    )


def _e3dc_native_floor_battery_stop_now(
    *,
    e3dc_native_toggle,
    charger_connected,
    verified_active_charge,
    cap_amp,
    target_budget_w,
    floor_battery_support_active,
    floor_effective_surplus_w,
    floor_min_hold_w,
    priority_forced_stop=False,
    local_price_optimizing_active=False,
    local_grid_allowed=False,
    price_boost_wallbox_active=False,
    budget_timeout=False,
    predump_wallbox_active=False,
):
    """Return true for a verified native charge that must not use the battery floor."""
    try:
        cap = int(round(float(cap_amp or 0)))
    except (TypeError, ValueError):
        cap = 0
    try:
        budget_w = float(target_budget_w or 0.0)
    except (TypeError, ValueError):
        budget_w = 0.0
    try:
        surplus_w = float(floor_effective_surplus_w or 0.0)
    except (TypeError, ValueError):
        surplus_w = 0.0
    try:
        min_hold_w = float(floor_min_hold_w or 0.0)
    except (TypeError, ValueError):
        min_hold_w = 0.0
    hold_threshold_w = max(750.0, min_hold_w - 250.0)
    return bool(
        e3dc_native_toggle
        and charger_connected
        and verified_active_charge
        and cap <= 0
        and budget_w <= 0.0
        and floor_battery_support_active
        and surplus_w < hold_threshold_w
        and not priority_forced_stop
        and not local_price_optimizing_active
        and not local_grid_allowed
        and not price_boost_wallbox_active
        and not budget_timeout
        and not predump_wallbox_active
    )


def _phase_forecast_holds(plan):
    """Return whether today's forecast is good enough to keep WB phases calm."""
    plan = plan if isinstance(plan, dict) else {}
    if not plan:
        return False
    if plan.get("can_reach_target") is True:
        return True

    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}

    def _first_float(*values):
        for value in values:
            try:
                if value is None:
                    continue
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    target_soc = _first_float(
        plan.get("effective_target_soc"),
        plan.get("target_soc"),
        meta.get("curve_end_soc"),
    )
    max_soc = _first_float(
        plan.get("max_soc_pct"),
        plan.get("max_reachable_soc"),
        meta.get("max_soc_pct"),
    )
    if target_soc is not None and max_soc is not None:
        return max_soc >= target_soc - 0.5
    return False


def _storage_budget_forecast_holds(budget):
    """Return whether the fresh storage budget carries a forecast hold contract."""

    budget = budget if isinstance(budget, dict) else {}
    state = str(budget.get("storage_state", budget.get("state", "")) or "")
    if state != "parallel_curve_auto_hold":
        return False
    return bool(
        _truthy_config(budget.get("forecast_curve_landing_hold_active"))
        or _truthy_config(budget.get("sliding_horizon_active"))
    )


def _curve_forecast_wallbox_guard(plan, budget, config, now_ts=None, storage_forecast_hold_active=None):
    """Return the forecast gate for PV-curve wallbox battery assist."""

    plan = plan if isinstance(plan, dict) else {}
    budget = budget if isinstance(budget, dict) else {}
    config = config if isinstance(config, dict) else {}
    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    now = time.time() if now_ts is None else float(now_ts or 0.0)

    stop_wh = max(0.0, _cfg_float(config.get("wb_curve_forecast_stop_shortfall_wh"), 500.0))
    release_wh = max(
        0.0,
        min(stop_wh, _cfg_float(config.get("wb_curve_forecast_release_shortfall_wh"), 100.0)),
    )
    stop_gap_pct = max(0.0, _cfg_float(config.get("wb_curve_forecast_stop_gap_pct"), 0.5))
    release_gap_pct = max(
        0.0,
        min(stop_gap_pct, _cfg_float(config.get("wb_curve_forecast_release_gap_pct"), 0.2)),
    )
    release_hold_s = max(0.0, _cfg_float(config.get("wb_curve_forecast_release_hold_s"), 90.0))

    budget_forecast_hold_active = (
        _storage_budget_forecast_holds(budget)
        if storage_forecast_hold_active is None
        else bool(storage_forecast_hold_active)
    )
    can_reach_target = plan.get("can_reach_target", True)
    evening_shortfall_wh = max(
        0.0,
        _first_cfg_float(
            budget.get("evening_shortfall_wh"),
            plan.get("evening_shortfall_wh"),
            meta.get("evening_shortfall_wh"),
            default=0.0,
        ),
    )
    target_gap_pct = max(
        0.0,
        _first_cfg_float(
            budget.get("shortfall_target_gap_pct"),
            budget.get("forecast_floor_target_gap_pct"),
            plan.get("shortfall_target_gap_pct"),
            meta.get("shortfall_target_gap_pct"),
            default=0.0,
        ),
    )
    latest_charge_start_s = _ts_seconds(
        _first_present(
            budget.get("latest_charge_start_ts"),
            plan.get("latest_charge_start_ts"),
            meta.get("latest_charge_start_ts"),
        )
    )
    latest_charge_due = bool(latest_charge_start_s > 0.0 and now >= latest_charge_start_s)

    block_unreachable = bool(can_reach_target is False and not budget_forecast_hold_active)
    block_shortfall = bool(stop_wh > 0.0 and evening_shortfall_wh >= stop_wh)
    block_late_gap = bool(
        latest_charge_due
        and stop_gap_pct > 0.0
        and target_gap_pct >= stop_gap_pct
        and not budget_forecast_hold_active
    )
    block_requested = bool(block_unreachable or block_shortfall or block_late_gap)
    release_ready = bool(
        (budget_forecast_hold_active or can_reach_target is not False)
        and evening_shortfall_wh <= release_wh
        and (budget_forecast_hold_active or target_gap_pct <= release_gap_pct)
    )

    reason = "ok"
    if block_unreachable:
        reason = "target_unreachable"
    elif block_shortfall:
        reason = "evening_shortfall"
    elif block_late_gap:
        reason = "latest_charge_gap"
    elif not release_ready:
        reason = "recovery_wait"

    return {
        "block_requested": block_requested,
        "release_ready": release_ready,
        "assist_allowed": bool(release_ready and not block_requested),
        "reason": reason,
        "can_reach_target": can_reach_target,
        "storage_forecast_hold_active": budget_forecast_hold_active,
        "evening_shortfall_wh": round(evening_shortfall_wh, 0),
        "target_gap_pct": round(target_gap_pct, 3),
        "latest_charge_start_ts": latest_charge_start_s,
        "latest_charge_due": latest_charge_due,
        "stop_shortfall_wh": round(stop_wh, 0),
        "release_shortfall_wh": round(release_wh, 0),
        "stop_gap_pct": round(stop_gap_pct, 3),
        "release_gap_pct": round(release_gap_pct, 3),
        "release_hold_s": release_hold_s,
    }

def _normalize_current_step_amp(value, default=1.0):
    try:
        step = float(value or default)
    except (TypeError, ValueError):
        step = float(default)
    if step <= 0.11:
        return 0.1
    if step <= 0.51:
        return 0.5
    return 1.0


def _current_step_amp_for_charger(charger, default=1.0):
    return _normalize_current_step_amp(getattr(charger, "current_step_amp", default), default=default)


def _budget_current_step_amp_for_chargers(chargers, wb_charge_mode=None, wb_locked=None, wb_manual_pause=None):
    candidates = []
    for c_data in chargers or []:
        cid = int(c_data.get("id", 0) or 0)
        if wb_locked and bool(wb_locked.get(cid, False)):
            continue
        if wb_manual_pause and bool(wb_manual_pause.get(cid, False)):
            continue
        if wb_charge_mode and normalize_wb_mode(wb_charge_mode.get(cid, MODE_OFF)) == MODE_OFF:
            continue
        charger = c_data.get("charger")
        if charger is None:
            continue
        candidates.append(_current_step_amp_for_charger(charger, default=1.0))
    if len(candidates) == 1:
        return candidates[0]
    return 1.0


def _round_amp_down_to_step(value, step):
    step = _normalize_current_step_amp(step, default=1.0)
    rounded = math.floor(max(0.0, float(value or 0.0)) / step) * step
    return float(int(round(rounded))) if step >= 0.99 else round(rounded, 1)


def _round_amp_up_to_step(value, step):
    step = _normalize_current_step_amp(step, default=1.0)
    rounded = math.ceil(max(0.0, float(value or 0.0)) / step) * step
    return float(int(round(rounded))) if step >= 0.99 else round(rounded, 1)


def _amp_text(value):
    try:
        amp = float(value or 0.0)
    except (TypeError, ValueError):
        amp = 0.0
    if abs(amp - round(amp)) < 0.01:
        return str(int(round(amp)))
    return f"{amp:.1f}"


def _openwb_pro_effective_w_per_amp(status, phases=None, current_amp=0.0):
    def _local_float(value, default=0.0):
        try:
            return float(value if value is not None else default)
        except (TypeError, ValueError):
            return float(default)

    st = status if isinstance(status, dict) else {}
    phase_count = max(
        1,
        int(
            phases
            or st.get("phases_in_use")
            or st.get("phase_effective_phases")
            or st.get("phases_actual")
            or st.get("phases_target")
            or 1
        ),
    )
    nominal_w_per_amp = 230.0 * float(phase_count)
    real_power_w = max(
        0.0,
        _local_float(st.get("phase_power_sum_w", 0.0), 0.0),
        _local_float(st.get("real_power_w", 0.0), 0.0),
    )
    offered_amp = max(
        _local_float(st.get("offered_current_raw", 0.0), 0.0),
        _local_float(st.get("evse_current", 0.0), 0.0),
        _local_float(st.get("amp", 0.0), 0.0),
        _local_float(current_amp, 0.0),
    )
    if real_power_w <= 500.0 or offered_amp < 5.5:
        return 0.0
    measured_w_per_amp = real_power_w / offered_amp
    if measured_w_per_amp <= 0.0:
        return 0.0
    return max(nominal_w_per_amp * 0.55, min(nominal_w_per_amp * 1.10, measured_w_per_amp))


def _openwb_pro_curve_direct_amp(target_w, phases, charger_max_amp, *, assist_allowed=False, assist_max_gap_w=0.0, current_step_amp=1.0, watts_per_amp=0.0):
    phase_count = max(1, int(phases or 1))
    max_amp = max(6, int(charger_max_amp or 16))
    step = _normalize_current_step_amp(current_step_amp, default=1.0)
    target = max(0.0, float(target_w or 0.0))
    nominal_w_per_amp = 230.0 * phase_count
    effective_w_per_amp = max(
        nominal_w_per_amp * 0.55,
        min(nominal_w_per_amp * 1.10, float(watts_per_amp or nominal_w_per_amp)),
    )
    min_w = 6.0 * effective_w_per_amp
    if target < min_w:
        return 0, 0.0

    base_amp = max(6.0, min(float(max_amp), _round_amp_down_to_step(target / effective_w_per_amp, step)))
    assist_gap_w = 0.0
    if assist_allowed and base_amp < max_amp:
        next_amp = min(float(max_amp), _round_amp_up_to_step(base_amp + step, step))
        next_w = next_amp * effective_w_per_amp
        gap_w = max(0.0, next_w - target)
        max_gap_w = max(0.0, float(assist_max_gap_w or 0.0))
        if 0.0 < gap_w <= max_gap_w:
            return next_amp, gap_w
    return base_amp, assist_gap_w


def _openwb_pro_one_phase_max_amp(config, charger_id=None, charger_max_amp=16):
    cfg = config or {}
    keys = []
    try:
        cid = int(charger_id or 0)
    except (TypeError, ValueError):
        cid = 0
    if cid > 0:
        keys.extend((
            f"wb{cid}_openwb_pro_1p_max_amp",
            f"wb{cid}_one_phase_max_amp",
        ))
    keys.extend((
        "openwb_pro_1p_max_amp",
        "wb_one_phase_max_amp",
        "wallbox_one_phase_max_amp",
    ))
    raw = None
    for key in keys:
        value = cfg.get(key)
        if value is not None and str(value).strip() != "":
            raw = value
            break
    try:
        configured = float(raw if raw is not None else 20.0)
    except (TypeError, ValueError):
        configured = 20.0
    try:
        wallbox_max = float(charger_max_amp or 16)
    except (TypeError, ValueError):
        wallbox_max = 16.0
    return max(6.0, min(max(6.0, wallbox_max), max(6.0, configured)))


def _cap_openwb_pro_one_phase_amp(amp, phases, config, charger_id=None, charger_max_amp=16):
    try:
        phase_count = int(phases or 1)
    except (TypeError, ValueError):
        phase_count = 1
    try:
        value = float(amp or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if phase_count <= 1 and value > 0.0:
        return min(value, _openwb_pro_one_phase_max_amp(config, charger_id, charger_max_amp))
    return value


def _charger_max_amp(config, charger_id, fallback_amp=16):
    cid = int(charger_id or 1)
    for key in (f"wb{cid}_max_amp", f"wb{cid}_maxladestrom"):
        raw = (config or {}).get(key)
        if raw is not None and str(raw).strip() != "":
            return _amp_limit(raw, fallback_amp)
    return _amp_limit(fallback_amp, 16)

def _wb_cfg_float(config, charger_id, key, default=0.0, minimum=None, maximum=None):
    """Read a wallbox timing value with wb1_/wb2_ override and global fallback."""
    cfg = config or {}
    cid = int(charger_id or 1)
    raw = cfg.get(f"wb{cid}_{key}")
    if raw is None or str(raw).strip() == "":
        raw = cfg.get(f"wb_{key}", cfg.get(key, default))
    value = _cfg_float(raw, default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value

WALLBOX_TIMING_PROFILES = {
    "generic": {
        "restart_delay_s": 60.0,
        "min_charge_time_s": 300.0,
        "cloud_stop_delay_s": 180.0,
        "phase_change_hold_s": 180.0,
        "current_change_hold_s": 30.0,
        "command_start_stop_gap_s": 180.0,
        "command_phase_gap_s": 300.0,
    },
    "e3dc_native": {
        "restart_delay_s": 30.0,
        "min_charge_time_s": 300.0,
        "cloud_stop_delay_s": 120.0,
        "phase_change_hold_s": 180.0,
        "current_change_hold_s": 15.0,
        "command_start_stop_gap_s": 45.0,
        "command_phase_gap_s": 180.0,
    },
    "openwb_pro": {
        "restart_delay_s": 60.0,
        "min_charge_time_s": 300.0,
        "cloud_stop_delay_s": 150.0,
        "phase_change_hold_s": 180.0,
        "current_change_hold_s": 20.0,
        "command_start_stop_gap_s": 90.0,
        "command_phase_gap_s": 180.0,
    },
    "openwb_classic": {
        "restart_delay_s": 60.0,
        "min_charge_time_s": 300.0,
        "cloud_stop_delay_s": 180.0,
        "phase_change_hold_s": 180.0,
        "current_change_hold_s": 30.0,
        "command_start_stop_gap_s": 180.0,
        "command_phase_gap_s": 300.0,
    },
    "external": {
        "restart_delay_s": 90.0,
        "min_charge_time_s": 300.0,
        "cloud_stop_delay_s": 180.0,
        "phase_change_hold_s": 300.0,
        "current_change_hold_s": 30.0,
        "command_start_stop_gap_s": 180.0,
        "command_phase_gap_s": 300.0,
    },
}

_WALLBOX_PROFILE_BY_CLASS = {
    "E3DCCharger": "e3dc_native",
    "E3DCMultiConnectCharger": "e3dc_native",
    "OpenWBProCharger": "openwb_pro",
    "OpenWBCharger": "openwb_classic",
    "GoECharger": "external",
}

def _wb_timing_profile(config=None, charger_id=None, charger_class_name=""):
    """Resolve the cooldown profile for one wallbox.

    Config may override this with wb_timing_profile or wb1_timing_profile.
    Unknown values intentionally fall back to generic.
    """
    cfg = config or {}
    try:
        cid = int(charger_id or 1)
    except Exception:
        cid = 1
    raw = cfg.get(f"wb{cid}_timing_profile")
    if raw is None or str(raw).strip() == "":
        raw = cfg.get("wb_timing_profile", "")
    profile = str(raw or "").strip().lower()
    if profile in WALLBOX_TIMING_PROFILES:
        return profile
    class_name = str(charger_class_name or "").strip()
    return _WALLBOX_PROFILE_BY_CLASS.get(class_name, "generic")

def _wb_timing(config, charger_id, charger_class_name=""):
    """Central wallbox timing contract, configurable per WB and type profile."""
    profile = _wb_timing_profile(config, charger_id, charger_class_name)
    defaults = WALLBOX_TIMING_PROFILES.get(profile, WALLBOX_TIMING_PROFILES["generic"])
    return {
        "profile": profile,
        "restart_delay_s": _wb_cfg_float(config, charger_id, "restart_delay_s", defaults["restart_delay_s"], 0.0, 1800.0),
        "min_charge_time_s": _wb_cfg_float(config, charger_id, "min_charge_time_s", defaults["min_charge_time_s"], 0.0, 7200.0),
        "cloud_stop_delay_s": _wb_cfg_float(config, charger_id, "cloud_stop_delay_s", defaults["cloud_stop_delay_s"], 0.0, 3600.0),
        "phase_change_hold_s": _wb_cfg_float(config, charger_id, "phase_change_hold_s", defaults["phase_change_hold_s"], 0.0, 1800.0),
        "current_change_hold_s": _wb_cfg_float(config, charger_id, "current_change_hold_s", defaults["current_change_hold_s"], 0.0, 1800.0),
        "command_start_stop_gap_s": _wb_cfg_float(config, charger_id, "command_start_stop_gap_s", defaults["command_start_stop_gap_s"], 0.0, 1800.0),
        "command_phase_gap_s": _wb_cfg_float(config, charger_id, "command_phase_gap_s", defaults["command_phase_gap_s"], 0.0, 1800.0),
    }

def _compact_vehicle_identifier(value):
    return VehicleManager.compact_vehicle_identifier(value)

def _load_saved_car_profiles():
    return VehicleManager.load_saved_car_profiles()

def _manual_soc_vehicle_identity(charger_id, max_age_s=12 * 3600):
    """Return a fresh per-wallbox session vehicle identity from manual_soc_wbX.

    openWB Pro does not always report vehicle_id/RFID after a restart or API
    dropout. The per-wallbox SoC file is written when the user assigns a
    vehicle/start SoC and is therefore the best session fallback before falling
    back to the static wallbox assignment.
    """
    return VehicleManager.manual_soc_vehicle_identity(
        charger_id,
        max_age_s=max_age_s,
        ramdisk_dir=RAMDISK_DIR,
        now_ts=time.time(),
    )

def _session_vehicle_key_from_status(charger_id, status=None):
    return VehicleManager.session_vehicle_key_from_status(
        charger_id,
        status=status,
        manual_identity_loader=_manual_soc_vehicle_identity,
    )

def _vehicle_max_ac_phases(config, charger_id, status=None):
    """Return configured/inferred AC phase capability for the active vehicle.

    Explicit profile keys win. If no key exists, an AC power <= 7.6 kW is a
    practical 1p hint (32 A * 230 V), while higher values indicate 3p.
    """
    return VehicleManager.vehicle_max_ac_phases(
        config,
        charger_id,
        status=status,
        profiles=_load_saved_car_profiles(),
    )

def _valid_phase_count(value, default=0):
    return wallbox_decision.valid_phase_count(value, default)

def _truthy_config(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on", "ja")

def _falsey_config(value):
    return str(value).strip().lower() in ("0", "false", "no", "off", "nein")

def _openwb_phase_switch_capability(charger_class_name, status=None, config=None):
    """Return whether Python may send 1p/3p commands to this wallbox.

    openWB Pro exposes phase switching via connect.php and is an EVSE-like
    actuator. A normal openWB is a full energy manager; E3DC-Control only
    sends Secondary current + heartbeat and never internal phase commands.
    """
    return wallbox_decision.openwb_phase_switch_capability(charger_class_name, status, config)


def _wallbox_phase_switch_capability(charger_class_name, status=None, config=None):
    """Return the diagnostic phase switching surface for any wallbox type."""

    return wallbox_decision.wallbox_phase_switch_capability(charger_class_name, status, config)


def _apply_phase_contract_to_status(status, contract):
    if status is None or not isinstance(contract, dict):
        return status
    status["phase_contract"] = contract
    status["phase_actual_phases"] = int(contract.get("actual_phases", 0) or 0)
    status["phase_actual_source"] = str(contract.get("actual_source", "") or "")
    status["phase_effective_phases"] = int(contract.get("effective_phases", 0) or 0)
    status["phase_effective_source"] = str(contract.get("effective_source", "") or "")
    status["phase_cable_phases"] = int(contract.get("cable_phases", 0) or 0)
    status["phase_vehicle_max_phases"] = int(contract.get("vehicle_max_phases", 0) or 0)
    status["phase_wallbox_phases"] = int(contract.get("wallbox_phases", 0) or 0)
    status["phase_can_switch"] = bool(contract.get("can_switch_phases", False))
    status["phase_switch_capability"] = str(contract.get("phase_switch_capability", status.get("phase_switch_capability", "")) or "")
    status["phase_switch_source"] = str(contract.get("phase_switch_source", status.get("phase_switch_source", "")) or "")
    status["api_surface"] = str(contract.get("api_surface", status.get("api_surface", "")) or "")
    status["can_switch_phases"] = bool(contract.get("can_switch_phases", status.get("can_switch_phases", False)))
    return status


def _wallbox_charge_observation_contract(status=None, c_data=None, *, now_ts=None):
    previous = {}
    if isinstance(c_data, dict):
        previous = c_data.get("_charge_energy_sample")
        if not isinstance(previous, dict):
            previous = {}
    contract = wallbox_decision.charge_observation_contract(
        status,
        previous=previous,
        now_ts=time.time() if now_ts is None else now_ts,
    )
    if isinstance(c_data, dict):
        sample = contract.get("energy_sample")
        if isinstance(sample, dict):
            c_data["_charge_energy_sample"] = sample
        c_data["_charge_contract"] = contract
    return contract


def _apply_charge_contract_to_status(status, contract):
    if status is None or not isinstance(contract, dict):
        return status
    status["charge_contract"] = contract
    status["charge_truth"] = str(contract.get("truth", "not_charging") or "not_charging")
    status["charge_is_charging"] = bool(contract.get("is_charging", False))
    status["charge_counts_as_real"] = bool(contract.get("counts_as_real_charge", False))
    status["charge_confidence"] = str(contract.get("confidence", "") or "")
    status["charge_source"] = str(contract.get("source", "") or "")
    status["charge_power_w"] = round(float(contract.get("power_w", 0.0) or 0.0), 1)
    status["charge_raw_power_w"] = round(float(contract.get("raw_power_w", 0.0) or 0.0), 1)
    status["phantom_power_w"] = round(float(contract.get("phantom_power_w", 0.0) or 0.0), 1)
    status["charge_energy_increasing"] = bool(contract.get("energy_increasing", False))
    status["charge_energy_delta_wh"] = round(float(contract.get("energy_delta_wh", 0.0) or 0.0), 3)
    status["charge_energy_source"] = str(contract.get("energy_source", "") or "")
    return status


def _wallbox_charge_end_release_token(config, charger_id, public_mode=None):
    cfg = config or {}
    try:
        cid = int(charger_id or 1)
    except Exception:
        cid = 1
    fields = {
        "mode": normalize_wb_mode(public_mode if public_mode is not None else cfg.get(f"wb{cid}_mode", 0)),
        "global_max_amp": str(cfg.get("wbmaxladestrom", cfg.get("wb_max_amp", ""))),
        "max_amp": str(cfg.get(f"wb{cid}_max_amp", cfg.get(f"wb{cid}_maxladestrom", ""))),
        "target_soc": str(cfg.get(f"wb{cid}_target_soc", cfg.get("car_target_soc", ""))),
        "max_soc": str(cfg.get(f"wb{cid}_max_soc_si", cfg.get("car_max_soc_si", ""))),
        "charge_power": str(cfg.get(f"wb{cid}_charge_power", cfg.get("car_charge_power", ""))),
        "capacity": str(cfg.get(f"wb{cid}_capacity", cfg.get("car_capacity", ""))),
        "car_id": str(cfg.get(f"wb{cid}_car_id", "")),
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


def _detect_wallbox_charge_end_user_release(c_data, config, charger_id, public_mode=None):
    if not isinstance(c_data, dict):
        return ""
    token = _wallbox_charge_end_release_token(config, charger_id, public_mode)
    previous = str(c_data.get("_charge_end_release_token") or "")
    c_data["_charge_end_release_token"] = token
    if previous and previous != token and bool(c_data.get("_bev_full_blocked", False)):
        return "wallbox_php_limit_or_profile_change"
    return ""


def _apply_charge_end_contract_to_status(status, contract):
    if status is None or not isinstance(contract, dict):
        return status
    status["charge_end_contract"] = contract
    status["charge_end_latched"] = bool(contract.get("latched", False))
    status["charge_end_action"] = str(contract.get("action", "") or "")
    status["charge_end_reason"] = str(contract.get("reason", "") or "")
    status["charge_end_exception"] = str(contract.get("exception", "") or "")
    status["charge_end_start_blocked"] = bool(contract.get("start_blocked", False))
    return status


def _clear_wallbox_charge_end_latch(c_data, reason=""):
    if not isinstance(c_data, dict):
        return
    c_data["_bev_full_blocked"] = False
    c_data["_bev_full_block_reason"] = ""
    c_data["_charge_end_release_reason"] = str(reason or "")
    c_data["_charge_end_release_ts"] = time.time()
    c_data["abort_count"] = 0
    c_data["abort_cooldown_ts"] = 0.0
    c_data["_openwb_start_reject_soft_until"] = 0.0
    c_data["_wb_stop_sent_active"] = False


def _wallbox_charge_end_latch_contract(
    c_data,
    status=None,
    *,
    now_ts=None,
    config=None,
    charger_id=None,
    public_mode=None,
    had_confirmed_charge=None,
    allow_new_latch=False,
    user_release_exception="",
    vehicle_changed=False,
    disconnected_release=False,
    mode_off=False,
    start_verifying=False,
    manager_stop_active=False,
    grace_active=False,
    target_soc_reached=False,
):
    if not isinstance(c_data, dict):
        return {}
    cid = charger_id if charger_id is not None else c_data.get("id", 1)
    release_exception = str(user_release_exception or "")
    if not release_exception and config is not None:
        release_exception = _detect_wallbox_charge_end_user_release(c_data, config, cid, public_mode)
    if (
        not release_exception
        and bool(c_data.get("_bev_full_blocked", False))
        and str(c_data.get("_bev_full_block_reason") or "") == "vehicle_charge_ended"
        and openwb_pro_session.is_openwb_pro_charger(c_data.get("charger"))
    ):
        target_not_reached, car_soc, target_soc = _wallbox_target_soc_not_reached(config, cid, status)
        if target_not_reached:
            release_exception = "vehicle_soc_below_target"
            c_data["_charge_end_release_car_soc"] = round(float(car_soc), 1)
            c_data["_charge_end_release_target_soc"] = round(float(target_soc), 1)
    phase_transition_grace = _wallbox_phase_transition_grace_active(c_data, status, now_ts=now_ts)
    openwb_pro_transient_grace = False
    if openwb_pro_session.is_openwb_pro_charger(c_data.get("charger")):
        phase_sequence = c_data.get("_openwb_pro_phase_sequence")
        openwb_pro_transient_grace = bool(
            (isinstance(phase_sequence, dict) and phase_sequence and str(phase_sequence.get("stage") or "") != "ready")
            or float(c_data.get("_openwb_pro_start_wakeup_allowed_after", 0.0) or 0.0)
            > float(now_ts if now_ts is not None else time.time())
        )
    if (
        (phase_transition_grace or openwb_pro_transient_grace)
        and not release_exception
        and bool(c_data.get("_bev_full_blocked", False))
    ):
        release_exception = "phase_switch_transition"
    if had_confirmed_charge is None:
        had_confirmed_charge = bool(c_data.get("_aha_real_charge_confirmed", False))
    contract = wallbox_decision.charge_end_latch_contract(
        status,
        previous_latched=bool(c_data.get("_bev_full_blocked", False)),
        previous_reason=str(c_data.get("_bev_full_block_reason") or ""),
        had_confirmed_charge=bool(had_confirmed_charge),
        allow_new_latch=bool(allow_new_latch),
        user_release_exception=release_exception,
        vehicle_changed=bool(vehicle_changed),
        disconnected_release=bool(disconnected_release),
        mode_off=bool(mode_off),
        start_verifying=bool(start_verifying),
        manager_stop_active=bool(manager_stop_active),
        grace_active=bool(grace_active or phase_transition_grace or openwb_pro_transient_grace),
        target_soc_reached=bool(target_soc_reached),
        now_ts=time.time() if now_ts is None else now_ts,
    )
    c_data["_charge_end_contract"] = contract
    _apply_charge_end_contract_to_status(status, contract)
    action = str(contract.get("action", "") or "")
    if action == "clear":
        _clear_wallbox_charge_end_latch(c_data, contract.get("exception", ""))
    elif action == "latch":
        c_data["_bev_full_blocked"] = True
        c_data["_bev_full_block_reason"] = str(contract.get("reason") or "vehicle_charge_ended")
        c_data["_charge_end_latched_ts"] = float(contract.get("ts", time.time()) or time.time())
    return contract


def _wallbox_transient_hold_contract(
    c_data,
    status=None,
    *,
    now_ts=None,
    phase_grace_s=360.0,
    openwb_phase_capable=None,
    current_amp=0,
    current_set_amp=None,
    hw_charging=None,
    hw_power_w=None,
    phase_wait_active=False,
    start_hold_active=False,
    native_start_grace_active=False,
    priority_forced_stop=False,
    mode_off=False,
    budget_timeout=False,
):
    if not isinstance(c_data, dict):
        return {}
    st = status or {}
    now = time.time() if now_ts is None else float(now_ts or 0.0)
    last_switch = float(c_data.get("_last_phase_switch_ts", 0.0) or 0.0)
    phase_age_s = (now - last_switch) if last_switch > 0.0 and now > 0.0 else 999999.0
    charger = c_data.get("charger")
    charger_class = charger.__class__.__name__ if charger is not None else str(c_data.get("_charger_class_name", "") or "")
    driver_variant = str(getattr(charger, "driver_variant", "") or st.get("driver_variant", "") or "")
    phase_capable = bool(
        openwb_phase_capable
        if openwb_phase_capable is not None
        else (
            hasattr(charger, "set_phases")
            or charger_class == "OpenWBProCharger"
            or driver_variant == "e3dc_multi_connect"
        )
    )
    target = _valid_phase_count(st.get("phases_target"), 0)
    if not target:
        target = _valid_phase_count(c_data.get("_phase_target_cmd"), 0)
    offered = max(
        _cfg_float(st.get("amp"), 0.0),
        _cfg_float(st.get("offered_current_raw"), 0.0),
        _cfg_float(st.get("offered_current"), 0.0),
    )
    if current_set_amp is None:
        current_set_amp = c_data.get("current_set_amp", 0.0)
    if hw_charging is None:
        hw_charging = _wb_status_real_charging(st)
    if hw_power_w is None:
        hw_power_w = _wb_status_real_power(st)
    return wallbox_decision.transient_hold_contract(
        st,
        charger_connected=_wb_status_connected(st),
        hw_charging=bool(hw_charging),
        hw_power_w=hw_power_w,
        current_amp=current_amp,
        current_set_amp=current_set_amp,
        offered_amp=offered,
        phase_capable=phase_capable,
        phase_target=target,
        phase_actual=st.get("phases_actual", 0),
        phases_in_use=st.get("phases_in_use", st.get("number_phases", 0)),
        phase_command_age_s=phase_age_s,
        phase_command_grace_s=phase_grace_s,
        phase_wait_active=bool(phase_wait_active),
        start_hold_active=bool(start_hold_active),
        native_start_grace_active=bool(native_start_grace_active),
        priority_forced_stop=bool(priority_forced_stop),
        mode_off=bool(mode_off),
        budget_timeout=bool(budget_timeout),
    )


def _wallbox_phase_transition_grace_active(c_data, status=None, *, now_ts=None, grace_s=360.0):
    """Return true while a commanded phase switch may interrupt charging."""
    contract = _wallbox_transient_hold_contract(
        c_data,
        status,
        now_ts=now_ts,
        phase_grace_s=grace_s,
    )
    return bool(contract.get("phase_transition_grace_active", False))


def _openwb_phase_command_grace_active(c_data, status=None, *, now_ts=None, grace_s=120.0):
    """Return true shortly after an openWB phase command was sent."""
    contract = _wallbox_transient_hold_contract(
        c_data,
        status,
        now_ts=now_ts,
        phase_grace_s=grace_s,
        openwb_phase_capable=True,
    )
    return bool(contract.get("phase_transition_offer_active", False))


def _apply_running_ramp_contract(
    c_data,
    start_stop_decision,
    *,
    target_amp,
    current_amp,
    current_set_amp,
    charger_connected,
    hw_charging,
    hw_power_w,
    now_ts,
    min_amp=6,
    max_amp=32,
    ramp_interval_s=7.0,
    up_step_a=1,
    down_step_a=1,
    bypass=False,
):
    if not isinstance(c_data, dict):
        return int(target_amp or 0), start_stop_decision
    contract = wallbox_decision.running_charge_ramp_contract(
        target_amp=target_amp,
        current_amp=current_amp,
        current_set_amp=current_set_amp,
        charger_connected=charger_connected,
        hw_charging=hw_charging,
        hw_power_w=hw_power_w,
        now_ts=now_ts,
        last_ramp_ts=c_data.get("_running_ramp_last_ts", 0.0),
        min_amp=min_amp,
        max_amp=max_amp,
        ramp_interval_s=ramp_interval_s,
        up_step_a=up_step_a,
        down_step_a=down_step_a,
        bypass=bypass,
    )
    c_data["_ramp_contract"] = contract
    if contract.get("changed"):
        c_data["_running_ramp_last_ts"] = float(contract.get("next_ramp_ts", now_ts) or now_ts)
    applied_amp = int(contract.get("applied_amp", target_amp) or 0)
    if isinstance(start_stop_decision, dict) and int(target_amp or 0) != applied_amp:
        start_stop_decision = dict(start_stop_decision)
        start_stop_decision["raw_target_amp"] = int(target_amp or 0)
        start_stop_decision["target_amp"] = applied_amp
        if int(start_stop_decision.get("hold_amp", 0) or 0) > 0:
            start_stop_decision["hold_amp"] = applied_amp
        start_stop_decision["ramp_contract"] = contract
        start_stop_decision["ramp_limited"] = bool(contract.get("limited", False))
        start_stop_decision["reason"] = "%s+ramp_%s" % (
            str(start_stop_decision.get("reason", start_stop_decision.get("action", "set_current")) or "set_current"),
            str(contract.get("reason", "limited") or "limited"),
        )
    return applied_amp, start_stop_decision


def _remember_phase_target(c_data, phases, now_ts=None, hold_s=900.0):
    """Remember our last phase command while openWB reports stale targets."""
    if not isinstance(c_data, dict):
        return
    phases = _valid_phase_count(phases, 0)
    if not phases:
        c_data["_phase_target_cmd"] = 0
        c_data["_phase_target_cmd_until"] = 0.0
        return
    now_ts = time.time() if now_ts is None else float(now_ts)
    c_data["_phase_target_cmd"] = int(phases)
    c_data["_phase_target_cmd_until"] = now_ts + max(30.0, float(hold_s or 0.0))

def _effective_phase_target(status, c_data, now_ts=None):
    """Return the phase target that must be used for physical budget checks."""
    now_ts = time.time() if now_ts is None else float(now_ts)
    if isinstance(c_data, dict):
        cmd = _valid_phase_count(c_data.get("_phase_target_cmd"), 0)
        until = float(c_data.get("_phase_target_cmd_until", 0.0) or 0.0)
        if cmd and now_ts <= until:
            return cmd
        if until and now_ts > until:
            c_data["_phase_target_cmd"] = 0
            c_data["_phase_target_cmd_until"] = 0.0
    return _valid_phase_count((status or {}).get("phases_target"), 0)


def _openwb_pro_curve_direct_phases(status, c_data, detected_phases=1, now_ts=None):
    st = status or {}
    detected = _valid_phase_count(detected_phases, 1) or 1
    now_ts = time.time() if now_ts is None else float(now_ts)
    remembered_target = 0
    if isinstance(c_data, dict):
        until = float(c_data.get("_phase_target_cmd_until", 0.0) or 0.0)
        if until and now_ts <= until:
            remembered_target = _valid_phase_count(c_data.get("_phase_target_cmd"), 0)
    target = _valid_phase_count(st.get("phases_target"), 0)
    actual = _valid_phase_count(st.get("phases_actual"), 0)
    in_use = _valid_phase_count(st.get("phases_in_use"), 0)
    current_set = 0.0
    if isinstance(c_data, dict):
        try:
            current_set = float(c_data.get("current_set_amp", 0.0) or 0.0)
        except (TypeError, ValueError):
            current_set = 0.0
    try:
        offered = float(st.get("amp", st.get("offered_current", 0.0)) or 0.0)
    except (TypeError, ValueError):
        offered = 0.0
    active_or_offered = bool(
        remembered_target
        or _wb_status_real_charging(st)
        or _wb_status_real_power(st) > 500.0
        or offered >= 5.5
        or current_set >= 5.5
    )
    if not active_or_offered:
        return detected
    return max(
        1,
        min(
            3,
            max(
                detected,
                remembered_target,
                in_use,
                actual,
                target,
            ),
        ),
    )


def _phase_transition_pending_for_start_reject(
    c_data,
    *,
    now_ts=None,
    phase_confirm_timeout_s=120.0,
    phase_effective_hold_s=0.0,
):
    """Return true while a recent phase command may temporarily hide power."""
    if not isinstance(c_data, dict):
        return False
    try:
        now_value = time.time() if now_ts is None else float(now_ts)
        confirm_s = max(30.0, float(phase_confirm_timeout_s or 0.0))
        hold_s = max(0.0, float(phase_effective_hold_s or 0.0))
        grace_s = max(confirm_s, hold_s) + 60.0
        pending_since = float(c_data.get("_phase_3p_pending_since", 0.0) or 0.0)
        last_switch = float(c_data.get("_last_phase_switch_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False

    if pending_since > 0.0 and 0.0 <= now_value - pending_since <= grace_s:
        return True
    if last_switch > 0.0 and 0.0 <= now_value - last_switch <= min(grace_s, confirm_s + 30.0):
        return True
    return False


def _wallbox_executable_budget(
    status=None,
    c_data=None,
    *,
    allowed_w=0.0,
    detected_phases=1,
    min_amp=6,
    vehicle_max_phases=0,
    phase_cap_phases=0,
    phase_switch_phases=0,
    phase_target=0,
    openwb_phase_capable=False,
    can_switch_to_1p=False,
    require_one_phase=False,
    grid_unlocked=False,
    phase_capability=None,
    charger_class_name="",
    driver_variant="",
):
    """Return whether the current budget can physically start or hold charging.

    This is deliberately a small physics gate: 6A * 230V * active phases is
    the minimum useful load. openWB/openWB Pro may avoid a 3p deadlock by first
    switching to 1p when the 1p minimum is available.
    """
    return wallbox_decision.wallbox_executable_budget(
        status,
        c_data,
        allowed_w=allowed_w,
        detected_phases=detected_phases,
        min_amp=min_amp,
        vehicle_max_phases=vehicle_max_phases,
        phase_cap_phases=phase_cap_phases,
        phase_switch_phases=phase_switch_phases,
        phase_target=phase_target,
        openwb_phase_capable=openwb_phase_capable,
        can_switch_to_1p=can_switch_to_1p,
        require_one_phase=require_one_phase,
        grid_unlocked=grid_unlocked,
        phase_capability=phase_capability,
        charger_class_name=charger_class_name,
        driver_variant=driver_variant,
    )

def _openwb_physical_amp_down_required(
    public_mode,
    current_amp,
    cap_amp,
    grid_power_w,
    *,
    charger_is_openwb_like=False,
    grid_allowed=False,
    price_active=False,
    price_boost_active=False,
    predump_active=False,
    threshold_w=-250.0,
):
    """Return whether an openWB-like charger must follow a lower physical cap now."""
    try:
        current_i = int(round(float(current_amp or 0)))
    except (TypeError, ValueError):
        current_i = 0
    try:
        cap_i = int(round(float(cap_amp or 0)))
    except (TypeError, ValueError):
        cap_i = 0
    try:
        grid_w = float(grid_power_w or 0.0)
    except (TypeError, ValueError):
        grid_w = 0.0
    try:
        threshold = float(threshold_w if threshold_w is not None else -250.0)
    except (TypeError, ValueError):
        threshold = -250.0

    return bool(
        charger_is_openwb_like
        and storage_floor_mode(public_mode)
        and cap_i > 0
        and current_i > cap_i
        and grid_w > threshold
        and not (grid_allowed or price_active or price_boost_active or predump_active)
    )

def _log_state_once(state_obj, slot, key, message, level="info", min_interval_s=0.0):
    """Log normal control states only when their semantic state changes."""
    now_ts = time.time()
    store = state_obj.setdefault("_quiet_log_state", {})
    prev = store.get(slot) or {}
    due = bool(min_interval_s and now_ts - float(prev.get("ts", 0.0) or 0.0) >= min_interval_s)
    if prev.get("key") == key and not due:
        return False
    getattr(logger, level)(message)
    store[slot] = {"key": key, "ts": now_ts}
    return True

def _build_wallbox_operator_hint(
    public_mode,
    current_price_ct,
    price_limit,
    *,
    mode5_grid_allowed=False,
    price_boost_active=False,
    market_plan_active=False,
    market_plan_action=None,
    scheduled_slot_active=False,
    predump_wallbox_active=False,
    budget_stale=False,
    budget_timeout=False,
    budget_age_s=0.0,
    house_fuse_limited=False,
    house_fuse_cap_amp=0,
    connected=True,
    cap_amp=0,
    battery_departure_active=False,
    battery_departure_blocked=False,
    battery_departure_label="",
    battery_departure_start_label="",
    battery_departure_reason="",
):
    """Explain the active user-facing decision without changing control logic."""
    return wallbox_decision.wallbox_operator_hint_contract(
        public_mode,
        current_price_ct,
        price_limit,
        mode5_grid_allowed=mode5_grid_allowed,
        price_boost_active=price_boost_active,
        market_plan_active=market_plan_active,
        market_plan_action=market_plan_action,
        scheduled_slot_active=scheduled_slot_active,
        predump_wallbox_active=predump_wallbox_active,
        budget_stale=budget_stale,
        budget_timeout=budget_timeout,
        budget_age_s=budget_age_s,
        house_fuse_limited=house_fuse_limited,
        house_fuse_cap_amp=house_fuse_cap_amp,
        connected=connected,
        cap_amp=cap_amp,
        battery_departure_active=battery_departure_active,
        battery_departure_blocked=battery_departure_blocked,
        battery_departure_label=battery_departure_label,
        battery_departure_start_label=battery_departure_start_label,
        battery_departure_reason=battery_departure_reason,
    )


def _wallbox_detail_status(
    status,
    c_data=None,
    *,
    public_mode=0,
    cap_amp=0,
    allowed_w=0,
    budget_stale=False,
    budget_timeout=False,
    mode5_grid_allowed=False,
    scheduled_slot_active=False,
    price_boost_active=False,
    predump_wallbox_active=False,
    wbminsoc_gate_open=True,
    house_fuse_limited=False,
    house_fuse_cap_amp=0,
    detected_phases=1,
    min_amp=6,
    physical_budget=None,
    vehicle_max_phases=0,
    openwb_phase_capable=False,
    battery_departure_state=None,
):
    """Compact per-wallbox UI state with a reason, not just "Bereit"."""
    c_data = c_data or {}
    mode = normalize_wb_mode(public_mode)
    connected = _wb_status_connected(status)
    real_charging = _wb_status_real_charging(status)
    real_power_w = _wb_status_real_power(status)
    if not isinstance(physical_budget, dict):
        physical_budget = _wallbox_executable_budget(
            status,
            c_data,
            allowed_w=allowed_w,
            detected_phases=detected_phases,
            min_amp=min_amp,
            vehicle_max_phases=vehicle_max_phases,
            openwb_phase_capable=openwb_phase_capable,
        )
    primary_phase_warning = _openwb_primary_grid_phase_warning(
        status,
        c_data,
        public_mode=mode,
        cap_amp=cap_amp,
        scheduled_slot_active=scheduled_slot_active,
        mode5_grid_allowed=mode5_grid_allowed,
        price_boost_active=price_boost_active,
        vehicle_max_phases=vehicle_max_phases,
    )
    under_acceptance_warning = _wallbox_vehicle_under_acceptance_warning(
        status,
        c_data,
        cap_amp=cap_amp,
        detected_phases=detected_phases,
        min_amp=min_amp,
    )
    detail = wallbox_decision.wallbox_detail_status_contract(
        status,
        c_data,
        public_mode=public_mode,
        cap_amp=cap_amp,
        allowed_w=allowed_w,
        budget_stale=budget_stale,
        budget_timeout=budget_timeout,
        mode5_grid_allowed=mode5_grid_allowed,
        scheduled_slot_active=scheduled_slot_active,
        price_boost_active=price_boost_active,
        predump_wallbox_active=predump_wallbox_active,
        wbminsoc_gate_open=wbminsoc_gate_open,
        house_fuse_limited=house_fuse_limited,
        detected_phases=detected_phases,
        min_amp=min_amp,
        physical_budget=physical_budget,
        battery_departure_state=battery_departure_state,
        primary_phase_warning=primary_phase_warning,
        under_acceptance_warning=under_acceptance_warning,
        car_soc_rule_confirmed=_car_soc_rule_confirmed(status),
        connected=connected,
        real_charging=real_charging,
        real_power_w=real_power_w,
        now_ts=time.time(),
    )
    for key in (
        "openwb_pro_vehicle_finished_contract",
        "openwb_pro_vehicle_finished_action",
        "openwb_pro_vehicle_finished_reason",
        "openwb_pro_vehicle_finished_allow_new_latch",
        "openwb_pro_vehicle_finished_blockers",
        "openwb_pro_vehicle_finished_had_confirmed_charge",
        "openwb_pro_vehicle_finished_time_since_start_s",
        "openwb_pro_temporary_stop_contract",
        "openwb_pro_temporary_stop_active",
        "openwb_pro_temporary_stop_reason",
        "openwb_pro_temporary_stop_state_hint",
        "openwb_pro_temporary_stop_waiting",
        "openwb_pro_start_retry_guard_contract",
    ):
        if isinstance(status, dict) and key in status:
            detail[key] = status[key]
    return detail


def _priority_target_connected(chargers, valid_chargers_status, priority_mode):
    """Return true only when the selected priority wallbox currently has a vehicle."""
    try:
        priority_id = int(priority_mode)
    except Exception:
        priority_id = 0
    if priority_id not in (1, 2):
        return False
    contract = wallbox_decision.multi_wallbox_allocation_contract(
        valid_chargers_status or [],
        priority_mode=priority_id,
    )
    if bool(contract.get("priority_target_connected", False)):
        return True
    for item in valid_chargers_status or []:
        try:
            if int(item.get("id", 0) or 0) == priority_id:
                return bool(_wb_status_connected(item.get("status")))
        except Exception:
            continue
    for c_data in chargers or []:
        try:
            if int(c_data.get("id", 0) or 0) == priority_id:
                return bool(c_data.get("is_connected") or c_data.get("connected"))
        except Exception:
            continue
    return False


def _build_wallbox_detail_list(
    chargers,
    valid_chargers_status,
    config,
    *,
    public_mode=0,
    cap_amp=0,
    allowed_w=0,
    budget_stale=False,
    budget_timeout=False,
    mode5_grid_allowed=False,
    scheduled_slot_active=False,
    price_boost_active=False,
    predump_wallbox_active=False,
    wbminsoc_gate_open=True,
    house_fuse_limited=False,
    house_fuse_cap_amp=0,
    detected_phases=1,
    min_amp=6,
    wb_global_max_amp=16,
    battery_departure_states=None,
    priority_mode=0,
    manual_pause=None,
):
    """Build slot-true UI details even when Python control is disabled."""
    try:
        priority_mode = int(priority_mode)
    except Exception:
        priority_mode = 0
    if priority_mode not in (0, 1, 2):
        priority_mode = 0
    status_by_id = {}
    manual_pause = manual_pause if isinstance(manual_pause, dict) else {}
    for item in valid_chargers_status or []:
        try:
            status_by_id[int(item.get("id"))] = item.get("status")
        except Exception:
            continue
    priority_target_connected = _priority_target_connected(
        chargers,
        valid_chargers_status,
        priority_mode,
    )

    details = []
    for c_data in chargers or []:
        try:
            c_id = int(c_data.get("id", len(details) + 1))
        except Exception:
            c_id = len(details) + 1
        st = status_by_id.get(c_id)
        charge_contract = dict(c_data.get("_charge_contract") or {})
        if not charge_contract:
            charge_contract = _wallbox_charge_observation_contract(st, c_data, now_ts=time.time())
        _apply_charge_contract_to_status(st, charge_contract)
        stop_display_state = _manager_stop_display_state(c_data)
        if stop_display_state.get("active", False):
            charge_contract = _apply_manager_stop_display_to_status(st, charge_contract, stop_display_state)
        connected = _wb_status_connected(st)
        real_state = _wb_status_real_charging(st)
        real_power_w = _wb_status_real_power(st) if real_state else 0.0
        bev_blocked = bool(c_data.get("_bev_full_blocked", False))
        charge_end_contract = dict(c_data.get("_charge_end_contract") or {})
        if not charge_end_contract:
            charge_end_contract = _wallbox_charge_end_latch_contract(
                c_data,
                st,
                now_ts=time.time(),
                config=config,
                charger_id=c_id,
                public_mode=public_mode,
                allow_new_latch=False,
                disconnected_release=not connected,
                mode_off=normalize_wb_mode(public_mode) == MODE_OFF,
            )
        ramp_contract = c_data.get("_ramp_contract")
        if not isinstance(ramp_contract, dict):
            ramp_contract = {}
        physical_detail = c_data.get("_physical_budget")
        if not isinstance(physical_detail, dict):
            physical_detail = {}
        phase_capability = c_data.get("_openwb_phase_capability")
        if not isinstance(phase_capability, dict):
            phase_capability = {}
        charger = c_data.get("charger") if isinstance(c_data, dict) else None
        charger_class_name = charger.__class__.__name__ if charger is not None else str(c_data.get("_charger_class_name", "") or "")
        e3dc_native_contract = c_data.get("_e3dc_native_contract")
        if not isinstance(e3dc_native_contract, dict):
            e3dc_native_contract = _wallbox_e3dc_native_production_contract(
                c_data,
                st,
                config,
                charger_class_name=charger_class_name,
            )
            if e3dc_native_contract.get("enabled", False):
                c_data["_e3dc_native_contract"] = e3dc_native_contract
        if e3dc_native_contract.get("enabled", False):
            _apply_e3dc_native_production_contract_to_status(st, e3dc_native_contract)
        if not phase_capability:
            phase_capability = _wallbox_phase_switch_capability(charger_class_name, st, config)
        phase_contract = dict(physical_detail.get("phase_contract") or c_data.get("_phase_contract") or {})
        if not phase_contract:
            phase_contract = wallbox_decision.phase_observation_contract(
                st,
                c_data,
                detected_phases=detected_phases,
                vehicle_max_phases=_vehicle_max_ac_phases(config, c_id, st),
                phase_target=_valid_phase_count((st or {}).get("phases_target"), 0),
                phase_capability=phase_capability,
                charger_class_name=charger_class_name,
                driver_variant=str((st or {}).get("driver_variant", "") or ""),
            )
        _apply_phase_contract_to_status(st, phase_contract)
        phase_policy_can_switch = bool(
            charger_class_name in ("OpenWBCharger", "OpenWBProCharger")
            and phase_capability.get("can_switch", False)
        )
        departure_detail = None
        if isinstance(battery_departure_states, dict):
            departure_detail = battery_departure_states.get(c_id)
        detail_state = _wallbox_detail_status(
            st,
            c_data,
            public_mode=public_mode,
            cap_amp=cap_amp,
            allowed_w=allowed_w,
            budget_stale=budget_stale,
            budget_timeout=budget_timeout,
            mode5_grid_allowed=mode5_grid_allowed,
            scheduled_slot_active=scheduled_slot_active,
            price_boost_active=price_boost_active,
            predump_wallbox_active=predump_wallbox_active,
            wbminsoc_gate_open=wbminsoc_gate_open,
            house_fuse_limited=house_fuse_limited,
            house_fuse_cap_amp=house_fuse_cap_amp,
            detected_phases=detected_phases,
            min_amp=min_amp,
            physical_budget=physical_detail,
            vehicle_max_phases=_vehicle_max_ac_phases(config, c_id, st),
            openwb_phase_capable=phase_policy_can_switch,
            battery_departure_state=departure_detail,
        )
        manual_pause_active = bool(manual_pause.get(c_id, False) or c_data.get("_manual_pause_active", False))
        if manual_pause_active:
            detail_state = {
                "state": "Manuell pausiert",
                "state_level": "warning",
                "state_reason": "Nutzerpause aktiv; Play gibt die bestehende Regelung wieder frei.",
                "min_power_w": detail_state.get("min_power_w", 0),
            }

        try:
            status_amp = int(round(float((st or {}).get("amp", 0) or 0)))
        except Exception:
            status_amp = 0
        try:
            current_amp = int(round(float(c_data.get("current_set_amp", 0) or 0)))
        except Exception:
            current_amp = 0
        if not connected:
            shown_amp = 0
        elif real_state:
            shown_amp = max(current_amp, status_amp)
        elif not c_data.get("is_charging", False) and current_amp <= 0:
            shown_amp = 0
        else:
            shown_amp = 0 if bev_blocked else status_amp
        if stop_display_state.get("active", False):
            real_state = False
            real_power_w = 0.0
            shown_amp = 0
            detail_state = {
                "state": "Stop gesendet",
                "state_level": "warning",
                "state_reason": "Harter Stop gesendet; Messwert läuft nach.",
                "min_power_w": detail_state.get("min_power_w", 0),
            }

        wb_detail = {
            "id": c_id,
            "amp": shown_amp,
            "state": detail_state.get("state", "Lade" if real_state else "Angesteckt"),
            "state_level": detail_state.get("state_level", "info"),
            "state_reason": detail_state.get("state_reason", ""),
            "min_power_w": detail_state.get("min_power_w", 0),
            "physical_budget_ready": bool(physical_detail.get("budget_ready", False)),
            "physical_chargeable": bool(physical_detail.get("can_start_or_hold", False)),
            "physical_phases": int(physical_detail.get("phases", 0) or 0),
            "physical_reason": physical_detail.get("reason", ""),
            "charge_contract": charge_contract,
            "charge_truth": str(charge_contract.get("truth", "not_charging") or "not_charging"),
            "charge_source": str(charge_contract.get("source", "") or ""),
            "charge_confidence": str(charge_contract.get("confidence", "") or ""),
            "charge_power_w": round(float(charge_contract.get("power_w", 0.0) or 0.0), 1),
            "charge_raw_power_w": round(float(charge_contract.get("raw_power_w", 0.0) or 0.0), 1),
            "phantom_power_w": round(float(charge_contract.get("phantom_power_w", 0.0) or 0.0), 1),
            "charge_energy_increasing": bool(charge_contract.get("energy_increasing", False)),
            "charge_energy_delta_wh": round(float(charge_contract.get("energy_delta_wh", 0.0) or 0.0), 3),
            "charge_energy_source": str(charge_contract.get("energy_source", "") or ""),
            "charge_end_contract": charge_end_contract,
            "charge_end_latched": bool(charge_end_contract.get("latched", bev_blocked)),
            "charge_end_action": str(charge_end_contract.get("action", "") or ""),
            "charge_end_reason": str(charge_end_contract.get("reason", "") or ""),
            "charge_end_exception": str(charge_end_contract.get("exception", "") or ""),
            "charge_end_start_blocked": bool(charge_end_contract.get("start_blocked", bev_blocked)),
            "ramp_contract": ramp_contract,
            "ramp_raw_target_amp": int(ramp_contract.get("raw_target_amp", 0) or 0),
            "ramp_applied_amp": int(ramp_contract.get("applied_amp", 0) or 0),
            "ramp_limited": bool(ramp_contract.get("limited", False)),
            "ramp_reason": str(ramp_contract.get("reason", "") or ""),
            "phase_contract": phase_contract,
            "phase_actual_phases": int(phase_contract.get("actual_phases", 0) or 0),
            "phase_actual_source": str(phase_contract.get("actual_source", "") or ""),
            "phase_effective_phases": int(phase_contract.get("effective_phases", 0) or 0),
            "phase_effective_source": str(phase_contract.get("effective_source", "") or ""),
            "phase_cable_phases": int(phase_contract.get("cable_phases", 0) or 0),
            "phase_vehicle_max_phases": int(phase_contract.get("vehicle_max_phases", 0) or 0),
            "phase_wallbox_phases": int(phase_contract.get("wallbox_phases", 0) or 0),
            "phase_can_switch": bool(phase_contract.get("can_switch_phases", False)),
            "power_w": round(real_power_w, 1),
            "plug": connected,
            "charging": real_state,
            "manager_stop_pending": bool(stop_display_state.get("active", False)),
            "manager_stop_reason": str(stop_display_state.get("reason", "") or ""),
            "manager_stop_age_s": round(float(stop_display_state.get("age_s", 0.0) or 0.0), 1),
            "manager_stop_remaining_s": round(float(stop_display_state.get("remaining_s", 0.0) or 0.0), 1),
            "bev_full_blocked": bev_blocked,
            "max_amp": _charger_max_amp(config, c_id, wb_global_max_amp),
            "priority_mode": int(priority_mode),
            "priority_active": bool(
                priority_mode in (1, 2)
                and c_id == priority_mode
                and connected
            ),
            "priority_waiting": bool(
                priority_mode in (1, 2)
                and c_id != priority_mode
                and priority_target_connected
            ),
            "manual_pause": manual_pause_active,
        }
        for detail_key in (
            "control_status", "control_label", "control_detail", "control_level",
            "openwb_primary_grid_phase_warning",
            "openwb_primary_grid_phase_warning_reason",
            "openwb_primary_grid_target_amp",
            "openwb_primary_grid_actual_power_w",
            "openwb_primary_grid_actual_phases",
            "openwb_primary_grid_expected_3p_w",
            "openwb_primary_grid_vehicle_phases",
            "openwb_primary_grid_connected_phases",
            "vehicle_under_acceptance_warning",
            "vehicle_under_acceptance_reason",
            "vehicle_under_acceptance_offered_amp",
            "vehicle_under_acceptance_target_phases",
            "vehicle_under_acceptance_actual_power_w",
            "vehicle_under_acceptance_expected_power_w",
            "vehicle_under_acceptance_accepted_ratio",
            "vehicle_under_acceptance_actual_phases",
            "vehicle_under_acceptance_charger_class",
        ):
            if detail_key in detail_state:
                wb_detail[detail_key] = detail_state[detail_key]
        if e3dc_native_contract.get("enabled", False):
            wb_detail["e3dc_native_contract"] = e3dc_native_contract
            wb_detail["e3dc_native_runtime_path"] = str(e3dc_native_contract.get("runtime_path", "") or "")
            wb_detail["e3dc_native_legacy_cpp_runtime_allowed"] = bool(
                e3dc_native_contract.get("legacy_cpp_runtime_allowed", False)
            )
            wb_detail["e3dc_native_fallback_role"] = str(e3dc_native_contract.get("fallback_role", "") or "")
            wb_detail["e3dc_native_session_guard_required"] = bool(
                e3dc_native_contract.get("session_guard_required", False)
            )
            wb_detail["e3dc_native_charge_verification_required"] = bool(
                e3dc_native_contract.get("charge_verification_required", False)
            )
        if phase_capability:
            wb_detail["can_switch_phases"] = bool(phase_capability.get("can_switch", False))
            wb_detail["phase_switch_capability"] = phase_capability.get("capability", "")
            wb_detail["phase_switch_source"] = phase_capability.get("source", "")
            wb_detail["api_surface"] = phase_capability.get("api_surface", "")
        decision_payload, driver_command = _wallbox_decision_payload_or_default(
            c_data,
            st,
            public_mode=public_mode,
            cap_amp=cap_amp,
            allowed_w=allowed_w,
            detected_phases=detected_phases,
            max_amp=_charger_max_amp(config, c_id, wb_global_max_amp),
            mode_label_text=mode_label(normalize_wb_mode(public_mode)),
            budget_timeout=budget_timeout,
        )
        wb_detail["decision_payload"] = decision_payload
        wb_detail["driver_command"] = driver_command
        _attach_wallbox_countdown_diagnostics(wb_detail, c_data)
        if isinstance(c_data.get("_command_guard_decision"), dict):
            wb_detail["command_guard"] = c_data.get("_command_guard_decision")
        if isinstance(c_data.get("_e3dc_edge_guard_decision"), dict):
            wb_detail["e3dc_edge_guard"] = c_data.get("_e3dc_edge_guard_decision")
        _copy_wallbox_status_diagnostics(wb_detail, st)
        if st:
            for key in (
                "driver_variant", "device_name", "firmware_version",
                "rscp_wb_index",
                "rscp_status", "rscp_error_active", "rscp_error_count",
                "rscp_last_error", "rscp_last_error_context",
                "rscp_last_error_ts", "rscp_last_ok_ts", "rscp_last_ok_context",
                "e3dc_native_contract", "e3dc_native_runtime_path",
                "e3dc_native_legacy_cpp_runtime_allowed", "e3dc_native_fallback_role",
                "e3dc_native_session_guard_required", "e3dc_native_charge_verification_required",
                "e3dc_session_state", "e3dc_session_label", "e3dc_session_level",
                "e3dc_session_reason", "e3dc_session_offered_amp",
                "e3dc_session_budget_ready", "e3dc_session_start_requested",
                "e3dc_session_start_verifying", "e3dc_session_stop_active",
                "e3dc_session_start_blocked",
                "e3dc_session_can_send_start_toggle", "e3dc_session_counts_as_real_charge",
                "openwb_pro_contract", "openwb_pro_runtime_path",
                "openwb_pro_session_guard_required", "openwb_pro_charge_verification_required",
                "openwb_pro_session_state", "openwb_pro_session_label", "openwb_pro_session_level",
                "openwb_pro_session_reason", "openwb_pro_session_offered_amp",
                "openwb_pro_session_budget_ready", "openwb_pro_session_start_requested",
                "openwb_pro_session_start_verifying", "openwb_pro_session_wakeup_pending",
                "openwb_pro_session_wakeup_remaining_s",
                "openwb_pro_session_start_hold_remaining_s",
                "openwb_pro_session_stop_remaining_s",
                "openwb_pro_session_phase_wait_remaining_s",
                "openwb_pro_session_start_hold_active",
                "openwb_pro_session_phase_wait_active", "openwb_pro_session_phase_wait_target",
                "openwb_pro_session_phase_wait_since_s",
                "openwb_pro_session_phase_wait_last_duration_s",
                "openwb_pro_session_phase_wait_last_result",
                "openwb_pro_session_phase_wait_last_target",
                "openwb_pro_session_phase_wait_samples",
                "openwb_pro_session_phase_wait_ema_s",
                "openwb_pro_session_phase_wait_max_s",
                "openwb_pro_session_stop_active",
                "openwb_pro_session_start_blocked",
                "openwb_pro_session_can_send_start_command", "openwb_pro_session_counts_as_real_charge",
                "openwb_pro_temporary_stop_contract",
                "openwb_pro_temporary_stop_active",
                "openwb_pro_temporary_stop_reason",
                "openwb_pro_temporary_stop_state_hint",
                "openwb_pro_temporary_stop_waiting",
                "openwb_pro_start_retry_guard_contract",
                "last_executed_command",
                "openwb_pro_vehicle_finished_contract",
                "openwb_pro_vehicle_finished_action",
                "openwb_pro_vehicle_finished_reason",
                "openwb_pro_vehicle_finished_allow_new_latch",
                "openwb_pro_vehicle_finished_blockers",
                "openwb_pro_vehicle_finished_had_confirmed_charge",
                "openwb_pro_vehicle_finished_time_since_start_s",
                "driver_status_valid", "driver_status_stale",
                "driver_status_degraded", "driver_status_age_s",
                "driver_status_reason", "driver_status_last_ok_ts",
                "driver_status_last_sample_ts", "driver_status_source",
                "driver_status_plausible", "driver_status_glitch",
                "driver_status_glitch_reason", "driver_status_last_good_ts",
                "mqtt_connected", "mqtt_reconnect_backoff_s",
                "openwb_secondary_contract", "openwb_secondary_runtime_path",
                "openwb_secondary_session_guard_required", "openwb_secondary_charge_verification_required",
                "openwb_secondary_session_state", "openwb_secondary_session_label", "openwb_secondary_session_level",
                "openwb_secondary_session_reason", "openwb_secondary_session_offered_amp",
                "openwb_secondary_session_current_set_amp", "openwb_secondary_session_cap_amp",
                "openwb_secondary_session_hardware_amp", "openwb_secondary_session_last_command_amp",
                "openwb_secondary_session_heartbeat_ok", "openwb_secondary_session_command_ok",
                "openwb_secondary_session_command_blocked", "openwb_secondary_session_api_surface",
                "openwb_secondary_session_secondary_active",
                "openwb_secondary_session_budget_ready", "openwb_secondary_session_start_requested",
                "openwb_secondary_session_start_verifying", "openwb_secondary_session_stop_active",
                "openwb_secondary_session_start_blocked",
                "openwb_secondary_session_can_send_start_command", "openwb_secondary_session_counts_as_real_charge",
                "goe_contract", "goe_runtime_path",
                "goe_session_guard_required", "goe_charge_verification_required",
                "goe_session_state", "goe_session_label", "goe_session_level",
                "goe_session_reason", "goe_session_offered_amp",
                "goe_session_current_set_amp", "goe_session_cap_amp", "goe_session_hardware_amp",
                "goe_session_frc", "goe_session_budget_ready", "goe_session_start_requested",
                "goe_session_start_verifying", "goe_session_stop_active",
                "goe_session_start_blocked",
                "goe_session_can_send_start_command", "goe_session_counts_as_real_charge",
                "charge_truth", "charge_is_charging", "charge_counts_as_real",
                "charge_confidence", "charge_source", "charge_power_w",
                "charge_raw_power_w", "phantom_power_w",
                "charge_energy_increasing", "charge_energy_delta_wh",
                "charge_energy_source",
                "charge_end_contract", "charge_end_latched", "charge_end_action",
                "charge_end_reason", "charge_end_exception", "charge_end_start_blocked",
                "ramp_contract", "ramp_raw_target_amp", "ramp_applied_amp",
                "ramp_limited", "ramp_reason",
                "enabled", "extern_alg_hex", "rfid_tag",
                "alg_seen", "alg_flags", "alg_charging", "alg_connected",
                "device_working",
                "chargepoint_name", "charge_template_name",
                "state_text", "fault_text", "fault_state",
                "manual_lock", "min_current",
                "pv_charging_min_current", "instant_charging_current",
                "instant_charging_limit", "instant_charging_soc",
                "car_soc", "car_soc_source", "car_soc_raw_ts", "car_soc_rule_confirmed",
                "car_name", "car_id", "vehicle_id",
                "car_capacity_kwh",
                "phase_power_l1_w", "phase_power_l2_w",
                "phase_power_l3_w", "phase_power_sum_w",
                "phase_apparent_l1_va", "phase_apparent_l2_va",
                "phase_apparent_l3_va", "apparent_power_va",
                "phase_current_l1_a", "phase_current_l2_a",
                "phase_current_l3_a",
                "apparent_power_kva", "power_factor",
                "phase_power_verified", "phases_in_use",
                "phases_target",
                "phase_contract",
                "phase_actual_phases", "phase_actual_source",
                "phase_effective_phases", "phase_effective_source",
                "phase_cable_phases", "phase_vehicle_max_phases",
                "phase_wallbox_phases", "phase_can_switch",
                "can_switch_phases", "phase_switch_capability",
                "phase_switch_source", "api_surface",
                "serial", "version", "v2g_ready",
                "evse_signaling", "offered_current_raw",
                "max_charge_power", "max_discharge_power",
                "temp_c", "cp_interrupt_isactive",
                "cp_interrupt_duration", "cp_interrupt_version",
                "control_status", "control_label", "control_detail",
                "control_level", "control_ts",
                "last_command_ok", "last_command_amp", "last_command_ts",
                "last_heartbeat_ok",
                "last_heartbeat_ts",
                "e3dc_live_power_fallback",
                "configured_role", "detected_role", "effective_role",
                "role_detection_source", "role_detection_detail",
                "role_mismatch", "command_failure_count",
                "command_failure_limit", "command_blocked",
                "command_blocked_until",
            ):
                if key in st and key not in wb_detail:
                    wb_detail[key] = st[key]
            if "session_kwh" in st:
                wb_detail["session_kwh"] = st["session_kwh"]
            if "total_kwh" in st:
                wb_detail["total_kwh"] = st["total_kwh"]
        details.append(wb_detail)
    return details

def _wallbox_global_max_amp(config, fallback_amp=16):
    global_amp = _amp_limit((config or {}).get("wbmaxladestrom", (config or {}).get("wb_max_amp", fallback_amp)), fallback_amp)
    limits = [global_amp]
    for cid in (1, 2):
        for key in (f"wb{cid}_max_amp", f"wb{cid}_maxladestrom"):
            raw = (config or {}).get(key)
            if raw is not None and str(raw).strip() != "":
                limits.append(_amp_limit(raw, global_amp))
                break
    return max(limits)

def _openwb_pro_start_verify_guard_override(c_data, command, now_ts=None, reason=""):
    """Allow narrow openWB Pro start/retry keepalives through the chatter guard."""
    box = c_data if isinstance(c_data, dict) else {}
    cmd = command if isinstance(command, dict) else {}
    if not openwb_pro_session.is_openwb_pro_charger(box.get("charger")):
        return False
    now_value = time.time() if now_ts is None else float(now_ts)
    session = box.get("_openwb_pro_session") if isinstance(box.get("_openwb_pro_session"), dict) else {}
    contract = openwb_pro_session.start_retry_guard_contract(
        cmd,
        box,
        session=session,
        now_ts=now_value,
        reason=reason,
    )
    box["_openwb_pro_start_retry_guard_contract"] = contract
    return bool(contract.get("allow_override", False))


def _wallbox_driver_observe_only_noop(c_data, command):
    """Return true when the driver gate will turn this command into a no-op."""
    box = c_data if isinstance(c_data, dict) else {}
    cmd = command if isinstance(command, dict) else {}
    charger = box.get("charger") or cmd.get("charger")
    if charger is None:
        return False
    ctx = getattr(charger, "_command_gate_context", None)
    if not isinstance(ctx, dict) or not bool(ctx.get("observe_only", False)):
        return False
    if command_gate.is_default_release_allowed(charger):
        return False
    return True


def _wallbox_command_guard_allows(c_data, command, c_id=None, reason=""):
    """Inline safety guard before a command reaches a real wallbox driver."""
    box = c_data if isinstance(c_data, dict) else {}
    cmd = command if isinstance(command, dict) else {}
    if bool(cmd.get("_control_guard_checked", False)):
        return True
    try:
        wb_id = int(c_id if c_id is not None else box.get("id", cmd.get("wb_id", 0)) or 0)
    except Exception:
        wb_id = 0
    if _wallbox_driver_observe_only_noop(box, cmd):
        decision = {
            "ts": round(time.time(), 3),
            "service": "control_command_guard",
            "domain": "wallbox",
            "actor": "wallbox:%d" % max(0, wb_id),
            "wb_id": int(wb_id),
            "action": "NOOP",
            "allowed": True,
            "decision": "observe_only_noop",
            "reason": str(reason or cmd.get("reason") or "openwb_primary_observe_only"),
            "block_reason": "",
            "target_reachable": bool(cmd.get("target_reachable", True)),
            "command": {
                key: cmd.get(key)
                for key in ("kind", "method", "amp", "force_state", "reason")
                if key in cmd
            },
            "candidate_event": None,
            "violations": [],
        }
        box["_command_guard_decision"] = decision
        cmd["_control_guard_checked"] = True
        cmd["command_guard"] = decision
        return True
    try:
        start_stop_gap_s = max(
            0,
            int(round(_cfg_float(box.get("_command_guard_start_stop_gap_s"), 180.0))),
        )
        phase_gap_s = max(
            0,
            int(round(_cfg_float(box.get("_command_guard_phase_gap_s"), 300.0))),
        )
        guard_cmd = cmd
        guard_reason = reason or str(cmd.get("reason", ""))
        if _openwb_pro_start_verify_guard_override(box, cmd, reason=guard_reason):
            guard_cmd = dict(cmd)
            guard_cmd["_guard_allow_restart_after_stop"] = True
            if "openwb_pro_start_verifying" not in guard_reason:
                guard_reason = (guard_reason + " openwb_pro_start_verifying").strip()
        guard_method = str(guard_cmd.get("method") or guard_cmd.get("kind") or "").strip()
        if (
            guard_method in ("set_current", "set_amp_and_state", "set_amp_sonnenmodus", "set_direct_current")
            and _cfg_float(guard_cmd.get("amp"), 0.0) > 0.0
            and bool(box.get("is_charging", False))
        ):
            if guard_cmd is cmd:
                guard_cmd = dict(cmd)
            guard_cmd["_guard_actor_active"] = True
        decision = control_command_guard.evaluate_wallbox_command(
            guard_cmd,
            wb_id=wb_id,
            reason=guard_reason,
            target_reachable=bool(guard_cmd.get("target_reachable", True)),
            start_stop_gap_s=start_stop_gap_s,
            phase_gap_s=phase_gap_s,
        )
    except Exception as exc:
        logger.debug("Wallbox Command-Guard uebersprungen (%s): %s", reason or cmd.get("reason", ""), exc)
        return True
    box["_command_guard_decision"] = decision
    cmd["_control_guard_checked"] = True
    cmd["command_guard"] = decision
    if decision.get("allowed", True):
        return True
    now_value = time.time()
    last_log = float(box.get("_last_command_guard_block_log_ts", 0.0) or 0.0)
    if now_value - last_log >= 30.0:
        box["_last_command_guard_block_log_ts"] = now_value
        if wb_id:
            logger.warning(
                "WB%d Command-Guard blockiert %s (%s)",
                wb_id,
                cmd.get("kind", cmd.get("method", "driver_command")),
                decision.get("block_reason", "command_chatter_guard"),
            )
        else:
            logger.warning(
                "Wallbox Command-Guard blockiert %s (%s)",
                cmd.get("kind", cmd.get("method", "driver_command")),
                decision.get("block_reason", "command_chatter_guard"),
            )
    return False


def _wallbox_command_guard_is_observe_only_noop(c_data, command):
    box = c_data if isinstance(c_data, dict) else {}
    cmd = command if isinstance(command, dict) else {}
    decision = cmd.get("command_guard")
    if not isinstance(decision, dict):
        decision = box.get("_command_guard_decision")
    return bool(isinstance(decision, dict) and decision.get("decision") == "observe_only_noop")


def _guard_e3dc_native_driver_edge(box, command, *, charger=None, c_id=None, reason=""):
    """Strip unsafe E3DC-native start/stop toggle impulses before driver IO."""

    data = box if isinstance(box, dict) else {}
    cmd = dict(command or {})
    active_charger = charger if charger is not None else data.get("charger")
    if not e3dc_session.is_e3dc_native_charger(active_charger):
        return cmd, {"action": "not_e3dc_native", "execute": True}

    contract_box = data
    if data.get("charger") is None and active_charger is not None:
        contract_box = dict(data)
        contract_box["charger"] = active_charger
    contract = _wallbox_e3dc_native_production_contract(contract_box, None, None)
    if contract.get("enabled", False):
        data["_e3dc_native_contract"] = contract

    session = data.get("_e3dc_session") if isinstance(data.get("_e3dc_session"), dict) else None
    guard = e3dc_session.guard_edge_command(
        cmd,
        session,
        hard_stop_allowed=_native_e3dc_hard_stop_allowed(reason),
        verified_active_charge=_e3dc_native_verified_active_charge(data),
        min_amp=cmd.get("min_amp", data.get("min_amp", 6)),
    )
    guarded_cmd = guard.get("command", cmd)
    decision = guard.get("decision", {}) if isinstance(guard, dict) else {}
    decision = dict(decision if isinstance(decision, dict) else {})
    decision["reason"] = str(reason or cmd.get("reason") or cmd.get("method") or "")
    decision["ts"] = time.time()
    data["_e3dc_edge_guard_decision"] = decision

    action = str(decision.get("action") or "unchanged")
    if action not in ("unchanged", "start_toggle_allowed", "stop_toggle_allowed"):
        last_log = float(data.get("_e3dc_edge_guard_last_log_ts", 0.0) or 0.0)
        now_value = float(decision.get("ts", time.time()) or time.time())
        if now_value - last_log >= 30.0:
            data["_e3dc_edge_guard_last_log_ts"] = now_value
            logger.info(
                "WB%s E3DC-Flankengate: %s (%s, Session=%s, force %s->%s, amp %s->%s)"
                % (
                    c_id if c_id is not None else "?",
                    action,
                    decision.get("reason", ""),
                    decision.get("session_state", ""),
                    decision.get("original_force_state"),
                    decision.get("force_state"),
                    decision.get("original_amp"),
                    decision.get("amp"),
                )
            )
    return guarded_cmd, decision


def _openwb_pro_driver_status(box, charger=None):
    active = charger if charger is not None else (box or {}).get("charger")
    state = getattr(active, "state", None)
    return state if isinstance(state, dict) else {}


def _openwb_pro_status_connected_idle(box, charger=None):
    st = _openwb_pro_driver_status(box, charger)
    connected = bool(st.get("plug_state") or st.get("car") == 2)
    real_power_w = max(
        _cfg_float(st.get("real_power_w"), 0.0),
        _cfg_float(st.get("phase_power_sum_w"), 0.0),
        _cfg_float(st.get("power_w"), 0.0),
    )
    charging = bool(st.get("charging") or st.get("charge_state") or real_power_w > 500.0)
    return connected, charging, real_power_w


def _openwb_pro_cp_payload_from_config(config=None, status=None):
    return openwb_pro_session.cp_interrupt_payload(config, status)


def _openwb_pro_phase_sequence_step(
    box,
    command,
    *,
    charger=None,
    c_id=None,
    reason="",
):
    """Execute the safe openWB-Pro phase sequence.

    Official connect.php offers independent `ampere`, `phasetarget` and
    `cp_interrupt` commands.  The manager-owned sequence is intentionally
    stricter than the device API: 0A -> wait -> phasetarget -> CP -> restart
    delay -> allow current.
    """

    data = box if isinstance(box, dict) else {}
    cmd = command if isinstance(command, dict) else {}
    active = charger if charger is not None else data.get("charger")
    if not openwb_pro_session.is_openwb_pro_charger(active):
        return None
    target = _valid_phase_count(cmd.get("phases", cmd.get("target_phases", 0)), 0)
    if target not in (1, 3):
        return False

    now_ts = time.time()
    cfg = data.get("_openwb_pro_config") if isinstance(data.get("_openwb_pro_config"), dict) else {}
    hold_s = openwb_pro_session.phase_wait_s(cfg)
    # The contactor/CP protection contract is a hard 480-second interruption.
    restart_delay_s = max(480.0, openwb_pro_session.phase_restart_delay_s(cfg))
    sequence_reason = str(reason or cmd.get("reason") or "phase_switch")
    sequencer = PhaseSwitchSequencer(data)

    for _ in range(2):
        st = _openwb_pro_driver_status(data, active)
        cp_payload = _openwb_pro_cp_payload_from_config(cfg, st)
        contract = sequencer.propose(
            target,
            now_ts=now_ts,
            current_set_amp=data.get("current_set_amp", 0),
            status=st,
            # 1:1 zum bisherigen Manager: Der Vertrag erhielt hier nur die
            # bereits berechneten Zeiten und das CP-Payload, nicht die Config.
            config=None,
            reason=sequence_reason,
            cp_payload=cp_payload,
            hold_s=hold_s,
            restart_delay_s=restart_delay_s,
            charger_max_amp=getattr(active, "max_amp", 32),
        )
        action = str(contract.get("action") or "invalid")
        if action == "send_zero":
            ok_zero = _execute_wallbox_driver_command(
                data,
                contract.get("command") or {},
                c_id=c_id,
                reason="openwb_pro_phase_zero",
            )
            if ok_zero:
                sequencer.acknowledge(
                    contract,
                    True,
                    config=cfg,
                    charger_max_amp=getattr(active, "max_amp", 32),
                )
                logger.info(
                    "WB%s openWB Pro Phasenwechsel vorbereitet: 0A gesetzt, %ds Beruhigungszeit vor phasetarget=%dp"
                    % (
                        c_id if c_id is not None else "?",
                        int(round(float(contract.get("zero_settle_s", 0.0) or 0.0))),
                        target,
                    )
                )
            return False
        if action == "wait_zero":
            sequencer.acknowledge(contract)
            return False
        if action == "send_phase":
            ok_phase = _execute_wallbox_driver_command(
                data,
                contract.get("command") or {},
                c_id=c_id,
                reason="openwb_pro_phase_target",
            )
            if not ok_phase:
                return False
            sequencer.acknowledge(
                contract,
                True,
                config=cfg,
                charger_max_amp=getattr(active, "max_amp", 32),
            )
            logger.info(
                "WB%s openWB Pro Phasenwechsel: phasetarget=%dp nach 0A-Beruhigung gesetzt"
                % (c_id if c_id is not None else "?", target)
            )
            # One normal hardware write per manager cycle. CP follows in the
            # next cycle; this also leaves a durable observation boundary.
            return False
        if action == "send_cp":
            ok_cp = _execute_wallbox_driver_command(
                data,
                contract.get("command") or {},
                c_id=c_id,
                reason="openwb_pro_phase_cp_interrupt",
            )
            if not ok_cp:
                return False
            sequencer.acknowledge(
                contract,
                True,
                config=cfg,
                charger_max_amp=getattr(active, "max_amp", 32),
            )
            logger.info(
                "WB%s openWB Pro Phasenwechsel: CP-Interrupt gesendet, Stromfreigabe ab %.0fs"
                % (c_id if c_id is not None else "?", restart_delay_s)
            )
            return False
        if action == "wait_restart":
            sequencer.acknowledge(contract)
            return False
        if action == "ready":
            sequencer.acknowledge(contract)
            return True
        return False

    return False


def _openwb_pro_start_wakeup_ready(box, command, *, charger=None, c_id=None, reason=""):
    data = box if isinstance(box, dict) else {}
    cmd = command if isinstance(command, dict) else {}
    active = charger if charger is not None else data.get("charger")
    if not openwb_pro_session.is_openwb_pro_charger(active):
        return True
    method = str(cmd.get("method") or cmd.get("kind") or "").strip()
    now_ts = time.time()
    cfg = data.get("_openwb_pro_config") if isinstance(data.get("_openwb_pro_config"), dict) else {}
    st = _openwb_pro_driver_status(data, active)
    cp_payload = _openwb_pro_cp_payload_from_config(cfg, st)
    contract = openwb_pro_session.start_wakeup_step_contract(
        method,
        cmd.get("amp", 0.0),
        data,
        st,
        cfg,
        now_ts=now_ts,
        cp_payload=cp_payload,
    )
    data["_openwb_pro_start_wakeup_contract"] = contract
    state_patch = contract.get("state_patch") if isinstance(contract.get("state_patch"), dict) else {}
    if state_patch:
        data.update(state_patch)
    command_patch = contract.get("command_patch") if isinstance(contract.get("command_patch"), dict) else {}
    if command_patch:
        cmd.update(command_patch)
    if str(contract.get("action") or "") != "send_cp_interrupt":
        return bool(contract.get("allow", True))

    ok_cp = _execute_wallbox_driver_command(
        data,
        contract.get("command") or {},
        c_id=c_id,
        reason="openwb_pro_start_wakeup",
    )
    if ok_cp:
        success_patch = contract.get("success_state_patch")
        if isinstance(success_patch, dict):
            data.update(success_patch)
        logger.info(
            "WB%s openWB Pro Wake-up: CP-Interrupt vor Stromfreigabe gesendet, %ds Einschaltverzoegerung"
            % (c_id if c_id is not None else "?", int(round(contract.get("delay_s", 0.0) or 0.0)))
        )
        return False
    return True


def _f040_int(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)


def _f040_bool(value, default=False):
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in ("1", "true", "yes", "on", "ja", "ein"):
        return True
    if text in ("0", "false", "no", "off", "nein", "aus", ""):
        return False
    return bool(default)


def _heatpump_transition_snapshot(box):
    """Separate configured intent from fresh physical heat-pump evidence."""

    data = box if isinstance(box, dict) else {}
    injected = data.get("_f040_heatpump_snapshot")
    if isinstance(injected, dict):
        snapshot = dict(injected)
    else:
        snapshot = {}
        try:
            if os.path.exists(ENERGY_DECISION_LATEST_FILE):
                snapshot = read_json_cached(ENERGY_DECISION_LATEST_FILE)
        except Exception:
            snapshot = {}
    heat = snapshot.get("heatpump") if isinstance(snapshot.get("heatpump"), dict) else {}

    injected_config = data.get("_f040_config_snapshot")
    if isinstance(injected_config, dict):
        config_snapshot = dict(injected_config)
    else:
        config_snapshot = {}
        for config_path in (CONFIG_FILE, V4_CONFIG_FILE):
            try:
                if os.path.exists(config_path):
                    loaded = read_json_cached(config_path)
                    if isinstance(loaded, dict):
                        config_snapshot.update(loaded)
            except Exception:
                continue
    try:
        wp_type = int(float(config_snapshot.get("wp_type", -1)))
    except (TypeError, ValueError):
        wp_type = -1
    has_shelly = any(
        str(config_snapshot.get(key, "") or "").strip() not in ("", "0.0.0.0")
        for key in ("shelly_sg_ip", "shelly_pause_ip")
    )
    configured_intent = bool(
        (_f040_bool(config_snapshot.get("luxtronik", False), False) and wp_type >= 0)
        or has_shelly
    )
    configured = bool(heat.get("configured", False) or configured_intent)
    if not configured:
        return {
            "configured": False,
            "fresh": True,
            "observation_valid": True,
            "running": False,
            "commitment_w": 0,
        }

    now_ts = float(data.get("_f040_now_ts", time.time()) or time.time())
    try:
        age_s = max(0.0, now_ts - float(snapshot.get("ts", 0.0) or 0.0))
    except (TypeError, ValueError):
        age_s = 9999.0
    return {
        "configured": True,
        "fresh": bool(snapshot and age_s <= 30.0),
        "age_s": age_s,
        "observation_valid": bool(heat.get("compressor_observation_valid", False)),
        "running": bool(heat.get("compressor_running", False)),
        "commitment_w": max(0, _f040_int(heat.get("confirmed_commitment_w", 0), 0)),
        "commitment_source": str(heat.get("confirmed_commitment_source") or "none"),
    }


def _wallbox_transition_budget_snapshot(box):
    data = box if isinstance(box, dict) else {}
    injected = data.get("_f040_budget_snapshot")
    if isinstance(injected, dict):
        budget = dict(injected)
    else:
        budget = {}
        path = os.path.join(RAMDISK_DIR, "wb_pv_budget.json")
        try:
            if os.path.exists(path):
                budget = read_json_cached(path)
        except Exception:
            budget = {}
    now_ts = float(data.get("_f040_now_ts", time.time()) or time.time())
    try:
        age_s = max(0.0, now_ts - float(budget.get("ts", 0.0) or 0.0))
    except (TypeError, ValueError):
        age_s = 9999.0
    available_w = budget.get("budget_w")
    if available_w is None:
        score = budget.get("energy_score") if isinstance(budget.get("energy_score"), dict) else {}
        available_w = score.get("free_for_limbs_w", 0)
    return {
        "fresh": bool(budget and age_s < 15.0 and not budget.get("live_sample_invalid", False)),
        "available_w": max(0, _f040_int(available_w, 0)),
        "age_s": age_s,
    }


def _wallbox_command_starts_or_switches(box, command, method, charger):
    status = box.get("last_valid") if isinstance(box.get("last_valid"), dict) else {}
    if not status and charger is not None:
        status = _openwb_pro_driver_status(box, charger)
    if method == "set_phases":
        target = _f040_int(command.get("phases", command.get("target_phases", 0)), 0)
        current = wallbox_phase_transition_guard.status_phase_count(status)
        return bool(target in (1, 3) and target != current), target, status
    if method in ("set_amp_and_state", "set_current", "set_direct_current"):
        try:
            amp = float(command.get("amp", 0.0) or 0.0)
        except (TypeError, ValueError):
            amp = 0.0
        running = bool(
            status.get("charging", False)
            or wallbox_phase_transition_guard.status_power_w(status) > 500.0
            or (box.get("is_charging", False) and float(box.get("current_set_amp", 0) or 0) >= 6.0)
        )
        phases = wallbox_phase_transition_guard.status_phase_count(status)
        return bool(amp >= 6.0 and not running), phases if phases in (1, 3) else 1, status
    return False, 0, status


def _wallbox_heatpump_transition_grant(box, command, method, charger, c_id=None):
    """Fail closed before a new WB transition can consume running-WP power."""

    needs_grant, target_phases, status = _wallbox_command_starts_or_switches(box, command, method, charger)
    if not needs_grant:
        return True, {"allowed": True, "reason": "not_a_new_transition"}
    heat = _heatpump_transition_snapshot(box)
    if not heat.get("configured", False):
        return True, {"allowed": True, "reason": "heatpump_not_configured"}
    if not heat.get("fresh", False) or not heat.get("observation_valid", False):
        return False, {"allowed": False, "reason": "heatpump_state_stale_or_unknown", "heatpump": heat}
    if heat.get("running", False) and int(heat.get("commitment_w", 0) or 0) <= 0:
        return False, {"allowed": False, "reason": "running_heatpump_commitment_unknown", "heatpump": heat}

    budget = _wallbox_transition_budget_snapshot(box)
    if not budget.get("fresh", False):
        return False, {"allowed": False, "reason": "wallbox_budget_stale_or_unknown", "heatpump": heat, "budget": budget}
    if box.get("_f040_connection_limit_ok") is False:
        return False, {"allowed": False, "reason": "connection_limit", "heatpump": heat, "budget": budget}

    restart_amp_value = command.get("amp", command.get("restart_amp"))
    if restart_amp_value is None and method == "set_phases":
        restart_amp_value = box.get("_f040_phase_restart_amp")
    if restart_amp_value is None and method == "set_phases":
        restart_amp_value = box.get("current_set_amp", status.get("amp"))
    try:
        restart_amp = float(restart_amp_value or 6.0)
    except (TypeError, ValueError):
        restart_amp = 6.0
    house_cap = _f040_int(box.get("_f040_house_fuse_cap_amp", 0), 0)
    if house_cap > 0 and restart_amp > house_cap:
        return False, {"allowed": False, "reason": "connection_limit", "heatpump": heat, "budget": budget}

    request = wallbox_phase_transition_guard.build_request(
        wb_id=max(0, _f040_int(c_id, box.get("id", 0))),
        from_phases=wallbox_phase_transition_guard.status_phase_count(status),
        target_phases=target_phases,
        restart_amp=max(6.0, restart_amp),
        current_step_amp=float(box.get("_f040_current_step_amp", 1.0) or 1.0),
        observed_before_w=wallbox_phase_transition_guard.status_power_w(status),
        max_power_w=max(0, _f040_int(box.get("_f040_max_power_w", 0), 0)),
    )
    commitment_w = int(heat.get("commitment_w", 0) or 0)
    arbitration = wallbox_phase_transition_guard.arbitrate_grants(
        [request],
        available_w=int(budget.get("available_w", 0) or 0) + commitment_w,
        heatpump_running=bool(heat.get("running", False)),
        heatpump_running_commitment_w=commitment_w,
    )
    grant = arbitration.get("grants", [{}])[0]
    allowed = bool(
        int(grant.get("granted_w", 0) or 0) >= max(1, int(request.get("requested_w", 0) or 0))
        and str(grant.get("grant_state") or "") == "granted"
    )
    return allowed, {
        "allowed": allowed,
        "reason": "granted" if allowed else str(grant.get("blocker") or "insufficient_headroom"),
        "heatpump": heat,
        "budget": budget,
        "request": request,
        "grant": grant,
    }


def _execute_wallbox_driver_command(c_data, command, c_id=None, reason=""):
    """Single edge for wallbox driver writes.

    The caller owns the rule decision. This helper only dispatches the already
    chosen driver method with the already chosen parameters.
    """
    box = c_data if isinstance(c_data, dict) else {}
    cmd = command if isinstance(command, dict) else {}
    charger = box.get("charger")
    if charger is None:
        charger = cmd.get("charger")
    if charger is None:
        return False
    method = str(cmd.get("method") or cmd.get("kind") or "").strip()
    kind = str(cmd.get("kind") or method or "driver_command").strip()
    command_reason = str(cmd.get("reason", reason or kind) or kind)

    cycle_token = box.get("_hardware_output_cycle_token")
    if (
        cycle_token is not None
        and box.get("_hardware_output_last_cycle_token") == cycle_token
        and int(box.get("_hardware_output_count", 0) or 0) >= 1
        and method != "emergency_stop"
    ):
        logger.error(
            "WB%s doppelter Hardwareausgang im selben Zyklus blockiert (%s)",
            c_id if c_id is not None else box.get("id", "?"),
            method or kind,
        )
        box["_duplicate_hardware_output_blocked"] = {
            "cycle_token": cycle_token,
            "method": method,
            "reason": command_reason,
        }
        return False

    if method not in ("stop", "emergency_stop", "release_to_default"):
        grant_allowed, grant_evidence = _wallbox_heatpump_transition_grant(
            box, cmd, method, charger, c_id=c_id
        )
        box["_f040_last_transition_grant"] = grant_evidence
        if not grant_allowed:
            logger.warning(
                "WB%s Start/Phasenwechsel wartet: %s",
                c_id if c_id is not None else box.get("id", "?"),
                grant_evidence.get("reason", "heatpump_priority"),
            )
            return False

    cmd, edge_decision = _guard_e3dc_native_driver_edge(
        box,
        cmd,
        charger=charger,
        c_id=c_id,
        reason=command_reason,
    )
    if not edge_decision.get("execute", True):
        return False

    def _cmd_int(name, default=0):
        try:
            return int(round(float(cmd.get(name, default) or 0)))
        except Exception:
            return int(default)

    def _cmd_amp(name="amp", default=0.0):
        try:
            return float(cmd.get(name, default) or 0.0)
        except Exception:
            return float(default)

    def _remember_executed_command():
        if not isinstance(c_data, dict):
            return
        try:
            amp_value = (
                cmd.get("amp", 0.0)
                if method == "set_amp_and_state"
                else (cmd.get("max_amp") or cmd.get("amp") or 0.0)
            )
            amp = float(amp_value or 0.0)
        except Exception:
            amp = 0.0
        try:
            target_phases = int(cmd.get("target_phases", cmd.get("phases", 0)) or 0)
        except Exception:
            target_phases = 0
        c_data["_last_executed_command"] = {
            "method": method,
            "amp": amp,
            "target_phases": target_phases,
            "reason": command_reason,
            "ts": time.time(),
        }

    def _return_executed(result):
        ok = bool(result)
        if ok:
            cycle_token = box.get("_hardware_output_cycle_token")
            if cycle_token is not None:
                if box.get("_hardware_output_last_cycle_token") != cycle_token:
                    box["_hardware_output_last_cycle_token"] = cycle_token
                    box["_hardware_output_count"] = 0
                box["_hardware_output_count"] = int(box.get("_hardware_output_count", 0) or 0) + 1
            _remember_executed_command()
        return ok

    openwb_pro_internal = bool(cmd.get("_openwb_pro_sequence_internal", False))
    if openwb_pro_session.is_openwb_pro_charger(charger) and not openwb_pro_internal:
        if method in ("trigger_cp_interrupt", "cp_interrupt"):
            now_guard_ts = time.time()
            status_now = _openwb_pro_driver_status(box, charger)
            status_power_w = max(
                _cfg_float(status_now.get("real_power_w"), 0.0),
                _cfg_float(status_now.get("phase_power_sum_w"), 0.0),
                _cfg_float(status_now.get("power_w"), 0.0),
            )
            if _openwb_pro_phase_wait_active(
                box,
                status_now,
                now_guard_ts,
                stable_hw_power_w=status_power_w,
            ):
                return False
        if method in ("set_amp_and_state", "set_current", "set_direct_current") and _cmd_amp("amp") >= 6.0:
            wakeup_allowed_after = float(box.get("_openwb_pro_start_wakeup_allowed_after", 0.0) or 0.0)
            if wakeup_allowed_after > time.time():
                status_now = _openwb_pro_driver_status(box, charger)
                status_power_w = max(
                    _cfg_float(status_now.get("real_power_w"), 0.0),
                    _cfg_float(status_now.get("phase_power_sum_w"), 0.0),
                    _cfg_float(status_now.get("power_w"), 0.0),
                )
                if status_power_w <= 500.0:
                    return False
            sequence = box.get("_openwb_pro_phase_sequence")
            sequence_stage = str(sequence.get("stage") or "") if isinstance(sequence, dict) else ""
            if isinstance(sequence, dict) and sequence and sequence_stage not in ("", "ready"):
                return False
        if method == "set_phases":
            return bool(_openwb_pro_phase_sequence_step(
                box,
                cmd,
                charger=charger,
                c_id=c_id,
                reason=command_reason,
            ))
        if method in ("set_amp_and_state", "set_current", "set_direct_current") and _cmd_amp("amp") >= 6.0:
            if not _openwb_pro_start_wakeup_ready(
                box,
                cmd,
                charger=charger,
                c_id=c_id,
                reason=command_reason,
            ):
                return False

    if method == "stop":
        if not _wallbox_command_guard_allows(box, cmd, c_id=c_id, reason=command_reason):
            return False
        if _wallbox_command_guard_is_observe_only_noop(box, cmd):
            return True
        return _send_wallbox_stop_command(box, c_id=c_id, reason=command_reason, _guard_checked=True)

    if not _wallbox_command_guard_allows(box, cmd, c_id=c_id, reason=command_reason):
        return False
    if _wallbox_command_guard_is_observe_only_noop(box, cmd):
        return True

    try:
        if method == "take_control":
            return _return_executed(charger.take_control())
        if method == "set_amp_and_state":
            return _return_executed(charger.set_amp_and_state(_cmd_amp("amp"), force_state=cmd.get("force_state")))
        if method == "set_amp_sonnenmodus":
            return _return_executed(charger.set_amp_sonnenmodus(_cmd_amp("amp"), force_state=cmd.get("force_state")))
        if method == "set_direct_current":
            return _return_executed(charger.set_direct_current(_cmd_amp("amp")))
        if method == "set_pv_mode":
            return _return_executed(charger.set_pv_mode())
        if method == "set_phases":
            target_phases = _cmd_int("phases", _cmd_int("target_phases", 0))
            status_before = box.get("last_valid") if isinstance(box.get("last_valid"), dict) else {}
            if not status_before:
                status_before = _openwb_pro_driver_status(box, charger)
            ok = _return_executed(charger.set_phases(target_phases))
            if ok and not openwb_pro_internal:
                begin_phase_transition_reservation(
                    box,
                    target_phases,
                    status=status_before,
                    now_ts=time.time(),
                    charger_max_amp=getattr(charger, "max_amp", box.get("max_amp", 32)),
                    source="%s_phase_command" % charger.__class__.__name__,
                    reason=command_reason,
                )
            return ok
        if method == "emergency_stop":
            return _return_executed(charger.emergency_stop())
        if method == "trigger_cp_interrupt":
            duration = cmd.get("duration")
            version = cmd.get("version")
            if duration is None and version is None:
                return _return_executed(charger.trigger_cp_interrupt())
            try:
                return _return_executed(charger.trigger_cp_interrupt(duration=duration, version=version))
            except TypeError:
                return _return_executed(charger.trigger_cp_interrupt())
        if method == "release_to_e3dc":
            return _return_executed(charger.release_to_e3dc(max_amp=_amp_limit(cmd.get("max_amp", cmd.get("amp", 6)), 32)))
        if method == "release_to_default":
            return _return_executed(charger.release_to_default(max_amp=_amp_limit(cmd.get("max_amp", cmd.get("amp", 32)), 32)))
        if method == "set_current":
            amp = _cmd_amp("amp")
            if hasattr(charger, "set_amp_sonnenmodus"):
                return _return_executed(charger.set_amp_sonnenmodus(amp, force_state=cmd.get("force_state")))
            if hasattr(charger, "set_direct_current"):
                return _return_executed(charger.set_direct_current(amp))
            if hasattr(charger, "set_amp_and_state"):
                return _return_executed(charger.set_amp_and_state(amp, force_state=cmd.get("force_state")))
            if hasattr(charger, "release_to_e3dc"):
                return _return_executed(charger.release_to_e3dc(max_amp=_amp_limit(amp, 32)))
    except Exception as exc:
        if c_id is not None:
            logger.warning("WB%d Treiberbefehl fehlgeschlagen (%s/%s): %s" % (c_id, method or kind, command_reason, exc))
        else:
            logger.warning("Wallbox-Treiberbefehl fehlgeschlagen (%s/%s): %s" % (method or kind, command_reason, exc))
        return False
    return False

def _release_wallbox_to_default(charger, max_amp, c_data=None):
    """Gibt eine Wallbox im Aus-Modus an ihren lokalen Grundzustand zurueck."""
    if charger is None:
        return False
    max_amp = _amp_limit(max_amp, 32)
    try:
        command_box = c_data if isinstance(c_data, dict) else {"charger": charger}
        if hasattr(charger, "release_to_default"):
            return _execute_wallbox_driver_command(
                command_box,
                {"method": "release_to_default", "max_amp": max_amp, "reason": "mode0_default_release"},
            )
        if hasattr(charger, "release_to_e3dc"):
            return _execute_wallbox_driver_command(
                command_box,
                {"method": "release_to_e3dc", "max_amp": max_amp, "reason": "mode0_default_release"},
            )
        if hasattr(charger, "set_pv_mode"):
            ok_current = True
            if hasattr(charger, "set_amp_and_state"):
                ok_current = _execute_wallbox_driver_command(
                    command_box,
                    {"method": "set_amp_and_state", "amp": max_amp, "force_state": None, "reason": "mode0_default_release"},
                )
            ok_pv = _execute_wallbox_driver_command(
                command_box,
                {"method": "set_pv_mode", "reason": "mode0_default_release"},
            )
            return ok_current or ok_pv
    except Exception as exc:
        logger.debug("Wallbox Default-Freigabe fehlgeschlagen: %s", exc)
    return False

def _release_wallbox_to_default_once(c_data, max_amp, reason="mode0"):
    """Mode 0/Aus darf nur einmal freigeben und danach schweigen."""
    if not isinstance(c_data, dict):
        return False
    now_ts = time.time()
    max_amp = _amp_limit(max_amp, 32)
    same_default = (
        bool(c_data.get("_mode0_default_release_sent", False))
        and int(c_data.get("_mode0_default_release_amp", 0) or 0) == int(max_amp)
        and str(c_data.get("_mode0_default_release_reason", "")) == str(reason)
    )
    if same_default:
        return False
    with command_gate.default_release_scope(c_data.get("charger"), reason=reason):
        ok = _release_wallbox_to_default(c_data.get("charger"), max_amp, c_data=c_data)
    c_data["_mode0_default_release_sent"] = True
    c_data["_mode0_default_release_ok"] = bool(ok)
    c_data["_mode0_default_release_amp"] = int(max_amp)
    c_data["_mode0_default_release_reason"] = str(reason)
    c_data["_mode0_default_release_ts"] = now_ts
    return ok

def _load_mode0_default_release_requests():
    try:
        if not os.path.exists(WB_DEFAULT_RELEASE_REQUEST_FILE):
            return {}
        with open(WB_DEFAULT_RELEASE_REQUEST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_mode0_default_release_requests(data):
    try:
        tmp = WB_DEFAULT_RELEASE_REQUEST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data if isinstance(data, dict) else {}, f)
        os.replace(tmp, WB_DEFAULT_RELEASE_REQUEST_FILE)
    except Exception:
        pass

def _consume_mode0_default_release_request(charger_id, max_age_s=86400.0):
    """Consumes the explicit WebUI switch-to-off request for one charger.

    Aus/NGNA must not send defaults after restarts, connection loss or a normal
    mode-0 loop. The release is allowed only when Wallbox.php recorded a fresh
    user action.
    """
    data = _load_mode0_default_release_requests()
    key = str(int(charger_id or 1))
    request = data.pop(key, None)
    changed = request is not None
    now_ts = time.time()
    for stale_key, stale_request in list(data.items()):
        try:
            if now_ts - float((stale_request or {}).get("ts", 0.0) or 0.0) > max_age_s:
                data.pop(stale_key, None)
                changed = True
        except Exception:
            data.pop(stale_key, None)
            changed = True
    if changed:
        _save_mode0_default_release_requests(data)
    if not isinstance(request, dict):
        return None
    try:
        if now_ts - float(request.get("ts", 0.0) or 0.0) > max_age_s:
            return None
    except Exception:
        return None
    return request

def _load_wallbox_user_mode_requests():
    try:
        if not os.path.exists(WB_USER_MODE_REQUEST_FILE):
            return {}
        with open(WB_USER_MODE_REQUEST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_wallbox_user_mode_requests(data):
    try:
        tmp = WB_USER_MODE_REQUEST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data if isinstance(data, dict) else {}, f)
        os.replace(tmp, WB_USER_MODE_REQUEST_FILE)
    except Exception:
        pass

def _consume_wallbox_user_mode_request(charger_id, target_mode=None, max_age_s=86400.0):
    """Consumes an explicit Wallbox.php mode-switch request for one charger."""
    data = _load_wallbox_user_mode_requests()
    key = str(int(charger_id or 1))
    now_ts = time.time()
    changed = False
    for stale_key, stale_request in list(data.items()):
        try:
            if now_ts - float((stale_request or {}).get("ts", 0.0) or 0.0) > max_age_s:
                data.pop(stale_key, None)
                changed = True
        except Exception:
            data.pop(stale_key, None)
            changed = True
    request = data.get(key)
    if isinstance(request, dict):
        try:
            if now_ts - float(request.get("ts", 0.0) or 0.0) > max_age_s:
                data.pop(key, None)
                changed = True
                request = None
        except Exception:
            data.pop(key, None)
            changed = True
            request = None
    if isinstance(request, dict) and target_mode is not None:
        try:
            if normalize_wb_mode(request.get("target_mode")) != normalize_wb_mode(target_mode):
                request = None
        except Exception:
            request = None
    if isinstance(request, dict):
        data.pop(key, None)
        changed = True
    if changed:
        _save_wallbox_user_mode_requests(data)
    return request if isinstance(request, dict) else None

def _wallbox_curve_switch_quiet_until(request, *, now_ts=None, hold_s=60.0):
    """Return the quiet-until timestamp after an explicit switch to PV curve."""
    if not isinstance(request, dict):
        return 0.0
    try:
        request_ts = float(request.get("ts", 0.0) or 0.0)
    except Exception:
        return 0.0
    if request_ts <= 0.0:
        return 0.0
    now = time.time() if now_ts is None else float(now_ts or 0.0)
    hold = max(0.0, float(hold_s or 0.0))
    quiet_until = request_ts + hold
    return quiet_until if quiet_until > now else 0.0

def _apply_pv_curve_mode_switch_quiet_request(
    c_data,
    charger_id,
    public_mode,
    quiet_supported,
    *,
    now_ts=None,
    hold_s=60.0,
):
    """Consume an explicit user switch to PV curve and debounce the next start.

    openWB Pro has no separate "set PV mode" API command in our HAL path. It
    still needs the same UI-visible quiet window after returning from wbminSoC
    support, otherwise the request file stays behind and the next cycle can
    immediately re-enter start/phase logic.
    """
    if not quiet_supported or normalize_wb_mode(public_mode) != MODE_CURVE:
        return None
    request = _consume_wallbox_user_mode_request(charger_id, MODE_CURVE)
    quiet_until = _wallbox_curve_switch_quiet_until(
        request,
        now_ts=now_ts,
        hold_s=hold_s,
    )
    if quiet_until > float(now_ts if now_ts is not None else time.time()):
        c_data["_pv_curve_mode_switch_quiet_until"] = max(
            float(c_data.get("_pv_curve_mode_switch_quiet_until", 0.0) or 0.0),
            quiet_until,
        )
    return request

def _reset_mode0_default_release(c_data):
    if isinstance(c_data, dict):
        c_data["_mode0_default_release_sent"] = False
        c_data["_mode0_default_release_ok"] = False

def _update_command_gate_context(chargers, wb_charge_mode=None, wb_locked=None, native_enabled=True):
    """Keep the driver-level command gate aligned with the current public mode."""
    wb_charge_mode = wb_charge_mode if isinstance(wb_charge_mode, dict) else {}
    wb_locked = wb_locked if isinstance(wb_locked, dict) else {}
    for c_data in chargers or []:
        try:
            c_id = int(c_data.get("id", 0) or 0)
            charger = c_data.get("charger")
            public_mode = normalize_wb_mode(wb_charge_mode.get(c_id, MODE_OFF))
            command_gate.configure_charger(
                charger,
                wb_id=c_id,
                mode=public_mode,
                native_enabled=bool(native_enabled),
                locked=bool(wb_locked.get(c_id, False)),
            )
        except Exception:
            pass

def _set_command_gate_observe_only(chargers, observe_only=False):
    for c_data in chargers or []:
        try:
            charger = c_data.get("charger")
            if charger is None:
                continue
            ctx = getattr(charger, "_command_gate_context", {}) or {}
            if not isinstance(ctx, dict):
                ctx = {}
            primary_openwb = bool(
                charger.__class__.__name__ == "OpenWBCharger"
                and getattr(charger, "primary_mode_enabled", False)
            )
            ctx["observe_only"] = bool(observe_only and primary_openwb)
            if ctx["observe_only"]:
                ctx["reason"] = "openwb_primary_observe_only"
            elif ctx.get("reason") == "openwb_primary_observe_only":
                ctx.pop("reason", None)
            charger._command_gate_context = ctx
        except Exception:
            pass

_NATIVE_E3DC_HARD_STOP_REASON_PREFIXES = (
    "battery_departure",
    "bev_full_blocked",
    "emergency",
    "grid_window_end",
    "manual",
    "mode0",
    "no_vehicle_connected",
    "native_battery_drain_zero_budget",
    "planned",
    "predump_exit",
    "priority",
    "schedule",
    "user",
    "vehicle_charge_ended",
    "wbminsoc_floor_battery_stop",
    "wbminsoc_floor_cloud_stop",
    "wbminsoc_floor_grid_stop",
    "zero_budget_stop",
)


def _native_e3dc_hard_stop_allowed(reason):
    """Return True only for native E3DC stops that may toggle force_state=1."""
    reason_text = str(reason or "").strip().lower()
    if not reason_text:
        return False
    return any(reason_text.startswith(prefix) for prefix in _NATIVE_E3DC_HARD_STOP_REASON_PREFIXES)


def _e3dc_native_verified_active_charge(c_data):
    """Return true when independent wallbox telemetry proves an active charge."""
    if not isinstance(c_data, dict):
        return False
    contract = c_data.get("_charge_contract")
    if isinstance(contract, dict):
        if bool(contract.get("counts_as_real_charge", False)):
            return True
        if bool(contract.get("is_charging", False)) and _cfg_float(contract.get("power_w"), 0.0) > 500.0:
            return True
    status = c_data.get("last_valid")
    if isinstance(status, dict):
        if bool(status.get("charge_counts_as_real", False)):
            return True
        if _wb_status_real_charging(status) or _wb_status_real_power(status) > 500.0:
            return True
    return False


def _mark_manager_stop_display_pending(c_data, reason="", *, now_ts=None, hold_s=30.0):
    if not isinstance(c_data, dict):
        return
    now_value = time.time() if now_ts is None else float(now_ts)
    hold_value = max(5.0, min(45.0, _cfg_float(hold_s, 30.0)))
    c_data["_manager_stop_display_since_ts"] = now_value
    c_data["_manager_stop_display_until_ts"] = now_value + hold_value
    c_data["_manager_stop_display_reason"] = str(reason or "stop")


def _manager_stop_display_state(c_data, *, now_ts=None):
    if not isinstance(c_data, dict):
        return {"active": False}
    now_value = time.time() if now_ts is None else float(now_ts)
    until_ts = _cfg_float(c_data.get("_manager_stop_display_until_ts"), 0.0)
    if until_ts <= now_value:
        return {"active": False}
    since_ts = _cfg_float(c_data.get("_manager_stop_display_since_ts"), 0.0)
    if since_ts <= 0.0:
        since_ts = _cfg_float(c_data.get("_last_manager_stop_request_ts"), now_value)
    return {
        "active": True,
        "since_ts": since_ts,
        "until_ts": until_ts,
        "age_s": max(0.0, now_value - since_ts),
        "remaining_s": max(0.0, until_ts - now_value),
        "reason": str(c_data.get("_manager_stop_display_reason") or c_data.get("_last_manager_zero_anchor_reason") or "stop"),
    }


def _wallbox_countdown_remaining_s(until_ts, now_ts):
    try:
        until_value = float(until_ts or 0.0)
        now_value = float(now_ts)
    except (TypeError, ValueError):
        return 0
    if until_value <= now_value:
        return 0
    return max(0, int(round(until_value - now_value)))


def _attach_wallbox_countdown_diagnostics(wb_detail, c_data, *, now_ts=None):
    """Ergänzt Diagnose-Restzeiten aus aktiven Schutz- und Haltefenstern."""
    if not isinstance(wb_detail, dict) or not isinstance(c_data, dict):
        return
    now_value = time.time() if now_ts is None else float(now_ts)
    countdown_sources = {
        "openwb_pro_session_phase_wait_remaining_s": "_openwb_pro_phase_wait_until",
        "openwb_pro_session_stop_remaining_s": "_manager_stop_display_until_ts",
        "openwb_pro_session_start_hold_remaining_s": "_phase_1p_start_hold_until",
        "openwb_pro_session_wakeup_remaining_s": "_openwb_pro_start_wakeup_allowed_after",
    }
    for detail_key, source_key in countdown_sources.items():
        wb_detail[detail_key] = _wallbox_countdown_remaining_s(c_data.get(source_key, 0.0), now_value)
    last_executed_command = c_data.get("_last_executed_command")
    wb_detail["last_executed_command"] = (
        last_executed_command
        if isinstance(last_executed_command, dict)
        else None
    )


def _apply_manager_stop_display_to_status(status, charge_contract, stop_state):
    if not isinstance(stop_state, dict) or not stop_state.get("active", False):
        return charge_contract
    pending_contract = dict(charge_contract or {})
    raw_power_w = max(
        _cfg_float(pending_contract.get("raw_power_w"), 0.0),
        _cfg_float(pending_contract.get("power_w"), 0.0),
        _cfg_float(pending_contract.get("phantom_power_w"), 0.0),
    )
    pending_contract.update({
        "truth": "stop_pending",
        "is_charging": False,
        "counts_as_real_charge": False,
        "confidence": "manager_stop_pending",
        "source": "manager_stop_pending",
        "power_w": 0.0,
        "phantom_power_w": raw_power_w,
        "manager_stop_pending": True,
        "manager_stop_reason": stop_state.get("reason", "stop"),
        "manager_stop_age_s": round(_cfg_float(stop_state.get("age_s"), 0.0), 1),
    })
    if status is not None:
        status["charging"] = False
        status["charge_state"] = False
        status["real_power_w"] = 0.0
        status["power_w"] = 0.0
        status["phase_power_l1_w"] = 0.0
        status["phase_power_l2_w"] = 0.0
        status["phase_power_l3_w"] = 0.0
        status["phase_power_sum_w"] = 0.0
        status["phase_power_verified"] = False
        status["manager_stop_pending"] = True
        status["manager_stop_reason"] = pending_contract["manager_stop_reason"]
        status["manager_stop_age_s"] = pending_contract["manager_stop_age_s"]
        _apply_charge_contract_to_status(status, pending_contract)
    return pending_contract


def _send_wallbox_stop_command(c_data, c_id=None, reason="", _guard_checked=False):
    """Send the driver-specific hard stop command for one wallbox."""
    if not isinstance(c_data, dict):
        return False
    charger = c_data.get("charger")
    if charger is None:
        return False
    if not _guard_checked:
        stop_intent = {"kind": "stop", "method": "stop", "reason": reason or "stop"}
        if not _wallbox_command_guard_allows(c_data, stop_intent, c_id=c_id, reason=reason or "stop"):
            return False
    now_value = time.time()
    c_data["_last_manager_stop_request_ts"] = now_value
    c_data["_last_manager_zero_anchor_ts"] = now_value
    c_data["_last_manager_zero_anchor_reason"] = str(reason or "stop")
    c_data["_manager_zero_anchor_active"] = True
    c_data["_aha_real_charge_confirmed"] = False
    c_data["_aha_real_charge_confirmed_since"] = 0.0
    # A following 0 W sample after our own Stop/Abort is not an external
    # vehicle end. Drop the previous real-charge marker so the AHA latch only
    # reacts to charge drops that were not caused by E3DC-Control.
    c_data["_real_charge_since"] = 0.0
    c_data["_openwb_start_reject_anchor_ts"] = 0.0
    try:
        if hasattr(charger, "set_amp_sonnenmodus"):
            force_state = 1 if _native_e3dc_hard_stop_allowed(reason) else None
            if force_state is None:
                c_data["_native_e3dc_hard_stop_suppressed"] = str(reason or "stop")
            ok = _execute_wallbox_driver_command(
                c_data,
                {
                    "kind": "stop",
                    "method": "set_amp_sonnenmodus",
                    "amp": 6,
                    "force_state": force_state,
                    "reason": reason or "stop",
                    "_control_guard_checked": True,
                },
                c_id=c_id,
                reason=reason,
            )
            if ok and force_state == 1:
                _mark_manager_stop_display_pending(c_data, reason or "stop", now_ts=now_value)
            return ok
        if hasattr(charger, "set_amp_and_state"):
            ok = _execute_wallbox_driver_command(
                c_data,
                {
                    "kind": "stop",
                    "method": "set_amp_and_state",
                    "amp": 0,
                    "force_state": 1,
                    "reason": reason or "stop",
                    "_control_guard_checked": True,
                },
                c_id=c_id,
                reason=reason,
            )
            if ok:
                _mark_manager_stop_display_pending(c_data, reason or "stop", now_ts=now_value)
            return ok
        if hasattr(charger, "set_direct_current"):
            return _execute_wallbox_driver_command(
                c_data,
                {
                    "kind": "stop",
                    "method": "set_direct_current",
                    "amp": 0,
                    "reason": reason or "stop",
                    "_control_guard_checked": True,
                },
                c_id=c_id,
                reason=reason,
            )
        if hasattr(charger, "release_to_e3dc"):
            return _execute_wallbox_driver_command(
                c_data,
                {
                    "kind": "stop",
                    "method": "release_to_e3dc",
                    "max_amp": 6,
                    "reason": reason or "stop",
                    "_control_guard_checked": True,
                },
                c_id=c_id,
                reason=reason,
            )
    except Exception as exc:
        if c_id is not None:
            logger.warning("WB%d Stop-Befehl fehlgeschlagen (%s): %s" % (c_id, reason or "stop", exc))
        else:
            logger.warning("Wallbox Stop-Befehl fehlgeschlagen (%s): %s" % (reason or "stop", exc))
    return False

def _mark_manager_zero_anchor(c_data, reason="zero_anchor"):
    if not isinstance(c_data, dict):
        return
    now_value = time.time()
    c_data["_last_manager_stop_request_ts"] = now_value
    c_data["_last_manager_zero_anchor_ts"] = now_value
    c_data["_last_manager_zero_anchor_reason"] = str(reason or "zero_anchor")
    c_data["_manager_zero_anchor_active"] = True
    c_data["_aha_real_charge_confirmed"] = False
    c_data["_aha_real_charge_confirmed_since"] = 0.0
    c_data["_real_charge_since"] = 0.0
    c_data["_openwb_start_reject_anchor_ts"] = 0.0

def _mark_manager_charge_anchor(c_data, amp=0, reason="charge_anchor", reset_real_marker=False):
    if not isinstance(c_data, dict):
        return
    now_value = time.time()
    try:
        amp_value = int(round(float(amp or 0)))
    except (TypeError, ValueError):
        amp_value = 0
    c_data["_last_manager_charge_anchor_ts"] = now_value
    c_data["_last_manager_charge_anchor_amp"] = max(0, amp_value)
    c_data["_last_manager_charge_anchor_reason"] = str(reason or "charge_anchor")
    if amp_value > 0:
        c_data["_manager_zero_anchor_active"] = False
    if reset_real_marker:
        c_data["_real_charge_since"] = 0.0
        c_data["_aha_real_charge_confirmed"] = False
        c_data["_aha_real_charge_confirmed_since"] = 0.0

def _openwb_pro_start_hold_s(config=None):
    return openwb_pro_session.start_hold_s(config)

def _mark_openwb_pro_start_offer(
    c_data,
    amp=6,
    *,
    now_ts=None,
    config=None,
    charger_max_amp=32,
    refresh=False,
):
    if isinstance(c_data, dict):
        now_value = _cfg_float(now_ts, 0.0)
        if now_value <= 0.0:
            now_value = time.time()
        try:
            amp_value = int(float(amp or 0))
        except (TypeError, ValueError):
            amp_value = 0
        if amp_value >= 6 and _cfg_float(c_data.get("_openwb_start_reject_anchor_ts"), 0.0) <= 0.0:
            c_data["_openwb_start_reject_anchor_ts"] = now_value
    openwb_pro_session.mark_start_offer(
        c_data,
        amp,
        now_ts=now_ts,
        config=config,
        charger_max_amp=charger_max_amp,
        refresh=refresh,
    )

def _openwb_pro_start_hold_active(c_data, now_ts=None, *, hw_charging=False, stable_hw_power_w=0.0):
    return openwb_pro_session.start_hold_active(
        c_data,
        now_ts,
        hw_charging=hw_charging,
        stable_hw_power_w=stable_hw_power_w,
    )

def _openwb_pro_recent_start_window_active(c_data, now_ts=None, *, config=None, min_amp=6):
    if not isinstance(c_data, dict):
        return False
    now_value = _cfg_float(now_ts, 0.0)
    if now_value <= 0.0:
        now_value = time.time()
    last_start_ts = _cfg_float(c_data.get("last_start_ts"), 0.0)
    if last_start_ts <= 0.0:
        return False
    start_age_s = now_value - last_start_ts
    if start_age_s < 0.0 or start_age_s >= _openwb_pro_start_hold_s(config):
        return False
    offered_amp = max(
        _cfg_float(c_data.get("current_set_amp", 0), 0.0),
        _cfg_float(c_data.get("_openwb_pro_start_hold_amp", 0), 0.0),
    )
    return bool(offered_amp >= float(min_amp or 6))

def _openwb_pro_phase_wait_s(config=None):
    return openwb_pro_session.phase_wait_s(config)

def _mark_openwb_pro_phase_wait(
    c_data,
    phases,
    *,
    current_amp=0,
    now_ts=None,
    config=None,
    charger_max_amp=32,
):
    openwb_pro_session.mark_phase_wait(
        c_data,
        phases,
        current_amp=current_amp,
        now_ts=now_ts,
        config=config,
        charger_max_amp=charger_max_amp,
    )

def _clear_openwb_pro_phase_wait(c_data):
    openwb_pro_session.clear_phase_wait(c_data)

def _openwb_pro_phase_wait_active(c_data, status=None, now_ts=None, *, stable_hw_power_w=0.0):
    return openwb_pro_session.phase_wait_active(
        c_data,
        status,
        now_ts,
        stable_hw_power_w=stable_hw_power_w,
    )

def _openwb_pro_direct_bulk_ready(status=None, *, hw_charging=False, stable_hw_power_w=0.0):
    return openwb_pro_session.direct_bulk_ready(
        status,
        hw_charging=hw_charging,
        stable_hw_power_w=stable_hw_power_w,
    )

def _openwb_pro_direct_target_amp(
    current_amp,
    direct_amp,
    direct_direction,
    *,
    bulk_ready=False,
    start_amp=6,
    down_step_a=2,
    current_step_amp=1.0,
):
    return openwb_pro_session.direct_target_amp(
        current_amp,
        direct_amp,
        direct_direction,
        bulk_ready=bulk_ready,
        start_amp=start_amp,
        down_step_a=down_step_a,
        current_step_amp=current_step_amp,
    )

def _stop_grid_session_after_window(
    c_data,
    current_amp=0,
    hw_charging=False,
    stable_hw_power_w=0.0,
    now_ts=None,
    last_change_ts=None,
    c_id=None,
):
    """Stop one grid-charge session when its planned/price window has ended."""
    if not isinstance(c_data, dict) or not c_data.get("_grid_session_active_last", False):
        return False
    need_window_stop = bool(
        c_data.get("is_charging", False)
        or int(c_data.get("current_set_amp", 0) or 0) > 0
        or int(current_amp or 0) > 0
        or hw_charging
        or float(stable_hw_power_w or 0.0) > 500.0
    )
    if not need_window_stop:
        c_data["_grid_session_active_last"] = False
        return False

    _send_wallbox_stop_command(c_data, c_id=c_id, reason="grid_window_end")

    now_value = float(now_ts if now_ts is not None else time.time())
    c_data["current_set_amp"] = 0
    c_data["is_charging"] = False
    c_data["_grid_session_active_last"] = False
    c_data["_pv_mode_active"] = False
    c_data["_wb_stop_sent_active"] = True
    c_data["_last_stop_toggle_ts"] = now_value
    c_data["_openwb_zero_budget_since"] = 0.0
    c_data["_native_multi_zero_budget_since"] = 0.0
    if isinstance(last_change_ts, dict) and c_id is not None:
        last_change_ts[c_id] = now_value
    if c_id is not None:
        logger.info("WB%d Netzladefenster beendet: Wallbox gestoppt" % c_id)
    return True

def write_storage_intent(data):
    """Meldet Wallbox-Wuensche an den Storage Manager.

    Der Wallbox Manager steuert keine Hausbatterie direkt. Er beschreibt nur,
    ob die Wallbox wegen wbminSoC/Modus einen Batterieschutz wuenscht. Der
    Storage Manager priorisiert diesen Wunsch gegen Ladekurve, Pre-Dump,
    Preisfenster und Abregelschutz und sendet als einzige Stelle EMS-Befehle.
    """
    try:
        payload = dict(data or {})
        payload["ts"] = int(time.time())
        payload["source"] = "wallbox_manager"
        tmp = WB_STORAGE_INTENT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, WB_STORAGE_INTENT_FILE)
    except Exception:
        pass


def _wallbox_observed_phase_count(status):
    st = status if isinstance(status, dict) else {}
    phase_powers = [
        abs(_cfg_float(st.get(key), 0.0))
        for key in ("phase_power_l1_w", "phase_power_l2_w", "phase_power_l3_w")
    ]
    measured = sum(1 for value in phase_powers if value > 100.0)
    if measured in (1, 2, 3):
        return measured
    for key in ("phase_actual_phases", "phases_actual", "phases_in_use", "number_phases"):
        phases = _valid_phase_count(st.get(key), 0)
        if phases:
            return phases
    pha = int(_cfg_float(st.get("pha"), 0.0))
    return 3 if pha == 56 else (2 if pha == 24 else (1 if pha in (8, 16, 32) else 0))


def _observe_wallbox_phase_transition(c_data, previous_status, status, *, now_ts=None):
    """Erkenne auch Phasenwechsel, die ein externer Wallbox-Master ausführt."""

    if not isinstance(c_data, dict) or not isinstance(status, dict):
        return {}
    if status.get("driver_status_valid") is False or status.get("driver_status_stale"):
        return {}
    now_value = float(now_ts if now_ts is not None else time.time())
    current = status
    previous = previous_status if isinstance(previous_status, dict) else {}
    connected = _wb_status_connected(current)
    existing = phase_transition_reservation(
        c_data,
        status=current,
        now_ts=now_value,
        connected=connected,
    )
    if existing.get("active"):
        return existing

    current_target = _valid_phase_count(current.get("phases_target"), 0)
    previous_target = _valid_phase_count(previous.get("phases_target"), 0)
    current_actual = _wallbox_observed_phase_count(current)
    previous_actual = _wallbox_observed_phase_count(previous)
    previous_power_w = _wb_status_real_power(previous)
    current_power_w = _wb_status_real_power(current)
    was_active = bool(_wb_status_real_charging(previous) or previous_power_w > 500.0)
    is_active = bool(_wb_status_real_charging(current) or current_power_w > 500.0)
    explicit_transition = bool(
        current.get("phase_transition_active")
        or current.get("phase_switch_active")
        or current.get("phase_switch_in_progress")
    )

    target = 0
    reason = ""
    if explicit_transition and current_target in (1, 3):
        target = current_target
        reason = "driver_reports_phase_transition"
    elif (
        current_target in (1, 3)
        and previous_target in (1, 3)
        and current_target != previous_target
        and (was_active or is_active)
    ):
        target = current_target
        reason = "observed_phase_target_change"
    elif (
        current_target in (1, 3)
        and previous_actual in (1, 3)
        and current_target != previous_actual
        and was_active
    ):
        target = current_target
        reason = "observed_target_actual_mismatch"
    elif (
        current_actual in (1, 3)
        and previous_actual in (1, 3)
        and current_actual != previous_actual
        and (was_active or is_active)
    ):
        target = current_actual
        reason = "observed_actual_phase_change"
    if target not in (1, 3):
        return existing

    reservation_status = dict(current)
    reservation_status["real_power_w"] = max(current_power_w, previous_power_w)
    charger = c_data.get("charger")
    charger_name = charger.__class__.__name__ if charger is not None else "Wallbox"
    begin_phase_transition_reservation(
        c_data,
        target,
        status=reservation_status,
        now_ts=now_value,
        charger_max_amp=getattr(charger, "max_amp", c_data.get("max_amp", 32)),
        source="%s_observed_phase_change" % charger_name,
        reason=reason,
    )
    return phase_transition_reservation(
        c_data,
        status=current,
        now_ts=now_value,
        connected=connected,
    )


def _wallbox_phase_transition_reservation(chargers, statuses=None, now_ts=None):
    """Aggregiere Phasenwechsel-Reservierungen aller überwachten Wallboxen."""

    now_value = float(now_ts if now_ts is not None else time.time())
    status_by_id = {
        int(entry.get("id", 0) or 0): entry.get("status")
        for entry in (statuses if isinstance(statuses, list) else []) if isinstance(entry, dict)
        if int(entry.get("id", 0) or 0) > 0 and isinstance(entry.get("status"), dict)
    }
    details = []
    for box in chargers if isinstance(chargers, list) else []:
        if not isinstance(box, dict):
            continue
        charger = box.get("charger")
        charger_id = int(box.get("id", 0) or 0)
        status = status_by_id.get(charger_id) or _openwb_pro_driver_status(box, charger)
        has_connection_state = any(
            key in status
            for key in ("plug_state", "car", "connected", "plugged")
        )
        connected = (
            bool(openwb_pro_session.status_connected(status))
            if has_connection_state
            else None
        )
        reservation = phase_transition_reservation(
            box,
            status=status,
            now_ts=now_value,
            connected=connected,
        )
        if not reservation.get("active"):
            continue
        details.append({
            "charger_id": charger_id,
            "target_phases": int(reservation.get("target_phases", 0) or 0),
            "reserved_w": int(reservation.get("reserved_w", 0) or 0),
            "started_ts": float(reservation.get("started_ts", 0.0) or 0.0),
            "expires_ts": float(reservation.get("expires_ts", 0.0) or 0.0),
            "remaining_s": round(float(reservation.get("remaining_s", 0.0) or 0.0), 1),
            "source": str(reservation.get("source") or "manager_phase_command"),
            "reason": str(reservation.get("reason") or "phase_transition"),
        })

    targets = sorted({
        int(detail.get("target_phases", 0) or 0)
        for detail in details
        if int(detail.get("target_phases", 0) or 0) in (1, 3)
    })
    sources = sorted({
        str(detail.get("source") or "")
        for detail in details
        if str(detail.get("source") or "")
    })
    return {
        "active": bool(details),
        "reserved_w": sum(max(0, int(detail.get("reserved_w", 0) or 0)) for detail in details),
        "target_phases": targets[0] if len(targets) == 1 else 0,
        "targets": targets,
        "charger_ids": [
            int(detail.get("charger_id", 0) or 0)
            for detail in details
            if int(detail.get("charger_id", 0) or 0) > 0
        ],
        "started_ts": min(
            (float(detail.get("started_ts", 0.0) or 0.0) for detail in details),
            default=0.0,
        ),
        "expires_ts": max(
            (float(detail.get("expires_ts", 0.0) or 0.0) for detail in details),
            default=0.0,
        ),
        "source": sources[0] if len(sources) == 1 else ("manager_phase_commands" if sources else ""),
        "details": details,
    }


def _effective_wallbox_floor_soc(config, live, wb_minsoc, *extra_floors):
    """Return the wallbox discharge floor including the E3DC wallbox limit."""
    live_data = live if isinstance(live, dict) else {}

    def _num(value, default=0.0):
        try:
            text = str(value).strip().replace(",", ".") if value is not None else ""
            return float(text) if text else float(default)
        except Exception:
            return float(default)

    def _pct(value):
        return max(0.0, min(100.0, _num(value, 0.0)))

    e3dc_wb_floor = _pct(live_data.get("e3dc_wb_discharge_bat_until_soc", 0.0))
    floor = max(_pct(wb_minsoc), e3dc_wb_floor)
    for extra in extra_floors:
        floor = max(floor, _pct(extra))
    return floor

def _load_wallbox_abort_state():
    try:
        if not os.path.exists(WB_ABORT_STATE_FILE):
            return {}
        with open(WB_ABORT_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_wallbox_abort_state(chargers):
    try:
        now = time.time()
        data = {}
        for c_data in chargers or []:
            c_id = str(c_data.get("id", ""))
            if not c_id:
                continue
            count = int(c_data.get("abort_count", 0) or 0)
            cooldown_ts = float(c_data.get("abort_cooldown_ts", 0.0) or 0.0)
            full_blocked = bool(c_data.get("_bev_full_blocked", False))
            full_block_reason = str(c_data.get("_bev_full_block_reason") or "")
            soft_until = float(c_data.get("_openwb_start_reject_soft_until", 0.0) or 0.0)
            if count > 0 or cooldown_ts > now or full_blocked or soft_until > now:
                data[c_id] = {
                    "abort_count": count,
                    "abort_cooldown_ts": cooldown_ts,
                    "bev_full_blocked": full_blocked,
                    "bev_full_block_reason": full_block_reason,
                    "openwb_start_reject_soft_until": soft_until,
                }
        tmp = WB_ABORT_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, WB_ABORT_STATE_FILE)
    except Exception:
        pass

def _restored_abort_fields(abort_state, charger_id):
    try:
        raw = abort_state.get(str(charger_id), {}) if isinstance(abort_state, dict) else {}
        cooldown_ts = float(raw.get("abort_cooldown_ts", 0.0) or 0.0)
        count = int(raw.get("abort_count", 0) or 0)
        full_blocked = bool(raw.get("bev_full_blocked", False))
        full_block_reason = str(raw.get("bev_full_block_reason") or "")
        soft_until = float(raw.get("openwb_start_reject_soft_until", 0.0) or 0.0)
        if cooldown_ts <= 0.0 and count <= 0 and not full_blocked and soft_until <= 0.0:
            return {}
        if not full_blocked and time.time() - max(cooldown_ts, soft_until) > 900.0:
            return {}
        return {
            "abort_count": max(0, count),
            "abort_cooldown_ts": max(0.0, cooldown_ts),
            "_bev_full_blocked": full_blocked,
            "_bev_full_block_reason": full_block_reason,
            "_openwb_start_reject_soft_until": max(0.0, soft_until),
        }
    except Exception:
        return {}

def _load_wallbox_phase_state():
    try:
        if not os.path.exists(WB_PHASE_STATE_FILE):
            return {}
        with open(WB_PHASE_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_wallbox_phase_state(chargers):
    try:
        now = time.time()
        data = {}
        for c_data in chargers or []:
            c_id = str(c_data.get("id", ""))
            if not c_id:
                continue
            block_until = float(c_data.get("_phase_3p_block_until", 0.0) or 0.0)
            if block_until > now:
                data[c_id] = {
                    "phase_3p_block_until": block_until,
                    "ts": now,
                }
        tmp = WB_PHASE_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, WB_PHASE_STATE_FILE)
    except Exception:
        pass

def _restored_phase_fields(phase_state, charger_id):
    try:
        raw = phase_state.get(str(charger_id), {}) if isinstance(phase_state, dict) else {}
        block_until = float(raw.get("phase_3p_block_until", 0.0) or 0.0)
        if block_until <= time.time():
            return {}
        return {"_phase_3p_block_until": block_until}
    except Exception:
        return {}

def read_price_boost_allow(device, config=None):
    try:
        if config is not None and str(config.get("cheap_grid_boost_enable", 0)).strip().lower() not in ("1", "true", "yes", "on"):
            return False
        if not os.path.exists(PRICE_BOOST_PLAN_FILE):
            return False
        with open(PRICE_BOOST_PLAN_FILE, "r", encoding="utf-8") as f:
            plan = json.load(f)
        win = plan.get("active_window") or {}
        now_ms = int(time.time() * 1000)
        start_ms = int(win.get("start_timestamp", 0) or 0)
        end_ms = int(win.get("end_timestamp", 0) or 0)
        return bool(plan.get("enabled") and plan.get("active")
                    and start_ms <= now_ms < end_ms
                    and plan.get("allow", {}).get(device, False))
    except Exception:
        return False

def read_market_plan_allow(device, storage_plan=None, config=None):
    try:
        ctx = current_market_consumer_release(storage_plan or {}, device, config)
        return ctx if isinstance(ctx, dict) else {"allowed": False}
    except Exception:
        return {"allowed": False, "reason": "market_plan_error"}


def _wallbox_market_price_mode_ids(wb_charge_mode, charger_ids):
    """Return chargers that explicitly opted into price-controlled grid charging."""
    result = set()
    for c_id in charger_ids or []:
        try:
            cid = int(c_id)
            if normalize_wb_mode(wb_charge_mode.get(cid, MODE_OFF)) == MODE_PRICE:
                result.add(cid)
        except Exception:
            continue
    return result

def read_predump_allow(device, config=None):
    """Pre-Dump-Verbraucherfreigabe lesen.

    Kein Netzmodus: die Wallbox darf nur Speicher/PV nutzen und wird wie
    C++ Mode 10 behandelt.
    """
    try:
        if config is not None:
            key = "predump_%s_enable" % device
            if str(config.get(key, 0)).strip().lower() not in ("1", "true", "yes", "on"):
                return False
        if not os.path.exists(PREDUMP_PLAN_FILE):
            return False
        with open(PREDUMP_PLAN_FILE, "r", encoding="utf-8") as f:
            plan = json.load(f)
        now = int(time.time())
        expires_ts = int(plan.get("expires_ts", 0) or 0)
        return bool(plan.get("enabled") and plan.get("active")
                    and now <= expires_ts
                    and plan.get("allow", {}).get(device, False))
    except Exception:
        return False


def _wb_status_connected(status):
    """True nur bei echtem Fahrzeug-/Steckersignal, nicht bei Sollstrom."""
    return wallbox_decision.status_connected(status)


def _wallbox_price_mode_grid_allowed_for_charger(wb_charge_mode, c_id, mode5_grid_allowed=False):
    """True when this concrete charger is in open price-limit mode."""
    if not bool(mode5_grid_allowed):
        return False
    try:
        return normalize_wb_mode(wb_charge_mode.get(int(c_id), MODE_OFF)) == MODE_PRICE
    except Exception:
        return False


def _clear_wallbox_manual_pause_text_config(path, key):
    if not path or not os.path.exists(path):
        return True
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
        changed = False
        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and not stripped.startswith("//") and "=" in line:
                name, raw_value = line.split("=", 1)
                if name.strip().lower() == key:
                    found = True
                    value_part = raw_value.split("//", 1)[0].split("#", 1)[0].strip()
                    if _truthy_config(value_part):
                        new_lines.append(f"{name.rstrip()} = 0")
                        changed = True
                        continue
            new_lines.append(line)
        if not found or not changed:
            return True
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o664)
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.warning(f"Manuelle Wallbox-Pause konnte in {path} nicht geloescht werden: {exc}")
        return False


def _clear_wallbox_manual_pause_json_config(path, key):
    if not path or not os.path.exists(path):
        return True
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return True
        changed = False
        containers = [data]
        if isinstance(data.get("config"), dict):
            containers.append(data["config"])
        for container in containers:
            for existing_key in list(container.keys()):
                if str(existing_key).strip().lower() == key and _truthy_config(container.get(existing_key)):
                    container[existing_key] = "0"
                    changed = True
        if not changed:
            return True
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)
        try:
            if os.path.abspath(path) == os.path.abspath(V4_CONFIG_FILE):
                apply_config_secret_permissions(path, data=data)
            else:
                os.chmod(path, 0o664)
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.warning(f"Manuelle Wallbox-Pause konnte in {path} nicht geloescht werden: {exc}")
        return False


def _clear_wallbox_manual_pause_config(wb_id):
    key = f"wb{int(wb_id)}_manual_pause"
    text_ok = _clear_wallbox_manual_pause_text_config(CONFIG_FILE, key)
    json_ok = _clear_wallbox_manual_pause_json_config(V4_CONFIG_FILE, key)
    return bool(text_ok and json_ok)


def _auto_clear_manual_pause_after_unplug(wb_manual_pause, valid_chargers_status, dyn_config):
    changed = False
    for item in valid_chargers_status or []:
        try:
            cid = int(item.get("id", 0) or 0)
        except Exception:
            cid = 0
        if cid not in (1, 2) or not bool(wb_manual_pause.get(cid, False)):
            continue
        status = item.get("status")
        if not isinstance(status, dict) or _wb_status_connected(status):
            continue
        if _clear_wallbox_manual_pause_config(cid):
            wb_manual_pause[cid] = False
            if isinstance(dyn_config, dict):
                dyn_config[f"wb{cid}_manual_pause"] = "0"
            changed = True
            logger.info(f"[WB{cid}] Manuelle Pause nach Abstecken automatisch beendet.")
        else:
            logger.warning(f"[WB{cid}] Manuelle Pause bleibt aktiv: Konfiguration konnte nicht aktualisiert werden.")
    return changed


def _wb_status_real_power(status):
    """Echte gemessene Wallboxleistung, keine Ableitung aus Soll-Ampere."""
    return wallbox_decision.status_real_power(status)


def _wb_status_real_charging(status):
    """Laden zaehlt nur mit Hardware-Ladebit oder verifizierter Phasenleistung."""
    return wallbox_decision.status_real_charging(status)


def _wallbox_status_stale_guard_s(config, default_s=WALLBOX_STATUS_STALE_DEFAULT_S):
    try:
        raw = (config or {}).get("wallbox_status_stale_guard_s", default_s)
        return max(10.0, min(300.0, float(str(raw).strip().replace(",", ".") or default_s)))
    except Exception:
        return float(default_s)


def _copy_wallbox_status_diagnostics(detail, status):
    if not isinstance(detail, dict) or not isinstance(status, dict):
        return detail
    for key in WALLBOX_STATUS_DIAG_KEYS:
        if key in status and key not in detail:
            detail[key] = status[key]
    return detail


def _safe_stale_wallbox_status(c_data, *, now_ts=None, age_s=999999.0, reason="status_unavailable"):
    now_value = time.time() if now_ts is None else float(now_ts)
    last_status = c_data.get("last_valid") if isinstance(c_data, dict) else None
    seed_status = last_status if isinstance(last_status, dict) else c_data.get("_last_invalid_status")
    status = dict(seed_status) if isinstance(seed_status, dict) else {}
    status.update({
        "car": 1,
        "amp": 0,
        "pha": 0,
        "charging": False,
        "charge_state": False,
        "plug_state": False,
        "locked": False,
        "real_power_w": 0.0,
        "power_w": 0.0,
        "evse_current": 0.0,
        "phase_power_l1_w": 0.0,
        "phase_power_l2_w": 0.0,
        "phase_power_l3_w": 0.0,
        "phase_power_sum_w": 0.0,
        "phase_power_verified": False,
        "phase_apparent_l1_va": 0.0,
        "phase_apparent_l2_va": 0.0,
        "phase_apparent_l3_va": 0.0,
        "apparent_power_va": 0.0,
        "power_factor": 0.0,
        "phases_in_use": 0,
        "phases_actual": 0,
        "driver_status_valid": False,
        "driver_status_stale": True,
        "driver_status_degraded": True,
        "driver_status_age_s": round(float(age_s), 1),
        "driver_status_reason": str(reason or "status_unavailable"),
        "driver_status_last_sample_ts": int(now_value),
        "driver_status_last_ok_ts": int(float(c_data.get("_status_last_ok_ts", 0.0) or 0.0)) if isinstance(c_data, dict) else 0,
        "charge_contract": {},
        "charge_truth": "unknown",
        "charge_source": "driver_status_stale",
    })
    if status.get("phases_target") is None:
        status["phases_target"] = 0
    return status


def _wallbox_driver_status_or_stale(c_data, raw_status, *, now_ts=None, stale_guard_s=WALLBOX_STATUS_STALE_DEFAULT_S):
    now_value = time.time() if now_ts is None else float(now_ts)
    raw_invalid = bool(
        isinstance(raw_status, dict)
        and (
            raw_status.get("driver_status_valid") is False
            or raw_status.get("driver_status_stale") is True
            or raw_status.get("driver_status_plausible") is False
            or raw_status.get("driver_status_glitch") is True
        )
    )
    if isinstance(raw_status, dict) and raw_status and not raw_invalid:
        raw_status["driver_status_valid"] = True
        raw_status["driver_status_stale"] = False
        raw_status["driver_status_degraded"] = False
        raw_status["driver_status_age_s"] = 0.0
        raw_status["driver_status_reason"] = str(raw_status.get("driver_status_reason") or "fresh")
        raw_status["driver_status_last_sample_ts"] = int(now_value)
        raw_status["driver_status_last_ok_ts"] = int(now_value)
        c_data["_status_last_ok_ts"] = now_value
        c_data["_status_last_fail_ts"] = 0.0
        c_data["_status_stale_logged"] = False
        c_data["last_valid"] = dict(raw_status)
        return raw_status
    if isinstance(raw_status, dict):
        c_data["_last_invalid_status"] = dict(raw_status)

    last_ok_ts = float(c_data.get("_status_last_ok_ts", 0.0) or 0.0)
    last_status = c_data.get("last_valid")
    age_s = now_value - last_ok_ts if last_ok_ts > 0.0 else 999999.0
    c_data["_status_last_fail_ts"] = now_value
    if isinstance(last_status, dict) and age_s <= float(stale_guard_s):
        status = dict(last_status)
        status["driver_status_valid"] = False
        status["driver_status_degraded"] = True
        status["driver_status_stale"] = False
        status["driver_status_age_s"] = round(float(age_s), 1)
        status["driver_status_reason"] = str(
            (raw_status or {}).get("driver_status_glitch_reason")
            or (raw_status or {}).get("driver_status_reason")
            or "last_good_within_grace"
        )
        status["driver_status_last_sample_ts"] = int(now_value)
        status["driver_status_last_ok_ts"] = int(last_ok_ts)
        for key in (
            "driver_status_plausible",
            "driver_status_glitch",
            "driver_status_glitch_reason",
            "driver_status_last_good_ts",
        ):
            if isinstance(raw_status, dict) and key in raw_status:
                status[key] = raw_status[key]
        return status

    if not c_data.get("_status_stale_logged", False):
        try:
            logger.warning(
                "WB%d Status stale: keine frischen Treiberdaten seit %.0fs, Messwerte werden entwertet."
                % (int(c_data.get("id", 0) or 0), float(age_s))
            )
        except Exception:
            pass
        c_data["_status_stale_logged"] = True
    stale_reason = (
        (raw_status or {}).get("driver_status_glitch_reason")
        or (raw_status or {}).get("driver_status_reason")
        or "status_timeout"
    )
    return _safe_stale_wallbox_status(c_data, now_ts=now_value, age_s=age_s, reason=str(stale_reason))


def _evaluate_e3dc_session_for_manager(
    c_data,
    status,
    *,
    cap_amp=0,
    budget_ready=False,
    switch_to_1p_ready=False,
    grid_allowed=False,
    price_active=False,
    price_boost_active=False,
    predump_active=False,
    mode_off=False,
    priority_forced_stop=False,
    min_amp=6,
    now_ts=None,
    start_verify_s=180,
):
    """Attach the pure E3DC session state to the live status dict."""

    if not e3dc_session.is_e3dc_native_charger((c_data or {}).get("charger")):
        return {}
    contract = _wallbox_e3dc_native_production_contract(c_data, status, None)
    if contract.get("enabled", False):
        c_data["_e3dc_native_contract"] = contract
    session = e3dc_session.evaluate_session(
        status,
        current_set_amp=(c_data or {}).get("current_set_amp", 0),
        cap_amp=cap_amp,
        min_amp=min_amp,
        budget_ready=bool(budget_ready),
        switch_to_1p_ready=bool(switch_to_1p_ready),
        grid_allowed=bool(grid_allowed),
        price_active=bool(price_active),
        price_boost_active=bool(price_boost_active),
        predump_active=bool(predump_active),
        mode_off=bool(mode_off),
        priority_forced_stop=bool(priority_forced_stop),
        stop_sent_active=bool((c_data or {}).get("_wb_stop_sent_active", False)),
        ended_latched=bool((c_data or {}).get("_bev_full_blocked", False)),
        end_reason=str((c_data or {}).get("_bev_full_block_reason") or ""),
        last_start_ts=(c_data or {}).get("last_start_ts", 0.0),
        now_ts=time.time() if now_ts is None else now_ts,
        start_verify_s=start_verify_s,
    )
    if status is not None:
        _apply_e3dc_native_production_contract_to_status(status, contract)
        e3dc_session.apply_session_to_status(status, session)
    c_data["_e3dc_session_state"] = session.get("state", e3dc_session.STATE_IDLE)
    c_data["_e3dc_session"] = session
    return session


def _evaluate_openwb_pro_session_for_manager(
    c_data,
    status,
    *,
    cap_amp=0,
    budget_ready=False,
    switch_to_1p_ready=False,
    grid_allowed=False,
    price_active=False,
    price_boost_active=False,
    predump_active=False,
    mode_off=False,
    priority_forced_stop=False,
    min_amp=6,
    now_ts=None,
    start_verify_s=180,
    stable_hw_power_w=0.0,
):
    """Attach the pure openWB Pro session contract to the live status dict."""

    if not openwb_pro_session.is_openwb_pro_charger((c_data or {}).get("charger")):
        return {}
    stop_display = _manager_stop_display_state(c_data, now_ts=now_ts)
    session = openwb_pro_session.evaluate_session(
        status,
        state_data=c_data,
        current_set_amp=(c_data or {}).get("current_set_amp", 0),
        cap_amp=cap_amp,
        min_amp=min_amp,
        budget_ready=bool(budget_ready),
        switch_to_1p_ready=bool(switch_to_1p_ready),
        grid_allowed=bool(grid_allowed),
        price_active=bool(price_active),
        price_boost_active=bool(price_boost_active),
        predump_active=bool(predump_active),
        mode_off=bool(mode_off),
        priority_forced_stop=bool(priority_forced_stop),
        stop_sent_active=bool((c_data or {}).get("_wb_stop_sent_active", False)),
        manager_stop_pending=bool(stop_display.get("active", False)),
        manager_stop_reason=str(stop_display.get("reason", "") or ""),
        ended_latched=bool((c_data or {}).get("_bev_full_blocked", False)),
        end_reason=str((c_data or {}).get("_bev_full_block_reason") or ""),
        last_start_ts=(c_data or {}).get("last_start_ts", 0.0),
        now_ts=time.time() if now_ts is None else now_ts,
        start_verify_s=start_verify_s,
        stable_hw_power_w=stable_hw_power_w,
    )
    if status is not None:
        openwb_pro_session.apply_session_to_status(status, session)
        finished_contract = (c_data or {}).get("_openwb_pro_vehicle_finished_contract")
        if isinstance(finished_contract, dict):
            openwb_pro_session.apply_vehicle_finished_drop_to_status(status, finished_contract)
        retry_contract = (c_data or {}).get("_openwb_pro_start_retry_guard_contract")
        if isinstance(retry_contract, dict):
            status["openwb_pro_start_retry_guard_contract"] = retry_contract
    c_data["_openwb_pro_session_state"] = session.get("state", openwb_pro_session.STATE_IDLE)
    c_data["_openwb_pro_session"] = session
    return session


def _evaluate_goe_session_for_manager(
    c_data,
    status,
    *,
    cap_amp=0,
    budget_ready=False,
    grid_allowed=False,
    price_active=False,
    price_boost_active=False,
    predump_active=False,
    mode_off=False,
    priority_forced_stop=False,
    min_amp=6,
    now_ts=None,
    start_verify_s=180,
    stable_hw_power_w=0.0,
):
    """Attach the pure go-e session contract to the live status dict."""

    if not goe_session.is_goe_charger((c_data or {}).get("charger")):
        return {}
    session = goe_session.evaluate_session(
        status,
        current_set_amp=(c_data or {}).get("current_set_amp", 0),
        cap_amp=cap_amp,
        min_amp=min_amp,
        budget_ready=bool(budget_ready),
        grid_allowed=bool(grid_allowed),
        price_active=bool(price_active),
        price_boost_active=bool(price_boost_active),
        predump_active=bool(predump_active),
        mode_off=bool(mode_off),
        priority_forced_stop=bool(priority_forced_stop),
        stop_sent_active=bool((c_data or {}).get("_wb_stop_sent_active", False)),
        ended_latched=bool((c_data or {}).get("_bev_full_blocked", False)),
        end_reason=str((c_data or {}).get("_bev_full_block_reason") or ""),
        last_start_ts=(c_data or {}).get("last_start_ts", 0.0),
        now_ts=time.time() if now_ts is None else now_ts,
        start_verify_s=start_verify_s,
        stable_hw_power_w=stable_hw_power_w,
    )
    if status is not None:
        goe_session.apply_session_to_status(status, session)
    c_data["_goe_session_state"] = session.get("state", goe_session.STATE_IDLE)
    c_data["_goe_session"] = session
    return session


def _evaluate_openwb_session_for_manager(
    c_data,
    status,
    *,
    cap_amp=0,
    budget_ready=False,
    grid_allowed=False,
    price_active=False,
    price_boost_active=False,
    predump_active=False,
    mode_off=False,
    priority_forced_stop=False,
    primary_delegate=False,
    min_amp=6,
    now_ts=None,
    start_verify_s=180,
    stable_hw_power_w=0.0,
):
    """Attach the pure regular openWB Secondary session contract to status."""

    if not openwb_session.is_openwb_charger((c_data or {}).get("charger")):
        return {}
    session = openwb_session.evaluate_session(
        status,
        current_set_amp=(c_data or {}).get("current_set_amp", 0),
        cap_amp=cap_amp,
        min_amp=min_amp,
        budget_ready=bool(budget_ready),
        grid_allowed=bool(grid_allowed),
        price_active=bool(price_active),
        price_boost_active=bool(price_boost_active),
        predump_active=bool(predump_active),
        mode_off=bool(mode_off),
        priority_forced_stop=bool(priority_forced_stop),
        stop_sent_active=bool((c_data or {}).get("_wb_stop_sent_active", False)),
        ended_latched=bool((c_data or {}).get("_bev_full_blocked", False)),
        end_reason=str((c_data or {}).get("_bev_full_block_reason") or ""),
        primary_delegate=bool(primary_delegate),
        last_start_ts=(c_data or {}).get("last_start_ts", 0.0),
        now_ts=time.time() if now_ts is None else now_ts,
        start_verify_s=start_verify_s,
        stable_hw_power_w=stable_hw_power_w,
    )
    if status is not None:
        openwb_session.apply_session_to_status(status, session)
    c_data["_openwb_secondary_session_state"] = session.get("state", openwb_session.STATE_IDLE)
    c_data["_openwb_secondary_session"] = session
    return session


def _refresh_openwb_secondary_heartbeat_if_due(
    c_data,
    public_mode,
    *,
    now_ts,
    c_id=None,
):
    """Pflege die Secondary-Lease vor allen fruehen Wallbox-Loop-Ausstiegen."""

    data = c_data if isinstance(c_data, dict) else {}
    charger = data.get("charger")
    if (
        charger is None
        or charger.__class__.__name__ != "OpenWBCharger"
        or bool(getattr(charger, "primary_mode_enabled", False))
        or normalize_wb_mode(public_mode) == MODE_OFF
    ):
        return None
    driver_state = getattr(charger, "state", {})
    if not isinstance(driver_state, dict):
        driver_state = {}
    last_ok_ts = max(
        _cfg_float(data.get("_openwb_secondary_heartbeat_last_ok_ts", 0.0), 0.0),
        _cfg_float(driver_state.get("last_heartbeat_ts", 0.0), 0.0),
    )
    if not openwb_session.secondary_heartbeat_refresh_due(
        last_success_ts=last_ok_ts,
        last_attempt_ts=data.get("_openwb_secondary_heartbeat_last_attempt_ts", 0.0),
        now_ts=now_ts,
    ):
        return None
    data["_openwb_secondary_heartbeat_last_attempt_ts"] = now_ts
    ok = _execute_wallbox_driver_command(
        data,
        {
            "method": "set_pv_mode",
            "reason": "openwb_secondary_heartbeat_refresh",
        },
        c_id=c_id,
    )
    if ok:
        data["_openwb_secondary_heartbeat_last_ok_ts"] = max(
            _cfg_float(now_ts, 0.0),
            _cfg_float(driver_state.get("last_heartbeat_ts", 0.0), 0.0),
        )
    return bool(ok)


def _clear_stale_e3dc_stop_latch_for_grid_start(
    c_data,
    *,
    force_state=None,
    grid_allowed=False,
    price_active=False,
    pv_start_active=False,
    hw_charging=False,
    hw_power_w=0.0,
):
    """Release an old E3DC stop latch when a new owned start takes control."""

    if not isinstance(c_data, dict):
        return False
    try:
        fs = int(force_state) if force_state is not None else None
    except Exception:
        fs = None
    try:
        power_w = float(hw_power_w or 0.0)
    except Exception:
        power_w = 0.0
    if (
        fs == 2
        and bool(grid_allowed or price_active or pv_start_active)
        and not bool(hw_charging)
        and power_w <= 500.0
        and bool(c_data.get("_wb_stop_sent_active", False))
    ):
        c_data["_wb_stop_sent_active"] = False
        return True
    return False


def _native_e3dc_stop_latch_retry_due(
    c_data,
    *,
    now_ts=None,
    export_w=0.0,
    grid_start_allowed=False,
    cap_amp=0,
    budget_ready=False,
    charger_connected=False,
    hw_charging=False,
    hw_power_w=0.0,
    priority_forced_stop=False,
    min_amp=6,
    retry_s=300.0,
    soft_gap_s=60.0,
    strong_export_w=1800.0,
):
    """Return true when an old native E3DC stop latch may be released for retry."""

    if not isinstance(c_data, dict) or not bool(c_data.get("_wb_stop_sent_active", False)):
        return False
    if bool(priority_forced_stop) or bool(c_data.get("_bev_full_blocked", False)):
        return False
    try:
        now_value = time.time() if now_ts is None else float(now_ts)
    except (TypeError, ValueError):
        now_value = time.time()
    try:
        export_value = max(0.0, float(export_w or 0.0))
        grid_allowed = bool(grid_start_allowed)
        power_value = max(0.0, float(hw_power_w or 0.0))
        cap_value = int(round(float(cap_amp or 0)))
        min_value = max(1, int(round(float(min_amp or 6))))
        retry_value = max(30.0, float(retry_s or 300.0))
        soft_value = max(30.0, float(soft_gap_s or 60.0))
        strong_value = max(800.0, float(strong_export_w or 1800.0))
        last_stop = float(c_data.get("_last_manager_stop_request_ts", 0.0) or 0.0)
        last_start = float(c_data.get("last_start_ts", 0.0) or 0.0)
        last_retry = float(c_data.get("_native_last_start_retry_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    if not bool(charger_connected) or not bool(budget_ready) or cap_value < min_value:
        return False
    if bool(hw_charging) or power_value > 500.0:
        return False
    if not grid_allowed and export_value < 800.0:
        return False
    required_stop_gap_s = soft_value if (grid_allowed or export_value >= strong_value) else retry_value
    if last_stop > 0.0 and now_value - last_stop < required_stop_gap_s:
        return False
    if last_start > 0.0 and now_value - last_start < soft_value:
        return False
    if last_retry > 0.0 and now_value - last_retry < soft_value:
        return False
    return True


def _house_fuse_phase_import_amp(live, grid_power_raw):
    """Aktuelle Hausanschluss-Phasenlast aus RSCP-Messwerten ableiten."""
    phase_imports = []
    for key in ("grid_p1", "grid_p2", "grid_p3"):
        try:
            phase_w = float(live.get(key, 0.0) or 0.0)
        except Exception:
            phase_w = 0.0
        if abs(phase_w) > 1.0:
            phase_imports.append(max(0.0, phase_w) / 230.0)
    if phase_imports:
        return max(phase_imports)
    return max(0.0, float(grid_power_raw or 0.0)) / (230.0 * 3.0)


def _wallbox_grid_power_for_budget(live, grid_power_raw=0.0, acute_import_override_w=3500.0):
    """Gedämpftes Netzsignal für Budgetpfade, Rohwert bei akuter Netzlast."""
    try:
        raw = float(grid_power_raw or 0.0)
    except (TypeError, ValueError):
        raw = 0.0
    if not isinstance(live, dict):
        return raw
    try:
        filtered = float(live.get("Grid_Power_Filtered"))
    except (TypeError, ValueError):
        return raw
    try:
        override_w = max(0.0, float(acute_import_override_w or 0.0))
    except (TypeError, ValueError):
        override_w = 3500.0
    if not math.isfinite(filtered):
        return raw
    if live.get("Grid_Power_Filtered_Valid") is False:
        return raw
    if raw > override_w and raw > filtered:
        return raw
    return filtered


def _wallbox_house_fuse_cap_amp(
    live,
    chargers,
    valid_chargers_status,
    wb_charge_mode,
    effective_wb_mode,
    grid_power_raw,
    grid_max_amps,
    wb_max_amp,
    price_optimizing_active,
    price_boost_wallbox_active,
    effective_allow_grid,
):
    """Deckelt Netz-Modi gegen die Hausabsicherung.

    Mode 11/Preisfenster duerfen Netz nutzen. Trotzdem darf die Summe aus
    Hauslast und Wallbox-Sollstrom den per-Phase-SLS nicht ueberfahren. Bei
    mehreren einphasigen Wallboxen rechnen wir bewusst worst-case gleiche Phase.
    """
    try:
        grid_max_amps = float(grid_max_amps or 0.0)
    except Exception:
        grid_max_amps = 0.0
    if grid_max_amps <= 0:
        return int(wb_max_amp), False, 0.0, 0, 0.0

    def _id_enabled(flag_or_ids, c_id):
        if isinstance(flag_or_ids, dict):
            return bool(flag_or_ids.get(c_id, False))
        if isinstance(flag_or_ids, (set, list, tuple)):
            return c_id in flag_or_ids
        return bool(flag_or_ids)

    status_by_id = {
        int(v.get("id", 0) or 0): v.get("status")
        for v in (valid_chargers_status or [])
    }
    grid_wb_count = 0
    current_grid_wb_amp = 0.0
    for c_data in chargers:
        c_id = int(c_data.get("id", 0) or 0)
        c_mode = int(wb_charge_mode.get(c_id, effective_wb_mode) or 0)
        grid_allowed = bool(
            _id_enabled(price_optimizing_active, c_id)
            or price_boost_wallbox_active
            or (not isinstance(price_optimizing_active, (dict, set, list, tuple)) and effective_allow_grid)
            or c_mode == 11
        )
        if not grid_allowed:
            continue
        status = status_by_id.get(c_id)
        if not _wb_status_connected(status):
            continue
        grid_wb_count += 1
        if _wb_status_real_charging(status):
            try:
                amp = int(status.get("amp", 0) or 0)
            except Exception:
                amp = 0
            if amp <= 0:
                amp = int(c_data.get("current_set_amp", 0) or 0)
            current_grid_wb_amp += max(0, amp)

    if grid_wb_count <= 0:
        return int(wb_max_amp), False, 0.0, 0, 0.0

    phase_import_amp = _house_fuse_phase_import_amp(live, grid_power_raw)
    # Wenn die Wallbox bereits laeuft, steckt ihr Anteil im Netzbezug. Fuer den
    # naechsten Sollwert betrachten wir die uebrige Hauslast ohne aktuelle WB-A.
    base_without_wb_amp = max(0.0, phase_import_amp - current_grid_wb_amp)
    reserve_amp = 2.0
    room_amp = grid_max_amps - reserve_amp - base_without_wb_amp
    cap_amp = int(math.floor(room_amp / max(1, grid_wb_count)))
    cap_amp = max(0, min(int(wb_max_amp), cap_amp))
    return cap_amp, cap_amp < int(wb_max_amp), base_without_wb_amp, grid_wb_count, current_grid_wb_amp


def run():
    import signal
    logger.info("Starte E3DC-Control Multi Wallbox Manager...")

    # SIGTERM-Handler: Wallboxen sauber stoppen wenn systemd den Dienst beendet
    _active_chargers = []  # Wird unten befuellt
    def _graceful_shutdown(signum, frame):
        import sys as _sys
        import os as _os
        logger.info("SIGTERM empfangen - stoppe alle Wallboxen sauber...")
        for c_data in _active_chargers:
            try:
                # Wir beenden den Dienst (Neustart), stoppen aber NICHT physisch die Ladung!
                # Dadurch kann das C++ Backend/Die E3DC Hardware nahtlos weiterladen,
                # und der nachfolgende Start des Python-Dienstes adoptiert den Zustand reibungslos.
                if hasattr(c_data['charger'], 'conn') and c_data['charger'].conn:
                    c_data['charger'].conn.close()
                logger.info(f"WB{c_data['id']} Verbindung getrennt, Laufzeit bleibt unberuehrt.")
            except Exception as _e:
                logger.error(f"Fehler beim Cleanup (SIGTERM): {_e}")
        # os._exit() umgeht haengenden Heartbeat-Thread (daemon=True reicht nicht bei SIGTERM)
        # Kein sys.exit() - das wuerde auf Thread-Cleanup warten und systemd-Timeout ausloesen
        _os._exit(0)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    was_disabled_logged = False
    plan_was_active = False  # Fuer Plan-Ende Erkennung im Automatik-Netz-Modus

    # =========================================================================
    # EMA-Zustand aus letztem Lauf wiederherstellen (verhindert Tick-Tack nach Neustart)
    _aval_restore  = 0.0
    _wbmin_restore = 1380.0
    try:
        import json as _j, os as _os
        _p = '/var/www/html/ramdisk/wallbox_native.json'
        if _os.path.exists(_p) and (time.time() - _os.path.getmtime(_p)) < 120:
            _d = _j.load(open(_p))
            _aval_restore  = float(_d.get('aval_power', 0.0) or 0.0)
            _wbmin_raw     = float(_d.get('wb_min_pwr', 1380.0) or 1380.0)
            # Plausibilitaets-Clamp: wb_min_pwr darf max. 5000W sein (6A*3ph*230V=4140W + Puffer).
            # Hoehere Werte sind Artefakte aus Volllast-Laeufen und wuerden die Min-Leistung falsch setzen!
            _wbmin_restore = max(1380.0, min(_wbmin_raw, 5000.0))
            if _wbmin_raw > 5000.0:
                logger.warning(f"wb_min_pwr Restore: {_wbmin_raw:.0f}W zu hoch (Volllast-Artefakt), auf {_wbmin_restore:.0f}W zurueckgesetzt.")
            if _aval_restore > 0:
                # Clamp auf konservativen Startwert: max. 2x gemessene 6A-Mindestleistung
                # Verhindert: Nach wildem Takt-Zustand EMA=7000W -> sofort 32A -> Grid-Spike -> Stop
                _aval_max = _wbmin_restore * 2.0
                if _aval_restore > _aval_max:
                    logger.info(f"EMA-Restore: aval={_aval_restore:.0f}W auf {_aval_max:.0f}W gedeckelt (konservativer Start)")
                    _aval_restore = _aval_max
                logger.info(f"EMA-Zustand wiederhergestellt: aval={_aval_restore:.0f}W wb_min={_wbmin_restore:.0f}W")
    except Exception:
        pass
    # =========================================================================
    # V5 Fuzzy-Regelung - Persistenter Zustand
    # wb_min_power_meas: selbstlernend, startet bei 6A-Messung
    #   ~1380W = 1-phasig, ~2760W = 2-phasig, ~4140W = 3-phasig
    # =========================================================================
    runtime = WallboxRuntimeState.from_restore(wb_min_power_meas=_wbmin_restore, wb_current_amp=6)
    wb_min_power_meas = runtime.wb_min_power_meas  # Selbstlernend (1ph: ~1380W, 3ph: ~4140W)
    wb_current_amp    = runtime.wb_current_amp     # Aktuell gesetzter Ladestrom

    # Haltezeit nach Deckel-Aenderung (30s: E3DC-EMS braucht Zeit)
    last_change_ts    = {}

    # Safety-Watcher: Netzbezug-Timer
    grid_overshoot_ts = None

    abort_restore_state = _load_wallbox_abort_state()
    phase_restore_state = _load_wallbox_phase_state()
    schedule_service = ScheduleService()
    vehicle_manager = VehicleManager()


    while True:
        try:
            config = get_config()
            enabled = config.get("wb_native_enable", "0")
            if enabled not in ["1", "true"]:
                if not was_disabled_logged:
                    logger.info("Native Regelung ist deaktiviert (wb_native_enable != 1). Melde Freigabe an Storage Manager...")
                    _update_command_gate_context(_active_chargers, native_enabled=False)
                    for c_data in _active_chargers:
                        try:
                            if hasattr(c_data.get('charger'), 'suspend_external_control'):
                                c_data['charger'].suspend_external_control("Native Regelung deaktiviert")
                        except Exception:
                            pass

                    write_storage_intent({
                        "active": False,
                        "battery_request": "release",
                        "reason": "wallbox_native_disabled",
                        "wb_mode_active": 0,
                        "car_active": False,
                        "charging_active": False,
                    })
                        
                    was_disabled_logged = True
                if os.path.exists(STATUS_OUTPUT_FILE):
                    try: os.remove(STATUS_OUTPUT_FILE)
                    except: pass
                time.sleep(30)
                continue
            
            was_disabled_logged = False

            def _sfloat(v, default):
                """Safe float: gibt default zurueck wenn v leer/None/ungueltig ist."""
                try:
                    s = str(v).strip() if v is not None else ''
                    return float(s) if s else default
                except (ValueError, TypeError):
                    return default

            wb_minsoc     = int(_sfloat(config.get("wbminsoc"),       70))
            wb_global_max_amp = _amp_limit(config.get("wbmaxladestrom", config.get("wb_max_amp", 16)), 16)
            wb_max_amp    = _wallbox_global_max_amp(config, wb_global_max_amp)
            grid_max_amps = max(16.0, min(125.0, _sfloat(config.get("grid_max_amps"), 63.0)))
            wb_min_lade_w = max(1380.0, _sfloat(config.get("wbminlade"), 1380))
            live_fresh_guard_s = max(5.0, min(60.0, _sfloat(config.get("wallbox_live_stale_guard_s"), 20.0)))
            _wb_native_mode_raw = int(_sfloat(config.get("wb_native_mode"), 0))
            wb_dist_mode = normalize_distribution_mode(_wb_native_mode_raw)
            wb_legacy_global_mode = normalize_wb_mode(_wb_native_mode_raw) if _wb_native_mode_raw > 2 else MODE_OFF

            # ---------------------------------------------------------------------------
            # AUTO-ERKENNUNG: E3DC-Wallbox via RSCP
            # Wenn kein Typ konfiguriert ist, nutzen wir automatisch den E3DC-Treiber.
            # Bewusst deaktivierte Wallboxen bleiben NGNA: nur beobachten, nie Auto-Detect.
            # Der Nutzer muss KEINE manuelle IP angeben - die E3DC-IP ist schon bekannt.
            # ---------------------------------------------------------------------------
            wb_type_raw = str(config.get("wb_native_type", "")).strip().lower()
            if wb_type_raw in DISABLED_WALLBOX_TYPES:
                logger.info("[WB1] Deaktiviert / Keine Wallbox konfiguriert - keine Auto-Erkennung, keine Steuerung.")
                config["wb_native_type"] = "none"
                config["wb_native_ip"] = ""
            elif not wb_type_raw:
                # Pruefe ob eine E3DC Wallbox im RSCP-Livesignal vorhanden ist
                _e3dc_ip = config.get("server_ip", "")
                if _e3dc_ip:
                    logger.info("[AutoDetect] Kein WB-Typ konfiguriert - erkenne automatisch als 'e3dc' (RSCP).")
                    config["wb_native_type"] = "e3dc"
                    config["wb_native_ip"]   = _e3dc_ip  # E3DC hat keine separate WB-IP - nutzt RSCP

            # openWB Software 2.x: Ladepunkte read-only erkennen. Dadurch muss
            # der Nutzer bei Standalone-/Duo-Setups nicht mehr jeden CP von Hand
            # fehlerfrei eintragen; explizite Konfiguration bleibt Vorrang.
            if str(config.get("wb_native_type", "")).strip().lower() == "openwb":
                _openwb_ip = str(config.get("wb_native_ip", "") or "").strip()
                _openwb_auto = str(config.get("wb_openwb_auto_discovery", "1")).strip().lower() in ("1", "true", "yes", "on")
                if _openwb_ip and _openwb_auto:
                    _detected_cps = discover_openwb_chargepoints(_openwb_ip, timeout=3.0)
                    if _detected_cps:
                        if not str(config.get("wb1_topic_prefix", "") or "").strip() and not str(config.get("wb_native_cp_id", "") or "").strip():
                            config["wb1_topic_prefix"] = f"openWB/simpleAPI/chargepoint/{_detected_cps[0]['id']}"
                            logger.info(
                                f"[AutoDetect] openWB WB1: Ladepunkt {_detected_cps[0]['id']} "
                                f"({_detected_cps[0].get('name') or 'ohne Namen'}) erkannt."
                            )
                        if (
                            len(_detected_cps) >= 2
                            and not str(config.get("wb_native_type2", "") or "").strip()
                            and not str(config.get("wb_native_ip2", "") or "").strip()
                        ):
                            config["wb_native_type2"] = "openwb"
                            config["wb_native_ip2"] = _openwb_ip
                            config["wb2_topic_prefix"] = f"openWB/simpleAPI/chargepoint/{_detected_cps[1]['id']}"
                            logger.info(
                                f"[AutoDetect] openWB WB2: Ladepunkt {_detected_cps[1]['id']} "
                                f"({_detected_cps[1].get('name') or 'ohne Namen'}) automatisch eingebunden."
                            )

            # Initialisiere Wallboxen
            chargers = []
            c1 = create_charger(config.get("wb_native_type", ""), config.get("wb_native_ip", ""), 1, config)
            if c1: chargers.append({'id': 1, 'charger': c1, 'is_charging': False, 'current_set_amp': 0,
                                        'iAvalPower': _aval_restore, 'ramp_amp': 6, 'last_ramp_ts': 0.0,
                                        '_real_charge_since': 0.0,
                                        **_restored_abort_fields(abort_restore_state, 1),
                                        **_restored_phase_fields(phase_restore_state, 1)})

            c2 = create_charger(config.get("wb_native_type2", ""), config.get("wb_native_ip2", ""), 2, config)
            if c2: chargers.append({'id': 2, 'charger': c2, 'is_charging': False, 'current_set_amp': 0,
                                        'iAvalPower': 0.0, 'ramp_amp': 6, 'last_ramp_ts': 0.0,
                                        '_real_charge_since': 0.0,
                                        **_restored_abort_fields(abort_restore_state, 2),
                                        **_restored_phase_fields(phase_restore_state, 2)})

            if not chargers:
                logger.warning("Keine gueltigen Wallboxen parametriert. Pausiere 30s...")
                _active_chargers.clear()
                _update_command_gate_context([], native_enabled=False)
                write_storage_intent({
                    "active": False,
                    "battery_request": "release",
                    "reason": "wallbox_no_configured_charger",
                    "wb_mode_active": 0,
                    "car_active": False,
                    "charging_active": False,
                })
                if os.path.exists(STATUS_OUTPUT_FILE):
                    try: os.remove(STATUS_OUTPUT_FILE)
                    except: pass
                time.sleep(30)
                continue

            _update_command_gate_context(
                chargers,
                {c_data["id"]: wb_legacy_global_mode for c_data in chargers},
                native_enabled=True,
            )
            logger.info(f"Verbunden mit {len(chargers)} Wallbox(en). Modus={wb_dist_mode}. Starte Regel-Schleife.")
            _active_chargers.clear()
            _active_chargers.extend(chargers)

            config_mtime = os.path.getmtime(V4_CONFIG_FILE) if os.path.exists(V4_CONFIG_FILE) else 0

            ema_brain_w = None

            # Regel-Loop
            while True:
                # 0. Initialer Status nur beim allerersten Lauf schreiben.
                # Sonst flackert das Frontend zwischen echtem Zustand und
                # "Initialisiere Regelung...", wenn ein Ladepunkt kurz langsam
                # antwortet.
                if not os.path.exists(STATUS_OUTPUT_FILE):
                    write_status({"connected": False, "status_msg": "Initialisiere Regelung...", "wb_type": f"Multi ({len(chargers)} WB)"})
                
                # Regel-Schleife mit robustem Error-Handling
                try:
                    # Native Ladeplanung auf Basis EPEX-Preisen generieren (Ersatz fuer C++ wallbox.out wenn wallbox=-1)
                    try:
                        schedule_service.refresh(get_config())
                    except Exception as e:
                        logger.error(f"Fehler bei Ladeplanung: {e}")

                    if not os.path.exists(LIVE_DATA_FILE_PY):
                        write_status({
                            "connected": False,
                            "charging_active": False,
                            "status_msg": "Keine Live-Daten: warte auf e3dc-live.",
                            "wb_type": f"Multi ({len(chargers)} WB)",
                            "live_stale": True,
                            "live_age_s": 999999,
                        })
                        time.sleep(5)
                        continue
                        
                    live = read_live_data(max_age_s=live_fresh_guard_s)
                    if not live:
                        _age = live_data_age_s()
                        _age_txt = "unbekannt" if _age >= 999999 else f"{_age:.0f}s"
                        write_status({
                            "connected": False,
                            "charging_active": False,
                            "status_msg": f"Keine frischen Live-Daten ({_age_txt}): Regelung pausiert.",
                            "wb_type": f"Multi ({len(chargers)} WB)",
                            "live_stale": True,
                            "live_age_s": round(float(_age), 1) if math.isfinite(float(_age)) else 999999,
                            "budget_stale": True,
                        })
                        time.sleep(5)
                        continue
                    live_plausibility = live_power_plausibility(live)
                    live_sample_invalid = not bool(live_plausibility.get("sample_valid", True))
                    
                    # Konfiguration zur Laufzeit checken - Nur bei strukturellen Änderungen Neustart erzwingen!
                    if os.path.exists(V4_CONFIG_FILE):
                        curr_mtime = os.path.getmtime(V4_CONFIG_FILE)
                        if config_mtime > 0 and curr_mtime != config_mtime:
                            test_config = get_config()
                            structural_changed = False
                            for key in ["wb_native_type", "wb_native_ip", "wb_native_type2", "wb_native_ip2", "wb_MQTT_Topic"]:
                                if str(test_config.get(key, "")) != str(config.get(key, "")):
                                    structural_changed = True
                                    
                            if structural_changed:
                                import sys
                                logger.info("Strukturaenderung (Netzwerk/WB-Typ) erkannt! Beende Script fuer Neustart...")
                                sys.exit(1)
                            else:
                                if test_config != config:
                                    logger.info("Parameteraenderung erkannt! Uebernehme Einstellungen nahtlos (ohne Neustart).")
                                    config = test_config
                                wb_dist_mode = normalize_distribution_mode(config.get("wb_native_mode", 0))
                                if str(config.get("wb_native_enable", "0")).lower() not in ("1", "true"):
                                    logger.info("Native Regelung wurde deaktiviert - gebe E3DC-Wallbox frei und verlasse Regel-Loop.")
                                    _update_command_gate_context(chargers, native_enabled=False)
                                    for cd in chargers:
                                        if hasattr(cd["charger"], "suspend_external_control"):
                                            cd["charger"].suspend_external_control("Native Regelung deaktiviert")
                                    if os.path.exists(STATUS_OUTPUT_FILE):
                                        try: os.remove(STATUS_OUTPUT_FILE)
                                        except: pass
                                    break
                                
                        config_mtime = curr_mtime

                    # Dynamische Ladeparameter parsen
                    # WICHTIG: wb_charge_mode und wb_locked VOR dem try-Block mit sicheren
                    # Defaults initialisieren. Bei einem Fehler in get_config() wuerde sonst
                    # ein 'cannot access free variable'-Fehler auf allen spaeteren Zugriffen
                    # (L280, L544, L710) auftreten da der try-Block nie L266 erreicht hat.
                    wb_locked      = {cid: False for cid in [1, 2]}
                    wb_manual_pause = {cid: False for cid in [1, 2]}
                    wb_charge_mode = {cid: wb_legacy_global_mode for cid in [1, 2]}
                    dyn_config = {}
                    try:
                        dyn_config = get_config()
                        if "wbminsoc" in dyn_config: wb_minsoc = int(float(dyn_config.get("wbminsoc", 70)))
                        wb_global_max_amp = _amp_limit(dyn_config.get("wbmaxladestrom", dyn_config.get("wb_max_amp", 16)), 16)
                        wb_max_amp = _wallbox_global_max_amp(dyn_config, wb_global_max_amp)
                        if "grid_max_amps" in dyn_config: grid_max_amps = max(16.0, min(125.0, _sfloat(dyn_config.get("grid_max_amps"), 63.0)))
                        if "wallbox_live_stale_guard_s" in dyn_config:
                            live_fresh_guard_s = max(5.0, min(60.0, _sfloat(dyn_config.get("wallbox_live_stale_guard_s"), 20.0)))
                        if "wbminlade" in dyn_config: 
                            wb_min_lade_w = float(dyn_config.get("wbminlade", 1380))
                            if wb_min_lade_w < 1380: wb_min_lade_w = 1380
                        if "wb_native_mode" in dyn_config:
                            _wb_native_mode_raw = int(float(dyn_config.get("wb_native_mode", 0) or 0))
                            wb_dist_mode = normalize_distribution_mode(_wb_native_mode_raw)
                            wb_legacy_global_mode = normalize_wb_mode(_wb_native_mode_raw) if _wb_native_mode_raw > 2 else MODE_OFF
                        
                        # Feature 1 & 2: Manuelles Sperren und Betriebsmodi pro Charger (ID=1 oder 2)
                        # wb_native_mode ist in der Config die Multi-WB-Prioritaet
                        # (0=balanced, 1=WB1, 2=WB2). Alte Installationen konnten
                        # dort noch einen globalen Lademodus >2 speichern; nur
                        # diesen Legacy-Fall nutzen wir als Fallback.
                        for cid in [1, 2]:
                            l_v = dyn_config.get(f"wb{cid}_locked")
                            m_v = dyn_config.get(f"wb{cid}_mode")
                            wb_locked[cid] = bool(int(l_v if l_v is not None else 0))
                            wb_manual_pause[cid] = _truthy_config(dyn_config.get(f"wb{cid}_manual_pause", 0))
                            if m_v is not None and str(m_v).strip() != "":
                                # Per-WB Modus explizit gesetzt (z.B. via Wallbox.php Button)
                                wb_charge_mode[cid] = normalize_wb_mode(m_v)
                            else:
                                wb_charge_mode[cid] = wb_legacy_global_mode
                    except Exception as _cfg_e:
                        logger.debug(f"Ladeparameter-Parse Fehler (verwende Defaults): {_cfg_e}")

                    _effective_wb_locked = dict(wb_locked)
                    for _cid, _paused in wb_manual_pause.items():
                        if _paused:
                            _effective_wb_locked[_cid] = True

                    _update_command_gate_context(
                        chargers,
                        wb_charge_mode,
                        _effective_wb_locked,
                        native_enabled=True,
                    )
                    
                    # Effektiver Modus: nur aktiv konfigurierte Charger beruecksichtigen!
                    # BUG-FIX: max([1,2]) nimmt wb2_mode=10 auch wenn WB2 gar nicht konfiguriert.
                    # -> max(9, 10) = 10 -> Mode 9 Code wird nie erreicht!
                    # Jetzt: nur Charger-IDs die tatsaechlich ein Charger-Objekt haben.
                    _active_ids = [
                        _cd['id']
                        for _cd in chargers
                        if not bool(wb_manual_pause.get(_cd.get('id'), False))
                    ] if chargers else [1]
                    if not _active_ids:
                        _active_ids = [_cd['id'] for _cd in chargers] if chargers else [1]
                    effective_public_wb_mode = _select_effective_public_wb_mode(wb_charge_mode, _active_ids)
                    effective_wb_mode = controller_mode(effective_public_wb_mode, grid_allowed=False)
                    price_optimizing_active = False


                    # 1. Sammle Status aller Wallboxen
                    system_connected = False
                    charging_active_any = False
                    valid_chargers_status = []
                    
                    total_current_wb_consumption = 0

                    # Per-cycle identity for the single normal output edge.
                    for _output_data in chargers:
                        _output_data["_hardware_output_cycle_token"] = time.time()
                    
                    for c_data in chargers:
                        c = c_data['charger']
                        c_id = c_data['id']
                        _status_now = time.time()
                        _previous_wallbox_status = (
                            dict(c_data.get("last_valid"))
                            if isinstance(c_data.get("last_valid"), dict)
                            else {}
                        )
                        try:
                            _raw_status = c.get_status()
                        except Exception as _status_e:
                            _raw_status = None
                            logger.warning(f"[WB{c_id}] Treiberstatus nicht lesbar: {_status_e}")
                        st = _wallbox_driver_status_or_stale(
                            c_data,
                            _raw_status,
                            now_ts=_status_now,
                            stale_guard_s=_wallbox_status_stale_guard_s(config),
                        )

                        # openWB: Status in RAM-Disk schreiben (openwb_data.json)
                        if hasattr(c, 'write_openwb_status'):
                            try:
                                c.write_openwb_status()
                            except Exception:
                                pass

                        if st:
                            _observe_wallbox_phase_transition(
                                c_data,
                                _previous_wallbox_status,
                                st,
                                now_ts=_status_now,
                            )
                            _charge_contract = _wallbox_charge_observation_contract(st, c_data, now_ts=time.time())
                            _apply_charge_contract_to_status(st, _charge_contract)
                            # Beim Modul-Start oder Neustart den echten Ladestatus aus der Hardware uebernehmen!
                            # Ansonsten denkt Python, die Wallbox waere aus, obwohl sie real laedt (was Timer unwirksam macht!)
                            if not c_data.get('state_synced', False):
                                c_data['is_charging'] = _wb_status_real_charging(st)
                                if c_data['is_charging']:
                                    sync_amp = int(st.get('amp', 0) or 0)
                                    try:
                                        pha_sync = st.get('pha', 0)
                                        sync_phases = 3 if pha_sync == 56 else (1 if pha_sync in [8, 16, 32] else 3)
                                        sync_power = float(st.get('real_power_w', 0) or 0)
                                        if sync_power > 500:
                                            derived_amp = int(max(6, min(round(sync_power / 230.0 / sync_phases), wb_max_amp)))
                                            if derived_amp > sync_amp:
                                                sync_amp = derived_amp
                                    except Exception:
                                        pass
                                    c_data['current_set_amp'] = sync_amp if sync_amp > 0 else 6
                                else:
                                    c_data['current_set_amp'] = 0
                                c_data['state_synced'] = True
                        if st and (wb_locked.get(c_id, False) or wb_manual_pause.get(c_id, False)):
                            st['locked'] = True
                        elif st:
                            st['locked'] = False

                        if st and not isinstance(st.get("charge_contract"), dict):
                            _charge_contract = _wallbox_charge_observation_contract(st, c_data, now_ts=time.time())
                            _apply_charge_contract_to_status(st, _charge_contract)

                        if st:
                            try:
                                vehicle_manager.update_status(
                                    c_id,
                                    config,
                                    st,
                                    charger_class=c.__class__.__name__,
                                    write_status=(
                                        c.write_openwb_status
                                        if hasattr(c, "write_openwb_status")
                                        else None
                                    ),
                                )
                            except Exception as _soc_e:
                                logger.debug(f"[WB{c_id}] Fahrzeug-SoC-Tracker Fehler: {_soc_e}")

                        if st and c_data.get('is_charging', False):
                            # Hardware Glitch Protection: Wenn Python glaubt, wir laden gerade,
                            # darf Ein kurzer RSCP-Aussetzer ('Kein Auto') nicht zum sofortigen Abbruch fuehren.
                            # Wir patchen den Status solange, bis die 20s Grace Period greift.
                            if st.get('car', 1) < 2 and _wb_status_real_charging(st):
                                st['car'] = 2
                            
                        if st:
                            valid_chargers_status.append({'id': c_data['id'], 'charger': c, 'status': st})
                            if _wb_status_connected(st):
                                system_connected = True
                                
                            # Berechne aktuellen Verbrauch dieser Wallbox
                            pha_est = st.get('pha', 0) if st else 0
                            phases_status = int(st.get('phases_in_use', 0) or 0) if st else 0
                            phases = (
                                phases_status
                                if 1 <= phases_status <= 3
                                else (3 if pha_est == 56 else (2 if pha_est == 24 else (1 if pha_est in [8, 16, 32] else 3)))
                            )
                            is_multi_direct_status = (
                                getattr(c_data.get("charger"), "driver_variant", "") == "e3dc_multi_connect"
                            )
                            phase_power_sum_status = float(st.get('phase_power_sum_w', 0.0) or 0.0)
                            phase_power_verified = bool(st.get('phase_power_verified', False))
                            
                            # wb_power immer initialisieren - verhindert UnboundLocalError beim Abstecken!
                            wb_power = 0.0

                            if _wb_status_real_power(st) > 50:
                                # Plausibilitaets-Check: E3DC liefert nach physischem Abbruch
                                # noch den alten stale Wert (z.B. 22080W = 32A*3ph*230V).
                                # Wenn charging=False → Wert ist Artefakt, nicht echte Leistung!
                                _is_hw_charging = _wb_status_real_charging(st)
                                _rp = _wb_status_real_power(st)
                                if not _is_hw_charging:
                                    # Stale RSCP-/Multi-Phantom: Ohne echtes
                                    # Ladebit ist das keine belastbare Leistung.
                                    # Kleine Rest-/Standbywerte werten wir
                                    # bewusst nicht als aktive Ladung.
                                    wb_power = 0.0
                                    c_data['_real_charge_since'] = 0.0
                                else:
                                    # Echte Messung vorhanden und plausibel (> 50W)
                                    wb_power = _rp
                                    charging_active_any = True
                                    if _rp > 500:
                                        _real_since = float(c_data.get('_real_charge_since', 0.0) or 0.0)
                                        _now_real = time.time()
                                        try:
                                            _aha_confirm_cfg = float(
                                                str(config.get("wb_aha_real_charge_confirm_s", 120)).strip() or 120
                                            )
                                        except Exception:
                                            _aha_confirm_cfg = 120.0
                                        _aha_confirm_s = max(
                                            60.0,
                                            _aha_confirm_cfg,
                                        )
                                        c_data["_manager_zero_anchor_active"] = False
                                        c_data["_openwb_start_reject_anchor_ts"] = 0.0
                                        if _real_since <= 0.0:
                                            c_data['_real_charge_since'] = _now_real
                                            c_data["_aha_real_charge_confirmed"] = False
                                            c_data["_aha_real_charge_confirmed_since"] = 0.0
                                        elif _now_real - _real_since >= _aha_confirm_s:
                                            c_data["_aha_real_charge_confirmed"] = True
                                            if float(c_data.get("_aha_real_charge_confirmed_since", 0.0) or 0.0) <= 0.0:
                                                c_data["_aha_real_charge_confirmed_since"] = _now_real
                                        if (
                                            c_data.get("charger").__class__.__name__ == "OpenWBProCharger"
                                            and (c_data.get('abort_count', 0) or c_data.get('abort_cooldown_ts', 0) or c_data.get('_bev_full_blocked', False))
                                        ):
                                            # Die openWB Pro pausiert beim Phasenwechsel
                                            # selbst. Wenn danach wieder echte
                                            # Leistung gemessen wird, war das kein
                                            # BEV-voll-Abbruch.
                                            c_data['abort_count'] = 0
                                            c_data['abort_cooldown_ts'] = 0.0
                                            c_data['_bev_full_blocked'] = False
                                            c_data['_bev_full_block_reason'] = ""
                                            c_data["_openwb_start_reject_soft_until"] = 0.0
                                            _save_wallbox_abort_state(chargers)
                                        elif str(c_data.get("_bev_full_block_reason") or "") == "start_rejected_soft":
                                            c_data["_bev_full_block_reason"] = ""
                                            c_data["_openwb_start_reject_soft_until"] = 0.0
                                        elif _now_real - _real_since >= 180.0:
                                            if c_data.get('abort_count', 0) or c_data.get('abort_cooldown_ts', 0) or c_data.get('_bev_full_blocked', False):
                                                c_data['abort_count'] = 0
                                                c_data['abort_cooldown_ts'] = 0.0
                                                c_data['_bev_full_blocked'] = False
                                                _save_wallbox_abort_state(chargers)
                                
                                # STARTUP-ADOPTION:
                                if not c_data.get('state_synced', False):
                                    c_data['is_charging'] = _is_hw_charging
                                    # Clamp auf wb_max_amp um 22kW Phantomwerte (32A*3ph*230V) zu verhindern!
                                    adopted_amp = int(max(6, min(wb_power / 230.0 / phases, wb_max_amp))) if _is_hw_charging else 0
                                    c_data['current_set_amp'] = adopted_amp
                                    c_data['last_start_ts'] = time.time()
                                    if _is_hw_charging:
                                        logger.info(f"Adoptiere laufenden Ladevorgang von WB{c_id} mit {c_data['current_set_amp']}A")
                                    
                            elif c_data['is_charging'] and c_data['current_set_amp'] > 0:
                                # RSCP-Glitch (0W oder None): Schätzung aus eingestelltem Strom
                                # Besser als 0W annehmen, was einen Phantom-Überschuss erzeugt!
                                # ABER NUR wenn Auto noch physisch verbunden ist!
                                # Beim Abstecken (car < 2) SOFORT auf 0 -- kein Glitch-Filter!
                                car_connected = st.get('car', 1) >= 2
                                recently_changed = car_connected and (time.time() - last_change_ts.get(c_id, 0)) < 12
                                # Sicherheits-Clamp: verhindert 22kW Phantomwerte (32A*3ph*230V=22080W)!
                                is_external_charger = c_data["charger"].__class__.__name__ in (
                                    "OpenWBCharger",
                                    "OpenWBProCharger",
                                    "GoECharger",
                                )
                                clamped_amp = min(c_data['current_set_amp'], wb_max_amp)
                                if is_external_charger:
                                    wb_power = 0.0
                                    charging_active_any = bool(st.get('charging', False))
                                elif st.get('charging', False):
                                    wb_power = clamped_amp * 230.0 * phases
                                    charging_active_any = True
                                elif recently_changed and not is_multi_direct_status:
                                    wb_power = clamped_amp * 230.0 * phases
                                    charging_active_any = True
                                elif recently_changed and is_multi_direct_status and phase_power_verified:
                                    # Multi Connect: Phasenwerte bestaetigen
                                    # echte Leistung waehrend ALG noch anlaufen
                                    # kann. Ohne Phasenverifikation bleibt 0W.
                                    wb_power = phase_power_sum_status
                                    charging_active_any = True
                                elif recently_changed and is_multi_direct_status:
                                    wb_power = 0.0
                                else:
                                    wb_power = 0.0
                                    if not c_data.get('state_synced', False):
                                        c_data['is_charging'] = False
                                        c_data['current_set_amp'] = 0

                            c_data['state_synced'] = True

                            # --- FIX: Silent Wallbox Drops (Auto voll / laedt nicht mehr) ---
                            # Wenn Python denkt wir laden, aber das Hardware-Flag aus ist:
                            # Hardware braucht bis zu 15 Sekunden, um nach dem Startbefehl auf "charging=True" zu springen!
                            # Daher geben wir ihm eine 20s Grace-Period, bevor wir auf "Abbruch" entscheiden.
                            time_since_start = time.time() - c_data.get('last_start_ts', 0)
                            _public_mode_for_cd = normalize_wb_mode(wb_charge_mode.get(c_id, effective_public_wb_mode))
                            _mode_for_cd = controller_mode(_public_mode_for_cd, grid_allowed=False)
                            _drop_observe_only = (
                                _public_mode_for_cd == MODE_OFF
                            )
                            _openwb_mode9_monitor = (
                                _mode_for_cd == 9
                                and c_data.get("charger").__class__.__name__ == "OpenWBCharger"
                                and not price_optimizing_active
                            )
                            _is_e3dc_multi_direct_drop = (
                                getattr(c_data.get("charger"), "driver_variant", "") == "e3dc_multi_connect"
                            )
                            _startup_grace_s = 60 if _is_e3dc_multi_direct_drop else 20
                            _drop_charger_class = c_data.get("charger").__class__.__name__
                            _drop_openwb_like = _drop_charger_class in ("OpenWBCharger", "OpenWBProCharger")
                            _drop_openwb_pro = _drop_charger_class == "OpenWBProCharger"
                            _drop_hw_offered_amp = int(float(st.get("amp", 0) or 0))
                            if _drop_openwb_like:
                                _startup_grace_s = max(_startup_grace_s, 90 if _drop_openwb_pro else 60)
                            _openwb_pro_phase_transition = False
                            _phase_switch_transition = False
                            try:
                                _phase_switch_transition = _wallbox_phase_transition_grace_active(
                                    c_data,
                                    st,
                                    now_ts=time.time(),
                                    grace_s=360.0,
                                )
                                _openwb_pro_phase_transition = (
                                    c_data.get("charger").__class__.__name__ == "OpenWBProCharger"
                                    and int(st.get("phases_target", 0) or 0) in (1, 3)
                                    and int(st.get("phases_in_use", 0) or 0) != int(st.get("phases_target", 0) or 0)
                                    and (time.time() - float(c_data.get("_last_phase_switch_ts", 0.0) or 0.0)) < 360.0
                                )
                            except Exception:
                                _phase_switch_transition = False
                                _openwb_pro_phase_transition = False
                            _had_real_charge_before_drop = bool(
                                c_data.get("_aha_real_charge_confirmed", False)
                            )
                            _drop_now_ts = time.time()
                            _recent_manager_stop = (
                                _drop_now_ts - float(c_data.get("_last_manager_stop_request_ts", 0.0) or 0.0)
                                < max(90.0, float(_startup_grace_s))
                            )
                            _manager_zero_anchor_active = bool(c_data.get("_manager_zero_anchor_active", False))
                            _recent_manager_start_retry = (
                                _drop_now_ts - float(c_data.get("_native_last_start_retry_ts", 0.0) or 0.0)
                                < max(30.0, min(90.0, float(_startup_grace_s)))
                            )
                            _drop_min_amp = int(_cfg_float(config.get("wbminladestrom", 6), 6.0))
                            _charge_end_drop_preconditions = bool(
                                c_data['is_charging'] and not _wb_status_real_charging(st)
                                and time_since_start > _startup_grace_s
                                and int(c_data.get('current_set_amp', 0) or 0) > 0
                                and not _drop_observe_only
                                and not (_drop_charger_class == "OpenWBCharger" and _drop_hw_offered_amp < 5)
                                and not c_data.get("_wb_stop_sent_active", False)
                                and not _manager_zero_anchor_active
                                and not _recent_manager_stop
                                and not _recent_manager_start_retry
                                and not _openwb_mode9_monitor
                                and not _phase_switch_transition
                                and not _openwb_pro_phase_transition
                            )
                            if _drop_openwb_pro:
                                _openwb_pro_finished_contract = openwb_pro_session.vehicle_finished_drop_contract(
                                    st,
                                    c_data,
                                    is_manager_charging=bool(c_data.get('is_charging', False)),
                                    current_set_amp=c_data.get('current_set_amp', 0),
                                    time_since_start_s=time_since_start,
                                    startup_grace_s=_startup_grace_s,
                                    min_amp=_drop_min_amp,
                                    had_confirmed_charge=_had_real_charge_before_drop,
                                    observe_only=_drop_observe_only,
                                    openwb_mode9_monitor=_openwb_mode9_monitor,
                                    stop_sent_active=bool(c_data.get("_wb_stop_sent_active", False)),
                                    manager_zero_anchor_active=_manager_zero_anchor_active,
                                    recent_manager_stop=_recent_manager_stop,
                                    recent_manager_start_retry=_recent_manager_start_retry,
                                    phase_transition_active=_phase_switch_transition,
                                    openwb_pro_phase_transition_active=_openwb_pro_phase_transition,
                                    now_ts=_drop_now_ts,
                                )
                                c_data["_openwb_pro_vehicle_finished_contract"] = _openwb_pro_finished_contract
                                openwb_pro_session.apply_vehicle_finished_drop_to_status(
                                    st,
                                    _openwb_pro_finished_contract,
                                )
                                _charge_end_drop_preconditions = bool(
                                    _openwb_pro_finished_contract.get("allow_new_latch", False)
                                )
                            _charge_end_contract = {}
                            if _charge_end_drop_preconditions:
                                _charge_end_contract = _wallbox_charge_end_latch_contract(
                                    c_data,
                                    st,
                                    now_ts=_drop_now_ts,
                                    config=config,
                                    charger_id=c_id,
                                    public_mode=_public_mode_for_cd,
                                    had_confirmed_charge=_had_real_charge_before_drop,
                                    allow_new_latch=True,
                                    start_verifying=False,
                                    manager_stop_active=False,
                                    grace_active=False,
                                )
                            if (
                                _charge_end_drop_preconditions
                                and str(_charge_end_contract.get("action", "") or "") == "latch"
                            ):
                                _charge_end_reason = str(
                                    _charge_end_contract.get("reason") or "vehicle_charge_ended"
                                )
                                c_data['abort_count'] = c_data.get('abort_count', 0) + 1
                                c_data['is_charging'] = False
                                c_data['current_set_amp'] = 0
                                c_data['_real_charge_since'] = 0.0
                                c_data['abort_cooldown_ts'] = time.time()
                                _send_wallbox_stop_command(
                                    c_data,
                                    c_id=c_id,
                                    reason=_charge_end_reason,
                                )
                                c_data['_wb_stop_sent_active'] = True
                                c_data['_last_stop_toggle_ts'] = time.time()
                                c_data['_openwb_zero_budget_since'] = 0.0
                                c_data['_native_multi_zero_budget_since'] = 0.0
                                c_data['_native_multi_start_grace_until'] = 0.0
                                c_data['_openwb_pro_start_hold_until'] = 0.0
                                c_data['_openwb_pro_start_hold_amp'] = 0
                                c_data['_openwb_start_retry_count'] = 0
                                c_data['_openwb_cp_start_sent'] = False
                                c_data['_wb_stable_budget_jump_done'] = False
                                logger.warning(
                                    "WB%d Wallbox/Fahrzeug hat die Ladung beendet (Modus=%s) - "
                                    "Start bleibt bis zum naechsten Steckvorgang gesperrt." %
                                    (c_id, mode_label(_public_mode_for_cd))
                                )
                                _save_wallbox_abort_state(chargers)
                                continue

                            total_current_wb_consumption += wb_power

                        else:
                            # Kein Status und kein last_valid: kein Messwert.
                            # current_set_amp ist nur ein Sollwert und darf
                            # keine Phantomladung fuer UI/Storage erzeugen.
                            c_data['is_charging'] = False
                            c_data['current_set_amp'] = 0
                            c_data['_real_charge_since'] = 0.0
                            valid_chargers_status.append({'id': c_data['id'], 'charger': c, 'status': None})

                    # Harte Messwert-Grenze fuer die weitere Regelung:
                    # Alles ab hier basiert nur noch auf Hardware-Status und
                    # gemessener Leistung. Sollstrom/current_set_amp darf keine
                    # Phantomladung fuer Speicher, Hausverbrauch oder UI erzeugen.
                    system_connected = False
                    charging_active_any = False
                    total_current_wb_consumption = 0.0
                    for _v in valid_chargers_status:
                        _st = _v.get('status')
                        if _wb_status_connected(_st):
                            system_connected = True
                        if _wb_status_real_charging(_st):
                            _p = _wb_status_real_power(_st)
                            total_current_wb_consumption += _p
                            charging_active_any = True
                            for _cd in chargers:
                                if _cd.get('id') == _v.get('id'):
                                    _cd['is_charging'] = True
                                    _amp = int((_st or {}).get('amp', 0) or 0)
                                    if _amp > 0:
                                        _cd['current_set_amp'] = min(_amp, wb_max_amp)
                                    break
                        else:
                            for _cd in chargers:
                                if _cd.get('id') == _v.get('id') and not _wb_status_connected(_st):
                                    _cd['is_charging'] = False
                                    _cd['current_set_amp'] = 0
                                    _cd['_real_charge_since'] = 0.0
                                    _cd['_aha_real_charge_confirmed'] = False
                                    _cd['_aha_real_charge_confirmed_since'] = 0.0
                                    _cd['_openwb_start_reject_soft_until'] = 0.0
                                    _cd['_manager_zero_anchor_active'] = False
                                    if _cd.get('abort_count', 0) or _cd.get('abort_cooldown_ts', 0) or _cd.get('_bev_full_blocked', False):
                                        _cd['abort_count'] = 0
                                        _cd['abort_cooldown_ts'] = 0.0
                                        _cd['_bev_full_blocked'] = False
                                        _cd['_bev_full_block_reason'] = ""
                                        _save_wallbox_abort_state(chargers)
                                    break

                    if _auto_clear_manual_pause_after_unplug(wb_manual_pause, valid_chargers_status, dyn_config):
                        for _v in valid_chargers_status:
                            _st = _v.get('status')
                            if isinstance(_st, dict):
                                _cid = _v.get('id')
                                _st['locked'] = bool(wb_locked.get(_cid, False) or wb_manual_pause.get(_cid, False))
                        _effective_wb_locked = dict(wb_locked)
                        for _cid, _paused in wb_manual_pause.items():
                            if _paused:
                                _effective_wb_locked[_cid] = True
                        _update_command_gate_context(
                            chargers,
                            wb_charge_mode,
                            _effective_wb_locked,
                            native_enabled=True,
                        )
                        _active_ids = [
                            _cd['id']
                            for _cd in chargers
                            if not bool(wb_manual_pause.get(_cd.get('id'), False))
                        ] if chargers else [1]
                        if not _active_ids:
                            _active_ids = [_cd['id'] for _cd in chargers] if chargers else [1]
                        effective_public_wb_mode = _select_effective_public_wb_mode(wb_charge_mode, _active_ids)
                        effective_wb_mode = controller_mode(effective_public_wb_mode, grid_allowed=False)

                    e3dc_live_wb_fallback_w = 0.0
                    try:
                        e3dc_live_wb_fallback_w = abs(float(live.get("Wallbox_Power", 0.0) or 0.0))
                    except Exception:
                        e3dc_live_wb_fallback_w = 0.0
                    e3dc_multi_live_fallback_active = False
                    if not charging_active_any and e3dc_live_wb_fallback_w > 500.0:
                        for _v in valid_chargers_status:
                            _cd = next((cd for cd in chargers if cd.get("id") == _v.get("id")), None)
                            if not _cd or getattr(_cd.get("charger"), "driver_variant", "") != "e3dc_multi_connect":
                                continue
                            _st = _v.get("status") if isinstance(_v.get("status"), dict) else {}
                            _st = dict(_st)
                            _st.update({
                                "plug_state": True,
                                "charging": True,
                                "charge_state": True,
                                "power_w": e3dc_live_wb_fallback_w,
                                "real_power_w": e3dc_live_wb_fallback_w,
                                "e3dc_live_power_fallback": True,
                                "state_text": "E3DC Live-Fallback",
                            })
                            if float(_st.get("phase_power_sum_w", 0.0) or 0.0) <= 50.0:
                                _st["phase_power_sum_w"] = e3dc_live_wb_fallback_w
                            _v["status"] = _st
                            _cd["is_charging"] = True
                            if int(_cd.get("current_set_amp", 0) or 0) <= 0:
                                _cd["current_set_amp"] = max(6, min(wb_max_amp, int(round(e3dc_live_wb_fallback_w / 230.0))))
                            system_connected = True
                            charging_active_any = True
                            total_current_wb_consumption = max(total_current_wb_consumption, e3dc_live_wb_fallback_w)
                            e3dc_multi_live_fallback_active = True
                            break

                    # Globaler UI Status (Basis). Die Anzeige nutzt nur die
                    # oeffentlichen Modi; alte C++-Nummern bleiben intern.
                    _mode_label = mode_label(effective_public_wb_mode)
                    ui_state = {
                        "connected": system_connected,
                        "charging_active": charging_active_any,
                        "wb_type": f"Multi ({len(chargers)} WB)",
                        "total_power_w": total_current_wb_consumption,
                        "status_msg": _mode_label,
                        "set_amp": 0,
                        "wb_mode": effective_public_wb_mode,
                        "wb_mode_label": _mode_label,
                        "wb_rule_path": effective_wb_mode,
                        "wb_control_mode": effective_wb_mode,
                    }
                    if e3dc_multi_live_fallback_active:
                        ui_state["wb_power_source"] = "e3dc_live_fallback"
                        ui_state["status_msg"] = f"{_mode_label} (E3DC Live-Fallback)"

                    # WebUI NOT-AUS: absoluter Vorrang vor jeder PV-/Preis-/Fuzzy-Regelung.
                    # Das Flag wird von Wallbox.php gesetzt. Der Manager bleibt aktiv und
                    # haelt STOP, bis der Nutzer den Ladepunkt wieder freigibt.
                    emergency_stop_file = os.path.join(RAMDISK_DIR, "wallbox_emergency_stop.flag")
                    if os.path.exists(emergency_stop_file):
                        for cd in chargers:
                            c_id = cd.get("id", 0)
                            charger = cd.get("charger")
                            try:
                                if hasattr(charger, "emergency_stop"):
                                    _execute_wallbox_driver_command(
                                        cd,
                                        {
                                            "method": "emergency_stop",
                                            "reason": "emergency_stop",
                                        },
                                        c_id=c_id,
                                    )
                                else:
                                    _send_wallbox_stop_command(cd, c_id=c_id, reason="emergency_stop")
                            except Exception as _stop_e:
                                logger.warning("WB%d NOT-AUS Stop fehlgeschlagen: %s" % (c_id, _stop_e))
                            cd["current_set_amp"] = 0
                            cd["is_charging"] = False
                            cd["_pv_mode_active"] = False
                            cd["_wb_stop_sent_active"] = True
                            cd["_predump_gate_stop_sent"] = True
                        ui_state.update({
                            "connected": system_connected,
                            "charging_active": False,
                            "total_power_w": 0,
                            "set_amp": 0,
                            "cap_amp": 0,
                            "status_msg": "NOT-AUS aktiv",
                            "operator_hint": "NOT-AUS aktiv: alle Ladepunkte bleiben gesperrt, bis der Nutzer wieder freigibt.",
                            "operator_hint_level": "danger",
                            "operator_hint_code": "emergency_stop",
                            "reason": "WebUI NOT-AUS: alle Ladepunkte gesperrt",
                            "locked": True,
                        })
                        write_status(ui_state)
                        time.sleep(5)
                        continue

                    # ================================================================
                    # ================================================================
                    # V5 FUZZY-REGELUNG (Sonnenmodus + SOC-Hysterese)
                    # E3DC EMS regelt Grid-Uebergangspunkt autonom.
                    # Python setzt nur Ampere-Deckel (WBchar6[1]) basierend auf
                    # SOC-Abstand zur Ladekurve (Fuzzy) + StoragePlan free_for_limbs_w.
                    # ================================================================
                    def get_float(d, key, default=0.0):
                        v = d.get(key, default)
                        if v is None: return default
                        try: return float(v)
                        except: return default

                    def read_recent_json(path, max_age_s=20.0):
                        try:
                            if not os.path.exists(path):
                                return {}
                            if time.time() - os.path.getmtime(path) > max_age_s:
                                return {}
                            with open(path, encoding='utf-8') as _jf:
                                data = json.load(_jf)
                            return data if isinstance(data, dict) else {}
                        except Exception:
                            return {}

                    grid_power_raw    = get_float(live, "Grid_Power")
                    try:
                        grid_raw_override_w = max(0.0, float(config.get("wb_grid_raw_override_w", 3500) or 3500))
                    except (TypeError, ValueError):
                        grid_raw_override_w = 3500.0
                    grid_power_budget_w = _wallbox_grid_power_for_budget(
                        live,
                        grid_power_raw,
                        grid_raw_override_w,
                    )
                    battery_soc       = get_float(live, "SOC")
                    battery_power_raw = get_float(live, "Battery_Power")
                    pv_power_raw      = get_float(live, "PV_Power")
                    live_wallbox_power_raw = get_float(live, "Wallbox_Power", 0.0)
                    wb_actual_power   = live_wallbox_power_raw
                    wb_phase_powers   = [
                        abs(get_float(live, "wb_p1", 0.0)),
                        abs(get_float(live, "wb_p2", 0.0)),
                        abs(get_float(live, "wb_p3", 0.0)),
                    ]
                    wb_phase_count_live = sum(1 for _p in wb_phase_powers if _p > 250.0)
                    ext_pv_power_raw  = get_float(live, "Ext_PV_Power", 0.0)
                    ac_power_limit_live_w = get_float(live, "ac_power_limit_w", 0.0)
                    wb_power_source = "live_data"
                    native_type_1 = str(config.get("wb_native_type", "")).strip().lower()
                    native_type_2 = str(config.get("wb_native_type2", "")).strip().lower()
                    multi_connect_configured = (
                        native_type_1 == "e3dc_multi"
                        or native_type_2 == "e3dc_multi"
                    )
                    if multi_connect_configured:
                        # E3DC Multi Connect liefert im Live-Backend gelegentlich
                        # stale Summenwerte. Primaer zaehlen deshalb die direkt
                        # abgefragten RSCP-Status-/Phasenwerte. Wenn diese keinen
                        # Ladepunkt durchreichen, aber der E3DC-Livekanal frisch
                        # echte Wallboxleistung meldet, nutzen wir das nur als
                        # Messwert-Fallback, nicht als Steckstatus-Erfindung bei 0 W.
                        if charging_active_any and abs(live_wallbox_power_raw) > 500.0:
                            wb_actual_power = abs(live_wallbox_power_raw)
                            wb_power_source = "e3dc_live_fallback"
                        else:
                            wb_actual_power = 0.0
                            wb_power_source = "multi_rscp"

                    for _wb_path, _wb_key, _wb_label in (
                        (os.path.join(RAMDISK_DIR, "wallbox_native.json"), "total_power_w", "wallbox_native"),
                        (os.path.join(RAMDISK_DIR, "openwb_data.json"), "power_w", "openwb_http"),
                    ):
                        if multi_connect_configured and _wb_label != "wallbox_native":
                            continue
                        _wb_data = read_recent_json(_wb_path)
                        _wb_power = get_float(_wb_data, _wb_key, 0.0)
                        if _wb_label == "wallbox_native":
                            # Schutz gegen stale Multi-Connect Summenwerte:
                            # Wenn laut Status kein Fahrzeug verbunden ist und
                            # die WB-Details kein aktives Laden zeigen, darf
                            # total_power_w nicht als echte Last zaehlen.
                            _wb_native_charging = bool(_wb_data.get("charging_active", False))
                            _wb_details = _wb_data.get("wb_details") or []
                            _wb_detail_active = False
                            for _d in _wb_details:
                                try:
                                    _st = str(_d.get("state", "")).strip().lower()
                                    _pp = abs(float(_d.get("phase_power_sum_w", 0.0) or 0.0))
                                    _ch = bool(_d.get("charging", False))
                                except Exception:
                                    _st = ""
                                    _pp = 0.0
                                    _ch = False
                                if _ch or _st in ("lade", "charging") or _pp > 500.0:
                                    _wb_detail_active = True
                                    break
                            if (not _wb_native_charging) and (not _wb_detail_active):
                                _wb_power = 0.0
                        if abs(_wb_power) > abs(wb_actual_power):
                            wb_actual_power = _wb_power
                            wb_power_source = _wb_label

                    if not charging_active_any:
                        wb_actual_power = 0.0
                        wb_power_source = "status_not_charging"

                    # --- Config-Parameter ---
                    def _sf(v, d):
                        try: s = str(v).strip(); return float(s) if s else d
                        except: return d
                    wb_minsoc_cfg  = _sf(config.get("wbminsoc", 20), 20.0)
                    wb_global_max_amp = _amp_limit(config.get("wbmaxladestrom", config.get("wb_max_amp", 16)), 16)
                    wb_max_amp     = _wallbox_global_max_amp(config, wb_global_max_amp)
                    grid_max_amps  = max(16.0, min(125.0, _sf(config.get("grid_max_amps", grid_max_amps), grid_max_amps)))
                    wb_hardware_max_amp = wb_max_amp
                    wb_soc_hyst_pct = _sf(config.get("wb_soc_hysterese_pct", config.get("wb_hysterese_pct", 0.7)), 0.7)
                    wb_soc_hyst_pct = max(0.1, min(2.0, wb_soc_hyst_pct))
                    # EcoScore Schwellen: konfigurierbar in e3dc_v4.json
                    eco_grid_score = _sf(config.get("eco_grid_score", 90), 90.0)
                    eco_pv_score   = _sf(config.get("eco_pv_score",   50), 50.0)

                    # --- Phasen-Erkennung (automatisch aus wb_min_power_meas) ---
                    # wb_min_power_meas: selbstlernend bei 6A-Ladung
                    #  1-phasig: ~1380W  2-phasig: ~2760W  3-phasig: ~4140W
                    if wb_min_power_meas > 3200:
                        detected_phases = 3
                    elif wb_min_power_meas > 2000:
                        detected_phases = 2
                    else:
                        detected_phases = 1
                    if multi_connect_configured:
                        try:
                            for _cd in chargers:
                                _st = _cd.get("last_valid") or {}
                                if getattr(_cd.get("charger"), "driver_variant", "") != "e3dc_multi_connect":
                                    continue
                                _native_phases_now = int(_st.get("phases_in_use", 0) or 0)
                                if 1 <= _native_phases_now <= 3:
                                    detected_phases = _native_phases_now
                                    break
                        except Exception:
                            pass
                    try:
                        _openwb_phase_data = read_recent_json(os.path.join(RAMDISK_DIR, "openwb_data.json"))
                        _openwb_phases = int(_openwb_phase_data.get("phases_in_use", 0) or 0)
                        if 1 <= _openwb_phases <= 3:
                            detected_phases = _openwb_phases
                    except Exception:
                        pass
                    try:
                        _native_phase_data = read_recent_json(os.path.join(RAMDISK_DIR, "wallbox_native.json"))
                        for _d in (_native_phase_data.get("wb_details") or []):
                            _active = bool(_d.get("charging", False))
                            _active = _active or str(_d.get("state", "")).strip().lower() in ("lade", "charging")
                            _active = _active or abs(float(_d.get("phase_power_sum_w", 0.0) or 0.0)) > 500.0
                            if not _active:
                                continue
                            _native_phases = int(
                                _d.get("phases_in_use")
                                or _d.get("phases_actual")
                                or _d.get("phases_target")
                                or 0
                            )
                            if 1 <= _native_phases <= 3:
                                detected_phases = _native_phases
                                break
                    except Exception:
                        pass
                    if wb_phase_count_live > 0 and abs(wb_actual_power) > 500:
                        detected_phases = wb_phase_count_live
                    elif multi_connect_configured:
                        try:
                            _native_status = read_recent_json(os.path.join(RAMDISK_DIR, "wallbox_native.json"))
                            _native_details = _native_status.get("wb_details") or []
                            if _native_details:
                                _native_phases = int(_native_details[0].get("phases_in_use", 0) or 0)
                                if 1 <= _native_phases <= 3:
                                    detected_phases = _native_phases
                        except Exception:
                            pass
                    if not charging_active_any:
                        try:
                            for _v in valid_chargers_status:
                                _st = _v.get("status")
                                if not _wb_status_connected(_st):
                                    continue
                                _cd = next((cd for cd in chargers if cd.get("id") == _v.get("id")), None)
                                _klass = _cd.get("charger").__class__.__name__ if _cd else ""
                                if _klass in ("OpenWBCharger", "OpenWBProCharger"):
                                    # Ohne echte Ladeleistung ist phases_in_use
                                    # bei openWB/openWB Pro oft nur Ziel/Relais-
                                    # Zustand. Fuer einen ruhigen Start darf
                                    # das nicht die Mindestleistung auf 3p
                                    # festnageln; die 3p-Anforderung passiert
                                    # weiter unten sobald genug Budget da ist.
                                    detected_phases = 1
                                    break
                        except Exception:
                            pass
                    try:
                        active_kw_limits = []
                        for _cd in chargers:
                            _kw = _sf(config.get("wb%d_charge_power" % _cd.get("id"), 0), 0.0)
                            if _kw > 0:
                                active_kw_limits.append(_kw)
                        if active_kw_limits:
                            _kw_limit = max(active_kw_limits)
                            _amp_by_kw = int(math.ceil((_kw_limit * 1000.0) / (230.0 * max(1, detected_phases))))
                            wb_max_amp = max(6, min(wb_max_amp, _amp_by_kw))
                    except Exception:
                        pass

                    # Min-Leistung selbst lernen (bei 6A-Ladung)
                    wb_min_amp_cfg = int(_sf(config.get("wbminladestrom", 6), 6.0))
                    for _cd in chargers:
                        if _cd.get("current_set_amp", 0) == wb_min_amp_cfg and _cd.get("is_charging", False):
                            if 1000 < wb_actual_power < 5500:
                                wb_min_power_meas = wb_actual_power
                                break

                    # --- WB-Budget-Signal lesen (primaer: wb_pv_budget.json, 2s-Intervall) ---
                    # Fallback: storage_plan.json (15min-Intervall, weniger frisch)
                    WB_BUDGET_FILE = "/var/www/html/ramdisk/wb_pv_budget.json"
                    _budget        = {}
                    _budget_age_s  = 9999.0
                    try:
                        if os.path.exists(WB_BUDGET_FILE):
                            _budget = read_json_cached(WB_BUDGET_FILE)
                            _budget_age_s = max(0.0, time.time() - float(_budget.get('ts', 0)))
                    except Exception: pass

                    _budget_ok      = _budget_age_s < 15.0   # Fresh: < 15s
                    _budget_stale   = 15.0 <= _budget_age_s < 45.0  # Stale: 15-45s
                    _budget_timeout = _budget_age_s >= 45.0          # Timeout: > 45s
                    _budget_state   = _budget.get('state', 'run') if _budget_ok else ('reduce' if _budget_stale else 'stop')
                    _budget_live_sample_invalid = bool(live_sample_invalid or (_budget_ok and _budget.get('live_sample_invalid')))
                    storage_grid_hold_active = (_budget_state == 'ifc_grid_hold')

                    # --- StoragePlan lesen (Fallback / can_reach_target / SoC-Timeline) ---
                    STORAGE_PLAN_FILE = "/var/www/html/ramdisk/storage_plan.json"
                    _plan = {}
                    try:
                        if os.path.exists(STORAGE_PLAN_FILE):
                            _plan = read_json_cached(STORAGE_PLAN_FILE)
                    except Exception: pass

                    can_reach_target = _plan.get("can_reach_target", True)
                    storage_forecast_hold_active = bool(
                        _budget_ok and _storage_budget_forecast_holds(_budget)
                    )
                    phase_forecast_hold_active = bool(
                        _phase_forecast_holds(_plan)
                        or storage_forecast_hold_active
                    )
                    free_for_limbs_w = 0.0
                    raw_iaval_w = 0.0
                    effective_iaval_w = 0.0
                    bat_charge_request_w = 0.0
                    storage_charge_request_w = 0.0
                    storage_charge_priority_active = False
                    openwb_pro_curve_direct_storage_block = False
                    wb_storage_cap_w = 0.0
                    wb_storage_extra_w = 0.0
                    wallbox_curve_reserve_w = 0.0
                    try:
                        if _budget_ok and 'budget_w' in _budget:
                            # Primaer: wb_pv_budget.json (Storage Manager, 2s-frisch)
                            free_for_limbs_w = float(_budget.get('budget_w', 0.0))
                            raw_iaval_w = float(_budget.get('raw_iAVal_w', _budget.get('iAVal_w', free_for_limbs_w)) or 0.0)
                            effective_iaval_w = float(_budget.get('iAVal_w', free_for_limbs_w) or 0.0)
                            bat_charge_request_w = float(_budget.get('iMinLade_w', 0.0) or 0.0)
                            storage_charge_request_w = float(_budget.get('storage_charge_request_w', _budget.get('storage_req_w', 0.0)) or 0.0)
                            wb_storage_cap_w = float(_budget.get('wb_storage_cap_w', 0.0) or 0.0)
                            wb_storage_extra_w = float(_budget.get('wb_storage_extra_w', free_for_limbs_w) or 0.0)
                            wallbox_curve_reserve_w = float(_budget.get('wallbox_curve_reserve_w', 0.0) or 0.0)
                            if _budget_live_sample_invalid:
                                free_for_limbs_w = 0.0
                                raw_iaval_w = 0.0
                                effective_iaval_w = 0.0
                                wb_storage_extra_w = 0.0
                        else:
                            # Fallback: storage_plan.json energy_score
                            cs = _plan.get("energy_score") or _plan.get("charge_score") or {}
                            free_for_limbs_w = float(cs.get("free_for_limbs_w", 0.0) or 0.0)
                            raw_iaval_w = free_for_limbs_w
                            effective_iaval_w = free_for_limbs_w
                            bat_charge_request_w = float(cs.get("bat_charge_request_w", 0.0) or 0.0)
                            storage_charge_request_w = bat_charge_request_w
                    except Exception: pass
                    if _budget_live_sample_invalid:
                        free_for_limbs_w = 0.0
                        raw_iaval_w = 0.0
                        effective_iaval_w = 0.0
                        wb_storage_extra_w = 0.0

                    _market_wallbox_release = {"allowed": False}
                    market_wallbox_active = False
                    _legacy_price_boost_wallbox_active = False
                    _price_boost_budget_override = False
                    try:
                        _market_wallbox_release = read_market_plan_allow("wallbox", _plan, config)
                        market_wallbox_active = bool(_market_wallbox_release.get("allowed"))
                        _market_price_mode_ids_live = set()
                        for _v in valid_chargers_status:
                            _st = _v.get('status') or {}
                            _cid = int(_v.get('id'))
                            if not (_wb_status_connected(_st) or bool(_st.get('charging', False))):
                                continue
                            if wb_locked.get(_cid, False) or wb_manual_pause.get(_cid, False):
                                continue
                            if normalize_wb_mode(wb_charge_mode.get(_cid, 0)) == MODE_PRICE:
                                _market_price_mode_ids_live.add(_cid)
                        _legacy_price_boost_wallbox_active = read_price_boost_allow("wallbox", config)
                        _price_boost_budget_override = bool(
                            _legacy_price_boost_wallbox_active
                            or (market_wallbox_active and _market_price_mode_ids_live)
                        )
                    except Exception:
                        _market_wallbox_release = {"allowed": False, "reason": "market_plan_error"}
                        market_wallbox_active = False
                        _legacy_price_boost_wallbox_active = False
                        _price_boost_budget_override = False
                    _predump_candidate_ids = set()
                    try:
                        for _v in valid_chargers_status:
                            _st = _v.get('status') or {}
                            _cid = int(_v.get('id'))
                            if (
                                bool(_st.get('car', 1) >= 2 or _st.get('charging', False))
                                and int(wb_charge_mode.get(_cid, 0) or 0) > 0
                                and not wb_locked.get(_cid, False)
                            ):
                                _predump_candidate_ids.add(_cid)
                    except Exception:
                        _predump_candidate_ids = set()
                    _predump_has_candidate = bool(_predump_candidate_ids)
                    predump_wallbox_active = False
                    try:
                        predump_wallbox_active = (
                            read_predump_allow("wallbox", config)
                            and _predump_has_candidate
                            and bool(_budget.get("predump_active") or _budget.get("state") == "pre_discharge")
                            and bool(_budget.get("predump_allow_wallbox", True))
                        )
                    except Exception:
                        predump_wallbox_active = False
                    storage_predump_active = bool(
                        _budget.get("predump_active")
                        or _budget.get("state") == "pre_discharge"
                        or _budget.get("storage_state") == "pre_discharge"
                    )
                    predump_floor_hold_active = bool(
                        _budget_ok
                        and (
                            _budget.get("force_wallbox_stop")
                            or _budget.get("predump_floor_hold")
                            or _budget_state == "wallbox_predump_floor_hold"
                            or _budget.get("storage_state") == "wallbox_predump_floor_hold"
                        )
                    )
                    e3dc_wb_discharge_bat_until_soc = _effective_wallbox_floor_soc(config, live, 0.0)
                    effective_wb_floor_soc = _effective_wallbox_floor_soc(
                        config,
                        live,
                        wb_minsoc_cfg,
                    )
                    if predump_wallbox_active:
                        try:
                            _pd_floor = float(_budget.get(
                                "predump_floor_soc",
                                _budget.get("predump_target_soc", wb_minsoc_cfg)
                            ))
                            # Nur temporaer fuer den Pre-Dump-Verbrauchermodus.
                            # Die normale wbminsoc-Konfiguration bleibt unangetastet.
                            emergency_floor = float(config.get("emergency_power_reserve", 0) or 0)
                            effective_wb_floor_soc = _effective_wallbox_floor_soc(
                                config,
                                live,
                                wb_minsoc_cfg,
                                emergency_floor,
                                _pd_floor,
                            )
                        except Exception:
                            effective_wb_floor_soc = _effective_wallbox_floor_soc(
                                config,
                                live,
                                wb_minsoc_cfg,
                            )
                    e3dc_wb_floor_clamp_active = bool(
                        e3dc_wb_discharge_bat_until_soc > wb_minsoc_cfg + 0.05
                    )
                    wbminsoc_floor_note = ""
                    if e3dc_wb_floor_clamp_active:
                        wbminsoc_floor_note = (
                            "E3DC-Untergrenze %.1f%% hebt lokale Wallbox-Reserve %.1f%% "
                            "auf wirksam %.1f%% an."
                        ) % (
                            float(e3dc_wb_discharge_bat_until_soc),
                            float(wb_minsoc_cfg),
                            float(effective_wb_floor_soc),
                        )

                    # Budget-Timeout-Reaktion: bei STALE auf 6A drosseln, bei TIMEOUT stoppen.
                    # Wichtig: Der PV-Fallback weiter unten darf STALE/TIMEOUT nicht wieder auffuellen.
                    if _price_boost_budget_override:
                        # Preis-Boost ist ein eigener Override. Er darf nicht an
                        # einem kurz alten PV-Budget haengen, besonders nicht
                        # waehrend der Storage Manager neu startet.
                        _budget_stale = False
                        _budget_timeout = False
                        _budget_state = 'run'
                        runtime.reset_budget_log_flags()
                    elif predump_wallbox_active and _budget_ok:
                        # Pre-Dump ist nur bei frischem Speicherbudget aktiv.
                        # Kein Netzbezug erlaubt, aber die WB darf die geplante
                        # Entladeleistung aufnehmen bevor wir einspeisen.
                        _budget_stale = False
                        _budget_timeout = False
                        _budget_state = 'run'
                        runtime.reset_budget_log_flags()
                    elif _budget_stale:
                        # Budget veraltet: WB auf 6A halten bis Signal zurueck
                        free_for_limbs_w, _budget_log_kind = runtime.apply_budget_freshness_guard(
                            free_for_limbs_w=free_for_limbs_w,
                            detected_phases=detected_phases,
                            budget_stale=True,
                            budget_timeout=False,
                        )
                        if _budget_log_kind == "stale":
                            logger.warning('wb_pv_budget.json veraltet (%.0fs) - drossle WB auf 6A' % _budget_age_s)
                    elif _budget_timeout:
                        # Budget-Timeout: WB graceful stoppen (als physischer Abbruch behandeln)
                        free_for_limbs_w, _budget_log_kind = runtime.apply_budget_freshness_guard(
                            free_for_limbs_w=free_for_limbs_w,
                            detected_phases=detected_phases,
                            budget_stale=False,
                            budget_timeout=True,
                        )
                        if _budget_log_kind == "timeout":
                            logger.warning('wb_pv_budget.json Timeout (%.0fs) - stoppe WB' % _budget_age_s)
                    else:
                        runtime.reset_budget_log_flags()
                    if predump_wallbox_active and not _budget_ok:
                        predump_wallbox_active = False
                    if not _budget_ok:
                        predump_floor_hold_active = False

                    # Pre-Dump-WB-Freigabe mit eigener Start-/Stop-Hysterese.
                    # Ohne Gate wuerde die WB bei knappem Budget sofort starten,
                    # Storage sieht WB-Last, Budget springt, WB stoppt: Takten.
                    predump_wallbox_active, predump_wallbox_gate_open, predump_wallbox_exited = (
                        runtime.update_predump_wallbox_gate(
                            predump_active=predump_wallbox_active,
                            has_candidate=_predump_has_candidate,
                            free_for_limbs_w=free_for_limbs_w,
                            grid_power_w=grid_power_budget_w,
                            detected_phases=detected_phases,
                            now_ts=time.time(),
                            bootstrap_ready=bool(_budget.get("predump_bev_block_w")),
                            bootstrap_power_w=_sf(_budget.get("predump_bev_block_w", 0), 0.0),
                        )
                    )

                    # Wenn Budget-State='reduce': ebenfalls auf 6A begrenzen
                    if _budget_state == 'reduce' and free_for_limbs_w > 0:
                        free_for_limbs_w = min(free_for_limbs_w, 6 * 230.0 * max(1, detected_phases))

                    # Fallback wenn StoragePlan kein free_for_limbs_w hat: PV-Surplus nutzen.
                    # Ausnahme Pre-Dump: Einspeisung entsteht hier absichtlich durch
                    # Entladung. Ohne explizite WB-Freigabe darf daraus kein neues
                    # Wallbox-Budget werden.
                    if (
                        free_for_limbs_w <= 0
                        and grid_power_budget_w < -200
                        and not (_budget_stale or _budget_timeout)
                        and not storage_grid_hold_active
                        and not predump_floor_hold_active
                        and not (storage_predump_active and not predump_wallbox_active)
                    ):
                        free_for_limbs_w = max(0.0, -grid_power_budget_w + max(0, battery_power_raw))

                    # Interpoliere Soll-SoC jetzt aus target_timeline
                    soll_soc_now = None
                    try:
                        now_ms = time.time() * 1000
                        tl = _plan.get("target_timeline", [])
                        if len(tl) >= 2:
                            if now_ms < tl[0]["ts"]:
                                soll_soc_now = tl[0]["soc"]
                            else:
                                for _i in range(len(tl) - 1):
                                    _a, _b = tl[_i], tl[_i + 1]
                                    if _a["ts"] <= now_ms < _b["ts"]:
                                        _frac = (now_ms - _a["ts"]) / max(1, _b["ts"] - _a["ts"])
                                        soll_soc_now = _a["soc"] + _frac * (_b["soc"] - _a["soc"])
                                        break
                        if soll_soc_now is None and tl:
                            soll_soc_now = tl[-1]["soc"]
                    except Exception: pass

                    # delta: positiv = SOC ueber Kurve (gut), negativ = SOC unter Kurve
                    delta = (battery_soc - soll_soc_now) if soll_soc_now is not None else 0.0

                    # --- EcoScore lesen (fuer Modus 5) ---
                    eco_score_now = 0.0
                    try:
                        ECO_FILE = "/var/www/html/ramdisk/eco_score.json"
                        if os.path.exists(ECO_FILE):
                            _es = json.load(open(ECO_FILE))
                            _now_ms = time.time() * 1000
                            for _slot in _es:
                                if _slot["start_timestamp"] <= _now_ms < _slot["end_timestamp"]:
                                    eco_score_now = float(_slot.get("optimization_score", 0.0))
                                    break
                    except Exception: pass

                    # Per-Zyklus Neuberechnung: nur Ladepunkte mit Fahrzeug duerfen
                    # den gemeinsamen Storage-/Preis-Kontext bestimmen. Sonst kann
                    # eine leere WB2 in Mode 11 eine aktive WB1 in Mode 3
                    # ungewollt auf Netz-/Speicherfreigabe ziehen.
                    connected_charger_ids = set()
                    for _v in valid_chargers_status:
                        try:
                            _st = _v.get('status') or {}
                            if _wb_status_connected(_st) or bool(_st.get('charging', False)):
                                connected_charger_ids.add(int(_v.get('id')))
                        except Exception:
                            pass

                    battery_departure_states = {}
                    battery_departure_active_ids = set()
                    battery_departure_blocked_ids = set()
                    battery_departure_expired_ids = set()
                    for _cid in connected_charger_ids:
                        _mode = normalize_wb_mode(wb_charge_mode.get(_cid, 0))
                        if _mode != MODE_BATTERY_DEPARTURE:
                            continue
                        _state = _battery_departure_state(config, _cid, _mode)
                        battery_departure_states[_cid] = _state
                        if _state.get("active"):
                            battery_departure_active_ids.add(_cid)
                        else:
                            battery_departure_blocked_ids.add(_cid)
                            if _state.get("expired"):
                                battery_departure_expired_ids.add(_cid)
                    battery_departure_label = ""
                    battery_departure_start_label = ""
                    battery_departure_block_reason = ""
                    if battery_departure_states:
                        battery_departure_label = "/".join(
                            sorted({str(_s.get("departure_time", "")) for _s in battery_departure_states.values() if _s.get("departure_time")})
                        )
                        battery_departure_start_label = "/".join(
                            sorted({str(_s.get("start_time", "")) for _s in battery_departure_states.values() if _s.get("start_time")})
                        )
                        battery_departure_block_reason = next(
                            (
                                str(_s.get("reason", ""))
                                for _s in battery_departure_states.values()
                                if _s.get("blocked")
                            ),
                            "",
                        )

                    controllable_charger_ids = {
                        _cid for _cid in connected_charger_ids
                        if normalize_wb_mode(wb_charge_mode.get(_cid, 0)) != MODE_OFF
                        and _cid not in battery_departure_blocked_ids
                        and not wb_locked.get(_cid, False)
                    }

                    # --- EPEX / Ladeplan Check ---
                    current_price = read_current_epex_price(config)
                    dvcarlimit = price_limit_ct(config)
                    price_boost_wallbox_active = False
                    tariff_price_window_active = price_allows_grid(current_price, config)
                    scheduled_slot_charger_ids = schedule_service.active_charger_ids(
                        controllable_charger_ids
                    )
                    instant_plan_charger_ids = set()
                    # Geplante Slots sind bereits das Ergebnis der Preislogik
                    # (guenstigste Stunden im Nutzerfenster). Sie oeffnen
                    # deshalb Netzladen eigenstaendig und werden nicht nochmal
                    # durch das spontane Wallbox-Preislimit blockiert.
                    planned_charger_ids = set(scheduled_slot_charger_ids)
                    price_optimizing_active = bool(planned_charger_ids)
                    # Sofortladen (wbhour=99) direkt aus Config lesen als Kurzschluss-Check.
                    # Verhindert Race Condition beim Start: schedule_file wird erst nach dem
                    # ersten Tick geschrieben -> ohne diesen Check wuerde Mode=1 (Sonnenmodus)
                    # gesendet, dann 20s spaeter Mode=2 (Netzmodus) -> physischer Abbruch!
                    _wbhour_raw = int(config.get("wbhour", config.get("Wbhour", 0)) or 0)
                    if _wbhour_raw >= 99:
                        if tariff_price_window_active:
                            _price_mode_ids = {
                                _cid for _cid in controllable_charger_ids
                                if normalize_wb_mode(wb_charge_mode.get(_cid, 0)) == MODE_PRICE
                            }
                            planned_charger_ids.update(_price_mode_ids)
                            instant_plan_charger_ids.update(_price_mode_ids)
                            price_optimizing_active = True
                    for _cid in controllable_charger_ids:
                        try:
                            if int(float(config.get(f"wb{_cid}_plan_hours", 0) or 0)) >= 99:
                                if tariff_price_window_active and normalize_wb_mode(wb_charge_mode.get(_cid, 0)) == MODE_PRICE:
                                    planned_charger_ids.add(_cid)
                                    instant_plan_charger_ids.add(_cid)
                                    price_optimizing_active = True
                        except Exception:
                            pass
                    _market_wallbox_grid_ids = _wallbox_market_price_mode_ids(
                        wb_charge_mode,
                        controllable_charger_ids,
                    )
                    market_wallbox_grid_active = bool(market_wallbox_active and _market_wallbox_grid_ids)
                    if market_wallbox_grid_active or _legacy_price_boost_wallbox_active:
                        price_boost_wallbox_active = True
                        price_optimizing_active = True
                    price_plan_storage_protect_active = bool(
                        price_boost_wallbox_active
                        or scheduled_slot_charger_ids
                    )

                    effective_public_wb_mode = _select_effective_public_wb_mode(
                        wb_charge_mode,
                        (_cd.get('id') for _cd in chargers if _cd.get('id') in connected_charger_ids),
                        blocked_ids=battery_departure_blocked_ids,
                    )
                    mode5_grid_allowed = bool(
                        effective_public_wb_mode == MODE_PRICE
                        and (tariff_price_window_active or price_boost_wallbox_active)
                    )
                    effective_wb_mode = controller_mode(effective_public_wb_mode, grid_allowed=mode5_grid_allowed)
                    price_optimized_charger_ids = set(planned_charger_ids)
                    if mode5_grid_allowed:
                        price_optimized_charger_ids.update(
                            _cid for _cid in controllable_charger_ids
                            if normalize_wb_mode(wb_charge_mode.get(_cid, 0)) == MODE_PRICE
                        )
                    if _legacy_price_boost_wallbox_active:
                        price_optimized_charger_ids.update(controllable_charger_ids)
                    elif market_wallbox_grid_active:
                        price_optimized_charger_ids.update(_market_wallbox_grid_ids)
                    price_optimizing_active = bool(price_optimized_charger_ids or mode5_grid_allowed)
                    if mode5_grid_allowed:
                        price_plan_storage_protect_active = True
                    grid_unlocked_all_controllable = bool(
                        price_boost_wallbox_active
                        or (
                            controllable_charger_ids
                            and controllable_charger_ids.issubset(price_optimized_charger_ids)
                        )
                    )
                    predump_charger_ids = {
                        _cid for _cid in connected_charger_ids
                        if normalize_wb_mode(wb_charge_mode.get(_cid, 0)) != MODE_OFF and not wb_locked.get(_cid, False)
                    }
                    if predump_wallbox_active and predump_wallbox_gate_open and predump_charger_ids:
                        # Pre-Dump-Freigabe: lokale Verbraucher vor Netzeinspeisung.
                        # Fachlich entspricht das Mode 10: PV+Speicher, aber kein Netz.
                        effective_wb_mode = max(effective_wb_mode, 10)
                    curve_wb_relief_active = bool(
                        _budget_ok
                        and bool(_budget.get("curve_wb_relief"))
                        and effective_wb_mode != MODE_OFF
                    )
                    direct_marketing_active = bool(
                        _budget_ok
                        and _truthy_config(_budget.get("direct_marketing_active", False))
                    )

                    def hysteresis_gate(name, value, start_at, stop_at, default_open=False):
                        return runtime.hysteresis_gate(name, value, start_at, stop_at, default_open)

                    predump_wallbox_floor_soc = 0.0
                    predump_wallbox_floor_gate_open = True
                    predump_wallbox_floor_block = False
                    try:
                        _pd_floor_raw = float(_budget.get(
                            "predump_floor_soc",
                            _budget.get(
                                "predump_target_soc",
                                _plan.get(
                                    "predump_min_soc",
                                    config.get("storage_predump_min_soc", 0),
                                ),
                            ),
                        ) or 0.0)
                    except Exception:
                        _pd_floor_raw = 0.0
                    if (
                        _pd_floor_raw > 0.0
                        and _predump_has_candidate
                        and _truthy_config(config.get("predump_enable", 1))
                        and _truthy_config(config.get("predump_wallbox_enable", 0))
                        and battery_soc is not None
                    ):
                        try:
                            _pd_emergency_floor = float(config.get("emergency_power_reserve", 0) or 0)
                        except Exception:
                            _pd_emergency_floor = 0.0
                        predump_wallbox_floor_soc = max(_pd_emergency_floor, _pd_floor_raw)
                        _pd_floor_release_pct = max(
                            1.0,
                            _sf(config.get("predump_wallbox_floor_release_pct", 2.0), 2.0),
                        )
                        _pd_floor_stop_pct = max(
                            0.0,
                            _sf(config.get("predump_wallbox_floor_stop_pct", 0.0), 0.0),
                        )
                        predump_wallbox_floor_gate_open = hysteresis_gate(
                            "predump_wallbox_floor",
                            float(battery_soc) - predump_wallbox_floor_soc,
                            _pd_floor_release_pct,
                            _pd_floor_stop_pct,
                            default_open=True,
                        )
                        predump_wallbox_floor_block = not predump_wallbox_floor_gate_open
                        if predump_wallbox_floor_block:
                            effective_wb_floor_soc = max(effective_wb_floor_soc, predump_wallbox_floor_soc)
                            curve_wb_relief_active = False

                    mode_policy = wallbox_policy.resolve_mode_policy(
                        effective_wb_mode=effective_wb_mode,
                        delta_pct=delta,
                        pv_power_w=pv_power_raw,
                        eco_score_now=eco_score_now,
                        eco_grid_score=eco_grid_score,
                        eco_pv_score=eco_pv_score,
                        battery_soc=battery_soc,
                        wb_soc_hyst_pct=wb_soc_hyst_pct,
                        curve_wb_relief_active=curve_wb_relief_active,
                        hysteresis_gate=hysteresis_gate,
                    )
                    params = mode_policy["params"]
                    effective_allow_grid = bool(mode_policy["effective_allow_grid"])
                    fuzzy_delta = float(mode_policy["fuzzy_delta"])
                    band_dn_eff = float(mode_policy["band_dn_eff"])
                    band_up = float(mode_policy["band_up"])
                    fz = float(mode_policy["fz"])
                    wbmin_mode4_gate_open = bool(mode_policy["wbmin_mode4_gate_open"])

                    # Grundladung stabil: 6A-Boden nur, solange wbminSoC im
                    # Hausspeicher erreichbar bleibt und der Storage Manager
                    # keinen iFc/Grid-Hold meldet.
                    # Reihenfolge wie im alten C++-Gedanken: erst WB-Budget
                    # senken und Netzpunkt beruhigen, dann Speicherladung.
                    base_floor_reachable = _storage_floor_reachable(
                        _plan,
                        battery_soc,
                        effective_wb_floor_soc,
                        wb_soc_hyst_pct,
                    )
                    base_6a_active = wallbox_policy.base_charge_active(
                        params=params,
                        base_floor_reachable=base_floor_reachable,
                        soll_soc_now_available=soll_soc_now is not None,
                        storage_grid_hold_active=storage_grid_hold_active,
                        curve_wb_relief_active=curve_wb_relief_active,
                    )

                    # --- Physikalischer Echtzeit-Deckel (IMMER berechnet) ---
                    # PV-only nutzt den Netzpunkt: aktuelle WB-Leistung plus freie Einspeisung
                    # minus Netzbezug. Batteriestuetzende Modi duerfen reale Bat-Entladung
                    # als Quelle sehen, bleiben aber ebenfalls unter dem WR-Limit.
                    home_w_raw = get_float(live, "Home_Power", 0.0)
                    home_w_control = home_w_raw
                    if not bool(live_plausibility.get("home_valid", True)):
                        balance_home_w = float(live_plausibility.get("home_balance_w", 0.0) or 0.0)
                        if balance_home_w > 0 and bool(live_plausibility.get("grid_valid", True)):
                            home_w_control = balance_home_w
                    live_wb_w = get_float(live, "Wallbox_Power", 0.0)
                    home_w = home_w_control
                    if abs(wb_actual_power) > 500 and abs(live_wb_w) < 500:
                        # Bei Fremd-WB/openWB steckt die Ladeleistung oft schon
                        # im E3DC-Hausverbrauch. Wir lesen sie zusaetzlich aus
                        # openwb_data.json, duerfen sie dann aber nicht doppelt
                        # vom PV-Budget abziehen.
                        home_w = max(0.0, home_w_control - abs(wb_actual_power))
                    bat_discharge_w  = max(0.0, -battery_power_raw)
                    grid_reserve_w = max(0.0, _sf(config.get("wb_grid_reserve_w", 450), 450.0))
                    openwb_pro_export_sink_available = False
                    if (
                        effective_public_wb_mode == MODE_CURVE
                        and not (
                            price_boost_wallbox_active
                            or price_optimizing_active
                            or effective_allow_grid
                            or predump_wallbox_active
                            or direct_marketing_active
                        )
                        and battery_soc is not None
                        and battery_soc >= effective_wb_floor_soc + max(
                            2.0,
                            float(wb_soc_hyst_pct or 0.0),
                        )
                    ):
                        for _cd in chargers:
                            _charger = _cd.get("charger")
                            if _charger is None or _charger.__class__.__name__ != "OpenWBProCharger":
                                continue
                            _cid = int(_cd.get("id", 0) or 0)
                            if normalize_wb_mode(wb_charge_mode.get(_cid, MODE_OFF)) != MODE_CURVE:
                                continue
                            if wb_locked.get(_cid, False) or wb_manual_pause.get(_cid, False):
                                continue
                            _pro_status = next(
                                (
                                    _v.get("status")
                                    for _v in valid_chargers_status
                                    if int(_v.get("id", 0) or 0) == _cid
                                ),
                                None,
                            )
                            if _wb_status_connected(_pro_status):
                                openwb_pro_export_sink_available = True
                                break
                    if openwb_pro_export_sink_available:
                        grid_reserve_w = min(
                            grid_reserve_w,
                            max(
                                0.0,
                                _sf(config.get("wb_openwb_pro_export_sink_grid_reserve_w", 120), 120.0),
                            ),
                        )
                    grid_export_room_w = max(0.0, -grid_power_budget_w - grid_reserve_w)
                    grid_import_w      = max(0.0, grid_power_budget_w)
                    grid_import_down_threshold_w = max(
                        0.0,
                        _sf(config.get("wb_grid_import_down_threshold_w", 200), 200.0),
                    )
                    grid_import_down_hold_s = max(
                        5.0,
                        _sf(config.get("wb_grid_import_down_hold_s", 25), 25.0),
                    )
                    grid_import_release_w = max(
                        0.0,
                        min(
                            grid_import_down_threshold_w,
                            _sf(config.get("wb_grid_import_release_w", 80), 80.0),
                        ),
                    )
                    grid_import_budget_down_active, grid_import_budget_down_age_s = (
                        runtime.update_grid_import_budget_gate(
                            grid_power_w=grid_power_budget_w,
                            now_ts=time.time(),
                            threshold_w=grid_import_down_threshold_w,
                            release_w=grid_import_release_w,
                            hold_s=grid_import_down_hold_s,
                        )
                    )
                    # Kurzer Netzbezug ist im PV-Wallbox-Pfad kein harter
                    # Fehler: der E3DC darf den Netzpunkt ausregeln. Erst wenn
                    # der Import gehalten ueber der Schwelle liegt, wird die
                    # Wallbox als Verbraucher zurueckgenommen.
                    grid_import_for_budget_w = grid_import_w if grid_import_budget_down_active else 0.0
                    curve_forecast_guard = _curve_forecast_wallbox_guard(
                        _plan,
                        _budget,
                        config,
                        now_ts=time.time(),
                        storage_forecast_hold_active=storage_forecast_hold_active,
                    )
                    curve_forecast_wallbox_guard_applicable = bool(
                        effective_public_wb_mode == MODE_CURVE
                        and not (
                            price_boost_wallbox_active
                            or price_optimizing_active
                            or effective_allow_grid
                            or predump_wallbox_active
                        )
                    )
                    (
                        curve_forecast_wallbox_block_active,
                        curve_forecast_wallbox_release_age_s,
                    ) = runtime.update_curve_forecast_wallbox_gate(
                        enabled=curve_forecast_wallbox_guard_applicable,
                        block_requested=bool(curve_forecast_guard.get("block_requested")),
                        release_ready=bool(curve_forecast_guard.get("release_ready")),
                        now_ts=time.time(),
                        release_hold_s=float(curve_forecast_guard.get("release_hold_s", 90.0) or 90.0),
                    )
                    curve_forecast_wallbox_stop_active = bool(
                        curve_forecast_wallbox_guard_applicable
                        and curve_forecast_wallbox_block_active
                    )
                    curve_forecast_wallbox_assist_allowed = bool(
                        curve_forecast_wallbox_guard_applicable
                        and not curve_forecast_wallbox_stop_active
                        and bool(curve_forecast_guard.get("assist_allowed"))
                    )
                    storage_budget_floor_relation = str(_budget.get("adaptive_curve_relation", "") or "")
                    try:
                        storage_budget_gap_pct = float(_budget.get("curve_gap_pct", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        storage_budget_gap_pct = 0.0
                    storage_budget_needs_curve_charge = bool(
                        _budget_ok
                        and storage_charge_request_w > max(250.0, grid_reserve_w)
                        and free_for_limbs_w <= max(120.0, grid_reserve_w)
                        and min(
                            max(0.0, raw_iaval_w),
                            max(0.0, effective_iaval_w),
                        ) <= max(120.0, grid_reserve_w)
                        and (
                            storage_budget_floor_relation.startswith("below")
                            or storage_budget_gap_pct > 0.5
                        )
                    )
                    if storage_budget_needs_curve_charge:
                        storage_charge_priority_active = True
                        openwb_pro_curve_direct_storage_block = True
                    if curve_forecast_wallbox_stop_active:
                        storage_charge_priority_active = True
                        openwb_pro_curve_direct_storage_block = True
                    pv_only_allowed_w  = max(0.0, wb_actual_power + grid_export_room_w - grid_import_for_budget_w)
                    pv_surplus_ex_wb_w = max(0.0, pv_power_raw - home_w - grid_reserve_w)
                    openwb_pro_curve_direct_start_min_w = 6.0 * 230.0
                    bat_assist_allowed_w = max(0.0, pv_power_raw + bat_discharge_w - home_w - grid_reserve_w)
                    wbminsoc_gate_open = True
                    if effective_wb_mode == 4:
                        wbminsoc_gate_open = bool(wbmin_mode4_gate_open)
                    elif effective_wb_mode in (9, 10, 11):
                        wbminsoc_restart_above_pct = wb_soc_hyst_pct
                        if effective_wb_mode in (9, 10) and not effective_allow_grid:
                            wbminsoc_restart_above_pct = max(
                                wb_soc_hyst_pct,
                                _sf(config.get("wb_target_restart_above_wbminsoc_pct", 2.0), 2.0),
                            )
                        wbminsoc_gate_open = hysteresis_gate(
                            "wbmin_discharge_mode%d" % effective_wb_mode,
                            battery_soc - effective_wb_floor_soc,
                            wbminsoc_restart_above_pct,
                            0.0
                        )
                    curve_wbminsoc_gate_open = True
                    if (
                        effective_public_wb_mode == MODE_CURVE
                        and effective_wb_mode > 0
                        and not effective_allow_grid
                        and not price_boost_wallbox_active
                        and not price_optimizing_active
                        and not predump_wallbox_active
                        and battery_soc is not None
                    ):
                        curve_wbminsoc_restart_above_pct = max(
                            wb_soc_hyst_pct,
                            _sf(
                                config.get(
                                    "wb_curve_restart_above_wbminsoc_pct",
                                    config.get("wb_target_restart_above_wbminsoc_pct", 2.0),
                                ),
                                2.0,
                            ),
                        )
                        curve_wbminsoc_gate_open = hysteresis_gate(
                            "wbmin_curve_mode",
                            battery_soc - effective_wb_floor_soc,
                            curve_wbminsoc_restart_above_pct,
                            0.0,
                        )
                    curve_wbminsoc_floor_guard_active = bool(
                        effective_public_wb_mode == MODE_CURVE
                        and effective_wb_mode > 0
                        and not effective_allow_grid
                        and not price_boost_wallbox_active
                        and not price_optimizing_active
                        and not predump_wallbox_active
                        and not curve_wbminsoc_gate_open
                    )
                    soc_diff_for_wb = max(0.0, battery_soc - effective_wb_floor_soc) if battery_soc is not None else 0.0
                    wbminsoc_discharge_taper_above_pct = max(
                        wb_soc_hyst_pct,
                        _sf(
                            config.get(
                                "wb_target_discharge_taper_above_pct",
                                config.get("wb_target_restart_above_wbminsoc_pct", 2.0),
                            ),
                            2.0,
                        ),
                    )
                    _forecast_auto_min_w = 6.0 * 230.0 * max(1, detected_phases)
                    _forecast_auto_storage_state = str(_budget.get("storage_state", _budget_state) or _budget_state)
                    _forecast_auto_storage_blocks_wb = bool(
                        _budget_ok
                        and _forecast_auto_storage_state in (
                            "morning_autonomy",
                            "wbmin_charge_recovery",
                            "wb9_wbminsoc_hold",
                        )
                    )
                    storage_curve_budget_active = bool(
                        _budget_ok
                        and (
                            wallbox_curve_reserve_w > 0
                            or _forecast_auto_storage_state in (
                                "parallel_wb_auto",
                                "parallel_curve_charge",
                                "parallel_curve_charge_cap",
                                "parallel_curve_auto_charge",
                                "parallel_curve_auto_hold",
                                "parallel_curve_auto_no_surplus",
                            )
                        )
                    )
                    openwb_pro_curve_direct_storage_soft_release = bool(
                        storage_curve_budget_active
                        and wbminsoc_gate_open
                        and pv_power_raw > 100.0
                        and grid_export_room_w > 0.0
                        and raw_iaval_w >= openwb_pro_curve_direct_start_min_w
                    )
                    # Unterhalb wbminSoC bleibt die intern geregelte Wallbox
                    # PV-Kurve-ruhig: keine eigene Storage-Floor-Pause und
                    # keine abgesenkte Ladekurve. Akkuhilfe wird erst bei
                    # offenem wbminSoC-Gate separat freigegeben.
                    controlled_wallbox_wbminsoc_pause = False
                    controlled_wallbox_wbminsoc_pv_only_active = bool(
                        (
                            effective_public_wb_mode == MODE_TARGET
                            and effective_wb_mode in (9, 10)
                            and not effective_allow_grid
                            and not wbminsoc_gate_open
                            and not price_boost_wallbox_active
                            and not price_optimizing_active
                            and not predump_wallbox_active
                        )
                        or curve_wbminsoc_floor_guard_active
                    )
                    target_wbminsoc_low_power_min_w = 6.0 * 230.0
                    target_wbminsoc_low_power_bootstrap_w = (
                        target_wbminsoc_low_power_min_w * max(1, detected_phases)
                    )
                    target_wbminsoc_low_power_real_surplus_w = max(
                        0.0,
                        pv_only_allowed_w,
                        pv_surplus_ex_wb_w,
                        float(free_for_limbs_w or 0.0),
                    )
                    target_wbminsoc_low_power_3p_ready_w = max(
                        6.0 * 230.0 * 3.0 + 600.0,
                        _sf(config.get("wb_target_low_power_3p_ready_w", 4800), 4800.0),
                    )
                    target_wbminsoc_low_power_start_ready = bool(
                        wb_actual_power > 500.0
                        or target_wbminsoc_low_power_real_surplus_w >= max(
                            750.0,
                            target_wbminsoc_low_power_min_w - 250.0,
                        )
                    )
                    target_wbminsoc_low_power_recovery_active = bool(
                        effective_public_wb_mode == MODE_TARGET
                        and effective_wb_mode in (9, 10)
                        and not effective_allow_grid
                        and wbminsoc_gate_open
                        and not controlled_wallbox_wbminsoc_pause
                        and not price_boost_wallbox_active
                        and not price_optimizing_active
                        and not predump_wallbox_active
                        and battery_soc is not None
                        and soc_diff_for_wb <= wbminsoc_discharge_taper_above_pct
                        and (
                            soll_soc_now is None
                            or battery_soc < float(soll_soc_now) - 0.5
                        )
                        and target_wbminsoc_low_power_real_surplus_w < target_wbminsoc_low_power_3p_ready_w
                    )
                    _forecast_auto_can_start_or_hold = bool(
                        free_for_limbs_w >= _forecast_auto_min_w
                        or (
                            (wb_actual_power > 500.0 or any(int(_c.get("current_set_amp", 0) or 0) > 0 for _c in chargers))
                            and raw_iaval_w > -_forecast_auto_min_w
                        )
                    )
                    forecast_auto_relief_active = bool(
                        phase_forecast_hold_active
                        and effective_wb_mode > 0
                        and not storage_charge_priority_active
                        and wbminsoc_gate_open
                        and not predump_wallbox_floor_block
                        and delta <= _sf(config.get("wb_forecast_auto_relief_above_pct", 2.0), 2.0)
                        and _forecast_auto_can_start_or_hold
                        and not _forecast_auto_storage_blocks_wb
                        and not controlled_wallbox_wbminsoc_pause
                    )
                    storage_charge_reserve_w = 0.0
                    if effective_wb_mode == 4 and not wbminsoc_gate_open:
                        # Mode 4: nur echte Notfallreserve bei fast leerem
                        # Speicher. Der normale wbminSoC ist fuer Mode 4 keine
                        # Sperre, sondern die C++-nahe iAvalPower-Regelung.
                        storage_charge_reserve_w = max(0.0, bat_charge_request_w)
                        if storage_charge_reserve_w > 0:
                            storage_charge_priority_active = True
                            pv_only_allowed_w = max(0.0, pv_only_allowed_w - storage_charge_reserve_w)
                            pv_surplus_ex_wb_w = max(0.0, pv_surplus_ex_wb_w - storage_charge_reserve_w)
                            bat_assist_allowed_w = max(0.0, bat_assist_allowed_w - storage_charge_reserve_w)
                    bat_assist_enabled = (
                        params.get("bat", False)
                        and wbminsoc_gate_open
                        and not predump_wallbox_floor_block
                    )
                    if bat_assist_enabled:
                        phys_surplus_w = bat_assist_allowed_w
                    elif effective_wb_mode in (9, 10, 11):
                        phys_surplus_w = pv_surplus_ex_wb_w
                    else:
                        phys_surplus_w = pv_only_allowed_w
                    controlled_floor_battery_guard_active = False
                    controlled_floor_battery_discharge_w = 0.0
                    controlled_floor_netpoint_limit_w = 0.0
                    if controlled_wallbox_wbminsoc_pause:
                        bat_assist_allowed_w = 0.0
                        phys_surplus_w = max(0.0, pv_only_allowed_w, pv_surplus_ex_wb_w)

                    # Eba Mode 4/9/10: iAvalPower ist der gemeinsame Leitwert.
                    # Im alten C++ lagen Speicher- und Wallboxregelung in einer
                    # Schleife; Python ist getrennt und muss diesen Leitwert
                    # deshalb hier aus Live-Netzpunkt, Batterie und SoC nachbilden.
                    # Wichtig: Das ist kein Speicherbefehl. Die reale Entladung
                    # setzt weiterhin nur der Storage Manager bei echter WB-Last.
                    max_discharge_w = float(config.get(
                        'maximaleentladeleistung',
                        config.get('maximumladeleistung', 11000)
                    ))
                    max_charge_w = float(config.get('maximumladeleistung', max_discharge_w))
                    wbminsoc_discharge_taper_factor = 1.0
                    wbminsoc_discharge_taper_active = False
                    if (
                        effective_wb_mode in (9, 10)
                        and not effective_allow_grid
                        and wbminsoc_gate_open
                        and battery_soc is not None
                    ):
                        wbminsoc_discharge_taper_factor = _wbminsoc_discharge_taper_factor(
                            soc_diff_for_wb,
                            wbminsoc_discharge_taper_above_pct,
                        )
                        wbminsoc_discharge_taper_active = wbminsoc_discharge_taper_factor < 0.999
                    wbminsoc_tapered_discharge_w = max(0.0, max_discharge_w * wbminsoc_discharge_taper_factor)
                    target_wbminsoc_taper_real_surplus_w = max(
                        0.0,
                        pv_surplus_ex_wb_w,
                        float(free_for_limbs_w or 0.0),
                    )
                    target_wbminsoc_discharge_taper_limit_w = (
                        max(
                            target_wbminsoc_low_power_min_w,
                            target_wbminsoc_taper_real_surplus_w + wbminsoc_tapered_discharge_w,
                        )
                        if wbminsoc_discharge_taper_active
                        else 0.0
                    )
                    target_wbminsoc_discharge_target_w = max(
                        0.0,
                        _sf(
                            config.get("wb_target_discharge_target_w", max_discharge_w),
                            max_discharge_w,
                        ),
                    )
                    target_wbminsoc_discharge_pull_w = 0.0
                    target_wbminsoc_discharge_pull_limit_w = 0.0
                    target_wbminsoc_discharge_pull_active = bool(
                        effective_wb_mode in (9, 10)
                        and not effective_allow_grid
                        and wbminsoc_gate_open
                        and not wbminsoc_discharge_taper_active
                        and battery_soc is not None
                        and soc_diff_for_wb > wbminsoc_discharge_taper_above_pct
                        and target_wbminsoc_discharge_target_w > 0
                    )
                    if target_wbminsoc_discharge_pull_active:
                        current_battery_discharge_w = max(0.0, -battery_power_raw)
                        target_wbminsoc_discharge_pull_w = max(
                            0.0,
                            max(0.0, battery_power_raw)
                            + max(0.0, target_wbminsoc_discharge_target_w - current_battery_discharge_w),
                        )
                        if target_wbminsoc_discharge_pull_w > 0:
                            target_wbminsoc_discharge_pull_limit_w = max(
                                target_wbminsoc_low_power_min_w,
                                wb_actual_power + target_wbminsoc_discharge_pull_w,
                            )
                    forecast_auto_limit_w = 0.0
                    forecast_auto_battery_assist = bool(
                        forecast_auto_relief_active
                        and effective_public_wb_mode != MODE_CURVE
                    )
                    if forecast_auto_relief_active:
                        if forecast_auto_battery_assist:
                            # Bei erreichbarer Prognose darf der E3DC im AUTO den
                            # Netzpunkt fuehren. Die Wallbox bekommt dann einen
                            # Gesamtdeckel aus PV plus maximaler Batterieentladung,
                            # nicht mehr als der Batteriewechselrichter liefern kann.
                            forecast_auto_limit_w = max(
                                0.0,
                                pv_power_raw + wbminsoc_tapered_discharge_w - home_w - grid_reserve_w,
                            )
                        else:
                            # PV-Kurve ruhig bleibt PV-gefuehrt: forecast_auto_relief
                            # darf Phasen und Mindeststrom beruhigen, aber keine
                            # Batteriehilfe in den Wallbox-Deckel hineinrechnen.
                            forecast_auto_limit_w = max(0.0, pv_only_allowed_w)
                    eba_i_power_w = 0.0
                    eba_iaval_w = 0.0
                    if effective_wb_mode == 4:
                        # C++ wbmode 4: Leitwert ist iMinLade2 bzw. die
                        # gewichtete Speicherladeleistung. Dadurch bekommt die
                        # Wallbox Vorrang vor Speicherladung, ohne dass der
                        # Speicher mit harten wbminSoC-Schwellen blockiert.
                        _eba_pv_ok = pv_power_raw > 100
                        if wbminsoc_gate_open and _eba_pv_ok:
                            _i_fc = float(_budget.get('iFc_w', 0.0) or 0.0)
                            _i_min = float(_budget.get('iMinLade_raw_w', _budget.get('iMinLade_w', 0.0)) or 0.0)
                            _i_min2 = float(_budget.get('iMinLade2_w', _i_min) or 0.0)
                            _i_batt_load = float(_budget.get('iBattLoad_w', 0.0) or 0.0)
                            _i_max_batt_lade = float(_budget.get('iMaxBattLade_w', max_charge_w) or max_charge_w)
                            _i_ref = _i_fc if _i_min > _i_fc else _i_min
                            if _i_ref > _i_max_batt_lade or abs(_i_ref) < 1.0:
                                _i_ref = _i_max_batt_lade
                            if _i_min2 > 0 and _i_ref > _i_min2:
                                _i_ref = _i_min2
                            if _i_batt_load > 0 and _i_ref > _i_batt_load:
                                _i_ref = _i_batt_load
                            _f_av = float(_budget.get('fAvBatterie_w', battery_power_raw) or 0.0)
                            _f_av900 = float(_budget.get('fAvBatterie900_w', _f_av) or 0.0)
                            eba_i_power_w = -grid_power_budget_w + (_i_ref - (_f_av900 + _f_av) / 2.0) * -1.0
                            _eba_cap_w = max_charge_w * 0.9 + battery_power_raw - grid_power_budget_w
                            if eba_i_power_w > _eba_cap_w:
                                eba_i_power_w = _eba_cap_w
                    elif effective_wb_mode in (9, 10, 11):
                        _eba_pv_ok = (effective_wb_mode in (10, 11) or pv_power_raw > 100)
                        if wbminsoc_gate_open and _eba_pv_ok and soc_diff_for_wb > 0:
                            if effective_wb_mode in (10, 11):
                                # PV + Akku bis Untergrenze ist Grundladung-stabil
                                # ohne Grundladung: oberhalb der Floor-Hysterese
                                # darf der Speicher stützen, nahe am Floor aber
                                # nur noch weich reduziert statt bis zum letzten
                                # Zehntel Prozent volle Entladeleistung zu liefern.
                                eba_i_power_w = (
                                    wbminsoc_tapered_discharge_w
                                    if effective_wb_mode == 10
                                    else max_discharge_w
                                )
                            else:
                                eba_i_power_w = min(
                                    max_discharge_w * soc_diff_for_wb**3 / 4.0,
                                    max_discharge_w * 0.9
                                )
                            # C++: iPower += iPower_Bat - fPower_Grid*2
                            eba_i_power_w += battery_power_raw - grid_power_budget_w * 2.0
                            # C++: if (iPower > max*.9+iPower_Bat-fPower_Grid*2) cap
                            _eba_cap_base_w = (
                                wbminsoc_tapered_discharge_w
                                if effective_wb_mode == 10
                                else max_discharge_w
                            )
                            _eba_cap_factor = 1.0 if effective_wb_mode in (10, 11) else 0.9
                            eba_cap_w = _eba_cap_base_w * _eba_cap_factor + battery_power_raw - grid_power_budget_w * 2.0
                            if eba_i_power_w > eba_cap_w:
                                eba_i_power_w = eba_cap_w
                    if effective_wb_mode in (4, 9, 10, 11):
                        for _c in chargers:
                            _prev_iaval = float(_c.get('iAvalPower', 0.0) or 0.0)
                            _prev_wb_w = float(_c.get('_eba_prev_wb_power_w', wb_actual_power) or 0.0)
                            if abs(eba_i_power_w) / 2.0 > abs(_prev_iaval):
                                _prev_iaval = eba_i_power_w / 2.0
                            if abs(wb_actual_power - _prev_wb_w) > 1.0:
                                _prev_iaval = _prev_iaval - wb_actual_power + _prev_wb_w
                            if (_prev_iaval > 0 and eba_i_power_w > 0) or (_prev_iaval < 0 and eba_i_power_w < 0):
                                _new_iaval = _prev_iaval * 0.995 + eba_i_power_w * 0.025
                            else:
                                _new_iaval = _prev_iaval * 0.8 + eba_i_power_w * 0.2
                            if effective_wb_mode == 4 and pv_power_raw < 100 and _new_iaval > 0:
                                _new_iaval = 0.0
                            _cap_base_w = max_charge_w if effective_wb_mode == 4 else (
                                wbminsoc_tapered_discharge_w
                                if effective_wb_mode == 10
                                else max_discharge_w
                            )
                            _cap_factor = 1.0 if effective_wb_mode in (10, 11) else 0.9
                            eba_iaval_cap_w = _cap_base_w * _cap_factor + battery_power_raw - grid_power_budget_w
                            if _new_iaval > eba_iaval_cap_w:
                                _new_iaval = eba_iaval_cap_w
                            if battery_soc is not None and battery_soc < 5 and battery_power_raw < 0:
                                _new_iaval = battery_power_raw - grid_power_budget_w - (6 * 230.0 * detected_phases) / 6.0 - wb_actual_power
                            if effective_wb_mode in (10, 11):
                                # Original C++ akkumulierender Sonderpfad.
                                _new_iaval = _new_iaval * 0.9 + eba_i_power_w * 0.3
                            _c['iAvalPower'] = _new_iaval
                            _c['_eba_prev_wb_power_w'] = wb_actual_power
                            eba_iaval_w = _new_iaval
                            break

                    # --- Erlaubte WB-Leistung ---
                    active_openwb_pro_curve_ids = set()
                    for _cd in chargers:
                        _charger = _cd.get("charger")
                        if _charger is None or _charger.__class__.__name__ != "OpenWBProCharger":
                            continue
                        _cid = int(_cd.get("id", 0) or 0)
                        if normalize_wb_mode(wb_charge_mode.get(_cid, MODE_OFF)) != MODE_CURVE:
                            continue
                        _pro_status = {}
                        for _v in valid_chargers_status:
                            if int(_v.get("id", 0) or 0) == _cid:
                                _pro_status = _v.get("status") or {}
                                break
                        if (
                            _wb_status_connected(_pro_status)
                            and (
                                _wb_status_real_charging(_pro_status)
                                or _wb_status_real_power(_pro_status) > 250.0
                                or int(_cd.get("current_set_amp", 0) or 0) > 0
                                or (
                                    not wb_locked.get(_cid, False)
                                    and not wb_manual_pause.get(_cid, False)
                                    and max(
                                        0.0,
                                        float(free_for_limbs_w or 0.0),
                                        float(pv_surplus_ex_wb_w or 0.0),
                                        (
                                            float(raw_iaval_w or 0.0)
                                            if (
                                                storage_curve_budget_active
                                                and (
                                                    (
                                                        not storage_charge_priority_active
                                                        and not openwb_pro_curve_direct_storage_block
                                                    )
                                                    or openwb_pro_curve_direct_storage_soft_release
                                                )
                                            )
                                            else 0.0
                                        ),
                                    ) >= openwb_pro_curve_direct_start_min_w
                                )
                            )
                        ):
                            active_openwb_pro_curve_ids.add(_cid)

                    native_sun_capable = any(
                        _cd.get("charger")
                        and hasattr(_cd.get("charger"), "set_amp_sonnenmodus")
                        and not hasattr(_cd.get("charger"), "set_pv_mode")
                        for _cd in chargers
                        if normalize_wb_mode(
                            wb_charge_mode.get(
                                int(_cd.get("id", 0) or 0),
                                MODE_OFF,
                            )
                        ) != MODE_OFF
                    )
                    energy_policy = wallbox_policy.decide_energy_policy(
                        wallbox_policy.EnergyPolicyInput(
                            effective_wb_mode=effective_wb_mode,
                            effective_public_wb_mode=effective_public_wb_mode,
                            params=params,
                            fz=fz,
                            wb_max_amp=wb_max_amp,
                            detected_phases=detected_phases,
                            wb_actual_power=wb_actual_power,
                            free_for_limbs_w=free_for_limbs_w,
                            phys_surplus_w=phys_surplus_w,
                            raw_iaval_w=raw_iaval_w,
                            eba_iaval_w=eba_iaval_w,
                            wb_storage_cap_w=wb_storage_cap_w,
                            wb_storage_extra_w=wb_storage_extra_w,
                            pv_power_raw=pv_power_raw,
                            pv_only_allowed_w=pv_only_allowed_w,
                            pv_surplus_ex_wb_w=pv_surplus_ex_wb_w,
                            wbminsoc_gate_open=wbminsoc_gate_open,
                            price_boost_wallbox_active=price_boost_wallbox_active,
                            predump_wallbox_active=predump_wallbox_active,
                            predump_wallbox_gate_open=predump_wallbox_gate_open,
                            price_optimizing_active=price_optimizing_active,
                            effective_allow_grid=effective_allow_grid,
                            base_6a_active=base_6a_active,
                            curve_wb_relief_active=curve_wb_relief_active,
                            forecast_auto_relief_active=forecast_auto_relief_active,
                            storage_charge_priority_active=storage_charge_priority_active,
                            grid_unlocked_all_controllable=grid_unlocked_all_controllable,
                            controlled_wallbox_wbminsoc_pause=controlled_wallbox_wbminsoc_pause,
                            controlled_wallbox_wbminsoc_pv_only_active=controlled_wallbox_wbminsoc_pv_only_active,
                            wbminsoc_discharge_taper_active=wbminsoc_discharge_taper_active,
                            target_wbminsoc_discharge_pull_active=target_wbminsoc_discharge_pull_active,
                            target_wbminsoc_low_power_recovery_active=target_wbminsoc_low_power_recovery_active,
                            target_wbminsoc_low_power_start_ready=target_wbminsoc_low_power_start_ready,
                            forecast_auto_battery_assist=forecast_auto_battery_assist,
                            predump_wallbox_floor_block=predump_wallbox_floor_block,
                            target_wbminsoc_discharge_taper_limit_w=target_wbminsoc_discharge_taper_limit_w,
                            target_wbminsoc_low_power_min_w=target_wbminsoc_low_power_min_w,
                            target_wbminsoc_discharge_pull_limit_w=target_wbminsoc_discharge_pull_limit_w,
                            target_wbminsoc_low_power_bootstrap_w=target_wbminsoc_low_power_bootstrap_w,
                            forecast_auto_limit_w=forecast_auto_limit_w,
                            forecast_auto_min_w=_forecast_auto_min_w,
                            storage_curve_budget_active=storage_curve_budget_active,
                            wr_limit_config_w=config.get("wr_ac_limit_w", 11900),
                            ac_power_limit_live_w=ac_power_limit_live_w,
                            ext_pv_power_raw=ext_pv_power_raw,
                            home_w=home_w,
                            grid_reserve_w=grid_reserve_w,
                            battery_power_raw=battery_power_raw,
                            floor_discharge_threshold_w=config.get("wb_target_floor_battery_discharge_threshold_w", 700),
                            floor_down_step_w=config.get("wb_target_floor_battery_down_step_w", 230.0 * max(1, detected_phases)),
                            budget_ok=_budget_ok,
                            openwb_pro_curve_direct_possible=bool(active_openwb_pro_curve_ids),
                            openwb_pro_curve_direct_storage_block=openwb_pro_curve_direct_storage_block,
                            wallbox_curve_reserve_w=wallbox_curve_reserve_w,
                            grid_export_room_w=grid_export_room_w,
                            grid_import_for_budget_w=grid_import_for_budget_w,
                            grid_import_budget_down_active=grid_import_budget_down_active,
                            grid_import_w=grid_import_w,
                            native_sun_capable=native_sun_capable,
                            direct_marketing_active=direct_marketing_active,
                            direct_marketing_policy_target_state=_budget.get("direct_marketing_policy_target_state"),
                            openwb_pro_curve_direct_start_min_w=openwb_pro_curve_direct_start_min_w,
                        )
                    )
                    allowed_w = float(energy_policy["allowed_w"])
                    display_wb_budget_curve_w = float(energy_policy["display_wb_budget_curve_w"])
                    native_mode9_batt_start = bool(energy_policy["native_mode9_batt_start"])
                    mode5_pv_surplus_active = bool(energy_policy["mode5_pv_surplus_active"])
                    max_phys_wb_w = float(energy_policy["max_phys_wb_w"])
                    controlled_floor_battery_guard_active = bool(energy_policy["controlled_floor_battery_guard_active"])
                    controlled_floor_battery_discharge_w = float(energy_policy["controlled_floor_battery_discharge_w"])
                    controlled_floor_netpoint_limit_w = float(energy_policy["controlled_floor_netpoint_limit_w"])
                    curve_wb_relief_active = bool(energy_policy["curve_wb_relief_active"])
                    forecast_auto_relief_active = bool(energy_policy["forecast_auto_relief_active"])
                    openwb_pro_curve_direct_active = bool(energy_policy["openwb_pro_curve_direct_active"])
                    openwb_pro_curve_direct_w = float(energy_policy["openwb_pro_curve_direct_w"])
                    openwb_pro_curve_direct_pv_start_ready = bool(
                        energy_policy["openwb_pro_curve_direct_pv_start_ready"]
                    )
                    openwb_pro_curve_direct_real_pv_w = float(
                        energy_policy["openwb_pro_curve_direct_real_pv_w"]
                    )
                    openwb_pro_curve_direct_direct_marketing_block = bool(
                        energy_policy["openwb_pro_curve_direct_direct_marketing_block"]
                    )
                    grid_import_budget_clamp_active = bool(energy_policy["grid_import_budget_clamp_active"])

                    house_fuse_cap_amp = int(wb_max_amp)
                    house_fuse_limited = False
                    house_fuse_base_amp = 0.0
                    house_fuse_wb_count = 0
                    house_fuse_current_wb_amp = 0.0
                    if price_boost_wallbox_active or price_optimizing_active or effective_allow_grid:
                        (
                            house_fuse_cap_amp,
                            house_fuse_limited,
                            house_fuse_base_amp,
                            house_fuse_wb_count,
                            house_fuse_current_wb_amp,
                        ) = _wallbox_house_fuse_cap_amp(
                            live,
                            chargers,
                            valid_chargers_status,
                            wb_charge_mode,
                            effective_wb_mode,
                            grid_power_raw,
                            grid_max_amps,
                            wb_max_amp,
                            price_optimized_charger_ids,
                            price_boost_wallbox_active,
                            effective_allow_grid,
                        )
                        # Ab hier bedeutet das Flag: Deckel wurde in diesem
                        # Zyklus wirklich auf den Sollstrom angewendet.
                        house_fuse_limited = False

                    for _guard_data in chargers:
                        _guard_data["_f040_house_fuse_cap_amp"] = int(house_fuse_cap_amp)
                        _guard_data["_f040_connection_limit_ok"] = bool(
                            not _budget_live_sample_invalid
                            and int(house_fuse_cap_amp) >= int(wb_min_amp_cfg)
                        )
                        configured_kw = _sfloat(
                            config.get("wb%d_charge_power" % int(_guard_data.get("id", 0) or 0), 0),
                            0.0,
                        )
                        _guard_data["_f040_max_power_w"] = max(
                            0,
                            int(round(configured_kw * 1000.0)),
                        )

                    openwb_pro_budget_w_per_amp = 0.0
                    try:
                        _openwb_pro_w_per_amp_values = []
                        for _c in chargers:
                            _charger = _c.get("charger")
                            if _charger is None or _charger.__class__.__name__ != "OpenWBProCharger":
                                continue
                            _cid = int(_c.get("id", 0) or 0)
                            _st = next(
                                (_v.get("status") for _v in valid_chargers_status if int(_v.get("id", 0) or 0) == _cid),
                                None,
                            ) or {}
                            if not _wb_status_real_charging(_st):
                                continue
                            _ph = int(
                                _st.get("phases_in_use")
                                or _st.get("phase_effective_phases")
                                or detected_phases
                                or 1
                            )
                            _w_per_amp = _openwb_pro_effective_w_per_amp(
                                _st,
                                phases=_ph,
                                current_amp=_c.get("current_set_amp", 0.0),
                            )
                            if _w_per_amp > 0.0:
                                _openwb_pro_w_per_amp_values.append(_w_per_amp)
                        if len(_openwb_pro_w_per_amp_values) == 1:
                            openwb_pro_budget_w_per_amp = _openwb_pro_w_per_amp_values[0]
                    except Exception as _openwb_pro_wpa_e:
                        logger.debug("openWB-Pro W/A-Korrektur uebersprungen: %s" % _openwb_pro_wpa_e)

                    # Ampere-Deckel
                    budget_current_step_amp = _budget_current_step_amp_for_chargers(
                        chargers,
                        wb_charge_mode,
                        _effective_wb_locked,
                        wb_manual_pause,
                    )
                    current_decision = wallbox_decision.budget_to_target_current(
                        allowed_w=allowed_w,
                        detected_phases=detected_phases,
                        min_amp=6,
                        max_amp=wb_max_amp,
                        current_step_amp=budget_current_step_amp,
                        house_fuse_cap_amp=house_fuse_cap_amp,
                        apply_house_fuse=(
                            price_boost_wallbox_active
                            or price_optimizing_active
                            or effective_allow_grid
                        ),
                        base_6a_active=base_6a_active,
                        watts_per_amp=openwb_pro_budget_w_per_amp,
                    )
                    raw_cap = float(current_decision["target_amp"])
                    if budget_current_step_amp >= 0.99:
                        raw_cap = int(round(raw_cap))
                    cap_amp = raw_cap
                    house_fuse_limited = bool(current_decision["house_fuse_limited"])
                    storage_curve_reserve_active = bool(
                        wallbox_curve_reserve_w > 0
                        and effective_public_wb_mode == MODE_CURVE
                        and not (
                            price_boost_wallbox_active
                            or price_optimizing_active
                            or effective_allow_grid
                            or predump_wallbox_active
                        )
                    )
                    if house_fuse_limited:
                        _hf_now = time.time()
                        if runtime.should_log_house_fuse_limit(_hf_now, interval_s=30.0):
                            logger.warning(
                                "Hausabsicherung: Deckel %dA je WB (SLS %.0fA, Hauslast %.1fA, WB %d, aktuell %.1fA)" % (
                                    raw_cap,
                                    grid_max_amps,
                                    house_fuse_base_amp,
                                    house_fuse_wb_count,
                                    house_fuse_current_wb_amp,
                                )
                            )
                    if raw_cap > 0:
                        # Hochregelung begrenzen. Die WB startet erst mit 6A
                        # und folgt nach bestaetigter echter Leistung dem
                        # Budget in groben, fahrzeugschonenden Kaskaden.
                        # Erst danach greift die ruhige Halte-/Feintrimm-
                        # Regelung.
                        storage_guided_ramp = (
                            not (price_boost_wallbox_active or price_optimizing_active or effective_allow_grid)
                            and (
                                bat_charge_request_w > 0
                                or float(_budget.get('wb_fine_trim_step_w', 0) or 0) > 0
                                or _budget_state in ('charge', 'wbmin_charge_recovery', 'ifc_grid_hold')
                            )
                        )
                        storage_fine_step_w = float(_budget.get('wb_fine_trim_step_w', 0) or 0)
                        storage_fine_count = int(float(_budget.get('wb_fine_next_step_count', 0) or 0))
                        _ramp_now = time.time()
                        if predump_wallbox_active and all(
                            int(_c.get("current_set_amp", 0) or 0) <= 0
                            for _c in chargers
                        ):
                            # Ruhiger Start: erst 6A, danach greift die 1A/7s
                            # Predump-Rampe. So bekommt die openWB Pro Zeit,
                            # echte Leistung zu melden, bevor Speicherleistung
                            # nachgeschoben wird.
                            raw_cap = min(raw_cap, 6)
                        if (
                            effective_wb_mode != MODE_OFF
                            and not (price_boost_wallbox_active or price_optimizing_active or effective_allow_grid)
                            and all(
                                int(_c.get("current_set_amp", 0) or 0) <= 0
                                for _c in chargers
                            )
                        ):
                            # Die Wallbox ist das traegste Stellglied: Erst
                            # mit 6A echte Leistung erzeugen, dann auf das
                            # berechnete Budget springen und erst danach
                            # nachregeln.
                            raw_cap = min(raw_cap, 6)
                        for _c in chargers:
                            _cur = _c.get("current_set_amp", 0)
                            _charger_cls = _c.get("charger").__class__.__name__ if _c.get("charger") is not None else ""
                            _openwb_like_ramp = _charger_cls in ("OpenWBCharger", "OpenWBProCharger")
                            if predump_wallbox_active:
                                _ramp = 1
                            elif storage_guided_ramp and _openwb_like_ramp:
                                _ramp = max(1, min(8, int(_sf(config.get("wb_openwb_budget_ramp_a", 5), 5.0))))
                            elif storage_guided_ramp:
                                _ramp = 1
                            else:
                                _ramp = 4 if free_for_limbs_w >= 99000 else 2
                            _storage_guided_amp_up_hold_s = max(
                                10.0 if _openwb_like_ramp else 30.0,
                                _sf(config.get("wb_storage_guided_amp_up_hold_s", 45), 45.0),
                            )
                            if _openwb_like_ramp:
                                _storage_guided_amp_up_hold_s = min(
                                    _storage_guided_amp_up_hold_s,
                                    max(10.0, _sf(config.get("wb_openwb_budget_jump_hold_s", 15), 15.0)),
                                )
                            _stable_jump_done = bool(_c.get("_wb_stable_budget_jump_done", False))
                            _fast_block_active = bool(
                                _c.get('last_fast_ts', 0) > 0
                                and _ramp_now < float(_c.get('fast_block_until', 0.0) or 0.0)
                            )
                            if (
                                _cur > 0
                                and raw_cap < _cur
                                and storage_curve_reserve_active
                            ):
                                _down_ramp = max(
                                    1,
                                    min(4, int(_sf(config.get("wb_curve_reserve_down_ramp_a", 1), 1.0))),
                                )
                                raw_cap = max(raw_cap, _cur - _down_ramp)
                                break
                            if (
                                _cur > 0
                                and raw_cap > _cur
                                and storage_guided_ramp
                                and not _openwb_like_ramp
                                and _stable_jump_done
                                and storage_fine_step_w > 0
                                and storage_fine_count < 6
                            ):
                                raw_cap = _cur
                                break
                            if (
                                _cur > 0
                                and raw_cap > _cur
                                and storage_guided_ramp
                                and _stable_jump_done
                                and (_ramp_now - _c.get('last_storage_guided_amp_up_ts', 0.0)) < _storage_guided_amp_up_hold_s
                            ):
                                raw_cap = _cur
                                break
                            if _cur > 0 and raw_cap > _cur + _ramp and (
                                predump_wallbox_active
                                or (storage_guided_ramp and _stable_jump_done)
                                or _fast_block_active
                            ):
                                raw_cap = _cur + _ramp
                                break
                        cap_amp = raw_cap
                    elif storage_curve_reserve_active:
                        _down_ramp = max(
                            1,
                            min(4, int(_sf(config.get("wb_curve_reserve_down_ramp_a", 1), 1.0))),
                        )
                        _running_amp = max(int(_c.get("current_set_amp", 0) or 0) for _c in chargers) if chargers else 0
                        if _running_amp > 6:
                            cap_amp = max(6, _running_amp - _down_ramp)

                    if cap_amp == 0 and effective_wb_mode in (9, 10, 11) and system_connected and wbminsoc_gate_open:
                        # E3DC-native Wallboxen brauchen nach dem Startbefehl
                        # teils mehrere Zyklen, bis ALG/Phasenleistung echte
                        # Ladung bestaetigen. In dieser Zeit halten wir nur den
                        # gesetzten 6A-Deckel. Das ist kein Verbrauchswert fuer
                        # die Speicherregelung und darf keinen DISCH ausloesen.
                        _start_hold_now = time.time()
                        for _c in chargers:
                            _start_age = _start_hold_now - float(_c.get("last_start_ts", 0.0) or 0.0)
                            _grace_until = float(_c.get("_native_multi_start_grace_until", 0.0) or 0.0)
                            _held_amp = int(_c.get("current_set_amp", 0) or 0)
                            if _held_amp > 0 and (
                                0.0 <= _start_age < 180.0
                                or _start_hold_now < _grace_until
                            ):
                                cap_amp = 6
                                break

                    native_e3dc_charger_ids = {
                        int(_c.get("id", 0) or 0)
                        for _c in chargers
                        if (
                            hasattr(_c.get("charger"), "set_amp_sonnenmodus")
                            and not hasattr(_c.get("charger"), "set_pv_mode")
                        )
                    }
                    native_e3dc_connected = any(
                        int(_v.get("id", 0) or 0) in native_e3dc_charger_ids
                        and _wb_status_connected(_v.get("status"))
                        for _v in valid_chargers_status
                    )
                    native_e3dc_real_power_w = sum(
                        _wb_status_real_power(_v.get("status"))
                        for _v in valid_chargers_status
                        if (
                            int(_v.get("id", 0) or 0) in native_e3dc_charger_ids
                            and _wb_status_real_charging(_v.get("status"))
                        )
                    )

                    native_e3dc_start_without_power = bool(
                        effective_wb_mode in (9, 10, 11)
                        and native_e3dc_connected
                        and wbminsoc_gate_open
                        and (effective_wb_mode in (10, 11) or pv_power_raw > 100)
                        and abs(native_e3dc_real_power_w) <= 500
                    )
                    if native_e3dc_start_without_power and cap_amp > 0:
                        # Ohne echte Phasen-/Leistungsbestaetigung ist das nur
                        # eine Startfreigabe. Kein 32A-Phantom und kein Signal
                        # fuer Speicherentladung.
                        cap_amp = min(cap_amp, 6)

                    native_e3dc_real_charge = bool(
                        effective_wb_mode in (9, 10, 11)
                        and native_e3dc_connected
                        and wbminsoc_gate_open
                        and abs(native_e3dc_real_power_w) > 500
                    )
                    if native_e3dc_real_charge:
                        _native_current_amp = max(
                            [
                                int(_c.get("current_set_amp", 0) or 0)
                                for _c in chargers
                                if int(_c.get("id", 0) or 0) in native_e3dc_charger_ids
                            ] or [0]
                        )
                        _native_current_amp = max(6, _native_current_amp)
                        _w_per_amp = abs(native_e3dc_real_power_w) / max(1.0, float(_native_current_amp))
                        # C++-nah: sobald der Netzpunkt etwa eine Ampere-Stufe
                        # Reserve zeigt oder der Storage Manager explizit
                        # Speicherdeckel freigibt, darf die WB eine Stufe
                        # nachziehen. Die Batterie bleibt der schnelle Feinregler.
                        _storage_headroom = (
                            wb_storage_cap_w > 0
                            and wb_storage_cap_w >= (wb_actual_power + _w_per_amp + 250.0)
                        )
                        if _storage_headroom:
                            cap_amp = max(cap_amp, min(wb_max_amp, _native_current_amp + 1))
                        elif grid_power_budget_w < -(_w_per_amp + 80.0):
                            cap_amp = max(cap_amp, min(wb_max_amp, _native_current_amp + 1))
                        else:
                            cap_amp = max(cap_amp, _native_current_amp)

                    wb_priority_alloc = {}
                    wb_multi_contract = {}
                    try:
                        _alloc_modes = {
                            int(_cd.get("id", 0) or 0): (
                                0 if wb_locked.get(int(_cd.get("id", 0) or 0), False)
                                else int(wb_charge_mode.get(int(_cd.get("id", 0) or 0), 0) or 0)
                            )
                            for _cd in chargers
                        }
                        if (
                            len(chargers) > 1
                            and int(wb_dist_mode) in (0, 1, 2)
                            and cap_amp > 0
                        ):
                            _connected_count = sum(
                                1 for _v in valid_chargers_status
                                if _wb_status_connected(_v.get("status"))
                                and int(_alloc_modes.get(int(_v.get("id", 0) or 0), 0) or 0) > 0
                            )
                            if _connected_count > 1:
                                _allocation_budget_w = max(0.0, float(allowed_w or 0.0))
                                if price_optimized_charger_ids and not grid_unlocked_all_controllable:
                                    _allocation_budget_w = max(0.0, float(phys_surplus_w or 0.0))
                                wb_priority_alloc = allocate_power(
                                    _allocation_budget_w,
                                    valid_chargers_status,
                                    int(wb_dist_mode),
                                    int(wb_max_amp),
                                    _alloc_modes,
                                    price_optimized_charger_ids,
                                )
                        wb_multi_contract = wallbox_decision.multi_wallbox_allocation_contract(
                            valid_chargers_status,
                            priority_mode=int(wb_dist_mode),
                            allocations=wb_priority_alloc,
                            charge_modes=_alloc_modes,
                            grid_allowed_charger_ids=price_optimized_charger_ids,
                            min_amp=wb_min_amp_cfg,
                        )
                    except Exception as _prio_e:
                        logger.debug("Multi-WB Leistungsverteilung uebersprungen: %s" % _prio_e)
                        wb_multi_contract = {}

                    # PV-Netto-Surplus fuer Dashboard
                    pv_netto_surplus_w = max(0, int(-grid_power_budget_w + max(0, battery_power_raw)))
                    made_changes = False

                    # --- UI-State befuellen ---
                    _mode_status = mode_label(effective_public_wb_mode)
                    if price_boost_wallbox_active:
                        _mode_status = "Preisfenster: Wallbox freigegeben"
                    if market_wallbox_grid_active:
                        _mode_status = "Marktfenster: Wallbox freigegeben"
                    elif market_wallbox_active:
                        _mode_status = "Marktfenster wartet auf Sofort bis Preislimit"
                    if scheduled_slot_charger_ids:
                        _mode_status = "Geplanter Lade-Slot: Netzladen"
                    if effective_public_wb_mode == MODE_PRICE and not mode5_grid_allowed and not price_boost_wallbox_active:
                        _mode_status = (
                            "Sofort: PV-Überschuss ohne Netz"
                            if mode5_pv_surplus_active
                            else "Sofort wartet auf Preislimit"
                        )
                    if scheduled_slot_charger_ids:
                        _mode_status = "Geplanter Lade-Slot: Netzladen"
                    if predump_wallbox_active:
                        _mode_status = "Pre-Dump: WB nutzt Speicher (kein Netz)"
                    if battery_departure_active_ids:
                        _mode_status = "Akku bis Abfahrt: aktiv bis " + (battery_departure_label or "Abfahrt")
                    elif battery_departure_blocked_ids:
                        _mode_status = "Akku bis Abfahrt: wartet"
                    _operator_public_mode = (
                        MODE_BATTERY_DEPARTURE
                        if battery_departure_blocked_ids and effective_public_wb_mode == MODE_OFF
                        else effective_public_wb_mode
                    )
                    _operator_hint = _build_wallbox_operator_hint(
                        _operator_public_mode,
                        current_price,
                        dvcarlimit,
                        mode5_grid_allowed=mode5_grid_allowed,
                        price_boost_active=price_boost_wallbox_active,
                        market_plan_active=market_wallbox_grid_active,
                        market_plan_action=_market_wallbox_release.get("action"),
                        scheduled_slot_active=bool(scheduled_slot_charger_ids),
                        predump_wallbox_active=predump_wallbox_active,
                        budget_stale=_budget_stale,
                        budget_timeout=_budget_timeout,
                        budget_age_s=_budget_age_s,
                        house_fuse_limited=house_fuse_limited,
                        house_fuse_cap_amp=house_fuse_cap_amp,
                        connected=system_connected,
                        cap_amp=cap_amp,
                        battery_departure_active=bool(battery_departure_active_ids),
                        battery_departure_blocked=bool(battery_departure_blocked_ids),
                        battery_departure_label=battery_departure_label,
                        battery_departure_start_label=battery_departure_start_label,
                        battery_departure_reason=battery_departure_block_reason,
                    )
                    ui_state["status_msg"]      = _mode_status
                    ui_state.update(_operator_hint)
                    ui_state["wb_mode_active"]    = effective_public_wb_mode
                    ui_state["wb_control_mode"]   = effective_wb_mode
                    ui_state["wb_priority_mode"]  = int(wb_dist_mode)
                    ui_state["wb_native_distribution_mode"] = int(wb_dist_mode)
                    ui_state["wb_priority_label"] = (
                        "WB1" if int(wb_dist_mode) == 1 else
                        "WB2" if int(wb_dist_mode) == 2 else
                        "Beide"
                    )
                    ui_state["wb_multi_contract"] = wb_multi_contract
                    ui_state["wallbox_price_limit_ct"] = float(dvcarlimit)
                    try:
                        _current_price_for_ui = float(current_price)
                        if not math.isfinite(_current_price_for_ui):
                            _current_price_for_ui = None
                    except (TypeError, ValueError):
                        _current_price_for_ui = None
                    ui_state["current_price_ct"]  = _current_price_for_ui
                    ui_state["aval_power"]        = int(allowed_w)
                    ui_state["avail_wb_w"]        = int(allowed_w)
                    ui_state["wb_budget_raw_w"]   = int(max(0.0, free_for_limbs_w))
                    ui_state["wb_budget_curve_w"] = int(max(0.0, display_wb_budget_curve_w))
                    ui_state["wallbox_curve_reserve_w"] = int(max(0.0, wallbox_curve_reserve_w))
                    ui_state["wb_effective_budget_w"] = int(max(0.0, allowed_w))
                    ui_state["wb_effective_extra_w"] = int(max(0.0, allowed_w - max(0.0, wb_actual_power)))
                    ui_state["pv_surplus_w"]      = pv_netto_surplus_w
                    ui_state["grid_w_raw"]        = round(grid_power_raw, 0)
                    ui_state["grid_w_budget"]     = round(grid_power_budget_w, 0)
                    ui_state["grid_w_filtered"]   = round(get_float(live, "Grid_Power_Filtered", grid_power_raw), 0)
                    ui_state["grid_filter_active"] = bool(abs(grid_power_budget_w - grid_power_raw) > 1.0)
                    ui_state["bat_w_raw"]         = round(battery_power_raw, 0)
                    ui_state["live_sample_valid"] = bool(live_plausibility.get("sample_valid", True))
                    ui_state["live_sample_invalid"] = bool(live_sample_invalid)
                    ui_state["home_power_valid"]  = bool(live_plausibility.get("home_valid", True))
                    ui_state["grid_power_valid"]  = bool(live_plausibility.get("grid_valid", True))
                    ui_state["home_power_source"] = live_plausibility.get("home_source", "")
                    ui_state["home_power_balance_w"] = int(live_plausibility.get("home_balance_w", 0) or 0)
                    ui_state["home_power_delta_w"] = int(live_plausibility.get("home_delta_w", 0) or 0)
                    ui_state["grid_reserve_w"] = int(max(0.0, grid_reserve_w))
                    ui_state["openwb_pro_export_sink_available"] = bool(openwb_pro_export_sink_available)
                    ui_state["live_glitch_reasons"] = live_plausibility.get("reasons") if isinstance(live_plausibility.get("reasons"), list) else []
                    ui_state["budget_live_sample_invalid"] = bool(_budget_live_sample_invalid)
                    ui_state["wb_power_for_calc"] = round(wb_actual_power, 0)
                    ui_state["wb_power_source"]   = wb_power_source
                    ui_state["wb_min_pwr"]        = int(wb_min_power_meas)
                    ui_state["fuzzy_delta"]       = round(fuzzy_delta, 2)
                    ui_state["fuzzy_factor"]      = round(fz, 3)
                    ui_state["fuzzy_band_up"]     = round(band_up, 2)
                    ui_state["fuzzy_band_dn"]     = round(band_dn_eff, 2)
                    ui_state["wb_soc_hysterese_pct"] = round(wb_soc_hyst_pct, 2)
                    ui_state["wbminsoc_restart_above_pct"] = round(
                        max(
                            wb_soc_hyst_pct,
                            _sf(config.get("wb_target_restart_above_wbminsoc_pct", 2.0), 2.0),
                        )
                        if effective_wb_mode in (9, 10) and not effective_allow_grid
                        else wb_soc_hyst_pct,
                        2,
                    )
                    ui_state["wbminsoc_gate_open"] = bool(wbminsoc_gate_open)
                    ui_state["curve_wbminsoc_gate_open"] = bool(curve_wbminsoc_gate_open)
                    ui_state["curve_wbminsoc_floor_guard_active"] = bool(curve_wbminsoc_floor_guard_active)
                    ui_state["controlled_wallbox_wbminsoc_pause"] = bool(controlled_wallbox_wbminsoc_pause)
                    ui_state["controlled_wallbox_wbminsoc_pv_only_active"] = bool(
                        controlled_wallbox_wbminsoc_pv_only_active
                    )
                    ui_state["wbminsoc_low_power_recovery_active"] = bool(target_wbminsoc_low_power_recovery_active)
                    ui_state["wbminsoc_low_power_start_ready"] = bool(target_wbminsoc_low_power_start_ready)
                    ui_state["wbminsoc_low_power_real_surplus_w"] = int(max(0.0, target_wbminsoc_low_power_real_surplus_w))
                    ui_state["wbminsoc_low_power_3p_ready_w"] = int(max(0.0, target_wbminsoc_low_power_3p_ready_w))
                    ui_state["wbminsoc_discharge_taper_active"] = bool(wbminsoc_discharge_taper_active)
                    ui_state["wbminsoc_discharge_taper_factor"] = round(float(wbminsoc_discharge_taper_factor or 0.0), 3)
                    ui_state["wbminsoc_discharge_taper_above_pct"] = round(float(wbminsoc_discharge_taper_above_pct or 0.0), 2)
                    ui_state["wbminsoc_discharge_taper_limit_w"] = int(max(0.0, target_wbminsoc_discharge_taper_limit_w))
                    ui_state["wbminsoc_discharge_pull_active"] = bool(target_wbminsoc_discharge_pull_active)
                    ui_state["wbminsoc_discharge_target_w"] = int(max(0.0, target_wbminsoc_discharge_target_w))
                    ui_state["wbminsoc_discharge_pull_w"] = int(max(0.0, target_wbminsoc_discharge_pull_w))
                    ui_state["wbminsoc_discharge_pull_limit_w"] = int(max(0.0, target_wbminsoc_discharge_pull_limit_w))
                    ui_state["phase_forecast_hold_active"] = bool(phase_forecast_hold_active)
                    ui_state["forecast_auto_relief_active"] = bool(forecast_auto_relief_active)
                    ui_state["forecast_auto_battery_assist"] = bool(forecast_auto_battery_assist)
                    ui_state["forecast_auto_limit_w"] = int(max(0.0, forecast_auto_limit_w))
                    ui_state["curve_forecast_wallbox_block_active"] = bool(curve_forecast_wallbox_block_active)
                    ui_state["curve_forecast_wallbox_stop_active"] = bool(curve_forecast_wallbox_stop_active)
                    ui_state["curve_forecast_wallbox_assist_allowed"] = bool(curve_forecast_wallbox_assist_allowed)
                    ui_state["curve_forecast_wallbox_reason"] = str(curve_forecast_guard.get("reason", ""))
                    ui_state["curve_forecast_wallbox_release_age_s"] = round(float(curve_forecast_wallbox_release_age_s or 0.0), 1)
                    ui_state["curve_forecast_evening_shortfall_wh"] = int(curve_forecast_guard.get("evening_shortfall_wh", 0) or 0)
                    ui_state["curve_forecast_target_gap_pct"] = float(curve_forecast_guard.get("target_gap_pct", 0.0) or 0.0)
                    ui_state["grid_import_budget_clamp_active"] = bool(grid_import_budget_clamp_active)
                    ui_state["grid_import_budget_down_active"] = bool(grid_import_budget_down_active)
                    ui_state["grid_import_budget_age_s"] = round(float(grid_import_budget_down_age_s or 0.0), 1)
                    ui_state["grid_import_budget_threshold_w"] = int(grid_import_down_threshold_w)
                    ui_state["openwb_pro_curve_direct_active"] = bool(openwb_pro_curve_direct_active)
                    ui_state["openwb_pro_curve_direct_w"] = int(max(0.0, openwb_pro_curve_direct_w))
                    ui_state["openwb_pro_curve_direct_pv_start_ready"] = bool(openwb_pro_curve_direct_pv_start_ready)
                    ui_state["openwb_pro_curve_direct_real_pv_w"] = int(max(0.0, openwb_pro_curve_direct_real_pv_w))
                    ui_state["openwb_pro_curve_direct_start_min_w"] = int(max(0.0, openwb_pro_curve_direct_start_min_w))
                    ui_state["openwb_pro_curve_direct_storage_soft_release"] = bool(
                        openwb_pro_curve_direct_storage_soft_release
                    )
                    ui_state["openwb_pro_curve_direct_direct_marketing_block"] = bool(
                        openwb_pro_curve_direct_direct_marketing_block
                    )
                    ui_state["cap_amp"]           = cap_amp
                    ui_state["soll_soc"]          = round(soll_soc_now, 1) if soll_soc_now is not None else None
                    ui_state["can_reach_target"]  = can_reach_target
                    ui_state["base_floor_reachable"] = bool(base_floor_reachable)
                    ui_state["detected_phases"]   = detected_phases
                    ui_state["grid_max_amps"]     = round(grid_max_amps, 1)
                    ui_state["house_fuse_cap_amp"] = int(house_fuse_cap_amp)
                    ui_state["house_fuse_limited"] = bool(house_fuse_limited)
                    ui_state["wb_max_amp"]        = int(wb_max_amp)
                    ui_state["wb_global_max_amp"] = int(wb_global_max_amp)
                    ui_state["eco_score"]         = round(eco_score_now, 1)
                    ui_state["dynamic_min_soc"]   = effective_wb_floor_soc
                    ui_state["bat_floor_soc"]     = effective_wb_floor_soc
                    ui_state["wbminsoc_configured_soc"] = round(float(wb_minsoc_cfg), 2)
                    ui_state["wbminsoc_effective_soc"] = round(float(effective_wb_floor_soc), 2)
                    ui_state["wbminsoc_floor_source"] = (
                        "e3dc_wallbox" if e3dc_wb_floor_clamp_active else "config"
                    )
                    ui_state["wbminsoc_floor_note"] = wbminsoc_floor_note
                    ui_state["predump_floor_hold_active"] = bool(predump_floor_hold_active)
                    # Preis-Optimierung Flag: storage_manager liest dies fuer EMS_IDLE-Entscheidung
                    ui_state["price_opt_active"]  = price_optimizing_active
                    ui_state["scheduled_slot_active"] = bool(scheduled_slot_charger_ids)
                    ui_state["battery_departure_active"] = bool(battery_departure_active_ids)
                    ui_state["battery_departure_blocked"] = bool(battery_departure_blocked_ids)
                    ui_state["battery_departure_ids"] = sorted(list(battery_departure_active_ids))
                    ui_state["battery_departure_blocked_ids"] = sorted(list(battery_departure_blocked_ids))
                    if battery_departure_states:
                        ui_state["battery_departure"] = {
                            str(_cid): {
                                "active": bool(_state.get("active")),
                                "blocked": bool(_state.get("blocked")),
                                "expired": bool(_state.get("expired")),
                                "reason": _state.get("reason"),
                                "departure_time": _state.get("departure_time"),
                                "start_time": _state.get("start_time"),
                                "window_h": _state.get("window_h"),
                                "remaining_s": round(float(_state.get("remaining_s", 0.0) or 0.0), 1),
                            }
                            for _cid, _state in battery_departure_states.items()
                        }
                    ui_state["price_boost_active"] = price_boost_wallbox_active
                    ui_state["mode5_pv_surplus_active"] = bool(mode5_pv_surplus_active)
                    ui_state["price_plan_storage_protect"] = price_plan_storage_protect_active
                    ui_state["predump_wallbox_active"] = predump_wallbox_active
                    ui_state["predump_wallbox_gate_open"] = bool(predump_wallbox_gate_open)
                    ui_state["predump_wallbox_floor_soc"] = round(predump_wallbox_floor_soc, 1) if predump_wallbox_floor_soc > 0 else None
                    ui_state["predump_wallbox_floor_gate_open"] = bool(predump_wallbox_floor_gate_open)
                    ui_state["predump_wallbox_floor_block"] = bool(predump_wallbox_floor_block)
                    ui_state["curve_wb_relief_active"] = bool(curve_wb_relief_active)
                    ui_state["curve_ref_soc"] = _budget.get("curve_ref_soc")
                    ui_state["curve_excess_pct"] = _budget.get("curve_excess_pct")
                    ui_state["storage_charge_reserve_w"] = int(storage_charge_reserve_w)
                    ui_state["wb_storage_cap_w"] = int(wb_storage_cap_w)
                    ui_state["wb_storage_extra_w"] = int(wb_storage_extra_w)

                    intent_now = time.time()
                    startable_connected = False
                    bev_full_blocked_connected = False
                    manual_pause_ids = sorted(
                        int(_cd.get("id"))
                        for _cd in chargers
                        if bool(wb_manual_pause.get(_cd.get("id"), False))
                    )
                    manual_pause_visible = False
                    unpaused_visible = False
                    for _v in valid_chargers_status:
                        _st = _v.get("status")
                        if not _wb_status_connected(_st):
                            continue
                        _vid = int(_v.get("id", 0) or 0)
                        if bool(wb_manual_pause.get(_vid, False)):
                            manual_pause_visible = True
                            continue
                        unpaused_visible = True
                        _cd = next((cd for cd in chargers if cd.get("id") == _v.get("id")), None)
                        if not _cd:
                            continue
                        if _cd.get("_bev_full_blocked", False):
                            bev_full_blocked_connected = True
                            continue
                        _cooldown_ts = float(_cd.get("abort_cooldown_ts", 0.0) or 0.0)
                        if intent_now - _cooldown_ts >= 60.0:
                            startable_connected = True
                            break

                    manual_pause_blocks_storage_intent = bool(
                        manual_pause_ids
                        and (
                            all(bool(wb_manual_pause.get(_cd.get("id"), False)) for _cd in chargers)
                            or (manual_pause_visible and not unpaused_visible)
                        )
                    )
                    openwb_primary_pv_mode_ids = {
                        int(_cd.get("id", 0) or 0)
                        for _cd in chargers
                        if (
                            int(_cd.get("id", 0) or 0) > 0
                            and (_cd.get("charger").__class__.__name__ == "OpenWBCharger")
                            and getattr(_cd.get("charger"), "primary_mode_enabled", False)
                            and normalize_wb_mode(
                                wb_charge_mode.get(
                                    int(_cd.get("id", 0) or 0),
                                    effective_public_wb_mode,
                                )
                            ) in (MODE_CURVE, MODE_TARGET)
                        )
                    }
                    openwb_primary_pv_mode_connected_ids = set()
                    if openwb_primary_pv_mode_ids:
                        for _v in valid_chargers_status:
                            _vid = int(_v.get("id", 0) or 0)
                            if (
                                _vid in openwb_primary_pv_mode_ids
                                and _wb_status_connected(_v.get("status"))
                                and not bool(wb_manual_pause.get(_vid, False))
                            ):
                                openwb_primary_pv_mode_connected_ids.add(_vid)
                    openwb_primary_pv_mode_intent = bool(
                        openwb_primary_pv_mode_connected_ids
                        and not effective_allow_grid
                        and not price_optimizing_active
                        and not price_boost_wallbox_active
                        and not predump_wallbox_active
                    )
                    ui_state["openwb_primary_pv_mode_active"] = bool(openwb_primary_pv_mode_intent)
                    ui_state["openwb_primary_pv_mode_ids"] = sorted(list(openwb_primary_pv_mode_connected_ids))
                    if manual_pause_blocks_storage_intent:
                        cap_amp = 0
                        allowed_w = 0.0
                        display_wb_budget_curve_w = 0.0
                        forecast_auto_relief_active = False
                        curve_wb_relief_active = False
                        target_wbminsoc_discharge_pull_active = False
                        target_wbminsoc_discharge_pull_w = 0.0
                        target_wbminsoc_discharge_pull_limit_w = 0.0
                        target_wbminsoc_low_power_recovery_active = False
                        target_wbminsoc_low_power_start_ready = False
                        ui_state["cap_amp"] = 0
                        ui_state["aval_power"] = 0
                        ui_state["avail_wb_w"] = 0
                        ui_state["wb_effective_budget_w"] = 0
                        ui_state["wb_effective_extra_w"] = 0
                        ui_state["wb_budget_curve_w"] = 0
                        ui_state["forecast_auto_relief_active"] = False
                        ui_state["curve_wb_relief_active"] = False
                        ui_state["status_msg"] = "Manuell pausiert"

                    if bev_full_blocked_connected and not charging_active_any and abs(wb_actual_power) <= 500:
                        cap_amp = 0
                        forecast_auto_relief_active = False
                        curve_wb_relief_active = False
                        ui_state["cap_amp"] = 0
                        ui_state["wb_effective_budget_w"] = 0
                        ui_state["wb_effective_extra_w"] = 0
                        ui_state["forecast_auto_relief_active"] = False
                        ui_state["curve_wb_relief_active"] = False
                        if effective_public_wb_mode == MODE_BATTERY_DEPARTURE:
                            ui_state["status_msg"] = "Akku bis Abfahrt: beendet"
                            ui_state["operator_hint"] = (
                                "Akku bis Abfahrt beendet: Ladeende oder Ziel erreicht."
                            )
                            ui_state["operator_hint_level"] = "secondary"
                            ui_state["operator_hint_code"] = "battery_departure_done"
                        else:
                            ui_state["status_msg"] = "Ladung beendet"
                            ui_state["operator_hint"] = (
                                "Ladung extern beendet. Neustart nach Umstecken/Moduswechsel."
                            )
                            ui_state["operator_hint_level"] = "secondary"
                            ui_state["operator_hint_code"] = "vehicle_charge_done"

                    car_active_for_intent = bool(
                        (system_connected or charging_active_any)
                        and not manual_pause_blocks_storage_intent
                    )
                    wb_start_intended = bool(
                        effective_wb_mode in (9, 10, 11)
                        and cap_amp > 0
                        and not manual_pause_blocks_storage_intent
                    )
                    wb_floor_pv_start_ready = _wbminsoc_floor_pv_start_ready(
                        control_mode=effective_wb_mode,
                        wbminsoc_gate_open=wbminsoc_gate_open,
                        cap_amp=cap_amp,
                        real_surplus_w=target_wbminsoc_low_power_real_surplus_w,
                        min_power_w=target_wbminsoc_low_power_min_w * max(1, detected_phases),
                        startable_connected=startable_connected,
                        manual_pause=manual_pause_blocks_storage_intent,
                        grid_allowed=(
                            effective_allow_grid
                            or price_optimizing_active
                            or price_boost_wallbox_active
                        ),
                    )
                    wb_start_requested = bool(
                        effective_wb_mode in (9, 10, 11)
                        and startable_connected
                        and not manual_pause_blocks_storage_intent
                        and (wbminsoc_gate_open or effective_wb_mode == 11 or wb_floor_pv_start_ready)
                        and (effective_wb_mode in (10, 11) or pv_power_raw > 100)
                    )
                    wb_real_charging_for_intent = bool(
                        system_connected
                        and not manual_pause_blocks_storage_intent
                        and (
                            charging_active_any
                            or abs(wb_actual_power) > 500
                        )
                    )
                    openwb_primary_pv_feed_requested = False
                    ui_state["charging_active"] = bool(wb_real_charging_for_intent)
                    ui_state["openwb_primary_pv_feed_requested"] = bool(openwb_primary_pv_feed_requested)
                    wb_active_for_intent = bool(wb_real_charging_for_intent)
                    # openWB Primary führt PV/PV+Akku selbst über den Netzpunkt.
                    # Nur Netzlade-/Preisfenster dürfen später Stromwerte senden.
                    openwb_primary_observe_only_intent = bool(openwb_primary_pv_mode_intent)
                    _set_command_gate_observe_only(chargers, openwb_primary_observe_only_intent)
                    battery_request = "none"
                    intent_reason = "wallbox_no_battery_request"
                    if manual_pause_blocks_storage_intent:
                        battery_request = "release"
                        intent_reason = "manual_pause"
                    elif bev_full_blocked_connected and not wb_real_charging_for_intent:
                        battery_request = "release"
                        intent_reason = "bev_full_blocked"
                    elif predump_wallbox_floor_block and not effective_allow_grid:
                        battery_request = "hold_discharge"
                        intent_reason = "predump_floor_hold"
                    elif not car_active_for_intent:
                        battery_request = "release"
                        intent_reason = "no_vehicle_connected"
                    elif (
                        scheduled_slot_charger_ids
                        and (
                            wb_real_charging_for_intent
                            or wb_start_requested
                            or (system_connected and cap_amp > 0)
                        )
                    ):
                        battery_request = "hold_discharge"
                        intent_reason = "slot_grid_storage_protection"
                    elif (
                        effective_allow_grid
                        and not wbminsoc_gate_open
                        and (
                            wb_real_charging_for_intent
                            or wb_start_requested
                            or (system_connected and cap_amp > 0)
                        )
                    ):
                        battery_request = "hold_discharge"
                        intent_reason = "price_plan_storage_protection"
                    elif (
                        effective_wb_mode in (4, 9, 10, 11)
                        and not effective_allow_grid
                        and not wbminsoc_gate_open
                        and (
                            wb_real_charging_for_intent
                            or wb_start_requested
                            or (system_connected and cap_amp > 0)
                        )
                    ):
                        battery_request = "none"
                        intent_reason = "wbminsoc_floor_pv_curve"
                    elif price_boost_wallbox_active or effective_allow_grid:
                        battery_request = "release"
                        intent_reason = "grid_allowed_mode"
                    elif price_plan_storage_protect_active and (
                        wb_real_charging_for_intent
                        or wb_start_requested
                        or (system_connected and cap_amp > 0)
                    ):
                        battery_request = "hold_discharge"
                        intent_reason = "price_plan_storage_protection"
                    elif openwb_primary_pv_mode_intent and (
                        wb_real_charging_for_intent
                        or wb_start_requested
                        or (system_connected and cap_amp > 0)
                    ):
                        battery_request = "none"
                        intent_reason = "openwb_primary_pv_ems_only"
                    elif predump_wallbox_active and predump_wallbox_gate_open:
                        battery_request = "allow_discharge"
                        intent_reason = "predump_wallbox_consumer"
                    elif curve_wb_relief_active and (
                        wb_real_charging_for_intent
                        or wb_start_requested
                        or (system_connected and cap_amp > 0)
                    ):
                        battery_request = "allow_discharge"
                        intent_reason = "curve_wb_relief"
                    elif forecast_auto_relief_active and forecast_auto_battery_assist and (
                        wb_real_charging_for_intent
                        or wb_start_requested
                        or (system_connected and cap_amp > 0)
                    ):
                        battery_request = "allow_discharge"
                        intent_reason = "forecast_auto_relief"
                    elif forecast_auto_relief_active and (
                        wb_real_charging_for_intent
                        or wb_start_requested
                        or (system_connected and cap_amp > 0)
                    ):
                        battery_request = "none"
                        intent_reason = "forecast_auto_pv_only"
                    elif (
                        effective_wb_mode in (4, 9, 10, 11)
                        and not effective_allow_grid
                        and not wbminsoc_gate_open
                        and wb_active_for_intent
                    ):
                        battery_request = "none"
                        intent_reason = "wbminsoc_floor_pv_curve"
                    elif (
                        effective_wb_mode in (4, 9, 10, 11)
                        and wbminsoc_gate_open
                        and (
                            wb_real_charging_for_intent
                            or wb_start_requested
                            or (system_connected and cap_amp > 0)
                        )
                    ):
                        battery_request = "allow_discharge"
                        intent_reason = "wbminsoc_gate_open_start_or_real_charge"
                    ui_state["bev_full_blocked"] = bool(bev_full_blocked_connected)
                    ui_state["market_plan_wallbox_active"] = bool(market_wallbox_grid_active)
                    ui_state["market_plan_wallbox_available"] = bool(market_wallbox_active)
                    ui_state["market_plan_wallbox_price_mode_ids"] = sorted(list(_market_wallbox_grid_ids))
                    ui_state["market_plan_action"] = _market_wallbox_release.get("action")

                    ui_state["battery_request"] = battery_request
                    ui_state["manual_pause"] = bool(manual_pause_blocks_storage_intent)
                    ui_state["wb_floor_pv_start_ready"] = bool(wb_floor_pv_start_ready)
                    ui_state["manual_pause_ids"] = manual_pause_ids
                    ui_state["e3dc_wb_discharge_bat_until_soc"] = round(float(e3dc_wb_discharge_bat_until_soc), 2)
                    phase_transition_reservation = _wallbox_phase_transition_reservation(
                        chargers,
                        valid_chargers_status,
                        now_ts=intent_now,
                    )
                    ui_state["phase_transition_active"] = bool(
                        phase_transition_reservation.get("active")
                    )
                    ui_state["phase_transition_reserved_w"] = int(
                        phase_transition_reservation.get("reserved_w", 0) or 0
                    )
                    write_storage_intent({
                        "active": car_active_for_intent,
                        "battery_request": battery_request,
                        "reason": intent_reason,
                        "wb_mode_active": int(effective_public_wb_mode),
                        "wb_control_mode": int(effective_wb_mode),
                        "wbminsoc": float(wb_minsoc_cfg),
                        "effective_wb_floor_soc": float(effective_wb_floor_soc),
                        "e3dc_wb_discharge_bat_until_soc": float(e3dc_wb_discharge_bat_until_soc),
                        "wbminsoc_floor_source": "e3dc_wallbox" if e3dc_wb_floor_clamp_active else "config",
                        "wbminsoc_floor_note": wbminsoc_floor_note,
                        "external_wallbox_manager": bool(openwb_primary_observe_only_intent),
                        "openwb_primary_observe_only": bool(openwb_primary_observe_only_intent),
                        "openwb_primary_pv_mode_active": bool(openwb_primary_pv_mode_intent),
                        "openwb_primary_pv_mode_ids": sorted(list(openwb_primary_pv_mode_connected_ids)),
                        "openwb_primary_pv_feed_requested": bool(openwb_primary_pv_feed_requested),
                        "wb_soc_hysterese_pct": float(wb_soc_hyst_pct),
                        "wbminsoc_gate_open": bool(wbminsoc_gate_open),
                        "curve_wbminsoc_gate_open": bool(curve_wbminsoc_gate_open),
                        "curve_wbminsoc_floor_guard_active": bool(curve_wbminsoc_floor_guard_active),
                        "wbminsoc_discharge_taper_active": bool(wbminsoc_discharge_taper_active),
                        "wbminsoc_discharge_taper_factor": round(float(wbminsoc_discharge_taper_factor or 0.0), 3),
                        "wbminsoc_discharge_taper_limit_w": int(max(0.0, target_wbminsoc_discharge_taper_limit_w)),
                        "wbminsoc_discharge_pull_active": bool(target_wbminsoc_discharge_pull_active),
                        "wbminsoc_discharge_target_w": int(max(0.0, target_wbminsoc_discharge_target_w)),
                        "wbminsoc_discharge_pull_w": int(max(0.0, target_wbminsoc_discharge_pull_w)),
                        "wbminsoc_discharge_pull_limit_w": int(max(0.0, target_wbminsoc_discharge_pull_limit_w)),
                        "car_active": car_active_for_intent,
                        "charging_active": bool(wb_real_charging_for_intent),
                        "wb_power_w": 0.0 if manual_pause_blocks_storage_intent else float(wb_actual_power),
                        "cap_amp": int(cap_amp),
                        "start_requested": bool(wb_start_requested),
                        "wb_floor_pv_start_ready": bool(wb_floor_pv_start_ready),
                        "manual_pause": bool(manual_pause_blocks_storage_intent),
                        "manual_pause_ids": manual_pause_ids,
                        "bev_full_blocked": bool(bev_full_blocked_connected),
                        "detected_phases": int(detected_phases),
                        "phase_transition_active": bool(phase_transition_reservation.get("active")),
                        "phase_transition_reserved_w": int(phase_transition_reservation.get("reserved_w", 0) or 0),
                        "phase_transition_target_phases": int(phase_transition_reservation.get("target_phases", 0) or 0),
                        "phase_transition_targets": phase_transition_reservation.get("targets", []),
                        "phase_transition_charger_ids": phase_transition_reservation.get("charger_ids", []),
                        "phase_transition_started_ts": float(phase_transition_reservation.get("started_ts", 0.0) or 0.0),
                        "phase_transition_until_ts": float(phase_transition_reservation.get("expires_ts", 0.0) or 0.0),
                        "phase_transition_source": str(phase_transition_reservation.get("source") or ""),
                        "phase_transition_reservation": phase_transition_reservation,
                        "price_opt_active": bool(price_optimizing_active),
                        "scheduled_slot_active": bool(scheduled_slot_charger_ids),
                        "battery_departure_active": bool(battery_departure_active_ids),
                        "battery_departure_blocked": bool(battery_departure_blocked_ids),
                        "battery_departure_ids": sorted(list(battery_departure_active_ids)),
                        "battery_departure_blocked_ids": sorted(list(battery_departure_blocked_ids)),
                        "price_boost_active": bool(price_boost_wallbox_active),
                        "market_plan_wallbox_active": bool(market_wallbox_grid_active),
                        "market_plan_wallbox_available": bool(market_wallbox_active),
                        "market_plan_wallbox_price_mode_ids": sorted(list(_market_wallbox_grid_ids)),
                        "market_plan_action": _market_wallbox_release.get("action"),
                        "market_plan_reason": _market_wallbox_release.get("reason"),
                        "price_plan_storage_protect": bool(price_plan_storage_protect_active),
                        "curve_wb_relief_active": bool(curve_wb_relief_active),
                        "curve_ref_soc": _budget.get("curve_ref_soc"),
                        "curve_excess_pct": _budget.get("curve_excess_pct"),
                        "predump_wallbox_active": bool(predump_wallbox_active),
                        "predump_wallbox_gate_open": bool(predump_wallbox_gate_open),
                        "predump_wallbox_floor_soc": predump_wallbox_floor_soc if predump_wallbox_floor_soc > 0 else None,
                        "predump_wallbox_floor_gate_open": bool(predump_wallbox_floor_gate_open),
                        "predump_wallbox_floor_block": bool(predump_wallbox_floor_block),
                    })

                    # === Kein Auto: Status schreiben, Schleife ueberspringen ===
                    if not system_connected:
                        # Auch im Leerlauf die Einzel-WB-Liste schreiben, damit das Frontend
                        # bei Multi-Wallbox-Systemen nicht auf "eine Wallbox" zurueckfaellt.
                        ui_state["wb_details"] = _build_wallbox_detail_list(
                            chargers,
                            valid_chargers_status,
                            config,
                            public_mode=effective_public_wb_mode,
                            cap_amp=0,
                            allowed_w=0,
                            budget_stale=_budget_stale,
                            budget_timeout=_budget_timeout,
                            mode5_grid_allowed=mode5_grid_allowed,
                            scheduled_slot_active=bool(scheduled_slot_charger_ids),
                            price_boost_active=price_boost_wallbox_active,
                            predump_wallbox_active=predump_wallbox_active,
                            wbminsoc_gate_open=wbminsoc_gate_open,
                            house_fuse_limited=house_fuse_limited,
                            house_fuse_cap_amp=house_fuse_cap_amp,
                            detected_phases=detected_phases,
                            min_amp=wb_min_amp_cfg,
                            wb_global_max_amp=wb_global_max_amp,
                            battery_departure_states=battery_departure_states,
                            priority_mode=wb_dist_mode,
                            manual_pause=wb_manual_pause,
                        )
                        ui_state["status_msg"] = "Kein Fahrzeug verbunden"
                        ui_state.update(_build_wallbox_operator_hint(
                            effective_public_wb_mode,
                            current_price,
                            dvcarlimit,
                            connected=False,
                        ))
                        ui_state["charging_active"] = False
                        ui_state["set_amp"] = 0
                        ui_state["cap_amp"] = 0
                        ui_state["total_power_w"] = 0
                        ui_state["wb_power_for_calc"] = 0
                        ui_state["wb_power_source"] = "no_vehicle"
                        write_wallbox_decision_snapshot(ui_state, config, {
                            "intent_reason": intent_reason,
                            "battery_request": battery_request,
                            "made_changes": made_changes,
                            "budget_stale": _budget_stale,
                            "budget_timeout": _budget_timeout,
                            "storage_state": _budget_state,
                        })
                        write_status(ui_state)
                        for c_data in chargers:
                            c_id = c_data.get("id", 1)
                            c_public_mode = normalize_wb_mode(wb_charge_mode.get(c_id, effective_public_wb_mode))
                            if c_public_mode == 0:
                                _default_max_amp = _charger_max_amp(config, c_id, wb_global_max_amp)
                                if _consume_mode0_default_release_request(c_id):
                                    _release_wallbox_to_default_once(c_data, _default_max_amp, reason="mode0_user_switch")
                                c_data["is_charging"] = False
                                c_data["current_set_amp"] = 0
                                continue
                            if c_data["is_charging"]:
                                _send_wallbox_stop_command(
                                    c_data,
                                    c_id=c_id,
                                    reason="no_vehicle_connected",
                                )
                                c_data["is_charging"] = False
                                c_data["current_set_amp"] = 0
                        time.sleep(10)
                        continue

                    if battery_departure_blocked_ids:
                        for c_data in chargers:
                            c_id = int(c_data.get("id", 0) or 0)
                            if c_id not in battery_departure_blocked_ids:
                                continue
                            charger_status = next(
                                (v.get('status') for v in valid_chargers_status if v.get('id') == c_id),
                                None
                            )
                            hw_charging = _wb_status_real_charging(charger_status)
                            hw_power_w = _wb_status_real_power(charger_status)
                            try:
                                hw_offered_amp = int(round(float((charger_status or {}).get("amp", 0) or 0)))
                            except Exception:
                                hw_offered_amp = 0
                            reason = "battery_departure_deadline" if c_id in battery_departure_expired_ids else "battery_departure_wait"
                            stop_already_sent = bool(c_data.get("_wb_stop_sent_active", False))
                            if (
                                c_data.get("is_charging", False)
                                or int(c_data.get("current_set_amp", 0) or 0) > 0
                                or hw_charging
                                or abs(hw_power_w) > 250
                                or (hw_offered_amp > 0 and not stop_already_sent)
                            ):
                                _send_wallbox_stop_command(c_data, c_id=c_id, reason=reason)
                                logger.info(
                                    "WB%d Akku-bis-Abfahrt: Stop (%s, Abfahrt %s)" % (
                                        c_id,
                                        "Abfahrtszeit erreicht" if c_id in battery_departure_expired_ids else "ausserhalb Freigabefenster",
                                        battery_departure_states.get(c_id, {}).get("departure_time", BATTERY_DEPARTURE_DEFAULT_TIME),
                                    )
                                )
                                made_changes = True
                                last_change_ts[c_id] = time.time()
                            c_data["current_set_amp"] = 0
                            c_data["is_charging"] = False
                            c_data["_pv_mode_active"] = False
                            c_data["_wb_stop_sent_active"] = True
                            c_data["_predump_gate_stop_sent"] = False

                        if not controllable_charger_ids:
                            ui_state["wb_details"] = _build_wallbox_detail_list(
                                chargers,
                                valid_chargers_status,
                                config,
                                public_mode=MODE_BATTERY_DEPARTURE,
                                cap_amp=0,
                                allowed_w=0,
                                budget_stale=_budget_stale,
                                budget_timeout=_budget_timeout,
                                mode5_grid_allowed=False,
                                scheduled_slot_active=False,
                                price_boost_active=False,
                                predump_wallbox_active=False,
                                wbminsoc_gate_open=wbminsoc_gate_open,
                                house_fuse_limited=house_fuse_limited,
                                house_fuse_cap_amp=house_fuse_cap_amp,
                                detected_phases=detected_phases,
                                min_amp=wb_min_amp_cfg,
                                wb_global_max_amp=wb_global_max_amp,
                                battery_departure_states=battery_departure_states,
                                priority_mode=wb_dist_mode,
                                manual_pause=wb_manual_pause,
                            )
                            ui_state["status_msg"] = "Akku bis Abfahrt: wartet"
                            ui_state.update(_build_wallbox_operator_hint(
                                MODE_BATTERY_DEPARTURE,
                                current_price,
                                dvcarlimit,
                                connected=True,
                                battery_departure_blocked=True,
                                battery_departure_label=battery_departure_label,
                                battery_departure_start_label=battery_departure_start_label,
                                battery_departure_reason=battery_departure_block_reason,
                            ))
                            ui_state["charging_active"] = False
                            ui_state["set_amp"] = 0
                            ui_state["cap_amp"] = 0
                            write_wallbox_decision_snapshot(ui_state, config, {
                                "intent_reason": "battery_departure_blocked",
                                "battery_request": "release",
                                "made_changes": made_changes,
                                "budget_stale": _budget_stale,
                                "budget_timeout": _budget_timeout,
                                "storage_state": _budget_state,
                            })
                            write_status(ui_state)
                            time.sleep(10)
                            continue

                    logger.debug(
                        "Fuzzy M%d(%s): delta=%.1f%% fz=%.2f cap=%dA phases=%d free=%.0fW soll=%.1f%%" % (
                            effective_wb_mode,
                            mode_label(effective_public_wb_mode),
                            fuzzy_delta, fz, cap_amp, detected_phases,
                            free_for_limbs_w, soll_soc_now if soll_soc_now is not None else 0)
                    )

                    # === MODUS 0: NGNA / Python aus - Python schweigt ===
                    # Keine automatischen Wallbox-Schreibbefehle. Nur eine bewusst
                    # angeforderte Default-Freigabe aus der WebUI darf einmalig senden.
                    if effective_wb_mode == 0:
                        for cd in chargers:
                            _mode0_max_amp = _charger_max_amp(config, cd.get("id", 1), wb_global_max_amp)
                            if _consume_mode0_default_release_request(cd.get("id", 1)):
                                _release_wallbox_to_default_once(cd, _mode0_max_amp, reason="mode0_user_switch")
                        ui_state["wb_details"] = _build_wallbox_detail_list(
                            chargers,
                            valid_chargers_status,
                            config,
                            public_mode=effective_public_wb_mode,
                            cap_amp=0,
                            allowed_w=0,
                            budget_stale=_budget_stale,
                            budget_timeout=_budget_timeout,
                            mode5_grid_allowed=mode5_grid_allowed,
                            scheduled_slot_active=bool(scheduled_slot_charger_ids),
                            price_boost_active=price_boost_wallbox_active,
                            predump_wallbox_active=predump_wallbox_active,
                            wbminsoc_gate_open=wbminsoc_gate_open,
                            house_fuse_limited=house_fuse_limited,
                            house_fuse_cap_amp=house_fuse_cap_amp,
                            detected_phases=detected_phases,
                            min_amp=wb_min_amp_cfg,
                            wb_global_max_amp=wb_global_max_amp,
                            battery_departure_states=battery_departure_states,
                            priority_mode=wb_dist_mode,
                            manual_pause=wb_manual_pause,
                        )
                        write_wallbox_decision_snapshot(ui_state, config, {
                            "intent_reason": "mode0_python_off",
                            "battery_request": battery_request,
                            "made_changes": made_changes,
                            "budget_stale": _budget_stale,
                            "budget_timeout": _budget_timeout,
                            "storage_state": _budget_state,
                        })
                        write_status(ui_state)
                        time.sleep(10)
                        continue

                    # ================================================================
                    # FAST-GRID-CORRECTION: Schnelle Abregelung bei Netzbezug
                    # Wenn WR am Anschlag (z.B. 12kW PV) und WB zieht zu viel,
                    # ist der 30s-Hold zu traege. Dieser Pfad reduziert 1A/7s
                    # SOFORT bei Netzbezug - OHNE die 30s-Haltezeit zu brechen.
                    # Hochregeln bleibt weiterhin an MIN_HOLD_SECS gebunden!
                    # Fuzzy-Logik und Ausschalthysterese bleiben unangetastet.
                    # ================================================================
                    FAST_GRID_W        = 150   # Ab diesem Netzbezug fast-ramp
                    FAST_GRID_SECS     = 7     # Minimaler Abstand zwischen Fast-Schritten
                    now_ts = time.time()
                    fast_correction_done = False
                    _priority_export_fallback_active = False
                    _priority_export_fallback_w = 0.0
                    _priority_export_fallback_prio_id = 0
                    try:
                        _priority_export_fallback_prio_id = int(
                            (wb_multi_contract or {}).get("priority_target_id", 0) or 0
                        )
                        if _priority_export_fallback_prio_id not in (1, 2):
                            _priority_export_fallback_prio_id = (
                                int(wb_dist_mode) if int(wb_dist_mode) in (1, 2) else 0
                            )
                        _priority_target_active_for_fallback = bool(
                            (wb_multi_contract or {}).get(
                                "priority_target_active",
                                bool(_priority_export_fallback_prio_id),
                            )
                        )
                        if _priority_export_fallback_prio_id and _priority_target_active_for_fallback:
                            _prio_data = next(
                                (
                                    _c for _c in chargers
                                    if int(_c.get("id", 0) or 0) == _priority_export_fallback_prio_id
                                ),
                                None,
                            )
                            _prio_status = next(
                                (
                                    _v.get("status") for _v in valid_chargers_status
                                    if int(_v.get("id", 0) or 0) == _priority_export_fallback_prio_id
                                ),
                                None,
                            ) or {}
                            _prio_charger = (_prio_data or {}).get("charger")
                            _prio_class = _prio_charger.__class__.__name__ if _prio_charger is not None else ""
                            _prio_phase_capability = _wallbox_phase_switch_capability(
                                _prio_class,
                                _prio_status,
                                config,
                            )
                            _prio_vehicle_phases = _vehicle_max_ac_phases(
                                config,
                                _priority_export_fallback_prio_id,
                                _prio_status,
                            )
                            _prio_phase_contract = wallbox_decision.phase_observation_contract(
                                _prio_status,
                                _prio_data or {},
                                detected_phases=detected_phases,
                                vehicle_max_phases=_prio_vehicle_phases,
                                phase_capability=_prio_phase_capability,
                                charger_class_name=_prio_class,
                                driver_variant=str((_prio_status or {}).get("driver_variant", "") or ""),
                            )
                            _prio_phases = max(
                                1,
                                min(3, int(_prio_phase_contract.get("effective_phases", detected_phases) or detected_phases or 1)),
                            )
                            _prio_min_w = float(max(1, int(wb_min_amp_cfg or 6)) * 230.0 * _prio_phases)
                            _prio_budget_w = max(
                                0.0,
                                float(allowed_w or 0.0),
                                float(free_for_limbs_w or 0.0),
                                max(0.0, -float(grid_power_budget_w or 0.0)),
                            )
                            _priority_export_fallback_w = max(
                                0.0,
                                -float(grid_power_budget_w or 0.0) - max(0.0, float(grid_import_for_budget_w or 0.0)),
                            )
                            _prio_session_state = str(
                                (_prio_status or {}).get(
                                    "e3dc_session_state",
                                    (_prio_data or {}).get("_e3dc_session_state", ""),
                                )
                                or ""
                            ).lower()
                            _prio_offered = bool(
                                int((_prio_data or {}).get("current_set_amp", 0) or 0) >= int(wb_min_amp_cfg or 6)
                                or float((_prio_data or {}).get("_native_multi_start_grace_until", 0.0) or 0.0) > now_ts
                                or _prio_session_state in ("offered", "starting", "start_verifying")
                            )
                            _priority_export_fallback_active = bool(
                                _priority_target_active_for_fallback
                                and not _wb_status_real_charging(_prio_status)
                                and not _prio_offered
                                and _prio_budget_w < _prio_min_w
                                and _priority_export_fallback_w >= (float(max(1, int(wb_min_amp_cfg or 6))) * 230.0)
                            )
                    except Exception as _prio_fallback_e:
                        logger.debug("Prioritaets-Exportfallback uebersprungen: %s" % _prio_fallback_e)

                    for c_data in chargers:
                        c_id = c_data["id"]
                        if bool(wb_manual_pause.get(c_id, False)):
                            c_data["_manual_pause_active"] = True
                            runtime.reset_min_current_import_integral(c_id)
                            continue
                        c_data["_manual_pause_active"] = False
                        charger_max_amp = _charger_max_amp(config, c_id, wb_global_max_amp)
                        charger = c_data["charger"]
                        charger_class_name = charger.__class__.__name__
                        openwb_controller = charger_class_name == "OpenWBCharger"
                        openwb_like_charger = charger_class_name in ("OpenWBCharger", "OpenWBProCharger")
                        _fast_price_mode_grid_allowed = _wallbox_price_mode_grid_allowed_for_charger(
                            wb_charge_mode,
                            c_id,
                            mode5_grid_allowed,
                        )
                        local_grid_allowed = bool(
                            price_boost_wallbox_active
                            or c_id in price_optimized_charger_ids
                            or _fast_price_mode_grid_allowed
                        )
                        local_price_optimizing_active = local_grid_allowed
                        _fast_public_mode = normalize_wb_mode(
                            wb_charge_mode.get(c_id, effective_public_wb_mode)
                        )
                        _fast_control_mode = controller_mode(
                            _fast_public_mode,
                            grid_allowed=local_grid_allowed,
                        )
                        _fast_native_e3dc = bool(
                            hasattr(charger, "set_amp_sonnenmodus")
                            and not hasattr(charger, "set_pv_mode")
                        )
                        _fast_openwb_primary_pv_mode = bool(
                            openwb_controller
                            and getattr(charger, "primary_mode_enabled", False)
                            and _fast_public_mode in (MODE_CURVE, MODE_TARGET)
                            and not local_price_optimizing_active
                            and not local_grid_allowed
                            and not price_boost_wallbox_active
                            and not predump_wallbox_active
                        )
                        if wb_charge_mode.get(c_id, 0) == 0:
                            runtime.reset_min_current_import_integral(c_id)
                            continue
                        if _fast_openwb_primary_pv_mode:
                            # Normale openWB Primary fuehrt PV-Laden und Phasen
                            # selbst. Fast-Grid- und Mindeststrom-Stop wuerden
                            # sie aus dem PV-Regler reissen und danach den
                            # Command-Guard gegen den Restart laufen lassen.
                            c_data['last_fast_ts'] = 0
                            runtime.reset_min_current_import_integral(c_id)
                            continue
                        if (
                            wb_charge_mode.get(c_id, effective_wb_mode) == 9
                            and openwb_controller
                            and not local_price_optimizing_active
                            and not (wbminsoc_gate_open and wb_storage_cap_w > 0)
                        ):
                            # openWB Mode 9 ist Monitor-only: openWB regelt PV-Laden
                            # selbst per HTML/simpleAPI. Python darf hier auch keine
                            # Fast-Korrektur-Ampere senden, sonst stoeren wir den openWB-Regler.
                            c_data['last_fast_ts'] = 0
                            runtime.reset_min_current_import_integral(c_id)
                            continue
                        _fast_status = next(
                            (v.get('status') for v in valid_chargers_status if v.get('id') == c_id),
                            None
                        )
                        if not _wb_status_real_charging(_fast_status):
                            runtime.reset_min_current_import_integral(c_id)
                            continue
                        current_amp = c_data.get("current_set_amp", 0)
                        _fast_openwb_like = openwb_like_charger
                        if charger_class_name == "OpenWBProCharger":
                            _fast_hw_offered_amp = _sf((_fast_status or {}).get("amp", 0), 0.0)
                            if _fast_hw_offered_amp > float(current_amp or 0.0):
                                # Fuer die Pro ist der Hardwarewert die Wahrheit.
                                # Nach ignorierten/uebersteuerten POSTs darf der
                                # Fast-Pfad nicht auf unserem alten Sollwert
                                # rechnen, sonst bleibt real zu viel Strom stehen.
                                current_amp = _fast_hw_offered_amp
                        _fast_priority_export_fallback_charger = bool(
                            _priority_export_fallback_active
                            and c_id != _priority_export_fallback_prio_id
                            and not curve_forecast_wallbox_stop_active
                        )
                        _fast_openwb_direct_w = float(openwb_pro_curve_direct_w or 0.0)
                        if _fast_priority_export_fallback_charger and not openwb_pro_curve_direct_active:
                            _fast_openwb_direct_w = float(_priority_export_fallback_w or 0.0)
                        if (
                            charger_class_name == "OpenWBProCharger"
                            and (openwb_pro_curve_direct_active or _fast_priority_export_fallback_charger)
                            and _fast_public_mode == MODE_CURVE
                            and not local_price_optimizing_active
                            and not local_grid_allowed
                            and not predump_wallbox_active
                        ):
                            charger_status = _fast_status or {}
                            direct_phases = _openwb_pro_curve_direct_phases(
                                charger_status,
                                c_data,
                                detected_phases=detected_phases,
                                now_ts=now_ts,
                            )
                            direct_step_amp = _current_step_amp_for_charger(c_data.get("charger"), default=1.0)
                            direct_amp, direct_assist_gap_w = _openwb_pro_curve_direct_amp(
                                _fast_openwb_direct_w,
                                direct_phases,
                                charger_max_amp,
                                assist_allowed=bool(
                                    curve_forecast_wallbox_assist_allowed
                                    and not _fast_priority_export_fallback_charger
                                ),
                                assist_max_gap_w=_sf(
                                    config.get("wb_curve_direct_assist_max_gap_w", 230.0 * direct_phases + 80.0),
                                    230.0 * direct_phases + 80.0,
                                ),
                                current_step_amp=direct_step_amp,
                                watts_per_amp=_openwb_pro_effective_w_per_amp(
                                    charger_status,
                                    phases=direct_phases,
                                    current_amp=current_amp,
                                ),
                            )
                            direct_amp = _cap_openwb_pro_one_phase_amp(
                                direct_amp,
                                direct_phases,
                                config,
                                c_id,
                                charger_max_amp,
                            )
                            c_data["_openwb_pro_curve_direct_assist_w"] = direct_assist_gap_w
                            _fast_prio_target = wb_priority_alloc.get(c_id)
                            if (
                                isinstance(_fast_prio_target, dict)
                                and int(wb_dist_mode) in (1, 2)
                                and c_id != int(wb_dist_mode)
                                and not (local_grid_allowed or local_price_optimizing_active or price_boost_wallbox_active)
                                and not _fast_priority_export_fallback_charger
                            ):
                                _fast_prio_amp = float(_fast_prio_target.get("target_amp", 0) or 0)
                                _fast_prio_state = int(_fast_prio_target.get("state", 1) or 1)
                                if _fast_prio_state != 2 or _fast_prio_amp <= 0:
                                    direct_amp = 0
                                else:
                                    direct_amp = min(float(direct_amp or 0.0), _fast_prio_amp)
                            time_since_direct = now_ts - c_data.get('last_fast_ts', 0)
                            direct_delta = abs(float(direct_amp or 0.0) - float(current_amp or 0.0))
                            openwb_pro_curve_direct_down_trigger_w = max(
                                150.0,
                                min(700.0, grid_reserve_w * 0.5),
                            )
                            if (
                                direct_amp < current_amp
                                and grid_import_budget_down_active
                                and grid_power_raw > openwb_pro_curve_direct_down_trigger_w
                            ):
                                direct_direction = -1
                            elif direct_amp > current_amp and grid_power_raw < -900.0:
                                direct_direction = 1
                            else:
                                direct_direction = 0
                            direct_since_key = "_openwb_pro_curve_direct_since"
                            direct_last_direction = int(c_data.get("_openwb_pro_curve_direct_direction", 0) or 0)
                            if direct_direction == 0:
                                c_data[direct_since_key] = 0.0
                                c_data["_openwb_pro_curve_direct_direction"] = 0
                            elif direct_last_direction != direct_direction or c_data.get(direct_since_key, 0.0) <= 0.0:
                                c_data[direct_since_key] = now_ts
                                c_data["_openwb_pro_curve_direct_direction"] = direct_direction
                            direct_age_s = now_ts - float(c_data.get(direct_since_key, now_ts) or now_ts)
                            direct_down_hold_s = max(
                                10.0,
                                _sf(config.get("wb_openwb_pro_curve_down_hold_s", 10), 10.0),
                            )
                            direct_up_hold_s = max(
                                direct_down_hold_s,
                                _sf(config.get("wb_openwb_pro_curve_up_hold_s", 60), 60.0),
                            )
                            direct_min_delta_amp = max(
                                float(direct_step_amp or 1.0),
                                _sf(config.get("wb_openwb_pro_curve_min_delta_a", 0.5), 0.5),
                            )
                            direct_bulk_ready = _openwb_pro_direct_bulk_ready(
                                charger_status,
                                hw_charging=True,
                                stable_hw_power_w=float(charger_status.get('real_power_w', 0.0) or 0.0),
                            )
                            direct_required_s = (
                                direct_down_hold_s
                                if direct_direction < 0
                                else (0.0 if float(current_amp or 0.0) <= 0.0 else direct_up_hold_s)
                            )
                            direct_target_amp = _openwb_pro_direct_target_amp(
                                current_amp,
                                direct_amp,
                                direct_direction,
                                bulk_ready=direct_bulk_ready,
                                current_step_amp=direct_step_amp,
                            )
                            direct_due = bool(
                                direct_direction != 0
                                and direct_delta + 1e-6 >= direct_min_delta_amp
                                and direct_age_s >= direct_required_s
                                and time_since_direct >= FAST_GRID_SECS
                            )
                            if direct_due:
                                fs = 2 if float(current_amp or 0.0) <= 0.0 and direct_target_amp > 0 else None
                                if _execute_wallbox_driver_command(
                                    c_data,
                                    {
                                        "method": "set_amp_and_state",
                                        "amp": direct_target_amp,
                                        "force_state": fs,
                                        "reason": "openwb_pro_curve_direct",
                                    },
                                    c_id=c_id,
                                ):
                                    if direct_target_amp > 0:
                                        _mark_manager_charge_anchor(
                                            c_data,
                                            amp=direct_target_amp,
                                            reason="openwb_pro_curve_direct",
                                            reset_real_marker=bool(float(current_amp or 0.0) <= 0.0 and not hw_charging),
                                        )
                                    else:
                                        _mark_manager_zero_anchor(c_data, reason="openwb_pro_curve_direct_zero")
                                    c_data["current_set_amp"] = direct_target_amp
                                    c_data["is_charging"] = bool(direct_target_amp > 0)
                                    c_data["_wb_stop_sent_active"] = bool(direct_target_amp <= 0)
                                    c_data["_last_openwb_hold_amp"] = direct_target_amp
                                    c_data['last_fast_ts'] = now_ts
                                    c_data['fast_block_until'] = now_ts + max(
                                        direct_down_hold_s,
                                        _sf(config.get("wb_openwb_pro_curve_hold_s", 30), 30.0),
                                    )
                                    c_data[direct_since_key] = 0.0
                                    c_data["_openwb_pro_curve_direct_direction"] = 0
                                    last_change_ts[c_id] = now_ts
                                    logger.info(
                                        "WB%d openWB Pro PV-Kurve: %.1fA -> %.1fA "
                                        "(Grid=%+.0fW, Ziel=%.0fW, %dp, gehalten %.0fs)" % (
                                            c_id,
                                            float(current_amp or 0.0),
                                            float(direct_target_amp or 0.0),
                                            grid_power_raw,
                                            _fast_openwb_direct_w,
                                            direct_phases,
                                            direct_age_s,
                                        )
                                    )
                                    fast_correction_done = True
                            continue

                        if grid_power_raw > FAST_GRID_W and not local_price_optimizing_active and not local_grid_allowed:
                            _fast_physical_amp_clamp = _openwb_physical_amp_down_required(
                                _fast_public_mode,
                                current_amp,
                                cap_amp,
                                grid_power_raw,
                                charger_is_openwb_like=_fast_openwb_like,
                                grid_allowed=local_grid_allowed,
                                price_active=local_price_optimizing_active,
                                price_boost_active=price_boost_wallbox_active,
                                predump_active=predump_wallbox_active,
                                threshold_w=FAST_GRID_W,
                            )
                            _min_current_amp = max(6, int(wb_min_amp_cfg or 6))
                            _min_current_integral_candidate = bool(
                                int(current_amp or 0) <= _min_current_amp
                                and not predump_wallbox_active
                            )
                            if (
                                not grid_import_budget_down_active
                                and not _fast_physical_amp_clamp
                                and not _min_current_integral_candidate
                            ):
                                c_data["_fast_import_since"] = 0.0
                                continue
                            _fast_grid_budget_clamp = bool(
                                _fast_openwb_like
                                and _budget_ok
                                and cap_amp < current_amp
                                and (grid_import_budget_clamp_active or _fast_physical_amp_clamp)
                                and not (
                                    price_boost_wallbox_active
                                    or predump_wallbox_active
                                    or local_price_optimizing_active
                                    or local_grid_allowed
                                )
                                and (
                                    grid_import_budget_clamp_active
                                    or free_for_limbs_w <= max(120.0, grid_reserve_w)
                                    or str(_budget.get("storage_state", _budget_state) or _budget_state).startswith("parallel_curve")
                                )
                            )
                            if (
                                _fast_openwb_like
                                and (now_ts - last_change_ts.get(c_id, 0.0)) < 30.0
                                and grid_power_raw < 3500.0
                                and not _fast_grid_budget_clamp
                                and not _min_current_integral_candidate
                            ):
                                # openWB/openWB Pro erst einschwingen lassen:
                                # ein gesetzter Ampere-Wert braucht einige
                                # Sekunden bis Fahrzeug, EVSE und E3DC-Messung
                                # sichtbar zusammenpassen.
                                continue
                            _native_auto_step_wait = bool(
                                _fast_control_mode in (9, 10)
                                and _fast_native_e3dc
                                and wb_storage_cap_w > 0
                                and (now_ts - float(c_data.get('last_storage_guided_amp_up_ts', 0.0) or 0.0)) < 22.0
                                and grid_power_raw < 2500.0
                                and not _min_current_integral_candidate
                            )
                            if _native_auto_step_wait:
                                # Nach einem 1A-Schritt im E3DC-Sonnenmodus darf
                                # der interne AUTO-Regler kurz nachziehen. Sonst
                                # macht die Fast-Korrektur jeden C++-nahen Schritt
                                # sofort wieder rueckgaengig.
                                continue
                            _fast_import_since_key = "_fast_import_since"
                            if not c_data.get(_fast_import_since_key):
                                c_data[_fast_import_since_key] = now_ts
                            # Induktionsfeld/Kochfeld taktet oft nur wenige
                            # Sekunden. Darauf darf die WB nicht mit Ampere-
                            # Saege reagieren. Kleine Bezuege erst nach
                            # stabiler Dauer, grosse Bezuege sofort.
                            if (
                                grid_power_raw < 1500.0
                                and (now_ts - c_data.get(_fast_import_since_key, now_ts)) < 12.0
                                and not _fast_grid_budget_clamp
                                and not _min_current_integral_candidate
                            ):
                                continue
                            time_since_fast = now_ts - c_data.get('last_fast_ts', 0)
                            if time_since_fast >= FAST_GRID_SECS:
                                charger_status = next(
                                    (v.get('status') for v in valid_chargers_status if v.get('id') == c_id),
                                    None
                                )
                                fast_phases = int(charger_status.get('phases_in_use', 0)) if charger_status else 0
                                fast_phases = max(1, fast_phases or detected_phases or 1)
                                fast_phase_target = int(float((charger_status or {}).get("phases_target", 0) or 0))
                                fast_phase_actual = int(float((charger_status or {}).get("phases_actual", 0) or 0))
                                if (
                                    _fast_openwb_like
                                    and phase_forecast_hold_active
                                    and max(fast_phases, fast_phase_target, fast_phase_actual) >= 3
                                    and not _fast_grid_budget_clamp
                                ):
                                    _buffer_s = max(
                                        15.0,
                                        _sf(config.get("wb_openwb_grid_buffer_s", 35), 35.0),
                                    )
                                    _buffer_w = max(
                                        3500.0,
                                        min(
                                            7000.0,
                                            float(max_discharge_w or 0.0) * 0.7 if float(max_discharge_w or 0.0) > 0 else 5000.0,
                                        ),
                                    )
                                    _recent_control_ts = max(
                                        float(c_data.get("_last_phase_switch_ts", 0.0) or 0.0),
                                        float(c_data.get("last_storage_guided_amp_up_ts", 0.0) or 0.0),
                                    )
                                    if (
                                        _recent_control_ts > 0.0
                                        and now_ts - _recent_control_ts < _buffer_s
                                        and grid_power_raw < _buffer_w
                                    ):
                                        c_data['last_fast_ts'] = now_ts
                                        c_data['fast_block_until'] = now_ts + 10.0
                                        _log_state_once(
                                            c_data,
                                            "openwb_grid_buffer",
                                            (fast_phases, int(grid_power_raw), int(_buffer_w)),
                                            "WB%d openWB Akku-Puffer: halte 3p nach Phasen-/Ampere-Sprung "
                                            "(Grid=+%.0fW < %.0fW, %.0fs Pufferfenster)" % (
                                                c_id, grid_power_raw, _buffer_w, _buffer_s
                                            ),
                                            min_interval_s=180.0,
                                        )
                                        fast_correction_done = True
                                        continue
                                if _min_current_integral_candidate:
                                    c_data["_grid_import_at_min_since"] = 0.0
                                    min_import_tolerance_w = max(
                                        0.0,
                                        _sf(config.get("wb_min_current_import_tolerance_w", 200), 200.0),
                                    )
                                    min_import_release_w = max(
                                        0.0,
                                        min(
                                            min_import_tolerance_w,
                                            _sf(config.get("wb_min_current_import_release_w", 80), 80.0),
                                        ),
                                    )
                                    min_import_stop_wh = max(
                                        5.0,
                                        _sf(config.get("wb_min_current_import_stop_wh", 40), 40.0),
                                    )
                                    min_import_debounce_s = max(
                                        3.0,
                                        min(
                                            30.0,
                                            _sf(config.get("wb_min_current_import_debounce_s", 8), 8.0),
                                        ),
                                    )
                                    min_import_max_step_s = max(
                                        3.0,
                                        min(
                                            30.0,
                                            _sf(config.get("wb_min_current_import_max_step_s", 10), 10.0),
                                        ),
                                    )
                                    min_import_status = runtime.update_min_current_import_integral(
                                        charger_id=c_id,
                                        grid_power_w=grid_power_raw,
                                        now_ts=now_ts,
                                        tolerance_w=min_import_tolerance_w,
                                        release_w=min_import_release_w,
                                        stop_wh=min_import_stop_wh,
                                        debounce_s=min_import_debounce_s,
                                        max_step_s=min_import_max_step_s,
                                    )
                                    min_import_wh = float(min_import_status.get("wh", 0.0) or 0.0)
                                    min_import_stable_s = float(min_import_status.get("stable_s", 0.0) or 0.0)
                                    c_data["_min_current_import_integral_wh"] = min_import_wh
                                    c_data["_min_current_import_stable_s"] = min_import_stable_s
                                    _is_openwb_pro_fast = charger_class_name == "OpenWBProCharger"
                                    min_import_decision = wallbox_decision.minimum_current_import_action(
                                        current_amp=current_amp,
                                        min_amp=_min_current_amp,
                                        grid_power_w=grid_power_raw,
                                        import_status=min_import_status,
                                        stop_wh=min_import_stop_wh,
                                        openwb_like_charger=_fast_openwb_like,
                                        phase_switch_supported=hasattr(c_data["charger"], "set_phases"),
                                        phase_count=fast_phases,
                                        phase_target=fast_phase_target,
                                        phase_actual=fast_phase_actual,
                                        phase_forecast_hold_active=phase_forecast_hold_active,
                                        phase_down_reup_block_s=_sf(
                                            config.get(
                                                "wb_phase_down_reup_block_s",
                                                180 if _is_openwb_pro_fast else 600,
                                            ),
                                            180.0 if _is_openwb_pro_fast else 600.0,
                                        ),
                                        phase_down_forecast_hold_s=_sf(
                                            config.get(
                                                "wb_phase_down_forecast_hold_s",
                                                600 if _is_openwb_pro_fast else 900,
                                            ),
                                            600.0 if _is_openwb_pro_fast else 900.0,
                                        ),
                                    )
                                    min_import_action = str(min_import_decision.get("action", ""))
                                    if min_import_action == "HOLD_MIN_CURRENT_IMPORT":
                                        hold_amp = int(max(6, min_import_decision.get("target_amp", current_amp or 6)))
                                        c_data["current_set_amp"] = hold_amp
                                        c_data["is_charging"] = True
                                        c_data["_wb_stop_sent_active"] = False
                                        c_data["_last_openwb_hold_amp"] = hold_amp
                                        c_data['last_fast_ts'] = now_ts
                                        c_data['fast_block_until'] = now_ts + float(
                                            min_import_decision.get("fast_block_s", 10.0) or 10.0
                                        )
                                        if bool(min_import_decision.get("forecast_hold", False)):
                                            _log_state_once(
                                                c_data,
                                                "forecast_phase_grid_hold",
                                                (
                                                    fast_phases,
                                                    int(grid_power_raw),
                                                    int(min_import_wh * 10.0),
                                                    int(min_import_stop_wh * 10.0),
                                                ),
                                                "WB%d Prognose-Halt: halte 3p/6A trotz Grid=+%.0fW, "
                                                "Import-Integral %.1f/%.1fWh" % (
                                                    c_id,
                                                    grid_power_raw,
                                                    min_import_wh,
                                                    min_import_stop_wh,
                                                ),
                                                min_interval_s=180.0,
                                            )
                                        else:
                                            _log_state_once(
                                                c_data,
                                                "min_current_import_integral_hold",
                                                (
                                                    int(grid_power_raw),
                                                    int(min_import_wh * 10.0),
                                                    int(min_import_stop_wh * 10.0),
                                                    int(min_import_stable_s),
                                                ),
                                                "WB%d Mindeststrom-Halt: Grid=+%.0fW, "
                                                "Import-Integral %.1f/%.1fWh, stabil %.0fs" % (
                                                    c_id,
                                                    grid_power_raw,
                                                    min_import_wh,
                                                    min_import_stop_wh,
                                                    min_import_stable_s,
                                                ),
                                                min_interval_s=120.0,
                                            )
                                        fast_correction_done = True
                                        continue
                                    if min_import_action == "SWITCH_1P_MIN_CURRENT_IMPORT":
                                        _fast_reup_block_s = float(min_import_decision.get("reup_block_s", 300.0) or 300.0)
                                        _fast_hold_amp = int(max(6, min_import_decision.get("target_amp", 6) or 6))
                                        if _execute_wallbox_driver_command(
                                            c_data,
                                            {
                                                "method": "set_phases",
                                                "phases": 1,
                                                "reason": "fast_grid_3p_to_1p",
                                            },
                                            c_id=c_id,
                                        ):
                                            _remember_phase_target(c_data, 1, now_ts, _fast_reup_block_s)
                                            c_data["_last_phase_switch_ts"] = now_ts
                                            c_data["_phase_change_seen_session"] = True
                                            c_data["_wb_stable_budget_jump_done"] = False
                                            c_data["_wb_stable_budget_jump_ts"] = 0.0
                                            c_data["last_storage_guided_amp_up_ts"] = now_ts
                                            c_data["_phase_down_since"] = 0.0
                                            c_data["_phase_up_budget_since"] = 0.0
                                            c_data["_phase_3p_pending_since"] = 0.0
                                            c_data["_phase_3p_block_until"] = max(
                                                float(c_data.get("_phase_3p_block_until", 0.0) or 0.0),
                                                now_ts + _fast_reup_block_s,
                                            )
                                            _fast_phase_hold_s = max(
                                                0.0,
                                                _wb_timing(config, c_id).get("phase_change_hold_s", 180.0),
                                            )
                                            c_data["_phase_1p_start_hold_until"] = now_ts + _fast_phase_hold_s
                                            c_data["current_set_amp"] = _fast_hold_amp
                                            c_data["is_charging"] = True
                                            c_data["_pv_mode_active"] = False
                                            c_data["_wb_stop_sent_active"] = False
                                            c_data["_last_openwb_hold_amp"] = _fast_hold_amp
                                            c_data['last_fast_ts'] = now_ts
                                            c_data['fast_block_until'] = now_ts + float(
                                                min_import_decision.get("fast_block_s", 60.0) or 60.0
                                            )
                                            last_change_ts[c_id] = now_ts
                                            _save_wallbox_phase_state(chargers)
                                            logger.info(
                                                "WB%d Fast-Grid: 3p bei 6A zu schwer "
                                                "(Grid=+%.0fW) -> 1p statt STOP, 3p-Sperre %.0fmin" % (
                                                    c_id, grid_power_raw, _fast_reup_block_s / 60.0
                                                )
                                            )
                                            fast_correction_done = True
                                            runtime.reset_min_current_import_integral(c_id)
                                            continue
                                    if min_import_action not in ("STOP_MIN_CURRENT_IMPORT", "SWITCH_1P_MIN_CURRENT_IMPORT"):
                                        c_data["_grid_import_at_min_since"] = 0.0
                                        runtime.reset_min_current_import_integral(c_id)
                                        continue
                                    _send_wallbox_stop_command(
                                        c_data,
                                        c_id=c_id,
                                        reason="min_current_import_integral",
                                    )
                                    c_data["current_set_amp"] = 0
                                    c_data["is_charging"] = False
                                    c_data["_pv_mode_active"] = False
                                    c_data["_wb_stop_sent_active"] = True
                                    c_data["_last_stop_toggle_ts"] = now_ts
                                    c_data['last_fast_ts'] = now_ts
                                    c_data['fast_block_until'] = now_ts + float(
                                        min_import_decision.get("fast_block_s", 60.0) or 60.0
                                    )
                                    last_change_ts[c_id] = now_ts
                                    runtime.reset_min_current_import_integral(c_id)
                                    logger.info(
                                        "WB%d Mindeststrom-Stop: Grid=+%.0fW bei %dA, "
                                        "Import-Integral %.1f/%.1fWh, stabil %.0fs -> STOP (%dp)" % (
                                            c_id,
                                            grid_power_raw,
                                            int(current_amp or 0),
                                            min_import_wh,
                                            min_import_stop_wh,
                                            min_import_stable_s,
                                            fast_phases,
                                        )
                                    )
                                    fast_correction_done = True
                                    continue
                                else:
                                    c_data["_grid_import_at_min_since"] = 0.0
                                    runtime.reset_min_current_import_integral(c_id)
                                _fast_step_amp = _current_step_amp_for_charger(c_data.get("charger"), default=1.0)
                                fast_grid_decision = wallbox_decision.fast_grid_current_reduction_action(
                                    current_amp=current_amp,
                                    cap_amp=cap_amp,
                                    max_amp=charger_max_amp,
                                    grid_power_w=grid_power_raw,
                                    phase_count=fast_phases,
                                    current_step_amp=_fast_step_amp,
                                    physical_amp_clamp=_fast_physical_amp_clamp,
                                    openwb_like_charger=_fast_openwb_like,
                                    direct_current_capable=hasattr(c_data["charger"], "set_direct_current"),
                                    sonnenmodus_capable=hasattr(c_data["charger"], "set_amp_sonnenmodus"),
                                    set_amp_and_state_capable=hasattr(c_data["charger"], "set_amp_and_state"),
                                    public_mode=_fast_public_mode,
                                    min_amp=wb_min_amp_cfg or 6,
                                    stable_after_fast_hold_s=_sf(
                                        config.get(
                                            "wb_stable_after_fast_hold_s",
                                            config.get("wb_curve_relief_after_fast_hold_s", 60),
                                        ),
                                        60.0,
                                    ),
                                )
                                fast_amp = fast_grid_decision.get("target_amp", current_amp)
                                fast_method = str(fast_grid_decision.get("method", "") or "")
                                if fast_method:
                                    fast_command = {
                                        "method": fast_method,
                                        "amp": fast_amp,
                                        "reason": "fast_grid_correction",
                                    }
                                    if fast_grid_decision.get("force_state", "") is None:
                                        fast_command["force_state"] = None
                                    _execute_wallbox_driver_command(
                                        c_data,
                                        fast_command,
                                        c_id=c_id,
                                    )
                                if fast_grid_decision.get("mark_charge_anchor", False):
                                    _mark_manager_charge_anchor(
                                        c_data,
                                        amp=fast_amp,
                                        reason="fast_grid_correction",
                                        reset_real_marker=False,
                                    )
                                logger.info("WB%d Fast-Korrektur: Grid=+%.0fW -> %sA->%sA (%dp)" % (
                                    c_id, grid_power_raw, _amp_text(current_amp), _amp_text(fast_amp), fast_phases))
                                c_data["current_set_amp"] = fast_amp
                                if _fast_openwb_like:
                                    c_data["_last_openwb_hold_amp"] = fast_amp
                                c_data['last_fast_ts'] = now_ts
                                c_data['last_storage_guided_amp_down_ts'] = now_ts
                                c_data['fast_block_until'] = now_ts + float(
                                    fast_grid_decision.get("fast_block_s", 25.0) or 25.0
                                )
                                if bool(fast_grid_decision.get("hold_up_after_fast", False)):
                                    # Nach einer Fast-Korrektur darf die
                                    # Wallbox nicht sofort wieder hochlaufen.
                                    # Sie bleibt die gehaltene Last; Speicher
                                    # und E3DC regeln zuerst den Netzpunkt.
                                    c_data['last_storage_guided_amp_up_ts'] = now_ts
                                    c_data['fast_block_until'] = max(
                                        float(c_data.get('fast_block_until', 0.0) or 0.0),
                                        now_ts + float(
                                            fast_grid_decision.get("stable_after_fast_hold_s", 60.0) or 60.0
                                        ),
                                    )
                                if bool(fast_grid_decision.get("update_last_change", False)):
                                    last_change_ts[c_id] = now_ts
                                # E3DC-native: last_change_ts NICHT aktualisieren;
                                # dort bleibt Hochregeln an 30s gebunden.
                                fast_correction_done = True
                        else:
                            c_data["_fast_import_since"] = 0.0
                            c_data['last_fast_ts'] = 0  # Reset: kein Netzbezug mehr
                            runtime.reset_min_current_import_integral(c_id)

                    # ================================================================
                    # Mode 4/9/10/11: C++-nahe iAvalPower-Rampe, aber mit
                    # wallboxschonendem Mindestintervall statt 7s-Takt.
                    # Normalmodi: 30s (E3DC-EMS braucht Einschwingzeit)
                    made_changes = False
                    active_chargers_count = 0
                    total_set_amp = 0
                    cycle_cap_amp = cap_amp

                    def _apply_stable_wallbox_amp_contract(
                        c_data,
                        proposed_amp,
                        current_amp,
                        real_power_w=0.0,
                        real_charging=False,
                    ):
                        stable_contract = wallbox_decision.stable_wallbox_amp_contract(
                            proposed_amp=proposed_amp,
                            current_amp=current_amp,
                            real_power_w=real_power_w,
                            real_charging=real_charging,
                            now_ts=now_ts,
                            charger_class_name=charger_class_name,
                            grid_power_w=grid_power_raw,
                            fast_grid_threshold_w=FAST_GRID_W,
                            budget_timeout=_budget_timeout,
                            storage_floor_mode_active=storage_floor_mode(c_public_mode),
                            grid_allowed=local_grid_allowed,
                            price_active=local_price_optimizing_active,
                            price_boost_active=price_boost_wallbox_active,
                            predump_active=predump_wallbox_active,
                            physical_amp_down_active=bool(c_data.get("_physical_amp_down_active", False)),
                            stable_budget_jump_done=bool(c_data.get("_wb_stable_budget_jump_done", False)),
                            last_storage_guided_amp_up_ts=c_data.get("last_storage_guided_amp_up_ts", 0.0),
                            last_storage_guided_amp_down_ts=c_data.get("last_storage_guided_amp_down_ts", 0.0),
                            fast_block_until=c_data.get("fast_block_until", 0.0),
                            stable_budget_jump_ts=c_data.get("_wb_stable_budget_jump_ts", 0.0),
                            last_openwb_grid_window_amp_up_ts=c_data.get(
                                "_last_openwb_grid_window_amp_up_ts", 0.0
                            ),
                            stable_follow_hold_s=_sf(
                                config.get(
                                    "wb_stable_follow_hold_s",
                                    config.get(
                                        "wb_curve_relief_follow_hold_s",
                                        config.get("wb_curve_relief_amp_up_hold_s", 25),
                                    ),
                                ),
                                25.0,
                            ),
                            openwb_budget_jump_hold_s=_sf(
                                config.get("wb_openwb_budget_jump_hold_s", 15),
                                15.0,
                            ),
                            stable_start_confirm_w=_sf(
                                config.get(
                                    "wb_stable_start_confirm_w",
                                    config.get("wb_curve_relief_start_confirm_w", 700),
                                ),
                                700.0,
                            ),
                            openwb_grid_window_ramp_a=_sf(
                                config.get("wb_openwb_grid_window_ramp_a", 5),
                                5.0,
                            ),
                            openwb_grid_window_ramp_hold_s=_sf(
                                config.get(
                                    "wb_openwb_grid_window_ramp_hold_s",
                                    config.get("wb_openwb_budget_jump_hold_s", 15),
                                ),
                                15.0,
                            ),
                            stable_budget_jump_max_a=_sf(
                                config.get("wb_stable_budget_jump_max_a", 5),
                                5.0,
                            ),
                            confirmed_start_direct_target=bool(
                                int(_sf(config.get("wb_confirmed_start_direct_target", 1), 1.0))
                            ),
                            storage_floor_amp_up_export_w=_sf(
                                config.get("wb_storage_floor_amp_up_export_w", 500),
                                500.0,
                            ),
                            storage_floor_amp_up_hold_s=_sf(
                                config.get("wb_storage_floor_amp_up_hold_s", 15),
                                15.0,
                            ),
                            stable_budget_jump_deadband_a=_sf(
                                config.get("wb_stable_budget_jump_deadband_a", 3),
                                3.0,
                            ),
                            stable_budget_jump_hold_s=_sf(
                                config.get(
                                    "wb_stable_budget_jump_hold_s",
                                    15 if charger_class_name in ("OpenWBCharger", "OpenWBProCharger") else 45,
                                ),
                                15.0 if charger_class_name in ("OpenWBCharger", "OpenWBProCharger") else 45.0,
                            ),
                        )
                        c_data["_stable_wallbox_amp_contract"] = stable_contract
                        for _key, _value in stable_contract.get("state_updates", {}).items():
                            c_data[_key] = _value
                        return int(stable_contract.get("applied_amp", proposed_amp) or 0)

                    for c_data in chargers:
                        c_data.pop("_decision_payload", None)
                        c_data.pop("_driver_command", None)
                        cap_amp = cycle_cap_amp
                        c_id = c_data["id"]
                        charger = c_data.get("charger")
                        charger_class_name = charger.__class__.__name__ if charger is not None else ""
                        c_data["_openwb_pro_config"] = config if charger_class_name == "OpenWBProCharger" else {}
                        charger_max_amp = _charger_max_amp(config, c_id, wb_global_max_amp)
                        wb_timing = _wb_timing(config, c_id, charger_class_name=charger_class_name)
                        c_data["_wallbox_timing_profile"] = wb_timing.get("profile", "generic")
                        c_data["_command_guard_start_stop_gap_s"] = wb_timing.get("command_start_stop_gap_s", 180.0)
                        c_data["_command_guard_phase_gap_s"] = wb_timing.get("command_phase_gap_s", 300.0)
                        restart_delay_s = float(wb_timing.get("restart_delay_s", 60.0) or 0.0)
                        min_charge_time_s = float(wb_timing.get("min_charge_time_s", 300.0) or 0.0)
                        cloud_stop_delay_s = float(wb_timing.get("cloud_stop_delay_s", 180.0) or 0.0)
                        phase_change_hold_s = float(wb_timing.get("phase_change_hold_s", 180.0) or 0.0)
                        current_change_hold_s = float(wb_timing.get("current_change_hold_s", 30.0) or 0.0)
                        cap_amp = min(int(cap_amp or 0), charger_max_amp)
                        price_mode_grid_allowed = _wallbox_price_mode_grid_allowed_for_charger(
                            wb_charge_mode,
                            c_id,
                            mode5_grid_allowed,
                        )
                        local_grid_allowed = bool(
                            price_boost_wallbox_active
                            or c_id in price_optimized_charger_ids
                            or price_mode_grid_allowed
                        )
                        local_price_optimizing_active = local_grid_allowed
                        c_mode = wb_charge_mode.get(c_id, 0)
                        c_public_mode = normalize_wb_mode(c_mode)
                        c_public_label = mode_label(c_public_mode)
                        c_control_mode = controller_mode(c_public_mode, grid_allowed=local_grid_allowed)
                        _refresh_openwb_secondary_heartbeat_if_due(
                            c_data,
                            c_public_mode,
                            now_ts=now_ts,
                            c_id=c_id,
                        )
                        if (
                            storage_charge_priority_active
                            and c_control_mode > 0
                            and not (
                                local_grid_allowed
                                or local_price_optimizing_active
                                or price_boost_wallbox_active
                                or predump_wallbox_active
                            )
                        ):
                            # Speicherprioritaet darf die Wallboxleistung spaeter
                            # schnell drosseln, aber nicht die physische Start-/
                            # Wolkenhaltezeit des Ladepunkts auf Heizstab-Takte
                            # kuerzen.
                            pass
                        priority_forced_stop = False
                        if curve_forecast_wallbox_stop_active and c_public_mode == MODE_CURVE:
                            priority_forced_stop = True
                        manual_pause_active = bool(wb_manual_pause.get(c_id, False))
                        c_data["_manual_pause_active"] = manual_pause_active
                        if manual_pause_active:
                            pause_status = next(
                                (v.get('status') for v in valid_chargers_status if v.get('id') == c_id),
                                None
                            )
                            pause_hw_charging = _wb_status_real_charging(pause_status)
                            pause_hw_power_w = _wb_status_real_power(pause_status)
                            try:
                                pause_hw_amp = int(round(float((pause_status or {}).get("amp", 0) or 0)))
                            except Exception:
                                pause_hw_amp = 0
                            pause_stop_due = (
                                c_data.get("is_charging", False)
                                or int(c_data.get("current_set_amp", 0) or 0) > 0
                                or pause_hw_charging
                                or pause_hw_power_w > 250.0
                                or (
                                    pause_hw_amp > 0
                                    and not bool(c_data.get("_wb_stop_sent_active", False))
                                )
                                or (
                                    bool(c_data.get("_wb_stop_sent_active", False))
                                    and pause_hw_power_w > 500.0
                                    and now_ts - float(c_data.get("_last_stop_toggle_ts", 0.0) or 0.0) >= 30.0
                                )
                            )
                            if pause_stop_due:
                                _send_wallbox_stop_command(
                                    c_data,
                                    c_id=c_id,
                                    reason="manual_pause",
                                )
                                last_change_ts[c_id] = now_ts
                                made_changes = True
                            c_data["current_set_amp"] = 0
                            c_data["is_charging"] = False
                            c_data["_pv_mode_active"] = False
                            c_data["_wb_stop_sent_active"] = True
                            c_data["_predump_gate_stop_sent"] = False
                            c_data["_native_multi_start_grace_until"] = 0.0
                            if charger_class_name in ("OpenWBCharger", "OpenWBProCharger"):
                                c_data["_last_openwb_hold_amp"] = 0
                            if charger_class_name == "OpenWBProCharger":
                                c_data["_openwb_pro_start_hold_until"] = 0.0
                                c_data["_openwb_pro_start_hold_amp"] = 0
                            _remember_phase_target(c_data, 0)
                            continue
                        if predump_floor_hold_active and c_id in predump_charger_ids:
                            charger = c_data["charger"]
                            _pf_charger_status = next(
                                (v.get('status') for v in valid_chargers_status if v.get('id') == c_id),
                                None
                            )
                            _pf_hw_charging = bool(_pf_charger_status and _pf_charger_status.get('charging', False))
                            _pf_hw_power_w = float((_pf_charger_status or {}).get('real_power_w', 0.0) or 0.0)
                            need_floor_stop = (
                                c_data.get("is_charging", False)
                                or c_data.get("current_set_amp", 0) > 0
                                or _pf_hw_charging
                                or _pf_hw_power_w > 250
                            )
                            if need_floor_stop:
                                _send_wallbox_stop_command(
                                    c_data,
                                    c_id=c_id,
                                    reason="predump_floor_hold",
                                )
                                logger.info("WB%d Pre-Dump-Untergrenze erreicht: Wallbox gestoppt, Speicherentladung bleibt frei" % c_id)
                            c_data["_predump_gate_stop_sent"] = True
                            c_data["_wb_stop_sent_active"] = True
                            c_data["_pv_mode_active"] = False
                            c_data["is_charging"] = False
                            c_data["current_set_amp"] = 0
                            c_data["cap_amp"] = 0
                            last_change_ts[c_id] = now_ts
                            continue
                        if predump_wallbox_active and predump_wallbox_gate_open and c_id in predump_charger_ids:
                            c_mode = max(c_mode, 10)
                            c_control_mode = max(c_control_mode, 10)
                        elif predump_wallbox_active and c_id in predump_charger_ids:
                            # Pre-Dump darf eine laufende WB nicht in E3DC-Autonom
                            # fallen lassen. Sonst laeuft eine native WB weiter,
                            # obwohl das Budget-Gate noch geschlossen ist.
                            charger = c_data["charger"]
                            _pd_charger_status = next(
                                (v.get('status') for v in valid_chargers_status if v.get('id') == c_id),
                                None
                            )
                            _pd_hw_charging = bool(_pd_charger_status and _pd_charger_status.get('charging', False))
                            _pd_hw_power_w = float((_pd_charger_status or {}).get('real_power_w', 0.0) or 0.0)
                            stop_already_sent = bool(c_data.get("_predump_gate_stop_sent", False))
                            stop_retry_due = (
                                stop_already_sent
                                and _pd_hw_charging
                                and _pd_hw_power_w > 500
                                and now_ts - c_data.get("_last_stop_toggle_ts", 0.0) >= 30.0
                            )
                            need_gate_stop = (
                                c_data.get("is_charging", False)
                                or c_data.get("current_set_amp", 0) > 0
                                or _pd_hw_charging
                                or _pd_hw_power_w > 500
                                or stop_retry_due
                            )
                            if need_gate_stop:
                                _send_wallbox_stop_command(
                                    c_data,
                                    c_id=c_id,
                                    reason="predump_wallbox_gate",
                                )
                                c_data["_predump_gate_stop_sent"] = True
                                c_data["_wb_stop_sent_active"] = True
                                c_data["_pv_mode_active"] = False
                                c_data["is_charging"] = False
                                c_data["current_set_amp"] = 0
                                c_data["_last_stop_toggle_ts"] = now_ts
                                last_change_ts[c_id] = now_ts
                                logger.info("WB%d Pre-Dump wartet: Budget-Gate geschlossen, Ladung gehalten" % c_id)
                            else:
                                c_data["_predump_gate_stop_sent"] = True
                                c_data["_wb_stop_sent_active"] = False
                                c_data["_pv_mode_active"] = False
                                c_data["is_charging"] = False
                                c_data["current_set_amp"] = 0
                            continue
                        if c_mode == 0:
                            charger = c_data["charger"]
                            if _consume_mode0_default_release_request(c_id):
                                _release_wallbox_to_default_once(c_data, charger_max_amp, reason="mode0_user_switch")
                            c_data["current_set_amp"] = 0
                            c_data["is_charging"] = False
                            c_data["_pv_mode_active"] = False
                            c_data["_wb_stop_sent_active"] = False
                            c_data["_predump_gate_stop_sent"] = False
                            _remember_phase_target(c_data, 0)
                            continue

                        _reset_mode0_default_release(c_data)
                        current_amp = c_data.get("current_set_amp", 0)
                        local_min_hold_secs = (
                            min(current_change_hold_s, 10.0)
                            if c_control_mode in (4, 9, 10, 11)
                            else current_change_hold_s
                        )
                        time_since_change = now_ts - last_change_ts.get(c_id, 0)
                        in_hold = (time_since_change < local_min_hold_secs and c_data.get("is_charging", False))
                        charger_status = next(
                            (v.get('status') for v in valid_chargers_status if v.get('id') == c_id),
                            None
                        )
                        hw_charging = bool(charger_status and charger_status.get('charging', False))
                        hw_charging = _wb_status_real_charging(charger_status)
                        stable_hw_power_w = _wb_status_real_power(charger_status)
                        hw_power_w = stable_hw_power_w
                        charger_connected = _wb_status_connected(charger_status)
                        current_phases = int(charger_status.get('phases_in_use', 0)) if charger_status else 0
                        current_phase_actual = int(charger_status.get('phases_actual', 0)) if charger_status else 0
                        live_vehicle_identity_key = _compact_vehicle_identifier(
                            (charger_status or {}).get("car_id")
                            or (charger_status or {}).get("vehicle_id")
                            or (charger_status or {}).get("rfid_tag")
                        )
                        vehicle_identity_key = _session_vehicle_key_from_status(c_id, charger_status)
                        previous_vehicle_key = c_data.get("_session_vehicle_key", "")
                        charge_end_vehicle_changed = False
                        if charger_connected and vehicle_identity_key:
                            if previous_vehicle_key and previous_vehicle_key != vehicle_identity_key:
                                charge_end_vehicle_changed = True
                                c_data["_session_1p_only"] = False
                                c_data["_phase_3p_block_until"] = 0.0
                                c_data["_phase_3p_pending_since"] = 0.0
                                c_data["_phase_up_budget_since"] = 0.0
                                c_data["_phase_down_since"] = 0.0
                                c_data["_openwb_cp_start_sent"] = False
                                c_data["_real_charge_since"] = 0.0
                                c_data["_aha_real_charge_confirmed"] = False
                                c_data["_aha_real_charge_confirmed_since"] = 0.0
                                c_data["_openwb_start_reject_soft_until"] = 0.0
                                c_data["_openwb_pro_phase_sequence"] = {}
                                c_data["_openwb_pro_phase_sequence_stage"] = ""
                                c_data["_openwb_pro_start_wakeup_allowed_after"] = 0.0
                                c_data["_openwb_pro_start_wakeup_pending"] = False
                                c_data["_openwb_pro_start_wakeup_count"] = 0
                                c_data["_manager_zero_anchor_active"] = False
                                c_data["abort_count"] = 0
                                c_data["abort_cooldown_ts"] = 0.0
                            c_data["_session_vehicle_key"] = vehicle_identity_key
                        elif not charger_connected:
                            _openwb_disconnected_since = float(c_data.get("_openwb_disconnected_since", 0.0) or 0.0)
                            if _openwb_disconnected_since <= 0.0:
                                c_data["_openwb_disconnected_since"] = now_ts
                            elif now_ts - _openwb_disconnected_since >= 120.0:
                                c_data["_openwb_cp_start_sent"] = False
                            c_data["_session_vehicle_key"] = ""
                            c_data["_real_charge_since"] = 0.0
                            c_data["_aha_real_charge_confirmed"] = False
                            c_data["_aha_real_charge_confirmed_since"] = 0.0
                            c_data["_openwb_start_reject_soft_until"] = 0.0
                            c_data["_openwb_pro_phase_sequence"] = {}
                            c_data["_openwb_pro_phase_sequence_stage"] = ""
                            c_data["_last_phase_switch_ts"] = 0.0
                            c_data["_openwb_pro_start_wakeup_allowed_after"] = 0.0
                            c_data["_openwb_pro_start_wakeup_pending"] = False
                            c_data["_openwb_pro_start_wakeup_count"] = 0
                            c_data["_manager_zero_anchor_active"] = False
                            _remember_phase_target(c_data, 0)
                        if charger_connected:
                            c_data["_openwb_disconnected_since"] = 0.0
                        else:
                            c_data["_phase_change_seen_session"] = False

                        charge_end_contract = _wallbox_charge_end_latch_contract(
                            c_data,
                            charger_status,
                            now_ts=now_ts,
                            config=config,
                            charger_id=c_id,
                            public_mode=c_public_mode,
                            allow_new_latch=False,
                            vehicle_changed=charge_end_vehicle_changed,
                            disconnected_release=not charger_connected,
                            mode_off=c_public_mode == MODE_OFF,
                            manager_stop_active=bool(c_data.get("_manager_zero_anchor_active", False)),
                        )
                        if str(charge_end_contract.get("action", "") or "") == "clear":
                            _save_wallbox_abort_state(chargers)

                        if local_grid_allowed:
                            c_data["_grid_session_active_last"] = True
                        elif c_data.get("_grid_session_active_last", False):
                            if _stop_grid_session_after_window(
                                c_data,
                                current_amp=current_amp,
                                hw_charging=hw_charging,
                                stable_hw_power_w=stable_hw_power_w,
                                now_ts=now_ts,
                                last_change_ts=last_change_ts,
                                c_id=c_id,
                            ):
                                made_changes = True
                                continue
                        charger = c_data["charger"]
                        charger_class_name = charger.__class__.__name__
                        c_data["_charger_class_name"] = charger_class_name
                        e3dc_native_toggle = (
                            hasattr(charger, "set_amp_sonnenmodus")
                            and not hasattr(charger, "set_pv_mode")
                        )
                        openwb_controller = charger_class_name == "OpenWBCharger"
                        openwb_primary_controller = bool(
                            openwb_controller and getattr(charger, "primary_mode_enabled", False)
                        )
                        openwb_pro = charger_class_name == "OpenWBProCharger"
                        openwb_like_charger = openwb_controller or openwb_pro
                        pv_curve_mode_switch_quiet_supported = bool(e3dc_native_toggle or openwb_pro)
                        _apply_pv_curve_mode_switch_quiet_request(
                            c_data,
                            c_id,
                            c_public_mode,
                            pv_curve_mode_switch_quiet_supported,
                            now_ts=now_ts,
                            hold_s=max(20.0, _sf(config.get("wb_pv_start_integral_s", 60), 60.0)),
                        )
                        _openwb_pro_priority_start_hold_active = bool(
                            openwb_pro
                            and _openwb_pro_start_hold_active(
                                c_data,
                                now_ts,
                                hw_charging=hw_charging,
                                stable_hw_power_w=stable_hw_power_w,
                            )
                        )
                        _openwb_pro_priority_offered_amp = max(
                            _sf((charger_status or {}).get("amp", 0), 0.0),
                            _sf((charger_status or {}).get("offered_current_raw", 0), 0.0),
                            _sf(c_data.get("current_set_amp", 0), 0.0),
                            _sf(c_data.get("_openwb_pro_start_hold_amp", 0), 0.0),
                        )
                        _openwb_pro_priority_recent_pv_age_s = (
                            now_ts - float(c_data.get("_openwb_curve_real_pv_seen_ts", 0.0) or 0.0)
                            if float(c_data.get("_openwb_curve_real_pv_seen_ts", 0.0) or 0.0) > 0.0
                            else 999999.0
                        )
                        _openwb_pro_priority_recent_start_active = bool(
                            openwb_pro
                            and 0.0 <= now_ts - float(c_data.get("last_start_ts", 0.0) or 0.0)
                            < _openwb_pro_start_hold_s(config)
                            and (
                                _openwb_pro_priority_offered_amp >= 6.0
                                or stable_hw_power_w > 500.0
                            )
                            and stable_hw_power_w <= max(
                                3000.0,
                                _openwb_pro_priority_offered_amp * 230.0 + 500.0,
                            )
                        )
                        if (
                            priority_forced_stop
                            and openwb_pro
                            and charger_connected
                            and not hw_charging
                            and (
                                (
                                    stable_hw_power_w <= 500.0
                                    and cap_amp > 0
                                    and openwb_pro_curve_direct_pv_start_ready
                                )
                                or (
                                    _openwb_pro_priority_start_hold_active
                                    and _openwb_pro_priority_offered_amp >= 6.0
                                    and grid_power_budget_w < 1500.0
                                    and (
                                        openwb_pro_curve_direct_pv_start_ready
                                        or _openwb_pro_priority_recent_pv_age_s < max(20.0, cloud_stop_delay_s)
                                        or grid_power_budget_w < -300.0
                                    )
                                )
                                or (
                                    _openwb_pro_priority_recent_start_active
                                    and grid_power_budget_w < 1500.0
                                    and (
                                        openwb_pro_curve_direct_pv_start_ready
                                        or _openwb_pro_priority_recent_pv_age_s < max(20.0, cloud_stop_delay_s)
                                        or grid_power_budget_w < -300.0
                                    )
                                )
                            )
                            and not bool(c_data.get("_bev_full_blocked", False))
                        ):
                            priority_forced_stop = False
                        openwb_primary_curve_frame_control = bool(
                            openwb_primary_controller
                            and c_public_mode == MODE_CURVE
                            and not local_price_optimizing_active
                            and not local_grid_allowed
                            and not price_boost_wallbox_active
                            and not predump_wallbox_active
                        )
                        if openwb_primary_curve_frame_control:
                            user_curve_request = _consume_wallbox_user_mode_request(c_id, MODE_CURVE)
                            if user_curve_request and hasattr(charger, "set_pv_mode"):
                                with command_gate.default_release_scope(charger, reason="mode2_user_switch_pv"):
                                    pv_ok = _execute_wallbox_driver_command(
                                        c_data,
                                        {
                                            "method": "set_pv_mode",
                                            "reason": "mode2_user_switch_pv",
                                        },
                                        c_id=c_id,
                                    )
                                c_data["_pv_mode_active"] = bool(pv_ok)
                                last_change_ts[c_id] = now_ts
                                made_changes = True
                                logger.info(
                                    "WB%d %s: openWB Primary einmalig auf PV-Modus; "
                                    "Ladepunkt bleibt openWB-geführt, E3DC-Control führt Speicher/Netzpunkt (ok=%s)" % (
                                        c_id,
                                        c_public_label,
                                        "ja" if pv_ok else "nein",
                                    )
                                )
                                c_data["_openwb_primary_pv_mode_set_ts"] = now_ts
                        openwb_primary_pv_mode_control = bool(
                            openwb_primary_controller
                            and charger_connected
                            and c_public_mode in (MODE_CURVE, MODE_TARGET)
                            and not priority_forced_stop
                            and not local_price_optimizing_active
                            and not local_grid_allowed
                            and not price_boost_wallbox_active
                            and not predump_wallbox_active
                        )
                        if openwb_primary_pv_mode_control:
                            _primary_chargemode = _openwb_primary_chargemode_key(
                                (charger_status or {}).get("chargemode_str")
                                or (charger_status or {}).get("chargemode")
                            )
                            _primary_pv_recent = bool(
                                now_ts - float(c_data.get("_openwb_primary_pv_mode_set_ts", 0.0) or 0.0) < 2.0
                            )
                            _primary_needs_pv = bool(
                                hasattr(charger, "set_pv_mode")
                                and not _primary_pv_recent
                                and (
                                    _primary_chargemode in ("stop", "instant")
                                    or not c_data.get("_pv_mode_active", False)
                                    or bool(c_data.get("_wb_stop_sent_active", False))
                                )
                            )
                            if _primary_needs_pv:
                                with command_gate.default_release_scope(charger, reason="openwb_primary_pv_mode"):
                                    pv_ok = _execute_wallbox_driver_command(
                                        c_data,
                                        {
                                            "method": "set_pv_mode",
                                            "reason": "openwb_primary_pv_mode",
                                        },
                                        c_id=c_id,
                                    )
                                c_data["_pv_mode_active"] = bool(pv_ok)
                                c_data["_openwb_primary_pv_mode_set_ts"] = now_ts
                                last_change_ts[c_id] = now_ts
                                made_changes = True
                                logger.info(
                                    "WB%d %s: openWB Primary bleibt im PV-Modus; "
                                    "Speicher und Netzpunkt führen die Unterstützung (ok=%s)" % (
                                        c_id,
                                        c_public_label,
                                        "ja" if pv_ok else "nein",
                                    )
                                )
                            else:
                                c_data["_pv_mode_active"] = True
                            c_data["_openwb_primary_pv_mode_control"] = True
                            c_data["_wb_stop_sent_active"] = False
                            c_data["is_charging"] = hw_charging
                            c_data["current_set_amp"] = (
                                int(charger_status.get("amp", 0))
                                if hw_charging and charger_status else 0
                            )
                            if hw_charging:
                                active_chargers_count += 1
                                total_set_amp += c_data.get("current_set_amp", 0)
                            continue
                        _priority_export_fallback_charger = bool(
                            _priority_export_fallback_active
                            and c_id != _priority_export_fallback_prio_id
                            and not curve_forecast_wallbox_stop_active
                        )
                        openwb_pro_curve_direct_local_w = float(openwb_pro_curve_direct_w or 0.0)
                        if _priority_export_fallback_charger and not openwb_pro_curve_direct_active:
                            openwb_pro_curve_direct_local_w = float(_priority_export_fallback_w or 0.0)
                        openwb_pro_curve_direct_charger = bool(
                            openwb_pro
                            and (openwb_pro_curve_direct_active or _priority_export_fallback_charger)
                            and c_public_mode == MODE_CURVE
                            and not local_price_optimizing_active
                            and not local_grid_allowed
                            and not price_boost_wallbox_active
                            and not predump_wallbox_active
                        )
                        if (
                            cap_amp > 0
                            and restart_delay_s > 0.0
                            and charger_connected
                            and int(current_amp or 0) <= 0
                            and not hw_charging
                            and not local_grid_allowed
                            and not local_price_optimizing_active
                            and not price_boost_wallbox_active
                            and not predump_wallbox_active
                        ):
                            _last_stop_toggle = float(c_data.get("_last_stop_toggle_ts", 0.0) or 0.0)
                            if _last_stop_toggle > 0.0 and now_ts - _last_stop_toggle < restart_delay_s:
                                _restart_left = restart_delay_s - (now_ts - _last_stop_toggle)
                                cap_amp = 0
                                _log_state_once(
                                    c_data,
                                    "wb_restart_delay",
                                    (int(_restart_left // 5), c_public_label, c_control_mode),
                                    "WB%d Wiedereinschaltverzoegerung: noch %.0fs bis neuer PV-Start "
                                    "(Modus=%s, Regelpfad=%d)" % (
                                        c_id, max(0.0, _restart_left), c_public_label, c_control_mode
                                    ),
                                    min_interval_s=30.0,
                                )
                        if openwb_pro and not c_data.get("_openwb_runtime_seen", False):
                            if (
                                charger_connected
                                and not hw_charging
                                and int((charger_status or {}).get("amp", 0) or 0) >= 6
                            ):
                                # Nach einem Manager-Neustart darf eine bereits
                                # laufende Startfreigabe weitergefuehrt werden,
                                # aber sie darf den CP-Wakeup nicht als erledigt
                                # markieren. Sonst bleibt ein schlafendes Auto
                                # nach Service-Neustart dauerhaft bei 0W stehen.
                                c_data["_openwb_cp_start_sent"] = bool(
                                    (charger_status or {}).get("cp_interrupt_isactive", 0)
                                )
                            c_data["_openwb_runtime_seen"] = True
                        phase_capability = _wallbox_phase_switch_capability(charger_class_name, charger_status, config)
                        c_data["_openwb_phase_capability"] = phase_capability
                        phase_control_capable = bool(
                            hasattr(charger, "set_phases")
                            and phase_capability.get("can_switch", False)
                        )
                        openwb_phase_capable = bool(phase_control_capable)
                        openwb_secondary_current = openwb_controller
                        # Normale openWB bleibt Energiemanager. E3DC-Control
                        # sendet nur Sollstrom + Heartbeat; kein automatischer
                        # Keine openWB-Lademodi oder Phasenbefehle senden.
                        openwb_pv_capable = False
                        phase_identity_status = charger_status
                        if charger_status and not live_vehicle_identity_key and c_data.get("_session_vehicle_key"):
                            phase_identity_status = dict(charger_status)
                            phase_identity_status["car_id"] = c_data.get("_session_vehicle_key")
                        vehicle_max_phases = _vehicle_max_ac_phases(config, c_id, phase_identity_status)
                        if vehicle_max_phases >= 3 and (
                            c_data.get("_session_1p_only", False)
                            or float(c_data.get("_phase_3p_block_until", 0.0) or 0.0) > 0.0
                        ):
                            c_data["_session_1p_only"] = False
                            c_data["_phase_3p_block_until"] = 0.0
                            c_data["_phase_3p_pending_since"] = 0.0
                            _save_wallbox_phase_state(chargers)
                        vehicle_1p_only = bool(
                            vehicle_max_phases == 1
                            or (
                                vehicle_max_phases == 0
                                and c_data.get("_session_1p_only", False)
                            )
                        )
                        phase_forecast_hold_for_wb = bool(
                            phase_forecast_hold_active
                            and c_control_mode > 0
                            and not storage_charge_priority_active
                        )
                        external_amp_charger = (
                            not hasattr(charger, "set_amp_sonnenmodus")
                            and (
                                hasattr(charger, "set_amp_and_state")
                                or hasattr(charger, "set_direct_current")
                            )
                        )
                        c_allowed_w = float(allowed_w or 0.0)
                        target_wbminsoc_low_power_for_wb = bool(
                            target_wbminsoc_low_power_recovery_active
                            and c_public_mode == MODE_TARGET
                            and not local_grid_allowed
                            and not local_price_optimizing_active
                            and not price_boost_wallbox_active
                            and not predump_wallbox_active
                            and not _priority_export_fallback_charger
                        )
                        target_wbminsoc_floor_pv_only_for_wb = bool(
                            controlled_wallbox_wbminsoc_pv_only_active
                            and c_public_mode == MODE_TARGET
                            and c_control_mode in (9, 10)
                            and not local_grid_allowed
                            and not local_price_optimizing_active
                            and not price_boost_wallbox_active
                            and not predump_wallbox_active
                            and not _priority_export_fallback_charger
                        )
                        curve_wbminsoc_floor_pv_only_for_wb = bool(
                            curve_wbminsoc_floor_guard_active
                            and c_public_mode == MODE_CURVE
                            and c_control_mode > 0
                            and not local_grid_allowed
                            and not local_price_optimizing_active
                            and not price_boost_wallbox_active
                            and not predump_wallbox_active
                            and not _priority_export_fallback_charger
                        )
                        floor_pv_only_guard_for_wb = bool(
                            target_wbminsoc_floor_pv_only_for_wb
                            or curve_wbminsoc_floor_pv_only_for_wb
                        )
                        target_wbminsoc_taper_for_wb = bool(
                            wbminsoc_discharge_taper_active
                            and c_public_mode == MODE_TARGET
                            and not local_grid_allowed
                            and not local_price_optimizing_active
                            and not price_boost_wallbox_active
                            and not predump_wallbox_active
                            and not _priority_export_fallback_charger
                        )
                        target_wbminsoc_phase_taper_for_wb = bool(
                            target_wbminsoc_taper_for_wb
                            and wbminsoc_discharge_taper_factor < _sf(
                                config.get("wb_target_discharge_3p_factor_min", 0.75),
                                0.75,
                            )
                        )
                        if target_wbminsoc_low_power_for_wb:
                            c_allowed_w = min(c_allowed_w, target_wbminsoc_low_power_min_w)
                        external_target_soft_cap_w = None
                        if (
                            external_amp_charger
                            and c_public_mode == MODE_TARGET
                            and not local_price_optimizing_active
                            and not local_grid_allowed
                        ):
                            # Ziel-wbminSoC darf bei externen Wallboxen nicht
                            # aus dem C++-nahen iAval-Leitwert direkt auf
                            # 3p/Max springen. Start und Phasenwahl laufen wie
                            # eine ruhige PV-Kurve; nahe wbminSoC wird die
                            # Speicherstuetzung vor dem harten Floor reduziert.
                            direct_w = max(0.0, wb_actual_power + max(0.0, free_for_limbs_w))
                            assist_w = max(0.0, min(max_discharge_w, max(0.0, wb_storage_extra_w)))
                            if target_wbminsoc_taper_for_wb:
                                assist_w = wbminsoc_tapered_discharge_w
                                external_target_soft_cap_w = max(
                                    target_wbminsoc_low_power_min_w,
                                    target_wbminsoc_taper_real_surplus_w + assist_w,
                                )
                            else:
                                if wbminsoc_gate_open and soc_diff_for_wb > 0.0:
                                    assist_w = max(assist_w, max_discharge_w)
                                external_target_soft_cap_w = direct_w + assist_w
                            c_allowed_w = min(c_allowed_w, external_target_soft_cap_w)
                        if openwb_like_charger and charger_connected and cap_amp > 0:
                            # Bei openWB/openWB Pro ist der Hardware-Sollstrom
                            # massgeblich. Wenn vorher ein Stop/0A geschrieben
                            # wurde, kann unser interner current_set_amp noch
                            # 6A halten, waehrend die Box real 0A anbietet.
                            # Dann muss der Startbefehl erneut raus.
                            hw_offered_amp = int((charger_status or {}).get('amp', 0) or 0)
                            if hw_offered_amp < int(cap_amp):
                                if (
                                    hw_offered_amp < 6
                                    and hw_charging
                                    and stable_hw_power_w > 500.0
                                ):
                                    # connect.php kann kurz 0A melden, obwohl das
                                    # Fahrzeug real weiter mehrere kW zieht. Das
                                    # ist kein neuer Start und darf eine laufende
                                    # Ladung nicht wieder auf 6A zurueckwerfen.
                                    inferred_amp = int(round(
                                        stable_hw_power_w / max(1, detected_phases or 1) / 230.0
                                    ))
                                    current_amp = max(
                                        int(current_amp or 0),
                                        min(int(cap_amp), max(6, inferred_amp)),
                                    )
                                else:
                                    current_amp = min(int(current_amp or 0), hw_offered_amp)
                            else:
                                _hw_over_physical_cap = _openwb_physical_amp_down_required(
                                    c_public_mode,
                                    hw_offered_amp,
                                    cap_amp,
                                    grid_power_budget_w,
                                    charger_is_openwb_like=True,
                                    grid_allowed=local_grid_allowed,
                                    price_active=local_price_optimizing_active,
                                    price_boost_active=price_boost_wallbox_active,
                                    predump_active=predump_wallbox_active,
                                    threshold_w=_sf(
                                        config.get("wb_physical_amp_down_threshold_w", -250),
                                        -250.0,
                                    ),
                                )
                            if (
                                openwb_pro
                                and hw_offered_amp > int(cap_amp)
                                and _budget_ok
                                and (
                                    grid_power_budget_w > 250.0
                                    or _hw_over_physical_cap
                                )
                                and not (
                                    local_grid_allowed
                                    or local_price_optimizing_active
                                    or price_boost_wallbox_active
                                    or predump_wallbox_active
                                )
                            ):
                                # Nach einem Neustart oder ignorierten POST ist
                                # der Hardwarewert fuehrend: wenn die Pro real
                                # mehr anbietet als der neue Netzpunkt-Deckel,
                                # muss die Fast-Korrektur erneut senden.
                                current_amp = max(int(current_amp or 0), hw_offered_amp)
                        phase_target = _effective_phase_target(charger_status, c_data, now_ts)
                        phase_3p_min_w = 6 * 230.0 * 3
                        phase_up_buffer_default_w = 300.0 if openwb_pro else 800.0
                        if phase_forecast_hold_for_wb:
                            phase_up_buffer_default_w = 600.0 if openwb_pro else 1600.0
                        phase_up_buffer_w = _sf(
                            config.get("wb_phase_up_buffer_w", phase_up_buffer_default_w),
                            phase_up_buffer_default_w
                        )
                        phase_up_grid_allow_w = _sf(config.get("wb_phase_up_grid_allow_w", 600), 600.0)
                        phase_down_default_s = 240.0 if openwb_pro else 900.0
                        phase_down_min_s = 90.0 if openwb_pro else 300.0
                        phase_down_delay_s = max(
                            phase_down_min_s,
                            _sf(config.get("wb_phase_down_delay_s", phase_down_default_s), phase_down_default_s),
                        )
                        phase_down_fast_default_s = 120.0 if openwb_pro else phase_down_delay_s
                        phase_down_fast_delay_s = max(
                            60.0 if openwb_pro else 300.0,
                            _sf(config.get("wb_phase_down_fast_delay_s", phase_down_fast_default_s), phase_down_fast_default_s),
                        )
                        phase_down_reup_block_s = max(
                            120.0 if openwb_pro else 300.0,
                            _sf(config.get("wb_phase_down_reup_block_s", 180 if openwb_pro else 600), 180.0 if openwb_pro else 600.0),
                        )
                        phase_down_grid_w = _sf(config.get("wb_phase_down_grid_w", 1800), 1800.0)
                        phase_down_margin_w = _sf(config.get("wb_phase_down_margin_w", 1200), 1200.0)
                        phase_confirm_timeout_s = max(
                            120.0,
                            _sf(config.get("wb_phase_confirm_timeout_s", 240 if openwb_pro else 120), 240 if openwb_pro else 120)
                        )
                        vehicle_phase_unknown = bool(vehicle_max_phases == 0)
                        if vehicle_phase_unknown and openwb_phase_capable:
                            # Gastfahrzeug/unbekanntes Profil: 3p darf bei
                            # ausreichendem Budget einmal angetestet werden.
                            # Wenn danach real nur 1p-Leistung anliegt, bleibt
                            # die aktuelle Ladesession auf 1p. Das
                            # verhindert langes Pendeln bei 1p-only-Fahrzeugen.
                            phase_confirm_timeout_s = min(
                                phase_confirm_timeout_s,
                                max(
                                    60.0,
                                    _sf(
                                        config.get("wb_phase_unknown_probe_timeout_s", 90 if openwb_pro else 120),
                                        90.0 if openwb_pro else 120.0,
                                    ),
                                ),
                            )
                        phase_up_forecast_hold_s = max(
                            60.0,
                            _sf(
                                config.get("wb_phase_up_forecast_hold_s", 180 if openwb_pro else 120),
                                180.0 if openwb_pro else 120.0,
                            ),
                        )
                        phase_down_forecast_hold_s = max(
                            phase_down_delay_s,
                            _sf(
                                config.get("wb_phase_down_forecast_hold_s", 600 if openwb_pro else 900),
                                600.0 if openwb_pro else 900.0,
                            ),
                        )
                        phase_forecast_zero_hold_s = max(
                            phase_down_forecast_hold_s,
                            120.0,
                            _sf(
                                config.get("wb_phase_forecast_zero_hold_s", 420 if openwb_pro else 600),
                                420.0 if openwb_pro else 600.0,
                            ),
                        )
                        if (
                            storage_charge_priority_active
                            and c_control_mode > 0
                            and not (
                                local_grid_allowed
                                or local_price_optimizing_active
                                or price_boost_wallbox_active
                                or predump_wallbox_active
                            )
                        ):
                            _storage_priority_hold_s = max(
                                20.0,
                                _sf(config.get("wb_storage_priority_hold_s", 45), 45.0),
                            )
                            phase_down_delay_s = min(phase_down_delay_s, _storage_priority_hold_s)
                            phase_down_fast_delay_s = min(phase_down_fast_delay_s, _storage_priority_hold_s)
                            phase_down_forecast_hold_s = min(phase_down_forecast_hold_s, _storage_priority_hold_s)
                            phase_forecast_zero_hold_s = min(phase_forecast_zero_hold_s, _storage_priority_hold_s)
                        if (
                            floor_pv_only_guard_for_wb
                            and controlled_floor_battery_guard_active
                            and c_control_mode > 0
                            and not (
                                local_grid_allowed
                                or local_price_optimizing_active
                                or price_boost_wallbox_active
                                or predump_wallbox_active
                            )
                        ):
                            _floor_pv_only_hold_s = max(
                                10.0,
                                _sf(config.get("wb_floor_pv_only_phase_down_hold_s", 20), 20.0),
                            )
                            phase_down_delay_s = min(phase_down_delay_s, _floor_pv_only_hold_s)
                            phase_down_fast_delay_s = min(phase_down_fast_delay_s, _floor_pv_only_hold_s)
                            phase_down_forecast_hold_s = min(phase_down_forecast_hold_s, _floor_pv_only_hold_s)
                            phase_forecast_zero_hold_s = min(phase_forecast_zero_hold_s, _floor_pv_only_hold_s)
                        if (
                            external_target_soft_cap_w is not None
                            or floor_pv_only_guard_for_wb
                        ):
                            phase_budget_w = float(c_allowed_w or 0.0)
                        else:
                            phase_budget_w = max(
                                float(c_allowed_w or 0.0),
                                float(free_for_limbs_w or 0.0),
                                float(wb_actual_power or 0.0) + float(free_for_limbs_w or 0.0),
                            )
                        phase_battery_assist = bool(
                            c_control_mode in (9, 10, 11)
                            and wbminsoc_gate_open
                            and soc_diff_for_wb > wb_soc_hyst_pct
                            and not target_wbminsoc_phase_taper_for_wb
                            and not target_wbminsoc_low_power_for_wb
                        )
                        phase_3p_supported = bool(
                            phase_budget_w >= phase_3p_min_w + phase_up_buffer_w
                            and (
                                grid_power_budget_w < phase_up_grid_allow_w
                                or local_grid_allowed
                                or predump_wallbox_active
                                or phase_battery_assist
                            )
                        )
                        phase_3p_keep_supported = bool(
                            phase_3p_supported
                            or local_grid_allowed
                            or predump_wallbox_active
                            or price_boost_wallbox_active
                            or (
                                openwb_pro
                                and phase_battery_assist
                                and phase_budget_w >= max(0.0, phase_3p_min_w - phase_down_margin_w)
                                and grid_power_budget_w < phase_down_grid_w
                            )
                        )
                        if target_wbminsoc_low_power_for_wb or target_wbminsoc_phase_taper_for_wb:
                            phase_3p_supported = False
                            phase_3p_keep_supported = False
                        if vehicle_1p_only:
                            phase_3p_supported = False
                            phase_3p_keep_supported = False
                        phase_1p_start_hold_until = float(c_data.get("_phase_1p_start_hold_until", 0.0) or 0.0)
                        if hw_charging and phase_1p_start_hold_until > 0.0:
                            c_data["_phase_1p_start_hold_until"] = 0.0
                            phase_1p_start_hold_until = 0.0
                        phase_1p_start_hold_active = bool(
                            openwb_pro
                            and not hw_charging
                            and phase_target == 1
                            and now_ts < phase_1p_start_hold_until
                        )
                        if phase_1p_start_hold_active:
                            phase_3p_supported = False
                            phase_3p_keep_supported = False
                        phase_confirmed_3p = bool(
                            hw_charging
                            and (
                                current_phase_actual >= 3
                                or current_phases >= 3
                            )
                        )
                        phase_switch_phases = (
                            current_phases
                            if hw_charging and current_phases in (1, 2, 3)
                            else (
                                phase_target
                                if phase_target in (1, 3)
                                else (current_phase_actual if current_phase_actual in (1, 3) else 0)
                            )
                        )
                        phase_fallback_phases = (
                            current_phases
                            if hw_charging and current_phases in (1, 2, 3)
                            else detected_phases
                        )
                        phase_target_3p_active = bool(
                            openwb_phase_capable
                            and phase_target == 3
                        )
                        phase_effective_hold_s = max(0.0, phase_change_hold_s)
                        if openwb_pro:
                            phase_effective_hold_s = max(phase_effective_hold_s, _openwb_pro_phase_wait_s(config))
                        phase_3p_start_hold_s = phase_effective_hold_s
                        phase_3p_pending_hold_active = bool(
                            openwb_phase_capable
                            and phase_target == 3
                            and not hw_charging
                            and c_control_mode > 0
                            and phase_3p_start_hold_s > 0.0
                            and now_ts - c_data.get("_last_phase_switch_ts", 0) < phase_3p_start_hold_s
                            and grid_power_budget_w < 1500.0
                        )
                        phase_one_phase_start_budget_w = (
                            float(c_allowed_w or 0.0)
                            if external_target_soft_cap_w is not None
                            else max(float(c_allowed_w or 0.0), float(phase_budget_w or 0.0))
                        )
                        phase_forecast_start_relief = bool(
                            openwb_phase_capable
                            and phase_forecast_hold_for_wb
                            and charger_connected
                            and not hw_charging
                            and effective_wb_mode > 0
                            and phase_target == 3
                            and not phase_3p_supported
                            and phase_budget_w < phase_3p_min_w + phase_up_buffer_w
                            and phase_one_phase_start_budget_w >= 6.0 * 230.0
                        )
                        phase_start_1p_possible = bool(
                            openwb_phase_capable
                            and charger_connected
                            and not hw_charging
                            and effective_wb_mode > 0
                            and phase_target == 3
                            and (not phase_forecast_hold_for_wb or phase_forecast_start_relief)
                            and not phase_3p_supported
                            and (
                                external_target_soft_cap_w is None
                                or phase_one_phase_start_budget_w >= 6.0 * 230.0
                            )
                            and not phase_3p_pending_hold_active
                            and now_ts >= float(c_data.get("_phase_3p_block_until", 0.0) or 0.0)
                            and now_ts - c_data.get("_last_phase_switch_ts", 0) >= phase_effective_hold_s
                        )
                        low_power_one_phase_required_for_wb = bool(
                            (
                                target_wbminsoc_low_power_for_wb
                                or target_wbminsoc_phase_taper_for_wb
                                or floor_pv_only_guard_for_wb
                            )
                            and c_control_mode > 0
                            and not (
                                local_grid_allowed
                                or local_price_optimizing_active
                                or price_boost_wallbox_active
                                or predump_wallbox_active
                            )
                            and target_wbminsoc_low_power_real_surplus_w < target_wbminsoc_low_power_3p_ready_w
                        )
                        phase_cap_phases = 1 if (vehicle_1p_only or low_power_one_phase_required_for_wb) else (
                            3 if (
                                openwb_phase_capable
                                and (
                                    phase_target_3p_active
                                    or phase_3p_supported
                                    or phase_confirmed_3p
                                )
                            ) else max(1, phase_fallback_phases or 1)
                        )
                        vehicle_phase_limit = _valid_phase_count(vehicle_max_phases, 0)
                        if vehicle_phase_limit in (2, 3) and phase_cap_phases > vehicle_phase_limit:
                            phase_cap_phases = vehicle_phase_limit
                        if openwb_phase_capable and cap_amp > 0 and phase_cap_phases != max(1, detected_phases):
                            phase_allowed_w = float(c_allowed_w or 0.0)
                            if price_boost_wallbox_active or price_optimizing_active or effective_allow_grid:
                                phase_allowed_w = max(
                                    phase_allowed_w,
                                    float(charger_max_amp) * 230.0 * float(phase_cap_phases)
                                )
                            phase_min_w = 6 * 230.0 * phase_cap_phases
                            if phase_allowed_w >= phase_min_w:
                                phase_cap_limit_amp = charger_max_amp
                                if price_boost_wallbox_active or price_optimizing_active or effective_allow_grid:
                                    phase_cap_limit_amp = min(phase_cap_limit_amp, house_fuse_cap_amp)
                                cap_amp = max(6, min(
                                    phase_cap_limit_amp,
                                    int(phase_allowed_w / 230.0 / phase_cap_phases)
                                ))
                            else:
                                cap_amp = 0
                        if low_power_one_phase_required_for_wb and cap_amp > 0:
                            cap_amp = min(cap_amp, 6)
                        phase_block_until = float(c_data.get("_phase_3p_block_until", 0.0) or 0.0)
                        keep_recent_1p_reup_block = bool(
                            openwb_phase_capable
                            and phase_target == 1
                            and phase_block_until > now_ts
                        )
                        if phase_confirmed_3p and not keep_recent_1p_reup_block:
                            _had_phase_block = float(c_data.get("_phase_3p_block_until", 0.0) or 0.0) > 0.0
                            c_data["_phase_3p_block_until"] = 0.0
                            if _had_phase_block:
                                _save_wallbox_phase_state(chargers)
                        _phase_3p_block_active = bool(
                            now_ts < float(c_data.get("_phase_3p_block_until", 0.0) or 0.0)
                        )
                        if _phase_3p_block_active:
                            phase_3p_supported = False
                        phase_retry_block_s = max(
                            300.0,
                            float(_sf(config.get("wb_phase_retry_block_s", 900 if openwb_pro else 1800), 900.0 if openwb_pro else 1800.0)),
                        )

                        if not charger_connected:
                            # Kein Fahrzeug an genau dieser Wallbox: keine
                            # Kommandos, kein Sollstrom, keine Speicherfreigabe.
                            # Das verhindert, dass WB2 durch den globalen Modus
                            # mitgezogen wird, obwohl nur WB1 belegt ist.
                            c_data["is_charging"] = False
                            c_data["current_set_amp"] = 0
                            c_data["_pv_mode_active"] = False
                            c_data["_wb_stop_sent_active"] = False
                            c_data["_openwb_zero_budget_since"] = 0.0
                            c_data["_session_1p_only"] = False
                            c_data["_session_vehicle_key"] = ""
                            c_data["_openwb_start_retry_count"] = 0
                            continue

                        _target_kwh_reached, _session_kwh, _target_kwh = _wallbox_target_kwh_reached(
                            config,
                            c_id,
                            charger_status,
                        )
                        if _target_kwh_reached:
                            try:
                                hw_offered_amp = int(round(float((charger_status or {}).get("amp", 0) or 0)))
                            except Exception:
                                hw_offered_amp = 0
                            stop_already_sent = bool(c_data.get("_wb_stop_sent_active", False))
                            if (
                                hw_charging
                                or c_data.get("is_charging", False)
                                or int(c_data.get("current_set_amp", 0) or 0) > 0
                                or stable_hw_power_w > 250
                                or (hw_offered_amp > 0 and not stop_already_sent)
                            ):
                                _send_wallbox_stop_command(
                                    c_data,
                                    c_id=c_id,
                                    reason="target_kwh_reached",
                                )
                            if not c_data.get("_bev_full_blocked", False):
                                logger.warning(
                                    "WB%d Lademenge erreicht "
                                    "(%.2f kWh >= Ziel %.2f kWh) - Auftrag beendet."
                                    % (c_id, _session_kwh, _target_kwh)
                                )
                            cap_amp = 0
                            c_data["abort_count"] = 0
                            c_data["abort_cooldown_ts"] = 0.0
                            c_data["_bev_full_blocked"] = True
                            c_data["_real_charge_since"] = 0.0
                            c_data["is_charging"] = False
                            c_data["current_set_amp"] = 0
                            c_data["_pv_mode_active"] = False
                            c_data["_wb_stop_sent_active"] = True
                            c_data["_openwb_zero_budget_since"] = 0.0
                            c_data["_native_multi_start_grace_until"] = 0.0
                            c_data["_openwb_pro_start_hold_until"] = 0.0
                            c_data["_openwb_pro_start_hold_amp"] = 0
                            c_data["_openwb_start_retry_count"] = 0
                            c_data["_openwb_cp_start_sent"] = False
                            continue

                        if c_public_mode == MODE_BATTERY_DEPARTURE:
                            _bd_target_reached, _bd_car_soc, _bd_target_soc = _battery_departure_target_reached(
                                config,
                                c_id,
                                charger_status,
                            )
                            if _bd_target_reached:
                                try:
                                    hw_offered_amp = int(round(float((charger_status or {}).get("amp", 0) or 0)))
                                except Exception:
                                    hw_offered_amp = 0
                                stop_already_sent = bool(c_data.get("_wb_stop_sent_active", False))
                                if (
                                    hw_charging
                                    or c_data.get("is_charging", False)
                                    or int(c_data.get("current_set_amp", 0) or 0) > 0
                                    or stable_hw_power_w > 250
                                    or (hw_offered_amp > 0 and not stop_already_sent)
                                ):
                                    _send_wallbox_stop_command(
                                        c_data,
                                        c_id=c_id,
                                        reason="battery_departure_target_reached",
                                    )
                                if not c_data.get("_bev_full_blocked", False):
                                    logger.warning(
                                        "WB%d Akku-bis-Abfahrt: Fahrzeugziel erreicht "
                                        "(SoC %.1f%% >= Ziel %.1f%%) - Auftrag beendet."
                                        % (c_id, _bd_car_soc, _bd_target_soc)
                                    )
                                cap_amp = 0
                                c_data["abort_count"] = 0
                                c_data["abort_cooldown_ts"] = 0.0
                                c_data["_bev_full_blocked"] = True
                                c_data["_real_charge_since"] = 0.0
                                c_data["is_charging"] = False
                                c_data["current_set_amp"] = 0
                                c_data["_pv_mode_active"] = False
                                c_data["_wb_stop_sent_active"] = True
                                c_data["_openwb_zero_budget_since"] = 0.0
                                c_data["_native_multi_start_grace_until"] = 0.0
                                c_data["_openwb_pro_start_hold_until"] = 0.0
                                c_data["_openwb_pro_start_hold_amp"] = 0
                                c_data["_openwb_start_retry_count"] = 0
                                c_data["_openwb_cp_start_sent"] = False
                                c_data["_wb_stable_budget_jump_done"] = False
                                last_change_ts[c_id] = now_ts
                                _save_wallbox_abort_state(chargers)
                                continue

                        if c_data.get("_bev_full_blocked", False):
                            try:
                                hw_offered_amp = int(round(float((charger_status or {}).get("amp", 0) or 0)))
                            except Exception:
                                hw_offered_amp = 0
                            stop_already_sent = bool(c_data.get("_wb_stop_sent_active", False))
                            if (
                                hw_charging
                                or c_data.get("is_charging", False)
                                or int(c_data.get("current_set_amp", 0) or 0) > 0
                                or stable_hw_power_w > 250
                                or (hw_offered_amp > 0 and not stop_already_sent)
                            ):
                                _send_wallbox_stop_command(
                                    c_data,
                                    c_id=c_id,
                                    reason="bev_full_blocked",
                                )
                            cap_amp = 0
                            c_data["is_charging"] = False
                            c_data["current_set_amp"] = 0
                            c_data["_pv_mode_active"] = False
                            c_data["_wb_stop_sent_active"] = True
                            c_data["_openwb_zero_budget_since"] = 0.0
                            c_data["_openwb_pro_start_hold_until"] = 0.0
                            c_data["_openwb_pro_start_hold_amp"] = 0
                            c_data["_openwb_start_retry_count"] = 0
                            c_data["_openwb_cp_start_sent"] = False
                            continue

                        start_reject_timeout_s = max(
                            60.0,
                            _sf(config.get("wb_start_reject_timeout_s", 240), 240.0),
                        )
                        if openwb_pro:
                            start_reject_timeout_s = max(
                                60.0,
                                _sf(config.get("openwb_pro_start_reject_timeout_s", 120), 120.0),
                            )
                        elif e3dc_native_toggle:
                            start_reject_timeout_s = max(
                                90.0,
                                _sf(config.get("e3dc_native_start_reject_timeout_s", 150), 150.0),
                            )
                        start_reject_last_start_ts = float(c_data.get("last_start_ts", 0.0) or 0.0)
                        start_reject_anchor_ts = float(c_data.get("_openwb_start_reject_anchor_ts", 0.0) or 0.0)
                        if openwb_pro and start_reject_anchor_ts <= 0.0 and start_reject_last_start_ts > 0.0:
                            start_reject_anchor_ts = start_reject_last_start_ts
                            c_data["_openwb_start_reject_anchor_ts"] = start_reject_anchor_ts
                        start_reject_start_ts = (
                            start_reject_anchor_ts
                            if openwb_pro and start_reject_anchor_ts > 0.0
                            else start_reject_last_start_ts
                        )
                        start_reject_age_s = now_ts - start_reject_start_ts if start_reject_start_ts > 0.0 else 0.0
                        start_reject_guard_supported = bool(openwb_pro or e3dc_native_toggle)
                        start_reject_openwb_wakeup_done = bool(
                            openwb_pro
                            and c_data.get("_openwb_cp_start_sent", False)
                            and int(c_data.get("_openwb_start_retry_count", 0) or 0) >= 1
                        )
                        start_reject_context_allows_soft_stop = bool(
                            (
                                not local_grid_allowed
                                and not local_price_optimizing_active
                                and not price_boost_wallbox_active
                                and not predump_wallbox_active
                            )
                            or start_reject_openwb_wakeup_done
                        )
                        phase_transition_pending_for_start_reject = _phase_transition_pending_for_start_reject(
                            c_data,
                            now_ts=now_ts,
                            phase_confirm_timeout_s=phase_confirm_timeout_s,
                            phase_effective_hold_s=phase_effective_hold_s,
                        )
                        openwb_pro_session_state_text = str(c_data.get("_openwb_pro_session_state") or "")
                        openwb_pro_wakeup_retry_s = max(
                            30.0,
                            _sf(config.get("openwb_pro_wakeup_retry_s", 60), 60.0),
                        )
                        openwb_pro_wakeup_retry_max = max(1, int(config.get("openwb_pro_wakeup_retry_max", 3) or 3))
                        _wakeup_count = int(c_data.get("_openwb_pro_start_wakeup_count", 0) or 0)
                        _last_start_ts = float(c_data.get("last_start_ts", 0.0) or 0.0)
                        if (
                            openwb_pro
                            and charger_connected
                            and not hw_charging
                            and stable_hw_power_w <= 100.0
                            and int((charger_status or {}).get("phases_actual", 0) or 0) == 0
                            and _wakeup_count < openwb_pro_wakeup_retry_max
                            and _last_start_ts > 0.0
                            and now_ts - _last_start_ts > openwb_pro_wakeup_retry_s
                            and int(c_data.get("current_set_amp", 0) or 0) >= 6
                            and not bool(c_data.get("_wb_stop_sent_active", False))
                            and not phase_transition_pending_for_start_reject
                            and openwb_pro_session_state_text in ("starting", "offered")
                        ):
                            _retry_cp_payload = _openwb_pro_cp_payload_from_config(config, charger_status)
                            ok_retry = _execute_wallbox_driver_command(
                                c_data,
                                {
                                    "method": "trigger_cp_interrupt",
                                    "reason": "wakeup_retry_no_response",
                                    **_retry_cp_payload,
                                },
                                c_id=c_id,
                            )
                            if ok_retry:
                                c_data["_openwb_pro_start_wakeup_count"] = _wakeup_count + 1
                                c_data["_openwb_pro_start_wakeup_cp_ts"] = now_ts
                                c_data["_openwb_pro_start_wakeup_allowed_after"] = now_ts + openwb_pro_session.start_wakeup_delay_s(config)
                                c_data["_openwb_pro_start_wakeup_pending"] = True
                                c_data["_openwb_cp_start_sent"] = True
                                c_data["_openwb_last_cp_start_ts"] = now_ts
                                c_data["last_start_ts"] = now_ts
                                c_data["_openwb_start_reject_anchor_ts"] = now_ts
                                logger.info(
                                    "WB%d openWB Pro Wake-up Retry %d/%d: Fahrzeug hat nicht geantwortet "
                                    "(phases_actual=0, %.0fs seit Start)" % (
                                        c_id,
                                        _wakeup_count + 1,
                                        openwb_pro_wakeup_retry_max,
                                        now_ts - _last_start_ts,
                                    )
                                )
                            continue
                        if (
                            charger_connected
                            and start_reject_guard_supported
                            and not phase_transition_pending_for_start_reject
                            and c_control_mode > 0
                            and start_reject_start_ts > 0.0
                            and int(c_data.get("current_set_amp", 0) or 0) >= 6
                            and not c_data.get("_wb_stop_sent_active", False)
                            and not hw_charging
                            and stable_hw_power_w <= 500.0
                            and start_reject_age_s >= start_reject_timeout_s
                            and now_ts - float(c_data.get("_last_manager_stop_request_ts", 0.0) or 0.0) >= start_reject_timeout_s
                            and start_reject_context_allows_soft_stop
                        ):
                            start_reject_cooldown_s = max(
                                30.0,
                                _sf(config.get("wb_start_reject_cooldown_s", 60), 60.0),
                            )
                            stop_ok = _send_wallbox_stop_command(
                                c_data,
                                c_id=c_id,
                                reason="vehicle_start_rejected_soft",
                                _guard_checked=True,
                            )
                            c_data["_last_start_reject_stop_ok"] = bool(stop_ok)
                            c_data["abort_count"] = max(1, int(c_data.get("abort_count", 0) or 0))
                            c_data["abort_cooldown_ts"] = now_ts
                            c_data["_bev_full_blocked"] = False
                            c_data["_bev_full_block_reason"] = "start_rejected_soft"
                            c_data["_openwb_start_reject_soft_until"] = now_ts + start_reject_cooldown_s
                            c_data["is_charging"] = False
                            if stop_ok:
                                c_data["current_set_amp"] = 0
                                c_data["cap_amp"] = 0
                            c_data["_pv_mode_active"] = False
                            c_data["_wb_stop_sent_active"] = bool(stop_ok)
                            c_data["_openwb_zero_budget_since"] = 0.0
                            c_data["_native_multi_zero_budget_since"] = 0.0
                            c_data["_native_multi_start_grace_until"] = 0.0
                            c_data["_openwb_pro_start_hold_until"] = 0.0
                            c_data["_openwb_pro_start_hold_amp"] = 0
                            c_data["_openwb_start_retry_count"] = 0
                            c_data["_openwb_cp_start_sent"] = False
                            c_data["_openwb_start_reject_anchor_ts"] = 0.0
                            _save_wallbox_abort_state(chargers)
                            logger.warning(
                                "WB%d Start abgelehnt: %.0fs Startfreigabe ohne messbare Ladeleistung "
                                "(Modus=%s) - weicher Cooldown %.0fs, danach erneuter Versuch bei Budget." % (
                                    c_id,
                                    start_reject_age_s,
                                    c_public_label,
                                    start_reject_cooldown_s,
                                )
                            )
                            if not stop_ok:
                                logger.warning(
                                    "WB%d Start abgelehnt: Stop-Befehl nicht bestätigt; "
                                    "zeige keinen gesendeten Stop und versuche bei nächster Freigabe erneut." % c_id
                                )
                            continue

                        _multi_slots = (wb_multi_contract or {}).get("slots") or {}
                        _multi_slot = _multi_slots.get(c_id) or _multi_slots.get(str(c_id)) or {}
                        _prio_target = wb_priority_alloc.get(c_id)
                        if _prio_target is not None:
                            _prio_amp = int(_prio_target.get("target_amp", 0) or 0)
                            _prio_state = int(_prio_target.get("state", 1) or 1)
                            if _prio_state != 2 or _prio_amp <= 0:
                                cap_amp = 0
                                priority_forced_stop = bool(int(wb_dist_mode) in (1, 2))
                            else:
                                cap_amp = min(cap_amp, _prio_amp)

                        _priority_mode_id = int(
                            (wb_multi_contract or {}).get("priority_target_id", 0) or 0
                        )
                        if _priority_mode_id not in (1, 2):
                            _priority_mode_id = int(wb_dist_mode) if int(wb_dist_mode) in (1, 2) else 0
                        _priority_secondary_waiting = bool(
                            _priority_mode_id
                            and bool(_multi_slot.get("must_wait_for_priority", False))
                            and charger_connected
                            and not hw_charging
                            and stable_hw_power_w <= 500.0
                            and not local_grid_allowed
                            and not local_price_optimizing_active
                            and not price_boost_wallbox_active
                            and not predump_wallbox_active
                        )
                        if _priority_secondary_waiting:
                            if cap_amp > 0:
                                _log_state_once(
                                    c_data,
                                    "priority_secondary_no_start",
                                    (_priority_mode_id, c_public_label, c_control_mode),
                                    "WB%d wartet auf Priorität WB%d: keine neue Neben-WB-Startfreigabe "
                                    "ohne echte Ladeleistung" % (c_id, _priority_mode_id),
                                    min_interval_s=60.0,
                                )
                            cap_amp = 0
                            c_data["current_set_amp"] = 0
                            c_data["is_charging"] = False
                            c_data["_wb_stop_sent_active"] = True
                            c_data["_pv_mode_active"] = False
                            continue

                        if external_target_soft_cap_w is not None and cap_amp > 0:
                            target_phase_count = max(
                                1,
                                int(
                                    phase_cap_phases
                                    or phase_switch_phases
                                    or current_phases
                                    or detected_phases
                                    or 1
                                )
                            )
                            target_min_w = 6.0 * 230.0 * target_phase_count
                            if c_allowed_w >= target_min_w:
                                cap_amp = max(
                                    6,
                                    min(
                                        int(charger_max_amp),
                                        int(c_allowed_w / (230.0 * target_phase_count))
                                    )
                                )
                            else:
                                cap_amp = 0

                        _physical_budget = _wallbox_executable_budget(
                            charger_status,
                            c_data,
                            allowed_w=c_allowed_w,
                            detected_phases=detected_phases,
                            min_amp=wb_min_amp_cfg,
                            vehicle_max_phases=vehicle_max_phases,
                            phase_cap_phases=phase_cap_phases,
                            phase_switch_phases=phase_switch_phases,
                            phase_target=phase_target,
                            openwb_phase_capable=openwb_phase_capable,
                            can_switch_to_1p=phase_start_1p_possible,
                            require_one_phase=low_power_one_phase_required_for_wb,
                            grid_unlocked=bool(local_grid_allowed or local_price_optimizing_active or price_boost_wallbox_active),
                            phase_capability=phase_capability,
                            charger_class_name=charger_class_name,
                            driver_variant=str((charger_status or {}).get("driver_variant", "") or ""),
                        )
                        c_data["_physical_budget"] = _physical_budget
                        c_data["_phase_contract"] = dict(_physical_budget.get("phase_contract") or {})
                        _apply_phase_contract_to_status(charger_status, c_data.get("_phase_contract"))
                        _physical_phase_count = max(
                            1,
                            min(
                                3,
                                int(_physical_budget.get("phases", detected_phases) or detected_phases or 1),
                            ),
                        )
                        if (
                            cap_amp <= 0
                            and charger_connected
                            and _physical_budget.get("can_start_or_hold", False)
                            and not _physical_budget.get("real_charging", False)
                        ):
                            _phase_current_budget_w = (
                                phase_one_phase_start_budget_w
                                if _physical_phase_count == 1
                                else float(c_allowed_w or 0.0)
                            )
                            _phase_current_decision = wallbox_decision.budget_to_target_current(
                                allowed_w=_phase_current_budget_w,
                                detected_phases=_physical_phase_count,
                                min_amp=wb_min_amp_cfg,
                                max_amp=charger_max_amp,
                                house_fuse_cap_amp=house_fuse_cap_amp,
                                apply_house_fuse=(
                                    price_boost_wallbox_active
                                    or price_optimizing_active
                                    or effective_allow_grid
                                ),
                                base_6a_active=base_6a_active,
                                watts_per_amp=(
                                    _openwb_pro_effective_w_per_amp(
                                        charger_status,
                                        phases=_physical_phase_count,
                                        current_amp=max(float(current_amp or 0.0), float(c_data.get("current_set_amp", 0) or 0)),
                                    )
                                    if openwb_pro and _wb_status_real_charging(charger_status)
                                    else 0.0
                                ),
                            )
                            if int(_phase_current_decision.get("target_amp", 0) or 0) > 0:
                                current_decision = _phase_current_decision
                                cap_amp = int(_phase_current_decision["target_amp"])
                                house_fuse_limited = bool(
                                    _phase_current_decision.get("house_fuse_limited", False)
                                )
                        if cap_amp > 0 and not _physical_budget.get("can_start_or_hold", False):
                            cap_amp = 0
                        if openwb_pro:
                            if (
                                cap_amp > 0
                                and charger_connected
                                and not priority_forced_stop
                                and c_public_mode != MODE_OFF
                                and not bool(c_data.get("_bev_full_blocked", False))
                                and bool(c_data.get("_wb_stop_sent_active", False))
                            ):
                                c_data["_wb_stop_sent_active"] = False
                            _evaluate_openwb_pro_session_for_manager(
                                c_data,
                                charger_status,
                                cap_amp=cap_amp,
                                budget_ready=bool(
                                    _physical_budget.get("budget_ready", False)
                                    or _physical_budget.get("can_start_or_hold", False)
                                ),
                                switch_to_1p_ready=bool(_physical_budget.get("switch_to_1p_ready", False)),
                                grid_allowed=local_grid_allowed,
                                price_active=local_price_optimizing_active,
                                price_boost_active=price_boost_wallbox_active,
                                predump_active=predump_wallbox_active,
                                mode_off=(c_public_mode == MODE_OFF),
                                priority_forced_stop=priority_forced_stop,
                                min_amp=wb_min_amp_cfg,
                                now_ts=now_ts,
                                start_verify_s=max(
                                    30.0,
                                    _sf(config.get("openwb_pro_start_hold_s", 180), 180.0),
                                ),
                                stable_hw_power_w=stable_hw_power_w,
                            )
                        if openwb_controller:
                            _evaluate_openwb_session_for_manager(
                                c_data,
                                charger_status,
                                cap_amp=cap_amp,
                                budget_ready=bool(
                                    _physical_budget.get("budget_ready", False)
                                    or _physical_budget.get("can_start_or_hold", False)
                                ),
                                grid_allowed=local_grid_allowed,
                                price_active=local_price_optimizing_active,
                                price_boost_active=price_boost_wallbox_active,
                                predump_active=predump_wallbox_active,
                                mode_off=(c_public_mode == MODE_OFF),
                                priority_forced_stop=priority_forced_stop,
                                primary_delegate=openwb_primary_controller,
                                min_amp=wb_min_amp_cfg,
                                now_ts=now_ts,
                                start_verify_s=max(
                                    30.0,
                                    _sf(config.get("openwb_secondary_start_verify_s", 180), 180.0),
                                ),
                                stable_hw_power_w=stable_hw_power_w,
                            )
                        if charger_class_name == "GoECharger":
                            _evaluate_goe_session_for_manager(
                                c_data,
                                charger_status,
                                cap_amp=cap_amp,
                                budget_ready=bool(
                                    _physical_budget.get("budget_ready", False)
                                    or _physical_budget.get("can_start_or_hold", False)
                                ),
                                grid_allowed=local_grid_allowed,
                                price_active=local_price_optimizing_active,
                                price_boost_active=price_boost_wallbox_active,
                                predump_active=predump_wallbox_active,
                                mode_off=(c_public_mode == MODE_OFF),
                                priority_forced_stop=priority_forced_stop,
                                min_amp=wb_min_amp_cfg,
                                now_ts=now_ts,
                                start_verify_s=max(
                                    30.0,
                                    _sf(config.get("goe_start_verify_s", 180), 180.0),
                                ),
                                stable_hw_power_w=stable_hw_power_w,
                            )
                        try:
                            _physical_hw_amp = int(round(float((charger_status or {}).get("amp", 0) or 0)))
                        except (TypeError, ValueError):
                            _physical_hw_amp = 0
                        _physical_current_amp = max(int(current_amp or 0), _physical_hw_amp)
                        _physical_down_threshold_w = _sf(
                            config.get("wb_physical_amp_down_threshold_w", -250),
                            -250.0,
                        )
                        c_data["_physical_amp_down_active"] = _openwb_physical_amp_down_required(
                            c_public_mode,
                            _physical_current_amp,
                            cap_amp,
                            grid_power_budget_w,
                            charger_is_openwb_like=openwb_like_charger,
                            grid_allowed=local_grid_allowed,
                            price_active=local_price_optimizing_active,
                            price_boost_active=price_boost_wallbox_active,
                            predump_active=predump_wallbox_active,
                            threshold_w=_physical_down_threshold_w,
                        )
                        if (
                            floor_pv_only_guard_for_wb
                            and controlled_floor_battery_guard_active
                        ):
                            c_data["_physical_amp_down_active"] = True

                        if c_public_mode == MODE_BATTERY_DEPARTURE and (
                            c_id in battery_departure_blocked_ids
                            or not wbminsoc_gate_open
                            or not battery_departure_states.get(c_id, {}).get("active", False)
                        ):
                            try:
                                hw_offered_amp = int(round(float((charger_status or {}).get("amp", 0) or 0)))
                            except Exception:
                                hw_offered_amp = 0
                            stop_already_sent = bool(c_data.get("_wb_stop_sent_active", False))
                            if (
                                hw_charging
                                or c_data.get("is_charging", False)
                                or int(c_data.get("current_set_amp", 0) or 0) > 0
                                or stable_hw_power_w > 250
                                or (hw_offered_amp > 0 and not stop_already_sent)
                            ):
                                _send_wallbox_stop_command(
                                    c_data,
                                    c_id=c_id,
                                    reason=(
                                        "battery_departure_floor"
                                        if not wbminsoc_gate_open
                                        else "battery_departure_window"
                                    ),
                                )
                                logger.info(
                                    "WB%d Akku-bis-Abfahrt: Start/Halten blockiert "
                                    "(wbminSoC-Gate=%s, Fenster=%s, Leistung=%.0fW)" % (
                                        c_id,
                                        "offen" if wbminsoc_gate_open else "geschlossen",
                                        "aktiv" if battery_departure_states.get(c_id, {}).get("active", False) else "geschlossen",
                                        stable_hw_power_w,
                                    )
                                )
                            cap_amp = 0
                            c_data["is_charging"] = False
                            c_data["current_set_amp"] = 0
                            c_data["_pv_mode_active"] = False
                            c_data["_wb_stop_sent_active"] = True
                            c_data["_openwb_zero_budget_since"] = 0.0
                            c_data["_native_multi_start_grace_until"] = 0.0
                            c_data["_openwb_pro_start_hold_until"] = 0.0
                            c_data["_openwb_pro_start_hold_amp"] = 0
                            c_data["_wb_stable_budget_jump_done"] = False
                            last_change_ts[c_id] = now_ts
                            continue

                        floor_battery_stop_until = float(c_data.get("_wb_floor_battery_stop_until", 0.0) or 0.0)
                        floor_battery_cooldown_active = bool(now_ts < floor_battery_stop_until)
                        floor_special_pause_active = bool(
                            (
                                (
                                    controlled_wallbox_wbminsoc_pause
                                    and storage_floor_mode(c_public_mode)
                                )
                                or curve_wbminsoc_floor_pv_only_for_wb
                            )
                            and (
                                c_allowed_w < max(750.0, 6.0 * 230.0 * max(1, detected_phases) - 250.0)
                                or controlled_floor_battery_guard_active
                                or floor_battery_cooldown_active
                            )
                        )
                        if not floor_special_pause_active:
                            c_data["_wb_floor_pause_since"] = 0.0
                            c_data["_wb_floor_battery_since"] = 0.0
                            c_data["_wb_floor_battery_stop_until"] = 0.0

                        if floor_special_pause_active:
                            try:
                                hw_offered_amp = int(round(float((charger_status or {}).get("amp", 0) or 0)))
                            except Exception:
                                hw_offered_amp = 0
                            stop_already_sent = bool(c_data.get("_wb_stop_sent_active", False))
                            floor_stop_command_sent = False
                            floor_pause_since = float(c_data.get("_wb_floor_pause_since", 0.0) or 0.0)
                            if floor_pause_since <= 0.0:
                                floor_pause_since = now_ts
                                c_data["_wb_floor_pause_since"] = floor_pause_since
                            floor_pause_age_s = max(0.0, now_ts - floor_pause_since)
                            floor_min_hold_w = 6.0 * 230.0 * max(1, detected_phases)
                            floor_soft_hold_window_s = max(60.0, cloud_stop_delay_s)
                            floor_battery_stop_w = max(
                                700.0,
                                _sf(config.get("wb_target_floor_battery_stop_discharge_w", 1200), 1200.0),
                            )
                            floor_battery_support_active = bool(
                                controlled_floor_battery_discharge_w > floor_battery_stop_w
                            )
                            floor_effective_surplus_w = max(
                                0.0,
                                target_wbminsoc_low_power_real_surplus_w
                                - controlled_floor_battery_discharge_w,
                            )
                            floor_running_or_offered = bool(
                                hw_charging
                                or c_data.get("is_charging", False)
                                or int(c_data.get("current_set_amp", 0) or 0) > 0
                                or stable_hw_power_w > 250
                                or hw_offered_amp > 0
                            )
                            floor_actively_running = bool(
                                hw_charging
                                or c_data.get("is_charging", False)
                                or int(c_data.get("current_set_amp", 0) or 0) > 0
                                or stable_hw_power_w > 250
                            )
                            floor_hold_threshold_w = max(
                                750.0,
                                floor_min_hold_w - 250.0,
                            )
                            floor_gross_surplus_w = max(
                                0.0,
                                target_wbminsoc_low_power_real_surplus_w,
                            )
                            floor_gross_surplus_ready = bool(
                                floor_gross_surplus_w >= floor_hold_threshold_w
                            )
                            floor_real_surplus_ready = bool(
                                floor_effective_surplus_w >= floor_hold_threshold_w
                            )
                            floor_cloud_hold_ready = bool(
                                floor_pause_age_s < floor_soft_hold_window_s
                                and grid_power_budget_w < 2500.0
                                and not floor_battery_support_active
                            )
                            floor_import_stop_w = max(
                                2500.0,
                                _sf(config.get("wb_target_floor_stop_import_w", 4500), 4500.0),
                            )
                            floor_stop_delay_s = max(
                                180.0,
                                cloud_stop_delay_s,
                                min_charge_time_s,
                                _sf(config.get("wb_target_floor_import_stop_delay_s", 600), 600.0),
                            )
                            floor_battery_stop_delay_s = max(
                                10.0,
                                _sf(config.get("wb_target_floor_battery_stop_delay_s", 15), 15.0),
                            )
                            floor_battery_stop_cooldown_s = max(
                                300.0,
                                _sf(config.get("wb_target_floor_battery_stop_cooldown_s", 600), 600.0),
                            )
                            if grid_power_budget_w > floor_import_stop_w:
                                floor_import_since = float(c_data.get("_wb_floor_import_since", 0.0) or 0.0)
                                if floor_import_since <= 0.0:
                                    floor_import_since = now_ts
                                    c_data["_wb_floor_import_since"] = floor_import_since
                            else:
                                floor_import_since = 0.0
                                c_data["_wb_floor_import_since"] = 0.0
                            floor_import_age_s = max(0.0, now_ts - floor_import_since) if floor_import_since > 0.0 else 0.0
                            floor_import_hold_ready = bool(
                                floor_running_or_offered
                                and grid_power_budget_w > floor_import_stop_w
                                and floor_import_age_s < floor_stop_delay_s
                            )
                            floor_hard_stop_due = bool(
                                grid_power_budget_w > floor_import_stop_w
                                and floor_import_age_s >= floor_stop_delay_s
                            )
                            if floor_battery_support_active:
                                floor_battery_since = float(c_data.get("_wb_floor_battery_since", 0.0) or 0.0)
                                if floor_battery_since <= 0.0:
                                    floor_battery_since = now_ts
                                    c_data["_wb_floor_battery_since"] = floor_battery_since
                            else:
                                floor_battery_since = 0.0
                                c_data["_wb_floor_battery_since"] = 0.0
                            floor_battery_age_s = (
                                max(0.0, now_ts - floor_battery_since)
                                if floor_battery_since > 0.0
                                else 0.0
                            )
                            floor_pv_buffer_ready = bool(
                                (floor_real_surplus_ready or floor_gross_surplus_ready)
                                and grid_power_budget_w < floor_import_stop_w
                            )
                            floor_battery_hard_stop_due = _wbminsoc_floor_battery_hard_stop_due(
                                floor_battery_support_active=floor_battery_support_active,
                                floor_pv_buffer_ready=floor_pv_buffer_ready,
                                floor_battery_age_s=floor_battery_age_s,
                                floor_battery_stop_delay_s=floor_battery_stop_delay_s,
                                floor_effective_surplus_w=floor_effective_surplus_w,
                                floor_gross_surplus_w=floor_gross_surplus_w,
                                floor_min_hold_w=floor_min_hold_w,
                            )
                            floor_e3dc_native_immediate_battery_stop = _e3dc_native_floor_battery_stop_now(
                                e3dc_native_toggle=e3dc_native_toggle,
                                charger_connected=charger_connected,
                                verified_active_charge=bool(
                                    _e3dc_native_verified_active_charge(c_data)
                                    or hw_charging
                                    or stable_hw_power_w > 500.0
                                ),
                                cap_amp=cap_amp,
                                target_budget_w=c_allowed_w,
                                floor_battery_support_active=floor_battery_support_active,
                                floor_effective_surplus_w=floor_effective_surplus_w,
                                floor_min_hold_w=floor_min_hold_w,
                                priority_forced_stop=priority_forced_stop,
                                local_price_optimizing_active=local_price_optimizing_active,
                                local_grid_allowed=local_grid_allowed,
                                price_boost_wallbox_active=price_boost_wallbox_active,
                                budget_timeout=_budget_timeout,
                                predump_wallbox_active=predump_wallbox_active,
                            )
                            c_data["_wb_floor_e3dc_native_immediate_battery_stop"] = bool(
                                floor_e3dc_native_immediate_battery_stop
                            )
                            if floor_e3dc_native_immediate_battery_stop:
                                floor_battery_hard_stop_due = True
                            if floor_battery_hard_stop_due:
                                floor_battery_stop_until = max(
                                    floor_battery_stop_until,
                                    now_ts + floor_battery_stop_cooldown_s,
                                )
                                c_data["_wb_floor_battery_stop_until"] = floor_battery_stop_until
                                floor_battery_cooldown_active = True
                            floor_native_hold_ready = bool(
                                hasattr(c_data["charger"], "set_amp_sonnenmodus")
                                and not hasattr(c_data["charger"], "set_pv_mode")
                                and floor_actively_running
                                and not c_data.get("_wb_floor_stop_active", False)
                            )
                            floor_one_phase_ready = bool(
                                not low_power_one_phase_required_for_wb
                                or ((_physical_budget or {}).get("one_phase_ready", False))
                            )
                            floor_soft_hold_active = bool(
                                floor_running_or_offered
                                and charger_connected
                                and not priority_forced_stop
                                and not local_price_optimizing_active
                                and not local_grid_allowed
                                and not price_boost_wallbox_active
                                and not predump_wallbox_active
                                and not _budget_timeout
                                and not floor_hard_stop_due
                                and not floor_battery_hard_stop_due
                                and not floor_battery_cooldown_active
                                and floor_one_phase_ready
                                and (
                                    floor_real_surplus_ready
                                    or floor_pv_buffer_ready
                                    or floor_cloud_hold_ready
                                    or floor_import_hold_ready
                                    or floor_native_hold_ready
                                )
                            )
                            if floor_soft_hold_active:
                                hold_amp = max(
                                    6,
                                    min(
                                        int(charger_max_amp),
                                        int(c_data.get("current_set_amp", 0) or current_amp or hw_offered_amp or 6),
                                    ),
                                )
                                hold_amp = min(hold_amp, 6)
                                hold_due = bool(
                                    int(c_data.get("_last_wb_floor_hold_amp", 0) or 0) != hold_amp
                                    or now_ts - float(c_data.get("_last_wb_floor_hold_ts", 0.0) or 0.0) >= 30.0
                                )
                                if hold_due:
                                    try:
                                        if floor_native_hold_ready and hasattr(c_data["charger"], "set_amp_sonnenmodus"):
                                            _execute_wallbox_driver_command(
                                                c_data,
                                                {
                                                    "method": "set_amp_sonnenmodus",
                                                    "amp": hold_amp,
                                                    "force_state": None,
                                                    "reason": "wbminsoc_floor_hold",
                                                },
                                                c_id=c_id,
                                            )
                                        elif hasattr(c_data["charger"], "set_amp_and_state"):
                                            _execute_wallbox_driver_command(
                                                c_data,
                                                {
                                                    "method": "set_amp_and_state",
                                                    "amp": hold_amp,
                                                    "force_state": None,
                                                    "reason": "wbminsoc_floor_hold",
                                                },
                                                c_id=c_id,
                                            )
                                        elif hasattr(c_data["charger"], "set_amp_sonnenmodus"):
                                            _execute_wallbox_driver_command(
                                                c_data,
                                                {
                                                    "method": "set_amp_sonnenmodus",
                                                    "amp": hold_amp,
                                                    "force_state": None,
                                                    "reason": "wbminsoc_floor_hold",
                                                },
                                                c_id=c_id,
                                            )
                                        else:
                                            hold_due = False
                                    except Exception as exc:
                                        hold_due = False
                                        logger.warning(
                                            "WB%d PV+Akku-Untergrenze-Haltebefehl fehlgeschlagen: %s" %
                                            (c_id, exc)
                                        )
                                if hold_due:
                                    c_data["_last_wb_floor_hold_amp"] = hold_amp
                                    c_data["_last_wb_floor_hold_ts"] = now_ts
                                    last_change_ts[c_id] = now_ts
                                    made_changes = True
                                cap_amp = hold_amp
                                c_data["current_set_amp"] = hold_amp
                                c_data["is_charging"] = bool(hw_charging or stable_hw_power_w > 250 or hold_amp > 0)
                                c_data["_pv_mode_active"] = False
                                c_data["_wb_floor_stop_active"] = False
                                c_data["_wb_stop_sent_active"] = False
                                c_data["_native_multi_start_grace_until"] = max(
                                    float(c_data.get("_native_multi_start_grace_until", 0.0) or 0.0),
                                    now_ts + 180.0,
                                )
                                ui_state["cap_amp"] = hold_amp
                                ui_state["set_amp"] = hold_amp
                                ui_state["status_msg"] = "PV + Akku bis Untergrenze hält 6A"
                                _log_state_once(
                                    c_data,
                                    "wbminsoc_floor_soft_hold",
                                    (
                                        hold_amp,
                                        c_public_label,
                                        int(grid_power_raw),
                                        int(floor_effective_surplus_w),
                                    ),
                                    "WB%d %s: halte %dA statt Stop "
                                    "(wbminSoC-Floor, Grid=%dW, echte freie PV=%dW)" % (
                                        c_id,
                                        c_public_label,
                                        hold_amp,
                                        int(grid_power_raw),
                                        int(max(floor_effective_surplus_w, floor_gross_surplus_w)),
                                    ),
                                    min_interval_s=60.0,
                                )
                                continue
                            if (
                                hw_charging
                                or c_data.get("is_charging", False)
                                or int(c_data.get("current_set_amp", 0) or 0) > 0
                                or stable_hw_power_w > 250
                                or (hw_offered_amp > 0 and not stop_already_sent)
                            ):
                                floor_stop_reason = "wbminsoc_floor_pause"
                                if floor_battery_hard_stop_due or floor_battery_cooldown_active:
                                    floor_stop_reason = "wbminsoc_floor_battery_stop"
                                elif floor_hard_stop_due:
                                    floor_stop_reason = "wbminsoc_floor_grid_stop"
                                elif floor_pause_age_s >= floor_soft_hold_window_s:
                                    floor_stop_reason = "wbminsoc_floor_cloud_stop"
                                _send_wallbox_stop_command(
                                    c_data,
                                    c_id=c_id,
                                    reason=floor_stop_reason,
                                )
                                floor_stop_command_sent = True
                                logger.info(
                                    "WB%d %s: gestoppt, Hausakku unter wbminSoC; "
                                    "E3DC bleibt fuer Hauslasten im AUTO-Freilauf" % (
                                        c_id,
                                        c_public_label,
                                    )
                                )
                            cap_amp = 0
                            c_data["is_charging"] = False
                            c_data["current_set_amp"] = 0
                            c_data["_pv_mode_active"] = False
                            c_data["_wb_floor_stop_active"] = True
                            c_data["_wb_stop_sent_active"] = True
                            c_data["_openwb_zero_budget_since"] = 0.0
                            c_data["_native_multi_start_grace_until"] = 0.0
                            c_data["_openwb_pro_start_hold_until"] = 0.0
                            c_data["_openwb_pro_start_hold_amp"] = 0
                            c_data["_wb_stable_budget_jump_done"] = False
                            if floor_stop_command_sent:
                                c_data["_last_stop_toggle_ts"] = now_ts
                                last_change_ts[c_id] = now_ts
                            continue

                        predump_exit_needs_hard_stop = (
                            predump_wallbox_exited
                            and c_mode == 0
                            and (hw_charging or c_data.get("is_charging", False) or c_data.get("current_set_amp", 0) > 0)
                        )
                        if predump_exit_needs_hard_stop:
                            _send_wallbox_stop_command(
                                c_data,
                                c_id=c_id,
                                reason="predump_exit",
                            )
                            c_data["is_charging"] = False
                            c_data["current_set_amp"] = 0
                            c_data["_pv_mode_active"] = False
                            c_data["_wb_stop_sent_active"] = True
                            c_data["_predump_gate_stop_sent"] = True
                            last_change_ts[c_id] = now_ts
                            logger.info("WB%d Pre-Dump beendet/pausiert: Wallbox sofort gestoppt" % c_id)
                            continue
                        elif predump_wallbox_exited:
                            # Wenn nach dem Pre-Dump ein normaler Python-WB-Modus
                            # aktiv bleibt, nicht hart stoppen. Die laufende
                            # Regelung uebernimmt nahtlos und verhindert ein
                            # unnoetiges Stop/Start-Takten am Ladestart.
                            c_data["_predump_gate_stop_sent"] = False

                        native_battery_drain_zero_budget_active = bool(
                            e3dc_native_toggle
                            and c_control_mode in (2, 3, 6)
                            and charger_connected
                            and hw_charging
                            and hw_power_w > 500.0
                            and int(cap_amp or 0) <= 0
                            and not priority_forced_stop
                            and not _budget_timeout
                            and not local_price_optimizing_active
                            and not local_grid_allowed
                            and not price_boost_wallbox_active
                            and not predump_wallbox_active
                            and grid_power_budget_w > -800.0
                            and pv_power_raw < max(300.0, hw_power_w * 0.25)
                            and battery_power_raw < -max(700.0, min(1200.0, hw_power_w * 0.5))
                        )
                        if native_battery_drain_zero_budget_active:
                            _send_wallbox_stop_command(
                                c_data,
                                c_id=c_id,
                                reason="native_battery_drain_zero_budget",
                            )
                            c_data["is_charging"] = False
                            c_data["current_set_amp"] = 0
                            c_data["_pv_mode_active"] = False
                            c_data["_wb_stop_sent_active"] = True
                            c_data["_native_multi_start_grace_until"] = 0.0
                            c_data["_last_stop_toggle_ts"] = now_ts
                            c_data["_openwb_zero_budget_since"] = 0.0
                            c_data["_native_multi_zero_budget_since"] = 0.0
                            last_change_ts[c_id] = now_ts
                            logger.info(
                                "WB%d %s: gestoppt, native Ladung würde den Hausakku leeren "
                                "(WB=%.0fW, Akku=%.0fW, PV=%.0fW, Grid=%.0fW, Regelpfad=%d)" % (
                                    c_id,
                                    c_public_label,
                                    hw_power_w,
                                    battery_power_raw,
                                    pv_power_raw,
                                    grid_power_raw,
                                    c_control_mode,
                                )
                            )
                            continue

                        # openWB Pro: 3p frueh anfordern, aber sehr spaet
                        # zurueckschalten. Kurze Wolken oder Lastspruenge soll
                        # der Hausakku abfedern, damit keine Phasen-Pendelung
                        # entsteht.
                        phase_configured_3p = (
                            phase_confirmed_3p
                            or phase_target == 3
                        )
                        _phase_pending_since = float(c_data.get("_phase_3p_pending_since", 0.0) or 0.0)
                        _phase_pending_age = max(0.0, now_ts - _phase_pending_since) if _phase_pending_since > 0.0 else 0.0
                        one_phase_confirmed = bool(
                            (
                                current_phases == 1
                                or (
                                    vehicle_phase_unknown
                                    and max(
                                        int((charger_status or {}).get("amp", 0) or 0),
                                        int(current_amp or 0),
                                        int(c_data.get("current_set_amp", 0) or 0),
                                    ) >= 6
                                    and float((charger_status or {}).get("real_power_w", 0.0) or 0.0) > 500.0
                                    and float((charger_status or {}).get("real_power_w", 0.0) or 0.0) <
                                        max(
                                            int((charger_status or {}).get("amp", 0) or 0),
                                            int(current_amp or 0),
                                            int(c_data.get("current_set_amp", 0) or 0),
                                        ) * 230.0 * 1.8
                                )
                            )
                            and hw_charging
                            and float((charger_status or {}).get("real_power_w", 0.0) or 0.0) > 500.0
                        )
                        phase_recommendation = wallbox_decision.phase_switch_recommendation(
                            openwb_phase_capable=openwb_phase_capable,
                            charger_connected=charger_connected,
                            control_mode=c_control_mode,
                            effective_wb_mode=effective_wb_mode,
                            hw_charging=hw_charging,
                            cap_amp=cap_amp,
                            openwb_pro=openwb_pro,
                            vehicle_1p_only=vehicle_1p_only,
                            vehicle_phase_unknown=vehicle_phase_unknown,
                            phase_target=phase_target,
                            phase_switch_phases=phase_switch_phases,
                            phase_cap_phases=phase_cap_phases,
                            phase_configured_3p=phase_configured_3p,
                            phase_3p_supported=phase_3p_supported,
                            phase_3p_keep_supported=phase_3p_keep_supported,
                            phase_3p_pending_hold_active=phase_3p_pending_hold_active,
                            phase_start_1p_possible=phase_start_1p_possible,
                            phase_1p_start_hold_active=phase_1p_start_hold_active,
                            phase_forecast_hold_for_wb=phase_forecast_hold_for_wb,
                            phase_block_active=_phase_3p_block_active,
                            last_phase_switch_age_s=now_ts - float(c_data.get("_last_phase_switch_ts", 0.0) or 0.0),
                            phase_effective_hold_s=phase_effective_hold_s,
                            phase_down_since_age_s=(
                                now_ts - float(c_data.get("_phase_down_since", 0.0) or 0.0)
                                if float(c_data.get("_phase_down_since", 0.0) or 0.0) > 0.0
                                else 0.0
                            ),
                            phase_down_delay_s=phase_down_delay_s,
                            phase_down_fast_delay_s=phase_down_fast_delay_s,
                            phase_down_forecast_hold_s=phase_down_forecast_hold_s,
                            phase_up_since_age_s=(
                                now_ts - float(c_data.get("_phase_up_budget_since", 0.0) or 0.0)
                                if float(c_data.get("_phase_up_budget_since", 0.0) or 0.0) > 0.0
                                else 0.0
                            ),
                            phase_up_forecast_hold_s=phase_up_forecast_hold_s,
                            predump_wallbox_active=predump_wallbox_active,
                            local_price_optimizing_active=local_price_optimizing_active,
                            local_grid_allowed=local_grid_allowed,
                            wbminsoc_gate_open=wbminsoc_gate_open,
                            grid_power_w=grid_power_budget_w,
                            phase_down_grid_w=phase_down_grid_w,
                            one_phase_confirmed=one_phase_confirmed,
                            phase_pending_age_s=_phase_pending_age,
                            phase_confirm_timeout_s=phase_confirm_timeout_s,
                        )
                        phase_switch_action = str(phase_recommendation.get("action", "KEEP_PHASES"))
                        phase_switch_reason = str(phase_recommendation.get("reason", "stable"))
                        openwb_pro_phase_wait_active = bool(
                            openwb_pro
                            and _openwb_pro_phase_wait_active(
                                c_data,
                                charger_status,
                                now_ts,
                                stable_hw_power_w=stable_hw_power_w,
                            )
                        )
                        if openwb_pro_phase_wait_active:
                            # Ticken der internen Phasenwechsel-Sequenz falls aktiv, da reguläre set_phases-Pfade blockiert sind.
                            phase_seq = c_data.get("_openwb_pro_phase_sequence")
                            if isinstance(phase_seq, dict) and phase_seq:
                                seq_target = int(c_data.get("_openwb_pro_phase_sequence_target", 0) or 0)
                                if seq_target in (1, 3):
                                    _openwb_pro_phase_sequence_step(
                                        c_data,
                                        {
                                            "method": "set_phases",
                                            "phases": seq_target,
                                            "reason": "openwb_pro_phase_wait_tick",
                                        },
                                        charger=charger,
                                        c_id=c_id,
                                        reason="openwb_pro_phase_wait_tick",
                                    )
                            c_data["_phase_down_since"] = 0.0
                            phase_switch_action = "KEEP_PHASES"
                            phase_switch_reason = "openwb_pro_phase_wait"
                            phase_recommendation = dict(phase_recommendation)
                            phase_recommendation["action"] = "KEEP_PHASES"
                            phase_recommendation["target_phases"] = 0
                            phase_recommendation["reason"] = "openwb_pro_phase_wait"
                        openwb_pro_cold_start_ready = bool(
                            openwb_pro
                            and openwb_phase_capable
                            and charger_connected
                            and c_control_mode > 0
                            and effective_wb_mode > 0
                            and cap_amp > 0
                            and phase_target != 1
                            and not hw_charging
                            and stable_hw_power_w <= 500.0
                            and int(c_data.get("current_set_amp", 0) or 0) <= 0
                            and not priority_forced_stop
                            and not bool(c_data.get("_bev_full_blocked", False))
                            and not openwb_pro_phase_wait_active
                        )
                        openwb_pro_cold_start_3p_ready = bool(
                            openwb_pro_cold_start_ready
                            and phase_3p_supported
                            and int(phase_cap_phases or 0) >= 3
                            and cap_amp >= 6
                            and not vehicle_1p_only
                            and not _phase_3p_block_active
                        )
                        openwb_pro_cold_start_1p_needed = bool(
                            openwb_pro_cold_start_ready
                            and phase_switch_action != "SWITCH_1P"
                            and not openwb_pro_cold_start_3p_ready
                        )
                        if openwb_pro_cold_start_1p_needed:
                            phase_switch_action = "SWITCH_1P"
                            phase_switch_reason = "openwb_pro_cold_start_1p"
                            phase_recommendation = dict(phase_recommendation)
                            phase_recommendation["action"] = "SWITCH_1P"
                            phase_recommendation["target_phases"] = 1
                            phase_recommendation["reason"] = "openwb_pro_cold_start_1p"
                        phase_start_stop_action = "NOOP"
                        phase_start_stop_target_amp = 0
                        phase_start_stop_hold_amp = 0
                        phase_start_stop_reason = "phase_decision_pending_start_stop"
                        if (
                            phase_switch_action == "KEEP_PHASES"
                            and not openwb_pro_phase_wait_active
                            and cap_amp > 0
                            and charger_connected
                        ):
                            phase_start_stop_action = (
                                "START"
                                if current_amp <= 0 and int(c_data.get("current_set_amp", 0) or 0) <= 0
                                else "SET_CURRENT"
                            )
                            phase_start_stop_target_amp = int(cap_amp)
                            phase_start_stop_hold_amp = int(cap_amp)
                            phase_start_stop_reason = "phase_decision_apply_current"
                        phase_decision_payload = wallbox_decision.build_wallbox_decision_payload(
                            wb_id=c_id,
                            public_mode=effective_wb_mode,
                            control_mode=c_control_mode,
                            current_decision=current_decision,
                            start_stop_decision={
                                "action": phase_start_stop_action,
                                "target_amp": phase_start_stop_target_amp,
                                "hold_amp": phase_start_stop_hold_amp,
                                "reason": phase_start_stop_reason,
                            },
                            phase_recommendation=phase_recommendation,
                            allowed_w=allowed_w,
                            detected_phases=detected_phases,
                            current_amp=current_amp,
                            current_set_amp=int(c_data.get("current_set_amp", 0) or 0),
                            cap_amp=cap_amp,
                            max_amp=_charger_max_amp(config, c_id, wb_global_max_amp),
                            charger_connected=charger_connected,
                            hw_charging=hw_charging,
                            hw_power_w=hw_power_w,
                            grid_power_w=grid_power_budget_w,
                            mode_label=c_public_label,
                            storage_state=str(_budget.get("storage_state", _budget_state) or _budget_state),
                            driver_class_name=(
                                c_data.get("charger").__class__.__name__
                                if c_data.get("charger") is not None
                                else ""
                            ),
                            openwb_like_charger=openwb_like_charger,
                            openwb_pro=openwb_pro,
                            e3dc_native_toggle=e3dc_native_toggle,
                            priority_forced_stop=priority_forced_stop,
                            budget_timeout=_budget_timeout,
                        )
                        _remember_wallbox_decision_payload(c_data, phase_decision_payload)
                        phase_start_1p_needed = bool(
                            phase_switch_action == "SWITCH_1P"
                            and phase_switch_reason in ("start_1p", "openwb_pro_cold_start_1p")
                            and (
                                (
                                    phase_start_1p_possible
                                    and phase_target != 1
                                    and (not phase_forecast_hold_for_wb or phase_forecast_start_relief)
                                )
                                or openwb_pro_cold_start_1p_needed
                            )
                        )
                        if (
                            phase_switch_action == "SWITCH_1P"
                            and phase_switch_reason == "vehicle_1p_only"
                        ):
                            if _execute_wallbox_driver_command(
                                c_data,
                                {
                                    "method": "set_phases",
                                    "phases": 1,
                                    "reason": "vehicle_1p_only",
                                },
                                c_id=c_id,
                            ):
                                if openwb_pro:
                                    _mark_openwb_pro_phase_wait(
                                        c_data,
                                        1,
                                        current_amp=max(
                                            int(current_amp or 0),
                                            int((charger_status or {}).get("amp", 0) or 0),
                                            int(c_data.get("current_set_amp", 0) or 0),
                                        ),
                                        now_ts=now_ts,
                                        config=config,
                                        charger_max_amp=charger_max_amp,
                                    )
                                _remember_phase_target(c_data, 1, now_ts, phase_retry_block_s)
                                c_data["_last_phase_switch_ts"] = now_ts
                                c_data["_phase_change_seen_session"] = True
                                c_data["_phase_3p_pending_since"] = 0.0
                                c_data["_phase_up_budget_since"] = 0.0
                                c_data["_phase_down_since"] = 0.0
                                c_data["_pv_mode_active"] = False
                                logger.info(
                                    "WB%d Fahrzeugprofil/Gast-Lernen: 1p-only -> keine 3p-Anforderung "
                                    "(Fahrzeugphasen=%d, Modus=%s)" % (
                                        c_id, int(vehicle_max_phases or 1), c_public_label
                                    )
                                )
                                continue
                        if phase_start_1p_needed:
                            _phase_start_hold_amp = max(
                                6,
                                min(
                                    int(charger_max_amp),
                                    int(
                                        max(
                                            current_amp or 0,
                                            (charger_status or {}).get("amp", 0) or 0,
                                            c_data.get("current_set_amp", 0) or 0,
                                            phase_budget_w / 230.0 if phase_budget_w > 0 else 0,
                                        )
                                    ),
                                ),
                            )
                            _phase_start_hold_amp = int(round(_cap_openwb_pro_one_phase_amp(
                                _phase_start_hold_amp,
                                1,
                                config,
                                c_id,
                                charger_max_amp,
                            )))
                            if _execute_wallbox_driver_command(
                                c_data,
                                {
                                    "method": "set_phases",
                                    "phases": 1,
                                    "reason": phase_switch_reason,
                                },
                                c_id=c_id,
                            ):
                                _phase_start_hold_sent = False
                                if openwb_pro:
                                    _mark_openwb_pro_phase_wait(
                                        c_data,
                                        1,
                                        current_amp=_phase_start_hold_amp,
                                        now_ts=now_ts,
                                        config=config,
                                        charger_max_amp=charger_max_amp,
                                    )
                                    if _execute_wallbox_driver_command(
                                        c_data,
                                        {
                                            "method": "set_amp_and_state",
                                            "amp": _phase_start_hold_amp,
                                            "force_state": None,
                                            "reason": "openwb_pro_phase_start_hold",
                                        },
                                        c_id=c_id,
                                    ):
                                        _phase_start_hold_sent = True
                                        _mark_openwb_pro_start_offer(
                                            c_data,
                                            _phase_start_hold_amp,
                                            now_ts=now_ts,
                                            config=config,
                                            charger_max_amp=charger_max_amp,
                                            refresh=True,
                                        )
                                _remember_phase_target(c_data, 1, now_ts, phase_down_reup_block_s)
                                c_data["_last_phase_switch_ts"] = now_ts
                                c_data["_phase_change_seen_session"] = True
                                c_data["_wb_stable_budget_jump_done"] = False
                                c_data["_wb_stable_budget_jump_ts"] = 0.0
                                c_data["last_storage_guided_amp_up_ts"] = now_ts
                                c_data["_phase_down_since"] = 0.0
                                c_data["_phase_up_budget_since"] = 0.0
                                c_data["_phase_1p_start_hold_until"] = now_ts + max(0.0, phase_effective_hold_s)
                                c_data["_pv_mode_active"] = False
                                c_data["is_charging"] = bool(hw_charging or _phase_start_hold_sent)
                                c_data["current_set_amp"] = _phase_start_hold_amp if _phase_start_hold_sent else 0
                                c_data["_wb_stop_sent_active"] = False
                                last_change_ts[c_id] = now_ts
                                logger.info(
                                    "WB%d openWB Start auf 1p angefordert, Startfreigabe gehalten "
                                    "(%dA, Modus=%s, Regelpfad=%d, Budget=%.0fW, Grid=%.0fW)" % (
                                        c_id, _phase_start_hold_amp, c_public_label, c_control_mode, phase_budget_w, grid_power_raw
                                    )
                                )
                                continue
                        phase_down_needed = bool(
                            phase_switch_action in ("WAIT_1P", "SWITCH_1P")
                            and phase_switch_reason in (
                                "effective_cap",
                                "grid_import",
                                "no_3p_budget",
                                "3p_minimum",
                                "wbminsoc_floor",
                            )
                        )
                        if phase_down_needed:
                            _phase_down_since = float(c_data.get("_phase_down_since", 0.0) or 0.0)
                            _phase_down_reason = "wirksamer Deckel"
                            if grid_power_budget_w > phase_down_grid_w:
                                _phase_down_reason = "Netzbezug"
                            elif cap_amp == 0:
                                _phase_down_reason = "kein wirksames 3p-Budget"
                            elif openwb_pro and cap_amp <= 6:
                                _phase_down_reason = "3p-Mindeststrom"
                            elif not wbminsoc_gate_open:
                                _phase_down_reason = "wbminSoC-Schutz"
                            _phase_down_wait_s = phase_down_delay_s
                            if (
                                phase_forecast_hold_for_wb
                                and grid_power_budget_w < max(phase_down_grid_w * 1.5, phase_down_grid_w + 1200.0)
                            ):
                                _phase_down_wait_s = max(_phase_down_wait_s, phase_down_forecast_hold_s)
                            elif (
                                openwb_pro
                                and (
                                    cap_amp == 0
                                    or (openwb_pro and cap_amp <= 6)
                                    or grid_power_budget_w > phase_down_grid_w
                                    or not wbminsoc_gate_open
                                )
                            ):
                                _phase_down_wait_s = phase_down_fast_delay_s
                            if _phase_down_since <= 0.0:
                                _phase_down_since = now_ts
                                c_data["_phase_down_since"] = _phase_down_since
                                _phase_down_observe_msg = (
                                    "WB%d openWB Prognose-Halt: 3p bleibt aktiv, "
                                    "Rueckschaltung auf 1p nur beobachtet "
                                    "(Grund=%s, Wartezeit=%.0fs, Modus=%s, Regelpfad=%d, RohBudget=%.0fW, Deckel=%.0fW, Grid=%.0fW)"
                                    if phase_forecast_hold_for_wb
                                    else
                                    "WB%d openWB 3p wird zu schwer: Rueckschaltung auf 1p beobachtet "
                                    "(Grund=%s, Wartezeit=%.0fs, Modus=%s, Regelpfad=%d, RohBudget=%.0fW, Deckel=%.0fW, Grid=%.0fW)"
                                )
                                _log_state_once(
                                    c_data,
                                    "phase_down_observe",
                                    (_phase_down_reason, c_public_label, c_control_mode, phase_switch_phases),
                                    _phase_down_observe_msg % (
                                        c_id, _phase_down_reason, _phase_down_wait_s,
                                        c_public_label, c_control_mode,
                                        phase_budget_w, c_allowed_w, grid_power_raw
                                    ),
                                    min_interval_s=600.0,
                                )
                            if now_ts - _phase_down_since >= _phase_down_wait_s:
                                if _execute_wallbox_driver_command(
                                    c_data,
                                    {
                                        "method": "set_phases",
                                        "phases": 1,
                                        "reason": phase_switch_reason,
                                    },
                                    c_id=c_id,
                                ):
                                    if openwb_pro:
                                        _mark_openwb_pro_phase_wait(
                                            c_data,
                                            1,
                                            current_amp=max(
                                                int(current_amp or 0),
                                                int((charger_status or {}).get("amp", 0) or 0),
                                                int(c_data.get("current_set_amp", 0) or 0),
                                            ),
                                            now_ts=now_ts,
                                            config=config,
                                            charger_max_amp=charger_max_amp,
                                        )
                                    _remember_phase_target(c_data, 1, now_ts, phase_down_reup_block_s)
                                    c_data["_last_phase_switch_ts"] = now_ts
                                    c_data["_phase_change_seen_session"] = True
                                    c_data["_wb_stable_budget_jump_done"] = False
                                    c_data["_wb_stable_budget_jump_ts"] = 0.0
                                    c_data["last_storage_guided_amp_up_ts"] = now_ts
                                    c_data["_phase_down_since"] = 0.0
                                    c_data["_pv_mode_active"] = False
                                    if openwb_pro:
                                        c_data["_phase_3p_block_until"] = max(
                                            float(c_data.get("_phase_3p_block_until", 0.0) or 0.0),
                                            now_ts + phase_down_reup_block_s,
                                        )
                                        _save_wallbox_phase_state(chargers)
                                    last_change_ts[c_id] = now_ts
                                    logger.info(
                                        "WB%d openWB auf 1p angefordert nach %.0fs Hysterese "
                                        "(Grund=%s, Modus=%s, Regelpfad=%d, RohBudget=%.0fW, Deckel=%.0fW, Grid=%.0fW, SOC=%.1f%% wbmin=%.1f%%)" % (
                                            c_id, now_ts - _phase_down_since,
                                            _phase_down_reason, c_public_label, c_control_mode,
                                            phase_budget_w, c_allowed_w, grid_power_raw,
                                            float(battery_soc or 0.0), wb_minsoc_cfg
                                        )
                                    )
                                    c_data["is_charging"] = hw_charging
                                    c_data["current_set_amp"] = 0
                                    continue
                        else:
                            c_data["_phase_down_since"] = 0.0

                        if (
                            phase_switch_action in ("WAIT_3P", "SWITCH_3P")
                            and phase_switch_reason == "phase_up"
                        ):
                            _phase_up_since = float(c_data.get("_phase_up_budget_since", 0.0) or 0.0)
                            if _phase_up_since <= 0.0:
                                _phase_up_since = now_ts
                                c_data["_phase_up_budget_since"] = _phase_up_since
                            _phase_up_wait_s = 0.0 if (openwb_pro or predump_wallbox_active) else 45.0
                            if (
                                phase_forecast_hold_for_wb
                                and not phase_configured_3p
                                and not (
                                    openwb_pro
                                    and phase_3p_supported
                                )
                            ):
                                _phase_up_wait_s = max(_phase_up_wait_s, phase_up_forecast_hold_s)
                            if now_ts - _phase_up_since >= _phase_up_wait_s:
                                if (
                                    not openwb_pro
                                    and hw_charging
                                    and current_amp > 6
                                    and hasattr(charger, "set_amp_and_state")
                                ):
                                    c_data["_f040_phase_restart_amp"] = max(
                                        6,
                                        int(
                                            current_amp
                                            or (charger_status or {}).get("amp", 0)
                                            or c_data.get("current_set_amp", 0)
                                            or 6
                                        ),
                                    )
                                    _execute_wallbox_driver_command(
                                        c_data,
                                        {
                                            "method": "set_amp_and_state",
                                            "amp": 6,
                                            "force_state": None,
                                            "reason": "phase_up_prelimit",
                                        },
                                        c_id=c_id,
                                    )
                                    c_data["current_set_amp"] = 6
                                if _execute_wallbox_driver_command(
                                    c_data,
                                    {
                                        "method": "set_phases",
                                        "phases": 3,
                                        "reason": "phase_up",
                                    },
                                    c_id=c_id,
                                ):
                                    _phase_up_start_hold_sent = False
                                    _phase_up_start_hold_amp = 0
                                    if openwb_pro:
                                        _phase_up_start_hold_amp = max(
                                            6,
                                            min(
                                                int(charger_max_amp),
                                                int(
                                                    max(
                                                        current_amp or 0,
                                                        (charger_status or {}).get("amp", 0) or 0,
                                                        c_data.get("current_set_amp", 0) or 0,
                                                        cap_amp or 0,
                                                    )
                                                ),
                                            ),
                                        )
                                        _mark_openwb_pro_phase_wait(
                                            c_data,
                                            3,
                                            current_amp=_phase_up_start_hold_amp,
                                            now_ts=now_ts,
                                            config=config,
                                            charger_max_amp=charger_max_amp,
                                        )
                                        if _execute_wallbox_driver_command(
                                            c_data,
                                            {
                                                "method": "set_amp_and_state",
                                                "amp": _phase_up_start_hold_amp,
                                                "force_state": None,
                                                "reason": "openwb_pro_phase_start_hold_3p",
                                            },
                                            c_id=c_id,
                                        ):
                                            _phase_up_start_hold_sent = True
                                            _mark_openwb_pro_start_offer(
                                                c_data,
                                                _phase_up_start_hold_amp,
                                                now_ts=now_ts,
                                                config=config,
                                                charger_max_amp=charger_max_amp,
                                                refresh=True,
                                            )
                                    _remember_phase_target(
                                        c_data,
                                        3,
                                        now_ts,
                                        max(phase_3p_start_hold_s, phase_confirm_timeout_s) + 60.0,
                                    )
                                    c_data["_last_phase_switch_ts"] = now_ts
                                    c_data["_phase_change_seen_session"] = True
                                    c_data["_wb_stable_budget_jump_done"] = False
                                    c_data["_wb_stable_budget_jump_ts"] = 0.0
                                    c_data["last_storage_guided_amp_up_ts"] = now_ts
                                    c_data["_phase_3p_pending_since"] = now_ts
                                    c_data["_phase_up_budget_since"] = 0.0
                                    c_data["_pv_mode_active"] = False
                                    if openwb_pro and _phase_up_start_hold_sent:
                                        c_data["current_set_amp"] = _phase_up_start_hold_amp
                                        c_data["_last_openwb_hold_amp"] = _phase_up_start_hold_amp
                                        c_data["_wb_stop_sent_active"] = False
                                        current_amp = _phase_up_start_hold_amp
                                    else:
                                        c_data["current_set_amp"] = 0
                                        current_amp = 0
                                    last_change_ts[c_id] = now_ts
                                    if openwb_pro:
                                        logger.info(
                                            "WB%d openWB auf 3p angefordert, Startfreigabe gehalten "
                                            "(%dA, Modus=%s, Regelpfad=%d, Budget=%.0fW, Grid=%.0fW, PV=%.0fW)" % (
                                                c_id, int(c_data.get("current_set_amp", 0) or 0),
                                                c_public_label, c_control_mode, phase_budget_w,
                                                grid_power_raw, pv_power_raw
                                            )
                                        )
                                    else:
                                        logger.info(
                                            "WB%d openWB auf 3p angefordert "
                                            "(Modus=%s, Regelpfad=%d, Budget=%.0fW, Grid=%.0fW, PV=%.0fW)" % (
                                                c_id, c_public_label, c_control_mode, phase_budget_w,
                                                grid_power_raw, pv_power_raw
                                            )
                                        )
                                    c_data["is_charging"] = bool(hw_charging or (openwb_pro and _phase_up_start_hold_sent))
                                    continue
                        else:
                            c_data["_phase_up_budget_since"] = 0.0

                        if (
                            openwb_phase_capable
                            and phase_target == 3
                            and phase_switch_phases in (0, 1)
                        ):
                            _phase_pending_since = float(c_data.get("_phase_3p_pending_since", 0.0) or 0.0)
                            if _phase_pending_since <= 0.0:
                                _phase_pending_since = now_ts
                                c_data["_phase_3p_pending_since"] = _phase_pending_since
                            _phase_pending_age = now_ts - _phase_pending_since
                            one_phase_confirmed = bool(
                                (
                                    current_phases == 1
                                    or (
                                        vehicle_phase_unknown
                                        and max(
                                            int((charger_status or {}).get("amp", 0) or 0),
                                            int(current_amp or 0),
                                            int(c_data.get("current_set_amp", 0) or 0),
                                        ) >= 6
                                        and float((charger_status or {}).get("real_power_w", 0.0) or 0.0) > 500.0
                                        and float((charger_status or {}).get("real_power_w", 0.0) or 0.0) <
                                            max(
                                                int((charger_status or {}).get("amp", 0) or 0),
                                                int(current_amp or 0),
                                                int(c_data.get("current_set_amp", 0) or 0),
                                            ) * 230.0 * 1.8
                                    )
                                )
                                and hw_charging
                                and float((charger_status or {}).get("real_power_w", 0.0) or 0.0) > 500.0
                            )
                            if (
                                phase_switch_action == "SWITCH_1P"
                                and phase_switch_reason == "unknown_vehicle_1p"
                            ):
                                c_data["_session_1p_only"] = True
                                if _execute_wallbox_driver_command(
                                    c_data,
                                    {
                                        "method": "set_phases",
                                        "phases": 1,
                                        "reason": "unknown_vehicle_1p",
                                    },
                                    c_id=c_id,
                                ):
                                    if openwb_pro:
                                        _mark_openwb_pro_phase_wait(
                                            c_data,
                                            1,
                                            current_amp=max(
                                                int(current_amp or 0),
                                                int((charger_status or {}).get("amp", 0) or 0),
                                                int(c_data.get("current_set_amp", 0) or 0),
                                            ),
                                            now_ts=now_ts,
                                            config=config,
                                            charger_max_amp=charger_max_amp,
                                        )
                                    _remember_phase_target(c_data, 1, now_ts, phase_retry_block_s)
                                    c_data["_last_phase_switch_ts"] = now_ts
                                    c_data["_phase_change_seen_session"] = True
                                    c_data["_wb_stable_budget_jump_done"] = False
                                    c_data["_wb_stable_budget_jump_ts"] = 0.0
                                    c_data["last_storage_guided_amp_up_ts"] = now_ts
                                    c_data["_phase_3p_block_until"] = now_ts + phase_retry_block_s
                                    c_data["_phase_3p_pending_since"] = 0.0
                                    c_data["_phase_up_budget_since"] = 0.0
                                    c_data["_pv_mode_active"] = False
                                    _save_wallbox_phase_state(chargers)
                                    last_change_ts[c_id] = now_ts
                                    logger.warning(
                                        "WB%d openWB 3p nicht bestaetigt nach %.0fs echter 1p-Ladung -> zurueck auf 1p, %.0fmin Sperre%s" % (
                                            c_id, _phase_pending_age, phase_retry_block_s / 60.0,
                                            " (Gast-/Fallback-Lernen)" if vehicle_phase_unknown else ""
                                        )
                                    )
                                c_data["is_charging"] = hw_charging
                                c_data["current_set_amp"] = min(current_amp, 6) if current_amp > 0 else 0
                                continue
                            # Umschaltung angefordert, aber noch nicht als
                            # 3-phasig bestaetigt. Die openWB Pro uebernimmt
                            # die Pause/Signalisierung selbst; bei der normalen
                            # openWB halten wir konservativ 6A.
                            if (not openwb_pro) and current_amp > 6 and hasattr(charger, "set_amp_and_state"):
                                _execute_wallbox_driver_command(
                                    c_data,
                                    {
                                        "method": "set_amp_and_state",
                                        "amp": 6,
                                        "force_state": None,
                                        "reason": "pending_3p_confirmation_hold",
                                    },
                                    c_id=c_id,
                                )
                                c_data["current_set_amp"] = 6
                                last_change_ts[c_id] = now_ts
                            c_data["is_charging"] = hw_charging
                            if not openwb_pro:
                                continue
                        elif openwb_phase_capable:
                            c_data["_phase_3p_pending_since"] = 0.0

                        if (
                            openwb_phase_capable
                            and not local_price_optimizing_active
                            and not local_grid_allowed
                            and c_control_mode > 0
                            and cap_amp > 0
                            and phase_switch_phases not in (0, 1)
                            and not phase_3p_keep_supported
                            and grid_power_budget_w > phase_down_grid_w
                            and (now_ts - float(c_data.get("_phase_down_since", now_ts) or now_ts)) >= phase_down_delay_s
                        ):
                            # 3p ist wirklich zu lange zu schwer fuer den
                            # Netzpunkt. Die eigentliche Rueckschaltung wurde
                            # oben bereits angefordert; bis sie bestaetigt ist,
                            # keine neue hohe Rampe starten.
                            c_data["is_charging"] = hw_charging
                            c_data["current_set_amp"] = 0
                            continue

                        if (
                            openwb_pro
                            and cap_amp > 6
                            and charger_connected
                            and not hw_charging
                            and not (phase_target == 3 or phase_3p_supported)
                            and float((charger_status or {}).get("real_power_w", 0.0) or 0.0) <= 500.0
                        ):
                            # openWB Pro Standalone startet zuverlaessig, wenn
                            # Phasenziel und Sollstrom stabil bleiben. Nicht auf
                            # 6A zwangsdeckeln; das kann manche Fahrzeuge im
                            # Wakeup haengen lassen. Der Start-Hold unten friert
                            # stattdessen den berechneten Strom ein.
                            pass

                        if openwb_pro:
                            _openwb_pro_phase_sequence = c_data.get("_openwb_pro_phase_sequence")
                            if isinstance(_openwb_pro_phase_sequence, dict) and _openwb_pro_phase_sequence:
                                _sequence_target = int(_openwb_pro_phase_sequence.get("target", 0) or 0)
                                _sequence_stage = str(_openwb_pro_phase_sequence.get("stage") or "")
                                _sequence_zero_until = float(_openwb_pro_phase_sequence.get("zero_until", 0.0) or 0.0)
                                _sequence_restart_after = float(
                                    _openwb_pro_phase_sequence.get("current_allowed_after", 0.0) or 0.0
                                )
                                _sequence_due = bool(
                                    (
                                        _sequence_stage == "zero_wait"
                                        and _sequence_zero_until > 0.0
                                        and now_ts >= _sequence_zero_until
                                    )
                                    or (
                                        _sequence_stage == "restart_delay"
                                        and _sequence_restart_after > 0.0
                                        and now_ts >= _sequence_restart_after
                                    )
                                    or _sequence_stage == "cp_after_phase"
                                )
                                if _sequence_target in (1, 3) and _sequence_due:
                                    _execute_wallbox_driver_command(
                                        c_data,
                                        {
                                            "method": "set_phases",
                                            "phases": _sequence_target,
                                            "reason": str(
                                                _openwb_pro_phase_sequence.get("reason")
                                                or "openwb_pro_phase_sequence_continue"
                                            ),
                                            "_openwb_pro_sequence_internal": True,
                                        },
                                        c_id=c_id,
                                    )
                                    c_data["is_charging"] = bool(hw_charging)
                                    c_data["current_set_amp"] = 0
                                    continue

                        _phase_wait_sequence = c_data.get("_openwb_pro_phase_sequence")
                        _phase_wait_sequence_stage = (
                            str(_phase_wait_sequence.get("stage") or "")
                            if isinstance(_phase_wait_sequence, dict)
                            else ""
                        )
                        _phase_wait_sequence_active = bool(
                            isinstance(_phase_wait_sequence, dict)
                            and _phase_wait_sequence
                            and _phase_wait_sequence_stage not in ("", "ready")
                        )
                        if openwb_pro_phase_wait_active and _phase_wait_sequence_active:
                            _phase_wait_hard_abort = bool(
                                priority_forced_stop
                                or c_public_mode == MODE_OFF
                                or not charger_connected
                                or _budget_timeout
                                or (
                                    storage_charge_priority_active
                                    and grid_power_raw > -800.0
                                    and not (
                                        local_grid_allowed
                                        or local_price_optimizing_active
                                        or price_boost_wallbox_active
                                        or predump_wallbox_active
                                    )
                                )
                                or (
                                    grid_power_budget_w > max(3500.0, phase_down_grid_w * 2.0)
                                    and not (
                                        local_grid_allowed
                                        or local_price_optimizing_active
                                        or price_boost_wallbox_active
                                    )
                                )
                            )
                            _phase_wait_amp = int(
                                max(
                                    c_data.get("_openwb_pro_phase_wait_amp", 0) or 0,
                                    c_data.get("_openwb_pro_phase_sequence", {}).get("hold_amp", 0)
                                    if isinstance(c_data.get("_openwb_pro_phase_sequence"), dict)
                                    else 0,
                                )
                            )
                            if _phase_wait_hard_abort:
                                _execute_wallbox_driver_command(
                                    c_data,
                                    {
                                        "method": "set_amp_and_state",
                                        "amp": 0,
                                        "force_state": 1,
                                        "reason": "openwb_pro_phase_wait_abort_zero",
                                    },
                                    c_id=c_id,
                                )
                            c_data["current_set_amp"] = 0
                            c_data["is_charging"] = bool(hw_charging)
                            c_data["_wb_stop_sent_active"] = False
                            _log_state_once(
                                c_data,
                                "openwb_pro_phase_wait",
                                (
                                    int(c_data.get("_openwb_pro_phase_wait_target", 0) or 0),
                                    _phase_wait_amp,
                                    c_public_label,
                                ),
                                "WB%d openWB Pro Phasenwechsel: 0A gehalten, "
                                "keine Stromrampe vor Sequenzende (Ziel=%dp, Halte-Amp=%dA, Grid=%+.0fW)" % (
                                    c_id,
                                    int(c_data.get("_openwb_pro_phase_wait_target", 0) or 0),
                                    _phase_wait_amp,
                                    grid_power_raw,
                                ),
                                min_interval_s=60.0,
                            )
                            continue

                        if openwb_pro_curve_direct_charger and charger_connected:
                            # openWB Pro im PV-Kurve-Modus ist der primaere
                            # Stellaktor. Danach darf der alte Fuzzy-/Deckelpfad
                            # in diesem Zyklus keinen zweiten Sollstrom setzen.
                            direct_phases = int(
                                current_phases
                                or phase_switch_phases
                                or phase_cap_phases
                                or detected_phases
                                or 1
                            )
                            direct_phases = max(1, direct_phases)
                            direct_step_amp = _current_step_amp_for_charger(c_data.get("charger"), default=1.0)
                            direct_amp, direct_assist_gap_w = _openwb_pro_curve_direct_amp(
                                openwb_pro_curve_direct_local_w,
                                direct_phases,
                                charger_max_amp,
                                assist_allowed=bool(
                                    curve_forecast_wallbox_assist_allowed
                                    and not _priority_export_fallback_charger
                                ),
                                assist_max_gap_w=_sf(
                                    config.get("wb_curve_direct_assist_max_gap_w", 230.0 * direct_phases + 80.0),
                                    230.0 * direct_phases + 80.0,
                                ),
                                current_step_amp=direct_step_amp,
                                watts_per_amp=_openwb_pro_effective_w_per_amp(
                                    charger_status,
                                    phases=direct_phases,
                                    current_amp=current_amp,
                                ),
                            )
                            direct_amp = _cap_openwb_pro_one_phase_amp(
                                direct_amp,
                                direct_phases,
                                config,
                                c_id,
                                charger_max_amp,
                            )
                            c_data["_openwb_pro_curve_direct_assist_w"] = direct_assist_gap_w
                            hw_offered_amp = _sf((charger_status or {}).get("amp", 0), 0.0)
                            current_amp = max(float(current_amp or 0.0), hw_offered_amp)
                            openwb_pro_session_state_text = str(c_data.get("_openwb_pro_session_state") or "")
                            allow_amp_update_in_starting = bool(
                                openwb_pro
                                and openwb_pro_session_state_text in ("starting", "offered")
                                and cap_amp > int(c_data.get("current_set_amp", 0) or 0)
                                and not priority_forced_stop
                                and not bool(c_data.get("_wb_stop_sent_active", False))
                            )
                            if allow_amp_update_in_starting:
                                direct_amp = max(float(direct_amp or 0.0), float(cap_amp or 0.0))
                            if _openwb_pro_start_hold_active(
                                c_data,
                                now_ts,
                                hw_charging=hw_charging,
                                stable_hw_power_w=stable_hw_power_w,
                            ) and grid_power_raw < 1500.0:
                                # Startfreigabe ist ein Angebot an Auto/Wallbox.
                                # Waehrend der Wakeup-Zeit nicht mit 0A dagegen
                                # regeln, nur weil noch keine stabile Leistung
                                # gemessen wird.
                                current_amp = max(
                                    float(current_amp or 0.0),
                                    float(c_data.get("_openwb_pro_start_hold_amp", 0) or 0),
                                )
                                direct_amp = current_amp
                            if (
                                int(wb_dist_mode) in (1, 2)
                                and c_id != int(wb_dist_mode)
                                and not (local_grid_allowed or local_price_optimizing_active or price_boost_wallbox_active)
                                and not _priority_export_fallback_charger
                            ):
                                if priority_forced_stop or int(cap_amp or 0) <= 0:
                                    direct_amp = 0
                                else:
                                    direct_amp = min(float(direct_amp or 0.0), float(cap_amp or 0.0))
                            direct_down_trigger_w = max(150.0, min(700.0, grid_reserve_w * 0.5))
                            direct_over_curve_cap = bool(
                                direct_amp > 0
                                and direct_amp < current_amp
                                and (float(current_amp or 0.0) - float(direct_amp or 0.0)) + 1e-6 >= direct_step_amp
                            )
                            if direct_amp < current_amp and (grid_power_raw > direct_down_trigger_w or direct_over_curve_cap):
                                direct_direction = -1
                            elif direct_amp > current_amp and grid_power_raw < -900.0:
                                direct_direction = 1
                            else:
                                direct_direction = 0
                            if allow_amp_update_in_starting and direct_amp > current_amp:
                                direct_direction = 1
                            direct_since_key = "_openwb_pro_curve_direct_main_since"
                            direct_last_direction = int(
                                c_data.get("_openwb_pro_curve_direct_main_direction", 0) or 0
                            )
                            if direct_direction == 0:
                                c_data[direct_since_key] = 0.0
                                c_data["_openwb_pro_curve_direct_main_direction"] = 0
                            elif (
                                direct_last_direction != direct_direction
                                or c_data.get(direct_since_key, 0.0) <= 0.0
                            ):
                                c_data[direct_since_key] = now_ts
                                c_data["_openwb_pro_curve_direct_main_direction"] = direct_direction
                            direct_age_s = now_ts - float(c_data.get(direct_since_key, now_ts) or now_ts)
                            direct_down_hold_s = max(
                                10.0,
                                _sf(config.get("wb_openwb_pro_curve_down_hold_s", 10), 10.0),
                            )
                            direct_up_hold_s = max(
                                direct_down_hold_s,
                                _sf(config.get("wb_openwb_pro_curve_up_hold_s", 60), 60.0),
                            )
                            direct_min_delta_amp = max(
                                float(direct_step_amp or 1.0),
                                _sf(config.get("wb_openwb_pro_curve_min_delta_a", 0.5), 0.5),
                            )
                            direct_bulk_ready = _openwb_pro_direct_bulk_ready(
                                charger_status,
                                hw_charging=hw_charging,
                                stable_hw_power_w=stable_hw_power_w,
                            )
                            direct_required_s = (
                                direct_down_hold_s
                                if direct_direction < 0
                                else (0.0 if float(current_amp or 0.0) <= 0.0 else direct_up_hold_s)
                            )
                            if allow_amp_update_in_starting:
                                direct_required_s = 0.0
                            direct_target_amp = _openwb_pro_direct_target_amp(
                                current_amp,
                                direct_amp,
                                direct_direction,
                                bulk_ready=direct_bulk_ready,
                                current_step_amp=direct_step_amp,
                            )
                            direct_due = bool(
                                (allow_amp_update_in_starting or direct_direction != 0)
                                and abs(float(direct_amp or 0.0) - float(current_amp or 0.0)) + 1e-6 >= direct_min_delta_amp
                                and direct_age_s >= direct_required_s
                                and now_ts - float(c_data.get("last_fast_ts", 0.0) or 0.0) >= FAST_GRID_SECS
                            )
                            if direct_due:
                                fs = 2 if float(current_amp or 0.0) <= 0.0 and direct_target_amp > 0 else None
                                if _execute_wallbox_driver_command(
                                    c_data,
                                    {
                                        "method": "set_amp_and_state",
                                        "amp": direct_target_amp,
                                        "force_state": fs,
                                        "reason": "openwb_pro_curve_direct_main",
                                    },
                                    c_id=c_id,
                                ):
                                    if direct_target_amp > 0:
                                        _mark_manager_charge_anchor(
                                            c_data,
                                            amp=direct_target_amp,
                                            reason="openwb_pro_curve_direct_main",
                                            reset_real_marker=bool(float(current_amp or 0.0) <= 0.0 and not hw_charging),
                                        )
                                        if float(current_amp or 0.0) <= 0.0 and not hw_charging:
                                            _mark_openwb_pro_start_offer(
                                                c_data,
                                                direct_target_amp,
                                                now_ts=now_ts,
                                                config=config,
                                                charger_max_amp=charger_max_amp,
                                                refresh=True,
                                            )
                                    else:
                                        _mark_manager_zero_anchor(c_data, reason="openwb_pro_curve_direct_main_zero")
                                    c_data["current_set_amp"] = direct_target_amp
                                    c_data["_last_openwb_hold_amp"] = direct_target_amp
                                    c_data["is_charging"] = bool(direct_target_amp > 0)
                                    c_data["_wb_stop_sent_active"] = bool(direct_target_amp <= 0)
                                    c_data["_pv_mode_active"] = False
                                    c_data["last_fast_ts"] = now_ts
                                    c_data["fast_block_until"] = now_ts + max(
                                        direct_down_hold_s,
                                        _sf(config.get("wb_openwb_pro_curve_hold_s", 30), 30.0),
                                    )
                                    c_data[direct_since_key] = 0.0
                                    c_data["_openwb_pro_curve_direct_main_direction"] = 0
                                    last_change_ts[c_id] = now_ts
                                    made_changes = True
                                    logger.info(
                                        "WB%d openWB Pro PV-Kurve direkt: %.1fA -> %.1fA "
                                        "(Grid=%+.0fW, Ziel=%.0fW, %dp, gehalten %.0fs)" % (
                                            c_id,
                                            float(current_amp or 0.0),
                                            float(direct_target_amp or 0.0),
                                            grid_power_raw,
                                            openwb_pro_curve_direct_local_w,
                                            direct_phases,
                                            direct_age_s,
                                        )
                                    )
                            else:
                                c_data["current_set_amp"] = float(current_amp or 0.0)
                                c_data["is_charging"] = bool(hw_charging or current_amp > 0)
                                c_data["_wb_stop_sent_active"] = False
                                c_data["_pv_mode_active"] = False
                            if c_data.get("is_charging", False) or int(c_data.get("current_set_amp", 0) or 0) > 0:
                                active_chargers_count += 1
                                total_set_amp += int(c_data.get("current_set_amp", 0) or 0)
                            continue

                        # openWB PV-Kurve: Nur der explizite PV-Modus darf die
                        # openWB in ihren eigenen PV-Regler uebergeben. Ziel
                        # wbminSoC/Preis/Grundladung bleiben Python-gefuehrt;
                        # sonst springt eine manuell gestoppte openWB bei
                        # jedem Steck-/Statuswechsel wieder auf PV.
                        if (
                            openwb_pv_capable
                            and c_public_mode == MODE_CURVE
                            and not priority_forced_stop
                            and not local_price_optimizing_active
                            and cap_amp == 0
                        ):
                            if storage_charge_reserve_w > 0 and hasattr(c_data["charger"], "set_direct_current"):
                                if hw_charging or c_data.get("current_set_amp", 0) != 0:
                                    _mark_manager_zero_anchor(c_data, reason="storage_charge_reserve")
                                    _execute_wallbox_driver_command(
                                        c_data,
                                        {
                                            "method": "set_direct_current",
                                            "amp": 0,
                                            "reason": "storage_charge_reserve",
                                        },
                                        c_id=c_id,
                                    )
                                    logger.info(
                                        "WB%d openWB pausiert: Speicher-Reserve %.0fW bis wbminSoC" %
                                        (c_id, storage_charge_reserve_w)
                                    )
                                c_data["_wb_stop_sent_active"] = False
                                c_data["_pv_mode_active"] = True
                                c_data["is_charging"] = False
                                c_data["current_set_amp"] = 0
                                continue
                            if not c_data.get("_pv_mode_active", False):
                                _execute_wallbox_driver_command(
                                    c_data,
                                    {
                                        "method": "set_pv_mode",
                                        "reason": "openwb_curve_pv_only",
                                    },
                                    c_id=c_id,
                                )
                                c_data["_pv_mode_active"] = True
                                logger.info(
                                    "WB%d openWB PV-only freigegeben "
                                    "(Modus=%s, Regelpfad=%d, keine Batterie-/Netzstuetzung)" %
                                    (c_id, c_public_label, c_control_mode)
                                )
                            c_data["_wb_stop_sent_active"] = False
                            c_data["is_charging"] = hw_charging
                            c_data["current_set_amp"] = (
                                int(charger_status.get('amp', 0))
                                if hw_charging and charger_status else 0
                            )
                            if hw_charging:
                                active_chargers_count += 1
                                total_set_amp += c_data.get("current_set_amp", 0)
                            continue

                        if (
                            c_control_mode == 9
                            and openwb_pv_capable
                            and not priority_forced_stop
                            and not local_price_optimizing_active
                            and not (wbminsoc_gate_open and wb_storage_cap_w > 0)
                        ):
                            # openWB regelt PV-Laden selbst. Ausnahme: Wenn der
                            # Speicher bei wbminSoC gehalten wird, muss Python die
                            # openWB stoppen. Sonst zieht eine 3-phasige openWB bei
                            # zu wenig PV Netzstrom, weil keine Batterie mehr stuetzt.
                            hw_charging = bool(charger_status and charger_status.get('charging', False))
                            wb_floor_hold = (not wbminsoc_gate_open) or (_budget_state == 'hold')
                            if wb_floor_hold:
                                if hw_charging or c_data.get("_pv_mode_active", False) or not c_data.get("_wb_floor_stop_active", False):
                                    _send_wallbox_stop_command(
                                        c_data,
                                        c_id=c_id,
                                        reason="wbminsoc_floor_hold",
                                    )
                                    c_data["_wb_floor_stop_active"] = True
                                    c_data["_pv_mode_active"] = False
                                    last_change_ts[c_id] = now_ts
                                    logger.info("WB%d %s: openWB gestoppt (wbminSoC-Hold, keine Batterie-/Netzstuetzung)" % (c_id, c_public_label))
                                c_data["is_charging"] = False
                                c_data["current_set_amp"] = 0
                                continue

                            want_1p = (
                                openwb_phase_capable
                                and current_phases != 1
                                and (pv_power_raw < 4500 or grid_power_budget_w > 200)
                            )
                            if want_1p and now_ts - c_data.get("_last_phase_switch_ts", 0) >= phase_effective_hold_s:
                                if _execute_wallbox_driver_command(
                                    c_data,
                                    {
                                        "method": "set_phases",
                                        "phases": 1,
                                        "reason": "openwb_pv_1p_request",
                                    },
                                    c_id=c_id,
                                ):
                                    if openwb_pro:
                                        _mark_openwb_pro_phase_wait(
                                            c_data,
                                            1,
                                            current_amp=max(
                                                int(current_amp or 0),
                                                int((charger_status or {}).get("amp", 0) or 0),
                                                int(c_data.get("current_set_amp", 0) or 0),
                                            ),
                                            now_ts=now_ts,
                                            config=config,
                                            charger_max_amp=charger_max_amp,
                                        )
                                    _remember_phase_target(c_data, 1, now_ts, phase_down_reup_block_s)
                                    c_data["_last_phase_switch_ts"] = now_ts
                                    c_data["_phase_change_seen_session"] = True
                                    c_data["_wb_stable_budget_jump_done"] = False
                                    c_data["_wb_stable_budget_jump_ts"] = 0.0
                                    c_data["last_storage_guided_amp_up_ts"] = now_ts
                                    c_data["_pv_mode_active"] = False
                                    last_change_ts[c_id] = now_ts
                                    logger.info("WB%d %s: openWB auf 1p angefordert (PV=%.0fW Grid=%.0fW)" % (
                                        c_id, c_public_label, pv_power_raw, grid_power_raw))

                            if not c_data.get("_pv_mode_active", False):
                                _execute_wallbox_driver_command(
                                    c_data,
                                    {
                                        "method": "set_pv_mode",
                                        "reason": "openwb_mode9_pv",
                                    },
                                    c_id=c_id,
                                )
                                c_data["_pv_mode_active"] = True
                                c_data["_wb_floor_stop_active"] = False
                                c_data["_wb_stop_sent_active"] = False
                                last_change_ts[c_id] = now_ts
                                logger.info("WB%d %s: openWB Secondary-Heartbeat aktiv (Monitor-only)" % (c_id, c_public_label))
                            c_data["is_charging"] = hw_charging
                            c_data["current_set_amp"] = int(charger_status.get('amp', 0)) if hw_charging and charger_status else 0
                            if hw_charging:
                                active_chargers_count += 1
                                total_set_amp += c_data.get("current_set_amp", 0)
                            continue

                        if openwb_pro and charger_connected and hw_charging:
                            _pro_start_confirm_s = max(
                                10.0,
                                _sf(config.get("openwb_pro_start_confirm_s", 20), 20.0),
                            )
                            _pro_real_since = float(c_data.get("_real_charge_since", 0.0) or 0.0)
                            if (
                                stable_hw_power_w > 500.0
                                and _pro_real_since > 0.0
                                and now_ts - _pro_real_since >= _pro_start_confirm_s
                            ):
                                c_data["_openwb_pro_start_hold_until"] = 0.0
                                c_data["_openwb_pro_start_hold_amp"] = 0
                                c_data["_openwb_start_retry_count"] = 0
                        elif openwb_pro and charger_connected and not hw_charging and cap_amp > 0:
                            _pro_start_hold_s = max(
                                60.0,
                                _sf(config.get("openwb_pro_start_hold_s", 180), 180.0)
                            )
                            _pro_start_hold_until = float(c_data.get("_openwb_pro_start_hold_until", 0.0) or 0.0)
                            _pro_start_hold_amp = int(c_data.get("_openwb_pro_start_hold_amp", 0) or 0)
                            if current_amp > 0 and _pro_start_hold_until <= 0.0:
                                # Nach Restart oder Phasenwechsel kann bereits
                                # ein Sollstrom anliegen, ohne dass das Auto
                                # wirklich zieht. Fuer die Pro ist dann ein
                                # stabiler berechneter Startstrom besser als ein
                                # wandernder Deckel.
                                _pro_start_hold_until = now_ts + _pro_start_hold_s
                                _pro_start_hold_amp = max(
                                    6,
                                    min(int(charger_max_amp), int(cap_amp or current_amp or 6)),
                                )
                                c_data["_openwb_pro_start_hold_until"] = _pro_start_hold_until
                                c_data["_openwb_pro_start_hold_amp"] = _pro_start_hold_amp
                                _mark_openwb_pro_start_offer(
                                    c_data,
                                    _pro_start_hold_amp,
                                    now_ts=now_ts,
                                    config=config,
                                    charger_max_amp=charger_max_amp,
                                )
                            if current_amp > 0 and now_ts < _pro_start_hold_until and _pro_start_hold_amp >= 6:
                                cap_amp = max(6, min(int(charger_max_amp), _pro_start_hold_amp))

                        if (
                            c_public_mode != MODE_OFF
                            and c_control_mode != 0
                        ):
                            cap_amp = _apply_stable_wallbox_amp_contract(
                                c_data,
                                cap_amp,
                                current_amp,
                                stable_hw_power_w,
                                hw_charging,
                            )
                        if (
                            openwb_pro
                            and charger_connected
                            and not hw_charging
                            and cap_amp > 0
                            and now_ts < float(c_data.get("_openwb_pro_start_hold_until", 0.0) or 0.0)
                            and int(c_data.get("_openwb_pro_start_hold_amp", 0) or 0) >= 6
                            and (
                                bool(_physical_budget.get("can_start_or_hold", False))
                                or grid_power_raw < -800.0
                                or local_grid_allowed
                                or local_price_optimizing_active
                                or price_boost_wallbox_active
                            )
                        ):
                            cap_amp = max(
                                int(cap_amp),
                                min(
                                    int(charger_max_amp),
                                    int(c_data.get("_openwb_pro_start_hold_amp", 0) or 0),
                                ),
                            )

                        _gate_phases = max(
                            1,
                            int(
                                phase_cap_phases
                                or phase_switch_phases
                                or current_phases
                                or detected_phases
                                or 1
                            ),
                        )
                        _gate_min_power_w = float(wb_min_amp_cfg) * 230.0 * float(_gate_phases)
                        _gate_budget_w = max(
                            float(c_allowed_w or 0.0),
                            float(cap_amp or 0) * 230.0 * float(_gate_phases),
                        )
                        _pv_hybrid_gate_enabled = bool(
                            c_public_mode != MODE_OFF
                            and c_control_mode > 0
                            and charger_connected
                            and not priority_forced_stop
                            and not _budget_timeout
                            and not (
                                local_grid_allowed
                                or local_price_optimizing_active
                                or price_boost_wallbox_active
                                or predump_wallbox_active
                                or c_id in scheduled_slot_charger_ids
                            )
                        )
                        _pv_hybrid_gate = wallbox_decision.pv_hybrid_energy_gate(
                            previous=c_data.get("_pv_hybrid_energy_gate"),
                            now_ts=now_ts,
                            budget_w=_gate_budget_w,
                            min_power_w=_gate_min_power_w,
                            cap_amp=cap_amp,
                            min_amp=wb_min_amp_cfg,
                            current_amp=current_amp,
                            current_set_amp=int(c_data.get("current_set_amp", 0) or 0),
                            hw_charging=hw_charging,
                            hw_power_w=stable_hw_power_w,
                            grid_power_w=grid_power_raw,
                            charger_connected=charger_connected,
                            start_hold_s=max(20.0, _sf(config.get("wb_pv_start_integral_s", 60), 60.0)),
                            start_energy_wh=max(0.0, _sf(config.get("wb_pv_start_integral_wh", 35), 35.0)),
                            strong_surplus_w=max(300.0, _sf(config.get("wb_pv_strong_start_surplus_w", 1500), 1500.0)),
                            stop_hold_s=max(120.0, cloud_stop_delay_s),
                            stop_energy_wh=max(0.0, _sf(config.get("wb_pv_stop_integral_wh", 75), 75.0)),
                            hard_import_w=max(1200.0, _sf(config.get("wb_pv_hard_import_w", 2500), 2500.0)),
                            enabled=_pv_hybrid_gate_enabled,
                        )
                        c_data["_pv_hybrid_energy_gate"] = _pv_hybrid_gate
                        _pv_gate_running = bool(
                            hw_charging
                            or stable_hw_power_w > 500.0
                            or int(current_amp or 0) >= int(wb_min_amp_cfg)
                            or int(c_data.get("current_set_amp", 0) or 0) >= int(wb_min_amp_cfg)
                        )
                        _pv_curve_mode_switch_quiet_until = float(
                            c_data.get("_pv_curve_mode_switch_quiet_until", 0.0) or 0.0
                        )
                        _pv_curve_mode_switch_quiet_active = bool(
                            pv_curve_mode_switch_quiet_supported
                            and c_public_mode == MODE_CURVE
                            and not _pv_gate_running
                            and now_ts < _pv_curve_mode_switch_quiet_until
                        )
                        if c_public_mode != MODE_CURVE or _pv_gate_running:
                            c_data["_pv_curve_mode_switch_quiet_until"] = 0.0
                        _pv_hybrid_action = wallbox_decision.pv_hybrid_hold_action(
                            gate=_pv_hybrid_gate,
                            enabled=_pv_hybrid_gate_enabled,
                            cap_amp=cap_amp,
                            current_amp=current_amp,
                            current_set_amp=int(c_data.get("current_set_amp", 0) or 0),
                            min_amp=wb_min_amp_cfg,
                            max_amp=charger_max_amp,
                            allowed_w=c_allowed_w,
                            min_power_w=_gate_min_power_w,
                            gate_running=_pv_gate_running,
                            mode_switch_quiet_active=_pv_curve_mode_switch_quiet_active,
                            mode_switch_quiet_remaining_s=max(
                                0.0,
                                _pv_curve_mode_switch_quiet_until - now_ts,
                            ),
                            floor_battery_guard_active=bool(
                                floor_pv_only_guard_for_wb
                                and controlled_floor_battery_guard_active
                            ),
                        )
                        c_data["_pv_hybrid_hold_action"] = _pv_hybrid_action
                        _pv_hybrid_action_name = str(_pv_hybrid_action.get("action", "ALLOW_PV_HYBRID") or "")
                        if _pv_hybrid_action_name == "HOLD_START_PV_HYBRID":
                            cap_amp = int(_pv_hybrid_action.get("target_amp", 0) or 0)
                            if str(_pv_hybrid_action.get("log_key", "")) == "pv_curve_mode_switch_quiet_wait":
                                _log_state_once(
                                    c_data,
                                    "pv_curve_mode_switch_quiet_wait",
                                    (
                                        int(_pv_hybrid_action.get("quiet_remaining_s", 0.0)),
                                        c_public_label,
                                        c_control_mode,
                                    ),
                                    "WB%d %s: Start wartet nach Moduswechsel/Pause-Freigabe "
                                    "(%.0fs Rest, Regelpfad=%d)" % (
                                        c_id,
                                        c_public_label,
                                        float(_pv_hybrid_action.get("quiet_remaining_s", 0.0) or 0.0),
                                        c_control_mode,
                                    ),
                                    min_interval_s=30.0,
                                )
                            else:
                                _log_state_once(
                                    c_data,
                                    "pv_hybrid_start_integral_wait",
                                    (
                                        int(_pv_hybrid_action.get("positive_age_s", 0.0)),
                                        int(_pv_hybrid_action.get("positive_wh", 0.0)),
                                        c_public_label,
                                        c_control_mode,
                                    ),
                                    "WB%d %s: Start wartet auf stabiles PV-/Speicherbudget "
                                    "(%.0fs, %.0fWh, Regelpfad=%d)" % (
                                        c_id,
                                        c_public_label,
                                        float(_pv_hybrid_action.get("positive_age_s", 0.0) or 0.0),
                                        float(_pv_hybrid_action.get("positive_wh", 0.0) or 0.0),
                                        c_control_mode,
                                    ),
                                    min_interval_s=60.0,
                                )
                        elif _pv_hybrid_action_name == "HOLD_STOP_PV_HYBRID":
                            cap_amp = int(_pv_hybrid_action.get("target_amp", cap_amp) or 0)
                            c_allowed_w = float(_pv_hybrid_action.get("allowed_w", c_allowed_w) or c_allowed_w or 0.0)
                            _log_state_once(
                                c_data,
                                "pv_hybrid_stop_integral_hold",
                                (
                                    int(_pv_hybrid_action.get("negative_age_s", 0.0)),
                                    int(_pv_hybrid_action.get("negative_wh", 0.0)),
                                    c_public_label,
                                    c_control_mode,
                                ),
                                "WB%d %s: halte statt Stop bis negative Energiebilanz stabil ist "
                                "(%.0fs, %.0fWh, Regelpfad=%d)" % (
                                    c_id,
                                    c_public_label,
                                    float(_pv_hybrid_action.get("negative_age_s", 0.0) or 0.0),
                                    float(_pv_hybrid_action.get("negative_wh", 0.0) or 0.0),
                                    c_control_mode,
                                ),
                                min_interval_s=60.0,
                            )

                        if cap_amp > 0:
                            c_data["_native_multi_zero_budget_since"] = 0.0
                        elif (
                            c_public_mode != MODE_OFF
                            and c_control_mode in (3, 6, 9, 10, 11)
                            and hasattr(c_data["charger"], "set_amp_sonnenmodus")
                            and not hasattr(c_data["charger"], "set_pv_mode")
                            and charger_connected
                            and not priority_forced_stop
                            and not local_price_optimizing_active
                            and not local_grid_allowed
                            and not price_boost_wallbox_active
                            and (
                                hw_charging
                                or stable_hw_power_w > 500.0
                                or int(current_amp or 0) > 0
                                or int(c_data.get("current_set_amp", 0) or 0) > 0
                            )
                        ):
                            if float(c_data.get("_native_multi_zero_budget_since", 0.0) or 0.0) <= 0.0:
                                c_data["_native_multi_zero_budget_since"] = now_ts

                        if in_hold:
                            # Haltezeit: nur minimale Anpassungen (keine Sprunge)
                            pass
                        else:
                            if cap_amp == 0 and (priority_forced_stop or not base_6a_active) and not price_boost_wallbox_active:
                                # Kein Laden: Strom auf 6A setzen, dann Stop-Toggle.
                                # In Mode=1 (Sonnenmodus): set_amp_sonnenmodus(6, force_state=1) setzt
                                # WBchar6[1]=6A und Toggle=STOP - exakt wie Eba's Stopp-Sequenz.
                                # Das verhindert abrupte Abbrueche und schont das Fahrzeug-OBC.
                                hw_charging = bool(charger_status and charger_status.get('charging', False))
                                hw_power_w = float((charger_status or {}).get('real_power_w', 0.0) or 0.0)
                                e3dc_native_toggle = (
                                    hasattr(c_data["charger"], "set_amp_sonnenmodus")
                                    and not hasattr(c_data["charger"], "set_pv_mode")
                                )
                                is_multi_direct_toggle = (
                                    getattr(c_data.get("charger"), "driver_variant", "") == "e3dc_multi_connect"
                                )
                                _last_start_age = now_ts - float(c_data.get("last_start_ts", 0.0) or 0.0)
                                min_charge_hold_active = bool(
                                    min_charge_time_s > 0.0
                                    and 0.0 <= _last_start_age < min_charge_time_s
                                    and charger_connected
                                    and not priority_forced_stop
                                    and not local_price_optimizing_active
                                    and not local_grid_allowed
                                    and not price_boost_wallbox_active
                                    and not _budget_timeout
                                    and grid_power_budget_w < 2500.0
                                    and (
                                        hw_charging
                                        or hw_power_w > 500.0
                                        or int(current_amp or 0) > 0
                                        or int(c_data.get("current_set_amp", 0) or 0) > 0
                                    )
                                )
                                native_start_grace_active = bool(
                                    c_control_mode in (9, 10, 11)
                                    and not hw_charging
                                    and (
                                        c_data.get("current_set_amp", 0) > 0
                                        or current_amp > 0
                                        or 0.0 <= _last_start_age < 180.0
                                    )
                                    and now_ts < max(
                                        float(c_data.get("_native_multi_start_grace_until", 0.0) or 0.0),
                                        float(c_data.get("last_start_ts", 0.0) or 0.0) + 180.0,
                                    )
                                )
                                if openwb_pro_curve_direct_pv_start_ready:
                                    c_data["_openwb_curve_real_pv_seen_ts"] = now_ts
                                _last_real_pv_seen_ts = float(
                                    c_data.get("_openwb_curve_real_pv_seen_ts", 0.0) or 0.0
                                )
                                _real_pv_hold_age_s = (
                                    now_ts - _last_real_pv_seen_ts
                                    if _last_real_pv_seen_ts > 0.0
                                    else 999999.0
                                )
                                openwb_zero_export_hold_allowed = bool(
                                    c_public_mode != MODE_CURVE
                                    or openwb_pro_curve_direct_pv_start_ready
                                    or _real_pv_hold_age_s < max(20.0, cloud_stop_delay_s)
                                )
                                _floor_pv_min_hold_w = max(
                                    750.0,
                                    float(target_wbminsoc_low_power_min_w or 0.0) - 250.0,
                                )
                                _floor_stop_real_pv_w = max(
                                    0.0,
                                    float(pv_surplus_ex_wb_w or 0.0),
                                    float(free_for_limbs_w or 0.0),
                                )
                                _floor_real_pv_hold_ready = bool(
                                    _floor_stop_real_pv_w >= _floor_pv_min_hold_w
                                    or (
                                        (_physical_budget or {}).get("budget_ready", False)
                                        and float(c_allowed_w or 0.0) >= _floor_pv_min_hold_w
                                    )
                                )
                                openwb_floor_zero_budget_stop_active = bool(
                                    openwb_like_charger
                                    and (
                                        floor_pv_only_guard_for_wb
                                        or controlled_wallbox_wbminsoc_pv_only_active
                                    )
                                    and (
                                        controlled_floor_battery_guard_active
                                        or controlled_wallbox_wbminsoc_pv_only_active
                                        or not wbminsoc_gate_open
                                    )
                                    and cap_amp <= 0
                                    and charger_connected
                                    and (
                                        hw_charging
                                        or hw_power_w > 500.0
                                    )
                                    and not _floor_real_pv_hold_ready
                                    and not (
                                        local_grid_allowed
                                        or local_price_optimizing_active
                                        or price_boost_wallbox_active
                                        or predump_wallbox_active
                                    )
                                )
                                transient_hold_contract = _wallbox_transient_hold_contract(
                                    c_data,
                                    charger_status,
                                    now_ts=now_ts,
                                    phase_grace_s=max(90.0, _openwb_pro_phase_wait_s(config) + 30.0),
                                    openwb_phase_capable=openwb_phase_capable,
                                    current_amp=current_amp,
                                    current_set_amp=int(c_data.get("current_set_amp", 0) or 0),
                                    hw_charging=hw_charging,
                                    hw_power_w=hw_power_w,
                                    phase_wait_active=bool(openwb_pro_phase_wait_active),
                                    start_hold_active=bool(
                                        openwb_pro
                                        and (
                                            _openwb_pro_start_hold_active(
                                                c_data,
                                                now_ts,
                                                hw_charging=hw_charging,
                                                stable_hw_power_w=hw_power_w,
                                            )
                                            or _openwb_pro_recent_start_window_active(
                                                c_data,
                                                now_ts,
                                                config=config,
                                                min_amp=wb_min_amp_cfg,
                                            )
                                        )
                                    ),
                                    native_start_grace_active=native_start_grace_active,
                                    priority_forced_stop=priority_forced_stop,
                                    mode_off=bool(c_public_mode == MODE_OFF),
                                    budget_timeout=_budget_timeout,
                                )
                                openwb_phase_transition_grace_active = bool(
                                    openwb_pro
                                    and openwb_phase_capable
                                    and transient_hold_contract.get("phase_transition_grace_active", False)
                                )
                                multi_phase_verified = bool(
                                    (charger_status or {}).get("phase_power_verified", False)
                                )
                                if (
                                    is_multi_direct_toggle
                                    and not priority_forced_stop
                                    and c_control_mode in (9, 10)
                                    and wbminsoc_gate_open
                                    and (hw_charging or multi_phase_verified)
                                    and not local_price_optimizing_active
                                    and not local_grid_allowed
                                ):
                                    zero_since = float(c_data.get("_native_multi_zero_budget_since", 0.0) or 0.0)
                                    if zero_since <= 0.0:
                                        c_data["_native_multi_zero_budget_since"] = now_ts
                                if (
                                    openwb_like_charger
                                    and not priority_forced_stop
                                    and c_control_mode in (1, 2, 3, 4, 5, 6, 9, 10)
                                    and charger_connected
                                    and not local_price_optimizing_active
                                    and not local_grid_allowed
                                    and not price_boost_wallbox_active
                                    and now_ts - float(c_data.get("abort_cooldown_ts", 0.0) or 0.0) >= 60.0
                                ):
                                    zero_since = float(c_data.get("_openwb_zero_budget_since", 0.0) or 0.0)
                                    if zero_since <= 0.0:
                                        c_data["_openwb_zero_budget_since"] = now_ts
                                stop_already_sent = bool(c_data.get("_wb_stop_sent_active", False))
                                stop_retry_due = (
                                    stop_already_sent
                                    and hw_charging
                                    and hw_power_w > 500
                                    and now_ts - c_data.get("_last_stop_toggle_ts", 0.0) >= 30.0
                                )
                                native_sun_shadow = False
                                if e3dc_native_toggle:
                                    try:
                                        extern_hex = str((charger_status or {}).get("extern_alg_hex", ""))
                                        if len(extern_hex) >= 6:
                                            native_sun_shadow = bool(int(extern_hex[4:6], 16) & 0x80)
                                    except Exception:
                                        native_sun_shadow = False
                                native_mode_no_stop_wait = bool(
                                    e3dc_native_toggle
                                    and not priority_forced_stop
                                    and c_control_mode in (9, 10)
                                    and charger_connected
                                    and wbminsoc_gate_open
                                    and hw_power_w <= 500
                                    and not local_price_optimizing_active
                                    and not local_grid_allowed
                                )
                                if e3dc_native_toggle:
                                    # Bei E3DC/Multi Connect ist Stop ein Toggle.
                                    # Ein altes Python-is_charging darf deshalb
                                    # keinen Stop ausloesen; nur RSCP/Leistung
                                    # loest einen Stop aus. Der Sonnenmodus-
                                    # Schatten (0x80) ist ohne echte Ladeleistung
                                    # kein Grund fuer ein Toggle; sonst erzeugen
                                    # wir genau das Phantom-Start/Stop-Bild.
                                    need_stop_toggle = (
                                        (hw_charging and hw_power_w > 500)
                                        or stop_retry_due
                                    )
                                else:
                                    need_stop_toggle = (
                                        c_data.get("is_charging", False)
                                        or hw_charging
                                        or hw_power_w > 500
                                        or not stop_already_sent
                                        or stop_retry_due
                                    )
                                start_stop_decision = wallbox_decision.start_stop_hold_action(
                                    cap_amp=cap_amp,
                                    current_amp=current_amp,
                                    current_set_amp=int(c_data.get("current_set_amp", 0) or 0),
                                    charger_connected=charger_connected,
                                    hw_charging=hw_charging,
                                    hw_power_w=hw_power_w,
                                    control_mode=c_control_mode,
                                    last_start_age_s=_last_start_age,
                                    min_charge_time_s=min_charge_time_s,
                                    priority_forced_stop=priority_forced_stop,
                                    local_price_optimizing_active=local_price_optimizing_active,
                                    local_grid_allowed=local_grid_allowed,
                                    price_boost_wallbox_active=price_boost_wallbox_active,
                                    budget_timeout=_budget_timeout,
                                    grid_power_w=grid_power_budget_w,
                                    is_multi_direct_toggle=is_multi_direct_toggle,
                                    wbminsoc_gate_open=wbminsoc_gate_open,
                                    multi_phase_verified=multi_phase_verified,
                                    native_multi_zero_budget_age_s=(
                                        now_ts - float(c_data.get("_native_multi_zero_budget_since", now_ts) or now_ts)
                                        if float(c_data.get("_native_multi_zero_budget_since", 0.0) or 0.0) > 0.0
                                        else 0.0
                                    ),
                                    openwb_like_charger=openwb_like_charger,
                                    openwb_pro=openwb_pro,
                                    abort_cooldown_age_s=now_ts - float(c_data.get("abort_cooldown_ts", 0.0) or 0.0),
                                    budget_ok=_budget_ok,
                                    budget_storage_state=str(_budget.get("storage_state", _budget_state) or _budget_state),
                                    openwb_zero_budget_age_s=(
                                        now_ts - float(c_data.get("_openwb_zero_budget_since", now_ts) or now_ts)
                                        if float(c_data.get("_openwb_zero_budget_since", 0.0) or 0.0) > 0.0
                                        else 0.0
                                    ),
                                    cloud_stop_delay_s=cloud_stop_delay_s,
                                    predump_wallbox_active=predump_wallbox_active,
                                    phase_forecast_hold_for_wb=phase_forecast_hold_for_wb,
                                    phase_down_grid_w=phase_down_grid_w,
                                    phase_forecast_zero_hold_s=phase_forecast_zero_hold_s,
                                    stop_already_sent=stop_already_sent,
                                    stop_retry_due=stop_retry_due,
                                    e3dc_native_toggle=e3dc_native_toggle,
                                    native_start_grace_active=native_start_grace_active,
                                    is_charging_memory=bool(c_data.get("is_charging", False)),
                                    openwb_zero_export_hold_allowed=openwb_zero_export_hold_allowed,
                                    openwb_phase_transition_grace_active=openwb_phase_transition_grace_active,
                                    transient_contract=transient_hold_contract,
                                    native_battery_drain_zero_budget_active=native_battery_drain_zero_budget_active,
                                    openwb_floor_zero_budget_stop_active=openwb_floor_zero_budget_stop_active,
                                )
                                if cap_amp > 0:
                                    _ramp_interval_s = (
                                        max(15.0, float(current_change_hold_s or 0.0))
                                        if (
                                            e3dc_native_toggle
                                            and c_control_mode in (4, 9, 10, 11)
                                            and not (local_grid_allowed or local_price_optimizing_active)
                                        )
                                        else max(7.0, float(current_change_hold_s or 0.0))
                                    )
                                    _ramp_bypass = bool(
                                        local_grid_allowed
                                        or local_price_optimizing_active
                                        or price_boost_wallbox_active
                                        or c_id in scheduled_slot_charger_ids
                                        or priority_forced_stop
                                        or _budget_timeout
                                        or bool(c_data.get("_physical_amp_down_active", False))
                                    )
                                    cap_amp, start_stop_decision = _apply_running_ramp_contract(
                                        c_data,
                                        start_stop_decision,
                                        target_amp=cap_amp,
                                        current_amp=current_amp,
                                        current_set_amp=int(c_data.get("current_set_amp", 0) or 0),
                                        charger_connected=charger_connected,
                                        hw_charging=hw_charging,
                                        hw_power_w=stable_hw_power_w,
                                        now_ts=now_ts,
                                        min_amp=wb_min_amp_cfg,
                                        max_amp=charger_max_amp,
                                        ramp_interval_s=_ramp_interval_s,
                                        up_step_a=1,
                                        down_step_a=1,
                                        bypass=_ramp_bypass,
                                    )
                                else:
                                    c_data["_ramp_contract"] = {
                                        "schema_version": "wallbox_running_ramp_v1",
                                        "raw_target_amp": int(cap_amp or 0),
                                        "applied_amp": int(cap_amp or 0),
                                        "current_amp": int(max(current_amp or 0, c_data.get("current_set_amp", 0) or 0)),
                                        "running": bool(hw_charging or stable_hw_power_w > 500.0),
                                        "limited": False,
                                        "changed": False,
                                        "direction": "flat",
                                        "reason": "target_zero",
                                    }
                                decision_payload = wallbox_decision.build_wallbox_decision_payload(
                                    wb_id=c_id,
                                    public_mode=effective_wb_mode,
                                    control_mode=c_control_mode,
                                    current_decision=current_decision,
                                    start_stop_decision=start_stop_decision,
                                    phase_recommendation=phase_recommendation,
                                    allowed_w=allowed_w,
                                    detected_phases=detected_phases,
                                    current_amp=current_amp,
                                    current_set_amp=int(c_data.get("current_set_amp", 0) or 0),
                                    cap_amp=cap_amp,
                                    max_amp=_charger_max_amp(config, c_id, wb_global_max_amp),
                                    charger_connected=charger_connected,
                                    hw_charging=hw_charging,
                                    hw_power_w=hw_power_w,
                                    grid_power_w=grid_power_budget_w,
                                    mode_label=c_public_label,
                                    storage_state=str(_budget.get("storage_state", _budget_state) or _budget_state),
                                    driver_class_name=(
                                        c_data.get("charger").__class__.__name__
                                        if c_data.get("charger") is not None
                                        else ""
                                    ),
                                    openwb_like_charger=openwb_like_charger,
                                    openwb_pro=openwb_pro,
                                    e3dc_native_toggle=e3dc_native_toggle,
                                    observe_only=False,
                                    priority_forced_stop=priority_forced_stop,
                                    budget_timeout=_budget_timeout,
                                )
                                _remember_wallbox_decision_payload(c_data, decision_payload)
                                start_stop_effective = wallbox_decision.start_stop_effective_action_contract(
                                    start_stop_decision,
                                    floor_pv_only_guard_for_wb=floor_pv_only_guard_for_wb,
                                    controlled_floor_battery_guard_active=controlled_floor_battery_guard_active,
                                    current_amp=current_amp,
                                    current_set_amp=int(c_data.get("current_set_amp", 0) or 0),
                                    detected_phases=detected_phases,
                                    low_power_one_phase_required_for_wb=low_power_one_phase_required_for_wb,
                                    physical_budget=_physical_budget,
                                )
                                zero_budget_action = str(start_stop_effective.get("action", "NOOP") or "NOOP")
                                if bool(start_stop_effective.get("decision_changed", False)):
                                    start_stop_decision = dict(start_stop_effective.get("decision") or start_stop_decision)
                                min_charge_hold_active = bool(start_stop_decision.get("min_charge_hold_active", False))
                                multi_zero_budget_hold = bool(start_stop_decision.get("multi_zero_budget_hold", False))
                                openwb_zero_budget_hold = bool(start_stop_effective.get(
                                    "openwb_zero_budget_hold",
                                    start_stop_decision.get("openwb_zero_budget_hold", False),
                                ))
                                controllable_export_cloud_hold = bool(start_stop_decision.get("controllable_export_cloud_hold", False))
                                native_mode_no_stop_wait = bool(start_stop_decision.get("native_mode_no_stop_wait", False))
                                native_start_grace_active = bool(start_stop_decision.get("native_start_grace_active", False))
                                need_stop_toggle = bool(start_stop_decision.get("need_stop_toggle", False))
                                if zero_budget_action == "HOLD_MIN_CHARGE":
                                    hold_amp = int(max(6, start_stop_decision.get("hold_amp", 0)))
                                    c_data["current_set_amp"] = hold_amp
                                    c_data["is_charging"] = True
                                    c_data["_wb_stop_sent_active"] = False
                                    _log_state_once(
                                        c_data,
                                        "wb_min_charge_time_hold",
                                        (hold_amp, c_public_label, c_control_mode),
                                        "WB%d Mindestladezeit: halte %dA statt Stop "
                                        "(%.0fs/%.0fs, Modus=%s, Regelpfad=%d)" % (
                                            c_id,
                                            hold_amp,
                                            max(0.0, _last_start_age),
                                            min_charge_time_s,
                                            c_public_label,
                                            c_control_mode,
                                        ),
                                        min_interval_s=60.0,
                                    )
                                elif zero_budget_action == "HOLD_MULTI_ZERO":
                                    hold_amp = int(max(6, start_stop_decision.get("hold_amp", 0)))
                                    c_data["current_set_amp"] = hold_amp
                                    c_data["is_charging"] = False
                                    if not c_data.get("last_start_ts"):
                                        c_data["last_start_ts"] = now_ts
                                    c_data["_wb_stop_sent_active"] = False
                                    logger.debug(
                                        "WB%d Multi-Connect Haltelogik: halte %dA statt Stop "
                                        "(Modus=%s, Regelpfad=%d, Grid=%dW)" %
                                        (c_id, hold_amp, c_public_label, c_control_mode, int(grid_power_raw))
                                    )
                                elif zero_budget_action == "HOLD_OPENWB_ZERO":
                                    hold_amp = int(max(6, start_stop_decision.get("target_amp", 6)))
                                    last_hold_amp = int(c_data.get("_last_openwb_hold_amp", 0) or 0)
                                    hold_interval_s = 5.0 if openwb_pro else 20.0
                                    hold_command_sent = False
                                    if (
                                        (
                                            last_hold_amp != hold_amp
                                            or now_ts - float(c_data.get("_last_openwb_min_hold_ts", 0.0) or 0.0) >= hold_interval_s
                                        )
                                        and now_ts - float(c_data.get("_last_openwb_min_hold_ts", 0.0) or 0.0) >= hold_interval_s
                                    ):
                                        _execute_wallbox_driver_command(
                                            c_data,
                                            {
                                                "method": "set_amp_and_state",
                                                "amp": hold_amp,
                                                "force_state": None,
                                                "reason": "openwb_zero_budget_hold",
                                            },
                                            c_id=c_id,
                                        )
                                        c_data["_last_openwb_hold_amp"] = hold_amp
                                        c_data["_last_openwb_min_hold_ts"] = now_ts
                                        last_change_ts[c_id] = now_ts
                                        made_changes = True
                                        hold_command_sent = True
                                    if openwb_pro and (hold_command_sent or not c_data.get("last_start_ts")):
                                        _mark_openwb_pro_start_offer(
                                            c_data,
                                            hold_amp,
                                            now_ts=now_ts,
                                            config=config,
                                            charger_max_amp=charger_max_amp,
                                        )
                                    c_data["current_set_amp"] = hold_amp
                                    c_data["is_charging"] = True
                                    c_data["_wb_stop_sent_active"] = False
                                    _log_state_once(
                                        c_data,
                                        "openwb_zero_budget_hold",
                                        (hold_amp, c_public_label, c_control_mode),
                                        "WB%d openWB Haltelogik: halte %dA Mindeststrom statt Stop "
                                        "(Modus=%s, Regelpfad=%d, Grid=%dW)" %
                                            (c_id, hold_amp, c_public_label, c_control_mode, int(grid_power_raw)),
                                    )
                                elif zero_budget_action in ("HOLD_NATIVE_RUNNING_CHARGE", "HOLD_NATIVE_CURRENT_DOWN"):
                                    if zero_budget_action == "HOLD_NATIVE_CURRENT_DOWN":
                                        hold_amp = int(max(6, start_stop_decision.get("hold_amp", 0)))
                                        if (
                                            floor_pv_only_guard_for_wb
                                            and controlled_floor_battery_guard_active
                                        ):
                                            hold_amp = 6
                                    else:
                                        hold_amp = int(max(
                                            6,
                                            start_stop_decision.get("hold_amp", 0),
                                            c_data.get("current_set_amp", 0) or 0,
                                        ))
                                    if zero_budget_action == "HOLD_NATIVE_CURRENT_DOWN":
                                        _execute_wallbox_driver_command(
                                            c_data,
                                            {
                                                "method": "set_amp_sonnenmodus",
                                                "amp": hold_amp,
                                                "force_state": None,
                                                "reason": "native_current_down_hold",
                                            },
                                            c_id=c_id,
                                        )
                                        last_change_ts[c_id] = now_ts
                                        made_changes = True
                                    c_data["current_set_amp"] = hold_amp
                                    c_data["is_charging"] = True
                                    c_data["_wb_stop_sent_active"] = False
                                    c_data["_native_multi_start_grace_until"] = 0.0
                                    _native_hold_key = (
                                        "native_current_down_hold"
                                        if zero_budget_action == "HOLD_NATIVE_CURRENT_DOWN"
                                        else "native_running_charge_hold"
                                    )
                                    _native_hold_msg = (
                                        "WB%d %s: E3DC-native erst auf %dA abregeln, kein Stop "
                                        "bei laufender Ladung (Grid=%dW, Regelpfad=%d)"
                                        if zero_budget_action == "HOLD_NATIVE_CURRENT_DOWN"
                                        else
                                        "WB%d %s: echte native Ladung %.0fW gehalten, kein Stop bei 0W-Momentanbudget "
                                        "(Grid=%dW, Regelpfad=%d)"
                                    )
                                    _native_hold_args = (
                                        (c_id, c_public_label, hold_amp, int(grid_power_raw), c_control_mode)
                                        if zero_budget_action == "HOLD_NATIVE_CURRENT_DOWN"
                                        else
                                        (c_id, c_public_label, hw_power_w, int(grid_power_raw), c_control_mode)
                                    )
                                    _log_state_once(
                                        c_data,
                                        _native_hold_key,
                                        (hold_amp, c_public_label, c_control_mode),
                                        _native_hold_msg % _native_hold_args,
                                        min_interval_s=60.0,
                                    )
                                elif zero_budget_action == "HOLD_CONTROLLABLE_EXPORT_CLOUD":
                                    hold_amp = int(max(6, start_stop_decision.get("hold_amp", 0)))
                                    c_data["current_set_amp"] = hold_amp
                                    c_data["is_charging"] = bool(
                                        hw_charging
                                        or hw_power_w > 500.0
                                        or hold_amp > 0
                                    )
                                    c_data["_wb_stop_sent_active"] = False
                                    _log_state_once(
                                        c_data,
                                        "controllable_export_cloud_hold",
                                        (hold_amp, c_public_label, c_control_mode),
                                        "WB%d %s: halte %dA statt Stop "
                                        "(Export/Wolkenhaltezeit, Grid=%dW, Regelpfad=%d)" %
                                        (c_id, c_public_label, hold_amp, int(grid_power_raw), c_control_mode),
                                        min_interval_s=60.0,
                                    )
                                elif zero_budget_action == "HOLD_GRID_WINDOW":
                                    hold_amp = int(max(6, start_stop_decision.get("hold_amp", 0)))
                                    c_data["current_set_amp"] = hold_amp
                                    c_data["is_charging"] = bool(
                                        hw_charging
                                        or hw_power_w > 500.0
                                        or hold_amp > 0
                                    )
                                    c_data["_wb_stop_sent_active"] = False
                                    c_data["_openwb_zero_budget_since"] = 0.0
                                    _log_state_once(
                                        c_data,
                                        "wb_grid_window_zero_budget_hold",
                                        (hold_amp, c_public_label, c_control_mode),
                                        "WB%d %s: Ladefenster haelt %dA, kein Stop bei kurzem 0W-Budget" %
                                        (c_id, c_public_label, hold_amp),
                                        min_interval_s=60.0,
                                    )
                                elif zero_budget_action == "HOLD_NATIVE_NO_STOP_WAIT":
                                    # Mode 9/10 ist bei E3DC-native ein Start-
                                    # und Freigabezustand. Ohne echte Phasen-
                                    # leistung ist ein Stop/Abort-Toggle kein
                                    # Messwertabgleich, sondern kann die WB
                                    # wieder genau aus dem Start herauswerfen.
                                    hold_amp = 6
                                    c_data["current_set_amp"] = hold_amp
                                    c_data["is_charging"] = False
                                    c_data["_wb_stop_sent_active"] = False
                                    c_data["_native_multi_start_grace_until"] = max(
                                        float(c_data.get("_native_multi_start_grace_until", 0.0) or 0.0),
                                        now_ts + 180.0,
                                    )
                                    logger.debug(
                                        "WB%d %s: Stop/Abort unterdrueckt, "
                                        "warte auf echte Ladeleistung (%dA gehalten)" %
                                        (c_id, c_public_label, hold_amp)
                                    )
                                elif zero_budget_action == "HOLD_NATIVE_START_GRACE":
                                    c_data["is_charging"] = True
                                    c_data["_wb_stop_sent_active"] = False
                                    logger.debug(
                                        "WB%d E3DC-Start-Gnadenzeit: Stop unterdrueckt "
                                        "(ALG noch nicht auf Laden, Modus=%s, Regelpfad=%d)" %
                                        (c_id, c_public_label, c_control_mode)
                                    )
                                elif zero_budget_action == "HOLD_NATIVE_START_CAP":
                                    # Kein Stop-Toggle gegen einen reinen Anlaufdeckel.
                                    # Ohne echte Phasen-/Leistungsmessung ist das kein
                                    # aktiver Ladevorgang, aber auch kein Grund fuer
                                    # wiederholtes Start/Stop.
                                    c_data["is_charging"] = False
                                    c_data["_wb_stop_sent_active"] = False
                                    logger.debug(
                                        "WB%d %s: Anlaufdeckel gehalten, "
                                        "kein Stop ohne echte Ladeleistung" %
                                        (c_id, c_public_label)
                                    )
                                elif zero_budget_action == "STOP":
                                    _send_wallbox_stop_command(
                                        c_data,
                                        c_id=c_id,
                                        reason="zero_budget_stop",
                                    )
                                    c_data["current_set_amp"] = 0
                                    if openwb_like_charger:
                                        c_data["_last_openwb_hold_amp"] = 0
                                    if openwb_pro:
                                        c_data["_openwb_pro_start_hold_until"] = 0.0
                                        c_data["_openwb_pro_start_hold_amp"] = 0
                                    c_data["is_charging"] = False
                                    c_data["_native_multi_start_grace_until"] = 0.0
                                    c_data["_pv_mode_active"] = False  # Mode 9 Flag reset
                                    c_data["_wb_stop_sent_active"] = True
                                    c_data["_last_stop_toggle_ts"] = now_ts
                                    if e3dc_native_toggle:
                                        c_data["abort_cooldown_ts"] = now_ts
                                    last_change_ts[c_id] = now_ts
                                    if native_sun_shadow and not hw_charging and hw_power_w <= 500:
                                        logger.info("WB%d Fuzzy=0: Multi-Connect Sonnenmodus-Schatten geloescht (Modus=%s, Regelpfad=%d)" % (c_id, c_public_label, c_control_mode))
                                    else:
                                        logger.info("WB%d Fuzzy=0: Stop/Abort gesetzt (Modus=%s, Regelpfad=%d)" % (c_id, c_public_label, c_control_mode))
                                    made_changes = True
                                elif zero_budget_action == "SUPPRESS_NATIVE_STOP":
                                    c_data["current_set_amp"] = 0
                                    c_data["is_charging"] = False
                                    c_data["_wb_stop_sent_active"] = True
                                    logger.debug(
                                        "WB%d Fuzzy=0: E3DC-Stop-Toggle unterdrueckt "
                                        "(keine echte Ladeleistung, Modus=%s, Regelpfad=%d)" %
                                        (c_id, c_public_label, c_control_mode)
                                    )

                            elif (
                                cap_amp > 0
                                and abs(float(cap_amp or 0.0) - float(current_amp or 0.0)) + 1e-6
                                >= _current_step_amp_for_charger(c_data.get("charger"), default=1.0)
                            ):
                                c_data["_native_multi_zero_budget_since"] = 0.0
                                c_data["_openwb_zero_budget_since"] = 0.0
                                if (
                                    charger_connected
                                    and not hw_charging
                                    and stable_hw_power_w <= 500.0
                                    and int(current_amp or 0) > 0
                                ):
                                    # Ein angebotener Strom ist noch keine
                                    # bestaetigte Ladung. Bis echte Leistung
                                    # messbar ist, bleibt die Freigabe bei
                                    # Mindeststrom; sonst sieht ein volles
                                    # Fahrzeug wie eine hochregelbare Last aus.
                                    cap_amp = min(int(cap_amp or 0), 6)
                                # Cooldown nach physischem Abbruch: 60s warten bevor Neustart
                                abort_cooldown = c_data.get('abort_cooldown_ts', 0)
                                if time.time() - abort_cooldown < 60:
                                    pass  # Cooldown aktiv: kein Neustart
                                # Nur hochregeln wenn kein Fast-Correction lief (vermeide Saege)
                                elif fast_correction_done and cap_amp > current_amp:
                                    pass  # Fast hat gerade runtergeregelt, nicht sofort wieder hoch
                                elif cap_amp > current_amp and time.time() < c_data.get('fast_block_until', 0):
                                    pass  # Nach Netzbezug-Korrektur erst beruhigen lassen
                                else:
                                    charger = c_data["charger"]
                                    is_new_start = (current_amp == 0)
                                    if openwb_pro and charger_connected and not hw_charging and is_new_start:
                                        _pro_start_hold_s = max(
                                            60.0,
                                            _sf(config.get("openwb_pro_start_hold_s", 180), 180.0)
                                        )
                                        cap_amp = max(6, min(int(charger_max_amp), int(cap_amp or 6)))
                                        c_data["_openwb_pro_start_hold_until"] = now_ts + _pro_start_hold_s
                                        c_data["_openwb_pro_start_hold_amp"] = cap_amp
                                    fs = 2 if is_new_start else None
                                    e3dc_native_toggle = (
                                        hasattr(charger, "set_amp_sonnenmodus")
                                        and not hasattr(charger, "set_pv_mode")
                                    )
                                    if (
                                        e3dc_native_toggle
                                        and is_new_start
                                        and time.time() - c_data.get('abort_cooldown_ts', 0) < 90
                                    ):
                                        continue

                                    # Mode 4/9/10/11 E3DC-nativ: iAvalPower bestimmt 1A-Schritt (Eba-Stil)
                                    # iFreeW = iAvalPower + wbMinimumPower/6
                                    # > 100W: 1A hoch | < -(min/6)-100: 1A runter
                                    if effective_wb_mode in (4, 9, 10, 11) and hasattr(charger, "set_amp_sonnenmodus"):
                                        _iAval = c_data.get('iAvalPower', 0.0)
                                        _wb_min_w = 6 * 230.0 * detected_phases
                                        i_free_w = _iAval + _wb_min_w / 6.0
                                        _last_ramp = c_data.get('last_ramp_ts', 0.0)
                                        _ramp_now = time.time()
                                        _native_waiting_for_real_charge = bool(
                                            current_amp > 0
                                            and not hw_charging
                                            and not bool((charger_status or {}).get("phase_power_verified", False))
                                            and _ramp_now < c_data.get("_native_multi_start_grace_until", 0.0)
                                        )
                                        _native_ramp_interval_s = max(15.0, float(current_change_hold_s or 0.0))
                                        if _native_waiting_for_real_charge:
                                            # E3DC-native WB: erst bei echter
                                            # ALG-/Phasenbestaetigung ueber 6A
                                            # hinaus. Sollstrom allein ist kein
                                            # Messwert und darf nicht hochrampen.
                                            new_amp = current_amp
                                        elif _ramp_now - _last_ramp >= _native_ramp_interval_s or is_new_start:
                                            if is_new_start:
                                                new_amp = 6  # Eba: Start immer bei 6A (wbminladestrom)
                                            elif (
                                                bool(int(_sf(config.get("wb_native_grid_confirmed_direct_target", 1), 1.0)))
                                                and (local_price_optimizing_active or local_grid_allowed)
                                                and hw_charging
                                                and stable_hw_power_w > 500.0
                                                and cap_amp > current_amp
                                            ):
                                                new_amp = int(cap_amp)
                                            elif current_amp > cap_amp:
                                                _native_down_step_a = 1
                                                if (
                                                    floor_pv_only_guard_for_wb
                                                    and controlled_floor_battery_guard_active
                                                ):
                                                    _native_down_step_a = max(
                                                        1,
                                                        int(_sf(config.get("wb_target_floor_pv_only_down_step_a", 2), 2.0)),
                                                    )
                                                new_amp = max(int(cap_amp), int(current_amp) - _native_down_step_a)
                                            elif i_free_w > 100 and current_amp < cap_amp:
                                                new_amp = current_amp + 1  # Eba: 1A hoch
                                            elif i_free_w < -(_wb_min_w / 6.0) - 100 and current_amp > 6:
                                                new_amp = current_amp - 1  # Eba: 1A runter
                                            else:
                                                new_amp = current_amp  # Stabil
                                            c_data['last_ramp_ts'] = _ramp_now
                                        else:
                                            new_amp = current_amp  # Ramp-Timer laeuft noch
                                        if new_amp != current_amp or is_new_start:
                                            # Mode 9: PV+Speicher. Mode=1 (Sonnenmodus) ist korrekt:
                                            # E3DC nutzt intern PV+Bat, regelt am Netzuebergabepunkt.
                                            # Python setzt nur den Ampere-Deckel.
                                            if local_price_optimizing_active or local_grid_allowed:
                                                _execute_wallbox_driver_command(
                                                    c_data,
                                                    {
                                                        "method": "take_control",
                                                        "reason": "native_grid_take_control",
                                                    },
                                                    c_id=c_id,
                                                )
                                                _clear_stale_e3dc_stop_latch_for_grid_start(
                                                    c_data,
                                                    force_state=fs,
                                                    grid_allowed=local_grid_allowed,
                                                    price_active=local_price_optimizing_active,
                                                    hw_charging=hw_charging,
                                                    hw_power_w=stable_hw_power_w,
                                                )
                                                _evaluate_e3dc_session_for_manager(
                                                    c_data,
                                                    charger_status,
                                                    cap_amp=new_amp,
                                                    budget_ready=bool((_physical_budget or {}).get("budget_ready", False)),
                                                    switch_to_1p_ready=bool((_physical_budget or {}).get("switch_to_1p_ready", False)),
                                                    grid_allowed=local_grid_allowed,
                                                    price_active=local_price_optimizing_active,
                                                    price_boost_active=price_boost_wallbox_active,
                                                    predump_active=predump_wallbox_active,
                                                    mode_off=(c_public_mode == MODE_OFF),
                                                    priority_forced_stop=priority_forced_stop,
                                                    min_amp=wb_min_amp_cfg,
                                                    now_ts=now_ts,
                                                    start_verify_s=max(
                                                        30.0,
                                                        _sf(config.get("e3dc_native_start_verify_s", 45), 45.0),
                                                    ),
                                                )
                                                _execute_wallbox_driver_command(
                                                    c_data,
                                                    {
                                                        "method": "set_amp_and_state",
                                                        "amp": new_amp,
                                                        "force_state": fs,
                                                        "reason": "native_grid_current",
                                                    },
                                                    c_id=c_id,
                                                )
                                            else:
                                                # Mode 4/9/10/11 + E3DC-native WB: Python setzt nur den
                                                # Ampere-Deckel im E3DC-Sonnenmodus. Das ist bewusst
                                                # nicht Mode 0/Funkstille und entspricht der C++-Rolle.
                                                _native_sun_budget_ready = bool(
                                                    ((_physical_budget or {}).get("budget_ready", False))
                                                    or ((_physical_budget or {}).get("can_start_or_hold", False))
                                                )
                                                _clear_stale_e3dc_stop_latch_for_grid_start(
                                                    c_data,
                                                    force_state=fs,
                                                    pv_start_active=_native_sun_budget_ready,
                                                    hw_charging=hw_charging,
                                                    hw_power_w=stable_hw_power_w,
                                                )
                                                _evaluate_e3dc_session_for_manager(
                                                    c_data,
                                                    charger_status,
                                                    cap_amp=new_amp,
                                                    budget_ready=_native_sun_budget_ready,
                                                    switch_to_1p_ready=bool((_physical_budget or {}).get("switch_to_1p_ready", False)),
                                                    grid_allowed=False,
                                                    price_active=False,
                                                    price_boost_active=price_boost_wallbox_active,
                                                    predump_active=predump_wallbox_active,
                                                    mode_off=(c_public_mode == MODE_OFF),
                                                    priority_forced_stop=priority_forced_stop,
                                                    min_amp=wb_min_amp_cfg,
                                                    now_ts=now_ts,
                                                    start_verify_s=max(
                                                        30.0,
                                                        _sf(config.get("e3dc_native_start_verify_s", 45), 45.0),
                                                    ),
                                                )
                                                _execute_wallbox_driver_command(
                                                    c_data,
                                                    {
                                                        "method": "set_amp_sonnenmodus",
                                                        "amp": new_amp,
                                                        "force_state": fs,
                                                        "reason": "native_sun_current",
                                                    },
                                                    c_id=c_id,
                                                )
                                        cap_amp = new_amp  # Fuer Logging unten

                                    # Standardpfad: openWB / go-e / PV-Modi 1-8
                                    elif (
                                        c_control_mode == 9
                                        and openwb_pv_capable
                                        and not (wbminsoc_gate_open and wb_storage_cap_w > 0)
                                    ):
                                        # Mode 9 + normale openWB: keine Lademodi senden.
                                        # openWB bleibt Master; E3DC-Control haelt nur den
                                        # Secondary-Pfad wach.
                                        if is_new_start or not c_data.get("_pv_mode_active", False):
                                            _execute_wallbox_driver_command(
                                                c_data,
                                                {
                                                    "method": "set_pv_mode",
                                                    "reason": "openwb_secondary_pv_heartbeat",
                                                },
                                                c_id=c_id,
                                            )
                                            c_data["_pv_mode_active"] = True
                                            logger.info("WB%d %s: openWB Secondary-Heartbeat aktiv (regelt autonom am Netzpunkt)" % (c_id, c_public_label))
                                        cap_amp = cap_amp  # unveraendert fuer Logging
                                    else:
                                        if c_control_mode == 4 and not (local_price_optimizing_active or local_grid_allowed):
                                            _target_cap_amp = int(cap_amp)
                                            _iAval = float(c_data.get('iAvalPower', 0.0) or 0.0)
                                            _wb_min_w = 6 * 230.0 * detected_phases
                                            i_free_w = _iAval + _wb_min_w / 6.0
                                            _last_ramp = float(c_data.get('last_ramp_ts', 0.0) or 0.0)
                                            _ramp_now = time.time()
                                            _mode4_ramp_interval_s = max(15.0, float(current_change_hold_s or 0.0))
                                            if is_new_start:
                                                cap_amp = min(_target_cap_amp, 6)
                                                c_data['last_ramp_ts'] = _ramp_now
                                            elif _ramp_now - _last_ramp >= _mode4_ramp_interval_s:
                                                if i_free_w > 100 and current_amp < _target_cap_amp:
                                                    cap_amp = min(_target_cap_amp, current_amp + 1)
                                                elif i_free_w < -(_wb_min_w / 6.0) - 100 and current_amp > 6:
                                                    cap_amp = max(6, current_amp - 1)
                                                else:
                                                    cap_amp = current_amp
                                                c_data['last_ramp_ts'] = _ramp_now
                                            else:
                                                cap_amp = current_amp

                                        if (
                                            hasattr(charger, "set_amp_and_state")
                                            and not hasattr(charger, "set_amp_sonnenmodus")
                                            and c_data.get("_pv_mode_active", False)
                                            and (
                                                effective_wb_mode != 9
                                                and c_control_mode != 9
                                                or wb_storage_cap_w > 0
                                                or local_grid_allowed
                                                or local_price_optimizing_active
                                            )
                                        ):
                                            # openWB muss beim Wechsel aus PV/Scheduler in
                                            # Sofort-/Speicher-gefuehrte Modi explizit auf
                                            # Direkt/Sofort gestellt werden, sonst bleibt in
                                            # der openWB-Oberflaeche "PV" stehen.
                                            fs = 2

                                        if hasattr(charger, "set_amp_sonnenmodus"):
                                            if local_price_optimizing_active or local_grid_allowed:
                                                _execute_wallbox_driver_command(
                                                    c_data,
                                                    {
                                                        "method": "take_control",
                                                        "reason": "native_grid_take_control",
                                                    },
                                                    c_id=c_id,
                                                )
                                                _clear_stale_e3dc_stop_latch_for_grid_start(
                                                    c_data,
                                                    force_state=fs,
                                                    grid_allowed=local_grid_allowed,
                                                    price_active=local_price_optimizing_active,
                                                    hw_charging=hw_charging,
                                                    hw_power_w=stable_hw_power_w,
                                                )
                                                _evaluate_e3dc_session_for_manager(
                                                    c_data,
                                                    charger_status,
                                                    cap_amp=cap_amp,
                                                    budget_ready=bool((_physical_budget or {}).get("budget_ready", False)),
                                                    switch_to_1p_ready=bool((_physical_budget or {}).get("switch_to_1p_ready", False)),
                                                    grid_allowed=local_grid_allowed,
                                                    price_active=local_price_optimizing_active,
                                                    price_boost_active=price_boost_wallbox_active,
                                                    predump_active=predump_wallbox_active,
                                                    mode_off=(c_public_mode == MODE_OFF),
                                                    priority_forced_stop=priority_forced_stop,
                                                    min_amp=wb_min_amp_cfg,
                                                    now_ts=now_ts,
                                                    start_verify_s=max(
                                                        30.0,
                                                        _sf(config.get("e3dc_native_start_verify_s", 45), 45.0),
                                                    ),
                                                )
                                                _execute_wallbox_driver_command(
                                                    c_data,
                                                    {
                                                        "method": "set_amp_and_state",
                                                        "amp": cap_amp,
                                                        "force_state": fs,
                                                        "reason": "native_grid_current",
                                                    },
                                                    c_id=c_id,
                                                )
                                            else:
                                                _native_sun_budget_ready = bool(
                                                    ((_physical_budget or {}).get("budget_ready", False))
                                                    or ((_physical_budget or {}).get("can_start_or_hold", False))
                                                )
                                                _clear_stale_e3dc_stop_latch_for_grid_start(
                                                    c_data,
                                                    force_state=fs,
                                                    pv_start_active=_native_sun_budget_ready,
                                                    hw_charging=hw_charging,
                                                    hw_power_w=stable_hw_power_w,
                                                )
                                                _evaluate_e3dc_session_for_manager(
                                                    c_data,
                                                    charger_status,
                                                    cap_amp=cap_amp,
                                                    budget_ready=_native_sun_budget_ready,
                                                    switch_to_1p_ready=bool((_physical_budget or {}).get("switch_to_1p_ready", False)),
                                                    grid_allowed=False,
                                                    price_active=False,
                                                    price_boost_active=price_boost_wallbox_active,
                                                    predump_active=predump_wallbox_active,
                                                    mode_off=(c_public_mode == MODE_OFF),
                                                    priority_forced_stop=priority_forced_stop,
                                                    min_amp=wb_min_amp_cfg,
                                                    now_ts=now_ts,
                                                    start_verify_s=max(
                                                        30.0,
                                                        _sf(config.get("e3dc_native_start_verify_s", 45), 45.0),
                                                    ),
                                                )
                                                _execute_wallbox_driver_command(
                                                    c_data,
                                                    {
                                                        "method": "set_amp_sonnenmodus",
                                                        "amp": cap_amp,
                                                        "force_state": fs,
                                                        "reason": "native_sun_current",
                                                    },
                                                    c_id=c_id,
                                                )
                                        elif hasattr(charger, "set_direct_current") and storage_charge_reserve_w > 0:
                                            _execute_wallbox_driver_command(
                                                c_data,
                                                {
                                                    "method": "set_direct_current",
                                                    "amp": cap_amp,
                                                    "reason": "storage_charge_reserve_current",
                                                },
                                                c_id=c_id,
                                            )
                                        elif hasattr(charger, "set_amp_and_state"):
                                            _execute_wallbox_driver_command(
                                                c_data,
                                                {
                                                    "method": "set_amp_and_state",
                                                    "amp": cap_amp,
                                                    "force_state": fs,
                                                    "reason": "set_current",
                                                },
                                                c_id=c_id,
                                            )
                                        elif hasattr(charger, "release_to_e3dc"):
                                            _execute_wallbox_driver_command(
                                                c_data,
                                                {
                                                    "method": "release_to_e3dc",
                                                    "max_amp": cap_amp,
                                                    "reason": "release_to_e3dc_current",
                                                },
                                                c_id=c_id,
                                            )
                                    if (
                                        effective_wb_mode in (9, 10, 11)
                                        and wb_storage_cap_w > 0
                                        and openwb_like_charger
                                    ):
                                        # Storage-gefuehrte Fremdwallbox: Python
                                        # setzt den Ampere-Deckel, der E3DC bleibt
                                        # im AUTO. Das ist C++-nah: iAvalPower
                                        # wird zu WB-Ampere, nicht zu Speicher-DISCH.
                                        c_data["_pv_mode_active"] = False
                                        c_data["_wb_floor_stop_active"] = False
                                    logger.info("WB%d %s: %sA -> %sA (delta=%.1f%% fz=%.2f budget=%dW)" % (
                                        c_id,
                                        "START" if is_new_start else "Deckel",
                                        _amp_text(current_amp), _amp_text(cap_amp), fuzzy_delta, fz, int(free_for_limbs_w)))
                                    if cap_amp > current_amp:
                                        c_data['last_storage_guided_amp_up_ts'] = time.time()
                                    elif cap_amp < current_amp:
                                        c_data['last_storage_guided_amp_down_ts'] = time.time()
                                    c_data["current_set_amp"] = cap_amp
                                    if openwb_like_charger:
                                        c_data["_last_openwb_hold_amp"] = cap_amp
                                    c_data["is_charging"] = True
                                    c_data["_predump_gate_stop_sent"] = False
                                    c_data["_wb_stop_sent_active"] = False
                                    # Nicht hier zuruecksetzen: Bei E3DC/Multi Connect
                                    # ist der Start nur ein Toggle. Erst echte
                                    # Rueckmeldung/Leistung beweist eine stabile Ladung.
                                    if is_new_start:
                                        c_data["last_start_ts"] = now_ts
                                        if e3dc_native_toggle:
                                            c_data["_native_multi_start_grace_until"] = now_ts + 180.0
                                    elif not c_data.get("last_start_ts"):
                                        c_data["last_start_ts"] = now_ts
                                    last_change_ts[c_id] = now_ts
                                    made_changes = True

                        if (
                            openwb_pro
                            and charger_connected
                            and not c_data.get("_wb_stop_sent_active", False)
                            and not c_data.get("_bev_full_blocked", False)
                        ):
                            keepalive_amp = int(c_data.get("current_set_amp", 0) or 0)
                            if keepalive_amp >= 6:
                                offered_amp = float((charger_status or {}).get(
                                    "offered_current_raw",
                                    (charger_status or {}).get("amp", 0)
                                ) or 0)
                                _keepalive_phases = max(
                                    1,
                                    int(
                                        current_phases
                                        or phase_switch_phases
                                        or phase_cap_phases
                                        or detected_phases
                                        or 1
                                    ),
                                )
                                _keepalive_expected_w = float(keepalive_amp) * 230.0 * float(_keepalive_phases)
                                _openwb_over_target = bool(
                                    offered_amp > keepalive_amp + 0.5
                                    or (
                                        hw_charging
                                        and stable_hw_power_w > _keepalive_expected_w + 900.0
                                        and not (local_grid_allowed or local_price_optimizing_active or price_boost_wallbox_active)
                                    )
                                )
                                if (
                                    (not hw_charging)
                                    or offered_amp < keepalive_amp - 0.5
                                    or _openwb_over_target
                                ):
                                    if _execute_wallbox_driver_command(
                                        c_data,
                                        {
                                            "method": "set_amp_and_state",
                                            "amp": keepalive_amp,
                                            "force_state": None,
                                            "reason": "openwb_pro_keepalive",
                                        },
                                        c_id=c_id,
                                    ):
                                        c_data["current_set_amp"] = keepalive_amp
                                        c_data["_last_openwb_hold_amp"] = keepalive_amp
                                        logger.debug(
                                            "WB%d openWB Pro Keepalive: %dA (offered %.1fA, charging=%s)" % (
                                                c_id, keepalive_amp, offered_amp, bool(hw_charging)
                                            )
                                        )

                        openwb_retry_charger = bool(
                            openwb_like_charger
                            and hasattr(c_data["charger"], "set_amp_and_state")
                            and not hasattr(c_data["charger"], "set_amp_sonnenmodus")
                        )
                        openwb_retry_amp = int(max(
                            0,
                            min(
                                int(charger_max_amp),
                                int(
                                    c_data.get("current_set_amp", 0)
                                    or cap_amp
                                    or (charger_status or {}).get("amp", 0)
                                    or 0
                                ),
                            ),
                        ))
                        if openwb_pro and not hw_charging and openwb_retry_amp >= 6:
                            if stable_hw_power_w <= 500.0:
                                openwb_retry_amp = min(openwb_retry_amp, 6)
                            _retry_count = int(c_data.get("_openwb_start_retry_count", 0) or 0)
                            if _retry_count >= 1 and stable_hw_power_w > 500.0:
                                _retry_phases = int(
                                    _physical_budget.get("phases")
                                    or phase_cap_phases
                                    or phase_target
                                    or detected_phases
                                    or 1
                                )
                                _retry_phases = max(1, min(3, _retry_phases))
                                _budget_retry_amp = int(float(c_allowed_w or 0.0) / (230.0 * _retry_phases))
                                if _budget_retry_amp >= 6:
                                    openwb_retry_amp = max(
                                        openwb_retry_amp,
                                        min(
                                            int(charger_max_amp),
                                            _budget_retry_amp,
                                            openwb_retry_amp + 5,
                                        ),
                                    )
                        openwb_retry_s = max(
                            30.0,
                            _sf(config.get("wb_openwb_start_retry_s", 45), 45.0),
                        )
                        openwb_cp_retry_max = max(
                            0,
                            min(
                                1,
                                int(_sf(config.get("wb_openwb_start_cp_retries", 1), 1.0)),
                            ),
                        )
                        openwb_start_budget_ready = bool(
                            cap_amp > 0
                            and (
                                grid_power_raw < -800.0
                                or local_grid_allowed
                                or local_price_optimizing_active
                                or price_boost_wallbox_active
                                or bool(_physical_budget.get("can_start_or_hold", False))
                            )
                        )
                        openwb_start_reject_soft_until = float(
                            c_data.get("_openwb_start_reject_soft_until", 0.0) or 0.0
                        )
                        openwb_start_reject_retry_due = bool(
                            openwb_start_reject_soft_until > 0.0
                            and now_ts >= openwb_start_reject_soft_until
                            and str(c_data.get("_bev_full_block_reason") or "") == "start_rejected_soft"
                        )
                        if openwb_start_reject_retry_due:
                            c_data["_wb_stop_sent_active"] = False
                        openwb_start_waiting = bool(
                            openwb_retry_charger
                            and charger_connected
                            and c_control_mode > 0
                            and openwb_start_budget_ready
                            and openwb_retry_amp >= 6
                            and not priority_forced_stop
                            and not c_data.get("_bev_full_blocked", False)
                            and (not c_data.get("_wb_stop_sent_active", False) or openwb_start_reject_retry_due)
                            and not hw_charging
                            and stable_hw_power_w <= 500.0
                            and now_ts - float(c_data.get("_last_manager_stop_request_ts", 0.0) or 0.0) >= openwb_retry_s
                            and now_ts - float(c_data.get("last_start_ts", 0.0) or 0.0) >= openwb_retry_s
                            and now_ts - float(c_data.get("_openwb_last_start_retry_ts", 0.0) or 0.0) >= openwb_retry_s
                        )
                        if openwb_start_waiting:
                            _cp_already_sent = bool(c_data.get("_openwb_cp_start_sent", False))
                            sent_ok = _execute_wallbox_driver_command(
                                c_data,
                                {
                                    "method": "set_amp_and_state",
                                    "amp": openwb_retry_amp,
                                    "force_state": 2,
                                    "reason": "openwb_start_retry",
                                },
                                c_id=c_id,
                            )
                            cp_triggered = False
                            if openwb_pro and hasattr(c_data["charger"], "trigger_cp_interrupt"):
                                _cp_already_sent = bool(c_data.get("_openwb_cp_start_sent", False))
                                cp_active = bool((charger_status or {}).get("cp_interrupt_isactive", 0))
                                phase_pause_active = bool(
                                    now_ts - float(getattr(c_data["charger"], "_last_phase_ts", 0.0) or 0.0)
                                    < max(15.0, _sf(config.get("openwb_pro_phase_wakeup_hold_s", 45), 45.0))
                                )
                                last_manager_cp_ts = float(c_data.get("_openwb_last_cp_start_ts", 0.0) or 0.0)
                                if (
                                    not _cp_already_sent
                                    and not cp_active
                                    and not phase_pause_active
                                    and openwb_cp_retry_max > 0
                                    and now_ts - last_manager_cp_ts >= 30.0
                                ):
                                    cp_triggered = _execute_wallbox_driver_command(
                                        c_data,
                                        {
                                            "method": "trigger_cp_interrupt",
                                            "reason": "openwb_start_retry_cp",
                                        },
                                        c_id=c_id,
                                    )
                                    if cp_triggered:
                                        c_data["_openwb_cp_start_sent"] = True
                                        c_data["_openwb_last_cp_start_ts"] = now_ts
                            if sent_ok or cp_triggered:
                                if sent_ok and openwb_retry_amp > 0:
                                    _mark_manager_charge_anchor(
                                        c_data,
                                        amp=openwb_retry_amp,
                                        reason="openwb_start_retry",
                                        reset_real_marker=not hw_charging,
                                    )
                                    if openwb_pro and not hw_charging:
                                        _mark_openwb_pro_start_offer(
                                            c_data,
                                            openwb_retry_amp,
                                            now_ts=now_ts,
                                            config=config,
                                            charger_max_amp=charger_max_amp,
                                            refresh=True,
                                        )
                                c_data["_openwb_start_retry_count"] = int(c_data.get("_openwb_start_retry_count", 0) or 0) + 1
                                c_data["_openwb_last_start_retry_ts"] = now_ts
                                c_data["current_set_amp"] = openwb_retry_amp
                                c_data["_last_openwb_hold_amp"] = openwb_retry_amp
                                if openwb_pro and openwb_retry_amp >= 6:
                                    c_data["_openwb_pro_start_hold_amp"] = max(
                                        int(c_data.get("_openwb_pro_start_hold_amp", 0) or 0),
                                        int(openwb_retry_amp),
                                    )
                                    c_data["_openwb_pro_start_hold_until"] = max(
                                        float(c_data.get("_openwb_pro_start_hold_until", 0.0) or 0.0),
                                        now_ts + max(60.0, _sf(config.get("openwb_pro_start_hold_s", 180), 180.0)),
                                    )
                                c_data["_wb_stop_sent_active"] = False
                                last_change_ts[c_id] = now_ts
                                made_changes = True
                                logger.info(
                                    "WB%d openWB-Startimpuls wiederholt: %dA bei %.0fW Export "
                                    "(Freigabe stand, aber 0W Ladeleistung, CP=%s)" % (
                                        c_id,
                                        openwb_retry_amp,
                                        abs(grid_power_raw),
                                        "ja" if cp_triggered else "nein",
                                    )
                                )

                        native_retry_charger = (
                            hasattr(c_data["charger"], "set_amp_sonnenmodus")
                            and not hasattr(c_data["charger"], "set_pv_mode")
                        )
                        native_retry_amp = int(max(
                            0,
                            min(
                                int(charger_max_amp),
                                int(max(
                                    float(c_data.get("current_set_amp", 0) or 0),
                                    float(cap_amp or 0),
                                    float((charger_status or {}).get("amp", 0) or 0),
                                )),
                            ),
                        ))
                        native_retry_s = max(45.0, _sf(config.get("e3dc_native_start_retry_s", 60), 60.0))
                        native_fresh_retry_s = max(
                            20.0,
                            min(
                                native_retry_s,
                                _sf(config.get("e3dc_native_fresh_start_retry_s", 30), 30.0),
                            ),
                        )
                        native_last_start_ts = float(c_data.get("last_start_ts", 0.0) or 0.0)
                        native_last_retry_ts = float(c_data.get("_native_last_start_retry_ts", 0.0) or 0.0)
                        native_fresh_start_retry_due = bool(
                            native_last_start_ts > 0.0
                            and now_ts - native_last_start_ts >= native_fresh_retry_s
                            and now_ts - native_last_start_ts < native_retry_s
                            and native_last_retry_ts < native_last_start_ts
                            and int(c_data.get("current_set_amp", 0) or 0) >= int(wb_min_amp_cfg or 6)
                        )
                        native_retry_soft_gap_s = max(
                            30.0,
                            _sf(config.get("wb_start_reject_cooldown_s", 60), 60.0),
                        )
                        native_start_reject_soft_until = float(
                            c_data.get("_openwb_start_reject_soft_until", 0.0) or 0.0
                        )
                        native_start_reject_retry_due = bool(
                            native_start_reject_soft_until > 0.0
                            and now_ts >= native_start_reject_soft_until
                            and str(c_data.get("_bev_full_block_reason") or "") == "start_rejected_soft"
                        )
                        native_start_budget_ready = bool(
                            cap_amp > 0
                            and (
                                bool((_physical_budget or {}).get("budget_ready", False))
                                or bool((_physical_budget or {}).get("switch_to_1p_ready", False))
                                or bool((_physical_budget or {}).get("can_start_or_hold", False))
                                or local_grid_allowed
                                or local_price_optimizing_active
                                or price_boost_wallbox_active
                                or predump_wallbox_active
                            )
                        )
                        native_grid_start_allowed = bool(
                            local_grid_allowed
                            or local_price_optimizing_active
                            or price_boost_wallbox_active
                            or bool(scheduled_slot_charger_ids)
                        )
                        native_stop_latch_retry_due = _native_e3dc_stop_latch_retry_due(
                            c_data,
                            now_ts=now_ts,
                            export_w=max(0.0, -float(grid_power_raw or 0.0)),
                            grid_start_allowed=native_grid_start_allowed,
                            cap_amp=native_retry_amp,
                            budget_ready=native_start_budget_ready,
                            charger_connected=charger_connected,
                            hw_charging=hw_charging,
                            hw_power_w=stable_hw_power_w,
                            priority_forced_stop=priority_forced_stop,
                            min_amp=wb_min_amp_cfg,
                            retry_s=native_retry_s,
                            soft_gap_s=native_retry_soft_gap_s,
                            strong_export_w=max(1800.0, 6.0 * 230.0),
                        )
                        if native_stop_latch_retry_due:
                            c_data["_wb_stop_sent_active"] = False
                            if not native_grid_start_allowed:
                                native_retry_amp = min(native_retry_amp, 6)
                        if native_start_reject_retry_due:
                            c_data["_wb_stop_sent_active"] = False
                        native_session = _evaluate_e3dc_session_for_manager(
                            c_data,
                            charger_status,
                            cap_amp=cap_amp,
                            budget_ready=bool(
                                ((_physical_budget or {}).get("budget_ready", False))
                                or ((_physical_budget or {}).get("can_start_or_hold", False))
                            ),
                            switch_to_1p_ready=bool((_physical_budget or {}).get("switch_to_1p_ready", False)),
                            grid_allowed=local_grid_allowed,
                            price_active=local_price_optimizing_active,
                            price_boost_active=price_boost_wallbox_active,
                            predump_active=predump_wallbox_active,
                            mode_off=(c_public_mode == MODE_OFF),
                            priority_forced_stop=priority_forced_stop,
                            min_amp=wb_min_amp_cfg,
                            now_ts=now_ts,
                            start_verify_s=max(
                                30.0,
                                _sf(config.get("e3dc_native_start_verify_s", 45), 45.0),
                            ),
                        )
                        native_retry_clock_ready = bool(
                            (
                                native_start_reject_retry_due
                                and now_ts - float(c_data.get("_native_last_start_retry_ts", 0.0) or 0.0)
                                >= native_retry_soft_gap_s
                            )
                            or native_stop_latch_retry_due
                            or native_fresh_start_retry_due
                            or (
                                now_ts - float(c_data.get("_last_manager_stop_request_ts", 0.0) or 0.0) >= native_retry_s
                                and now_ts - native_last_start_ts >= native_retry_s
                                and now_ts - native_last_retry_ts >= native_retry_s
                            )
                        )
                        native_start_waiting = bool(
                            native_retry_charger
                            and charger_connected
                            and c_control_mode > 0
                            and native_start_budget_ready
                            and native_retry_amp >= 6
                            and not priority_forced_stop
                            and not bool(native_session.get("start_blocked", False))
                            and not c_data.get("_bev_full_blocked", False)
                            and not hw_charging
                            and stable_hw_power_w <= 500.0
                            and (grid_power_raw < -800.0 or native_grid_start_allowed)
                            and native_retry_clock_ready
                        )
                        native_stop_retry_due = bool(
                            native_start_waiting
                            and c_data.get("_wb_stop_sent_active", False)
                            and (
                                native_start_reject_retry_due
                                or now_ts - float(c_data.get("_last_manager_stop_request_ts", 0.0) or 0.0) >= native_retry_s
                            )
                        )
                        if native_stop_retry_due:
                            c_data["_wb_stop_sent_active"] = False
                        native_start_waiting = bool(
                            native_start_waiting
                            and (
                                not c_data.get("_wb_stop_sent_active", False)
                                or native_stop_retry_due
                            )
                        )
                        if native_start_waiting:
                            native_retry_method = (
                                "set_amp_and_state" if native_grid_start_allowed else "set_amp_sonnenmodus"
                            )
                            native_retry_reason = (
                                "native_grid_start_retry" if native_grid_start_allowed else "native_start_retry"
                            )
                            if _execute_wallbox_driver_command(
                                c_data,
                                {
                                    "method": native_retry_method,
                                    "amp": native_retry_amp,
                                    "force_state": 2,
                                    "reason": native_retry_reason,
                                },
                                c_id=c_id,
                            ):
                                _mark_manager_charge_anchor(
                                    c_data,
                                    amp=native_retry_amp,
                                    reason="native_start_retry",
                                    reset_real_marker=not hw_charging,
                                )
                                c_data["_native_last_start_retry_ts"] = now_ts
                                c_data["_native_multi_start_grace_until"] = now_ts + 180.0
                                c_data["current_set_amp"] = native_retry_amp
                                _evaluate_e3dc_session_for_manager(
                                    c_data,
                                    charger_status,
                                    cap_amp=cap_amp,
                                    budget_ready=bool(
                                        ((_physical_budget or {}).get("budget_ready", False))
                                        or ((_physical_budget or {}).get("can_start_or_hold", False))
                                    ),
                                    switch_to_1p_ready=bool((_physical_budget or {}).get("switch_to_1p_ready", False)),
                                    grid_allowed=local_grid_allowed,
                                    price_active=local_price_optimizing_active,
                                    price_boost_active=price_boost_wallbox_active,
                                    predump_active=predump_wallbox_active,
                                    mode_off=(c_public_mode == MODE_OFF),
                                    priority_forced_stop=priority_forced_stop,
                                    min_amp=wb_min_amp_cfg,
                                    now_ts=now_ts,
                                    start_verify_s=max(
                                        30.0,
                                        _sf(config.get("e3dc_native_start_verify_s", 45), 45.0),
                                    ),
                                )
                                if str(c_data.get("_bev_full_block_reason") or "") == "start_rejected_soft":
                                    c_data["_bev_full_block_reason"] = ""
                                    c_data["_openwb_start_reject_soft_until"] = 0.0
                                c_data["_wb_stop_sent_active"] = False
                                last_change_ts[c_id] = now_ts
                                made_changes = True
                                logger.info(
                                    "WB%d E3DC-Startimpuls wiederholt: %dA bei %.0fW %s "
                                    "(Freigabe stand, aber 0W Ladeleistung)" %
                                    (
                                        c_id,
                                        native_retry_amp,
                                        abs(grid_power_raw),
                                        "Netz/Slot" if native_grid_start_allowed else "Export",
                                    )
                                )

                        _loop_status = next(
                            (v.get('status') for v in valid_chargers_status if v.get('id') == c_id),
                            None
                        )
                        if _wb_status_real_charging(_loop_status) and c_data.get("current_set_amp", 0) > 0:
                            active_chargers_count += 1
                            total_set_amp += c_data.get("current_set_amp", 0)

                    # Safety-Watcher: anhaltender Netzbezug > 500W fuer > 45s -> Deckel auf 6A
                    # AUSNAHMEN: Im Preis-Optimierungs-Modus ODER Mode 11 (Sofortladen inkl. Netz)
                    # ist Netzbezug gewollt! Wachter inaktiv wenn Netz explizit erlaubt.
                    GRID_WATCH_W = 500
                    GRID_WATCH_S = 45
                    _grid_intentional = price_optimizing_active or effective_allow_grid
                    if grid_power_raw > GRID_WATCH_W and charging_active_any and not _grid_intentional:
                        if grid_overshoot_ts is None:
                            grid_overshoot_ts = now_ts
                        elif (now_ts - grid_overshoot_ts) >= GRID_WATCH_S:
                            logger.warning("[Wachter] %.0fW Netzbezug > %ds -> Deckel auf 6A" % (
                                grid_power_raw, GRID_WATCH_S))
                            for cd in chargers:
                                if hasattr(cd["charger"], "set_amp_sonnenmodus"):
                                    _execute_wallbox_driver_command(
                                        cd,
                                        {
                                            "method": "set_amp_sonnenmodus",
                                            "amp": 6,
                                            "force_state": None,
                                            "reason": "grid_watchdog_clamp",
                                        },
                                        c_id=cd.get("id"),
                                    )
                                elif hasattr(cd["charger"], "set_direct_current"):
                                    _execute_wallbox_driver_command(
                                        cd,
                                        {
                                            "method": "set_direct_current",
                                            "amp": 6,
                                            "reason": "grid_watchdog_clamp",
                                        },
                                        c_id=cd.get("id"),
                                    )
                                cd["current_set_amp"] = 6
                                last_change_ts[cd["id"]] = now_ts
                            grid_overshoot_ts = None
                    else:
                        grid_overshoot_ts = None

                    # UI Update
                    # wb_current_amp nachfuehren (fuer selbstlernende Min-Leistung)
                    # Das ist der tatsaechlich gesetzte Strom der ersten aktiven Wallbox.
                    for cd in chargers:
                        _cd_status = next(
                            (v.get('status') for v in valid_chargers_status if v.get('id') == cd.get('id')),
                            None
                        )
                        if _wb_status_real_charging(_cd_status) and cd.get('current_set_amp', 0) > 0:
                            wb_current_amp = cd['current_set_amp']
                            break

                    if active_chargers_count > 0:
                        ui_state["charging_active"] = True
                        ui_state["set_amp"] = total_set_amp 
                        if len(chargers) > 1:
                            ui_state["status_msg"] = f"Lade parallel ({active_chargers_count} WB) Max: {total_set_amp}A"
                        else:
                            ui_state["status_msg"] = f"Lade mit {total_set_amp}A (PV Ok)"
                    else:
                        if charging_active_any:
                            ui_state["charging_active"] = True
                            ui_state["status_msg"] = "Wallbox führt autonom"
                            # Wir lesen das Ampere-Limit aus dem Charger (entweder real oder zuletzt gesetzt)
                            ui_state["set_amp"] = sum(
                                cd.get('current_set_amp', 6)
                                for cd in chargers
                                if _wb_status_real_charging(next(
                                    (v.get('status') for v in valid_chargers_status if v.get('id') == cd.get('id')),
                                    None
                                ))
                            )
                            if ui_state["set_amp"] == 0:
                                ui_state["set_amp"] = 6
                        else:
                            ui_state["charging_active"] = False
                            start_permission_amp = 0
                            for _cd in chargers:
                                _cd_status = next(
                                    (v.get('status') for v in valid_chargers_status if v.get('id') == _cd.get('id')),
                                    None
                                )
                                if not _wb_status_connected(_cd_status):
                                    continue
                                start_permission_amp = max(
                                    start_permission_amp,
                                    int(_cd.get('current_set_amp', 0) or 0),
                                )
                            if (native_e3dc_start_without_power and cap_amp > 0) or start_permission_amp > 0:
                                ui_state["set_amp"] = max(int(cap_amp or 0), start_permission_amp)
                                ui_state["status_msg"] = "Startfreigabe %dA (warte auf Fahrzeug)" % ui_state["set_amp"]
                            else:
                                ui_state["status_msg"] = "Preis-Boost: Wallbox freigegeben" if price_boost_wallbox_active else "Warte auf Sonne..."

                    # Detailed WB status
                    wb_detail_list = []
                    _detail_priority_target_connected = _priority_target_connected(
                        chargers,
                        valid_chargers_status,
                        wb_dist_mode,
                    )
                    for c_data in chargers:
                        c_id = c_data['id']
                        st = None
                        for v in valid_chargers_status:
                            if v['id'] == c_id:
                                st = v['status']
                                break
                        if _wb_status_connected(st):
                            _charge_contract = dict(c_data.get("_charge_contract") or {})
                            if not _charge_contract:
                                _charge_contract = _wallbox_charge_observation_contract(st, c_data, now_ts=time.time())
                            _apply_charge_contract_to_status(st, _charge_contract)
                            _stop_display_state = _manager_stop_display_state(c_data)
                            if _stop_display_state.get("active", False):
                                _charge_contract = _apply_manager_stop_display_to_status(
                                    st,
                                    _charge_contract,
                                    _stop_display_state,
                                )
                            _real_state = _wb_status_real_charging(st)
                            _real_power_w = _wb_status_real_power(st) if _real_state else 0.0
                            _bev_blocked = bool(c_data.get("_bev_full_blocked", False))
                            _charge_end_contract = dict(c_data.get("_charge_end_contract") or {})
                            if not _charge_end_contract:
                                _charge_end_contract = _wallbox_charge_end_latch_contract(
                                    c_data,
                                    st,
                                    now_ts=time.time(),
                                    config=config,
                                    charger_id=c_id,
                                    public_mode=effective_public_wb_mode,
                                    allow_new_latch=False,
                                    disconnected_release=not _wb_status_connected(st),
                                    mode_off=effective_public_wb_mode == MODE_OFF,
                                )
                            _ramp_contract = c_data.get("_ramp_contract")
                            if not isinstance(_ramp_contract, dict):
                                _ramp_contract = {}
                            _physical_detail = c_data.get("_physical_budget")
                            if not isinstance(_physical_detail, dict):
                                _physical_detail = {}
                            _phase_contract = dict(_physical_detail.get("phase_contract") or c_data.get("_phase_contract") or {})
                            if not _phase_contract:
                                _phase_contract = wallbox_decision.phase_observation_contract(
                                    st,
                                    c_data,
                                    detected_phases=detected_phases,
                                    vehicle_max_phases=_vehicle_max_ac_phases(config, c_id, st),
                                    phase_target=_valid_phase_count((st or {}).get("phases_target"), 0),
                                    phase_capability=c_data.get("_openwb_phase_capability"),
                                    charger_class_name=str(c_data.get("_charger_class_name", "") or ""),
                                    driver_variant=str((st or {}).get("driver_variant", "") or ""),
                                )
                            _apply_phase_contract_to_status(st, _phase_contract)
                            detail_state = _wallbox_detail_status(
                                st,
                                c_data,
                                public_mode=effective_public_wb_mode,
                                cap_amp=cap_amp,
                                allowed_w=allowed_w,
                                budget_stale=_budget_stale,
                                budget_timeout=_budget_timeout,
                                mode5_grid_allowed=mode5_grid_allowed,
                                scheduled_slot_active=bool(scheduled_slot_charger_ids),
                                price_boost_active=price_boost_wallbox_active,
                                predump_wallbox_active=predump_wallbox_active,
                                wbminsoc_gate_open=wbminsoc_gate_open,
                                house_fuse_limited=house_fuse_limited,
                                house_fuse_cap_amp=house_fuse_cap_amp,
                                detected_phases=detected_phases,
                                min_amp=wb_min_amp_cfg,
                                physical_budget=c_data.get("_physical_budget"),
                                vehicle_max_phases=_vehicle_max_ac_phases(config, c_id, st),
                                openwb_phase_capable=bool((c_data.get("_openwb_phase_capability") or {}).get("can_switch", False)),
                            )
                            _detail_manual_pause_active = bool(
                                wb_manual_pause.get(c_id, False)
                                or c_data.get("_manual_pause_active", False)
                            )
                            if _detail_manual_pause_active:
                                detail_state = {
                                    'state': 'Manuell pausiert',
                                    'state_level': 'warning',
                                    'state_reason': 'Nutzerpause aktiv; Play gibt die bestehende Regelung wieder frei.',
                                    'min_power_w': detail_state.get('min_power_w', 0),
                                }
                            state_str = detail_state.get("state", "Lade" if _real_state else "Angesteckt")
                            _detail_manager_amp = round(float(c_data.get('current_set_amp', 0) or 0), 1)
                            _detail_hw_amp = round(float(st.get('amp', 0) or 0), 1)
                            _detail_cap_amp = round(float(cap_amp or 0), 1)
                            if _real_state:
                                _detail_amp = _detail_manager_amp
                            elif _bev_blocked:
                                _detail_amp = 0.0
                            elif state_str == "Startfreigabe":
                                _detail_amp = max(_detail_manager_amp, _detail_hw_amp)
                            else:
                                _detail_amp = 0.0
                            if _stop_display_state.get("active", False):
                                _real_state = False
                                _real_power_w = 0.0
                                _detail_amp = 0.0
                                state_str = "Stop gesendet"
                                detail_state = {
                                    "state": state_str,
                                    "state_level": "warning",
                                    "state_reason": "Harter Stop gesendet; Messwert läuft nach.",
                                    "min_power_w": detail_state.get("min_power_w", 0),
                                }
                            wb_detail = {
                                'id': c_id,
                                'amp': _detail_amp,
                                'current_set_amp': _detail_manager_amp,
                                'cap_amp': _detail_cap_amp,
                                'target_amp': round(float(_detail_cap_amp or _detail_manager_amp or 0), 1),
                                'status_amp': _detail_hw_amp,
                                'state': state_str,
                                'state_level': detail_state.get('state_level', 'info'),
                                'state_reason': detail_state.get('state_reason', ''),
                                'min_power_w': detail_state.get('min_power_w', 0),
                                'physical_budget_ready': bool(_physical_detail.get('budget_ready', False)),
                                'physical_chargeable': bool(_physical_detail.get('can_start_or_hold', False)),
                                'physical_phases': int(_physical_detail.get('phases', 0) or 0),
                                'physical_reason': _physical_detail.get('reason', ''),
                                'charge_contract': _charge_contract,
                                'charge_truth': str(_charge_contract.get('truth', 'not_charging') or 'not_charging'),
                                'charge_source': str(_charge_contract.get('source', '') or ''),
                                'charge_confidence': str(_charge_contract.get('confidence', '') or ''),
                                'charge_power_w': round(float(_charge_contract.get('power_w', 0.0) or 0.0), 1),
                                'charge_raw_power_w': round(float(_charge_contract.get('raw_power_w', 0.0) or 0.0), 1),
                                'phantom_power_w': round(float(_charge_contract.get('phantom_power_w', 0.0) or 0.0), 1),
                                'charge_energy_increasing': bool(_charge_contract.get('energy_increasing', False)),
                                'charge_energy_delta_wh': round(float(_charge_contract.get('energy_delta_wh', 0.0) or 0.0), 3),
                                'charge_energy_source': str(_charge_contract.get('energy_source', '') or ''),
                                'charge_end_contract': _charge_end_contract,
                                'charge_end_latched': bool(_charge_end_contract.get('latched', _bev_blocked)),
                                'charge_end_action': str(_charge_end_contract.get('action', '') or ''),
                                'charge_end_reason': str(_charge_end_contract.get('reason', '') or ''),
                                'charge_end_exception': str(_charge_end_contract.get('exception', '') or ''),
                                'charge_end_start_blocked': bool(_charge_end_contract.get('start_blocked', _bev_blocked)),
                                'ramp_contract': _ramp_contract,
                                'ramp_raw_target_amp': int(_ramp_contract.get('raw_target_amp', 0) or 0),
                                'ramp_applied_amp': int(_ramp_contract.get('applied_amp', 0) or 0),
                                'ramp_limited': bool(_ramp_contract.get('limited', False)),
                                'ramp_reason': str(_ramp_contract.get('reason', '') or ''),
                                'phase_contract': _phase_contract,
                                'phase_actual_phases': int(_phase_contract.get('actual_phases', 0) or 0),
                                'phase_actual_source': str(_phase_contract.get('actual_source', '') or ''),
                                'phase_effective_phases': int(_phase_contract.get('effective_phases', 0) or 0),
                                'phase_effective_source': str(_phase_contract.get('effective_source', '') or ''),
                                'phase_cable_phases': int(_phase_contract.get('cable_phases', 0) or 0),
                                'phase_vehicle_max_phases': int(_phase_contract.get('vehicle_max_phases', 0) or 0),
                                'phase_wallbox_phases': int(_phase_contract.get('wallbox_phases', 0) or 0),
                                'phase_can_switch': bool(_phase_contract.get('can_switch_phases', False)),
                                'power_w': round(_real_power_w, 1),
                                'plug': _wb_status_connected(st),
                                'charging': _real_state,
                                'manager_stop_pending': bool(_stop_display_state.get("active", False)),
                                'manager_stop_reason': str(_stop_display_state.get("reason", "") or ""),
                                'manager_stop_age_s': round(float(_stop_display_state.get("age_s", 0.0) or 0.0), 1),
                                'manager_stop_remaining_s': round(float(_stop_display_state.get("remaining_s", 0.0) or 0.0), 1),
                                'bev_full_blocked': _bev_blocked,
                                'max_amp': _charger_max_amp(config, c_id, wb_global_max_amp),
                                'priority_mode': int(wb_dist_mode),
                                'priority_active': bool(
                                    int(wb_dist_mode) in (1, 2)
                                    and c_id == int(wb_dist_mode)
                                    and _wb_status_connected(st)
                                ),
                                'priority_waiting': bool(
                                    int(wb_dist_mode) in (1, 2)
                                    and c_id != int(wb_dist_mode)
                                    and _detail_priority_target_connected
                                ),
                                'manual_pause': _detail_manual_pause_active,
                            }
                            for _detail_key in (
                                'control_status', 'control_label', 'control_detail', 'control_level',
                                'openwb_primary_grid_phase_warning',
                                'openwb_primary_grid_phase_warning_reason',
                                'openwb_primary_grid_target_amp',
                                'openwb_primary_grid_actual_power_w',
                                'openwb_primary_grid_actual_phases',
                                'openwb_primary_grid_expected_3p_w',
                                'openwb_primary_grid_vehicle_phases',
                                'openwb_primary_grid_connected_phases',
                                'vehicle_under_acceptance_warning',
                                'vehicle_under_acceptance_reason',
                                'vehicle_under_acceptance_offered_amp',
                                'vehicle_under_acceptance_target_phases',
                                'vehicle_under_acceptance_actual_power_w',
                                'vehicle_under_acceptance_expected_power_w',
                                'vehicle_under_acceptance_accepted_ratio',
                                'vehicle_under_acceptance_actual_phases',
                                'vehicle_under_acceptance_charger_class',
                            ):
                                if _detail_key in detail_state:
                                    wb_detail[_detail_key] = detail_state[_detail_key]
                            phase_capability = c_data.get("_openwb_phase_capability")
                            if isinstance(phase_capability, dict):
                                wb_detail['can_switch_phases'] = bool(phase_capability.get('can_switch', False))
                                wb_detail['phase_switch_capability'] = phase_capability.get('capability', '')
                                wb_detail['phase_switch_source'] = phase_capability.get('source', '')
                                wb_detail['api_surface'] = phase_capability.get('api_surface', '')
                            decision_payload, driver_command = _wallbox_decision_payload_or_default(
                                c_data,
                                st,
                                public_mode=effective_public_wb_mode,
                                control_mode=controller_mode(effective_public_wb_mode),
                                cap_amp=cap_amp,
                                allowed_w=allowed_w,
                                detected_phases=detected_phases,
                                max_amp=_charger_max_amp(config, c_id, wb_global_max_amp),
                                mode_label_text=mode_label(effective_public_wb_mode),
                                storage_state=_budget_state,
                                budget_timeout=_budget_timeout,
                            )
                            wb_detail["decision_payload"] = decision_payload
                            wb_detail["driver_command"] = driver_command
                            _attach_wallbox_countdown_diagnostics(wb_detail, c_data)
                            _copy_wallbox_status_diagnostics(wb_detail, st)
                            for _k in (
                                'driver_variant', 'device_name', 'firmware_version',
                                'rscp_wb_index',
                                'rscp_status', 'rscp_error_active', 'rscp_error_count',
                                'rscp_last_error', 'rscp_last_error_context',
                                'rscp_last_error_ts', 'rscp_last_ok_ts', 'rscp_last_ok_context',
                                'e3dc_session_state', 'e3dc_session_label', 'e3dc_session_level',
                                'e3dc_session_reason', 'e3dc_session_offered_amp',
                                'e3dc_session_budget_ready', 'e3dc_session_start_requested',
                                'e3dc_session_start_verifying', 'e3dc_session_stop_active',
                                'e3dc_session_start_blocked',
                                'e3dc_session_can_send_start_toggle', 'e3dc_session_counts_as_real_charge',
                                'openwb_pro_contract', 'openwb_pro_runtime_path',
                                'openwb_pro_session_guard_required', 'openwb_pro_charge_verification_required',
                                'openwb_pro_session_state', 'openwb_pro_session_label', 'openwb_pro_session_level',
                                'openwb_pro_session_reason', 'openwb_pro_session_offered_amp',
                                'openwb_pro_session_budget_ready', 'openwb_pro_session_start_requested',
                                'openwb_pro_session_start_verifying', 'openwb_pro_session_wakeup_pending',
                                'openwb_pro_session_wakeup_remaining_s',
                                'openwb_pro_session_start_hold_remaining_s',
                                'openwb_pro_session_stop_remaining_s',
                                'openwb_pro_session_phase_wait_remaining_s',
                                'openwb_pro_session_start_hold_active',
                                'openwb_pro_session_phase_wait_active', 'openwb_pro_session_phase_wait_target',
                                'openwb_pro_session_phase_wait_since_s',
                                'openwb_pro_session_phase_wait_last_duration_s',
                                'openwb_pro_session_phase_wait_last_result',
                                'openwb_pro_session_phase_wait_last_target',
                                'openwb_pro_session_phase_wait_samples',
                                'openwb_pro_session_phase_wait_ema_s',
                                'openwb_pro_session_phase_wait_max_s',
                                'openwb_pro_session_stop_active',
                                'openwb_pro_session_start_blocked',
                                'openwb_pro_session_can_send_start_command', 'openwb_pro_session_counts_as_real_charge',
                                'openwb_pro_temporary_stop_contract',
                                'openwb_pro_temporary_stop_active',
                                'openwb_pro_temporary_stop_reason',
                                'openwb_pro_temporary_stop_state_hint',
                                'openwb_pro_temporary_stop_waiting',
                                'openwb_pro_start_retry_guard_contract',
                                'last_executed_command',
                                'driver_status_valid', 'driver_status_stale',
                                'driver_status_degraded', 'driver_status_age_s',
                                'driver_status_reason', 'driver_status_last_ok_ts',
                                'driver_status_last_sample_ts', 'driver_status_source',
                                'driver_status_plausible', 'driver_status_glitch',
                                'driver_status_glitch_reason', 'driver_status_last_good_ts',
                                'mqtt_connected', 'mqtt_reconnect_backoff_s',
                                'openwb_secondary_contract', 'openwb_secondary_runtime_path',
                                'openwb_secondary_session_guard_required', 'openwb_secondary_charge_verification_required',
                                'openwb_secondary_session_state', 'openwb_secondary_session_label', 'openwb_secondary_session_level',
                                'openwb_secondary_session_reason', 'openwb_secondary_session_offered_amp',
                                'openwb_secondary_session_current_set_amp', 'openwb_secondary_session_cap_amp',
                                'openwb_secondary_session_hardware_amp', 'openwb_secondary_session_last_command_amp',
                                'openwb_secondary_session_heartbeat_ok', 'openwb_secondary_session_command_ok',
                                'openwb_secondary_session_command_blocked', 'openwb_secondary_session_api_surface',
                                'openwb_secondary_session_secondary_active',
                                'openwb_secondary_session_budget_ready', 'openwb_secondary_session_start_requested',
                                'openwb_secondary_session_start_verifying', 'openwb_secondary_session_stop_active',
                                'openwb_secondary_session_start_blocked',
                                'openwb_secondary_session_can_send_start_command', 'openwb_secondary_session_counts_as_real_charge',
                                'goe_contract', 'goe_runtime_path',
                                'goe_session_guard_required', 'goe_charge_verification_required',
                                'goe_session_state', 'goe_session_label', 'goe_session_level',
                                'goe_session_reason', 'goe_session_offered_amp',
                                'goe_session_current_set_amp', 'goe_session_cap_amp', 'goe_session_hardware_amp',
                                'goe_session_frc', 'goe_session_budget_ready', 'goe_session_start_requested',
                                'goe_session_start_verifying', 'goe_session_stop_active',
                                'goe_session_start_blocked',
                                'goe_session_can_send_start_command', 'goe_session_counts_as_real_charge',
                                'charge_truth', 'charge_is_charging', 'charge_counts_as_real',
                                'charge_confidence', 'charge_source', 'charge_power_w',
                                'charge_raw_power_w', 'phantom_power_w',
                                'charge_energy_increasing', 'charge_energy_delta_wh',
                                'charge_energy_source',
                                'charge_end_contract', 'charge_end_latched', 'charge_end_action',
                                'charge_end_reason', 'charge_end_exception', 'charge_end_start_blocked',
                                'ramp_contract', 'ramp_raw_target_amp', 'ramp_applied_amp',
                                'ramp_limited', 'ramp_reason',
                                'enabled', 'extern_alg_hex', 'rfid_tag',
                                'alg_seen', 'alg_flags', 'alg_charging', 'alg_connected',
                                'device_working',
                                'chargepoint_name', 'charge_template_name',
                                'state_text', 'fault_text', 'fault_state',
                                'manual_lock', 'min_current',
                                'pv_charging_min_current', 'instant_charging_current',
                                'instant_charging_limit', 'instant_charging_soc',
                                'car_soc', 'car_soc_source', 'car_soc_raw_ts', 'car_soc_rule_confirmed',
                                'car_name', 'car_id', 'vehicle_id',
                                'car_capacity_kwh',
                                'phase_power_l1_w', 'phase_power_l2_w',
                                'phase_power_l3_w', 'phase_power_sum_w',
                                'phase_apparent_l1_va', 'phase_apparent_l2_va',
                                'phase_apparent_l3_va', 'apparent_power_va',
                                'phase_current_l1_a', 'phase_current_l2_a',
                                'phase_current_l3_a',
                                'apparent_power_kva', 'power_factor',
                                'phase_power_verified', 'phases_in_use',
                                'phases_target',
                                'phase_contract',
                                'phase_actual_phases', 'phase_actual_source',
                                'phase_effective_phases', 'phase_effective_source',
                                'phase_cable_phases', 'phase_vehicle_max_phases',
                                'phase_wallbox_phases', 'phase_can_switch',
                                'can_switch_phases', 'phase_switch_capability',
                                'phase_switch_source', 'api_surface',
                                'serial', 'version', 'v2g_ready',
                                'evse_signaling', 'offered_current_raw',
                                'current_step_amp', 'fractional_current_supported',
                                'max_charge_power', 'max_discharge_power',
                                'temp_c', 'cp_interrupt_isactive',
                                'cp_interrupt_duration', 'cp_interrupt_version',
                                'control_status', 'control_label', 'control_detail',
                                'control_level', 'control_ts',
                                'last_command_ok', 'last_command_amp', 'last_command_ts',
                                'last_heartbeat_ok',
                                'last_heartbeat_ts',
                                'configured_role', 'detected_role', 'effective_role',
                                'role_detection_source', 'role_detection_detail',
                                'role_mismatch', 'command_failure_count',
                                'command_failure_limit', 'command_blocked',
                                'command_blocked_until',
                            ):
                                if _k in st and _k not in wb_detail:
                                    wb_detail[_k] = st[_k]
                            if 'session_kwh' in st: wb_detail['session_kwh'] = st['session_kwh']
                            if 'total_kwh' in st: wb_detail['total_kwh'] = st['total_kwh']
                            wb_detail_list.append(wb_detail)
                        else:
                            decision_payload, driver_command = _wallbox_decision_payload_or_default(
                                c_data,
                                st,
                                public_mode=effective_public_wb_mode,
                                control_mode=controller_mode(effective_public_wb_mode),
                                cap_amp=0,
                                allowed_w=0,
                                detected_phases=detected_phases,
                                max_amp=_charger_max_amp(config, c_id, wb_global_max_amp),
                                mode_label_text=mode_label(effective_public_wb_mode),
                                storage_state=_budget_state,
                                budget_timeout=_budget_timeout,
                            )
                            idle_detail = {
                                'id': c_id,
                                'amp': 0,
                                'current_set_amp': 0,
                                'cap_amp': 0,
                                'target_amp': 0,
                                'status_amp': 0,
                                'state': 'Idle',
                                'state_level': 'secondary',
                                'state_reason': 'Kein Fahrzeug verbunden.',
                                'min_power_w': 0,
                                'power_w': 0,
                                'plug': False,
                                'charging': False,
                                'max_amp': _charger_max_amp(config, c_id, wb_global_max_amp),
                                'priority_mode': int(wb_dist_mode),
                                'priority_active': False,
                                'priority_waiting': bool(
                                    int(wb_dist_mode) in (1, 2)
                                    and c_id != int(wb_dist_mode)
                                    and _detail_priority_target_connected
                                ),
                                'manual_pause': bool(
                                    wb_manual_pause.get(c_id, False)
                                    or c_data.get("_manual_pause_active", False)
                                ),
                                'decision_payload': decision_payload,
                                'driver_command': driver_command,
                            }
                            _copy_wallbox_status_diagnostics(idle_detail, st)
                            wb_detail_list.append(idle_detail)
                    ui_state["wb_details"] = wb_detail_list
                    if (
                        any(bool(_d.get("bev_full_blocked", False)) for _d in wb_detail_list)
                        and not any(bool(_d.get("charging", False)) for _d in wb_detail_list)
                    ):
                        if effective_public_wb_mode == MODE_BATTERY_DEPARTURE:
                            ui_state["status_msg"] = "Akku bis Abfahrt: beendet"
                            ui_state["operator_hint"] = (
                                "Akku bis Abfahrt beendet: Ladeende oder Ziel erreicht."
                            )
                            ui_state["operator_hint_level"] = "secondary"
                            ui_state["operator_hint_code"] = "battery_departure_done"
                        else:
                            ui_state["status_msg"] = "Ladung beendet"
                            ui_state["operator_hint"] = (
                                "Ladung extern beendet. Neustart nach Umstecken/Moduswechsel."
                            )
                            ui_state["operator_hint_level"] = "secondary"
                            ui_state["operator_hint_code"] = "vehicle_charge_done"

                    # --- RSCP-Native Session Tracking (nur fuer E3DC Charger) ---
                    # Schreibt wb_live_session.json direkt aus RSCP-Daten (kein C++ Polling-Glitch-Risiko!)
                    wb_live_session_file = os.path.join(LOG_DIR, "wb_live_session.json")
                    for c_data in chargers:
                        st = None
                        for v in valid_chargers_status:
                            if v['id'] == c_data['id']:
                                st = v['status']
                                break
                        if st is None:
                            continue
                        # Nur E3DC-Charger haben RSCP-Session-Daten
                        if 'session_kwh' not in st:
                            continue
                        
                        rscp_session_kwh     = st.get('session_kwh')     # kWh aus E3DC Firmware
                        rscp_session_start   = st.get('session_start_ts') # Unix-Timestamp Steckverbindung
                        car_connected_rscp   = st.get('car_connected_rscp', st.get('car', 1) >= 2)
                        
                        # Lese vorherige Session-Datei (um session_start dauerhaft zu speichern)
                        prev_session = {}
                        try:
                            if os.path.exists(wb_live_session_file):
                                with open(wb_live_session_file, 'r') as f:
                                    prev_session = json.load(f)
                        except Exception:
                            pass

                        # Wenn das Auto angesteckt ist, schreibe Session-Daten direkt aus RSCP
                        if car_connected_rscp:
                            # Session-Start aus RSCP verwenden; Fallback: bereits gespeicherter Wert
                            start_ts = rscp_session_start or prev_session.get('session_start_ts') or int(time.time())
                            
                            # Leistungs-Puffer: Wenn RSCP aktuell 0W liefert aber das Auto verbunden ist,
                            # behalten wir den letzten gueltigen Wert fuer max. 20 Sekunden (Polling-Glitch-Schutz).
                            # WICHTIG: Nur ECHTE gemessene Leistung (real_power_w) verwenden!
                            # NIEMALS den theoretischen Schaetzwert (set_amp x 230 x phases = 22kW) schreiben!
                            now_ts = int(time.time())
                             
                            # Echte gemessene Leistung aus RSCP (nicht die theoretische!)
                            real_power_w = st.get('real_power_w')
                            if real_power_w is not None:
                                real_power_w = float(real_power_w)
                            else:
                                real_power_w = 0.0
                            
                            real_charging_bit = _wb_status_real_charging(st)
                            power_to_write = real_power_w if real_charging_bit and real_power_w > 50 else 0.0
                            
                            # Glitch-Schutz: Letzten gemessenen Wert einfrieren (max. 8s)
                            # Nur wenn vorheriger Wert auch REAL gemessen war (source='rscp_real')
                            if power_to_write == 0 and real_charging_bit and prev_session.get('car_connected', False):
                                prev_power     = prev_session.get('power_w', 0)
                                prev_power_ts  = prev_session.get('last_power_ts', 0)
                                prev_source    = prev_session.get('power_source', '')
                                # Nur einfrieren wenn vorheriger Wert REAL gemessen (nicht Schaetzung!)
                                if prev_power > 50 and prev_source == 'rscp_real' and (now_ts - prev_power_ts) < 20:
                                    power_to_write = prev_power
                                    logger.debug(f"WB{c_data['id']} power_w Glitch, halte letzten Messwert {prev_power:.0f}W")

                            session_data = {
                                'power_w':          round(power_to_write),
                                'power_source':     (
                                    'rscp_real' if real_charging_bit and real_power_w > 50
                                    else ('glitch_hold' if real_charging_bit else 'idle')
                                ),
                                'session_kwh':      rscp_session_kwh if rscp_session_kwh is not None else prev_session.get('session_kwh', 0),
                                'session_start_ts': start_ts,
                                'car_connected':    True,
                                'source':           'rscp',
                                'ts':               now_ts,
                                'last_power_ts':    now_ts if real_charging_bit and real_power_w > 50 else prev_session.get('last_power_ts', 0),
                            }
                            try:
                                tmp = wb_live_session_file + '.tmp'
                                with open(tmp, 'w') as f:
                                    json.dump(session_data, f)
                                os.replace(tmp, wb_live_session_file)
                                os.chmod(wb_live_session_file, 0o664)
                            except Exception as e:
                                logger.error(f"Fehler beim Schreiben wb_live_session.json: {e}")
                        else:
                            # Auto abgesteckt: Session beenden (power=0, car_connected=False)
                            if prev_session.get('car_connected', False):
                                logger.info(f"WB{c_data['id']} Stecker gezogen. Session beendet. "
                                            f"Gesamt: {prev_session.get('session_kwh', 0):.3f} kWh")
                                session_data = {
                                    'power_w':          0,
                                    'session_kwh':      prev_session.get('session_kwh', 0),
                                    'session_start_ts': prev_session.get('session_start_ts'),
                                    'car_connected':    False,
                                    'source':           'rscp',
                                    'ts':               int(time.time()),
                                }
                                try:
                                    tmp = wb_live_session_file + '.tmp'
                                    with open(tmp, 'w') as f:
                                        json.dump(session_data, f)
                                    os.replace(tmp, wb_live_session_file)
                                    os.chmod(wb_live_session_file, 0o664)
                                except Exception:
                                    pass
                        break  # Nur erste E3DC WB bearbeiten (WB1)

                    loop_sleep = wallbox_loop_sleep_s(
                        config,
                        made_changes=made_changes,
                        statuses=valid_chargers_status,
                        wb_details=wb_detail_list,
                        scheduled_slot_active=bool(scheduled_slot_charger_ids),
                        price_boost_active=price_boost_wallbox_active,
                        predump_wallbox_active=predump_wallbox_active,
                        budget_stale=_budget_stale,
                        budget_timeout=_budget_timeout,
                        storage_state=_budget_state,
                    )
                    ui_state["loop_sleep_s"] = loop_sleep
                    ui_state["loop_sleep_reason"] = "idle_backoff" if loop_sleep > 2.0 else "active_edge"

                    write_wallbox_decision_snapshot(ui_state, config, {
                        "intent_reason": intent_reason,
                        "battery_request": battery_request,
                        "made_changes": made_changes,
                        "budget_stale": _budget_stale,
                        "budget_timeout": _budget_timeout,
                        "storage_state": _budget_state,
                    })
                    write_status(ui_state)
                    
                    # C++ Brücke für Eba-M 
                    try:
                        import paho.mqtt.publish as publish
                        bridge_topic = config.get("wb_topic", "openWB/lp/1/W")
                        if not bridge_topic: bridge_topic = "openWB/lp/1/W"
                        publish.single(bridge_topic, str(int(total_current_wb_consumption)), hostname="127.0.0.1", keepalive=60)
                    except: pass
                        
                    time.sleep(loop_sleep)
                    
                except Exception as inner_e:
                    logger.error(f"Fehler im Regel-Durchlauf: {inner_e}")
                    time.sleep(5)

        except Exception as e:
            logger.error(f"Unerwarteter Fehler im Hauptloop: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
