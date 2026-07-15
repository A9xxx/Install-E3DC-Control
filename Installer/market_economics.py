# -*- coding: utf-8 -*-
"""Forecast-based market economics contracts.

This module is deliberately side-effect free. It turns the prepared storage
timeline into a machine-readable owner contract, but the Storage Manager remains
the only component that may translate a contract into an RSCP command.
"""

import math
import time
from datetime import datetime


SLOT_MS = 15 * 60 * 1000
HORIZON_MS = 48 * 60 * 60 * 1000
OWNER_CONTRACT_VERSION = 1
SUPPORTED_TARIFFS = {"tibber", "awattar", "dynamic", "epex", "octopus_heat", "special"}
BILLING_PRICE_REQUIRED_TARIFFS = {"octopus_heat", "special"}
DEFAULT_ROUNDTRIP_EFFICIENCY_PCT = 85.0
DEFAULT_DEGRADATION_CT_PER_KWH = 4.0
DEFAULT_MIN_MARGIN_PCT = 10.0
DEFAULT_PROFIT_HOLD_CT_PER_KWH = 0.5
DEFAULT_MARGIN_HOLD_PCT = 5.0
DEFAULT_LATE_FILL_BUFFER_PCT = 3.0
DEFAULT_LATE_FILL_SAFETY_MIN = 10.0
DEFAULT_LATE_FILL_MIN_DELAY_MIN = 10.0
DEFAULT_AUTARKY_LOW_SOC_PCT = 20.0
DEFAULT_AUTARKY_HORIZON_BUFFER_WH = 500.0
CONSUMER_RELEASE_ACTIONS = {"grid_charge", "negative_price_absorb"}


def safe_float(value, default=0.0):
    try:
        if value is None or str(value).strip() == "":
            return float(default)
        return float(str(value).replace(",", "."))
    except Exception:
        return float(default)


def cfg_bool(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw == "":
        return bool(default)
    return raw in ("1", "true", "yes", "on", "ja", "ein")


def _clamp(value, low, high):
    return max(low, min(high, value))


def _format_t(ts):
    try:
        return datetime.fromtimestamp(float(ts) / 1000.0).strftime("%H:%M")
    except Exception:
        return ""


def _slot_ts(slot):
    return safe_float(slot.get("ts", slot.get("start_timestamp", 0.0)), 0.0)


def _slot_end_ts(slot):
    return safe_float(slot.get("end_timestamp"), _slot_ts(slot) + SLOT_MS)


def _market_ct(slot):
    return safe_float(slot.get("marketprice"), 0.0) / 10.0


def _billing_ct(slot, market_ct=None):
    if market_ct is None:
        market_ct = _market_ct(slot)
    billing = slot.get("billing_price_ct")
    if billing is None:
        billing = slot.get("billing_price")
    return safe_float(billing, market_ct)


def _has_explicit_billing_price(slot):
    for key in ("billing_price_ct", "billing_price"):
        value = slot.get(key)
        if value is not None and str(value).strip() != "":
            return True
    return False


def _duration_h(slot):
    return max(0.0, (_slot_end_ts(slot) - _slot_ts(slot)) / 3600000.0)


def _price_score(slot, min_billing_ct, max_billing_ct):
    for key in ("optimization_score", "pure_eco_score", "eco_score"):
        raw = slot.get(key)
        if raw is not None and str(raw).strip() != "":
            return _clamp(safe_float(raw, 50.0), 0.0, 100.0)
    span = max_billing_ct - min_billing_ct
    if span <= 0.001:
        return 50.0
    return _clamp(100.0 - (((_billing_ct(slot) - min_billing_ct) / span) * 100.0), 0.0, 100.0)


def _configured_float(config, primary_key, fallback_key, default):
    if isinstance(config, dict):
        primary = config.get(primary_key)
        if primary is not None and str(primary).strip() != "":
            return safe_float(primary, default)
        fallback = config.get(fallback_key)
        if fallback is not None and str(fallback).strip() != "":
            return safe_float(fallback, default)
    return float(default)


def _planned_load_w(slot):
    return max(0.0, safe_float(slot.get("planned_load_w"), 0.0))


def _configured_charge_power_w(config):
    for key in ("market_battery_max_w", "cheap_grid_battery_max_w", "maximumladeleistung"):
        value = safe_float((config or {}).get(key), 0.0)
        if value >= 300.0:
            return value
    return 0.0


def _finite_float(value, default=None):
    try:
        if value is None:
            return default
        text = str(value).strip().replace(",", ".")
        if not text or text.lower() in ("none", "null", "nan", "inf", "infinity"):
            return default
        result = float(text)
        if math.isfinite(result):
            return result
    except Exception:
        pass
    return default


def _target_curve_points(target_timeline):
    points = []
    for point in target_timeline or []:
        if not isinstance(point, dict):
            continue
        ts = _finite_float(point.get("ts", point.get("timestamp")), None)
        soc = _finite_float(point.get("soc", point.get("target_soc")), None)
        if ts is None or soc is None or ts <= 0:
            continue
        points.append({"ts": float(ts), "soc": _clamp(float(soc), 0.0, 100.0)})
    points.sort(key=lambda item: item["ts"])
    deduped = []
    for point in points:
        if deduped and abs(point["ts"] - deduped[-1]["ts"]) <= 1000.0:
            deduped[-1] = point
        else:
            deduped.append(point)
    return deduped


def _target_curve_floor_at(target_timeline, now_ms):
    points = _target_curve_points(target_timeline)
    if not points:
        return {}
    ts = float(now_ms)
    first = points[0]
    last = points[-1]
    if ts < first["ts"]:
        return {
            "active": True,
            "soc": round(first["soc"], 1),
            "source": "target_timeline_next",
            "points": len(points),
        }
    if ts > last["ts"] + SLOT_MS:
        return {
            "active": False,
            "soc": None,
            "source": "target_timeline_expired",
            "points": len(points),
        }
    previous = first
    for point in points[1:]:
        if ts <= point["ts"]:
            span = max(1.0, point["ts"] - previous["ts"])
            ratio = _clamp((ts - previous["ts"]) / span, 0.0, 1.0)
            soc = previous["soc"] + ((point["soc"] - previous["soc"]) * ratio)
            return {
                "active": True,
                "soc": round(_clamp(soc, 0.0, 100.0), 1),
                "source": "target_timeline",
                "points": len(points),
            }
        previous = point
    return {
        "active": True,
        "soc": round(last["soc"], 1),
        "source": "target_timeline_tail",
        "points": len(points),
    }


def _low_price_window(annotated, current_idx, slot_allowed=None):
    if current_idx < 0 or current_idx >= len(annotated):
        return {}
    current = annotated[current_idx]
    if slot_allowed is not None and not slot_allowed(current):
        return {}
    if not (current.get("is_low") or current.get("is_negative_billing")):
        return {}
    end_idx = current_idx
    while end_idx + 1 < len(annotated):
        slot = annotated[end_idx + 1]
        if slot_allowed is not None and not slot_allowed(slot):
            break
        if not (slot.get("is_low") or slot.get("is_negative_billing")):
            break
        if safe_float(slot.get("ts"), 0.0) > safe_float(annotated[end_idx].get("end_ts"), 0.0) + 1000.0:
            break
        end_idx += 1
    return {
        "start_ts": int(current["ts"]),
        "end_ts": int(annotated[end_idx]["end_ts"]),
        "end_idx": end_idx,
    }


def _late_fill_state(config, annotated, current_idx, now_ms, forecast, capacity_wh, efficiency, slot_allowed=None):
    if not cfg_bool((config or {}).get("market_late_fill_enable"), True):
        return {}
    if current_idx < 0 or current_idx >= len(annotated):
        return {}
    current = annotated[current_idx]
    if current.get("is_negative_billing"):
        return {}
    if not current.get("is_low"):
        return {}
    window = _low_price_window(annotated, current_idx, slot_allowed=slot_allowed)
    if not window:
        return {}
    charge_power_w = _configured_charge_power_w(config)
    if charge_power_w < 300.0:
        return {}
    need_wh = max(0.0, safe_float(forecast.get("grid_charge_need_wh"), 0.0))
    if need_wh <= 100.0:
        return {}
    capacity_wh = max(0.0, safe_float(capacity_wh, 0.0))
    buffer_pct = _clamp(
        safe_float((config or {}).get("market_late_fill_buffer_pct"), DEFAULT_LATE_FILL_BUFFER_PCT),
        0.0,
        20.0,
    )
    buffer_wh = capacity_wh * buffer_pct / 100.0
    required_storage_wh = need_wh / max(0.01, efficiency) + buffer_wh
    charge_duration_ms = int((required_storage_wh / charge_power_w) * 3600000.0)
    safety_ms = int(
        max(
            0.0,
            safe_float((config or {}).get("market_late_fill_safety_min"), DEFAULT_LATE_FILL_SAFETY_MIN),
        )
        * 60000.0
    )
    latest_start_ts = max(int(now_ms), int(window["end_ts"]) - charge_duration_ms - safety_ms)
    min_delay_ms = int(
        max(
            0.0,
            safe_float((config or {}).get("market_late_fill_min_delay_min"), DEFAULT_LATE_FILL_MIN_DELAY_MIN),
        )
        * 60000.0
    )
    wait_active = bool(latest_start_ts > int(now_ms) + min_delay_ms)
    return {
        "active": True,
        "wait_active": wait_active,
        "window_start_ts": int(window["start_ts"]),
        "window_end_ts": int(window["end_ts"]),
        "latest_start_ts": int(latest_start_ts),
        "charge_duration_min": round(charge_duration_ms / 60000.0, 1),
        "safety_min": round(safety_ms / 60000.0, 1),
        "min_delay_min": round(min_delay_ms / 60000.0, 1),
        "charge_power_w": int(round(charge_power_w)),
        "need_wh": round(need_wh, 0),
        "buffer_pct": round(buffer_pct, 1),
        "buffer_wh": round(buffer_wh, 0),
        "required_storage_wh": round(required_storage_wh, 0),
        "phase": "hold_until_late_fill" if wait_active else "charge_due",
    }


def _autarky_first_state(config, forecast, reserve, efficiency):
    enabled = cfg_bool((config or {}).get("market_autarky_first_enable"), True)
    current_soc = _clamp(safe_float((reserve or {}).get("current_soc_pct"), 0.0), 0.0, 100.0)
    low_soc_pct = _clamp(
        safe_float((config or {}).get("market_autarky_low_soc_pct"), DEFAULT_AUTARKY_LOW_SOC_PCT),
        0.0,
        100.0,
    )
    buffer_wh = max(
        0.0,
        safe_float((config or {}).get("market_autarky_horizon_buffer_wh"), DEFAULT_AUTARKY_HORIZON_BUFFER_WH),
    )
    available_discharge_wh = max(
        0.0,
        safe_float(
            (forecast or {}).get("available_discharge_wh"),
            safe_float((reserve or {}).get("available_discharge_wh"), 0.0),
        ),
    )
    future_deficit_wh = max(0.0, safe_float((forecast or {}).get("future_deficit_wh"), 0.0))
    future_pv_surplus_wh = max(0.0, safe_float((forecast or {}).get("future_pv_surplus_wh"), 0.0))
    effective_future_pv_wh = future_pv_surplus_wh * max(0.01, efficiency)
    balance_wh = available_discharge_wh + effective_future_pv_wh - future_deficit_wh
    horizon_sufficient = bool(balance_wh >= buffer_wh)
    low_soc_escape = bool(current_soc <= low_soc_pct + 0.001)
    active = bool(enabled and horizon_sufficient and not low_soc_escape)
    return {
        "enabled": bool(enabled),
        "active": active,
        "horizon_sufficient": horizon_sufficient,
        "low_soc_escape": low_soc_escape,
        "current_soc_pct": round(current_soc, 1),
        "low_soc_threshold_pct": round(low_soc_pct, 1),
        "available_discharge_wh": round(available_discharge_wh, 0),
        "future_deficit_wh": round(future_deficit_wh, 0),
        "future_pv_surplus_effective_wh": round(effective_future_pv_wh, 0),
        "balance_wh": round(balance_wh, 0),
        "buffer_wh": round(buffer_wh, 0),
    }


def _market_enabled(config, key, default=False):
    if not isinstance(config, dict):
        return bool(default)
    raw = config.get(key)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return cfg_bool(raw, default)


def _negative_price_consumer_release(config):
    if not _market_enabled(config, "cheap_grid_boost_enable", False):
        return {
            "storage": False,
            "wallbox": False,
            "heatpump": False,
            "heater": False,
        }
    return {
        "storage": _market_enabled(config, "cheap_grid_battery_enable", True),
        "wallbox": _market_enabled(config, "cheap_grid_wallbox_enable", False),
        "heatpump": _market_enabled(config, "cheap_grid_heatpump_enable", False),
        "heater": _market_enabled(config, "cheap_grid_heater_enable", False),
    }


def _consumer_release(config, action=None):
    """Return explicit normal-market consumer releases.

    Legacy ``cheap_grid_*`` flags intentionally do not unlock the normal
    forecast market path. They stay scoped to the legacy/negative-price boost.
    Storage is split because grid charging and discharge holding have different
    operator risk profiles. The normal grid-charge market path must not release
    heat pumps: they already have forecast/PV/pre-dump owners with takt
    protection, and short relative-price slots are too coarse for compressor
    protection.
    """
    storage_grid = _market_enabled(config, "market_battery_grid_charge_enable", False)
    storage_hold = _market_enabled(config, "market_battery_hold_enable", False)
    action = str(action or "").strip()
    if action == "negative_price_absorb":
        return _negative_price_consumer_release(config)
    if action in ("grid_charge", "grid_charge_candidate"):
        storage = storage_grid
    elif action == "hold_discharge":
        storage = storage_hold
    else:
        storage = bool(storage_grid or storage_hold)
    return {
        "storage": storage,
        "wallbox": _market_enabled(config, "market_wallbox_enable", False),
        "heatpump": False,
        "heater": _market_enabled(config, "market_heater_enable", False),
    }


def _current_contract_from_market_plan(market, now_ms):
    if not isinstance(market, dict):
        return None
    contract = market.get("active_contract") if isinstance(market.get("active_contract"), dict) else None
    if not contract:
        return None
    start_ms = int(safe_float(contract.get("start_ts"), 0.0))
    end_ms = int(safe_float(contract.get("end_ts"), 0.0))
    if start_ms <= int(now_ms) < end_ms:
        return contract
    return None


def _market_plan_contract_error(market, now_ms):
    if not isinstance(market, dict) or not market:
        return "market_plan_missing"
    if not cfg_bool(market.get("enabled"), False) or not cfg_bool(market.get("commands_allowed"), False):
        return "market_plan_not_allowed"
    valid_until = int(safe_float(market.get("valid_until_ts"), 0.0))
    if valid_until <= 0 or valid_until < int(now_ms):
        return "plan_expired"
    if int(safe_float(market.get("owner_contract_version"), 0.0)) != OWNER_CONTRACT_VERSION:
        return "owner_contract_mismatch"
    if not str(market.get("plan_owner") or "").startswith("market_economics:"):
        return "plan_owner_mismatch"
    if str(market.get("controller_owner") or "") != "storage_manager":
        return "controller_owner_mismatch"
    return ""


def current_market_consumer_release(storage_plan, device, config=None, now_ms=None):
    """Return the active market-plan release for one external consumer.

    This helper is intentionally read-only. It lets Wallbox, heatpump and heater
    managers consume the same Storage-Manager-owned market contract without
    reading the legacy price_boost_plan for normal price windows.
    """
    config = config or {}
    plan = storage_plan if isinstance(storage_plan, dict) else {}
    market = plan.get("market_plan") if isinstance(plan.get("market_plan"), dict) else plan
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    result = {
        "allowed": False,
        "active": False,
        "source": "market_plan",
        "action": None,
        "reason": "market_plan_inactive",
        "negative_price": False,
        "contract": None,
        "released_consumers": [],
    }
    contract_error = _market_plan_contract_error(market, now_ms)
    if contract_error:
        result["reason"] = contract_error
        return result
    contract = _current_contract_from_market_plan(market, now_ms)
    if not contract:
        result["reason"] = "no_current_contract"
        return result

    action = str(contract.get("action") or "")
    released = contract.get("released_consumers") if isinstance(contract.get("released_consumers"), list) else []
    released_set = {str(item).strip().lower() for item in released}
    result.update({
        "active": bool(action in CONSUMER_RELEASE_ACTIONS),
        "action": action,
        "reason": str(contract.get("reason") or action or "market_contract"),
        "negative_price": action == "negative_price_absorb",
        "contract": contract,
        "released_consumers": sorted(released_set),
    })
    if action not in CONSUMER_RELEASE_ACTIONS:
        result["reason"] = "contract_not_consumer_release"
        return result
    if str(device).strip().lower() not in released_set:
        result["reason"] = "consumer_not_released"
        return result
    device_key = str(device).strip().lower()
    if device_key == "heatpump" and action in ("grid_charge", "grid_charge_candidate"):
        result["reason"] = "heatpump_grid_charge_market_path_disabled"
        return result
    current_release = _consumer_release(config, action)
    if not bool(current_release.get(device_key)):
        if action == "negative_price_absorb" and not cfg_bool(config.get("cheap_grid_boost_enable"), False):
            result["reason"] = "negative_price_boost_disabled"
        else:
            result["reason"] = "consumer_release_disabled"
        return result
    cheap = plan.get("cheap_grid_charge") if isinstance(plan.get("cheap_grid_charge"), dict) else {}
    if device != "storage" and action == "grid_charge" and cfg_bool(cheap.get("active"), False):
        result["reason"] = "legacy_price_boost_active"
        return result
    result["allowed"] = True
    return result


def market_consumer_allowed(storage_plan, device, config=None, now_ms=None):
    return bool(current_market_consumer_release(storage_plan, device, config, now_ms).get("allowed"))


def _reserve_state(config, current_soc, capacity_wh, target_soc, target_timeline=None, now_ms=None):
    ep_reserve = _clamp(safe_float(config.get("ep_reserve_pct"), 8.0), 0.0, 100.0)
    current_soc = _clamp(safe_float(current_soc, 0.0), 0.0, 100.0)
    capacity_wh = max(0.0, safe_float(capacity_wh, 0.0))
    target_soc = _clamp(safe_float(target_soc, current_soc), 0.0, 100.0)
    curve_floor = _target_curve_floor_at(target_timeline, now_ms or time.time() * 1000.0)
    curve_soc = safe_float(curve_floor.get("soc"), -1.0) if curve_floor.get("active") else -1.0
    reserve_floor = ep_reserve
    reserve_source = "ep_reserve"
    if curve_soc >= 0.0 and curve_soc > reserve_floor + 0.05:
        reserve_floor = curve_soc
        reserve_source = curve_floor.get("source") or "target_timeline"
    target_soc = max(target_soc, reserve_floor)
    available_soc = max(0.0, current_soc - reserve_floor)
    return {
        "current_soc_pct": round(current_soc, 1),
        "target_soc_pct": round(target_soc, 1),
        "reserve_floor_soc_pct": round(reserve_floor, 1),
        "configured_reserve_floor_soc_pct": round(ep_reserve, 1),
        "curve_floor_soc_pct": round(curve_soc, 1) if curve_soc >= 0.0 else None,
        "reserve_floor_source": reserve_source,
        "target_curve_floor_active": bool(curve_floor.get("active")),
        "target_curve_floor_source": curve_floor.get("source", ""),
        "target_curve_floor_points": int(curve_floor.get("points", 0) or 0),
        "available_discharge_soc_pct": round(available_soc, 1),
        "available_discharge_wh": round((available_soc / 100.0) * capacity_wh, 0),
    }


def _base_plan(
    reason,
    now_ms,
    enabled=False,
    tariff_supported=False,
    commands_allowed=False,
    blocked_reasons=None,
    reserve=None,
    economics=None,
):
    return {
        "version": "market_economics_v1",
        "active": False,
        "enabled": bool(enabled),
        "shadow": not bool(commands_allowed),
        "commands_allowed": bool(commands_allowed),
        "owner_contract_version": OWNER_CONTRACT_VERSION,
        "plan_owner": "market_economics:price_based",
        "controller_owner": "storage_manager",
        "reason": reason,
        "created_ts": int(now_ms),
        "valid_until_ts": int(now_ms + SLOT_MS),
        "tariff_supported": bool(tariff_supported),
        "current": {},
        "forecast": {},
        "economics": economics or {},
        "reserve": reserve or {},
        "active_contract": None,
        "contracts": [],
        "blocked_reasons": list(blocked_reasons or []),
        "summary": {
            "state": reason,
            "active": False,
            "current_action": None,
            "current_billing_allowed": True,
            "current_billing_ct": None,
            "grid_charge_billing_limit_ct": None,
            "next_grid_charge": None,
            "blocked_reasons": list(blocked_reasons or []),
        },
    }


def _annotate_slots(raw_slots, min_billing_ct, max_billing_ct, low_cut_ct, high_cut_ct):
    annotated = []
    for slot in raw_slots:
        ts = _slot_ts(slot)
        end_ts = _slot_end_ts(slot)
        market_ct = _market_ct(slot)
        billing_ct = _billing_ct(slot, market_ct)
        score = _price_score(slot, min_billing_ct, max_billing_ct)
        hours = max(0.0, (end_ts - ts) / 3600000.0)
        pv_w = max(0.0, safe_float(slot.get("pv_w"), 0.0))
        home_w = max(0.0, safe_float(slot.get("home_w"), 0.0))
        wp_w = max(0.0, safe_float(slot.get("wp_w"), 0.0))
        load_w = home_w + wp_w + _planned_load_w(slot)
        deficit_wh = max(0.0, load_w - pv_w) * hours
        surplus_wh = max(0.0, pv_w - load_w) * hours
        is_low = billing_ct <= low_cut_ct + 0.001 or score >= 75.0
        is_high = billing_ct >= high_cut_ct - 0.001 or score <= 25.0
        annotated.append({
            "ts": ts,
            "end_ts": end_ts,
            "market_ct": market_ct,
            "billing_ct": billing_ct,
            "score": score,
            "pv_w": pv_w,
            "load_w": load_w,
            "deficit_wh": deficit_wh,
            "surplus_wh": surplus_wh,
            "is_negative_billing": billing_ct < 0.0,
            "is_negative_market": market_ct < 0.0,
            "is_low": is_low,
            "is_high": is_high,
        })
    return annotated


def _current_index(annotated, now_ms):
    for idx, slot in enumerate(annotated):
        if slot["ts"] <= now_ms < slot["end_ts"]:
            return idx
    for idx, slot in enumerate(annotated):
        if slot["ts"] >= now_ms:
            return idx
    return len(annotated) - 1 if annotated else -1


def _future_need(annotated, start_idx, reserve, efficiency):
    available_from_storage_wh = max(0.0, safe_float(reserve.get("available_discharge_wh"), 0.0))
    pv_buffer_wh = 0.0
    future_deficit_wh = 0.0
    future_surplus_wh = 0.0
    high_deficit_wh = 0.0
    uncovered_high_deficit_wh = 0.0
    best_future_high = None
    best_future_high_deficit = None

    for slot in annotated[start_idx + 1:]:
        future_deficit_wh += slot["deficit_wh"]
        future_surplus_wh += slot["surplus_wh"]
        if best_future_high is None or slot["billing_ct"] > best_future_high["billing_ct"]:
            best_future_high = slot
        if slot["deficit_wh"] > 0.0 and (best_future_high_deficit is None or slot["billing_ct"] > best_future_high_deficit["billing_ct"]):
            best_future_high_deficit = slot

        if slot["is_high"] and slot["deficit_wh"] > 0.0:
            high_deficit_wh += slot["deficit_wh"]
            covered_by_pv = min(pv_buffer_wh, slot["deficit_wh"])
            pv_buffer_wh -= covered_by_pv
            uncovered_high_deficit_wh += max(0.0, slot["deficit_wh"] - covered_by_pv)
        if slot["surplus_wh"] > 0.0:
            pv_buffer_wh += slot["surplus_wh"] * max(0.01, efficiency)

    grid_charge_need_wh = max(0.0, uncovered_high_deficit_wh - available_from_storage_wh)
    return {
        "future_deficit_wh": round(future_deficit_wh, 0),
        "future_pv_surplus_wh": round(future_surplus_wh, 0),
        "future_high_deficit_wh": round(high_deficit_wh, 0),
        "future_high_deficit_uncovered_by_pv_wh": round(uncovered_high_deficit_wh, 0),
        "available_discharge_wh": round(available_from_storage_wh, 0),
        "grid_charge_need_wh": round(grid_charge_need_wh, 0),
        "best_future_high_billing_ct": round(
            safe_float((best_future_high_deficit or best_future_high or {}).get("billing_ct"), 0.0),
            2,
        ),
        "best_future_high_ts": int((best_future_high_deficit or best_future_high or {}).get("ts", 0) or 0),
    }


def _economic_state(config, annotated, current_idx, reserve):
    efficiency_pct = _clamp(
        _configured_float(
            config,
            "market_roundtrip_efficiency_pct",
            "direct_marketing_roundtrip_efficiency_pct",
            DEFAULT_ROUNDTRIP_EFFICIENCY_PCT,
        ),
        1.0,
        100.0,
    )
    efficiency = efficiency_pct / 100.0
    degradation = max(
        0.0,
        _configured_float(
            config,
            "market_degradation_ct_per_kwh",
            "direct_marketing_degradation_ct_per_kwh",
            DEFAULT_DEGRADATION_CT_PER_KWH,
        ),
    )
    safety_correction = _clamp(
        _configured_float(
            config,
            "market_safety_correction_ct_per_kwh",
            "direct_marketing_safety_margin_ct_per_kwh",
            0.0,
        ),
        -10.0,
        50.0,
    )
    min_margin_pct = max(
        0.0,
        _configured_float(
            config,
            "market_min_margin_pct",
            "direct_marketing_min_margin_pct",
            DEFAULT_MIN_MARGIN_PCT,
        ),
    )
    profit_hold_ct = max(
        0.0,
        _configured_float(
            config,
            "market_profit_hold_ct_per_kwh",
            "direct_marketing_profit_hold_ct_per_kwh",
            DEFAULT_PROFIT_HOLD_CT_PER_KWH,
        ),
    )
    margin_hold_pct = max(
        0.0,
        _configured_float(
            config,
            "market_margin_hold_pct",
            "direct_marketing_margin_hold_pct",
            DEFAULT_MARGIN_HOLD_PCT,
        ),
    )

    current = annotated[current_idx]
    forecast = _future_need(annotated, current_idx, reserve, efficiency)
    future_benefit_ct = safe_float(forecast.get("best_future_high_billing_ct"), current["billing_ct"])
    effective_charge_cost_ct = (current["billing_ct"] / max(0.01, efficiency)) + degradation + safety_correction
    grid_spread_ct = future_benefit_ct - effective_charge_cost_ct
    grid_margin_pct = (grid_spread_ct / max(1.0, abs(effective_charge_cost_ct))) * 100.0
    negative_profit_ok = current["is_negative_billing"]
    grid_profit_ok = bool(negative_profit_ok or (grid_spread_ct >= profit_hold_ct and grid_margin_pct >= min_margin_pct))

    # Holding the battery is not grid-charging: no additional storage cycle is
    # created, so roundtrip efficiency and battery wear do not belong here.
    effective_hold_cost_ct = current["billing_ct"] + safety_correction
    future_hold_spread_ct = future_benefit_ct - effective_hold_cost_ct
    future_hold_margin_pct = (future_hold_spread_ct / max(1.0, abs(effective_hold_cost_ct))) * 100.0
    forecast_shortage_wh = safe_float(forecast.get("grid_charge_need_wh"), 0.0)
    hold_profit_ok = bool(
        forecast_shortage_wh > 100.0
        and forecast.get("future_high_deficit_wh", 0.0) > 0.0
        and future_hold_spread_ct >= profit_hold_ct
        and future_hold_margin_pct >= margin_hold_pct
    )

    economics = {
        "roundtrip_efficiency_pct": round(efficiency_pct, 1),
        "battery_cost_ct_per_kwh": round(degradation, 2),
        "safety_correction_ct_per_kwh": round(safety_correction, 2),
        "min_margin_pct": round(min_margin_pct, 1),
        "profit_hold_ct_per_kwh": round(profit_hold_ct, 2),
        "margin_hold_pct": round(margin_hold_pct, 1),
        "current_billing_ct": round(current["billing_ct"], 2),
        "future_benefit_ct": round(future_benefit_ct, 2),
        "effective_grid_charge_cost_ct": round(effective_charge_cost_ct, 2),
        "effective_hold_cost_ct": round(effective_hold_cost_ct, 2),
        "grid_spread_ct_per_kwh": round(grid_spread_ct, 2),
        "grid_margin_pct": round(grid_margin_pct, 1),
        "grid_profit_ok": grid_profit_ok,
        "hold_spread_ct_per_kwh": round(future_hold_spread_ct, 2),
        "hold_margin_pct": round(future_hold_margin_pct, 1),
        "hold_profit_ok": hold_profit_ok,
    }
    return economics, forecast, efficiency


def _grid_charge_billing_limit_ct(tariff, config, annotated, current_idx, forecast, efficiency):
    if tariff not in BILLING_PRICE_REQUIRED_TARIFFS:
        return None
    if current_idx < 0 or current_idx >= len(annotated):
        return None
    high_ts = safe_float(forecast.get("best_future_high_ts"), 0.0)
    candidates = []
    for slot in annotated[current_idx:]:
        if high_ts > 0.0 and safe_float(slot.get("ts"), 0.0) > high_ts:
            break
        candidates.append(slot)
    if not candidates:
        return None

    cheapest = min(safe_float(slot.get("billing_ct"), 0.0) for slot in candidates)
    need_wh = max(0.0, safe_float(forecast.get("grid_charge_need_wh"), 0.0)) / max(0.01, efficiency)
    charge_power_w = _configured_charge_power_w(config)
    if need_wh <= 100.0 or charge_power_w < 300.0:
        return round(cheapest, 2)

    covered_wh = 0.0
    threshold = cheapest
    for slot in sorted(candidates, key=lambda item: (safe_float(item.get("billing_ct"), 0.0), safe_float(item.get("ts"), 0.0))):
        threshold = safe_float(slot.get("billing_ct"), cheapest)
        covered_wh += charge_power_w * _duration_h({"ts": slot["ts"], "end_timestamp": slot["end_ts"]})
        if covered_wh >= need_wh:
            break
    return round(threshold, 2)


def _grid_charge_billing_allowed(tariff, slot, billing_limit_ct):
    if tariff not in BILLING_PRICE_REQUIRED_TARIFFS:
        return True
    if slot.get("is_negative_billing"):
        return True
    if billing_limit_ct is None:
        return False
    return safe_float(slot.get("billing_ct"), 0.0) <= safe_float(billing_limit_ct, 0.0) + 0.001


def _new_contract(slot, action, reason, forecast=None, economics=None, consumers=None):
    contract = {
        "start_ts": int(slot["ts"]),
        "end_ts": int(slot["end_ts"]),
        "start_t": _format_t(slot["ts"]),
        "end_t": _format_t(slot["end_ts"]),
        "action": action,
        "reason": reason,
        "market_ct": round(slot["market_ct"], 2),
        "billing_ct": round(slot["billing_ct"], 2),
        "score": round(slot["score"], 1),
    }
    if forecast:
        contract["forecast"] = forecast
    if economics:
        contract["economics"] = {
            "grid_profit_ok": bool(economics.get("grid_profit_ok")),
            "hold_profit_ok": bool(economics.get("hold_profit_ok")),
            "grid_spread_ct_per_kwh": economics.get("grid_spread_ct_per_kwh"),
            "grid_margin_pct": economics.get("grid_margin_pct"),
        }
    if consumers:
        contract["released_consumers"] = [name for name, active in consumers.items() if active]
    return contract


def _group_contracts(contracts):
    grouped = []
    for entry in sorted(contracts, key=lambda item: item["start_ts"]):
        current = grouped[-1] if grouped else None
        mergeable = (
            current is not None
            and current.get("action") == entry.get("action")
            and current.get("reason") == entry.get("reason")
            and current.get("released_consumers") == entry.get("released_consumers")
            and abs(int(current.get("end_ts", 0)) - int(entry.get("start_ts", 0))) <= 1000
        )
        if not mergeable:
            item = dict(entry)
            item["slot_count"] = 1
            item["_billing"] = [entry["billing_ct"]]
            item["_score"] = [entry["score"]]
            grouped.append(item)
            continue
        current["end_ts"] = entry["end_ts"]
        current["end_t"] = entry["end_t"]
        current["slot_count"] += 1
        current["_billing"].append(entry["billing_ct"])
        current["_score"].append(entry["score"])

    for item in grouped:
        billing = item.pop("_billing", [])
        scores = item.pop("_score", [])
        if billing:
            item["avg_billing_ct"] = round(sum(billing) / len(billing), 2)
            item["min_billing_ct"] = round(min(billing), 2)
            item["max_billing_ct"] = round(max(billing), 2)
        if scores:
            item["avg_score"] = round(sum(scores) / len(scores), 1)
    return grouped


def _compact_grid_charge_contract(contract, late_fill=None, billing_limit_ct=None, active=False):
    if not isinstance(contract, dict) or not contract:
        return None
    forecast = contract.get("forecast") if isinstance(contract.get("forecast"), dict) else {}
    economics = contract.get("economics") if isinstance(contract.get("economics"), dict) else {}
    result = {
        "active": bool(active),
        "action": str(contract.get("action") or ""),
        "reason": str(contract.get("reason") or ""),
        "start_ts": int(safe_float(contract.get("start_ts"), 0.0)),
        "end_ts": int(safe_float(contract.get("end_ts"), 0.0)),
        "start_t": contract.get("start_t"),
        "end_t": contract.get("end_t"),
        "billing_ct": contract.get("billing_ct"),
        "avg_billing_ct": contract.get("avg_billing_ct", contract.get("billing_ct")),
        "max_billing_ct": contract.get("max_billing_ct", contract.get("billing_ct")),
        "released_consumers": contract.get("released_consumers") if isinstance(contract.get("released_consumers"), list) else [],
        "grid_charge_need_wh": forecast.get("grid_charge_need_wh"),
        "grid_charge_target_soc_pct": forecast.get("grid_charge_target_soc_pct"),
        "grid_spread_ct_per_kwh": economics.get("grid_spread_ct_per_kwh"),
    }
    if billing_limit_ct is not None:
        max_billing_ct = safe_float(result.get("max_billing_ct"), safe_float(result.get("billing_ct"), 0.0))
        result["billing_limit_ct"] = round(safe_float(billing_limit_ct, 0.0), 2)
        result["billing_allowed"] = bool(max_billing_ct <= safe_float(billing_limit_ct, 0.0) + 0.001)
    if isinstance(late_fill, dict) and late_fill:
        result.update({
            "late_fill_phase": late_fill.get("phase"),
            "late_fill_wait_active": bool(late_fill.get("wait_active")),
            "late_fill_latest_start_ts": late_fill.get("latest_start_ts"),
            "late_fill_window_end_ts": late_fill.get("window_end_ts"),
            "late_fill_required_storage_wh": late_fill.get("required_storage_wh"),
        })
    return result


def _market_plan_summary(
    active_contract,
    grouped_contracts,
    current_summary,
    blocked_reasons,
    late_fill,
    billing_limit_ct=None,
    commands_allowed=False,
):
    current_action = str(active_contract.get("action") or "") if isinstance(active_contract, dict) else ""
    next_grid_charge = None
    next_grid_charge_active = False
    if current_action == "grid_charge":
        next_grid_charge = active_contract
        next_grid_charge_active = True
    else:
        for contract in grouped_contracts:
            action = str(contract.get("action") or "")
            if action in ("grid_charge", "grid_charge_candidate"):
                next_grid_charge = contract
                break

    if current_action == "grid_charge":
        state = "grid_charge_wait" if bool((late_fill or {}).get("wait_active")) else "grid_charge_due"
    elif current_action:
        state = current_action
    elif next_grid_charge:
        state = "grid_charge_candidate_pending"
    elif blocked_reasons:
        state = "blocked"
    else:
        state = "normal_forecast_control"

    return {
        "state": state,
        "active": bool(active_contract and commands_allowed),
        "current_action": current_action or None,
        "current_billing_allowed": bool((current_summary or {}).get("grid_charge_billing_allowed", True)),
        "current_billing_ct": (current_summary or {}).get("billing_ct"),
        "grid_charge_billing_limit_ct": billing_limit_ct,
        "next_grid_charge": _compact_grid_charge_contract(
            next_grid_charge,
            late_fill=late_fill,
            billing_limit_ct=billing_limit_ct,
            active=next_grid_charge_active,
        ),
        "blocked_reasons": list(blocked_reasons or []),
    }


def build_market_economics_plan(config, timeline, current_soc, capacity_wh, target_soc=None, now_ms=None, target_timeline=None):
    """Return the forecast-based price regulation contract.

    The returned plan intentionally stays in shadow mode. It is the common
    contract basis for normal price regulation; direct marketing can build the
    active sell/export branch on top of the same economics.
    """
    config = config or {}
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    reserve = _reserve_state(
        config,
        current_soc,
        capacity_wh,
        target_soc or current_soc,
        target_timeline=target_timeline,
        now_ms=now_ms,
    )
    tariff = str(config.get("stromtarif_typ", "") or "").strip().lower()
    tariff_supported = tariff in SUPPORTED_TARIFFS

    raw_slots = []
    for slot in timeline or []:
        if not isinstance(slot, dict):
            continue
        if (
            slot.get("price_available") is False
            and slot.get("eco_score_available") is False
            and slot.get("billing_price_ct") is None
            and slot.get("billing_price") is None
        ):
            continue
        ts = _slot_ts(slot)
        if ts < now_ms - SLOT_MS:
            continue
        if ts > now_ms + HORIZON_MS:
            continue
        raw_slots.append(slot)

    if not raw_slots:
        return _base_plan(
            "no_timeline",
            now_ms,
            enabled=False,
            tariff_supported=tariff_supported,
            blocked_reasons=["no_timeline"],
            reserve=reserve,
        )

    missing_billing_slots = []
    if tariff in BILLING_PRICE_REQUIRED_TARIFFS:
        for slot in raw_slots:
            if not _has_explicit_billing_price(slot):
                missing_billing_slots.append(int(_slot_ts(slot)))
        if missing_billing_slots:
            plan = _base_plan(
                "billing_price_unavailable",
                now_ms,
                enabled=False,
                tariff_supported=tariff_supported,
                commands_allowed=False,
                blocked_reasons=["billing_price_unavailable"],
                reserve=reserve,
            )
            plan["tariff"] = tariff
            plan["price_quality"] = {
                "billing_price_required": True,
                "billing_price_missing_slots": len(missing_billing_slots),
                "first_missing_billing_ts": missing_billing_slots[0],
            }
            return plan

    billing_values = [_billing_ct(slot) for slot in raw_slots]
    sorted_billing = sorted(billing_values)
    low_idx = max(0, int((len(sorted_billing) - 1) * 0.25))
    high_idx = min(len(sorted_billing) - 1, int(round((len(sorted_billing) - 1) * 0.75)))
    annotated = _annotate_slots(
        raw_slots,
        min(billing_values),
        max(billing_values),
        sorted_billing[low_idx],
        sorted_billing[high_idx],
    )
    current_idx = _current_index(annotated, now_ms)
    if current_idx < 0:
        return _base_plan(
            "no_current_slot",
            now_ms,
            enabled=False,
            tariff_supported=tariff_supported,
            blocked_reasons=["no_current_slot"],
            reserve=reserve,
        )

    economics, forecast, _efficiency = _economic_state(config, annotated, current_idx, reserve)
    forecast = dict(forecast)
    autarky_first = _autarky_first_state(config, forecast, reserve, _efficiency)
    forecast["autarky_first"] = autarky_first
    grid_charge_billing_limit_ct = _grid_charge_billing_limit_ct(tariff, config, annotated, current_idx, forecast, _efficiency)
    grid_charge_need_wh = safe_float(forecast.get("grid_charge_need_wh"), 0.0)
    capacity = max(0.0, safe_float(capacity_wh, 0.0))
    late_fill = _late_fill_state(
        config,
        annotated,
        current_idx,
        now_ms,
        forecast,
        capacity,
        _efficiency,
        slot_allowed=lambda slot: _grid_charge_billing_allowed(tariff, slot, grid_charge_billing_limit_ct),
    )
    reserve_target_soc = safe_float(reserve.get("target_soc_pct"), current_soc)
    if grid_charge_need_wh > 0.0 and capacity > 0.0:
        buffer_wh = safe_float(late_fill.get("buffer_wh"), 0.0) if late_fill else 0.0
        stored_need_wh = grid_charge_need_wh / max(0.01, _efficiency) + buffer_wh
        need_soc = (stored_need_wh / capacity) * 100.0
        grid_charge_target_soc = _clamp(
            safe_float(current_soc, 0.0) + need_soc,
            safe_float(current_soc, 0.0),
            reserve_target_soc,
        )
        forecast["grid_charge_target_soc_pct"] = round(grid_charge_target_soc, 1)
        forecast["grid_charge_target_source"] = "forecast_deficit_need"
    if late_fill:
        forecast["late_fill"] = late_fill
    current = annotated[current_idx]
    consumer_policy = _consumer_release(config)
    grid_consumers = _consumer_release(config, "grid_charge")
    negative_consumers = _consumer_release(config, "negative_price_absorb")
    hold_consumers = _consumer_release(config, "hold_discharge")
    any_grid_consumer_released = any(bool(active) for active in grid_consumers.values())
    any_negative_consumer_released = any(bool(active) for active in negative_consumers.values())
    any_consumer_released = any(bool(active) for active in consumer_policy.values()) or any_negative_consumer_released
    storage_hold_released = bool(hold_consumers.get("storage"))
    enabled = bool(tariff_supported and annotated)
    commands_allowed = bool(enabled and cfg_bool(config.get("grid_friendly_mode", 1), True))
    forecast_need_open = grid_charge_need_wh > 100.0
    normal_market_autarky_blocked = bool(autarky_first.get("active"))
    blocked_reasons = []
    if not tariff_supported:
        blocked_reasons.append("unsupported_tariff")
    if enabled and not commands_allowed:
        blocked_reasons.append("grid_friendly_mode_disabled")
    if enabled and not any_consumer_released:
        blocked_reasons.append("market_consumers_disabled")
    if enabled and forecast_need_open and not any_grid_consumer_released:
        blocked_reasons.append("market_grid_consumers_disabled")
    if enabled and economics.get("hold_profit_ok") and not storage_hold_released:
        blocked_reasons.append("market_storage_hold_disabled")
    if grid_charge_need_wh <= 100.0:
        blocked_reasons.append("forecast_pv_or_stored_energy_sufficient")
    if normal_market_autarky_blocked:
        blocked_reasons.append("autarky_first_horizon_sufficient")
    if not economics.get("grid_profit_ok"):
        blocked_reasons.append("margin_below_threshold")
    if tariff in BILLING_PRICE_REQUIRED_TARIFFS and not _grid_charge_billing_allowed(tariff, current, grid_charge_billing_limit_ct):
        blocked_reasons.append("billing_price_not_best_charge_tier")

    active_contract = None
    if current["is_negative_billing"] and any_negative_consumer_released:
        active_contract = _new_contract(
            current,
            "negative_price_absorb",
            "negative_total_price",
            forecast=forecast,
            economics=economics,
            consumers=negative_consumers,
        )
    elif (
        current["is_low"]
        and any_grid_consumer_released
        and forecast_need_open
        and not normal_market_autarky_blocked
        and economics.get("grid_profit_ok")
        and _grid_charge_billing_allowed(tariff, current, grid_charge_billing_limit_ct)
    ):
        active_contract = _new_contract(
            current,
            "grid_charge",
            "forecast_price_valley_before_deficit_peak",
            forecast=forecast,
            economics=economics,
            consumers=grid_consumers,
        )
    elif (
        economics.get("hold_profit_ok")
        and storage_hold_released
        and forecast_need_open
        and not normal_market_autarky_blocked
        and forecast.get("future_high_deficit_wh", 0.0) > 0.0
        and reserve.get("available_discharge_wh", 0.0) > 100.0
    ):
        active_contract = _new_contract(
            current,
            "hold_discharge",
            "forecast_price_peak_ahead",
            forecast=forecast,
            economics=economics,
            consumers={"storage": True},
        )

    contracts = []
    for idx, slot in enumerate(annotated):
        if idx == current_idx and active_contract:
            contracts.append(active_contract)
            continue
        slot_economics = economics
        slot_forecast = forecast
        slot_autarky_blocked = normal_market_autarky_blocked
        if idx != current_idx:
            slot_economics, slot_forecast, _slot_efficiency = _economic_state(config, annotated, idx, reserve)
            slot_forecast = dict(slot_forecast)
            slot_forecast["autarky_first"] = _autarky_first_state(config, slot_forecast, reserve, _slot_efficiency)
            slot_autarky_blocked = bool(slot_forecast["autarky_first"].get("active"))
        if slot["is_negative_billing"] and any_negative_consumer_released:
            contracts.append(_new_contract(slot, "negative_price_absorb", "negative_total_price", consumers=negative_consumers))
        elif (
            slot["is_low"]
            and any_grid_consumer_released
            and slot_forecast.get("grid_charge_need_wh", 0.0) > 100.0
            and not (normal_market_autarky_blocked or slot_autarky_blocked)
            and slot_economics.get("grid_profit_ok")
            and _grid_charge_billing_allowed(tariff, slot, grid_charge_billing_limit_ct)
        ):
            contracts.append(_new_contract(
                slot,
                "grid_charge_candidate",
                "relative_price_valley",
                forecast=slot_forecast,
                economics=slot_economics,
                consumers=grid_consumers,
            ))
    current_billing_allowed = _grid_charge_billing_allowed(tariff, current, grid_charge_billing_limit_ct)
    current_summary = {
        "ts": int(current["ts"]),
        "end_ts": int(current["end_ts"]),
        "t": _format_t(current["ts"]),
        "market_ct": round(current["market_ct"], 2),
        "billing_ct": round(current["billing_ct"], 2),
        "grid_charge_billing_allowed": bool(current_billing_allowed),
        "grid_charge_billing_limit_ct": grid_charge_billing_limit_ct,
        "score": round(current["score"], 1),
        "is_low": bool(current["is_low"]),
        "is_high": bool(current["is_high"]),
        "is_negative_billing": bool(current["is_negative_billing"]),
        "deficit_wh": round(current["deficit_wh"], 0),
        "surplus_wh": round(current["surplus_wh"], 0),
    }
    if active_contract:
        reason = active_contract["action"]
        blocked_reasons = [
            item
            for item in blocked_reasons
            if item not in (
                "margin_below_threshold",
                "forecast_pv_or_stored_energy_sufficient",
                "autarky_first_horizon_sufficient",
            )
        ]
    else:
        reason = "normal_forecast_control"

    grouped_contracts = _group_contracts(contracts)
    summary = _market_plan_summary(
        active_contract,
        grouped_contracts,
        current_summary,
        blocked_reasons,
        late_fill,
        billing_limit_ct=grid_charge_billing_limit_ct,
        commands_allowed=commands_allowed,
    )

    return {
        "version": "market_economics_v1",
        "active": bool(active_contract and commands_allowed),
        "enabled": enabled,
        "shadow": not commands_allowed,
        "commands_allowed": commands_allowed,
        "owner_contract_version": OWNER_CONTRACT_VERSION,
        "plan_owner": "market_economics:price_based",
        "controller_owner": "storage_manager",
        "reason": reason,
        "created_ts": int(now_ms),
        "valid_until_ts": int(now_ms + SLOT_MS),
        "tariff_supported": tariff_supported,
        "tariff": tariff,
        "price_quality": {
            "billing_price_required": bool(tariff in BILLING_PRICE_REQUIRED_TARIFFS),
            "billing_price_missing_slots": 0,
            "grid_charge_billing_limit_ct": grid_charge_billing_limit_ct,
        },
        "current": current_summary,
        "forecast": forecast,
        "economics": economics,
        "reserve": reserve,
        "consumer_policy": {
            "storage_grid_charge": bool(grid_consumers.get("storage")),
            "storage_hold": bool(hold_consumers.get("storage")),
            "wallbox": bool(consumer_policy.get("wallbox")),
            "heatpump": bool(consumer_policy.get("heatpump")),
            "heater": bool(consumer_policy.get("heater")),
        },
        "active_contract": active_contract,
        "contracts": grouped_contracts,
        "blocked_reasons": blocked_reasons,
        "summary": summary,
    }
