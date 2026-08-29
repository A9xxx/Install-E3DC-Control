#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shadow-Regler fuer den Storage Manager.

Dieser Baustein ist absichtlich read-only gegenueber dem E3DC. Er liest dieselben
Ramdisk-/Config-Signale wie der aktive Storage Manager, berechnet eine parallele
Empfehlung und schreibt nur einen Trace in die Ramdisk. RSCP-Kommandos bleiben
allein beim aktiven Storage Manager.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

try:
    from reserve import effective_ep_reserve_pct
except Exception:  # pragma: no cover - package import fallback
    from .reserve import effective_ep_reserve_pct  # type: ignore

try:
    from pv_forecast_topology import resolve_buffered_pcc_limit
except Exception:  # pragma: no cover - package import fallback
    from .pv_forecast_topology import resolve_buffered_pcc_limit  # type: ignore


RAMDISK = "/var/www/html/ramdisk"
DATA_DIR = "/var/www/html/data"
V4_CFG = os.path.join(DATA_DIR, "e3dc_v4.json")
LIVE_F = os.path.join(RAMDISK, "live_data_py.json")
PLAN_F = os.path.join(RAMDISK, "storage_plan.json")
WB_BUDGET_F = os.path.join(RAMDISK, "wb_pv_budget.json")
WB_INTENT_F = os.path.join(RAMDISK, "wallbox_storage_intent.json")
SHADOW_STATE_F = os.path.join(RAMDISK, "storage_parallel_state.json")
SHADOW_HISTORY_F = os.path.join(RAMDISK, "storage_parallel_history.jsonl")
SHADOW_DIFF_STATE_F = os.path.join(RAMDISK, "storage_parallel_diff_state.json")
SHADOW_DIFF_HISTORY_F = os.path.join(RAMDISK, "storage_parallel_diff_history.jsonl")
SHADOW_DIFF_BRIEF_F = os.path.join(RAMDISK, "storage_parallel_diff_brief.json")
SHADOW_DIFF_BRIEF_TXT_F = os.path.join(RAMDISK, "storage_parallel_diff_brief.txt")

DIFF_LOG = logging.getLogger("StorageManager.ShadowCompare")
DISABLED_WALLBOX_TYPES = {
    "none", "disabled", "deaktiviert", "aus", "keine", "keine_wallbox",
    "no_wallbox", "off", "false", "no", "0", "-1",
}

MODE_AUTO = 0
MODE_IDLE = 1
MODE_DISCH = 2
MODE_CHRG = 3
MODE_GRID = 4
EMS_POWER_SETTINGS_NONZERO_MIN_W = 300
# Der etablierte RSCP_POWER_SETTINGS-Vertrag arbeitet mit einem strikten
# 50-W-Readback-Fenster. Ein frischer Readback innerhalb dieses Fensters
# bestätigt denselben früheren Cap; ab 50 W bleibt der Pfad fail-closed und
# hält den zuletzt sicheren Rahmen.
RSCP_POWER_SETTINGS_TOLERANCE_W = 50

AUTO_HOLD_STATES = {
    "parallel_auto",
    "parallel_evening_release",
    "parallel_curve_auto_hold",
    "parallel_curve_auto_charge",
    "parallel_curve_auto_no_surplus",
    "parallel_grid_relief_auto",
    "parallel_price_auto",
    "parallel_planned_load_hold",
    "parallel_planned_load_price_support",
    "parallel_wb_auto",
}

STORAGE_STATE_DEFAULT_MODES = {
    "parallel_no_data": -1,
    "parallel_passthrough": MODE_AUTO,
    "parallel_emergency_auto": MODE_AUTO,
    "parallel_price_grid": MODE_GRID,
    "parallel_price_auto": MODE_AUTO,
    "parallel_price_hold": MODE_AUTO,
    "parallel_price_house_discharge": MODE_AUTO,
    "parallel_planned_load_hold": MODE_AUTO,
    "parallel_planned_load_price_support": MODE_AUTO,
    "parallel_night_floor_hold": MODE_AUTO,
    "parallel_curve_charge_cap": MODE_CHRG,
    "parallel_curve_auto_hold": MODE_AUTO,
    "parallel_curve_auto_charge": MODE_AUTO,
    "parallel_curve_charge": MODE_CHRG,
    "parallel_curve_auto_no_surplus": MODE_AUTO,
    "parallel_headroom_discharge": MODE_DISCH,
    "parallel_wb_auto": MODE_AUTO,
    "parallel_grid_relief_auto": MODE_AUTO,
    "parallel_evening_release": MODE_AUTO,
    "parallel_auto": MODE_AUTO,
}

STORAGE_HARD_OVERRIDE_STATES = {
    "parallel_no_data",
    "parallel_passthrough",
    "parallel_emergency_auto",
    "parallel_price_grid",
    "parallel_price_auto",
    "parallel_price_hold",
    "parallel_price_house_discharge",
    "parallel_planned_load_hold",
    "parallel_planned_load_price_support",
    "parallel_curve_charge_cap",
    "parallel_evening_release",
}

STORAGE_HARD_OVERRIDE_PRIORITIES = {"failsafe", "protected", "safety", "price", "load", "forecast_shortfall"}

STORAGE_TRANSITION_RULES = {
    # Kurvenkanten werden gehalten. Genau hier entstehen die sichtbaren
    # AUTO/CHRG/IDLE-Wechsel, wenn Wolken und Messlatenz gegeneinander laufen.
    "parallel_curve_charge": {
        "min_hold_s": 300,
        "allow": {
            "parallel_curve_charge",
            "parallel_curve_auto_no_surplus",
            "parallel_curve_auto_charge",
            "parallel_curve_auto_hold",
            "parallel_curve_charge_cap",
            "parallel_headroom_discharge",
            "parallel_wb_auto",
            "parallel_grid_relief_auto",
            "parallel_evening_release",
            "parallel_auto",
        },
    },
    "parallel_curve_auto_no_surplus": {
        "min_hold_s": 120,
        "allow": {
            "parallel_curve_auto_no_surplus",
            "parallel_curve_charge",
            "parallel_curve_auto_charge",
            "parallel_curve_auto_hold",
            "parallel_curve_charge_cap",
            "parallel_headroom_discharge",
            "parallel_wb_auto",
            "parallel_grid_relief_auto",
            "parallel_evening_release",
            "parallel_auto",
        },
    },
    "parallel_curve_auto_hold": {
        "min_hold_s": 90,
        "allow": {
            "parallel_curve_auto_hold",
            "parallel_curve_charge_cap",
            "parallel_curve_charge",
            "parallel_curve_auto_no_surplus",
            "parallel_headroom_discharge",
            "parallel_wb_auto",
            "parallel_grid_relief_auto",
            "parallel_evening_release",
            "parallel_auto",
        },
    },
    "parallel_wb_auto": {
        "min_hold_s": 120,
        "allow": {
            "parallel_wb_auto",
            "parallel_curve_auto_hold",
            "parallel_curve_auto_no_surplus",
            "parallel_curve_charge",
            "parallel_curve_charge_cap",
            "parallel_headroom_discharge",
            "parallel_grid_relief_auto",
            "parallel_evening_release",
            "parallel_auto",
        },
    },
    "parallel_auto": {
        "min_hold_s": 60,
        "allow": {
            "parallel_auto",
            "parallel_curve_auto_hold",
            "parallel_curve_auto_no_surplus",
            "parallel_curve_charge",
            "parallel_curve_charge_cap",
            "parallel_headroom_discharge",
            "parallel_wb_auto",
            "parallel_grid_relief_auto",
            "parallel_evening_release",
        },
    },
    "parallel_grid_relief_auto": {
        "min_hold_s": 60,
        "allow": {
            "parallel_grid_relief_auto",
            "parallel_auto",
            "parallel_curve_auto_hold",
            "parallel_curve_auto_no_surplus",
            "parallel_curve_charge",
            "parallel_headroom_discharge",
            "parallel_wb_auto",
            "parallel_evening_release",
        },
    },
    # Aktive harte Eingriffe haben ihre eigene Eintrittslogik; die Tabelle
    # dokumentiert sie, haelt sie aber nicht blind, damit kein Netzbezug durch
    # eine alte Begrenzung erzwungen wird.
    "parallel_curve_charge_cap": {
        "min_hold_s": 0,
        "allow": {
            "parallel_curve_charge_cap",
            "parallel_curve_auto_hold",
            "parallel_curve_auto_no_surplus",
            "parallel_curve_charge",
            "parallel_headroom_discharge",
            "parallel_wb_auto",
            "parallel_grid_relief_auto",
            "parallel_evening_release",
            "parallel_auto",
        },
    },
    "parallel_headroom_discharge": {
        "min_hold_s": 0,
        "allow": {
            "parallel_headroom_discharge",
            "parallel_curve_charge_cap",
            "parallel_curve_auto_hold",
            "parallel_curve_auto_no_surplus",
            "parallel_curve_charge",
            "parallel_headroom_discharge",
            "parallel_wb_auto",
            "parallel_grid_relief_auto",
            "parallel_evening_release",
            "parallel_auto",
        },
    },
    "parallel_night_floor_hold": {
        "min_hold_s": 0,
        "allow": {
            "parallel_night_floor_hold",
            "parallel_auto",
            "parallel_grid_relief_auto",
            "parallel_curve_auto_no_surplus",
            "parallel_curve_charge_cap",
            "parallel_curve_charge",
            "parallel_wb_auto",
        },
    },
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return float(default)
        text = str(value).strip().replace(",", ".")
        if text == "" or text.lower() in ("none", "null", "nan", "inf", "infinity"):
            return float(default)
        result = float(text)
        return result if math.isfinite(result) else float(default)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    return int(round(_safe_float(value, default)))


def _cfg_bool(cfg: Dict[str, Any], key: str, default: bool = False) -> bool:
    raw = cfg.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "ja", "ein")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "ja", "ein")


def _configured_power_limit_w(cfg: Dict[str, Any], key: str) -> int:
    if key not in cfg:
        return 0
    value = _safe_int(cfg.get(key), 0)
    return value if value >= 300 else 0


def _live_power_limit_w(live: Dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = abs(_safe_int(live.get(key), 0))
        if value >= 300:
            return value
    return 0


def _effective_max_charge_w(cfg: Dict[str, Any], live: Optional[Dict[str, Any]] = None) -> int:
    live = live or {}
    configured = _configured_power_limit_w(cfg, "maximumladeleistung")
    if configured:
        return configured
    rscp_limit = _live_power_limit_w(
        live,
        "user_charge_limit_w",
        "bat_charge_limit_w",
    )
    return rscp_limit or 12500


def _effective_max_discharge_w(
    cfg: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    fallback_charge_w: Optional[int] = None,
) -> int:
    live = live or {}
    configured = _configured_power_limit_w(cfg, "maximaleentladeleistung")
    if configured:
        return configured
    rscp_limit = _live_power_limit_w(
        live,
        "user_discharge_limit_w",
        "bat_discharge_limit_w",
    )
    if rscp_limit:
        return rscp_limit
    return max(300, int(fallback_charge_w or _effective_max_charge_w(cfg, live)))


def _configured_kw_or_w(value: Any) -> int:
    raw = _safe_float(value, 0.0)
    if raw <= 0:
        return 0
    if raw < 250:
        return int(round(raw * 1000.0))
    return int(round(raw))


def _dc_tracker_limit_w(source: Dict[str, Any]) -> int:
    total = 0
    for idx in range(8):
        total += max(0, _safe_int(source.get(f"dc{idx}_max_w"), 0))
    return total if total >= 1000 else 0


def _inverter_ac_limit_w(
    cfg: Dict[str, Any],
    active_state: Dict[str, Any],
    live: Dict[str, Any],
) -> int:
    for key in (
        "wr_ac_limit_w",
        "wechselrichter_limit_w",
        "wechselrichterleistung_w",
        "inverter_ac_limit_w",
        "ac_power_limit_w",
    ):
        val = _configured_kw_or_w(cfg.get(key))
        if val >= 1000:
            return val
    for source in (live, active_state):
        for key in ("ac_power_limit_w", "wr_ac_limit_w", "inverter_ac_limit_w"):
            val = _safe_int(source.get(key), 0)
            if val >= 1000:
                return val
        val = _dc_tracker_limit_w(source)
        if val >= 1000:
            return val
    return 0


def _dc_coupled_ems_budget(
    pv_potential_w: Any,
    wr_max_ac_w: Any,
    export_limit_w: Any,
    home_w: Any,
    wp_w: Any,
    wallbox_actual_w: Any,
    battery_max_charge_w: Any,
    soc_pct: Any,
    external_ac_pv_w: Any = 0,
    external_ac_pv_valid: Any = False,
    external_ac_pv_source: Any = "",
) -> Dict[str, Any]:
    pv_total_w = max(0, _safe_int(pv_potential_w, 0))
    wr_limit_w = max(0, _safe_int(wr_max_ac_w, 0))
    feed_limit_w = max(0, _safe_int(export_limit_w, 0))
    fixed_load_w = max(0, _safe_int(home_w, 0)) + max(0, _safe_int(wp_w, 0))
    wb_w = max(0, _safe_int(wallbox_actual_w, 0))
    max_charge_w = max(0, _safe_int(battery_max_charge_w, 0))
    soc = _safe_float(soc_pct, 0.0)

    # PV_Power ist die physikalische Gesamt-PV am Netzpunkt. E3DC liefert den
    # externen AC-Erzeuger separat als EMS_POWER_ADD; e3dc_live hat ihn in
    # PV_Power bereits eingerechnet. Nur ein typgültiger Quellensplit darf ihn
    # vor der DC-Wechselrichtergrenze wieder abziehen. Fehlt dieser Nachweis,
    # bleibt die bisherige konservative Behandlung erhalten.
    external_source = str(external_ac_pv_source or "").strip()
    external_value = _safe_float(external_ac_pv_w, 0.0)
    external_trusted = bool(
        external_ac_pv_valid is True
        and external_source == "e3dc_add_power"
        and not isinstance(external_ac_pv_w, bool)
        and math.isfinite(external_value)
        and external_value >= 0.0
    )
    external_ac_w = (
        min(pv_total_w, max(0, int(round(external_value))))
        if external_trusted
        else 0
    )
    e3dc_dc_pv_w = max(0, pv_total_w - external_ac_w)

    if wr_limit_w >= 1000:
        battery_must_dc_w = max(0, e3dc_dc_pv_w - wr_limit_w)
        e3dc_ac_available_w = min(
            max(0, e3dc_dc_pv_w - battery_must_dc_w),
            wr_limit_w,
        )
    else:
        battery_must_dc_w = 0
        e3dc_ac_available_w = e3dc_dc_pv_w

    # Der externe AC-Erzeuger belastet nicht die E3DC-DC-/WR-Grenze, gehört
    # aber weiterhin vollständig in die PCC-/Einspeisebilanz.
    ac_available_w = e3dc_ac_available_w + external_ac_w

    total_ac_load_w = fixed_load_w + wb_w
    export_potential_w = max(0, ac_available_w - total_ac_load_w)
    battery_must_ac_w = (
        max(0, export_potential_w - feed_limit_w)
        if feed_limit_w > 0
        else 0
    )
    battery_must_total_w = battery_must_dc_w + battery_must_ac_w
    battery_room_w = 0 if soc >= 100.0 else max_charge_w
    battery_must_real_w = min(battery_must_total_w, battery_room_w)

    dc_real_w = min(battery_must_dc_w, battery_room_w)
    remaining_after_dc_w = max(0, battery_room_w - dc_real_w)
    ac_real_w = min(battery_must_ac_w, remaining_after_dc_w)
    loss_dc_w = max(0, battery_must_dc_w - dc_real_w)
    loss_ac_w = max(0, battery_must_ac_w - ac_real_w)

    wallbox_budget_total_w = max(0, ac_available_w - fixed_load_w - battery_must_ac_w)
    wallbox_budget_increase_w = max(0, wallbox_budget_total_w - wb_w)

    return {
        "pv_total_w": pv_total_w,
        "e3dc_dc_pv_w": e3dc_dc_pv_w,
        "e3dc_ac_available_w": e3dc_ac_available_w,
        "external_ac_pv_w": external_ac_w,
        "external_ac_pv_trusted": external_trusted,
        "external_ac_pv_source": external_source,
        "ac_available_w": ac_available_w,
        "battery_must_dc_w": battery_must_dc_w,
        "battery_must_ac_w": battery_must_ac_w,
        "battery_must_real_w": battery_must_real_w,
        "wallbox_budget_total_w": wallbox_budget_total_w,
        "wallbox_budget_increase_w": wallbox_budget_increase_w,
        "curtailment_loss_w": loss_dc_w + loss_ac_w,
        "export_limit_w": feed_limit_w,
        "wr_max_ac_w": wr_limit_w,
    }


def _meter_balance_home_w(pv_w: Any, grid_w: Any, bat_w: Any) -> int:
    return int(_safe_int(pv_w, 0) + _safe_int(grid_w, 0) - _safe_int(bat_w, 0))


def _meter_balance_plausible(
    pv_w: Any,
    grid_w: Any,
    bat_w: Any,
    home_w: Any,
) -> Dict[str, Any]:
    pv = max(0, _safe_int(pv_w, 0))
    grid = _safe_int(grid_w, 0)
    bat = _safe_int(bat_w, 0)
    home = max(0, _safe_int(home_w, 0))
    balance_home_w = _meter_balance_home_w(pv, grid, bat)
    if balance_home_w >= 0:
        balance_error_w = abs(balance_home_w - home)
    else:
        balance_error_w = abs(balance_home_w) + home
    error_limit_w = max(1800, int(round(pv * 0.25)))
    relevant = bool(pv > 1000 and (abs(grid) > 1000 or abs(bat) > 500))
    plausible = not (
        relevant
        and (
            balance_home_w < -500
            or balance_error_w > error_limit_w
        )
    )
    return {
        "home_balance_w": balance_home_w,
        "home_balance_error_w": balance_error_w,
        "home_balance_error_limit_w": error_limit_w,
        "home_balance_plausible": plausible,
    }


def _normalize_wb_mode(value: Any) -> int:
    raw = _safe_int(value, 0)
    if raw == 0:
        return 0
    if raw in (1, 2):
        return 2
    if raw in (3, 6):
        return 3
    if raw in (4, 9, 10):
        return 4
    if raw in (5, 11):
        return 5
    return 2


def _wallbox_configured(cfg: Dict[str, Any], wb_id: int) -> bool:
    key = "wb_native_type2" if int(wb_id) == 2 else "wb_native_type"
    wb_type = str(cfg.get(key, "") or "").strip().lower()
    if int(wb_id) == 2 and wb_type == "":
        return False
    return bool(wb_type and wb_type not in DISABLED_WALLBOX_TYPES)


def _runtime_wallbox_topology_ids(
    wb_intent: Dict[str, Any],
    now_s: float,
    max_age_s: float = 60.0,
) -> Tuple[set, set, bool]:
    """Liest ausschließlich einen frischen Managervertrag für Legacy-WB2."""

    if not isinstance(wb_intent, dict):
        return set(), set(), False
    topology = wb_intent.get("wallbox_runtime_topology")
    if (
        wb_intent.get("schema_version") != "wallbox_storage_intent_v2"
        or wb_intent.get("source") != "wallbox_manager"
        or not isinstance(topology, dict)
        or topology.get("schema_version") != "wallbox_runtime_topology_v1"
        or topology.get("valid") is not True
    ):
        return set(), set(), False
    try:
        intent_ts = float(wb_intent.get("ts", 0.0) or 0.0)
        manager_ts = float(topology.get("manager_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        return set(), set(), False
    intent_age_s = float(now_s) - intent_ts
    manager_age_s = float(now_s) - manager_ts
    if not (
        intent_ts > 0.0
        and manager_ts > 0.0
        and -5.0 <= intent_age_s <= float(max_age_s)
        and -5.0 <= manager_age_s <= float(max_age_s)
        and abs(intent_ts - manager_ts) <= 1.0
    ):
        return set(), set(), False

    def _strict_ids(value):
        if not isinstance(value, list):
            return None
        ids = []
        for raw in value:
            if isinstance(raw, bool):
                return None
            try:
                wb_id = int(raw)
            except (TypeError, ValueError):
                return None
            if wb_id not in (1, 2) or wb_id in ids:
                return None
            ids.append(wb_id)
        return set(ids)

    configured = _strict_ids(topology.get("configured_wb_ids"))
    active = _strict_ids(topology.get("active_mode_wb_ids"))
    runtime_wb2 = topology.get("runtime_wb2")
    if (
        configured is None
        or active is None
        or not active.issubset(configured)
        or not {1, 2}.issubset(configured)
        or not isinstance(runtime_wb2, dict)
        or runtime_wb2.get("schema_version") != "wallbox_runtime_wb2_v1"
        or runtime_wb2.get("valid") is not True
        or runtime_wb2.get("status_confirmed") is not True
        or runtime_wb2.get("physical_output_allowed") is not True
        or str(runtime_wb2.get("source") or "")
        != "manager_simpleapi_direct"
    ):
        return set(), set(), False
    try:
        cp_id = int(runtime_wb2.get("cp_id", 0) or 0)
        peer_cp_id = int(runtime_wb2.get("peer_cp_id", 0) or 0)
        detected_at = float(runtime_wb2.get("detected_at", 0.0) or 0.0)
        confirmed_ts = float(
            runtime_wb2.get("status_confirmed_ts", 0.0) or 0.0
        )
    except (TypeError, ValueError):
        return set(), set(), False
    output_identity = str(
        runtime_wb2.get("physical_output_identity") or ""
    )
    peer_output_identity = str(
        runtime_wb2.get("peer_physical_output_identity") or ""
    )
    if not (
        cp_id > 0
        and peer_cp_id > 0
        and cp_id != peer_cp_id
        and detected_at > 0.0
        and detected_at <= manager_ts + 5.0
        and confirmed_ts >= detected_at
        and confirmed_ts <= manager_ts + 5.0
        and str(runtime_wb2.get("controller_identity") or "")
        and str(runtime_wb2.get("endpoint_kind") or "")
        and output_identity
        and peer_output_identity
        and output_identity != peer_output_identity
    ):
        return set(), set(), False
    return {2}, ({2} if 2 in active else set()), True


def _read_json(path: str, max_age_s: Optional[float] = None) -> Dict[str, Any]:
    try:
        if max_age_s is not None and time.time() - os.path.getmtime(path) > max_age_s:
            return {}
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_cfg() -> Dict[str, Any]:
    raw = _read_json(V4_CFG)
    cfg: Dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                cfg[str(sub_key).lower()] = sub_value
        else:
            cfg[str(key).lower()] = value
    return cfg


def _atomic_write(path: str, payload: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _atomic_write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")
    os.replace(tmp, path)


def _append_history(path: str, payload: Dict[str, Any], max_lines: int = 720) -> None:
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        if os.path.getsize(path) > 768 * 1024:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[-max(20, max_lines):]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.writelines(lines)
            os.replace(tmp, path)
    except Exception:
        pass


def _current_curve_soc(
    plan: Dict[str, Any],
    now_ms: Optional[float] = None,
    allow_before_start: bool = False,
    timeline_key: str = "target_timeline",
) -> Optional[float]:
    now = time.time() * 1000.0 if now_ms is None else float(now_ms)
    timeline = plan.get(timeline_key)
    if timeline_key == "target_timeline":
        timeline = timeline or plan.get("timeline") or []
    else:
        timeline = timeline or []
    if not isinstance(timeline, list) or not timeline:
        return None
    points = []
    for slot in timeline:
        if not isinstance(slot, dict):
            continue
        ts = _safe_float(slot.get("ts"), 0.0)
        soc = slot.get("soc", slot.get("target_soc"))
        if ts <= 0 or soc is None:
            continue
        points.append((ts, max(0.0, min(100.0, _safe_float(soc, 0.0)))))
    if not points:
        return None
    points.sort(key=lambda item: item[0])
    if now < points[0][0] and not allow_before_start:
        return None
    if now >= points[-1][0]:
        return points[-1][1]
    for idx in range(1, len(points)):
        prev_ts, prev_soc = points[idx - 1]
        next_ts, next_soc = points[idx]
        if now <= next_ts:
            span = max(1.0, next_ts - prev_ts)
            ratio = max(0.0, min(1.0, (now - prev_ts) / span))
            return max(0.0, min(100.0, prev_soc + (next_soc - prev_soc) * ratio))
    return points[-1][1]


def _adaptive_curve_context(
    plan: Dict[str, Any],
    now_ms: float,
    soc: float,
    allow_before_start: bool = False,
) -> Dict[str, Any]:
    floor_soc = _current_curve_soc(
        plan,
        now_ms,
        allow_before_start=allow_before_start,
        timeline_key="soc_min_curve",
    )
    if floor_soc is None:
        floor_soc = _current_curve_soc(plan, now_ms, allow_before_start=allow_before_start)
    ceiling_soc = _current_curve_soc(
        plan,
        now_ms,
        allow_before_start=allow_before_start,
        timeline_key="soc_ceiling_curve",
    )
    active = bool(floor_soc is not None and ceiling_soc is not None)
    if active and ceiling_soc is not None and floor_soc is not None and ceiling_soc < floor_soc:
        ceiling_soc = floor_soc

    control_soc = floor_soc
    relation = "no_curve"
    if floor_soc is not None:
        relation = "below_floor"
        if active and ceiling_soc is not None:
            if soc > ceiling_soc:
                control_soc = ceiling_soc
                relation = "above_ceiling"
            elif soc >= floor_soc:
                control_soc = soc
                relation = "inside_band"
            else:
                control_soc = floor_soc
        else:
            control_soc = floor_soc

    return {
        "active": active,
        "curve_soc": control_soc,
        "floor_soc": floor_soc,
        "ceiling_soc": ceiling_soc,
        "relation": relation,
    }


def _first_curve_point(plan: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    timeline = plan.get("target_timeline") or plan.get("timeline") or []
    if not isinstance(timeline, list) or not timeline:
        return None, None
    points = []
    for slot in timeline:
        if not isinstance(slot, dict):
            continue
        ts = _safe_float(slot.get("ts"), 0.0)
        soc = slot.get("soc", slot.get("target_soc"))
        if ts <= 0 or soc is None:
            continue
        points.append((ts, max(0.0, min(100.0, _safe_float(soc, 0.0)))))
    if not points:
        return None, None
    points.sort(key=lambda item: item[0])
    return points[0]


def _mode_name(mode: int) -> str:
    return {
        MODE_AUTO: "AUTO",
        MODE_IDLE: "IDLE",
        MODE_DISCH: "DISCH",
        MODE_CHRG: "CHRG",
        MODE_GRID: "GRID",
    }.get(int(mode), "UNKNOWN")


def _decision_text(decision: Dict[str, Any]) -> str:
    state = str(decision.get("state") or "unknown")
    mode = _safe_int(decision.get("mode"), -1)
    val = max(0, _safe_int(decision.get("val"), 0))
    return f"{state}/{_mode_name(mode)} {val}W"


def _state_default_mode(state: str, fallback: int = MODE_AUTO) -> int:
    return _safe_int(STORAGE_STATE_DEFAULT_MODES.get(str(state), fallback), fallback)


def _state_default_value(state: str, mode: int, max_charge_w: int, fallback: int = 0) -> int:
    if mode == MODE_AUTO:
        return max(300, int(max_charge_w))
    if mode == MODE_CHRG:
        return max(300, int(fallback or 300))
    if mode == MODE_DISCH:
        return max(300, int(fallback or 300))
    if mode == MODE_GRID:
        return max(300, int(fallback or 300))
    return max(0, int(fallback or 0))


def _score_add(reasons: List[Dict[str, Any]], points: float, reason: str) -> float:
    if abs(points) >= 0.05:
        reasons.append({"points": round(points, 2), "reason": reason})
    return points


def _candidate_score(
    decision: Dict[str, Any],
    inputs: Dict[str, Any],
    cfg: Dict[str, Any],
    previous_state: str = "",
) -> Dict[str, Any]:
    """Bewertet eine Reglerempfehlung vorsichtig gegen aktuelle Messziele.

    Positiv heisst: die Aktion ist passender fuer Netzpunkt, Ladekurve und
    Verbraucherprioritaet. Das ist keine Wahrheitssimulation, sondern ein
    robuster Hinweisgeber fuer den Live-Vergleich.
    """
    mode = _safe_int(decision.get("mode"), -1)
    state = str(decision.get("state") or "")
    val = max(0, _safe_int(decision.get("val"), 0))
    reasons: List[Dict[str, Any]] = []
    score = 0.0

    soc = _safe_float(inputs.get("soc"), 0.0)
    curve_soc_raw = inputs.get("curve_soc")
    curve_soc = None if curve_soc_raw is None else _safe_float(curve_soc_raw, 0.0)
    curve_delta = None if curve_soc is None else soc - curve_soc
    tolerance = max(0.5, _safe_float(cfg.get("storage_parallel_curve_tolerance_pct"), 3.0))
    grid_ema_w = _safe_float(inputs.get("grid_ema_w"), 0.0)
    wallbox_w = _safe_float(inputs.get("wallbox_w"), 0.0)
    wb_active = bool(inputs.get("wb_active"))
    wb_budget_w = max(0.0, _safe_float(inputs.get("wb_budget_w"), 0.0))
    i_fc_w = max(0.0, _safe_float(inputs.get("iFc_w"), 0.0))
    i_min_lade_w = max(0.0, _safe_float(inputs.get("iMinLade_w"), 0.0))
    pv_after_fixed_w = max(0.0, _safe_float(inputs.get("pv_after_fixed_w"), 0.0))
    charge_need = bool(i_fc_w >= 250 or i_min_lade_w >= 250 or (curve_delta is not None and curve_delta < -tolerance))

    if grid_ema_w > 600:
        if mode == MODE_DISCH:
            score += _score_add(reasons, 2.0, "Netzbezug: Entladen entlastet den Uebergabepunkt")
        elif mode == MODE_AUTO:
            score += _score_add(reasons, 1.2, "Netzbezug: AUTO gibt dem E3DC Entladung frei")
        elif mode == MODE_IDLE:
            score += _score_add(reasons, -1.1, "Netzbezug: IDLE kann Batteriestuetze blockieren")
        elif mode in (MODE_CHRG, MODE_GRID):
            score += _score_add(reasons, -2.4, "Netzbezug: Laden verschaerft den Bezug")
    elif grid_ema_w < -800:
        if mode in (MODE_CHRG, MODE_GRID):
            score += _score_add(
                reasons,
                1.4 if charge_need else 0.4,
                "Einspeisung: Laden nutzt Ueberschuss" if charge_need else "Einspeisung: Laden ist moeglich, aber Kurvenbedarf klein",
            )
        elif mode == MODE_AUTO:
            score += _score_add(reasons, 1.0, "Einspeisung: AUTO laesst den E3DC aufnehmen")
        elif mode == MODE_DISCH:
            score += _score_add(reasons, -2.0, "Einspeisung: zusaetzliches Entladen waere unguenstig")

    if "abregel" in state.lower():
        if mode in (MODE_CHRG, MODE_GRID) and grid_ema_w < -1000:
            score += _score_add(reasons, 2.4, "Abregelschutz: aktive Ladefreigabe schuetzt vor Abregelung")
        elif mode == MODE_AUTO and grid_ema_w < -1000:
            score += _score_add(reasons, 0.6, "Abregelschutz: AUTO kann helfen, ist aber weniger verbindlich")

    if curve_delta is not None:
        if state == "parallel_night_floor_hold" and curve_delta < 0.0:
            if mode == MODE_IDLE:
                score += _score_add(reasons, 1.2, "Nachtreserve: IDLE schuetzt die Untergrenze")
            elif mode == MODE_AUTO:
                score += _score_add(reasons, -1.2, "Nachtreserve: AUTO kann die Untergrenze weiter entladen")
        if curve_delta < -tolerance:
            if mode in (MODE_CHRG, MODE_GRID):
                score += _score_add(reasons, 1.6, "Unter Ladekurve: aktive Ladung holt auf")
            elif mode == MODE_AUTO:
                score += _score_add(reasons, 0.9, "Unter Ladekurve: AUTO kann aufholen")
            elif mode in (MODE_IDLE, MODE_DISCH):
                score += _score_add(reasons, -1.5, "Unter Ladekurve: Halten/Entladen gefaehrdet Ziel")
        elif curve_delta > tolerance:
            if mode == MODE_AUTO:
                score += _score_add(reasons, 1.0, "Ueber Ladekurve: AUTO vermeidet unnoetige Bremse")
            elif mode == MODE_IDLE:
                score += _score_add(reasons, 0.6, "Ueber Ladekurve: IDLE kann Speicher beruhigen")
            elif mode == MODE_DISCH and grid_ema_w >= -300:
                score += _score_add(reasons, 0.7, "Ueber Ladekurve: Entladen kann Richtung Kurve helfen")
            elif mode in (MODE_CHRG, MODE_GRID) and pv_after_fixed_w < val + 300:
                score += _score_add(reasons, -1.3, "Ueber Ladekurve: zusaetzliches Laden wirkt zu frueh")

    if wb_active or wallbox_w > 250:
        if mode == MODE_AUTO:
            score += _score_add(reasons, 1.3, "Wallbox aktiv: AUTO laesst E3DC und WB ruhiger zusammenarbeiten")
        elif mode == MODE_IDLE:
            score += _score_add(reasons, -1.2, "Wallbox aktiv: IDLE kann Netzbezug provozieren")
        elif mode in (MODE_CHRG, MODE_GRID):
            score += _score_add(reasons, -0.7, "Wallbox aktiv: paralleles Speicherladen konkurriert mit WB")
    elif wb_budget_w <= 0 and wallbox_w <= 50:
        if mode in (MODE_CHRG, MODE_GRID):
            score += _score_add(reasons, -0.8, "Kein WB-Budget: Speicherladen nur bei klarer Kurvenanforderung sinnvoll")
        elif mode == MODE_AUTO:
            score += _score_add(reasons, 0.3, "Kein WB-Budget: AUTO ist neutral und robust")

    if previous_state and state and state != previous_state:
        score += _score_add(reasons, -0.25, "Schaltunruhe: Zustandswechsel gegenueber letztem Shadow-Vergleich")
    if mode in (MODE_CHRG, MODE_DISCH, MODE_GRID) and val < 300:
        score += _score_add(reasons, -0.4, "Kleinstwert: Regelmodus ohne wirksame Leistung")

    return {
        "score": round(score, 2),
        "reasons": reasons[:6],
    }


def _window_leader(avg_score: float) -> str:
    if avg_score >= 0.35:
        return "parallel"
    if avg_score <= -0.35:
        return "active"
    return "neutral"


def _fmt_w(value: Any) -> str:
    watts = _safe_float(value, 0.0)
    sign = "+" if watts > 0 else ""
    if abs(watts) >= 1000:
        return f"{sign}{watts / 1000.0:.2f} kW"
    return f"{sign}{watts:.0f} W"


def _fmt_soc(value: Any) -> str:
    if value is None:
        return "-"
    return f"{_safe_float(value, 0.0):.1f}%"


def _reason_texts(score_payload: Dict[str, Any], limit: int = 2) -> List[str]:
    reasons = score_payload.get("reasons") if isinstance(score_payload.get("reasons"), list) else []
    reasons = sorted(
        [item for item in reasons if isinstance(item, dict)],
        key=lambda item: abs(_safe_float(item.get("points"), 0.0)),
        reverse=True,
    )
    texts: List[str] = []
    for item in reasons:
        text = str(item.get("reason") or "").strip()
        if text and text not in texts:
            texts.append(text)
        if len(texts) >= limit:
            break
    return texts


def _brief_status_text(leader: str) -> str:
    if leader == "parallel":
        return "Shadow wirkt passender"
    if leader == "active":
        return "Aktive Regelung wirkt passender"
    return "Gleichstand"


def _build_brief(report: Dict[str, Any]) -> Dict[str, Any]:
    current = report.get("current") if isinstance(report.get("current"), dict) else {}
    windows = report.get("windows") if isinstance(report.get("windows"), dict) else {}
    inputs = current.get("inputs") if isinstance(current.get("inputs"), dict) else {}
    leader = str(current.get("leader") or "neutral")
    active = str(current.get("active") or "-")
    parallel = str(current.get("parallel") or "-")
    action_diff = bool(current.get("action_diff"))
    state_diff_only = bool(current.get("state_diff_only"))

    if action_diff:
        summary = f"{_brief_status_text(leader)}: aktiv {active}, Shadow {parallel}."
    elif state_diff_only:
        summary = f"Gleichstand: beide empfehlen dieselbe Aktion ({active}); nur der Zustandsname unterscheidet sich."
    else:
        summary = f"Gleichstand: beide empfehlen {active}."

    if leader == "parallel":
        why = _reason_texts(current.get("parallel_score", {}), 3)
    elif leader == "active":
        why = _reason_texts(current.get("active_score", {}), 3)
    else:
        why = _reason_texts(current.get("active_score", {}), 2) or _reason_texts(current.get("parallel_score", {}), 2)
    if not why:
        why = ["Keine eindeutige Bewertung; Messlage neutral."]

    window_brief = {}
    for name in ("5m", "15m", "60m"):
        win = windows.get(name) if isinstance(windows.get(name), dict) else {}
        window_brief[name] = {
            "leader": win.get("leader", "neutral"),
            "score": win.get("avg_score", 0),
        }

    numbers = {
        "soc": _fmt_soc(inputs.get("soc")),
        "curve": _fmt_soc(inputs.get("curve_soc")),
        "grid": _fmt_w(inputs.get("grid_ema_w")),
        "wallbox": _fmt_w(inputs.get("wallbox_w")),
        "wb_budget": _fmt_w(inputs.get("wb_budget_w")),
    }
    text = (
        f"{time.strftime('%H:%M:%S', time.localtime(_safe_int(report.get('ts'), int(time.time()))))} | "
        f"{summary} Fenster: 5m {window_brief['5m']['leader']}, "
        f"15m {window_brief['15m']['leader']}, 60m {window_brief['60m']['leader']}. "
        f"SoC {numbers['soc']} / Kurve {numbers['curve']}, Netz {numbers['grid']}, "
        f"WB {numbers['wallbox']}, WB-Budget {numbers['wb_budget']}. "
        f"Grund: {'; '.join(why[:2])}"
    )
    return {
        "ts": report.get("ts"),
        "status": leader,
        "action_diff": action_diff,
        "state_diff_only": state_diff_only,
        "log_relevant": bool(current.get("log_relevant")),
        "summary": summary,
        "active": active,
        "parallel": parallel,
        "why": why,
        "windows": window_brief,
        "numbers": numbers,
        "text": text,
    }


def _update_diff_windows(
    previous: Dict[str, Any],
    now: int,
    delta_score: float,
    different: bool,
) -> Dict[str, Any]:
    prev_windows = previous.get("windows") if isinstance(previous.get("windows"), dict) else {}
    windows: Dict[str, Any] = {}
    for name, seconds in (("5m", 300.0), ("15m", 900.0), ("60m", 3600.0)):
        prev = prev_windows.get(name) if isinstance(prev_windows.get(name), dict) else {}
        last_ts = _safe_float(prev.get("ts"), now)
        elapsed = max(0.0, min(seconds * 4.0, now - last_ts))
        decay = math.exp(-elapsed / seconds) if elapsed > 0 else 1.0
        score_sum = _safe_float(prev.get("score_sum"), 0.0) * decay + float(delta_score)
        samples = _safe_float(prev.get("samples"), 0.0) * decay + 1.0
        diff_samples = _safe_float(prev.get("diff_samples"), 0.0) * decay + (1.0 if different else 0.0)
        parallel_better = _safe_float(prev.get("parallel_better"), 0.0) * decay + (1.0 if delta_score >= 0.75 else 0.0)
        active_better = _safe_float(prev.get("active_better"), 0.0) * decay + (1.0 if delta_score <= -0.75 else 0.0)
        avg_score = score_sum / max(1.0, samples)
        windows[name] = {
            "ts": now,
            "score_sum": round(score_sum, 3),
            "samples": round(samples, 3),
            "diff_samples": round(diff_samples, 3),
            "parallel_better": round(parallel_better, 3),
            "active_better": round(active_better, 3),
            "avg_score": round(avg_score, 3),
            "leader": _window_leader(avg_score),
        }
    return windows


def _build_diff_report(
    payload: Dict[str, Any],
    cfg: Dict[str, Any],
    previous_diff: Dict[str, Any],
) -> Dict[str, Any]:
    now = int(payload.get("ts") or time.time())
    active = payload.get("active") if isinstance(payload.get("active"), dict) else {}
    parallel = payload.get("parallel") if isinstance(payload.get("parallel"), dict) else {}
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    diff = payload.get("diff") if isinstance(payload.get("diff"), dict) else {}
    min_delta_w = max(50, _safe_int(cfg.get("storage_parallel_diff_min_w"), 250))
    val_diff_w = _safe_int(diff.get("val_diff_w"), 0)
    action_diff = bool(diff.get("mode_diff") or abs(val_diff_w) >= min_delta_w)
    state_diff = bool(diff.get("state_diff"))
    different = bool(state_diff or action_diff)

    prev_current = previous_diff.get("current") if isinstance(previous_diff.get("current"), dict) else {}
    active_score = _candidate_score(active, inputs, cfg, str(prev_current.get("active_state") or ""))
    parallel_score = _candidate_score(parallel, inputs, cfg, str(prev_current.get("parallel_state") or ""))
    delta_score = round(_safe_float(parallel_score.get("score"), 0.0) - _safe_float(active_score.get("score"), 0.0), 2)
    if delta_score >= 0.75:
        leader = "parallel"
    elif delta_score <= -0.75:
        leader = "active"
    else:
        leader = "neutral"

    signature = "|".join([
        str(active.get("state") or ""),
        str(_safe_int(active.get("mode"), -1)),
        str(int(round(_safe_float(active.get("val"), 0.0) / 100.0) * 100)),
        str(parallel.get("state") or ""),
        str(_safe_int(parallel.get("mode"), -1)),
        str(int(round(_safe_float(parallel.get("val"), 0.0) / 100.0) * 100)),
        leader,
    ])
    windows = _update_diff_windows(previous_diff, now, delta_score if different else 0.0, different)
    current = {
        "ts": now,
        "different": different,
        "action_diff": action_diff,
        "state_diff_only": bool(state_diff and not action_diff),
        "log_relevant": bool(action_diff or abs(delta_score) >= 0.75),
        "signature": signature,
        "leader": leader if different else "neutral",
        "delta_score": delta_score if different else 0.0,
        "active": _decision_text(active),
        "parallel": _decision_text(parallel),
        "active_state": str(active.get("state") or ""),
        "parallel_state": str(parallel.get("state") or ""),
        "val_diff_w": val_diff_w,
        "inputs": {
            "soc": inputs.get("soc"),
            "curve_soc": inputs.get("curve_soc"),
            "grid_ema_w": inputs.get("grid_ema_w"),
            "wallbox_w": inputs.get("wallbox_w"),
            "wb_active": inputs.get("wb_active"),
            "wb_budget_w": inputs.get("wb_budget_w"),
            "iFc_w": inputs.get("iFc_w"),
            "iMinLade_w": inputs.get("iMinLade_w"),
        },
        "active_score": active_score,
        "parallel_score": parallel_score,
    }
    return {
        "ts": now,
        "service": "storage_parallel_diff",
        "shadow_only": True,
        "current": current,
        "windows": windows,
        "last_log_ts": _safe_int(previous_diff.get("last_log_ts"), 0),
        "last_log_signature": str(previous_diff.get("last_log_signature") or ""),
    }


def _maybe_log_diff_report(report: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    current = report.get("current") if isinstance(report.get("current"), dict) else {}
    if not current.get("different") or not current.get("log_relevant"):
        return report
    now = _safe_int(report.get("ts"), int(time.time()))
    min_interval_s = max(15, _safe_int(cfg.get("storage_parallel_diff_log_interval_s"), 60))
    signature = str(current.get("signature") or "")
    last_signature = str(report.get("last_log_signature") or "")
    last_log_ts = _safe_int(report.get("last_log_ts"), 0)
    if signature == last_signature and now - last_log_ts < min_interval_s:
        return report

    windows = report.get("windows") if isinstance(report.get("windows"), dict) else {}
    score_text = " ".join(
        f"{name}={win.get('leader','neutral')}({win.get('avg_score',0)})"
        for name, win in windows.items() if isinstance(win, dict)
    )
    inputs = current.get("inputs") if isinstance(current.get("inputs"), dict) else {}
    DIFF_LOG.info(
        "SHADOW-DIFF active=%s parallel=%s leader=%s delta=%s grid=%sW soc=%s curve=%s wb=%sW budget=%sW windows=%s",
        current.get("active"),
        current.get("parallel"),
        current.get("leader"),
        current.get("delta_score"),
        inputs.get("grid_ema_w"),
        inputs.get("soc"),
        inputs.get("curve_soc"),
        inputs.get("wallbox_w"),
        inputs.get("wb_budget_w"),
        score_text,
    )
    report["last_log_ts"] = now
    report["last_log_signature"] = signature
    return report


@dataclass
class ParallelDecision:
    state: str
    mode: int
    val: int
    reason: str
    priority: str

    def as_payload(self) -> Dict[str, Any]:
        data = asdict(self)
        data["mode_name"] = _mode_name(self.mode)
        return data


class ParallelStorageRegulator:
    """Erste Stufe des neuen Storage-Reglers im Shadow-Modus."""

    PASSTHROUGH_STATES = (
        "manual_override",
        "pre_discharge",
        "tl_autodump",
    )

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}
        self.max_charge_w = _effective_max_charge_w(self.cfg)
        self.max_discharge_w = _effective_max_discharge_w(self.cfg, fallback_charge_w=self.max_charge_w)
        self.target_soc = max(5.0, min(100.0, _safe_float(self.cfg.get("storage_target_soc"), 90.0)))
        self.curve_tolerance_pct = max(0.2, _safe_float(
            self.cfg.get("storage_parallel_curve_tolerance_pct"),
            _safe_float(self.cfg.get("tl_tolerance_pct"), 3.0),
        ))
        self.grid_limit_w = max(50, _safe_int(self.cfg.get("tl_grid_limit_w"), 100))
        self.grid_relief_enter_w = max(300, _safe_int(
            self.cfg.get("storage_parallel_grid_relief_enter_w"),
            max(300, self.grid_limit_w * 3),
        ))
        self.wb_hold_s = max(30, _safe_int(self.cfg.get("storage_parallel_wb_hold_s"), 300))
        self.price_house_discharge_w = 0
        self.price_house_discharge_step_up_w = max(100, _safe_int(
            self.cfg.get("storage_parallel_price_house_step_up_w"), 300
        ))
        self.price_house_discharge_step_down_w = max(100, _safe_int(
            self.cfg.get("storage_parallel_price_house_step_down_w"), 600
        ))
        self.curve_charge_enter_w = max(300, _safe_int(
            self.cfg.get("storage_parallel_curve_charge_enter_w"), 300
        ))
        self.curve_charge_keep_w = max(50, min(
            self.curve_charge_enter_w,
            _safe_int(self.cfg.get("storage_parallel_curve_charge_keep_w"), 120),
        ))
        self.curve_charge_reenter_w = max(
            self.curve_charge_enter_w,
            _safe_int(
                self.cfg.get("storage_parallel_curve_charge_reenter_w"),
                self.curve_charge_enter_w + 300,
            ),
        )
        curve_charge_servo_mode_raw = str(
            self.cfg.get("storage_curve_charge_servo_mode", "dynamic") or "dynamic"
        ).strip().lower().replace("-", "_").replace(" ", "_")
        self.curve_charge_servo_enabled = curve_charge_servo_mode_raw in {
            "steady",
            "ruhig",
            "servo",
            "kurven_servo",
            "curve_servo",
            "constant",
            "1",
            "true",
            "yes",
            "on",
        }
        self.curve_charge_servo_mode = "steady" if self.curve_charge_servo_enabled else "dynamic"
        self.curve_charge_servo_min_w = max(
            self.curve_charge_keep_w,
            _safe_int(
                self.cfg.get("storage_curve_charge_servo_min_w"),
                self.curve_charge_enter_w,
            ),
        )
        self.curve_charge_servo_deadband_w = max(
            50,
            _safe_int(self.cfg.get("storage_curve_charge_servo_deadband_w"), 250),
        )
        self.curve_charge_servo_step_up_w = max(
            50,
            _safe_int(self.cfg.get("storage_curve_charge_servo_step_up_w"), 250),
        )
        self.curve_charge_servo_step_down_w = max(
            50,
            _safe_int(self.cfg.get("storage_curve_charge_servo_step_down_w"), 350),
        )
        self.curve_charge_servo_max_age_s = max(
            0,
            _safe_int(self.cfg.get("storage_curve_charge_servo_max_age_s"), 3600),
        )
        self.curve_edge_soft_hold_s = max(30, _safe_int(
            self.cfg.get("storage_parallel_curve_edge_soft_hold_s"),
            300,
        ))
        self.curve_edge_soft_factor = max(0.1, min(
            1.0,
            _safe_float(self.cfg.get("storage_parallel_curve_edge_soft_factor"), 0.65),
        ))
        self.pre_curve_ifc_start_w = max(
            self.curve_charge_enter_w,
            _safe_int(
                self.cfg.get("storage_parallel_pre_curve_ifc_start_w"),
                max(1800, int(self.max_charge_w * 0.25)),
            ),
        )
        self.abregel_min_charge_w = max(0, _safe_int(
            self.cfg.get("abregel_min_charge_w"), 300
        ))
        self.curve_guard_enter_below_pct = max(0.0, _safe_float(
            self.cfg.get("storage_parallel_curve_guard_enter_below_pct"), 1.0
        ))
        self.price_house_discharge_enter_w = max(300, _safe_int(
            self.cfg.get("storage_parallel_price_house_discharge_enter_w"), 300
        ))
        self.price_house_discharge_keep_w = max(50, min(
            self.price_house_discharge_enter_w,
            _safe_int(self.cfg.get("storage_parallel_price_house_discharge_keep_w"), 120),
        ))
        # Retired: night-floor IDLE caused avoidable grid import. A stale
        # previous state may still be logged, but it must no longer enter.
        self.night_floor_enabled = False
        self.night_floor_enter_pct = max(0.0, _safe_float(
            self.cfg.get("storage_parallel_night_floor_enter_pct"), 0.1
        ))
        self.night_floor_keep_pct = max(
            self.night_floor_enter_pct,
            _safe_float(self.cfg.get("storage_parallel_night_floor_keep_pct"), 0.6),
        )
        self.curve_auto_hold_exit_pct = max(0.5, min(
            self.curve_tolerance_pct,
            _safe_float(
                self.cfg.get("storage_parallel_curve_auto_hold_exit_pct"),
                0.5,
            ),
        ))
        self.curve_auto_hold_release_below_pct = max(0.0, min(
            self.curve_guard_enter_below_pct,
            self.curve_tolerance_pct,
            _safe_float(
                self.cfg.get("storage_parallel_curve_auto_hold_release_below_pct"),
                0.5,
            ),
        ))
        self.curve_charge_release_stabilize_s = max(
            0,
            _safe_int(self.cfg.get("storage_parallel_curve_charge_release_stabilize_s"), 600),
        )
        self.curve_charge_soc_step_hold_s = max(
            0,
            _safe_int(
                self.cfg.get("storage_parallel_curve_charge_soc_step_hold_s"),
                max(1800, self.curve_charge_release_stabilize_s),
            ),
        )
        self.curve_cap_enter_margin_w = max(100, _safe_int(
            self.cfg.get("storage_parallel_curve_cap_enter_margin_w"), 600
        ))
        self.curve_cap_keep_margin_w = max(0, min(
            self.curve_cap_enter_margin_w,
            _safe_int(self.cfg.get("storage_parallel_curve_cap_keep_margin_w"), 150),
        ))
        self.curve_cap_feed_buffer_w = max(0, _safe_int(
            self.cfg.get("abregel_puffer_w"),
            300,
        ))
        # Der native Pfad verwendet denselben fachlichen Austrittsvertrag wie
        # die bestehende Konfiguration. Bei älteren Konfigurationen ohne den
        # Hystereseschlüssel bleibt das bisherige AUTO-Band der konservative
        # Migrationswert; neue Installationen nutzen abregel_hysterese_w.
        curve_cap_release_hysteresis_default_w = (
            _safe_int(self.cfg.get("abregel_auto_band_w"), 1800)
            if "abregel_auto_band_w" in self.cfg
            else 2000
        )
        self.curve_cap_release_hysteresis_w = max(
            self.curve_cap_feed_buffer_w,
            _safe_int(
                self.cfg.get("abregel_hysterese_w"),
                curve_cap_release_hysteresis_default_w,
            ),
        )
        self.curve_cap_release_band_w = self.curve_cap_release_hysteresis_w
        self.curve_cap_release_grace_s = max(
            0.0,
            _safe_float(self.cfg.get("abregel_auto_grace_s"), 30.0),
        )
        self.curve_cap_feedback_band_w = max(
            self.curve_cap_release_band_w,
            _safe_int(
                self.cfg.get("storage_parallel_curve_cap_feedback_band_w"),
                self.curve_cap_release_band_w,
            ),
        )
        # Der Abregel-Puffer definiert den Zielabstand zum harten Einspeiselimit.
        # Die Ausloeseschwelle muss darunter liegen, sonst reagiert der Schutz
        # erst am harten Limit statt im Pufferband.
        default_curve_cap_trigger_w = min(100, max(50, self.curve_cap_feed_buffer_w))
        self.curve_cap_export_trigger_w = max(50, _safe_int(
            self.cfg.get("storage_parallel_curve_cap_export_trigger_w"),
            default_curve_cap_trigger_w,
        ))
        self.curve_cap_short_hold_s = max(0.0, min(
            15.0,
            _safe_float(
                self.cfg.get("storage_parallel_curve_cap_short_hold_s"),
                8.0,
            ),
        ))
        self.curve_cap_step_w = max(100, _safe_int(
            self.cfg.get("storage_parallel_curve_cap_step_w"), 500
        ))
        self.headroom_discharge_enable = _cfg_bool(
            self.cfg,
            "storage_headroom_discharge_enable",
            True,
        )
        self.headroom_discharge_enter_pct = max(0.3, _safe_float(
            self.cfg.get("storage_headroom_discharge_enter_pct"),
            1.0,
        ))
        self.headroom_discharge_keep_pct = max(0.1, min(
            self.headroom_discharge_enter_pct,
            _safe_float(self.cfg.get("storage_headroom_discharge_keep_pct"), 0.35),
        ))
        self.headroom_discharge_min_pv_w = max(0, _safe_int(
            self.cfg.get("storage_headroom_discharge_min_pv_w"),
            1200,
        ))
        self.headroom_discharge_min_pressure_wh = max(0.0, _safe_float(
            self.cfg.get("storage_headroom_discharge_min_pressure_wh"),
            700.0,
        ))
        self.headroom_discharge_min_w = max(300, _safe_int(
            self.cfg.get("storage_headroom_discharge_min_w"),
            500,
        ))
        self.headroom_discharge_max_w_cfg = max(
            self.headroom_discharge_min_w,
            _safe_int(self.cfg.get("storage_headroom_discharge_max_w"), 2500),
        )
        self.headroom_discharge_step_w = max(100, _safe_int(
            self.cfg.get("storage_headroom_discharge_step_w"),
            500,
        ))
        self.headroom_discharge_horizon_h = max(0.25, _safe_float(
            self.cfg.get("storage_headroom_discharge_horizon_h"),
            2.0,
        ))
        self.headroom_discharge_export_margin_w = max(
            self.curve_cap_feed_buffer_w,
            _safe_int(self.cfg.get("storage_headroom_discharge_export_margin_w"), 900),
        )
        self.headroom_discharge_import_guard_w = max(0, _safe_int(
            self.cfg.get("storage_headroom_discharge_import_guard_w"),
            150,
        ))
        self.headroom_discharge_daily_limit_pct = max(0.0, min(
            50.0,
            _safe_float(self.cfg.get("storage_headroom_discharge_daily_limit_pct"), 10.0),
        ))
        self.headroom_discharge_cooldown_s = max(
            0,
            _safe_int(self.cfg.get("storage_headroom_discharge_cooldown_min"), 10),
        ) * 60
        self.headroom_discharge_target_plateau_margin_pct = max(0.05, min(
            5.0,
            _safe_float(self.cfg.get("storage_headroom_discharge_target_plateau_margin_pct"), 0.3),
        ))
        self.headroom_discharge_energy_gap_s = max(15.0, min(
            300.0,
            _safe_float(self.cfg.get("storage_headroom_discharge_energy_gap_s"), 180.0),
        ))
        self.auto_hold_s = max(0, _safe_int(
            self.cfg.get("storage_parallel_auto_hold_s"), 90
        ))
        self.wb_auto_grid_abort_w = max(500, _safe_int(
            self.cfg.get("storage_parallel_wb_auto_grid_abort_w"), 2000
        ))

    def _apply_transition_table(
        self,
        decision: ParallelDecision,
        *,
        previous_state: str,
        previous_mode: int,
        previous_val: int,
        previous_age_s: float,
        grid_ema_w: int,
        forecast_curve_below_floor: bool,
        wb_owner_evidence_active: bool,
        trace: List[Dict[str, Any]],
    ) -> ParallelDecision:
        if not previous_state or previous_state == decision.state:
            return decision
        rule = STORAGE_TRANSITION_RULES.get(previous_state)
        if not rule:
            trace.append({
                "step": "transition_table",
                "previous": previous_state,
                "candidate": decision.state,
                "action": "pass_no_rule",
            })
            return decision

        strong_grid_override = bool(
            decision.state == "parallel_grid_relief_auto"
            and grid_ema_w > max(1500, self.grid_limit_w * 10)
        )
        wb_auto_grid_abort_override = bool(
            previous_state == "parallel_wb_auto"
            and decision.state != "parallel_wb_auto"
            and grid_ema_w >= self.wb_auto_grid_abort_w
        )
        wb_auto_owner_evidence_missing_override = bool(
            previous_state == "parallel_wb_auto"
            and decision.state != "parallel_wb_auto"
            and not wb_owner_evidence_active
        )
        curve_limit_override = bool(
            previous_state == "parallel_auto"
            and decision.state == "parallel_curve_charge"
            and previous_val > max(0, int(decision.val) + self.curve_charge_enter_w)
        )
        curve_auto_limit_reassert = bool(
            previous_state == "parallel_auto"
            and decision.priority == "curve"
            and decision.mode == MODE_AUTO
            and decision.state in (
                "parallel_curve_auto_hold",
                "parallel_curve_auto_no_surplus",
            )
        )
        pre_curve_ifc_override = bool(
            decision.priority == "curve_pressure"
            and decision.state == "parallel_curve_charge"
        )
        forecast_curve_floor_override = bool(
            forecast_curve_below_floor
            and decision.state in {
                "parallel_auto",
                "parallel_curve_auto_no_surplus",
                "parallel_curve_charge",
            }
            and decision.priority
            in {"default", "curve", "forecast_shortfall"}
        )
        hard_override = bool(
            decision.priority in STORAGE_HARD_OVERRIDE_PRIORITIES
            or decision.state in STORAGE_HARD_OVERRIDE_STATES
            or strong_grid_override
            or wb_auto_grid_abort_override
            or wb_auto_owner_evidence_missing_override
            or curve_limit_override
            or curve_auto_limit_reassert
            or pre_curve_ifc_override
            or forecast_curve_floor_override
        )
        allow = set(rule.get("allow") or ())
        min_hold_s = max(0, _safe_int(rule.get("min_hold_s"), 0))
        if hard_override:
            trace.append({
                "step": "transition_table",
                "previous": previous_state,
                "candidate": decision.state,
                "age_s": round(previous_age_s, 1),
                "min_hold_s": min_hold_s,
                "action": "hard_override",
            })
            return decision

        invalid = bool(allow and decision.state not in allow)
        too_early = bool(min_hold_s > 0 and previous_age_s < min_hold_s)
        if not invalid and not too_early:
            trace.append({
                "step": "transition_table",
                "previous": previous_state,
                "candidate": decision.state,
                "age_s": round(previous_age_s, 1),
                "min_hold_s": min_hold_s,
                "action": "pass",
            })
            return decision

        remaining_s = max(1, int(round(min_hold_s - previous_age_s))) if too_early else 0
        held_mode = previous_mode if previous_mode >= -1 else _state_default_mode(previous_state, MODE_AUTO)
        if previous_val <= 0:
            fallback_val = self.curve_charge_enter_w if held_mode == MODE_CHRG else 0
            held_val = _state_default_value(previous_state, held_mode, self.max_charge_w, fallback_val)
        else:
            held_val = previous_val
        if invalid:
            reason = (
                "Zustandswechsel gesperrt: %s -> %s ist nicht in der Storage-"
                "Transition-Tabelle definiert"
            ) % (previous_state, decision.state)
        else:
            reason = (
                "Zustandshaltezeit: %s bleibt noch %ds aktiv, bevor %s uebernehmen darf"
            ) % (previous_state, remaining_s, decision.state)
        trace.append({
            "step": "transition_table",
            "previous": previous_state,
            "candidate": decision.state,
            "age_s": round(previous_age_s, 1),
            "min_hold_s": min_hold_s,
            "action": "hold",
            "invalid": invalid,
            "remaining_s": remaining_s,
            "held_mode": _mode_name(held_mode),
            "held_val": held_val,
        })
        return ParallelDecision(previous_state, held_mode, held_val, reason, "transition_hold")

    def decide(
        self,
        active_state: Dict[str, Any],
        live: Dict[str, Any],
        plan: Dict[str, Any],
        wb_budget: Dict[str, Any],
        wb_intent: Dict[str, Any],
    ) -> Dict[str, Any]:
        trace: List[Dict[str, Any]] = []
        now_s = _safe_float(active_state.get("now_ts_s"), time.time())
        self.max_charge_w = _effective_max_charge_w(self.cfg, live)
        self.max_discharge_w = _effective_max_discharge_w(self.cfg, live, self.max_charge_w)

        active_state_name = str(active_state.get("storage_state") or active_state.get("state") or "unknown")
        active_mode = _safe_int(active_state.get("mode"), -1)
        active_val = max(0, _safe_int(active_state.get("val"), 0))
        soc = _safe_float(active_state.get("soc", live.get("SOC")), 0.0)
        pv_w = _safe_int(active_state.get("pv_w", live.get("PV_Power")), 0)
        grid_w = _safe_int(active_state.get("grid_w", live.get("Grid_Power")), 0)
        grid_ema_w = _safe_int(active_state.get("grid_ema_w", grid_w), grid_w)
        home_w = max(0, _safe_int(active_state.get("home_ema_w", live.get("Home_Power")), 0))
        bat_w = _safe_int(active_state.get("bat_w", live.get("Battery_Power")), 0)
        wallbox_w = abs(_safe_float(live.get("Wallbox_Power"), 0.0))
        wp_w = max(0, _safe_int(live.get("WP_Power", live.get("Heatpump_Power")), 0))
        i_fc_w = max(0, _safe_int(active_state.get("iFc_w"), 0))
        i_min_lade_w = max(0, _safe_int(active_state.get("iMinLade_w"), 0))
        previous_parallel_state = str(
            active_state.get("previous_parallel_state")
            or active_state.get("last_parallel_state")
            or ""
        )
        previous_parallel_mode = _safe_int(
            active_state.get("previous_parallel_mode"),
            _state_default_mode(previous_parallel_state, active_mode),
        )
        previous_parallel_val_default = active_val
        if (
            previous_parallel_state == "parallel_curve_charge_cap"
            and "previous_parallel_val" not in active_state
            and active_mode == MODE_AUTO
            and active_val >= max(0, self.max_charge_w - 50)
        ):
            previous_parallel_val_default = 0
        previous_parallel_val = max(
            0,
            _safe_int(active_state.get("previous_parallel_val"), previous_parallel_val_default),
        )
        previous_parallel_ts = _safe_float(active_state.get("previous_parallel_ts"), 0.0)
        previous_parallel_age_s = (
            max(0.0, now_s - previous_parallel_ts)
            if previous_parallel_ts > 0
            else 999999.0
        )
        previous_decision_ts = _safe_float(active_state.get("previous_state_ts"), previous_parallel_ts)
        previous_decision_age_s = (
            max(0.0, now_s - previous_decision_ts)
            if previous_decision_ts > 0
            else previous_parallel_age_s
        )
        previous_headroom_discharge = previous_parallel_state == "parallel_headroom_discharge"
        headroom_discharge_day = time.strftime("%Y-%m-%d", time.localtime(now_s))
        previous_headroom_day = str(active_state.get("headroom_discharge_day") or "")
        headroom_discharge_today_wh = max(
            0.0,
            _safe_float(active_state.get("headroom_discharge_today_wh"), 0.0),
        )
        if previous_headroom_day != headroom_discharge_day:
            headroom_discharge_today_wh = 0.0
        headroom_discharge_last_active_ts = _safe_float(
            active_state.get("headroom_discharge_last_active_ts"),
            0.0,
        )
        headroom_discharge_last_account_ts = _safe_float(
            active_state.get("headroom_discharge_last_account_ts"),
            _safe_float(active_state.get("previous_state_ts"), 0.0),
        )
        if (
            previous_headroom_discharge
            and previous_parallel_val > 0
            and headroom_discharge_last_account_ts > 0
        ):
            accounted_s = max(
                0.0,
                min(self.headroom_discharge_energy_gap_s, now_s - headroom_discharge_last_account_ts),
            )
            if accounted_s > 0:
                headroom_discharge_today_wh += previous_parallel_val * accounted_s / 3600.0
            headroom_discharge_last_active_ts = now_s
        storage_kwh_for_headroom = max(1.0, _safe_float(self.cfg.get("speichergroesse"), 10.0))
        headroom_discharge_daily_limit_wh = (
            storage_kwh_for_headroom * 1000.0 * self.headroom_discharge_daily_limit_pct / 100.0
            if self.headroom_discharge_daily_limit_pct > 0.0
            else 0.0
        )
        headroom_discharge_daily_remaining_wh = (
            max(0.0, headroom_discharge_daily_limit_wh - headroom_discharge_today_wh)
            if headroom_discharge_daily_limit_wh > 0.0
            else 0.0
        )
        headroom_discharge_daily_blocked = bool(
            headroom_discharge_daily_limit_wh > 0.0
            and headroom_discharge_today_wh >= headroom_discharge_daily_limit_wh
        )
        headroom_discharge_cooldown_remaining_s = 0.0
        if (
            self.headroom_discharge_cooldown_s > 0
            and not previous_headroom_discharge
            and headroom_discharge_last_active_ts > 0
        ):
            headroom_discharge_cooldown_remaining_s = max(
                0.0,
                self.headroom_discharge_cooldown_s - max(0.0, now_s - headroom_discharge_last_active_ts),
            )
        headroom_discharge_cooldown_active = headroom_discharge_cooldown_remaining_s > 0.0
        last_auto_ts = _safe_float(active_state.get("last_auto_ts"), 0.0)
        auto_hold_age_s = max(0.0, now_s - last_auto_ts) if last_auto_ts > 0 else 999999.0
        can_reach_target = bool(plan.get("can_reach_target", True))
        max_reachable_soc = _safe_float(plan.get("max_reachable_soc"), -1.0)
        adaptive_curve = _adaptive_curve_context(plan, now_s * 1000.0, soc, allow_before_start=pv_w > 250)
        curve_soc = adaptive_curve.get("curve_soc")
        adaptive_curve_active = bool(adaptive_curve.get("active"))
        adaptive_curve_relation = str(adaptive_curve.get("relation") or "")
        adaptive_curve_below_floor = bool(
            adaptive_curve_relation == "below_floor"
        )
        target_curve_meta = (
            plan.get("target_curve_meta")
            if isinstance(plan.get("target_curve_meta"), dict)
            else {}
        )
        forecast_only_target_active = bool(
            target_curve_meta.get("forecast_only_target_active")
            or str(
                target_curve_meta.get("target_mode") or ""
            )
            .strip()
            .lower()
            in {
                "forecast_100",
                "forecast_only_100",
                "forecast_only",
                "prognose_100",
                "prognose",
            }
        )
        forecast_curve_below_floor = bool(
            adaptive_curve_below_floor
            and forecast_only_target_active
        )
        adaptive_floor_soc = adaptive_curve.get("floor_soc")
        adaptive_ceiling_soc = adaptive_curve.get("ceiling_soc")
        target_curve_soc = _current_curve_soc(plan, now_s * 1000.0, allow_before_start=pv_w > 250)
        adaptive_headroom_required_wh = max(
            0.0,
            _safe_float(active_state.get("adaptive_headroom_required_wh"), 0.0),
        )
        adaptive_headroom_available_wh = max(
            0.0,
            _safe_float(active_state.get("adaptive_headroom_available_wh"), 0.0),
        )
        curtailment_pressure_wh = max(
            0.0,
            _safe_float(active_state.get("curtailment_pressure_wh"), 0.0),
        )
        headroom_reserve_pressure_wh = max(0.0, _safe_float(active_state.get("headroom_reserve_pressure_wh"), 0.0))
        headroom_reserve_active = bool(
            _truthy(active_state.get("headroom_reserve_active"))
            or headroom_reserve_pressure_wh >= 200.0
        )
        headroom_reserve_source = str(active_state.get("headroom_reserve_source") or "")
        headroom_execution = (
            active_state.get("headroom_execution")
            if isinstance(active_state.get("headroom_execution"), dict)
            else {}
        )
        headroom_execution_allowed = bool(
            headroom_execution.get("schema_version") == 1
            and headroom_execution.get("allowed") is True
        )
        headroom_execution_reason = str(
            headroom_execution.get("reason_code") or "HEADROOM_EXECUTION_CONTRACT_MISSING"
        )
        headroom_execution_residual_wh = max(
            0.0,
            _safe_float(headroom_execution.get("residual_wh"), 0.0),
        )
        headroom_execution_target_soc = _safe_float(
            headroom_execution.get("target_soc"),
            -1.0,
        )
        headroom_execution_hard_floor_soc = _safe_float(
            headroom_execution.get("hard_floor_soc"),
            -1.0,
        )
        curve_gap_pct = None if curve_soc is None else float(curve_soc) - float(soc)
        curve_above_pct = max(0.0, -float(curve_gap_pct)) if curve_gap_pct is not None else 0.0
        curve_soft_taper_pct = max(0.2, self.curve_tolerance_pct)
        curve_soft_factor = (
            max(0.0, min(1.0, 1.0 - (curve_above_pct / curve_soft_taper_pct)))
            if curve_above_pct > 0.0
            else 1.0
        )
        curve_soft_charge_limit_w = self.max_charge_w
        curve_soft_charge_active = False
        if curve_above_pct > 0.0:
            curve_soft_charge_limit_w = max(
                0,
                min(self.max_charge_w, int(round(self.max_charge_w * curve_soft_factor))),
            )
            curve_soft_charge_active = curve_soft_charge_limit_w < self.max_charge_w
        curve_above_enter = bool(
            curve_gap_pct is not None
            and -curve_gap_pct >= self.curve_tolerance_pct
        )
        curve_above_keep = bool(
            previous_parallel_state == "parallel_curve_auto_hold"
            and curve_gap_pct is not None
            and -curve_gap_pct >= self.curve_auto_hold_exit_pct
        )
        curve_above_soft = bool(
            curve_gap_pct is not None
            and -curve_gap_pct >= self.curve_auto_hold_exit_pct
        )
        curve_guard_active = bool(
            curve_gap_pct is not None
            and curve_gap_pct <= self.curve_guard_enter_below_pct
        )
        first_curve_ts, first_curve_soc = _first_curve_point(plan)
        pre_curve_hold_margin_pct = max(
            0.0,
            _safe_float(self.cfg.get("storage_parallel_pre_curve_hold_margin_pct"), 0.2),
        )
        pre_curve_hold_active = bool(
            first_curve_ts is not None
            and first_curve_soc is not None
            and not forecast_curve_below_floor
            and now_s * 1000.0 < float(first_curve_ts)
            and pv_w > 250
            and soc >= float(first_curve_soc) - pre_curve_hold_margin_pct
        )
        pre_curve_hold_raw_active = bool(pre_curve_hold_active)
        pre_curve_ifc_start_active = bool(
            pre_curve_hold_raw_active
            and i_fc_w >= self.pre_curve_ifc_start_w
        )
        if pre_curve_ifc_start_active:
            pre_curve_hold_active = False
        wallbox_home_includes = _truthy(
            live.get(
                "Wallbox_Home_Includes",
                active_state.get("Wallbox_Home_Includes", False),
            )
        )
        home_rule_w = max(0, int(home_w))
        fixed_load_w = home_rule_w + wp_w
        pv_after_fixed_w = max(0, pv_w - fixed_load_w)
        house_deficit_w = max(0, fixed_load_w - pv_w)
        price_curve_need_w = max(i_fc_w, i_min_lade_w)
        price_export_w = max(0, -grid_ema_w, pv_w - fixed_load_w - int(wallbox_w))
        curve_export_w = max(0, -grid_ema_w)
        curve_safe_charge_w = max(0, int(curve_export_w) - max(150, self.grid_limit_w))
        if (
            previous_parallel_state in ("parallel_curve_charge", "parallel_curve_charge_cap")
            and bat_w > 300
            and grid_ema_w <= self.grid_limit_w
        ):
            # Reale Batterieladung ohne Netzbezug ist nur Haltehilfe für eine
            # bereits aktive Kurven-/Abregelphase. Zusammen mit der weiterhin
            # vorhandenen Einspeisung bildet sie den stabilen PV-Laderahmen;
            # AUTO selbst ist kein Grund, neu in CHRG zu springen.
            curve_safe_charge_w = max(
                curve_safe_charge_w,
                max(0, int(bat_w) + max(0, -grid_ema_w) - max(150, self.grid_limit_w)),
            )
        curve_safe_charge_w = min(curve_safe_charge_w, self.max_charge_w)
        if previous_parallel_state in ("parallel_curve_charge", "parallel_curve_charge_cap"):
            curve_ifc_export_catchup_floor_w = self.curve_charge_keep_w
        elif previous_parallel_state == "parallel_curve_auto_no_surplus":
            curve_ifc_export_catchup_floor_w = self.curve_charge_reenter_w
        else:
            curve_ifc_export_catchup_floor_w = self.curve_charge_enter_w
        curve_ifc_export_catchup_w = 0
        curve_ifc_export_catchup_active = bool(
            i_fc_w >= curve_ifc_export_catchup_floor_w
            and pv_w > 250
            and curve_safe_charge_w >= curve_ifc_export_catchup_floor_w
            and not curve_above_soft
        )
        if curve_ifc_export_catchup_active:
            curve_ifc_export_catchup_w = min(
                max(i_fc_w, curve_ifc_export_catchup_floor_w),
                self.max_charge_w,
                curve_safe_charge_w,
            )
        curve_cap_target_w = 0
        curve_cap_margin_w = self.curve_cap_keep_margin_w if previous_parallel_state == "parallel_curve_charge_cap" else self.curve_cap_enter_margin_w
        curve_cap_relevant = bool(
            curve_above_enter
            or curve_above_keep
            or (previous_parallel_state == "parallel_curve_charge_cap" and curve_above_soft)
        )
        previous_curve_cap_w = (
            max(0, _safe_int(previous_parallel_val, 0))
            if previous_parallel_state == "parallel_curve_charge_cap"
            else 0
        )
        curve_cap_release_pending = bool(active_state.get("curve_cap_release_pending"))
        curve_cap_release_requested = bool(
            active_state.get("curve_cap_release_requested")
        )
        curve_cap_release_confirmed_since_ts = max(
            0.0,
            _safe_float(active_state.get("curve_cap_release_confirmed_since_ts"), 0.0),
        )
        curve_cap_bounded_zero_w = max(
            EMS_POWER_SETTINGS_NONZERO_MIN_W,
            self.abregel_min_charge_w,
        )
        curve_cap_tracking_active = bool(
            (
                previous_parallel_state == "parallel_curve_charge_cap"
                and previous_curve_cap_w > 0
            )
            or curve_cap_release_pending
            or curve_cap_release_requested
        )
        if curve_cap_relevant:
            curve_cap_target_w = 0
        curve_cap_keep_active = False
        curve_cap_grid_contract_valid = bool(
            _truthy(live.get("RSCP_Sample_Valid", True))
            and _truthy(live.get("Grid_Power_Valid", True))
        )
        grid_import_w = max(0, grid_w, grid_ema_w)
        grid_export_w = 0 if grid_import_w > 0 else max(0, -grid_w, -grid_ema_w)
        curve_cap_real_grid_import_active = bool(
            curve_cap_grid_contract_valid
            and grid_import_w >= self.grid_relief_enter_w
        )
        curve_cap_release_below_since_ts = (
            max(0.0, _safe_float(active_state.get("curve_cap_release_below_since_ts"), 0.0))
            if (
                previous_parallel_state == "parallel_curve_charge_cap"
                and previous_curve_cap_w > 0
            )
            else 0.0
        )
        curve_cap_post_release_until_ts = max(
            0.0,
            _safe_float(active_state.get("curve_cap_post_release_until_ts"), 0.0),
        )
        if curve_cap_post_release_until_ts <= now_s:
            curve_cap_post_release_until_ts = 0.0
        curve_cap_post_release_guard_initial = bool(curve_cap_post_release_until_ts > now_s)
        settings_readback_valid = bool(
            live.get("ems_power_settings_read") is True
            and live.get("ems_power_settings_valid") is True
            and isinstance(live.get("power_limits_active"), bool)
            and isinstance(live.get("ems_max_charge_power_w"), int)
            and not isinstance(live.get("ems_max_charge_power_w"), bool)
            and int(live.get("ems_max_charge_power_w")) >= 0
            and isinstance(live.get("ems_max_discharge_power_w"), int)
            and not isinstance(live.get("ems_max_discharge_power_w"), bool)
            and int(live.get("ems_max_discharge_power_w")) >= 0
            and isinstance(live.get("ems_discharge_start_power_w"), int)
            and not isinstance(live.get("ems_discharge_start_power_w"), bool)
            and int(live.get("ems_discharge_start_power_w")) >= 0
        )
        settings_bounded_zero_confirmed = bool(
            settings_readback_valid
            and live.get("power_limits_active") is True
            and int(live.get("ems_max_charge_power_w")) <= curve_cap_bounded_zero_w
        )
        settings_previous_curve_cap_confirmed = bool(
            settings_readback_valid
            and live.get("power_limits_active") is True
            and previous_curve_cap_w > 0
            and abs(int(live.get("ems_max_charge_power_w")) - previous_curve_cap_w)
            < RSCP_POWER_SETTINGS_TOLERANCE_W
        )
        settings_release_confirmed = bool(
            settings_readback_valid
            and live.get("power_limits_active") is False
        )
        meter_home_w = home_rule_w + wp_w
        if wallbox_home_includes and wallbox_w > 250:
            meter_home_w += int(wallbox_w)
        meter_balance = _meter_balance_plausible(pv_w, grid_w, bat_w, meter_home_w)
        meter_balance_plausible = bool(meter_balance["home_balance_plausible"])
        # Harter Abregelschutz darf sich nicht an der aktuellen Batterieladung
        # hochziehen. Die Batterieladung kann bereits Ergebnis der vorherigen
        # Regelentscheidung sein. Entscheidend sind nur Netz-Einspeisung und
        # echte PV-Leistung oberhalb der WR-Grenze.
        curve_cap_measured_charge_w = 0
        inverter_limit_w = _inverter_ac_limit_w(self.cfg, active_state, live)
        configured_export_limit_w = _configured_kw_or_w(self.cfg.get("einspeiselimit", 0))
        live_derate_limit_w = _safe_int(live.get("derate_at_power_w", active_state.get("derate_at_power_w")), 0)
        pcc_limit_contract = resolve_buffered_pcc_limit(
            configured_export_limit_w,
            live_derate_limit_w,
            self.curve_cap_feed_buffer_w,
        )
        derate_hard_limit_w = int(pcc_limit_contract.get("hard_limit_w") or 0)
        derate_limit_w = int(pcc_limit_contract.get("limit_w") or 0)
        derate_limit_source = str(pcc_limit_contract.get("source") or "none")
        derating_active = bool(
            _truthy(live.get("pv_derating_active", active_state.get("pv_derating_active", False)))
            or _truthy(live.get("ems_derating_active", active_state.get("ems_derating_active", False)))
            or _truthy(live.get("power_limits_active", active_state.get("power_limits_active", False)))
        )
        ems_budget = _dc_coupled_ems_budget(
            pv_w,
            inverter_limit_w,
            derate_limit_w,
            home_w,
            wp_w,
            wallbox_w,
            self.max_charge_w,
            soc,
            external_ac_pv_w=live.get("Ext_PV_Power"),
            external_ac_pv_valid=live.get("Ext_PV_Power_Valid"),
            external_ac_pv_source=live.get("Ext_PV_Power_Source"),
        )
        inverter_pressure_w = int(ems_budget["battery_must_dc_w"])
        derating_pressure_w = int(ems_budget["battery_must_ac_w"])
        ems_mandatory_charge_w = int(ems_budget["battery_must_real_w"])
        feed_export_threshold_w = max(0, int(derate_limit_w))
        release_export_threshold_w = (
            max(0, int(feed_export_threshold_w) - int(self.curve_cap_release_band_w))
            if feed_export_threshold_w > 0
            else 0
        )
        if feed_export_threshold_w > 0:
            grid_export_error_w = int(grid_export_w) - int(feed_export_threshold_w)
            grid_export_over_limit_w = max(0, grid_export_error_w)
            curve_cap_below_threshold_w = max(0, -grid_export_error_w)
        else:
            grid_export_error_w = int(grid_export_w) if curve_above_enter else 0
            grid_export_over_limit_w = max(0, grid_export_error_w)
            curve_cap_below_threshold_w = 0
        curve_cap_feedback_active = bool(
            curve_cap_tracking_active
            and curve_cap_grid_contract_valid
            and feed_export_threshold_w > 0
            and not curve_cap_real_grid_import_active
            and (
                grid_export_over_limit_w > 0
                or curve_cap_below_threshold_w <= self.curve_cap_feedback_band_w
            )
        )
        if curve_cap_feedback_active:
            curve_cap_grid_pressure_w = max(0, int(previous_curve_cap_w) + int(grid_export_error_w))
            if curve_cap_grid_pressure_w < previous_curve_cap_w:
                curve_cap_grid_pressure_w = max(
                    curve_cap_grid_pressure_w,
                    max(0, int(previous_curve_cap_w) - int(self.curve_cap_step_w)),
                )
        else:
            curve_cap_grid_pressure_w = int(grid_export_over_limit_w)
        curve_cap_dc_hold_margin_w = max(
            int(previous_curve_cap_w),
            int(inverter_pressure_w),
        )
        curve_cap_dc_hold_active = bool(
            previous_curve_cap_w >= self.curve_charge_enter_w
            and curve_cap_grid_contract_valid
            and curve_above_soft
            and inverter_pressure_w >= max(self.abregel_min_charge_w, self.curve_charge_enter_w)
            and feed_export_threshold_w > 0
            and release_export_threshold_w > 0
            and not curve_cap_real_grid_import_active
            and (
                int(grid_export_w) + int(curve_cap_dc_hold_margin_w)
                >= max(0, int(release_export_threshold_w) - int(self.curve_cap_feedback_band_w))
            )
        )
        # DC-/WR-Druck darf den harten Abregelschutz nur begleiten, wenn der
        # AC-Netzpunkt schon an der Abregelgrenze arbeitet oder der Pfad durch
        # die Hysterese gehalten wird. Ist der Abregelschutz bereits aktiv,
        # bleibt echter WR-Druck auch bei kurzen Haus-/Grid-Spruengen erhalten.
        # PV knapp oberhalb der WR-Leistung ist sonst nur Diagnose; der E3DC
        # kann diesen Anteil oft autonom aufnehmen.
        curve_cap_dc_pressure_active = bool(
            inverter_pressure_w > 0
            and curve_cap_grid_contract_valid
            and feed_export_threshold_w > 0
            and not curve_cap_real_grid_import_active
            and (
                grid_export_over_limit_w > 0
                or curve_cap_feedback_active
                or curve_cap_dc_hold_active
                or (
                    release_export_threshold_w > 0
                    and grid_export_w >= release_export_threshold_w
                    and (
                        curve_above_soft
                        or curve_above_keep
                        or curve_above_enter
                        or previous_curve_cap_w >= self.curve_charge_enter_w
                        or derating_active
                    )
                )
            )
        )
        curve_cap_dc_pressure_w = int(inverter_pressure_w) if curve_cap_dc_pressure_active else 0
        curve_cap_model_pressure_w = (
            int(ems_mandatory_charge_w)
            if (
                meter_balance_plausible
                and (
                    grid_export_over_limit_w > 0
                    or curve_cap_feedback_active
                    or curve_cap_dc_pressure_active
                )
            )
            else 0
        )
        curve_cap_direct_pressure_w = int(grid_export_over_limit_w) + int(curve_cap_dc_pressure_w)
        curve_cap_pressure_w = max(
            int(curve_cap_grid_pressure_w),
            int(curve_cap_direct_pressure_w),
        )
        curve_cap_physical_pressure_w = max(
            int(curve_cap_grid_pressure_w) + int(inverter_pressure_w),
            int(inverter_pressure_w),
        )
        curve_cap_export_room_w = int(curve_cap_pressure_w)
        curve_cap_excess_charge_w = max(0, int(bat_w) - int(curve_cap_export_room_w))
        projected_export_without_charge_w = int(grid_export_w)
        projected_export_threshold_w = int(derate_limit_w) if derate_limit_w > 0 else int(feed_export_threshold_w)
        projected_export_over_limit_w = int(grid_export_over_limit_w)
        current_feed_pressure_w = int(grid_export_over_limit_w)
        curve_cap_hard_pressure_active = bool(
            curve_cap_grid_contract_valid
            and not curve_cap_real_grid_import_active
            and (
                curve_cap_pressure_w >= self.curve_cap_export_trigger_w
                or grid_export_over_limit_w > 0
                or curve_cap_grid_pressure_w > 0
            )
        )
        curve_cap_post_release_reentry_blocked = bool(
            (
                curve_cap_release_pending
                or curve_cap_release_requested
                or curve_cap_post_release_guard_initial
            )
            and grid_export_over_limit_w <= 0
            and curve_cap_hard_pressure_active
        )
        if curve_cap_post_release_reentry_blocked:
            # Während der bestätigungsgebundenen Freigabe darf alleiniger
            # DC-/WR-Druck unterhalb der realen Netzpunkt-Eintrittsschwelle
            # keinen neuen mehr-kW-Laderahmen öffnen.
            curve_cap_hard_pressure_active = False
        if curve_cap_hard_pressure_active:
            curve_cap_relevant = True
        curve_cap_short_hold_active = bool(
            previous_curve_cap_w >= self.curve_charge_enter_w
            and curve_above_soft
            and previous_parallel_age_s <= self.curve_cap_short_hold_s
            and grid_w <= max(150, self.grid_limit_w)
            and grid_ema_w <= max(150, self.grid_limit_w)
            and (
                feed_export_threshold_w <= 0
                or curve_cap_below_threshold_w <= self.curve_cap_feedback_band_w
            )
        )
        curve_cap_export_room_active = bool(
            curve_cap_grid_contract_valid
            and not curve_cap_real_grid_import_active
            and curve_cap_pressure_w > 0
            and curve_cap_hard_pressure_active
            and (
                feed_export_threshold_w > 0
                or curve_cap_pressure_w >= self.curve_cap_export_trigger_w
            )
        )
        curve_cap_neutral_keep = bool(
            previous_curve_cap_w >= self.curve_charge_enter_w
            and (curve_above_soft or curve_cap_feedback_active)
            and grid_w <= max(150, self.grid_limit_w)
            and grid_ema_w <= max(150, self.grid_limit_w)
            and (
                curve_cap_hard_pressure_active
                or (curve_cap_feedback_active and curve_cap_grid_pressure_w > 0)
            )
        )
        curve_cap_pv_surplus_w = 0
        curve_cap_proactive_active = False
        curve_cap_release_below_active = bool(
            curve_cap_tracking_active
            and curve_cap_grid_contract_valid
            and not curve_cap_real_grid_import_active
            and feed_export_threshold_w > 0
            and grid_export_w < release_export_threshold_w
        )
        curve_cap_release_elapsed_s = 0.0
        curve_cap_release_grace_active = False
        curve_cap_release_ramp_active = False
        curve_cap_hysteresis_hold_active = False
        curve_cap_hysteresis_amount_follow_active = False
        curve_cap_hysteresis_floor_w = curve_cap_bounded_zero_w
        curve_cap_invalid_hold_active = False
        curve_cap_release_phase = "inactive"
        if curve_cap_tracking_active:
            if not curve_cap_grid_contract_valid:
                # Eine ungültige/stale Netzpunktprobe unterbricht den
                # Kontinuitätsnachweis. Sie ist weder 0 W noch Zeit unterhalb
                # der Release-Schwelle.
                curve_cap_release_below_since_ts = 0.0
                curve_cap_invalid_hold_active = True
                curve_cap_release_phase = "hold_invalid_grid"
            elif curve_cap_real_grid_import_active:
                curve_cap_release_below_since_ts = 0.0
                curve_cap_release_confirmed_since_ts = 0.0
                curve_cap_release_pending = False
                curve_cap_release_requested = False
                curve_cap_release_phase = "grid_import_release"
            elif grid_export_over_limit_w > 0:
                curve_cap_release_below_since_ts = 0.0
                curve_cap_release_confirmed_since_ts = 0.0
                curve_cap_release_pending = False
                curve_cap_release_requested = False
                curve_cap_post_release_until_ts = 0.0
                curve_cap_release_phase = "hard_pressure"
            elif curve_cap_release_requested:
                if not curve_cap_release_below_active:
                    curve_cap_release_requested = False
                    curve_cap_release_pending = True
                    curve_cap_release_confirmed_since_ts = 0.0
                    curve_cap_release_phase = "post_release_hysteresis_hold"
                elif settings_release_confirmed:
                    curve_cap_release_requested = False
                    curve_cap_release_below_since_ts = 0.0
                    curve_cap_release_confirmed_since_ts = 0.0
                    curve_cap_release_phase = "released_confirmed"
                    curve_cap_post_release_until_ts = max(
                        curve_cap_post_release_until_ts,
                        now_s + self.curve_cap_release_grace_s,
                    )
                elif settings_bounded_zero_confirmed:
                    # Der finale Freigaberahmen wurde bereits angefordert,
                    # physisch ist aber noch der sichere Protokollboden aktiv.
                    # Das ist kein neuer Cap-Eintritt. Bis zum frischen
                    # limits_used=false-Readback bleibt ausschließlich die
                    # bestätigungsgebundene Freigabe aktiv.
                    curve_cap_release_phase = "await_release_readback"
                else:
                    # Stale, unbekannt oder oberhalb des bestätigten Bodens:
                    # nie optimistisch freigeben, sondern auf den sicheren
                    # begrenzten Nullrahmen zurückfallen.
                    curve_cap_release_requested = False
                    curve_cap_release_pending = True
                    curve_cap_release_confirmed_since_ts = 0.0
                    curve_cap_release_phase = "post_release_readback_unconfirmed"
            elif curve_cap_release_pending:
                if not settings_bounded_zero_confirmed:
                    curve_cap_release_confirmed_since_ts = 0.0
                    curve_cap_release_phase = (
                        "await_zero_readback"
                        if settings_readback_valid
                        else "hold_unknown_readback"
                    )
                elif not curve_cap_release_below_active:
                    curve_cap_release_confirmed_since_ts = 0.0
                    curve_cap_release_phase = "post_release_hysteresis_hold"
                else:
                    if curve_cap_release_confirmed_since_ts <= 0.0:
                        curve_cap_release_confirmed_since_ts = now_s
                    curve_cap_release_elapsed_s = max(
                        0.0,
                        now_s - curve_cap_release_confirmed_since_ts,
                    )
                    if curve_cap_release_elapsed_s < self.curve_cap_release_grace_s:
                        curve_cap_release_phase = "post_release_confirmed_grace"
                    else:
                        curve_cap_release_pending = False
                        curve_cap_release_requested = True
                        curve_cap_release_phase = "release_requested"
                        curve_cap_post_release_until_ts = (
                            now_s + self.curve_cap_release_grace_s
                        )
            elif feed_export_threshold_w <= 0:
                # Ohne belegte Abregelgrenze gibt es keine fachliche
                # Release-Schwelle. Der historische allgemeine Kurvenpfad
                # darf dadurch nicht in einem Cap festgehalten werden.
                curve_cap_release_below_since_ts = 0.0
                curve_cap_release_phase = "inactive_no_feed_limit"
            elif curve_cap_release_below_active:
                if curve_cap_release_below_since_ts <= 0.0:
                    curve_cap_release_below_since_ts = now_s
                curve_cap_release_elapsed_s = max(
                    0.0,
                    now_s - curve_cap_release_below_since_ts,
                )
                curve_cap_release_grace_active = bool(
                    curve_cap_release_elapsed_s < self.curve_cap_release_grace_s
                )
                curve_cap_release_ramp_active = not curve_cap_release_grace_active
                curve_cap_release_phase = (
                    "release_grace"
                    if curve_cap_release_grace_active
                    else "release_ramp"
                )
            else:
                curve_cap_release_below_since_ts = 0.0
                curve_cap_hysteresis_hold_active = True
                curve_cap_release_phase = "hysteresis_hold"
        # Nach einer bestätigten Freigabe gehört ein neuer begrenzter Readback
        # dem nachfolgenden Storage-Owner, zum Beispiel dem Kurven-Auto-Hold.
        # Der Post-Release-Guard darf daraus ohne neuen typisierten Druck keinen
        # Abregelbesitz zurückgewinnen. Echter Grid-Druck tritt im nächsten
        # Zweig sofort wieder ein; unbestätigter DC-Druck bleibt durch den
        # Reentry-Guard oben bis zum Ablauf des Fensters gesperrt.
        elif curve_cap_hard_pressure_active:
            curve_cap_post_release_until_ts = 0.0
            curve_cap_release_phase = "hard_entry"
        curve_cap_active = bool(curve_cap_target_w > 0 and curve_cap_keep_active)
        if curve_cap_real_grid_import_active:
            curve_cap_target_w = 0
            curve_cap_active = False
            curve_cap_keep_active = False
            curve_cap_neutral_keep = False
        elif curve_cap_release_pending or curve_cap_release_requested:
            curve_cap_target_w = 0
            curve_cap_keep_active = True
            curve_cap_active = True
        elif curve_cap_invalid_hold_active:
            curve_cap_target_w = min(self.max_charge_w, previous_curve_cap_w)
            curve_cap_keep_active = True
            curve_cap_active = True
        elif curve_cap_hysteresis_hold_active:
            # Die Netzpunkthysterese hält den Abregelschutz als Zustand aktiv,
            # nicht pauschal den Betrag eines früheren Druckframes. Nur ein
            # frischer, exakt zum letzten Cap passender POWER_SETTINGS-Readback
            # darf den bereits netzpunkt- und schrittbegrenzten Zielwert
            # absenken. Der Protokollboden und echter DC-/WR-Druck bleiben
            # dabei erhalten; eine Erhöhung entsteht aus diesem Hold-Zweig nie.
            curve_cap_hysteresis_amount_follow_active = bool(
                curve_cap_grid_contract_valid
                and not curve_cap_real_grid_import_active
                and settings_previous_curve_cap_confirmed
                and curve_cap_pressure_w < previous_curve_cap_w
            )
            if curve_cap_hysteresis_amount_follow_active:
                curve_cap_hysteresis_floor_w = max(
                    curve_cap_bounded_zero_w,
                    curve_cap_dc_pressure_w,
                )
                curve_cap_target_w = min(
                    self.max_charge_w,
                    previous_curve_cap_w,
                    max(
                        curve_cap_hysteresis_floor_w,
                        curve_cap_pressure_w,
                        previous_curve_cap_w - self.curve_cap_step_w,
                    ),
                )
            else:
                curve_cap_target_w = min(self.max_charge_w, previous_curve_cap_w)
            curve_cap_keep_active = True
            curve_cap_active = True
        elif curve_cap_release_grace_active:
            curve_cap_target_w = min(self.max_charge_w, previous_curve_cap_w)
            curve_cap_keep_active = True
            curve_cap_active = True
        elif curve_cap_release_ramp_active:
            if previous_curve_cap_w <= curve_cap_bounded_zero_w:
                curve_cap_target_w = 0
            else:
                curve_cap_target_w = max(
                    curve_cap_bounded_zero_w,
                    previous_curve_cap_w - self.curve_cap_step_w,
                )
            curve_cap_keep_active = curve_cap_target_w > 0
            curve_cap_active = curve_cap_target_w > 0
            if not curve_cap_active:
                curve_cap_release_pending = True
                curve_cap_release_confirmed_since_ts = 0.0
                curve_cap_keep_active = True
                curve_cap_active = True
                curve_cap_release_phase = "await_zero_readback"
        elif curve_cap_relevant and curve_cap_export_room_active:
            next_curve_cap_target_w = int(max(
                0,
                min(self.max_charge_w, curve_cap_pressure_w),
            ))
            if curve_ifc_export_catchup_active:
                next_curve_cap_target_w = max(
                    next_curve_cap_target_w,
                    int(curve_ifc_export_catchup_w),
                )
            if next_curve_cap_target_w > 0:
                next_curve_cap_target_w = max(
                    curve_cap_bounded_zero_w,
                    next_curve_cap_target_w,
                )
            if (
                previous_curve_cap_w >= self.curve_charge_enter_w
                and abs(next_curve_cap_target_w - previous_curve_cap_w) < self.curve_cap_step_w
                and not (curve_cap_hard_pressure_active and next_curve_cap_target_w > previous_curve_cap_w)
                and not curve_cap_feedback_active
            ):
                next_curve_cap_target_w = previous_curve_cap_w
            curve_cap_target_w = next_curve_cap_target_w
            curve_cap_keep_active = bool(previous_curve_cap_w >= self.curve_charge_enter_w)
            curve_cap_active = True
        elif curve_cap_relevant and curve_cap_neutral_keep:
            neutral_keep_w = previous_curve_cap_w
            curve_cap_target_w = min(self.max_charge_w, neutral_keep_w)
            curve_cap_keep_active = True
            curve_cap_active = True
        curve_cap_below_curve_catchup_w = 0
        curve_cap_below_curve_catchup_active = bool(
            curve_cap_active
            and curve_cap_hard_pressure_active
            and not curve_cap_release_pending
            and not curve_cap_release_requested
            and curve_cap_grid_contract_valid
            and not curve_cap_real_grid_import_active
            and curve_gap_pct is not None
            and curve_gap_pct >= self.curve_tolerance_pct
            and pv_w > 250
            and curve_safe_charge_w >= self.curve_charge_enter_w
        )
        if curve_cap_below_curve_catchup_active:
            # Abregel-/WR-Druck darf einen belegten Kurvenrückstand nicht auf
            # seinen kleinen Pflichtanteil reduzieren. Der zusätzlich geöffnete
            # Rahmen stammt ausschließlich aus bereits gemessener, sicherer
            # PV-Einspeisung; Entladung bleibt im nachfolgenden AUTO-Vertrag
            # offen und Netzladen wird nicht freigegeben.
            curve_cap_below_curve_catchup_w = min(
                self.max_charge_w,
                curve_safe_charge_w,
            )
            curve_cap_target_w = min(
                self.max_charge_w,
                max(curve_cap_target_w, curve_cap_below_curve_catchup_w),
            )
            curve_cap_keep_active = True
            curve_cap_active = True
        curve_cap_post_release_guard_active = bool(
            curve_cap_post_release_until_ts > now_s
        )
        reserve_live = dict(live or {})
        if active_state.get("ep_reserve_pct") is not None:
            reserve_live["ep_reserve_pct"] = active_state.get("ep_reserve_pct")
        reserve_soc = effective_ep_reserve_pct(self.cfg, reserve_live, default=8.0) + 0.5
        wb_possible_w = max(
            0,
            _safe_int(active_state.get("wb_possible_power_w"), 0),
            _safe_int(wb_budget.get("wb_possible_power_w"), 0),
        )
        wb_budget_w = max(0, _safe_int(wb_budget.get("budget_w", active_state.get("iAVal_w")), 0))
        wb_modes = {
            1: _normalize_wb_mode(self.cfg.get("wb1_mode", 0)),
            2: _normalize_wb_mode(self.cfg.get("wb2_mode", 0)),
        }
        persistent_wb_configured_ids = {
            cid for cid in (1, 2)
            if _wallbox_configured(self.cfg, cid)
        }
        (
            runtime_wb_configured_ids,
            runtime_wb_active_mode_ids,
            wb_runtime_topology_valid,
        ) = _runtime_wallbox_topology_ids(wb_intent, now_s)
        wb_configured_ids = (
            persistent_wb_configured_ids | runtime_wb_configured_ids
        )
        wb_active_mode_ids = {
            cid for cid in persistent_wb_configured_ids
            if wb_modes.get(cid, 0) != 0
        } | runtime_wb_active_mode_ids
        wallbox_ngna = bool(
            not _cfg_bool(self.cfg, "wb_native_enable", False)
            or not wb_configured_ids
            or not wb_active_mode_ids
        )
        wallbox_rule_w = 0.0 if wallbox_ngna else wallbox_w
        wb_intent_fresh = bool(wb_intent) and now_s - _safe_float(wb_intent.get("ts"), 0.0) <= 45
        intent_mode_off = bool(
            wb_intent_fresh
            and "wb_mode_active" in wb_intent
            and _normalize_wb_mode(wb_intent.get("wb_mode_active", 0)) == 0
        )
        if intent_mode_off:
            wallbox_ngna = True
            wallbox_rule_w = 0.0
        wb_intent_bev_full_blocked = bool(
            wb_intent_fresh
            and wb_intent.get("bev_full_blocked")
            and not wb_intent.get("charging_active")
            and _safe_float(wb_intent.get("wb_power_w"), 0.0) <= 250.0
        )
        wb_intent_cap_amp = _safe_int(
            wb_intent.get("cap_amp", wb_intent.get("set_amp")),
            0,
        )
        wb_intent_physical_charge_active = bool(
            wb_intent.get("charging_active")
            or _safe_float(wb_intent.get("wb_power_w"), 0.0) > 250
        )
        wb_intent_authorized_start_active = bool(
            not wb_intent_bev_full_blocked
            and wb_intent.get("start_requested")
            and wb_intent.get("start_request_authorized")
            and str(wb_intent.get("start_request_contract_version") or "")
            == "wallbox_start_v1"
            and wb_intent_cap_amp > 0
            and (
                wb_intent.get("active")
                or wb_intent.get("car_active")
                or wb_intent.get("connected")
                or wb_intent.get("plugged")
            )
        )
        wb_intent_active = bool(
            wb_intent_fresh
            and (
                wb_intent_physical_charge_active
                or wb_intent_authorized_start_active
            )
        )
        if wallbox_ngna:
            wb_possible_w = 0
            wb_budget_w = 0
            wb_intent_fresh = False
            wb_intent_active = False
        grid_house_fallback_w = 0
        if (
            grid_ema_w >= self.grid_relief_enter_w
            and bat_w >= -100
            and wallbox_rule_w < 250
            and not wb_intent_active
        ):
            # Einige Installationen liefern keinen belastbaren Home_Power-Wert.
            # In Preis-/Haltezustaenden ist der Netzpunkt dann das robustere
            # Ersatzsignal fuer Hausverbrauch, bevor IDLE teuren Netzbezug haelt.
            grid_house_fallback_w = max(0, grid_ema_w)
            house_deficit_w = max(house_deficit_w, grid_house_fallback_w)
        wb_intent_car_present = bool(
            wb_intent_fresh
            and not wb_intent_bev_full_blocked
            and (
                wb_intent.get("car_active")
                or wb_intent.get("connected")
                or wb_intent.get("plugged")
                or wb_intent.get("active")
            )
        )
        intent_wb_mode = _normalize_wb_mode(wb_intent.get("wb_mode_active", 0)) if wb_intent_fresh else 0
        wb_floor_request = str(wb_intent.get("battery_request", "none") or "none").strip().lower()
        wb_storage_floor_requested = bool(
            wb_floor_request in ("allow_discharge", "hold_discharge")
            or wb_intent.get("scheduled_slot_active")
            or wb_intent.get("price_boost_active")
            or wb_intent.get("price_plan_storage_protect")
        )
        wb_storage_floor_active = bool(
            wb_intent_car_present
            and wb_storage_floor_requested
            and (
                intent_wb_mode in (4, 5)
                or any(wb_modes.get(cid, 0) in (4, 5) for cid in wb_active_mode_ids)
            )
        )
        if wb_storage_floor_active:
            curve_cap_active = False
            curve_cap_keep_active = False
            curve_cap_neutral_keep = False
        curve_cap_pv_surplus_w = max(0, wb_budget_w)
        if curve_cap_target_w > 0 and not wb_storage_floor_active:
            curve_cap_proactive_active = bool(
                curve_above_enter
                and curve_cap_export_room_active
                and curve_cap_pv_surplus_w >= curve_cap_target_w + self.curve_cap_enter_margin_w
                and grid_ema_w <= self.grid_limit_w
                and grid_w <= self.grid_limit_w
            )
            curve_cap_active = bool(curve_cap_active or curve_cap_proactive_active)
        wb_budget_signal_active = bool(
            not wallbox_ngna
            and wb_budget_w > 0
            and (
                wallbox_rule_w > 50
                or wb_intent_active
            )
        )
        last_wb_active_ts = _safe_float(active_state.get("last_wb_active_ts"), 0.0)
        last_wb_active_age_s = max(0.0, now_s - last_wb_active_ts) if last_wb_active_ts > 0 else 999999.0
        wb_active_enter = bool(
            not wallbox_ngna
            and (
                wallbox_rule_w > 250
                or wb_intent_active
                or wb_budget_signal_active
            )
        )
        wb_active_keep = bool(
            not wallbox_ngna
            and not wb_intent_bev_full_blocked
            and previous_parallel_state == "parallel_wb_auto"
            and (
                wallbox_rule_w > 50
                or last_wb_active_age_s <= self.wb_hold_s
                or wb_intent_active
            )
        )
        effective_last_wb_active_ts = now_s if wb_active_enter else last_wb_active_ts
        if wb_active_keep and wb_possible_w <= 0:
            wb_possible_w = max(
                6 * 230,
                _safe_int(active_state.get("last_wb_possible_power_w"), 0),
            )
        wb_active = bool(wb_active_enter or wb_active_keep)
        wb_capacity_sink_active = bool(wallbox_rule_w > 250)
        curve_cap_blocked_by_wb_capacity = False
        curve_cap_blocked_by_wb_grid = bool(
            wb_active
            and curve_cap_target_w > 0
            and max(grid_w, grid_ema_w) > self.grid_limit_w
            and not curve_cap_export_room_active
        )
        if curve_cap_blocked_by_wb_grid:
            curve_cap_active = False
            curve_cap_keep_active = False
            curve_cap_neutral_keep = False
            curve_cap_proactive_active = False
        # C++-nah: Jede Regelstelle hat eine eigene Eintritts-/Haltehysterese.
        # Bei aktiver Wallbox darf AUTO bleiben, solange Wallbox plus
        # Batterielader den PV-Rest aufnehmen koennen.
        wb_auto_enter_margin_w = self.max_charge_w
        wb_auto_keep_margin_w = self.max_charge_w + 1800
        wb_grid_abort = bool(grid_ema_w >= self.wb_auto_grid_abort_w)
        wb_auto_enter = bool(
            wb_active
            and not wb_grid_abort
            and pv_after_fixed_w <= wb_possible_w + wb_auto_enter_margin_w
        )
        wb_auto_keep = bool(
            wb_active
            and previous_parallel_state == "parallel_wb_auto"
            and not wb_grid_abort
            and pv_after_fixed_w <= wb_possible_w + wb_auto_keep_margin_w
        )
        wb_owner_real_min_w = max(
            500.0,
            _safe_float(self.cfg.get("storage_parallel_wb_owner_real_min_w"), 6.0 * 230.0),
        )
        wb_internal_owner_allowed = not bool(
            wb_intent.get("external_wallbox_manager")
            or wb_intent.get("openwb_primary_observe_only")
            or wb_intent.get("autonomous_wallbox")
        )
        wb_real_owner_active = bool(wb_internal_owner_allowed and wallbox_rule_w >= wb_owner_real_min_w)
        # Der historische Anti-Flatter-Hold ist keine aktuelle Owner-Evidenz.
        # Maßgeblich bleiben reale Leistung oder ein frischer, autorisierter
        # Intent desselben internen Wallboxpfads.
        wb_current_owner_evidence_active = bool(
            wb_real_owner_active
            or (wb_internal_owner_allowed and wb_intent_active)
        )
        shortfall_pv_catchup_active = bool(_truthy(active_state.get("shortfall_pv_catchup_active")))
        forecast_curve_landing_hold_active = bool(
            _truthy(active_state.get("forecast_curve_landing_hold_active"))
        )
        sliding_horizon_raw_active = bool(
            _truthy(active_state.get("sliding_horizon_active"))
        )
        sliding_horizon_candidate_active = bool(
            _truthy(
                active_state.get(
                    "sliding_horizon_candidate_active",
                    sliding_horizon_raw_active,
                )
            )
        )
        sliding_horizon_corridor_veto = bool(
            _truthy(active_state.get("sliding_horizon_corridor_veto"))
            or (
                sliding_horizon_raw_active
                and adaptive_curve_relation in ("below_floor", "no_curve")
            )
        )
        sliding_horizon_active = bool(
            sliding_horizon_raw_active and not sliding_horizon_corridor_veto
        )
        sliding_horizon_reason = str(active_state.get("sliding_horizon_reason") or "")
        sliding_horizon_confidence = max(
            0.0,
            _safe_float(active_state.get("sliding_horizon_confidence"), 0.0),
        )
        sliding_horizon_min_confidence = max(
            0.0,
            _safe_float(active_state.get("sliding_horizon_min_confidence"), 0.0),
        )
        sliding_horizon_season = str(active_state.get("sliding_horizon_season") or "")
        sliding_horizon_minutes_until_latest_charge = active_state.get("sliding_horizon_minutes_until_latest_charge")
        sliding_horizon_headroom_available_wh = max(
            0.0,
            _safe_float(active_state.get("sliding_horizon_headroom_available_wh"), 0.0),
        )
        sliding_horizon_uncovered_pressure_wh = max(
            0.0,
            _safe_float(active_state.get("sliding_horizon_uncovered_pressure_wh"), 0.0),
        )
        sliding_horizon_uncovered_curtailment_pressure_wh = max(
            0.0,
            _safe_float(active_state.get("sliding_horizon_uncovered_curtailment_pressure_wh"), 0.0),
        )
        forecast_floor_target_gap_pct = max(
            0.0,
            _safe_float(active_state.get("forecast_floor_target_gap_pct"), 0.0),
        )
        forecast_landing_margin_pct = max(
            0.0,
            _safe_float(active_state.get("forecast_landing_margin_pct"), 0.0),
        )
        shortfall_release_margin_pct = max(
            0.1,
            _safe_float(
                self.cfg.get("storage_curve_shortfall_release_margin_pct"),
                0.5,
            ),
        )
        forecast_shortfall_pv_release_active = bool(
            not shortfall_pv_catchup_active
            and not can_reach_target
            and max_reachable_soc > 0.0
            and soc + shortfall_release_margin_pct < max_reachable_soc
            and (
                pv_after_fixed_w >= self.curve_charge_enter_w
                or grid_export_w >= self.curve_charge_enter_w
            )
            and not headroom_reserve_active
        )
        curve_charge_keep = bool(
            previous_parallel_state == "parallel_curve_charge"
            and i_fc_w >= self.curve_charge_keep_w
        )
        curve_no_surplus_keep = bool(
            previous_parallel_state == "parallel_curve_auto_no_surplus"
            and curve_guard_active
            and i_fc_w >= self.curve_charge_keep_w
            and curve_safe_charge_w < self.curve_charge_reenter_w
        )
        curve_settle_hold_active = bool(
            previous_parallel_state == "parallel_curve_auto_hold"
            and not forecast_curve_below_floor
            and curve_gap_pct is not None
            and curve_gap_pct <= self.curve_auto_hold_release_below_pct
            and (
                i_fc_w < self.curve_charge_keep_w
                or -curve_gap_pct >= self.curve_auto_hold_exit_pct
            )
            and pv_w > 250
            and grid_ema_w < self.grid_relief_enter_w
            and not shortfall_pv_catchup_active
            and not curve_ifc_export_catchup_active
        )
        curve_crossed_from_charge_hold = bool(
            previous_parallel_state in (
                "parallel_curve_charge",
                "parallel_curve_charge_cap",
                "parallel_curve_auto_charge",
            )
            and curve_gap_pct is not None
            and -curve_gap_pct >= self.curve_auto_hold_exit_pct
            and pv_w > 250
            and grid_ema_w < self.grid_relief_enter_w
            and not shortfall_pv_catchup_active
            and not curve_ifc_export_catchup_active
        )
        curve_near_idle_hold = bool(
            not forecast_curve_below_floor
            and curve_gap_pct is not None
            and curve_gap_pct <= self.curve_auto_hold_release_below_pct
            and i_fc_w < self.curve_charge_enter_w
            and pv_w > 250
            and grid_ema_w < self.grid_relief_enter_w
            and not shortfall_pv_catchup_active
        )
        curve_edge_export_keep_active = bool(
            previous_parallel_state in ("parallel_curve_auto_hold", "parallel_curve_auto_no_surplus")
            and not forecast_curve_below_floor
            and (curve_gap_pct is None or curve_gap_pct <= self.curve_auto_hold_release_below_pct)
            and i_fc_w < self.curve_charge_enter_w
            and pv_w > 250
            and grid_ema_w < self.grid_relief_enter_w
            and not shortfall_pv_catchup_active
            and not curve_ifc_export_catchup_active
            and (
                grid_export_w >= self.curve_charge_keep_w
                or max(0, bat_w) >= self.curve_charge_keep_w
                or pv_after_fixed_w >= self.curve_charge_enter_w
            )
        )
        curve_edge_soft_charge_active = bool(
            previous_parallel_state == "parallel_curve_charge"
            and previous_decision_age_s <= self.curve_edge_soft_hold_s
            and curve_gap_pct is not None
            and curve_gap_pct <= self.curve_auto_hold_release_below_pct
            and curve_gap_pct >= -self.curve_auto_hold_exit_pct
            and pv_w > 250
            and not headroom_reserve_active
            and not forecast_curve_landing_hold_active
            and not pre_curve_hold_active
            and not shortfall_pv_catchup_active
            and not curve_cap_active
            and not curve_cap_hard_pressure_active
            and curve_safe_charge_w >= self.curve_charge_keep_w
            and (
                i_fc_w >= self.curve_charge_keep_w
                or grid_export_w >= self.curve_charge_keep_w
                or max(0, bat_w) >= self.curve_charge_keep_w
                or pv_after_fixed_w >= self.curve_charge_enter_w
            )
        )
        curve_edge_soft_charge_w = 0
        if curve_edge_soft_charge_active:
            curve_edge_soft_charge_w = min(
                self.max_charge_w,
                curve_safe_charge_w,
                max(
                    self.curve_charge_enter_w,
                    i_fc_w,
                    int(round(previous_parallel_val * self.curve_edge_soft_factor)),
                ),
            )
            if curve_edge_soft_charge_w < self.curve_charge_enter_w:
                curve_edge_soft_charge_active = False
                curve_edge_soft_charge_w = 0
        price_house_discharge_keep = bool(
            previous_parallel_state == "parallel_price_house_discharge"
            and (
                house_deficit_w >= self.price_house_discharge_keep_w
                or previous_parallel_val > 0
            )
            and soc > reserve_soc
        )
        night_floor_keep = bool(
            self.night_floor_enabled
            and previous_parallel_state == "parallel_night_floor_hold"
            and curve_soc is not None
            and pv_w <= 250
            and grid_ema_w < self.grid_relief_enter_w
            and not wb_active
            and soc < float(curve_soc) + self.night_floor_keep_pct
        )
        night_floor_enter = bool(
            self.night_floor_enabled
            and curve_soc is not None
            and pv_w <= 250
            and grid_ema_w < self.grid_relief_enter_w
            and not wb_active
            and soc < float(curve_soc) - self.night_floor_enter_pct
        )

        headroom_floor_candidates = [reserve_soc]
        if adaptive_floor_soc is not None:
            headroom_floor_candidates.append(float(adaptive_floor_soc))
        elif curve_soc is not None:
            headroom_floor_candidates.append(float(curve_soc))
        if 0.0 <= headroom_execution_target_soc <= 100.0:
            headroom_floor_candidates.append(headroom_execution_target_soc)
        if 0.0 <= headroom_execution_hard_floor_soc <= 100.0:
            headroom_floor_candidates.append(headroom_execution_hard_floor_soc)
        headroom_discharge_floor_soc = max(headroom_floor_candidates)
        headroom_discharge_gap_pct = (
            max(0.0, float(soc) - float(headroom_discharge_floor_soc))
            if headroom_discharge_floor_soc is not None
            else 0.0
        )
        headroom_discharge_enter = bool(
            headroom_discharge_gap_pct >= self.headroom_discharge_enter_pct
        )
        headroom_discharge_keep = bool(
            previous_headroom_discharge
            and headroom_discharge_gap_pct >= self.headroom_discharge_keep_pct
        )
        headroom_discharge_curve_ok = bool(
            self.headroom_discharge_enable
            and adaptive_curve_active
            and headroom_discharge_floor_soc is not None
            and (headroom_discharge_enter or headroom_discharge_keep)
        )
        uncovered_curtailment_pressure_wh = max(
            0.0,
            curtailment_pressure_wh - adaptive_headroom_available_wh,
        )
        headroom_discharge_pressure_wh = max(
            adaptive_headroom_required_wh,
            uncovered_curtailment_pressure_wh,
        )
        headroom_discharge_pressure_ok = bool(
            headroom_discharge_pressure_wh >= self.headroom_discharge_min_pressure_wh
        )
        headroom_target_plateau_margin_pct = self.headroom_discharge_target_plateau_margin_pct
        headroom_discharge_target_plateau_reached = bool(
            target_curve_soc is not None
            and float(target_curve_soc) >= self.target_soc - headroom_target_plateau_margin_pct
            and soc >= float(target_curve_soc) - headroom_target_plateau_margin_pct
        )
        headroom_discharge_export_room_w = 0
        if feed_export_threshold_w > 0:
            headroom_discharge_export_room_w = max(
                0,
                int(feed_export_threshold_w)
                - int(grid_export_w)
                - int(self.headroom_discharge_export_margin_w),
            )
        headroom_discharge_grid_ok = bool(
            feed_export_threshold_w > 0
            and grid_import_w <= self.headroom_discharge_import_guard_w
            and headroom_discharge_export_room_w >= self.headroom_discharge_min_w
        )
        headroom_discharge_abregel_blocked = bool(
            curve_cap_active
            or curve_cap_hard_pressure_active
            or curve_cap_feedback_active
            or curve_cap_dc_pressure_active
            or curve_cap_pressure_w >= self.curve_cap_export_trigger_w
            or curve_cap_post_release_guard_active
        )
        headroom_discharge_blocked_reason = ""
        if not self.headroom_discharge_enable:
            headroom_discharge_blocked_reason = "disabled"
        elif not headroom_execution_allowed:
            headroom_discharge_blocked_reason = "execution:%s" % headroom_execution_reason.lower()
        elif headroom_execution_residual_wh <= 0.0:
            headroom_discharge_blocked_reason = "execution:headroom_residual_depleted"
        elif headroom_discharge_daily_blocked:
            headroom_discharge_blocked_reason = "daily_limit"
        elif headroom_discharge_cooldown_active:
            headroom_discharge_blocked_reason = "cooldown"
        elif not headroom_discharge_curve_ok:
            headroom_discharge_blocked_reason = "curve_floor"
        elif not headroom_discharge_pressure_ok:
            headroom_discharge_blocked_reason = "no_headroom_pressure"
        elif headroom_discharge_target_plateau_reached:
            headroom_discharge_blocked_reason = "target_plateau_reached"
        elif pv_w < self.headroom_discharge_min_pv_w:
            headroom_discharge_blocked_reason = "low_pv"
        elif wb_active or wb_storage_floor_active:
            headroom_discharge_blocked_reason = "wallbox_active"
        elif shortfall_pv_catchup_active or i_fc_w >= self.curve_charge_keep_w:
            headroom_discharge_blocked_reason = "curve_charge_needed"
        elif headroom_discharge_abregel_blocked:
            headroom_discharge_blocked_reason = "abregel_pressure"
        elif not headroom_discharge_grid_ok:
            headroom_discharge_blocked_reason = "no_export_room"

        headroom_discharge_target_w = 0
        headroom_discharge_w = 0
        headroom_discharge_active = False
        if not headroom_discharge_blocked_reason:
            storage_kwh = max(1.0, _safe_float(self.cfg.get("speichergroesse"), 10.0))
            usable_gap_pct = max(0.0, headroom_discharge_gap_pct - self.headroom_discharge_keep_pct)
            gap_based_w = int(round(
                (usable_gap_pct / 100.0)
                * storage_kwh
                * 1000.0
                / self.headroom_discharge_horizon_h
            ))
            pressure_based_w = int(round(headroom_discharge_pressure_wh / self.headroom_discharge_horizon_h))
            headroom_discharge_max_w = min(
                self.max_discharge_w,
                self.headroom_discharge_max_w_cfg,
                headroom_discharge_export_room_w,
                int(round(
                    headroom_execution_residual_wh
                    * 3600.0
                    / max(15.0, self.headroom_discharge_energy_gap_s)
                )),
            )
            headroom_discharge_target_w = max(
                self.headroom_discharge_min_w,
                min(headroom_discharge_max_w, max(gap_based_w, pressure_based_w)),
            )
            if previous_headroom_discharge and previous_parallel_val > 0:
                if headroom_discharge_target_w > previous_parallel_val:
                    headroom_discharge_w = min(
                        headroom_discharge_target_w,
                        previous_parallel_val + self.headroom_discharge_step_w,
                    )
                else:
                    headroom_discharge_w = max(
                        headroom_discharge_target_w,
                        previous_parallel_val - self.headroom_discharge_step_w,
                    )
            else:
                headroom_discharge_w = min(
                    headroom_discharge_target_w,
                    self.headroom_discharge_min_w + self.headroom_discharge_step_w,
                )
            headroom_discharge_w = max(
                0,
                min(headroom_discharge_w, headroom_discharge_max_w),
            )
            headroom_discharge_active = headroom_discharge_w >= self.headroom_discharge_min_w

        def choose(state: str, mode: int, val: int, reason: str, priority: str) -> ParallelDecision:
            decision = ParallelDecision(state, mode, max(0, int(val)), reason, priority)
            trace.append({
                "step": state,
                "mode": _mode_name(mode),
                "val": decision.val,
                "priority": priority,
                "reason": reason,
            })
            return decision

        trace.append({
            "step": "inputs",
            "soc": round(soc, 2),
            "curve_soc": None if curve_soc is None else round(curve_soc, 2),
            "curve_gap_pct": None if curve_gap_pct is None else round(curve_gap_pct, 2),
            "curve_above_pct": round(curve_above_pct, 2),
            "adaptive_curve_active": adaptive_curve_active,
            "adaptive_curve_relation": adaptive_curve_relation,
            "adaptive_soc_floor": None if adaptive_floor_soc is None else round(float(adaptive_floor_soc), 2),
            "adaptive_soc_ceiling": None if adaptive_ceiling_soc is None else round(float(adaptive_ceiling_soc), 2),
            "can_reach_target": can_reach_target,
            "max_reachable_soc": None if max_reachable_soc < 0.0 else round(max_reachable_soc, 2),
            "forecast_shortfall_pv_release_active": forecast_shortfall_pv_release_active,
            "shortfall_pv_catchup_active": shortfall_pv_catchup_active,
            "shortfall_target_soc": active_state.get("shortfall_target_soc"),
            "shortfall_target_gap_pct": active_state.get("shortfall_target_gap_pct"),
            "shortfall_real_surplus_w": active_state.get("shortfall_real_surplus_w"),
            "shortfall_catchup_curve_pressure": active_state.get("shortfall_catchup_curve_pressure"),
            "shortfall_catchup_blocked_curve_ready": active_state.get("shortfall_catchup_blocked_curve_ready"),
            "shortfall_catchup_blocked_low_surplus": active_state.get("shortfall_catchup_blocked_low_surplus"),
            "shortfall_release_margin_pct": round(shortfall_release_margin_pct, 2),
            "forecast_curve_landing_hold_active": forecast_curve_landing_hold_active,
            "forecast_floor_target_gap_pct": round(forecast_floor_target_gap_pct, 3),
            "forecast_landing_margin_pct": round(forecast_landing_margin_pct, 3),
            "sliding_horizon_active": sliding_horizon_active,
            "sliding_horizon_candidate_active": sliding_horizon_candidate_active,
            "sliding_horizon_corridor_veto": sliding_horizon_corridor_veto,
            "sliding_horizon_reason": sliding_horizon_reason,
            "sliding_horizon_confidence": round(sliding_horizon_confidence, 4),
            "sliding_horizon_min_confidence": round(sliding_horizon_min_confidence, 4),
            "sliding_horizon_season": sliding_horizon_season,
            "sliding_horizon_minutes_until_latest_charge": sliding_horizon_minutes_until_latest_charge,
            "sliding_horizon_headroom_available_wh": round(sliding_horizon_headroom_available_wh, 0),
            "sliding_horizon_uncovered_pressure_wh": round(sliding_horizon_uncovered_pressure_wh, 0),
            "sliding_horizon_uncovered_curtailment_pressure_wh": round(
                sliding_horizon_uncovered_curtailment_pressure_wh,
                0,
            ),
            "headroom_reserve_active": headroom_reserve_active,
            "headroom_reserve_pressure_wh": round(headroom_reserve_pressure_wh, 0),
            "headroom_reserve_source": headroom_reserve_source,
            "headroom_discharge_pressure_wh": round(headroom_discharge_pressure_wh, 0),
            "headroom_execution_schema_version": headroom_execution.get("schema_version"),
            "headroom_execution_allowed": headroom_execution_allowed,
            "headroom_execution_reason_code": headroom_execution_reason,
            "headroom_execution_plan_id": headroom_execution.get("plan_id"),
            "headroom_execution_slot_id": headroom_execution.get("slot_id"),
            "headroom_execution_earliest_start_ts": headroom_execution.get("earliest_start_ts"),
            "headroom_execution_deadline_ts": headroom_execution.get("deadline_ts"),
            "headroom_execution_target_soc": headroom_execution.get("target_soc"),
            "headroom_execution_hard_floor_soc": headroom_execution.get("hard_floor_soc"),
            "headroom_execution_plan_accounted_wh": headroom_execution.get("plan_accounted_wh", 0.0),
            "headroom_execution_slot_accounted_wh": headroom_execution.get("slot_accounted_wh", 0.0),
            "headroom_execution_residual_wh": headroom_execution_residual_wh,
            "headroom_execution_accounted_observed_w": headroom_execution.get("accounted_observed_w", 0.0),
            "headroom_execution_accounted_interval_s": headroom_execution.get("accounted_interval_s", 0.0),
            "headroom_execution_generation_reset": bool(headroom_execution.get("generation_reset", True)),
            "headroom_execution_last_account_ts": round(now_s, 3),
            "headroom_discharge_min_pressure_wh": round(self.headroom_discharge_min_pressure_wh, 0),
            "headroom_discharge_active": headroom_discharge_active,
            "headroom_discharge_blocked_reason": headroom_discharge_blocked_reason,
            "headroom_discharge_w": headroom_discharge_w,
            "headroom_discharge_target_w": headroom_discharge_target_w,
            "headroom_discharge_floor_soc": (
                None
                if headroom_discharge_floor_soc is None
                else round(float(headroom_discharge_floor_soc), 2)
            ),
            "headroom_discharge_gap_pct": round(headroom_discharge_gap_pct, 2),
            "headroom_discharge_enter_pct": round(self.headroom_discharge_enter_pct, 2),
            "headroom_discharge_keep_pct": round(self.headroom_discharge_keep_pct, 2),
            "headroom_discharge_export_room_w": headroom_discharge_export_room_w,
            "headroom_discharge_export_margin_w": self.headroom_discharge_export_margin_w,
            "headroom_discharge_import_guard_w": self.headroom_discharge_import_guard_w,
            "headroom_discharge_abregel_blocked": headroom_discharge_abregel_blocked,
            "headroom_discharge_day": headroom_discharge_day,
            "headroom_discharge_today_wh": round(headroom_discharge_today_wh, 1),
            "headroom_discharge_daily_limit_wh": round(headroom_discharge_daily_limit_wh, 1),
            "headroom_discharge_daily_remaining_wh": round(headroom_discharge_daily_remaining_wh, 1),
            "headroom_discharge_daily_limit_pct": round(self.headroom_discharge_daily_limit_pct, 2),
            "headroom_discharge_daily_blocked": headroom_discharge_daily_blocked,
            "headroom_discharge_cooldown_s": self.headroom_discharge_cooldown_s,
            "headroom_discharge_cooldown_remaining_s": round(headroom_discharge_cooldown_remaining_s, 1),
            "headroom_discharge_cooldown_active": headroom_discharge_cooldown_active,
            "headroom_discharge_last_active_ts": round(headroom_discharge_last_active_ts, 1) if headroom_discharge_last_active_ts > 0 else 0,
            "headroom_discharge_last_account_ts": round(now_s, 1),
            "curve_guard_active": curve_guard_active,
            "first_curve_ts": int(first_curve_ts) if first_curve_ts else 0,
            "first_curve_soc": None if first_curve_soc is None else round(first_curve_soc, 2),
            "pre_curve_hold_raw_active": pre_curve_hold_raw_active,
            "pre_curve_hold_active": pre_curve_hold_active,
            "pre_curve_ifc_start_active": pre_curve_ifc_start_active,
            "pre_curve_ifc_start_w": self.pre_curve_ifc_start_w,
            "pre_curve_hold_margin_pct": round(pre_curve_hold_margin_pct, 2),
            "pv_w": pv_w,
            "grid_ema_w": grid_ema_w,
            "grid_relief_enter_w": self.grid_relief_enter_w,
            "grid_house_fallback_w": grid_house_fallback_w,
            "home_w": home_w,
            "home_rule_w": home_rule_w,
            "wp_w": wp_w,
            "wallbox_w": round(wallbox_rule_w, 1),
            "wallbox_actual_w": round(wallbox_w, 1),
            "wallbox_home_includes": wallbox_home_includes,
            "wallbox_ngna": bool(wallbox_ngna),
            "wb_modes": wb_modes,
            "wb_configured_ids": sorted(wb_configured_ids),
            "wb_active_mode_ids": sorted(wb_active_mode_ids),
            "wb_runtime_topology_valid": bool(wb_runtime_topology_valid),
            "wb_storage_floor_active": wb_storage_floor_active,
            "wb_storage_floor_requested": wb_storage_floor_requested,
            "wb_possible_w": wb_possible_w,
            "wb_budget_w": wb_budget_w,
            "previous_parallel_state": previous_parallel_state,
            "last_auto_age_s": round(auto_hold_age_s, 1) if last_auto_ts > 0 else None,
            "auto_hold_s": self.auto_hold_s,
            "curve_auto_hold_release_below_pct": self.curve_auto_hold_release_below_pct,
            "curve_settle_hold_active": curve_settle_hold_active,
            "curve_crossed_from_charge_hold": curve_crossed_from_charge_hold,
            "curve_near_idle_hold": curve_near_idle_hold,
            "curve_edge_export_keep_active": curve_edge_export_keep_active,
            "curve_edge_soft_charge_active": curve_edge_soft_charge_active,
            "curve_edge_soft_charge_w": curve_edge_soft_charge_w,
            "curve_edge_soft_hold_s": self.curve_edge_soft_hold_s,
            "curve_edge_soft_factor": round(self.curve_edge_soft_factor, 3),
            "wb_auto_grid_abort_w": self.wb_auto_grid_abort_w,
            "wb_active_enter": wb_active_enter,
            "wb_intent_active": wb_intent_active,
            "wb_intent_physical_charge_active": wb_intent_physical_charge_active,
            "wb_intent_authorized_start_active": wb_intent_authorized_start_active,
            "wb_intent_car_present": wb_intent_car_present,
            "wb_intent_bev_full_blocked": wb_intent_bev_full_blocked,
            "wb_budget_signal_active": wb_budget_signal_active,
            "wb_active_keep": wb_active_keep,
            "wb_capacity_sink_active": wb_capacity_sink_active,
            "last_wb_active_age_s": round(last_wb_active_age_s, 1) if last_wb_active_ts > 0 else None,
            "wb_auto_enter_margin_w": wb_auto_enter_margin_w,
            "wb_auto_keep_margin_w": wb_auto_keep_margin_w,
            "wb_owner_real_min_w": round(wb_owner_real_min_w, 1),
            "wb_real_owner_active": wb_real_owner_active,
            "wb_internal_owner_allowed": wb_internal_owner_allowed,
            "wb_grid_abort": wb_grid_abort,
            "wb_auto_enter": wb_auto_enter,
            "wb_auto_keep": wb_auto_keep,
            "night_floor_enter": night_floor_enter,
            "night_floor_keep": night_floor_keep,
            "curve_charge_enter_w": self.curve_charge_enter_w,
            "curve_charge_keep_w": self.curve_charge_keep_w,
            "curve_charge_reenter_w": self.curve_charge_reenter_w,
            "curve_guard_enter_below_pct": self.curve_guard_enter_below_pct,
            "curve_auto_hold_exit_pct": self.curve_auto_hold_exit_pct,
            "curve_above_enter": curve_above_enter,
            "curve_above_keep": curve_above_keep,
            "curve_above_soft": curve_above_soft,
            "curve_soft_charge_active": curve_soft_charge_active,
            "curve_soft_charge_factor": round(curve_soft_factor, 3),
            "curve_soft_charge_limit_w": curve_soft_charge_limit_w,
            "curve_cap_target_w": curve_cap_target_w,
            "curve_cap_below_curve_catchup_active": curve_cap_below_curve_catchup_active,
            "curve_cap_below_curve_catchup_w": curve_cap_below_curve_catchup_w,
            "curve_cap_active": curve_cap_active,
            "curve_cap_keep_active": curve_cap_keep_active,
            "curve_cap_neutral_keep": curve_cap_neutral_keep,
            "curve_cap_short_hold_s": self.curve_cap_short_hold_s,
            "curve_cap_short_hold_active": curve_cap_short_hold_active,
            "curve_cap_proactive_active": curve_cap_proactive_active,
            "curve_cap_blocked_by_wb_capacity": curve_cap_blocked_by_wb_capacity,
            "curve_cap_blocked_by_wb_grid": curve_cap_blocked_by_wb_grid,
            "curve_cap_export_room_w": curve_cap_export_room_w,
            "curve_cap_excess_charge_w": curve_cap_excess_charge_w,
            "curve_cap_measured_charge_w": curve_cap_measured_charge_w,
            "meter_balance_home_w": meter_balance["home_balance_w"],
            "meter_balance_error_w": meter_balance["home_balance_error_w"],
            "meter_balance_error_limit_w": meter_balance["home_balance_error_limit_w"],
            "meter_balance_plausible": meter_balance_plausible,
            "curve_cap_pressure_w": curve_cap_pressure_w,
            "curve_cap_physical_pressure_w": curve_cap_physical_pressure_w,
            "curve_cap_dc_pressure_w": curve_cap_dc_pressure_w,
            "curve_cap_dc_pressure_active": curve_cap_dc_pressure_active,
            "curve_cap_dc_hold_active": curve_cap_dc_hold_active,
            "curve_cap_dc_hold_margin_w": curve_cap_dc_hold_margin_w,
            "curve_cap_direct_pressure_w": curve_cap_direct_pressure_w,
            "curve_cap_model_pressure_w": curve_cap_model_pressure_w,
            "curve_cap_grid_pressure_w": curve_cap_grid_pressure_w,
            "curve_cap_grid_export_over_limit_w": grid_export_over_limit_w,
            "curve_cap_current_feed_pressure_w": current_feed_pressure_w,
            "curve_cap_projected_export_without_charge_w": projected_export_without_charge_w,
            "curve_cap_projected_export_threshold_w": projected_export_threshold_w,
            "curve_cap_projected_export_over_limit_w": projected_export_over_limit_w,
            "curve_cap_feed_export_threshold_w": feed_export_threshold_w,
            "curve_cap_release_export_threshold_w": release_export_threshold_w,
            "curve_cap_feed_buffer_w": self.curve_cap_feed_buffer_w,
            "curve_cap_release_band_w": self.curve_cap_release_band_w,
            "curve_cap_feedback_band_w": self.curve_cap_feedback_band_w,
            "curve_cap_release_hysteresis_w": self.curve_cap_release_hysteresis_w,
            "curve_cap_release_grace_s": self.curve_cap_release_grace_s,
            "curve_cap_grid_contract_valid": curve_cap_grid_contract_valid,
            "curve_cap_real_grid_import_active": curve_cap_real_grid_import_active,
            "curve_cap_release_below_active": curve_cap_release_below_active,
            "curve_cap_release_below_since_ts": curve_cap_release_below_since_ts,
            "curve_cap_release_elapsed_s": curve_cap_release_elapsed_s,
            "curve_cap_release_grace_active": curve_cap_release_grace_active,
            "curve_cap_release_ramp_active": curve_cap_release_ramp_active,
            "curve_cap_hysteresis_hold_active": curve_cap_hysteresis_hold_active,
            "curve_cap_hysteresis_amount_follow_active": curve_cap_hysteresis_amount_follow_active,
            "curve_cap_hysteresis_floor_w": curve_cap_hysteresis_floor_w,
            "curve_cap_invalid_hold_active": curve_cap_invalid_hold_active,
            "curve_cap_release_phase": curve_cap_release_phase,
            "curve_cap_release_pending": curve_cap_release_pending,
            "curve_cap_release_requested": curve_cap_release_requested,
            "curve_cap_release_confirmed_since_ts": curve_cap_release_confirmed_since_ts,
            "curve_cap_post_release_until_ts": curve_cap_post_release_until_ts,
            "curve_cap_post_release_guard_active": curve_cap_post_release_guard_active,
            "curve_cap_post_release_reentry_blocked": curve_cap_post_release_reentry_blocked,
            "curve_cap_settings_readback_valid": settings_readback_valid,
            "curve_cap_settings_previous_cap_confirmed": settings_previous_curve_cap_confirmed,
            "curve_cap_settings_bounded_zero_confirmed": settings_bounded_zero_confirmed,
            "curve_cap_settings_release_confirmed": settings_release_confirmed,
            "curve_cap_bounded_zero_w": curve_cap_bounded_zero_w,
            "curve_cap_min_charge_w": curve_cap_bounded_zero_w,
            "curve_cap_hard_pressure_active": curve_cap_hard_pressure_active,
            "curve_cap_feedback_active": curve_cap_feedback_active,
            "curve_cap_grid_export_error_w": grid_export_error_w,
            "curve_cap_below_threshold_w": curve_cap_below_threshold_w,
            "inverter_limit_w": inverter_limit_w,
            "inverter_pressure_w": inverter_pressure_w,
            "derate_limit_w": derate_limit_w,
            "derate_hard_limit_w": derate_hard_limit_w,
            "derate_limit_source": derate_limit_source,
            "configured_export_limit_w": configured_export_limit_w,
            "live_derate_limit_w": live_derate_limit_w,
            "derating_pressure_w": derating_pressure_w,
            "ems_mandatory_charge_w": ems_mandatory_charge_w,
            "ems_pv_total_w": ems_budget["pv_total_w"],
            "ems_e3dc_dc_pv_w": ems_budget["e3dc_dc_pv_w"],
            "ems_e3dc_ac_available_w": ems_budget["e3dc_ac_available_w"],
            "ems_external_ac_pv_w": ems_budget["external_ac_pv_w"],
            "ems_external_ac_pv_trusted": ems_budget["external_ac_pv_trusted"],
            "ems_external_ac_pv_source": ems_budget["external_ac_pv_source"],
            "ems_ac_available_w": ems_budget["ac_available_w"],
            "ems_wallbox_budget_total_w": ems_budget["wallbox_budget_total_w"],
            "ems_wallbox_budget_increase_w": ems_budget["wallbox_budget_increase_w"],
            "ems_curtailment_loss_w": ems_budget["curtailment_loss_w"],
            "curve_cap_export_trigger_w": self.curve_cap_export_trigger_w,
            "curve_cap_pv_surplus_w": curve_cap_pv_surplus_w,
            "curve_cap_margin_w": curve_cap_margin_w,
            "curve_cap_step_w": self.curve_cap_step_w,
            "curve_charge_keep": curve_charge_keep,
            "curve_no_surplus_keep": curve_no_surplus_keep,
            "price_house_discharge_enter_w": self.price_house_discharge_enter_w,
            "price_house_discharge_keep_w": self.price_house_discharge_keep_w,
            "price_house_discharge_keep": price_house_discharge_keep,
            "planned_load_confirmed": bool(active_state.get("planned_load_confirmed")),
            "planned_load_expected_w": _safe_int(active_state.get("planned_load_expected_w"), 0),
            "planned_load_observed_extra_w": _safe_int(active_state.get("planned_load_observed_extra_w"), 0),
            "planned_load_mode": active_state.get("planned_load_mode", ""),
            "planned_load_support_allowed": bool(active_state.get("planned_load_support_allowed")),
            "planned_load_support_reason": active_state.get("planned_load_support_reason", ""),
            "planned_load_support": active_state.get("planned_load_support", {}),
            "iFc_w": i_fc_w,
            "iMinLade_w": i_min_lade_w,
            "curve_gap_pct": _safe_float(active_state.get("curve_gap_pct"), 0.0),
            "curve_gap_catchup_w": _safe_int(active_state.get("curve_gap_catchup_w"), 0),
            "curve_gap_catchup_cap_w": _safe_int(active_state.get("curve_gap_catchup_cap_w"), 0),
            "curve_gap_catchup_factor": _safe_float(active_state.get("curve_gap_catchup_factor"), 0.0),
            "curve_gap_catchup_min_w": _safe_int(active_state.get("curve_gap_catchup_min_w"), 0),
            "curve_gap_catchup_taper_pct": _safe_float(active_state.get("curve_gap_catchup_taper_pct"), 0.0),
            "curve_need_raw_w": _safe_int(active_state.get("curve_need_raw_w"), 0),
            "lookahead_need_w": _safe_int(active_state.get("lookahead_need_w"), 0),
            "curve_export_w": curve_export_w,
            "curve_safe_charge_w": curve_safe_charge_w,
            "curve_ifc_export_catchup_active": curve_ifc_export_catchup_active,
            "curve_ifc_export_catchup_floor_w": curve_ifc_export_catchup_floor_w,
            "curve_ifc_export_catchup_w": curve_ifc_export_catchup_w,
            "previous_parallel_mode": _mode_name(previous_parallel_mode),
            "previous_parallel_val": previous_parallel_val,
            "previous_parallel_age_s": (
                round(previous_parallel_age_s, 1)
                if previous_parallel_ts > 0
                else None
            ),
            "previous_decision_age_s": (
                round(previous_decision_age_s, 1)
                if previous_decision_ts > 0
                else None
            ),
        })

        if active_state_name == "no_data" or active_mode < 0:
            decision = choose("parallel_no_data", -1, 0, "Keine gueltigen Live-Daten", "failsafe")
        elif any(active_state_name.startswith(prefix) for prefix in self.PASSTHROUGH_STATES):
            decision = choose(
                "parallel_passthrough",
                active_mode,
                active_val,
                "Aktiver Schutz-/Handpfad wird im Shadow-Modus gespiegelt",
                "protected",
            )
        elif active_state_name == "emergency_power":
            decision = choose(
                "parallel_emergency_auto",
                MODE_AUTO,
                self.max_charge_w,
                "Notstrom: E3DC autonom lassen",
                "safety",
            )
        elif active_state_name == "price_boost_grid":
            decision = choose(
                "parallel_price_grid",
                MODE_GRID,
                max(active_val, 300),
                "Preisfenster fordert Netzladen",
                "price",
            )
        elif active_state_name == "planned_load_storage_hold":
            self.price_house_discharge_w = 0
            expected_w = _safe_int(active_state.get("planned_load_expected_w"), 0)
            observed_w = _safe_int(active_state.get("planned_load_observed_extra_w"), 0)
            decision = choose(
                "parallel_planned_load_hold",
                MODE_AUTO,
                self.max_charge_w,
                (
                    "Geplante externe Last erkannt: Speicherentladung per EMS-Limit sperren "
                    f"(erwartet {expected_w}W, erkannt {observed_w}W)"
                ),
                "load",
            )
        elif active_state_name == "planned_load_price_support":
            support = active_state.get("planned_load_support", {})
            if not isinstance(support, dict):
                support = {}
            expected_w = _safe_int(active_state.get("planned_load_expected_w"), 0)
            observed_w = _safe_int(active_state.get("planned_load_observed_extra_w"), 0)
            support_w = max(
                300,
                min(
                    self.max_discharge_w,
                    _safe_int(
                        support.get(
                            "support_max_discharge_w",
                            min(expected_w, observed_w if observed_w > 0 else expected_w),
                        ),
                        0,
                    ),
                ),
            )
            decision = choose(
                "parallel_planned_load_price_support",
                MODE_AUTO,
                support_w,
                (
                    "Geplante externe Last preisgefuehrt stuetzen: "
                    f"Entladung bis {support_w}W erlaubt "
                    f"(erwartet {expected_w}W, erkannt {observed_w}W)"
                ),
                "load",
            )
        elif active_state_name == "price_plan_storage_hold":
            if price_curve_need_w > 0 and price_export_w > 500:
                self.price_house_discharge_w = 0
                decision = choose(
                    "parallel_price_auto",
                    MODE_AUTO,
                    self.max_charge_w,
                    "Preis-/Slotfenster: PV-Ueberschuss darf Speicher entlang der Kurve laden",
                    "price",
                )
            elif (
                (house_deficit_w >= self.price_house_discharge_enter_w or price_house_discharge_keep)
                and soc > reserve_soc
            ):
                target_house_w = min(1500, max(200, int(fixed_load_w + 30)))
                self.price_house_discharge_w = target_house_w
                decision = choose(
                    "parallel_price_house_discharge",
                    MODE_AUTO,
                    int(self.price_house_discharge_w),
                    "Preis-/Slotfenster: Auto darf Netz nutzen, Haus/WP/Klima wird per E3DC-AUTO begrenzt aus Akku gestuetzt",
                    "price",
                )
            else:
                self.price_house_discharge_w = 0
                decision = choose(
                    "parallel_price_hold",
                    MODE_AUTO,
                    0,
                    "Preis-/Slotfenster: Speicherentladung per EMS-Limit sperren, Auto nutzt Netz",
                    "price",
                )
        elif active_state_name == "price_plan_house_discharge":
            target_house_w = min(self.max_discharge_w, max(self.price_house_discharge_enter_w, int(house_deficit_w + 150)))
            decision = choose(
                "parallel_price_house_discharge",
                MODE_AUTO,
                max(active_val, target_house_w),
                "Teures Preisfenster: Haus/WP/Klima per E3DC-AUTO begrenzt aus Speicher stuetzen",
                "price",
            )
        elif active_state_name == "evening_release" and not curve_cap_hard_pressure_active and not shortfall_pv_catchup_active:
            self.price_house_discharge_w = 0
            decision = choose(
                "parallel_evening_release",
                MODE_AUTO,
                self.max_charge_w,
                "Freilauf erreicht: EMS-Grenzen freigeben, E3DC uebernimmt Rest-PV und Nachtversorgung",
                "default",
            )
        elif curve_cap_active:
            self.price_house_discharge_w = 0
            curve_cap_reason = (
                "SOC unterhalb Kurve; sichere PV-Einspeisung hebt den "
                "Abregelrahmen bis zum Kurvenbedarf an"
                if curve_cap_below_curve_catchup_active
                else "SOC oberhalb Kurve; Batterieladung auf die benötigte Rampenleistung begrenzen"
            )
            decision = choose(
                "parallel_curve_charge_cap",
                MODE_CHRG,
                curve_cap_target_w,
                curve_cap_reason,
                "curve",
            )
        elif headroom_discharge_active:
            self.price_house_discharge_w = 0
            decision = choose(
                "parallel_headroom_discharge",
                MODE_DISCH,
                headroom_discharge_w,
                (
                    "Abregel-Headroom: SoC liegt %.1f Prozentpunkte ueber der "
                    "Untergrenze; Platz-Schaff-Entladung %dW, Abregelschutz "
                    "ueberstimmt sofort"
                ) % (headroom_discharge_gap_pct, headroom_discharge_w),
                "headroom",
            )
        elif (wb_auto_enter or wb_auto_keep) and wb_real_owner_active:
            self.price_house_discharge_w = 0
            if i_fc_w > 0 and not shortfall_pv_catchup_active:
                wb_charge_cap_w = int(min(self.max_charge_w, max(self.curve_charge_enter_w, i_fc_w)))
                decision = choose(
                    "parallel_wb_auto",
                    MODE_AUTO,
                    wb_charge_cap_w,
                    f"Wallbox aktiv: E3DC-AUTO mit iFc-Führung {wb_charge_cap_w}W begrenzt, Entladestützung frei",
                    "wallbox",
                )
            else:
                decision = choose(
                    "parallel_wb_auto",
                    MODE_AUTO,
                    self.max_charge_w,
                    "Wallbox aktiv: E3DC-AUTO frei",
                    "wallbox",
                )
        elif pre_curve_ifc_start_active:
            self.price_house_discharge_w = 0
            decision = choose(
                "parallel_curve_charge",
                MODE_CHRG,
                min(self.max_charge_w, max(self.curve_charge_enter_w, i_fc_w)),
                (
                    "Vor Kurvenstart: hoher iFc hebt den Start-Hold bewusst auf; "
                    "Speicherladung darf dem berechneten Bedarf folgen"
                ),
                "curve_pressure",
            )
        elif pre_curve_hold_active:
            self.price_house_discharge_w = 0
            decision = choose(
                "parallel_curve_auto_hold",
                MODE_AUTO,
                self.max_charge_w,
                "Vor Kurvenstart: Start-SoC erreicht, E3DC-AUTO mit Ladegrenze 0W bis die Kurve beginnt",
                "curve",
            )
        elif active_state_name == "evening_release" and not shortfall_pv_catchup_active:
            self.price_house_discharge_w = 0
            decision = choose(
                "parallel_evening_release",
                MODE_AUTO,
                self.max_charge_w,
                "Freilauf erreicht: EMS-Grenzen freigeben, E3DC uebernimmt Rest-PV und Nachtversorgung",
                "default",
            )
        elif forecast_shortfall_pv_release_active:
            self.price_house_discharge_w = 0
            decision = choose(
                "parallel_auto",
                MODE_AUTO,
                self.max_charge_w,
                (
                    "Tagesziel laut Prognose nicht erreichbar: Komfort-/Headroom-Kante sperrt "
                    "keine reale PV-Ladung, E3DC darf den Überschuss autonom mitnehmen"
                ),
                "forecast_shortfall",
            )
        elif forecast_curve_landing_hold_active:
            self.price_house_discharge_w = 0
            decision = choose(
                "parallel_curve_auto_hold",
                MODE_AUTO,
                self.max_charge_w,
                (
                    "Prognose-100-Landevertrag: Sollkurve ist erreicht und Tagesziel bleibt "
                    "erreichbar; EMS-Ladegrenze 0W haelt Speicherplatz bis zum Freilauf frei"
                ),
                "curve",
            )
        elif sliding_horizon_active:
            self.price_house_discharge_w = 0
            decision = choose(
                "parallel_curve_auto_hold",
                MODE_AUTO,
                self.max_charge_w,
                (
                    "Gleitender Prognosehorizont: Rest-PV deckt das Tagesziel vor dem spätesten "
                    "Ladebeginn, Speicherladung wird entspannt"
                ),
                "curve",
            )
        elif curve_edge_soft_charge_active:
            self.price_house_discharge_w = 0
            decision = choose(
                "parallel_curve_charge",
                MODE_CHRG,
                curve_edge_soft_charge_w,
                (
                    "Kurvenkante weich geführt: laufende Kurvenladung wird gedämpft "
                    "weitergeführt statt auf 0W zu springen"
                ),
                "curve",
            )
        elif (
            curve_settle_hold_active
            or curve_crossed_from_charge_hold
            or curve_near_idle_hold
            or curve_edge_export_keep_active
            or curve_above_soft
            or curve_above_keep
        ):
            self.price_house_discharge_w = 0
            if headroom_reserve_active:
                curve_hold_reason = (
                    "Abregelreserve aktiv: Speicherplatz fuer PV-Spitzen freihalten; "
                    "echter Netz-/WR-Druck bleibt Pflichtladung"
                )
            else:
                curve_hold_reason = (
                    "Kurvenkante stabilisiert; EMS-Ladegrenze bleibt 0W bis der Speicher unter die untere Hysterese faellt"
                    if (
                        curve_settle_hold_active
                        or curve_crossed_from_charge_hold
                        or curve_near_idle_hold
                        or curve_edge_export_keep_active
                    ) and not (curve_above_soft or curve_above_keep)
                    else "SOC oberhalb Kurve; Speicherladung auf 0 W begrenzen und erst bei echtem Netz-/Abregelrisiko eingreifen"
                )
            decision = choose(
                "parallel_curve_auto_hold",
                MODE_AUTO,
                self.max_charge_w,
                curve_hold_reason,
                "curve",
            )
        elif (wb_auto_enter or wb_auto_keep) and wb_real_owner_active:
            self.price_house_discharge_w = 0
            if i_fc_w > 0 and not shortfall_pv_catchup_active:
                wb_charge_cap_w = int(min(self.max_charge_w, max(self.curve_charge_enter_w, i_fc_w)))
                decision = choose(
                    "parallel_wb_auto",
                    MODE_AUTO,
                    wb_charge_cap_w,
                    f"Wallbox aktiv: E3DC-AUTO mit iFc-Führung {wb_charge_cap_w}W begrenzt, Entladestützung frei",
                    "wallbox",
                )
            else:
                decision = choose(
                    "parallel_wb_auto",
                    MODE_AUTO,
                    self.max_charge_w,
                    "Wallbox aktiv: E3DC-AUTO frei",
                    "wallbox",
                )
        elif curve_guard_active and i_fc_w >= self.max_charge_w - 50:
            self.price_house_discharge_w = 0
            if curve_soft_charge_active and curve_soft_charge_limit_w < self.max_charge_w - 50:
                soft_charge_w = min(i_fc_w, self.max_charge_w, curve_soft_charge_limit_w)
                if soft_charge_w >= self.curve_charge_enter_w:
                    decision = choose(
                        "parallel_curve_charge",
                        MODE_CHRG,
                        max(self.curve_charge_enter_w, soft_charge_w),
                        "Kurve fordert maximale Ladung; oberhalb der aktuellen Kurve proportional gedämpft",
                        "curve",
                    )
                else:
                    decision = choose(
                        "parallel_curve_auto_hold",
                        MODE_AUTO,
                        self.max_charge_w,
                        "Kurve fordert maximale Ladung; SoC liegt oberhalb der Kurve, Ladefreigabe weich auf 0 gedämpft",
                        "curve",
                    )
            else:
                decision = choose(
                    "parallel_curve_auto_charge",
                    MODE_AUTO,
                    self.max_charge_w,
                    "Kurve fordert maximale Ladung",
                    "curve",
                )
        elif curve_guard_active and (i_fc_w >= self.curve_charge_enter_w or curve_charge_keep or curve_no_surplus_keep):
            self.price_house_discharge_w = 0
            curve_request_cap_w = curve_soft_charge_limit_w if curve_soft_charge_active else self.max_charge_w
            curve_request_w = min(max(i_fc_w, self.curve_charge_keep_w), self.max_charge_w, curve_request_cap_w)
            curve_charge_w = min(curve_request_w, curve_safe_charge_w)
            curve_charge_floor_w = (
                self.curve_charge_reenter_w
                if previous_parallel_state == "parallel_curve_auto_no_surplus"
                else self.curve_charge_keep_w
                if curve_charge_keep
                else self.curve_charge_enter_w
            )
            curve_safe_reenter_w = (
                self.curve_charge_reenter_w
                if previous_parallel_state == "parallel_curve_auto_no_surplus"
                else self.curve_charge_enter_w
            )
            if curve_charge_w >= curve_charge_floor_w and curve_safe_charge_w >= curve_safe_reenter_w:
                curve_charge_reason = "Kurve lädt begrenzt nach; PV-/Netzpunkt-Kappe verhindert zu schnelles Füllen"
                curve_charge_priority = "curve"
                if shortfall_pv_catchup_active:
                    curve_charge_reason = (
                        "Abendziel-Rückstand bei realer Einspeisung: "
                        "Komfortband wird übersteuert und PV-Überschuss gespeichert"
                    )
                    curve_charge_priority = "forecast_shortfall"
                decision = choose(
                    "parallel_curve_charge",
                    MODE_CHRG,
                    max(self.curve_charge_enter_w, curve_charge_w),
                    curve_charge_reason,
                    curve_charge_priority,
                )
            else:
                decision = choose(
                    "parallel_curve_auto_no_surplus",
                    MODE_AUTO,
                    self.max_charge_w,
                    "Kurve fordert Ladung, aber keine sichere PV-/Exportreserve; E3DC autonom",
                    "curve",
                )
        elif curve_ifc_export_catchup_active:
            self.price_house_discharge_w = 0
            curve_charge_reason = "Kurve lädt Rückstand bei sicherer Einspeise-/PV-Reserve auf; EMS-Ladegrenze folgt der wirksamen Ladeanforderung"
            curve_charge_priority = "curve"
            if shortfall_pv_catchup_active:
                curve_charge_reason = (
                    "Abendziel-Rückstand bei realer Einspeisung: "
                    "Komfortband wird übersteuert und PV-Überschuss gespeichert"
                )
                curve_charge_priority = "forecast_shortfall"
            decision = choose(
                "parallel_curve_charge",
                MODE_CHRG,
                curve_ifc_export_catchup_w,
                curve_charge_reason,
                curve_charge_priority,
            )
        elif grid_ema_w >= self.grid_relief_enter_w and bat_w >= -100:
            self.price_house_discharge_w = 0
            decision = choose(
                "parallel_grid_relief_auto",
                MODE_AUTO,
                self.max_charge_w,
                "Netzbezug erkannt; Entladung durch E3DC-AUTO freigeben",
                "grid",
            )
        elif (
            adaptive_curve_relation == "below_floor"
            and i_fc_w >= self.curve_charge_enter_w
            and pv_after_fixed_w >= self.curve_charge_enter_w
            and curve_safe_charge_w < self.curve_charge_enter_w
            and grid_ema_w < self.grid_relief_enter_w
        ):
            self.price_house_discharge_w = 0
            decision = choose(
                "parallel_auto",
                MODE_AUTO,
                self.max_charge_w,
                (
                    "Kurve fordert Ladung, aber E3DC-AUTO nimmt den PV-Überschuss "
                    "bereits netzneutral auf; keine EMS-Ladegrenze nötig"
                ),
                "curve",
            )
        else:
            self.price_house_discharge_w = 0
            decision = choose(
                "parallel_auto",
                MODE_AUTO,
                self.max_charge_w,
                "Neutraler Zustand: E3DC autonom",
                "default",
            )

        curve_charge_servo_active = False
        curve_charge_servo_candidate_state = decision.state
        curve_charge_servo_block_reason = "disabled"
        curve_charge_servo_phase = ""
        curve_charge_servo_target_w = 0
        curve_charge_servo_frame_w = 0
        curve_charge_servo_previous_w = max(0, min(self.max_charge_w, previous_parallel_val))
        curve_charge_servo_available_w = max(
            0,
            min(
                self.max_charge_w,
                max(
                    curve_safe_charge_w,
                    curve_export_w,
                    pv_after_fixed_w,
                    max(0, bat_w),
                ),
            ),
        )
        curve_charge_servo_allowed_states = {
            "parallel_auto",
            "parallel_curve_auto_no_surplus",
            "parallel_curve_charge",
        }
        if self.curve_charge_servo_enabled:
            curve_charge_servo_block_reason = ""
            if previous_parallel_state != "parallel_curve_charge":
                curve_charge_servo_block_reason = "previous_not_curve_charge"
            elif curve_charge_servo_previous_w < self.curve_charge_servo_min_w:
                curve_charge_servo_block_reason = "previous_below_min"
            elif (
                self.curve_charge_servo_max_age_s > 0
                and (
                    previous_parallel_ts <= 0.0
                    or previous_parallel_age_s > self.curve_charge_servo_max_age_s
                )
            ):
                curve_charge_servo_block_reason = "previous_stale"
            elif decision.state not in curve_charge_servo_allowed_states:
                curve_charge_servo_block_reason = "candidate_not_eligible"
            elif decision.priority not in {"curve", "default"}:
                curve_charge_servo_block_reason = "priority_protected"
            elif (
                decision.priority in STORAGE_HARD_OVERRIDE_PRIORITIES
                or curve_cap_active
                or curve_cap_hard_pressure_active
                or headroom_discharge_active
                or headroom_reserve_active
                or forecast_curve_landing_hold_active
                or sliding_horizon_active
                or shortfall_pv_catchup_active
                or pre_curve_hold_active
                or curve_above_soft
                or curve_above_keep
            ):
                curve_charge_servo_block_reason = "curve_protection_active"
            elif pv_w <= 250:
                curve_charge_servo_block_reason = "no_pv"
            elif bat_w < -100:
                curve_charge_servo_block_reason = "battery_discharge"
            elif grid_ema_w >= self.grid_relief_enter_w or grid_w >= self.grid_relief_enter_w:
                curve_charge_servo_block_reason = "grid_import"
            elif curve_charge_servo_available_w < self.curve_charge_servo_min_w:
                curve_charge_servo_block_reason = "available_below_min"
            elif (
                decision.state != "parallel_curve_charge"
                and curve_charge_servo_available_w + self.curve_charge_servo_step_down_w
                < curve_charge_servo_previous_w
                and max(0, bat_w) < self.curve_charge_servo_min_w
            ):
                curve_charge_servo_block_reason = "available_drop"
            else:
                if decision.state == "parallel_curve_charge" and int(decision.val) > 0:
                    desired_servo_w = int(decision.val)
                else:
                    desired_servo_w = max(
                        self.curve_charge_servo_min_w,
                        curve_safe_charge_w,
                        min(curve_charge_servo_previous_w, curve_charge_servo_available_w),
                    )
                curve_charge_servo_target_w = max(
                    0,
                    min(self.max_charge_w, int(desired_servo_w)),
                )
                delta_w = curve_charge_servo_target_w - curve_charge_servo_previous_w
                if abs(delta_w) <= self.curve_charge_servo_deadband_w:
                    curve_charge_servo_frame_w = curve_charge_servo_previous_w
                    curve_charge_servo_phase = "hold"
                elif delta_w > 0:
                    curve_charge_servo_frame_w = min(
                        curve_charge_servo_target_w,
                        curve_charge_servo_previous_w + self.curve_charge_servo_step_up_w,
                    )
                    curve_charge_servo_phase = "ramp_up"
                else:
                    curve_charge_servo_frame_w = max(
                        curve_charge_servo_target_w,
                        curve_charge_servo_previous_w - self.curve_charge_servo_step_down_w,
                    )
                    curve_charge_servo_phase = "ramp_down"
                if (
                    curve_charge_servo_target_w >= self.curve_charge_servo_min_w
                    and 0 < curve_charge_servo_frame_w < self.curve_charge_servo_min_w
                ):
                    curve_charge_servo_frame_w = min(
                        curve_charge_servo_target_w,
                        self.curve_charge_servo_min_w,
                    )
                curve_charge_servo_frame_w = max(
                    0,
                    min(self.max_charge_w, int(curve_charge_servo_frame_w)),
                )
                if curve_charge_servo_frame_w >= self.curve_charge_servo_min_w:
                    curve_charge_servo_active = True
                    curve_charge_servo_block_reason = ""
                    trace.append({
                        "step": "curve_charge_servo",
                        "candidate": curve_charge_servo_candidate_state,
                        "phase": curve_charge_servo_phase,
                        "previous_w": curve_charge_servo_previous_w,
                        "target_w": curve_charge_servo_target_w,
                        "frame_w": curve_charge_servo_frame_w,
                        "available_w": curve_charge_servo_available_w,
                    })
                    decision = choose(
                        "parallel_curve_charge",
                        MODE_CHRG,
                        curve_charge_servo_frame_w,
                        (
                            "Kurven-Servo: laufender Laderahmen bleibt ruhig aktiv; "
                            "kleine Mess- und Prognosesprünge werden gedämpft"
                        ),
                        "curve",
                    )
                else:
                    curve_charge_servo_block_reason = "frame_below_min"

        auto_to_curve_charge_reentry = bool(
            previous_parallel_state == "parallel_auto"
            and decision.state == "parallel_curve_charge"
            and decision.priority == "curve"
            and curve_gap_pct is not None
            and curve_gap_pct >= self.curve_tolerance_pct
        )
        auto_hold_break = bool(
            decision.priority in STORAGE_HARD_OVERRIDE_PRIORITIES
            or decision.priority == "curve_pressure"
            or (
                forecast_curve_below_floor
                and decision.priority
                in {"curve", "forecast_shortfall"}
            )
            or decision.state == "parallel_curve_charge_cap"
            or (
                decision.state == "parallel_curve_charge"
                and not auto_to_curve_charge_reentry
                and int(decision.val) >= self.curve_charge_reenter_w
            )
            or grid_ema_w > max(1500, self.grid_limit_w * 10)
        )
        auto_hold_active = bool(
            self.auto_hold_s > 0
            and previous_parallel_state in AUTO_HOLD_STATES
            and last_auto_ts > 0
            and auto_hold_age_s < self.auto_hold_s
            and decision.mode != MODE_AUTO
            and not auto_hold_break
        )
        if auto_hold_active:
            remaining_s = max(1, int(round(self.auto_hold_s - auto_hold_age_s)))
            decision = choose(
                previous_parallel_state,
                MODE_AUTO,
                self.max_charge_w,
                "AUTO-Haltezeit: %s bleibt noch %ds aktiv, damit kurze Wolken-/Lastspruenge "
                "nicht zwischen AUTO und externer Vorgabe pendeln" % (
                    previous_parallel_state,
                    remaining_s,
                ),
                "auto_hold",
            )

        curve_under_ifc_charge_due = bool(
            curve_gap_pct is not None
            and curve_gap_pct > self.curve_auto_hold_exit_pct
            and i_fc_w >= self.curve_charge_keep_w
        )
        curve_charge_release_stabilize_active = bool(
            self.curve_charge_release_stabilize_s > 0
            and not forecast_curve_below_floor
            and previous_parallel_state == "parallel_curve_charge"
            and decision.state == "parallel_auto"
            and decision.priority == "default"
            and previous_parallel_age_s < self.curve_charge_release_stabilize_s
            and pv_w > 250
            and grid_ema_w < self.grid_relief_enter_w
            and not shortfall_pv_catchup_active
            and not curve_ifc_export_catchup_active
            and not curve_under_ifc_charge_due
            and (
                headroom_reserve_active
                or i_fc_w >= self.curve_charge_keep_w
                or grid_export_w >= self.curve_charge_keep_w
                or curve_safe_charge_w >= self.curve_charge_enter_w
                or max(0, bat_w) >= self.curve_charge_keep_w
            )
        )
        curve_charge_soc_step_hold_active = False
        if curve_charge_release_stabilize_active:
            remaining_s = max(1, int(round(self.curve_charge_release_stabilize_s - previous_parallel_age_s)))
            decision = choose(
                "parallel_curve_auto_hold",
                MODE_AUTO,
                self.max_charge_w,
                (
                    "Kurvenladung stabilisiert: AUTO-Freilauf bleibt noch "
                    "%ds gesperrt, solange PV-/Kurvendruck sichtbar ist"
                ) % remaining_s,
                "curve",
            )

        curve_charge_soc_step_hold_active = bool(
            not curve_charge_release_stabilize_active
            and not forecast_curve_below_floor
            and self.curve_charge_soc_step_hold_s > 0
            and previous_parallel_state == "parallel_curve_charge"
            and decision.state == "parallel_auto"
            and decision.priority == "default"
            and previous_parallel_age_s < self.curve_charge_soc_step_hold_s
            and curve_gap_pct is not None
            and curve_gap_pct <= self.curve_auto_hold_release_below_pct
            and i_fc_w < self.curve_charge_enter_w
            and pv_w > 250
            and (
                grid_ema_w < self.grid_relief_enter_w
                or grid_w < self.grid_relief_enter_w
                or grid_export_w >= self.curve_charge_keep_w
                or pv_after_fixed_w >= self.curve_charge_enter_w
            )
            and not headroom_reserve_active
            and not forecast_curve_landing_hold_active
            and not sliding_horizon_active
            and not shortfall_pv_catchup_active
            and not curve_ifc_export_catchup_active
            and (
                previous_parallel_val >= self.curve_charge_keep_w
                or grid_export_w >= self.curve_charge_keep_w
                or curve_safe_charge_w >= self.curve_charge_enter_w
                or max(0, bat_w) >= self.curve_charge_keep_w
                or pv_after_fixed_w >= self.curve_charge_enter_w
            )
        )
        if curve_charge_soc_step_hold_active:
            remaining_s = max(1, int(round(self.curve_charge_soc_step_hold_s - previous_parallel_age_s)))
            decision = choose(
                "parallel_curve_auto_hold",
                MODE_AUTO,
                self.max_charge_w,
                (
                    "Kurvenladung an SoC-Stufe stabilisiert: AUTO-Freilauf bleibt noch "
                    "%ds gesperrt, damit der Laderahmen gleiten kann"
                ) % remaining_s,
                "curve",
            )

        curve_auto_hold_release_stabilize_active = bool(
            previous_parallel_state == "parallel_curve_auto_hold"
            and not forecast_curve_below_floor
            and decision.state == "parallel_auto"
            and decision.priority == "default"
            and previous_parallel_age_s < 600
            and pv_w > 250
            and grid_ema_w < self.grid_relief_enter_w
            and not shortfall_pv_catchup_active
            and not curve_under_ifc_charge_due
            and (
                headroom_reserve_active
                or grid_export_w >= self.curve_charge_keep_w
                or curve_safe_charge_w >= self.curve_charge_enter_w
                or max(0, bat_w) >= self.curve_charge_keep_w
            )
        )
        if curve_auto_hold_release_stabilize_active:
            remaining_s = max(1, int(round(600 - previous_parallel_age_s)))
            decision = choose(
                "parallel_curve_auto_hold",
                MODE_AUTO,
                self.max_charge_w,
                (
                    "Kurven-Haltefreigabe stabilisiert: AUTO-Freilauf bleibt noch "
                    "%ds gesperrt, solange PV-/Headroom-Druck sichtbar ist"
                ) % remaining_s,
                "curve",
            )

        decision = self._apply_transition_table(
            decision,
            previous_state=previous_parallel_state,
            previous_mode=previous_parallel_mode,
            previous_val=previous_parallel_val,
            previous_age_s=previous_parallel_age_s,
            grid_ema_w=grid_ema_w,
            forecast_curve_below_floor=forecast_curve_below_floor,
            wb_owner_evidence_active=wb_current_owner_evidence_active,
            trace=trace,
        )

        payload = {
            "ts": int(time.time()),
            "service": "storage_parallel_regulator",
            "enabled": True,
            "shadow_only": True,
            "active": {
                "state": active_state_name,
                "mode": active_mode,
                "mode_name": _mode_name(active_mode),
                "val": active_val,
            },
            "parallel": decision.as_payload(),
            "headroom_discharge": {
                "day": headroom_discharge_day,
                "today_wh": round(headroom_discharge_today_wh, 1),
                "daily_limit_wh": round(headroom_discharge_daily_limit_wh, 1),
                "daily_remaining_wh": round(headroom_discharge_daily_remaining_wh, 1),
                "daily_limit_pct": round(self.headroom_discharge_daily_limit_pct, 2),
                "daily_blocked": headroom_discharge_daily_blocked,
                "cooldown_s": self.headroom_discharge_cooldown_s,
                "cooldown_remaining_s": round(headroom_discharge_cooldown_remaining_s, 1),
                "cooldown_active": headroom_discharge_cooldown_active,
                "last_active_ts": round(headroom_discharge_last_active_ts, 1) if headroom_discharge_last_active_ts > 0 else 0,
                "last_account_ts": round(now_s, 1),
            },
            "diff": {
                "state_diff": active_state_name != decision.state,
                "mode_diff": active_mode != decision.mode,
                "val_diff_w": int(decision.val - active_val),
            },
            "inputs": {
                "soc": round(soc, 2),
                "curve_soc": None if curve_soc is None else round(curve_soc, 2),
                "curve_gap_pct": None if curve_gap_pct is None else round(curve_gap_pct, 2),
                "curve_above_pct": round(curve_above_pct, 2),
                "adaptive_curve_active": adaptive_curve_active,
                "adaptive_curve_relation": adaptive_curve_relation,
                "adaptive_soc_floor": None if adaptive_floor_soc is None else round(float(adaptive_floor_soc), 2),
                "adaptive_soc_ceiling": None if adaptive_ceiling_soc is None else round(float(adaptive_ceiling_soc), 2),
                "can_reach_target": can_reach_target,
                "max_reachable_soc": None if max_reachable_soc < 0.0 else round(max_reachable_soc, 2),
                "forecast_shortfall_pv_release_active": forecast_shortfall_pv_release_active,
                "shortfall_pv_catchup_active": shortfall_pv_catchup_active,
                "shortfall_target_soc": active_state.get("shortfall_target_soc"),
            "shortfall_target_gap_pct": active_state.get("shortfall_target_gap_pct"),
            "shortfall_real_surplus_w": active_state.get("shortfall_real_surplus_w"),
            "shortfall_catchup_enter_w": active_state.get("shortfall_catchup_enter_w"),
            "shortfall_catchup_nominal_enter_w": active_state.get("shortfall_catchup_nominal_enter_w"),
            "shortfall_late_catchup_enter_w": active_state.get("shortfall_late_catchup_enter_w"),
            "shortfall_late_catchup_active": active_state.get("shortfall_late_catchup_active"),
            "shortfall_catchup_curve_pressure": active_state.get("shortfall_catchup_curve_pressure"),
            "shortfall_catchup_blocked_curve_ready": active_state.get("shortfall_catchup_blocked_curve_ready"),
            "shortfall_catchup_blocked_low_surplus": active_state.get("shortfall_catchup_blocked_low_surplus"),
                "shortfall_release_margin_pct": round(shortfall_release_margin_pct, 2),
                "forecast_curve_landing_hold_active": forecast_curve_landing_hold_active,
                "forecast_floor_target_gap_pct": round(forecast_floor_target_gap_pct, 3),
                "forecast_landing_margin_pct": round(forecast_landing_margin_pct, 3),
                "sliding_horizon_active": sliding_horizon_active,
                "sliding_horizon_candidate_active": sliding_horizon_candidate_active,
                "sliding_horizon_corridor_veto": sliding_horizon_corridor_veto,
                "sliding_horizon_reason": sliding_horizon_reason,
                "sliding_horizon_confidence": round(sliding_horizon_confidence, 4),
                "sliding_horizon_min_confidence": round(sliding_horizon_min_confidence, 4),
                "sliding_horizon_season": sliding_horizon_season,
                "sliding_horizon_minutes_until_latest_charge": sliding_horizon_minutes_until_latest_charge,
                "sliding_horizon_headroom_available_wh": round(sliding_horizon_headroom_available_wh, 0),
                "sliding_horizon_uncovered_pressure_wh": round(sliding_horizon_uncovered_pressure_wh, 0),
                "sliding_horizon_uncovered_curtailment_pressure_wh": round(
                    sliding_horizon_uncovered_curtailment_pressure_wh,
                    0,
                ),
                "headroom_reserve_active": headroom_reserve_active,
                "headroom_reserve_pressure_wh": round(headroom_reserve_pressure_wh, 0),
                "headroom_reserve_source": headroom_reserve_source,
                "headroom_discharge_pressure_wh": round(headroom_discharge_pressure_wh, 0),
                "headroom_execution_schema_version": headroom_execution.get("schema_version"),
                "headroom_execution_allowed": headroom_execution_allowed,
                "headroom_execution_reason_code": headroom_execution_reason,
                "headroom_execution_plan_id": headroom_execution.get("plan_id"),
                "headroom_execution_slot_id": headroom_execution.get("slot_id"),
                "headroom_execution_earliest_start_ts": headroom_execution.get("earliest_start_ts"),
                "headroom_execution_deadline_ts": headroom_execution.get("deadline_ts"),
                "headroom_execution_target_soc": headroom_execution.get("target_soc"),
                "headroom_execution_hard_floor_soc": headroom_execution.get("hard_floor_soc"),
                "headroom_execution_plan_accounted_wh": headroom_execution.get("plan_accounted_wh", 0.0),
                "headroom_execution_slot_accounted_wh": headroom_execution.get("slot_accounted_wh", 0.0),
                "headroom_execution_residual_wh": headroom_execution_residual_wh,
                "headroom_execution_accounted_observed_w": headroom_execution.get("accounted_observed_w", 0.0),
                "headroom_execution_accounted_interval_s": headroom_execution.get("accounted_interval_s", 0.0),
                "headroom_execution_generation_reset": bool(headroom_execution.get("generation_reset", True)),
                "headroom_execution_last_account_ts": round(now_s, 3),
                "headroom_discharge_min_pressure_wh": round(self.headroom_discharge_min_pressure_wh, 0),
                "headroom_discharge_active": headroom_discharge_active,
                "headroom_discharge_blocked_reason": headroom_discharge_blocked_reason,
                "headroom_discharge_target_plateau_reached": headroom_discharge_target_plateau_reached,
                "headroom_discharge_target_curve_soc": (
                    None
                    if target_curve_soc is None
                    else round(float(target_curve_soc), 2)
                ),
                "headroom_discharge_target_plateau_margin_pct": round(headroom_target_plateau_margin_pct, 2),
                "headroom_discharge_w": headroom_discharge_w,
                "headroom_discharge_target_w": headroom_discharge_target_w,
                "headroom_discharge_floor_soc": (
                    None
                    if headroom_discharge_floor_soc is None
                    else round(float(headroom_discharge_floor_soc), 2)
                ),
                "headroom_discharge_gap_pct": round(headroom_discharge_gap_pct, 2),
                "headroom_discharge_enter_pct": round(self.headroom_discharge_enter_pct, 2),
                "headroom_discharge_keep_pct": round(self.headroom_discharge_keep_pct, 2),
                "headroom_discharge_export_room_w": headroom_discharge_export_room_w,
                "headroom_discharge_export_margin_w": self.headroom_discharge_export_margin_w,
                "headroom_discharge_import_guard_w": self.headroom_discharge_import_guard_w,
                "headroom_discharge_abregel_blocked": headroom_discharge_abregel_blocked,
                "headroom_discharge_day": headroom_discharge_day,
                "headroom_discharge_today_wh": round(headroom_discharge_today_wh, 1),
                "headroom_discharge_daily_limit_wh": round(headroom_discharge_daily_limit_wh, 1),
                "headroom_discharge_daily_remaining_wh": round(headroom_discharge_daily_remaining_wh, 1),
                "headroom_discharge_daily_limit_pct": round(self.headroom_discharge_daily_limit_pct, 2),
                "headroom_discharge_daily_blocked": headroom_discharge_daily_blocked,
                "headroom_discharge_cooldown_s": self.headroom_discharge_cooldown_s,
                "headroom_discharge_cooldown_remaining_s": round(headroom_discharge_cooldown_remaining_s, 1),
                "headroom_discharge_cooldown_active": headroom_discharge_cooldown_active,
                "headroom_discharge_last_active_ts": round(headroom_discharge_last_active_ts, 1) if headroom_discharge_last_active_ts > 0 else 0,
                "headroom_discharge_last_account_ts": round(now_s, 1),
                "curve_guard_active": curve_guard_active,
                "target_soc": round(self.target_soc, 1),
                "pv_w": pv_w,
                "grid_w": grid_w,
                "grid_ema_w": grid_ema_w,
                "grid_relief_enter_w": self.grid_relief_enter_w,
                "grid_house_fallback_w": grid_house_fallback_w,
                "home_w": home_w,
                "home_rule_w": home_rule_w,
                "wp_w": wp_w,
                "bat_w": bat_w,
                "wallbox_w": round(wallbox_rule_w, 1),
                "wallbox_actual_w": round(wallbox_w, 1),
                "wallbox_home_includes": wallbox_home_includes,
                "wallbox_ngna": bool(wallbox_ngna),
                "wb_storage_floor_active": wb_storage_floor_active,
                "wb_storage_floor_requested": wb_storage_floor_requested,
                "wb_active": bool(wb_active),
                "wb_active_enter": wb_active_enter,
                "wb_intent_active": wb_intent_active,
                "wb_intent_physical_charge_active": wb_intent_physical_charge_active,
                "wb_intent_authorized_start_active": wb_intent_authorized_start_active,
                "wb_intent_car_present": wb_intent_car_present,
                "wb_intent_bev_full_blocked": wb_intent_bev_full_blocked,
                "wb_budget_signal_active": wb_budget_signal_active,
                "wb_active_keep": wb_active_keep,
                "wb_capacity_sink_active": wb_capacity_sink_active,
                "last_wb_active_ts": int(effective_last_wb_active_ts) if effective_last_wb_active_ts > 0 else 0,
                "last_wb_active_age_s": round(last_wb_active_age_s, 1) if last_wb_active_ts > 0 else None,
                "wb_possible_w": wb_possible_w,
                "wb_budget_w": wb_budget_w,
                "pv_after_fixed_w": pv_after_fixed_w,
                "previous_parallel_state": previous_parallel_state,
                "previous_parallel_mode": _mode_name(previous_parallel_mode),
                "previous_parallel_val": previous_parallel_val,
                "previous_parallel_age_s": (
                    round(previous_parallel_age_s, 1)
                    if previous_parallel_ts > 0
                    else None
                ),
                "previous_decision_age_s": (
                    round(previous_decision_age_s, 1)
                    if previous_decision_ts > 0
                    else None
                ),
                "wb_auto_enter_margin_w": wb_auto_enter_margin_w,
                "wb_auto_keep_margin_w": wb_auto_keep_margin_w,
                "wb_owner_real_min_w": round(wb_owner_real_min_w, 1),
                "wb_real_owner_active": wb_real_owner_active,
                "wb_internal_owner_allowed": wb_internal_owner_allowed,
                "wb_auto_enter": wb_auto_enter,
                "wb_auto_keep": wb_auto_keep,
                "night_floor_enter": night_floor_enter,
                "night_floor_keep": night_floor_keep,
                "reserve_soc": round(reserve_soc, 2),
                "curve_auto_hold_exit_pct": self.curve_auto_hold_exit_pct,
                "curve_auto_hold_release_below_pct": self.curve_auto_hold_release_below_pct,
                "curve_settle_hold_active": curve_settle_hold_active,
                "curve_crossed_from_charge_hold": curve_crossed_from_charge_hold,
                "curve_near_idle_hold": curve_near_idle_hold,
                "curve_edge_export_keep_active": curve_edge_export_keep_active,
                "curve_edge_soft_charge_active": curve_edge_soft_charge_active,
                "curve_edge_soft_charge_w": curve_edge_soft_charge_w,
                "curve_edge_soft_hold_s": self.curve_edge_soft_hold_s,
                "curve_edge_soft_factor": round(self.curve_edge_soft_factor, 3),
                "curve_under_ifc_charge_due": curve_under_ifc_charge_due,
                "curve_charge_release_stabilize_active": curve_charge_release_stabilize_active,
                "curve_charge_release_stabilize_s": self.curve_charge_release_stabilize_s,
                "curve_charge_soc_step_hold_active": curve_charge_soc_step_hold_active,
                "curve_charge_soc_step_hold_s": self.curve_charge_soc_step_hold_s,
                "curve_auto_hold_release_stabilize_active": curve_auto_hold_release_stabilize_active,
                "curve_charge_servo_mode": self.curve_charge_servo_mode,
                "curve_charge_servo_enabled": self.curve_charge_servo_enabled,
                "curve_charge_servo_active": curve_charge_servo_active,
                "curve_charge_servo_candidate_state": curve_charge_servo_candidate_state,
                "curve_charge_servo_block_reason": curve_charge_servo_block_reason,
                "curve_charge_servo_phase": curve_charge_servo_phase,
                "curve_charge_servo_previous_w": curve_charge_servo_previous_w,
                "curve_charge_servo_target_w": curve_charge_servo_target_w,
                "curve_charge_servo_frame_w": curve_charge_servo_frame_w,
                "curve_charge_servo_available_w": curve_charge_servo_available_w,
                "curve_charge_servo_min_w": self.curve_charge_servo_min_w,
                "curve_charge_servo_deadband_w": self.curve_charge_servo_deadband_w,
                "curve_charge_servo_step_up_w": self.curve_charge_servo_step_up_w,
                "curve_charge_servo_step_down_w": self.curve_charge_servo_step_down_w,
                "curve_charge_servo_max_age_s": self.curve_charge_servo_max_age_s,
                "steady_curve_guidance_enabled": bool(
                    active_state.get("steady_curve_guidance_enabled")
                ),
                "steady_curve_approach_active": bool(
                    active_state.get("steady_curve_approach_active")
                ),
                "steady_curve_approach_margin_pct": round(
                    max(
                        0.0,
                        _safe_float(
                            active_state.get("steady_curve_approach_margin_pct"),
                            0.0,
                        ),
                    ),
                    3,
                ),
                "first_curve_ts": int(first_curve_ts) if first_curve_ts else 0,
                "first_curve_soc": None if first_curve_soc is None else round(first_curve_soc, 2),
                "pre_curve_hold_raw_active": pre_curve_hold_raw_active,
                "pre_curve_hold_active": pre_curve_hold_active,
                "pre_curve_ifc_start_active": pre_curve_ifc_start_active,
                "pre_curve_ifc_start_w": self.pre_curve_ifc_start_w,
                "pre_curve_hold_margin_pct": round(pre_curve_hold_margin_pct, 2),
                "curve_above_enter": curve_above_enter,
                "curve_above_keep": curve_above_keep,
                "curve_above_soft": curve_above_soft,
                "curve_soft_charge_active": curve_soft_charge_active,
                "curve_soft_charge_factor": round(curve_soft_factor, 3),
                "curve_soft_charge_limit_w": curve_soft_charge_limit_w,
                "curve_cap_target_w": curve_cap_target_w,
                "curve_cap_below_curve_catchup_active": curve_cap_below_curve_catchup_active,
                "curve_cap_below_curve_catchup_w": curve_cap_below_curve_catchup_w,
                "curve_cap_active": curve_cap_active,
                "curve_cap_keep_active": curve_cap_keep_active,
                "curve_cap_neutral_keep": curve_cap_neutral_keep,
                "curve_cap_short_hold_s": self.curve_cap_short_hold_s,
                "curve_cap_short_hold_active": curve_cap_short_hold_active,
                "curve_cap_proactive_active": curve_cap_proactive_active,
                "curve_cap_blocked_by_wb_capacity": curve_cap_blocked_by_wb_capacity,
                "curve_cap_blocked_by_wb_grid": curve_cap_blocked_by_wb_grid,
                "curve_cap_export_room_w": curve_cap_export_room_w,
                "curve_cap_excess_charge_w": curve_cap_excess_charge_w,
                "curve_cap_measured_charge_w": curve_cap_measured_charge_w,
                "meter_balance_home_w": meter_balance["home_balance_w"],
                "meter_balance_error_w": meter_balance["home_balance_error_w"],
                "meter_balance_error_limit_w": meter_balance["home_balance_error_limit_w"],
                "meter_balance_plausible": meter_balance_plausible,
                "curve_cap_pressure_w": curve_cap_pressure_w,
                "curve_cap_physical_pressure_w": curve_cap_physical_pressure_w,
                "curve_cap_dc_pressure_w": curve_cap_dc_pressure_w,
                "curve_cap_dc_pressure_active": curve_cap_dc_pressure_active,
                "curve_cap_dc_hold_active": curve_cap_dc_hold_active,
                "curve_cap_dc_hold_margin_w": curve_cap_dc_hold_margin_w,
                "curve_cap_direct_pressure_w": curve_cap_direct_pressure_w,
                "curve_cap_model_pressure_w": curve_cap_model_pressure_w,
                "curve_cap_grid_pressure_w": curve_cap_grid_pressure_w,
                "curve_cap_grid_export_over_limit_w": grid_export_over_limit_w,
                "curve_cap_current_feed_pressure_w": current_feed_pressure_w,
                "curve_cap_projected_export_without_charge_w": projected_export_without_charge_w,
                "curve_cap_projected_export_threshold_w": projected_export_threshold_w,
                "curve_cap_projected_export_over_limit_w": projected_export_over_limit_w,
                "curve_cap_feed_export_threshold_w": feed_export_threshold_w,
                "curve_cap_release_export_threshold_w": release_export_threshold_w,
                "curve_cap_feed_buffer_w": self.curve_cap_feed_buffer_w,
                "curve_cap_release_band_w": self.curve_cap_release_band_w,
                "curve_cap_feedback_band_w": self.curve_cap_feedback_band_w,
                "curve_cap_release_hysteresis_w": self.curve_cap_release_hysteresis_w,
                "curve_cap_release_grace_s": self.curve_cap_release_grace_s,
                "curve_cap_grid_contract_valid": curve_cap_grid_contract_valid,
                "curve_cap_real_grid_import_active": curve_cap_real_grid_import_active,
                "curve_cap_release_below_active": curve_cap_release_below_active,
                "curve_cap_release_below_since_ts": curve_cap_release_below_since_ts,
                "curve_cap_release_elapsed_s": curve_cap_release_elapsed_s,
                "curve_cap_release_grace_active": curve_cap_release_grace_active,
                "curve_cap_release_ramp_active": curve_cap_release_ramp_active,
                "curve_cap_hysteresis_hold_active": curve_cap_hysteresis_hold_active,
                "curve_cap_hysteresis_amount_follow_active": curve_cap_hysteresis_amount_follow_active,
                "curve_cap_hysteresis_floor_w": curve_cap_hysteresis_floor_w,
                "curve_cap_invalid_hold_active": curve_cap_invalid_hold_active,
                "curve_cap_release_phase": curve_cap_release_phase,
                "curve_cap_release_pending": curve_cap_release_pending,
                "curve_cap_release_requested": curve_cap_release_requested,
                "curve_cap_release_confirmed_since_ts": curve_cap_release_confirmed_since_ts,
                "curve_cap_post_release_until_ts": curve_cap_post_release_until_ts,
                "curve_cap_post_release_guard_active": curve_cap_post_release_guard_active,
                "curve_cap_post_release_reentry_blocked": curve_cap_post_release_reentry_blocked,
                "curve_cap_settings_readback_valid": settings_readback_valid,
                "curve_cap_settings_previous_cap_confirmed": settings_previous_curve_cap_confirmed,
                "curve_cap_settings_bounded_zero_confirmed": settings_bounded_zero_confirmed,
                "curve_cap_settings_release_confirmed": settings_release_confirmed,
                "curve_cap_bounded_zero_w": curve_cap_bounded_zero_w,
                "curve_cap_min_charge_w": curve_cap_bounded_zero_w,
                "curve_cap_hard_pressure_active": curve_cap_hard_pressure_active,
                "curve_cap_feedback_active": curve_cap_feedback_active,
                "curve_cap_grid_export_error_w": grid_export_error_w,
                "curve_cap_below_threshold_w": curve_cap_below_threshold_w,
                "inverter_limit_w": inverter_limit_w,
                "inverter_pressure_w": inverter_pressure_w,
                "derate_limit_w": derate_limit_w,
                "derate_hard_limit_w": derate_hard_limit_w,
                "derate_limit_source": derate_limit_source,
                "configured_export_limit_w": configured_export_limit_w,
                "live_derate_limit_w": live_derate_limit_w,
                "derating_pressure_w": derating_pressure_w,
                "ems_mandatory_charge_w": ems_mandatory_charge_w,
                "ems_pv_total_w": ems_budget["pv_total_w"],
                "ems_e3dc_dc_pv_w": ems_budget["e3dc_dc_pv_w"],
                "ems_e3dc_ac_available_w": ems_budget["e3dc_ac_available_w"],
                "ems_external_ac_pv_w": ems_budget["external_ac_pv_w"],
                "ems_external_ac_pv_trusted": ems_budget["external_ac_pv_trusted"],
                "ems_external_ac_pv_source": ems_budget["external_ac_pv_source"],
                "ems_ac_available_w": ems_budget["ac_available_w"],
                "ems_wallbox_budget_total_w": ems_budget["wallbox_budget_total_w"],
                "ems_wallbox_budget_increase_w": ems_budget["wallbox_budget_increase_w"],
                "ems_curtailment_loss_w": ems_budget["curtailment_loss_w"],
                "curve_cap_export_trigger_w": self.curve_cap_export_trigger_w,
                "curve_cap_pv_surplus_w": curve_cap_pv_surplus_w,
                "curve_cap_margin_w": curve_cap_margin_w,
                "curve_cap_step_w": self.curve_cap_step_w,
                "curve_charge_enter_w": self.curve_charge_enter_w,
                "curve_charge_keep_w": self.curve_charge_keep_w,
                "curve_charge_keep": curve_charge_keep,
                "price_house_discharge_enter_w": self.price_house_discharge_enter_w,
                "price_house_discharge_keep_w": self.price_house_discharge_keep_w,
                "price_house_discharge_keep": price_house_discharge_keep,
                "planned_load_confirmed": bool(active_state.get("planned_load_confirmed")),
                "planned_load_expected_w": _safe_int(active_state.get("planned_load_expected_w"), 0),
                "planned_load_observed_extra_w": _safe_int(active_state.get("planned_load_observed_extra_w"), 0),
                "planned_load_mode": active_state.get("planned_load_mode", ""),
                "planned_load_support_allowed": bool(active_state.get("planned_load_support_allowed")),
                "planned_load_support_reason": active_state.get("planned_load_support_reason", ""),
                "planned_load_support": active_state.get("planned_load_support", {}),
                "iFc_w": i_fc_w,
                "iMinLade_w": i_min_lade_w,
                "curve_gap_pct": _safe_float(active_state.get("curve_gap_pct"), 0.0),
                "curve_gap_catchup_w": _safe_int(active_state.get("curve_gap_catchup_w"), 0),
                "curve_gap_catchup_cap_w": _safe_int(active_state.get("curve_gap_catchup_cap_w"), 0),
                "curve_gap_catchup_factor": _safe_float(active_state.get("curve_gap_catchup_factor"), 0.0),
                "curve_gap_catchup_min_w": _safe_int(active_state.get("curve_gap_catchup_min_w"), 0),
                "curve_gap_catchup_taper_pct": _safe_float(active_state.get("curve_gap_catchup_taper_pct"), 0.0),
                "curve_need_raw_w": _safe_int(active_state.get("curve_need_raw_w"), 0),
                "lookahead_need_w": _safe_int(active_state.get("lookahead_need_w"), 0),
                "curve_export_w": curve_export_w,
                "curve_safe_charge_w": curve_safe_charge_w,
                "curve_ifc_export_catchup_active": curve_ifc_export_catchup_active,
                "curve_ifc_export_catchup_floor_w": curve_ifc_export_catchup_floor_w,
                "curve_ifc_export_catchup_w": curve_ifc_export_catchup_w,
            },
            "trace": trace,
        }
        return payload


def emit_parallel_decision(active_state: Dict[str, Any]) -> None:
    cfg = _load_cfg()
    if not _cfg_bool(cfg, "storage_parallel_enable", True):
        _atomic_write(SHADOW_STATE_F, {
            "ts": int(time.time()),
            "service": "storage_parallel_regulator",
            "enabled": False,
            "shadow_only": True,
            "active": {
                "state": str(active_state.get("storage_state") or active_state.get("state") or "unknown"),
                "mode": _safe_int(active_state.get("mode"), -1),
                "val": max(0, _safe_int(active_state.get("val"), 0)),
            },
        })
        return

    previous_payload = _read_json(SHADOW_STATE_F, max_age_s=300)
    previous_parallel = previous_payload.get("parallel") if isinstance(previous_payload.get("parallel"), dict) else {}
    state_for_decision = dict(active_state or {})
    if previous_parallel.get("state"):
        state_for_decision.setdefault("previous_parallel_state", previous_parallel.get("state"))
        state_for_decision.setdefault("previous_parallel_mode", previous_parallel.get("mode"))
        state_for_decision.setdefault("previous_parallel_val", previous_parallel.get("val"))
        state_for_decision.setdefault("previous_parallel_ts", previous_payload.get("ts"))
    previous_headroom = previous_payload.get("headroom_discharge") if isinstance(previous_payload.get("headroom_discharge"), dict) else {}
    if previous_headroom:
        state_for_decision.setdefault("headroom_discharge_day", previous_headroom.get("day"))
        state_for_decision.setdefault("headroom_discharge_today_wh", previous_headroom.get("today_wh"))
        state_for_decision.setdefault("headroom_discharge_last_active_ts", previous_headroom.get("last_active_ts"))
        state_for_decision.setdefault("headroom_discharge_last_account_ts", previous_headroom.get("last_account_ts", previous_payload.get("ts")))
    if previous_payload.get("ts"):
        state_for_decision.setdefault("previous_state_ts", previous_payload.get("ts"))
    previous_inputs = previous_payload.get("inputs") if isinstance(previous_payload.get("inputs"), dict) else {}
    if previous_inputs.get("wb_possible_w"):
        state_for_decision.setdefault("last_wb_possible_power_w", previous_inputs.get("wb_possible_w"))
    last_wb_active_ts = _safe_float(previous_inputs.get("last_wb_active_ts"), 0.0)
    if _safe_float(previous_inputs.get("wallbox_w"), 0.0) > 250 or previous_inputs.get("wb_active_enter"):
        last_wb_active_ts = _safe_float(previous_payload.get("ts"), 0.0)
    if last_wb_active_ts > 0:
        state_for_decision.setdefault("last_wb_active_ts", last_wb_active_ts)

    regulator = ParallelStorageRegulator(cfg)
    payload = regulator.decide(
        active_state=state_for_decision,
        live=_read_json(LIVE_F, max_age_s=30),
        plan=_read_json(PLAN_F, max_age_s=1800),
        wb_budget=_read_json(WB_BUDGET_F, max_age_s=30),
        wb_intent=_read_json(WB_INTENT_F, max_age_s=60),
    )
    if _cfg_bool(cfg, "storage_parallel_diff_enable", True):
        previous_diff = _read_json(SHADOW_DIFF_STATE_F, max_age_s=86400)
        diff_report = _build_diff_report(payload, cfg, previous_diff)
        diff_report = _maybe_log_diff_report(diff_report, cfg)
        brief = _build_brief(diff_report)
        diff_report["brief"] = brief
        payload["comparison"] = {
            "current": diff_report.get("current", {}),
            "windows": diff_report.get("windows", {}),
            "brief": brief,
        }
        _atomic_write(SHADOW_DIFF_STATE_F, diff_report)
        _atomic_write(SHADOW_DIFF_BRIEF_F, brief)
        _atomic_write_text(SHADOW_DIFF_BRIEF_TXT_F, brief.get("text", ""))
        current = diff_report.get("current") if isinstance(diff_report.get("current"), dict) else {}
        if (
            current.get("different")
            and current.get("log_relevant")
            and _cfg_bool(cfg, "storage_parallel_history_enable", True)
        ):
            max_lines = max(20, _safe_int(cfg.get("storage_parallel_history_max_lines"), 720))
            _append_history(SHADOW_DIFF_HISTORY_F, {
                "ts": diff_report["ts"],
                "summary": brief.get("summary"),
                "status": brief.get("status"),
                "active": brief.get("active"),
                "parallel": brief.get("parallel"),
                "why": brief.get("why", [])[:3],
                "numbers": brief.get("numbers", {}),
                "windows": brief.get("windows", {}),
                "text": brief.get("text"),
            }, max_lines=max_lines)
    _atomic_write(SHADOW_STATE_F, payload)
    if _cfg_bool(cfg, "storage_parallel_history_enable", True):
        max_lines = max(20, _safe_int(cfg.get("storage_parallel_history_max_lines"), 720))
        _append_history(SHADOW_HISTORY_F, {
            "ts": payload["ts"],
            "active": payload["active"],
            "parallel": payload["parallel"],
            "diff": payload["diff"],
            "inputs": payload["inputs"],
        }, max_lines=max_lines)
