"""Pure central heat policy decisions.

The heat managers gather live state and the HAL/drivers translate decisions to
SG-Ready, Modbus, REST or relays. This module owns the shared heat policy:
protection states, budget cascade, tariff gates, warm-water cycle guards and
heater grid-boost safety.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


TARGET_NORMAL = "NORMAL"
TARGET_PV_SURPLUS = "PV_SURPLUS"
TARGET_PRE_DUMP = "PRE_DUMP"
TARGET_BOOST = "BOOST"
TARGET_BLOCKED = "BLOCKED"
TARGET_PROTECTED = "PROTECTED"

SG_READY_BLOCKED = 1
SG_READY_NORMAL = 2
SG_READY_PV = 3
SG_READY_BOOST = 4

WW_CYCLE_MIN_RUNTIME_S = 30 * 60
PRICE_BLOCK_MAX_S = 3 * 60 * 60


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        result = float(str(value).replace(",", "."))
        return result if math.isfinite(result) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    return int(round(_safe_float(value, default)))


def _clamp_w(value: Any) -> int:
    return max(0, _safe_int(value, 0))


def _positive(value: Any) -> bool:
    return _safe_float(value, 0.0) > 0.0


def sg_ready_for_target(target_state: str, *, fallback: int = SG_READY_NORMAL) -> int:
    """Map the abstract heat target to the generic SG-Ready state."""

    if target_state == TARGET_BLOCKED:
        return SG_READY_BLOCKED
    if target_state == TARGET_PV_SURPLUS:
        return SG_READY_PV
    if target_state in (TARGET_BOOST, TARGET_PRE_DUMP):
        return SG_READY_BOOST
    if target_state == TARGET_NORMAL:
        return SG_READY_NORMAL
    if target_state == TARGET_PROTECTED:
        return int(fallback or SG_READY_NORMAL)
    return SG_READY_NORMAL


@dataclass(frozen=True)
class HeatPolicyInput:
    """Input contract for one heat policy cycle.

    All budgets are already bounded by upstream owners. In particular, storage
    budgets must come from the Storage Manager; this policy never commands
    battery charge or discharge.
    """

    now_ts: float
    auto_enabled: bool = True
    heat_enabled: bool = True
    heatpump_configured: bool = True
    heater_configured: bool = False

    pv_available_budget_w: float = 0.0
    pv_start_w: float = 500.0
    pv_stop_w: float = 200.0
    pv_hysteresis_active: bool = False
    storage_available_budget_w: float = 0.0
    predump_available_budget_w: float = 0.0

    low_price_window_active: bool = False
    expensive_price_window_active: bool = False
    price_quality_valid: bool = True
    current_price_ct: Optional[float] = None
    price_window_end_ts: Optional[float] = None
    price_pain_limit_ct: float = 45.0
    battery_empty: bool = False
    price_block_started_ts: Optional[float] = None
    price_block_max_s: float = PRICE_BLOCK_MAX_S

    forecast_deficit_kwh: float = 0.0
    forecast_valid: bool = True
    forecast_source: str = ""
    forecast_quality: str = ""
    forecast_need_kwh: float = 0.0
    boost_delivered_kwh: float = 0.0
    control_cycle_s: float = 60.0
    heatpump_grid_boost_enable: bool = True
    heatpump_grid_boost_max_w: float = 3500.0
    heater_grid_boost_enable: bool = False
    heater_grid_boost_ack: bool = False
    heater_grid_boost_requires_deficit: bool = True
    heater_grid_boost_max_w: float = 3000.0
    heater_grid_boost_price_limit_ct: float = 0.0

    temperature_valid: bool = True
    temperature_c: Optional[float] = None
    temperature_min_c: Optional[float] = None
    temperature_max_c: Optional[float] = None
    temperature_deadband_c: float = 0.2

    ww_cycle_requested: bool = False
    ww_cycle_running: bool = False
    ww_cycle_started_ts: Optional[float] = None
    ww_cycle_min_runtime_s: float = WW_CYCLE_MIN_RUNTIME_S

    defrost_active: bool = False
    legionella_active: bool = False
    hardware_protection_active: bool = False
    source_protection_active: bool = False
    restart_block_remaining_s: float = 0.0
    min_runtime_stop_remaining_s: float = 0.0

    previous_target_state: str = TARGET_NORMAL
    previous_sg_ready_state: int = SG_READY_NORMAL
    previous_available_budget_w: float = 0.0


@dataclass(frozen=True)
class HeatPolicyDecision:
    target_state: str
    sg_ready_state: int
    available_budget_w: int
    block_reason: str
    owner: str
    valid_until_ts: Optional[float] = None
    heatpump: Dict[str, Any] = field(default_factory=dict)
    heater: Dict[str, Any] = field(default_factory=dict)
    forecast: Dict[str, Any] = field(default_factory=dict)
    data_quality: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "target_state": self.target_state,
            "sg_ready_state": self.sg_ready_state,
            "available_budget_w": self.available_budget_w,
            "block_reason": self.block_reason,
            "owner": self.owner,
            "valid_until_ts": self.valid_until_ts,
            "heatpump": dict(self.heatpump),
            "heater": dict(self.heater),
            "forecast": dict(self.forecast),
            "data_quality": dict(self.data_quality),
        }


def remaining_forecast_deficit_kwh(ctx: HeatPolicyInput) -> float:
    """Return the still uncovered heat energy deficit for this horizon."""

    return max(
        0.0,
        _safe_float(ctx.forecast_deficit_kwh, 0.0)
        - _safe_float(ctx.boost_delivered_kwh, 0.0),
    )


def deficit_limited_power_w(ctx: HeatPolicyInput, max_power_w: float) -> int:
    """Cap boost power so one control cycle cannot over-cover the deficit."""

    remaining_kwh = remaining_forecast_deficit_kwh(ctx)
    cycle_s = max(1.0, _safe_float(ctx.control_cycle_s, 60.0))
    energy_limited_w = remaining_kwh * 1000.0 * 3600.0 / cycle_s
    return _clamp_w(min(_safe_float(max_power_w, 0.0), energy_limited_w))


def ww_start_blocked_by_window(ctx: HeatPolicyInput) -> bool:
    """Return true when a new WW cycle would not fit into the cheap window."""

    if not ctx.ww_cycle_requested or ctx.ww_cycle_running:
        return False
    if not ctx.low_price_window_active or ctx.price_window_end_ts is None:
        return False
    remaining_s = _safe_float(ctx.price_window_end_ts, 0.0) - _safe_float(ctx.now_ts, 0.0)
    return remaining_s < _safe_float(ctx.ww_cycle_min_runtime_s, WW_CYCLE_MIN_RUNTIME_S)


def ww_cycle_min_runtime_remaining_s(ctx: HeatPolicyInput) -> float:
    """Return remaining protected WW runtime after a started cycle."""

    started = _safe_float(ctx.ww_cycle_started_ts, 0.0)
    if started <= 0.0:
        return 0.0
    min_s = max(0.0, _safe_float(ctx.ww_cycle_min_runtime_s, WW_CYCLE_MIN_RUNTIME_S))
    if min_s <= 0.0:
        return 0.0
    elapsed_s = max(0.0, _safe_float(ctx.now_ts, 0.0) - started)
    return max(0.0, min_s - elapsed_s)


def _temperature_blocks_grid_boost(ctx: HeatPolicyInput) -> Optional[str]:
    if not ctx.temperature_valid or ctx.temperature_c is None:
        return "Missing Temperature Sensor"
    temp = _safe_float(ctx.temperature_c, 0.0)
    deadband = max(0.0, _safe_float(ctx.temperature_deadband_c, 0.0))
    if ctx.temperature_max_c is not None and temp >= _safe_float(ctx.temperature_max_c, 0.0) - deadband:
        return "Max Temperature Reached"
    if ctx.temperature_min_c is None:
        return "Missing Minimum Temperature"
    if temp >= _safe_float(ctx.temperature_min_c, 0.0):
        return "Minimum Temperature Satisfied"
    return None


def _thermal_protection_required(ctx: HeatPolicyInput) -> bool:
    if not ctx.temperature_valid or ctx.temperature_c is None or ctx.temperature_min_c is None:
        return False
    return _safe_float(ctx.temperature_c, 0.0) < _safe_float(ctx.temperature_min_c, 0.0)


def _price_block_reason(ctx: HeatPolicyInput) -> str:
    started = ctx.price_block_started_ts
    duration_s = 0.0
    if started is not None:
        duration_s = max(0.0, _safe_float(ctx.now_ts, 0.0) - _safe_float(started, 0.0))
    duration_h = duration_s / 3600.0
    max_h = max(0.0, _safe_float(ctx.price_block_max_s, PRICE_BLOCK_MAX_S)) / 3600.0
    return "Blocked by Price (Duration %.1fh, Max %.0fh)" % (duration_h, max_h)


def _price_block_active(ctx: HeatPolicyInput) -> bool:
    if not (ctx.expensive_price_window_active and ctx.battery_empty):
        return False
    if _thermal_protection_required(ctx):
        return False
    max_s = max(0.0, _safe_float(ctx.price_block_max_s, PRICE_BLOCK_MAX_S))
    if max_s <= 0.0:
        return False
    if ctx.price_block_started_ts is None:
        return True
    elapsed_s = max(0.0, _safe_float(ctx.now_ts, 0.0) - _safe_float(ctx.price_block_started_ts, 0.0))
    return elapsed_s < max_s


def _pv_hysteresis_open(ctx: HeatPolicyInput) -> bool:
    budget = _safe_float(ctx.pv_available_budget_w, 0.0)
    if ctx.pv_hysteresis_active:
        return budget >= max(0.0, _safe_float(ctx.pv_stop_w, 0.0))
    return budget >= max(0.0, _safe_float(ctx.pv_start_w, 0.0))


def _protected_decision(ctx: HeatPolicyInput, reason: str, owner: str) -> HeatPolicyDecision:
    previous_sg = int(ctx.previous_sg_ready_state or SG_READY_NORMAL)
    if previous_sg not in (SG_READY_NORMAL, SG_READY_PV, SG_READY_BOOST):
        previous_sg = SG_READY_NORMAL
    budget_w = max(
        _clamp_w(ctx.previous_available_budget_w),
        _clamp_w(ctx.pv_available_budget_w),
        _clamp_w(ctx.storage_available_budget_w),
        _clamp_w(ctx.predump_available_budget_w),
    )
    return _decision(
        ctx,
        TARGET_PROTECTED,
        previous_sg,
        budget_w,
        reason,
        owner,
        protected=True,
    )


def _decision(
    ctx: HeatPolicyInput,
    target_state: str,
    sg_ready_state: int,
    available_budget_w: int,
    block_reason: str,
    owner: str,
    *,
    protected: bool = False,
    valid_until_ts: Optional[float] = None,
) -> HeatPolicyDecision:
    remaining_kwh = remaining_forecast_deficit_kwh(ctx)
    return HeatPolicyDecision(
        target_state=target_state,
        sg_ready_state=int(sg_ready_state),
        available_budget_w=_clamp_w(available_budget_w),
        block_reason=block_reason,
        owner=owner,
        valid_until_ts=valid_until_ts,
        heatpump={
            "configured": bool(ctx.heatpump_configured),
            "sg_ready_state": int(sg_ready_state),
            "protected": bool(protected),
            "restart_block_remaining_s": max(0, _safe_int(ctx.restart_block_remaining_s, 0)),
            "min_runtime_stop_remaining_s": max(0, _safe_int(ctx.min_runtime_stop_remaining_s, 0)),
            "ww_cycle_running": bool(ctx.ww_cycle_running),
            "ww_cycle_requested": bool(ctx.ww_cycle_requested),
            "ww_cycle_min_runtime_remaining_s": max(0, _safe_int(ww_cycle_min_runtime_remaining_s(ctx), 0)),
            "ww_start_blocked_by_window": bool(ww_start_blocked_by_window(ctx)),
        },
        heater={
            "configured": bool(ctx.heater_configured),
            "available_budget_w": _clamp_w(available_budget_w),
            "grid_boost_enabled": bool(ctx.heater_grid_boost_enable),
            "grid_boost_ack": bool(ctx.heater_grid_boost_ack),
        },
        forecast={
            "need_kwh": round(max(0.0, _safe_float(ctx.forecast_need_kwh, 0.0)), 4),
            "deficit_kwh": round(max(0.0, _safe_float(ctx.forecast_deficit_kwh, 0.0)), 4),
            "boost_delivered_kwh": round(max(0.0, _safe_float(ctx.boost_delivered_kwh, 0.0)), 4),
            "remaining_boost_kwh": round(remaining_kwh, 4),
            "valid": bool(ctx.forecast_valid),
            "source": str(ctx.forecast_source or ""),
            "quality": str(ctx.forecast_quality or ""),
        },
        data_quality={
            "price_valid": bool(ctx.price_quality_valid),
            "temperature_valid": bool(ctx.temperature_valid),
            "forecast_valid": bool(ctx.forecast_valid),
        },
    )


def decide_heat_policy(ctx: HeatPolicyInput) -> HeatPolicyDecision:
    """Compute the central heat decision before HAL/driver execution."""

    if not ctx.auto_enabled or not ctx.heat_enabled:
        return _decision(
            ctx,
            TARGET_BLOCKED,
            SG_READY_BLOCKED,
            0,
            "Blocked by User Off",
            "user_off",
        )

    if not (ctx.heatpump_configured or ctx.heater_configured):
        return _decision(
            ctx,
            TARGET_BLOCKED,
            SG_READY_BLOCKED,
            0,
            "Blocked by Missing Heat Device",
            "no_heat_device",
        )

    if ctx.hardware_protection_active:
        return _protected_decision(ctx, "Hardware Protection Active", "hardware_protection")
    if ctx.defrost_active:
        return _protected_decision(ctx, "Defrost Active", "defrost")
    if ctx.legionella_active:
        return _protected_decision(ctx, "Legionella Protection Active", "legionella")
    if ctx.source_protection_active:
        return _protected_decision(ctx, "Source Protection Active", "source_protection")

    ww_runtime_remaining_s = ww_cycle_min_runtime_remaining_s(ctx)
    if ctx.ww_cycle_running or ww_runtime_remaining_s > 0.0:
        price_pain = (
            ctx.price_quality_valid
            and ctx.current_price_ct is not None
            and _safe_float(ctx.current_price_ct, 0.0) > _safe_float(ctx.price_pain_limit_ct, 45.0)
            and not ctx.low_price_window_active
        )
        if price_pain:
            return _decision(
                ctx,
                TARGET_BLOCKED,
                SG_READY_BLOCKED,
                0,
                "WW Cycle Stopped by Price Pain",
                "ww_price_pain",
            )
        if ww_runtime_remaining_s > 0.0:
            return _protected_decision(ctx, "WW Cycle Hold Until 30min Minimum", "ww_cycle")
        return _protected_decision(ctx, "WW Cycle Hold Until Done", "ww_cycle")

    if _price_block_active(ctx):
        return _decision(
            ctx,
            TARGET_BLOCKED,
            SG_READY_BLOCKED,
            0,
            _price_block_reason(ctx),
            "price_block",
        )

    if ctx.restart_block_remaining_s > 0:
        return _decision(
            ctx,
            TARGET_BLOCKED,
            SG_READY_BLOCKED,
            0,
            "Blocked by Restart Delay",
            "restart_block",
        )

    if ww_start_blocked_by_window(ctx):
        return _decision(
            ctx,
            TARGET_NORMAL,
            SG_READY_NORMAL,
            0,
            "WW Cycle Start Blocked (Window End < 30min)",
            "ww_window_guard",
        )

    if _pv_hysteresis_open(ctx):
        budget_w = _clamp_w(ctx.pv_available_budget_w)
        return _decision(
            ctx,
            TARGET_PV_SURPLUS,
            SG_READY_PV,
            budget_w,
            "PV Surplus",
            "pv_surplus",
        )

    predump_w = _clamp_w(ctx.predump_available_budget_w)
    if predump_w > 0:
        return _decision(
            ctx,
            TARGET_PRE_DUMP,
            SG_READY_BOOST,
            predump_w,
            "Pre-Dump Budget",
            "predump",
        )

    storage_w = _clamp_w(ctx.storage_available_budget_w)
    if storage_w > 0:
        return _decision(
            ctx,
            TARGET_NORMAL,
            SG_READY_NORMAL,
            storage_w,
            "Storage Heat Budget",
            "storage_budget",
        )

    if not ctx.low_price_window_active:
        return _decision(
            ctx,
            TARGET_NORMAL,
            SG_READY_NORMAL,
            0,
            "Waiting for Low Price Window",
            "idle",
        )

    if not ctx.price_quality_valid:
        return _decision(
            ctx,
            TARGET_NORMAL,
            SG_READY_NORMAL,
            0,
            "Waiting for Valid Price Data",
            "price_data_invalid",
        )

    if not ctx.forecast_valid:
        return _decision(
            ctx,
            TARGET_NORMAL,
            SG_READY_NORMAL,
            0,
            "Waiting for Valid Heat Forecast",
            "forecast_data_invalid",
        )

    temp_block = _temperature_blocks_grid_boost(ctx)
    if temp_block:
        return _decision(ctx, TARGET_NORMAL, SG_READY_NORMAL, 0, temp_block, "temperature_guard")

    remaining_kwh = remaining_forecast_deficit_kwh(ctx)
    if remaining_kwh <= 0.0:
        return _decision(ctx, TARGET_NORMAL, SG_READY_NORMAL, 0, "No Forecast Deficit", "forecast_satisfied")

    heatpump_w = 0
    if ctx.heatpump_configured and ctx.heatpump_grid_boost_enable:
        heatpump_w = deficit_limited_power_w(ctx, ctx.heatpump_grid_boost_max_w)

    heater_w = 0
    heater_wait_reason = None
    heater_wait_owner = None
    if ctx.heater_configured and ctx.heater_grid_boost_enable:
        if not ctx.heater_grid_boost_ack:
            heater_wait_reason = "Waiting for Heater Grid Boost Acknowledgement"
            heater_wait_owner = "heater_grid_boost_ack"
        elif ctx.current_price_ct is None or _safe_float(ctx.current_price_ct, 999.0) > _safe_float(ctx.heater_grid_boost_price_limit_ct, 0.0):
            heater_wait_reason = "Waiting for Heater Grid Boost Price Limit"
            heater_wait_owner = "heater_grid_boost_price"
        elif ctx.heater_grid_boost_requires_deficit and remaining_kwh <= 0.0:
            heater_wait_reason = "No Heater Forecast Deficit"
            heater_wait_owner = "heater_grid_boost_deficit"
        else:
            heater_w = deficit_limited_power_w(ctx, ctx.heater_grid_boost_max_w)

    boost_w = max(heatpump_w, heater_w)
    if boost_w > 0:
        owner = "heater_grid_boost" if heater_w >= heatpump_w and heater_w > 0 else "heatpump_grid_boost"
        return _decision(
            ctx,
            TARGET_BOOST,
            SG_READY_BOOST,
            boost_w,
            "Low Price Forecast Deficit Boost",
            owner,
            valid_until_ts=ctx.price_window_end_ts,
        )

    if heater_wait_reason:
        return _decision(
            ctx,
            TARGET_NORMAL,
            SG_READY_NORMAL,
            0,
            heater_wait_reason,
            heater_wait_owner or "heater_grid_boost_wait",
        )

    return _decision(ctx, TARGET_NORMAL, SG_READY_NORMAL, 0, "Waiting for Heat Boost Eligibility", "idle")
