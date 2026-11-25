#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reine Policy für abrechnungsfeste 15-Minuten-Lastspitzenbegrenzung.

Das Modul führt weder Datei- noch Gerätezugriffe aus. Es integriert nur
vollständig belegte Netzpunktmessungen innerhalb fester Zähler-Viertelstunden
und liefert höchstens einen Kandidaten für den zentralen Storage Manager.
Aktive Spitzenkappung bleibt in E3/DC-AUTO und setzt ausschließlich eine
flüchtige Entladeobergrenze; sie fordert niemals eine Entladung oder Einspeisung
an.

Unterhalb der Speicherschwelle rechnet der Planer je Takt genau eine Zahl aus dem
Viertelstundenkonto: die Entladegrenze, 0 W zum Halten des Puffers oder den
Spielraum für das ausdrücklich freigegebene Nachladen. Die Entladegrenze folgt nur
dem gemessenen Netzbezug; oberhalb der Schwelle entlädt E3/DC-AUTO frei.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Optional

try:
    from Installer.Storage.common import safe_float, safe_int
    from Installer.storage_parallel_regulator import MODE_AUTO, MODE_GRID
except ModuleNotFoundError:
    from Storage.common import safe_float, safe_int  # type: ignore
    from storage_parallel_regulator import MODE_AUTO, MODE_GRID  # type: ignore


PEAK_SHAVING_SCHEMA = "peak_shaving_interval_v2"
PEAK_SHAVING_WINDOW_S = 15 * 60
PEAK_SHAVING_MIN_COMMAND_W = 300
PEAK_SHAVING_MIN_HYSTERESIS_W = PEAK_SHAVING_MIN_COMMAND_W + 1
PEAK_SHAVING_DEFAULT_HYSTERESIS_W = 600
# Rechentakt am Viertelstundenraster: lang gegen die Totzeit der Entladegrenze
# (ein bis drei Messrahmen) und gegen Lastrauschen, kurz gegen die Viertelstunde.
PEAK_SHAVING_STEP_S = 30


def _enabled(cfg: Dict[str, Any], key: str, default: bool = False) -> bool:
    raw = cfg.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "ja",
        "ein",
    }


def _configured_number(
    cfg: Dict[str, Any],
    key: str,
    default: float,
) -> tuple[float, bool]:
    raw = cfg.get(key, default)
    try:
        value = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return float(default), False
    return value, math.isfinite(value)


def _typed_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _config_contract(
    cfg: Dict[str, Any],
    *,
    ep_reserve_pct: float,
) -> Dict[str, Any]:
    limit_w, limit_typed = _configured_number(
        cfg,
        "peak_shaving_grid_import_limit_w",
        10_000.0,
    )
    reserve_soc, reserve_typed = _configured_number(
        cfg,
        "peak_shaving_reserve_soc_pct",
        30.0,
    )
    max_discharge_w, max_discharge_typed = _configured_number(
        cfg,
        "peak_shaving_max_discharge_w",
        0.0,
    )
    recharge_max_w, recharge_max_typed = _configured_number(
        cfg,
        "peak_shaving_recharge_max_w",
        0.0,
    )
    margin_w, margin_typed = _configured_number(
        cfg,
        "peak_shaving_control_margin_w",
        300.0,
    )
    hysteresis_w, hysteresis_typed = _configured_number(
        cfg,
        "peak_shaving_hysteresis_w",
        float(PEAK_SHAVING_DEFAULT_HYSTERESIS_W),
    )
    soc_hysteresis_pct, soc_hysteresis_typed = _configured_number(
        cfg,
        "peak_shaving_soc_hysteresis_pct",
        1.0,
    )
    max_sample_gap_s, max_sample_gap_typed = _configured_number(
        cfg,
        "peak_shaving_max_sample_gap_s",
        10.0,
    )

    errors = []
    if not limit_typed or limit_w < 1000.0:
        errors.append("grid_import_limit_invalid")
    if (
        not reserve_typed
        or reserve_soc <= float(ep_reserve_pct)
        or reserve_soc > 100.0
    ):
        errors.append("reserve_soc_invalid")
    if not max_discharge_typed or max_discharge_w < 0.0:
        errors.append("max_discharge_invalid")
    if (
        _enabled(cfg, "peak_shaving_grid_recharge_enable", False)
        and (not recharge_max_typed or recharge_max_w < 0.0)
    ):
        errors.append("recharge_max_invalid")
    if (
        not margin_typed
        or margin_w < 0.0
        or margin_w > max(0.0, limit_w - PEAK_SHAVING_MIN_COMMAND_W)
    ):
        errors.append("control_margin_invalid")
    if (
        not hysteresis_typed
        or hysteresis_w < PEAK_SHAVING_MIN_HYSTERESIS_W
    ):
        errors.append("power_hysteresis_invalid")
    if (
        not soc_hysteresis_typed
        or soc_hysteresis_pct < 0.1
        or soc_hysteresis_pct > 5.0
    ):
        errors.append("soc_hysteresis_invalid")
    if (
        not max_sample_gap_typed
        or max_sample_gap_s < 2.0
        or max_sample_gap_s > 60.0
    ):
        errors.append("sample_gap_invalid")

    return {
        "valid": not errors,
        "errors": errors,
        "grid_import_limit_w": limit_w,
        "reserve_soc_pct": reserve_soc,
        "max_discharge_w": max_discharge_w,
        "recharge_max_w": recharge_max_w,
        "control_margin_w": margin_w,
        "hysteresis_w": hysteresis_w,
        "soc_hysteresis_pct": soc_hysteresis_pct,
        "max_sample_gap_s": max_sample_gap_s,
    }


def _config_signature(cfg: Dict[str, Any]) -> str:
    keys = (
        "peak_shaving_grid_import_limit_w",
        "peak_shaving_reserve_soc_pct",
        "peak_shaving_max_discharge_w",
        "peak_shaving_grid_recharge_enable",
        "peak_shaving_recharge_max_w",
        "peak_shaving_control_margin_w",
        "peak_shaving_hysteresis_w",
        "peak_shaving_soc_hysteresis_pct",
        "peak_shaving_max_sample_gap_s",
    )
    encoded = json.dumps(
        {key: cfg.get(key) for key in keys},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _interval_start(ts_s: float) -> int:
    return int(
        math.floor(float(ts_s) / PEAK_SHAVING_WINDOW_S)
        * PEAK_SHAVING_WINDOW_S
    )


def _linear_value(start_w: float, end_w: float, ratio: float) -> float:
    bounded = max(0.0, min(1.0, float(ratio)))
    return float(start_w) + (float(end_w) - float(start_w)) * bounded


def _trapezoid_ws(start_w: float, end_w: float, duration_s: float) -> float:
    return (
        max(0.0, float(start_w))
        + max(0.0, float(end_w))
    ) * 0.5 * max(0.0, float(duration_s))


def _sample_contract(
    live: Dict[str, Any],
    *,
    now_s: float,
    externally_valid: bool,
    max_age_s: float,
) -> Dict[str, Any]:
    sample_ts = _typed_number(live.get("_ts"))
    grid_w = _typed_number(live.get("Grid_Power"))
    battery_w = _typed_number(live.get("Battery_Power"))
    soc = _typed_number(live.get("SOC"))
    age_s = (
        float(now_s) - float(sample_ts)
        if sample_ts is not None and sample_ts > 0.0
        else None
    )
    blocker = ""
    if not externally_valid:
        blocker = "manager_sample_invalid"
    elif sample_ts is None or sample_ts <= 0.0:
        blocker = "sample_timestamp_missing"
    elif age_s is None or age_s < -2.0:
        blocker = "sample_timestamp_future"
    elif age_s > max(2.0, float(max_age_s)):
        blocker = "sample_stale"
    elif grid_w is None:
        blocker = "grid_power_missing"
    elif battery_w is None:
        blocker = "battery_power_missing"
    elif soc is None or soc < 0.0 or soc > 100.0:
        blocker = "soc_invalid"
    return {
        "valid": not blocker,
        "blocker": blocker,
        "sample_ts": float(sample_ts or 0.0),
        "age_s": round(float(age_s), 3) if age_s is not None else None,
        "grid_w": grid_w,
        "battery_w": battery_w,
        "soc": soc,
    }


def _auto_discharge_cap(
    *,
    max_charge_w: int,
    max_discharge_w: int,
    heartbeat_s: float,
    reason: str,
) -> Dict[str, Any]:
    return {
        "enabled": True,
        "release": False,
        "set_power_auto": True,
        "max_charge_w": max(0, int(max_charge_w)),
        "max_discharge_w": max(0, int(max_discharge_w)),
        "discharge_start_w": 0,
        "heartbeat_s": max(1.0, float(heartbeat_s)),
        "reason": reason,
    }


def _auto_release(
    *,
    max_charge_w: int,
    max_discharge_w: int,
    heartbeat_s: float,
    reason: str,
) -> Dict[str, Any]:
    return {
        "enabled": False,
        "release": True,
        "set_power_auto": True,
        "set_power_value": 0,
        "max_charge_w": max(0, int(max_charge_w)),
        "max_discharge_w": max(0, int(max_discharge_w)),
        "discharge_start_w": 0,
        "heartbeat_s": max(1.0, float(heartbeat_s)),
        "reason": reason,
    }


def _base_context(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    *,
    now_s: float,
    ep_reserve_pct: float,
    max_charge_w: int,
    max_discharge_w: int,
    externally_valid: bool,
) -> Dict[str, Any]:
    enabled = _enabled(cfg, "peak_shaving_enable", False)
    config = _config_contract(cfg, ep_reserve_pct=ep_reserve_pct)
    max_sample_gap_s = max(
        2.0,
        min(60.0, safe_float(config.get("max_sample_gap_s"), 10.0)),
    )
    sample = _sample_contract(
        live,
        now_s=now_s,
        externally_valid=externally_valid,
        max_age_s=max_sample_gap_s,
    )
    sample_ts = safe_float(sample.get("sample_ts"), 0.0)
    interval_start = _interval_start(sample_ts) if sample_ts > 0.0 else 0
    interval_end = (
        interval_start + PEAK_SHAVING_WINDOW_S
        if interval_start > 0
        else 0
    )
    configured_limit_w = max(
        0,
        safe_int(config.get("grid_import_limit_w"), 10_000),
    )
    control_margin_w = max(
        0,
        min(
            configured_limit_w,
            safe_int(config.get("control_margin_w"), 300),
        ),
    )
    effective_limit_w = max(0, configured_limit_w - control_margin_w)
    reserve_soc_pct = max(
        float(ep_reserve_pct),
        min(100.0, safe_float(config.get("reserve_soc_pct"), 30.0)),
    )
    storage_kwh = max(
        0.1,
        safe_float(
            cfg.get("speichergroesse"),
            safe_float(live.get("bat_full_cap_kwh"), 10.0),
        ),
    )
    configured_peak_discharge_w = max(
        0,
        safe_int(config.get("max_discharge_w"), 0),
    )
    effective_peak_discharge_w = min(
        max(0, int(max_discharge_w)),
        configured_peak_discharge_w
        if configured_peak_discharge_w > 0
        else max(0, int(max_discharge_w)),
    )
    grid_w = sample.get("grid_w")
    battery_w = sample.get("battery_w")
    base_import_w = (
        max(0.0, float(grid_w) - float(battery_w))
        if grid_w is not None and battery_w is not None
        else None
    )
    usable_reserve_pct = max(0.0, reserve_soc_pct - float(ep_reserve_pct))
    return {
        "schema": PEAK_SHAVING_SCHEMA,
        "enabled": bool(enabled),
        "config_valid": bool(config.get("valid")),
        "config_errors": list(config.get("errors") or []),
        "config_signature": _config_signature(cfg),
        "updated_ts": round(sample_ts, 3) if sample_ts > 0.0 else None,
        "sample_valid": bool(sample.get("valid")),
        "sample_blocker": str(sample.get("blocker") or ""),
        "sample_age_s": sample.get("age_s"),
        "sample_ts": round(sample_ts, 3) if sample_ts > 0.0 else None,
        "interval_start_ts": interval_start or None,
        "interval_end_ts": interval_end or None,
        "window_s": PEAK_SHAVING_WINDOW_S,
        "configured_limit_w": configured_limit_w,
        "control_margin_w": control_margin_w,
        "effective_limit_w": effective_limit_w,
        "reserve_soc_pct": round(reserve_soc_pct, 2),
        "ep_reserve_pct": round(float(ep_reserve_pct), 2),
        "usable_reserve_pct": round(usable_reserve_pct, 2),
        "usable_reserve_kwh": round(
            storage_kwh * usable_reserve_pct / 100.0,
            3,
        ),
        "storage_capacity_kwh": round(storage_kwh, 3),
        "soc": sample.get("soc"),
        "grid_w": round(float(grid_w), 1) if grid_w is not None else None,
        "battery_w": (
            round(float(battery_w), 1)
            if battery_w is not None
            else None
        ),
        "base_import_w": (
            round(float(base_import_w), 1)
            if base_import_w is not None
            else None
        ),
        "max_charge_w": max(0, int(max_charge_w)),
        "max_discharge_w": max(0, int(max_discharge_w)),
        "peak_max_discharge_w": effective_peak_discharge_w,
        "grid_recharge_enabled": _enabled(
            cfg,
            "peak_shaving_grid_recharge_enable",
            False,
        ),
        "recharge_max_w": max(
            0,
            safe_int(config.get("recharge_max_w"), 0),
        ),
        "deadband_w": max(
            PEAK_SHAVING_MIN_HYSTERESIS_W,
            safe_int(
                config.get("hysteresis_w"),
                PEAK_SHAVING_DEFAULT_HYSTERESIS_W,
            ),
        ),
        "soc_hysteresis_pct": max(
            0.1,
            safe_float(config.get("soc_hysteresis_pct"), 1.0),
        ),
        "max_sample_gap_s": max_sample_gap_s,
        "history_status": (
            "disabled"
            if not enabled
            else "config_invalid"
            if not config.get("valid")
            else str(sample.get("blocker") or "initializing")
        ),
        "coverage_complete": False,
        "interval_tainted": False,
        "valid_coverage_s": 0.0,
        "import_ws": 0.0,
        "import_wh": 0.0,
        "last_sample_ts": None,
        "last_grid_import_w": None,
        "last_action": "disabled" if not enabled else "observe",
        "last_target_w": 0,
        "last_discharge_interval_start_ts": None,
        "step_ts": None,
        "step_import_ws": 0.0,
        "step_needed_w": None,
        "step_room_w": None,
        "allowed_remaining_import_w": None,
        "unavoidable_exceedance_w": 0,
        "grid_import_headroom_w": None,
    }


def _integrate_interval(
    context: Dict[str, Any],
    previous: Dict[str, Any],
) -> Dict[str, Any]:
    current = dict(context)
    if not current.get("enabled") or not current.get("config_valid"):
        return current

    signature = str(current.get("config_signature") or "")
    previous_valid = bool(
        isinstance(previous, dict)
        and previous.get("schema") == PEAK_SHAVING_SCHEMA
        and previous.get("enabled") is True
        and str(previous.get("config_signature") or "") == signature
    )
    for key in (
        "last_action",
        "last_target_w",
        "last_discharge_interval_start_ts",
        "step_ts",
        "step_import_ws",
        "step_needed_w",
        "step_room_w",
    ):
        if previous_valid and key in previous:
            current[key] = previous[key]

    if not current.get("sample_valid"):
        if previous_valid:
            for key in (
                "interval_start_ts",
                "interval_end_ts",
                "import_ws",
                "import_wh",
                "valid_coverage_s",
                "last_sample_ts",
                "last_grid_import_w",
            ):
                if key in previous:
                    current[key] = previous[key]
        current["coverage_complete"] = False
        current["interval_tainted"] = True
        current["history_status"] = str(
            current.get("sample_blocker") or "sample_invalid"
        )
        return current

    sample_ts = safe_float(current.get("sample_ts"), 0.0)
    grid_import_w = max(0.0, safe_float(current.get("grid_w"), 0.0))
    interval_start = safe_int(current.get("interval_start_ts"), 0)
    max_gap_s = safe_float(current.get("max_sample_gap_s"), 10.0)
    previous_sample_ts = (
        safe_float(previous.get("last_sample_ts"), 0.0)
        if previous_valid
        else 0.0
    )
    previous_grid_w = (
        max(0.0, safe_float(previous.get("last_grid_import_w"), 0.0))
        if previous_valid
        else 0.0
    )
    previous_interval_start = (
        safe_int(previous.get("interval_start_ts"), -1)
        if previous_valid
        else -1
    )
    previous_coverage = bool(
        previous.get("coverage_complete")
        and not previous.get("interval_tainted")
    ) if previous_valid else False

    if previous_valid and sample_ts < previous_sample_ts:
        current["history_status"] = "time_reversed"
        current["coverage_complete"] = False
        current["interval_tainted"] = True
    elif previous_valid and previous_interval_start == interval_start:
        current["import_ws"] = max(
            0.0,
            safe_float(previous.get("import_ws"), 0.0),
        )
        current["valid_coverage_s"] = max(
            0.0,
            safe_float(previous.get("valid_coverage_s"), 0.0),
        )
        current["coverage_complete"] = previous_coverage
        current["interval_tainted"] = not previous_coverage
        gap_s = sample_ts - previous_sample_ts
        if gap_s < 0.0:
            current["history_status"] = "time_reversed"
            current["coverage_complete"] = False
            current["interval_tainted"] = True
        elif gap_s == 0.0:
            current["history_status"] = (
                "duplicate_sample" if previous_coverage else "history_missing"
            )
        elif gap_s > max_gap_s:
            current["history_status"] = "sample_gap"
            current["coverage_complete"] = False
            current["interval_tainted"] = True
        elif previous_coverage:
            current["import_ws"] += _trapezoid_ws(
                previous_grid_w,
                grid_import_w,
                gap_s,
            )
            current["valid_coverage_s"] = min(
                float(PEAK_SHAVING_WINDOW_S),
                safe_float(current.get("valid_coverage_s"), 0.0) + gap_s,
            )
            current["history_status"] = "complete"
        else:
            current["history_status"] = "history_missing"
    elif (
        previous_valid
        and previous_sample_ts <= interval_start <= sample_ts
        and 0.0 < sample_ts - previous_sample_ts <= max_gap_s
    ):
        gap_s = sample_ts - previous_sample_ts
        boundary_ratio = (
            (interval_start - previous_sample_ts) / gap_s
            if gap_s > 0.0
            else 1.0
        )
        boundary_w = _linear_value(
            previous_grid_w,
            grid_import_w,
            boundary_ratio,
        )
        duration_s = max(0.0, sample_ts - interval_start)
        current["import_ws"] = _trapezoid_ws(
            boundary_w,
            grid_import_w,
            duration_s,
        )
        current["valid_coverage_s"] = duration_s
        current["coverage_complete"] = True
        current["interval_tainted"] = False
        current["history_status"] = "complete"
    else:
        current["history_status"] = "history_missing"
        current["coverage_complete"] = False
        current["interval_tainted"] = True

    current["last_sample_ts"] = round(sample_ts, 3)
    current["last_grid_import_w"] = round(grid_import_w, 1)
    current["import_wh"] = round(
        max(0.0, safe_float(current.get("import_ws"), 0.0)) / 3600.0,
        6,
    )
    if not current.get("coverage_complete"):
        return current

    interval_end = safe_int(current.get("interval_end_ts"), 0)
    remaining_s = max(1.0, float(interval_end) - sample_ts)
    target_ws = (
        max(0.0, safe_float(current.get("effective_limit_w"), 0.0))
        * PEAK_SHAVING_WINDOW_S
    )
    remaining_ws = max(
        0.0,
        target_ws - safe_float(current.get("import_ws"), 0.0),
    )
    allowed_w = remaining_ws / remaining_s
    base_import_w = max(
        0.0,
        safe_float(current.get("base_import_w"), 0.0),
    )
    configured_discharge_w = max(
        0,
        safe_int(current.get("peak_max_discharge_w"), 0),
    )
    required_discharge_w = max(0.0, base_import_w - allowed_w)
    current.update({
        "remaining_s": round(remaining_s, 3),
        "target_import_wh": round(target_ws / 3600.0, 6),
        "remaining_import_wh": round(remaining_ws / 3600.0, 6),
        "allowed_remaining_import_w": max(0, int(math.floor(allowed_w))),
        "required_discharge_w": max(
            0,
            int(math.ceil(required_discharge_w)),
        ),
        "grid_import_headroom_w": max(
            0,
            int(math.floor(allowed_w - base_import_w)),
        ),
        "unavoidable_exceedance_w": max(
            0,
            int(
                math.ceil(
                    required_discharge_w - configured_discharge_w
                )
            ),
        ),
    })
    return current


def _interval_step(
    context: Dict[str, Any],
    *,
    discharge_cap_w: int,
    charge_w: float,
) -> int:
    """Führt die Entladegrenze einmal je Takt am Viertelstundenkonto nach.

    Regelgröße ist der mittlere Netzbezug des Takts gegen den zulässigen
    Restbezug. Jede Abweichung steht im Konto und wird in derselben
    Viertelstunde ausgeglichen. Ein negativer Bedarf ist der Spielraum für das
    freigegebene Nachladen; nur dafür zählt die wirksame Ladeleistung.
    """
    interval_start = safe_int(context.get("interval_start_ts"), 0)
    sample_ts = safe_float(context.get("sample_ts"), 0.0)
    step_ts = safe_float(context.get("step_ts"), 0.0)
    import_ws = safe_float(context.get("import_ws"), 0.0)
    if not interval_start <= step_ts <= sample_ts:
        # Neue Viertelstunde (oder Uhrsprung): Grenze weiterführen, Taktmittel
        # neu beginnen. Ein laufendes Nachladen behält höchstens seine Leistung,
        # ein neues beginnt erst mit dem ersten Takt.
        context["step_ts"] = float(interval_start)
        context["step_import_ws"] = 0.0
        context["step_room_w"] = (
            max(0, safe_int(context.get("last_target_w"), 0))
            if context.get("last_action") == "grid_recharge"
            else 0
        )
        return discharge_cap_w
    if (
        (sample_ts - interval_start) // PEAK_SHAVING_STEP_S
        <= (step_ts - interval_start) // PEAK_SHAVING_STEP_S
    ):
        return discharge_cap_w
    mean_import_w = (
        import_ws - safe_float(context.get("step_import_ws"), 0.0)
    ) / (sample_ts - step_ts)
    needed_w = (
        discharge_cap_w
        - charge_w
        + mean_import_w
        - safe_float(context.get("allowed_remaining_import_w"), 0.0)
    )
    context.update({
        "step_ts": sample_ts,
        "step_import_ws": import_ws,
        "step_needed_w": int(round(needed_w)),
        "step_room_w": max(0, int(math.floor(-needed_w))),
    })
    if needed_w < PEAK_SHAVING_MIN_COMMAND_W:
        return 0
    if abs(needed_w - discharge_cap_w) < safe_int(
        context.get("deadband_w"),
        PEAK_SHAVING_DEFAULT_HYSTERESIS_W,
    ):
        return discharge_cap_w
    return int(math.ceil(needed_w))


def _active_cap_decision(
    context: Dict[str, Any],
    *,
    target_w: int,
    max_charge_w: int,
    heartbeat_s: float,
) -> Dict[str, Any]:
    reason = (
        "15-Minuten-Netzbezug sanft begrenzen: %d von %d Wh bezogen, "
        "zulässiger Restbezug %d W; E3/DC-AUTO bleibt aktiv, Entladung ist "
        "auf %d W begrenzt und Einspeisung wird nicht angefordert."
        % (
            int(round(safe_float(context.get("import_wh"), 0.0))),
            int(round(safe_float(context.get("target_import_wh"), 0.0))),
            safe_int(context.get("allowed_remaining_import_w"), 0),
            target_w,
        )
    )
    return {
        "state": "peak_shaving_active",
        "mode": MODE_AUTO,
        "val": target_w,
        "priority": "peak_shaving",
        "reason": reason,
        "protected": True,
        "storage_req_w": 0,
        "budget_w": 0,
        "peak_shaving_action_class": "active_cap",
        "peak_shaving_active": True,
        "peak_shaving_target_w": target_w,
        "peak_shaving_no_export_contract": True,
        "auto_limit": _auto_discharge_cap(
            max_charge_w=max_charge_w,
            max_discharge_w=target_w,
            heartbeat_s=heartbeat_s,
            reason=reason,
        ),
    }


def _reserve_hold_decision(
    context: Dict[str, Any],
    *,
    max_charge_w: int,
    heartbeat_s: float,
) -> Dict[str, Any]:
    reason = (
        "Lastspitzenpuffer halten: Die %.1f%%-SoC-Schwelle ist erreicht. "
        "Laden bleibt offen; Entladen ist bis zum nächsten echten "
        "Lastspitzenbedarf auf 0 W begrenzt. Nutzbarer Puffer bis zur "
        "physischen Notstromreserve: %.1f Prozentpunkte."
        % (
            safe_float(context.get("reserve_soc_pct"), 30.0),
            safe_float(context.get("usable_reserve_pct"), 0.0),
        )
    )
    if (
        context.get("coverage_complete")
        and safe_float(context.get("effective_limit_w"), 0.0)
        < safe_float(context.get("grid_w"), 0.0)
        <= safe_float(context.get("allowed_remaining_import_w"), 0.0)
    ):
        reason += (
            " Der Netzbezug liegt über der Grenze abzüglich Sicherheitsabstand "
            "(%d W); die laufende Viertelstunde reicht noch (%d von %d Wh)."
            % (
                safe_int(context.get("effective_limit_w"), 0),
                int(round(safe_float(context.get("import_wh"), 0.0))),
                int(round(safe_float(context.get("target_import_wh"), 0.0))),
            )
        )
    recharge_blocker = str(context.get("grid_recharge_blocker") or "")
    if recharge_blocker == "pv_source_invalid":
        reason += " PV-Daten fehlen; eine Netz-Nachladung bleibt gesperrt."
    elif recharge_blocker == "pv_first":
        reason += " PV kann den Puffer lokal füllen; Netz-Nachladung bleibt gesperrt."
    return {
        "state": "peak_shaving_reserve_hold",
        "mode": MODE_AUTO,
        "val": max(0, int(max_charge_w)),
        "priority": "peak_shaving",
        "reason": reason,
        "protected": True,
        "storage_req_w": 0,
        "budget_w": 0,
        "peak_shaving_action_class": "reserve_hold",
        "peak_shaving_active": False,
        "peak_shaving_reserve_hold": True,
        "auto_limit": _auto_discharge_cap(
            max_charge_w=max_charge_w,
            max_discharge_w=0,
            heartbeat_s=heartbeat_s,
            reason=reason,
        ),
    }


def _grid_recharge_decision(
    context: Dict[str, Any],
    *,
    charge_w: int,
) -> Dict[str, Any]:
    reason = (
        "Lastspitzenpuffer ausdrücklich aus dem Netz nachladen: %d W "
        "innerhalb des verbleibenden Viertelstunden- und "
        "Hausanschlussrahmens."
        % charge_w
    )
    return {
        "state": "peak_shaving_recharge",
        "mode": MODE_GRID,
        "val": charge_w,
        "priority": "peak_shaving",
        "reason": reason,
        "protected": True,
        "storage_req_w": charge_w,
        "budget_w": 0,
        "peak_shaving_action_class": "grid_recharge",
        "peak_shaving_active": True,
        "peak_shaving_recharge_active": True,
        "peak_shaving_target_w": charge_w,
        "peak_shaving_grid_recharge_authorized": True,
        "peak_shaving_interval_room_w": safe_int(
            context.get("step_room_w"),
            0,
        ),
        "peak_shaving_house_connection_room_w": safe_int(
            context.get("house_grid_charge_room_w"),
            0,
        ),
        "peak_shaving_history_complete": True,
    }


def _release_decision(
    context: Dict[str, Any],
    *,
    max_charge_w: int,
    max_discharge_w: int,
    heartbeat_s: float,
) -> Dict[str, Any]:
    reason = (
        "Lastspitzeneingriff beendet: flüchtige Leistungsgrenzen werden "
        "freigegeben; E3/DC-AUTO übernimmt."
    )
    return {
        "state": "peak_shaving_release",
        "mode": MODE_AUTO,
        "val": max(0, int(max_charge_w)),
        "priority": "peak_shaving",
        "reason": reason,
        "protected": True,
        "storage_req_w": 0,
        "budget_w": 0,
        "peak_shaving_action_class": "release",
        "peak_shaving_active": False,
        "auto_limit": _auto_release(
            max_charge_w=max_charge_w,
            max_discharge_w=max_discharge_w,
            heartbeat_s=heartbeat_s,
            reason=reason,
        ),
    }


def evaluate_peak_shaving(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    previous: Optional[Dict[str, Any]],
    *,
    now_s: float,
    ep_reserve_pct: float,
    max_charge_w: int,
    max_discharge_w: int,
    sample_valid: bool,
    pv_source_valid: bool,
    heartbeat_s: float,
    house_grid_charge_room_w: Optional[int] = None,
) -> Dict[str, Any]:
    """Berechnet Intervallzustand und höchstens einen Storage-Kandidaten."""

    previous_context = previous if isinstance(previous, dict) else {}
    previous_action = (
        str(previous_context.get("last_action") or "")
        if previous_context.get("schema") == PEAK_SHAVING_SCHEMA
        else ""
    )
    previous_owned_frame = previous_action in {
        "active_cap",
        "reserve_hold",
        "grid_recharge",
    }
    context = _base_context(
        cfg,
        live,
        now_s=float(now_s),
        ep_reserve_pct=float(ep_reserve_pct),
        max_charge_w=max_charge_w,
        max_discharge_w=max_discharge_w,
        externally_valid=bool(sample_valid),
    )
    context["pv_source_valid"] = bool(pv_source_valid)
    context["house_grid_charge_room_w"] = (
        max(0, int(house_grid_charge_room_w))
        if house_grid_charge_room_w is not None
        else None
    )
    context = _integrate_interval(
        context,
        previous_context,
    )
    if not context.get("enabled"):
        if previous_owned_frame:
            context["last_action"] = "release"
            context["last_target_w"] = 0
            return {
                "context": context,
                "decision": _release_decision(
                    context,
                    max_charge_w=max_charge_w,
                    max_discharge_w=max_discharge_w,
                    heartbeat_s=heartbeat_s,
                ),
            }
        return {"context": context, "decision": None}
    if not context.get("config_valid"):
        context["history_status"] = "config_invalid"
        context["last_action"] = (
            "release" if previous_owned_frame else "config_invalid"
        )
        if previous_owned_frame:
            context["last_target_w"] = 0
            return {
                "context": context,
                "decision": _release_decision(
                    context,
                    max_charge_w=max_charge_w,
                    max_discharge_w=max_discharge_w,
                    heartbeat_s=heartbeat_s,
                ),
            }
        return {"context": context, "decision": None}
    if not context.get("sample_valid"):
        if previous_owned_frame:
            context["last_action"] = "release"
            context["last_target_w"] = 0
            return {
                "context": context,
                "decision": _release_decision(
                    context,
                    max_charge_w=max_charge_w,
                    max_discharge_w=max_discharge_w,
                    heartbeat_s=heartbeat_s,
                ),
            }
        return {"context": context, "decision": None}

    soc = safe_float(context.get("soc"), 0.0)
    reserve_soc = safe_float(context.get("reserve_soc_pct"), 30.0)
    physical_reserve = safe_float(context.get("ep_reserve_pct"), 8.0)
    soc_hysteresis = safe_float(
        context.get("soc_hysteresis_pct"),
        1.0,
    )
    previous_action = str(context.get("last_action") or "")
    previous_active = previous_action == "active_cap"
    reserve_hold_active = bool(
        soc <= reserve_soc
        or (
            previous_action in {"active_cap", "reserve_hold", "grid_recharge"}
            and soc < reserve_soc + soc_hysteresis
        )
    )

    if not context.get("coverage_complete"):
        if reserve_hold_active:
            context["last_action"] = "reserve_hold"
            context["last_target_w"] = 0
            return {
                "context": context,
                "decision": _reserve_hold_decision(
                    context,
                    max_charge_w=max_charge_w,
                    heartbeat_s=heartbeat_s,
                ),
            }
        if previous_active:
            context["last_action"] = "release"
            context["last_target_w"] = 0
            return {
                "context": context,
                "decision": _release_decision(
                    context,
                    max_charge_w=max_charge_w,
                    max_discharge_w=max_discharge_w,
                    heartbeat_s=heartbeat_s,
                ),
            }
        context["last_action"] = "observe"
        context["last_target_w"] = 0
        return {"context": context, "decision": None}

    grid_w = safe_float(context.get("grid_w"), 0.0)
    battery_w = safe_float(context.get("battery_w"), 0.0)
    previous_target_w = max(0, safe_int(context.get("last_target_w"), 0))
    target_w = min(
        _interval_step(
            context,
            discharge_cap_w=previous_target_w if previous_active else 0,
            charge_w=(
                min(previous_target_w, max(0.0, battery_w))
                if previous_action == "grid_recharge"
                else 0.0
            ),
        ),
        max(0, int(max_discharge_w)),
        max(0, safe_int(context.get("peak_max_discharge_w"), 0)),
    )
    if (
        reserve_hold_active
        and soc > physical_reserve + 0.2
        and target_w >= PEAK_SHAVING_MIN_COMMAND_W
    ):
        context["last_action"] = "active_cap"
        context["last_target_w"] = target_w
        context["last_discharge_interval_start_ts"] = context.get(
            "interval_start_ts"
        )
        return {
            "context": context,
            "decision": _active_cap_decision(
                context,
                target_w=target_w,
                max_charge_w=max_charge_w,
                heartbeat_s=heartbeat_s,
            ),
        }

    below_recharge_threshold = bool(
        soc < reserve_soc - soc_hysteresis
        or (
            previous_action == "grid_recharge"
            and soc < reserve_soc
        )
    )
    recharge_enabled = bool(context.get("grid_recharge_enabled"))
    same_discharge_interval = (
        context.get("last_discharge_interval_start_ts") is not None
        and safe_int(context.get("last_discharge_interval_start_ts"), -1)
        == safe_int(context.get("interval_start_ts"), -2)
    )
    pv_w = _typed_number(live.get("PV_Power"))
    home_w = _typed_number(live.get("Home_Power"))
    pv_first_block = bool(
        not pv_source_valid
        or pv_w is None
        or home_w is None
        or (
            grid_w <= 100.0
            and (
                battery_w > 100.0
                or pv_w >= home_w + PEAK_SHAVING_MIN_COMMAND_W
            )
        )
    )
    if not pv_source_valid or pv_w is None or home_w is None:
        context["grid_recharge_blocker"] = "pv_source_invalid"
    elif pv_first_block:
        context["grid_recharge_blocker"] = "pv_first"
    else:
        context["grid_recharge_blocker"] = None
    house_room_w = context.get("house_grid_charge_room_w")
    # Spielraum aus demselben Takt; der Beginn braucht wie jede Änderung der
    # Grenze mindestens das Totband.
    interval_room_w = max(0, safe_int(context.get("step_room_w"), 0))
    if (
        below_recharge_threshold
        and recharge_enabled
        and interval_room_w >= (
            PEAK_SHAVING_MIN_COMMAND_W
            if previous_action == "grid_recharge"
            else safe_int(
                context.get("deadband_w"),
                PEAK_SHAVING_DEFAULT_HYSTERESIS_W,
            )
        )
        and not same_discharge_interval
        and not pv_first_block
        and house_room_w is not None
    ):
        recharge_max_w = max(
            0,
            safe_int(context.get("recharge_max_w"), 0),
        )
        if recharge_max_w <= 0:
            recharge_max_w = max(0, int(max_charge_w))
        charge_w = min(
            max(0, int(max_charge_w)),
            recharge_max_w,
            interval_room_w,
            max(0, int(house_room_w)),
        )
        if charge_w >= PEAK_SHAVING_MIN_COMMAND_W:
            context["last_action"] = "grid_recharge"
            context["last_target_w"] = charge_w
            return {
                "context": context,
                "decision": _grid_recharge_decision(
                    context,
                    charge_w=charge_w,
                ),
            }

    if reserve_hold_active:
        context["last_action"] = "reserve_hold"
        context["last_target_w"] = 0
        return {
            "context": context,
            "decision": _reserve_hold_decision(
                context,
                max_charge_w=max_charge_w,
                heartbeat_s=heartbeat_s,
            ),
        }

    if previous_action in {"active_cap", "reserve_hold", "grid_recharge"}:
        context["last_action"] = "release"
        context["last_target_w"] = 0
        return {
            "context": context,
            "decision": _release_decision(
                context,
                max_charge_w=max_charge_w,
                max_discharge_w=max_discharge_w,
                heartbeat_s=heartbeat_s,
            ),
        }

    context["last_action"] = "idle"
    context["last_target_w"] = 0
    return {"context": context, "decision": None}
