"""Runtime state for the native wallbox manager loop.

The wallbox manager intentionally keeps some hysteresis and throttle state
between cycles.  This module makes that state explicit so the main loop does
not need to attach ad-hoc attributes to ``run()``.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


@dataclass
class WallboxRuntimeState:
    """Mutable state that belongs to one wallbox-manager process."""

    was_disabled_logged: bool = False
    plan_was_active: bool = False
    wb_min_power_meas: float = 1380.0
    wb_current_amp: int = 6
    last_change_ts: Dict[int, float] = field(default_factory=dict)
    grid_overshoot_ts: Optional[float] = None

    budget_stale_logged: bool = False
    budget_timeout_logged: bool = False
    grid_import_budget_since: float = 0.0
    grid_import_budget_active: bool = False
    min_current_import_wh: Dict[int, float] = field(default_factory=dict)
    min_current_import_since: Dict[int, float] = field(default_factory=dict)
    min_current_import_last_ts: Dict[int, float] = field(default_factory=dict)

    predump_wb_gate_open: bool = False
    predump_wb_gate_start_ts: float = 0.0
    predump_wb_above_since: float = 0.0
    predump_wb_below_since: float = 0.0
    predump_wb_grid_since: float = 0.0
    predump_wb_pause_hold_until: float = 0.0
    predump_wb_was_active: bool = False

    curve_forecast_wb_block_active: bool = False
    curve_forecast_wb_release_since: float = 0.0

    hysteresis_gates: Dict[str, bool] = field(default_factory=dict)
    house_fuse_log_ts: float = 0.0

    @classmethod
    def from_restore(cls, *, wb_min_power_meas: float = 1380.0, wb_current_amp: int = 6):
        return cls(
            wb_min_power_meas=float(wb_min_power_meas or 1380.0),
            wb_current_amp=int(wb_current_amp or 6),
        )

    def reset_budget_log_flags(self):
        self.budget_stale_logged = False
        self.budget_timeout_logged = False

    def apply_budget_freshness_guard(
        self,
        *,
        free_for_limbs_w: float,
        detected_phases: float,
        budget_stale: bool,
        budget_timeout: bool,
    ) -> Tuple[float, Optional[str]]:
        """Apply stale/timeout budget fallback and return an optional log kind."""

        if budget_stale:
            limited_w = min(float(free_for_limbs_w or 0.0), 6 * 230.0 * max(1, detected_phases))
            log_kind = None if self.budget_stale_logged else "stale"
            self.budget_stale_logged = True
            self.budget_timeout_logged = False
            return limited_w, log_kind
        if budget_timeout:
            log_kind = None if self.budget_timeout_logged else "timeout"
            self.budget_timeout_logged = True
            self.budget_stale_logged = False
            return 0.0, log_kind
        self.reset_budget_log_flags()
        return float(free_for_limbs_w or 0.0), None

    def hysteresis_gate(self, name: str, value: float, start_at: float, stop_at: float, default_open: bool = False) -> bool:
        key = str(name)
        is_open = bool(self.hysteresis_gates.get(key, default_open))
        if is_open:
            if value <= stop_at:
                is_open = False
        else:
            if value >= start_at:
                is_open = True
        self.hysteresis_gates[key] = is_open
        return is_open

    def update_grid_import_budget_gate(
        self,
        *,
        grid_power_w: float,
        now_ts: float,
        threshold_w: float = 200.0,
        release_w: float = 80.0,
        hold_s: float = 25.0,
    ) -> Tuple[bool, float]:
        """Open the wallbox down-regulation gate only after sustained grid import."""

        grid_w = float(grid_power_w or 0.0)
        now = float(now_ts or 0.0)
        threshold = max(0.0, float(threshold_w or 0.0))
        release = max(0.0, min(threshold, float(release_w or 0.0)))
        hold = max(0.0, float(hold_s or 0.0))

        if grid_w <= release:
            self.grid_import_budget_since = 0.0
            self.grid_import_budget_active = False
        elif grid_w > threshold:
            if self.grid_import_budget_since <= 0.0:
                self.grid_import_budget_since = now
            if now - self.grid_import_budget_since >= hold:
                self.grid_import_budget_active = True
        elif not self.grid_import_budget_active:
            self.grid_import_budget_since = 0.0

        age_s = 0.0
        if self.grid_import_budget_since > 0.0:
            age_s = max(0.0, now - self.grid_import_budget_since)
        return bool(self.grid_import_budget_active), age_s

    def reset_min_current_import_integral(self, charger_id: int) -> None:
        key = int(charger_id or 0)
        self.min_current_import_wh.pop(key, None)
        self.min_current_import_since.pop(key, None)
        self.min_current_import_last_ts.pop(key, None)

    def update_min_current_import_integral(
        self,
        *,
        charger_id: int,
        grid_power_w: float,
        now_ts: float,
        tolerance_w: float = 200.0,
        release_w: float = 80.0,
        stop_wh: float = 40.0,
        debounce_s: float = 8.0,
        max_step_s: float = 10.0,
    ) -> Dict[str, Any]:
        """Accumulate excess grid-import energy at wallbox minimum current."""

        key = int(charger_id or 0)
        grid_w = float(grid_power_w or 0.0)
        now = float(now_ts or 0.0)
        tolerance = max(0.0, float(tolerance_w or 0.0))
        release = max(0.0, min(tolerance, float(release_w or 0.0)))
        stop_limit_wh = max(0.0, float(stop_wh or 0.0))
        debounce = max(0.0, float(debounce_s or 0.0))
        max_step = max(1.0, float(max_step_s or 1.0))

        last_ts = float(self.min_current_import_last_ts.get(key, now) or now)
        dt_s = max(0.0, min(max_step, now - last_ts))
        self.min_current_import_last_ts[key] = now

        if grid_w <= release:
            self.reset_min_current_import_integral(key)
            return {
                "stop": False,
                "wh": 0.0,
                "stable_s": 0.0,
                "counted_w": 0.0,
                "dt_s": dt_s,
            }

        wh = max(0.0, float(self.min_current_import_wh.get(key, 0.0) or 0.0))

        if grid_w <= tolerance:
            self.min_current_import_since.pop(key, None)
            if wh > 0.0 and dt_s > 0.0:
                decay_w = max(0.0, tolerance - grid_w)
                wh = max(0.0, wh - (decay_w * dt_s / 3600.0))
                self.min_current_import_wh[key] = wh
            return {
                "stop": False,
                "wh": wh,
                "stable_s": 0.0,
                "counted_w": 0.0,
                "dt_s": dt_s,
            }

        since = float(self.min_current_import_since.get(key, 0.0) or 0.0)
        if since <= 0.0:
            since = now
            self.min_current_import_since[key] = since
        stable_s = max(0.0, now - since)
        counted_w = max(0.0, grid_w - tolerance)

        if stable_s >= debounce and dt_s > 0.0:
            wh += counted_w * dt_s / 3600.0
            self.min_current_import_wh[key] = wh
        else:
            self.min_current_import_wh[key] = wh

        return {
            "stop": bool(stable_s >= debounce and wh >= stop_limit_wh),
            "wh": wh,
            "stable_s": stable_s,
            "counted_w": counted_w,
            "dt_s": dt_s,
        }

    def update_curve_forecast_wallbox_gate(
        self,
        *,
        enabled: bool,
        block_requested: bool,
        release_ready: bool,
        now_ts: float,
        release_hold_s: float = 90.0,
    ) -> Tuple[bool, float]:
        """Stop PV-curve wallbox charging immediately, release it only after stable recovery."""

        now = float(now_ts or 0.0)
        hold_s = max(0.0, float(release_hold_s or 0.0))

        if not enabled:
            self.curve_forecast_wb_block_active = False
            self.curve_forecast_wb_release_since = 0.0
            return False, 0.0

        if block_requested:
            self.curve_forecast_wb_block_active = True
            self.curve_forecast_wb_release_since = 0.0
            return True, 0.0

        if self.curve_forecast_wb_block_active:
            if release_ready:
                if self.curve_forecast_wb_release_since <= 0.0:
                    self.curve_forecast_wb_release_since = now
                age_s = max(0.0, now - self.curve_forecast_wb_release_since)
                if age_s >= hold_s:
                    self.curve_forecast_wb_block_active = False
                    self.curve_forecast_wb_release_since = 0.0
                    return False, 0.0
                return True, age_s
            self.curve_forecast_wb_release_since = 0.0
            return True, 0.0

        self.curve_forecast_wb_release_since = 0.0
        return False, 0.0

    def should_log_house_fuse_limit(self, now_ts: float, interval_s: float = 30.0) -> bool:
        if float(now_ts or 0.0) - float(self.house_fuse_log_ts or 0.0) > float(interval_s or 0.0):
            self.house_fuse_log_ts = float(now_ts or 0.0)
            return True
        return False

    def reset_predump_wallbox_gate(self):
        self.predump_wb_gate_open = False
        self.predump_wb_above_since = 0.0
        self.predump_wb_below_since = 0.0
        self.predump_wb_grid_since = 0.0

    def update_predump_wallbox_gate(
        self,
        *,
        predump_active: bool,
        has_candidate: bool,
        free_for_limbs_w: float,
        grid_power_w: float,
        detected_phases: int,
        now_ts: float,
        pause_s: float = 180.0,
        bootstrap_ready: bool = False,
        bootstrap_power_w: float = 0.0,
    ) -> Tuple[bool, bool, bool]:
        """Update the pre-dump wallbox gate without changing its semantics."""

        predump_active = bool(predump_active)
        if predump_active and self.predump_wb_pause_hold_until > now_ts:
            predump_active = False

        gate_open = False
        if predump_active:
            min_w = 6 * 230.0 * max(1, int(detected_phases or 1))
            start_w = min_w + 300.0
            stop_w = max(0.0, min_w - 500.0)
            gate_open = bool(self.predump_wb_gate_open)
            gate_start_ts = float(self.predump_wb_gate_start_ts or 0.0)

            if gate_open:
                if free_for_limbs_w < stop_w:
                    if not self.predump_wb_below_since:
                        self.predump_wb_below_since = now_ts
                else:
                    self.predump_wb_below_since = 0.0

                if grid_power_w > 700:
                    if not self.predump_wb_grid_since:
                        self.predump_wb_grid_since = now_ts
                else:
                    self.predump_wb_grid_since = 0.0

                min_hold_done = (now_ts - gate_start_ts) >= 120.0
                grid_too_long = (
                    bool(self.predump_wb_grid_since)
                    and (now_ts - self.predump_wb_grid_since) >= 35.0
                )
                # Low budget alone is intentionally not a stop reason here:
                # pre-dump is allowed to use the wallbox as a local consumer.
                if min_hold_done and grid_too_long:
                    gate_open = False
                    self.predump_wb_below_since = 0.0
                    self.predump_wb_grid_since = 0.0
            else:
                bootstrap_start_w = max(
                    min_w,
                    float(bootstrap_power_w or 0.0),
                )
                start_ready = bool(free_for_limbs_w >= start_w)
                if bootstrap_ready:
                    start_ready = bool(
                        start_ready
                        or free_for_limbs_w >= max(0.0, bootstrap_start_w - 100.0)
                    )
                if start_ready:
                    if not self.predump_wb_above_since:
                        self.predump_wb_above_since = now_ts
                else:
                    self.predump_wb_above_since = 0.0
                if (
                    bool(self.predump_wb_above_since)
                    and (now_ts - self.predump_wb_above_since) >= 20.0
                ):
                    gate_open = True
                    self.predump_wb_gate_start_ts = now_ts
                    self.predump_wb_above_since = 0.0

            self.predump_wb_gate_open = gate_open
        else:
            self.reset_predump_wallbox_gate()

        exited = bool(self.predump_wb_was_active) and not predump_active
        if exited and has_candidate:
            self.predump_wb_pause_hold_until = now_ts + max(0.0, float(pause_s or 0.0))
        self.predump_wb_was_active = bool(predump_active)
        return bool(predump_active), bool(gate_open), bool(exited)
