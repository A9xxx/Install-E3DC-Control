#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministischer Storage-Dispatch als lokaler Shadow.

Der Optimierer erzeugt ausschließlich einen erklärbaren Vergleichsplan. Er
kennt weder RSCP noch Treiber und besitzt keine Ausführungsfreigabe.
"""

from __future__ import annotations

import copy
import bisect
import datetime as dt
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

try:
    from .direct_marketing_actions import (
        direct_marketing_export_gate_contract_valid,
        direct_marketing_typed_int_equals,
    )
except ImportError:
    from direct_marketing_actions import (  # type: ignore
        direct_marketing_export_gate_contract_valid,
        direct_marketing_typed_int_equals,
    )


SHADOW_SCHEMA = "storage_dispatch_shadow_v1"
ALGORITHM = "discrete_dynamic_programming_v1"
SLOT_HOURS = 0.25
MODES = (-1, 0, 1)  # Entladen, Halten, Laden
NEG_INF = -1.0e30
SLOT_DURATION_MS = 900_000
MARKET_TIMEZONE = "Europe/Berlin"
PRICE_HORIZON_SCHEMA = "storage_dispatch_price_horizon_v2"
ACTION_HORIZON_SCHEMA = "storage_dispatch_action_horizon_v1"


class ShadowInputError(ValueError):
    """Ein Pflichtinput erlaubt keinen belastbaren Shadowplan."""

    def __init__(
        self,
        reason_code: str,
        diagnostic: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.reason_code = str(reason_code or "SHADOW_INPUT_ERROR")
        self.diagnostic = (
            copy.deepcopy(diagnostic)
            if isinstance(diagnostic, dict)
            else None
        )
        super().__init__(self.reason_code)


def _sf(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _deterministic_result_material(result: Dict[str, Any]) -> Dict[str, Any]:
    """Entfernt reine Messwerte, die keine fachliche Planrevision darstellen."""

    material = copy.deepcopy(result)
    material.pop("runtime_ms", None)
    material.pop("runtime_measurement", None)
    material.pop("shadow_plan_id", None)
    for slot in material.get("slots") or []:
        if isinstance(slot, dict):
            slot.pop("slot_id", None)
    return material


def _percentile(values: Iterable[float], ratio: float) -> Optional[float]:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    position = _clamp(ratio, 0.0, 1.0) * (len(ordered) - 1)
    left = int(math.floor(position))
    right = int(math.ceil(position))
    if left == right:
        return ordered[left]
    weight = position - left
    return ordered[left] * (1.0 - weight) + ordered[right] * weight


def _capacity_wh(plan: Dict[str, Any]) -> float:
    value = _sf(plan.get("battery_capacity"), 0.0) or 0.0
    if 1.0 < value < 500.0:
        value *= 1000.0
    if value <= 1000.0:
        value = (_sf(plan.get("bat_cap_kwh"), 0.0) or 0.0) * 1000.0
    if value <= 1000.0:
        raise ShadowInputError("BATTERY_CAPACITY_MISSING")
    return value


def _parameters(plan: Dict[str, Any], capacity_wh: float) -> Dict[str, Any]:
    direct = plan.get("direct_marketing") if isinstance(plan.get("direct_marketing"), dict) else {}
    economics = direct.get("economics") if isinstance(direct.get("economics"), dict) else {}
    flags = direct.get("flags") if isinstance(direct.get("flags"), dict) else {}
    cfg = plan.get("shadow_dispatch_config") if isinstance(plan.get("shadow_dispatch_config"), dict) else {}
    roundtrip = _clamp(
        (_sf(cfg.get("roundtrip_efficiency_pct", economics.get("roundtrip_efficiency_pct")), 85.0) or 85.0) / 100.0,
        0.50,
        0.99,
    )
    degradation = max(
        0.0,
        _sf(cfg.get("degradation_ct_per_kwh", economics.get("battery_cost_ct_per_kwh")), 4.0) or 4.0,
    )
    max_charge_w = max(0.0, _sf(plan.get("max_charge_w"), 0.0) or 0.0)
    max_discharge_w = max(
        0.0,
        _sf(plan.get("max_discharge_w"), max_charge_w) or 0.0,
    )
    configured_export_w = _sf(plan.get("export_limit_w"), 0.0) or 0.0
    direct_export_w = _sf(flags.get("max_export_w"), 0.0) or 0.0
    export_limit_w = configured_export_w if configured_export_w > 0.0 else direct_export_w
    if export_limit_w <= 0.0:
        export_limit_w = max(20_000.0, max_discharge_w)
    step_default = max(100.0, min(500.0, round((capacity_wh / 140.0) / 50.0) * 50.0))
    requested_step_wh = max(
        50.0,
        _sf(cfg.get("state_step_wh"), step_default) or step_default,
    )
    charge_step_cap_wh = (
        max_charge_w * SLOT_HOURS * math.sqrt(roundtrip)
        if max_charge_w > 0.0
        else None
    )
    discharge_step_cap_wh = (
        max_discharge_w * SLOT_HOURS / max(0.01, math.sqrt(roundtrip))
        if max_discharge_w > 0.0
        else None
    )
    positive_step_caps = [
        value
        for value in (charge_step_cap_wh, discharge_step_cap_wh)
        if value is not None and value >= 50.0
    ]
    state_step_wh = requested_step_wh
    if positive_step_caps:
        physical_step_cap_wh = min(positive_step_caps)
        rounded_cap_wh = max(
            50.0,
            math.floor((physical_step_cap_wh + 1.0e-6) / 50.0) * 50.0,
        )
        state_step_wh = min(requested_step_wh, rounded_cap_wh)
    return {
        "state_step_wh": state_step_wh,
        "state_step_requested_wh": requested_step_wh,
        "state_step_charge_cap_wh": (
            round(charge_step_cap_wh, 6)
            if charge_step_cap_wh is not None
            else None
        ),
        "state_step_discharge_cap_wh": (
            round(discharge_step_cap_wh, 6)
            if discharge_step_cap_wh is not None
            else None
        ),
        "state_step_limited_by_slot_power": state_step_wh < requested_step_wh,
        "roundtrip_efficiency_pct": round(roundtrip * 100.0, 3),
        "charge_efficiency": math.sqrt(roundtrip),
        "discharge_efficiency": math.sqrt(roundtrip),
        "degradation_base_ct_per_kwh": degradation,
        "degradation_segments": [
            {"throughput_from_capacity": 0.0, "throughput_to_capacity": 0.10, "ct_per_kwh": round(degradation, 4)},
            {"throughput_from_capacity": 0.10, "throughput_to_capacity": 0.30, "ct_per_kwh": round(degradation * 1.25, 4)},
            {"throughput_from_capacity": 0.30, "throughput_to_capacity": None, "ct_per_kwh": round(degradation * 1.50, 4)},
        ],
        "degradation_sensitivity_ct_per_kwh": {
            "low": round(max(0.0, degradation * 0.5), 4),
            "base": round(degradation, 4),
            "high": round(degradation * 2.0, 4),
            "calibration": "conservative_configurable_no_cell_parameter_claim",
        },
        "risk_aversion": _clamp(_sf(cfg.get("risk_aversion"), 0.25) or 0.25, 0.0, 2.0),
        "cvar_alpha": _clamp(_sf(cfg.get("cvar_alpha"), 0.90) or 0.90, 0.50, 0.99),
        "switching_cost_ct": max(0.0, _sf(cfg.get("switching_cost_ct"), 0.03) or 0.03),
        "ramp_cost_ct_per_kw": max(0.0, _sf(cfg.get("ramp_cost_ct_per_kw"), 0.005) or 0.005),
        "max_charge_w": max_charge_w,
        "max_discharge_w": max_discharge_w,
        "export_limit_w": export_limit_w,
        "grid_import_limit_w": max(0.0, _sf(plan.get("grid_import_limit_w"), 0.0) or 0.0),
        "economic_reserve_max_pct": _clamp(_sf(cfg.get("economic_reserve_max_pct"), 30.0) or 30.0, 0.0, 80.0),
        "credible_pv_surplus_w": max(100.0, _sf(cfg.get("credible_pv_surplus_w"), 500.0) or 500.0),
        "decision_horizon_slots": max(4, min(192, int(_sf(cfg.get("decision_horizon_slots"), 192) or 192))),
        "terminal_tail_slots": max(0, min(96, int(_sf(cfg.get("terminal_tail_slots"), 96) or 96))),
    }


def _states(capacity_wh: float, step_wh: float) -> List[float]:
    count = max(2, int(math.floor(capacity_wh / step_wh)))
    values = [round(index * step_wh, 6) for index in range(count + 1)]
    if values[-1] < capacity_wh - 1.0:
        values.append(round(capacity_wh, 6))
    else:
        values[-1] = round(capacity_wh, 6)
    return values


def _slot_forecast(slot: Dict[str, Any]) -> Dict[str, Any]:
    forecast = slot.get("forecast_w") if isinstance(slot.get("forecast_w"), dict) else {}
    pv = forecast.get("pv") if isinstance(forecast.get("pv"), dict) else {}
    load = forecast.get("load") if isinstance(forecast.get("load"), dict) else {}
    projection = slot.get("projection") if isinstance(slot.get("projection"), dict) else {}
    pv_point = _sf(
        pv.get("point"),
        _sf(projection.get("pv_w"), 0.0),
    ) or 0.0
    load_point = _sf(load.get("point"), None)
    if load_point is None:
        load_point = sum(
            _sf(projection.get(key), 0.0) or 0.0
            for key in ("home_w", "heat_w", "wallbox_w")
        )
    pv10 = _sf(pv.get("p10"), None)
    pv50 = _sf(pv.get("p50"), None)
    pv90 = _sf(pv.get("p90"), None)
    load10 = _sf(load.get("p10"), None)
    load50 = _sf(load.get("p50"), None)
    load90 = _sf(load.get("p90"), None)
    quantile_contract = (
        forecast.get("quantile_contract")
        if isinstance(forecast.get("quantile_contract"), dict)
        else {}
    )
    pv_contract = (
        quantile_contract.get("pv")
        if isinstance(quantile_contract.get("pv"), dict)
        else {}
    )
    load_contract = (
        quantile_contract.get("load")
        if isinstance(quantile_contract.get("load"), dict)
        else {}
    )
    quantiles = bool(
        quantile_contract.get("status") == "complete"
        and quantile_contract.get("canonical_convention")
        == "cdf_non_exceedance"
        and all(
            contract.get("status") == "complete"
            and contract.get("canonical_convention")
            == "cdf_non_exceedance"
            and isinstance(contract.get("source"), str)
            and bool(contract.get("source"))
            and isinstance(contract.get("revision"), str)
            and bool(contract.get("revision"))
            and contract.get("fresh") is True
            and contract.get("order_valid") is True
            for contract in (pv_contract, load_contract)
        )
        and all(
            value is not None and value >= 0.0
            for value in (pv10, pv50, pv90, load10, load50, load90)
        )
        and float(pv10) <= float(pv50) <= float(pv90)
        and float(load10) <= float(load50) <= float(load90)
    )
    if quantiles:
        scenarios = [
            ("conservative", float(pv10), float(load90), 0.20),
            ("p50", float(pv50), float(load50), 0.60),
            ("favorable", float(pv90), float(load10), 0.20),
        ]
    else:
        scenarios = [
            ("deterministic_point", pv_point, float(load_point), 1.0)
        ]
    return {
        "pv_p10": pv10,
        "pv_p50": pv50,
        "pv_p90": pv90,
        "pv_point": pv_point,
        "load_p10": load10,
        "load_p50": load50,
        "load_p90": load90,
        "load_point": float(load_point),
        "quantiles_available": quantiles,
        "scenarios": scenarios,
    }


def _slot_price(slot: Dict[str, Any]) -> Dict[str, Any]:
    prices = slot.get("prices_ct_kwh") if isinstance(slot.get("prices_ct_kwh"), dict) else {}
    return {
        "buy": _sf(prices.get("buy"), None),
        "gross_sell": _sf(prices.get("gross_sell"), None),
        "net_sell": _sf(prices.get("net_sell"), None),
        "fresh": prices.get("fresh"),
        "status": prices.get("status"),
    }


def _local_market_day_contract(current_start_ms: int) -> Dict[str, Any]:
    """Bindet den aktuellen UTC-Slot an die nächste lokale Markttagsgrenze."""

    try:
        timezone = ZoneInfo(MARKET_TIMEZONE)
        current_local = dt.datetime.fromtimestamp(
            current_start_ms / 1000.0,
            tz=dt.timezone.utc,
        ).astimezone(timezone)
        day_start_local = dt.datetime.combine(
            current_local.date(),
            dt.time.min,
            tzinfo=timezone,
        )
        next_day_start_local = dt.datetime.combine(
            current_local.date() + dt.timedelta(days=1),
            dt.time.min,
            tzinfo=timezone,
        )
        day_start_ms = int(round(day_start_local.timestamp() * 1000.0))
        boundary_ms = int(round(next_day_start_local.timestamp() * 1000.0))
    except (OverflowError, OSError, ValueError) as exc:
        raise ShadowInputError("PRICE_HORIZON_MARKET_DAY_BOUNDARY_INVALID") from exc
    remaining_ms = boundary_ms - current_start_ms
    market_day_ms = boundary_ms - day_start_ms
    if (
        current_start_ms <= 0
        or remaining_ms <= 0
        or remaining_ms % SLOT_DURATION_MS != 0
        or market_day_ms <= 0
        or market_day_ms % SLOT_DURATION_MS != 0
    ):
        raise ShadowInputError("PRICE_HORIZON_MARKET_DAY_BOUNDARY_INVALID")
    return {
        "timezone": MARKET_TIMEZONE,
        "current_slot_start_ts_ms": current_start_ms,
        "local_market_day_start_ts_ms": day_start_ms,
        "next_local_market_day_boundary_ts_ms": boundary_ms,
        "market_day_total_slots": market_day_ms // SLOT_DURATION_MS,
        "required_slots_to_market_day_boundary": remaining_ms // SLOT_DURATION_MS,
    }


def _hard_floor_wh(slot: Dict[str, Any], capacity_wh: float) -> float:
    soc = slot.get("soc_pct") if isinstance(slot.get("soc_pct"), dict) else {}
    # Nur die physische Notstromreserve ist hier lexikographisch hart. Der
    # veröffentlichte/adaptive Kurvenfloor ist ein prädiktiver Risikofloor und
    # darf weder einen historischen Sollpunkt zum Safety-Veto umdeuten noch
    # einen bereits darunter liegenden Ist-SoC rechnerisch hochspringen lassen.
    values = [_sf(soc.get("notstrom_floor"), None)]
    floor_pct = max((value for value in values if value is not None), default=0.0)
    return capacity_wh * _clamp(floor_pct, 0.0, 100.0) / 100.0


def _published_reserve_floor_wh(slot: Dict[str, Any], capacity_wh: float) -> float:
    soc = slot.get("soc_pct") if isinstance(slot.get("soc_pct"), dict) else {}
    reserve_pct = _sf(soc.get("reserve_floor"), None)
    return capacity_wh * _clamp(reserve_pct if reserve_pct is not None else 0.0, 0.0, 100.0) / 100.0


def _ceiling_wh(slot: Dict[str, Any], capacity_wh: float) -> float:
    soc = slot.get("soc_pct") if isinstance(slot.get("soc_pct"), dict) else {}
    ceiling_pct = _sf(soc.get("ceiling"), 100.0)
    return capacity_wh * _clamp(ceiling_pct if ceiling_pct is not None else 100.0, 0.0, 100.0) / 100.0


def _risk_floors(slots: Sequence[Dict[str, Any]], capacity_wh: float, params: Dict[str, Any]) -> Tuple[List[float], Dict[str, Any]]:
    forecasts = [_slot_forecast(slot) for slot in slots]
    quantile_slots = sum(1 for forecast in forecasts if forecast["quantiles_available"])
    published_risk_floor_slots = sum(
        1
        for slot in slots
        if isinstance(slot.get("soc_pct"), dict)
        and _sf(slot["soc_pct"].get("reserve_floor"), None) is not None
    )
    physical_floor_slots = sum(
        1
        for slot in slots
        if isinstance(slot.get("soc_pct"), dict)
        and _sf(slot["soc_pct"].get("notstrom_floor"), None) is not None
    )
    point_forecast_slots = sum(
        1
        for slot in slots
        if slot.get("forecast_scenario_contract")
        == "deterministic_point_without_quantile_claim"
    )
    floors: List[float] = []
    max_extra = capacity_wh * params["economic_reserve_max_pct"] / 100.0
    credible_w = params["credible_pv_surplus_w"]
    for index, slot in enumerate(slots):
        hard = _hard_floor_wh(slot, capacity_wh)
        published = _published_reserve_floor_wh(slot, capacity_wh)
        base_floor = max(hard, published)
        if not forecasts[index]["quantiles_available"]:
            floors.append(base_floor)
            continue
        deficit_wh = 0.0
        credible_seen = 0
        for future in forecasts[index : min(len(slots), index + 96)]:
            if not future["quantiles_available"]:
                break
            surplus_w = float(future["pv_p10"]) - float(future["load_p90"])
            if surplus_w >= credible_w:
                credible_seen += 1
                if credible_seen >= 2:
                    break
            else:
                credible_seen = 0
                deficit_wh += max(0.0, -surplus_w) * SLOT_HOURS / max(0.01, params["discharge_efficiency"])
        floors.append(min(capacity_wh, base_floor + min(max_extra, deficit_wh)))
    if quantile_slots == len(slots):
        scenario_contract = "explicit_cdf_p10_p50_p90"
        method = "p10_pv_p90_load_until_credible_recharge_v1"
    elif quantile_slots == 0 and point_forecast_slots == len(slots):
        scenario_contract = (
            "deterministic_point_with_published_risk_floor_"
            "without_quantile_claim"
        )
        method = (
            "published_risk_floor_plus_deterministic_point_"
            "visible_scenario_v1"
        )
    else:
        scenario_contract = "mixed_or_incomplete_forecast_scenarios"
        method = "mixed_forecast_contract_fail_closed"
    field_activation_input_complete = bool(
        physical_floor_slots == len(slots)
        and quantile_slots == len(slots)
    )
    return floors, {
        "method": method,
        "scenario_contract": scenario_contract,
        "quantile_slots": quantile_slots,
        "point_forecast_slots": point_forecast_slots,
        "published_risk_floor_slots": published_risk_floor_slots,
        "physical_floor_slots": physical_floor_slots,
        "slots": len(slots),
        "field_activation_input_complete": field_activation_input_complete,
        "quantile_invention": False,
        "economic_reserve_max_pct": params["economic_reserve_max_pct"],
        "credible_pv_surplus_w": credible_w,
    }


def _marginal_degradation_ct_per_kwh(energy_wh: float, capacity_wh: float, params: Dict[str, Any]) -> float:
    depth = energy_wh / max(1.0, capacity_wh)
    for segment in params["degradation_segments"]:
        upper = segment["throughput_to_capacity"]
        if upper is None or depth <= float(upper):
            return float(segment["ct_per_kwh"])
    return float(params["degradation_base_ct_per_kwh"])


def _transition_score(
    energy_wh: float,
    next_energy_wh: float,
    previous_mode: int,
    params: Dict[str, Any],
    capacity_wh: float,
    forecast: Dict[str, Any],
    price: Dict[str, Optional[float]],
    *,
    degradation_rate: Optional[float] = None,
) -> Optional[Tuple[int, float, float]]:
    """Bewertet einen DP-Übergang ohne die nur für den gewählten Pfad nötigen Details."""

    delta_wh = next_energy_wh - energy_wh
    eta_c = params["charge_efficiency"]
    eta_d = params["discharge_efficiency"]
    if delta_wh > 0.5:
        mode = 1
        battery_w = delta_wh / max(0.01, eta_c) / SLOT_HOURS
        if battery_w > params["max_charge_w"] + 1.0:
            return None
    elif delta_wh < -0.5:
        mode = -1
        battery_w = delta_wh * eta_d / SLOT_HOURS
        if abs(battery_w) > params["max_discharge_w"] + 1.0:
            return None
    else:
        mode = 0
        battery_w = 0.0

    buy_price = price.get("buy")
    net_sell_price = price.get("net_sell")
    if buy_price is None or net_sell_price is None:
        return None
    buy_price = float(buy_price)
    net_sell_price = float(net_sell_price)
    export_limit_w = params["export_limit_w"]
    grid_import_limit_w = params["grid_import_limit_w"]
    expected_revenue = 0.0
    expected_import_cost = 0.0
    expected_before = 0.0
    worst_before: Optional[float] = None
    for _name, pv_w, load_w, weight in forecast["scenarios"]:
        raw_grid_w = float(load_w) + battery_w - float(pv_w)
        import_w = max(0.0, raw_grid_w)
        raw_export_w = max(0.0, -raw_grid_w)
        export_w = min(raw_export_w, export_limit_w)
        if grid_import_limit_w > 0.0 and import_w > grid_import_limit_w + 1.0:
            return None
        revenue_ct = export_w * SLOT_HOURS / 1000.0 * net_sell_price
        import_cost_ct = import_w * SLOT_HOURS / 1000.0 * buy_price
        scenario_net = revenue_ct - import_cost_ct
        expected_revenue += revenue_ct * weight
        expected_import_cost += import_cost_ct * weight
        expected_before += scenario_net * weight
        if worst_before is None or scenario_net < worst_before:
            worst_before = scenario_net

    throughput_kwh = abs(delta_wh) / 1000.0
    if degradation_rate is None:
        degradation_rate = _marginal_degradation_ct_per_kwh(energy_wh, capacity_wh, params)
    degradation_cost = throughput_kwh * degradation_rate
    if delta_wh > 0.0:
        efficiency_loss_wh = max(0.0, battery_w * SLOT_HOURS - delta_wh)
        efficiency_price = max(0.0, buy_price)
    else:
        efficiency_loss_wh = max(0.0, -delta_wh - abs(battery_w) * SLOT_HOURS)
        efficiency_price = max(0.0, net_sell_price)
    efficiency_loss_cost = efficiency_loss_wh / 1000.0 * efficiency_price
    risk_margin = params["risk_aversion"] * max(
        0.0,
        expected_before - (worst_before if worst_before is not None else expected_before),
    )
    switching_cost = (
        params["switching_cost_ct"]
        if previous_mode != 0 and mode != 0 and mode != previous_mode
        else 0.0
    )
    ramp_cost = params["ramp_cost_ct_per_kw"] * abs(battery_w) / 1000.0
    immediate = (
        expected_revenue
        - expected_import_cost
        - degradation_cost
        - efficiency_loss_cost
        - risk_margin
        - switching_cost
        - ramp_cost
    )
    return mode, battery_w, immediate


def _transition(
    slot: Dict[str, Any],
    energy_wh: float,
    next_energy_wh: float,
    previous_mode: int,
    params: Dict[str, Any],
    capacity_wh: float,
    *,
    detailed: bool = True,
    forecast: Optional[Dict[str, Any]] = None,
    price: Optional[Dict[str, Optional[float]]] = None,
) -> Optional[Any]:
    forecast = forecast or _slot_forecast(slot)
    price = price or _slot_price(slot)
    if not detailed:
        return _transition_score(
            energy_wh,
            next_energy_wh,
            previous_mode,
            params,
            capacity_wh,
            forecast,
            price,
        )

    delta_wh = next_energy_wh - energy_wh
    eta_c = params["charge_efficiency"]
    eta_d = params["discharge_efficiency"]
    if delta_wh > 0.5:
        mode = 1
        battery_w = delta_wh / max(0.01, eta_c) / SLOT_HOURS
        if battery_w > params["max_charge_w"] + 1.0:
            return None
    elif delta_wh < -0.5:
        mode = -1
        battery_w = delta_wh * eta_d / SLOT_HOURS
        if abs(battery_w) > params["max_discharge_w"] + 1.0:
            return None
    else:
        mode = 0
        battery_w = 0.0

    if price["buy"] is None or price["net_sell"] is None:
        return None
    scenario_rows = []
    expected_revenue = 0.0
    expected_import_cost = 0.0
    expected_curtailment_wh = 0.0
    expected_avoided_curtailment_wh = 0.0
    expected_baseline_net_ct = 0.0
    scenario_net_before_internal_cost = []
    battery_export_wh = 0.0
    for name, pv_w, load_w, weight in forecast["scenarios"]:
        raw_grid_w = float(load_w) + battery_w - float(pv_w)
        import_w = max(0.0, raw_grid_w)
        raw_export_w = max(0.0, -raw_grid_w)
        export_w = min(raw_export_w, params["export_limit_w"])
        curtailment_w = max(0.0, raw_export_w - export_w)
        no_battery_raw_export_w = max(0.0, float(pv_w) - float(load_w))
        no_battery_import_w = max(0.0, float(load_w) - float(pv_w))
        no_battery_export_w = min(no_battery_raw_export_w, params["export_limit_w"])
        no_battery_curtailment_w = max(0.0, no_battery_raw_export_w - params["export_limit_w"])
        avoided_curtailment_w = max(0.0, no_battery_curtailment_w - curtailment_w)
        if params["grid_import_limit_w"] > 0.0 and import_w > params["grid_import_limit_w"] + 1.0:
            return None
        revenue_ct = export_w * SLOT_HOURS / 1000.0 * float(price["net_sell"])
        import_cost_ct = import_w * SLOT_HOURS / 1000.0 * float(price["buy"])
        scenario_net = revenue_ct - import_cost_ct
        baseline_net_ct = (
            no_battery_export_w * SLOT_HOURS / 1000.0 * float(price["net_sell"])
            - no_battery_import_w * SLOT_HOURS / 1000.0 * float(price["buy"])
        )
        scenario_net_before_internal_cost.append((name, scenario_net, weight))
        expected_revenue += revenue_ct * weight
        expected_import_cost += import_cost_ct * weight
        expected_curtailment_wh += curtailment_w * SLOT_HOURS * weight
        expected_avoided_curtailment_wh += avoided_curtailment_w * SLOT_HOURS * weight
        expected_baseline_net_ct += baseline_net_ct * weight
        if battery_w < 0.0:
            natural_export_w = min(no_battery_raw_export_w, params["export_limit_w"])
            battery_export_wh += max(0.0, export_w - natural_export_w) * SLOT_HOURS * weight
        if detailed:
            scenario_rows.append({
                "name": name,
                "weight": weight,
                "pv_w": round(float(pv_w), 3),
                "load_w": round(float(load_w), 3),
                "grid_import_w": round(import_w, 3),
                "grid_export_w": round(export_w, 3),
                "curtailment_w": round(curtailment_w, 3),
                "net_before_internal_cost_ct": round(scenario_net, 6),
                "baseline_net_ct": round(baseline_net_ct, 6),
                "incremental_net_before_internal_cost_ct": round(
                    scenario_net - baseline_net_ct,
                    6,
                ),
            })

    throughput_kwh = abs(delta_wh) / 1000.0
    degradation_rate = _marginal_degradation_ct_per_kwh(energy_wh, capacity_wh, params)
    degradation_cost = throughput_kwh * degradation_rate
    if delta_wh > 0.0:
        efficiency_loss_wh = max(0.0, battery_w * SLOT_HOURS - delta_wh)
        efficiency_price = max(0.0, float(price["buy"]))
    else:
        efficiency_loss_wh = max(0.0, -delta_wh - abs(battery_w) * SLOT_HOURS)
        efficiency_price = max(0.0, float(price["net_sell"]))
    efficiency_loss_cost = efficiency_loss_wh / 1000.0 * efficiency_price
    expected_before = sum(value * weight for _, value, weight in scenario_net_before_internal_cost)
    worst_before = min(value for _, value, _ in scenario_net_before_internal_cost)
    risk_margin = params["risk_aversion"] * max(0.0, expected_before - worst_before)
    switching_cost = params["switching_cost_ct"] if previous_mode != 0 and mode != 0 and mode != previous_mode else 0.0
    ramp_cost = params["ramp_cost_ct_per_kw"] * abs(battery_w) / 1000.0
    immediate = expected_revenue - expected_import_cost - degradation_cost - efficiency_loss_cost - risk_margin - switching_cost - ramp_cost
    return {
        "mode": mode,
        "battery_w": round(battery_w, 6),
        "delta_wh": round(delta_wh, 6),
        "revenue_ct": expected_revenue,
        "import_cost_ct": expected_import_cost,
        "efficiency_loss_cost_ct": efficiency_loss_cost,
        "degradation_cost_ct": degradation_cost,
        "risk_margin_ct": risk_margin,
        "switching_cost_ct": switching_cost + ramp_cost,
        "immediate_net_ct": immediate,
        "baseline_net_ct": expected_baseline_net_ct,
        "incremental_net_ct": immediate - expected_baseline_net_ct,
        "expected_curtailment_wh": expected_curtailment_wh,
        "avoided_curtailment_wh": expected_avoided_curtailment_wh,
        "battery_export_wh": battery_export_wh,
        "scenario_rows": scenario_rows,
        "forecast_contract": (
            "explicit_cdf_p10_p50_p90"
            if forecast["quantiles_available"]
            else "deterministic_point_without_quantile_claim"
        ),
        "prices": price,
    }


def _hold_immediate_from_transition(
    transition: Dict[str, Any],
    params: Dict[str, Any],
) -> float:
    """Rekonstruiert HOLD ohne einen zweiten Übergang auszuwerten."""

    scenario_rows = [
        row
        for row in transition.get("scenario_rows") or []
        if isinstance(row, dict)
    ]
    if not scenario_rows:
        raise ShadowInputError("HOLD_COUNTERFACTUAL_SCENARIOS_MISSING")
    expected_baseline_net_ct = float(transition["baseline_net_ct"])
    worst_baseline_net_ct = min(
        float(row["baseline_net_ct"])
        for row in scenario_rows
    )
    hold_risk_margin_ct = params["risk_aversion"] * max(
        0.0,
        expected_baseline_net_ct - worst_baseline_net_ct,
    )
    return expected_baseline_net_ct - hold_risk_margin_ct


def _terminal_salvage(slots: Sequence[Dict[str, Any]], states: Sequence[float], params: Dict[str, Any]) -> Tuple[List[List[float]], Dict[str, Any]]:
    recent = list(slots[-min(8, len(slots)) :])
    buy_prices = [price for slot in recent if (price := _slot_price(slot)["buy"]) is not None]
    net_prices = [price for slot in recent if (price := _slot_price(slot)["net_sell"]) is not None]
    conservative_buy = _percentile(buy_prices, 0.25) or 0.0
    conservative_sell = _percentile(net_prices, 0.25) or 0.0
    salvage_rate = max(
        0.0,
        min(conservative_buy * params["discharge_efficiency"], conservative_sell)
        - params["degradation_base_ct_per_kwh"],
    )
    values = [[state / 1000.0 * salvage_rate for _mode in MODES] for state in states]
    return values, {
        "method": "conservative_tail_bound_v1",
        "salvage_ct_per_kwh": round(salvage_rate, 6),
        "price_sample_count": max(len(buy_prices), len(net_prices)),
        "fallback_reason_code": None if recent else "TAIL_INPUT_MISSING",
    }


def _solve_backward(
    slots: Sequence[Dict[str, Any]],
    states: Sequence[float],
    floors: Sequence[float],
    ceilings: Sequence[float],
    params: Dict[str, Any],
    capacity_wh: float,
    terminal_values: List[List[float]],
    *,
    deadline_index: Optional[int] = None,
    deadline_max_energy_wh: Optional[float] = None,
    minimum_deadline_index: Optional[int] = None,
    minimum_deadline_energy_wh: Optional[float] = None,
    keep_choices: bool = True,
) -> Tuple[List[List[float]], List[Dict[Tuple[int, int], Tuple[int, int, Optional[float]]]]]:
    future = copy.deepcopy(terminal_values)
    choices: List[Dict[Tuple[int, int], Tuple[int, int, Optional[float]]]] = [
        dict() for _ in slots
    ]
    mode_count = len(MODES)
    hold_mode_index = MODES.index(0)
    score_epsilon = 1.0e-9

    # Physikalische Reichweite und Degradationsklasse hängen nur vom
    # Ausgangszustand ab, nicht vom betrachteten Slot. Die bisherige Schleife
    # berechnete deshalb dieselben Bisect-Grenzen und Klassen für jeden der bis
    # zu 192 Slots erneut. Die vorbereiteten Indizes verändern weder das
    # Zustandsraster noch die inklusive +/-1-Wh-Toleranz.
    discharge_span_wh = (
        params["max_discharge_w"]
        * SLOT_HOURS
        / max(0.01, params["discharge_efficiency"])
    )
    charge_span_wh = (
        params["max_charge_w"]
        * SLOT_HOURS
        * params["charge_efficiency"]
    )
    reachable_ranges: List[Tuple[int, int]] = []
    degradation_rates: List[float] = []
    for energy in states:
        reachable_ranges.append((
            bisect.bisect_left(states, energy - discharge_span_wh - 1.0),
            bisect.bisect_right(states, energy + charge_span_wh + 1.0),
        ))
        degradation_rates.append(
            _marginal_degradation_ct_per_kwh(energy, capacity_wh, params)
        )

    # Ein Übergangsscore hängt innerhalb dieses DP-Aufrufs ausschließlich von
    # Slotprognose/-preis, Energiedelta und Degradationsklasse ab. Identische
    # numerische Slots dürfen daher dieselben unveränderlichen Tupel verwenden.
    # Plan-, Slot- und Action-Identitäten bleiben außerhalb dieses rein
    # numerischen Kerns vollständig und werden weiterhin separat gebildet.
    transition_caches: Dict[
        Tuple[Any, ...],
        Dict[Tuple[float, float], Optional[Tuple[int, float, float]]],
    ] = {}
    cache_miss = object()
    for slot_index in range(len(slots) - 1, -1, -1):
        slot = slots[slot_index]
        slot_forecast = _slot_forecast(slot)
        slot_price = _slot_price(slot)
        current_values = [[NEG_INF for _mode in MODES] for _state in states]
        floor = floors[slot_index]
        ceiling = ceilings[slot_index]
        first_next_state = bisect.bisect_left(states, floor - 1.0)
        after_current_state = bisect.bisect_right(states, ceiling + 1.0)
        after_next_state = after_current_state
        if deadline_index == slot_index and deadline_max_energy_wh is not None:
            after_next_state = min(
                after_next_state,
                bisect.bisect_right(states, deadline_max_energy_wh + 1.0),
            )
        if (
            minimum_deadline_index == slot_index
            and minimum_deadline_energy_wh is not None
        ):
            first_next_state = max(
                first_next_state,
                bisect.bisect_left(states, minimum_deadline_energy_wh - 1.0),
            )

        transition_signature = (
            tuple(
                (str(name), float(pv_w), float(load_w), float(weight))
                for name, pv_w, load_w, weight in slot_forecast["scenarios"]
            ),
            slot_price.get("buy"),
            slot_price.get("net_sell"),
        )
        transition_cache = transition_caches.setdefault(
            transition_signature,
            {},
        )
        for state_index in range(after_current_state):
            energy = states[state_index]
            # Der Startwert eines Slots wurde am Ende des vorherigen Slots
            # gegen dessen Floor geprüft. Ein jetzt ansteigender Risikofloor
            # muss innerhalb dieses Slots physisch erreichbar werden dürfen;
            # sonst würde ein Istwert unter einer neuen Kurve rechnerisch
            # verschwinden oder den gesamten Plan fälschlich infeasible machen.
            # Für jeden Zielmodus genügt dessen bestes Ziel. Umschaltkosten
            # hängen nur von Quell- und Zielmodus ab; ein innerhalb desselben
            # Zielmodus schlechterer Übergang kann deshalb für keinen der drei
            # Vorgängermodi gewinnen. Das erhält Score und Tie-Breaking exakt,
            # vermeidet aber die dreifache Vollauswertung aller Zielzustände.
            best_next_indices = [-1] * mode_count
            best_scores = [NEG_INF] * mode_count
            best_negative_abs_w = [NEG_INF] * mode_count
            degradation_rate = degradation_rates[state_index]
            physical_first, physical_after = reachable_ranges[state_index]
            first_reachable = max(physical_first, first_next_state)
            after_reachable = min(physical_after, after_next_state)
            for next_index in range(first_reachable, after_reachable):
                next_energy = states[next_index]
                cache_key = (next_energy - energy, degradation_rate)
                transition_score = transition_cache.get(cache_key, cache_miss)
                if transition_score is cache_miss:
                    transition_score = _transition_score(
                        energy,
                        next_energy,
                        0,
                        params,
                        capacity_wh,
                        slot_forecast,
                        slot_price,
                        degradation_rate=degradation_rate,
                    )
                    transition_cache[cache_key] = transition_score
                if transition_score is None:
                    continue
                mode, battery_w, immediate_net_ct = transition_score
                mode_index = int(mode) + 1
                continuation = future[next_index][mode_index]
                if continuation <= NEG_INF / 2:
                    continue
                base_score = float(immediate_net_ct) + continuation
                negative_abs_w = -abs(float(battery_w))
                existing_next = best_next_indices[mode_index]
                existing_score = best_scores[mode_index]
                if existing_next < 0 or base_score > existing_score + score_epsilon or (
                    abs(base_score - existing_score) <= score_epsilon
                    and (negative_abs_w, -next_index)
                    > (best_negative_abs_w[mode_index], -existing_next)
                ):
                    best_next_indices[mode_index] = next_index
                    best_scores[mode_index] = base_score
                    best_negative_abs_w[mode_index] = negative_abs_w
            hold_next = best_next_indices[hold_mode_index]
            hold_score = (
                float(best_scores[hold_mode_index])
                if hold_next >= 0
                else None
            )
            for previous_mode_index, previous_mode in enumerate(MODES):
                best_score = NEG_INF
                best_choice = None
                for mode_index, mode in enumerate(MODES):
                    next_index = best_next_indices[mode_index]
                    if next_index < 0:
                        continue
                    base_score = best_scores[mode_index]
                    direction_switch_cost = (
                        params["switching_cost_ct"]
                        if previous_mode != 0 and mode != 0 and mode != previous_mode
                        else 0.0
                    )
                    score = base_score - direction_switch_cost
                    tie = (
                        int(mode == previous_mode),
                        int(mode == 0),
                        best_negative_abs_w[mode_index],
                        -next_index,
                    )
                    if best_choice is None or score > best_score + score_epsilon or (
                        abs(score - best_score) <= score_epsilon
                        and tie > best_choice[2]
                    ):
                        best_score = score
                        best_choice = (
                            next_index,
                            mode_index,
                            tie,
                        )
                if best_choice is not None:
                    current_values[state_index][previous_mode_index] = best_score
                    if keep_choices:
                        score_margin_vs_hold = (
                            float(best_score) - hold_score
                            if best_choice[1] != MODES.index(0)
                            and hold_score is not None
                            else None
                        )
                        choices[slot_index][(state_index, previous_mode_index)] = (
                            best_choice[0],
                            best_choice[1],
                            score_margin_vs_hold,
                        )
        future = current_values
    return future, choices


def _forward_infeasibility_diagnostic(
    slots: Sequence[Dict[str, Any]],
    states: Sequence[float],
    floors: Sequence[float],
    ceilings: Sequence[float],
    params: Dict[str, Any],
    capacity_wh: float,
    initial_index: int,
) -> Dict[str, Any]:
    """Lokalisiert den ersten physikalisch unerreichbaren Shadow-Slot.

    Diese Zusatzprüfung läuft ausschließlich nach einem gescheiterten DP-Lauf.
    Sie verändert weder Floors noch Kandidaten und erzeugt keine Ersatzwerte.
    """

    reachable = {int(initial_index)}
    last_reachable = sorted(reachable)
    for slot_index, slot in enumerate(slots):
        forecast = _slot_forecast(slot)
        price = _slot_price(slot)
        next_reachable = set()
        for state_index in reachable:
            energy = states[state_index]
            if energy > ceilings[slot_index] + 1.0:
                continue
            minimum_reachable = energy - params["max_discharge_w"] * SLOT_HOURS / max(
                0.01,
                params["discharge_efficiency"],
            )
            maximum_reachable = (
                energy
                + params["max_charge_w"]
                * SLOT_HOURS
                * params["charge_efficiency"]
            )
            first_index = bisect.bisect_left(states, minimum_reachable - 1.0)
            after_index = bisect.bisect_right(states, maximum_reachable + 1.0)
            for next_index in range(first_index, after_index):
                next_energy = states[next_index]
                if (
                    next_energy < floors[slot_index] - 1.0
                    or next_energy > ceilings[slot_index] + 1.0
                ):
                    continue
                if _transition_score(
                    energy,
                    next_energy,
                    0,
                    params,
                    capacity_wh,
                    forecast,
                    price,
                ) is not None:
                    next_reachable.add(next_index)
        if not next_reachable:
            before_values = [states[index] for index in sorted(reachable)]
            return {
                "schema_version": "storage_dispatch_infeasibility_v1",
                "first_infeasible_slot_index": slot_index,
                "first_infeasible_slot_start_ts_ms": int(
                    _sf(slot.get("start_ts_ms"), 0.0) or 0.0
                ),
                "first_infeasible_slot_end_ts_ms": int(
                    _sf(slot.get("end_ts_ms"), 0.0) or 0.0
                ),
                "reachable_state_count_before": len(before_values),
                "reachable_energy_min_wh_before": (
                    round(min(before_values), 3) if before_values else None
                ),
                "reachable_energy_max_wh_before": (
                    round(max(before_values), 3) if before_values else None
                ),
                "required_floor_wh": round(float(floors[slot_index]), 3),
                "allowed_ceiling_wh": round(float(ceilings[slot_index]), 3),
                "state_step_wh": round(float(params["state_step_wh"]), 3),
                "max_charge_w": round(float(params["max_charge_w"]), 3),
                "max_discharge_w": round(float(params["max_discharge_w"]), 3),
                "grid_import_limit_w": round(
                    float(params["grid_import_limit_w"]),
                    3,
                ),
                "price_status": price.get("status"),
                "price_fresh": price.get("fresh"),
                "forecast_contract": (
                    "explicit_cdf_p10_p50_p90"
                    if forecast["quantiles_available"]
                    else "deterministic_point_without_quantile_claim"
                ),
                "analysis_scope": (
                    "DECISION_HORIZON_FORWARD_REACHABILITY_"
                    "NO_TERMINAL_CONTINUATION"
                ),
                "diagnostic_effect": "READ_ONLY_NO_CONSTRAINT_RELAXATION",
            }
        last_reachable = sorted(next_reachable)
        reachable = next_reachable
    final_values = [states[index] for index in last_reachable]
    return {
        "schema_version": "storage_dispatch_infeasibility_v1",
        "first_infeasible_slot_index": None,
        "forward_path_complete": True,
        "final_reachable_state_count": len(final_values),
        "final_reachable_energy_min_wh": (
            round(min(final_values), 3) if final_values else None
        ),
        "final_reachable_energy_max_wh": (
            round(max(final_values), 3) if final_values else None
        ),
        "analysis_scope": (
            "DECISION_HORIZON_FORWARD_REACHABILITY_"
            "NO_TERMINAL_CONTINUATION"
        ),
        "diagnostic_effect": "READ_ONLY_NO_CONSTRAINT_RELAXATION",
    }


def _closest_state_index(states: Sequence[float], energy_wh: float, floor_wh: float, ceiling_wh: float) -> int:
    candidates = [
        (abs(state - energy_wh), index)
        for index, state in enumerate(states)
        if floor_wh - 1.0 <= state <= ceiling_wh + 1.0
    ]
    if not candidates:
        raise ShadowInputError("INITIAL_STATE_OUTSIDE_CONSTRAINTS")
    return min(candidates)[1]


def _trace_path(
    slots: Sequence[Dict[str, Any]],
    states: Sequence[float],
    choices: Sequence[Dict[Tuple[int, int], Tuple[int, int, Optional[float]]]],
    initial_index: int,
    params: Dict[str, Any],
    capacity_wh: float,
) -> Dict[str, Any]:
    state_index = initial_index
    mode_index = MODES.index(0)
    rows = []
    energies = [states[state_index]]
    scenario_totals: Dict[str, float] = {}
    for slot_index, slot in enumerate(slots):
        choice = choices[slot_index].get((state_index, mode_index))
        if choice is None:
            raise ShadowInputError("DP_PATH_INFEASIBLE")
        next_index, next_mode_index, score_margin_vs_hold = choice
        transition = _transition(
            slot,
            states[state_index],
            states[next_index],
            MODES[mode_index],
            params,
            capacity_wh,
        )
        if not isinstance(transition, dict):
            raise ShadowInputError("DP_PATH_TRANSITION_INVALID")
        hold_immediate_net_ct = (
            _hold_immediate_from_transition(transition, params)
            if score_margin_vs_hold is not None
            else None
        )
        immediate_delta_vs_hold = (
            float(transition["immediate_net_ct"])
            - hold_immediate_net_ct
            if hold_immediate_net_ct is not None
            else None
        )
        for scenario in transition["scenario_rows"]:
            scenario_totals[scenario["name"]] = scenario_totals.get(scenario["name"], 0.0) + float(scenario["net_before_internal_cost_ct"])
        rows.append({
            "slot": slot,
            "energy_start_wh": states[state_index],
            "energy_end_wh": states[next_index],
            "transition": transition,
            "choice_diagnostic": {
                "hold_available": score_margin_vs_hold is not None,
                "score_margin_vs_hold_ct": score_margin_vs_hold,
                "immediate_delta_vs_hold_ct": immediate_delta_vs_hold,
                "continuation_uplift_vs_hold_ct": (
                    score_margin_vs_hold - immediate_delta_vs_hold
                    if score_margin_vs_hold is not None
                    and immediate_delta_vs_hold is not None
                    else None
                ),
            },
        })
        state_index = next_index
        mode_index = next_mode_index
        energies.append(states[state_index])
    return {
        "rows": rows,
        "energies": energies,
        "scenario_totals_ct": scenario_totals,
        "final_state_index": state_index,
        "final_mode_index": mode_index,
    }


def _deadline_index(slots: Sequence[Dict[str, Any]], deadline_ms: int) -> Optional[int]:
    if deadline_ms <= 0:
        return None
    for index, slot in enumerate(slots):
        start = int(_sf(slot.get("start_ts_ms"), 0.0) or 0.0)
        end = int(_sf(slot.get("end_ts_ms"), start + 900_000) or (start + 900_000))
        if start < deadline_ms <= end:
            return index
        if start >= deadline_ms:
            return max(0, index - 1)
    return None


def _curve_target_contract(
    plan: Dict[str, Any],
    slots: Sequence[Dict[str, Any]],
    capacity_wh: float,
    initial_energy_wh: float,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Projiziert die Legacy-Ladekurve ausschließlich als Vergleichsbaseline.

    Die kanonische Entscheidung schützt Haus-, Nacht- und Notstrombedarf über
    harte Floors, den P10-PV/P90-Last-Risikofloor und den Terminalwert. Ein
    Legacy-Kurvenpunkt darf diese Verträge weder verschärfen noch wirtschaftlich
    sinnvolle Entladung hinter eine historische Kurvendeadline verschieben.
    """

    points: List[Tuple[int, float]] = []
    for item in plan.get("target_timeline") or []:
        if not isinstance(item, dict):
            continue
        timestamp = int(_sf(item.get("ts"), 0.0) or 0.0)
        if 0 < timestamp < 100_000_000_000:
            timestamp *= 1000
        soc_pct = _sf(item.get("soc"), None)
        if timestamp > 0 and soc_pct is not None:
            points.append((timestamp, _clamp(float(soc_pct), 0.0, 100.0)))
    points.sort(key=lambda item: item[0])
    start_ms = int(_sf(slots[0].get("start_ts_ms"), 0.0) or 0.0) if slots else 0
    end_ms = int(_sf(slots[-1].get("end_ts_ms"), 0.0) or 0.0) if slots else 0
    future_points = [point for point in points if start_ms < point[0] <= end_ms]
    if not future_points:
        return {
            "source": "target_timeline",
            "status": "OBSERVATION_BASELINE_NO_FUTURE_POINT",
            "enforced": False,
            "deadline_ts_ms": None,
            "deadline_slot_index": None,
            "target_soc_pct": None,
            "target_energy_wh": None,
            "initial_energy_wh": round(initial_energy_wh, 3),
            "minimum_energy_delta_wh": 0.0,
            "intermediate_points_policy": "ALL_POINTS_DIAGNOSTIC_ONLY",
            "canonical_energy_contract": "HARD_FLOOR_PLUS_P10_PV_P90_LOAD_RISK_FLOOR_AND_TERMINAL_VALUE",
        }

    deadline_ms, target_soc_pct = future_points[-1]
    deadline_index = _deadline_index(slots, deadline_ms)
    if deadline_index is None:
        return {
            "source": "target_timeline",
            "status": "OBSERVATION_BASELINE_OUTSIDE_DECISION_HORIZON",
            "enforced": False,
            "deadline_ts_ms": deadline_ms,
            "deadline_slot_index": None,
            "target_soc_pct": round(target_soc_pct, 3),
            "target_energy_wh": round(capacity_wh * target_soc_pct / 100.0, 3),
            "initial_energy_wh": round(initial_energy_wh, 3),
            "minimum_energy_delta_wh": 0.0,
            "intermediate_points_policy": "ALL_POINTS_DIAGNOSTIC_ONLY",
            "canonical_energy_contract": "HARD_FLOOR_PLUS_P10_PV_P90_LOAD_RISK_FLOOR_AND_TERMINAL_VALUE",
        }

    target_energy_wh = capacity_wh * target_soc_pct / 100.0
    state_step_wh = max(1.0, float(params["state_step_wh"]))
    maximum_slot_state_gain_wh = math.floor(
        (
            params["max_charge_w"]
            * SLOT_HOURS
            * params["charge_efficiency"]
            + 1.0
        )
        / state_step_wh
    ) * state_step_wh
    physical_reachable_wh = min(
        capacity_wh,
        initial_energy_wh
        + (deadline_index + 1) * maximum_slot_state_gain_wh,
        _ceiling_wh(slots[deadline_index], capacity_wh),
    )
    reachable_state_wh = math.floor((physical_reachable_wh + 1.0) / state_step_wh) * state_step_wh
    if capacity_wh - physical_reachable_wh <= 1.0:
        reachable_state_wh = capacity_wh
    constraint_energy_wh = min(target_energy_wh, reachable_state_wh)
    unavoidable_shortfall_wh = max(0.0, target_energy_wh - constraint_energy_wh)
    considered = list(slots[: deadline_index + 1])
    later_prices = [
        float(price)
        for slot in considered[1:]
        if (price := _slot_price(slot)["net_sell"]) is not None
    ]
    conservative_surplus_wh = 0.0
    point_surplus_wh = 0.0
    quantile_slots = 0
    for slot in considered[1:]:
        forecast = _slot_forecast(slot)
        point_surplus_wh += max(
            0.0,
            float(forecast["pv_point"])
            - float(forecast["load_point"]),
        ) * SLOT_HOURS
        if forecast["quantiles_available"]:
            quantile_slots += 1
            conservative_surplus_wh += max(
                0.0,
                float(forecast["pv_p10"]) - float(forecast["load_p90"]),
            ) * SLOT_HOURS
    first_projection = slots[0].get("projection") if isinstance(slots[0].get("projection"), dict) else {}
    return {
        "source": "target_timeline",
        "status": "OBSERVATION_BASELINE_ONLY",
        "enforced": False,
        "deadline_ts_ms": deadline_ms,
        "deadline_slot_index": deadline_index,
        "target_soc_pct": round(target_soc_pct, 3),
        "target_energy_wh": round(target_energy_wh, 3),
        "constraint_energy_wh": round(constraint_energy_wh, 3),
        "physical_reachable_energy_wh": round(physical_reachable_wh, 3),
        "unavoidable_target_shortfall_wh": round(unavoidable_shortfall_wh, 3),
        "initial_energy_wh": round(initial_energy_wh, 3),
        "minimum_energy_delta_wh": round(max(0.0, target_energy_wh - initial_energy_wh), 3),
        "current_net_sell_ct_kwh": _slot_price(slots[0])["net_sell"],
        "later_min_net_sell_ct_kwh": round(min(later_prices), 6) if later_prices else None,
        "later_conservative_pv_surplus_wh": round(conservative_surplus_wh, 3),
        "later_point_pv_surplus_wh": round(point_surplus_wh, 3),
        "later_p50_pv_surplus_wh": (
            round(point_surplus_wh, 3)
            if considered[1:] and quantile_slots == len(considered[1:])
            else None
        ),
        "forecast_contract": (
            "P10_PV_MINUS_P90_LOAD"
            if considered[1:] and quantile_slots == len(considered[1:])
            else "DETERMINISTIC_POINT_WITHOUT_QUANTILE_CLAIM"
        ),
        "legacy_first_slot": {
            "owner": "legacy_curve_projection",
            "action": slots[0].get("planned_action"),
            "battery_w": round(float(_sf(first_projection.get("battery_w"), 0.0) or 0.0), 3),
            "target_soc_pct": first_projection.get("target_soc_pct"),
        },
        "intermediate_points_policy": "ALL_POINTS_DIAGNOSTIC_ONLY",
        "valuation_policy": "NO_LEGACY_CURVE_CONSTRAINT_OR_TERMINAL_VALUE",
        "canonical_energy_contract": "HARD_FLOOR_PLUS_P10_PV_P90_LOAD_RISK_FLOOR_AND_TERMINAL_VALUE",
    }


def _headroom_contract(plan: Dict[str, Any], slots: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    raw_required = max(
        0.0,
        (_sf(plan.get("adaptive_headroom_required_wh"), None) or 0.0)
        if plan.get("adaptive_headroom_required_wh") is not None
        else (_sf(plan.get("predump_dump_wh"), 0.0) or 0.0),
    )
    if raw_required <= 0.0:
        raw_required = sum(
            max(0.0, _sf((slot.get("headroom_wh") or {}).get("required"), 0.0) or 0.0)
            for slot in slots
            if isinstance(slot.get("headroom_wh"), dict)
        )
    deadline = int(_sf(plan.get("predump_end_ts"), 0.0) or 0.0)
    if 0 < deadline < 100_000_000_000:
        deadline *= 1000
    preventable_present = plan.get("predump_preventable_clipping_wh") is not None
    required_present = bool(
        plan.get("adaptive_headroom_required_wh") is not None
        or plan.get("predump_dump_wh") is not None
    )
    preventable = max(0.0, _sf(plan.get("predump_preventable_clipping_wh"), 0.0) or 0.0)
    pressure_valid = bool(
        required_present
        and preventable_present
        and raw_required >= 200.0
        and preventable >= 200.0
        and deadline > 0
    )
    required = min(raw_required, preventable) if pressure_valid else 0.0
    block_reason = None
    if not required_present or raw_required < 200.0:
        block_reason = "HEADROOM_REQUIRED_ENERGY_MISSING"
    elif not preventable_present or preventable < 200.0:
        block_reason = "PREVENTABLE_CURTAILMENT_MISSING"
    elif deadline <= 0:
        block_reason = "HEADROOM_DEADLINE_MISSING"
    return {
        "required_wh": required,
        "raw_required_wh": raw_required,
        "deadline_ts_ms": deadline or None,
        "preventable_clipping_reference_wh": preventable,
        "reference_source": "predump_preventable_clipping_wh" if preventable_present else None,
        "physical_headroom_justified": pressure_valid,
        "block_reason_code": block_reason,
    }


def _policy_ts_ms(value: Any) -> int:
    timestamp = int(_sf(value, 0.0) or 0.0)
    if 0 < timestamp < 100_000_000_000:
        timestamp *= 1000
    return timestamp


def _policy_decision_for_slot(plan: Dict[str, Any], slot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    direct = plan.get("direct_marketing") if isinstance(plan.get("direct_marketing"), dict) else {}
    start_ms = int(_sf(slot.get("start_ts_ms"), 0.0) or 0.0)
    decisions = [
        decision
        for decision in direct.get("policy_timeline") or []
        if isinstance(decision, dict)
    ]
    current = direct.get("policy_decision") if isinstance(direct.get("policy_decision"), dict) else None
    if current:
        decisions.append(current)
    for decision in decisions:
        selected = decision.get("selected_window") if isinstance(decision.get("selected_window"), dict) else {}
        start = _policy_ts_ms(decision.get("start_ts", selected.get("start_ts")))
        end = _policy_ts_ms(decision.get("end_ts", selected.get("end_ts")))
        if start > 0 and end > start and start <= start_ms < end:
            return decision
    return None


def _action_horizon_contract(
    plan: Dict[str, Any],
    slot: Dict[str, Any],
    action: str,
    horizon_start_ms: int,
    horizon_end_ms: int,
    policy_decision: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Bindet ein Aktionsfenster vollständig an den fachlichen Planungshorizont."""

    slot_start_ms = int(_sf(slot.get("start_ts_ms"), 0.0) or 0.0)
    slot_end_ms = int(_sf(slot.get("end_ts_ms"), 0.0) or 0.0)
    window_start_ms = slot_start_ms
    window_end_ms = slot_end_ms
    window_source = "canonical_dispatch_slot"
    blocker: Optional[str] = None
    if action == "ECONOMIC_EXPORT":
        decision = policy_decision
        execution = (
            decision.get("execution_window")
            if isinstance(decision, dict) and isinstance(decision.get("execution_window"), dict)
            else {}
        )
        window_start_ms = _policy_ts_ms(execution.get("start_ts"))
        window_end_ms = _policy_ts_ms(execution.get("end_ts"))
        window_source = str(execution.get("source") or "")
        selected = (
            decision.get("selected_window")
            if isinstance(decision, dict) and isinstance(decision.get("selected_window"), dict)
            else {}
        )
        selected_action = str(selected.get("action") or "")
        if not (
            direct_marketing_typed_int_equals(
                execution.get("contract_version"),
                1,
            )
            and window_source == "active_plan_window"
            and selected_action
            and str(execution.get("action") or "") == selected_action
            and window_start_ms > 0
            and window_end_ms > window_start_ms
            and window_start_ms <= slot_start_ms
            and slot_end_ms <= window_end_ms
        ):
            blocker = "ECONOMIC_EXPORT_EXECUTION_WINDOW_MISSING_OR_INVALID"
    elif action == "HEADROOM_EXPORT":
        deadline_ms = int(_sf(plan.get("predump_end_ts"), 0.0) or 0.0)
        if 0 < deadline_ms < 100_000_000_000:
            deadline_ms *= 1000
        window_end_ms = deadline_ms
        window_source = "canonical_headroom_deadline"
        if deadline_ms <= slot_start_ms:
            blocker = "HEADROOM_DEADLINE_MISSING_OR_EXPIRED"
    if blocker is None and not (
        horizon_start_ms <= window_start_ms < window_end_ms <= horizon_end_ms
    ):
        blocker = "%s_WINDOW_OUTSIDE_BOUND_HORIZON" % action
    return {
        "schema_version": ACTION_HORIZON_SCHEMA,
        "action": action,
        "slot_start_ts_ms": slot_start_ms,
        "slot_end_ts_ms": slot_end_ms,
        "bound_horizon_start_ts_ms": horizon_start_ms,
        "bound_horizon_end_ts_ms": horizon_end_ms,
        "window_start_ts_ms": window_start_ms or None,
        "window_end_ts_ms": window_end_ms or None,
        "window_source": window_source or None,
        "complete": blocker is None,
        "block_reason_code": blocker,
    }


def _economic_export_start_gate(
    decision: Optional[Dict[str, Any]],
    economics: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Validiert den einmaligen DV-Startvertrag ohne neuen Entscheidungsbesitz."""

    if not direct_marketing_export_gate_contract_valid(
        decision,
        economics,
        # Eine SUSPENDED-Lineage darf hier ausschließlich als belegter,
        # wirkungsloser Policy-HOLD diagnostiziert werden. Die spätere
        # POLICY_NOT_EXECUTABLE-Kante verhindert jede Exportfreigabe.
        allowed_lineage_statuses={"ACTIVE", "SUSPENDED"},
    ):
        return None, False
    gate = decision["export_window_start_gate"]
    continuation = bool(
        decision.get("continuation_active") is True
        and decision.get("continuation_reason_code")
        == "WINDOW_START_GATES_ALREADY_SATISFIED"
    )
    return copy.deepcopy(gate), continuation


def _economic_export_profit_gate(
    plan: Dict[str, Any],
    slot: Dict[str, Any],
    action_horizon: Dict[str, Any],
    policy_decision: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Verwendet die bereits berechnete DV-Kostenzerlegung, ohne sie neu abzuziehen."""

    decision = policy_decision
    profile = str(
        decision.get("profit_profile")
        if isinstance(decision, dict)
        else "standard"
    ).strip().lower() or "standard"
    economics = decision.get("economics") if isinstance(decision, dict) and isinstance(decision.get("economics"), dict) else {}
    budget = decision.get("storage_budget") if isinstance(decision, dict) and isinstance(decision.get("storage_budget"), dict) else {}
    required_values = {
        "margin_ct_kwh": _sf(economics.get("margin_ct_kwh"), None),
        "user_min_margin_ct": _sf(economics.get("user_min_margin_ct"), None),
        "expected_profit_eur": _sf(economics.get("expected_profit_eur"), None),
        "min_window_profit_eur": _sf(economics.get("min_window_profit_eur"), None),
    }
    start_gate, start_gate_continuation = _economic_export_start_gate(
        decision,
        economics,
    )
    missing = [key for key, value in required_values.items() if value is None]
    missing.extend(
        key
        for key in ("user_min_margin_ct", "min_window_profit_eur")
        if required_values.get(key) is not None and float(required_values[key]) < 0.0
    )
    blockers: List[str] = []
    if action_horizon.get("complete") is not True:
        blockers.append(str(
            action_horizon.get("block_reason_code")
            or "ECONOMIC_EXPORT_WINDOW_OUTSIDE_BOUND_HORIZON"
        ))
    if decision is None or missing:
        blockers.append("ECONOMIC_EXPORT_PROFIT_CONTRACT_MISSING_OR_INVALID")
    else:
        if start_gate is None:
            blockers.append(
                "ECONOMIC_EXPORT_START_GATE_OR_LINEAGE_MISSING_OR_INVALID"
            )
        # Ausschließlich Diagnose einer bereits vom Policy Owner abgelehnten
        # Standard-Kante; diese Werte erzeugen keine zweite Freigabe.
        if (
            profile == "standard"
            and float(required_values["margin_ct_kwh"]) + 0.000001
            < float(required_values["user_min_margin_ct"])
        ):
            blockers.append("ECONOMIC_EXPORT_MARGIN_BELOW_USER_MINIMUM")
        if (
            profile == "standard"
            and float(required_values["expected_profit_eur"]) + 0.000001
            < float(required_values["min_window_profit_eur"])
            and not start_gate_continuation
        ):
            blockers.append(
                "ECONOMIC_EXPORT_WINDOW_PROFIT_BELOW_USER_MINIMUM"
            )
        if (
            decision.get("commands_allowed") is not True
            or bool(decision.get("blocked"))
            or str(decision.get("dv_target_state") or "").upper() != "FORCE_EXPORT"
            or (_sf(budget.get("export_budget_w"), 0.0) or 0.0) <= 0.0
        ):
            blockers.append("ECONOMIC_EXPORT_POLICY_NOT_EXECUTABLE")
    return {
        "allowed": not blockers,
        "blockers": blockers,
        "block_reason_code": blockers[0] if blockers else None,
        "margin_ct_kwh": required_values["margin_ct_kwh"],
        "user_min_margin_ct": required_values["user_min_margin_ct"],
        "expected_profit_eur": required_values["expected_profit_eur"],
        "min_window_profit_eur": required_values["min_window_profit_eur"],
        "export_window_start_gate": start_gate,
        "export_window_gate_lineage": (
            copy.deepcopy(decision.get("export_window_gate_lineage"))
            if start_gate is not None and isinstance(decision, dict)
            else None
        ),
        "window_start_gate_continuation_active": start_gate_continuation,
        "policy_commands_allowed": bool(decision and decision.get("commands_allowed") is True),
        "policy_export_budget_w": round(max(0.0, _sf(budget.get("export_budget_w"), 0.0) or 0.0), 3),
        "action_horizon_contract": copy.deepcopy(action_horizon),
        "accounting_contract": "DIRECT_MARKETING_POLICY_ECONOMICS_REUSED_NO_DOUBLE_DEDUCTION",
    }


def _path_headroom_credit(
    path: Dict[str, Any],
    deadline_index: Optional[int],
    discharge_efficiency: float,
) -> Dict[str, float]:
    if deadline_index is None:
        return {"economic_export_wh": 0.0, "other_discharge_wh": 0.0, "total_wh": 0.0}
    export_credit = 0.0
    other = 0.0
    for row in path["rows"][: deadline_index + 1]:
        transition = row["transition"]
        if transition["delta_wh"] >= 0.0:
            continue
        discharge_wh = -float(transition["delta_wh"])
        export_internal_wh = min(
            discharge_wh,
            float(transition["battery_export_wh"]) / max(0.01, discharge_efficiency),
        )
        export_credit += max(0.0, export_internal_wh)
        other += max(0.0, discharge_wh - export_internal_wh)
    return {"economic_export_wh": export_credit, "other_discharge_wh": other, "total_wh": export_credit + other}


def _action_for_row(row: Dict[str, Any], economic_row: Optional[Dict[str, Any]], forced_headroom: bool) -> Tuple[str, str]:
    transition = row["transition"]
    battery_w = float(transition["battery_w"])
    scenario = next(
        (
            item
            for item in transition["scenario_rows"]
            if item["name"] in {"p50", "deterministic_point"}
        ),
        transition["scenario_rows"][0],
    )
    if battery_w > 50.0:
        return ("PV_STORE", "SHADOW_PV_STORE") if scenario["grid_import_w"] <= 50.0 else ("GRID_CHARGE", "SHADOW_GRID_CHARGE")
    if battery_w < -50.0:
        if forced_headroom:
            return "HEADROOM_EXPORT", "RESIDUAL_HEADROOM_BEST_SLOT"
        if scenario["grid_export_w"] > 50.0 or transition["battery_export_wh"] > 10.0:
            return "ECONOMIC_EXPORT", "MARGINAL_NET_EXPORT_POSITIVE"
        return "HOUSE_SUPPLY", "ECONOMIC_HOUSE_SUPPLY"
    return "HOLD", "SHADOW_HOLD"


def _cvar(values: Iterable[float], alpha: float) -> Optional[float]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    tail_count = max(1, int(math.ceil((1.0 - alpha) * len(ordered))))
    return sum(ordered[:tail_count]) / tail_count


def optimize_shadow_dispatch(plan: Dict[str, Any], slots: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Berechnet einen deterministischen, nicht ausführbaren Vergleichsplan."""

    # Der Optimierer behandelt Slots strikt read-only. Die bisherige vollständige
    # Kopie verdoppelte die großen 96–192-Slot-Strukturen, obwohl sämtliche
    # Trajektorien und Ergebniszeilen neu aufgebaut werden.
    source_slots = [slot for slot in slots if isinstance(slot, dict)]
    if not source_slots:
        raise ShadowInputError("SLOTS_MISSING")
    capacity_wh = _capacity_wh(plan)
    params = _parameters(plan, capacity_wh)
    requested_decision_count = min(len(source_slots), params["decision_horizon_slots"])
    validated_slot_count = min(
        len(source_slots),
        requested_decision_count + params["terminal_tail_slots"],
    )
    previous_start_ms: Optional[int] = None
    for index, slot in enumerate(source_slots[:validated_slot_count]):
        start_value = _sf(slot.get("start_ts_ms"), None)
        end_value = _sf(slot.get("end_ts_ms"), None)
        start_ms = int(start_value) if start_value is not None else 0
        end_ms = int(end_value) if end_value is not None else 0
        sequence_valid = bool(
            start_ms > 0
            and end_ms - start_ms == SLOT_DURATION_MS
            and (
                previous_start_ms is None
                or start_ms == previous_start_ms + SLOT_DURATION_MS
            )
        )
        if not sequence_valid:
            raise ShadowInputError("PRICE_HORIZON_SLOT_SEQUENCE_INVALID_AT_SLOT_%d" % index)
        previous_start_ms = start_ms
    first_start_ms = int(_sf(source_slots[0].get("start_ts_ms"), 0.0) or 0.0)
    market_day = _local_market_day_contract(first_start_ms)
    required_market_day_slots = int(market_day["required_slots_to_market_day_boundary"])
    priced_prefix_count = 0
    first_price_blocker: Optional[str] = None
    for index, slot in enumerate(source_slots[:requested_decision_count]):
        price = _slot_price(slot)
        if price["buy"] is None or price["net_sell"] is None:
            first_price_blocker = "PRICE_INPUT_MISSING_AT_SLOT_%d" % index
            break
        if price.get("fresh") is False:
            first_price_blocker = "PRICE_INPUT_STALE_AT_SLOT_%d" % index
            break
        priced_prefix_count += 1
    if first_price_blocker and priced_prefix_count < required_market_day_slots:
        raise ShadowInputError(first_price_blocker or "PRICE_HORIZON_TOO_SHORT")
    decision_count = priced_prefix_count
    decision_slots = source_slots[:decision_count]
    tail_slots = []
    terminal_tail_blocker: Optional[str] = None
    if decision_count == requested_decision_count:
        for tail_index, slot in enumerate(source_slots[
            decision_count : decision_count + params["terminal_tail_slots"]
        ]):
            price = _slot_price(slot)
            if price["buy"] is None or price["net_sell"] is None:
                terminal_tail_blocker = "TERMINAL_PRICE_INPUT_MISSING_AT_SLOT_%d" % (decision_count + tail_index)
                break
            if price.get("fresh") is False:
                terminal_tail_blocker = "TERMINAL_PRICE_INPUT_STALE_AT_SLOT_%d" % (decision_count + tail_index)
                break
            tail_slots.append(slot)
    if not decision_slots:
        raise ShadowInputError("DECISION_HORIZON_TOO_SHORT")
    bound_horizon_end_ms = int(_sf(decision_slots[-1].get("end_ts_ms"), 0.0) or 0.0)
    complete_to_market_day_boundary = bool(
        decision_count >= required_market_day_slots
        and bound_horizon_end_ms >= market_day["next_local_market_day_boundary_ts_ms"]
    )
    if first_price_blocker is None and not complete_to_market_day_boundary:
        first_price_blocker = "PRICE_HORIZON_SOURCE_END_BEFORE_MARKET_DAY_BOUNDARY"
    price_horizon_contract = {
        "schema_version": PRICE_HORIZON_SCHEMA,
        "requested_decision_slots": requested_decision_count,
        "effective_decision_slots": decision_count,
        "priced_terminal_tail_slots": len(tail_slots),
        "first_unusable_slot_index": decision_count if first_price_blocker else None,
        "first_unusable_reason_code": first_price_blocker,
        "terminal_tail_first_unusable_reason_code": terminal_tail_blocker,
        "unpriced_slots_imputed": 0,
        "policy": "CONTIGUOUS_COMPLETE_TO_NEXT_LOCAL_MARKET_DAY_BOUNDARY_NO_IMPUTATION",
        "applicability_basis": "NEXT_LOCAL_MARKET_DAY_BOUNDARY",
        "slot_duration_ms": SLOT_DURATION_MS,
        **market_day,
        "bound_horizon_end_ts_ms": bound_horizon_end_ms,
        "minimum_field_activation_slots": required_market_day_slots,
        "minimum_complete_decision_slots": required_market_day_slots,
        "complete_to_next_local_market_day_boundary": complete_to_market_day_boundary,
        "field_activation_horizon_complete": complete_to_market_day_boundary,
        "rolling_24h_slots": min(decision_count, 96),
        "rolling_24h_complete": decision_count >= 96,
    }

    states = _states(capacity_wh, params["state_step_wh"])
    decision_floors, reserve_contract = _risk_floors(decision_slots, capacity_wh, params)
    reserve_contract["decision_horizon_end_ts_ms"] = bound_horizon_end_ms
    decision_ceilings = [_ceiling_wh(slot, capacity_wh) for slot in decision_slots]
    first_soc = decision_slots[0].get("soc_pct") if isinstance(decision_slots[0].get("soc_pct"), dict) else {}
    initial_soc_pct = _sf(first_soc.get("start", first_soc.get("end")), None)
    if initial_soc_pct is None:
        raise ShadowInputError("INITIAL_SOC_MISSING")
    initial_energy_wh = capacity_wh * _clamp(initial_soc_pct, 0.0, 100.0) / 100.0
    initial_hard_floor_wh = _hard_floor_wh(decision_slots[0], capacity_wh)
    if initial_energy_wh < initial_hard_floor_wh - 1.0:
        raise ShadowInputError("INITIAL_SOC_BELOW_HARD_FLOOR")
    initial_index = _closest_state_index(
        states,
        initial_energy_wh,
        initial_hard_floor_wh,
        decision_ceilings[0],
    )
    initial_energy_wh = states[initial_index]
    state_step_wh = max(1.0, float(params["state_step_wh"]))
    physical_slot_state_gain_wh = (
        params["max_charge_w"]
        * SLOT_HOURS
        * params["charge_efficiency"]
    )
    maximum_slot_state_gain_wh = math.floor(
        (physical_slot_state_gain_wh + 1.0e-6) / state_step_wh
    ) * state_step_wh
    reachability_clamped_slots = 0
    for index, floor_wh in enumerate(list(decision_floors)):
        hard_floor_wh = _hard_floor_wh(decision_slots[index], capacity_wh)
        reachable_wh = min(capacity_wh, initial_energy_wh + (index + 1) * maximum_slot_state_gain_wh)
        reachable_wh = math.floor((reachable_wh + 1.0) / state_step_wh) * state_step_wh
        reachable_floor_wh = max(hard_floor_wh, min(float(floor_wh), reachable_wh))
        if reachable_floor_wh < float(floor_wh) - 1.0:
            reachability_clamped_slots += 1
        decision_floors[index] = reachable_floor_wh
    reserve_contract["reachability_policy"] = "RISK_FLOOR_CLAMPED_TO_PHYSICAL_CHARGE_REACHABILITY_HARD_FLOOR_UNCHANGED"
    reserve_contract["reachability_clamped_slots"] = reachability_clamped_slots
    reserve_contract["lattice_charge_gain_per_slot_wh"] = round(
        maximum_slot_state_gain_wh,
        3,
    )
    curve_target = _curve_target_contract(
        plan,
        decision_slots,
        capacity_wh,
        initial_energy_wh,
        params,
    )
    # Die Legacy-Ladekurve ist nur Baseline. Reserve, Lastdeckung und
    # Wiederauflade-Risiko stammen ausschließlich aus kanonischen Verträgen.
    curve_deadline_index = None
    curve_minimum_energy_wh = None

    salvage_values, salvage_contract = _terminal_salvage(tail_slots or decision_slots, states, params)
    terminal_values = salvage_values
    tail_contract: Dict[str, Any]
    if tail_slots:
        tail_floors, _ = _risk_floors(tail_slots, capacity_wh, params)
        tail_ceilings = [_ceiling_wh(slot, capacity_wh) for slot in tail_slots]
        terminal_values, _ = _solve_backward(
            tail_slots,
            states,
            tail_floors,
            tail_ceilings,
            params,
            capacity_wh,
            salvage_values,
            keep_choices=False,
        )
        tail_contract = {"method": "dp_continuation_value_v1", "fallback_reason_code": None}
    else:
        tail_contract = salvage_contract

    economic_values, economic_choices = _solve_backward(
        decision_slots,
        states,
        decision_floors,
        decision_ceilings,
        params,
        capacity_wh,
        terminal_values,
        minimum_deadline_index=curve_deadline_index,
        minimum_deadline_energy_wh=curve_minimum_energy_wh,
    )
    if economic_values[initial_index][MODES.index(0)] <= NEG_INF / 2:
        diagnostic = _forward_infeasibility_diagnostic(
            decision_slots,
            states,
            decision_floors,
            decision_ceilings,
            params,
            capacity_wh,
            initial_index,
        )
        diagnostic["failure_class"] = "ECONOMIC_BACKWARD_DP_NO_INITIAL_VALUE"
        raise ShadowInputError("ECONOMIC_DP_INFEASIBLE", diagnostic)
    economic_path = _trace_path(
        decision_slots,
        states,
        economic_choices,
        initial_index,
        params,
        capacity_wh,
    )

    headroom = _headroom_contract(plan, decision_slots)
    deadline_index = _deadline_index(decision_slots, int(headroom["deadline_ts_ms"] or 0))
    credit = _path_headroom_credit(economic_path, deadline_index, params["discharge_efficiency"])
    residual_wh = max(0.0, float(headroom["required_wh"]) - credit["total_wh"])
    selected_path = economic_path
    economic_objective_ct = float(economic_values[initial_index][MODES.index(0)])
    selected_objective_ct = economic_objective_ct
    constraint_target_wh = None
    headroom_infeasible_wh = 0.0
    if residual_wh > params["state_step_wh"] * 0.25 and deadline_index is not None:
        hard_target = initial_energy_wh - float(headroom["required_wh"])
        minimum_at_deadline = decision_floors[deadline_index]
        constraint_target_wh = max(minimum_at_deadline, hard_target)
        headroom_infeasible_wh = max(0.0, minimum_at_deadline - hard_target)
        forced_values, forced_choices = _solve_backward(
            decision_slots,
            states,
            decision_floors,
            decision_ceilings,
            params,
            capacity_wh,
            terminal_values,
            deadline_index=deadline_index,
            deadline_max_energy_wh=constraint_target_wh,
            minimum_deadline_index=curve_deadline_index,
            minimum_deadline_energy_wh=curve_minimum_energy_wh,
        )
        if forced_values[initial_index][MODES.index(0)] > NEG_INF / 2:
            selected_objective_ct = float(forced_values[initial_index][MODES.index(0)])
            selected_path = _trace_path(
                decision_slots,
                states,
                forced_choices,
                initial_index,
                params,
                capacity_wh,
            )

    selected_credit = _path_headroom_credit(selected_path, deadline_index, params["discharge_efficiency"])
    residual_after_wh = max(0.0, float(headroom["required_wh"]) - selected_credit["total_wh"])
    residual_opportunity_cost_ct = max(0.0, economic_objective_ct - selected_objective_ct)
    economic_curtailment_wh = sum(
        float(row["transition"]["expected_curtailment_wh"])
        for row in economic_path["rows"]
    )
    selected_curtailment_wh = sum(
        float(row["transition"]["expected_curtailment_wh"])
        for row in selected_path["rows"]
    )
    residual_avoided_curtailment_wh = max(0.0, economic_curtailment_wh - selected_curtailment_wh)
    rows = []
    previous_power_w = 0.0
    headroom_extra_remaining = max(0.0, selected_credit["total_wh"] - credit["total_wh"])
    total_net_ct = 0.0
    total_baseline_net_ct = 0.0
    total_incremental_net_ct = 0.0
    total_throughput_wh = 0.0
    total_curtailment_wh = 0.0
    total_avoided_curtailment_wh = 0.0
    action_switches = 0
    previous_action = "HOLD"
    for index, row in enumerate(selected_path["rows"]):
        economic_row = economic_path["rows"][index] if index < len(economic_path["rows"]) else None
        transition = row["transition"]
        extra_discharge_wh = 0.0
        if economic_row is not None:
            extra_discharge_wh = max(
                0.0,
                -float(transition["delta_wh"]) - max(0.0, -float(economic_row["transition"]["delta_wh"])),
            )
        forced_headroom = bool(
            deadline_index is not None
            and index <= deadline_index
            and extra_discharge_wh > params["state_step_wh"] * 0.25
            and headroom_extra_remaining > 0.0
        )
        action, reason = _action_for_row(row, economic_row, forced_headroom)
        if forced_headroom:
            headroom_extra_remaining = max(0.0, headroom_extra_remaining - extra_discharge_wh)
        if action != previous_action:
            action_switches += 1
        previous_action = action
        energy_start = float(row["energy_start_wh"])
        energy_end = float(row["energy_end_wh"])
        opportunity_cost = 0.0
        if economic_row is not None and action == "HEADROOM_EXPORT":
            opportunity_cost = max(
                0.0,
                float(economic_row["transition"]["immediate_net_ct"]) - float(transition["immediate_net_ct"]),
            )
        avoided_value = float(transition["avoided_curtailment_wh"]) / 1000.0 * max(
            0.0, float(transition["prices"]["net_sell"] or 0.0)
        )
        target_shortfall_avoided_wh = 0.0
        if (
            curve_deadline_index is not None
            and curve_minimum_energy_wh is not None
            and index <= curve_deadline_index
        ):
            target_shortfall_avoided_wh = max(
                0.0,
                max(0.0, curve_minimum_energy_wh - energy_start)
                - max(0.0, curve_minimum_energy_wh - energy_end),
            )
        net_value = float(transition["immediate_net_ct"]) - opportunity_cost
        choice_diagnostic = (
            row.get("choice_diagnostic")
            if isinstance(row.get("choice_diagnostic"), dict)
            else {}
        )
        negative_incremental_explanation = None
        incremental_net_ct = float(transition["incremental_net_ct"])
        if action != "HOLD" and incremental_net_ct < -1.0e-9:
            hold_available = choice_diagnostic.get("hold_available") is True
            risk_floor_gap_before_wh = max(0.0, decision_floors[index] - energy_start)
            if forced_headroom:
                explanation_reason = "PHYSICAL_HEADROOM_CONSTRAINT_WITH_EXPLICIT_OPPORTUNITY_COST"
            elif not hold_available and risk_floor_gap_before_wh > 1.0:
                explanation_reason = "CURRENT_RISK_FLOOR_REQUIRES_ENERGY_INCREASE"
            elif not hold_available:
                explanation_reason = "FUTURE_CANONICAL_CONSTRAINT_MAKES_HOLD_PATH_INFEASIBLE"
            else:
                explanation_reason = "DP_SUFFIX_VALUE_OUTWEIGHS_IMMEDIATE_LOSS"
            negative_incremental_explanation = {
                "status": "EXPLAINED_BY_GLOBAL_OBJECTIVE_OR_CANONICAL_CONSTRAINT",
                "reason_code": explanation_reason,
                "immediate_incremental_value_ct": round(incremental_net_ct, 6),
                "hold_counterfactual_available": hold_available,
                "dp_score_margin_vs_hold_ct": (
                    round(float(choice_diagnostic["score_margin_vs_hold_ct"]), 6)
                    if choice_diagnostic.get("score_margin_vs_hold_ct") is not None
                    else None
                ),
                "immediate_delta_vs_hold_ct": (
                    round(float(choice_diagnostic["immediate_delta_vs_hold_ct"]), 6)
                    if choice_diagnostic.get("immediate_delta_vs_hold_ct") is not None
                    else None
                ),
                "continuation_uplift_vs_hold_ct": (
                    round(float(choice_diagnostic["continuation_uplift_vs_hold_ct"]), 6)
                    if choice_diagnostic.get("continuation_uplift_vs_hold_ct") is not None
                    else None
                ),
                "risk_floor_gap_before_wh": round(risk_floor_gap_before_wh, 3),
                "continuation_scope": "REMAINING_DECISION_HORIZON_PLUS_TERMINAL_TAIL",
                "later_economic_export_slots": 0,
                "later_house_supply_slots": 0,
            }
        total_net_ct += net_value
        total_baseline_net_ct += float(transition["baseline_net_ct"])
        total_incremental_net_ct += float(transition["incremental_net_ct"])
        total_throughput_wh += abs(float(transition["delta_wh"]))
        total_curtailment_wh += float(transition["expected_curtailment_wh"])
        total_avoided_curtailment_wh += float(transition["avoided_curtailment_wh"])
        slot = row["slot"]
        projection = slot.get("projection") if isinstance(slot.get("projection"), dict) else {}
        reference_scenario = next(
            (
                scenario
                for scenario in transition["scenario_rows"]
                if scenario["name"] in {"p50", "deterministic_point"}
            ),
            transition["scenario_rows"][0],
        )
        shadow_candidate = action != "HOLD"
        policy_decision = _policy_decision_for_slot(plan, slot) if action == "ECONOMIC_EXPORT" else None
        action_horizon = _action_horizon_contract(
            plan,
            slot,
            action,
            int(_sf(decision_slots[0].get("start_ts_ms"), 0.0) or 0.0),
            bound_horizon_end_ms,
            policy_decision,
        )
        profit_gate = (
            _economic_export_profit_gate(plan, slot, action_horizon, policy_decision)
            if action == "ECONOMIC_EXPORT"
            else {
                "allowed": action_horizon.get("complete") is True,
                "blockers": (
                    []
                    if action_horizon.get("complete") is True
                    else [str(action_horizon.get("block_reason_code") or "ACTION_WINDOW_OUTSIDE_BOUND_HORIZON")]
                ),
                "block_reason_code": action_horizon.get("block_reason_code"),
                "action_horizon_contract": copy.deepcopy(action_horizon),
                "accounting_contract": "NOT_ECONOMIC_EXPORT",
            }
        )
        headroom_gate = {
            "allowed": bool(
                action != "HEADROOM_EXPORT"
                or (
                    headroom.get("physical_headroom_justified") is True
                    and extra_discharge_wh > 0.0
                    and headroom.get("deadline_ts_ms") is not None
                )
            ),
            "block_reason_code": (
                None
                if action != "HEADROOM_EXPORT"
                or (
                    headroom.get("physical_headroom_justified") is True
                    and extra_discharge_wh > 0.0
                    and headroom.get("deadline_ts_ms") is not None
                )
                else str(headroom.get("block_reason_code") or "HEADROOM_PRESSURE_NOT_CAUSALLY_BOUND")
            ),
            "physical_headroom_justified": bool(headroom.get("physical_headroom_justified")),
        }
        shadow_selected = bool(
            shadow_candidate
            and profit_gate.get("allowed") is True
            and headroom_gate.get("allowed") is True
        )
        candidate_block_reason = (
            profit_gate.get("block_reason_code")
            or headroom_gate.get("block_reason_code")
        )
        rows.append({
            "slot_id": slot.get("slot_id"),
            "start_ts_ms": slot.get("start_ts_ms"),
            "end_ts_ms": slot.get("end_ts_ms"),
            "planned_action": action,
            "reason_code": reason,
            "shadow_only": True,
            "commands_allowed": False,
            "candidate": shadow_candidate,
            "selected": shadow_selected,
            "selection_scope": "SHADOW_COMPARISON_ONLY",
            "executable": False,
            "requested": False,
            "acknowledged": False,
            "readback_confirmed": False,
            "block_reason_code": (
                "SHADOW_ONLY_NOT_RUNTIME_AUTHORIZED"
                if shadow_selected
                else candidate_block_reason
                if shadow_candidate
                else "NO_STORAGE_ACTION_CANDIDATE"
            ),
            "battery_w": round(float(transition["battery_w"]), 3),
            "candidate_power_w": round(abs(float(transition["battery_w"])), 3) if shadow_candidate else 0.0,
            "selected_power_w": round(abs(float(transition["battery_w"])), 3) if shadow_selected else 0.0,
            "economic_export_gate": profit_gate,
            "headroom_gate": headroom_gate,
            "action_horizon_contract": action_horizon,
            "soc_start_pct": round(energy_start / capacity_wh * 100.0, 3),
            "soc_end_pct": round(energy_end / capacity_wh * 100.0, 3),
            "hard_floor_pct": round(_hard_floor_wh(slot, capacity_wh) / capacity_wh * 100.0, 3),
            "risk_floor_pct": round(decision_floors[index] / capacity_wh * 100.0, 3),
            "ceiling_pct": round(decision_ceilings[index] / capacity_wh * 100.0, 3),
            "headroom": {
                "required_wh": round(float(headroom["required_wh"]), 3),
                "credited_economic_export_wh": round(credit["economic_export_wh"], 3),
                "credited_other_discharge_wh": round(credit["other_discharge_wh"], 3),
                "credited_residual_headroom_wh": round(
                    max(0.0, selected_credit["total_wh"] - credit["total_wh"]),
                    3,
                ),
                "residual_wh": round(residual_after_wh, 3),
                "deadline_ts_ms": headroom["deadline_ts_ms"],
                "additional_forced_in_slot_wh": round(extra_discharge_wh if forced_headroom else 0.0, 3),
                "physical_headroom_justified": bool(headroom.get("physical_headroom_justified")),
                "pressure_block_reason_code": headroom.get("block_reason_code"),
            },
            "economics_ct": {
                "revenue": round(float(transition["revenue_ct"]), 6),
                "import_cost": round(float(transition["import_cost_ct"]), 6),
                "opportunity_cost": round(opportunity_cost, 6),
                "efficiency_loss_cost": round(float(transition["efficiency_loss_cost_ct"]), 6),
                "degradation_cost": round(float(transition["degradation_cost_ct"]), 6),
                "avoided_curtailment_value": round(avoided_value, 6),
                "risk_margin": round(float(transition["risk_margin_ct"]), 6),
                "switching_cost": round(float(transition["switching_cost_ct"]), 6),
                "net_value": round(net_value, 6),
                "baseline_net_value": round(float(transition["baseline_net_ct"]), 6),
                "incremental_net_value": round(float(transition["incremental_net_ct"]), 6),
            },
            "negative_incremental_explanation": negative_incremental_explanation,
            "scenario_contract": transition["forecast_contract"],
            "scenarios": transition["scenario_rows"],
            "baseline_delta": {
                "battery_w": round(
                    float(transition["battery_w"])
                    - float(projection.get("battery_w") or 0.0),
                    3,
                ),
                "baseline_action": slot.get("planned_action"),
                "explanation": "Shadowvergleich; keine Manager- oder Hardwarewirkung.",
            },
            "legacy_curve_baseline": {
                "owner": "legacy_curve_projection",
                "action": slot.get("planned_action"),
                "battery_w": round(float(projection.get("battery_w") or 0.0), 3),
                "target_soc_pct": projection.get("target_soc_pct"),
                "source": "canonical_legacy_adapter",
            },
            "canonical_counterdecision": {
                "action": action,
                "battery_w": round(float(transition["battery_w"]), 3),
                "candidate": shadow_candidate,
                "selected": shadow_candidate,
                "executable": False,
                "reason_code": reason,
                "marginal": {
                    "current_net_sell_ct_kwh": transition["prices"]["net_sell"],
                    "later_min_net_sell_ct_kwh_before_target": curve_target.get("later_min_net_sell_ct_kwh"),
                    "pv_surplus_export_w_point": round(
                        float(reference_scenario["grid_export_w"]),
                        3,
                    ),
                    "pv_surplus_export_w_p50": (
                        round(
                            float(reference_scenario["grid_export_w"]),
                            3,
                        )
                        if reference_scenario.get("name") == "p50"
                        else None
                    ),
                    "efficiency_loss_cost_ct": round(float(transition["efficiency_loss_cost_ct"]), 6),
                    "degradation_cost_ct": round(float(transition["degradation_cost_ct"]), 6),
                    "risk_margin_ct": round(float(transition["risk_margin_ct"]), 6),
                    "switching_cost_ct": round(float(transition["switching_cost_ct"]), 6),
                    "incremental_net_value_ct": round(float(transition["incremental_net_ct"]), 6),
                    "target_shortfall_avoided_wh": round(target_shortfall_avoided_wh, 3),
                },
                "target_value_status": "LEXICOGRAPHIC_ENERGY_FULFILLMENT_NOT_MONETIZED",
            },
            "ramp_delta_w": round(float(transition["battery_w"]) - previous_power_w, 3),
        })
        previous_power_w = float(transition["battery_w"])

    later_economic_export_slots = 0
    later_house_supply_slots = 0
    later_unselected_modeled_action_count = 0
    later_unselected_reason_types: set[str] = set()
    for item in reversed(rows):
        explanation = item.get("negative_incremental_explanation")
        if isinstance(explanation, dict):
            explanation["later_economic_export_slots"] = later_economic_export_slots
            explanation["later_house_supply_slots"] = later_house_supply_slots
            explanation["later_unselected_modeled_action_count"] = (
                later_unselected_modeled_action_count
            )
            explanation["later_unselected_reason_types"] = sorted(
                later_unselected_reason_types
            )
            if (
                explanation.get("reason_code")
                == "DP_SUFFIX_VALUE_OUTWEIGHS_IMMEDIATE_LOSS"
                and later_unselected_modeled_action_count
            ):
                explanation["status"] = "EVIDENCE_LIMIT_UNSELECTED_MODELED_ACTIONS"
                explanation["reason_code"] = "MODEL_SUFFIX_DEPENDS_ON_UNSELECTED_ACTIONS"
        if item.get("planned_action") == "ECONOMIC_EXPORT":
            later_economic_export_slots += 1
        if item.get("planned_action") == "HOUSE_SUPPLY":
            later_house_supply_slots += 1
        if item.get("candidate") is True and item.get("selected") is not True:
            later_unselected_modeled_action_count += 1
            later_unselected_reason_types.add(
                str(item.get("block_reason_code") or "UNSPECIFIED_ACTION_CONTRACT")
            )

    scenario_totals = selected_path["scenario_totals_ct"]
    terminal_energy_values = []
    for index in range(len(states)):
        value = max(terminal_values[index]) if terminal_values[index] else NEG_INF
        terminal_energy_values.append(None if value <= NEG_INF / 2 else float(value))
    marginal_prices = []
    for left, right, left_value, right_value in zip(states, states[1:], terminal_energy_values, terminal_energy_values[1:]):
        delta_kwh = max(0.001, (right - left) / 1000.0)
        marginal_prices.append(
            round((right_value - left_value) / delta_kwh, 6)
            if left_value is not None and right_value is not None
            else None
        )
    finite_terminal_values = [value for value in terminal_energy_values if value is not None]
    if not finite_terminal_values:
        raise ShadowInputError("TERMINAL_VALUE_INFEASIBLE")

    selected_immediate_objective_ct = sum(
        float(path_row["transition"]["immediate_net_ct"])
        for path_row in selected_path["rows"]
    )
    selected_final_state_index = int(selected_path["final_state_index"])
    selected_final_mode_index = int(selected_path["final_mode_index"])
    selected_terminal_continuation_ct = float(
        terminal_values[selected_final_state_index][selected_final_mode_index]
    )
    selected_objective_identity_residual_ct = (
        selected_objective_ct
        - selected_immediate_objective_ct
        - selected_terminal_continuation_ct
    )
    baseline_hold_immediate_ct = sum(
        _hold_immediate_from_transition(path_row["transition"], params)
        for path_row in selected_path["rows"]
    )
    baseline_hold_blockers: List[str] = []
    for index, slot in enumerate(decision_slots):
        slot_forecast = _slot_forecast(slot)
        if (
            params["grid_import_limit_w"] > 0.0
            and any(
                max(0.0, float(load_w) - float(pv_w))
                > params["grid_import_limit_w"] + 1.0
                for _name, pv_w, load_w, _weight
                in slot_forecast["scenarios"]
            )
        ):
            baseline_hold_blockers.append(
                "GRID_IMPORT_LIMIT_AT_SLOT_%d" % index
            )
        if initial_energy_wh < decision_floors[index] - 1.0:
            baseline_hold_blockers.append("RISK_FLOOR_AT_SLOT_%d" % index)
        if initial_energy_wh > decision_ceilings[index] + 1.0:
            baseline_hold_blockers.append("ENERGY_CEILING_AT_SLOT_%d" % index)
        if (
            deadline_index == index
            and constraint_target_wh is not None
            and initial_energy_wh > constraint_target_wh + 1.0
        ):
            baseline_hold_blockers.append("HEADROOM_DEADLINE_AT_SLOT_%d" % index)
        if (
            curve_deadline_index == index
            and curve_minimum_energy_wh is not None
            and initial_energy_wh < curve_minimum_energy_wh - 1.0
        ):
            baseline_hold_blockers.append("CURVE_MINIMUM_DEADLINE_AT_SLOT_%d" % index)
    baseline_terminal_value = terminal_values[initial_index][MODES.index(0)]
    baseline_terminal_defined = baseline_terminal_value > NEG_INF / 2
    if not baseline_terminal_defined:
        baseline_hold_blockers.append("TERMINAL_CONTINUATION_UNDEFINED")
    baseline_hold_defined = bool(baseline_terminal_defined)
    baseline_hold_feasible = bool(
        baseline_hold_defined and not baseline_hold_blockers
    )
    baseline_hold_total_ct = (
        baseline_hold_immediate_ct + float(baseline_terminal_value)
        if baseline_hold_defined
        else None
    )
    incremental_vs_hold_counterfactual_ct = (
        selected_objective_ct - baseline_hold_total_ct
        if baseline_hold_total_ct is not None
        else None
    )
    model_incremental_objective_ct = (
        incremental_vs_hold_counterfactual_ct
        if baseline_hold_feasible
        else None
    )
    modeled_action_rows = [
        item for item in rows
        if item.get("candidate") is True
    ]
    unselected_modeled_action_rows = [
        item for item in modeled_action_rows
        if item.get("selected") is not True
    ]
    unselected_reason_types = sorted({
        str(item.get("block_reason_code") or "UNSPECIFIED_ACTION_CONTRACT")
        for item in unselected_modeled_action_rows
    })
    unselected_action_types = sorted({
        str(item.get("planned_action") or "UNKNOWN")
        for item in unselected_modeled_action_rows
    })
    decision_path_action_contract_complete = not unselected_modeled_action_rows
    terminal_tail_action_contract_verified = False
    if unselected_modeled_action_rows:
        objective_realizability_status = "EVIDENCE_LIMIT_UNSELECTED_MODELED_ACTIONS"
    elif tail_slots:
        objective_realizability_status = "MODEL_ONLY_TERMINAL_TAIL_ACTION_CONTRACT_NOT_VERIFIED"
    else:
        objective_realizability_status = "MODEL_ONLY_TERMINAL_SALVAGE_NOT_RUNTIME_AUTHORIZED"
    if not baseline_hold_defined:
        objective_comparison_status = "BASELINE_TERMINAL_OR_TRANSITION_UNDEFINED"
    elif not baseline_hold_feasible:
        objective_comparison_status = "COUNTERFACTUAL_ONLY_BASELINE_VIOLATES_CANONICAL_CONSTRAINTS"
    elif unselected_modeled_action_rows:
        objective_comparison_status = "EVIDENCE_LIMIT_UNSELECTED_MODELED_ACTIONS"
    else:
        objective_comparison_status = (
            "MODEL_ONLY_FEASIBLE_STATIC_DECISION_HORIZON_HOLD_BASELINE"
        )
    baseline_blocker_types = sorted({
        blocker.split("_AT_SLOT_", 1)[0]
        for blocker in baseline_hold_blockers
    })

    objective_accounting = {
        "schema_version": "storage_dispatch_objective_accounting_v1",
        "shadow": {
            "immediate_value_ct": round(selected_immediate_objective_ct, 6),
            "terminal_continuation_value_ct": round(
                selected_terminal_continuation_ct,
                6,
            ),
            "total_objective_value_ct": round(selected_objective_ct, 6),
            "identity": "DIRECT_TERMINAL_LOOKUP_PLUS_IMMEDIATE_EQUALS_DP_OBJECTIVE",
            "identity_residual_ct": round(selected_objective_identity_residual_ct, 9),
            "headroom_opportunity_cost_diagnostic_ct": round(
                sum(
                    float(item["economics_ct"]["opportunity_cost"])
                    for item in rows
                ),
                6,
            ),
            "headroom_opportunity_cost_additional_objective_subtraction": False,
        },
        "baseline": {
            "kind": "STATIC_HOLD_DURING_DECISION_HORIZON_WITH_SAME_TERMINAL_VALUE_MODEL",
            "decision_horizon_battery_dispatch": "HOLD",
            "terminal_tail_scope": "MODEL_CONTINUATION",
            "productive_legacy_plan_comparison": False,
            "defined": baseline_hold_defined,
            "feasible_under_canonical_constraints": baseline_hold_feasible,
            "immediate_value_ct": (
                round(baseline_hold_immediate_ct, 6)
                if baseline_hold_defined
                else None
            ),
            "terminal_continuation_value_ct": (
                round(float(baseline_terminal_value), 6)
                if baseline_terminal_defined
                else None
            ),
            "total_objective_value_ct": (
                round(baseline_hold_total_ct, 6)
                if baseline_hold_total_ct is not None
                else None
            ),
            "constraint_blocker_count": len(set(baseline_hold_blockers)),
            "constraint_blocker_types": baseline_blocker_types,
            "first_constraint_blocker": (
                baseline_hold_blockers[0]
                if baseline_hold_blockers
                else None
            ),
        },
        "comparison": {
            "status": objective_comparison_status,
            "incremental_objective_value_ct": None,
            "model_incremental_objective_value_ct": (
                round(model_incremental_objective_ct, 6)
                if model_incremental_objective_ct is not None
                else None
            ),
            "incremental_vs_hold_counterfactual_ct": (
                round(incremental_vs_hold_counterfactual_ct, 6)
                if incremental_vs_hold_counterfactual_ct is not None
                else None
            ),
            "claim_scope": "MODEL_ONLY_NO_PRODUCTIVE_OR_EXECUTABLE_SUPERIORITY_CLAIM",
        },
        "objective_realizability": {
            "status": objective_realizability_status,
            "decision_path_modeled_action_count": len(modeled_action_rows),
            "decision_path_selected_action_count": (
                len(modeled_action_rows) - len(unselected_modeled_action_rows)
            ),
            "decision_path_unselected_action_count": len(unselected_modeled_action_rows),
            "decision_path_unselected_action_types": unselected_action_types,
            "decision_path_unselected_reason_types": unselected_reason_types,
            "decision_path_action_contract_complete": decision_path_action_contract_complete,
            "terminal_tail_action_contract_verified": terminal_tail_action_contract_verified,
            "terminal_value_scope": (
                "DP_CONTINUATION_MODEL_WITHOUT_RUNTIME_PROFIT_OR_EXECUTION_GATE_PROOF"
                if tail_slots
                else "CONSERVATIVE_SALVAGE_MODEL_NOT_RUNTIME_AUTHORIZED"
            ),
        },
        "target_value_contract": {
            "legacy_curve_target_monetized": False,
            "legacy_curve_target_value_ct": None,
            "legacy_curve_role": "OBSERVATION_BASELINE_ONLY",
            "canonical_risk_floor_role": "LEXICOGRAPHIC_CONSTRAINT_NOT_MONETIZED",
            "headroom_role": (
                "LEXICOGRAPHIC_CONSTRAINT_WITH_OPPORTUNITY_COST_DIAGNOSTIC"
                if constraint_target_wh is not None
                else "NOT_BINDING"
            ),
        },
    }

    curve_selected_energy_wh = None
    if curve_deadline_index is not None:
        curve_selected_energy_wh = float(selected_path["rows"][curve_deadline_index]["energy_end_wh"])
    curve_target_result = {
        **curve_target,
        "selected_energy_at_deadline_wh": (
            round(curve_selected_energy_wh, 3)
            if curve_selected_energy_wh is not None
            else None
        ),
        "selected_target_met": (
            bool(
                curve_target.get("target_energy_wh") is not None
                and curve_selected_energy_wh is not None
                and curve_selected_energy_wh >= float(curve_target["target_energy_wh"]) - 1.0
            )
            if curve_deadline_index is not None
            else None
        ),
        "selected_constraint_met": (
            bool(
                curve_minimum_energy_wh is not None
                and curve_selected_energy_wh is not None
                and curve_selected_energy_wh >= curve_minimum_energy_wh - 1.0
            )
            if curve_deadline_index is not None
            else None
        ),
        "binding_within_state_step": (
            bool(
                curve_minimum_energy_wh is not None
                and curve_selected_energy_wh is not None
                and abs(curve_selected_energy_wh - curve_minimum_energy_wh) <= params["state_step_wh"] + 1.0
            )
            if curve_deadline_index is not None
            else None
        ),
    }
    result = {
        "schema_version": SHADOW_SCHEMA,
        "status": "SHADOW_OK" if residual_after_wh <= params["state_step_wh"] + headroom_infeasible_wh else "SHADOW_HEADROOM_PARTIAL",
        "shadow_only": True,
        "commands_allowed": False,
        "owner": "storage_dispatch_shadow",
        "historical_claim_status": "FORWARD_SHADOW_ONLY_NO_RETROACTIVE_FIELD_FAULT_CLAIM",
        "owner_contract": {
            "economic_decision_owner": "storage_dispatch_shadow",
            "legacy_curve_role": "OBSERVATION_BASELINE_ONLY",
            "independent_curve_economic_dispatch_allowed": False,
            "runtime_hardware_owner": "storage_manager",
            "shadow_hardware_effect": False,
        },
        "algorithm": ALGORITHM,
        "algorithm_version": 1,
        "runtime_ms": 0.0,
        "runtime_measurement": "external_benchmark_only_to_keep_plan_deterministic",
        "parameter_revision": _hash(params),
        "parameters": params,
        "decision_horizon": {
            "slots": len(decision_slots),
            "requested_slots": requested_decision_count,
            "start_ts_ms": decision_slots[0].get("start_ts_ms"),
            "end_ts_ms": decision_slots[-1].get("end_ts_ms"),
            "timezone": MARKET_TIMEZONE,
            "only_first_slot_theoretically_selectable": True,
            "runtime_selection_authorized": False,
        },
        "price_horizon_contract": price_horizon_contract,
        "terminal_value": {
            **tail_contract,
            "decision_horizon_end_ts_ms": decision_slots[-1].get("end_ts_ms"),
            "tail_horizon_end_ts_ms": tail_slots[-1].get("end_ts_ms") if tail_slots else decision_slots[-1].get("end_ts_ms"),
            "tail_slots": len(tail_slots),
            "soc_grid_wh": [round(value, 3) for value in states],
            "value_ct_by_soc": [round(value, 6) if value is not None else None for value in terminal_energy_values],
            "marginal_shadow_price_ct_kwh_by_soc": marginal_prices,
            "lower_value_ct": round(min(finite_terminal_values), 6),
            "expected_value_ct": round(sum(finite_terminal_values) / len(finite_terminal_values), 6),
            "upper_value_ct": round(max(finite_terminal_values), 6),
            "risk_method": reserve_contract["method"],
            "fresh": True,
            "horizon_binding": "ACTUAL_CONTIGUOUS_PRICE_HORIZON_END",
        },
        "reserve_contract": reserve_contract,
        "curve_target_contract": curve_target_result,
        "objective_accounting": objective_accounting,
        "headroom_summary": {
            "required_wh": round(float(headroom["required_wh"]), 3),
            "raw_required_wh": round(float(headroom["raw_required_wh"]), 3),
            "deadline_ts_ms": headroom["deadline_ts_ms"],
            "economic_export_credit_wh": round(credit["economic_export_wh"], 3),
            "other_discharge_credit_wh": round(credit["other_discharge_wh"], 3),
            "residual_headroom_export_wh": round(
                max(0.0, selected_credit["total_wh"] - credit["total_wh"]),
                3,
            ),
            "selected_total_headroom_wh": round(selected_credit["total_wh"], 3),
            "residual_before_forced_wh": round(residual_wh, 3),
            "residual_after_wh": round(residual_after_wh, 3),
            "deadline_constraint_target_wh": round(constraint_target_wh, 3) if constraint_target_wh is not None else None,
            "infeasible_due_hard_floor_wh": round(headroom_infeasible_wh, 3),
            "preventable_clipping_reference_wh": round(
                float(headroom["preventable_clipping_reference_wh"]),
                3,
            ),
            "reference_source": headroom["reference_source"],
            "physical_headroom_justified": bool(headroom.get("physical_headroom_justified")),
            "pressure_block_reason_code": headroom.get("block_reason_code"),
            "residual_opportunity_cost_ct": round(residual_opportunity_cost_ct, 6),
            "modeled_avoided_curtailment_delta_wh": round(residual_avoided_curtailment_wh, 3),
            "valuation_status": "PHYSICAL_ORACLE_WITH_EXPLICIT_OPPORTUNITY_COST_NO_INVENTED_CURTAILMENT_PRICE",
        },
        "metrics": {
            "net_value_ct_excluding_terminal": round(total_net_ct, 6),
            "baseline_net_value_ct": round(total_baseline_net_ct, 6),
            "incremental_net_value_ct_excluding_terminal": round(total_incremental_net_ct, 6),
            "accounting_contract": {
                "net_value_ct_excluding_terminal_scope": "LEGACY_SLOT_DIAGNOSTIC",
                "net_value_ct_excluding_terminal_is_dp_objective": False,
                "net_value_ct_excluding_terminal_formula": (
                    "SUM(IMMEDIATE_NET_CT_MINUS_HEADROOM_OPPORTUNITY_COST_DIAGNOSTIC_CT)"
                ),
                "includes_additional_headroom_opportunity_cost_subtraction": True,
                "canonical_dp_objective_path": (
                    "objective_accounting.shadow.total_objective_value_ct"
                ),
            },
            "battery_throughput_wh": round(total_throughput_wh, 3),
            "equivalent_full_cycles": round(total_throughput_wh / max(1.0, 2.0 * capacity_wh), 6),
            "expected_curtailment_wh": round(total_curtailment_wh, 3),
            "avoided_curtailment_wh": round(total_avoided_curtailment_wh, 3),
            "action_switches": action_switches,
            "simultaneous_charge_discharge_slots": 0,
            "hard_constraint_violations": 0,
            "scenario_total_net_before_internal_cost_ct": {key: round(value, 6) for key, value in sorted(scenario_totals.items())},
            "cvar_ct": round(_cvar(scenario_totals.values(), params["cvar_alpha"]) or 0.0, 6),
        },
        "slots": rows,
        "fallback": False,
        "fallback_reason_code": None,
    }
    result["shadow_plan_id"] = _hash(_deterministic_result_material(result))
    return result


def shadow_fallback(
    reason_code: str,
    runtime_ms: float = 0.0,
    diagnostic: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Sichtbarer Fallback ohne Kandidat, Budget oder Ausführungswirkung."""

    result = {
        "schema_version": SHADOW_SCHEMA,
        "status": "SHADOW_FALLBACK_BASELINE",
        "shadow_only": True,
        "commands_allowed": False,
        "owner": "storage_dispatch_shadow",
        "algorithm": ALGORITHM,
        "runtime_ms": round(max(0.0, runtime_ms), 3),
        "historical_claim_status": "NO_RETROACTIVE_FAULT_CLAIM_WITHOUT_COMPLETE_INPUT_PREIMAGE",
        "owner_contract": {
            "economic_decision_owner": "storage_dispatch_shadow",
            "legacy_curve_role": "OBSERVATION_BASELINE_ONLY",
            "independent_curve_economic_dispatch_allowed": False,
            "runtime_hardware_owner": "storage_manager",
            "shadow_hardware_effect": False,
        },
        "slots": [],
        "fallback": True,
        "fallback_reason_code": str(reason_code or "SHADOW_UNKNOWN_ERROR"),
    }
    if isinstance(diagnostic, dict):
        result["infeasibility_diagnostic"] = copy.deepcopy(diagnostic)
    result["shadow_plan_id"] = _hash(_deterministic_result_material(result))
    return result


def shadow_not_applicable(reason_code: str = "DIRECT_MARKETING_DISABLED") -> Dict[str, Any]:
    """Expliziter, wirkungsloser Zustand für Anlagen ohne DV-Capability.

    Ein deaktivierter Produktpfad ist kein technischer Fallback. Insbesondere
    werden deshalb weder fehlende Preis-/Forecastdaten noch ein leerer
    Shadowhorizont als Laufzeitfehler ausgegeben oder für ein Shadow-60-Gate
    angerechnet.
    """

    result = {
        "schema_version": SHADOW_SCHEMA,
        "status": "SHADOW_NOT_APPLICABLE",
        "applicable": False,
        "not_applicable": True,
        "not_applicable_reason_code": str(reason_code or "DIRECT_MARKETING_DISABLED"),
        "shadow_only": True,
        "commands_allowed": False,
        "owner": "storage_dispatch_shadow",
        "algorithm": ALGORITHM,
        "runtime_ms": 0.0,
        "historical_claim_status": "NOT_APPLICABLE_NO_DIRECT_MARKETING_CAPABILITY",
        "owner_contract": {
            "economic_decision_owner": None,
            "legacy_curve_role": "UNCHANGED_PRODUCTIVE_BASELINE",
            "independent_curve_economic_dispatch_allowed": False,
            "runtime_hardware_owner": "storage_manager",
            "shadow_hardware_effect": False,
        },
        "decision_horizon": {"slots": 0, "not_applicable": True},
        "price_horizon_contract": {
            "schema_version": PRICE_HORIZON_SCHEMA,
            "effective_decision_slots": 0,
            "field_activation_horizon_complete": False,
            "complete_to_next_local_market_day_boundary": False,
            "rolling_24h_complete": False,
            "unpriced_slots_imputed": 0,
            "not_applicable": True,
        },
        "reserve_contract": {
            "field_activation_input_complete": False,
            "not_applicable": True,
        },
        "terminal_value": {
            "fresh": False,
            "not_applicable": True,
        },
        "slots": [],
        "fallback": False,
        "fallback_reason_code": None,
    }
    result["shadow_plan_id"] = _hash(_deterministic_result_material(result))
    return result
