#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Next generation E3DC Storage Manager.

Design goal:
  one input snapshot -> one decision -> one RSCP command -> one budget output.

The old manager remains the rollback path. This manager deliberately avoids the
legacy chain of early sends and side watchers. Guard logic is either a protected
owner (manual, emergency, predump, price/grid charge, direct marketing) or bundled into the
parallel storage regulator.
"""

from __future__ import annotations

import datetime
import copy
import json
import logging
import math
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
for _path in (REPO_ROOT, SCRIPT_DIR):
    if _path and _path not in sys.path:
        sys.path.insert(0, _path)

from rscp_client import RscpConnection, RscpTag, RscpType  # noqa: E402
from storage_parallel_regulator import (  # noqa: E402
    MODE_AUTO,
    MODE_CHRG,
    MODE_DISCH,
    MODE_GRID,
    MODE_IDLE,
    ParallelStorageRegulator,
)
from Wallbox.modes import (  # noqa: E402
    CONTROL_TARGET,
    MODE_CURVE,
    MODE_OFF,
    MODE_TARGET,
    normalize_wb_mode,
    price_limit_ct,
    storage_floor_mode,
)
from Wallbox import phase_transition as wallbox_phase_transition_policy  # noqa: E402
from runtime_logging import configure_service_logger  # noqa: E402
from config_validator import write_config_validation  # noqa: E402
from aux_inverter_contract import (  # noqa: E402
    effective_contract as aux_inverter_effective_contract,
    migrate_state_files as migrate_aux_inverter_state_files,
    state_migration_required as aux_inverter_state_migration_required,
    write_canonical_state as write_aux_inverter_canonical_state,
)
from decision_history import write_history_record  # noqa: E402
from ems_decision_diagnostics import (  # noqa: E402
    build_storage_decision_record,
    default_surface_path,
    write_decision_surface_record,
)
from data_models import (  # noqa: E402
    LiveDataSnapshot,
    StoragePlanSnapshot,
    WallboxStatusSnapshot,
    live_power_plausibility,
)
try:
    from Installer.storage_dispatch_contract import (  # noqa: E402
        build_runtime_overlay,
        load_validated_canonical_plan_snapshot,
        ValidatedCanonicalPlanSnapshot,
        validate_canonical_plan,
    )
except ModuleNotFoundError:
    from storage_dispatch_contract import (  # type: ignore  # noqa: E402
        build_runtime_overlay,
        load_validated_canonical_plan_snapshot,
        ValidatedCanonicalPlanSnapshot,
        validate_canonical_plan,
    )
try:
    from Installer.storage_dispatch_phase5 import phase5_arbitration_contract  # noqa: E402
except ModuleNotFoundError:
    from storage_dispatch_phase5 import phase5_arbitration_contract  # type: ignore  # noqa: E402
try:
    from Installer.Storage.common import mode_label, safe_float, safe_int  # noqa: E402
except ModuleNotFoundError:
    from Storage.common import mode_label, safe_float, safe_int  # type: ignore  # noqa: E402
try:
    from Installer.Storage.charge_curve import (  # noqa: E402
        _plan_ts_s,
        adaptive_curve_context,
        build_ladekurve_meta,
        current_curve,
        hard_noon_anchor,
        latest_charge_start_clamp_context,
        sliding_forecast_horizon_context,
    )
except ModuleNotFoundError:
    from Storage.charge_curve import (  # type: ignore  # noqa: E402
        _plan_ts_s,
        adaptive_curve_context,
        build_ladekurve_meta,
        current_curve,
        hard_noon_anchor,
        latest_charge_start_clamp_context,
        sliding_forecast_horizon_context,
    )
try:
    from Installer.Storage.predump import (  # noqa: E402
        augment_predump_consumer_live,
        hard_predump_grid_limit_w,
        predump_actual_consumer_load_w,
        predump_allow_flags,
        predump_budget_step_w,
        predump_consumer_landing_band,
        predump_consumer_status,
        predump_floor_budget_w,
        predump_grid_export_headroom,
        predump_grid_fallback_window,
        predump_grid_ramped_discharge_w,
        predump_guarded_home_load_w,
        predump_heatpump_power_w,
        predump_plan_is_hard,
        predump_reopen_block_s,
        predump_reopen_blocked,
        predump_reopen_margin_pct,
        predump_request_from_plan,
        predump_round_budget_w,
        predump_trajectory_state,
        predump_wallbox_block_window,
        predump_wallbox_floor_hold_decision,
        predump_wallbox_minimum_power_w,
        stabilize_predump_request,
    )
except ModuleNotFoundError:
    from Storage.predump import (  # type: ignore  # noqa: E402
        augment_predump_consumer_live,
        hard_predump_grid_limit_w,
        predump_actual_consumer_load_w,
        predump_allow_flags,
        predump_budget_step_w,
        predump_consumer_landing_band,
        predump_consumer_status,
        predump_floor_budget_w,
        predump_grid_export_headroom,
        predump_grid_fallback_window,
        predump_grid_ramped_discharge_w,
        predump_guarded_home_load_w,
        predump_heatpump_power_w,
        predump_plan_is_hard,
        predump_reopen_block_s,
        predump_reopen_blocked,
        predump_reopen_margin_pct,
        predump_request_from_plan,
        predump_round_budget_w,
        predump_trajectory_state,
        predump_wallbox_block_window,
        predump_wallbox_floor_hold_decision,
        predump_wallbox_minimum_power_w,
        stabilize_predump_request,
    )
try:
    from Installer.Storage.rscp_commands import (  # noqa: E402
        ACTIVE_REFRESH_MODES,
        ACTIVE_RELEASE_MODES,
        BattCtrl,
        RSCP_COMMAND_CONTRACT_VERSION,
        rscp_command_contract,
        rscp_settings_from_cfg,
    )
except ModuleNotFoundError:
    from Storage.rscp_commands import (  # type: ignore  # noqa: E402
        ACTIVE_REFRESH_MODES,
        ACTIVE_RELEASE_MODES,
        BattCtrl,
        RSCP_COMMAND_CONTRACT_VERSION,
        rscp_command_contract,
        rscp_settings_from_cfg,
    )
from reserve import effective_ep_reserve_pct  # noqa: E402
try:
    from planned_loads import current_planned_load_status  # noqa: E402
except Exception:
    from .planned_loads import current_planned_load_status  # type: ignore  # noqa: E402


AUTO_LIMIT_STATES = {
    "parallel_price_hold",
    "parallel_price_house_discharge",
    "parallel_planned_load_hold",
    "parallel_planned_load_price_support",
    "parallel_curve_auto_hold",
    "parallel_curve_auto_no_surplus",
    "parallel_curve_charge",
    "parallel_curve_charge_cap",
    "parallel_wb_auto",
}
AUTO_LIMIT_REQUIRED_STATES = {
    "parallel_price_hold",
    "parallel_price_house_discharge",
    "parallel_planned_load_hold",
    "parallel_planned_load_price_support",
}
OBSERVE_RESERVE_RELEASE_AUTO_STATES = {
    "parallel_curve_auto_hold",
    "parallel_curve_auto_no_surplus",
    "parallel_curve_charge",
    "parallel_curve_charge_cap",
    "parallel_wb_auto",
}
STORAGE_OWNER_CONTRACT_VERSION = 1
HARD_MODE_GUARD_CONTRACT_VERSION = 1
AUTO_LIMIT_CONTRACT_VERSION = 5
CURVE_CONTEXT_CONTRACT_VERSION = 1
STORAGE_DECISION_PATH_CONTRACT_VERSION = 7
MARKET_PATH_CONTRACT_VERSION = 1
DIRECT_MARKETING_PATH_CONTRACT_VERSION = 1
DIRECT_MARKETING_POLICY_SCHEMA = "direct_marketing_policy_v1"
DIRECT_MARKETING_POLICY_EXPORT_STATES = {"FORCE_EXPORT", "HEADROOM_EXPORT"}
DIRECT_MARKETING_POLICY_CHARGE_STATES = {"FORCE_CHARGE_PV"}
DIRECT_MARKETING_POLICY_PASSIVE_STATES = {"HOLD", "NORMAL"}
DIRECT_MARKETING_POLICY_ACTIVE_STATES = DIRECT_MARKETING_POLICY_EXPORT_STATES | DIRECT_MARKETING_POLICY_CHARGE_STATES
DIRECT_MARKETING_EXPORT_EXECUTION_SCHEMA = "direct_marketing_export_execution_v1"
EP_RESERVE_VETO_SIGNATURE = "STORAGE_EP_RESERVE_VETO"
PREDUMP_PATH_CONTRACT_VERSION = 1
PROTECTION_PATH_CONTRACT_VERSION = 2
BUDGET_READINESS_CONTRACT_VERSION = 1
BUDGET_ARBITRATION_CONTRACT_VERSION = 1
BUDGET_STABILITY_CONTRACT_VERSION = 1
BUDGET_EXECUTOR_GATE_CONTRACT_VERSION = 1
BUDGET_EXECUTOR_LATCH_CONTRACT_VERSION = 1
BUDGET_EXECUTOR_ACK_CONTRACT_VERSION = 1
EMS_BUDGET_RUNTIME_CONTRACT_VERSION = 1


log = configure_service_logger(
    "StorageManager",
    log_path="/var/www/html/logs/storage_manager.log",
    max_bytes=2 * 1024 * 1024,
    backup_count=3,
    quiet_interval_s=180.0,
    always_keywords=(
        "fehler",
        "error",
        "beende",
        "gestartet",
        "keine e3dc-rscp-konfiguration",
    ),
)

RAMDISK = "/var/www/html/ramdisk"
DATA_DIR = "/var/www/html/data"
LOG_DIR = "/var/www/html/logs"
V4_CFG = os.path.join(DATA_DIR, "e3dc_v4.json")
LIVE_F = os.path.join(RAMDISK, "live_data_py.json")
PLAN_F = os.path.join(RAMDISK, "storage_plan.json")
DISPATCH_RUNTIME_F = os.path.join(RAMDISK, "storage_dispatch_runtime.json")
STATE_F = os.path.join(RAMDISK, "storage_manager_state.json")
WB_F = os.path.join(RAMDISK, "wb_pv_budget.json")
WB_DIAGNOSTIC_F = os.path.join(RAMDISK, "wb_pv_budget_diagnostics.json")
WB_INTENT_F = os.path.join(RAMDISK, "wallbox_storage_intent.json")
WB_NATIVE_F = os.path.join(RAMDISK, "wallbox_native.json")
MANUAL_OVERRIDE_F = os.path.join(RAMDISK, "manual_bat_override.json")
MANUAL_ANCHOR_F = os.path.join(RAMDISK, "manual_bat_anchor.json")
PREDUMP_PLAN_F = os.path.join(RAMDISK, "predump_consumer_plan.json")
ENERGY_DECISION_F = os.path.join(RAMDISK, "energy_decision_latest.json")
EMS_DECISION_F = default_surface_path(RAMDISK)
DECISION_LATEST_F = os.path.join(RAMDISK, "storage_decision_latest.json")
DIRECT_MARKETING_REPORT_F = os.path.join(RAMDISK, "direct_marketing_daily_report.json")
DIRECT_MARKETING_AUX_INVERTER_SHELLY_F = os.path.join(RAMDISK, "direct_marketing_aux_inverter_shelly_state.json")
DIRECT_MARKETING_AUX_INVERTER_SHELLY_MANUAL_LOCK_F = os.path.join(
    DATA_DIR,
    "direct_marketing_aux_inverter_shelly_manual_lock.json",
)
DIRECT_MARKETING_AUX_INVERTER_SHELLY_GUARD_F = os.path.join(
    DATA_DIR,
    "direct_marketing_aux_inverter_shelly_guard_state.json",
)
DIRECT_MARKETING_AUX_INVERTER_SHELLY_MIGRATION_F = os.path.join(
    DATA_DIR,
    "direct_marketing_aux_inverter_shelly_migration.json",
)
EPEX_F = os.path.join(RAMDISK, "epex_daten.json")
DECISION_HISTORY_PREFIX = "storage_decision_history_"
EMS_REACTION_LATEST_F = os.path.join(RAMDISK, "ems_reaction_latest.json")
EMS_REACTION_HISTORY_PREFIX = "ems_reaction_history_"

CYCLE_S = 5
_stop = False
_decision_history_state: Dict[str, Any] = {}
_direct_marketing_report_state: Dict[str, Any] = {}
_ems_reaction_history_state: Dict[str, Any] = {}
_budget_stability_shadow_state: Dict[str, Any] = {}
_budget_executor_latch_shadow_state: Dict[str, Any] = {}
_budget_executor_ack_shadow_state: Dict[str, Any] = {}
_direct_marketing_aux_inverter_shelly_state: Dict[str, Any] = {}
_direct_marketing_aux_inverter_migration_failure_latches: Dict[tuple, Dict[str, Any]] = {}
_JSON_READ_CACHE: Dict[str, Dict[str, Any]] = {}
_JSON_WRITE_CACHE: Dict[str, Dict[str, Any]] = {}
_JSON_WRITE_NOISE_KEYS = {
    "ts",
    "live_age_s",
    "wbminsoc_previous_state_age_s",
    "direct_marketing_pv_store_state_age_s",
    "direct_marketing_owner_switch_cooldown_age_s",
}
_HISTORY_EVENT_FORCE_INTERVAL_S = 300.0
_HISTORY_EVENT_POWER_DELTA_W = 500
_WB_BUDGET_FORCE_INTERVAL_S = 10.0
_WB_BUDGET_DIAGNOSTIC_FORCE_INTERVAL_S = 60.0
_WB_BUDGET_DIAGNOSTIC_STATE: Dict[str, Any] = {}
_decision_history_event_state: Dict[str, Any] = {}
_ems_reaction_history_event_state: Dict[str, Any] = {}


def _sig(signum: int, _frame: Any) -> None:
    global _stop
    _stop = True
    log.info("Signal %d - beende.", signum)


signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)


def ep_reserve_soc(cfg: Dict[str, Any], live: Optional[Dict[str, Any]] = None) -> float:
    return effective_ep_reserve_pct(cfg, live or {}, default=8.0)


def cfg_bool(cfg: Dict[str, Any], key: str, default: bool = False) -> bool:
    raw = cfg.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "ja", "ein")


def auto_limit_heartbeat_enabled(cfg: Dict[str, Any]) -> bool:
    return cfg_bool(cfg, "storage_auto_limit_heartbeat_enable", True)


def auto_limit_heartbeat_s(cfg: Dict[str, Any]) -> float:
    return max(1.0, min(3.0, safe_float(cfg.get("storage_auto_limit_heartbeat_s"), 2.0)))


def manual_override_max_age_s(cfg: Dict[str, Any]) -> int:
    hours = safe_float(cfg.get("storage_manual_override_max_age_h"), 12.0)
    return int(max(1.0, min(24.0, hours)) * 3600)


def manual_override_expired(
    manual_override: Dict[str, Any],
    now_s: Optional[float] = None,
    max_age_s: Optional[float] = None,
) -> bool:
    if not manual_override:
        return False
    now = time.time() if now_s is None else float(now_s)
    deadlines: List[float] = []
    expires_ts = safe_float(manual_override.get("expires_ts"), 0.0)
    if expires_ts > 0:
        deadlines.append(expires_ts)
    if max_age_s is not None:
        ts = safe_float(manual_override.get("ts"), safe_float(manual_override.get("created_ts"), 0.0))
        if ts <= 0 or ts > now + 300.0:
            return True
        deadlines.append(ts + max(1.0, float(max_age_s)))
    if not deadlines:
        return False
    return now >= min(deadlines)


def discharge_block_auto_limit(cfg: Dict[str, Any], max_charge_w: int, reason: str) -> Dict[str, Any]:
    return {
        "enabled": True,
        "release": False,
        "max_charge_w": max(0, int(max_charge_w)),
        "max_discharge_w": 0,
        "discharge_start_w": 0,
        "heartbeat_s": auto_limit_heartbeat_s(cfg),
        "reason": reason,
    }


def discharge_cap_auto_limit(
    cfg: Dict[str, Any],
    max_charge_w: int,
    max_discharge_w: int,
    reason: str,
) -> Dict[str, Any]:
    return {
        "enabled": True,
        "release": False,
        "max_charge_w": max(0, int(max_charge_w)),
        "max_discharge_w": max(0, int(max_discharge_w)),
        "discharge_start_w": 0,
        "heartbeat_s": auto_limit_heartbeat_s(cfg),
        "reason": reason,
    }


def charge_block_auto_limit(cfg: Dict[str, Any], max_discharge_w: int, reason: str) -> Dict[str, Any]:
    return {
        "enabled": True,
        "release": False,
        "max_charge_w": 0,
        "max_discharge_w": max(0, int(max_discharge_w)),
        "discharge_start_w": 0,
        "heartbeat_s": auto_limit_heartbeat_s(cfg),
        "reason": reason,
    }


def charge_cap_auto_limit(
    cfg: Dict[str, Any],
    max_charge_w: int,
    max_discharge_w: int,
    reason: str,
) -> Dict[str, Any]:
    return {
        "enabled": True,
        "release": False,
        "max_charge_w": max(0, int(max_charge_w)),
        "max_discharge_w": max(0, int(max_discharge_w)),
        "discharge_start_w": 0,
        "set_power_auto": True,
        "heartbeat_s": auto_limit_heartbeat_s(cfg),
        "reason": reason,
    }


def storage_auto_limit_contract(
    auto_limit: Optional[Dict[str, Any]],
    *,
    state: str = "",
    mode: int = MODE_AUTO,
    reason: str = "",
    direct_marketing_active: bool = False,
    direct_marketing_auto_limit_active: bool = False,
) -> Dict[str, Any]:
    """Normalize an EMS auto-limit payload without changing control output."""
    auto = auto_limit if isinstance(auto_limit, dict) else {}
    present = bool(auto)
    enabled_raw = bool(auto.get("enabled"))
    release = bool(auto.get("release"))
    enabled = bool(enabled_raw and not release)
    active = bool(present and (enabled_raw or release))
    max_charge_w = max(0, safe_int(auto.get("max_charge_w"), 0))
    max_discharge_w = max(0, safe_int(auto.get("max_discharge_w"), 0))
    discharge_start_w = max(0, safe_int(auto.get("discharge_start_w"), 0))
    heartbeat_s = safe_float(auto.get("heartbeat_s"), 0.0)
    set_power_auto = bool(auto.get("set_power_auto"))
    set_power_value_w = max(0, safe_int(auto.get("set_power_value"), 0))
    mode_i = safe_int(mode, MODE_AUTO)
    reason_text = str(auto.get("reason") or reason or "")
    reason_l = reason_text.lower()
    state_l = str(state or "").lower()
    command_class = "release" if release else ("limit" if enabled else "none")
    if not active and present:
        command_class = "inactive"
    if not active:
        source_class = "none"
    elif (
        bool(direct_marketing_auto_limit_active)
        or state_l.startswith("direct_marketing_")
        or "direktvermarkt" in reason_l
    ):
        source_class = "direct_marketing"
    elif state_l.startswith("market_") or any(token in reason_l for token in ("preis", "slot", "epex", "octopus")):
        source_class = "market"
    elif state_l.startswith("pre_discharge") or "pre-dump" in reason_l or "predump" in reason_l:
        source_class = "predump"
    elif (
        state_l in ("parallel_no_data", "parallel_passthrough", "parallel_emergency_auto", "live_stale_auto")
        or state_l.startswith("hard_mode_guard")
        or state_l.startswith("live_plausibility")
    ):
        source_class = "protection"
    # Ein typisierter Wallbox-State ist die führende Quelle. Freier
    # Begründungstext wie "Hausreserve" beschreibt hier nur die zulässige
    # Entladegrenze und darf den untergeordneten Release nicht nachträglich
    # als unabhängigen Schutzpfad klassifizieren.
    elif state_l.startswith("parallel_wb"):
        source_class = "wallbox"
    # Auch der typisierte Kurvenzustand ist führend. Ein nachgelagerter
    # Präsentationstext darf die Quelle nicht zu Wallbox oder Schutz
    # umklassifizieren (z. B. "Wallbox regelt Ladeleistung" in einer
    # parallel_curve_auto_hold-Ladegrenze).
    elif state_l == "parallel_curve_charge_cap" or state_l.startswith("parallel_headroom"):
        source_class = "protection"
    elif state_l.startswith("parallel_curve"):
        source_class = "curve"
    elif any(token in reason_l for token in ("abregel", "notstrom", "schutz", "reserve")):
        source_class = "protection"
    elif "wallbox" in reason_l or "wbminsoc" in reason_l:
        source_class = "wallbox"
    elif "kurve" in reason_l or "korridor" in reason_l:
        source_class = "curve"
    elif bool(direct_marketing_active):
        source_class = "direct_marketing_observer"
    elif any(token in reason_l for token in ("guard", "plausibil", "glitch")):
        source_class = "guard"
    else:
        source_class = "auto_limit"
    return {
        "contract_version": AUTO_LIMIT_CONTRACT_VERSION,
        "present": present,
        "active": active,
        "enabled_raw": enabled_raw,
        "enabled": enabled,
        "release": release,
        "command_class": command_class,
        "source_class": source_class,
        "mode": mode_i,
        "mode_name": mode_label(mode_i),
        "state": str(state or ""),
        "max_charge_w": max_charge_w,
        "max_discharge_w": max_discharge_w,
        "discharge_start_w": discharge_start_w,
        "heartbeat_s": heartbeat_s,
        "set_power_auto": set_power_auto,
        "set_power_value_w": set_power_value_w,
        "charge_blocked": bool(enabled and max_charge_w <= 0),
        "discharge_blocked": bool(enabled and max_discharge_w <= 0),
        "charge_limited": bool(enabled),
        "discharge_limited": bool(enabled),
        "reason": reason_text,
        "value_signature": "auto_limit:%d:%d:%d:%d:%d" % (
            1 if enabled_raw else 0,
            1 if release else 0,
            max_charge_w,
            max_discharge_w,
            discharge_start_w,
        ),
    }


def storage_curve_context_contract(
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Describe curve position and EMS action context without controlling hardware."""
    plan = plan if isinstance(plan, dict) else {}
    state_name = str(payload.get("state") or "")
    mode_value = safe_int(payload.get("mode"), MODE_AUTO)
    soc_now = round(safe_float(payload.get("soc"), 0.0), 2)
    curve_soc_raw = payload.get("curve_soc")
    has_curve = curve_soc_raw is not None
    curve_soc = round(safe_float(curve_soc_raw, 0.0), 2) if has_curve else None
    curve_gap_pct = round(soc_now - safe_float(curve_soc, 0.0), 2) if has_curve else None
    target_raw = payload.get("target_soc")
    if target_raw is None:
        target_raw = plan.get("planning_target_soc", plan.get("target_soc"))
    has_target = target_raw is not None
    target_soc = round(safe_float(target_raw, 0.0), 2) if has_target else None
    target_gap_pct = round(soc_now - safe_float(target_soc, 0.0), 2) if has_target else None
    auto_limit = payload.get("auto_limit") if isinstance(payload.get("auto_limit"), dict) else {}
    auto_contract = storage_auto_limit_contract(
        auto_limit,
        state=state_name,
        mode=mode_value,
        reason=str(payload.get("display_reason") or payload.get("reason") or payload.get("priority") or ""),
        direct_marketing_active=bool(payload.get("direct_marketing_active")),
        direct_marketing_auto_limit_active=bool(payload.get("direct_marketing_pv_store_auto_limit_active")),
    )
    if curve_gap_pct is None:
        curve_position = "no_curve"
    elif curve_gap_pct > 1.0:
        curve_position = "above"
    elif curve_gap_pct < -2.0:
        curve_position = "below"
    else:
        curve_position = "corridor"
    if target_gap_pct is None:
        target_position = "no_target"
    elif target_gap_pct >= 0.0:
        target_position = "target_reached"
    else:
        target_position = "target_missing"
    if not has_curve:
        action_class = "no_curve"
    elif bool(auto_contract.get("release")):
        action_class = "auto_limit_release"
    elif bool(auto_contract.get("enabled")) and safe_int(auto_contract.get("max_charge_w"), 0) <= 0:
        action_class = "auto_limit_hold_zero_charge"
    elif bool(auto_contract.get("enabled")):
        action_class = "auto_limit_charge"
    elif mode_value == MODE_CHRG:
        action_class = "active_charge"
    elif mode_value == MODE_DISCH:
        action_class = "active_discharge"
    elif mode_value == MODE_GRID:
        action_class = "grid_charge"
    elif mode_value == MODE_IDLE:
        action_class = "idle"
    else:
        action_class = "observe_auto"
    return {
        "contract_version": CURVE_CONTEXT_CONTRACT_VERSION,
        "state": state_name,
        "mode": mode_value,
        "mode_name": mode_label(mode_value),
        "soc_now": soc_now,
        "has_curve": has_curve,
        "curve_soc": curve_soc,
        "curve_gap_pct": curve_gap_pct,
        "curve_position": curve_position,
        "has_target": has_target,
        "target_soc": target_soc,
        "target_gap_pct": target_gap_pct,
        "target_position": target_position,
        "action_class": action_class,
        "auto_limit_source_class": auto_contract.get("source_class"),
        "auto_limit_command_class": auto_contract.get("command_class"),
        "auto_limit_charge_w": safe_int(auto_contract.get("max_charge_w"), 0)
        if bool(auto_contract.get("active"))
        else None,
        "auto_limit_discharge_w": safe_int(auto_contract.get("max_discharge_w"), 0)
        if bool(auto_contract.get("active"))
        else None,
        "in_curve_state": state_name.startswith("parallel_curve"),
    }


def _contract_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "ja", "ein")


def _contract_optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    return _contract_bool(value)


def _payload_state_reason(payload: Dict[str, Any]) -> Tuple[str, str, str, str]:
    state_name = str(payload.get("state") or "")
    reason_text = str(payload.get("display_reason") or payload.get("reason") or payload.get("priority") or "")
    return state_name, state_name.lower(), reason_text, reason_text.lower()


def _list_field(*values: Any) -> List[Any]:
    for value in values:
        if isinstance(value, list):
            return value
    return []


def _market_action_from_state(state_l: str) -> str:
    return {
        "market_grid_charge": "grid_charge",
        "market_grid_wait": "grid_charge",
        "market_grid_pv_wait": "grid_charge",
        "market_negative_absorb_grid": "negative_price_absorb",
        "market_negative_absorb_wait": "negative_price_absorb",
        "market_discharge_hold": "hold_discharge",
        "market_house_supply_release": "house_supply_release",
    }.get(state_l, state_l[7:] if state_l.startswith("market_") else "")


def _has_direct_marketing_marker(payload: Dict[str, Any], state_l: str) -> bool:
    monitor = payload.get("direct_marketing_monitor") if isinstance(payload.get("direct_marketing_monitor"), dict) else {}
    monitor_state = str(monitor.get("state") or "").lower()
    return bool(
        state_l.startswith("direct_marketing_")
        or _contract_bool(payload.get("direct_marketing_active"))
        or bool(payload.get("direct_marketing_action"))
        or bool(payload.get("direct_marketing_owner"))
        or _contract_bool(monitor.get("active"))
        or monitor_state in ("active", "hold")
    )


def _has_market_marker(payload: Dict[str, Any], state_l: str) -> bool:
    return bool(
        state_l.startswith("market_")
        or _contract_bool(payload.get("market_economics_active"))
        or bool(payload.get("market_economics_action"))
        or bool(payload.get("market_economics_owner"))
        or _contract_bool(payload.get("scheduled_grid_charge"))
    )


def _has_predump_marker(payload: Dict[str, Any], state_l: str, reason_l: str) -> bool:
    return bool(
        state_l.startswith("pre_discharge")
        or "pre-dump" in reason_l
        or "predump" in reason_l
        or _contract_bool(payload.get("predump_active"))
        or _contract_bool(payload.get("predump_grid_fallback"))
        or _contract_bool(payload.get("predump_hard_predump"))
    )


def storage_market_path_contract(
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Describe market/price storage ownership without changing the decision."""
    plan = plan if isinstance(plan, dict) else {}
    state_name, state_l, reason_text, reason_l = _payload_state_reason(payload)
    market = plan.get("market_plan") if isinstance(plan.get("market_plan"), dict) else {}
    action = str(payload.get("market_economics_action") or _market_action_from_state(state_l) or "")
    active = _has_market_marker(payload, state_l)
    commands_allowed = _contract_optional_bool(
        payload.get("market_economics_commands_allowed", market.get("commands_allowed"))
    )
    shadow = _contract_bool(payload.get("market_economics_shadow", market.get("shadow")))
    veto_reasons: List[str] = []
    if active and commands_allowed is False:
        veto_reasons.append("commands_blocked")
    if active and _has_direct_marketing_marker(payload, state_l):
        veto_reasons.append("competes_direct_marketing")
    if active and _has_predump_marker(payload, state_l, reason_l):
        veto_reasons.append("competes_predump")
    return {
        "contract_version": MARKET_PATH_CONTRACT_VERSION,
        "active": active,
        "path_name": "market_price",
        "state": state_name,
        "action": action,
        "owner": str(payload.get("market_economics_owner") or market.get("plan_owner") or ""),
        "commands_allowed": commands_allowed,
        "shadow": shadow,
        "scheduled_grid_charge": _contract_bool(payload.get("scheduled_grid_charge")),
        "contract_version_seen": safe_int(payload.get("market_economics_contract_version"), 0),
        "dwell_active": _contract_bool(payload.get("market_economics_dwell_active")),
        "dwell_remaining_s": safe_float(payload.get("market_economics_dwell_remaining_s"), 0.0),
        "blocked_reasons": _list_field(payload.get("market_economics_blocked_reasons"), market.get("blocked_reasons")),
        "reason": str(payload.get("market_economics_reason") or reason_text or action),
        "veto_reasons": veto_reasons,
    }


def storage_direct_marketing_path_contract(
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Describe direct-marketing storage ownership without sending commands."""
    state_name, state_l, reason_text, reason_l = _payload_state_reason(payload)
    monitor = payload.get("direct_marketing_monitor") if isinstance(payload.get("direct_marketing_monitor"), dict) else {}
    policy_ctx = direct_marketing_policy_context_from_payload(payload, plan)
    policy_gate = {"allowed": True, "reason": "not_applicable"}
    if policy_ctx.get("present") and isinstance(plan, dict):
        policy_gate = direct_marketing_policy_executor_gate(
            direct_marketing_plan(plan),
            policy_ctx,
            safe_float(payload.get("ts"), time.time()),
            plan,
        )
    policy_active = bool(policy_ctx.get("commands_allowed") and policy_gate.get("allowed"))
    policy_action = direct_marketing_policy_action(str(policy_ctx.get("target_state") or "")) if policy_ctx.get("present") else ""
    action = str(payload.get("direct_marketing_action") or monitor.get("current_action") or monitor.get("action") or policy_action or "")
    active = bool(_has_direct_marketing_marker(payload, state_l) or policy_active)
    if policy_ctx.get("present"):
        commands_allowed = bool(policy_ctx.get("commands_allowed"))
    else:
        commands_allowed = _contract_optional_bool(monitor.get("commands_allowed"))
    shadow = _contract_bool(monitor.get("shadow")) or str(monitor.get("state") or "").lower() == "shadow"
    if policy_ctx.get("present") and not policy_active:
        shadow = True
    pv_store_auto_limit_active = bool(
        _contract_bool(payload.get("direct_marketing_pv_store_auto_limit_active"))
        or _contract_bool(monitor.get("pv_store_auto_limit_active"))
    )
    auto_contract = storage_auto_limit_contract(
        payload.get("auto_limit") if isinstance(payload.get("auto_limit"), dict) else {},
        state=state_name,
        mode=safe_int(payload.get("mode"), MODE_AUTO),
        reason=reason_text,
        direct_marketing_active=active,
        direct_marketing_auto_limit_active=pv_store_auto_limit_active,
    )
    veto_reasons: List[str] = []
    if active and commands_allowed is False:
        veto_reasons.append("commands_blocked")
    if active and _has_market_marker(payload, state_l):
        veto_reasons.append("competes_market")
    if active and _has_predump_marker(payload, state_l, reason_l):
        veto_reasons.append("competes_predump")
    if (
        active
        and pv_store_auto_limit_active
        and bool(auto_contract.get("active"))
        and str(auto_contract.get("source_class") or "") != "direct_marketing"
    ):
        veto_reasons.append("auto_limit_source_mismatch")
    blocked_reasons = _list_field(monitor.get("blocked_reasons"))
    policy_block_reason = str(policy_ctx.get("block_reason") or "")
    if policy_ctx.get("present") and policy_block_reason and not policy_active:
        blocked_reasons.append(policy_block_reason)
    if policy_ctx.get("present") and not policy_gate.get("allowed"):
        blocked_reasons.append(str(policy_gate.get("reason") or "policy_executor_gate_blocked"))
    return {
        "contract_version": DIRECT_MARKETING_PATH_CONTRACT_VERSION,
        "active": active,
        "policy_selected": policy_active,
        "path_name": "direct_marketing",
        "state": state_name,
        "action": action,
        "owner": str(payload.get("direct_marketing_owner") or monitor.get("owner") or ""),
        "monitor_state": monitor.get("state"),
        "commands_allowed": commands_allowed,
        "shadow": shadow,
        "expected_profit_ct_per_kwh": monitor.get("expected_profit_ct_per_kwh"),
        "blocked_reasons": blocked_reasons,
        "policy_schema": policy_ctx.get("schema") if policy_ctx.get("present") else None,
        "policy_target_state": policy_ctx.get("target_state") if policy_ctx.get("present") else None,
        "policy_blocked": policy_ctx.get("blocked") if policy_ctx.get("present") else None,
        "policy_block_reason": policy_block_reason if policy_ctx.get("present") else "",
        "policy_executor_gate": policy_gate if policy_ctx.get("present") else None,
        "policy_export_budget_w": policy_ctx.get("export_budget_w") if policy_ctx.get("present") else 0,
        "policy_charge_budget_w": policy_ctx.get("charge_budget_w") if policy_ctx.get("present") else 0,
        "pv_store_auto_limit_active": pv_store_auto_limit_active,
        "auto_limit_source_class": auto_contract.get("source_class"),
        "auto_limit_command_class": auto_contract.get("command_class"),
        "reason": reason_text or action,
        "veto_reasons": veto_reasons,
    }


def storage_predump_path_contract(
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Describe pre-dump storage ownership without changing discharge output."""
    state_name, state_l, reason_text, reason_l = _payload_state_reason(payload)
    mode_value = safe_int(payload.get("mode"), MODE_AUTO)
    protected = bool(payload.get("protected"))
    active = _has_predump_marker(payload, state_l, reason_l)
    if state_l == "pre_discharge_consumer_auto":
        action = "consumer_auto"
    elif state_l == "pre_discharge_wait":
        action = "consumer_wait"
    elif _contract_bool(payload.get("predump_grid_fallback")):
        action = "grid_fallback"
    elif _contract_bool(payload.get("predump_hard_predump")):
        action = "hard_predump"
    elif active:
        action = "discharge"
    else:
        action = ""
    veto_reasons: List[str] = []
    if active and mode_value != MODE_AUTO and not protected:
        veto_reasons.append("active_mode_unprotected")
    if active and _has_market_marker(payload, state_l):
        veto_reasons.append("competes_market")
    if active and _has_direct_marketing_marker(payload, state_l):
        veto_reasons.append("competes_direct_marketing")
    return {
        "contract_version": PREDUMP_PATH_CONTRACT_VERSION,
        "active": active,
        "path_name": "predump",
        "state": state_name,
        "action": action,
        "mode": mode_value,
        "mode_name": mode_label(mode_value),
        "val_w": max(0, safe_int(payload.get("val"), 0)),
        "protected": protected,
        "grid_fallback": _contract_bool(payload.get("predump_grid_fallback")),
        "consumer_plan_active": bool(
            state_l in ("pre_discharge_wait", "pre_discharge_consumer_auto")
            or _contract_bool(payload.get("predump_consumer_plan_active"))
            or _contract_bool(payload.get("predump_consumer_active"))
        ),
        "floor_soc": payload.get("predump_floor_soc"),
        "target_soc": payload.get("predump_target_soc"),
        "reason": reason_text or action,
        "veto_reasons": veto_reasons,
    }


def storage_protection_path_contract(
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Describe safety/fallback storage ownership without becoming a guard."""
    state_name, state_l, reason_text, _reason_l = _payload_state_reason(payload)
    priority_l = str(payload.get("priority") or "").lower()
    control_reason_l = str(payload.get("reason") or payload.get("priority") or "").lower()
    mode_value = safe_int(payload.get("mode"), MODE_AUTO)
    raw_protected = bool(payload.get("protected"))
    typed_headroom_protected = bool(
        state_l == "parallel_headroom_discharge"
        and mode_value == MODE_DISCH
        and _contract_bool(payload.get("headroom_discharge_active"))
    )
    protected = bool(raw_protected or typed_headroom_protected)
    headroom_active = bool(
        state_l.startswith("parallel_headroom")
        or _contract_bool(payload.get("abregel_active"))
        or _contract_bool(payload.get("headroom_active"))
        or _contract_bool(payload.get("export_headroom_active"))
        or _contract_bool(payload.get("protection_headroom_active"))
    )
    live_glitch = any(
        _contract_bool(payload.get(key))
        for key in (
            "live_plausibility_preserved_auto_limit",
            "live_plausibility_preserved_wbminsoc_contract",
            "live_plausibility_preserved_discharge_owner",
            "live_plausibility_preserved_charge_owner",
            "live_plausibility_manual_override_kept",
        )
    )
    active = bool(
        state_l in ("parallel_no_data", "parallel_passthrough", "parallel_emergency_auto", "live_stale_auto")
        or priority_l == "safety"
        or headroom_active
        or state_l.startswith("hard_mode_guard")
        or state_l.startswith("live_plausibility")
        or live_glitch
        or any(
            token in control_reason_l
            for token in (
                "notstrom",
                "schutz",
                "plausibil",
                "glitch",
                "stale",
                "no data",
                "keine daten",
                "hard-mode",
                "hard mode",
            )
        )
        or (headroom_active and "abregel" in control_reason_l)
    )
    if state_l in ("parallel_no_data", "parallel_passthrough") or "no data" in control_reason_l or "keine daten" in control_reason_l:
        protection_class = "no_data"
    elif state_l.startswith("live_plausibility") or live_glitch or "plausibil" in control_reason_l or "glitch" in control_reason_l:
        protection_class = "live_plausibility"
    elif state_l.startswith("hard_mode_guard") or "hard-mode" in control_reason_l or "hard mode" in control_reason_l:
        protection_class = "hard_mode_guard"
    elif headroom_active:
        protection_class = "headroom"
    elif (
        state_l == "parallel_emergency_auto"
        or "notstrom" in control_reason_l
        or (priority_l == "safety" and "reserve" in control_reason_l)
    ):
        protection_class = "emergency"
    elif "stale" in control_reason_l:
        protection_class = "stale"
    elif active:
        protection_class = "generic"
    else:
        protection_class = "none"
    auto_contract = storage_auto_limit_contract(
        payload.get("auto_limit") if isinstance(payload.get("auto_limit"), dict) else {},
        state=state_name,
        mode=mode_value,
        reason=reason_text,
        direct_marketing_active=bool(payload.get("direct_marketing_active")),
        direct_marketing_auto_limit_active=bool(payload.get("direct_marketing_pv_store_auto_limit_active")),
    )
    veto_reasons: List[str] = []
    if active and protection_class == "no_data" and mode_value != MODE_AUTO:
        veto_reasons.append("no_data_not_auto")
    if active and mode_value in (MODE_DISCH, MODE_CHRG, MODE_GRID) and not protected:
        veto_reasons.append("active_mode_unprotected")
    return {
        "contract_version": PROTECTION_PATH_CONTRACT_VERSION,
        "active": active,
        "path_name": "protection",
        "state": state_name,
        "protection_class": protection_class,
        "mode": mode_value,
        "mode_name": mode_label(mode_value),
        "protected": protected,
        "raw_protected": raw_protected,
        "typed_headroom_protected": typed_headroom_protected,
        "protection_binding": (
            "typed_headroom_discharge"
            if typed_headroom_protected
            else ("explicit_protected" if raw_protected else "none")
        ),
        "auto_limit_source_class": auto_contract.get("source_class"),
        "auto_limit_command_class": auto_contract.get("command_class"),
        "reason": reason_text,
        "veto_reasons": veto_reasons,
    }


def storage_budget_readiness_contract(
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Classify shared surplus-budget readiness without changing control output."""
    _ = plan
    budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
    energy_score = budget.get("energy_score") if isinstance(budget.get("energy_score"), dict) else {}
    grid_w = safe_int(payload.get("grid_w"), 0)
    grid_export_w = max(0, -grid_w)
    grid_import_w = max(0, grid_w)
    budget_w = max(0, safe_int(budget.get("budget_w"), 0))
    raw_iaval_w = max(0, safe_int(budget.get("raw_iAVal_w"), 0))
    iaval_w = max(0, safe_int(budget.get("iAVal_w"), budget_w))
    free_for_consumers_w = max(
        0,
        safe_int(
            energy_score.get(
                "free_for_consumers_raw_w",
                energy_score.get("free_for_limbs_w"),
            ),
            budget_w,
        ),
    )
    storage_req_w = max(0, safe_int(energy_score.get("bat_charge_request_w"), 0))
    storage_reserved_w = max(
        storage_req_w,
        max(0, safe_int(payload.get("iFc_w"), 0)),
        max(0, safe_int(payload.get("iMinLade_w"), 0)),
    )
    min_candidates = [
        budget.get("min_required_w"),
        budget.get("wb_min_required_w"),
        payload.get("wb_min_required_w"),
        payload.get("min_required_w"),
    ]
    min_consumer_w = max(
        [max(0, safe_int(candidate, 0)) for candidate in min_candidates]
        or [0]
    )
    car_present = bool(payload.get("wb_car_present"))
    possible_power_w = max(0, safe_int(payload.get("wb_possible_power_w"), 0))
    physical_chargeable = budget.get("physical_chargeable")
    data_valid = bool(
        not _contract_bool(payload.get("live_stale"))
        and _contract_bool(payload.get("live_sample_valid", True))
        and _contract_bool(payload.get("home_power_valid", True))
        and _contract_bool(payload.get("grid_power_valid", True))
    )
    consumer_shortfall_w = max(0, min_consumer_w - free_for_consumers_w) if min_consumer_w > 0 else 0
    blockers: List[str] = []
    if not data_valid:
        blockers.append("data_quality")
    if grid_import_w > 500 and free_for_consumers_w <= 0:
        blockers.append("grid_import_guard")
    if storage_reserved_w > 0 and free_for_consumers_w <= 0:
        blockers.append("storage_reserved")
    if consumer_shortfall_w > 0:
        blockers.append("below_consumer_minimum")
    if physical_chargeable is False:
        blockers.append("physical_not_chargeable")
    if not data_valid:
        readiness_class = "data_invalid"
    elif grid_import_w > 500 and free_for_consumers_w <= 0:
        readiness_class = "import_guard"
    elif min_consumer_w > 0 and free_for_consumers_w >= min_consumer_w and (car_present or possible_power_w > 0):
        readiness_class = "consumer_min_met"
    elif min_consumer_w > 0 and grid_export_w > 0 and free_for_consumers_w < min_consumer_w:
        readiness_class = "export_below_minimum"
    elif storage_reserved_w > 0 and free_for_consumers_w <= 0:
        readiness_class = "storage_reserving"
    elif grid_export_w > 0 and free_for_consumers_w > 0:
        readiness_class = "consumer_budget_available"
    elif grid_export_w > 0:
        readiness_class = "export_available"
    else:
        readiness_class = "idle_or_balanced"
    if grid_import_w > 0:
        balance_class = "import"
    elif grid_export_w > 0:
        balance_class = "export"
    else:
        balance_class = "balanced"
    return {
        "contract_version": BUDGET_READINESS_CONTRACT_VERSION,
        "readiness_class": readiness_class,
        "balance_class": balance_class,
        "data_valid": data_valid,
        "blockers": blockers,
        "grid_w": grid_w,
        "grid_export_w": grid_export_w,
        "grid_import_w": grid_import_w,
        "budget_w": budget_w,
        "raw_iAVal_w": raw_iaval_w,
        "iAVal_w": iaval_w,
        "free_for_consumers_w": free_for_consumers_w,
        "storage_req_w": storage_req_w,
        "storage_reserved_w": storage_reserved_w,
        "min_consumer_w": min_consumer_w,
        "consumer_shortfall_w": consumer_shortfall_w,
        "consumer_min_met": bool(min_consumer_w > 0 and free_for_consumers_w >= min_consumer_w),
        "car_present": car_present,
        "possible_power_w": possible_power_w,
        "physical_chargeable": physical_chargeable,
        "physical_reason": str(budget.get("physical_reason") or ""),
        "live_stale": bool(payload.get("live_stale")),
        "live_sample_valid": bool(payload.get("live_sample_valid", True)),
        "home_power_valid": bool(payload.get("home_power_valid", True)),
        "grid_power_valid": bool(payload.get("grid_power_valid", True)),
    }


def storage_budget_arbitration_contract(
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
    readiness: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a shadow-only shared-budget allocation without changing outputs."""
    plan = plan if isinstance(plan, dict) else {}
    readiness_contract = readiness if isinstance(readiness, dict) else storage_budget_readiness_contract(payload, plan)
    path_contract = storage_decision_path_contract(payload, plan)
    budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
    energy_score = budget.get("energy_score") if isinstance(budget.get("energy_score"), dict) else {}
    consumer_allocations = budget.get("consumer_allocations")
    if not isinstance(consumer_allocations, dict):
        consumer_allocations = energy_score.get("consumer_allocations")
    if not isinstance(consumer_allocations, dict):
        consumer_allocations = {}

    available_w = max(0, safe_int(readiness_contract.get("free_for_consumers_w"), 0))
    remaining_w = available_w
    min_wallbox_w = max(0, safe_int(readiness_contract.get("min_consumer_w"), 0))
    wallbox_possible_w = max(0, safe_int(readiness_contract.get("possible_power_w"), 0))
    wallbox_candidate = bool(readiness_contract.get("car_present")) and wallbox_possible_w > 0 and min_wallbox_w > 0
    heatpump_request_w = max(
        0,
        safe_int(consumer_allocations.get("heatpump"), 0),
        safe_int(energy_score.get("heatpump_request_w"), 0),
        safe_int(budget.get("heatpump_budget_w"), 0),
        safe_int(payload.get("heatpump_budget_w"), 0),
    )
    storage_reserved_w = max(0, safe_int(readiness_contract.get("storage_reserved_w"), 0))
    export_observed_w = max(0, safe_int(readiness_contract.get("grid_export_w"), 0))
    data_valid = bool(readiness_contract.get("data_valid"))
    readiness_class = str(readiness_contract.get("readiness_class") or "")
    blockers = list(readiness_contract.get("blockers")) if isinstance(readiness_contract.get("blockers"), list) else []
    candidates: List[Dict[str, Any]] = []
    allocations = {
        "storage_reserved_w": storage_reserved_w,
        "wallbox_w": 0,
        "heatpump_w": 0,
        "export_w": 0,
        "direct_marketing_export_w": 0,
    }

    def add_candidate(
        sink: str,
        priority: int,
        request_w: int,
        *,
        min_w: int = 0,
        eligible: bool = True,
        allocation_w: int = 0,
        blocker: str = "",
        reason: str = "",
    ) -> None:
        candidates.append({
            "sink": sink,
            "priority": int(priority),
            "request_w": max(0, int(request_w)),
            "min_w": max(0, int(min_w)),
            "eligible": bool(eligible),
            "allocation_w": max(0, int(allocation_w)),
            "blocker": blocker,
            "reason": reason,
        })

    if storage_reserved_w > 0:
        add_candidate(
            "storage_reserved",
            10,
            storage_reserved_w,
            eligible=True,
            allocation_w=storage_reserved_w,
            reason="storage_curve_or_reserve",
        )

    blocked_candidates: List[str] = []
    if not data_valid:
        arbitration_class = "blocked_data_quality"
        primary_sink = "none"
        blocked_candidates = ["wallbox", "heatpump", "export"]
    elif readiness_class == "import_guard":
        arbitration_class = "blocked_import_guard"
        primary_sink = "none"
        blocked_candidates = ["wallbox", "heatpump", "export"]
    else:
        if wallbox_candidate:
            if remaining_w >= min_wallbox_w:
                wallbox_request_w = wallbox_possible_w if wallbox_possible_w > 0 else remaining_w
                allocation_w = min(remaining_w, wallbox_request_w)
                allocations["wallbox_w"] = allocation_w
                remaining_w = max(0, remaining_w - allocation_w)
                add_candidate(
                    "wallbox",
                    20,
                    wallbox_request_w,
                    min_w=min_wallbox_w,
                    eligible=True,
                    allocation_w=allocation_w,
                    reason="minimum_met",
                )
            else:
                blocked_candidates.append("wallbox:below_minimum")
                add_candidate(
                    "wallbox",
                    20,
                    wallbox_possible_w,
                    min_w=min_wallbox_w,
                    eligible=False,
                    allocation_w=0,
                    blocker="below_minimum",
                    reason="available_below_minimum",
                )
        if heatpump_request_w > 0:
            if remaining_w > 0:
                allocation_w = min(remaining_w, heatpump_request_w)
                allocations["heatpump_w"] = allocation_w
                remaining_w = max(0, remaining_w - allocation_w)
                add_candidate(
                    "heatpump",
                    30,
                    heatpump_request_w,
                    eligible=True,
                    allocation_w=allocation_w,
                    reason="budget_after_wallbox",
                )
            else:
                blocked_candidates.append("heatpump:no_budget")
                add_candidate(
                    "heatpump",
                    30,
                    heatpump_request_w,
                    eligible=False,
                    allocation_w=0,
                    blocker="no_budget",
                    reason="budget_consumed_or_reserved",
                )
        export_sink = (
            "direct_marketing_export"
            if str(path_contract.get("primary_path") or "") == "direct_marketing"
            else "grid_export"
        )
        if remaining_w > 0 or export_observed_w > 0:
            if export_sink == "direct_marketing_export":
                allocations["direct_marketing_export_w"] = remaining_w
            else:
                allocations["export_w"] = remaining_w
            add_candidate(
                export_sink,
                90,
                export_observed_w,
                eligible=remaining_w > 0 or export_observed_w > 0,
                allocation_w=remaining_w,
                reason="residual_or_observed_export",
            )
        if allocations["wallbox_w"] > 0:
            arbitration_class = "wallbox_allocated"
            primary_sink = "wallbox"
        elif allocations["heatpump_w"] > 0:
            arbitration_class = "heatpump_allocated"
            primary_sink = "heatpump"
        elif storage_reserved_w > 0 and available_w <= 0:
            arbitration_class = "storage_reserving"
            primary_sink = "storage_reserved"
        elif wallbox_candidate and allocations["wallbox_w"] <= 0:
            arbitration_class = "wallbox_minimum_blocked"
            primary_sink = export_sink if (remaining_w > 0 or export_observed_w > 0) else "none"
        elif remaining_w > 0 or export_observed_w > 0:
            arbitration_class = "export_remaining"
            primary_sink = export_sink
        else:
            arbitration_class = "idle_or_balanced"
            primary_sink = "none"

    return {
        "contract_version": BUDGET_ARBITRATION_CONTRACT_VERSION,
        "shadow_only": True,
        "arbitration_class": arbitration_class,
        "primary_sink": primary_sink,
        "reserved_sink": "storage" if storage_reserved_w > 0 else "none",
        "available_for_arbitration_w": available_w,
        "remaining_after_consumers_w": max(0, remaining_w),
        "observed_export_w": export_observed_w,
        "readiness_class": readiness_class,
        "readiness_contract_version": safe_int(readiness_contract.get("contract_version"), 0),
        "decision_primary_path": path_contract.get("primary_path"),
        "allocations": allocations,
        "candidates": candidates,
        "blocked_candidates": blocked_candidates,
        "blockers": blockers,
        "data_valid": data_valid,
        "wallbox_min_w": min_wallbox_w,
        "wallbox_possible_w": wallbox_possible_w,
        "heatpump_request_w": heatpump_request_w,
        "storage_reserved_w": storage_reserved_w,
    }


def _budget_stability_min_hold_s(sink: str) -> int:
    return {
        "wallbox": 180,
        "heatpump": 900,
        "storage_reserved": 300,
        "direct_marketing_export": 180,
        "grid_export": 30,
        "none": 0,
    }.get(str(sink or "none"), 120)


def _budget_arbitration_allocation_signature(arbitration: Dict[str, Any]) -> str:
    allocations = arbitration.get("allocations") if isinstance(arbitration.get("allocations"), dict) else {}
    return "%s:%s:%d:%d:%d:%d" % (
        arbitration.get("primary_sink") or "none",
        arbitration.get("reserved_sink") or "none",
        safe_int(allocations.get("wallbox_w"), 0),
        safe_int(allocations.get("heatpump_w"), 0),
        safe_int(allocations.get("export_w"), 0),
        safe_int(allocations.get("direct_marketing_export_w"), 0),
    )


def _budget_primary_allocation_w(arbitration: Dict[str, Any], sink: str) -> int:
    allocations = arbitration.get("allocations") if isinstance(arbitration.get("allocations"), dict) else {}
    if sink == "wallbox":
        return max(0, safe_int(allocations.get("wallbox_w"), 0))
    if sink == "heatpump":
        return max(0, safe_int(allocations.get("heatpump_w"), 0))
    if sink == "direct_marketing_export":
        return max(0, safe_int(allocations.get("direct_marketing_export_w"), 0))
    if sink == "grid_export":
        return max(0, safe_int(allocations.get("export_w"), 0))
    if sink == "storage_reserved":
        return max(0, safe_int(allocations.get("storage_reserved_w"), 0))
    return 0


def storage_budget_stability_contract(
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
    arbitration: Optional[Dict[str, Any]] = None,
    previous: Optional[Dict[str, Any]] = None,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Debounce the shadow budget arbitration without changing live outputs."""
    plan = plan if isinstance(plan, dict) else {}
    current_arbitration = (
        arbitration
        if isinstance(arbitration, dict)
        else storage_budget_arbitration_contract(payload, plan)
    )
    previous_state = previous if isinstance(previous, dict) else {}
    now_value = float(now_s if now_s is not None else safe_float(payload.get("ts"), time.time()))
    requested_sink = str(current_arbitration.get("primary_sink") or "none")
    requested_class = str(current_arbitration.get("arbitration_class") or "")
    requested_allocation_w = _budget_primary_allocation_w(current_arbitration, requested_sink)
    requested_signature = _budget_arbitration_allocation_signature(current_arbitration)
    data_valid = bool(current_arbitration.get("data_valid", True))
    safety_block = bool(
        not data_valid
        or requested_class in ("blocked_data_quality", "blocked_import_guard")
    )
    previous_sink = str(previous_state.get("stable_primary_sink") or previous_state.get("primary_sink") or "")
    previous_since = safe_float(previous_state.get("since_ts"), 0.0)
    previous_allocation_w = max(0, safe_int(previous_state.get("allocation_w"), 0))
    previous_signature = str(previous_state.get("allocation_signature") or "")
    switch_deadband_w = max(100, safe_int(payload.get("budget_arbitration_switch_deadband_w"), 300))

    if safety_block:
        stable_sink = requested_sink
        since_ts = now_value
        stability_class = "safety_release"
        action = "release_to_safe"
        min_hold_s = 0
        hold_remaining_s = 0.0
        blocked_reason = requested_class or "data_invalid"
    elif not previous_sink:
        stable_sink = requested_sink
        since_ts = now_value
        stability_class = "init"
        action = "accept"
        min_hold_s = _budget_stability_min_hold_s(stable_sink)
        hold_remaining_s = float(min_hold_s)
        blocked_reason = ""
    elif previous_sink == requested_sink:
        stable_sink = requested_sink
        since_ts = previous_since if previous_since > 0 else now_value
        stability_class = "stable"
        action = "hold_same_sink"
        min_hold_s = _budget_stability_min_hold_s(stable_sink)
        age_s = max(0.0, now_value - since_ts)
        hold_remaining_s = max(0.0, float(min_hold_s) - age_s)
        blocked_reason = ""
    else:
        min_hold_s = _budget_stability_min_hold_s(previous_sink)
        since_ts = previous_since if previous_since > 0 else now_value
        age_s = max(0.0, now_value - since_ts)
        previous_delta_w = abs(requested_allocation_w - previous_allocation_w)
        if age_s < min_hold_s:
            stable_sink = previous_sink
            stability_class = "hold_previous_min_runtime"
            action = "block_switch"
            hold_remaining_s = max(0.0, float(min_hold_s) - age_s)
            blocked_reason = "min_runtime"
        elif previous_delta_w < switch_deadband_w and requested_signature != previous_signature:
            stable_sink = previous_sink
            stability_class = "hold_previous_deadband"
            action = "block_switch"
            hold_remaining_s = 0.0
            blocked_reason = "deadband"
        else:
            stable_sink = requested_sink
            since_ts = now_value
            stability_class = "switch_allowed"
            action = "switch"
            min_hold_s = _budget_stability_min_hold_s(stable_sink)
            hold_remaining_s = float(min_hold_s)
            blocked_reason = ""

    stable_allocation_w = (
        requested_allocation_w
        if stable_sink == requested_sink
        else previous_allocation_w
    )
    next_state = {
        "contract_version": BUDGET_STABILITY_CONTRACT_VERSION,
        "stable_primary_sink": stable_sink,
        "primary_sink": stable_sink,
        "since_ts": since_ts,
        "allocation_w": stable_allocation_w,
        "allocation_signature": requested_signature if stable_sink == requested_sink else previous_signature,
        "ts": now_value,
    }
    return {
        "contract_version": BUDGET_STABILITY_CONTRACT_VERSION,
        "shadow_only": True,
        "stability_class": stability_class,
        "action": action,
        "requested_primary_sink": requested_sink,
        "stable_primary_sink": stable_sink,
        "previous_primary_sink": previous_sink or "none",
        "requested_arbitration_class": requested_class,
        "requested_allocation_w": requested_allocation_w,
        "stable_allocation_w": stable_allocation_w,
        "switch_deadband_w": switch_deadband_w,
        "min_hold_s": min_hold_s,
        "hold_remaining_s": round(hold_remaining_s, 1),
        "blocked_reason": blocked_reason,
        "data_valid": data_valid,
        "safety_block": safety_block,
        "allocation_signature": requested_signature,
        "next_state": next_state,
    }


def storage_budget_executor_gate_contract(
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
    arbitration: Optional[Dict[str, Any]] = None,
    stability: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Describe whether a future budget executor would be allowed to act."""
    plan = plan if isinstance(plan, dict) else {}
    current_arbitration = (
        arbitration
        if isinstance(arbitration, dict)
        else storage_budget_arbitration_contract(payload, plan)
    )
    current_stability = (
        stability
        if isinstance(stability, dict)
        else storage_budget_stability_contract(payload, plan, current_arbitration)
    )
    allocations = (
        current_arbitration.get("allocations")
        if isinstance(current_arbitration.get("allocations"), dict)
        else {}
    )
    requested_sink = str(
        current_stability.get("requested_primary_sink")
        or current_arbitration.get("primary_sink")
        or "none"
    )
    stable_sink = str(current_stability.get("stable_primary_sink") or "none")
    stability_class = str(current_stability.get("stability_class") or "")
    stability_action = str(current_stability.get("action") or "")
    arbitration_class = str(current_arbitration.get("arbitration_class") or "")
    hold_remaining_s = safe_float(current_stability.get("hold_remaining_s"), 0.0)
    stable_ready = (
        stable_sink == requested_sink
        and stability_class == "stable"
        and hold_remaining_s <= 0.0
        and stability_action != "block_switch"
    )
    stable_allocation_w = max(0, safe_int(current_stability.get("stable_allocation_w"), 0))
    arbitration_target_w = _budget_primary_allocation_w(current_arbitration, stable_sink)
    target_w = arbitration_target_w if stable_sink == requested_sink else stable_allocation_w
    if stable_sink == requested_sink:
        if stable_sink == "wallbox":
            target_w = max(0, safe_int(allocations.get("wallbox_w"), target_w))
        elif stable_sink == "heatpump":
            target_w = max(0, safe_int(allocations.get("heatpump_w"), target_w))
        elif stable_sink == "grid_export":
            target_w = max(0, safe_int(allocations.get("export_w"), target_w))
        elif stable_sink == "direct_marketing_export":
            target_w = max(0, safe_int(allocations.get("direct_marketing_export_w"), target_w))
        elif stable_sink == "storage_reserved":
            target_w = max(0, safe_int(allocations.get("storage_reserved_w"), target_w))

    blockers: List[str] = []

    def add_blocker(reason: str) -> None:
        if reason and reason not in blockers:
            blockers.append(reason)

    for blocker in current_arbitration.get("blockers", []):
        if isinstance(blocker, str):
            add_blocker(blocker)
    for blocker in current_arbitration.get("blocked_candidates", []):
        if isinstance(blocker, str):
            add_blocker(blocker)

    data_valid = bool(current_arbitration.get("data_valid", True)) and bool(
        current_stability.get("data_valid", True)
    )
    safety_block = bool(current_stability.get("safety_block"))
    min_wallbox_w = max(0, safe_int(current_arbitration.get("wallbox_min_w"), 0))
    gate_open_shadow = False
    if arbitration_class == "blocked_import_guard":
        gate_class = "blocked_import_guard"
        add_blocker("import_guard")
    elif not data_valid or safety_block or stability_class == "safety_release":
        gate_class = "blocked_data_quality"
        add_blocker(str(current_stability.get("blocked_reason") or "data_quality"))
    elif stable_sink != requested_sink or stability_action == "block_switch" or stability_class.startswith("hold_previous"):
        gate_class = "blocked_stability_hold"
        add_blocker(str(current_stability.get("blocked_reason") or "stability_hold"))
    elif stable_sink in ("none", ""):
        gate_class = "blocked_no_sink"
        add_blocker("no_sink")
    elif stable_sink in ("grid_export", "direct_marketing_export"):
        gate_class = "export_observe_only"
        add_blocker("export_observe_only")
    elif stable_sink == "storage_reserved":
        gate_class = "storage_reserved_observe_only"
        add_blocker("storage_reserved_observe_only")
    elif not stable_ready:
        gate_class = "blocked_stability_hold"
        add_blocker("stability_warmup")
    elif stable_sink == "wallbox":
        if target_w <= 0:
            gate_class = "blocked_no_budget"
            add_blocker("no_wallbox_budget")
        elif min_wallbox_w > 0 and target_w < min_wallbox_w:
            gate_class = "blocked_minimum"
            add_blocker("below_wallbox_minimum")
        else:
            gate_class = "shadow_ready_wallbox"
            gate_open_shadow = True
    elif stable_sink == "heatpump":
        if target_w <= 0:
            gate_class = "blocked_no_budget"
            add_blocker("no_heatpump_budget")
        else:
            gate_class = "shadow_ready_heatpump"
            gate_open_shadow = True
    else:
        gate_class = "blocked_unknown_sink"
        add_blocker("unknown_sink")

    return {
        "contract_version": BUDGET_EXECUTOR_GATE_CONTRACT_VERSION,
        "shadow_only": True,
        "gate_open_shadow": gate_open_shadow,
        "gate_class": gate_class,
        "requested_sink": requested_sink,
        "stable_sink": stable_sink,
        "target_sink": stable_sink,
        "target_w": max(0, target_w),
        "blockers": blockers,
        "arbitration_class": arbitration_class,
        "arbitration_contract_version": safe_int(current_arbitration.get("contract_version"), 0),
        "stability_class": stability_class,
        "stability_action": stability_action,
        "stability_contract_version": safe_int(current_stability.get("contract_version"), 0),
        "stability_hold_remaining_s": hold_remaining_s,
        "stable_ready": stable_ready,
        "data_valid": data_valid,
        "would_write_consumer_allocations": False,
        "would_send_rscp": False,
        "would_command_wallbox": False,
        "would_command_heatpump": False,
    }


def _budget_executor_latch_min_runtime_s(sink: str) -> int:
    return {
        "wallbox": 180,
        "heatpump": 900,
    }.get(str(sink or "none"), 0)


def _budget_executor_gate_signature(gate: Dict[str, Any]) -> str:
    return "%s:%s:%d" % (
        gate.get("gate_class") or "unknown",
        gate.get("target_sink") or "none",
        max(0, safe_int(gate.get("target_w"), 0)),
    )


def storage_budget_executor_latch_contract(
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
    gate: Optional[Dict[str, Any]] = None,
    previous: Optional[Dict[str, Any]] = None,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Track when a future budget executor output would be accepted."""
    plan = plan if isinstance(plan, dict) else {}
    current_gate = gate if isinstance(gate, dict) else storage_budget_executor_gate_contract(payload, plan)
    previous_state = previous if isinstance(previous, dict) else {}
    now_value = float(now_s if now_s is not None else safe_float(payload.get("ts"), time.time()))
    gate_class = str(current_gate.get("gate_class") or "")
    gate_open = bool(current_gate.get("gate_open_shadow"))
    target_sink = str(current_gate.get("target_sink") or "none")
    target_w = max(0, safe_int(current_gate.get("target_w"), 0))
    controllable_sink = target_sink in ("wallbox", "heatpump")
    gate_signature = _budget_executor_gate_signature(current_gate)
    gate_blockers = [
        str(blocker)
        for blocker in current_gate.get("blockers", [])
        if isinstance(blocker, str) and blocker
    ]
    blockers: List[str] = []

    def add_blocker(reason: str) -> None:
        if reason and reason not in blockers:
            blockers.append(reason)

    for blocker in gate_blockers:
        add_blocker(blocker)

    safety_release = bool(
        gate_class in ("blocked_import_guard", "blocked_data_quality")
        or not bool(current_gate.get("data_valid", True))
    )
    previous_active = bool(
        previous_state.get("accepted_active_shadow", previous_state.get("accepted_active", False))
    )
    previous_sink = str(previous_state.get("accepted_sink") or "none")
    previous_since_ts = safe_float(previous_state.get("accepted_since_ts"), 0.0)
    if previous_since_ts <= 0.0:
        previous_since_ts = now_value
    previous_target_w = max(0, safe_int(previous_state.get("accepted_target_w"), 0))
    previous_signature = str(previous_state.get("accepted_signature") or "")
    previous_min_runtime_s = max(
        0,
        safe_int(
            previous_state.get("min_runtime_s"),
            _budget_executor_latch_min_runtime_s(previous_sink),
        ),
    )
    previous_age_s = max(0.0, now_value - previous_since_ts) if previous_active else 0.0
    previous_hold_remaining_s = max(0.0, float(previous_min_runtime_s) - previous_age_s)

    accepted_active = False
    accepted_sink = "none"
    accepted_since_ts = 0.0
    accepted_target_w = 0
    accepted_signature = ""
    min_runtime_s = 0
    accepted_age_s = 0.0
    hold_remaining_s = 0.0
    release_allowed_shadow = True
    hold_previous_output_shadow = False

    if safety_release:
        latch_class = "safety_release"
        action = "release_to_safe"
        add_blocker("safety_release")
    elif not previous_active:
        if gate_open and controllable_sink and target_w > 0:
            accepted_active = True
            accepted_sink = target_sink
            accepted_since_ts = now_value
            accepted_target_w = target_w
            accepted_signature = gate_signature
            min_runtime_s = _budget_executor_latch_min_runtime_s(accepted_sink)
            accepted_age_s = 0.0
            hold_remaining_s = float(min_runtime_s)
            release_allowed_shadow = min_runtime_s <= 0
            latch_class = "accepted_new"
            action = "accept_shadow_output"
        elif gate_open:
            latch_class = "blocked_not_controllable"
            action = "observe"
            add_blocker("not_controllable_sink")
        else:
            latch_class = "idle_closed"
            action = "observe"
    else:
        same_sink_open = gate_open and controllable_sink and target_sink == previous_sink and target_w > 0
        different_sink_open = gate_open and controllable_sink and target_sink != previous_sink and target_w > 0
        if same_sink_open:
            accepted_active = True
            accepted_sink = previous_sink
            accepted_since_ts = previous_since_ts
            accepted_target_w = target_w
            accepted_signature = gate_signature
            min_runtime_s = previous_min_runtime_s
            accepted_age_s = previous_age_s
            hold_remaining_s = previous_hold_remaining_s
            release_allowed_shadow = hold_remaining_s <= 0.0
            if hold_remaining_s > 0.0:
                latch_class = "accepted_min_runtime"
                action = "hold_accepted"
            else:
                latch_class = "accepted_runtime_satisfied"
                action = "keep_accepted"
        elif different_sink_open:
            if previous_hold_remaining_s > 0.0:
                accepted_active = True
                accepted_sink = previous_sink
                accepted_since_ts = previous_since_ts
                accepted_target_w = previous_target_w
                accepted_signature = previous_signature
                min_runtime_s = previous_min_runtime_s
                accepted_age_s = previous_age_s
                hold_remaining_s = previous_hold_remaining_s
                release_allowed_shadow = False
                hold_previous_output_shadow = True
                latch_class = "switch_blocked_min_runtime"
                action = "hold_accepted"
                add_blocker("min_runtime")
            else:
                accepted_active = True
                accepted_sink = target_sink
                accepted_since_ts = now_value
                accepted_target_w = target_w
                accepted_signature = gate_signature
                min_runtime_s = _budget_executor_latch_min_runtime_s(accepted_sink)
                accepted_age_s = 0.0
                hold_remaining_s = float(min_runtime_s)
                release_allowed_shadow = min_runtime_s <= 0
                latch_class = "accepted_switch"
                action = "switch_accept_shadow_output"
        else:
            if previous_hold_remaining_s > 0.0:
                accepted_active = True
                accepted_sink = previous_sink
                accepted_since_ts = previous_since_ts
                accepted_target_w = previous_target_w
                accepted_signature = previous_signature
                min_runtime_s = previous_min_runtime_s
                accepted_age_s = previous_age_s
                hold_remaining_s = previous_hold_remaining_s
                release_allowed_shadow = False
                hold_previous_output_shadow = True
                latch_class = "release_blocked_min_runtime"
                action = "hold_accepted"
                add_blocker("min_runtime")
            else:
                latch_class = "release_after_min_runtime"
                action = "release"

    next_state = {
        "contract_version": BUDGET_EXECUTOR_LATCH_CONTRACT_VERSION,
        "accepted_active_shadow": bool(accepted_active),
        "accepted_sink": accepted_sink if accepted_active else "none",
        "accepted_since_ts": accepted_since_ts if accepted_active else 0.0,
        "accepted_target_w": max(0, accepted_target_w) if accepted_active else 0,
        "accepted_signature": accepted_signature if accepted_active else "",
        "min_runtime_s": max(0, min_runtime_s) if accepted_active else 0,
        "accepted_source": "shadow_gate" if accepted_active else "",
        "ts": now_value,
    }
    return {
        "contract_version": BUDGET_EXECUTOR_LATCH_CONTRACT_VERSION,
        "shadow_only": True,
        "latch_class": latch_class,
        "action": action,
        "accepted_active_shadow": bool(accepted_active),
        "accepted_sink": accepted_sink if accepted_active else "none",
        "accepted_target_w": max(0, accepted_target_w) if accepted_active else 0,
        "accepted_signature": accepted_signature if accepted_active else "",
        "accepted_age_s": round(accepted_age_s, 1),
        "min_runtime_s": max(0, min_runtime_s),
        "hold_remaining_s": round(hold_remaining_s, 1),
        "release_allowed_shadow": bool(release_allowed_shadow),
        "hold_previous_output_shadow": bool(hold_previous_output_shadow),
        "gate_class": gate_class,
        "gate_open_shadow": gate_open,
        "gate_target_sink": target_sink,
        "gate_target_w": target_w,
        "gate_signature": gate_signature,
        "previous_active_shadow": previous_active,
        "previous_sink": previous_sink if previous_active else "none",
        "previous_age_s": round(previous_age_s, 1),
        "blockers": blockers,
        "safety_release": safety_release,
        "requires_executor_ack": True,
        "would_write_consumer_allocations": False,
        "would_send_rscp": False,
        "would_command_wallbox": False,
        "would_command_heatpump": False,
        "next_state": next_state,
    }


def _budget_executor_ack_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(payload.get("budget_executor_ack"), dict):
        return dict(payload.get("budget_executor_ack") or {})
    budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
    ack = budget.get("executor_ack") if isinstance(budget.get("executor_ack"), dict) else {}
    return dict(ack) if isinstance(ack, dict) else {}


def _budget_executor_ack_source_allowed(source: str) -> bool:
    return str(source or "").strip() in (
        "storage_budget_executor",
        "storage_manager_budget_executor",
    )


def storage_budget_executor_central_ack_contract(
    latch: Optional[Dict[str, Any]],
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Erzeugt den einzigen zentralen Ack-Payload für akzeptierte Budgets."""
    current_latch = latch if isinstance(latch, dict) else {}
    now_value = float(now_s if now_s is not None else time.time())
    accepted_active = bool(current_latch.get("accepted_active_shadow"))
    accepted_sink = str(current_latch.get("accepted_sink") or "none")
    accepted_target_w = max(0, safe_int(current_latch.get("accepted_target_w"), 0))
    accepted_signature = str(
        current_latch.get("accepted_signature")
        or current_latch.get("gate_signature")
        or ""
    )
    blockers: List[str] = []

    def add_blocker(reason: str) -> None:
        if reason and reason not in blockers:
            blockers.append(reason)

    ack_payload: Dict[str, Any] = {}
    if not accepted_active:
        ack_class = "central_ack_idle"
        add_blocker("latch_inactive")
    elif accepted_sink not in ("wallbox", "heatpump"):
        ack_class = "central_ack_blocked_sink"
        add_blocker("sink_not_controllable")
    elif accepted_target_w <= 0:
        ack_class = "central_ack_blocked_target"
        add_blocker("target_zero")
    elif not accepted_signature:
        ack_class = "central_ack_blocked_signature"
        add_blocker("signature_missing")
    else:
        ack_class = "central_ack_emitted"
        ack_payload = {
            "source": "storage_budget_executor",
            "accepted": True,
            "sink": accepted_sink,
            "target_w": accepted_target_w,
            "signature": accepted_signature,
            "ts": now_value,
        }

    return {
        "contract_version": BUDGET_EXECUTOR_ACK_CONTRACT_VERSION,
        "shadow_only": True,
        "ack_class": ack_class,
        "ack_emitted": bool(ack_payload),
        "ack": ack_payload,
        "blockers": blockers,
        "accepted_sink": accepted_sink if accepted_active else "none",
        "accepted_target_w": accepted_target_w if accepted_active else 0,
        "accepted_signature": accepted_signature if accepted_active else "",
        "source": "storage_budget_executor" if ack_payload else "",
        "would_write_consumer_allocations": False,
        "would_send_rscp": False,
        "would_command_wallbox": False,
        "would_command_heatpump": False,
    }


def storage_budget_executor_ack_contract(
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
    latch: Optional[Dict[str, Any]] = None,
    previous: Optional[Dict[str, Any]] = None,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Validate the future executor acknowledgement for accepted budget output."""
    plan = plan if isinstance(plan, dict) else {}
    current_latch = latch if isinstance(latch, dict) else storage_budget_executor_latch_contract(payload, plan)
    previous_state = previous if isinstance(previous, dict) else {}
    now_value = float(now_s if now_s is not None else safe_float(payload.get("ts"), time.time()))
    ack = _budget_executor_ack_payload(payload)
    ack_present = bool(ack)
    ack_source = str(ack.get("source") or "").strip()
    ack_accepted = bool(ack.get("accepted"))
    ack_sink = str(ack.get("sink") or "none")
    ack_target_w = max(0, safe_int(ack.get("target_w"), 0))
    ack_signature = str(ack.get("signature") or "")
    ack_ts = safe_float(ack.get("ts"), 0.0)
    ack_age_s = max(0.0, now_value - ack_ts) if ack_ts > 0.0 else None
    accepted_active = bool(current_latch.get("accepted_active_shadow"))
    latch_sink = str(current_latch.get("accepted_sink") or "none")
    latch_target_w = max(0, safe_int(current_latch.get("accepted_target_w"), 0))
    latch_age_s = max(0.0, safe_float(current_latch.get("accepted_age_s"), 0.0))
    latch_signature = str(
        current_latch.get("accepted_signature")
        or current_latch.get("gate_signature")
        or ""
    )
    ack_timeout_s = max(5, safe_int(payload.get("budget_executor_ack_timeout_s"), 30))
    blockers: List[str] = []

    def add_blocker(reason: str) -> None:
        if reason and reason not in blockers:
            blockers.append(reason)

    expected_source = "storage_budget_executor"
    ack_required = bool(accepted_active and current_latch.get("requires_executor_ack", True))
    ack_source_allowed = _budget_executor_ack_source_allowed(ack_source)
    target_tolerance_w = max(250, int(round(max(1, latch_target_w) * 0.10)))
    target_matches = ack_target_w > 0 and abs(ack_target_w - latch_target_w) <= target_tolerance_w
    signature_matches = bool(ack_signature and latch_signature and ack_signature == latch_signature)
    sink_matches = ack_sink == latch_sink
    ack_stale = bool(ack_age_s is not None and ack_age_s > ack_timeout_s)
    elapsed_since_accept_s = latch_age_s
    previous_ack_confirmed = bool(previous_state.get("ack_valid_shadow"))

    ack_valid = False
    productive_allowed_shadow = False
    release_latch_shadow = False
    fallback_action = "observe"
    if not ack_required:
        ack_class = "ack_not_required"
        fallback_action = "observe"
    elif not ack_present:
        if elapsed_since_accept_s > ack_timeout_s:
            ack_class = "ack_missing_timeout"
            fallback_action = "safe_release_without_runtime"
            release_latch_shadow = True
            add_blocker("ack_missing")
        else:
            ack_class = "ack_pending"
            fallback_action = "wait_for_ack"
            add_blocker("ack_pending")
    elif not ack_source_allowed:
        ack_class = "ack_rejected_source"
        fallback_action = "safe_release_without_runtime"
        release_latch_shadow = True
        add_blocker("ack_source")
    elif not ack_accepted:
        ack_class = "ack_rejected_not_accepted"
        fallback_action = "safe_release_without_runtime"
        release_latch_shadow = True
        add_blocker("ack_not_accepted")
    elif ack_stale:
        ack_class = "ack_stale"
        fallback_action = "safe_release_without_runtime"
        release_latch_shadow = True
        add_blocker("ack_stale")
    elif not sink_matches:
        ack_class = "ack_rejected_mismatch"
        fallback_action = "safe_release_without_runtime"
        release_latch_shadow = True
        add_blocker("ack_sink_mismatch")
    elif not signature_matches or not target_matches:
        ack_class = "ack_rejected_mismatch"
        fallback_action = "safe_release_without_runtime"
        release_latch_shadow = True
        if not signature_matches:
            add_blocker("ack_signature_mismatch")
        if not target_matches:
            add_blocker("ack_target_mismatch")
    else:
        ack_class = "ack_confirmed_shadow"
        ack_valid = True
        productive_allowed_shadow = True
        fallback_action = "none"

    next_state = {
        "contract_version": BUDGET_EXECUTOR_ACK_CONTRACT_VERSION,
        "ack_valid_shadow": bool(ack_valid),
        "ack_class": ack_class,
        "ack_source": ack_source if ack_present else "",
        "ack_sink": ack_sink if ack_present else "none",
        "ack_target_w": ack_target_w if ack_present else 0,
        "ack_ts": ack_ts if ack_present else 0.0,
        "ack_signature": ack_signature if ack_present else "",
        "productive_allowed_shadow": bool(productive_allowed_shadow),
        "release_latch_shadow": bool(release_latch_shadow),
        "ts": now_value,
    }
    return {
        "contract_version": BUDGET_EXECUTOR_ACK_CONTRACT_VERSION,
        "shadow_only": True,
        "ack_required_shadow": ack_required,
        "ack_class": ack_class,
        "ack_valid_shadow": bool(ack_valid),
        "ack_present": ack_present,
        "ack_source": ack_source if ack_present else "",
        "expected_ack_source": expected_source,
        "ack_source_allowed": bool(ack_source_allowed) if ack_present else False,
        "ack_sink": ack_sink if ack_present else "none",
        "ack_target_w": ack_target_w if ack_present else 0,
        "ack_age_s": round(ack_age_s, 1) if ack_age_s is not None else None,
        "ack_timeout_s": ack_timeout_s,
        "latch_active_shadow": accepted_active,
        "latch_sink": latch_sink,
        "latch_target_w": latch_target_w,
        "latch_age_s": round(latch_age_s, 1),
        "sink_matches": bool(sink_matches) if ack_present else False,
        "target_matches": bool(target_matches) if ack_present else False,
        "signature_matches": bool(signature_matches) if ack_present else False,
        "previous_ack_valid_shadow": previous_ack_confirmed,
        "productive_allowed_shadow": bool(productive_allowed_shadow),
        "release_latch_shadow": bool(release_latch_shadow),
        "fallback_action": fallback_action,
        "blockers": blockers,
        "would_write_consumer_allocations": False,
        "would_send_rscp": False,
        "would_command_wallbox": False,
        "would_command_heatpump": False,
        "next_state": next_state,
    }


def ems_budget_runtime_contract(
    cfg: Dict[str, Any],
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
    readiness: Optional[Dict[str, Any]] = None,
    latch: Optional[Dict[str, Any]] = None,
    ack: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Central budget runtime switch with local vetoes, still side-effect-free."""
    enabled = cfg_bool(cfg, "ems_budget_runtime_enable", False)
    plan = plan if isinstance(plan, dict) else {}
    readiness_contract = readiness if isinstance(readiness, dict) else storage_budget_readiness_contract(payload, plan)
    latch_contract = latch if isinstance(latch, dict) else storage_budget_executor_latch_contract(payload, plan)
    ack_contract = ack if isinstance(ack, dict) else storage_budget_executor_ack_contract(payload, plan, latch_contract)
    accepted_sink = str(latch_contract.get("accepted_sink") or "none")
    accepted_target_w = max(0, safe_int(latch_contract.get("accepted_target_w"), 0))
    ack_valid = bool(ack_contract.get("productive_allowed_shadow") and ack_contract.get("ack_valid_shadow"))
    ack_required = bool(ack_contract.get("ack_required_shadow"))
    controllable_sink = accepted_sink in ("wallbox", "heatpump") and accepted_target_w > 0
    blockers: List[str] = []

    def add_blocker(reason: str) -> None:
        if reason and reason not in blockers:
            blockers.append(reason)

    if not enabled:
        add_blocker("runtime_disabled")
    if bool(payload.get("live_stale")) or safe_float(payload.get("live_age_s"), 0.0) > 10.0:
        add_blocker("live_stale")
    if not bool(payload.get("live_sample_valid", True)):
        add_blocker("live_sample_invalid")
    if not bool(payload.get("grid_power_valid", True)):
        add_blocker("grid_power_invalid")
    if not bool(payload.get("home_power_valid", True)):
        add_blocker("home_power_invalid")
    if not bool(readiness_contract.get("data_valid", True)):
        add_blocker("budget_data_invalid")
    if str(readiness_contract.get("readiness_class") or "") == "import_guard":
        add_blocker("import_guard")
    if bool(payload.get("force_wallbox_stop")):
        add_blocker("manual_user_off")
    if bool(ack_contract.get("release_latch_shadow")):
        add_blocker("executor_ack_release")
    if enabled and ack_required and not ack_valid:
        add_blocker("executor_ack_missing_or_invalid")

    active = bool(enabled and ack_valid and controllable_sink and not blockers)
    wallbox_budget_w = accepted_target_w if active and accepted_sink == "wallbox" else 0
    heatpump_budget_w = accepted_target_w if active and accepted_sink == "heatpump" else 0
    safe_fallback = bool(enabled and blockers)
    runtime_class = "runtime_disabled"
    if enabled:
        if active:
            runtime_class = "runtime_active_%s" % accepted_sink
        elif safe_fallback:
            runtime_class = "runtime_safe_fallback"
        else:
            runtime_class = "runtime_observe"
    return {
        "contract_version": EMS_BUDGET_RUNTIME_CONTRACT_VERSION,
        "enabled": bool(enabled),
        "active": active,
        "runtime_class": runtime_class,
        "accepted_sink": accepted_sink,
        "accepted_target_w": accepted_target_w,
        "wallbox_budget_w": wallbox_budget_w,
        "heatpump_budget_w": heatpump_budget_w,
        "rscp_budget_w": 0,
        "safe_fallback": safe_fallback,
        "blockers": blockers,
        "ack_valid": ack_valid,
        "ack_class": ack_contract.get("ack_class"),
        "latch_class": latch_contract.get("latch_class"),
        "would_write_consumer_allocations": bool(active),
        "would_send_rscp": False,
        "would_command_wallbox": bool(active and accepted_sink == "wallbox"),
        "would_command_heatpump": bool(active and accepted_sink == "heatpump"),
    }


def storage_budget_runtime_contract_suite(
    cfg: Dict[str, Any],
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
    budget_stability_previous: Optional[Dict[str, Any]] = None,
    budget_executor_latch_previous: Optional[Dict[str, Any]] = None,
    budget_executor_ack_previous: Optional[Dict[str, Any]] = None,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Bewertet den zentralen Budgetpfad genau einmal für Runtime und Diagnose."""
    plan = plan if isinstance(plan, dict) else {}
    now_value = float(now_s if now_s is not None else safe_float(payload.get("ts"), time.time()))
    budget_contract = storage_budget_readiness_contract(payload, plan)
    budget_arbitration_contract = storage_budget_arbitration_contract(payload, plan, budget_contract)
    budget_stability_contract = storage_budget_stability_contract(
        payload,
        plan,
        budget_arbitration_contract,
        budget_stability_previous,
        now_s=now_value,
    )
    budget_executor_gate_contract = storage_budget_executor_gate_contract(
        payload,
        plan,
        budget_arbitration_contract,
        budget_stability_contract,
    )
    budget_executor_latch_contract = storage_budget_executor_latch_contract(
        payload,
        plan,
        budget_executor_gate_contract,
        budget_executor_latch_previous,
        now_s=now_value,
    )
    central_ack_contract = storage_budget_executor_central_ack_contract(
        budget_executor_latch_contract,
        now_s=now_value,
    )
    payload_for_ack = payload
    runtime_enabled = cfg_bool(cfg, "ems_budget_runtime_enable", False)
    if not runtime_enabled and central_ack_contract.get("ack_emitted"):
        central_ack_contract = {
            **central_ack_contract,
            "ack_class": "central_ack_runtime_disabled",
            "ack_emitted": False,
            "ack": {},
            "source": "",
            "blockers": ["runtime_disabled"],
        }
    if runtime_enabled and central_ack_contract.get("ack_emitted"):
        payload_for_ack = {**payload, "budget_executor_ack": dict(central_ack_contract.get("ack") or {})}
    budget_executor_ack_contract = storage_budget_executor_ack_contract(
        payload_for_ack,
        plan,
        budget_executor_latch_contract,
        budget_executor_ack_previous,
        now_s=now_value,
    )
    ems_budget_runtime = ems_budget_runtime_contract(
        cfg,
        payload_for_ack,
        plan,
        budget_contract,
        budget_executor_latch_contract,
        budget_executor_ack_contract,
    )
    return {
        "readiness": budget_contract,
        "arbitration": budget_arbitration_contract,
        "stability": budget_stability_contract,
        "executor_gate": budget_executor_gate_contract,
        "executor_latch": budget_executor_latch_contract,
        "central_ack": central_ack_contract,
        "executor_ack": budget_executor_ack_contract,
        "runtime": ems_budget_runtime,
        "budget_executor_ack_payload": dict(central_ack_contract.get("ack") or {}),
    }


def _direct_marketing_policy_owns_storage_cycle(contract: Dict[str, Any]) -> bool:
    """Akzeptiert nur eine vollständig ausgewählte, gerichtete DV-Policy als Zyklus-Owner."""
    gate = contract.get("policy_executor_gate") if isinstance(contract.get("policy_executor_gate"), dict) else {}
    target_state = str(contract.get("policy_target_state") or "").strip().upper()
    charge_budget_w = max(0, safe_int(contract.get("policy_charge_budget_w"), 0))
    export_budget_w = max(0, safe_int(contract.get("policy_export_budget_w"), 0))
    common = bool(
        contract.get("active")
        and contract.get("policy_selected") is True
        and contract.get("commands_allowed") is True
        and contract.get("shadow") is False
        and contract.get("policy_schema") == DIRECT_MARKETING_POLICY_SCHEMA
        and contract.get("policy_blocked") is False
        and gate.get("allowed") is True
    )
    if not common:
        return False
    if target_state == "FORCE_CHARGE_PV":
        return bool(charge_budget_w > 0 and export_budget_w == 0)
    if target_state == "FORCE_EXPORT":
        return bool(export_budget_w > 0 and charge_budget_w == 0)
    return False


def _phase5_decision_only_hold_path_projection(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Bindet einen wirkungslosen HOLD exakt an den Legacy-Hardwareowner."""

    phase5 = (
        payload.get("storage_dispatch_phase5")
        if isinstance(payload.get("storage_dispatch_phase5"), dict)
        else {}
    )
    intent = phase5.get("execution_intent") if isinstance(phase5.get("execution_intent"), dict) else {}
    decision_hold = (
        phase5.get("decision_only_hold")
        if isinstance(phase5.get("decision_only_hold"), dict)
        else {}
    )
    translation = phase5.get("translation") if isinstance(phase5.get("translation"), dict) else {}
    claimed = bool(
        str(phase5.get("selection_class") or "") == "decision_only_hold"
        or str(intent.get("class") or "") == "decision_only_hold"
        or decision_hold.get("active") is True
    )
    if not claimed:
        return {
            "schema_version": "phase5_decision_only_hold_path_projection_v1",
            "claimed": False,
            "valid": False,
            "blockers": [],
        }

    blockers: List[str] = []

    def require(condition: bool, code: str) -> None:
        if not condition and code not in blockers:
            blockers.append(code)

    def is_exact_zero(value: Any) -> bool:
        return bool(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and float(value) == 0.0
        )

    def is_sha256_id(value: Any) -> bool:
        text = str(value or "")
        suffix = text[7:] if text.startswith("sha256:") else ""
        return bool(len(suffix) == 64 and all(char in "0123456789abcdef" for char in suffix))

    require(phase5.get("schema_version") == "storage_dispatch_phase5_v1", "PHASE5_HOLD_SCHEMA_INVALID")
    require(phase5.get("selected") is True, "PHASE5_HOLD_NOT_SELECTED")
    require(phase5.get("executable") is False, "PHASE5_HOLD_EXECUTABLE_CLAIM")
    require(phase5.get("commands_allowed") is False, "PHASE5_HOLD_COMMANDS_ALLOWED_CLAIM")
    require(phase5.get("selection_class") == "decision_only_hold", "PHASE5_HOLD_SELECTION_CLASS_INVALID")
    require(decision_hold.get("active") is True, "PHASE5_HOLD_DECISION_CONTRACT_INACTIVE")
    require(
        phase5.get("selected_source") == "canonical_phase5_decision_only_hold",
        "PHASE5_HOLD_SOURCE_INVALID",
    )
    require(str(phase5.get("selected_action") or "").upper() == "HOLD", "PHASE5_HOLD_ACTION_INVALID")
    require(
        "selected_power_w" in phase5 and is_exact_zero(phase5.get("selected_power_w")),
        "PHASE5_HOLD_POWER_NONZERO_OR_MISSING",
    )
    require(phase5.get("requested") is False, "PHASE5_HOLD_REQUESTED_CLAIM")
    require(phase5.get("issued") is False, "PHASE5_HOLD_ISSUED_CLAIM")
    require(phase5.get("hardware_effect") is False, "PHASE5_HOLD_EFFECT_CLAIM")
    require(
        "request" in phase5 and phase5.get("request") is None,
        "PHASE5_HOLD_REQUEST_PAYLOAD_PRESENT_OR_UNTYPED",
    )
    require(
        "request_attempted_by" in phase5 and phase5.get("request_attempted_by") is None,
        "PHASE5_HOLD_REQUEST_ATTEMPT_PRESENT_OR_UNTYPED",
    )
    require(
        "power_settings_after_request" in phase5 and phase5.get("power_settings_after_request") is None,
        "PHASE5_HOLD_POWER_SETTINGS_TRANSACTION_PRESENT_OR_UNTYPED",
    )
    require(intent.get("class") == "decision_only_hold", "PHASE5_HOLD_INTENT_CLASS_INVALID")
    require(intent.get("authorized") is False, "PHASE5_HOLD_INTENT_AUTHORIZED")
    require(str(intent.get("action") or "").upper() == "HOLD", "PHASE5_HOLD_INTENT_ACTION_INVALID")
    require(
        "power_w" in intent and is_exact_zero(intent.get("power_w")),
        "PHASE5_HOLD_INTENT_POWER_NONZERO_OR_MISSING",
    )
    require(intent.get("owner") == "legacy_storage_manager", "PHASE5_HOLD_INTENT_OWNER_INVALID")
    require(str(translation.get("action") or "").upper() == "HOLD", "PHASE5_HOLD_TRANSLATION_ACTION_INVALID")
    require(translation.get("translated") is False, "PHASE5_HOLD_TRANSLATION_EFFECT_CLAIM")
    require(
        "requested_power_w" in translation
        and is_exact_zero(translation.get("requested_power_w")),
        "PHASE5_HOLD_TRANSLATION_POWER_NONZERO_OR_MISSING",
    )
    require(is_sha256_id(phase5.get("plan_id")), "PHASE5_HOLD_PLAN_ID_INVALID")
    require(is_sha256_id(phase5.get("slot_id")), "PHASE5_HOLD_SLOT_ID_INVALID")
    phase5_ts_ms = safe_int(phase5.get("ts_ms"), 0)
    payload_ts_ms = int(round(safe_float(payload.get("ts"), 0.0) * 1000.0))
    require(
        phase5_ts_ms > 0
        and payload_ts_ms > 0
        and abs(phase5_ts_ms - payload_ts_ms) <= 2000,
        "PHASE5_HOLD_CYCLE_BINDING_INVALID",
    )
    return {
        "schema_version": "phase5_decision_only_hold_path_projection_v1",
        "claimed": True,
        "valid": not blockers,
        "blockers": blockers,
        "intent_owner": intent.get("owner"),
        "plan_id": phase5.get("plan_id"),
        "slot_id": phase5.get("slot_id"),
    }


def _direct_marketing_effectless_no_action_path_projection(
    payload: Dict[str, Any],
    direct_marketing_contract: Dict[str, Any],
    curve_contract: Dict[str, Any],
    auto_contract: Dict[str, Any],
    protection_contract: Dict[str, Any],
) -> Dict[str, Any]:
    """Trennt einen passiven DV-Monitor vom tatsächlichen Kurvenowner."""

    phase5 = (
        payload.get("storage_dispatch_phase5")
        if isinstance(payload.get("storage_dispatch_phase5"), dict)
        else {}
    )
    monitor = (
        payload.get("direct_marketing_monitor")
        if isinstance(payload.get("direct_marketing_monitor"), dict)
        else {}
    )
    state_l = str(payload.get("state") or "").strip().lower()
    action_l = str(
        payload.get("direct_marketing_action")
        or monitor.get("current_action")
        or monitor.get("action")
        or ""
    ).strip().lower()
    monitor_state_l = str(monitor.get("state") or "").strip().lower()
    passive_actions = {"", "hold", "policy_hold", "normal", "auto", "wait", "waiting"}
    passive_monitor_states = {"hold", "waiting", "idle", "observe", "shadow"}
    claimed = bool(
        direct_marketing_contract.get("active")
        and state_l.startswith("parallel_curve")
        and action_l in passive_actions
        and monitor_state_l in passive_monitor_states
        and phase5.get("selected") is not True
    )
    if not claimed:
        return {
            "schema_version": "direct_marketing_effectless_no_action_path_projection_v1",
            "claimed": False,
            "valid": False,
            "blockers": [],
        }

    blockers: List[str] = []

    def require(condition: bool, code: str) -> None:
        if not condition and code not in blockers:
            blockers.append(code)

    def is_exact_zero(value: Any) -> bool:
        return bool(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and float(value) == 0.0
        )

    require(
        not _direct_marketing_policy_owns_storage_cycle(direct_marketing_contract),
        "DIRECT_MARKETING_POLICY_OWNS_CYCLE",
    )
    require(
        direct_marketing_contract.get("policy_selected") is False,
        "DIRECT_MARKETING_POLICY_SELECTION_UNTYPED",
    )
    require(
        max(0, safe_int(direct_marketing_contract.get("policy_charge_budget_w"), 0)) == 0
        and max(0, safe_int(direct_marketing_contract.get("policy_export_budget_w"), 0)) == 0,
        "DIRECT_MARKETING_DIRECTIONAL_BUDGET_PRESENT",
    )
    require(
        safe_int(payload.get("mode"), MODE_AUTO) == MODE_AUTO
        and is_exact_zero(payload.get("val")),
        "DIRECT_MARKETING_EFFECTLESS_MODE_OR_POWER_INVALID",
    )
    require(
        protection_contract.get("active") is False,
        "DIRECT_MARKETING_EFFECTLESS_PROTECTION_ACTIVE",
    )
    require(
        curve_contract.get("in_curve_state") is True
        and curve_contract.get("action_class") in {
            "auto_limit_hold_zero_charge",
            "auto_limit_release",
        },
        "DIRECT_MARKETING_EFFECTLESS_CURVE_CONTRACT_INVALID",
    )
    require(
        auto_contract.get("active") is True
        and auto_contract.get("source_class") == "curve"
        and auto_contract.get("command_class") in {"limit", "release"},
        "DIRECT_MARKETING_EFFECTLESS_AUTO_CONTRACT_INVALID",
    )
    if phase5:
        require(
            phase5.get("schema_version") == "storage_dispatch_phase5_v1",
            "DIRECT_MARKETING_EFFECTLESS_PHASE5_SCHEMA_INVALID",
        )
        for key in (
            "selected",
            "executable",
            "commands_allowed",
            "requested",
            "issued",
            "hardware_effect",
        ):
            require(
                phase5.get(key) is False,
                "DIRECT_MARKETING_EFFECTLESS_PHASE5_%s_CLAIM" % key.upper(),
            )
        require(
            phase5.get("selected_action") in (None, "HOLD")
            and is_exact_zero(phase5.get("selected_power_w")),
            "DIRECT_MARKETING_EFFECTLESS_PHASE5_ACTION_OR_POWER_INVALID",
        )
        for key in ("request", "request_attempted_by", "power_settings_after_request"):
            require(
                key in phase5 and phase5.get(key) is None,
                "DIRECT_MARKETING_EFFECTLESS_PHASE5_%s_PRESENT_OR_UNTYPED" % key.upper(),
            )
    return {
        "schema_version": "direct_marketing_effectless_no_action_path_projection_v1",
        "claimed": True,
        "valid": not blockers,
        "blockers": blockers,
        "state": payload.get("state"),
        "action": action_l or None,
        "monitor_state": monitor_state_l or None,
        "plan_id": phase5.get("plan_id") if phase5 else None,
        "slot_id": phase5.get("slot_id") if phase5 else None,
    }


def _curve_release_is_direct_marketing_subcontract(
    payload: Dict[str, Any],
    direct_marketing_contract: Dict[str, Any],
    curve_contract: Dict[str, Any],
    auto_contract: Dict[str, Any],
    protection_contract: Dict[str, Any],
) -> bool:
    """Klassifiziert nur eine befehlslose AUTO-Freigabe unter einer ausgewählten DV-Policy."""
    return bool(
        _direct_marketing_policy_owns_storage_cycle(direct_marketing_contract)
        and protection_contract.get("active") is False
        and curve_contract.get("in_curve_state") is True
        and curve_contract.get("action_class") == "auto_limit_release"
        and safe_int(payload.get("mode"), MODE_AUTO) == MODE_AUTO
        and max(0, safe_int(payload.get("val"), 0)) == 0
        and auto_contract.get("active") is True
        and auto_contract.get("release") is True
        and auto_contract.get("enabled") is False
        and auto_contract.get("command_class") == "release"
        and auto_contract.get("source_class") == "curve"
    )


def storage_decision_path_contract(
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Select one diagnostic storage path and expose conflicts as veto reasons."""
    plan = plan if isinstance(plan, dict) else {}
    state_name = str(payload.get("state") or "")
    state_l = state_name.lower()
    reason_text = str(payload.get("display_reason") or payload.get("reason") or payload.get("priority") or "")
    reason_l = reason_text.lower()
    mode_value = safe_int(payload.get("mode"), MODE_AUTO)
    owner_contract = storage_owner_contract(payload)
    curve_contract = storage_curve_context_contract(payload, plan)
    auto_limit = payload.get("auto_limit") if isinstance(payload.get("auto_limit"), dict) else {}
    auto_contract = storage_auto_limit_contract(
        auto_limit,
        state=state_name,
        mode=mode_value,
        reason=reason_text,
        direct_marketing_active=bool(payload.get("direct_marketing_active")),
        direct_marketing_auto_limit_active=bool(payload.get("direct_marketing_pv_store_auto_limit_active")),
    )
    market_contract = storage_market_path_contract(payload, plan)
    direct_marketing_contract = storage_direct_marketing_path_contract(payload, plan)
    predump_contract = storage_predump_path_contract(payload, plan)
    protection_contract = storage_protection_path_contract(payload, plan)
    phase5_hold_projection = _phase5_decision_only_hold_path_projection(payload)
    effectless_phase5_hold = phase5_hold_projection.get("valid") is True
    effectless_no_action_projection = _direct_marketing_effectless_no_action_path_projection(
        payload,
        direct_marketing_contract,
        curve_contract,
        auto_contract,
        protection_contract,
    )
    effectless_direct_marketing_no_action = effectless_no_action_projection.get("valid") is True
    direct_marketing_policy_owns_cycle = bool(
        not effectless_phase5_hold
        and _direct_marketing_policy_owns_storage_cycle(direct_marketing_contract)
    )
    curve_is_direct_marketing_release_subcontract = bool(
        not effectless_phase5_hold
        and _curve_release_is_direct_marketing_subcontract(
            payload,
            direct_marketing_contract,
            curve_contract,
            auto_contract,
            protection_contract,
        )
    )
    predump_is_protection_subcontract = bool(
        state_l == "wallbox_predump_floor_hold"
        and mode_value == MODE_AUTO
        and protection_contract.get("active")
        and predump_contract.get("active")
    )
    candidates: List[str] = []
    candidate_reasons: Dict[str, str] = {}

    def add_candidate(name: str, why: str) -> None:
        if name not in candidates:
            candidates.append(name)
            candidate_reasons[name] = why

    if bool(protection_contract.get("active")):
        add_candidate("protection", "subcontract")
    if state_l.startswith("manual_override"):
        add_candidate("manual", "state")
    if (
        bool(direct_marketing_contract.get("active"))
        and not effectless_phase5_hold
        and not effectless_direct_marketing_no_action
    ):
        add_candidate("direct_marketing", "subcontract")
    if bool(market_contract.get("active")):
        add_candidate("market_price", "subcontract")
    if bool(predump_contract.get("active")) and not predump_is_protection_subcontract:
        add_candidate("predump", "subcontract")
    if state_l.startswith("parallel_wb") or bool(payload.get("wbminsoc_pv_charge_active")) or "wbminsoc" in reason_l:
        add_candidate("wallbox_support", "state_or_flag")
    curve_is_headroom_protection_subcontract = bool(
        state_l == "parallel_curve_charge_cap"
        and protection_contract.get("active") is True
        and protection_contract.get("protection_class") == "headroom"
    )
    if (
        state_l.startswith("parallel_curve")
        or str(payload.get("priority") or "").lower() == "curve"
    ) and not curve_is_headroom_protection_subcontract and not curve_is_direct_marketing_release_subcontract:
        add_candidate("curve", "state_or_priority")
    if not candidates and mode_value != MODE_AUTO:
        add_candidate("storage_active", "active_rscp_mode")
    if not candidates:
        add_candidate("e3dc_auto", "fallback")

    priority = [
        "protection",
        "manual",
        "direct_marketing",
        "market_price",
        "predump",
        "wallbox_support",
        "curve",
        "storage_active",
        "e3dc_auto",
    ]
    primary_path = next((name for name in priority if name in candidates), candidates[0])
    expected_auto_source = {
        "direct_marketing": "direct_marketing",
        "market_price": "market",
        "predump": "predump",
        "wallbox_support": "wallbox",
        "curve": "curve",
        "protection": "protection",
    }.get(primary_path)
    auto_source = str(auto_contract.get("source_class") or "none")
    subordinate_paths: List[str] = []
    veto_reasons: List[str] = []
    if effectless_phase5_hold and bool(direct_marketing_contract.get("active")):
        subordinate_paths.append("direct_marketing:phase5_decision_only_hold_diagnostic")
    if effectless_direct_marketing_no_action and bool(direct_marketing_contract.get("active")):
        subordinate_paths.append("direct_marketing:effectless_no_action_diagnostic")
    if phase5_hold_projection.get("claimed") is True and not effectless_phase5_hold:
        veto_reasons.append("phase5_decision_only_hold_projection_invalid")
    if (
        effectless_no_action_projection.get("claimed") is True
        and not effectless_direct_marketing_no_action
    ):
        veto_reasons.append("direct_marketing_effectless_no_action_projection_invalid")
    if predump_is_protection_subcontract:
        subordinate_paths.append("predump:wallbox_floor_protection_subcontract")
    if curve_is_headroom_protection_subcontract:
        subordinate_paths.append("curve:headroom_protection_subcontract")
    if curve_is_direct_marketing_release_subcontract:
        subordinate_paths.append("curve:direct_marketing_auto_release_subcontract")
    if bool(auto_contract.get("active")):
        subordinate_paths.append("auto_limit:%s:%s" % (auto_source, auto_contract.get("command_class") or "unknown"))
        aligned_sources = {expected_auto_source, "auto_limit", "guard", "none"}
        if predump_is_protection_subcontract:
            aligned_sources.add("predump")
        if curve_is_direct_marketing_release_subcontract:
            aligned_sources.add("curve")
        aligned_sources.discard(None)
        if expected_auto_source and auto_source not in aligned_sources:
            veto_reasons.append("auto_limit_source_mismatch:%s->%s" % (primary_path, auto_source))
    if bool(curve_contract.get("has_curve")) and primary_path != "curve":
        subordinate_paths.append("curve_context:%s" % (curve_contract.get("curve_position") or "unknown"))
    protection_subordinate_paths = {"wallbox_support", "curve"}
    if primary_path == "protection":
        if (
            "direct_marketing" in candidates
            and (
                direct_marketing_contract.get("shadow") is True
                or direct_marketing_contract.get("commands_allowed") is False
            )
        ):
            protection_subordinate_paths.add("direct_marketing")
        if (
            "market_price" in candidates
            and (
                market_contract.get("shadow") is True
                or market_contract.get("commands_allowed") is False
            )
        ):
            protection_subordinate_paths.add("market_price")
        for path in candidates:
            if path in protection_subordinate_paths:
                subordinate_paths.append("%s:protection_override" % path)
    hard_candidates = [
        path for path in candidates
        if path not in ("e3dc_auto", "storage_active")
        and path != primary_path
        and not (primary_path == "protection" and path in protection_subordinate_paths)
    ]
    if hard_candidates:
        veto_reasons.append("multiple_policy_candidates:%s" % ",".join(hard_candidates))
    if effectless_phase5_hold:
        incompatible_hold_paths = [
            path
            for path in candidates
            if path in {"protection", "manual", "market_price", "predump", "wallbox_support", "storage_active"}
        ]
        if incompatible_hold_paths:
            veto_reasons.append(
                "phase5_decision_only_hold_legacy_path_conflict:%s"
                % ",".join(incompatible_hold_paths)
            )
    effective_owner_control = owner_contract.get("control_owner")
    effective_owner_contract = owner_contract.get("contract_owner")
    effective_owner_binding = "legacy_state_contract"
    if effectless_phase5_hold:
        effective_owner_binding = "phase5_decision_only_hold_legacy_state_contract"
        if primary_path == "e3dc_auto":
            # Ein reiner DV-Zustands-/Monitor-Marker ist in diesem exakt
            # wirkungslosen HOLD kein Hardwareowner. Ohne einen anderen
            # typisierten Legacy-Unterpfad bleibt E3DC-AUTO führend.
            effective_owner_control = "e3dc_auto"
            effective_owner_contract = "E3DC_AUTONOM"
    if effectless_direct_marketing_no_action:
        effective_owner_binding = "direct_marketing_effectless_no_action_legacy_state_contract"
    if primary_path == "direct_marketing" and direct_marketing_policy_owns_cycle:
        effective_owner_control = "direct_marketing"
        effective_owner_contract = "MARKET_DIRECT"
        effective_owner_binding = "selected_direct_marketing_policy"
    if primary_path in ("direct_marketing", "market_price", "predump") and str(effective_owner_contract) == "E3DC_AUTONOM":
        veto_reasons.append("owner_contract_not_market_owned")
    if primary_path == "curve" and str(curve_contract.get("action_class")) == "active_discharge":
        veto_reasons.append("curve_path_active_discharge")
    subcontracts = {
        "market_price": market_contract,
        "direct_marketing": direct_marketing_contract,
        "predump": predump_contract,
        "protection": protection_contract,
    }
    for path_name, contract in subcontracts.items():
        if (
            (effectless_phase5_hold or effectless_direct_marketing_no_action)
            and path_name == "direct_marketing"
        ):
            continue
        if primary_path == "protection" and path_name in protection_subordinate_paths:
            continue
        for reason in contract.get("veto_reasons") if isinstance(contract.get("veto_reasons"), list) else []:
            veto_reasons.append("%s:%s" % (path_name, reason))
    return {
        "contract_version": STORAGE_DECISION_PATH_CONTRACT_VERSION,
        "primary_path": primary_path,
        "active_paths": candidates,
        "subordinate_paths": subordinate_paths,
        "subcontracts": subcontracts,
        "phase5_decision_only_hold_projection": phase5_hold_projection,
        "direct_marketing_effectless_no_action_projection": effectless_no_action_projection,
        "candidate_reasons": candidate_reasons,
        "path_conflict": bool(veto_reasons),
        "veto_required": bool(veto_reasons),
        "veto_reasons": veto_reasons,
        "owner_control": effective_owner_control,
        "owner_contract": effective_owner_contract,
        "owner_binding": effective_owner_binding,
        "raw_owner_control": owner_contract.get("control_owner"),
        "raw_owner_contract": owner_contract.get("contract_owner"),
        "execution_class": owner_contract.get("execution_class"),
        "auto_limit_source_class": auto_source,
        "auto_limit_command_class": auto_contract.get("command_class"),
        "auto_limit_aligned": not any(reason.startswith("auto_limit_source_mismatch") for reason in veto_reasons),
        "curve_position": curve_contract.get("curve_position"),
        "curve_action_class": curve_contract.get("action_class"),
        "state": state_name,
        "mode": mode_value,
        "mode_name": mode_label(mode_value),
    }


def _configured_power_limit_w(cfg: Dict[str, Any], key: str) -> int:
    if key not in cfg:
        return 0
    value = safe_int(cfg.get(key), 0)
    return value if value >= 300 else 0


def _rscp_power_limit_w(live: Dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = abs(safe_int(live.get(key), 0))
        if value >= 300:
            return value
    return 0


def _fresh_rscp_charge_limits_w(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    *,
    now_s: Optional[float] = None,
) -> List[int]:
    """Liefert ausschließlich frische, typisierte RSCP-Ladegrenzen.

    ``EMS_USER_CHARGE_LIMIT`` und ``EMS_BAT_CHARGE_LIMIT`` sind Obergrenzen.
    Sie dürfen einen konfigurierten Sollwert deshalb nur absenken, niemals
    anheben. Temporäre ``EMS_POWER_SETTINGS``-Readbacks werden hier bewusst
    nicht verwendet, damit eine vorherige dynamische Begrenzung nicht als
    neue Hardwarefähigkeit festgeschrieben wird.
    """

    if (
        live.get("ems_power_settings_read") is not True
        or live.get("ems_power_settings_valid") is not True
        or live.get("RSCP_Sample_Valid") is not True
        or live.get("Power_Decision_Usable") is not True
    ):
        return []
    raw_ts = live.get("_ts")
    if (
        not isinstance(raw_ts, (int, float))
        or isinstance(raw_ts, bool)
        or not math.isfinite(float(raw_ts))
        or float(raw_ts) <= 0.0
    ):
        return []
    now_value = time.time() if now_s is None else float(now_s)
    max_age_s = max(
        1.0,
        min(30.0, safe_float(cfg.get("storage_live_stale_guard_s"), 10.0)),
    )
    age_s = now_value - float(raw_ts)
    if not math.isfinite(age_s) or age_s < -5.0 or age_s > max_age_s:
        return []

    limits: List[int] = []
    for key in ("user_charge_limit_w", "bat_charge_limit_w"):
        value = live.get(key)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 300
        ):
            limits.append(int(value))
    return limits


def configured_charge_limit_w(
    cfg: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    *,
    now_s: Optional[float] = None,
) -> int:
    live = live or {}
    configured_limit_w = _configured_power_limit_w(cfg, "maximumladeleistung")
    fresh_rscp_limits_w = _fresh_rscp_charge_limits_w(
        cfg,
        live,
        now_s=now_s,
    )
    if fresh_rscp_limits_w:
        hardware_limit_w = min(fresh_rscp_limits_w)
        return min(configured_limit_w, hardware_limit_w) if configured_limit_w else hardware_limit_w
    return configured_limit_w or 12000


def configured_discharge_limit_w(
    cfg: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    fallback_charge_w: Optional[int] = None,
) -> int:
    live = live or {}
    user_limit_w = _configured_power_limit_w(cfg, "maximaleentladeleistung")
    if user_limit_w:
        return user_limit_w
    rscp_limit_w = _rscp_power_limit_w(
        live,
        "user_discharge_limit_w",
        "bat_discharge_limit_w",
    )
    if rscp_limit_w:
        return rscp_limit_w
    return max(300, int(fallback_charge_w or configured_charge_limit_w(cfg, live)))


def configured_kw_or_w(value: Any) -> int:
    raw = safe_float(value, 0.0)
    if raw <= 0.0:
        return 0
    if raw < 100.0:
        return int(round(raw * 1000.0))
    return int(round(raw))


def configured_export_target_w(cfg: Dict[str, Any], live: Dict[str, Any]) -> int:
    configured_w = configured_kw_or_w(cfg.get("einspeiselimit", 0))
    live_derate_w = safe_int(live.get("derate_at_power_w"), 0)
    buffer_w = max(0, safe_int(cfg.get("abregel_puffer_w"), 300))
    if configured_w > 0 and live_derate_w > 0:
        hard_limit_w = min(configured_w, live_derate_w)
        return max(0, hard_limit_w - buffer_w)
    if configured_w > 0:
        return max(0, configured_w - buffer_w)
    if live_derate_w > 0:
        return max(0, live_derate_w - buffer_w)
    return 0




def direct_marketing_local_deficit_w(live: Dict[str, Any]) -> int:
    """PV-uncovered local load that must be covered before market export."""
    wallbox_w = abs(safe_float(live.get("Wallbox_Power"), 0.0))
    home_w = max(0, int(round(house_power_excluding_wallbox_w(live, wallbox_w))))
    wp_w = max(
        0,
        safe_int(live.get("WP_Power", live.get("Heatpump_Power")), 0),
    )
    heater_w = max(0, safe_int(live.get("heizstab_power", live.get("Heizstab_Power")), 0))
    pv_w = max(0, safe_int(live.get("PV_Power"), 0))
    return max(0, home_w + wp_w + heater_w - pv_w)


def direct_marketing_export_import_guard(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    planned_discharge_w: int,
) -> Dict[str, int]:
    grid_w = safe_int(live.get("Grid_Power"), 0)
    grid_ema_w = safe_int(live.get("Grid_EMA_W", grid_w), grid_w)
    grid_import_w = max(0, grid_w, grid_ema_w)
    import_guard_w = max(
        0,
        safe_int(cfg.get("direct_marketing_import_guard_w"), 30),
    )
    deficit_margin_w = max(0, safe_int(cfg.get("direct_marketing_local_load_guard_w"), 0))
    local_deficit_w = direct_marketing_local_deficit_w(live)
    planned_w = max(0, int(planned_discharge_w or 0))
    blocked = bool(grid_import_w > import_guard_w and planned_w + deficit_margin_w < local_deficit_w)
    return {
        "blocked": 1 if blocked else 0,
        "grid_import_w": grid_import_w,
        "import_guard_w": import_guard_w,
        "local_deficit_w": local_deficit_w,
        "deficit_margin_w": deficit_margin_w,
        "planned_discharge_w": planned_w,
    }


def direct_marketing_export_control_w(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    previous_state: Dict[str, Any],
    base_discharge_w: int,
    max_allowed_discharge_w: int,
) -> Dict[str, int]:
    """Return the DV export discharge target with a calm net-point guard."""
    base_w = max(0, int(base_discharge_w or 0))
    max_allowed_w = max(0, int(max_allowed_discharge_w or 0))
    base_w = min(base_w, max_allowed_w)
    min_export_w = max(0, safe_int(cfg.get("direct_marketing_min_grid_export_w"), 100))
    deadband_w = max(0, safe_int(cfg.get("direct_marketing_netpoint_deadband_w"), 30))
    up_step_w = max(50, safe_int(cfg.get("direct_marketing_netpoint_ramp_up_w"), 1000))
    down_step_w = max(50, safe_int(cfg.get("direct_marketing_netpoint_ramp_down_w"), 100))
    release_margin_w = max(
        deadband_w,
        safe_int(cfg.get("direct_marketing_netpoint_release_margin_w"), 80),
    )
    grid_w = safe_int(live.get("Grid_Power"), 0)
    grid_ema_w = safe_int(live.get("Grid_EMA_W", grid_w), grid_w)
    current_discharge_w = max(0, -safe_int(live.get("Battery_Power"), 0))
    local_deficit_w = direct_marketing_local_deficit_w(live)
    grid_error_w = max(0, grid_w + min_export_w - deadband_w, grid_ema_w + min_export_w - deadband_w)
    grid_export_w = max(0, -max(grid_w, grid_ema_w))
    export_surplus_w = max(0, grid_export_w - min_export_w)
    required_by_grid_w = current_discharge_w + grid_error_w if grid_error_w > 0 else 0
    required_by_load_w = local_deficit_w + min_export_w
    desired_w = min(max_allowed_w, max(base_w, required_by_grid_w))

    previous_name = str((previous_state or {}).get("state") or "")
    previous_w = safe_int((previous_state or {}).get("val"), 0) if previous_name.startswith("direct_marketing_") else current_discharge_w
    previous_w = max(0, min(max_allowed_w, previous_w))
    release_hold = False
    if desired_w > previous_w:
        target_w = min(desired_w, previous_w + up_step_w)
        urgent_floor_w = min(desired_w, max(required_by_load_w, required_by_grid_w))
        if target_w < urgent_floor_w:
            target_w = urgent_floor_w
    elif desired_w < previous_w:
        release_room_w = max(0, export_surplus_w - release_margin_w)
        if release_room_w < predump_budget_step_w(cfg):
            target_w = previous_w
            release_hold = True
        else:
            target_w = max(desired_w, previous_w - min(down_step_w, release_room_w))
        if target_w < base_w:
            target_w = base_w
    else:
        target_w = desired_w
    target_w = min(max_allowed_w, predump_round_budget_w(cfg, target_w))
    return {
        "target_w": target_w,
        "base_w": base_w,
        "max_allowed_w": max_allowed_w,
        "min_grid_export_w": min_export_w,
        "netpoint_deadband_w": deadband_w,
        "netpoint_release_margin_w": release_margin_w,
        "grid_export_w": grid_export_w,
        "export_surplus_w": export_surplus_w,
        "local_deficit_w": local_deficit_w,
        "grid_error_w": grid_error_w,
        "required_by_grid_w": required_by_grid_w,
        "required_by_load_w": required_by_load_w,
        "previous_w": previous_w,
        "desired_w": desired_w,
        "release_hold": 1 if release_hold else 0,
        "ramp_up_w": up_step_w,
        "ramp_down_w": down_step_w,
    }








def effective_power_limit_cfg(cfg: Dict[str, Any], live: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    live = live or {}
    result = dict(cfg or {})
    charge_w = configured_charge_limit_w(result, live)
    discharge_w = configured_discharge_limit_w(result, live, charge_w)
    result["maximumladeleistung"] = charge_w
    result["maximaleentladeleistung"] = discharge_w
    return result


def load_cfg(path: str = V4_CFG) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        cfg: Dict[str, Any] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    cfg[str(sub_key).lower()] = sub_value
            else:
                cfg[str(key).lower()] = value
        return cfg
    except Exception:
        return {}




def _json_cache_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_cache_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_cache_copy(item) for item in value]
    return value


def read_json_file(path: str, max_age_s: Optional[float] = None) -> Dict[str, Any]:
    global _JSON_READ_CACHE
    cache_key = os.path.abspath(path)
    try:
        now = time.time()
        mtime = os.path.getmtime(path)
        if max_age_s is not None and now - mtime > max_age_s:
            return {}
        cached = _JSON_READ_CACHE.get(cache_key)
        if cached and cached.get("mtime") == mtime:
            cached_data = cached.get("data")
            return _json_cache_copy(cached_data) if isinstance(cached_data, dict) else {}
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        clean_data = data if isinstance(data, dict) else {}
        _JSON_READ_CACHE[cache_key] = {"mtime": mtime, "data": _json_cache_copy(clean_data)}
        return _json_cache_copy(clean_data)
    except Exception:
        return {}


def read_json_any(path: str, max_age_s: Optional[float] = None) -> Any:
    cache_key = os.path.abspath(path)
    try:
        now = time.time()
        mtime = os.path.getmtime(path)
        if max_age_s is not None and now - mtime > max_age_s:
            return None
        cached = _JSON_READ_CACHE.get(cache_key)
        if cached and cached.get("mtime") == mtime:
            return _json_cache_copy(cached.get("data"))
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        _JSON_READ_CACHE[cache_key] = {"mtime": mtime, "data": _json_cache_copy(data)}
        return _json_cache_copy(data)
    except Exception:
        return None


def _boolish_direct_marketing_shelly_override(value: Any) -> bool:
    return str(value or "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "ein",
        "central",
        "zentral",
    }


def _valid_ipv4(value: Any) -> bool:
    text = str(value or "").strip()
    parts = text.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit():
            return False
        number = int(part)
        if number < 0 or number > 255:
            return False
    return True


def direct_marketing_current_market_price_ct(
    now_s: float,
    path: str = EPEX_F,
    require_direct_market: bool = False,
) -> Tuple[Optional[float], Dict[str, Any]]:
    data = read_json_any(path, max_age_s=48 * 3600)
    rows: List[Dict[str, Any]] = []
    if isinstance(data, list):
        rows = [row for row in data if isinstance(row, dict)]
    elif isinstance(data, dict):
        for key in ("prices", "data", "items", "slots"):
            value = data.get(key)
            if isinstance(value, list):
                rows = [row for row in value if isinstance(row, dict)]
                break
    now_ms = float(now_s) * 1000.0
    selected: Dict[str, Any] = {}
    for row in rows:
        start = safe_float(row.get("start_timestamp", row.get("start_ts", row.get("ts"))), 0.0)
        end = safe_float(row.get("end_timestamp", row.get("end_ts")), 0.0)
        if start > 0.0 and start < 10_000_000_000:
            start *= 1000.0
        if end > 0.0 and end < 10_000_000_000:
            end *= 1000.0
        if start <= now_ms < end:
            selected = row
            break
    if not selected:
        return None, {"status": "no_current_price_slot", "price_file": path}

    if require_direct_market:
        for key in ("direct_marketing_market_price_ct", "direct_marketing_market_ct"):
            if selected.get(key) is not None:
                return safe_float(selected.get(key), 0.0), {
                    "status": "ok",
                    "source_key": key,
                    "slot_start_ts": selected.get("start_timestamp", selected.get("start_ts")),
                    "slot_end_ts": selected.get("end_timestamp", selected.get("end_ts")),
                }
        for key in ("direct_marketing_marketprice", "direct_marketing_market_price_eur_mwh"):
            if selected.get(key) is not None:
                return safe_float(selected.get(key), 0.0) / 10.0, {
                    "status": "ok",
                    "source_key": key,
                    "source_unit": "EUR/MWh",
                    "slot_start_ts": selected.get("start_timestamp", selected.get("start_ts")),
                    "slot_end_ts": selected.get("end_timestamp", selected.get("end_ts")),
                }
        return None, {
            "status": "direct_market_price_missing",
            "price_source": selected.get("price_source"),
            "tariff_provider": selected.get("tariff_provider"),
            "slot_start_ts": selected.get("start_timestamp", selected.get("start_ts")),
            "slot_end_ts": selected.get("end_timestamp", selected.get("end_ts")),
        }

    for key in (
        "direct_marketing_market_price_ct",
        "market_price_ct_kwh",
        "price_ct_kwh",
        "billing_price_ct",
        "ct_kwh",
    ):
        if selected.get(key) is not None:
            return safe_float(selected.get(key), 0.0), {
                "status": "ok",
                "source_key": key,
                "slot_start_ts": selected.get("start_timestamp", selected.get("start_ts")),
                "slot_end_ts": selected.get("end_timestamp", selected.get("end_ts")),
            }

    for key in ("direct_marketing_marketprice", "marketprice", "price_eur_mwh"):
        if selected.get(key) is not None:
            eur_mwh = safe_float(selected.get(key), 0.0)
            return eur_mwh / 10.0, {
                "status": "ok",
                "source_key": key,
                "source_unit": "EUR/MWh",
                "slot_start_ts": selected.get("start_timestamp", selected.get("start_ts")),
                "slot_end_ts": selected.get("end_timestamp", selected.get("end_ts")),
            }

    return None, {"status": "price_value_missing", "slot_start_ts": selected.get("start_timestamp", selected.get("start_ts"))}


def direct_marketing_aux_inverter_shelly_control(
    cfg: Dict[str, Any],
    now_s: float,
    *,
    live: Optional[Dict[str, Any]] = None,
    price_path: str = EPEX_F,
    state_path: str = DIRECT_MARKETING_AUX_INVERTER_SHELLY_F,
    manual_lock_path: str = DIRECT_MARKETING_AUX_INVERTER_SHELLY_MANUAL_LOCK_F,
    guard_path: str = DIRECT_MARKETING_AUX_INVERTER_SHELLY_GUARD_F,
    migration_status_path: str = DIRECT_MARKETING_AUX_INVERTER_SHELLY_MIGRATION_F,
    timeout_s: float = 1.0,
) -> Dict[str, Any]:
    """Optionale zentrale Steuerung eines Schützes für ungeregelte AC-Zusatz-WR.

    Der Standard ist bewusst lokal/aus. Diese Funktion darf weder die
    Speicherentscheidung beeinflussen noch die RSCP-Steuerung sperren.
    """
    global _direct_marketing_aux_inverter_shelly_state

    contract = aux_inverter_effective_contract(cfg)
    config_blocked = bool(contract.get("commands_blocked"))
    override_requested = bool(contract.get("override") == "central" and not config_blocked)
    contract_migration_required = aux_inverter_state_migration_required(
        contract,
        override_requested=override_requested,
    )
    direct_marketing_explicitly_disabled = bool(
        "direct_marketing_enable" in cfg
        and not cfg_bool(cfg, "direct_marketing_enable", False)
    )
    configured_mode = str(cfg.get("direct_marketing_mode", "") or "").strip().lower().replace("+", "_plus")
    observe_only_mode = bool("direct_marketing_mode" in cfg and configured_mode in ("off", "safe"))
    override = bool(
        override_requested
        and not direct_marketing_explicitly_disabled
        and not observe_only_mode
    )
    capability_active = bool(override)
    ip = str(contract.get("ip") or "").strip()
    invert = bool(contract.get("invert"))
    # Eine Lastschwelle allein kann die Aufnahmeleistung eines großen Zusatz-WR
    # nicht garantieren. Deshalb bleibt die dynamische Freigabe opt-in.
    dynamic_unblock = bool(contract.get("dynamic_unblock_enable"))
    unblock_threshold_w = max(
        100,
        safe_int(contract.get("unblock_threshold_w"), 3000),
    )
    min_switch_interval_s = 600.0
    migration_identity = tuple(
        os.path.abspath(os.path.normpath(path))
        for path in (state_path, manual_lock_path, guard_path, migration_status_path)
    )

    if config_blocked:
        migration = {
            "status": "blocked",
            "terminal": False,
            "blocked": True,
            "reasons": ["config_contract_blocked"],
        }
    elif not contract_migration_required:
        migration = {
            "status": "not_applicable_unconfigured",
            "terminal": False,
            "blocked": False,
            "reasons": [],
            "cleanup_complete": False,
            "command_sent": False,
        }
    elif migration_identity in _direct_marketing_aux_inverter_migration_failure_latches:
        migration = dict(_direct_marketing_aux_inverter_migration_failure_latches[migration_identity])
    else:
        try:
            migration = migrate_aux_inverter_state_files(
                canonical_state_path=state_path,
                canonical_lock_path=manual_lock_path,
                canonical_guard_path=guard_path,
                status_path=migration_status_path,
                now_s=now_s,
                config_blocked=False,
                capability_active=capability_active,
                override_requested=override_requested,
                min_switch_interval_s=min_switch_interval_s,
            )
            if bool(migration.get("blocked")) and any(
                str(reason).endswith("_backup_failed")
                for reason in migration.get("reasons") or []
            ):
                migration = {
                    **migration,
                    "latched": True,
                    "latch_scope": "process_path_identity",
                }
                _direct_marketing_aux_inverter_migration_failure_latches[migration_identity] = dict(migration)
        except Exception as exc:
            migration = {
                "status": "blocked",
                "terminal": False,
                "blocked": True,
                "reasons": ["state_migration_error"],
                "error_type": type(exc).__name__,
                "latched": True,
                "latch_scope": "process_path_identity",
            }
            _direct_marketing_aux_inverter_migration_failure_latches[migration_identity] = dict(migration)

    migration_required = bool(
        migration.get("migration_required", contract_migration_required)
    )

    live = live if isinstance(live, dict) else {}
    grid_w = safe_float(live.get("grid_w", live.get("Grid_Power")), 0.0)
    house_w = max(0.0, safe_float(live.get("house_w", live.get("home_w", live.get("House_Power"))), 0.0))
    wallbox_w = max(0.0, abs(safe_float(live.get("wallbox_w", live.get("Wallbox_Power")), 0.0)))
    load_w = max(house_w, wallbox_w)
    live_stale = bool(live.get("live_stale", False))
    live_sample_valid = bool(live.get("live_sample_valid", live.get("RSCP_Sample_Valid", True)))
    live_valid = bool(live and not live_stale and live_sample_valid)
    manual_lock = read_json_file(manual_lock_path)
    manual_locked = bool(manual_lock.get("locked") is True)
    previous = (
        _direct_marketing_aux_inverter_shelly_state
        if isinstance(_direct_marketing_aux_inverter_shelly_state, dict)
        else {}
    )
    if not migration_required:
        previous = {}
    elif not previous:
        previous = read_json_file(state_path)
    persisted_guard = read_json_file(guard_path) if migration_required else {}
    if safe_float(persisted_guard.get("last_state_change_ts"), 0.0) > safe_float(previous.get("last_state_change_ts"), 0.0):
        previous = {
            **previous,
            "desired_wr_on": persisted_guard.get("desired_wr_on"),
            "desired_on": persisted_guard.get("desired_wr_on"),
            "relay_on": persisted_guard.get("relay_on"),
            "last_state_change_ts": persisted_guard.get("last_state_change_ts"),
            "last_send_ts": persisted_guard.get("last_send_ts"),
            "last_attempt_ts": persisted_guard.get("last_send_ts"),
        }
    previous_relay_on = previous.get("relay_on")
    if not isinstance(previous_relay_on, bool):
        previous_relay_on = None
    previous_wr_on = previous.get("desired_wr_on", previous.get("desired_on"))
    if not isinstance(previous_wr_on, bool):
        previous_wr_on = None
    previous_send_ts = safe_float(previous.get("last_send_ts"), 0.0)
    previous_attempt_ts = safe_float(previous.get("last_attempt_ts"), previous_send_ts)
    previous_change_ts = safe_float(previous.get("last_state_change_ts"), 0.0)
    guard_block_until = max(
        safe_float(persisted_guard.get("block_until"), 0.0),
        safe_float(persisted_guard.get("next_switch_allowed_ts"), 0.0),
        previous_change_ts + min_switch_interval_s if previous_change_ts > 0 else 0.0,
    )
    state: Dict[str, Any] = {
        "schema": "direct_marketing_aux_inverter_shelly_v2",
        "ts": int(now_s),
        "enabled": bool(override),
        "override_requested": bool(override_requested),
        "direct_marketing_explicitly_disabled": bool(direct_marketing_explicitly_disabled),
        "direct_marketing_mode": configured_mode or None,
        "mode": "central" if override else "local",
        "invert": bool(invert),
        "relay_semantics": "nc_contactor_shelly_on_means_wr_off" if invert else "normal_shelly_on_means_wr_on",
        "contract_status": "blocked" if config_blocked else "ok",
        "contract_reason": str((contract.get("migration") or {}).get("reason") or ""),
        "migration_status": str(migration.get("status") or "blocked"),
        "migration_reasons": list(migration.get("reasons") or []),
        "migration_required": bool(migration_required),
        "ip_configured": bool(ip),
        "ip_valid": _valid_ipv4(ip),
        "control_available": bool(override and _valid_ipv4(ip) and not config_blocked),
        "lock_available": bool(override and _valid_ipv4(ip) and not config_blocked),
        "price_ct_kwh": None,
        "threshold_ct_kwh": -0.0001,
        "dynamic_unblock_enabled": bool(dynamic_unblock),
        "unblock_threshold_w": int(unblock_threshold_w),
        "grid_w": round(grid_w, 1) if live else None,
        "house_w": round(house_w, 1) if live else None,
        "wallbox_w": round(wallbox_w, 1) if live else None,
        "load_w": round(load_w, 1) if live else None,
        "live_valid": bool(live_valid),
        "live_stale": bool(live_stale),
        "manual_locked": bool(manual_locked),
        "manual_lock_ts": manual_lock.get("ts") if manual_locked else None,
        "min_switch_interval_s": int(min_switch_interval_s),
        "last_state_change_ts": previous_change_ts,
        "switch_lock_remaining_s": max(0, int(round(guard_block_until - now_s))),
        "requested_wr_on": None,
        "requested_relay_on": None,
        "desired_wr_on": None,
        "desired_on": None,
        "relay_on": None,
        "command_sent": False,
        "command_status": "idle",
        "status": "local_fallback",
        "error": "",
    }

    def persist_state() -> Dict[str, Any]:
        global _direct_marketing_aux_inverter_shelly_state
        _direct_marketing_aux_inverter_shelly_state = dict(state)
        try:
            write_aux_inverter_canonical_state(state_path, state, mode=0o664)
        except Exception:
            pass
        return state

    if config_blocked:
        state["status"] = "migration_blocked"
        state["command_status"] = "blocked"
        state["error"] = "config_contract_blocked"
        return persist_state()
    if bool(migration.get("blocked")):
        state["status"] = "migration_blocked"
        state["command_status"] = "blocked"
        state["error"] = "state_contract_blocked"
        return persist_state()
    if not override:
        if direct_marketing_explicitly_disabled:
            state["status"] = "direct_marketing_disabled"
        elif observe_only_mode:
            state["status"] = "strategy_observe_only"
        return persist_state()
    if not state["ip_valid"]:
        state["status"] = "invalid_ip"
        return persist_state()

    settlement_basis = str(cfg.get("direct_marketing_settlement_basis", "day_ahead_15min") or "day_ahead_15min").strip().lower()
    tariff_provider = str(cfg.get("tariff_provider", "") or "").strip().lower()
    require_direct_market = bool(
        settlement_basis in ("day_ahead_15min", "day-ahead-15min", "day_ahead", "day-ahead", "market")
        and tariff_provider in ("tibber", "octopus", "octopus_energy", "retail", "end_customer")
    )
    price_ct, price_meta = direct_marketing_current_market_price_ct(
        now_s,
        price_path,
        require_direct_market=require_direct_market,
    )
    state["price_meta"] = price_meta
    if price_ct is None and not manual_locked:
        state["status"] = "price_unavailable"
        return persist_state()

    state["price_ct_kwh"] = round(float(price_ct), 6) if price_ct is not None else None
    negative_price = bool(price_ct is not None and float(price_ct) < -0.0001)
    state["export_constraint_class"] = "negative_hard" if negative_price else "positive_price_allowed"
    state["hard_export_limit_active"] = bool(negative_price)
    state["hard_export_limit_w"] = 0 if negative_price else None
    state["pv_export_allowed"] = not negative_price
    decision_reason = "positive_price"
    requested_status = "wr_on"
    if manual_locked:
        requested_wr_on = False
        requested_status = "manual_locked"
        decision_reason = "manual_lock"
    elif not negative_price:
        requested_wr_on = True
    elif not dynamic_unblock:
        requested_wr_on = False
        requested_status = "wr_off"
        decision_reason = "negative_price_static_lock"
    elif not live_valid:
        requested_wr_on = False
        requested_status = "wr_off"
        decision_reason = "negative_price_live_data_invalid"
    elif previous_wr_on is True:
        requested_wr_on = not (grid_w < -100.0)
        requested_status = "load_unblocked" if requested_wr_on else "wr_off"
        decision_reason = "negative_price_no_export" if requested_wr_on else "negative_price_export_detected"
    else:
        requested_wr_on = bool(load_w > float(unblock_threshold_w) and grid_w >= -100.0)
        requested_status = "load_unblocked" if requested_wr_on else "wr_off"
        decision_reason = "negative_price_load_unblocked" if requested_wr_on else "negative_price_load_below_threshold"

    requested_relay_on = (not requested_wr_on) if invert else requested_wr_on
    state["negative_price"] = bool(negative_price)
    state["requested_wr_on"] = bool(requested_wr_on)
    state["requested_relay_on"] = bool(requested_relay_on)
    state["decision_reason"] = decision_reason

    relay_change_requested = previous_relay_on is None or previous_relay_on != requested_relay_on
    previous_requested_relay_on = previous.get("requested_relay_on")
    previous_http_error = previous.get("command_status") == "http_error" or previous.get("status") == "http_error"
    if (
        previous_http_error
        and previous_requested_relay_on == requested_relay_on
        and now_s - previous_attempt_ts < 60.0
    ):
        state["desired_wr_on"] = previous_wr_on
        state["desired_on"] = previous_wr_on
        state["relay_on"] = previous_relay_on
        state["status"] = "http_error"
        state["command_status"] = "retry_backoff"
        state["last_send_ts"] = previous_send_ts
        state["last_attempt_ts"] = previous_attempt_ts
        state["hold_reason"] = "retry_backoff"
        state["error"] = str(previous.get("error") or "")[:80]
        return persist_state()

    switch_lock_remaining_s = state["switch_lock_remaining_s"]
    manual_off_bypass = bool(manual_locked and requested_wr_on is False)
    if relay_change_requested and switch_lock_remaining_s > 0 and not manual_off_bypass and previous_relay_on is not None:
        state["desired_wr_on"] = previous_wr_on
        state["desired_on"] = previous_wr_on
        state["relay_on"] = previous_relay_on
        state["status"] = "load_unblocked" if previous_wr_on and negative_price else ("wr_on" if previous_wr_on else "wr_off")
        state["command_status"] = "hysteresis_hold"
        state["last_send_ts"] = previous_send_ts
        state["last_attempt_ts"] = previous_attempt_ts
        state["hold_reason"] = "anti_flatter_600s"
        return persist_state()

    state["desired_wr_on"] = bool(requested_wr_on)
    state["desired_on"] = bool(requested_wr_on)
    state["relay_on"] = bool(requested_relay_on)
    state["status"] = requested_status

    resend_s = 60.0
    if not relay_change_requested and now_s - previous_attempt_ts < resend_s:
        state["command_status"] = "held"
        state["last_send_ts"] = previous_send_ts
        state["last_attempt_ts"] = previous_attempt_ts
        state["hold_reason"] = "retry_backoff" if previous.get("command_status") == "http_error" or previous.get("status") == "http_error" else "unchanged_state"
        return persist_state()

    url = f"http://{ip}/rpc/Switch.Set?id=0&on={'true' if requested_relay_on else 'false'}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "E3DC-Control-DV-Shelly/1"})
        with urllib.request.urlopen(request, timeout=max(0.2, float(timeout_s))) as response:
            state["http_status"] = getattr(response, "status", None)
            try:
                response.read(512)
            except Exception:
                pass
        state["relay_status"] = "relay_on" if requested_relay_on else "relay_off"
        state["command_sent"] = True
        state["command_status"] = "sent"
        state["last_send_ts"] = now_s
        state["last_attempt_ts"] = now_s
        if relay_change_requested:
            state["last_state_change_ts"] = now_s
            state["switch_lock_remaining_s"] = int(min_switch_interval_s)
            try:
                guard_payload = {
                    "schema": "direct_marketing_aux_inverter_shelly_guard_v1",
                    "last_state_change_ts": now_s,
                    "last_send_ts": now_s,
                    "block_until": now_s + min_switch_interval_s,
                    "next_switch_allowed_ts": now_s + min_switch_interval_s,
                    "desired_wr_on": bool(requested_wr_on),
                    "relay_on": bool(requested_relay_on),
                }
                write_aux_inverter_canonical_state(guard_path, guard_payload, mode=0o660)
            except Exception:
                pass
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        state["status"] = "http_error"
        state["command_status"] = "http_error"
        state["error"] = type(exc).__name__
        state["last_send_ts"] = previous_send_ts
        state["last_attempt_ts"] = now_s
        state["last_state_change_ts"] = previous_change_ts
        if previous_relay_on is not None:
            state["relay_on"] = previous_relay_on
            state["desired_wr_on"] = previous_wr_on
            state["desired_on"] = previous_wr_on

    return persist_state()


def acknowledge_manual_override_done(
    manual_override: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    path: str = MANUAL_OVERRIDE_F,
    anchor_path: str = MANUAL_ANCHOR_F,
    now_s: Optional[float] = None,
) -> bool:
    if not manual_override or not payload or payload.get("state") != "manual_override_done":
        return False
    mode = str(manual_override.get("mode", "") or "").lower()
    target = safe_float(manual_override.get("target_soc"), -1.0)
    ts = safe_int(manual_override.get("ts"), 0)
    if mode not in ("charge", "discharge") or target < 0.0 or ts <= 0:
        return False

    try:
        current = read_json_file(path)
        if str(current.get("mode", "") or "").lower() != mode:
            return False
        if abs(safe_float(current.get("target_soc"), -999.0) - target) > 0.01:
            return False
        if safe_int(current.get("ts"), -1) != ts:
            return False
        anchor_ts = int(now_s if now_s is not None else time.time())
        anchor = {
            "active": True,
            "source": "manual_override_done",
            "mode": mode,
            "target_soc": round(target, 2),
            "reached_soc": round(safe_float(payload.get("manual_reached_soc"), target), 2),
            "manual_ts": ts,
            "ts": anchor_ts,
            "expires_ts": anchor_ts + 6 * 3600,
            "release_mode": payload.get("manual_release_mode"),
        }
        try:
            atomic_write(anchor_path, anchor, indent=2)
        except Exception as exc:
            log.warning("Manual Override Anker konnte nicht geschrieben werden: %s", exc)
        os.unlink(path)
        log.info(
            "Manual Override %s bis %.1f%% erreicht - Override quittiert und Anker gesetzt.",
            mode,
            target,
        )
        return True
    except FileNotFoundError:
        return False
    except Exception as exc:
        log.warning("Manual Override konnte nach Zielerreichung nicht geloescht werden: %s", exc)
        return False


def augment_consumer_live(
    live: Dict[str, Any],
    energy_decision: Dict[str, Any],
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(live, dict) or not isinstance(energy_decision, dict):
        return live
    cfg = cfg if isinstance(cfg, dict) else {}
    heatpump = energy_decision.get("heatpump") if isinstance(energy_decision.get("heatpump"), dict) else {}
    if not heatpump:
        return live
    wp_power_w = max(0, safe_int(heatpump.get("wp_power_w"), 0))
    if wp_power_w <= 0:
        return live
    merged = dict(live)
    raw_home_w = max(0, safe_int(merged.get("Home_Power"), 0))
    split_mode = str(cfg.get("storage_home_wp_split", "auto") or "auto").strip().lower()
    if split_mode in ("1", "true", "yes", "on", "include", "included", "home_includes_wp"):
        home_includes_wp = True
    elif split_mode in ("0", "false", "no", "off", "separate", "excluded", "home_excludes_wp"):
        home_includes_wp = False
    else:
        home_includes_wp = raw_home_w >= max(500, int(wp_power_w * 0.55))
    merged["WP_Power"] = wp_power_w
    merged["WP_Power_Source"] = "energy_decision"
    if home_includes_wp:
        merged["Home_Power_Raw"] = raw_home_w
        merged["Home_Power"] = max(0, raw_home_w - wp_power_w)
        merged["Home_Includes_WP"] = True
    else:
        merged.setdefault("Home_Power_Raw", raw_home_w)
        merged["Home_Includes_WP"] = False
    if bool(heatpump.get("predump_active")):
        merged["Predump_Heatpump_Power"] = wp_power_w
        merged["Predump_Heatpump_Source"] = "energy_decision"
    return merged




def live_data_age_s(live: Dict[str, Any], now_s: Optional[float] = None) -> float:
    now_s = time.time() if now_s is None else float(now_s)
    ts = safe_float(live.get("_ts"), 0.0)
    if ts <= 0.0:
        return 0.0
    return max(0.0, now_s - ts)


@dataclass(frozen=True)
class StorageInputSnapshot:
    live: LiveDataSnapshot
    plan: Any
    wallbox: WallboxStatusSnapshot


@dataclass(frozen=True)
class WallboxBudgetContext:
    wallbox_w: float
    wallbox_power_source: str
    live_wallbox_w: float
    native_wallbox_w: float
    home_includes_wallbox: bool
    live_with_wallbox: Dict[str, Any]
    wb_possible_w: int
    pv_after_fixed_w: int
    base_wb_budget_w: int
    wb_car_present: bool


def build_storage_input_snapshot(
    live: Dict[str, Any],
    plan: Dict[str, Any],
    wb_native: Optional[Dict[str, Any]],
    *,
    now_s: float,
) -> StorageInputSnapshot:
    return StorageInputSnapshot(
        live=LiveDataSnapshot.from_dict(live, now_s=now_s),
        plan=plan if isinstance(plan, ValidatedCanonicalPlanSnapshot) else StoragePlanSnapshot.from_dict(plan),
        wallbox=WallboxStatusSnapshot.from_dict(wb_native or {}),
    )


def storage_live_stale_decision(
    *,
    cfg: Dict[str, Any],
    plan: Dict[str, Any],
    live_age_s_value: float,
    live_stale: bool,
    max_charge_w: int,
    now_s: float,
) -> Optional[Dict[str, Any]]:
    """Return the data guard decision for stale live data, if it must own the cycle."""

    if not live_stale:
        return None
    curve_soc, target_soc, target_ts = current_curve(plan, now_s)
    return {
        "decision": {
            "state": "live_stale_auto",
            "mode": MODE_AUTO,
            "val": max_charge_w,
            "priority": "data_guard",
            "reason": "Live-Daten %.0fs alt: keine neue aktive Speicher-Vorgabe, E3DC autonom" % live_age_s_value,
            "protected": False,
            "storage_req_w": 0,
            "budget_w": 0,
        },
        "curve_soc": curve_soc,
        "target_soc": target_soc,
        "target_ts": target_ts,
        "i_fc_w": 0,
        "i_min_lade_w": 0,
    }


def storage_live_plausibility_decision(
    *,
    cfg: Optional[Dict[str, Any]] = None,
    plan: Dict[str, Any],
    plausibility: Dict[str, Any],
    max_charge_w: int,
    now_s: float,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return a data guard decision for explicitly invalid live power frames."""

    if bool(plausibility.get("sample_valid", True)):
        return None
    curve_soc, target_soc, target_ts = current_curve(plan, now_s)
    reasons = plausibility.get("reasons") if isinstance(plausibility.get("reasons"), list) else []
    reason_text = ", ".join(str(item) for item in reasons if str(item or "").strip())
    if not reason_text:
        invalid_parts = []
        if not bool(plausibility.get("home_valid", True)):
            invalid_parts.append("Home_Power")
        if not bool(plausibility.get("grid_valid", True)):
            invalid_parts.append("Grid_Power")
        reason_text = ", ".join(invalid_parts) or "Live-Plausibilität"
    previous_state = previous_state or {}
    previous_auto_limit = (
        previous_state.get("auto_limit")
        if isinstance(previous_state.get("auto_limit"), dict)
        else {}
    )
    preserve_auto_limit = bool(previous_auto_limit.get("enabled")) and not bool(
        previous_auto_limit.get("release")
    )
    preserved_auto_limit = dict(previous_auto_limit) if preserve_auto_limit else None
    if preserved_auto_limit is not None:
        preserved_auto_limit["reason"] = (
            str(preserved_auto_limit.get("reason") or "Vorherige EMS-Ladegrenze")
            + "; Live-Frame unplausibel, letzte aktive EMS-Grenze wird gehalten"
        )[:220]
    decision = {
        "state": "live_plausibility_auto",
        "mode": MODE_AUTO,
        "val": (
            max(0, safe_int(preserved_auto_limit.get("max_charge_w"), max_charge_w))
            if preserved_auto_limit is not None
            else max_charge_w
        ),
        "priority": "data_guard",
        "reason": "Live-Frame unplausibel (%s): %s"
        % (
            reason_text,
            (
                "letzte aktive EMS-Grenze bleibt gehalten"
                if preserved_auto_limit is not None
                else "keine neue aktive Speicher-Vorgabe, E3DC autonom"
            ),
        ),
        "protected": False,
        "storage_req_w": 0,
        "budget_w": 0,
        "live_sample_invalid": True,
        "live_plausibility": plausibility,
    }
    if preserved_auto_limit is not None:
        decision["auto_limit"] = preserved_auto_limit
        decision["live_plausibility_preserved_auto_limit"] = True
    preserved_discharge = storage_live_plausibility_preserved_discharge_owner(
        cfg=cfg or {},
        plan=plan,
        plausibility=plausibility,
        reason_text=reason_text,
        now_s=now_s,
        previous_state=previous_state,
    )
    preserved_charge = None
    if preserved_discharge is None:
        preserved_charge = storage_live_plausibility_preserved_charge_owner(
            cfg=cfg or {},
            plan=plan,
            plausibility=plausibility,
            reason_text=reason_text,
            now_s=now_s,
            previous_state=previous_state,
        )
    if preserved_discharge is not None:
        decision = preserved_discharge
    elif preserved_charge is not None:
        decision = preserved_charge
    if str(previous_state.get("state") or "") == "unmanaged_wallbox_wbminsoc_hold":
        previous_mode = safe_int(previous_state.get("mode"), MODE_AUTO)
        previous_pv_charge_w = max(0, safe_int(previous_state.get("planned_grid_pv_charge_w"), 0))
        previous_storage_req_w = max(0, safe_int(previous_state.get("storage_req_w"), 0))
        previous_active_charge = bool(
            previous_mode == MODE_CHRG
            or previous_pv_charge_w > 0
            or previous_storage_req_w > 0
        )
        if (
            bool(previous_state.get("scheduled_grid_charge"))
            and bool(previous_state.get("unmanaged_wallbox_wbminsoc_hold"))
        ):
            hold_w = 0
            if previous_active_charge:
                hold_w = max(
                    0,
                    min(
                        max_charge_w,
                        previous_pv_charge_w
                        or previous_storage_req_w
                        or safe_int(previous_state.get("val"), 0),
                    ),
                )
            hold_auto_limit = preserved_auto_limit
            if hold_auto_limit is None:
                previous_limit = previous_state.get("auto_limit") if isinstance(previous_state.get("auto_limit"), dict) else {}
                hold_auto_limit = dict(previous_limit) if previous_limit else None
            if hold_auto_limit is None:
                hold_auto_limit = {
                    "enabled": True,
                    "release": False,
                    "max_charge_w": max_charge_w,
                    "max_discharge_w": 0,
                    "discharge_start_w": 0,
                    "heartbeat_s": 2.0,
                    "reason": "Vorheriger Wallbox-Entladungsschutz",
                }
            hold_auto_limit["max_discharge_w"] = 0
            hold_auto_limit["max_charge_w"] = max(0, safe_int(hold_auto_limit.get("max_charge_w"), max_charge_w))
            hold_auto_limit["release"] = False
            hold_auto_limit["enabled"] = True
            hold_auto_limit["reason"] = (
                str(hold_auto_limit.get("reason") or "Vorheriger Wallbox-Entladungsschutz")
                + "; Live-Frame unplausibel, geplanter Wallbox-Schutzvertrag bleibt gehalten"
            )[:220]
            decision.update(
                {
                    "state": "unmanaged_wallbox_wbminsoc_hold",
                    "mode": MODE_CHRG if hold_w > 0 else MODE_AUTO,
                    "val": hold_w if hold_w > 0 else max_charge_w,
                    "priority": "safety",
                    "reason": "Live-Frame unplausibel (%s): geplanter Wallbox-Netzlade-Schutzvertrag bleibt gehalten"
                    % reason_text,
                    "protected": True,
                    "storage_req_w": hold_w,
                    "budget_w": 0,
                    "unmanaged_wallbox_wbminsoc_hold": True,
                    "scheduled_grid_charge": True,
                    "wbminsoc_pv_charge_active": hold_w > 0,
                    "planned_grid_pv_charge_w": hold_w,
                    "planned_grid_pv_surplus_w": hold_w,
                    "auto_limit": hold_auto_limit,
                    "live_plausibility_preserved_wbminsoc_contract": True,
                    "live_plausibility_preserved_auto_limit": True,
                }
            )
    return {
        "decision": decision,
        "curve_soc": curve_soc,
        "target_soc": target_soc,
        "target_ts": target_ts,
        "i_fc_w": 0,
        "i_min_lade_w": 0,
    }


def storage_live_plausibility_preserved_discharge_owner(
    *,
    cfg: Dict[str, Any],
    plan: Dict[str, Any],
    plausibility: Dict[str, Any],
    reason_text: str,
    now_s: float,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Hält einen gerade aktiven Entladepfad durch einen kurzen Messwertaussetzer."""

    previous_state = previous_state or {}
    previous_name = str(previous_state.get("state") or "")
    previous_mode = safe_int(previous_state.get("mode"), MODE_AUTO)
    previous_w = max(0, safe_int(previous_state.get("val"), 0))
    if previous_mode != MODE_DISCH or previous_w < 300 or not bool(previous_state.get("protected")):
        return None

    hold_s = max(0.0, safe_float(cfg.get("storage_live_glitch_discharge_hold_s"), 20.0))
    if hold_s <= 0.0:
        return None
    previous_ts = safe_float(previous_state.get("ts"), 0.0)
    if previous_ts <= 0.0:
        return None
    age_s = max(0.0, now_s - previous_ts)
    if age_s > hold_s:
        return None

    hold_owner = ""
    if previous_name.startswith("direct_marketing_eco_plus_export"):
        direct = direct_marketing_plan(plan)
        direct_mode = str(direct.get("mode") or "").strip().lower().replace("-", "_").replace(" ", "_")
        if direct_mode in {"eco+", "ecoplus"}:
            direct_mode = "eco_plus"
        if direct_mode != "eco_plus":
            return None
        window = direct_marketing_current_window(direct, now_s)
        action = str((window or {}).get("action") or "")
        if action != "eco_plus_export_candidate":
            return None
        previous_action = str(previous_state.get("direct_marketing_action") or "")
        if previous_action and previous_action != action:
            return None
        reserve_floor = safe_float(previous_state.get("direct_marketing_reserve_floor_soc_pct"), 0.0)
        previous_soc = safe_float(previous_state.get("soc"), 0.0)
        if reserve_floor > 0.0 and previous_soc <= reserve_floor + 0.5:
            return None
        hold_owner = "Direktvermarktungs-Export"
    elif previous_name.startswith("pre_discharge"):
        previous_soc = safe_float(previous_state.get("soc"), 0.0)
        floor_soc = safe_float(
            previous_state.get("predump_floor_soc"),
            safe_float(previous_state.get("predump_target_soc"), 0.0),
        )
        if floor_soc > 0.0 and previous_soc <= floor_soc + 0.5:
            return None
        if not (
            bool(previous_state.get("predump_active"))
            or bool(previous_state.get("predump_grid_fallback"))
            or bool(previous_state.get("predump_hard_predump"))
        ):
            return None
        hold_owner = "Pre-Dump-Entladung"
    else:
        return None

    decision = dict(previous_state)
    decision.update({
        "state": previous_name,
        "mode": MODE_DISCH,
        "val": previous_w,
        "priority": str(previous_state.get("priority") or "data_guard"),
        "protected": True,
        "reason": (
            "Live-Frame unplausibel (%s): %s bleibt %.0fs gehalten; "
            "keine neue Leistungsberechnung aus dem ungültigen Frame"
        ) % (reason_text, hold_owner, age_s),
        "live_sample_invalid": True,
        "live_plausibility": plausibility,
        "live_plausibility_preserved_discharge_owner": True,
        "live_plausibility_preserved_discharge_state": previous_name,
        "live_plausibility_preserved_discharge_age_s": round(age_s, 1),
        "live_plausibility_preserved_discharge_hold_s": round(hold_s, 1),
    })
    return decision


def storage_live_plausibility_preserved_charge_owner(
    *,
    cfg: Dict[str, Any],
    plan: Dict[str, Any],
    plausibility: Dict[str, Any],
    reason_text: str,
    now_s: float,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Hält einen gerade aktiven PV-Speicherpfad durch einen kurzen Messwertaussetzer."""

    previous_state = previous_state or {}
    previous_name = str(previous_state.get("state") or "")
    previous_mode = safe_int(previous_state.get("mode"), MODE_AUTO)
    previous_w = max(0, safe_int(previous_state.get("val"), 0))
    previous_market_export_absorb = bool(
        previous_name == "market_grid_charge"
        and previous_mode in (MODE_GRID, MODE_AUTO)
        and bool(previous_state.get("market_live_export_absorb_active"))
    )
    if (
        not (previous_mode == MODE_CHRG or previous_market_export_absorb)
        or previous_w < 300
        or not bool(previous_state.get("protected"))
    ):
        return None

    hold_s = max(
        0.0,
        safe_float(
            cfg.get("storage_live_glitch_charge_hold_s"),
            safe_float(cfg.get("storage_live_glitch_owner_hold_s"), 20.0),
        ),
    )
    if hold_s <= 0.0:
        return None
    previous_ts = safe_float(previous_state.get("ts"), 0.0)
    if previous_ts <= 0.0:
        return None
    age_s = max(0.0, now_s - previous_ts)
    if age_s > hold_s:
        return None

    hold_owner = ""
    hold_mode = MODE_CHRG
    if previous_market_export_absorb:
        market = market_economics_plan(plan)
        contract = market_economics_current_contract(market, now_s)
        if not isinstance(contract, dict) or str(contract.get("action") or "") != "grid_charge":
            return None
        forecast = contract.get("forecast") if isinstance(contract.get("forecast"), dict) else {}
        target_soc = safe_float(
            forecast.get("grid_charge_target_soc_pct"),
            safe_float(previous_state.get("market_economics_target_soc_pct"), 0.0),
        )
        previous_soc = safe_float(previous_state.get("soc"), 0.0)
        if target_soc > 0.0 and previous_soc >= target_soc - 0.5:
            return None
        hold_owner = "Markt-Netzladen Export-Absorb"
        hold_mode = previous_mode
    else:
        if previous_name != "direct_marketing_eco_plus_pv_store":
            return None
        direct = direct_marketing_plan(plan)
        window = direct_marketing_current_window(direct, now_s)
        action = str((window or {}).get("action") or "")
        if action not in DIRECT_MARKETING_PV_STORE_ACTIONS:
            return None
        previous_action = str(previous_state.get("direct_marketing_action") or "")
        if previous_action and previous_action != action:
            return None

        target_soc = safe_float(
            previous_state.get("direct_marketing_target_soc_pct"),
            safe_float((window or {}).get("target_soc_pct"), 0.0),
        )
        previous_soc = safe_float(previous_state.get("soc"), 0.0)
        if target_soc > 0.0 and previous_soc >= target_soc - 0.5:
            return None

        import_guard_w = max(
            0,
            safe_int(previous_state.get("direct_marketing_pv_store_import_guard_w"), 80),
        )
        grid_import_w = max(0, safe_int(previous_state.get("direct_marketing_pv_store_grid_import_w"), 0))
        if grid_import_w > import_guard_w:
            return None

        dc_only = bool(previous_state.get("direct_marketing_pv_store_dc_only"))
        previous_surplus_w = max(0, safe_int(previous_state.get("direct_marketing_pv_store_surplus_w"), 0))
        previous_dc_surplus_w = max(0, safe_int(previous_state.get("direct_marketing_pv_store_dc_surplus_w"), 0))
        previous_offer_w = max(
            0,
            safe_int(previous_state.get("direct_marketing_pv_store_offer_w"), 0),
            safe_int(previous_state.get("direct_marketing_pv_store_pv_safe_cap_w"), 0),
        )
        safe_source_w = previous_dc_surplus_w if dc_only else max(previous_surplus_w, previous_offer_w)
        if safe_source_w > 0 and safe_source_w + 100 < previous_w:
            return None
        hold_owner = "Direktvermarktungs-PV-Speichern"

    decision = dict(previous_state)
    decision.update({
        "state": previous_name,
        "mode": hold_mode,
        "val": previous_w,
        "priority": str(previous_state.get("priority") or "data_guard"),
        "protected": True,
        "reason": (
            "Live-Frame unplausibel (%s): %s bleibt %.0fs gehalten; "
            "keine neue Leistungsberechnung aus dem ungültigen Frame"
        ) % (reason_text, hold_owner, age_s),
        "live_sample_invalid": True,
        "live_plausibility": plausibility,
        "live_plausibility_preserved_charge_owner": True,
        "live_plausibility_preserved_charge_state": previous_name,
        "live_plausibility_preserved_charge_age_s": round(age_s, 1),
        "live_plausibility_preserved_charge_hold_s": round(hold_s, 1),
    })
    return decision


def soc_jump_guard_context(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    previous_state: Optional[Dict[str, Any]],
    now_s: float,
) -> Dict[str, Any]:
    """Detect physically impossible SoC frames before safety owners act on them."""

    if not cfg_bool(cfg, "storage_soc_jump_guard_enable", True):
        return {"invalid": False}

    previous_state = previous_state or {}
    raw_soc = safe_float(live.get("SOC"), -1.0)
    if raw_soc < 0.0 or raw_soc > 100.0:
        return {
            "invalid": True,
            "reason": "SoC %.1f%% außerhalb des gültigen Bereichs" % raw_soc,
            "raw_soc": raw_soc,
            "last_valid_soc": previous_state.get("last_valid_soc"),
            "drop_pct": None,
            "allowed_drop_pct": None,
        }

    last_valid_soc = previous_state.get("last_valid_soc")
    if last_valid_soc is None or str(last_valid_soc).strip() == "":
        last_valid_soc = previous_state.get("soc")
    last_valid = safe_float(last_valid_soc, -1.0)
    if last_valid < 0.0 or last_valid > 100.0:
        return {"invalid": False, "last_valid_soc": raw_soc, "last_valid_soc_ts": now_s}

    last_ts = safe_float(
        previous_state.get("last_valid_soc_ts"),
        safe_float(previous_state.get("ts"), 0.0),
    )
    elapsed_s = max(0.0, min(3600.0, now_s - last_ts)) if last_ts > 0.0 else 0.0
    drop_pct = last_valid - raw_soc
    if drop_pct <= 0.0:
        return {
            "invalid": False,
            "last_valid_soc": raw_soc,
            "last_valid_soc_ts": now_s,
            "drop_pct": round(drop_pct, 2),
        }

    reserve_soc = ep_reserve_soc(cfg, live)
    min_drop_pct = max(3.0, safe_float(cfg.get("storage_soc_jump_guard_min_drop_pct"), 8.0))
    capacity_kwh = max(
        1.0,
        safe_float(
            live.get("bat_full_cap_kwh"),
            safe_float(live.get("battery_capacity_kwh"), safe_float(cfg.get("speichergroesse"), 10.0)),
        ),
    )
    max_discharge_w = max(
        250.0,
        safe_float(
            live.get("user_discharge_limit_w"),
            safe_float(cfg.get("maximaleentladeleistung"), safe_float(cfg.get("maximumladeleistung"), 5000.0)),
        ),
    )
    physical_drop_pct = (max_discharge_w * max(elapsed_s, 60.0) / 3600.0) / (capacity_kwh * 1000.0) * 100.0
    fallback_drop_pct = max(
        0.5,
        safe_float(cfg.get("storage_soc_jump_guard_max_drop_pct_per_min"), 1.0),
    ) * max(1.0, elapsed_s / 60.0)
    base_margin_pct = max(1.0, safe_float(cfg.get("storage_soc_jump_guard_base_margin_pct"), 2.0))
    allowed_drop = base_margin_pct + min(physical_drop_pct, fallback_drop_pct)
    # Near the reserve floor, do not hide a real empty-battery situation.
    protect_floor_margin_pct = max(1.0, safe_float(cfg.get("storage_soc_jump_guard_reserve_margin_pct"), 3.0))
    high_enough_to_reject = bool(last_valid > reserve_soc + protect_floor_margin_pct)
    invalid = bool(
        high_enough_to_reject
        and drop_pct >= min_drop_pct
        and drop_pct > allowed_drop
    )
    if not invalid:
        return {
            "invalid": False,
            "last_valid_soc": raw_soc,
            "last_valid_soc_ts": now_s,
            "drop_pct": round(drop_pct, 2),
            "allowed_drop_pct": round(allowed_drop, 2),
        }

    zero_power_frame = bool(
        raw_soc <= 0.1
        and abs(safe_float(live.get("PV_Power"), 0.0)) <= 1.0
        and abs(safe_float(live.get("Grid_Power"), 0.0)) <= 1.0
        and abs(safe_float(live.get("Home_Power"), 0.0)) <= 1.0
        and abs(safe_float(live.get("Battery_Power"), 0.0)) <= 1.0
    )
    reason = (
        "Unplausibler SoC-Sprung %.1f%% -> %.1f%% in %.0fs "
        "(Abfall %.1f Prozentpunkte, erlaubt %.1f); Live-Frame verworfen"
    ) % (last_valid, raw_soc, elapsed_s, drop_pct, allowed_drop)
    if zero_power_frame:
        reason += "; gleichzeitiger 0W-Frame erkannt"
    return {
        "invalid": True,
        "reason": reason,
        "raw_soc": round(raw_soc, 2),
        "last_valid_soc": round(last_valid, 2),
        "last_valid_soc_ts": last_ts,
        "drop_pct": round(drop_pct, 2),
        "allowed_drop_pct": round(allowed_drop, 2),
        "physical_drop_pct": round(physical_drop_pct, 2),
        "capacity_kwh": round(capacity_kwh, 2),
        "max_discharge_w": round(max_discharge_w, 1),
        "elapsed_s": round(elapsed_s, 1),
        "reserve_soc": round(reserve_soc, 2),
        "zero_power_frame": zero_power_frame,
    }


def storage_soc_jump_guard_decision(
    cfg: Dict[str, Any],
    plan: Dict[str, Any],
    guard: Dict[str, Any],
    max_charge_w: int,
    now_s: float,
) -> Dict[str, Any]:
    curve_soc, target_soc, target_ts = current_curve(plan, now_s)
    return {
        "decision": {
            "state": "live_soc_unrealistic_auto",
            "mode": MODE_AUTO,
            "val": max_charge_w,
            "priority": "data_guard",
            "reason": str(guard.get("reason") or "Unplausibler SoC-Sprung; E3DC autonom")[:220],
            "protected": False,
            "storage_req_w": 0,
            "budget_w": 0,
            "soc_jump_guard": guard,
        },
        "curve_soc": curve_soc,
        "target_soc": target_soc,
        "target_ts": target_ts,
        "i_fc_w": 0,
        "i_min_lade_w": 0,
    }


def atomic_write(path: str, payload: Dict[str, Any], *, indent: Optional[int] = None) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def _json_compare_without_noise(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _json_compare_without_noise(item)
            for key, item in value.items()
            if key not in _JSON_WRITE_NOISE_KEYS
        }
    if isinstance(value, list):
        return [_json_compare_without_noise(item) for item in value]
    return value


def _json_write_payload_changed(path: str, data: Dict[str, Any], force_interval_s: float = 60.0) -> bool:
    now = time.time()
    cache_key = os.path.abspath(path)
    compare_data = _json_compare_without_noise(data if isinstance(data, dict) else {})
    cached = _JSON_WRITE_CACHE.get(cache_key)
    if cached:
        last_ts = safe_float(cached.get("ts"), 0.0)
        if cached.get("compare") == compare_data and now - last_ts < max(0.0, force_interval_s):
            return False
    return True


def _remember_json_write_cache(
    path: str,
    data: Dict[str, Any],
    *,
    now_s: Optional[float] = None,
    update_read_cache: bool = True,
) -> None:
    cache_key = os.path.abspath(path)
    write_ts = float(now_s if now_s is not None else time.time())
    clean_data = data if isinstance(data, dict) else {}
    _JSON_WRITE_CACHE[cache_key] = {
        "compare": _json_compare_without_noise(clean_data),
        "ts": write_ts,
    }
    if not update_read_cache:
        return
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = write_ts
    _JSON_READ_CACHE[cache_key] = {"mtime": mtime, "data": _json_cache_copy(clean_data)}


def atomic_write_on_change(
    path: str,
    data: Dict[str, Any],
    force_interval_s: float = 60.0,
    *,
    indent: Optional[int] = None,
) -> bool:
    if not _json_write_payload_changed(path, data, force_interval_s):
        return False
    atomic_write(path, data, indent=indent)
    _remember_json_write_cache(path, data)
    return True


def _compact_text(value: Any, limit: int = 220) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def _compact_trace_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    keep = (
        "step",
        "action",
        "state",
        "mode",
        "val",
        "priority",
        "previous",
        "candidate",
        "age_s",
        "min_hold_s",
        "remaining_s",
        "held_mode",
        "held_val",
        "invalid",
        "reason",
    )
    compact: Dict[str, Any] = {}
    for key in keep:
        if key not in entry:
            continue
        value = entry[key]
        compact[key] = _compact_text(value, 180) if isinstance(value, str) else value
    return compact


def _compact_storage_trace(payload: Dict[str, Any], limit: int = 8) -> List[Dict[str, Any]]:
    shadow = payload.get("shadow_payload")
    if not isinstance(shadow, dict):
        return []
    trace = shadow.get("trace")
    if not isinstance(trace, list):
        return []
    result: List[Dict[str, Any]] = []
    for entry in trace[-limit:]:
        if isinstance(entry, dict):
            result.append(_compact_trace_entry(entry))
    return result


def storage_owner_contract(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Classify the storage decision owner without touching IO or RSCP output."""
    ts = safe_int(payload.get("ts"), int(time.time()))
    shadow = payload.get("shadow_payload") if isinstance(payload.get("shadow_payload"), dict) else {}
    shadow_inputs = shadow.get("inputs") if isinstance(shadow.get("inputs"), dict) else {}
    previous_state = shadow_inputs.get("previous_parallel_state", "")
    previous_age = shadow_inputs.get("previous_parallel_age_s")
    soc_now = safe_float(payload.get("soc"), 0.0)
    curve_soc = payload.get("curve_soc")
    curve_gap_pct = (
        round(soc_now - safe_float(curve_soc, 0.0), 2)
        if curve_soc is not None
        else None
    )
    bat_w = safe_int(payload.get("bat_w"), 0)
    grid_w = safe_int(payload.get("grid_w"), 0)
    wallbox_w = safe_float(payload.get("wallbox_w"), 0.0)
    auto_limit = payload.get("auto_limit") if isinstance(payload.get("auto_limit"), dict) else {}
    direct_monitor = payload.get("direct_marketing_monitor") if isinstance(payload.get("direct_marketing_monitor"), dict) else {}
    state_name = str(payload.get("state") or "")
    mode_value = safe_int(payload.get("mode"), MODE_AUTO)
    auto_limit_contract = storage_auto_limit_contract(
        auto_limit,
        state=state_name,
        mode=mode_value,
        reason=str(payload.get("display_reason") or payload.get("reason") or payload.get("priority") or ""),
        direct_marketing_active=bool(payload.get("direct_marketing_active")),
        direct_marketing_auto_limit_active=bool(payload.get("direct_marketing_pv_store_auto_limit_active")),
    )
    auto_limit_enabled = bool(auto_limit_contract.get("enabled"))
    active_charge_limit_w = safe_int(auto_limit_contract.get("max_charge_w"), 0) if auto_limit_enabled else None
    state_since_ts = payload.get("parallel_state_since_ts")
    try:
        state_age_s = max(0.0, float(ts) - float(state_since_ts)) if state_since_ts is not None else None
    except (TypeError, ValueError):
        state_age_s = None
    above_curve_active_charge = curve_gap_pct is not None and curve_gap_pct > 1.0 and bat_w > 100
    charge_limit_slack_w = max(300, int((active_charge_limit_w or 0) * 0.25))
    above_curve_soft_charge = (
        above_curve_active_charge
        and auto_limit_enabled
        and active_charge_limit_w is not None
        and bat_w <= active_charge_limit_w + charge_limit_slack_w
    )
    above_curve_unbounded_charge = above_curve_active_charge and not above_curve_soft_charge
    control_owner = "e3dc_auto"
    if state_name.startswith("direct_marketing_"):
        control_owner = "direct_marketing"
    elif state_name.startswith("market_"):
        control_owner = "market_economics"
    elif state_name.startswith("pre_discharge"):
        control_owner = "predump"
    elif auto_limit_enabled:
        control_owner = "ems_auto_limit"
    elif bool(auto_limit_contract.get("release")):
        control_owner = "ems_limit_release"
    elif mode_value != MODE_AUTO:
        control_owner = "rscp_mode"
    mode_name = str(
        payload.get("mode_name")
        or {
            MODE_AUTO: "AUTO",
            MODE_IDLE: "IDLE",
            MODE_DISCH: "DISCH",
            MODE_CHRG: "CHRG",
            MODE_GRID: "GRID",
        }.get(mode_value, "AUTO")
    ).upper()
    reason_text = str(payload.get("display_reason") or payload.get("reason") or payload.get("priority") or "").lower()
    if state_name.startswith("direct_marketing_") or control_owner == "direct_marketing":
        contract_owner = "MARKET_DIRECT"
    elif state_name.startswith("market_") or control_owner == "market_economics":
        contract_owner = "MARKET_PRICE"
    elif state_name.startswith("pre_discharge") or control_owner == "predump":
        contract_owner = "PREDUMP"
    elif (
        state_name in ("parallel_no_data", "parallel_passthrough", "parallel_emergency_auto")
        or bool(payload.get("protected"))
        or "notstrom" in reason_text
    ):
        contract_owner = "PROTECTION"
    elif mode_name == "GRID":
        contract_owner = "MARKET_PRICE" if ("price" in reason_text or "preis" in reason_text) else "STORAGE_ACTIVE"
    elif mode_name in ("CHRG", "DISCH", "IDLE"):
        if state_name.startswith("parallel_headroom") or "abregel" in reason_text:
            contract_owner = "PROTECTION"
        elif state_name.startswith("parallel_curve"):
            contract_owner = "CURVE"
        else:
            contract_owner = "STORAGE_ACTIVE"
    else:
        contract_owner = "E3DC_AUTONOM"
    if mode_name == "AUTO":
        if bool(auto_limit_contract.get("release")):
            execution_class = "AUTO_RELEASE"
        elif auto_limit_enabled:
            execution_class = "AUTO_LIMITED"
        else:
            execution_class = "AUTO_FREE"
    else:
        execution_class = mode_name
    state_reason = state_name.upper() if state_name else "UNKNOWN"
    if bool(auto_limit_contract.get("present")):
        value_signature = str(auto_limit_contract.get("value_signature") or "")
    else:
        value_signature = "mode:%s:%d" % (mode_name, max(0, safe_int(payload.get("val"), 0)))
    grid_import_with_battery_charge_raw = grid_w > 500 and bat_w > 100
    freilauf_settling_active = (
        state_name == "parallel_evening_release"
        and state_age_s is not None
        and state_age_s <= 15 * 60
    )
    e3dc_auto_no_limits = (
        control_owner == "e3dc_auto"
        and mode_value == MODE_AUTO
        and not auto_limit_enabled
        and active_charge_limit_w is None
    )
    house_load_transient = bool(
        grid_import_with_battery_charge_raw
        and e3dc_auto_no_limits
        and abs(wallbox_w) <= 100.0
        and state_name in ("parallel_auto", "parallel_grid_relief_auto", "parallel_evening_release")
    )
    grid_import_with_battery_charge = grid_import_with_battery_charge_raw and not house_load_transient
    diagnosis_class = "normal"
    if house_load_transient:
        diagnosis_class = "freilauf_settling" if freilauf_settling_active else "house_load_transient"
    elif above_curve_unbounded_charge or grid_import_with_battery_charge:
        diagnosis_class = "suspicious"
    return {
        "contract_version": STORAGE_OWNER_CONTRACT_VERSION,
        "ts": ts,
        "control_owner": control_owner,
        "contract_owner": contract_owner,
        "execution_class": execution_class,
        "state_reason": state_reason,
        "value_signature": value_signature,
        "diagnosis_class": diagnosis_class,
        "state_name": state_name,
        "mode_value": mode_value,
        "mode_name": mode_name,
        "curve_gap_pct": curve_gap_pct,
        "state_age_s": state_age_s,
        "previous_state": previous_state,
        "previous_age_s": previous_age,
        "active_charge_limit_w": active_charge_limit_w,
        "active_discharge_limit_w": safe_int(auto_limit_contract.get("max_discharge_w"), 0) if auto_limit_enabled else None,
        "auto_limit_enabled": auto_limit_enabled,
        "auto_limit_contract_version": safe_int(auto_limit_contract.get("contract_version"), 0),
        "auto_limit_active": bool(auto_limit_contract.get("active")),
        "auto_limit_command_class": auto_limit_contract.get("command_class"),
        "auto_limit_source_class": auto_limit_contract.get("source_class"),
        "auto_limit_value_signature": auto_limit_contract.get("value_signature") if bool(auto_limit_contract.get("present")) else None,
        "auto_limit_set_power_auto": bool(auto_limit_contract.get("set_power_auto")),
        "auto_limit_charge_blocked": bool(auto_limit_contract.get("charge_blocked")),
        "auto_limit_discharge_blocked": bool(auto_limit_contract.get("discharge_blocked")),
        "above_curve_active_charge": above_curve_active_charge,
        "above_curve_soft_charge": above_curve_soft_charge,
        "above_curve_unbounded_charge": above_curve_unbounded_charge,
        "above_curve_hold": curve_gap_pct is not None and curve_gap_pct > 1.0 and bat_w <= 50,
        "below_curve_discharge": curve_gap_pct is not None and curve_gap_pct < -2.0 and bat_w < -100,
        "grid_import_gt500": grid_w > 500,
        "grid_import_with_battery_charge_raw": grid_import_with_battery_charge_raw,
        "grid_import_with_battery_charge": grid_import_with_battery_charge,
        "house_load_transient": house_load_transient,
        "freilauf_settling_active": freilauf_settling_active,
        "charge_limit_slack_w": charge_limit_slack_w if auto_limit_enabled else None,
        "observed_battery_charge_w": max(0, bat_w),
        "observed_battery_discharge_w": max(0, -bat_w),
        "state_family": "curve" if state_name.startswith("parallel_curve") else ("wallbox" if state_name.startswith("parallel_wb") else "default"),
    }


def build_decision_history_record(payload: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    return build_decision_history_record_with_context(payload, plan, None)


def build_decision_history_record_with_context(
    payload: Dict[str, Any],
    plan: Dict[str, Any],
    budget_stability_previous: Optional[Dict[str, Any]] = None,
    budget_executor_latch_previous: Optional[Dict[str, Any]] = None,
    budget_executor_ack_previous: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    owner_contract = storage_owner_contract(payload)
    curve_contract = storage_curve_context_contract(payload, plan)
    path_contract = storage_decision_path_contract(payload, plan)
    runtime_suite = (
        payload.get("storage_budget_contracts")
        if isinstance(payload.get("storage_budget_contracts"), dict)
        else storage_budget_runtime_contract_suite(
            {},
            payload,
            plan,
            budget_stability_previous,
            budget_executor_latch_previous,
            budget_executor_ack_previous,
        )
    )
    budget_contract = runtime_suite.get("readiness") if isinstance(runtime_suite.get("readiness"), dict) else {}
    budget_arbitration_contract = (
        runtime_suite.get("arbitration") if isinstance(runtime_suite.get("arbitration"), dict) else {}
    )
    budget_stability_contract = (
        runtime_suite.get("stability") if isinstance(runtime_suite.get("stability"), dict) else {}
    )
    budget_executor_gate_contract = (
        runtime_suite.get("executor_gate") if isinstance(runtime_suite.get("executor_gate"), dict) else {}
    )
    budget_executor_latch_contract = (
        runtime_suite.get("executor_latch") if isinstance(runtime_suite.get("executor_latch"), dict) else {}
    )
    central_ack_contract = (
        runtime_suite.get("central_ack") if isinstance(runtime_suite.get("central_ack"), dict) else {}
    )
    budget_executor_ack_contract = (
        runtime_suite.get("executor_ack") if isinstance(runtime_suite.get("executor_ack"), dict) else {}
    )
    ems_budget_runtime = runtime_suite.get("runtime") if isinstance(runtime_suite.get("runtime"), dict) else {}
    ts = safe_int(owner_contract.get("ts"), int(time.time()))
    budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
    curve_gap_pct = owner_contract.get("curve_gap_pct")
    state_age_s = owner_contract.get("state_age_s")
    previous_state = owner_contract.get("previous_state", "")
    previous_age = owner_contract.get("previous_age_s")
    direct_monitor = payload.get("direct_marketing_monitor") if isinstance(payload.get("direct_marketing_monitor"), dict) else {}
    payload_export_execution = (
        payload.get("direct_marketing_export_execution")
        if isinstance(payload.get("direct_marketing_export_execution"), dict)
        else {}
    )
    monitor_export_execution = (
        direct_monitor.get("export_execution")
        if isinstance(direct_monitor.get("export_execution"), dict)
        else {}
    )
    direct_export_execution = (
        payload_export_execution
        if payload_export_execution.get("schema") == DIRECT_MARKETING_EXPORT_EXECUTION_SCHEMA
        else monitor_export_execution
    )
    rscp_power_settings = (
        payload.get("rscp_power_settings")
        if isinstance(payload.get("rscp_power_settings"), dict)
        else {}
    )
    path_subcontracts = path_contract.get("subcontracts") if isinstance(path_contract.get("subcontracts"), dict) else {}
    market_path_contract = (
        path_subcontracts.get("market_price") if isinstance(path_subcontracts.get("market_price"), dict) else {}
    )
    direct_marketing_path_contract = (
        path_subcontracts.get("direct_marketing")
        if isinstance(path_subcontracts.get("direct_marketing"), dict)
        else {}
    )
    predump_path_contract = path_subcontracts.get("predump") if isinstance(path_subcontracts.get("predump"), dict) else {}
    protection_path_contract = (
        path_subcontracts.get("protection") if isinstance(path_subcontracts.get("protection"), dict) else {}
    )
    return {
        "ts": ts,
        "time": datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds"),
        "service": "storage_manager",
        # Keep the full execution evidence in the latest/history envelope. The
        # compact decision projection alone is insufficient for offline D9/D10
        # diagnostics and must not turn a missing field into a false claim.
        "direct_marketing_monitor": direct_monitor,
        "direct_marketing_export_execution": direct_export_execution,
        "rscp_power_settings": rscp_power_settings,
        "decision": {
            "state": payload.get("state"),
            "label": payload.get("state_label", payload.get("state")),
            "reason": _compact_text(payload.get("display_reason") or payload.get("reason"), 260),
            "priority": payload.get("priority"),
            "protected": bool(payload.get("protected")),
            "mode": safe_int(payload.get("mode"), MODE_AUTO),
            "mode_name": payload.get("mode_name"),
            "val_w": max(0, safe_int(payload.get("val"), 0)),
            "wbminsoc_pv_charge_active": bool(payload.get("wbminsoc_pv_charge_active")),
            "scheduled_grid_charge": bool(payload.get("scheduled_grid_charge")),
        },
        "inputs": {
            "soc": round(safe_float(payload.get("soc"), 0.0), 2),
            "pv_w": safe_int(payload.get("pv_w"), 0),
            "grid_w": safe_int(payload.get("grid_w"), 0),
            "home_w": safe_int(payload.get("home_w"), 0),
            "wp_w": safe_int(payload.get("wp_w"), 0),
            "bat_w": safe_int(payload.get("bat_w"), 0),
            "wallbox_w": round(safe_float(payload.get("wallbox_w"), 0.0), 1),
            "wallbox_power_source": payload.get("wallbox_power_source", ""),
            "live_age_s": round(safe_float(payload.get("live_age_s"), 0.0), 1),
            "live_stale": bool(payload.get("live_stale")),
            "live_sample_valid": bool(payload.get("live_sample_valid", True)),
            "home_power_valid": bool(payload.get("home_power_valid", True)),
            "grid_power_valid": bool(payload.get("grid_power_valid", True)),
            "home_power_source": payload.get("home_power_source", ""),
            "home_power_balance_w": safe_int(payload.get("home_power_balance_w"), 0),
            "home_power_delta_w": safe_int(payload.get("home_power_delta_w"), 0),
        },
        "curve": {
            "contract_version": safe_int(curve_contract.get("contract_version"), 0),
            "soc_now": payload.get("curve_soc"),
            "target_soc": payload.get("target_soc"),
            "target_ts": payload.get("target_ts"),
            "gap_pct": curve_gap_pct,
            "position": curve_contract.get("curve_position"),
            "target_position": curve_contract.get("target_position"),
            "action_class": curve_contract.get("action_class"),
            "ladeende": safe_float(plan.get("planning_target_soc", plan.get("target_soc")), 95.0),
        },
        "limits": {
            "max_charge_w": safe_int(payload.get("max_charge_w"), 0),
            "max_discharge_w": safe_int(payload.get("max_discharge_w"), 0),
            "ep_reserve_pct": round(safe_float(payload.get("ep_reserve_pct"), 0.0), 1),
            "auto_limit": payload.get("auto_limit"),
        },
        "wallbox": {
            "car_present": bool(payload.get("wb_car_present")),
            "possible_power_w": safe_int(payload.get("wb_possible_power_w"), 0),
            "phase_transition_active": bool(payload.get("wallbox_phase_transition_active")),
            "phase_transition_reserved_w": safe_int(payload.get("wallbox_phase_transition_reserved_w"), 0),
            "phase_transition_target_phases": safe_int(payload.get("wallbox_phase_transition_target_phases"), 0),
            "phase_transition_until_ts": safe_float(payload.get("wallbox_phase_transition_until_ts"), 0.0),
            "budget_w": safe_int(budget.get("budget_w"), 0),
            "raw_iAVal_w": safe_int(budget.get("raw_iAVal_w"), 0),
            "iAVal_w": safe_int(budget.get("iAVal_w"), 0),
            "budget_amp_1ph": safe_int(budget.get("budget_amp_1ph"), 0),
            "budget_amp_3ph": safe_int(budget.get("budget_amp_3ph"), 0),
            "physical_chargeable": budget.get("physical_chargeable"),
            "physical_reason": _compact_text(budget.get("physical_reason"), 180),
            "planned_grid_pv_charge_w": safe_int(payload.get("planned_grid_pv_charge_w"), 0),
            "planned_grid_pv_surplus_w": safe_int(payload.get("planned_grid_pv_surplus_w"), 0),
            "pv_house_surplus_w": safe_int(payload.get("pv_house_surplus_w"), 0),
        },
        "storage_budget": {
            "contract_version": safe_int(budget_contract.get("contract_version"), 0),
            "readiness_class": budget_contract.get("readiness_class"),
            "balance_class": budget_contract.get("balance_class"),
            "data_valid": bool(budget_contract.get("data_valid")),
            "blockers": budget_contract.get("blockers") if isinstance(budget_contract.get("blockers"), list) else [],
            "iFc_w": safe_int(payload.get("iFc_w"), 0),
            "iMinLade_w": safe_int(payload.get("iMinLade_w"), 0),
            "abregel_charge_req_w": safe_int(payload.get("abregel_charge_req_w"), 0),
            "abregel_target_w": safe_int(payload.get("abregel_target_w"), 0),
            "abregel_release_w": safe_int(payload.get("abregel_release_w"), 0),
            "storage_req_w": safe_int(budget.get("energy_score", {}).get("bat_charge_request_w"), 0)
            if isinstance(budget.get("energy_score"), dict)
            else 0,
            "free_for_consumers_w": safe_int(
                budget.get("energy_score", {}).get("free_for_limbs_w"),
                safe_int(budget.get("budget_w"), 0),
            )
            if isinstance(budget.get("energy_score"), dict)
            else safe_int(budget.get("budget_w"), 0),
            "min_consumer_w": safe_int(budget_contract.get("min_consumer_w"), 0),
            "flexible_consumer_budget_w": safe_int(
                payload.get("wallbox_phase_transition_flexible_budget_w"),
                safe_int(budget_contract.get("free_for_consumers_w"), 0),
            ),
            "consumer_shortfall_w": safe_int(budget_contract.get("consumer_shortfall_w"), 0),
            "grid_export_w": safe_int(budget_contract.get("grid_export_w"), 0),
            "grid_import_w": safe_int(budget_contract.get("grid_import_w"), 0),
            "storage_reserved_w": safe_int(budget_contract.get("storage_reserved_w"), 0),
            "arbitration": budget_arbitration_contract,
            "stability": budget_stability_contract,
            "executor_gate": budget_executor_gate_contract,
            "executor_latch": budget_executor_latch_contract,
            "central_ack": central_ack_contract,
            "executor_ack": budget_executor_ack_contract,
            "runtime": ems_budget_runtime,
        },
        "transition": {
            "previous_state": previous_state,
            "previous_age_s": previous_age,
            "state_since_ts": payload.get("parallel_state_since_ts"),
            "state_age_s": round(state_age_s, 1) if state_age_s is not None else None,
            "last_auto_ts": payload.get("last_auto_ts"),
        },
        "path": {
            "contract_version": safe_int(path_contract.get("contract_version"), 0),
            "primary_path": path_contract.get("primary_path"),
            "active_paths": path_contract.get("active_paths"),
            "subordinate_paths": path_contract.get("subordinate_paths"),
            "subcontracts": path_subcontracts,
            "path_conflict": bool(path_contract.get("path_conflict")),
            "veto_required": bool(path_contract.get("veto_required")),
            "veto_reasons": path_contract.get("veto_reasons") if isinstance(path_contract.get("veto_reasons"), list) else [],
            "owner_control": path_contract.get("owner_control"),
            "owner_contract": path_contract.get("owner_contract"),
            "owner_binding": path_contract.get("owner_binding"),
            "raw_owner_control": path_contract.get("raw_owner_control"),
            "raw_owner_contract": path_contract.get("raw_owner_contract"),
        },
        "r5": {
            "storage_owner_contract_version": safe_int(owner_contract.get("contract_version"), 0),
            "storage_decision_path_contract_version": safe_int(path_contract.get("contract_version"), 0),
            "auto_limit_contract_version": safe_int(owner_contract.get("auto_limit_contract_version"), 0),
            "control_owner": path_contract.get("owner_control", owner_contract.get("control_owner")),
            "contract_owner": path_contract.get("owner_contract", owner_contract.get("contract_owner")),
            "owner_binding": path_contract.get("owner_binding"),
            "raw_control_owner": owner_contract.get("control_owner"),
            "raw_contract_owner": owner_contract.get("contract_owner"),
            "execution_class": owner_contract.get("execution_class"),
            "state_reason": owner_contract.get("state_reason"),
            "value_signature": owner_contract.get("value_signature"),
            "diagnosis_class": owner_contract.get("diagnosis_class"),
            "decision_primary_path": path_contract.get("primary_path"),
            "decision_active_paths": path_contract.get("active_paths"),
            "decision_subordinate_paths": path_contract.get("subordinate_paths"),
            "decision_path_conflict": bool(path_contract.get("path_conflict")),
            "decision_veto_required": bool(path_contract.get("veto_required")),
            "decision_veto_reasons": path_contract.get("veto_reasons") if isinstance(path_contract.get("veto_reasons"), list) else [],
            "auto_limit_active": bool(owner_contract.get("auto_limit_active")),
            "auto_limit_command_class": owner_contract.get("auto_limit_command_class"),
            "auto_limit_source_class": owner_contract.get("auto_limit_source_class"),
            "auto_limit_value_signature": owner_contract.get("auto_limit_value_signature"),
            "auto_limit_set_power_auto": bool(owner_contract.get("auto_limit_set_power_auto")),
            "auto_limit_charge_blocked": bool(owner_contract.get("auto_limit_charge_blocked")),
            "auto_limit_discharge_blocked": bool(owner_contract.get("auto_limit_discharge_blocked")),
            "curve_context_contract_version": safe_int(curve_contract.get("contract_version"), 0),
            "curve_position": curve_contract.get("curve_position"),
            "curve_target_position": curve_contract.get("target_position"),
            "curve_action_class": curve_contract.get("action_class"),
            "curve_target_gap_pct": curve_contract.get("target_gap_pct"),
            "curve_auto_limit_source_class": curve_contract.get("auto_limit_source_class"),
            "curve_auto_limit_charge_w": curve_contract.get("auto_limit_charge_w"),
            "budget_readiness_contract_version": safe_int(budget_contract.get("contract_version"), 0),
            "budget_readiness_class": budget_contract.get("readiness_class"),
            "budget_balance_class": budget_contract.get("balance_class"),
            "budget_data_valid": bool(budget_contract.get("data_valid")),
            "budget_blockers": budget_contract.get("blockers") if isinstance(budget_contract.get("blockers"), list) else [],
            "budget_free_for_consumers_w": safe_int(budget_contract.get("free_for_consumers_w"), 0),
            "budget_storage_reserved_w": safe_int(budget_contract.get("storage_reserved_w"), 0),
            "budget_min_consumer_w": safe_int(budget_contract.get("min_consumer_w"), 0),
            "budget_consumer_shortfall_w": safe_int(budget_contract.get("consumer_shortfall_w"), 0),
            "budget_grid_export_w": safe_int(budget_contract.get("grid_export_w"), 0),
            "budget_grid_import_w": safe_int(budget_contract.get("grid_import_w"), 0),
            "budget_arbitration_contract_version": safe_int(
                budget_arbitration_contract.get("contract_version"), 0
            ),
            "budget_arbitration_shadow_only": bool(budget_arbitration_contract.get("shadow_only")),
            "budget_arbitration_class": budget_arbitration_contract.get("arbitration_class"),
            "budget_arbitration_primary_sink": budget_arbitration_contract.get("primary_sink"),
            "budget_arbitration_reserved_sink": budget_arbitration_contract.get("reserved_sink"),
            "budget_arbitration_wallbox_w": safe_int(
                budget_arbitration_contract.get("allocations", {}).get("wallbox_w")
                if isinstance(budget_arbitration_contract.get("allocations"), dict)
                else 0,
                0,
            ),
            "budget_arbitration_heatpump_w": safe_int(
                budget_arbitration_contract.get("allocations", {}).get("heatpump_w")
                if isinstance(budget_arbitration_contract.get("allocations"), dict)
                else 0,
                0,
            ),
            "budget_arbitration_export_w": safe_int(
                budget_arbitration_contract.get("allocations", {}).get("export_w")
                if isinstance(budget_arbitration_contract.get("allocations"), dict)
                else 0,
                0,
            ),
            "budget_arbitration_direct_marketing_export_w": safe_int(
                budget_arbitration_contract.get("allocations", {}).get("direct_marketing_export_w")
                if isinstance(budget_arbitration_contract.get("allocations"), dict)
                else 0,
                0,
            ),
            "budget_arbitration_blocked_candidates": budget_arbitration_contract.get("blocked_candidates")
            if isinstance(budget_arbitration_contract.get("blocked_candidates"), list)
            else [],
            "budget_stability_contract_version": safe_int(budget_stability_contract.get("contract_version"), 0),
            "budget_stability_shadow_only": bool(budget_stability_contract.get("shadow_only")),
            "budget_stability_class": budget_stability_contract.get("stability_class"),
            "budget_stability_action": budget_stability_contract.get("action"),
            "budget_stability_requested_sink": budget_stability_contract.get("requested_primary_sink"),
            "budget_stability_stable_sink": budget_stability_contract.get("stable_primary_sink"),
            "budget_stability_previous_sink": budget_stability_contract.get("previous_primary_sink"),
            "budget_stability_hold_remaining_s": safe_float(
                budget_stability_contract.get("hold_remaining_s"), 0.0
            ),
            "budget_stability_blocked_reason": budget_stability_contract.get("blocked_reason"),
            "budget_executor_gate_contract_version": safe_int(
                budget_executor_gate_contract.get("contract_version"), 0
            ),
            "budget_executor_gate_shadow_only": bool(budget_executor_gate_contract.get("shadow_only")),
            "budget_executor_gate_class": budget_executor_gate_contract.get("gate_class"),
            "budget_executor_gate_open_shadow": bool(budget_executor_gate_contract.get("gate_open_shadow")),
            "budget_executor_gate_target_sink": budget_executor_gate_contract.get("target_sink"),
            "budget_executor_gate_target_w": safe_int(budget_executor_gate_contract.get("target_w"), 0),
            "budget_executor_gate_blockers": budget_executor_gate_contract.get("blockers")
            if isinstance(budget_executor_gate_contract.get("blockers"), list)
            else [],
            "budget_executor_latch_contract_version": safe_int(
                budget_executor_latch_contract.get("contract_version"), 0
            ),
            "budget_executor_latch_shadow_only": bool(budget_executor_latch_contract.get("shadow_only")),
            "budget_executor_latch_class": budget_executor_latch_contract.get("latch_class"),
            "budget_executor_latch_action": budget_executor_latch_contract.get("action"),
            "budget_executor_latch_active_shadow": bool(
                budget_executor_latch_contract.get("accepted_active_shadow")
            ),
            "budget_executor_latch_sink": budget_executor_latch_contract.get("accepted_sink"),
            "budget_executor_latch_target_w": safe_int(
                budget_executor_latch_contract.get("accepted_target_w"), 0
            ),
            "budget_executor_latch_age_s": safe_float(
                budget_executor_latch_contract.get("accepted_age_s"), 0.0
            ),
            "budget_executor_latch_min_runtime_s": safe_int(
                budget_executor_latch_contract.get("min_runtime_s"), 0
            ),
            "budget_executor_latch_hold_remaining_s": safe_float(
                budget_executor_latch_contract.get("hold_remaining_s"), 0.0
            ),
            "budget_executor_latch_release_allowed_shadow": bool(
                budget_executor_latch_contract.get("release_allowed_shadow")
            ),
            "budget_executor_latch_blockers": budget_executor_latch_contract.get("blockers")
            if isinstance(budget_executor_latch_contract.get("blockers"), list)
            else [],
            "budget_executor_central_ack_contract_version": safe_int(
                central_ack_contract.get("contract_version"), 0
            ),
            "budget_executor_central_ack_class": central_ack_contract.get("ack_class"),
            "budget_executor_central_ack_emitted": bool(central_ack_contract.get("ack_emitted")),
            "budget_executor_central_ack_source": central_ack_contract.get("source"),
            "budget_executor_central_ack_blockers": central_ack_contract.get("blockers")
            if isinstance(central_ack_contract.get("blockers"), list)
            else [],
            "budget_executor_ack_contract_version": safe_int(
                budget_executor_ack_contract.get("contract_version"), 0
            ),
            "budget_executor_ack_shadow_only": bool(budget_executor_ack_contract.get("shadow_only")),
            "budget_executor_ack_class": budget_executor_ack_contract.get("ack_class"),
            "budget_executor_ack_required_shadow": bool(
                budget_executor_ack_contract.get("ack_required_shadow")
            ),
            "budget_executor_ack_valid_shadow": bool(
                budget_executor_ack_contract.get("ack_valid_shadow")
            ),
            "budget_executor_ack_source": budget_executor_ack_contract.get("ack_source"),
            "budget_executor_ack_expected_source": budget_executor_ack_contract.get("expected_ack_source"),
            "budget_executor_ack_productive_allowed_shadow": bool(
                budget_executor_ack_contract.get("productive_allowed_shadow")
            ),
            "budget_executor_ack_release_latch_shadow": bool(
                budget_executor_ack_contract.get("release_latch_shadow")
            ),
            "budget_executor_ack_fallback_action": budget_executor_ack_contract.get("fallback_action"),
            "budget_executor_ack_blockers": budget_executor_ack_contract.get("blockers")
            if isinstance(budget_executor_ack_contract.get("blockers"), list)
            else [],
            "ems_budget_runtime_contract_version": safe_int(ems_budget_runtime.get("contract_version"), 0),
            "ems_budget_runtime_enabled": bool(ems_budget_runtime.get("enabled")),
            "ems_budget_runtime_active": bool(ems_budget_runtime.get("active")),
            "ems_budget_runtime_class": ems_budget_runtime.get("runtime_class"),
            "ems_budget_runtime_sink": ems_budget_runtime.get("accepted_sink"),
            "ems_budget_runtime_target_w": safe_int(ems_budget_runtime.get("accepted_target_w"), 0),
            "ems_budget_runtime_wallbox_w": safe_int(ems_budget_runtime.get("wallbox_budget_w"), 0),
            "ems_budget_runtime_heatpump_w": safe_int(ems_budget_runtime.get("heatpump_budget_w"), 0),
            "ems_budget_runtime_safe_fallback": bool(ems_budget_runtime.get("safe_fallback")),
            "ems_budget_runtime_blockers": ems_budget_runtime.get("blockers")
            if isinstance(ems_budget_runtime.get("blockers"), list)
            else [],
            "market_path_contract_version": safe_int(market_path_contract.get("contract_version"), 0),
            "market_path_active": bool(market_path_contract.get("active")),
            "market_path_action": market_path_contract.get("action"),
            "market_path_commands_allowed": market_path_contract.get("commands_allowed"),
            "market_path_shadow": bool(market_path_contract.get("shadow")),
            "market_path_veto_reasons": market_path_contract.get("veto_reasons")
            if isinstance(market_path_contract.get("veto_reasons"), list)
            else [],
            "direct_marketing_path_contract_version": safe_int(direct_marketing_path_contract.get("contract_version"), 0),
            "direct_marketing_path_active": bool(direct_marketing_path_contract.get("active")),
            "direct_marketing_path_action": direct_marketing_path_contract.get("action"),
            "direct_marketing_path_commands_allowed": direct_marketing_path_contract.get("commands_allowed"),
            "direct_marketing_path_shadow": bool(direct_marketing_path_contract.get("shadow")),
            "direct_marketing_path_veto_reasons": direct_marketing_path_contract.get("veto_reasons")
            if isinstance(direct_marketing_path_contract.get("veto_reasons"), list)
            else [],
            "predump_path_contract_version": safe_int(predump_path_contract.get("contract_version"), 0),
            "predump_path_active": bool(predump_path_contract.get("active")),
            "predump_path_action": predump_path_contract.get("action"),
            "predump_path_grid_fallback": bool(predump_path_contract.get("grid_fallback")),
            "predump_path_veto_reasons": predump_path_contract.get("veto_reasons")
            if isinstance(predump_path_contract.get("veto_reasons"), list)
            else [],
            "protection_path_contract_version": safe_int(protection_path_contract.get("contract_version"), 0),
            "protection_path_active": bool(protection_path_contract.get("active")),
            "protection_path_class": protection_path_contract.get("protection_class"),
            "protection_path_veto_reasons": protection_path_contract.get("veto_reasons")
            if isinstance(protection_path_contract.get("veto_reasons"), list)
            else [],
            "direct_marketing_active": bool(payload.get("direct_marketing_active")),
            "direct_marketing_mode": payload.get("direct_marketing_mode"),
            "direct_marketing_action": payload.get("direct_marketing_action"),
            "direct_marketing_owner": payload.get("direct_marketing_owner"),
            "direct_marketing_monitor_state": direct_monitor.get("state"),
            "direct_marketing_commands_allowed": direct_monitor.get("commands_allowed"),
            "direct_marketing_shadow": direct_monitor.get("shadow"),
            "direct_marketing_expected_profit_ct_per_kwh": direct_monitor.get("expected_profit_ct_per_kwh"),
            "direct_marketing_blocked_reasons": direct_monitor.get("blocked_reasons") if isinstance(direct_monitor.get("blocked_reasons"), list) else [],
            "market_economics_active": bool(payload.get("market_economics_active")),
            "market_economics_action": payload.get("market_economics_action"),
            "market_economics_owner": payload.get("market_economics_owner"),
            "market_economics_contract_version": payload.get("market_economics_contract_version"),
            "market_economics_commands_allowed": payload.get("market_economics_commands_allowed"),
            "market_economics_shadow": payload.get("market_economics_shadow"),
            "market_economics_reason": payload.get("market_economics_reason"),
            "market_economics_blocked_reasons": payload.get("market_economics_blocked_reasons") if isinstance(payload.get("market_economics_blocked_reasons"), list) else [],
            "market_economics_dwell_active": bool(payload.get("market_economics_dwell_active")),
            "market_economics_dwell_remaining_s": safe_float(payload.get("market_economics_dwell_remaining_s"), 0.0),
            "active_charge_limit_w": owner_contract.get("active_charge_limit_w"),
            "active_discharge_limit_w": owner_contract.get("active_discharge_limit_w"),
            "observed_battery_charge_w": owner_contract.get("observed_battery_charge_w"),
            "observed_battery_discharge_w": owner_contract.get("observed_battery_discharge_w"),
            "curve_gap_abs_pct": abs(curve_gap_pct) if curve_gap_pct is not None else None,
            "above_curve": curve_gap_pct is not None and curve_gap_pct > 0.0,
            "below_curve": curve_gap_pct is not None and curve_gap_pct < 0.0,
            "above_curve_active_charge": owner_contract.get("above_curve_active_charge"),
            "above_curve_soft_charge": owner_contract.get("above_curve_soft_charge"),
            "above_curve_unbounded_charge": owner_contract.get("above_curve_unbounded_charge"),
            "above_curve_hold": owner_contract.get("above_curve_hold"),
            "below_curve_discharge": owner_contract.get("below_curve_discharge"),
            "grid_import_gt500": owner_contract.get("grid_import_gt500"),
            "grid_import_with_battery_charge_raw": owner_contract.get("grid_import_with_battery_charge_raw"),
            "grid_import_with_battery_charge": owner_contract.get("grid_import_with_battery_charge"),
            "house_load_transient": owner_contract.get("house_load_transient"),
            "freilauf_settling_active": owner_contract.get("freilauf_settling_active"),
            "charge_limit_slack_w": owner_contract.get("charge_limit_slack_w"),
            "live_stale": bool(payload.get("live_stale")),
            "state_family": owner_contract.get("state_family"),
        },
        "direct_marketing": direct_monitor or None,
        "trace": _compact_storage_trace(payload),
    }


def _cleanup_decision_history(retention_days: int) -> None:
    global _decision_history_state
    today = datetime.date.today().isoformat()
    if _decision_history_state.get("legacy_cleanup_day") == today:
        return
    _decision_history_state["legacy_cleanup_day"] = today
    cutoff = time.time() - max(1, retention_days) * 86400
    try:
        for name in os.listdir(LOG_DIR):
            if not name.startswith(DECISION_HISTORY_PREFIX):
                continue
            if not (name.endswith(".jsonl") or name.endswith(".jsonl.gz")):
                continue
            path = os.path.join(LOG_DIR, name)
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except Exception as exc:
        log.debug("Decision-History Cleanup uebersprungen: %s", exc)


def _update_budget_runtime_shadow_states_from_suite(runtime_suite: Dict[str, Any]) -> None:
    global _budget_stability_shadow_state, _budget_executor_latch_shadow_state, _budget_executor_ack_shadow_state
    if not isinstance(runtime_suite, dict):
        return
    stability = runtime_suite.get("stability") if isinstance(runtime_suite.get("stability"), dict) else {}
    next_state = stability.get("next_state") if isinstance(stability.get("next_state"), dict) else {}
    if next_state:
        _budget_stability_shadow_state = dict(next_state)
    latch = runtime_suite.get("executor_latch") if isinstance(runtime_suite.get("executor_latch"), dict) else {}
    latch_next_state = latch.get("next_state") if isinstance(latch.get("next_state"), dict) else {}
    if latch_next_state:
        _budget_executor_latch_shadow_state = dict(latch_next_state)
    ack = runtime_suite.get("executor_ack") if isinstance(runtime_suite.get("executor_ack"), dict) else {}
    ack_next_state = ack.get("next_state") if isinstance(ack.get("next_state"), dict) else {}
    if ack_next_state:
        _budget_executor_ack_shadow_state = dict(ack_next_state)


def _refresh_decision_history_shadow_states(payload: Dict[str, Any]) -> bool:
    runtime_suite = payload.get("storage_budget_contracts") if isinstance(payload.get("storage_budget_contracts"), dict) else {}
    if not runtime_suite:
        return False
    _update_budget_runtime_shadow_states_from_suite(runtime_suite)
    return True


def _history_event_write_due(
    payload: Dict[str, Any],
    state: Dict[str, Any],
    *,
    now_s: Optional[float] = None,
    force_interval_s: float = _HISTORY_EVENT_FORCE_INTERVAL_S,
    power_delta_w: int = _HISTORY_EVENT_POWER_DELTA_W,
) -> bool:
    now_value = float(now_s if now_s is not None else time.time())
    mode_value = str((payload or {}).get("mode"))
    val_w = safe_int((payload or {}).get("val"), 0)
    rscp_event = _rscp_power_settings_event_signature(payload)
    if "last_ts" not in state:
        return True
    if state.get("mode") != mode_value:
        return True
    if abs(val_w - safe_int(state.get("val"), 0)) > max(0, int(power_delta_w)):
        return True
    if state.get("rscp_power_settings_event") != rscp_event:
        return True
    return now_value - safe_float(state.get("last_ts"), 0.0) >= max(0.0, force_interval_s)


def _remember_history_event_write(payload: Dict[str, Any], state: Dict[str, Any], *, now_s: Optional[float] = None) -> None:
    state["last_ts"] = float(now_s if now_s is not None else time.time())
    state["mode"] = str((payload or {}).get("mode"))
    state["val"] = safe_int((payload or {}).get("val"), 0)
    state["rscp_power_settings_event"] = _rscp_power_settings_event_signature(payload)


def _rscp_power_settings_event_signature(payload: Dict[str, Any]) -> Tuple[Any, ...]:
    diag = payload.get("rscp_power_settings") if isinstance(payload, dict) else None
    if not isinstance(diag, dict):
        return ()
    requested = diag.get("requested") if isinstance(diag.get("requested"), dict) else {}
    readback = diag.get("readback") if isinstance(diag.get("readback"), dict) else {}
    return (
        diag.get("status"),
        diag.get("stage"),
        diag.get("confirmed"),
        diag.get("acknowledged"),
        requested.get("limits_used"),
        requested.get("max_charge_w"),
        requested.get("max_discharge_w"),
        requested.get("discharge_start_w"),
        readback.get("limits_used"),
        readback.get("max_charge_w"),
        readback.get("max_discharge_w"),
        readback.get("discharge_start_w"),
    )


def write_decision_history(payload: Dict[str, Any], plan: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    global _budget_stability_shadow_state, _budget_executor_latch_shadow_state, _budget_executor_ack_shadow_state
    runtime_suite = payload.get("storage_budget_contracts") if isinstance(payload.get("storage_budget_contracts"), dict) else {}
    _refresh_decision_history_shadow_states(payload)
    if not cfg_bool(cfg, "storage_decision_history_enable", True):
        return
    record = build_decision_history_record_with_context(
        payload,
        plan,
        _budget_stability_shadow_state,
        _budget_executor_latch_shadow_state,
        _budget_executor_ack_shadow_state,
    )
    if not runtime_suite:
        record_budget = record.get("storage_budget") if isinstance(record.get("storage_budget"), dict) else {}
        _update_budget_runtime_shadow_states_from_suite(
            {
                "stability": record_budget.get("stability"),
                "executor_latch": record_budget.get("executor_latch"),
                "executor_ack": record_budget.get("executor_ack"),
            }
        )
    write_history_record(
        record,
        config=cfg,
        log_dir=LOG_DIR,
        latest_path=DECISION_LATEST_F,
        prefix=DECISION_HISTORY_PREFIX,
        enable_key="storage_decision_history_enable",
        max_bytes_key="storage_decision_history_max_bytes",
        retention_key="storage_decision_history_retention_days",
        interval_key="storage_decision_history_interval_s",
        state=_decision_history_state,
        signature_paths=(
            "decision.state",
            "decision.priority",
            "decision.protected",
            "decision.mode",
            "decision.mode_name",
            "limits.auto_limit.enabled",
            "limits.auto_limit.release",
            "r5.control_owner",
            "r5.diagnosis_class",
            "r5.above_curve_soft_charge",
            "r5.above_curve_unbounded_charge",
            "r5.grid_import_gt500",
            "r5.grid_import_with_battery_charge",
            "r5.house_load_transient",
            "r5.freilauf_settling_active",
            "r5.direct_marketing_monitor_state",
            "r5.direct_marketing_commands_allowed",
            "r5.direct_marketing_expected_profit_ct_per_kwh",
            "r5.market_economics_action",
            "r5.market_economics_commands_allowed",
            "r5.market_economics_shadow",
            "r5.market_economics_reason",
            "r5.market_economics_dwell_active",
            "r5.budget_readiness_class",
            "r5.budget_arbitration_class",
            "r5.budget_arbitration_primary_sink",
            "r5.budget_arbitration_reserved_sink",
            "r5.budget_arbitration_wallbox_w",
            "r5.budget_arbitration_heatpump_w",
            "r5.budget_arbitration_export_w",
            "r5.budget_arbitration_direct_marketing_export_w",
            "r5.budget_stability_stable_sink",
            "r5.budget_stability_blocked_reason",
            "r5.budget_executor_gate_class",
            "r5.budget_executor_gate_open_shadow",
            "r5.budget_executor_gate_target_sink",
            "r5.budget_executor_gate_blockers",
            "r5.budget_executor_latch_class",
            "r5.budget_executor_latch_action",
            "r5.budget_executor_latch_active_shadow",
            "r5.budget_executor_latch_sink",
            "r5.budget_executor_latch_release_allowed_shadow",
            "r5.budget_executor_latch_blockers",
            "r5.budget_executor_central_ack_class",
            "r5.budget_executor_central_ack_emitted",
            "r5.budget_executor_central_ack_blockers",
            "r5.budget_executor_ack_class",
            "r5.budget_executor_ack_required_shadow",
            "r5.budget_executor_ack_valid_shadow",
            "r5.budget_executor_ack_productive_allowed_shadow",
            "r5.budget_executor_ack_fallback_action",
            "r5.budget_executor_ack_blockers",
            "rscp_power_settings.status",
            "rscp_power_settings.stage",
            "rscp_power_settings.confirmed",
            "rscp_power_settings.acknowledged",
            "rscp_power_settings.requested.limits_used",
            "rscp_power_settings.requested.max_charge_w",
            "rscp_power_settings.requested.max_discharge_w",
            "rscp_power_settings.requested.discharge_start_w",
            "rscp_power_settings.readback.limits_used",
            "rscp_power_settings.readback.max_charge_w",
            "rscp_power_settings.readback.max_discharge_w",
            "rscp_power_settings.readback.discharge_start_w",
        ),
        default_interval_s=60,
        default_max_bytes=8 * 1024 * 1024,
        default_retention_days=2,
        logger=log,
    )


def _ems_reaction_target_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    auto_limit = payload.get("auto_limit") if isinstance(payload.get("auto_limit"), dict) else {}
    max_charge_w = safe_int(payload.get("max_charge_w"), 0)
    max_discharge_w = safe_int(payload.get("max_discharge_w"), 0)
    if bool(auto_limit.get("enabled")) and not bool(auto_limit.get("release")):
        return {
            "active": True,
            "kind": "limit",
            "limits_used": True,
            "max_charge_w": max(0, safe_int(auto_limit.get("max_charge_w"), max_charge_w)),
            "max_discharge_w": max(0, safe_int(auto_limit.get("max_discharge_w"), max_discharge_w)),
            "discharge_start_w": max(0, safe_int(auto_limit.get("discharge_start_w"), 0)),
            "reason": str(auto_limit.get("reason") or payload.get("reason") or ""),
        }
    if bool(auto_limit.get("release")):
        return {
            "active": True,
            "kind": "release",
            "limits_used": False,
            "max_charge_w": max(0, safe_int(auto_limit.get("max_charge_w"), max_charge_w)),
            "max_discharge_w": max(0, safe_int(auto_limit.get("max_discharge_w"), max_discharge_w)),
            "discharge_start_w": max(0, safe_int(auto_limit.get("discharge_start_w"), 0)),
            "reason": str(auto_limit.get("reason") or payload.get("reason") or ""),
        }
    return {
        "active": False,
        "kind": "none",
        "limits_used": False,
        "max_charge_w": max_charge_w,
        "max_discharge_w": max_discharge_w,
        "discharge_start_w": 0,
        "reason": "Keine aktive EMS-Ladevorgabe aus dem Storage Manager",
    }


def _ems_reaction_signature(target: Dict[str, Any]) -> str:
    def _bucket(key: str) -> Any:
        value = target.get(key)
        if key.endswith("_w"):
            return int(round(safe_int(value, 0) / 50.0) * 50)
        return value

    return "|".join(
        str(_bucket(key))
        for key in ("kind", "limits_used", "max_charge_w", "max_discharge_w", "discharge_start_w")
    )


def _payload_optional_int(payload: Dict[str, Any], key: str) -> Optional[int]:
    if key not in payload or payload.get(key) is None:
        return None
    return safe_int(payload.get(key), 0)


def _ems_reaction_sample(payload: Dict[str, Any]) -> Dict[str, Any]:
    bat_w = safe_int(payload.get("bat_w"), 0)
    grid_w = safe_int(payload.get("grid_w"), 0)
    return {
        "bat_w": bat_w,
        "battery_charge_w": max(0, bat_w),
        "battery_discharge_w": max(0, -bat_w),
        "grid_w": grid_w,
        "grid_export_w": max(0, -grid_w),
        "grid_import_w": max(0, grid_w),
        "pv_w": safe_int(payload.get("pv_w"), 0),
        "home_w": safe_int(payload.get("home_w"), 0),
        "soc": round(safe_float(payload.get("soc"), 0.0), 2),
    }


def _ems_reaction_cap_status(
    before_w: int,
    current_w: int,
    cap_w: int,
    age_s: float,
    *,
    autonomous_hint: bool = False,
    firmware_limit_hint: bool = False,
    direction: str = "charge",
) -> Tuple[str, Optional[float], int, str]:
    tolerance_w = 300
    cap_w = max(0, int(cap_w))
    before_w = max(0, int(before_w))
    current_w = max(0, int(current_w))
    need_drop_w = max(0, before_w - cap_w)
    observed_drop_w = before_w - current_w
    required_drop_w = max(tolerance_w, min(1500, int(need_drop_w * 0.35))) if need_drop_w > 0 else tolerance_w
    label = "Ladegrenze" if direction == "charge" else "Entladegrenze"

    if before_w <= cap_w + tolerance_w:
        if current_w <= cap_w + tolerance_w:
            return "ziel_eingehalten", 0.0, tolerance_w, f"{label} war bereits eingehalten"
        if autonomous_hint and direction == "charge":
            return "e3dc_autonom_lädt", None, tolerance_w, "E3DC lädt trotz EMS-Ladegrenze wegen Abregel-/WR-Druck autonom"
        if firmware_limit_hint and direction == "charge":
            return "e3dc_lädt_trotz_limit", None, tolerance_w, "E3DC meldet EMS-Limit 0W, lädt aber intern weiter"
        if age_s < 20.0:
            return "wartet", None, tolerance_w, f"{label} war vorher eingehalten, aktueller Wert driftet noch"
        return "grenze_überschritten", None, tolerance_w, f"{label} wird nach {age_s:.1f}s überschritten"

    if current_w <= cap_w + tolerance_w or observed_drop_w >= required_drop_w:
        return "reagiert", round(age_s, 1), tolerance_w, f"{label} zeigt sichtbare Reaktion"
    if autonomous_hint and direction == "charge":
        return "e3dc_autonom_lädt", None, tolerance_w, "E3DC lädt trotz EMS 0/Limit weiter, vermutlich interner Abregel-/WR-Pfad"
    if firmware_limit_hint and direction == "charge":
        return "e3dc_lädt_trotz_limit", None, tolerance_w, "E3DC meldet EMS-Limit 0W, lädt aber intern weiter"
    if age_s < 20.0:
        return "wartet", None, tolerance_w, f"{label} wartet auf sichtbare Akkuantwort"
    return "keine_sichtbare_reaktion", None, tolerance_w, f"{label} ohne sichtbare Antwort nach {age_s:.1f}s"


def build_ems_reaction_record(
    payload: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    state = _ems_reaction_history_state if state is None else state
    now_s = float(now_s if now_s is not None else time.time())
    target = _ems_reaction_target_from_payload(payload)
    signature = _ems_reaction_signature(target)
    current = _ems_reaction_sample(payload)
    if state.get("signature") != signature:
        state.clear()
        state["signature"] = signature
        state["command_ts"] = now_s
        state["before"] = dict(current)

    command_ts = safe_float(state.get("command_ts"), now_s)
    before = state.get("before") if isinstance(state.get("before"), dict) else dict(current)
    age_s = max(0.0, now_s - command_ts)
    abregel_pressure_w = max(
        0,
        safe_int(payload.get("abregel_grid_pressure_w"), 0),
        safe_int(payload.get("abregel_inverter_pressure_w"), 0),
        safe_int(payload.get("abregel_physical_pressure_w"), 0),
    )
    autonomous_hint = bool(payload.get("abregel_active")) or abregel_pressure_w > 0
    firmware_charge_limit_hint = (
        target.get("kind") == "limit"
        and safe_int(target.get("max_charge_w"), 0) <= 50
        and safe_int(payload.get("ems_max_charge_power_w"), 0) <= 50
        and safe_int(payload.get("used_charge_limit_w"), 0) <= 50
        and safe_int(current.get("battery_charge_w"), 0) > 300
    )

    if not target["active"]:
        status = "keine_ems_vorgabe"
        charge_status = "inaktiv"
        discharge_status = "inaktiv"
        reaction_s: Optional[float] = None
        note = "Storage Manager sendet in diesem Zyklus keine EMS-Ladegrenze."
        tolerance_w = 300
    elif target["kind"] == "release":
        live_limits_active = payload.get("ems_power_limits_active")
        released = live_limits_active is False or live_limits_active is None
        status = "freigegeben" if released else "wartet_freigabe"
        charge_status = status
        discharge_status = status
        reaction_s = round(age_s, 1) if released else None
        note = "EMS-Grenzen werden freigegeben; danach arbeitet E3DC autonom."
        tolerance_w = 300
    else:
        charge_status, charge_reaction_s, tolerance_w, charge_note = _ems_reaction_cap_status(
            safe_int(before.get("battery_charge_w"), 0),
            safe_int(current.get("battery_charge_w"), 0),
            safe_int(target.get("max_charge_w"), 0),
            age_s,
            autonomous_hint=autonomous_hint,
            firmware_limit_hint=firmware_charge_limit_hint,
            direction="charge",
        )
        discharge_status, discharge_reaction_s, _, discharge_note = _ems_reaction_cap_status(
            safe_int(before.get("battery_discharge_w"), 0),
            safe_int(current.get("battery_discharge_w"), 0),
            safe_int(target.get("max_discharge_w"), 0),
            age_s,
            autonomous_hint=False,
            direction="discharge",
        )
        latched = state.setdefault("latched", {})
        if charge_status == "reagiert" and charge_reaction_s is not None:
            latched.setdefault("charge_reaction_s", charge_reaction_s)
            charge_reaction_s = safe_float(latched.get("charge_reaction_s"), charge_reaction_s)
            charge_note = f"Ladegrenze hatte bereits nach {charge_reaction_s:.1f}s sichtbar reagiert"
        elif charge_status in ("wartet", "keine_sichtbare_reaktion") and latched.get("charge_reaction_s") is not None:
            charge_status = "reagiert"
            charge_reaction_s = safe_float(latched.get("charge_reaction_s"), 0.0)
            charge_note = f"Ladegrenze hatte bereits nach {charge_reaction_s:.1f}s sichtbar reagiert"
        if discharge_status == "reagiert" and discharge_reaction_s is not None:
            latched.setdefault("discharge_reaction_s", discharge_reaction_s)
            discharge_reaction_s = safe_float(latched.get("discharge_reaction_s"), discharge_reaction_s)
            discharge_note = f"Entladegrenze hatte bereits nach {discharge_reaction_s:.1f}s sichtbar reagiert"
        elif discharge_status in ("wartet", "keine_sichtbare_reaktion") and latched.get("discharge_reaction_s") is not None:
            discharge_status = "reagiert"
            discharge_reaction_s = safe_float(latched.get("discharge_reaction_s"), 0.0)
            discharge_note = f"Entladegrenze hatte bereits nach {discharge_reaction_s:.1f}s sichtbar reagiert"
        if charge_status in ("e3dc_autonom_lädt", "e3dc_lädt_trotz_limit"):
            status = charge_status
            reaction_s = None
            note = charge_note
        elif "keine_sichtbare_reaktion" in (charge_status, discharge_status):
            status = "keine_sichtbare_reaktion"
            reaction_s = None
            note = charge_note if charge_status == status else discharge_note
        elif "wartet" in (charge_status, discharge_status):
            status = "wartet"
            reaction_s = None
            note = charge_note if charge_status == "wartet" else discharge_note
        elif "reagiert" in (charge_status, discharge_status):
            status = "reagiert"
            reaction_values = []
            if charge_status == "reagiert" and charge_reaction_s is not None:
                reaction_values.append(charge_reaction_s)
            if discharge_status == "reagiert" and discharge_reaction_s is not None:
                reaction_values.append(discharge_reaction_s)
            reaction_s = min(reaction_values) if reaction_values else None
            note = charge_note if charge_status == "reagiert" else discharge_note
        else:
            status = "ziel_eingehalten"
            reaction_s = 0.0
            note = "EMS-Grenzen und beobachtete Akkuwerte passen bereits zusammen."

    record = {
        "ts": int(now_s),
        "time": datetime.datetime.fromtimestamp(now_s).isoformat(timespec="seconds"),
        "service": "storage_manager",
        "command": {
            "signature": signature,
            "active": bool(target.get("active")),
            "kind": target.get("kind"),
            "limits_used": bool(target.get("limits_used")),
            "max_charge_w": safe_int(target.get("max_charge_w"), 0),
            "max_discharge_w": safe_int(target.get("max_discharge_w"), 0),
            "discharge_start_w": safe_int(target.get("discharge_start_w"), 0),
            "reason": _compact_text(target.get("reason"), 220),
            "command_ts": int(command_ts),
            "age_s": round(age_s, 1),
        },
        "before": before,
        "current": current,
        "live_ems": {
            "power_limits_active": payload.get("ems_power_limits_active"),
            "ems_max_charge_power_w": _payload_optional_int(payload, "ems_max_charge_power_w"),
            "ems_max_discharge_power_w": _payload_optional_int(payload, "ems_max_discharge_power_w"),
            "ems_discharge_start_power_w": _payload_optional_int(payload, "ems_discharge_start_power_w"),
            "used_charge_limit_w": _payload_optional_int(payload, "used_charge_limit_w"),
            "remaining_charge_w": _payload_optional_int(payload, "remaining_charge_w"),
            "used_discharge_limit_w": _payload_optional_int(payload, "used_discharge_limit_w"),
            "remaining_discharge_w": _payload_optional_int(payload, "remaining_discharge_w"),
        },
        "reaction": {
            "status": status,
            "charge_status": charge_status,
            "discharge_status": discharge_status,
            "reaction_s": reaction_s,
            "delta_charge_w": safe_int(current.get("battery_charge_w"), 0) - safe_int(before.get("battery_charge_w"), 0),
            "delta_discharge_w": safe_int(current.get("battery_discharge_w"), 0) - safe_int(before.get("battery_discharge_w"), 0),
            "tolerance_w": tolerance_w,
            "autonomous_hint": autonomous_hint,
            "note": note,
        },
        "context": {
            "state": payload.get("state"),
            "label": payload.get("state_label", payload.get("state")),
            "curve_gap_pct": payload.get("curve_gap_pct"),
            "abregel_active": bool(payload.get("abregel_active")),
            "abregel_pressure_w": abregel_pressure_w,
            "abregel_grid_pressure_w": safe_int(payload.get("abregel_grid_pressure_w"), 0),
            "abregel_inverter_pressure_w": safe_int(payload.get("abregel_inverter_pressure_w"), 0),
            "iFc_w": safe_int(payload.get("iFc_w"), 0),
            "iMinLade_w": safe_int(payload.get("iMinLade_w"), 0),
        },
    }
    payload["ems_reaction"] = record
    return record


def write_ems_reaction_history(payload: Dict[str, Any], cfg: Dict[str, Any], *, physical_write: bool = True) -> None:
    record = build_ems_reaction_record(payload)
    if not physical_write:
        return
    write_history_record(
        record,
        config=cfg,
        log_dir=LOG_DIR,
        latest_path=EMS_REACTION_LATEST_F,
        prefix=EMS_REACTION_HISTORY_PREFIX,
        enable_key="ems_reaction_history_enable",
        max_bytes_key="ems_reaction_history_max_bytes",
        retention_key="ems_reaction_history_retention_days",
        interval_key="ems_reaction_history_interval_s",
        state=_ems_reaction_history_state,
        signature_paths=(
            "command.signature",
            "reaction.status",
            "reaction.charge_status",
            "reaction.discharge_status",
            "live_ems.power_limits_active",
            "context.state",
            "context.abregel_active",
        ),
        default_interval_s=10,
        default_max_bytes=4 * 1024 * 1024,
        default_retention_days=2,
        logger=log,
    )


DIRECT_MARKETING_OWNER_PREFIX = "direct_marketing:"
DIRECT_MARKETING_CONTRACT_VERSION = 1
DIRECT_MARKETING_EXPORT_ACTIONS = {
    "eco_plus_export_candidate",
    "arbitrage_export_candidate",
}
DIRECT_MARKETING_PV_STORE_ACTIONS = {
    "eco_plus_store_pv_candidate",
}
DIRECT_MARKETING_HEADROOM_ACTIONS = {
    "eco_plus_negative_headroom_hold",
}
DIRECT_MARKETING_GRID_ACTIONS = {
    "arbitrage_grid_charge_candidate",
}
DIRECT_MARKETING_CONTROLLABLE_ACTIONS = (
    DIRECT_MARKETING_EXPORT_ACTIONS
    | DIRECT_MARKETING_GRID_ACTIONS
    | DIRECT_MARKETING_PV_STORE_ACTIONS
    | DIRECT_MARKETING_HEADROOM_ACTIONS
)
DIRECT_MARKETING_ACTION_STATES = {
    "eco_plus_negative_headroom_hold": "direct_marketing_eco_plus_headroom_hold",
    "eco_plus_store_pv_candidate": "direct_marketing_eco_plus_pv_store",
    "eco_plus_export_candidate": "direct_marketing_eco_plus_export",
    "arbitrage_export_candidate": "direct_marketing_arbitrage_export",
    "arbitrage_grid_charge_candidate": "direct_marketing_arbitrage_grid_charge",
}
MARKET_ECONOMICS_OWNER_PREFIX = "market_economics:"
MARKET_ECONOMICS_CONTRACT_VERSION = 1
MARKET_TARGET_HYSTERESIS_PCT = 0.5
MARKET_OWNER_DWELL_S = 600.0
MARKET_GRID_ACTIONS = {"grid_charge", "negative_price_absorb"}
MARKET_HOLD_ACTIONS = {"hold_discharge"}
MARKET_AUTONOMOUS_ACTIONS = {"house_supply"}
MARKET_ECONOMICS_STATES = {
    "grid_charge": "market_grid_charge",
    "negative_price_absorb": "market_negative_absorb_grid",
    "hold_discharge": "market_discharge_hold",
    "house_supply": "market_house_supply_release",
}
MARKET_ACTION_BY_STATE = {state: action for action, state in MARKET_ECONOMICS_STATES.items()}
MARKET_ACTION_BY_STATE.update({
    "market_grid_wait": "grid_charge",
    "market_grid_pv_wait": "grid_charge",
    "market_negative_absorb_wait": "negative_price_absorb",
})

HARD_DISCHARGE_OWNER_PREFIXES = (
    "manual_override",
    "pre_discharge",
    "tl_autodump",
    "direct_marketing_eco_plus_export",
    "direct_marketing_eco_plus_headroom_export",
)
HARD_GRID_OWNER_PREFIXES = (
    "price_boost_grid",
    "storm_guard_grid",
    "market_grid_charge",
    "market_negative_absorb_grid",
)
HARD_IDLE_OWNER_PREFIXES = ("emergency_power",)


def hard_mode_justification_errors(live: Dict[str, Any], decision: Dict[str, Any]) -> List[str]:
    state = str(decision.get("state") or "")
    mode = safe_int(decision.get("mode"), MODE_AUTO)
    val_w = max(0, safe_int(decision.get("val"), 0))
    protected = bool(decision.get("protected"))
    grid_w = safe_int(live.get("Grid_Power"), 0)
    grid_ema_w = safe_int(live.get("Grid_EMA_W", grid_w), grid_w)
    import_w = max(grid_w, grid_ema_w)
    errors: List[str] = []

    if mode == MODE_DISCH:
        discharge_owner = protected and state.startswith(HARD_DISCHARGE_OWNER_PREFIXES)
        headroom_discharge_owner = bool(
            state.startswith("parallel_headroom_discharge")
            and decision.get("headroom_discharge_active")
            and not decision.get("abregel_active")
            and import_w <= safe_int(decision.get("headroom_discharge_import_guard_w"), 150)
            and safe_int(decision.get("headroom_discharge_export_room_w"), 0) >= max(300, val_w)
        )
        if not (discharge_owner or headroom_discharge_owner):
            errors.append(f"DISCH {val_w} W ohne geschützten Entlade-Besitzer: {state or 'unbekannt'}")

        if discharge_owner and state.startswith("direct_marketing_eco_plus_export"):
            dm_guard = direct_marketing_export_import_guard({}, live, val_w)
            if dm_guard.get("blocked"):
                errors.append(
                    "DV-DISCH %d W unter lokaler Last %d W bei Netzbezug %d W"
                    % (
                        val_w,
                        safe_int(dm_guard.get("local_deficit_w"), 0),
                        safe_int(dm_guard.get("grid_import_w"), 0),
                    )
                )

    if mode == MODE_GRID:
        grid_owner = protected and state.startswith(HARD_GRID_OWNER_PREFIXES)
        if not grid_owner:
            errors.append(f"GRID {val_w} W ohne geschützten Netzlade-Besitzer: {state or 'unbekannt'}")

    if mode == MODE_IDLE:
        idle_owner = protected and state.startswith(HARD_IDLE_OWNER_PREFIXES)
        if not idle_owner:
            errors.append(f"IDLE ohne expliziten Schutz-Besitzer: {state or 'unbekannt'}")
        if import_w > 150:
            errors.append(f"IDLE kann Netzbezug festhalten: {import_w} W")

    if mode in (MODE_GRID, MODE_IDLE) and import_w > 150 and not state.startswith(HARD_GRID_OWNER_PREFIXES):
        errors.append(f"{mode_label(mode)} kann vermeidbaren Netzbezug festhalten: {import_w} W")

    return errors


def hard_mode_guard_contract(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    decision: Dict[str, Any],
    max_charge_w: int,
    max_discharge_w: int,
    previous_state: Optional[Dict[str, Any]] = None,
    *,
    now_s: float,
) -> Dict[str, Any]:
    """Erzeugt die Hard-Mode-Schutzentscheidung ohne Logging oder RSCP-Ausgang."""
    errors = hard_mode_justification_errors(live, decision)
    if not errors:
        return {
            "contract_version": HARD_MODE_GUARD_CONTRACT_VERSION,
            "active": False,
            "errors": [],
            "send_auto_release": False,
        }

    previous_state = previous_state or {}
    now_s = float(now_s)
    previous_guard = str(previous_state.get("state") or "") == "hard_mode_guard_auto"
    previous_ts = safe_float(previous_state.get("ts"), 0.0)
    retry_s = max(5.0, safe_float(cfg.get("hard_mode_guard_retry_s"), 30.0))
    retry_due = previous_ts <= 0.0 or (now_s - previous_ts) >= retry_s
    send_auto_release = bool(not previous_guard or retry_due)
    reason = "Hard-Mode-Guard: " + "; ".join(errors) + "; E3DC AUTO freigegeben"
    reason_short = reason[:220]
    charge_w = max(300, int(max_charge_w or 0))
    discharge_w = max(300, int(max_discharge_w or 0))
    guarded = {
        "state": "hard_mode_guard_auto",
        "mode": MODE_AUTO,
        "val": charge_w,
        "priority": "safety",
        "reason": reason_short,
        "protected": True,
        "storage_req_w": 0,
        "budget_w": 0,
        "hard_mode_guard_contract_version": HARD_MODE_GUARD_CONTRACT_VERSION,
        "hard_mode_guard_errors": errors,
        "hard_mode_guard_retry_s": retry_s,
        "hard_mode_guard_set_power_auto": send_auto_release,
        "auto_limit": {
            "enabled": False,
            "release": True,
            "set_power_auto": send_auto_release,
            "set_power_value": 0,
            "max_charge_w": charge_w,
            "max_discharge_w": discharge_w,
            "discharge_start_w": 0,
            "heartbeat_s": auto_limit_heartbeat_s(cfg),
            "reason": reason_short,
        },
    }
    if isinstance(decision.get("shadow_payload"), dict):
        guarded["shadow_payload"] = decision["shadow_payload"]
    return {
        "contract_version": HARD_MODE_GUARD_CONTRACT_VERSION,
        "active": True,
        "errors": errors,
        "send_auto_release": send_auto_release,
        "retry_s": retry_s,
        "retry_due": retry_due,
        "previous_guard": previous_guard,
        "previous_ts": previous_ts,
        "reason": reason_short,
        "guarded_decision": guarded,
    }


def enforce_hard_mode_guard(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    decision: Dict[str, Any],
    max_charge_w: int,
    max_discharge_w: int,
    previous_state: Optional[Dict[str, Any]] = None,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    now_s = time.time() if now_s is None else float(now_s)
    contract = hard_mode_guard_contract(
        cfg,
        live,
        decision,
        max_charge_w,
        max_discharge_w,
        previous_state,
        now_s=now_s,
    )
    if not contract.get("active"):
        return decision

    reason = str(contract.get("reason") or "")
    if bool(contract.get("send_auto_release")):
        log.error("%s", log_safe_text(reason))
    else:
        log.debug("%s", log_safe_text(reason))
    guarded = contract.get("guarded_decision")
    return guarded if isinstance(guarded, dict) else decision


def ep_reserve_floor_decision(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    max_charge_w: int,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    reserve_soc = ep_reserve_soc(cfg, live)
    if reserve_soc <= 0.0:
        return None

    soc = safe_float(live.get("SOC"), 0.0)
    previous_state = previous_state or {}
    previous_hold = str(previous_state.get("state") or "") == "ep_reserve_discharge_hold"
    hysteresis_pct = max(0.2, safe_float(cfg.get("storage_ep_reserve_hold_hysteresis_pct"), 0.7))
    hold_active = bool(
        soc <= reserve_soc
        or (previous_hold and soc < reserve_soc + hysteresis_pct)
    )
    if not hold_active:
        return None

    relation = "unterschritten" if soc < reserve_soc - 0.05 else "erreicht"
    reason = (
        EP_RESERVE_VETO_SIGNATURE + ": Notstromreserve %.1f%% %s (SoC %.1f%%): "
        "Entladung hart per EMS-Grenze 0W gesperrt; Inselbetrieb bleibt E3DC-autonom"
    ) % (reserve_soc, relation, soc)
    if previous_hold and soc > reserve_soc:
        reason = (
            EP_RESERVE_VETO_SIGNATURE + ": Notstromreserve %.1f%% in Hysterese gehalten (SoC %.1f%%, Freigabe ab %.1f%%): "
            "Entladung bleibt per EMS-Grenze 0W gesperrt"
        ) % (reserve_soc, soc, reserve_soc + hysteresis_pct)

    return {
        "state": "ep_reserve_discharge_hold",
        "mode": MODE_AUTO,
        "val": max(300, int(max_charge_w or 0)),
        "priority": "safety",
        "reason": reason,
        "protected": True,
        "storage_req_w": 0,
        "budget_w": 0,
        "diagnostic_signature": EP_RESERVE_VETO_SIGNATURE,
        "decision_owner": "storage_manager",
        "safety_veto": True,
        "discharge_allowed": False,
        "export_allowed": False,
        "ep_reserve_floor_hold": True,
        "ep_reserve_pct": reserve_soc,
        "ep_reserve_hysteresis_pct": hysteresis_pct,
        "auto_limit": discharge_block_auto_limit(cfg, max(300, int(max_charge_w or 0)), reason),
    }


def log_safe_text(value: Any) -> str:
    text = str(value or "")
    repl = {
        "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
        "ß": "ss", "€": "EUR",
    }
    for src, dst in repl.items():
        text = text.replace(src, dst)
    return text.encode("ascii", "replace").decode("ascii")


def curve_relation_text(
    payload: Dict[str, Any],
    *,
    target_label: str = "der Sollkurve",
    neutral_label: str = "an der Sollkurve",
    tolerance_pct: float = 0.15,
) -> str:
    raw_soc = safe_float(payload.get("soc"), 0.0)
    relation_soc = safe_float(payload.get("curve_control_soc"), raw_soc)
    curve_soc = payload.get("curve_soc")
    if curve_soc is None:
        return f"SoC {raw_soc:.1f}% liegt zur Kurve --"
    curve = safe_float(curve_soc, raw_soc)
    use_control_soc = "curve_control_soc" in payload and abs(relation_soc - raw_soc) >= 0.25
    prefix = "Regel-SoC" if use_control_soc else "SoC"
    live_suffix = f" (Live-SoC {raw_soc:.1f}%)" if use_control_soc else ""
    delta = relation_soc - curve
    curve_txt = f"{curve:.1f}%"
    if delta > tolerance_pct:
        return (
            f"{prefix} {relation_soc:.1f}% liegt {delta:.1f} Prozentpunkte "
            f"über {target_label} {curve_txt}{live_suffix}"
        )
    if delta < -tolerance_pct:
        return (
            f"{prefix} {relation_soc:.1f}% liegt {abs(delta):.1f} Prozentpunkte "
            f"unter {target_label} {curve_txt}{live_suffix}"
        )
    return f"{prefix} {relation_soc:.1f}% liegt nahe {neutral_label} {curve_txt}{live_suffix}"


def charge_acceptance_diagnostic_contract(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    payload: Dict[str, Any],
    plan: Dict[str, Any],
    previous_state: Dict[str, Any],
    *,
    now_s: float,
) -> Dict[str, Any]:
    """Diagnostiziert begrenzte Ladeannahme ohne Voll- oder Regelclaim."""

    schema = "storage_charge_acceptance_diagnostic_v1"
    text = "Ladeannahme begrenzt – Ursache unklar"
    plan_id = plan.get("plan_id") if isinstance(plan, dict) else None
    generation_valid = bool(
        isinstance(plan_id, str)
        and len(plan_id) == 71
        and plan_id.startswith("sha256:")
    )
    remaining_valid = live.get("remaining_charge_w_valid") is True
    remaining_w = (
        safe_float(live.get("remaining_charge_w"), float("nan"))
        if remaining_valid
        else float("nan")
    )
    remaining_age_s = safe_float(live.get("remaining_charge_w_age_s"), float("nan"))
    battery_w = safe_float(live.get("Battery_Power"), float("nan"))
    offer_w = max(0.0, safe_float(payload.get("storage_charge_request_w"), 0.0))
    max_charge_w = safe_float(live.get("ems_max_charge_power_w"), float("nan"))
    live_age_s = safe_float(payload.get("live_age_s"), float("nan"))
    sources_valid = bool(
        generation_valid
        and remaining_valid
        and math.isfinite(remaining_w)
        and remaining_w >= 0.0
        and str(live.get("remaining_charge_w_source") or "")
        == "rscp_ems_remaining_bat_charge_power"
        and math.isfinite(remaining_age_s)
        and 0.0 <= remaining_age_s <= 30.0
        and math.isfinite(battery_w)
        and math.isfinite(live_age_s)
        and -5.0 <= live_age_s <= 30.0
        and payload.get("live_sample_valid", True)
        and not payload.get("live_stale")
    )
    power_settings_permissive = bool(
        live.get("ems_power_settings_read") is True
        and live.get("ems_power_settings_valid") is True
        and live.get("power_limits_active") is True
        and math.isfinite(max_charge_w)
        and offer_w >= 300.0
        and max_charge_w + 1.0 >= offer_w
    )
    low_threshold_w = max(150.0, min(300.0, offer_w * 0.15))
    limited_sample = bool(
        sources_valid
        and power_settings_permissive
        and remaining_w <= low_threshold_w
        and max(0.0, battery_w) <= low_threshold_w
    )
    previous = previous_state.get("charge_acceptance_diagnostic") if isinstance(previous_state, dict) else None
    previous = previous if isinstance(previous, dict) else {}
    previous_ts = safe_float(previous.get("ts"), 0.0)
    same_generation = previous.get("plan_id") == plan_id
    contiguous = bool(same_generation and 0.0 <= now_s - previous_ts <= 30.0)
    samples = safe_int(previous.get("coherent_limited_samples"), 0) + 1 if limited_sample and contiguous else (1 if limited_sample else 0)
    active = bool(limited_sample and samples >= 3)
    if not sources_valid:
        status = "UNAVAILABLE"
        reason_code = "CHARGE_ACCEPTANCE_SOURCE_MISSING_INVALID_OR_STALE"
    elif not power_settings_permissive:
        status = "NOT_APPLICABLE_NO_PERMISSIVE_CHARGE_OFFER"
        reason_code = "PERMISSIVE_CHARGE_OFFER_NOT_BOUND"
    elif not limited_sample:
        status = "ACCEPTANCE_NOT_LIMITED"
        reason_code = None
    elif not active:
        status = "OBSERVING"
        reason_code = "MULTI_CYCLE_CONFIRMATION_PENDING"
    else:
        status = "LIMITED_CAUSE_UNKNOWN"
        reason_code = "CHARGE_ACCEPTANCE_LIMITED_CAUSE_UNKNOWN"
    return {
        "schema_version": schema,
        "ts": round(now_s, 3),
        "plan_id": plan_id if generation_valid else None,
        "status": status,
        "reason_code": reason_code,
        "active": active,
        "display_text": text if active else None,
        "coherent_limited_samples": samples,
        "required_samples": 3,
        "ttl_s": 30.0,
        "remaining_charge_w": round(remaining_w, 3) if math.isfinite(remaining_w) else None,
        "remaining_charge_w_valid": remaining_valid,
        "actual_charge_w": round(max(0.0, battery_w), 3) if math.isfinite(battery_w) else None,
        "charge_offer_w": round(offer_w, 3),
        "low_acceptance_threshold_w": round(low_threshold_w, 3),
        "power_settings_permissive": power_settings_permissive,
        "sources_valid": sources_valid,
        "physical_full": False,
        "target_reached": False,
        "control_effect": False,
    }


def build_display(payload: Dict[str, Any]) -> Dict[str, str]:
    state = str(payload.get("state") or "parallel_auto")
    soc = safe_float(payload.get("soc"), 0.0)
    curve_soc = payload.get("curve_soc")
    curve_txt = "--" if curve_soc is None else f"{safe_float(curve_soc):.1f}%"
    pv_w = safe_int(payload.get("pv_w"), 0)
    reserve = safe_float(payload.get("ep_reserve_pct"), 0.0)
    auto_limit = payload.get("auto_limit") if isinstance(payload.get("auto_limit"), dict) else {}
    auto_limit_enabled = bool(auto_limit.get("enabled")) and not bool(auto_limit.get("release"))
    auto_limit_charge_w = safe_int(auto_limit.get("max_charge_w"), 0)
    shadow_payload = payload.get("shadow_payload") if isinstance(payload.get("shadow_payload"), dict) else {}
    shadow_inputs = shadow_payload.get("inputs") if isinstance(shadow_payload.get("inputs"), dict) else {}

    def _fmt_time_from_ts(value: Any) -> str:
        ts = safe_float(value, 0.0)
        if ts <= 0.0:
            return "--:--"
        if ts > 10000000000.0:
            ts /= 1000.0
        try:
            return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")
        except Exception:
            return "--:--"

    labels = {
        "manual_override": "Manuell",
        "manual_override_done": "Manuell beendet",
        "emergency_power": "Notstrom-Automatik",
        "ep_reserve_discharge_hold": "Notstromreserve",
        "pre_discharge": "Pre-Dump",
        "pre_discharge_wait": "Pre-Dump Verbraucher",
        "pre_discharge_consumer_auto": "Pre-Dump Verbraucher",
        "tl_autodump": "Kurven-Entladung",
        "live_stale_auto": "Live-Daten verzögert",
        "live_plausibility_auto": "Live-Daten unplausibel",
        "live_soc_unrealistic_auto": "SoC-Sprung verworfen",
        "storm_guard_grid": "Unwetter-Netzladen",
        "storm_guard_grid_wait": "Unwetter-Schutz wartet",
        "price_boost_grid": "Preis-Netzladen",
        "price_boost_grid_wait": "Preisfenster wartet",
        "market_grid_charge": "Markt-Netzladen",
        "market_grid_wait": "Markt-Netzladen wartet",
        "market_negative_absorb_grid": "Negativpreis-Aufnahme",
        "market_negative_absorb_wait": "Negativpreis wartet",
        "market_discharge_hold": "Markt-Entladesperre",
        "market_house_supply_release": "Markt-Hausversorgung frei",
        "direct_marketing_eco_plus_pv_store": "DV Eco+ PV speichern",
        "direct_marketing_eco_plus_headroom_hold": "DV Eco+ Speicherplatz halten",
        "direct_marketing_eco_plus_headroom_export": "DV Eco+ Speicherplatz schaffen",
        "direct_marketing_eco_plus_export": "DV Eco+ Einspeisung",
        "direct_marketing_arbitrage_grid_charge": "DV Arbitrage Netzladen",
        "direct_marketing_arbitrage_export": "DV Arbitrage Einspeisung",
        "price_boost_target_reached": "Preisziel erreicht",
        "unmanaged_wallbox_price_hold": "Fremdladen halten",
        "unmanaged_wallbox_wbminsoc_hold": "WB-Entladungsschutz",
        "wallbox_wbminsoc_curve_charge": "PV in Speicher",
        "wallbox_predump_floor_hold": "Pre-Dump-Untergrenze",
        "hard_mode_guard_auto": "Hard-Mode-Guard",
        "price_plan_house_discharge": "Slot: Hausstütze",
        "parallel_price_house_discharge": "Slot: Hausstütze",
        "parallel_price_hold": "Slot: Entladung gesperrt",
        "parallel_planned_load_hold": "Geplante Last",
        "parallel_planned_load_price_support": "Last: Preisstütze",
        "parallel_price_auto": "Slot: PV-Automatik",
        "parallel_price_grid": "Preis-Netzladen",
        "parallel_wb_auto": "Wallboxladung",
        "parallel_night_floor_hold": "Nachtreserve halten",
        "parallel_curve_auto_hold": "Nacht-AUTO" if pv_w <= 250 else "AUTO-Freilauf",
        "parallel_curve_auto_charge": "Kurvenladung Auto",
        "parallel_curve_charge": "Kurvenladung",
        "parallel_curve_charge_cap": "Abregel-Schutz",
        "parallel_headroom_discharge": "Headroom-Entladung",
        "parallel_curve_auto_no_surplus": "AUTO-Band",
        "parallel_grid_relief_auto": "Netzpunkt-AUTO",
        "parallel_evening_release": "Freilauf-Übergabe",
        "parallel_auto": "AUTO",
        "bev_bridge_3p_min": "BEV-Brücke",
        "no_config": "Keine Konfiguration",
        "no_data": "Keine Live-Daten",
        "stopped": "Gestoppt",
    }
    control_owners = {
        "live_stale_auto": "E3DC",
        "live_plausibility_auto": "E3DC",
        "live_soc_unrealistic_auto": "E3DC",
        "parallel_auto": "E3DC",
        "parallel_curve_auto_no_surplus": "E3DC",
        "parallel_grid_relief_auto": "E3DC",
        "parallel_evening_release": "E3DC",
        "parallel_wb_auto": "Wallbox Manager",
        "manual_override": "Storage Manager",
        "manual_override_done": "E3DC",
        "ep_reserve_discharge_hold": "Storage Manager",
        "pre_discharge": "Storage Manager",
        "pre_discharge_wait": "Storage Manager",
        "pre_discharge_consumer_auto": "Storage Manager",
        "tl_autodump": "Storage Manager",
        "storm_guard_grid": "Storage Manager",
        "storm_guard_grid_wait": "Storage Manager",
        "price_boost_grid": "Storage Manager",
        "price_boost_grid_wait": "Storage Manager",
        "market_grid_charge": "Storage Manager",
        "market_grid_wait": "Storage Manager",
        "market_negative_absorb_grid": "Storage Manager",
        "market_negative_absorb_wait": "Storage Manager",
        "market_discharge_hold": "Storage Manager",
        "market_house_supply_release": "Storage Manager",
        "direct_marketing_eco_plus_pv_store": "Storage Manager",
        "direct_marketing_eco_plus_headroom_hold": "Storage Manager",
        "direct_marketing_eco_plus_headroom_export": "Storage Manager",
        "direct_marketing_eco_plus_export": "Storage Manager",
        "direct_marketing_arbitrage_grid_charge": "Storage Manager",
        "direct_marketing_arbitrage_export": "Storage Manager",
        "price_boost_target_reached": "Storage Manager",
        "unmanaged_wallbox_price_hold": "Storage Manager",
        "unmanaged_wallbox_wbminsoc_hold": "Storage Manager",
        "wallbox_wbminsoc_curve_charge": "Storage Manager",
        "wallbox_predump_floor_hold": "Storage Manager",
        "hard_mode_guard_auto": "Storage Manager",
        "price_plan_house_discharge": "Storage Manager",
        "parallel_price_house_discharge": "Storage Manager",
        "parallel_price_hold": "Storage Manager",
        "parallel_planned_load_hold": "Storage Manager",
        "parallel_planned_load_price_support": "Storage Manager",
        "parallel_price_auto": "Storage Manager",
        "parallel_price_grid": "Storage Manager",
        "parallel_night_floor_hold": "Storage Manager",
        "parallel_curve_auto_charge": "Storage Manager",
        "parallel_curve_charge": "Storage Manager",
        "parallel_curve_charge_cap": "Storage Manager",
        "parallel_headroom_discharge": "Storage Manager",
        "bev_bridge_3p_min": "Storage Manager",
    }
    label = labels.get(state, state.replace("_", " "))
    control_owner = control_owners.get(state, "")
    if state == "parallel_curve_auto_hold" and auto_limit_enabled:
        label = "Ladegrenze aktiv" if pv_w <= 250 else "Ladeleistung geführt"
        control_owner = "Storage Manager"
    elif state == "parallel_curve_auto_hold":
        control_owner = "E3DC"
    if control_owner:
        label = f"{control_owner} führt: {label}"
    if state in ("pre_discharge_wait", "pre_discharge_consumer_auto"):
        budget_w = safe_int((payload.get("budget") or {}).get("budget_w"), safe_int(payload.get("val"), 0))
        consumer_w = safe_int(payload.get("predump_consumer_load_w"), 0)
        bev_block_w = safe_int(payload.get("predump_bev_block_w"), 0)
        bev_note = (
            f" BEV-Mindestleistung {bev_block_w} W wird als früher Block freigegeben."
            if bev_block_w > 0
            else ""
        )
        if state == "pre_discharge_consumer_auto":
            reason = (
                f"Pre-Dump nutzt lokale Verbraucher ({consumer_w} W). "
                f"E3DC bleibt autonom; nur die Akku-Entladefreigabe für Verbraucher ({budget_w} W) wird geführt."
                f"{bev_note}"
            )
        else:
            reason = (
                "Pre-Dump gibt Akku-Entladeleistung für lokale Verbraucher frei und wartet auf Last. "
                f"E3DC bleibt autonom; Akku-Freigabe aus der Batterie: {budget_w} W."
                f"{bev_note}"
            )
    elif state == "parallel_night_floor_hold":
        reason = (
            f"Nachtreserve: SoC {soc:.1f}% liegt unter Soll {curve_txt}. "
            "Nachtreserve geschützt; Hausverbrauch nutzt Netz bis PV oder ein Preisfenster übernimmt."
        )
        if reserve > 0:
            reason += f" E3DC-Notstromreserve {reserve:.1f}% bleibt geschützt."
    elif state == "parallel_curve_auto_hold" and auto_limit_enabled and bool(shadow_inputs.get("pre_curve_hold_active")):
        first_soc = shadow_inputs.get("first_curve_soc")
        first_soc_txt = f"{safe_float(first_soc):.1f}%" if first_soc is not None else "--"
        first_time = _fmt_time_from_ts(shadow_inputs.get("first_curve_ts"))
        ifc_w = safe_int(payload.get("iFc_w"), 0)
        start_w = safe_int(shadow_inputs.get("pre_curve_ifc_start_w"), 0)
        threshold_txt = f"; Frühstart ab {start_w} W iFc" if start_w > 0 else ""
        reason = (
            f"Vor Kurvenstart: Startanker {first_time} bei {first_soc_txt}. "
            f"SoC {soc:.1f}% liegt über diesem Anker; iFc {ifc_w} W{threshold_txt}. "
            f"E3DC-AUTO mit EMS-Ladegrenze {auto_limit_charge_w} W hält Speicherplatz bis zum Kurvenstart frei."
        )
    elif state == "parallel_curve_auto_hold" and auto_limit_enabled and bool(shadow_inputs.get("forecast_curve_landing_hold_active")):
        floor = shadow_inputs.get("adaptive_soc_floor", payload.get("curve_soc"))
        floor_txt = f"{safe_float(floor):.1f}%" if floor is not None else curve_txt
        gap_txt = ""
        gap = shadow_inputs.get("forecast_floor_target_gap_pct")
        if gap is not None:
            gap_txt = f" Restbedarf entlang der Sollkurve {safe_float(gap):.1f} Prozentpunkte."
        reason = (
            f"Prognose 100%: SoC {soc:.1f}% liegt an/über der Sollkurve {floor_txt}; "
            "das Tagesziel bleibt rechnerisch erreichbar."
            f"{gap_txt} E3DC-AUTO mit EMS-Ladegrenze {auto_limit_charge_w} W führt den Speicher weiter entlang der Kurve."
        )
    elif state == "parallel_curve_auto_hold" and auto_limit_enabled and bool(shadow_inputs.get("sliding_horizon_active")):
        latest_min = shadow_inputs.get("sliding_horizon_minutes_until_latest_charge")
        latest_txt = ""
        if latest_min is not None:
            latest_txt = f" Spätester Ladebeginn in {safe_float(latest_min, 0.0):.0f} min."
        confidence = safe_float(shadow_inputs.get("sliding_horizon_confidence"), 0.0)
        season = str(shadow_inputs.get("sliding_horizon_season") or "")
        season_txt = f" ({season}, Konfidenz {confidence:.2f})" if season else f" (Konfidenz {confidence:.2f})"
        reason = (
            f"Gleitender Prognosehorizont: {curve_relation_text(payload)}. "
            f"Rest-PV deckt das Tagesziel rechnerisch vor dem spätesten Ladebeginn{season_txt}."
            f"{latest_txt} E3DC-AUTO mit EMS-Ladegrenze {auto_limit_charge_w} W hält die Batterieladung ruhig."
        )
    elif state == "parallel_curve_auto_hold" and auto_limit_enabled and pv_w <= 250:
        reason = (
            f"Nacht: {curve_relation_text(payload, target_label='dem aktuellen Nacht-Soll', neutral_label='am aktuellen Nacht-Soll')}. "
            f"E3DC-AUTO haelt eine EMS-Ladegrenze von {auto_limit_charge_w} W; "
            "Hausversorgung und Speicherentladung bleiben intern geregelt."
        )
    elif state == "parallel_curve_auto_hold" and auto_limit_enabled:
        reason = (
            f"{curve_relation_text(payload)}. "
            f"E3DC-AUTO mit EMS-Ladegrenze {auto_limit_charge_w} W: "
            "die Batterieladung wird geführt, die Hausversorgung bleibt intern geregelt."
        )
    elif state == "parallel_curve_auto_hold" and pv_w <= 250:
        reason = (
            f"Nacht: {curve_relation_text(payload, target_label='dem aktuellen Nacht-Soll', neutral_label='am aktuellen Nacht-Soll')}. "
            "E3DC arbeitet autonom; die Regelung greift erst bei Preisfenster, Reserve-, Netz- oder Abregelrisiko ein."
        )
    elif state == "parallel_curve_auto_hold":
        reason = (
            f"{curve_relation_text(payload)}. "
            "E3DC arbeitet autonom; aktive Eingriffe bleiben für Netzpunkt, Abregelung und Verbraucherbudget reserviert."
        )
    elif state == "parallel_auto":
        raw_reason = str(payload.get("reason") or "")
        if (
            "Kurve fordert Ladung" in raw_reason
            or "AUTO-Haltezeit" in raw_reason
        ):
            reason = raw_reason
        else:
            reason = "Neutraler Zustand: E3DC führt im AUTO-Freilauf; keine aktive Speicherbremse."
    elif state.startswith("direct_marketing_"):
        dm_mode_raw = str(payload.get("direct_marketing_mode") or "")
        dm_mode = {"eco_plus": "Eco+", "arbitrage": "Arbitrage"}.get(dm_mode_raw, dm_mode_raw or "DV")
        dm_action = str(payload.get("direct_marketing_action") or "")
        dm_action_label = {
            "policy_headroom_hold": "Speicherplatz halten",
            "policy_headroom_export": "Speicherplatz schaffen",
            "policy_force_charge_pv": "PV speichern",
            "policy_force_export": "Hochpreisverkauf",
        }.get(dm_action, dm_action.replace("_", " "))
        profit = payload.get("direct_marketing_profit_ct_per_kwh")
        profit_txt = ""
        if profit is not None:
            profit_txt = f", erwartete Spanne {safe_float(profit, 0.0):.2f} ct/kWh"
        if state == "direct_marketing_eco_plus_headroom_hold":
            reason = (
                f"Direktvermarktung {dm_mode}: Speicherplatz bleibt für ein kommendes günstiges "
                "oder negatives Preisfenster frei. Aktives Laden ist gesperrt; es gibt keinen "
                "Batterie-Verkaufsbefehl. Hausversorgung und Reserven bleiben geschützt."
            )
        elif state == "direct_marketing_eco_plus_headroom_export":
            reserve_floor = payload.get("direct_marketing_reserve_floor_soc_pct")
            reserve_txt = (
                f" bis Reserveboden {safe_float(reserve_floor, 0.0):.1f}%"
                if reserve_floor is not None
                else ""
            )
            discharge_w = max(0, safe_int(payload.get("val"), 0))
            reason = (
                f"Direktvermarktung {dm_mode}: Der Speicher schafft gezielt Platz für ein kommendes "
                f"günstiges oder negatives Preisfenster. Entladefreigabe {discharge_w} W{reserve_txt}; "
                "zuerst wird die Hauslast gedeckt, nur der verbleibende Anteil fließt ins Netz. "
                "Der berechnete Speicherplatzbedarf und alle Reserven begrenzen den Eingriff."
            )
        elif state == "direct_marketing_eco_plus_pv_store":
            charge_w = max(
                0,
                safe_int(
                    payload.get(
                        "direct_marketing_pv_store_control_w",
                        payload.get("storage_req_w", payload.get("val")),
                    ),
                    0,
                ),
            )
            reason = (
                f"Direktvermarktung {dm_mode}: PV-Speichern im günstigen Preisfenster mit einem "
                f"Laderahmen bis {charge_w} W. Ein hartes Negativpreislimit und die DC-only-Freigabe "
                "werden getrennt ausgewertet; Netzbezug und Reserven bleiben geschützt."
            )
        elif state == "direct_marketing_arbitrage_grid_charge":
            reason = (
                f"Direktvermarktung {dm_mode}: günstiges Arbitrage-Fenster aktiv, "
                f"Speicher lädt aus dem Netz mit {safe_int(payload.get('val'), 0)} W{profit_txt}. "
                "Wirtschaftlichkeit, Nutzerfreigaben, Reserve und Hausanschluss sind geprüft."
            )
        else:
            reserve_floor = payload.get("direct_marketing_reserve_floor_soc_pct")
            reserve_txt = (
                f" bis Reserveboden {safe_float(reserve_floor, 0.0):.1f}%"
                if reserve_floor is not None
                else ""
            )
            reason = (
                f"Direktvermarktung {dm_mode}: {dm_action_label}, "
                f"Batterieeinspeisung {safe_int(payload.get('val'), 0)} W{reserve_txt}{profit_txt}. "
                "Hausversorgung und Notstrom-/Nachtreserve bleiben geschützt."
            )
    elif state == "parallel_curve_auto_no_surplus":
        reason = (
            f"SoC {soc:.1f}% liegt zur Kurve {curve_txt} im freien AUTO-Band. "
            "E3DC führt; die Regelung wartet auf echten PV-Überschuss oder Schutzbedarf."
        )
    elif state == "parallel_headroom_discharge":
        discharge_w = safe_int(payload.get("headroom_discharge_w", payload.get("val")), 0)
        gap_pct = safe_float(payload.get("headroom_discharge_gap_pct"), 0.0)
        room_w = safe_int(payload.get("headroom_discharge_export_room_w"), 0)
        floor_soc = payload.get("headroom_discharge_floor_soc", payload.get("adaptive_soc_floor"))
        floor_txt = f"{safe_float(floor_soc):.1f}%" if floor_soc is not None else curve_txt
        reason = (
            f"Abregel-Headroom: SoC {soc:.1f}% liegt {gap_pct:.1f} Prozentpunkte "
            f"über der Untergrenze {floor_txt}. Speicher entlädt begrenzt mit "
            f"{discharge_w} W, Exportraum {room_w} W; bei Abregeldruck übernimmt "
            "sofort der Abregel-Schutz."
        )
    elif state == "parallel_curve_charge" and bool(shadow_inputs.get("pre_curve_ifc_start_active")):
        first_soc = shadow_inputs.get("first_curve_soc")
        first_soc_txt = f"{safe_float(first_soc):.1f}%" if first_soc is not None else "--"
        first_time = _fmt_time_from_ts(shadow_inputs.get("first_curve_ts"))
        ifc_w = safe_int(payload.get("iFc_w"), 0)
        start_w = safe_int(shadow_inputs.get("pre_curve_ifc_start_w"), 0)
        reason = (
            f"Frühstart vor dem Kurvenanker {first_time} ({first_soc_txt}): "
            f"iFc {ifc_w} W überschreitet die Frühstartschwelle {start_w} W. "
            f"E3DC-AUTO lädt mit berechneter EMS-Ladegrenze {auto_limit_charge_w} W; "
            "der ursprüngliche Startanker bleibt als Referenz erhalten."
        )
    elif state == "parallel_grid_relief_auto":
        reason = (
            "E3DC führt den kurzzeitigen Netzpunkt-Ausgleich im AUTO-Freilauf; "
            "kein Speicher-Moduswechsel, solange der E3DC selbst ausregeln kann."
        )
    elif state == "parallel_evening_release":
        release_ts = safe_float(payload.get("curve_release_ts"), 0.0)
        release_txt = (
            datetime.datetime.fromtimestamp(release_ts).strftime("%H:%M")
            if release_ts > 0
            else "Freilauf"
        )
        reason = (
            f"{release_txt}: Freilauf erreicht. EMS-Grenzen werden sauber freigegeben; "
            "der E3DC uebernimmt Rest-PV und Nachtversorgung intern."
        )
    elif state == "parallel_wb_auto":
        reserve_w = max(
            0,
            safe_int(
                payload.get(
                    "wallbox_curve_reserve_w",
                    payload.get("storage_charge_request_w", payload.get("storage_req_w")),
                ),
                0,
            ),
        )
        if reserve_w > 0:
            reason = (
                f"Wallbox aktiv: Der Wallbox Manager führt die Ladeleistung. "
                f"Der Speicher bleibt auf {reserve_w} W iFc-Führung begrenzt."
            )
        else:
            reason = "Wallbox aktiv: E3DC arbeitet autonom, der Wallbox Manager führt die Ladeleistung."
    elif state == "unmanaged_wallbox_wbminsoc_hold":
        if bool(payload.get("wbminsoc_pv_charge_active")):
            reason = (
                "Storage Manager schützt die Hausakku-Reserve: Die Wallbox nutzt Netz, "
                "Speicherentladung ins Auto bleibt gesperrt; freie PV lädt den Speicher innerhalb des Schutzvertrags."
            )
        else:
            reason = (
                "Storage Manager schützt die Hausakku-Reserve: Auto-Ladung bekommt unterhalb der Reserve "
                "kein Speicherbudget; der Akku stützt nur Hausverbrauch und Wärmepumpe."
            )
    elif state == "wallbox_wbminsoc_curve_charge":
        raw_reason = str(payload.get("reason") or "")
        if "openWB Primary PV" in raw_reason:
            reason = raw_reason
        elif bool(payload.get("scheduled_grid_charge")):
            reason = (
                "Geplanter Netzlade-Slot: freie PV lädt den Speicher bis zur Hausakku-Reserve; "
                "die Wallbox darf im Slot Netz nutzen."
            )
        else:
            reason = (
                "PV + Akku bis Untergrenze: Weil ein Auto angesteckt ist und lädt, führt der Storage Manager "
                "den Speicher mit freier PV bis zur Hausakku-Reserve. Das Auto lädt bis dahin normal; "
                "Netz bleibt aus. Wenn die Wallbox mehr Leistung will, stützt der Akku nur Hausverbrauch und Wärmepumpe."
            )
    elif state == "manual_override_done":
        reason = str(payload.get("reason") or "Manueller Speicherbefehl beendet; E3DC wird auf AUTO freigegeben.")
    elif state == "wallbox_predump_floor_hold":
        reason = (
            f"Pre-Dump-Untergrenze erreicht: SoC {soc:.1f}% liegt am Schutzboden. "
            "Wallbox wird gestoppt; Hausversorgung und normale Batterieentladung bleiben freigegeben."
        )
    elif state == "live_stale_auto":
        age = safe_float(payload.get("live_age_s"), 0.0)
        reason = (
            f"Live-Daten sind {age:.0f}s alt; E3DC bleibt autonom, "
            "bis frische Messwerte vorliegen."
        )
    elif state == "live_plausibility_auto":
        plausibility = payload.get("live_plausibility") if isinstance(payload.get("live_plausibility"), dict) else {}
        reasons = plausibility.get("reasons") if isinstance(plausibility.get("reasons"), list) else []
        reason_txt = ", ".join(str(item) for item in reasons if str(item or "").strip())
        if not reason_txt:
            reason_txt = str(plausibility.get("home_source") or "Messwert-Plausibilität")
        reason = (
            f"Live-Frame unplausibel ({reason_txt}); keine neue aktive Speicher-Vorgabe. "
            "E3DC bleibt autonom bis ein gültiger RSCP-Frame vorliegt."
        )
    elif state == "live_soc_unrealistic_auto":
        guard = payload.get("soc_jump_guard") if isinstance(payload.get("soc_jump_guard"), dict) else {}
        raw_soc = safe_float(guard.get("raw_soc"), safe_float(payload.get("soc"), 0.0))
        last_soc = safe_float(guard.get("last_valid_soc"), raw_soc)
        drop_pct = safe_float(guard.get("drop_pct"), max(0.0, last_soc - raw_soc))
        allowed_pct = safe_float(guard.get("allowed_drop_pct"), 0.0)
        reason = (
            f"Unplausibler SoC-Sprung {last_soc:.1f}% -> {raw_soc:.1f}% "
            f"({drop_pct:.1f} Prozentpunkte, plausibel {allowed_pct:.1f}) verworfen; "
            "keine neue aktive Speicher-Vorgabe, E3DC bleibt autonom bis plausible Live-Daten vorliegen."
        )
    elif state in ("price_plan_house_discharge", "parallel_price_house_discharge"):
        reason = "Geplantes Laden: Auto darf Netz nutzen, Haus/WP wird begrenzt aus dem Speicher gestützt."
    elif state == "parallel_planned_load_hold":
        expected_w = safe_int(payload.get("planned_load_expected_w"), 0)
        observed_w = safe_int(payload.get("planned_load_observed_extra_w"), 0)
        names = payload.get("planned_load_names") or []
        name_txt = ", ".join(str(n) for n in names[:2]) if isinstance(names, list) else str(names or "geplante Last")
        reason = (
            f"Geplante externe Last aktiv ({name_txt}): erwartet {expected_w} W, erkannt {observed_w} W. "
            "Speicherentladung ist per EMS auf 0 W begrenzt; die Last nutzt PV/Netz statt den Akku."
        )
    elif state == "parallel_planned_load_price_support":
        expected_w = safe_int(payload.get("planned_load_expected_w"), 0)
        observed_w = safe_int(payload.get("planned_load_observed_extra_w"), 0)
        names = payload.get("planned_load_names") or []
        support = payload.get("planned_load_support") if isinstance(payload.get("planned_load_support"), dict) else {}
        support_w = safe_int(support.get("support_max_discharge_w", payload.get("val")), 0)
        price_ct = safe_float(support.get("price_ct"), 0.0)
        name_txt = ", ".join(str(n) for n in names[:2]) if isinstance(names, list) else str(names or "geplante Last")
        reason = (
            f"Geplante externe Last preisgeführt ({name_txt}): erwartet {expected_w} W, erkannt {observed_w} W. "
            f"Akku darf wegen Preis {price_ct:.1f} ct/kWh begrenzt bis {support_w} W stützen; "
            "Morgenreserve, Mindest-SoC, Entladeenergie und PV-Erholung sind geprüft."
        )
    else:
        reason = str(payload.get("reason") or label)
    return {
        "manager_title": "Speicher-Regelung",
        "control_owner": control_owner,
        "control_owner_label": f"{control_owner} führt" if control_owner else "",
        "state_label": label,
        "display_reason": reason,
    }


def cheap_grid_charge_active(plan: Dict[str, Any], now_ms: Optional[int] = None) -> bool:
    cheap = plan.get("cheap_grid_charge") or {}
    if not cheap.get("active"):
        return False
    win = cheap.get("active_window") or {}
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    start_ms = safe_int(win.get("start_timestamp"), 0)
    end_ms = safe_int(win.get("end_timestamp", cheap.get("window_end")), 0)
    return start_ms <= now_ms < end_ms


def storm_grid_charge_active(plan: Dict[str, Any], now_ms: Optional[int] = None) -> bool:
    storm = plan.get("storm_grid_charge") or {}
    if not storm.get("active"):
        return False
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    start_ms = safe_int(storm.get("window_start"), 0)
    end_ms = safe_int(storm.get("window_end"), 0)
    return start_ms <= now_ms < end_ms


def current_price_ct_from_plan(plan: Dict[str, Any]) -> Optional[float]:
    for key in ("current_price_ct", "price_ct", "awattar_price_ct"):
        price = safe_float(plan.get(key), -1.0)
        if price > 0:
            return price
    price = safe_float(plan.get("awattar_price"), -1.0)
    if price <= 0:
        return None
    return price / 10.0 if price > 80.0 else price


def current_slot_is_low_price(plan: Dict[str, Any], now_s: float) -> bool:
    timeline = plan.get("timeline") or []
    if not isinstance(timeline, list) or not timeline:
        return False
    now_ms = now_s * 1000.0
    horizon_ms = now_ms + 24.0 * 3600.0 * 1000.0
    current_price = None
    future_prices = []
    for slot in timeline:
        try:
            ts = float(slot.get("ts", slot.get("start_timestamp", 0)) or 0)
            end_ts = float(slot.get("end_timestamp", ts + 900000.0) or 0)
            price = safe_float(slot.get("marketprice", slot.get("price")), -1.0)
            if price <= 0:
                continue
            if ts <= now_ms < end_ts:
                current_price = price
            if now_ms <= ts <= horizon_ms:
                future_prices.append(price)
        except Exception:
            continue
    if current_price is None or not future_prices:
        return False
    return current_price <= min(future_prices) + 1.0


def grid_charge_room_w(cfg: Dict[str, Any], live: Dict[str, Any]) -> Optional[int]:
    amps = safe_float(cfg.get("grid_max_amps"), 0.0)
    if amps <= 0:
        return None
    reserve_amp = max(0.0, safe_float(cfg.get("grid_max_reserve_amp"), 2.0))
    max_import_w = max(0.0, (amps - reserve_amp) * 230.0 * 3.0)
    grid_w = max(0.0, safe_float(live.get("Grid_Power"), 0.0))
    # E3DC live power is positive while charging. Remove the current charge
    # request to estimate the base import before the next GRID command.
    current_bat_charge_w = max(0.0, safe_float(live.get("Battery_Power"), 0.0))
    base_import_w = max(0.0, grid_w - current_bat_charge_w)
    return int(max(0.0, max_import_w - base_import_w))


def market_grid_charge_live_pv_guard(live: Dict[str, Any]) -> Dict[str, Any]:
    """Block normal market grid charging while live PV can charge locally."""
    live = live or {}
    grid_w = safe_float(live.get("Grid_Power"), 0.0)
    pv_w = max(0.0, safe_float(live.get("PV_Power"), 0.0))
    home_w = max(0.0, safe_float(live.get("Home_Power"), 0.0))
    battery_w = safe_float(live.get("Battery_Power"), 0.0)
    pv_house_surplus_w = max(0.0, pv_w - home_w)
    export_guard_w = 150.0
    import_guard_w = 100.0
    battery_charge_guard_w = 300.0
    export_active = grid_w <= -export_guard_w
    pv_charging_active = bool(
        battery_w >= battery_charge_guard_w
        and pv_house_surplus_w >= battery_charge_guard_w
        and grid_w <= import_guard_w
    )
    active = bool(export_active or pv_charging_active)
    reason = ""
    if export_active:
        reason = "grid_export"
    elif pv_charging_active:
        reason = "pv_battery_charge"
    return {
        "active": active,
        "reason": reason,
        "grid_w": round(grid_w, 0),
        "pv_w": round(pv_w, 0),
        "home_w": round(home_w, 0),
        "battery_w": round(battery_w, 0),
        "pv_house_surplus_w": round(pv_house_surplus_w, 0),
        "export_guard_w": round(export_guard_w, 0),
        "import_guard_w": round(import_guard_w, 0),
        "battery_charge_guard_w": round(battery_charge_guard_w, 0),
    }


def market_live_export_absorb_charge_w(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    max_charge_w: int,
) -> Dict[str, Any]:
    """Charge just enough to absorb live export instead of shooting to max GRID."""
    live = live or {}
    max_charge_w = max(0, safe_int(max_charge_w, 0))
    grid_w = safe_float(live.get("Grid_Power"), 0.0)
    battery_charge_w = max(0.0, safe_float(live.get("Battery_Power"), 0.0))
    margin_w = max(0.0, safe_float(cfg.get("market_live_export_absorb_margin_w"), 250.0))
    raw_target_w = battery_charge_w - grid_w + margin_w
    target_w = min(max_charge_w, max(0, int(round(raw_target_w))))
    if 0 < target_w < 300:
        target_w = min(max_charge_w, 300)
    return {
        "charge_w": target_w,
        "grid_w": round(grid_w, 0),
        "battery_charge_w": round(battery_charge_w, 0),
        "margin_w": round(margin_w, 0),
        "raw_target_w": round(raw_target_w, 0),
    }


def direct_marketing_pv_source_breakdown(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
) -> Dict[str, Any]:
    del cfg
    live = live or {}
    total_pv_w = max(0, safe_int(live.get("PV_Power"), 0))
    external_ac_w = max(0, safe_int(live.get("Ext_PV_Power"), 0))
    if external_ac_w <= 0:
        for key in ("External_PV_Power", "PV_External_Power", "Additional_PV_Power"):
            value = safe_int(live.get(key), 0)
            if value > 0:
                external_ac_w = max(external_ac_w, value)
        for key in ("EMS_POWER_ADD", "Power_ADD", "ADD_POWER", "add_power"):
            value = safe_int(live.get(key), 0)
            if value < 0:
                external_ac_w = max(external_ac_w, abs(value))
    e3dc_pv_w = max(0, total_pv_w - external_ac_w)
    home_w = max(0, safe_int(live.get("Home_Power"), 0))
    wallbox_w = max(0, int(abs(safe_float(live.get("Wallbox_Power"), 0.0))))
    local_load_w = max(0, home_w + wallbox_w)
    local_load_after_external_w = max(0, local_load_w - external_ac_w)
    e3dc_dc_surplus_w = max(0, e3dc_pv_w - local_load_after_external_w)
    return {
        "total_pv_w": total_pv_w,
        "e3dc_pv_w": e3dc_pv_w,
        "external_ac_pv_w": external_ac_w,
        "home_w": home_w,
        "wallbox_w": wallbox_w,
        "local_load_w": local_load_w,
        "local_load_after_external_w": local_load_after_external_w,
        "e3dc_dc_surplus_w": e3dc_dc_surplus_w,
        "source": "e3dc_plus_external_ac" if external_ac_w > 0 else "e3dc_only",
    }


def direct_marketing_external_derating_context(
    live: Dict[str, Any],
    cfg: Optional[Dict[str, Any]] = None,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Describe upstream E3DC/LUOX PV/export derating without taking ownership."""

    live = live or {}
    cfg = cfg or {}
    raw_derating_active = any(
        _contract_bool(live.get(key))
        for key in (
            "pv_derating_active",
            "ems_derating_active",
            "EMS_IS_PV_DERATING",
        )
    )
    live_ts = safe_float(live.get("_ts"), 0.0)
    timestamp_known = live_ts > 0.0
    live_age_s = live_data_age_s(live, now_s)
    max_age_s = max(
        1.0,
        min(30.0, safe_float(cfg.get("storage_live_stale_guard_s"), 10.0)),
    )
    sample_valid = _contract_bool(
        live.get("RSCP_Sample_Valid", live.get("live_sample_valid", True))
    )
    decision_usable = _contract_bool(
        live.get("Power_Decision_Usable", live.get("decision_usable", True))
    )
    frame_fresh = bool(
        sample_valid
        and decision_usable
        and (not timestamp_known or live_age_s <= max_age_s)
    )
    pv_derating_active = bool(raw_derating_active and frame_fresh)
    raw_ac_limit = live.get("ac_power_limit_w")
    if raw_ac_limit is None:
        raw_ac_limit = live.get("EMS_AC_POWER_LIMIT")
    ac_limit_present = raw_ac_limit is not None
    ac_limit_w = max(0, safe_int(raw_ac_limit, 0))
    derate_power_w = safe_int(live.get("derate_at_power_w", live.get("EMS_DERATE_AT_POWER_VALUE")), 0)
    derate_percent = safe_float(live.get("derate_at_percent", live.get("EMS_DERATE_AT_PERCENT_VALUE")), 0.0)
    # EMS_AC_POWER_LIMIT is the live actuator value. During LUOX zero-export
    # E3/DC reports it explicitly as 0 W while the static plant derating value
    # remains at installed power. The live value must therefore win whenever
    # a fresh derating signal is present, including the valid zero value.
    if raw_derating_active and ac_limit_present:
        limit_w = ac_limit_w
        limit_source = "ems_ac_power_limit"
    elif derate_power_w > 0:
        limit_w = derate_power_w
        limit_source = "configured_derate_power"
    else:
        limit_w = 0 if pv_derating_active else None
        limit_source = "derating_signal" if pv_derating_active else "none"
    return {
        "active": bool(pv_derating_active),
        "signal_active": bool(raw_derating_active),
        "source": (
            "e3dc_luox_derating"
            if pv_derating_active
            else ("e3dc_luox_derating_stale" if raw_derating_active else "none")
        ),
        "limit_w": limit_w,
        "limit_source": limit_source,
        "ac_power_limit_w": ac_limit_w,
        "derate_at_power_w": derate_power_w,
        "derate_at_percent": round(derate_percent, 3),
        "frame_fresh": frame_fresh,
        "sample_valid": sample_valid,
        "decision_usable": decision_usable,
        "timestamp_known": timestamp_known,
        "live_age_s": round(live_age_s, 2),
        "max_age_s": round(max_age_s, 2),
    }


def direct_marketing_export_execution_contract(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    export_constraint: Dict[str, Any],
    *,
    external_derating: Optional[Dict[str, Any]] = None,
    storage_absorption_w: int = 0,
    storage_absorption_cap_w: int = 0,
    unavoidable_export_w: int = 0,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    """State what a requested export limit physically achieved without claiming actuator ownership."""

    cfg = cfg or {}
    live = live or {}
    constraint = export_constraint if isinstance(export_constraint, dict) else {}
    requested = bool(constraint.get("hard"))
    requested_limit_w = max(0, safe_int(constraint.get("limit_w"), 0)) if requested else None
    tolerance_w = max(
        0,
        safe_int(cfg.get("direct_marketing_pv_store_export_limit_guard_w"), 100),
    )
    external = (
        external_derating
        if isinstance(external_derating, dict)
        else direct_marketing_external_derating_context(live, cfg, now_s)
    )

    grid_values = []
    for key in ("Grid_Power", "Grid_EMA_W"):
        raw_value = live.get(key)
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            grid_values.append(value)
    grid_present = bool(grid_values)
    grid_export_w = max([0.0] + [-value for value in grid_values])
    frame_fresh = bool(external.get("frame_fresh"))
    external_limit_w = external.get("limit_w")
    external_owner_confirmed = bool(
        requested
        and frame_fresh
        and external.get("active")
        and external_limit_w is not None
        and safe_int(external_limit_w, requested_limit_w or 0) <= safe_int(requested_limit_w, 0) + tolerance_w
    )
    grid_point_confirmed = bool(
        requested
        and frame_fresh
        and grid_present
        and grid_export_w <= safe_int(requested_limit_w, 0) + tolerance_w
    )
    compliance_confirmed = bool(external_owner_confirmed and grid_point_confirmed)
    storage_absorption_w = max(0, safe_int(storage_absorption_w, 0))
    storage_absorption_cap_w = max(0, safe_int(storage_absorption_cap_w, 0))
    unavoidable_export_w = max(0, safe_int(unavoidable_export_w, 0))
    violation_w = (
        max(0, int(round(grid_export_w - safe_int(requested_limit_w, 0) - tolerance_w)))
        if requested and frame_fresh and grid_present
        else 0
    )
    storage_absorption_active = storage_absorption_w >= 300

    state = "not_requested"
    claim = "none"
    reason = "no_hard_export_limit_requested"
    best_effort = False
    unavoidable = False
    if requested:
        state = "requested_unconfirmed"
        claim = "requested"
        reason = "export_limit_requested_not_confirmed"
        if not frame_fresh:
            reason = "export_limit_live_frame_stale_or_invalid"
        elif not grid_present:
            reason = "export_limit_grid_point_missing"
        elif compliance_confirmed:
            state = "external_confirmed"
            claim = "confirmed"
            reason = "external_limit_and_grid_point_confirmed"
        elif external_owner_confirmed:
            state = "external_owner_grid_violation" if violation_w > 0 else "external_owner_pending"
            reason = (
                "external_limit_reported_grid_point_above_limit"
                if violation_w > 0
                else "external_limit_reported_grid_point_pending"
            )
        elif storage_absorption_active:
            best_effort = True
            state = "best_effort_storage_absorption"
            claim = "best_effort"
            reason = "storage_absorption_without_external_export_owner"
        if violation_w > 0 and (
            unavoidable_export_w > 0
            or storage_absorption_cap_w <= 0
            or storage_absorption_w >= storage_absorption_cap_w
        ):
            unavoidable = True
            state = "violated_unavoidable"
            claim = "unavoidable"
            reason = "export_limit_violation_unavoidable"

    return {
        "schema": DIRECT_MARKETING_EXPORT_EXECUTION_SCHEMA,
        "state": state,
        "claim": claim,
        "reason": reason,
        "requested": requested,
        "requested_limit_w": requested_limit_w,
        "scope": str(constraint.get("scope") or ("grid_connection" if requested else "storage_priority")),
        "enforcement": str(constraint.get("enforcement") or ("requested" if requested else "storage_priority")),
        "expected_execution_owner": str(
            constraint.get("execution_owner")
            or ("external_e3dc_luox" if requested else "storage_manager")
        ),
        "external_owner_confirmed": external_owner_confirmed,
        "grid_point_confirmed": grid_point_confirmed,
        "compliance_confirmed": compliance_confirmed,
        "best_effort": best_effort,
        "violation": bool(violation_w > 0),
        "unavoidable": unavoidable,
        "violation_w": violation_w,
        "grid_export_w": int(round(grid_export_w)),
        "grid_tolerance_w": tolerance_w,
        "grid_point_present": grid_present,
        "storage_absorption_w": storage_absorption_w,
        "storage_absorption_cap_w": storage_absorption_cap_w,
        "unavoidable_export_w": unavoidable_export_w,
        "frame_fresh": frame_fresh,
        "live_age_s": external.get("live_age_s"),
        "sample_valid": bool(external.get("sample_valid")),
        "external_derating": dict(external),
    }


def direct_marketing_window_export_constraint(window: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the price export contract, including safe legacy fallback."""

    window = window if isinstance(window, dict) else {}
    classification = str(window.get("export_constraint_class") or "").strip().lower()
    reason = str(window.get("reason") or "").strip().lower()
    price_class = str(window.get("pv_store_price_class") or "").strip().lower()
    explicit_hard = _contract_optional_bool(window.get("hard_export_limit_active"))
    if explicit_hard is None:
        legacy_negative = bool(
            reason in ("negative_price", "negative_price_headroom")
            or price_class == "negative_price"
            or bool(window.get("negative_headroom_limited"))
        )
        hard = bool(
            legacy_negative
            and (window.get("curtailment_allowed") or window.get("headroom_limited"))
        )
    else:
        hard = bool(explicit_hard)

    if not classification:
        if hard:
            classification = "negative_hard"
        elif reason == "threshold_below_eeg" or price_class == "eeg_soft" or window.get("pv_store_soft_threshold"):
            classification = "eeg_soft"
        elif reason == "negative_price" or price_class == "negative_price":
            classification = "negative_allowed"
        else:
            classification = "low_price_soft"

    limit_w = None
    if hard:
        limit_w = max(
            0,
            safe_int(
                window.get("hard_export_limit_w"),
                safe_int(window.get("curtail_export_limit_w"), 0),
            ),
        )
    explicit_pv_export_allowed = _contract_optional_bool(window.get("pv_export_allowed"))
    return {
        "class": classification,
        "hard": hard,
        "limit_w": limit_w,
        "scope": str(
            window.get("export_constraint_scope")
            or ("grid_connection" if hard else "storage_priority")
        ),
        "pv_export_allowed": not hard if explicit_pv_export_allowed is None else explicit_pv_export_allowed,
        "enforcement": str(
            window.get("export_constraint_enforcement")
            or ("requested" if hard else "storage_priority")
        ),
        "execution_owner": str(
            window.get("export_constraint_execution_owner")
            or ("external_e3dc_luox" if hard else "storage_manager")
        ),
    }


def direct_marketing_pv_store_control_w(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    window: Dict[str, Any],
    flags: Dict[str, Any],
    max_charge_w: int,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    max_charge_w = max(0, safe_int(max_charge_w, 0))
    if max_charge_w <= 0:
        return {"blocked": True, "blocker": "pv_store_charge_power_below_min", "charge_w": 0}

    pv_sources = direct_marketing_pv_source_breakdown(cfg, live)
    source_diag = {
        "pv_total_w": pv_sources["total_pv_w"],
        "pv_e3dc_w": pv_sources["e3dc_pv_w"],
        "pv_external_ac_w": pv_sources["external_ac_pv_w"],
        "pv_source": pv_sources["source"],
        "dc_surplus_w": pv_sources["e3dc_dc_surplus_w"],
        "local_load_after_external_w": pv_sources["local_load_after_external_w"],
    }
    grid_w = safe_float(live.get("Grid_Power"), 0.0)
    grid_ema_w = safe_float(live.get("Grid_EMA_W", grid_w), grid_w)
    grid_import_w = max(0, int(round(max(grid_w, grid_ema_w, 0.0))))
    grid_export_w = max(0, int(round(max(-grid_w, -grid_ema_w, 0.0))))
    external_derating = direct_marketing_external_derating_context(live, cfg, now_s)
    export_constraint = direct_marketing_window_export_constraint(window)
    export_limit_active = bool(export_constraint.get("hard"))
    export_limit_w = max(0, safe_int(export_constraint.get("limit_w"), 0))
    external_owner_tolerance_w = max(
        0,
        safe_int(
            flags.get("pv_store_export_limit_guard_w"),
            safe_int(cfg.get("direct_marketing_pv_store_export_limit_guard_w"), 100),
        ),
    )
    external_limit_w = external_derating.get("limit_w")
    external_export_owner_active = bool(
        external_derating.get("active")
        and (
            not export_constraint.get("hard")
            or (
                external_limit_w is not None
                and safe_int(external_limit_w, export_limit_w) <= export_limit_w + external_owner_tolerance_w
            )
        )
    )
    if external_derating.get("active"):
        external_limit_w = safe_int(external_derating.get("limit_w"), 0)
        export_limit_w = min(export_limit_w, external_limit_w) if export_limit_active else external_limit_w
        export_limit_active = True
    export_limit_guard_w = max(
        0,
        safe_int(
            flags.get("pv_store_export_limit_guard_w"),
            safe_int(cfg.get("direct_marketing_pv_store_export_limit_guard_w"), 100),
        ),
    )
    export_over_limit_w = (
        max(0, grid_export_w - export_limit_w - export_limit_guard_w)
        if export_limit_active
        else 0
    )
    export_limit_guard_active = bool(export_limit_active and export_over_limit_w > 0)
    import_guard_w = max(
        0,
        safe_int(
            flags.get("pv_store_import_guard_w"),
            safe_int(cfg.get("direct_marketing_pv_store_import_guard_w"), 80),
        ),
    )
    pv_w = pv_sources["total_pv_w"]
    home_w = pv_sources["home_w"]
    wallbox_w = pv_sources["wallbox_w"]
    current_battery_charge_w = max(0, safe_int(live.get("Battery_Power"), 0))
    physical_surplus_w = max(0, pv_w - home_w - wallbox_w)
    estimated_offer_w = max(physical_surplus_w, grid_export_w + current_battery_charge_w)
    export_absorb_target_w = current_battery_charge_w + export_over_limit_w if export_limit_guard_active else current_battery_charge_w
    if export_limit_guard_active:
        estimated_offer_w = max(estimated_offer_w, export_absorb_target_w)
    pv_safe_cap_w = physical_surplus_w
    dc_only = bool(flags.get("pv_store_dc_only_enable")) or cfg_bool(cfg, "direct_marketing_pv_store_dc_only_enable", False)
    external_ac_guard_w = max(
        0,
        safe_int(
            flags.get("pv_store_external_ac_guard_w"),
            safe_int(cfg.get("direct_marketing_pv_store_external_ac_guard_w"), 100),
        ),
    )
    if dc_only and pv_sources["external_ac_pv_w"] > external_ac_guard_w:
        pv_safe_cap_w = min(pv_safe_cap_w, pv_sources["e3dc_dc_surplus_w"])
        physical_surplus_w = min(physical_surplus_w, pv_sources["e3dc_dc_surplus_w"])
    # The running battery charge may be the result of our previous command. For
    # PV-only storage it is only an estimate, never permission to exceed PV.
    offer_w = min(estimated_offer_w, pv_safe_cap_w)
    self_reference_limited = bool(estimated_offer_w > pv_safe_cap_w)
    unavoidable_export_w = max(0, export_absorb_target_w - offer_w) if export_limit_guard_active else 0
    export_limit_diag = {
        "export_constraint_class": export_constraint.get("class"),
        "hard_export_limit_active": bool(export_constraint.get("hard")),
        "hard_export_limit_w": export_constraint.get("limit_w"),
        "export_constraint_scope": export_constraint.get("scope"),
        "pv_export_allowed": bool(export_constraint.get("pv_export_allowed")),
        "export_limit_active": export_limit_active,
        "external_export_owner_active": external_export_owner_active,
        "hard_export_owner_confirmed": bool(
            not export_constraint.get("hard") or external_export_owner_active
        ),
        "export_limit_guard_active": export_limit_guard_active,
        "export_limit_w": export_limit_w if export_limit_active else None,
        "export_limit_guard_w": export_limit_guard_w,
        "export_over_limit_w": export_over_limit_w,
        "export_absorb_target_w": int(export_absorb_target_w),
        "unavoidable_export_w": int(unavoidable_export_w),
        "external_derating_active": bool(external_derating.get("active")),
        "external_derating_source": external_derating.get("source"),
        "external_derating_limit_w": external_derating.get("limit_w"),
        "external_derating_ac_power_limit_w": external_derating.get("ac_power_limit_w"),
        "external_derating_power_w": external_derating.get("derate_at_power_w"),
        "external_derating_percent": external_derating.get("derate_at_percent"),
    }
    if grid_import_w > import_guard_w:
        return {
            "blocked": True,
            "blocker": "pv_store_grid_import_guard",
            "charge_w": 0,
            "grid_import_w": grid_import_w,
            "grid_export_w": grid_export_w,
            "import_guard_w": import_guard_w,
            "estimated_offer_w": int(estimated_offer_w),
            "pv_safe_cap_w": int(pv_safe_cap_w),
            "self_reference_limited": self_reference_limited,
            **export_limit_diag,
            **source_diag,
        }

    min_surplus_w = max(
        0,
        safe_int(
            flags.get("pv_store_min_surplus_w"),
            safe_int(cfg.get("direct_marketing_pv_store_min_surplus_w"), 300),
        ),
    )
    if offer_w < max(300, min_surplus_w):
        blocker = "pv_store_surplus_below_min"
        if dc_only and pv_sources["external_ac_pv_w"] > external_ac_guard_w:
            blocker = "pv_store_dc_surplus_below_min"
        return {
            "blocked": True,
            "blocker": blocker,
            "charge_w": 0,
            "offer_w": offer_w,
            "physical_surplus_w": physical_surplus_w,
            "grid_import_w": grid_import_w,
            "grid_export_w": grid_export_w,
            "import_guard_w": import_guard_w,
            "min_surplus_w": min_surplus_w,
            "dc_only": dc_only,
            "external_ac_guard_w": external_ac_guard_w,
            "estimated_offer_w": estimated_offer_w,
            "pv_safe_cap_w": pv_safe_cap_w,
            "self_reference_limited": self_reference_limited,
            **export_limit_diag,
            **source_diag,
        }

    cap_w = max_charge_w
    for value in (
        flags.get("pv_store_max_w"),
        window.get("max_power_w"),
    ):
        limit_w = safe_int(value, 0)
        if limit_w > 0:
            cap_w = min(cap_w, limit_w)
    charge_w = min(max_charge_w, cap_w, offer_w)
    if export_limit_guard_active:
        export_absorb_cap_w = min(max_charge_w, offer_w)
        charge_w = max(charge_w, min(export_absorb_cap_w, export_absorb_target_w))
    charge_w = predump_round_budget_w(cfg, charge_w)
    if charge_w < max(300, min_surplus_w):
        return {
            "blocked": True,
            "blocker": "pv_store_charge_power_below_min",
            "charge_w": charge_w,
            "offer_w": offer_w,
            "physical_surplus_w": physical_surplus_w,
            "grid_import_w": grid_import_w,
            "grid_export_w": grid_export_w,
            "import_guard_w": import_guard_w,
            "min_surplus_w": min_surplus_w,
            "max_w": cap_w,
            "dc_only": dc_only,
            "external_ac_guard_w": external_ac_guard_w,
            "estimated_offer_w": estimated_offer_w,
            "pv_safe_cap_w": pv_safe_cap_w,
            "self_reference_limited": self_reference_limited,
            **export_limit_diag,
            **source_diag,
        }
    return {
        "blocked": False,
        "charge_w": int(charge_w),
        "offer_w": int(offer_w),
        "physical_surplus_w": int(physical_surplus_w),
        "grid_import_w": grid_import_w,
        "grid_export_w": grid_export_w,
        "import_guard_w": import_guard_w,
        "min_surplus_w": min_surplus_w,
        "max_w": int(cap_w),
        "pv_w": pv_w,
        "home_w": home_w,
        "wallbox_w": wallbox_w,
        "current_battery_charge_w": current_battery_charge_w,
        "dc_only": dc_only,
        "external_ac_guard_w": external_ac_guard_w,
        "estimated_offer_w": int(estimated_offer_w),
        "pv_safe_cap_w": int(pv_safe_cap_w),
        "self_reference_limited": self_reference_limited,
        **export_limit_diag,
        **source_diag,
    }


def direct_marketing_pv_store_curve_catchup_w(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    window: Dict[str, Any],
    max_charge_w: int,
    now_s: float,
) -> Dict[str, Any]:
    max_charge_w = max(0, safe_int(max_charge_w, 0))
    if max_charge_w <= 0:
        return {"active": False, "charge_w": 0, "reason": "no_charge_capacity"}

    capacity_wh = safe_float(
        plan.get("battery_capacity"),
        safe_float(live.get("bat_full_cap_kwh"), safe_float(cfg.get("speichergroesse"), 10.0)) * 1000.0,
    )
    if capacity_wh <= 0.0:
        return {"active": False, "charge_w": 0, "reason": "no_capacity"}

    soc = max(0.0, min(100.0, safe_float(live.get("SOC"), 0.0)))
    curve_soc, lookahead_soc, lookahead_ts = current_curve(
        plan,
        now_s,
        allow_before_start=True,
    )
    deadband_pct = max(
        0.05,
        safe_float(
            cfg.get("direct_marketing_pv_store_curve_deadband_pct"),
            safe_float(cfg.get("storage_curve_catchup_deadband_pct"), 0.05),
        ),
    )
    candidates: List[Tuple[str, int, float, float]] = []

    def add_candidate(kind: str, target_pct: Any, deadline_s: Optional[float]) -> None:
        target = max(0.0, min(100.0, safe_float(target_pct, -1.0)))
        gap_pct = max(0.0, target - soc)
        if gap_pct <= deadband_pct:
            return
        if deadline_s is not None and deadline_s > now_s:
            hours = max(0.20, (float(deadline_s) - float(now_s)) / 3600.0)
        else:
            hours = max(
                0.25,
                safe_float(
                    cfg.get("direct_marketing_pv_store_curve_catchup_h"),
                    safe_float(cfg.get("storage_curve_catchup_h"), 0.75),
                ),
            )
        required_w = int(round((gap_pct / 100.0) * capacity_wh / hours))
        if required_w <= 0:
            return
        candidates.append((kind, required_w, target, gap_pct))

    add_candidate("curve_now", curve_soc, None)
    add_candidate("curve_lookahead", lookahead_soc, lookahead_ts)

    window_target_soc = window.get("target_soc_pct") if isinstance(window, dict) else None
    window_end_ms = safe_int(window.get("end_ts"), 0) if isinstance(window, dict) else 0
    has_curve_reference = bool(curve_soc is not None or lookahead_soc is not None)
    if has_curve_reference and window_target_soc is not None and window_end_ms > 0:
        add_candidate("window_target", window_target_soc, window_end_ms / 1000.0)

    if not candidates:
        return {
            "active": False,
            "charge_w": 0,
            "soc_pct": round(soc, 1),
            "curve_soc_pct": round(safe_float(curve_soc, 0.0), 1) if curve_soc is not None else None,
            "curve_target_soc_pct": round(safe_float(lookahead_soc, 0.0), 1) if lookahead_soc is not None else None,
            "deadband_pct": round(deadband_pct, 2),
        }

    selected_kind, selected_w, selected_target, selected_gap = max(candidates, key=lambda item: item[1])
    min_w = min(
        max_charge_w,
        max(
            300,
            safe_int(
                cfg.get("direct_marketing_pv_store_curve_min_w"),
                safe_int(cfg.get("storage_curve_catchup_min_w"), 300),
            ),
        ),
    )
    charge_w = max(min_w if selected_w > 0 else 0, selected_w)
    charge_w = min(max_charge_w, predump_round_budget_w(cfg, charge_w))
    return {
        "active": charge_w > 0,
        "charge_w": int(charge_w),
        "source": selected_kind,
        "raw_w": int(selected_w),
        "soc_pct": round(soc, 1),
        "target_soc_pct": round(selected_target, 1),
        "gap_pct": round(selected_gap, 2),
        "curve_soc_pct": round(safe_float(curve_soc, 0.0), 1) if curve_soc is not None else None,
        "curve_target_soc_pct": round(safe_float(lookahead_soc, 0.0), 1) if lookahead_soc is not None else None,
        "curve_target_ts": safe_float(lookahead_ts, 0.0) if lookahead_ts is not None else 0.0,
        "window_target_soc_pct": round(safe_float(window_target_soc, 0.0), 1) if window_target_soc is not None else None,
        "window_end_ts": window_end_ms,
        "capacity_wh": round(capacity_wh, 0),
        "deadband_pct": round(deadband_pct, 2),
    }


def direct_marketing_pv_store_threshold_ok(window: Dict[str, Any]) -> bool:
    if not isinstance(window, dict) or window.get("pv_store_threshold_ct") is None:
        return False
    if window.get("net_sell_ct") is None:
        return False
    return safe_float(window.get("net_sell_ct"), 999999.0) <= safe_float(window.get("pv_store_threshold_ct"), -999999.0)


def direct_marketing_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    direct = plan.get("direct_marketing") if isinstance(plan.get("direct_marketing"), dict) else {}
    return direct if isinstance(direct, dict) else {}


def direct_marketing_policy_context(direct: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the additive DV policy contract without changing control output."""
    policy = direct.get("policy_decision") if isinstance(direct, dict) and isinstance(direct.get("policy_decision"), dict) else {}
    present = bool(policy)
    schema = str(policy.get("schema") or "")
    schema_valid = bool(present and schema == DIRECT_MARKETING_POLICY_SCHEMA)
    target_state = str(policy.get("dv_target_state") or "").strip().upper()
    blocked = bool(policy.get("blocked"))
    active_state = bool(target_state in DIRECT_MARKETING_POLICY_ACTIVE_STATES)
    passive_state = bool(target_state in DIRECT_MARKETING_POLICY_PASSIVE_STATES)
    explicit_commands_allowed = _contract_optional_bool(policy.get("commands_allowed"))
    commands_allowed = bool(schema_valid and not blocked and active_state)
    if explicit_commands_allowed is not None:
        commands_allowed = bool(commands_allowed and explicit_commands_allowed)
    storage_budget = policy.get("storage_budget") if isinstance(policy.get("storage_budget"), dict) else {}
    export_constraint = policy.get("export_constraint") if isinstance(policy.get("export_constraint"), dict) else {}
    block_reason = str(policy.get("block_reason") or "")
    if present and not schema_valid:
        block_reason = block_reason or "policy_schema_invalid"
    elif present and blocked:
        block_reason = block_reason or "policy_blocked"
    elif present and not active_state and not passive_state:
        block_reason = block_reason or "policy_target_state_unsupported"
    return {
        "present": present,
        "policy": policy,
        "schema": schema,
        "schema_valid": schema_valid,
        "target_state": target_state,
        "blocked": blocked,
        "active_state": active_state,
        "passive_state": passive_state,
        "commands_allowed": commands_allowed,
        "storage_budget": storage_budget,
        "export_constraint": export_constraint,
        "block_reason": block_reason,
        "export_budget_w": max(0, safe_int(storage_budget.get("export_budget_w"), 0)),
        "charge_budget_w": max(0, safe_int(storage_budget.get("charge_budget_w"), 0)),
        "protected_reserve_wh": max(0, safe_int(storage_budget.get("protected_reserve_wh"), 0)),
        "sellable_wh": max(0, safe_int(storage_budget.get("sellable_wh"), 0)),
        "headroom_hold_active": bool(storage_budget.get("headroom_hold_active")),
        "headroom_deficit_wh": max(0, safe_int(storage_budget.get("headroom_deficit_wh"), 0)),
        "headroom_target_soc_pct": max(
            0.0,
            min(100.0, safe_float(storage_budget.get("headroom_target_soc_pct"), 0.0)),
        ),
    }


def direct_marketing_policy_context_from_payload(
    payload: Dict[str, Any],
    plan: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Read a policy contract from the live payload first, then from the plan."""
    payload_policy = (
        payload.get("direct_marketing_policy_decision")
        if isinstance(payload, dict) and isinstance(payload.get("direct_marketing_policy_decision"), dict)
        else None
    )
    if payload_policy is not None:
        return direct_marketing_policy_context({"policy_decision": payload_policy})
    return direct_marketing_policy_context(direct_marketing_plan(plan or {}))


def direct_marketing_policy_action(target_state: str, mode: str = "") -> str:
    target = str(target_state or "").strip().upper()
    if target == "FORCE_CHARGE_PV":
        return "policy_force_charge_pv"
    if target == "HEADROOM_EXPORT":
        return "policy_headroom_export"
    if target == "FORCE_EXPORT":
        return "policy_force_export"
    if target == "HOLD":
        return "policy_hold"
    if target == "NORMAL":
        return "policy_normal"
    return "policy_unknown"


def direct_marketing_policy_state(target_state: str, mode: str = "") -> str:
    target = str(target_state or "").strip().upper()
    strategy = str(mode or "").strip().lower()
    if target == "FORCE_CHARGE_PV":
        return "direct_marketing_eco_plus_pv_store"
    if target == "HEADROOM_EXPORT":
        return "direct_marketing_eco_plus_headroom_export"
    if target == "FORCE_EXPORT" and strategy == "arbitrage":
        return "direct_marketing_arbitrage_export"
    if target == "FORCE_EXPORT":
        return "direct_marketing_eco_plus_export"
    return ""


def direct_marketing_policy_window(policy_ctx: Dict[str, Any], now_s: float) -> Dict[str, Any]:
    """Build a stable diagnostic window from a policy decision."""
    policy = policy_ctx.get("policy") if isinstance(policy_ctx.get("policy"), dict) else {}
    selected = policy.get("selected_window") if isinstance(policy.get("selected_window"), dict) else {}
    target_state = str(policy_ctx.get("target_state") or "")
    action = direct_marketing_policy_action(target_state)
    window = {
        "start_ts": safe_int(selected.get("start_ts"), 0),
        "end_ts": safe_int(selected.get("end_ts"), 0),
        "action": action,
        "reason": str(selected.get("reason") or policy_ctx.get("block_reason") or target_state.lower()),
        "policy_target_state": target_state,
        "max_power_w": safe_int(
            policy_ctx.get("charge_budget_w") if target_state == "FORCE_CHARGE_PV" else policy_ctx.get("export_budget_w"),
            0,
        ),
    }
    for key in (
        "next_charge_window_start_ts",
        "storage_action",
        "export_constraint_class",
        "hard_export_limit_active",
        "hard_export_limit_w",
        "export_constraint_scope",
        "pv_export_allowed",
        "export_constraint_enforcement",
        "export_constraint_execution_owner",
    ):
        if selected.get(key) is not None:
            window[key] = selected.get(key)
    policy_constraint = policy_ctx.get("export_constraint") if isinstance(policy_ctx.get("export_constraint"), dict) else {}
    if "export_constraint_class" not in window and policy_constraint:
        window.update({
            "export_constraint_class": policy_constraint.get("class"),
            "hard_export_limit_active": bool(policy_constraint.get("hard")),
            "hard_export_limit_w": policy_constraint.get("limit_w"),
            "export_constraint_scope": policy_constraint.get("scope"),
            "pv_export_allowed": bool(policy_constraint.get("pv_export_allowed", True)),
            "export_constraint_enforcement": policy_constraint.get("enforcement"),
            "export_constraint_execution_owner": policy_constraint.get("execution_owner"),
        })
    return window


def direct_marketing_current_window(direct: Dict[str, Any], now_s: float) -> Optional[Dict[str, Any]]:
    windows = direct.get("windows") if isinstance(direct.get("windows"), list) else []
    now_ms = int(float(now_s) * 1000.0)
    for window in windows:
        if not isinstance(window, dict):
            continue
        start_ms = safe_int(window.get("start_ts"), 0)
        end_ms = safe_int(window.get("end_ts"), 0)
        if start_ms <= now_ms < end_ms:
            return window
    return None


def direct_marketing_policy_executor_gate(
    direct: Dict[str, Any],
    policy_ctx: Dict[str, Any],
    now_s: float,
    plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Bindet ein Policy-Ausführungsfenster an den aktuellen kanonischen Planslot."""
    now_ms = int(float(now_s) * 1000.0)
    policy = policy_ctx.get("policy") if isinstance(policy_ctx.get("policy"), dict) else {}
    selected = policy.get("selected_window") if isinstance(policy.get("selected_window"), dict) else {}
    execution = policy.get("execution_window") if isinstance(policy.get("execution_window"), dict) else {}
    target_state = str(policy_ctx.get("target_state") or "").strip().upper()
    blocked = {
        "allowed": False,
        "reason": "policy_window_bounds_missing",
        "target_state": target_state,
        "now_ms": now_ms,
    }
    if not policy_ctx.get("schema_valid") or not policy_ctx.get("commands_allowed"):
        blocked["reason"] = "policy_contract_blocked"
        return blocked
    if str(direct.get("controller_owner") or "") != "storage_manager" or not str(
        direct.get("plan_owner") or ""
    ).startswith(DIRECT_MARKETING_OWNER_PREFIX):
        blocked["reason"] = "policy_owner_mismatch"
        return blocked

    def bounds(document: Dict[str, Any]) -> Optional[Tuple[int, int]]:
        start = safe_int(document.get("start_ts"), 0)
        end = safe_int(document.get("end_ts"), 0)
        if start < 10_000_000_000 or end < 10_000_000_000 or end <= start:
            return None
        return start, end

    segment_bounds = bounds(policy)
    selected_bounds = bounds(selected)
    execution_bounds = bounds(execution)
    if segment_bounds is None or selected_bounds is None:
        return blocked
    segment_start, segment_end = segment_bounds
    selected_start, selected_end = selected_bounds
    canonical = None
    canonical_slot: Dict[str, Any] = {}
    if isinstance(plan, dict):
        canonical = validate_canonical_plan(plan, now_ms)
        if not canonical.get("valid"):
            blocked["reason"] = "canonical_%s" % str(
                canonical.get("block_reason_code") or "plan_invalid"
            ).lower()
            blocked["plan_id"] = canonical.get("plan_id")
            return blocked
        canonical_direct = direct_marketing_plan(plan)
        if direct != canonical_direct:
            blocked["reason"] = "canonical_direct_marketing_mismatch"
            blocked["plan_id"] = canonical.get("plan_id")
            blocked["slot_id"] = canonical.get("slot_id")
            return blocked
        canonical_slot = canonical.get("slot") if isinstance(canonical.get("slot"), dict) else {}

    # Bereits persistierte kanonische Pläne aus der vorherigen Vertragsversion
    # besitzen noch kein execution_window. Nur ein gültiger aktueller
    # plan_id/slot_id-Snapshot darf daraus genau ein Ausführungsfenster ableiten.
    if execution_bounds is None and isinstance(canonical, dict):
        canonical_start = safe_int(canonical_slot.get("start_ts_ms"), 0)
        canonical_end = safe_int(canonical_slot.get("end_ts_ms"), 0)
        selected_action = str(selected.get("action") or "")
        selected_window_id = str(selected.get("window_id") or policy.get("window_id") or "")
        fallback_matches = []
        for window in direct.get("windows") if isinstance(direct.get("windows"), list) else []:
            if not isinstance(window, dict):
                continue
            window_start = safe_int(window.get("start_ts"), 0)
            window_end = safe_int(window.get("end_ts"), 0)
            window_id = str(window.get("export_plateau_id") or window.get("window_id") or "")
            if (
                str(window.get("action") or "") == selected_action
                and window_start <= canonical_start < canonical_end <= window_end
                and window_end == selected_end
                and (not selected_window_id or not window_id or selected_window_id == window_id)
            ):
                fallback_matches.append(window)
        if len(fallback_matches) == 1:
            fallback_window = fallback_matches[0]
            execution = {
                "contract_version": 1,
                "action": selected_action,
                "start_ts": max(
                    safe_int(fallback_window.get("start_ts"), 0),
                    segment_start,
                    canonical_start,
                ),
                "end_ts": min(
                    safe_int(fallback_window.get("end_ts"), 0),
                    segment_end,
                    canonical_end,
                ),
                "plan_window_start_ts": safe_int(fallback_window.get("start_ts"), 0),
                "plan_window_end_ts": safe_int(fallback_window.get("end_ts"), 0),
                "origin_start_ts": selected_start,
                "window_id": selected_window_id or str(
                    fallback_window.get("export_plateau_id")
                    or fallback_window.get("window_id")
                    or ""
                ),
                "source": "canonical_slot_legacy_policy_adapter_v1",
                "plan_id": canonical.get("plan_id"),
                "slot_id": canonical.get("slot_id"),
            }
            execution_bounds = bounds(execution)
            blocked["execution_window_adapter"] = execution.get("source")
        elif len(fallback_matches) > 1:
            blocked["reason"] = "policy_execution_window_ambiguous"
            blocked["plan_id"] = canonical.get("plan_id")
            blocked["slot_id"] = canonical.get("slot_id")
            return blocked
    if execution_bounds is None:
        blocked["reason"] = "policy_execution_window_missing"
        if isinstance(canonical, dict):
            blocked["plan_id"] = canonical.get("plan_id")
            blocked["slot_id"] = canonical.get("slot_id")
        return blocked
    execution_start, execution_end = execution_bounds
    if not (segment_start <= now_ms < segment_end):
        blocked["reason"] = "policy_segment_expired" if now_ms >= segment_end else "policy_segment_not_started"
        return blocked
    if not (selected_start <= now_ms < selected_end):
        blocked["reason"] = "policy_selected_window_expired" if now_ms >= selected_end else "policy_selected_window_future"
        return blocked
    if not (execution_start <= now_ms < execution_end):
        blocked["reason"] = "policy_execution_window_expired" if now_ms >= execution_end else "policy_execution_window_future"
        return blocked

    selected_action = str(selected.get("action") or "")
    execution_action = str(execution.get("action") or "")
    source_action = str(policy.get("source_action") or selected_action)
    executable_action = str(policy.get("executable_action") or "")
    expected_actions = {
        "FORCE_CHARGE_PV": {"eco_plus_store_pv_candidate"},
        "FORCE_EXPORT": {"eco_plus_export_candidate"},
        "HEADROOM_EXPORT": {"eco_plus_negative_headroom_hold"},
    }.get(target_state, set())
    if (
        not expected_actions
        or selected_action not in expected_actions
        or execution_action != selected_action
        or source_action != selected_action
        or executable_action != selected_action
    ):
        blocked["reason"] = "policy_window_mismatch"
        return blocked
    storage_budget = policy_ctx.get("storage_budget") if isinstance(policy_ctx.get("storage_budget"), dict) else {}
    if target_state == "FORCE_EXPORT" and safe_int(storage_budget.get("export_budget_w"), 0) <= 0:
        blocked["reason"] = "policy_budget_missing"
        return blocked
    if target_state == "FORCE_CHARGE_PV" and safe_int(storage_budget.get("charge_budget_w"), 0) <= 0:
        blocked["reason"] = "policy_budget_missing"
        return blocked

    source_window_start = safe_int(execution.get("plan_window_start_ts"), 0)
    source_window_end = safe_int(execution.get("plan_window_end_ts"), 0)
    execution_window_id = str(execution.get("window_id") or "")
    matching_windows = []
    for window in direct.get("windows") if isinstance(direct.get("windows"), list) else []:
        if not isinstance(window, dict):
            continue
        window_id = str(
            window.get("export_plateau_id")
            or window.get("window_id")
            or ""
        )
        if (
            str(window.get("action") or "") == selected_action
            and safe_int(window.get("start_ts"), 0) == source_window_start
            and safe_int(window.get("end_ts"), 0) == source_window_end
            and (not execution_window_id or not window_id or execution_window_id == window_id)
        ):
            matching_windows.append(window)
    if len(matching_windows) != 1:
        blocked["reason"] = "policy_window_mismatch"
        return blocked
    matching_window = matching_windows[0]

    if not (
        selected_start <= execution_start < execution_end <= selected_end
        and segment_start <= execution_start < execution_end <= segment_end
    ):
        blocked["reason"] = "policy_execution_window_mismatch"
        return blocked

    plan_valid_until = safe_int(direct.get("valid_until_ts"), 0)
    if plan_valid_until < 10_000_000_000:
        blocked["reason"] = "plan_bounds_missing"
        return blocked
    effective_end = min(segment_end, selected_end, execution_end, plan_valid_until)
    effective_start = max(segment_start, selected_start, execution_start)

    if isinstance(plan, dict):
        canonical_start = safe_int(canonical_slot.get("start_ts_ms"), 0)
        canonical_end = safe_int(canonical_slot.get("end_ts_ms"), 0)
        if not (
            effective_start <= canonical_start <= now_ms < canonical_end <= effective_end
        ):
            blocked["reason"] = "canonical_slot_policy_window_mismatch"
            blocked["plan_id"] = canonical.get("plan_id")
            blocked["slot_id"] = canonical.get("slot_id")
            return blocked
        effective_start = canonical_start
        effective_end = canonical_end
    if now_ms < effective_start:
        blocked["reason"] = "policy_window_not_started"
        return blocked
    if now_ms >= effective_end:
        blocked["reason"] = "plan_expired" if now_ms >= plan_valid_until else "policy_selected_window_expired"
        return blocked
    bound_window = dict(matching_window)
    bound_window.update({
        "source_window_start_ts": source_window_start,
        "source_window_end_ts": source_window_end,
        "selected_window_origin_start_ts": selected_start,
        "start_ts": effective_start,
        "end_ts": effective_end,
        "plan_id": canonical.get("plan_id") if isinstance(canonical, dict) else None,
        "slot_id": canonical.get("slot_id") if isinstance(canonical, dict) else None,
        "generation_contract": "canonical_plan_slot_v1" if isinstance(canonical, dict) else "policy_window_only_v1",
    })
    return {
        "allowed": True,
        "reason": "ok",
        "target_state": target_state,
        "now_ms": now_ms,
        "effective_start_ts": effective_start,
        "effective_end_ts": effective_end,
        "plan_id": canonical.get("plan_id") if isinstance(canonical, dict) else None,
        "slot_id": canonical.get("slot_id") if isinstance(canonical, dict) else None,
        "execution_window_source": str(execution.get("source") or ""),
        "selected_window": selected,
        "execution_window": execution,
        "plan_window": bound_window,
    }


def direct_marketing_future_pv_store_reservation(
    plan: Dict[str, Any],
    now_s: float,
) -> Dict[str, Any]:
    direct = direct_marketing_plan(plan or {})
    reservation = direct.get("future_pv_store_reservation") if isinstance(direct.get("future_pv_store_reservation"), dict) else {}
    result = dict(reservation)
    result.setdefault("active", False)
    result.setdefault("reason", "reservation_missing")
    result["valid"] = False
    if reservation.get("schema") != "direct_marketing_future_pv_store_reservation_v1":
        result["reason"] = "reservation_schema_invalid"
        return result
    if not bool(reservation.get("active")) or not bool(reservation.get("commands_allowed")):
        return result
    next_window = reservation.get("next_window") if isinstance(reservation.get("next_window"), dict) else {}
    start_ms = safe_int(next_window.get("start_ts"), 0)
    end_ms = safe_int(next_window.get("end_ts"), 0)
    valid_until = safe_int(reservation.get("valid_until_ts"), 0)
    now_ms = int(float(now_s) * 1000.0)
    if min(start_ms, end_ms, valid_until) < 10_000_000_000 or end_ms <= start_ms:
        result.update({"active": False, "reason": "reservation_bounds_invalid"})
        return result
    if now_ms >= start_ms or now_ms >= valid_until:
        result.update({"active": False, "reason": "reservation_expired"})
        return result
    if str(next_window.get("action") or "") != "eco_plus_store_pv_candidate":
        result.update({"active": False, "reason": "reservation_window_mismatch"})
        return result
    if str(reservation.get("data_quality") or "") != "ok":
        result.update({"active": False, "reason": "reservation_data_quality_invalid"})
        return result
    result["valid"] = True
    return result


def direct_marketing_curve_charge_reservation_cap(
    cfg: Dict[str, Any],
    plan: Dict[str, Any],
    now_s: float,
    *,
    auto_state: str,
    current_limit_w: int,
    current_release: bool,
    max_charge_w: int,
    pv_after_fixed_w: int,
    grid_w: int,
) -> Dict[str, Any]:
    """Übersetzt eine gültige Reservierung in eine flüchtige AUTO-Ladegrenze."""
    reservation = direct_marketing_future_pv_store_reservation(plan, now_s)
    result = {
        "active": False,
        "max_charge_w": max(0, safe_int(current_limit_w, 0)),
        "reservation": reservation,
        "reason": str(reservation.get("reason") or "reservation_inactive"),
        "grid_import_w": max(0, safe_int(grid_w, 0)),
    }
    if not reservation.get("valid") or auto_state not in {
        "parallel_curve_auto_hold",
        "parallel_curve_auto_no_surplus",
        "parallel_curve_charge",
    }:
        return result
    cap_w = max(
        0,
        min(
            max(0, safe_int(max_charge_w, 0)),
            safe_int(reservation.get("max_curve_charge_w"), 0),
        ),
    )
    import_guard_w = max(0, safe_int(cfg.get("direct_marketing_pv_store_import_guard_w"), 80))
    grid_import_w = max(0, safe_int(grid_w, 0))
    pv_charge_available_w = max(0, safe_int(pv_after_fixed_w, 0), max(0, -safe_int(grid_w, 0)))
    if grid_import_w > import_guard_w:
        cap_w = 0
    elif cap_w > 0:
        cap_w = min(cap_w, pv_charge_available_w)
    opens_existing_hold = bool(current_release or safe_int(current_limit_w, 0) <= 0)
    applied_cap_w = cap_w if opens_existing_hold else min(max(0, safe_int(current_limit_w, 0)), cap_w)
    return {
        "active": True,
        "max_charge_w": applied_cap_w,
        "reservation": reservation,
        "reason": str(reservation.get("reason") or "future_pv_store_headroom_reserved"),
        "grid_import_w": grid_import_w,
        "import_guard_w": import_guard_w,
        "pv_charge_available_w": pv_charge_available_w,
        "opens_existing_hold": opens_existing_hold,
    }


def direct_marketing_contract_errors(
    cfg: Dict[str, Any],
    direct: Dict[str, Any],
    now_s: float,
) -> List[str]:
    errors: List[str] = []
    if not cfg_bool(cfg, "direct_marketing_enable", False):
        errors.append("config_disabled")
    if not direct:
        errors.append("plan_missing")
        return errors
    if not bool(direct.get("active")):
        errors.append("plan_inactive")
    if str(direct.get("controller_owner") or "") != "storage_manager":
        errors.append("controller_owner_mismatch")
    if not str(direct.get("plan_owner") or "").startswith(DIRECT_MARKETING_OWNER_PREFIX):
        errors.append("plan_owner_mismatch")
    contract_version = safe_int(
        direct.get("owner_contract_version", (direct.get("flags") or {}).get("owner_contract_version")),
        0,
    )
    if contract_version != DIRECT_MARKETING_CONTRACT_VERSION:
        errors.append("contract_version_mismatch")
    flags = direct.get("flags") if isinstance(direct.get("flags"), dict) else {}
    if not bool(flags.get("commands_allowed")):
        errors.append("commands_not_allowed")
    mode = str(direct.get("mode") or cfg.get("direct_marketing_mode") or "").strip().lower()
    if mode == "arbitrage":
        errors.append("arbitrage_not_released")
    valid_until = safe_int(direct.get("valid_until_ts"), 0)
    now_ms = int(float(now_s) * 1000.0)
    if 0 < valid_until < 10_000_000_000:
        errors.append("plan_bounds_missing")
    elif valid_until > 0 and valid_until <= now_ms:
        errors.append("plan_expired")
    return errors


def direct_marketing_contract_warnings(
    cfg: Dict[str, Any],
    direct: Dict[str, Any],
) -> List[str]:
    warnings: List[str] = []
    if not cfg_bool(cfg, "direct_marketing_enable", False):
        return warnings
    flags = direct.get("flags") if isinstance(direct.get("flags"), dict) else {}
    mode = str(direct.get("mode") or cfg.get("direct_marketing_mode") or "").strip().lower()
    if mode in ("eco+", "ecoplus"):
        mode = "eco_plus"
    threshold_missing = (
        mode in ("eco", "eco_plus")
        and bool(flags.get("pv_store_enable"))
        and flags.get("pv_store_threshold_ct") is None
        and not cfg_bool(cfg, "direct_marketing_eeg_enable", False)
    )
    if threshold_missing:
        warnings.append("pv_store_threshold_missing_score_fallback")
    if (
        bool(flags.get("export_enable"))
        and bool(flags.get("grid_charge_enable"))
        and cfg_bool(cfg, "direct_marketing_eeg_enable", False)
        and not bool(flags.get("eeg_grid_export_risk_ack"))
    ):
        warnings.append("eeg_grid_export_risk_ack_missing")
    return sorted(set(warnings))


def direct_marketing_ramped_power_w(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    previous_state: Dict[str, Any],
    target_w: int,
) -> Tuple[int, bool, int, int]:
    target = max(0, int(target_w or 0))
    prev_state = str((previous_state or {}).get("state") or "")
    if prev_state.startswith("direct_marketing_"):
        previous_w = max(0, safe_int((previous_state or {}).get("val"), 0))
    else:
        previous_w = max(0, -safe_int((live or {}).get("Battery_Power"), 0))
        if previous_w < 250:
            previous_w = 0
        previous_w = min(previous_w, target)
    if target <= previous_w:
        return target, False, previous_w, 0
    step_w = max(100, safe_int(cfg.get("direct_marketing_ramp_step_w"), safe_int(cfg.get("predump_grid_ramp_up_w"), 300)))
    ramped = min(target, previous_w + step_w)
    if 0 < ramped < 300:
        ramped = min(target, 300)
    ramped = min(target, predump_round_budget_w(cfg, ramped))
    return ramped, ramped < target, previous_w, step_w


def direct_marketing_previous_state_age_s(previous_state: Optional[Dict[str, Any]], now_s: float) -> float:
    previous_state = previous_state or {}
    since_ts = safe_float(previous_state.get("parallel_state_since_ts"), 0.0)
    if since_ts <= 0.0:
        since_ts = safe_float(previous_state.get("ts"), 0.0)
    if since_ts <= 0.0:
        return 0.0
    return max(0.0, float(now_s) - since_ts)


def direct_marketing_owner_switch_cooldown(
    cfg: Dict[str, Any],
    previous_state: Optional[Dict[str, Any]],
    next_state: str,
    window: Dict[str, Any],
    live: Dict[str, Any],
    now_s: float,
) -> Dict[str, Any]:
    """Debounce soft DV owner changes without delaying hard price/derating cases."""

    previous_state = previous_state or {}
    previous_name = str(previous_state.get("state") or previous_state.get("parallel_state") or "")
    next_name = str(next_state or "")
    hold_s = max(0.0, safe_float(cfg.get("direct_marketing_owner_switch_hold_s"), 45.0))
    inactive = {
        "active": False,
        "hold_s": round(hold_s, 1),
        "age_s": direct_marketing_previous_state_age_s(previous_state, now_s),
        "remaining_s": 0.0,
        "previous_state": previous_name,
        "next_state": next_name,
    }
    if hold_s <= 0.0:
        return inactive
    if next_name not in ("direct_marketing_eco_plus_pv_store", "direct_marketing_eco_plus_headroom_hold"):
        return inactive
    if not previous_name.startswith("direct_marketing_") or previous_name == next_name:
        return inactive

    reason = str((window or {}).get("reason") or "")
    external_derating = direct_marketing_external_derating_context(live, cfg, now_s)
    hard_window = bool(
        reason in ("negative_price", "negative_price_headroom")
        or (window or {}).get("negative_headroom_limited")
        or external_derating.get("active")
    )
    if hard_window:
        return inactive

    age_s = direct_marketing_previous_state_age_s(previous_state, now_s)
    if age_s >= hold_s:
        return inactive
    return {
        "active": True,
        "hold_s": round(hold_s, 1),
        "age_s": round(age_s, 1),
        "remaining_s": round(max(0.0, hold_s - age_s), 1),
        "previous_state": previous_name,
        "next_state": next_name,
        "reason": reason,
    }


def direct_marketing_pv_store_hold_context(
    cfg: Dict[str, Any],
    previous_state: Optional[Dict[str, Any]],
    now_s: float,
) -> Dict[str, Any]:
    previous_state = previous_state or {}
    previous_name = str(previous_state.get("state") or previous_state.get("parallel_state") or "")
    min_hold_s = max(0.0, safe_float(cfg.get("direct_marketing_pv_store_min_hold_s"), 600.0))
    age_s = direct_marketing_previous_state_age_s(previous_state, now_s)
    active = bool(previous_name == "direct_marketing_eco_plus_pv_store" and min_hold_s > 0.0 and age_s < min_hold_s)
    return {
        "active": active,
        "min_s": round(min_hold_s, 1),
        "age_s": round(age_s, 1),
        "remaining_s": round(max(0.0, min_hold_s - age_s), 1) if active else 0.0,
    }


def direct_marketing_pv_store_ramped_power_w(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    previous_state: Optional[Dict[str, Any]],
    target_w: int,
    now_s: float,
) -> Dict[str, Any]:
    target = max(0, int(target_w or 0))
    previous_state = previous_state or {}
    previous_name = str(previous_state.get("state") or previous_state.get("parallel_state") or "")
    same_state = previous_name == "direct_marketing_eco_plus_pv_store"
    if previous_name == "direct_marketing_eco_plus_pv_store":
        previous_w = max(0, safe_int(previous_state.get("val"), 0))
    else:
        previous_w = max(0, safe_int((live or {}).get("Battery_Power"), 0))
        if previous_w < 250:
            previous_w = 0
        previous_w = min(previous_w, target)
    step_w = max(
        100,
        safe_int(
            cfg.get("direct_marketing_pv_store_ramp_step_w"),
            safe_int(cfg.get("direct_marketing_ramp_step_w"), safe_int(cfg.get("predump_grid_ramp_up_w"), 300)),
        ),
    )
    state_age_s = direct_marketing_previous_state_age_s(previous_state, now_s)
    resync_threshold_w = max(500, 2 * step_w)
    stale_tiny_cap_w = 300
    observed_charge_w = max(0, safe_int((live or {}).get("Battery_Power"), 0))
    resync_active = bool(
        same_state
        and 0 < previous_w < stale_tiny_cap_w
        and target - previous_w > resync_threshold_w
        and state_age_s >= 30.0
        and observed_charge_w <= max(stale_tiny_cap_w, previous_w + step_w)
    )
    if target <= previous_w or resync_active:
        return {
            "charge_w": target,
            "ramp_limited": False,
            "ramp_base_w": previous_w,
            "ramp_step_w": 0 if target <= previous_w else step_w,
            "resync_active": resync_active,
            "resync_reason": "stale_tiny_charge_cap" if resync_active else "",
            "resync_gap_w": max(0, target - previous_w),
            "resync_threshold_w": resync_threshold_w,
            "observed_charge_w": observed_charge_w,
        }
    ramped = min(target, previous_w + step_w)
    if 0 < ramped < 300:
        ramped = min(target, 300)
    ramped = min(target, predump_round_budget_w(cfg, ramped))
    return {
        "charge_w": ramped,
        "ramp_limited": ramped < target,
        "ramp_base_w": previous_w,
        "ramp_step_w": step_w,
        "resync_active": False,
        "resync_reason": "",
        "resync_gap_w": max(0, target - previous_w),
        "resync_threshold_w": resync_threshold_w,
        "observed_charge_w": observed_charge_w,
    }


def direct_marketing_pv_store_release_hold_w(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    previous_state: Optional[Dict[str, Any]],
    control: Dict[str, Any],
    window: Dict[str, Any],
    action: str,
    now_s: float,
    max_charge_w: int,
) -> Dict[str, Any]:
    """Keep a running DV PV-store EMS frame through tiny threshold dips."""

    previous_state = previous_state or {}
    previous_name = str(previous_state.get("state") or previous_state.get("parallel_state") or "")
    if previous_name != "direct_marketing_eco_plus_pv_store":
        return {"active": False}

    previous_action = str(previous_state.get("direct_marketing_action") or "")
    if previous_action and previous_action != action:
        return {"active": False}

    previous_auto_limit = previous_state.get("auto_limit") if isinstance(previous_state.get("auto_limit"), dict) else {}
    previous_auto_active = bool(previous_auto_limit.get("enabled")) and not bool(previous_auto_limit.get("release"))
    if not previous_auto_active:
        return {"active": False}

    min_hold_s = max(0.0, safe_float(cfg.get("direct_marketing_pv_store_min_hold_s"), 600.0))
    if min_hold_s <= 0.0:
        return {"active": False}
    age_s = direct_marketing_previous_state_age_s(previous_state, now_s)
    if age_s >= min_hold_s:
        return {"active": False, "age_s": round(age_s, 1), "min_s": round(min_hold_s, 1)}

    blocker = str(control.get("blocker") or "")
    if blocker not in ("pv_store_surplus_below_min", "pv_store_charge_power_below_min"):
        return {"active": False, "blocker": blocker, "age_s": round(age_s, 1), "min_s": round(min_hold_s, 1)}

    import_guard_w = max(
        0,
        safe_int(
            control.get("import_guard_w"),
            safe_int(previous_state.get("direct_marketing_pv_store_import_guard_w"), 80),
        ),
    )
    grid_import_w = max(0, safe_int(control.get("grid_import_w"), 0))
    if grid_import_w > import_guard_w:
        return {"active": False, "blocker": "pv_store_grid_import_guard", "grid_import_w": grid_import_w}

    if bool(previous_state.get("direct_marketing_pv_store_dc_only")) and blocker == "pv_store_dc_surplus_below_min":
        return {"active": False, "blocker": blocker}

    target_soc = safe_float(
        previous_state.get("direct_marketing_target_soc_pct"),
        safe_float((window or {}).get("target_soc_pct"), 0.0),
    )
    soc = safe_float(live.get("SOC"), 0.0)
    hysteresis = max(0.2, safe_float(cfg.get("direct_marketing_target_hysteresis_pct"), 0.7))
    if target_soc > 0.0 and soc >= target_soc - hysteresis:
        return {"active": False, "blocker": "target_soc_reached"}

    previous_w = max(
        0,
        safe_int(previous_auto_limit.get("max_charge_w"), 0),
        safe_int(previous_state.get("val"), 0),
    )
    current_battery_charge_w = max(0, safe_int(live.get("Battery_Power"), 0))
    offer_w = max(
        0,
        safe_int(control.get("offer_w"), 0),
        safe_int(control.get("charge_w"), 0),
        safe_int(control.get("estimated_offer_w"), 0),
        current_battery_charge_w,
    )
    floor_w = max(
        300,
        safe_int(
            control.get("min_surplus_w"),
            safe_int(previous_state.get("direct_marketing_pv_store_min_surplus_w"), 300),
        ),
    )
    hold_w = max(floor_w, min(previous_w or floor_w, max(floor_w, offer_w)))
    hold_w = min(max(0, safe_int(max_charge_w, 0)), predump_round_budget_w(cfg, hold_w))
    if hold_w < 300:
        return {"active": False, "blocker": "hold_power_below_min"}

    return {
        "active": True,
        "charge_w": hold_w,
        "age_s": round(age_s, 1),
        "min_s": round(min_hold_s, 1),
        "remaining_s": round(max(0.0, min_hold_s - age_s), 1),
        "blocker": blocker,
        "previous_w": previous_w,
        "offer_w": safe_int(control.get("offer_w"), 0),
        "estimated_offer_w": safe_int(control.get("estimated_offer_w"), 0),
        "grid_import_w": grid_import_w,
        "import_guard_w": import_guard_w,
    }


def direct_marketing_export_hold_context(
    cfg: Dict[str, Any],
    direct: Dict[str, Any],
    window: Dict[str, Any],
    previous_state: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Allow a running export window to survive tiny profitability replans."""
    previous_state = previous_state or {}
    previous_name = str(previous_state.get("state") or "")
    if previous_name != "direct_marketing_eco_plus_export":
        return {"allowed": False}

    mode = str(direct.get("mode") or "").strip().lower()
    if mode != "eco_plus":
        return {"allowed": False}

    flags = direct.get("flags") if isinstance(direct.get("flags"), dict) else {}
    if not bool(flags.get("export_enable")) or safe_int(flags.get("max_export_w"), 0) <= 0:
        return {"allowed": False}

    action = str((window or {}).get("action") or "")
    if action not in {"eco_plus_house_supply", "eco_plus_export_candidate"}:
        return {"allowed": False}

    economics = direct.get("economics") if isinstance(direct.get("economics"), dict) else {}
    if bool(economics.get("pv_shift_profit_ok")):
        return {"allowed": False}

    min_profit_ct = max(
        0.0,
        safe_float(
            economics.get("min_profit_ct_per_kwh"),
            safe_float(cfg.get("direct_marketing_min_profit_ct_per_kwh"), 0.0),
        ),
    )
    min_margin_pct = max(
        0.0,
        safe_float(
            economics.get("min_margin_pct"),
            safe_float(cfg.get("direct_marketing_min_margin_pct"), 10.0),
        ),
    )
    profit_hold_ct = max(0.0, safe_float(cfg.get("direct_marketing_profit_hold_ct_per_kwh"), 0.5))
    margin_hold_pct = max(0.0, safe_float(cfg.get("direct_marketing_margin_hold_pct"), 5.0))
    profit_floor_ct = max(0.0, min_profit_ct - profit_hold_ct)
    margin_floor_pct = max(0.0, min_margin_pct - margin_hold_pct)
    spread_ct = safe_float(economics.get("pv_shift_spread_ct_per_kwh"), -999.0)
    margin_pct = safe_float(economics.get("pv_shift_margin_pct"), -999.0)
    if spread_ct < profit_floor_ct or margin_pct < margin_floor_pct:
        return {
            "allowed": False,
            "spread_ct": round(spread_ct, 2),
            "margin_pct": round(margin_pct, 1),
            "profit_floor_ct": round(profit_floor_ct, 2),
            "margin_floor_pct": round(margin_floor_pct, 1),
        }

    hold_window = dict(window or {})
    hold_window.update({
        "action": "eco_plus_export_candidate",
        "reason": "profit_hold_hysteresis",
        "max_power_w": safe_int(flags.get("max_export_w"), 0),
        "economic_basis": "pv_shift",
    })
    return {
        "allowed": True,
        "window": hold_window,
        "action": "eco_plus_export_candidate",
        "state": "direct_marketing_eco_plus_export",
        "spread_ct": round(spread_ct, 2),
        "margin_pct": round(margin_pct, 1),
        "profit_hold_ct": round(profit_hold_ct, 2),
        "margin_hold_pct": round(margin_hold_pct, 1),
        "profit_floor_ct": round(profit_floor_ct, 2),
        "margin_floor_pct": round(margin_floor_pct, 1),
    }


def direct_marketing_storage_decision(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    direct: Dict[str, Any],
    now_s: float,
    max_charge_w: int,
    max_discharge_w: int,
    previous_state: Optional[Dict[str, Any]] = None,
    plan: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Map a v1 DV policy decision to storage output using local guards only."""
    policy_ctx = direct_marketing_policy_context(direct)
    if not policy_ctx.get("present"):
        return False, None

    target_state = str(policy_ctx.get("target_state") or "")
    if (
        not policy_ctx.get("schema_valid")
        or bool(policy_ctx.get("blocked"))
        or target_state in DIRECT_MARKETING_POLICY_PASSIVE_STATES
        or not policy_ctx.get("commands_allowed")
    ):
        return True, None
    if target_state not in DIRECT_MARKETING_POLICY_ACTIVE_STATES:
        return True, None

    contract_errors = direct_marketing_contract_errors(cfg, direct, now_s)
    blocking_contract_errors = [err for err in contract_errors if err != "commands_not_allowed"]
    if blocking_contract_errors:
        return True, None

    mode_name = str(direct.get("mode") or cfg.get("direct_marketing_mode") or "").strip().lower()
    if mode_name in ("eco+", "ecoplus"):
        mode_name = "eco_plus"
    state = direct_marketing_policy_state(target_state, mode_name)
    if not state:
        return True, None
    action = direct_marketing_policy_action(target_state, mode_name)
    executor_gate = direct_marketing_policy_executor_gate(direct, policy_ctx, now_s, plan)
    if not executor_gate.get("allowed"):
        return True, None
    window = executor_gate.get("plan_window") if isinstance(executor_gate.get("plan_window"), dict) else {}
    reserve = direct.get("reserve") if isinstance(direct.get("reserve"), dict) else {}
    economics = direct.get("economics") if isinstance(direct.get("economics"), dict) else {}
    flags = direct.get("flags") if isinstance(direct.get("flags"), dict) else {}
    reserve_floor = max(
        safe_float(reserve.get("effective_min_soc_pct"), 0.0),
        ep_reserve_soc(cfg, live),
    )
    owner_switch_ctx = direct_marketing_owner_switch_cooldown(
        cfg,
        previous_state,
        state,
        window,
        live,
        now_s,
    )
    if owner_switch_ctx.get("active"):
        return True, None

    common = {
        "priority": "direct_marketing",
        "protected": True,
        "direct_marketing_active": True,
        "direct_marketing_policy_active": True,
        "direct_marketing_policy_schema": policy_ctx.get("schema"),
        "direct_marketing_policy_decision": policy_ctx.get("policy"),
        "direct_marketing_policy_target_state": target_state,
        "direct_marketing_policy_block_reason": policy_ctx.get("block_reason"),
        "direct_marketing_policy_export_budget_w": policy_ctx.get("export_budget_w"),
        "direct_marketing_policy_charge_budget_w": policy_ctx.get("charge_budget_w"),
        "direct_marketing_policy_protected_reserve_wh": policy_ctx.get("protected_reserve_wh"),
        "direct_marketing_policy_sellable_wh": policy_ctx.get("sellable_wh"),
        "direct_marketing_policy_executor_gate": executor_gate,
        "direct_marketing_policy_effective_end_ts": executor_gate.get("effective_end_ts"),
        "direct_marketing_policy_export_constraint": policy_ctx.get("export_constraint"),
        "direct_marketing_mode": mode_name,
        "direct_marketing_owner": str(direct.get("plan_owner") or ""),
        "direct_marketing_contract_version": DIRECT_MARKETING_CONTRACT_VERSION,
        "direct_marketing_action": action,
        "direct_marketing_window": window,
        "direct_marketing_owner_switch_cooldown_active": bool(owner_switch_ctx.get("active")),
        "direct_marketing_owner_switch_cooldown_s": owner_switch_ctx.get("hold_s"),
        "direct_marketing_owner_switch_cooldown_age_s": owner_switch_ctx.get("age_s"),
        "direct_marketing_owner_switch_cooldown_remaining_s": owner_switch_ctx.get("remaining_s"),
        "direct_marketing_owner_switch_previous_state": owner_switch_ctx.get("previous_state"),
        "direct_marketing_owner_switch_next_state": owner_switch_ctx.get("next_state"),
        "direct_marketing_reserve_floor_soc_pct": round(reserve_floor, 1),
        "direct_marketing_economics": economics,
    }

    if target_state in DIRECT_MARKETING_POLICY_EXPORT_STATES:
        if target_state == "HEADROOM_EXPORT" and policy_ctx.get("headroom_hold_active"):
            policy_economics = (
                policy_ctx.get("policy", {}).get("economics")
                if isinstance(policy_ctx.get("policy", {}).get("economics"), dict)
                else {}
            )
            reason = (
                "Direktvermarktung Policy: Headroom wird bis zum wirtschaftlich "
                "ausgewaehlten Export-/Aufnahmefenster freigehalten; Laden gesperrt"
            )
            auto_limit = charge_block_auto_limit(cfg, max_discharge_w, reason)
            result = {
                "state": "direct_marketing_eco_plus_headroom_hold",
                "mode": MODE_AUTO,
                "val": 0,
                "reason": reason,
                "storage_req_w": 0,
                "budget_w": 0,
                "auto_limit": auto_limit,
                "direct_marketing_headroom_hold_active": True,
                "direct_marketing_headroom_soc_ceiling_pct": policy_ctx.get("headroom_target_soc_pct"),
                "direct_marketing_headroom_deficit_wh": policy_ctx.get("headroom_deficit_wh"),
                "direct_marketing_headroom_forecast_surplus_wh": max(
                    0,
                    safe_int(policy_economics.get("forecast_absorption_wh"), 0),
                ),
            }
            result.update(common)
            result["direct_marketing_action"] = "policy_headroom_hold"
            return True, result

        soc = safe_float(live.get("SOC"), 0.0)
        ep_reserve_floor = ep_reserve_soc(cfg, live)
        if ep_reserve_floor > 0.0 and soc <= ep_reserve_floor:
            return True, None
        if soc <= reserve_floor + 0.2:
            return True, None
        if abs(safe_float(live.get("Wallbox_Power"), 0.0)) > 250.0:
            return True, None
        base_export_w = min(
            max(0, safe_int(policy_ctx.get("export_budget_w"), 0)),
            max(0, safe_int(max_discharge_w, 0)),
        )
        max_export_w = min(
            max(0, safe_int(max_discharge_w, 0)),
            base_export_w,
        )
        export_headroom = predump_grid_export_headroom(cfg, live)
        if export_headroom.get("limited"):
            max_export_w = min(max_export_w, safe_int(export_headroom.get("discharge_limit_w"), max_export_w))
        control = direct_marketing_export_control_w(
            cfg,
            live,
            previous_state or {},
            base_export_w,
            max_export_w,
        )
        export_w = safe_int(control.get("target_w"), 0)
        import_guard = direct_marketing_export_import_guard(cfg, live, export_w)
        if import_guard.get("blocked"):
            return True, None
        if export_w < 300:
            return True, None
        ramp_limited = export_w != safe_int(control.get("desired_w"), export_w)
        ramp_base_w = safe_int(control.get("previous_w"), 0)
        release_hold = bool(control.get("release_hold"))
        ramp_step_w = (
            safe_int(control.get("ramp_up_w"), 0)
            if export_w > ramp_base_w
            else safe_int(control.get("ramp_down_w"), 0)
        )
        local_deficit_w = safe_int(import_guard.get("local_deficit_w"), 0)
        reason = (
            "Direktvermarktung Policy: %s, Batterieeinspeisung nach Reserve-, Budget- und Netzpunktprüfung"
            % ("Headroom-Export" if target_state == "HEADROOM_EXPORT" else "Hochpreisfenster")
        )
        if local_deficit_w > 0 and export_w >= local_deficit_w:
            reason += f"; Netzwächter deckt lokale Last {local_deficit_w}W"
        if release_hold:
            reason += f"; Netzpunkt-Halteband hält {export_w}W"
        elif ramp_limited:
            reason += f"; Exportrampe {ramp_base_w}W -> {export_w}W (Schritt {ramp_step_w}W)"
        result = {
            "state": state,
            "mode": MODE_DISCH,
            "val": export_w,
            "reason": reason,
            "storage_req_w": 0,
            "budget_w": 0,
            "direct_marketing_export_target_w": export_headroom.get("target_w"),
            "direct_marketing_export_w": export_headroom.get("export_w"),
            "direct_marketing_export_headroom_w": export_headroom.get("headroom_w"),
            "direct_marketing_export_discharge_limit_w": export_headroom.get("discharge_limit_w"),
            "direct_marketing_export_local_deficit_w": import_guard.get("local_deficit_w"),
            "direct_marketing_export_grid_import_w": import_guard.get("grid_import_w"),
            "direct_marketing_export_import_guard_w": import_guard.get("import_guard_w"),
            "direct_marketing_export_base_w": control.get("base_w"),
            "direct_marketing_export_desired_w": control.get("desired_w"),
            "direct_marketing_export_min_grid_export_w": control.get("min_grid_export_w"),
            "direct_marketing_export_netpoint_deadband_w": control.get("netpoint_deadband_w"),
            "direct_marketing_export_netpoint_release_margin_w": control.get("netpoint_release_margin_w"),
            "direct_marketing_export_grid_export_w": control.get("grid_export_w"),
            "direct_marketing_export_surplus_w": control.get("export_surplus_w"),
            "direct_marketing_export_grid_error_w": control.get("grid_error_w"),
            "direct_marketing_export_required_by_grid_w": control.get("required_by_grid_w"),
            "direct_marketing_export_required_by_load_w": control.get("required_by_load_w"),
            "direct_marketing_ramp_limited": ramp_limited,
            "direct_marketing_netpoint_release_hold": release_hold,
            "direct_marketing_ramp_base_w": ramp_base_w,
            "direct_marketing_ramp_step_w": ramp_step_w,
        }
        result.update(common)
        return True, result

    if target_state == "FORCE_CHARGE_PV":
        control_window = window
        fallback_target_soc = safe_float(
            reserve.get("target_soc_pct"),
            safe_float(cfg.get("storage_target_soc"), 100.0),
        )
        target_soc = safe_float((control_window or {}).get("target_soc_pct"), -1.0)
        if target_soc <= 0.0:
            target_soc = safe_float((control_window or {}).get("soc_ceiling_pct"), fallback_target_soc)
        target_soc = max(0.0, min(100.0, target_soc))
        target_hysteresis = max(0.2, safe_float(cfg.get("direct_marketing_target_hysteresis_pct"), 0.7))
        if safe_float(live.get("SOC"), 0.0) >= target_soc - target_hysteresis:
            return True, None
        control = direct_marketing_pv_store_control_w(
            cfg,
            live,
            control_window,
            flags,
            max_charge_w,
            now_s,
        )
        release_hold_ctx: Dict[str, Any] = {"active": False}
        if control.get("blocked"):
            release_hold_ctx = direct_marketing_pv_store_release_hold_w(
                cfg,
                live,
                previous_state,
                control,
                control_window,
                str((control_window or {}).get("action") or "eco_plus_store_pv_candidate"),
                now_s,
                max_charge_w,
            )
            if not release_hold_ctx.get("active"):
                return True, None
            control = dict(control)
            control["blocked"] = False
            control["charge_w"] = safe_int(release_hold_ctx.get("charge_w"), 0)
            control["release_hold_active"] = True
        external_derating = direct_marketing_external_derating_context(live, cfg, now_s)
        export_limit_owner = bool(control.get("external_export_owner_active"))
        requested_charge_w = min(
            max(0, safe_int(policy_ctx.get("charge_budget_w"), 0)),
            max(0, safe_int(max_charge_w, 0)),
            max(0, safe_int(control.get("charge_w"), 0)),
        )
        curve_catchup_ctx: Dict[str, Any] = {"active": False}
        if export_limit_owner and isinstance(plan, dict):
            curve_catchup_cap_w = min(
                max(0, safe_int(max_charge_w, 0)),
                max(0, safe_int(policy_ctx.get("charge_budget_w"), 0)),
            )
            control_max_w = safe_int(control.get("max_w"), 0)
            if control_max_w > 0:
                curve_catchup_cap_w = min(curve_catchup_cap_w, control_max_w)
            curve_catchup_ctx = direct_marketing_pv_store_curve_catchup_w(
                cfg,
                live,
                plan,
                control_window,
                curve_catchup_cap_w,
                now_s,
            )
            if (
                curve_catchup_ctx.get("active")
                and safe_int(curve_catchup_ctx.get("charge_w"), 0) > requested_charge_w
            ):
                requested_charge_w = safe_int(curve_catchup_ctx.get("charge_w"), requested_charge_w)
        ramp_ctx = direct_marketing_pv_store_ramped_power_w(
            cfg,
            live,
            previous_state,
            requested_charge_w,
            now_s,
        )
        charge_w = safe_int(ramp_ctx.get("charge_w"), 0)
        ramp_limited = bool(ramp_ctx.get("ramp_limited"))
        export_limit_ramp_bypass = False
        export_limit_bypass_threshold_w = max(
            0,
            safe_int(
                flags.get("pv_store_export_limit_ramp_bypass_w"),
                safe_int(cfg.get("direct_marketing_pv_store_export_limit_ramp_bypass_w"), 300),
            ),
        )
        if bool(control.get("export_limit_guard_active")) and safe_int(control.get("export_over_limit_w"), 0) >= export_limit_bypass_threshold_w:
            absorb_w = safe_int(control.get("export_absorb_target_w"), 0)
            accelerated_w = min(requested_charge_w, max(charge_w, absorb_w))
            accelerated_w = predump_round_budget_w(cfg, accelerated_w)
            if accelerated_w > charge_w:
                charge_w = accelerated_w
                export_limit_ramp_bypass = True
                ramp_limited = False

        if charge_w < 300:
            return True, None
        execution_constraint = {
            "hard": bool(control.get("hard_export_limit_active")),
            "limit_w": control.get("hard_export_limit_w"),
            "scope": control.get("export_constraint_scope"),
        }
        export_execution = direct_marketing_export_execution_contract(
            cfg,
            live,
            execution_constraint,
            external_derating=external_derating,
            storage_absorption_w=charge_w,
            storage_absorption_cap_w=min(
                max(0, safe_int(max_charge_w, 0)),
                max(0, safe_int(control.get("offer_w"), 0)),
            ),
            unavoidable_export_w=safe_int(control.get("unavoidable_export_w"), 0),
            now_s=now_s,
        )
        export_limit_owner = bool(
            export_execution.get("external_owner_confirmed")
            if execution_constraint.get("hard")
            else control.get("external_export_owner_active")
        )
        execution = "hard_charge"
        storage_mode = MODE_CHRG
        auto_limit = None
        reason = "Direktvermarktung Policy: Negativ-/Niedrigpreisfenster, PV-Speicherladung priorisiert"
        if ramp_limited:
            if export_limit_ramp_bypass:
                reason += "; PV-Laderampe umgangen um Einspeisebegrenzung abzufangen -> %dW" % charge_w
            else:
                reason += (
                    "; PV-Laderampe %dW -> %dW (Schritt %dW)"
                    % (
                        safe_int(ramp_ctx.get("ramp_base_w"), 0),
                        charge_w,
                        safe_int(ramp_ctx.get("ramp_step_w"), 0),
                    )
                )
        if export_limit_owner:
            execution = "auto_limit"
            storage_mode = MODE_AUTO
            reason += "; externe Einspeisebegrenzung bleibt E3DC-autonom, Storage Manager setzt nur den EMS-Laderahmen"
            auto_limit = charge_cap_auto_limit(cfg, charge_w, max_discharge_w, reason)
        elif bool(control.get("hard_export_limit_active")):
            reason += "; externes 0-W-Exportlimit nicht live bestätigt, PV-Aufnahme erfolgt best-effort"
        if export_execution.get("state") == "external_confirmed":
            reason += "; externes Exportlimit und Netzpunktwirkung live bestätigt"
        elif export_execution.get("state") == "external_owner_grid_violation":
            reason += "; Exportlimit gemeldet, Netzpunkt noch %.0fW über Toleranz" % safe_float(
                export_execution.get("violation_w"),
                0.0,
            )
        elif export_execution.get("state") == "violated_unavoidable":
            reason += "; WARNUNG: Exportlimit physisch nicht einhaltbar, Restexport %.0fW" % safe_float(
                export_execution.get("violation_w"),
                0.0,
            )
        if curve_catchup_ctx.get("active"):
            reason += (
                "; Kurvenrückstand %.1f%% -> EMS-Zielrahmen %.0fW"
                % (
                    safe_float(curve_catchup_ctx.get("gap_pct"), 0.0),
                    safe_float(curve_catchup_ctx.get("charge_w"), 0.0),
                )
            )
        result = {
            "state": state,
            "mode": storage_mode,
            "val": charge_w,
            "reason": reason,
            "storage_req_w": charge_w,
            "budget_w": 0,
            "auto_limit": auto_limit,
            "direct_marketing_pv_store_execution": execution,
            "direct_marketing_pv_store_auto_limit_active": bool(auto_limit),
            "direct_marketing_pv_store_external_export_owner": export_limit_owner,
            "direct_marketing_hard_export_owner_confirmed": bool(export_execution.get("external_owner_confirmed")),
            "direct_marketing_export_execution": export_execution,
            "direct_marketing_export_execution_state": export_execution.get("state"),
            "direct_marketing_export_execution_claim": export_execution.get("claim"),
            "direct_marketing_export_compliance_confirmed": bool(export_execution.get("compliance_confirmed")),
            "direct_marketing_export_violation_w": safe_int(export_execution.get("violation_w"), 0),
            "direct_marketing_pv_store_curve_catchup_active": bool(curve_catchup_ctx.get("active")),
            "direct_marketing_pv_store_curve_catchup_w": safe_int(curve_catchup_ctx.get("charge_w"), 0),
            "direct_marketing_pv_store_curve_catchup_source": curve_catchup_ctx.get("source"),
            "direct_marketing_pv_store_curve_catchup_gap_pct": safe_float(curve_catchup_ctx.get("gap_pct"), 0.0),
            "direct_marketing_pv_store_curve_soc_pct": curve_catchup_ctx.get("curve_soc_pct"),
            "direct_marketing_pv_store_curve_target_soc_pct": curve_catchup_ctx.get("curve_target_soc_pct"),
            "direct_marketing_pv_store_w": charge_w,
            "direct_marketing_pv_store_offer_w": control.get("offer_w"),
            "direct_marketing_pv_store_max_w": control.get("max_w"),
            "direct_marketing_pv_store_surplus_w": control.get("physical_surplus_w"),
            "direct_marketing_pv_store_grid_import_w": control.get("grid_import_w"),
            "direct_marketing_pv_store_grid_export_w": control.get("grid_export_w"),
            "direct_marketing_pv_store_import_guard_w": control.get("import_guard_w"),
            "direct_marketing_pv_store_min_surplus_w": control.get("min_surplus_w"),
            "direct_marketing_pv_store_requested_w": requested_charge_w,
            "direct_marketing_pv_store_estimated_offer_w": control.get("estimated_offer_w"),
            "direct_marketing_pv_store_pv_safe_cap_w": control.get("pv_safe_cap_w"),
            "direct_marketing_pv_store_self_reference_limited": bool(control.get("self_reference_limited")),
            "direct_marketing_export_constraint_class": control.get("export_constraint_class"),
            "direct_marketing_hard_export_limit_active": bool(control.get("hard_export_limit_active")),
            "direct_marketing_hard_export_limit_w": control.get("hard_export_limit_w"),
            "direct_marketing_export_constraint_scope": control.get("export_constraint_scope"),
            "direct_marketing_pv_export_allowed": bool(control.get("pv_export_allowed")),
            "direct_marketing_pv_store_export_limit_active": bool(control.get("export_limit_active")),
            "direct_marketing_pv_store_export_limit_guard_active": bool(control.get("export_limit_guard_active")),
            "direct_marketing_pv_store_export_limit_w": control.get("export_limit_w"),
            "direct_marketing_pv_store_export_limit_guard_w": control.get("export_limit_guard_w"),
            "direct_marketing_pv_store_export_over_limit_w": control.get("export_over_limit_w"),
            "direct_marketing_pv_store_export_absorb_target_w": control.get("export_absorb_target_w"),
            "direct_marketing_pv_store_unavoidable_export_w": control.get("unavoidable_export_w"),
            "direct_marketing_pv_store_export_limit_ramp_bypass": export_limit_ramp_bypass,
            "direct_marketing_pv_store_ramp_limited": ramp_limited,
            "direct_marketing_pv_store_ramp_base_w": safe_int(ramp_ctx.get("ramp_base_w"), 0),
            "direct_marketing_pv_store_ramp_step_w": safe_int(ramp_ctx.get("ramp_step_w"), 0),
            "direct_marketing_pv_store_resync_active": bool(ramp_ctx.get("resync_active")),
            "direct_marketing_pv_store_resync_reason": ramp_ctx.get("resync_reason"),
            "direct_marketing_pv_store_resync_gap_w": safe_int(ramp_ctx.get("resync_gap_w"), 0),
            "direct_marketing_pv_store_resync_threshold_w": safe_int(ramp_ctx.get("resync_threshold_w"), 0),
            "direct_marketing_pv_store_observed_charge_w": safe_int(ramp_ctx.get("observed_charge_w"), 0),
            "direct_marketing_external_derating_active": bool(external_derating.get("active")),
            "direct_marketing_external_derating_source": control.get("external_derating_source", external_derating.get("source")),
            "direct_marketing_external_derating_limit_w": control.get("external_derating_limit_w", external_derating.get("limit_w")),
            "direct_marketing_external_derating_ac_power_limit_w": control.get("external_derating_ac_power_limit_w", external_derating.get("ac_power_limit_w")),
            "direct_marketing_external_derating_power_w": control.get("external_derating_power_w", external_derating.get("derate_at_power_w")),
            "direct_marketing_external_derating_percent": control.get("external_derating_percent", external_derating.get("derate_at_percent")),
            "direct_marketing_pv_store_dc_only": bool(control.get("dc_only")),
            "direct_marketing_pv_store_external_ac_guard_w": control.get("external_ac_guard_w"),
            "direct_marketing_pv_total_w": control.get("pv_total_w"),
            "direct_marketing_pv_e3dc_w": control.get("pv_e3dc_w"),
            "direct_marketing_pv_external_ac_w": control.get("pv_external_ac_w"),
            "direct_marketing_pv_source": control.get("pv_source"),
            "direct_marketing_pv_store_dc_surplus_w": control.get("dc_surplus_w"),
            "direct_marketing_pv_store_local_load_after_external_w": control.get("local_load_after_external_w"),
        }
        result.update(common)
        return True, result

    return True, None


def direct_marketing_decision(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    now_s: float,
    max_charge_w: int,
    max_discharge_w: int,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    direct = direct_marketing_plan(plan)
    policy_handled, policy_result = direct_marketing_storage_decision(
        cfg,
        live,
        direct,
        now_s,
        max_charge_w,
        max_discharge_w,
        previous_state,
        plan,
    )
    if policy_handled:
        return policy_result
    window = direct_marketing_current_window(direct, now_s)
    if not window:
        return None

    mode = str(direct.get("mode") or "").strip().lower()
    flags = direct.get("flags") if isinstance(direct.get("flags"), dict) else {}
    economics = direct.get("economics") if isinstance(direct.get("economics"), dict) else {}
    reserve = direct.get("reserve") if isinstance(direct.get("reserve"), dict) else {}
    hold_ctx = direct_marketing_export_hold_context(cfg, direct, window, previous_state)
    pv_store_hold_ctx = direct_marketing_pv_store_hold_context(cfg, previous_state, now_s)
    contract_errors = direct_marketing_contract_errors(cfg, direct, now_s)
    blocking_contract_errors = [err for err in contract_errors if err != "commands_not_allowed"]
    if blocking_contract_errors:
        return None
    if contract_errors and not bool(hold_ctx.get("allowed")):
        return None
    if hold_ctx.get("allowed"):
        window = hold_ctx.get("window") if isinstance(hold_ctx.get("window"), dict) else window

    action = str(window.get("action") or "")
    if action not in DIRECT_MARKETING_CONTROLLABLE_ACTIONS:
        return None
    state = DIRECT_MARKETING_ACTION_STATES.get(action)
    if not state:
        return None
    owner_switch_ctx = direct_marketing_owner_switch_cooldown(
        cfg,
        previous_state,
        state,
        window,
        live,
        now_s,
    )
    if owner_switch_ctx.get("active"):
        return None

    if action in DIRECT_MARKETING_HEADROOM_ACTIONS:
        if mode not in ("eco", "eco_plus"):
            return None
        if not bool(flags.get("pv_store_enable")):
            return None
        window_reason = str(window.get("reason") or "")
        negative_headroom = bool(window.get("negative_headroom_limited")) or window_reason == "negative_price_headroom"
        low_price_headroom = (
            (bool(window.get("pv_store_headroom_limited")) and not negative_headroom)
            or window_reason == "low_price_headroom"
        )
        if not bool(flags.get("negative_headroom_enable")):
            return None
        if negative_headroom and not bool(flags.get("negative_price_no_export")):
            return None
        if low_price_headroom and not bool(flags.get("low_price_headroom_enable", True)):
            return None
    elif action in DIRECT_MARKETING_PV_STORE_ACTIONS:
        if mode not in ("eco", "eco_plus"):
            return None
        if not bool(flags.get("pv_store_enable")):
            return None
        pv_store_profit_ok = bool(
            economics.get("pv_shift_profit_ok")
            or str(window.get("reason") or "") == "negative_price"
            or bool(window.get("headroom_limited"))
            or direct_marketing_pv_store_threshold_ok(window)
        )
        if not pv_store_profit_ok:
            return None
    elif action == "eco_plus_export_candidate":
        if mode != "eco_plus":
            return None
        if not bool(flags.get("export_enable")) or safe_int(flags.get("max_export_w"), 0) <= 0:
            return None
        if not bool(economics.get("pv_shift_profit_ok")) and not bool(hold_ctx.get("allowed")):
            return None
    elif action in ("arbitrage_grid_charge_candidate", "arbitrage_export_candidate"):
        return None

    soc = safe_float(live.get("SOC"), 0.0)
    reserve_floor = max(
        safe_float(reserve.get("effective_min_soc_pct"), 0.0),
        ep_reserve_soc(cfg, live),
    )
    common = {
        "priority": "direct_marketing",
        "protected": True,
        "direct_marketing_active": True,
        "direct_marketing_mode": mode,
        "direct_marketing_owner": str(direct.get("plan_owner") or ""),
        "direct_marketing_contract_version": DIRECT_MARKETING_CONTRACT_VERSION,
        "direct_marketing_action": action,
        "direct_marketing_window": window,
        "direct_marketing_hold_active": bool(hold_ctx.get("allowed")),
        "direct_marketing_profit_hold_ct_per_kwh": hold_ctx.get("profit_hold_ct"),
        "direct_marketing_margin_hold_pct": hold_ctx.get("margin_hold_pct"),
        "direct_marketing_hold_profit_floor_ct_per_kwh": hold_ctx.get("profit_floor_ct"),
        "direct_marketing_hold_margin_floor_pct": hold_ctx.get("margin_floor_pct"),
        "direct_marketing_pv_store_hold_active": bool(pv_store_hold_ctx.get("active")),
        "direct_marketing_pv_store_min_hold_s": pv_store_hold_ctx.get("min_s"),
        "direct_marketing_pv_store_state_age_s": pv_store_hold_ctx.get("age_s"),
        "direct_marketing_pv_store_hold_remaining_s": pv_store_hold_ctx.get("remaining_s"),
        "direct_marketing_owner_switch_cooldown_active": bool(owner_switch_ctx.get("active")),
        "direct_marketing_owner_switch_cooldown_s": owner_switch_ctx.get("hold_s"),
        "direct_marketing_owner_switch_cooldown_age_s": owner_switch_ctx.get("age_s"),
        "direct_marketing_owner_switch_cooldown_remaining_s": owner_switch_ctx.get("remaining_s"),
        "direct_marketing_owner_switch_previous_state": owner_switch_ctx.get("previous_state"),
        "direct_marketing_owner_switch_next_state": owner_switch_ctx.get("next_state"),
        "direct_marketing_reserve_floor_soc_pct": round(reserve_floor, 1),
        "direct_marketing_profit_ct_per_kwh": (
            economics.get("pv_shift_spread_ct_per_kwh")
            if action in DIRECT_MARKETING_PV_STORE_ACTIONS or action in DIRECT_MARKETING_HEADROOM_ACTIONS or action == "eco_plus_export_candidate"
            else economics.get("grid_spread_ct_per_kwh")
        ),
        "direct_marketing_economics": economics,
    }

    if action in DIRECT_MARKETING_HEADROOM_ACTIONS:
        headroom_ceiling = safe_float(window.get("soc_ceiling_pct"), 100.0)
        hysteresis = max(0.2, safe_float(cfg.get("direct_marketing_target_hysteresis_pct"), 0.7))
        if soc <= reserve_floor + 0.2:
            return None
        if soc <= headroom_ceiling - hysteresis:
            return None
        next_start_ts = safe_int(window.get("negative_headroom_next_start_ts"), 0)
        next_start_txt = ""
        if next_start_ts > 0:
            try:
                next_start_txt = datetime.datetime.fromtimestamp(next_start_ts / 1000.0).strftime("%H:%M")
            except Exception:
                next_start_txt = ""
        forecast_wh = safe_float(window.get("negative_headroom_forecast_surplus_wh"), 0.0)
        next_kind = str(window.get("pv_store_headroom_next_reason") or "")
        headroom_label = "Negativpreisfenster" if bool(window.get("negative_headroom_limited")) else "PV-Speicherfenster"
        reason = (
            f"Direktvermarktung Eco+: {headroom_label} steht bevor, "
            "Speicherladung wird zugunsten von PV-Headroom gesperrt"
        )
        if next_start_txt:
            reason += f"; nächstes {headroom_label} ab {next_start_txt}"
        if next_kind == "low_price":
            reason += "; Niedrigpreis-/EEG-Schwelle priorisiert"
        reason += (
            "; SoC %.1f%% über Headroom-Ziel %.1f%%, prognostizierte Aufnahme %.0fWh"
            % (soc, headroom_ceiling, forecast_wh)
        )
        auto_limit = charge_block_auto_limit(cfg, max_discharge_w, reason)
        result = {
            "state": state,
            "mode": MODE_AUTO,
            "val": 0,
            "reason": reason,
            "storage_req_w": 0,
            "budget_w": 0,
            "auto_limit": auto_limit,
            "direct_marketing_headroom_hold_active": True,
            "direct_marketing_headroom_soc_ceiling_pct": round(headroom_ceiling, 1),
            "direct_marketing_headroom_next_start_ts": next_start_ts,
            "direct_marketing_headroom_window_min": safe_float(window.get("negative_headroom_window_min"), 0.0),
            "direct_marketing_headroom_forecast_surplus_wh": round(forecast_wh, 0),
            "direct_marketing_headroom_required_pct": safe_float(window.get("negative_headroom_required_pct"), 0.0),
        }
        result.update(common)
        return result

    if action in DIRECT_MARKETING_PV_STORE_ACTIONS:
        fallback_target_soc = safe_float(
            reserve.get("target_soc_pct"),
            safe_float(cfg.get("storage_target_soc"), 100.0),
        )
        raw_target_soc = safe_float(window.get("target_soc_pct"), -1.0)
        raw_ceiling_soc = safe_float(window.get("soc_ceiling_pct"), -1.0)
        target_soc_fallback_active = raw_target_soc <= 0.0 and raw_ceiling_soc <= 0.0
        if raw_target_soc > 0.0:
            target_soc = raw_target_soc
        elif raw_ceiling_soc > 0.0:
            target_soc = raw_ceiling_soc
        else:
            target_soc = fallback_target_soc
        target_soc = max(0.0, min(100.0, target_soc))
        hysteresis = max(0.2, safe_float(cfg.get("direct_marketing_target_hysteresis_pct"), 0.7))
        if soc >= target_soc - hysteresis:
            return None
        control = direct_marketing_pv_store_control_w(cfg, live, window, flags, max_charge_w, now_s)
        release_hold_ctx: Dict[str, Any] = {"active": False}
        if control.get("blocked"):
            release_hold_ctx = direct_marketing_pv_store_release_hold_w(
                cfg,
                live,
                previous_state,
                control,
                window,
                action,
                now_s,
                max_charge_w,
            )
            if not release_hold_ctx.get("active"):
                return None
            control = dict(control)
            control["blocked"] = False
            control["charge_w"] = safe_int(release_hold_ctx.get("charge_w"), 0)
            control["release_hold_active"] = True
        external_export_owner = bool(control.get("external_export_owner_active"))
        curve_catchup_ctx: Dict[str, Any] = {"active": False}
        requested_charge_w = safe_int(control.get("charge_w"), 0)
        if external_export_owner:
            curve_catchup_cap_w = max_charge_w
            control_max_w = safe_int(control.get("max_w"), 0)
            if control_max_w > 0:
                curve_catchup_cap_w = min(curve_catchup_cap_w, control_max_w)
            curve_catchup_ctx = direct_marketing_pv_store_curve_catchup_w(
                cfg,
                live,
                plan,
                window,
                curve_catchup_cap_w,
                now_s,
            )
            if (
                curve_catchup_ctx.get("active")
                and safe_int(curve_catchup_ctx.get("charge_w"), 0) > requested_charge_w
            ):
                requested_charge_w = safe_int(curve_catchup_ctx.get("charge_w"), requested_charge_w)
        ramp_ctx = direct_marketing_pv_store_ramped_power_w(
            cfg,
            live,
            previous_state,
            requested_charge_w,
            now_s,
        )
        charge_w = safe_int(ramp_ctx.get("charge_w"), 0)
        ramp_limited = bool(ramp_ctx.get("ramp_limited"))
        ramp_base_w = safe_int(ramp_ctx.get("ramp_base_w"), 0)
        ramp_step_w = safe_int(ramp_ctx.get("ramp_step_w"), 0)
        export_limit_ramp_bypass = False
        export_limit_bypass_threshold_w = max(
            0,
            safe_int(
                flags.get("pv_store_export_limit_ramp_bypass_w"),
                safe_int(cfg.get("direct_marketing_pv_store_export_limit_ramp_bypass_w"), 300),
            ),
        )
        if bool(control.get("export_limit_guard_active")) and safe_int(control.get("export_over_limit_w"), 0) >= export_limit_bypass_threshold_w:
            absorb_w = safe_int(control.get("export_absorb_target_w"), 0)
            accelerated_w = min(requested_charge_w, max(charge_w, absorb_w))
            accelerated_w = predump_round_budget_w(cfg, accelerated_w)
            if accelerated_w > charge_w:
                charge_w = accelerated_w
                ramp_limited = charge_w < requested_charge_w
                export_limit_ramp_bypass = True
        if charge_w < 300:
            return None
        external_derating = direct_marketing_external_derating_context(live, cfg, now_s)
        execution_constraint = {
            "hard": bool(control.get("hard_export_limit_active")),
            "limit_w": control.get("hard_export_limit_w"),
            "scope": control.get("export_constraint_scope"),
        }
        export_execution = direct_marketing_export_execution_contract(
            cfg,
            live,
            execution_constraint,
            external_derating=external_derating,
            storage_absorption_w=charge_w,
            storage_absorption_cap_w=min(
                max(0, safe_int(max_charge_w, 0)),
                max(0, safe_int(control.get("offer_w"), 0)),
            ),
            unavoidable_export_w=safe_int(control.get("unavoidable_export_w"), 0),
            now_s=now_s,
        )
        external_export_owner = bool(
            export_execution.get("external_owner_confirmed")
            if execution_constraint.get("hard")
            else control.get("external_export_owner_active")
        )
        reason = (
            "Direktvermarktung Eco+: niedriger Nettoeinspeisewert, "
            "PV-Überschuss wird prognosebasiert in den Speicher geführt"
        )
        if window.get("pv_store_threshold_ct") is not None:
            reason += "; Schwelle %.2f ct/kWh" % safe_float(window.get("pv_store_threshold_ct"), 0.0)
        if window.get("net_sell_ct") is not None:
            reason += "; Nettoerlös %.2f ct/kWh" % safe_float(window.get("net_sell_ct"), 0.0)
        if target_soc_fallback_active:
            reason += "; Planfenster ohne Ziel-SoC, nutze Speicherziel %.1f%%" % target_soc
        reason += (
            "; Ziel %.1f%%, Netzimport-Wächter %dW"
            % (target_soc, safe_int(control.get("import_guard_w"), 0))
        )
        if control.get("dc_only"):
            reason += "; DC-only %.0fW E3DC-Überschuss" % safe_float(control.get("dc_surplus_w"), 0.0)
        if pv_store_hold_ctx.get("active"):
            reason += "; Mindesthaltezeit %.0fs aktiv" % safe_float(pv_store_hold_ctx.get("min_s"), 0.0)
        if release_hold_ctx.get("active"):
            reason += (
                "; EMS-Release-Hysterese %.0fs hält %.0fW trotz Schwelle %s"
                % (
                    safe_float(release_hold_ctx.get("remaining_s"), 0.0),
                    safe_float(charge_w, 0.0),
                    str(release_hold_ctx.get("blocker") or "pv_store_below_min"),
                )
            )
        if ramp_limited:
            reason += f"; PV-Laderampe {ramp_base_w}W -> {charge_w}W (Schritt {ramp_step_w}W)"
        if curve_catchup_ctx.get("active"):
            reason += (
                "; Kurvenrückstand %.1f%% -> EMS-Zielrahmen %.0fW"
                % (
                    safe_float(curve_catchup_ctx.get("gap_pct"), 0.0),
                    safe_float(curve_catchup_ctx.get("charge_w"), 0.0),
                )
            )
        if export_limit_ramp_bypass:
            reason += (
                "; Exportlimit %.0fW: Laderampe auf %.0fW beschleunigt"
                % (
                    safe_float(control.get("export_limit_w"), 0.0),
                    safe_float(charge_w, 0.0),
                )
            )
        elif bool(control.get("export_limit_guard_active")):
            reason += (
                "; Exportlimit %.0fW: Restexport %.0fW über Wächter"
                % (
                    safe_float(control.get("export_limit_w"), 0.0),
                    safe_float(control.get("export_over_limit_w"), 0.0),
                )
            )
        if bool(control.get("external_derating_active")):
            reason += (
                "; E3DC/LUOX-Abregelung aktiv, Limit %.0fW"
                % safe_float(control.get("external_derating_limit_w"), 0.0)
            )
        if ramp_ctx.get("resync_active"):
            reason += (
                "; PV-Laderampe resynchronisiert "
                f"{ramp_base_w}W -> {charge_w}W"
            )
        pv_store_execution = "hard_charge"
        auto_limit = None
        mode = MODE_CHRG
        if external_export_owner:
            pv_store_execution = "auto_limit"
            mode = MODE_AUTO
            reason += (
                "; externe Einspeisebegrenzung bleibt E3DC-autonom, "
                "Storage Manager setzt nur den EMS-Laderahmen"
            )
            auto_limit = charge_cap_auto_limit(cfg, charge_w, max_discharge_w, reason)
        elif bool(control.get("hard_export_limit_active")):
            reason += "; externes 0-W-Exportlimit nicht live bestätigt, PV-Aufnahme erfolgt best-effort"
        if export_execution.get("state") == "external_confirmed":
            reason += "; externes Exportlimit und Netzpunktwirkung live bestätigt"
        elif export_execution.get("state") == "external_owner_grid_violation":
            reason += "; Exportlimit gemeldet, Netzpunkt noch %.0fW über Toleranz" % safe_float(
                export_execution.get("violation_w"),
                0.0,
            )
        elif export_execution.get("state") == "violated_unavoidable":
            reason += "; WARNUNG: Exportlimit physisch nicht einhaltbar, Restexport %.0fW" % safe_float(
                export_execution.get("violation_w"),
                0.0,
            )
        result = {
            "state": state,
            "mode": mode,
            "val": charge_w,
            "reason": reason,
            "storage_req_w": charge_w,
            "budget_w": 0,
            "auto_limit": auto_limit,
            "direct_marketing_pv_store_execution": pv_store_execution,
            "direct_marketing_pv_store_auto_limit_active": bool(auto_limit),
            "direct_marketing_pv_store_external_export_owner": external_export_owner,
            "direct_marketing_hard_export_owner_confirmed": bool(export_execution.get("external_owner_confirmed")),
            "direct_marketing_export_execution": export_execution,
            "direct_marketing_export_execution_state": export_execution.get("state"),
            "direct_marketing_export_execution_claim": export_execution.get("claim"),
            "direct_marketing_export_compliance_confirmed": bool(export_execution.get("compliance_confirmed")),
            "direct_marketing_export_violation_w": safe_int(export_execution.get("violation_w"), 0),
            "direct_marketing_target_soc_pct": round(target_soc, 1),
            "direct_marketing_pv_store_w": charge_w,
            "direct_marketing_pv_store_offer_w": control.get("offer_w"),
            "direct_marketing_pv_store_max_w": control.get("max_w"),
            "direct_marketing_pv_store_surplus_w": control.get("physical_surplus_w"),
            "direct_marketing_pv_store_grid_import_w": control.get("grid_import_w"),
            "direct_marketing_pv_store_grid_export_w": control.get("grid_export_w"),
            "direct_marketing_pv_store_import_guard_w": control.get("import_guard_w"),
            "direct_marketing_pv_store_min_surplus_w": control.get("min_surplus_w"),
            "direct_marketing_pv_store_requested_w": requested_charge_w,
            "direct_marketing_pv_store_target_fallback_active": bool(target_soc_fallback_active),
            "direct_marketing_pv_store_estimated_offer_w": control.get("estimated_offer_w"),
            "direct_marketing_pv_store_pv_safe_cap_w": control.get("pv_safe_cap_w"),
            "direct_marketing_pv_store_self_reference_limited": bool(control.get("self_reference_limited")),
            "direct_marketing_export_constraint_class": control.get("export_constraint_class"),
            "direct_marketing_hard_export_limit_active": bool(control.get("hard_export_limit_active")),
            "direct_marketing_hard_export_limit_w": control.get("hard_export_limit_w"),
            "direct_marketing_export_constraint_scope": control.get("export_constraint_scope"),
            "direct_marketing_pv_export_allowed": bool(control.get("pv_export_allowed")),
            "direct_marketing_pv_store_export_limit_active": bool(control.get("export_limit_active")),
            "direct_marketing_pv_store_export_limit_guard_active": bool(control.get("export_limit_guard_active")),
            "direct_marketing_pv_store_export_limit_w": control.get("export_limit_w"),
            "direct_marketing_pv_store_export_limit_guard_w": control.get("export_limit_guard_w"),
            "direct_marketing_pv_store_export_over_limit_w": control.get("export_over_limit_w"),
            "direct_marketing_pv_store_export_absorb_target_w": control.get("export_absorb_target_w"),
            "direct_marketing_pv_store_unavoidable_export_w": control.get("unavoidable_export_w"),
            "direct_marketing_external_derating_active": bool(control.get("external_derating_active")),
            "direct_marketing_external_derating_source": control.get("external_derating_source"),
            "direct_marketing_external_derating_limit_w": control.get("external_derating_limit_w"),
            "direct_marketing_external_derating_ac_power_limit_w": control.get("external_derating_ac_power_limit_w"),
            "direct_marketing_external_derating_power_w": control.get("external_derating_power_w"),
            "direct_marketing_external_derating_percent": control.get("external_derating_percent"),
            "direct_marketing_pv_store_export_limit_ramp_bypass": export_limit_ramp_bypass,
            "direct_marketing_pv_store_ramp_limited": ramp_limited,
            "direct_marketing_pv_store_ramp_base_w": ramp_base_w,
            "direct_marketing_pv_store_ramp_step_w": ramp_step_w,
            "direct_marketing_pv_store_curve_catchup_active": bool(curve_catchup_ctx.get("active")),
            "direct_marketing_pv_store_curve_catchup_w": safe_int(curve_catchup_ctx.get("charge_w"), 0),
            "direct_marketing_pv_store_curve_catchup_source": curve_catchup_ctx.get("source"),
            "direct_marketing_pv_store_curve_catchup_raw_w": safe_int(curve_catchup_ctx.get("raw_w"), 0),
            "direct_marketing_pv_store_curve_catchup_gap_pct": safe_float(curve_catchup_ctx.get("gap_pct"), 0.0),
            "direct_marketing_pv_store_curve_soc_pct": curve_catchup_ctx.get("curve_soc_pct"),
            "direct_marketing_pv_store_curve_target_soc_pct": curve_catchup_ctx.get("curve_target_soc_pct"),
            "direct_marketing_pv_store_curve_target_ts": safe_float(curve_catchup_ctx.get("curve_target_ts"), 0.0),
            "direct_marketing_pv_store_curve_window_target_soc_pct": curve_catchup_ctx.get("window_target_soc_pct"),
            "direct_marketing_pv_store_release_hold_active": bool(release_hold_ctx.get("active")),
            "direct_marketing_pv_store_release_hold_reason": release_hold_ctx.get("blocker"),
            "direct_marketing_pv_store_release_hold_remaining_s": safe_float(release_hold_ctx.get("remaining_s"), 0.0),
            "direct_marketing_pv_store_release_hold_previous_w": safe_int(release_hold_ctx.get("previous_w"), 0),
            "direct_marketing_pv_store_release_hold_offer_w": safe_int(release_hold_ctx.get("offer_w"), 0),
            "direct_marketing_pv_store_resync_active": bool(ramp_ctx.get("resync_active")),
            "direct_marketing_pv_store_resync_reason": ramp_ctx.get("resync_reason"),
            "direct_marketing_pv_store_resync_gap_w": safe_int(ramp_ctx.get("resync_gap_w"), 0),
            "direct_marketing_pv_store_resync_threshold_w": safe_int(ramp_ctx.get("resync_threshold_w"), 0),
            "direct_marketing_pv_store_observed_charge_w": safe_int(ramp_ctx.get("observed_charge_w"), 0),
            "direct_marketing_pv_store_dc_only": bool(control.get("dc_only")),
            "direct_marketing_pv_store_external_ac_guard_w": control.get("external_ac_guard_w"),
            "direct_marketing_pv_total_w": control.get("pv_total_w"),
            "direct_marketing_pv_e3dc_w": control.get("pv_e3dc_w"),
            "direct_marketing_pv_external_ac_w": control.get("pv_external_ac_w"),
            "direct_marketing_pv_source": control.get("pv_source"),
            "direct_marketing_pv_store_dc_surplus_w": control.get("dc_surplus_w"),
            "direct_marketing_pv_store_local_load_after_external_w": control.get("local_load_after_external_w"),
        }
        result.update(common)
        return result

    if action in DIRECT_MARKETING_GRID_ACTIONS:
        target_soc = safe_float(window.get("target_soc_pct"), safe_float(reserve.get("target_soc_pct"), 100.0))
        hysteresis = max(0.2, safe_float(cfg.get("direct_marketing_target_hysteresis_pct"), 0.7))
        if soc >= target_soc - hysteresis:
            return None
        charge_w = min(
            max_charge_w,
            safe_int(flags.get("max_grid_charge_w"), max_charge_w),
            safe_int(window.get("max_power_w"), max_charge_w),
        )
        room_w = grid_charge_room_w(cfg, live)
        if room_w is not None:
            charge_w = min(charge_w, room_w)
        if charge_w < 300:
            return None
        reason = (
            "Direktvermarktung Arbitrage: günstiges Netzladefenster, "
            "Wirtschaftlichkeit und Hausanschluss geprüft"
        )
        result = {
            "state": state,
            "mode": MODE_GRID,
            "val": charge_w,
            "reason": reason,
            "storage_req_w": charge_w,
            "budget_w": 0,
            "direct_marketing_target_soc_pct": round(target_soc, 1),
        }
        result.update(common)
        return result

    if action in DIRECT_MARKETING_EXPORT_ACTIONS:
        if soc <= reserve_floor + 0.2:
            return None
        if abs(safe_float(live.get("Wallbox_Power"), 0.0)) > 250.0:
            return None
        base_export_w = min(
            max_discharge_w,
            safe_int(flags.get("max_export_w"), max_discharge_w),
            safe_int(window.get("max_power_w"), max_discharge_w),
        )
        max_export_w = max_discharge_w
        export_headroom = predump_grid_export_headroom(cfg, live)
        if export_headroom.get("limited"):
            max_export_w = min(max_export_w, safe_int(export_headroom.get("discharge_limit_w"), max_export_w))
        control = direct_marketing_export_control_w(
            cfg,
            live,
            previous_state or {},
            base_export_w,
            max_export_w,
        )
        export_w = safe_int(control.get("target_w"), 0)
        import_guard = direct_marketing_export_import_guard(cfg, live, export_w)
        if import_guard.get("blocked"):
            return None
        ramp_limited = export_w != safe_int(control.get("desired_w"), export_w)
        ramp_base_w = safe_int(control.get("previous_w"), 0)
        release_hold = bool(control.get("release_hold"))
        ramp_step_w = (
            safe_int(control.get("ramp_up_w"), 0)
            if export_w > ramp_base_w
            else safe_int(control.get("ramp_down_w"), 0)
        )
        local_deficit_w = safe_int(import_guard.get("local_deficit_w"), 0)
        if export_w < 300:
            return None
        reason = (
            "Direktvermarktung %s: Hochpreisfenster, Batterieeinspeisung nach Reserve- und Wirtschaftlichkeitsprüfung"
            % ("Eco+" if mode == "eco_plus" else "Arbitrage")
        )
        if hold_ctx.get("allowed"):
            reason += (
                "; Profit-Hysterese haelt laufendes Fenster "
                f"(Spread {safe_float(hold_ctx.get('spread_ct'), 0.0):.2f} ct/kWh >= "
                f"Halteschwelle {safe_float(hold_ctx.get('profit_floor_ct'), 0.0):.2f} ct/kWh)"
            )
        if local_deficit_w > 0 and export_w >= local_deficit_w:
            reason += f"; Netzwaechter deckt lokale Last {local_deficit_w}W"
        if release_hold:
            reason += (
                "; Netzpunkt-Halteband haelt "
                f"{export_w}W (Export {safe_int(control.get('grid_export_w'), 0)}W, "
                f"Freigabe ab {safe_int(control.get('min_grid_export_w'), 0) + safe_int(control.get('netpoint_release_margin_w'), 0)}W)"
            )
        elif ramp_limited:
            reason += f"; Rampe {ramp_base_w}W -> {export_w}W (Schritt {ramp_step_w}W)"
        result = {
            "state": state,
            "mode": MODE_DISCH,
            "val": export_w,
            "reason": reason,
            "storage_req_w": 0,
            "budget_w": 0,
            "direct_marketing_export_target_w": export_headroom.get("target_w"),
            "direct_marketing_export_w": export_headroom.get("export_w"),
            "direct_marketing_export_headroom_w": export_headroom.get("headroom_w"),
            "direct_marketing_export_discharge_limit_w": export_headroom.get("discharge_limit_w"),
            "direct_marketing_export_local_deficit_w": import_guard.get("local_deficit_w"),
            "direct_marketing_export_grid_import_w": import_guard.get("grid_import_w"),
            "direct_marketing_export_import_guard_w": import_guard.get("import_guard_w"),
            "direct_marketing_export_base_w": control.get("base_w"),
            "direct_marketing_export_desired_w": control.get("desired_w"),
            "direct_marketing_export_min_grid_export_w": control.get("min_grid_export_w"),
            "direct_marketing_export_netpoint_deadband_w": control.get("netpoint_deadband_w"),
            "direct_marketing_export_netpoint_release_margin_w": control.get("netpoint_release_margin_w"),
            "direct_marketing_export_grid_export_w": control.get("grid_export_w"),
            "direct_marketing_export_surplus_w": control.get("export_surplus_w"),
            "direct_marketing_export_grid_error_w": control.get("grid_error_w"),
            "direct_marketing_export_required_by_grid_w": control.get("required_by_grid_w"),
            "direct_marketing_export_required_by_load_w": control.get("required_by_load_w"),
            "direct_marketing_ramp_limited": ramp_limited,
            "direct_marketing_netpoint_release_hold": release_hold,
            "direct_marketing_hold_active": bool(hold_ctx.get("allowed")),
            "direct_marketing_profit_hold_ct_per_kwh": hold_ctx.get("profit_hold_ct"),
            "direct_marketing_margin_hold_pct": hold_ctx.get("margin_hold_pct"),
            "direct_marketing_hold_profit_floor_ct_per_kwh": hold_ctx.get("profit_floor_ct"),
            "direct_marketing_hold_margin_floor_pct": hold_ctx.get("margin_floor_pct"),
            "direct_marketing_ramp_base_w": ramp_base_w,
            "direct_marketing_ramp_step_w": ramp_step_w,
        }
        result.update(common)
        return result

    return None


def market_economics_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    market = plan.get("market_plan") if isinstance(plan.get("market_plan"), dict) else {}
    return market if isinstance(market, dict) else {}


def market_economics_current_contract(market: Dict[str, Any], now_s: float) -> Optional[Dict[str, Any]]:
    contract = market.get("active_contract") if isinstance(market.get("active_contract"), dict) else None
    if not contract:
        return None
    now_ms = int(float(now_s) * 1000.0)
    start_ms = safe_int(contract.get("start_ts"), 0)
    end_ms = safe_int(contract.get("end_ts"), 0)
    if start_ms <= now_ms < end_ms:
        return contract
    return None


def market_economics_contract_errors(market: Dict[str, Any], now_s: float) -> List[str]:
    errors: List[str] = []
    if not market:
        errors.append("plan_missing")
        return errors
    if not cfg_bool(market, "enabled", False):
        errors.append("plan_disabled")
    if not cfg_bool(market, "commands_allowed", False):
        errors.append("commands_not_allowed")
    if str(market.get("controller_owner") or "") != "storage_manager":
        errors.append("controller_owner_mismatch")
    if not str(market.get("plan_owner") or "").startswith(MARKET_ECONOMICS_OWNER_PREFIX):
        errors.append("plan_owner_mismatch")
    if safe_int(market.get("owner_contract_version"), 0) != MARKET_ECONOMICS_CONTRACT_VERSION:
        errors.append("contract_version_mismatch")
    valid_until = safe_int(market.get("valid_until_ts"), 0)
    now_ms = int(float(now_s) * 1000.0)
    if valid_until <= 0 or valid_until < now_ms:
        errors.append("plan_expired")
    return errors


def market_economics_plan_authoritative(plan: Dict[str, Any], now_s: float) -> bool:
    market = market_economics_plan(plan)
    if not market:
        return False
    return not market_economics_contract_errors(market, now_s)


def market_economics_storage_released(contract: Dict[str, Any]) -> bool:
    released = contract.get("released_consumers")
    if not isinstance(released, list):
        return False
    return "storage" in {str(item).strip().lower() for item in released}


def market_economics_storage_action_authorized(cfg: Dict[str, Any], action: str) -> bool:
    action = str(action or "").strip()
    if action == "grid_charge":
        return cfg_bool(cfg, "market_battery_grid_charge_enable", False)
    if action == "hold_discharge":
        return cfg_bool(cfg, "market_battery_hold_enable", False)
    if action == "negative_price_absorb":
        return (
            cfg_bool(cfg, "cheap_grid_boost_enable", False)
            and cfg_bool(cfg, "cheap_grid_battery_enable", True)
        )
    return False


def market_previous_action(previous_state: Optional[Dict[str, Any]]) -> str:
    previous_state = previous_state or {}
    action = str(previous_state.get("market_economics_action") or "").strip()
    if action:
        return action
    state = str(previous_state.get("state") or previous_state.get("parallel_state") or "").strip()
    return MARKET_ACTION_BY_STATE.get(state, "")


def market_previous_state_age_s(previous_state: Optional[Dict[str, Any]], now_s: float) -> float:
    previous_state = previous_state or {}
    since_ts = safe_float(previous_state.get("parallel_state_since_ts"), 0.0)
    if since_ts <= 0.0:
        since_ts = safe_float(previous_state.get("ts"), 0.0)
    if since_ts <= 0.0:
        return 0.0
    return max(0.0, float(now_s) - since_ts)


def market_contract_valid_at(contract: Optional[Dict[str, Any]], now_s: float) -> bool:
    if not isinstance(contract, dict):
        return False
    now_ms = int(float(now_s) * 1000.0)
    start_ms = safe_int(contract.get("start_ts"), 0)
    end_ms = safe_int(contract.get("end_ts"), 0)
    return start_ms <= now_ms < end_ms


def market_economics_dwell_contract(
    cfg: Dict[str, Any],
    contract: Dict[str, Any],
    action: str,
    now_s: float,
    previous_state: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    dwell_s = max(0.0, safe_float(cfg.get("market_owner_dwell_s"), MARKET_OWNER_DWELL_S))
    context = {
        "active": False,
        "min_s": round(dwell_s, 1),
        "age_s": round(market_previous_state_age_s(previous_state, now_s), 1),
        "remaining_s": 0.0,
        "previous_action": market_previous_action(previous_state),
    }
    if dwell_s <= 0.0 or action == "negative_price_absorb":
        return contract, action, context
    if action not in (MARKET_GRID_ACTIONS | MARKET_HOLD_ACTIONS):
        return contract, action, context
    previous_action = str(context["previous_action"] or "")
    if previous_action == action or previous_action == "negative_price_absorb":
        return contract, action, context
    if previous_action not in (MARKET_GRID_ACTIONS | MARKET_HOLD_ACTIONS):
        return contract, action, context
    age_s = float(context["age_s"])
    if age_s >= dwell_s:
        return contract, action, context
    previous_contract = (previous_state or {}).get("market_economics_contract")
    if not market_contract_valid_at(previous_contract, now_s):
        return contract, action, context
    held_contract = dict(previous_contract)
    held_contract.setdefault("reason", "market_owner_dwell")
    context.update({
        "active": True,
        "held_action": previous_action,
        "new_action": action,
        "remaining_s": round(max(0.0, dwell_s - age_s), 1),
    })
    return held_contract, previous_action, context


def market_economics_release_decision(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    market: Dict[str, Any],
    contract: Optional[Dict[str, Any]],
    max_charge_w: int,
    max_discharge_w: int,
    previous_state: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    previous_name = str((previous_state or {}).get("state") or "")
    if not previous_name.startswith("market_") or previous_name == "market_house_supply_release":
        return None
    contract = contract if isinstance(contract, dict) else {}
    action = str(contract.get("action") or "")
    reason = (
        "Marktpfad gibt E3DC-AUTO frei: Hausversorgung läuft autonom"
        if action == "house_supply"
        else "Marktpfad beendet: E3DC-AUTO freigegeben"
    )
    return {
        "state": "market_house_supply_release",
        "mode": MODE_AUTO,
        "val": max_charge_w,
        "priority": "market",
        "reason": reason,
        "protected": True,
        "storage_req_w": 0,
        "budget_w": 0,
        "market_economics_active": True,
        "market_economics_action": action,
        "market_economics_owner": str(market.get("plan_owner") or ""),
        "market_economics_contract_version": MARKET_ECONOMICS_CONTRACT_VERSION,
        "market_economics_commands_allowed": bool(market.get("commands_allowed")),
        "market_economics_shadow": bool(market.get("shadow")),
        "market_economics_reason": str(market.get("reason") or contract.get("reason") or action),
        "market_economics_blocked_reasons": market.get("blocked_reasons") if isinstance(market.get("blocked_reasons"), list) else [],
        "market_economics_contract": contract,
        "auto_limit": {
            "enabled": False,
            "release": True,
            "set_power_auto": True,
            "set_power_value": 0,
            "max_charge_w": max_charge_w,
            "max_discharge_w": max_discharge_w,
            "discharge_start_w": 0,
            "heartbeat_s": auto_limit_heartbeat_s(cfg),
            "reason": reason,
        },
    }


def market_economics_decision(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    now_s: float,
    max_charge_w: int,
    max_discharge_w: int,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    market = market_economics_plan(plan)
    contract = market_economics_current_contract(market, now_s)
    if not contract:
        return market_economics_release_decision(
            cfg,
            live,
            market,
            None,
            max_charge_w,
            max_discharge_w,
            previous_state,
        )

    contract_errors = market_economics_contract_errors(market, now_s)
    if contract_errors:
        return market_economics_release_decision(
            cfg,
            live,
            market,
            contract,
            max_charge_w,
            max_discharge_w,
            previous_state,
        )

    action = str(contract.get("action") or "")
    if action in MARKET_AUTONOMOUS_ACTIONS:
        return market_economics_release_decision(
            cfg,
            live,
            market,
            contract,
            max_charge_w,
            max_discharge_w,
            previous_state,
        )
    if action not in (MARKET_GRID_ACTIONS | MARKET_HOLD_ACTIONS):
        return None
    if not market_economics_storage_released(contract):
        return None
    if not market_economics_storage_action_authorized(cfg, action):
        return market_economics_release_decision(
            cfg,
            live,
            market,
            contract,
            max_charge_w,
            max_discharge_w,
            previous_state,
        )

    contract, action, dwell_context = market_economics_dwell_contract(
        cfg,
        contract,
        action,
        now_s,
        previous_state,
    )
    if not market_economics_storage_action_authorized(cfg, action):
        return market_economics_release_decision(
            cfg,
            live,
            market,
            contract,
            max_charge_w,
            max_discharge_w,
            previous_state,
        )

    economics = contract.get("economics") if isinstance(contract.get("economics"), dict) else {}
    forecast = contract.get("forecast") if isinstance(contract.get("forecast"), dict) else {}
    reserve = market.get("reserve") if isinstance(market.get("reserve"), dict) else {}
    soc = safe_float(live.get("SOC"), 0.0)
    target_soc = safe_float(
        reserve.get("target_soc_pct"),
        safe_float(plan.get("planning_target_soc", plan.get("target_soc")), 95.0),
    )
    if action == "grid_charge":
        target_soc = safe_float(forecast.get("grid_charge_target_soc_pct"), target_soc)
    hysteresis = max(0.2, safe_float(cfg.get("market_target_hysteresis_pct"), MARKET_TARGET_HYSTERESIS_PCT))
    common = {
        "priority": "market",
        "protected": True,
        "market_economics_active": True,
        "market_economics_action": action,
        "market_economics_owner": str(market.get("plan_owner") or ""),
        "market_economics_contract_version": MARKET_ECONOMICS_CONTRACT_VERSION,
        "market_economics_commands_allowed": bool(market.get("commands_allowed")),
        "market_economics_shadow": bool(market.get("shadow")),
        "market_economics_reason": str(market.get("reason") or contract.get("reason") or action),
        "market_economics_blocked_reasons": market.get("blocked_reasons") if isinstance(market.get("blocked_reasons"), list) else [],
        "market_economics_dwell": dwell_context,
        "market_economics_dwell_active": bool(dwell_context.get("active")),
        "market_economics_dwell_remaining_s": dwell_context.get("remaining_s"),
        "market_economics_contract": contract,
        "market_economics_economics": economics,
        "market_economics_forecast": forecast,
        "market_economics_target_soc_pct": round(target_soc, 1),
    }

    if action in MARKET_GRID_ACTIONS:
        if action == "negative_price_absorb" and not cfg_bool(cfg, "cheap_grid_boost_enable", False):
            return market_economics_release_decision(
                cfg,
                live,
                market,
                contract,
                max_charge_w,
                max_discharge_w,
                previous_state,
            )
        if soc >= target_soc - hysteresis:
            return market_economics_release_decision(
                cfg,
                live,
                market,
                contract,
                max_charge_w,
                max_discharge_w,
                previous_state,
            )
        live_pv_guard = market_grid_charge_live_pv_guard(live)
        forecast_grid_need_wh = max(
            0.0,
            safe_float(forecast.get("grid_charge_need_wh"), 0.0),
            safe_float(forecast.get("future_high_deficit_uncovered_by_pv_wh"), 0.0),
        )
        export_absorb_hold_s = max(0.0, safe_float(cfg.get("market_live_export_absorb_hold_s"), 45.0))
        previous_export_absorb_hold = bool(
            action == "grid_charge"
            and export_absorb_hold_s > 0.0
            and str((previous_state or {}).get("state") or "") == "market_grid_charge"
            and bool((previous_state or {}).get("market_live_export_absorb_active"))
            and market_previous_state_age_s(previous_state, now_s) <= export_absorb_hold_s
            and forecast_grid_need_wh > 100.0
            and soc < target_soc - hysteresis
        )
        live_export_charge_override = bool(
            action == "grid_charge"
            and (
                (
                    bool(live_pv_guard.get("active"))
                    and str(live_pv_guard.get("reason") or "") == "grid_export"
                )
                or previous_export_absorb_hold
            )
            and forecast_grid_need_wh > 100.0
            and soc < target_soc - hysteresis
        )
        if action == "grid_charge" and bool(live_pv_guard.get("active")) and not live_export_charge_override:
            reason = (
                "Marktpfad wartet: Live-PV/Netzexport vorhanden; "
                "normales Speicher-Netzladen wird nicht vorgezogen, Speicherentladung bleibt gesperrt"
            )
            result = {
                "state": "market_grid_pv_wait",
                "mode": MODE_AUTO,
                "val": max_charge_w,
                "reason": reason,
                "storage_req_w": 0,
                "budget_w": 0,
                "auto_limit": discharge_block_auto_limit(cfg, max_charge_w, reason),
                "market_live_pv_first": live_pv_guard,
            }
            result.update(common)
            return result
        charge_w = max_charge_w
        room_w = grid_charge_room_w(cfg, live)
        if room_w is not None:
            charge_w = min(charge_w, room_w)
        max_market_w = safe_int(cfg.get("market_battery_max_w"), 0)
        if max_market_w <= 0:
            max_market_w = safe_int(cfg.get("cheap_grid_battery_max_w"), 0)
        if max_market_w > 0:
            charge_w = min(charge_w, max_market_w)
        late_fill = forecast.get("late_fill") if isinstance(forecast.get("late_fill"), dict) else {}
        late_fill_start_ms = safe_int(late_fill.get("latest_start_ts"), 0)
        late_fill_planned_w = max(0, safe_int(late_fill.get("charge_power_w"), 0))
        late_fill_power_ok = bool(
            late_fill_planned_w <= 0
            or charge_w >= max(300, int(late_fill_planned_w * 0.8))
        )
        late_fill_wait_active = bool(
            bool(late_fill.get("wait_active"))
            and late_fill_start_ms > int(now_s * 1000.0)
            and late_fill_power_ok
        )
        early_export_absorb = bool(live_export_charge_override and late_fill_wait_active)
        export_absorb = {}
        if early_export_absorb:
            export_absorb = market_live_export_absorb_charge_w(cfg, live, charge_w)
            absorb_charge_w = max(0, safe_int(export_absorb.get("charge_w"), 0))
            if absorb_charge_w > 0:
                charge_w = min(charge_w, absorb_charge_w)
        if (
            late_fill_wait_active
            and not live_export_charge_override
        ):
            reason = (
                "Marktpfad: günstiges Preisfenster, spätes Netzladen geplant; "
                "Speicherentladung wird bis zum spätesten sicheren Ladestart gesperrt"
            )
            result = {
                "state": "market_grid_wait",
                "mode": MODE_AUTO,
                "val": max_charge_w,
                "reason": reason,
                "storage_req_w": 0,
                "budget_w": 0,
                "auto_limit": discharge_block_auto_limit(cfg, max_charge_w, reason),
                "market_late_fill_wait_active": True,
                "market_late_fill_latest_start_ts": late_fill_start_ms,
                "market_late_fill_window_end_ts": safe_int(late_fill.get("window_end_ts"), 0),
                "market_late_fill_required_storage_wh": safe_int(late_fill.get("required_storage_wh"), 0),
            }
            result.update(common)
            return result
        state = MARKET_ECONOMICS_STATES[action]
        if charge_w < 300:
            reason = (
                "Negativpreis-Aufnahme wartet"
                if action == "negative_price_absorb"
                else "Marktpfad wartet: günstiges Preisfenster, Speicherentladung gesperrt"
            )
            result = {
                "state": "market_negative_absorb_wait" if action == "negative_price_absorb" else "market_grid_wait",
                "mode": MODE_AUTO,
                "val": max_charge_w,
                "reason": reason + "; Hausanschluss-Limit lässt kein Speicher-Netzladen zu",
                "storage_req_w": 0,
                "budget_w": 0,
                "auto_limit": discharge_block_auto_limit(cfg, max_charge_w, reason),
            }
            result.update(common)
            return result
        reason = (
            "Negativpreis-Boost: Speicher nimmt Netzstrom auf"
            if action == "negative_price_absorb"
            else (
                "Marktpfad: günstiges Preisfenster vor prognostiziertem Defizit, "
                "Speicher wird aus dem Netz geladen"
            )
        )
        if live_export_charge_override:
            if early_export_absorb:
                reason = (
                    "Marktpfad: gültiges günstiges Preisfenster mit prognostiziertem Defizit; "
                    "vor dem spätesten Netzladestart wird Live-Netzexport begrenzt in den Speicher aufgenommen"
                )
            else:
                reason = (
                    "Marktpfad: Netzladen ist im gültigen Preisfenster fällig; "
                    "Speicher wird mit freigegebener Leistung geladen"
                )
        result = {
            "state": state,
            "mode": MODE_AUTO if early_export_absorb else MODE_GRID,
            "val": charge_w,
            "reason": reason,
            "storage_req_w": charge_w,
            "budget_w": 0,
        }
        if early_export_absorb:
            result["auto_limit"] = charge_cap_auto_limit(cfg, charge_w, 0, reason)
        if live_export_charge_override:
            result["market_live_pv_first"] = live_pv_guard
            result["market_live_pv_first_overridden"] = True
            result["market_live_export_absorb_active"] = bool(early_export_absorb)
            result["market_live_export_absorb_hold_active"] = bool(previous_export_absorb_hold)
            result["market_live_export_absorb_charge_w"] = charge_w
            result["market_live_export_absorb"] = export_absorb
            result["market_late_fill_wait_overridden"] = bool(late_fill_wait_active)
            result["market_forecast_grid_charge_need_wh"] = round(forecast_grid_need_wh, 1)
        result.update(common)
        return result

    if action in MARKET_HOLD_ACTIONS:
        if soc <= ep_reserve_soc(cfg, live) + 0.2:
            return None
        reason = (
            "Marktpfad: künftiger Preisberg mit Defizit prognostiziert, "
            "Speicherentladung wird bis zum Hochpreisfenster gehalten"
        )
        auto_limit = discharge_block_auto_limit(cfg, 0, reason)
        auto_limit["max_charge_w"] = 0
        result = {
            "state": MARKET_ECONOMICS_STATES[action],
            "mode": MODE_AUTO,
            "val": 0,
            "reason": reason,
            "storage_req_w": 0,
            "budget_w": 0,
            "auto_limit": auto_limit,
        }
        result.update(common)
        return result

    return None


def _direct_marketing_public_window(window: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key in (
        "start_ts",
        "end_ts",
        "start_t",
        "end_t",
        "action",
        "reason",
        "avg_market_ct",
        "min_market_ct",
        "max_market_ct",
        "avg_billing_ct",
        "avg_score",
        "slot_count",
        "max_power_w",
        "target_soc_pct",
        "soc_ceiling_pct",
        "curtailment_allowed",
        "curtail_export_limit_w",
        "export_constraint_class",
        "hard_export_limit_active",
        "hard_export_limit_w",
        "export_constraint_scope",
        "pv_export_allowed",
        "economic_basis",
        "reserve_floor_soc_pct",
        "storage_action",
        "headroom_limited",
        "net_sell_ct",
        "gross_sell_ct",
        "fee_cost_ct",
        "fee_basis",
        "fee_basis_ct",
        "fee_pct",
        "pv_store_price_class",
        "pv_store_soft_threshold",
        "pv_store_threshold_ct",
        "pv_store_threshold_source",
        "pv_store_min_surplus_w",
        "pv_store_import_guard_w",
        "pv_store_min_hold_s",
        "pv_store_ramp_step_w",
        "pv_store_dc_only_enable",
        "pv_store_external_ac_guard_w",
        "pv_store_export_limit_guard_w",
        "pv_store_export_limit_ramp_bypass_w",
        "negative_headroom_limited",
        "negative_headroom_next_start_ts",
        "negative_headroom_next_end_ts",
        "negative_headroom_window_min",
        "negative_headroom_forecast_surplus_wh",
        "negative_headroom_required_pct",
        "negative_headroom_lookahead_min",
        "pv_store_headroom_limited",
        "pv_store_headroom_next_reason",
        "expected_profit_ct_per_kwh",
        "export_segment_id",
        "export_segment_start_ts",
        "export_segment_end_ts",
        "export_segment_budget_source",
        "export_segment_budget_wh",
        "export_segment_available_wh",
        "export_segment_load_reserve_wh",
        "export_segment_load_forecast_used",
        "export_segment_selected_wh",
        "export_segment_next_recharge_ts",
        "export_segment_next_recharge_action",
    ):
        if key in window:
            result[key] = window.get(key)
    start_ms = safe_int(window.get("start_ts"), 0)
    end_ms = safe_int(window.get("end_ts"), 0)
    result["current"] = bool(start_ms <= now_ms < end_ms)
    return result


def _direct_marketing_public_windows(
    direct: Dict[str, Any],
    now_s: float,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    windows = direct.get("windows") if isinstance(direct.get("windows"), list) else []
    now_ms = int(float(now_s) * 1000.0)
    future: List[Dict[str, Any]] = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        end_ms = safe_int(window.get("end_ts"), 0)
        if end_ms <= now_ms:
            continue
        future.append(_direct_marketing_public_window(window, now_ms))
    future.sort(key=lambda item: safe_int(item.get("start_ts"), 0))
    return future[: max(1, int(limit or 1))]


def _direct_marketing_expected_profit_ct(
    mode: str,
    action: str,
    economics: Dict[str, Any],
) -> Optional[float]:
    if mode == "safe" or action in (
        "keep_headroom",
        "safe_house_supply",
        "eco_house_supply",
        "eco_plus_house_supply",
    ):
        return None
    if action in DIRECT_MARKETING_PV_STORE_ACTIONS or action == "eco_plus_export_candidate" or mode == "eco_plus":
        return safe_float(economics.get("pv_shift_spread_ct_per_kwh"), 0.0)
    if action in ("arbitrage_grid_charge_candidate", "arbitrage_export_candidate") or mode == "arbitrage":
        return safe_float(economics.get("grid_spread_ct_per_kwh"), 0.0)
    if economics.get("best_spread_ct_per_kwh") is not None:
        return safe_float(economics.get("best_spread_ct_per_kwh"), 0.0)
    return None


_DIRECT_MARKETING_ALLOCATION_DIAGNOSTICS = {
    "export_energy_prioritized",
    "pv_store_energy_budget_prioritized",
}


def _direct_marketing_reason_projection(reasons: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Trenne Ausführungsgates, Kandidatenablehnungen und Allokationshinweise."""
    projected: Dict[str, List[Dict[str, Any]]] = {
        "global_gate_blockers": [],
        "candidate_rejections": [],
        "allocation_diagnostics": [],
    }
    seen = set()
    for raw_value in reasons if isinstance(reasons, list) else []:
        raw = str(raw_value or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        normalized = raw.lower()
        if normalized in _DIRECT_MARKETING_ALLOCATION_DIAGNOSTICS:
            projected["allocation_diagnostics"].append({"code": normalized})
            continue
        if normalized.startswith("blocked by margin:"):
            numbers = []
            for token in normalized.replace("<", " ").split():
                try:
                    numbers.append(float(token))
                except (TypeError, ValueError):
                    continue
            projected["candidate_rejections"].append({
                "code": "ECONOMIC_EXPORT_WINDOW_PROFIT_BELOW_USER_MINIMUM",
                "expected_profit_eur": numbers[0] if numbers else None,
                "minimum_profit_eur": numbers[1] if len(numbers) > 1 else None,
            })
            continue
        if normalized in {"profit_below_threshold", "economic_export_window_profit_below_user_minimum"}:
            projected["candidate_rejections"].append({
                "code": "ECONOMIC_EXPORT_WINDOW_PROFIT_BELOW_USER_MINIMUM",
            })
            continue
        projected["global_gate_blockers"].append({"code": raw})
    return projected


def direct_marketing_monitor(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    now_s: float,
    max_charge_w: int,
    max_discharge_w: int,
    decision: Optional[Dict[str, Any]] = None,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Expose the direct-marketing owner contract without changing decisions."""
    direct = direct_marketing_plan(plan)
    policy_ctx = direct_marketing_policy_context(direct)
    policy_executor_gate = (
        direct_marketing_policy_executor_gate(direct, policy_ctx, now_s, plan)
        if policy_ctx.get("present") and policy_ctx.get("commands_allowed")
        else {"allowed": False, "reason": "policy_not_active"}
    )
    future_store_reservation = direct_marketing_future_pv_store_reservation(plan, now_s)
    flags = direct.get("flags") if isinstance(direct.get("flags"), dict) else {}
    economics = direct.get("economics") if isinstance(direct.get("economics"), dict) else {}
    reserve = direct.get("reserve") if isinstance(direct.get("reserve"), dict) else {}
    now_ms = int(float(now_s) * 1000.0)
    cfg_enabled = cfg_bool(cfg, "direct_marketing_enable", False)
    mode = str(direct.get("mode") or cfg.get("direct_marketing_mode") or ("safe" if cfg_enabled else "off"))
    mode = mode.strip().lower().replace("-", "_").replace(" ", "_")
    if mode in ("eco+", "ecoplus"):
        mode = "eco_plus"
    contract_errors = direct_marketing_contract_errors(cfg, direct, now_s)
    contract_warnings = direct_marketing_contract_warnings(cfg, direct)
    current_window = direct_marketing_current_window(direct, now_s)
    current_action = str((current_window or {}).get("action") or "")
    current_state = DIRECT_MARKETING_ACTION_STATES.get(current_action, "")
    owner_switch_ctx = direct_marketing_owner_switch_cooldown(
        cfg,
        previous_state,
        current_state,
        current_window or {},
        live,
        now_s,
    )
    active_decision = bool(decision and decision.get("direct_marketing_active"))
    pv_store_control_diag: Dict[str, Any] = {}
    external_derating = direct_marketing_external_derating_context(live, cfg, now_s)

    blockers = set()
    if isinstance(direct.get("blocked_reasons"), list):
        blockers.update(str(reason) for reason in direct.get("blocked_reasons") if str(reason or "").strip())
    blockers.update(contract_errors)
    if policy_ctx.get("present"):
        policy_block_reason = str(policy_ctx.get("block_reason") or "")
        if not policy_ctx.get("schema_valid"):
            blockers.add(policy_block_reason or "policy_schema_invalid")
        elif policy_ctx.get("blocked"):
            blockers.add(policy_block_reason or "policy_blocked")
        elif policy_ctx.get("commands_allowed") and str(policy_ctx.get("target_state") or "") in DIRECT_MARKETING_POLICY_EXPORT_STATES:
            policy_ep_reserve_pct = ep_reserve_soc(cfg, live)
            policy_soc = safe_float(live.get("SOC"), 0.0)
            if policy_ep_reserve_pct > 0.0 and policy_soc <= policy_ep_reserve_pct:
                blockers.add("ep_reserve_floor_reached")
        if policy_ctx.get("commands_allowed") and not policy_executor_gate.get("allowed"):
            blockers.add(str(policy_executor_gate.get("reason") or "policy_executor_gate_blocked"))
    hold_active_decision = bool(decision and decision.get("direct_marketing_hold_active"))
    if hold_active_decision:
        blockers.discard("commands_not_allowed")
        blockers.discard("pv_shift_below_threshold_for_export")
    if owner_switch_ctx.get("active") and not active_decision:
        blockers.add("owner_switch_cooldown")

    if direct:
        windows = direct.get("windows") if isinstance(direct.get("windows"), list) else []
        if not current_window:
            has_future = any(
                isinstance(window, dict) and safe_int(window.get("end_ts"), 0) > now_ms
                for window in windows
            )
            blockers.add("no_current_window" if has_future else "no_candidate_windows")
        elif current_action not in DIRECT_MARKETING_CONTROLLABLE_ACTIONS:
            blockers.add("window_observe_only")

    if current_window and current_action in DIRECT_MARKETING_CONTROLLABLE_ACTIONS and not active_decision:
        if current_action in DIRECT_MARKETING_HEADROOM_ACTIONS:
            if mode not in ("eco", "eco_plus"):
                blockers.add("mode_mismatch")
            if not bool(flags.get("pv_store_enable")):
                blockers.add("pv_store_not_enabled")
            if not bool(flags.get("negative_headroom_enable")):
                blockers.add("negative_headroom_disabled")
            if not bool(flags.get("negative_price_no_export")):
                blockers.add("negative_price_export_allowed")
        elif current_action in DIRECT_MARKETING_PV_STORE_ACTIONS:
            if mode not in ("eco", "eco_plus"):
                blockers.add("mode_mismatch")
            if not bool(flags.get("pv_store_enable")):
                blockers.add("pv_store_not_enabled")
            pv_store_profit_ok = bool(
                economics.get("pv_shift_profit_ok")
                or str(current_window.get("reason") or "") == "negative_price"
                or bool(current_window.get("headroom_limited"))
                or direct_marketing_pv_store_threshold_ok(current_window)
            )
            if not pv_store_profit_ok:
                blockers.add("pv_shift_below_threshold_for_pv_store")
        elif current_action == "eco_plus_export_candidate":
            if mode != "eco_plus":
                blockers.add("mode_mismatch")
            if not bool(flags.get("export_enable")) or safe_int(flags.get("max_export_w"), 0) <= 0:
                blockers.add("export_not_enabled")
            if not bool(economics.get("pv_shift_profit_ok")):
                blockers.add("pv_shift_below_threshold_for_export")
        elif current_action in ("arbitrage_grid_charge_candidate", "arbitrage_export_candidate"):
            blockers.add("arbitrage_not_released")

        soc = safe_float(live.get("SOC"), 0.0)
        reserve_floor = max(
            safe_float(reserve.get("effective_min_soc_pct"), 0.0),
            ep_reserve_soc(cfg, live),
        )
        if current_action in DIRECT_MARKETING_HEADROOM_ACTIONS:
            headroom_ceiling = safe_float(current_window.get("soc_ceiling_pct"), 100.0)
            hysteresis = max(0.2, safe_float(cfg.get("direct_marketing_target_hysteresis_pct"), 0.7))
            if soc <= reserve_floor + 0.2:
                blockers.add("reserve_floor_reached")
            elif soc <= headroom_ceiling - hysteresis:
                blockers.add("negative_headroom_already_available")
        elif current_action in DIRECT_MARKETING_PV_STORE_ACTIONS:
            target_soc = safe_float(
                current_window.get("target_soc_pct"),
                safe_float(
                    current_window.get("soc_ceiling_pct"),
                    safe_float(reserve.get("target_soc_pct"), safe_float(cfg.get("storage_target_soc"), 100.0)),
                ),
            )
            hysteresis = max(0.2, safe_float(cfg.get("direct_marketing_target_hysteresis_pct"), 0.7))
            if soc >= target_soc - hysteresis:
                blockers.add("target_soc_reached")
            control = direct_marketing_pv_store_control_w(cfg, live, current_window, flags, max_charge_w, now_s)
            pv_store_control_diag = control if isinstance(control, dict) else {}
            if control.get("blocked"):
                blockers.add(str(control.get("blocker") or "pv_store_blocked"))
            elif safe_int(control.get("charge_w"), 0) < 300:
                blockers.add("pv_store_charge_power_below_min")
        elif current_action in DIRECT_MARKETING_GRID_ACTIONS:
            target_soc = safe_float(current_window.get("target_soc_pct"), safe_float(reserve.get("target_soc_pct"), 100.0))
            hysteresis = max(0.2, safe_float(cfg.get("direct_marketing_target_hysteresis_pct"), 0.7))
            if soc >= target_soc - hysteresis:
                blockers.add("target_soc_reached")
            charge_w = min(
                max_charge_w,
                safe_int(flags.get("max_grid_charge_w"), max_charge_w),
                safe_int(current_window.get("max_power_w"), max_charge_w),
            )
            room_w = grid_charge_room_w(cfg, live)
            if room_w is not None:
                charge_w = min(charge_w, room_w)
                if room_w < 300:
                    blockers.add("house_connection_limited")
            if charge_w < 300:
                blockers.add("grid_charge_power_below_min")
        elif current_action in DIRECT_MARKETING_EXPORT_ACTIONS:
            if soc <= reserve_floor + 0.2:
                blockers.add("reserve_floor_reached")
            if abs(safe_float(live.get("Wallbox_Power"), 0.0)) > 250.0:
                blockers.add("wallbox_active")
            base_export_w = min(
                max_discharge_w,
                safe_int(flags.get("max_export_w"), max_discharge_w),
                safe_int(current_window.get("max_power_w"), max_discharge_w),
            )
            max_export_w = max_discharge_w
            export_headroom = predump_grid_export_headroom(cfg, live)
            if export_headroom.get("limited"):
                max_export_w = min(max_export_w, safe_int(export_headroom.get("discharge_limit_w"), max_export_w))
                if max_export_w < 300:
                    blockers.add("export_headroom_limited")
            control = direct_marketing_export_control_w(
                cfg,
                live,
                previous_state or {},
                base_export_w,
                max_export_w,
            )
            export_w = safe_int(control.get("target_w"), 0)
            import_guard = direct_marketing_export_import_guard(cfg, live, export_w)
            if import_guard.get("blocked"):
                blockers.add("export_below_local_load_with_grid_import")
            if export_w < 300:
                blockers.add("export_power_below_min")

    if active_decision:
        if decision and decision.get("direct_marketing_policy_active"):
            blockers.discard("no_current_window")
            blockers.discard("no_candidate_windows")
            blockers.discard("window_observe_only")
            blockers.discard("commands_not_allowed")
        monitor_state = "active"
        current_action = str(decision.get("direct_marketing_action") or current_action)
        active_window = decision.get("direct_marketing_window")
        if isinstance(active_window, dict):
            current_window = active_window
    elif bool(decision and decision.get("direct_marketing_future_pv_store_reservation_active")):
        monitor_state = "reservation"
    elif not cfg_enabled or mode == "off":
        monitor_state = "off"
    elif bool(direct.get("shadow")) or not bool(flags.get("commands_allowed")):
        monitor_state = "shadow"
    elif current_window:
        monitor_state = "waiting"
    else:
        monitor_state = "blocked"

    profit_ct = None
    if decision and decision.get("direct_marketing_profit_ct_per_kwh") is not None:
        profit_ct = safe_float(decision.get("direct_marketing_profit_ct_per_kwh"), 0.0)
    else:
        profit_ct = _direct_marketing_expected_profit_ct(mode, current_action, economics)

    effective_commands_allowed = (
        bool(flags.get("commands_allowed"))
        or hold_active_decision
        or bool(policy_ctx.get("commands_allowed") and policy_executor_gate.get("allowed"))
    )

    def pv_store_diag_value(decision_key: str, control_key: str, default: Any = None) -> Any:
        if isinstance(decision, dict) and decision.get(decision_key) is not None:
            return decision.get(decision_key)
        if pv_store_control_diag.get(control_key) is not None:
            return pv_store_control_diag.get(control_key)
        return default

    hard_export_limit_active_diag = bool(pv_store_diag_value(
        "direct_marketing_hard_export_limit_active",
        "hard_export_limit_active",
        (current_window or {}).get("hard_export_limit_active", False) if isinstance(current_window, dict) else False,
    ))
    decision_export_execution = (
        decision.get("direct_marketing_export_execution")
        if isinstance(decision, dict) and isinstance(decision.get("direct_marketing_export_execution"), dict)
        else None
    )
    if decision_export_execution is None:
        monitor_constraint = (
            policy_ctx.get("export_constraint")
            if isinstance(policy_ctx.get("export_constraint"), dict) and policy_ctx.get("export_constraint")
            else direct_marketing_window_export_constraint(current_window or {})
        )
        if hard_export_limit_active_diag and not bool(monitor_constraint.get("hard")):
            monitor_constraint = {
                **monitor_constraint,
                "hard": True,
                "limit_w": pv_store_diag_value(
                    "direct_marketing_hard_export_limit_w",
                    "hard_export_limit_w",
                    0,
                ),
                "scope": pv_store_diag_value(
                    "direct_marketing_export_constraint_scope",
                    "export_constraint_scope",
                    "grid_connection",
                ),
            }
        observed_absorption_w = max(
            0,
            safe_int(
                (decision or {}).get("direct_marketing_pv_store_w"),
                safe_int(live.get("Battery_Power"), 0),
            ),
        )
        absorption_cap_w = (
            0
            if safe_float(live.get("SOC"), 0.0) >= 99.5
            else min(
                max(0, safe_int(max_charge_w, 0)),
                max(
                    observed_absorption_w,
                    safe_int(pv_store_control_diag.get("offer_w"), 0),
                ),
            )
        )
        decision_export_execution = direct_marketing_export_execution_contract(
            cfg,
            live,
            monitor_constraint,
            external_derating=external_derating,
            storage_absorption_w=observed_absorption_w,
            storage_absorption_cap_w=absorption_cap_w,
            unavoidable_export_w=safe_int(
                pv_store_diag_value(
                    "direct_marketing_pv_store_unavoidable_export_w",
                    "unavoidable_export_w",
                    0,
                ),
                0,
            ),
            now_s=now_s,
        )
    hard_export_owner_confirmed = bool(decision_export_execution.get("external_owner_confirmed"))
    if hard_export_limit_active_diag and not hard_export_owner_confirmed:
        blockers.add("hard_export_owner_unconfirmed")
    if hard_export_limit_active_diag and not bool(decision_export_execution.get("grid_point_confirmed")):
        blockers.add("hard_export_grid_point_unconfirmed")
    if bool(decision_export_execution.get("violation")):
        blockers.add(
            "hard_export_limit_unavoidable"
            if decision_export_execution.get("unavoidable")
            else "hard_export_limit_violation"
        )

    reason_projection = _direct_marketing_reason_projection(sorted(blockers))
    return {
        "enabled": bool(cfg_enabled),
        "active": active_decision,
        "state": monitor_state,
        "mode": mode,
        "shadow": bool(direct.get("shadow")) if direct else not bool(cfg_enabled),
        "commands_allowed": effective_commands_allowed,
        "policy_schema": policy_ctx.get("schema") if policy_ctx.get("present") else None,
        "policy_target_state": policy_ctx.get("target_state") if policy_ctx.get("present") else None,
        "policy_commands_allowed": bool(
            policy_ctx.get("commands_allowed") and policy_executor_gate.get("allowed")
        ),
        "policy_export_budget_w": policy_ctx.get("export_budget_w") if policy_ctx.get("present") else 0,
        "policy_charge_budget_w": policy_ctx.get("charge_budget_w") if policy_ctx.get("present") else 0,
        "policy_executable_action": (
            policy_ctx.get("policy", {}).get("executable_action")
            if isinstance(policy_ctx.get("policy"), dict)
            else None
        ),
        "policy_blocked": policy_ctx.get("blocked") if policy_ctx.get("present") else None,
        "policy_block_reason": policy_ctx.get("block_reason") if policy_ctx.get("present") else "",
        "policy_export_constraint": policy_ctx.get("export_constraint") if policy_ctx.get("present") else {},
        "policy_executor_gate": policy_executor_gate if policy_ctx.get("present") else None,
        "future_pv_store_reservation": future_store_reservation,
        "future_pv_store_reservation_active": bool(decision and decision.get("direct_marketing_future_pv_store_reservation_active")),
        "hold_active": hold_active_decision,
        "headroom_hold_active": bool(decision and decision.get("direct_marketing_headroom_hold_active")),
        "headroom_soc_ceiling_pct": safe_float((decision or {}).get("direct_marketing_headroom_soc_ceiling_pct"), 0.0),
        "headroom_deficit_wh": safe_int((decision or {}).get("direct_marketing_headroom_deficit_wh"), 0),
        "headroom_next_start_ts": safe_int((decision or {}).get("direct_marketing_headroom_next_start_ts"), 0),
        "headroom_window_min": safe_float((decision or {}).get("direct_marketing_headroom_window_min"), 0.0),
        "headroom_forecast_surplus_wh": safe_int((decision or {}).get("direct_marketing_headroom_forecast_surplus_wh"), 0),
        "headroom_required_pct": safe_float((decision or {}).get("direct_marketing_headroom_required_pct"), 0.0),
        "pv_store_hold_active": bool(decision and decision.get("direct_marketing_pv_store_hold_active")),
        "pv_store_ramp_limited": bool(decision and decision.get("direct_marketing_pv_store_ramp_limited")),
        "pv_store_curve_catchup_active": bool(decision and decision.get("direct_marketing_pv_store_curve_catchup_active")),
        "pv_store_curve_catchup_w": safe_int((decision or {}).get("direct_marketing_pv_store_curve_catchup_w"), 0),
        "pv_store_curve_catchup_source": (decision or {}).get("direct_marketing_pv_store_curve_catchup_source"),
        "pv_store_curve_catchup_gap_pct": safe_float((decision or {}).get("direct_marketing_pv_store_curve_catchup_gap_pct"), 0.0),
        "pv_store_curve_soc_pct": (decision or {}).get("direct_marketing_pv_store_curve_soc_pct"),
        "pv_store_curve_target_soc_pct": (decision or {}).get("direct_marketing_pv_store_curve_target_soc_pct"),
        "pv_store_release_hold_active": bool(decision and decision.get("direct_marketing_pv_store_release_hold_active")),
        "pv_store_release_hold_reason": (decision or {}).get("direct_marketing_pv_store_release_hold_reason"),
        "pv_store_release_hold_remaining_s": safe_float((decision or {}).get("direct_marketing_pv_store_release_hold_remaining_s"), 0.0),
        "pv_store_release_hold_previous_w": safe_int((decision or {}).get("direct_marketing_pv_store_release_hold_previous_w"), 0),
        "pv_store_release_hold_offer_w": safe_int((decision or {}).get("direct_marketing_pv_store_release_hold_offer_w"), 0),
        "pv_store_resync_active": bool(decision and decision.get("direct_marketing_pv_store_resync_active")),
        "pv_store_resync_reason": (decision or {}).get("direct_marketing_pv_store_resync_reason"),
        "pv_store_resync_gap_w": safe_int((decision or {}).get("direct_marketing_pv_store_resync_gap_w"), 0),
        "pv_store_resync_threshold_w": safe_int((decision or {}).get("direct_marketing_pv_store_resync_threshold_w"), 0),
        "pv_store_observed_charge_w": safe_int((decision or {}).get("direct_marketing_pv_store_observed_charge_w"), 0),
        "owner_switch_cooldown_active": bool(
            (decision or {}).get("direct_marketing_owner_switch_cooldown_active")
            or owner_switch_ctx.get("active")
        ),
        "owner_switch_cooldown_s": safe_float(
            (decision or {}).get("direct_marketing_owner_switch_cooldown_s"),
            safe_float(owner_switch_ctx.get("hold_s"), 0.0),
        ),
        "owner_switch_cooldown_age_s": safe_float(
            (decision or {}).get("direct_marketing_owner_switch_cooldown_age_s"),
            safe_float(owner_switch_ctx.get("age_s"), 0.0),
        ),
        "owner_switch_cooldown_remaining_s": safe_float(
            (decision or {}).get("direct_marketing_owner_switch_cooldown_remaining_s"),
            safe_float(owner_switch_ctx.get("remaining_s"), 0.0),
        ),
        "owner_switch_previous_state": (
            (decision or {}).get("direct_marketing_owner_switch_previous_state")
            or owner_switch_ctx.get("previous_state")
        ),
        "owner_switch_next_state": (
            (decision or {}).get("direct_marketing_owner_switch_next_state")
            or owner_switch_ctx.get("next_state")
        ),
        "pv_store_dc_only": bool(
            pv_store_diag_value("direct_marketing_pv_store_dc_only", "dc_only", False)
            or flags.get("pv_store_dc_only_enable")
        ),
        "pv_total_w": safe_int(pv_store_diag_value("direct_marketing_pv_total_w", "pv_total_w", 0), 0),
        "pv_e3dc_w": safe_int(pv_store_diag_value("direct_marketing_pv_e3dc_w", "pv_e3dc_w", 0), 0),
        "pv_external_ac_w": safe_int(pv_store_diag_value("direct_marketing_pv_external_ac_w", "pv_external_ac_w", 0), 0),
        "pv_source": pv_store_diag_value("direct_marketing_pv_source", "pv_source"),
        "pv_store_dc_surplus_w": safe_int(pv_store_diag_value("direct_marketing_pv_store_dc_surplus_w", "dc_surplus_w", 0), 0),
        "pv_store_estimated_offer_w": safe_int(pv_store_diag_value("direct_marketing_pv_store_estimated_offer_w", "estimated_offer_w", 0), 0),
        "pv_store_pv_safe_cap_w": safe_int(pv_store_diag_value("direct_marketing_pv_store_pv_safe_cap_w", "pv_safe_cap_w", 0), 0),
        "pv_store_self_reference_limited": bool(
            pv_store_diag_value("direct_marketing_pv_store_self_reference_limited", "self_reference_limited", False)
        ),
        "pv_store_target_fallback_active": bool((decision or {}).get("direct_marketing_pv_store_target_fallback_active")),
        "pv_store_execution": str((decision or {}).get("direct_marketing_pv_store_execution") or ""),
        "pv_store_auto_limit_active": bool((decision or {}).get("direct_marketing_pv_store_auto_limit_active")),
        "pv_store_external_export_owner": bool((decision or {}).get("direct_marketing_pv_store_external_export_owner")),
        "hard_export_owner_confirmed": hard_export_owner_confirmed,
        "export_execution": decision_export_execution,
        "export_execution_state": decision_export_execution.get("state"),
        "export_execution_claim": decision_export_execution.get("claim"),
        "export_compliance_confirmed": bool(decision_export_execution.get("compliance_confirmed")),
        "export_violation_w": safe_int(decision_export_execution.get("violation_w"), 0),
        "export_constraint_class": pv_store_diag_value(
            "direct_marketing_export_constraint_class",
            "export_constraint_class",
            (current_window or {}).get("export_constraint_class") if isinstance(current_window, dict) else None,
        ),
        "hard_export_limit_active": hard_export_limit_active_diag,
        "hard_export_limit_w": pv_store_diag_value(
            "direct_marketing_hard_export_limit_w",
            "hard_export_limit_w",
            (current_window or {}).get("hard_export_limit_w") if isinstance(current_window, dict) else None,
        ),
        "export_constraint_scope": pv_store_diag_value(
            "direct_marketing_export_constraint_scope",
            "export_constraint_scope",
            (current_window or {}).get("export_constraint_scope") if isinstance(current_window, dict) else None,
        ),
        "pv_export_allowed": bool(pv_store_diag_value(
            "direct_marketing_pv_export_allowed",
            "pv_export_allowed",
            (current_window or {}).get("pv_export_allowed", True) if isinstance(current_window, dict) else True,
        )),
        "pv_store_export_limit_active": bool(pv_store_diag_value("direct_marketing_pv_store_export_limit_active", "export_limit_active", False)),
        "pv_store_export_limit_guard_active": bool(pv_store_diag_value("direct_marketing_pv_store_export_limit_guard_active", "export_limit_guard_active", False)),
        "pv_store_export_limit_w": safe_int(pv_store_diag_value("direct_marketing_pv_store_export_limit_w", "export_limit_w", 0), 0),
        "pv_store_export_limit_guard_w": safe_int(pv_store_diag_value("direct_marketing_pv_store_export_limit_guard_w", "export_limit_guard_w", 0), 0),
        "pv_store_export_over_limit_w": safe_int(pv_store_diag_value("direct_marketing_pv_store_export_over_limit_w", "export_over_limit_w", 0), 0),
        "pv_store_export_absorb_target_w": safe_int(pv_store_diag_value("direct_marketing_pv_store_export_absorb_target_w", "export_absorb_target_w", 0), 0),
        "pv_store_unavoidable_export_w": safe_int(pv_store_diag_value("direct_marketing_pv_store_unavoidable_export_w", "unavoidable_export_w", 0), 0),
        "pv_store_export_limit_ramp_bypass": bool(decision and decision.get("direct_marketing_pv_store_export_limit_ramp_bypass")),
        "external_derating": {
            "active": bool(
                (decision or {}).get("direct_marketing_external_derating_active")
                or pv_store_diag_value("direct_marketing_external_derating_active", "external_derating_active", False)
                or external_derating.get("active")
            ),
            "signal_active": bool(external_derating.get("signal_active")),
            "frame_fresh": bool(external_derating.get("frame_fresh")),
            "sample_valid": bool(external_derating.get("sample_valid")),
            "decision_usable": bool(external_derating.get("decision_usable")),
            "timestamp_known": bool(external_derating.get("timestamp_known")),
            "live_age_s": external_derating.get("live_age_s"),
            "max_age_s": external_derating.get("max_age_s"),
            "source": (
                (decision or {}).get("direct_marketing_external_derating_source")
                or pv_store_diag_value("direct_marketing_external_derating_source", "external_derating_source")
                or external_derating.get("source")
            ),
            "limit_w": (
                (decision or {}).get("direct_marketing_external_derating_limit_w")
                if (decision or {}).get("direct_marketing_external_derating_limit_w") is not None
                else pv_store_diag_value("direct_marketing_external_derating_limit_w", "external_derating_limit_w", external_derating.get("limit_w"))
            ),
            "ac_power_limit_w": safe_int(
                (decision or {}).get("direct_marketing_external_derating_ac_power_limit_w"),
                safe_int(external_derating.get("ac_power_limit_w"), 0),
            ),
            "derate_at_power_w": safe_int(
                (decision or {}).get("direct_marketing_external_derating_power_w"),
                safe_int(external_derating.get("derate_at_power_w"), 0),
            ),
            "derate_at_percent": safe_float(
                (decision or {}).get("direct_marketing_external_derating_percent"),
                safe_float(external_derating.get("derate_at_percent"), 0.0),
            ),
        },
        "controller_owner": str(direct.get("controller_owner") or ""),
        "plan_owner": str(direct.get("plan_owner") or ""),
        "owner_contract_version": safe_int(
            direct.get("owner_contract_version", flags.get("owner_contract_version")),
            0,
        ),
        "contract_expected_version": DIRECT_MARKETING_CONTRACT_VERSION,
        "reason": str(direct.get("reason") or ""),
        "blocked_reasons": sorted(blockers),
        "global_gate_blockers": reason_projection["global_gate_blockers"],
        "candidate_rejections": reason_projection["candidate_rejections"],
        "allocation_diagnostics": reason_projection["allocation_diagnostics"],
        "contract_errors": sorted(set(contract_errors)),
        "contract_warnings": contract_warnings,
        "price_domain_policy": str(
            (direct.get("flags") if isinstance(direct.get("flags"), dict) else {}).get(
                "price_domain_policy",
                "negative_hard_eeg_soft_score_fallback",
            )
        ),
        "current_action": current_action,
        "current_window": _direct_marketing_public_window(current_window, now_ms) if isinstance(current_window, dict) else None,
        "upcoming_windows": _direct_marketing_public_windows(direct, now_s),
        "expected_profit_ct_per_kwh": round(profit_ct, 2) if profit_ct is not None else None,
        "economics": economics,
        "settlement_accounting": direct.get("settlement_accounting") if isinstance(direct.get("settlement_accounting"), dict) else {},
        "reserve": reserve,
        "flags": {
            "export_enable": bool(flags.get("export_enable")),
            "grid_charge_enable": bool(flags.get("grid_charge_enable")),
            "arbitrage_release_allowed": False,
            "pv_store_enable": bool(flags.get("pv_store_enable")),
            "negative_price_no_export": bool(flags.get("negative_price_no_export")),
            "low_price_no_export": bool(flags.get("low_price_no_export")),
            "low_price_curtail_enable": bool(flags.get("low_price_curtail_enable")),
            "max_export_w": safe_int(flags.get("max_export_w"), 0),
            "max_grid_charge_w": safe_int(flags.get("max_grid_charge_w"), 0),
            "pv_store_max_w": safe_int(flags.get("pv_store_max_w"), 0),
            "pv_store_min_surplus_w": safe_int(flags.get("pv_store_min_surplus_w"), 0),
            "pv_store_import_guard_w": safe_int(flags.get("pv_store_import_guard_w"), 0),
            "pv_store_min_hold_s": safe_int(flags.get("pv_store_min_hold_s"), 0),
            "pv_store_ramp_step_w": safe_int(flags.get("pv_store_ramp_step_w"), 0),
            "pv_store_dc_only_enable": bool(flags.get("pv_store_dc_only_enable")),
            "pv_store_external_ac_guard_w": safe_int(flags.get("pv_store_external_ac_guard_w"), 0),
            "pv_store_export_limit_guard_w": safe_int(flags.get("pv_store_export_limit_guard_w"), 0),
            "pv_store_export_limit_ramp_bypass_w": safe_int(flags.get("pv_store_export_limit_ramp_bypass_w"), 0),
            "pv_store_threshold_ct": flags.get("pv_store_threshold_ct"),
            "pv_store_threshold_source": flags.get("pv_store_threshold_source"),
            "negative_headroom_enable": bool(flags.get("negative_headroom_enable")),
            "negative_headroom_lookahead_min": safe_float(flags.get("negative_headroom_lookahead_min"), 0.0),
            "negative_headroom_min_window_min": safe_float(flags.get("negative_headroom_min_window_min"), 0.0),
            "negative_headroom_min_surplus_wh": safe_int(flags.get("negative_headroom_min_surplus_wh"), 0),
            "negative_headroom_buffer_pct": safe_float(flags.get("negative_headroom_buffer_pct"), 0.0),
            "commands_allowed": bool(flags.get("commands_allowed")),
        },
        "ts": int(now_s),
    }


def _direct_marketing_report_day(ts: int) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(ts)).date().isoformat()
    except Exception:
        return datetime.date.today().isoformat()


def _direct_marketing_report_empty(day: str, ts: int) -> Dict[str, Any]:
    return {
        "date": day,
        "created_ts": int(ts),
        "last_ts": int(ts),
        "cycles": 0,
        "enabled_cycles": 0,
        "shadow_cycles": 0,
        "active_cycles": 0,
        "commands_allowed_cycles": 0,
        "mode_counts": {},
        "state_counts": {},
        "blocker_counts": {},
        "warning_counts": {},
        "window_action_counts": {},
        "window_duration_h_by_action": {},
        "window_kwh_by_action": {},
        "window_keys": [],
        "windows": [],
        "theoretical_export_kwh": 0.0,
        "theoretical_pv_store_kwh": 0.0,
        "theoretical_grid_charge_kwh": 0.0,
        "theoretical_window_profit_eur": 0.0,
        "real_export_kwh": 0.0,
        "real_profit_export_kwh": 0.0,
        "real_headroom_export_kwh": 0.0,
        "real_pv_store_kwh": 0.0,
        "real_export_revenue_eur": 0.0,
        "real_gross_export_revenue_eur": 0.0,
        "real_variable_fee_cost_eur": 0.0,
        "real_net_export_revenue_eur": 0.0,
        "real_expected_profit_eur": 0.0,
        "policy_export_budget_kwh": 0.0,
        "export_tracking_error_kwh": 0.0,
        "missed_export_kwh": 0.0,
        "missed_export_cycles": 0,
        "policy_battery_throughput_kwh": 0.0,
        "policy_battery_discharge_kwh": 0.0,
        "policy_battery_charge_kwh": 0.0,
        "last_energy_sample_ts": None,
        "house_supply_windows": 0,
        "headroom_windows": 0,
        "pv_shift_profit_ok_cycles": 0,
        "grid_profit_ok_cycles": 0,
        "external_derating_cycles": 0,
        "export_execution_state_counts": {},
        "export_compliance_confirmed_cycles": 0,
        "export_violation_cycles": 0,
        "export_unavoidable_cycles": 0,
        "best_pv_shift_spread_ct_per_kwh": None,
        "best_grid_spread_ct_per_kwh": None,
        "latest_economics": {},
        "latest_settlement_accounting": {},
        "latest_reserve": {},
        "latest_external_derating": {},
        "latest_export_execution": {},
        "latest_export_tracking": {},
        "latest_summary": {},
    }


def _direct_marketing_report_count(container: Dict[str, Any], key: str, amount: int = 1) -> None:
    if not key:
        key = "unknown"
    container[key] = safe_int(container.get(key), 0) + int(amount)


def _direct_marketing_report_add_float(container: Dict[str, Any], key: str, amount: float) -> None:
    container[key] = round(safe_float(container.get(key), 0.0) + float(amount or 0.0), 3)


def _direct_marketing_report_max(report: Dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    current = report.get(key)
    numeric = safe_float(value, 0.0)
    if current is None or numeric > safe_float(current, -999999.0):
        report[key] = round(numeric, 2)


def _direct_marketing_window_duration_h(window: Dict[str, Any]) -> float:
    start_ms = safe_float(window.get("start_ts"), 0.0)
    end_ms = safe_float(window.get("end_ts"), 0.0)
    if end_ms <= start_ms:
        return 0.0
    return max(0.0, (end_ms - start_ms) / 3600000.0)


def _direct_marketing_window_key(window: Dict[str, Any]) -> str:
    return "%s:%d:%d" % (
        str(window.get("action") or "unknown"),
        safe_int(window.get("start_ts"), 0),
        safe_int(window.get("end_ts"), 0),
    )


def _direct_marketing_report_window_kwh(window: Dict[str, Any]) -> float:
    duration_h = _direct_marketing_window_duration_h(window)
    power_w = max(0, safe_int(window.get("max_power_w"), 0))
    return round((power_w * duration_h) / 1000.0, 3)


def _direct_marketing_report_window_spread_ct(window: Dict[str, Any], economics: Dict[str, Any]) -> Optional[float]:
    if window.get("spread_ct_per_kwh") is not None:
        return safe_float(window.get("spread_ct_per_kwh"), 0.0)
    action = str(window.get("action") or "")
    if action == "eco_plus_export_candidate" or action in DIRECT_MARKETING_PV_STORE_ACTIONS:
        return safe_float(economics.get("pv_shift_spread_ct_per_kwh"), 0.0)
    if action in ("arbitrage_grid_charge_candidate", "arbitrage_export_candidate"):
        return safe_float(economics.get("grid_spread_ct_per_kwh"), 0.0)
    return None


def _direct_marketing_report_windows_overlap(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    if str(left.get("action") or "") != str(right.get("action") or ""):
        return False
    left_start = safe_int(left.get("start_ts"), 0)
    left_end = safe_int(left.get("end_ts"), 0)
    right_start = safe_int(right.get("start_ts"), 0)
    right_end = safe_int(right.get("end_ts"), 0)
    return left_start < right_end and right_start < left_end


def _direct_marketing_report_rebuild_window_stats(report: Dict[str, Any]) -> None:
    report["window_action_counts"] = {}
    report["window_duration_h_by_action"] = {}
    report["window_kwh_by_action"] = {}
    report["window_keys"] = []
    report["theoretical_export_kwh"] = 0.0
    report["theoretical_pv_store_kwh"] = 0.0
    report["theoretical_grid_charge_kwh"] = 0.0
    report["theoretical_window_profit_eur"] = 0.0
    report["house_supply_windows"] = 0
    report["headroom_windows"] = 0

    windows = report.get("windows") if isinstance(report.get("windows"), list) else []
    for window in windows:
        if not isinstance(window, dict):
            continue
        action = str(window.get("action") or "unknown")
        duration_h = _direct_marketing_window_duration_h(window)
        window_kwh = _direct_marketing_report_window_kwh(window)
        key = _direct_marketing_window_key(window)
        window["key"] = key
        window["duration_h"] = round(duration_h, 2)
        window["theoretical_kwh"] = window_kwh
        report["window_keys"].append(key)
        _direct_marketing_report_count(report.setdefault("window_action_counts", {}), action)
        _direct_marketing_report_add_float(report.setdefault("window_duration_h_by_action", {}), action, duration_h)
        _direct_marketing_report_add_float(report.setdefault("window_kwh_by_action", {}), action, window_kwh)

        if action in DIRECT_MARKETING_EXPORT_ACTIONS:
            report["theoretical_export_kwh"] = round(safe_float(report.get("theoretical_export_kwh"), 0.0) + window_kwh, 3)
        elif action in DIRECT_MARKETING_PV_STORE_ACTIONS:
            report["theoretical_pv_store_kwh"] = round(safe_float(report.get("theoretical_pv_store_kwh"), 0.0) + window_kwh, 3)
        elif action in DIRECT_MARKETING_GRID_ACTIONS:
            report["theoretical_grid_charge_kwh"] = round(safe_float(report.get("theoretical_grid_charge_kwh"), 0.0) + window_kwh, 3)
        elif action in ("safe_house_supply", "eco_house_supply", "eco_plus_house_supply"):
            report["house_supply_windows"] = safe_int(report.get("house_supply_windows"), 0) + 1
        elif action in ("keep_headroom", "eco_plus_store_pv_candidate", "eco_plus_negative_headroom_hold"):
            report["headroom_windows"] = safe_int(report.get("headroom_windows"), 0) + 1

        spread_ct = _direct_marketing_report_window_spread_ct(window, {})
        if spread_ct is not None and window_kwh > 0:
            report["theoretical_window_profit_eur"] = round(
                safe_float(report.get("theoretical_window_profit_eur"), 0.0)
                + max(0.0, safe_float(spread_ct, 0.0)) * window_kwh / 100.0,
                3,
            )
    if len(report["window_keys"]) > 160:
        report["window_keys"] = report["window_keys"][-160:]


def _direct_marketing_report_compact_windows(report: Dict[str, Any]) -> None:
    raw_windows = report.get("windows") if isinstance(report.get("windows"), list) else []
    compacted: List[Dict[str, Any]] = []
    for raw in sorted(
        (w for w in raw_windows if isinstance(w, dict)),
        key=lambda item: (str(item.get("action") or ""), safe_int(item.get("start_ts"), 0), safe_int(item.get("end_ts"), 0)),
    ):
        candidate = dict(raw)
        candidate["start_ts"] = safe_int(candidate.get("start_ts"), 0)
        candidate["end_ts"] = safe_int(candidate.get("end_ts"), 0)
        if candidate["end_ts"] <= candidate["start_ts"]:
            continue
        merged = False
        for existing in compacted:
            if not _direct_marketing_report_windows_overlap(existing, candidate):
                continue
            existing["start_ts"] = min(safe_int(existing.get("start_ts"), 0), candidate["start_ts"])
            existing["end_ts"] = max(safe_int(existing.get("end_ts"), 0), candidate["end_ts"])
            existing["max_power_w"] = max(safe_int(existing.get("max_power_w"), 0), safe_int(candidate.get("max_power_w"), 0))
            if candidate.get("avg_market_ct") is not None:
                existing["avg_market_ct"] = candidate.get("avg_market_ct")
            if candidate.get("avg_billing_ct") is not None:
                existing["avg_billing_ct"] = candidate.get("avg_billing_ct")
            if candidate.get("spread_ct_per_kwh") is not None:
                existing["spread_ct_per_kwh"] = candidate.get("spread_ct_per_kwh")
            for key in (
                "net_sell_ct",
                "pv_store_price_class",
                "pv_store_soft_threshold",
                "pv_store_threshold_ct",
                "pv_store_threshold_source",
                "pv_store_headroom_limited",
                "pv_store_headroom_next_reason",
            ):
                if candidate.get(key) is not None:
                    existing[key] = candidate.get(key)
            existing["reason"] = candidate.get("reason") or existing.get("reason")
            existing["start_t"] = existing.get("start_t") or candidate.get("start_t")
            existing["end_t"] = candidate.get("end_t") or existing.get("end_t")
            merged = True
            break
        if not merged:
            compacted.append(candidate)

    compacted.sort(key=lambda item: safe_int(item.get("start_ts"), 0))
    if len(compacted) > 48:
        compacted = compacted[-48:]
    report["windows"] = compacted
    _direct_marketing_report_rebuild_window_stats(report)


def _direct_marketing_report_add_window(
    report: Dict[str, Any],
    window: Dict[str, Any],
    economics: Dict[str, Any],
) -> None:
    if not isinstance(window, dict):
        return

    action = str(window.get("action") or "unknown")
    duration_h = _direct_marketing_window_duration_h(window)
    power_w = max(0, safe_int(window.get("max_power_w"), 0))
    if duration_h <= 0.0 or power_w <= 0:
        return

    spread_ct = None
    if action == "eco_plus_export_candidate":
        spread_ct = economics.get("pv_shift_spread_ct_per_kwh")
    elif action in DIRECT_MARKETING_PV_STORE_ACTIONS:
        spread_ct = economics.get("pv_shift_spread_ct_per_kwh")
    elif action in ("arbitrage_grid_charge_candidate", "arbitrage_export_candidate"):
        spread_ct = economics.get("grid_spread_ct_per_kwh")

    public = {
        "action": action,
        "start_ts": safe_int(window.get("start_ts"), 0),
        "end_ts": safe_int(window.get("end_ts"), 0),
        "start_t": window.get("start_t"),
        "end_t": window.get("end_t"),
        "duration_h": round(duration_h, 2),
        "max_power_w": power_w,
        "theoretical_kwh": _direct_marketing_report_window_kwh(window),
        "avg_market_ct": window.get("avg_market_ct"),
        "avg_billing_ct": window.get("avg_billing_ct"),
        "net_sell_ct": window.get("net_sell_ct"),
        "pv_store_price_class": window.get("pv_store_price_class"),
        "pv_store_soft_threshold": bool(window.get("pv_store_soft_threshold")),
        "pv_store_threshold_ct": window.get("pv_store_threshold_ct"),
        "pv_store_threshold_source": window.get("pv_store_threshold_source"),
        "pv_store_headroom_limited": bool(window.get("pv_store_headroom_limited")),
        "pv_store_headroom_next_reason": window.get("pv_store_headroom_next_reason"),
        "spread_ct_per_kwh": round(safe_float(spread_ct, 0.0), 3) if spread_ct is not None else None,
        "reason": window.get("reason"),
    }
    public["key"] = _direct_marketing_window_key(public)
    windows = report.setdefault("windows", [])
    windows.append(public)
    _direct_marketing_report_compact_windows(report)


def update_direct_marketing_daily_report(
    payload: Dict[str, Any],
    plan: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    monitor = payload.get("direct_marketing_monitor") if isinstance(payload.get("direct_marketing_monitor"), dict) else {}
    if not monitor:
        return None

    ts = safe_int(payload.get("ts"), int(time.time()))
    day = _direct_marketing_report_day(ts)
    report = _direct_marketing_report_state.get("report")
    if not isinstance(report, dict) or report.get("date") != day:
        existing = read_json_file(DIRECT_MARKETING_REPORT_F, max_age_s=90000)
        if isinstance(existing, dict) and existing.get("date") == day:
            report = existing
        else:
            report = _direct_marketing_report_empty(day, ts)
    _direct_marketing_report_compact_windows(report)

    sample_ts = (
        safe_float(payload.get("ts_float"), 0.0)
        if payload.get("ts_float") is not None
        else time.time()
    )
    previous_sample_ts = safe_float(report.get("last_energy_sample_ts"), sample_ts)
    elapsed_s = sample_ts - previous_sample_ts
    if elapsed_s < 0.0 or elapsed_s > 30.0:
        elapsed_s = 0.0
    report["last_energy_sample_ts"] = round(sample_ts, 3)
    report["last_ts"] = int(ts)
    report["cycles"] = safe_int(report.get("cycles"), 0) + 1
    if monitor.get("enabled"):
        report["enabled_cycles"] = safe_int(report.get("enabled_cycles"), 0) + 1
    if monitor.get("shadow"):
        report["shadow_cycles"] = safe_int(report.get("shadow_cycles"), 0) + 1
    if monitor.get("active"):
        report["active_cycles"] = safe_int(report.get("active_cycles"), 0) + 1
    if monitor.get("commands_allowed"):
        report["commands_allowed_cycles"] = safe_int(report.get("commands_allowed_cycles"), 0) + 1

    _direct_marketing_report_count(report.setdefault("mode_counts", {}), str(monitor.get("mode") or "unknown"))
    _direct_marketing_report_count(report.setdefault("state_counts", {}), str(monitor.get("state") or "unknown"))
    for reason in monitor.get("blocked_reasons") if isinstance(monitor.get("blocked_reasons"), list) else []:
        _direct_marketing_report_count(report.setdefault("blocker_counts", {}), str(reason or "unknown"))
    for warning in monitor.get("contract_warnings") if isinstance(monitor.get("contract_warnings"), list) else []:
        _direct_marketing_report_count(report.setdefault("warning_counts", {}), str(warning or "unknown"))

    policy_target = str(monitor.get("policy_target_state") or "").strip().upper()
    policy_decision = payload.get("direct_marketing_policy_decision") if isinstance(payload.get("direct_marketing_policy_decision"), dict) else {}
    policy_storage_budget = policy_decision.get("storage_budget") if isinstance(policy_decision.get("storage_budget"), dict) else {}
    policy_export_budget_w = max(0.0, safe_float(policy_storage_budget.get("export_budget_w"), 0.0))
    policy_active = bool(
        monitor.get("active")
        and monitor.get("policy_commands_allowed")
        and policy_target in {"FORCE_EXPORT", "HEADROOM_EXPORT", "FORCE_CHARGE_PV"}
    )
    decision_state = str(payload.get("state") or "")
    actual_policy_export = bool(
        policy_active
        and policy_target in {"FORCE_EXPORT", "HEADROOM_EXPORT"}
        and safe_int(payload.get("mode"), MODE_AUTO) == MODE_DISCH
        and decision_state.startswith((
            "direct_marketing_eco_plus_export",
            "direct_marketing_eco_plus_headroom_export",
        ))
    )
    bat_w = safe_float(payload.get("bat_w"), 0.0)
    grid_w = safe_float(payload.get("grid_w"), 0.0)
    if policy_active and elapsed_s > 0.0:
        throughput_kwh = abs(bat_w) * elapsed_s / 3600000.0
        report["policy_battery_throughput_kwh"] = round(
            safe_float(report.get("policy_battery_throughput_kwh"), 0.0) + throughput_kwh,
            4,
        )
        throughput_key = "policy_battery_discharge_kwh" if bat_w < 0.0 else "policy_battery_charge_kwh"
        report[throughput_key] = round(safe_float(report.get(throughput_key), 0.0) + throughput_kwh, 4)

    actual_export_kwh = 0.0
    if actual_policy_export and elapsed_s > 0.0:
        battery_to_grid_w = min(max(0.0, -bat_w), max(0.0, -grid_w))
        export_kwh = battery_to_grid_w * elapsed_s / 3600000.0
        actual_export_kwh = export_kwh
        report["real_export_kwh"] = round(safe_float(report.get("real_export_kwh"), 0.0) + export_kwh, 4)
        target_key = "real_headroom_export_kwh" if policy_target == "HEADROOM_EXPORT" else "real_profit_export_kwh"
        report[target_key] = round(safe_float(report.get(target_key), 0.0) + export_kwh, 4)
        current_window = monitor.get("current_window") if isinstance(monitor.get("current_window"), dict) else {}
        net_sell_ct = safe_float(
            current_window.get("net_sell_ct"),
            safe_float(current_window.get("avg_market_ct"), 0.0),
        )
        gross_sell_ct = safe_float(current_window.get("gross_sell_ct"), net_sell_ct)
        fee_cost_ct = max(0.0, safe_float(current_window.get("fee_cost_ct"), gross_sell_ct - net_sell_ct))
        report["real_export_revenue_eur"] = round(
            safe_float(report.get("real_export_revenue_eur"), 0.0) + export_kwh * net_sell_ct / 100.0,
            4,
        )
        report["real_gross_export_revenue_eur"] = round(
            safe_float(report.get("real_gross_export_revenue_eur"), 0.0) + export_kwh * gross_sell_ct / 100.0,
            4,
        )
        report["real_variable_fee_cost_eur"] = round(
            safe_float(report.get("real_variable_fee_cost_eur"), 0.0) + export_kwh * fee_cost_ct / 100.0,
            4,
        )
        report["real_net_export_revenue_eur"] = round(
            safe_float(report.get("real_net_export_revenue_eur"), 0.0) + export_kwh * net_sell_ct / 100.0,
            4,
        )
        policy_economics = policy_decision.get("economics") if isinstance(policy_decision.get("economics"), dict) else {}
        margin_ct = policy_economics.get("margin_ct_kwh")
        if margin_ct is not None:
            report["real_expected_profit_eur"] = round(
                safe_float(report.get("real_expected_profit_eur"), 0.0)
                + export_kwh * safe_float(margin_ct, 0.0) / 100.0,
                4,
            )
    if policy_active and policy_target in {"FORCE_EXPORT", "HEADROOM_EXPORT"} and elapsed_s > 0.0:
        budget_kwh = policy_export_budget_w * elapsed_s / 3600000.0
        tracking_error_kwh = max(0.0, budget_kwh - actual_export_kwh)
        report["policy_export_budget_kwh"] = round(
            safe_float(report.get("policy_export_budget_kwh"), 0.0) + budget_kwh,
            4,
        )
        report["export_tracking_error_kwh"] = round(
            safe_float(report.get("export_tracking_error_kwh"), 0.0) + tracking_error_kwh,
            4,
        )
        if tracking_error_kwh > 0.0001:
            report["missed_export_kwh"] = round(
                safe_float(report.get("missed_export_kwh"), 0.0) + tracking_error_kwh,
                4,
            )
            report["missed_export_cycles"] = safe_int(report.get("missed_export_cycles"), 0) + 1
    elif policy_active and elapsed_s > 0.0 and policy_target == "FORCE_CHARGE_PV":
        import_guard_w = max(0.0, safe_float(cfg.get("direct_marketing_pv_store_import_guard_w"), 80.0))
        grid_import_w = max(0.0, grid_w - import_guard_w)
        pv_charge_w = max(0.0, max(0.0, bat_w) - grid_import_w)
        charge_kwh = pv_charge_w * elapsed_s / 3600000.0
        report["real_pv_store_kwh"] = round(
            safe_float(report.get("real_pv_store_kwh"), 0.0) + charge_kwh,
            4,
        )

    economics = monitor.get("economics") if isinstance(monitor.get("economics"), dict) else {}
    reserve = monitor.get("reserve") if isinstance(monitor.get("reserve"), dict) else {}
    direct_plan = plan.get("direct_marketing") if isinstance(plan, dict) and isinstance(plan.get("direct_marketing"), dict) else plan
    settlement_accounting = (
        direct_plan.get("settlement_accounting")
        if isinstance(direct_plan, dict) and isinstance(direct_plan.get("settlement_accounting"), dict)
        else {}
    )
    if not settlement_accounting and isinstance(monitor.get("settlement_accounting"), dict):
        settlement_accounting = monitor.get("settlement_accounting")
    report["latest_settlement_accounting"] = dict(settlement_accounting)
    report["latest_economics"] = {
        "pv_shift_spread_ct_per_kwh": economics.get("pv_shift_spread_ct_per_kwh"),
        "pv_shift_margin_pct": economics.get("pv_shift_margin_pct"),
        "pv_shift_profit_ok": bool(economics.get("pv_shift_profit_ok")),
        "grid_spread_ct_per_kwh": economics.get("grid_spread_ct_per_kwh"),
        "grid_margin_pct": economics.get("grid_margin_pct"),
        "grid_profit_ok": bool(economics.get("grid_profit_ok")),
        "best_low_t": economics.get("best_low_t"),
        "best_high_t": economics.get("best_high_t"),
    }
    report["latest_reserve"] = {
        "current_soc_pct": reserve.get("current_soc_pct"),
        "effective_min_soc_pct": reserve.get("effective_min_soc_pct"),
        "available_export_wh": reserve.get("available_export_wh"),
    }
    external_derating = monitor.get("external_derating") if isinstance(monitor.get("external_derating"), dict) else {}
    report["latest_external_derating"] = {
        "active": bool(external_derating.get("active")),
        "source": external_derating.get("source"),
        "limit_w": external_derating.get("limit_w"),
        "ac_power_limit_w": external_derating.get("ac_power_limit_w"),
        "derate_at_power_w": external_derating.get("derate_at_power_w"),
        "derate_at_percent": external_derating.get("derate_at_percent"),
    }
    if external_derating.get("active"):
        report["external_derating_cycles"] = safe_int(report.get("external_derating_cycles"), 0) + 1
    export_execution = monitor.get("export_execution") if isinstance(monitor.get("export_execution"), dict) else {}
    report["latest_export_execution"] = dict(export_execution)
    _direct_marketing_report_count(
        report.setdefault("export_execution_state_counts", {}),
        str(export_execution.get("state") or "not_available"),
    )
    if export_execution.get("compliance_confirmed"):
        report["export_compliance_confirmed_cycles"] = safe_int(report.get("export_compliance_confirmed_cycles"), 0) + 1
    if export_execution.get("violation"):
        report["export_violation_cycles"] = safe_int(report.get("export_violation_cycles"), 0) + 1
    if export_execution.get("unavoidable"):
        report["export_unavoidable_cycles"] = safe_int(report.get("export_unavoidable_cycles"), 0) + 1
    _direct_marketing_report_max(report, "best_pv_shift_spread_ct_per_kwh", economics.get("pv_shift_spread_ct_per_kwh"))
    _direct_marketing_report_max(report, "best_grid_spread_ct_per_kwh", economics.get("grid_spread_ct_per_kwh"))
    if economics.get("pv_shift_profit_ok"):
        report["pv_shift_profit_ok_cycles"] = safe_int(report.get("pv_shift_profit_ok_cycles"), 0) + 1
    if economics.get("grid_profit_ok"):
        report["grid_profit_ok_cycles"] = safe_int(report.get("grid_profit_ok_cycles"), 0) + 1

    windows: List[Dict[str, Any]] = []
    current_window = monitor.get("current_window") if isinstance(monitor.get("current_window"), dict) else None
    if current_window:
        windows.append(current_window)
    for window in monitor.get("upcoming_windows") if isinstance(monitor.get("upcoming_windows"), list) else []:
        if isinstance(window, dict):
            windows.append(window)
    for window in windows:
        _direct_marketing_report_add_window(report, window, economics)

    next_window = windows[0] if windows else None
    report["latest_summary"] = {
        "mode": monitor.get("mode"),
        "state": monitor.get("state"),
        "active": bool(monitor.get("active")),
        "shadow": bool(monitor.get("shadow")),
        "commands_allowed": bool(monitor.get("commands_allowed")),
        "blocked_reasons": monitor.get("blocked_reasons") if isinstance(monitor.get("blocked_reasons"), list) else [],
        "contract_warnings": monitor.get("contract_warnings") if isinstance(monitor.get("contract_warnings"), list) else [],
        "external_derating": report.get("latest_external_derating"),
        "export_execution": report.get("latest_export_execution"),
        "next_action": next_window.get("action") if isinstance(next_window, dict) else None,
        "next_start_t": next_window.get("start_t") if isinstance(next_window, dict) else None,
        "next_end_t": next_window.get("end_t") if isinstance(next_window, dict) else None,
    }

    _direct_marketing_report_state["report"] = report
    atomic_write(DIRECT_MARKETING_REPORT_F, report, indent=2)
    return report


def price_storage_hold_requested(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    wb_intent: Dict[str, Any],
    now_s: float,
) -> bool:
    wallbox_w = abs(safe_float(live.get("Wallbox_Power"), 0.0))
    if wallbox_w < max(500.0, safe_float(cfg.get("storage_unmanaged_wb_hold_min_w"), 500.0)):
        return False

    request = str(wb_intent.get("battery_request", "") or "").strip().lower()
    if request == "allow_discharge":
        return False
    intent_fresh = bool(wb_intent) and now_s - safe_float(wb_intent.get("ts"), 0.0) <= 60.0
    wb_mode = normalize_wb_mode(wb_intent.get("wb_mode_active", cfg.get("wb1_mode", 0)))
    regulated_by_us = bool(
        intent_fresh
        and wb_mode != MODE_OFF
        and (
            wb_intent.get("active")
            or wb_intent.get("start_requested")
            or safe_int(wb_intent.get("cap_amp", wb_intent.get("set_amp")), 0) > 0
        )
    )
    unmanaged = not regulated_by_us
    controlled_floor_request = bool(
        intent_fresh
        and regulated_by_us
        and request == "hold_discharge"
        and not bool(wb_intent.get("scheduled_slot_active"))
        and not bool(wb_intent.get("price_boost_active"))
        and not bool(wb_intent.get("price_plan_storage_protect"))
        and not bool(wb_intent.get("wbminsoc_gate_open", True))
    )
    if controlled_floor_request:
        return False

    awattar_mode = safe_int(plan.get("awattar_mode"), 1)
    price_ct = current_price_ct_from_plan(plan)
    limit_ct = price_limit_ct(cfg)
    price_limit_allows = bool(price_ct is not None and limit_ct > 0 and price_ct <= limit_ct + 0.001)
    now_ms = int(now_s * 1000.0)
    cheap_active = cheap_grid_charge_active(plan, now_ms)
    storm_active = storm_grid_charge_active(plan, now_ms)
    market_authoritative = market_economics_plan_authoritative(plan, now_s)
    # The old "current slot is simply the cheapest" heuristic is only a
    # fallback when the market planner is absent or stale. A valid market plan
    # may explicitly decide to do nothing because PV/storage already cover the
    # expensive horizon.
    legacy_low_price_slot = bool(
        current_slot_is_low_price(plan, now_s)
        and not market_authoritative
    )
    legacy_awattar_price_mode = awattar_mode in (0, 2)
    if awattar_mode == 2 and market_authoritative and not (cheap_active or storm_active):
        legacy_awattar_price_mode = False
    price_window_active = bool(
        cheap_active
        or storm_active
    )
    price_hold_requested = bool(
        request == "hold_discharge"
        or wb_intent.get("scheduled_slot_active")
        or wb_intent.get("price_boost_active")
        or wb_intent.get("price_plan_storage_protect")
        or price_window_active
        or (
            unmanaged
            and (
                legacy_awattar_price_mode
                or price_limit_allows
                or legacy_low_price_slot
            )
        )
    )
    return price_hold_requested


def wallbox_intent_external_manager(wb_intent: Dict[str, Any]) -> bool:
    return bool(
        wb_intent.get("external_wallbox_manager")
        or wb_intent.get("openwb_primary_observe_only")
        or wb_intent.get("openwb_primary_pv_mode_active")
        or wb_intent.get("autonomous_wallbox")
    )


def controlled_wallbox_floor_pv_wait_requested(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_native: Optional[Dict[str, Any]],
    now_s: float,
) -> bool:
    """PV-curve behaviour for a controlled wallbox waiting below wbminSoC."""
    intent_ts = safe_float(wb_intent.get("ts"), 0.0)
    intent_fresh = bool(wb_intent) and (intent_ts <= 0.0 or now_s - intent_ts <= 90.0)
    if not intent_fresh:
        return False
    wb_mode = normalize_wb_mode(wb_intent.get("wb_mode_active", cfg.get("wb1_mode", 0)))
    request = str(wb_intent.get("battery_request", "") or "").strip().lower()
    if (
        not storage_floor_mode(wb_mode)
        or request not in ("", "none")
        or bool(wb_intent.get("wbminsoc_gate_open", True))
        or wallbox_intent_external_manager(wb_intent)
        or wb_intent.get("scheduled_slot_active")
        or wb_intent.get("price_boost_active")
        or wb_intent.get("price_plan_storage_protect")
    ):
        return False
    car_waiting = bool(
        wb_intent.get("active")
        or wb_intent.get("car_active")
        or wb_intent.get("connected")
        or wb_intent.get("plugged")
    )
    if not car_waiting:
        return False
    wb_native = wb_native or {}
    if bool(wb_intent.get("charging_active") or wb_native.get("charging_active")):
        return False
    cap_amp = safe_float(wb_intent.get("cap_amp", wb_intent.get("set_amp")), 0.0)
    wallbox_w = max(
        abs(safe_float(wb_intent.get("wb_power_w"), 0.0)),
        abs(safe_float(live.get("Wallbox_Power"), 0.0)),
        abs(safe_float(wb_native.get("total_power_w"), 0.0)),
    )
    return bool(cap_amp <= 0.0 and wallbox_w <= 250.0)


def controlled_wallbox_auto_freerun_requested(
    cfg: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_mode: int,
    wallbox_w: float,
    now_s: float,
    *,
    wb_intent_fresh: Optional[bool] = None,
) -> bool:
    if wb_intent_fresh is None:
        wb_intent_fresh = bool(wb_intent) and now_s - safe_float(wb_intent.get("ts"), 0.0) <= 60.0
    if not wb_intent_fresh or wb_mode == MODE_OFF:
        return False

    request = str(wb_intent.get("battery_request", "") or "").strip().lower()
    if request != "allow_discharge":
        return False

    if (
        wallbox_intent_external_manager(wb_intent)
        or wb_intent.get("scheduled_slot_active")
        or wb_intent.get("price_boost_active")
        or wb_intent.get("price_plan_storage_protect")
        or wb_intent.get("price_opt_active")
        or wb_intent.get("battery_departure_active")
        or wb_intent.get("bev_full_blocked")
    ):
        return False
    if "wbminsoc_gate_open" in wb_intent and not bool(wb_intent.get("wbminsoc_gate_open")):
        return False

    observed_w = max(abs(safe_float(wallbox_w, 0.0)), abs(safe_float(wb_intent.get("wb_power_w"), 0.0)))
    cap_amp = safe_float(wb_intent.get("cap_amp", wb_intent.get("set_amp")), 0.0)
    return bool(
        wb_intent.get("charging_active")
        or wb_intent.get("start_requested")
        or observed_w > 250.0
        or (
            wb_intent.get("active")
            and cap_amp > 0.0
            and (
                wb_intent.get("car_active")
                or wb_intent.get("connected")
                or wb_intent.get("plugged")
            )
        )
    )


def _observe_wallbox_storage_policy_value(
    cfg: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_mode: int,
    *,
    allow_inactive: bool = False,
) -> str:
    """Liefert die exakte Speicherpolicy für eine Wallbox im Beobachtungsmodus."""
    if wb_mode != MODE_OFF:
        return ""
    reason = str(wb_intent.get("reason") or "").strip().lower()
    request = str(wb_intent.get("battery_request") or "").strip().lower()
    if reason in ("no_vehicle_connected", "manual_pause", "bev_full_blocked") or request == "release":
        return ""
    active = bool(
        wb_intent.get("active")
        or wb_intent.get("car_active")
        or wb_intent.get("charging_active")
        or wb_intent.get("connected")
        or wb_intent.get("plugged")
        or abs(safe_float(wb_intent.get("wb_power_w"), 0.0)) > 500.0
    )
    if not active and not allow_inactive:
        return ""
    raw_policy = wb_intent.get("observe_storage_policy")
    if raw_policy is None:
        wb_id = safe_int(wb_intent.get("wb_id", wb_intent.get("charger_id", 1)), 1)
        for key in (
            f"wb{wb_id}_observe_storage_policy",
            f"wb{wb_id}_storage_observe_policy",
            "wb_observe_storage_policy",
        ):
            if key in cfg:
                raw_policy = cfg.get(key)
                break
    if raw_policy is None:
        for wb_id in (1, 2):
            if normalize_wb_mode(cfg.get(f"wb{wb_id}_mode", MODE_OFF)) != MODE_OFF:
                continue
            for key in (f"wb{wb_id}_observe_storage_policy", f"wb{wb_id}_storage_observe_policy"):
                if key in cfg:
                    raw_policy = cfg.get(key)
                    break
            if raw_policy is not None:
                break
    return str(raw_policy or "").strip().lower()


def _observe_wallbox_storage_policy_requested(
    cfg: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_mode: int,
    *,
    allow_inactive: bool = False,
) -> bool:
    """Speicherreserve-Policy für eine extern gesteuerte Wallbox."""
    policy = _observe_wallbox_storage_policy_value(
        cfg,
        wb_intent,
        wb_mode,
        allow_inactive=allow_inactive,
    )
    return policy in ("reserve", "pv_battery", "battery", "akku", "wbminsoc", "floor", "1", "true", "yes", "on")


def _observe_wallbox_curve_floor_guard_requested(
    cfg: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_mode: int,
    *,
    allow_inactive: bool = False,
) -> bool:
    """Erhält die harte wbminSoC-Untergrenze, während der Speicher sonst seiner Kurve folgt."""
    return _observe_wallbox_storage_policy_value(
        cfg,
        wb_intent,
        wb_mode,
        allow_inactive=allow_inactive,
    ) == "curve"


def observe_wallbox_reserve_release_context(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_mode: int,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Diagnose for observe-only PV+Akku: car may use battery above house reserve."""
    previous_state = previous_state or {}
    requested = _observe_wallbox_storage_policy_requested(cfg, wb_intent, wb_mode)
    if not requested:
        return {"requested": False, "active": False}
    floor_soc = safe_float(
        wb_intent.get("effective_wb_floor_soc", wb_intent.get("wbminsoc", cfg.get("wbminsoc"))),
        0.0,
    )
    if floor_soc <= 0.0:
        return {"requested": True, "active": False, "floor_soc": floor_soc}
    soc = safe_float(live.get("SOC"), 0.0)
    hysteresis_pct = max(
        1.5,
        safe_float(
            cfg.get(
                "storage_wbminsoc_hold_hysteresis_pct",
                wb_intent.get("wb_soc_hysterese_pct"),
            ),
            0.7,
        ),
    )
    previous_hold = str(previous_state.get("state") or "") in (
        "unmanaged_wallbox_wbminsoc_hold",
        "wallbox_wbminsoc_curve_charge",
    )
    release_floor = floor_soc + (hysteresis_pct if previous_hold else 0.0)
    active = bool(soc > release_floor + 0.05)
    return {
        "requested": True,
        "active": active,
        "soc": round(soc, 2),
        "floor_soc": round(floor_soc, 2),
        "release_floor_soc": round(release_floor, 2),
        "hysteresis_pct": round(hysteresis_pct, 2),
        "previous_hold": previous_hold,
    }


def unmanaged_wallbox_wbminsoc_hold_decision(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_native: Optional[Dict[str, Any]],
    now_s: float,
    max_charge_w: int,
    max_discharge_w: int,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    previous_state = previous_state or {}
    previous_name = str(previous_state.get("state") or "")
    previous_hold = previous_name in (
        "unmanaged_wallbox_wbminsoc_hold",
        "wallbox_wbminsoc_curve_charge",
    )
    previous_ts = safe_float(previous_state.get("ts"), 0.0)
    previous_age_s = max(0.0, now_s - previous_ts) if previous_ts > 0.0 else 999999.0
    previous_since_ts = safe_float(previous_state.get("parallel_state_since_ts"), 0.0)
    if previous_since_ts <= 0.0:
        previous_since_ts = previous_ts
    previous_state_age_s = max(0.0, now_s - previous_since_ts) if previous_since_ts > 0.0 else previous_age_s
    owner_grace_s = max(30.0, safe_float(cfg.get("storage_wbminsoc_owner_grace_s"), 90.0))
    previous_hold_grace = bool(previous_hold and previous_age_s <= owner_grace_s)
    intent_fresh = bool(wb_intent) and now_s - safe_float(wb_intent.get("ts"), 0.0) <= 90.0
    wb_mode = normalize_wb_mode(wb_intent.get("wb_mode_active", cfg.get("wb1_mode", 0)))
    request = str(wb_intent.get("battery_request", "") or "").strip().lower()
    external_manager = wallbox_intent_external_manager(wb_intent)
    openwb_primary_pv_mode = bool(wb_intent.get("openwb_primary_pv_mode_active"))
    observe_storage_floor = _observe_wallbox_storage_policy_requested(
        cfg,
        wb_intent,
        wb_mode,
        allow_inactive=previous_hold_grace,
    )
    observe_curve_floor_guard = _observe_wallbox_curve_floor_guard_requested(
        cfg,
        wb_intent,
        wb_mode,
        allow_inactive=previous_hold_grace,
    )
    if (observe_storage_floor or observe_curve_floor_guard) and request in ("", "none"):
        request = "hold_discharge"
    controlled_waiting_wbmin = bool(
        intent_fresh
        and wb_mode != MODE_OFF
        and not external_manager
        and request == "hold_discharge"
        and not bool(wb_intent.get("wbminsoc_gate_open", True))
        and (
            wb_intent.get("active")
            or wb_intent.get("car_active")
            or wb_intent.get("connected")
            or wb_intent.get("plugged")
            or wb_intent.get("start_requested")
        )
    )
    if (not intent_fresh and not previous_hold_grace) or (
        wb_mode != MODE_OFF
        and not external_manager
        and request != "hold_discharge"
        and not controlled_waiting_wbmin
    ):
        return None

    floor_soc = safe_float(
        wb_intent.get("effective_wb_floor_soc", wb_intent.get("wbminsoc", cfg.get("wbminsoc"))),
        0.0,
    )
    if floor_soc <= 0.0:
        return None

    wb_power = wallbox_actual_power_snapshot(live, wb_native)
    wallbox_w = abs(safe_float(wb_power.get("power_w"), 0.0))
    intent_w = abs(safe_float(wb_intent.get("wb_power_w"), 0.0))
    native_charging = bool((wb_native or {}).get("charging_active"))
    details = (wb_native or {}).get("wb_details") or []
    native_seen = isinstance(wb_native, dict) and bool(wb_native)
    native_plugged = False
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        if detail.get("plug") or detail.get("plugged") or detail.get("connected"):
            native_plugged = True
        if detail.get("charging") or abs(safe_float(detail.get("power_w"), 0.0)) > 500.0:
            native_charging = True
            break
    native_total_w = abs(safe_float((wb_native or {}).get("total_power_w"), 0.0))
    observed_wallbox_w = max(wallbox_w, intent_w, native_total_w)
    no_vehicle_reason = str(
        wb_intent.get("reason")
        or ((wb_intent.get("decision") or {}).get("reason") if isinstance(wb_intent.get("decision"), dict) else "")
        or ""
    ).strip().lower()
    release_reason_without_charge = no_vehicle_reason in (
        "no_vehicle_connected",
        "manual_pause",
        "bev_full_blocked",
    )
    fresh_release_without_charge = bool(
        intent_fresh
        and request == "release"
        and observed_wallbox_w <= 250.0
        and intent_w <= 250.0
        and not native_charging
        and (
            release_reason_without_charge
            or (native_seen and native_total_w <= 250.0 and not native_plugged)
        )
    )
    if fresh_release_without_charge:
        return None
    wallbox_charging = bool(
        observed_wallbox_w > 500.0
        or intent_w > 500.0
        or wb_intent.get("charging_active")
        or native_charging
        or previous_hold_grace
    )
    if not wallbox_charging and not controlled_waiting_wbmin:
        return None

    soc = safe_float(live.get("SOC"), 0.0)
    hysteresis_pct = max(
        1.5,
        safe_float(
            cfg.get(
                "storage_wbminsoc_hold_hysteresis_pct",
                wb_intent.get("wb_soc_hysterese_pct"),
            ),
            0.7,
        ),
    )
    previous_pv_charge_active = bool(
        previous_name == "wallbox_wbminsoc_curve_charge"
        or (
            previous_name == "unmanaged_wallbox_wbminsoc_hold"
            and (
                bool(previous_state.get("wbminsoc_pv_charge_active"))
                or max(0, safe_int(previous_state.get("planned_grid_pv_charge_w"), 0)) > 0
                or (
                    safe_int(previous_state.get("mode"), MODE_AUTO) == MODE_CHRG
                    and max(0, safe_int(previous_state.get("storage_req_w"), 0)) > 0
                )
            )
        )
    )
    previous_curve_charge = previous_pv_charge_active
    external_wb_owner = bool(
        (external_manager and wb_mode != MODE_OFF)
        or observe_storage_floor
        or observe_curve_floor_guard
    )
    hold_active = bool(
        soc <= floor_soc
        or controlled_waiting_wbmin
        or (previous_hold and soc < floor_soc + hysteresis_pct)
    )
    if not hold_active:
        return None

    live_with_wallbox = dict(live)
    live_with_wallbox["Wallbox_Power"] = observed_wallbox_w
    live_with_wallbox["Wallbox_Power_Source"] = str(wb_power.get("source") or "live_data")
    live_with_wallbox["Wallbox_Live_Power"] = abs(safe_float(wb_power.get("live_w"), 0.0))
    live_with_wallbox["Wallbox_Home_Includes"] = bool(wb_power.get("home_includes_wallbox"))
    live_with_wallbox = live_with_wallbox_native_balance(live_with_wallbox, wb_native)

    house_wp_cap_w = house_heatpump_discharge_cap_w(
        live_with_wallbox,
        observed_wallbox_w,
        max_discharge_w,
    )
    house_wp_cap_w = smooth_house_heatpump_discharge_cap_w(cfg, house_wp_cap_w, previous_state)
    intent_reason = str(wb_intent.get("reason") or "").strip()
    scheduled_grid_charge = bool(
        request == "hold_discharge"
        and (
            wb_intent.get("scheduled_slot_active")
            or wb_intent.get("price_plan_storage_protect")
            or intent_reason in ("slot_grid_storage_protection", "price_plan_storage_protection")
        )
    )
    controlled_wallbox_no_grid = bool(not scheduled_grid_charge and not external_wb_owner)
    if controlled_wallbox_no_grid:
        return None
    curve_soc, _target_soc, _target_ts = current_curve(
        plan,
        now_s,
        allow_before_start=safe_float(live.get("PV_Power"), 0.0) > 250.0,
    )
    explicit_external_wallbox_mode = bool((external_manager and wb_mode != MODE_OFF) or observe_storage_floor)
    wallbox_floor_drives_storage_curve = bool(
        scheduled_grid_charge
        or (explicit_external_wallbox_mode and request == "hold_discharge")
    )
    if wallbox_floor_drives_storage_curve:
        if curve_soc is None:
            curve_soc = floor_soc
        else:
            curve_soc = max(float(curve_soc), floor_soc)
    pv_house_surplus_w = pv_surplus_after_house_heatpump_w(live_with_wallbox, observed_wallbox_w)
    grid_power_w = safe_float(live_with_wallbox.get("Grid_Power"), 0.0)
    grid_import_w = max(0, int(round(grid_power_w)))
    grid_export_w = max(0, int(round(-grid_power_w)))
    current_battery_charge_w = max(0.0, safe_float(live.get("Battery_Power"), 0.0))
    curve_gap_pct = max(0.0, float(curve_soc) - soc) if curve_soc is not None else 0.0
    min_charge_w = max(300, safe_int(cfg.get("storage_slot_grid_pv_charge_min_w"), 300))
    pv_charge_keep_w = max(
        100,
        safe_int(cfg.get("storage_wbminsoc_pv_charge_keep_w"), min_charge_w),
    )
    pv_charge_enter_w = max(
        min_charge_w,
        pv_charge_keep_w,
        safe_int(
            cfg.get("storage_wbminsoc_pv_charge_enter_w"),
            max(500, min_charge_w),
        ),
    )
    pv_charge_threshold_w = pv_charge_keep_w if previous_curve_charge else pv_charge_enter_w
    grid_tolerance_w = max(
        0,
        safe_int(cfg.get("storage_wbminsoc_pv_charge_grid_tolerance_w"), 150),
    )
    if openwb_primary_pv_mode:
        pv_charge_available_w = max(
            0.0,
            pv_house_surplus_w
            - observed_wallbox_w
            - max(0, grid_import_w - grid_tolerance_w),
        )
    elif controlled_wallbox_no_grid:
        pv_charge_available_w = max(
            0.0,
            current_battery_charge_w
            + grid_export_w
            - max(0, grid_import_w - grid_tolerance_w),
        )
    else:
        pv_charge_available_w = pv_house_surplus_w
    charge_enter_delay_s = max(
        0.0,
        safe_float(
            cfg.get("storage_wbminsoc_charge_enter_delay_s"),
            safe_float(cfg.get("storage_wbminsoc_state_min_s"), 30.0),
        ),
    )
    charge_fast_enter_w = max(
        pv_charge_enter_w,
        safe_int(
            cfg.get("storage_wbminsoc_pv_charge_fast_enter_w"),
            max(2500, pv_charge_enter_w + 1000),
        ),
    )
    charge_enter_dwell_active = bool(
        previous_name == "unmanaged_wallbox_wbminsoc_hold"
        and not previous_pv_charge_active
        and previous_state_age_s < charge_enter_delay_s
        and pv_charge_available_w < charge_fast_enter_w
    )
    effective_pv_charge_threshold_w = (
        max(pv_charge_threshold_w, charge_fast_enter_w)
        if charge_enter_dwell_active
        else pv_charge_threshold_w
    )
    curve_min_s = max(
        0.0,
        safe_float(
            cfg.get("storage_wbminsoc_curve_min_s"),
            safe_float(cfg.get("storage_wbminsoc_state_min_s"), 30.0),
        ),
    )
    curve_keep_min_w = max(
        0,
        safe_int(
            cfg.get("storage_wbminsoc_curve_keep_min_w"),
            min(pv_charge_keep_w, 200),
        ),
    )
    curve_keep_import_w = max(
        grid_tolerance_w,
        safe_int(cfg.get("storage_wbminsoc_curve_keep_import_w"), grid_tolerance_w + 300),
    )
    curve_leave_dwell_active = bool(
        previous_curve_charge
        and previous_state_age_s < curve_min_s
        and curve_gap_pct >= 0.2
        and pv_charge_available_w >= curve_keep_min_w
        and grid_import_w <= curve_keep_import_w
    )
    if (
        not openwb_primary_pv_mode
        and
        curve_gap_pct >= 0.2
        and (
            pv_charge_available_w >= effective_pv_charge_threshold_w
            or curve_leave_dwell_active
        )
    ):
        charge_w = max(0, min(max_charge_w, pv_charge_available_w))
        if curve_leave_dwell_active and charge_w < pv_charge_keep_w:
            charge_w = min(max_charge_w, pv_charge_keep_w)
        curve_txt = "%.1f%%" % float(curve_soc) if curve_soc is not None else "--"
        if scheduled_grid_charge:
            reason = (
                "Geplante Netzladung unter Kurve/wbminSoC %.1f%% (SoC %.1f%%, Kurve %s): "
                "PV-Überschuss nach Haus/WP %.0fW lädt Speicher, Wallbox nutzt Netz"
            ) % (floor_soc, soc, curve_txt, pv_house_surplus_w)
        elif observe_storage_floor:
            reason = (
                "Beobachten + PV + Akku bis Untergrenze %.1f%% (SoC %.1f%%, Kurve %s): "
                "freie PV nach Haus/WP %.0fW lädt den Speicher bis zur Hausakku-Reserve; "
                "die Wallbox bleibt beobachtet, Netz bleibt aus"
            ) % (floor_soc, soc, curve_txt, pv_house_surplus_w)
        elif observe_curve_floor_guard:
            reason = (
                "Beobachten + Ladekurve am wbminSoC-Hartboden %.1f%% (SoC %.1f%%, Kurve %s): "
                "freie PV nach Haus/WP %.0fW folgt weiter der Speicherkurve; "
                "die Wallbox bleibt ausschließlich beobachtet"
            ) % (floor_soc, soc, curve_txt, pv_house_surplus_w)
        elif explicit_external_wallbox_mode:
            reason = (
                "Bekannte Wallbox in PV + Akku bis Untergrenze %.1f%% (SoC %.1f%%, Kurve %s): "
                "freie PV nach Haus/WP %.0fW lädt den Speicher bis zur Hausakku-Reserve; "
                "das Auto lädt bis dahin normal, Netz bleibt aus"
            ) % (floor_soc, soc, curve_txt, pv_house_surplus_w)
        elif external_wb_owner:
            reason = (
                "Beobachtete Wallbox unter der Speicherkurve %.1f%% (SoC %.1f%%, Kurve %s): "
                "freie PV nach Haus/WP %.0fW lädt den Speicher; die Wallbox bleibt im Modus Beobachten"
            ) % (floor_soc, soc, curve_txt, pv_house_surplus_w)
        else:
            reason = (
                "Kontrollierte Wallbox unter Kurve/wbminSoC %.1f%% (SoC %.1f%%, Kurve %s): "
                "freie Ladeleistung %.0fW lädt Speicher, Wallbox erhält kein Speicherbudget am wbminSoC-Tor"
            ) % (floor_soc, soc, curve_txt, pv_charge_available_w)
        state_name = "unmanaged_wallbox_wbminsoc_hold" if scheduled_grid_charge else "wallbox_wbminsoc_curve_charge"
        result = {
            "state": state_name,
            "mode": MODE_CHRG,
            "val": charge_w,
            "priority": "safety",
            "reason": reason,
            "protected": True,
            "storage_req_w": charge_w,
            "budget_w": 0,
            "unmanaged_wallbox_wbminsoc_hold": True,
            "wbminsoc_pv_charge_active": True,
            "observe_storage_policy": "reserve" if observe_storage_floor else "curve",
            "observe_curve_floor_guard": bool(observe_curve_floor_guard),
            "wallbox_power_w": int(round(observed_wallbox_w)),
            "wallbox_power_source": str(wb_power.get("source") or "live_data"),
            "wallbox_home_includes": bool(wb_power.get("home_includes_wallbox")),
            "house_heatpump_discharge_cap_w": house_wp_cap_w,
            "planned_grid_pv_charge_w": charge_w,
            "planned_grid_pv_surplus_w": pv_charge_available_w,
            "pv_house_surplus_w": pv_house_surplus_w,
            "wbminsoc_grid_import_w": grid_import_w,
            "wbminsoc_grid_export_w": grid_export_w,
            "wbminsoc_pv_charge_grid_tolerance_w": grid_tolerance_w,
            "wbminsoc_pv_charge_enter_w": pv_charge_enter_w,
            "wbminsoc_pv_charge_keep_w": pv_charge_keep_w,
            "wbminsoc_effective_pv_charge_threshold_w": effective_pv_charge_threshold_w,
            "wbminsoc_transition_dwell_active": curve_leave_dwell_active,
            "wbminsoc_curve_dwell_active": curve_leave_dwell_active,
            "wbminsoc_previous_state_age_s": previous_state_age_s,
            "curve_soc": curve_soc,
            "scheduled_grid_charge": scheduled_grid_charge,
            "controlled_wallbox_wbminsoc_pause": controlled_wallbox_no_grid,
            "wbminsoc_hold_hysteresis_pct": hysteresis_pct,
            "wbminsoc_owner_grace_active": previous_hold_grace,
        }
        return result
    floor_relation = (
        "unter wbminSoC %.1f%% (SoC %.1f%%)"
        if soc < floor_soc - 0.05
        else "mit wbminSoC-Ziel %.1f%% erreicht (SoC %.1f%%)"
    )
    floor_txt = floor_relation % (floor_soc, soc)
    if scheduled_grid_charge:
        reason = (
            "Geplante Netzladung %s: "
            "Speicherentladung für die Wallbox gesperrt; Wallbox nutzt Netz, Speicher stützt nur Haus/WP-Defizit bis %dW"
        ) % (floor_txt, house_wp_cap_w)
    elif observe_storage_floor:
        reason = (
            "Beobachten + PV + Akku bis Untergrenze %s: "
            "Wallbox bleibt beobachtet; oberhalb der Reserve darf das Auto den Akku nutzen. "
            "Unterhalb der Reserve stützt der Speicher nur Haus/WP-Defizit bis %dW"
        ) % (floor_txt, house_wp_cap_w)
    elif observe_curve_floor_guard:
        reason = (
            "Beobachten + Ladekurve am wbminSoC-Hartboden %s: "
            "die Wallbox bleibt fremdgesteuert; der Speicher folgt oberhalb der Untergrenze seiner Kurve. "
            "Am Hartboden stützt er nur Haus/WP-Defizit bis %dW"
        ) % (floor_txt, house_wp_cap_w)
    elif external_manager and wb_mode != MODE_OFF:
        reason = (
            "Bekannte Wallbox im Modus PV + Akku bis Untergrenze %s: "
            "Auto darf oberhalb der Reserve normal laden; Netz bleibt aus. "
            "Unterhalb der Reserve stützt der Speicher nur Haus/WP-Defizit bis %dW"
        ) % (floor_txt, house_wp_cap_w)
    elif controlled_wallbox_no_grid:
        reason = (
            "Kontrollierte Wallbox %s: "
            "Wallbox erhält kein Speicherbudget; Speicher stützt nur Haus/WP-Defizit bis %dW"
        ) % (floor_txt, house_wp_cap_w)
    else:
        reason = (
            "Beobachtete Wallbox %s: "
            "E3DC-Control sendet keine Ladebefehle; Speicherentladung für die Wallbox ist gesperrt. "
            "Der Speicher stützt nur Haus/WP-Defizit bis %dW"
        ) % (floor_txt, house_wp_cap_w)
    if controlled_wallbox_no_grid:
        auto_limit = discharge_block_auto_limit(cfg, max_charge_w, reason)
        auto_limit["max_discharge_w"] = house_wp_cap_w
    else:
        auto_limit = discharge_block_auto_limit(cfg, max_charge_w, reason)
        auto_limit["max_discharge_w"] = house_wp_cap_w
    return {
        "state": "unmanaged_wallbox_wbminsoc_hold",
        "mode": MODE_AUTO,
        "val": max_charge_w,
        "priority": "safety",
        "reason": reason,
        "protected": True,
        "storage_req_w": 0,
        "budget_w": 0,
        "unmanaged_wallbox_wbminsoc_hold": True,
        "wbminsoc_target_active": external_wb_owner,
        "observe_storage_policy": "reserve" if observe_storage_floor else "curve",
        "observe_curve_floor_guard": bool(observe_curve_floor_guard),
        "wallbox_power_w": int(round(observed_wallbox_w)),
        "wallbox_power_source": str(wb_power.get("source") or "live_data"),
        "wallbox_home_includes": bool(wb_power.get("home_includes_wallbox")),
        "house_heatpump_discharge_cap_w": house_wp_cap_w,
        "auto_limit": auto_limit,
        "scheduled_grid_charge": scheduled_grid_charge,
        "controlled_wallbox_wbminsoc_pause": controlled_wallbox_no_grid,
        "wbminsoc_owner_grace_active": previous_hold_grace,
        "wbminsoc_pv_charge_active": False,
        "wbminsoc_transition_dwell_active": charge_enter_dwell_active,
        "wbminsoc_curve_dwell_active": False,
        "wbminsoc_previous_state_age_s": previous_state_age_s,
        "wbminsoc_effective_pv_charge_threshold_w": effective_pv_charge_threshold_w,
    }


def openwb_primary_pv_curve_charge_decision(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_native: Optional[Dict[str, Any]],
    now_s: float,
    max_charge_w: int,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    intent_ts = safe_float(wb_intent.get("ts"), 0.0)
    intent_fresh = bool(wb_intent) and intent_ts > 0.0 and now_s - intent_ts <= 90.0
    if not (
        intent_fresh
        and bool(wb_intent.get("openwb_primary_pv_mode_active"))
        and normalize_wb_mode(wb_intent.get("wb_mode_active", cfg.get("wb1_mode", 0))) == MODE_CURVE
    ):
        return None
    if safe_int(wb_intent.get("wb_control_mode"), 0) == CONTROL_TARGET:
        return None
    if (
        wb_intent.get("scheduled_slot_active")
        or wb_intent.get("price_boost_active")
        or wb_intent.get("price_plan_storage_protect")
        or wb_intent.get("battery_departure_active")
    ):
        return None

    wb_power = wallbox_actual_power_snapshot(live, wb_native)
    wallbox_w = max(
        abs(safe_float(wb_power.get("power_w"), 0.0)),
        abs(safe_float(wb_intent.get("wb_power_w"), 0.0)),
    )
    wb_native = wb_native or {}
    native_charging = bool(wb_native.get("charging_active"))
    details = wb_native.get("wb_details") or []
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        if detail.get("charging") or abs(safe_float(detail.get("power_w"), 0.0)) > 250.0:
            native_charging = True
            break
    cap_amp = safe_float(wb_intent.get("cap_amp", wb_intent.get("set_amp")), 0.0)
    wallbox_present = bool(
        wb_intent.get("active")
        or wb_intent.get("car_active")
        or wb_intent.get("connected")
        or wb_intent.get("plugged")
        or wb_intent.get("charging_active")
        or wallbox_w > 250.0
        or native_charging
        or cap_amp > 0.0
    )
    if not wallbox_present:
        return None

    soc = safe_float(live.get("SOC"), 0.0)
    curve_soc, _target_soc, _target_ts = current_curve(
        plan,
        now_s,
        allow_before_start=safe_float(live.get("PV_Power"), 0.0) > 250.0,
    )
    floor_soc = safe_float(
        wb_intent.get("effective_wb_floor_soc", wb_intent.get("wbminsoc", cfg.get("wbminsoc"))),
        0.0,
    )
    if floor_soc > 0.0 and soc < floor_soc:
        curve_soc = max(float(curve_soc), floor_soc) if curve_soc is not None else floor_soc
    if curve_soc is None:
        return None

    previous_state = previous_state or {}
    previous_name = str(previous_state.get("state") or "")
    previous_charge = previous_name == "wallbox_wbminsoc_curve_charge"
    curve_gap_pct = max(0.0, float(curve_soc) - soc)
    enter_gap_pct = max(0.1, safe_float(cfg.get("storage_openwb_primary_pv_charge_enter_gap_pct"), 0.25))
    keep_gap_pct = max(0.05, safe_float(cfg.get("storage_openwb_primary_pv_charge_keep_gap_pct"), 0.12))
    if curve_gap_pct < (keep_gap_pct if previous_charge else enter_gap_pct):
        return None

    live_with_wallbox = dict(live)
    live_with_wallbox["Wallbox_Power"] = wallbox_w
    live_with_wallbox["Wallbox_Power_Source"] = str(wb_power.get("source") or "live_data")
    live_with_wallbox["Wallbox_Live_Power"] = abs(safe_float(wb_power.get("live_w"), 0.0))
    live_with_wallbox["Wallbox_Home_Includes"] = bool(wb_power.get("home_includes_wallbox"))
    live_with_wallbox = live_with_wallbox_native_balance(live_with_wallbox, wb_native)

    pv_house_surplus_w = pv_surplus_after_house_heatpump_w(live_with_wallbox, wallbox_w)
    if pv_house_surplus_w <= 0:
        return None
    grid_tolerance_w = max(
        0,
        safe_int(cfg.get("storage_openwb_primary_pv_charge_grid_tolerance_w", cfg.get("storage_wbminsoc_pv_charge_grid_tolerance_w")), 150),
    )
    min_charge_w = max(
        300,
        safe_int(cfg.get("storage_openwb_primary_pv_charge_min_w", cfg.get("storage_wbminsoc_pv_charge_enter_w")), 500),
    )
    step_up_w = max(100, safe_int(cfg.get("storage_openwb_primary_pv_charge_step_up_w"), 300))
    step_down_w = max(step_up_w, safe_int(cfg.get("storage_openwb_primary_pv_charge_step_down_w"), 1000))
    target_charge_w = max(0, min(max_charge_w, pv_house_surplus_w))
    if target_charge_w < min_charge_w and not previous_charge:
        return None

    grid_power_w = safe_float(live_with_wallbox.get("Grid_Power"), 0.0)
    grid_import_w = max(0, int(round(grid_power_w)))
    grid_export_w = max(0, int(round(-grid_power_w)))
    current_battery_charge_w = max(0, safe_int(live_with_wallbox.get("Battery_Power"), 0))
    previous_val_w = max(0, safe_int(previous_state.get("val"), 0)) if previous_charge else 0
    base_charge_w = max(current_battery_charge_w, previous_val_w)
    if grid_import_w > grid_tolerance_w:
        charge_w = max(0, min(base_charge_w, current_battery_charge_w) - max(step_down_w, grid_import_w - grid_tolerance_w))
    else:
        charge_w = min(target_charge_w, base_charge_w + step_up_w + grid_export_w)
        if not previous_charge and target_charge_w >= min_charge_w:
            charge_w = max(charge_w, min_charge_w)
    if charge_w < min_charge_w:
        return None

    curve_txt = "%.1f%%" % float(curve_soc)
    reason = (
        "openWB Primary PV: Speicher führt die Ladekurve aktiv mit CHRG %dW "
        "(SoC %.1f%%, Kurve %s, PV nach Haus/WP %dW). "
        "openWB bleibt im PV-Modus und erhält nur den verbleibenden Überschuss."
    ) % (charge_w, soc, curve_txt, pv_house_surplus_w)
    return {
        "state": "wallbox_wbminsoc_curve_charge",
        "mode": MODE_CHRG,
        "val": charge_w,
        "priority": "safety",
        "reason": reason,
        "protected": True,
        "storage_req_w": charge_w,
        "budget_w": 0,
        "openwb_primary_pv_curve_charge": True,
        "unmanaged_wallbox_wbminsoc_hold": True,
        "house_heatpump_discharge_cap_w": 0,
        "planned_grid_pv_charge_w": charge_w,
        "planned_grid_pv_surplus_w": target_charge_w,
        "pv_house_surplus_w": pv_house_surplus_w,
        "wbminsoc_grid_import_w": grid_import_w,
        "wbminsoc_grid_export_w": grid_export_w,
        "wbminsoc_pv_charge_grid_tolerance_w": grid_tolerance_w,
        "wbminsoc_pv_charge_enter_w": min_charge_w,
        "wbminsoc_pv_charge_keep_w": min_charge_w,
        "wbminsoc_effective_pv_charge_threshold_w": min_charge_w,
        "curve_soc": curve_soc,
        "wbminsoc_hold_hysteresis_pct": 0.0,
        "wbminsoc_owner_grace_active": previous_charge,
    }


def openwb_primary_pv_discharge_cap_decision(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_native: Optional[Dict[str, Any]],
    now_s: float,
    max_charge_w: int,
    max_discharge_w: int,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    intent_ts = safe_float(wb_intent.get("ts"), 0.0)
    intent_fresh = bool(wb_intent) and intent_ts > 0.0 and now_s - intent_ts <= 90.0
    if not (
        intent_fresh
        and bool(wb_intent.get("openwb_primary_pv_mode_active"))
    ):
        return None

    wb_mode = normalize_wb_mode(wb_intent.get("wb_mode_active", cfg.get("wb1_mode", 0)))
    if wb_mode == MODE_OFF:
        return None
    if (
        wb_intent.get("scheduled_slot_active")
        or wb_intent.get("price_boost_active")
        or wb_intent.get("price_plan_storage_protect")
        or wb_intent.get("battery_departure_active")
    ):
        return None

    control_mode = safe_int(wb_intent.get("wb_control_mode"), 0)
    gate_open = bool(wb_intent.get("wbminsoc_gate_open", True))
    if wb_mode == MODE_TARGET and control_mode == CONTROL_TARGET and gate_open:
        return None

    wb_power = wallbox_actual_power_snapshot(live, wb_native)
    wallbox_w = max(
        abs(safe_float(wb_power.get("power_w"), 0.0)),
        abs(safe_float(wb_intent.get("wb_power_w"), 0.0)),
    )
    wb_native = wb_native or {}
    native_charging = bool(wb_native.get("charging_active"))
    native_present = bool(wb_native.get("connected"))
    details = wb_native.get("wb_details") or []
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        if detail.get("plug") or detail.get("plugged") or detail.get("connected"):
            native_present = True
        if detail.get("charging") or abs(safe_float(detail.get("power_w"), 0.0)) > 250.0:
            native_charging = True
            native_present = True

    cap_amp = safe_float(wb_intent.get("cap_amp", wb_intent.get("set_amp")), 0.0)
    wallbox_present = bool(
        wb_intent.get("active")
        or wb_intent.get("car_active")
        or wb_intent.get("connected")
        or wb_intent.get("plugged")
        or wb_intent.get("charging_active")
        or wb_intent.get("start_requested")
        or cap_amp > 0.0
        or wallbox_w > 250.0
        or native_present
        or native_charging
    )
    if not wallbox_present:
        return None

    live_with_wallbox = dict(live)
    live_with_wallbox["Wallbox_Power"] = wallbox_w
    live_with_wallbox["Wallbox_Power_Source"] = str(wb_power.get("source") or "live_data")
    live_with_wallbox["Wallbox_Live_Power"] = abs(safe_float(wb_power.get("live_w"), 0.0))
    live_with_wallbox["Wallbox_Home_Includes"] = bool(wb_power.get("home_includes_wallbox"))
    live_with_wallbox = live_with_wallbox_native_balance(live_with_wallbox, wb_native)
    house_wp_cap_w = house_heatpump_discharge_cap_w(live_with_wallbox, wallbox_w, max_discharge_w)
    house_wp_cap_w = smooth_house_heatpump_discharge_cap_w(cfg, house_wp_cap_w, previous_state)

    if wb_mode == MODE_TARGET and control_mode == CONTROL_TARGET:
        reason = (
            "openWB Primary PV+Akku: wbminSoC-Tor geschlossen; "
            "openWB regelt PV-Laden, E3DC-AUTO stützt nur Haus/WP-Defizit bis %dW"
        ) % house_wp_cap_w
    else:
        reason = (
            "openWB Primary PV: openWB regelt PV-Laden selbst; "
            "E3DC-AUTO bleibt aktiv, Speicherentladung ist auf Haus/WP-Defizit bis %dW begrenzt"
        ) % house_wp_cap_w

    return {
        "state": "parallel_wb_auto",
        "mode": MODE_AUTO,
        "val": max_charge_w,
        "priority": "wallbox",
        "reason": reason,
        "protected": True,
        "storage_req_w": 0,
        "budget_w": 0,
        "openwb_primary_pv_discharge_cap": True,
        "house_heatpump_discharge_cap_w": house_wp_cap_w,
        "wallbox_power_w": wallbox_w,
        "wallbox_power_source": str(wb_power.get("source") or "live_data"),
        "auto_limit": discharge_cap_auto_limit(cfg, max_charge_w, house_wp_cap_w, reason),
    }









































def curve_control_soc_estimate(
    raw_soc: float,
    bat_w: int,
    storage_kwh: float,
    previous_state: Dict[str, Any],
    now_s: float,
    cfg: Dict[str, Any],
) -> float:
    if safe_int(cfg.get("storage_curve_control_soc_enable"), 1) == 0:
        return raw_soc
    capacity_wh = max(1000.0, safe_float(storage_kwh, 0.0) * 1000.0)
    prev_ts = safe_float(
        previous_state.get("curve_control_soc_ts"),
        safe_float(previous_state.get("ts"), 0.0),
    )
    if "curve_control_soc" in previous_state:
        prev_control = safe_float(previous_state.get("curve_control_soc"), raw_soc)
    else:
        prev_control = raw_soc
        prev_target_soc = safe_float(previous_state.get("target_soc", previous_state.get("tl_soc_target")), -1.0)
        prev_target_ts = safe_float(previous_state.get("target_ts", previous_state.get("tl_ts_target")), 0.0)
        prev_ifc_w = max(0, safe_int(previous_state.get("iFc_w"), 0))
        if prev_target_soc >= 0.0 and prev_target_ts > prev_ts and prev_ifc_w > 0:
            prev_hours = max(0.05, (prev_target_ts - prev_ts) / 3600.0)
            inferred = prev_target_soc - (float(prev_ifc_w) * prev_hours / capacity_wh * 100.0)
            band_pct = max(0.2, min(1.2, safe_float(cfg.get("storage_curve_control_soc_band_pct"), 0.95)))
            if raw_soc - band_pct <= inferred <= raw_soc + band_pct:
                prev_control = inferred
    if prev_ts <= 0.0 or now_s <= prev_ts:
        return raw_soc
    prev_raw = safe_float(previous_state.get("curve_control_raw_soc"), previous_state.get("soc", raw_soc))
    hard_reset_pct = max(1.2, min(5.0, safe_float(cfg.get("storage_curve_control_soc_reset_pct"), 2.0)))
    if abs(raw_soc - prev_raw) >= hard_reset_pct:
        return raw_soc
    elapsed_s = max(0.0, min(90.0, now_s - prev_ts))
    prev_bat_w = safe_int(previous_state.get("bat_w"), bat_w)
    avg_bat_w = (float(prev_bat_w) + float(bat_w)) / 2.0
    delta_pct = (avg_bat_w * elapsed_s / 3600.0) / capacity_wh * 100.0
    estimate = prev_control + delta_pct
    band_pct = max(0.2, min(1.2, safe_float(cfg.get("storage_curve_control_soc_band_pct"), 0.95)))
    return max(0.0, min(100.0, max(raw_soc - band_pct, min(raw_soc + band_pct, estimate))))


def curve_release_ts_s(plan: Dict[str, Any]) -> float:
    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    raw = safe_float(plan.get("ladeende_ts"), 0.0) or safe_float(meta.get("curve_end_ts"), 0.0)
    if raw <= 0:
        return 0.0
    return raw / 1000.0 if raw > 10000000000.0 else raw


def curve_release_active(plan: Dict[str, Any], now_s: Optional[float] = None) -> bool:
    release_ts = curve_release_ts_s(plan)
    if release_ts <= 0:
        return False
    now = time.time() if now_s is None else float(now_s)
    return now >= release_ts


def curve_release_opening(
    plan: Dict[str, Any],
    now_s: float,
    cfg: Dict[str, Any],
    max_charge_w: int,
    base_limit_w: int = 0,
) -> Dict[str, Any]:
    release_ts = curve_release_ts_s(plan)
    max_charge_w = max(0, int(max_charge_w or 0))
    if release_ts <= 0 or max_charge_w <= 0:
        return {"active": False}

    window_s = max(
        0.0,
        min(7200.0, safe_float(cfg.get("storage_curve_release_opening_s"), 1800.0)),
    )
    seconds_to_release = release_ts - float(now_s)
    if window_s <= 0.0 or seconds_to_release <= 0.0 or seconds_to_release > window_s:
        return {
            "active": False,
            "seconds_to_release": max(0, int(round(seconds_to_release))),
            "window_s": int(round(window_s)),
            "release_ts_s": release_ts,
        }

    raw_progress = max(0.0, min(1.0, 1.0 - (seconds_to_release / window_s)))
    smooth_progress = raw_progress * raw_progress * (3.0 - 2.0 * raw_progress)
    base_limit_w = max(0, min(max_charge_w, int(base_limit_w or 0)))
    opened_limit_w = int(round(base_limit_w + (max_charge_w - base_limit_w) * smooth_progress))
    if 0 < opened_limit_w < 300:
        opened_limit_w = 0
    return {
        "active": True,
        "seconds_to_release": max(0, int(round(seconds_to_release))),
        "window_s": int(round(window_s)),
        "release_ts_s": release_ts,
        "progress": round(smooth_progress, 4),
        "raw_progress": round(raw_progress, 4),
        "base_limit_w": base_limit_w,
        "max_charge_w": max_charge_w,
        "opened_limit_w": max(0, min(max_charge_w, opened_limit_w)),
    }


def next_curve_evening_pv_release_context(
    cfg: Dict[str, Any],
    shadow_inputs: Dict[str, Any],
    *,
    now_s: float,
    soc: float,
    pv_w: int,
    grid_w: int,
    grid_ema_w: int,
    pv_after_fixed_w: int,
    offer_w: int,
    offer_threshold_w: int,
    max_charge_w: int,
) -> Dict[str, Any]:
    first_curve_ts_s = _plan_ts_s(shadow_inputs.get("first_curve_ts"))
    if first_curve_ts_s <= 0.0 or max_charge_w <= 0:
        return {"active": False}

    seconds_to_first = first_curve_ts_s - float(now_s)
    max_lead_s = max(
        3600.0,
        safe_float(cfg.get("storage_parallel_pre_curve_hold_max_lead_s"), 8 * 3600.0),
    )
    release_soc_ceiling = max(
        95.0,
        min(99.8, safe_float(cfg.get("storage_curve_evening_pv_release_soc_ceiling_pct"), 99.6)),
    )
    real_offer_w = max(
        0,
        safe_int(offer_w, 0),
        safe_int(pv_after_fixed_w, 0),
        max(0, -safe_int(grid_w, 0)),
        max(0, -safe_int(grid_ema_w, 0)),
    )
    threshold_w = max(300, safe_int(offer_threshold_w, 300))
    active = bool(
        seconds_to_first > max_lead_s
        and pv_w > 250
        and real_offer_w >= threshold_w
        and soc < release_soc_ceiling
    )
    return {
        "active": active,
        "first_curve_ts_s": first_curve_ts_s,
        "seconds_to_first_curve": max(0, int(round(seconds_to_first))),
        "max_lead_s": int(round(max_lead_s)),
        "offer_w": real_offer_w,
        "offer_threshold_w": threshold_w,
        "soc_ceiling_pct": round(release_soc_ceiling, 2),
    }


def heat_source_policy(cfg: Dict[str, Any], live: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return whether Quell-Erholung is allowed for the configured heat source."""
    live = live or {}
    raw = str(
        cfg.get(
            "wp_source_type",
            live.get("wp_source_type", live.get("heat_source_type", "auto")),
        )
        or "auto"
    ).strip().lower()
    normalized = (
        raw.replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
        .replace("-", "_")
        .replace("/", "_")
        .replace(" ", "_")
    )

    source_types = {
        "sole": ("sole", "Sole/Erdreich", True),
        "brine": ("sole", "Sole/Erdreich", True),
        "ground": ("sole", "Sole/Erdreich", True),
        "earth": ("sole", "Sole/Erdreich", True),
        "erdreich": ("sole", "Sole/Erdreich", True),
        "erdsonde": ("sole", "Sole/Erdreich", True),
        "kollektor": ("sole", "Sole/Erdreich", True),
        "geothermal": ("sole", "Sole/Erdreich", True),
        "water": ("water", "Grundwasser", True),
        "grundwasser": ("water", "Grundwasser", True),
        "groundwater": ("water", "Grundwasser", True),
        "direct": ("direct", "Direktverdampfung", True),
        "direct_evaporation": ("direct", "Direktverdampfung", True),
        "direktverdampfung": ("direct", "Direktverdampfung", True),
        "air": ("air", "Luft", False),
        "luft": ("air", "Luft", False),
    }
    canonical, label, supported = source_types.get(
        normalized,
        ("auto", "Unbekannt", False),
    )
    if supported:
        reason = "Wärmequelle %s erlaubt Quell-Erholung" % label
    elif canonical == "air":
        reason = "Luft-Wärmepumpe: Quell-Erholung bleibt gesperrt"
    else:
        reason = "Wärmequelle unbekannt; Quell-Erholung braucht Sole/Erdreich, Grundwasser oder Direktverdampfung"
    return {
        "heat_source_type": canonical,
        "heat_source_label": label,
        "source_recovery_supported": bool(supported),
        "source_recovery_source_raw": raw,
        "source_recovery_source_reason": reason,
    }


def source_recovery_request(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    now_s: float,
) -> Dict[str, Any]:
    """Build a Storage-Manager-owned heatpump pause request.

    The Energy Manager remains the actuator. This function only publishes a
    short-lived intent with owner, reason and comfort limits.
    """
    enabled = cfg_bool(cfg, "source_recovery_enable", cfg_bool(cfg, "pv_pause_enable", False))
    if not enabled:
        return {"active": False, "owner": "none", "reason": "Quell-Erholung deaktiviert"}

    source_info = heat_source_policy(cfg, live)
    if not source_info.get("source_recovery_supported"):
        return {
            "active": False,
            "owner": "source_recovery_heatpump",
            "reason": source_info.get("source_recovery_source_reason", "Wärmequelle nicht für Quell-Erholung freigegeben"),
            **source_info,
        }

    soc = safe_float(live.get("SOC"), 0.0)
    min_soc = max(0.0, min(100.0, safe_float(cfg.get("source_recovery_min_soc", cfg.get("pv_pause_soc")), 80.0)))
    if soc < min_soc:
        return {
            "active": False,
            "owner": "source_recovery_heatpump",
            "reason": "SoC %.1f%% unter Quell-Erholung-Schwelle %.1f%%" % (soc, min_soc),
            **source_info,
        }

    min_at = safe_float(cfg.get("source_recovery_min_at", cfg.get("pv_pause_min_at")), 0.0)
    outside_candidates = (
        "Outdoor_Temp",
        "Outside_Temp",
        "Aussentemperatur",
        "Außentemperatur",
        "wp_outdoor_temp",
    )
    outside_temp: Optional[float] = None
    for key in outside_candidates:
        if key in live and live.get(key) not in (None, ""):
            outside_temp = safe_float(live.get(key), 0.0)
            break
    if outside_temp is not None and outside_temp < min_at:
        return {
            "active": False,
            "owner": "source_recovery_heatpump",
            "reason": "Außentemperatur %.1f°C unter Quell-Erholung-Grenze %.1f°C" % (outside_temp, min_at),
            **source_info,
        }

    now_ms = float(now_s) * 1000.0
    lookahead_h = max(0.25, min(4.0, safe_float(cfg.get("source_recovery_lookahead_h"), 1.5)))
    future_until_ms = now_ms + lookahead_h * 3600.0 * 1000.0
    current_pv_w = max(0, safe_int(live.get("PV_Power"), 0))
    max_future_w = 0
    max_future_ts_ms = 0.0
    current_forecast_pv_w = 0
    timeline = plan.get("timeline") or []
    if isinstance(timeline, list):
        for item in timeline:
            if not isinstance(item, dict):
                continue
            ts = safe_float(item.get("ts", item.get("start_timestamp", 0)), 0.0)
            end_ts = safe_float(item.get("end_timestamp", ts + 900000.0), ts + 900000.0)
            if ts <= now_ms < end_ts:
                current_forecast_pv_w = max(current_forecast_pv_w, safe_int(item.get("pv_w"), 0))
            if now_ms <= ts <= future_until_ms:
                candidate_w = max(0, safe_int(item.get("pv_w"), 0))
                if candidate_w > max_future_w:
                    max_future_w = candidate_w
                    max_future_ts_ms = ts

    min_future_w = max(0, safe_int(cfg.get("source_recovery_min_future_pv_w", cfg.get("pv_pause_watt")), 3000))
    peak_factor = max(1.0, min(2.0, safe_float(cfg.get("source_recovery_peak_factor"), 1.1)))

    # Hysterese: Nutze das Maximum aus Live-Leistung und prognostizierter Leistung.
    # Dadurch bleibt die Pause stabil inaktiv, wenn wir uns bereits im prognostizierten Peak-Fenster
    # befinden, selbst wenn eine Wolke die aktuelle Live-Leistung kurzzeitig einbrechen lässt.
    effective_pv_w = max(current_pv_w, current_forecast_pv_w)
    if max_future_w < min_future_w or max_future_w <= int(effective_pv_w * peak_factor):
        return {
            "active": False,
            "owner": "source_recovery_heatpump",
            "reason": "Keine belastbare PV-Kante für Quell-Erholung",
            "current_pv_w": current_pv_w,
            "current_forecast_pv_w": current_forecast_pv_w,
            "max_future_pv_w": max_future_w,
            "max_future_ts": int(max_future_ts_ms / 1000.0) if max_future_ts_ms > 0 else None,
            "min_future_pv_w": min_future_w,
            **source_info,
        }

    timeout_min = max(5.0, min(360.0, safe_float(cfg.get("source_recovery_timeout_minutes", cfg.get("pv_pause_timeout_minutes")), 120.0)))
    restart_block_min = max(0.0, min(720.0, safe_float(cfg.get("source_recovery_restart_block_min"), 180.0)))
    max_temp_drop = max(0.5, min(12.0, safe_float(cfg.get("source_recovery_max_temp_drop", cfg.get("pv_pause_max_temp_drop")), 4.0)))
    timeout_s = int(timeout_min * 60.0)
    pause_until_ts = (
        int(max_future_ts_ms / 1000.0)
        if max_future_ts_ms > now_ms
        else int(now_s + min(timeout_s, lookahead_h * 3600.0))
    )
    planned_pause_s = max(0, min(timeout_s, int(pause_until_ts - now_s)))
    return {
        "active": True,
        "owner": "source_recovery_heatpump",
        "label": "Quell-Erholung",
        "reason": "Quell-Erholung: PV-Kante %.0fW in %.1fh erwartet, SoC %.1f%%" % (
            max_future_w,
            lookahead_h,
            soc,
        ),
        "ts": int(now_s),
        "expires_ts": int(now_s + max(20.0, CYCLE_S * 4.0)),
        "min_runtime_s": int(max(0.0, safe_float(cfg.get("source_recovery_min_runtime_min"), 15.0)) * 60.0),
        "timeout_s": timeout_s,
        "pause_until_ts": pause_until_ts,
        "planned_pause_s": planned_pause_s,
        "forecast_edge_id": "%d:%d" % (pause_until_ts, max_future_w),
        "restart_block_s": int(restart_block_min * 60.0),
        "max_temp_drop_k": round(max_temp_drop, 1),
        "min_outdoor_temp_c": round(min_at, 1),
        "current_pv_w": current_pv_w,
        "current_forecast_pv_w": current_forecast_pv_w,
        "max_future_pv_w": max_future_w,
        "max_future_ts": pause_until_ts,
        "min_future_pv_w": min_future_w,
        "lookahead_h": round(lookahead_h, 2),
        **source_info,
    }


def wallbox_phase_transition_reservation_contract(
    wb_intent: Optional[Dict[str, Any]],
    *,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Prüft unabhängige Aufträge je Wallbox ohne slotübergreifende Sperren."""

    intent = wb_intent if isinstance(wb_intent, dict) else {}
    now_value = float(now_s if now_s is not None else time.time())
    intent_ts = safe_float(intent.get("ts"), 0.0)
    fresh = bool(intent) and (intent_ts <= 0.0 or (now_value - intent_ts) <= 60.0)
    raw_items = intent.get("phase_transition_reservations")
    items: List[Dict[str, Any]] = []
    if isinstance(raw_items, list):
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            lease_until = safe_float(item.get("lease_until_ts", item.get("expires_ts")), 0.0)
            requested_w = max(0, safe_int(item.get("requested_w", item.get("reserved_w")), 0))
            committed = str(item.get("grant_state") or "") == "committed" or safe_int(item.get("committed_w"), 0) > 0
            blockers: List[str] = []
            if not fresh:
                blockers.append("stale_intent")
            if not item.get("active"):
                blockers.append("not_requested")
            if requested_w <= 0:
                blockers.append("no_reservation")
            if lease_until > 0.0 and now_value >= lease_until:
                blockers.append("reservation_expired")
            # Slotlokale Pause-/Voll-Evidenz darf einen neuen Auftrag ablehnen.
            # Der globale Ladeende-Latch einer anderen Wallbox wird bewusst nicht
            # herangezogen; eine bereits gebundene Sequenz bleibt maßgeblich.
            if not committed and item.get("manual_pause"):
                blockers.append("manual_pause")
            if not committed and item.get("bev_full_blocked"):
                blockers.append("bev_full")
            if blockers:
                continue
            item.update({
                "reservation_id": str(item.get("reservation_id") or item.get("transition_id") or ""),
                "wb_id": max(0, safe_int(item.get("wb_id"), 0)),
                "requested_w": requested_w,
                "reserved_w": requested_w,
                "lease_until_ts": lease_until,
                "expires_ts": lease_until,
                "blockers": [],
            })
            items.append(item)

    if not isinstance(raw_items, list):
        expires_ts = safe_float(intent.get("phase_transition_until_ts"), 0.0)
        requested_w = max(0, safe_int(intent.get("phase_transition_reserved_w"), 0))
        requested = bool(intent.get("phase_transition_active"))
        expired = bool(expires_ts > 0.0 and now_value >= expires_ts)
        blockers = []
        if not requested:
            blockers.append("not_requested")
        if not fresh:
            blockers.append("stale_intent")
        if requested_w <= 0:
            blockers.append("no_reservation")
        if expired:
            blockers.append("reservation_expired")
        if bool(intent.get("manual_pause")):
            blockers.append("manual_pause")
        if bool(intent.get("bev_full_blocked")):
            blockers.append("bev_full")
        if "wb_mode_active" in intent and normalize_wb_mode(intent.get("wb_mode_active")) == MODE_OFF:
            blockers.append("wallbox_off")
        if not blockers:
            items.append({
                "schema_version": "wallbox_phase_transition_v1_compat",
                "reservation_id": str(intent.get("phase_transition_reservation_id") or "legacy-phase-transition"),
                "wb_id": safe_int((intent.get("phase_transition_charger_ids") or [0])[0], 0),
                "stage": "recovery_hold",
                "target_phases": safe_int(intent.get("phase_transition_target_phases"), 0),
                "requested_w": requested_w,
                "reserved_w": requested_w,
                "lease_until_ts": expires_ts,
                "expires_ts": expires_ts,
                "started_ts": safe_float(intent.get("phase_transition_started_ts"), 0.0),
                "source": str(intent.get("phase_transition_source") or "legacy"),
                "reason_code": "legacy_phase_transition",
                "active": True,
                "grant_state": "waiting",
            })

    reserved_w = sum(max(0, safe_int(item.get("requested_w"), 0)) for item in items)
    targets = sorted({safe_int(item.get("target_phases"), 0) for item in items if safe_int(item.get("target_phases"), 0) in (1, 3)})
    return {
        "contract": "wallbox_phase_transition_reservation_v2",
        "active": bool(items),
        "requested": bool(items),
        "reservations": items,
        "reserved_w": reserved_w,
        "requested_w": reserved_w,
        "fresh": fresh,
        "connected": bool(items),
        "live_connected": bool(intent.get("active") or intent.get("car_active") or intent.get("charging_active")),
        "connection_held_by_transition": bool(items),
        "expired": False,
        "intent_ts": intent_ts,
        "started_ts": min((safe_float(item.get("started_ts"), 0.0) for item in items), default=0.0),
        "expires_ts": max((safe_float(item.get("lease_until_ts"), 0.0) for item in items), default=0.0),
        "remaining_s": max((max(0.0, safe_float(item.get("lease_until_ts"), 0.0) - now_value) for item in items), default=0.0),
        "target_phases": targets[0] if len(targets) == 1 else 0,
        "targets": targets,
        "charger_ids": [safe_int(item.get("wb_id"), 0) for item in items],
        "source": "per_wallbox_request_grant_commit",
        "blockers": [] if items else ["no_valid_reservation"],
    }
    budget_kwh = safe_float(report.get("policy_export_budget_kwh"), 0.0)
    report["latest_export_tracking"] = {
        "budget_kwh": round(budget_kwh, 4),
        "real_export_kwh": round(safe_float(report.get("real_export_kwh"), 0.0), 4),
        "tracking_error_kwh": round(safe_float(report.get("export_tracking_error_kwh"), 0.0), 4),
        "fulfilment_pct": round(
            100.0 * safe_float(report.get("real_export_kwh"), 0.0) / budget_kwh,
            1,
        ) if budget_kwh > 0.0 else None,
        "missed_export_cycles": safe_int(report.get("missed_export_cycles"), 0),
    }


def wallbox_possible_power(cfg: Dict[str, Any], wb_intent: Dict[str, Any], wb_native: Dict[str, Any]) -> int:
    possible = safe_int(wb_native.get("wb_possible_power_w"), 0)
    details = wb_native.get("wb_details") or []
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        active = bool(
            detail.get("plug")
            or detail.get("charging")
            or safe_int(detail.get("amp"), 0) > 0
            or abs(safe_float(detail.get("power_w"), 0.0)) > 250
        )
        if not active:
            continue
        amp = safe_int(detail.get("max_amp", detail.get("amp")), 0)
        phases = safe_int(
            detail.get("phases_target", detail.get("phases_in_use", detail.get("phases_actual"))),
            0,
        )
        if phases not in (1, 2, 3):
            phases = 1
        if amp > 0:
            possible = max(possible, max(6, min(32, amp)) * 230 * phases)
    if possible <= 0:
        amp = max(
            safe_int(wb_native.get("wb_max_amp"), 0),
            safe_int(wb_native.get("wb_global_max_amp"), 0),
            safe_int(wb_intent.get("max_amp", wb_intent.get("set_amp", wb_intent.get("cap_amp"))), 0),
            safe_int(cfg.get("wbmaxladestrom", cfg.get("wb_max_amp")), 16),
        )
        phases = safe_int(wb_intent.get("detected_phases", wb_native.get("detected_phases")), 1)
        if phases not in (1, 2, 3):
            phases = 1
        possible = max(0, max(6, min(32, amp)) * 230 * phases)
    return int(possible)


def _wallbox_detail_power_w(detail: Dict[str, Any]) -> float:
    for key in ("power_w", "phase_power_sum_w", "meter_power_w", "charge_power_w"):
        power = abs(safe_float(detail.get(key), 0.0))
        if power > 0:
            return power
    return 0.0


def wallbox_actual_power_snapshot(
    live: Dict[str, Any],
    wb_native: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the best current wallbox power, including native multi-WB slots."""
    wb_native = wb_native or {}
    live_w = abs(safe_float(live.get("Wallbox_Power"), 0.0))
    native_details_w = 0.0
    details = wb_native.get("wb_details") or []
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            power = _wallbox_detail_power_w(detail)
            if bool(detail.get("charging")) or power > 250.0:
                native_details_w += power

    native_total_w = abs(safe_float(wb_native.get("total_power_w"), 0.0))
    has_native_charge_flag = "charging_active" in wb_native
    native_charging = bool(wb_native.get("charging_active", False))
    native_total_valid = native_total_w > 250.0 and (
        native_charging
        or native_details_w > 250.0
        or not has_native_charge_flag
    )
    native_w = native_details_w if native_details_w > 50.0 else (native_total_w if native_total_valid else 0.0)
    if native_w > max(250.0, live_w):
        return {
            "power_w": native_w,
            "source": "wallbox_native",
            "live_w": live_w,
            "native_w": native_w,
            "home_includes_wallbox": bool(live.get("Wallbox_Home_Includes", False)) or live_w <= 50.0,
        }
    return {
        "power_w": live_w,
        "source": "live_data",
        "live_w": live_w,
        "native_w": native_w,
        "home_includes_wallbox": bool(live.get("Wallbox_Home_Includes", False)),
    }


def wallbox_actual_power(live: Dict[str, Any], wb_native: Optional[Dict[str, Any]] = None) -> float:
    return safe_float(wallbox_actual_power_snapshot(live, wb_native).get("power_w"), 0.0)


def live_with_wallbox_native_balance(live: Dict[str, Any], wb_native: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(wb_native, dict):
        return dict(live)
    enriched = dict(live)
    if "grid_w_raw" in wb_native:
        enriched["Wallbox_Grid_Power_Raw"] = safe_float(wb_native.get("grid_w_raw"), 0.0)
    if "bat_w_raw" in wb_native:
        enriched["Wallbox_Battery_Power_Raw"] = safe_float(wb_native.get("bat_w_raw"), 0.0)
    return enriched


def _raw_total_load_from_balance_w(live: Dict[str, Any]) -> Optional[float]:
    has_grid = "Wallbox_Grid_Power_Raw" in live
    has_bat = "Wallbox_Battery_Power_Raw" in live
    if not has_grid and not has_bat:
        return None
    pv_w = max(0.0, safe_float(live.get("PV_Power"), 0.0))
    grid_w = safe_float(live.get("Wallbox_Grid_Power_Raw"), safe_float(live.get("Grid_Power"), 0.0))
    bat_w = safe_float(live.get("Wallbox_Battery_Power_Raw"), safe_float(live.get("Battery_Power"), 0.0))
    total_w = pv_w + max(grid_w, 0.0) + max(-bat_w, 0.0) - max(-grid_w, 0.0) - max(bat_w, 0.0)
    return max(0.0, total_w)


def house_power_excluding_wallbox_w(live: Dict[str, Any], wallbox_w: float) -> float:
    home_w = max(0.0, safe_float(live.get("Home_Power"), 0.0))
    if not bool(live.get("Wallbox_Home_Includes")):
        return home_w

    wallbox_w = max(0.0, safe_float(wallbox_w, 0.0))
    direct_home_w = max(0.0, home_w - wallbox_w)
    raw_total_w = _raw_total_load_from_balance_w(live)
    if raw_total_w is None:
        return direct_home_w

    raw_home_w = max(0.0, raw_total_w - wallbox_w)
    if direct_home_w > 250.0:
        return direct_home_w
    if raw_home_w <= 0.0:
        return direct_home_w
    return raw_home_w


def smooth_house_heatpump_discharge_cap_w(
    cfg: Dict[str, Any],
    current_cap_w: int,
    previous_state: Optional[Dict[str, Any]],
) -> int:
    previous_state = previous_state or {}
    previous_name = str(previous_state.get("state") or "")
    if previous_name not in (
        "unmanaged_wallbox_wbminsoc_hold",
        "wallbox_wbminsoc_curve_charge",
        "parallel_curve_auto_hold",
        "parallel_curve_auto_no_surplus",
        "parallel_curve_charge",
        "parallel_curve_charge_cap",
        "parallel_wb_auto",
        "wallbox_predump_floor_hold",
    ):
        return current_cap_w

    prev_cap = previous_state.get("house_heatpump_discharge_cap_w")
    if prev_cap is None and isinstance(previous_state.get("auto_limit"), dict):
        prev_cap = previous_state["auto_limit"].get("max_discharge_w")
    prev_cap_w = safe_int(prev_cap, -1)
    if prev_cap_w < 0:
        return current_cap_w

    hold_band_w = max(300, safe_int(cfg.get("storage_wbminsoc_house_cap_hold_band_w"), 800))
    step_w = max(300, safe_int(cfg.get("storage_wbminsoc_house_cap_step_w"), 1000))
    delta_w = current_cap_w - prev_cap_w
    if delta_w <= 0:
        return max(0, current_cap_w)
    if delta_w <= hold_band_w:
        return max(0, prev_cap_w)
    return max(0, min(current_cap_w, prev_cap_w + step_w))


def house_heatpump_discharge_cap_w(
    live: Dict[str, Any],
    wallbox_w: float,
    max_discharge_w: int,
) -> int:
    """Allow only the PV-uncovered house/heat-pump deficit, excluding wallbox load."""
    pv_w = max(0, safe_int(live.get("PV_Power"), 0))
    home_w = max(0, int(round(house_power_excluding_wallbox_w(live, wallbox_w))))
    wp_w = max(
        0,
        safe_int(
            live.get("WP_Power", live.get("heizstab_power", live.get("Heizstab_Power"))),
            0,
        ),
    )
    deficit_w = max(0, home_w + wp_w - pv_w)
    return max(0, min(max_discharge_w, deficit_w))


def pv_surplus_after_house_heatpump_w(live: Dict[str, Any], wallbox_w: float) -> int:
    """Return PV surplus after house and heat pump, excluding wallbox load."""
    pv_w = max(0.0, safe_float(live.get("PV_Power"), 0.0))
    home_w = house_power_excluding_wallbox_w(live, wallbox_w)
    wp_w = max(
        0.0,
        safe_float(
            live.get("WP_Power", live.get("Heatpump_Power", live.get("heizstab_power", 0.0))),
            0.0,
        ),
    )
    return max(0, int(round(pv_w - home_w - wp_w)))


def wallbox_curve_reserve_budget(
    cfg: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_native: Optional[Dict[str, Any]],
    previous_state: Dict[str, Any],
    wallbox_w: float,
    pv_after_fixed_w: int,
    i_fc_w: int,
    max_charge_w: int,
    shadow_inputs: Dict[str, Any],
) -> Dict[str, int]:
    """Reserve battery charge by reducing a controllable PV-curve wallbox gently."""
    i_fc_w = max(0, int(i_fc_w or 0))
    if i_fc_w <= 0 or wallbox_w <= 250.0:
        return {}

    curve_guard_active = bool(shadow_inputs.get("curve_guard_active"))
    curve_gap_pct = safe_float(shadow_inputs.get("curve_gap_pct"), 0.0)
    curve_charge_enter_w = max(
        0,
        safe_int(
            shadow_inputs.get("curve_charge_enter_w"),
            safe_int(cfg.get("storage_parallel_curve_charge_enter_w"), 300),
        ),
    )
    if not (curve_guard_active or curve_gap_pct > 0.0) or i_fc_w < curve_charge_enter_w:
        return {}

    minimum = predump_wallbox_minimum_power_w(cfg, wb_intent, wb_native or {})
    min_wallbox_w = max(0, safe_int(minimum.get("power_w"), 0))
    phases = max(1, safe_int(minimum.get("phases"), 1))
    if phases not in (1, 2, 3):
        phases = 3
    if min_wallbox_w <= 0:
        min_wallbox_w = 6 * 230 * phases

    reducible_w = max(0, int(round(wallbox_w + max(0, pv_after_fixed_w) - min_wallbox_w)))
    base_target_w = max(0, min(i_fc_w, max_charge_w, reducible_w))
    catchup_target_w = base_target_w
    export_catchup_active = False
    export_catchup_min_gap_pct = max(
        0.0,
        safe_float(cfg.get("storage_wb_curve_export_catchup_gap_pct"), 0.5),
    )
    export_catchup_min_w = max(
        curve_charge_enter_w,
        safe_int(cfg.get("storage_wb_curve_export_catchup_min_w"), curve_charge_enter_w),
    )
    export_catchup_max_w = min(
        max_charge_w,
        max(
            export_catchup_min_w,
            safe_int(cfg.get("storage_wb_curve_export_catchup_max_w"), max_charge_w),
        ),
    )
    if (
        curve_gap_pct >= export_catchup_min_gap_pct
        and reducible_w >= export_catchup_min_w
        and export_catchup_max_w > 0
    ):
        # Wenn die Kurve bereits unterschritten ist, darf ein laufender
        # Wallbox-AUTO-Pfad echte PV-/Exportreste nicht bei 6A liegen lassen.
        # Zuerst wird der Speicher weich bis zur verfuegbaren Restleistung
        # nachgefuehrt; nur der verbleibende Rest geht danach zur Wallbox.
        catchup_target_w = min(export_catchup_max_w, max(base_target_w, reducible_w))
        export_catchup_active = catchup_target_w > base_target_w
    target_w = catchup_target_w
    if target_w < curve_charge_enter_w:
        target_w = 0

    previous_budget = previous_state.get("budget") if isinstance(previous_state.get("budget"), dict) else {}
    previous_reserve_w = max(
        0,
        safe_int(
            previous_state.get(
                "wallbox_curve_reserve_w",
                previous_budget.get("wallbox_curve_reserve_w", 0),
            ),
            0,
        ),
    )
    step_w = max(
        230,
        safe_int(cfg.get("storage_wb_curve_reserve_step_w"), 230 * phases),
    )
    if target_w > previous_reserve_w:
        applied_w = min(target_w, previous_reserve_w + step_w)
    else:
        applied_w = max(target_w, previous_reserve_w - step_w * 2)
    if 0 < applied_w < curve_charge_enter_w:
        applied_w = min(target_w, curve_charge_enter_w)

    return {
        "reserve_w": max(0, int(applied_w)),
        "target_w": max(0, int(target_w)),
        "step_w": int(step_w),
        "min_wallbox_w": int(min_wallbox_w),
        "phases": int(phases),
        "export_catchup_active": bool(export_catchup_active),
        "export_catchup_w": int(catchup_target_w if export_catchup_active else 0),
        "raw_iaval_w": int(round(pv_after_fixed_w - max(0, int(applied_w)))),
    }


def curve_charge_base_frame_smoothing(
    cfg: Dict[str, Any],
    previous_state: Dict[str, Any],
    desired_w: int,
    max_charge_w: int,
    now_s: float,
    curve_gap_pct: float = 0.0,
    hard_anchor_need_w: int = 0,
    shortfall_active: bool = False,
    headroom_active: bool = False,
    curve_charge_enter_w: int = 300,
) -> Dict[str, Any]:
    desired_w = max(0, int(desired_w or 0))
    max_charge_w = max(0, int(max_charge_w or 0))
    if desired_w <= 0 or max_charge_w <= 0:
        return {"active": False}

    max_gap_pct = max(
        0.0,
        safe_float(
            cfg.get("storage_curve_frame_base_smoothing_max_gap_pct"),
            safe_float(cfg.get("storage_curve_frame_lift_gap_pct"), 0.75),
        ),
    )
    if hard_anchor_need_w > 0 or shortfall_active or headroom_active or curve_gap_pct >= max_gap_pct:
        return {"active": False}

    previous_state_name = str(previous_state.get("state") or previous_state.get("parallel_state") or "")
    if previous_state_name != "parallel_curve_charge":
        return {"active": False}

    previous_ts = safe_float(previous_state.get("ts"), 0.0)
    max_age_s = max(0.0, safe_float(cfg.get("storage_curve_frame_base_smoothing_max_age_s"), 180.0))
    if previous_ts > 0.0 and max_age_s > 0.0 and now_s - previous_ts > max_age_s:
        return {"active": False}

    previous_auto_limit = previous_state.get("auto_limit") if isinstance(previous_state.get("auto_limit"), dict) else {}
    previous_limit_w = max(
        0,
        safe_int(
            previous_state.get(
                "iFc_w",
                previous_state.get(
                    "storage_charge_request_w",
                    previous_state.get(
                        "parallel_val",
                        previous_state.get("val", previous_auto_limit.get("max_charge_w", 0)),
                    ),
                ),
            ),
            0,
        ),
    )
    if previous_limit_w <= 0:
        return {"active": False}

    previous_limit_w = min(max_charge_w, previous_limit_w)
    hold_band_w = max(50, safe_int(cfg.get("storage_curve_frame_base_hold_band_w"), 250))
    step_up_w = max(50, safe_int(cfg.get("storage_curve_frame_base_step_up_w"), 300))
    step_down_w = max(50, safe_int(cfg.get("storage_curve_frame_base_step_down_w"), 500))
    delta_w = desired_w - previous_limit_w
    if abs(delta_w) <= hold_band_w:
        target_w = previous_limit_w
        phase = "hold"
        step_w = 0
    elif delta_w > 0:
        target_w = min(desired_w, previous_limit_w + step_up_w)
        phase = "ramp_up"
        step_w = step_up_w
    else:
        target_w = max(desired_w, previous_limit_w - step_down_w)
        phase = "ramp_down"
        step_w = step_down_w

    curve_charge_enter_w = max(0, int(curve_charge_enter_w or 0))
    if desired_w >= curve_charge_enter_w > 0 and 0 < target_w < curve_charge_enter_w:
        target_w = min(desired_w, curve_charge_enter_w)
    target_w = max(0, min(max_charge_w, int(target_w)))
    if target_w == desired_w:
        return {"active": False, "phase": phase, "previous_w": previous_limit_w}
    return {
        "active": True,
        "frame_w": target_w,
        "desired_w": desired_w,
        "previous_w": previous_limit_w,
        "phase": phase,
        "hold_band_w": hold_band_w,
        "step_w": step_w,
        "curve_gap_pct": round(float(curve_gap_pct or 0.0), 3),
    }


def curve_charge_frame_followup(
    cfg: Dict[str, Any],
    previous_state: Dict[str, Any],
    actual_charge_w: int,
    desired_w: int,
    current_limit_w: int,
    pv_after_fixed_w: int,
    grid_w: int,
    max_charge_w: int,
    gentle_measured_trim: bool = False,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    now_s = time.time() if now_s is None else float(now_s)
    desired_w = max(0, int(desired_w or 0))
    current_limit_w = max(0, int(current_limit_w or 0))
    max_charge_w = max(0, int(max_charge_w or 0))
    if desired_w <= 0 or current_limit_w <= 0 or max_charge_w <= 0:
        return {"active": False}

    actual_charge_w = max(0, int(actual_charge_w or 0))
    export_w = max(0, -int(grid_w or 0))
    reserve_w = max(0, int(pv_after_fixed_w or 0))
    export_min_w = max(150, safe_int(cfg.get("storage_curve_frame_export_min_w"), 500))
    if export_w < export_min_w and reserve_w < desired_w + export_min_w:
        return {"active": False}

    deadband_w = max(50, safe_int(cfg.get("storage_curve_frame_lift_deadband_w"), 80))
    shortfall_w = max(0, desired_w - actual_charge_w)
    prev_auto_limit = previous_state.get("auto_limit") if isinstance(previous_state.get("auto_limit"), dict) else {}
    previous_limit_w = max(
        0,
        safe_int(
            prev_auto_limit.get(
                "max_charge_w",
                previous_state.get("parallel_val", previous_state.get("val", current_limit_w)),
            ),
            current_limit_w,
        ),
    )
    previous_gentle_trim = bool(
        gentle_measured_trim
        and previous_state.get("curve_frame_lift_active")
        and str(previous_state.get("curve_frame_lift_reason") or "") == "measured_trim"
        and previous_limit_w > current_limit_w + 10
    )
    if gentle_measured_trim:
        trim_max_shortfall_w = max(
            deadband_w,
            safe_int(cfg.get("storage_curve_frame_measured_trim_max_shortfall_w"), 260),
        )
        trim_max_boost_w = max(
            20,
            safe_int(cfg.get("storage_curve_frame_measured_trim_max_boost_w"), 160),
        )
        release_band_w = max(deadband_w, safe_int(cfg.get("storage_curve_frame_measured_trim_release_band_w"), 120))
        recheck_s = max(30.0, safe_float(cfg.get("storage_curve_frame_measured_trim_recheck_s"), 300.0))
        if actual_charge_w >= desired_w + release_band_w:
            return {"active": False, "shortfall_w": shortfall_w, "phase": "release"}
        if previous_gentle_trim:
            previous_offset_w = max(
                0,
                safe_int(
                    previous_state.get("curve_frame_measured_trim_offset_w"),
                    previous_limit_w - current_limit_w,
                ),
            )
            previous_offset_w = min(trim_max_boost_w, previous_offset_w)
            hold_until_ts = safe_float(previous_state.get("curve_frame_measured_trim_hold_until_ts"), 0.0)
            hold_active = bool(previous_offset_w > 0 and hold_until_ts > now_s)
            if hold_active or shortfall_w <= deadband_w:
                target_w = min(max_charge_w, current_limit_w + previous_offset_w)
                if reserve_w > 0:
                    target_w = min(target_w, max(desired_w, reserve_w))
                if target_w <= current_limit_w + 10:
                    return {"active": False, "shortfall_w": shortfall_w, "phase": "hold_release"}
                return {
                    "active": True,
                    "frame_w": max(0, int(target_w)),
                    "desired_w": desired_w,
                    "actual_charge_w": actual_charge_w,
                    "shortfall_w": shortfall_w,
                    "previous_limit_w": previous_limit_w,
                    "step_w": 0,
                    "max_boost_w": trim_max_boost_w,
                    "gentle_trim": True,
                    "gentle_trim_phase": "hold" if hold_active else "refresh",
                    "measured_trim_offset_w": previous_offset_w,
                    "measured_trim_anchor_ts": safe_int(
                        previous_state.get("curve_frame_measured_trim_anchor_ts"),
                        safe_int(previous_state.get("ts"), int(now_s)),
                    ),
                    "measured_trim_hold_until_ts": int(now_s + recheck_s),
                    "export_w": export_w,
                    "reserve_w": reserve_w,
                }
        if shortfall_w <= deadband_w:
            return {"active": False, "shortfall_w": shortfall_w}
        if shortfall_w <= trim_max_shortfall_w:
            trim_boost_w = max(20, min(trim_max_boost_w, shortfall_w))
            target_w = int(round(current_limit_w + trim_boost_w))
            target_w = max(current_limit_w, min(max_charge_w, target_w))
            if reserve_w > 0:
                target_w = min(target_w, max(desired_w, reserve_w))
            if target_w <= current_limit_w + 10:
                return {"active": False, "shortfall_w": shortfall_w}
            return {
                "active": True,
                "frame_w": max(0, int(target_w)),
                "desired_w": desired_w,
                "actual_charge_w": actual_charge_w,
                "shortfall_w": shortfall_w,
                "previous_limit_w": previous_limit_w,
                "step_w": 0,
                "max_boost_w": trim_max_boost_w,
                "gentle_trim": True,
                "gentle_trim_phase": "sample",
                "measured_trim_offset_w": trim_boost_w,
                "measured_trim_anchor_ts": int(now_s),
                "measured_trim_hold_until_ts": int(now_s + recheck_s),
                "export_w": export_w,
                "reserve_w": reserve_w,
            }

    max_boost_w = max(deadband_w, safe_int(cfg.get("storage_curve_frame_lift_max_boost_w"), 900))
    gentle_trim = False
    factor = max(0.25, min(2.0, safe_float(cfg.get("storage_curve_frame_lift_factor"), 0.7)))
    target_w = int(round(desired_w + shortfall_w * factor))
    target_w = min(target_w, desired_w + max_boost_w)
    target_w = max(current_limit_w, min(max_charge_w, target_w))
    if reserve_w > 0:
        target_w = min(target_w, max(desired_w, reserve_w))

    step_w = max(
        50 if gentle_trim else 100,
        safe_int(
            cfg.get("storage_curve_frame_measured_trim_step_w" if gentle_trim else "storage_curve_frame_lift_step_w"),
            120 if gentle_trim else 250,
        ),
    )
    if previous_limit_w > 0 and target_w > previous_limit_w:
        target_w = min(target_w, previous_limit_w + step_w)

    active_margin_w = 10 if gentle_trim else 50
    if target_w <= current_limit_w + active_margin_w:
        return {"active": False, "shortfall_w": shortfall_w}
    return {
        "active": True,
        "frame_w": max(0, int(target_w)),
        "desired_w": desired_w,
        "actual_charge_w": actual_charge_w,
        "shortfall_w": shortfall_w,
        "previous_limit_w": previous_limit_w,
        "step_w": step_w,
        "max_boost_w": max_boost_w,
        "gentle_trim": gentle_trim,
        "export_w": export_w,
        "reserve_w": reserve_w,
    }


def curve_charge_frame_smoothing(
    cfg: Dict[str, Any],
    previous_state: Dict[str, Any],
    actual_charge_w: int,
    desired_w: int,
    current_limit_w: int,
    pv_after_fixed_w: int,
    grid_w: int,
    max_charge_w: int,
    curve_gap_pct: float = 0.0,
) -> Dict[str, Any]:
    desired_w = max(0, int(desired_w or 0))
    current_limit_w = max(0, int(current_limit_w or 0))
    max_charge_w = max(0, int(max_charge_w or 0))
    if desired_w <= 0 or current_limit_w <= 0 or max_charge_w <= 0:
        return {"active": False}

    previous_state_name = str(previous_state.get("state") or previous_state.get("parallel_state") or "")
    previous_lift_active = bool(previous_state.get("curve_frame_lift_active"))
    if previous_state_name != "parallel_curve_charge" or not previous_lift_active:
        return {"active": False}

    prev_auto_limit = previous_state.get("auto_limit") if isinstance(previous_state.get("auto_limit"), dict) else {}
    previous_limit_w = max(
        0,
        safe_int(
            previous_state.get(
                "curve_frame_lift_w",
                prev_auto_limit.get(
                    "max_charge_w",
                    previous_state.get("parallel_val", previous_state.get("val", 0)),
                ),
            ),
            0,
        ),
    )
    if previous_limit_w <= current_limit_w + 50:
        return {"active": False}

    export_w = max(0, -int(grid_w or 0))
    reserve_w = max(0, int(pv_after_fixed_w or 0))
    export_min_w = max(150, safe_int(cfg.get("storage_curve_frame_export_min_w"), 500))
    if export_w < export_min_w and reserve_w < desired_w + export_min_w:
        return {"active": False}

    actual_charge_w = max(0, int(actual_charge_w or 0))
    settle_band_w = max(50, safe_int(cfg.get("storage_curve_frame_lift_settle_band_w"), 120))
    release_band_w = max(
        settle_band_w,
        safe_int(cfg.get("storage_curve_frame_lift_release_band_w"), 260),
    )
    decay_w = max(40, safe_int(cfg.get("storage_curve_frame_lift_decay_w"), 80))
    gap_hold_pct = max(
        0.0,
        safe_float(
            cfg.get("storage_curve_frame_lift_hold_gap_pct"),
            safe_float(cfg.get("storage_curve_frame_lift_gap_pct"), 0.75),
        ),
    )
    gap_hold_active = bool(curve_gap_pct >= gap_hold_pct)

    if gap_hold_active:
        target_w = previous_limit_w
        phase = "gap_hold"
    elif actual_charge_w > desired_w + release_band_w:
        target_w = max(current_limit_w, previous_limit_w - decay_w * 3)
        phase = "release"
    elif actual_charge_w >= max(0, desired_w - settle_band_w):
        target_w = max(current_limit_w, previous_limit_w - decay_w)
        phase = "decay"
    else:
        target_w = previous_limit_w
        phase = "hold"

    target_w = min(max_charge_w, target_w)
    if reserve_w > 0:
        target_w = min(target_w, max(desired_w, reserve_w))
    if target_w <= current_limit_w + 50:
        return {"active": False, "phase": phase}
    return {
        "active": True,
        "frame_w": max(0, int(target_w)),
        "desired_w": desired_w,
        "actual_charge_w": actual_charge_w,
        "shortfall_w": max(0, desired_w - actual_charge_w),
        "previous_limit_w": previous_limit_w,
        "step_w": decay_w,
        "export_w": export_w,
        "reserve_w": reserve_w,
        "phase": phase,
    }


def curve_auto_hold_continuation_frame(
    cfg: Dict[str, Any],
    previous_state: Dict[str, Any],
    shadow_inputs: Dict[str, Any],
    max_charge_w: int,
) -> Dict[str, Any]:
    if not isinstance(previous_state, dict) or not isinstance(shadow_inputs, dict):
        return {"active": False}
    if not (
        bool(shadow_inputs.get("curve_charge_release_stabilize_active"))
        or bool(shadow_inputs.get("curve_charge_soc_step_hold_active"))
        or bool(shadow_inputs.get("curve_auto_hold_release_stabilize_active"))
        or bool(shadow_inputs.get("curve_settle_hold_active"))
        or bool(shadow_inputs.get("curve_crossed_from_charge_hold"))
        or bool(shadow_inputs.get("curve_near_idle_hold"))
        or bool(shadow_inputs.get("curve_edge_export_keep_active"))
    ):
        return {"active": False}
    if (
        bool(shadow_inputs.get("pre_curve_hold_active"))
        or bool(shadow_inputs.get("forecast_curve_landing_hold_active"))
        or bool(shadow_inputs.get("sliding_horizon_active"))
    ):
        return {"active": False}
    previous_state_name = str(previous_state.get("state") or previous_state.get("parallel_state") or "")
    if previous_state_name not in ("parallel_curve_charge", "parallel_curve_auto_hold"):
        return {"active": False}
    if previous_state_name == "parallel_curve_auto_hold" and not bool(previous_state.get("curve_auto_hold_continuation_active")):
        return {"active": False}

    max_charge_w = max(0, int(max_charge_w or 0))
    if max_charge_w <= 0:
        return {"active": False}
    prev_auto_limit = previous_state.get("auto_limit") if isinstance(previous_state.get("auto_limit"), dict) else {}
    previous_limit_w = max(
        0,
        safe_int(prev_auto_limit.get("max_charge_w"), 0),
        safe_int(previous_state.get("curve_auto_hold_continuation_w"), 0),
        safe_int(previous_state.get("curve_frame_lift_w"), 0),
        safe_int(previous_state.get("parallel_val"), 0),
        safe_int(previous_state.get("val"), 0),
    )
    if previous_limit_w <= 0:
        return {"active": False}

    keep_w = max(
        50,
        safe_int(
            shadow_inputs.get("curve_charge_keep_w"),
            safe_int(cfg.get("storage_parallel_curve_charge_keep_w"), 120),
        ),
    )
    floor_w = max(300, keep_w)
    decay_w = max(
        40,
        safe_int(
            cfg.get("storage_curve_auto_hold_continuation_decay_w"),
            safe_int(cfg.get("storage_curve_frame_lift_decay_w"), 80),
        ),
    )
    offer_w = max(
        0,
        -safe_int(shadow_inputs.get("grid_ema_w"), 0),
        safe_int(shadow_inputs.get("curve_export_w"), 0),
        safe_int(shadow_inputs.get("pv_after_fixed_w"), 0),
        safe_int(shadow_inputs.get("curve_safe_charge_w"), 0),
        safe_int(shadow_inputs.get("curve_cap_excess_charge_w"), 0),
    )
    if offer_w < floor_w:
        return {"active": False, "offer_w": offer_w, "floor_w": floor_w}

    frame_w = min(max_charge_w, offer_w, max(floor_w, previous_limit_w - decay_w))
    if frame_w < floor_w:
        return {"active": False, "offer_w": offer_w, "floor_w": floor_w}
    return {
        "active": True,
        "frame_w": int(frame_w),
        "previous_limit_w": int(previous_limit_w),
        "decay_w": int(decay_w),
        "offer_w": int(offer_w),
        "floor_w": int(floor_w),
    }


def pv_after_fixed_load_w(
    pv_w: int,
    home_w: int,
    wp_w: int,
    wallbox_w: float,
    wallbox_power_source: str = "",
    live_wallbox_w: float = 0.0,
) -> int:
    if wallbox_w > 250.0 and wallbox_power_source == "wallbox_native" and live_wallbox_w <= 50.0:
        return max(0, int(pv_w) - int(home_w) - int(wp_w))
    return max(0, int(pv_w) - int(home_w) - int(wp_w) - int(wallbox_w))


def build_wallbox_budget_context(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_native: Optional[Dict[str, Any]],
    *,
    wb_intent_fresh: bool,
    wb_intent_bev_full_blocked: bool,
    wb_mode: int,
    wp_w: int,
    pv_w: int,
    home_w: int,
) -> WallboxBudgetContext:
    wb_power = wallbox_actual_power_snapshot(live, wb_native)
    wallbox_w = abs(safe_float(wb_power.get("power_w"), 0.0))
    wallbox_power_source = str(wb_power.get("source") or "live_data")
    live_wallbox_w = abs(safe_float(wb_power.get("live_w"), 0.0))
    native_wallbox_w = abs(safe_float(wb_power.get("native_w"), 0.0))
    home_includes_wallbox = bool(wb_power.get("home_includes_wallbox"))

    live_with_wallbox = dict(live)
    live_with_wallbox["Wallbox_Power"] = wallbox_w
    live_with_wallbox["Wallbox_Power_Source"] = wallbox_power_source
    live_with_wallbox["Wallbox_Live_Power"] = live_wallbox_w
    live_with_wallbox["Wallbox_Home_Includes"] = home_includes_wallbox
    live_with_wallbox = live_with_wallbox_native_balance(live_with_wallbox, wb_native)

    intent = wb_intent if wb_intent_fresh else {}
    wb_car_present = bool(
        wb_intent_fresh
        and not wb_intent_bev_full_blocked
        and wb_mode != MODE_OFF
        and (
            intent.get("active")
            or intent.get("car_active")
            or intent.get("connected")
            or intent.get("plugged")
        )
    )
    wb_possible_w = wallbox_possible_power(cfg, intent, wb_native or {})
    pv_after_fixed_w = pv_after_fixed_load_w(
        pv_w,
        home_w,
        wp_w,
        wallbox_w,
        wallbox_power_source,
        live_wallbox_w,
    )
    base_wb_budget_w = pv_after_fixed_w if wb_car_present else 0
    return WallboxBudgetContext(
        wallbox_w=wallbox_w,
        wallbox_power_source=wallbox_power_source,
        live_wallbox_w=live_wallbox_w,
        native_wallbox_w=native_wallbox_w,
        home_includes_wallbox=home_includes_wallbox,
        live_with_wallbox=live_with_wallbox,
        wb_possible_w=wb_possible_w,
        pv_after_fixed_w=pv_after_fixed_w,
        base_wb_budget_w=base_wb_budget_w,
        wb_car_present=wb_car_present,
    )


def hardening_contracts_from_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    raw_contracts = plan.get("hardening_contracts")
    if not isinstance(raw_contracts, dict):
        raw_contracts = meta.get("hardening_contracts")
    contracts = {}
    if isinstance(raw_contracts, dict):
        for key, value in raw_contracts.items():
            contracts[str(key)] = dict(value) if isinstance(value, dict) else value
    version = plan.get("hardening_contracts_version") or meta.get("hardening_contracts_version")
    scope = plan.get("hardening_contracts_scope") or meta.get("hardening_contracts_scope")
    return {
        "version": str(version or "hardening_contracts_v1"),
        "scope": str(scope or "roadmap_3_to_7"),
        "contracts": contracts,
    }


def build_multi_wallbox_fairness_contract(
    wb_native: Dict[str, Any],
    wb_context: WallboxBudgetContext,
    *,
    wb_intent_fresh: bool,
    wb_mode: int,
    decision: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    decision = decision or {}
    details = wb_native.get("wb_details") if isinstance(wb_native.get("wb_details"), list) else []
    slots = []
    connected_count = 0
    charging_count = 0
    active_count = 0
    phase_set = set()
    for idx, detail in enumerate(details if isinstance(details, list) else []):
        if not isinstance(detail, dict):
            continue
        slot_id = detail.get("id", detail.get("charger_id", detail.get("index", idx + 1)))
        power_w = max(0.0, _wallbox_detail_power_w(detail))
        connected = bool(detail.get("plug", detail.get("plug_state", detail.get("connected", False))))
        charging = bool(detail.get("charging", detail.get("charge_state", False))) or power_w > 250.0
        amp = safe_float(
            detail.get(
                "current_set_amp",
                detail.get("amp", detail.get("set_amp", detail.get("max_amp", 0.0))),
            ),
            0.0,
        )
        phases = safe_int(
            detail.get("phases_target", detail.get("phases_in_use", detail.get("phases_actual"))),
            0,
        )
        if phases not in (1, 2, 3):
            phases = 0
        if phases > 0:
            phase_set.add(phases)
        if connected:
            connected_count += 1
        if charging:
            charging_count += 1
        active = bool(connected or charging or amp >= 6.0 or power_w > 250.0)
        if active:
            active_count += 1
        slots.append({
            "id": slot_id,
            "connected": connected,
            "charging": charging,
            "power_w": round(power_w, 1),
            "current_set_amp": round(amp, 1),
            "phases": phases,
        })

    configured_count = len(slots)
    blockers = []
    if wb_mode == MODE_OFF:
        blockers.append("mode_off_observe_only")
    if not wb_intent_fresh:
        blockers.append("wallbox_intent_stale_or_missing")

    if configured_count > 1:
        status = "multi_visible"
    elif configured_count == 1:
        status = "single_wallbox"
    else:
        status = "no_native_slots"
    if blockers and configured_count <= 1:
        status = "observe_or_stale"

    return {
        "roadmap_item": 5,
        "name": "Multi-Wallbox-Fairness",
        "owner": "wallbox_manager+storage_manager",
        "active": bool(configured_count > 1 or active_count > 0),
        "status": status,
        "rule": (
            "One approved storage/wallbox budget; per-slot connection, charge, "
            "phase and minimum-power signals remain visible before prioritization."
        ),
        "signals": {
            "configured_slots": configured_count,
            "connected_slots": connected_count,
            "charging_slots": charging_count,
            "active_slots": active_count,
            "slot_ids": [slot.get("id") for slot in slots],
            "slot_phases": sorted(phase_set),
            "slots": slots,
            "wb_mode": wb_mode,
            "wb_intent_fresh": bool(wb_intent_fresh),
            "possible_power_w": max(0, safe_int(wb_context.wb_possible_w, 0)),
            "base_budget_w": max(0, safe_int(wb_context.base_wb_budget_w, 0)),
            "pv_after_fixed_w": max(0, safe_int(wb_context.pv_after_fixed_w, 0)),
            "wallbox_power_w": round(max(0.0, safe_float(wb_context.wallbox_w, 0.0)), 1),
            "wallbox_power_source": wb_context.wallbox_power_source,
            "home_includes_wallbox": bool(wb_context.home_includes_wallbox),
            "decision_state": str(decision.get("state") or ""),
            "decision_mode": safe_int(decision.get("mode"), MODE_AUTO),
        },
        "blockers": blockers,
        "exports": [
            "wallbox_native.wb_details",
            "wallbox_storage_intent.json",
            "storage_manager_state.hardening_contracts",
        ],
    }


def runtime_hardening_contracts(
    plan: Dict[str, Any],
    wb_native: Dict[str, Any],
    wb_context: WallboxBudgetContext,
    *,
    wb_intent_fresh: bool,
    wb_mode: int,
    decision: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    contracts_bundle = hardening_contracts_from_plan(plan)
    contracts = contracts_bundle.get("contracts") if isinstance(contracts_bundle.get("contracts"), dict) else {}
    contracts["multi_wallbox_fairness"] = build_multi_wallbox_fairness_contract(
        wb_native,
        wb_context,
        wb_intent_fresh=wb_intent_fresh,
        wb_mode=wb_mode,
        decision=decision,
    )
    contracts_bundle["contracts"] = contracts
    return contracts_bundle



def manual_override_storage_decision(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    manual_override: Dict[str, Any],
    max_charge_w: int,
    max_discharge_w: int,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    soc = safe_float(live.get("SOC"), 0.0)
    mode = str(manual_override.get("mode", "") or "").lower()
    target = safe_float(manual_override.get("target_soc"), 0.0)
    previous_state = previous_state or {}
    previous_manual_done = str(previous_state.get("state") or "") == "manual_override_done"
    previous_manual_release_mode = safe_int(previous_state.get("manual_release_mode"), -1)
    if mode == "charge" and (target <= 0 or soc < target - 0.2):
        return {
            "state": "manual_override",
            "mode": MODE_CHRG,
            "val": max_charge_w,
            "priority": "manual",
            "reason": "Manuell laden",
            "protected": True,
            "storage_req_w": max_charge_w,
            "budget_w": 0,
        }
    if (
        mode == "charge"
        and target > 0
        and soc >= target - 0.2
        and not (previous_manual_done and previous_manual_release_mode == MODE_CHRG)
    ):
        reason = "Manuell laden beendet: Ziel %.1f%% erreicht (SoC %.1f%%); E3DC AUTO freigeben" % (target, soc)
        return {
            "state": "manual_override_done",
            "mode": MODE_AUTO,
            "val": max_charge_w,
            "priority": "manual",
            "reason": reason,
            "protected": True,
            "storage_req_w": 0,
            "budget_w": 0,
            "manual_target_soc": target,
            "manual_reached_soc": round(soc, 2),
            "manual_release_mode": MODE_CHRG,
            "auto_limit": {
                "enabled": False,
                "release": True,
                "set_power_auto": True,
                "set_power_value": 0,
                "max_charge_w": max_charge_w,
                "max_discharge_w": max_discharge_w,
                "discharge_start_w": 0,
                "heartbeat_s": auto_limit_heartbeat_s(cfg),
                "reason": reason,
            },
        }
    if mode == "discharge" and (target <= 0 or soc > target + 0.2):
        return {
            "state": "manual_override",
            "mode": MODE_DISCH,
            "val": max_discharge_w,
            "priority": "manual",
            "reason": "Manuell entladen",
            "protected": True,
            "storage_req_w": 0,
            "budget_w": max_discharge_w,
        }
    if (
        mode == "discharge"
        and target > 0
        and soc <= target + 0.2
        and not (previous_manual_done and previous_manual_release_mode == MODE_DISCH)
    ):
        reason = "Manuell entladen beendet: Ziel %.1f%% erreicht (SoC %.1f%%); E3DC AUTO freigeben" % (target, soc)
        return {
            "state": "manual_override_done",
            "mode": MODE_AUTO,
            "val": max_charge_w,
            "priority": "manual",
            "reason": reason,
            "protected": True,
            "storage_req_w": 0,
            "budget_w": 0,
            "manual_target_soc": target,
            "manual_reached_soc": round(soc, 2),
            "manual_release_mode": MODE_DISCH,
            "auto_limit": {
                "enabled": False,
                "release": True,
                "set_power_auto": True,
                "set_power_value": 0,
                "max_charge_w": max_charge_w,
                "max_discharge_w": max_discharge_w,
                "discharge_start_w": 0,
                "heartbeat_s": auto_limit_heartbeat_s(cfg),
                "reason": reason,
            },
        }

    return None


def protected_decision(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_native: Optional[Dict[str, Any]],
    manual_override: Dict[str, Any],
    now_s: float,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    live = live_with_wallbox_native_balance(live, wb_native)
    max_charge_w = configured_charge_limit_w(cfg, live)
    max_discharge_w = configured_discharge_limit_w(cfg, live, max_charge_w)
    soc = safe_float(live.get("SOC"), 0.0)
    emergency = safe_int(live.get("Notstrom_Status", live.get("ems_emergency_power_status")), 0)
    if emergency in (1, 4):
        return {
            "state": "emergency_power",
            "mode": MODE_AUTO,
            "val": max_charge_w,
            "priority": "safety",
            "reason": "Notstrom/Inselbetrieb: E3DC autonom, externe Budgets gesperrt",
            "protected": True,
            "storage_req_w": 0,
            "budget_w": 0,
        }

    reserve_hold = ep_reserve_floor_decision(cfg, live, max_charge_w, previous_state)
    if reserve_hold is not None:
        market_owner = market_economics_decision(
            cfg,
            live,
            plan,
            now_s,
            max_charge_w,
            max_discharge_w,
            previous_state,
        )
        if (
            market_owner is not None
            and safe_int(market_owner.get("mode"), MODE_AUTO) == MODE_GRID
            and str(market_owner.get("market_economics_action") or "") in MARKET_GRID_ACTIONS
        ):
            market_owner["reserve_floor_charge_override"] = True
            market_owner["reserve_floor_charge_reason"] = reserve_hold.get("reason")
            return market_owner
        return reserve_hold

    manual_decision = manual_override_storage_decision(
        cfg,
        live,
        manual_override,
        max_charge_w,
        max_discharge_w,
        previous_state,
    )
    if manual_decision is not None:
        return manual_decision

    cheap = plan.get("cheap_grid_charge") or {}
    storm_grid = plan.get("storm_grid_charge") or {}
    awattar_mode = safe_int(plan.get("awattar_mode"), 1)
    storm_grid_active = storm_grid_charge_active(plan, int(now_s * 1000.0))
    cheap_grid_active = cheap_grid_charge_active(plan, int(now_s * 1000.0))
    if storm_grid_active or (awattar_mode == 2 and cheap_grid_active):
        grid_plan = storm_grid if storm_grid_active else cheap
        target_soc = safe_float(grid_plan.get("target_soc"), safe_float(plan.get("planning_target_soc"), 95.0))
        hysteresis = max(0.1, safe_float(grid_plan.get("hysteresis_pct"), 0.5))
        charge_w = max(300, min(max_charge_w, safe_int(grid_plan.get("charge_w"), max_charge_w)))
        room_w = grid_charge_room_w(cfg, live)
        if room_w is not None:
            charge_w = min(charge_w, room_w)
        if soc < target_soc - hysteresis:
            if charge_w < 300:
                auto_reason = "Unwetter-Netzladen wartet" if storm_grid_active else "Preisfenster aktiv: Netzladen wartet, Speicherentladung gesperrt"
                return {
                    "state": "storm_guard_grid_wait" if storm_grid_active else "price_boost_grid_wait",
                    "mode": MODE_AUTO,
                    "val": max_charge_w,
                    "priority": "safety",
                    "reason": auto_reason + "; Hausanschluss-Limit laesst kein Speicher-Netzladen zu",
                    "protected": True,
                    "storage_req_w": 0,
                    "budget_w": 0,
                    "auto_limit": discharge_block_auto_limit(cfg, max_charge_w, auto_reason),
                }
            return {
                "state": "storm_guard_grid" if storm_grid_active else "price_boost_grid",
                "mode": MODE_GRID,
                "val": charge_w,
                "priority": "storm_guard" if storm_grid_active else "price",
                "reason": grid_plan.get("reason") or ("Unwetterwarnung: Speicher aus Netz laden" if storm_grid_active else "Guenstiges Preisfenster: Speicher aus Netz laden"),
                "protected": True,
                "storage_req_w": charge_w,
                "budget_w": 0,
            }
        return None

    direct_marketing_owner = direct_marketing_decision(
        cfg,
        live,
        plan,
        now_s,
        max_charge_w,
        max_discharge_w,
        previous_state,
    )
    if direct_marketing_owner is not None:
        return direct_marketing_owner

    predump_floor_hold = predump_wallbox_floor_hold_decision(
        cfg,
        live,
        plan,
        wb_intent,
        wb_native,
        now_s,
        max_charge_w,
        max_discharge_w,
        previous_state,
    )
    if predump_floor_hold is not None:
        return predump_floor_hold

    openwb_primary_pv_charge = openwb_primary_pv_curve_charge_decision(
        cfg,
        live,
        plan,
        wb_intent,
        wb_native,
        now_s,
        max_charge_w,
        previous_state,
    )
    if openwb_primary_pv_charge is not None:
        return openwb_primary_pv_charge

    unmanaged_wb_floor_hold = unmanaged_wallbox_wbminsoc_hold_decision(
        cfg,
        live,
        plan,
        wb_intent,
        wb_native,
        now_s,
        max_charge_w,
        max_discharge_w,
        previous_state,
    )
    if unmanaged_wb_floor_hold is not None:
        return unmanaged_wb_floor_hold

    openwb_primary_pv_cap = openwb_primary_pv_discharge_cap_decision(
        cfg,
        live,
        wb_intent,
        wb_native,
        now_s,
        max_charge_w,
        max_discharge_w,
        previous_state,
    )
    if openwb_primary_pv_cap is not None:
        return openwb_primary_pv_cap

    floor_intent_fresh = bool(wb_intent) and now_s - safe_float(wb_intent.get("ts"), 0.0) <= 90.0
    floor_wb_mode = normalize_wb_mode(wb_intent.get("wb_mode_active", cfg.get("wb1_mode", 0)))
    floor_request = str(wb_intent.get("battery_request", "") or "").strip().lower()
    controlled_local_floor_regulation = bool(
        floor_intent_fresh
        and floor_wb_mode != MODE_OFF
        and floor_request == "hold_discharge"
        and not wallbox_intent_external_manager(wb_intent)
        and not bool(wb_intent.get("scheduled_slot_active"))
        and not bool(wb_intent.get("price_boost_active"))
        and not bool(wb_intent.get("price_plan_storage_protect"))
        and not bool(wb_intent.get("wbminsoc_gate_open", True))
        and (
            wb_intent.get("active")
            or wb_intent.get("car_active")
            or wb_intent.get("connected")
            or wb_intent.get("plugged")
            or wb_intent.get("charging_active")
            or wb_intent.get("start_requested")
        )
    )
    price_hold = price_storage_hold_requested(cfg, live, plan, wb_intent, now_s)
    floor_pv_wait = controlled_wallbox_floor_pv_wait_requested(cfg, live, wb_intent, wb_native, now_s)
    predump = (
        {}
        if price_hold or controlled_local_floor_regulation or floor_pv_wait
        else predump_request_from_plan(cfg, live, plan, now_s, max_discharge_w, previous_state)
    )
    if predump.get("active"):
        discharge_w = max(300, min(max_discharge_w, safe_int(predump.get("discharge_w"), max_discharge_w)))
        allow = predump_allow_flags(cfg, predump)
        consumer = predump_consumer_status(cfg, live, predump, wb_intent, wb_native, now_s)
        hard_predump_active = bool(predump.get("hard_predump"))
        grid_fallback_allowed = bool(
            (not hard_predump_active)
            or cfg_bool(cfg, "hard_predump_grid_enable", False)
        )
        hard_grid_limit_w = hard_predump_grid_limit_w(cfg, max_discharge_w) if (
            hard_predump_active and grid_fallback_allowed
        ) else 0
        bev_block_w = 0
        bev_timing: Dict[str, Any] = {}
        bev_grid_fallback_due = False
        grid_timing = (
            predump_grid_fallback_window(
                cfg,
                predump,
                now_s,
                hard_grid_limit_w if hard_grid_limit_w > 0 else max_discharge_w,
            )
            if grid_fallback_allowed
            else {}
        )
        grid_fallback_due = bool(grid_timing.get("due")) and grid_fallback_allowed
        if allow.get("wallbox") and "wallbox" in str(consumer.get("device_label", "")).split(","):
            wallbox_min_w = safe_int(consumer.get("wallbox_min_power_w"), 0)
            bev_timing = predump_wallbox_block_window(
                cfg,
                live,
                plan,
                predump,
                now_s,
                max_discharge_w,
                wallbox_min_w,
            )
            bev_grid_fallback_due = bool(bev_timing.get("grid_fallback_due"))
            if bev_timing.get("waiting"):
                other_devices = [
                    dev
                    for dev in str(consumer.get("device_label", "")).split(",")
                    if dev and dev != "wallbox"
                ]
                if not other_devices:
                    return None
                allow = dict(allow)
                allow["wallbox"] = False
                predump["allow"] = allow
                consumer["devices"] = other_devices
                consumer["device_label"] = ",".join(other_devices)
                consumer["available"] = bool(other_devices)
                consumer["wallbox_min_power_w"] = 0
                consumer["wallbox_min_amp"] = 0
                consumer["wallbox_min_phases"] = 0
                consumer["actual_load_w"] = predump_actual_consumer_load_w(live, allow)
            elif bev_timing and wallbox_min_w > 0 and discharge_w < wallbox_min_w:
                bev_block_w = min(max_discharge_w, wallbox_min_w)
                old_discharge_w = discharge_w
                discharge_w = max(discharge_w, bev_block_w)
                predump["discharge_w"] = discharge_w
                predump["budget_w"] = max(safe_int(predump.get("budget_w"), 0), discharge_w)
                reason = str(predump.get("reason") or "Pre-Dump aktiv")
                predump["reason"] = (
                    f"{reason}; BEV-Mindestleistung: {old_discharge_w}W -> {discharge_w}W "
                    f"({safe_int(consumer.get('wallbox_min_amp'), 0)}A/"
                    f"{safe_int(consumer.get('wallbox_min_phases'), 0)}p), berechneter Start statt Taktung"
                )
        consumer_load_w = safe_int(consumer.get("actual_load_w"), 0)
        consumer_active_w = max(250, safe_int(cfg.get("predump_consumer_active_w"), 250))
        consumer_wait_s = max(30.0, safe_float(cfg.get("predump_consumer_wait_s"), 180.0))
        force_before_s = max(0.0, safe_float(cfg.get("predump_force_grid_before_ladestart_s"), 900.0))
        previous_predump_consumer = str((previous_state or {}).get("state") or "") in (
            "pre_discharge_wait",
            "pre_discharge_consumer_auto",
        )
        previous_grid_fallback = (
            str((previous_state or {}).get("state") or "") == "pre_discharge"
            and bool((previous_state or {}).get("predump_grid_fallback"))
            and grid_fallback_allowed
        )
        if previous_predump_consumer:
            wait_since_ts = safe_float(
                (previous_state or {}).get("predump_consumer_wait_since_ts"),
                safe_float((previous_state or {}).get("ts"), now_s),
            )
        else:
            wait_since_ts = now_s
        consumer_takes_load = consumer_load_w >= consumer_active_w
        if consumer_takes_load:
            wait_since_ts = now_s
        wait_elapsed_s = max(0.0, now_s - wait_since_ts)
        deadline_s = safe_float(predump.get("deadline_ts"), 0.0)
        deadline_near = bool(deadline_s > 0.0 and (deadline_s - now_s) <= force_before_s)
        sufficient_consumer_w = max(consumer_active_w, int(discharge_w * 0.65))
        consumer_shortfall = bool(consumer_takes_load and consumer_load_w < sufficient_consumer_w)
        fallback_reason = ""
        if previous_grid_fallback and not consumer_takes_load:
            fallback_reason = "Grid-Fallback bleibt aktiv, Verbraucher nimmt Pre-Dump-Leistung nicht an"
        elif not consumer.get("available"):
            fallback_reason = "kein freigegebener Verbraucherpfad"
        elif not consumer_takes_load and (
            deadline_near
            or grid_fallback_due
            or bev_grid_fallback_due
            or (wait_elapsed_s >= consumer_wait_s and not bev_timing)
        ):
            if grid_fallback_due and not deadline_near and not bev_grid_fallback_due:
                fallback_reason = "späteste Entladezeit erreicht, Verbraucher nimmt Pre-Dump-Leistung nicht an"
            else:
                fallback_reason = "Verbraucher nimmt Pre-Dump-Leistung nicht an"
        elif consumer_shortfall and deadline_near:
            fallback_reason = "Verbraucher nimmt zu wenig Pre-Dump-Leistung an"

        predump_floor_soc = predump.get(
            "consumer_landing_floor_soc",
            predump.get("target_soc"),
        )
        predump_common = {
            "priority": "predump",
            "protected": True,
            "storage_req_w": 0,
            "budget_w": max(0, safe_int(predump.get("budget_w"), discharge_w)),
            "predump_active": True,
            "predump_allow": allow,
            "predump_target_soc": predump.get("target_soc"),
            "predump_floor_soc": predump_floor_soc,
            "predump_trajectory_soc": predump.get("trajectory_soc"),
            "predump_trajectory_start_soc": predump.get("trajectory_start_soc"),
            "predump_hours_remaining": predump.get("hours_remaining"),
            "predump_consumer_landing_under_pct": predump.get("consumer_landing_under_pct"),
            "predump_consumer_landing_under_wh": predump.get("consumer_landing_under_wh"),
            "predump_consumer_devices": consumer.get("device_label", ""),
            "predump_consumer_load_w": consumer_load_w,
            "predump_wallbox_min_power_w": safe_int(consumer.get("wallbox_min_power_w"), 0),
            "predump_wallbox_min_amp": safe_int(consumer.get("wallbox_min_amp"), 0),
            "predump_wallbox_min_phases": safe_int(consumer.get("wallbox_min_phases"), 0),
            "predump_bev_block_w": bev_block_w,
            "predump_bev_start_ts": bev_timing.get("start_ts"),
            "predump_bev_grid_latest_ts": bev_timing.get("grid_latest_ts"),
            "predump_bev_remaining_wh": bev_timing.get("remaining_wh"),
            "predump_grid_latest_ts": grid_timing.get("latest_ts"),
            "predump_grid_remaining_wh": grid_timing.get("remaining_wh"),
            "predump_grid_duration_s": grid_timing.get("duration_s"),
            "predump_consumer_wait_since_ts": wait_since_ts,
            "predump_consumer_wait_elapsed_s": round(wait_elapsed_s, 1),
            "predump_hard_predump": hard_predump_active,
            "predump_grid_allowed": grid_fallback_allowed,
            "predump_grid_blocked_by_comfort": False,
        }
        if hard_grid_limit_w > 0:
            predump_common["predump_hard_grid_limit_w"] = hard_grid_limit_w
        if isinstance(predump.get("home_feedback_guard"), dict):
            predump_common["home_feedback_guard"] = predump["home_feedback_guard"]
        export_headroom = predump_grid_export_headroom(cfg, live)
        if export_headroom.get("limited"):
            predump_common["predump_grid_export_target_w"] = export_headroom.get("target_w")
            predump_common["predump_grid_export_w"] = export_headroom.get("export_w")
            predump_common["predump_grid_export_headroom_w"] = export_headroom.get("headroom_w")
            predump_common["predump_grid_base_export_w"] = export_headroom.get("base_export_w")
            predump_common["predump_grid_battery_discharge_w"] = export_headroom.get("battery_discharge_w")
            predump_common["predump_grid_discharge_limit_w"] = export_headroom.get("discharge_limit_w")
        if not fallback_reason:
            state = "pre_discharge_consumer_auto" if consumer_takes_load else "pre_discharge_wait"
            reason = predump.get("reason") or "Pre-Dump aktiv"
            if consumer_takes_load:
                reason = (
                    f"{reason}; Verbraucherpfad aktiv ({consumer_load_w}W), "
                    "E3DC-AUTO mit Entladegrenze"
                )
            else:
                reason = (
                    f"{reason}; warte auf Verbraucherpfad "
                    f"({consumer.get('device_label') or 'freigegeben'})"
                )
            result = {
                "state": state,
                "mode": MODE_AUTO,
                "val": discharge_w,
                "reason": reason,
                "predump_no_grid": True,
                "predump_grid_fallback": False,
                "auto_limit": charge_block_auto_limit(
                    cfg,
                    max_discharge_w,
                    "Pre-Dump wartet/fährt über Verbraucher: Laden gesperrt, Entladen freigegeben",
                ),
            }
            result.update(predump_common)
            return result
        if fallback_reason and not grid_fallback_allowed:
            reason = predump.get("reason") or "Pre-Dump aktiv"
            reason = (
                f"{reason}; Komfort-Netz-Fallback deaktiviert: {fallback_reason}; "
                "warte auf freigegebene Verbraucher oder normalen Hausverbrauch"
            )
            predump_common["predump_grid_blocked_by_comfort"] = True
            result = {
                "state": "pre_discharge_wait",
                "mode": MODE_AUTO,
                "val": discharge_w,
                "reason": reason,
                "predump_no_grid": True,
                "predump_grid_fallback": False,
                "auto_limit": charge_block_auto_limit(
                    cfg,
                    max_discharge_w,
                    "Komfort-Pre-Dump wartet: Netz-Fallback ist in der Konfiguration nicht freigegeben",
                ),
            }
            result.update(predump_common)
            return result
        reason = predump.get("reason") or "Pre-Dump aktiv"
        if fallback_reason:
            reason = f"{reason}; Grid-Fallback: {fallback_reason}"
        if export_headroom.get("limited"):
            discharge_limit_w = safe_int(export_headroom.get("discharge_limit_w"), 0)
            target_w = safe_int(export_headroom.get("target_w"), 0)
            export_w = safe_int(export_headroom.get("export_w"), 0)
            base_export_w = safe_int(export_headroom.get("base_export_w"), 0)
            battery_discharge_w = safe_int(export_headroom.get("battery_discharge_w"), 0)
            capped_w = predump_floor_budget_w(cfg, min(discharge_w, discharge_limit_w))
            if capped_w < 300:
                if previous_grid_fallback:
                    reason = (
                        f"{reason}; Grid-Fallback hält DISCH 0W: Export {export_w}W am Ziel {target_w}W, "
                        f"Basis-Export ohne Akku {base_export_w}W"
                    )
                    predump_common["budget_w"] = 0
                    result = {
                        "state": "pre_discharge",
                        "mode": MODE_DISCH,
                        "val": 0,
                        "reason": reason,
                        "predump_no_grid": False,
                        "predump_grid_fallback": True,
                    }
                    result.update(predump_common)
                    return result
                reason = (
                    f"{reason}; Grid-Fallback wartet: Export {export_w}W am Ziel {target_w}W, "
                    f"Basis-Export ohne Akku {base_export_w}W, kein Netz-Headroom für zusätzlichen Pre-Dump"
                )
                result = {
                    "state": "pre_discharge_wait",
                    "mode": MODE_AUTO,
                    "val": discharge_w,
                    "reason": reason,
                    "predump_no_grid": True,
                    "predump_grid_fallback": False,
                    "auto_limit": charge_block_auto_limit(
                        cfg,
                        max_discharge_w,
                        "Pre-Dump wartet: Exportgrenze schützt vor Netz-Dump",
                    ),
                }
                result.update(predump_common)
                return result
            if capped_w < discharge_w:
                old_discharge_w = discharge_w
                discharge_w = capped_w
                reason = (
                    f"{reason}; Export-Headroom begrenzt Netz-Dump "
                    f"{old_discharge_w}W -> {discharge_w}W "
                    f"(Export {export_w}W, Akku-Ist {battery_discharge_w}W, Ziel {target_w}W)"
                )
                predump_common["budget_w"] = min(
                    safe_int(predump_common.get("budget_w"), discharge_w),
                    discharge_w,
                )
        hard_grid_limit_applies = bool(
            hard_predump_active
            and fallback_reason
            and not consumer_takes_load
            and hard_grid_limit_w > 0
        )
        if hard_grid_limit_applies and discharge_w > hard_grid_limit_w:
            old_discharge_w = discharge_w
            discharge_w = predump_floor_budget_w(cfg, hard_grid_limit_w)
            predump_common["budget_w"] = min(
                safe_int(predump_common.get("budget_w"), discharge_w),
                discharge_w,
            )
            predump_common["predump_hard_grid_limited"] = True
            predump_common["predump_hard_grid_uncapped_w"] = old_discharge_w
            reason = (
                f"{reason}; Komfort-Netz-Fallback begrenzt "
                f"{old_discharge_w}W -> {discharge_w}W "
                f"(Limit {hard_grid_limit_w}W, kein aktiver Verbraucherpfad)"
            )
        grid_guard_active = safe_int(predump.get("grid_guard_w"), 0) > 0
        ramp_allowed = not (grid_guard_active or deadline_near or grid_fallback_due or bev_grid_fallback_due)
        if ramp_allowed:
            ramped_w, ramp_limited, ramp_base_w, ramp_step_w = predump_grid_ramped_discharge_w(
                cfg,
                live,
                previous_state,
                discharge_w,
            )
        else:
            ramped_w, ramp_limited, ramp_base_w, ramp_step_w = discharge_w, False, discharge_w, 0
        if ramp_limited:
            reason = (
                f"{reason}; Grid-Fallback-Rampe begrenzt Anhebung "
                f"{discharge_w}W -> {ramped_w}W "
                f"(Basis {ramp_base_w}W, Schritt {ramp_step_w}W)"
            )
            discharge_w = ramped_w
            predump_common["budget_w"] = min(
                safe_int(predump_common.get("budget_w"), discharge_w),
                discharge_w,
            )
        predump_common["predump_grid_ramp_base_w"] = ramp_base_w
        predump_common["predump_grid_ramp_step_w"] = ramp_step_w
        result = {
            "state": "pre_discharge",
            "mode": MODE_DISCH,
            "val": discharge_w,
            "reason": reason,
            "predump_no_grid": False,
            "predump_grid_fallback": bool(fallback_reason or previous_grid_fallback),
        }
        result.update(predump_common)
        return result

    market_owner = market_economics_decision(
        cfg,
        live,
        plan,
        now_s,
        max_charge_w,
        max_discharge_w,
        previous_state,
    )
    if market_owner is not None:
        return market_owner

    return None


HEADROOM_EXECUTION_CONTRACT_VERSION = 1


def _headroom_typed_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _headroom_timestamp_s(value: Any) -> Optional[float]:
    parsed = _headroom_typed_number(value)
    if parsed is None or parsed <= 0.0:
        return None
    return parsed / 1000.0 if parsed > 100_000_000_000.0 else parsed


def legacy_headroom_execution_contract(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    previous_state: Dict[str, Any],
    now_s: float,
) -> Dict[str, Any]:
    """Bindet die Legacy-Headroomentladung an den aktuellen kanonischen Slot.

    Der globale Prognosedruck bleibt Diagnose. Eine reale DISCH-Freigabe entsteht
    erst aus einem frischen, hashgültigen Plan, dessen aktueller Slot innerhalb
    des expliziten Pre-Dump-Fensters positive Restenergie ausweist.
    """

    blocked: Dict[str, Any] = {
        "schema_version": HEADROOM_EXECUTION_CONTRACT_VERSION,
        "allowed": False,
        "reason_code": "HEADROOM_PLAN_UNVALIDATED",
        "plan_id": None,
        "slot_id": None,
        "earliest_start_ts": None,
        "deadline_ts": None,
        "target_soc": None,
        "hard_floor_soc": None,
        "plan_accounted_wh": 0.0,
        "slot_accounted_wh": 0.0,
        "residual_wh": 0.0,
        "accounted_observed_w": 0.0,
        "accounted_interval_s": 0.0,
        "generation_reset": True,
    }
    validation = validate_canonical_plan(plan, int(now_s * 1000.0))
    blocked["plan_id"] = validation.get("plan_id")
    blocked["slot_id"] = validation.get("slot_id")
    if not validation.get("valid"):
        blocked["reason_code"] = "HEADROOM_%s" % str(
            validation.get("block_reason_code") or "PLAN_INVALID"
        )
        return blocked

    plan_id = str(validation.get("plan_id") or "")
    slot_id = str(validation.get("slot_id") or "")
    slot = validation.get("slot") if isinstance(validation.get("slot"), dict) else {}
    slots = plan.get("slots") if isinstance(plan.get("slots"), list) else []
    execution_slots = []
    execution_deadlines = set()
    for item in slots:
        if not isinstance(item, dict):
            blocked["reason_code"] = "HEADROOM_PLAN_SLOT_INVALID"
            return blocked
        item_headroom = item.get("headroom_wh") if isinstance(item.get("headroom_wh"), dict) else None
        item_residual_wh = (
            _headroom_typed_number(item_headroom.get("residual"))
            if item_headroom is not None
            else None
        )
        if item_residual_wh is None or item_residual_wh < 0.0:
            blocked["reason_code"] = "HEADROOM_PLAN_SLOT_RESIDUAL_INVALID"
            return blocked
        if item_residual_wh <= 0.0:
            continue
        item_start_s = _headroom_timestamp_s(item.get("start_ts_ms"))
        item_deadline_s = _headroom_timestamp_s(item_headroom.get("deadline_ts_ms"))
        if item_start_s is None or item_deadline_s is None or item_start_s >= item_deadline_s:
            blocked["reason_code"] = "HEADROOM_PLAN_SLOT_WINDOW_INVALID"
            return blocked
        execution_slots.append((item, item_residual_wh, item_start_s, item_deadline_s))
        execution_deadlines.add(round(item_deadline_s, 3))
    if not execution_slots:
        blocked["reason_code"] = "HEADROOM_PLAN_RESIDUAL_DEPLETED"
        return blocked
    if len(execution_deadlines) != 1:
        blocked["reason_code"] = "HEADROOM_PLAN_DEADLINE_INCONSISTENT"
        return blocked

    start_s = min(item[2] for item in execution_slots)
    deadline_s = execution_slots[0][3]
    blocked["earliest_start_ts"] = start_s
    blocked["deadline_ts"] = deadline_s
    if start_s is None:
        blocked["reason_code"] = "HEADROOM_EARLIEST_START_MISSING_OR_INVALID"
        return blocked
    if deadline_s is None or start_s >= deadline_s:
        blocked["reason_code"] = "HEADROOM_DEADLINE_MISSING_OR_INVALID"
        return blocked
    if now_s < start_s:
        blocked["reason_code"] = "HEADROOM_EXECUTION_BEFORE_START"
        return blocked
    if now_s >= deadline_s:
        blocked["reason_code"] = "HEADROOM_EXECUTION_AFTER_DEADLINE"
        return blocked

    slot_start_s = _headroom_timestamp_s(slot.get("start_ts_ms"))
    slot_end_s = _headroom_timestamp_s(slot.get("end_ts_ms"))
    if (
        not slot_id
        or slot_start_s is None
        or slot_end_s is None
        or not slot_start_s <= now_s < slot_end_s
    ):
        blocked["reason_code"] = "HEADROOM_CURRENT_SLOT_BINDING_INVALID"
        return blocked

    slot_headroom = slot.get("headroom_wh") if isinstance(slot.get("headroom_wh"), dict) else None
    if slot_headroom is None:
        blocked["reason_code"] = "HEADROOM_CURRENT_SLOT_RESIDUAL_MISSING"
        return blocked
    slot_required_wh = _headroom_typed_number(slot_headroom.get("required"))
    slot_residual_wh = _headroom_typed_number(slot_headroom.get("residual"))
    slot_deadline_s = _headroom_timestamp_s(slot_headroom.get("deadline_ts_ms"))
    if (
        slot_required_wh is None
        or slot_residual_wh is None
        or slot_required_wh < 0.0
        or slot_residual_wh < 0.0
        or slot_residual_wh > slot_required_wh + 0.5
        or slot_deadline_s is None
        or abs(slot_deadline_s - deadline_s) > 1.0
    ):
        blocked["reason_code"] = "HEADROOM_CURRENT_SLOT_RESIDUAL_INVALID"
        return blocked

    # Nur die hashgebundenen kanonischen Slots sind ausführungsautoritativ.
    # Die Legacy-Wurzelfelder bleiben Diagnose und dürfen keine Freigabe oder
    # Restenergie am plan_id-Vertrag vorbei erzeugen.
    total_residual_wh = sum(item[1] for item in execution_slots)
    projection = slot.get("projection") if isinstance(slot.get("projection"), dict) else {}
    target_soc = _headroom_typed_number(projection.get("target_soc_pct"))
    if target_soc is None or not 0.0 <= target_soc <= 100.0:
        blocked["reason_code"] = "HEADROOM_TARGET_SOC_MISSING_OR_INVALID"
        return blocked
    soc_contract = slot.get("soc_pct") if isinstance(slot.get("soc_pct"), dict) else {}
    hard_floor_candidates = [target_soc]
    for value in (
        soc_contract.get("reserve_floor"),
        soc_contract.get("notstrom_floor"),
        cfg.get("storage_predump_min_soc"),
        cfg.get("emergency_power_reserve"),
    ):
        parsed = _headroom_typed_number(value)
        if parsed is not None and 0.0 <= parsed <= 100.0:
            hard_floor_candidates.append(parsed)
    hard_floor_soc = max(hard_floor_candidates)
    blocked["target_soc"] = round(target_soc, 3)
    blocked["hard_floor_soc"] = round(hard_floor_soc, 3)

    previous_plan_id = str(previous_state.get("headroom_execution_plan_id") or "")
    previous_deadline_s = _headroom_timestamp_s(
        previous_state.get("headroom_execution_deadline_ts")
    )
    same_generation = bool(
        previous_plan_id == plan_id
        and previous_deadline_s is not None
        and abs(previous_deadline_s - deadline_s) <= 1.0
    )
    plan_accounted_wh = 0.0
    slot_accounted_wh = 0.0
    if same_generation:
        plan_accounted_wh = max(
            0.0,
            _headroom_typed_number(previous_state.get("headroom_execution_plan_accounted_wh")) or 0.0,
        )
        if str(previous_state.get("headroom_execution_slot_id") or "") == slot_id:
            slot_accounted_wh = max(
                0.0,
                _headroom_typed_number(previous_state.get("headroom_execution_slot_accounted_wh")) or 0.0,
            )

    accounted_interval_s = 0.0
    accounted_observed_w = 0.0
    if (
        same_generation
        and str(previous_state.get("parallel_state") or previous_state.get("state") or "")
        == "parallel_headroom_discharge"
    ):
        last_account_ts = _headroom_typed_number(
            previous_state.get("headroom_execution_last_account_ts")
        )
        max_gap_s = max(
            15.0,
            min(180.0, safe_float(cfg.get("storage_headroom_discharge_energy_gap_s"), 180.0)),
        )
        if last_account_ts is not None and 0.0 < last_account_ts <= now_s:
            accounted_interval_s = min(max_gap_s, max(0.0, now_s - last_account_ts))
            accounted_observed_w = max(0.0, -safe_float(live.get("Battery_Power"), 0.0))
            realized_wh = accounted_observed_w * accounted_interval_s / 3600.0
            plan_accounted_wh += realized_wh
            if str(previous_state.get("headroom_execution_slot_id") or "") == slot_id:
                slot_accounted_wh += realized_wh

    remaining_plan_wh = max(0.0, total_residual_wh - plan_accounted_wh)
    remaining_slot_wh = max(0.0, slot_residual_wh - slot_accounted_wh)
    residual_wh = min(remaining_plan_wh, remaining_slot_wh)
    blocked.update({
        "plan_id": plan_id,
        "slot_id": slot_id,
        "plan_accounted_wh": round(plan_accounted_wh, 3),
        "slot_accounted_wh": round(slot_accounted_wh, 3),
        "slot_required_wh": round(slot_required_wh, 3),
        "slot_residual_planned_wh": round(slot_residual_wh, 3),
        "total_residual_planned_wh": round(total_residual_wh, 3),
        "residual_wh": round(residual_wh, 3),
        "accounted_observed_w": round(accounted_observed_w, 3),
        "accounted_interval_s": round(accounted_interval_s, 3),
        "generation_reset": not same_generation,
    })
    soc = _headroom_typed_number(live.get("SOC"))
    if soc is None:
        blocked["reason_code"] = "HEADROOM_LIVE_SOC_MISSING_OR_INVALID"
        return blocked
    if soc <= hard_floor_soc:
        blocked["reason_code"] = "HEADROOM_TARGET_OR_HARD_FLOOR_REACHED"
        return blocked
    if residual_wh <= 0.0:
        blocked["reason_code"] = "HEADROOM_RESIDUAL_DEPLETED"
        return blocked

    return {
        **blocked,
        "allowed": True,
        "reason_code": "HEADROOM_EXECUTION_WINDOW_ACTIVE",
        "generation_reset": not same_generation,
    }


def decide_next_cycle(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_native: Optional[Dict[str, Any]] = None,
    manual_override: Optional[Dict[str, Any]] = None,
    previous_state: Optional[Dict[str, Any]] = None,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    now_s = time.time() if now_s is None else float(now_s)
    wb_native = wb_native or {}
    manual_override = manual_override or {}
    previous_state = previous_state or {}

    input_snapshot = build_storage_input_snapshot(live, plan, wb_native, now_s=now_s)
    live_model = input_snapshot.live
    live = live_model.to_legacy_dict()
    plan = (
        input_snapshot.plan
        if isinstance(input_snapshot.plan, ValidatedCanonicalPlanSnapshot)
        else input_snapshot.plan.to_legacy_dict()
    )
    wb_native = input_snapshot.wallbox.to_legacy_dict()
    live_plausibility = live_model.plausibility if isinstance(live_model.plausibility, dict) else live_power_plausibility(live)
    live_sample_invalid = not bool(live_plausibility.get("sample_valid", True))

    cfg = effective_power_limit_cfg(cfg, live)
    max_charge_w = configured_charge_limit_w(cfg, live)
    max_discharge_w = configured_discharge_limit_w(cfg, live, max_charge_w)
    soc = safe_float(live_model.soc, 0.0)
    curve_control_soc = soc
    pv_w = safe_int(live_model.pv_w, 0)
    grid_w = safe_int(live_model.grid_w, 0)
    grid_ema_w = safe_int(live.get("Grid_EMA_W", grid_w), grid_w)
    home_w = max(0, safe_int(live_model.home_w, 0))
    bat_w = safe_int(live_model.battery_w, 0)
    wp_w = max(0, safe_int(live_model.heatpump_w, 0))
    live_age_s = live_model.age_s if live_model.ts > 0.0 else live_data_age_s(live, now_s)
    live_stale_guard_s = max(0.0, safe_float(cfg.get("storage_live_stale_guard_s"), 10.0))
    live_stale = bool(live_stale_guard_s > 0.0 and live_age_s > live_stale_guard_s)
    soc_jump_guard = soc_jump_guard_context(cfg, live, previous_state, now_s)
    soc_unrealistic = bool(soc_jump_guard.get("invalid"))
    wb_intent_fresh = bool(wb_intent) and now_s - safe_float(wb_intent.get("ts"), 0.0) <= 60.0
    wallbox_phase_transition = wallbox_phase_transition_reservation_contract(
        wb_intent,
        now_s=now_s,
    )
    wb_intent_bev_full_blocked = bool(
        wb_intent_fresh
        and wb_intent.get("bev_full_blocked")
        and not wb_intent.get("charging_active")
        and safe_float(wb_intent.get("wb_power_w"), 0.0) <= 250.0
    )
    wb_mode = normalize_wb_mode(wb_intent.get("wb_mode_active", cfg.get("wb1_mode", 0)))
    external_wallbox_manager_active = bool(
        wb_intent_fresh
        and wallbox_intent_external_manager(wb_intent)
    )
    wb_budget_context = build_wallbox_budget_context(
        cfg,
        live,
        wb_intent,
        wb_native,
        wb_intent_fresh=wb_intent_fresh,
        wb_intent_bev_full_blocked=wb_intent_bev_full_blocked,
        wb_mode=wb_mode,
        wp_w=wp_w,
        pv_w=pv_w,
        home_w=home_w,
    )
    wallbox_w = wb_budget_context.wallbox_w
    wallbox_power_source = wb_budget_context.wallbox_power_source
    live_wallbox_w = wb_budget_context.live_wallbox_w
    native_wallbox_w = wb_budget_context.native_wallbox_w
    home_includes_wallbox = wb_budget_context.home_includes_wallbox
    live_with_wallbox = wb_budget_context.live_with_wallbox
    wb_car_present = wb_budget_context.wb_car_present
    wb_possible_w = wb_budget_context.wb_possible_w
    pv_after_fixed_w = wb_budget_context.pv_after_fixed_w
    base_wb_budget_w = wb_budget_context.base_wb_budget_w
    observe_reserve_release = observe_wallbox_reserve_release_context(
        cfg,
        live_with_wallbox,
        wb_intent if wb_intent_fresh else {},
        wb_mode,
        previous_state,
    )
    observe_reserve_release_active = bool(observe_reserve_release.get("active"))
    active_state = {}
    adaptive_curve = {}
    adaptive_curve_active = False
    adaptive_curve_relation = ""
    adaptive_floor_soc = None
    adaptive_ceiling_soc = None
    adaptive_latest_charge_due = False
    adaptive_latest_charge_start_ts = 0.0
    adaptive_evening_shortfall_wh = 0.0
    adaptive_headroom_required_wh = 0.0
    adaptive_headroom_available_wh = 0.0
    adaptive_curtailment_pressure_wh = 0.0
    adaptive_curtailment_unavoidable_wh = 0.0
    adaptive_latest_charge_clamped = False
    adaptive_latest_charge_raw_ts = 0.0
    adaptive_latest_charge_previous_ts = 0.0
    headroom_reserve_active = False
    headroom_reserve_pressure_wh = 0.0
    headroom_reserve_source = ""
    forecast_only_target_active = False
    forecast_curve_landing_hold_active = False
    forecast_floor_target_gap_pct = 0.0
    forecast_landing_margin_pct = 0.0
    sliding_horizon = {"active": False, "reason": ""}
    sliding_horizon_active = False

    protected = None if (live_stale or live_sample_invalid or soc_unrealistic) else protected_decision(
        cfg,
        live_with_wallbox,
        plan,
        wb_intent if wb_intent_fresh else {},
        wb_native,
        manual_override,
        now_s,
        previous_state,
    )
    manual_invalid_sample_decision = None
    manual_override_mode = str(manual_override.get("mode", "") or "").lower()
    if (
        live_sample_invalid
        and not live_stale
        and not soc_unrealistic
        and manual_override_mode in ("charge", "discharge")
    ):
        # A power-balance glitch must remain diagnostic-only for an explicit manual battery command.
        manual_invalid_sample_decision = manual_override_storage_decision(
            cfg,
            live_with_wallbox,
            manual_override,
            max_charge_w,
            max_discharge_w,
            previous_state,
        )
        if manual_invalid_sample_decision is not None:
            manual_invalid_sample_decision = dict(manual_invalid_sample_decision)
            manual_invalid_sample_decision["live_plausibility_manual_override_kept"] = True
    decision = None
    stale_guard = storage_live_stale_decision(
        cfg=cfg,
        plan=plan,
        live_age_s_value=live_age_s,
        live_stale=live_stale,
        max_charge_w=max_charge_w,
        now_s=now_s,
    )
    if stale_guard is not None:
        curve_soc = stale_guard["curve_soc"]
        target_soc = stale_guard["target_soc"]
        target_ts = stale_guard["target_ts"]
        i_fc_w = stale_guard["i_fc_w"]
        i_min_lade_w = stale_guard["i_min_lade_w"]
        decision = stale_guard["decision"]
    else:
        plausibility_guard = None
        if manual_invalid_sample_decision is not None:
            curve_soc, target_soc, target_ts = current_curve(plan, now_s)
            i_fc_w = safe_int(manual_invalid_sample_decision.get("storage_req_w"), 0)
            i_min_lade_w = i_fc_w
            decision = manual_invalid_sample_decision
        else:
            plausibility_guard = storage_live_plausibility_decision(
                cfg=cfg,
                plan=plan,
                plausibility=live_plausibility,
                max_charge_w=max_charge_w,
                now_s=now_s,
                previous_state=previous_state,
            )
        if decision is None and plausibility_guard is not None:
            curve_soc = plausibility_guard["curve_soc"]
            target_soc = plausibility_guard["target_soc"]
            target_ts = plausibility_guard["target_ts"]
            i_fc_w = plausibility_guard["i_fc_w"]
            i_min_lade_w = plausibility_guard["i_min_lade_w"]
            decision = plausibility_guard["decision"]
        elif soc_unrealistic:
            soc_guard = storage_soc_jump_guard_decision(cfg, plan, soc_jump_guard, max_charge_w, now_s)
            curve_soc = soc_guard["curve_soc"]
            target_soc = soc_guard["target_soc"]
            target_ts = soc_guard["target_ts"]
            i_fc_w = soc_guard["i_fc_w"]
            i_min_lade_w = soc_guard["i_min_lade_w"]
            decision = soc_guard["decision"]
        elif protected is not None:
            decision = protected
            i_fc_w = safe_int(decision.get("storage_req_w"), 0)
            i_min_lade_w = i_fc_w
            curve_soc, target_soc, target_ts = current_curve(plan, now_s)
        else:
            curve_soc, target_soc, target_ts = current_curve(plan, now_s)
            # Normal regulation continues below.
    if decision is None:
        pv_curve_before_start = bool(pv_w > 250)
        curve_lookahead_h = max(0.25, safe_float(cfg.get("tl_lookahead_h"), 1.0))
        adaptive_curve = adaptive_curve_context(
            plan,
            soc,
            now_s,
            lookahead_h=curve_lookahead_h,
            allow_before_start=pv_curve_before_start,
        )
        adaptive_curve_active = bool(adaptive_curve.get("active"))
        adaptive_curve_relation = str(adaptive_curve.get("relation") or "")
        adaptive_floor_soc = adaptive_curve.get("floor_soc")
        adaptive_ceiling_soc = adaptive_curve.get("ceiling_soc")
        adaptive_latest_charge_due = bool(adaptive_curve.get("latest_charge_due"))
        adaptive_latest_charge_start_ts = safe_float(adaptive_curve.get("latest_charge_start_ts"), 0.0)
        adaptive_evening_shortfall_wh = safe_float(adaptive_curve.get("evening_shortfall_wh"), 0.0)
        adaptive_headroom_required_wh = safe_float(adaptive_curve.get("headroom_required_wh"), 0.0)
        adaptive_headroom_available_wh = safe_float(adaptive_curve.get("headroom_available_wh"), 0.0)
        adaptive_curtailment_pressure_wh = safe_float(adaptive_curve.get("curtailment_pressure_wh"), 0.0)
        adaptive_curtailment_unavoidable_wh = safe_float(adaptive_curve.get("curtailment_unavoidable_wh"), 0.0)
        headroom_reserve_active = bool(adaptive_curve.get("headroom_reserve_active"))
        headroom_reserve_pressure_wh = safe_float(adaptive_curve.get("headroom_reserve_pressure_wh"), 0.0)
        headroom_reserve_source = str(adaptive_curve.get("headroom_reserve_source") or "")
        curve_soc = adaptive_curve.get("control_soc")
        target_soc = adaptive_curve.get("target_soc")
        target_ts = adaptive_curve.get("target_ts")
        storage_kwh = max(1.0, safe_float(cfg.get("speichergroesse"), safe_float(live.get("bat_full_cap_kwh"), 10.0)))
        curve_control_soc = curve_control_soc_estimate(soc, bat_w, storage_kwh, previous_state, now_s, cfg)
        can_reach_target = bool(plan.get("can_reach_target", True))
        awattar_mode = safe_int(plan.get("awattar_mode"), 1)
        release_ts_s = curve_release_ts_s(plan)
        latest_clamp = latest_charge_start_clamp_context(
            cfg,
            previous_state,
            adaptive_latest_charge_start_ts,
            now_s,
        )
        if bool(latest_clamp.get("active")):
            adaptive_latest_charge_clamped = True
            adaptive_latest_charge_raw_ts = safe_float(
                latest_clamp.get("raw_latest_charge_start_ts"),
                adaptive_latest_charge_start_ts,
            )
            adaptive_latest_charge_previous_ts = safe_float(
                latest_clamp.get("previous_latest_charge_start_ts"),
                0.0,
            )
            adaptive_latest_charge_start_ts = safe_float(
                latest_clamp.get("latest_charge_start_ts"),
                adaptive_latest_charge_start_ts,
            )
            adaptive_latest_charge_due = bool(
                adaptive_evening_shortfall_wh > 0.0
                or (adaptive_latest_charge_start_ts > 0.0 and now_s >= adaptive_latest_charge_start_ts)
            )
        raw_evening_release = bool(release_ts_s > 0 and now_s >= release_ts_s)
        effective_target_soc = max(
            safe_float(plan.get("effective_target_soc"), -1.0),
            safe_float(plan.get("planning_target_soc"), -1.0),
            safe_float(plan.get("target_soc"), -1.0),
            safe_float(target_soc, -1.0) if target_soc is not None else -1.0,
            safe_float(cfg.get("storage_target_soc"), -1.0),
        )
        shortfall_catchup_margin_pct = max(
            0.1,
            safe_float(cfg.get("storage_curve_shortfall_catchup_margin_pct"), 0.5),
        )
        parallel_curve_enter_w = max(120, safe_int(cfg.get("storage_parallel_curve_charge_enter_w"), 300))
        parallel_curve_reenter_w = max(
            parallel_curve_enter_w,
            safe_int(cfg.get("storage_parallel_curve_charge_reenter_w"), parallel_curve_enter_w + 300),
        )
        default_shortfall_catchup_enter_w = max(
            1200,
            parallel_curve_reenter_w,
            parallel_curve_enter_w * 3,
        )
        shortfall_catchup_enter_w = max(
            120,
            safe_int(
                cfg.get("storage_curve_shortfall_catchup_enter_w"),
                default_shortfall_catchup_enter_w,
            ),
        )
        shortfall_catchup_nominal_enter_w = shortfall_catchup_enter_w
        shortfall_late_catchup_active = bool(
            adaptive_latest_charge_due
            or adaptive_evening_shortfall_wh >= 200.0
            or adaptive_latest_charge_clamped
        )
        if shortfall_late_catchup_active:
            shortfall_late_enter_w = max(
                120,
                safe_int(
                    cfg.get("storage_curve_shortfall_late_catchup_enter_w"),
                    parallel_curve_enter_w,
                ),
            )
            shortfall_catchup_enter_w = min(
                shortfall_catchup_enter_w,
                max(parallel_curve_enter_w, shortfall_late_enter_w),
            )
        else:
            shortfall_late_enter_w = shortfall_catchup_enter_w
        shortfall_real_surplus_w = max(
            0,
            -grid_w,
            pv_w - home_w - wp_w - max(0, safe_int(live_with_wallbox.get("Wallbox_Power"), 0)),
        )
        shortfall_target_gap_pct = max(0.0, effective_target_soc - curve_control_soc)
        evening_release_margin_pct = max(
            0.05,
            safe_float(cfg.get("storage_evening_release_target_margin_pct"), 0.3),
        )
        evening_target_reached = bool(
            effective_target_soc <= 0.0
            or curve_control_soc >= (effective_target_soc - evening_release_margin_pct)
            or not can_reach_target
        )
        evening_release_blocked_by_target = bool(raw_evening_release and not evening_target_reached)
        evening_release = bool(raw_evening_release and evening_target_reached)
        adaptive_below_floor = bool(adaptive_curve_relation in ("below_floor", "no_curve"))
        adaptive_inside_band = bool(adaptive_curve_relation == "inside_band")
        adaptive_above_ceiling = bool(adaptive_curve_relation == "above_ceiling")
        target_meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
        forecast_only_target_active = bool(target_meta.get("forecast_only_target_active")) or str(
            target_meta.get("target_mode") or ""
        ).strip().lower() in ("forecast_100", "forecast_only_100", "forecast_only", "prognose_100", "prognose")
        forecast_landing_margin_pct = max(
            0.05,
            safe_float(cfg.get("storage_forecast100_curve_landing_margin_pct"), 0.2),
        )
        if target_soc is not None:
            forecast_floor_target_gap_pct = max(0.0, float(target_soc) - curve_control_soc)
        forecast_curve_landing_hold_active = bool(
            forecast_only_target_active
            and can_reach_target
            and not evening_release
            and adaptive_curve_active
            and adaptive_floor_soc is not None
            and curve_control_soc >= safe_float(adaptive_floor_soc, 0.0) - forecast_landing_margin_pct
            and forecast_floor_target_gap_pct <= forecast_landing_margin_pct
            and not adaptive_latest_charge_due
            and adaptive_curtailment_pressure_wh < 200.0
            and adaptive_curtailment_unavoidable_wh < 200.0
        )
        hard_anchor = hard_noon_anchor(plan, now_s=now_s)
        sliding_horizon = sliding_forecast_horizon_context(
            cfg,
            plan,
            now_s,
            curve_control_soc,
            effective_target_soc,
            release_ts_s,
            adaptive_latest_charge_start_ts,
            adaptive_evening_shortfall_wh,
            forecast_only_target_active,
            can_reach_target,
            adaptive_headroom_required_wh,
            adaptive_headroom_available_wh,
            adaptive_curtailment_pressure_wh,
            adaptive_curtailment_unavoidable_wh,
            headroom_reserve_active,
            hard_anchor,
            previous_state,
        )
        sliding_horizon_active = bool(sliding_horizon.get("active"))
        shortfall_catchup_curve_pressure = bool(
            (not can_reach_target)
            or (adaptive_below_floor and not sliding_horizon_active)
            or adaptive_latest_charge_due
            or evening_release_blocked_by_target
        )
        shortfall_pv_catchup_base_candidate = bool(
            forecast_only_target_active
            and not forecast_curve_landing_hold_active
            and effective_target_soc > 0.0
            and shortfall_target_gap_pct >= shortfall_catchup_margin_pct
            and pv_w > 250
            and adaptive_curve_relation != "above_ceiling"
            and not headroom_reserve_active
            and adaptive_headroom_required_wh < 200.0
            and adaptive_curtailment_pressure_wh < 200.0
        )
        shortfall_catchup_blocked_curve_ready = bool(
            shortfall_pv_catchup_base_candidate
            and not shortfall_catchup_curve_pressure
        )
        shortfall_pv_catchup_candidate = bool(
            shortfall_pv_catchup_base_candidate
            and shortfall_catchup_curve_pressure
        )
        shortfall_catchup_blocked_low_surplus = bool(
            shortfall_pv_catchup_candidate
            and shortfall_real_surplus_w < shortfall_catchup_enter_w
        )
        shortfall_pv_catchup_active = bool(
            shortfall_pv_catchup_candidate
            and shortfall_real_surplus_w >= shortfall_catchup_enter_w
        )
        if shortfall_pv_catchup_active and target_soc is not None:
            target_soc = max(float(target_soc), effective_target_soc)
        i_fc_w = 0
        i_min_lade_w = 0
        curve_gap_catchup_w = 0
        curve_gap_catchup_cap_w = 0
        curve_gap_catchup_factor = 0.0
        curve_gap_catchup_min_w = 0
        curve_gap_catchup_taper_pct = 0.0
        curve_need_raw_w = 0
        lookahead_need_w = 0
        curve_gap_pct = 0.0
        curve_hard_anchor_need_w = 0
        curve_hard_anchor_gap_pct = 0.0
        curve_hard_anchor_missed = False
        curve_hard_anchor_mode = ""
        curve_frame_base_smoothing = {"active": False}
        state_name = "auto"
        curve_follow_allowed = bool(
            (not adaptive_curve_active)
            or (adaptive_below_floor and not sliding_horizon_active)
            or adaptive_latest_charge_due
            or shortfall_pv_catchup_active
        )
        price_hold = price_storage_hold_requested(
            cfg,
            live_with_wallbox,
            plan,
            wb_intent if wb_intent_fresh else {},
            now_s,
        )
        planned_load = current_planned_load_status(cfg, live_with_wallbox, plan, now_s)
        if bool(planned_load.get("active")) and bool(planned_load.get("confirmed")):
            if str(planned_load.get("mode") or "") == "price_support" and bool(planned_load.get("support_allowed")):
                state_name = "planned_load_price_support"
            else:
                state_name = "planned_load_storage_hold"
        elif awattar_mode == 0 or price_hold:
            state_name = "price_plan_storage_hold"
        elif evening_release and not shortfall_pv_catchup_active:
            state_name = "evening_release"
        elif (
            (can_reach_target or shortfall_pv_catchup_active)
            and target_soc is not None
            and target_ts is not None
            and pv_w > 250
            and curve_follow_allowed
        ):
            delta_pct = max(0.0, float(target_soc) - curve_control_soc)
            hours = max(0.20, (float(target_ts) - now_s) / 3600.0)
            lookahead_need_w = int((delta_pct / 100.0) * storage_kwh * 1000.0 / hours)
            catchup_deadband_pct = max(0.0, safe_float(cfg.get("storage_curve_catchup_deadband_pct"), 0.05))
            if curve_soc is not None:
                curve_gap_pct = max(0.0, float(curve_soc) - curve_control_soc)
                curve_gap_catchup_taper_pct = max(
                    0.2,
                    safe_float(
                        cfg.get("storage_curve_catchup_taper_pct"),
                        safe_float(
                            cfg.get("storage_parallel_curve_tolerance_pct"),
                            safe_float(cfg.get("tl_tolerance_pct"), 3.0),
                        ),
                    ),
                )
                curve_gap_catchup_min_w = min(
                    max_charge_w,
                    max(
                        0,
                        safe_int(
                            cfg.get("storage_curve_catchup_min_w"),
                            safe_int(cfg.get("storage_parallel_curve_charge_enter_w"), 300),
                        ),
                    ),
                )
                if curve_gap_pct > catchup_deadband_pct:
                    curve_gap_catchup_factor = max(
                        0.0,
                        min(1.0, curve_gap_pct / curve_gap_catchup_taper_pct),
                    )
                    catchup_h = max(
                        0.5,
                        safe_float(
                            cfg.get("storage_curve_catchup_h"),
                            0.75,
                        ),
                    )
                    catchup_extra_wh = max(
                        0.0,
                        (curve_gap_pct - catchup_deadband_pct) / 100.0 * storage_kwh * 1000.0,
                    )
                    catchup_avg_w = int(round(catchup_extra_wh / catchup_h)) if catchup_h > 0 else 0
                    catchup_max_w = min(
                        max_charge_w,
                        max(
                            curve_gap_catchup_min_w,
                            safe_int(cfg.get("storage_curve_catchup_max_w"), max_charge_w),
                        ),
                    )
                    curve_gap_catchup_cap_w = min(
                        catchup_max_w,
                        max(curve_gap_catchup_min_w, catchup_avg_w),
                    )
                    curve_gap_catchup_w = curve_gap_catchup_cap_w
            if bool(hard_anchor.get("active")) and bool(hard_anchor.get("locked")):
                anchor_ts_s = safe_float(hard_anchor.get("ts_s"), 0.0)
                anchor_soc = safe_float(hard_anchor.get("soc"), -1.0)
                curve_hard_anchor_gap_pct = max(0.0, anchor_soc - curve_control_soc)
                if anchor_ts_s > 0.0 and anchor_soc >= 0.0 and curve_hard_anchor_gap_pct > 0.0:
                    if now_s <= anchor_ts_s:
                        anchor_hours = max(0.20, (anchor_ts_s - now_s) / 3600.0)
                        anchor_raw_w = int(
                            (curve_hard_anchor_gap_pct / 100.0) * storage_kwh * 1000.0 / anchor_hours
                        )
                        anchor_max_w = min(
                            max_charge_w,
                            max(
                                0,
                                safe_int(
                                    cfg.get("storage_curve_hard_anchor_catchup_max_w"),
                                    safe_int(cfg.get("storage_curve_catchup_max_w"), max_charge_w),
                                ),
                            ),
                        )
                        curve_hard_anchor_need_w = min(anchor_raw_w, anchor_max_w) if anchor_max_w > 0 else anchor_raw_w
                        curve_hard_anchor_mode = "deadline"
                    elif curve_hard_anchor_gap_pct > catchup_deadband_pct:
                        anchor_catchup_h = max(
                            0.25,
                            safe_float(cfg.get("storage_curve_hard_anchor_catchup_h"), 0.75),
                        )
                        anchor_extra_wh = max(
                            0.0,
                            (curve_hard_anchor_gap_pct - catchup_deadband_pct)
                            / 100.0
                            * storage_kwh
                            * 1000.0,
                        )
                        anchor_raw_w = int(round(anchor_extra_wh / anchor_catchup_h)) if anchor_catchup_h > 0 else 0
                        anchor_min_w = min(
                            max_charge_w,
                            max(
                                0,
                                curve_gap_catchup_min_w,
                                safe_int(cfg.get("storage_curve_catchup_min_w"), 0),
                                safe_int(cfg.get("storage_parallel_curve_charge_enter_w"), 300),
                            ),
                        )
                        anchor_max_w = min(
                            max_charge_w,
                            max(
                                anchor_min_w,
                                safe_int(
                                    cfg.get("storage_curve_hard_anchor_catchup_max_w"),
                                    safe_int(cfg.get("storage_curve_catchup_max_w"), max_charge_w),
                                ),
                            ),
                        )
                        curve_hard_anchor_need_w = min(anchor_max_w, max(anchor_min_w, anchor_raw_w))
                        curve_hard_anchor_missed = True
                        curve_hard_anchor_mode = "missed"
            curve_need_raw_w = max(lookahead_need_w, curve_gap_catchup_w, curve_hard_anchor_need_w)
            # Der Lookahead ist die normale Fuehrungsleistung bis zum naechsten
            # Anker. Die Catch-up-Kappe darf nur zusaetzliche Aufholjagd
            # begrenzen, aber den bereits passenden Lookahead nicht nach unten
            # druecken.
            need_w = curve_need_raw_w
            if need_w >= 120:
                i_fc_w = max(0, min(max_charge_w, need_w))
                i_min_lade_w = max(0, min(max_charge_w, int(need_w * 0.65)))
                curve_frame_base_smoothing = curve_charge_base_frame_smoothing(
                    cfg,
                    previous_state,
                    i_fc_w,
                    max_charge_w,
                    now_s,
                    curve_gap_pct=curve_gap_pct,
                    hard_anchor_need_w=curve_hard_anchor_need_w,
                    shortfall_active=shortfall_pv_catchup_active,
                    headroom_active=headroom_reserve_active,
                    curve_charge_enter_w=parallel_curve_enter_w,
                )
                if bool(curve_frame_base_smoothing.get("active")):
                    i_fc_w = max(0, min(max_charge_w, safe_int(curve_frame_base_smoothing.get("frame_w"), i_fc_w)))
                    i_min_lade_w = max(0, min(max_charge_w, int(i_fc_w * 0.65)))
                state_name = "curve_follow"
        elif not can_reach_target:
            state_name = "auto"

        headroom_execution = legacy_headroom_execution_contract(
            cfg,
            live,
            plan,
            previous_state,
            now_s,
        )
        active_state = {
            "state": state_name,
            "storage_state": state_name,
            "mode": MODE_AUTO,
            "val": max_charge_w,
            "now_ts_s": now_s,
            "soc": curve_control_soc,
            "raw_soc": soc,
            "curve_control_soc": curve_control_soc,
            "pv_w": pv_w,
            "grid_w": grid_w,
            "grid_ema_w": grid_ema_w,
            "home_ema_w": home_w,
            "live_sample_valid": bool(live_plausibility.get("sample_valid", True)),
            "home_power_valid": bool(live_plausibility.get("home_valid", True)),
            "grid_power_valid": bool(live_plausibility.get("grid_valid", True)),
            "home_power_source": live_plausibility.get("home_source"),
            "home_power_balance_w": safe_int(live_plausibility.get("home_balance_w"), 0),
            "home_power_delta_w": safe_int(live_plausibility.get("home_delta_w"), 0),
            "live_glitch_reasons": live_plausibility.get("reasons") if isinstance(live_plausibility.get("reasons"), list) else [],
            "bat_w": bat_w,
            "ep_reserve_pct": ep_reserve_soc(cfg, live),
            "iFc_w": i_fc_w,
            "iMinLade_w": i_min_lade_w,
            "curve_gap_pct": curve_gap_pct,
            "curve_gap_catchup_w": curve_gap_catchup_w,
            "curve_gap_catchup_cap_w": curve_gap_catchup_cap_w,
            "curve_gap_catchup_factor": curve_gap_catchup_factor,
            "curve_gap_catchup_min_w": curve_gap_catchup_min_w,
            "curve_gap_catchup_taper_pct": curve_gap_catchup_taper_pct,
            "curve_need_raw_w": curve_need_raw_w,
            "lookahead_need_w": lookahead_need_w,
            "curve_hard_anchor_need_w": curve_hard_anchor_need_w,
            "curve_hard_anchor_gap_pct": curve_hard_anchor_gap_pct,
            "curve_hard_anchor_missed": curve_hard_anchor_missed,
            "curve_hard_anchor_mode": curve_hard_anchor_mode,
            "curve_hard_anchor_soc": hard_anchor.get("soc") if bool(hard_anchor.get("active")) else None,
            "curve_hard_anchor_ts": hard_anchor.get("ts_s") if bool(hard_anchor.get("active")) else None,
            "curve_frame_base_smoothing_active": bool(curve_frame_base_smoothing.get("active")),
            "curve_frame_base_smoothing_phase": curve_frame_base_smoothing.get("phase", ""),
            "curve_frame_base_smoothing_desired_w": safe_int(curve_frame_base_smoothing.get("desired_w"), 0),
            "curve_frame_base_smoothing_previous_w": safe_int(curve_frame_base_smoothing.get("previous_w"), 0),
            "curve_frame_base_smoothing_hold_band_w": safe_int(curve_frame_base_smoothing.get("hold_band_w"), 0),
            "curve_frame_base_smoothing_step_w": safe_int(curve_frame_base_smoothing.get("step_w"), 0),
            "adaptive_curve_active": adaptive_curve_active,
            "adaptive_curve_relation": adaptive_curve_relation,
            "adaptive_soc_floor": adaptive_floor_soc,
            "adaptive_soc_ceiling": adaptive_ceiling_soc,
            "adaptive_inside_band": adaptive_inside_band,
            "adaptive_below_floor": adaptive_below_floor,
            "adaptive_above_ceiling": adaptive_above_ceiling,
            "adaptive_latest_charge_due": adaptive_latest_charge_due,
            "latest_charge_start_ts": adaptive_latest_charge_start_ts,
            "latest_charge_start_clamped": bool(adaptive_latest_charge_clamped),
            "latest_charge_start_raw_ts": adaptive_latest_charge_raw_ts,
            "latest_charge_start_previous_ts": adaptive_latest_charge_previous_ts,
            "evening_shortfall_wh": adaptive_evening_shortfall_wh,
            "shortfall_pv_catchup_active": shortfall_pv_catchup_active,
            "shortfall_target_soc": round(effective_target_soc, 2) if effective_target_soc > 0 else None,
            "shortfall_target_gap_pct": round(shortfall_target_gap_pct, 2),
            "shortfall_real_surplus_w": int(shortfall_real_surplus_w),
            "shortfall_catchup_enter_w": int(shortfall_catchup_enter_w),
            "shortfall_catchup_nominal_enter_w": int(shortfall_catchup_nominal_enter_w),
            "shortfall_late_catchup_enter_w": int(shortfall_late_enter_w),
            "shortfall_late_catchup_active": bool(shortfall_late_catchup_active),
            "shortfall_catchup_curve_pressure": shortfall_catchup_curve_pressure,
            "shortfall_catchup_blocked_curve_ready": shortfall_catchup_blocked_curve_ready,
            "shortfall_catchup_blocked_low_surplus": shortfall_catchup_blocked_low_surplus,
            "forecast_only_target_active": bool(forecast_only_target_active),
            "forecast_curve_landing_hold_active": bool(forecast_curve_landing_hold_active),
            "forecast_floor_target_gap_pct": round(forecast_floor_target_gap_pct, 3),
            "forecast_landing_margin_pct": round(forecast_landing_margin_pct, 3),
            "sliding_horizon_active": bool(sliding_horizon_active),
            "sliding_horizon_enabled": bool(sliding_horizon.get("enabled", False)),
            "sliding_horizon_reason": str(sliding_horizon.get("reason") or ""),
            "sliding_horizon_confidence": safe_float(sliding_horizon.get("confidence"), 0.0),
            "sliding_horizon_min_confidence": safe_float(sliding_horizon.get("min_confidence"), 0.0),
            "sliding_horizon_season": str(sliding_horizon.get("season") or ""),
            "sliding_horizon_season_factor": safe_float(sliding_horizon.get("season_factor"), 0.0),
            "sliding_horizon_soc_factor": safe_float(sliding_horizon.get("soc_factor"), 0.0),
            "sliding_horizon_horizon_factor": safe_float(sliding_horizon.get("horizon_factor"), 0.0),
            "sliding_horizon_target_gap_pct": safe_float(sliding_horizon.get("target_gap_pct"), 0.0),
            "sliding_horizon_minutes_until_latest_charge": sliding_horizon.get("minutes_until_latest_charge"),
            "sliding_horizon_headroom_available_wh": safe_float(sliding_horizon.get("headroom_available_wh"), 0.0),
            "sliding_horizon_uncovered_pressure_wh": safe_float(sliding_horizon.get("uncovered_pressure_wh"), 0.0),
            "sliding_horizon_uncovered_curtailment_pressure_wh": safe_float(
                sliding_horizon.get("uncovered_curtailment_pressure_wh"),
                0.0,
            ),
            "adaptive_headroom_required_wh": adaptive_headroom_required_wh,
            "adaptive_headroom_available_wh": adaptive_headroom_available_wh,
            "curtailment_pressure_wh": adaptive_curtailment_pressure_wh,
            "curtailment_unavoidable_wh": adaptive_curtailment_unavoidable_wh,
            "headroom_reserve_active": headroom_reserve_active,
            "headroom_reserve_pressure_wh": headroom_reserve_pressure_wh,
            "headroom_reserve_source": headroom_reserve_source,
            "headroom_execution": headroom_execution,
            "iAVal_w": base_wb_budget_w,
            "wb_possible_power_w": wb_possible_w,
            "ac_power_limit_w": safe_int(live.get("ac_power_limit_w"), 0),
            "dc0_max_w": safe_int(live.get("dc0_max_w"), 0),
            "dc1_max_w": safe_int(live.get("dc1_max_w"), 0),
            "dc2_max_w": safe_int(live.get("dc2_max_w"), 0),
            "dc3_max_w": safe_int(live.get("dc3_max_w"), 0),
            "derate_at_power_w": safe_int(live.get("derate_at_power_w"), 0),
            "pv_derating_active": bool(live.get("pv_derating_active")),
            "ems_derating_active": bool(live.get("ems_derating_active")),
            "power_limits_active": bool(live.get("power_limits_active")),
            "ems_power_settings_read": live.get("ems_power_settings_read") is True,
            "ems_power_settings_valid": live.get("ems_power_settings_valid") is True,
            "ems_max_charge_power_w": live.get("ems_max_charge_power_w"),
            "ems_max_discharge_power_w": live.get("ems_max_discharge_power_w"),
            "ems_discharge_start_power_w": live.get("ems_discharge_start_power_w"),
            "curve_release_ts_s": release_ts_s,
            "evening_release_active": evening_release,
            "evening_release_blocked_by_target": evening_release_blocked_by_target,
            "evening_release_target_reached": evening_target_reached,
            "evening_release_target_margin_pct": round(evening_release_margin_pct, 3),
            "planned_load_active": bool(planned_load.get("active")),
            "planned_load_confirmed": bool(planned_load.get("confirmed")),
            "planned_load_expected_w": safe_int(planned_load.get("expected_w"), 0),
            "planned_load_observed_extra_w": safe_int(planned_load.get("observed_extra_w"), 0),
            "planned_load_observed_home_w": safe_int(planned_load.get("observed_home_w"), 0),
            "planned_load_baseline_home_w": safe_int(planned_load.get("baseline_home_w"), 0),
            "planned_load_tolerance_w": safe_int(planned_load.get("tolerance_w"), 0),
            "planned_load_mode": planned_load.get("mode", ""),
            "planned_load_reason": planned_load.get("reason", ""),
            "planned_load_support_allowed": bool(planned_load.get("support_allowed")),
            "planned_load_support_reason": planned_load.get("support_reason", ""),
            "planned_load_support": planned_load.get("support", {}),
            "planned_load_windows": planned_load.get("windows", []),
        }
        previous_parallel_state = str(
            previous_state.get("parallel_state")
            or (
                previous_state.get("state")
                if str(previous_state.get("state") or "").startswith("parallel_")
                else ""
            )
            or ""
        )
        if previous_parallel_state:
            active_state["previous_parallel_state"] = previous_parallel_state
            active_state["previous_parallel_mode"] = safe_int(
                previous_state.get("parallel_mode", previous_state.get("mode")),
                MODE_AUTO,
            )
            active_state["previous_parallel_val"] = max(
                0,
                safe_int(previous_state.get("parallel_val", previous_state.get("val")), 0),
            )
            previous_since_ts = safe_float(
                previous_state.get("parallel_state_since_ts"),
                0.0,
            )
            if previous_since_ts <= 0:
                previous_since_ts = safe_float(previous_state.get("ts"), 0.0)
            if previous_since_ts > 0:
                active_state["previous_parallel_ts"] = previous_since_ts
        for key in (
            "headroom_discharge_day",
            "headroom_discharge_today_wh",
            "headroom_discharge_last_active_ts",
            "headroom_discharge_last_account_ts",
            "curve_cap_release_below_since_ts",
            "curve_cap_release_pending",
            "curve_cap_release_requested",
            "curve_cap_release_confirmed_since_ts",
            "curve_cap_post_release_until_ts",
        ):
            if previous_state.get(key) is not None:
                active_state[key] = previous_state.get(key)
        if previous_state.get("ts") is not None:
            active_state["previous_state_ts"] = previous_state.get("ts")
        if previous_state.get("last_wb_active_ts"):
            active_state["last_wb_active_ts"] = previous_state.get("last_wb_active_ts")
        if previous_state.get("last_wb_possible_power_w"):
            active_state["last_wb_possible_power_w"] = previous_state.get("last_wb_possible_power_w")
        if previous_state.get("last_auto_ts"):
            active_state["last_auto_ts"] = previous_state.get("last_auto_ts")
        elif safe_int(previous_state.get("mode"), -1) == MODE_AUTO and previous_state.get("ts"):
            active_state["last_auto_ts"] = previous_state.get("ts")

        wb_budget_in = {
            "budget_w": base_wb_budget_w,
            "iAVal_w": base_wb_budget_w,
            "wb_possible_power_w": wb_possible_w,
        }
        payload = ParallelStorageRegulator(cfg).decide(
            active_state=active_state,
            live={
                "SOC": soc,
                "PV_Power": pv_w,
                "Ext_PV_Power": live.get("Ext_PV_Power"),
                "Ext_PV_Power_Valid": live.get("Ext_PV_Power_Valid") is True,
                "Ext_PV_Power_Source": live.get("Ext_PV_Power_Source"),
                "Ext_PV_Power_Age_S": live.get("Ext_PV_Power_Age_S"),
                "Grid_Power": grid_w,
                "Home_Power": home_w,
                "Home_Power_Valid": bool(live_plausibility.get("home_valid", True)),
                "Grid_Power_Valid": bool(live_plausibility.get("grid_valid", True)),
                "RSCP_Sample_Valid": bool(live_plausibility.get("sample_valid", True)),
                "Home_Power_Source": live_plausibility.get("home_source"),
                "Home_Power_Balance": safe_int(live_plausibility.get("home_balance_w"), 0),
                "Home_Power_Delta": safe_int(live_plausibility.get("home_delta_w"), 0),
                "WP_Power": wp_w,
                "Battery_Power": bat_w,
                "Wallbox_Power": wallbox_w,
                "Wallbox_Home_Includes": home_includes_wallbox,
                "Wallbox_Power_Source": wallbox_power_source,
                "Wallbox_Live_Power": live_wallbox_w,
                "ac_power_limit_w": safe_int(live.get("ac_power_limit_w"), 0),
                "dc0_max_w": safe_int(live.get("dc0_max_w"), 0),
                "dc1_max_w": safe_int(live.get("dc1_max_w"), 0),
                "dc2_max_w": safe_int(live.get("dc2_max_w"), 0),
                "dc3_max_w": safe_int(live.get("dc3_max_w"), 0),
                "derate_at_power_w": safe_int(live.get("derate_at_power_w"), 0),
                "pv_derating_active": bool(live.get("pv_derating_active")),
                "ems_derating_active": bool(live.get("ems_derating_active")),
                "power_limits_active": bool(live.get("power_limits_active")),
                "ems_power_settings_read": live.get("ems_power_settings_read") is True,
                "ems_power_settings_valid": live.get("ems_power_settings_valid") is True,
                "ems_max_charge_power_w": live.get("ems_max_charge_power_w"),
                "ems_max_discharge_power_w": live.get("ems_max_discharge_power_w"),
                "ems_discharge_start_power_w": live.get("ems_discharge_start_power_w"),
            },
            plan=plan,
            wb_budget=wb_budget_in,
            wb_intent=wb_intent if wb_intent_fresh else {},
        )
        parallel = payload["parallel"]
        mode = safe_int(parallel.get("mode"), MODE_AUTO)
        val = max(0, safe_int(parallel.get("val"), 0))
        storage_req_w = val if mode in (MODE_CHRG, MODE_GRID) else 0
        budget_w = max(0, pv_after_fixed_w - storage_req_w)
        decision = {
            "state": parallel.get("state", "parallel_auto"),
            "mode": mode,
            "val": val,
            "priority": parallel.get("priority", "default"),
            "reason": parallel.get("reason", "neuer Storage-Regler"),
            "protected": False,
            "storage_req_w": storage_req_w,
            "budget_w": budget_w,
            "shadow_payload": payload,
        }
        shadow_inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
        for key in (
            "headroom_discharge_active",
            "headroom_discharge_w",
            "headroom_discharge_target_w",
            "headroom_discharge_floor_soc",
            "headroom_discharge_gap_pct",
            "headroom_discharge_enter_pct",
            "headroom_discharge_keep_pct",
            "headroom_discharge_export_room_w",
            "headroom_discharge_export_margin_w",
            "headroom_discharge_import_guard_w",
            "headroom_discharge_pressure_wh",
            "headroom_discharge_min_pressure_wh",
            "headroom_discharge_abregel_blocked",
            "headroom_discharge_blocked_reason",
            "headroom_discharge_target_plateau_reached",
            "headroom_discharge_target_curve_soc",
            "headroom_discharge_target_plateau_margin_pct",
            "headroom_discharge_day",
            "headroom_discharge_today_wh",
            "headroom_discharge_daily_limit_wh",
            "headroom_discharge_daily_remaining_wh",
            "headroom_discharge_daily_limit_pct",
            "headroom_discharge_daily_blocked",
            "headroom_discharge_cooldown_s",
            "headroom_discharge_cooldown_remaining_s",
            "headroom_discharge_cooldown_active",
            "headroom_discharge_last_active_ts",
            "headroom_discharge_last_account_ts",
            "headroom_execution_schema_version",
            "headroom_execution_allowed",
            "headroom_execution_reason_code",
            "headroom_execution_plan_id",
            "headroom_execution_slot_id",
            "headroom_execution_earliest_start_ts",
            "headroom_execution_deadline_ts",
            "headroom_execution_target_soc",
            "headroom_execution_hard_floor_soc",
            "headroom_execution_plan_accounted_wh",
            "headroom_execution_slot_accounted_wh",
            "headroom_execution_residual_wh",
            "headroom_execution_accounted_observed_w",
            "headroom_execution_accounted_interval_s",
            "headroom_execution_generation_reset",
            "headroom_execution_last_account_ts",
        ):
            if key in shadow_inputs:
                decision[key] = shadow_inputs.get(key)
        for key in (
            "curve_charge_servo_mode",
            "curve_charge_servo_enabled",
            "curve_charge_servo_active",
            "curve_charge_servo_candidate_state",
            "curve_charge_servo_block_reason",
            "curve_charge_servo_phase",
            "curve_charge_servo_previous_w",
            "curve_charge_servo_target_w",
            "curve_charge_servo_frame_w",
            "curve_charge_servo_available_w",
            "curve_charge_servo_min_w",
            "curve_charge_servo_deadband_w",
            "curve_charge_servo_step_up_w",
            "curve_charge_servo_step_down_w",
            "curve_charge_servo_max_age_s",
        ):
            if key in shadow_inputs:
                decision[key] = shadow_inputs.get(key)
        for key in (
            "curve_cap_release_hysteresis_w",
            "curve_cap_release_grace_s",
            "curve_cap_grid_contract_valid",
            "curve_cap_real_grid_import_active",
            "curve_cap_release_below_active",
            "curve_cap_release_below_since_ts",
            "curve_cap_release_elapsed_s",
            "curve_cap_release_grace_active",
            "curve_cap_release_ramp_active",
            "curve_cap_hysteresis_hold_active",
            "curve_cap_invalid_hold_active",
            "curve_cap_release_phase",
            "curve_cap_release_pending",
            "curve_cap_release_requested",
            "curve_cap_release_confirmed_since_ts",
            "curve_cap_post_release_until_ts",
            "curve_cap_post_release_guard_active",
            "curve_cap_post_release_reentry_blocked",
            "curve_cap_settings_readback_valid",
            "curve_cap_settings_bounded_zero_confirmed",
            "curve_cap_settings_release_confirmed",
            "curve_cap_bounded_zero_w",
        ):
            if key in shadow_inputs:
                decision[key] = shadow_inputs.get(key)
        if str(decision.get("state") or "") == "parallel_headroom_discharge":
            for key in (
                "headroom_discharge_active",
                "headroom_discharge_w",
                "headroom_discharge_target_w",
                "headroom_discharge_floor_soc",
                "headroom_discharge_gap_pct",
                "headroom_discharge_enter_pct",
                "headroom_discharge_keep_pct",
                "headroom_discharge_export_room_w",
                "headroom_discharge_export_margin_w",
                "headroom_discharge_pressure_wh",
                "headroom_discharge_min_pressure_wh",
                "headroom_discharge_abregel_blocked",
                "headroom_discharge_blocked_reason",
                "headroom_discharge_target_plateau_reached",
                "headroom_discharge_target_curve_soc",
                "headroom_discharge_target_plateau_margin_pct",
            ):
                decision[key] = shadow_inputs.get(key)
            decision["headroom_discharge_import_guard_w"] = max(
                0,
                safe_int(
                    shadow_inputs.get(
                        "headroom_discharge_import_guard_w",
                        cfg.get("storage_headroom_discharge_import_guard_w", 150),
                    ),
                    150,
                ),
            )
            decision["abregel_active"] = bool(
                shadow_inputs.get("curve_cap_feedback_active")
                or shadow_inputs.get("curve_cap_hard_pressure_active")
                or shadow_inputs.get("headroom_discharge_abregel_blocked")
            )
        if bool(shadow_inputs.get("forecast_curve_landing_hold_active")):
            decision["forecast_curve_landing_hold_active"] = True
        if bool(shadow_inputs.get("sliding_horizon_active")):
            decision["sliding_horizon_active"] = True
            decision["sliding_horizon_reason"] = str(shadow_inputs.get("sliding_horizon_reason") or "")
            decision["sliding_horizon_confidence"] = safe_float(shadow_inputs.get("sliding_horizon_confidence"), 0.0)
        controlled_wb_auto_freerun = controlled_wallbox_auto_freerun_requested(
            cfg,
            wb_intent,
            wb_mode,
            wallbox_w,
            now_s,
            wb_intent_fresh=wb_intent_fresh,
        )
        if (
            controlled_wb_auto_freerun
            and str(decision.get("state") or "") in {
                "parallel_curve_auto_hold",
                "parallel_curve_auto_no_surplus",
                "parallel_curve_charge",
                "parallel_curve_charge_cap",
            }
            and not bool(decision.get("forecast_curve_landing_hold_active"))
            and not bool(decision.get("sliding_horizon_active"))
            and not bool(decision.get("protected"))
        ):
            decision["controlled_wallbox_auto_freerun"] = True
            decision["controlled_wallbox_auto_original_state"] = str(decision.get("state") or "")
            decision["state"] = "parallel_wb_auto"
            decision["mode"] = MODE_AUTO
            decision["val"] = max_charge_w
            decision["priority"] = "wallbox"
            decision["reason"] = (
                "Wallbox aktiv: E3DC-AUTO frei; "
                "Wallbox Manager regelt Ladeleistung"
            )
            decision["storage_req_w"] = 0
            decision["budget_w"] = max(0, pv_after_fixed_w)
        if (
            str(decision.get("state") or "") in AUTO_LIMIT_STATES
            and (
                auto_limit_heartbeat_enabled(cfg)
                or str(decision.get("state") or "") in AUTO_LIMIT_REQUIRED_STATES
            )
            and not bool(decision.get("protected"))
        ):
            auto_state = str(decision.get("state") or "")
            auto_limit_charge_w = max(0, min(max_charge_w, val))
            auto_limit_discharge_w = max_discharge_w
            auto_limit_reason = "Kurvenladung als E3DC-AUTO mit EMS-Ladegrenze"
            auto_storage_req_w = storage_req_w
            auto_limit_enabled = True
            auto_limit_release = False
            target_wbminsoc_auto_guidance_active = bool(
                wb_mode == MODE_TARGET
                and wb_intent_fresh
                and str(wb_intent.get("battery_request", "") or "").strip().lower() == "allow_discharge"
                and bool(wb_intent.get("wbminsoc_gate_open", False))
            )
            curve_mode_ifc_guidance_active = bool(
                (wb_mode == MODE_CURVE or target_wbminsoc_auto_guidance_active)
                and cfg_bool(cfg, "storage_curve_mode_wallbox_discharge_protect", True)
                and wb_intent_fresh
                and (
                    bool(wb_intent.get("car_active"))
                    or bool(wb_intent.get("connected"))
                    or bool(wb_intent.get("charging_active"))
                    or wallbox_w > 250.0
                )
                and not bool(wb_intent.get("price_opt_active"))
                and not bool(wb_intent.get("scheduled_slot_active"))
                and not bool(wb_intent.get("price_boost_active"))
                and not bool(wb_intent.get("battery_departure_active"))
            )
            if auto_state == "parallel_price_hold":
                auto_limit_charge_w = max_charge_w
                auto_limit_discharge_w = 0
                auto_limit_reason = "Preis-/Slotfenster: E3DC-AUTO mit Entladegrenze 0W"
                auto_storage_req_w = 0
            elif auto_state == "parallel_planned_load_hold":
                auto_limit_charge_w = max_charge_w
                auto_limit_discharge_w = 0
                shadow_inputs = decision.get("shadow_payload", {}).get("inputs", {}) if isinstance(decision.get("shadow_payload"), dict) else {}
                expected_w = max(0, safe_int(shadow_inputs.get("planned_load_expected_w"), active_state.get("planned_load_expected_w", 0)))
                observed_w = max(0, safe_int(shadow_inputs.get("planned_load_observed_extra_w"), active_state.get("planned_load_observed_extra_w", 0)))
                auto_limit_reason = (
                    "Geplante externe Last: E3DC-AUTO mit Entladegrenze 0W "
                    f"(erwartet {expected_w}W, erkannt {observed_w}W)"
                )
                auto_storage_req_w = 0
                decision["planned_load_expected_w"] = expected_w
                decision["planned_load_observed_extra_w"] = observed_w
                decision["planned_load_windows"] = active_state.get("planned_load_windows", [])
                decision["planned_load_names"] = [
                    str(w.get("name") or "geplante Last")
                    for w in (active_state.get("planned_load_windows") or [])
                    if isinstance(w, dict)
                ]
            elif auto_state == "parallel_planned_load_price_support":
                auto_limit_charge_w = max_charge_w
                shadow_inputs = decision.get("shadow_payload", {}).get("inputs", {}) if isinstance(decision.get("shadow_payload"), dict) else {}
                expected_w = max(0, safe_int(shadow_inputs.get("planned_load_expected_w"), active_state.get("planned_load_expected_w", 0)))
                observed_w = max(0, safe_int(shadow_inputs.get("planned_load_observed_extra_w"), active_state.get("planned_load_observed_extra_w", 0)))
                support = active_state.get("planned_load_support", {})
                if not isinstance(support, dict):
                    support = {}
                support_w = max(0, safe_int(support.get("support_max_discharge_w"), val))
                auto_limit_discharge_w = max(0, min(max_discharge_w, support_w))
                auto_limit_reason = (
                    "Geplante externe Last preisgeführt stützen: "
                    f"Entladung bis {auto_limit_discharge_w}W erlaubt "
                    f"(erwartet {expected_w}W, erkannt {observed_w}W)"
                )
                auto_storage_req_w = 0
                decision["planned_load_expected_w"] = expected_w
                decision["planned_load_observed_extra_w"] = observed_w
                decision["planned_load_windows"] = active_state.get("planned_load_windows", [])
                decision["planned_load_names"] = [
                    str(w.get("name") or "geplante Last")
                    for w in (active_state.get("planned_load_windows") or [])
                    if isinstance(w, dict)
                ]
                decision["planned_load_support"] = support
            elif auto_state == "parallel_price_house_discharge":
                auto_limit_charge_w = max_charge_w
                auto_limit_discharge_w = max(0, min(max_discharge_w, val))
                auto_limit_reason = "Preis-/Slotfenster: E3DC-AUTO mit begrenzter Hausstuetze"
                auto_storage_req_w = 0
            elif auto_state == "parallel_curve_auto_hold":
                auto_limit_charge_w = 0
                shadow_inputs = decision.get("shadow_payload", {}).get("inputs", {}) if isinstance(decision.get("shadow_payload"), dict) else {}
                if bool(shadow_inputs.get("pre_curve_hold_active")):
                    auto_limit_reason = "Vor Kurvenstart: E3DC-AUTO mit Ladegrenze 0W"
                elif bool(shadow_inputs.get("forecast_curve_landing_hold_active")):
                    auto_limit_reason = "Prognose 100%: Sollkurve erreicht, E3DC-AUTO mit Ladegrenze 0W"
                elif bool(shadow_inputs.get("sliding_horizon_active")):
                    auto_limit_reason = (
                        "Gleitender Prognosehorizont: Rest-PV reicht bis zum Tagesziel; "
                        "E3DC-AUTO mit Ladegrenze 0W bis zum spätesten Ladebeginn"
                    )
                elif bool(shadow_inputs.get("headroom_reserve_active")):
                    reserve_wh = safe_int(shadow_inputs.get("headroom_reserve_pressure_wh"), 0)
                    auto_limit_reason = (
                        "Abregelreserve aktiv: E3DC-AUTO mit Ladegrenze 0W, "
                        f"{reserve_wh}Wh Speicherplatz fuer PV-Spitzen freihalten"
                    )
                elif (
                    bool(shadow_inputs.get("curve_settle_hold_active"))
                    or bool(shadow_inputs.get("curve_crossed_from_charge_hold"))
                    or bool(shadow_inputs.get("curve_near_idle_hold"))
                ):
                    auto_limit_reason = "Kurvenkante stabil: E3DC-AUTO mit Ladegrenze 0W bis zur unteren Hysterese"
                else:
                    auto_limit_reason = "SoC oberhalb Kurve: E3DC-AUTO mit Ladegrenze 0W"
                auto_storage_req_w = 0
                decision["val"] = 0
                hold_offer_threshold_w = max(
                    300,
                    safe_int(
                        shadow_inputs.get("curve_charge_keep_w"),
                        safe_int(cfg.get("storage_parallel_curve_charge_keep_w"), 120),
                    ),
                )
                hold_offer_w = max(
                    0,
                    safe_int(shadow_inputs.get("curve_safe_charge_w"), 0),
                    safe_int(shadow_inputs.get("grid_export_w"), max(0, -grid_w, -grid_ema_w)),
                    safe_int(shadow_inputs.get("pv_after_fixed_w"), pv_after_fixed_w),
                    safe_int(shadow_inputs.get("curve_cap_pressure_w"), 0),
                    max(0, safe_int(bat_w, 0)),
                )
                hold_pressure_active = bool(
                    shadow_inputs.get("curve_cap_hard_pressure_active")
                    or shadow_inputs.get("curve_cap_feedback_active")
                    or shadow_inputs.get("curve_cap_dc_pressure_active")
                    or shadow_inputs.get("curve_cap_active")
                )
                hold_offer_active = bool(
                    hold_offer_w >= hold_offer_threshold_w
                    or hold_pressure_active
                )
                decision["curve_auto_hold_charge_offer_w"] = hold_offer_w
                decision["curve_auto_hold_charge_offer_threshold_w"] = hold_offer_threshold_w
                decision["curve_auto_hold_charge_offer_active"] = hold_offer_active
                next_curve_evening_release = next_curve_evening_pv_release_context(
                    cfg,
                    shadow_inputs,
                    now_s=now_s,
                    soc=soc,
                    pv_w=pv_w,
                    grid_w=grid_w,
                    grid_ema_w=grid_ema_w,
                    pv_after_fixed_w=pv_after_fixed_w,
                    offer_w=hold_offer_w,
                    offer_threshold_w=hold_offer_threshold_w,
                    max_charge_w=max_charge_w,
                )
                if bool(next_curve_evening_release.get("active")):
                    auto_limit_charge_w = max_charge_w
                    auto_limit_enabled = False
                    auto_limit_release = True
                    auto_limit_reason = (
                        "Morgenkurve beginnt erst in "
                        f"{safe_int(next_curve_evening_release.get('seconds_to_first_curve'), 0) // 60} min: "
                        "EMS-Grenzen frei, E3DC darf heutige Rest-PV autonom mitnehmen"
                    )
                    auto_storage_req_w = 0
                    decision["val"] = max_charge_w
                    decision["next_curve_evening_pv_release_active"] = True
                    decision["next_curve_evening_pv_release_seconds_to_first_curve"] = safe_int(
                        next_curve_evening_release.get("seconds_to_first_curve"),
                        0,
                    )
                    decision["next_curve_evening_pv_release_offer_w"] = safe_int(
                        next_curve_evening_release.get("offer_w"),
                        0,
                    )
                    decision["next_curve_evening_pv_release_max_lead_s"] = safe_int(
                        next_curve_evening_release.get("max_lead_s"),
                        0,
                    )
                elif not hold_offer_active:
                    auto_limit_charge_w = max_charge_w
                    auto_limit_enabled = False
                    auto_limit_release = True
                    auto_limit_reason = (
                        "Kurven-Hold ohne realen Ladepfad: EMS-Grenzen frei, "
                        "E3DC bleibt autonom bis PV-/Exportdruck entsteht"
                    )
                    decision["val"] = max_charge_w
                else:
                    continuation = curve_auto_hold_continuation_frame(
                        cfg,
                        previous_state,
                        shadow_inputs,
                        max_charge_w,
                    )
                    if bool(continuation.get("active")):
                        auto_limit_charge_w = max(0, safe_int(continuation.get("frame_w"), auto_limit_charge_w))
                        auto_storage_req_w = max(
                            auto_storage_req_w,
                            min(auto_limit_charge_w, max(0, pv_after_fixed_w)),
                        )
                        decision["val"] = auto_limit_charge_w
                        decision["curve_auto_hold_continuation_active"] = True
                        decision["curve_auto_hold_continuation_w"] = auto_limit_charge_w
                        decision["curve_auto_hold_continuation_previous_w"] = max(
                            0,
                            safe_int(continuation.get("previous_limit_w"), 0),
                        )
                        decision["curve_auto_hold_continuation_decay_w"] = max(
                            0,
                            safe_int(continuation.get("decay_w"), 0),
                        )
                        decision["curve_auto_hold_continuation_offer_w"] = max(
                            0,
                            safe_int(continuation.get("offer_w"), 0),
                        )
                        auto_limit_reason = (
                            f"Kurven-Hold stabilisiert: Laderahmen gleitet auf {auto_limit_charge_w}W "
                            f"statt auf 0W zu springen (vorher "
                            f"{safe_int(continuation.get('previous_limit_w'), 0)}W)"
                        )
            elif auto_state == "parallel_curve_auto_no_surplus":
                seconds_to_release = release_ts_s - now_s if release_ts_s > 0 else 999999.0
                late_autonomy_s = max(0, safe_int(cfg.get("storage_curve_late_autonomy_s"), 3600))
                late_shortfall_autonomy = bool(
                    release_ts_s > 0
                    and 0 <= seconds_to_release <= late_autonomy_s
                    and i_fc_w >= safe_int(cfg.get("storage_parallel_curve_charge_keep_w"), 120)
                    and curve_gap_pct > 0.0
                    and pv_w > 250
                )
                if late_shortfall_autonomy:
                    auto_limit_charge_w = max_charge_w
                    auto_limit_enabled = False
                    auto_limit_release = True
                    auto_limit_reason = (
                        "Später Kurvenrückstand ohne sichere Exportreserve: "
                        "EMS-Grenzen frei, E3DC darf Rest-PV autonom mitnehmen"
                    )
                    auto_storage_req_w = 0
                    decision["late_shortfall_auto_release"] = True
                    decision["late_shortfall_seconds_to_release"] = max(0, int(seconds_to_release))
                else:
                    auto_limit_charge_w = 0
                    auto_limit_reason = "Kurve ohne sichere Exportreserve: E3DC-AUTO mit Ladegrenze 0W"
                    auto_storage_req_w = 0
                    decision["val"] = 0
            elif auto_state == "parallel_curve_charge":
                shadow_inputs = decision.get("shadow_payload", {}).get("inputs", {}) if isinstance(decision.get("shadow_payload"), dict) else {}
                if bool(shadow_inputs.get("curve_charge_servo_active")):
                    auto_limit_reason = "Kurven-Servo: E3DC-AUTO mit ruhigem Laderahmen"
                elif bool(shadow_inputs.get("curve_edge_soft_charge_active")):
                    auto_limit_reason = "Kurvenkante weich geführt: E3DC-AUTO mit gedämpfter Ladegrenze"
                elif bool(shadow_inputs.get("curve_frame_base_smoothing_active")):
                    auto_limit_reason = "Kurvenladung ruhig geführt: E3DC-AUTO mit geglättetem Laderahmen"
                elif bool(shadow_inputs.get("pre_curve_ifc_start_active")):
                    auto_limit_reason = "Vor Kurvenstart: hoher iFc startet die Kurvenladung bewusst früher"
                elif bool(shadow_inputs.get("shortfall_pv_catchup_active")):
                    auto_limit_reason = "Abendziel-Rückstand: reale PV-Einspeisung wird trotz Komfortband gespeichert"
                elif bool(shadow_inputs.get("curve_soft_charge_active")):
                    auto_limit_reason = "SoC knapp oberhalb Kurve: E3DC-AUTO mit gedämpfter Ladegrenze"
                else:
                    auto_limit_reason = "SoC liegt nahe oder unter dem Zielkorridor: E3DC-AUTO lädt mit berechneter Ladegrenze"
                hard_anchor_need_w = max(
                    safe_int(shadow_inputs.get("curve_hard_anchor_need_w"), 0),
                    safe_int(active_state.get("curve_hard_anchor_need_w"), 0),
                )
                hard_anchor_due = (
                    bool(shadow_inputs.get("curve_hard_anchor_missed"))
                    or bool(active_state.get("curve_hard_anchor_missed"))
                    or hard_anchor_need_w > 0
                )
                curve_followup_gap_pct = max(
                    0.0,
                    safe_float(
                        shadow_inputs.get("curve_gap_pct"),
                        active_state.get("curve_gap_pct", 0.0),
                    ),
                )
                curve_followup_min_gap_pct = max(
                    0.0,
                    safe_float(cfg.get("storage_curve_frame_lift_gap_pct"), 0.75),
                )
                previous_curve_charge = str(previous_state.get("state") or "") == "parallel_curve_charge" or str(
                    previous_state.get("parallel_state") or ""
                ) == "parallel_curve_charge"
                curve_gap_followup_due = bool(
                    previous_curve_charge and curve_followup_gap_pct >= curve_followup_min_gap_pct
                )
                curve_frame_deadband_w = max(
                    50,
                    safe_int(cfg.get("storage_curve_frame_lift_deadband_w"), 80),
                )
                curve_frame_trim_soc_hysteresis_pct = max(
                    0.1,
                    safe_float(cfg.get("storage_curve_frame_measured_trim_soc_hysteresis_pct"), 0.5),
                )
                curve_frame_trim_settle_s = max(
                    7.0,
                    safe_float(cfg.get("storage_curve_frame_measured_trim_settle_s"), 24.0),
                )
                curve_followup_desired_w = max(auto_storage_req_w, i_fc_w, hard_anchor_need_w)
                previous_measured_trim_active = bool(
                    previous_curve_charge
                    and previous_state.get("curve_frame_lift_active")
                    and str(previous_state.get("curve_frame_lift_reason") or "") == "measured_trim"
                    and max(0, safe_int(previous_state.get("curve_frame_measured_trim_offset_w"), 0)) > 0
                )
                previous_curve_since_ts = safe_float(previous_state.get("parallel_state_since_ts"), 0.0)
                if previous_curve_since_ts <= 0.0 and previous_curve_charge:
                    previous_curve_since_ts = safe_float(previous_state.get("ts"), 0.0)
                previous_curve_age_s = max(0.0, now_s - previous_curve_since_ts) if previous_curve_since_ts > 0.0 else 0.0
                curve_measured_sample_ready = bool(
                    previous_measured_trim_active
                    or previous_curve_age_s >= curve_frame_trim_settle_s
                )
                curve_measured_hold_due = bool(
                    previous_measured_trim_active
                    and curve_followup_desired_w > 0
                    and curve_followup_gap_pct >= -curve_frame_trim_soc_hysteresis_pct
                )
                curve_measured_followup_due = bool(
                    previous_curve_charge
                    and curve_measured_sample_ready
                    and curve_followup_desired_w > 0
                    and max(0, bat_w) + curve_frame_deadband_w < curve_followup_desired_w
                )
                if hard_anchor_due or curve_gap_followup_due or curve_measured_followup_due or curve_measured_hold_due:
                    followup = curve_charge_frame_followup(
                        cfg,
                        previous_state,
                        max(0, bat_w),
                        curve_followup_desired_w,
                        auto_limit_charge_w,
                        pv_after_fixed_w,
                        grid_w,
                        max_charge_w,
                        gentle_measured_trim=bool(
                            (curve_measured_followup_due or curve_measured_hold_due)
                            and not hard_anchor_due
                            and not curve_gap_followup_due
                        ),
                        now_s=now_s,
                    )
                    if bool(followup.get("active")):
                        auto_limit_charge_w = max(0, safe_int(followup.get("frame_w"), auto_limit_charge_w))
                        auto_storage_req_w = max(
                            auto_storage_req_w,
                            min(auto_limit_charge_w, max(0, pv_after_fixed_w)),
                        )
                        decision["val"] = auto_limit_charge_w
                        decision["curve_frame_lift_active"] = True
                        decision["curve_frame_lift_w"] = auto_limit_charge_w
                        decision["curve_frame_lift_desired_w"] = max(0, safe_int(followup.get("desired_w"), 0))
                        decision["curve_frame_lift_actual_w"] = max(0, safe_int(followup.get("actual_charge_w"), 0))
                        decision["curve_frame_lift_shortfall_w"] = max(0, safe_int(followup.get("shortfall_w"), 0))
                        decision["curve_frame_lift_step_w"] = max(0, safe_int(followup.get("step_w"), 0))
                        decision["curve_frame_lift_max_boost_w"] = max(0, safe_int(followup.get("max_boost_w"), 0))
                        decision["curve_frame_measured_trim_phase"] = str(followup.get("gentle_trim_phase") or "")
                        decision["curve_frame_measured_trim_offset_w"] = max(
                            0,
                            safe_int(followup.get("measured_trim_offset_w"), 0),
                        )
                        decision["curve_frame_measured_trim_anchor_ts"] = max(
                            0,
                            safe_int(followup.get("measured_trim_anchor_ts"), 0),
                        )
                        decision["curve_frame_measured_trim_hold_until_ts"] = max(
                            0,
                            safe_int(followup.get("measured_trim_hold_until_ts"), 0),
                        )
                        if hard_anchor_due:
                            decision["curve_frame_lift_reason"] = "hard_anchor"
                        elif curve_gap_followup_due:
                            decision["curve_frame_lift_reason"] = "curve_gap"
                        elif bool(followup.get("gentle_trim")):
                            decision["curve_frame_lift_reason"] = "measured_trim"
                        else:
                            decision["curve_frame_lift_reason"] = "measured_delta"
                        decision["curve_frame_lift_gap_pct"] = round(curve_followup_gap_pct, 3)
                        if bool(followup.get("gentle_trim")):
                            auto_limit_reason = (
                                f"{auto_limit_reason}; Rahmen fein korrigiert auf {auto_limit_charge_w}W "
                                f"(E3DC lädt {safe_int(followup.get('actual_charge_w'), 0)}W)"
                            )
                        else:
                            auto_limit_reason = (
                                f"{auto_limit_reason}; Rahmen nachgeführt auf {auto_limit_charge_w}W "
                                f"(E3DC lädt nur {safe_int(followup.get('actual_charge_w'), 0)}W)"
                            )
                if not bool(decision.get("curve_frame_lift_active")):
                    smoothing = curve_charge_frame_smoothing(
                        cfg,
                        previous_state,
                        max(0, bat_w),
                        max(auto_storage_req_w, i_fc_w),
                        auto_limit_charge_w,
                        pv_after_fixed_w,
                        grid_w,
                        max_charge_w,
                        curve_followup_gap_pct,
                    )
                    if bool(smoothing.get("active")):
                        auto_limit_charge_w = max(0, safe_int(smoothing.get("frame_w"), auto_limit_charge_w))
                        auto_storage_req_w = max(
                            auto_storage_req_w,
                            min(auto_limit_charge_w, max(0, pv_after_fixed_w)),
                        )
                        decision["val"] = auto_limit_charge_w
                        decision["curve_frame_lift_active"] = True
                        decision["curve_frame_lift_w"] = auto_limit_charge_w
                        decision["curve_frame_lift_desired_w"] = max(0, safe_int(smoothing.get("desired_w"), 0))
                        decision["curve_frame_lift_actual_w"] = max(0, safe_int(smoothing.get("actual_charge_w"), 0))
                        decision["curve_frame_lift_shortfall_w"] = max(0, safe_int(smoothing.get("shortfall_w"), 0))
                        decision["curve_frame_lift_step_w"] = max(0, safe_int(smoothing.get("step_w"), 0))
                        decision["curve_frame_lift_max_boost_w"] = max(
                            0,
                            safe_int(previous_state.get("curve_frame_lift_max_boost_w"), 0),
                        )
                        decision["curve_frame_measured_trim_phase"] = ""
                        decision["curve_frame_measured_trim_offset_w"] = 0
                        decision["curve_frame_measured_trim_anchor_ts"] = 0
                        decision["curve_frame_measured_trim_hold_until_ts"] = 0
                        decision["curve_frame_lift_reason"] = "smooth_" + str(smoothing.get("phase") or "hold")
                        decision["curve_frame_lift_gap_pct"] = round(curve_followup_gap_pct, 3)
                        auto_limit_reason = (
                            f"{auto_limit_reason}; Laderahmen geglättet auf {auto_limit_charge_w}W "
                            f"(vorher {safe_int(smoothing.get('previous_limit_w'), 0)}W, "
                            f"E3DC lädt {safe_int(smoothing.get('actual_charge_w'), 0)}W)"
                        )
            elif auto_state == "parallel_curve_charge_cap":
                release_requested = bool(
                    shadow_inputs.get("curve_cap_release_requested")
                )
                if release_requested:
                    auto_limit_charge_w = max_charge_w
                    auto_limit_enabled = False
                    auto_limit_release = True
                    auto_storage_req_w = 0
                    decision["val"] = 0
                    auto_limit_reason = (
                        "Abregel-/WR-Pflichtladung: physischer Rampenboden bestätigt; "
                        "EMS-Grenzen readback-gebunden freigeben"
                    )
                else:
                    auto_limit_reason = "Abregel-/WR-Pflichtladung: E3DC-AUTO mit EMS-Ladegrenze"
            elif auto_state == "parallel_wb_auto":
                auto_limit_charge_w = max_charge_w
                auto_limit_enabled = False
                auto_limit_release = True
                shadow_inputs = decision.get("shadow_payload", {}).get("inputs", {}) if isinstance(decision.get("shadow_payload"), dict) else {}
                curve_i_fc_w = max(0, safe_int(shadow_inputs.get("iFc_w"), i_fc_w))
                curve_gap_input_pct = safe_float(shadow_inputs.get("curve_gap_pct"), curve_gap_pct)
                curve_relation = str(shadow_inputs.get("adaptive_curve_relation") or adaptive_curve_relation or "")
                curve_charge_enter_w = max(
                    0,
                    safe_int(
                        shadow_inputs.get("curve_charge_enter_w"),
                        safe_int(cfg.get("storage_parallel_curve_charge_enter_w"), 300),
                    ),
                )
                target_corridor_catchup_active = bool(
                    curve_mode_ifc_guidance_active
                    and curve_i_fc_w >= curve_charge_enter_w
                    and (
                        curve_relation in ("below_floor", "no_curve")
                        or curve_gap_input_pct > 0.0
                    )
                )
                curve_wb_reserve_w = 0
                curve_wb_reserve = {}
                reserve_shadow_inputs = shadow_inputs
                if target_corridor_catchup_active:
                    reserve_shadow_inputs = dict(shadow_inputs)
                    reserve_shadow_inputs["curve_guard_active"] = True
                    reserve_shadow_inputs["curve_gap_pct"] = max(
                        curve_gap_input_pct,
                        safe_float(reserve_shadow_inputs.get("curve_gap_pct"), 0.0),
                        0.01,
                    )
                if curve_mode_ifc_guidance_active:
                    curve_wb_reserve = wallbox_curve_reserve_budget(
                        cfg,
                        wb_intent if wb_intent_fresh else {},
                        wb_native,
                        previous_state,
                        wallbox_w,
                        pv_after_fixed_w,
                        max(0, safe_int(shadow_inputs.get("iFc_w"), i_fc_w)),
                        max_charge_w,
                        reserve_shadow_inputs,
                    )
                    curve_wb_reserve_w = max(0, safe_int(curve_wb_reserve.get("reserve_w"), 0))
                if curve_wb_reserve_w <= 0 and wallbox_w > 250.0 and bool(shadow_inputs.get("curve_guard_active")):
                    curve_safe_charge_w = max(0, safe_int(shadow_inputs.get("curve_safe_charge_w"), 0))
                    if curve_i_fc_w >= curve_charge_enter_w and curve_safe_charge_w >= curve_charge_enter_w:
                        curve_wb_reserve_w = max(
                            curve_charge_enter_w,
                            min(curve_i_fc_w, curve_safe_charge_w, max_charge_w),
                        )
                if target_corridor_catchup_active:
                    target_corridor_req_w = max(0, min(max_charge_w, curve_i_fc_w))
                    target_corridor_reserve_w = max(
                        curve_wb_reserve_w,
                        max(0, safe_int(curve_wb_reserve.get("target_w"), 0)) if curve_wb_reserve else 0,
                    )
                    if target_corridor_req_w > 0:
                        target_corridor_reserve_w = min(target_corridor_req_w, target_corridor_reserve_w)
                    wallbox_first_reserve_active = bool(
                        not external_wallbox_manager_active
                        and target_corridor_req_w > 0
                        and target_corridor_reserve_w > 0
                        and wallbox_w > 250.0
                    )
                    auto_limit_charge_w = max_charge_w
                    auto_storage_req_w = target_corridor_req_w
                    decision["val"] = max_charge_w
                    decision["target_corridor_fast_charge_active"] = True
                    decision["target_corridor_storage_req_w"] = target_corridor_req_w
                    auto_limit_reason = (
                        "Wallbox-AUTO: Zielkorridor unterschritten; "
                        "E3DC-AUTO-Rahmen frei, iFc wird über Wallboxreserve geführt"
                    )
                    if wallbox_first_reserve_active:
                        decision["wallbox_curve_reserve_w"] = target_corridor_reserve_w
                        decision["wallbox_curve_reserve_target_w"] = target_corridor_reserve_w
                        decision["wallbox_curve_reserve_step_w"] = max(
                            target_corridor_reserve_w,
                            max(0, safe_int(curve_wb_reserve.get("step_w"), 0)) if curve_wb_reserve else 0,
                        )
                        if curve_wb_reserve:
                            decision["wallbox_curve_min_w"] = max(0, safe_int(curve_wb_reserve.get("min_wallbox_w"), 0))
                            decision["wallbox_curve_phases"] = max(0, safe_int(curve_wb_reserve.get("phases"), 0))
                            decision["wallbox_curve_export_catchup_active"] = bool(curve_wb_reserve.get("export_catchup_active"))
                            decision["wallbox_curve_export_catchup_w"] = max(0, safe_int(curve_wb_reserve.get("export_catchup_w"), 0))
                        decision["raw_iAVal_w"] = safe_int(
                            pv_after_fixed_w - target_corridor_reserve_w,
                            pv_after_fixed_w,
                        )
                    if external_wallbox_manager_active:
                        auto_limit_enabled = True
                        auto_limit_release = False
                        auto_limit_charge_w = target_corridor_req_w
                        auto_limit_discharge_w = house_heatpump_discharge_cap_w(
                            live_with_wallbox,
                            wallbox_w,
                            max_discharge_w,
                        )
                        auto_limit_discharge_w = smooth_house_heatpump_discharge_cap_w(
                            cfg,
                            auto_limit_discharge_w,
                            previous_state,
                        )
                        decision["external_wallbox_curve_storage_protect"] = True
                        decision["house_heatpump_discharge_cap_w"] = auto_limit_discharge_w
                        auto_limit_reason += (
                            "; externe Wallbox beobachtet, Speicher folgt iFc direkt"
                        )
                    elif not wallbox_first_reserve_active:
                        auto_limit_enabled = True
                        auto_limit_release = False
                        auto_limit_charge_w = target_corridor_req_w
                        auto_limit_reason += (
                            "; Wallboxleistung reicht nicht, Speicher folgt iFc direkt"
                        )
                    else:
                        auto_limit_enabled = False
                        auto_limit_release = True
                elif curve_wb_reserve_w > 0:
                    auto_limit_charge_w = max(
                        0,
                        min(max_charge_w, curve_wb_reserve_w),
                    )
                    auto_limit_enabled = True
                    auto_limit_release = False
                    if external_wallbox_manager_active:
                        auto_limit_discharge_w = house_heatpump_discharge_cap_w(
                            live_with_wallbox,
                            wallbox_w,
                            max_discharge_w,
                        )
                        auto_limit_discharge_w = smooth_house_heatpump_discharge_cap_w(
                            cfg,
                            auto_limit_discharge_w,
                            previous_state,
                        )
                        decision["wallbox_curve_discharge_protect"] = True
                        decision["house_heatpump_discharge_cap_w"] = auto_limit_discharge_w
                    elif curve_mode_ifc_guidance_active:
                        auto_limit_discharge_w = max_discharge_w
                    if external_wallbox_manager_active:
                        decision["external_wallbox_curve_storage_protect"] = True
                    decision["val"] = auto_limit_charge_w
                    auto_limit_reason = (
                        "Wallbox-AUTO: Speicher bleibt auf iFc-Führung begrenzt"
                    )
                    if external_wallbox_manager_active:
                        auto_limit_reason += (
                            "; Ladekurve hat Vorrang, Entladegrenze Haus/WP-Defizit "
                            f"{auto_limit_discharge_w}W"
                        )
                    elif curve_mode_ifc_guidance_active:
                        auto_limit_reason += (
                            "; PV-Kurve ruhig: Entladeleistung frei, "
                            "Wallbox regelt Ladeleistung"
                        )
                    auto_storage_req_w = auto_limit_charge_w
                    decision["wallbox_curve_reserve_w"] = curve_wb_reserve_w
                    if curve_wb_reserve:
                        decision["wallbox_curve_reserve_target_w"] = max(0, safe_int(curve_wb_reserve.get("target_w"), 0))
                        decision["wallbox_curve_reserve_step_w"] = max(0, safe_int(curve_wb_reserve.get("step_w"), 0))
                        decision["wallbox_curve_min_w"] = max(0, safe_int(curve_wb_reserve.get("min_wallbox_w"), 0))
                        decision["wallbox_curve_phases"] = max(0, safe_int(curve_wb_reserve.get("phases"), 0))
                        decision["wallbox_curve_export_catchup_active"] = bool(curve_wb_reserve.get("export_catchup_active"))
                        decision["wallbox_curve_export_catchup_w"] = max(0, safe_int(curve_wb_reserve.get("export_catchup_w"), 0))
                        decision["raw_iAVal_w"] = safe_int(curve_wb_reserve.get("raw_iaval_w"), pv_after_fixed_w)
                elif curve_mode_ifc_guidance_active and not target_wbminsoc_auto_guidance_active:
                    auto_limit_charge_w = 0
                    auto_limit_enabled = True
                    auto_limit_release = False
                    auto_limit_discharge_w = max_discharge_w
                    decision["val"] = 0
                    auto_limit_reason = (
                        "Wallbox-AUTO: PV-Kurve ruhig, Speicher folgt iFc=0; "
                        "Entladeleistung frei, Wallbox regelt Ladeleistung"
                    )
                    auto_storage_req_w = 0
                elif target_wbminsoc_auto_guidance_active:
                    auto_limit_reason = (
                        "Wallbox-AUTO: Ziel-wbminSoC oberhalb Untergrenze, "
                        "Wallbox regelt netzneutral; E3DC-Ladegrenzen frei"
                    )
                    auto_storage_req_w = 0
                else:
                    auto_limit_reason = "Wallbox-AUTO: Wallbox regelt Fahrstrom, E3DC-Ladegrenzen frei"
                    auto_storage_req_w = 0

            if (
                observe_reserve_release_active
                and auto_state in OBSERVE_RESERVE_RELEASE_AUTO_STATES
            ):
                auto_limit_discharge_w = max_discharge_w
                decision["observe_wallbox_storage_policy"] = "reserve"
                decision["observe_wallbox_reserve_release_active"] = True
                decision["observe_wallbox_reserve_floor_soc"] = observe_reserve_release.get("floor_soc")
                decision["observe_wallbox_reserve_release_floor_soc"] = observe_reserve_release.get("release_floor_soc")
                decision["observe_wallbox_reserve_soc"] = observe_reserve_release.get("soc")
                auto_limit_reason = (
                    f"{auto_limit_reason}; Beobachten PV+Akku: Entladung bis Hausreserve "
                    f"{safe_float(observe_reserve_release.get('floor_soc'), 0.0):.1f}% frei"
                )

            if (
                curve_mode_ifc_guidance_active
                and auto_state in {
                    "parallel_curve_auto_hold",
                    "parallel_curve_auto_no_surplus",
                    "parallel_curve_charge",
                    "parallel_curve_charge_cap",
                }
            ):
                if auto_limit_release:
                    auto_limit_enabled = True
                    auto_limit_release = False
                auto_limit_discharge_w = max_discharge_w
                auto_limit_reason = (
                    f"{auto_limit_reason}; PV-Kurve ruhig: Entladeleistung frei, "
                    "Wallbox regelt Ladeleistung"
                )
                if wallbox_w > 250.0:
                    protect_inputs = (
                        decision.get("shadow_payload", {}).get("inputs", {})
                        if isinstance(decision.get("shadow_payload"), dict)
                        else {}
                    )
                    protect_ifc_w = max(
                        0,
                        safe_int(protect_inputs.get("iFc_w"), i_fc_w),
                    )
                    if protect_ifc_w > 0:
                        protect_phases = max(
                            1,
                            min(3, safe_int(wb_intent.get("detected_phases"), 3)),
                        )
                        ifc_charge_limit_w = max(0, min(max_charge_w, protect_ifc_w))
                        if ifc_charge_limit_w > auto_limit_charge_w:
                            auto_limit_charge_w = ifc_charge_limit_w
                            auto_storage_req_w = max(auto_storage_req_w, ifc_charge_limit_w)
                            decision["val"] = auto_limit_charge_w
                        decision["wallbox_curve_reserve_w"] = max(
                            protect_ifc_w,
                            safe_int(decision.get("wallbox_curve_reserve_w"), 0),
                        )
                        decision["wallbox_curve_reserve_target_w"] = max(
                            protect_ifc_w,
                            safe_int(decision.get("wallbox_curve_reserve_target_w"), 0),
                        )
                        decision["wallbox_curve_reserve_step_w"] = max(
                            230,
                            safe_int(
                                cfg.get("storage_wb_curve_reserve_step_w"),
                                230 * protect_phases,
                            ),
                        )

            if (
                auto_state in {"parallel_curve_auto_hold", "parallel_curve_auto_no_surplus", "parallel_curve_charge"}
                and auto_limit_enabled
                and not auto_limit_release
            ):
                release_opening = curve_release_opening(
                    plan,
                    now_s,
                    cfg,
                    max_charge_w,
                    auto_limit_charge_w,
                )
                if bool(release_opening.get("active")):
                    opened_limit_w = max(0, safe_int(release_opening.get("opened_limit_w"), auto_limit_charge_w))
                    if opened_limit_w > auto_limit_charge_w:
                        auto_limit_charge_w = opened_limit_w
                        auto_storage_req_w = max(
                            auto_storage_req_w,
                            min(auto_limit_charge_w, max(0, pv_after_fixed_w)),
                        )
                        decision["val"] = auto_limit_charge_w
                        decision["curve_release_opening_active"] = True
                        decision["curve_release_opening_w"] = auto_limit_charge_w
                        decision["curve_release_opening_progress"] = release_opening.get("progress")
                        decision["curve_release_opening_seconds"] = release_opening.get("seconds_to_release")
                        decision["curve_release_opening_window_s"] = release_opening.get("window_s")
                        decision["curve_release_opening_base_w"] = release_opening.get("base_limit_w")
                        auto_limit_reason = (
                            f"{auto_limit_reason}; Freilauf-Öffnung: "
                            f"EMS-Ladegrenze steigt weich auf {auto_limit_charge_w}W"
                        )

            reservation_cap = direct_marketing_curve_charge_reservation_cap(
                cfg,
                plan,
                now_s,
                auto_state=auto_state,
                current_limit_w=auto_limit_charge_w,
                current_release=auto_limit_release,
                max_charge_w=max_charge_w,
                pv_after_fixed_w=pv_after_fixed_w,
                grid_w=grid_w,
            )
            if reservation_cap.get("active"):
                future_store_reservation = reservation_cap.get("reservation") or {}
                auto_limit_enabled = True
                auto_limit_release = False
                auto_limit_charge_w = max(0, safe_int(reservation_cap.get("max_charge_w"), 0))
                auto_storage_req_w = min(auto_storage_req_w, auto_limit_charge_w)
                decision["val"] = auto_limit_charge_w
                decision["direct_marketing_future_pv_store_reservation"] = future_store_reservation
                decision["direct_marketing_future_pv_store_reservation_active"] = True
                decision["direct_marketing_future_pv_store_reservation_cap_w"] = auto_limit_charge_w
                decision["direct_marketing_future_pv_store_reservation_grid_import_w"] = reservation_cap.get("grid_import_w")
                decision["direct_marketing_future_pv_store_reservation_import_guard_w"] = reservation_cap.get("import_guard_w")
                reservation_reason = str(future_store_reservation.get("reason") or "")
                if reservation_reason == "reserve_recovery":
                    reservation_text = "Kurvenladung wegen harter Reserve zugelassen"
                elif reservation_reason == "future_window_energy_insufficient":
                    reservation_text = "Kurvenladung wegen Prognosedefizit zugelassen"
                else:
                    reservation_text = "Kurvenladung für kommendes DV-PV-Fenster zurückgestellt"
                auto_limit_reason = f"{reservation_text}: Ladegrenze {auto_limit_charge_w}W"

            if auto_limit_enabled and 0 < auto_limit_charge_w < 300:
                auto_limit_charge_w = 0
                if auto_state in {"parallel_curve_auto_hold", "parallel_curve_auto_no_surplus", "parallel_curve_charge"}:
                    auto_storage_req_w = 0
                    decision["val"] = 0
                    if safe_int(decision.get("curve_release_opening_w"), 0) < 300:
                        decision["curve_release_opening_w"] = 0
            decision["auto_limit"] = {
                "enabled": auto_limit_enabled,
                "release": auto_limit_release,
                "max_charge_w": auto_limit_charge_w,
                "max_discharge_w": auto_limit_discharge_w,
                "discharge_start_w": 0,
                "heartbeat_s": auto_limit_heartbeat_s(cfg),
                "reason": auto_limit_reason,
            }
            decision["mode"] = MODE_AUTO
            decision["storage_req_w"] = auto_storage_req_w
            if auto_storage_req_w > 0:
                decision["budget_w"] = max(0, pv_after_fixed_w - auto_storage_req_w)
            decision["reason"] = (
                str(decision.get("reason") or "")
                + "; "
                + auto_limit_reason
            )[:220]
    if observe_reserve_release_active:
        decision.setdefault("observe_wallbox_storage_policy", "reserve")
        decision.setdefault("observe_wallbox_reserve_release_active", True)
        decision.setdefault("observe_wallbox_reserve_floor_soc", observe_reserve_release.get("floor_soc"))
        decision.setdefault("observe_wallbox_reserve_release_floor_soc", observe_reserve_release.get("release_floor_soc"))
        decision.setdefault("observe_wallbox_reserve_soc", observe_reserve_release.get("soc"))
    decision = enforce_hard_mode_guard(
        cfg,
        live_with_wallbox,
        decision,
        max_charge_w,
        max_discharge_w,
        previous_state=previous_state,
        now_s=now_s,
    )
    state = str(decision.get("state") or "parallel_auto")
    mode = safe_int(decision.get("mode"), MODE_AUTO)
    val = max(0, safe_int(decision.get("val"), 0))
    budget_w = max(0, safe_int(decision.get("budget_w"), 0))
    shadow_payload = decision.get("shadow_payload") if isinstance(decision.get("shadow_payload"), dict) else {}
    shadow_inputs = shadow_payload.get("inputs") if isinstance(shadow_payload.get("inputs"), dict) else {}
    storage_charge_request_w = max(0, safe_int(decision.get("storage_req_w"), 0))
    raw_iaval_w = safe_int(decision.get("raw_iAVal_w"), pv_after_fixed_w)
    curve_gap_diag_pct = safe_float(shadow_inputs.get("curve_gap_pct"), safe_float(active_state.get("curve_gap_pct"), 0.0))
    curve_gap_catchup_w = max(0, safe_int(shadow_inputs.get("curve_gap_catchup_w"), active_state.get("curve_gap_catchup_w", 0)))
    curve_gap_catchup_cap_w = max(0, safe_int(shadow_inputs.get("curve_gap_catchup_cap_w"), active_state.get("curve_gap_catchup_cap_w", 0)))
    curve_gap_catchup_factor = max(
        0.0,
        min(1.0, safe_float(shadow_inputs.get("curve_gap_catchup_factor"), active_state.get("curve_gap_catchup_factor", 0.0))),
    )
    curve_gap_catchup_min_w = max(0, safe_int(shadow_inputs.get("curve_gap_catchup_min_w"), active_state.get("curve_gap_catchup_min_w", 0)))
    curve_gap_catchup_taper_pct = max(
        0.0,
        safe_float(shadow_inputs.get("curve_gap_catchup_taper_pct"), active_state.get("curve_gap_catchup_taper_pct", 0.0)),
    )
    curve_need_raw_w = max(0, safe_int(shadow_inputs.get("curve_need_raw_w"), active_state.get("curve_need_raw_w", 0)))
    lookahead_need_w = max(0, safe_int(shadow_inputs.get("lookahead_need_w"), active_state.get("lookahead_need_w", 0)))
    curve_hard_anchor_need_w = max(0, safe_int(shadow_inputs.get("curve_hard_anchor_need_w"), active_state.get("curve_hard_anchor_need_w", 0)))
    curve_hard_anchor_gap_pct = max(
        0.0,
        safe_float(shadow_inputs.get("curve_hard_anchor_gap_pct"), active_state.get("curve_hard_anchor_gap_pct", 0.0)),
    )
    curve_hard_anchor_missed = bool(shadow_inputs.get("curve_hard_anchor_missed", active_state.get("curve_hard_anchor_missed", False)))
    curve_hard_anchor_mode = str(shadow_inputs.get("curve_hard_anchor_mode", active_state.get("curve_hard_anchor_mode", "")) or "")
    curve_hard_anchor_soc = shadow_inputs.get("curve_hard_anchor_soc", active_state.get("curve_hard_anchor_soc"))
    curve_hard_anchor_ts = shadow_inputs.get("curve_hard_anchor_ts", active_state.get("curve_hard_anchor_ts"))
    curve_frame_base_smoothing_active = bool(
        shadow_inputs.get(
            "curve_frame_base_smoothing_active",
            active_state.get("curve_frame_base_smoothing_active", False),
        )
    )
    curve_frame_base_smoothing_phase = str(
        shadow_inputs.get(
            "curve_frame_base_smoothing_phase",
            active_state.get("curve_frame_base_smoothing_phase", ""),
        )
        or ""
    )
    curve_frame_base_smoothing_desired_w = max(
        0,
        safe_int(
            shadow_inputs.get(
                "curve_frame_base_smoothing_desired_w",
                active_state.get("curve_frame_base_smoothing_desired_w", 0),
            ),
            0,
        ),
    )
    curve_frame_base_smoothing_previous_w = max(
        0,
        safe_int(
            shadow_inputs.get(
                "curve_frame_base_smoothing_previous_w",
                active_state.get("curve_frame_base_smoothing_previous_w", 0),
            ),
            0,
        ),
    )
    curve_frame_base_smoothing_hold_band_w = max(
        0,
        safe_int(
            shadow_inputs.get(
                "curve_frame_base_smoothing_hold_band_w",
                active_state.get("curve_frame_base_smoothing_hold_band_w", 0),
            ),
            0,
        ),
    )
    curve_frame_base_smoothing_step_w = max(
        0,
        safe_int(
            shadow_inputs.get(
                "curve_frame_base_smoothing_step_w",
                active_state.get("curve_frame_base_smoothing_step_w", 0),
            ),
            0,
        ),
    )
    curve_frame_lift_w = max(0, safe_int(decision.get("curve_frame_lift_w"), 0))
    curve_frame_lift_desired_w = max(0, safe_int(decision.get("curve_frame_lift_desired_w"), 0))
    curve_frame_lift_actual_w = max(0, safe_int(decision.get("curve_frame_lift_actual_w"), 0))
    curve_frame_lift_shortfall_w = max(0, safe_int(decision.get("curve_frame_lift_shortfall_w"), 0))
    curve_frame_lift_step_w = max(0, safe_int(decision.get("curve_frame_lift_step_w"), 0))
    curve_frame_lift_max_boost_w = max(0, safe_int(decision.get("curve_frame_lift_max_boost_w"), 0))
    curve_frame_lift_reason = str(decision.get("curve_frame_lift_reason") or "")
    curve_frame_lift_gap_pct = safe_float(decision.get("curve_frame_lift_gap_pct"), 0.0)
    curve_frame_measured_trim_phase = str(decision.get("curve_frame_measured_trim_phase") or "")
    curve_frame_measured_trim_offset_w = max(0, safe_int(decision.get("curve_frame_measured_trim_offset_w"), 0))
    curve_frame_measured_trim_anchor_ts = max(0, safe_int(decision.get("curve_frame_measured_trim_anchor_ts"), 0))
    curve_frame_measured_trim_hold_until_ts = max(
        0,
        safe_int(decision.get("curve_frame_measured_trim_hold_until_ts"), 0),
    )
    curve_auto_hold_charge_offer_w = max(0, safe_int(decision.get("curve_auto_hold_charge_offer_w"), 0))
    curve_auto_hold_charge_offer_threshold_w = max(
        0,
        safe_int(decision.get("curve_auto_hold_charge_offer_threshold_w"), 0),
    )
    curve_auto_hold_charge_offer_active = bool(decision.get("curve_auto_hold_charge_offer_active"))
    abregel_charge_req_w = max(0, safe_int(shadow_inputs.get("curve_cap_pressure_w"), 0))
    abregel_grid_pressure_w = max(0, safe_int(shadow_inputs.get("curve_cap_grid_pressure_w"), 0))
    abregel_physical_pressure_w = max(0, safe_int(shadow_inputs.get("curve_cap_physical_pressure_w"), 0))
    abregel_inverter_pressure_w = max(0, safe_int(shadow_inputs.get("inverter_pressure_w"), 0))
    abregel_grid_error_w = safe_int(shadow_inputs.get("curve_cap_grid_export_error_w"), 0)
    abregel_target_w = max(0, safe_int(shadow_inputs.get("curve_cap_feed_export_threshold_w"), 0))
    abregel_release_w = max(0, safe_int(shadow_inputs.get("curve_cap_release_export_threshold_w"), 0))
    abregel_rscp_limit_w = max(0, safe_int(shadow_inputs.get("live_derate_limit_w"), 0))
    abregel_source = str(shadow_inputs.get("derate_limit_source") or "")
    abregel_active = bool(
        state == "parallel_curve_charge_cap"
        or shadow_inputs.get("curve_cap_feedback_active")
        or shadow_inputs.get("curve_cap_hard_pressure_active")
    )
    heatpump_pause_request = source_recovery_request(cfg, live_with_wallbox, plan, now_s)
    if (
        mode == MODE_AUTO
        and wb_car_present
        and state not in {"parallel_curve_charge", "parallel_curve_charge_cap"}
        and storage_charge_request_w <= 0
        and not bool(decision.get("protected"))
    ):
        budget_w = max(budget_w, base_wb_budget_w)
    direct_marketing_monitor_state = direct_marketing_monitor(
        cfg,
        live,
        plan,
        now_s,
        max_charge_w,
        max_discharge_w,
        decision,
        previous_state,
    )
    heatpump_running = bool(wp_w > max(100, safe_int(cfg.get("heatpump_running_threshold_w"), 150)))
    heatpump_running_commitment_w = wp_w if heatpump_running else 0
    heatpump_starting_until_ts = safe_float(previous_state.get("heatpump_starting_until_ts"), 0.0)
    if heatpump_running and not bool(previous_state.get("heatpump_running")):
        heatpump_starting_until_ts = now_s + max(
            10.0,
            safe_float(cfg.get("heatpump_start_settle_s"), 30.0),
        )
    heatpump_starting = bool(heatpump_running and now_s < heatpump_starting_until_ts)
    current_wallbox_commitment_w = max(0, safe_int(wb_intent.get("wb_power_w"), 0))
    phase_transition_grants = wallbox_phase_transition_policy.arbitrate_grants(
        wallbox_phase_transition.get("reservations", []),
        available_w=(
            max(0, safe_int(budget_w, 0))
            + heatpump_running_commitment_w
            + current_wallbox_commitment_w
        ),
        heatpump_running=heatpump_running,
        heatpump_running_commitment_w=heatpump_running_commitment_w,
        safety_margin_w=max(0, safe_int(cfg.get("phase_transition_safety_margin_w"), 0)),
        now_ts=now_s,
    )
    phase_transition_reserved_w = max(
        0,
        safe_int(phase_transition_grants.get("reserved_w_total"), 0),
    )
    phase_transition_requested_w = max(
        0,
        safe_int(wallbox_phase_transition.get("requested_w"), 0),
    )
    flexible_consumer_budget_w = max(
        0,
        safe_int(phase_transition_grants.get("flexible_budget_after_commitments_w"), budget_w),
    )
    phase_transition_allocations = (
        {
            "wallbox": phase_transition_reserved_w,
            "heatpump": heatpump_running_commitment_w if heatpump_running else flexible_consumer_budget_w,
            "heater": flexible_consumer_budget_w,
        }
        if wallbox_phase_transition.get("active")
        else None
    )

    budget = {
        "budget_w": budget_w,
        "iAVal_w": budget_w,
        "raw_iAVal_w": raw_iaval_w,
        "wb_min_required_w": phase_transition_requested_w,
        "wallbox_phase_transition_active": bool(wallbox_phase_transition.get("active")),
        "wallbox_phase_transition_reserved_w": phase_transition_reserved_w,
        "wallbox_phase_transition_flexible_budget_w": flexible_consumer_budget_w,
        "wallbox_phase_transition_target_phases": safe_int(wallbox_phase_transition.get("target_phases"), 0),
        "wallbox_phase_transition_until_ts": safe_float(wallbox_phase_transition.get("expires_ts"), 0.0),
        "wallbox_phase_transition_source": str(wallbox_phase_transition.get("source") or ""),
        "wallbox_phase_transition": wallbox_phase_transition,
        "wallbox_phase_transition_grants": phase_transition_grants,
        "wallbox_phase_transition_requested_w_total": phase_transition_requested_w,
        "wallbox_phase_transition_reserved_w_total": phase_transition_reserved_w,
        "heatpump_running": heatpump_running,
        "heatpump_running_commitment_w": heatpump_running_commitment_w,
        "heatpump_starting": heatpump_starting,
        "heatpump_starting_until_ts": heatpump_starting_until_ts,
        "heatpump_new_start_allowed": not bool(wallbox_phase_transition.get("active")),
        "flexible_budget_after_commitments_w": flexible_consumer_budget_w,
        "iFc_w": i_fc_w,
        "iMinLade_w": i_min_lade_w,
        "curve_frame_base_smoothing_active": curve_frame_base_smoothing_active,
        "curve_frame_base_smoothing_phase": curve_frame_base_smoothing_phase,
        "curve_frame_base_smoothing_desired_w": curve_frame_base_smoothing_desired_w,
        "curve_frame_base_smoothing_previous_w": curve_frame_base_smoothing_previous_w,
        "curve_frame_base_smoothing_hold_band_w": curve_frame_base_smoothing_hold_band_w,
        "curve_frame_base_smoothing_step_w": curve_frame_base_smoothing_step_w,
        "storage_charge_request_w": storage_charge_request_w,
        "curve_charge_servo_mode": decision.get("curve_charge_servo_mode"),
        "curve_charge_servo_enabled": bool(decision.get("curve_charge_servo_enabled")),
        "curve_charge_servo_active": bool(decision.get("curve_charge_servo_active")),
        "curve_charge_servo_candidate_state": str(decision.get("curve_charge_servo_candidate_state") or ""),
        "curve_charge_servo_block_reason": str(decision.get("curve_charge_servo_block_reason") or ""),
        "curve_charge_servo_phase": str(decision.get("curve_charge_servo_phase") or ""),
        "curve_charge_servo_previous_w": max(0, safe_int(decision.get("curve_charge_servo_previous_w"), 0)),
        "curve_charge_servo_target_w": max(0, safe_int(decision.get("curve_charge_servo_target_w"), 0)),
        "curve_charge_servo_frame_w": max(0, safe_int(decision.get("curve_charge_servo_frame_w"), 0)),
        "curve_charge_servo_available_w": max(0, safe_int(decision.get("curve_charge_servo_available_w"), 0)),
        "curve_charge_servo_min_w": max(0, safe_int(decision.get("curve_charge_servo_min_w"), 0)),
        "curve_charge_servo_deadband_w": max(0, safe_int(decision.get("curve_charge_servo_deadband_w"), 0)),
        "curve_charge_servo_step_up_w": max(0, safe_int(decision.get("curve_charge_servo_step_up_w"), 0)),
        "curve_charge_servo_step_down_w": max(0, safe_int(decision.get("curve_charge_servo_step_down_w"), 0)),
        "curve_charge_servo_max_age_s": max(0, safe_int(decision.get("curve_charge_servo_max_age_s"), 0)),
        "curve_frame_measured_trim_phase": curve_frame_measured_trim_phase,
        "curve_frame_measured_trim_offset_w": curve_frame_measured_trim_offset_w,
        "curve_frame_measured_trim_anchor_ts": curve_frame_measured_trim_anchor_ts,
        "curve_frame_measured_trim_hold_until_ts": curve_frame_measured_trim_hold_until_ts,
        "wallbox_curve_reserve_w": max(0, safe_int(decision.get("wallbox_curve_reserve_w"), 0)),
        "wallbox_curve_reserve_target_w": max(0, safe_int(decision.get("wallbox_curve_reserve_target_w"), 0)),
        "wallbox_curve_reserve_step_w": max(0, safe_int(decision.get("wallbox_curve_reserve_step_w"), 0)),
        "wallbox_curve_min_w": max(0, safe_int(decision.get("wallbox_curve_min_w"), 0)),
        "wallbox_curve_phases": max(0, safe_int(decision.get("wallbox_curve_phases"), 0)),
        "wallbox_curve_export_catchup_active": bool(decision.get("wallbox_curve_export_catchup_active")),
        "wallbox_curve_export_catchup_w": max(0, safe_int(decision.get("wallbox_curve_export_catchup_w"), 0)),
        "external_wallbox_curve_storage_protect": bool(decision.get("external_wallbox_curve_storage_protect")),
        "wallbox_curve_discharge_protect": bool(decision.get("wallbox_curve_discharge_protect")),
        "observe_wallbox_storage_policy": decision.get("observe_wallbox_storage_policy"),
        "observe_wallbox_reserve_release_active": bool(decision.get("observe_wallbox_reserve_release_active")),
        "observe_wallbox_reserve_floor_soc": decision.get("observe_wallbox_reserve_floor_soc"),
        "observe_wallbox_reserve_release_floor_soc": decision.get("observe_wallbox_reserve_release_floor_soc"),
        "observe_wallbox_reserve_soc": decision.get("observe_wallbox_reserve_soc"),
        "wallbox_curve_pv_only": bool(decision.get("wallbox_curve_pv_only")),
        "target_corridor_fast_charge_active": bool(decision.get("target_corridor_fast_charge_active")),
        "market_economics_active": bool(decision.get("market_economics_active")),
        "market_economics_owner": decision.get("market_economics_owner"),
        "market_economics_contract_version": decision.get("market_economics_contract_version"),
        "market_economics_action": decision.get("market_economics_action"),
        "market_economics_commands_allowed": decision.get("market_economics_commands_allowed"),
        "market_economics_shadow": decision.get("market_economics_shadow"),
        "market_economics_reason": decision.get("market_economics_reason"),
        "market_economics_blocked_reasons": decision.get("market_economics_blocked_reasons") if isinstance(decision.get("market_economics_blocked_reasons"), list) else [],
        "market_economics_dwell": decision.get("market_economics_dwell") if isinstance(decision.get("market_economics_dwell"), dict) else {},
        "market_economics_dwell_active": bool(decision.get("market_economics_dwell_active")),
        "market_economics_dwell_remaining_s": safe_float(decision.get("market_economics_dwell_remaining_s"), 0.0),
        "market_economics_contract": decision.get("market_economics_contract") if isinstance(decision.get("market_economics_contract"), dict) else {},
        "market_economics_forecast": decision.get("market_economics_forecast") if isinstance(decision.get("market_economics_forecast"), dict) else {},
        "market_economics_economics": decision.get("market_economics_economics") if isinstance(decision.get("market_economics_economics"), dict) else {},
        "market_economics_target_soc_pct": decision.get("market_economics_target_soc_pct"),
        "market_live_pv_first": decision.get("market_live_pv_first") if isinstance(decision.get("market_live_pv_first"), dict) else {},
        "market_live_pv_first_overridden": bool(decision.get("market_live_pv_first_overridden")),
        "market_live_export_absorb_active": bool(decision.get("market_live_export_absorb_active")),
        "market_live_export_absorb_hold_active": bool(decision.get("market_live_export_absorb_hold_active")),
        "market_live_export_absorb_charge_w": max(0, safe_int(decision.get("market_live_export_absorb_charge_w"), 0)),
        "market_live_export_absorb": decision.get("market_live_export_absorb") if isinstance(decision.get("market_live_export_absorb"), dict) else {},
        "market_late_fill_wait_overridden": bool(decision.get("market_late_fill_wait_overridden")),
        "market_forecast_grid_charge_need_wh": safe_float(decision.get("market_forecast_grid_charge_need_wh"), 0.0),
        "target_corridor_storage_req_w": max(0, safe_int(decision.get("target_corridor_storage_req_w"), 0)),
        "controlled_wallbox_wbminsoc_pause": bool(decision.get("controlled_wallbox_wbminsoc_pause")),
        "controlled_wallbox_auto_freerun": bool(decision.get("controlled_wallbox_auto_freerun")),
        "controlled_wallbox_auto_original_state": str(decision.get("controlled_wallbox_auto_original_state") or ""),
        "wbminsoc_pv_charge_active": bool(decision.get("wbminsoc_pv_charge_active")),
        "planned_grid_pv_charge_w": max(0, safe_int(decision.get("planned_grid_pv_charge_w"), 0)),
        "planned_grid_pv_surplus_w": max(0, safe_int(decision.get("planned_grid_pv_surplus_w"), 0)),
        "pv_house_surplus_w": max(0, safe_int(decision.get("pv_house_surplus_w"), 0)),
        "scheduled_grid_charge": bool(decision.get("scheduled_grid_charge")),
        "wallbox_storage_protection": bool(decision.get("wallbox_storage_protection")),
        "wbminsoc_transition_dwell_active": bool(decision.get("wbminsoc_transition_dwell_active")),
        "wbminsoc_curve_dwell_active": bool(decision.get("wbminsoc_curve_dwell_active")),
        "wbminsoc_previous_state_age_s": round(safe_float(decision.get("wbminsoc_previous_state_age_s"), 0.0), 1),
        "wbminsoc_effective_pv_charge_threshold_w": max(
            0,
            safe_int(decision.get("wbminsoc_effective_pv_charge_threshold_w"), 0),
        ),
        "house_heatpump_discharge_cap_w": max(0, safe_int(decision.get("house_heatpump_discharge_cap_w"), 0)),
        "curve_gap_pct": curve_gap_diag_pct,
        "curve_gap_catchup_w": curve_gap_catchup_w,
        "curve_gap_catchup_cap_w": curve_gap_catchup_cap_w,
        "curve_gap_catchup_factor": curve_gap_catchup_factor,
        "curve_gap_catchup_min_w": curve_gap_catchup_min_w,
        "curve_gap_catchup_taper_pct": curve_gap_catchup_taper_pct,
        "curve_need_raw_w": curve_need_raw_w,
        "lookahead_need_w": lookahead_need_w,
        "curve_hard_anchor_need_w": curve_hard_anchor_need_w,
        "curve_hard_anchor_gap_pct": curve_hard_anchor_gap_pct,
        "curve_hard_anchor_missed": curve_hard_anchor_missed,
        "curve_hard_anchor_mode": curve_hard_anchor_mode,
        "curve_hard_anchor_soc": curve_hard_anchor_soc,
        "curve_hard_anchor_ts": curve_hard_anchor_ts,
        "curve_frame_base_smoothing_active": curve_frame_base_smoothing_active,
        "curve_frame_base_smoothing_phase": curve_frame_base_smoothing_phase,
        "curve_frame_base_smoothing_desired_w": curve_frame_base_smoothing_desired_w,
        "curve_frame_base_smoothing_previous_w": curve_frame_base_smoothing_previous_w,
        "curve_frame_base_smoothing_hold_band_w": curve_frame_base_smoothing_hold_band_w,
        "curve_frame_base_smoothing_step_w": curve_frame_base_smoothing_step_w,
        "curve_auto_hold_continuation_active": bool(decision.get("curve_auto_hold_continuation_active")),
        "curve_auto_hold_continuation_w": max(0, safe_int(decision.get("curve_auto_hold_continuation_w"), 0)),
        "curve_auto_hold_continuation_previous_w": max(
            0,
            safe_int(decision.get("curve_auto_hold_continuation_previous_w"), 0),
        ),
        "curve_auto_hold_continuation_decay_w": max(
            0,
            safe_int(decision.get("curve_auto_hold_continuation_decay_w"), 0),
        ),
        "curve_auto_hold_continuation_offer_w": max(
            0,
            safe_int(decision.get("curve_auto_hold_continuation_offer_w"), 0),
        ),
        "next_curve_evening_pv_release_active": bool(decision.get("next_curve_evening_pv_release_active")),
        "next_curve_evening_pv_release_seconds_to_first_curve": max(
            0,
            safe_int(decision.get("next_curve_evening_pv_release_seconds_to_first_curve"), 0),
        ),
        "next_curve_evening_pv_release_offer_w": max(
            0,
            safe_int(decision.get("next_curve_evening_pv_release_offer_w"), 0),
        ),
        "next_curve_evening_pv_release_max_lead_s": max(
            0,
            safe_int(decision.get("next_curve_evening_pv_release_max_lead_s"), 0),
        ),
        "adaptive_curve_active": adaptive_curve_active,
        "adaptive_curve_relation": adaptive_curve_relation,
        "adaptive_soc_floor": adaptive_floor_soc,
        "adaptive_soc_ceiling": adaptive_ceiling_soc,
        "adaptive_latest_charge_due": adaptive_latest_charge_due,
        "latest_charge_start_ts": adaptive_latest_charge_start_ts,
        "latest_charge_start_clamped": bool(adaptive_latest_charge_clamped),
        "latest_charge_start_raw_ts": adaptive_latest_charge_raw_ts,
        "latest_charge_start_previous_ts": adaptive_latest_charge_previous_ts,
        "evening_shortfall_wh": round(adaptive_evening_shortfall_wh, 0),
        "shortfall_late_catchup_active": bool(
            shadow_inputs.get("shortfall_late_catchup_active", active_state.get("shortfall_late_catchup_active", False))
        ),
        "shortfall_catchup_enter_w": max(
            0,
            safe_int(
                shadow_inputs.get("shortfall_catchup_enter_w", active_state.get("shortfall_catchup_enter_w", 0)),
                0,
            ),
        ),
        "shortfall_catchup_nominal_enter_w": max(
            0,
            safe_int(
                shadow_inputs.get(
                    "shortfall_catchup_nominal_enter_w",
                    active_state.get("shortfall_catchup_nominal_enter_w", 0),
                ),
                0,
            ),
        ),
        "shortfall_late_catchup_enter_w": max(
            0,
            safe_int(
                shadow_inputs.get(
                    "shortfall_late_catchup_enter_w",
                    active_state.get("shortfall_late_catchup_enter_w", 0),
                ),
                0,
            ),
        ),
        "shortfall_real_surplus_w": max(
            0,
            safe_int(
                shadow_inputs.get("shortfall_real_surplus_w", active_state.get("shortfall_real_surplus_w", 0)),
                0,
            ),
        ),
        "forecast_only_target_active": bool(forecast_only_target_active),
        "forecast_curve_landing_hold_active": bool(forecast_curve_landing_hold_active),
        "forecast_floor_target_gap_pct": round(forecast_floor_target_gap_pct, 3),
        "forecast_landing_margin_pct": round(forecast_landing_margin_pct, 3),
        "sliding_horizon_active": bool(
            shadow_inputs.get("sliding_horizon_active", active_state.get("sliding_horizon_active", False))
        ),
        "sliding_horizon_reason": str(
            shadow_inputs.get("sliding_horizon_reason", active_state.get("sliding_horizon_reason", "")) or ""
        ),
        "sliding_horizon_confidence": round(
            safe_float(
                shadow_inputs.get(
                    "sliding_horizon_confidence",
                    active_state.get("sliding_horizon_confidence", 0.0),
                ),
                0.0,
            ),
            4,
        ),
        "sliding_horizon_min_confidence": round(
            safe_float(
                shadow_inputs.get(
                    "sliding_horizon_min_confidence",
                    active_state.get("sliding_horizon_min_confidence", 0.0),
                ),
                0.0,
            ),
            4,
        ),
        "sliding_horizon_season": str(
            shadow_inputs.get("sliding_horizon_season", active_state.get("sliding_horizon_season", "")) or ""
        ),
        "sliding_horizon_minutes_until_latest_charge": shadow_inputs.get(
            "sliding_horizon_minutes_until_latest_charge",
            active_state.get("sliding_horizon_minutes_until_latest_charge"),
        ),
        "sliding_horizon_headroom_available_wh": round(
            safe_float(
                shadow_inputs.get(
                    "sliding_horizon_headroom_available_wh",
                    active_state.get("sliding_horizon_headroom_available_wh", 0.0),
                ),
                0.0,
            ),
            0,
        ),
        "sliding_horizon_uncovered_pressure_wh": round(
            safe_float(
                shadow_inputs.get(
                    "sliding_horizon_uncovered_pressure_wh",
                    active_state.get("sliding_horizon_uncovered_pressure_wh", 0.0),
                ),
                0.0,
            ),
            0,
        ),
        "sliding_horizon_uncovered_curtailment_pressure_wh": round(
            safe_float(
                shadow_inputs.get(
                    "sliding_horizon_uncovered_curtailment_pressure_wh",
                    active_state.get("sliding_horizon_uncovered_curtailment_pressure_wh", 0.0),
                ),
                0.0,
            ),
            0,
        ),
        "adaptive_headroom_required_wh": round(adaptive_headroom_required_wh, 0),
        "adaptive_headroom_available_wh": round(adaptive_headroom_available_wh, 0),
        "curtailment_pressure_wh": round(adaptive_curtailment_pressure_wh, 0),
        "curtailment_unavoidable_wh": round(adaptive_curtailment_unavoidable_wh, 0),
        "headroom_reserve_active": bool(headroom_reserve_active),
        "headroom_reserve_pressure_wh": round(headroom_reserve_pressure_wh, 0),
        "headroom_reserve_source": headroom_reserve_source,
        "headroom_discharge_active": bool(
            state == "parallel_headroom_discharge"
            and (
                decision.get("headroom_discharge_active")
                or shadow_inputs.get("headroom_discharge_active")
            )
        ),
        "headroom_discharge_candidate": bool(
            decision.get("headroom_discharge_active")
            or shadow_inputs.get("headroom_discharge_active")
        ),
        "headroom_discharge_w": max(
            0,
            safe_int(decision.get("headroom_discharge_w"), shadow_inputs.get("headroom_discharge_w", 0)),
        ),
        "headroom_discharge_target_w": max(
            0,
            safe_int(
                decision.get("headroom_discharge_target_w"),
                shadow_inputs.get("headroom_discharge_target_w", 0),
            ),
        ),
        "headroom_discharge_floor_soc": decision.get(
            "headroom_discharge_floor_soc",
            shadow_inputs.get("headroom_discharge_floor_soc"),
        ),
        "headroom_discharge_gap_pct": safe_float(
            decision.get("headroom_discharge_gap_pct"),
            shadow_inputs.get("headroom_discharge_gap_pct", 0.0),
        ),
        "headroom_discharge_export_room_w": max(
            0,
            safe_int(
                decision.get("headroom_discharge_export_room_w"),
                shadow_inputs.get("headroom_discharge_export_room_w", 0),
            ),
        ),
        "headroom_discharge_pressure_wh": round(
            max(
                0.0,
                safe_float(
                    decision.get("headroom_discharge_pressure_wh"),
                    shadow_inputs.get("headroom_discharge_pressure_wh", 0.0),
                ),
            ),
            0,
        ),
        "headroom_discharge_min_pressure_wh": round(
            max(
                0.0,
                safe_float(
                    decision.get("headroom_discharge_min_pressure_wh"),
                    shadow_inputs.get("headroom_discharge_min_pressure_wh", 0.0),
                ),
            ),
            0,
        ),
        "headroom_discharge_blocked_reason": str(
            decision.get(
                "headroom_discharge_blocked_reason",
                shadow_inputs.get("headroom_discharge_blocked_reason", ""),
            )
            or ""
        ),
        "headroom_discharge_target_plateau_reached": bool(
            decision.get("headroom_discharge_target_plateau_reached")
            or shadow_inputs.get("headroom_discharge_target_plateau_reached")
        ),
        "headroom_discharge_target_curve_soc": (
            None
            if decision.get("headroom_discharge_target_curve_soc", shadow_inputs.get("headroom_discharge_target_curve_soc")) is None
            else round(
                safe_float(
                    decision.get("headroom_discharge_target_curve_soc"),
                    shadow_inputs.get("headroom_discharge_target_curve_soc", 0.0),
                ),
                2,
            )
        ),
        "headroom_discharge_target_plateau_margin_pct": round(
            max(
                0.0,
                safe_float(
                    decision.get("headroom_discharge_target_plateau_margin_pct"),
                    shadow_inputs.get("headroom_discharge_target_plateau_margin_pct", 0.0),
                ),
            ),
            2,
        ),
        "headroom_discharge_abregel_blocked": bool(
            decision.get("headroom_discharge_abregel_blocked")
            or shadow_inputs.get("headroom_discharge_abregel_blocked")
        ),
        "headroom_discharge_day": str(
            decision.get("headroom_discharge_day", shadow_inputs.get("headroom_discharge_day", ""))
            or ""
        ),
        "headroom_discharge_today_wh": round(
            safe_float(
                decision.get("headroom_discharge_today_wh"),
                shadow_inputs.get("headroom_discharge_today_wh", 0.0),
            ),
            1,
        ),
        "headroom_discharge_daily_limit_wh": round(
            safe_float(
                decision.get("headroom_discharge_daily_limit_wh"),
                shadow_inputs.get("headroom_discharge_daily_limit_wh", 0.0),
            ),
            1,
        ),
        "headroom_discharge_daily_remaining_wh": round(
            safe_float(
                decision.get("headroom_discharge_daily_remaining_wh"),
                shadow_inputs.get("headroom_discharge_daily_remaining_wh", 0.0),
            ),
            1,
        ),
        "headroom_discharge_daily_limit_pct": round(
            safe_float(
                decision.get("headroom_discharge_daily_limit_pct"),
                shadow_inputs.get("headroom_discharge_daily_limit_pct", 0.0),
            ),
            2,
        ),
        "headroom_discharge_daily_blocked": bool(
            decision.get("headroom_discharge_daily_blocked")
            or shadow_inputs.get("headroom_discharge_daily_blocked")
        ),
        "headroom_discharge_cooldown_s": max(
            0,
            safe_int(
                decision.get("headroom_discharge_cooldown_s"),
                shadow_inputs.get("headroom_discharge_cooldown_s", 0),
            ),
        ),
        "headroom_discharge_cooldown_remaining_s": round(
            safe_float(
                decision.get("headroom_discharge_cooldown_remaining_s"),
                shadow_inputs.get("headroom_discharge_cooldown_remaining_s", 0.0),
            ),
            1,
        ),
        "headroom_discharge_cooldown_active": bool(
            decision.get("headroom_discharge_cooldown_active")
            or shadow_inputs.get("headroom_discharge_cooldown_active")
        ),
        "headroom_discharge_last_active_ts": safe_float(
            decision.get("headroom_discharge_last_active_ts"),
            shadow_inputs.get("headroom_discharge_last_active_ts", 0.0),
        ),
        "headroom_discharge_last_account_ts": safe_float(
            decision.get("headroom_discharge_last_account_ts"),
            shadow_inputs.get("headroom_discharge_last_account_ts", now_s),
        ),
        "headroom_execution_schema_version": decision.get(
            "headroom_execution_schema_version",
            shadow_inputs.get("headroom_execution_schema_version"),
        ),
        "headroom_execution_allowed": bool(
            decision.get("headroom_execution_allowed", shadow_inputs.get("headroom_execution_allowed", False))
        ),
        "headroom_execution_reason_code": str(
            decision.get(
                "headroom_execution_reason_code",
                shadow_inputs.get("headroom_execution_reason_code", "HEADROOM_EXECUTION_CONTRACT_MISSING"),
            )
            or "HEADROOM_EXECUTION_CONTRACT_MISSING"
        ),
        "headroom_execution_plan_id": decision.get(
            "headroom_execution_plan_id",
            shadow_inputs.get("headroom_execution_plan_id"),
        ),
        "headroom_execution_slot_id": decision.get(
            "headroom_execution_slot_id",
            shadow_inputs.get("headroom_execution_slot_id"),
        ),
        "headroom_execution_earliest_start_ts": decision.get(
            "headroom_execution_earliest_start_ts",
            shadow_inputs.get("headroom_execution_earliest_start_ts"),
        ),
        "headroom_execution_deadline_ts": decision.get(
            "headroom_execution_deadline_ts",
            shadow_inputs.get("headroom_execution_deadline_ts"),
        ),
        "headroom_execution_target_soc": decision.get(
            "headroom_execution_target_soc",
            shadow_inputs.get("headroom_execution_target_soc"),
        ),
        "headroom_execution_hard_floor_soc": decision.get(
            "headroom_execution_hard_floor_soc",
            shadow_inputs.get("headroom_execution_hard_floor_soc"),
        ),
        "headroom_execution_plan_accounted_wh": round(
            max(0.0, safe_float(decision.get("headroom_execution_plan_accounted_wh"), shadow_inputs.get("headroom_execution_plan_accounted_wh", 0.0))),
            3,
        ),
        "headroom_execution_slot_accounted_wh": round(
            max(0.0, safe_float(decision.get("headroom_execution_slot_accounted_wh"), shadow_inputs.get("headroom_execution_slot_accounted_wh", 0.0))),
            3,
        ),
        "headroom_execution_residual_wh": round(
            max(0.0, safe_float(decision.get("headroom_execution_residual_wh"), shadow_inputs.get("headroom_execution_residual_wh", 0.0))),
            3,
        ),
        "headroom_execution_accounted_observed_w": round(
            max(0.0, safe_float(decision.get("headroom_execution_accounted_observed_w"), shadow_inputs.get("headroom_execution_accounted_observed_w", 0.0))),
            3,
        ),
        "headroom_execution_accounted_interval_s": round(
            max(0.0, safe_float(decision.get("headroom_execution_accounted_interval_s"), shadow_inputs.get("headroom_execution_accounted_interval_s", 0.0))),
            3,
        ),
        "headroom_execution_generation_reset": bool(
            decision.get("headroom_execution_generation_reset", shadow_inputs.get("headroom_execution_generation_reset", True))
        ),
        "headroom_execution_last_account_ts": safe_float(
            decision.get("headroom_execution_last_account_ts"),
            shadow_inputs.get("headroom_execution_last_account_ts", now_s),
        ),
        "curve_frame_lift_active": bool(decision.get("curve_frame_lift_active")),
        "curve_frame_lift_w": curve_frame_lift_w,
        "curve_frame_lift_desired_w": curve_frame_lift_desired_w,
        "curve_frame_lift_actual_w": curve_frame_lift_actual_w,
        "curve_frame_lift_shortfall_w": curve_frame_lift_shortfall_w,
        "curve_frame_lift_step_w": curve_frame_lift_step_w,
        "curve_frame_lift_max_boost_w": curve_frame_lift_max_boost_w,
        "curve_frame_lift_reason": curve_frame_lift_reason,
        "curve_frame_lift_gap_pct": curve_frame_lift_gap_pct,
        "curve_frame_measured_trim_phase": curve_frame_measured_trim_phase,
        "curve_frame_measured_trim_offset_w": curve_frame_measured_trim_offset_w,
        "curve_frame_measured_trim_anchor_ts": curve_frame_measured_trim_anchor_ts,
        "curve_frame_measured_trim_hold_until_ts": curve_frame_measured_trim_hold_until_ts,
        "curve_auto_hold_charge_offer_w": curve_auto_hold_charge_offer_w,
        "curve_auto_hold_charge_offer_threshold_w": curve_auto_hold_charge_offer_threshold_w,
        "curve_auto_hold_charge_offer_active": curve_auto_hold_charge_offer_active,
        "planned_load_expected_w": safe_int(decision.get("planned_load_expected_w"), 0),
        "planned_load_observed_extra_w": safe_int(decision.get("planned_load_observed_extra_w"), 0),
        "planned_load_windows": decision.get("planned_load_windows", []),
        "planned_load_names": decision.get("planned_load_names", []),
        "planned_load_support": decision.get("planned_load_support", {}),
        "heatpump_pause_request": heatpump_pause_request,
        "direct_marketing_active": bool(decision.get("direct_marketing_active")),
        "direct_marketing_policy_active": bool(decision.get("direct_marketing_policy_active")),
        "direct_marketing_policy_schema": decision.get("direct_marketing_policy_schema"),
        "direct_marketing_policy_decision": decision.get("direct_marketing_policy_decision") if isinstance(decision.get("direct_marketing_policy_decision"), dict) else None,
        "direct_marketing_policy_target_state": decision.get("direct_marketing_policy_target_state"),
        "direct_marketing_policy_block_reason": decision.get("direct_marketing_policy_block_reason"),
        "direct_marketing_policy_export_budget_w": safe_int(decision.get("direct_marketing_policy_export_budget_w"), 0),
        "direct_marketing_policy_charge_budget_w": safe_int(decision.get("direct_marketing_policy_charge_budget_w"), 0),
        "direct_marketing_policy_protected_reserve_wh": safe_int(decision.get("direct_marketing_policy_protected_reserve_wh"), 0),
        "direct_marketing_policy_sellable_wh": safe_int(decision.get("direct_marketing_policy_sellable_wh"), 0),
        "direct_marketing_policy_executor_gate": decision.get("direct_marketing_policy_executor_gate") if isinstance(decision.get("direct_marketing_policy_executor_gate"), dict) else None,
        "direct_marketing_future_pv_store_reservation": decision.get("direct_marketing_future_pv_store_reservation") if isinstance(decision.get("direct_marketing_future_pv_store_reservation"), dict) else None,
        "direct_marketing_future_pv_store_reservation_active": bool(decision.get("direct_marketing_future_pv_store_reservation_active")),
        "direct_marketing_future_pv_store_reservation_cap_w": safe_int(decision.get("direct_marketing_future_pv_store_reservation_cap_w"), 0),
        "direct_marketing_mode": decision.get("direct_marketing_mode"),
        "direct_marketing_owner": decision.get("direct_marketing_owner"),
        "direct_marketing_contract_version": decision.get("direct_marketing_contract_version"),
        "direct_marketing_action": decision.get("direct_marketing_action"),
        "direct_marketing_window": decision.get("direct_marketing_window"),
        "direct_marketing_profit_ct_per_kwh": decision.get("direct_marketing_profit_ct_per_kwh"),
        "direct_marketing_reserve_floor_soc_pct": decision.get("direct_marketing_reserve_floor_soc_pct"),
        "direct_marketing_target_soc_pct": decision.get("direct_marketing_target_soc_pct"),
        "direct_marketing_headroom_hold_active": bool(decision.get("direct_marketing_headroom_hold_active")),
        "direct_marketing_headroom_soc_ceiling_pct": safe_float(decision.get("direct_marketing_headroom_soc_ceiling_pct"), 0.0),
        "direct_marketing_headroom_deficit_wh": safe_int(decision.get("direct_marketing_headroom_deficit_wh"), 0),
        "direct_marketing_headroom_next_start_ts": safe_int(decision.get("direct_marketing_headroom_next_start_ts"), 0),
        "direct_marketing_headroom_window_min": safe_float(decision.get("direct_marketing_headroom_window_min"), 0.0),
        "direct_marketing_headroom_forecast_surplus_wh": safe_int(decision.get("direct_marketing_headroom_forecast_surplus_wh"), 0),
        "direct_marketing_headroom_required_pct": safe_float(decision.get("direct_marketing_headroom_required_pct"), 0.0),
        "direct_marketing_pv_store_w": safe_int(decision.get("direct_marketing_pv_store_w"), 0),
        "direct_marketing_pv_store_offer_w": safe_int(decision.get("direct_marketing_pv_store_offer_w"), 0),
        "direct_marketing_pv_store_max_w": safe_int(decision.get("direct_marketing_pv_store_max_w"), 0),
        "direct_marketing_pv_store_surplus_w": safe_int(decision.get("direct_marketing_pv_store_surplus_w"), 0),
        "direct_marketing_pv_store_grid_import_w": safe_int(decision.get("direct_marketing_pv_store_grid_import_w"), 0),
        "direct_marketing_pv_store_grid_export_w": safe_int(decision.get("direct_marketing_pv_store_grid_export_w"), 0),
        "direct_marketing_pv_store_import_guard_w": safe_int(decision.get("direct_marketing_pv_store_import_guard_w"), 0),
        "direct_marketing_pv_store_min_surplus_w": safe_int(decision.get("direct_marketing_pv_store_min_surplus_w"), 0),
        "direct_marketing_pv_store_requested_w": safe_int(decision.get("direct_marketing_pv_store_requested_w"), 0),
        "direct_marketing_pv_store_target_fallback_active": bool(decision.get("direct_marketing_pv_store_target_fallback_active")),
        "direct_marketing_pv_store_estimated_offer_w": safe_int(decision.get("direct_marketing_pv_store_estimated_offer_w"), 0),
        "direct_marketing_pv_store_pv_safe_cap_w": safe_int(decision.get("direct_marketing_pv_store_pv_safe_cap_w"), 0),
        "direct_marketing_pv_store_self_reference_limited": bool(decision.get("direct_marketing_pv_store_self_reference_limited")),
        "direct_marketing_pv_store_execution": str(decision.get("direct_marketing_pv_store_execution") or ""),
        "direct_marketing_pv_store_auto_limit_active": bool(decision.get("direct_marketing_pv_store_auto_limit_active")),
        "direct_marketing_pv_store_external_export_owner": bool(decision.get("direct_marketing_pv_store_external_export_owner")),
        "direct_marketing_hard_export_owner_confirmed": bool(decision.get("direct_marketing_hard_export_owner_confirmed")),
        "direct_marketing_export_execution": decision.get("direct_marketing_export_execution") if isinstance(decision.get("direct_marketing_export_execution"), dict) else {},
        "direct_marketing_export_execution_state": decision.get("direct_marketing_export_execution_state"),
        "direct_marketing_export_execution_claim": decision.get("direct_marketing_export_execution_claim"),
        "direct_marketing_export_compliance_confirmed": bool(decision.get("direct_marketing_export_compliance_confirmed")),
        "direct_marketing_export_violation_w": safe_int(decision.get("direct_marketing_export_violation_w"), 0),
        "direct_marketing_export_constraint_class": decision.get("direct_marketing_export_constraint_class"),
        "direct_marketing_hard_export_limit_active": bool(decision.get("direct_marketing_hard_export_limit_active")),
        "direct_marketing_hard_export_limit_w": decision.get("direct_marketing_hard_export_limit_w"),
        "direct_marketing_export_constraint_scope": decision.get("direct_marketing_export_constraint_scope"),
        "direct_marketing_pv_export_allowed": bool(decision.get("direct_marketing_pv_export_allowed")),
        "direct_marketing_pv_store_export_limit_active": bool(decision.get("direct_marketing_pv_store_export_limit_active")),
        "direct_marketing_pv_store_export_limit_guard_active": bool(decision.get("direct_marketing_pv_store_export_limit_guard_active")),
        "direct_marketing_pv_store_export_limit_w": safe_int(decision.get("direct_marketing_pv_store_export_limit_w"), 0),
        "direct_marketing_pv_store_export_limit_guard_w": safe_int(decision.get("direct_marketing_pv_store_export_limit_guard_w"), 0),
        "direct_marketing_pv_store_export_over_limit_w": safe_int(decision.get("direct_marketing_pv_store_export_over_limit_w"), 0),
        "direct_marketing_pv_store_export_absorb_target_w": safe_int(decision.get("direct_marketing_pv_store_export_absorb_target_w"), 0),
        "direct_marketing_pv_store_unavoidable_export_w": safe_int(decision.get("direct_marketing_pv_store_unavoidable_export_w"), 0),
        "direct_marketing_external_derating_active": bool(decision.get("direct_marketing_external_derating_active")),
        "direct_marketing_external_derating_source": decision.get("direct_marketing_external_derating_source"),
        "direct_marketing_external_derating_limit_w": decision.get("direct_marketing_external_derating_limit_w"),
        "direct_marketing_external_derating_ac_power_limit_w": safe_int(decision.get("direct_marketing_external_derating_ac_power_limit_w"), 0),
        "direct_marketing_external_derating_power_w": safe_int(decision.get("direct_marketing_external_derating_power_w"), 0),
        "direct_marketing_external_derating_percent": safe_float(decision.get("direct_marketing_external_derating_percent"), 0.0),
        "direct_marketing_pv_store_export_limit_ramp_bypass": bool(decision.get("direct_marketing_pv_store_export_limit_ramp_bypass")),
        "direct_marketing_pv_store_ramp_limited": bool(decision.get("direct_marketing_pv_store_ramp_limited")),
        "direct_marketing_pv_store_ramp_base_w": safe_int(decision.get("direct_marketing_pv_store_ramp_base_w"), 0),
        "direct_marketing_pv_store_ramp_step_w": safe_int(decision.get("direct_marketing_pv_store_ramp_step_w"), 0),
        "direct_marketing_pv_store_curve_catchup_active": bool(decision.get("direct_marketing_pv_store_curve_catchup_active")),
        "direct_marketing_pv_store_curve_catchup_w": safe_int(decision.get("direct_marketing_pv_store_curve_catchup_w"), 0),
        "direct_marketing_pv_store_curve_catchup_source": decision.get("direct_marketing_pv_store_curve_catchup_source"),
        "direct_marketing_pv_store_curve_catchup_raw_w": safe_int(decision.get("direct_marketing_pv_store_curve_catchup_raw_w"), 0),
        "direct_marketing_pv_store_curve_catchup_gap_pct": safe_float(decision.get("direct_marketing_pv_store_curve_catchup_gap_pct"), 0.0),
        "direct_marketing_pv_store_curve_soc_pct": decision.get("direct_marketing_pv_store_curve_soc_pct"),
        "direct_marketing_pv_store_curve_target_soc_pct": decision.get("direct_marketing_pv_store_curve_target_soc_pct"),
        "direct_marketing_pv_store_curve_target_ts": safe_float(decision.get("direct_marketing_pv_store_curve_target_ts"), 0.0),
        "direct_marketing_pv_store_curve_window_target_soc_pct": decision.get("direct_marketing_pv_store_curve_window_target_soc_pct"),
        "direct_marketing_pv_store_release_hold_active": bool(decision.get("direct_marketing_pv_store_release_hold_active")),
        "direct_marketing_pv_store_release_hold_reason": decision.get("direct_marketing_pv_store_release_hold_reason"),
        "direct_marketing_pv_store_release_hold_remaining_s": safe_float(decision.get("direct_marketing_pv_store_release_hold_remaining_s"), 0.0),
        "direct_marketing_pv_store_release_hold_previous_w": safe_int(decision.get("direct_marketing_pv_store_release_hold_previous_w"), 0),
        "direct_marketing_pv_store_release_hold_offer_w": safe_int(decision.get("direct_marketing_pv_store_release_hold_offer_w"), 0),
        "direct_marketing_pv_store_resync_active": bool(decision.get("direct_marketing_pv_store_resync_active")),
        "direct_marketing_pv_store_resync_reason": decision.get("direct_marketing_pv_store_resync_reason"),
        "direct_marketing_pv_store_resync_gap_w": safe_int(decision.get("direct_marketing_pv_store_resync_gap_w"), 0),
        "direct_marketing_pv_store_resync_threshold_w": safe_int(decision.get("direct_marketing_pv_store_resync_threshold_w"), 0),
        "direct_marketing_pv_store_observed_charge_w": safe_int(decision.get("direct_marketing_pv_store_observed_charge_w"), 0),
        "direct_marketing_pv_store_hold_active": bool(decision.get("direct_marketing_pv_store_hold_active")),
        "direct_marketing_pv_store_min_hold_s": safe_float(decision.get("direct_marketing_pv_store_min_hold_s"), 0.0),
        "direct_marketing_pv_store_state_age_s": safe_float(decision.get("direct_marketing_pv_store_state_age_s"), 0.0),
        "direct_marketing_pv_store_hold_remaining_s": safe_float(decision.get("direct_marketing_pv_store_hold_remaining_s"), 0.0),
        "direct_marketing_owner_switch_cooldown_active": bool(decision.get("direct_marketing_owner_switch_cooldown_active")),
        "direct_marketing_owner_switch_cooldown_s": safe_float(decision.get("direct_marketing_owner_switch_cooldown_s"), 0.0),
        "direct_marketing_owner_switch_cooldown_age_s": safe_float(decision.get("direct_marketing_owner_switch_cooldown_age_s"), 0.0),
        "direct_marketing_owner_switch_cooldown_remaining_s": safe_float(decision.get("direct_marketing_owner_switch_cooldown_remaining_s"), 0.0),
        "direct_marketing_owner_switch_previous_state": decision.get("direct_marketing_owner_switch_previous_state"),
        "direct_marketing_owner_switch_next_state": decision.get("direct_marketing_owner_switch_next_state"),
        "direct_marketing_pv_store_dc_only": bool(decision.get("direct_marketing_pv_store_dc_only")),
        "direct_marketing_pv_store_external_ac_guard_w": safe_int(decision.get("direct_marketing_pv_store_external_ac_guard_w"), 0),
        "direct_marketing_pv_total_w": safe_int(decision.get("direct_marketing_pv_total_w"), 0),
        "direct_marketing_pv_e3dc_w": safe_int(decision.get("direct_marketing_pv_e3dc_w"), 0),
        "direct_marketing_pv_external_ac_w": safe_int(decision.get("direct_marketing_pv_external_ac_w"), 0),
        "direct_marketing_pv_source": decision.get("direct_marketing_pv_source"),
        "direct_marketing_pv_store_dc_surplus_w": safe_int(decision.get("direct_marketing_pv_store_dc_surplus_w"), 0),
        "direct_marketing_pv_store_local_load_after_external_w": safe_int(decision.get("direct_marketing_pv_store_local_load_after_external_w"), 0),
        "direct_marketing_export_target_w": safe_int(decision.get("direct_marketing_export_target_w"), 0),
        "direct_marketing_export_w": safe_int(decision.get("direct_marketing_export_w"), 0),
        "direct_marketing_export_headroom_w": safe_int(decision.get("direct_marketing_export_headroom_w"), 0),
        "direct_marketing_export_discharge_limit_w": safe_int(decision.get("direct_marketing_export_discharge_limit_w"), 0),
        "direct_marketing_export_local_deficit_w": safe_int(decision.get("direct_marketing_export_local_deficit_w"), 0),
        "direct_marketing_export_grid_import_w": safe_int(decision.get("direct_marketing_export_grid_import_w"), 0),
        "direct_marketing_export_import_guard_w": safe_int(decision.get("direct_marketing_export_import_guard_w"), 0),
        "direct_marketing_export_base_w": safe_int(decision.get("direct_marketing_export_base_w"), 0),
        "direct_marketing_export_desired_w": safe_int(decision.get("direct_marketing_export_desired_w"), 0),
        "direct_marketing_export_min_grid_export_w": safe_int(decision.get("direct_marketing_export_min_grid_export_w"), 0),
        "direct_marketing_export_netpoint_deadband_w": safe_int(decision.get("direct_marketing_export_netpoint_deadband_w"), 0),
        "direct_marketing_export_netpoint_release_margin_w": safe_int(decision.get("direct_marketing_export_netpoint_release_margin_w"), 0),
        "direct_marketing_export_grid_export_w": safe_int(decision.get("direct_marketing_export_grid_export_w"), 0),
        "direct_marketing_export_surplus_w": safe_int(decision.get("direct_marketing_export_surplus_w"), 0),
        "direct_marketing_export_grid_error_w": safe_int(decision.get("direct_marketing_export_grid_error_w"), 0),
        "direct_marketing_export_required_by_grid_w": safe_int(decision.get("direct_marketing_export_required_by_grid_w"), 0),
        "direct_marketing_export_required_by_load_w": safe_int(decision.get("direct_marketing_export_required_by_load_w"), 0),
        "direct_marketing_ramp_limited": bool(decision.get("direct_marketing_ramp_limited")),
        "direct_marketing_netpoint_release_hold": bool(decision.get("direct_marketing_netpoint_release_hold")),
        "direct_marketing_hold_active": bool(decision.get("direct_marketing_hold_active")),
        "direct_marketing_profit_hold_ct_per_kwh": safe_float(decision.get("direct_marketing_profit_hold_ct_per_kwh"), 0.0),
        "direct_marketing_margin_hold_pct": safe_float(decision.get("direct_marketing_margin_hold_pct"), 0.0),
        "direct_marketing_hold_profit_floor_ct_per_kwh": safe_float(decision.get("direct_marketing_hold_profit_floor_ct_per_kwh"), 0.0),
        "direct_marketing_hold_margin_floor_pct": safe_float(decision.get("direct_marketing_hold_margin_floor_pct"), 0.0),
        "direct_marketing_ramp_base_w": safe_int(decision.get("direct_marketing_ramp_base_w"), 0),
        "direct_marketing_ramp_step_w": safe_int(decision.get("direct_marketing_ramp_step_w"), 0),
        "direct_marketing_economics": decision.get("direct_marketing_economics") if isinstance(decision.get("direct_marketing_economics"), dict) else {},
        "direct_marketing_monitor": direct_marketing_monitor_state,
        "market_economics_active": bool(decision.get("market_economics_active")),
        "market_economics_owner": decision.get("market_economics_owner"),
        "market_economics_contract_version": decision.get("market_economics_contract_version"),
        "market_economics_action": decision.get("market_economics_action"),
        "market_economics_commands_allowed": decision.get("market_economics_commands_allowed"),
        "market_economics_shadow": decision.get("market_economics_shadow"),
        "market_economics_reason": decision.get("market_economics_reason"),
        "market_economics_blocked_reasons": decision.get("market_economics_blocked_reasons") if isinstance(decision.get("market_economics_blocked_reasons"), list) else [],
        "market_economics_dwell": decision.get("market_economics_dwell") if isinstance(decision.get("market_economics_dwell"), dict) else {},
        "market_economics_dwell_active": bool(decision.get("market_economics_dwell_active")),
        "market_economics_dwell_remaining_s": safe_float(decision.get("market_economics_dwell_remaining_s"), 0.0),
        "market_economics_contract": decision.get("market_economics_contract") if isinstance(decision.get("market_economics_contract"), dict) else {},
        "market_economics_forecast": decision.get("market_economics_forecast") if isinstance(decision.get("market_economics_forecast"), dict) else {},
        "market_economics_economics": decision.get("market_economics_economics") if isinstance(decision.get("market_economics_economics"), dict) else {},
        "market_economics_target_soc_pct": decision.get("market_economics_target_soc_pct"),
        "market_live_pv_first": decision.get("market_live_pv_first") if isinstance(decision.get("market_live_pv_first"), dict) else {},
        "market_live_pv_first_overridden": bool(decision.get("market_live_pv_first_overridden")),
        "market_live_export_absorb_active": bool(decision.get("market_live_export_absorb_active")),
        "market_live_export_absorb_hold_active": bool(decision.get("market_live_export_absorb_hold_active")),
        "market_live_export_absorb_charge_w": max(0, safe_int(decision.get("market_live_export_absorb_charge_w"), 0)),
        "market_live_export_absorb": decision.get("market_live_export_absorb") if isinstance(decision.get("market_live_export_absorb"), dict) else {},
        "market_late_fill_wait_overridden": bool(decision.get("market_late_fill_wait_overridden")),
        "market_forecast_grid_charge_need_wh": safe_float(decision.get("market_forecast_grid_charge_need_wh"), 0.0),
        "curve_release_opening_active": bool(decision.get("curve_release_opening_active")),
        "curve_release_opening_w": max(0, safe_int(decision.get("curve_release_opening_w"), 0)),
        "curve_release_opening_progress": decision.get("curve_release_opening_progress"),
        "curve_release_opening_seconds": decision.get("curve_release_opening_seconds"),
        "curve_release_opening_window_s": decision.get("curve_release_opening_window_s"),
        "curve_release_opening_base_w": max(0, safe_int(decision.get("curve_release_opening_base_w"), 0)),
        "state": state,
        "storage_state": state,
        "reason": str(decision.get("reason") or "")[:160],
        "wb_possible_power_w": wb_possible_w,
        "live_age_s": round(live_age_s, 1),
        "live_stale": live_stale,
        "live_sample_valid": bool(live_plausibility.get("sample_valid", True)),
        "live_sample_invalid": live_sample_invalid,
        "live_plausibility": live_plausibility,
        "live_plausibility_preserved_auto_limit": bool(
            decision.get("live_plausibility_preserved_auto_limit")
        ),
        "live_plausibility_preserved_wbminsoc_contract": bool(
            decision.get("live_plausibility_preserved_wbminsoc_contract")
        ),
        "live_plausibility_manual_override_kept": bool(
            decision.get("live_plausibility_manual_override_kept")
        ),
        "live_plausibility_preserved_discharge_owner": bool(
            decision.get("live_plausibility_preserved_discharge_owner")
        ),
        "live_plausibility_preserved_discharge_state": decision.get(
            "live_plausibility_preserved_discharge_state"
        ),
        "live_plausibility_preserved_discharge_age_s": safe_float(
            decision.get("live_plausibility_preserved_discharge_age_s"),
            0.0,
        ),
        "live_plausibility_preserved_discharge_hold_s": safe_float(
            decision.get("live_plausibility_preserved_discharge_hold_s"),
            0.0,
        ),
        "live_plausibility_preserved_charge_owner": bool(
            decision.get("live_plausibility_preserved_charge_owner")
        ),
        "live_plausibility_preserved_charge_state": decision.get(
            "live_plausibility_preserved_charge_state"
        ),
        "live_plausibility_preserved_charge_age_s": safe_float(
            decision.get("live_plausibility_preserved_charge_age_s"),
            0.0,
        ),
        "live_plausibility_preserved_charge_hold_s": safe_float(
            decision.get("live_plausibility_preserved_charge_hold_s"),
            0.0,
        ),
        "home_power_valid": bool(live_plausibility.get("home_valid", True)),
        "grid_power_valid": bool(live_plausibility.get("grid_valid", True)),
        "home_power_source": live_plausibility.get("home_source"),
        "home_power_balance_w": safe_int(live_plausibility.get("home_balance_w"), 0),
        "home_power_delta_w": safe_int(live_plausibility.get("home_delta_w"), 0),
        "next_manager": True,
        "abregel_charge_req_w": abregel_charge_req_w,
        "abregel_grid_pressure_w": abregel_grid_pressure_w,
        "abregel_physical_pressure_w": abregel_physical_pressure_w,
        "abregel_inverter_pressure_w": abregel_inverter_pressure_w,
        "abregel_grid_error_w": abregel_grid_error_w,
        "abregel_target_w": abregel_target_w,
        "abregel_release_w": abregel_release_w,
        "abregel_rscp_limit_w": abregel_rscp_limit_w,
        "abregel_source": abregel_source,
        "abregel_active": abregel_active,
        "curve_cap_release_hysteresis_w": safe_int(decision.get("curve_cap_release_hysteresis_w"), 0),
        "curve_cap_release_grace_s": safe_float(decision.get("curve_cap_release_grace_s"), 0.0),
        "curve_cap_grid_contract_valid": bool(decision.get("curve_cap_grid_contract_valid")),
        "curve_cap_real_grid_import_active": bool(decision.get("curve_cap_real_grid_import_active")),
        "curve_cap_release_below_active": bool(decision.get("curve_cap_release_below_active")),
        "curve_cap_release_below_since_ts": safe_float(decision.get("curve_cap_release_below_since_ts"), 0.0),
        "curve_cap_release_elapsed_s": safe_float(decision.get("curve_cap_release_elapsed_s"), 0.0),
        "curve_cap_release_grace_active": bool(decision.get("curve_cap_release_grace_active")),
        "curve_cap_release_ramp_active": bool(decision.get("curve_cap_release_ramp_active")),
        "curve_cap_hysteresis_hold_active": bool(decision.get("curve_cap_hysteresis_hold_active")),
        "curve_cap_invalid_hold_active": bool(decision.get("curve_cap_invalid_hold_active")),
        "curve_cap_release_phase": str(decision.get("curve_cap_release_phase") or "inactive"),
        "curve_cap_release_pending": bool(decision.get("curve_cap_release_pending")),
        "curve_cap_release_requested": bool(decision.get("curve_cap_release_requested")),
        "curve_cap_release_confirmed_since_ts": safe_float(decision.get("curve_cap_release_confirmed_since_ts"), 0.0),
        "curve_cap_post_release_until_ts": safe_float(decision.get("curve_cap_post_release_until_ts"), 0.0),
        "curve_cap_post_release_guard_active": bool(decision.get("curve_cap_post_release_guard_active")),
        "curve_cap_post_release_reentry_blocked": bool(decision.get("curve_cap_post_release_reentry_blocked")),
        "curve_cap_settings_readback_valid": bool(decision.get("curve_cap_settings_readback_valid")),
        "curve_cap_settings_bounded_zero_confirmed": bool(decision.get("curve_cap_settings_bounded_zero_confirmed")),
        "curve_cap_settings_release_confirmed": bool(decision.get("curve_cap_settings_release_confirmed")),
        "energy_score": {
            "pv_surplus_w": pv_after_fixed_w,
            "free_for_limbs_w": flexible_consumer_budget_w,
            "free_for_consumers_raw_w": budget_w,
            "free_for_limbs_raw_w": raw_iaval_w,
            "bat_charge_request_w": storage_charge_request_w,
            "wallbox_phase_transition_reserved_w": phase_transition_reserved_w,
            "abregel_charge_request_w": abregel_charge_req_w,
            "abregel_grid_pressure_w": abregel_grid_pressure_w,
            "abregel_physical_pressure_w": abregel_physical_pressure_w,
            "abregel_inverter_pressure_w": abregel_inverter_pressure_w,
            "abregel_grid_error_w": abregel_grid_error_w,
            "abregel_target_w": abregel_target_w,
            "abregel_release_w": abregel_release_w,
            "prio_factor": 1.0,
            "prio_reason": str(decision.get("priority") or "next"),
        },
    }
    if phase_transition_allocations:
        budget["consumer_allocations"] = phase_transition_allocations
        budget["consumer_priority_order"] = ["wallbox", "heatpump", "heater"]
        budget["consumer_priority_effective_order"] = ["wallbox", "heatpump", "heater"]
        budget["energy_score"]["consumer_allocations"] = dict(phase_transition_allocations)
    if decision.get("force_wallbox_stop"):
        budget["force_wallbox_stop"] = True
    if decision.get("predump_floor_hold"):
        budget["predump_floor_hold"] = True
    if "predump_allow" in decision:
        predump_allow = decision["predump_allow"] if isinstance(decision["predump_allow"], dict) else {}
        budget["predump_active"] = bool(decision.get("predump_active") or str(state).startswith("pre_discharge"))
        budget["predump_allow"] = predump_allow
        budget["predump_allow_wallbox"] = bool(predump_allow.get("wallbox"))
        budget["predump_allow_heatpump"] = bool(predump_allow.get("heatpump"))
        budget["predump_allow_heater"] = bool(predump_allow.get("heater"))
        budget["predump_target_soc"] = decision.get("predump_target_soc")
        budget["predump_floor_soc"] = decision.get("predump_floor_soc", decision.get("predump_target_soc"))
        budget["predump_consumer_landing_under_pct"] = decision.get("predump_consumer_landing_under_pct")
        budget["predump_consumer_landing_under_wh"] = decision.get("predump_consumer_landing_under_wh")
        budget["predump_no_grid"] = bool(decision.get("predump_no_grid"))
        budget["predump_grid_fallback"] = bool(decision.get("predump_grid_fallback"))
        budget["predump_hard_predump"] = bool(decision.get("predump_hard_predump"))
        budget["predump_grid_allowed"] = bool(decision.get("predump_grid_allowed"))
        budget["predump_grid_blocked_by_comfort"] = bool(decision.get("predump_grid_blocked_by_comfort"))
        budget["predump_grid_export_target_w"] = safe_int(decision.get("predump_grid_export_target_w"), 0)
        budget["predump_grid_export_w"] = safe_int(decision.get("predump_grid_export_w"), 0)
        budget["predump_grid_export_headroom_w"] = safe_int(decision.get("predump_grid_export_headroom_w"), 0)
        budget["predump_grid_base_export_w"] = safe_int(decision.get("predump_grid_base_export_w"), 0)
        budget["predump_grid_battery_discharge_w"] = safe_int(decision.get("predump_grid_battery_discharge_w"), 0)
        budget["predump_grid_discharge_limit_w"] = safe_int(decision.get("predump_grid_discharge_limit_w"), 0)
        budget["predump_grid_ramp_base_w"] = safe_int(decision.get("predump_grid_ramp_base_w"), 0)
        budget["predump_grid_ramp_step_w"] = safe_int(decision.get("predump_grid_ramp_step_w"), 0)
        budget["predump_hard_grid_limit_w"] = safe_int(decision.get("predump_hard_grid_limit_w"), 0)
        budget["predump_hard_grid_uncapped_w"] = safe_int(decision.get("predump_hard_grid_uncapped_w"), 0)
        budget["predump_hard_grid_limited"] = bool(decision.get("predump_hard_grid_limited"))
        budget["predump_consumer_load_w"] = safe_int(decision.get("predump_consumer_load_w"), 0)
        budget["predump_wallbox_min_power_w"] = safe_int(decision.get("predump_wallbox_min_power_w"), 0)
        budget["predump_wallbox_min_amp"] = safe_int(decision.get("predump_wallbox_min_amp"), 0)
        budget["predump_wallbox_min_phases"] = safe_int(decision.get("predump_wallbox_min_phases"), 0)
        budget["predump_bev_block_w"] = safe_int(decision.get("predump_bev_block_w"), 0)
        budget["predump_bev_start_ts"] = decision.get("predump_bev_start_ts")
        budget["predump_bev_grid_latest_ts"] = decision.get("predump_bev_grid_latest_ts")
        budget["predump_bev_remaining_wh"] = decision.get("predump_bev_remaining_wh")
        budget["predump_grid_latest_ts"] = decision.get("predump_grid_latest_ts")
        budget["predump_grid_remaining_wh"] = decision.get("predump_grid_remaining_wh")
        budget["predump_grid_duration_s"] = decision.get("predump_grid_duration_s")
        if isinstance(decision.get("home_feedback_guard"), dict):
            budget["home_feedback_guard"] = decision["home_feedback_guard"]
        if budget["predump_active"] and budget["predump_allow_wallbox"] and not budget["predump_grid_fallback"]:
            budget["force_wb_mode"] = 10
    previous_parallel_state = str(
        previous_state.get("parallel_state")
        or (
            previous_state.get("state")
            if str(previous_state.get("state") or "").startswith("parallel_")
            else ""
        )
        or ""
    )
    previous_parallel_since_ts = safe_float(
        previous_state.get("parallel_state_since_ts"),
        0.0,
    )
    if previous_parallel_since_ts <= 0 and previous_parallel_state:
        previous_parallel_since_ts = safe_float(previous_state.get("ts"), 0.0)
    parallel_state_since_ts = (
        previous_parallel_since_ts
        if previous_parallel_state == state and previous_parallel_since_ts > 0
        else now_s
    )
    predump_reopen_block = {}
    current_predump_state = str(state or "").startswith("pre_discharge")
    if not current_predump_state:
        previous_block_until = safe_float(previous_state.get("predump_reopen_block_until_ts"), 0.0)
        if previous_block_until > now_s:
            predump_reopen_block = {
                "active": True,
                "until_ts": previous_block_until,
                "target_soc": safe_float(previous_state.get("predump_reopen_target_soc"), 0.0),
                "floor_soc": safe_float(previous_state.get("predump_reopen_floor_soc"), 0.0),
                "reason": str(previous_state.get("predump_reopen_block_reason") or "Pre-Dump Reopen-Sperre aktiv"),
            }
        elif str(previous_state.get("state") or "").startswith("pre_discharge"):
            predump_target_soc = safe_float(
                previous_state.get("predump_floor_soc", previous_state.get("predump_target_soc")),
                -1.0,
            )
            block_s = predump_reopen_block_s(cfg)
            if predump_target_soc >= 0.0 and block_s > 0.0:
                margin_pct = predump_reopen_margin_pct(cfg)
                predump_reopen_block = {
                    "active": True,
                    "until_ts": now_s + block_s,
                    "target_soc": predump_target_soc,
                    "floor_soc": min(100.0, predump_target_soc + margin_pct),
                    "reason": (
                        "Pre-Dump Zielkante erreicht: Reopen-Sperre gegen SoC-Rundungsflattern"
                    ),
                }
    hardening_contracts = runtime_hardening_contracts(
        plan,
        wb_native,
        wb_budget_context,
        wb_intent_fresh=wb_intent_fresh,
        wb_mode=wb_mode,
        decision=decision,
    )
    budget["hardening_contracts_version"] = hardening_contracts.get("version")
    budget["hardening_contracts_scope"] = hardening_contracts.get("scope")
    budget["hardening_contracts"] = hardening_contracts.get("contracts", {})

    payload = {
        "ts": int(now_s),
        "state": state,
        "mode": mode,
        "mode_name": mode_label(mode),
        "val": val,
        "reason": str(decision.get("reason") or ""),
        "priority": str(decision.get("priority") or "default"),
        "protected": bool(decision.get("protected")),
        "diagnostic_signature": decision.get("diagnostic_signature"),
        "decision_owner": decision.get("decision_owner"),
        "safety_veto": bool(decision.get("safety_veto")),
        "discharge_allowed": decision.get("discharge_allowed"),
        "export_allowed": decision.get("export_allowed"),
        "ep_reserve_floor_hold": bool(decision.get("ep_reserve_floor_hold")),
        "soc": soc,
        "curve_control_soc": curve_control_soc,
        "curve_control_raw_soc": soc,
        "curve_control_soc_ts": now_s,
        "pv_w": pv_w,
        "grid_w": grid_w,
        "home_w": home_w,
        "wp_w": wp_w,
        "bat_w": bat_w,
        "ep_reserve_pct": ep_reserve_soc(cfg, live),
        "wallbox_w": wallbox_w,
        "wallbox_power_source": wallbox_power_source,
        "wallbox_live_w": live_wallbox_w,
        "wallbox_native_w": native_wallbox_w,
        "wallbox_home_includes": home_includes_wallbox,
        "live_age_s": round(live_age_s, 1),
        "live_stale": live_stale,
        "live_sample_valid": bool(live_plausibility.get("sample_valid", True)),
        "live_sample_invalid": live_sample_invalid,
        "live_plausibility": live_plausibility,
        "live_plausibility_preserved_auto_limit": bool(
            decision.get("live_plausibility_preserved_auto_limit")
        ),
        "live_plausibility_preserved_wbminsoc_contract": bool(
            decision.get("live_plausibility_preserved_wbminsoc_contract")
        ),
        "live_plausibility_manual_override_kept": bool(
            decision.get("live_plausibility_manual_override_kept")
        ),
        "live_plausibility_preserved_discharge_owner": bool(
            decision.get("live_plausibility_preserved_discharge_owner")
        ),
        "live_plausibility_preserved_discharge_state": decision.get(
            "live_plausibility_preserved_discharge_state"
        ),
        "live_plausibility_preserved_discharge_age_s": safe_float(
            decision.get("live_plausibility_preserved_discharge_age_s"),
            0.0,
        ),
        "live_plausibility_preserved_discharge_hold_s": safe_float(
            decision.get("live_plausibility_preserved_discharge_hold_s"),
            0.0,
        ),
        "live_plausibility_preserved_charge_owner": bool(
            decision.get("live_plausibility_preserved_charge_owner")
        ),
        "live_plausibility_preserved_charge_state": decision.get(
            "live_plausibility_preserved_charge_state"
        ),
        "live_plausibility_preserved_charge_age_s": safe_float(
            decision.get("live_plausibility_preserved_charge_age_s"),
            0.0,
        ),
        "live_plausibility_preserved_charge_hold_s": safe_float(
            decision.get("live_plausibility_preserved_charge_hold_s"),
            0.0,
        ),
        "home_power_valid": bool(live_plausibility.get("home_valid", True)),
        "grid_power_valid": bool(live_plausibility.get("grid_valid", True)),
        "home_power_source": live_plausibility.get("home_source"),
        "home_power_balance_w": safe_int(live_plausibility.get("home_balance_w"), 0),
        "home_power_delta_w": safe_int(live_plausibility.get("home_delta_w"), 0),
        "soc_unrealistic": soc_unrealistic,
        "soc_jump_guard": soc_jump_guard if soc_unrealistic else None,
        "last_valid_soc": soc_jump_guard.get("last_valid_soc", soc),
        "last_valid_soc_ts": soc_jump_guard.get("last_valid_soc_ts", now_s),
        "ems_power_limits_active": bool(live.get("power_limits_active")) if "power_limits_active" in live else None,
        "ems_power_settings_read": live.get("ems_power_settings_read") is True,
        "ems_power_settings_valid": live.get("ems_power_settings_valid") is True,
        "ems_max_charge_power_w": live.get("ems_max_charge_power_w"),
        "ems_max_discharge_power_w": live.get("ems_max_discharge_power_w"),
        "ems_discharge_start_power_w": live.get("ems_discharge_start_power_w"),
        "used_charge_limit_w": live.get("used_charge_limit_w"),
        "remaining_charge_w": live.get("remaining_charge_w"),
        "used_discharge_limit_w": live.get("used_discharge_limit_w"),
        "remaining_discharge_w": live.get("remaining_discharge_w"),
        "wb_car_present": wb_car_present,
        "wb_possible_power_w": wb_possible_w,
        "wb_min_required_w": phase_transition_requested_w,
        "wallbox_phase_transition_active": bool(wallbox_phase_transition.get("active")),
        "wallbox_phase_transition_reserved_w": phase_transition_reserved_w,
        "wallbox_phase_transition_flexible_budget_w": flexible_consumer_budget_w,
        "wallbox_phase_transition_target_phases": safe_int(wallbox_phase_transition.get("target_phases"), 0),
        "wallbox_phase_transition_until_ts": safe_float(wallbox_phase_transition.get("expires_ts"), 0.0),
        "wallbox_phase_transition_source": str(wallbox_phase_transition.get("source") or ""),
        "wallbox_phase_transition": wallbox_phase_transition,
        "wallbox_phase_transition_grants": phase_transition_grants,
        "wallbox_phase_transition_requested_w_total": phase_transition_requested_w,
        "wallbox_phase_transition_reserved_w_total": phase_transition_reserved_w,
        "heatpump_running": heatpump_running,
        "heatpump_running_commitment_w": heatpump_running_commitment_w,
        "heatpump_starting": heatpump_starting,
        "heatpump_starting_until_ts": heatpump_starting_until_ts,
        "heatpump_new_start_allowed": not bool(wallbox_phase_transition.get("active")),
        "flexible_budget_after_commitments_w": flexible_consumer_budget_w,
        "curve_soc": curve_soc,
        "target_soc": target_soc,
        "target_ts": target_ts,
        "curve_release_ts": curve_release_ts_s(plan),
        "iFc_w": i_fc_w,
        "iMinLade_w": i_min_lade_w,
        "storage_charge_request_w": storage_charge_request_w,
        "curve_auto_hold_continuation_active": bool(decision.get("curve_auto_hold_continuation_active")),
        "curve_auto_hold_continuation_w": max(0, safe_int(decision.get("curve_auto_hold_continuation_w"), 0)),
        "curve_auto_hold_continuation_previous_w": max(
            0,
            safe_int(decision.get("curve_auto_hold_continuation_previous_w"), 0),
        ),
        "curve_auto_hold_continuation_decay_w": max(
            0,
            safe_int(decision.get("curve_auto_hold_continuation_decay_w"), 0),
        ),
        "curve_auto_hold_continuation_offer_w": max(
            0,
            safe_int(decision.get("curve_auto_hold_continuation_offer_w"), 0),
        ),
        "next_curve_evening_pv_release_active": bool(decision.get("next_curve_evening_pv_release_active")),
        "next_curve_evening_pv_release_seconds_to_first_curve": max(
            0,
            safe_int(decision.get("next_curve_evening_pv_release_seconds_to_first_curve"), 0),
        ),
        "next_curve_evening_pv_release_offer_w": max(
            0,
            safe_int(decision.get("next_curve_evening_pv_release_offer_w"), 0),
        ),
        "next_curve_evening_pv_release_max_lead_s": max(
            0,
            safe_int(decision.get("next_curve_evening_pv_release_max_lead_s"), 0),
        ),
        "wallbox_curve_reserve_w": max(0, safe_int(decision.get("wallbox_curve_reserve_w"), 0)),
        "wallbox_curve_reserve_target_w": max(0, safe_int(decision.get("wallbox_curve_reserve_target_w"), 0)),
        "wallbox_curve_reserve_step_w": max(0, safe_int(decision.get("wallbox_curve_reserve_step_w"), 0)),
        "external_wallbox_curve_storage_protect": bool(decision.get("external_wallbox_curve_storage_protect")),
        "wallbox_curve_discharge_protect": bool(decision.get("wallbox_curve_discharge_protect")),
        "observe_wallbox_storage_policy": decision.get("observe_wallbox_storage_policy"),
        "observe_wallbox_reserve_release_active": bool(decision.get("observe_wallbox_reserve_release_active")),
        "observe_wallbox_reserve_floor_soc": decision.get("observe_wallbox_reserve_floor_soc"),
        "observe_wallbox_reserve_release_floor_soc": decision.get("observe_wallbox_reserve_release_floor_soc"),
        "observe_wallbox_reserve_soc": decision.get("observe_wallbox_reserve_soc"),
        "wallbox_curve_pv_only": bool(decision.get("wallbox_curve_pv_only")),
        "target_corridor_fast_charge_active": bool(decision.get("target_corridor_fast_charge_active")),
        "market_economics_active": bool(decision.get("market_economics_active")),
        "market_economics_owner": decision.get("market_economics_owner"),
        "market_economics_contract_version": decision.get("market_economics_contract_version"),
        "market_economics_action": decision.get("market_economics_action"),
        "market_economics_commands_allowed": decision.get("market_economics_commands_allowed"),
        "market_economics_shadow": decision.get("market_economics_shadow"),
        "market_economics_reason": decision.get("market_economics_reason"),
        "market_economics_blocked_reasons": decision.get("market_economics_blocked_reasons") if isinstance(decision.get("market_economics_blocked_reasons"), list) else [],
        "market_economics_dwell": decision.get("market_economics_dwell") if isinstance(decision.get("market_economics_dwell"), dict) else {},
        "market_economics_dwell_active": bool(decision.get("market_economics_dwell_active")),
        "market_economics_dwell_remaining_s": safe_float(decision.get("market_economics_dwell_remaining_s"), 0.0),
        "market_economics_contract": decision.get("market_economics_contract") if isinstance(decision.get("market_economics_contract"), dict) else {},
        "market_economics_forecast": decision.get("market_economics_forecast") if isinstance(decision.get("market_economics_forecast"), dict) else {},
        "market_economics_economics": decision.get("market_economics_economics") if isinstance(decision.get("market_economics_economics"), dict) else {},
        "market_economics_target_soc_pct": decision.get("market_economics_target_soc_pct"),
        "market_live_pv_first": decision.get("market_live_pv_first") if isinstance(decision.get("market_live_pv_first"), dict) else {},
        "target_corridor_storage_req_w": max(0, safe_int(decision.get("target_corridor_storage_req_w"), 0)),
        "controlled_wallbox_wbminsoc_pause": bool(decision.get("controlled_wallbox_wbminsoc_pause")),
        "controlled_wallbox_auto_freerun": bool(decision.get("controlled_wallbox_auto_freerun")),
        "controlled_wallbox_auto_original_state": str(decision.get("controlled_wallbox_auto_original_state") or ""),
        "wbminsoc_pv_charge_active": bool(decision.get("wbminsoc_pv_charge_active")),
        "planned_grid_pv_charge_w": max(0, safe_int(decision.get("planned_grid_pv_charge_w"), 0)),
        "planned_grid_pv_surplus_w": max(0, safe_int(decision.get("planned_grid_pv_surplus_w"), 0)),
        "pv_house_surplus_w": max(0, safe_int(decision.get("pv_house_surplus_w"), 0)),
        "scheduled_grid_charge": bool(decision.get("scheduled_grid_charge")),
        "wbminsoc_transition_dwell_active": bool(decision.get("wbminsoc_transition_dwell_active")),
        "wbminsoc_curve_dwell_active": bool(decision.get("wbminsoc_curve_dwell_active")),
        "wbminsoc_previous_state_age_s": round(safe_float(decision.get("wbminsoc_previous_state_age_s"), 0.0), 1),
        "wbminsoc_effective_pv_charge_threshold_w": max(
            0,
            safe_int(decision.get("wbminsoc_effective_pv_charge_threshold_w"), 0),
        ),
        "house_heatpump_discharge_cap_w": max(0, safe_int(decision.get("house_heatpump_discharge_cap_w"), 0)),
        "late_shortfall_auto_release": bool(decision.get("late_shortfall_auto_release")),
        "late_shortfall_seconds_to_release": decision.get("late_shortfall_seconds_to_release"),
        "curve_release_opening_active": bool(decision.get("curve_release_opening_active")),
        "curve_release_opening_w": max(0, safe_int(decision.get("curve_release_opening_w"), 0)),
        "curve_release_opening_progress": decision.get("curve_release_opening_progress"),
        "curve_release_opening_seconds": decision.get("curve_release_opening_seconds"),
        "curve_release_opening_window_s": decision.get("curve_release_opening_window_s"),
        "curve_release_opening_base_w": max(0, safe_int(decision.get("curve_release_opening_base_w"), 0)),
        "curve_auto_hold_charge_offer_w": curve_auto_hold_charge_offer_w,
        "curve_auto_hold_charge_offer_threshold_w": curve_auto_hold_charge_offer_threshold_w,
        "curve_auto_hold_charge_offer_active": curve_auto_hold_charge_offer_active,
        "curve_gap_pct": curve_gap_diag_pct,
        "curve_gap_catchup_w": curve_gap_catchup_w,
        "curve_gap_catchup_cap_w": curve_gap_catchup_cap_w,
        "curve_gap_catchup_factor": curve_gap_catchup_factor,
        "curve_gap_catchup_min_w": curve_gap_catchup_min_w,
        "curve_gap_catchup_taper_pct": curve_gap_catchup_taper_pct,
        "curve_need_raw_w": curve_need_raw_w,
        "lookahead_need_w": lookahead_need_w,
        "curve_hard_anchor_need_w": curve_hard_anchor_need_w,
        "curve_hard_anchor_gap_pct": curve_hard_anchor_gap_pct,
        "curve_hard_anchor_missed": curve_hard_anchor_missed,
        "curve_hard_anchor_mode": curve_hard_anchor_mode,
        "curve_hard_anchor_soc": curve_hard_anchor_soc,
        "curve_hard_anchor_ts": curve_hard_anchor_ts,
        "curve_frame_base_smoothing_active": curve_frame_base_smoothing_active,
        "curve_frame_base_smoothing_phase": curve_frame_base_smoothing_phase,
        "curve_frame_base_smoothing_desired_w": curve_frame_base_smoothing_desired_w,
        "curve_frame_base_smoothing_previous_w": curve_frame_base_smoothing_previous_w,
        "curve_frame_base_smoothing_hold_band_w": curve_frame_base_smoothing_hold_band_w,
        "curve_frame_base_smoothing_step_w": curve_frame_base_smoothing_step_w,
        "adaptive_curve_active": adaptive_curve_active,
        "adaptive_curve_relation": adaptive_curve_relation,
        "adaptive_soc_floor": adaptive_floor_soc,
        "adaptive_soc_ceiling": adaptive_ceiling_soc,
        "adaptive_latest_charge_due": adaptive_latest_charge_due,
        "latest_charge_start_ts": adaptive_latest_charge_start_ts,
        "latest_charge_start_clamped": bool(adaptive_latest_charge_clamped),
        "latest_charge_start_raw_ts": adaptive_latest_charge_raw_ts,
        "latest_charge_start_previous_ts": adaptive_latest_charge_previous_ts,
        "evening_shortfall_wh": round(adaptive_evening_shortfall_wh, 0),
        "forecast_only_target_active": bool(forecast_only_target_active),
        "forecast_curve_landing_hold_active": bool(forecast_curve_landing_hold_active),
        "forecast_floor_target_gap_pct": round(forecast_floor_target_gap_pct, 3),
        "forecast_landing_margin_pct": round(forecast_landing_margin_pct, 3),
        "sliding_horizon_active": bool(budget.get("sliding_horizon_active")),
        "sliding_horizon_reason": str(budget.get("sliding_horizon_reason") or ""),
        "sliding_horizon_confidence": safe_float(budget.get("sliding_horizon_confidence"), 0.0),
        "sliding_horizon_min_confidence": safe_float(budget.get("sliding_horizon_min_confidence"), 0.0),
        "sliding_horizon_season": str(budget.get("sliding_horizon_season") or ""),
        "sliding_horizon_minutes_until_latest_charge": budget.get("sliding_horizon_minutes_until_latest_charge"),
        "sliding_horizon_headroom_available_wh": round(safe_float(budget.get("sliding_horizon_headroom_available_wh"), 0.0), 0),
        "sliding_horizon_uncovered_pressure_wh": round(safe_float(budget.get("sliding_horizon_uncovered_pressure_wh"), 0.0), 0),
        "sliding_horizon_uncovered_curtailment_pressure_wh": round(
            safe_float(budget.get("sliding_horizon_uncovered_curtailment_pressure_wh"), 0.0),
            0,
        ),
        "adaptive_headroom_required_wh": round(adaptive_headroom_required_wh, 0),
        "adaptive_headroom_available_wh": round(adaptive_headroom_available_wh, 0),
        "curtailment_pressure_wh": round(adaptive_curtailment_pressure_wh, 0),
        "curtailment_unavoidable_wh": round(adaptive_curtailment_unavoidable_wh, 0),
        "headroom_reserve_active": bool(headroom_reserve_active),
        "headroom_reserve_pressure_wh": round(headroom_reserve_pressure_wh, 0),
        "headroom_reserve_source": headroom_reserve_source,
        "headroom_discharge_active": bool(budget.get("headroom_discharge_active")),
        "headroom_discharge_candidate": bool(budget.get("headroom_discharge_candidate")),
        "headroom_discharge_w": max(0, safe_int(budget.get("headroom_discharge_w"), 0)),
        "headroom_discharge_target_w": max(0, safe_int(budget.get("headroom_discharge_target_w"), 0)),
        "headroom_discharge_floor_soc": budget.get("headroom_discharge_floor_soc"),
        "headroom_discharge_gap_pct": safe_float(budget.get("headroom_discharge_gap_pct"), 0.0),
        "headroom_discharge_export_room_w": max(0, safe_int(budget.get("headroom_discharge_export_room_w"), 0)),
        "headroom_discharge_pressure_wh": round(
            max(0.0, safe_float(budget.get("headroom_discharge_pressure_wh"), 0.0)),
            0,
        ),
        "headroom_discharge_min_pressure_wh": round(
            max(0.0, safe_float(budget.get("headroom_discharge_min_pressure_wh"), 0.0)),
            0,
        ),
        "headroom_discharge_blocked_reason": str(budget.get("headroom_discharge_blocked_reason") or ""),
        "headroom_discharge_target_plateau_reached": bool(budget.get("headroom_discharge_target_plateau_reached")),
        "headroom_discharge_target_curve_soc": budget.get("headroom_discharge_target_curve_soc"),
        "headroom_discharge_target_plateau_margin_pct": budget.get("headroom_discharge_target_plateau_margin_pct", 0.0),
        "headroom_discharge_abregel_blocked": bool(budget.get("headroom_discharge_abregel_blocked")),
        "headroom_discharge_day": budget.get("headroom_discharge_day", ""),
        "headroom_discharge_today_wh": budget.get("headroom_discharge_today_wh", 0.0),
        "headroom_discharge_daily_limit_wh": budget.get("headroom_discharge_daily_limit_wh", 0.0),
        "headroom_discharge_daily_remaining_wh": budget.get("headroom_discharge_daily_remaining_wh", 0.0),
        "headroom_discharge_daily_limit_pct": budget.get("headroom_discharge_daily_limit_pct", 0.0),
        "headroom_discharge_daily_blocked": bool(budget.get("headroom_discharge_daily_blocked")),
        "headroom_discharge_cooldown_s": budget.get("headroom_discharge_cooldown_s", 0),
        "headroom_discharge_cooldown_remaining_s": budget.get("headroom_discharge_cooldown_remaining_s", 0.0),
        "headroom_discharge_cooldown_active": bool(budget.get("headroom_discharge_cooldown_active")),
        "headroom_discharge_last_active_ts": budget.get("headroom_discharge_last_active_ts", 0.0),
        "headroom_discharge_last_account_ts": budget.get("headroom_discharge_last_account_ts", now_s),
        "headroom_execution_schema_version": budget.get("headroom_execution_schema_version"),
        "headroom_execution_allowed": bool(budget.get("headroom_execution_allowed")),
        "headroom_execution_reason_code": str(
            budget.get("headroom_execution_reason_code") or "HEADROOM_EXECUTION_CONTRACT_MISSING"
        ),
        "headroom_execution_plan_id": budget.get("headroom_execution_plan_id"),
        "headroom_execution_slot_id": budget.get("headroom_execution_slot_id"),
        "headroom_execution_earliest_start_ts": budget.get("headroom_execution_earliest_start_ts"),
        "headroom_execution_deadline_ts": budget.get("headroom_execution_deadline_ts"),
        "headroom_execution_target_soc": budget.get("headroom_execution_target_soc"),
        "headroom_execution_hard_floor_soc": budget.get("headroom_execution_hard_floor_soc"),
        "headroom_execution_plan_accounted_wh": budget.get("headroom_execution_plan_accounted_wh", 0.0),
        "headroom_execution_slot_accounted_wh": budget.get("headroom_execution_slot_accounted_wh", 0.0),
        "headroom_execution_residual_wh": budget.get("headroom_execution_residual_wh", 0.0),
        "headroom_execution_accounted_observed_w": budget.get("headroom_execution_accounted_observed_w", 0.0),
        "headroom_execution_accounted_interval_s": budget.get("headroom_execution_accounted_interval_s", 0.0),
        "headroom_execution_generation_reset": bool(budget.get("headroom_execution_generation_reset", True)),
        "headroom_execution_last_account_ts": budget.get("headroom_execution_last_account_ts", now_s),
        "curve_frame_lift_active": bool(decision.get("curve_frame_lift_active")),
        "curve_frame_lift_w": curve_frame_lift_w,
        "curve_frame_lift_desired_w": curve_frame_lift_desired_w,
        "curve_frame_lift_actual_w": curve_frame_lift_actual_w,
        "curve_frame_lift_shortfall_w": curve_frame_lift_shortfall_w,
        "curve_frame_lift_step_w": curve_frame_lift_step_w,
        "curve_frame_lift_max_boost_w": curve_frame_lift_max_boost_w,
        "curve_frame_lift_reason": curve_frame_lift_reason,
        "curve_frame_lift_gap_pct": curve_frame_lift_gap_pct,
        "curve_frame_measured_trim_phase": curve_frame_measured_trim_phase,
        "curve_frame_measured_trim_offset_w": curve_frame_measured_trim_offset_w,
        "curve_frame_measured_trim_anchor_ts": curve_frame_measured_trim_anchor_ts,
        "curve_frame_measured_trim_hold_until_ts": curve_frame_measured_trim_hold_until_ts,
        "curve_auto_hold_charge_offer_w": curve_auto_hold_charge_offer_w,
        "curve_auto_hold_charge_offer_threshold_w": curve_auto_hold_charge_offer_threshold_w,
        "curve_auto_hold_charge_offer_active": curve_auto_hold_charge_offer_active,
        "planned_load_expected_w": safe_int(decision.get("planned_load_expected_w"), 0),
        "planned_load_observed_extra_w": safe_int(decision.get("planned_load_observed_extra_w"), 0),
        "planned_load_windows": decision.get("planned_load_windows", []),
        "planned_load_names": decision.get("planned_load_names", []),
        "planned_load_support": decision.get("planned_load_support", {}),
        "heatpump_pause_request": heatpump_pause_request,
        "direct_marketing_active": bool(decision.get("direct_marketing_active")),
        "direct_marketing_policy_active": bool(decision.get("direct_marketing_policy_active")),
        "direct_marketing_policy_schema": decision.get("direct_marketing_policy_schema"),
        "direct_marketing_policy_decision": decision.get("direct_marketing_policy_decision") if isinstance(decision.get("direct_marketing_policy_decision"), dict) else None,
        "direct_marketing_policy_target_state": decision.get("direct_marketing_policy_target_state"),
        "direct_marketing_policy_block_reason": decision.get("direct_marketing_policy_block_reason"),
        "direct_marketing_policy_export_budget_w": safe_int(decision.get("direct_marketing_policy_export_budget_w"), 0),
        "direct_marketing_policy_charge_budget_w": safe_int(decision.get("direct_marketing_policy_charge_budget_w"), 0),
        "direct_marketing_policy_protected_reserve_wh": safe_int(decision.get("direct_marketing_policy_protected_reserve_wh"), 0),
        "direct_marketing_policy_sellable_wh": safe_int(decision.get("direct_marketing_policy_sellable_wh"), 0),
        "direct_marketing_policy_executor_gate": decision.get("direct_marketing_policy_executor_gate") if isinstance(decision.get("direct_marketing_policy_executor_gate"), dict) else None,
        "direct_marketing_future_pv_store_reservation": decision.get("direct_marketing_future_pv_store_reservation") if isinstance(decision.get("direct_marketing_future_pv_store_reservation"), dict) else None,
        "direct_marketing_future_pv_store_reservation_active": bool(decision.get("direct_marketing_future_pv_store_reservation_active")),
        "direct_marketing_future_pv_store_reservation_cap_w": safe_int(decision.get("direct_marketing_future_pv_store_reservation_cap_w"), 0),
        "direct_marketing_mode": decision.get("direct_marketing_mode"),
        "direct_marketing_owner": decision.get("direct_marketing_owner"),
        "direct_marketing_contract_version": decision.get("direct_marketing_contract_version"),
        "direct_marketing_action": decision.get("direct_marketing_action"),
        "direct_marketing_window": decision.get("direct_marketing_window"),
        "direct_marketing_profit_ct_per_kwh": decision.get("direct_marketing_profit_ct_per_kwh"),
        "direct_marketing_reserve_floor_soc_pct": decision.get("direct_marketing_reserve_floor_soc_pct"),
        "direct_marketing_target_soc_pct": decision.get("direct_marketing_target_soc_pct"),
        "direct_marketing_headroom_hold_active": bool(decision.get("direct_marketing_headroom_hold_active")),
        "direct_marketing_headroom_soc_ceiling_pct": safe_float(decision.get("direct_marketing_headroom_soc_ceiling_pct"), 0.0),
        "direct_marketing_headroom_deficit_wh": safe_int(decision.get("direct_marketing_headroom_deficit_wh"), 0),
        "direct_marketing_headroom_next_start_ts": safe_int(decision.get("direct_marketing_headroom_next_start_ts"), 0),
        "direct_marketing_headroom_window_min": safe_float(decision.get("direct_marketing_headroom_window_min"), 0.0),
        "direct_marketing_headroom_forecast_surplus_wh": safe_int(decision.get("direct_marketing_headroom_forecast_surplus_wh"), 0),
        "direct_marketing_headroom_required_pct": safe_float(decision.get("direct_marketing_headroom_required_pct"), 0.0),
        "direct_marketing_pv_store_w": safe_int(decision.get("direct_marketing_pv_store_w"), 0),
        "direct_marketing_pv_store_offer_w": safe_int(decision.get("direct_marketing_pv_store_offer_w"), 0),
        "direct_marketing_pv_store_max_w": safe_int(decision.get("direct_marketing_pv_store_max_w"), 0),
        "direct_marketing_pv_store_surplus_w": safe_int(decision.get("direct_marketing_pv_store_surplus_w"), 0),
        "direct_marketing_pv_store_grid_import_w": safe_int(decision.get("direct_marketing_pv_store_grid_import_w"), 0),
        "direct_marketing_pv_store_grid_export_w": safe_int(decision.get("direct_marketing_pv_store_grid_export_w"), 0),
        "direct_marketing_pv_store_import_guard_w": safe_int(decision.get("direct_marketing_pv_store_import_guard_w"), 0),
        "direct_marketing_pv_store_min_surplus_w": safe_int(decision.get("direct_marketing_pv_store_min_surplus_w"), 0),
        "direct_marketing_pv_store_requested_w": safe_int(decision.get("direct_marketing_pv_store_requested_w"), 0),
        "direct_marketing_pv_store_target_fallback_active": bool(decision.get("direct_marketing_pv_store_target_fallback_active")),
        "direct_marketing_pv_store_estimated_offer_w": safe_int(decision.get("direct_marketing_pv_store_estimated_offer_w"), 0),
        "direct_marketing_pv_store_pv_safe_cap_w": safe_int(decision.get("direct_marketing_pv_store_pv_safe_cap_w"), 0),
        "direct_marketing_pv_store_self_reference_limited": bool(decision.get("direct_marketing_pv_store_self_reference_limited")),
        "direct_marketing_pv_store_execution": str(decision.get("direct_marketing_pv_store_execution") or ""),
        "direct_marketing_pv_store_auto_limit_active": bool(decision.get("direct_marketing_pv_store_auto_limit_active")),
        "direct_marketing_pv_store_external_export_owner": bool(decision.get("direct_marketing_pv_store_external_export_owner")),
        "direct_marketing_hard_export_owner_confirmed": bool(decision.get("direct_marketing_hard_export_owner_confirmed")),
        "direct_marketing_export_execution": decision.get("direct_marketing_export_execution") if isinstance(decision.get("direct_marketing_export_execution"), dict) else {},
        "direct_marketing_export_execution_state": decision.get("direct_marketing_export_execution_state"),
        "direct_marketing_export_execution_claim": decision.get("direct_marketing_export_execution_claim"),
        "direct_marketing_export_compliance_confirmed": bool(decision.get("direct_marketing_export_compliance_confirmed")),
        "direct_marketing_export_violation_w": safe_int(decision.get("direct_marketing_export_violation_w"), 0),
        "direct_marketing_export_constraint_class": decision.get("direct_marketing_export_constraint_class"),
        "direct_marketing_hard_export_limit_active": bool(decision.get("direct_marketing_hard_export_limit_active")),
        "direct_marketing_hard_export_limit_w": decision.get("direct_marketing_hard_export_limit_w"),
        "direct_marketing_export_constraint_scope": decision.get("direct_marketing_export_constraint_scope"),
        "direct_marketing_pv_export_allowed": bool(decision.get("direct_marketing_pv_export_allowed")),
        "direct_marketing_pv_store_export_limit_active": bool(decision.get("direct_marketing_pv_store_export_limit_active")),
        "direct_marketing_pv_store_export_limit_guard_active": bool(decision.get("direct_marketing_pv_store_export_limit_guard_active")),
        "direct_marketing_pv_store_export_limit_w": safe_int(decision.get("direct_marketing_pv_store_export_limit_w"), 0),
        "direct_marketing_pv_store_export_limit_guard_w": safe_int(decision.get("direct_marketing_pv_store_export_limit_guard_w"), 0),
        "direct_marketing_pv_store_export_over_limit_w": safe_int(decision.get("direct_marketing_pv_store_export_over_limit_w"), 0),
        "direct_marketing_pv_store_export_absorb_target_w": safe_int(decision.get("direct_marketing_pv_store_export_absorb_target_w"), 0),
        "direct_marketing_pv_store_unavoidable_export_w": safe_int(decision.get("direct_marketing_pv_store_unavoidable_export_w"), 0),
        "direct_marketing_external_derating_active": bool(decision.get("direct_marketing_external_derating_active")),
        "direct_marketing_external_derating_source": decision.get("direct_marketing_external_derating_source"),
        "direct_marketing_external_derating_limit_w": decision.get("direct_marketing_external_derating_limit_w"),
        "direct_marketing_external_derating_ac_power_limit_w": safe_int(decision.get("direct_marketing_external_derating_ac_power_limit_w"), 0),
        "direct_marketing_external_derating_power_w": safe_int(decision.get("direct_marketing_external_derating_power_w"), 0),
        "direct_marketing_external_derating_percent": safe_float(decision.get("direct_marketing_external_derating_percent"), 0.0),
        "direct_marketing_pv_store_export_limit_ramp_bypass": bool(decision.get("direct_marketing_pv_store_export_limit_ramp_bypass")),
        "direct_marketing_pv_store_ramp_limited": bool(decision.get("direct_marketing_pv_store_ramp_limited")),
        "direct_marketing_pv_store_ramp_base_w": safe_int(decision.get("direct_marketing_pv_store_ramp_base_w"), 0),
        "direct_marketing_pv_store_ramp_step_w": safe_int(decision.get("direct_marketing_pv_store_ramp_step_w"), 0),
        "direct_marketing_pv_store_curve_catchup_active": bool(decision.get("direct_marketing_pv_store_curve_catchup_active")),
        "direct_marketing_pv_store_curve_catchup_w": safe_int(decision.get("direct_marketing_pv_store_curve_catchup_w"), 0),
        "direct_marketing_pv_store_curve_catchup_source": decision.get("direct_marketing_pv_store_curve_catchup_source"),
        "direct_marketing_pv_store_curve_catchup_raw_w": safe_int(decision.get("direct_marketing_pv_store_curve_catchup_raw_w"), 0),
        "direct_marketing_pv_store_curve_catchup_gap_pct": safe_float(decision.get("direct_marketing_pv_store_curve_catchup_gap_pct"), 0.0),
        "direct_marketing_pv_store_curve_soc_pct": decision.get("direct_marketing_pv_store_curve_soc_pct"),
        "direct_marketing_pv_store_curve_target_soc_pct": decision.get("direct_marketing_pv_store_curve_target_soc_pct"),
        "direct_marketing_pv_store_curve_target_ts": safe_float(decision.get("direct_marketing_pv_store_curve_target_ts"), 0.0),
        "direct_marketing_pv_store_curve_window_target_soc_pct": decision.get("direct_marketing_pv_store_curve_window_target_soc_pct"),
        "direct_marketing_pv_store_release_hold_active": bool(decision.get("direct_marketing_pv_store_release_hold_active")),
        "direct_marketing_pv_store_release_hold_reason": decision.get("direct_marketing_pv_store_release_hold_reason"),
        "direct_marketing_pv_store_release_hold_remaining_s": safe_float(decision.get("direct_marketing_pv_store_release_hold_remaining_s"), 0.0),
        "direct_marketing_pv_store_release_hold_previous_w": safe_int(decision.get("direct_marketing_pv_store_release_hold_previous_w"), 0),
        "direct_marketing_pv_store_release_hold_offer_w": safe_int(decision.get("direct_marketing_pv_store_release_hold_offer_w"), 0),
        "direct_marketing_pv_store_resync_active": bool(decision.get("direct_marketing_pv_store_resync_active")),
        "direct_marketing_pv_store_resync_reason": decision.get("direct_marketing_pv_store_resync_reason"),
        "direct_marketing_pv_store_resync_gap_w": safe_int(decision.get("direct_marketing_pv_store_resync_gap_w"), 0),
        "direct_marketing_pv_store_resync_threshold_w": safe_int(decision.get("direct_marketing_pv_store_resync_threshold_w"), 0),
        "direct_marketing_pv_store_observed_charge_w": safe_int(decision.get("direct_marketing_pv_store_observed_charge_w"), 0),
        "direct_marketing_pv_store_hold_active": bool(decision.get("direct_marketing_pv_store_hold_active")),
        "direct_marketing_pv_store_min_hold_s": safe_float(decision.get("direct_marketing_pv_store_min_hold_s"), 0.0),
        "direct_marketing_pv_store_state_age_s": safe_float(decision.get("direct_marketing_pv_store_state_age_s"), 0.0),
        "direct_marketing_pv_store_hold_remaining_s": safe_float(decision.get("direct_marketing_pv_store_hold_remaining_s"), 0.0),
        "direct_marketing_owner_switch_cooldown_active": bool(decision.get("direct_marketing_owner_switch_cooldown_active")),
        "direct_marketing_owner_switch_cooldown_s": safe_float(decision.get("direct_marketing_owner_switch_cooldown_s"), 0.0),
        "direct_marketing_owner_switch_cooldown_age_s": safe_float(decision.get("direct_marketing_owner_switch_cooldown_age_s"), 0.0),
        "direct_marketing_owner_switch_cooldown_remaining_s": safe_float(decision.get("direct_marketing_owner_switch_cooldown_remaining_s"), 0.0),
        "direct_marketing_owner_switch_previous_state": decision.get("direct_marketing_owner_switch_previous_state"),
        "direct_marketing_owner_switch_next_state": decision.get("direct_marketing_owner_switch_next_state"),
        "direct_marketing_pv_store_dc_only": bool(decision.get("direct_marketing_pv_store_dc_only")),
        "direct_marketing_pv_store_external_ac_guard_w": safe_int(decision.get("direct_marketing_pv_store_external_ac_guard_w"), 0),
        "direct_marketing_pv_total_w": safe_int(decision.get("direct_marketing_pv_total_w"), 0),
        "direct_marketing_pv_e3dc_w": safe_int(decision.get("direct_marketing_pv_e3dc_w"), 0),
        "direct_marketing_pv_external_ac_w": safe_int(decision.get("direct_marketing_pv_external_ac_w"), 0),
        "direct_marketing_pv_source": decision.get("direct_marketing_pv_source"),
        "direct_marketing_pv_store_dc_surplus_w": safe_int(decision.get("direct_marketing_pv_store_dc_surplus_w"), 0),
        "direct_marketing_pv_store_local_load_after_external_w": safe_int(decision.get("direct_marketing_pv_store_local_load_after_external_w"), 0),
        "direct_marketing_export_target_w": safe_int(decision.get("direct_marketing_export_target_w"), 0),
        "direct_marketing_export_w": safe_int(decision.get("direct_marketing_export_w"), 0),
        "direct_marketing_export_headroom_w": safe_int(decision.get("direct_marketing_export_headroom_w"), 0),
        "direct_marketing_export_discharge_limit_w": safe_int(decision.get("direct_marketing_export_discharge_limit_w"), 0),
        "direct_marketing_export_local_deficit_w": safe_int(decision.get("direct_marketing_export_local_deficit_w"), 0),
        "direct_marketing_export_grid_import_w": safe_int(decision.get("direct_marketing_export_grid_import_w"), 0),
        "direct_marketing_export_import_guard_w": safe_int(decision.get("direct_marketing_export_import_guard_w"), 0),
        "direct_marketing_export_base_w": safe_int(decision.get("direct_marketing_export_base_w"), 0),
        "direct_marketing_export_desired_w": safe_int(decision.get("direct_marketing_export_desired_w"), 0),
        "direct_marketing_export_min_grid_export_w": safe_int(decision.get("direct_marketing_export_min_grid_export_w"), 0),
        "direct_marketing_export_netpoint_deadband_w": safe_int(decision.get("direct_marketing_export_netpoint_deadband_w"), 0),
        "direct_marketing_export_netpoint_release_margin_w": safe_int(decision.get("direct_marketing_export_netpoint_release_margin_w"), 0),
        "direct_marketing_export_grid_export_w": safe_int(decision.get("direct_marketing_export_grid_export_w"), 0),
        "direct_marketing_export_surplus_w": safe_int(decision.get("direct_marketing_export_surplus_w"), 0),
        "direct_marketing_export_grid_error_w": safe_int(decision.get("direct_marketing_export_grid_error_w"), 0),
        "direct_marketing_export_required_by_grid_w": safe_int(decision.get("direct_marketing_export_required_by_grid_w"), 0),
        "direct_marketing_export_required_by_load_w": safe_int(decision.get("direct_marketing_export_required_by_load_w"), 0),
        "direct_marketing_ramp_limited": bool(decision.get("direct_marketing_ramp_limited")),
        "direct_marketing_netpoint_release_hold": bool(decision.get("direct_marketing_netpoint_release_hold")),
        "direct_marketing_hold_active": bool(decision.get("direct_marketing_hold_active")),
        "direct_marketing_profit_hold_ct_per_kwh": safe_float(decision.get("direct_marketing_profit_hold_ct_per_kwh"), 0.0),
        "direct_marketing_margin_hold_pct": safe_float(decision.get("direct_marketing_margin_hold_pct"), 0.0),
        "direct_marketing_hold_profit_floor_ct_per_kwh": safe_float(decision.get("direct_marketing_hold_profit_floor_ct_per_kwh"), 0.0),
        "direct_marketing_hold_margin_floor_pct": safe_float(decision.get("direct_marketing_hold_margin_floor_pct"), 0.0),
        "direct_marketing_ramp_base_w": safe_int(decision.get("direct_marketing_ramp_base_w"), 0),
        "direct_marketing_ramp_step_w": safe_int(decision.get("direct_marketing_ramp_step_w"), 0),
        "direct_marketing_economics": decision.get("direct_marketing_economics") if isinstance(decision.get("direct_marketing_economics"), dict) else {},
        "direct_marketing_monitor": direct_marketing_monitor_state,
        "max_charge_w": max_charge_w,
        "max_discharge_w": max_discharge_w,
        "abregel_charge_req_w": abregel_charge_req_w,
        "abregel_grid_pressure_w": abregel_grid_pressure_w,
        "abregel_physical_pressure_w": abregel_physical_pressure_w,
        "abregel_inverter_pressure_w": abregel_inverter_pressure_w,
        "abregel_grid_error_w": abregel_grid_error_w,
        "abregel_target_w": abregel_target_w,
        "abregel_release_w": abregel_release_w,
        "abregel_rscp_limit_w": abregel_rscp_limit_w,
        "abregel_source": abregel_source,
        "abregel_active": abregel_active,
        "curve_cap_release_hysteresis_w": safe_int(decision.get("curve_cap_release_hysteresis_w"), 0),
        "curve_cap_release_grace_s": safe_float(decision.get("curve_cap_release_grace_s"), 0.0),
        "curve_cap_grid_contract_valid": bool(decision.get("curve_cap_grid_contract_valid")),
        "curve_cap_real_grid_import_active": bool(decision.get("curve_cap_real_grid_import_active")),
        "curve_cap_release_below_active": bool(decision.get("curve_cap_release_below_active")),
        "curve_cap_release_below_since_ts": safe_float(decision.get("curve_cap_release_below_since_ts"), 0.0),
        "curve_cap_release_elapsed_s": safe_float(decision.get("curve_cap_release_elapsed_s"), 0.0),
        "curve_cap_release_grace_active": bool(decision.get("curve_cap_release_grace_active")),
        "curve_cap_release_ramp_active": bool(decision.get("curve_cap_release_ramp_active")),
        "curve_cap_hysteresis_hold_active": bool(decision.get("curve_cap_hysteresis_hold_active")),
        "curve_cap_invalid_hold_active": bool(decision.get("curve_cap_invalid_hold_active")),
        "curve_cap_release_phase": str(decision.get("curve_cap_release_phase") or "inactive"),
        "curve_cap_release_pending": bool(decision.get("curve_cap_release_pending")),
        "curve_cap_release_requested": bool(decision.get("curve_cap_release_requested")),
        "curve_cap_release_confirmed_since_ts": safe_float(decision.get("curve_cap_release_confirmed_since_ts"), 0.0),
        "curve_cap_post_release_until_ts": safe_float(decision.get("curve_cap_post_release_until_ts"), 0.0),
        "curve_cap_post_release_guard_active": bool(decision.get("curve_cap_post_release_guard_active")),
        "curve_cap_post_release_reentry_blocked": bool(decision.get("curve_cap_post_release_reentry_blocked")),
        "curve_cap_settings_readback_valid": bool(decision.get("curve_cap_settings_readback_valid")),
        "curve_cap_settings_bounded_zero_confirmed": bool(decision.get("curve_cap_settings_bounded_zero_confirmed")),
        "curve_cap_settings_release_confirmed": bool(decision.get("curve_cap_settings_release_confirmed")),
        "hardening_contracts_version": hardening_contracts.get("version"),
        "hardening_contracts_scope": hardening_contracts.get("scope"),
        "hardening_contracts": hardening_contracts.get("contracts", {}),
        "budget": budget,
        "parallel_state": state,
        "parallel_state_since_ts": parallel_state_since_ts,
        "parallel_mode": mode,
        "parallel_val": val,
        "auto_limit": decision.get("auto_limit"),
        "manual_target_soc": decision.get("manual_target_soc"),
        "manual_release_mode": decision.get("manual_release_mode"),
        "hard_mode_guard_errors": decision.get("hard_mode_guard_errors"),
        "hard_mode_guard_set_power_auto": decision.get("hard_mode_guard_set_power_auto"),
        "hard_mode_guard_retry_s": decision.get("hard_mode_guard_retry_s"),
        "force_wallbox_stop": bool(decision.get("force_wallbox_stop")),
        "predump_floor_hold": bool(decision.get("predump_floor_hold")),
        "predump_active": bool(decision.get("predump_active")),
        "predump_allow": decision.get("predump_allow"),
        "predump_target_soc": decision.get("predump_target_soc"),
        "predump_floor_soc": decision.get("predump_floor_soc"),
        "predump_consumer_landing_under_pct": decision.get("predump_consumer_landing_under_pct"),
        "predump_consumer_landing_under_wh": decision.get("predump_consumer_landing_under_wh"),
        "predump_no_grid": decision.get("predump_no_grid"),
        "predump_grid_fallback": decision.get("predump_grid_fallback"),
        "predump_hard_predump": decision.get("predump_hard_predump"),
        "predump_grid_allowed": decision.get("predump_grid_allowed"),
        "predump_grid_blocked_by_comfort": decision.get("predump_grid_blocked_by_comfort"),
        "predump_grid_export_target_w": decision.get("predump_grid_export_target_w"),
        "predump_grid_export_w": decision.get("predump_grid_export_w"),
        "predump_grid_export_headroom_w": decision.get("predump_grid_export_headroom_w"),
        "predump_grid_base_export_w": decision.get("predump_grid_base_export_w"),
        "predump_grid_battery_discharge_w": decision.get("predump_grid_battery_discharge_w"),
        "predump_grid_discharge_limit_w": decision.get("predump_grid_discharge_limit_w"),
        "predump_grid_ramp_base_w": decision.get("predump_grid_ramp_base_w"),
        "predump_grid_ramp_step_w": decision.get("predump_grid_ramp_step_w"),
        "predump_hard_grid_limit_w": decision.get("predump_hard_grid_limit_w"),
        "predump_hard_grid_uncapped_w": decision.get("predump_hard_grid_uncapped_w"),
        "predump_hard_grid_limited": bool(decision.get("predump_hard_grid_limited")),
        "predump_consumer_devices": decision.get("predump_consumer_devices"),
        "predump_consumer_load_w": decision.get("predump_consumer_load_w"),
        "predump_wallbox_min_power_w": decision.get("predump_wallbox_min_power_w"),
        "predump_wallbox_min_amp": decision.get("predump_wallbox_min_amp"),
        "predump_wallbox_min_phases": decision.get("predump_wallbox_min_phases"),
        "predump_bev_block_w": decision.get("predump_bev_block_w"),
        "predump_bev_start_ts": decision.get("predump_bev_start_ts"),
        "predump_bev_grid_latest_ts": decision.get("predump_bev_grid_latest_ts"),
        "predump_bev_remaining_wh": decision.get("predump_bev_remaining_wh"),
        "predump_grid_latest_ts": decision.get("predump_grid_latest_ts"),
        "predump_grid_remaining_wh": decision.get("predump_grid_remaining_wh"),
        "predump_grid_duration_s": decision.get("predump_grid_duration_s"),
        "predump_consumer_wait_since_ts": decision.get("predump_consumer_wait_since_ts"),
        "predump_consumer_wait_elapsed_s": decision.get("predump_consumer_wait_elapsed_s"),
        "predump_reopen_block_active": bool(predump_reopen_block.get("active")),
        "predump_reopen_block_until_ts": predump_reopen_block.get("until_ts"),
        "predump_reopen_target_soc": predump_reopen_block.get("target_soc"),
        "predump_reopen_floor_soc": predump_reopen_block.get("floor_soc"),
        "predump_reopen_block_reason": predump_reopen_block.get("reason"),
        "home_feedback_guard": decision.get("home_feedback_guard") if isinstance(decision.get("home_feedback_guard"), dict) else None,
        "last_wb_active_ts": now_s if wb_car_present else previous_state.get("last_wb_active_ts", 0),
        "last_wb_possible_power_w": wb_possible_w or previous_state.get("last_wb_possible_power_w", 0),
        "last_auto_ts": (
            (
                safe_float(previous_state.get("last_auto_ts"), 0.0)
                if safe_int(previous_state.get("mode"), -1) == MODE_AUTO and safe_float(previous_state.get("last_auto_ts"), 0.0) > 0
                else now_s
            )
            if mode == MODE_AUTO
            else previous_state.get("last_auto_ts", 0)
        ),
        "shadow_payload": decision.get("shadow_payload"),
    }
    payload["charge_acceptance_diagnostic"] = charge_acceptance_diagnostic_contract(
        cfg,
        live,
        payload,
        plan,
        previous_state,
        now_s=now_s,
    )
    display = build_display(payload)
    payload.update(display)
    budget.update({
        "manager_title": display["manager_title"],
        "control_owner": display.get("control_owner", ""),
        "control_owner_label": display.get("control_owner_label", ""),
        "state_label": display["state_label"],
        "display_reason": display["display_reason"],
    })
    return payload




def write_predump_consumer_plan(
    active: bool,
    allow: Optional[Dict[str, Any]] = None,
    budget_w: int = 0,
    discharge_w: int = 0,
    reason: str = "",
    target_soc: Optional[Any] = None,
    no_grid: bool = True,
    grid_fallback: bool = False,
    state: str = "",
) -> None:
    try:
        now = int(time.time())
        allow_map = allow if isinstance(allow, dict) else {}
        payload: Dict[str, Any] = {
            "enabled": bool(active),
            "active": bool(active),
            "state": state or ("pre_discharge" if active else "idle"),
            "ts": now,
            "expires_ts": now + max(20, int(CYCLE_S * 4)),
            "allow": {
                "wallbox": bool(allow_map.get("wallbox")),
                "heatpump": bool(allow_map.get("heatpump")),
                "heater": bool(allow_map.get("heater")),
            },
            "budget_w": max(0, int(budget_w or 0)),
            "discharge_w": max(0, int(discharge_w or 0)),
            "no_grid": bool(no_grid),
            "grid_fallback": bool(grid_fallback),
            "reason": str(reason or "")[:160],
        }
        if target_soc is not None:
            payload["target_soc"] = round(safe_float(target_soc), 1)
            payload["floor_soc"] = round(safe_float(target_soc), 1)
        atomic_write(PREDUMP_PLAN_F, payload)
    except Exception:
        pass


def write_state(payload: Dict[str, Any], plan: Dict[str, Any]) -> None:
    state = {
        "state": payload["state"],
        "reason": payload["reason"],
        "manager_title": payload.get("manager_title", "Speicher-Regelung"),
        "state_label": payload.get("state_label", payload["state"]),
        "display_reason": payload.get("display_reason", payload["reason"]),
        "mode": payload["mode"],
        "mode_name": payload.get("mode_name"),
        "val": payload["val"],
        "soc": payload["soc"],
        "curve_control_soc": payload.get("curve_control_soc"),
        "curve_control_raw_soc": payload.get("curve_control_raw_soc"),
        "curve_control_soc_ts": payload.get("curve_control_soc_ts"),
        "ladeende": safe_float(plan.get("planning_target_soc", plan.get("target_soc")), 95.0),
        "tl_soc_now": payload.get("curve_soc"),
        "tl_soc_target": payload.get("target_soc"),
        "tl_ts_target": payload.get("target_ts"),
        "iFc_w": payload.get("iFc_w", 0),
        "iMinLade_w": payload.get("iMinLade_w", 0),
        "storage_charge_request_w": payload.get("storage_charge_request_w", 0),
        "charge_acceptance_diagnostic": copy.deepcopy(payload.get("charge_acceptance_diagnostic"))
        if isinstance(payload.get("charge_acceptance_diagnostic"), dict)
        else None,
        "wallbox_phase_transition_active": bool(payload.get("wallbox_phase_transition_active")),
        "wallbox_phase_transition_reserved_w": payload.get("wallbox_phase_transition_reserved_w", 0),
        "wallbox_phase_transition_flexible_budget_w": payload.get("wallbox_phase_transition_flexible_budget_w", 0),
        "wallbox_phase_transition_target_phases": payload.get("wallbox_phase_transition_target_phases", 0),
        "wallbox_phase_transition_until_ts": payload.get("wallbox_phase_transition_until_ts", 0.0),
        "wallbox_phase_transition_source": payload.get("wallbox_phase_transition_source", ""),
        "wallbox_phase_transition_grants": payload.get("wallbox_phase_transition_grants") if isinstance(payload.get("wallbox_phase_transition_grants"), dict) else None,
        "wallbox_phase_transition_requested_w_total": payload.get("wallbox_phase_transition_requested_w_total", 0),
        "wallbox_phase_transition_reserved_w_total": payload.get("wallbox_phase_transition_reserved_w_total", 0),
        "heatpump_running": bool(payload.get("heatpump_running")),
        "heatpump_running_commitment_w": payload.get("heatpump_running_commitment_w", 0),
        "heatpump_starting": bool(payload.get("heatpump_starting")),
        "heatpump_starting_until_ts": payload.get("heatpump_starting_until_ts", 0.0),
        "heatpump_new_start_allowed": bool(payload.get("heatpump_new_start_allowed", True)),
        "flexible_budget_after_commitments_w": payload.get("flexible_budget_after_commitments_w", 0),
        "wallbox_curve_reserve_w": payload.get("wallbox_curve_reserve_w", 0),
        "wallbox_curve_reserve_target_w": payload.get("wallbox_curve_reserve_target_w", 0),
        "wallbox_curve_reserve_step_w": payload.get("wallbox_curve_reserve_step_w", 0),
        "controlled_wallbox_wbminsoc_pause": bool(payload.get("controlled_wallbox_wbminsoc_pause")),
        "wbminsoc_pv_charge_active": bool(payload.get("wbminsoc_pv_charge_active")),
        "planned_grid_pv_charge_w": max(0, safe_int(payload.get("planned_grid_pv_charge_w"), 0)),
        "planned_grid_pv_surplus_w": max(0, safe_int(payload.get("planned_grid_pv_surplus_w"), 0)),
        "pv_house_surplus_w": max(0, safe_int(payload.get("pv_house_surplus_w"), 0)),
        "scheduled_grid_charge": bool(payload.get("scheduled_grid_charge")),
        "wbminsoc_transition_dwell_active": bool(payload.get("wbminsoc_transition_dwell_active")),
        "wbminsoc_curve_dwell_active": bool(payload.get("wbminsoc_curve_dwell_active")),
        "wbminsoc_previous_state_age_s": payload.get("wbminsoc_previous_state_age_s", 0.0),
        "wbminsoc_effective_pv_charge_threshold_w": payload.get("wbminsoc_effective_pv_charge_threshold_w", 0),
        "heatpump_pause_request": payload.get("heatpump_pause_request") if isinstance(payload.get("heatpump_pause_request"), dict) else None,
        "max_charge_w": payload.get("max_charge_w", 0),
        "max_discharge_w": payload.get("max_discharge_w", 0),
        "abregel_charge_req_w": payload.get("abregel_charge_req_w", 0),
        "abregel_grid_pressure_w": payload.get("abregel_grid_pressure_w", 0),
        "abregel_physical_pressure_w": payload.get("abregel_physical_pressure_w", 0),
        "abregel_inverter_pressure_w": payload.get("abregel_inverter_pressure_w", 0),
        "abregel_grid_error_w": payload.get("abregel_grid_error_w", 0),
        "abregel_target_w": payload.get("abregel_target_w", 0),
        "abregel_release_w": payload.get("abregel_release_w", 0),
        "abregel_rscp_limit_w": payload.get("abregel_rscp_limit_w", 0),
        "abregel_source": payload.get("abregel_source", ""),
        "abregel_active": bool(payload.get("abregel_active")),
        "pv_w": payload.get("pv_w", 0),
        "grid_w": payload.get("grid_w", 0),
        "bat_w": payload.get("bat_w", 0),
        "live_age_s": payload.get("live_age_s"),
        "live_stale": bool(payload.get("live_stale")),
        "soc_unrealistic": bool(payload.get("soc_unrealistic")),
        "soc_jump_guard": payload.get("soc_jump_guard") if isinstance(payload.get("soc_jump_guard"), dict) else None,
        "last_valid_soc": payload.get("last_valid_soc"),
        "last_valid_soc_ts": payload.get("last_valid_soc_ts"),
        "ems_power_limits_active": payload.get("ems_power_limits_active"),
        "ems_max_charge_power_w": payload.get("ems_max_charge_power_w"),
        "ems_max_discharge_power_w": payload.get("ems_max_discharge_power_w"),
        "ems_discharge_start_power_w": payload.get("ems_discharge_start_power_w"),
        "ems_reaction": payload.get("ems_reaction") if isinstance(payload.get("ems_reaction"), dict) else None,
        "ems_budget_runtime": payload.get("ems_budget_runtime") if isinstance(payload.get("ems_budget_runtime"), dict) else None,
        "ems_budget_runtime_veto": bool(payload.get("ems_budget_runtime_veto")),
        "ems_budget_runtime_veto_reason": payload.get("ems_budget_runtime_veto_reason"),
        "last_auto_ts": payload.get("last_auto_ts"),
        "parallel_state": payload.get("parallel_state", payload.get("state")),
        "parallel_state_since_ts": payload.get("parallel_state_since_ts"),
        "parallel_mode": payload.get("parallel_mode", payload.get("mode")),
        "parallel_val": payload.get("parallel_val", payload.get("val")),
        "predump_reopen_block_active": bool(payload.get("predump_reopen_block_active")),
        "predump_reopen_block_until_ts": payload.get("predump_reopen_block_until_ts"),
        "predump_reopen_target_soc": payload.get("predump_reopen_target_soc"),
        "predump_reopen_floor_soc": payload.get("predump_reopen_floor_soc"),
        "predump_reopen_block_reason": payload.get("predump_reopen_block_reason"),
        "next_manager": True,
        "protected": bool(payload.get("protected")),
        "priority": payload.get("priority"),
        "market_live_pv_first": payload.get("market_live_pv_first") if isinstance(payload.get("market_live_pv_first"), dict) else None,
        "market_live_pv_first_overridden": bool(payload.get("market_live_pv_first_overridden")),
        "market_live_export_absorb_active": bool(payload.get("market_live_export_absorb_active")),
        "market_live_export_absorb_hold_active": bool(payload.get("market_live_export_absorb_hold_active")),
        "market_live_export_absorb_charge_w": max(0, safe_int(payload.get("market_live_export_absorb_charge_w"), 0)),
        "market_live_export_absorb": payload.get("market_live_export_absorb") if isinstance(payload.get("market_live_export_absorb"), dict) else None,
        "market_late_fill_wait_overridden": bool(payload.get("market_late_fill_wait_overridden")),
        "market_forecast_grid_charge_need_wh": safe_float(payload.get("market_forecast_grid_charge_need_wh"), 0.0),
        "direct_marketing_active": bool(payload.get("direct_marketing_active")),
        "direct_marketing_policy_active": bool(payload.get("direct_marketing_policy_active")),
        "direct_marketing_policy_schema": payload.get("direct_marketing_policy_schema"),
        "direct_marketing_policy_decision": payload.get("direct_marketing_policy_decision") if isinstance(payload.get("direct_marketing_policy_decision"), dict) else None,
        "direct_marketing_policy_target_state": payload.get("direct_marketing_policy_target_state"),
        "direct_marketing_policy_block_reason": payload.get("direct_marketing_policy_block_reason"),
        "direct_marketing_policy_export_budget_w": payload.get("direct_marketing_policy_export_budget_w"),
        "direct_marketing_policy_charge_budget_w": payload.get("direct_marketing_policy_charge_budget_w"),
        "direct_marketing_policy_protected_reserve_wh": payload.get("direct_marketing_policy_protected_reserve_wh"),
        "direct_marketing_policy_sellable_wh": payload.get("direct_marketing_policy_sellable_wh"),
        "direct_marketing_policy_executor_gate": payload.get("direct_marketing_policy_executor_gate") if isinstance(payload.get("direct_marketing_policy_executor_gate"), dict) else None,
        "direct_marketing_future_pv_store_reservation": payload.get("direct_marketing_future_pv_store_reservation") if isinstance(payload.get("direct_marketing_future_pv_store_reservation"), dict) else None,
        "direct_marketing_future_pv_store_reservation_active": bool(payload.get("direct_marketing_future_pv_store_reservation_active")),
        "direct_marketing_future_pv_store_reservation_cap_w": payload.get("direct_marketing_future_pv_store_reservation_cap_w"),
        "direct_marketing_mode": payload.get("direct_marketing_mode"),
        "direct_marketing_owner": payload.get("direct_marketing_owner"),
        "direct_marketing_contract_version": payload.get("direct_marketing_contract_version"),
        "direct_marketing_action": payload.get("direct_marketing_action"),
        "direct_marketing_window": payload.get("direct_marketing_window") if isinstance(payload.get("direct_marketing_window"), dict) else None,
        "direct_marketing_profit_ct_per_kwh": payload.get("direct_marketing_profit_ct_per_kwh"),
        "direct_marketing_reserve_floor_soc_pct": payload.get("direct_marketing_reserve_floor_soc_pct"),
        "direct_marketing_target_soc_pct": payload.get("direct_marketing_target_soc_pct"),
        "direct_marketing_pv_store_w": payload.get("direct_marketing_pv_store_w"),
        "direct_marketing_pv_store_offer_w": payload.get("direct_marketing_pv_store_offer_w"),
        "direct_marketing_pv_store_max_w": payload.get("direct_marketing_pv_store_max_w"),
        "direct_marketing_pv_store_surplus_w": payload.get("direct_marketing_pv_store_surplus_w"),
        "direct_marketing_pv_store_grid_import_w": payload.get("direct_marketing_pv_store_grid_import_w"),
        "direct_marketing_pv_store_grid_export_w": payload.get("direct_marketing_pv_store_grid_export_w"),
        "direct_marketing_pv_store_import_guard_w": payload.get("direct_marketing_pv_store_import_guard_w"),
        "direct_marketing_pv_store_min_surplus_w": payload.get("direct_marketing_pv_store_min_surplus_w"),
        "direct_marketing_pv_store_requested_w": payload.get("direct_marketing_pv_store_requested_w"),
        "direct_marketing_pv_store_target_fallback_active": payload.get("direct_marketing_pv_store_target_fallback_active"),
        "direct_marketing_pv_store_estimated_offer_w": payload.get("direct_marketing_pv_store_estimated_offer_w"),
        "direct_marketing_pv_store_pv_safe_cap_w": payload.get("direct_marketing_pv_store_pv_safe_cap_w"),
        "direct_marketing_pv_store_self_reference_limited": payload.get("direct_marketing_pv_store_self_reference_limited"),
        "direct_marketing_pv_store_execution": payload.get("direct_marketing_pv_store_execution"),
        "direct_marketing_pv_store_auto_limit_active": payload.get("direct_marketing_pv_store_auto_limit_active"),
        "direct_marketing_pv_store_external_export_owner": payload.get("direct_marketing_pv_store_external_export_owner"),
        "direct_marketing_hard_export_owner_confirmed": payload.get("direct_marketing_hard_export_owner_confirmed"),
        "direct_marketing_export_execution": payload.get("direct_marketing_export_execution"),
        "direct_marketing_export_execution_state": payload.get("direct_marketing_export_execution_state"),
        "direct_marketing_export_execution_claim": payload.get("direct_marketing_export_execution_claim"),
        "direct_marketing_export_compliance_confirmed": payload.get("direct_marketing_export_compliance_confirmed"),
        "direct_marketing_export_violation_w": payload.get("direct_marketing_export_violation_w"),
        "direct_marketing_export_constraint_class": payload.get("direct_marketing_export_constraint_class"),
        "direct_marketing_hard_export_limit_active": payload.get("direct_marketing_hard_export_limit_active"),
        "direct_marketing_hard_export_limit_w": payload.get("direct_marketing_hard_export_limit_w"),
        "direct_marketing_export_constraint_scope": payload.get("direct_marketing_export_constraint_scope"),
        "direct_marketing_pv_export_allowed": payload.get("direct_marketing_pv_export_allowed"),
        "direct_marketing_pv_store_export_limit_active": payload.get("direct_marketing_pv_store_export_limit_active"),
        "direct_marketing_pv_store_export_limit_guard_active": payload.get("direct_marketing_pv_store_export_limit_guard_active"),
        "direct_marketing_pv_store_export_limit_w": payload.get("direct_marketing_pv_store_export_limit_w"),
        "direct_marketing_pv_store_export_limit_guard_w": payload.get("direct_marketing_pv_store_export_limit_guard_w"),
        "direct_marketing_pv_store_export_over_limit_w": payload.get("direct_marketing_pv_store_export_over_limit_w"),
        "direct_marketing_pv_store_export_absorb_target_w": payload.get("direct_marketing_pv_store_export_absorb_target_w"),
        "direct_marketing_pv_store_unavoidable_export_w": payload.get("direct_marketing_pv_store_unavoidable_export_w"),
        "direct_marketing_external_derating_active": payload.get("direct_marketing_external_derating_active"),
        "direct_marketing_external_derating_source": payload.get("direct_marketing_external_derating_source"),
        "direct_marketing_external_derating_limit_w": payload.get("direct_marketing_external_derating_limit_w"),
        "direct_marketing_external_derating_ac_power_limit_w": payload.get("direct_marketing_external_derating_ac_power_limit_w"),
        "direct_marketing_external_derating_power_w": payload.get("direct_marketing_external_derating_power_w"),
        "direct_marketing_external_derating_percent": payload.get("direct_marketing_external_derating_percent"),
        "direct_marketing_pv_store_export_limit_ramp_bypass": payload.get("direct_marketing_pv_store_export_limit_ramp_bypass"),
        "direct_marketing_pv_store_ramp_limited": payload.get("direct_marketing_pv_store_ramp_limited"),
        "direct_marketing_pv_store_ramp_base_w": payload.get("direct_marketing_pv_store_ramp_base_w"),
        "direct_marketing_pv_store_ramp_step_w": payload.get("direct_marketing_pv_store_ramp_step_w"),
        "direct_marketing_pv_store_curve_catchup_active": payload.get("direct_marketing_pv_store_curve_catchup_active"),
        "direct_marketing_pv_store_curve_catchup_w": payload.get("direct_marketing_pv_store_curve_catchup_w"),
        "direct_marketing_pv_store_curve_catchup_source": payload.get("direct_marketing_pv_store_curve_catchup_source"),
        "direct_marketing_pv_store_curve_catchup_raw_w": payload.get("direct_marketing_pv_store_curve_catchup_raw_w"),
        "direct_marketing_pv_store_curve_catchup_gap_pct": payload.get("direct_marketing_pv_store_curve_catchup_gap_pct"),
        "direct_marketing_pv_store_curve_soc_pct": payload.get("direct_marketing_pv_store_curve_soc_pct"),
        "direct_marketing_pv_store_curve_target_soc_pct": payload.get("direct_marketing_pv_store_curve_target_soc_pct"),
        "direct_marketing_pv_store_curve_target_ts": payload.get("direct_marketing_pv_store_curve_target_ts"),
        "direct_marketing_pv_store_curve_window_target_soc_pct": payload.get("direct_marketing_pv_store_curve_window_target_soc_pct"),
        "direct_marketing_pv_store_release_hold_active": payload.get("direct_marketing_pv_store_release_hold_active"),
        "direct_marketing_pv_store_release_hold_reason": payload.get("direct_marketing_pv_store_release_hold_reason"),
        "direct_marketing_pv_store_release_hold_remaining_s": payload.get("direct_marketing_pv_store_release_hold_remaining_s"),
        "direct_marketing_pv_store_release_hold_previous_w": payload.get("direct_marketing_pv_store_release_hold_previous_w"),
        "direct_marketing_pv_store_release_hold_offer_w": payload.get("direct_marketing_pv_store_release_hold_offer_w"),
        "direct_marketing_pv_store_resync_active": payload.get("direct_marketing_pv_store_resync_active"),
        "direct_marketing_pv_store_resync_reason": payload.get("direct_marketing_pv_store_resync_reason"),
        "direct_marketing_pv_store_resync_gap_w": payload.get("direct_marketing_pv_store_resync_gap_w"),
        "direct_marketing_pv_store_resync_threshold_w": payload.get("direct_marketing_pv_store_resync_threshold_w"),
        "direct_marketing_pv_store_observed_charge_w": payload.get("direct_marketing_pv_store_observed_charge_w"),
        "direct_marketing_pv_store_hold_active": payload.get("direct_marketing_pv_store_hold_active"),
        "direct_marketing_pv_store_min_hold_s": payload.get("direct_marketing_pv_store_min_hold_s"),
        "direct_marketing_pv_store_state_age_s": payload.get("direct_marketing_pv_store_state_age_s"),
        "direct_marketing_pv_store_hold_remaining_s": payload.get("direct_marketing_pv_store_hold_remaining_s"),
        "direct_marketing_owner_switch_cooldown_active": payload.get("direct_marketing_owner_switch_cooldown_active"),
        "direct_marketing_owner_switch_cooldown_s": payload.get("direct_marketing_owner_switch_cooldown_s"),
        "direct_marketing_owner_switch_cooldown_age_s": payload.get("direct_marketing_owner_switch_cooldown_age_s"),
        "direct_marketing_owner_switch_cooldown_remaining_s": payload.get("direct_marketing_owner_switch_cooldown_remaining_s"),
        "direct_marketing_owner_switch_previous_state": payload.get("direct_marketing_owner_switch_previous_state"),
        "direct_marketing_owner_switch_next_state": payload.get("direct_marketing_owner_switch_next_state"),
        "direct_marketing_pv_store_dc_only": payload.get("direct_marketing_pv_store_dc_only"),
        "direct_marketing_pv_store_external_ac_guard_w": payload.get("direct_marketing_pv_store_external_ac_guard_w"),
        "direct_marketing_pv_total_w": payload.get("direct_marketing_pv_total_w"),
        "direct_marketing_pv_e3dc_w": payload.get("direct_marketing_pv_e3dc_w"),
        "direct_marketing_pv_external_ac_w": payload.get("direct_marketing_pv_external_ac_w"),
        "direct_marketing_pv_source": payload.get("direct_marketing_pv_source"),
        "direct_marketing_pv_store_dc_surplus_w": payload.get("direct_marketing_pv_store_dc_surplus_w"),
        "direct_marketing_pv_store_local_load_after_external_w": payload.get("direct_marketing_pv_store_local_load_after_external_w"),
        "direct_marketing_export_target_w": payload.get("direct_marketing_export_target_w"),
        "direct_marketing_export_w": payload.get("direct_marketing_export_w"),
        "direct_marketing_export_headroom_w": payload.get("direct_marketing_export_headroom_w"),
        "direct_marketing_export_discharge_limit_w": payload.get("direct_marketing_export_discharge_limit_w"),
        "direct_marketing_export_local_deficit_w": payload.get("direct_marketing_export_local_deficit_w"),
        "direct_marketing_export_grid_import_w": payload.get("direct_marketing_export_grid_import_w"),
        "direct_marketing_export_import_guard_w": payload.get("direct_marketing_export_import_guard_w"),
        "direct_marketing_export_base_w": payload.get("direct_marketing_export_base_w"),
        "direct_marketing_export_desired_w": payload.get("direct_marketing_export_desired_w"),
        "direct_marketing_export_min_grid_export_w": payload.get("direct_marketing_export_min_grid_export_w"),
        "direct_marketing_export_netpoint_deadband_w": payload.get("direct_marketing_export_netpoint_deadband_w"),
        "direct_marketing_export_netpoint_release_margin_w": payload.get("direct_marketing_export_netpoint_release_margin_w"),
        "direct_marketing_export_grid_export_w": payload.get("direct_marketing_export_grid_export_w"),
        "direct_marketing_export_surplus_w": payload.get("direct_marketing_export_surplus_w"),
        "direct_marketing_export_grid_error_w": payload.get("direct_marketing_export_grid_error_w"),
        "direct_marketing_export_required_by_grid_w": payload.get("direct_marketing_export_required_by_grid_w"),
        "direct_marketing_export_required_by_load_w": payload.get("direct_marketing_export_required_by_load_w"),
        "direct_marketing_ramp_limited": bool(payload.get("direct_marketing_ramp_limited")),
        "direct_marketing_netpoint_release_hold": bool(payload.get("direct_marketing_netpoint_release_hold")),
        "direct_marketing_hold_active": bool(payload.get("direct_marketing_hold_active")),
        "direct_marketing_profit_hold_ct_per_kwh": payload.get("direct_marketing_profit_hold_ct_per_kwh"),
        "direct_marketing_margin_hold_pct": payload.get("direct_marketing_margin_hold_pct"),
        "direct_marketing_hold_profit_floor_ct_per_kwh": payload.get("direct_marketing_hold_profit_floor_ct_per_kwh"),
        "direct_marketing_hold_margin_floor_pct": payload.get("direct_marketing_hold_margin_floor_pct"),
        "direct_marketing_ramp_base_w": payload.get("direct_marketing_ramp_base_w"),
        "direct_marketing_ramp_step_w": payload.get("direct_marketing_ramp_step_w"),
        "direct_marketing_economics": payload.get("direct_marketing_economics") if isinstance(payload.get("direct_marketing_economics"), dict) else None,
        "direct_marketing_monitor": payload.get("direct_marketing_monitor") if isinstance(payload.get("direct_marketing_monitor"), dict) else None,
        "direct_marketing_daily_report": payload.get("direct_marketing_daily_report") if isinstance(payload.get("direct_marketing_daily_report"), dict) else None,
        "direct_marketing_aux_inverter_shelly": payload.get("direct_marketing_aux_inverter_shelly") if isinstance(payload.get("direct_marketing_aux_inverter_shelly"), dict) else None,
        "auto_limit": payload.get("auto_limit") if isinstance(payload.get("auto_limit"), dict) else None,
        "live_plausibility_preserved_wbminsoc_contract": bool(payload.get("live_plausibility_preserved_wbminsoc_contract")),
        "live_plausibility_preserved_discharge_owner": bool(payload.get("live_plausibility_preserved_discharge_owner")),
        "live_plausibility_preserved_discharge_state": payload.get("live_plausibility_preserved_discharge_state"),
        "live_plausibility_preserved_discharge_age_s": payload.get("live_plausibility_preserved_discharge_age_s"),
        "live_plausibility_preserved_discharge_hold_s": payload.get("live_plausibility_preserved_discharge_hold_s"),
        "live_plausibility_preserved_charge_owner": bool(payload.get("live_plausibility_preserved_charge_owner")),
        "live_plausibility_preserved_charge_state": payload.get("live_plausibility_preserved_charge_state"),
        "live_plausibility_preserved_charge_age_s": payload.get("live_plausibility_preserved_charge_age_s"),
        "live_plausibility_preserved_charge_hold_s": payload.get("live_plausibility_preserved_charge_hold_s"),
        "manual_target_soc": payload.get("manual_target_soc"),
        "manual_release_mode": payload.get("manual_release_mode"),
        "force_wallbox_stop": bool(payload.get("force_wallbox_stop")),
        "predump_floor_hold": bool(payload.get("predump_floor_hold")),
        "predump_active": bool(payload.get("predump_active")),
        "predump_allow": payload.get("predump_allow") if isinstance(payload.get("predump_allow"), dict) else None,
        "predump_target_soc": payload.get("predump_target_soc"),
        "predump_floor_soc": payload.get("predump_floor_soc"),
        "predump_consumer_landing_under_pct": payload.get("predump_consumer_landing_under_pct"),
        "predump_consumer_landing_under_wh": payload.get("predump_consumer_landing_under_wh"),
        "predump_no_grid": payload.get("predump_no_grid"),
        "predump_grid_fallback": payload.get("predump_grid_fallback"),
        "predump_hard_predump": payload.get("predump_hard_predump"),
        "predump_grid_allowed": payload.get("predump_grid_allowed"),
        "predump_grid_blocked_by_comfort": payload.get("predump_grid_blocked_by_comfort"),
        "predump_grid_export_target_w": payload.get("predump_grid_export_target_w"),
        "predump_grid_export_w": payload.get("predump_grid_export_w"),
        "predump_grid_export_headroom_w": payload.get("predump_grid_export_headroom_w"),
        "predump_grid_base_export_w": payload.get("predump_grid_base_export_w"),
        "predump_grid_battery_discharge_w": payload.get("predump_grid_battery_discharge_w"),
        "predump_grid_discharge_limit_w": payload.get("predump_grid_discharge_limit_w"),
        "predump_grid_ramp_base_w": payload.get("predump_grid_ramp_base_w"),
        "predump_grid_ramp_step_w": payload.get("predump_grid_ramp_step_w"),
        "predump_hard_grid_limit_w": payload.get("predump_hard_grid_limit_w"),
        "predump_hard_grid_uncapped_w": payload.get("predump_hard_grid_uncapped_w"),
        "predump_hard_grid_limited": bool(payload.get("predump_hard_grid_limited")),
        "predump_consumer_devices": payload.get("predump_consumer_devices"),
        "predump_consumer_load_w": payload.get("predump_consumer_load_w"),
        "predump_wallbox_min_power_w": payload.get("predump_wallbox_min_power_w"),
        "predump_wallbox_min_amp": payload.get("predump_wallbox_min_amp"),
        "predump_wallbox_min_phases": payload.get("predump_wallbox_min_phases"),
        "predump_bev_block_w": payload.get("predump_bev_block_w"),
        "predump_bev_start_ts": payload.get("predump_bev_start_ts"),
        "predump_bev_grid_latest_ts": payload.get("predump_bev_grid_latest_ts"),
        "predump_bev_remaining_wh": payload.get("predump_bev_remaining_wh"),
        "predump_grid_latest_ts": payload.get("predump_grid_latest_ts"),
        "predump_grid_remaining_wh": payload.get("predump_grid_remaining_wh"),
        "predump_grid_duration_s": payload.get("predump_grid_duration_s"),
        "predump_consumer_wait_since_ts": payload.get("predump_consumer_wait_since_ts"),
        "predump_consumer_wait_elapsed_s": payload.get("predump_consumer_wait_elapsed_s"),
        "home_feedback_guard": payload.get("home_feedback_guard") if isinstance(payload.get("home_feedback_guard"), dict) else None,
        "ts": int(time.time()),
        "service": "storage_manager",
        "ladekurve": build_ladekurve_meta(plan),
    }
    atomic_write_on_change(STATE_F, state, indent=2)


_WB_BUDGET_CONTROL_KEYS = {
    "state", "storage_state", "budget_w", "raw_iAVal_w", "iAVal_w",
    "budget_amp_1ph", "budget_amp_3ph", "storage_charge_request_w", "storage_req_w",
    "wb_possible_power_w",
    "wb_min_required_w", "wallbox_phase_transition_active",
    "wallbox_phase_transition_reserved_w", "wallbox_phase_transition_flexible_budget_w",
    "wallbox_phase_transition_target_phases", "wallbox_phase_transition_until_ts",
    "wallbox_phase_transition_source", "wallbox_phase_transition",
    "wallbox_phase_transition_grants", "wallbox_phase_transition_reserved_w_total",
    "wallbox_phase_transition_requested_w_total",
    "heatpump_running", "heatpump_running_commitment_w", "heatpump_new_start_allowed",
    "heatpump_starting", "heatpump_starting_until_ts",
    "flexible_budget_after_commitments_w",
    "wb_storage_cap_w", "wb_storage_extra_w", "wallbox_curve_reserve_w",
    "forecast_curve_landing_hold_active", "sliding_horizon_active", "evening_shortfall_wh",
    "shortfall_target_gap_pct", "forecast_floor_target_gap_pct", "latest_charge_start_ts",
    "curve_wb_relief", "curve_ref_soc", "curve_excess_pct", "curve_gap_pct",
    "adaptive_curve_relation", "direct_marketing_active", "direct_marketing_policy_target_state",
    "force_wallbox_stop", "predump_active", "predump_allow_wallbox", "predump_floor_hold",
    "predump_target_soc", "predump_bev_block_w", "live_sample_invalid", "phase_contract",
    "phases", "budget_ready", "can_start_or_hold", "real_charging", "switch_to_1p_ready",
    "iFc_w", "iMinLade_w", "iMinLade_raw_w", "iMinLade2_w", "iBattLoad_w",
    "iMaxBattLade_w", "fAvBatterie_w", "fAvBatterie900_w", "wb_fine_trim_step_w",
    "wb_fine_next_step_count", "consumer_allocations", "consumer_priority_order",
    "consumer_priority_effective_order", "heatpump_pause_request", "manager_title",
    "state_label", "display_reason", "reason", "control_owner", "control_owner_label",
    "wallbox_w", "wallbox_power_source", "auto_limit", "runtime_block_reason",
    "abregel_active", "abregel_source", "safe_start",
}

_WB_BUDGET_ENERGY_SCORE_KEYS = {
    "free_for_limbs_w", "free_for_limbs_raw_w", "bat_charge_request_w", "must_consume_w",
    "free_for_consumers_raw_w", "wallbox_phase_transition_reserved_w",
    "consumer_allocations", "consumer_priority_order", "consumer_priority_effective_order",
    "abregel_charge_request_w",
}


def _wb_budget_control_payload(budget: Dict[str, Any]) -> Dict[str, Any]:
    control = {key: budget[key] for key in _WB_BUDGET_CONTROL_KEYS if key in budget}
    energy_score = budget.get("energy_score") if isinstance(budget.get("energy_score"), dict) else {}
    control["energy_score"] = {
        key: energy_score[key]
        for key in _WB_BUDGET_ENERGY_SCORE_KEYS
        if key in energy_score
    }
    control["schema_version"] = "wb_pv_budget_control_v2"
    control["diagnostic_surface"] = os.path.basename(WB_DIAGNOSTIC_F)
    control["ts"] = safe_int(budget.get("ts"), int(time.time()))
    return control


def _write_wb_budget_diagnostics(budget: Dict[str, Any], *, now_s: Optional[float] = None) -> bool:
    now_value = float(now_s if now_s is not None else time.time())
    signature = (
        str(budget.get("state") or ""),
        str(budget.get("storage_state") or ""),
        str(budget.get("reason") or ""),
        bool(budget.get("force_wallbox_stop")),
        bool(budget.get("predump_active")),
        str(budget.get("direct_marketing_policy_target_state") or ""),
        bool(budget.get("live_sample_invalid")),
    )
    last_ts = safe_float(_WB_BUDGET_DIAGNOSTIC_STATE.get("last_write_ts"), 0.0)
    if (
        _WB_BUDGET_DIAGNOSTIC_STATE.get("signature") == signature
        and now_value - last_ts < _WB_BUDGET_DIAGNOSTIC_FORCE_INTERVAL_S
    ):
        return False
    diagnostic_budget = dict(budget)
    diagnostic_budget["schema_version"] = "wb_pv_budget_diagnostics_v1"
    diagnostic_budget["control_surface"] = os.path.basename(WB_F)
    atomic_write(WB_DIAGNOSTIC_F, diagnostic_budget)
    _remember_json_write_cache(WB_DIAGNOSTIC_F, diagnostic_budget, now_s=now_value)
    _WB_BUDGET_DIAGNOSTIC_STATE["signature"] = signature
    _WB_BUDGET_DIAGNOSTIC_STATE["last_write_ts"] = now_value
    return True


def write_wb_budget(payload: Dict[str, Any]) -> None:
    budget = dict(payload.get("budget") or {})
    runtime = payload.get("ems_budget_runtime") if isinstance(payload.get("ems_budget_runtime"), dict) else {}
    if bool(runtime.get("enabled")):
        runtime_wallbox_w = max(0, safe_int(runtime.get("wallbox_budget_w"), 0))
        budget["ems_budget_runtime"] = runtime
        budget["budget_w"] = min(max(0, safe_int(budget.get("budget_w"), 0)), runtime_wallbox_w)
        budget["raw_iAVal_w"] = min(max(0, safe_int(budget.get("raw_iAVal_w"), 0)), runtime_wallbox_w)
        budget["iAVal_w"] = min(max(0, safe_int(budget.get("iAVal_w"), 0)), runtime_wallbox_w)
        if runtime_wallbox_w <= 0:
            budget["budget_amp_1ph"] = 0
            budget["budget_amp_3ph"] = 0
            budget["runtime_block_reason"] = ",".join(runtime.get("blockers") or ["ems_budget_runtime_no_wallbox_budget"])
    budget["ts"] = int(time.time())
    w = max(0, safe_int(budget.get("budget_w"), 0))
    budget["budget_w"] = w
    if bool(budget.get("wallbox_phase_transition_active")):
        reserved_w = max(0, safe_int(budget.get("wallbox_phase_transition_reserved_w"), 0))
        flexible_w = max(
            0,
            safe_int(
                budget.get("flexible_budget_after_commitments_w"),
                budget.get("wallbox_phase_transition_flexible_budget_w", max(0, w - reserved_w)),
            ),
        )
        allocations = budget.get("consumer_allocations") if isinstance(budget.get("consumer_allocations"), dict) else {}
        allocations = dict(allocations)
        allocations["wallbox"] = reserved_w
        allocations["heatpump"] = (
            max(0, safe_int(budget.get("heatpump_running_commitment_w"), 0))
            if budget.get("heatpump_running")
            else flexible_w
        )
        allocations["heater"] = flexible_w
        budget["wallbox_phase_transition_flexible_budget_w"] = flexible_w
        budget["consumer_allocations"] = allocations
        budget["consumer_priority_order"] = ["wallbox", "heatpump", "heater"]
        budget["consumer_priority_effective_order"] = ["wallbox", "heatpump", "heater"]
        energy_score = budget.get("energy_score") if isinstance(budget.get("energy_score"), dict) else {}
        energy_score = dict(energy_score)
        energy_score["free_for_consumers_raw_w"] = w
        energy_score["free_for_limbs_w"] = flexible_w
        energy_score["wallbox_phase_transition_reserved_w"] = reserved_w
        energy_score["consumer_allocations"] = dict(allocations)
        budget["energy_score"] = energy_score
    budget.setdefault("budget_amp_1ph", max(6, min(32, int(w / 230))) if w >= 6 * 230 else 0)
    budget.setdefault("budget_amp_3ph", max(6, min(32, int(w / 690))) if w >= 6 * 690 else 0)
    budget.setdefault("manager_title", payload.get("manager_title", "Speicher-Regelung"))
    budget.setdefault("state_label", payload.get("state_label", budget.get("storage_state", "")))
    budget.setdefault("display_reason", payload.get("display_reason", budget.get("reason", "")))
    budget.setdefault("wallbox_w", payload.get("wallbox_w", 0))
    budget.setdefault("wallbox_power_source", payload.get("wallbox_power_source", ""))
    if isinstance(payload.get("auto_limit"), dict):
        budget["auto_limit"] = payload["auto_limit"]
    if isinstance(payload.get("direct_marketing_daily_report"), dict):
        budget["direct_marketing_daily_report"] = payload["direct_marketing_daily_report"]
    predump_active = bool(budget.get("predump_active") or payload.get("predump_active"))
    if predump_active or str(payload.get("state") or "").startswith("pre_discharge"):
        write_predump_consumer_plan(
            predump_active,
            allow=budget.get("predump_allow") if isinstance(budget.get("predump_allow"), dict) else payload.get("predump_allow"),
            budget_w=w,
            discharge_w=safe_int(payload.get("val"), 0),
            reason=payload.get("reason", ""),
            target_soc=budget.get("predump_floor_soc", payload.get("predump_floor_soc")),
            no_grid=bool(budget.get("predump_no_grid", payload.get("predump_no_grid"))),
            grid_fallback=bool(budget.get("predump_grid_fallback", payload.get("predump_grid_fallback"))),
            state=str(payload.get("state") or "pre_discharge"),
        )
    else:
        write_predump_consumer_plan(False, reason="Pre-Dump inaktiv", state="idle")
    control_budget = _wb_budget_control_payload(budget)
    atomic_write_on_change(WB_F, control_budget, force_interval_s=_WB_BUDGET_FORCE_INTERVAL_S)
    _write_wb_budget_diagnostics(budget)


def write_storage_decision_surface(payload: Dict[str, Any]) -> None:
    try:
        record = build_storage_decision_record(payload)
        if not _json_write_payload_changed(EMS_DECISION_F, record):
            return
        write_decision_surface_record(record, path=EMS_DECISION_F)
        _remember_json_write_cache(EMS_DECISION_F, record, update_read_cache=False)
    except Exception as exc:
        log.debug("EMS-Decision-Surface für Storage konnte nicht geschrieben werden: %s", exc)


def write_safe_start_state(
    *,
    state: str = "no_data",
    reason: str = "Keine Live-Daten",
    mode: int = MODE_AUTO,
) -> None:
    """Publish a safe boot/update state without sending an RSCP command."""
    payload: Dict[str, Any] = {
        "state": state,
        "mode": mode,
        "mode_name": mode_label(mode) if mode >= 0 else "UNKNOWN",
        "reason": reason,
        "next_manager": True,
        "ts": int(time.time()),
        "service": "storage_manager",
        "safe_start": True,
    }
    payload.update(build_display(payload))
    payload["storage_dispatch_runtime"] = build_runtime_overlay(
        {}, payload, {}, now_ms=int(payload["ts"] * 1000)
    )
    atomic_write(STATE_F, payload, indent=2)
    atomic_write_on_change(
        DISPATCH_RUNTIME_F,
        payload["storage_dispatch_runtime"],
        force_interval_s=15.0,
        indent=2,
    )
    atomic_write(
        WB_F,
        {
            "state": "timeout",
            "storage_state": state,
            "budget_w": 0,
            "budget_amp_1ph": 0,
            "budget_amp_3ph": 0,
            "reason": reason,
            "display_reason": reason,
            "manager_title": "Speicher-Regelung",
            "state_label": payload.get("state_label", "Keine Live-Daten"),
            "safe_start": True,
            "ts": int(time.time()),
        },
        indent=2,
    )
    write_storage_decision_surface(payload)




def _phase5_legacy_fallback(
    legacy: Dict[str, Any],
    arbitration: Dict[str, Any],
    reason_code: Optional[str] = None,
) -> Dict[str, Any]:
    result = copy.deepcopy(legacy)
    diagnostic = copy.deepcopy(arbitration)
    if reason_code:
        blockers = list(diagnostic.get("blockers") or [])
        if reason_code not in blockers:
            blockers.insert(0, reason_code)
        diagnostic.update({
            "selected": False,
            "executable": False,
            "commands_allowed": False,
            "selected_source": "legacy_fallback",
            "selected_action": None,
            "selected_power_w": 0.0,
            "block_reason_code": reason_code,
            "blockers": blockers,
        })
    diagnostic.update({
        "requested": False,
        "issued": False,
        "request_attempted_by": None,
        "request": None,
        "power_settings_after_request": None,
        "hardware_effect": False,
    })
    result["storage_dispatch_phase5"] = diagnostic
    return result


_PHASE5_COMMAND_ACTIONS = frozenset({
    "PV_STORE",
    "HOUSE_SUPPLY",
    "ECONOMIC_EXPORT",
    "HEADROOM_EXPORT",
})


def _phase5_command_intent_error(arbitration: Dict[str, Any]) -> Optional[str]:
    """Validiert den Phase-5-Befehlsintent vor jeder Managerübersetzung."""

    action = str(arbitration.get("selected_action") or "HOLD").upper()
    power_w = max(0, safe_int(arbitration.get("selected_power_w"), 0))
    if action == "HOLD":
        effect_claimed = bool(
            arbitration.get("executable")
            or arbitration.get("commands_allowed")
            or power_w > 0
            or arbitration.get("requested")
            or arbitration.get("issued")
            or arbitration.get("hardware_effect")
            or arbitration.get("request") is not None
        )
        return "PHASE5_HOLD_EFFECT_CLAIM_INVALID" if effect_claimed else None
    if action == "GRID_CHARGE":
        return "PHASE5_GRID_CHARGE_NOT_RELEASED"
    if action not in _PHASE5_COMMAND_ACTIONS:
        return "PHASE5_ACTION_TRANSLATION_UNKNOWN"
    if not bool(
        arbitration.get("selected")
        and arbitration.get("executable")
        and arbitration.get("commands_allowed")
        and power_w >= 300
    ):
        return "PHASE5_COMMAND_INTENT_INCOMPLETE"
    return None


def _phase5_economic_export_binding_valid(arbitration: Dict[str, Any]) -> bool:
    """Bindet einen Exportintent erneut an den kanonisch ausgewählten Eco+-Slot."""

    candidate = arbitration.get("candidate") if isinstance(arbitration.get("candidate"), dict) else {}
    canonical = (
        arbitration.get("canonical_direct_marketing_slot")
        if isinstance(arbitration.get("canonical_direct_marketing_slot"), dict)
        else {}
    )
    candidate_power_w = safe_float(candidate.get("power_w"), -1.0)
    planned_w = safe_float(canonical.get("planned_w"), -1.0)
    selected_power_w = safe_float(arbitration.get("selected_power_w"), -1.0)
    return bool(
        candidate.get("action") == "ECONOMIC_EXPORT"
        and arbitration.get("selected_source") == "canonical_phase5"
        and candidate.get("selection_source") == "canonical_slot_projection"
        and candidate.get("source_action") == "eco_plus_export_candidate"
        and candidate.get("source_mode") == "eco_plus"
        and canonical.get("valid_selected_contract") is True
        and canonical.get("action") == "ECONOMIC_EXPORT"
        and canonical.get("source_action") == "eco_plus_export_candidate"
        and canonical.get("source_mode") == "eco_plus"
        and candidate.get("window_id") == canonical.get("window_id")
        and candidate.get("action_id") == canonical.get("action_id")
        and candidate.get("segment_id") == canonical.get("segment_id")
        and candidate_power_w >= 300.0
        and planned_w >= 300.0
        and abs(candidate_power_w - planned_w) <= 1.0
        and 300.0 <= selected_power_w <= planned_w + 1.0
    )


def _phase5_effectless_hold(
    legacy: Dict[str, Any],
    arbitration: Dict[str, Any],
) -> Dict[str, Any]:
    """Publiziert HOLD als Entscheidung, ohne den Legacy-Hardwarepfad zu ersetzen."""

    result = copy.deepcopy(legacy)
    diagnostic = copy.deepcopy(arbitration)
    diagnostic.update({
        "executable": False,
        "commands_allowed": False,
        "selected_power_w": 0.0,
        "requested": False,
        "issued": False,
        "request_attempted_by": None,
        "request": None,
        "power_settings_after_request": None,
        "hardware_effect": False,
        "execution_intent": {
            "class": "decision_only_hold",
            "authorized": False,
            "action": "HOLD",
            "power_w": 0,
            "owner": "legacy_storage_manager",
        },
        "translation": {
            "action": "HOLD",
            "requested_power_w": 0,
            "translated": False,
            "reason_code": "PHASE5_HOLD_NO_ACTUATION",
        },
    })
    result["storage_dispatch_phase5"] = diagnostic
    return result


def apply_storage_dispatch_phase5(
    cfg: Dict[str, Any],
    plan: Dict[str, Any],
    legacy: Dict[str, Any],
    live: Dict[str, Any],
    power_settings: Dict[str, Any],
    previous_state: Optional[Dict[str, Any]] = None,
    *,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Ersetzt eine vollständige Legacyentscheidung transaktional oder gar nicht."""

    now_value = time.time() if now_s is None else float(now_s)
    baseline = copy.deepcopy(legacy if isinstance(legacy, dict) else {})
    try:
        path_contract = storage_decision_path_contract(baseline, plan)
        arbitration = phase5_arbitration_contract(
            cfg,
            plan,
            baseline,
            live,
            power_settings,
            path_contract,
            previous_state,
            now_ms=int(now_value * 1000.0),
        )
    except Exception as exc:
        arbitration = {
            "schema_version": "storage_dispatch_phase5_v1",
            "selected": False,
            "executable": False,
            "commands_allowed": False,
            "selected_source": "legacy_fallback",
            "selected_action": None,
            "selected_power_w": 0.0,
            "block_reason_code": "PHASE5_ARBITRATION_EXCEPTION",
            "blockers": ["PHASE5_ARBITRATION_EXCEPTION"],
            "technical_blockers": ["PHASE5_ARBITRATION_EXCEPTION"],
            "arbitration_exception": type(exc).__name__,
            "hardware_effect": False,
        }
        return _phase5_legacy_fallback(baseline, arbitration)
    if not arbitration.get("selected"):
        return _phase5_legacy_fallback(baseline, arbitration)

    intent_error = _phase5_command_intent_error(arbitration)
    if intent_error:
        return _phase5_legacy_fallback(baseline, arbitration, intent_error)
    if str(arbitration.get("selected_action") or "HOLD").upper() == "HOLD":
        return _phase5_effectless_hold(baseline, arbitration)

    try:
        decision = copy.deepcopy(baseline)
        action = str(arbitration.get("selected_action") or "HOLD")
        requested_w = max(0, safe_int(arbitration.get("selected_power_w"), 0))
        max_charge_w = max(300, safe_int(decision.get("max_charge_w"), configured_charge_limit_w(cfg, live)))
        max_discharge_w = max(
            300,
            safe_int(decision.get("max_discharge_w"), configured_discharge_limit_w(cfg, live, max_charge_w)),
        )
        reason = "Kanonischer Phase-5-Dispatch: %s" % action
        translation: Dict[str, Any] = {"action": action, "requested_power_w": requested_w}

        if action == "PV_STORE":
            control = direct_marketing_pv_store_control_w(
                cfg,
                live,
                {"max_power_w": requested_w},
                {"pv_store_max_w": requested_w},
                min(max_charge_w, requested_w),
                now_value,
            )
            translation["pv_store_control"] = control
            if control.get("blocked"):
                return _phase5_legacy_fallback(baseline, arbitration, "PHASE5_PV_STORE_LIVE_GUARD")
            charge_w = min(requested_w, max_charge_w, max(0, safe_int(control.get("charge_w"), 0)))
            if charge_w < 300:
                return _phase5_legacy_fallback(baseline, arbitration, "PHASE5_PV_STORE_BELOW_MINIMUM")
            decision.update({
                "state": "direct_marketing_phase5_pv_store",
                "mode": MODE_AUTO,
                "val": charge_w,
                "priority": "direct_marketing",
                "protected": False,
                "storage_req_w": charge_w,
                "budget_w": charge_w,
                "auto_limit": charge_cap_auto_limit(cfg, charge_w, max_discharge_w, reason),
                "direct_marketing_pv_store_control": control,
            })
            translation["translated_power_w"] = charge_w
        elif action == "HOUSE_SUPPLY":
            local_deficit_w = direct_marketing_local_deficit_w(live)
            discharge_w = min(requested_w, max_discharge_w, max(0, int(local_deficit_w)))
            if discharge_w < 300:
                return _phase5_legacy_fallback(baseline, arbitration, "PHASE5_HOUSE_SUPPLY_NO_LOCAL_DEFICIT")
            decision.update({
                "state": "direct_marketing_phase5_house_supply",
                "mode": MODE_AUTO,
                "val": discharge_w,
                "priority": "direct_marketing",
                "protected": False,
                "storage_req_w": -discharge_w,
                "budget_w": discharge_w,
                "auto_limit": discharge_cap_auto_limit(cfg, max_charge_w, discharge_w, reason),
            })
            translation.update({"local_deficit_w": local_deficit_w, "translated_power_w": discharge_w})
        elif action == "ECONOMIC_EXPORT":
            if not _phase5_economic_export_binding_valid(arbitration):
                return _phase5_legacy_fallback(
                    baseline,
                    arbitration,
                    "PHASE5_ECONOMIC_EXPORT_SOURCE_NOT_RELEASED",
                )
            import_guard = direct_marketing_export_import_guard(cfg, live, requested_w)
            translation["import_guard"] = import_guard
            if import_guard.get("blocked"):
                return _phase5_legacy_fallback(baseline, arbitration, "PHASE5_EXPORT_IMPORT_GUARD")
            control = direct_marketing_export_control_w(
                cfg,
                live,
                previous_state or {},
                requested_w,
                max_discharge_w,
            )
            discharge_w = min(requested_w, max_discharge_w, max(0, safe_int(control.get("target_w"), 0)))
            if discharge_w < 300:
                return _phase5_legacy_fallback(baseline, arbitration, "PHASE5_EXPORT_BELOW_MINIMUM")
            decision.update({
                "state": "direct_marketing_eco_plus_export_phase5",
                "mode": MODE_DISCH,
                "val": discharge_w,
                "priority": "direct_marketing",
                "protected": True,
                "storage_req_w": -discharge_w,
                "budget_w": discharge_w,
                "auto_limit": {"enabled": False, "release": False, "reason": reason},
                "direct_marketing_export_control": control,
            })
            translation.update({"export_control": control, "translated_power_w": discharge_w})
        elif action == "HEADROOM_EXPORT":
            headroom = predump_grid_export_headroom(cfg, live)
            hard_limit_w = hard_predump_grid_limit_w(cfg, max_discharge_w)
            sink_limit_w = max(0, safe_int(headroom.get("discharge_limit_w"), 0))
            discharge_w = min(requested_w, max_discharge_w, hard_limit_w, sink_limit_w)
            import_guard = direct_marketing_export_import_guard(cfg, live, discharge_w)
            translation.update({"headroom": headroom, "hard_sink_limit_w": hard_limit_w, "import_guard": import_guard})
            if import_guard.get("blocked"):
                return _phase5_legacy_fallback(baseline, arbitration, "PHASE5_HEADROOM_IMPORT_GUARD")
            if discharge_w < 300:
                return _phase5_legacy_fallback(baseline, arbitration, "PHASE5_HEADROOM_SINK_ROOM_BELOW_MINIMUM")
            decision.update({
                "state": "direct_marketing_eco_plus_headroom_export_phase5",
                "mode": MODE_DISCH,
                "val": discharge_w,
                "priority": "direct_marketing",
                "protected": True,
                "storage_req_w": -discharge_w,
                "budget_w": discharge_w,
                "auto_limit": {"enabled": False, "release": False, "reason": reason},
                "predump_grid_fallback": True,
                "predump_active": True,
                "predump_grid_export_headroom": headroom,
            })
            translation["translated_power_w"] = discharge_w
        else:
            return _phase5_legacy_fallback(baseline, arbitration, "PHASE5_ACTION_TRANSLATION_UNKNOWN")

        decision.update({
            "mode_name": mode_label(safe_int(decision.get("mode"), MODE_AUTO)),
            "reason": reason,
            "display_reason": reason,
            "direct_marketing_active": True,
            "direct_marketing_action": action,
            "direct_marketing_target_state": action,
            "storage_dispatch_selected_plan_id": arbitration.get("plan_id"),
            "storage_dispatch_selected_slot_id": arbitration.get("slot_id"),
        })
        errors = hard_mode_justification_errors(live, decision)
        if errors:
            arbitration["translation_guard_errors"] = errors
            return _phase5_legacy_fallback(baseline, arbitration, "PHASE5_HARD_MODE_TRANSLATION_GUARD")
        arbitration["translation"] = translation
        arbitration["translated_mode"] = decision.get("mode")
        arbitration["translated_mode_name"] = decision.get("mode_name")
        arbitration["translated_power_w"] = decision.get("val")
        arbitration["translated_state"] = decision.get("state")
        arbitration["execution_intent"] = {
            "class": "authorized_command",
            "authorized": True,
            "action": action,
            "power_w": max(0, safe_int(decision.get("val"), 0)),
            "owner": "direct_marketing",
        }
        arbitration["requested"] = False
        arbitration["issued"] = False
        arbitration["request_attempted_by"] = None
        arbitration["request"] = None
        arbitration["power_settings_after_request"] = None
        arbitration["hardware_effect"] = False
        decision["storage_dispatch_phase5"] = arbitration
        return decision
    except Exception as exc:
        arbitration["translation_exception"] = type(exc).__name__
        return _phase5_legacy_fallback(baseline, arbitration, "PHASE5_TRANSLATION_EXCEPTION")


def _rscp_runtime_settings_complete(settings: Tuple[str, int, str, str, str]) -> bool:
    host, port, user, password, rscp_password = settings
    return bool(
        str(host or "").strip()
        and 1 <= safe_int(port, 0) <= 65535
        and str(user or "").strip()
        and str(password or "").strip()
        and str(rscp_password or "").strip()
    )


def execute_rscp_cycle(ctrl: BattCtrl, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Bindet Live-Readback, Soll und Ausgang in genau einem Managerzyklus."""
    payload_auto_limit = payload.get("auto_limit") if isinstance(payload.get("auto_limit"), dict) else {}
    ctrl.set_auto_discharge_cap(safe_int(payload.get("max_discharge_w"), 0))
    rscp_contract = rscp_command_contract(
        payload["mode"],
        payload["val"],
        current_mode=getattr(ctrl, "_mode", -1),
        auto_limit=payload_auto_limit,
        discharge_cap_w=safe_int(payload.get("max_discharge_w"), 0),
        auto_discharge_cap_w=getattr(ctrl, "_auto_discharge_cap", 0),
    )
    payload["rscp_command_contract_version"] = safe_int(rscp_contract.get("contract_version"), 0)
    payload["rscp_command_path"] = rscp_contract.get("path")
    payload["rscp_active_refresh"] = bool(rscp_contract.get("active_refresh"))
    payload["rscp_limit_refresh"] = bool(rscp_contract.get("limit_refresh"))
    payload["rscp_power_settings_reconciled"] = ctrl.reconcile_power_settings(
        payload,
        fresh=bool(
            not payload.get("live_stale")
            and payload.get("live_sample_valid", True)
            and payload.get("grid_power_valid", True)
        ),
    )
    ctrl.send(
        payload["mode"],
        payload["val"],
        force=bool(payload["protected"])
        or bool(rscp_contract.get("active_refresh"))
        or bool(rscp_contract.get("limit_refresh")),
        discharge_cap_w=safe_int(payload.get("max_discharge_w"), 0),
        auto_limit=payload_auto_limit,
    )
    payload["rscp_power_settings"] = ctrl.power_settings_diagnostics()
    return payload["rscp_power_settings"]


def finalize_storage_dispatch_phase5_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Bindet einen echten Phase-5-Request an die bereits übersetzte Managerentscheidung."""

    phase5 = payload.get("storage_dispatch_phase5") if isinstance(payload.get("storage_dispatch_phase5"), dict) else {}
    if not phase5:
        return phase5
    action = str(phase5.get("selected_action") or "HOLD").upper()
    intent = phase5.get("execution_intent") if isinstance(phase5.get("execution_intent"), dict) else {}
    translation = phase5.get("translation") if isinstance(phase5.get("translation"), dict) else {}
    translated_power_w = max(0, safe_int(translation.get("translated_power_w"), phase5.get("translated_power_w", 0)))
    request_authorized = bool(
        action in _PHASE5_COMMAND_ACTIONS
        and phase5.get("selected")
        and phase5.get("executable")
        and phase5.get("commands_allowed")
        and intent.get("authorized") is True
        and intent.get("class") == "authorized_command"
        and str(intent.get("action") or "").upper() == action
        and str(translation.get("action") or "").upper() == action
        and translated_power_w >= 300
        and payload.get("direct_marketing_active") is True
        and str(payload.get("priority") or "").lower() == "direct_marketing"
        and max(0, safe_int(payload.get("val"), 0)) >= 300
    )
    phase5["requested"] = request_authorized
    phase5["issued"] = request_authorized
    phase5["request_attempted_by"] = "storage_manager" if request_authorized else None
    phase5["hardware_effect"] = request_authorized
    phase5["request"] = {
        "mode": payload.get("mode_name"),
        "mode_value": payload.get("mode"),
        "power_w": payload.get("val"),
        "rscp_path": payload.get("rscp_command_path"),
    } if request_authorized else None
    phase5["power_settings_after_request"] = (
        copy.deepcopy(payload.get("rscp_power_settings"))
        if request_authorized
        else None
    )
    return phase5


def main() -> None:
    log.info("=== E3DC Storage Manager gestartet ===")
    cfg = load_cfg()
    cfg_ts = time.time()
    rscp_settings = rscp_settings_from_cfg(cfg)
    ctrl: Optional[BattCtrl] = BattCtrl(*rscp_settings) if _rscp_runtime_settings_complete(rscp_settings) else None
    previous_state: Dict[str, Any] = {}
    last_decision_log_sig: Optional[Tuple[Any, ...]] = None
    while not _stop:
        start = time.time()
        if start - cfg_ts > 60:
            cfg = load_cfg()
            cfg_ts = start
            new_settings = rscp_settings_from_cfg(cfg)
            if new_settings != rscp_settings:
                if ctrl:
                    ctrl.close()
                rscp_settings = new_settings
                ctrl = BattCtrl(*rscp_settings) if _rscp_runtime_settings_complete(rscp_settings) else None
        cycle_s = auto_limit_heartbeat_s(cfg) if auto_limit_heartbeat_enabled(cfg) else CYCLE_S
        if not ctrl:
            write_safe_start_state(
                state="no_config",
                reason="Keine E3DC-RSCP-Konfiguration",
                mode=-1,
            )
            log.error("Unvollständige E3DC-RSCP-Konfiguration. Warte auf Ziel und Zugangsdaten.")
            time.sleep(cycle_s)
            continue
        live = read_json_file(LIVE_F, max_age_s=30)
        if not live:
            write_safe_start_state(
                state="no_data",
                reason="Keine Live-Daten: warte auf frische Messwerte von e3dc-live.",
                mode=MODE_AUTO,
            )
            time.sleep(cycle_s)
            continue
        try:
            write_config_validation(cfg, live)
        except Exception as exc:
            log.debug("Config-Validierung konnte nicht geschrieben werden: %s", exc)
        # Pro Zyklus genau eine stabile, statisch validierte Planrevision.
        # Bei Replace, Hash- oder Generationsabweichung gibt es keinen Altplan-Fallback.
        plan = load_validated_canonical_plan_snapshot(PLAN_F, max_age_s=1800) or {}
        wb_intent = read_json_file(WB_INTENT_F, max_age_s=90)
        wb_native = read_json_file(WB_NATIVE_F, max_age_s=90)
        manual_max_age_s = manual_override_max_age_s(cfg)
        manual = read_json_file(MANUAL_OVERRIDE_F)
        if manual_override_expired(manual, start, manual_max_age_s):
            log.info("Manueller Batterie-Override abgelaufen; Automatik übernimmt.")
            try:
                os.remove(MANUAL_OVERRIDE_F)
            except FileNotFoundError:
                pass
            except Exception as exc:
                log.debug("Abgelaufener Batterie-Override konnte nicht entfernt werden: %s", exc)
            manual = {}
        energy_decision = read_json_file(ENERGY_DECISION_F, max_age_s=45)
        live = augment_consumer_live(live, energy_decision, cfg)
        payload = decide_next_cycle(cfg, live, plan, wb_intent, wb_native, manual, previous_state, start)
        runtime_suite = storage_budget_runtime_contract_suite(
            cfg,
            payload,
            plan,
            _budget_stability_shadow_state,
            _budget_executor_latch_shadow_state,
            _budget_executor_ack_shadow_state,
            now_s=start,
        )
        payload["storage_budget_contracts"] = runtime_suite
        central_ack = runtime_suite.get("central_ack") if isinstance(runtime_suite.get("central_ack"), dict) else {}
        if central_ack.get("ack_emitted"):
            payload["budget_executor_ack"] = dict(central_ack.get("ack") or {})
        runtime_contract = runtime_suite.get("runtime") if isinstance(runtime_suite.get("runtime"), dict) else {}
        payload["ems_budget_runtime"] = runtime_contract
        if bool(runtime_contract.get("enabled")) and bool(runtime_contract.get("safe_fallback")):
            payload["ems_budget_runtime_veto"] = True
            payload["ems_budget_runtime_veto_reason"] = ",".join(runtime_contract.get("blockers") or ["runtime_safe_fallback"])
            payload["mode"] = MODE_AUTO
            payload["mode_name"] = mode_label(MODE_AUTO)
            payload["val"] = 0
            payload["priority"] = "ems_budget_runtime"
            payload["state"] = "ems_budget_runtime_safe_fallback"
            payload["reason"] = "EMS-Budget-Runtime: sichere Freigabe, bis zentraler Budget-Ack gültig ist."
            payload["display_reason"] = payload["reason"]
            payload["protected"] = False
            payload["auto_limit"] = {
                "enabled": False,
                "release": True,
                "set_power_auto": False,
                "reason": "ems_budget_runtime_safe_fallback",
            }
        # Phase 5 arbitriert erst nach vollständiger Legacy-/Safetyentscheidung
        # und unmittelbar vor dem einzigen RSCP-Ausgang. Der bestätigte
        # POWER_SETTINGS-Zustand stammt aus dem vorherigen Managerzyklus; ein
        # Phase-5-Fehler verändert die Legacyentscheidung nicht.
        payload = apply_storage_dispatch_phase5(
            cfg,
            plan,
            payload,
            live,
            ctrl.power_settings_diagnostics(),
            previous_state,
            now_s=start,
        )
        payload_auto_limit = payload.get("auto_limit") if isinstance(payload.get("auto_limit"), dict) else {}
        if payload_auto_limit and payload_auto_limit.get("enabled"):
            cycle_s = max(1.0, min(3.0, safe_float(payload_auto_limit.get("heartbeat_s"), auto_limit_heartbeat_s(cfg))))
        # Aktive Eingriffe müssen gehalten werden: einige E3DC-Firmwares fallen
        # sonst nach wenigen Sekunden in interne PV-Ladung/Entladung zurück.
        # AUTO bleibt der einzige bewusst nicht ge-heartbeatete Freilaufpfad.
        execute_rscp_cycle(ctrl, payload)
        finalize_storage_dispatch_phase5_request(payload)
        payload["storage_dispatch_runtime"] = build_runtime_overlay(
            plan,
            payload,
            live,
            now_ms=int(start * 1000),
        )
        atomic_write_on_change(
            DISPATCH_RUNTIME_F,
            payload["storage_dispatch_runtime"],
            force_interval_s=15.0,
            indent=2,
        )
        acknowledge_manual_override_done(manual, payload, now_s=start)
        ems_reaction_history_due = _history_event_write_due(payload, _ems_reaction_history_event_state, now_s=start)
        try:
            write_ems_reaction_history(payload, cfg, physical_write=ems_reaction_history_due)
            if ems_reaction_history_due:
                _remember_history_event_write(payload, _ems_reaction_history_event_state, now_s=start)
        except Exception as exc:
            log.debug("EMS-Reaktionszeit konnte nicht geschrieben werden: %s", exc)
        try:
            direct_report = update_direct_marketing_daily_report(payload, plan, cfg)
            if isinstance(direct_report, dict):
                payload["direct_marketing_daily_report"] = direct_report
        except Exception as exc:
            log.debug("DV-Tagesreport konnte nicht geschrieben werden: %s", exc)
        try:
            shelly_state = direct_marketing_aux_inverter_shelly_control(cfg, start, live=payload)
            if isinstance(shelly_state, dict):
                payload["direct_marketing_aux_inverter_shelly"] = shelly_state
        except Exception as exc:
            fallback_state = {
                "schema": "direct_marketing_aux_inverter_shelly_v2",
                "ts": int(start),
                "enabled": False,
                "command_sent": False,
                "status": "exception",
                "error": type(exc).__name__,
            }
            payload["direct_marketing_aux_inverter_shelly"] = fallback_state
            log.debug("DV-Shelly-Zusatz-WR-Check konnte nicht ausgeführt werden: %s", exc)
        write_state(payload, plan)
        write_wb_budget(payload)
        decision_history_due = _history_event_write_due(payload, _decision_history_event_state, now_s=start)
        try:
            if decision_history_due:
                write_decision_history(payload, plan, cfg)
                _remember_history_event_write(payload, _decision_history_event_state, now_s=start)
            else:
                _refresh_decision_history_shadow_states(payload)
        except Exception as exc:
            log.debug("Decision-History konnte nicht geschrieben werden: %s", exc)
        write_storage_decision_surface(payload)
        previous_state = payload
        decision_log_sig = (
            payload["mode"],
            int(payload["val"] / 100) * 100,
            int(safe_int(payload.get("budget", {}).get("budget_w"), 0) / 100) * 100,
            bool(payload.get("protected")),
        )
        log_fn = log.info if decision_log_sig != last_decision_log_sig else log.debug
        log_fn(
            "[%s] Speicher-Regelung: %s -> %s %dW SOC=%.1f%% Grid=%dW PV=%dW Budget=%dW | %s",
            datetime.datetime.now().strftime("%H:%M"),
            log_safe_text(payload.get("state_label", payload["state"])),
            payload["mode_name"],
            payload["val"],
            payload["soc"],
            payload["grid_w"],
            payload["pv_w"],
            safe_int(payload.get("budget", {}).get("budget_w"), 0),
            log_safe_text(payload.get("display_reason") or payload["reason"]),
        )
        last_decision_log_sig = decision_log_sig
        elapsed = time.time() - start
        time.sleep(max(0.2, cycle_s - elapsed))
    log.info("Beende - AUTO freigeben...")
    ctrl.release()
    ctrl.close()
    atomic_write(STATE_F, {"state": "stopped", "reason": "Dienst beendet", "next_manager": True, "ts": int(time.time())}, indent=2)
    log.info("E3DC Storage Manager beendet.")


if __name__ == "__main__":
    main()
