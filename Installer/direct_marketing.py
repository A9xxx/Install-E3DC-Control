# -*- coding: utf-8 -*-
"""Direktvermarktung planning helper.

This module must stay side-effect free: it reads prepared forecast slots and
returns a plan/owner contract. The Storage Manager remains the only component
that may turn a contract window into an RSCP command.

Architecture guard:
- No hard minimum battery-size gate. Capacity only limits available_export_wh
  and cycle_limit_wh.
- Profitability is decided by net sell revenue, import cost, fees,
  round-trip efficiency, degradation, safety margin, minimum profit and margin.
- Active commands always require the Storage-Manager owner contract.
"""

import math
import time
from datetime import datetime


SLOT_MS = 15 * 60 * 1000
OWNER_CONTRACT_VERSION = 1
POLICY_SCHEMA = "direct_marketing_policy_v1"
VALID_MODES = {"safe", "eco", "eco_plus", "arbitrage"}
VALID_PROFIT_PROFILES = {"standard", "aggressive", "expert"}
MODE_PROFILES = {
    "safe": {"low_score_min": 75.0, "high_score_max": 25.0, "economic_basis": "house_supply"},
    "eco": {"low_score_min": 70.0, "high_score_max": 30.0, "economic_basis": "pv_shift"},
    "eco_plus": {"low_score_min": 70.0, "high_score_max": 30.0, "economic_basis": "pv_shift"},
    "arbitrage": {"low_score_min": 75.0, "high_score_max": 35.0, "economic_basis": "grid_arbitrage"},
}


def safe_float(value, default=0.0):
    try:
        if value is None or str(value).strip() == "":
            return float(default)
        return float(str(value).replace(",", "."))
    except Exception:
        return float(default)


def _valid_soc_input(value):
    if value is None or str(value).strip() == "":
        return False
    try:
        parsed = float(str(value).replace(",", "."))
    except Exception:
        return False
    return math.isfinite(parsed) and 0.0 <= parsed <= 100.0


def safe_int(value, default=0):
    try:
        if value is None or str(value).strip() == "":
            return int(default)
        return int(round(float(str(value).replace(",", "."))))
    except Exception:
        return int(default)


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


def _normalize_mode(raw):
    mode = str(raw or "safe").strip().lower().replace("-", "_").replace(" ", "_")
    if mode in ("eco+", "eco_plus", "ecoplus"):
        return "eco_plus"
    if mode in VALID_MODES:
        return mode
    return "safe"


def _mode_profile(mode):
    return MODE_PROFILES.get(mode, MODE_PROFILES["safe"])


def _normalize_profit_profile(raw):
    profile = str(raw or "standard").strip().lower().replace("-", "_").replace(" ", "_")
    if profile in ("aggressiv", "micro", "kleinstgewinne"):
        return "aggressive"
    if profile in ("experte", "forced", "force", "forced_export"):
        return "expert"
    if profile in VALID_PROFIT_PROFILES:
        return profile
    return "standard"


def _configured_float(config, key, default):
    raw = config.get(key) if isinstance(config, dict) else None
    if raw is None or str(raw).strip() == "":
        return float(default)
    return safe_float(raw, default)


def _configured_optional_float(config, key):
    raw = config.get(key) if isinstance(config, dict) else None
    if raw is None or str(raw).strip() == "":
        return None
    return safe_float(raw, 0.0)


def _numeric_token(value):
    text = str(value or "").strip().replace(",", ".")
    keep = []
    for ch in text:
        if ch.isdigit() or ch in ".-":
            keep.append(ch)
        elif keep:
            break
    try:
        return float("".join(keep))
    except Exception:
        return None


def _parse_eeg_tariff_tiers(raw):
    tiers = []
    for line in str(raw or "").replace(";", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.replace("\t", "|").replace(":", "|").split("|") if part.strip()]
        if len(parts) < 2:
            parts = [part.strip() for part in line.split() if part.strip()]
        if len(parts) < 2:
            continue
        limit = _numeric_token(parts[0])
        rate = _numeric_token(parts[1])
        if limit is None or rate is None:
            continue
        tiers.append((max(0.0, limit), rate))
    tiers.sort(key=lambda item: item[0])
    return tiers


def _configured_pv_capacity_kwp(config):
    total = 0.0
    for key in ("forecast1", "forecast2", "forecast3", "forecast4", "forecast5"):
        raw = str((config or {}).get(key) or "").strip()
        if not raw:
            continue
        parts = [part.strip() for part in raw.split("/")]
        if len(parts) < 3:
            continue
        kwp = _numeric_token(parts[2])
        if kwp is not None and kwp > 0:
            total += kwp
    return total


def _weighted_eeg_rate_ct(tiers, capacity_kwp):
    if not tiers:
        return None
    if capacity_kwp <= 0.0:
        return min(rate for _, rate in tiers)
    remaining = capacity_kwp
    previous_limit = 0.0
    weighted = 0.0
    for idx, (limit, rate) in enumerate(tiers):
        upper = limit if limit > previous_limit else capacity_kwp
        width = max(0.0, min(capacity_kwp, upper) - previous_limit)
        if width > 0.0:
            weighted += width * rate
            remaining -= width
        previous_limit = max(previous_limit, upper)
        if remaining <= 0.0001:
            break
        if idx == len(tiers) - 1 and remaining > 0.0:
            weighted += remaining * rate
            remaining = 0.0
    used = max(0.0, capacity_kwp - remaining)
    if used <= 0.0:
        return None
    return weighted / used


def _pv_store_threshold_state(config):
    manual = _configured_optional_float(config, "direct_marketing_pv_store_threshold_ct")
    if manual is not None:
        return {
            "value": round(manual, 3),
            "source": "manual",
            "capacity_kwp": None,
        }
    if cfg_bool((config or {}).get("direct_marketing_eeg_enable"), False):
        tiers = _parse_eeg_tariff_tiers((config or {}).get("direct_marketing_eeg_tariff_tiers"))
        if tiers:
            capacity_kwp = _configured_pv_capacity_kwp(config)
            weighted = _weighted_eeg_rate_ct(tiers, capacity_kwp)
            if weighted is not None:
                return {
                    "value": round(weighted, 3),
                    "source": "eeg_weighted" if capacity_kwp > 0.0 else "eeg_lowest_tier",
                    "capacity_kwp": round(capacity_kwp, 3) if capacity_kwp > 0.0 else None,
                }
    return {
        "value": None,
        "source": "score",
        "capacity_kwp": None,
    }


def _slot_ts(slot):
    return safe_float(slot.get("ts", slot.get("start_timestamp", 0)), 0.0)


def _slot_end_ts(slot):
    return safe_float(slot.get("end_timestamp"), _slot_ts(slot) + SLOT_MS)


def _slot_has_market_price(slot):
    if slot.get("price_available") is False and not cfg_bool(slot.get("direct_marketing_price_available"), False):
        return False
    for key in (
        "direct_marketing_market_price_ct",
        "direct_marketing_market_ct",
        "direct_marketing_marketprice",
        "direct_marketing_market_price_eur_mwh",
        "marketprice",
        "market_price",
        "market_price_eur_mwh",
        "market_price_ct",
        "market_ct",
    ):
        raw = slot.get(key)
        if raw is not None and str(raw).strip() != "":
            return True
    return False


def _slot_has_direct_market_price(slot):
    for key in (
        "direct_marketing_market_price_ct",
        "direct_marketing_market_ct",
        "direct_marketing_marketprice",
        "direct_marketing_market_price_eur_mwh",
    ):
        raw = slot.get(key)
        if raw is not None and str(raw).strip() != "":
            return True
    return False


def _active_settlement_basis(config):
    basis = str(
        (config or {}).get("direct_marketing_settlement_basis", "day_ahead_15min")
        or "day_ahead_15min"
    ).strip().lower().replace("-", "_")
    return basis == "day_ahead_15min"


def _slot_requires_direct_market_price(slot, config):
    if not _active_settlement_basis(config):
        return False
    retail_sources = {"tibber", "octopus", "octopus_energy", "retail", "end_customer"}
    sources = {
        str((slot or {}).get("price_source") or "").strip().lower(),
        str((slot or {}).get("tariff_provider") or "").strip().lower(),
        str((config or {}).get("tariff_provider") or "").strip().lower(),
    }
    return bool(sources.intersection(retail_sources))


def _slot_price_quality_blocker(slot, config):
    if not _active_settlement_basis(config):
        return "unsupported_settlement_basis"
    if _slot_requires_direct_market_price(slot, config) and not _slot_has_direct_market_price(slot):
        return "direct_market_price_missing"
    if not _slot_has_market_price(slot):
        return "market_price_missing"
    for key in ("price_stale", "market_price_stale", "price_data_stale", "stale_price"):
        if cfg_bool(slot.get(key), False):
            return "stale_market_price"
    max_age_s = safe_float((config or {}).get("direct_marketing_price_max_age_s"), 0.0)
    if max_age_s > 0.0:
        for key in ("price_age_s", "market_price_age_s", "price_data_age_s"):
            raw = slot.get(key)
            if raw is not None and str(raw).strip() != "" and safe_float(raw, 0.0) > max_age_s:
                return "stale_market_price"
    resolution_raw = slot.get("direct_marketing_price_resolution_min")
    if resolution_raw is None or str(resolution_raw).strip() == "":
        resolution_raw = slot.get("price_resolution_min")
    if (
        _active_settlement_basis(config)
        and resolution_raw is not None
        and str(resolution_raw).strip() != ""
        and safe_float(resolution_raw, 15.0) > 15.0
    ):
        return "unsupported_price_resolution"
    return ""


def _market_ct(slot):
    for key in ("direct_marketing_market_price_ct", "direct_marketing_market_ct", "market_price_ct", "market_ct"):
        raw = slot.get(key)
        if raw is not None and str(raw).strip() != "":
            return safe_float(raw, 0.0)
    for key in (
        "direct_marketing_marketprice",
        "direct_marketing_market_price_eur_mwh",
        "marketprice",
        "market_price",
        "market_price_eur_mwh",
    ):
        raw = slot.get(key)
        if raw is not None and str(raw).strip() != "":
            return safe_float(raw, 0.0) / 10.0
    return 0.0


def _market_price_source(slot):
    for key in ("direct_marketing_price_source", "market_price_source", "price_source", "tariff_provider"):
        raw = slot.get(key)
        if raw is not None and str(raw).strip() != "":
            return str(raw)
    return ""


def _market_price_resolution_min(slot):
    for key in ("direct_marketing_price_resolution_min", "price_resolution_min", "source_resolution_min"):
        raw = slot.get(key)
        if raw is not None and str(raw).strip() != "":
            return safe_float(raw, 0.0)
    return None


def _billing_ct(slot, market_ct=None):
    if market_ct is None:
        market_ct = _market_ct(slot)
    billing = slot.get("billing_price_ct")
    if billing is None:
        billing = slot.get("billing_price")
    return safe_float(billing, market_ct)


def _slot_power(slot, keys):
    for key in keys:
        if key in slot and slot.get(key) is not None and str(slot.get(key)).strip() != "":
            return max(0.0, safe_float(slot.get(key), 0.0))
    return 0.0


def _slot_power_present(slot, keys):
    for key in keys:
        if key in slot and slot.get(key) is not None and str(slot.get(key)).strip() != "":
            return True
    return False


def _slot_forecast_power(slot):
    pv_w = _slot_power(slot, ("pv_w", "pv", "PV_Power"))
    home_w = _slot_power(slot, ("home_w", "home", "Home_Power", "load_w"))
    wp_w = _slot_power(slot, ("wp_w", "heatpump_w", "WP_Power"))
    heater_w = _slot_power(slot, ("heater_w", "heizstab_w", "Heizstab_Power"))
    wallbox_w = _slot_power(slot, ("wallbox_w", "Wallbox_Power", "wb_w"))
    has_power = bool(
        _slot_power_present(slot, ("pv_w", "pv", "PV_Power", "surplus_w", "surplus"))
        or _slot_power_present(slot, ("home_w", "home", "Home_Power", "load_w"))
        or _slot_power_present(slot, ("wp_w", "heatpump_w", "WP_Power"))
        or _slot_power_present(slot, ("heater_w", "heizstab_w", "Heizstab_Power"))
        or _slot_power_present(slot, ("wallbox_w", "Wallbox_Power", "wb_w"))
    )
    if "surplus_w" in slot or "surplus" in slot:
        surplus_w = safe_float(slot.get("surplus_w", slot.get("surplus")), pv_w - home_w - wp_w - heater_w - wallbox_w)
    else:
        surplus_w = pv_w - home_w - wp_w - heater_w - wallbox_w
    return {
        "pv_w": pv_w,
        "home_w": home_w,
        "wp_w": wp_w,
        "heater_w": heater_w,
        "wallbox_w": wallbox_w,
        "forecast_surplus_w": max(0.0, surplus_w),
        "forecast_deficit_w": max(0.0, -surplus_w),
        "has_power_forecast": has_power,
    }


def _price_score(slot, min_market_ct, max_market_ct):
    if not _slot_has_direct_market_price(slot):
        for key in ("optimization_score", "pure_eco_score", "eco_score"):
            raw = slot.get(key)
            if raw is not None and str(raw).strip() != "":
                return _clamp(safe_float(raw, 50.0), 0.0, 100.0)

    span = max_market_ct - min_market_ct
    if span <= 0.001:
        return 50.0
    # Existing EcoScore convention: high score means cheap/net-friendly.
    return _clamp(100.0 - (((_market_ct(slot) - min_market_ct) / span) * 100.0), 0.0, 100.0)


def _net_sell_ct(market_ct, config):
    revenue_offset = safe_float(config.get("direct_marketing_revenue_offset_ct"), 0.0)
    fee_ct = safe_float(config.get("direct_marketing_fee_ct_per_kwh"), 0.0)
    fee_pct = safe_float(config.get("direct_marketing_fee_pct"), 0.0)
    gross = market_ct + revenue_offset
    variable_fee = max(0.0, gross) * max(0.0, fee_pct) / 100.0
    return gross - max(0.0, fee_ct) - variable_fee


def _format_t(ts):
    try:
        return datetime.fromtimestamp(float(ts) / 1000.0).strftime("%H:%M")
    except Exception:
        return ""


def _policy_empty_decision(block_reason="", reserve=None, flags=None):
    reserve = reserve or {}
    flags = flags or {}
    protected_wh = max(0.0, safe_float(reserve.get("protected_energy_wh"), 0.0))
    sellable_wh = max(0.0, safe_float(reserve.get("sellable_wh"), reserve.get("available_export_wh", 0.0)))
    return {
        "schema": POLICY_SCHEMA,
        "profit_profile": _normalize_profit_profile(flags.get("profit_profile", "standard")),
        "commands_allowed": False,
        "dv_target_state": "NORMAL",
        "storage_budget": {
            "export_budget_w": 0,
            "charge_budget_w": 0,
            "protected_reserve_wh": round(protected_wh, 0),
            "sellable_wh": round(sellable_wh, 0),
        },
        "consumer_budget": {
            "wallbox_w": 0,
            "heatpump_w": 0,
            "heater_w": 0,
            "reason": "no_negative_price_budget",
        },
        "export_constraint": {
            "class": "none",
            "hard": False,
            "limit_w": None,
            "scope": "storage_only",
            "pv_export_allowed": True,
            "enforcement": "none",
            "execution_owner": "none",
        },
        "block_reason": str(block_reason or "normal"),
        "blocked": bool(block_reason),
    }


def _slot_export_constraint(slot, flags):
    """Classify PV export independently from storage charge priority."""

    is_negative = bool((slot or {}).get("is_negative"))
    negative_hard = bool(
        is_negative
        and (
            cfg_bool((flags or {}).get("negative_price_no_export"), True)
            or cfg_bool((flags or {}).get("low_price_curtail_enable"), False)
        )
    )
    if negative_hard:
        limit_w = (
            max(0, int(round(safe_float((flags or {}).get("low_price_curtail_limit_w"), 0.0))))
            if cfg_bool((flags or {}).get("low_price_curtail_enable"), False)
            else 0
        )
        return {
            "export_constraint_class": "negative_hard",
            "hard_export_limit_active": True,
            "hard_export_limit_w": limit_w,
            "export_constraint_scope": "grid_connection",
            "pv_export_allowed": False,
            "export_constraint_enforcement": "requested",
            "export_constraint_execution_owner": "external_e3dc_luox",
        }

    if is_negative:
        classification = "negative_allowed"
    elif bool((slot or {}).get("is_threshold_soft")):
        classification = "eeg_soft"
    else:
        classification = "low_price_soft"
    return {
        "export_constraint_class": classification,
        "hard_export_limit_active": False,
        "hard_export_limit_w": None,
        "export_constraint_scope": "storage_priority",
        "pv_export_allowed": True,
        "export_constraint_enforcement": "storage_priority",
        "export_constraint_execution_owner": "storage_manager",
    }


def _policy_consumer_budget(config, reason):
    return {
        "wallbox_w": int(max(0.0, safe_float(config.get("direct_marketing_negative_price_wallbox_budget_w"), 0.0))),
        "heatpump_w": int(max(0.0, safe_float(config.get("direct_marketing_negative_price_heatpump_budget_w"), 0.0))),
        "heater_w": int(max(0.0, safe_float(config.get("direct_marketing_negative_price_heater_budget_w"), 0.0))),
        "reason": reason,
    }


def _policy_lcos_ct(config, profile):
    explicit = _configured_optional_float(config, "direct_marketing_lcos_ct_per_kwh")
    if explicit is None:
        explicit = _configured_float(config, "direct_marketing_degradation_ct_per_kwh", 4.0)
    explicit = max(0.0, safe_float(explicit, 0.0))
    if profile == "aggressive":
        factor = _clamp(
            safe_float(config.get("direct_marketing_aggressive_lcos_factor"), 0.0),
            0.0,
            1.0,
        )
        return explicit * factor
    if profile == "expert":
        return 0.0
    return explicit


def _policy_thresholds(config):
    return {
        "min_window_profit_eur": max(0.0, safe_float(config.get("direct_marketing_min_window_profit_eur"), 0.10)),
        "min_export_energy_kwh": max(0.0, safe_float(config.get("direct_marketing_min_export_energy_kwh"), 1.0)),
        "min_export_window_min": max(0.0, safe_float(config.get("direct_marketing_min_export_window_min"), 60.0)),
        "min_margin_pct": max(0.0, safe_float(config.get("direct_marketing_min_margin_pct"), 10.0)),
        "user_min_margin_ct": max(
            0.0,
            safe_float(
                config.get(
                    "direct_marketing_user_min_margin_ct_per_kwh",
                    config.get("direct_marketing_min_profit_ct_per_kwh", 0.0),
                ),
                0.0,
            ),
        ),
    }


def _policy_current_slot(annotated, now_ms):
    now_ms = safe_float(now_ms, 0.0)
    for slot in sorted(annotated or [], key=lambda item: safe_float(item.get("ts"), 0.0)):
        start_ts = safe_float(slot.get("ts"), 0.0)
        end_ts = safe_float(slot.get("end_ts"), start_ts + SLOT_MS)
        if start_ts <= now_ms < end_ts:
            return slot
    return None


def _policy_current_window(windows, now_ms, actions=None):
    now_ms = safe_float(now_ms, 0.0)
    actions = set(actions or [])
    for window in sorted(windows or [], key=lambda item: safe_float(item.get("start_ts"), 0.0)):
        if actions and window.get("action") not in actions:
            continue
        start_ts = safe_float(window.get("start_ts"), 0.0)
        end_ts = safe_float(window.get("end_ts"), start_ts + SLOT_MS)
        if start_ts <= now_ms < end_ts:
            return window
    return None


def _policy_next_recharge_ts(annotated, now_ms):
    for slot in sorted(annotated or [], key=lambda item: safe_float(item.get("ts"), 0.0)):
        if safe_float(slot.get("end_ts"), safe_float(slot.get("ts"), 0.0) + SLOT_MS) <= now_ms:
            continue
        if slot.get("is_pv_store") and safe_float(slot.get("forecast_surplus_w"), 0.0) > 0.0:
            return safe_float(slot.get("ts"), 0.0)
    return None


def _policy_house_need_until_next_recharge_wh(config, annotated, now_ms):
    explicit = _configured_optional_float(config, "direct_marketing_forecast_house_need_until_next_recharge_wh")
    if explicit is not None:
        return max(0.0, explicit), True, "configured"
    next_recharge_ts = _policy_next_recharge_ts(annotated, now_ms)
    if next_recharge_ts is None:
        lookahead_min = max(15.0, safe_float(config.get("direct_marketing_house_need_lookahead_min"), 360.0))
        next_recharge_ts = now_ms + lookahead_min * 60.0 * 1000.0
    need_wh, used_forecast = _forecast_deficit_wh(annotated or [], now_ms, next_recharge_ts, enabled=True)
    return max(0.0, need_wh), bool(used_forecast), "forecast_deficit"


def _policy_reserve_state(config, reserve, annotated, now_ms, current_soc, capacity_wh):
    capacity_wh = max(0.0, safe_float(capacity_wh, 0.0))
    current_soc = _clamp(safe_float(current_soc, 0.0), 0.0, 100.0)
    current_storage_wh = max(0.0, (current_soc / 100.0) * capacity_wh)

    ep_reserve_wh = _configured_optional_float(config, "direct_marketing_ep_reserve_wh")
    if ep_reserve_wh is None:
        ep_reserve_wh = (_clamp(safe_float(reserve.get("ep_reserve_soc_pct"), 0.0), 0.0, 100.0) / 100.0) * capacity_wh
    configured_home_reserve_wh = _configured_optional_float(config, "direct_marketing_home_reserve_wh")
    if configured_home_reserve_wh is None:
        configured_home_reserve_wh = (
            _clamp(safe_float(reserve.get("home_reserve_soc_pct"), 0.0), 0.0, 100.0) / 100.0
        ) * capacity_wh
    night_reserve_wh = _configured_optional_float(config, "direct_marketing_night_reserve_wh")
    if night_reserve_wh is None:
        night_reserve_wh = (
            _clamp(safe_float(reserve.get("night_reserve_soc_pct"), 0.0), 0.0, 100.0) / 100.0
        ) * capacity_wh
    house_need_wh, house_need_forecast_used, house_need_source = _policy_house_need_until_next_recharge_wh(
        config,
        annotated,
        now_ms,
    )
    ep_plus_house_need_wh = max(0.0, ep_reserve_wh) + max(0.0, house_need_wh)
    protected_wh = max(
        0.0,
        ep_plus_house_need_wh,
        configured_home_reserve_wh,
        night_reserve_wh,
    )
    sellable_wh = max(0.0, current_storage_wh - protected_wh)
    return {
        "current_storage_wh": round(current_storage_wh, 0),
        "ep_reserve_wh": round(max(0.0, ep_reserve_wh), 0),
        "configured_home_reserve_wh": round(max(0.0, configured_home_reserve_wh), 0),
        "night_reserve_wh": round(max(0.0, night_reserve_wh), 0),
        "forecast_house_need_until_next_recharge_wh": round(house_need_wh, 0),
        "ep_plus_forecast_house_need_wh": round(ep_plus_house_need_wh, 0),
        "forecast_house_need_used": bool(house_need_forecast_used),
        "forecast_house_need_source": house_need_source,
        "protected_energy_wh": round(protected_wh, 0),
        "sellable_wh": round(sellable_wh, 0),
    }


def _policy_export_economics(config, window, economics, profile, sellable_wh):
    duration_h = _entry_duration_h(window)
    duration_min = duration_h * 60.0
    max_power_w = max(0.0, safe_float(window.get("max_power_w"), 0.0))
    planned_wh = min(max(0.0, sellable_wh), max_power_w * duration_h)
    export_energy_kwh = max(0.0, planned_wh / 1000.0)
    sell_net_ct = safe_float(
        window.get("avg_net_sell_ct"),
        safe_float(window.get("net_sell_ct"), safe_float(window.get("avg_market_ct"), 0.0)),
    )
    opportunity_cost_ct = _configured_optional_float(config, "direct_marketing_opportunity_cost_ct")
    if opportunity_cost_ct is None:
        opportunity_cost_ct = safe_float(economics.get("pv_shift_opportunity_ct"), 0.0)
    efficiency_pct = _clamp(
        safe_float(config.get("direct_marketing_roundtrip_efficiency_pct"), 85.0),
        1.0,
        100.0,
    )
    efficiency = efficiency_pct / 100.0
    efficiency_loss_ct = max(0.0, sell_net_ct * (1.0 - efficiency))
    lcos_ct = _policy_lcos_ct(config, profile)
    safety_margin_ct = _clamp(
        safe_float(config.get("direct_marketing_safety_margin_ct_per_kwh"), 0.0),
        -10.0,
        50.0,
    )
    thresholds = _policy_thresholds(config)
    margin_ct = sell_net_ct - opportunity_cost_ct - efficiency_loss_ct - lcos_ct - safety_margin_ct
    cost_basis_ct = max(
        1.0,
        abs(opportunity_cost_ct) + efficiency_loss_ct + lcos_ct + max(0.0, safety_margin_ct),
    )
    margin_pct = margin_ct / cost_basis_ct * 100.0
    expected_profit_eur = export_energy_kwh * margin_ct / 100.0
    return {
        "export_energy_kwh": round(export_energy_kwh, 3),
        "duration_min": round(duration_min, 1),
        "sell_net_ct_kwh": round(sell_net_ct, 3),
        "opportunity_cost_ct": round(opportunity_cost_ct, 3),
        "efficiency_loss_ct": round(efficiency_loss_ct, 3),
        "lcos_ct": round(lcos_ct, 3),
        "safety_margin_ct": round(safety_margin_ct, 3),
        "margin_ct_kwh": round(margin_ct, 3),
        "cost_basis_ct_kwh": round(cost_basis_ct, 3),
        "margin_pct": round(margin_pct, 1),
        "expected_profit_eur": round(expected_profit_eur, 4),
        "roundtrip_efficiency_pct": round(efficiency_pct, 1),
        **thresholds,
    }


def _policy_export_allowed(profile, export_economics):
    if safe_float(export_economics.get("export_energy_kwh"), 0.0) <= 0.0:
        return False, "Blocked by Reserve: sellable energy is 0 kWh"
    if profile == "expert":
        return True, "Expert: margin check bypassed, hard reserve kept"
    margin = safe_float(export_economics.get("margin_ct_kwh"), 0.0)
    profit = safe_float(export_economics.get("expected_profit_eur"), 0.0)
    if profile == "aggressive":
        if profit > 0.0 and margin > 0.0:
            return True, "Aggressive: positive net profit after efficiency"
        return False, "Blocked by Margin: expected profit %.4f EUR <= 0 EUR" % profit
    if margin < safe_float(export_economics.get("user_min_margin_ct"), 0.0):
        return False, "Blocked by Margin: %.2f ct/kWh below user minimum" % margin
    if safe_float(export_economics.get("margin_pct"), 0.0) + 0.000001 < safe_float(
        export_economics.get("min_margin_pct"),
        10.0,
    ):
        return False, "Blocked by Margin: %.1f%% < Threshold %.1f%%" % (
            safe_float(export_economics.get("margin_pct"), 0.0),
            safe_float(export_economics.get("min_margin_pct"), 10.0),
        )
    if profit + 0.000001 < safe_float(export_economics.get("min_window_profit_eur"), 0.10):
        return False, "Blocked by Margin: Expected Profit %.2f EUR < Threshold %.2f EUR" % (
            profit,
            safe_float(export_economics.get("min_window_profit_eur"), 0.10),
        )
    if safe_float(export_economics.get("export_energy_kwh"), 0.0) + 0.000001 < safe_float(
        export_economics.get("min_export_energy_kwh"),
        1.0,
    ):
        return False, "Blocked by Energy: %.2f kWh < Threshold %.2f kWh" % (
            safe_float(export_economics.get("export_energy_kwh"), 0.0),
            safe_float(export_economics.get("min_export_energy_kwh"), 1.0),
        )
    if safe_float(export_economics.get("duration_min"), 0.0) + 0.000001 < safe_float(
        export_economics.get("min_export_window_min"),
        60.0,
    ):
        return False, "Blocked by Duration: %.1f min < Threshold %.1f min" % (
            safe_float(export_economics.get("duration_min"), 0.0),
            safe_float(export_economics.get("min_export_window_min"), 60.0),
        )
    return True, "Profit export allowed"


def _build_policy_decision(config, annotated, windows, reserve, flags, economics, mode, now_ms, current_soc, capacity_wh, blocked_reasons):
    profile = _normalize_profit_profile(config.get("direct_marketing_profit_profile", "standard"))
    reserve_policy = _policy_reserve_state(config, reserve, annotated, now_ms, current_soc, capacity_wh)
    reserve = dict(reserve or {})
    reserve.update(reserve_policy)
    decision = _policy_empty_decision("", reserve, {**(flags or {}), "profit_profile": profile})
    decision["reserve_components"] = {
        key: reserve_policy[key]
        for key in (
            "current_storage_wh",
            "ep_reserve_wh",
            "configured_home_reserve_wh",
            "night_reserve_wh",
            "forecast_house_need_until_next_recharge_wh",
            "ep_plus_forecast_house_need_wh",
            "forecast_house_need_used",
            "forecast_house_need_source",
        )
    }

    quality_blocks = [reason for reason in (blocked_reasons or []) if reason in {
        "direct_market_price_missing",
        "market_price_missing",
        "stale_market_price",
        "unsupported_price_resolution",
        "unsupported_settlement_basis",
        "no_timeline",
        "disabled",
    }]
    if quality_blocks:
        decision["block_reason"] = "price_quality_blocked:%s" % ",".join(sorted(set(quality_blocks)))
        decision["blocked"] = True
        return decision

    if not cfg_bool(flags.get("live_soc_valid"), True):
        decision["block_reason"] = "live_values_missing:current_soc"
        decision["blocked"] = True
        return decision

    if safe_float(capacity_wh, 0.0) <= 0.0:
        decision["block_reason"] = "live_values_missing:capacity_wh"
        decision["blocked"] = True
        return decision

    if mode == "safe" or not cfg_bool(flags.get("commands_allowed"), False):
        decision["dv_target_state"] = "HOLD"
        decision["block_reason"] = "Safe: observe only" if mode == "safe" else "Hold: strategy has no active command release"
        decision["blocked"] = False
        return decision

    current_slot = _policy_current_slot(annotated, now_ms)
    if current_slot and current_slot.get("is_negative") and cfg_bool(flags.get("negative_price_no_export"), True):
        negative_window = _policy_current_window(windows, now_ms, {"eco_plus_store_pv_candidate"})
        export_constraint = _slot_export_constraint(current_slot, flags)
        decision["export_constraint"] = {
            "class": export_constraint["export_constraint_class"],
            "hard": export_constraint["hard_export_limit_active"],
            "limit_w": export_constraint["hard_export_limit_w"],
            "scope": export_constraint["export_constraint_scope"],
            "pv_export_allowed": export_constraint["pv_export_allowed"],
            "enforcement": export_constraint["export_constraint_enforcement"],
            "execution_owner": export_constraint["export_constraint_execution_owner"],
        }
        if mode not in {"eco", "eco_plus"} or not cfg_bool(flags.get("pv_store_enable"), False):
            decision["dv_target_state"] = "HOLD"
            decision["block_reason"] = "Negative price observed, but PV storage control is not released"
            decision["blocked"] = False
            return decision
        charge_headroom_wh = max(
            0.0,
            safe_float(capacity_wh, 0.0) - safe_float(reserve_policy.get("current_storage_wh"), 0.0),
        )
        if charge_headroom_wh <= 100.0:
            decision["dv_target_state"] = "HOLD"
            decision["block_reason"] = "Negative price hard: storage charge headroom exhausted"
            decision["blocked"] = False
            return decision
        charge_w = max(0.0, safe_float(flags.get("pv_store_max_w"), 0.0))
        if charge_w <= 0.0:
            charge_w = max(0.0, safe_float(config.get("maximumladeleistung"), 0.0))
        if current_slot.get("has_power_forecast"):
            surplus_w = max(0.0, safe_float(current_slot.get("forecast_surplus_w"), 0.0))
            charge_w = min(charge_w, surplus_w) if charge_w > 0.0 and surplus_w > 0.0 else charge_w
        decision.update({
            "commands_allowed": True,
            "dv_target_state": "FORCE_CHARGE_PV",
            "storage_budget": {
                "export_budget_w": 0,
                "charge_budget_w": int(round(max(0.0, charge_w))),
                "protected_reserve_wh": reserve_policy["protected_energy_wh"],
                "sellable_wh": reserve_policy["sellable_wh"],
            },
            "consumer_budget": _policy_consumer_budget(config, "negative_price_budget"),
            "export_constraint": {
                "class": export_constraint["export_constraint_class"],
                "hard": export_constraint["hard_export_limit_active"],
                "limit_w": export_constraint["hard_export_limit_w"],
                "scope": export_constraint["export_constraint_scope"],
                "pv_export_allowed": export_constraint["pv_export_allowed"],
                "enforcement": export_constraint["export_constraint_enforcement"],
                "execution_owner": export_constraint["export_constraint_execution_owner"],
            },
            "block_reason": "Negative price hard: zero-export requested, PV charge prioritized",
            "blocked": False,
            "selected_window": {
                "action": (negative_window or {}).get("action", "eco_plus_store_pv_candidate"),
                "reason": "negative_price",
                "start_ts": (negative_window or {}).get("start_ts", current_slot.get("ts")),
                "end_ts": (negative_window or {}).get("end_ts", current_slot.get("end_ts")),
                **export_constraint,
            },
        })
        return decision

    headroom_window = _policy_current_window(windows, now_ms, {"eco_plus_negative_headroom_hold"})
    if headroom_window:
        required_headroom_pct = _clamp(
            safe_float(
                headroom_window.get("negative_headroom_required_pct"),
                100.0 - safe_float(headroom_window.get("soc_ceiling_pct"), 100.0),
            ),
            0.0,
            100.0,
        )
        protected_soc_pct = _clamp(
            (safe_float(reserve_policy.get("protected_energy_wh"), 0.0) / max(1.0, safe_float(capacity_wh, 0.0))) * 100.0,
            0.0,
            100.0,
        )
        headroom_target_soc_pct = max(protected_soc_pct, 100.0 - required_headroom_pct)
        headroom_deficit_wh = min(
            safe_float(reserve_policy.get("sellable_wh"), 0.0),
            max(0.0, (safe_float(current_soc, 0.0) - headroom_target_soc_pct) / 100.0 * safe_float(capacity_wh, 0.0)),
        )
        remaining_h = max(
            0.001,
            (safe_float(headroom_window.get("end_ts"), now_ms + SLOT_MS) - safe_float(now_ms, 0.0)) / 3600000.0,
        )
        planned_headroom_w = max(0.0, safe_float(headroom_window.get("max_power_w"), 0.0))
        export_w = int(round(min(
            max(0.0, safe_float(flags.get("max_export_w"), 0.0)),
            planned_headroom_w,
            headroom_deficit_wh / remaining_h,
        )))
        headroom_control_released = bool(
            mode in {"eco", "eco_plus"}
            and cfg_bool(flags.get("pv_store_enable"), False)
            and cfg_bool(flags.get("negative_headroom_enable"), False)
        )
        active_headroom_export = bool(
            headroom_control_released
            and cfg_bool(flags.get("export_enable"), False)
            and export_w >= 300
        )
        decision.update({
            "commands_allowed": headroom_control_released,
            "dv_target_state": "HEADROOM_EXPORT" if headroom_control_released else "HOLD",
            "storage_budget": {
                "export_budget_w": max(0, export_w) if active_headroom_export else 0,
                "charge_budget_w": 0,
                "protected_reserve_wh": reserve_policy["protected_energy_wh"],
                "sellable_wh": reserve_policy["sellable_wh"],
                "headroom_deficit_wh": round(headroom_deficit_wh, 0),
                "headroom_target_soc_pct": round(headroom_target_soc_pct, 1),
                "headroom_hold_active": bool(headroom_control_released and not active_headroom_export),
            },
            "block_reason": (
                "Headroom export: highest-value slot before future low/negative price absorption"
                if active_headroom_export
                else (
                    "Headroom hold: charging blocked until selected export/absorption window"
                    if headroom_control_released
                    else "Headroom hold: control is not released"
                )
            ),
            "blocked": False,
            "economics": {
                "economic_basis": "headroom_value",
                "current_net_sell_ct_kwh": round(
                    safe_float(
                        headroom_window.get("avg_net_sell_ct"),
                        safe_float(headroom_window.get("net_sell_ct"), safe_float(headroom_window.get("avg_market_ct"), 0.0)),
                    ),
                    3,
                ),
                "future_low_market_ct_kwh": economics.get("best_low_market_ct") if isinstance(economics, dict) else None,
                "headroom_export_energy_kwh": round(min(headroom_deficit_wh, export_w * remaining_h) / 1000.0, 3),
                "forecast_absorption_wh": round(safe_float(headroom_window.get("negative_headroom_forecast_surplus_wh"), 0.0), 0),
            },
            "selected_window": {
                "action": headroom_window.get("action"),
                "reason": headroom_window.get("reason"),
                "start_ts": headroom_window.get("start_ts"),
                "end_ts": headroom_window.get("end_ts"),
                "next_charge_window_start_ts": headroom_window.get("negative_headroom_next_start_ts"),
                "headroom_export_selected": bool(headroom_window.get("headroom_export_selected")),
                "headroom_export_budget_wh": headroom_window.get("headroom_export_budget_wh"),
            },
        })
        return decision

    pv_store_window = _policy_current_window(windows, now_ms, {"eco_plus_store_pv_candidate"})
    if pv_store_window:
        constraint_class = str(pv_store_window.get("export_constraint_class") or "low_price_soft")
        hard_export_limit = bool(pv_store_window.get("hard_export_limit_active"))
        pv_export_allowed = bool(pv_store_window.get("pv_export_allowed", not hard_export_limit))
        charge_w = max(0.0, safe_float(pv_store_window.get("max_power_w"), safe_float(flags.get("pv_store_max_w"), 0.0)))
        if charge_w <= 0.0:
            charge_w = max(0.0, safe_float(config.get("maximumladeleistung"), 0.0))
        charge_headroom_wh = max(
            0.0,
            safe_float(capacity_wh, 0.0) - safe_float(reserve_policy.get("current_storage_wh"), 0.0),
        )
        active_pv_store = bool(
            mode in {"eco", "eco_plus"}
            and cfg_bool(flags.get("pv_store_enable"), False)
            and charge_w >= 300.0
            and charge_headroom_wh > 100.0
        )
        decision.update({
            "commands_allowed": active_pv_store,
            "dv_target_state": "FORCE_CHARGE_PV" if active_pv_store else "HOLD",
            "storage_budget": {
                "export_budget_w": 0,
                "charge_budget_w": int(round(max(0.0, charge_w))) if active_pv_store else 0,
                "protected_reserve_wh": reserve_policy["protected_energy_wh"],
                "sellable_wh": reserve_policy["sellable_wh"],
            },
            "export_constraint": {
                "class": constraint_class,
                "hard": hard_export_limit,
                "limit_w": pv_store_window.get("hard_export_limit_w") if hard_export_limit else None,
                "scope": str(pv_store_window.get("export_constraint_scope") or "storage_priority"),
                "pv_export_allowed": pv_export_allowed,
                "enforcement": str(
                    pv_store_window.get("export_constraint_enforcement")
                    or ("requested" if hard_export_limit else "storage_priority")
                ),
                "execution_owner": str(
                    pv_store_window.get("export_constraint_execution_owner")
                    or ("external_e3dc_luox" if hard_export_limit else "storage_manager")
                ),
            },
            "block_reason": (
                "PV store hold: charge control is not released or storage is full"
                if not active_pv_store
                else (
                    "EEG soft: PV charge prioritized, positive PV export allowed"
                    if constraint_class == "eeg_soft"
                    else "PV store: low price window, charge prioritized"
                )
            ),
            "blocked": False,
            "selected_window": {
                "action": pv_store_window.get("action"),
                "reason": pv_store_window.get("reason"),
                "start_ts": pv_store_window.get("start_ts"),
                "end_ts": pv_store_window.get("end_ts"),
                "export_constraint_class": constraint_class,
                "hard_export_limit_active": hard_export_limit,
                "hard_export_limit_w": pv_store_window.get("hard_export_limit_w") if hard_export_limit else None,
                "export_constraint_scope": str(pv_store_window.get("export_constraint_scope") or "storage_priority"),
                "pv_export_allowed": pv_export_allowed,
            },
        })
        return decision


    export_window = _policy_current_window(
        windows,
        now_ms,
        {"eco_plus_export_candidate", "arbitrage_export_candidate"},
    )
    if export_window:
        export_economics = _policy_export_economics(
            config,
            export_window,
            economics or {},
            profile,
            reserve_policy["sellable_wh"],
        )
        allowed, reason = _policy_export_allowed(profile, export_economics)
        allowed = bool(
            allowed
            and mode in {"eco_plus", "arbitrage"}
            and cfg_bool(flags.get("export_enable"), False)
        )
        export_w = 0
        if allowed:
            duration_h = max(0.001, _entry_duration_h(export_window))
            export_w = int(round(min(
                max(0.0, safe_float(export_window.get("max_power_w"), safe_float(flags.get("max_export_w"), 0.0))),
                (safe_float(export_economics.get("export_energy_kwh"), 0.0) * 1000.0) / duration_h,
            )))
        decision.update({
            "commands_allowed": allowed,
            "dv_target_state": "FORCE_EXPORT" if allowed else "HOLD",
            "storage_budget": {
                "export_budget_w": max(0, export_w),
                "charge_budget_w": 0,
                "protected_reserve_wh": reserve_policy["protected_energy_wh"],
                "sellable_wh": reserve_policy["sellable_wh"],
            },
            "block_reason": reason,
            "blocked": not allowed,
            "economics": export_economics,
            "selected_window": {
                "action": export_window.get("action"),
                "reason": export_window.get("reason"),
                "start_ts": export_window.get("start_ts"),
                "end_ts": export_window.get("end_ts"),
            },
        })
        return decision

    hold_window = _policy_current_window(windows, now_ms, {"eco_house_supply", "eco_plus_house_supply", "safe_house_supply"})
    if hold_window:
        decision.update({
            "dv_target_state": "HOLD",
            "block_reason": "Hold: Mid-Price Zone",
            "blocked": False,
            "selected_window": {
                "action": hold_window.get("action"),
                "reason": hold_window.get("reason"),
                "start_ts": hold_window.get("start_ts"),
                "end_ts": hold_window.get("end_ts"),
            },
        })
        return decision

    if not windows:
        decision["block_reason"] = "Hold: no policy candidate window"
        decision["blocked"] = True
    else:
        decision.update({
            "dv_target_state": "HOLD",
            "block_reason": "Hold: future opportunity value exceeds current window",
            "blocked": False,
        })
    return decision


def _policy_target_soc_at(target_timeline, target_ts):
    points = []
    for point in target_timeline or []:
        if not isinstance(point, dict):
            continue
        ts = safe_float(point.get("ts"), 0.0)
        soc = safe_float(point.get("soc"), -1.0)
        if ts <= 0.0 or soc < 0.0:
            continue
        points.append((ts, _clamp(soc, 0.0, 100.0)))
    points.sort(key=lambda item: item[0])
    if not points:
        return None
    target_ts = safe_float(target_ts, 0.0)
    if target_ts < points[0][0] or target_ts > points[-1][0] + SLOT_MS:
        return None
    if target_ts >= points[-1][0]:
        return points[-1][1]
    for (left_ts, left_soc), (right_ts, right_soc) in zip(points, points[1:]):
        if not left_ts <= target_ts <= right_ts:
            continue
        span = max(1.0, right_ts - left_ts)
        fraction = _clamp((target_ts - left_ts) / span, 0.0, 1.0)
        return left_soc + (right_soc - left_soc) * fraction
    return None


def _policy_projected_soc_for_window(window, target_timeline, current_soc, reserve, capacity_wh, now_ms):
    start_ts = safe_float((window or {}).get("start_ts"), now_ms)
    end_ts = safe_float((window or {}).get("end_ts"), start_ts + SLOT_MS)
    if start_ts <= safe_float(now_ms, 0.0) < end_ts:
        return _clamp(safe_float(current_soc, 0.0), 0.0, 100.0), "live"

    projected_soc = _policy_target_soc_at(target_timeline, start_ts)
    source = "target_timeline" if projected_soc is not None else "current_soc_fallback"
    if projected_soc is None:
        projected_soc = _clamp(safe_float(current_soc, 0.0), 0.0, 100.0)

    action = str((window or {}).get("action") or "")
    if action in {"eco_plus_export_candidate", "arbitrage_export_candidate"}:
        segment_available_wh = _configured_optional_float(window or {}, "export_segment_available_wh")
        if segment_available_wh is not None and safe_float(capacity_wh, 0.0) > 0.0:
            reserve_floor_soc = _clamp(
                safe_float((reserve or {}).get("effective_min_soc_pct"), 0.0),
                0.0,
                100.0,
            )
            segment_soc = reserve_floor_soc + (
                max(0.0, segment_available_wh) / safe_float(capacity_wh, 1.0)
            ) * 100.0
            projected_soc = max(projected_soc, _clamp(segment_soc, 0.0, 100.0))
            source = "%s+segment_budget" % source
    return _clamp(projected_soc, 0.0, 100.0), source


def _policy_projection_points(forecast_timeline, target_timeline):
    for point in forecast_timeline or []:
        if not isinstance(point, dict):
            continue
        if safe_float(point.get("ts"), 0.0) > 0.0 and safe_float(point.get("soc"), -1.0) >= 0.0:
            return forecast_timeline, "forecast_timeline"
    return target_timeline or [], "target_timeline"


def _policy_forecast_charge_wh(annotated, start_ts, end_ts, charge_budget_w):
    budget_w = max(0.0, safe_float(charge_budget_w, 0.0))
    if budget_w <= 0.0 or end_ts <= start_ts:
        return 0.0, False
    energy_wh = 0.0
    used_forecast = False
    for slot in annotated or []:
        if not isinstance(slot, dict):
            continue
        slot_start = safe_float(slot.get("ts"), 0.0)
        slot_end = safe_float(slot.get("end_ts"), slot_start + SLOT_MS)
        overlap_ms = max(0.0, min(end_ts, slot_end) - max(start_ts, slot_start))
        if overlap_ms <= 0.0:
            continue
        if not bool(slot.get("has_power_forecast")):
            continue
        used_forecast = True
        charge_w = min(budget_w, max(0.0, safe_float(slot.get("forecast_surplus_w"), 0.0)))
        energy_wh += charge_w * overlap_ms / 3600000.0
    return max(0.0, energy_wh), used_forecast


def _policy_rollforward_soc(
    decision,
    annotated,
    start_ts,
    end_ts,
    start_soc,
    baseline_delta_pct,
    capacity_wh,
):
    capacity_wh = max(0.0, safe_float(capacity_wh, 0.0))
    if capacity_wh <= 0.0 or end_ts <= start_ts:
        return _clamp(start_soc + baseline_delta_pct, 0.0, 100.0)
    if not isinstance(decision, dict) or not cfg_bool(decision.get("commands_allowed"), False) or bool(decision.get("blocked")):
        return _clamp(start_soc + baseline_delta_pct, 0.0, 100.0)

    target_state = str(decision.get("dv_target_state") or "").strip().upper()
    storage_budget = decision.get("storage_budget") if isinstance(decision.get("storage_budget"), dict) else {}
    duration_h = max(0.0, end_ts - start_ts) / 3600000.0
    passive_discharge_pct = min(0.0, safe_float(baseline_delta_pct, 0.0))

    if target_state in {"FORCE_EXPORT", "HEADROOM_EXPORT"}:
        export_w = max(0.0, safe_float(storage_budget.get("export_budget_w"), 0.0))
        export_pct = export_w * duration_h / capacity_wh * 100.0
        return _clamp(start_soc + passive_discharge_pct - export_pct, 0.0, 100.0)

    if target_state == "FORCE_CHARGE_PV":
        charge_wh, used_forecast = _policy_forecast_charge_wh(
            annotated,
            start_ts,
            end_ts,
            storage_budget.get("charge_budget_w"),
        )
        if not used_forecast:
            return _clamp(start_soc + baseline_delta_pct, 0.0, 100.0)
        charge_pct = charge_wh / capacity_wh * 100.0
        return _clamp(start_soc + passive_discharge_pct + charge_pct, 0.0, 100.0)

    return _clamp(start_soc + baseline_delta_pct, 0.0, 100.0)


def _build_policy_timeline(
    config,
    annotated,
    windows,
    reserve,
    flags,
    economics,
    mode,
    now_ms,
    current_soc,
    capacity_wh,
    blocked_reasons,
    target_timeline=None,
    forecast_timeline=None,
):
    timeline = []
    projection_points, projection_source = _policy_projection_points(forecast_timeline, target_timeline)
    policy_soc = _clamp(safe_float(current_soc, 0.0), 0.0, 100.0)
    cursor_ts = safe_float(now_ms, 0.0)
    for window in sorted(windows or [], key=lambda item: safe_float(item.get("start_ts"), 0.0)):
        if not isinstance(window, dict):
            continue
        start_ts = safe_float(window.get("start_ts"), 0.0)
        end_ts = safe_float(window.get("end_ts"), start_ts + SLOT_MS)
        if start_ts <= 0.0 or end_ts <= start_ts:
            continue
        if end_ts <= safe_float(now_ms, 0.0):
            continue
        effective_start_ts = max(start_ts, safe_float(now_ms, 0.0), cursor_ts)
        if effective_start_ts > cursor_ts:
            cursor_soc = _policy_target_soc_at(projection_points, cursor_ts)
            start_baseline_soc = _policy_target_soc_at(projection_points, effective_start_ts)
            if cursor_soc is not None and start_baseline_soc is not None:
                policy_soc = _clamp(policy_soc + start_baseline_soc - cursor_soc, 0.0, 100.0)
        evaluation_ts = safe_float(now_ms, 0.0) if start_ts <= safe_float(now_ms, 0.0) < end_ts else start_ts + 1.0
        projected_soc = policy_soc
        projected_soc_source = "live" if start_ts <= safe_float(now_ms, 0.0) < end_ts else "%s+policy_rollforward" % projection_source
        segment_decision = _build_policy_decision(
            config,
            annotated,
            windows,
            reserve,
            flags,
            economics,
            mode,
            evaluation_ts,
            projected_soc,
            capacity_wh,
            blocked_reasons,
        )
        segment_decision = dict(segment_decision or {})
        baseline_start_soc = _policy_target_soc_at(projection_points, effective_start_ts)
        baseline_end_soc = _policy_target_soc_at(projection_points, end_ts)
        baseline_delta_pct = (
            baseline_end_soc - baseline_start_soc
            if baseline_start_soc is not None and baseline_end_soc is not None
            else 0.0
        )
        projected_end_soc = _policy_rollforward_soc(
            segment_decision,
            annotated,
            effective_start_ts,
            end_ts,
            projected_soc,
            baseline_delta_pct,
            capacity_wh,
        )
        segment_decision.update({
            "start_ts": int(start_ts),
            "end_ts": int(end_ts),
            "source_action": window.get("action"),
            "source_reason": window.get("reason"),
            "projected_soc_pct": round(projected_soc, 1),
            "projected_soc_end_pct": round(projected_end_soc, 1),
            "projected_soc_source": projected_soc_source,
        })
        timeline.append(segment_decision)
        policy_soc = projected_end_soc
        cursor_ts = max(cursor_ts, end_ts)
    return timeline


def _base_plan(mode, reason, now_ms, blocked_reasons=None, economics=None, reserve=None, flags=None):
    plan_flags = flags or {}
    plan_flags.setdefault("commands_allowed", False)
    plan_flags.setdefault("owner_contract_version", OWNER_CONTRACT_VERSION)
    plan_flags.setdefault("price_domain_policy", "negative_hard_eeg_soft_score_fallback")
    plan_flags.setdefault("optimization_model", "rule_based_segment_budget_v2")
    plan_flags.setdefault("profit_profile", "standard")
    policy_reason = reason
    if not cfg_bool(plan_flags.get("live_soc_valid"), True):
        policy_reason = "live_values_missing:current_soc"
    policy_decision = _policy_empty_decision(policy_reason, reserve or {}, plan_flags)
    return {
        "active": False,
        "shadow": True,
        "mode": mode,
        "owner_contract_version": OWNER_CONTRACT_VERSION,
        "plan_owner": "direct_marketing:%s" % mode,
        "controller_owner": "storage_manager",
        "reason": reason,
        "created_ts": int(now_ms),
        "valid_until_ts": int(now_ms + SLOT_MS),
        "windows": [],
        "reserve": reserve or {},
        "economics": economics or {},
        "flags": plan_flags,
        "blocked_reasons": list(blocked_reasons or []),
        "policy_decision": policy_decision,
        "policy_timeline": [],
    }


def _reserve_state(config, mode, current_soc, capacity_wh, target_soc):
    ep_reserve = _configured_float(config, "ep_reserve_pct", 8.0)
    mode_default = {
        "safe": 30.0,
        "eco": 20.0,
        "eco_plus": 20.0,
        "arbitrage": 12.0,
    }.get(mode, 30.0)

    home_reserve = _configured_float(
        config,
        "direct_marketing_home_reserve_soc_pct",
        max(ep_reserve, mode_default),
    )
    night_reserve = _configured_float(
        config,
        "direct_marketing_night_reserve_soc_pct",
        max(home_reserve, mode_default),
    )
    morning_target_raw = None
    if isinstance(config, dict):
        morning_target_raw = config.get("direct_marketing_morning_export_target_soc_pct")
    morning_target = None
    if morning_target_raw is not None and str(morning_target_raw).strip() != "":
        morning_target = _clamp(safe_float(morning_target_raw, home_reserve), 0.0, 100.0)

    reserve_floor = _clamp(max(ep_reserve, home_reserve, night_reserve), 0.0, 100.0)
    current_soc = _clamp(safe_float(current_soc, 0.0), 0.0, 100.0)
    capacity_wh = max(0.0, safe_float(capacity_wh, 0.0))
    available_soc = max(0.0, current_soc - reserve_floor)

    return {
        "current_soc_pct": round(current_soc, 1),
        "target_soc_pct": round(_clamp(safe_float(target_soc, current_soc), 0.0, 100.0), 1),
        "ep_reserve_soc_pct": round(ep_reserve, 1),
        "home_reserve_soc_pct": round(_clamp(home_reserve, 0.0, 100.0), 1),
        "night_reserve_soc_pct": round(_clamp(night_reserve, 0.0, 100.0), 1),
        "effective_min_soc_pct": round(reserve_floor, 1),
        "morning_export_target_soc_pct": round(morning_target, 1) if morning_target is not None else None,
        "available_export_soc_pct": round(available_soc, 1),
        "available_export_wh": round((available_soc / 100.0) * capacity_wh, 0),
    }


def _economic_state(config, annotated):
    efficiency_pct = _clamp(
        safe_float(config.get("direct_marketing_roundtrip_efficiency_pct"), 85.0),
        1.0,
        100.0,
    )
    efficiency = efficiency_pct / 100.0
    degradation = max(0.0, safe_float(config.get("direct_marketing_degradation_ct_per_kwh"), 4.0))
    safety_margin = _clamp(
        safe_float(config.get("direct_marketing_safety_margin_ct_per_kwh"), 0.0),
        -10.0,
        50.0,
    )
    min_margin_pct = max(0.0, safe_float(config.get("direct_marketing_min_margin_pct"), 10.0))
    min_profit_ct = max(0.0, safe_float(config.get("direct_marketing_min_profit_ct_per_kwh"), 0.0))
    fixed_fee_ct = max(0.0, safe_float(config.get("direct_marketing_fee_ct_per_kwh"), 0.0))

    if not annotated:
        return {
            "roundtrip_efficiency_pct": round(efficiency_pct, 1),
            "battery_cost_ct_per_kwh": round(degradation, 2),
            "safety_margin_ct_per_kwh": round(safety_margin, 2),
            "min_margin_pct": round(min_margin_pct, 1),
            "min_profit_ct_per_kwh": round(min_profit_ct, 2),
            "grid_spread_ct_per_kwh": 0.0,
            "grid_margin_pct": 0.0,
            "grid_profit_ok": False,
            "pv_shift_spread_ct_per_kwh": 0.0,
            "pv_shift_margin_pct": 0.0,
            "pv_shift_profit_ok": False,
            "best_spread_ct_per_kwh": 0.0,
            "best_margin_pct": 0.0,
            "profit_ok": False,
        }

    best_low = min(annotated, key=lambda s: s["billing_ct"])
    best_low_market = min(annotated, key=lambda s: s["net_sell_ct"])
    best_high = max(annotated, key=lambda s: s["net_sell_ct"])
    low_import_cost = best_low["billing_ct"] / max(0.01, efficiency)
    grid_spread = best_high["net_sell_ct"] - low_import_cost - degradation - safety_margin
    grid_margin = (grid_spread / max(1.0, abs(low_import_cost))) * 100.0
    grid_profit_ok = bool(grid_spread >= min_profit_ct and grid_margin >= min_margin_pct)

    # Eco+ verschiebt PV-Ertrag: Kostenbasis ist der entgangene Billigpreis-
    # Verkauf, nicht der komplette Netzbezug wie bei echter Arbitrage.
    # Die fixe Direktvermarktergebühr darf dabei nicht als positiver
    # Opportunitätskosten-Abzug wirken, sonst würde eine höhere Gebühr den
    # PV-Shift-Spread künstlich verbessern.
    pv_shift_revenue = best_high["net_sell_ct"] * efficiency
    pv_shift_opportunity = best_low_market["net_sell_ct"] + fixed_fee_ct
    pv_shift_cost_basis = abs(pv_shift_opportunity) + degradation + safety_margin
    pv_shift_spread = pv_shift_revenue - pv_shift_opportunity - degradation - safety_margin
    pv_shift_margin = (pv_shift_spread / max(1.0, pv_shift_cost_basis)) * 100.0
    pv_shift_profit_ok = bool(pv_shift_spread >= min_profit_ct and pv_shift_margin >= min_margin_pct)

    return {
        "roundtrip_efficiency_pct": round(efficiency_pct, 1),
        "battery_cost_ct_per_kwh": round(degradation, 2),
        "safety_margin_ct_per_kwh": round(safety_margin, 2),
        "min_margin_pct": round(min_margin_pct, 1),
        "min_profit_ct_per_kwh": round(min_profit_ct, 2),
        "best_low_ts": int(best_low["ts"]),
        "best_high_ts": int(best_high["ts"]),
        "best_low_market_ts": int(best_low_market["ts"]),
        "best_low_t": _format_t(best_low["ts"]),
        "best_low_market_t": _format_t(best_low_market["ts"]),
        "best_high_t": _format_t(best_high["ts"]),
        "best_low_billing_ct": round(best_low["billing_ct"], 2),
        "best_low_market_ct": round(best_low_market["market_ct"], 2),
        "best_low_net_sell_ct": round(best_low_market["net_sell_ct"], 2),
        "pv_shift_opportunity_ct": round(pv_shift_opportunity, 2),
        "pv_shift_cost_basis_ct": round(pv_shift_cost_basis, 2),
        "best_high_market_ct": round(best_high["market_ct"], 2),
        "best_high_net_sell_ct": round(best_high["net_sell_ct"], 2),
        "best_high_effective_pv_sell_ct": round(pv_shift_revenue, 2),
        "grid_spread_ct_per_kwh": round(grid_spread, 2),
        "grid_margin_pct": round(grid_margin, 1),
        "grid_profit_ok": grid_profit_ok,
        "pv_shift_spread_ct_per_kwh": round(pv_shift_spread, 2),
        "pv_shift_margin_pct": round(pv_shift_margin, 1),
        "pv_shift_profit_ok": pv_shift_profit_ok,
        "best_spread_ct_per_kwh": round(grid_spread, 2),
        "best_margin_pct": round(grid_margin, 1),
        "profit_ok": grid_profit_ok,
    }


def _new_slot_action(slot, action, reason, extra=None):
    entry = {
        "start_ts": int(slot["ts"]),
        "end_ts": int(slot["end_ts"]),
        "action": action,
        "reason": reason,
        "market_ct": slot["market_ct"],
        "billing_ct": slot["billing_ct"],
        "score": slot["score"],
        "net_sell_ct": slot.get("net_sell_ct"),
        "market_price_source": slot.get("market_price_source"),
        "market_price_resolution_min": slot.get("market_price_resolution_min"),
    }
    for key in (
        "pv_w",
        "home_w",
        "wp_w",
        "heater_w",
        "wallbox_w",
        "forecast_surplus_w",
        "forecast_deficit_w",
        "has_power_forecast",
    ):
        if key in slot:
            entry[key] = slot[key]
    if extra:
        entry.update(extra)
    return entry


def _entry_duration_h(entry):
    return max(0.0, (safe_float(entry.get("end_ts"), 0.0) - safe_float(entry.get("start_ts"), 0.0)) / 3600000.0)


def _forecast_deficit_wh(annotated, start_ts, end_ts, enabled=True):
    if not enabled or not annotated:
        return 0.0, False
    start_ts = safe_float(start_ts, 0.0)
    end_ts = safe_float(end_ts, 0.0)
    if end_ts <= start_ts:
        return 0.0, False
    total_wh = 0.0
    used_forecast = False
    for slot in annotated:
        if not slot.get("has_power_forecast"):
            continue
        slot_start = safe_float(slot.get("ts"), 0.0)
        slot_end = safe_float(slot.get("end_ts"), slot_start + SLOT_MS)
        overlap_ms = min(end_ts, slot_end) - max(start_ts, slot_start)
        if overlap_ms <= 0:
            continue
        used_forecast = True
        total_wh += max(0.0, safe_float(slot.get("forecast_deficit_w"), 0.0)) * (overlap_ms / 3600000.0)
    return max(0.0, total_wh), used_forecast


def _entry_target_export_wh(entry, reserve, capacity_wh):
    reserve_floor = _clamp(safe_float(reserve.get("effective_min_soc_pct"), 0.0), 0.0, 100.0)
    target_soc = _clamp(safe_float(entry.get("target_soc_pct"), 100.0), 0.0, 100.0)
    if target_soc <= reserve_floor:
        target_soc = 100.0
    return max(0.0, ((target_soc - reserve_floor) / 100.0) * max(0.0, safe_float(capacity_wh, 0.0)))


def _entry_recharge_wh(entry, flags, efficiency):
    action = entry.get("action")
    duration_h = _entry_duration_h(entry)
    if duration_h <= 0.0:
        return 0.0, "none"
    if action == "eco_plus_store_pv_candidate":
        max_power_w = max(
            0.0,
            safe_float(entry.get("max_power_w"), safe_float(flags.get("pv_store_max_w"), 0.0)),
        )
        if entry.get("has_power_forecast"):
            forecast_surplus_w = max(0.0, safe_float(entry.get("forecast_surplus_w"), 0.0))
            if max_power_w > 0.0:
                max_power_w = min(max_power_w, forecast_surplus_w)
            else:
                max_power_w = forecast_surplus_w
            source = "forecast_pv_surplus"
        else:
            source = "window_power"
        return max(0.0, max_power_w * duration_h * max(0.01, efficiency)), source
    if action == "arbitrage_grid_charge_candidate":
        max_power_w = max(
            0.0,
            safe_float(entry.get("max_power_w"), safe_float(flags.get("max_grid_charge_w"), 0.0)),
        )
        return max(0.0, max_power_w * duration_h * max(0.01, efficiency)), "grid_charge_window"
    return 0.0, "none"


def _pv_store_entry_stored_wh(entry, flags, efficiency):
    if entry.get("action") != "eco_plus_store_pv_candidate":
        return 0.0
    duration_h = _entry_duration_h(entry)
    if duration_h <= 0.0:
        return 0.0
    max_power_w = max(
        0.0,
        safe_float(entry.get("max_power_w"), safe_float(flags.get("pv_store_max_w"), 0.0)),
    )
    if entry.get("has_power_forecast"):
        forecast_surplus_w = max(0.0, safe_float(entry.get("forecast_surplus_w"), 0.0))
        max_power_w = min(max_power_w, forecast_surplus_w) if max_power_w > 0.0 else forecast_surplus_w
    return max(0.0, max_power_w * duration_h * max(0.01, efficiency))


def _entry_export_wh(entry):
    if entry.get("action") not in ("eco_plus_export_candidate", "arbitrage_export_candidate"):
        return 0.0
    return max(0.0, safe_float(entry.get("max_power_w"), 0.0) * _entry_duration_h(entry))


def _pv_store_priority_key(entry):
    reason = str(entry.get("reason") or "")
    start_ts = safe_float(entry.get("start_ts"), 0.0)
    net_sell = safe_float(entry.get("net_sell_ct"), safe_float(entry.get("avg_market_ct"), 0.0))
    if reason == "negative_price":
        return (0, start_ts)
    if reason == "threshold_below_eeg":
        return (1, net_sell, start_ts)
    return (2, net_sell, start_ts)


def _apply_pv_store_energy_budget(entries, reserve, capacity_wh, flags, efficiency, current_soc):
    if not entries or not flags.get("pv_store_enable"):
        return entries, 0

    cap_wh = max(0.0, safe_float(capacity_wh, 0.0))
    if cap_wh <= 0.0:
        return entries, 0

    adjusted = [dict(entry) for entry in entries]
    ordered_indices = sorted(range(len(adjusted)), key=lambda idx: safe_float(adjusted[idx].get("start_ts"), 0.0))
    current_need_wh = 0.0
    cluster = []
    changed = 0

    def target_need_from_cluster(cluster_indices, fallback_soc):
        targets = []
        for idx in cluster_indices:
            raw = adjusted[idx].get("target_soc_pct")
            if raw is None or str(raw).strip() == "":
                continue
            value = safe_float(raw, 0.0)
            if value > 0.0:
                targets.append(value)
        target_soc = max(targets) if targets else fallback_soc
        target_soc = _clamp(target_soc, 0.0, 100.0)
        return max(0.0, ((target_soc - _clamp(safe_float(current_soc, 0.0), 0.0, 100.0)) / 100.0) * cap_wh)

    def flush_cluster():
        nonlocal current_need_wh, changed
        if not cluster:
            return
        cluster_indices = list(cluster)
        cluster.clear()
        fallback_soc = safe_float(reserve.get("target_soc_pct"), 100.0)
        if fallback_soc <= 0.0:
            fallback_soc = 100.0
        budget_need_wh = max(current_need_wh, target_need_from_cluster(cluster_indices, fallback_soc))
        if budget_need_wh <= 50.0:
            for idx in cluster_indices:
                if adjusted[idx].get("action") == "eco_plus_store_pv_candidate":
                    adjusted[idx]["_remove_pv_store_budget"] = True
                    adjusted[idx]["pv_store_budget_limited"] = True
                    adjusted[idx]["pv_store_budget_need_wh"] = round(budget_need_wh, 0)
                    changed += 1
            current_need_wh = 0.0
            return

        remaining_wh = budget_need_wh
        selected_wh = 0.0
        selected = {}
        for idx in sorted(cluster_indices, key=lambda item: _pv_store_priority_key(adjusted[item])):
            stored_wh = _pv_store_entry_stored_wh(adjusted[idx], flags, efficiency)
            if stored_wh <= 50.0 or remaining_wh <= 50.0:
                continue
            take_wh = min(stored_wh, remaining_wh)
            selected[idx] = (take_wh, stored_wh)
            selected_wh += take_wh
            remaining_wh -= take_wh

        for idx in cluster_indices:
            entry = adjusted[idx]
            take_wh, stored_wh = selected.get(idx, (0.0, _pv_store_entry_stored_wh(entry, flags, efficiency)))
            if take_wh <= 50.0:
                entry["_remove_pv_store_budget"] = True
                entry["pv_store_budget_limited"] = True
                entry["pv_store_budget_need_wh"] = round(budget_need_wh, 0)
                entry["pv_store_budget_selected_wh"] = round(selected_wh, 0)
                changed += 1
                continue
            entry["pv_store_budget_need_wh"] = round(budget_need_wh, 0)
            entry["pv_store_budget_selected_wh"] = round(selected_wh, 0)
            entry["pv_store_budget_priority"] = "negative_hard" if entry.get("reason") == "negative_price" else "cheapest_soft"
            if take_wh + 1.0 < stored_wh:
                duration_h = _entry_duration_h(entry)
                capped_w = 0
                if duration_h > 0.0:
                    capped_w = int(round(take_wh / (duration_h * max(0.01, efficiency))))
                if capped_w < 300:
                    entry["_remove_pv_store_budget"] = True
                else:
                    entry["max_power_w"] = capped_w
                entry["pv_store_budget_limited"] = True
                changed += 1
        current_need_wh = max(0.0, budget_need_wh - selected_wh)

    for idx in ordered_indices:
        action = adjusted[idx].get("action")
        if action == "eco_plus_store_pv_candidate":
            cluster.append(idx)
            continue
        if action in ("eco_plus_export_candidate", "arbitrage_export_candidate"):
            flush_cluster()
            current_need_wh = min(cap_wh, current_need_wh + _entry_export_wh(adjusted[idx]))

    flush_cluster()

    if not changed:
        return entries, 0
    filtered = [
        {key: value for key, value in entry.items() if key != "_remove_pv_store_budget"}
        for entry in adjusted
        if not entry.get("_remove_pv_store_budget")
    ]
    filtered.sort(key=lambda item: safe_float(item.get("start_ts"), 0.0))
    return filtered, changed


def _negative_headroom_slot_wh(entry, flags):
    duration_h = _entry_duration_h(entry)
    if duration_h <= 0.0:
        return 0.0
    max_power_w = max(
        0.0,
        safe_float(entry.get("max_power_w"), safe_float(flags.get("pv_store_max_w"), 0.0)),
    )
    if entry.get("has_power_forecast"):
        forecast_surplus_w = max(0.0, safe_float(entry.get("forecast_surplus_w"), 0.0))
        max_power_w = min(max_power_w, forecast_surplus_w) if max_power_w > 0.0 else forecast_surplus_w
    return max(0.0, max_power_w * duration_h)


def _apply_negative_headroom_holds(entries, annotated, reserve, capacity_wh, flags, config, now_ms, soc_ceiling):
    if not (
        flags.get("negative_headroom_enable")
        and flags.get("pv_store_enable")
        and (flags.get("negative_price_no_export") or flags.get("low_price_headroom_enable"))
    ):
        return entries, 0

    lookahead_ms = max(0.0, safe_float(flags.get("negative_headroom_lookahead_min"), 240.0)) * 60.0 * 1000.0
    min_window_ms = max(0.0, safe_float(flags.get("negative_headroom_min_window_min"), 30.0)) * 60.0 * 1000.0
    min_surplus_wh = max(0.0, safe_float(flags.get("negative_headroom_min_surplus_wh"), 1000.0))
    buffer_pct = max(0.0, safe_float(flags.get("negative_headroom_buffer_pct"), 3.0))
    if lookahead_ms <= 0.0 or min_window_ms <= 0.0:
        return entries, 0

    sorted_entries = sorted((dict(entry) for entry in entries), key=lambda item: safe_float(item.get("start_ts"), 0.0))
    groups = []
    current = None
    for entry in sorted_entries:
        if entry.get("action") != "eco_plus_store_pv_candidate":
            continue
        reason = str(entry.get("reason") or "")
        is_negative_group = reason == "negative_price"
        if not is_negative_group and not flags.get("low_price_headroom_enable"):
            continue
        if not is_negative_group and reason != "low_price":
            continue
        start_ts = safe_float(entry.get("start_ts"), 0.0)
        end_ts = safe_float(entry.get("end_ts"), start_ts + SLOT_MS)
        slot_wh = _negative_headroom_slot_wh(entry, flags)
        group_kind = "negative_price" if is_negative_group else "low_price"
        if (
            current
            and current.get("kind") == group_kind
            and start_ts <= safe_float(current.get("end_ts"), 0.0) + 1000.0
        ):
            current["end_ts"] = max(safe_float(current.get("end_ts"), 0.0), end_ts)
            current["forecast_surplus_wh"] = safe_float(current.get("forecast_surplus_wh"), 0.0) + slot_wh
            current["slot_count"] = safe_int(current.get("slot_count"), 0) + 1
            continue
        current = {
            "kind": group_kind,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "forecast_surplus_wh": slot_wh,
            "slot_count": 1,
        }
        groups.append(current)

    eligible_groups = []
    for group in groups:
        duration_ms = safe_float(group.get("end_ts"), 0.0) - safe_float(group.get("start_ts"), 0.0)
        surplus_wh = safe_float(group.get("forecast_surplus_wh"), 0.0)
        if duration_ms + 1.0 < min_window_ms or surplus_wh + 1.0 < min_surplus_wh:
            continue
        eligible_groups.append(group)
    if not eligible_groups:
        return entries, 0

    entry_by_start = {int(safe_float(entry.get("start_ts"), 0.0)): idx for idx, entry in enumerate(sorted_entries)}
    # Headroom ist die hoehere DV-Prioritaet. Bereits erkannte Verkaufsfenster
    # werden deshalb in denselben Headroom-Bedarf aufgenommen und danach nach
    # Nettoerloes priorisiert. Netzladefenster bleiben unantastbar.
    protected_actions = {"arbitrage_grid_charge_candidate"}
    reserve_floor = _clamp(safe_float(reserve.get("effective_min_soc_pct"), 0.0), 0.0, 100.0)
    cap_wh = max(0.0, safe_float(capacity_wh, 0.0))
    changed = 0

    for slot in sorted(annotated or [], key=lambda item: safe_float(item.get("ts"), 0.0)):
        start_ts = safe_float(slot.get("ts"), 0.0)
        end_ts = safe_float(slot.get("end_ts"), start_ts + SLOT_MS)
        if end_ts <= now_ms or slot.get("is_negative"):
            continue
        next_group = None
        for group in eligible_groups:
            delta_ms = safe_float(group.get("start_ts"), 0.0) - start_ts
            if delta_ms <= 0.0 or delta_ms > lookahead_ms:
                continue
            next_group = group
            break
        if not next_group:
            continue

        idx = entry_by_start.get(int(start_ts))
        existing = sorted_entries[idx] if idx is not None else None
        if existing and existing.get("action") in protected_actions:
            continue
        surplus_wh = safe_float(next_group.get("forecast_surplus_wh"), 0.0)
        group_kind = str(next_group.get("kind") or "negative_price")
        if existing and existing.get("action") == "eco_plus_store_pv_candidate":
            existing_reason = str(existing.get("reason") or "")
            if group_kind != "negative_price" or existing_reason == "negative_price":
                continue
        negative_limited = group_kind == "negative_price"
        required_headroom_pct = 0.0
        if cap_wh > 0.0:
            required_headroom_pct = _clamp((surplus_wh / cap_wh) * 100.0 + buffer_pct, 0.0, 100.0)
        headroom_ceiling = soc_ceiling
        if required_headroom_pct > 0.0:
            headroom_ceiling = min(headroom_ceiling, 100.0 - required_headroom_pct)
        headroom_ceiling = round(_clamp(headroom_ceiling, reserve_floor, 100.0), 1)
        extra = {
            "max_power_w": 0,
            "soc_ceiling_pct": headroom_ceiling,
            "headroom_limited": True,
            "negative_headroom_limited": negative_limited,
            "pv_store_headroom_limited": True,
            "pv_store_headroom_next_reason": group_kind,
            "negative_headroom_next_start_ts": int(safe_float(next_group.get("start_ts"), 0.0)),
            "negative_headroom_next_end_ts": int(safe_float(next_group.get("end_ts"), 0.0)),
            "negative_headroom_window_min": round(
                max(0.0, safe_float(next_group.get("end_ts"), 0.0) - safe_float(next_group.get("start_ts"), 0.0)) / 60000.0,
                1,
            ),
            "negative_headroom_forecast_surplus_wh": round(surplus_wh, 0),
            "negative_headroom_required_pct": round(required_headroom_pct, 1),
            "negative_headroom_lookahead_min": round(lookahead_ms / 60000.0, 1),
            "storage_action": "charge_block_auto_limit",
        }
        replacement_reason = "negative_price_headroom" if negative_limited else "low_price_headroom"
        replacement = _new_slot_action(slot, "eco_plus_negative_headroom_hold", replacement_reason, extra)
        if idx is None:
            entry_by_start[int(start_ts)] = len(sorted_entries)
            sorted_entries.append(replacement)
        else:
            sorted_entries[idx] = replacement
        changed += 1

    if not changed:
        return entries, 0

    max_export_w = max(0.0, safe_float(flags.get("max_export_w"), 0.0))
    max_cycle_wh = max(0.0, safe_float(flags.get("max_cycles_per_day"), 1.0)) * cap_wh
    available_export_wh = max(0.0, safe_float(reserve.get("available_export_wh"), 0.0))
    candidates_by_charge_start = {}
    for idx, entry in enumerate(sorted_entries):
        if entry.get("action") != "eco_plus_negative_headroom_hold":
            continue
        charge_start = safe_int(entry.get("negative_headroom_next_start_ts"), 0)
        if charge_start <= 0:
            continue
        candidates_by_charge_start.setdefault(charge_start, []).append(idx)

    for charge_start, candidate_indices in candidates_by_charge_start.items():
        sample = sorted_entries[candidate_indices[0]]
        required_pct = _clamp(safe_float(sample.get("negative_headroom_required_pct"), 0.0), 0.0, 100.0)
        target_soc = max(reserve_floor, 100.0 - required_pct)
        current_soc = _clamp(safe_float(reserve.get("current_soc_pct"), reserve_floor), 0.0, 100.0)
        required_export_wh = max(0.0, ((current_soc - target_soc) / 100.0) * cap_wh)
        required_export_wh = min(required_export_wh, available_export_wh)
        if max_cycle_wh > 0.0:
            required_export_wh = min(required_export_wh, max_cycle_wh)
        remaining_wh = required_export_wh
        selected_wh = 0.0
        ordered_candidates = sorted(
            candidate_indices,
            key=lambda idx: (
                safe_float(sorted_entries[idx].get("net_sell_ct"), safe_float(sorted_entries[idx].get("market_ct"), 0.0)),
                -safe_float(sorted_entries[idx].get("start_ts"), 0.0),
            ),
            reverse=True,
        )
        for idx in ordered_candidates:
            entry = sorted_entries[idx]
            duration_h = _entry_duration_h(entry)
            take_wh = min(max_export_w * duration_h, remaining_wh) if duration_h > 0.0 else 0.0
            if take_wh >= 50.0 and duration_h > 0.0:
                entry["max_power_w"] = int(round(take_wh / duration_h))
                entry["headroom_export_selected"] = True
                selected_wh += take_wh
                remaining_wh -= take_wh
            else:
                entry["max_power_w"] = 0
                entry["headroom_export_selected"] = False
            entry["headroom_export_budget_wh"] = round(required_export_wh, 0)
            entry["headroom_export_selected_wh"] = round(selected_wh, 0)
            entry["headroom_export_remaining_wh"] = round(max(0.0, remaining_wh), 0)
            entry["headroom_export_charge_start_ts"] = charge_start
        for idx in candidate_indices:
            sorted_entries[idx]["headroom_export_selected_wh"] = round(selected_wh, 0)
            sorted_entries[idx]["headroom_export_remaining_wh"] = round(max(0.0, remaining_wh), 0)

    sorted_entries.sort(key=lambda item: safe_float(item.get("start_ts"), 0.0))
    return sorted_entries, changed


def _prioritize_export_entries(entries, reserve, capacity_wh, flags, efficiency, annotated=None):
    export_actions = {"eco_plus_export_candidate", "arbitrage_export_candidate"}
    recharge_actions = {"eco_plus_store_pv_candidate", "arbitrage_grid_charge_candidate"}
    if not any(entry.get("action") in export_actions for entry in entries):
        return entries, 0

    available_wh = max(0.0, safe_float(reserve.get("available_export_wh"), 0.0))
    export_capacity_wh = max(
        available_wh,
        max(0.0, ((100.0 - safe_float(reserve.get("effective_min_soc_pct"), 0.0)) / 100.0) * max(0.0, safe_float(capacity_wh, 0.0))),
    )
    max_cycle_wh = max(0.0, safe_float(flags.get("max_cycles_per_day"), 1.0)) * max(0.0, safe_float(capacity_wh, 0.0))
    global_remaining_wh = max_cycle_wh if max_cycle_wh > 0.0 else None
    load_reserve_enabled = cfg_bool(flags.get("export_segment_load_reserve_enable"), True)
    ordered_indices = sorted(range(len(entries)), key=lambda idx: safe_float(entries[idx].get("start_ts"), 0.0))
    adjusted = [dict(entry) for entry in entries]
    first_ts = min((safe_float(entry.get("start_ts"), 0.0) for entry in entries), default=0.0)
    horizon_end_ts = max(
        [safe_float(entry.get("end_ts"), 0.0) for entry in entries]
        + [safe_float(slot.get("end_ts"), safe_float(slot.get("ts"), 0.0) + SLOT_MS) for slot in (annotated or [])],
        default=first_ts,
    )
    segment_start_ts = first_ts
    segment_id = 0
    segment_budget_wh = min(available_wh, export_capacity_wh)
    budget_source = "current_soc"
    pending_exports = []
    limited = 0

    def apply_segment(export_indices, end_ts, next_recharge):
        nonlocal limited, global_remaining_wh
        if end_ts < segment_start_ts:
            end_ts = segment_start_ts
        load_reserve_wh, used_load_forecast = _forecast_deficit_wh(
            annotated or [],
            segment_start_ts,
            end_ts,
            enabled=load_reserve_enabled,
        )
        raw_budget_wh = min(segment_budget_wh, export_capacity_wh)
        export_budget_wh = max(0.0, raw_budget_wh - load_reserve_wh)
        if global_remaining_wh is not None:
            export_budget_wh = min(export_budget_wh, max(0.0, global_remaining_wh))

        candidates = []
        for idx in export_indices:
            entry = entries[idx]
            duration_h = _entry_duration_h(entry)
            max_power_w = max(0.0, safe_float(entry.get("max_power_w"), 0.0))
            energy_wh = max_power_w * duration_h
            if energy_wh <= 0.0:
                continue
            candidates.append({
                "idx": idx,
                "energy_wh": energy_wh,
                "max_power_w": max_power_w,
                "net_sell_ct": safe_float(entry.get("net_sell_ct"), safe_float(entry.get("market_ct"), 0.0)),
                "start_ts": safe_float(entry.get("start_ts"), 0.0),
            })

        selected = {}
        remaining_wh = export_budget_wh
        for candidate in sorted(candidates, key=lambda item: (item["net_sell_ct"], item["start_ts"]), reverse=True):
            if remaining_wh <= 50.0:
                break
            take_wh = min(candidate["energy_wh"], remaining_wh)
            if take_wh <= 50.0:
                continue
            selected[candidate["idx"]] = take_wh
            remaining_wh -= take_wh

        selected_wh = sum(selected.values())
        if global_remaining_wh is not None:
            global_remaining_wh = max(0.0, global_remaining_wh - selected_wh)

        next_recharge_ts = int(next_recharge.get("start_ts")) if isinstance(next_recharge, dict) else None
        next_recharge_action = next_recharge.get("action") if isinstance(next_recharge, dict) else None
        segment_meta = {
            "export_segment_id": segment_id,
            "export_segment_start_ts": int(segment_start_ts),
            "export_segment_end_ts": int(end_ts),
            "export_segment_budget_source": budget_source,
            "export_segment_budget_wh": round(raw_budget_wh, 0),
            "export_segment_available_wh": round(export_budget_wh, 0),
            "export_segment_load_reserve_wh": round(load_reserve_wh, 0),
            "export_segment_load_forecast_used": bool(used_load_forecast),
            "export_segment_selected_wh": round(selected_wh, 0),
        }
        if next_recharge_ts is not None:
            segment_meta["export_segment_next_recharge_ts"] = next_recharge_ts
            segment_meta["export_segment_next_recharge_action"] = next_recharge_action

        segment_limited = 0
        for idx in export_indices:
            entry = entries[idx]
            chosen_wh = selected.get(idx, 0.0)
            duration_h = _entry_duration_h(entry)
            if chosen_wh <= 50.0 or duration_h <= 0.0:
                next_entry = dict(entry)
                next_entry.update(segment_meta)
                next_entry.update({
                    "action": "eco_plus_house_supply",
                    "reason": "reserve_for_higher_profit",
                    "max_power_w": 0,
                    "energy_limited": True,
                })
                adjusted[idx] = next_entry
                segment_limited += 1
                continue
            original_w = max(0.0, safe_float(entry.get("max_power_w"), 0.0))
            next_entry = dict(entry)
            next_entry.update(segment_meta)
            next_entry["max_power_w"] = int(round(min(original_w, chosen_wh / duration_h)))
            if chosen_wh + 1.0 < original_w * duration_h:
                next_entry["energy_limited"] = True
                segment_limited += 1
            adjusted[idx] = next_entry
        limited += segment_limited
        return max(0.0, raw_budget_wh - load_reserve_wh - selected_wh)

    for idx in ordered_indices:
        entry = entries[idx]
        action = entry.get("action")
        if action in recharge_actions:
            segment_budget_wh = apply_segment(pending_exports, safe_float(entry.get("start_ts"), segment_start_ts), entry)
            pending_exports = []
            recharge_wh, recharge_source = _entry_recharge_wh(entry, flags, efficiency)
            target_export_wh = _entry_target_export_wh(entry, reserve, capacity_wh)
            if target_export_wh <= 0.0:
                target_export_wh = export_capacity_wh
            segment_budget_wh = min(max(segment_budget_wh, 0.0) + recharge_wh, target_export_wh, export_capacity_wh)
            budget_source = "%s:%s" % (action, recharge_source)
            segment_start_ts = safe_float(entry.get("end_ts"), safe_float(entry.get("start_ts"), segment_start_ts))
            segment_id += 1
            continue
        if action in export_actions:
            pending_exports.append(idx)

    apply_segment(pending_exports, horizon_end_ts, None)
    return adjusted, limited


def _group_windows(entries):
    windows = []
    for entry in sorted(entries, key=lambda e: e["start_ts"]):
        current = windows[-1] if windows else None
        mergeable = (
            current is not None
            and current.get("action") == entry.get("action")
            and current.get("reason") == entry.get("reason")
            and abs(int(current.get("end_ts", 0)) - int(entry.get("start_ts", 0))) <= 1000
            and current.get("max_power_w") == entry.get("max_power_w")
            and current.get("target_soc_pct") == entry.get("target_soc_pct")
            and current.get("soc_ceiling_pct") == entry.get("soc_ceiling_pct")
            and current.get("curtailment_allowed") == entry.get("curtailment_allowed")
            and current.get("curtail_export_limit_w") == entry.get("curtail_export_limit_w")
            and current.get("export_constraint_class") == entry.get("export_constraint_class")
            and current.get("hard_export_limit_active") == entry.get("hard_export_limit_active")
            and current.get("hard_export_limit_w") == entry.get("hard_export_limit_w")
            and current.get("export_constraint_scope") == entry.get("export_constraint_scope")
            and current.get("pv_export_allowed") == entry.get("pv_export_allowed")
            and current.get("headroom_limited") == entry.get("headroom_limited")
            and current.get("pv_store_budget_limited") == entry.get("pv_store_budget_limited")
            and current.get("pv_store_budget_need_wh") == entry.get("pv_store_budget_need_wh")
            and current.get("pv_store_budget_selected_wh") == entry.get("pv_store_budget_selected_wh")
            and current.get("pv_store_budget_priority") == entry.get("pv_store_budget_priority")
            and current.get("negative_headroom_next_start_ts") == entry.get("negative_headroom_next_start_ts")
            and current.get("negative_headroom_required_pct") == entry.get("negative_headroom_required_pct")
            and current.get("pv_store_headroom_next_reason") == entry.get("pv_store_headroom_next_reason")
            and current.get("export_segment_id") == entry.get("export_segment_id")
        )
        if not mergeable:
            item = {
                "start_ts": entry["start_ts"],
                "end_ts": entry["end_ts"],
                "start_t": _format_t(entry["start_ts"]),
                "end_t": _format_t(entry["end_ts"]),
                "action": entry["action"],
                "reason": entry["reason"],
                "slot_count": 1,
                "_market": [entry["market_ct"]],
                "_billing": [entry["billing_ct"]],
                "_score": [entry["score"]],
                "_net_sell": [(
                    safe_float(entry.get("net_sell_ct"), safe_float(entry.get("market_ct"), 0.0)),
                    max(0.0, _entry_duration_h(entry)),
                )],
            }
            for key in (
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
                "net_sell_ct",
                "market_price_source",
                "market_price_resolution_min",
                "energy_limited",
                "pv_store_budget_limited",
                "pv_store_budget_need_wh",
                "pv_store_budget_selected_wh",
                "pv_store_budget_priority",
                "storage_action",
                "pv_store_price_class",
                "pv_store_soft_threshold",
                "headroom_limited",
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
                "headroom_export_selected",
                "headroom_export_budget_wh",
                "headroom_export_selected_wh",
                "headroom_export_remaining_wh",
                "headroom_export_charge_start_ts",
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
                if key in entry:
                    item[key] = entry[key]
            windows.append(item)
            continue

        current["end_ts"] = entry["end_ts"]
        current["end_t"] = _format_t(entry["end_ts"])
        current["slot_count"] += 1
        if entry.get("energy_limited"):
            current["energy_limited"] = True
        current["_market"].append(entry["market_ct"])
        current["_billing"].append(entry["billing_ct"])
        current["_score"].append(entry["score"])
        current["_net_sell"].append((
            safe_float(entry.get("net_sell_ct"), safe_float(entry.get("market_ct"), 0.0)),
            max(0.0, _entry_duration_h(entry)),
        ))

    for item in windows:
        market = item.pop("_market")
        billing = item.pop("_billing")
        score = item.pop("_score")
        net_sell = item.pop("_net_sell")
        net_sell_duration_h = sum(duration_h for _value, duration_h in net_sell)
        avg_net_sell_ct = (
            sum(value * duration_h for value, duration_h in net_sell) / net_sell_duration_h
            if net_sell_duration_h > 0.0
            else sum(value for value, _duration_h in net_sell) / max(1, len(net_sell))
        )
        item["avg_market_ct"] = round(sum(market) / max(1, len(market)), 2)
        item["min_market_ct"] = round(min(market), 2)
        item["max_market_ct"] = round(max(market), 2)
        item["avg_billing_ct"] = round(sum(billing) / max(1, len(billing)), 2)
        item["avg_score"] = round(sum(score) / max(1, len(score)), 1)
        item["net_sell_ct"] = round(avg_net_sell_ct, 3)
        item["avg_net_sell_ct"] = round(avg_net_sell_ct, 3)
        item["min_net_sell_ct"] = round(min(value for value, _duration_h in net_sell), 3)
        item["max_net_sell_ct"] = round(max(value for value, _duration_h in net_sell), 3)
        item["theoretical_kwh"] = round(
            max(0.0, safe_float(item.get("max_power_w"), 0.0))
            * _entry_duration_h(item)
            / 1000.0,
            3,
        )
    return windows


def build_direct_marketing_shadow_plan(
    config,
    timeline,
    current_soc,
    capacity_wh,
    target_soc=None,
    now_ms=None,
    target_timeline=None,
):
    """Return a side-effect-free owner contract for Safe, Eco, Eco+ and Arbitrage.

    The returned plan contains candidate windows and command eligibility, but no
    RSCP command fields. The Storage Manager validates the contract again before
    it may own an active direct_marketing_* decision.
    """
    config = config or {}
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    if not cfg_bool(config.get("direct_marketing_enable"), False):
        return _base_plan("off", "disabled", now_ms, blocked_reasons=["disabled"])

    mode = _normalize_mode(config.get("direct_marketing_mode", "safe"))
    profile = _mode_profile(mode)
    pv_store_threshold = _pv_store_threshold_state(config)
    flags = {
        "export_enable": cfg_bool(config.get("direct_marketing_export_enable"), False),
        "grid_charge_enable": cfg_bool(config.get("direct_marketing_grid_charge_enable"), False),
        "arbitrage_enable": cfg_bool(
            config.get("direct_marketing_arbitrage_enable"), False
        ),
        "pv_store_enable": cfg_bool(config.get("direct_marketing_pv_store_enable"), True),
        "pv_store_threshold_ct": pv_store_threshold.get("value"),
        "pv_store_threshold_source": pv_store_threshold.get("source"),
        "pv_store_threshold_capacity_kwp": pv_store_threshold.get("capacity_kwp"),
        "pv_store_max_w": max(0.0, safe_float(config.get("direct_marketing_pv_store_max_w"), 0.0)),
        "pv_store_min_surplus_w": max(0.0, safe_float(config.get("direct_marketing_pv_store_min_surplus_w"), 300.0)),
        "pv_store_import_guard_w": max(0.0, safe_float(config.get("direct_marketing_pv_store_import_guard_w"), 80.0)),
        "pv_store_min_hold_s": max(0.0, safe_float(config.get("direct_marketing_pv_store_min_hold_s"), 600.0)),
        "pv_store_ramp_step_w": max(100.0, safe_float(config.get("direct_marketing_pv_store_ramp_step_w"), 300.0)),
        "pv_store_dc_only_enable": cfg_bool(config.get("direct_marketing_pv_store_dc_only_enable"), False),
        "pv_store_external_ac_guard_w": max(0.0, safe_float(config.get("direct_marketing_pv_store_external_ac_guard_w"), 100.0)),
        "pv_store_export_limit_guard_w": max(0.0, safe_float(config.get("direct_marketing_pv_store_export_limit_guard_w"), 100.0)),
        "pv_store_export_limit_ramp_bypass_w": max(0.0, safe_float(config.get("direct_marketing_pv_store_export_limit_ramp_bypass_w"), 300.0)),
        "negative_price_no_export": cfg_bool(config.get("direct_marketing_negative_price_no_export"), True),
        "negative_headroom_enable": cfg_bool(config.get("direct_marketing_negative_headroom_enable"), True),
        "negative_headroom_lookahead_min": max(0.0, safe_float(config.get("direct_marketing_negative_headroom_lookahead_min"), 240.0)),
        "negative_headroom_min_window_min": max(0.0, safe_float(config.get("direct_marketing_negative_headroom_min_window_min"), 30.0)),
        "negative_headroom_min_surplus_wh": max(0.0, safe_float(config.get("direct_marketing_negative_headroom_min_surplus_wh"), 1000.0)),
        "negative_headroom_buffer_pct": max(0.0, safe_float(config.get("direct_marketing_negative_headroom_buffer_pct"), 3.0)),
        "low_price_headroom_enable": cfg_bool(config.get("direct_marketing_low_price_headroom_enable"), True),
        "low_price_no_export": cfg_bool(config.get("direct_marketing_low_price_no_export"), True),
        "low_price_curtail_enable": cfg_bool(config.get("direct_marketing_low_price_curtail_enable"), False),
        "low_price_curtail_limit_w": max(
            0.0,
            safe_float(config.get("direct_marketing_low_price_curtail_limit_w"), 0.0),
        ),
        "eeg_enable": cfg_bool(config.get("direct_marketing_eeg_enable"), False),
        "eeg_grid_export_risk_ack": cfg_bool(
            config.get("direct_marketing_eeg_grid_export_risk_ack"), False
        ),
        "max_export_w": max(0.0, safe_float(config.get("direct_marketing_max_export_w"), 0.0)),
        "max_grid_charge_w": max(0.0, safe_float(config.get("direct_marketing_max_grid_charge_w"), 0.0)),
        "max_cycles_per_day": max(0.0, safe_float(config.get("direct_marketing_max_cycles_per_day"), 1.0)),
        "export_segment_load_reserve_enable": cfg_bool(
            config.get("direct_marketing_export_segment_load_reserve_enable"),
            True,
        ),
    }
    flags["commands_allowed"] = False
    flags["owner_contract_version"] = OWNER_CONTRACT_VERSION
    flags["price_domain_policy"] = "negative_hard_eeg_soft_score_fallback"
    flags["optimization_model"] = "rule_based_segment_budget_v2"
    flags["profit_profile"] = _normalize_profit_profile(config.get("direct_marketing_profit_profile", "standard"))

    flags["live_soc_valid"] = _valid_soc_input(current_soc)
    current_soc = safe_float(current_soc, 0.0) if flags["live_soc_valid"] else 0.0

    reserve = _reserve_state(config, mode, current_soc, capacity_wh, target_soc or current_soc)

    raw_slots = []
    price_quality_blockers = []
    current_price_quality_blockers = []
    price_quality_blocker_counts = {}
    unpriced_tail_blocker_counts = {}
    candidate_slots = []
    for slot in timeline or []:
        if not isinstance(slot, dict):
            continue
        ts = _slot_ts(slot)
        if ts < now_ms - SLOT_MS:
            continue
        if ts > now_ms + (48 * 3600 * 1000):
            continue
        candidate_slots.append((slot, ts, _slot_price_quality_blocker(slot, config)))

    last_valid_price_ts = max(
        (ts for _slot, ts, blocker in candidate_slots if not blocker),
        default=0,
    )
    unpriced_tail_slot_count = 0
    for slot, ts, price_blocker in candidate_slots:
        if price_blocker:
            if last_valid_price_ts > 0 and ts > last_valid_price_ts and ts > now_ms:
                unpriced_tail_slot_count += 1
                unpriced_tail_blocker_counts[price_blocker] = unpriced_tail_blocker_counts.get(price_blocker, 0) + 1
                continue
            price_quality_blockers.append(price_blocker)
            price_quality_blocker_counts[price_blocker] = price_quality_blocker_counts.get(price_blocker, 0) + 1
            if ts <= now_ms < _slot_end_ts(slot):
                current_price_quality_blockers.append(price_blocker)
            continue
        raw_slots.append(slot)

    if not raw_slots:
        blocked = sorted(set(price_quality_blockers)) or ["no_timeline"]
        reason = "price_quality_blocked" if price_quality_blockers else "no_timeline"
        return _base_plan(
            mode,
            reason,
            now_ms,
            blocked_reasons=blocked,
            reserve=reserve,
            flags=flags,
        )

    market_values = [_market_ct(slot) for slot in raw_slots]
    min_market_ct = min(market_values)
    max_market_ct = max(market_values)

    annotated = []
    threshold_ct = flags.get("pv_store_threshold_ct")
    threshold_configured = threshold_ct is not None
    for slot in raw_slots:
        market_ct = _market_ct(slot)
        billing_ct = _billing_ct(slot, market_ct)
        score = _price_score(slot, min_market_ct, max_market_ct)
        net_sell_ct = _net_sell_ct(market_ct, config)
        is_negative = market_ct < 0.0
        is_score_low = score >= profile["low_score_min"]
        is_threshold_low = bool(threshold_ct is not None and net_sell_ct <= safe_float(threshold_ct, 0.0))
        is_threshold_soft = bool(is_threshold_low and not is_negative)
        is_score_pv_store = bool(is_score_low and not threshold_configured)
        forecast_power = _slot_forecast_power(slot)
        annotated.append({
            "ts": _slot_ts(slot),
            "end_ts": _slot_end_ts(slot),
            "market_ct": market_ct,
            "billing_ct": billing_ct,
            "score": score,
            "market_price_source": _market_price_source(slot),
            "market_price_resolution_min": _market_price_resolution_min(slot),
            "is_negative": is_negative,
            "is_score_low": is_score_low,
            "is_score_pv_store": is_score_pv_store,
            "is_threshold_low": is_threshold_low,
            "is_threshold_soft": is_threshold_soft,
            "is_low": is_negative or is_score_low or is_threshold_low,
            "is_pv_store": is_negative or is_threshold_low or is_score_pv_store,
            "is_high": score <= profile["high_score_max"],
            "net_sell_ct": net_sell_ct,
            **forecast_power,
        })

    economics = _economic_state(config, annotated)
    blocked_reasons = list(current_price_quality_blockers)
    if not flags["live_soc_valid"]:
        blocked_reasons.append("live_values_missing:current_soc")

    if mode == "arbitrage":
        if not flags["arbitrage_enable"]:
            blocked_reasons.append("arbitrage_disabled")
        if not flags["export_enable"] or flags["max_export_w"] <= 0.0:
            blocked_reasons.append("export_not_enabled")
        if not flags["grid_charge_enable"] or flags["max_grid_charge_w"] <= 0.0:
            blocked_reasons.append("grid_charge_not_enabled")
        if (
            flags["eeg_enable"]
            and flags["export_enable"]
            and flags["grid_charge_enable"]
            and not flags["eeg_grid_export_risk_ack"]
        ):
            blocked_reasons.append("eeg_grid_export_ack_missing")
        if not economics.get("grid_profit_ok"):
            blocked_reasons.append("profit_below_threshold")
        if blocked_reasons:
            return _base_plan(
                mode,
                "blocked",
                now_ms,
                blocked_reasons=blocked_reasons,
                economics=economics,
                reserve=reserve,
                flags=flags,
            )

    if reserve.get("available_export_soc_pct", 0.0) <= 1.0 and (mode not in {"eco", "eco_plus"} or flags["export_enable"]):
        blocked_reasons.append("reserve_floor_reached")

    entries = []
    soc_ceiling = round(_clamp(100.0 - safe_float(config.get("direct_marketing_keep_headroom_pct"), 20.0), 0.0, 100.0), 1)
    target_soc_limit = round(_clamp(safe_float(target_soc, 100.0), 0.0, 100.0), 1)
    if target_soc_limit <= 0.1:
        target_soc_limit = 100.0
    negative_charge_target = round(
        _clamp(safe_float(config.get("direct_marketing_negative_price_charge_target_soc_pct"), 80.0), 0.0, 100.0),
        1,
    )

    for slot in annotated:
        if slot["is_low"]:
            if slot["is_negative"]:
                low_reason = "negative_price"
            elif slot.get("is_threshold_low"):
                low_reason = "threshold_below_eeg"
            else:
                low_reason = "low_price"
            soft_threshold = bool(slot.get("is_threshold_soft"))
            export_constraint = _slot_export_constraint(slot, flags)
            headroom_required = bool(export_constraint["hard_export_limit_active"])
            if mode == "arbitrage":
                entries.append(_new_slot_action(
                    slot,
                    "arbitrage_grid_charge_candidate",
                    "profitable_low_price",
                    {
                        "max_power_w": int(flags["max_grid_charge_w"]),
                        "target_soc_pct": negative_charge_target if slot["is_negative"] else soc_ceiling,
                        "soc_ceiling_pct": soc_ceiling,
                        **export_constraint,
                    },
                ))
            elif mode in {"eco", "eco_plus"}:
                pv_store_profit_ok = bool(
                    economics.get("pv_shift_profit_ok")
                    or slot["is_negative"]
                    or slot.get("is_threshold_low")
                    or headroom_required
                )
                if not flags["pv_store_enable"] or not slot.get("is_pv_store") or not pv_store_profit_ok:
                    continue
                target_soc = max(target_soc_limit, negative_charge_target) if slot["is_negative"] else target_soc_limit
                if soft_threshold:
                    target_soc = min(target_soc, soc_ceiling)
                max_power_w = int(flags["pv_store_max_w"])
                if max_power_w <= 0:
                    max_power_w = int(max(0.0, safe_float(config.get("maximumladeleistung"), 0.0)))
                extra = {
                    "curtailment_allowed": bool(export_constraint["hard_export_limit_active"]),
                    "curtail_export_limit_w": (
                        int(export_constraint["hard_export_limit_w"])
                        if export_constraint["hard_export_limit_active"]
                        else 0
                    ),
                    **export_constraint,
                    "economic_basis": "pv_shift",
                    "storage_action": "pv_only_charge",
                    "target_soc_pct": target_soc,
                    "pv_store_price_class": "negative_price" if slot["is_negative"] else ("eeg_soft" if soft_threshold else "low_price"),
                    "pv_store_soft_threshold": soft_threshold,
                    "pv_store_min_surplus_w": int(flags["pv_store_min_surplus_w"]),
                    "pv_store_import_guard_w": int(flags["pv_store_import_guard_w"]),
                    "pv_store_min_hold_s": int(flags["pv_store_min_hold_s"]),
                    "pv_store_ramp_step_w": int(flags["pv_store_ramp_step_w"]),
                    "pv_store_dc_only_enable": bool(flags["pv_store_dc_only_enable"]),
                    "pv_store_external_ac_guard_w": int(flags["pv_store_external_ac_guard_w"]),
                    "pv_store_export_limit_guard_w": int(flags["pv_store_export_limit_guard_w"]),
                    "pv_store_export_limit_ramp_bypass_w": int(flags["pv_store_export_limit_ramp_bypass_w"]),
                    "pv_store_threshold_source": flags.get("pv_store_threshold_source"),
                    "expected_profit_ct_per_kwh": economics.get("pv_shift_spread_ct_per_kwh"),
                }
                if max_power_w > 0:
                    extra["max_power_w"] = max_power_w
                if flags.get("pv_store_threshold_ct") is not None:
                    extra["pv_store_threshold_ct"] = round(safe_float(flags.get("pv_store_threshold_ct"), 0.0), 3)
                if headroom_required:
                    extra["soc_ceiling_pct"] = soc_ceiling
                    extra["headroom_limited"] = True
                entries.append(_new_slot_action(slot, "eco_plus_store_pv_candidate", low_reason, extra))
            elif export_constraint["hard_export_limit_active"]:
                extra = {
                    "soc_ceiling_pct": soc_ceiling,
                    "curtailment_allowed": True,
                    "curtail_export_limit_w": int(export_constraint["hard_export_limit_w"]),
                    **export_constraint,
                }
                entries.append(_new_slot_action(slot, "keep_headroom", low_reason, extra))

        if not slot["is_high"]:
            continue
        if reserve.get("available_export_soc_pct", 0.0) <= 1.0:
            continue

        if mode == "safe":
            entries.append(_new_slot_action(
                slot,
                "safe_house_supply",
                "high_price_house_supply",
                {"max_power_w": 0},
            ))
        elif mode == "eco":
            entries.append(_new_slot_action(
                slot,
                "eco_house_supply",
                "high_price_house_supply",
                {"max_power_w": 0},
            ))
        elif mode == "eco_plus":
            if flags["export_enable"] and flags["max_export_w"] > 0.0:
                entries.append(_new_slot_action(
                    slot,
                    "eco_plus_export_candidate",
                    "profitable_high_price",
                    {
                        "max_power_w": int(flags["max_export_w"]),
                        "economic_basis": "pv_shift",
                        "reserve_floor_soc_pct": reserve.get("effective_min_soc_pct"),
                    },
                ))
            else:
                entries.append(_new_slot_action(
                    slot,
                    "eco_plus_house_supply",
                    "high_price_house_supply",
                    {"max_power_w": 0},
                ))
        elif mode == "arbitrage":
            entries.append(_new_slot_action(
                slot,
                "arbitrage_export_candidate",
                "profitable_high_price",
                {"max_power_w": int(flags["max_export_w"])},
            ))

    entries, negative_headroom_count = _apply_negative_headroom_holds(
        entries,
        annotated,
        reserve,
        capacity_wh,
        flags,
        config,
        now_ms,
        soc_ceiling,
    )
    if negative_headroom_count > 0:
        blocked_reasons.append("pv_store_headroom_prioritized")
        blocked_reasons.append("negative_price_headroom_prioritized")

    efficiency = safe_float(config.get("direct_marketing_roundtrip_efficiency_pct"), 85.0) / 100.0
    entries, prioritized_limited = _prioritize_export_entries(
        entries,
        reserve,
        capacity_wh,
        flags,
        efficiency,
        annotated=annotated,
    )
    entries, pv_store_budget_limited = _apply_pv_store_energy_budget(
        entries,
        reserve,
        capacity_wh,
        flags,
        efficiency,
        current_soc,
    )
    if pv_store_budget_limited > 0:
        blocked_reasons.append("pv_store_energy_budget_prioritized")
        entries, reprioritized_limited = _prioritize_export_entries(
            entries,
            reserve,
            capacity_wh,
            flags,
            efficiency,
            annotated=annotated,
        )
        prioritized_limited += reprioritized_limited
        entries, second_budget_limited = _apply_pv_store_energy_budget(
            entries,
            reserve,
            capacity_wh,
            flags,
            efficiency,
            current_soc,
        )
        if second_budget_limited > 0:
            pv_store_budget_limited += second_budget_limited
    if prioritized_limited > 0:
        blocked_reasons.append("export_energy_prioritized")

    windows = _group_windows(entries)
    if not windows and not blocked_reasons:
        blocked_reasons.append("no_candidate_windows")

    active_actions = {str(window.get("action") or "") for window in windows}
    eco_pv_store_commands = bool(
        mode in {"eco", "eco_plus"}
        and flags["pv_store_enable"]
        and "eco_plus_store_pv_candidate" in active_actions
    )
    eco_plus_export_commands = bool(
        mode == "eco_plus"
        and flags["export_enable"]
        and flags["max_export_w"] > 0.0
        and "eco_plus_export_candidate" in active_actions
    )
    eco_negative_headroom_commands = bool(
        mode in {"eco", "eco_plus"}
        and flags["pv_store_enable"]
        and flags["negative_headroom_enable"]
        and (flags["negative_price_no_export"] or flags.get("low_price_headroom_enable"))
        and "eco_plus_negative_headroom_hold" in active_actions
    )
    eco_commands = bool(eco_pv_store_commands or eco_plus_export_commands or eco_negative_headroom_commands)
    arbitrage_commands = bool(
        mode == "arbitrage"
        and flags["arbitrage_enable"]
        and flags["export_enable"]
        and flags["max_export_w"] > 0.0
        and flags["grid_charge_enable"]
        and flags["max_grid_charge_w"] > 0.0
        and (not flags["eeg_enable"] or flags["eeg_grid_export_risk_ack"])
        and economics.get("grid_profit_ok")
        and active_actions.intersection({
            "arbitrage_grid_charge_candidate",
            "arbitrage_export_candidate",
        })
    )
    flags["commands_allowed"] = bool(eco_commands or arbitrage_commands)
    if not flags["live_soc_valid"]:
        flags["commands_allowed"] = False

    valid_until_ts = min((w["end_ts"] for w in windows), default=now_ms + SLOT_MS)
    policy_timeline = _build_policy_timeline(
        config,
        annotated,
        windows,
        reserve,
        flags,
        economics,
        mode,
        now_ms,
        current_soc,
        capacity_wh,
        blocked_reasons,
        target_timeline=target_timeline,
        forecast_timeline=raw_slots,
    )
    policy_decision = next(
        (
            item for item in policy_timeline
            if safe_float(item.get("start_ts"), 0.0) <= now_ms < safe_float(item.get("end_ts"), 0.0)
        ),
        None,
    )
    if policy_decision is None:
        policy_decision = _build_policy_decision(
            config,
            annotated,
            windows,
            reserve,
            flags,
            economics,
            mode,
            now_ms,
            current_soc,
            capacity_wh,
            blocked_reasons,
        )

    return {
        "active": bool(windows),
        "shadow": not bool(flags["commands_allowed"]),
        "mode": mode,
        "owner_contract_version": OWNER_CONTRACT_VERSION,
        "plan_owner": "direct_marketing:%s" % mode,
        "controller_owner": "storage_manager",
        "reason": "candidate_windows" if windows else "blocked",
        "created_ts": int(now_ms),
        "valid_until_ts": int(valid_until_ts),
        "windows": windows,
        "reserve": reserve,
        "economics": economics,
        "flags": flags,
        "blocked_reasons": sorted(set(blocked_reasons)),
        "price_quality": {
            "excluded_slot_count": len(price_quality_blockers),
            "blocker_counts": price_quality_blocker_counts,
            "current_blockers": sorted(set(current_price_quality_blockers)),
            "unpriced_tail_slot_count": unpriced_tail_slot_count,
            "unpriced_tail_blocker_counts": unpriced_tail_blocker_counts,
            "last_valid_price_ts": int(last_valid_price_ts) if last_valid_price_ts > 0 else None,
        },
        "policy_decision": policy_decision,
        "policy_timeline": policy_timeline,
    }
