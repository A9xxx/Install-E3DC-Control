"""Reine Pre-Dump-Rechnung; keine Geräte, Dateien oder Dienstimporte."""
from __future__ import annotations
from typing import Any, Dict, Optional
try:
    from Installer.data_models import LiveDataSnapshot
    from Installer.Storage.leistungsleser import safe_float
except ModuleNotFoundError:
    from data_models import LiveDataSnapshot
    from Storage.leistungsleser import safe_float


def cfg_bool(cfg: Dict[str, Any], key: str, default: bool = False) -> bool:
    raw = cfg.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "ja", "ein")



def predump_plan_is_hard(plan: Dict[str, Any]) -> bool:
    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    reason = str(plan.get("predump_reason", meta.get("predump_reason", "")) or "")
    return bool(
        plan.get("hard_predump_enabled", meta.get("hard_predump_enabled", False))
        or reason.startswith("Hard-Pre-Dump")
    )



def _predump_timestamp_s(value: Any) -> float:
    raw = safe_float(value, 0.0)
    return raw / 1000.0 if raw > 10000000000.0 else raw



def predump_time_window(cfg: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    """Löst genau einen fehlersicheren Pre-Dump-Zeitvertrag auf.

    ``predump_end_ts`` ist autoritativ. ``ladestart_ts`` bleibt nur als
    Kompatibilitätsrückfall für Pläne ohne explizit veröffentlichtes Ende erhalten.
    """
    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    start_raw = plan.get("predump_start_ts")
    if safe_float(start_raw, 0.0) <= 0.0:
        start_raw = meta.get("predump_start_ts")
    explicit_end_raw = plan.get("predump_end_ts")
    if safe_float(explicit_end_raw, 0.0) <= 0.0:
        explicit_end_raw = meta.get("predump_end_ts")
    legacy_end_raw = plan.get("ladestart_ts")

    start_s = _predump_timestamp_s(start_raw)
    explicit_end_s = _predump_timestamp_s(explicit_end_raw)
    legacy_end_s = _predump_timestamp_s(legacy_end_raw)
    explicit_start = start_s > 0.0
    deadline_s = explicit_end_s if explicit_end_s > 0.0 else legacy_end_s
    deadline_source = "predump_end_ts" if explicit_end_s > 0.0 else "ladestart_ts_legacy_fallback"
    legacy_ignored = bool(
        explicit_end_s > 0.0
        and legacy_end_s > 0.0
        and abs(explicit_end_s - legacy_end_s) > 1.0
    )

    base = {
        "start_ts": start_s,
        "deadline_ts": deadline_s,
        "deadline_source": deadline_source,
        "legacy_ladestart_ts": legacy_end_s or None,
        "legacy_deadline_ignored": legacy_ignored,
    }
    if deadline_s <= 0.0:
        return {
            **base,
            "valid": False,
            "status": "invalid",
            "reason_code": "predump_deadline_missing",
            "reason": "Pre-Dump-Zeitvertrag ohne Deadline",
        }
    if explicit_start and start_s >= deadline_s:
        return {
            **base,
            "valid": False,
            "status": "invalid",
            "reason_code": "predump_window_order_invalid",
            "reason": "Pre-Dump-Zeitvertrag ungültig: Start liegt nicht vor Ende",
        }
    if not explicit_start:
        window_h_raw = safe_float(cfg.get("pd_max_hours"), 5.0)
        window_h = window_h_raw if window_h_raw > 0.0 else 5.0
        start_s = deadline_s - max(0.25, window_h) * 3600.0
        base["start_ts"] = start_s
        base["start_source"] = "pd_max_hours_fallback"
    else:
        base["start_source"] = "predump_start_ts"
    return {
        **base,
        "valid": True,
        "status": "valid",
        "reason_code": "predump_time_window_valid",
        "reason": "Pre-Dump-Zeitvertrag gültig",
    }



def predump_trajectory_state(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    now_s: float,
    target_soc: float,
    planned_dump_wh: float,
    landing_floor_soc: Optional[float] = None,
    *, hysteresis_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """Return the Pre-Dump landing ramp state for the configured start window."""
    snapshot = LiveDataSnapshot.from_dict(live)
    if not snapshot.soc_valid:
        return {"valid": False, "phase": "invalid", "reason": "Ladestand unbekannt"}
    capacity_wh = safe_float(plan.get("battery_capacity"), safe_float(cfg.get("speichergroesse"), 10.0) * 1000.0)
    if capacity_wh <= 0.0 or target_soc < 0.0 or planned_dump_wh < 200.0:
        return {}

    time_window = predump_time_window(cfg, plan)
    start_s = safe_float(time_window.get("start_ts"), 0.0)
    end_s = safe_float(time_window.get("deadline_ts"), 0.0)
    if not time_window.get("valid"):
        return {**time_window, "phase": "invalid"}

    if now_s < start_s:
        return {
            **time_window,
            "phase": "waiting",
            "reason": "Pre-Dump wartet auf Startfenster",
        }
    if now_s >= end_s:
        return {
            **time_window,
            "phase": "expired",
            "reason": "Pre-Dump-Fenster ist abgelaufen",
        }

    soc = snapshot.soc
    hysteresis = (max(0.3, safe_float(cfg.get("pd_traj_hyst"), 0.7))
                  if hysteresis_pct is None else hysteresis_pct)
    stop_soc = target_soc + hysteresis
    if landing_floor_soc is not None:
        landing_floor_soc = max(0.0, min(100.0, float(landing_floor_soc)))
        if landing_floor_soc < target_soc:
            stop_soc = landing_floor_soc
    start_soc = max(0.0, min(100.0, target_soc + planned_dump_wh * 100.0 / capacity_wh))
    progress = max(0.0, min(1.0, (now_s - start_s) / max(300.0, end_s - start_s)))
    trajectory_soc = start_soc + (target_soc - start_soc) * progress
    remaining_h = max(0.25, (end_s - now_s) / 3600.0)
    remaining_wh = max(0.0, (soc - min(target_soc, stop_soc)) * capacity_wh / 100.0)
    required_w = remaining_wh / remaining_h if remaining_h > 0.0 else 0.0

    phase = "active"
    if soc <= stop_soc:
        phase = "done"
    elif soc < trajectory_soc - hysteresis:
        phase = "ahead"

    return {
        **time_window,
        "phase": phase,
        "start_soc": round(start_soc, 2),
        "trajectory_soc": round(trajectory_soc, 2),
        "target_soc": round(target_soc, 2),
        "remaining_wh": round(remaining_wh, 0),
        "required_w": max(0, int(required_w)),
        "hysteresis_pct": hysteresis,
        "landing_floor_soc": round(stop_soc, 2) if stop_soc < target_soc else None,
        "hours_remaining": round(remaining_h, 2),
    }



def predump_wallbox_block_window(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    plan: Dict[str, Any],
    predump: Dict[str, Any],
    now_s: float,
    max_discharge_w: int,
    wallbox_min_power_w: int,
    *, start_buffer_s: Optional[float] = None,
) -> Dict[str, Any]:
    snapshot = LiveDataSnapshot.from_dict(live)
    if not snapshot.soc_valid:
        return {}
    target_soc = safe_float(predump.get("target_soc"), -1.0)
    capacity_wh = safe_float(plan.get("battery_capacity"), safe_float(cfg.get("speichergroesse"), 10.0) * 1000.0)
    time_window = predump_time_window(cfg, plan)
    if not time_window.get("valid"):
        return {}
    deadline_s = safe_float(time_window.get("deadline_ts"), 0.0)
    if deadline_s <= 0.0:
        deadline_s = safe_float(predump.get("deadline_ts"), 0.0)
    if target_soc < 0.0 or capacity_wh <= 0.0 or deadline_s <= now_s:
        return {}

    soc = snapshot.soc
    remaining_wh = max(0.0, (soc - target_soc) * capacity_wh / 100.0)
    if remaining_wh < 100.0:
        return {}

    window_start_s = safe_float(time_window.get("start_ts"), 0.0)

    sink_power_w = min(max(0, int(max_discharge_w)), max(0, int(wallbox_min_power_w)))
    if sink_power_w < 300:
        return {}
    buffer_s = (max(0.0, safe_float(cfg.get("predump_bev_start_buffer_s"), 120.0))
                if start_buffer_s is None else start_buffer_s)
    sink_duration_s = remaining_wh * 3600.0 / max(300.0, float(sink_power_w))
    grid_duration_s = remaining_wh * 3600.0 / max(300.0, float(max_discharge_w or sink_power_w))
    start_s = max(window_start_s, deadline_s - sink_duration_s - buffer_s)
    grid_latest_s = max(window_start_s, deadline_s - grid_duration_s - buffer_s)
    return {
        "start_ts": start_s,
        "grid_latest_ts": grid_latest_s,
        "deadline_ts": deadline_s,
        "remaining_wh": round(remaining_wh, 0),
        "power_w": int(sink_power_w),
        "duration_s": round(sink_duration_s, 1),
        "grid_duration_s": round(grid_duration_s, 1),
        "waiting": bool(now_s < start_s),
        "grid_fallback_due": bool(now_s >= grid_latest_s),
    }



def physical_headroom_gate(cfg, plan, now_s, planned_dump_wh: float) -> Dict[str, Any]:
    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    required_present = (
        "adaptive_headroom_required_wh" in plan
        or "adaptive_headroom_required_wh" in meta
    )
    preventable_present = (
        "predump_preventable_clipping_wh" in plan
        or "predump_preventable_clipping_wh" in meta
    )
    required_wh = max(0.0, safe_float(
        plan.get("adaptive_headroom_required_wh", meta.get("adaptive_headroom_required_wh"))
        if required_present
        else planned_dump_wh,
        0.0,
    ))
    preventable_wh = max(
        0.0,
        safe_float(
            plan.get("predump_preventable_clipping_wh", meta.get("predump_preventable_clipping_wh"))
            if preventable_present
            else planned_dump_wh,
            0.0,
        ),
    )
    time_window = predump_time_window(cfg, plan)
    deadline_s = safe_float(time_window.get("deadline_ts"), 0.0)
    deadline_source = time_window.get("deadline_source")
    deadline_valid = bool(time_window.get("valid") and deadline_s > now_s)
    if not deadline_valid:
        explicit_request = plan.get("predump_request") if isinstance(plan.get("predump_request"), dict) else {}
        explicit_deadline = safe_float(explicit_request.get("deadline_ts"), 0.0)
        if explicit_deadline > 100_000_000_000:
            explicit_deadline /= 1000.0
        if explicit_deadline > now_s:
            deadline_s = explicit_deadline
            deadline_source = "predump_request.deadline_ts"
            deadline_valid = True
    if not deadline_valid:
        timeline_points = sorted(
            (
                safe_float(item.get("ts"), 0.0),
                safe_float(item.get("grid_dump_w"), 0.0),
            )
            for item in plan.get("timeline") or []
            if isinstance(item, dict) and safe_float(item.get("ts"), 0.0) > 0.0
        )
        for index, (timestamp_ms, grid_dump_w) in enumerate(timeline_points):
            timestamp_s = timestamp_ms / 1000.0 if timestamp_ms > 100_000_000_000 else timestamp_ms
            next_timestamp_ms = (
                timeline_points[index + 1][0]
                if index + 1 < len(timeline_points)
                else timestamp_ms + 900_000.0
            )
            next_timestamp_s = (
                next_timestamp_ms / 1000.0
                if next_timestamp_ms > 100_000_000_000
                else next_timestamp_ms
            )
            if timestamp_s <= now_s < next_timestamp_s and grid_dump_w > 0.0:
                deadline_s = next_timestamp_s
                deadline_source = "current_timeline_slot_end"
                deadline_valid = deadline_s > now_s
                break
    residual_wh = min(max(0.0, planned_dump_wh), required_wh, preventable_wh)
    if required_wh < 200.0:
        reason = "Pre-Dump blockiert: kein residualer Headroombedarf belegt"
    elif preventable_wh < 200.0:
        reason = "Pre-Dump blockiert: keine vermeidbare Abregelenergie belegt"
    elif not deadline_valid:
        reason = "Pre-Dump blockiert: physikalische Deadline fehlt oder ist abgelaufen"
    elif residual_wh < 200.0:
        reason = "Pre-Dump blockiert: residualer Headroom unter Mindestschwelle"
    else:
        reason = ""
    return {
        "allowed": not reason,
        "reason": reason,
        "required_wh": required_wh,
        "preventable_wh": preventable_wh,
        "residual_wh": residual_wh,
        "deadline_ts": deadline_s or None,
        "deadline_source": deadline_source,
        "required_source": "adaptive_headroom_required_wh" if required_present else "legacy_predump_dump_wh",
        "preventable_source": "predump_preventable_clipping_wh" if preventable_present else "legacy_predump_dump_wh",
        "accounting_contract": "RESIDUAL_HEADROOM_ONLY_NOT_DV_SALE",
    }



def adaptive_gate(cfg, plan, now_s, soc, target_soc: float, planned_dump_wh: float, hard_predump: bool) -> Dict[str, Any]:
    meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
    pressure = physical_headroom_gate(cfg, plan, now_s, planned_dump_wh)
    if not pressure.get("allowed"):
        return pressure
    if hard_predump:
        headroom_target_soc = safe_float(
            plan.get("adaptive_headroom_target_soc", meta.get("adaptive_headroom_target_soc")),
            -1.0,
        )
        if 0.0 <= headroom_target_soc < target_soc - 0.05:
            capacity_wh = safe_float(
                plan.get("battery_capacity"),
                safe_float(cfg.get("speichergroesse"), 10.0) * 1000.0,
            )
            soc = max(0.0, min(100.0, soc))
            min_soc = safe_float(
                plan.get(
                    "predump_min_soc",
                    meta.get("predump_min_soc", cfg.get("storage_predump_min_soc", cfg.get("eco_dump_min_soc"))),
                ),
                0.0,
            )
            effective_target_soc = max(0.0, min(target_soc, max(min_soc, headroom_target_soc)))
            effective_dump_wh = max(0.0, planned_dump_wh)
            if capacity_wh > 0.0 and soc > effective_target_soc:
                effective_dump_wh = max(
                    effective_dump_wh,
                    (soc - effective_target_soc) * capacity_wh / 100.0,
                )
            effective_dump_wh = min(
                effective_dump_wh,
                safe_float(pressure.get("residual_wh"), 0.0),
            )
            if capacity_wh > 0.0:
                effective_target_soc = max(
                    effective_target_soc,
                    soc - effective_dump_wh * 100.0 / capacity_wh,
                )
            return {
                **pressure,
                "allowed": True,
                "target_soc": effective_target_soc,
                "planned_dump_wh": effective_dump_wh,
                "adaptive_headroom_target_soc": headroom_target_soc,
                "reason": (
                    "Hard-Pre-Dump auf residualen Abregel-Headroom %.0fWh / %.1f%% begrenzt"
                    % (effective_dump_wh, effective_target_soc)
                ),
            }
        return {
            **pressure,
            "allowed": True,
            "target_soc": target_soc,
            "planned_dump_wh": min(
                max(0.0, planned_dump_wh),
                safe_float(pressure.get("residual_wh"), 0.0),
            ),
            "reason": "",
        }

    evening_shortfall_wh = max(
        0.0,
        safe_float(plan.get("evening_shortfall_wh", meta.get("evening_shortfall_wh")), 0.0),
    )
    if evening_shortfall_wh >= 200.0:
        return {
            "allowed": False,
            "reason": "Pre-Dump blockiert: Abendziel-Risiko %.0fWh" % evening_shortfall_wh,
        }

    required_present = (
        "adaptive_headroom_required_wh" in plan
        or "adaptive_headroom_required_wh" in meta
    )
    adaptive_required_wh = max(
        0.0,
        safe_float(
            plan.get("adaptive_headroom_required_wh", meta.get("adaptive_headroom_required_wh")),
            0.0,
        ),
    )
    if required_present and adaptive_required_wh < 200.0:
        return {
            "allowed": False,
            "reason": "Pre-Dump blockiert: kein zusätzlicher Headroom nötig",
        }

    effective_dump_wh = max(0.0, planned_dump_wh)
    cap_reason = ""
    if effective_dump_wh < 200.0 and not required_present:
        return {
            "allowed": True,
            "target_soc": target_soc,
            "planned_dump_wh": effective_dump_wh,
            "adaptive_headroom_required_wh": None,
            "evening_shortfall_wh": evening_shortfall_wh,
            "reason": "",
        }
    if effective_dump_wh < 200.0 and required_present and adaptive_required_wh >= 200.0:
        effective_dump_wh = adaptive_required_wh
        cap_reason = "adaptiver Zusatz-Headroom %.0fWh" % adaptive_required_wh
    if required_present:
        capped_dump_wh = min(effective_dump_wh, adaptive_required_wh)
        if capped_dump_wh < effective_dump_wh - 50.0:
            cap_reason = (
                "adaptiv begrenzt %.0fWh -> %.0fWh"
                % (effective_dump_wh, capped_dump_wh)
            )
        effective_dump_wh = capped_dump_wh
    pressure_capped_wh = min(
        effective_dump_wh,
        safe_float(pressure.get("residual_wh"), 0.0),
    )
    if pressure_capped_wh < effective_dump_wh - 50.0:
        cap_reason = "residual begrenzt %.0fWh -> %.0fWh" % (
            effective_dump_wh,
            pressure_capped_wh,
        )
    effective_dump_wh = pressure_capped_wh

    if effective_dump_wh < 200.0:
        return {
            "allowed": False,
            "reason": "Pre-Dump blockiert: Zusatz-Headroom unter Mindestschwelle",
        }

    capacity_wh = safe_float(
        plan.get("battery_capacity"),
        safe_float(cfg.get("speichergroesse"), 10.0) * 1000.0,
    )
    soc = soc
    effective_target_soc = target_soc
    if capacity_wh > 0.0 and effective_dump_wh < planned_dump_wh - 50.0:
        effective_target_soc = max(
            target_soc,
            max(0.0, min(100.0, soc)) - (effective_dump_wh * 100.0 / capacity_wh),
        )

    return {
        **pressure,
        "allowed": True,
        "target_soc": max(0.0, min(100.0, effective_target_soc)),
        "planned_dump_wh": effective_dump_wh,
        "adaptive_headroom_required_wh": adaptive_required_wh if required_present else None,
        "evening_shortfall_wh": evening_shortfall_wh,
        "reason": cap_reason,
    }

