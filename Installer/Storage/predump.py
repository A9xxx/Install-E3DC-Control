#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pre-Dump- und Verbraucher-Comfort-Helfer für den Storage Manager."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

try:
    from Installer.Storage.common import safe_float, safe_int
except ModuleNotFoundError:
    from Storage.common import safe_float, safe_int  # type: ignore

try:
    from Installer.storage_parallel_regulator import MODE_AUTO
except ModuleNotFoundError:
    from storage_parallel_regulator import MODE_AUTO  # type: ignore

try:
    from Installer.Wallbox.modes import MODE_OFF, MODE_PRICE, normalize_wb_mode
except ModuleNotFoundError:
    from Wallbox.modes import MODE_OFF, MODE_PRICE, normalize_wb_mode  # type: ignore


def cfg_bool(cfg: Dict[str, Any], key: str, default: bool = False) -> bool:
    raw = cfg.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "ja", "ein")


def auto_limit_heartbeat_s(cfg: Dict[str, Any]) -> float:
    return max(1.0, min(3.0, safe_float(cfg.get("storage_auto_limit_heartbeat_s"), 2.0)))


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
        if configured_w < live_derate_w - 50:
            return configured_w
        return max(0, hard_limit_w - buffer_w)
    if configured_w > 0:
        return configured_w
    if live_derate_w > 0:
        return max(0, live_derate_w - buffer_w)
    return 0


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


def predump_grid_export_headroom(cfg: Dict[str, Any], live: Dict[str, Any]) -> Dict[str, int]:
    target_w = configured_export_target_w(cfg, live)
    if target_w <= 0:
        return {
            "target_w": 0,
            "export_w": 0,
            "headroom_w": 0,
            "battery_discharge_w": 0,
            "base_export_w": 0,
            "discharge_limit_w": 0,
            "limited": 0,
        }
    grid_w = safe_int(live.get("Grid_Power"), 0)
    grid_ema_w = safe_int(live.get("Grid_EMA_W", grid_w), grid_w)
    export_w = max(0, -grid_w, -grid_ema_w)
    battery_discharge_w = max(0, -safe_int(live.get("Battery_Power"), 0))
    base_export_w = max(0, export_w - battery_discharge_w)
    return {
        "target_w": target_w,
        "export_w": export_w,
        "headroom_w": max(0, target_w - export_w),
        "battery_discharge_w": battery_discharge_w,
        "base_export_w": base_export_w,
        "discharge_limit_w": max(0, target_w - base_export_w),
        "limited": 1,
    }


def predump_floor_budget_w(cfg: Dict[str, Any], value_w: int) -> int:
    value = max(0, int(value_w or 0))
    if value <= 0:
        return 0
    step = predump_budget_step_w(cfg)
    floored = int(value // step) * step
    return max(300, floored) if value >= 300 else 0


def predump_grid_ramped_discharge_w(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    previous_state: Dict[str, Any],
    target_w: int,
) -> Tuple[int, bool, int, int]:
    target = max(0, int(target_w or 0))
    prev_state = str((previous_state or {}).get("state") or "")
    if prev_state == "pre_discharge" and bool((previous_state or {}).get("predump_grid_fallback")):
        previous_w = max(0, safe_int((previous_state or {}).get("val"), 0))
    else:
        # When Grid-Fallback takes over from AUTO/Wait, start from the measured
        # battery discharge instead of jumping directly to the calculated target.
        previous_w = max(0, -safe_int((live or {}).get("Battery_Power"), 0))
        if previous_w < 250:
            previous_w = 0
        previous_w = min(previous_w, target)
    if target <= previous_w:
        return target, False, previous_w, 0
    step_w = max(100, safe_int(cfg.get("predump_grid_ramp_up_w"), 300))
    ramped = min(target, previous_w + step_w)
    if 0 < ramped < 300:
        ramped = min(target, 300)
    ramped = min(target, predump_round_budget_w(cfg, ramped))
    return ramped, ramped < target, previous_w, step_w


def hard_predump_grid_limit_w(cfg: Dict[str, Any], max_discharge_w: int) -> int:
    configured_w = configured_kw_or_w(cfg.get("hard_predump_grid_max_w", 3000))
    if configured_w <= 0:
        configured_w = 3000
    return max(300, min(max(300, int(max_discharge_w or 0)), configured_w))


def augment_predump_consumer_live(live: Dict[str, Any], energy_decision: Dict[str, Any]) -> Dict[str, Any]:
    return augment_consumer_live(live, energy_decision, {})


def explicit_mode5_grid_slot_active(
    wb_intent: Dict[str, Any],
    *,
    intent_fresh: bool,
    wb_mode: int,
    now_s: float,
) -> bool:
    """Bindet ausschließlich den kanonischen, expliziten Modus-5-Netzladeslot."""
    intent_ts = safe_float(wb_intent.get("ts"), 0.0)
    if intent_ts > 100_000_000_000.0:
        intent_ts /= 1000.0
    intent_age_s = now_s - intent_ts if intent_ts > 0.0 else float("inf")
    request = str(wb_intent.get("battery_request", "") or "").strip().lower()
    reason = str(wb_intent.get("reason") or "").strip()
    vehicle_present = bool(
        wb_intent.get("active")
        and (
            wb_intent.get("car_active")
            or wb_intent.get("connected")
            or wb_intent.get("plugged")
        )
    )
    charge_permission = bool(
        wb_intent.get("charging_active")
        or wb_intent.get("start_requested")
        or safe_int(wb_intent.get("cap_amp", wb_intent.get("set_amp")), 0) > 0
    )
    return bool(
        intent_fresh
        and 0.0 <= intent_age_s <= 90.0
        and wb_mode == MODE_PRICE
        and wb_intent.get("scheduled_slot_active")
        and wb_intent.get("price_opt_active")
        and wb_intent.get("price_plan_storage_protect")
        and request == "hold_discharge"
        and reason == "slot_grid_storage_protection"
        and vehicle_present
        and charge_permission
        and not wb_intent.get("manual_pause")
        and not wb_intent.get("bev_full_blocked")
    )


def predump_wallbox_floor_hold_decision(
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
    if not cfg_bool(cfg, "predump_enable", True) or not cfg_bool(cfg, "predump_wallbox_enable", False):
        return None

    floor_soc = safe_float(
        plan.get("predump_min_soc", cfg.get("storage_predump_min_soc", cfg.get("eco_dump_min_soc"))),
        0.0,
    )
    if floor_soc <= 0.0:
        return None

    soc = safe_float(live.get("SOC"), 0.0)
    margin_pct = max(0.7, safe_float(cfg.get("predump_wallbox_floor_release_pct"), 2.0))
    if soc > floor_soc + margin_pct:
        return None

    wb_native = wb_native or {}
    wallbox_w = abs(safe_float(live.get("Wallbox_Power"), 0.0))
    intent_ts = safe_float(wb_intent.get("ts"), 0.0)
    intent_fresh = bool(wb_intent) and (intent_ts <= 0.0 or now_s - intent_ts <= 90.0)
    intent_full_blocked = bool(
        intent_fresh
        and wb_intent.get("bev_full_blocked")
        and not wb_intent.get("charging_active")
        and safe_float(wb_intent.get("wb_power_w"), 0.0) <= 250.0
    )
    wb_mode = normalize_wb_mode(wb_intent.get("wb_mode_active", cfg.get("wb1_mode", 0)))
    intent_present = bool(
        intent_fresh
        and not intent_full_blocked
        and wb_mode != MODE_OFF
        and (
            wb_intent.get("active")
            or wb_intent.get("car_active")
            or wb_intent.get("connected")
            or wb_intent.get("plugged")
            or wb_intent.get("charging_active")
            or wb_intent.get("start_requested")
            or safe_int(wb_intent.get("cap_amp", wb_intent.get("set_amp")), 0) > 0
        )
    )
    native_present = bool(wb_native.get("connected") or wb_native.get("charging_active"))
    details = wb_native.get("wb_details") or []
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        if (
            detail.get("plug")
            or detail.get("charging")
            or abs(safe_float(detail.get("power_w"), 0.0)) > 250.0
        ):
            native_present = True
            break
    if not (wallbox_w > 250.0 or intent_present or native_present):
        return None

    intent_w = (
        abs(safe_float(wb_intent.get("wb_power_w"), 0.0))
        if intent_fresh
        else 0.0
    )
    native_total_w = abs(safe_float(wb_native.get("total_power_w"), 0.0))
    for detail in details if isinstance(details, list) else []:
        if isinstance(detail, dict):
            native_total_w = max(
                native_total_w,
                abs(safe_float(detail.get("power_w"), 0.0)),
            )
    observed_wallbox_w = max(wallbox_w, intent_w, native_total_w)

    # Ein gültiger, expliziter Modus-5-Netzladeslot besitzt nach den bereits
    # vorgelagerten Hard-Safety-Gates die Wallbox. Der Pre-Dump-Floor darf
    # diesen rein wirtschaftlichen Slot nicht auf 0 A stoppen. Gleichzeitig
    # darf der anschließende Pre-Dump-Verbraucherpfad die Wallbox nicht aus dem
    # Speicher speisen. Daher bindet dieser eigene AUTO-Limit-Entscheid die
    # Speicherentladung ausschließlich an das Haus-/WP-Defizit.
    if explicit_mode5_grid_slot_active(
        wb_intent,
        intent_fresh=intent_fresh,
        wb_mode=wb_mode,
        now_s=now_s,
    ):
        house_wp_cap_w = house_heatpump_discharge_cap_w(
            live,
            observed_wallbox_w,
            max_discharge_w,
        )
        house_wp_cap_w = smooth_house_heatpump_discharge_cap_w(
            cfg,
            house_wp_cap_w,
            previous_state,
        )
        reason = (
            "Geplanter Modus-5-Netzladeslot am Pre-Dump-Floor %.1f%% "
            "(SoC %.1f%%): Wallbox bleibt freigegeben und nutzt Netz; "
            "Speicherentladung bleibt auf Haus/WP-Defizit bis %dW begrenzt"
        ) % (floor_soc, soc, house_wp_cap_w)
        return {
            "state": "wallbox_grid_slot_storage_hold",
            "mode": MODE_AUTO,
            "val": max_charge_w,
            "priority": "safety",
            "reason": reason,
            "protected": True,
            "storage_req_w": 0,
            "budget_w": 0,
            "force_wallbox_stop": False,
            "predump_floor_hold": False,
            "predump_wallbox_excluded": True,
            "wallbox_storage_protection": True,
            "scheduled_grid_charge": True,
            "house_heatpump_discharge_cap_w": house_wp_cap_w,
            "wallbox_power_w": int(round(observed_wallbox_w)),
            "auto_limit": discharge_cap_auto_limit(
                cfg,
                max_charge_w,
                house_wp_cap_w,
                reason,
            ),
        }

    canonical_predump = predump_request_from_plan(
        cfg,
        live,
        plan,
        now_s,
        max_discharge_w,
        previous_state,
    )
    canonical_wallbox_bound = bool(
        canonical_predump.get("active")
        and predump_allow_flags(cfg, canonical_predump).get("wallbox")
    )
    previous_state = previous_state or {}
    previous_ts = safe_float(previous_state.get("ts"), 0.0)
    if previous_ts > 100_000_000_000.0:
        previous_ts /= 1000.0
    previous_age_s = now_s - previous_ts if previous_ts > 0.0 else float("inf")
    previous_allow = (
        previous_state.get("predump_allow")
        if isinstance(previous_state.get("predump_allow"), dict)
        else {}
    )
    previous_devices = {
        device.strip()
        for device in str(previous_state.get("predump_consumer_devices") or "").split(",")
        if device.strip()
    }
    previous_wallbox_bound = bool(
        str(previous_state.get("state") or "") in (
            "pre_discharge_wait",
            "pre_discharge_consumer_auto",
        )
        and bool(previous_state.get("predump_active"))
        and bool(previous_allow.get("wallbox"))
        and "wallbox" in previous_devices
        and 0.0 <= previous_age_s <= 90.0
    )
    if not (canonical_wallbox_bound or previous_wallbox_bound):
        return None

    request = str(wb_intent.get("battery_request", "") or "").strip().lower()
    intent_reason = str(wb_intent.get("reason") or "").strip()
    protected_wallbox_grid_charge = bool(
        intent_fresh
        and request == "hold_discharge"
        and (
            wb_intent.get("scheduled_slot_active")
            or wb_intent.get("price_plan_storage_protect")
            or intent_reason in ("slot_grid_storage_protection", "price_plan_storage_protection")
        )
    )
    auto_limit: Dict[str, Any]
    house_wp_cap_w: Optional[int] = None
    reason = (
        "Pre-Dump-Untergrenze %.1f%% erreicht (SoC %.1f%%): "
        "Wallbox wird gestoppt, Hausversorgung bleibt freigegeben"
    ) % (floor_soc, soc)
    if protected_wallbox_grid_charge:
        house_wp_cap_w = house_heatpump_discharge_cap_w(live, observed_wallbox_w, max_discharge_w)
        house_wp_cap_w = smooth_house_heatpump_discharge_cap_w(cfg, house_wp_cap_w, previous_state)
        reason = (
            f"{reason}; Wallbox-Speicherschutz aktiv, "
            f"Speicherentladung bleibt auf Haus/WP-Defizit bis {house_wp_cap_w}W begrenzt"
        )
        auto_limit = discharge_cap_auto_limit(cfg, max_charge_w, house_wp_cap_w, reason)
    else:
        auto_limit = {
            "enabled": False,
            "release": True,
            "max_charge_w": max(0, int(max_charge_w)),
            "max_discharge_w": max(0, int(max_discharge_w)),
            "discharge_start_w": 0,
            "heartbeat_s": auto_limit_heartbeat_s(cfg),
            "reason": reason,
        }
    decision = {
        "state": "wallbox_predump_floor_hold",
        "mode": MODE_AUTO,
        "val": max_charge_w,
        "priority": "safety",
        "reason": reason,
        "protected": True,
        "storage_req_w": 0,
        "budget_w": 0,
        "force_wallbox_stop": True,
        "predump_floor_hold": True,
        "auto_limit": auto_limit,
    }
    if protected_wallbox_grid_charge:
        decision["wallbox_storage_protection"] = True
        decision["scheduled_grid_charge"] = True
        decision["house_heatpump_discharge_cap_w"] = house_wp_cap_w
        decision["wallbox_power_w"] = max(
            wallbox_w,
            abs(safe_float(wb_intent.get("wb_power_w"), 0.0)),
        )
    return decision


def predump_request_from_plan(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    now_s: float,
    max_discharge_w: int,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    def timeline_dump_wh() -> float:
        points = sorted(
            (
                safe_float(item.get("ts"), 0.0),
                max(0.0, safe_float(item.get("grid_dump_w"), 0.0)),
            )
            for item in plan.get("timeline") or []
            if isinstance(item, dict) and safe_float(item.get("ts"), 0.0) > 0.0
        )
        total_wh = 0.0
        for index, (timestamp, power_w) in enumerate(points):
            next_timestamp = points[index + 1][0] if index + 1 < len(points) else timestamp + 900_000.0
            duration_h = max(0.0, min(3_600_000.0, next_timestamp - timestamp)) / 3_600_000.0
            total_wh += power_w * duration_h
        return total_wh

    def physical_headroom_gate(planned_dump_wh: float) -> Dict[str, Any]:
        meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
        required_present = (
            "adaptive_headroom_required_wh" in plan
            or "adaptive_headroom_required_wh" in meta
        )
        preventable_present = (
            "predump_preventable_clipping_wh" in plan
            or "predump_preventable_clipping_wh" in meta
        )
        required_wh = max(0.0, safe_float(
            plan.get("adaptive_headroom_required_wh", meta.get("adaptive_headroom_required_wh"))
            if required_present
            else planned_dump_wh,
            0.0,
        ))
        preventable_wh = max(
            0.0,
            safe_float(
                plan.get("predump_preventable_clipping_wh", meta.get("predump_preventable_clipping_wh"))
                if preventable_present
                else planned_dump_wh,
                0.0,
            ),
        )
        time_window = predump_time_window(cfg, plan)
        deadline_s = safe_float(time_window.get("deadline_ts"), 0.0)
        deadline_source = time_window.get("deadline_source")
        deadline_valid = bool(time_window.get("valid") and deadline_s > now_s)
        if not deadline_valid:
            explicit_request = plan.get("predump_request") if isinstance(plan.get("predump_request"), dict) else {}
            explicit_deadline = safe_float(explicit_request.get("deadline_ts"), 0.0)
            if explicit_deadline > 100_000_000_000:
                explicit_deadline /= 1000.0
            if explicit_deadline > now_s:
                deadline_s = explicit_deadline
                deadline_source = "predump_request.deadline_ts"
                deadline_valid = True
        if not deadline_valid:
            timeline_points = sorted(
                (
                    safe_float(item.get("ts"), 0.0),
                    safe_float(item.get("grid_dump_w"), 0.0),
                )
                for item in plan.get("timeline") or []
                if isinstance(item, dict) and safe_float(item.get("ts"), 0.0) > 0.0
            )
            for index, (timestamp_ms, grid_dump_w) in enumerate(timeline_points):
                timestamp_s = timestamp_ms / 1000.0 if timestamp_ms > 100_000_000_000 else timestamp_ms
                next_timestamp_ms = (
                    timeline_points[index + 1][0]
                    if index + 1 < len(timeline_points)
                    else timestamp_ms + 900_000.0
                )
                next_timestamp_s = (
                    next_timestamp_ms / 1000.0
                    if next_timestamp_ms > 100_000_000_000
                    else next_timestamp_ms
                )
                if timestamp_s <= now_s < next_timestamp_s and grid_dump_w > 0.0:
                    deadline_s = next_timestamp_s
                    deadline_source = "current_timeline_slot_end"
                    deadline_valid = deadline_s > now_s
                    break
        residual_wh = min(max(0.0, planned_dump_wh), required_wh, preventable_wh)
        if required_wh < 200.0:
            reason = "Pre-Dump blockiert: kein residualer Headroombedarf belegt"
        elif preventable_wh < 200.0:
            reason = "Pre-Dump blockiert: keine vermeidbare Abregelenergie belegt"
        elif not deadline_valid:
            reason = "Pre-Dump blockiert: physikalische Deadline fehlt oder ist abgelaufen"
        elif residual_wh < 200.0:
            reason = "Pre-Dump blockiert: residualer Headroom unter Mindestschwelle"
        else:
            reason = ""
        return {
            "allowed": not reason,
            "reason": reason,
            "required_wh": required_wh,
            "preventable_wh": preventable_wh,
            "residual_wh": residual_wh,
            "deadline_ts": deadline_s or None,
            "deadline_source": deadline_source,
            "required_source": "adaptive_headroom_required_wh" if required_present else "legacy_predump_dump_wh",
            "preventable_source": "predump_preventable_clipping_wh" if preventable_present else "legacy_predump_dump_wh",
            "accounting_contract": "RESIDUAL_HEADROOM_ONLY_NOT_DV_SALE",
        }

    def adaptive_gate(target_soc: float, planned_dump_wh: float, hard_predump: bool) -> Dict[str, Any]:
        meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
        pressure = physical_headroom_gate(planned_dump_wh)
        if not pressure.get("allowed"):
            return pressure
        if hard_predump:
            headroom_target_soc = safe_float(
                plan.get("adaptive_headroom_target_soc", meta.get("adaptive_headroom_target_soc")),
                -1.0,
            )
            if 0.0 <= headroom_target_soc < target_soc - 0.05:
                capacity_wh = safe_float(
                    plan.get("battery_capacity"),
                    safe_float(cfg.get("speichergroesse"), 10.0) * 1000.0,
                )
                soc = max(0.0, min(100.0, safe_float(live.get("SOC"), target_soc)))
                min_soc = safe_float(
                    plan.get(
                        "predump_min_soc",
                        meta.get("predump_min_soc", cfg.get("storage_predump_min_soc", cfg.get("eco_dump_min_soc"))),
                    ),
                    0.0,
                )
                effective_target_soc = max(0.0, min(target_soc, max(min_soc, headroom_target_soc)))
                effective_dump_wh = max(0.0, planned_dump_wh)
                if capacity_wh > 0.0 and soc > effective_target_soc:
                    effective_dump_wh = max(
                        effective_dump_wh,
                        (soc - effective_target_soc) * capacity_wh / 100.0,
                    )
                effective_dump_wh = min(
                    effective_dump_wh,
                    safe_float(pressure.get("residual_wh"), 0.0),
                )
                if capacity_wh > 0.0:
                    effective_target_soc = max(
                        effective_target_soc,
                        soc - effective_dump_wh * 100.0 / capacity_wh,
                    )
                return {
                    **pressure,
                    "allowed": True,
                    "target_soc": effective_target_soc,
                    "planned_dump_wh": effective_dump_wh,
                    "adaptive_headroom_target_soc": headroom_target_soc,
                    "reason": (
                        "Hard-Pre-Dump auf residualen Abregel-Headroom %.0fWh / %.1f%% begrenzt"
                        % (effective_dump_wh, effective_target_soc)
                    ),
                }
            return {
                **pressure,
                "allowed": True,
                "target_soc": target_soc,
                "planned_dump_wh": min(
                    max(0.0, planned_dump_wh),
                    safe_float(pressure.get("residual_wh"), 0.0),
                ),
                "reason": "",
            }

        evening_shortfall_wh = max(
            0.0,
            safe_float(plan.get("evening_shortfall_wh", meta.get("evening_shortfall_wh")), 0.0),
        )
        if evening_shortfall_wh >= 200.0:
            return {
                "allowed": False,
                "reason": "Pre-Dump blockiert: Abendziel-Risiko %.0fWh" % evening_shortfall_wh,
            }

        required_present = (
            "adaptive_headroom_required_wh" in plan
            or "adaptive_headroom_required_wh" in meta
        )
        adaptive_required_wh = max(
            0.0,
            safe_float(
                plan.get("adaptive_headroom_required_wh", meta.get("adaptive_headroom_required_wh")),
                0.0,
            ),
        )
        if required_present and adaptive_required_wh < 200.0:
            return {
                "allowed": False,
                "reason": "Pre-Dump blockiert: kein zusätzlicher Headroom nötig",
            }

        effective_dump_wh = max(0.0, planned_dump_wh)
        cap_reason = ""
        if effective_dump_wh < 200.0 and not required_present:
            return {
                "allowed": True,
                "target_soc": target_soc,
                "planned_dump_wh": effective_dump_wh,
                "adaptive_headroom_required_wh": None,
                "evening_shortfall_wh": evening_shortfall_wh,
                "reason": "",
            }
        if effective_dump_wh < 200.0 and required_present and adaptive_required_wh >= 200.0:
            effective_dump_wh = adaptive_required_wh
            cap_reason = "adaptiver Zusatz-Headroom %.0fWh" % adaptive_required_wh
        if required_present:
            capped_dump_wh = min(effective_dump_wh, adaptive_required_wh)
            if capped_dump_wh < effective_dump_wh - 50.0:
                cap_reason = (
                    "adaptiv begrenzt %.0fWh -> %.0fWh"
                    % (effective_dump_wh, capped_dump_wh)
                )
            effective_dump_wh = capped_dump_wh
        pressure_capped_wh = min(
            effective_dump_wh,
            safe_float(pressure.get("residual_wh"), 0.0),
        )
        if pressure_capped_wh < effective_dump_wh - 50.0:
            cap_reason = "residual begrenzt %.0fWh -> %.0fWh" % (
                effective_dump_wh,
                pressure_capped_wh,
            )
        effective_dump_wh = pressure_capped_wh

        if effective_dump_wh < 200.0:
            return {
                "allowed": False,
                "reason": "Pre-Dump blockiert: Zusatz-Headroom unter Mindestschwelle",
            }

        capacity_wh = safe_float(
            plan.get("battery_capacity"),
            safe_float(cfg.get("speichergroesse"), 10.0) * 1000.0,
        )
        soc = safe_float(live.get("SOC"), target_soc)
        effective_target_soc = target_soc
        if capacity_wh > 0.0 and effective_dump_wh < planned_dump_wh - 50.0:
            effective_target_soc = max(
                target_soc,
                max(0.0, min(100.0, soc)) - (effective_dump_wh * 100.0 / capacity_wh),
            )

        return {
            **pressure,
            "allowed": True,
            "target_soc": max(0.0, min(100.0, effective_target_soc)),
            "planned_dump_wh": effective_dump_wh,
            "adaptive_headroom_required_wh": adaptive_required_wh if required_present else None,
            "evening_shortfall_wh": evening_shortfall_wh,
            "reason": cap_reason,
        }

    explicit = plan.get("predump_request") or {}
    if explicit.get("active"):
        explicit = dict(explicit)
        hard_predump = predump_plan_is_hard(plan)
        explicit.setdefault("hard_predump", hard_predump)
        target_soc = safe_float(explicit.get("target_soc"), safe_float(live.get("SOC"), 0.0))
        planned_dump_wh = safe_float(
            explicit.get("planned_dump_wh", explicit.get("remaining_wh")),
            safe_float(plan.get("predump_dump_wh", (plan.get("target_curve_meta") or {}).get("predump_dump_wh")), 0.0),
        )
        gate = adaptive_gate(target_soc, planned_dump_wh, hard_predump)
        if not gate.get("allowed"):
            return {}
        target_soc = safe_float(gate.get("target_soc"), target_soc)
        planned_dump_wh = safe_float(gate.get("planned_dump_wh"), planned_dump_wh)
        explicit["target_soc"] = target_soc
        explicit["planned_dump_wh"] = planned_dump_wh
        explicit["headroom_required_wh"] = gate.get("required_wh")
        explicit["preventable_clipping_wh"] = gate.get("preventable_wh")
        explicit["residual_headroom_wh"] = gate.get("residual_wh")
        explicit["headroom_accounting_contract"] = gate.get("accounting_contract")
        if gate.get("adaptive_headroom_target_soc") is not None:
            explicit["adaptive_headroom_target_soc"] = gate.get("adaptive_headroom_target_soc")
        if gate.get("reason"):
            explicit["reason"] = "%s; %s" % (explicit.get("reason") or "Pre-Dump aktiv", gate["reason"])
        reopen_block = predump_reopen_blocked(cfg, live, now_s, target_soc, previous_state)
        if reopen_block:
            return {}
        capacity_wh = safe_float(
            plan.get("battery_capacity"),
            safe_float(cfg.get("speichergroesse"), 10.0) * 1000.0,
        )
        landing_band = predump_consumer_landing_band(
            cfg, live, plan, target_soc, capacity_wh, previous_state
        )
        if landing_band:
            explicit["consumer_landing_floor_soc"] = landing_band["floor_soc"]
            explicit["consumer_landing_under_pct"] = landing_band["under_pct"]
            explicit["consumer_landing_under_wh"] = landing_band["under_wh"]
        return stabilize_predump_request(cfg, live, explicit, max_discharge_w)
    if not cfg_bool(cfg, "predump_enable", True):
        return {}

    now_ms = now_s * 1000.0
    timeline = plan.get("timeline") or []
    if isinstance(timeline, list) and timeline:
        points = []
        for item in timeline:
            if not isinstance(item, dict):
                continue
            ts = safe_float(item.get("ts"), 0.0)
            if ts > 0:
                points.append((ts, item))
        points.sort(key=lambda entry: entry[0])
        for idx, (ts, item) in enumerate(points):
            next_ts = points[idx + 1][0] if idx + 1 < len(points) else ts + 15 * 60 * 1000
            slot_ms = max(60 * 1000, min(60 * 60 * 1000, next_ts - ts))
            if not (ts <= now_ms < ts + slot_ms):
                continue
            grid_dump_w = safe_int(item.get("grid_dump_w"), 0)
            if grid_dump_w <= 0:
                break
            min_soc = safe_float(
                plan.get("predump_min_soc", cfg.get("storage_predump_min_soc", cfg.get("eco_dump_min_soc"))),
                0.0,
            )
            hysteresis = max(0.3, safe_float(cfg.get("pd_traj_hyst"), 0.7))
            soc = safe_float(live.get("SOC"), 0.0)
            if min_soc > 0.0 and soc <= min_soc + hysteresis:
                return {}
            meta = plan.get("target_curve_meta") or {}
            target_soc = safe_float(plan.get("predump_curve_soc", meta.get("predump_curve_soc")), min_soc)
            legacy_timeline_energy_only = bool(
                plan.get("predump_dump_wh") is None
                and meta.get("predump_dump_wh") is None
                and plan.get("adaptive_headroom_required_wh") is None
                and meta.get("adaptive_headroom_required_wh") is None
            )
            planned_dump_wh = safe_float(plan.get("predump_dump_wh", meta.get("predump_dump_wh")), 0.0)
            if planned_dump_wh < 200.0:
                planned_dump_wh = timeline_dump_wh()
            hard_predump = predump_plan_is_hard(plan)
            gate = adaptive_gate(target_soc, planned_dump_wh, hard_predump)
            if not gate.get("allowed"):
                return {}
            target_soc = safe_float(gate.get("target_soc"), target_soc)
            planned_dump_wh = safe_float(gate.get("planned_dump_wh"), planned_dump_wh)
            reopen_block = predump_reopen_blocked(cfg, live, now_s, target_soc, previous_state)
            if reopen_block:
                return {}
            capacity_wh = safe_float(
                plan.get("battery_capacity"),
                safe_float(cfg.get("speichergroesse"), 10.0) * 1000.0,
            )
            landing_band = predump_consumer_landing_band(
                cfg, live, plan, target_soc, capacity_wh, previous_state
            )
            time_window = predump_time_window(cfg, plan)
            window_start_s = safe_float(time_window.get("start_ts"), 0.0)
            window_deadline_s = safe_float(time_window.get("deadline_ts"), 0.0)
            timeline_slot_only = bool(
                not time_window.get("valid")
                and time_window.get("reason_code") == "predump_deadline_missing"
                and window_start_s <= 0.0
            )
            if not time_window.get("valid") and not timeline_slot_only:
                return {}
            if time_window.get("valid") and (
                now_s < window_start_s or now_s >= window_deadline_s
            ):
                return {}
            if timeline_slot_only:
                time_window = {
                    **time_window,
                    "valid": True,
                    "status": "legacy_timeline_slot_only",
                    "reason_code": "predump_timeline_slot_current",
                    "deadline_source": "timeline_slot_only",
                }
            if timeline_slot_only or legacy_timeline_energy_only:
                trajectory = {
                    "phase": "active",
                    "required_w": grid_dump_w,
                    "deadline_ts": next_ts / 1000.0,
                    "deadline_source": (
                        "timeline_slot_only"
                        if timeline_slot_only
                        else time_window.get("deadline_source")
                    ),
                    "legacy_deadline_ignored": False,
                    "trajectory_soc": None,
                    "start_soc": None,
                    "hours_remaining": round(max(0.0, next_ts / 1000.0 - now_s) / 3600.0, 2),
                    "remaining_wh": round(planned_dump_wh, 0),
                }
            else:
                trajectory = predump_trajectory_state(
                    cfg,
                    live,
                    plan,
                    now_s,
                    target_soc,
                    planned_dump_wh,
                    landing_floor_soc=landing_band.get("floor_soc") if landing_band else None,
                )
            if trajectory.get("phase") in ("waiting", "expired", "done", "invalid"):
                return {}
            discharge_w = max(300, min(max_discharge_w, safe_int(trajectory.get("required_w"), grid_dump_w) or grid_dump_w))
            discharge_w = max(300, min(max_discharge_w, predump_round_budget_w(cfg, discharge_w)))
            return stabilize_predump_request(cfg, live, {
                "active": True,
                "discharge_w": discharge_w,
                "budget_w": discharge_w,
                "target_soc": target_soc,
                "hard_predump": hard_predump,
                "adaptive_headroom_required_wh": gate.get("adaptive_headroom_required_wh"),
                "adaptive_headroom_target_soc": gate.get("adaptive_headroom_target_soc"),
                "evening_shortfall_wh": gate.get("evening_shortfall_wh"),
                "headroom_required_wh": gate.get("required_wh"),
                "preventable_clipping_wh": gate.get("preventable_wh"),
                "residual_headroom_wh": gate.get("residual_wh"),
                "headroom_accounting_contract": gate.get("accounting_contract"),
                "deadline_ts": time_window.get("deadline_ts"),
                "deadline_source": time_window.get("deadline_source"),
                "legacy_deadline_ignored": time_window.get("legacy_deadline_ignored", False),
                "trajectory_soc": trajectory.get("trajectory_soc"),
                "trajectory_start_soc": trajectory.get("start_soc"),
                "hours_remaining": trajectory.get("hours_remaining"),
                "remaining_wh": trajectory.get("remaining_wh"),
                "reason": (
                    "%s Punktlandung aus StorageSimulator: %.0fW bis Ziel %.1f%%%s" % (
                        "Hard-Pre-Dump" if hard_predump else "Pre-Dump",
                        discharge_w,
                        target_soc,
                        ("; " + gate["reason"]) if gate.get("reason") else "",
                    )
                ),
                "allow": {
                    "wallbox": cfg_bool(cfg, "predump_wallbox_enable", False),
                    "heatpump": cfg_bool(cfg, "predump_heatpump_enable", False),
                    "heater": cfg_bool(cfg, "predump_heater_enable", False),
                },
                "consumer_landing_floor_soc": landing_band.get("floor_soc") if landing_band else None,
                "consumer_landing_under_pct": landing_band.get("under_pct") if landing_band else None,
                "consumer_landing_under_wh": landing_band.get("under_wh") if landing_band else None,
            }, max_discharge_w)

    meta = plan.get("target_curve_meta") or {}
    curve_soc_raw = plan.get("predump_curve_soc", meta.get("predump_curve_soc"))
    curve_soc = safe_float(curve_soc_raw, -1.0)
    dump_wh = safe_float(plan.get("predump_dump_wh", meta.get("predump_dump_wh")), 0.0)
    preventable_wh = safe_float(
        plan.get("predump_preventable_clipping_wh", meta.get("predump_preventable_clipping_wh")),
        0.0,
    )
    if curve_soc < 0.0 or dump_wh < 300.0:
        return {}
    hard_predump = predump_plan_is_hard(plan)
    gate = adaptive_gate(curve_soc, dump_wh, hard_predump)
    if not gate.get("allowed"):
        return {}
    curve_soc = safe_float(gate.get("target_soc"), curve_soc)
    dump_wh = safe_float(gate.get("planned_dump_wh"), dump_wh)
    reopen_block = predump_reopen_blocked(cfg, live, now_s, curve_soc, previous_state)
    if reopen_block:
        return {}
    soc = safe_float(live.get("SOC"), 0.0)
    capacity_wh = safe_float(plan.get("battery_capacity"), safe_float(cfg.get("speichergroesse"), 10.0) * 1000.0)
    landing_band = predump_consumer_landing_band(
        cfg, live, plan, curve_soc, capacity_wh, previous_state
    )
    trajectory = predump_trajectory_state(
        cfg,
        live,
        plan,
        now_s,
        curve_soc,
        dump_wh,
        landing_floor_soc=landing_band.get("floor_soc") if landing_band else None,
    )
    if trajectory.get("phase") in ("waiting", "expired", "done", "invalid"):
        return {}
    discharge_w = int(max(300, min(max_discharge_w, safe_int(trajectory.get("required_w"), 0))))
    discharge_w = max(300, min(max_discharge_w, predump_round_budget_w(cfg, discharge_w)))
    if discharge_w <= 0:
        return {}
    return stabilize_predump_request(cfg, live, {
        "active": True,
        "discharge_w": discharge_w,
        "budget_w": discharge_w,
        "target_soc": curve_soc,
        "hard_predump": hard_predump,
        "adaptive_headroom_required_wh": gate.get("adaptive_headroom_required_wh"),
        "adaptive_headroom_target_soc": gate.get("adaptive_headroom_target_soc"),
        "evening_shortfall_wh": gate.get("evening_shortfall_wh"),
        "headroom_required_wh": gate.get("required_wh"),
        "preventable_clipping_wh": gate.get("preventable_wh"),
        "residual_headroom_wh": gate.get("residual_wh"),
        "headroom_accounting_contract": gate.get("accounting_contract"),
        "deadline_ts": trajectory.get("deadline_ts"),
        "deadline_source": trajectory.get("deadline_source"),
        "legacy_deadline_ignored": trajectory.get("legacy_deadline_ignored", False),
        "trajectory_soc": trajectory.get("trajectory_soc"),
        "trajectory_start_soc": trajectory.get("start_soc"),
        "hours_remaining": trajectory.get("hours_remaining"),
        "remaining_wh": trajectory.get("remaining_wh"),
        "reason": (
            "%s Punktlandung: Ziel %.1f%%, Dump %.0fWh, vermeidbar %.0fWh%s"
            % (
                "Hard-Pre-Dump" if hard_predump else "Pre-Dump",
                curve_soc,
                dump_wh,
                preventable_wh,
                ("; " + gate["reason"]) if gate.get("reason") else "",
            )
        ),
        "allow": {
            "wallbox": cfg_bool(cfg, "predump_wallbox_enable", False),
            "heatpump": cfg_bool(cfg, "predump_heatpump_enable", False),
            "heater": cfg_bool(cfg, "predump_heater_enable", False),
        },
        "consumer_landing_floor_soc": landing_band.get("floor_soc") if landing_band else None,
        "consumer_landing_under_pct": landing_band.get("under_pct") if landing_band else None,
        "consumer_landing_under_wh": landing_band.get("under_wh") if landing_band else None,
    }, max_discharge_w)


def predump_plan_is_hard(plan: Dict[str, Any]) -> bool:
    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    reason = str(plan.get("predump_reason", meta.get("predump_reason", "")) or "")
    return bool(
        plan.get("hard_predump_enabled", meta.get("hard_predump_enabled", False))
        or reason.startswith("Hard-Pre-Dump")
    )


def predump_consumer_landing_band(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    target_soc: float,
    capacity_wh: float,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Small target under-band for an already running local pre-dump consumer."""

    previous_state = previous_state or {}
    previous_consumer_auto = str(previous_state.get("state") or "") == "pre_discharge_consumer_auto"
    allow = {
        "wallbox": cfg_bool(cfg, "predump_wallbox_enable", False),
        "heatpump": cfg_bool(cfg, "predump_heatpump_enable", False),
        "heater": cfg_bool(cfg, "predump_heater_enable", False),
    }
    if not any(allow.values()):
        return {}
    consumer_active_w = max(250, safe_int(cfg.get("predump_consumer_active_w"), 250))
    consumer_load_w = predump_actual_consumer_load_w(live, allow)
    if not (previous_consumer_auto or consumer_load_w >= consumer_active_w):
        return {}

    if capacity_wh <= 0.0 or target_soc <= 0.0:
        return {}

    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    predump_min_soc = safe_float(
        plan.get("predump_min_soc", meta.get("predump_min_soc", cfg.get("storage_predump_min_soc"))),
        safe_float(cfg.get("storage_predump_min_soc", cfg.get("eco_dump_min_soc")), 0.0),
    )
    emergency_floor = safe_float(cfg.get("emergency_power_reserve"), 0.0)
    hard_floor_soc = max(0.0, min(100.0, max(predump_min_soc, emergency_floor)))

    pct_limit = max(0.0, safe_float(cfg.get("predump_consumer_landing_under_pct"), 0.5))
    wh_limit = max(0.0, safe_float(cfg.get("predump_consumer_landing_under_wh"), 300.0))
    pct_limits = []
    if pct_limit > 0.0:
        pct_limits.append(pct_limit)
    if wh_limit > 0.0:
        pct_limits.append(wh_limit * 100.0 / capacity_wh)
    if not pct_limits:
        return {}

    under_pct = max(0.0, min(2.0, min(pct_limits)))
    floor_soc = max(hard_floor_soc, target_soc - under_pct)
    actual_under_pct = max(0.0, target_soc - floor_soc)
    if actual_under_pct <= 0.02:
        return {}

    return {
        "active": True,
        "floor_soc": round(floor_soc, 2),
        "under_pct": round(actual_under_pct, 3),
        "under_wh": round(actual_under_pct * capacity_wh / 100.0, 0),
        "consumer_load_w": consumer_load_w,
        "hard_floor_soc": round(hard_floor_soc, 2),
    }


def _predump_timestamp_s(value: Any) -> float:
    raw = safe_float(value, 0.0)
    return raw / 1000.0 if raw > 10000000000.0 else raw


def predump_time_window(cfg: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    """Löst genau einen fehlersicheren Pre-Dump-Zeitvertrag auf.

    ``predump_end_ts`` ist autoritativ. ``ladestart_ts`` bleibt nur als
    Kompatibilitätsrückfall für Pläne ohne explizit veröffentlichtes Ende erhalten.
    """
    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    start_raw = plan.get("predump_start_ts")
    if safe_float(start_raw, 0.0) <= 0.0:
        start_raw = meta.get("predump_start_ts")
    explicit_end_raw = plan.get("predump_end_ts")
    if safe_float(explicit_end_raw, 0.0) <= 0.0:
        explicit_end_raw = meta.get("predump_end_ts")
    legacy_end_raw = plan.get("ladestart_ts")

    start_s = _predump_timestamp_s(start_raw)
    explicit_end_s = _predump_timestamp_s(explicit_end_raw)
    legacy_end_s = _predump_timestamp_s(legacy_end_raw)
    explicit_start = start_s > 0.0
    deadline_s = explicit_end_s if explicit_end_s > 0.0 else legacy_end_s
    deadline_source = "predump_end_ts" if explicit_end_s > 0.0 else "ladestart_ts_legacy_fallback"
    legacy_ignored = bool(
        explicit_end_s > 0.0
        and legacy_end_s > 0.0
        and abs(explicit_end_s - legacy_end_s) > 1.0
    )

    base = {
        "start_ts": start_s,
        "deadline_ts": deadline_s,
        "deadline_source": deadline_source,
        "legacy_ladestart_ts": legacy_end_s or None,
        "legacy_deadline_ignored": legacy_ignored,
    }
    if deadline_s <= 0.0:
        return {
            **base,
            "valid": False,
            "status": "invalid",
            "reason_code": "predump_deadline_missing",
            "reason": "Pre-Dump-Zeitvertrag ohne Deadline",
        }
    if explicit_start and start_s >= deadline_s:
        return {
            **base,
            "valid": False,
            "status": "invalid",
            "reason_code": "predump_window_order_invalid",
            "reason": "Pre-Dump-Zeitvertrag ungültig: Start liegt nicht vor Ende",
        }
    if not explicit_start:
        window_h_raw = safe_float(cfg.get("pd_max_hours"), 5.0)
        window_h = window_h_raw if window_h_raw > 0.0 else 5.0
        start_s = deadline_s - max(0.25, window_h) * 3600.0
        base["start_ts"] = start_s
        base["start_source"] = "pd_max_hours_fallback"
    else:
        base["start_source"] = "predump_start_ts"
    return {
        **base,
        "valid": True,
        "status": "valid",
        "reason_code": "predump_time_window_valid",
        "reason": "Pre-Dump-Zeitvertrag gültig",
    }


def predump_trajectory_state(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    now_s: float,
    target_soc: float,
    planned_dump_wh: float,
    landing_floor_soc: Optional[float] = None,
) -> Dict[str, Any]:
    """Return the Pre-Dump landing ramp state for the configured start window."""
    capacity_wh = safe_float(plan.get("battery_capacity"), safe_float(cfg.get("speichergroesse"), 10.0) * 1000.0)
    if capacity_wh <= 0.0 or target_soc < 0.0 or planned_dump_wh < 200.0:
        return {}

    time_window = predump_time_window(cfg, plan)
    start_s = safe_float(time_window.get("start_ts"), 0.0)
    end_s = safe_float(time_window.get("deadline_ts"), 0.0)
    if not time_window.get("valid"):
        return {**time_window, "phase": "invalid"}

    if now_s < start_s:
        return {
            **time_window,
            "phase": "waiting",
            "reason": "Pre-Dump wartet auf Startfenster",
        }
    if now_s >= end_s:
        return {
            **time_window,
            "phase": "expired",
            "reason": "Pre-Dump-Fenster ist abgelaufen",
        }

    soc = safe_float(live.get("SOC"), 0.0)
    hysteresis = max(0.3, safe_float(cfg.get("pd_traj_hyst"), 0.7))
    stop_soc = target_soc + hysteresis
    if landing_floor_soc is not None:
        landing_floor_soc = max(0.0, min(100.0, float(landing_floor_soc)))
        if landing_floor_soc < target_soc:
            stop_soc = landing_floor_soc
    start_soc = max(0.0, min(100.0, target_soc + planned_dump_wh * 100.0 / capacity_wh))
    progress = max(0.0, min(1.0, (now_s - start_s) / max(300.0, end_s - start_s)))
    trajectory_soc = start_soc + (target_soc - start_soc) * progress
    remaining_h = max(0.25, (end_s - now_s) / 3600.0)
    remaining_wh = max(0.0, (soc - min(target_soc, stop_soc)) * capacity_wh / 100.0)
    required_w = remaining_wh / remaining_h if remaining_h > 0.0 else 0.0

    phase = "active"
    if soc <= stop_soc:
        phase = "done"
    elif soc < trajectory_soc - hysteresis:
        phase = "ahead"

    return {
        **time_window,
        "phase": phase,
        "start_soc": round(start_soc, 2),
        "trajectory_soc": round(trajectory_soc, 2),
        "target_soc": round(target_soc, 2),
        "remaining_wh": round(remaining_wh, 0),
        "required_w": max(0, int(required_w)),
        "hysteresis_pct": hysteresis,
        "landing_floor_soc": round(stop_soc, 2) if stop_soc < target_soc else None,
        "hours_remaining": round(remaining_h, 2),
    }


def predump_reopen_margin_pct(cfg: Dict[str, Any]) -> float:
    return max(
        0.8,
        safe_float(
            cfg.get("predump_reopen_soc_margin_pct"),
            safe_float(cfg.get("pd_traj_hyst"), 0.7) + 1.0,
        ),
    )


def predump_reopen_block_s(cfg: Dict[str, Any]) -> float:
    return max(0.0, safe_float(cfg.get("predump_reopen_block_s"), 900.0))


def predump_reopen_blocked(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    now_s: float,
    target_soc: float,
    previous_state: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    previous_state = previous_state or {}
    until_ts = safe_float(previous_state.get("predump_reopen_block_until_ts"), 0.0)
    if until_ts <= now_s:
        return {}
    block_target = safe_float(previous_state.get("predump_reopen_target_soc"), target_soc)
    target_delta = abs(float(target_soc) - block_target)
    if target_delta > max(0.3, safe_float(cfg.get("predump_reopen_target_change_pct"), 0.7)):
        return {}
    margin_pct = predump_reopen_margin_pct(cfg)
    reopen_floor = safe_float(previous_state.get("predump_reopen_floor_soc"), block_target + margin_pct)
    soc = safe_float(live.get("SOC"), 0.0)
    if soc > reopen_floor:
        return {}
    return {
        "blocked": True,
        "until_ts": until_ts,
        "target_soc": block_target,
        "floor_soc": reopen_floor,
        "soc": soc,
        "remaining_s": max(0.0, until_ts - now_s),
        "reason": (
            "Pre-Dump Reopen blockiert: Zielkante %.1f%% gerade erreicht, "
            "SoC %.1f%% unter Reopen-Schwelle %.1f%%"
            % (block_target, soc, reopen_floor)
        ),
    }


def predump_guarded_home_load_w(
    cfg: Dict[str, Any],
    *,
    home_w: int,
    pv_w: int,
    grid_w: int,
    grid_ema_w: int,
    battery_discharge_w: int,
) -> Tuple[int, Dict[str, int]]:
    if not cfg_bool(cfg, "predump_home_feedback_guard_enable", True):
        return max(0, int(home_w or 0)), {}

    home_w = max(0, int(home_w or 0))
    pv_w = max(0, int(pv_w or 0))
    battery_discharge_w = max(0, int(battery_discharge_w or 0))
    if home_w <= 0 or battery_discharge_w <= 0:
        return home_w, {}

    max_pv_w = max(0, safe_int(cfg.get("predump_home_feedback_guard_max_pv_w"), 1500))
    min_battery_w = max(100, safe_int(cfg.get("predump_home_feedback_guard_min_bat_w"), 500))
    min_home_w = max(300, safe_int(cfg.get("predump_home_feedback_guard_min_home_w"), 900))
    dominance_pct = max(
        0.2,
        min(1.0, safe_float(cfg.get("predump_home_feedback_guard_bat_share"), 0.6)),
    )
    if pv_w > max_pv_w or battery_discharge_w < min_battery_w:
        return home_w, {}
    if home_w < min_home_w or battery_discharge_w < int(home_w * dominance_pct):
        return home_w, {}

    margin_w = max(0, safe_int(cfg.get("predump_home_feedback_guard_margin_w"), 500))
    grid_import_w = max(0, int(grid_w or 0), int(grid_ema_w or 0))
    guarded_w = min(home_w, grid_import_w + margin_w)
    if guarded_w >= home_w - 100:
        return home_w, {}
    return guarded_w, {
        "raw_home_w": home_w,
        "guarded_home_w": guarded_w,
        "battery_discharge_w": battery_discharge_w,
        "grid_import_w": grid_import_w,
        "margin_w": margin_w,
    }


def stabilize_predump_request(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    request: Dict[str, Any],
    max_discharge_w: int,
) -> Dict[str, Any]:
    """Keep Pre-Dump from creating avoidable grid import while DISCH owns control."""
    if not request or not request.get("active"):
        return request or {}

    req = dict(request)
    allow = req.get("allow") if isinstance(req.get("allow"), dict) else {}
    if not allow:
        allow = {
            "wallbox": cfg_bool(cfg, "predump_wallbox_enable", False),
            "heatpump": cfg_bool(cfg, "predump_heatpump_enable", False),
            "heater": cfg_bool(cfg, "predump_heater_enable", False),
        }
        req["allow"] = allow

    pv_w = max(0, safe_int(live.get("PV_Power"), 0))
    grid_w = safe_int(live.get("Grid_Power"), 0)
    grid_ema_w = safe_int(live.get("Grid_EMA_W", grid_w), grid_w)
    home_w = max(0, safe_int(live.get("Home_Power"), 0))
    battery_discharge_w = max(0, -safe_int(live.get("Battery_Power"), 0))
    guarded_home_w, home_guard = predump_guarded_home_load_w(
        cfg,
        home_w=home_w,
        pv_w=pv_w,
        grid_w=grid_w,
        grid_ema_w=grid_ema_w,
        battery_discharge_w=battery_discharge_w,
    )
    wp_w = max(0, safe_int(live.get("WP_Power", live.get("Heatpump_Power")), 0))
    wallbox_w = int(abs(safe_float(live.get("Wallbox_Power"), 0.0)))
    heater_w = max(0, safe_int(live.get("heizstab_power", live.get("Heizstab_Power")), 0))

    local_load_w = guarded_home_w + wp_w
    if allow.get("wallbox"):
        local_load_w += wallbox_w
    if allow.get("heater"):
        local_load_w += heater_w

    natural_discharge_w = max(0, local_load_w - pv_w)
    grid_deadband_w = max(0, safe_int(cfg.get("predump_pause_grid_guard_w"), 120))
    grid_import_w = max(0, max(grid_w, grid_ema_w) - grid_deadband_w)

    old_discharge_w = max(0, safe_int(req.get("discharge_w"), 0))
    discharge_w = max(300, old_discharge_w, natural_discharge_w)
    if grid_import_w > 0:
        discharge_w = max(discharge_w, grid_import_w + 350)
    discharge_w = max(300, min(max_discharge_w, int(discharge_w)))
    discharge_w = max(300, min(max_discharge_w, predump_round_budget_w(cfg, discharge_w)))

    req["discharge_w"] = discharge_w
    req["budget_w"] = max(0, safe_int(req.get("budget_w"), old_discharge_w), discharge_w)
    if home_guard:
        req["home_feedback_guard"] = home_guard
    if discharge_w > old_discharge_w:
        reason = str(req.get("reason") or "Pre-Dump aktiv")
        req["reason"] = (
            f"{reason} | Netzschutz: DISCH {old_discharge_w}W -> {discharge_w}W "
            f"(Last={local_load_w}W, PV={pv_w}W, Netz={max(grid_w, grid_ema_w)}W)"
        )
        if home_guard:
            req["reason"] += (
                "; Hauslast geglättet "
                f"{home_guard['raw_home_w']}W -> {home_guard['guarded_home_w']}W "
                f"(Akku-Ist {home_guard['battery_discharge_w']}W)"
            )
        req["grid_guard_w"] = grid_import_w
        req["natural_discharge_w"] = natural_discharge_w
        if home_guard:
            req["home_feedback_guard"] = home_guard
    elif home_guard:
        reason = str(req.get("reason") or "Pre-Dump aktiv")
        req["reason"] = (
            f"{reason} | Netzschutz: Hauslast geglättet "
            f"{home_guard['raw_home_w']}W -> {home_guard['guarded_home_w']}W "
            f"(Akku-Ist {home_guard['battery_discharge_w']}W)"
        )
    return req


def predump_allow_flags(cfg: Dict[str, Any], predump: Dict[str, Any]) -> Dict[str, bool]:
    allow = predump.get("allow") if isinstance(predump.get("allow"), dict) else {}
    return {
        "wallbox": bool(allow.get("wallbox", cfg_bool(cfg, "predump_wallbox_enable", False))),
        "heatpump": bool(allow.get("heatpump", cfg_bool(cfg, "predump_heatpump_enable", False))),
        "heater": bool(allow.get("heater", cfg_bool(cfg, "predump_heater_enable", False))),
    }


def predump_budget_step_w(cfg: Dict[str, Any]) -> int:
    return max(1, safe_int(cfg.get("predump_budget_step_w"), 100))


def predump_round_budget_w(cfg: Dict[str, Any], value_w: int) -> int:
    value = max(0, int(value_w or 0))
    if value <= 0:
        return 0
    step = predump_budget_step_w(cfg)
    return int(math.ceil(value / step) * step)


def predump_heatpump_power_w(live: Dict[str, Any]) -> int:
    return max(
        0,
        safe_int(
            live.get(
                "Predump_Heatpump_Power",
                live.get("WP_Power", live.get("Heatpump_Power")),
            ),
            0,
        ),
    )


def predump_actual_consumer_load_w(live: Dict[str, Any], allow: Dict[str, bool]) -> int:
    load_w = 0
    if allow.get("wallbox"):
        load_w += int(abs(safe_float(live.get("Wallbox_Power"), 0.0)))
    if allow.get("heatpump"):
        load_w += predump_heatpump_power_w(live)
    if allow.get("heater"):
        load_w += max(0, safe_int(live.get("heizstab_power", live.get("Heizstab_Power")), 0))
    return max(0, int(load_w))


def predump_wallbox_minimum_power_w(
    cfg: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_native: Optional[Dict[str, Any]],
) -> Dict[str, int]:
    """Return the physical wallbox minimum, so Pre-Dump avoids unusable trickle budgets."""
    wb_native = wb_native or {}
    min_amp = max(6, min(32, safe_int(cfg.get("wbminladestrom", cfg.get("wb_min_amp")), 6)))

    def valid_phases(value: Any) -> int:
        phases = safe_int(value, 0)
        return phases if phases in (1, 2, 3) else 0

    fallback_phases = (
        valid_phases(wb_intent.get("detected_phases"))
        or valid_phases(wb_intent.get("phases_target"))
        or valid_phases(wb_intent.get("phases_in_use"))
        or valid_phases(wb_intent.get("phases_actual"))
        or valid_phases(wb_native.get("detected_phases"))
        or valid_phases(wb_native.get("phases_target"))
        or valid_phases(wb_native.get("phases_in_use"))
        or valid_phases(wb_native.get("phases_actual"))
    )
    if not fallback_phases:
        fallback_phases = 1 if cfg_bool(cfg, "wb_phase_1p_allowed", True) else 3

    best_phases = fallback_phases
    best_amp = min_amp
    details = wb_native.get("wb_details") or []
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            active = bool(
                detail.get("plug")
                or detail.get("charging")
                or safe_int(detail.get("amp"), 0) > 0
                or safe_int(detail.get("current_set_amp"), 0) > 0
                or abs(safe_float(detail.get("power_w"), 0.0)) > 250.0
            )
            if not active:
                continue
            detail_phases = (
                valid_phases(detail.get("phases_target"))
                or valid_phases(detail.get("phases_in_use"))
                or valid_phases(detail.get("phases_actual"))
                or fallback_phases
            )
            detail_min_amp = max(
                min_amp,
                safe_int(detail.get("min_amp", detail.get("min_current", min_amp)), min_amp),
            )
            if detail_min_amp * detail_phases > best_amp * best_phases:
                best_amp = max(6, min(32, detail_min_amp))
                best_phases = detail_phases

    return {
        "power_w": int(best_amp * 230 * max(1, best_phases)),
        "amp": int(best_amp),
        "phases": int(max(1, best_phases)),
    }


def predump_wallbox_block_window(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    predump: Dict[str, Any],
    now_s: float,
    max_discharge_w: int,
    wallbox_min_power_w: int,
) -> Dict[str, Any]:
    target_soc = safe_float(predump.get("target_soc"), -1.0)
    capacity_wh = safe_float(plan.get("battery_capacity"), safe_float(cfg.get("speichergroesse"), 10.0) * 1000.0)
    time_window = predump_time_window(cfg, plan)
    if not time_window.get("valid"):
        return {}
    deadline_s = safe_float(time_window.get("deadline_ts"), 0.0)
    if deadline_s <= 0.0:
        deadline_s = safe_float(predump.get("deadline_ts"), 0.0)
    if target_soc < 0.0 or capacity_wh <= 0.0 or deadline_s <= now_s:
        return {}

    soc = safe_float(live.get("SOC"), 0.0)
    remaining_wh = max(0.0, (soc - target_soc) * capacity_wh / 100.0)
    if remaining_wh < 100.0:
        return {}

    window_start_s = safe_float(time_window.get("start_ts"), 0.0)

    sink_power_w = min(max(0, int(max_discharge_w)), max(0, int(wallbox_min_power_w)))
    if sink_power_w < 300:
        return {}
    buffer_s = max(0.0, safe_float(cfg.get("predump_bev_start_buffer_s"), 120.0))
    sink_duration_s = remaining_wh * 3600.0 / max(300.0, float(sink_power_w))
    grid_duration_s = remaining_wh * 3600.0 / max(300.0, float(max_discharge_w or sink_power_w))
    start_s = max(window_start_s, deadline_s - sink_duration_s - buffer_s)
    grid_latest_s = max(window_start_s, deadline_s - grid_duration_s - buffer_s)
    return {
        "start_ts": start_s,
        "grid_latest_ts": grid_latest_s,
        "deadline_ts": deadline_s,
        "remaining_wh": round(remaining_wh, 0),
        "power_w": int(sink_power_w),
        "duration_s": round(sink_duration_s, 1),
        "grid_duration_s": round(grid_duration_s, 1),
        "waiting": bool(now_s < start_s),
        "grid_fallback_due": bool(now_s >= grid_latest_s),
    }


def predump_grid_fallback_window(
    cfg: Dict[str, Any],
    predump: Dict[str, Any],
    now_s: float,
    max_discharge_w: int,
) -> Dict[str, Any]:
    deadline_s = safe_float(predump.get("deadline_ts"), 0.0)
    remaining_wh = safe_float(predump.get("remaining_wh"), 0.0)
    if deadline_s <= now_s or remaining_wh < 100.0:
        return {}
    discharge_w = max(300, safe_int(max_discharge_w, 0))
    buffer_s = max(
        0.0,
        safe_float(
            cfg.get("predump_grid_start_buffer_s", cfg.get("predump_bev_start_buffer_s")),
            120.0,
        ),
    )
    duration_s = remaining_wh * 3600.0 / float(discharge_w)
    latest_s = deadline_s - duration_s - buffer_s
    return {
        "latest_ts": latest_s,
        "deadline_ts": deadline_s,
        "remaining_wh": round(remaining_wh, 0),
        "duration_s": round(duration_s, 1),
        "buffer_s": round(buffer_s, 1),
        "due": bool(now_s >= latest_s),
    }


def predump_consumer_status(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    predump: Dict[str, Any],
    wb_intent: Dict[str, Any],
    wb_native: Optional[Dict[str, Any]],
    now_s: float,
) -> Dict[str, Any]:
    allow = predump_allow_flags(cfg, predump)
    devices: List[str] = []
    wb_native = wb_native or {}
    wallbox_minimum = {"power_w": 0, "amp": 0, "phases": 0}

    if allow.get("wallbox"):
        wb_mode = normalize_wb_mode(wb_intent.get("wb_mode_active", cfg.get("wb1_mode", cfg.get("wb_mode", 0))))
        intent_ts = safe_float(wb_intent.get("ts"), 0.0)
        intent_fresh = bool(wb_intent) and (intent_ts <= 0.0 or now_s - intent_ts <= 90.0)
        intent_connected = bool(
            intent_fresh
            and wb_mode != MODE_OFF
            and (
                wb_intent.get("active")
                or wb_intent.get("car_active")
                or wb_intent.get("connected")
                or wb_intent.get("plugged")
                or wb_intent.get("start_requested")
            )
        )
        native_connected = bool(wb_native.get("connected") or wb_native.get("charging_active"))
        details = wb_native.get("wb_details") or []
        for detail in details if isinstance(details, list) else []:
            if not isinstance(detail, dict):
                continue
            if (
                detail.get("plug")
                or detail.get("charging")
                or abs(safe_float(detail.get("power_w"), 0.0)) > 250.0
            ):
                native_connected = True
                break
        if wb_mode != MODE_OFF and (intent_connected or native_connected):
            devices.append("wallbox")
            wallbox_minimum = predump_wallbox_minimum_power_w(cfg, wb_intent, wb_native)

    # Heatpump/heater managers read predump_consumer_plan.json and can start
    # from an allow signal. If they fail to take load, the wait timer falls back
    # to hard DISCH before the pre-dump deadline.
    if allow.get("heatpump"):
        devices.append("heatpump")
    if allow.get("heater"):
        devices.append("heater")

    actual_load_w = predump_actual_consumer_load_w(live, allow)
    return {
        "allow": allow,
        "devices": devices,
        "available": bool(devices),
        "actual_load_w": actual_load_w,
        "device_label": ",".join(devices),
        "wallbox_min_power_w": safe_int(wallbox_minimum.get("power_w"), 0),
        "wallbox_min_amp": safe_int(wallbox_minimum.get("amp"), 0),
        "wallbox_min_phases": safe_int(wallbox_minimum.get("phases"), 0),
    }
