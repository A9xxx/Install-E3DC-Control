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

import hashlib
import json
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
    e3dc_pv_w = _slot_power(slot, ("e3dc_pv_w", "pv_dc_w", "E3DC_PV_Power"))
    external_pv_w = _slot_power(slot, ("external_pv_w", "ext_pv_w", "Ext_PV_Power"))
    has_e3dc_pv_forecast = _slot_power_present(slot, ("e3dc_pv_w", "pv_dc_w", "E3DC_PV_Power"))
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
    controlled_load_w = home_w + wp_w + heater_w + wallbox_w
    dc_surplus_w = max(0.0, e3dc_pv_w - controlled_load_w) if has_e3dc_pv_forecast else 0.0
    return {
        "pv_w": pv_w,
        "e3dc_pv_w": e3dc_pv_w,
        "external_pv_w": external_pv_w,
        "has_e3dc_pv_forecast": has_e3dc_pv_forecast,
        "home_w": home_w,
        "wp_w": wp_w,
        "heater_w": heater_w,
        "wallbox_w": wallbox_w,
        "forecast_surplus_w": max(0.0, surplus_w),
        "forecast_dc_surplus_w": dc_surplus_w,
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
    return _net_sell_components(market_ct, config)["net_sell_ct"]


def _input_vat_multiplier(config):
    vat_pct = _clamp(safe_float((config or {}).get("direct_marketing_service_vat_pct"), 19.0), 0.0, 100.0)
    recoverable = cfg_bool((config or {}).get("direct_marketing_input_vat_recoverable"), False)
    return 1.0 if recoverable else 1.0 + (vat_pct / 100.0)


def _eeg_compensation_rate_ct(config):
    tiers = _parse_eeg_tariff_tiers((config or {}).get("direct_marketing_eeg_tariff_tiers"))
    if not tiers:
        return None
    return _weighted_eeg_rate_ct(tiers, _configured_pv_capacity_kwp(config))


def _variable_fee_basis(config, gross_sell_ct):
    mode = str((config or {}).get("direct_marketing_variable_fee_basis", "sell_revenue") or "sell_revenue")
    mode = mode.strip().lower().replace("-", "_")
    if mode in {"manual", "manual_ct", "manual_ct_per_kwh"}:
        manual_ct = _configured_optional_float(config, "direct_marketing_variable_fee_basis_ct_per_kwh")
        return "manual", max(0.0, safe_float(manual_ct, 0.0)), manual_ct is not None
    if mode in {"eeg", "eeg_compensation", "market_premium"}:
        eeg_ct = _eeg_compensation_rate_ct(config)
        return "eeg_compensation", max(0.0, safe_float(eeg_ct, 0.0)), eeg_ct is not None
    if mode in {"sell_revenue", "sales_revenue", "export_revenue"}:
        return "sell_revenue", max(0.0, gross_sell_ct), True
    return mode or "unknown", 0.0, False


def _net_sell_components(market_ct, config):
    config = config or {}
    revenue_offset = safe_float(config.get("direct_marketing_revenue_offset_ct"), 0.0)
    fee_ct_net = max(0.0, safe_float(config.get("direct_marketing_fee_ct_per_kwh"), 0.0))
    fee_pct = max(0.0, safe_float(config.get("direct_marketing_fee_pct"), 0.0))
    gross = safe_float(market_ct, 0.0) + revenue_offset
    fee_basis_mode, fee_basis_ct, fee_basis_valid = _variable_fee_basis(config, gross)
    variable_fee_net_ct = fee_basis_ct * fee_pct / 100.0
    vat_multiplier = _input_vat_multiplier(config)
    fee_cost_ct = (fee_ct_net + variable_fee_net_ct) * vat_multiplier
    return {
        "gross_sell_ct": round(gross, 6),
        "net_sell_ct": round(gross - fee_cost_ct, 6),
        "fee_basis": fee_basis_mode,
        "fee_basis_ct": round(fee_basis_ct, 6),
        "fee_basis_valid": bool(fee_basis_valid),
        "fee_pct": round(fee_pct, 6),
        "fixed_fee_net_ct": round(fee_ct_net, 6),
        "variable_fee_net_ct": round(variable_fee_net_ct, 6),
        "fee_cost_ct": round(fee_cost_ct, 6),
        "service_vat_pct": round(_clamp(safe_float(config.get("direct_marketing_service_vat_pct"), 19.0), 0.0, 100.0), 3),
        "input_vat_recoverable": cfg_bool(config.get("direct_marketing_input_vat_recoverable"), False),
    }


def _settlement_accounting(config):
    config = config or {}
    capacity_override = _configured_optional_float(config, "direct_marketing_installed_kwp")
    forecast_capacity_kwp = _configured_pv_capacity_kwp(config)
    installed_kwp = max(
        0.0,
        safe_float(capacity_override, forecast_capacity_kwp)
        if capacity_override is not None and capacity_override > 0.0
        else forecast_capacity_kwp,
    )
    monthly_fee_net = max(0.0, safe_float(config.get("direct_marketing_monthly_fee_eur"), 0.0))
    balancing_estimate_per_kwp = max(
        0.0,
        safe_float(config.get("direct_marketing_balancing_cost_eur_per_kwp_month"), 0.0),
    )
    balancing_actual_per_kwp = _configured_optional_float(
        config,
        "direct_marketing_balancing_cost_actual_eur_per_kwp_month",
    )
    balancing_estimate_net = installed_kwp * balancing_estimate_per_kwp
    balancing_actual_net = (
        installed_kwp * max(0.0, safe_float(balancing_actual_per_kwp, 0.0))
        if balancing_actual_per_kwp is not None
        else None
    )
    vat_multiplier = _input_vat_multiplier(config)
    sample = _net_sell_components(0.0, config)
    return {
        "schema": "direct_marketing_settlement_v1",
        "variable_fee": {
            "percent": sample["fee_pct"],
            "basis": sample["fee_basis"],
            "basis_ct_per_kwh": sample["fee_basis_ct"] if sample["fee_basis"] != "sell_revenue" else None,
            "basis_valid": sample["fee_basis_valid"],
            "fixed_net_ct_per_kwh": sample["fixed_fee_net_ct"],
            "marginal_policy_cost": True,
        },
        "tax": {
            "service_vat_pct": sample["service_vat_pct"],
            "input_vat_recoverable": sample["input_vat_recoverable"],
            "cost_multiplier": round(vat_multiplier, 6),
        },
        "installed_capacity_kwp": round(installed_kwp, 3),
        "monthly_service_fee": {
            "net_eur": round(monthly_fee_net, 2),
            "effective_eur": round(monthly_fee_net * vat_multiplier, 2),
        },
        "balancing_cost": {
            "estimate_eur_per_kwp_month": round(balancing_estimate_per_kwp, 4),
            "estimate_net_eur": round(balancing_estimate_net, 2),
            "estimate_effective_eur": round(balancing_estimate_net * vat_multiplier, 2),
            "actual_eur_per_kwp_month": round(max(0.0, balancing_actual_per_kwp), 4) if balancing_actual_per_kwp is not None else None,
            "actual_net_eur": round(balancing_actual_net, 2) if balancing_actual_net is not None else None,
            "actual_effective_eur": round(balancing_actual_net * vat_multiplier, 2) if balancing_actual_net is not None else None,
            "policy_effect": "diagnostic_only",
        },
        "monthly_fixed_cost_estimate": {
            "net_eur": round(monthly_fee_net + balancing_estimate_net, 2),
            "effective_eur": round((monthly_fee_net + balancing_estimate_net) * vat_multiplier, 2),
            "marginal_policy_cost": False,
        },
        "market_value_solar_role": "monitor_and_reconciliation_only",
    }


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
    """Klassifiziert PV-Export unabhängig von der Speicherladepriorität.

    Ein negativer Rohmarktpreis kennzeichnet das Marktfenster, ist für sich
    allein aber kein Exportveto. Der Aktorvertrag folgt dem marginalen Nettoerlös
    nach konfiguriertem Abrechnungsoffset und variablen Gebühren.
    """

    is_negative = bool((slot or {}).get("is_negative"))
    negative_policy_enabled = bool(
        is_negative
        and (
            cfg_bool((flags or {}).get("negative_price_no_export"), True)
            or cfg_bool((flags or {}).get("low_price_curtail_enable"), False)
        )
    )
    net_sell_ct = safe_float((slot or {}).get("net_sell_ct"), float("nan"))
    settlement_valid = bool((slot or {}).get("fee_basis_valid") is True and math.isfinite(net_sell_ct))
    economic_release = bool(is_negative and settlement_valid and net_sell_ct > 0.0)
    # Die aktuelle Produktclosure kann den autonomen Zusatz-WR-/Shelly-Pfad nicht
    # als Consumer derselben Grenzerlösfreigabe binden. Bis beide Aktorpfade
    # typisiert geschlossen sind, bleibt der bestehende Rohnegativpreis-Veto
    # deshalb hardwarewirksam. Das Marktfenster und der positive Grenzerlös
    # bleiben davon getrennt sichtbar.
    actuator_field_ready = False
    negative_hard = bool(
        negative_policy_enabled
        and (not economic_release or not actuator_field_ready)
    )
    if negative_hard:
        limit_w = (
            max(0, int(round(safe_float((flags or {}).get("low_price_curtail_limit_w"), 0.0))))
            if cfg_bool((flags or {}).get("low_price_curtail_enable"), False)
            else 0
        )
        if not settlement_valid:
            marginal_classification = "negative_margin_invalid_hard"
        elif net_sell_ct <= 0.0:
            marginal_classification = "negative_net_revenue_hard"
        else:
            marginal_classification = "negative_net_positive_allowed"
        return {
            "export_constraint_class": "negative_hard",
            "marginal_export_class": marginal_classification,
            "hard_export_limit_active": True,
            "hard_export_limit_w": limit_w,
            "export_constraint_scope": "grid_connection",
            "pv_export_allowed": False,
            "export_constraint_enforcement": "requested",
            "export_constraint_execution_owner": "external_e3dc_luox",
            "marginal_net_sell_ct": round(net_sell_ct, 6) if settlement_valid else None,
            "marginal_settlement_valid": settlement_valid,
            "actuator_closure": {
                "schema": "direct_marketing_export_actuator_closure_v1",
                "required_paths": ["e3dc_luox", "aux_inverter_shelly"],
                "economic_release": economic_release,
                "field_ready": actuator_field_ready,
                "reason": (
                    "both_consumers_not_runtime_bound"
                    if economic_release
                    else ("net_revenue_nonpositive" if settlement_valid else "settlement_contract_invalid")
                ),
            },
        }

    if is_negative:
        classification = "negative_allowed"
    elif bool((slot or {}).get("is_threshold_soft")):
        classification = "eeg_soft"
    else:
        classification = "low_price_soft"
    return {
        "export_constraint_class": classification,
        "marginal_export_class": "negative_net_positive_allowed" if is_negative else classification,
        "hard_export_limit_active": False,
        "hard_export_limit_w": None,
        "export_constraint_scope": "storage_priority",
        "pv_export_allowed": True,
        "export_constraint_enforcement": "storage_priority",
        "export_constraint_execution_owner": "storage_manager",
        "marginal_net_sell_ct": round(net_sell_ct, 6) if settlement_valid else None,
        "marginal_settlement_valid": settlement_valid,
        "actuator_closure": {
            "schema": "direct_marketing_export_actuator_closure_v1",
            "required_paths": ["e3dc_luox", "aux_inverter_shelly"],
            "economic_release": bool(is_negative and settlement_valid and net_sell_ct > 0.0),
            "field_ready": False,
            "reason": "both_consumers_not_runtime_bound" if is_negative else "outside_negative_market_window",
        },
    }


def _market_window_digest(slots):
    material = [
        {
            "start_ts": int(slot["ts"]),
            "end_ts": int(slot["end_ts"]),
            "market_ct": round(safe_float(slot.get("market_ct"), 0.0), 6),
            "source": str(slot.get("market_price_source") or ""),
            "resolution_min": safe_int(slot.get("market_price_resolution_min"), 0),
        }
        for slot in slots
    ]
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _build_negative_price_market_windows(annotated, previous_market_windows=None):
    """Erstellt stabile Rohpreisfenster unabhängig von jeder Speicheraktion."""

    grouped = []
    for slot in sorted(
        (item for item in annotated if item.get("is_negative")),
        key=lambda item: int(item.get("ts", 0)),
    ):
        if not grouped or int(grouped[-1][-1]["end_ts"]) != int(slot["ts"]):
            grouped.append([slot])
        else:
            grouped[-1].append(slot)

    previous = [item for item in (previous_market_windows or []) if isinstance(item, dict)]
    windows = []
    for slots in grouped:
        start_ts = int(slots[0]["ts"])
        end_ts = int(slots[-1]["end_ts"])
        digest = _market_window_digest(slots)
        stable_start = start_ts
        stable_id = None
        matched_old = None
        for old in previous:
            old_start = safe_int(old.get("start_ts"), 0)
            old_end = safe_int(old.get("end_ts"), 0)
            old_slots = old.get("slot_prices") if isinstance(old.get("slot_prices"), list) else []
            old_by_ts = {
                safe_int(item.get("start_ts"), 0): round(safe_float(item.get("market_ct"), 0.0), 6)
                for item in old_slots if isinstance(item, dict)
            }
            suffix_matches = bool(old_by_ts) and all(
                old_by_ts.get(int(slot["ts"])) == round(safe_float(slot.get("market_ct"), 0.0), 6)
                for slot in slots
            )
            if old_start <= start_ts < old_end and old_end == end_ts and suffix_matches:
                stable_start = old_start
                stable_id = str(old.get("market_window_id") or "") or None
                matched_old = old
                break
        if stable_id is None:
            stable_id = "market:negative_price:%d:%d:%s" % (stable_start, end_ts, digest[:16])

        valid_values = [
            safe_float(slot.get("net_sell_ct"), float("nan"))
            for slot in slots
            if slot.get("fee_basis_valid") is True
            and math.isfinite(safe_float(slot.get("net_sell_ct"), float("nan")))
        ]
        invalid_count = len(slots) - len(valid_values)
        positive_count = sum(1 for value in valid_values if value > 0.0)
        nonpositive_count = sum(1 for value in valid_values if value <= 0.0)
        if invalid_count:
            margin_class = "invalid" if not valid_values else "mixed_invalid"
        elif positive_count and nonpositive_count:
            margin_class = "mixed"
        elif positive_count:
            margin_class = "positive"
        else:
            margin_class = "nonpositive"
        window = {
            "schema": "direct_marketing_negative_market_window_v1",
            "market_window_id": stable_id,
            "window_id": stable_id,
            "action": "negative_price_market_window",
            "reason": "raw_market_price_negative",
            "start_ts": stable_start,
            "end_ts": end_ts,
            "start_t": _format_t(stable_start),
            "end_t": _format_t(end_ts),
            "slot_count": len(slots),
            "observed_start_ts": start_ts,
            "observed_slot_count": len(slots),
            "price_revision_sha256": digest,
            "min_market_ct": round(min(safe_float(slot.get("market_ct"), 0.0) for slot in slots), 3),
            "max_market_ct": round(max(safe_float(slot.get("market_ct"), 0.0) for slot in slots), 3),
            "avg_market_ct": round(sum(safe_float(slot.get("market_ct"), 0.0) for slot in slots) / len(slots), 3),
            "min_net_sell_ct": round(min(valid_values), 3) if valid_values else None,
            "max_net_sell_ct": round(max(valid_values), 3) if valid_values else None,
            "margin_class": margin_class,
            "positive_margin_slot_count": positive_count,
            "nonpositive_margin_slot_count": nonpositive_count,
            "invalid_margin_slot_count": invalid_count,
            "market_window_only": True,
            "planned_power_w": 0,
            "soc_effect": False,
            "action_overlay_separate": True,
            "slot_prices": [
                {"start_ts": int(slot["ts"]), "market_ct": round(safe_float(slot.get("market_ct"), 0.0), 6)}
                for slot in slots
            ],
        }
        if isinstance(matched_old, dict):
            # Die Preisrevision ist unverändert; nur der Replan-Horizont hat
            # bereits vergangene Slots abgeschnitten. Vollständige semantische
            # Fenstergrenzen und Aggregationen bleiben revisionsstabil.
            for key in (
                "slot_count",
                "price_revision_sha256",
                "min_market_ct",
                "max_market_ct",
                "avg_market_ct",
                "min_net_sell_ct",
                "max_net_sell_ct",
                "margin_class",
                "positive_margin_slot_count",
                "nonpositive_margin_slot_count",
                "invalid_margin_slot_count",
                "slot_prices",
            ):
                if key in matched_old:
                    window[key] = matched_old[key]
        windows.append(window)
        for slot in slots:
            slot.update({
                "market_window_id": stable_id,
                "market_window_start_ts": stable_start,
                "market_window_end_ts": end_ts,
                "market_window_margin_class": margin_class,
                "market_margin_class": (
                    "invalid" if slot.get("fee_basis_valid") is not True
                    else ("positive" if safe_float(slot.get("net_sell_ct"), 0.0) > 0.0 else "nonpositive")
                ),
            })
    return windows


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
        "min_window_profit_eur": max(0.0, safe_float(config.get("direct_marketing_min_window_profit_eur"), 0.25)),
        "min_export_energy_kwh": max(0.0, safe_float(config.get("direct_marketing_min_export_energy_kwh"), 1.5)),
        "min_export_window_min": max(15.0, safe_float(config.get("direct_marketing_min_export_window_min"), 15.0)),
        "preferred_export_plateau_min": max(
            15.0,
            safe_float(config.get("direct_marketing_preferred_export_plateau_min"), 60.0),
        ),
        "price_plateau_tolerance_ct": _clamp(
            safe_float(config.get("direct_marketing_price_plateau_tolerance_ct"), 0.75),
            0.0,
            20.0,
        ),
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


def _annotate_export_price_plateaus(annotated, config):
    """Expand high-price seeds into calm, adjacent net-sell plateaus."""

    ordered = sorted((dict(slot) for slot in (annotated or [])), key=lambda item: safe_float(item.get("ts"), 0.0))
    if not ordered:
        return ordered
    thresholds = _policy_thresholds(config)
    tolerance_ct = safe_float(thresholds.get("price_plateau_tolerance_ct"), 0.75)
    preferred_min = safe_float(thresholds.get("preferred_export_plateau_min"), 60.0)
    selected = set()
    for seed_idx, seed in enumerate(ordered):
        if not seed.get("is_high") or seed.get("is_low") or seed.get("is_negative"):
            continue
        seed_price = safe_float(seed.get("net_sell_ct"), safe_float(seed.get("market_ct"), 0.0))
        selected.add(seed_idx)
        for direction in (-1, 1):
            idx = seed_idx + direction
            previous_idx = seed_idx
            while 0 <= idx < len(ordered):
                candidate = ordered[idx]
                previous = ordered[previous_idx]
                if candidate.get("is_low") or candidate.get("is_negative"):
                    break
                if direction < 0:
                    contiguous = abs(safe_float(candidate.get("end_ts"), 0.0) - safe_float(previous.get("ts"), 0.0)) <= 1000.0
                else:
                    contiguous = abs(safe_float(previous.get("end_ts"), 0.0) - safe_float(candidate.get("ts"), 0.0)) <= 1000.0
                if not contiguous:
                    break
                candidate_price = safe_float(candidate.get("net_sell_ct"), safe_float(candidate.get("market_ct"), 0.0))
                if candidate_price + 0.000001 < seed_price - tolerance_ct:
                    break
                selected.add(idx)
                previous_idx = idx
                idx += direction

    groups = []
    current = []
    for idx in sorted(selected):
        candidate_price = safe_float(ordered[idx].get("net_sell_ct"), safe_float(ordered[idx].get("market_ct"), 0.0))
        current_prices = [
            safe_float(ordered[item].get("net_sell_ct"), safe_float(ordered[item].get("market_ct"), 0.0))
            for item in current
        ]
        price_range_too_wide = bool(
            current_prices
            and max(current_prices + [candidate_price]) - min(current_prices + [candidate_price]) > tolerance_ct + 0.000001
        )
        if current and (idx != current[-1] + 1 or price_range_too_wide):
            groups.append(current)
            current = []
        current.append(idx)
    if current:
        groups.append(current)

    for group in groups:
        start_ts = int(safe_float(ordered[group[0]].get("ts"), 0.0))
        end_ts = int(safe_float(ordered[group[-1]].get("end_ts"), start_ts + SLOT_MS))
        duration_min = max(0.0, end_ts - start_ts) / 60000.0
        prices = [safe_float(ordered[idx].get("net_sell_ct"), safe_float(ordered[idx].get("market_ct"), 0.0)) for idx in group]
        peak_ct = max(prices)
        plateau_id = "export:%d:%d" % (end_ts, int(round(peak_ct * 100.0)))
        for idx in group:
            ordered[idx].update({
                "is_export_plateau": True,
                "export_plateau_id": plateau_id,
                "export_plateau_origin_start_ts": start_ts,
                "export_plateau_end_ts": end_ts,
                "export_plateau_duration_min": round(duration_min, 1),
                "export_plateau_peak_net_sell_ct": round(peak_ct, 3),
                "export_plateau_tolerance_ct": round(tolerance_ct, 3),
                "export_plateau_preferred_min": round(preferred_min, 1),
                "export_plateau_preferred_met": duration_min + 0.000001 >= preferred_min,
            })
    for idx, slot in enumerate(ordered):
        slot.setdefault("is_export_plateau", idx in selected)
    return ordered


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


def _policy_export_economics(config, window, economics, profile, sellable_wh, capacity_wh=0.0, annotated=None):
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
    opportunity_cost_source = "configured"
    if opportunity_cost_ct is None:
        next_recharge_ts = safe_float(window.get("export_segment_next_recharge_ts"), 0.0)
        recharge_slots = []
        if next_recharge_ts > 0.0:
            collecting = False
            previous_end_ts = 0.0
            for slot in sorted(annotated or [], key=lambda item: safe_float(item.get("ts"), 0.0)):
                start_ts = safe_float(slot.get("ts"), 0.0)
                end_ts = safe_float(slot.get("end_ts"), start_ts + SLOT_MS)
                if not collecting and abs(start_ts - next_recharge_ts) <= 1000.0:
                    collecting = True
                if not collecting:
                    continue
                if previous_end_ts > 0.0 and abs(start_ts - previous_end_ts) > 1000.0:
                    break
                if not slot.get("is_pv_store"):
                    break
                duration_h = max(0.0, end_ts - start_ts) / 3600000.0
                forecast_wh = max(0.0, safe_float(slot.get("forecast_surplus_w"), 0.0)) * duration_h
                weight = forecast_wh if forecast_wh > 0.0 else duration_h
                net_revenue_ct = safe_float(slot.get("net_sell_ct"), 0.0)
                gross_revenue_ct = safe_float(slot.get("gross_sell_ct"), net_revenue_ct)
                # Avoided Vermarktungsgebühren werden konservativ nicht als
                # zusätzlicher Batterie-Arbitragegewinn gutgeschrieben.
                recharge_slots.append((max(net_revenue_ct, gross_revenue_ct), weight))
                previous_end_ts = end_ts
        if recharge_slots and sum(weight for _value, weight in recharge_slots) > 0.0:
            opportunity_cost_ct = sum(value * weight for value, weight in recharge_slots) / sum(
                weight for _value, weight in recharge_slots
            )
            opportunity_cost_source = "next_recharge_window"
        else:
            opportunity_cost_ct = safe_float(economics.get("pv_shift_opportunity_ct"), 0.0)
            opportunity_cost_source = "global_fallback"
    efficiency_pct = _clamp(
        safe_float(config.get("direct_marketing_roundtrip_efficiency_pct"), 85.0),
        1.0,
        100.0,
    )
    efficiency = efficiency_pct / 100.0
    efficiency_loss_ct = max(0.0, sell_net_ct * (1.0 - efficiency))
    base_lcos_ct = _policy_lcos_ct(config, profile)
    discharge_depth_pct = (
        planned_wh / max(1.0, safe_float(capacity_wh, 0.0)) * 100.0
        if safe_float(capacity_wh, 0.0) > 0.0
        else 0.0
    )
    deep_cycle_threshold_pct = _clamp(
        safe_float(config.get("direct_marketing_deep_cycle_threshold_pct"), 20.0),
        0.0,
        100.0,
    )
    deep_cycle_lcos_factor = _clamp(
        safe_float(config.get("direct_marketing_deep_cycle_lcos_factor"), 0.5),
        0.0,
        5.0,
    )
    deep_cycle_excess = max(0.0, discharge_depth_pct - deep_cycle_threshold_pct)
    depth_lcos_ct = base_lcos_ct * deep_cycle_lcos_factor * (deep_cycle_excess / 100.0)
    lcos_ct = base_lcos_ct + depth_lcos_ct
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
        "sell_gross_ct_kwh": round(
            safe_float(window.get("avg_gross_sell_ct"), safe_float(window.get("gross_sell_ct"), sell_net_ct)),
            3,
        ),
        "marketing_fee_cost_ct_kwh": round(
            max(0.0, safe_float(window.get("avg_fee_cost_ct"), safe_float(window.get("fee_cost_ct"), 0.0))),
            3,
        ),
        "marketing_fee_basis": window.get("fee_basis"),
        "marketing_fee_basis_ct_kwh": window.get("fee_basis_ct"),
        "marketing_fee_pct": window.get("fee_pct"),
        "opportunity_cost_ct": round(opportunity_cost_ct, 3),
        "opportunity_cost_source": opportunity_cost_source,
        "next_recharge_ts": int(safe_float(window.get("export_segment_next_recharge_ts"), 0.0)) or None,
        "efficiency_loss_ct": round(efficiency_loss_ct, 3),
        "base_lcos_ct": round(base_lcos_ct, 3),
        "depth_lcos_ct": round(depth_lcos_ct, 3),
        "lcos_ct": round(lcos_ct, 3),
        "planned_discharge_depth_pct": round(discharge_depth_pct, 2),
        "deep_cycle_threshold_pct": round(deep_cycle_threshold_pct, 1),
        "deep_cycle_lcos_factor": round(deep_cycle_lcos_factor, 2),
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
    if profit + 0.000001 < safe_float(export_economics.get("min_window_profit_eur"), 0.25):
        return False, "Blocked by Margin: Expected Profit %.2f EUR < Threshold %.2f EUR" % (
            profit,
            safe_float(export_economics.get("min_window_profit_eur"), 0.25),
        )
    if safe_float(export_economics.get("export_energy_kwh"), 0.0) + 0.000001 < safe_float(
        export_economics.get("min_export_energy_kwh"),
        1.5,
    ):
        return False, "Blocked by Energy: %.2f kWh < Threshold %.2f kWh" % (
            safe_float(export_economics.get("export_energy_kwh"), 0.0),
            safe_float(export_economics.get("min_export_energy_kwh"), 1.5),
        )
    if safe_float(export_economics.get("duration_min"), 0.0) + 0.000001 < safe_float(
        export_economics.get("min_export_window_min"),
        15.0,
    ):
        return False, "Blocked by Duration: %.1f min < Threshold %.1f min" % (
            safe_float(export_economics.get("duration_min"), 0.0),
            safe_float(export_economics.get("min_export_window_min"), 15.0),
        )
    return True, "Profit export allowed"


def _policy_export_previous_window_context(previous_policy, export_window, profile, now_ms):
    """Gleicht ein bereits freigegebenes Exportfenster ohne Policy-Persistenz ab."""
    previous = previous_policy if isinstance(previous_policy, dict) else {}
    selected = previous.get("selected_window") if isinstance(previous.get("selected_window"), dict) else {}
    if (
        previous.get("schema") != POLICY_SCHEMA
        or bool(previous.get("blocked"))
        or not cfg_bool(previous.get("commands_allowed"), False)
        or str(previous.get("dv_target_state") or "").strip().upper() != "FORCE_EXPORT"
        or _normalize_profit_profile(previous.get("profit_profile", "standard")) != profile
    ):
        return {"matched": False}

    previous_action = str(selected.get("action") or previous.get("source_action") or "")
    current_action = str((export_window or {}).get("action") or "")
    previous_window_id = str(selected.get("window_id") or previous.get("window_id") or "")
    current_window_id = str((export_window or {}).get("export_plateau_id") or "")
    previous_end_ts = safe_float(selected.get("end_ts", previous.get("end_ts")), 0.0)
    current_end_ts = safe_float((export_window or {}).get("end_ts"), 0.0)
    if (
        previous_action != current_action
        or (previous_window_id and current_window_id and previous_window_id != current_window_id)
        or previous_end_ts <= 0.0
        or current_end_ts <= 0.0
        or abs(previous_end_ts - current_end_ts) > 1000.0
        or safe_float(now_ms, 0.0) >= current_end_ts
    ):
        return {"matched": False}

    origin_start_ts = safe_float(
        previous.get("window_origin_start_ts", selected.get("start_ts", previous.get("start_ts"))),
        0.0,
    )
    if origin_start_ts <= 0.0 or safe_float(now_ms, 0.0) < origin_start_ts:
        return {"matched": False}
    return {
        "matched": True,
        "origin_start_ts": int(origin_start_ts),
        "end_ts": int(current_end_ts),
        "action": current_action,
        "window_id": previous_window_id or current_window_id,
    }


def _build_policy_decision_legacy(
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
    previous_policy_decision=None,
):
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
        protected_limited_deficit_wh = min(
            safe_float(reserve_policy.get("sellable_wh"), 0.0),
            max(0.0, (safe_float(current_soc, 0.0) - headroom_target_soc_pct) / 100.0 * safe_float(capacity_wh, 0.0)),
        )
        additional_headroom_wh = max(
            0.0,
            safe_float(headroom_window.get("negative_headroom_additional_wh"), protected_limited_deficit_wh),
        )
        headroom_deficit_wh = min(protected_limited_deficit_wh, additional_headroom_wh)
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
                "headroom_required_wh": headroom_window.get("negative_headroom_required_wh"),
                "headroom_free_before_wh": headroom_window.get("negative_headroom_free_before_wh"),
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
                "required_headroom_wh": round(safe_float(headroom_window.get("negative_headroom_required_wh"), 0.0), 0),
                "free_headroom_before_wh": round(safe_float(headroom_window.get("negative_headroom_free_before_wh"), 0.0), 0),
                "additional_headroom_wh": round(additional_headroom_wh, 0),
            },
            "selected_window": {
                "action": headroom_window.get("action"),
                "reason": headroom_window.get("reason"),
                "start_ts": headroom_window.get("start_ts"),
                "end_ts": headroom_window.get("end_ts"),
                "next_charge_window_start_ts": headroom_window.get("negative_headroom_next_start_ts"),
                "headroom_export_selected": bool(headroom_window.get("headroom_export_selected")),
                "headroom_export_budget_wh": headroom_window.get("headroom_export_budget_wh"),
                "headroom_required_wh": headroom_window.get("negative_headroom_required_wh"),
                "headroom_free_before_wh": headroom_window.get("negative_headroom_free_before_wh"),
                "headroom_additional_wh": headroom_window.get("negative_headroom_additional_wh"),
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
            capacity_wh,
            annotated,
        )
        allowed, reason = _policy_export_allowed(profile, export_economics)
        previous_window = _policy_export_previous_window_context(
            previous_policy_decision,
            export_window,
            profile,
            now_ms,
        )
        continuation_active = bool(
            previous_window.get("matched")
            and not allowed
            and str(reason).startswith("Blocked by Duration:")
        )
        if continuation_active:
            allowed = True
            reason = "Profit export continuation: startup duration gate already satisfied"
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
            "continuation_active": continuation_active,
            "window_origin_start_ts": int(
                previous_window.get("origin_start_ts")
                if previous_window.get("matched")
                else safe_float(export_window.get("start_ts"), now_ms)
            ),
            "window_id": str(
                previous_window.get("window_id")
                or export_window.get("export_plateau_id")
                or "export:%d" % int(safe_float(export_window.get("end_ts"), now_ms + SLOT_MS))
            ),
            "economics": export_economics,
            "selected_window": {
                "window_id": str(
                    previous_window.get("window_id")
                    or export_window.get("export_plateau_id")
                    or "export:%d" % int(safe_float(export_window.get("end_ts"), now_ms + SLOT_MS))
                ),
                "action": export_window.get("action"),
                "reason": export_window.get("reason"),
                "start_ts": int(
                    previous_window.get("origin_start_ts")
                    if previous_window.get("matched")
                    else safe_float(export_window.get("start_ts"), now_ms)
                ),
                "end_ts": export_window.get("end_ts"),
                "plateau_duration_min": export_window.get("export_plateau_duration_min"),
                "plateau_preferred_min": export_window.get("export_plateau_preferred_min"),
                "plateau_preferred_met": export_window.get("export_plateau_preferred_met"),
                "plateau_tolerance_ct": export_window.get("export_plateau_tolerance_ct"),
                "plateau_dispatch_power_w": export_window.get("plateau_dispatch_power_w"),
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


_POLICY_CANDIDATE_PRIORITY = (
    "eco_plus_negative_headroom_hold",
    "eco_plus_store_pv_candidate",
    "eco_plus_export_candidate",
    "arbitrage_export_candidate",
    "arbitrage_grid_charge_candidate",
    "eco_plus_house_supply",
    "eco_house_supply",
    "safe_house_supply",
)


def _policy_active_candidate_windows(windows, now_ms):
    active = []
    for window in windows or []:
        if not isinstance(window, dict):
            continue
        start_ts = safe_float(window.get("start_ts"), 0.0)
        end_ts = safe_float(window.get("end_ts"), start_ts + SLOT_MS)
        if start_ts <= safe_float(now_ms, 0.0) < end_ts:
            active.append(window)
    priority = {action: index for index, action in enumerate(_POLICY_CANDIDATE_PRIORITY)}
    return sorted(
        active,
        key=lambda item: (
            priority.get(str(item.get("action") or ""), len(priority)),
            safe_float(item.get("start_ts"), 0.0),
            str(item.get("action") or ""),
        ),
    )


def _enrich_policy_candidate_contract(decision, windows, now_ms):
    """Stellt Kandidat-, Auswahl- und Ausführungsrollen ohne Prioritätsänderung bereit."""
    result = dict(decision or {})
    active_windows = _policy_active_candidate_windows(windows, now_ms)
    candidate_actions = []
    for window in active_windows:
        action = str(window.get("action") or "")
        if action and action not in candidate_actions:
            candidate_actions.append(action)

    selected = result.get("selected_window") if isinstance(result.get("selected_window"), dict) else None
    selected_action = str((selected or {}).get("action") or "")
    selected_end_ts = safe_int((selected or {}).get("end_ts"), 0)
    selected_window_id = str(
        (selected or {}).get("window_id")
        or result.get("window_id")
        or ""
    )
    execution_matches = []
    for window in active_windows:
        if str(window.get("action") or "") != selected_action:
            continue
        window_end_ts = safe_int(window.get("end_ts"), 0)
        if selected_end_ts > 0 and window_end_ts != selected_end_ts:
            continue
        window_id = str(
            window.get("export_plateau_id")
            or window.get("window_id")
            or ""
        )
        if selected_window_id and window_id and selected_window_id != window_id:
            continue
        execution_matches.append(window)

    execution_window = None
    if len(execution_matches) == 1:
        plan_window = execution_matches[0]
        execution_window = {
            "contract_version": 1,
            "action": selected_action,
            "start_ts": safe_int(plan_window.get("start_ts"), 0),
            "end_ts": safe_int(plan_window.get("end_ts"), 0),
            "plan_window_start_ts": safe_int(plan_window.get("start_ts"), 0),
            "plan_window_end_ts": safe_int(plan_window.get("end_ts"), 0),
            "origin_start_ts": safe_int(
                result.get("window_origin_start_ts"),
                safe_int((selected or {}).get("start_ts"), 0),
            ),
            "window_id": selected_window_id or str(
                plan_window.get("export_plateau_id")
                or plan_window.get("window_id")
                or ""
            ),
            "source": "active_plan_window",
        }
    result["execution_window"] = execution_window
    result["execution_window_match_count"] = len(execution_matches)
    result["candidate_actions"] = candidate_actions
    result["selected_candidate"] = dict(selected) if selected else None
    result["source_action"] = selected_action or None
    result["suppressed_candidates"] = [
        {
            "action": action,
            "reason": "superseded_by:%s" % selected_action if selected_action else "not_selected",
        }
        for action in candidate_actions
        if action != selected_action
    ]

    target_state = str(result.get("dv_target_state") or "").strip().upper()
    budget = result.get("storage_budget") if isinstance(result.get("storage_budget"), dict) else {}
    execution_required = bool(
        target_state in {"FORCE_EXPORT", "FORCE_CHARGE_PV", "HEADROOM_EXPORT"}
        and result.get("commands_allowed")
        and not result.get("blocked")
        and selected_action
    )
    if execution_required and execution_window is None:
        budget = dict(budget)
        budget["charge_budget_w"] = 0
        budget["export_budget_w"] = 0
        result["storage_budget"] = budget
        result["commands_allowed"] = False
        result["blocked"] = True
        result["execution_contract_block_reason"] = "policy_execution_window_missing_fail_closed"
        previous_reason = str(result.get("block_reason") or "").strip()
        result["block_reason"] = "; ".join(
            part for part in (previous_reason, "policy_execution_window_missing_fail_closed") if part
        )
    executable = bool(result.get("commands_allowed") and not result.get("blocked") and selected_action)
    if target_state == "FORCE_EXPORT":
        executable = executable and safe_float(budget.get("export_budget_w"), 0.0) > 0.0
    elif target_state == "FORCE_CHARGE_PV":
        executable = executable and safe_float(budget.get("charge_budget_w"), 0.0) > 0.0
    elif target_state != "HEADROOM_EXPORT":
        executable = False
    result["executable_action"] = selected_action if executable else None
    return result


def _build_policy_decision(
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
    previous_policy_decision=None,
):
    decision = _build_policy_decision_legacy(
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
        previous_policy_decision=previous_policy_decision,
    )
    return _enrich_policy_candidate_contract(decision, windows, now_ms)


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
    previous_policy_decision=None,
):
    timeline = []
    projection_points, projection_source = _policy_projection_points(forecast_timeline, target_timeline)
    policy_soc = _clamp(safe_float(current_soc, 0.0), 0.0, 100.0)
    cursor_ts = safe_float(now_ms, 0.0)
    valid_windows = [
        window for window in (windows or [])
        if isinstance(window, dict)
        and safe_float(window.get("start_ts"), 0.0) > 0.0
        and safe_float(window.get("end_ts"), 0.0) > safe_float(window.get("start_ts"), 0.0)
        and safe_float(window.get("end_ts"), 0.0) > safe_float(now_ms, 0.0)
    ]
    boundaries = sorted({
        int(boundary)
        for window in valid_windows
        for boundary in (safe_float(window.get("start_ts"), 0.0), safe_float(window.get("end_ts"), 0.0))
        if boundary > 0.0
    })
    for start_ts, end_ts in zip(boundaries, boundaries[1:]):
        segment_windows = [
            window for window in valid_windows
            if safe_float(window.get("start_ts"), 0.0) < end_ts
            and safe_float(window.get("end_ts"), 0.0) > start_ts
        ]
        if not segment_windows or end_ts <= safe_float(now_ms, 0.0):
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
            segment_windows,
            reserve,
            flags,
            economics,
            mode,
            evaluation_ts,
            projected_soc,
            capacity_wh,
            blocked_reasons,
            previous_policy_decision=(
                previous_policy_decision
                if start_ts <= safe_float(now_ms, 0.0) < end_ts
                else None
            ),
        )
        segment_decision = dict(segment_decision or {})
        execution_window = (
            dict(segment_decision.get("execution_window"))
            if isinstance(segment_decision.get("execution_window"), dict)
            else None
        )
        if execution_window is not None:
            execution_window["start_ts"] = int(max(
                safe_float(execution_window.get("start_ts"), start_ts),
                safe_float(start_ts, 0.0),
            ))
            execution_window["end_ts"] = int(min(
                safe_float(execution_window.get("end_ts"), end_ts),
                safe_float(end_ts, 0.0),
            ))
            if execution_window["end_ts"] <= execution_window["start_ts"]:
                execution_window = None
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
            "source_action": (
                (segment_decision.get("selected_window") or {}).get("action")
                if isinstance(segment_decision.get("selected_window"), dict)
                else None
            ),
            "source_reason": (
                (segment_decision.get("selected_window") or {}).get("reason")
                if isinstance(segment_decision.get("selected_window"), dict)
                else None
            ),
            "projected_soc_pct": round(projected_soc, 1),
            "projected_soc_end_pct": round(projected_end_soc, 1),
            "projected_soc_source": projected_soc_source,
            "execution_window": execution_window,
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
    plan_flags.setdefault("optimization_model", "rolling_plateau_budget_v3")
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
        "market_windows": [],
        "reserve": reserve or {},
        "economics": economics or {},
        "flags": plan_flags,
        "blocked_reasons": list(blocked_reasons or []),
        "policy_decision": policy_decision,
        "policy_timeline": [],
        "future_pv_store_reservation": {
            "schema": "direct_marketing_future_pv_store_reservation_v1",
            "active": False,
            "commands_allowed": False,
            "reason": reason,
            "data_quality": "blocked",
            "next_window": None,
            "valid_until_ts": None,
            "max_curve_charge_w": None,
        },
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
    # Variable, mengenbezogene Vermarktungsentgelte mindern den tatsächlich
    # entgangenen Verkaufserlös. Monatliche Fixkosten bleiben bewusst außerhalb
    # dieser marginalen Slotentscheidung.
    pv_shift_revenue = best_high["net_sell_ct"] * efficiency
    pv_shift_opportunity = max(
        best_low_market["net_sell_ct"],
        safe_float(best_low_market.get("gross_sell_ct"), best_low_market["net_sell_ct"]),
    )
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
        "e3dc_pv_w",
        "external_pv_w",
        "has_e3dc_pv_forecast",
        "home_w",
        "wp_w",
        "heater_w",
        "wallbox_w",
        "forecast_surplus_w",
        "forecast_dc_surplus_w",
        "forecast_deficit_w",
        "has_power_forecast",
        "is_export_plateau",
        "export_plateau_id",
        "export_plateau_origin_start_ts",
        "export_plateau_end_ts",
        "export_plateau_duration_min",
        "export_plateau_peak_net_sell_ct",
        "export_plateau_tolerance_ct",
        "export_plateau_preferred_min",
        "export_plateau_preferred_met",
        "gross_sell_ct",
        "fee_basis",
        "fee_basis_ct",
        "fee_basis_valid",
        "fee_pct",
        "fixed_fee_net_ct",
        "variable_fee_net_ct",
        "fee_cost_ct",
        "service_vat_pct",
        "input_vat_recoverable",
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


def _build_future_pv_store_reservation(
    config,
    entries,
    reserve,
    annotated,
    capacity_wh,
    current_soc,
    now_ms,
    flags,
    mode,
    efficiency,
):
    """Reserviert physischen Headroom, ohne Hardware-Entscheider zu werden."""
    result = {
        "schema": "direct_marketing_future_pv_store_reservation_v1",
        "active": False,
        "commands_allowed": False,
        "reason": "no_future_pv_store_window",
        "data_quality": "not_evaluated",
        "next_window": None,
        "valid_until_ts": None,
        "max_curve_charge_w": None,
    }
    if mode not in {"eco", "eco_plus"} or not cfg_bool(flags.get("pv_store_enable"), False):
        result.update({"reason": "strategy_not_released", "data_quality": "ok"})
        return result
    if not cfg_bool(flags.get("commands_allowed"), False):
        result.update({"reason": "commands_not_released", "data_quality": "ok"})
        return result
    if not cfg_bool(flags.get("live_soc_valid"), True) or safe_float(capacity_wh, 0.0) <= 0.0:
        result.update({"reason": "live_storage_values_invalid", "data_quality": "invalid"})
        return result

    ordered = sorted(
        (dict(item) for item in (entries or []) if isinstance(item, dict)),
        key=lambda item: safe_float(item.get("start_ts"), 0.0),
    )
    current_store = next(
        (
            item for item in ordered
            if item.get("action") == "eco_plus_store_pv_candidate"
            and safe_float(item.get("start_ts"), 0.0) <= now_ms < safe_float(item.get("end_ts"), 0.0)
        ),
        None,
    )
    if current_store is not None:
        result.update({"reason": "current_pv_store_window", "data_quality": "ok"})
        return result

    lookahead_min = max(
        15.0,
        safe_float(config.get("direct_marketing_future_pv_store_reservation_lookahead_min"), 360.0),
    )
    lookahead_end = now_ms + lookahead_min * 60_000.0
    future_store = [
        item for item in ordered
        if item.get("action") == "eco_plus_store_pv_candidate"
        and safe_float(item.get("start_ts"), 0.0) > now_ms
        and safe_float(item.get("start_ts"), 0.0) <= lookahead_end
    ]
    if not future_store:
        result.update({"data_quality": "ok"})
        return result

    first_start = safe_float(future_store[0].get("start_ts"), 0.0)
    next_export_start = min(
        (
            safe_float(item.get("start_ts"), 0.0)
            for item in ordered
            if item.get("action") in {"eco_plus_export_candidate", "arbitrage_export_candidate"}
            and safe_float(item.get("start_ts"), 0.0) > first_start
        ),
        default=lookahead_end + 1.0,
    )
    selected_store = [
        item for item in future_store
        if safe_float(item.get("start_ts"), 0.0) < next_export_start
    ]
    if not selected_store or any(not cfg_bool(item.get("has_power_forecast"), False) for item in selected_store):
        result.update({
            "reason": "future_forecast_incomplete",
            "data_quality": "invalid",
            "valid_until_ts": int(first_start) if first_start > 0 else None,
        })
        return result

    safe_future_wh = sum(
        _pv_store_entry_stored_wh(item, flags, efficiency)
        for item in selected_store
    )
    if safe_future_wh <= 50.0:
        result.update({
            "reason": "future_window_energy_insufficient",
            "data_quality": "ok",
            "valid_until_ts": int(first_start),
        })
        return result

    cap_wh = max(0.0, safe_float(capacity_wh, 0.0))
    soc = _clamp(safe_float(current_soc, 0.0), 0.0, 100.0)
    current_storage_wh = cap_wh * soc / 100.0
    reserve_policy = _policy_reserve_state(config, reserve, annotated, now_ms, soc, cap_wh)
    protected_wh = min(cap_wh, max(0.0, safe_float(reserve_policy.get("protected_energy_wh"), 0.0)))
    house_need_to_window_wh, house_forecast_used = _forecast_deficit_wh(
        annotated or [],
        now_ms,
        first_start,
        enabled=True,
    )
    if not house_forecast_used:
        result.update({
            "reason": "future_forecast_incomplete",
            "data_quality": "invalid",
            "valid_until_ts": int(first_start),
        })
        return result

    target_soc = max(
        [safe_float(item.get("target_soc_pct"), 0.0) for item in selected_store]
        + [safe_float(reserve.get("target_soc_pct"), 100.0)],
    )
    target_soc = _clamp(target_soc if target_soc > 0.0 else 100.0, 0.0, 100.0)
    target_storage_wh = cap_wh * target_soc / 100.0
    expected_storage_at_window_wh = max(0.0, current_storage_wh - house_need_to_window_wh)
    target_need_wh = max(0.0, target_storage_wh - expected_storage_at_window_wh)
    future_energy_insufficient = safe_future_wh + 50.0 < target_need_wh
    required_headroom_wh = min(safe_future_wh, max(0.0, cap_wh - protected_wh))
    headroom_cap_now_wh = min(
        cap_wh,
        max(protected_wh, cap_wh - required_headroom_wh + house_need_to_window_wh),
    )
    allowed_curve_charge_wh = max(0.0, headroom_cap_now_wh - current_storage_wh)
    reserve_recovery_wh = max(0.0, protected_wh - current_storage_wh)
    if reserve_recovery_wh > 0.0:
        allowed_curve_charge_wh = max(allowed_curve_charge_wh, reserve_recovery_wh)

    max_charge_w = max(0.0, safe_float(config.get("maximumladeleistung"), safe_float(flags.get("pv_store_max_w"), 0.0)))
    seconds_to_window = max(1.0, (first_start - now_ms) / 1000.0)
    if reserve_recovery_wh > 0.0:
        max_curve_charge_w = max_charge_w
        reason = "reserve_recovery"
    elif allowed_curve_charge_wh <= 50.0:
        max_curve_charge_w = 0.0
        reason = "future_pv_store_headroom_reserved"
    else:
        proven_precharge_wh = allowed_curve_charge_wh
        if future_energy_insufficient:
            proven_precharge_wh = min(
                allowed_curve_charge_wh,
                max(0.0, target_need_wh - safe_future_wh),
            )
            reason = "future_window_energy_insufficient"
        else:
            reason = "house_need_until_future_window"
        max_curve_charge_w = min(
            max_charge_w,
            max(0.0, proven_precharge_wh * 3600.0 / seconds_to_window),
        )
        if 0.0 < max_curve_charge_w < 300.0:
            max_curve_charge_w = 0.0

    last_end = max(safe_float(item.get("end_ts"), first_start) for item in selected_store)
    result.update({
        "active": True,
        "commands_allowed": True,
        "reason": reason,
        "data_quality": "ok",
        "next_window": {
            "start_ts": int(first_start),
            "end_ts": int(last_end),
            "action": "eco_plus_store_pv_candidate",
            "slot_count": len(selected_store),
        },
        "valid_until_ts": int(first_start),
        "safe_future_pv_absorption_wh": round(safe_future_wh, 0),
        "required_headroom_wh": round(required_headroom_wh, 0),
        "protected_energy_wh": round(protected_wh, 0),
        "protected_soc_pct": round((protected_wh / cap_wh) * 100.0 if cap_wh > 0 else 0.0, 2),
        "current_storage_wh": round(current_storage_wh, 0),
        "current_soc_pct": round(soc, 2),
        "house_need_until_window_wh": round(house_need_to_window_wh, 0),
        "target_storage_wh": round(target_storage_wh, 0),
        "target_soc_pct": round(target_soc, 2),
        "target_need_wh": round(target_need_wh, 0),
        "future_energy_insufficient": bool(future_energy_insufficient),
        "reserve_recovery_wh": round(reserve_recovery_wh, 0),
        "max_storage_before_window_wh": round(headroom_cap_now_wh, 0),
        "max_storage_before_window_soc_pct": round((headroom_cap_now_wh / cap_wh) * 100.0 if cap_wh > 0 else 0.0, 2),
        "max_curve_charge_energy_wh": round(allowed_curve_charge_wh, 0),
        "max_curve_charge_w": int(round(max_curve_charge_w)),
        "no_grid_charge": True,
        "consumer_budgets_untouched": True,
    })
    return result


def _negative_headroom_slot_wh(entry, flags):
    duration_h = _entry_duration_h(entry)
    if duration_h <= 0.0:
        return 0.0
    max_power_w = max(
        0.0,
        safe_float(entry.get("max_power_w"), safe_float(flags.get("pv_store_max_w"), 0.0)),
    )
    if entry.get("has_power_forecast"):
        surplus_key = "forecast_dc_surplus_w" if cfg_bool(flags.get("pv_store_dc_only_enable"), False) else "forecast_surplus_w"
        forecast_surplus_w = max(0.0, safe_float(entry.get(surplus_key), 0.0))
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
        required_headroom_wh = 0.0
        free_headroom_before_wh = max(
            0.0,
            (100.0 - _clamp(safe_float(reserve.get("current_soc_pct"), 0.0), 0.0, 100.0)) / 100.0 * cap_wh,
        )
        if cap_wh > 0.0:
            required_headroom_wh = min(cap_wh, surplus_wh + (buffer_pct / 100.0) * cap_wh)
            required_headroom_pct = _clamp((required_headroom_wh / cap_wh) * 100.0, 0.0, 100.0)
        additional_headroom_wh = max(0.0, required_headroom_wh - free_headroom_before_wh)
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
            "negative_headroom_required_wh": round(required_headroom_wh, 0),
            "negative_headroom_free_before_wh": round(free_headroom_before_wh, 0),
            "negative_headroom_additional_wh": round(additional_headroom_wh, 0),
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


def _uniform_plateau_allocation(candidates, budget_wh):
    """Verteilt ein Plateaubudget mit der kleinstmöglichen konstanten Leistung."""

    allocations = {}
    active = [dict(candidate) for candidate in candidates if candidate.get("duration_h", 0.0) > 0.0]
    remaining_wh = min(
        max(0.0, safe_float(budget_wh, 0.0)),
        sum(max(0.0, safe_float(item.get("energy_wh"), 0.0)) for item in active),
    )
    while active and remaining_wh > 0.01:
        total_h = sum(max(0.0, safe_float(item.get("duration_h"), 0.0)) for item in active)
        if total_h <= 0.0:
            break
        target_w = remaining_wh / total_h
        constrained = [
            item for item in active
            if safe_float(item.get("max_power_w"), 0.0) + 0.000001 < target_w
        ]
        if not constrained:
            for item in active:
                duration_h = safe_float(item.get("duration_h"), 0.0)
                take_wh = min(
                    safe_float(item.get("energy_wh"), 0.0),
                    target_w * duration_h,
                )
                allocations[item["idx"]] = max(0.0, take_wh)
            remaining_wh = 0.0
            break
        constrained_ids = {item["idx"] for item in constrained}
        for item in constrained:
            take_wh = max(0.0, safe_float(item.get("energy_wh"), 0.0))
            allocations[item["idx"]] = take_wh
            remaining_wh = max(0.0, remaining_wh - take_wh)
        active = [item for item in active if item["idx"] not in constrained_ids]
    return allocations


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
    cap_wh = max(0.0, safe_float(capacity_wh, 0.0))
    max_cycle_wh = max(0.0, safe_float(flags.get("max_cycles_per_day"), 1.0)) * cap_wh
    configured_daily_wh = max(0.0, safe_float(flags.get("max_daily_export_kwh"), 0.0)) * 1000.0
    daily_limit_candidates = [value for value in (max_cycle_wh, configured_daily_wh) if value > 0.0]
    daily_limit_wh = min(daily_limit_candidates) if daily_limit_candidates else 0.0
    daily_used_wh = max(0.0, safe_float(flags.get("daily_export_used_wh"), 0.0))
    global_remaining_wh = max(0.0, daily_limit_wh - daily_used_wh) if daily_limit_wh > 0.0 else None
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
                "duration_h": duration_h,
                "net_sell_ct": safe_float(entry.get("net_sell_ct"), safe_float(entry.get("market_ct"), 0.0)),
                "start_ts": safe_float(entry.get("start_ts"), 0.0),
                "plateau_id": str(entry.get("export_plateau_id") or "slot:%d" % idx),
            })

        selected = {}
        remaining_wh = export_budget_wh
        plateau_groups = {}
        for candidate in candidates:
            plateau_groups.setdefault(candidate["plateau_id"], []).append(candidate)
        ordered_plateaus = sorted(
            plateau_groups.values(),
            key=lambda group: (
                sum(item["net_sell_ct"] * item["duration_h"] for item in group)
                / max(0.001, sum(item["duration_h"] for item in group)),
                max(item["start_ts"] for item in group),
            ),
            reverse=True,
        )
        for plateau in ordered_plateaus:
            if remaining_wh <= 50.0:
                break
            plateau_capacity_wh = sum(item["energy_wh"] for item in plateau)
            plateau_budget_wh = min(plateau_capacity_wh, remaining_wh)
            plateau_selected = _uniform_plateau_allocation(plateau, plateau_budget_wh)
            selected.update(plateau_selected)
            remaining_wh = max(0.0, remaining_wh - sum(plateau_selected.values()))

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
            "daily_export_limit_wh": round(daily_limit_wh, 0),
            "daily_export_used_wh": round(daily_used_wh, 0),
            "daily_export_remaining_wh": round(
                max(0.0, (global_remaining_wh if global_remaining_wh is not None else export_capacity_wh)),
                0,
            ),
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
            next_entry["plateau_dispatch_power_w"] = next_entry["max_power_w"]
            next_entry["plateau_dispatch_uniform"] = True
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
            and (
                current.get("action") not in {"eco_plus_export_candidate", "arbitrage_export_candidate"}
                or current.get("export_plateau_id") == entry.get("export_plateau_id")
            )
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
                "_gross_sell": [(
                    safe_float(entry.get("gross_sell_ct"), safe_float(entry.get("market_ct"), 0.0)),
                    max(0.0, _entry_duration_h(entry)),
                )],
                "_fee_cost": [(
                    max(0.0, safe_float(entry.get("fee_cost_ct"), 0.0)),
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
                "gross_sell_ct",
                "fee_basis",
                "fee_basis_ct",
                "fee_basis_valid",
                "fee_pct",
                "fixed_fee_net_ct",
                "variable_fee_net_ct",
                "fee_cost_ct",
                "service_vat_pct",
                "input_vat_recoverable",
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
                "negative_headroom_required_wh",
                "negative_headroom_free_before_wh",
                "negative_headroom_additional_wh",
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
                "export_plateau_id",
                "export_plateau_origin_start_ts",
                "export_plateau_end_ts",
                "export_plateau_duration_min",
                "export_plateau_peak_net_sell_ct",
                "export_plateau_tolerance_ct",
                "export_plateau_preferred_min",
                "export_plateau_preferred_met",
                "plateau_dispatch_power_w",
                "plateau_dispatch_uniform",
                "daily_export_limit_wh",
                "daily_export_used_wh",
                "daily_export_remaining_wh",
                "market_window_id",
                "market_window_start_ts",
                "market_window_end_ts",
                "market_window_margin_class",
                "market_margin_class",
                "marginal_net_sell_ct",
                "marginal_settlement_valid",
                "marginal_export_class",
                "actuator_closure",
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
        current["_gross_sell"].append((
            safe_float(entry.get("gross_sell_ct"), safe_float(entry.get("market_ct"), 0.0)),
            max(0.0, _entry_duration_h(entry)),
        ))
        current["_fee_cost"].append((
            max(0.0, safe_float(entry.get("fee_cost_ct"), 0.0)),
            max(0.0, _entry_duration_h(entry)),
        ))

    for item in windows:
        market = item.pop("_market")
        billing = item.pop("_billing")
        score = item.pop("_score")
        net_sell = item.pop("_net_sell")
        gross_sell = item.pop("_gross_sell")
        fee_cost = item.pop("_fee_cost")
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
        item["gross_sell_ct"] = round(
            sum(value * duration_h for value, duration_h in gross_sell) / max(0.000001, sum(duration_h for _value, duration_h in gross_sell)),
            3,
        )
        item["avg_gross_sell_ct"] = item["gross_sell_ct"]
        item["fee_cost_ct"] = round(
            sum(value * duration_h for value, duration_h in fee_cost) / max(0.000001, sum(duration_h for _value, duration_h in fee_cost)),
            3,
        )
        item["avg_fee_cost_ct"] = item["fee_cost_ct"]
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
    previous_policy_decision=None,
    previous_market_windows=None,
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
        "arbitrage_requested": bool(
            cfg_bool(config.get("direct_marketing_arbitrage_enable"), False)
            or cfg_bool(config.get("direct_marketing_arbitrage_experimental_enable"), False)
            or mode == "arbitrage"
        ),
        "arbitrage_release_allowed": False,
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
        "v2x_discharge_enable": cfg_bool(config.get("direct_marketing_v2x_discharge_enable"), False),
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
        "max_daily_export_kwh": max(
            0.0,
            safe_float(config.get("direct_marketing_max_daily_export_kwh"), 0.0),
        ),
        "daily_export_used_wh": max(
            0.0,
            safe_float(config.get("_runtime_direct_marketing_daily_export_used_wh"), 0.0),
        ),
        "preferred_export_plateau_min": max(
            15.0,
            safe_float(config.get("direct_marketing_preferred_export_plateau_min"), 60.0),
        ),
        "price_plateau_tolerance_ct": _clamp(
            safe_float(config.get("direct_marketing_price_plateau_tolerance_ct"), 0.75),
            0.0,
            20.0,
        ),
        "export_segment_load_reserve_enable": cfg_bool(
            config.get("direct_marketing_export_segment_load_reserve_enable"),
            True,
        ),
    }
    flags["commands_allowed"] = False
    flags["owner_contract_version"] = OWNER_CONTRACT_VERSION
    flags["price_domain_policy"] = "negative_hard_eeg_soft_score_fallback"
    flags["optimization_model"] = "rolling_plateau_budget_v3"
    flags["profit_profile"] = _normalize_profit_profile(config.get("direct_marketing_profit_profile", "standard"))
    settlement_accounting = _settlement_accounting(config)
    flags["settlement_fee_basis"] = settlement_accounting["variable_fee"]["basis"]
    flags["settlement_fee_basis_valid"] = settlement_accounting["variable_fee"]["basis_valid"]

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
        sell_components = _net_sell_components(market_ct, config)
        net_sell_ct = sell_components["net_sell_ct"]
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
            **{key: value for key, value in sell_components.items() if key != "net_sell_ct"},
            **forecast_power,
        })

    annotated = _annotate_export_price_plateaus(annotated, config)
    market_windows = _build_negative_price_market_windows(
        annotated,
        previous_market_windows=previous_market_windows,
    )
    economics = _economic_state(config, annotated)
    blocked_reasons = list(current_price_quality_blockers)
    if (
        settlement_accounting["variable_fee"]["percent"] > 0.0
        and not settlement_accounting["variable_fee"]["basis_valid"]
    ):
        blocked_reasons.append("settlement_fee_basis_missing")
    if not flags["live_soc_valid"]:
        blocked_reasons.append("live_values_missing:current_soc")

    if mode == "arbitrage":
        blocked_reasons.append("arbitrage_not_released")
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
                # PV_STORE verwendet das höchste aktuell erlaubte Planungsziel.
                # Das rohe Marktfenster bleibt von dieser Aktion unabhängig.
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
                    "market_window_id": slot.get("market_window_id"),
                    "market_window_start_ts": slot.get("market_window_start_ts"),
                    "market_window_end_ts": slot.get("market_window_end_ts"),
                    "market_window_margin_class": slot.get("market_window_margin_class"),
                    "market_margin_class": slot.get("market_margin_class"),
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

        if not slot.get("is_export_plateau", slot["is_high"]):
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

    future_pv_store_reservation = _build_future_pv_store_reservation(
        config,
        entries,
        reserve,
        annotated,
        capacity_wh,
        current_soc,
        now_ms,
        flags,
        mode,
        efficiency,
    )
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
    arbitrage_commands = False
    flags["commands_allowed"] = bool(eco_commands or arbitrage_commands)
    if not flags["live_soc_valid"]:
        flags["commands_allowed"] = False
    if not flags.get("settlement_fee_basis_valid", True):
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
        previous_policy_decision=previous_policy_decision,
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
            previous_policy_decision=previous_policy_decision,
        )

    planned_export_wh = sum(
        max(0.0, safe_float(window.get("max_power_w"), 0.0)) * _entry_duration_h(window)
        for window in windows
        if window.get("action") in {"eco_plus_export_candidate", "arbitrage_export_candidate"}
    )
    cap_wh = max(0.0, safe_float(capacity_wh, 0.0))
    cycle_limit_wh = max(0.0, safe_float(flags.get("max_cycles_per_day"), 0.0)) * cap_wh
    configured_daily_wh = max(0.0, safe_float(flags.get("max_daily_export_kwh"), 0.0)) * 1000.0
    daily_limits = [value for value in (cycle_limit_wh, configured_daily_wh) if value > 0.0]
    effective_daily_limit_wh = min(daily_limits) if daily_limits else 0.0
    daily_used_wh = max(0.0, safe_float(flags.get("daily_export_used_wh"), 0.0))
    battery_wear_budget = {
        "planned_export_wh": round(planned_export_wh, 0),
        "planned_equivalent_full_cycles": round(planned_export_wh / cap_wh, 4) if cap_wh > 0.0 else 0.0,
        "daily_export_limit_wh": round(effective_daily_limit_wh, 0),
        "daily_export_used_wh": round(daily_used_wh, 0),
        "daily_export_remaining_wh": round(
            max(0.0, effective_daily_limit_wh - daily_used_wh)
            if effective_daily_limit_wh > 0.0
            else 0.0,
            0,
        ),
        "lcos_model": "base_plus_depth",
        "temperature_guard": "not_available" if not cfg_bool(config.get("_runtime_direct_marketing_battery_temperature_valid"), False) else "runtime_input",
    }

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
        "market_windows": market_windows,
        "reserve": reserve,
        "economics": economics,
        "settlement_accounting": settlement_accounting,
        "battery_wear_budget": battery_wear_budget,
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
        "future_pv_store_reservation": future_pv_store_reservation,
    }
