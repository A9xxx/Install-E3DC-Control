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

import copy
import hashlib
import json
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    from .pv_forecast_topology import (
        build_pv_forecast_topology,
        has_explicit_topology_config,
    )
    from .direct_marketing_identity import (
        PASSIVE_NORMAL_BINDING_SCHEMA,
        passive_normal_identity,
    )
    from .direct_marketing_actions import (
        DIRECT_MARKETING_ACTIVE_TARGETS,
        DIRECT_MARKETING_EXPORT_GATE_LINEAGE_EFFECT_CONTRACT,
        DIRECT_MARKETING_EXPORT_GATE_LINEAGE_SCHEMA,
        DIRECT_MARKETING_EXPORT_GATE_LINEAGE_STATES,
        DIRECT_MARKETING_EXPORT_START_GATE_SCHEMA,
        direct_marketing_contract_sha256,
        direct_marketing_export_gate_contract_valid,
        direct_marketing_export_gate_generation_id,
        direct_marketing_export_gate_lineage_id,
        direct_marketing_export_gate_lineage_shape_valid,
        direct_marketing_export_gate_lineage_valid,
        direct_marketing_export_gate_sha256,
        direct_marketing_typed_int_equals,
    )
except ImportError:
    from pv_forecast_topology import (
        build_pv_forecast_topology,
        has_explicit_topology_config,
    )
    from direct_marketing_identity import (
        PASSIVE_NORMAL_BINDING_SCHEMA,
        passive_normal_identity,
    )
    from direct_marketing_actions import (  # type: ignore
        DIRECT_MARKETING_ACTIVE_TARGETS,
        DIRECT_MARKETING_EXPORT_GATE_LINEAGE_EFFECT_CONTRACT,
        DIRECT_MARKETING_EXPORT_GATE_LINEAGE_SCHEMA,
        DIRECT_MARKETING_EXPORT_GATE_LINEAGE_STATES,
        DIRECT_MARKETING_EXPORT_START_GATE_SCHEMA,
        direct_marketing_contract_sha256,
        direct_marketing_export_gate_contract_valid,
        direct_marketing_export_gate_generation_id,
        direct_marketing_export_gate_lineage_id,
        direct_marketing_export_gate_lineage_shape_valid,
        direct_marketing_export_gate_lineage_valid,
        direct_marketing_export_gate_sha256,
        direct_marketing_typed_int_equals,
    )


SLOT_MS = 15 * 60 * 1000
OWNER_CONTRACT_VERSION = 1
POLICY_SCHEMA = "direct_marketing_policy_v1"
EXPORT_WINDOW_START_GATE_SCHEMA = DIRECT_MARKETING_EXPORT_START_GATE_SCHEMA
EXPORT_WINDOW_GATE_LINEAGE_SCHEMA = DIRECT_MARKETING_EXPORT_GATE_LINEAGE_SCHEMA
EXPORT_WINDOW_GATE_LINEAGE_STATES = DIRECT_MARKETING_EXPORT_GATE_LINEAGE_STATES
EXPORT_WINDOW_GATE_LINEAGE_EFFECT_CONTRACT = (
    DIRECT_MARKETING_EXPORT_GATE_LINEAGE_EFFECT_CONTRACT
)
VALID_MODES = {"safe", "eco", "eco_plus", "arbitrage"}
VALID_PROFIT_PROFILES = {"standard", "aggressive", "expert"}
MARKET_TIMEZONE = ZoneInfo("Europe/Berlin")
AUX_AC_STORAGE_MODES = {
    "off",
    "reserve_only",
    "house_supply",
    "economic",
}
AUX_AC_POSITIVE_MARGIN_EPSILON_CT = 1e-6
E3DC_EXPORT_EXECUTION_OWNERS = {"storage_manager", "external_e3dc_luox"}
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


def _normalize_e3dc_export_execution_owner(raw):
    """Bindet einen externen E3/DC-Aktor nur über eine explizite Topologieangabe."""

    owner = str(raw or "storage_manager").strip().lower()
    if owner in E3DC_EXPORT_EXECUTION_OWNERS:
        return owner
    return "storage_manager"


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


def _normalize_aux_ac_storage_mode(config):
    """Liest den neuen AC-Modus fail-safe und migriert nur explizite Altfreigaben.

    Der frühere Bool-Schalter war eine Nutzerfreigabe, enthielt aber keine
    wirtschaftliche Aussage. ``true`` wird deshalb ausschließlich auf den
    konservativen Modus ``reserve_only`` abgebildet. Fehlende, leere oder
    unbekannte Werte bleiben ``off``.
    """

    config = config or {}
    canonical_keys = (
        "direct_marketing_aux_inverter_ac_storage_mode",
        "direct_marketing_pv_store_aux_ac_mode",
    )
    for key in canonical_keys:
        if key not in config:
            continue
        raw = str(config.get(key) or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "aus": "off",
            "disabled": "off",
            "reserve": "reserve_only",
            "reserve_sichern": "reserve_only",
            "reserve_only": "reserve_only",
            "hausversorgung": "house_supply",
            "hausversorgung_sichern": "house_supply",
            "house_supply": "house_supply",
            "wirtschaftlich": "economic",
            "economical": "economic",
            "economic": "economic",
        }
        mode = aliases.get(raw, raw)
        return (
            mode if mode in AUX_AC_STORAGE_MODES else "off",
            key if mode in AUX_AC_STORAGE_MODES else "%s_invalid" % key,
        )

    legacy_key = "direct_marketing_aux_inverter_ac_storage_enable"
    if legacy_key in config and cfg_bool(config.get(legacy_key), False):
        return "reserve_only", "legacy_bool_explicit_true"
    return "off", "default_off"


def _pv_store_route_efficiencies(config):
    """Getrennte Lade- und Entladewirkungsgrade für DC- und Zusatz-AC-Routen."""

    config = config or {}
    dc_charge_pct = _clamp(
        safe_float(config.get("direct_marketing_pv_store_dc_charge_efficiency_pct"), 96.0),
        50.0,
        100.0,
    )
    aux_ac_charge_pct = _clamp(
        safe_float(config.get("direct_marketing_pv_store_aux_ac_charge_efficiency_pct"), 90.0),
        50.0,
        100.0,
    )
    discharge_pct = _clamp(
        safe_float(config.get("direct_marketing_pv_store_discharge_efficiency_pct"), 95.0),
        50.0,
        100.0,
    )
    return {
        "dc_charge_pct": dc_charge_pct,
        "aux_ac_charge_pct": aux_ac_charge_pct,
        "discharge_pct": discharge_pct,
        "dc_charge": dc_charge_pct / 100.0,
        "aux_ac_charge": aux_ac_charge_pct / 100.0,
        "discharge": discharge_pct / 100.0,
        "dc_route_pct": dc_charge_pct * discharge_pct / 100.0,
        "aux_ac_route_pct": aux_ac_charge_pct * discharge_pct / 100.0,
    }


def _optional_contract_bool(value):
    if value is None or str(value).strip() == "":
        return None
    return cfg_bool(value, False)


def _slot_numeric_present(slot, keys):
    for key in keys:
        if key not in (slot or {}):
            continue
        value = slot.get(key)
        if value is None or isinstance(value, bool) or str(value).strip() == "":
            continue
        try:
            if math.isfinite(float(str(value).replace(",", "."))):
                return True
        except Exception:
            continue
    return False


def _slot_source_split_contract(slot):
    """Defensiver Adapter für alte und neue Topologie-/Forecast-Slotverträge."""

    slot = slot or {}
    topology_status = str(
        slot.get("pv_topology_status")
        or slot.get("pv_source_split_status")
        or slot.get("generator_group_projection_status")
        or ""
    ).strip().lower()
    topology_quality = str(
        slot.get("pv_topology_quality")
        or slot.get("pv_source_split_quality")
        or ""
    ).strip().lower()
    projection_status = str(
        slot.get("pv_resource_projection_status")
        or slot.get("provider_projection_status")
        or slot.get("generator_group_provider_status")
        or ""
    ).strip().lower()
    topology_revision = str(
        slot.get("pv_topology_revision")
        or slot.get("pv_source_split_revision")
        or slot.get("topology_revision")
        or ""
    ).strip()
    complete = bool(
        topology_status in {"bound", "complete"}
        and topology_quality in {"complete", "bound"}
        and projection_status in {"complete", "bound"}
        and _slot_numeric_present(slot, ("e3dc_dc_pv_w",))
        and _slot_numeric_present(slot, ("external_ac_pv_w",))
    )

    freshness = None
    for key in (
        "pv_forecast_fresh",
        "forecast_fresh",
        "pv_source_split_fresh",
        "provider_forecast_fresh",
    ):
        if key in slot:
            freshness = _optional_contract_bool(slot.get(key))
            break
    producer_freshness_source = ""
    for key in (
        "pv_forecast_freshness_source",
        "forecast_freshness_source",
        "pv_source_split_freshness_source",
    ):
        if key in slot and str(slot.get(key) or "").strip():
            producer_freshness_source = str(slot.get(key)).strip()
            break
    freshness_source = (
        producer_freshness_source
        or ("explicit_slot_flag" if freshness is not None else "unconfirmed")
    )
    return {
        "status": topology_status or "missing",
        "quality": topology_quality or "missing",
        "projection_status": projection_status or "missing",
        "revision": topology_revision or None,
        "complete": complete,
        "fresh": bool(freshness is True),
        "freshness_source": freshness_source,
    }


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
    config = config or {}
    if has_explicit_topology_config(config):
        topology = build_pv_forecast_topology(config)
        if (
            topology.get("contract_mode") != "explicit_generator_groups"
            or topology.get("status") != "bound"
        ):
            return None
        resources = [
            item
            for item in topology.get("resources", [])
            if isinstance(item, dict)
        ]
        capacities = []
        for item in resources:
            kwp = safe_float(item.get("kwp"), float("nan"))
            if (
                not math.isfinite(kwp)
                or kwp <= 0.0
                or safe_int(item.get("surface_count"), 0) <= 0
                or str(item.get("coupling") or "") not in {"E3DC_DC", "EXTERNAL_AC"}
            ):
                return None
            capacities.append(kwp)
        return sum(capacities) if capacities else None

    total = 0.0
    configured = False
    for key in ("forecast1", "forecast2", "forecast3", "forecast4", "forecast5"):
        raw = str(config.get(key) or "").strip()
        if not raw:
            continue
        configured = True
        parts = [part.strip() for part in raw.split("/")]
        if len(parts) < 3:
            return None
        kwp = _numeric_token(parts[2])
        if kwp is None or kwp <= 0:
            return None
        total += kwp
    return total if configured and total > 0.0 else None


def _weighted_eeg_rate_ct(tiers, capacity_kwp):
    if not tiers:
        return None
    if capacity_kwp is None:
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
        str((slot or {}).get("market_price_source") or "").strip().lower(),
        str((slot or {}).get("tariff_provider") or "").strip().lower(),
        str((config or {}).get("tariff_provider") or "").strip().lower(),
        str((config or {}).get("direct_marketing_price_provider") or "").strip().lower(),
        str((config or {}).get("price_source") or "").strip().lower(),
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


def _external_ac_source_configured(config):
    """True, wenn die Konfiguration einen externen AC-PV-Pfad erkennen lässt."""

    config = config or {}
    if safe_float(config.get("pv_external_ac_inverter_limit_w"), 0.0) > 0.0:
        return True
    aux_ac_mode, _mode_source = _normalize_aux_ac_storage_mode(config)
    if aux_ac_mode != "off":
        return True
    try:
        topology = build_pv_forecast_topology(config)
    except Exception:
        topology = {}
    if any(
        str(item.get("coupling") or "").strip().upper() == "EXTERNAL_AC"
        for item in topology.get("resources", [])
        if isinstance(item, dict)
    ):
        return True
    if cfg_bool(config.get("direct_marketing_aux_inverter_ac_storage_enable"), False):
        return True
    if str(config.get("direct_marketing_aux_inverter_shelly_override") or "").strip().lower() == "central":
        return True
    if str(config.get("direct_marketing_aux_inverter_shelly_ip") or "").strip():
        return True
    nested = config.get("direct_marketing_aux_inverter_shelly")
    if isinstance(nested, dict):
        if str(nested.get("override") or "").strip().lower() == "central":
            return True
        if str(nested.get("ip") or "").strip():
            return True
    return any(
        str(config.get(key) or "").strip().upper().replace("-", "_") == "EXTERNAL_AC"
        for key in (
            "pv_forecast_coupling_fc1",
            "pv_forecast_coupling_fc2",
            "pv_forecast_coupling_fc3",
            "pv_forecast_coupling_fc4",
        )
    )


def _slot_forecast_power(slot, assume_total_pv_is_e3dc_dc=False, cut_external_ac=False):
    source_split = _slot_source_split_contract(slot)
    pv_w = _slot_power(slot, ("pv_w", "pv", "PV_Power"))
    e3dc_keys = ("e3dc_dc_pv_w", "e3dc_pv_w", "pv_dc_w", "E3DC_PV_Power")
    external_keys = ("external_ac_pv_w", "external_pv_w", "ext_pv_w", "Ext_PV_Power")
    e3dc_pv_w = _slot_power(slot, e3dc_keys)
    external_pv_w = 0.0 if cut_external_ac else _slot_power(slot, external_keys)
    if cut_external_ac and _slot_power_present(slot, e3dc_keys):
        pv_w = e3dc_pv_w
    has_e3dc_pv_forecast = _slot_power_present(slot, e3dc_keys)
    if has_e3dc_pv_forecast:
        e3dc_pv_forecast_source = (
            "bound_pv_topology"
            if _slot_power_present(slot, ("e3dc_dc_pv_w",))
            else "explicit_e3dc_pv_forecast"
        )
    elif assume_total_pv_is_e3dc_dc and _slot_power_present(slot, ("pv_w", "pv", "PV_Power")):
        e3dc_pv_w = pv_w
        has_e3dc_pv_forecast = True
        e3dc_pv_forecast_source = "total_pv_without_external_ac_configuration"
    else:
        e3dc_pv_forecast_source = "missing"
    home_w = _slot_power(slot, ("home_w", "home", "Home_Power", "load_w"))
    wp_w = _slot_power(slot, ("wp_w", "heatpump_w", "WP_Power"))
    heater_w = _slot_power(slot, ("heater_w", "heizstab_w", "Heizstab_Power"))
    wallbox_w = _slot_power(slot, ("wallbox_w", "Wallbox_Power", "wb_w"))
    has_power = bool(
        _slot_power_present(slot, ("pv_w", "pv", "PV_Power", "surplus_w", "surplus"))
        or _slot_power_present(slot, e3dc_keys)
        or _slot_power_present(slot, external_keys)
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
    local_load_after_external_w = max(0.0, controlled_load_w - external_pv_w)
    dc_surplus_w = max(0.0, e3dc_pv_w - local_load_after_external_w) if has_e3dc_pv_forecast else 0.0
    return {
        "pv_w": pv_w,
        "e3dc_pv_w": e3dc_pv_w,
        "external_pv_w": external_pv_w,
        "has_e3dc_pv_forecast": has_e3dc_pv_forecast,
        "e3dc_pv_forecast_source": e3dc_pv_forecast_source,
        "home_w": home_w,
        "wp_w": wp_w,
        "heater_w": heater_w,
        "wallbox_w": wallbox_w,
        "forecast_surplus_w": max(0.0, surplus_w),
        "forecast_dc_surplus_w": dc_surplus_w,
        "forecast_deficit_w": max(0.0, -surplus_w),
        "has_power_forecast": has_power,
        "pv_topology_status": source_split["status"],
        "pv_topology_quality": source_split["quality"],
        "pv_resource_projection_status": source_split["projection_status"],
        "pv_topology_revision": source_split["revision"],
        "pv_store_source_split_complete": source_split["complete"],
        "pv_store_forecast_fresh": source_split["fresh"],
        "pv_store_forecast_freshness_source": source_split["freshness_source"],
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
    installed_capacity_value = (
        capacity_override
        if capacity_override is not None and capacity_override > 0.0
        else forecast_capacity_kwp
    )
    installed_capacity_known = bool(
        installed_capacity_value is not None
        and safe_float(installed_capacity_value, 0.0) > 0.0
    )
    installed_kwp = (
        max(0.0, safe_float(installed_capacity_value, 0.0))
        if installed_capacity_known
        else 0.0
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
        "installed_capacity_kwp": (
            round(installed_kwp, 3)
            if installed_capacity_known
            else None
        ),
        "installed_capacity_source": (
            "manual_override"
            if capacity_override is not None and capacity_override > 0.0
            else (
                "forecast_topology"
                if forecast_capacity_kwp is not None
                else "missing_or_invalid"
            )
        ),
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


def _market_day_key(ts_ms):
    """Liefert den lokalen ENTSO-E-/EPEX-Abrechnungstag eines Slots."""

    try:
        value = float(ts_ms)
        if not math.isfinite(value) or value <= 0.0:
            return ""
        return datetime.fromtimestamp(
            value / 1000.0,
            tz=MARKET_TIMEZONE,
        ).date().isoformat()
    except (OverflowError, OSError, TypeError, ValueError):
        return ""


def _bind_passive_normal_identity(decision, mode):
    """Versiegelt einen passiven Eco+-NORMAL-Abschnitt ohne Hardware-Intent."""

    source = dict(decision or {})
    selected = (
        source.get("selected_window")
        if isinstance(source.get("selected_window"), dict)
        else {}
    )
    start_ts = safe_int(source.get("start_ts"), 0)
    end_ts = safe_int(source.get("end_ts"), 0)
    selected_start_ts = safe_int(selected.get("start_ts"), 0)
    selected_end_ts = safe_int(selected.get("end_ts"), 0)
    eligible = bool(
        str(mode or "") == "eco_plus"
        and source.get("schema") == POLICY_SCHEMA
        and source.get("commands_allowed") is False
        and source.get("blocked") is False
        and str(source.get("dv_target_state") or "").upper() == "NORMAL"
        and str(source.get("source_action") or "") == "eco_plus_house_supply"
        and source.get("executable_action") is None
        and str(selected.get("action") or "") == "eco_plus_house_supply"
        and min(
            start_ts,
            end_ts,
            selected_start_ts,
            selected_end_ts,
        ) >= 10_000_000_000
        and start_ts < end_ts
        and selected_start_ts <= start_ts < end_ts <= selected_end_ts
    )
    if not eligible:
        source.pop("policy_action_id", None)
        source.pop("policy_slot_id", None)
        source.pop("passive_normal_binding", None)
        return source

    binding = passive_normal_identity(
        start_ts=start_ts,
        end_ts=end_ts,
        selected_start_ts=selected_start_ts,
        selected_end_ts=selected_end_ts,
        window_id=selected.get("window_id"),
    )
    source["policy_action_id"] = binding["policy_action_id"]
    source["policy_slot_id"] = binding["policy_slot_id"]
    source["passive_normal_binding"] = binding
    return source


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
        execution_owner = _normalize_e3dc_export_execution_owner(
            (flags or {}).get("e3dc_export_execution_owner")
        )
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
            "export_constraint_execution_owner": execution_owner,
            "external_e3dc_owner_configured": execution_owner == "external_e3dc_luox",
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
        "external_e3dc_owner_configured": False,
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


def _policy_export_continuous_allowed(profile, export_economics):
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
    return True, "Profit export continuous gates allowed"


def _policy_export_start_allowed(profile, export_economics):
    if profile != "standard":
        return True, "Profit export start gates not required for profile"
    profit = safe_float(export_economics.get("expected_profit_eur"), 0.0)
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
    return True, "Profit export start gates allowed"


def _policy_export_allowed(profile, export_economics):
    continuous_allowed, reason = _policy_export_continuous_allowed(
        profile,
        export_economics,
    )
    if not continuous_allowed:
        return False, reason
    start_allowed, reason = _policy_export_start_allowed(
        profile,
        export_economics,
    )
    if not start_allowed:
        return False, reason
    if profile == "expert":
        return True, "Expert: margin check bypassed, hard reserve kept"
    if profile == "aggressive":
        return True, "Aggressive: positive net profit after efficiency"
    return True, "Profit export allowed"


def _policy_finite_contract_number(value):
    return bool(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _policy_sha256_contract(material):
    return direct_marketing_contract_sha256(material)


def _policy_export_gate_sha256(gate):
    return direct_marketing_export_gate_sha256(gate)


def _policy_export_gate_lineage_id(gate):
    return direct_marketing_export_gate_lineage_id(gate)


def _policy_export_gate_generation_id(gate_lineage_id, generation):
    return direct_marketing_export_gate_generation_id(
        gate_lineage_id,
        generation,
    )


def _policy_export_gate_lineage_contract(
    gate,
    status,
    transition_reason_codes,
    previous_lineage=None,
):
    """Erzeugt einen wirkungslosen Statusvertrag für genau eine Gate-Lineage."""

    if not isinstance(gate, dict) or status not in EXPORT_WINDOW_GATE_LINEAGE_STATES:
        return None
    gate_sha256 = _policy_export_gate_sha256(gate)
    gate_lineage_id = _policy_export_gate_lineage_id(gate)
    if not gate_sha256 or not gate_lineage_id:
        return None
    previous = previous_lineage if isinstance(previous_lineage, dict) else {}
    previous_valid = _policy_export_gate_lineage_shape_valid(previous)
    generation = 1
    if previous_valid and previous.get("gate_lineage_id") == gate_lineage_id:
        generation = previous["current_generation"]
        if status == "ACTIVE" and previous.get("status") == "SUSPENDED":
            generation += 1
    reasons = sorted({
        str(reason).strip()
        for reason in transition_reason_codes or []
        if str(reason).strip()
    })
    if not reasons:
        return None
    current_generation_id = _policy_export_gate_generation_id(
        gate_lineage_id,
        generation,
    )
    previous_generation_id = (
        _policy_export_gate_generation_id(gate_lineage_id, generation - 1)
        if generation > 1
        else None
    )
    return {
        "schema": EXPORT_WINDOW_GATE_LINEAGE_SCHEMA,
        "gate_lineage_id": gate_lineage_id,
        "gate_sha256": gate_sha256,
        "status": status,
        "current_generation": generation,
        "current_generation_id": current_generation_id,
        "previous_generation_id": previous_generation_id,
        "transition_reason_codes": reasons,
        "action": gate.get("action"),
        "window_id": gate.get("window_id"),
        "origin_start_ts": gate.get("origin_start_ts"),
        "end_ts": gate.get("end_ts"),
        "effect_contract": EXPORT_WINDOW_GATE_LINEAGE_EFFECT_CONTRACT,
    }


def _policy_export_gate_lineage_shape_valid(lineage, allowed_statuses=None):
    return direct_marketing_export_gate_lineage_shape_valid(
        lineage,
        allowed_statuses,
    )


def _policy_export_gate_lineage_valid(
    lineage,
    gate,
    allowed_statuses=None,
):
    return direct_marketing_export_gate_lineage_valid(
        lineage,
        gate,
        allowed_statuses,
    )


def _policy_export_business_slot_contracts(annotated, export_window):
    """Bindet die Preis-/Tarifsemantik der Slots eines Exportfensters."""

    window = export_window if isinstance(export_window, dict) else {}
    start_ts = safe_int(window.get("start_ts"), 0)
    end_ts = safe_int(window.get("end_ts"), 0)
    if start_ts <= 0 or end_ts <= start_ts:
        return []
    contracts = []
    for slot in sorted(
        (item for item in (annotated or []) if isinstance(item, dict)),
        key=lambda item: safe_int(item.get("ts"), 0),
    ):
        slot_start = safe_int(slot.get("ts"), 0)
        slot_end = safe_int(slot.get("end_ts"), slot_start + SLOT_MS)
        if slot_start < start_ts or slot_end > end_ts:
            continue
        contract = {
            "start_ts": slot_start,
            "end_ts": slot_end,
            "market_ct": slot.get("market_ct"),
            "net_sell_ct": slot.get("net_sell_ct"),
            "gross_sell_ct": slot.get("gross_sell_ct"),
            "market_price_source": slot.get("market_price_source"),
            "market_price_resolution_min": slot.get(
                "market_price_resolution_min"
            ),
            "fee_basis": slot.get("fee_basis"),
            "fee_basis_ct": slot.get("fee_basis_ct"),
            "fee_pct": slot.get("fee_pct"),
            "fixed_fee_net_ct": slot.get("fixed_fee_net_ct"),
            "variable_fee_net_ct": slot.get("variable_fee_net_ct"),
            "service_vat_pct": slot.get("service_vat_pct"),
            "input_vat_recoverable": slot.get("input_vat_recoverable"),
            "price_revision": (
                slot.get("direct_marketing_price_revision")
                or slot.get("market_price_revision")
                or slot.get("price_revision")
                or slot.get("price_revision_sha256")
            ),
        }
        if not _policy_sha256_contract(contract):
            return []
        contracts.append(contract)
    if not contracts:
        return []
    cursor = start_ts
    for contract in contracts:
        if contract["start_ts"] != cursor or contract["end_ts"] <= cursor:
            return []
        cursor = contract["end_ts"]
    return contracts if cursor == end_ts else []


def _policy_export_business_binding(
    annotated,
    export_window,
    config,
    flags,
    profile,
    mode,
):
    """Trennt das stabile Exportgeschäft von der rollenden Planprojektion."""

    window = export_window if isinstance(export_window, dict) else {}
    slots = _policy_export_business_slot_contracts(annotated, window)
    if not slots:
        return None
    owner_contract = {
        "mode": str(mode or ""),
        "profile": str(profile or ""),
        "source_action": str(window.get("action") or ""),
        "commands_allowed": bool(cfg_bool((flags or {}).get("commands_allowed"), False)),
        "export_enable": bool(cfg_bool((flags or {}).get("export_enable"), False)),
    }
    safety_contract = {
        "thresholds": _policy_thresholds(config or {}),
        "max_export_w": (flags or {}).get("max_export_w"),
        "max_cycles_per_day": (flags or {}).get("max_cycles_per_day"),
        "max_daily_export_kwh": (flags or {}).get("max_daily_export_kwh"),
        "negative_price_no_export": (flags or {}).get(
            "negative_price_no_export"
        ),
    }
    binding = {
        "schema": "direct_marketing_export_business_binding_v1",
        "action": str(window.get("action") or ""),
        "origin_start_ts": safe_int(window.get("start_ts"), 0),
        "end_ts": safe_int(window.get("end_ts"), 0),
        "slot_contracts": slots,
        "price_tariff_revision_sha256": _policy_sha256_contract(slots),
        "owner_revision_sha256": _policy_sha256_contract(owner_contract),
        "safety_revision_sha256": _policy_sha256_contract(safety_contract),
    }
    binding["business_contract_sha256"] = _policy_sha256_contract(binding)
    if not all(
        isinstance(binding.get(key), str) and binding.get(key).startswith("sha256:")
        for key in (
            "price_tariff_revision_sha256",
            "owner_revision_sha256",
            "safety_revision_sha256",
            "business_contract_sha256",
        )
    ):
        return None
    return binding


def _policy_export_business_window_id(binding):
    contract = binding if isinstance(binding, dict) else {}
    digest = str(contract.get("business_contract_sha256") or "")
    return "export-business:%s" % digest[7:31] if digest.startswith("sha256:") else ""


def _policy_export_suffix_matches(start_binding, current_binding):
    """Erkennt nur einen lückenlosen, unveränderten Suffix desselben Geschäfts."""

    previous = start_binding if isinstance(start_binding, dict) else {}
    current = current_binding if isinstance(current_binding, dict) else {}
    if not bool(
        previous.get("schema") == current.get("schema")
        == "direct_marketing_export_business_binding_v1"
        and previous.get("action") == current.get("action")
        and safe_int(previous.get("end_ts"), 0)
        == safe_int(current.get("end_ts"), 0)
        and safe_int(previous.get("origin_start_ts"), 0)
        <= safe_int(current.get("origin_start_ts"), 0)
        < safe_int(previous.get("end_ts"), 0)
        and previous.get("owner_revision_sha256")
        == current.get("owner_revision_sha256")
        and previous.get("safety_revision_sha256")
        == current.get("safety_revision_sha256")
    ):
        return False
    current_start = safe_int(current.get("origin_start_ts"), 0)
    expected_suffix = [
        slot
        for slot in previous.get("slot_contracts") or []
        if safe_int(slot.get("start_ts"), 0) >= current_start
    ]
    return bool(
        expected_suffix
        and expected_suffix == current.get("slot_contracts")
    )


def _policy_export_window_start_gate_contract(
    profile,
    export_window,
    export_economics,
    window_id,
    business_binding=None,
):
    """Bindet die einmaligen Eintrittsgates eines freigegebenen Verkaufsfensters."""

    if profile not in VALID_PROFIT_PROFILES or not isinstance(export_window, dict):
        return None
    action = str(export_window.get("action") or "")
    # Der Startvertrag bindet den tatsächlichen ersten Freigabezeitpunkt. Ein
    # älterer Plateau-Ursprung darf nicht als scheinbar ausgeführter Start
    # dienen, wenn die Policy erstmals mitten im Fenster freigibt.
    origin_start_ts = safe_int(export_window.get("start_ts"), 0)
    end_ts = safe_int(export_window.get("end_ts"), 0)
    values = {
        "initial_expected_profit_eur": export_economics.get(
            "expected_profit_eur"
        ),
        "min_window_profit_eur": export_economics.get(
            "min_window_profit_eur"
        ),
        "initial_export_energy_kwh": export_economics.get(
            "export_energy_kwh"
        ),
        "min_export_energy_kwh": export_economics.get(
            "min_export_energy_kwh"
        ),
        "initial_duration_min": export_economics.get("duration_min"),
        "min_export_window_min": export_economics.get(
            "min_export_window_min"
        ),
    }
    if not bool(
        action in {"eco_plus_export_candidate", "arbitrage_export_candidate"}
        and isinstance(window_id, str)
        and bool(window_id)
        and origin_start_ts > 0
        and end_ts > origin_start_ts
        and all(_policy_finite_contract_number(value) for value in values.values())
        and safe_float(values["min_window_profit_eur"], -1.0) >= 0.0
        and safe_float(values["min_export_energy_kwh"], -1.0) >= 0.0
        and safe_float(values["min_export_window_min"], -1.0) >= 15.0
        and (
            profile != "standard"
            or (
                safe_float(values["initial_expected_profit_eur"], -1.0)
                + 0.000001
                >= safe_float(values["min_window_profit_eur"], 0.0)
                and safe_float(values["initial_export_energy_kwh"], -1.0)
                + 0.000001
                >= safe_float(values["min_export_energy_kwh"], 0.0)
                and safe_float(values["initial_duration_min"], -1.0)
                + 0.000001
                >= safe_float(values["min_export_window_min"], 15.0)
            )
        )
    ):
        return None
    gate = {
        "schema": EXPORT_WINDOW_START_GATE_SCHEMA,
        "passed": True,
        "profile": profile,
        "action": action,
        "window_id": window_id,
        "origin_start_ts": origin_start_ts,
        "end_ts": end_ts,
        **values,
        "accounting_contract": (
            "START_ONLY_NO_REMAINING_WINDOW_REAPPLICATION"
        ),
    }
    if isinstance(business_binding, dict):
        gate["business_window_id"] = window_id
        gate["business_binding"] = copy.deepcopy(business_binding)
        gate["business_contract_sha256"] = business_binding.get(
            "business_contract_sha256"
        )
    return gate


def _policy_export_window_start_gate_valid(
    gate,
    previous_policy,
    export_window,
    export_economics,
    profile,
    current_window_id,
):
    """Prüft einen alten Eintrittsvertrag gegen das aktuelle Geschäftsfenster."""
    previous = previous_policy if isinstance(previous_policy, dict) else {}
    return bool(
        isinstance(gate, dict)
        and gate is previous.get("export_window_start_gate")
        and profile in VALID_PROFIT_PROFILES
        and gate.get("profile") == profile
        and gate.get("action") == str((export_window or {}).get("action") or "")
        and direct_marketing_export_gate_contract_valid(
            previous,
            previous.get("economics"),
            allowed_lineage_statuses={"ACTIVE"},
            current_window_id=current_window_id,
            current_window_end_ts_ms=(export_window or {}).get("end_ts"),
        )
    )


def _policy_export_suspended_window_start_gate_valid(
    gate,
    previous_policy,
    export_window,
    export_economics,
    profile,
    current_window_id,
):
    """Bindet eine wirkungslose Pause an dieselbe unveränderte Gate-Lineage."""
    previous = previous_policy if isinstance(previous_policy, dict) else {}
    return bool(
        isinstance(gate, dict)
        and gate is previous.get("export_window_start_gate")
        and profile in VALID_PROFIT_PROFILES
        and gate.get("profile") == profile
        and gate.get("action") == str((export_window or {}).get("action") or "")
        and direct_marketing_export_gate_contract_valid(
            previous,
            previous.get("economics"),
            allowed_lineage_statuses={"SUSPENDED"},
            current_window_id=current_window_id,
            current_window_end_ts_ms=(export_window or {}).get("end_ts"),
        )
    )


def _policy_export_previous_window_context(
    previous_policy,
    export_window,
    export_economics,
    profile,
    now_ms,
    current_business_binding=None,
):
    """Bindet ACTIVE/SUSPENDED an dasselbe unveränderte Geschäftsfenster."""

    previous = previous_policy if isinstance(previous_policy, dict) else {}
    if not previous:
        return {"matched": False}
    selected = (
        previous.get("selected_window")
        if isinstance(previous.get("selected_window"), dict)
        else {}
    )
    start_gate = previous.get("export_window_start_gate")
    lineage = (
        previous.get("export_window_gate_lineage")
        if isinstance(previous.get("export_window_gate_lineage"), dict)
        else {}
    )
    previous_action = str(
        selected.get("action") or previous.get("source_action") or ""
    )
    previous_target_state = str(
        previous.get("dv_target_state") or ""
    ).strip().upper()
    has_previous_export_contract = bool(
        (
            "export_window_gate_lineage" in previous
            and previous.get("export_window_gate_lineage") is not None
        )
        or (
            "export_window_start_gate" in previous
            and previous.get("export_window_start_gate") is not None
        )
        or previous_action in {
            "eco_plus_export_candidate",
            "arbitrage_export_candidate",
        }
        or previous_target_state in {"FORCE_EXPORT", "HEADROOM_EXPORT"}
    )
    if not has_previous_export_contract:
        # Ein normaler HOLD-/Hausversorgungs-Slot ist kein beschädigter
        # Exportvertrag. Das neu beginnende Verkaufsfenster muss seine eigene
        # Start-Gate-Lineage aufbauen dürfen. Nur tatsächlich vorhandene,
        # widersprüchliche Exportverträge werden unten widerrufen.
        return {"matched": False}
    if not _policy_export_gate_lineage_shape_valid(lineage):
        return {
            "matched": False,
            "revoke": True,
            "revoke_reason_code": "REVOKED_GATE_LINEAGE_INVALID",
            "previous_lineage": dict(lineage) if lineage else None,
        }
    if previous.get("schema") != POLICY_SCHEMA:
        return {
            "matched": False,
            "revoke": True,
            "revoke_reason_code": "REVOKED_POLICY_SCHEMA_CHANGED",
            "previous_lineage": dict(lineage),
            "start_gate": dict(start_gate),
        }
    if _normalize_profit_profile(
        previous.get("profit_profile", "standard")
    ) != profile:
        return {
            "matched": False,
            "revoke": True,
            "revoke_reason_code": "REVOKED_PROFILE_OR_START_THRESHOLDS_CHANGED",
            "previous_lineage": dict(lineage),
            "start_gate": dict(start_gate),
        }

    current_plan_window_id = _policy_window_id(export_window or {})
    previous_window_id = str(
        selected.get("window_id") or previous.get("window_id") or ""
    )
    previous_end_ts = safe_int(
        selected.get("end_ts", previous.get("end_ts")),
        0,
    )
    current_start_ts = safe_int((export_window or {}).get("start_ts"), 0)
    current_end_ts = safe_int((export_window or {}).get("end_ts"), 0)
    distinct_current_window = bool(
        previous_window_id
        and current_plan_window_id
        and previous_window_id != current_plan_window_id
        and previous_end_ts > 0
        and current_start_ts >= previous_end_ts
        and current_start_ts <= safe_int(now_ms, 0) < current_end_ts
    )
    previous_lineage_valid = _policy_export_gate_lineage_valid(
        lineage,
        start_gate,
        {"ACTIVE", "SUSPENDED", "REVOKED"},
    )
    previous_identity_valid = bool(
        previous_lineage_valid
        and previous_action == lineage.get("action")
        and previous_window_id == lineage.get("window_id")
        and previous_end_ts == safe_int(lineage.get("end_ts"), 0)
        and safe_int(
            previous.get(
                "window_origin_start_ts",
                selected.get("start_ts", previous.get("start_ts")),
            ),
            0,
        )
        == safe_int(lineage.get("origin_start_ts"), 0)
    )
    if distinct_current_window and previous_identity_valid:
        # Ein vollständig neues, nicht überlappendes Geschäftsfenster erhält
        # immer eine eigene Start-Gate-Lineage. Der regulär beendete Vertrag
        # des vorherigen Fensters ist weder Fortsetzung noch Widerrufsgrund –
        # auch dann nicht, wenn dessen vollständig gebundene Lineage bereits
        # terminal REVOKED ist. Eine manipulierte oder intern widersprüchliche
        # Vorgänger-Lineage darf diese Trennung dagegen nicht öffnen.
        return {
            "matched": False,
            "previous_window_final": True,
            "previous_window_id": previous_window_id,
            "previous_window_end_ts": previous_end_ts,
        }
    if lineage.get("status") == "REVOKED":
        return {
            "matched": False,
            "revoke": True,
            "revoke_reason_code": "REVOKED_LINEAGE_ALREADY_FINAL",
            "previous_lineage": dict(lineage),
        }
    if not previous_lineage_valid:
        return {
            "matched": False,
            "revoke": True,
            "revoke_reason_code": "REVOKED_GATE_HASH_TYPE_OR_SCHEMA_INVALID",
            "previous_lineage": dict(lineage),
        }
    threshold_keys = (
        "min_window_profit_eur",
        "min_export_energy_kwh",
        "min_export_window_min",
    )
    if any(
        not _policy_finite_contract_number(start_gate.get(key))
        or not _policy_finite_contract_number(export_economics.get(key))
        or abs(
            float(start_gate.get(key))
            - float(export_economics.get(key))
        ) > 0.000001
        for key in threshold_keys
    ):
        return {
            "matched": False,
            "revoke": True,
            "revoke_reason_code": (
                "REVOKED_PROFILE_OR_START_THRESHOLDS_CHANGED"
            ),
            "previous_lineage": dict(lineage),
            "start_gate": dict(start_gate),
        }

    current_action = str((export_window or {}).get("action") or "")
    start_business_binding = (
        start_gate.get("business_binding")
        if isinstance(start_gate, dict)
        and isinstance(start_gate.get("business_binding"), dict)
        else None
    )
    stable_suffix = _policy_export_suffix_matches(
        start_business_binding,
        current_business_binding,
    )
    current_window_id = (
        previous_window_id if stable_suffix else current_plan_window_id
    )
    if safe_float(now_ms, 0.0) >= safe_float(start_gate.get("end_ts"), 0.0):
        return {
            "matched": False,
            "revoke": True,
            "revoke_reason_code": "REVOKED_WINDOW_EXPIRED",
            "previous_lineage": dict(lineage),
            "start_gate": dict(start_gate),
        }
    if not bool(
        previous_action == current_action == lineage.get("action")
        and previous_window_id
        and current_window_id
        and previous_window_id == current_window_id
        == lineage.get("window_id")
        and previous_end_ts > 0.0
        and current_end_ts > 0.0
        and abs(previous_end_ts - current_end_ts) <= 1000.0
        and safe_int(start_gate.get("end_ts"), 0) == int(current_end_ts)
    ):
        return {
            "matched": False,
            "revoke": True,
            "revoke_reason_code": "REVOKED_WINDOW_ACTION_OR_END_CHANGED",
            "previous_lineage": dict(lineage),
            "start_gate": dict(start_gate),
        }

    origin_start_ts = safe_float(
        previous.get("window_origin_start_ts", selected.get("start_ts", previous.get("start_ts"))),
        0.0,
    )
    if not bool(
        origin_start_ts > 0.0
        and int(origin_start_ts) == lineage.get("origin_start_ts")
        and safe_float(now_ms, 0.0) >= origin_start_ts
    ):
        return {
            "matched": False,
            "revoke": True,
            "revoke_reason_code": "REVOKED_WINDOW_ORIGIN_CHANGED",
            "previous_lineage": dict(lineage),
            "start_gate": dict(start_gate),
        }
    status = lineage.get("status")
    start_gate_valid = (
        _policy_export_window_start_gate_valid(
            start_gate,
            previous,
            export_window,
            export_economics,
            profile,
            current_window_id,
        )
        if status == "ACTIVE"
        else _policy_export_suspended_window_start_gate_valid(
            start_gate,
            previous,
            export_window,
            export_economics,
            profile,
            current_window_id,
        )
    )
    if not start_gate_valid:
        return {
            "matched": False,
            "revoke": True,
            "revoke_reason_code": (
                "REVOKED_POLICY_OR_EXECUTION_AMBIGUITY"
                if status == "ACTIVE"
                else "REVOKED_SUSPENSION_CONTRACT_INVALID"
            ),
            "previous_lineage": dict(lineage),
            "start_gate": dict(start_gate),
        }
    return {
        "matched": True,
        "origin_start_ts": int(origin_start_ts),
        "end_ts": int(current_end_ts),
        "action": current_action,
        "window_id": previous_window_id or current_window_id,
        "plan_window_id": current_plan_window_id,
        "start_gate_valid": start_gate_valid,
        "start_gate": dict(start_gate) if start_gate_valid else None,
        "previous_lineage": dict(lineage),
        "previous_lineage_status": status,
        "recovery": status == "SUSPENDED",
    }


def _policy_export_lineage_effectless_decision(
    decision,
    previous_policy,
    reserve_policy,
    status,
    reason_codes,
    current_economics=None,
):
    """Materialisiert SUSPENDED/REVOKED ohne Budget oder Ausführungswirkung."""

    if status not in {"SUSPENDED", "REVOKED"}:
        return decision
    previous = previous_policy if isinstance(previous_policy, dict) else {}
    previous_budget = (
        previous.get("storage_budget")
        if isinstance(previous.get("storage_budget"), dict)
        else {}
    )
    gate = (
        previous.get("export_window_start_gate")
        if isinstance(previous.get("export_window_start_gate"), dict)
        else None
    )
    previous_lineage = (
        previous.get("export_window_gate_lineage")
        if isinstance(previous.get("export_window_gate_lineage"), dict)
        else {}
    )
    lineage = None
    gate_valid = _policy_export_gate_lineage_valid(
        previous_lineage,
        gate,
        {"ACTIVE", "SUSPENDED"},
    )
    if gate_valid:
        lineage = _policy_export_gate_lineage_contract(
            gate,
            status,
            reason_codes,
            previous_lineage=previous_lineage,
        )
    elif status == "REVOKED" and _policy_export_gate_lineage_shape_valid(
        previous_lineage,
        {"ACTIVE", "SUSPENDED", "REVOKED"},
    ):
        lineage = dict(previous_lineage)
        lineage["status"] = "REVOKED"
        lineage["transition_reason_codes"] = sorted({
            str(reason).strip()
            for reason in reason_codes or []
            if str(reason).strip()
        }) or ["REVOKED_GATE_HASH_TYPE_OR_SCHEMA_INVALID"]
    elif status == "REVOKED" and isinstance(gate, dict):
        # Eine typisiert beschädigte Lineage darf nicht fortgesetzt werden.
        # Der weiterhin vorhandene Gate-Preimage erlaubt aber einen neuen,
        # wirkungslosen REVOKED-Tombstone ohne Ausführungsautorität.
        lineage = _policy_export_gate_lineage_contract(
            gate,
            "REVOKED",
            reason_codes or ["REVOKED_GATE_HASH_TYPE_OR_SCHEMA_INVALID"],
        )
    if not isinstance(lineage, dict):
        return decision

    selected = (
        dict(previous.get("selected_window"))
        if isinstance(previous.get("selected_window"), dict)
        else {
            "action": lineage.get("action"),
            "window_id": lineage.get("window_id"),
            "start_ts": lineage.get("origin_start_ts"),
            "end_ts": lineage.get("end_ts"),
        }
    )
    selected.update({
        "action": lineage.get("action"),
        "window_id": lineage.get("window_id"),
        "start_ts": lineage.get("origin_start_ts"),
        "end_ts": lineage.get("end_ts"),
    })
    reason_text = ",".join(lineage.get("transition_reason_codes") or [])
    decision.update({
        "profit_profile": previous.get(
            "profit_profile",
            decision.get("profit_profile", "standard"),
        ),
        "commands_allowed": False,
        "dv_target_state": "HOLD",
        "storage_budget": {
            "export_budget_w": 0,
            "charge_budget_w": 0,
            "protected_reserve_wh": round(
                max(
                    0.0,
                    safe_float(
                        reserve_policy.get("protected_energy_wh"),
                        safe_float(
                            previous_budget.get("protected_reserve_wh"),
                            0.0,
                        ),
                    ),
                ),
                0,
            ),
            "sellable_wh": round(
                max(0.0, safe_float(reserve_policy.get("sellable_wh"), 0.0)),
                0,
            ),
        },
        "blocked": True,
        "block_reason": "%s:%s" % (status.lower(), reason_text),
        "continuation_active": False,
        "continuation_reason_code": None,
        "export_window_start_gate": (
            dict(gate)
            if _policy_export_gate_lineage_valid(
                lineage,
                gate,
                {status},
            )
            else None
        ),
        "export_window_gate_lineage": lineage,
        "window_origin_start_ts": lineage.get("origin_start_ts"),
        "window_id": lineage.get("window_id"),
        "economics": (
            dict(current_economics)
            if isinstance(current_economics, dict)
            else dict(previous.get("economics") or {})
        ),
        "selected_window": selected,
        "executable_action": None,
    })
    return decision


def _policy_export_lineage_previous_status(previous_policy):
    previous = previous_policy if isinstance(previous_policy, dict) else {}
    lineage = (
        previous.get("export_window_gate_lineage")
        if isinstance(previous.get("export_window_gate_lineage"), dict)
        else {}
    )
    gate = previous.get("export_window_start_gate")
    if not _policy_export_gate_lineage_shape_valid(lineage):
        return None
    if lineage.get("status") == "REVOKED":
        return "REVOKED"
    if not _policy_export_gate_lineage_valid(
        lineage,
        gate,
        {"ACTIVE", "SUSPENDED"},
    ):
        return "INVALID"
    return lineage.get("status")


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
    previous_lineage_status = _policy_export_lineage_previous_status(
        previous_policy_decision
    )

    def lineage_effectless(status, reason_codes, current_economics=None):
        return _policy_export_lineage_effectless_decision(
            decision,
            previous_policy_decision,
            reserve_policy,
            status,
            reason_codes,
            current_economics=current_economics,
        )

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
        if previous_lineage_status in {"ACTIVE", "SUSPENDED"}:
            return lineage_effectless(
                "REVOKED" if "disabled" in quality_blocks else "SUSPENDED",
                [
                    "REVOKED_USER_OR_EXPORT_DISABLED"
                    if "disabled" in quality_blocks
                    else "SUSPENDED_PRICE_OR_REVISION_EVIDENCE_INCOMPLETE"
                ],
            )
        if previous_lineage_status == "INVALID":
            return lineage_effectless(
                "REVOKED",
                ["REVOKED_GATE_HASH_TYPE_OR_SCHEMA_INVALID"],
            )
        decision["block_reason"] = "price_quality_blocked:%s" % ",".join(sorted(set(quality_blocks)))
        decision["blocked"] = True
        return decision

    if not cfg_bool(flags.get("live_soc_valid"), True):
        if previous_lineage_status in {"ACTIVE", "SUSPENDED"}:
            return lineage_effectless(
                "SUSPENDED",
                ["SUSPENDED_LIVE_OR_RUNTIME_EVIDENCE_INCOMPLETE"],
            )
        decision["block_reason"] = "live_values_missing:current_soc"
        decision["blocked"] = True
        return decision

    if safe_float(capacity_wh, 0.0) <= 0.0:
        if previous_lineage_status in {"ACTIVE", "SUSPENDED"}:
            return lineage_effectless(
                "SUSPENDED",
                ["SUSPENDED_INPUT_OR_FORECAST_EVIDENCE_INCOMPLETE"],
            )
        decision["block_reason"] = "live_values_missing:capacity_wh"
        decision["blocked"] = True
        return decision

    if mode == "safe" or not cfg_bool(flags.get("commands_allowed"), False):
        if previous_lineage_status in {"ACTIVE", "SUSPENDED"}:
            if mode == "safe" or not cfg_bool(flags.get("export_enable"), False):
                return lineage_effectless(
                    "REVOKED",
                    ["REVOKED_USER_MODE_EXPORT_OR_OWNER_CHANGED"],
                )
            return lineage_effectless(
                "SUSPENDED",
                ["SUSPENDED_INPUT_OR_FORECAST_EVIDENCE_INCOMPLETE"],
            )
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
        if negative_window is None:
            decision["dv_target_state"] = "HOLD"
            decision["block_reason"] = "Negative price observed, but no physically allocated PV storage slot is executable"
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
        charge_w = max(0.0, safe_float(negative_window.get("max_power_w"), 0.0))
        if charge_w < 300.0:
            decision["dv_target_state"] = "HOLD"
            decision["block_reason"] = "Negative price observed, but allocated PV storage power is below dispatch minimum"
            decision["blocked"] = False
            return decision
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
                "window_id": _policy_window_id(negative_window or {}),
                "target_soc_pct": (negative_window or {}).get("target_soc_pct"),
                "pv_store_source_contract": (negative_window or {}).get("pv_store_source_contract"),
                "pv_store_dc_only_enable": (negative_window or {}).get("pv_store_dc_only_enable"),
                "pv_store_aux_ac_storage_enable": (negative_window or {}).get("pv_store_aux_ac_storage_enable"),
                "pv_store_aux_ac_storage_allowed": (negative_window or {}).get("pv_store_aux_ac_storage_allowed"),
                "pv_store_dc_forecast_complete": (negative_window or {}).get("pv_store_dc_forecast_complete"),
                "pv_store_dc_forecast_deficit_wh": (negative_window or {}).get("pv_store_dc_forecast_deficit_wh"),
                "pv_store_live_dc_fallback": (negative_window or {}).get("pv_store_live_dc_fallback"),
                "pv_store_live_dc_fallback_contract_version": (negative_window or {}).get("pv_store_live_dc_fallback_contract_version"),
                "pv_store_runtime_measurement_required": (negative_window or {}).get("pv_store_runtime_measurement_required"),
                "pv_store_runtime_source_contract": (negative_window or {}).get("pv_store_runtime_source_contract"),
                "pv_store_live_dc_fallback_max_power_w": (negative_window or {}).get("pv_store_live_dc_fallback_max_power_w"),
                "pv_store_raw_market_price_ct_kwh": (negative_window or {}).get("pv_store_raw_market_price_ct_kwh"),
                "pv_store_raw_market_price_source": (negative_window or {}).get("pv_store_raw_market_price_source"),
                "pv_store_raw_market_price_resolution_min": (negative_window or {}).get("pv_store_raw_market_price_resolution_min"),
                "pv_store_market_price_revision_sha256": (negative_window or {}).get("pv_store_market_price_revision_sha256"),
                "market_window_id": (negative_window or {}).get("market_window_id"),
                "market_window_start_ts": (negative_window or {}).get("market_window_start_ts"),
                "market_window_end_ts": (negative_window or {}).get("market_window_end_ts"),
                "grid_ac_allowed": (negative_window or {}).get("grid_ac_allowed"),
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
                "window_id": _policy_window_id(headroom_window),
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
                    or "storage_manager"
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
                "window_id": _policy_window_id(pv_store_window),
                "target_soc_pct": pv_store_window.get("target_soc_pct"),
                "export_constraint_class": constraint_class,
                "hard_export_limit_active": hard_export_limit,
                "hard_export_limit_w": pv_store_window.get("hard_export_limit_w") if hard_export_limit else None,
                "export_constraint_scope": str(pv_store_window.get("export_constraint_scope") or "storage_priority"),
                "pv_export_allowed": pv_export_allowed,
                "pv_store_source_contract": pv_store_window.get("pv_store_source_contract"),
                "pv_store_dc_only_enable": pv_store_window.get("pv_store_dc_only_enable"),
                "pv_store_aux_ac_storage_enable": pv_store_window.get("pv_store_aux_ac_storage_enable"),
                "pv_store_aux_ac_storage_allowed": pv_store_window.get("pv_store_aux_ac_storage_allowed"),
                "pv_store_dc_forecast_complete": pv_store_window.get("pv_store_dc_forecast_complete"),
                "pv_store_dc_forecast_deficit_wh": pv_store_window.get("pv_store_dc_forecast_deficit_wh"),
                "pv_store_live_dc_fallback": pv_store_window.get("pv_store_live_dc_fallback"),
                "pv_store_live_dc_fallback_contract_version": pv_store_window.get("pv_store_live_dc_fallback_contract_version"),
                "pv_store_runtime_measurement_required": pv_store_window.get("pv_store_runtime_measurement_required"),
                "pv_store_runtime_source_contract": pv_store_window.get("pv_store_runtime_source_contract"),
                "pv_store_live_dc_fallback_max_power_w": pv_store_window.get("pv_store_live_dc_fallback_max_power_w"),
                "pv_store_raw_market_price_ct_kwh": pv_store_window.get("pv_store_raw_market_price_ct_kwh"),
                "pv_store_raw_market_price_source": pv_store_window.get("pv_store_raw_market_price_source"),
                "pv_store_raw_market_price_resolution_min": pv_store_window.get("pv_store_raw_market_price_resolution_min"),
                "pv_store_market_price_revision_sha256": pv_store_window.get("pv_store_market_price_revision_sha256"),
                "market_window_id": pv_store_window.get("market_window_id"),
                "market_window_start_ts": pv_store_window.get("market_window_start_ts"),
                "market_window_end_ts": pv_store_window.get("market_window_end_ts"),
                "grid_ac_allowed": pv_store_window.get("grid_ac_allowed"),
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
        current_business_binding = _policy_export_business_binding(
            annotated,
            export_window,
            config,
            flags,
            profile,
            mode,
        )
        previous_window = _policy_export_previous_window_context(
            previous_policy_decision,
            export_window,
            export_economics,
            profile,
            now_ms,
            current_business_binding=current_business_binding,
        )
        if previous_window.get("revoke"):
            return lineage_effectless(
                "REVOKED",
                [
                    previous_window.get("revoke_reason_code")
                    or "REVOKED_POLICY_OR_EXECUTION_AMBIGUITY"
                ],
                current_economics=export_economics,
            )
        continuous_allowed, continuous_reason = (
            _policy_export_continuous_allowed(profile, export_economics)
        )
        start_allowed, start_reason = _policy_export_start_allowed(
            profile,
            export_economics,
        )
        if previous_window.get("matched") and not continuous_allowed:
            pause_reason = (
                "SUSPENDED_RESERVE_SOC_OR_SELLABLE_INSUFFICIENT"
                if "Reserve" in continuous_reason
                else "SUSPENDED_CURRENT_MARGIN_BELOW_LIMIT"
            )
            return lineage_effectless(
                "SUSPENDED",
                [pause_reason],
                current_economics=export_economics,
            )
        allowed = bool(continuous_allowed and start_allowed)
        reason = continuous_reason if not continuous_allowed else start_reason
        if allowed:
            reason = (
                "Expert: margin check bypassed, hard reserve kept"
                if profile == "expert"
                else "Aggressive: positive net profit after efficiency"
                if profile == "aggressive"
                else "Profit export allowed"
            )
        continuation_active = bool(
            previous_window.get("matched")
            and previous_window.get("start_gate_valid")
            and continuous_allowed
            and not start_allowed
        )
        if continuation_active:
            allowed = True
            reason = (
                "Profit export continuation: window start gates already satisfied"
            )
        execution_release = bool(
            mode in {"eco_plus", "arbitrage"}
            and cfg_bool(flags.get("export_enable"), False)
        )
        if previous_window.get("matched") and not execution_release:
            return lineage_effectless(
                "REVOKED",
                ["REVOKED_USER_MODE_EXPORT_OR_OWNER_CHANGED"],
                current_economics=export_economics,
            )
        allowed = bool(allowed and execution_release)
        window_id = str(
            previous_window.get("window_id")
            or _policy_export_business_window_id(current_business_binding)
            or _policy_window_id(export_window)
            or "export:%d"
            % int(safe_float(export_window.get("end_ts"), now_ms + SLOT_MS))
        )
        export_window_start_gate = None
        export_window_gate_lineage = None
        if allowed and profile in VALID_PROFIT_PROFILES:
            export_window_start_gate = (
                dict(previous_window.get("start_gate"))
                if previous_window.get("start_gate_valid")
                and isinstance(previous_window.get("start_gate"), dict)
                else _policy_export_window_start_gate_contract(
                    profile,
                    export_window,
                    export_economics,
                    window_id,
                    business_binding=current_business_binding,
                )
            )
            if export_window_start_gate is None:
                allowed = False
                continuation_active = False
                reason = "Blocked by Evidence: export window start gate invalid"
        export_w = 0
        if allowed:
            duration_h = max(0.001, _entry_duration_h(export_window))
            export_w = int(round(min(
                max(0.0, safe_float(export_window.get("max_power_w"), safe_float(flags.get("max_export_w"), 0.0))),
                (safe_float(export_economics.get("export_energy_kwh"), 0.0) * 1000.0) / duration_h,
            )))
        if allowed and export_w <= 0:
            if previous_window.get("matched"):
                return lineage_effectless(
                    "SUSPENDED",
                    ["SUSPENDED_PHYSICAL_ZERO_OR_DIRECTION_GUARD"],
                    current_economics=export_economics,
                )
            allowed = False
            continuation_active = False
            reason = "Blocked by Evidence: physical export budget is 0 W"
        if allowed and profile in VALID_PROFIT_PROFILES:
            previous_lineage = previous_window.get("previous_lineage")
            previous_status = previous_window.get("previous_lineage_status")
            transition_reasons = (
                ["RECOVERED_ALL_CURRENT_GATES_GREEN"]
                if previous_status == "SUSPENDED"
                else ["ACTIVE_REPLAN_ALL_CURRENT_GATES_GREEN"]
                if previous_status == "ACTIVE"
                else ["RELEASED_START_GATES_SATISFIED"]
            )
            export_window_gate_lineage = _policy_export_gate_lineage_contract(
                export_window_start_gate,
                "ACTIVE",
                transition_reasons,
                previous_lineage=previous_lineage,
            )
            if export_window_gate_lineage is None:
                if previous_window.get("matched"):
                    return lineage_effectless(
                        "REVOKED",
                        ["REVOKED_GATE_HASH_TYPE_OR_SCHEMA_INVALID"],
                        current_economics=export_economics,
                    )
                allowed = False
                continuation_active = False
                export_w = 0
                reason = "Blocked by Evidence: export gate lineage invalid"
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
            "continuation_reason_code": (
                "WINDOW_START_GATES_ALREADY_SATISFIED"
                if continuation_active
                else None
            ),
            "export_window_start_gate": export_window_start_gate,
            "export_window_gate_lineage": export_window_gate_lineage,
            "window_origin_start_ts": int(
                previous_window.get("origin_start_ts")
                if previous_window.get("matched")
                else safe_float(export_window.get("start_ts"), now_ms)
            ),
            "window_id": window_id,
            "economics": export_economics,
            "selected_window": {
                "window_id": window_id,
                "business_window_id": window_id,
                "plan_window_id": (
                    previous_window.get("plan_window_id")
                    or _policy_window_id(export_window)
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

    house_supply_window = _policy_current_window(
        windows,
        now_ms,
        {"eco_house_supply", "eco_plus_house_supply", "safe_house_supply"},
    )
    if house_supply_window:
        decision.update({
            # Hausversorgung ist ein vollständig geplanter, aber passiver
            # Abschnitt. Die Direktvermarktung gibt den Speicher frei; der
            # normale Storage Manager beziehungsweise E3/DC-AUTO entscheidet.
            "commands_allowed": False,
            "dv_target_state": "NORMAL",
            "block_reason": "Hausversorgung: Speicherregelung bleibt im normalen AUTO-Pfad",
            "blocked": False,
            "selected_window": {
                "action": house_supply_window.get("action"),
                "reason": house_supply_window.get("reason"),
                "start_ts": house_supply_window.get("start_ts"),
                "end_ts": house_supply_window.get("end_ts"),
                "window_id": _policy_window_id(house_supply_window),
            },
        })
        return decision

    if not windows:
        decision["block_reason"] = "Hold: no policy candidate window"
        decision["blocked"] = True
    else:
        decision.update({
            "commands_allowed": False,
            "dv_target_state": "NORMAL",
            "block_reason": "Hausversorgung: bis zum nächsten aktiven Planfenster AUTO",
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


def _policy_window_id(window):
    """Liefert auch für Nicht-Preisplateaus eine stabile Fensteridentität."""

    if not isinstance(window, dict):
        return ""
    explicit = str(
        window.get("export_plateau_id")
        or window.get("market_window_id")
        or window.get("window_id")
        or ""
    )
    if explicit:
        return explicit
    action = str(window.get("action") or "")
    start_ts = safe_int(window.get("start_ts"), 0)
    end_ts = safe_int(window.get("end_ts"), 0)
    if not action or start_ts <= 0 or end_ts <= start_ts:
        return ""
    material = {
        "action": action,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "reason": str(window.get("reason") or ""),
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return "policy-window:%s" % digest[:24]


def _enrich_policy_candidate_contract(decision, windows, now_ms):
    """Stellt Kandidat-, Auswahl- und Ausführungsrollen ohne Prioritätsänderung bereit."""
    result = dict(decision or {})
    active_windows = _policy_active_candidate_windows(windows, now_ms)
    candidate_actions = []
    for window in active_windows:
        action = str(window.get("action") or "")
        if action and action not in candidate_actions:
            candidate_actions.append(action)

    selected = (
        dict(result.get("selected_window"))
        if isinstance(result.get("selected_window"), dict)
        else None
    )
    selected_action = str((selected or {}).get("action") or "")
    selected_end_ts = safe_int((selected or {}).get("end_ts"), 0)
    selected_window_id = _policy_window_id(selected or {}) or str(
        result.get("window_id") or ""
    )
    selected_plan_window_id = str(
        (selected or {}).get("plan_window_id") or selected_window_id
    )
    if selected is not None and selected_window_id:
        selected["window_id"] = selected_window_id
        result["selected_window"] = selected
    execution_matches = []
    for window in active_windows:
        if str(window.get("action") or "") != selected_action:
            continue
        window_end_ts = safe_int(window.get("end_ts"), 0)
        if selected_end_ts > 0 and window_end_ts != selected_end_ts:
            continue
        window_id = _policy_window_id(window)
        if (
            selected_plan_window_id
            and window_id
            and selected_plan_window_id != window_id
        ):
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
            "window_id": selected_window_id or _policy_window_id(plan_window),
            "plan_window_id": _policy_window_id(plan_window),
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
        target_state in DIRECT_MARKETING_ACTIVE_TARGETS
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
    elif target_state in {"FORCE_CHARGE_PV", "DV_CURVE_CHARGE"}:
        executable = executable and safe_float(budget.get("charge_budget_w"), 0.0) > 0.0
    elif target_state not in DIRECT_MARKETING_ACTIVE_TARGETS:
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

    if target_state in {"FORCE_CHARGE_PV", "DV_CURVE_CHARGE"}:
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


def _build_charge_block_wait_policy_slots(
    policy_timeline,
    annotated,
    flags,
    mode,
    now_ms,
):
    """Materialisiert nur explizite Headroom-Warteslots.

    Der Producer prüft den lückenlosen 15-Minuten-Plan semantisch. Neutrale
    NORMAL-/HOLD-Abschnitte bleiben passive Hausversorgung und erzeugen keinen
    Speicherbefehl. Nur ein bereits typisierter Headroom-Hold vor einem
    konkreten Aufnahmefenster wird als CHARGE_BLOCK_WAIT materialisiert.
    Bestehende PV- und Exportentscheidungen bleiben eigene typisierte Slots.
    """

    report = {
        "schema": "direct_marketing_charge_block_wait_plan_v1",
        "complete": False,
        "slot_duration_ms": SLOT_MS,
        "slot_count": 0,
        "contiguous_900s": False,
        "wait_slot_count": 0,
        "neutral_slot_count": 0,
        "synthesized_neutral_slot_count": 0,
        "existing_action_slot_count": 0,
        "action_gap_slot_count": 0,
        "reason": "not_applicable",
    }
    if mode != "eco_plus" or not cfg_bool(flags.get("commands_allowed"), False):
        report["reason"] = "strategy_not_released"
        return [], [], report
    if not cfg_bool(flags.get("pv_store_enable"), False):
        # Ohne freigegebene PV-Speicherstrategie gibt es kein gebundenes
        # späteres Aufnahmefenster. Ein ganztägiger Ladeblock würde die
        # Nutzer-Aus-Semantik verletzen und könnte die normale PV-Ladung
        # unnötig verhindern.
        report["reason"] = "pv_store_strategy_disabled"
        return [], [], report

    slots = sorted(
        (
            {
                "start_ts": safe_int(_slot_ts(item), 0),
                "end_ts": safe_int(_slot_end_ts(item), 0),
            }
            for item in (annotated or [])
            if isinstance(item, dict)
        ),
        key=lambda item: item["start_ts"],
    )
    slots = [
        item for item in slots
        if item["start_ts"] > 0
        and item["end_ts"] - item["start_ts"] == SLOT_MS
        and item["end_ts"] > now_ms
    ]
    if not slots:
        report["reason"] = "planned_900s_slots_missing"
        return [], [], report
    contiguous = all(
        right["start_ts"] == left["end_ts"]
        for left, right in zip(slots, slots[1:])
    )
    report.update({
        "slot_count": len(slots),
        "contiguous_900s": contiguous,
    })
    if not contiguous:
        report["reason"] = "planned_900s_slots_not_contiguous"
        return [], [], report

    decisions = [
        item for item in (policy_timeline or [])
        if isinstance(item, dict)
    ]


    def decisions_for_slot(slot):
        return [
            item for item in decisions
            if safe_int(item.get("start_ts"), 0) <= slot["start_ts"]
            and slot["end_ts"] <= safe_int(item.get("end_ts"), 0)
        ]

    slot_decisions = [(slot, decisions_for_slot(slot)) for slot in slots]
    waits = []
    synthesized_neutral_slots = []
    neutral_slots = 0
    existing_action_slots = 0
    action_gaps = 0
    for slot, matches in slot_decisions:
        if len(matches) > 1:
            # Eine überlappende Policy-Auswahl wäre eine zweite implizite
            # Entscheiderkante. Sie bleibt ein sichtbarer Safety-Gap.
            action_gaps += 1
            continue
        decision = matches[0] if matches else None
        if not isinstance(decision, dict):
            # Ein vorhandener, vollständig validierter 900-s-Inputslot ohne
            # gerichtete Policy-Aktion ist semantisch Hausversorgung/AUTO.
            # Er ist keine Lücke und rechtfertigt keinen Hardware-Intent.
            neutral = _policy_empty_decision("", {}, flags)
            neutral.update({
                "commands_allowed": False,
                "dv_target_state": "NORMAL",
                "blocked": False,
                "block_reason": "Hausversorgung: Speicherregelung bleibt im normalen AUTO-Pfad",
                "start_ts": slot["start_ts"],
                "end_ts": slot["end_ts"],
                "source_action": "eco_plus_house_supply",
                "source_reason": "neutral_dv_slot",
                "executable_action": None,
                "execution_window": None,
                "selected_window": {
                    "action": "eco_plus_house_supply",
                    "reason": "neutral_dv_slot",
                    "start_ts": slot["start_ts"],
                    "end_ts": slot["end_ts"],
                },
            })
            synthesized_neutral_slots.append(neutral)
            neutral_slots += 1
            continue
        target = str(decision.get("dv_target_state") or "").upper()
        storage_budget = (
            decision.get("storage_budget")
            if isinstance(decision.get("storage_budget"), dict)
            else {}
        )
        headroom_hold = bool(
            target == "HEADROOM_EXPORT"
            and (
                storage_budget.get("headroom_hold_active") is True
                or safe_float(storage_budget.get("export_budget_w"), 0.0) < 300.0
            )
        )
        if target in {"FORCE_EXPORT", "FORCE_CHARGE_PV", "HEADROOM_EXPORT", "DV_CURVE_CHARGE"} and not headroom_hold:
            selected = (
                decision.get("selected_window")
                if isinstance(decision.get("selected_window"), dict)
                else {}
            )
            execution = (
                decision.get("execution_window")
                if isinstance(decision.get("execution_window"), dict)
                else {}
            )
            selected_action = str(selected.get("action") or "")
            execution_start = safe_int(execution.get("start_ts"), 0)
            execution_end = safe_int(execution.get("end_ts"), 0)
            execution_valid = bool(
                direct_marketing_typed_int_equals(
                    execution.get("contract_version"),
                    1,
                )
                and execution.get("action") == selected_action
                and str(decision.get("source_action") or "") == selected_action
                and str(decision.get("executable_action") or "") == selected_action
                and execution_start <= slot["start_ts"] < slot["end_ts"] <= execution_end
            )
            if (
                decision.get("commands_allowed") is True
                and decision.get("blocked") is False
                and execution_valid
            ):
                existing_action_slots += 1
                continue
            action_gaps += 1
            continue
        if (
            target not in {"HOLD", "NORMAL"}
            and not headroom_hold
        ) or decision.get("blocked") is True:
            action_gaps += 1
            continue
        if target in {"HOLD", "NORMAL"} and not headroom_hold:
            neutral_slots += 1
            continue
        window_id = "charge-block-wait:%d" % slot["start_ts"]
        selected_window = {
            "action": "direct_marketing_charge_block_wait",
            "reason": "headroom_reservation_hold",
            "start_ts": slot["start_ts"],
            "end_ts": slot["end_ts"],
            "window_id": window_id,
        }
        wait = dict(decision)
        wait.update({
            "commands_allowed": True,
            "dv_target_state": "CHARGE_BLOCK_WAIT",
            "storage_budget": {
                **(
                    decision.get("storage_budget")
                    if isinstance(decision.get("storage_budget"), dict)
                    else {}
                ),
                "export_budget_w": 0,
                "charge_budget_w": 0,
            },
            "blocked": False,
            "block_reason": (
                "Typisierter Headroom-Halteslot mit Ladesperre"
            ),
            "selected_window": selected_window,
            "source_action": "direct_marketing_charge_block_wait",
            "executable_action": "direct_marketing_charge_block_wait",
            "execution_window": {
                "contract_version": 1,
                "action": "direct_marketing_charge_block_wait",
                "start_ts": slot["start_ts"],
                "end_ts": slot["end_ts"],
                "plan_window_start_ts": slot["start_ts"],
                "plan_window_end_ts": slot["end_ts"],
                "origin_start_ts": slot["start_ts"],
                "window_id": window_id,
                "source": "active_plan_window",
            },
        })
        waits.append(wait)

    report.update({
        "complete": (
            action_gaps == 0
            and len(slots) == len(waits) + neutral_slots + existing_action_slots
        ),
        "wait_slot_count": len(waits),
        "neutral_slot_count": neutral_slots,
        "synthesized_neutral_slot_count": len(synthesized_neutral_slots),
        "existing_action_slot_count": existing_action_slots,
        "action_gap_slot_count": action_gaps,
        "reason": "ok" if action_gaps == 0 else "typed_action_coverage_incomplete",
    })
    if not report["complete"]:
        return [], [], report
    return waits, synthesized_neutral_slots, report


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
        "pv_store_allocation": _empty_pv_store_allocation_diagnostic(
            plan_flags,
            reason,
        ),
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
    lcos_ct = _configured_optional_float(config, "direct_marketing_lcos_ct_per_kwh")
    if lcos_ct is None:
        lcos_ct = degradation
    lcos_ct = max(0.0, safe_float(lcos_ct, degradation))
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
            "lcos_ct_per_kwh": round(lcos_ct, 2),
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
    pv_shift_opportunity = best_low_market["net_sell_ct"]
    pv_shift_cost_basis = abs(pv_shift_opportunity) + lcos_ct + max(0.0, safety_margin)
    pv_shift_spread = pv_shift_revenue - pv_shift_opportunity - lcos_ct - safety_margin
    pv_shift_margin = (pv_shift_spread / max(1.0, pv_shift_cost_basis)) * 100.0
    pv_shift_profit_ok = bool(pv_shift_spread >= min_profit_ct and pv_shift_margin >= min_margin_pct)

    return {
        "roundtrip_efficiency_pct": round(efficiency_pct, 1),
        "battery_cost_ct_per_kwh": round(degradation, 2),
        "lcos_ct_per_kwh": round(lcos_ct, 2),
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
        "market_price_revision": slot.get("market_price_revision"),
    }
    for key in (
        "pv_w",
        "e3dc_pv_w",
        "external_pv_w",
        "has_e3dc_pv_forecast",
        "e3dc_pv_forecast_source",
        "home_w",
        "wp_w",
        "heater_w",
        "wallbox_w",
        "forecast_surplus_w",
        "forecast_dc_surplus_w",
        "forecast_deficit_w",
        "has_power_forecast",
        "pv_topology_status",
        "pv_topology_quality",
        "pv_resource_projection_status",
        "pv_topology_revision",
        "pv_store_source_split_complete",
        "pv_store_forecast_fresh",
        "pv_store_forecast_freshness_source",
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
        "market_price_revision",
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


def _forecast_deficit_slot_allocations(annotated, start_ts, end_ts, enabled=True):
    """Zerlegt die Lastreserve kanonisch in nicht überlappende Forecast-Slots."""

    if not enabled or not annotated:
        return {}
    start_ts = safe_float(start_ts, 0.0)
    end_ts = safe_float(end_ts, 0.0)
    if end_ts <= start_ts:
        return {}
    allocations = {}
    for slot in annotated:
        if not slot.get("has_power_forecast"):
            continue
        slot_start = safe_float(slot.get("ts"), 0.0)
        slot_end = safe_float(slot.get("end_ts"), slot_start + SLOT_MS)
        overlap_start = max(start_ts, slot_start)
        overlap_end = min(end_ts, slot_end)
        if overlap_end <= overlap_start:
            continue
        deficit_wh = (
            max(0.0, safe_float(slot.get("forecast_deficit_w"), 0.0))
            * (overlap_end - overlap_start)
            / 3_600_000.0
        )
        if deficit_wh <= 0.0:
            continue
        key = (safe_int(slot_start, 0), safe_int(slot_end, 0))
        allocations[key] = max(allocations.get(key, 0.0), deficit_wh)
    return allocations


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
    return _pv_store_slot_physical_limit(entry, flags, efficiency)["available_stored_wh"]


def _entry_export_wh(entry):
    if entry.get("action") not in ("eco_plus_export_candidate", "arbitrage_export_candidate"):
        return 0.0
    return max(0.0, safe_float(entry.get("max_power_w"), 0.0) * _entry_duration_h(entry))


def _pv_store_priority_key(entry):
    reason = str(entry.get("reason") or "")
    start_ts = safe_float(entry.get("start_ts"), 0.0)
    net_sell = safe_float(entry.get("net_sell_ct"), safe_float(entry.get("avg_market_ct"), 0.0))
    if reason == "negative_price":
        priority_class = 0
    elif reason == "threshold_below_eeg":
        priority_class = 1
    else:
        priority_class = 2
    return (priority_class, net_sell, start_ts)


def _empty_pv_store_allocation_diagnostic(flags=None, reason="not_evaluated"):
    flags = flags or {}
    aux_ac_mode = str(flags.get("pv_store_aux_ac_mode") or "off")
    if aux_ac_mode not in AUX_AC_STORAGE_MODES:
        aux_ac_mode = "off"
    aux_ac_user_release = aux_ac_mode != "off"
    return {
        "schema": "direct_marketing_pv_store_allocation_v1",
        "active": False,
        "evaluated": False,
        "reason": str(reason),
        "priority_basis": "negative_hard_then_price_class_net_sell_ct_ascending_start_ts",
        "energy_domain": "battery_stored_wh",
        "forecast_required": True,
        "dc_only": True,
        "candidate_slot_count": 0,
        "selected_slot_count": 0,
        "storage_headroom_wh": 0.0,
        "requested_stored_wh": 0.0,
        "selected_stored_wh": 0.0,
        "remaining_stored_wh": 0.0,
        "marginal_slot": None,
        "segments": [],
        "source_contract": {
            "schema": "direct_marketing_pv_store_source_contract_v1",
            "default_source": "E3DC_DC",
            "allowed_sources": ["E3DC_DC"],
            "mode": aux_ac_mode,
            "mode_source": str(flags.get("pv_store_aux_ac_mode_source") or "default_off"),
            "aux_ac_user_release": aux_ac_user_release,
            "dc_forecast_complete": False,
            "forecast_fresh": False,
            "forecast_freshness_source": "unconfirmed",
            "topology_revision": None,
            "dc_requested_wh": 0.0,
            "dc_selected_wh": 0.0,
            "dc_conservative_selected_wh": 0.0,
            "dc_forecast_deficit_wh": 0.0,
            "dc_forecast_sources": [],
            "deadband_wh": 0.0,
            "protected_target_soc_pct": None,
            "forecast_confidence_pct": None,
            "dc_charge_efficiency_pct": safe_float(
                flags.get("pv_store_dc_charge_efficiency_pct"),
                96.0,
            ),
            "aux_ac_charge_efficiency_pct": safe_float(
                flags.get("pv_store_aux_ac_charge_efficiency_pct"),
                90.0,
            ),
            "discharge_efficiency_pct": safe_float(
                flags.get("pv_store_discharge_efficiency_pct"),
                95.0,
            ),
            "aux_ac_route_margin_ct_per_kwh": None,
            "dc_route_margin_ct_per_kwh": None,
            "aux_ac_min_margin_ct_per_kwh": safe_float(
                flags.get("pv_store_aux_ac_min_margin_ct_per_kwh"),
                0.0,
            ),
            "aux_ac_used": False,
            "aux_ac_selected_wh": 0.0,
            "grid_ac_allowed": False,
            "reason": str(reason),
        },
    }


def _pv_store_slot_physical_limit(entry, flags, efficiency):
    duration_h = _entry_duration_h(entry)
    battery_limit_w = max(
        0.0,
        safe_float(entry.get("max_power_w"), safe_float(flags.get("pv_store_max_w"), 0.0)),
    )
    dc_only = bool(cfg_bool(flags.get("pv_store_dc_only_enable"), False))
    forecast_source = "forecast_dc_surplus_w" if dc_only else "forecast_surplus_w"
    forecast_valid = bool(cfg_bool(entry.get("has_power_forecast"), False))
    if dc_only:
        forecast_valid = bool(
            forecast_valid
            and cfg_bool(entry.get("has_e3dc_pv_forecast"), False)
        )
    total_surplus_w = (
        max(0.0, safe_float(entry.get("forecast_surplus_w"), 0.0))
        if forecast_valid
        else 0.0
    )
    dc_surplus_w = (
        max(0.0, safe_float(entry.get("forecast_dc_surplus_w"), 0.0))
        if forecast_valid and cfg_bool(entry.get("has_e3dc_pv_forecast"), False)
        else 0.0
    )
    aux_ac_economic_allowed = cfg_bool(
        entry.get("pv_store_aux_ac_economic_allowed"),
        True,
    )
    if dc_only or not aux_ac_economic_allowed:
        forecast_surplus_w = dc_surplus_w
    else:
        forecast_surplus_w = total_surplus_w
    physical_power_w = min(battery_limit_w, forecast_surplus_w)
    dc_input_power_w = min(physical_power_w, dc_surplus_w)
    aux_ac_input_power_w = max(0.0, physical_power_w - dc_input_power_w)
    fallback_efficiency = _clamp(safe_float(efficiency, 0.0), 0.01, 1.0)
    dc_efficiency = _clamp(
        safe_float(flags.get("pv_store_dc_charge_efficiency_pct"), fallback_efficiency * 100.0) / 100.0,
        0.01,
        1.0,
    )
    aux_ac_efficiency = _clamp(
        safe_float(flags.get("pv_store_aux_ac_charge_efficiency_pct"), fallback_efficiency * 100.0) / 100.0,
        0.01,
        1.0,
    )
    forecast_available_wh = max(0.0, forecast_surplus_w * duration_h)
    input_wh = max(0.0, physical_power_w * duration_h)
    dc_stored_wh = dc_input_power_w * duration_h * dc_efficiency
    aux_ac_stored_wh = aux_ac_input_power_w * duration_h * aux_ac_efficiency
    stored_wh = dc_stored_wh + aux_ac_stored_wh
    return {
        "duration_h": duration_h,
        "forecast_valid": forecast_valid,
        "forecast_source": forecast_source,
        "forecast_surplus_w": forecast_surplus_w,
        "battery_limit_w": battery_limit_w,
        "physical_power_w": physical_power_w,
        "forecast_available_wh": forecast_available_wh,
        "available_input_wh": input_wh,
        "available_stored_wh": max(0.0, stored_wh),
        "dc_input_power_w": dc_input_power_w,
        "aux_ac_input_power_w": aux_ac_input_power_w,
        "available_dc_stored_wh": max(0.0, dc_stored_wh),
        "available_aux_ac_stored_wh": max(0.0, aux_ac_stored_wh),
        "dc_charge_efficiency": dc_efficiency,
        "aux_ac_charge_efficiency": aux_ac_efficiency,
        "source_split_complete": bool(entry.get("pv_store_source_split_complete") is True),
        "topology_revision": (
            str(entry.get("pv_topology_revision")).strip()
            if entry.get("pv_topology_revision") is not None
            and str(entry.get("pv_topology_revision")).strip()
            else None
        ),
        "forecast_fresh": bool(entry.get("pv_store_forecast_fresh") is True),
        "forecast_freshness_source": str(
            entry.get("pv_store_forecast_freshness_source") or "unconfirmed"
        ),
    }


def _pv_store_dispatch_for_stored_wh(physical, desired_stored_wh):
    """Rechnet gespeicherte Energie stückweise DC-zuerst in Eingangsleistung um."""

    duration_h = max(0.000001, safe_float(physical.get("duration_h"), 0.0))
    remaining_wh = max(0.0, safe_float(desired_stored_wh, 0.0))
    dc_available_wh = max(0.0, safe_float(physical.get("available_dc_stored_wh"), 0.0))
    aux_available_wh = max(0.0, safe_float(physical.get("available_aux_ac_stored_wh"), 0.0))
    dc_selected_wh = min(remaining_wh, dc_available_wh)
    remaining_wh = max(0.0, remaining_wh - dc_selected_wh)
    aux_selected_wh = min(remaining_wh, aux_available_wh)
    dc_efficiency = _clamp(safe_float(physical.get("dc_charge_efficiency"), 0.0), 0.01, 1.0)
    aux_efficiency = _clamp(safe_float(physical.get("aux_ac_charge_efficiency"), 0.0), 0.01, 1.0)
    dc_power_w = dc_selected_wh / (duration_h * dc_efficiency)
    aux_power_w = aux_selected_wh / (duration_h * aux_efficiency)
    return {
        "stored_wh": dc_selected_wh + aux_selected_wh,
        "dc_stored_wh": dc_selected_wh,
        "aux_ac_stored_wh": aux_selected_wh,
        "power_w": dc_power_w + aux_power_w,
        "dc_power_w": dc_power_w,
        "aux_ac_power_w": aux_power_w,
    }


def _pv_store_slot_diagnostic(entry, physical, rank, selected_wh=0.0, selected_power_w=0, limit_reason=""):
    market_ct = safe_float(entry.get("market_ct"), float("nan"))
    net_sell_ct = safe_float(entry.get("net_sell_ct"), float("nan"))
    return {
        "start_ts": int(safe_float(entry.get("start_ts"), 0.0)),
        "end_ts": int(safe_float(entry.get("end_ts"), 0.0)),
        "rank": int(rank),
        "reason": entry.get("reason"),
        "market_ct": round(market_ct, 3) if math.isfinite(market_ct) else None,
        "net_sell_ct": round(net_sell_ct, 3) if math.isfinite(net_sell_ct) else None,
        "forecast_valid": bool(physical["forecast_valid"]),
        "forecast_source": physical["forecast_source"],
        "forecast_provenance": entry.get("e3dc_pv_forecast_source"),
        "forecast_available_wh": round(physical["forecast_available_wh"], 1),
        "battery_limit_w": round(physical["battery_limit_w"], 1),
        "physical_power_w": round(physical["physical_power_w"], 1),
        "available_stored_wh": round(physical["available_stored_wh"], 1),
        "available_dc_stored_wh": round(physical["available_dc_stored_wh"], 1),
        "available_aux_ac_stored_wh": round(physical["available_aux_ac_stored_wh"], 1),
        "selected_stored_wh": round(max(0.0, selected_wh), 1),
        "selected_power_w": max(0, safe_int(selected_power_w, 0)),
        "source_split_complete": bool(physical["source_split_complete"]),
        "topology_revision": physical["topology_revision"],
        "forecast_fresh": bool(physical["forecast_fresh"]),
        "forecast_freshness_source": physical["forecast_freshness_source"],
        "dc_charge_efficiency_pct": round(physical["dc_charge_efficiency"] * 100.0, 2),
        "aux_ac_charge_efficiency_pct": round(physical["aux_ac_charge_efficiency"] * 100.0, 2),
        "aux_ac_route_margin_ct_per_kwh": entry.get("pv_store_aux_ac_route_margin_ct_per_kwh"),
        "dc_route_margin_ct_per_kwh": entry.get("pv_store_dc_route_margin_ct_per_kwh"),
        "aux_ac_economic_allowed": entry.get("pv_store_aux_ac_economic_allowed"),
        "limit_reason": str(limit_reason),
    }


def _finalize_pv_store_allocation_diagnostic(diagnostic, entries):
    if not isinstance(diagnostic, dict):
        return _empty_pv_store_allocation_diagnostic(reason="diagnostic_missing")
    selected_entries = {
        (
            int(safe_float(entry.get("start_ts"), 0.0)),
            int(safe_float(entry.get("end_ts"), 0.0)),
        ): entry
        for entry in (entries or [])
        if isinstance(entry, dict) and entry.get("action") == "eco_plus_store_pv_candidate"
    }
    selected_slots = []
    total_selected_wh = 0.0
    for segment in diagnostic.get("segments", []):
        segment_selected_wh = 0.0
        segment_selected_count = 0
        segment_marginal = None
        for slot in segment.get("slots", []):
            key = (
                int(safe_float(slot.get("start_ts"), 0.0)),
                int(safe_float(slot.get("end_ts"), 0.0)),
            )
            selected_entry = selected_entries.get(key)
            previously_selected_wh = max(0.0, safe_float(slot.get("selected_stored_wh"), 0.0))
            selected_wh = (
                max(0.0, safe_float(selected_entry.get("pv_store_budget_slot_selected_wh"), 0.0))
                if selected_entry is not None
                else 0.0
            )
            selected_power_w = (
                max(0, safe_int(selected_entry.get("max_power_w"), 0))
                if selected_entry is not None
                else 0
            )
            slot["selected_stored_wh"] = round(selected_wh, 1)
            slot["selected_power_w"] = selected_power_w
            if selected_entry is None and previously_selected_wh > 0.0:
                slot["limit_reason"] = "removed_after_reprioritization"
            if selected_wh > 0.0:
                segment_selected_wh += selected_wh
                segment_selected_count += 1
                segment_marginal = slot
                selected_slots.append(slot)
        if segment_marginal is not None:
            segment["marginal_slot"] = {
                "start_ts": segment_marginal["start_ts"],
                "end_ts": segment_marginal["end_ts"],
                "rank": segment_marginal["rank"],
                "market_ct": segment_marginal["market_ct"],
                "net_sell_ct": segment_marginal["net_sell_ct"],
                "selected_stored_wh": segment_marginal["selected_stored_wh"],
                "selected_power_w": segment_marginal["selected_power_w"],
            }
        else:
            segment["marginal_slot"] = None
        segment["selected_slot_count"] = segment_selected_count
        segment["selected_stored_wh"] = round(segment_selected_wh, 1)
        segment["remaining_stored_wh"] = round(
            max(0.0, safe_float(segment.get("requested_stored_wh"), 0.0) - segment_selected_wh),
            1,
        )
        total_selected_wh += segment_selected_wh

    marginal_slot = max(
        selected_slots,
        key=_pv_store_priority_key,
        default=None,
    )
    diagnostic["selected_slot_count"] = len(selected_slots)
    diagnostic["selected_stored_wh"] = round(total_selected_wh, 1)
    diagnostic["remaining_stored_wh"] = round(
        max(0.0, safe_float(diagnostic.get("requested_stored_wh"), 0.0) - total_selected_wh),
        1,
    )
    diagnostic["marginal_slot"] = (
        {
            "start_ts": marginal_slot["start_ts"],
            "end_ts": marginal_slot["end_ts"],
            "rank": marginal_slot["rank"],
            "market_ct": marginal_slot["market_ct"],
            "net_sell_ct": marginal_slot["net_sell_ct"],
            "selected_stored_wh": marginal_slot["selected_stored_wh"],
            "selected_power_w": marginal_slot["selected_power_w"],
        }
        if marginal_slot is not None
        else None
    )
    diagnostic["evaluated"] = diagnostic.get("candidate_slot_count", 0) > 0
    diagnostic["active"] = total_selected_wh > 0.0
    diagnostic["reason"] = "allocated" if total_selected_wh > 0.0 else "no_executable_slot"
    return diagnostic


def _apply_pv_store_energy_budget_for_source(entries, reserve, capacity_wh, flags, efficiency, current_soc):
    if not entries or not flags.get("pv_store_enable"):
        reason = "no_entries" if not entries else "pv_store_disabled"
        return entries, 0, _empty_pv_store_allocation_diagnostic(flags, reason)

    cap_wh = max(0.0, safe_float(capacity_wh, 0.0))
    if cap_wh <= 0.0:
        return entries, 0, _empty_pv_store_allocation_diagnostic(flags, "storage_capacity_invalid")

    adjusted = [dict(entry) for entry in entries]
    ordered_indices = sorted(range(len(adjusted)), key=lambda idx: safe_float(adjusted[idx].get("start_ts"), 0.0))
    current_need_wh = 0.0
    cluster = []
    changed = 0
    diagnostic = _empty_pv_store_allocation_diagnostic(flags)

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
        target_headroom_wh = target_need_from_cluster(cluster_indices, fallback_soc)
        carry_need_wh = current_need_wh
        budget_need_wh = min(
            cap_wh,
            max(carry_need_wh, target_headroom_wh),
        )
        ordered_cluster = sorted(
            cluster_indices,
            key=lambda item: _pv_store_priority_key(adjusted[item]),
        )
        segment = {
            "sequence": len(diagnostic["segments"]) + 1,
            "storage_headroom_wh": round(target_headroom_wh, 1),
            "carry_need_wh": round(carry_need_wh, 1),
            "requested_stored_wh": round(budget_need_wh, 1),
            "selected_stored_wh": 0.0,
            "remaining_stored_wh": round(budget_need_wh, 1),
            "selected_slot_count": 0,
            "marginal_slot": None,
            "slots": [],
        }
        diagnostic["segments"].append(segment)
        diagnostic["candidate_slot_count"] += len(ordered_cluster)
        diagnostic["storage_headroom_wh"] = round(
            safe_float(diagnostic.get("storage_headroom_wh"), 0.0) + target_headroom_wh,
            1,
        )
        diagnostic["requested_stored_wh"] = round(
            safe_float(diagnostic.get("requested_stored_wh"), 0.0) + budget_need_wh,
            1,
        )
        if budget_need_wh <= 50.0:
            for rank, idx in enumerate(ordered_cluster, start=1):
                if adjusted[idx].get("action") == "eco_plus_store_pv_candidate":
                    physical = _pv_store_slot_physical_limit(adjusted[idx], flags, efficiency)
                    segment["slots"].append(_pv_store_slot_diagnostic(
                        adjusted[idx],
                        physical,
                        rank,
                        limit_reason="storage_need_satisfied",
                    ))
                    adjusted[idx]["_remove_pv_store_budget"] = True
                    adjusted[idx]["pv_store_budget_limited"] = True
                    adjusted[idx]["pv_store_budget_need_wh"] = round(budget_need_wh, 0)
                    changed += 1
            current_need_wh = 0.0
            return

        remaining_wh = budget_need_wh
        selected_wh = 0.0
        selected = {}
        min_dispatch_w = max(300.0, safe_float(flags.get("pv_store_min_surplus_w"), 300.0))
        for rank, idx in enumerate(ordered_cluster, start=1):
            entry = adjusted[idx]
            physical = _pv_store_slot_physical_limit(entry, flags, efficiency)
            available_wh = physical["available_stored_wh"]
            take_wh = 0.0
            dispatch_w = 0
            if not physical["forecast_valid"]:
                limit_reason = "forecast_missing"
            elif physical["battery_limit_w"] <= 0.0:
                limit_reason = "battery_power_unavailable"
            elif physical["forecast_surplus_w"] <= 0.0:
                limit_reason = "forecast_surplus_unavailable"
            elif available_wh <= 50.0:
                limit_reason = "physical_energy_too_small"
            elif remaining_wh <= 50.0:
                limit_reason = "storage_need_satisfied"
            else:
                desired_wh = min(available_wh, remaining_wh)
                dispatch = _pv_store_dispatch_for_stored_wh(physical, desired_wh)
                desired_w = dispatch["power_w"]
                if desired_w + 0.000001 < min_dispatch_w:
                    limit_reason = "below_min_dispatch"
                else:
                    dispatch_w = int(math.floor(min(physical["physical_power_w"], desired_w) + 0.000001))
                    if dispatch_w < int(math.ceil(min_dispatch_w)):
                        limit_reason = "below_min_dispatch"
                        dispatch_w = 0
                    else:
                        actual_dc_power_w = min(
                            float(dispatch_w),
                            safe_float(physical.get("dc_input_power_w"), 0.0),
                        )
                        actual_aux_power_w = max(0.0, float(dispatch_w) - actual_dc_power_w)
                        take_wh = min(
                            available_wh,
                            remaining_wh,
                            physical["duration_h"]
                            * (
                                actual_dc_power_w * physical["dc_charge_efficiency"]
                                + actual_aux_power_w * physical["aux_ac_charge_efficiency"]
                            ),
                        )
                        if take_wh <= 50.0:
                            take_wh = 0.0
                            dispatch_w = 0
                            limit_reason = "physical_energy_too_small"
                        elif take_wh + 0.1 < available_wh:
                            limit_reason = "remaining_need"
                        elif physical["battery_limit_w"] + 0.1 < physical["forecast_surplus_w"]:
                            limit_reason = "battery_power"
                        else:
                            limit_reason = "forecast_surplus"
            if take_wh > 0.0:
                selected[idx] = (take_wh, available_wh, dispatch_w)
                selected_wh += take_wh
                remaining_wh = max(0.0, remaining_wh - take_wh)
            segment["slots"].append(_pv_store_slot_diagnostic(
                entry,
                physical,
                rank,
                selected_wh=take_wh,
                selected_power_w=dispatch_w,
                limit_reason=limit_reason,
            ))

        for idx in cluster_indices:
            entry = adjusted[idx]
            physical = _pv_store_slot_physical_limit(entry, flags, efficiency)
            take_wh, stored_wh, dispatch_w = selected.get(
                idx,
                (0.0, physical["available_stored_wh"], 0),
            )
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
            entry["pv_store_budget_slot_available_wh"] = round(stored_wh, 1)
            entry["pv_store_budget_slot_selected_wh"] = round(take_wh, 1)
            original_power_w = max(0, safe_int(entry.get("max_power_w"), 0))
            entry["max_power_w"] = dispatch_w
            if dispatch_w != original_power_w or take_wh + 0.1 < stored_wh:
                entry["pv_store_budget_limited"] = True
                changed += 1
        current_need_wh = max(0.0, budget_need_wh - selected_wh)
        segment["selected_stored_wh"] = round(selected_wh, 1)
        segment["remaining_stored_wh"] = round(current_need_wh, 1)

    for idx in ordered_indices:
        action = adjusted[idx].get("action")
        if action == "eco_plus_store_pv_candidate":
            cluster.append(idx)
            continue
        if action in ("eco_plus_export_candidate", "arbitrage_export_candidate"):
            flush_cluster()
            current_need_wh = min(cap_wh, current_need_wh + _entry_export_wh(adjusted[idx]))

    flush_cluster()

    filtered = [
        {key: value for key, value in entry.items() if key != "_remove_pv_store_budget"}
        for entry in adjusted
        if not entry.get("_remove_pv_store_budget")
    ]
    filtered.sort(key=lambda item: safe_float(item.get("start_ts"), 0.0))
    diagnostic = _finalize_pv_store_allocation_diagnostic(diagnostic, filtered)
    if not changed:
        return filtered, 0, diagnostic
    return filtered, changed, diagnostic


def _pv_store_allocation_slots(diagnostic):
    return [
        slot
        for segment in (diagnostic or {}).get("segments", [])
        if isinstance(segment, dict)
        for slot in segment.get("slots", [])
        if isinstance(slot, dict)
    ]


def _pv_store_aux_ac_deadband_wh(flags, capacity_wh, entries):
    slot_durations_h = [
        _entry_duration_h(entry)
        for entry in (entries or [])
        if isinstance(entry, dict)
        and entry.get("action") == "eco_plus_store_pv_candidate"
        and _entry_duration_h(entry) > 0.0
    ]
    slot_duration_h = min(slot_durations_h) if slot_durations_h else SLOT_MS / 3600000.0
    min_charge_w = max(
        300.0,
        safe_float((flags or {}).get("pv_store_min_surplus_w"), 300.0),
    )
    configured_wh = max(
        0.0,
        safe_float((flags or {}).get("pv_store_aux_ac_deadband_wh"), 0.0),
    )
    return max(
        500.0,
        max(0.0, safe_float(capacity_wh, 0.0)) * 0.02,
        min_charge_w * slot_duration_h,
        configured_wh,
    )


def _pv_store_aux_ac_economic_context(entry, flags):
    high_net_sell_ct = safe_float(
        (flags or {}).get("pv_store_aux_ac_best_high_net_sell_ct"),
        float("nan"),
    )
    opportunity_ct = safe_float(
        entry.get("net_sell_ct"),
        safe_float(entry.get("market_ct"), float("nan")),
    )
    dc_route_efficiency = _clamp(
        safe_float((flags or {}).get("pv_store_dc_route_efficiency_pct"), 0.0) / 100.0,
        0.01,
        1.0,
    )
    aux_ac_route_efficiency = _clamp(
        safe_float((flags or {}).get("pv_store_aux_ac_route_efficiency_pct"), 0.0) / 100.0,
        0.01,
        1.0,
    )
    degradation_ct = max(
        0.0,
        safe_float((flags or {}).get("pv_store_aux_ac_degradation_ct_per_kwh"), 0.0),
    )
    safety_margin_ct = safe_float(
        (flags or {}).get("pv_store_aux_ac_safety_margin_ct_per_kwh"),
        0.0,
    )
    min_margin_ct = max(
        0.0,
        safe_float((flags or {}).get("pv_store_aux_ac_min_margin_ct_per_kwh"), 0.0),
    )
    if not math.isfinite(high_net_sell_ct) or not math.isfinite(opportunity_ct):
        return {
            "valid": False,
            "allowed": False,
            "dc_margin_ct_per_kwh": None,
            "margin_ct_per_kwh": None,
            "min_margin_ct_per_kwh": round(min_margin_ct, 3),
            "reason": "economic_price_missing",
        }
    dc_delivered_revenue_ct = high_net_sell_ct * dc_route_efficiency
    aux_ac_delivered_revenue_ct = high_net_sell_ct * aux_ac_route_efficiency
    dc_margin_ct = dc_delivered_revenue_ct - opportunity_ct - degradation_ct - safety_margin_ct
    margin_ct = aux_ac_delivered_revenue_ct - opportunity_ct - degradation_ct - safety_margin_ct
    positive_margin = margin_ct > AUX_AC_POSITIVE_MARGIN_EPSILON_CT
    minimum_reached = margin_ct + AUX_AC_POSITIVE_MARGIN_EPSILON_CT >= min_margin_ct
    allowed = bool(positive_margin and minimum_reached)
    return {
        "valid": True,
        "allowed": allowed,
        "dc_margin_ct_per_kwh": round(dc_margin_ct, 3),
        "margin_ct_per_kwh": round(margin_ct, 3),
        "min_margin_ct_per_kwh": round(min_margin_ct, 3),
        "high_net_sell_ct_per_kwh": round(high_net_sell_ct, 3),
        "opportunity_ct_per_kwh": round(opportunity_ct, 3),
        "dc_delivered_revenue_ct_per_kwh": round(dc_delivered_revenue_ct, 3),
        "aux_ac_delivered_revenue_ct_per_kwh": round(aux_ac_delivered_revenue_ct, 3),
        "dc_route_efficiency_pct": round(dc_route_efficiency * 100.0, 2),
        "aux_ac_route_efficiency_pct": round(aux_ac_route_efficiency * 100.0, 2),
        "degradation_ct_per_kwh": round(degradation_ct, 3),
        "safety_margin_ct_per_kwh": round(safety_margin_ct, 3),
        "reason": (
            "economic_margin_positive"
            if allowed
            else (
                "economic_margin_not_positive"
                if not positive_margin
                else "economic_margin_below_minimum"
            )
        ),
    }


def _merge_dc_and_aux_ac_entries(dc_entries, routed_entries):
    """Bewahrt den vollständigen DC-Plan und ergänzt nur belegte AC-Anteile."""

    def slot_key(entry):
        return (
            int(safe_float(entry.get("start_ts"), 0.0)),
            int(safe_float(entry.get("end_ts"), 0.0)),
            str(entry.get("action") or ""),
        )

    merged = {slot_key(entry): dict(entry) for entry in (dc_entries or [])}
    for routed in routed_entries or []:
        key = slot_key(routed)
        current = merged.get(key)
        if current is None:
            merged[key] = dict(routed)
            continue
        if routed.get("action") != "eco_plus_store_pv_candidate":
            continue
        current_selected_wh = max(
            0.0,
            safe_float(current.get("pv_store_budget_slot_selected_wh"), 0.0),
        )
        routed_selected_wh = max(
            0.0,
            safe_float(routed.get("pv_store_budget_slot_selected_wh"), 0.0),
        )
        if routed_selected_wh <= 0.0:
            continue
        combined = dict(current)
        combined["max_power_w"] = (
            max(0, safe_int(current.get("max_power_w"), 0))
            + max(0, safe_int(routed.get("max_power_w"), 0))
        )
        combined["pv_store_budget_slot_selected_wh"] = round(
            current_selected_wh + routed_selected_wh,
            1,
        )
        combined["pv_store_budget_slot_available_wh"] = round(
            max(0.0, safe_float(current.get("pv_store_budget_slot_available_wh"), 0.0))
            + max(0.0, safe_float(routed.get("pv_store_budget_slot_available_wh"), 0.0)),
            1,
        )
        combined["pv_store_budget_selected_wh"] = round(
            max(0.0, safe_float(current.get("pv_store_budget_selected_wh"), 0.0))
            + max(0.0, safe_float(routed.get("pv_store_budget_selected_wh"), 0.0)),
            1,
        )
        for field in (
            "pv_store_source_contract",
            "pv_store_dc_only_enable",
            "pv_store_aux_ac_storage_allowed",
            "pv_store_aux_ac_mode",
            "pv_store_aux_ac_mode_source",
            "pv_store_dc_forecast_complete",
            "pv_store_forecast_fresh",
            "pv_store_forecast_freshness_source",
            "pv_store_topology_revision",
            "pv_store_dc_forecast_requested_wh",
            "pv_store_dc_forecast_selected_wh",
            "pv_store_dc_forecast_conservative_selected_wh",
            "pv_store_aux_ac_deadband_wh",
            "pv_store_aux_ac_protected_target_soc_pct",
            "pv_store_aux_ac_forecast_confidence_pct",
            "pv_store_dc_charge_efficiency_pct",
            "pv_store_aux_ac_charge_efficiency_pct",
            "pv_store_discharge_efficiency_pct",
            "pv_store_dc_route_margin_ct_per_kwh",
            "pv_store_aux_ac_route_margin_ct_per_kwh",
            "pv_store_aux_ac_min_margin_ct_per_kwh",
            "pv_store_dc_forecast_deficit_wh",
        ):
            if field in routed:
                combined[field] = routed.get(field)
        combined["target_soc_pct"] = routed.get(
            "target_soc_pct",
            current.get("target_soc_pct"),
        )
        merged[key] = combined
    ordered = sorted(merged.values(), key=lambda item: safe_float(item.get("start_ts"), 0.0))
    cluster = []

    def finalize_cluster():
        if not cluster:
            return
        selected_wh = round(
            sum(
                max(0.0, safe_float(ordered[index].get("pv_store_budget_slot_selected_wh"), 0.0))
                for index in cluster
            ),
            1,
        )
        for index in cluster:
            ordered[index]["pv_store_budget_selected_wh"] = selected_wh
        cluster.clear()

    for index, entry in enumerate(ordered):
        action = str(entry.get("action") or "")
        if action == "eco_plus_store_pv_candidate":
            cluster.append(index)
        elif action in {"eco_plus_export_candidate", "arbitrage_export_candidate"}:
            finalize_cluster()
    finalize_cluster()
    return ordered


def _pv_store_source_contract(
    *,
    mode="off",
    mode_source="default_off",
    aux_ac_user_release,
    dc_forecast_complete,
    forecast_fresh=False,
    forecast_freshness_source="unconfirmed",
    topology_revision=None,
    dc_requested_wh,
    dc_selected_wh,
    dc_conservative_selected_wh=None,
    dc_forecast_deficit_wh,
    dc_forecast_sources=None,
    deadband_wh=0.0,
    protected_target_soc_pct=None,
    forecast_confidence_pct=None,
    dc_charge_efficiency_pct=None,
    aux_ac_charge_efficiency_pct=None,
    discharge_efficiency_pct=None,
    dc_route_margin_ct_per_kwh=None,
    aux_ac_route_margin_ct_per_kwh=None,
    aux_ac_min_margin_ct_per_kwh=0.0,
    house_supply_evidence_complete=False,
    house_supply_evidence_revision=None,
    aux_ac_used=False,
    aux_ac_selected_wh=0.0,
    reason,
):
    allowed_sources = ["E3DC_DC"]
    if aux_ac_used:
        allowed_sources.append("AUX_AC_PV")
    return {
        "schema": "direct_marketing_pv_store_source_contract_v1",
        "default_source": "E3DC_DC",
        "allowed_sources": allowed_sources,
        "mode": str(mode if mode in AUX_AC_STORAGE_MODES else "off"),
        "mode_source": str(mode_source or "default_off"),
        "aux_ac_user_release": bool(aux_ac_user_release),
        "house_supply_evidence_status": (
            "complete"
            if mode == "house_supply" and house_supply_evidence_complete
            else (
                "evidence_limit"
                if mode == "house_supply"
                else "not_applicable"
            )
        ),
        "house_supply_evidence_revision": (
            str(house_supply_evidence_revision or "") or None
        ),
        "dc_forecast_complete": bool(dc_forecast_complete),
        "forecast_fresh": bool(forecast_fresh),
        "forecast_freshness_source": str(
            forecast_freshness_source or "unconfirmed"
        ),
        "topology_revision": (
            str(topology_revision).strip()
            if topology_revision is not None and str(topology_revision).strip()
            else None
        ),
        "dc_requested_wh": round(max(0.0, dc_requested_wh), 1),
        "dc_selected_wh": round(max(0.0, dc_selected_wh), 1),
        "dc_conservative_selected_wh": round(
            max(
                0.0,
                dc_selected_wh
                if dc_conservative_selected_wh is None
                else dc_conservative_selected_wh,
            ),
            1,
        ),
        "dc_forecast_deficit_wh": round(max(0.0, dc_forecast_deficit_wh), 1),
        "dc_forecast_sources": sorted({
            str(source)
            for source in (dc_forecast_sources or [])
            if source is not None and str(source).strip()
        }),
        "deadband_wh": round(max(0.0, deadband_wh), 1),
        "protected_target_soc_pct": (
            round(_clamp(safe_float(protected_target_soc_pct, 0.0), 0.0, 100.0), 2)
            if protected_target_soc_pct is not None
            else None
        ),
        "forecast_confidence_pct": (
            round(_clamp(safe_float(forecast_confidence_pct, 0.0), 0.0, 100.0), 2)
            if forecast_confidence_pct is not None
            else None
        ),
        "dc_charge_efficiency_pct": (
            round(safe_float(dc_charge_efficiency_pct, 0.0), 2)
            if dc_charge_efficiency_pct is not None
            else None
        ),
        "aux_ac_charge_efficiency_pct": (
            round(safe_float(aux_ac_charge_efficiency_pct, 0.0), 2)
            if aux_ac_charge_efficiency_pct is not None
            else None
        ),
        "discharge_efficiency_pct": (
            round(safe_float(discharge_efficiency_pct, 0.0), 2)
            if discharge_efficiency_pct is not None
            else None
        ),
        "dc_route_margin_ct_per_kwh": (
            round(safe_float(dc_route_margin_ct_per_kwh, 0.0), 3)
            if dc_route_margin_ct_per_kwh is not None
            else None
        ),
        "aux_ac_route_margin_ct_per_kwh": (
            round(safe_float(aux_ac_route_margin_ct_per_kwh, 0.0), 3)
            if aux_ac_route_margin_ct_per_kwh is not None
            else None
        ),
        "aux_ac_min_margin_ct_per_kwh": round(
            max(0.0, safe_float(aux_ac_min_margin_ct_per_kwh, 0.0)),
            3,
        ),
        "aux_ac_used": bool(aux_ac_used),
        "aux_ac_selected_wh": round(max(0.0, aux_ac_selected_wh), 1),
        "grid_ac_allowed": False,
        "reason": str(reason),
    }


def _decorate_pv_store_source_entries(
    entries,
    *,
    source_contract,
    aux_ac_selected_wh_by_slot=None,
):
    aux_ac_selected_wh_by_slot = aux_ac_selected_wh_by_slot or {}
    result = []
    for raw in entries or []:
        entry = dict(raw)
        if entry.get("action") == "eco_plus_store_pv_candidate":
            slot_key = (
                int(safe_float(entry.get("start_ts"), 0.0)),
                int(safe_float(entry.get("end_ts"), 0.0)),
            )
            slot_aux_ac_wh = max(
                0.0,
                safe_float(aux_ac_selected_wh_by_slot.get(slot_key), 0.0),
            )
            slot_aux_ac_allowed = bool(
                source_contract.get("aux_ac_used")
                and slot_aux_ac_wh > 50.0
            )
            entry["pv_store_source_contract"] = (
                "E3DC_DC_PLUS_AUX_AC_PV"
                if slot_aux_ac_allowed
                else "E3DC_DC"
            )
            entry["pv_store_dc_only_enable"] = not slot_aux_ac_allowed
            entry["pv_store_aux_ac_storage_allowed"] = slot_aux_ac_allowed
            entry["pv_store_aux_ac_mode"] = source_contract.get("mode")
            entry["pv_store_aux_ac_mode_source"] = source_contract.get("mode_source")
            entry["pv_store_aux_ac_house_supply_evidence_status"] = (
                source_contract.get("house_supply_evidence_status")
            )
            entry["pv_store_aux_ac_house_supply_evidence_revision"] = (
                source_contract.get("house_supply_evidence_revision")
            )
            entry["pv_store_aux_ac_quantile_evidence_complete"] = bool(
                source_contract.get("quantile_evidence_complete")
            )
            entry["pv_store_aux_ac_quantile_evidence_status"] = (
                source_contract.get("quantile_evidence_status")
            )
            entry["pv_store_aux_ac_quantile_evidence_revision"] = (
                source_contract.get("quantile_evidence_revision")
            )
            entry["pv_store_aux_ac_point_confidence_control_effect"] = False
            entry["pv_store_dc_forecast_complete"] = bool(
                source_contract.get("dc_forecast_complete")
            )
            entry["pv_store_forecast_fresh"] = bool(
                source_contract.get("forecast_fresh")
            )
            entry["pv_store_forecast_freshness_source"] = source_contract.get(
                "forecast_freshness_source"
            )
            entry["pv_store_topology_revision"] = source_contract.get(
                "topology_revision"
            )
            entry["pv_store_dc_forecast_requested_wh"] = source_contract.get(
                "dc_requested_wh"
            )
            entry["pv_store_dc_forecast_selected_wh"] = source_contract.get(
                "dc_selected_wh"
            )
            entry["pv_store_dc_forecast_conservative_selected_wh"] = source_contract.get(
                "dc_conservative_selected_wh"
            )
            entry["pv_store_aux_ac_deadband_wh"] = source_contract.get("deadband_wh")
            entry["pv_store_aux_ac_protected_target_soc_pct"] = source_contract.get(
                "protected_target_soc_pct"
            )
            entry["pv_store_aux_ac_forecast_confidence_pct"] = source_contract.get(
                "forecast_confidence_pct"
            )
            entry["pv_store_dc_charge_efficiency_pct"] = source_contract.get(
                "dc_charge_efficiency_pct"
            )
            entry["pv_store_aux_ac_charge_efficiency_pct"] = source_contract.get(
                "aux_ac_charge_efficiency_pct"
            )
            entry["pv_store_discharge_efficiency_pct"] = source_contract.get(
                "discharge_efficiency_pct"
            )
            entry["pv_store_dc_route_margin_ct_per_kwh"] = source_contract.get(
                "dc_route_margin_ct_per_kwh"
            )
            entry["pv_store_aux_ac_route_margin_ct_per_kwh"] = source_contract.get(
                "aux_ac_route_margin_ct_per_kwh"
            )
            entry["pv_store_aux_ac_min_margin_ct_per_kwh"] = source_contract.get(
                "aux_ac_min_margin_ct_per_kwh"
            )
            entry["pv_store_dc_forecast_deficit_wh"] = round(
                (
                    slot_aux_ac_wh
                    if slot_aux_ac_allowed
                    else 0.0
                ),
                1,
            )
        result.append(entry)
    return result


def _apply_pv_store_energy_budget(entries, reserve, capacity_wh, flags, efficiency, current_soc):
    """Allokiert E3DC-DC zuerst und ergänzt Zusatz-AC nur über den Modusvertrag."""

    flags = dict(flags or {})
    aux_ac_mode = str(flags.get("pv_store_aux_ac_mode") or "off")
    if aux_ac_mode not in AUX_AC_STORAGE_MODES:
        aux_ac_mode = "off"
    mode_source = str(flags.get("pv_store_aux_ac_mode_source") or "default_off")
    aux_ac_user_release = aux_ac_mode != "off"
    dc_flags = dict(flags or {})
    dc_flags["pv_store_dc_only_enable"] = True
    dc_entries, dc_changed, dc_diagnostic = _apply_pv_store_energy_budget_for_source(
        entries,
        reserve,
        capacity_wh,
        dc_flags,
        efficiency,
        current_soc,
    )
    dc_slots = _pv_store_allocation_slots(dc_diagnostic)
    dc_forecast_sources = sorted({
        str(slot.get("forecast_provenance"))
        for slot in dc_slots
        if slot.get("forecast_provenance")
    })
    dc_topology_revisions = {
        str(slot.get("topology_revision")).strip()
        for slot in dc_slots
        if slot.get("topology_revision") is not None
        and str(slot.get("topology_revision")).strip()
    }
    plan_topology_revision = (
        next(iter(dc_topology_revisions))
        if len(dc_topology_revisions) == 1
        else None
    )
    dc_forecast_freshness_sources = {
        str(slot.get("forecast_freshness_source")).strip()
        for slot in dc_slots
        if slot.get("forecast_freshness_source") is not None
        and str(slot.get("forecast_freshness_source")).strip()
    }
    forecast_freshness_source = (
        next(iter(dc_forecast_freshness_sources))
        if len(dc_forecast_freshness_sources) == 1
        else (
            "mixed"
            if dc_forecast_freshness_sources
            else "unconfirmed"
        )
    )
    dc_forecast_complete = bool(
        dc_slots
        and all(slot.get("forecast_valid") is True for slot in dc_slots)
        and len(dc_slots) == safe_int(dc_diagnostic.get("candidate_slot_count"), 0)
        and all(slot.get("source_split_complete") is True for slot in dc_slots)
        and plan_topology_revision is not None
        and all(
            str(slot.get("topology_revision") or "").strip()
            == plan_topology_revision
            for slot in dc_slots
        )
    )
    forecast_fresh = bool(
        dc_slots
        and len(dc_slots) == safe_int(dc_diagnostic.get("candidate_slot_count"), 0)
        and all(slot.get("forecast_fresh") is True for slot in dc_slots)
    )
    dc_requested_wh = max(
        0.0,
        safe_float(dc_diagnostic.get("requested_stored_wh"), 0.0),
    )
    dc_selected_wh = max(
        0.0,
        safe_float(dc_diagnostic.get("selected_stored_wh"), 0.0),
    )
    cap_wh = max(0.0, safe_float(capacity_wh, 0.0))
    confidence_pct = _clamp(
        safe_float(flags.get("pv_store_aux_ac_forecast_confidence_pct"), 80.0),
        0.0,
        100.0,
    )
    quantile_evidence_complete = cfg_bool(
        flags.get("pv_store_aux_ac_quantile_evidence_complete"),
        False,
    )
    protected_target_soc_pct = _clamp(
        safe_float(
            flags.get("pv_store_aux_ac_protected_target_soc_pct"),
            safe_float(reserve.get("effective_min_soc_pct"), 0.0),
        ),
        0.0,
        100.0,
    )
    protected_need_wh = max(
        0.0,
        (
            protected_target_soc_pct
            - _clamp(safe_float(current_soc, 0.0), 0.0, 100.0)
        )
        / 100.0
        * cap_wh,
    )
    if aux_ac_mode in {"reserve_only", "house_supply"}:
        route_requested_wh = min(dc_requested_wh, protected_need_wh)
        # Der frühere Prozentabschlag auf eine Punktprognose ist kein
        # kalibriertes Quantil und hat deshalb keinerlei Freigabewirkung.
        dc_conservative_selected_wh = dc_selected_wh
    else:
        route_requested_wh = dc_requested_wh
        dc_conservative_selected_wh = dc_selected_wh
    dc_deficit_wh = max(0.0, route_requested_wh - dc_conservative_selected_wh)
    deadband_wh = _pv_store_aux_ac_deadband_wh(flags, capacity_wh, entries)
    route_efficiency_fields = {
        "dc_charge_efficiency_pct": safe_float(
            flags.get("pv_store_dc_charge_efficiency_pct"),
            96.0,
        ),
        "aux_ac_charge_efficiency_pct": safe_float(
            flags.get("pv_store_aux_ac_charge_efficiency_pct"),
            90.0,
        ),
        "discharge_efficiency_pct": safe_float(
            flags.get("pv_store_discharge_efficiency_pct"),
            95.0,
        ),
        "aux_ac_min_margin_ct_per_kwh": safe_float(
            flags.get("pv_store_aux_ac_min_margin_ct_per_kwh"),
            0.0,
        ),
    }

    if aux_ac_mode == "off":
        reason = "e3dc_dc_only_default"
    elif not cfg_bool(flags.get("pv_store_aux_ac_topology_ready"), False):
        reason = "source_topology_unbound"
    elif not dc_forecast_complete:
        reason = "dc_forecast_incomplete"
    elif not forecast_fresh:
        reason = "forecast_freshness_unconfirmed"
    elif not quantile_evidence_complete:
        reason = "calibrated_joint_horizon_quantile_evidence_missing"
    elif (
        aux_ac_mode == "house_supply"
        and not cfg_bool(
            flags.get("pv_store_aux_ac_house_supply_evidence_complete"),
            False,
        )
    ):
        reason = "house_supply_conservative_source_forecast_missing"
    elif dc_deficit_wh <= deadband_wh:
        reason = "dc_forecast_sufficient"
    else:
        reason = "dc_point_forecast_deficit_diagnostic_only"
    dc_contract = _pv_store_source_contract(
        mode=aux_ac_mode,
        mode_source=mode_source,
        aux_ac_user_release=aux_ac_user_release,
        dc_forecast_complete=dc_forecast_complete,
        forecast_fresh=forecast_fresh,
        forecast_freshness_source=forecast_freshness_source,
        topology_revision=plan_topology_revision,
        dc_requested_wh=route_requested_wh,
        dc_selected_wh=dc_selected_wh,
        dc_conservative_selected_wh=dc_conservative_selected_wh,
        dc_forecast_deficit_wh=dc_deficit_wh,
        dc_forecast_sources=dc_forecast_sources,
        deadband_wh=deadband_wh,
        protected_target_soc_pct=protected_target_soc_pct,
        forecast_confidence_pct=confidence_pct,
        house_supply_evidence_complete=cfg_bool(
            flags.get("pv_store_aux_ac_house_supply_evidence_complete"),
            False,
        ),
        house_supply_evidence_revision=flags.get(
            "pv_store_aux_ac_house_supply_evidence_revision"
        ),
        **route_efficiency_fields,
        reason=reason,
    )
    dc_diagnostic["dc_only"] = True
    dc_contract["quantile_evidence_complete"] = bool(
        quantile_evidence_complete
    )
    dc_contract["quantile_evidence_status"] = str(
        flags.get("pv_store_aux_ac_quantile_evidence_status")
        or "evidence_limit"
    )
    dc_contract["quantile_evidence_revision"] = flags.get(
        "pv_store_aux_ac_quantile_evidence_revision"
    )
    dc_contract["point_confidence_control_effect"] = False
    dc_contract["dc_forecast_deficit_claim"] = "diagnostic_only"
    dc_diagnostic["source_contract"] = dc_contract
    dc_entries = _decorate_pv_store_source_entries(
        dc_entries,
        source_contract=dc_contract,
    )
    if (
        not aux_ac_user_release
        or not cfg_bool(flags.get("pv_store_aux_ac_topology_ready"), False)
        or not dc_forecast_complete
        or not forecast_fresh
        or not quantile_evidence_complete
        or (
            aux_ac_mode == "house_supply"
            and not cfg_bool(
                flags.get("pv_store_aux_ac_house_supply_evidence_complete"),
                False,
            )
        )
        or dc_deficit_wh <= deadband_wh
    ):
        return dc_entries, dc_changed, dc_diagnostic

    dc_power_by_slot = {
        (
            int(safe_float(entry.get("start_ts"), 0.0)),
            int(safe_float(entry.get("end_ts"), 0.0)),
        ): max(0.0, safe_float(entry.get("max_power_w"), 0.0))
        for entry in dc_entries
        if entry.get("action") == "eco_plus_store_pv_candidate"
    }
    original_target_by_slot = {}
    routed_entries = []
    residual_target_soc_pct = _clamp(
        _clamp(safe_float(current_soc, 0.0), 0.0, 100.0)
        + (dc_deficit_wh / cap_wh * 100.0 if cap_wh > 0.0 else 0.0),
        0.0,
        100.0,
    )
    for raw_entry in entries:
        if raw_entry.get("action") != "eco_plus_store_pv_candidate":
            continue
        entry = dict(raw_entry)
        slot_key = (
            int(safe_float(entry.get("start_ts"), 0.0)),
            int(safe_float(entry.get("end_ts"), 0.0)),
        )
        original_target_by_slot[slot_key] = entry.get("target_soc_pct")
        original_dc_w = max(0.0, safe_float(entry.get("forecast_dc_surplus_w"), 0.0))
        original_total_w = max(0.0, safe_float(entry.get("forecast_surplus_w"), 0.0))
        external_surplus_w = max(0.0, original_total_w - original_dc_w)
        slot_battery_limit_w = max(
            0.0,
            safe_float(
                entry.get("max_power_w"),
                safe_float(flags.get("pv_store_max_w"), 0.0),
            ),
        )
        if slot_battery_limit_w <= 0.0:
            slot_battery_limit_w = max(
                0.0,
                safe_float(flags.get("pv_store_max_w"), 0.0),
            )
        entry["max_power_w"] = max(
            0.0,
            slot_battery_limit_w - safe_float(dc_power_by_slot.get(slot_key), 0.0),
        )
        entry["forecast_dc_surplus_w"] = 0.0
        entry["forecast_surplus_w"] = external_surplus_w
        entry["target_soc_pct"] = residual_target_soc_pct
        routed_entries.append(entry)

    best_route_margin_ct = None
    best_dc_route_margin_ct = None
    if aux_ac_mode in {"reserve_only", "house_supply"}:
        for entry in routed_entries:
            entry["pv_store_aux_ac_economic_allowed"] = True
    else:
        any_economic_slot = False
        for entry in routed_entries:
            economic = _pv_store_aux_ac_economic_context(entry, flags)
            entry["pv_store_aux_ac_economic_allowed"] = bool(economic["allowed"])
            entry["pv_store_dc_route_margin_ct_per_kwh"] = economic[
                "dc_margin_ct_per_kwh"
            ]
            entry["pv_store_aux_ac_route_margin_ct_per_kwh"] = economic["margin_ct_per_kwh"]
            entry["pv_store_aux_ac_economic_reason"] = economic["reason"]
            if economic["dc_margin_ct_per_kwh"] is not None:
                best_dc_route_margin_ct = (
                    economic["dc_margin_ct_per_kwh"]
                    if best_dc_route_margin_ct is None
                    else max(best_dc_route_margin_ct, economic["dc_margin_ct_per_kwh"])
                )
            if economic["margin_ct_per_kwh"] is not None:
                best_route_margin_ct = (
                    economic["margin_ct_per_kwh"]
                    if best_route_margin_ct is None
                    else max(best_route_margin_ct, economic["margin_ct_per_kwh"])
                )
            any_economic_slot = bool(any_economic_slot or economic["allowed"])
        if not any_economic_slot:
            dc_contract["reason"] = "economic_margin_below_minimum"
            dc_contract["dc_route_margin_ct_per_kwh"] = best_dc_route_margin_ct
            dc_contract["aux_ac_route_margin_ct_per_kwh"] = best_route_margin_ct
            dc_diagnostic["source_contract"] = dc_contract
            return dc_entries, dc_changed, dc_diagnostic

    total_flags = dict(flags or {})
    total_flags["pv_store_dc_only_enable"] = False
    total_entries, total_changed, total_diagnostic = _apply_pv_store_energy_budget_for_source(
        routed_entries,
        reserve,
        capacity_wh,
        total_flags,
        efficiency,
        current_soc,
    )
    aux_ac_selected_wh_by_slot = {}
    for entry in total_entries:
        if entry.get("action") != "eco_plus_store_pv_candidate":
            continue
        slot_key = (
            int(safe_float(entry.get("start_ts"), 0.0)),
            int(safe_float(entry.get("end_ts"), 0.0)),
        )
        selected_wh = max(
            0.0,
            safe_float(entry.get("pv_store_budget_slot_selected_wh"), 0.0),
        )
        slot_aux_ac_wh = selected_wh
        if slot_aux_ac_wh > 0.0:
            aux_ac_selected_wh_by_slot[slot_key] = slot_aux_ac_wh
        entry["target_soc_pct"] = (
            protected_target_soc_pct
            if aux_ac_mode in {"reserve_only", "house_supply"}
            else original_target_by_slot.get(slot_key, entry.get("target_soc_pct"))
        )
    aux_ac_selected_wh = sum(aux_ac_selected_wh_by_slot.values())
    if aux_ac_selected_wh <= deadband_wh:
        dc_contract["reason"] = "aux_ac_residual_within_deadband"
        dc_contract["aux_ac_route_margin_ct_per_kwh"] = best_route_margin_ct
        dc_diagnostic["source_contract"] = dc_contract
        return dc_entries, dc_changed, dc_diagnostic

    total_contract = _pv_store_source_contract(
        mode=aux_ac_mode,
        mode_source=mode_source,
        aux_ac_user_release=True,
        dc_forecast_complete=True,
        forecast_fresh=True,
        forecast_freshness_source=forecast_freshness_source,
        topology_revision=plan_topology_revision,
        dc_requested_wh=route_requested_wh,
        dc_selected_wh=dc_selected_wh,
        dc_conservative_selected_wh=dc_conservative_selected_wh,
        dc_forecast_deficit_wh=dc_deficit_wh,
        dc_forecast_sources=dc_forecast_sources,
        deadband_wh=deadband_wh,
        protected_target_soc_pct=protected_target_soc_pct,
        forecast_confidence_pct=confidence_pct,
        dc_route_margin_ct_per_kwh=best_dc_route_margin_ct,
        aux_ac_route_margin_ct_per_kwh=best_route_margin_ct,
        house_supply_evidence_complete=cfg_bool(
            flags.get("pv_store_aux_ac_house_supply_evidence_complete"),
            False,
        ),
        house_supply_evidence_revision=flags.get(
            "pv_store_aux_ac_house_supply_evidence_revision"
        ),
        **route_efficiency_fields,
        aux_ac_used=True,
        aux_ac_selected_wh=aux_ac_selected_wh,
        reason=(
            "aux_ac_released_for_protected_reserve_deficit"
            if aux_ac_mode == "reserve_only"
            else (
                "aux_ac_released_for_protected_house_supply_deficit"
                if aux_ac_mode == "house_supply"
                else "aux_ac_released_for_positive_route_margin"
            )
        ),
    )
    aux_ac_diagnostic = total_diagnostic
    total_diagnostic = dict(dc_diagnostic)
    total_diagnostic["dc_only"] = False
    total_diagnostic["source_contract"] = total_contract
    total_diagnostic["aux_ac_allocation"] = aux_ac_diagnostic
    total_diagnostic["aux_ac_selected_wh"] = round(aux_ac_selected_wh, 1)
    total_entries = _decorate_pv_store_source_entries(
        total_entries,
        source_contract=total_contract,
        aux_ac_selected_wh_by_slot=aux_ac_selected_wh_by_slot,
    )
    merged_entries = _merge_dc_and_aux_ac_entries(dc_entries, total_entries)
    selected_entries = [
        entry
        for entry in merged_entries
        if entry.get("action") == "eco_plus_store_pv_candidate"
        and safe_float(entry.get("pv_store_budget_slot_selected_wh"), 0.0) > 0.0
    ]
    source_accounted_selected_wh = round(
        sum(
            max(
                0.0,
                safe_float(entry.get("pv_store_budget_slot_selected_wh"), 0.0),
            )
            for entry in selected_entries
        ),
        1,
    )
    total_diagnostic["source_accounted_selected_wh"] = source_accounted_selected_wh
    total_diagnostic["selected_stored_wh"] = source_accounted_selected_wh
    total_diagnostic["remaining_stored_wh"] = round(
        max(
            0.0,
            safe_float(total_diagnostic.get("requested_stored_wh"), 0.0)
            - source_accounted_selected_wh,
        ),
        1,
    )
    total_diagnostic["selected_slot_count"] = len(selected_entries)
    marginal_entry = max(selected_entries, key=_pv_store_priority_key, default=None)
    if marginal_entry is not None:
        priority_order = sorted(
            (
                entry
                for entry in merged_entries
                if entry.get("action") == "eco_plus_store_pv_candidate"
            ),
            key=_pv_store_priority_key,
        )
        rank_by_slot = {
            (
                int(safe_float(entry.get("start_ts"), 0.0)),
                int(safe_float(entry.get("end_ts"), 0.0)),
            ): rank
            for rank, entry in enumerate(priority_order, start=1)
        }
        marginal_key = (
            int(safe_float(marginal_entry.get("start_ts"), 0.0)),
            int(safe_float(marginal_entry.get("end_ts"), 0.0)),
        )
        marginal_market_ct = safe_float(
            marginal_entry.get("market_ct"),
            float("nan"),
        )
        marginal_net_sell_ct = safe_float(
            marginal_entry.get("net_sell_ct"),
            float("nan"),
        )
        total_diagnostic["marginal_slot"] = {
            "start_ts": marginal_key[0],
            "end_ts": marginal_key[1],
            "rank": rank_by_slot.get(marginal_key),
            "market_ct": (
                round(marginal_market_ct, 3)
                if math.isfinite(marginal_market_ct)
                else None
            ),
            "net_sell_ct": (
                round(marginal_net_sell_ct, 3)
                if math.isfinite(marginal_net_sell_ct)
                else None
            ),
            "selected_stored_wh": round(
                max(
                    0.0,
                    safe_float(
                        marginal_entry.get("pv_store_budget_slot_selected_wh"),
                        0.0,
                    ),
                ),
                1,
            ),
            "selected_power_w": max(
                0,
                safe_int(marginal_entry.get("max_power_w"), 0),
            ),
        }
    total_diagnostic["active"] = source_accounted_selected_wh > 0.0
    total_diagnostic["reason"] = (
        "allocated"
        if source_accounted_selected_wh > 0.0
        else "no_executable_slot"
    )
    return merged_entries, dc_changed + total_changed, total_diagnostic


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


def _future_export_credit_from_selected_pv_store(
    entries,
    reserve,
    capacity_wh,
    current_soc,
    annotated,
    now_ms,
    candidate_start_timestamps,
    efficiency,
):
    """Berechnet einen rein planerischen Exportkredit aus *ausgewählten* PV-Slots.

    Der Kredit darf ausschließlich einen späteren Verkaufskandidaten sichtbar
    machen. Er hebt weder die Notstromreserve noch irgendein Laufzeitveto auf.
    Fehlende Hauslast-Prognose zwischen jetzt und dem Kandidaten führt bewusst
    zu keinem Kredit.
    """
    result = {
        "schema": "direct_marketing_future_export_credit_v1",
        "reason": "no_selected_future_pv_store",
        "data_quality": "not_evaluated",
        "credits": {},
    }
    cap_wh = max(0.0, safe_float(capacity_wh, 0.0))
    now_ms = safe_float(now_ms, 0.0)
    reserve_wh = max(
        0.0,
        _clamp(safe_float(reserve.get("effective_min_soc_pct"), 0.0), 0.0, 100.0)
        / 100.0
        * cap_wh,
    )
    if cap_wh <= 0.0 or now_ms <= 0.0:
        result.update({"reason": "storage_values_invalid", "data_quality": "invalid"})
        return result

    roundtrip_efficiency = _clamp(safe_float(efficiency, 0.0), 0.01, 1.0)
    selected_charge_events = []
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("action") != "eco_plus_store_pv_candidate":
            continue
        start_ts = safe_float(entry.get("start_ts"), 0.0)
        end_ts = safe_float(entry.get("end_ts"), start_ts)
        selected_wh = max(0.0, safe_float(entry.get("pv_store_budget_slot_selected_wh"), 0.0))
        if start_ts < now_ms or end_ts <= start_ts or selected_wh <= 50.0:
            continue
        # Der Allokator arbeitet mit gespeicherter Energie. Für den späteren
        # Export rechnen wir zusätzlich nur den konservativen Planwirkungsgrad
        # an, niemals die größere physische Slot-Obergrenze.
        plan_limited_wh = max(0.0, safe_float(entry.get("max_power_w"), 0.0)) * _entry_duration_h(entry) * roundtrip_efficiency
        credited_wh = min(selected_wh, plan_limited_wh)
        if credited_wh > 50.0:
            selected_charge_events.append((end_ts, credited_wh))
    if not selected_charge_events:
        result.update({"data_quality": "ok"})
        return result

    forecast_slots = sorted(
        (dict(slot) for slot in (annotated or []) if isinstance(slot, dict)),
        key=lambda slot: safe_float(slot.get("ts"), 0.0),
    )
    if not forecast_slots:
        result.update({"reason": "house_forecast_missing", "data_quality": "invalid"})
        return result

    result["data_quality"] = "ok"
    for raw_target_ts in sorted(set(candidate_start_timestamps or [])):
        target_ts = safe_float(raw_target_ts, 0.0)
        key = str(int(target_ts))
        credit = {
            "candidate_start_ts": int(target_ts) if target_ts > 0.0 else None,
            "eligible": False,
            "reason": "candidate_not_future",
            "credit_wh": 0.0,
            "reserve_wh": round(reserve_wh, 1),
            "house_forecast_complete": False,
            "selected_pv_store_wh": 0.0,
        }
        if target_ts <= now_ms:
            result["credits"][key] = credit
            continue

        storage_wh = _clamp(safe_float(current_soc, 0.0), 0.0, 100.0) / 100.0 * cap_wh
        event_index = 0
        cursor_ts = now_ms
        selected_wh = 0.0
        forecast_complete = True
        for slot in forecast_slots:
            slot_start = safe_float(slot.get("ts"), 0.0)
            slot_end = safe_float(slot.get("end_ts"), slot_start + SLOT_MS)
            if slot_end <= cursor_ts:
                continue
            if slot_start > cursor_ts + 1.0:
                forecast_complete = False
                break
            interval_end = min(slot_end, target_ts)
            if interval_end <= cursor_ts:
                continue
            if not cfg_bool(slot.get("has_power_forecast"), False):
                forecast_complete = False
                break

            # Zuerst wird die PV-Ladung kapazitätsbegrenzt gutgeschrieben,
            # danach die Hauslast abgezogen. Das ist bei unbekannter Reihenfolge
            # innerhalb des Viertelstunden-Slots die konservativere Reihenfolge.
            while event_index < len(selected_charge_events) and selected_charge_events[event_index][0] <= interval_end:
                event_end_ts, event_wh = selected_charge_events[event_index]
                if event_end_ts > cursor_ts:
                    storage_wh = min(cap_wh, storage_wh + event_wh)
                    selected_wh += event_wh
                event_index += 1
            overlap_h = max(0.0, interval_end - cursor_ts) / 3600000.0
            storage_wh = max(
                0.0,
                storage_wh - max(0.0, safe_float(slot.get("forecast_deficit_w"), 0.0)) * overlap_h,
            )
            cursor_ts = interval_end
            if cursor_ts >= target_ts:
                break

        if cursor_ts + 1.0 < target_ts:
            forecast_complete = False
        credit.update({
            "house_forecast_complete": bool(forecast_complete),
            "selected_pv_store_wh": round(selected_wh, 1),
        })
        if not forecast_complete:
            credit["reason"] = "house_forecast_incomplete"
        else:
            available_wh = max(0.0, storage_wh - reserve_wh)
            credit["credit_wh"] = round(available_wh, 1)
            credit["eligible"] = available_wh > 50.0
            credit["reason"] = "selected_pv_store_before_export" if available_wh > 50.0 else "reserve_remains_protected"
        result["credits"][key] = credit

    if any(item.get("eligible") for item in result["credits"].values()):
        result["reason"] = "selected_pv_store_credit_available"
    else:
        result["reason"] = "selected_pv_store_credit_unavailable"
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
    accounting_day = str(flags.get("daily_export_accounting_day") or "")
    daily_remaining_wh_by_key = {}
    daily_selected_wh_by_key = {}

    def daily_remaining(day_key):
        if daily_limit_wh <= 0.0:
            return None
        if not day_key:
            return 0.0
        if day_key not in daily_remaining_wh_by_key:
            already_used_wh = daily_used_wh if day_key == accounting_day else 0.0
            daily_remaining_wh_by_key[day_key] = max(
                0.0,
                daily_limit_wh - already_used_wh,
            )
        return daily_remaining_wh_by_key[day_key]
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
        nonlocal limited
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
                "market_day": _market_day_key(entry.get("start_ts")),
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
            plateau_days = {item.get("market_day") for item in plateau}
            if len(plateau_days) != 1:
                # Ein Preisplateau darf niemals zwei Abrechnungstage zu einer
                # gemeinsamen Tagesgrenze verschmelzen.
                plateau_parts = [
                    [item for item in plateau if item.get("market_day") == day]
                    for day in sorted(plateau_days)
                ]
            else:
                plateau_parts = [plateau]
            for plateau_part in plateau_parts:
                if remaining_wh <= 50.0:
                    break
                day_key = str(plateau_part[0].get("market_day") or "")
                day_remaining_wh = daily_remaining(day_key)
                if day_remaining_wh is not None and day_remaining_wh <= 50.0:
                    continue
                plateau_capacity_wh = sum(item["energy_wh"] for item in plateau_part)
                plateau_budget_wh = min(plateau_capacity_wh, remaining_wh)
                if day_remaining_wh is not None:
                    plateau_budget_wh = min(plateau_budget_wh, day_remaining_wh)
                plateau_selected = _uniform_plateau_allocation(
                    plateau_part,
                    plateau_budget_wh,
                )
                selected.update(plateau_selected)
                selected_part_wh = sum(plateau_selected.values())
                remaining_wh = max(0.0, remaining_wh - selected_part_wh)
                daily_selected_wh_by_key[day_key] = (
                    daily_selected_wh_by_key.get(day_key, 0.0)
                    + selected_part_wh
                )
                if day_remaining_wh is not None:
                    daily_remaining_wh_by_key[day_key] = max(
                        0.0,
                        day_remaining_wh - selected_part_wh,
                    )

        selected_wh = sum(selected.values())

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
            entry_day = _market_day_key(entry.get("start_ts"))
            entry_daily_remaining = daily_remaining(entry_day)
            entry_daily_meta = {
                "daily_export_accounting_day": entry_day or None,
                "daily_export_limit_wh": round(daily_limit_wh, 0),
                "daily_export_used_wh": round(
                    daily_used_wh if entry_day == accounting_day else 0.0,
                    0,
                ),
                "daily_export_planned_wh": round(
                    daily_selected_wh_by_key.get(entry_day, 0.0),
                    0,
                ),
                "daily_export_remaining_wh": round(
                    max(
                        0.0,
                        entry_daily_remaining
                        if entry_daily_remaining is not None
                        else export_capacity_wh,
                    ),
                    0,
                ),
            }
            chosen_wh = selected.get(idx, 0.0)
            duration_h = _entry_duration_h(entry)
            if chosen_wh <= 50.0 or duration_h <= 0.0:
                next_entry = dict(entry)
                next_entry.update(segment_meta)
                next_entry.update(entry_daily_meta)
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
            next_entry.update(entry_daily_meta)
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
    for idx, entry in enumerate(entries):
        if entry.get("action") not in export_actions:
            continue
        entry_day = _market_day_key(entry.get("start_ts"))
        final_remaining_wh = daily_remaining(entry_day)
        adjusted[idx]["daily_export_accounting_day"] = entry_day or None
        adjusted[idx]["daily_export_limit_wh"] = round(daily_limit_wh, 0)
        adjusted[idx]["daily_export_used_wh"] = round(
            daily_used_wh if entry_day == accounting_day else 0.0,
            0,
        )
        adjusted[idx]["daily_export_planned_wh"] = round(
            daily_selected_wh_by_key.get(entry_day, 0.0),
            0,
        )
        adjusted[idx]["daily_export_remaining_wh"] = round(
            max(
                0.0,
                final_remaining_wh
                if final_remaining_wh is not None
                else export_capacity_wh,
            ),
            0,
        )
    return adjusted, limited


def _pv_shift_slot_revision(slot):
    for key in (
        "direct_marketing_price_revision",
        "market_price_revision",
        "price_revision",
        "price_revision_sha256",
    ):
        value = (slot or {}).get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _pv_shift_price_contract_matches(store_entry, export_entry):
    """Bindet beide Seiten einer PV-Verschiebung an denselben Preisvertrag."""

    if store_entry.get("fee_basis_valid") is not True or export_entry.get("fee_basis_valid") is not True:
        return False, "settlement_contract_invalid"
    store_source = str(store_entry.get("market_price_source") or "").strip().lower()
    export_source = str(export_entry.get("market_price_source") or "").strip().lower()
    if not store_source or not export_source:
        return False, "market_price_source_missing"
    if store_source != export_source:
        return False, "market_price_source_mismatch"
    store_resolution = store_entry.get("market_price_resolution_min")
    export_resolution = export_entry.get("market_price_resolution_min")
    if store_resolution in (None, "") or export_resolution in (None, ""):
        return False, "market_price_resolution_missing"
    store_resolution_min = safe_float(store_resolution, float("nan"))
    export_resolution_min = safe_float(export_resolution, float("nan"))
    if (
        not math.isfinite(store_resolution_min)
        or not math.isfinite(export_resolution_min)
        or store_resolution_min <= 0.0
        or export_resolution_min <= 0.0
    ):
        return False, "market_price_resolution_invalid"
    if abs(store_resolution_min - export_resolution_min) > 0.001:
        return False, "market_price_resolution_mismatch"
    store_revision = _pv_shift_slot_revision(store_entry)
    export_revision = _pv_shift_slot_revision(export_entry)
    if not store_revision or not export_revision:
        return False, "market_price_revision_missing"
    if store_revision != export_revision:
        return False, "market_price_revision_mismatch"
    for key in (
        "fee_basis",
        "fee_pct",
        "fixed_fee_net_ct",
        "service_vat_pct",
        "input_vat_recoverable",
    ):
        left = store_entry.get(key)
        right = export_entry.get(key)
        if isinstance(left, bool) or isinstance(right, bool):
            if left is not right:
                return False, "settlement_component_mismatch:%s" % key
        elif isinstance(left, (int, float)) or isinstance(right, (int, float)):
            left_number = safe_float(left, float("nan"))
            right_number = safe_float(right, float("nan"))
            if (
                not math.isfinite(left_number)
                or not math.isfinite(right_number)
                or abs(left_number - right_number) > 0.000001
            ):
                return False, "settlement_component_mismatch:%s" % key
        elif left != right:
            return False, "settlement_component_mismatch:%s" % key
    return True, "bound"


def _pv_shift_marginal_economics(config, store_entry, export_entry):
    contract_matches, contract_reason = _pv_shift_price_contract_matches(
        store_entry,
        export_entry,
    )
    current_net_ct = safe_float(store_entry.get("net_sell_ct"), float("nan"))
    future_net_ct = safe_float(export_entry.get("net_sell_ct"), float("nan"))
    if not contract_matches or not math.isfinite(current_net_ct) or not math.isfinite(future_net_ct):
        return {
            "valid": False,
            "profit_ok": False,
            "reason": contract_reason if not contract_matches else "net_sell_price_missing",
        }
    efficiency_pct = _clamp(
        safe_float(config.get("direct_marketing_roundtrip_efficiency_pct"), 85.0),
        1.0,
        100.0,
    )
    efficiency = efficiency_pct / 100.0
    lcos_ct = _configured_optional_float(config, "direct_marketing_lcos_ct_per_kwh")
    if lcos_ct is None:
        lcos_ct = _configured_float(config, "direct_marketing_degradation_ct_per_kwh", 4.0)
    lcos_ct = max(0.0, safe_float(lcos_ct, 0.0))
    safety_margin_ct = _clamp(
        safe_float(config.get("direct_marketing_safety_margin_ct_per_kwh"), 0.0),
        -10.0,
        50.0,
    )
    min_profit_ct = max(
        0.0,
        safe_float(config.get("direct_marketing_min_profit_ct_per_kwh"), 0.0),
    )
    min_margin_pct = max(
        0.0,
        safe_float(config.get("direct_marketing_min_margin_pct"), 10.0),
    )
    effective_future_ct = future_net_ct * efficiency
    spread_ct = effective_future_ct - current_net_ct - lcos_ct - safety_margin_ct
    cost_basis_ct = max(
        1.0,
        abs(current_net_ct) + lcos_ct + max(0.0, safety_margin_ct),
    )
    margin_pct = spread_ct / cost_basis_ct * 100.0
    profit_ok = bool(
        spread_ct > AUX_AC_POSITIVE_MARGIN_EPSILON_CT
        and spread_ct + AUX_AC_POSITIVE_MARGIN_EPSILON_CT >= min_profit_ct
        and margin_pct + 0.000001 >= min_margin_pct
    )
    return {
        "valid": True,
        "profit_ok": profit_ok,
        "reason": "marginal_profit_positive" if profit_ok else "marginal_profit_below_user_minimum",
        "current_net_sell_ct_per_kwh": round(current_net_ct, 6),
        "future_net_sell_ct_per_kwh": round(future_net_ct, 6),
        "effective_future_net_sell_ct_per_kwh": round(effective_future_ct, 6),
        "roundtrip_efficiency_pct": round(efficiency_pct, 3),
        "lcos_ct_per_kwh": round(lcos_ct, 6),
        "safety_margin_ct_per_kwh": round(safety_margin_ct, 6),
        "spread_ct_per_kwh": round(spread_ct, 6),
        "cost_basis_ct_per_kwh": round(cost_basis_ct, 6),
        "margin_pct": round(margin_pct, 6),
        "min_profit_ct_per_kwh": round(min_profit_ct, 6),
        "min_margin_pct": round(min_margin_pct, 6),
    }


def _bind_positive_pv_store_margins(
    entries,
    export_slots,
    config,
    reserve,
    capacity_wh,
    flags,
    annotated,
    mode,
):
    """Bindet jeden positiven PV_STORE-Slot an freie spätere Exportkapazität.

    Negative Rohpreis-Slots und deren harte Export-/Headroom-Kanten werden hier
    bewusst nicht bewertet oder verändert.
    """

    adjusted = [dict(entry) for entry in (entries or [])]
    positive_indices = [
        idx
        for idx, entry in enumerate(adjusted)
        if entry.get("action") == "eco_plus_store_pv_candidate"
        and safe_float(entry.get("market_ct"), 0.0) >= 0.0
    ]
    diagnostic = {
        "schema": "direct_marketing_pv_store_marginal_contract_v1",
        "evaluated": bool(positive_indices),
        "candidate_slot_count": len(positive_indices),
        "selected_slot_count": 0,
        "rejected_slot_count": 0,
        "released_export_slot_count": 0,
        "unsaturated_export_slot_count": 0,
        "available_export_headroom_wh": 0.0,
        "allocated_export_headroom_wh": 0.0,
        "reason_counts": {},
        "formula": "future_net_sell_ct*roundtrip_efficiency-current_net_sell_ct-lcos_ct-safety_margin_ct",
        "raw_negative_price_separate": True,
    }
    if not positive_indices:
        return adjusted, diagnostic

    def reject(idx, reason):
        adjusted[idx]["_remove_positive_pv_store"] = True
        diagnostic["rejected_slot_count"] += 1
        diagnostic["reason_counts"][reason] = diagnostic["reason_counts"].get(reason, 0) + 1

    if (
        mode != "eco_plus"
        or not cfg_bool(flags.get("export_enable"), False)
        or safe_float(flags.get("max_export_w"), 0.0) <= 0.0
    ):
        for idx in positive_indices:
            reject(idx, "later_export_not_released")
        return [
            {key: value for key, value in entry.items() if key != "_remove_positive_pv_store"}
            for entry in adjusted
            if not entry.get("_remove_positive_pv_store")
        ], diagnostic

    efficiency = _clamp(
        safe_float(config.get("direct_marketing_roundtrip_efficiency_pct"), 85.0) / 100.0,
        0.01,
        1.0,
    )
    baseline_entries = [
        dict(entry)
        for entry in adjusted
        if entry.get("action") == "eco_plus_store_pv_candidate"
        and safe_float(entry.get("market_ct"), 0.0) < 0.0
    ]
    export_bindings = []
    for slot in sorted(export_slots or [], key=lambda item: safe_float(item.get("ts"), 0.0)):
        export_entry = _new_slot_action(
            slot,
            "eco_plus_export_candidate",
            "profitable_high_price",
            {
                "max_power_w": int(max(0.0, safe_float(flags.get("max_export_w"), 0.0))),
                "economic_basis": "pv_shift",
                "reserve_floor_soc_pct": reserve.get("effective_min_soc_pct"),
            },
        )
        baseline_index = len(baseline_entries)
        baseline_entries.append(export_entry)
        export_bindings.append({
            "baseline_index": baseline_index,
            "entry": export_entry,
            "key": (
                safe_int(export_entry.get("start_ts"), 0),
                safe_int(export_entry.get("end_ts"), 0),
            ),
            "capacity_wh": max(0.0, safe_float(export_entry.get("max_power_w"), 0.0)) * _entry_duration_h(export_entry),
        })
    diagnostic["released_export_slot_count"] = len(export_bindings)
    if not export_bindings:
        for idx in positive_indices:
            reject(idx, "later_export_slot_missing")
    else:
        baseline_adjusted, _limited = _prioritize_export_entries(
            baseline_entries,
            reserve,
            capacity_wh,
            flags,
            efficiency,
            annotated=annotated,
        )

        def export_allocation_snapshot(simulated_entries):
            snapshot = {}
            for binding in export_bindings:
                allocated_entry = simulated_entries[binding["baseline_index"]]
                allocated_wh = (
                    max(0.0, safe_float(allocated_entry.get("max_power_w"), 0.0))
                    * _entry_duration_h(allocated_entry)
                    if allocated_entry.get("action") == "eco_plus_export_candidate"
                    else 0.0
                )
                snapshot[binding["key"]] = allocated_wh
            return snapshot

        baseline_allocations = export_allocation_snapshot(baseline_adjusted)
        selected_positive_entries = []
        used_export_keys = set()
        bound_export_wh_by_key = {}
        bound_load_reserve_wh_by_slot = {}

        for idx in sorted(
            positive_indices,
            key=lambda item: (
                safe_float(adjusted[item].get("net_sell_ct"), float("inf")),
                safe_float(adjusted[item].get("start_ts"), 0.0),
            ),
        ):
            entry = adjusted[idx]
            physical = _pv_store_slot_physical_limit(entry, flags, efficiency)
            duration_h = max(0.0, _entry_duration_h(entry))
            potential_output_wh = max(0.0, physical.get("available_input_wh", 0.0)) * efficiency
            if not physical.get("forecast_valid") or duration_h <= 0.0 or potential_output_wh <= 50.0:
                reject(idx, "positive_pv_energy_not_proven")
                continue

            candidates = []
            mismatch_reasons = []
            for destination in export_bindings:
                export_entry = destination["entry"]
                if safe_float(export_entry.get("start_ts"), 0.0) < safe_float(entry.get("end_ts"), 0.0):
                    continue
                economics = _pv_shift_marginal_economics(config, entry, export_entry)
                if not economics.get("valid"):
                    mismatch_reasons.append(str(economics.get("reason") or "price_contract_invalid"))
                    continue
                if not economics.get("profit_ok"):
                    mismatch_reasons.append("marginal_profit_below_user_minimum")
                    continue
                candidates.append((destination, economics))
            candidates.sort(
                key=lambda item: (
                    safe_float(item[1].get("future_net_sell_ct_per_kwh"), 0.0),
                    -safe_float(item[0]["entry"].get("start_ts"), 0.0),
                ),
                reverse=True,
            )
            min_dispatch_w = max(300.0, safe_float(flags.get("pv_store_min_surplus_w"), 300.0))
            if not candidates:
                if mismatch_reasons:
                    reason = sorted(set(mismatch_reasons))[0]
                elif not any(
                    safe_float(item["entry"].get("start_ts"), 0.0)
                    >= safe_float(entry.get("end_ts"), 0.0)
                    for item in export_bindings
                ):
                    reason = "later_export_slot_missing"
                else:
                    reason = "marginal_profit_below_user_minimum"
                reject(idx, reason)
                continue

            eligible_by_key = {destination["key"]: economics for destination, economics in candidates}
            trial_entries = baseline_entries + selected_positive_entries + [dict(entry)]
            trial_adjusted, _trial_limited = _prioritize_export_entries(
                trial_entries,
                reserve,
                capacity_wh,
                flags,
                efficiency,
                annotated=annotated,
            )
            trial_allocations = export_allocation_snapshot(trial_adjusted)
            total_incremental_wh = max(
                0.0,
                sum(trial_allocations.values())
                - sum(baseline_allocations.values()),
            )
            binding_by_key = {binding["key"]: binding for binding in export_bindings}
            eligible_binding_wh = 0.0
            for key in eligible_by_key:
                binding = binding_by_key[key]
                physical_headroom_wh = max(
                    0.0,
                    safe_float(binding.get("capacity_wh"), 0.0)
                    - baseline_allocations.get(key, 0.0),
                )
                unbound_final_export_wh = max(
                    0.0,
                    trial_allocations.get(key, 0.0)
                    - bound_export_wh_by_key.get(key, 0.0),
                )
                eligible_binding_wh += min(
                    physical_headroom_wh,
                    unbound_final_export_wh,
                )
            latest_eligible_export_start_ts = max(
                (
                    safe_int(binding_by_key[key]["entry"].get("start_ts"), 0)
                    for key in eligible_by_key
                ),
                default=0,
            )
            eligible_load_reserve_by_slot = _forecast_deficit_slot_allocations(
                annotated,
                safe_int(entry.get("end_ts"), 0),
                latest_eligible_export_start_ts,
                enabled=cfg_bool(
                    flags.get("export_segment_load_reserve_enable"),
                    True,
                ),
            )
            eligible_load_reserve_wh = sum(
                max(
                    0.0,
                    available_wh
                    - bound_load_reserve_wh_by_slot.get(slot_key, 0.0),
                )
                for slot_key, available_wh in eligible_load_reserve_by_slot.items()
            )
            if total_incremental_wh <= 50.0:
                reject(idx, "later_export_budget_saturated")
                continue
            if eligible_binding_wh <= 50.0:
                reject(idx, "marginal_export_destination_not_bound")
                continue

            selected_input_w = int(
                math.floor(
                    min(
                        potential_output_wh,
                        min(total_incremental_wh, eligible_binding_wh)
                        + eligible_load_reserve_wh,
                    )
                    / (efficiency * duration_h)
                    + 0.000001
                )
            )
            selected_input_w = min(selected_input_w, max(0, safe_int(entry.get("max_power_w"), 0)))
            if selected_input_w + 0.000001 < min_dispatch_w:
                reject(idx, "marginal_export_budget_below_min_dispatch")
                continue

            capped_entry = dict(entry)
            capped_entry["max_power_w"] = selected_input_w
            capped_trial_entries = baseline_entries + selected_positive_entries + [capped_entry]
            capped_adjusted, _capped_limited = _prioritize_export_entries(
                capped_trial_entries,
                reserve,
                capacity_wh,
                flags,
                efficiency,
                annotated=annotated,
            )
            capped_allocations = export_allocation_snapshot(capped_adjusted)
            capped_incremental_wh = max(
                0.0,
                sum(capped_allocations.values())
                - sum(baseline_allocations.values()),
            )
            allocations = []
            capped_binding_capacity_wh = 0.0
            for destination, _destination_economics in candidates:
                key = destination["key"]
                physical_headroom_wh = max(
                    0.0,
                    safe_float(destination.get("capacity_wh"), 0.0)
                    - baseline_allocations.get(key, 0.0),
                )
                unbound_final_export_wh = max(
                    0.0,
                    capped_allocations.get(key, 0.0)
                    - bound_export_wh_by_key.get(key, 0.0),
                )
                capped_binding_capacity_wh += min(
                    physical_headroom_wh,
                    unbound_final_export_wh,
                )
            selected_output_wh = max(
                0.0,
                selected_input_w * duration_h * efficiency,
            )
            remaining_binding_wh = min(
                selected_output_wh,
                capped_incremental_wh,
                capped_binding_capacity_wh,
            )
            for destination, destination_economics in candidates:
                if remaining_binding_wh <= 0.01:
                    break
                key = destination["key"]
                physical_headroom_wh = max(
                    0.0,
                    safe_float(destination.get("capacity_wh"), 0.0)
                    - baseline_allocations.get(key, 0.0),
                )
                unbound_final_export_wh = max(
                    0.0,
                    capped_allocations.get(key, 0.0)
                    - bound_export_wh_by_key.get(key, 0.0),
                )
                attributable_wh = min(
                    physical_headroom_wh,
                    unbound_final_export_wh,
                    remaining_binding_wh,
                )
                if attributable_wh <= 0.01:
                    continue
                allocations.append(
                    (destination, destination_economics, attributable_wh)
                )
                remaining_binding_wh = max(
                    0.0,
                    remaining_binding_wh - attributable_wh,
                )
            allocated_binding_wh = sum(item[2] for item in allocations)
            unbound_output_wh = max(0.0, selected_output_wh - allocated_binding_wh)
            latest_bound_export_start_ts = max(
                (
                    safe_int(item[0]["entry"].get("start_ts"), 0)
                    for item in allocations
                ),
                default=0,
            )
            capped_load_reserve_by_slot = _forecast_deficit_slot_allocations(
                annotated,
                safe_int(entry.get("end_ts"), 0),
                latest_bound_export_start_ts,
                enabled=cfg_bool(
                    flags.get("export_segment_load_reserve_enable"),
                    True,
                ),
            )
            capped_available_load_reserve_wh = sum(
                max(
                    0.0,
                    available_wh
                    - bound_load_reserve_wh_by_slot.get(slot_key, 0.0),
                )
                for slot_key, available_wh in capped_load_reserve_by_slot.items()
            )
            if (
                not allocations
                or remaining_binding_wh > 1.0
                or unbound_output_wh > capped_available_load_reserve_wh + 1.0
            ):
                reject(idx, "final_marginal_export_destination_not_bound")
                continue

            load_reserve_allocations = []
            remaining_load_reserve_wh = unbound_output_wh
            for slot_key in sorted(capped_load_reserve_by_slot):
                if remaining_load_reserve_wh <= 0.01:
                    break
                available_wh = max(
                    0.0,
                    capped_load_reserve_by_slot[slot_key]
                    - bound_load_reserve_wh_by_slot.get(slot_key, 0.0),
                )
                allocated_load_wh = min(available_wh, remaining_load_reserve_wh)
                if allocated_load_wh <= 0.01:
                    continue
                load_reserve_allocations.append({
                    "start_ts": slot_key[0],
                    "end_ts": slot_key[1],
                    "allocated_wh": round(allocated_load_wh, 1),
                })
                remaining_load_reserve_wh = max(
                    0.0,
                    remaining_load_reserve_wh - allocated_load_wh,
                )
            if remaining_load_reserve_wh > 1.0:
                reject(idx, "final_marginal_load_reserve_not_bound")
                continue

            allocated_wh = allocated_binding_wh
            weighted_future_ct = sum(
                safe_float(item[1].get("future_net_sell_ct_per_kwh"), 0.0) * item[2]
                for item in allocations
            ) / max(0.000001, allocated_wh)
            marginal_destination, marginal_economics, _marginal_wh = min(
                allocations,
                key=lambda item: safe_float(item[1].get("spread_ct_per_kwh"), 0.0),
            )
            current_net_ct = safe_float(entry.get("net_sell_ct"), 0.0)
            lcos_ct = safe_float(marginal_economics.get("lcos_ct_per_kwh"), 0.0)
            safety_margin_ct = safe_float(marginal_economics.get("safety_margin_ct_per_kwh"), 0.0)
            spread_ct = weighted_future_ct * efficiency - current_net_ct - lcos_ct - safety_margin_ct
            cost_basis_ct = max(1.0, abs(current_net_ct) + lcos_ct + max(0.0, safety_margin_ct))
            margin_pct = spread_ct / cost_basis_ct * 100.0
            marginal_contract = {
                "schema": "direct_marketing_pv_store_marginal_slot_v1",
                "price_contract": "same_net_sell_components",
                "current_slot_start_ts": safe_int(entry.get("start_ts"), 0),
                "current_slot_end_ts": safe_int(entry.get("end_ts"), 0),
                "current_net_sell_ct_per_kwh": round(current_net_ct, 6),
                "future_export_slot_start_ts": safe_int(marginal_destination["entry"].get("start_ts"), 0),
                "future_export_slot_end_ts": safe_int(marginal_destination["entry"].get("end_ts"), 0),
                "future_export_window_id": _policy_window_id(marginal_destination["entry"]),
                "future_net_sell_ct_per_kwh": round(weighted_future_ct, 6),
                "roundtrip_efficiency_pct": round(efficiency * 100.0, 3),
                "lcos_ct_per_kwh": round(lcos_ct, 6),
                "safety_margin_ct_per_kwh": round(safety_margin_ct, 6),
                "spread_ct_per_kwh": round(spread_ct, 6),
                "margin_pct": round(margin_pct, 6),
                "min_profit_ct_per_kwh": marginal_economics.get("min_profit_ct_per_kwh"),
                "min_margin_pct": marginal_economics.get("min_margin_pct"),
                "allocated_future_export_wh": round(allocated_wh, 1),
                "intervening_load_reserve_wh": round(unbound_output_wh, 1),
                "intervening_load_reserve_allocations": load_reserve_allocations,
                "future_export_slot_count": len(allocations),
                "future_export_allocations": [
                    {
                        "start_ts": safe_int(item[0]["entry"].get("start_ts"), 0),
                        "end_ts": safe_int(item[0]["entry"].get("end_ts"), 0),
                        "window_id": _policy_window_id(item[0]["entry"]),
                        "allocated_wh": round(item[2], 1),
                    }
                    for item in allocations
                ],
                "reserve_and_daily_budget_applied": True,
                "profit_ok": True,
            }
            entry["max_power_w"] = selected_input_w
            entry["economic_basis"] = "pv_shift_marginal_slot_v1"
            entry["expected_profit_ct_per_kwh"] = round(spread_ct, 3)
            entry["pv_store_marginal_contract"] = marginal_contract
            entry["pv_store_marginal_profit_ok"] = True
            entry["pv_store_marginal_future_export_wh"] = round(allocated_wh, 1)
            selected_positive_entries.append(dict(entry))
            baseline_adjusted = capped_adjusted
            baseline_allocations = capped_allocations
            for allocation_destination, _allocation_economics, allocation_wh in allocations:
                allocation_key = allocation_destination["key"]
                bound_export_wh_by_key[allocation_key] = (
                    bound_export_wh_by_key.get(allocation_key, 0.0)
                    + allocation_wh
                )
            for load_allocation in load_reserve_allocations:
                load_key = (
                    safe_int(load_allocation.get("start_ts"), 0),
                    safe_int(load_allocation.get("end_ts"), 0),
                )
                bound_load_reserve_wh_by_slot[load_key] = (
                    bound_load_reserve_wh_by_slot.get(load_key, 0.0)
                    + safe_float(load_allocation.get("allocated_wh"), 0.0)
                )
            used_export_keys.update(item[0]["key"] for item in allocations)
            diagnostic["selected_slot_count"] += 1
            diagnostic["allocated_export_headroom_wh"] = round(
                safe_float(diagnostic.get("allocated_export_headroom_wh"), 0.0) + allocated_wh,
                1,
            )

        diagnostic["unsaturated_export_slot_count"] = len(used_export_keys)
        diagnostic["available_export_headroom_wh"] = diagnostic["allocated_export_headroom_wh"]

    filtered = [
        {key: value for key, value in entry.items() if key != "_remove_positive_pv_store"}
        for entry in adjusted
        if not entry.get("_remove_positive_pv_store")
    ]
    return filtered, diagnostic


def _drop_positive_pv_store_without_final_export(
    entries,
    policy_timeline=None,
    annotated=None,
    load_reserve_enabled=True,
):
    """Entfernt eine Quelle, wenn ihr gebundener Export final nicht mehr existiert."""

    export_energy_remaining_by_slot = {
        (
            safe_int(entry.get("start_ts"), 0),
            safe_int(entry.get("end_ts"), 0),
        ): max(0.0, safe_float(entry.get("max_power_w"), 0.0)) * _entry_duration_h(entry)
        for entry in (entries or [])
        if isinstance(entry, dict)
        and entry.get("action") == "eco_plus_export_candidate"
    }
    if policy_timeline is not None:
        # Die Policy bindet an gruppierte Planfenster, während ``entries``
        # weiterhin einzelne Viertelstunden-Slots enthält. Die frühere
        # Slot-gegen-Fenster-Prüfung machte deshalb ein gültiges mehrslotiges
        # Exportfenster wirkungslos und entfernte anschließend seine bereits
        # wirtschaftlich gebundene PV_STORE-Quelle. Identität und Gate-Lineage
        # werden einmal zentral validiert; lokal bleibt nur die Bindung an
        # genau ein tatsächlich veröffentlichtes Planfenster.
        raw_export_entries = [
            entry
            for entry in (entries or [])
            if isinstance(entry, dict)
            and entry.get("action") == "eco_plus_export_candidate"
        ]
        grouped_export_entries = [
            dict(entry)
            for entry in raw_export_entries
            if "slot_count" in entry or "avg_market_ct" in entry
        ]
        slot_export_entries = [
            entry
            for entry in raw_export_entries
            if "slot_count" not in entry and "avg_market_ct" not in entry
        ]
        policy_plan_windows = grouped_export_entries
        if slot_export_entries and all(
            "market_ct" in entry for entry in slot_export_entries
        ):
            policy_plan_windows.extend(_group_windows(slot_export_entries))
        policy_export_energy_by_slot = {
            key: 0.0 for key in export_energy_remaining_by_slot
        }
        valid_export_policies = []
        for decision in (policy_timeline or []):
            if not isinstance(decision, dict):
                continue
            selected = (
                decision.get("selected_window")
                if isinstance(decision.get("selected_window"), dict)
                else {}
            )
            execution = (
                decision.get("execution_window")
                if isinstance(decision.get("execution_window"), dict)
                else {}
            )
            budget = (
                decision.get("storage_budget")
                if isinstance(decision.get("storage_budget"), dict)
                else {}
            )
            selected_action = str(selected.get("action") or "")
            selected_end_ts = safe_int(selected.get("end_ts"), 0)
            selected_window_id = str(selected.get("window_id") or "")
            selected_plan_window_id = str(
                selected.get("plan_window_id")
                or execution.get("plan_window_id")
                or selected_window_id
            )
            execution_start_ts = safe_int(execution.get("start_ts"), 0)
            execution_end_ts = safe_int(execution.get("end_ts"), 0)
            plan_window_start_ts = safe_int(
                execution.get("plan_window_start_ts"),
                0,
            )
            plan_window_end_ts = safe_int(
                execution.get("plan_window_end_ts"),
                0,
            )
            matching_plan_windows = [
                window
                for window in policy_plan_windows
                if safe_int(window.get("start_ts"), 0) == plan_window_start_ts
                and safe_int(window.get("end_ts"), 0) == plan_window_end_ts
                and _policy_window_id(window) == selected_plan_window_id
            ]
            export_budget_w = budget.get("export_budget_w")
            protected_reserve_wh = budget.get("protected_reserve_wh")
            sellable_wh = budget.get("sellable_wh")

            if not bool(
                selected_action == "eco_plus_export_candidate"
                and len(matching_plan_windows) == 1
                and direct_marketing_export_gate_contract_valid(
                    decision,
                    decision.get("economics"),
                    allowed_lineage_statuses={"ACTIVE"},
                    current_window_id=selected_window_id,
                    current_window_end_ts_ms=selected_end_ts,
                )
                and _policy_finite_contract_number(export_budget_w)
                and float(export_budget_w) > 0.0
                and _policy_finite_contract_number(protected_reserve_wh)
                and float(protected_reserve_wh) >= 0.0
                and _policy_finite_contract_number(sellable_wh)
                and float(sellable_wh) >= 0.0
            ):
                continue
            start_ts = execution_start_ts
            end_ts = execution_end_ts
            export_w = float(export_budget_w)
            valid_export_policies.append({
                "start_ts": start_ts,
                "end_ts": end_ts,
                "export_w": export_w,
            })
        for index, policy_contract in enumerate(valid_export_policies):
            start_ts = policy_contract["start_ts"]
            end_ts = policy_contract["end_ts"]
            if any(
                index != other_index
                and max(start_ts, other_contract["start_ts"])
                < min(end_ts, other_contract["end_ts"])
                for other_index, other_contract in enumerate(
                    valid_export_policies
                )
            ):
                continue
            export_w = policy_contract["export_w"]
            for key in policy_export_energy_by_slot:
                overlap_ms = max(0, min(end_ts, key[1]) - max(start_ts, key[0]))
                if overlap_ms <= 0:
                    continue
                policy_export_energy_by_slot[key] += export_w * overlap_ms / 3_600_000.0
        export_energy_remaining_by_slot = {
            key: min(value, policy_export_energy_by_slot.get(key, 0.0))
            for key, value in export_energy_remaining_by_slot.items()
        }
    source_indices = [
        idx
        for idx, entry in enumerate(entries or [])
        if isinstance(entry, dict)
        and isinstance(entry.get("pv_store_marginal_contract"), dict)
    ]
    source_indices.sort(
        key=lambda idx: (
            safe_float((entries or [])[idx].get("net_sell_ct"), float("inf")),
            safe_float((entries or [])[idx].get("start_ts"), 0.0),
            idx,
        )
    )
    global_load_reserve_remaining_by_slot = _forecast_deficit_slot_allocations(
        annotated,
        min(
            (safe_int((entries or [])[idx].get("end_ts"), 0) for idx in source_indices),
            default=0,
        ),
        max(
            (
                safe_int(allocation.get("start_ts"), 0)
                for idx in source_indices
                for allocation in (
                    (entries or [])[idx]
                    .get("pv_store_marginal_contract", {})
                    .get("future_export_allocations", [])
                )
                if isinstance(allocation, dict)
            ),
            default=0,
        ),
        enabled=load_reserve_enabled,
    )
    removed_indices = set()
    for idx in source_indices:
        entry = (entries or [])[idx]
        contract = entry["pv_store_marginal_contract"]
        allocations = contract.get("future_export_allocations")
        allocations = allocations if isinstance(allocations, list) else []
        source_end_ts = safe_int(entry.get("end_ts"), 0)
        realized = bool(allocations)
        requested_exports_by_slot = {}
        for allocation in allocations:
            if not isinstance(allocation, dict):
                realized = False
                break
            key = (
                safe_int(allocation.get("start_ts"), 0),
                safe_int(allocation.get("end_ts"), 0),
            )
            required_wh = max(0.0, safe_float(allocation.get("allocated_wh"), 0.0))
            if key[0] < source_end_ts or required_wh <= 0.0:
                realized = False
                break
            requested_exports_by_slot[key] = (
                requested_exports_by_slot.get(key, 0.0) + required_wh
            )
        requested_exports = sorted(requested_exports_by_slot.items())
        if any(
            export_energy_remaining_by_slot.get(key, 0.0) + 1.0 < required_wh
            for key, required_wh in requested_exports
        ):
            realized = False
        latest_export_start_ts = max(
            (key[0] for key, _required_wh in requested_exports),
            default=0,
        )
        eligible_load_reserve_by_slot = _forecast_deficit_slot_allocations(
            annotated,
            source_end_ts,
            latest_export_start_ts,
            enabled=load_reserve_enabled,
        )
        declared_load_reserve_wh = max(
            0.0,
            safe_float(contract.get("intervening_load_reserve_wh"), 0.0),
        )
        load_allocations = contract.get("intervening_load_reserve_allocations")
        load_allocations = load_allocations if isinstance(load_allocations, list) else []
        requested_load_reserve_by_slot = {}
        for allocation in load_allocations:
            if not isinstance(allocation, dict):
                realized = False
                break
            key = (
                safe_int(allocation.get("start_ts"), 0),
                safe_int(allocation.get("end_ts"), 0),
            )
            required_wh = max(0.0, safe_float(allocation.get("allocated_wh"), 0.0))
            if required_wh <= 0.0 or key not in eligible_load_reserve_by_slot:
                realized = False
                break
            requested_load_reserve_by_slot[key] = (
                requested_load_reserve_by_slot.get(key, 0.0) + required_wh
            )
        requested_load_reserve = sorted(requested_load_reserve_by_slot.items())
        requested_load_total_wh = sum(
            required_wh for _key, required_wh in requested_load_reserve
        )
        if abs(requested_load_total_wh - declared_load_reserve_wh) > 1.0:
            realized = False
        if any(
            required_wh
            > min(
                eligible_load_reserve_by_slot.get(key, 0.0),
                global_load_reserve_remaining_by_slot.get(key, 0.0),
            )
            + 1.0
            for key, required_wh in requested_load_reserve
        ):
            realized = False
        if realized:
            for key, required_wh in requested_exports:
                export_energy_remaining_by_slot[key] = max(
                    0.0,
                    export_energy_remaining_by_slot.get(key, 0.0) - required_wh,
                )
            for key, required_wh in requested_load_reserve:
                global_load_reserve_remaining_by_slot[key] = max(
                    0.0,
                    global_load_reserve_remaining_by_slot.get(key, 0.0)
                    - required_wh,
                )
        else:
            removed_indices.add(idx)
    return [
        entry for idx, entry in enumerate(entries or []) if idx not in removed_indices
    ], len(removed_indices)


def _finalize_pv_store_marginal_contract_diagnostic(diagnostic, entries):
    """Spiegelt ausschließlich die im finalen Plan verbleibenden Bindungen."""

    result = dict(diagnostic or {})
    contracts = [
        entry.get("pv_store_marginal_contract")
        for entry in (entries or [])
        if isinstance(entry, dict)
        and entry.get("action") == "eco_plus_store_pv_candidate"
        and isinstance(entry.get("pv_store_marginal_contract"), dict)
    ]
    allocation_keys = set()
    allocated_export_wh = 0.0
    for contract in contracts:
        allocations = contract.get("future_export_allocations")
        allocations = allocations if isinstance(allocations, list) else []
        for allocation in allocations:
            if not isinstance(allocation, dict):
                continue
            allocated_wh = max(
                0.0,
                safe_float(allocation.get("allocated_wh"), 0.0),
            )
            if allocated_wh <= 0.0:
                continue
            allocation_keys.add((
                safe_int(allocation.get("start_ts"), 0),
                safe_int(allocation.get("end_ts"), 0),
            ))
            allocated_export_wh += allocated_wh

    selected_count = len(contracts)
    candidate_count = max(
        selected_count,
        safe_int(result.get("candidate_slot_count"), selected_count),
    )
    result["selected_slot_count"] = selected_count
    result["rejected_slot_count"] = max(0, candidate_count - selected_count)
    result["unsaturated_export_slot_count"] = len(allocation_keys)
    result["allocated_export_headroom_wh"] = round(allocated_export_wh, 1)
    result["available_export_headroom_wh"] = round(allocated_export_wh, 1)
    result["released_export_slot_count"] = len({
        (
            safe_int(entry.get("start_ts"), 0),
            safe_int(entry.get("end_ts"), 0),
        )
        for entry in (entries or [])
        if isinstance(entry, dict)
        and entry.get("action") == "eco_plus_export_candidate"
        and safe_float(entry.get("max_power_w"), 0.0) > 0.0
    })
    return result


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
            and current.get("export_constraint_enforcement") == entry.get("export_constraint_enforcement")
            and current.get("export_constraint_execution_owner") == entry.get("export_constraint_execution_owner")
            and current.get("external_e3dc_owner_configured") == entry.get("external_e3dc_owner_configured")
            and current.get("pv_export_allowed") == entry.get("pv_export_allowed")
            and current.get("headroom_limited") == entry.get("headroom_limited")
            and current.get("pv_store_budget_limited") == entry.get("pv_store_budget_limited")
            and current.get("pv_store_budget_need_wh") == entry.get("pv_store_budget_need_wh")
            and current.get("pv_store_budget_selected_wh") == entry.get("pv_store_budget_selected_wh")
            and current.get("pv_store_budget_priority") == entry.get("pv_store_budget_priority")
            and current.get("pv_store_source_contract") == entry.get("pv_store_source_contract")
            and current.get("pv_store_aux_ac_storage_allowed") == entry.get("pv_store_aux_ac_storage_allowed")
            and current.get("pv_store_aux_ac_mode") == entry.get("pv_store_aux_ac_mode")
            and current.get("pv_store_dc_forecast_complete") == entry.get("pv_store_dc_forecast_complete")
            and current.get("pv_store_forecast_fresh") == entry.get("pv_store_forecast_fresh")
            and current.get("pv_store_forecast_freshness_source") == entry.get("pv_store_forecast_freshness_source")
            and current.get("pv_store_topology_revision") == entry.get("pv_store_topology_revision")
            and current.get("pv_store_dc_forecast_requested_wh") == entry.get("pv_store_dc_forecast_requested_wh")
            and current.get("pv_store_dc_forecast_selected_wh") == entry.get("pv_store_dc_forecast_selected_wh")
            and current.get("pv_store_dc_forecast_conservative_selected_wh") == entry.get("pv_store_dc_forecast_conservative_selected_wh")
            and current.get("pv_store_dc_route_margin_ct_per_kwh") == entry.get("pv_store_dc_route_margin_ct_per_kwh")
            and current.get("pv_store_aux_ac_route_margin_ct_per_kwh") == entry.get("pv_store_aux_ac_route_margin_ct_per_kwh")
            and current.get("negative_headroom_next_start_ts") == entry.get("negative_headroom_next_start_ts")
            and current.get("negative_headroom_required_pct") == entry.get("negative_headroom_required_pct")
            and current.get("pv_store_headroom_next_reason") == entry.get("pv_store_headroom_next_reason")
            and current.get("export_segment_id") == entry.get("export_segment_id")
            and current.get("pv_store_live_dc_fallback") == entry.get("pv_store_live_dc_fallback")
            and current.get("pv_store_live_dc_fallback_contract_version") == entry.get("pv_store_live_dc_fallback_contract_version")
            and current.get("pv_store_live_dc_fallback_max_power_w") == entry.get("pv_store_live_dc_fallback_max_power_w")
            and current.get("pv_store_raw_market_price_ct_kwh") == entry.get("pv_store_raw_market_price_ct_kwh")
            and current.get("pv_store_market_price_revision_sha256") == entry.get("pv_store_market_price_revision_sha256")
            and current.get("pv_store_marginal_contract") == entry.get("pv_store_marginal_contract")
            and current.get("daily_export_accounting_day") == entry.get("daily_export_accounting_day")
            and current.get("daily_export_planned_wh") == entry.get("daily_export_planned_wh")
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
                "export_constraint_enforcement",
                "export_constraint_execution_owner",
                "external_e3dc_owner_configured",
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
                "pv_store_source_contract",
                "pv_store_aux_ac_storage_allowed",
                "pv_store_aux_ac_mode",
                "pv_store_aux_ac_mode_source",
                "pv_store_aux_ac_house_supply_evidence_status",
                "pv_store_aux_ac_house_supply_evidence_revision",
                "pv_store_dc_forecast_complete",
                "pv_store_forecast_fresh",
                "pv_store_forecast_freshness_source",
                "pv_store_topology_revision",
                "pv_store_dc_forecast_requested_wh",
                "pv_store_dc_forecast_selected_wh",
                "pv_store_dc_forecast_conservative_selected_wh",
                "pv_store_dc_forecast_deficit_wh",
                "pv_store_aux_ac_deadband_wh",
                "pv_store_aux_ac_protected_target_soc_pct",
                "pv_store_aux_ac_forecast_confidence_pct",
                "pv_store_dc_charge_efficiency_pct",
                "pv_store_aux_ac_charge_efficiency_pct",
                "pv_store_discharge_efficiency_pct",
                "pv_store_dc_route_margin_ct_per_kwh",
                "pv_store_aux_ac_route_margin_ct_per_kwh",
                "pv_store_aux_ac_min_margin_ct_per_kwh",
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
                "pv_store_aux_ac_storage_enable",
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
                "pv_store_marginal_contract",
                "pv_store_marginal_profit_ok",
                "pv_store_marginal_future_export_wh",
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
                "daily_export_accounting_day",
                "daily_export_used_wh",
                "daily_export_planned_wh",
                "daily_export_remaining_wh",
                "market_window_id",
                "market_window_start_ts",
                "market_window_end_ts",
                "market_window_margin_class",
                "market_margin_class",
                "pv_store_live_dc_fallback",
                "pv_store_live_dc_fallback_contract_version",
                "pv_store_runtime_measurement_required",
                "pv_store_runtime_source_contract",
                "pv_store_live_dc_fallback_max_power_w",
                "pv_store_raw_market_price_ct_kwh",
                "pv_store_raw_market_price_source",
                "pv_store_raw_market_price_resolution_min",
                "pv_store_market_price_revision_sha256",
                "grid_ac_allowed",
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
        if current.get("pv_store_aux_ac_storage_allowed"):
            current["pv_store_dc_forecast_deficit_wh"] = round(
                max(
                    0.0,
                    safe_float(
                        current.get("pv_store_dc_forecast_deficit_wh"),
                        0.0,
                    ),
                )
                + max(
                    0.0,
                    safe_float(
                        entry.get("pv_store_dc_forecast_deficit_wh"),
                        0.0,
                    ),
                ),
                1,
            )
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
        if not item.get("window_id"):
            item["window_id"] = _policy_window_id(item)
    return windows


def _plan_valid_until_ts(windows, now_ms):
    """Bindet die Plangültigkeit nur an noch wirksame Fenster.

    Abgelaufene Fenster bleiben für Diagnose und Herkunft im Plan erhalten,
    dürfen aber einen exakt an der Slotkante neu erzeugten Aktionsvertrag
    nicht rückwirkend ungültig machen.
    """

    current_end_times = [
        safe_int(window.get("end_ts"), 0)
        for window in (windows or [])
        if isinstance(window, dict)
        and safe_int(window.get("end_ts"), 0) > safe_int(now_ms, 0)
    ]
    return min(current_end_times, default=safe_int(now_ms, 0) + SLOT_MS)


def _append_current_live_dc_pv_store_fallback(
    entries,
    annotated,
    market_windows,
    config,
    reserve,
    flags,
    mode,
    now_ms,
    current_soc,
    capacity_wh,
    target_soc_limit,
    negative_charge_target,
    previous_policy_decision=None,
):
    """Ergänzt genau den aktuellen Negativpreis-Slot als DC-only-AUTO-Freigabe.

    Der Vertrag ersetzt keine fehlende Prognose und allokiert keine künftige
    Energie. Er autorisiert nur einen kanonischen E3/DC-AUTO-Rahmen ohne
    CHRG-/Netzladebefehl; die tatsächlich verfügbare DC-Leistung wird erst nach
    dem Öffnen dieses Rahmens durch E3/DC genutzt. Jeder neue 15-Minuten-Slot
    wird erneut gegen den originalen Rohbörsenpreis gebunden.
    """

    diagnostic = {
        "schema": "direct_marketing_pv_store_auto_dc_permission_v2",
        "active": False,
        "reason": "not_applicable",
        "forecast_imputed": False,
        "future_slots_authorized": 0,
    }
    if mode not in {"eco", "eco_plus"}:
        diagnostic["reason"] = "mode_not_supported"
        return entries, diagnostic
    if not cfg_bool(flags.get("pv_store_enable"), False):
        diagnostic["reason"] = "pv_store_disabled"
        return entries, diagnostic
    if not cfg_bool(flags.get("pv_store_dc_only_enable"), False):
        diagnostic["reason"] = "e3dc_dc_only_not_selected"
        return entries, diagnostic
    if not _valid_soc_input(current_soc) or safe_float(capacity_wh, 0.0) <= 0.0:
        diagnostic["reason"] = "live_soc_or_capacity_missing"
        return entries, diagnostic

    current_slots = [
        slot
        for slot in (annotated or [])
        if safe_int(slot.get("ts"), 0) <= safe_int(now_ms, 0)
        < safe_int(slot.get("end_ts"), 0)
    ]
    if len(current_slots) != 1:
        diagnostic["reason"] = "current_raw_price_slot_not_unique"
        return entries, diagnostic
    slot = current_slots[0]
    raw_market_ct = safe_float(slot.get("market_ct"), float("nan"))
    if not math.isfinite(raw_market_ct) or raw_market_ct >= 0.0:
        diagnostic["reason"] = "raw_market_price_not_negative"
        return entries, diagnostic
    price_source = str(slot.get("market_price_source") or "").strip()
    price_resolution_min = safe_int(slot.get("market_price_resolution_min"), 0)
    if not price_source or price_resolution_min != 15:
        diagnostic["reason"] = "raw_market_price_provenance_incomplete"
        return entries, diagnostic

    window_id = str(slot.get("market_window_id") or "").strip()
    matching_market_windows = [
        window
        for window in (market_windows or [])
        if isinstance(window, dict)
        and str(window.get("market_window_id") or "") == window_id
        and safe_int(window.get("start_ts"), 0) <= safe_int(slot.get("ts"), 0)
        < safe_int(window.get("end_ts"), 0)
    ]
    if len(matching_market_windows) != 1:
        diagnostic["reason"] = "negative_market_window_not_unique"
        return entries, diagnostic
    market_window = matching_market_windows[0]
    price_revision = str(
        market_window.get("price_revision_sha256") or ""
    ).strip()
    slot_prices = [
        item
        for item in (market_window.get("slot_prices") or [])
        if isinstance(item, dict)
        and safe_int(item.get("start_ts"), 0) == safe_int(slot.get("ts"), 0)
        and abs(safe_float(item.get("market_ct"), float("nan")) - raw_market_ct)
        <= 0.000001
    ]
    if not window_id or not price_revision or len(slot_prices) != 1:
        diagnostic["reason"] = "negative_market_price_revision_unbound"
        return entries, diagnostic
    # Die externe Einspeisebegrenzung ist ein eigener Aktorpfad. Ob Luox
    # vorhanden oder live bestätigt ist, darf die wirtschaftlich und als
    # E3DC-DC-only gebundene Speicherfreigabe weder erzeugen noch widerrufen.
    # Der Exportvertrag bleibt ausschließlich als Diagnose am Slot erhalten.
    export_constraint = _slot_export_constraint(slot, flags)

    current_start = safe_int(slot.get("ts"), 0)
    current_end = safe_int(slot.get("end_ts"), 0)
    current_pv_entries = [
        (index, entry)
        for index, entry in enumerate(entries or [])
        if isinstance(entry, dict)
        and entry.get("action") == "eco_plus_store_pv_candidate"
        and safe_int(entry.get("start_ts"), 0) <= safe_int(now_ms, 0)
        < safe_int(entry.get("end_ts"), 0)
    ]
    if len(current_pv_entries) > 1:
        diagnostic["reason"] = "current_pv_store_slot_not_unique"
        return entries, diagnostic

    target_soc = max(
        _clamp(safe_float(target_soc_limit, 100.0), 0.0, 100.0),
        _clamp(safe_float(negative_charge_target, 100.0), 0.0, 100.0),
    )
    current_soc_value = _clamp(safe_float(current_soc, 0.0), 0.0, 100.0)
    remaining_wh = max(
        0.0,
        (target_soc - current_soc_value) / 100.0
        * safe_float(capacity_wh, 0.0),
    )
    if target_soc <= current_soc_value + 0.05 or remaining_wh <= 100.0:
        diagnostic["reason"] = "target_soc_reached"
        return entries, diagnostic

    max_power_w = max(0, safe_int(flags.get("pv_store_max_w"), 0))
    if max_power_w <= 0:
        max_power_w = max(
            0,
            safe_int(config.get("maximumladeleistung"), 0),
        )
    if max_power_w < 300:
        diagnostic["reason"] = "pv_store_power_below_minimum"
        return entries, diagnostic

    fallback_markers = {
        "pv_store_live_dc_fallback": True,
        "pv_store_live_dc_fallback_contract_version": 2,
        "pv_store_runtime_measurement_required": False,
        "pv_store_runtime_source_contract": "E3DC_DC_AUTO_CAP_RAW_PRICE",
        "pv_store_live_dc_fallback_max_power_w": max_power_w,
        "target_soc_pct": round(target_soc, 1),
        "pv_store_raw_market_price_ct_kwh": round(raw_market_ct, 6),
        "pv_store_raw_market_price_source": price_source,
        "pv_store_raw_market_price_resolution_min": price_resolution_min,
        "pv_store_market_price_revision_sha256": price_revision,
        "market_window_id": window_id,
        "market_window_start_ts": market_window.get("start_ts"),
        "market_window_end_ts": market_window.get("end_ts"),
        "market_window_margin_class": market_window.get("margin_class"),
        "market_margin_class": slot.get("market_margin_class"),
        "grid_ac_allowed": False,
    }
    if current_pv_entries:
        index, current_entry = current_pv_entries[0]
        if not bool(
            current_entry.get("pv_store_dc_only_enable") is True
            and current_entry.get("pv_store_aux_ac_storage_allowed") is not True
            and current_entry.get("pv_store_source_contract") == "E3DC_DC"
        ):
            diagnostic["reason"] = "selected_pv_store_not_e3dc_dc_only"
            return entries, diagnostic
        result = [dict(entry) for entry in (entries or [])]
        result[index].update(fallback_markers)
        diagnostic.update({
            "active": True,
            "reason": "current_forecast_pv_store_live_dc_guard",
            "slot_start_ts": current_start,
            "slot_end_ts": current_end,
            "market_window_id": window_id,
            "market_window_end_ts": safe_int(market_window.get("end_ts"), 0),
            "raw_market_price_ct_kwh": round(raw_market_ct, 6),
            "raw_market_price_source": price_source,
            "raw_market_price_resolution_min": price_resolution_min,
            "price_revision_sha256": price_revision,
            "target_soc_pct": round(target_soc, 1),
            "remaining_to_target_wh": round(remaining_wh, 1),
            "runtime_cap_w": min(
                max_power_w,
                max(300, safe_int(current_entry.get("max_power_w"), max_power_w)),
            ),
            "source": "E3DC_DC",
            "forecast_slot_preserved": True,
        })
        return result, diagnostic

    same_window_pv_entries = [
        entry
        for entry in (entries or [])
        if isinstance(entry, dict)
        and entry.get("action") == "eco_plus_store_pv_candidate"
        and str(entry.get("market_window_id") or "") == window_id
    ]
    previous_policy = (
        previous_policy_decision
        if isinstance(previous_policy_decision, dict)
        else {}
    )
    previous_selected = (
        previous_policy.get("selected_window")
        if isinstance(previous_policy.get("selected_window"), dict)
        else {}
    )
    previous_start = safe_int(previous_selected.get("start_ts"), 0)
    previous_end = safe_int(previous_selected.get("end_ts"), 0)
    previous_window_id = str(
        previous_selected.get("market_window_id")
        or previous_selected.get("window_id")
        or ""
    )
    continued_from_previous_slot = bool(
        str(previous_policy.get("dv_target_state") or "").upper()
        == "FORCE_CHARGE_PV"
        and previous_policy.get("commands_allowed") is True
        and previous_selected.get("action")
        == "eco_plus_store_pv_candidate"
        and previous_selected.get("pv_store_live_dc_fallback") is True
        and safe_int(
            previous_selected.get(
                "pv_store_live_dc_fallback_contract_version"
            ),
            0,
        )
        == 2
        and previous_selected.get("pv_store_runtime_measurement_required")
        is False
        and previous_selected.get("pv_store_runtime_source_contract")
        in {
            "E3DC_DC_AUTO_CAP_RAW_PRICE",
            "E3DC_DC_AUTO_CAP_LUOX_ZERO_EXPORT",
        }
        and previous_selected.get("pv_store_source_contract") == "E3DC_DC"
        and previous_selected.get("pv_store_dc_only_enable") is True
        and previous_selected.get("pv_store_aux_ac_storage_allowed") is not True
        and previous_selected.get("grid_ac_allowed") is False
        and math.isfinite(
            safe_float(
                previous_selected.get("pv_store_raw_market_price_ct_kwh"),
                float("nan"),
            )
        )
        and safe_float(
            previous_selected.get("pv_store_raw_market_price_ct_kwh"),
            0.0,
        )
        < 0.0
        and str(
            previous_selected.get("pv_store_raw_market_price_source") or ""
        )
        == price_source
        and safe_int(
            previous_selected.get("pv_store_raw_market_price_resolution_min"),
            0,
        )
        == 15
        and str(
            previous_selected.get("pv_store_market_price_revision_sha256")
            or ""
        )
        == price_revision
        and previous_window_id == window_id
        and safe_int(previous_selected.get("market_window_end_ts"), 0)
        == safe_int(market_window.get("end_ts"), 0)
        and abs(
            safe_float(previous_selected.get("target_soc_pct"), -1.0)
            - target_soc
        )
        <= 0.05
        and previous_start <= current_start
        and (
            previous_end == current_start
            or (
                previous_start == current_start
                and previous_end == current_end
            )
        )
    )
    if any(
        safe_int(entry.get("start_ts"), 0) >= current_end
        for entry in same_window_pv_entries
    ) and not continued_from_previous_slot:
        diagnostic["reason"] = "future_forecast_pv_store_keeps_priority"
        return entries, diagnostic

    extra = {
        "max_power_w": max_power_w,
        "target_soc_pct": round(target_soc, 1),
        "curtailment_allowed": bool(
            export_constraint.get("hard_export_limit_active")
        ),
        "curtail_export_limit_w": (
            safe_int(export_constraint.get("hard_export_limit_w"), 0)
            if export_constraint.get("hard_export_limit_active")
            else 0
        ),
        **export_constraint,
        "economic_basis": "raw_negative_market_price_live_dc",
        "storage_action": "pv_only_charge",
        "pv_store_price_class": "negative_price",
        "pv_store_soft_threshold": False,
        "pv_store_min_surplus_w": safe_int(flags.get("pv_store_min_surplus_w"), 300),
        "pv_store_import_guard_w": safe_int(flags.get("pv_store_import_guard_w"), 80),
        "pv_store_min_hold_s": safe_int(flags.get("pv_store_min_hold_s"), 0),
        "pv_store_ramp_step_w": safe_int(flags.get("pv_store_ramp_step_w"), 300),
        "pv_store_dc_only_enable": True,
        "pv_store_aux_ac_storage_enable": False,
        "pv_store_aux_ac_storage_allowed": False,
        "pv_store_external_ac_guard_w": safe_int(flags.get("pv_store_external_ac_guard_w"), 100),
        "pv_store_export_limit_guard_w": safe_int(flags.get("pv_store_export_limit_guard_w"), 100),
        "pv_store_export_limit_ramp_bypass_w": safe_int(flags.get("pv_store_export_limit_ramp_bypass_w"), 0),
        "pv_store_source_contract": "E3DC_DC",
        "pv_store_aux_ac_mode": "off",
        "pv_store_aux_ac_mode_source": "live_dc_fallback",
        "pv_store_dc_forecast_complete": False,
        "pv_store_forecast_fresh": False,
        "pv_store_forecast_freshness_source": (
            "e3dc_auto_dc_permission_current_slot_only"
        ),
        **fallback_markers,
    }
    result = list(entries or [])
    result.append(
        _new_slot_action(
            slot,
            "eco_plus_store_pv_candidate",
            "negative_price_live_dc_fallback",
            extra,
        )
    )
    diagnostic.update({
        "active": True,
        "reason": "current_negative_raw_price_live_dc",
        "slot_start_ts": current_start,
        "slot_end_ts": current_end,
        "market_window_id": window_id,
        "market_window_end_ts": safe_int(market_window.get("end_ts"), 0),
        "raw_market_price_ct_kwh": round(raw_market_ct, 6),
        "raw_market_price_source": price_source,
        "raw_market_price_resolution_min": price_resolution_min,
        "price_revision_sha256": price_revision,
        "target_soc_pct": round(target_soc, 1),
        "remaining_to_target_wh": round(remaining_wh, 1),
        "runtime_cap_w": max_power_w,
        "source": "E3DC_DC",
        "continued_from_previous_slot": continued_from_previous_slot,
    })
    return result, diagnostic


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
        plan = _base_plan(
            "off",
            "disabled",
            now_ms,
            blocked_reasons=["disabled"],
        )
        revoked = _policy_export_lineage_effectless_decision(
            plan["policy_decision"],
            previous_policy_decision,
            {},
            "REVOKED",
            ["REVOKED_USER_OR_EXPORT_DISABLED"],
        )
        plan["policy_decision"] = _enrich_policy_candidate_contract(
            revoked,
            [],
            now_ms,
        )
        return plan

    mode = _normalize_mode(config.get("direct_marketing_mode", "safe"))
    profile = _mode_profile(mode)
    pv_store_threshold = _pv_store_threshold_state(config)
    legacy_dc_only_veto = cfg_bool(
        config.get("direct_marketing_pv_store_dc_only_enable"),
        False,
    )
    aux_ac_mode, aux_ac_mode_source = _normalize_aux_ac_storage_mode(config)
    if legacy_dc_only_veto:
        aux_ac_mode = "off"
        aux_ac_mode_source = "legacy_dc_only_veto"
    route_efficiencies = _pv_store_route_efficiencies(config)
    pv_topology_contract = build_pv_forecast_topology(config)
    aux_ac_topology_ready = bool(
        pv_topology_contract.get("status") == "bound"
        and pv_topology_contract.get("e3dc_dc_bound")
        and pv_topology_contract.get("external_ac_bound")
    )
    aux_ac_storage_requested = bool(
        aux_ac_mode != "off"
        and not legacy_dc_only_veto
        and aux_ac_topology_ready
    )
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
        "pv_store_dc_only_enable": True,
        "pv_store_aux_ac_mode": aux_ac_mode,
        "pv_store_aux_ac_mode_source": aux_ac_mode_source,
        # Der DV-Plan besitzt aktuell nur Punktwerte. Deshalb bleibt jeder
        # optionale Zusatz-AC-Pfad bis zu einem gemeinsamen, über den ganzen
        # Horizont kalibrierten Nettoenergievertrag fail-closed.
        "pv_store_aux_ac_storage_requested": aux_ac_storage_requested,
        "pv_store_aux_ac_storage_enable": False,
        "pv_store_aux_ac_topology_ready": aux_ac_topology_ready,
        "pv_store_aux_ac_quantile_evidence_complete": False,
        "pv_store_aux_ac_quantile_evidence_status": "evidence_limit",
        "pv_store_aux_ac_quantile_evidence_revision": None,
        "pv_store_aux_ac_point_confidence_control_effect": False,
        # Für die neue Hausversorgungsfreigabe genügt ein Abschlag auf eine
        # deterministische Punktprognose nicht.
        # Bis Forecast und Topologie gemeinsame P10-Quellenpfade liefern,
        # bleibt die Auswahl sichtbar, aber fail-closed.
        "pv_store_aux_ac_house_supply_evidence_complete": False,
        "pv_store_aux_ac_house_supply_evidence_status": (
            "evidence_limit"
            if aux_ac_mode == "house_supply"
            else "not_applicable"
        ),
        "pv_store_aux_ac_house_supply_evidence_revision": None,
        "pv_store_aux_ac_forecast_confidence_pct": _clamp(
            safe_float(
                config.get("direct_marketing_aux_inverter_ac_forecast_confidence_pct"),
                80.0,
            ),
            0.0,
            100.0,
        ),
        "pv_store_aux_ac_deadband_wh": max(
            0.0,
            safe_float(
                config.get("direct_marketing_aux_inverter_ac_deadband_wh"),
                0.0,
            ),
        ),
        "pv_store_aux_ac_min_margin_ct_per_kwh": max(
            0.0,
            safe_float(
                config.get("direct_marketing_aux_inverter_ac_min_margin_ct_per_kwh"),
                safe_float(config.get("direct_marketing_min_profit_ct_per_kwh"), 0.0),
            ),
        ),
        "pv_store_dc_charge_efficiency_pct": route_efficiencies["dc_charge_pct"],
        "pv_store_aux_ac_charge_efficiency_pct": route_efficiencies["aux_ac_charge_pct"],
        "pv_store_discharge_efficiency_pct": route_efficiencies["discharge_pct"],
        "pv_store_dc_route_efficiency_pct": route_efficiencies["dc_route_pct"],
        "pv_store_aux_ac_route_efficiency_pct": route_efficiencies["aux_ac_route_pct"],
        "pv_store_aux_ac_degradation_ct_per_kwh": max(
            0.0,
            safe_float(config.get("direct_marketing_degradation_ct_per_kwh"), 4.0),
        ),
        "pv_store_aux_ac_safety_margin_ct_per_kwh": safe_float(
            config.get("direct_marketing_safety_margin_ct_per_kwh"),
            0.0,
        ),
        "pv_store_external_ac_guard_w": max(0.0, safe_float(config.get("direct_marketing_pv_store_external_ac_guard_w"), 100.0)),
        "pv_store_export_limit_guard_w": max(0.0, safe_float(config.get("direct_marketing_pv_store_export_limit_guard_w"), 100.0)),
        "pv_store_export_limit_ramp_bypass_w": max(0.0, safe_float(config.get("direct_marketing_pv_store_export_limit_ramp_bypass_w"), 300.0)),
        "v2x_discharge_enable": cfg_bool(config.get("direct_marketing_v2x_discharge_enable"), False),
        "negative_price_no_export": cfg_bool(config.get("direct_marketing_negative_price_no_export"), True),
        "e3dc_export_execution_owner": _normalize_e3dc_export_execution_owner(
            config.get("direct_marketing_e3dc_export_execution_owner")
        ),
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
        "daily_export_accounting_day": _market_day_key(now_ms),
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
    protected_target_raw = _configured_optional_float(
        config,
        "direct_marketing_aux_inverter_ac_protected_target_soc_pct",
    )
    if protected_target_raw is None:
        protected_target_raw = max(
            safe_float(reserve.get("effective_min_soc_pct"), 0.0),
            safe_float(reserve.get("home_reserve_soc_pct"), 0.0),
            safe_float(reserve.get("night_reserve_soc_pct"), 0.0),
        )
    flags["pv_store_aux_ac_protected_target_soc_pct"] = _clamp(
        protected_target_raw,
        0.0,
        100.0,
    )

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
        plan = _base_plan(
            mode,
            reason,
            now_ms,
            blocked_reasons=blocked,
            reserve=reserve,
            flags=flags,
        )
        plan["policy_decision"] = _build_policy_decision(
            config,
            [],
            [],
            reserve,
            flags,
            {},
            mode,
            now_ms,
            current_soc,
            capacity_wh,
            blocked,
            previous_policy_decision=previous_policy_decision,
        )
        return plan


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
        shelly_cutoff_configured = bool(
            config.get("direct_marketing_aux_inverter_shelly")
            or config.get("direct_marketing_aux_inverter_shelly_ip")
            or cfg_bool(config.get("direct_marketing_negative_price_no_export"), True)
        )
        cut_external_ac = bool(is_negative and shelly_cutoff_configured)
        forecast_power = _slot_forecast_power(
            slot,
            assume_total_pv_is_e3dc_dc=not _external_ac_source_configured(config),
            cut_external_ac=cut_external_ac,
        )
        annotated.append({
            "ts": _slot_ts(slot),
            "end_ts": _slot_end_ts(slot),
            "market_ct": market_ct,
            "billing_ct": billing_ct,
            "score": score,
            "market_price_source": _market_price_source(slot),
            "market_price_resolution_min": _market_price_resolution_min(slot),
            "market_price_revision": _pv_shift_slot_revision(slot),
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
    flags["pv_store_aux_ac_best_high_net_sell_ct"] = economics.get(
        "best_high_net_sell_ct"
    )
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
    export_slots = []
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
                if not flags["pv_store_enable"] or not slot.get("is_pv_store"):
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
                    "pv_store_aux_ac_storage_enable": bool(flags["pv_store_aux_ac_storage_enable"]),
                    "pv_store_external_ac_guard_w": int(flags["pv_store_external_ac_guard_w"]),
                    "pv_store_export_limit_guard_w": int(flags["pv_store_export_limit_guard_w"]),
                    "pv_store_export_limit_ramp_bypass_w": int(flags["pv_store_export_limit_ramp_bypass_w"]),
                    "pv_store_threshold_source": flags.get("pv_store_threshold_source"),
                    "expected_profit_ct_per_kwh": (
                        economics.get("pv_shift_spread_ct_per_kwh")
                        if slot["is_negative"]
                        else None
                    ),
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

        if slot.get("is_export_plateau", slot["is_high"]):
            export_slots.append(slot)

    entries, pv_store_marginal_contract = _bind_positive_pv_store_margins(
        entries,
        export_slots,
        config,
        reserve,
        capacity_wh,
        flags,
        annotated,
        mode,
    )

    # Ein späterer Verkauf darf bei aktueller Reserve nur dann überhaupt als
    # Kandidat auftauchen, wenn vorher bereits eine physisch begrenzte
    # PV-Speicherung ausgewählt wurde. Diese Vorallokation ist reine Planung;
    # die reguläre Allokation und der harte Runtime-Reservewächter bleiben
    # unverändert nachgelagert.
    efficiency = safe_float(config.get("direct_marketing_roundtrip_efficiency_pct"), 85.0) / 100.0
    future_export_credit = {
        "schema": "direct_marketing_future_export_credit_v1",
        "reason": "current_export_energy_available",
        "data_quality": "not_needed",
        "credits": {},
    }
    reserve_limited = reserve.get("available_export_soc_pct", 0.0) <= 1.0
    if (
        reserve_limited
        and mode == "eco_plus"
        and flags["export_enable"]
        and flags["max_export_w"] > 0.0
        and export_slots
    ):
        pv_store_entries = [
            dict(entry)
            for entry in entries
            if entry.get("action") == "eco_plus_store_pv_candidate"
        ]
        planned_pv_entries, _planned_limited, _planned_allocation = _apply_pv_store_energy_budget(
            pv_store_entries,
            reserve,
            capacity_wh,
            flags,
            efficiency,
            current_soc,
        )
        future_export_credit = _future_export_credit_from_selected_pv_store(
            planned_pv_entries,
            reserve,
            capacity_wh,
            current_soc,
            annotated,
            now_ms,
            [safe_float(slot.get("ts"), 0.0) for slot in export_slots],
            efficiency,
        )

    for slot in export_slots:
        planned_credit = future_export_credit.get("credits", {}).get(str(int(safe_float(slot.get("ts"), 0.0))), {})
        if reserve_limited and not cfg_bool(planned_credit.get("eligible"), False):
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
                        "future_pv_store_credit_required": bool(reserve_limited),
                        "future_pv_store_credit_wh": (
                            safe_float(planned_credit.get("credit_wh"), 0.0)
                            if reserve_limited
                            else 0.0
                        ),
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

    entries, prioritized_limited = _prioritize_export_entries(
        entries,
        reserve,
        capacity_wh,
        flags,
        efficiency,
        annotated=annotated,
    )
    entries, pv_store_budget_limited, pv_store_allocation = _apply_pv_store_energy_budget(
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
        entries, second_budget_limited, second_pv_store_allocation = _apply_pv_store_energy_budget(
            entries,
            reserve,
            capacity_wh,
            flags,
            efficiency,
            current_soc,
        )
        pv_store_allocation = second_pv_store_allocation
        if second_budget_limited > 0:
            pv_store_budget_limited += second_budget_limited
    binding_fixpoint_iterations = 0
    while True:
        filtered_entries, unrealized_marginal_sources = (
            _drop_positive_pv_store_without_final_export(
                entries,
                annotated=annotated,
                load_reserve_enabled=cfg_bool(
                    flags.get("export_segment_load_reserve_enable"),
                    True,
                ),
            )
        )
        if unrealized_marginal_sources <= 0:
            break
        binding_fixpoint_iterations += 1
        entries = filtered_entries
        pv_store_marginal_contract["reason_counts"]["final_export_binding_unrealized"] = (
            pv_store_marginal_contract["reason_counts"].get("final_export_binding_unrealized", 0)
            + unrealized_marginal_sources
        )
        pv_store_marginal_contract["rejected_slot_count"] += unrealized_marginal_sources
        pv_store_marginal_contract["selected_slot_count"] = max(
            0,
            pv_store_marginal_contract["selected_slot_count"] - unrealized_marginal_sources,
        )
        entries, binding_reprioritized_limited = _prioritize_export_entries(
            entries,
            reserve,
            capacity_wh,
            flags,
            efficiency,
            annotated=annotated,
        )
        prioritized_limited += binding_reprioritized_limited
        entries, binding_budget_limited, pv_store_allocation = _apply_pv_store_energy_budget(
            entries,
            reserve,
            capacity_wh,
            flags,
            efficiency,
            current_soc,
        )
        pv_store_budget_limited += binding_budget_limited
    pv_store_marginal_contract["final_binding_fixpoint_iterations"] = binding_fixpoint_iterations
    pv_store_allocation = _finalize_pv_store_allocation_diagnostic(
        pv_store_allocation,
        entries,
    )
    entries, pv_store_live_dc_fallback = _append_current_live_dc_pv_store_fallback(
        entries,
        annotated,
        market_windows,
        config,
        reserve,
        flags,
        mode,
        now_ms,
        current_soc,
        capacity_wh,
        target_soc_limit,
        negative_charge_target,
        previous_policy_decision=previous_policy_decision,
    )
    pv_store_allocation = dict(pv_store_allocation or {})
    pv_store_allocation["live_dc_fallback"] = pv_store_live_dc_fallback
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
    arbitrage_commands = False
    flags["commands_allowed"] = bool(eco_commands or arbitrage_commands)
    if not flags["live_soc_valid"]:
        flags["commands_allowed"] = False
    if not flags.get("settlement_fee_basis_valid", True):
        flags["commands_allowed"] = False

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
    valid_until_ts = _plan_valid_until_ts(windows, now_ms)
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
    policy_binding_fixpoint_iterations = 0
    while True:
        filtered_entries, policy_rejected_sources = (
            _drop_positive_pv_store_without_final_export(
                entries,
                policy_timeline=policy_timeline,
                annotated=annotated,
                load_reserve_enabled=cfg_bool(
                    flags.get("export_segment_load_reserve_enable"),
                    True,
                ),
            )
        )
        if policy_rejected_sources <= 0:
            break
        policy_binding_fixpoint_iterations += 1
        entries = filtered_entries
        pv_store_marginal_contract["reason_counts"]["final_policy_export_unreleased"] = (
            pv_store_marginal_contract["reason_counts"].get(
                "final_policy_export_unreleased",
                0,
            )
            + policy_rejected_sources
        )
        pv_store_marginal_contract["rejected_slot_count"] += policy_rejected_sources
        pv_store_marginal_contract["selected_slot_count"] = max(
            0,
            pv_store_marginal_contract["selected_slot_count"]
            - policy_rejected_sources,
        )
        entries, policy_reprioritized_limited = _prioritize_export_entries(
            entries,
            reserve,
            capacity_wh,
            flags,
            efficiency,
            annotated=annotated,
        )
        prioritized_limited += policy_reprioritized_limited
        entries, policy_budget_limited, pv_store_allocation = (
            _apply_pv_store_energy_budget(
                entries,
                reserve,
                capacity_wh,
                flags,
                efficiency,
                current_soc,
            )
        )
        pv_store_budget_limited += policy_budget_limited
        while True:
            filtered_entries, unrealized_sources = (
                _drop_positive_pv_store_without_final_export(
                    entries,
                    annotated=annotated,
                    load_reserve_enabled=cfg_bool(
                        flags.get("export_segment_load_reserve_enable"),
                        True,
                    ),
                )
            )
            if unrealized_sources <= 0:
                break
            entries = filtered_entries
            pv_store_marginal_contract["reason_counts"]["final_export_binding_unrealized"] = (
                pv_store_marginal_contract["reason_counts"].get(
                    "final_export_binding_unrealized",
                    0,
                )
                + unrealized_sources
            )
            pv_store_marginal_contract["rejected_slot_count"] += unrealized_sources
            pv_store_marginal_contract["selected_slot_count"] = max(
                0,
                pv_store_marginal_contract["selected_slot_count"]
                - unrealized_sources,
            )
            entries, policy_reprioritized_limited = _prioritize_export_entries(
                entries,
                reserve,
                capacity_wh,
                flags,
                efficiency,
                annotated=annotated,
            )
            prioritized_limited += policy_reprioritized_limited
            entries, policy_budget_limited, pv_store_allocation = (
                _apply_pv_store_energy_budget(
                    entries,
                    reserve,
                    capacity_wh,
                    flags,
                    efficiency,
                    current_soc,
                )
            )
            pv_store_budget_limited += policy_budget_limited
        windows = _group_windows(entries)
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
            and (
                flags["negative_price_no_export"]
                or flags.get("low_price_headroom_enable")
            )
            and "eco_plus_negative_headroom_hold" in active_actions
        )
        flags["commands_allowed"] = bool(
            eco_pv_store_commands
            or eco_plus_export_commands
            or eco_negative_headroom_commands
        )
        if not flags["live_soc_valid"] or not flags.get(
            "settlement_fee_basis_valid",
            True,
        ):
            flags["commands_allowed"] = False
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
    pv_store_marginal_contract["final_policy_binding_fixpoint_iterations"] = (
        policy_binding_fixpoint_iterations
    )
    pv_store_marginal_contract = _finalize_pv_store_marginal_contract_diagnostic(
        pv_store_marginal_contract,
        entries,
    )
    pv_store_allocation = _finalize_pv_store_allocation_diagnostic(
        pv_store_allocation,
        entries,
    )
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
    valid_until_ts = _plan_valid_until_ts(windows, now_ms)
    timeline_horizon_slots = [
        {
            "ts": _slot_ts(slot),
            "end_ts": _slot_end_ts(slot),
        }
        for slot in (timeline or [])
        if isinstance(slot, dict)
        and _slot_end_ts(slot) > now_ms
        and not _slot_price_quality_blocker(slot, config)
    ]

    (
        charge_block_wait_slots,
        neutral_policy_slots,
        charge_block_wait_plan,
    ) = _build_charge_block_wait_policy_slots(
        policy_timeline,
        timeline_horizon_slots if timeline_horizon_slots else annotated,
        flags,
        mode,
        now_ms,
    )
    if neutral_policy_slots:
        policy_timeline.extend(neutral_policy_slots)

    if charge_block_wait_slots:
        windows.extend(
            dict(item["selected_window"])
            for item in charge_block_wait_slots
            if isinstance(item.get("selected_window"), dict)
        )
        # Der veröffentlichte DV-Plan ist selbsttragend. Neue Warteslots werden
        # deshalb bereits im Producer vollständig an ihr einziges aktives
        # Zeitfenster gebunden; Test- und Runtime-Consumer dürfen den Vertrag
        # später weder ergänzen noch stillschweigend verändern.
        enriched_charge_block_wait_slots = []
        for item in charge_block_wait_slots:
            start_ts = safe_int(item.get("start_ts"), now_ms)
            end_ts = safe_int(item.get("end_ts"), start_ts + SLOT_MS)
            probe_ms = max(now_ms, start_ts)
            if end_ts > probe_ms:
                probe_ms = min(end_ts - 1, probe_ms)
            enriched_charge_block_wait_slots.append(
                _enrich_policy_candidate_contract(item, windows, probe_ms)
            )
        policy_timeline.extend(enriched_charge_block_wait_slots)
        # Das erneuerte Zeitfenster ist enger als ein neutraler Ursprungsslot
        # und muss deshalb bei gleichem Now-Zeitpunkt vor ihm gewählt werden.
        valid_until_ts = _plan_valid_until_ts(windows, now_ms)
    policy_timeline.sort(
        key=lambda item: (
            safe_int(item.get("start_ts"), 0),
            0 if str(item.get("dv_target_state") or "").upper() == "CHARGE_BLOCK_WAIT" else 1,
            safe_int(item.get("end_ts"), 0),
        )
    )
    policy_timeline = [
        _bind_passive_normal_identity(item, mode)
        for item in policy_timeline
    ]
    policy_decision = next(
        (
            item for item in policy_timeline
            if (
                str(item.get("dv_target_state") or "").upper() == "CHARGE_BLOCK_WAIT"
                and safe_float(item.get("start_ts"), 0.0) <= now_ms < safe_float(item.get("end_ts"), 0.0)
            )
        ),
        None,
    )
    if policy_decision is None:
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
    policy_decision = _bind_passive_normal_identity(
        policy_decision,
        mode,
    )
    policy_end_ts = safe_int(
        policy_decision.get("end_ts") if isinstance(policy_decision, dict) else 0,
        0,
    )
    if policy_end_ts > now_ms:
        valid_until_ts = min(valid_until_ts, policy_end_ts)

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
        "charge_block_wait_plan": charge_block_wait_plan,
        "pv_store_allocation": pv_store_allocation,
        "pv_store_marginal_contract": pv_store_marginal_contract,
        "future_export_credit": future_export_credit,
        "future_pv_store_reservation": future_pv_store_reservation,
    }
