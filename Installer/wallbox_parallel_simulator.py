#!/usr/bin/env python3
"""Shadow simulator for Wallbox Manager decisions.

This module intentionally never talks to a real charger. It models command
decisions and the slow physical response of a wallbox/car combination for the
read-only comparison view.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from Wallbox.modes import MODE_BASE, MODE_CURVE, MODE_OFF, MODE_PRICE, MODE_TARGET, normalize_wb_mode
except Exception:  # pragma: no cover - direct package import from tests
    from Installer.Wallbox.modes import MODE_BASE, MODE_CURVE, MODE_OFF, MODE_PRICE, MODE_TARGET, normalize_wb_mode


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
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


def _mode_name(mode: int) -> str:
    return {
        MODE_OFF: "OFF",
        MODE_CURVE: "CURVE",
        MODE_BASE: "BASE",
        MODE_TARGET: "TARGET",
        MODE_PRICE: "PRICE",
    }.get(normalize_wb_mode(mode), "CURVE")


@dataclass
class ShadowWallboxState:
    command_amp: int = 0
    command_phases: int = 1
    actual_phases: int = 0
    real_power_w: float = 0.0
    meter_power_w: float = 0.0
    meter_phases: int = 0
    charging: bool = False
    last_ts: Optional[float] = None
    last_meter_ts: Optional[float] = None
    start_ready_ts: float = 0.0
    phase_pause_until: float = 0.0
    last_phase_switch_ts: float = 0.0
    phase_down_since: float = 0.0
    phase_up_since: float = 0.0
    phase_3p_block_until: float = 0.0
    zero_budget_since: float = 0.0
    zero_budget_restart_block_until: float = 0.0
    grid_import_since: float = 0.0
    stable_budget_jump_done: bool = False
    stable_budget_jump_ts: float = 0.0
    last_amp_up_ts: float = 0.0
    last_amp_down_ts: float = 0.0
    fast_block_until: float = 0.0
    last_change_ts: float = 0.0
    power_history: List[Tuple[float, float, int]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_amp": self.command_amp,
            "command_phases": self.command_phases,
            "actual_phases": self.actual_phases,
            "real_power_w": round(self.real_power_w, 1),
            "meter_power_w": round(self.meter_power_w, 1),
            "meter_phases": self.meter_phases,
            "charging": self.charging,
            "last_ts": self.last_ts,
            "last_meter_ts": self.last_meter_ts,
            "start_ready_ts": self.start_ready_ts,
            "phase_pause_until": self.phase_pause_until,
            "last_phase_switch_ts": self.last_phase_switch_ts,
            "phase_down_since": self.phase_down_since,
            "phase_up_since": self.phase_up_since,
            "phase_3p_block_until": self.phase_3p_block_until,
            "zero_budget_since": self.zero_budget_since,
            "zero_budget_restart_block_until": self.zero_budget_restart_block_until,
            "grid_import_since": self.grid_import_since,
            "stable_budget_jump_done": self.stable_budget_jump_done,
            "stable_budget_jump_ts": self.stable_budget_jump_ts,
            "last_amp_up_ts": self.last_amp_up_ts,
            "last_amp_down_ts": self.last_amp_down_ts,
            "fast_block_until": self.fast_block_until,
            "last_change_ts": self.last_change_ts,
        }


class ShadowWallboxSimulator:
    """C++-style shadow wallbox controller with per-rule hysteresis."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg = cfg or {}
        self.max_amp = max(6, min(32, _safe_int(self.cfg.get("wbmaxladestrom", self.cfg.get("wb_max_amp")), 16)))
        self.max_phases = max(1, min(3, _safe_int(
            self.cfg.get(
                "wb_shadow_max_phases",
                self.cfg.get("vehicle_max_phases", self.cfg.get("wb_max_phases", 3)),
            ),
            3,
        )))
        self.start_delay_s = max(0.0, _safe_float(self.cfg.get("wb_shadow_start_delay_s"), 75.0))
        self.power_ramp_s = max(5.0, _safe_float(self.cfg.get("wb_shadow_power_ramp_s"), 60.0))
        self.meter_delay_s = max(0.0, _safe_float(self.cfg.get("wb_shadow_meter_delay_s"), 30.0))
        self.meter_ramp_s = max(0.0, _safe_float(self.cfg.get("wb_shadow_meter_ramp_s"), 10.0))
        self.phase_pause_s = max(10.0, _safe_float(self.cfg.get("wb_shadow_phase_pause_s"), 120.0))
        self.phase_up_hold_s = max(0.0, _safe_float(self.cfg.get("wb_phase_up_forecast_hold_s"), 180.0))
        self.phase_down_delay_s = max(60.0, _safe_float(self.cfg.get("wb_phase_down_delay_s"), 300.0))
        self.phase_reup_block_s = max(120.0, _safe_float(self.cfg.get("wb_phase_down_reup_block_s"), 600.0))
        self.phase_up_buffer_w = max(0.0, _safe_float(self.cfg.get("wb_phase_up_buffer_w"), 1200.0))
        self.phase_down_grid_w = max(300.0, _safe_float(self.cfg.get("wb_phase_down_grid_w"), 1800.0))
        self.zero_budget_stop_s = max(30.0, _safe_float(self.cfg.get("wb_shadow_zero_budget_stop_s"), 300.0))
        self.zero_budget_grid_stop_s = max(20.0, _safe_float(self.cfg.get("wb_shadow_zero_budget_grid_stop_s"), 90.0))
        self.zero_budget_restart_block_s = max(30.0, _safe_float(self.cfg.get("wb_shadow_zero_budget_restart_block_s"), 180.0))
        self.follow_hold_s = max(5.0, _safe_float(self.cfg.get("wb_stable_follow_hold_s"), 25.0))
        self.budget_jump_max_a = max(1, min(8, _safe_int(self.cfg.get("wb_stable_budget_jump_max_a"), 5)))
        self.budget_jump_deadband_a = max(2, _safe_int(self.cfg.get("wb_stable_budget_jump_deadband_a"), 3))
        self.budget_jump_hold_s = max(self.follow_hold_s, _safe_float(self.cfg.get("wb_stable_budget_jump_hold_s"), 45.0))
        self.amp_deadband_a = max(0, _safe_int(self.cfg.get("wb_shadow_amp_deadband_a"), 1))
        self.start_confirm_w = max(250.0, _safe_float(self.cfg.get("wb_stable_start_confirm_w"), 700.0))
        self.fast_grid_w = max(250.0, _safe_float(self.cfg.get("wb_shadow_fast_grid_w"), 500.0))
        self.fast_grid_s = max(5.0, _safe_float(self.cfg.get("wb_shadow_fast_grid_s"), 45.0))

    def initial_state(self, phases: int = 1, amp: int = 0, real_power_w: float = 0.0, ts: Optional[float] = None) -> ShadowWallboxState:
        phases = min(self.max_phases, max(1, int(phases or 1)))
        state = ShadowWallboxState(
            command_amp=max(0, int(amp or 0)),
            command_phases=3 if int(phases or 1) >= 3 else 1,
            actual_phases=3 if int(phases or 1) >= 3 and real_power_w > 250 else (1 if real_power_w > 250 else 0),
            real_power_w=max(0.0, float(real_power_w or 0.0)),
            meter_power_w=max(0.0, float(real_power_w or 0.0)),
            meter_phases=3 if int(phases or 1) >= 3 and real_power_w > 250 else (1 if real_power_w > 250 else 0),
            charging=real_power_w > 250,
            last_ts=ts,
            last_meter_ts=ts,
        )
        if ts is not None:
            state.power_history.append((float(ts), state.real_power_w, state.actual_phases))
        if state.command_amp > 0 and ts is not None:
            state.start_ready_ts = float(ts)
        return state

    def _event(self, events: List[Dict[str, Any]], kind: str, ts: float, **data: Any) -> None:
        item = {"kind": kind, "ts": int(ts)}
        item.update(data)
        events.append(item)

    def _physical_step(self, state: ShadowWallboxState, sample: Dict[str, Any], now: float) -> None:
        dt_s = 0.0 if state.last_ts is None else max(0.0, min(600.0, now - float(state.last_ts)))
        connected = bool(sample.get("car_connected", True))
        if state.command_amp <= 0 or not connected:
            state.real_power_w = max(0.0, state.real_power_w - (dt_s / 20.0) * max(state.real_power_w, 1.0))
            if state.real_power_w < 80:
                state.real_power_w = 0.0
                state.actual_phases = 0
                state.charging = False
                state.start_ready_ts = 0.0
            state.last_ts = now
            return

        if now < state.phase_pause_until:
            state.real_power_w = 0.0
            state.actual_phases = 0
            state.charging = False
            state.last_ts = now
            return

        if state.start_ready_ts <= 0.0 and state.real_power_w <= 250.0:
            state.start_ready_ts = now + self.start_delay_s
        if now < state.start_ready_ts:
            state.real_power_w = 0.0
            state.actual_phases = 0
            state.charging = False
            state.last_ts = now
            return

        target_w = float(state.command_amp) * 230.0 * float(max(1, state.command_phases))
        if self.power_ramp_s <= 0:
            state.real_power_w = target_w
        else:
            step_share = min(1.0, dt_s / self.power_ramp_s)
            state.real_power_w += (target_w - state.real_power_w) * step_share
        state.actual_phases = max(1, state.command_phases)
        state.charging = state.real_power_w > 250.0
        state.last_ts = now

    def _update_meter(self, state: ShadowWallboxState, now: float) -> None:
        state.power_history.append((now, max(0.0, float(state.real_power_w)), int(state.actual_phases)))
        keep_after = now - max(600.0, self.meter_delay_s + 300.0)
        state.power_history = [item for item in state.power_history if item[0] >= keep_after]

        delayed_power = max(0.0, float(state.real_power_w))
        delayed_phases = int(state.actual_phases)
        if self.meter_delay_s > 0.0:
            cutoff = now - self.meter_delay_s
            delayed_candidates = [item for item in state.power_history if item[0] <= cutoff]
            if delayed_candidates:
                _ts, delayed_power, delayed_phases = delayed_candidates[-1]
            elif state.power_history:
                _ts, delayed_power, delayed_phases = state.power_history[0]

        dt_s = 0.0 if state.last_meter_ts is None else max(0.0, min(600.0, now - float(state.last_meter_ts)))
        if self.meter_ramp_s <= 0.0:
            state.meter_power_w = delayed_power
        else:
            share = min(1.0, dt_s / self.meter_ramp_s)
            state.meter_power_w += (delayed_power - state.meter_power_w) * share
        if state.meter_power_w < 80.0:
            state.meter_power_w = 0.0
            state.meter_phases = 0
        else:
            state.meter_phases = delayed_phases if delayed_power > 250.0 else int(state.actual_phases)
        state.last_meter_ts = now

    def _switch_phases(self, state: ShadowWallboxState, target_phases: int, now: float, events: List[Dict[str, Any]], reason: str) -> None:
        target = 3 if int(target_phases) >= 3 else 1
        target = min(self.max_phases, target)
        if state.command_phases == target and now < state.phase_pause_until:
            return
        previous = state.command_phases
        state.command_phases = target
        state.command_amp = 0
        state.real_power_w = 0.0
        state.charging = False
        state.actual_phases = 0
        state.phase_pause_until = now + self.phase_pause_s
        state.last_phase_switch_ts = now
        state.stable_budget_jump_done = False
        state.fast_block_until = now + max(30.0, self.phase_pause_s)
        if target == 1:
            state.phase_3p_block_until = max(state.phase_3p_block_until, now + self.phase_reup_block_s)
        self._event(events, "phase_switch", now, previous=previous, target=target, reason=reason)

    def _stable_amp(self, state: ShadowWallboxState, proposed_amp: int, now: float, grid_w: float, events: List[Dict[str, Any]]) -> int:
        proposed = int(max(0, min(self.max_amp, proposed_amp)))
        current = int(max(0, state.command_amp))
        if proposed <= 0:
            state.stable_budget_jump_done = False
            state.stable_budget_jump_ts = 0.0
            return 0
        if current <= 0:
            state.stable_budget_jump_done = False
            state.stable_budget_jump_ts = 0.0
            return min(proposed, 6)
        real_confirmed = bool(state.charging or state.real_power_w >= self.start_confirm_w)
        if not real_confirmed:
            state.stable_budget_jump_done = False
            state.stable_budget_jump_ts = 0.0
            return min(current, proposed) if proposed < current else current
        if not state.stable_budget_jump_done:
            if now < state.fast_block_until:
                return current
            state.stable_budget_jump_done = True
            state.stable_budget_jump_ts = now
            target = min(proposed, current + self.budget_jump_max_a)
            self._event(events, "budget_jump", now, amp=target)
            return target
        if (
            self.amp_deadband_a > 0
            and current > 0
            and proposed > 0
            and abs(proposed - current) <= self.amp_deadband_a
            and not (proposed < current and grid_w > self.fast_grid_w)
            and not (proposed > current and grid_w < -1200.0)
        ):
            return current
        if proposed > current:
            if (
                proposed - current >= self.budget_jump_deadband_a
                and now >= state.fast_block_until
                and now - state.stable_budget_jump_ts >= self.budget_jump_hold_s
                and grid_w <= self.fast_grid_w
            ):
                state.stable_budget_jump_ts = now
                target = min(proposed, current + self.budget_jump_max_a)
                self._event(events, "budget_jump", now, amp=target)
                return target
            if now < state.fast_block_until or now - state.last_amp_up_ts < self.follow_hold_s:
                return current
            return min(proposed, current + 1)
        if proposed < current:
            if grid_w <= self.fast_grid_w and now - state.last_amp_down_ts < self.follow_hold_s:
                return current
            return max(proposed, current - 1)
        return proposed

    def step(self, state: ShadowWallboxState, sample: Dict[str, Any]) -> Dict[str, Any]:
        now = _safe_float(sample.get("ts_s"), 0.0)
        if now <= 0:
            now = 0.0 if state.last_ts is None else float(state.last_ts) + max(1.0, _safe_float(sample.get("dt_s"), 60.0))
        wallbox_online = bool(sample.get("wallbox_online", sample.get("driver_online", True)))
        wallbox_configured = bool(sample.get("wallbox_configured", True))
        regulation_enabled = bool(sample.get("regulation_enabled", True))
        self._physical_step(state, sample, now)
        if wallbox_online:
            self._update_meter(state, now)

        events: List[Dict[str, Any]] = []
        mode = normalize_wb_mode(sample.get("mode", MODE_CURVE))
        grid_allowed = bool(sample.get("grid_allowed", False) or sample.get("scheduled_slot_active", False))
        scheduled_grid = bool(mode != MODE_OFF and grid_allowed)
        connected = bool(sample.get("car_connected", True))
        budget_w = max(0.0, _safe_float(sample.get("budget_w"), 0.0))
        grid_w = _safe_float(sample.get("grid_w"), 0.0)
        phase_forecast_hold = bool(sample.get("phase_forecast_hold", False))
        floor_reachable = bool(sample.get("storage_floor_reachable", True))
        min_1p_w = 6.0 * 230.0
        min_3p_w = 6.0 * 230.0 * 3.0
        previous_amp = state.command_amp
        previous_phases = state.command_phases

        reason = "hold"
        proposed_amp = 0
        target_phases = max(1, state.command_phases or 1)

        if not wallbox_online or not wallbox_configured or not regulation_enabled:
            if not wallbox_online:
                reason = "driver_offline_fallback"
                # Ein Offline-Intervall ist keine stabile 3p-Budgetevidenz.
                # Nach Recovery beginnt ausschließlich die kurze fachliche
                # Haltefrist neu; ein Hardwareausgang entsteht hier nicht.
                state.phase_up_since = 0.0
            elif not wallbox_configured:
                reason = "wallbox_observe_only"
            else:
                reason = "regulation_disabled_observe_only"
            payload = {
                "ts": int(now),
                "shadow_only": True,
                "mode": mode,
                "mode_name": _mode_name(mode),
                "inputs": {
                    "budget_w": round(budget_w, 1),
                    "grid_w": round(grid_w, 1),
                    "car_connected": connected,
                    "grid_allowed": grid_allowed,
                    "phase_forecast_hold": phase_forecast_hold,
                    "storage_floor_reachable": floor_reachable,
                    "wallbox_online": wallbox_online,
                    "wallbox_configured": wallbox_configured,
                    "regulation_enabled": regulation_enabled,
                },
                "decision": {
                    "amp": state.command_amp,
                    "phases": state.command_phases,
                    "reason": reason,
                    "stop_due": False,
                    "command_sent": False,
                },
                "physical": {
                    "charging": state.charging,
                    "actual_phases": state.actual_phases,
                    "real_power_w": round(state.real_power_w, 1),
                    "meter_power_w": round(state.meter_power_w, 1),
                    "meter_phases": state.meter_phases,
                    "meter_delay_s": round(self.meter_delay_s, 1),
                    "phase_pause_active": now < state.phase_pause_until,
                    "start_wait_active": bool(state.command_amp > 0 and not state.charging and now < state.start_ready_ts),
                    "wallbox_online": wallbox_online,
                },
                "state": state.to_dict(),
                "events": events,
            }
            return payload

        if mode == MODE_OFF or not connected:
            proposed_amp = 0
            reason = "off_or_disconnected"
            if not connected:
                state.zero_budget_restart_block_until = 0.0
                if state.real_power_w <= 80.0:
                    state.command_phases = 1
                    state.phase_down_since = 0.0
                    state.phase_up_since = 0.0
                    state.phase_3p_block_until = 0.0
        elif scheduled_grid:
            if self.max_phases >= 3 and state.command_phases != 3 and now - state.last_phase_switch_ts >= 60.0:
                self._switch_phases(state, 3, now, events, "grid_slot_3p")
            target_phases = min(self.max_phases, 3)
            proposed_amp = self.max_amp
            reason = "grid_allowed"
        else:
            if target_phases >= 3:
                keep_3p = bool(
                    phase_forecast_hold
                    or budget_w >= min_3p_w - 800.0
                    or (state.charging and grid_w < self.phase_down_grid_w and budget_w >= min_3p_w - 1800.0)
                )
                if not keep_3p:
                    if state.phase_down_since <= 0.0:
                        state.phase_down_since = now
                    if now - state.phase_down_since >= self.phase_down_delay_s:
                        self._switch_phases(state, 1, now, events, "3p_too_heavy")
                        target_phases = 1
                        reason = "phase_down_pause"
                    else:
                        reason = "observe_3p_down"
                else:
                    state.phase_down_since = 0.0
            else:
                can_up = bool(
                    self.max_phases >= 3
                    and budget_w >= min_3p_w + self.phase_up_buffer_w
                    and now >= state.phase_3p_block_until
                    and now - state.last_phase_switch_ts >= 300.0
                )
                if can_up:
                    if state.phase_up_since <= 0.0:
                        state.phase_up_since = now
                    if now - state.phase_up_since >= self.phase_up_hold_s:
                        self._switch_phases(state, 3, now, events, "3p_budget_supported")
                        target_phases = 3
                        reason = "phase_up_pause"
                    else:
                        reason = "observe_3p_up"
                else:
                    state.phase_up_since = 0.0

            target_phases = max(1, state.command_phases or target_phases)
            w_per_amp = 230.0 * float(target_phases)
            if mode == MODE_BASE and floor_reachable:
                proposed_amp = 6
                if budget_w > min_1p_w:
                    proposed_amp = max(proposed_amp, int(budget_w / w_per_amp))
                reason = "base_floor"
            elif budget_w >= 6.0 * w_per_amp:
                proposed_amp = int(budget_w / w_per_amp)
                reason = "budget"
            else:
                proposed_amp = 0
                reason = "zero_budget"

            if (
                proposed_amp > 0
                and state.command_amp <= 0
                and now < state.zero_budget_restart_block_until
            ):
                proposed_amp = 0
                reason = "zero_budget_restart_block"

            if proposed_amp <= 0 and state.command_amp > 0 and not scheduled_grid:
                if state.zero_budget_since <= 0.0:
                    state.zero_budget_since = now
                zero_age = now - state.zero_budget_since
                stop_due = bool(zero_age >= self.zero_budget_stop_s)
                if grid_w > self.fast_grid_w:
                    if state.grid_import_since <= 0.0:
                        state.grid_import_since = now
                    stop_due = stop_due or (now - state.grid_import_since >= self.zero_budget_grid_stop_s)
                else:
                    state.grid_import_since = 0.0
                if not stop_due:
                    proposed_amp = 6
                    reason = "zero_budget_hold"
                else:
                    reason = "zero_budget_stop"
            else:
                state.zero_budget_since = 0.0
                if grid_w <= self.fast_grid_w:
                    state.grid_import_since = 0.0

            if grid_w > self.fast_grid_w and state.command_amp > 6 and not grid_allowed:
                if state.grid_import_since <= 0.0:
                    state.grid_import_since = now
                if now - state.grid_import_since >= self.fast_grid_s:
                    drop_amp = max(1, int(math.ceil((grid_w + 120.0) / (230.0 * max(1, state.command_phases)))))
                    proposed_amp = min(proposed_amp if proposed_amp > 0 else state.command_amp, max(6, state.command_amp - drop_amp))
                    state.fast_block_until = now + 60.0
                    reason = "fast_grid_drop"

        final_amp = self._stable_amp(state, proposed_amp, now, grid_w, events)
        final_amp = max(0, min(self.max_amp, int(final_amp)))
        if final_amp != previous_amp:
            if final_amp > previous_amp:
                state.last_amp_up_ts = now
            else:
                state.last_amp_down_ts = now
            state.last_change_ts = now
            if final_amp <= 0:
                state.start_ready_ts = 0.0
                if previous_amp > 0 and reason == "zero_budget_stop":
                    state.zero_budget_restart_block_until = now + self.zero_budget_restart_block_s
            elif previous_amp <= 0:
                state.start_ready_ts = now + self.start_delay_s
            self._event(events, "amp_change", now, previous=previous_amp, target=final_amp, reason=reason)
        state.command_amp = final_amp
        if state.command_phases != previous_phases:
            state.last_change_ts = now

        payload = {
            "ts": int(now),
            "shadow_only": True,
            "mode": mode,
            "mode_name": _mode_name(mode),
            "inputs": {
                "budget_w": round(budget_w, 1),
                "grid_w": round(grid_w, 1),
                "car_connected": connected,
                "grid_allowed": grid_allowed,
                "phase_forecast_hold": phase_forecast_hold,
                "storage_floor_reachable": floor_reachable,
                "wallbox_online": wallbox_online,
                "wallbox_configured": wallbox_configured,
                "regulation_enabled": regulation_enabled,
            },
            "decision": {
                "amp": state.command_amp,
                "phases": state.command_phases,
                "reason": reason,
                "stop_due": reason == "zero_budget_stop",
                "command_sent": bool(state.command_amp != previous_amp or state.command_phases != previous_phases),
            },
            "physical": {
                "charging": state.charging,
                "actual_phases": state.actual_phases,
                "real_power_w": round(state.real_power_w, 1),
                "meter_power_w": round(state.meter_power_w, 1),
                "meter_phases": state.meter_phases,
                "meter_delay_s": round(self.meter_delay_s, 1),
                "phase_pause_active": now < state.phase_pause_until,
                "start_wait_active": bool(state.command_amp > 0 and not state.charging and now < state.start_ready_ts),
                "wallbox_online": wallbox_online,
            },
            "state": state.to_dict(),
            "events": events,
        }
        state.events.extend(events)
        return payload
