#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lückenloser Direktvermarktungs-Dispatch als reiner Shadow-Vertrag.

Das Modul liest ausschließlich bereits normalisierte Planungsdaten. Es kennt
keine Treiber, keine RSCP-Tags und keinen Hardwareausgang. Der Storage Manager
bleibt der einzige spätere Aktor; dieser Vertrag ist noch nicht ausführbar.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from .direct_marketing_actions import (
        direct_marketing_typed_int_equals,
        storage_action_contract,
    )
except ImportError:
    from direct_marketing_actions import (  # type: ignore
        direct_marketing_typed_int_equals,
        storage_action_contract,
    )


SHADOW_SCHEMA = "direct_marketing_dispatch_shadow_v1"
PLANNING_INPUT_SCHEMA = "planning_input_v1"
DV_PLAN_SCHEMA = "dv_plan_v1"
VALIDATION_SCHEMA = "dv_plan_validation_v1"
PLANNER_VERSION = "explicit_dv_action_adapter_v1"
VALIDATOR_VERSION = "dv_physics_validator_v1"
TIMEZONE = "Europe/Berlin"
SLOT_DURATION_S = 900
SLOT_DURATION_MS = SLOT_DURATION_S * 1000
MAX_SHADOW_SLOTS = 400
ALLOWED_ACTIONS = (
    "HOUSE_SUPPLY",
    "PV_STORE",
    "CHARGE_BLOCK_WAIT",
    "GRID_CHARGE",
    "ECONOMIC_EXPORT",
    "HEADROOM_EXPORT",
    "DV_CURVE_CHARGE",
)
KNOWN_SOURCE_ACTIONS = {
    "eco_plus_store_pv_candidate",
    "arbitrage_grid_charge_candidate",
    "eco_plus_export_candidate",
    "arbitrage_export_candidate",
    "eco_plus_negative_headroom_hold",
    "direct_marketing_charge_block_wait",
    "keep_headroom",
    "charge_block_wait",
    "dv_curve_charge",
    "eco_plus_curve_charge",
    "eco_plus_curve_charge_candidate",
}


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _safe_int(value: Any, default: int = 0) -> int:
    number = _safe_float(value, None)
    return int(number) if number is not None else int(default)


def _explicit_bool(*values: Any, default: bool = False) -> bool:
    for value in values:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on", "ja", "ein"}:
                return True
            if normalized in {"0", "false", "no", "off", "nein", "aus"}:
                return False
    return default


def _round(value: Any, digits: int = 3) -> Optional[float]:
    number = _safe_float(value, None)
    return round(number, digits) if number is not None else None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _revision(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _point_forecast(contract: Any) -> Optional[float]:
    """Liest den Legacy-Punktkanal, ohne daraus ein Quantil zu behaupten."""

    if not isinstance(contract, dict):
        return None
    return _safe_float(
        contract.get("point"),
        _safe_float(contract.get("p50"), None),
    )


def _capacity_wh(source: Dict[str, Any]) -> float:
    capacity = _safe_float(source.get("battery_capacity"), 0.0) or 0.0
    if 1.0 < capacity < 500.0:
        capacity *= 1000.0
    if capacity <= 1000.0:
        capacity = (_safe_float(source.get("bat_cap_kwh"), 0.0) or 0.0) * 1000.0
    return max(0.0, capacity)


def _hardware_limit(source: Dict[str, Any], *keys: str) -> float:
    for key in keys:
        number = _safe_float(source.get(key), None)
        if number is not None:
            return number
    return 0.0


def _direct_contract(source: Dict[str, Any]) -> Dict[str, Any]:
    value = source.get("direct_marketing")
    return value if isinstance(value, dict) else {}


def _direct_flags(source: Dict[str, Any]) -> Dict[str, Any]:
    direct = _direct_contract(source)
    value = direct.get("flags")
    return value if isinstance(value, dict) else {}


def _permissions(source: Dict[str, Any]) -> Dict[str, Any]:
    direct = _direct_contract(source)
    flags = _direct_flags(source)
    mode = str(direct.get("mode") or "").strip().lower()
    aux_ac_mode = str(
        flags.get("pv_store_aux_ac_mode") or "off"
    ).strip().lower()
    if aux_ac_mode not in {
        "off",
        "reserve_only",
        "house_supply",
        "economic",
    }:
        aux_ac_mode = "off"
    direct_enabled = bool(
        direct.get("active") is True
        and direct.get("shadow") is False
        and mode in {"eco", "eco_plus", "arbitrage"}
        and flags.get("commands_allowed") is True
    )
    return {
        "direct_marketing_enabled": direct_enabled,
        "pv_store_enabled": _explicit_bool(
            flags.get("pv_store_enable"),
            source.get("direct_marketing_pv_store_enable"),
            default=False,
        ),
        "economic_export_enabled": _explicit_bool(
            flags.get("export_enable"),
            source.get("direct_marketing_export_enable"),
            default=False,
        ),
        "grid_charge_enabled": _explicit_bool(
            flags.get("grid_charge_enable"),
            source.get("direct_marketing_grid_charge_enable"),
            source.get("market_battery_grid_charge_enable"),
            source.get("grid_charge_enable"),
            default=False,
        ),
        "external_ac_storage_enabled": _explicit_bool(
            flags.get("pv_store_aux_ac_storage_enable"),
            source.get("direct_marketing_pv_store_aux_ac_storage_enable"),
            source.get("pv_store_aux_ac_storage_enable"),
            default=False,
        ),
        "external_ac_storage_mode": aux_ac_mode,
        "external_ac_storage_mode_source": str(
            flags.get("pv_store_aux_ac_mode_source") or "default_off"
        ),
        "external_ac_house_supply_evidence_status": str(
            flags.get("pv_store_aux_ac_house_supply_evidence_status")
            or (
                "evidence_limit"
                if aux_ac_mode == "house_supply"
                else "not_applicable"
            )
        ),
        "external_ac_house_supply_evidence_revision": str(
            flags.get("pv_store_aux_ac_house_supply_evidence_revision")
            or ""
        ) or None,
        "external_ac_fallback_supported": False,
        "dc_first_required": True,
    }


def _state_contract(source: Dict[str, Any], canonical_slots: List[Dict[str, Any]]) -> Dict[str, Any]:
    flags = _direct_flags(source)
    first_soc = None
    if canonical_slots:
        soc_contract = canonical_slots[0].get("soc_pct")
        if isinstance(soc_contract, dict):
            first_soc = _safe_float(soc_contract.get("start"), None)
            if first_soc is None:
                first_soc = _safe_float(soc_contract.get("end"), None)
    current_soc = _safe_float(source.get("current_soc"), first_soc)
    state_fresh = _explicit_bool(
        source.get("_runtime_soc_valid"),
        source.get("live_soc_valid"),
        flags.get("live_soc_valid"),
        default=False,
    )
    return {
        "initial_soc_pct": _round(current_soc),
        "state_fresh": state_fresh,
        "capacity_wh": _round(_capacity_wh(source)),
        "hard_reserve_soc_pct": _round(
            source.get("physical_reserve_soc", source.get("notstrom_reserve_soc"))
        ),
        "ceiling_soc_pct": _round(source.get("adaptive_soc_ceiling", 100.0)),
    }


def _topology_slot(slot: Dict[str, Any]) -> Dict[str, Any]:
    forecast = slot.get("forecast_w") if isinstance(slot.get("forecast_w"), dict) else {}
    topology = forecast.get("topology") if isinstance(forecast.get("topology"), dict) else {}
    evidence = forecast.get("evidence") if isinstance(forecast.get("evidence"), dict) else {}
    dc_pv_w = _point_forecast(forecast.get("e3dc_dc_pv"))
    external_ac_w = _point_forecast(forecast.get("external_ac_pv"))
    status = str(topology.get("status") or "topology_unbound")
    quality = str(topology.get("quality") or "missing")
    reason = str(topology.get("reason") or "TOPOLOGY_REASON_MISSING")
    projection_status = str(
        topology.get("resource_projection_status") or "unbound"
    )
    projection_reason = str(
        topology.get("resource_projection_reason")
        or "RESOURCE_PROJECTION_REASON_MISSING"
    )
    pv_forecast_fresh = evidence.get("pv_fresh") is True
    complete = bool(
        status == "bound"
        and quality in {"complete", "bound"}
        and reason == "OK"
        and topology.get("revision")
        and dc_pv_w is not None
        and external_ac_w is not None
        and projection_status in {"complete", "bound"}
        and projection_reason == "OK"
        and pv_forecast_fresh
    )
    return {
        "status": status,
        "complete": complete,
        "revision": topology.get("revision"),
        "e3dc_dc_pv_w": _round(dc_pv_w),
        "external_ac_pv_w": _round(external_ac_w),
        "pv_forecast_fresh": pv_forecast_fresh,
        "pv_forecast_freshness_source": str(
            evidence.get("pv_freshness_source") or "unconfirmed"
        ),
        "load_forecast_valid": evidence.get("load_valid") is True,
        "load_forecast_validity_source": str(
            evidence.get("load_validity_source") or "unconfirmed"
        ),
    }


def _planning_goals(source: Dict[str, Any]) -> Dict[str, Any]:
    target_soc = None
    for key in ("planning_target_soc", "target_soc", "target_soc_pct"):
        target_soc = _safe_float(source.get(key), None)
        if target_soc is not None:
            break
    return {
        "target_soc_pct": _round(target_soc),
        "grid_charge_requires_forecast_deficit": True,
    }


def _timestamp_ms(value: Any) -> int:
    parsed = _safe_int(value)
    if 0 < parsed < 100_000_000_000:
        parsed *= 1000
    return parsed


def _current_policy_protected_reserve_wh(
    direct: Dict[str, Any],
    generated_ms: int,
) -> Optional[float]:
    candidates: List[float] = []
    policies = []
    current = direct.get("policy_decision")
    if isinstance(current, dict):
        policies.append(current)
    policies.extend(
        item
        for item in (direct.get("policy_timeline") or [])
        if isinstance(item, dict)
    )
    for policy in policies:
        start_ms = _timestamp_ms(policy.get("start_ts"))
        end_ms = _timestamp_ms(policy.get("end_ts"))
        if not (start_ms <= generated_ms < end_ms):
            continue
        budget = (
            policy.get("storage_budget")
            if isinstance(policy.get("storage_budget"), dict)
            else {}
        )
        value = _safe_float(budget.get("protected_reserve_wh"), None)
        if value is not None and value >= 0.0:
            candidates.append(value)
    return max(candidates) if candidates else None


def _reserve_class_contracts(
    source: Dict[str, Any],
    state: Dict[str, Any],
    goals: Dict[str, Any],
    generated_ms: int,
) -> Dict[str, Any]:
    """Trennt physischen Floor, Bedarfsreserve und weiches Ladeziel.

    Ein fehlender Bedarfszeitpunkt oder eine reine Punktprognose wird
    absichtlich nicht als belastbare 0-W-Entscheidung ausgelegt.
    """

    direct = _direct_contract(source)
    capacity_wh = max(
        0.0,
        _safe_float(state.get("capacity_wh"), 0.0) or 0.0,
    )
    current_soc_pct = _safe_float(state.get("initial_soc_pct"), None)
    current_stored_wh = (
        capacity_wh * current_soc_pct / 100.0
        if capacity_wh > 0.0 and current_soc_pct is not None
        else None
    )
    hard_floor_pct = _safe_float(
        state.get("hard_reserve_soc_pct"),
        None,
    )
    hard_floor_wh = (
        capacity_wh * hard_floor_pct / 100.0
        if capacity_wh > 0.0 and hard_floor_pct is not None
        else None
    )
    hard_physical_floor = {
        "schema_version": "hard_physical_floor_v1",
        "soc_pct": _round(hard_floor_pct),
        "stored_wh": _round(hard_floor_wh),
        "source": "physical_reserve_soc_or_notstrom_reserve_soc",
        "immediate": True,
    }
    hard_physical_floor["revision"] = _revision(hard_physical_floor)

    explicit = source.get("protected_demand_reserve")
    if not isinstance(explicit, dict):
        explicit = direct.get("protected_demand_reserve")
    if not isinstance(explicit, dict):
        explicit = {}
    conservative = (
        explicit.get("conservative_refillability")
        if isinstance(explicit.get("conservative_refillability"), dict)
        else {}
    )
    reserve_candidates: List[Tuple[str, float]] = []

    def add_reserve_candidate(source_name: str, value: Any) -> None:
        parsed = _safe_float(value, None)
        if parsed is None or parsed < 0.0:
            return
        reserve_candidates.append((source_name, parsed))

    explicit_required_wh = _safe_float(
        explicit.get("required_stored_wh"),
        None,
    )
    add_reserve_candidate(
        "explicit_protected_demand_reserve",
        explicit_required_wh,
    )
    policy_required_wh = _current_policy_protected_reserve_wh(
        direct,
        generated_ms,
    )
    add_reserve_candidate(
        "current_direct_marketing_policy_budget",
        policy_required_wh,
    )
    reservation = (
        direct.get("future_pv_store_reservation")
        if isinstance(direct.get("future_pv_store_reservation"), dict)
        else {}
    )
    reservation_required_wh = _safe_float(
        reservation.get("protected_energy_wh"),
        None,
    )
    add_reserve_candidate(
        "future_pv_store_reservation",
        reservation_required_wh,
    )
    reserve_state = (
        direct.get("reserve")
        if isinstance(direct.get("reserve"), dict)
        else {}
    )
    static_reserve_pct = max(
        (
            value
            for value in (
                _safe_float(
                    reserve_state.get("home_reserve_soc_pct"),
                    None,
                ),
                _safe_float(
                    reserve_state.get("night_reserve_soc_pct"),
                    None,
                ),
            )
            if value is not None
        ),
        default=None,
    )
    if capacity_wh > 0.0 and static_reserve_pct is not None:
        add_reserve_candidate(
            "static_house_or_night_reserve",
            capacity_wh * max(0.0, static_reserve_pct) / 100.0,
        )
    add_reserve_candidate("hard_physical_floor", hard_floor_wh)
    required_stored_wh = (
        max(value for _source_name, value in reserve_candidates)
        if reserve_candidates
        else None
    )
    requirement_candidates_wh = {
        source_name: _round(
            max(
                value
                for candidate_source, value in reserve_candidates
                if candidate_source == source_name
            )
        )
        for source_name in sorted({
            source_name
            for source_name, _value in reserve_candidates
        })
    }
    required_sources = (
        sorted({
            source_name
            for source_name, value in reserve_candidates
            if required_stored_wh is not None
            and abs(value - required_stored_wh) <= 0.001
        })
        if required_stored_wh is not None
        else []
    )
    deadline_ms = _timestamp_ms(
        explicit.get("deadline_ts_ms", explicit.get("deadline_ts"))
    )
    shortfall_wh = (
        max(0.0, required_stored_wh - current_stored_wh)
        if required_stored_wh is not None
        and current_stored_wh is not None
        else None
    )
    external_refillability_claim_complete = bool(
        explicit.get("schema_version") == "protected_demand_reserve_v1"
        and str(explicit.get("evidence_status") or "").upper() == "COMPLETE"
        and deadline_ms > generated_ms
        and conservative.get("status") in {
            "PROVEN_REFILLABLE",
            "PROVEN_SHORTFALL",
        }
        and conservative.get("revision")
    )
    if shortfall_wh is None:
        reserve_status = "EVIDENCE_LIMIT"
        reserve_reason = "PROTECTED_DEMAND_RESERVE_INPUT_MISSING"
    elif shortfall_wh <= 50.0:
        reserve_status = "SATISFIED_NOW"
        reserve_reason = "PROTECTED_DEMAND_STATIC_FLOOR_ACTIVE"
    elif (
        external_refillability_claim_complete
        and conservative.get("status") == "PROVEN_SHORTFALL"
    ):
        reserve_status = "EVIDENCE_LIMIT"
        reserve_reason = "PROTECTED_DEMAND_RECOVERY_PATH_NOT_IMPLEMENTED"
    elif external_refillability_claim_complete:
        # Ein Statusstring und irgendeine Revision sind noch kein
        # wissenschaftlich belastbarer Wiederbefüllbarkeitsnachweis. Der
        # Shadow muss Szenariomaterial, Quelle, Kapazitätsuntergrenze,
        # Wirkungsgrade und Leistungsgrenzen selbst nachrechnen.
        reserve_status = "EVIDENCE_LIMIT"
        reserve_reason = (
            "PROTECTED_DEMAND_REFILLABILITY_VALIDATOR_NOT_IMPLEMENTED"
        )
    else:
        reserve_status = "EVIDENCE_LIMIT"
        reserve_reason = (
            "PROTECTED_DEMAND_DEADLINE_MISSING"
            if deadline_ms <= generated_ms
            else "PROTECTED_DEMAND_CONSERVATIVE_REFILLABILITY_MISSING"
        )
    permissions = _permissions(source)
    protected_demand_reserve = {
        "schema_version": "protected_demand_reserve_v1",
        "status": reserve_status,
        "reason_code": reserve_reason,
        "required_stored_wh": _round(required_stored_wh),
        "current_stored_wh": _round(current_stored_wh),
        "shortfall_wh": _round(shortfall_wh),
        "current_requirement_met": (
            shortfall_wh is not None and shortfall_wh <= 50.0
        ),
        "requirement_candidates_wh": requirement_candidates_wh,
        "required_sources": required_sources,
        "deadline_ts_ms": deadline_ms or None,
        "demand_class": str(
            explicit.get("demand_class")
            or "house_night_weather_wallbox_reserve"
        ),
        "source": (
            required_sources[0]
            if len(required_sources) == 1
            else "max_protected_requirement"
        ),
        "uncertainty_contract": (
            str(conservative.get("uncertainty_contract") or "")
            or "POINT_FORECAST_WITHOUT_QUANTILE_NOT_SUFFICIENT"
        ),
        "conservative_refillability": copy.deepcopy(conservative),
        "refillability_evidence_status": (
            "UNVALIDATED_EXTERNAL_CLAIM"
            if external_refillability_claim_complete
            else "POINT_FORECAST_WITHOUT_QUANTILE_NOT_SUFFICIENT"
        ),
        "protection_semantics": (
            "STATIC_CONSERVATIVE_FLOOR_UNTIL_DEADLINE_CONTRACT_AVAILABLE"
        ),
        "external_ac_storage_mode": permissions.get(
            "external_ac_storage_mode"
        ),
        "external_ac_storage_mode_source": permissions.get(
            "external_ac_storage_mode_source"
        ),
        "eligible_for_shadow_decision": reserve_status != "EVIDENCE_LIMIT",
        "eligible_for_refill_decision": False,
    }
    protected_demand_reserve["revision"] = _revision(
        protected_demand_reserve
    )

    soft_charge_target = {
        "schema_version": "soft_charge_target_v1",
        "target_soc_pct": _round(goals.get("target_soc_pct")),
        "deadline_ts_ms": None,
        "hard_floor": False,
        "source": "planning_target_soc",
    }
    soft_charge_target["revision"] = _revision(soft_charge_target)
    return {
        "hard_physical_floor": hard_physical_floor,
        "protected_demand_reserve": protected_demand_reserve,
        "soft_charge_target": soft_charge_target,
    }


def _forecast_charge_adequacy(
    state: Dict[str, Any],
    goals: Dict[str, Any],
    slots: List[Dict[str, Any]],
    charge_efficiency: float,
    max_charge_w: float,
) -> Dict[str, Any]:
    capacity_wh = _safe_float(state.get("capacity_wh"), 0.0) or 0.0
    current_soc = _safe_float(state.get("initial_soc_pct"), None)
    target_soc = _safe_float(goals.get("target_soc_pct"), None)
    complete = bool(slots) and all(
        item.get("topology_complete") is True
        and item.get("pv_forecast_fresh") is True
        and item.get("load_forecast_valid") is True
        for item in slots
    )
    if (
        not complete
        or capacity_wh <= 0.0
        or current_soc is None
        or target_soc is None
    ):
        return {
            "status": "EVIDENCE_INCOMPLETE",
            "required_source_wh": None,
            "forecast_dc_charge_potential_wh": None,
            "forecast_charge_deficit_wh": None,
        }
    stored_energy_needed_wh = max(
        0.0,
        (target_soc - current_soc) / 100.0 * capacity_wh,
    )
    required_source_wh = stored_energy_needed_wh / max(charge_efficiency, 0.001)
    potential_wh = 0.0
    for item in slots:
        dc_w = max(0.0, _safe_float(item.get("e3dc_dc_pv_w"), 0.0) or 0.0)
        external_ac_w = max(
            0.0,
            _safe_float(item.get("external_ac_pv_w"), 0.0) or 0.0,
        )
        load_w = max(0.0, _safe_float(item.get("load_w"), 0.0) or 0.0)
        residual_load_after_external_ac_w = max(0.0, load_w - external_ac_w)
        potential_wh += min(
            max(0.0, max_charge_w),
            max(0.0, dc_w - residual_load_after_external_ac_w),
        ) * (SLOT_DURATION_S / 3600.0)
    return {
        "status": "COMPLETE",
        "required_source_wh": _round(required_source_wh),
        "forecast_dc_charge_potential_wh": _round(potential_wh),
        "forecast_charge_deficit_wh": _round(max(0.0, required_source_wh - potential_wh)),
    }


def _planning_slot(slot: Dict[str, Any]) -> Dict[str, Any]:
    prices = slot.get("prices_ct_kwh") if isinstance(slot.get("prices_ct_kwh"), dict) else {}
    forecast = slot.get("forecast_w") if isinstance(slot.get("forecast_w"), dict) else {}
    topology = _topology_slot(slot)
    total_pv_w = _point_forecast(forecast.get("pv"))
    load_w = _point_forecast(forecast.get("load"))
    row = {
        "start_ts_ms": _safe_int(slot.get("start_ts_ms")),
        "end_ts_ms": _safe_int(slot.get("end_ts_ms")),
        "buy_ct_kwh": _round(prices.get("buy")),
        "net_sell_ct_kwh": _round(prices.get("net_sell")),
        "price_fresh": prices.get("fresh") is True,
        "price_status": str(prices.get("status") or "unknown"),
        "pv_total_w": _round(total_pv_w),
        "e3dc_dc_pv_w": topology["e3dc_dc_pv_w"],
        "external_ac_pv_w": topology["external_ac_pv_w"],
        "load_w": _round(load_w),
        "topology_status": topology["status"],
        "topology_complete": topology["complete"],
        "topology_revision": topology["revision"],
        "pv_forecast_fresh": topology["pv_forecast_fresh"],
        "pv_forecast_freshness_source": topology[
            "pv_forecast_freshness_source"
        ],
        "load_forecast_valid": topology["load_forecast_valid"],
        "load_forecast_validity_source": topology[
            "load_forecast_validity_source"
        ],
    }
    row["input_slot_id"] = _revision(row)
    return row


def build_planning_input_v1(
    source: Dict[str, Any],
    canonical_plan: Dict[str, Any],
) -> Dict[str, Any]:
    """Baut einen entscheidungsfreien, kompakten Planungseingang."""

    canonical_slots = [
        item
        for item in (canonical_plan.get("slots") or [])
        if isinstance(item, dict)
    ]
    if len(canonical_slots) > MAX_SHADOW_SLOTS:
        raise ValueError("DV_SHADOW_HORIZON_TOO_LARGE")
    slots = [_planning_slot(item) for item in canonical_slots]
    state = _state_contract(source, canonical_slots)
    goals = _planning_goals(source)
    generated_ms = _safe_int(canonical_plan.get("generated_at_ts_ms"))
    reserve_classes = _reserve_class_contracts(
        source,
        state,
        goals,
        generated_ms,
    )
    charge_efficiency_pct = _safe_float(
        source.get("charge_efficiency_pct"),
        None,
    )
    if charge_efficiency_pct is None:
        charge_efficiency_pct = 95.0
    discharge_efficiency_pct = _safe_float(
        source.get("discharge_efficiency_pct"),
        None,
    )
    if discharge_efficiency_pct is None:
        discharge_efficiency_pct = 95.0
    charge_efficiency = charge_efficiency_pct / 100.0
    discharge_efficiency = discharge_efficiency_pct / 100.0
    hardware_limits = {
        "max_charge_w": _round(
            _hardware_limit(source, "max_charge_w", "maximumladeleistung")
        ),
        "max_discharge_w": _round(
            _hardware_limit(source, "max_discharge_w", "maximaleentladeleistung")
        ),
        "max_grid_import_w": _round(
            _hardware_limit(source, "max_grid_import_w", "grid_import_limit_w")
        ),
        "max_grid_export_w": _round(
            _hardware_limit(source, "export_limit_w", "max_grid_export_w")
        ),
        "max_grid_charge_w": _round(
            _safe_float(
                _direct_flags(source).get("max_grid_charge_w"),
                _hardware_limit(
                    source,
                    "direct_marketing_max_grid_charge_w",
                    "market_battery_max_charge_w",
                ),
            )
        ),
        "max_economic_export_w": _round(
            _safe_float(
                _direct_flags(source).get("max_export_w"),
                _hardware_limit(source, "direct_marketing_max_export_w"),
            )
        ),
    }
    charge_adequacy = _forecast_charge_adequacy(
        state,
        goals,
        slots,
        charge_efficiency,
        _safe_float(hardware_limits.get("max_charge_w"), 0.0) or 0.0,
    )
    permissions = _permissions(source)
    input_revisions = {
        "price": _revision([
            {
                "start_ts_ms": item["start_ts_ms"],
                "buy_ct_kwh": item["buy_ct_kwh"],
                "net_sell_ct_kwh": item["net_sell_ct_kwh"],
                "fresh": item["price_fresh"],
                "status": item["price_status"],
            }
            for item in slots
        ]),
        "pv_forecast": _revision([
            {
                "start_ts_ms": item["start_ts_ms"],
                "total_w": item["pv_total_w"],
                "e3dc_dc_w": item["e3dc_dc_pv_w"],
                "external_ac_w": item["external_ac_pv_w"],
                "fresh": item["pv_forecast_fresh"],
                "freshness_source": item["pv_forecast_freshness_source"],
            }
            for item in slots
        ]),
        "load_forecast": _revision([
            {
                "start_ts_ms": item["start_ts_ms"],
                "load_w": item["load_w"],
                "valid": item["load_forecast_valid"],
                "validity_source": item["load_forecast_validity_source"],
            }
            for item in slots
        ]),
        "topology": _revision([
            {
                "start_ts_ms": item["start_ts_ms"],
                "status": item["topology_status"],
                "complete": item["topology_complete"],
                "revision": item["topology_revision"],
            }
            for item in slots
        ]),
        "storage_state": _revision(state),
        "hardware_limits": _revision(hardware_limits),
        "permissions": _revision(permissions),
        "planning_goals": _revision(goals),
        "reserve_classes": _revision(reserve_classes),
    }
    input_contract = {
        "schema_version": PLANNING_INPUT_SCHEMA,
        "generated_at_ts_ms": generated_ms,
        "valid_from_ts_ms": slots[0]["start_ts_ms"] if slots else 0,
        "horizon_end_ts_ms": slots[-1]["end_ts_ms"] if slots else 0,
        "timezone": str(canonical_plan.get("timezone") or TIMEZONE),
        "slot_duration_s": SLOT_DURATION_S,
        "shadow_only": True,
        "commands_allowed": False,
        "input_revisions": input_revisions,
        "storage": state,
        "planning_goals": goals,
        **reserve_classes,
        "forecast_charge_adequacy": charge_adequacy,
        "hardware_limits": hardware_limits,
        "permissions": permissions,
        "efficiency": {
            "charge": _round(charge_efficiency, 6),
            "discharge": _round(discharge_efficiency, 6),
        },
        "slots": slots,
    }
    # Die Revision bindet ausschließlich die Whitelist oben. Legacy-`charge_w`,
    # `planned_action` und Batterieprojektionen sind absichtlich nicht enthalten.
    input_contract["input_id"] = _revision(input_contract)
    return input_contract


def _interval_contains(item: Dict[str, Any], start_ms: int, end_ms: int) -> bool:
    return bool(
        _safe_int(item.get("start_ts")) <= start_ms
        and end_ms <= _safe_int(item.get("end_ts"))
    )


def _source_action(item: Dict[str, Any]) -> str:
    selected = item.get("selected_window") if isinstance(item.get("selected_window"), dict) else {}
    return str(
        item.get("source_action")
        or item.get("executable_action")
        or selected.get("action")
        or item.get("action")
        or ""
    ).strip().lower()


def _mapped_action(item: Dict[str, Any]) -> Tuple[str, str, str]:
    target = str(item.get("dv_target_state") or "").strip().upper()
    source_action = _source_action(item)
    storage_budget = (
        item.get("storage_budget")
        if isinstance(item.get("storage_budget"), dict)
        else {}
    )
    if (
        target in {"", "HOLD", "NORMAL"}
        and item.get("commands_allowed") is not True
        and source_action not in {
            "eco_plus_negative_headroom_hold",
            "keep_headroom",
            "charge_block_wait",
        }
    ):
        return "HOUSE_SUPPLY", "NORMAL_OPERATION", "NO_EXPLICIT_DV_ACTION"
    if target == "FORCE_CHARGE_PV" or source_action == "eco_plus_store_pv_candidate":
        return "PV_STORE", "PV_SHIFT", "EXPLICIT_PV_STORE_WINDOW"
    if target in {"GRID_CHARGE", "FORCE_GRID_CHARGE"} or source_action == "arbitrage_grid_charge_candidate":
        return "GRID_CHARGE", "PRICE_ARBITRAGE", "EXPLICIT_GRID_CHARGE_WINDOW"
    if target == "DV_CURVE_CHARGE" or source_action in {
        "dv_curve_charge",
        "eco_plus_curve_charge",
        "eco_plus_curve_charge_candidate",
    }:
        return "DV_CURVE_CHARGE", "NIGHT_RESERVE_PROTECTION", "NIGHT_AUTARKY_CURVE_CHARGE"
    if (
        target == "HEADROOM_EXPORT"
        and (
            storage_budget.get("headroom_hold_active") is True
            or (_safe_float(storage_budget.get("export_budget_w"), 0.0) or 0.0) < 300.0
        )
    ):
        return "CHARGE_BLOCK_WAIT", "HEADROOM_RESERVATION", "EXPLICIT_CHARGE_BLOCK_WINDOW"
    if target in {"FORCE_EXPORT", "HEADROOM_EXPORT"} or source_action in {
        "eco_plus_export_candidate",
        "arbitrage_export_candidate",
    }:
        purpose = "HEADROOM_PREPARE" if target == "HEADROOM_EXPORT" else "MARKET_SALE"
        return "ECONOMIC_EXPORT", purpose, "EXPLICIT_EXPORT_WINDOW"
    if target == "CHARGE_BLOCK_WAIT" or source_action in {
        "eco_plus_negative_headroom_hold",
        "keep_headroom",
        "charge_block_wait",
    }:
        return "CHARGE_BLOCK_WAIT", "HEADROOM_RESERVATION", "EXPLICIT_CHARGE_BLOCK_WINDOW"
    return "HOUSE_SUPPLY", "NORMAL_OPERATION", "NO_EXPLICIT_DV_ACTION"


def _policy_binding_valid(
    direct: Dict[str, Any],
    item: Dict[str, Any],
    action: str,
    start_ms: int,
    end_ms: int,
) -> bool:
    if action == "HOUSE_SUPPLY":
        return True
    if item.get("commands_allowed") is not True or item.get("blocked") is True:
        return False
    source_action = str(item.get("source_action") or "").strip().lower()
    executable_action = str(
        item.get("executable_action") or ""
    ).strip().lower()
    selected = (
        item.get("selected_window")
        if isinstance(item.get("selected_window"), dict)
        else {}
    )
    execution = (
        item.get("execution_window")
        if isinstance(item.get("execution_window"), dict)
        else {}
    )
    selected_action = str(selected.get("action") or "").strip().lower()
    execution_action = str(execution.get("action") or "").strip().lower()
    selected_start_ms = _safe_int(selected.get("start_ts"))
    selected_end_ms = _safe_int(selected.get("end_ts"))
    execution_start_ms = _safe_int(execution.get("start_ts"))
    execution_end_ms = _safe_int(execution.get("end_ts"))
    plan_window_start_ms = _safe_int(execution.get("plan_window_start_ts"))
    plan_window_end_ms = _safe_int(execution.get("plan_window_end_ts"))
    selected_window_id = str(
        selected.get("window_id")
        or item.get("window_id")
        or selected.get("export_plateau_id")
        or ""
    )
    execution_window_id = str(execution.get("window_id") or "")
    selected_plan_window_id = str(
        selected.get("plan_window_id")
        or selected.get("export_plateau_id")
        or selected.get("market_window_id")
        or selected_window_id
        or ""
    )
    execution_plan_window_id = str(
        execution.get("plan_window_id")
        or execution_window_id
        or ""
    )
    target = str(item.get("dv_target_state") or "").strip().upper()
    expected_actions = {
        "PV_STORE": (
            {"FORCE_CHARGE_PV"},
            {"eco_plus_store_pv_candidate"},
        ),
        "GRID_CHARGE": (
            {"GRID_CHARGE", "FORCE_GRID_CHARGE"},
            {"arbitrage_grid_charge_candidate"},
        ),
        "ECONOMIC_EXPORT": (
            {"FORCE_EXPORT", "HEADROOM_EXPORT"},
            {"eco_plus_export_candidate", "arbitrage_export_candidate"},
        ),
        "CHARGE_BLOCK_WAIT": (
            {"CHARGE_BLOCK_WAIT", "HEADROOM_EXPORT"},
            {
                "direct_marketing_charge_block_wait",
                "eco_plus_negative_headroom_hold",
                "keep_headroom",
                "charge_block_wait",
            },
        ),
        "DV_CURVE_CHARGE": (
            {"DV_CURVE_CHARGE"},
            {"eco_plus_curve_charge_candidate"},
        ),
    }
    expected_targets, expected_sources = expected_actions.get(
        action,
        (set(), set()),
    )
    plan_windows = []
    for window in direct.get("windows") or []:
        if not isinstance(window, dict):
            continue
        plan_window_id = str(
            window.get("export_plateau_id")
            or window.get("market_window_id")
            or window.get("window_id")
            or ""
        )
        if (
            str(window.get("action") or "") == source_action
            and plan_window_id == execution_plan_window_id
            and _safe_int(window.get("start_ts")) == plan_window_start_ms
            and _safe_int(window.get("end_ts")) == plan_window_end_ms
        ):
            plan_windows.append(window)
    return bool(
        source_action in KNOWN_SOURCE_ACTIONS
        and target in expected_targets
        and source_action in expected_sources
        and executable_action == source_action
        and selected_action == source_action
        and execution_action == source_action
        and direct_marketing_typed_int_equals(
            execution.get("contract_version"),
            1,
        )
        and execution.get("source") == "active_plan_window"
        and direct_marketing_typed_int_equals(
            item.get("execution_window_match_count"),
            1,
        )
        and selected_window_id
        and execution_window_id == selected_window_id
        and selected_plan_window_id
        and execution_plan_window_id == selected_plan_window_id
        and selected_start_ms <= start_ms
        and end_ms <= selected_end_ms
        and selected_start_ms <= execution_start_ms
        and execution_end_ms <= selected_end_ms
        and execution_start_ms <= start_ms
        and end_ms <= execution_end_ms
        and plan_window_start_ms <= execution_start_ms
        and execution_end_ms <= plan_window_end_ms
        and len(plan_windows) == 1
    )


def _candidate_for_slot(
    direct: Dict[str, Any],
    start_ms: int,
    end_ms: int,
) -> Tuple[Dict[str, Any], str, str, str]:
    policies = [
        item
        for item in (direct.get("policy_timeline") or [])
        if isinstance(item, dict) and _interval_contains(item, start_ms, end_ms)
    ]
    active_policies = []
    neutral_policies = []
    blocked_active_policy = False
    for item in policies:
        action, purpose, reason = _mapped_action(item)
        if action == "HOUSE_SUPPLY":
            neutral_policies.append((item, action, purpose, reason))
        elif _policy_binding_valid(direct, item, action, start_ms, end_ms):
            active_policies.append((item, action, purpose, reason))
        else:
            blocked_active_policy = True
    if len(active_policies) > 1:
        marker = {"mapping_blocker_code": "DV_POLICY_ACTION_AMBIGUOUS"}
        return marker, "HOUSE_SUPPLY", "NORMAL_OPERATION", "AMBIGUOUS_ACTIVE_POLICY"
    if active_policies:
        return active_policies[0]
    if neutral_policies:
        neutral_policies.sort(
            key=lambda entry: (
                _safe_int(entry[0].get("end_ts"))
                - _safe_int(entry[0].get("start_ts"))
            )
        )
        return neutral_policies[0]
    if blocked_active_policy:
        return {}, "HOUSE_SUPPLY", "NORMAL_OPERATION", "SOURCE_POLICY_NOT_EXECUTABLE"

    current_policy = (
        direct.get("policy_decision")
        if isinstance(direct.get("policy_decision"), dict)
        else {}
    )
    if current_policy and _interval_contains(current_policy, start_ms, end_ms):
        action, purpose, reason = _mapped_action(current_policy)
        if _policy_binding_valid(
            direct,
            current_policy,
            action,
            start_ms,
            end_ms,
        ):
            return current_policy, action, purpose, reason
        if action != "HOUSE_SUPPLY":
            return {}, "HOUSE_SUPPLY", "NORMAL_OPERATION", "SOURCE_POLICY_NOT_EXECUTABLE"

    # Rohe `windows` sind Kandidaten und niemals ein ausführbarer Vertrag.
    # Sie werden erst nach der Producer-Selektion über policy_timeline adaptiert.
    return {}, "HOUSE_SUPPLY", "NORMAL_OPERATION", "NO_EXPLICIT_DV_ACTION"


def _eco_plus_normal_zero_charge_runtime_candidate(
    direct: Dict[str, Any],
    source_item: Dict[str, Any],
) -> bool:
    """Erkennt die Policykante, ohne fehlenden Runtimezustand zu erfinden."""

    return bool(
        direct.get("active") is True
        and direct.get("shadow") is False
        and str(direct.get("mode") or "").strip().lower() == "eco_plus"
        and source_item.get("schema") == "direct_marketing_policy_v1"
        and str(source_item.get("dv_target_state") or "").strip().upper()
        == "NORMAL"
        and source_item.get("commands_allowed") is False
        and source_item.get("blocked") is not True
    )


def _requested_power(
    direct: Dict[str, Any],
    item: Dict[str, Any],
    action: str,
) -> float:
    selected = item.get("selected_window") if isinstance(item.get("selected_window"), dict) else {}
    execution = item.get("execution_window") if isinstance(item.get("execution_window"), dict) else {}
    budget = item.get("storage_budget") if isinstance(item.get("storage_budget"), dict) else {}
    if action in {"PV_STORE", "GRID_CHARGE"}:
        candidates = (
            budget.get("charge_budget_w"),
            selected.get("max_power_w"),
            item.get("max_power_w"),
        )
    elif action == "ECONOMIC_EXPORT":
        candidates = (
            budget.get("export_budget_w"),
            selected.get("max_power_w"),
            item.get("max_power_w"),
        )
    else:
        return 0.0
    execution_window_id = str(execution.get("window_id") or "")
    execution_plan_window_id = str(
        execution.get("plan_window_id")
        or execution_window_id
        or ""
    )
    plan_window_start_ms = _safe_int(execution.get("plan_window_start_ts"))
    plan_window_end_ms = _safe_int(execution.get("plan_window_end_ts"))
    source_action = str(item.get("source_action") or "").strip().lower()
    plan_windows = []
    for window in direct.get("windows") or []:
        if not isinstance(window, dict):
            continue
        plan_window_id = str(
            window.get("export_plateau_id")
            or window.get("market_window_id")
            or window.get("window_id")
            or ""
        )
        if (
            str(window.get("action") or "").strip().lower() == source_action
            and plan_window_id == execution_plan_window_id
            and _safe_int(window.get("start_ts")) == plan_window_start_ms
            and _safe_int(window.get("end_ts")) == plan_window_end_ms
        ):
            plan_windows.append(window)
    if len(plan_windows) != 1:
        return 0.0
    plan_window_limit = plan_windows[0].get("max_power_w")
    candidates = (*candidates, plan_window_limit)
    positive = []
    for candidate in candidates:
        value = _safe_float(candidate, None)
        if value is not None and value > 0.0:
            positive.append(value)
    return min(positive) if positive else 0.0


def _direct_action_source_revision(direct: Dict[str, Any]) -> str:
    def material(item: Dict[str, Any]) -> Dict[str, Any]:
        selected = (
            item.get("selected_window")
            if isinstance(item.get("selected_window"), dict)
            else {}
        )
        budget = (
            item.get("storage_budget")
            if isinstance(item.get("storage_budget"), dict)
            else {}
        )
        return {
            "start_ts": _safe_int(item.get("start_ts")),
            "end_ts": _safe_int(item.get("end_ts")),
            "target": str(item.get("dv_target_state") or ""),
            "commands_allowed": item.get("commands_allowed") is True,
            "blocked": item.get("blocked") is True,
            "source_action": _source_action(item),
            "charge_budget_w": _round(budget.get("charge_budget_w")),
            "export_budget_w": _round(budget.get("export_budget_w")),
            "selected_action": str(selected.get("action") or ""),
            "selected_max_power_w": _round(selected.get("max_power_w")),
            "window_id": (
                item.get("window_id")
                or item.get("action_id")
                or selected.get("window_id")
                or selected.get("action_id")
            ),
        }

    future_reservation = (
        direct.get("future_pv_store_reservation")
        if isinstance(direct.get("future_pv_store_reservation"), dict)
        else {}
    )
    next_window = (
        future_reservation.get("next_window")
        if isinstance(future_reservation.get("next_window"), dict)
        else {}
    )
    return _revision({
        "policy_decision": (
            material(direct["policy_decision"])
            if isinstance(direct.get("policy_decision"), dict)
            else None
        ),
        "policy_timeline": [
            material(item)
            for item in (direct.get("policy_timeline") or [])
            if isinstance(item, dict)
        ],
        "windows": [
            material(item)
            for item in (direct.get("windows") or [])
            if isinstance(item, dict)
        ],
        "future_pv_store_reservation": {
            "schema": str(future_reservation.get("schema") or ""),
            "active": future_reservation.get("active") is True,
            "commands_allowed": (
                future_reservation.get("commands_allowed") is True
            ),
            "reason": str(future_reservation.get("reason") or ""),
            "data_quality": str(
                future_reservation.get("data_quality") or ""
            ),
            "valid_until_ts": _safe_int(
                future_reservation.get("valid_until_ts")
            ),
            "max_curve_charge_w": _round(
                future_reservation.get("max_curve_charge_w")
            ),
            "required_headroom_wh": _round(
                future_reservation.get("required_headroom_wh")
            ),
            "safe_future_pv_absorption_wh": _round(
                future_reservation.get("safe_future_pv_absorption_wh")
            ),
            "target_soc_pct": _round(
                future_reservation.get("target_soc_pct")
            ),
            "next_window": {
                "start_ts": _safe_int(next_window.get("start_ts")),
                "end_ts": _safe_int(next_window.get("end_ts")),
                "action": str(next_window.get("action") or ""),
                "slot_count": _safe_int(next_window.get("slot_count")),
            },
        },
    })


def _bound_future_pv_store_headroom_hold(
    direct: Dict[str, Any],
    planning_input: Dict[str, Any],
) -> Dict[str, Any]:
    """Bewertet einen künftigen PV-Speicher-Hold ohne Punktwert zu überhöhen."""

    inactive = {
        "active": False,
        "positive_precharge_bound": False,
        "evidence_status": "NOT_APPLICABLE",
        "reason_code": "NO_BOUND_FUTURE_PV_STORE_RESERVATION",
        "start_ts_ms": 0,
        "end_ts_ms": 0,
        "revision": None,
    }
    reservation = (
        direct.get("future_pv_store_reservation")
        if isinstance(direct.get("future_pv_store_reservation"), dict)
        else {}
    )
    next_window = (
        reservation.get("next_window")
        if isinstance(reservation.get("next_window"), dict)
        else {}
    )
    permissions = (
        planning_input.get("permissions")
        if isinstance(planning_input.get("permissions"), dict)
        else {}
    )
    if not (
        reservation.get("schema")
        == "direct_marketing_future_pv_store_reservation_v1"
        and reservation.get("active") is True
        and reservation.get("commands_allowed") is True
        and str(reservation.get("data_quality") or "") == "ok"
        and permissions.get("direct_marketing_enabled") is True
        and permissions.get("pv_store_enabled") is True
        and str(next_window.get("action") or "")
        == "eco_plus_store_pv_candidate"
    ):
        return inactive

    generated_ms = _safe_int(planning_input.get("generated_at_ts_ms"))
    window_start_ms = _safe_int(next_window.get("start_ts"))
    window_end_ms = _safe_int(next_window.get("end_ts"))
    valid_until_ms = _safe_int(reservation.get("valid_until_ts"))
    declared_slot_count = _safe_int(next_window.get("slot_count"))
    max_curve_charge_w = _safe_float(
        reservation.get("max_curve_charge_w"),
        None,
    )
    required_headroom_wh = _safe_float(
        reservation.get("required_headroom_wh"),
        None,
    )
    future_absorption_wh = _safe_float(
        reservation.get("safe_future_pv_absorption_wh"),
        None,
    )
    target_soc_pct = _safe_float(
        reservation.get("target_soc_pct"),
        None,
    )
    if not (
        generated_ms > 0
        and generated_ms < window_start_ms
        and window_end_ms > window_start_ms
        and valid_until_ms == window_start_ms
        and declared_slot_count > 0
        and max_curve_charge_w is not None
        and max_curve_charge_w >= 0.0
        and required_headroom_wh is not None
        and required_headroom_wh > 50.0
        and future_absorption_wh is not None
        and future_absorption_wh > 50.0
        and target_soc_pct is not None
        and 0.0 <= target_soc_pct <= 100.0
    ):
        return inactive

    future_slots = [
        item
        for item in (planning_input.get("slots") or [])
        if isinstance(item, dict)
        and window_start_ms <= _safe_int(item.get("start_ts_ms"))
        and _safe_int(item.get("end_ts_ms")) <= window_end_ms
    ]
    if not (
        len(future_slots) == declared_slot_count
        and future_slots
        and _safe_int(future_slots[0].get("start_ts_ms"))
        == window_start_ms
        and _safe_int(future_slots[-1].get("end_ts_ms"))
        == window_end_ms
        and all(
            _safe_int(right.get("start_ts_ms"))
            == _safe_int(left.get("end_ts_ms"))
            for left, right in zip(future_slots, future_slots[1:])
        )
        and all(
            item.get("topology_complete") is True
            and item.get("pv_forecast_fresh") is True
            and item.get("load_forecast_valid") is True
            for item in future_slots
        )
    ):
        return inactive

    max_charge_w = max(
        0.0,
        _safe_float(
            (
                planning_input.get("hardware_limits")
                if isinstance(planning_input.get("hardware_limits"), dict)
                else {}
            ).get("max_charge_w"),
            0.0,
        )
        or 0.0,
    )
    if max_curve_charge_w > max_charge_w:
        return inactive
    independent_dc_source_wh = 0.0
    for item in future_slots:
        dc_w = max(
            0.0,
            _safe_float(item.get("e3dc_dc_pv_w"), 0.0) or 0.0,
        )
        load_w = max(
            0.0,
            _safe_float(item.get("load_w"), 0.0) or 0.0,
        )
        # Die künftige DC-Aufnahme wird bewusst ohne Gutschrift des externen
        # AC-Wechselrichters belegt. Nur der E3/DC-DC-Überschuss nach dem
        # vollständigen Hausverbrauch darf den DC-Headroom-Hold bestätigen.
        independent_dc_source_wh += min(
            max_charge_w,
            max(0.0, dc_w - load_w),
        ) * (SLOT_DURATION_S / 3600.0)
    if independent_dc_source_wh <= 50.0:
        return inactive

    material = {
        "schema": reservation.get("schema"),
        "reason": str(reservation.get("reason") or ""),
        "valid_until_ts": valid_until_ms,
        "max_curve_charge_w": _round(max_curve_charge_w),
        "required_headroom_wh": _round(required_headroom_wh),
        "safe_future_pv_absorption_wh": _round(future_absorption_wh),
        "independent_dc_source_wh": _round(independent_dc_source_wh),
        "target_soc_pct": _round(target_soc_pct),
        "next_window": {
            "start_ts": window_start_ms,
            "end_ts": window_end_ms,
            "action": next_window.get("action"),
            "slot_count": declared_slot_count,
        },
    }
    # Der Quellensplit enthält derzeit nur eine deterministische
    # Punktprognose. Auch ein vollständiger, frischer und topologisch
    # gebundener Punktverlauf beweist weder eine
    # konservative DC-Untergrenze noch die Lastobergrenze. Deshalb bleibt
    # der Zukunftskandidat sichtbar, autorisiert aber keinen eigenen 0-W-Hold.
    return {
        "active": False,
        "positive_precharge_bound": max_curve_charge_w > 0.0,
        "legacy_candidate_active": max_curve_charge_w == 0.0,
        "evidence_status": "EVIDENCE_LIMIT",
        "reason_code": (
            "POINT_FORECAST_WITHOUT_QUANTILE_NOT_SUFFICIENT"
        ),
        "independent_dc_point_forecast_wh": _round(
            independent_dc_source_wh
        ),
        "start_ts_ms": _safe_int(
            planning_input.get("valid_from_ts_ms")
        ),
        "end_ts_ms": window_start_ms,
        "revision": _revision(material),
    }


def _execution_contract(action: str, requested_w: float, max_discharge_w: float) -> Dict[str, Any]:
    if action == "DV_CURVE_CHARGE":
        action_contract = storage_action_contract(action) or {}
        return {
            "class": "PASSIVE_RELEASE",
            "effect": action_contract.get("effect", "AUTO_CHARGE_CAP"),
            "mode": "AUTO",
            "requested_power_w": 0.0,
            "max_charge_w": None,
            "max_discharge_w": _round(max_discharge_w),
            "release_existing_dv_limits": True,
            "would_require_runtime_command": False,
            "runtime_command_condition": None,
            "steady_state_command_required": False,
            "commands_allowed": False,
        }
    if action == "HOUSE_SUPPLY":
        action_contract = storage_action_contract(action) or {}
        return {
            "class": "PASSIVE_RELEASE",
            "effect": action_contract.get("effect"),
            "mode": "AUTO",
            "requested_power_w": 0.0,
            "max_charge_w": 0.0,
            "max_discharge_w": _round(max_discharge_w),
            "release_existing_dv_limits": False,
            "would_require_runtime_command": True,
            "runtime_command_condition": "ACTIVE_DV_POWER_SETTINGS",
            "steady_state_command_required": False,
            "commands_allowed": False,
        }
    if action == "CHARGE_BLOCK_WAIT":
        return {
            "class": "CHARGE_CAP",
            "mode": "AUTO",
            "requested_power_w": 0.0,
            "max_charge_w": 0.0,
            "max_discharge_w": _round(max_discharge_w),
            "release_existing_dv_limits": False,
            "would_require_runtime_command": True,
            "commands_allowed": False,
        }
    if action == "PV_STORE":
        return {
            "class": "CHARGE_CAP",
            "mode": "AUTO",
            "requested_power_w": _round(requested_w),
            "max_charge_w": _round(requested_w),
            "max_discharge_w": _round(max_discharge_w),
            "release_existing_dv_limits": False,
            "would_require_runtime_command": True,
            "commands_allowed": False,
        }
    if action == "GRID_CHARGE":
        return {
            "class": "ACTIVE_CHARGE",
            "mode": "CHARGE",
            "requested_power_w": _round(requested_w),
            "max_charge_w": _round(requested_w),
            "max_discharge_w": 0.0,
            "release_existing_dv_limits": False,
            "would_require_runtime_command": True,
            "commands_allowed": False,
        }
    return {
        "class": "ACTIVE_DISCHARGE",
        "mode": "DISCHARGE",
        "requested_power_w": _round(requested_w),
        "max_charge_w": 0.0,
        "max_discharge_w": _round(requested_w),
        "release_existing_dv_limits": False,
        "would_require_runtime_command": True,
        "commands_allowed": False,
    }


def _dv_curve_execution_contract_valid(
    execution: Dict[str, Any],
    max_discharge_w: float,
) -> bool:
    """Versiegelt die passive AUTO-Sidecar-Semantik der Kurvenladung."""

    return bool(
        isinstance(execution, dict)
        and execution.get("class") == "PASSIVE_RELEASE"
        and execution.get("effect") == "AUTO_CHARGE_CAP"
        and execution.get("mode") == "AUTO"
        and execution.get("commands_allowed") is False
        and execution.get("max_charge_w") is None
        and _safe_float(execution.get("max_discharge_w"), None)
        == max_discharge_w
        and _safe_float(execution.get("requested_power_w"), None) == 0.0
        and execution.get("release_existing_dv_limits") is True
        and execution.get("would_require_runtime_command") is False
        and execution.get("runtime_command_condition") is None
        and execution.get("steady_state_command_required") is False
    )


def build_dv_plan_v1(
    source: Dict[str, Any],
    planning_input: Dict[str, Any],
) -> Dict[str, Any]:
    """Materialisiert jeden Eingabeslot als genau eine explizite DV-Aktion."""

    direct = _direct_contract(source)
    limits = planning_input.get("hardware_limits") if isinstance(planning_input.get("hardware_limits"), dict) else {}
    max_discharge_w = _safe_float(limits.get("max_discharge_w"), 0.0) or 0.0
    candidates = []
    for input_slot in planning_input.get("slots") or []:
        start_ms = _safe_int(input_slot.get("start_ts_ms"))
        end_ms = _safe_int(input_slot.get("end_ts_ms"))
        source_item, action, purpose, reason = _candidate_for_slot(
            direct,
            start_ms,
            end_ms,
        )
        candidates.append(
            (input_slot, source_item, action, purpose, reason)
        )
    future_headroom_hold = _bound_future_pv_store_headroom_hold(
        direct,
        planning_input,
    )

    plan_slots = []
    for candidate in candidates:
        input_slot, source_item, action, purpose, reason = candidate
        start_ms = _safe_int(input_slot.get("start_ts_ms"))
        end_ms = _safe_int(input_slot.get("end_ts_ms"))
        reservation_applies = bool(
            future_headroom_hold.get("active") is True
            and _safe_int(future_headroom_hold.get("start_ts_ms"))
            <= start_ms
            and end_ms
            <= _safe_int(future_headroom_hold.get("end_ts_ms"))
        )
        active_policy_runtime_candidate = bool(
            action == "HOUSE_SUPPLY"
            and _eco_plus_normal_zero_charge_runtime_candidate(
                direct,
                source_item,
            )
        )
        requested_w = _requested_power(direct, source_item, action)
        storage_budget = (
            source_item.get("storage_budget")
            if isinstance(source_item.get("storage_budget"), dict)
            else {}
        )
        raw_window_id = (
            source_item.get("window_id")
            or source_item.get("action_id")
            or (
                source_item.get("selected_window", {}).get("window_id")
                if isinstance(source_item.get("selected_window"), dict)
                else None
            )
        )
        plan_slots.append({
            "input_slot_id": input_slot.get("input_slot_id"),
            "start_ts_ms": start_ms,
            "end_ts_ms": end_ms,
            "applies": True,
            "action": action,
            "purpose": purpose,
            "reason_code": reason,
            "active_policy_runtime_candidate": (
                active_policy_runtime_candidate
            ),
            "active_policy_runtime_evidence_status": (
                "COMPLETE"
                if active_policy_runtime_candidate
                else "NOT_APPLICABLE"
            ),
            "active_policy_runtime_reason_code": (
                "ACTIVE_DV_AUTO_CHARGE_CAP_BOUND"
                if active_policy_runtime_candidate
                else None
            ),
            "active_policy_runtime_expected_max_charge_w": (
                0.0 if active_policy_runtime_candidate else None
            ),
            "forecast_recommendation_applies": reservation_applies,
            "forecast_recommendation_evidence_status": (
                future_headroom_hold.get("evidence_status")
                if active_policy_runtime_candidate
                else None
            ),
            "forecast_recommendation_reason_code": (
                future_headroom_hold.get("reason_code")
                if active_policy_runtime_candidate
                else None
            ),
            "mapping_blocker_code": source_item.get("mapping_blocker_code"),
            "source_action": (
                _source_action(source_item)
                if _source_action(source_item) in KNOWN_SOURCE_ACTIONS
                else None
            ),
            "source_window_revision": (
                _revision(str(raw_window_id)) if raw_window_id else None
            ),
            "headroom_reservation_revision": (
                future_headroom_hold.get("revision")
                if reservation_applies
                else None
            ),
            "protected_reserve_wh": _round(
                storage_budget.get("protected_reserve_wh")
            ),
            "sellable_wh": _round(storage_budget.get("sellable_wh")),
            "charge_source_contract": (
                "E3DC_DC_ONLY"
                if action == "PV_STORE"
                else ("GRID_EXPLICIT" if action == "GRID_CHARGE" else None)
            ),
            "execution": _execution_contract(action, requested_w, max_discharge_w),
        })

    structural_codes = []
    previous_end = None
    for slot in plan_slots:
        start_ms = _safe_int(slot.get("start_ts_ms"))
        end_ms = _safe_int(slot.get("end_ts_ms"))
        if end_ms - start_ms != SLOT_DURATION_MS:
            structural_codes.append("DV_SLOT_DURATION_INVALID")
        if previous_end is not None and start_ms != previous_end:
            structural_codes.append(
                "DV_SLOT_GAP" if start_ms > previous_end else "DV_SLOT_OVERLAP"
            )
        if slot.get("action") not in ALLOWED_ACTIONS:
            structural_codes.append("DV_ACTION_MISSING_OR_UNKNOWN")
        previous_end = end_ms

    plan = {
        "schema_version": DV_PLAN_SCHEMA,
        "algorithm": PLANNER_VERSION,
        "planning_input_id": planning_input.get("input_id"),
        "migration_action_source_revision": _direct_action_source_revision(direct),
        "generated_at_ts_ms": planning_input.get("generated_at_ts_ms"),
        "valid_from_ts_ms": planning_input.get("valid_from_ts_ms"),
        "horizon_end_ts_ms": planning_input.get("horizon_end_ts_ms"),
        "slot_duration_s": SLOT_DURATION_S,
        "shadow_only": True,
        "commands_allowed": False,
        "owner_contract": {
            "planner_has_hardware_effect": False,
            "hardware_executor": "storage_manager",
            "rscp_output_count": 1,
        },
        "complete": bool(plan_slots) and not structural_codes,
        "blockers": sorted(set(structural_codes)),
        "future_headroom_hold_evidence": copy.deepcopy(
            future_headroom_hold
        ),
        "slots": plan_slots,
    }
    plan_material = copy.deepcopy(plan)
    plan["plan_id"] = _revision(plan_material)
    for slot in plan["slots"]:
        slot["slot_id"] = _revision({
            "plan_id": plan["plan_id"],
            "input_slot_id": slot.get("input_slot_id"),
            "start_ts_ms": slot.get("start_ts_ms"),
            "end_ts_ms": slot.get("end_ts_ms"),
        })
    return plan


def _append_once(target: List[str], code: str) -> None:
    if code not in target:
        target.append(code)


def _project_passive_power(
    input_slot: Dict[str, Any],
    max_charge_w: float,
    max_discharge_w: float,
) -> Dict[str, Any]:
    """Projiziert AUTO ohne externes AC-PV als sichere Ladequelle zu erfinden."""

    if (
        input_slot.get("pv_forecast_fresh") is not True
        or input_slot.get("load_forecast_valid") is not True
    ):
        return {
            "battery_w": 0.0,
            "source_budget_w": 0.0,
            "source_contract": "PASSIVE_FORECAST_EVIDENCE_INCOMPLETE",
            "tighten_code": None,
        }
    pv_w = _safe_float(input_slot.get("pv_total_w"), None)
    load_w = _safe_float(input_slot.get("load_w"), None)
    if pv_w is None or load_w is None:
        return {
            "battery_w": 0.0,
            "source_budget_w": 0.0,
            "source_contract": "PASSIVE_FORECAST_POWER_MISSING",
            "tighten_code": None,
        }
    net_surplus_w = float(pv_w) - float(load_w)
    if net_surplus_w <= 0.0:
        return {
            "battery_w": max(-max_discharge_w, net_surplus_w),
            "source_budget_w": max(0.0, -net_surplus_w),
            "source_contract": "PASSIVE_TOTAL_PCC_DEFICIT",
            "tighten_code": None,
        }
    if input_slot.get("topology_complete") is not True:
        return {
            "battery_w": 0.0,
            "source_budget_w": 0.0,
            "source_contract": "PASSIVE_CHARGE_TOPOLOGY_UNPROVEN",
            "tighten_code": "DV_PASSIVE_CHARGE_TOPOLOGY_UNPROVEN",
        }
    dc_pv_w = _safe_float(input_slot.get("e3dc_dc_pv_w"), None)
    external_ac_w = _safe_float(input_slot.get("external_ac_pv_w"), None)
    if dc_pv_w is None or external_ac_w is None:
        return {
            "battery_w": 0.0,
            "source_budget_w": 0.0,
            "source_contract": "PASSIVE_CHARGE_TOPOLOGY_POWER_MISSING",
            "tighten_code": "DV_PASSIVE_CHARGE_TOPOLOGY_UNPROVEN",
        }
    residual_load_after_external_ac_w = max(
        0.0,
        float(load_w) - max(0.0, float(external_ac_w)),
    )
    dc_surplus_w = max(
        0.0,
        max(0.0, float(dc_pv_w)) - residual_load_after_external_ac_w,
    )
    battery_w = min(max_charge_w, dc_surplus_w)
    return {
        "battery_w": battery_w,
        "source_budget_w": dc_surplus_w,
        "source_contract": "PASSIVE_E3DC_DC_AFTER_EXTERNAL_AC_LOAD_OFFSET",
        "tighten_code": (
            "DV_TIGHTEN_PASSIVE_DC_SOURCE_BUDGET"
            if battery_w + 0.001 < min(max_charge_w, net_surplus_w)
            else None
        ),
    }


def _append_forecast_evidence_rejects(
    input_slot: Dict[str, Any],
    slot_rejects: List[str],
) -> None:
    if (
        input_slot.get("pv_forecast_fresh") is not True
        or _safe_float(input_slot.get("pv_total_w"), None) is None
    ):
        _append_once(slot_rejects, "DV_PV_FORECAST_MISSING_OR_STALE")
    if (
        input_slot.get("load_forecast_valid") is not True
        or _safe_float(input_slot.get("load_w"), None) is None
    ):
        _append_once(slot_rejects, "DV_LOAD_FORECAST_INVALID")


def validate_dv_plan_v1(
    planning_input: Dict[str, Any],
    plan: Dict[str, Any],
) -> Dict[str, Any]:
    """Prüft Physik und Quellenbindung, ohne den Originalplan zu verändern."""

    reject_codes: List[str] = []
    tighten_codes: List[str] = []
    input_slots = [
        item for item in (planning_input.get("slots") or []) if isinstance(item, dict)
    ]
    plan_slots = [item for item in (plan.get("slots") or []) if isinstance(item, dict)]
    if planning_input.get("schema_version") != PLANNING_INPUT_SCHEMA:
        _append_once(reject_codes, "DV_PLAN_REVISION_INCOMPLETE")
    input_material = copy.deepcopy(planning_input)
    input_id = input_material.pop("input_id", None)
    if input_id != _revision(input_material):
        _append_once(reject_codes, "DV_PLANNING_INPUT_HASH_MISMATCH")
    if plan.get("schema_version") != DV_PLAN_SCHEMA:
        _append_once(reject_codes, "DV_PLAN_SCHEMA_INVALID")
    plan_material = copy.deepcopy(plan)
    plan_id = plan_material.pop("plan_id", None)
    for material_slot in plan_material.get("slots") or []:
        if isinstance(material_slot, dict):
            material_slot.pop("slot_id", None)
    if plan_id != _revision(plan_material):
        _append_once(reject_codes, "DV_PLAN_HASH_MISMATCH")
    if plan.get("planning_input_id") != planning_input.get("input_id"):
        _append_once(reject_codes, "DV_PLAN_REVISION_INCOMPLETE")
    if (
        _safe_int(plan.get("valid_from_ts_ms"))
        != _safe_int(planning_input.get("valid_from_ts_ms"))
        or _safe_int(plan.get("horizon_end_ts_ms"))
        != _safe_int(planning_input.get("horizon_end_ts_ms"))
    ):
        _append_once(reject_codes, "DV_HORIZON_BINDING_MISMATCH")
    if not plan.get("complete"):
        _append_once(reject_codes, "DV_HORIZON_INCOMPLETE")
    if len(input_slots) != len(plan_slots) or not plan_slots:
        _append_once(reject_codes, "DV_HORIZON_INCOMPLETE")
    if input_slots:
        if (
            _safe_int(planning_input.get("valid_from_ts_ms"))
            != _safe_int(input_slots[0].get("start_ts_ms"))
            or _safe_int(planning_input.get("horizon_end_ts_ms"))
            != _safe_int(input_slots[-1].get("end_ts_ms"))
        ):
            _append_once(reject_codes, "DV_HORIZON_BINDING_MISMATCH")
    if plan_slots:
        if (
            _safe_int(plan.get("valid_from_ts_ms"))
            != _safe_int(plan_slots[0].get("start_ts_ms"))
            or _safe_int(plan.get("horizon_end_ts_ms"))
            != _safe_int(plan_slots[-1].get("end_ts_ms"))
        ):
            _append_once(reject_codes, "DV_HORIZON_BINDING_MISMATCH")

    storage = planning_input.get("storage") if isinstance(planning_input.get("storage"), dict) else {}
    limits = planning_input.get("hardware_limits") if isinstance(planning_input.get("hardware_limits"), dict) else {}
    permissions = planning_input.get("permissions") if isinstance(planning_input.get("permissions"), dict) else {}
    efficiency = planning_input.get("efficiency") if isinstance(planning_input.get("efficiency"), dict) else {}
    charge_adequacy = (
        planning_input.get("forecast_charge_adequacy")
        if isinstance(planning_input.get("forecast_charge_adequacy"), dict)
        else {}
    )
    protected_demand_reserve = (
        planning_input.get("protected_demand_reserve")
        if isinstance(
            planning_input.get("protected_demand_reserve"),
            dict,
        )
        else {}
    )
    capacity_wh = _safe_float(storage.get("capacity_wh"), 0.0) or 0.0
    soc = _safe_float(storage.get("initial_soc_pct"), None)
    hard_floor = _safe_float(storage.get("hard_reserve_soc_pct"), None)
    ceiling_raw = _safe_float(storage.get("ceiling_soc_pct"), None)
    ceiling = ceiling_raw if ceiling_raw is not None else 100.0
    parsed_limits = {
        key: _safe_float(limits.get(key), None)
        for key in (
            "max_charge_w",
            "max_discharge_w",
            "max_grid_import_w",
            "max_grid_export_w",
            "max_grid_charge_w",
            "max_economic_export_w",
        )
    }
    max_charge_w = parsed_limits["max_charge_w"] or 0.0
    max_discharge_w = parsed_limits["max_discharge_w"] or 0.0
    max_grid_import_w = parsed_limits["max_grid_import_w"] or 0.0
    max_grid_export_w = parsed_limits["max_grid_export_w"] or 0.0
    max_grid_charge_w = parsed_limits["max_grid_charge_w"] or 0.0
    max_economic_export_w = parsed_limits["max_economic_export_w"] or 0.0
    charge_eff_raw = _safe_float(efficiency.get("charge"), None)
    discharge_eff_raw = _safe_float(efficiency.get("discharge"), None)
    charge_eff = (
        charge_eff_raw
        if charge_eff_raw is not None and 0.0 < charge_eff_raw <= 1.0
        else 0.95
    )
    discharge_eff = (
        discharge_eff_raw
        if discharge_eff_raw is not None and 0.0 < discharge_eff_raw <= 1.0
        else 0.95
    )
    if capacity_wh <= 1000.0:
        _append_once(reject_codes, "DV_CAPACITY_INVALID")
    if soc is None or not bool(storage.get("state_fresh")):
        _append_once(reject_codes, "DV_INITIAL_SOC_MISSING_OR_STALE")
    elif not 0.0 <= soc <= 100.0:
        _append_once(reject_codes, "DV_INITIAL_SOC_OUT_OF_RANGE")
    if hard_floor is None:
        _append_once(reject_codes, "DV_HARD_RESERVE_MISSING")
    elif not 0.0 <= hard_floor <= 100.0:
        _append_once(reject_codes, "DV_HARD_RESERVE_INVALID")
    if (
        ceiling_raw is None
        or not 0.0 <= ceiling <= 100.0
        or (hard_floor is not None and ceiling < hard_floor)
    ):
        _append_once(reject_codes, "DV_SOC_BOUNDS_INVALID")
    if (
        charge_eff_raw is None
        or not 0.0 < charge_eff_raw <= 1.0
        or discharge_eff_raw is None
        or not 0.0 < discharge_eff_raw <= 1.0
    ):
        _append_once(reject_codes, "DV_EFFICIENCY_INVALID")
    if any(value is None or value < 0.0 for value in parsed_limits.values()):
        _append_once(reject_codes, "DV_HARDWARE_LIMIT_INVALID")
    if soc is not None and hard_floor is not None and soc < hard_floor:
        _append_once(reject_codes, "DV_INITIAL_SOC_BELOW_HARD_RESERVE")
    protected_shortfall_wh = _safe_float(
        protected_demand_reserve.get("shortfall_wh"),
        None,
    )
    protected_required_wh = _safe_float(
        protected_demand_reserve.get("required_stored_wh"),
        None,
    )
    protected_floor_pct = (
        protected_required_wh / capacity_wh * 100.0
        if protected_required_wh is not None
        and protected_required_wh >= 0.0
        and capacity_wh > 0.0
        else None
    )
    if (
        protected_demand_reserve.get("schema_version")
        != "protected_demand_reserve_v1"
    ):
        _append_once(
            reject_codes,
            "DV_PROTECTED_DEMAND_RESERVE_SCHEMA_INVALID",
        )
    elif (
        protected_shortfall_wh is not None
        and protected_shortfall_wh > 50.0
        and protected_demand_reserve.get("status")
        == "EVIDENCE_LIMIT"
    ):
        _append_once(
            reject_codes,
            "DV_PROTECTED_DEMAND_RESERVE_EVIDENCE_LIMIT",
        )

    forecast_deficit_wh = _safe_float(
        charge_adequacy.get("forecast_charge_deficit_wh"),
        None,
    )
    remaining_grid_charge_deficit_wh = (
        max(0.0, forecast_deficit_wh)
        if charge_adequacy.get("status") == "COMPLETE"
        and forecast_deficit_wh is not None
        else None
    )
    global_fail_closed_codes = list(reject_codes)
    remaining_sellable_wh_by_window: Dict[str, float] = {}

    validation_slots = []
    previous_end = None
    for index, plan_slot in enumerate(plan_slots):
        input_slot = input_slots[index] if index < len(input_slots) else {}
        slot_rejects: List[str] = []
        slot_tightens: List[str] = []
        action = str(plan_slot.get("action") or "")
        start_ms = _safe_int(plan_slot.get("start_ts_ms"))
        end_ms = _safe_int(plan_slot.get("end_ts_ms"))
        if action not in ALLOWED_ACTIONS:
            _append_once(slot_rejects, "DV_ACTION_MISSING_OR_UNKNOWN")
        if end_ms - start_ms != SLOT_DURATION_MS:
            _append_once(slot_rejects, "DV_SLOT_DURATION_INVALID")
        if start_ms % SLOT_DURATION_MS != 0:
            _append_once(slot_rejects, "DV_SLOT_ALIGNMENT_INVALID")
        if previous_end is not None and start_ms != previous_end:
            _append_once(
                slot_rejects,
                "DV_SLOT_GAP" if start_ms > previous_end else "DV_SLOT_OVERLAP",
            )
        if plan_slot.get("input_slot_id") != input_slot.get("input_slot_id"):
            _append_once(slot_rejects, "DV_PLAN_REVISION_INCOMPLETE")
        if (
            start_ms != _safe_int(input_slot.get("start_ts_ms"))
            or end_ms != _safe_int(input_slot.get("end_ts_ms"))
        ):
            _append_once(slot_rejects, "DV_SLOT_INPUT_INTERVAL_MISMATCH")
        expected_slot_id = _revision({
            "plan_id": plan.get("plan_id"),
            "input_slot_id": plan_slot.get("input_slot_id"),
            "start_ts_ms": start_ms,
            "end_ts_ms": end_ms,
        })
        if plan_slot.get("slot_id") != expected_slot_id:
            _append_once(slot_rejects, "DV_PLAN_HASH_MISMATCH")
        if plan_slot.get("mapping_blocker_code"):
            _append_once(
                slot_rejects,
                str(plan_slot.get("mapping_blocker_code")),
            )
        previous_end = end_ms

        execution = plan_slot.get("execution") if isinstance(plan_slot.get("execution"), dict) else {}
        requested_w = max(0.0, _safe_float(execution.get("requested_power_w"), 0.0) or 0.0)
        effective_charge_cap_w = 0.0
        effective_discharge_w = 0.0
        projected_battery_w = 0.0
        source_budget_w = 0.0
        sellable_window_key: Optional[str] = None
        passive_power_source_contract: Optional[str] = None
        if action == "HOUSE_SUPPLY":
            if (
                execution.get("steady_state_command_required") is True
                or (
                    execution.get("would_require_runtime_command") is True
                    and execution.get("runtime_command_condition")
                    != "ACTIVE_DV_POWER_SETTINGS"
                )
            ):
                _append_once(slot_rejects, "DV_HOUSE_SUPPLY_COMMAND_FORBIDDEN")
            if not (
                execution.get("class") == "PASSIVE_RELEASE"
                and execution.get("effect") == "AUTO_CHARGE_CAP"
                and execution.get("mode") == "AUTO"
                and execution.get("release_existing_dv_limits") is False
                and execution.get("commands_allowed") is False
                and _safe_float(execution.get("max_charge_w"), None) == 0.0
                and _safe_float(execution.get("max_discharge_w"), None)
                == max_discharge_w
                and _safe_float(
                    execution.get("requested_power_w"),
                    None,
                )
                == 0.0
            ):
                _append_once(slot_rejects, "DV_HOUSE_SUPPLY_SEMANTICS_INVALID")
            passive_projection = _project_passive_power(
                input_slot,
                max_charge_w,
                max_discharge_w,
            )
            projected_battery_w = float(
                passive_projection.get("battery_w") or 0.0
            )
            source_budget_w = float(
                passive_projection.get("source_budget_w") or 0.0
            )
            passive_power_source_contract = str(
                passive_projection.get("source_contract") or ""
            ) or None
            if passive_projection.get("tighten_code"):
                _append_once(
                    slot_tightens,
                    str(passive_projection["tighten_code"]),
                )
        elif action == "DV_CURVE_CHARGE":
            if not _dv_curve_execution_contract_valid(
                execution,
                max_discharge_w,
            ):
                _append_once(slot_rejects, "DV_CURVE_CHARGE_SEMANTICS_INVALID")
            passive_projection = _project_passive_power(
                input_slot,
                max_charge_w,
                max_discharge_w,
            )
            projected_battery_w = float(
                passive_projection.get("battery_w") or 0.0
            )
            source_budget_w = float(
                passive_projection.get("source_budget_w") or 0.0
            )
            passive_power_source_contract = str(
                passive_projection.get("source_contract") or ""
            ) or None
            if passive_projection.get("tighten_code"):
                _append_once(
                    slot_tightens,
                    str(passive_projection["tighten_code"]),
                )
        elif action == "CHARGE_BLOCK_WAIT":
            if not bool(permissions.get("direct_marketing_enabled")):
                _append_once(slot_rejects, "DV_DIRECT_MARKETING_NOT_ENABLED")
            if not bool(permissions.get("pv_store_enabled")):
                _append_once(
                    slot_rejects,
                    "DV_PV_STORE_NOT_USER_RELEASED",
                )
            if input_slot.get("price_fresh") is not True:
                _append_once(
                    slot_rejects,
                    "DV_PRICE_MISSING_OR_STALE",
                )
            if input_slot.get("topology_complete") is not True:
                _append_once(slot_rejects, "DV_TOPOLOGY_UNBOUND")
            _append_forecast_evidence_rejects(
                input_slot,
                slot_rejects,
            )
            if _safe_float(execution.get("max_charge_w"), None) != 0.0:
                _append_once(slot_rejects, "DV_CHARGE_BLOCK_SEMANTICS_INVALID")
            passive_projection = _project_passive_power(
                input_slot,
                0.0,
                max_discharge_w,
            )
            projected_battery_w = min(
                0.0,
                float(passive_projection.get("battery_w") or 0.0),
            )
            source_budget_w = float(
                passive_projection.get("source_budget_w") or 0.0
            )
            passive_power_source_contract = str(
                passive_projection.get("source_contract") or ""
            ) or None
        elif action == "PV_STORE":
            if not bool(permissions.get("direct_marketing_enabled")):
                _append_once(slot_rejects, "DV_DIRECT_MARKETING_NOT_ENABLED")
            if not bool(permissions.get("pv_store_enabled")):
                _append_once(slot_rejects, "DV_PV_STORE_NOT_USER_RELEASED")
            if requested_w <= 0.0:
                _append_once(slot_rejects, "DV_CHARGE_SOURCE_CONTRACT_MISSING")
            if max_charge_w <= 0.0:
                _append_once(
                    slot_rejects,
                    "DV_HARDWARE_CHARGE_LIMIT_MISSING",
                )
            if plan_slot.get("charge_source_contract") != "E3DC_DC_ONLY":
                _append_once(slot_rejects, "DV_CHARGE_SOURCE_CONTRACT_MISSING")
            if input_slot.get("price_fresh") is not True:
                _append_once(slot_rejects, "DV_PRICE_MISSING_OR_STALE")
            if input_slot.get("topology_complete") is not True:
                _append_once(slot_rejects, "DV_TOPOLOGY_UNBOUND")
            _append_forecast_evidence_rejects(input_slot, slot_rejects)
            dc_pv_w = max(0.0, _safe_float(input_slot.get("e3dc_dc_pv_w"), 0.0) or 0.0)
            source_budget_w = dc_pv_w
            if source_budget_w <= 0.0:
                _append_once(slot_rejects, "DV_DC_SOURCE_UNAVAILABLE")
            effective_charge_cap_w = min(requested_w, max_charge_w, source_budget_w)
            if effective_charge_cap_w < requested_w:
                _append_once(slot_tightens, "DV_TIGHTEN_DC_SOURCE_BUDGET")
            surplus_w = max(
                0.0,
                (_safe_float(input_slot.get("pv_total_w"), 0.0) or 0.0)
                - (_safe_float(input_slot.get("load_w"), 0.0) or 0.0),
            )
            projected_battery_w = min(effective_charge_cap_w, surplus_w)
        elif action == "GRID_CHARGE":
            if not bool(permissions.get("direct_marketing_enabled")):
                _append_once(slot_rejects, "DV_DIRECT_MARKETING_NOT_ENABLED")
            if requested_w <= 0.0:
                _append_once(slot_rejects, "DV_CHARGE_SOURCE_CONTRACT_MISSING")
            if max_charge_w <= 0.0:
                _append_once(
                    slot_rejects,
                    "DV_HARDWARE_CHARGE_LIMIT_MISSING",
                )
            if plan_slot.get("charge_source_contract") != "GRID_EXPLICIT":
                _append_once(slot_rejects, "DV_CHARGE_SOURCE_CONTRACT_MISSING")
            if not bool(permissions.get("grid_charge_enabled")):
                _append_once(slot_rejects, "DV_GRID_CHARGE_NOT_USER_RELEASED")
            if input_slot.get("price_fresh") is not True:
                _append_once(slot_rejects, "DV_PRICE_MISSING_OR_STALE")
            if input_slot.get("topology_complete") is not True:
                _append_once(slot_rejects, "DV_TOPOLOGY_UNBOUND")
            _append_forecast_evidence_rejects(input_slot, slot_rejects)
            if max_grid_import_w <= 0.0 or max_grid_charge_w <= 0.0:
                _append_once(slot_rejects, "DV_GRID_IMPORT_LIMIT_MISSING_OR_EXCEEDED")
            if (
                charge_adequacy.get("status") != "COMPLETE"
                or remaining_grid_charge_deficit_wh is None
                or remaining_grid_charge_deficit_wh <= 0.0
            ):
                _append_once(
                    slot_rejects,
                    "DV_GRID_CHARGE_FORECAST_DEFICIT_UNPROVEN",
                )
            pv_w = max(
                0.0,
                _safe_float(input_slot.get("pv_total_w"), 0.0) or 0.0,
            )
            load_w = max(
                0.0,
                _safe_float(input_slot.get("load_w"), 0.0) or 0.0,
            )
            residual_grid_load_w = max(0.0, load_w - pv_w)
            grid_import_headroom_w = max(
                0.0,
                max_grid_import_w - residual_grid_load_w,
            )
            deficit_power_w = (
                remaining_grid_charge_deficit_wh
                / (SLOT_DURATION_S / 3600.0)
                if remaining_grid_charge_deficit_wh is not None
                else 0.0
            )
            effective_charge_cap_w = min(
                requested_w,
                max_charge_w,
                max_grid_charge_w,
                grid_import_headroom_w,
                max(0.0, deficit_power_w),
            )
            if grid_import_headroom_w <= 0.0:
                _append_once(
                    slot_rejects,
                    "DV_GRID_IMPORT_HEADROOM_EXHAUSTED",
                )
            if effective_charge_cap_w < requested_w:
                _append_once(slot_tightens, "DV_TIGHTEN_GRID_IMPORT_HEADROOM")
            projected_battery_w = effective_charge_cap_w
        elif action == "ECONOMIC_EXPORT":
            if not bool(permissions.get("direct_marketing_enabled")):
                _append_once(slot_rejects, "DV_DIRECT_MARKETING_NOT_ENABLED")
            if requested_w <= 0.0:
                _append_once(slot_rejects, "DV_DIRECTION_ACTION_MISMATCH")
            if max_discharge_w <= 0.0:
                _append_once(
                    slot_rejects,
                    "DV_HARDWARE_DISCHARGE_LIMIT_MISSING",
                )
            if input_slot.get("price_fresh") is not True:
                _append_once(slot_rejects, "DV_PRICE_MISSING_OR_STALE")
            if not bool(permissions.get("economic_export_enabled")):
                _append_once(slot_rejects, "DV_EXPORT_NOT_USER_RELEASED")
            _append_forecast_evidence_rejects(input_slot, slot_rejects)
            protected_reserve_wh = _safe_float(
                plan_slot.get("protected_reserve_wh"),
                None,
            )
            sellable_wh = _safe_float(plan_slot.get("sellable_wh"), None)
            if protected_reserve_wh is None:
                _append_once(
                    slot_rejects,
                    "DV_PROTECTED_HOUSE_RESERVE_MISSING",
                )
            if sellable_wh is None or sellable_wh < 0.0:
                _append_once(
                    slot_rejects,
                    "DV_EXPORT_SELLABLE_BUDGET_MISSING",
                )
            if max_grid_export_w <= 0.0 or max_economic_export_w <= 0.0:
                _append_once(
                    slot_rejects,
                    "DV_GRID_EXPORT_LIMIT_MISSING_OR_EXCEEDED",
                )
            pv_w = max(
                0.0,
                _safe_float(input_slot.get("pv_total_w"), 0.0) or 0.0,
            )
            load_w = max(
                0.0,
                _safe_float(input_slot.get("load_w"), 0.0) or 0.0,
            )
            existing_pv_export_w = max(0.0, pv_w - load_w)
            export_headroom_w = max(
                0.0,
                max_grid_export_w - existing_pv_export_w,
            )
            sellable_window_key = str(
                plan_slot.get("source_window_revision")
                or plan_slot.get("slot_id")
                or ""
            )
            if sellable_wh is not None and sellable_wh >= 0.0:
                previous_sellable_wh = remaining_sellable_wh_by_window.get(
                    sellable_window_key,
                    sellable_wh,
                )
                remaining_sellable_wh_by_window[sellable_window_key] = min(
                    previous_sellable_wh,
                    sellable_wh,
                )
            remaining_sellable_wh = remaining_sellable_wh_by_window.get(
                sellable_window_key,
                0.0,
            )
            sellable_power_w = (
                remaining_sellable_wh
                * max(discharge_eff, 0.001)
                / (SLOT_DURATION_S / 3600.0)
            )
            effective_discharge_w = min(
                requested_w,
                max_discharge_w,
                max_economic_export_w,
                export_headroom_w,
                sellable_power_w,
            )
            source_budget_w = sellable_power_w
            if export_headroom_w <= 0.0:
                _append_once(
                    slot_rejects,
                    "DV_EXPORT_PCC_HEADROOM_EXHAUSTED",
                )
            if sellable_power_w <= 0.0:
                _append_once(
                    slot_rejects,
                    "DV_EXPORT_SELLABLE_BUDGET_EXHAUSTED",
                )
            if effective_discharge_w < requested_w:
                _append_once(slot_tightens, "DV_TIGHTEN_EXPORT_LIMIT")
            projected_battery_w = -effective_discharge_w

        soc_start = soc
        slot_floor = hard_floor
        if (
            protected_demand_reserve.get("status") == "SATISFIED_NOW"
            and protected_floor_pct is not None
        ):
            slot_floor = max(
                slot_floor if slot_floor is not None else 0.0,
                protected_floor_pct,
            )
        if (
            action == "ECONOMIC_EXPORT"
            and capacity_wh > 0.0
            and hard_floor is not None
        ):
            protected_reserve_wh = _safe_float(
                plan_slot.get("protected_reserve_wh"),
                None,
            )
            if protected_reserve_wh is not None:
                slot_floor = max(
                    slot_floor if slot_floor is not None else hard_floor,
                    hard_floor,
                    protected_reserve_wh / capacity_wh * 100.0,
                )
            if soc_start is not None and soc_start <= slot_floor:
                _append_once(
                    slot_rejects,
                    "DV_EXPORT_RESERVE_EXHAUSTED",
                )

        for code in global_fail_closed_codes:
            _append_once(slot_rejects, code)
        effective_action: Optional[str] = action
        if slot_rejects:
            # Ein abgelehnter Shadow-Slot bleibt absichtlich wirkungslos. Er
            # darf keine Energie für einen späteren Slot erzeugen oder abbauen.
            effective_action = None
            effective_charge_cap_w = 0.0
            effective_discharge_w = 0.0
            projected_battery_w = 0.0
            slot_tightens = []

        if (
            not slot_rejects
            and soc_start is not None
            and capacity_wh > 0.0
            and slot_floor is not None
        ):
            if projected_battery_w >= 0.0:
                room_wh = max(0.0, (ceiling - soc_start) / 100.0 * capacity_wh)
                max_soc_charge_w = room_wh / (SLOT_DURATION_S / 3600.0) / max(charge_eff, 0.001)
                if projected_battery_w > max_soc_charge_w:
                    projected_battery_w = max_soc_charge_w
                    effective_charge_cap_w = min(effective_charge_cap_w, max_soc_charge_w)
                    _append_once(slot_tightens, "DV_TIGHTEN_SOC_CEILING")
                soc_end = soc_start + (
                    projected_battery_w
                    * (SLOT_DURATION_S / 3600.0)
                    * charge_eff
                    / capacity_wh
                    * 100.0
                )
            else:
                available_wh = max(
                    0.0,
                    (soc_start - slot_floor) / 100.0 * capacity_wh,
                )
                max_soc_discharge_w = (
                    available_wh
                    * max(discharge_eff, 0.001)
                    / (SLOT_DURATION_S / 3600.0)
                )
                if abs(projected_battery_w) > max_soc_discharge_w:
                    projected_battery_w = -max_soc_discharge_w
                    effective_discharge_w = min(effective_discharge_w, max_soc_discharge_w)
                    _append_once(
                        slot_tightens,
                        (
                            "DV_TIGHTEN_PROTECTED_DEMAND_RESERVE"
                            if hard_floor is not None
                            and slot_floor > hard_floor + 0.001
                            else "DV_TIGHTEN_HARD_RESERVE"
                        ),
                    )
                soc_end = soc_start + (
                    projected_battery_w
                    * (SLOT_DURATION_S / 3600.0)
                    / max(discharge_eff, 0.001)
                    / capacity_wh
                    * 100.0
                )
            soc = min(ceiling, max(slot_floor, soc_end))
            if (
                action == "GRID_CHARGE"
                and remaining_grid_charge_deficit_wh is not None
            ):
                remaining_grid_charge_deficit_wh = max(
                    0.0,
                    remaining_grid_charge_deficit_wh
                    - projected_battery_w * (SLOT_DURATION_S / 3600.0),
                )
            if (
                action == "ECONOMIC_EXPORT"
                and sellable_window_key is not None
            ):
                remaining_sellable_wh_by_window[sellable_window_key] = max(
                    0.0,
                    remaining_sellable_wh_by_window.get(
                        sellable_window_key,
                        0.0,
                    )
                    - abs(projected_battery_w)
                    * (SLOT_DURATION_S / 3600.0)
                    / max(discharge_eff, 0.001),
                )
        else:
            soc = soc_start

        pv_w = _safe_float(input_slot.get("pv_total_w"), 0.0) or 0.0
        load_w = _safe_float(input_slot.get("load_w"), 0.0) or 0.0
        projected_grid_w = load_w + projected_battery_w - pv_w
        for code in slot_rejects:
            _append_once(reject_codes, code)
        for code in slot_tightens:
            _append_once(tighten_codes, code)
        validation_slots.append({
            "slot_id": plan_slot.get("slot_id"),
            "action": action,
            "effective_action": effective_action,
            "fallback_effect": "EFFECTLESS" if slot_rejects else None,
            "status": "REJECTED" if slot_rejects else ("VALID_TIGHTENED" if slot_tightens else "VALID"),
            "eligible": not slot_rejects,
            "requested_power_w": _round(requested_w),
            "effective_charge_cap_w": _round(effective_charge_cap_w),
            "effective_discharge_w": _round(effective_discharge_w),
            "projected_battery_w": _round(projected_battery_w),
            "projected_grid_w": _round(projected_grid_w),
            "source_budget_w": _round(source_budget_w),
            "passive_power_source_contract": passive_power_source_contract,
            "protected_reserve_floor_pct": _round(slot_floor),
            "soc_start_pct": _round(soc_start),
            "soc_end_pct": _round(soc),
            "reject_codes": slot_rejects,
            "tighten_codes": slot_tightens,
        })

    status = "REJECTED" if reject_codes else ("VALID_TIGHTENED" if tighten_codes else "VALID")
    validation = {
        "schema_version": VALIDATION_SCHEMA,
        "validator": VALIDATOR_VERSION,
        "planning_input_id": planning_input.get("input_id"),
        "plan_id": plan.get("plan_id"),
        "status": status,
        "shadow_only": True,
        "commands_allowed": False,
        "field_activation_ready": False,
        "reject_codes": reject_codes,
        "tighten_codes": tighten_codes,
        "protected_demand_reserve": copy.deepcopy(
            protected_demand_reserve
        ),
        "summary": {
            "slot_count": len(validation_slots),
            "valid": sum(1 for item in validation_slots if item["status"] == "VALID"),
            "tightened": sum(1 for item in validation_slots if item["status"] == "VALID_TIGHTENED"),
            "rejected": sum(1 for item in validation_slots if item["status"] == "REJECTED"),
        },
        "slots": validation_slots,
    }
    validation["validation_id"] = _revision(validation)
    return validation


def build_direct_marketing_dispatch_shadow(
    source: Dict[str, Any],
    canonical_plan: Dict[str, Any],
) -> Dict[str, Any]:
    """Erzeugt Eingang, lückenlosen Plan und Physikoverlay deterministisch."""

    planning_input = build_planning_input_v1(source, canonical_plan)
    plan = build_dv_plan_v1(source, planning_input)
    validation = validate_dv_plan_v1(planning_input, plan)
    return {
        "schema_version": SHADOW_SCHEMA,
        "algorithm": PLANNER_VERSION,
        "shadow_only": True,
        "commands_allowed": False,
        "runtime_owner": "storage_manager",
        "status": validation.get("status"),
        "planning_input_revision": planning_input.get("input_id"),
        "dv_plan_revision": plan.get("plan_id"),
        "physics_validation_revision": validation.get("validation_id"),
        "planning_input": planning_input,
        "dv_plan": plan,
        "physics_validation": validation,
    }


def summarize_direct_marketing_dispatch_shadow(
    shadow: Dict[str, Any],
    generated_at_ts_ms: int,
) -> Dict[str, Any]:
    """Verdichtet die Shadow-Evidenz für den hashgebundenen 1-Hz-Planpfad."""

    plan = shadow.get("dv_plan") if isinstance(shadow.get("dv_plan"), dict) else {}
    validation = (
        shadow.get("physics_validation")
        if isinstance(shadow.get("physics_validation"), dict)
        else {}
    )
    planning_input = (
        shadow.get("planning_input")
        if isinstance(shadow.get("planning_input"), dict)
        else {}
    )
    plan_slots = [
        item for item in (plan.get("slots") or []) if isinstance(item, dict)
    ]
    validation_by_id = {
        item.get("slot_id"): item
        for item in (validation.get("slots") or [])
        if isinstance(item, dict) and item.get("slot_id")
    }
    action_counts = {action: 0 for action in ALLOWED_ACTIONS}
    for slot in plan_slots:
        action = str(slot.get("action") or "")
        if action in action_counts:
            action_counts[action] += 1

    def compact_slot(slot: Dict[str, Any]) -> Dict[str, Any]:
        checked = validation_by_id.get(slot.get("slot_id"), {})
        return {
            "slot_id": slot.get("slot_id"),
            "start_ts_ms": slot.get("start_ts_ms"),
            "end_ts_ms": slot.get("end_ts_ms"),
            "action": slot.get("action"),
            "purpose": slot.get("purpose"),
            "reason_code": slot.get("reason_code"),
            "active_policy_runtime_candidate": slot.get(
                "active_policy_runtime_candidate"
            ),
            "active_policy_runtime_evidence_status": slot.get(
                "active_policy_runtime_evidence_status"
            ),
            "active_policy_runtime_reason_code": slot.get(
                "active_policy_runtime_reason_code"
            ),
            "active_policy_runtime_expected_max_charge_w": slot.get(
                "active_policy_runtime_expected_max_charge_w"
            ),
            "forecast_recommendation_applies": slot.get(
                "forecast_recommendation_applies"
            ),
            "forecast_recommendation_evidence_status": slot.get(
                "forecast_recommendation_evidence_status"
            ),
            "forecast_recommendation_reason_code": slot.get(
                "forecast_recommendation_reason_code"
            ),
            "validation_status": checked.get("status"),
            "effective_action": checked.get("effective_action"),
            "effective_charge_cap_w": checked.get("effective_charge_cap_w"),
            "effective_discharge_w": checked.get("effective_discharge_w"),
            "projected_battery_w": checked.get("projected_battery_w"),
            "passive_power_source_contract": checked.get(
                "passive_power_source_contract"
            ),
            "reject_codes": list(checked.get("reject_codes") or [])[:16],
            "tighten_codes": list(checked.get("tighten_codes") or [])[:16],
        }

    generated_ms = _safe_int(generated_at_ts_ms)
    current_index = next(
        (
            index
            for index, slot in enumerate(plan_slots)
            if _safe_int(slot.get("start_ts_ms"))
            <= generated_ms
            < _safe_int(slot.get("end_ts_ms"))
        ),
        None,
    )
    current_slot = (
        compact_slot(plan_slots[current_index])
        if current_index is not None
        else None
    )
    next_transition = None
    search_from = current_index if current_index is not None else -1
    current_action = (
        str(plan_slots[current_index].get("action") or "")
        if current_index is not None
        else None
    )
    for candidate in plan_slots[search_from + 1 :]:
        candidate_action = str(candidate.get("action") or "")
        if current_action is None or candidate_action != current_action:
            next_transition = compact_slot(candidate)
            break

    summary = {
        "schema_version": SHADOW_SCHEMA,
        "representation": "COMPACT_SUMMARY",
        "algorithm": shadow.get("algorithm") or PLANNER_VERSION,
        "shadow_only": True,
        "commands_allowed": False,
        "runtime_owner": "storage_manager",
        "status": shadow.get("status") or validation.get("status"),
        "planning_input_revision": shadow.get("planning_input_revision"),
        "dv_plan_revision": shadow.get("dv_plan_revision"),
        "physics_validation_revision": shadow.get(
            "physics_validation_revision"
        ),
        "plan_complete": plan.get("complete") is True,
        "slot_count": len(plan_slots),
        "action_counts": action_counts,
        "validation_summary": copy.deepcopy(validation.get("summary") or {}),
        "reject_codes": list(validation.get("reject_codes") or [])[:32],
        "tighten_codes": list(validation.get("tighten_codes") or [])[:32],
        "future_headroom_hold_evidence": copy.deepcopy(
            plan.get("future_headroom_hold_evidence")
        ),
        "reserve_classes": {
            "hard_physical_floor": copy.deepcopy(
                planning_input.get("hard_physical_floor")
            ),
            "protected_demand_reserve": copy.deepcopy(
                planning_input.get("protected_demand_reserve")
            ),
            "soft_charge_target": copy.deepcopy(
                planning_input.get("soft_charge_target")
            ),
        },
        "current_slot": current_slot,
        "next_transition": next_transition,
        "full_payload_persisted": False,
    }
    summary["summary_id"] = _revision(summary)
    return summary


def shadow_not_applicable(
    reason_code: str = "DIRECT_MARKETING_DISABLED",
) -> Dict[str, Any]:
    """Liefert für Systeme ohne DV einen kleinen, wirkungslosen Vertrag."""

    summary = {
        "schema_version": SHADOW_SCHEMA,
        "representation": "COMPACT_SUMMARY",
        "algorithm": PLANNER_VERSION,
        "shadow_only": True,
        "commands_allowed": False,
        "runtime_owner": "storage_manager",
        "status": "NOT_APPLICABLE",
        "reason_code": str(reason_code),
        "planning_input_revision": None,
        "dv_plan_revision": None,
        "physics_validation_revision": None,
        "plan_complete": False,
        "slot_count": 0,
        "action_counts": {action: 0 for action in ALLOWED_ACTIONS},
        "validation_summary": {},
        "reject_codes": [],
        "tighten_codes": [],
        "current_slot": None,
        "next_transition": None,
        "full_payload_persisted": False,
    }
    summary["summary_id"] = _revision(summary)
    return summary


def shadow_error(reason_code: str) -> Dict[str, Any]:
    """Liefert bei Shadow-Fehlern einen sichtbaren, wirkungslosen Vertrag."""

    reason = str(reason_code or "DV_SHADOW_INTERNAL_ERROR")
    summary = {
        "schema_version": SHADOW_SCHEMA,
        "representation": "COMPACT_SUMMARY",
        "algorithm": PLANNER_VERSION,
        "shadow_only": True,
        "commands_allowed": False,
        "runtime_owner": "storage_manager",
        "status": "SHADOW_ERROR",
        "reason_code": reason,
        "planning_input_revision": None,
        "dv_plan_revision": None,
        "physics_validation_revision": None,
        "plan_complete": False,
        "slot_count": 0,
        "action_counts": {action: 0 for action in ALLOWED_ACTIONS},
        "validation_summary": {},
        "reject_codes": [reason],
        "tighten_codes": [],
        "current_slot": None,
        "next_transition": None,
        "full_payload_persisted": False,
    }
    summary["summary_id"] = _revision(summary)
    return summary
