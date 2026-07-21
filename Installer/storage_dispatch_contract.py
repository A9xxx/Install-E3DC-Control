#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Versionierter, deterministischer Storage-Dispatchvertrag.

Das Modul adaptiert den bestehenden Simulatorplan und bindet bereits getroffene
Managerentscheidungen diagnostisch daran. Es sendet keine Hardwarebefehle.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import stat
import threading
import time
from bisect import bisect_left
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo


PLAN_SCHEMA = "storage_dispatch_plan_v1"
RUNTIME_SCHEMA = "storage_dispatch_runtime_v1"
ADAPTER_VERSION = "legacy_storage_plan_adapter_v1"
SHADOW_INPUT_BINDING_SCHEMA = "storage_dispatch_shadow_input_binding_v2"
PRICE_HORIZON_SCHEMA = "storage_dispatch_price_horizon_v2"
DIRECT_MARKETING_PLAN_PROJECTION_SCHEMA = "direct_marketing_plan_projection_v1"
TIMEZONE = "Europe/Berlin"
SLOT_DURATION_S = 900
ACTIVE_ACTIONS = {"PV_STORE", "GRID_CHARGE", "ECONOMIC_EXPORT", "HEADROOM_EXPORT", "HOUSE_SUPPLY"}

_PLAN_SNAPSHOT_MAX_BYTES = 16 * 1024 * 1024
_PLAN_SNAPSHOT_CACHE_LIMIT = 16
_PLAN_SNAPSHOT_CACHE = OrderedDict()
_PLAN_SNAPSHOT_LOCK = threading.Lock()

_PLAN_MATERIAL_KEYS = (
    "schema_version",
    "generated_at",
    "generated_at_ts_ms",
    "valid_from",
    "valid_from_ts_ms",
    "valid_until",
    "valid_until_ts_ms",
    "horizon_end",
    "horizon_end_ts_ms",
    "timezone",
    "slot_duration_s",
    "input_revisions",
    "planner",
    "slots",
    "pv_topology",
    "headroom_topology",
    "terminal_value",
    "shadow_dispatch",
    "compatibility",
    "direct_marketing",
)


class _ImmutablePlanList(list):
    """Lazy JSON-kompatible Sequenzsicht ohne eager Deep-Freeze-Kopie."""

    def __getitem__(self, index: Any) -> Any:
        value = list.__getitem__(self, index)
        if isinstance(index, slice):
            return _ImmutablePlanList(value)
        return _freeze_plan_value(value)

    def __iter__(self):
        for value in list.__iter__(self):
            yield _freeze_plan_value(value)

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("validated canonical plan snapshot is immutable")

    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = _immutable
    __iadd__ = __imul__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, _memo):
        return self


class ValidatedCanonicalPlanSnapshot(dict):
    """Einmal statisch validierte, tief unveränderliche Planrevision."""

    def __init__(
        self,
        plan: Dict[str, Any],
        *,
        static_validation: Dict[str, Any],
        source_path: Optional[str],
        source_generation: Optional[Tuple[int, int, int, int]],
        source_sha256: str,
    ) -> None:
        # Die Planladegrenze besitzt bereits eine unabhängige JSON-Revision.
        # Nur die Wurzel wird flach übernommen; verschachtelte Views entstehen
        # lazy beim Zugriff und sind ebenfalls mutationsgesperrt.
        dict.__init__(self, plan)
        object.__setattr__(self, "static_validation", _frozen_plan_mapping(static_validation))
        object.__setattr__(self, "source_path", source_path)
        object.__setattr__(self, "source_generation", source_generation)
        object.__setattr__(self, "source_sha256", source_sha256)
        object.__setattr__(self, "_dynamic_validation_cache", {})

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("validated canonical plan snapshot is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable
    __ior__ = _immutable

    def __getitem__(self, key: str) -> Any:
        return _freeze_plan_value(dict.__getitem__(self, key))

    def get(self, key: str, default: Any = None) -> Any:
        return _freeze_plan_value(dict.get(self, key, default))

    def items(self):
        for key, value in dict.items(self):
            yield key, _freeze_plan_value(value)

    def values(self):
        for value in dict.values(self):
            yield _freeze_plan_value(value)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise TypeError("validated canonical plan snapshot is immutable")

    def __delattr__(self, _name: str) -> None:
        raise TypeError("validated canonical plan snapshot is immutable")

    def __copy__(self):
        return self

    def __deepcopy__(self, _memo):
        return self


def _freeze_plan_value(value: Any) -> Any:
    if isinstance(value, (ValidatedCanonicalPlanSnapshot, _ImmutablePlanList)):
        return value
    if isinstance(value, dict):
        frozen = ValidatedCanonicalPlanSnapshot.__new__(ValidatedCanonicalPlanSnapshot)
        dict.__init__(frozen, value)
        return frozen
    if isinstance(value, (list, tuple)):
        return _ImmutablePlanList(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError("canonical plan contains a non-JSON or non-finite value")


def _frozen_plan_mapping(value: Dict[str, Any]) -> ValidatedCanonicalPlanSnapshot:
    frozen = ValidatedCanonicalPlanSnapshot.__new__(ValidatedCanonicalPlanSnapshot)
    dict.__init__(frozen, value)
    return frozen


def _plan_file_generation(info: os.stat_result) -> Tuple[int, int, int, int]:
    return (int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns))


def _safe_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    number = _safe_float(value, None)
    return int(round(number)) if number is not None else int(default)


def _safe_bool(value: Any, default: Optional[bool] = None) -> Optional[bool]:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "ja"}:
        return True
    if text in {"0", "false", "no", "off", "nein"}:
        return False
    return default


def _round_or_none(value: Any, digits: int = 3) -> Optional[float]:
    number = _safe_float(value, None)
    return round(number, digits) if number is not None else None


def _canonical_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def canonical_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        text = json.dumps(
            _canonical_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    return text.encode("utf-8")


def revision_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _to_ts_ms(value: Any) -> int:
    number = _safe_float(value, 0.0) or 0.0
    if number <= 0:
        return 0
    if number < 100_000_000_000:
        number *= 1000.0
    return int(round(number))


def _parse_generated_ts_ms(value: Any, fallback_ms: int) -> int:
    if isinstance(value, (int, float)):
        parsed = _to_ts_ms(value)
        return parsed or fallback_ms
    text = str(value or "").strip()
    if not text:
        return fallback_ms
    try:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed_dt = dt.datetime.fromisoformat(normalized)
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=ZoneInfo(TIMEZONE))
        return int(round(parsed_dt.timestamp() * 1000.0))
    except (TypeError, ValueError, OverflowError):
        return fallback_ms


def _utc_iso(ts_ms: int) -> str:
    value = dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=dt.timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _local_time_fields(ts_ms: int) -> Dict[str, Any]:
    local = dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=dt.timezone.utc).astimezone(ZoneInfo(TIMEZONE))
    offset = local.utcoffset()
    return {
        "local_start": local.isoformat(timespec="seconds"),
        "utc_offset_s": int(offset.total_seconds()) if offset is not None else 0,
        "fold": int(local.fold),
    }


def _timeline_rows(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = plan.get("timeline") if isinstance(plan.get("timeline"), list) else []
    valid = [dict(row) for row in rows if isinstance(row, dict) and _to_ts_ms(row.get("ts")) > 0]
    return sorted(valid, key=lambda row: _to_ts_ms(row.get("ts")))


def _window_for_ts(windows: Iterable[Any], ts_ms: int) -> Dict[str, Any]:
    for item in windows:
        if not isinstance(item, dict):
            continue
        start_ms = _to_ts_ms(item.get("start_ts"))
        end_ms = _to_ts_ms(item.get("end_ts"))
        if start_ms <= ts_ms < end_ms:
            return item
    return {}


def _curve_points(target_timeline: Iterable[Any]) -> List[Tuple[int, float]]:
    points = []
    for item in target_timeline:
        if not isinstance(item, dict):
            continue
        point_ts = _to_ts_ms(item.get("ts"))
        point_soc = _safe_float(item.get("soc"), None)
        if point_ts > 0 and point_soc is not None:
            points.append((point_ts, point_soc))
    points.sort(key=lambda item: item[0])
    return points


def _curve_soc_at_points(points: List[Tuple[int, float]], ts_ms: int) -> Optional[float]:
    if not points:
        return None
    if ts_ms <= points[0][0]:
        return round(points[0][1], 3)
    if ts_ms >= points[-1][0]:
        return round(points[-1][1], 3)
    index = bisect_left(points, (ts_ms, float("-inf")))
    previous = points[index - 1]
    current = points[index]
    width = max(1, current[0] - previous[0])
    ratio = (ts_ms - previous[0]) / width
    return round(previous[1] + (current[1] - previous[1]) * ratio, 3)


def _curve_soc_at(target_timeline: Iterable[Any], ts_ms: int) -> Optional[float]:
    return _curve_soc_at_points(_curve_points(target_timeline), ts_ms)


def _input_revisions(plan: Dict[str, Any], timeline: List[Dict[str, Any]]) -> Dict[str, str]:
    def selected(keys: Tuple[str, ...]) -> List[Dict[str, Any]]:
        return [
            {"ts": _to_ts_ms(row.get("ts")), **{key: row.get(key) for key in keys}}
            for row in timeline
        ]

    price_keys = (
        "billing_price_ct",
        "marketprice",
        "direct_marketing_market_price_ct",
        "direct_marketing_marketprice",
        "price_source",
        "price_resolution_min",
        "price_fresh",
        "price_stale",
        "price_status",
    )
    pv_keys = (
        "pv_w",
        "pv_p10_w",
        "pv_p50_w",
        "pv_p90_w",
        "external_ac_pv_w",
    )
    topology_slot_material = any(
        str(row.get("pv_topology_status") or "") == "bound"
        or row.get("e3dc_dc_pv_w") is not None
        or bool(row.get("pv_resource_contributions"))
        for row in timeline
    )
    if topology_slot_material:
        pv_keys += (
            "e3dc_dc_pv_w",
            "pv_topology_status",
            "pv_topology_reason",
            "pv_topology_revision",
            "pv_topology_source",
            "pv_topology_quality",
            "pv_resource_projection_status",
            "pv_resource_projection_reason",
            "pv_resource_contributions",
        )
    load_keys = ("home_w", "wp_w", "climate_w", "wb_w", "wb2_w", "planned_load_w")
    state_keys = ("soc", "charge_w", "target_charge_w", "surplus_w")
    hardware = {
        key: plan.get(key)
        for key in (
            "battery_capacity",
            "bat_cap_kwh",
            "max_charge_w",
            "max_discharge_w",
            "export_limit_w",
            "grid_import_limit_w",
            "physical_reserve_soc",
            "notstrom_reserve_soc",
            "hard_predump_grid_enabled",
            "hard_predump_target_soc",
            "adaptive_soc_floor",
            "adaptive_soc_ceiling",
            "pv_topology",
            "headroom_topology",
        )
    }
    config = {
        key: plan.get(key)
        for key in (
            "target_soc",
            "planning_target_soc",
            "morning_target",
            "morning_hour",
            "predump_enabled",
            "predump_min_soc",
            "mid_target_soc",
            "mid_hour",
            "noon_target_soc",
            "noon_hour",
            "shadow_dispatch_config",
        )
    }
    policy = {
        "direct_marketing": plan.get("direct_marketing"),
        "market_plan": plan.get("market_plan"),
        "cheap_grid_charge": plan.get("cheap_grid_charge"),
        "storm_grid_charge": plan.get("storm_grid_charge"),
        "predump": {
            key: plan.get(key)
            for key in (
                "predump_start_ts",
                "predump_end_ts",
                "predump_dump_wh",
                "predump_reason",
                "predump_curve_soc",
            )
        },
    }
    return {
        "price": revision_hash(selected(price_keys)),
        "pv_ensemble": revision_hash(selected(pv_keys)),
        "load_ensemble": revision_hash(selected(load_keys)),
        "state": revision_hash({
            "current_soc": plan.get("current_soc"),
            "timeline": selected(state_keys),
        }),
        "hardware_limits": revision_hash(hardware),
        "config": revision_hash(config),
        "policy": revision_hash(policy),
    }


def _price_contract(row: Dict[str, Any], window: Dict[str, Any]) -> Dict[str, Any]:
    buy = _round_or_none(row.get("billing_price_ct"), 4)
    gross = _round_or_none(
        row.get("direct_marketing_market_price_ct", row.get("direct_marketing_marketprice")),
        4,
    )
    net = _round_or_none(window.get("net_sell_ct"), 4) if window else None
    if net is None:
        net = gross
    stale = _safe_bool(row.get("price_stale"), None)
    fresh = False if stale is True else _safe_bool(row.get("price_fresh"), None)
    status = str(row.get("price_status") or "").strip() or None
    revision = revision_hash({
        "source": row.get("direct_marketing_price_source", row.get("price_source")),
        "resolution_min": row.get("direct_marketing_price_resolution_min", row.get("price_resolution_min")),
        "buy": buy,
        "gross_sell": gross,
        "net_sell": net,
        "fresh": fresh,
        "status": status,
    })
    return {
        "buy": buy,
        "gross_sell": gross,
        "net_sell": net,
        "fresh": fresh,
        "status": status,
        "tariff_revision": revision,
    }


def _action_contract(
    row: Dict[str, Any],
    window: Dict[str, Any],
    charge_w: float,
    predump_candidate_w: float,
) -> Tuple[str, str, str, float]:
    action_text = str(window.get("storage_action") or window.get("action") or "").upper()
    if "HEADROOM" in action_text:
        return "HEADROOM_EXPORT", "HEADROOM_WINDOW", str(window.get("reason") or "Headroom-Fenster"), max(0.0, _safe_float(window.get("max_power_w"), 0.0) or 0.0)
    if any(token in action_text for token in ("EXPORT", "SELL", "DISCHARGE")):
        return "ECONOMIC_EXPORT", "ECONOMIC_EXPORT_WINDOW", str(window.get("reason") or "Wirtschaftliches Exportfenster"), max(0.0, _safe_float(window.get("max_power_w"), 0.0) or 0.0)
    if any(token in action_text for token in ("GRID_CHARGE", "BUY", "IMPORT")):
        return "GRID_CHARGE", "ECONOMIC_GRID_CHARGE", str(window.get("reason") or "Wirtschaftliches Ladefenster"), max(0.0, charge_w)
    if any(token in action_text for token in ("PV_STORE", "CHARGE", "STORE")):
        return "PV_STORE", "PV_STORE_WINDOW", str(window.get("reason") or "PV-Speicherfenster"), max(0.0, charge_w)
    if predump_candidate_w > 0.0:
        # Der typisierte Headroom-Kandidat bleibt erhalten, darf aber niemals
        # ein explizites Marktfenster verdrängen. Auswahl und Ausführung bleiben
        # davon getrennte Storage-Manager-Entscheidungen.
        return (
            "HEADROOM_EXPORT",
            "RESIDUAL_HEADROOM_CANDIDATE",
            "Simulatorischer Rest-Headroombedarf; Auswahl und Ausführung ausschließlich im Storage Manager.",
            predump_candidate_w,
        )
    if charge_w > 50.0:
        return "PV_STORE", "LEGACY_SIMULATOR_CHARGE", "Bestehender Simulator-Ladeplan.", charge_w
    if charge_w < -50.0:
        return "HOUSE_SUPPLY", "LEGACY_SIMULATOR_DISCHARGE", "Bestehender Simulator-Entladeplan.", abs(charge_w)
    return "HOLD", "LEGACY_SIMULATOR_HOLD", "Bestehender Simulator hält den Speicher.", 0.0


def _build_slot(
    row: Dict[str, Any],
    previous_soc: Optional[float],
    plan: Dict[str, Any],
    direct_windows: List[Any],
    curve_points: Optional[Dict[str, List[Tuple[int, float]]]] = None,
) -> Dict[str, Any]:
    start_ms = _to_ts_ms(row.get("ts"))
    end_ms = start_ms + SLOT_DURATION_S * 1000
    charge_w = _safe_float(row.get("charge_w"), 0.0) or 0.0
    predump_candidate_w = max(0.0, _safe_float(row.get("predump_candidate_w", row.get("grid_dump_w")), 0.0) or 0.0)
    pv_w = _safe_float(row.get("pv_w"), 0.0) or 0.0
    home_w = _safe_float(row.get("home_w"), 0.0) or 0.0
    heat_w = (_safe_float(row.get("wp_w"), 0.0) or 0.0) + (_safe_float(row.get("climate_w"), 0.0) or 0.0)
    explicit_wallbox_w = (_safe_float(row.get("wb_w"), 0.0) or 0.0) + (_safe_float(row.get("wb2_w"), 0.0) or 0.0)
    # Explizite Nutzerfenster werden im Simulator als geplante Aggregate-Last
    # geführt. Phase 2 übernimmt sie genau einmal; eine Geräteaufteilung wird
    # nicht erfunden.
    wallbox_w = explicit_wallbox_w + (_safe_float(row.get("planned_load_w"), 0.0) or 0.0)
    total_load_w = home_w + heat_w + wallbox_w
    pv_p10_w = _safe_float(row.get("pv_p10_w"), None)
    pv_p50_w = _safe_float(row.get("pv_p50_w"), pv_w)
    pv_p90_w = _safe_float(row.get("pv_p90_w"), None)
    load_p10_w = _safe_float(row.get("load_p10_w"), None)
    load_p50_w = _safe_float(row.get("load_p50_w"), total_load_w)
    load_p90_w = _safe_float(row.get("load_p90_w"), None)
    quantiles_available = all(
        value is not None
        for value in (pv_p10_w, pv_p90_w, load_p10_w, load_p90_w)
    )
    grid_w = total_load_w + charge_w - pv_w
    soc_end = _safe_float(row.get("soc"), previous_soc)
    soc_start = previous_soc if previous_soc is not None else soc_end
    curves = curve_points or {}
    floor_soc = _curve_soc_at_points(curves["floor"], start_ms) if "floor" in curves else _curve_soc_at(plan.get("soc_min_curve") or [], start_ms)
    floor_source = "soc_min_curve" if floor_soc is not None else None
    if floor_soc is None:
        floor_soc = _safe_float(plan.get("adaptive_soc_floor", plan.get("predump_min_soc")), None)
        floor_source = "adaptive_soc_floor" if plan.get("adaptive_soc_floor") is not None else "predump_min_soc"
    notstrom_soc = _safe_float(plan.get("physical_reserve_soc", plan.get("notstrom_reserve_soc")), None)
    ceiling_soc = _curve_soc_at_points(curves["ceiling"], start_ms) if "ceiling" in curves else _curve_soc_at(plan.get("soc_ceiling_curve") or [], start_ms)
    if ceiling_soc is None:
        ceiling_soc = _safe_float(plan.get("adaptive_soc_ceiling"), 100.0)
    window = _window_for_ts(direct_windows, start_ms)
    action, reason_code, reason_detail, candidate_power_w = _action_contract(
        row, window, charge_w, predump_candidate_w
    )
    price = _price_contract(row, window)
    slot_hours = SLOT_DURATION_S / 3600.0
    import_w = max(0.0, grid_w)
    export_w = max(0.0, -grid_w)
    planned_charge_w = max(0.0, charge_w)
    planned_discharge_w = max(0.0, -charge_w)
    headroom_wh = predump_candidate_w * slot_hours
    target_soc = _curve_soc_at_points(curves["target"], start_ms) if "target" in curves else _curve_soc_at(plan.get("target_timeline") or [], start_ms)
    predicted_profit_ct_kwh = _safe_float(window.get("expected_profit_ct_per_kwh"), None) if window else None
    revenue_ct = None
    if price.get("net_sell") is not None and export_w > 0.0:
        revenue_ct = round(export_w * slot_hours / 1000.0 * float(price["net_sell"]), 5)
    net_value_ct = None
    if predicted_profit_ct_kwh is not None and candidate_power_w > 0.0:
        net_value_ct = round(candidate_power_w * slot_hours / 1000.0 * predicted_profit_ct_kwh, 5)

    topology_status = str(row.get("pv_topology_status") or "topology_unbound")
    topology_bound = bool(
        topology_status == "bound"
        and row.get("pv_topology_revision")
        and row.get("e3dc_dc_pv_w") is not None
        and row.get("external_ac_pv_w") is not None
    )
    pressure_details = row.get("headroom_pressure") if isinstance(row.get("headroom_pressure"), dict) else None
    dc_pressure_w = max(0.0, _safe_float(row.get("dc_headroom_pressure_w"), 0.0) or 0.0)
    pcc_pressure_w = max(0.0, _safe_float(row.get("pcc_headroom_pressure_w"), 0.0) or 0.0)
    combined_pressure_w = max(0.0, _safe_float(row.get("headroom_pressure_w"), 0.0) or 0.0)
    material_pressure = bool(
        max(dc_pressure_w, pcc_pressure_w, combined_pressure_w) > 0.0
        or (
            pressure_details
            and max(
                _safe_float(pressure_details.get("dc_pressure_w"), 0.0) or 0.0,
                _safe_float(pressure_details.get("pcc_pressure_w"), 0.0) or 0.0,
                _safe_float(pressure_details.get("combined_pressure_w"), 0.0) or 0.0,
            ) > 0.0
        )
    )
    topology_diagnostic_material = row.get("pv_topology_status") is not None
    slot_topology_material = topology_bound or material_pressure

    forecast_contract = {
        "pv": {
            "p10": _round_or_none(pv_p10_w, 3),
            "p50": _round_or_none(pv_p50_w, 3),
            "p90": _round_or_none(pv_p90_w, 3),
        },
        "load": {
            "p10": _round_or_none(load_p10_w, 3),
            "p50": _round_or_none(load_p50_w, 3),
            "p90": _round_or_none(load_p90_w, 3),
        },
        "house": {"p10": None, "p50": round(home_w, 3), "p90": None},
        "heat": {"p10": None, "p50": round(heat_w, 3), "p90": None},
        "wallbox": {"p10": None, "p50": round(wallbox_w, 3), "p90": None},
        "external_ac_pv": {"p10": None, "p50": _round_or_none(row.get("external_ac_pv_w"), 3), "p90": None},
    }
    if topology_bound:
        forecast_contract["e3dc_dc_pv"] = {
            "p10": None,
            "p50": _round_or_none(row.get("e3dc_dc_pv_w"), 3),
            "p90": None,
        }
    if topology_diagnostic_material:
        forecast_contract["topology"] = {
            "status": topology_status,
            "reason": str(
                row.get("pv_topology_reason")
                or ("OK" if topology_bound else "SLOT_TOPOLOGY_MISSING")
            ),
            "revision": row.get("pv_topology_revision"),
            "source": str(row.get("pv_topology_source") or "pv_forecast_slot"),
            "quality": str(
                row.get("pv_topology_quality")
                or ("complete" if topology_bound else "missing_or_incoherent_resource_projection")
            ),
            "resource_projection_status": str(
                row.get("pv_resource_projection_status")
                or ("complete" if topology_bound else "unbound")
            ),
            "resource_projection_reason": str(
                row.get("pv_resource_projection_reason")
                or ("OK" if topology_bound else "SLOT_TOPOLOGY_MISSING")
            ),
            "resources": copy.deepcopy(row.get("pv_resource_contributions"))
            if isinstance(row.get("pv_resource_contributions"), list)
            else [],
        }

    headroom_contract = {
        "required": round(headroom_wh, 3),
        "credited_economic_export": 0.0,
        "residual": round(headroom_wh, 3),
        "deadline_ts_ms": _to_ts_ms(plan.get("predump_end_ts")) or None,
    }
    if slot_topology_material:
        headroom_contract.update({
            "dc_pressure": round(dc_pressure_w * slot_hours, 3),
            "pcc_pressure": round(pcc_pressure_w * slot_hours, 3),
            "combined_pressure": round(combined_pressure_w * slot_hours, 3),
            "combination_rule": "max_dc_pcc_no_double_count",
        })

    binding_constraints = [
        {
            "code": "STORAGE_MANAGER_ONLY_EXECUTOR",
            "source": "owner_contract",
            "limit_w": None,
        }
    ]
    if slot_topology_material:
        binding_constraints.append({
            "code": "PV_HEADROOM_TOPOLOGY",
            "source": str((pressure_details or {}).get("status") or topology_status),
            "limit_w": None,
            "details": copy.deepcopy(pressure_details),
        })

    projection = {
        "pv_w": round(pv_w, 3),
        "home_w": round(home_w, 3),
        "home_source": row.get("home_source"),
        "home_quality": row.get("home_quality"),
        "heat_w": round(heat_w, 3),
        "wp_w": _round_or_none(row.get("wp_w"), 3),
        "wp_source": row.get("wp_source"),
        "wp_quality": row.get("wp_quality"),
        "climate_w": _round_or_none(row.get("climate_w"), 3),
        "climate_source": row.get("climate_source"),
        "climate_quality": row.get("climate_quality"),
        "wallbox_w": round(wallbox_w, 3),
        "battery_w": round(charge_w, 3),
        "grid_w": round(grid_w, 3),
        "soc_pct": _round_or_none(soc_end, 3),
        "target_soc_pct": target_soc,
        "predump_candidate_w": round(predump_candidate_w, 3),
        "predump_executable_w": 0.0,
        "predump_status": "candidate_only" if predump_candidate_w > 0.0 else "none",
        "direct_marketing_candidate": action == "ECONOMIC_EXPORT",
        "direct_marketing_candidate_w": round(candidate_power_w, 3) if action == "ECONOMIC_EXPORT" else 0.0,
        "direct_marketing_selected": False,
        "direct_marketing_shadow_selected": False,
        "direct_marketing_executable": False,
        "direct_marketing_commands_allowed": False,
        "direct_marketing_plan_executable": False,
        "direct_marketing_plan_commands_allowed": False,
        "direct_marketing_block_reason": (
            "PLANNER_CANDIDATE_NOT_RUNTIME_SELECTED"
            if action == "ECONOMIC_EXPORT"
            else None
        ),
        "direct_marketing_export_w": 0.0,
        "direct_marketing_planned_w": 0.0,
        "direct_marketing_soc_pct": None,
        "direct_marketing_plan_battery_w": None,
        "direct_marketing_plan_grid_w": None,
        "direct_marketing_plan_action": None,
        "direct_marketing_plan_source_action": None,
        "direct_marketing_plan_source_mode": None,
        "direct_marketing_plan_action_id": None,
        "direct_marketing_plan_segment_id": None,
        "direct_marketing_action_horizon_contract": None,
        "direct_marketing_economic_export_gate": None,
        "direct_marketing_window_id": None,
        "direct_marketing_window_start_ts_ms": None,
        "direct_marketing_window_end_ts_ms": None,
        "direct_marketing_export_segment_id": None,
        "direct_marketing_export_plateau_id": None,
        "direct_marketing_budget_source": None,
        "direct_marketing_segment_selected_wh": None,
        # Generisches PV-/Netzladen ist keine Direktvermarktungswirkung. Eine
        # DV-Ladeleistung darf erst der plan-/slotgebundene Runtimevertrag bei
        # selected+executable+commands_allowed veröffentlichen.
        "direct_marketing_charge_w": 0.0,
        "direct_marketing_action": None,
        "direct_marketing_candidate_action": action if action == "ECONOMIC_EXPORT" else None,
        "headroom_export_candidate_w": round(candidate_power_w, 3) if action == "HEADROOM_EXPORT" else 0.0,
        "headroom_export_reason": reason_code if action == "HEADROOM_EXPORT" else None,
        "market_charge_w": round(candidate_power_w, 3) if action == "GRID_CHARGE" else 0.0,
        "market_hold": action == "HOLD",
        "market_action": action,
        "price_ct_kwh": price.get("buy"),
        "market_price_ct_kwh": price.get("gross_sell"),
        "source": ADAPTER_VERSION,
    }
    local_fields = _local_time_fields(start_ms)
    return {
        "start_ts_ms": start_ms,
        "end_ts_ms": end_ms,
        **local_fields,
        "prices_ct_kwh": price,
        "forecast_w": forecast_contract,
        "forecast_scenario_contract": (
            "explicit_p10_p50_p90"
            if quantiles_available
            else "legacy_p50_only_no_quantile_invention"
        ),
        "soc_pct": {
            "start": _round_or_none(soc_start, 3),
            "end": _round_or_none(soc_end, 3),
            "p10": None,
            "p90": None,
            "reserve_floor": _round_or_none(floor_soc, 3),
            "reserve_floor_source": floor_source,
            "reserve_floor_hardness": "predictive_risk_floor_not_physical_reserve",
            "notstrom_floor": _round_or_none(notstrom_soc, 3),
            "notstrom_floor_source": "physical_reserve_soc" if plan.get("physical_reserve_soc") is not None else "notstrom_reserve_soc",
            "notstrom_floor_hardness": "hard",
            "ceiling": _round_or_none(ceiling_soc, 3),
        },
        "headroom_wh": headroom_contract,
        "planned_w": {
            "charge": round(planned_charge_w, 3),
            "discharge": round(planned_discharge_w, 3),
            "import": round(import_w, 3),
            "export": round(export_w, 3),
            "curtailment": _round_or_none(row.get("curtailment_w"), 3),
        },
        "planned_wh": {
            "charge": round(planned_charge_w * slot_hours, 3),
            "discharge": round(planned_discharge_w * slot_hours, 3),
            "import": round(import_w * slot_hours, 3),
            "export": round(export_w * slot_hours, 3),
            "curtailment": _round_or_none((_safe_float(row.get("curtailment_w"), 0.0) or 0.0) * slot_hours, 3) if row.get("curtailment_w") is not None else None,
        },
        "candidate": {
            "action": action,
            "power_w": round(candidate_power_w, 3),
            "status": "candidate_only",
        },
        "planned_action": action,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "economics_ct": {
            "revenue": revenue_ct,
            "import_cost": None,
            "opportunity_cost": None,
            "efficiency_loss_cost": None,
            "degradation_cost": None,
            "avoided_curtailment_value": None,
            "risk_margin": None,
            "switching_cost": None,
            "net_value": net_value_ct,
        },
        "binding_constraints": binding_constraints,
        "risk": {
            "expected_value_ct": net_value_ct,
            "worst_reserve_pct": _round_or_none(floor_soc, 3),
            "cvar_ct": None,
            "hard_violation": False,
            "model": "phase2_no_uncertainty_model",
        },
        "projection": projection,
    }


def _plan_material(plan: Dict[str, Any]) -> Dict[str, Any]:
    material = {key: plan.get(key) for key in _PLAN_MATERIAL_KEYS}
    slots = material.get("slots") if isinstance(material.get("slots"), list) else []
    material["slots"] = [
        {key: value for key, value in slot.items() if key != "slot_id"}
        if isinstance(slot, dict)
        else slot
        for slot in slots
    ]
    source_shadow = material.get("shadow_dispatch") if isinstance(material.get("shadow_dispatch"), dict) else {}
    shadow = dict(source_shadow)
    shadow.pop("runtime_ms", None)
    shadow.pop("runtime_measurement", None)
    shadow["slots"] = [
        {key: value for key, value in slot.items() if key != "slot_id"}
        if isinstance(slot, dict)
        else slot
        for slot in shadow.get("slots") or []
    ]
    material["shadow_dispatch"] = shadow
    return material


def _normalized_direct_marketing_mode(value: Any) -> str:
    """Normalisiert nur die freigegebenen DV-Modusbezeichner."""

    mode = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if mode in {"eco+", "ecoplus"}:
        return "eco_plus"
    return mode


def _shadow_input_binding_contract(
    source: Dict[str, Any],
    canonical: Dict[str, Any],
    shadow: Dict[str, Any],
) -> Dict[str, Any]:
    """Bindet Shadowinputs ohne erfundene Providerfrische an Plan und Slot.

    Der Vertrag wird vor der plan_id-Bildung in den fachlichen Planhash
    aufgenommen. Die spätere Phase-5-Auflösung koppelt ihn dadurch an die
    validierte plan_id und den daraus abgeleiteten aktuellen slot_id.
    """

    revisions = canonical.get("input_revisions") if isinstance(canonical.get("input_revisions"), dict) else {}
    price_horizon = shadow.get("price_horizon_contract") if isinstance(shadow.get("price_horizon_contract"), dict) else {}
    reserve = shadow.get("reserve_contract") if isinstance(shadow.get("reserve_contract"), dict) else {}
    terminal = shadow.get("terminal_value") if isinstance(shadow.get("terminal_value"), dict) else {}
    decision = shadow.get("decision_horizon") if isinstance(shadow.get("decision_horizon"), dict) else {}
    forecast_source = str(source.get("forecast_source") or "")
    forecast_trust = str(source.get("forecast_trust") or "")
    forecast_confidence = _round_or_none(source.get("forecast_confidence"), 6)
    decision_end_ms = _safe_int(decision.get("end_ts_ms"), 0)
    price_complete = bool(
        shadow.get("fallback") is not True
        and price_horizon.get("schema_version") == PRICE_HORIZON_SCHEMA
        and price_horizon.get("timezone") == TIMEZONE
        and _safe_int(price_horizon.get("slot_duration_ms"), 0) == SLOT_DURATION_S * 1000
        and price_horizon.get("applicability_basis") == "NEXT_LOCAL_MARKET_DAY_BOUNDARY"
        and price_horizon.get("complete_to_next_local_market_day_boundary") is True
        and price_horizon.get("field_activation_horizon_complete") is True
        and _safe_int(price_horizon.get("required_slots_to_market_day_boundary"), 0) > 0
        and _safe_int(price_horizon.get("effective_decision_slots"), 0)
        >= _safe_int(price_horizon.get("required_slots_to_market_day_boundary"), 0)
        and _safe_int(price_horizon.get("bound_horizon_end_ts_ms"), 0) == decision_end_ms
        and _safe_int(price_horizon.get("unpriced_slots_imputed"), -1) == 0
    )
    forecast_complete = bool(
        shadow.get("fallback") is not True
        and reserve.get("field_activation_input_complete") is True
        and _safe_int(reserve.get("decision_horizon_end_ts_ms"), 0) == decision_end_ms
        and forecast_source
        and forecast_trust
    )
    reserve_complete = bool(forecast_complete and reserve.get("method"))
    terminal_complete = bool(
        shadow.get("fallback") is not True
        and terminal.get("fresh") is True
        and terminal.get("method")
        and terminal.get("horizon_binding") == "ACTUAL_CONTIGUOUS_PRICE_HORIZON_END"
        and _safe_int(terminal.get("decision_horizon_end_ts_ms"), 0) == decision_end_ms
    )
    source_revisions = {
        key: revisions.get(key)
        for key in (
            "price",
            "pv_ensemble",
            "load_ensemble",
            "state",
            "hardware_limits",
            "config",
            "policy",
        )
    }
    if shadow.get("not_applicable") is True:
        reason_code = str(
            shadow.get("not_applicable_reason_code")
            or "DIRECT_MARKETING_DISABLED"
        )
        return {
            "schema_version": SHADOW_INPUT_BINDING_SCHEMA,
            "applicable": False,
            "not_applicable_reason_code": reason_code,
            "plan_generated_at_ts_ms": canonical.get("generated_at_ts_ms"),
            "current_slot_start_ts_ms": canonical.get("valid_from_ts_ms"),
            "current_slot_end_ts_ms": canonical.get("valid_until_ts_ms"),
            "source_revisions": source_revisions,
            "price": {"complete": False, "not_applicable": True},
            "forecast": {"complete": False, "not_applicable": True},
            "reserve": {"complete": False, "not_applicable": True},
            "terminal": {"complete": False, "not_applicable": True},
            "field_activation_input_complete": False,
        }
    return {
        "schema_version": SHADOW_INPUT_BINDING_SCHEMA,
        "applicable": True,
        "not_applicable_reason_code": None,
        "plan_generated_at_ts_ms": canonical.get("generated_at_ts_ms"),
        "current_slot_start_ts_ms": canonical.get("valid_from_ts_ms"),
        "current_slot_end_ts_ms": canonical.get("valid_until_ts_ms"),
        "source_revisions": source_revisions,
        "price": {
            "source": "canonical_slot_prices_ct_kwh",
            "source_revision": revisions.get("price"),
            "freshness_contract": "EXPLICIT_CONTIGUOUS_SLOT_FRESHNESS_NO_TAIL_IMPUTATION",
            "horizon_schema_version": price_horizon.get("schema_version"),
            "applicability_basis": price_horizon.get("applicability_basis"),
            "timezone": price_horizon.get("timezone"),
            "next_local_market_day_boundary_ts_ms": price_horizon.get("next_local_market_day_boundary_ts_ms"),
            "required_slots_to_market_day_boundary": price_horizon.get("required_slots_to_market_day_boundary"),
            "effective_decision_slots": price_horizon.get("effective_decision_slots"),
            "bound_horizon_end_ts_ms": price_horizon.get("bound_horizon_end_ts_ms"),
            "rolling_24h_complete": price_horizon.get("rolling_24h_complete"),
            "unpriced_slots_imputed": price_horizon.get("unpriced_slots_imputed"),
            "complete": price_complete,
        },
        "forecast": {
            "source": forecast_source,
            "trust": forecast_trust,
            "confidence": forecast_confidence,
            "pv_source_revision": revisions.get("pv_ensemble"),
            "load_source_revision": revisions.get("load_ensemble"),
            "scenario_contract": reserve.get("scenario_contract"),
            "horizon_end_ts_ms": decision_end_ms,
            "freshness_contract": "BOUND_TO_CANONICAL_PLAN_VALIDITY_NO_PROVIDER_AGE_CLAIM",
            "complete": forecast_complete,
        },
        "reserve": {
            "source": str(reserve.get("method") or ""),
            "state_source_revision": revisions.get("state"),
            "hardware_source_revision": revisions.get("hardware_limits"),
            "config_source_revision": revisions.get("config"),
            "policy_source_revision": revisions.get("policy"),
            "horizon_end_ts_ms": decision_end_ms,
            "complete": reserve_complete,
        },
        "terminal": {
            "source": str(terminal.get("method") or ""),
            "price_source_revision": revisions.get("price"),
            "pv_source_revision": revisions.get("pv_ensemble"),
            "load_source_revision": revisions.get("load_ensemble"),
            "state_source_revision": revisions.get("state"),
            "decision_horizon_end_ts_ms": decision.get("end_ts_ms"),
            "freshness_contract": "RECOMPUTED_WITH_CANONICAL_PLAN_GENERATION",
            "complete": terminal_complete,
        },
        "field_activation_input_complete": bool(
            price_complete and forecast_complete and reserve_complete and terminal_complete
        ),
    }


def _shadow_plan_id(shadow: Dict[str, Any]) -> str:
    material = copy.deepcopy(shadow)
    material.pop("runtime_ms", None)
    material.pop("runtime_measurement", None)
    material.pop("shadow_plan_id", None)
    for slot in material.get("slots") or []:
        if isinstance(slot, dict):
            slot.pop("slot_id", None)
    return revision_hash(material)


def _direct_marketing_not_applicable(direct: Dict[str, Any]) -> bool:
    """Akzeptiert ausschließlich den typisierten Produzentenvertrag `disabled`."""

    blocked = direct.get("blocked_reasons") if isinstance(direct.get("blocked_reasons"), list) else []
    flags = direct.get("flags") if isinstance(direct.get("flags"), dict) else {}
    return bool(
        direct.get("active") is False
        and direct.get("shadow") is True
        and direct.get("mode") == "off"
        and direct.get("reason") == "disabled"
        and "disabled" in blocked
        and flags.get("commands_allowed") is False
    )


def _battery_capacity_wh(plan: Dict[str, Any]) -> float:
    capacity_wh = _safe_float(plan.get("battery_capacity"), 0.0) or 0.0
    if 1.0 < capacity_wh < 500.0:
        capacity_wh *= 1000.0
    if capacity_wh <= 1000.0:
        capacity_wh = (_safe_float(plan.get("bat_cap_kwh"), 0.0) or 0.0) * 1000.0
    return capacity_wh if capacity_wh > 1000.0 else 0.0


def _direct_marketing_policy_projection_for_slot(
    direct: Dict[str, Any],
    slot: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Bindet einen DV-Slot an genau einen ausführbaren Policyvertrag.

    Die Policy-Auswahl ist Planung, keine Runtime-Autorisierung. Deshalb wird
    hier nur ``planned_w`` materialisiert; der aktuelle RSCP-Ausgang bleibt ein
    separater Runtimevertrag.
    """

    flags = direct.get("flags") if isinstance(direct.get("flags"), dict) else {}
    source_mode = _normalized_direct_marketing_mode(direct.get("mode"))
    if flags.get("commands_allowed") is not True:
        return None
    slot_start_ms = int(slot.get("start_ts_ms") or 0)
    slot_end_ms = int(slot.get("end_ts_ms") or 0)
    if slot_start_ms <= 0 or slot_end_ms - slot_start_ms != SLOT_DURATION_S * 1000:
        return None

    decisions = [
        item
        for item in direct.get("policy_timeline") or []
        if isinstance(item, dict)
    ]
    current = direct.get("policy_decision") if isinstance(direct.get("policy_decision"), dict) else None
    if current:
        decisions.append(current)

    matches: Dict[str, Dict[str, Any]] = {}
    for decision in decisions:
        decision_start_ms = _to_ts_ms(decision.get("start_ts"))
        decision_end_ms = _to_ts_ms(decision.get("end_ts"))
        if not (decision_start_ms <= slot_start_ms and slot_end_ms <= decision_end_ms):
            continue
        selected = decision.get("selected_window") if isinstance(decision.get("selected_window"), dict) else {}
        execution = decision.get("execution_window") if isinstance(decision.get("execution_window"), dict) else {}
        budget = decision.get("storage_budget") if isinstance(decision.get("storage_budget"), dict) else {}
        selected_action = str(selected.get("action") or "")
        execution_action = str(execution.get("action") or "")
        source_action = str(decision.get("source_action") or "")
        executable_action = str(decision.get("executable_action") or "")
        window_id = str(
            execution.get("window_id")
            or selected.get("export_plateau_id")
            or selected.get("window_id")
            or ""
        )
        execution_start_ms = _to_ts_ms(execution.get("start_ts"))
        execution_end_ms = _to_ts_ms(execution.get("end_ts"))
        plan_window_start_ms = _to_ts_ms(execution.get("plan_window_start_ts"))
        plan_window_end_ms = _to_ts_ms(execution.get("plan_window_end_ts"))
        target_state = str(decision.get("dv_target_state") or "").upper()
        action_map = {
            # Netz-Arbitrage ist in 5.4 nicht freigegeben. Ein kanonischer
            # ECONOMIC_EXPORT darf deshalb nur aus dem Eco+-Exportfenster
            # entstehen; ein eingespielter Arbitragevertrag bleibt wirkungslos.
            "FORCE_EXPORT": ("ECONOMIC_EXPORT", {"eco_plus_export_candidate"}, "export_budget_w"),
            "FORCE_CHARGE_PV": ("PV_STORE", {"eco_plus_store_pv_candidate"}, "charge_budget_w"),
        }
        action_contract = action_map.get(target_state)
        if action_contract is None:
            continue
        plan_action, allowed_source_actions, budget_key = action_contract
        allowed_source_modes = {
            "ECONOMIC_EXPORT": {"eco_plus"},
            "PV_STORE": {"eco", "eco_plus"},
        }.get(plan_action, set())
        action_budget_w = max(0.0, _safe_float(budget.get(budget_key), 0.0) or 0.0)
        protected_reserve_wh = _safe_float(budget.get("protected_reserve_wh"), None)
        sellable_wh = _safe_float(budget.get("sellable_wh"), None)
        economics = decision.get("economics") if isinstance(decision.get("economics"), dict) else {}
        economic_export_gate = None
        if plan_action == "ECONOMIC_EXPORT":
            economic_values = {
                "margin_ct_kwh": _safe_float(economics.get("margin_ct_kwh"), None),
                "user_min_margin_ct": _safe_float(economics.get("user_min_margin_ct"), None),
                "expected_profit_eur": _safe_float(economics.get("expected_profit_eur"), None),
                "min_window_profit_eur": _safe_float(economics.get("min_window_profit_eur"), None),
            }
            if (
                any(value is None for value in economic_values.values())
                or economic_values["user_min_margin_ct"] < 0.0
                or economic_values["min_window_profit_eur"] < 0.0
                or economic_values["margin_ct_kwh"] + 0.000001 < economic_values["user_min_margin_ct"]
                or economic_values["expected_profit_eur"] + 0.000001 < economic_values["min_window_profit_eur"]
            ):
                continue
            economic_export_gate = {
                "allowed": True,
                "blockers": [],
                "block_reason_code": None,
                **economic_values,
                "policy_commands_allowed": True,
                "policy_export_budget_w": round(action_budget_w, 3),
                "accounting_contract": "DIRECT_MARKETING_POLICY_ECONOMICS_REUSED_NO_DOUBLE_DEDUCTION",
            }
        if not (
            decision.get("commands_allowed") is True
            and decision.get("blocked") is False
            and source_mode in allowed_source_modes
            and selected_action in allowed_source_actions
            and source_action == selected_action
            and executable_action == selected_action
            and execution.get("contract_version") == 1
            and execution_action == selected_action
            and execution.get("source") == "active_plan_window"
            and window_id
            and execution_start_ms <= slot_start_ms
            and slot_end_ms <= execution_end_ms
            and plan_window_start_ms <= execution_start_ms
            and execution_end_ms <= plan_window_end_ms
            and action_budget_w > 0.0
            # Ein künftiger Verkauf darf den weichen Sollkurvenboden nur dann
            # überstimmen, wenn die vorgelagerte DV-Policy ihren harten
            # Haus-/Nacht-/Forecast-Reservevertrag explizit mitliefert.
            and protected_reserve_wh is not None
            and protected_reserve_wh >= 0.0
            and sellable_wh is not None
            and sellable_wh >= 0.0
        ):
            continue

        plan_windows = []
        for window in direct.get("windows") or []:
            if not isinstance(window, dict) or str(window.get("action") or "") != selected_action:
                continue
            candidate_id = str(window.get("export_plateau_id") or window.get("window_id") or "")
            if candidate_id != window_id:
                continue
            if (
                _to_ts_ms(window.get("start_ts")) == plan_window_start_ms
                and _to_ts_ms(window.get("end_ts")) == plan_window_end_ms
            ):
                plan_windows.append(window)
        if len(plan_windows) != 1:
            continue
        plan_window = plan_windows[0]
        power_limits = [action_budget_w]
        for value in (selected.get("max_power_w"), plan_window.get("max_power_w")):
            parsed = _safe_float(value, None)
            if parsed is not None and parsed > 0.0:
                power_limits.append(parsed)
        planned_w = min(power_limits)
        if planned_w <= 0.0:
            continue
        action_id = revision_hash({
            "action": plan_action,
            "window_id": window_id,
            "window_start_ts_ms": plan_window_start_ms,
            "window_end_ts_ms": plan_window_end_ms,
        })
        contract = {
            "schema_version": DIRECT_MARKETING_PLAN_PROJECTION_SCHEMA,
            "selected": True,
            "plan_executable": True,
            "plan_commands_allowed": True,
            "action": plan_action,
            "source_action": selected_action,
            "source_mode": source_mode,
            "action_id": action_id,
            "segment_id": str(
                plan_window.get("export_segment_id")
                or plan_window.get("segment_id")
                or action_id
            ),
            "planned_w": round(planned_w, 3),
            "window_id": window_id,
            "window_start_ts_ms": plan_window_start_ms,
            "window_end_ts_ms": plan_window_end_ms,
            "export_segment_id": plan_window.get("export_segment_id", selected.get("export_segment_id")),
            "export_plateau_id": plan_window.get("export_plateau_id", selected.get("export_plateau_id")),
            "budget_source": plan_window.get("export_segment_budget_source", selected.get("export_segment_budget_source")),
            "protected_reserve_wh": round(protected_reserve_wh, 3),
            "sellable_wh": round(sellable_wh, 3),
            "segment_selected_wh": _round_or_none(
                plan_window.get("export_segment_selected_wh", selected.get("export_segment_selected_wh")),
                3,
            ),
            "economic_export_gate": economic_export_gate,
        }
        matches[revision_hash(contract)] = contract

    if len(matches) != 1:
        return None
    return next(iter(matches.values()))


def _materialize_direct_marketing_plan_projection(
    source: Dict[str, Any],
    canonical: Dict[str, Any],
    valid_from_ms: int,
) -> Dict[str, Any]:
    """Erzeugt eine plan-id-gebundene DV-Leistungs- und SoC-Folgeprojektion."""

    direct = canonical.get("direct_marketing") if isinstance(canonical.get("direct_marketing"), dict) else {}
    slots = canonical.get("slots") if isinstance(canonical.get("slots"), list) else []
    future_slots = [
        slot for slot in slots
        if isinstance(slot, dict) and int(slot.get("start_ts_ms") or 0) >= valid_from_ms
    ]
    contracts = {
        int(slot.get("start_ts_ms") or 0): _direct_marketing_policy_projection_for_slot(direct, slot)
        for slot in future_slots
    }
    selected_contracts = [item for item in contracts.values() if isinstance(item, dict)]
    if not selected_contracts:
        return {
            "schema_version": DIRECT_MARKETING_PLAN_PROJECTION_SCHEMA,
            "status": "NO_EFFECTIVE_EXPORT_WINDOW",
            "complete": True,
            "selected_slot_count": 0,
            "window_ids": [],
        }

    capacity_wh = _battery_capacity_wh(source)
    if capacity_wh <= 0.0 or not future_slots:
        return {
            "schema_version": DIRECT_MARKETING_PLAN_PROJECTION_SCHEMA,
            "status": "BATTERY_CAPACITY_OR_TIMELINE_MISSING",
            "complete": False,
            "selected_slot_count": len(selected_contracts),
            "window_ids": sorted({str(item.get("window_id")) for item in selected_contracts}),
        }

    first_soc_contract = future_slots[0].get("soc_pct") if isinstance(future_slots[0].get("soc_pct"), dict) else {}
    planned_soc = _safe_float(source.get("current_soc"), None)
    if planned_soc is None:
        planned_soc = _safe_float(first_soc_contract.get("start"), None)
    shadow_cfg = source.get("shadow_dispatch_config") if isinstance(source.get("shadow_dispatch_config"), dict) else {}
    economics = direct.get("economics") if isinstance(direct.get("economics"), dict) else {}
    roundtrip_pct = _safe_float(
        shadow_cfg.get("roundtrip_efficiency_pct", economics.get("roundtrip_efficiency_pct")),
        85.0,
    ) or 85.0
    roundtrip = max(0.5, min(0.99, roundtrip_pct / 100.0))
    charge_efficiency = math.sqrt(roundtrip)
    discharge_efficiency = math.sqrt(roundtrip)
    max_charge_w = max(0.0, _safe_float(canonical.get("max_charge_w", source.get("max_charge_w")), 0.0) or 0.0)
    max_discharge_w = max(0.0, _safe_float(canonical.get("max_discharge_w", source.get("max_discharge_w")), 0.0) or 0.0)
    slot_hours = SLOT_DURATION_S / 3600.0
    action_horizon_end_ms = max(
        (_safe_int(item.get("window_end_ts_ms"), 0) for item in selected_contracts),
        default=0,
    )
    shadow_decision_horizon = (
        canonical.get("shadow_dispatch", {}).get("decision_horizon")
        if isinstance(canonical.get("shadow_dispatch"), dict)
        and isinstance(canonical.get("shadow_dispatch", {}).get("decision_horizon"), dict)
        else {}
    )
    bound_horizon_start_ms = _safe_int(shadow_decision_horizon.get("start_ts_ms"), valid_from_ms)
    bound_horizon_end_ms = _safe_int(
        shadow_decision_horizon.get("end_ts_ms"),
        int(future_slots[-1].get("end_ts_ms") or 0),
    )
    trajectory: Dict[int, Dict[str, Any]] = {}
    complete = planned_soc is not None
    floor_limited_slots = 0
    for slot in future_slots:
        start_ms = int(slot.get("start_ts_ms") or 0)
        projection = slot.get("projection") if isinstance(slot.get("projection"), dict) else {}
        soc_contract = slot.get("soc_pct") if isinstance(slot.get("soc_pct"), dict) else {}
        baseline_start = _safe_float(soc_contract.get("start"), None)
        baseline_end = _safe_float(soc_contract.get("end"), None)
        if planned_soc is None or baseline_start is None or baseline_end is None:
            complete = False
            break
        contract = contracts.get(start_ms)
        planned_w = 0.0
        baseline_battery_w = _safe_float(projection.get("battery_w"), 0.0) or 0.0
        planned_battery_w = baseline_battery_w
        planned_grid_w = _safe_float(projection.get("grid_w"), 0.0) or 0.0
        predictive_floor_pct = _safe_float(soc_contract.get("reserve_floor"), None)
        hard_floor_values = [_safe_float(soc_contract.get("notstrom_floor"), None)]
        if str(soc_contract.get("reserve_floor_hardness") or "") == "hard":
            hard_floor_values.append(predictive_floor_pct)
        ceiling_pct = max(0.0, min(100.0, _safe_float(soc_contract.get("ceiling"), 100.0) or 100.0))
        phase = "within_action_horizon_no_action" if start_ms < action_horizon_end_ms else "post_action_horizon_physics"
        if isinstance(contract, dict):
            plan_action = str(contract.get("action") or "").upper()
            hard_floor_values.append(
                max(0.0, _safe_float(contract.get("protected_reserve_wh"), 0.0) or 0.0)
                / capacity_wh
                * 100.0
            )
            # `soc_min_curve` ist ausdrücklich ein prädiktiver Risikoboden und
            # kein physischer Reserveboden. Ein bereits ausgewählter,
            # wirtschaftlicher Export darf ihn überstimmen. Harte Notstrom- und
            # Policy-Reserven bleiben dagegen bindend.
            floor_pct = max((value for value in hard_floor_values if value is not None), default=0.0)
            floor_pct = max(0.0, min(100.0, floor_pct))
            requested_w = max(0.0, _safe_float(contract.get("planned_w"), 0.0) or 0.0)
            if plan_action == "PV_STORE":
                home_w = _safe_float(projection.get("home_w"), 0.0) or 0.0
                heat_w = _safe_float(projection.get("heat_w"), 0.0) or 0.0
                wallbox_w = _safe_float(projection.get("wallbox_w"), 0.0) or 0.0
                pv_w = _safe_float(projection.get("pv_w"), 0.0) or 0.0
                pv_surplus_w = max(0.0, pv_w - home_w - heat_w - wallbox_w)
                room_wh = max(0.0, (ceiling_pct - planned_soc) / 100.0 * capacity_wh)
                planned_w = min(
                    requested_w,
                    max_charge_w if max_charge_w > 0.0 else requested_w,
                    pv_surplus_w,
                    room_wh / charge_efficiency / slot_hours,
                )
                planned_battery_w = planned_w
                planned_grid_w = home_w + heat_w + wallbox_w + planned_battery_w - pv_w
                planned_soc_end = planned_soc + (
                    planned_w * slot_hours * charge_efficiency / capacity_wh * 100.0
                )
            else:
                available_wh = max(0.0, (planned_soc - floor_pct) / 100.0 * capacity_wh)
                planned_w = min(
                    requested_w,
                    max_discharge_w if max_discharge_w > 0.0 else requested_w,
                    available_wh * discharge_efficiency / slot_hours,
                )
                planned_battery_w = -planned_w
                planned_grid_w = (
                    (_safe_float(projection.get("home_w"), 0.0) or 0.0)
                    + (_safe_float(projection.get("heat_w"), 0.0) or 0.0)
                    + (_safe_float(projection.get("wallbox_w"), 0.0) or 0.0)
                    + planned_battery_w
                    - (_safe_float(projection.get("pv_w"), 0.0) or 0.0)
                )
                planned_soc_end = planned_soc - (
                    planned_w * slot_hours / discharge_efficiency / capacity_wh * 100.0
                )
            if planned_w + 0.001 < requested_w:
                floor_limited_slots += 1
            phase = "selected_action"
        else:
            floor_pct = max((value for value in hard_floor_values if value is not None), default=0.0)
            floor_pct = max(0.0, min(100.0, floor_pct))
            home_w = _safe_float(projection.get("home_w"), 0.0) or 0.0
            heat_w = _safe_float(projection.get("heat_w"), 0.0) or 0.0
            wallbox_w = _safe_float(projection.get("wallbox_w"), 0.0) or 0.0
            pv_w = _safe_float(projection.get("pv_w"), 0.0) or 0.0
            pv_surplus_w = max(0.0, pv_w - home_w - heat_w - wallbox_w)
            energy_start_wh = planned_soc / 100.0 * capacity_wh
            floor_wh = floor_pct / 100.0 * capacity_wh
            ceiling_wh = ceiling_pct / 100.0 * capacity_wh
            if baseline_battery_w >= 0.0:
                charge_offer_w = max(baseline_battery_w, pv_surplus_w)
                charge_limit_w = max_charge_w if max_charge_w > 0.0 else charge_offer_w
                planned_battery_w = min(
                    charge_offer_w,
                    charge_limit_w,
                    max(0.0, ceiling_wh - energy_start_wh) / charge_efficiency / slot_hours,
                )
                energy_end_wh = energy_start_wh + planned_battery_w * slot_hours * charge_efficiency
            else:
                requested_discharge_w = abs(baseline_battery_w)
                discharge_limit_w = max_discharge_w if max_discharge_w > 0.0 else requested_discharge_w
                discharge_w = min(
                    requested_discharge_w,
                    discharge_limit_w,
                    max(0.0, energy_start_wh - floor_wh) * discharge_efficiency / slot_hours,
                )
                planned_battery_w = -discharge_w
                energy_end_wh = energy_start_wh - discharge_w * slot_hours / discharge_efficiency
            energy_end_wh = max(floor_wh, min(ceiling_wh, energy_end_wh))
            planned_soc_end = energy_end_wh / capacity_wh * 100.0
            planned_grid_w = home_w + heat_w + wallbox_w + planned_battery_w - pv_w
        planned_soc_end = max(floor_pct, min(ceiling_pct, planned_soc_end))
        trajectory[start_ms] = {
            "soc_pct": round(planned_soc_end, 3),
            "planned_w": round(planned_w, 3),
            "battery_w": round(planned_battery_w, 3),
            "grid_w": round(planned_grid_w, 3),
            "hard_floor_pct": round(floor_pct, 3),
            "ceiling_pct": round(ceiling_pct, 3),
            "phase": phase,
            "predictive_floor_pct": (
                round(predictive_floor_pct, 3)
                if predictive_floor_pct is not None
                else None
            ),
        }
        planned_soc = planned_soc_end

    if not complete or len(trajectory) != len(future_slots):
        return {
            "schema_version": DIRECT_MARKETING_PLAN_PROJECTION_SCHEMA,
            "status": "SOC_BASELINE_INCOMPLETE",
            "complete": False,
            "selected_slot_count": len(selected_contracts),
            "window_ids": sorted({str(item.get("window_id")) for item in selected_contracts}),
        }

    window_ids = set()
    selected_slot_count = 0
    for slot in future_slots:
        start_ms = int(slot.get("start_ts_ms") or 0)
        projection = slot.get("projection") if isinstance(slot.get("projection"), dict) else {}
        contract = contracts.get(start_ms)
        point = trajectory[start_ms]
        projection["direct_marketing_soc_pct"] = point["soc_pct"]
        projection["direct_marketing_plan_battery_w"] = point["battery_w"]
        projection["direct_marketing_plan_grid_w"] = point["grid_w"]
        projection["direct_marketing_soc_projection_phase"] = point["phase"]
        if isinstance(contract, dict) and point["planned_w"] > 0.0:
            plan_action = str(contract.get("action") or "").upper()
            window_start_ms = _safe_int(contract.get("window_start_ts_ms"), 0)
            window_end_ms = _safe_int(contract.get("window_end_ts_ms"), 0)
            action_horizon_contract = {
                "schema_version": "storage_dispatch_action_horizon_v1",
                "action": plan_action,
                "slot_start_ts_ms": int(slot.get("start_ts_ms") or 0),
                "slot_end_ts_ms": int(slot.get("end_ts_ms") or 0),
                "bound_horizon_start_ts_ms": bound_horizon_start_ms,
                "bound_horizon_end_ts_ms": bound_horizon_end_ms,
                "window_start_ts_ms": window_start_ms or None,
                "window_end_ts_ms": window_end_ms or None,
                "window_source": "canonical_direct_marketing_plan_projection",
                "complete": bool(
                    bound_horizon_start_ms <= window_start_ms
                    < window_end_ms <= bound_horizon_end_ms
                    and window_start_ms <= int(slot.get("start_ts_ms") or 0)
                    and int(slot.get("end_ts_ms") or 0) <= window_end_ms
                ),
                "block_reason_code": None,
            }
            if not action_horizon_contract["complete"]:
                action_horizon_contract["block_reason_code"] = (
                    "%s_WINDOW_OUTSIDE_BOUND_HORIZON" % plan_action
                )
            economic_export_gate = copy.deepcopy(contract.get("economic_export_gate"))
            if isinstance(economic_export_gate, dict):
                economic_export_gate["action_horizon_contract"] = copy.deepcopy(
                    action_horizon_contract
                )
            selected_slot_count += 1
            window_ids.add(str(contract.get("window_id")))
            projection.update({
                "direct_marketing_candidate": True,
                "direct_marketing_candidate_w": max(
                    point["planned_w"],
                    _safe_float(projection.get("direct_marketing_candidate_w"), 0.0) or 0.0,
                ),
                "direct_marketing_selected": True,
                "direct_marketing_plan_executable": True,
                "direct_marketing_plan_commands_allowed": True,
                "direct_marketing_planned_w": point["planned_w"],
                "direct_marketing_block_reason": None,
                "direct_marketing_plan_action": plan_action,
                "direct_marketing_plan_source_action": contract.get("source_action"),
                "direct_marketing_plan_source_mode": contract.get("source_mode"),
                "direct_marketing_plan_action_id": contract.get("action_id"),
                "direct_marketing_plan_segment_id": contract.get("segment_id"),
                "direct_marketing_action_horizon_contract": action_horizon_contract,
                "direct_marketing_economic_export_gate": economic_export_gate,
                "direct_marketing_window_id": contract.get("window_id"),
                "direct_marketing_window_start_ts_ms": contract.get("window_start_ts_ms"),
                "direct_marketing_window_end_ts_ms": contract.get("window_end_ts_ms"),
                "direct_marketing_export_segment_id": contract.get("export_segment_id"),
                "direct_marketing_export_plateau_id": contract.get("export_plateau_id"),
                "direct_marketing_budget_source": contract.get("budget_source"),
                "direct_marketing_protected_reserve_wh": contract.get("protected_reserve_wh"),
                "direct_marketing_sellable_wh": contract.get("sellable_wh"),
                "direct_marketing_segment_selected_wh": contract.get("segment_selected_wh"),
                "direct_marketing_hard_floor_pct": point.get("hard_floor_pct"),
                "direct_marketing_predictive_floor_pct": point.get("predictive_floor_pct"),
                "direct_marketing_predictive_floor_overridden": bool(
                    point.get("predictive_floor_pct") is not None
                    and point.get("hard_floor_pct") is not None
                    and point["predictive_floor_pct"] > point["hard_floor_pct"] + 0.001
                ),
            })
            projection["direct_marketing_candidate_action"] = plan_action
        slot["projection"] = projection

    return {
        "schema_version": DIRECT_MARKETING_PLAN_PROJECTION_SCHEMA,
        "status": "COMPLETE" if floor_limited_slots == 0 else "COMPLETE_FLOOR_BOUNDED",
        "complete": True,
        "selected_slot_count": selected_slot_count,
        "floor_limited_slot_count": floor_limited_slots,
        "window_ids": sorted(window_ids),
        "soc_source": "canonical_storage_physics_from_selected_direct_marketing_slots",
        "action_horizon_end_ts_ms": action_horizon_end_ms,
        "projection_horizon_end_ts_ms": int(future_slots[-1].get("end_ts_ms") or 0),
        "slot_duration_s": SLOT_DURATION_S,
        "roundtrip_efficiency_pct": round(roundtrip * 100.0, 3),
        "post_action_horizon_physics": True,
        "runtime_authorization_separate": True,
    }


def build_canonical_dispatch_plan(legacy_plan: Dict[str, Any]) -> Dict[str, Any]:
    """Ergänzt einen Legacyplan deterministisch um den v1-Vertrag."""

    source = copy.deepcopy(legacy_plan if isinstance(legacy_plan, dict) else {})
    timeline = _timeline_rows(source)
    if not timeline:
        raise ValueError("canonical_plan_timeline_missing")
    first_ts_ms = _to_ts_ms(timeline[0].get("ts"))
    generated_ms = _parse_generated_ts_ms(source.get("ts"), first_ts_ms)
    direct = source.get("direct_marketing") if isinstance(source.get("direct_marketing"), dict) else {}
    windows = direct.get("windows") if isinstance(direct.get("windows"), list) else []
    curves = {
        "floor": _curve_points(source.get("soc_min_curve") or []),
        "ceiling": _curve_points(source.get("soc_ceiling_curve") or []),
        "target": _curve_points(source.get("target_timeline") or []),
    }
    slots: List[Dict[str, Any]] = []
    previous_soc: Optional[float] = None
    for row in timeline:
        slot = _build_slot(row, previous_soc, source, windows, curves)
        slots.append(slot)
        previous_soc = _safe_float(row.get("soc"), previous_soc)

    # Das Rohmarktfenster ist eine reine Preisprojektion. Es darf weder die
    # Batterie-/Netzleistung noch die SoC-Folge verändern. Die geplante
    # PV_STORE-/Exportleistung bleibt ein getrenntes Action-Overlay.
    market_windows = direct.get("market_windows") if isinstance(direct.get("market_windows"), list) else []
    for slot in slots:
        start_ms = int(slot.get("start_ts_ms") or 0)
        end_ms = int(slot.get("end_ts_ms") or 0)
        projection = slot.get("projection") if isinstance(slot.get("projection"), dict) else {}
        market_window = next(
            (
                item for item in market_windows
                if isinstance(item, dict)
                and int(item.get("start_ts") or 0) <= start_ms
                and end_ms <= int(item.get("end_ts") or 0)
            ),
            None,
        )
        projection.update({
            "direct_marketing_market_eligible": isinstance(market_window, dict),
            "direct_marketing_market_window_id": (
                market_window.get("market_window_id") if isinstance(market_window, dict) else None
            ),
            "direct_marketing_market_window_start_ts_ms": (
                market_window.get("start_ts") if isinstance(market_window, dict) else None
            ),
            "direct_marketing_market_window_end_ts_ms": (
                market_window.get("end_ts") if isinstance(market_window, dict) else None
            ),
            "direct_marketing_market_margin_class": (
                market_window.get("margin_class") if isinstance(market_window, dict) else None
            ),
            "direct_marketing_market_net_sell_ct": (
                _round_or_none((slot.get("prices_ct_kwh") or {}).get("net_sell"), 3)
                if isinstance(market_window, dict)
                else None
            ),
        })
        slot["projection"] = projection

    generation_slot = next(
        (slot for slot in slots if slot["start_ts_ms"] <= generated_ms < slot["end_ts_ms"]),
        None,
    )
    if generation_slot is None:
        generation_slot = next((slot for slot in slots if slot["start_ts_ms"] >= generated_ms), slots[0])
    valid_from_ms = int(generation_slot["start_ts_ms"])
    valid_until_ms = int(generation_slot["end_ts_ms"])
    horizon_end_ms = int(slots[-1]["end_ts_ms"])
    canonical = {
        "schema_version": PLAN_SCHEMA,
        "generated_at": _utc_iso(generated_ms),
        "generated_at_ts_ms": generated_ms,
        "valid_from": _utc_iso(valid_from_ms),
        "valid_from_ts_ms": valid_from_ms,
        "valid_until": _utc_iso(valid_until_ms),
        "valid_until_ts_ms": valid_until_ms,
        "horizon_end": _utc_iso(horizon_end_ms),
        "horizon_end_ts_ms": horizon_end_ms,
        "timezone": TIMEZONE,
        "slot_duration_s": SLOT_DURATION_S,
        "input_revisions": _input_revisions(source, timeline),
        "pv_topology": copy.deepcopy(source.get("pv_topology"))
        if isinstance(source.get("pv_topology"), dict)
        else {
            "schema_version": "pv_forecast_topology_v1",
            "status": "topology_unbound",
            "reason": "TOPOLOGY_CONTRACT_MISSING",
            "split_usable": False,
        },
        "headroom_topology": copy.deepcopy(source.get("headroom_topology"))
        if isinstance(source.get("headroom_topology"), dict)
        else {
            "schema_version": "pv_headroom_topology_evidence_v1",
            "topology_status": "topology_unbound",
            "topology_reason": "HEADROOM_TOPOLOGY_MISSING",
            "combination_rule": "legacy_total_pv_pcc_only",
        },
        "planner": {
            "algorithm": "legacy_behavior_adapter",
            "algorithm_version": ADAPTER_VERSION,
            "parameter_revision": revision_hash({"adapter": ADAPTER_VERSION, "slot_duration_s": SLOT_DURATION_S}),
            "runtime_ms": 0,
            "fallback": False,
            "fallback_reason": None,
            "shadow_only": False,
        },
        "slots": slots,
        "direct_marketing": copy.deepcopy(direct),
        "compatibility": {
            "legacy_root_preserved": True,
            "legacy_timeline_preserved": True,
            "projection_source": ADAPTER_VERSION,
            "commands_allowed_in_planner": False,
            "hardware_owner": "storage_manager",
            "external_ac_pv_contract": "single_optional_input_no_invention",
            "shadow_pv_input": "timeline_pv_w_total_external_ac_not_added_again",
            "pv_headroom_topology": "typed_dc_pcc_split_only_when_bound_otherwise_legacy_pcc_only",
            "direct_marketing_capability": (
                "disabled_not_applicable"
                if _direct_marketing_not_applicable(direct)
                else "configured"
            ),
        },
    }
    try:
        try:
            from .storage_dispatch_optimizer import (
                ShadowInputError,
                optimize_shadow_dispatch,
                shadow_fallback,
                shadow_not_applicable,
            )
        except ImportError:
            from storage_dispatch_optimizer import (  # type: ignore
                ShadowInputError,
                optimize_shadow_dispatch,
                shadow_fallback,
                shadow_not_applicable,
            )
        if _direct_marketing_not_applicable(direct):
            shadow = shadow_not_applicable("DIRECT_MARKETING_DISABLED")
        else:
            try:
                # optimize_shadow_dispatch liest Slots ausschließlich. Nur der erste
                # Live-SoC-Anker braucht eine lokale Copy-on-write-Kopie; alle übrigen
                # kanonischen Slots können ohne zweite Vollkopie übergeben werden.
                shadow_input_slots = [
                    slot
                    for slot in canonical["slots"]
                    if int(slot.get("start_ts_ms") or 0) >= valid_from_ms
                ]
                current_soc = _safe_float(source.get("current_soc"), None)
                if current_soc is None:
                    current_soc = _safe_float(timeline[0].get("soc"), None)
                if shadow_input_slots and current_soc is not None:
                    shadow_input_slots[0] = dict(shadow_input_slots[0])
                    first_soc = (
                        dict(shadow_input_slots[0].get("soc_pct"))
                        if isinstance(shadow_input_slots[0].get("soc_pct"), dict)
                        else {}
                    )
                    first_soc["start"] = round(current_soc, 3)
                    first_soc["end"] = round(current_soc, 3)
                    shadow_input_slots[0]["soc_pct"] = first_soc
                    shadow_input_slots[0]["current_soc_source"] = (
                        "plan_current_soc"
                        if source.get("current_soc") is not None
                        else "legacy_timeline_first_slot_soc"
                    )
                shadow = optimize_shadow_dispatch(source, shadow_input_slots)
            except ShadowInputError as exc:
                shadow = shadow_fallback(str(exc))
    except Exception:
        try:
            shadow = shadow_fallback("SHADOW_INTERNAL_ERROR")
        except Exception:
            shadow = {
                "schema_version": "storage_dispatch_shadow_v1",
                "status": "SHADOW_FALLBACK_BASELINE",
                "shadow_only": True,
                "commands_allowed": False,
                "owner": "storage_dispatch_shadow",
                "slots": [],
                "fallback": True,
                "fallback_reason_code": "SHADOW_INTERNAL_ERROR",
            }
    shadow["input_binding_contract"] = _shadow_input_binding_contract(source, canonical, shadow)
    shadow["shadow_plan_id"] = _shadow_plan_id(shadow)
    canonical["shadow_dispatch"] = shadow
    canonical["terminal_value"] = copy.deepcopy(shadow.get("terminal_value"))
    shadow_slots_by_start = {
        int(item.get("start_ts_ms") or 0): item
        for item in shadow.get("slots") or []
        if isinstance(item, dict)
    }
    for slot in canonical["slots"]:
        projection = slot.get("projection") if isinstance(slot.get("projection"), dict) else {}
        shadow_slot = shadow_slots_by_start.get(int(slot.get("start_ts_ms") or 0), {})
        shadow_action = str(shadow_slot.get("planned_action") or "").upper()
        if shadow_action == "ECONOMIC_EXPORT":
            projection.update({
                "direct_marketing_candidate": True,
                "direct_marketing_candidate_w": round(
                    max(0.0, _safe_float(shadow_slot.get("candidate_power_w"), 0.0) or 0.0),
                    3,
                ),
                "direct_marketing_shadow_selected": shadow_slot.get("selected") is True,
                "direct_marketing_executable": False,
                "direct_marketing_commands_allowed": False,
                "direct_marketing_block_reason": shadow_slot.get("block_reason_code"),
                "direct_marketing_export_w": 0.0,
                "direct_marketing_action": None,
                "direct_marketing_candidate_action": "ECONOMIC_EXPORT",
            })
        elif shadow_action == "HEADROOM_EXPORT":
            projection.update({
                "headroom_export_candidate_w": round(
                    max(0.0, _safe_float(shadow_slot.get("candidate_power_w"), 0.0) or 0.0),
                    3,
                ),
                "headroom_export_reason": shadow_slot.get("reason_code"),
                "direct_marketing_export_w": 0.0,
                "direct_marketing_action": None,
            })
        slot["projection"] = projection
    canonical["planner"]["direct_marketing_projection"] = _materialize_direct_marketing_plan_projection(
        source,
        canonical,
        valid_from_ms,
    )
    canonical["planner"]["shadow"] = {
        "schema_version": shadow.get("schema_version"),
        "algorithm": shadow.get("algorithm"),
        "shadow_plan_id": shadow.get("shadow_plan_id"),
        "status": shadow.get("status"),
        "fallback": bool(shadow.get("fallback", True)),
        "fallback_reason_code": shadow.get("fallback_reason_code"),
        "commands_allowed": False,
    }
    plan_id = revision_hash(_plan_material(canonical))
    canonical["plan_id"] = plan_id
    for slot in canonical["slots"]:
        slot["slot_id"] = revision_hash({
            "plan_id": plan_id,
            "start_ts_ms": slot["start_ts_ms"],
            "end_ts_ms": slot["end_ts_ms"],
        })
    slot_ids_by_start = {
        int(slot["start_ts_ms"]): slot["slot_id"]
        for slot in canonical["slots"]
    }
    for shadow_slot in canonical["shadow_dispatch"].get("slots") or []:
        if isinstance(shadow_slot, dict):
            shadow_slot["slot_id"] = slot_ids_by_start.get(_safe_int(shadow_slot.get("start_ts_ms"), 0))
    result = source
    result.update(canonical)
    return result


def validate_canonical_plan_static(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Validiert alle zeitunabhängigen Identitäts- und Strukturverträge."""

    if not isinstance(plan, dict) or not plan:
        return {"valid": False, "block_reason_code": "PLAN_MISSING", "plan_id": None, "slot": None}
    if plan.get("schema_version") != PLAN_SCHEMA:
        return {"valid": False, "block_reason_code": "PLAN_SCHEMA_MISSING_OR_UNKNOWN", "plan_id": plan.get("plan_id"), "slot": None}
    plan_id = str(plan.get("plan_id") or "")
    if not plan_id.startswith("sha256:") or len(plan_id) != 71:
        return {"valid": False, "block_reason_code": "PLAN_ID_INVALID", "plan_id": plan_id or None, "slot": None}
    if revision_hash(_plan_material(plan)) != plan_id:
        return {"valid": False, "block_reason_code": "PLAN_HASH_MISMATCH", "plan_id": plan_id, "slot": None}
    valid_from = _safe_int(plan.get("valid_from_ts_ms"), 0)
    valid_until = _safe_int(plan.get("valid_until_ts_ms"), 0)
    if not valid_from < valid_until:
        return {"valid": False, "block_reason_code": "PLAN_VALIDITY_INVALID", "plan_id": plan_id, "slot": None}
    slots = plan.get("slots") if isinstance(plan.get("slots"), list) else []
    direct = plan.get("direct_marketing") if isinstance(plan.get("direct_marketing"), dict) else {}
    direct_mode = _normalized_direct_marketing_mode(direct.get("mode"))
    slot_ids_by_start: Dict[int, str] = {}
    for item in slots:
        if not isinstance(item, dict):
            return {"valid": False, "block_reason_code": "PLAN_SLOT_INVALID", "plan_id": plan_id, "slot": None}
        projection = item.get("projection") if isinstance(item.get("projection"), dict) else {}
        planned_w = _safe_float(projection.get("direct_marketing_planned_w"), 0.0) or 0.0
        direct_selection_claim = bool(
            projection.get("direct_marketing_selected") is True
            or projection.get("direct_marketing_plan_executable") is True
            or projection.get("direct_marketing_plan_commands_allowed") is True
            or planned_w > 0.0
        )
        projection_mode = _normalized_direct_marketing_mode(
            projection.get("direct_marketing_plan_source_mode")
        )
        if direct_selection_claim and (not direct_mode or projection_mode != direct_mode):
            return {
                "valid": False,
                "block_reason_code": "DIRECT_MARKETING_SOURCE_MODE_MISMATCH",
                "plan_id": plan_id,
                "slot": None,
            }
        expected_id = revision_hash({
            "plan_id": plan_id,
            "start_ts_ms": item.get("start_ts_ms"),
            "end_ts_ms": item.get("end_ts_ms"),
        })
        if item.get("slot_id") != expected_id:
            return {"valid": False, "block_reason_code": "SLOT_ID_MISMATCH", "plan_id": plan_id, "slot": None}
        slot_ids_by_start[_safe_int(item.get("start_ts_ms"), 0)] = expected_id
    shadow = plan.get("shadow_dispatch") if isinstance(plan.get("shadow_dispatch"), dict) else {}
    for shadow_slot in shadow.get("slots") or []:
        if not isinstance(shadow_slot, dict):
            return {"valid": False, "block_reason_code": "SHADOW_SLOT_INVALID", "plan_id": plan_id, "slot": None}
        expected_id = slot_ids_by_start.get(_safe_int(shadow_slot.get("start_ts_ms"), 0))
        if expected_id is None or shadow_slot.get("slot_id") != expected_id:
            return {"valid": False, "block_reason_code": "SHADOW_SLOT_ID_MISMATCH", "plan_id": plan_id, "slot": None}
    return {
        "valid": True,
        "block_reason_code": None,
        "plan_id": plan_id,
        "valid_from_ts_ms": valid_from,
        "valid_until_ts_ms": valid_until,
        "slot": None,
    }


def _snapshot_source_reason(plan: ValidatedCanonicalPlanSnapshot) -> Optional[str]:
    if plan.source_path is None or plan.source_generation is None:
        return None
    try:
        current = os.stat(plan.source_path, follow_symlinks=False)
    except OSError:
        return "PLAN_SOURCE_MISSING"
    if not stat.S_ISREG(current.st_mode) or _plan_file_generation(current) != plan.source_generation:
        return "PLAN_SOURCE_GENERATION_MISMATCH"
    return None


def load_validated_canonical_plan_snapshot(
    path: str,
    *,
    max_age_s: Optional[float] = 1800,
    max_bytes: int = _PLAN_SNAPSHOT_MAX_BYTES,
) -> Optional[ValidatedCanonicalPlanSnapshot]:
    """Lädt genau eine stabile Dateigeneration; alte Revisionen sind kein Fallback."""

    normalized = os.path.abspath(os.fspath(path))
    descriptor = -1
    try:
        descriptor = os.open(normalized, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > int(max_bytes):
            return None
        source = b""
        while len(source) <= int(max_bytes):
            chunk = os.read(descriptor, min(1024 * 1024, int(max_bytes) - len(source) + 1))
            if not chunk:
                break
            source += chunk
        after = os.fstat(descriptor)
        generation = _plan_file_generation(before)
        if len(source) > int(max_bytes) or generation != _plan_file_generation(after) or len(source) != after.st_size:
            return None
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        current = os.stat(normalized, follow_symlinks=False)
        if generation != _plan_file_generation(current):
            return None
        if max_age_s is not None and time.time() - current.st_mtime > max(0.0, float(max_age_s)):
            return None
        digest = hashlib.sha256(source).hexdigest()
        key = (normalized, generation, digest)
        with _PLAN_SNAPSHOT_LOCK:
            cached = _PLAN_SNAPSHOT_CACHE.get(key)
            if cached is not None:
                _PLAN_SNAPSHOT_CACHE.move_to_end(key)
                return cached
            parsed = json.loads(source.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            static = validate_canonical_plan_static(parsed)
            if static.get("valid") is not True:
                return None
            snapshot = ValidatedCanonicalPlanSnapshot(
                parsed,
                static_validation=static,
                source_path=normalized,
                source_generation=generation,
                source_sha256=digest,
            )
            final = os.stat(normalized, follow_symlinks=False)
            if generation != _plan_file_generation(final):
                return None
            _PLAN_SNAPSHOT_CACHE[key] = snapshot
            while len(_PLAN_SNAPSHOT_CACHE) > _PLAN_SNAPSHOT_CACHE_LIMIT:
                _PLAN_SNAPSHOT_CACHE.popitem(last=False)
            return snapshot
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def validate_canonical_plan(
    plan: Dict[str, Any],
    now_ms: Optional[int] = None,
    *,
    max_age_s: int = 1800,
) -> Dict[str, Any]:
    """Validiert Schema, Hash, Frische und aktuellen ausführbaren Slot."""

    now_value = int(now_ms if now_ms is not None else dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    metadata: Dict[str, Any] = {}
    if isinstance(plan, ValidatedCanonicalPlanSnapshot):
        static = plan.static_validation
        source_reason = _snapshot_source_reason(plan)
        metadata = {"source_sha256": plan.source_sha256, "source_generation": plan.source_generation}
        if source_reason:
            return {"valid": False, "block_reason_code": source_reason, "plan_id": static.get("plan_id"), "slot": None, **metadata}
        cached = object.__getattribute__(plan, "_dynamic_validation_cache").get(now_value)
        if cached is not None:
            return dict(cached)
    else:
        static = validate_canonical_plan_static(plan)
    if static.get("valid") is not True:
        return dict(static)
    plan_id = str(static.get("plan_id") or "")
    generated_ms = _safe_int(plan.get("generated_at_ts_ms"), 0)
    age_ms = now_value - generated_ms if generated_ms > 0 else None
    if age_ms is None or age_ms < -60_000:
        return {"valid": False, "block_reason_code": "PLAN_TIME_INVALID", "plan_id": plan_id, "slot": None, **metadata}
    if age_ms > max(1, int(max_age_s)) * 1000:
        return {"valid": False, "block_reason_code": "PLAN_STALE", "plan_id": plan_id, "slot": None, "age_s": round(age_ms / 1000.0, 1), **metadata}
    valid_from = _safe_int(plan.get("valid_from_ts_ms"), 0)
    valid_until = _safe_int(plan.get("valid_until_ts_ms"), 0)
    if not valid_from <= now_value < valid_until:
        return {"valid": False, "block_reason_code": "PLAN_SLOT_EXPIRED", "plan_id": plan_id, "slot": None, "age_s": round(age_ms / 1000.0, 1), **metadata}
    slots = plan.get("slots") if isinstance(plan.get("slots"), list) else []
    slot = next(
        (
            item for item in slots
            if isinstance(item, dict)
            and _safe_int(item.get("start_ts_ms"), 0) <= now_value < _safe_int(item.get("end_ts_ms"), 0)
        ),
        None,
    )
    if slot is None:
        return {"valid": False, "block_reason_code": "CURRENT_SLOT_MISSING", "plan_id": plan_id, "slot": None, **metadata}
    result = {
        "valid": True,
        "block_reason_code": None,
        "plan_id": plan_id,
        "slot_id": slot.get("slot_id"),
        "slot": slot,
        "age_s": round(age_ms / 1000.0, 1),
        **metadata,
    }
    if isinstance(plan, ValidatedCanonicalPlanSnapshot):
        cache = object.__getattribute__(plan, "_dynamic_validation_cache")
        cache.clear()
        cache[now_value] = result
    return result


def _actual_action(payload: Dict[str, Any]) -> str:
    state = str(payload.get("state") or "").lower()
    priority = str(payload.get("priority") or "").lower()
    target = str(
        payload.get("direct_marketing_target_state")
        or payload.get("dv_target_state")
        or ""
    ).upper()
    if payload.get("predump_active") or state.startswith("pre_discharge"):
        return "HEADROOM_EXPORT"
    if target == "HEADROOM_EXPORT":
        return "HEADROOM_EXPORT"
    if target == "FORCE_EXPORT" or "direct_marketing" in state or "direct_marketing" in priority:
        return "ECONOMIC_EXPORT"
    mode_name = str(payload.get("mode_name") or "AUTO").upper()
    value_w = max(0, _safe_int(payload.get("val"), 0))
    if mode_name == "CHARGE" or mode_name == "CHRG":
        return "GRID_CHARGE" if "grid" in state or "market" in priority else "PV_STORE"
    if mode_name in {"DISCHARGE", "DISCH"} and value_w > 0:
        return "HOUSE_SUPPLY"
    return "HOLD"


def _direct_marketing_runtime_plan_binding(
    plan: Dict[str, Any],
    slot: Dict[str, Any],
    phase5: Dict[str, Any],
    *,
    plan_id: Optional[str],
    slot_id: Optional[str],
) -> Dict[str, Any]:
    """Prüft aktive DV-Claims gegen genau die veröffentlichte Slotauswahl."""

    projection = slot.get("projection") if isinstance(slot.get("projection"), dict) else {}
    candidate = phase5.get("candidate") if isinstance(phase5.get("candidate"), dict) else {}
    action = str(candidate.get("action") or "").upper()
    selected_action = str(phase5.get("selected_action") or "").upper()
    runtime_claim = bool(
        action in {"ECONOMIC_EXPORT", "PV_STORE", "GRID_CHARGE"}
        and any(
            (
                phase5.get("selected") is True and selected_action == action,
                phase5.get("executable") is True,
                phase5.get("commands_allowed") is True,
                phase5.get("requested") is True,
                phase5.get("issued") is True,
                phase5.get("hardware_effect") is True,
            )
        )
    )
    planned_w = _safe_float(projection.get("direct_marketing_planned_w"), None)
    source_action = str(projection.get("direct_marketing_plan_source_action") or "")
    source_mode = _normalized_direct_marketing_mode(
        projection.get("direct_marketing_plan_source_mode")
    )
    direct = plan.get("direct_marketing") if isinstance(plan.get("direct_marketing"), dict) else {}
    plan_source_mode = _normalized_direct_marketing_mode(direct.get("mode"))
    source_mode_matches_plan = bool(plan_source_mode and source_mode == plan_source_mode)
    expected_source_action = {
        "ECONOMIC_EXPORT": "eco_plus_export_candidate",
        "PV_STORE": "eco_plus_store_pv_candidate",
    }.get(action)
    source_mode_valid = bool(
        (action == "ECONOMIC_EXPORT" and source_mode == "eco_plus")
        or (action == "PV_STORE" and source_mode in {"eco", "eco_plus"})
    )
    window_id = projection.get("direct_marketing_window_id")
    action_id = projection.get("direct_marketing_plan_action_id")
    segment_id = projection.get("direct_marketing_plan_segment_id")
    action_horizon = (
        projection.get("direct_marketing_action_horizon_contract")
        if isinstance(projection.get("direct_marketing_action_horizon_contract"), dict)
        else {}
    )
    economic_gate = (
        projection.get("direct_marketing_economic_export_gate")
        if isinstance(projection.get("direct_marketing_economic_export_gate"), dict)
        else {}
    )
    plan_selected = bool(
        projection.get("direct_marketing_candidate") is True
        and projection.get("direct_marketing_selected") is True
        and projection.get("direct_marketing_plan_executable") is True
        and projection.get("direct_marketing_plan_commands_allowed") is True
        and str(projection.get("direct_marketing_plan_action") or "").upper() == action
        and source_action == expected_source_action
        and source_mode_valid
        and source_mode_matches_plan
        and planned_w is not None
        and planned_w >= 300.0
        and isinstance(window_id, str)
        and bool(window_id.strip())
        and isinstance(action_id, str)
        and bool(action_id)
        and isinstance(segment_id, str)
        and bool(segment_id)
        and action_horizon.get("schema_version") == "storage_dispatch_action_horizon_v1"
        and action_horizon.get("action") == action
        and action_horizon.get("complete") is True
        and (
            action != "ECONOMIC_EXPORT"
            or (
                economic_gate.get("allowed") is True
                and not economic_gate.get("blockers")
            )
        )
    )
    candidate_window_id = candidate.get("window_id")
    candidate_source_action = candidate.get("source_action")
    candidate_source_mode = candidate.get("source_mode")
    candidate_action_id = candidate.get("action_id")
    candidate_segment_id = candidate.get("segment_id")
    candidate_power_w = _safe_float(candidate.get("power_w"), None)
    generation_match = bool(
        isinstance(plan_id, str)
        and bool(plan_id)
        and phase5.get("plan_id") == plan_id
        and isinstance(slot_id, str)
        and bool(slot_id)
        and phase5.get("slot_id") == slot_id
        and slot.get("slot_id") == slot_id
    )
    exact_candidate = bool(
        plan_selected
        and generation_match
        and candidate_window_id == window_id
        and candidate_source_action == source_action
        and candidate_source_mode == source_mode
        and candidate_action_id == action_id
        and candidate_segment_id == segment_id
        and candidate_power_w is not None
        and abs(candidate_power_w - planned_w) <= 1.0
    )
    valid = bool(not runtime_claim or exact_candidate)
    return {
        "valid": valid,
        "runtime_claim": runtime_claim,
        "plan_selected": plan_selected,
        "generation_match": generation_match,
        "exact_candidate": exact_candidate,
        "window_id": window_id if isinstance(window_id, str) else None,
        "source_action": source_action or None,
        "source_mode": source_mode or None,
        "plan_source_mode": plan_source_mode or None,
        "source_mode_matches_plan": source_mode_matches_plan,
        "planned_w": round(planned_w, 3) if planned_w is not None else None,
        "reason_code": None if valid else "PLAN_RUNTIME_SELECTION_INVARIANT_VIOLATION",
    }


def build_runtime_overlay(
    plan: Dict[str, Any],
    payload: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Bindet die echte Managerentscheidung an den aktuellen kanonischen Slot."""

    payload = payload if isinstance(payload, dict) else {}
    live = live if isinstance(live, dict) else {}
    now_value = int(now_ms if now_ms is not None else (_safe_float(payload.get("ts"), dt.datetime.now().timestamp()) or 0.0) * 1000)
    validation = validate_canonical_plan(plan, now_value)
    slot = validation.get("slot") if isinstance(validation.get("slot"), dict) else {}
    phase5 = payload.get("storage_dispatch_phase5") if isinstance(payload.get("storage_dispatch_phase5"), dict) else {}
    explicit_phase5 = bool(phase5.get("schema_version") == "storage_dispatch_phase5_v1")
    direct_marketing_binding = _direct_marketing_runtime_plan_binding(
        plan,
        slot,
        phase5,
        plan_id=validation.get("plan_id"),
        slot_id=validation.get("slot_id"),
    ) if explicit_phase5 else {
        "valid": True,
        "runtime_claim": False,
        "plan_selected": False,
        "generation_match": False,
        "exact_candidate": False,
        "reason_code": None,
    }
    if explicit_phase5 and not direct_marketing_binding.get("valid"):
        phase5 = copy.deepcopy(phase5)
        phase5.update({
            "selected": False,
            "executable": False,
            "commands_allowed": False,
            "requested": False,
            "issued": False,
            "hardware_effect": False,
            "selected_action": None,
            "selected_power_w": 0.0,
            "block_reason_code": direct_marketing_binding.get("reason_code"),
            "technical_block_reason_code": direct_marketing_binding.get("reason_code"),
        })
        blockers = list(phase5.get("blockers") or [])
        if direct_marketing_binding.get("reason_code") not in blockers:
            blockers.insert(0, direct_marketing_binding.get("reason_code"))
        phase5["blockers"] = blockers
    if explicit_phase5:
        phase5_candidate = phase5.get("candidate") if isinstance(phase5.get("candidate"), dict) else {}
        candidate = {
            "action": phase5_candidate.get("action") or "HOLD",
            "power_w": phase5_candidate.get("power_w", 0.0),
            "status": "phase5_decision_available" if phase5.get("decision_available") else "unavailable",
            "candidate": bool(phase5.get("decision_available") and phase5_candidate.get("action") not in {None, "HOLD"}),
            "selected": bool(phase5.get("selected")),
            "executable": bool(phase5.get("executable")),
            "commands_allowed": bool(phase5.get("commands_allowed")),
            "block_reason_code": (
                phase5_candidate.get("block_reason_code")
                or phase5.get("technical_block_reason_code")
                or phase5.get("block_reason_code")
            ),
            "economic_export_gate": copy.deepcopy(phase5_candidate.get("economic_export_gate")),
            "headroom_gate": copy.deepcopy(phase5_candidate.get("headroom_gate")),
        }
    else:
        candidate = slot.get("candidate") if isinstance(slot.get("candidate"), dict) else {"action": "HOLD", "power_w": 0.0, "status": "unavailable"}
    candidate_action = str(candidate.get("action") or "HOLD").upper()
    actual_action = (
        str(phase5.get("selected_action") or _actual_action(payload)).upper()
        if explicit_phase5 and phase5.get("requested")
        else _actual_action(payload)
    )
    selected = (
        bool(phase5.get("selected"))
        if explicit_phase5
        else bool(validation.get("valid") and candidate_action == actual_action)
    )
    live_valid = bool(
        not payload.get("safe_start")
        and not payload.get("live_stale")
        and payload.get("live_sample_valid", True)
        and payload.get("grid_power_valid", True)
    )
    executable = (
        bool(phase5.get("executable") and live_valid and not payload.get("ems_budget_runtime_veto"))
        if explicit_phase5
        else bool(selected and live_valid and not payload.get("ems_budget_runtime_veto"))
    )
    mode_name = str(payload.get("mode_name") or payload.get("mode") or "AUTO").upper()
    value_w = max(0, _safe_int(payload.get("val"), 0))
    commands_allowed = (
        bool(phase5.get("commands_allowed") and executable)
        if explicit_phase5
        else bool(executable and candidate_action in ACTIVE_ACTIONS and mode_name != "AUTO" and value_w > 0)
    )
    block_reason = phase5.get("block_reason_code") if explicit_phase5 else validation.get("block_reason_code")
    if not explicit_phase5:
        if validation.get("valid") and not selected:
            block_reason = "CANDIDATE_NOT_SELECTED"
        elif selected and not live_valid:
            block_reason = "RUNTIME_DATA_INVALID"
        elif selected and live_valid and not commands_allowed:
            block_reason = "HOLD_OR_ZERO_BUDGET"
        if commands_allowed:
            block_reason = None
    elif selected and not live_valid:
        block_reason = "RUNTIME_DATA_INVALID_AFTER_SELECTION"

    charge_budget_w = value_w if commands_allowed and candidate_action in {"PV_STORE", "GRID_CHARGE"} else 0
    export_budget_w = value_w if commands_allowed and candidate_action in {"ECONOMIC_EXPORT", "HEADROOM_EXPORT"} else 0
    power_diag = payload.get("rscp_power_settings") if isinstance(payload.get("rscp_power_settings"), dict) else {}
    readback = power_diag.get("readback") if isinstance(power_diag.get("readback"), dict) else {}
    acknowledged = bool(power_diag.get("acknowledged") or power_diag.get("response_codes") is not None or power_diag.get("confirmed"))
    confirmed = bool(power_diag.get("confirmed"))
    requested = {
        "mode": mode_name,
        "mode_value": _safe_int(payload.get("mode"), 0),
        "power_w": value_w,
        "rscp_path": payload.get("rscp_command_path"),
        "issued_by": "storage_manager",
        "issued": bool(phase5.get("requested")) if explicit_phase5 else bool(selected and executable),
        "dispatch_authorized": bool(selected and executable and commands_allowed),
    }
    return {
        "schema_version": RUNTIME_SCHEMA,
        "runtime_generated_at": _utc_iso(now_value),
        "runtime_generated_at_ts_ms": now_value,
        "plan_id": validation.get("plan_id"),
        "slot_id": validation.get("slot_id"),
        "plan_valid": bool(validation.get("valid")),
        "plan_age_s": validation.get("age_s"),
        "candidate": {
            "action": candidate_action,
            "power_w": _round_or_none(candidate.get("power_w"), 3) or 0.0,
            "status": candidate.get("status"),
            "candidate": bool(candidate.get("candidate", candidate_action != "HOLD")),
            "selected": selected,
            "executable": executable,
            "commands_allowed": commands_allowed,
            "block_reason_code": candidate.get("block_reason_code") or block_reason,
            "economic_export_gate": copy.deepcopy(candidate.get("economic_export_gate")),
            "headroom_gate": copy.deepcopy(candidate.get("headroom_gate")),
        },
        "actual_manager_action": actual_action,
        "selected": selected,
        "executable": executable,
        "commands_allowed": commands_allowed,
        "owner": "storage_manager",
        "selection_source": phase5.get("selected_source") if explicit_phase5 else "legacy_runtime_projection",
        "plan_runtime_selection_invariant": direct_marketing_binding,
        "block_reason_code": block_reason,
        "technical_block_reason_code": phase5.get("technical_block_reason_code") if explicit_phase5 else block_reason,
        "blockers": copy.deepcopy(phase5.get("blockers")) if explicit_phase5 else ([block_reason] if block_reason else []),
        "charge_budget_w": charge_budget_w,
        "export_budget_w": export_budget_w,
        "requested": requested,
        "ack": {
            "acknowledged": acknowledged,
            "dispatch_acknowledged": bool(explicit_phase5 and phase5.get("requested") and acknowledged),
            "settings_acknowledged": acknowledged,
            "scope": "POWER_SETTINGS_ONLY_NO_SET_POWER_ACK",
            "status": power_diag.get("status"),
            "response_codes": power_diag.get("response_codes"),
            "ts_ms": _to_ts_ms(power_diag.get("ts")) or None,
        },
        "readback": {
            "confirmed": confirmed,
            "fresh": bool(power_diag.get("readback_source") == "canonical_live" or power_diag.get("stage") == "live_reconciliation"),
            "status": power_diag.get("status"),
            "values": readback or None,
            "ts_ms": _to_ts_ms(power_diag.get("readback_cycle_ts", power_diag.get("ts"))) or None,
        },
        "physics": {
            "battery_power_w": _round_or_none(live.get("Battery_Power", payload.get("bat_w")), 3),
            "grid_power_w": _round_or_none(live.get("Grid_Power", payload.get("grid_w")), 3),
            "soc_pct": _round_or_none(live.get("SOC", payload.get("soc")), 3),
            "valid": live_valid,
            "ts_ms": _to_ts_ms(live.get("_ts", payload.get("ts"))) or now_value,
        },
        "legacy_baseline": copy.deepcopy(phase5.get("legacy_baseline")) if explicit_phase5 else None,
        "phase5": copy.deepcopy(phase5) if explicit_phase5 else None,
    }


def canonical_projection_for_ts(plan: Dict[str, Any], ts_ms: int) -> Optional[Dict[str, Any]]:
    """Liefert nur die veröffentlichte Projektion, ohne fachliche Neuberechnung."""

    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA:
        return None
    for slot in plan.get("slots") if isinstance(plan.get("slots"), list) else []:
        if not isinstance(slot, dict):
            continue
        if _safe_int(slot.get("start_ts_ms"), 0) <= ts_ms < _safe_int(slot.get("end_ts_ms"), 0):
            projection = slot.get("projection")
            if isinstance(projection, dict):
                return {
                    "plan_id": plan.get("plan_id"),
                    "slot_id": slot.get("slot_id"),
                    "projection": copy.deepcopy(projection),
                }
    return None
