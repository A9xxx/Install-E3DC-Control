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

try:
    from .direct_marketing_identity import (
        PASSIVE_NORMAL_BINDING_SCHEMA,
        passive_normal_identity,
    )
    from .direct_marketing_actions import (
        DIRECT_MARKETING_RELEASED_PLAN_ACTIONS,
        direct_marketing_export_gate_contract_valid,
        direct_marketing_source_action_mode_valid,
        direct_marketing_source_action_released,
        direct_marketing_target_for_plan_action,
        storage_action_contract,
    )
except ImportError:
    from direct_marketing_identity import (
        PASSIVE_NORMAL_BINDING_SCHEMA,
        passive_normal_identity,
    )
    from direct_marketing_actions import (  # type: ignore
        DIRECT_MARKETING_RELEASED_PLAN_ACTIONS,
        direct_marketing_export_gate_contract_valid,
        direct_marketing_source_action_mode_valid,
        direct_marketing_source_action_released,
        direct_marketing_target_for_plan_action,
        storage_action_contract,
    )

PLAN_SCHEMA = "storage_dispatch_plan_v1"
RUNTIME_SCHEMA = "storage_dispatch_runtime_v1"
EFFECTIVE_STORAGE_PLAN_SCHEMA = "storage_effective_plan_v1"
ADAPTER_VERSION = "legacy_storage_plan_adapter_v1"
SHADOW_INPUT_BINDING_SCHEMA = "storage_dispatch_shadow_input_binding_v2"
PRICE_HORIZON_SCHEMA = "storage_dispatch_price_horizon_v2"
DIRECT_MARKETING_PLAN_PROJECTION_SCHEMA = "direct_marketing_plan_projection_v1"
DIRECT_MARKETING_TRAJECTORY_SCHEMA = "direct_marketing_trajectory_v1"
DIRECT_MARKETING_SELECTED_ACTION_FALLBACK_SCHEMA = (
    "direct_marketing_selected_action_fallback_v1"
)
STORAGE_PLAN_ACTION_PROJECTION_SCHEMA = "storage_plan_action_projection_v1"
PV_ZERO_EVIDENCE_SCHEMA = "pv_zero_evidence_v1"
DIRECT_MARKETING_PV_AXIS_EVIDENCE_SCHEMA = (
    "direct_marketing_pv_axis_evidence_v1"
)
DIRECT_MARKETING_PASSIVE_NORMAL_BINDING_SCHEMA = PASSIVE_NORMAL_BINDING_SCHEMA
TIMEZONE = "Europe/Berlin"
SLOT_DURATION_S = 900
ACTIVE_ACTIONS = {"PV_STORE", "GRID_CHARGE", "ECONOMIC_EXPORT", "HEADROOM_EXPORT", "HOUSE_SUPPLY", "CHARGE_BLOCK_WAIT"}
DIRECT_MARKETING_ACTION_ROLES_SCHEMA = "direct_marketing_action_roles_v1"
SHADOW_EXECUTION_READINESS_SCHEMA = "shadow_execution_readiness_v1"
SHADOW_EXECUTION_REVISION_KEYS = ("schema_version", "plan_id", "slot_id")
DIRECT_MARKETING_EXECUTION_READY_ACTIONS = frozenset(
    DIRECT_MARKETING_RELEASED_PLAN_ACTIONS
)
DIRECT_MARKETING_RUNTIME_ACTION_ROLES_SCHEMA = (
    "direct_marketing_runtime_action_roles_v1"
)
FORECAST_SHORTFALL_AUX_AC_ACTION_MATERIAL_KEYS = ("action", "mode", "charge_w", "target_soc_pct", "source")
FORECAST_SHORTFALL_AUX_AC_RELEASED = False
FORECAST_SHORTFALL_JOINT_HORIZON_SCHEMA = (
    "storage_forecast_joint_horizon_contract_v1"
)

def shadow_slot_forecast_complete(slot: Dict[str, Any]) -> bool:
    if not isinstance(slot, dict):
        return False
    if slot.get("forecast_complete") or slot.get("forecast_fresh"):
        return True
    forecast = (
        slot.get("forecast_w")
        if isinstance(slot.get("forecast_w"), dict)
        else {}
    )
    evidence = (
        forecast.get("evidence")
        if isinstance(forecast.get("evidence"), dict)
        else {}
    )
    quantiles = (
        forecast.get("quantile_contract")
        if isinstance(forecast.get("quantile_contract"), dict)
        else {}
    )
    return bool(
        evidence.get("pv_fresh") is True
        and evidence.get("load_valid") is True
        and quantiles.get("status") == "complete"
    )

_PLAN_SNAPSHOT_MAX_BYTES = 16 * 1024 * 1024
# Der Runtime-Consumer benötigt ausschließlich die aktuell gebundene
# Dateigeneration. Alte Revisionen sind ausdrücklich kein Fallback und dürfen
# auf kleinen Anlagenrechnern nicht als vollständige Python-Objektgraphen
# resident bleiben.
_PLAN_SNAPSHOT_CACHE_LIMIT = 1
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
    "direct_marketing_trajectory",
)


class _ImmutablePlanList(list):
    """Einmal materialisierte, JSON-kompatible und unveränderliche Sequenz."""

    def __init__(self, values: Any = ()) -> None:
        list.__init__(self, (_freeze_plan_value(value) for value in values))

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
        source_generation: Optional[Tuple[int, int, int, int, int]],
        source_sha256: str,
    ) -> None:
        # Verschachtelte Container werden genau einmal eingefroren. Die frühere
        # lazy Sicht kopierte bei jedem ``get()`` große Slotlisten erneut.
        dict.__init__(
            self,
            ((key, _freeze_plan_value(value)) for key, value in plan.items()),
        )
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
        dict.__init__(
            frozen,
            ((key, _freeze_plan_value(item)) for key, item in value.items()),
        )
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
    dict.__init__(
        frozen,
        ((key, _freeze_plan_value(item)) for key, item in value.items()),
    )
    return frozen


def _plan_file_generation(info: os.stat_result) -> Tuple[int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


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


def _quantile_axis_contract(
    row: Dict[str, Any],
    axis: str,
    p10: Optional[float],
    p50: Optional[float],
    p90: Optional[float],
) -> Dict[str, Any]:
    """Normalisiert ausschließlich explizit deklarierte Prognosequantile.

    Intern gilt die CDF-/Nichtüberschreitungsreihenfolge P10 <= P50 <= P90.
    Eine deklarierte Überschreitungsreihenfolge wird erst nach erfolgreicher
    Ordnungs-, Quellen-, Revisions- und Frischeprüfung umgedreht.
    Punktprognosen werden nie als P50 ergänzt.
    """

    convention_raw = str(
        row.get(f"{axis}_quantile_convention") or ""
    ).strip().lower()
    source = str(row.get(f"{axis}_quantile_source") or "").strip()
    revision = str(row.get(f"{axis}_quantile_revision") or "").strip()
    fresh = row.get(f"{axis}_quantile_fresh") is True
    explicit = all(
        value is not None and value >= 0.0
        for value in (p10, p50, p90)
    )
    cdf_conventions = {"cdf", "cdf_non_exceedance"}
    exceedance_conventions = {
        "exceedance",
        "probability_of_exceedance",
    }
    convention_supported = convention_raw in (
        cdf_conventions | exceedance_conventions
    )
    order_valid = bool(
        explicit
        and (
            (
                convention_raw in cdf_conventions
                and float(p10) <= float(p50) <= float(p90)
            )
            or (
                convention_raw in exceedance_conventions
                and float(p10) >= float(p50) >= float(p90)
            )
        )
    )
    complete = bool(
        explicit
        and convention_supported
        and order_valid
        and source
        and revision
        and fresh
    )
    normalized = (p10, p50, p90)
    if complete and convention_raw in exceedance_conventions:
        normalized = (p90, p50, p10)
    return {
        "schema_version": "forecast_quantile_axis_v1",
        "status": "complete" if complete else "evidence_limit",
        "input_convention": convention_raw or None,
        "canonical_convention": (
            "cdf_non_exceedance" if complete else None
        ),
        "source": source or None,
        "revision": revision or None,
        "fresh": fresh,
        "explicit": explicit,
        "order_valid": order_valid,
        "p10": normalized[0] if complete else None,
        "p50": normalized[1] if complete else None,
        "p90": normalized[2] if complete else None,
    }


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


def forecast_shortfall_joint_horizon_contract(
    plan: Optional[Dict[str, Any]],
    *,
    horizon_start_ts_ms: int,
    horizon_end_ts_ms: int,
    risk_threshold_pct: float,
    topology_revision: str,
) -> Dict[str, Any]:
    """Definiert den noch nicht freigegebenen Joint-Horizon-Vertrag.

    Der Produktstand besitzt weder einen freigegebenen Produzenten noch eine
    fachlich beschlossene Risikoschwelle. Deshalb bleibt selbst mit
    synthetisch vollständig wirkenden Eingaben jede Entscheidungs- und
    Action-Autorität gesperrt. Marginale PV- und Lastquantile dürfen hier
    insbesondere nicht zu einem Nettoquantil verrechnet werden.
    """

    source = plan if isinstance(plan, dict) else {}
    evidence = source.get("forecast_shortfall_joint_horizon_evidence")
    evidence_present = isinstance(evidence, dict) and bool(evidence)
    start_ms = (
        int(horizon_start_ts_ms)
        if type(horizon_start_ts_ms) is int
        and horizon_start_ts_ms > 0
        else None
    )
    end_ms = (
        int(horizon_end_ts_ms)
        if type(horizon_end_ts_ms) is int
        and horizon_end_ts_ms > 0
        else None
    )
    risk_raw = risk_threshold_pct
    risk_value = (
        float(risk_raw)
        if not isinstance(risk_raw, bool)
        and isinstance(risk_raw, (int, float))
        and math.isfinite(float(risk_raw))
        else None
    )
    topology_value = str(topology_revision or "").strip()
    blockers = ["joint_horizon_producer_not_released"]
    if start_ms is None or end_ms is None or end_ms <= start_ms:
        blockers.append("joint_horizon_window_invalid")
    if risk_value is None or not 0.0 < risk_value <= 100.0:
        blockers.append("joint_horizon_risk_threshold_missing")
    if not topology_value:
        blockers.append("joint_horizon_topology_revision_missing")

    return {
        "schema_version": FORECAST_SHORTFALL_JOINT_HORIZON_SCHEMA,
        "valid": False,
        "evidence_status": "EVIDENCE_LIMIT",
        "blocker": blockers[0],
        "blockers": blockers,
        "released": FORECAST_SHORTFALL_AUX_AC_RELEASED,
        "producer_evidence_present": evidence_present,
        "start_ts_ms": start_ms,
        "end_ts_ms": end_ms,
        "slot_count": 0,
        "risk_threshold_pct": risk_value,
        "topology_revision": topology_value or None,
        "revision": None,
        "dc_forecast_revision": None,
        "external_ac_forecast_revision": None,
        "load_forecast_revision": None,
        "joint_horizon_evidence_revision": None,
        "taper_model": None,
        "taper_revision": None,
        "modeled_shortfall_probability_floor_pct": None,
        "chargeable_energy_quantile_wh": None,
        "optimistic_chargeable_wh": None,
        "shortfall_claim": "none_joint_horizon_producer_not_released",
        "decision_use_allowed": False,
        "marginal_quantile_arithmetic_allowed": False,
        "action_materializable": False,
    }


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
        "pv_quantile_convention",
        "pv_quantile_source",
        "pv_quantile_revision",
        "pv_quantile_fresh",
        "external_ac_pv_w",
        "pv_forecast_fresh",
        "forecast_fresh",
        "pv_forecast_freshness_source",
        "pv_store_forecast_fresh",
        "pv_store_forecast_freshness_source",
        "pv_zero_evidence",
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
    pv_e3dc_dc_keys = (
        "e3dc_dc_pv_w",
        "e3dc_dc_pv_p10_w",
        "e3dc_dc_pv_p50_w",
        "e3dc_dc_pv_p90_w",
        "e3dc_dc_pv_quantile_convention",
        "e3dc_dc_pv_quantile_source",
        "e3dc_dc_pv_quantile_revision",
        "e3dc_dc_pv_quantile_fresh",
        "e3dc_dc_pv_quantile_generated_ts_ms",
        "e3dc_dc_pv_quantile_lead_time_bucket",
        "e3dc_dc_pv_quantile_lead_time_min_minutes",
        "e3dc_dc_pv_quantile_lead_time_max_minutes",
        "e3dc_dc_pv_quantile_decision_use_allowed",
        "pv_topology_status",
        "pv_topology_reason",
        "pv_topology_revision",
        "pv_topology_source",
        "pv_topology_quality",
        "pv_resource_projection_status",
        "pv_resource_projection_reason",
        "pv_resource_contributions",
    )
    pv_external_ac_keys = (
        "external_ac_pv_w",
        "pv_external_ac_capped_w",
        "external_ac_pv_p10_w",
        "external_ac_pv_p50_w",
        "external_ac_pv_p90_w",
        "external_ac_pv_quantile_convention",
        "external_ac_pv_quantile_source",
        "external_ac_pv_quantile_revision",
        "external_ac_pv_quantile_fresh",
        "external_ac_pv_quantile_generated_ts_ms",
        "external_ac_pv_quantile_lead_time_bucket",
        "external_ac_pv_quantile_lead_time_min_minutes",
        "external_ac_pv_quantile_lead_time_max_minutes",
        "external_ac_pv_quantile_decision_use_allowed",
        "pv_topology_status",
        "pv_topology_reason",
        "pv_topology_revision",
        "pv_topology_source",
        "pv_topology_quality",
        "pv_resource_projection_status",
        "pv_resource_projection_reason",
        "pv_resource_contributions",
    )
    calibration_keys = tuple(
        "%s_quantile_%s" % (axis, field)
        for axis in ("pv", "e3dc_dc_pv", "external_ac_pv", "load")
        for field in (
            "calibration_status",
            "calibration_method",
            "calibration_revision",
            "calibration_sample_count",
            "calibration_day_count",
            "calibration_window_start_ts_ms",
            "calibration_window_end_ts_ms",
            "decision_use_allowed",
        )
    )
    load_keys = (
        "home_w",
        "home_source",
        "home_quality",
        "wp_w",
        "wp_source",
        "wp_quality",
        "climate_w",
        "climate_source",
        "climate_quality",
        "wb_w",
        "wb2_w",
        "planned_load_w",
        "load_p10_w",
        "load_p50_w",
        "load_p90_w",
        "load_quantile_convention",
        "load_quantile_source",
        "load_quantile_revision",
        "load_quantile_fresh",
    )
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
            "heat_price_boost_config",
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
        "pv_e3dc_dc_ensemble": revision_hash(selected(pv_e3dc_dc_keys)),
        "pv_external_ac_ensemble": revision_hash(selected(pv_external_ac_keys)),
        "load_ensemble": revision_hash(selected(load_keys)),
        "forecast_calibration": revision_hash(selected(calibration_keys)),
        "state": revision_hash({
            "current_soc": plan.get("current_soc"),
            "timeline": selected(state_keys),
        }),
        "hardware_limits": revision_hash(hardware),
        "config": revision_hash(config),
        "policy": revision_hash(policy),
    }


def _execution_binding_input_revisions(
    revisions: Dict[str, str],
    *,
    generated_at_ts_ms: int,
    valid_from_ts_ms: int,
    valid_until_ts_ms: int,
    horizon_end_ts_ms: int,
) -> Dict[str, str]:
    """Ergänzt vor der Planversiegelung die strukturelle Ausführungslinie.

    Die Schlüssel heißen entsprechend dem späteren Verbrauchervertrag,
    enthalten aber bewusst keine rückwärts auf sich selbst zeigende finale
    ``plan_id``/``slot_id``. Stattdessen binden sie die unveränderliche
    Planerzeugung und den aktuellen Gültigkeitsslot. Der äußere Planhash bindet
    diese Revisionen anschließend an die tatsächliche Plan- und Slotidentität.
    """

    base = copy.deepcopy(revisions if isinstance(revisions, dict) else {})
    schema_revision = revision_hash({"schema_version": PLAN_SCHEMA})
    plan_generation_revision = revision_hash({
        "schema_version_revision": schema_revision,
        "generated_at_ts_ms": int(generated_at_ts_ms),
        "valid_from_ts_ms": int(valid_from_ts_ms),
        "valid_until_ts_ms": int(valid_until_ts_ms),
        "horizon_end_ts_ms": int(horizon_end_ts_ms),
        "input_revisions": copy.deepcopy(base),
    })
    slot_generation_revision = revision_hash({
        "plan_generation_revision": plan_generation_revision,
        "start_ts_ms": int(valid_from_ts_ms),
        "end_ts_ms": int(valid_until_ts_ms),
    })
    base.update({
        "schema_version": schema_revision,
        "plan_id": plan_generation_revision,
        "slot_id": slot_generation_revision,
    })
    return base


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


def _projection_quality_usable(value: Any, *, not_applicable_ok: bool) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return False
    if not_applicable_ok and "not_applicable" in normalized:
        return True
    return not any(
        marker in normalized
        for marker in ("missing", "unresolved", "unknown", "invalid", "stale")
    )


def _load_projection_evidence(
    row: Dict[str, Any],
    load_point_w: Optional[float],
) -> Dict[str, Any]:
    """Bindet Lastgültigkeit ohne eine nicht vorhandene Providerfrische zu erfinden."""

    home_source = str(row.get("home_source") or "").strip()
    home_valid = bool(
        load_point_w is not None
        and home_source
        and "unresolved" not in home_source.lower()
        and _projection_quality_usable(
            row.get("home_quality"),
            not_applicable_ok=False,
        )
    )

    def optional_component_valid(
        power_key: str,
        source_key: str,
        quality_key: str,
    ) -> bool:
        power = _safe_float(row.get(power_key), None)
        if power is None:
            return False
        quality = row.get(quality_key)
        if abs(power) <= 0.001 and _projection_quality_usable(
            quality,
            not_applicable_ok=True,
        ):
            return True
        source = str(row.get(source_key) or "").strip()
        return bool(
            source
            and "unresolved" not in source.lower()
            and _projection_quality_usable(
                quality,
                not_applicable_ok=False,
            )
        )

    explicit_loads_valid = all(
        _safe_float(row.get(key), None) is not None
        for key in ("wb_w", "wb2_w", "planned_load_w")
        if key in row
    )
    load_valid = bool(
        home_valid
        and optional_component_valid("wp_w", "wp_source", "wp_quality")
        and optional_component_valid(
            "climate_w",
            "climate_source",
            "climate_quality",
        )
        and explicit_loads_valid
    )
    return {
        "load_valid": load_valid,
        "load_validity_source": "component_projection_quality_v1",
        "load_quality": "complete" if load_valid else "missing_or_invalid",
    }


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
    pv_p50_w = _safe_float(row.get("pv_p50_w"), None)
    pv_p90_w = _safe_float(row.get("pv_p90_w"), None)
    load_p10_w = _safe_float(row.get("load_p10_w"), None)
    load_p50_w = _safe_float(row.get("load_p50_w"), None)
    load_p90_w = _safe_float(row.get("load_p90_w"), None)
    load_evidence = _load_projection_evidence(row, total_load_w)
    pv_quantiles = _quantile_axis_contract(
        row,
        "pv",
        pv_p10_w,
        pv_p50_w,
        pv_p90_w,
    )
    load_quantiles = _quantile_axis_contract(
        row,
        "load",
        load_p10_w,
        load_p50_w,
        load_p90_w,
    )
    quantiles_available = bool(
        pv_quantiles.get("status") == "complete"
        and load_quantiles.get("status") == "complete"
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
            "point": round(pv_w, 3),
            "p10": _round_or_none(pv_quantiles.get("p10"), 3),
            "p50": _round_or_none(pv_quantiles.get("p50"), 3),
            "p90": _round_or_none(pv_quantiles.get("p90"), 3),
        },
        "load": {
            "point": round(total_load_w, 3),
            "p10": _round_or_none(load_quantiles.get("p10"), 3),
            "p50": _round_or_none(load_quantiles.get("p50"), 3),
            "p90": _round_or_none(load_quantiles.get("p90"), 3),
        },
        "house": {
            "point": round(home_w, 3),
            "p10": None,
            "p50": None,
            "p90": None,
        },
        "heat": {
            "point": round(heat_w, 3),
            "p10": None,
            "p50": None,
            "p90": None,
        },
        "wallbox": {
            "point": round(wallbox_w, 3),
            "p10": None,
            "p50": None,
            "p90": None,
        },
        "external_ac_pv": {
            "point": _round_or_none(row.get("external_ac_pv_w"), 3),
            "p10": None,
            "p50": None,
            "p90": None,
        },
        "quantile_contract": {
            "schema_version": "forecast_quantile_contract_v1",
            "status": (
                "complete" if quantiles_available else "evidence_limit"
            ),
            "canonical_convention": (
                "cdf_non_exceedance" if quantiles_available else None
            ),
            "pv": pv_quantiles,
            "load": load_quantiles,
        },
        "evidence": {
            "pv_fresh": bool(
                row.get(
                    "pv_store_forecast_fresh",
                    row.get(
                        "pv_forecast_fresh",
                        row.get("forecast_fresh"),
                    ),
                )
                is True
            ),
            "pv_freshness_source": str(
                row.get("pv_store_forecast_freshness_source")
                or row.get("pv_forecast_freshness_source")
                or "unconfirmed"
            ),
            **load_evidence,
        },
    }
    if isinstance(row.get("pv_zero_evidence"), dict):
        forecast_contract["evidence"]["pv_zero_evidence"] = copy.deepcopy(
            row.get("pv_zero_evidence")
        )
    if topology_bound:
        forecast_contract["e3dc_dc_pv"] = {
            "point": _round_or_none(row.get("e3dc_dc_pv_w"), 3),
            "p10": None,
            "p50": None,
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
            "explicit_cdf_p10_p50_p90"
            if quantiles_available
            else "deterministic_point_without_quantile_claim"
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
    # Der Trajektorienvertrag wurde nach dem v1-Plan eingeführt. Sein Fehlen
    # darf alte, DV-deaktivierte Planabbilder nicht durch ein neu erfundenes
    # ``None`` im Hashmaterial ungültig machen.
    material = {
        key: plan.get(key)
        for key in _PLAN_MATERIAL_KEYS
        if key != "direct_marketing_trajectory" or key in plan
    }
    source_planner = (
        material.get("planner")
        if isinstance(material.get("planner"), dict)
        else {}
    )
    planner = copy.deepcopy(source_planner)
    # Der neue DV-Planer ist in dieser Phase reine Diagnose. Seine Revision
    # darf deshalb weder die produktive plan_id noch daraus abgeleitete
    # slot_id-/Runtime-Generationen verändern.
    planner.pop("dv_shadow_v1", None)
    material["planner"] = planner
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
    source_trajectory = (
        material.get("direct_marketing_trajectory")
        if isinstance(material.get("direct_marketing_trajectory"), dict)
        else None
    )
    if source_trajectory is not None:
        trajectory = copy.deepcopy(source_trajectory)
        # Selbstidentitäten werden erst nach dem fachlichen Planhash gesetzt.
        # Alle physikalischen Werte, Aktionen, Lasten und Revisionen bleiben
        # dagegen vollständig Teil des Planmaterials.
        trajectory["plan_id"] = None
        trajectory["trajectory_revision"] = None
        trajectory["slots"] = [
            {**item, "slot_id": None}
            if isinstance(item, dict)
            else item
            for item in trajectory.get("slots") or []
        ]
        material["direct_marketing_trajectory"] = trajectory
    return material


def _materialize_heat_intent_candidate(
    source: Dict[str, Any],
    canonical: Dict[str, Any],
    generation_slot: Dict[str, Any],
) -> Dict[str, Any]:
    """Erzeugt einen plan-/slotgebundenen, strikt wirkungslosen Heat-Candidate.

    Der Candidate wird erst nach ``plan_id`` und ``slot_id`` gebildet und ist
    deshalb absichtlich kein Bestandteil von ``_PLAN_MATERIAL_KEYS``. Sein
    eigener Hash bindet zurück auf genau diese Planrevision. Laufzeitbedingungen
    werden hier nicht erfunden; sie bleiben bis zur Heat-Policy-Auswertung
    unvollständig. Auch die vorhandenen Punktprognosen werden nicht künstlich
    als kalibrierte Quantile ausgegeben.
    """

    try:
        try:
            from .Heat import intent as heat_intent
        except ImportError:
            from Heat import intent as heat_intent  # type: ignore

        config = (
            source.get("heat_price_boost_config")
            if isinstance(source.get("heat_price_boost_config"), dict)
            else {}
        )
        revisions = (
            canonical.get("input_revisions")
            if isinstance(canonical.get("input_revisions"), dict)
            else {}
        )
        plan_id = str(canonical.get("plan_id") or "")
        slot_id = str(generation_slot.get("slot_id") or "")
        horizon_start_ms = _safe_int(generation_slot.get("start_ts_ms"), 0)
        required_horizon_h = 24.0
        horizon_end_ms = horizon_start_ms + int(required_horizon_h * 3_600_000)
        cursor_ms = horizon_start_ms
        covered_ms = 0
        heat_wh = 0.0
        pv_cover_wh = 0.0
        values_complete = True
        freshness_complete = True

        slots = sorted(
            (
                slot
                for slot in canonical.get("slots") or []
                if isinstance(slot, dict)
            ),
            key=lambda slot: _safe_int(slot.get("start_ts_ms"), 0),
        )
        for slot in slots:
            start_ms = _safe_int(slot.get("start_ts_ms"), 0)
            end_ms = _safe_int(slot.get("end_ts_ms"), 0)
            if end_ms <= horizon_start_ms or start_ms >= horizon_end_ms:
                continue
            overlap_start_ms = max(horizon_start_ms, start_ms)
            overlap_end_ms = min(horizon_end_ms, end_ms)
            if end_ms <= start_ms or overlap_start_ms > cursor_ms:
                values_complete = False
                break
            forecast = (
                slot.get("forecast_w")
                if isinstance(slot.get("forecast_w"), dict)
                else {}
            )
            evidence = (
                forecast.get("evidence")
                if isinstance(forecast.get("evidence"), dict)
                else {}
            )
            heat = (
                forecast.get("heat")
                if isinstance(forecast.get("heat"), dict)
                else {}
            )
            pv = (
                forecast.get("pv")
                if isinstance(forecast.get("pv"), dict)
                else {}
            )
            house = (
                forecast.get("house")
                if isinstance(forecast.get("house"), dict)
                else {}
            )
            wallbox = (
                forecast.get("wallbox")
                if isinstance(forecast.get("wallbox"), dict)
                else {}
            )
            heat_w = _safe_float(heat.get("point"), None)
            pv_w = _safe_float(pv.get("point"), None)
            house_w = _safe_float(house.get("point"), None)
            wallbox_w = _safe_float(wallbox.get("point"), None)
            if any(
                value is None or value < 0.0
                for value in (heat_w, pv_w, house_w, wallbox_w)
            ):
                values_complete = False
            else:
                duration_h = (overlap_end_ms - overlap_start_ms) / 3_600_000.0
                heat_wh += float(heat_w) * duration_h
                pv_cover_wh += max(
                    0.0,
                    float(pv_w) - float(house_w) - float(wallbox_w),
                ) * duration_h
            freshness_complete = bool(
                freshness_complete
                and evidence.get("pv_fresh") is True
                and evidence.get("load_valid") is True
            )
            covered_ms += max(0, overlap_end_ms - overlap_start_ms)
            cursor_ms = max(cursor_ms, overlap_end_ms)
            if cursor_ms >= horizon_end_ms:
                break

        horizon_complete = bool(
            values_complete
            and cursor_ms >= horizon_end_ms
            and covered_ms >= int(required_horizon_h * 3_600_000)
        )
        raw_complete = bool(horizon_complete and freshness_complete)
        method_revision = heat_intent.revision_hash({
            "schema_version": "heat_intent_point_projection_method_v1",
            "horizon_h": required_horizon_h,
            "heat_source": "canonical_forecast_heat_point",
            "pv_coverage": "max_0_pv_minus_house_minus_wallbox",
            "quantile_claim": "unconfirmed_point_forecast",
        })
        pv_source_revision = heat_intent.revision_hash({
            "pv_ensemble": revisions.get("pv_ensemble"),
            "load_ensemble": revisions.get("load_ensemble"),
            "method_revision": method_revision,
        })
        evidence_status = "COMPLETE" if raw_complete else "EVIDENCE_LIMIT"
        forecast_evidence = heat_intent.seal_conservative_forecast_evidence({
            "schema_version": heat_intent.CONSERVATIVE_EVIDENCE_SCHEMA,
            "status": evidence_status,
            "plan_id": plan_id,
            "plan_revision": plan_id,
            "slot_id": slot_id,
            "slot_revision": slot_id,
            "method_revision": method_revision,
            "heat_need": {
                "status": evidence_status,
                "quantile": None,
                "value_kwh": round(max(0.0, heat_wh) / 1000.0, 4)
                if values_complete
                else None,
                "fresh": bool(freshness_complete),
                "required_horizon_h": required_horizon_h,
                "horizon_h": required_horizon_h,
                "coverage_h": round(max(0, covered_ms) / 3_600_000.0, 4),
                "horizon_complete": horizon_complete,
                "source_revision": revisions.get("load_ensemble"),
            },
            "pv_coverage": {
                "status": evidence_status,
                "quantile": None,
                "value_kwh": round(max(0.0, pv_cover_wh) / 1000.0, 4)
                if values_complete
                else None,
                "fresh": bool(freshness_complete),
                "required_horizon_h": required_horizon_h,
                "horizon_h": required_horizon_h,
                "coverage_h": round(max(0, covered_ms) / 3_600_000.0, 4),
                "horizon_complete": horizon_complete,
                "source_revision": pv_source_revision,
            },
        })

        scope = str(config.get("heat_price_boost_scope") or "both").strip().lower()
        driver_class = str(
            config.get("heatpump_driver_class") or "unavailable"
        )
        allowed_scopes = (
            list(config.get("heatpump_allowed_scopes"))
            if isinstance(config.get("heatpump_allowed_scopes"), list)
            else []
        )
        if driver_class == "combined_sg_ready":
            scope = "both"
        config_revision = revisions.get("config")
        return heat_intent.build_heat_intent_candidate(
            binding={
                "plan_id": plan_id,
                "plan_revision": plan_id,
                "slot_id": slot_id,
                "slot_revision": slot_id,
                "slot_start_ts_ms": horizon_start_ms,
                "slot_end_ts_ms": _safe_int(generation_slot.get("end_ts_ms"), 0),
            },
            request={
                "target_state": "BOOST",
                "scope": scope,
                "transition": "start",
                # Nutzerfreigabe ist keine wirtschaftliche Auswahl. Solange
                # kein kanonischer Wärme-Preisentscheid gebunden ist, bleibt
                # der Candidate ausdrücklich unselected.
                "selection_requested": False,
            },
            user={
                "enabled": bool(
                    _safe_bool(config.get("price_boost_enable"), False)
                    and _safe_bool(config.get("auto_mode"), True)
                ),
                "source": "canonical_heat_price_boost_config",
                "revision": config_revision,
            },
            capability={
                "available": bool(config.get("heatpump_configured") is True),
                "controllable": bool(config.get("heatpump_controllable") is True),
                "driver_class": driver_class,
                "allowed_scopes": allowed_scopes,
                "revision": config_revision,
            },
            forecast_evidence=forecast_evidence,
            constraints={
                "minimum_runtime": {
                    "satisfied": None,
                    "remaining_s": None,
                    "revision": None,
                },
                "restart": {
                    "allowed": None,
                    "remaining_s": None,
                    "revision": None,
                },
                "protection": {
                    "clear": None,
                    "reasons": None,
                    "revision": None,
                },
            },
        )
    except Exception:
        # Der neue Vertrag ist noch reine Diagnose. Ein Fehler darf den
        # bestehenden Storage-Plan niemals verändern oder dessen Writer
        # abbrechen; downstream bleibt der unversiegelte Candidate fail-closed.
        return {
            "schema_version": "heat_intent_candidate_v1",
            "shadow_only": True,
            "commands_allowed": False,
            "status": "EVIDENCE_LIMIT",
            "eligibility_status": "INELIGIBLE",
            "eligible": False,
            "selection_requested": False,
            "selected": False,
            "executable": False,
            "confirmed": False,
            "reason_codes": ["HEAT_INTENT_CANDIDATE_BUILD_ERROR"],
        }


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
            "pv_e3dc_dc_ensemble",
            "pv_external_ac_ensemble",
            "load_ensemble",
            "forecast_calibration",
            "state",
            "hardware_limits",
            "config",
            "policy",
            *SHADOW_EXECUTION_REVISION_KEYS,
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
            "pv_e3dc_dc_source_revision": revisions.get(
                "pv_e3dc_dc_ensemble"
            ),
            "pv_external_ac_source_revision": revisions.get(
                "pv_external_ac_ensemble"
            ),
            "load_source_revision": revisions.get("load_ensemble"),
            "calibration_source_revision": revisions.get(
                "forecast_calibration"
            ),
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


def _direct_marketing_pv_store_live_dc_fallback_projection(
    selected: Dict[str, Any],
    plan_window: Dict[str, Any],
    slot: Dict[str, Any],
    *,
    source_action: str,
    source_mode: str,
    action_id: str,
    window_id: str,
    window_start_ts_ms: int,
    window_end_ts_ms: int,
) -> Optional[Dict[str, Any]]:
    """Projiziert die eng gebundene PV_STORE-v2-DC-Freigabe.

    Der Producer veröffentlicht den Fallback ausschließlich im aktuellen
    negativen Rohpreis-Slot. Auswahl und kanonisches Planfenster müssen die
    identischen Marker tragen; fehlende oder widersprüchliche Angaben werden
    nicht ergänzt. Die Projektion ist nur eine flüchtige E3/DC-DC-AUTO-
    Erlaubnis und erteilt insbesondere keinen Netz-/AC-Ladebefehl.
    """

    if not bool(
        source_action == "eco_plus_store_pv_candidate"
        and source_mode in {"eco", "eco_plus"}
        and isinstance(action_id, str)
        and action_id.startswith("sha256:")
        and len(action_id) == 71
        and window_id
        and 0 < window_start_ts_ms < window_end_ts_ms
    ):
        return None

    marker_keys = (
        "pv_store_live_dc_fallback",
        "pv_store_live_dc_fallback_contract_version",
        "pv_store_runtime_measurement_required",
        "pv_store_runtime_source_contract",
        "pv_store_live_dc_fallback_max_power_w",
        "target_soc_pct",
        "pv_store_raw_market_price_ct_kwh",
        "pv_store_raw_market_price_source",
        "pv_store_raw_market_price_resolution_min",
        "pv_store_market_price_revision_sha256",
        "market_window_id",
        "market_window_start_ts",
        "market_window_end_ts",
        "pv_store_dc_only_enable",
        "pv_store_aux_ac_storage_allowed",
        "pv_store_source_contract",
        "grid_ac_allowed",
    )
    if any(selected.get(key) != plan_window.get(key) for key in marker_keys):
        return None
    if not bool(
        selected.get("pv_store_live_dc_fallback") is True
        and type(selected.get("pv_store_live_dc_fallback_contract_version")) is int
        and selected.get("pv_store_live_dc_fallback_contract_version") == 2
        and selected.get("pv_store_runtime_measurement_required") is False
        and selected.get("pv_store_runtime_source_contract")
        in {
            "E3DC_DC_AUTO_CAP_RAW_PRICE",
            "E3DC_DC_AUTO_CAP_LUOX_ZERO_EXPORT",
        }
        and selected.get("pv_store_dc_only_enable") is True
        and selected.get("pv_store_aux_ac_storage_allowed") is False
        and selected.get("pv_store_source_contract") == "E3DC_DC"
        and selected.get("grid_ac_allowed") is False
    ):
        return None

    raw_price = _safe_float(
        selected.get("pv_store_raw_market_price_ct_kwh"),
        None,
    )
    target_soc = _safe_float(selected.get("target_soc_pct"), None)
    runtime_cap_w = _safe_float(
        selected.get("pv_store_live_dc_fallback_max_power_w"),
        None,
    )
    raw_price_source = str(
        selected.get("pv_store_raw_market_price_source") or ""
    ).strip()
    price_revision = str(
        selected.get("pv_store_market_price_revision_sha256") or ""
    ).strip()
    market_window_id = str(selected.get("market_window_id") or "").strip()
    market_window_start_ms = _to_ts_ms(selected.get("market_window_start_ts"))
    market_window_end_ms = _to_ts_ms(selected.get("market_window_end_ts"))
    slot_prices = (
        slot.get("prices_ct_kwh")
        if isinstance(slot.get("prices_ct_kwh"), dict)
        else {}
    )
    slot_raw_price = _safe_float(slot_prices.get("gross_sell"), None)
    tariff_revision = str(slot_prices.get("tariff_revision") or "").strip()
    if not bool(
        raw_price is not None
        and math.isfinite(raw_price)
        and raw_price < 0.0
        and slot_raw_price is not None
        and math.isfinite(slot_raw_price)
        and abs(raw_price - slot_raw_price) <= 0.0001
        and slot_prices.get("fresh") is True
        and tariff_revision
        and raw_price_source
        and type(selected.get("pv_store_raw_market_price_resolution_min")) is int
        and selected.get("pv_store_raw_market_price_resolution_min") == 15
        and price_revision
        and market_window_id == window_id
        and market_window_start_ms <= window_start_ts_ms
        and window_end_ts_ms <= market_window_end_ms
        and target_soc is not None
        and math.isfinite(target_soc)
        and 0.0 < target_soc <= 100.0
        and runtime_cap_w is not None
        and math.isfinite(runtime_cap_w)
        and runtime_cap_w >= 300.0
    ):
        return None

    return {
        "schema_version": "direct_marketing_pv_store_auto_dc_permission_v2",
        "valid": True,
        "execution_semantics": "PV_STORE_E3DC_AUTO_DC_PERMISSION",
        "forecast_imputed": False,
        "soc_effect": False,
        "runtime_measurement_required": False,
        "runtime_source_contract": selected.get(
            "pv_store_runtime_source_contract"
        ),
        "source": "E3DC_DC",
        "dc_only": True,
        "aux_ac_allowed": False,
        "grid_ac_allowed": False,
        "raw_market_price_ct_kwh": round(raw_price, 6),
        "raw_market_price_source": raw_price_source,
        "raw_market_price_resolution_min": 15,
        "market_price_revision_sha256": price_revision,
        "tariff_revision": tariff_revision,
        "market_window_id": market_window_id,
        "market_window_start_ts_ms": market_window_start_ms,
        "market_window_end_ts_ms": market_window_end_ms,
        "target_soc_pct": round(target_soc, 3),
        "runtime_cap_w": round(runtime_cap_w, 3),
        "action": "PV_STORE",
        "source_action": source_action,
        "source_mode": source_mode,
        "action_id": action_id,
        "window_id": window_id,
        "window_start_ts_ms": window_start_ts_ms,
        "window_end_ts_ms": window_end_ts_ms,
    }


def _direct_marketing_enabled(direct: Dict[str, Any]) -> bool:
    """Trennt nur den expliziten Config-Aus-Zustand von konfigurierter DV."""

    return not _direct_marketing_not_applicable(direct)


def _direct_marketing_execution_enabled(direct: Dict[str, Any]) -> bool:
    """Bindet ausführbare DV-Physik an denselben Vertrag wie der Planer."""

    flags = direct.get("flags") if isinstance(direct.get("flags"), dict) else {}
    return bool(
        direct.get("active") is True
        and direct.get("shadow") is False
        and _normalized_direct_marketing_mode(direct.get("mode"))
        in {"eco", "eco_plus", "arbitrage"}
        and flags.get("commands_allowed") is True
    )


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
        plan_window_id = str(
            execution.get("plan_window_id")
            or selected.get("plan_window_id")
            or selected.get("export_plateau_id")
            or selected.get("window_id")
            or ""
        )
        selected_plan_window_id = str(
            selected.get("plan_window_id")
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
            # Eine Ladesperre ist nur dann ein aktiver Hardwarevertrag, wenn
            # die Policy sie als aktuellen, slotgebundenen Planentscheid
            # veröffentlicht. HOLD und künftige Exportfenster genügen nicht.
            "CHARGE_BLOCK_WAIT": ("CHARGE_BLOCK_WAIT", {"direct_marketing_charge_block_wait"}, None),
        }
        action_contract = action_map.get(target_state)
        if action_contract is None:
            continue
        plan_action, allowed_source_actions, budget_key = action_contract
        allowed_source_modes = {
            "ECONOMIC_EXPORT": {"eco_plus"},
            "PV_STORE": {"eco", "eco_plus"},
            # Der Producer materialisiert den lückenlosen Warteslot-Vertrag
            # derzeit ausschließlich für Eco+. Der Consumer darf diese
            # Freigabe nicht vorsorglich auf weitere Strategien ausdehnen.
            "CHARGE_BLOCK_WAIT": {"eco_plus"},
        }.get(plan_action, set())
        action_budget_w = (
            max(0.0, _safe_float(budget.get(budget_key), 0.0) or 0.0)
            if budget_key
            else 0.0
        )
        protected_reserve_wh = _safe_float(budget.get("protected_reserve_wh"), None)
        sellable_wh = _safe_float(budget.get("sellable_wh"), None)
        economics = decision.get("economics") if isinstance(decision.get("economics"), dict) else {}
        economic_export_gate = None
        export_gate_lineage: Dict[str, Any] = {}
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
            if not direct_marketing_export_gate_contract_valid(
                decision,
                economics,
                allowed_lineage_statuses={"ACTIVE"},
                current_window_id=window_id,
                current_window_end_ts_ms=execution_end_ms,
            ):
                continue
            export_gate_lineage = copy.deepcopy(
                decision.get("export_window_gate_lineage")
                if isinstance(
                    decision.get("export_window_gate_lineage"), dict
                )
                else {}
            )
            economic_export_gate = {
                "allowed": True,
                "blockers": [],
                "block_reason_code": None,
                **economic_values,
                "policy_commands_allowed": True,
                "policy_export_budget_w": round(action_budget_w, 3),
                "accounting_contract": "DIRECT_MARKETING_POLICY_ECONOMICS_REUSED_NO_DOUBLE_DEDUCTION",
                "export_window_start_gate": copy.deepcopy(
                    decision.get("export_window_start_gate")
                ),
                "export_window_gate_lineage": copy.deepcopy(
                    export_gate_lineage
                ),
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
            and plan_window_id
            and selected_plan_window_id == plan_window_id
            and execution_start_ms <= slot_start_ms
            and slot_end_ms <= execution_end_ms
            and plan_window_start_ms <= execution_start_ms
            and execution_end_ms <= plan_window_end_ms
            and (plan_action == "CHARGE_BLOCK_WAIT" or action_budget_w > 0.0)
            and (
                plan_action != "PV_STORE"
                or selected.get("pv_store_source_contract")
                in {"E3DC_DC", "E3DC_DC_PLUS_AUX_AC_PV"}
            )
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
            if candidate_id != plan_window_id:
                continue
            if (
                _to_ts_ms(window.get("start_ts")) == plan_window_start_ms
                and _to_ts_ms(window.get("end_ts")) == plan_window_end_ms
            ):
                plan_windows.append(window)
        if len(plan_windows) != 1:
            continue
        plan_window = plan_windows[0]
        if bool(
            plan_action == "PV_STORE"
            and plan_window.get("pv_store_source_contract")
            != selected.get("pv_store_source_contract")
        ):
            continue
        power_limits = [action_budget_w]
        for value in (selected.get("max_power_w"), plan_window.get("max_power_w")):
            parsed = _safe_float(value, None)
            if parsed is not None and parsed > 0.0:
                power_limits.append(parsed)
        planned_w = 0.0 if plan_action == "CHARGE_BLOCK_WAIT" else min(power_limits)
        if planned_w <= 0.0 and plan_action != "CHARGE_BLOCK_WAIT":
            continue
        action_identity_material = {
            "action": plan_action,
            "window_id": window_id,
            "window_start_ts_ms": plan_window_start_ms,
            "window_end_ts_ms": plan_window_end_ms,
        }
        if plan_action == "ECONOMIC_EXPORT":
            action_identity_material.update({
                "gate_lineage_id": export_gate_lineage.get(
                    "gate_lineage_id"
                ),
                "gate_generation": export_gate_lineage.get(
                    "current_generation"
                ),
                "gate_generation_id": export_gate_lineage.get(
                    "current_generation_id"
                ),
            })
        action_id = revision_hash(action_identity_material)
        live_dc_fallback_contract = None
        if plan_action == "PV_STORE":
            live_dc_fallback_contract = (
                _direct_marketing_pv_store_live_dc_fallback_projection(
                    selected,
                    plan_window,
                    slot,
                    source_action=selected_action,
                    source_mode=source_mode,
                    action_id=action_id,
                    window_id=window_id,
                    window_start_ts_ms=plan_window_start_ms,
                    window_end_ts_ms=plan_window_end_ms,
                )
            )
        source_action_execution_released = bool(
            direct_marketing_source_action_released(
                target_state,
                selected_action,
            )
            and direct_marketing_source_action_mode_valid(
                target_state,
                selected_action,
                source_mode,
            )
        )
        contract = {
            "schema_version": DIRECT_MARKETING_PLAN_PROJECTION_SCHEMA,
            "selected": True,
            "plan_executable": True,
            "plan_commands_allowed": True,
            "action": plan_action,
            "source_action": selected_action,
            "source_mode": source_mode,
            "source_action_execution_released": (
                True if source_action_execution_released else None
            ),
            "action_id": action_id,
            "action_lineage_id": (
                action_id if source_action_execution_released else None
            ),
            "gate_lineage_id": export_gate_lineage.get("gate_lineage_id"),
            "gate_generation": export_gate_lineage.get(
                "current_generation"
            ),
            "gate_generation_id": export_gate_lineage.get(
                "current_generation_id"
            ),
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
            "pv_store_live_dc_fallback_contract": (
                live_dc_fallback_contract
                if plan_action == "PV_STORE"
                else None
            ),
            "pv_store_source_contract": (
                selected.get("pv_store_source_contract")
                if plan_action == "PV_STORE"
                else None
            ),
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
        # Auswahlidentität bleibt auch bei einer physikalisch auf 0 W
        # begrenzten Aktion erhalten. Insbesondere eine DC-only-PV_STORE-
        # Runtimefreigabe darf nicht als unselected erscheinen, nur weil die
        # Prognose keinen belastbaren SoC-Effekt materialisieren kann.
        if isinstance(contract, dict):
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
                    _safe_float(contract.get("planned_w"), 0.0) or 0.0,
                    _safe_float(projection.get("direct_marketing_candidate_w"), 0.0) or 0.0,
                ),
                "direct_marketing_selected": True,
                "direct_marketing_plan_executable": True,
                "direct_marketing_plan_commands_allowed": True,
                "direct_marketing_requested_w": contract.get("planned_w"),
                # ``planned_w`` bleibt der ausgewählte Policy-/Runtime-Cap.
                # Der getrennte physikalische SoC-Effekt steht ausschließlich
                # in ``direct_marketing_plan_battery_w`` beziehungsweise der
                # neuen Backend-Trajektorie.
                "direct_marketing_planned_w": contract.get("planned_w"),
                "direct_marketing_block_reason": None,
                "direct_marketing_plan_action": plan_action,
                "direct_marketing_plan_source_action": contract.get("source_action"),
                "direct_marketing_plan_source_mode": contract.get("source_mode"),
                "direct_marketing_plan_pv_store_source_contract": contract.get(
                    "pv_store_source_contract"
                ),
                "direct_marketing_plan_source_action_execution_released": contract.get(
                    "source_action_execution_released"
                ),
                "direct_marketing_plan_action_id": contract.get("action_id"),
                "direct_marketing_plan_action_lineage_id": contract.get(
                    "action_lineage_id"
                ),
                "direct_marketing_gate_lineage_id": contract.get(
                    "gate_lineage_id"
                ),
                "direct_marketing_gate_generation": contract.get(
                    "gate_generation"
                ),
                "direct_marketing_gate_generation_id": contract.get(
                    "gate_generation_id"
                ),
                "direct_marketing_plan_segment_id": contract.get("segment_id"),
                "direct_marketing_action_horizon_contract": action_horizon_contract,
                "direct_marketing_economic_export_gate": economic_export_gate,
                "direct_marketing_pv_store_live_dc_fallback_contract": copy.deepcopy(
                    contract.get("pv_store_live_dc_fallback_contract")
                ),
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
        "plan_id": str(canonical.get("plan_id") or source.get("plan_id") or ""),
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


def _direct_marketing_future_pv_store_delegation(
    direct: Dict[str, Any],
    slot_start_ms: int,
    slot_end_ms: int,
) -> Optional[Dict[str, Any]]:
    """Liefert nur eine vollständig typisierte künftige PV_STORE-Freigabe.

    Der Vertrag ist keine Candidate-Imputation. Er stammt aus dem aktiven
    DV-Producer und darf ausschließlich PV-Überschuss bis zu seinem positiven
    Kurvenlade-Cap aufnehmen. Ein bloßer Schutzenergie-/Verkaufsboden ohne
    positiven Cap erzeugt keine Ladung.
    """

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
    valid_until_ms = _safe_int(reservation.get("valid_until_ts"), 0)
    next_start_ms = _safe_int(next_window.get("start_ts"), 0)
    next_end_ms = _safe_int(next_window.get("end_ts"), 0)
    max_curve_charge_w = _safe_float(
        reservation.get("max_curve_charge_w"),
        None,
    )
    max_storage_before_window_wh = _safe_float(
        reservation.get("max_storage_before_window_wh"),
        None,
    )
    reason = str(reservation.get("reason") or "")
    if not bool(
        reservation.get("schema")
        == "direct_marketing_future_pv_store_reservation_v1"
        and reservation.get("active") is True
        and reservation.get("commands_allowed") is True
        and reservation.get("data_quality") == "ok"
        and reason
        in {
            "reserve_recovery",
            "house_need_until_future_window",
            "future_window_energy_insufficient",
        }
        and next_window.get("action") == "eco_plus_store_pv_candidate"
        and min(valid_until_ms, next_start_ms, next_end_ms) >= 10_000_000_000
        and valid_until_ms == next_start_ms
        and slot_start_ms < slot_end_ms <= valid_until_ms
        and next_start_ms < next_end_ms
        and max_curve_charge_w is not None
        and math.isfinite(max_curve_charge_w)
        and max_curve_charge_w >= 300.0
        and max_storage_before_window_wh is not None
        and math.isfinite(max_storage_before_window_wh)
        and max_storage_before_window_wh > 0.0
        and reservation.get("no_grid_charge") is True
        and reservation.get("consumer_budgets_untouched") is True
    ):
        return None
    return {
        "schema_version": "direct_marketing_future_pv_store_delegation_v1",
        "active": True,
        "commands_allowed": True,
        "action": "PV_STORE",
        "reason": reason,
        "max_curve_charge_w": round(max_curve_charge_w, 3),
        "max_storage_before_window_wh": round(
            max_storage_before_window_wh,
            3,
        ),
        "valid_until_ts_ms": valid_until_ms,
        "next_window_start_ts_ms": next_start_ms,
        "next_window_end_ts_ms": next_end_ms,
        "source": "direct_marketing.future_pv_store_reservation",
        "pv_store_source_contract": "E3DC_DC",
        "no_grid_charge": True,
    }


def _direct_marketing_slot_has_active_action_claim(
    direct: Dict[str, Any],
    slot_start_ms: int,
    slot_end_ms: int,
) -> bool:
    """Erkennt einen verworfenen aktiven Claim vor einem passiven Rückfall."""

    decisions = [
        item
        for item in direct.get("policy_timeline") or []
        if isinstance(item, dict)
    ]
    current = (
        direct.get("policy_decision")
        if isinstance(direct.get("policy_decision"), dict)
        else None
    )
    if current is not None:
        decisions.append(current)
    active_targets = {"FORCE_EXPORT", "FORCE_CHARGE_PV", "CHARGE_BLOCK_WAIT"}
    active_source_actions = {
        "eco_plus_export_candidate",
        "eco_plus_store_pv_candidate",
        "direct_marketing_charge_block_wait",
    }
    for decision in decisions:
        start_ms = _to_ts_ms(decision.get("start_ts"))
        end_ms = _to_ts_ms(decision.get("end_ts"))
        if not (start_ms <= slot_start_ms and slot_end_ms <= end_ms):
            continue
        selected = (
            decision.get("selected_window")
            if isinstance(decision.get("selected_window"), dict)
            else {}
        )
        if bool(
            str(decision.get("dv_target_state") or "").upper()
            in active_targets
            or str(decision.get("source_action") or "")
            in active_source_actions
            or str(selected.get("action") or "") in active_source_actions
        ):
            return True
    return False


def _direct_marketing_trajectory_material(
    trajectory: Dict[str, Any],
) -> Dict[str, Any]:
    material = copy.deepcopy(trajectory)
    material.pop("trajectory_revision", None)
    return material


def _sha256_revision_valid(value: Any) -> bool:
    text = str(value or "")
    return bool(
        len(text) == 71
        and text.startswith("sha256:")
        and all(char in "0123456789abcdef" for char in text[7:])
    )


def _optional_numeric_equal(
    left: Any,
    right: Any,
    *,
    tolerance: float = 0.01,
) -> bool:
    left_value = _safe_float(left, None)
    right_value = _safe_float(right, None)
    if left_value is None or right_value is None:
        return left_value is None and right_value is None
    return bool(
        math.isfinite(left_value)
        and math.isfinite(right_value)
        and abs(left_value - right_value) <= max(0.0, tolerance)
    )


def _canonical_trajectory_finite_number(value: Any) -> Optional[float]:
    """Liest eine kanonische JSON-Zahl ohne Typkoerzision."""

    if type(value) is bool:
        return None
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _direct_marketing_pv_axis_evidence_for_slot(
    slot: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Bindet die PV-Achse an Frische oder einen Producer-Nachtbeleg.

    Nacht wird hier ausdrücklich nicht aus Uhrzeit oder fehlenden Modellen
    hergeleitet. Der Contract akzeptiert nur den selbstversiegelten Beleg des
    Forecast-Producers und bindet ihn erneut an den kanonischen Slot, die
    Topologie und sämtliche PV-Komponenten.
    """

    if not isinstance(slot, dict):
        return None
    start_ms = _safe_int(slot.get("start_ts_ms"), 0)
    end_ms = _safe_int(slot.get("end_ts_ms"), 0)
    if end_ms - start_ms != SLOT_DURATION_S * 1000:
        return None
    projection = (
        slot.get("projection")
        if isinstance(slot.get("projection"), dict)
        else {}
    )
    forecast = (
        slot.get("forecast_w")
        if isinstance(slot.get("forecast_w"), dict)
        else {}
    )
    pv_contract = (
        forecast.get("pv")
        if isinstance(forecast.get("pv"), dict)
        else {}
    )
    evidence = (
        forecast.get("evidence")
        if isinstance(forecast.get("evidence"), dict)
        else {}
    )
    projection_pv_w = _safe_float(projection.get("pv_w"), None)
    point_pv_w = _safe_float(pv_contract.get("point"), None)
    if not bool(
        projection_pv_w is not None
        and point_pv_w is not None
        and math.isfinite(projection_pv_w)
        and math.isfinite(point_pv_w)
        and projection_pv_w >= 0.0
        and point_pv_w >= 0.0
        and abs(projection_pv_w - point_pv_w)
        <= max(0.01, max(projection_pv_w, point_pv_w) * 0.000001)
    ):
        return None
    if evidence.get("pv_fresh") is True:
        return {
            "schema_version": DIRECT_MARKETING_PV_AXIS_EVIDENCE_SCHEMA,
            "class": "fresh_forecast",
            "source": str(
                evidence.get("pv_freshness_source") or "explicit_pv_fresh"
            ),
            "producer_evidence_revision": None,
        }

    zero = (
        evidence.get("pv_zero_evidence")
        if isinstance(evidence.get("pv_zero_evidence"), dict)
        else {}
    )
    material = copy.deepcopy(zero)
    evidence_revision = material.pop("evidence_revision", None)
    topology = (
        forecast.get("topology")
        if isinstance(forecast.get("topology"), dict)
        else {}
    )
    e3dc_dc = (
        forecast.get("e3dc_dc_pv")
        if isinstance(forecast.get("e3dc_dc_pv"), dict)
        else {}
    )
    external_ac = (
        forecast.get("external_ac_pv")
        if isinstance(forecast.get("external_ac_pv"), dict)
        else {}
    )
    e3dc_dc_w = _safe_float(e3dc_dc.get("point"), None)
    external_ac_w = _safe_float(external_ac.get("point"), None)
    resources = (
        topology.get("resources")
        if isinstance(topology.get("resources"), list)
        else []
    )
    resource_values = [
        _safe_float(item.get("forecast_w"), None)
        if isinstance(item, dict)
        else None
        for item in resources
    ]
    resource_total_w = (
        sum(value for value in resource_values if value is not None)
        if resource_values and all(value is not None for value in resource_values)
        else None
    )
    midpoint_ms = start_ms + (SLOT_DURATION_S * 1000 // 2)
    if not bool(
        zero.get("schema_version") == PV_ZERO_EVIDENCE_SCHEMA
        and zero.get("status") == "COMPLETE"
        and zero.get("reason") == "ASTRONOMICAL_NIGHT"
        and zero.get("full_slot_night") is True
        and _safe_int(zero.get("slot_start_ts_ms"), 0) == start_ms
        and _safe_int(zero.get("slot_end_ts_ms"), 0) == end_ms
        and _safe_int(zero.get("slot_midpoint_ts_ms"), 0) == midpoint_ms
        and _safe_float(zero.get("daylight_factor_midpoint"), None) == 0.0
        and _safe_int(zero.get("solar_guard_s"), 0) == 5400
        and all(
            _sha256_revision_valid(zero.get(key))
            for key in (
                "method_revision",
                "site_revision",
                "astronomy_revision",
                "topology_revision",
            )
        )
        and _sha256_revision_valid(evidence_revision)
        and evidence_revision == revision_hash(material)
        and zero.get("topology_revision") == topology.get("revision")
        and topology.get("status") == "bound"
        and topology.get("resource_projection_status") == "complete"
        and _safe_float(zero.get("pv_total_w"), None) == 0.0
        and _safe_float(zero.get("e3dc_dc_pv_w"), None) == 0.0
        and _safe_float(zero.get("external_ac_pv_w"), None) == 0.0
        and _safe_int(zero.get("resource_count"), 0) == len(resources)
        and len(resources) > 0
        and _safe_float(zero.get("resource_total_w"), None) == 0.0
        and projection_pv_w == 0.0
        and point_pv_w == 0.0
        and e3dc_dc_w == 0.0
        and external_ac_w == 0.0
        and resource_total_w == 0.0
        and all(
            isinstance(item, dict)
            and item.get("resource_key")
            and str(item.get("coupling") or "")
            in {"E3DC_DC", "EXTERNAL_AC"}
            for item in resources
        )
    ):
        return None
    return {
        "schema_version": DIRECT_MARKETING_PV_AXIS_EVIDENCE_SCHEMA,
        "class": "astronomical_night_zero",
        "source": "forecast_producer",
        "producer_evidence_revision": evidence_revision,
    }


def _materialize_direct_marketing_trajectory(
    source: Dict[str, Any],
    canonical: Dict[str, Any],
    valid_from_ms: int,
) -> Dict[str, Any]:
    """Integriert die kanonische DV-SoC-Folge genau einmal im Backend.

    Kandidaten und Shadow-Slots haben keinerlei Wirkung. Positive
    Ladeleistung entsteht nur aus einem ausgewählten PV_STORE-Vertrag oder
    aus der expliziten, typisierten PV_STORE-Reservierungsdelegation.
    CHARGE_BLOCK_WAIT sperrt Ladung, lässt die passive Hausversorgung aber bis
    zum harten Reserveboden zu.
    """

    direct = (
        canonical.get("direct_marketing")
        if isinstance(canonical.get("direct_marketing"), dict)
        else {}
    )
    revisions = (
        canonical.get("input_revisions")
        if isinstance(canonical.get("input_revisions"), dict)
        else {}
    )
    base = {
        "schema_version": DIRECT_MARKETING_TRAJECTORY_SCHEMA,
        "active": False,
        "complete": True,
        "status": "DIRECT_MARKETING_DISABLED",
        "plan_id": None,
        "trajectory_revision": None,
        "generated_at_ts_ms": canonical.get("generated_at_ts_ms"),
        "valid_from_ts_ms": valid_from_ms,
        "horizon_end_ts_ms": canonical.get("horizon_end_ts_ms"),
        "slot_duration_s": SLOT_DURATION_S,
        "input_revisions": copy.deepcopy(revisions),
        "meta": {
            "soc_integrator": "storage_dispatch_contract_backend",
            "runtime_authorization_separate": True,
            "candidate_effect": False,
            "shadow_effect": False,
        },
        "slots": [],
    }
    if not _direct_marketing_enabled(direct):
        return base
    if not _direct_marketing_execution_enabled(direct):
        base.update({
            "active": True,
            "complete": False,
            "status": "DIRECT_MARKETING_POLICY_NOT_EXECUTION_READY",
        })
        return base

    slots = [
        slot
        for slot in canonical.get("slots") or []
        if isinstance(slot, dict)
        and _safe_int(slot.get("start_ts_ms"), 0) >= valid_from_ms
    ]
    capacity_wh = _battery_capacity_wh(source)
    first_soc_contract = (
        slots[0].get("soc_pct")
        if slots and isinstance(slots[0].get("soc_pct"), dict)
        else {}
    )
    planned_soc = _safe_float(source.get("current_soc"), None)
    initial_soc_source = "plan_current_soc"
    if planned_soc is None:
        planned_soc = _safe_float(first_soc_contract.get("start"), None)
        initial_soc_source = "canonical_first_slot_soc_start"
    shadow_cfg = (
        source.get("shadow_dispatch_config")
        if isinstance(source.get("shadow_dispatch_config"), dict)
        else {}
    )
    economics = (
        direct.get("economics")
        if isinstance(direct.get("economics"), dict)
        else {}
    )
    roundtrip_pct = _safe_float(
        shadow_cfg.get(
            "roundtrip_efficiency_pct",
            economics.get("roundtrip_efficiency_pct"),
        ),
        85.0,
    ) or 85.0
    roundtrip = max(0.5, min(0.99, roundtrip_pct / 100.0))
    charge_efficiency = math.sqrt(roundtrip)
    discharge_efficiency = math.sqrt(roundtrip)
    max_charge_raw = _safe_float(
        canonical.get("max_charge_w", source.get("max_charge_w")),
        None,
    )
    max_discharge_raw = _safe_float(
        canonical.get(
            "max_discharge_w",
            source.get("max_discharge_w"),
        ),
        None,
    )
    max_charge_w = (
        max_charge_raw
        if max_charge_raw is not None
        and math.isfinite(max_charge_raw)
        and max_charge_raw > 0.0
        else 0.0
    )
    max_discharge_w = (
        max_discharge_raw
        if max_discharge_raw is not None
        and math.isfinite(max_discharge_raw)
        and max_discharge_raw > 0.0
        else 0.0
    )
    base.update({
        "active": True,
        "complete": False,
        "status": "TRAJECTORY_INPUT_INCOMPLETE",
        "meta": {
            **base["meta"],
            "capacity_wh": round(capacity_wh, 3) if capacity_wh > 0.0 else None,
            "capacity_source": (
                "battery_capacity_or_bat_cap_kwh"
                if capacity_wh > 0.0
                else None
            ),
            "initial_soc_source": initial_soc_source,
            "efficiencies": {
                "roundtrip_pct": round(roundtrip * 100.0, 3),
                "charge": round(charge_efficiency, 6),
                "discharge": round(discharge_efficiency, 6),
            },
            "hardware_limits_w": {
                "charge": round(max_charge_w, 3),
                "discharge": round(max_discharge_w, 3),
            },
            "signs": {
                "battery_w": "positive_charge_negative_discharge",
                "grid_w": "positive_import_negative_export",
                "residual_w": "positive_pv_surplus_negative_load_deficit",
            },
            "balance_contract": (
                "grid_w=loads_total_w+battery_w-pv_total_w;"
                "residual_after_storage_w=-grid_w"
            ),
            "load_aggregation_contract": (
                "house_plus_heat_plus_wallbox_exactly_once;"
                "wp_and_climate_are_diagnostics_within_heat;"
                "planned_load_is_already_within_wallbox"
            ),
            "pv_aggregation_contract": (
                "canonical_projection_pv_total_no_external_ac_readdition"
            ),
        },
    })
    if not bool(
        slots
        and capacity_wh > 0.0
        and planned_soc is not None
        and math.isfinite(planned_soc)
        and 0.0 <= planned_soc <= 100.0
    ):
        return base
    if max_charge_w <= 0.0 or max_discharge_w <= 0.0:
        base.update({
            "complete": False,
            "status": "HARDWARE_LIMITS_INCOMPLETE",
            "slots": [],
        })
        return base

    slot_hours = SLOT_DURATION_S / 3600.0
    trajectory_slots: List[Dict[str, Any]] = []
    bounded_slots = 0
    evidence_limit_slots = 0
    for slot in slots:
        start_ms = _safe_int(slot.get("start_ts_ms"), 0)
        end_ms = _safe_int(slot.get("end_ts_ms"), 0)
        projection = (
            slot.get("projection")
            if isinstance(slot.get("projection"), dict)
            else {}
        )
        soc_contract = (
            slot.get("soc_pct")
            if isinstance(slot.get("soc_pct"), dict)
            else {}
        )
        forecast = (
            slot.get("forecast_w")
            if isinstance(slot.get("forecast_w"), dict)
            else {}
        )
        forecast_evidence = (
            forecast.get("evidence")
            if isinstance(forecast.get("evidence"), dict)
            else {}
        )
        pv_axis_evidence = _direct_marketing_pv_axis_evidence_for_slot(slot)
        axis_values = {
            key: _safe_float(projection.get(key), None)
            for key in ("pv_w", "home_w", "heat_w", "wallbox_w")
        }
        soc_start_axis = _safe_float(soc_contract.get("start"), None)
        soc_end_axis = _safe_float(soc_contract.get("end"), None)
        notstrom_floor_axis = _safe_float(
            soc_contract.get("notstrom_floor"),
            None,
        )
        ceiling_axis = _safe_float(soc_contract.get("ceiling"), None)
        reserve_floor_hard = bool(
            str(soc_contract.get("reserve_floor_hardness") or "") == "hard"
        )
        reserve_floor_axis = _safe_float(
            soc_contract.get("reserve_floor"),
            None,
        )
        if not bool(
            end_ms - start_ms == SLOT_DURATION_S * 1000
            and all(
                value is not None
                and math.isfinite(value)
                and value >= 0.0
                for value in axis_values.values()
            )
            and soc_start_axis is not None
            and math.isfinite(soc_start_axis)
            and 0.0 <= soc_start_axis <= 100.0
            and soc_end_axis is not None
            and math.isfinite(soc_end_axis)
            and 0.0 <= soc_end_axis <= 100.0
            and notstrom_floor_axis is not None
            and math.isfinite(notstrom_floor_axis)
            and 0.0 <= notstrom_floor_axis <= 100.0
            and ceiling_axis is not None
            and math.isfinite(ceiling_axis)
            and notstrom_floor_axis <= ceiling_axis <= 100.0
            and (
                not reserve_floor_hard
                or (
                    reserve_floor_axis is not None
                    and math.isfinite(reserve_floor_axis)
                    and 0.0 <= reserve_floor_axis <= ceiling_axis
                )
            )
            and pv_axis_evidence is not None
            and forecast_evidence.get("load_valid") is True
        ):
            base.update({
                "complete": False,
                "status": "TRAJECTORY_AXIS_EVIDENCE_LIMIT",
                "slots": [],
            })
            return base
        pv_total_w = axis_values["pv_w"] or 0.0
        home_w = axis_values["home_w"] or 0.0
        heat_w = axis_values["heat_w"] or 0.0
        wallbox_w = axis_values["wallbox_w"] or 0.0
        loads_total_w = home_w + heat_w + wallbox_w
        residual_before_w = pv_total_w - loads_total_w
        e3dc_dc = (
            forecast.get("e3dc_dc_pv")
            if isinstance(forecast.get("e3dc_dc_pv"), dict)
            else {}
        )
        external_ac = (
            forecast.get("external_ac_pv")
            if isinstance(forecast.get("external_ac_pv"), dict)
            else {}
        )
        hard_floor_values = [_safe_float(soc_contract.get("notstrom_floor"), None)]
        if str(soc_contract.get("reserve_floor_hardness") or "") == "hard":
            hard_floor_values.append(
                _safe_float(soc_contract.get("reserve_floor"), None)
            )
        contract = _direct_marketing_policy_projection_for_slot(direct, slot)
        if contract is None and _direct_marketing_slot_has_active_action_claim(
            direct,
            start_ms,
            end_ms,
        ):
            base.update({
                "complete": False,
                "status": "ACTIVE_ACTION_CLAIM_INVALID",
                "slots": [],
            })
            return base
        if isinstance(contract, dict):
            protected_reserve_wh = _safe_float(
                contract.get("protected_reserve_wh"),
                None,
            )
            if protected_reserve_wh is not None:
                hard_floor_values.append(
                    max(0.0, protected_reserve_wh) / capacity_wh * 100.0
                )
        hard_floor_pct = max(
            (value for value in hard_floor_values if value is not None),
            default=0.0,
        )
        hard_floor_pct = max(0.0, min(100.0, hard_floor_pct))
        ceiling_pct = max(
            hard_floor_pct,
            min(
                100.0,
                _safe_float(soc_contract.get("ceiling"), 100.0) or 100.0,
            ),
        )
        # Der Reserveboden ist ausschließlich eine Entladegrenze. Ein bereits
        # darunter liegender Ist-/Start-SoC darf nicht rechnerisch auf den
        # Boden angehoben werden.
        energy_start_wh = max(
            0.0,
            min(capacity_wh, planned_soc / 100.0 * capacity_wh),
        )
        floor_wh = hard_floor_pct / 100.0 * capacity_wh
        ceiling_wh = ceiling_pct / 100.0 * capacity_wh
        action = "PASSIVE_NORMAL"
        reason_code = "DIRECT_MARKETING_PASSIVE_HOUSE_SUPPLY"
        requested_w = 0.0
        battery_w = 0.0
        delegation = None
        passive_binding = None
        slot_projection_status = "complete"
        selected = isinstance(contract, dict)
        if selected:
            action = str(contract.get("action") or "").upper()
            requested_w = max(
                0.0,
                _safe_float(contract.get("planned_w"), 0.0) or 0.0,
            )
        if selected and action == "PV_STORE":
            reason_code = "DIRECT_MARKETING_SELECTED_PV_STORE"
            room_wh = max(0.0, ceiling_wh - energy_start_wh)
            pv_charge_available_w = max(0.0, residual_before_w)
            pv_store_source_contract = str(
                contract.get("pv_store_source_contract") or ""
            )
            if pv_store_source_contract == "E3DC_DC":
                e3dc_dc_w = _safe_float(e3dc_dc.get("point"), None)
                if e3dc_dc_w is None or not math.isfinite(e3dc_dc_w):
                    # Der v2-Livevertrag darf den E3/DC zur Laufzeit freigeben,
                    # er behauptet aber ausdrücklich keinen Prognose-SoC-Effekt.
                    # Daher bleibt die Trajektorie sichtbar konservativ bei 0 W
                    # und als unvollständig markiert.
                    live_dc = (
                        contract.get("pv_store_live_dc_fallback_contract")
                        if isinstance(
                            contract.get(
                                "pv_store_live_dc_fallback_contract"
                            ),
                            dict,
                        )
                        else {}
                    )
                    battery_w = 0.0
                    slot_projection_status = (
                        "evidence_limit_runtime_dc_permission"
                        if bool(
                            live_dc.get("valid") is True
                            and live_dc.get("dc_only") is True
                            and live_dc.get("runtime_measurement_required")
                            is False
                            and live_dc.get("soc_effect") is False
                        )
                        else "evidence_limit_dc_forecast_missing"
                    )
                else:
                    pv_charge_available_w = min(
                        pv_charge_available_w,
                        max(0.0, e3dc_dc_w),
                    )
                    battery_w = min(
                        requested_w,
                        max_charge_w if max_charge_w > 0.0 else requested_w,
                        pv_charge_available_w,
                        room_wh / charge_efficiency / slot_hours,
                    )
            else:
                battery_w = min(
                    requested_w,
                    max_charge_w if max_charge_w > 0.0 else requested_w,
                    pv_charge_available_w,
                    room_wh / charge_efficiency / slot_hours,
                )
        elif selected and action == "ECONOMIC_EXPORT":
            reason_code = "DIRECT_MARKETING_SELECTED_ECONOMIC_EXPORT"
            available_wh = max(0.0, energy_start_wh - floor_wh)
            discharge_w = min(
                requested_w,
                max_discharge_w if max_discharge_w > 0.0 else requested_w,
                available_wh * discharge_efficiency / slot_hours,
            )
            battery_w = -discharge_w
        else:
            if selected and action == "CHARGE_BLOCK_WAIT":
                reason_code = "DIRECT_MARKETING_CHARGE_BLOCK_WAIT"
            else:
                delegation = _direct_marketing_future_pv_store_delegation(
                    direct,
                    start_ms,
                    end_ms,
                )
                if delegation is None:
                    passive_binding = (
                        _direct_marketing_passive_normal_binding_for_slot(
                            direct,
                            slot,
                        )
                    )
                    if passive_binding is None:
                        base.update({
                            "complete": False,
                            "status": "PASSIVE_POLICY_BINDING_MISSING",
                            "slots": [],
                        })
                        return base
            if delegation is not None:
                action = "PV_STORE"
                reason_code = str(delegation.get("reason") or "")
                delegated_ceiling_wh = min(
                    ceiling_wh,
                    _safe_float(
                        delegation.get("max_storage_before_window_wh"),
                        ceiling_wh,
                    )
                    or ceiling_wh,
                )
                room_wh = max(0.0, delegated_ceiling_wh - energy_start_wh)
                requested_w = max(
                    0.0,
                    _safe_float(delegation.get("max_curve_charge_w"), 0.0)
                    or 0.0,
                )
                e3dc_dc_w = _safe_float(e3dc_dc.get("point"), None)
                if e3dc_dc_w is None or not math.isfinite(e3dc_dc_w):
                    battery_w = 0.0
                    slot_projection_status = (
                        "evidence_limit_dc_forecast_missing"
                    )
                else:
                    battery_w = min(
                        requested_w,
                        max_charge_w if max_charge_w > 0.0 else requested_w,
                        max(0.0, residual_before_w),
                        max(0.0, e3dc_dc_w),
                        room_wh / charge_efficiency / slot_hours,
                    )
            elif passive_binding is not None:
                reason_code = "DIRECT_MARKETING_PASSIVE_NORMAL_BINDING"
                baseline_battery_w = (
                    _safe_float(projection.get("battery_w"), 0.0) or 0.0
                )
                if baseline_battery_w >= 0.0:
                    # PASSIVE_NORMAL delegiert ausschließlich die normale
                    # Hausversorgung. Positive Kurven-/Reserveladung braucht
                    # immer selected PV_STORE oder die typisierte künftige
                    # PV_STORE-Reservierungsfreigabe.
                    deficit_w = max(0.0, -residual_before_w)
                    available_wh = max(0.0, energy_start_wh - floor_wh)
                    requested_w = deficit_w
                    discharge_w = min(
                        deficit_w,
                        max_discharge_w,
                        available_wh * discharge_efficiency / slot_hours,
                    )
                    battery_w = -discharge_w
                else:
                    requested_w = abs(baseline_battery_w)
                    deficit_w = max(0.0, -residual_before_w)
                    available_wh = max(0.0, energy_start_wh - floor_wh)
                    discharge_w = min(
                        requested_w,
                        deficit_w,
                        (
                            max_discharge_w
                            if max_discharge_w > 0.0
                            else requested_w
                        ),
                        available_wh * discharge_efficiency / slot_hours,
                    )
                    battery_w = -discharge_w
            else:
                # CHARGE_BLOCK_WAIT deckt nur den Hausbedarf und exportiert
                # keine zusätzliche Batterieenergie.
                deficit_w = max(0.0, -residual_before_w)
                available_wh = max(0.0, energy_start_wh - floor_wh)
                discharge_w = min(
                    deficit_w,
                    max_discharge_w if max_discharge_w > 0.0 else deficit_w,
                    available_wh * discharge_efficiency / slot_hours,
                )
                battery_w = -discharge_w
        if battery_w >= 0.0:
            energy_end_wh = (
                energy_start_wh
                + battery_w * slot_hours * charge_efficiency
            )
        else:
            energy_end_wh = (
                energy_start_wh
                - abs(battery_w) * slot_hours / discharge_efficiency
            )
        # Ebenso sind Boden und Soll-Decke keine Energiequelle/-senke. Die
        # vorherigen Leistungsgrenzen verhindern Be- und Entladung über ihre
        # Schranken; der Integrator klemmt nur an die physische Kapazität.
        energy_end_wh = max(0.0, min(capacity_wh, energy_end_wh))
        soc_end_pct = energy_end_wh / capacity_wh * 100.0
        grid_w = loads_total_w + battery_w - pv_total_w
        residual_after_w = pv_total_w - loads_total_w - battery_w
        if abs(battery_w) + 0.001 < requested_w:
            bounded_slots += 1
        if slot_projection_status != "complete":
            evidence_limit_slots += 1
        trajectory_slots.append({
            "slot_id": None,
            "start_ts_ms": start_ms,
            "end_ts_ms": end_ms,
            "soc_start_pct": round(planned_soc, 3),
            "soc_end_pct": round(soc_end_pct, 3),
            "hard_reserve_soc_pct": round(hard_floor_pct, 3),
            "ceiling_soc_pct": round(ceiling_pct, 3),
            "battery_w": round(battery_w, 3),
            "grid_w": round(grid_w, 3),
            "pv_w": {
                "total": round(pv_total_w, 3),
                "e3dc_dc": _round_or_none(e3dc_dc.get("point"), 3),
                "external_ac": _round_or_none(external_ac.get("point"), 3),
            },
            "pv_axis_evidence": copy.deepcopy(pv_axis_evidence),
            "loads_w": {
                "house": round(home_w, 3),
                "heat": round(heat_w, 3),
                "wp": _round_or_none(projection.get("wp_w"), 3),
                "climate": _round_or_none(projection.get("climate_w"), 3),
                "wallbox": round(wallbox_w, 3),
                "total": round(loads_total_w, 3),
            },
            "residual_before_storage_w": round(residual_before_w, 3),
            "residual_after_storage_w": round(residual_after_w, 3),
            "action": action,
            "projection_status": slot_projection_status,
            "selection": {
                "selected": selected,
                "executable": bool(
                    selected and contract.get("plan_executable") is True
                ),
                "commands_allowed": bool(
                    selected
                    and contract.get("plan_commands_allowed") is True
                ),
                "requested_w": round(requested_w, 3),
                "action_id": contract.get("action_id") if selected else None,
                "window_id": contract.get("window_id") if selected else None,
                "segment_id": contract.get("segment_id") if selected else None,
                "source_action": (
                    contract.get("source_action") if selected else None
                ),
                "source_mode": (
                    contract.get("source_mode") if selected else None
                ),
                "pv_store_source_contract": (
                    contract.get("pv_store_source_contract")
                    if selected
                    else None
                ),
            },
            "delegation": copy.deepcopy(delegation),
            "passive_binding": copy.deepcopy(passive_binding),
            "reason_code": reason_code,
            "provenance": {
                "balance_source": "canonical_slot_projection",
                "action_source": (
                    "canonical_direct_marketing_selected_policy"
                    if selected
                    else (
                        "direct_marketing.future_pv_store_reservation"
                        if delegation is not None
                        else "canonical_passive_house_supply"
                    )
                ),
                "candidate_effect": False,
                "shadow_effect": False,
                "pv_axis_evidence_class": pv_axis_evidence.get("class"),
            },
        })
        planned_soc = soc_end_pct

    base.update({
        "complete": bool(
            len(trajectory_slots) == len(slots)
            and evidence_limit_slots == 0
        ),
        "status": (
            "EVIDENCE_LIMIT_DC_SOURCE"
            if evidence_limit_slots > 0
            else (
                "COMPLETE_BOUNDED"
                if bounded_slots > 0
                else "COMPLETE"
            )
        ),
        "bounded_slot_count": bounded_slots,
        "evidence_limit_slot_count": evidence_limit_slots,
        "slots": trajectory_slots,
    })
    return base


def _direct_marketing_passive_normal_binding_for_slot(
    direct: Dict[str, Any],
    slot: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Projiziert genau einen versiegelten passiven NORMAL-Policyabschnitt."""

    flags = (
        direct.get("flags")
        if isinstance(direct.get("flags"), dict)
        else {}
    )
    if not bool(
        direct.get("active") is True
        and direct.get("shadow") is False
        and _normalized_direct_marketing_mode(direct.get("mode")) == "eco_plus"
        and direct.get("controller_owner") == "storage_manager"
        and direct.get("plan_owner") == "direct_marketing:eco_plus"
        and type(direct.get("owner_contract_version")) is int
        and direct.get("owner_contract_version") == 1
        and flags.get("commands_allowed") is True
    ):
        return None
    slot_start = _safe_int(slot.get("start_ts_ms"), 0)
    slot_end = _safe_int(slot.get("end_ts_ms"), 0)
    if min(slot_start, slot_end) < 10_000_000_000 or slot_end <= slot_start:
        return None

    covering = []
    for item in (
        direct.get("policy_timeline")
        if isinstance(direct.get("policy_timeline"), list)
        else []
    ):
        if not isinstance(item, dict):
            continue
        item_start = _safe_int(item.get("start_ts"), 0)
        item_end = _safe_int(item.get("end_ts"), 0)
        if item_start < slot_end and item_end > slot_start:
            covering.append(item)
    if len(covering) != 1:
        return None

    policy = covering[0]
    selected = (
        policy.get("selected_window")
        if isinstance(policy.get("selected_window"), dict)
        else {}
    )
    binding = (
        policy.get("passive_normal_binding")
        if isinstance(policy.get("passive_normal_binding"), dict)
        else {}
    )
    policy_start = _safe_int(policy.get("start_ts"), 0)
    policy_end = _safe_int(policy.get("end_ts"), 0)
    selected_start = _safe_int(selected.get("start_ts"), 0)
    selected_end = _safe_int(selected.get("end_ts"), 0)
    action_id = str(policy.get("policy_action_id") or "")
    policy_slot_id = str(policy.get("policy_slot_id") or "")
    selected_window_id = str(selected.get("window_id") or "")
    expected_binding = passive_normal_identity(
        start_ts=policy_start,
        end_ts=policy_end,
        selected_start_ts=selected_start,
        selected_end_ts=selected_end,
        window_id=selected_window_id,
    )
    if not bool(
        policy.get("schema") == "direct_marketing_policy_v1"
        and policy.get("blocked") is False
        and policy.get("commands_allowed") is False
        and str(policy.get("dv_target_state") or "").strip().upper() == "NORMAL"
        and str(policy.get("source_action") or "") == "eco_plus_house_supply"
        and policy.get("executable_action") is None
        and str(selected.get("action") or "") == "eco_plus_house_supply"
        and action_id.startswith("sha256:")
        and len(action_id) == 71
        and policy_slot_id.startswith("sha256:")
        and len(policy_slot_id) == 71
        and policy_start <= slot_start < slot_end <= policy_end
        and selected_start <= slot_start < slot_end <= selected_end
        and binding == expected_binding
        and action_id == expected_binding["policy_action_id"]
        and policy_slot_id == expected_binding["policy_slot_id"]
    ):
        return None
    return copy.deepcopy(binding)


def _materialize_direct_marketing_passive_normal_bindings(
    canonical: Dict[str, Any],
) -> None:
    """Bindet passive NORMAL-Semantik vor der kanonischen Planhashbildung."""

    direct = (
        canonical.get("direct_marketing")
        if isinstance(canonical.get("direct_marketing"), dict)
        else {}
    )
    for slot in (
        canonical.get("slots")
        if isinstance(canonical.get("slots"), list)
        else []
    ):
        if not isinstance(slot, dict):
            continue
        projection = (
            slot.get("projection")
            if isinstance(slot.get("projection"), dict)
            else {}
        )
        binding = _direct_marketing_passive_normal_binding_for_slot(
            direct,
            slot,
        )
        if binding is None:
            projection.pop(
                DIRECT_MARKETING_PASSIVE_NORMAL_BINDING_SCHEMA,
                None,
            )
        else:
            projection[
                DIRECT_MARKETING_PASSIVE_NORMAL_BINDING_SCHEMA
            ] = binding
        slot["projection"] = projection


def _optional_direct_marketing_action(value: Any) -> Optional[str]:
    action = str(value or "").strip().upper()
    return action or None


def _materialize_direct_marketing_action_roles(
    canonical: Dict[str, Any],
) -> None:
    """Bindet Kandidat, Planauswahl und Ausführungsrolle je Slot eindeutig."""

    for slot in (
        canonical.get("slots")
        if isinstance(canonical.get("slots"), list)
        else []
    ):
        if not isinstance(slot, dict):
            continue
        projection = (
            slot.get("projection")
            if isinstance(slot.get("projection"), dict)
            else {}
        )
        candidate_flag = projection.get("direct_marketing_candidate") is True
        selected_flag = projection.get("direct_marketing_selected") is True
        executable_flag = (
            projection.get("direct_marketing_plan_executable") is True
        )
        commands_allowed = (
            projection.get("direct_marketing_plan_commands_allowed") is True
        )
        candidate_action = _optional_direct_marketing_action(
            projection.get("direct_marketing_candidate_action")
        )
        plan_action = _optional_direct_marketing_action(
            projection.get("direct_marketing_plan_action")
        )
        selected_action = plan_action if selected_flag else None
        executable_action = (
            plan_action
            if selected_flag and executable_flag and commands_allowed
            else None
        )
        candidate_only = bool(
            candidate_action is not None and executable_action is None
        )
        known_actions = set(ACTIVE_ACTIONS)
        consistent = bool(
            candidate_flag is (candidate_action is not None)
            and selected_flag is (selected_action is not None)
            and executable_flag is commands_allowed
            and executable_flag is (executable_action is not None)
            and (candidate_action is None or candidate_action in known_actions)
            and (plan_action is None or plan_action in known_actions)
            and (
                selected_action is None
                or candidate_action == selected_action
            )
            and executable_action == selected_action
            and projection.get("direct_marketing_effective_action") is None
        )
        roles = {
            "schema_version": DIRECT_MARKETING_ACTION_ROLES_SCHEMA,
            "status": "CONSISTENT" if consistent else "EVIDENCE_LIMIT",
            "candidate_action": candidate_action,
            "candidate_only": candidate_only,
            "plan_selected_action": selected_action,
            "plan_executable_action": executable_action,
            "effective_action": None,
            "runtime_effect_claim_allowed": False,
            "slot_start_ts_ms": _safe_int(slot.get("start_ts_ms"), 0),
            "slot_end_ts_ms": _safe_int(slot.get("end_ts_ms"), 0),
        }
        projection.update({
            "direct_marketing_candidate_only": candidate_only,
            "direct_marketing_plan_selected_action": selected_action,
            "direct_marketing_plan_executable_action": executable_action,
            "direct_marketing_effective_action": None,
            "direct_marketing_action_roles": roles,
        })
        slot["projection"] = projection


def _materialize_shadow_execution_readiness(
    canonical: Dict[str, Any],
    generation_slot: Dict[str, Any],
) -> None:
    """Versiegelt die aktuelle Action-/Revision-/Slotrolle fail-closed."""

    shadow = (
        canonical.get("shadow_dispatch")
        if isinstance(canonical.get("shadow_dispatch"), dict)
        else {}
    )
    projection = (
        generation_slot.get("projection")
        if isinstance(generation_slot.get("projection"), dict)
        else {}
    )
    roles = (
        projection.get("direct_marketing_action_roles")
        if isinstance(
            projection.get("direct_marketing_action_roles"), dict
        )
        else {}
    )
    binding = (
        shadow.get("input_binding_contract")
        if isinstance(shadow.get("input_binding_contract"), dict)
        else {}
    )
    revisions = (
        canonical.get("input_revisions")
        if isinstance(canonical.get("input_revisions"), dict)
        else {}
    )
    source_revisions = {
        key: revisions.get(key) for key in SHADOW_EXECUTION_REVISION_KEYS
    }
    optimizer_status = str(shadow.get("optimizer_status") or "")
    candidate_action = _optional_direct_marketing_action(
        roles.get("candidate_action")
    )
    selected_action = _optional_direct_marketing_action(
        roles.get("plan_selected_action")
    )
    executable_action = _optional_direct_marketing_action(
        roles.get("plan_executable_action")
    )
    blockers: List[str] = []

    def block(condition: bool, code: str) -> None:
        if condition and code not in blockers:
            blockers.append(code)

    block(
        roles.get("status") != "CONSISTENT",
        "DIRECT_MARKETING_ACTION_ROLES_INCONSISTENT",
    )
    block(
        optimizer_status not in {"SHADOW_OK", "SHADOW_HEADROOM_PARTIAL"},
        "SHADOW_OPTIMIZER_STATUS_NOT_EXECUTION_READY",
    )
    block(
        not all(
            isinstance(source_revisions.get(key), str)
            and source_revisions[key].startswith("sha256:")
            and len(source_revisions[key]) == 71
            for key in SHADOW_EXECUTION_REVISION_KEYS
        ),
        "SHADOW_EXECUTION_SOURCE_REVISIONS_INCOMPLETE",
    )
    block(
        binding.get("schema_version") != SHADOW_INPUT_BINDING_SCHEMA
        or binding.get("applicable") is not True
        or binding.get("source_revisions") != revisions
        or binding.get("field_activation_input_complete") is not True,
        "SHADOW_INPUT_BINDING_NOT_EXECUTION_READY",
    )

    canonical_action = bool(
        candidate_action == selected_action == executable_action
        and executable_action in DIRECT_MARKETING_EXECUTION_READY_ACTIONS
    )
    no_action = bool(
        candidate_action is None
        and selected_action is None
        and executable_action is None
    )
    if not canonical_action and not no_action:
        block(
            candidate_action is not None and executable_action is None,
            "CANONICAL_DIRECT_MARKETING_SLOT_NOT_SELECTED",
        )
        block(
            not blockers,
            "DIRECT_MARKETING_ACTION_NOT_EXECUTION_READY",
        )

    if blockers:
        status = "EVIDENCE_LIMIT"
        execution_ready = False
        execution_class = (
            "CANONICAL_ACTION"
            if canonical_action
            else "NO_ACTION"
            if no_action
            else "EVIDENCE_LIMIT"
        )
    elif canonical_action:
        status = "READY"
        execution_ready = True
        execution_class = "CANONICAL_ACTION"
    else:
        status = "READY_NO_ACTION"
        execution_ready = True
        execution_class = "NO_ACTION"

    readiness = {
        "schema_version": SHADOW_EXECUTION_READINESS_SCHEMA,
        "status": status,
        "execution_ready": execution_ready,
        "execution_class": execution_class,
        "blockers": blockers,
        "optimizer_status": optimizer_status or None,
        "current_slot_start_ts_ms": _safe_int(
            generation_slot.get("start_ts_ms"), 0
        ),
        "current_slot_end_ts_ms": _safe_int(
            generation_slot.get("end_ts_ms"), 0
        ),
        "source_revisions": source_revisions,
        "input_binding_revision": revision_hash(binding),
        "action_roles_revision": revision_hash(roles),
        "candidate_action": candidate_action,
        "plan_selected_action": selected_action,
        "plan_executable_action": executable_action,
    }
    shadow.update({
        "execution_readiness_contract": readiness,
        "execution_readiness_status": status,
        "execution_ready": execution_ready,
        "execution_readiness_blockers": copy.deepcopy(blockers),
    })
    canonical["shadow_dispatch"] = shadow


def build_canonical_dispatch_plan(
    legacy_plan: Dict[str, Any],
    *,
    capture_dv_shadow_history: bool = False,
) -> Dict[str, Any]:
    """Ergänzt einen Legacyplan deterministisch um den v1-Vertrag.

    ``capture_dv_shadow_history`` reicht den vollständigen, rein
    diagnostischen DV-Shadow ausschließlich flüchtig an den aufrufenden
    Simulator weiter. Der normale Vertrag bleibt kompakt und frei von dieser
    internen Übergabe.
    """

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
    input_revisions = _execution_binding_input_revisions(
        _input_revisions(source, timeline),
        generated_at_ts_ms=generated_ms,
        valid_from_ts_ms=valid_from_ms,
        valid_until_ts_ms=valid_until_ms,
        horizon_end_ts_ms=horizon_end_ms,
    )
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
        "input_revisions": input_revisions,
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
    dv_shadow_full: Optional[Dict[str, Any]] = None
    try:
        try:
            from .direct_marketing_dispatch_planner import (
                build_direct_marketing_dispatch_shadow,
                shadow_not_applicable as dv_shadow_not_applicable,
                shadow_error as dv_shadow_error,
                summarize_direct_marketing_dispatch_shadow,
            )
        except ImportError:
            from direct_marketing_dispatch_planner import (  # type: ignore
                build_direct_marketing_dispatch_shadow,
                shadow_not_applicable as dv_shadow_not_applicable,
                shadow_error as dv_shadow_error,
                summarize_direct_marketing_dispatch_shadow,
            )
        try:
            if _direct_marketing_not_applicable(direct):
                dv_shadow = dv_shadow_not_applicable()
            else:
                dv_shadow_full = build_direct_marketing_dispatch_shadow(
                    source,
                    canonical,
                )
                dv_shadow = summarize_direct_marketing_dispatch_shadow(
                    dv_shadow_full,
                    generated_ms,
                )
        except Exception:
            dv_shadow = dv_shadow_error("DV_SHADOW_INTERNAL_ERROR")
    except Exception:
        # Der neue Vertrag ist in dieser Phase rein diagnostisch. Selbst ein
        # Importfehler darf den bestehenden Produktionsplan nicht verändern.
        dv_shadow = {
            "schema_version": "direct_marketing_dispatch_shadow_v1",
            "algorithm": "explicit_dv_action_adapter_v1",
            "shadow_only": True,
            "commands_allowed": False,
            "runtime_owner": "storage_manager",
            "status": "SHADOW_ERROR",
            "reason_code": "DV_SHADOW_IMPORT_ERROR",
            "representation": "COMPACT_SUMMARY",
            "full_payload_persisted": False,
        }
    # Der Namespace wird weder von Phase 5 noch vom Storage Manager gelesen.
    # Seine drei eigenen Revisionen sind hashgebunden; die produktive
    # plan_id/slot_id-Identität bleibt davon ausdrücklich unabhängig.
    canonical["planner"]["dv_shadow_v1"] = dv_shadow
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
                shadow = shadow_fallback(
                    str(exc),
                    diagnostic=getattr(exc, "diagnostic", None),
                )
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
    shadow["optimizer_status"] = str(shadow.get("status") or "") or None
    shadow["input_binding_contract"] = _shadow_input_binding_contract(source, canonical, shadow)
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
    canonical["direct_marketing_trajectory"] = _materialize_direct_marketing_trajectory(
        source,
        canonical,
        valid_from_ms,
    )
    _materialize_direct_marketing_passive_normal_bindings(canonical)
    _materialize_direct_marketing_action_roles(canonical)
    _materialize_shadow_execution_readiness(canonical, generation_slot)
    canonical["shadow_dispatch"]["shadow_plan_id"] = _shadow_plan_id(
        canonical["shadow_dispatch"]
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
    trajectory = (
        canonical.get("direct_marketing_trajectory")
        if isinstance(canonical.get("direct_marketing_trajectory"), dict)
        else None
    )
    if trajectory is not None:
        trajectory["plan_id"] = plan_id
        for trajectory_slot in trajectory.get("slots") or []:
            if isinstance(trajectory_slot, dict):
                trajectory_slot["slot_id"] = slot_ids_by_start.get(
                    _safe_int(trajectory_slot.get("start_ts_ms"), 0)
                )
        trajectory["trajectory_revision"] = revision_hash(
            _direct_marketing_trajectory_material(trajectory)
        )
    canonical["heat_intent_candidate"] = _materialize_heat_intent_candidate(
        source,
        canonical,
        generation_slot,
    )
    result = source
    result.update(canonical)
    if capture_dv_shadow_history and isinstance(dv_shadow_full, dict):
        # Dieser Schlüssel gehört nicht zum Planvertrag und muss vom
        # aufrufenden Simulator vor dem atomaren JSON-Write entfernt werden.
        result["_dv_shadow_history_payload"] = dv_shadow_full
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
        if direct_selection_claim:
            target_state = direct_marketing_target_for_plan_action(
                projection.get("direct_marketing_plan_action")
            )
            if target_state is not None and not direct_marketing_source_action_mode_valid(
                target_state,
                projection.get("direct_marketing_plan_source_action"),
                projection_mode,
            ):
                return {
                    "valid": False,
                    "block_reason_code": (
                        "DIRECT_MARKETING_SOURCE_ACTION_MODE_MISMATCH"
                    ),
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
    trajectory = (
        plan.get("direct_marketing_trajectory")
        if isinstance(plan.get("direct_marketing_trajectory"), dict)
        else None
    )
    if trajectory is None:
        if not _direct_marketing_not_applicable(direct):
            return {
                "valid": False,
                "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_MISSING",
                "plan_id": plan_id,
                "slot": None,
            }
    else:
        if trajectory.get("schema_version") != DIRECT_MARKETING_TRAJECTORY_SCHEMA:
            return {
                "valid": False,
                "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_SCHEMA_INVALID",
                "plan_id": plan_id,
                "slot": None,
            }
        if trajectory.get("plan_id") != plan_id:
            return {
                "valid": False,
                "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_PLAN_ID_MISMATCH",
                "plan_id": plan_id,
                "slot": None,
            }
        if trajectory.get("input_revisions") != plan.get("input_revisions"):
            return {
                "valid": False,
                "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_INPUT_REVISION_MISMATCH",
                "plan_id": plan_id,
                "slot": None,
            }
        if not bool(
            _safe_int(trajectory.get("generated_at_ts_ms"), 0)
            == _safe_int(plan.get("generated_at_ts_ms"), 0)
            and _safe_int(trajectory.get("valid_from_ts_ms"), 0)
            == _safe_int(plan.get("valid_from_ts_ms"), 0)
            and _safe_int(trajectory.get("horizon_end_ts_ms"), 0)
            == _safe_int(plan.get("horizon_end_ts_ms"), 0)
            and _safe_int(trajectory.get("slot_duration_s"), 0)
            == SLOT_DURATION_S
        ):
            return {
                "valid": False,
                "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_HORIZON_MISMATCH",
                "plan_id": plan_id,
                "slot": None,
            }
        if trajectory.get("trajectory_revision") != revision_hash(
            _direct_marketing_trajectory_material(trajectory)
        ):
            return {
                "valid": False,
                "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_REVISION_MISMATCH",
                "plan_id": plan_id,
                "slot": None,
            }
        trajectory_slots = (
            trajectory.get("slots")
            if isinstance(trajectory.get("slots"), list)
            else []
        )
        if trajectory.get("active") is False:
            if bool(
                trajectory_slots
                or trajectory.get("complete") is not True
                or trajectory.get("status") != "DIRECT_MARKETING_DISABLED"
                or not _direct_marketing_not_applicable(direct)
            ):
                return {
                    "valid": False,
                    "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_DISABLED_STATE_INVALID",
                    "plan_id": plan_id,
                    "slot": None,
                }
        else:
            complete_statuses = {"COMPLETE", "COMPLETE_BOUNDED"}
            if bool(
                (trajectory.get("complete") is True)
                != (trajectory.get("status") in complete_statuses)
            ):
                return {
                    "valid": False,
                    "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_STATUS_INVALID",
                    "plan_id": plan_id,
                    "slot": None,
                }
            if trajectory_slots:
                meta = (
                    trajectory.get("meta")
                    if isinstance(trajectory.get("meta"), dict)
                    else {}
                )
                efficiencies = (
                    meta.get("efficiencies")
                    if isinstance(meta.get("efficiencies"), dict)
                    else {}
                )
                signs = (
                    meta.get("signs")
                    if isinstance(meta.get("signs"), dict)
                    else {}
                )
                hardware_limits = (
                    meta.get("hardware_limits_w")
                    if isinstance(meta.get("hardware_limits_w"), dict)
                    else {}
                )
                capacity_wh = _canonical_trajectory_finite_number(
                    meta.get("capacity_wh")
                )
                charge_efficiency = _canonical_trajectory_finite_number(
                    efficiencies.get("charge")
                )
                discharge_efficiency = _canonical_trajectory_finite_number(
                    efficiencies.get("discharge")
                )
                max_charge_w = _canonical_trajectory_finite_number(
                    hardware_limits.get("charge")
                )
                max_discharge_w = _canonical_trajectory_finite_number(
                    hardware_limits.get("discharge")
                )
                if not bool(
                    capacity_wh is not None
                    and capacity_wh > 1000.0
                    and charge_efficiency is not None
                    and 0.0 < charge_efficiency <= 1.0
                    and discharge_efficiency is not None
                    and 0.0 < discharge_efficiency <= 1.0
                    and max_charge_w is not None
                    and max_charge_w > 0.0
                    and max_discharge_w is not None
                    and max_discharge_w > 0.0
                    and signs.get("battery_w")
                    == "positive_charge_negative_discharge"
                    and signs.get("grid_w")
                    == "positive_import_negative_export"
                ):
                    return {
                        "valid": False,
                        "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_META_INVALID",
                        "plan_id": plan_id,
                        "slot": None,
                    }
            canonical_slots_by_start = {
                _safe_int(item.get("start_ts_ms"), 0): item
                for item in slots
                if isinstance(item, dict)
            }
            seen_trajectory_slots = set()
            previous_trajectory_soc_end: Optional[float] = None
            for trajectory_slot in trajectory_slots:
                if not isinstance(trajectory_slot, dict):
                    return {
                        "valid": False,
                        "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_SLOT_INVALID",
                        "plan_id": plan_id,
                        "slot": None,
                    }
                start_ms = _safe_int(trajectory_slot.get("start_ts_ms"), 0)
                end_ms = _safe_int(trajectory_slot.get("end_ts_ms"), 0)
                expected_slot_id = slot_ids_by_start.get(start_ms)
                if bool(
                    expected_slot_id is None
                    or trajectory_slot.get("slot_id") != expected_slot_id
                    or end_ms - start_ms != SLOT_DURATION_S * 1000
                    or start_ms in seen_trajectory_slots
                ):
                    return {
                        "valid": False,
                        "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_SLOT_ID_MISMATCH",
                        "plan_id": plan_id,
                        "slot": None,
                    }
                seen_trajectory_slots.add(start_ms)
                canonical_slot = canonical_slots_by_start.get(start_ms, {})
                canonical_projection = (
                    canonical_slot.get("projection")
                    if isinstance(canonical_slot.get("projection"), dict)
                    else {}
                )
                expected_pv_axis_evidence = (
                    _direct_marketing_pv_axis_evidence_for_slot(canonical_slot)
                )
                if bool(
                    expected_pv_axis_evidence is None
                    or trajectory_slot.get("pv_axis_evidence")
                    != expected_pv_axis_evidence
                    or (
                        trajectory_slot.get("provenance")
                        if isinstance(trajectory_slot.get("provenance"), dict)
                        else {}
                    ).get("pv_axis_evidence_class")
                    != expected_pv_axis_evidence.get("class")
                ):
                    return {
                        "valid": False,
                        "block_reason_code": (
                            "DIRECT_MARKETING_TRAJECTORY_PV_EVIDENCE_MISMATCH"
                        ),
                        "plan_id": plan_id,
                        "slot": None,
                    }
                selection = (
                    trajectory_slot.get("selection")
                    if isinstance(trajectory_slot.get("selection"), dict)
                    else {}
                )
                delegation = (
                    trajectory_slot.get("delegation")
                    if isinstance(trajectory_slot.get("delegation"), dict)
                    else {}
                )
                selection_requested_w = _canonical_trajectory_finite_number(
                    selection.get("requested_w")
                )
                delegation_max_curve_charge_raw = delegation.get(
                    "max_curve_charge_w"
                )
                delegation_max_curve_charge_w = (
                    _canonical_trajectory_finite_number(
                        delegation_max_curve_charge_raw
                    )
                )
                delegation_power_type_valid = bool(
                    delegation_max_curve_charge_raw is None
                    or delegation_max_curve_charge_w is not None
                )
                action = str(trajectory_slot.get("action") or "").upper()
                if selection.get("selected") is True:
                    if not bool(
                        selection.get("executable") is True
                        and selection.get("commands_allowed") is True
                        and action
                        in {
                            "PV_STORE",
                            "ECONOMIC_EXPORT",
                            "CHARGE_BLOCK_WAIT",
                        }
                        and str(selection.get("action_id") or "").startswith(
                            "sha256:"
                        )
                        and len(str(selection.get("action_id") or "")) == 71
                        and selection.get("window_id")
                        and canonical_projection.get("direct_marketing_selected")
                        is True
                        and canonical_projection.get(
                            "direct_marketing_plan_executable"
                        )
                        is True
                        and canonical_projection.get(
                            "direct_marketing_plan_commands_allowed"
                        )
                        is True
                        and canonical_projection.get(
                            "direct_marketing_plan_action"
                        )
                        == action
                        and canonical_projection.get(
                            "direct_marketing_plan_action_id"
                        )
                        == selection.get("action_id")
                        and canonical_projection.get(
                            "direct_marketing_window_id"
                        )
                        == selection.get("window_id")
                        and canonical_projection.get(
                            "direct_marketing_plan_segment_id"
                        )
                        == selection.get("segment_id")
                        and canonical_projection.get(
                            "direct_marketing_plan_source_action"
                        )
                        == selection.get("source_action")
                        and canonical_projection.get(
                            "direct_marketing_plan_source_mode"
                        )
                        == selection.get("source_mode")
                        and canonical_projection.get(
                            "direct_marketing_plan_pv_store_source_contract"
                        )
                        == selection.get("pv_store_source_contract")
                        and _canonical_trajectory_finite_number(
                            canonical_projection.get(
                                "direct_marketing_requested_w"
                            )
                        )
                        == selection_requested_w
                    ):
                        return {
                            "valid": False,
                            "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_ACTION_IDENTITY_INVALID",
                            "plan_id": plan_id,
                            "slot": None,
                        }
                elif action == "PV_STORE":
                    if not bool(
                        delegation.get("schema_version")
                        == "direct_marketing_future_pv_store_delegation_v1"
                        and delegation.get("active") is True
                        and delegation.get("commands_allowed") is True
                        and delegation.get("action") == "PV_STORE"
                        and delegation.get("reason")
                        in {
                            "reserve_recovery",
                            "house_need_until_future_window",
                            "future_window_energy_insufficient",
                        }
                        and delegation.get("pv_store_source_contract")
                        == "E3DC_DC"
                        and delegation_max_curve_charge_w is not None
                        and delegation_max_curve_charge_w >= 300.0
                        and end_ms
                        <= _safe_int(
                            delegation.get("valid_until_ts_ms"),
                            0,
                        )
                    ):
                        return {
                            "valid": False,
                            "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_DELEGATION_INVALID",
                            "plan_id": plan_id,
                            "slot": None,
                        }
                elif action == "PASSIVE_NORMAL" and not bool(
                    isinstance(trajectory_slot.get("passive_binding"), dict)
                    and trajectory_slot.get("passive_binding", {}).get("schema")
                    == DIRECT_MARKETING_PASSIVE_NORMAL_BINDING_SCHEMA
                    and canonical_projection.get(
                        DIRECT_MARKETING_PASSIVE_NORMAL_BINDING_SCHEMA
                    )
                    == trajectory_slot.get("passive_binding")
                ):
                    return {
                        "valid": False,
                        "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_PASSIVE_BINDING_MISSING",
                        "plan_id": plan_id,
                        "slot": None,
                    }
                battery_w = _canonical_trajectory_finite_number(
                    trajectory_slot.get("battery_w")
                )
                grid_w = _canonical_trajectory_finite_number(
                    trajectory_slot.get("grid_w")
                )
                pv_values = (
                    trajectory_slot.get("pv_w")
                    if isinstance(trajectory_slot.get("pv_w"), dict)
                    else {}
                )
                pv_total_raw = pv_values.get("total")
                pv_e3dc_dc_raw = pv_values.get("e3dc_dc")
                pv_external_ac_raw = pv_values.get("external_ac")
                pv_w = _canonical_trajectory_finite_number(pv_total_raw)
                e3dc_dc_w = _canonical_trajectory_finite_number(
                    pv_e3dc_dc_raw
                )
                external_ac_w = _canonical_trajectory_finite_number(
                    pv_external_ac_raw
                )
                trajectory_pv_types_valid = bool(
                    pv_w is not None
                    and (
                        pv_e3dc_dc_raw is None
                        or e3dc_dc_w is not None
                    )
                    and (
                        pv_external_ac_raw is None
                        or external_ac_w is not None
                    )
                )
                canonical_forecast = (
                    canonical_slot.get("forecast_w")
                    if isinstance(canonical_slot.get("forecast_w"), dict)
                    else {}
                )
                canonical_e3dc_dc = (
                    canonical_forecast.get("e3dc_dc_pv")
                    if isinstance(canonical_forecast.get("e3dc_dc_pv"), dict)
                    else {}
                )
                canonical_external_ac = (
                    canonical_forecast.get("external_ac_pv")
                    if isinstance(
                        canonical_forecast.get("external_ac_pv"),
                        dict,
                    )
                    else {}
                )
                canonical_pv_total_raw = canonical_projection.get("pv_w")
                canonical_e3dc_dc_raw = canonical_e3dc_dc.get("point")
                canonical_external_ac_raw = canonical_external_ac.get("point")
                canonical_pv_w = _canonical_trajectory_finite_number(
                    canonical_pv_total_raw
                )
                canonical_e3dc_dc_w = _canonical_trajectory_finite_number(
                    canonical_e3dc_dc_raw
                )
                canonical_external_ac_w = _canonical_trajectory_finite_number(
                    canonical_external_ac_raw
                )
                canonical_pv_types_valid = bool(
                    canonical_pv_w is not None
                    and (
                        canonical_e3dc_dc_raw is None
                        or canonical_e3dc_dc_w is not None
                    )
                    and (
                        canonical_external_ac_raw is None
                        or canonical_external_ac_w is not None
                    )
                )
                if not bool(
                    trajectory_pv_types_valid
                    and canonical_pv_types_valid
                    and _optional_numeric_equal(
                        pv_w,
                        canonical_pv_w,
                    )
                    and _optional_numeric_equal(
                        e3dc_dc_w,
                        canonical_e3dc_dc_w,
                    )
                    and _optional_numeric_equal(
                        external_ac_w,
                        canonical_external_ac_w,
                    )
                ):
                    return {
                        "valid": False,
                        "block_reason_code": (
                            "DIRECT_MARKETING_TRAJECTORY_PV_EVIDENCE_MISMATCH"
                        ),
                        "plan_id": plan_id,
                        "slot": None,
                    }
                load_values = (
                    trajectory_slot.get("loads_w")
                    if isinstance(trajectory_slot.get("loads_w"), dict)
                    else {}
                )
                loads_w = _canonical_trajectory_finite_number(
                    load_values.get("total")
                )
                house_w = _canonical_trajectory_finite_number(
                    load_values.get("house")
                )
                heat_w = _canonical_trajectory_finite_number(
                    load_values.get("heat")
                )
                wallbox_w = _canonical_trajectory_finite_number(
                    load_values.get("wallbox")
                )
                soc_start_pct = _canonical_trajectory_finite_number(
                    trajectory_slot.get("soc_start_pct")
                )
                soc_end_pct = _canonical_trajectory_finite_number(
                    trajectory_slot.get("soc_end_pct")
                )
                residual_before_w = _canonical_trajectory_finite_number(
                    trajectory_slot.get("residual_before_storage_w")
                )
                residual_after_w = _canonical_trajectory_finite_number(
                    trajectory_slot.get("residual_after_storage_w")
                )
                expected_soc_end_pct = None
                if None not in {
                    battery_w,
                    soc_start_pct,
                    capacity_wh,
                    charge_efficiency,
                    discharge_efficiency,
                }:
                    if (battery_w or 0.0) >= 0.0:
                        expected_soc_end_pct = (
                            (soc_start_pct or 0.0)
                            + (battery_w or 0.0)
                            * (SLOT_DURATION_S / 3600.0)
                            * (charge_efficiency or 0.0)
                            / (capacity_wh or 1.0)
                            * 100.0
                        )
                    else:
                        expected_soc_end_pct = (
                            (soc_start_pct or 0.0)
                            - abs(battery_w or 0.0)
                            * (SLOT_DURATION_S / 3600.0)
                            / (discharge_efficiency or 1.0)
                            / (capacity_wh or 1.0)
                            * 100.0
                        )
                    expected_soc_end_pct = max(
                        0.0,
                        min(100.0, expected_soc_end_pct),
                    )
                hard_reserve_pct = _canonical_trajectory_finite_number(
                    trajectory_slot.get("hard_reserve_soc_pct")
                )
                ceiling_pct = _canonical_trajectory_finite_number(
                    trajectory_slot.get("ceiling_soc_pct")
                )
                available_discharge_hi_w = None
                if None not in {
                    soc_start_pct,
                    hard_reserve_pct,
                    capacity_wh,
                    discharge_efficiency,
                }:
                    # Der Vertrag veröffentlicht SoC und Reserveboden mit
                    # 0,001 Prozentpunkten, die Kapazität mit 0,001 Wh und den
                    # Wirkungsgrad mit sechs Nachkommastellen. Für den daraus
                    # rekonstruierten Reserveleistungsdeckel gilt deshalb die
                    # exakte obere Grenze dieser Rundungsintervalle.
                    start_hi_pct = min(100.0, (soc_start_pct or 0.0) + 0.0005)
                    reserve_lo_pct = max(
                        0.0,
                        (hard_reserve_pct or 0.0) - 0.0005,
                    )
                    capacity_hi_wh = (capacity_wh or 0.0) + 0.0005
                    discharge_efficiency_hi = min(
                        1.0,
                        (discharge_efficiency or 0.0) + 0.0000005,
                    )
                    slot_h = 0.25
                    available_discharge_hi_w = max(
                        0.0,
                        (start_hi_pct - reserve_lo_pct)
                        / 100.0
                        * capacity_hi_wh
                        * discharge_efficiency_hi
                        / slot_h,
                    )
                    if not math.isfinite(available_discharge_hi_w):
                        available_discharge_hi_w = None
                action_power_invalid = False
                if action == "PV_STORE" and (battery_w or 0.0) >= 0.0:
                    source_contract = str(
                        selection.get("pv_store_source_contract")
                        if selection.get("selected") is True
                        else delegation.get("pv_store_source_contract")
                        or ""
                    )
                    requested_charge_w = (
                        selection_requested_w
                        if selection.get("selected") is True
                        else delegation_max_curve_charge_w
                    )
                    charge_caps = [
                        value
                        for value in (
                            requested_charge_w,
                            max_charge_w,
                            max(0.0, residual_before_w or 0.0),
                            e3dc_dc_w
                            if source_contract == "E3DC_DC"
                            else None,
                        )
                        if value is not None
                    ]
                    action_power_invalid = bool(
                        requested_charge_w is None
                        or max_charge_w is None
                        or source_contract == "E3DC_DC"
                        and e3dc_dc_w is None
                        and (battery_w or 0.0) > 0.001
                        or charge_caps
                        and (battery_w or 0.0) > min(charge_caps) + 0.01
                    )
                elif action == "ECONOMIC_EXPORT":
                    requested_discharge_w = selection_requested_w
                    action_power_invalid = bool(
                        requested_discharge_w is None
                        or max_discharge_w is None
                        or available_discharge_hi_w is None
                        or abs(battery_w or 0.0)
                        > min(
                            requested_discharge_w or 0.0,
                            max_discharge_w or 0.0,
                        )
                        + 0.01
                        or abs(battery_w or 0.0)
                        > (available_discharge_hi_w or 0.0) + 0.0005
                    )
                elif action in {"PASSIVE_NORMAL", "CHARGE_BLOCK_WAIT"}:
                    action_power_invalid = bool(
                        available_discharge_hi_w is None
                        or abs(battery_w or 0.0)
                        > min(
                            max(0.0, -(residual_before_w or 0.0)),
                            max_discharge_w or 0.0,
                        )
                        + 0.01
                        or abs(battery_w or 0.0)
                        > (available_discharge_hi_w or 0.0) + 0.0005
                    )
                if bool(
                    None
                    in {
                        battery_w,
                        grid_w,
                        pv_w,
                        loads_w,
                        house_w,
                        heat_w,
                        wallbox_w,
                        soc_start_pct,
                        soc_end_pct,
                        residual_before_w,
                        residual_after_w,
                        expected_soc_end_pct,
                        hard_reserve_pct,
                        ceiling_pct,
                        selection_requested_w,
                    }
                    or not delegation_power_type_valid
                    or not (0.0 <= (soc_start_pct or 0.0) <= 100.0)
                    or not (0.0 <= (soc_end_pct or 0.0) <= 100.0)
                    or not (
                        0.0
                        <= (hard_reserve_pct or 0.0)
                        <= (ceiling_pct or 0.0)
                        <= 100.0
                    )
                    or (soc_end_pct or 0.0) > (ceiling_pct or 0.0) + 0.002
                    or (
                        previous_trajectory_soc_end is not None
                        and abs(
                            (soc_start_pct or 0.0)
                            - previous_trajectory_soc_end
                        )
                        > 0.002
                    )
                    or abs(
                        (soc_end_pct or 0.0)
                        - (expected_soc_end_pct or 0.0)
                    )
                    > 0.002
                    or abs(
                        (house_w or 0.0)
                        + (heat_w or 0.0)
                        + (wallbox_w or 0.0)
                        - (loads_w or 0.0)
                    )
                    > 0.01
                    or abs(
                        (pv_w or 0.0)
                        - (loads_w or 0.0)
                        - (residual_before_w or 0.0)
                    )
                    > 0.01
                    or abs(
                        (pv_w or 0.0)
                        - (loads_w or 0.0)
                        - (battery_w or 0.0)
                        - (residual_after_w or 0.0)
                    )
                    > 0.01
                    or abs(
                        (loads_w or 0.0)
                        + (battery_w or 0.0)
                        - (pv_w or 0.0)
                        - (grid_w or 0.0)
                    )
                    > 0.01
                    or (action == "PV_STORE" and (battery_w or 0.0) < -0.001)
                    or (
                        action == "ECONOMIC_EXPORT"
                        and (battery_w or 0.0) > 0.001
                    )
                    or (
                        action == "CHARGE_BLOCK_WAIT"
                        and (battery_w or 0.0) > 0.001
                    )
                    or action_power_invalid
                ):
                    return {
                        "valid": False,
                        "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_BALANCE_INVALID",
                        "plan_id": plan_id,
                        "slot": None,
                    }
                previous_trajectory_soc_end = soc_end_pct
            if trajectory.get("complete") is True:
                expected_starts = {
                    _safe_int(item.get("start_ts_ms"), 0)
                    for item in slots
                    if _safe_int(item.get("start_ts_ms"), 0)
                    >= _safe_int(trajectory.get("valid_from_ts_ms"), 0)
                }
                if seen_trajectory_slots != expected_starts:
                    return {
                        "valid": False,
                        "block_reason_code": "DIRECT_MARKETING_TRAJECTORY_SLOT_COVERAGE_INCOMPLETE",
                        "plan_id": plan_id,
                        "slot": None,
                    }
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


def _selected_action_projection_limited(
    plan: Dict[str, Any],
    reason_code: str,
) -> Dict[str, Any]:
    direct = (
        plan.get("direct_marketing")
        if isinstance(plan.get("direct_marketing"), dict)
        else {}
    )
    active = _direct_marketing_enabled(direct)
    return {
        "schema_version": DIRECT_MARKETING_SELECTED_ACTION_FALLBACK_SCHEMA,
        "active": active,
        "complete": False,
        "status": "EVIDENCE_LIMIT" if active else "INACTIVE",
        "consumer_scope": "web_projection",
        "control_effect": False,
        "runtime_effect_claim_allowed": False,
        "hardware_effect_claim_allowed": False,
        "candidate_effect_allowed": False,
        "reason_code": (
            str(reason_code or "DIRECT_MARKETING_ACTION_PROJECTION_INVALID")
            if active
            else "DIRECT_MARKETING_DISABLED"
        ),
        "plan_id": plan.get("plan_id"),
        "plan_material_revision": None,
        "generated_at_ts_ms": plan.get("generated_at_ts_ms"),
        "valid_from_ts_ms": plan.get("valid_from_ts_ms"),
        "valid_until_ts_ms": plan.get("valid_until_ts_ms"),
        "horizon_end_ts_ms": plan.get("horizon_end_ts_ms"),
        "slot_duration_s": plan.get("slot_duration_s"),
        "input_revisions": copy.deepcopy(plan.get("input_revisions")),
        "input_revisions_revision": None,
        "trajectory_revision": None,
        "slot_axis_revision": None,
        "action_axis_revision": None,
        "slots": [],
        "projection_revision": None,
    }


def _selected_action_projection_binding(
    plan: Dict[str, Any],
    slot: Dict[str, Any],
    projection: Dict[str, Any],
    action: str,
    planned_w: float,
    *,
    valid_from_ms: int,
    horizon_end_ms: int,
) -> Optional[Dict[str, Any]]:
    expected_sources = {
        "ECONOMIC_EXPORT": ("eco_plus_export_candidate", {"eco_plus"}),
        "PV_STORE": ("eco_plus_store_pv_candidate", {"eco", "eco_plus"}),
        "CHARGE_BLOCK_WAIT": (
            "direct_marketing_charge_block_wait",
            {"eco_plus"},
        ),
    }
    expected = expected_sources.get(action)
    direct = (
        plan.get("direct_marketing")
        if isinstance(plan.get("direct_marketing"), dict)
        else {}
    )
    flags = (
        direct.get("flags")
        if isinstance(direct.get("flags"), dict)
        else {}
    )
    decision_horizon = (
        plan.get("shadow_dispatch", {}).get("decision_horizon")
        if isinstance(plan.get("shadow_dispatch"), dict)
        and isinstance(
            plan.get("shadow_dispatch", {}).get("decision_horizon"),
            dict,
        )
        else {}
    )
    slot_start_ms = _safe_int(slot.get("start_ts_ms"), 0)
    slot_end_ms = _safe_int(slot.get("end_ts_ms"), 0)
    source_action = str(
        projection.get("direct_marketing_plan_source_action") or ""
    ).strip()
    source_mode = _normalized_direct_marketing_mode(
        projection.get("direct_marketing_plan_source_mode")
    )
    plan_mode = _normalized_direct_marketing_mode(direct.get("mode"))
    action_id = str(
        projection.get("direct_marketing_plan_action_id") or ""
    )
    action_lineage_id = str(
        projection.get("direct_marketing_plan_action_lineage_id") or ""
    )
    window_id = str(projection.get("direct_marketing_window_id") or "").strip()
    window_start_ms = _safe_int(
        projection.get("direct_marketing_window_start_ts_ms"),
        0,
    )
    window_end_ms = _safe_int(
        projection.get("direct_marketing_window_end_ts_ms"),
        0,
    )
    segment_id = str(
        projection.get("direct_marketing_plan_segment_id") or ""
    ).strip()
    horizon = (
        projection.get("direct_marketing_action_horizon_contract")
        if isinstance(
            projection.get("direct_marketing_action_horizon_contract"),
            dict,
        )
        else {}
    )
    roles = (
        projection.get("direct_marketing_action_roles")
        if isinstance(projection.get("direct_marketing_action_roles"), dict)
        else {}
    )
    requested_w = _safe_float(
        projection.get("direct_marketing_requested_w"),
        None,
    )
    if not bool(
        expected is not None
        and direct.get("active") is True
        and direct.get("shadow") is False
        and flags.get("commands_allowed") is True
        and source_action == expected[0]
        and source_mode in expected[1]
        and plan_mode == source_mode
        and projection.get(
            "direct_marketing_plan_source_action_execution_released"
        )
        is True
        and _sha256_revision_valid(action_id)
        and action_lineage_id == action_id
        and window_id
        and segment_id
        and window_start_ms > 0
        and window_end_ms > window_start_ms
        and window_start_ms <= slot_start_ms
        and slot_end_ms <= window_end_ms
        and requested_w is not None
        and abs(requested_w - planned_w) <= 0.01
        and projection.get("direct_marketing_candidate") is True
        and projection.get("direct_marketing_candidate_action") == action
        and projection.get("direct_marketing_candidate_only") is False
        and projection.get("direct_marketing_plan_selected_action") == action
        and projection.get("direct_marketing_plan_executable_action") == action
        and projection.get("direct_marketing_effective_action") is None
        and projection.get("direct_marketing_block_reason") is None
    ):
        return None

    decision_start_ms = _safe_int(decision_horizon.get("start_ts_ms"), 0)
    decision_end_ms = _safe_int(decision_horizon.get("end_ts_ms"), 0)
    if not bool(
        horizon.get("schema_version") == "storage_dispatch_action_horizon_v1"
        and horizon.get("action") == action
        and horizon.get("complete") is True
        and horizon.get("block_reason_code") is None
        and horizon.get("window_source")
        == "canonical_direct_marketing_plan_projection"
        and _safe_int(horizon.get("slot_start_ts_ms"), 0) == slot_start_ms
        and _safe_int(horizon.get("slot_end_ts_ms"), 0) == slot_end_ms
        and _safe_int(horizon.get("window_start_ts_ms"), 0)
        == window_start_ms
        and _safe_int(horizon.get("window_end_ts_ms"), 0) == window_end_ms
        and decision_start_ms == valid_from_ms
        and decision_end_ms > 0
        and window_end_ms <= decision_end_ms <= horizon_end_ms
        and _safe_int(horizon.get("bound_horizon_start_ts_ms"), 0)
        == decision_start_ms
        and _safe_int(horizon.get("bound_horizon_end_ts_ms"), 0)
        == decision_end_ms
    ):
        return None
    if not bool(
        roles.get("schema_version") == DIRECT_MARKETING_ACTION_ROLES_SCHEMA
        and roles.get("status") == "CONSISTENT"
        and roles.get("candidate_action") == action
        and roles.get("candidate_only") is False
        and roles.get("plan_selected_action") == action
        and roles.get("plan_executable_action") == action
        and roles.get("effective_action") is None
        and roles.get("runtime_effect_claim_allowed") is False
        and _safe_int(roles.get("slot_start_ts_ms"), 0) == slot_start_ms
        and _safe_int(roles.get("slot_end_ts_ms"), 0) == slot_end_ms
    ):
        return None

    source_windows: List[Dict[str, Any]] = []
    for window in direct.get("windows") or []:
        if not isinstance(window, dict):
            continue
        if str(window.get("action") or "") != source_action:
            continue
        if bool(
            _to_ts_ms(window.get("start_ts")) != window_start_ms
            or _to_ts_ms(window.get("end_ts")) != window_end_ms
        ):
            continue
        source_window_id = str(
            (
                window.get("export_plateau_id")
                if action == "ECONOMIC_EXPORT"
                else window.get("window_id")
            )
            or ""
        ).strip()
        projected_window_id = str(
            (
                projection.get("direct_marketing_export_plateau_id")
                if action == "ECONOMIC_EXPORT"
                else window_id
            )
            or ""
        ).strip()
        if not source_window_id or source_window_id != projected_window_id:
            continue
        if window.get("export_segment_id") != projection.get(
            "direct_marketing_export_segment_id"
        ):
            continue
        if bool(
            action == "PV_STORE"
            and window.get("pv_store_source_contract")
            != projection.get("direct_marketing_plan_pv_store_source_contract")
        ):
            continue
        source_windows.append(window)
    if len(source_windows) != 1:
        return None
    source_window = source_windows[0]
    source_max_w = _safe_float(source_window.get("max_power_w"), None)
    if bool(
        action != "CHARGE_BLOCK_WAIT"
        and (source_max_w is None or source_max_w + 0.01 < planned_w)
    ):
        return None
    source_segment = (
        source_window.get("export_segment_id")
        or source_window.get("segment_id")
        or action_id
    )
    if segment_id != str(source_segment):
        return None

    identity_material: Dict[str, Any] = {
        "action": action,
        "window_id": window_id,
        "window_start_ts_ms": window_start_ms,
        "window_end_ts_ms": window_end_ms,
    }
    export_gate = (
        projection.get("direct_marketing_economic_export_gate")
        if isinstance(
            projection.get("direct_marketing_economic_export_gate"),
            dict,
        )
        else None
    )
    gate_revision = None
    gate_lineage_id = None
    gate_generation = None
    gate_generation_id = None
    if action != "ECONOMIC_EXPORT":
        if bool(
            export_gate is not None
            or projection.get("direct_marketing_gate_lineage_id") is not None
            or projection.get("direct_marketing_gate_generation") is not None
            or projection.get("direct_marketing_gate_generation_id") is not None
        ):
            return None
        pv_contract = projection.get(
            "direct_marketing_plan_pv_store_source_contract"
        )
        if bool(
            action == "PV_STORE"
            and pv_contract not in {"E3DC_DC", "E3DC_DC_PLUS_AUX_AC_PV"}
        ):
            return None
        if action != "PV_STORE" and pv_contract is not None:
            return None
    else:
        if not isinstance(export_gate, dict):
            return None
        economics = [
            _safe_float(export_gate.get(key), None)
            for key in (
                "margin_ct_kwh",
                "user_min_margin_ct",
                "expected_profit_eur",
                "min_window_profit_eur",
            )
        ]
        policy_budget_w = _safe_float(
            export_gate.get("policy_export_budget_w"),
            None,
        )
        if not bool(
            export_gate.get("allowed") is True
            and isinstance(export_gate.get("blockers"), list)
            and not export_gate.get("blockers")
            and export_gate.get("block_reason_code") is None
            and export_gate.get("policy_commands_allowed") is True
            and export_gate.get("accounting_contract")
            == "DIRECT_MARKETING_POLICY_ECONOMICS_REUSED_NO_DOUBLE_DEDUCTION"
            and policy_budget_w is not None
            and policy_budget_w + 0.01 >= planned_w
            and all(value is not None for value in economics)
            and (economics[1] or 0.0) >= 0.0
            and (economics[3] or 0.0) >= 0.0
            and (economics[0] or 0.0) + 0.000001 >= (economics[1] or 0.0)
            and (economics[2] or 0.0) + 0.000001 >= (economics[3] or 0.0)
        ):
            return None
        start_gate = (
            export_gate.get("export_window_start_gate")
            if isinstance(export_gate.get("export_window_start_gate"), dict)
            else {}
        )
        business = (
            start_gate.get("business_binding")
            if isinstance(start_gate.get("business_binding"), dict)
            else {}
        )
        business_revision = str(
            business.get("business_contract_sha256") or ""
        )
        business_material = copy.deepcopy(business)
        business_material.pop("business_contract_sha256", None)
        if not bool(
            start_gate.get("schema") == "export_window_start_gate_v1"
            and start_gate.get("passed") is True
            and start_gate.get("profile")
            in {"standard", "aggressive", "expert"}
            and start_gate.get("action") == source_action
            and start_gate.get("window_id") == window_id
            and start_gate.get("business_window_id") == window_id
            and _to_ts_ms(start_gate.get("origin_start_ts")) == window_start_ms
            and _to_ts_ms(start_gate.get("end_ts")) == window_end_ms
            and start_gate.get("accounting_contract")
            == "START_ONLY_NO_REMAINING_WINDOW_REAPPLICATION"
            and business.get("schema")
            == "direct_marketing_export_business_binding_v1"
            and business.get("action") == source_action
            and _to_ts_ms(business.get("origin_start_ts")) == window_start_ms
            and _to_ts_ms(business.get("end_ts")) == window_end_ms
            and _sha256_revision_valid(business_revision)
            and business_revision == revision_hash(business_material)
            and start_gate.get("business_contract_sha256")
            == business_revision
            and window_id
            == "export-business:" + business_revision[7:31]
        ):
            return None
        lineage = (
            export_gate.get("export_window_gate_lineage")
            if isinstance(
                export_gate.get("export_window_gate_lineage"),
                dict,
            )
            else {}
        )
        gate_sha256 = revision_hash(start_gate)
        expected_lineage_id = revision_hash({
            "schema": "export_window_gate_lineage_v1",
            "gate_sha256": gate_sha256,
            "action": source_action,
            "window_id": window_id,
            "origin_start_ts": window_start_ms,
            "end_ts": window_end_ms,
        })
        generation = lineage.get("current_generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            return None
        expected_generation_id = revision_hash({
            "gate_lineage_id": expected_lineage_id,
            "generation": generation,
        })
        expected_previous_id = (
            revision_hash({
                "gate_lineage_id": expected_lineage_id,
                "generation": generation - 1,
            })
            if generation > 1
            else None
        )
        transition_reasons = lineage.get("transition_reason_codes")
        if not bool(
            lineage.get("schema") == "export_window_gate_lineage_v1"
            and lineage.get("status") == "ACTIVE"
            and lineage.get("effect_contract")
            == "STATUS_ONLY_NO_EXECUTION_AUTHORITY"
            and lineage.get("gate_sha256") == gate_sha256
            and lineage.get("gate_lineage_id") == expected_lineage_id
            and lineage.get("current_generation_id")
            == expected_generation_id
            and lineage.get("previous_generation_id") == expected_previous_id
            and lineage.get("action") == source_action
            and lineage.get("window_id") == window_id
            and _to_ts_ms(lineage.get("origin_start_ts")) == window_start_ms
            and _to_ts_ms(lineage.get("end_ts")) == window_end_ms
            and isinstance(transition_reasons, list)
            and bool(transition_reasons)
            and transition_reasons == sorted(set(transition_reasons))
            and projection.get("direct_marketing_gate_lineage_id")
            == expected_lineage_id
            and projection.get("direct_marketing_gate_generation")
            == generation
            and projection.get("direct_marketing_gate_generation_id")
            == expected_generation_id
            and export_gate.get("action_horizon_contract") == horizon
        ):
            return None
        identity_material.update({
            "gate_lineage_id": expected_lineage_id,
            "gate_generation": generation,
            "gate_generation_id": expected_generation_id,
        })
        gate_revision = revision_hash(export_gate)
        gate_lineage_id = expected_lineage_id
        gate_generation = generation
        gate_generation_id = expected_generation_id

    if revision_hash(identity_material) != action_id:
        return None
    return {
        "slot_id": slot.get("slot_id"),
        "start_ts_ms": slot_start_ms,
        "end_ts_ms": slot_end_ms,
        "selected": True,
        "executable": True,
        "commands_allowed": True,
        "action": action,
        "planned_w": round(planned_w, 3),
        "action_id": action_id,
        "action_lineage_id": action_lineage_id,
        "window_id": window_id,
        "window_start_ts_ms": window_start_ms,
        "window_end_ts_ms": window_end_ms,
        "segment_id": segment_id,
        "source_action": source_action,
        "source_mode": source_mode,
        "pv_store_source_contract": projection.get(
            "direct_marketing_plan_pv_store_source_contract"
        ),
        "gate_lineage_id": gate_lineage_id,
        "gate_generation": gate_generation,
        "gate_generation_id": gate_generation_id,
        "source_window_revision": revision_hash(source_window),
        "source_projection_revision": revision_hash(projection),
        "action_horizon_revision": revision_hash(horizon),
        "action_roles_revision": revision_hash(roles),
        "economic_export_gate_revision": gate_revision,
    }


def _direct_marketing_selected_action_projection(
    plan: Dict[str, Any],
    *,
    plan_material_revision: str,
) -> Dict[str, Any]:
    limited = lambda reason: _selected_action_projection_limited(plan, reason)
    direct = (
        plan.get("direct_marketing")
        if isinstance(plan.get("direct_marketing"), dict)
        else {}
    )
    if not _direct_marketing_enabled(direct):
        projection = limited("DIRECT_MARKETING_DISABLED")
        projection["plan_material_revision"] = plan_material_revision
        projection["input_revisions_revision"] = revision_hash(
            plan.get("input_revisions")
        )
        return projection
    trajectory = (
        plan.get("direct_marketing_trajectory")
        if isinstance(plan.get("direct_marketing_trajectory"), dict)
        else None
    )
    trajectory_status = (
        trajectory.get("status")
        if isinstance(trajectory, dict)
        else None
    )
    trajectory_meta = (
        trajectory.get("meta")
        if isinstance(trajectory, dict)
        and isinstance(trajectory.get("meta"), dict)
        else None
    )
    passive_policy_binding_meta_valid = bool(
        isinstance(trajectory_meta, dict)
        and trajectory_meta.get("candidate_effect") is False
        and trajectory_meta.get("shadow_effect") is False
        and trajectory_meta.get("runtime_authorization_separate") is True
    )
    if not bool(
        isinstance(trajectory, dict)
        and trajectory.get("schema_version") == DIRECT_MARKETING_TRAJECTORY_SCHEMA
        and trajectory.get("active") is True
        and trajectory.get("complete") is False
        and trajectory_status in (
            "TRAJECTORY_AXIS_EVIDENCE_LIMIT",
            "PASSIVE_POLICY_BINDING_MISSING",
        )
        and (
            trajectory_status != "PASSIVE_POLICY_BINDING_MISSING"
            or passive_policy_binding_meta_valid
        )
        and trajectory.get("reason_code") is None
        and isinstance(trajectory.get("slots"), list)
        and len(trajectory.get("slots")) == 0
        and trajectory.get("plan_id") == plan.get("plan_id")
        and trajectory.get("input_revisions") == plan.get("input_revisions")
        and _safe_int(trajectory.get("generated_at_ts_ms"), 0)
        == _safe_int(plan.get("generated_at_ts_ms"), 0)
        and _safe_int(trajectory.get("valid_from_ts_ms"), 0)
        == _safe_int(plan.get("valid_from_ts_ms"), 0)
        and _safe_int(trajectory.get("horizon_end_ts_ms"), 0)
        == _safe_int(plan.get("horizon_end_ts_ms"), 0)
        and _safe_int(trajectory.get("slot_duration_s"), 0)
        == _safe_int(plan.get("slot_duration_s"), 0)
        and trajectory.get("trajectory_revision")
        == revision_hash(_direct_marketing_trajectory_material(trajectory))
    ):
        return limited(
            "DIRECT_MARKETING_ACTION_PROJECTION_TRAJECTORY_NOT_AXIS_LIMITED"
        )
    plan_id = str(plan.get("plan_id") or "")
    duration_s = _safe_int(plan.get("slot_duration_s"), 0)
    duration_ms = duration_s * 1000
    valid_from_ms = _safe_int(plan.get("valid_from_ts_ms"), 0)
    valid_until_ms = _safe_int(plan.get("valid_until_ts_ms"), 0)
    horizon_end_ms = _safe_int(plan.get("horizon_end_ts_ms"), 0)
    input_revisions = plan.get("input_revisions")
    plan_slots = plan.get("slots") if isinstance(plan.get("slots"), list) else []
    if not bool(
        _sha256_revision_valid(plan_id)
        and plan_material_revision == plan_id
        and duration_s > 0
        and valid_from_ms > 0
        and valid_from_ms < valid_until_ms <= horizon_end_ms
        and isinstance(input_revisions, dict)
        and plan_slots
    ):
        return limited("DIRECT_MARKETING_ACTION_PROJECTION_PLAN_STRUCTURE_INVALID")
    result_slots: List[Dict[str, Any]] = []
    previous_end_ms: Optional[int] = None
    for slot in plan_slots:
        if not isinstance(slot, dict):
            return limited("DIRECT_MARKETING_ACTION_PROJECTION_SLOT_INVALID")
        start_ms = _safe_int(slot.get("start_ts_ms"), 0)
        end_ms = _safe_int(slot.get("end_ts_ms"), 0)
        if end_ms <= valid_from_ms:
            continue
        if not bool(
            start_ms >= valid_from_ms
            and end_ms - start_ms == duration_ms
            and (previous_end_ms is not None or start_ms == valid_from_ms)
            and (previous_end_ms is None or start_ms == previous_end_ms)
            and slot.get("slot_id")
            == revision_hash({
                "plan_id": plan_id,
                "start_ts_ms": start_ms,
                "end_ts_ms": end_ms,
            })
        ):
            return limited(
                "DIRECT_MARKETING_ACTION_PROJECTION_SLOT_BINDING_INVALID"
            )
        projection = (
            slot.get("projection")
            if isinstance(slot.get("projection"), dict)
            else {}
        )
        selected = projection.get("direct_marketing_selected") is True
        executable = (
            projection.get("direct_marketing_plan_executable") is True
        )
        commands_allowed = (
            projection.get("direct_marketing_plan_commands_allowed") is True
        )
        any_role = selected or executable or commands_allowed
        all_roles = selected and executable and commands_allowed
        if any_role and not all_roles:
            return limited(
                "DIRECT_MARKETING_ACTION_PROJECTION_ROLE_INCOMPLETE"
            )
        if all_roles:
            action = str(
                projection.get("direct_marketing_plan_action") or ""
            ).strip().upper()
            planned_w = _safe_float(
                projection.get("direct_marketing_planned_w"),
                None,
            )
            positive_power = action in {"PV_STORE", "ECONOMIC_EXPORT"}
            zero_power = action == "CHARGE_BLOCK_WAIT"
            if not bool(
                planned_w is not None
                and (positive_power or zero_power)
                and (not positive_power or planned_w > 0.0)
                and (not zero_power or abs(planned_w) <= 0.01)
            ):
                return limited(
                    "DIRECT_MARKETING_ACTION_PROJECTION_ACTION_BINDING_INVALID"
                )
            entry = _selected_action_projection_binding(
                plan,
                slot,
                projection,
                action,
                planned_w,
                valid_from_ms=valid_from_ms,
                horizon_end_ms=horizon_end_ms,
            )
            if entry is None:
                return limited(
                    "DIRECT_MARKETING_ACTION_PROJECTION_ACTION_BINDING_INVALID"
                )
        else:
            entry = {
                "slot_id": slot.get("slot_id"),
                "start_ts_ms": start_ms,
                "end_ts_ms": end_ms,
                "selected": False,
                "executable": False,
                "commands_allowed": False,
                "action": None,
                "planned_w": None,
                "action_id": None,
                "action_lineage_id": None,
                "window_id": None,
                "window_start_ts_ms": None,
                "window_end_ts_ms": None,
                "segment_id": None,
                "source_action": None,
                "source_mode": None,
                "pv_store_source_contract": None,
                "gate_lineage_id": None,
                "gate_generation": None,
                "gate_generation_id": None,
                "source_window_revision": None,
                "source_projection_revision": None,
                "action_horizon_revision": None,
                "action_roles_revision": None,
                "economic_export_gate_revision": None,
            }
        result_slots.append(entry)
        previous_end_ms = end_ms
    if previous_end_ms != horizon_end_ms:
        return limited("DIRECT_MARKETING_ACTION_PROJECTION_HORIZON_INVALID")
    slot_axis = [
        {
            "slot_id": slot.get("slot_id"),
            "start_ts_ms": slot.get("start_ts_ms"),
            "end_ts_ms": slot.get("end_ts_ms"),
        }
        for slot in result_slots
    ]
    action_axis = [
        {
            key: slot.get(key)
            for key in (
                "slot_id",
                "action",
                "planned_w",
                "action_id",
                "action_lineage_id",
                "window_id",
                "window_start_ts_ms",
                "window_end_ts_ms",
                "segment_id",
                "source_action",
                "source_mode",
                "pv_store_source_contract",
                "gate_lineage_id",
                "gate_generation",
                "gate_generation_id",
                "source_window_revision",
                "source_projection_revision",
                "action_horizon_revision",
                "action_roles_revision",
                "economic_export_gate_revision",
            )
        }
        for slot in result_slots
        if slot.get("selected") is True
    ]
    return {
        "schema_version": DIRECT_MARKETING_SELECTED_ACTION_FALLBACK_SCHEMA,
        "active": True,
        "complete": True,
        "status": "COMPLETE",
        "consumer_scope": "web_projection",
        "control_effect": False,
        "runtime_effect_claim_allowed": False,
        "hardware_effect_claim_allowed": False,
        "candidate_effect_allowed": False,
        "reason_code": None,
        "plan_id": plan_id,
        "plan_material_revision": plan_material_revision,
        "generated_at_ts_ms": plan.get("generated_at_ts_ms"),
        "valid_from_ts_ms": valid_from_ms,
        "valid_until_ts_ms": valid_until_ms,
        "horizon_end_ts_ms": horizon_end_ms,
        "slot_duration_s": duration_s,
        "input_revisions": copy.deepcopy(input_revisions),
        "input_revisions_revision": revision_hash(input_revisions),
        "trajectory_revision": trajectory.get("trajectory_revision"),
        "slot_axis_revision": revision_hash(slot_axis),
        "action_axis_revision": revision_hash(action_axis),
        "slots": result_slots,
        "projection_revision": None,
    }


def build_storage_plan_action_projection_artifact(
    plan: Dict[str, Any],
    *,
    raw_plan_sha256: str,
    raw_plan_size: int,
) -> Dict[str, Any]:
    """Versiegelt eine kleine, wirkungslose Action-only-Projektion.

    Die teure Planmaterialprüfung läuft genau einmal producerseitig. Webreader
    binden anschließend nur die exakten Rohbytes an dieses kleine Artefakt und
    validieren dessen beide kompakten Revisionen. Der Vertrag erteilt weder
    Runtime- noch Hardwareautorität.
    """

    if not bool(
        isinstance(plan, dict)
        and _sha256_revision_valid(raw_plan_sha256)
        and isinstance(raw_plan_size, int)
        and not isinstance(raw_plan_size, bool)
        and raw_plan_size > 0
    ):
        raise ValueError("storage_plan_action_projection_source_invalid")
    plan_material_revision = revision_hash(_plan_material(plan))
    static = validate_canonical_plan_static(plan)
    if not bool(
        static.get("valid") is True
        and plan_material_revision == plan.get("plan_id")
    ):
        projection = _selected_action_projection_limited(
            plan,
            "DIRECT_MARKETING_ACTION_PROJECTION_PLAN_INVALID",
        )
        projection["plan_material_revision"] = plan_material_revision
        projection["input_revisions_revision"] = revision_hash(
            plan.get("input_revisions")
        )
    else:
        projection = _direct_marketing_selected_action_projection(
            plan,
            plan_material_revision=plan_material_revision,
        )
    projection_material = copy.deepcopy(projection)
    projection_material.pop("projection_revision", None)
    projection_revision = revision_hash(projection_material)
    projection["projection_revision"] = projection_revision
    binding = {
        "plan_schema_version": plan.get("schema_version"),
        "plan_id": plan.get("plan_id"),
        "plan_material_revision": plan_material_revision,
        "raw_plan_sha256": raw_plan_sha256,
        "raw_plan_size": raw_plan_size,
        "generated_at_ts_ms": plan.get("generated_at_ts_ms"),
        "valid_from_ts_ms": plan.get("valid_from_ts_ms"),
        "valid_until_ts_ms": plan.get("valid_until_ts_ms"),
        "horizon_end_ts_ms": plan.get("horizon_end_ts_ms"),
        "slot_duration_s": plan.get("slot_duration_s"),
        "input_revisions_revision": revision_hash(plan.get("input_revisions")),
        "trajectory_revision": (
            plan.get("direct_marketing_trajectory", {}).get(
                "trajectory_revision"
            )
            if isinstance(plan.get("direct_marketing_trajectory"), dict)
            else None
        ),
        "slot_axis_revision": projection.get("slot_axis_revision"),
        "action_axis_revision": projection.get("action_axis_revision"),
        "projection_revision": projection_revision,
    }
    artifact = {
        "schema_version": STORAGE_PLAN_ACTION_PROJECTION_SCHEMA,
        "status": projection.get("status"),
        "consumer_scope": "web_projection",
        "control_effect": False,
        "runtime_effect_claim_allowed": False,
        "hardware_effect_claim_allowed": False,
        "candidate_effect_allowed": False,
        "reason_code": projection.get("reason_code"),
        "plan_binding": binding,
        "projection": projection,
        "projection_revision": projection_revision,
        "artifact_revision": None,
    }
    artifact_material = copy.deepcopy(artifact)
    artifact_material.pop("artifact_revision", None)
    artifact["artifact_revision"] = revision_hash(artifact_material)
    return artifact


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
    try:
        current = os.stat(normalized, follow_symlinks=False)
        generation = _plan_file_generation(current)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_size <= 0
            or current.st_size > int(max_bytes)
            or (
                max_age_s is not None
                and time.time() - current.st_mtime > max(0.0, float(max_age_s))
            )
        ):
            with _PLAN_SNAPSHOT_LOCK:
                for cache_key, cached_plan in list(_PLAN_SNAPSHOT_CACHE.items()):
                    if cached_plan.source_path == normalized:
                        _PLAN_SNAPSHOT_CACHE.pop(cache_key, None)
            return None
    except (OSError, TypeError, ValueError):
        with _PLAN_SNAPSHOT_LOCK:
            for cache_key, cached_plan in list(_PLAN_SNAPSHOT_CACHE.items()):
                if cached_plan.source_path == normalized:
                    _PLAN_SNAPSHOT_CACHE.pop(cache_key, None)
        return None

    # Ein unveränderter, bereits vollständig validierter Dateistand benötigt
    # weder einen erneuten Mehr-MiB-Read noch einen neuen SHA-256-Lauf. Zwei
    # identische no-follow-Stats binden den Cachetreffer an dieselbe Generation;
    # die dynamische Validierung prüft diese Bindung vor jeder Ausführung erneut.
    with _PLAN_SNAPSHOT_LOCK:
        cached = next(
            (
                candidate
                for candidate in _PLAN_SNAPSHOT_CACHE.values()
                if candidate.source_path == normalized
            ),
            None,
        )
        if cached is not None and cached.source_generation == generation:
            try:
                verified = os.stat(normalized, follow_symlinks=False)
            except OSError:
                verified = None
            if (
                verified is not None
                and stat.S_ISREG(verified.st_mode)
                and _plan_file_generation(verified) == generation
            ):
                return cached
        if cached is not None:
            for cache_key, cached_plan in list(_PLAN_SNAPSHOT_CACHE.items()):
                if cached_plan is cached:
                    _PLAN_SNAPSHOT_CACHE.pop(cache_key, None)

    descriptor = -1
    try:
        descriptor = os.open(normalized, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > int(max_bytes):
            return None
        source = bytearray()
        while len(source) <= int(max_bytes):
            chunk = os.read(descriptor, min(1024 * 1024, int(max_bytes) - len(source) + 1))
            if not chunk:
                break
            source.extend(chunk)
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
                try:
                    cached_final = os.stat(normalized, follow_symlinks=False)
                except OSError:
                    cached_final = None
                if (
                    cached_final is not None
                    and stat.S_ISREG(cached_final.st_mode)
                    and _plan_file_generation(cached_final) == generation
                ):
                    _PLAN_SNAPSHOT_CACHE.move_to_end(key)
                    return cached
                _PLAN_SNAPSHOT_CACHE.pop(key, None)
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
    direct_action = str(
        payload.get("direct_marketing_action") or ""
    ).strip().upper()
    target = str(
        payload.get("direct_marketing_target_state")
        or payload.get("direct_marketing_policy_target_state")
        or payload.get("dv_target_state")
        or ""
    ).upper()
    mode_name = str(payload.get("mode_name") or "AUTO").upper()
    value_w = max(0, _safe_int(payload.get("val"), 0))
    auto_limit = (
        payload.get("auto_limit")
        if isinstance(payload.get("auto_limit"), dict)
        else {}
    )
    if payload.get("predump_active") or state.startswith("pre_discharge"):
        return "HEADROOM_EXPORT"
    if target == "HEADROOM_EXPORT":
        return "HEADROOM_EXPORT"
    if target == "CHARGE_BLOCK_WAIT" or state == "direct_marketing_charge_block_wait":
        return "CHARGE_BLOCK_WAIT"
    pv_store_claim = bool(
        direct_action
        in {
            "PV_STORE",
            "FORCE_CHARGE_PV",
            "POLICY_FORCE_CHARGE_PV",
            "ECO_PLUS_STORE_PV_CANDIDATE",
        }
        or target in {"PV_STORE", "FORCE_CHARGE_PV"}
        or "pv_store" in state
    )
    if pv_store_claim:
        if bool(
            value_w >= 300
            and (
                mode_name in {"CHARGE", "CHRG"}
                or (
                    mode_name == "AUTO"
                    and auto_limit.get("enabled") is True
                    and auto_limit.get("release") is not True
                    and _safe_int(auto_limit.get("max_charge_w"), 0) >= 300
                )
            )
        ):
            return "PV_STORE"
        if (
            mode_name == "AUTO"
            and auto_limit.get("enabled") is True
            and auto_limit.get("release") is not True
        ):
            return (
                "AUTO_CHARGE_BLOCK"
                if _safe_int(auto_limit.get("max_charge_w"), -1) == 0
                else "AUTO_CHARGE_LIMIT"
            )
        return "HOLD"
    if target == "FORCE_EXPORT" or "direct_marketing" in state or "direct_marketing" in priority:
        return "ECONOMIC_EXPORT"
    if mode_name == "CHARGE" or mode_name == "CHRG":
        return "GRID_CHARGE" if "grid" in state or "market" in priority else "PV_STORE"
    if mode_name in {"DISCHARGE", "DISCH"} and value_w > 0:
        return "HOUSE_SUPPLY"
    if (
        mode_name == "AUTO"
        and auto_limit.get("enabled") is True
        and auto_limit.get("release") is not True
    ):
        return (
            "AUTO_CHARGE_BLOCK"
            if _safe_int(auto_limit.get("max_charge_w"), -1) == 0
            else "AUTO_CHARGE_LIMIT"
        )
    if state == "parallel_curve_charge":
        return "CURVE_CHARGE"
    return "HOLD"


def _runtime_storage_action(value: Any) -> Optional[str]:
    """Normalisiert Manageraktionen auf den gemeinsamen Speichervertrag."""

    action = str(value or "").strip().upper()
    if not action:
        return None
    if action in {
        "DIRECT_MARKETING_CHARGE_BLOCK_WAIT",
        "DIRECT_MARKETING_CHARGE_BLOCK_WAIT_SAFE_FALLBACK",
    }:
        action = "CHARGE_BLOCK_WAIT"
    return action if storage_action_contract(action) is not None else None


def _effective_storage_plan_projection(
    plan: Dict[str, Any],
    slot: Dict[str, Any],
    payload: Dict[str, Any],
    runtime: Dict[str, Any],
) -> Dict[str, Any]:
    """Projiziert Phase 5 für Diagnose/UI, ohne einen Ausgang zu erzeugen."""

    projection = slot.get("projection") if isinstance(slot.get("projection"), dict) else {}
    phase5 = payload.get("storage_dispatch_phase5") if isinstance(payload.get("storage_dispatch_phase5"), dict) else {}
    candidate = phase5.get("candidate") if isinstance(phase5.get("candidate"), dict) else {}
    phase5_lifecycle = phase5.get("request_lifecycle") if isinstance(phase5.get("request_lifecycle"), dict) else {}
    direct = plan.get("direct_marketing") if isinstance(plan.get("direct_marketing"), dict) else {}
    passive_binding = projection.get(DIRECT_MARKETING_PASSIVE_NORMAL_BINDING_SCHEMA) if isinstance(projection.get(DIRECT_MARKETING_PASSIVE_NORMAL_BINDING_SCHEMA), dict) else {}
    passive_curve = phase5.get("passive_normal_curve_charge") if isinstance(phase5.get("passive_normal_curve_charge"), dict) else {}
    guard = phase5.get("charge_block_contract") if isinstance(phase5.get("charge_block_contract"), dict) else {}
    guard_window = guard.get("source_window") if isinstance(guard.get("source_window"), dict) else {}
    lifecycle_source = runtime.get("requested") if isinstance(runtime.get("requested"), dict) else {}
    lifecycle = {
        "selected": bool(runtime.get("selected")),
        "executable": bool(runtime.get("executable")),
        "commands_allowed": bool(runtime.get("commands_allowed")),
        "requested": bool(lifecycle_source.get("requested")),
        "attempted": bool(lifecycle_source.get("attempted")),
        "issued": bool(lifecycle_source.get("issued")),
        "confirmed": bool(lifecycle_source.get("confirmed")),
        "hardware_effect": bool(lifecycle_source.get("hardware_effect")),
        "retained": bool(phase5_lifecycle.get("retained")),
        "retained_effect": bool(phase5_lifecycle.get("retained_effect")),
    }
    direct_active = bool(
        direct.get("active") is True and direct.get("shadow") is not True
        and _normalized_direct_marketing_mode(direct.get("mode")) in {"eco", "eco_plus", "arbitrage"}
    )
    phase5_action_raw = str(phase5.get("selected_action") or "").strip().upper()
    runtime_action_raw = phase5_action_raw or str(runtime.get("effective_action") or "").strip().upper()
    action = _runtime_storage_action(runtime_action_raw)
    unknown_runtime_action = bool(
        runtime_action_raw and action is None and runtime_action_raw != "PASSIVE_NORMAL"
    )
    if action is None and not unknown_runtime_action and direct_active and passive_binding:
        action = "PASSIVE_NORMAL"
    known = {"CHARGE_BLOCK_WAIT", "PV_STORE", "ECONOMIC_EXPORT", "HEADROOM_EXPORT", "PASSIVE_NORMAL"}

    projected_action = _runtime_storage_action(projection.get("direct_marketing_plan_action"))
    candidate_action = _runtime_storage_action(candidate.get("action"))
    action_id = window_id = segment_id = None
    identity_source = None
    if action == projected_action:
        action_id = projection.get("direct_marketing_plan_action_id")
        window_id = projection.get("direct_marketing_window_id")
        segment_id = projection.get("direct_marketing_plan_segment_id")
        identity_source = "canonical_slot_projection"
    elif action == candidate_action:
        action_id = candidate.get("action_id")
        window_id = candidate.get("window_id")
        segment_id = candidate.get("segment_id")
        identity_source = "phase5_candidate"
    elif action == "PASSIVE_NORMAL":
        action_id = passive_binding.get("policy_action_id")
        window_id = passive_binding.get("window_id")
        segment_id = passive_binding.get("policy_slot_id")
        identity_source = "passive_normal_binding"

    start_ms = _safe_int(projection.get("direct_marketing_window_start_ts_ms"), _safe_int(guard_window.get("start_ts_ms"), _safe_int(slot.get("start_ts_ms"), 0)))
    end_ms = _safe_int(projection.get("direct_marketing_window_end_ts_ms"), _safe_int(guard_window.get("end_ts_ms"), _safe_int(slot.get("end_ts_ms"), 0)))
    guard_bound = bool(
        action == "CHARGE_BLOCK_WAIT"
        and guard.get("valid") is True
        and guard.get("plan_id") == runtime.get("plan_id")
        and guard.get("slot_id") == runtime.get("slot_id")
    )
    if guard_bound and not all(isinstance(value, str) and value for value in (action_id, window_id, segment_id)):
        window_id = revision_hash({"plan_id": runtime.get("plan_id"), "start": start_ms, "end": end_ms})
        segment_id = revision_hash({"window_id": window_id, "slot_id": runtime.get("slot_id")})
        action_id = revision_hash({"action": action, "window_id": window_id, "segment_id": segment_id})
        identity_source = "phase5_charge_guard"

    passive_effect = bool(
        action == "PASSIVE_NORMAL"
        and passive_curve.get("valid") is True
        and passive_curve.get("plan_id") == runtime.get("plan_id")
        and passive_curve.get("slot_id") == runtime.get("slot_id")
        and not any(lifecycle.values())
    )
    active_effect = bool(
        action in known - {"PASSIVE_NORMAL"} and lifecycle["requested"] and lifecycle["hardware_effect"]
        and (lifecycle["issued"] or lifecycle["retained"] or lifecycle["retained_effect"])
        and (lifecycle["confirmed"] or action in {"ECONOMIC_EXPORT", "HEADROOM_EXPORT"})
    )
    lifecycle["effect_confirmed"] = bool(active_effect or passive_effect)
    runtime_ts_ms = _safe_int(runtime.get("runtime_generated_at_ts_ms"), 0)
    slot_start_ms = _safe_int(slot.get("start_ts_ms"), 0)
    slot_end_ms = _safe_int(slot.get("end_ts_ms"), 0)
    identity_complete = bool(
        _sha256_revision_valid(runtime.get("plan_id"))
        and _sha256_revision_valid(runtime.get("slot_id"))
        and _sha256_revision_valid(action_id)
        and isinstance(window_id, str)
        and bool(window_id)
        and isinstance(segment_id, str)
        and bool(segment_id)
        and start_ms > 0
        and end_ms > start_ms
        and slot_start_ms > 0
        and slot_end_ms > slot_start_ms
        and slot_start_ms <= runtime_ts_ms < slot_end_ms
        and start_ms <= runtime_ts_ms < end_ms
    )

    consistent = bool(runtime.get("plan_valid"))
    reason = "ok"
    status = "CLASSICAL_CURVE_EFFECTIVE"
    if direct_active:
        if unknown_runtime_action or action not in known:
            consistent, reason = False, "DIRECT_MARKETING_ACTION_UNKNOWN"
        elif not identity_complete:
            consistent, reason = False, "DIRECT_MARKETING_IDENTITY_INCOMPLETE"
        elif action == "PASSIVE_NORMAL":
            consistent = bool(consistent and passive_effect)
            reason = "ok" if consistent else "PASSIVE_NORMAL_EFFECT_UNCONFIRMED"
            status = "DIRECT_MARKETING_PASSIVE_NORMAL_EFFECTIVE"
        elif not bool(lifecycle["selected"] and lifecycle["executable"] and lifecycle["commands_allowed"]):
            consistent, reason = False, "ACTIVE_SELECTION_INCOMPLETE"
        elif lifecycle["hardware_effect"] and not bool(
            lifecycle["requested"] and (lifecycle["issued"] or lifecycle["retained"] or lifecycle["retained_effect"])
        ):
            consistent, reason = False, "LIFECYCLE_EFFECT_WITHOUT_ISSUE"
        elif action == "CHARGE_BLOCK_WAIT" and _safe_int(runtime.get("charge_budget_w"), 0) != 0:
            consistent, reason = False, "CHARGE_BLOCK_NONZERO_BUDGET"
        elif active_effect and action == "PV_STORE" and _safe_int(runtime.get("charge_budget_w"), 0) <= 0:
            consistent, reason = False, "PV_STORE_WITHOUT_POSITIVE_BUDGET"
        elif active_effect and action in {"ECONOMIC_EXPORT", "HEADROOM_EXPORT"} and _safe_int(runtime.get("export_budget_w"), 0) <= 0:
            consistent, reason = False, "EXPORT_WITHOUT_POSITIVE_BUDGET"
        else:
            status = "DIRECT_MARKETING_%s_%s" % (action, "EFFECTIVE" if active_effect else "PENDING")
    if not consistent:
        status = "EVIDENCE_LIMIT"

    curve_authorized = status in {"CLASSICAL_CURVE_EFFECTIVE", "DIRECT_MARKETING_PV_STORE_EFFECTIVE", "DIRECT_MARKETING_PASSIVE_NORMAL_EFFECTIVE"}
    target_authorized: Optional[bool] = True if curve_authorized else False if status.endswith("_EFFECTIVE") else None
    curve_w: Optional[int] = None
    if curve_authorized:
        curve_w = (
            max(0, _safe_int(passive_curve.get("preserved_charge_w"), 0))
            if action == "PASSIVE_NORMAL"
            else max(0, _safe_int(runtime.get("charge_budget_w"), 0))
            if action == "PV_STORE"
            else max(0, _safe_int(payload.get("iFc_w"), 0))
        )
    elif status.endswith("_EFFECTIVE"):
        curve_w = 0
    effective_power_w = max(0, _safe_int(runtime.get("export_budget_w"), 0)) \
        if status.endswith("_EFFECTIVE") and action in {"ECONOMIC_EXPORT", "HEADROOM_EXPORT"} else curve_w

    binding = {
        "plan_id": runtime.get("plan_id"),
        "slot_id": runtime.get("slot_id"),
        "action": action,
        "action_id": action_id,
        "window_id": window_id,
        "segment_id": segment_id,
        "window_start_ts_ms": start_ms,
        "window_end_ts_ms": end_ms,
        "owner": runtime.get("owner"),
        "runtime_generated_at_ts_ms": runtime.get("runtime_generated_at_ts_ms"),
        "slot_start_ts_ms": slot_start_ms,
        "slot_end_ts_ms": slot_end_ms,
        "identity_source": identity_source,
    }
    values = {
        "target_soc": plan.get("target_soc"),
        "planning_target_soc": plan.get("planning_target_soc"),
        "effective_target_soc": plan.get("effective_target_soc"),
        "current_curve_soc": payload.get("curve_soc"),
        "next_curve_soc": payload.get("target_soc"),
        "next_curve_ts": payload.get("target_ts"),
        "can_reach_target": plan.get("can_reach_target"),
        "max_reachable_soc": plan.get("max_reachable_soc"),
        "sim_max_soc_pct": plan.get("max_soc_pct"),
    }
    if not curve_authorized:
        values = {key: None for key in values}
    result = {
        "schema_version": EFFECTIVE_STORAGE_PLAN_SCHEMA,
        "status": status,
        "consistent": consistent,
        "consistency_reason": reason,
        "direct_marketing_active": direct_active,
        "binding": binding,
        "lifecycle": lifecycle,
        "effective_action": action,
        "target_projection_authorized": target_authorized,
        "clear_classical_curves": bool(direct_active and target_authorized is not True),
        "classical_curve_role": (
            "AUTHORIZED" if curve_authorized else "BLOCKED" if target_authorized is False else "EVIDENCE_LIMIT"
        ),
        "effective_power_w": effective_power_w,
        "effective_charge_w": curve_w,
        "target_reach_state": plan.get("target_reach_state") if curve_authorized else "direct_marketing",
        "target_reach_reason": (
            plan.get("target_reach_reason")
            if curve_authorized
            else "DV führt den Slot; klassische Zielkurve und Ladeleistung sind nicht wirksam bestätigt."
        ),
        "reserve_forecast": {
            "physical_reserve_soc": plan.get("physical_reserve_soc"),
            "morning_target": plan.get("morning_target"),
            "weather_reserve_active": plan.get("weather_reserve_active"),
            "weather_reserve_need_wh": plan.get("weather_reserve_need_wh"),
            "target_reach_forecast_fresh": plan.get("target_reach_forecast_fresh"),
        },
        "classical_curve": {
            "charge_w": payload.get("iFc_w"),
            "target_soc": plan.get("target_soc"),
            "can_reach_target": plan.get("can_reach_target"),
        },
        **values,
    }
    result["revision"] = revision_hash(result)
    return result


def _runtime_exact_zero(value: Any) -> bool:
    """Akzeptiert nur eine echte endliche numerische 0, keine Bool-/Textwerte."""

    return bool(
        type(value) in {int, float}
        and math.isfinite(float(value))
        and float(value) == 0.0
    )


def _runtime_nonnegative_number(value: Any) -> bool:
    """Akzeptiert nur endliche numerische, nichtnegative Runtimewerte."""

    return bool(
        type(value) in {int, float}
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def _default_charge_guard_runtime_safety_claim(
    plan: Dict[str, Any],
    slot: Dict[str, Any],
    phase5: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    plan_id: Optional[str],
    slot_id: Optional[str],
) -> Dict[str, Any]:
    """Bindet ausschließlich den fail-closed Default-Ladeblock.

    Der Plankandidat bleibt rein diagnostisch. Der Storage Manager darf im
    aktiven DV-Slot den separat typisierten 0-W-Ladeblock ausführen, auch wenn
    der passive Planslot bewusst keinen Kandidaten besitzt. Autoritativ sind
    ausschließlich Plan/Slot, DV-Owner und der tatsächlich übersetzte sichere
    Ausgang. Eine zusätzliche Kandidatenform darf diese Entscheidung nicht
    nachträglich aushebeln.
    """

    projection = (
        slot.get("projection")
        if isinstance(slot.get("projection"), dict)
        else {}
    )
    direct = (
        plan.get("direct_marketing")
        if isinstance(plan.get("direct_marketing"), dict)
        else {}
    )
    direct_flags = (
        direct.get("flags")
        if isinstance(direct.get("flags"), dict)
        else {}
    )
    direct_mode = _normalized_direct_marketing_mode(direct.get("mode"))
    roles = (
        projection.get("direct_marketing_action_roles")
        if isinstance(projection.get("direct_marketing_action_roles"), dict)
        else {}
    )
    guard = (
        phase5.get("charge_block_contract")
        if isinstance(phase5.get("charge_block_contract"), dict)
        else {}
    )
    mirrored_guard = phase5.get("direct_marketing_default_charge_guard")
    source_window = (
        guard.get("source_window")
        if isinstance(guard.get("source_window"), dict)
        else {}
    )
    intent = (
        phase5.get("execution_intent")
        if isinstance(phase5.get("execution_intent"), dict)
        else {}
    )
    translation = (
        phase5.get("translation")
        if isinstance(phase5.get("translation"), dict)
        else {}
    )
    auto_limit = (
        payload.get("auto_limit")
        if isinstance(payload.get("auto_limit"), dict)
        else {}
    )
    selected_source = str(phase5.get("selected_source") or "")
    selected_action = str(phase5.get("selected_action") or "").upper()
    role_candidate_action = _runtime_storage_action(
        roles.get("candidate_action")
    )
    applicable = bool(
        selected_source
        == "canonical_phase5_direct_marketing_default_charge_guard"
        or guard.get("schema")
        == "phase5_direct_marketing_default_charge_guard_v1"
        or isinstance(mirrored_guard, dict)
    )
    if not applicable:
        return {
            "applicable": False,
            "valid": False,
            "claim_type": None,
            "action": None,
            "reason_code": None,
        }

    translation_max_discharge_w = _safe_float(
        translation.get("max_discharge_w"),
        None,
    )
    auto_max_discharge_w = _safe_float(
        auto_limit.get("max_discharge_w"),
        None,
    )
    valid = bool(
        phase5.get("schema_version") == "storage_dispatch_phase5_v1"
        and isinstance(plan_id, str)
        and bool(plan_id)
        and phase5.get("plan_id") == plan_id
        and isinstance(slot_id, str)
        and bool(slot_id)
        and phase5.get("slot_id") == slot_id
        and slot.get("slot_id") == slot_id
        and direct_mode in {"eco", "eco_plus", "arbitrage"}
        and direct.get("controller_owner") == "storage_manager"
        and direct.get("plan_owner") == "direct_marketing:%s" % direct_mode
        and type(direct.get("owner_contract_version")) is int
        and direct.get("owner_contract_version") == 1
        and type(direct_flags.get("owner_contract_version")) is int
        and direct_flags.get("owner_contract_version") == 1
        and roles.get("schema_version")
        == DIRECT_MARKETING_ACTION_ROLES_SCHEMA
        and roles.get("status") == "CONSISTENT"
        and roles.get("plan_selected_action") is None
        and roles.get("plan_executable_action") is None
        and roles.get("effective_action") is None
        and roles.get("runtime_effect_claim_allowed") is False
        and phase5.get("decision_available") is True
        and phase5.get("selected") is True
        and phase5.get("executable") is True
        and phase5.get("commands_allowed") is True
        and selected_source
        == "canonical_phase5_direct_marketing_default_charge_guard"
        and selected_action == "DIRECT_MARKETING_CHARGE_BLOCK_WAIT"
        and phase5.get("selection_class") == "default_charge_guard"
        and _runtime_exact_zero(phase5.get("selected_power_w"))
        and guard.get("schema")
        == "phase5_direct_marketing_default_charge_guard_v1"
        and guard.get("valid") is True
        and guard.get("reason")
        == "active_direct_marketing_slot_without_authorized_pv_store"
        and guard.get("charge_authorized") is False
        and guard.get("plan_id") == plan_id
        and guard.get("slot_id") == slot_id
        and (
            mirrored_guard is None
            or (
                isinstance(mirrored_guard, dict)
                and mirrored_guard == guard
            )
        )
        and source_window.get("slot_id") == slot_id
        and type(source_window.get("start_ts_ms")) is int
        and source_window.get("start_ts_ms") == slot.get("start_ts_ms")
        and type(source_window.get("end_ts_ms")) is int
        and source_window.get("end_ts_ms") == slot.get("end_ts_ms")
        and intent.get("class") == "authorized_charge_block"
        and intent.get("authorized") is True
        and str(intent.get("action") or "").upper()
        == "DIRECT_MARKETING_CHARGE_BLOCK_WAIT"
        and _runtime_exact_zero(intent.get("power_w"))
        and intent.get("owner") == "direct_marketing"
        and str(translation.get("action") or "").upper()
        == "DIRECT_MARKETING_CHARGE_BLOCK_WAIT"
        and _runtime_exact_zero(translation.get("requested_power_w"))
        and _runtime_exact_zero(translation.get("translated_power_w"))
        and translation.get("power_settings_only") is True
        and translation.get("power_limits_used") is True
        and _runtime_exact_zero(translation.get("max_charge_w"))
        and _runtime_exact_zero(translation.get("requested_charge_cap_w"))
        and _runtime_nonnegative_number(
            translation.get("max_discharge_w")
        )
        and translation_max_discharge_w is not None
        and type(phase5.get("translated_mode")) is int
        and phase5.get("translated_mode") == 0
        and str(phase5.get("translated_mode_name") or "").upper()
        == "AUTO"
        and _runtime_exact_zero(phase5.get("translated_power_w"))
        and str(phase5.get("translated_state") or "")
        == "direct_marketing_charge_block_wait"
        and str(payload.get("state") or "")
        == "direct_marketing_charge_block_wait"
        and type(payload.get("mode")) is int
        and payload.get("mode") == 0
        and str(payload.get("mode_name") or "").upper() == "AUTO"
        and _runtime_exact_zero(payload.get("val"))
        and not payload.get("safe_start")
        and not payload.get("live_stale")
        and payload.get("live_sample_valid") is True
        and payload.get("grid_power_valid") is True
        and not payload.get("ems_budget_runtime_veto")
        and payload.get("direct_marketing_active") is True
        and str(payload.get("priority") or "").lower()
        == "direct_marketing"
        and str(payload.get("direct_marketing_action") or "").upper()
        == "DIRECT_MARKETING_CHARGE_BLOCK_WAIT"
        and str(payload.get("direct_marketing_target_state") or "").upper()
        == "CHARGE_BLOCK_WAIT"
        and payload.get("storage_dispatch_selected_plan_id") == plan_id
        and payload.get("storage_dispatch_selected_slot_id") == slot_id
        and auto_limit.get("enabled") is True
        and auto_limit.get("release") is False
        and auto_limit.get("power_limits_used") is True
        and _runtime_exact_zero(auto_limit.get("max_charge_w"))
        and _runtime_exact_zero(auto_limit.get("requested_charge_cap_w"))
        and _runtime_nonnegative_number(auto_limit.get("max_discharge_w"))
        and auto_max_discharge_w is not None
        and auto_max_discharge_w == translation_max_discharge_w
    )
    return {
        "applicable": True,
        "valid": valid,
        "claim_type": "default_charge_guard",
        "action": "CHARGE_BLOCK_WAIT" if valid else None,
        "plan_candidate_action": role_candidate_action,
        "plan_candidate_only": roles.get("candidate_only") is True,
        "candidate_role": "diagnostic_only",
        "reason_code": (
            None
            if valid
            else "DIRECT_MARKETING_CHARGE_GUARD_RUNTIME_BINDING_INVALID"
        ),
    }


def _direct_marketing_runtime_plan_binding(
    plan: Dict[str, Any],
    slot: Dict[str, Any],
    phase5: Dict[str, Any],
    payload: Dict[str, Any],
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
        action in {"ECONOMIC_EXPORT", "PV_STORE", "GRID_CHARGE", "CHARGE_BLOCK_WAIT"}
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
        "CHARGE_BLOCK_WAIT": "direct_marketing_charge_block_wait",
    }.get(action)
    source_mode_valid = bool(
        (action == "ECONOMIC_EXPORT" and source_mode == "eco_plus")
        or (action == "PV_STORE" and source_mode in {"eco", "eco_plus"})
        or (action == "CHARGE_BLOCK_WAIT" and source_mode == "eco_plus")
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
        and (planned_w >= 300.0 if action != "CHARGE_BLOCK_WAIT" else planned_w == 0.0)
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
    projected_live_dc_fallback = (
        projection.get(
            "direct_marketing_pv_store_live_dc_fallback_contract"
        )
        if isinstance(
            projection.get(
                "direct_marketing_pv_store_live_dc_fallback_contract"
            ),
            dict,
        )
        else {}
    )
    candidate_live_dc_fallback = (
        candidate.get("pv_store_live_dc_fallback_contract")
        if isinstance(candidate.get("pv_store_live_dc_fallback_contract"), dict)
        else {}
    )
    generation_match = bool(
        isinstance(plan_id, str)
        and bool(plan_id)
        and phase5.get("plan_id") == plan_id
        and isinstance(slot_id, str)
        and bool(slot_id)
        and phase5.get("slot_id") == slot_id
        and slot.get("slot_id") == slot_id
    )
    exact_candidate_identity = bool(
        plan_selected
        and generation_match
        and candidate_window_id == window_id
        and candidate_source_action == source_action
        and candidate_source_mode == source_mode
        and candidate_action_id == action_id
        and candidate_segment_id == segment_id
        and candidate_power_w is not None
    )
    exact_planned_candidate = bool(
        exact_candidate_identity
        and abs(candidate_power_w - planned_w) <= 1.0
    )
    projected_runtime_cap_w = _safe_float(
        projected_live_dc_fallback.get("runtime_cap_w"),
        None,
    )
    candidate_runtime_cap_w = _safe_float(
        candidate_live_dc_fallback.get("runtime_cap_w"),
        None,
    )
    candidate_hardware_cap_w = _safe_float(
        candidate_live_dc_fallback.get("hardware_cap_w"),
        None,
    )
    candidate_charge_w = _safe_float(
        candidate_live_dc_fallback.get("charge_w"),
        None,
    )
    projected_window_start_ms = _safe_int(
        projection.get("direct_marketing_window_start_ts_ms"),
        0,
    )
    projected_window_end_ms = _safe_int(
        projection.get("direct_marketing_window_end_ts_ms"),
        0,
    )
    exact_pv_store_v2_runtime_candidate = bool(
        exact_candidate_identity
        and action == "PV_STORE"
        and projected_live_dc_fallback.get("schema_version")
        == "direct_marketing_pv_store_auto_dc_permission_v2"
        and projected_live_dc_fallback.get("valid") is True
        and projected_live_dc_fallback.get("execution_semantics")
        == "PV_STORE_E3DC_AUTO_DC_PERMISSION"
        and projected_live_dc_fallback.get("action") == "PV_STORE"
        and projected_live_dc_fallback.get("source_action") == source_action
        and projected_live_dc_fallback.get("source_mode") == source_mode
        and projected_live_dc_fallback.get("action_id") == action_id
        and projected_live_dc_fallback.get("window_id") == window_id
        and projected_live_dc_fallback.get("market_window_id") == window_id
        and 0 < projected_window_start_ms < projected_window_end_ms
        and _safe_int(
            projected_live_dc_fallback.get("window_start_ts_ms"),
            0,
        )
        == projected_window_start_ms
        and _safe_int(
            projected_live_dc_fallback.get("window_end_ts_ms"),
            0,
        )
        == projected_window_end_ms
        and _safe_int(
            projected_live_dc_fallback.get("market_window_start_ts_ms"),
            0,
        )
        <= projected_window_start_ms
        and projected_window_end_ms
        <= _safe_int(
            projected_live_dc_fallback.get("market_window_end_ts_ms"),
            0,
        )
        and projected_live_dc_fallback.get("dc_only") is True
        and projected_live_dc_fallback.get("aux_ac_allowed") is False
        and projected_live_dc_fallback.get("grid_ac_allowed") is False
        and isinstance(action_id, str)
        and action_id.startswith("sha256:")
        and len(action_id) == 71
        and candidate_live_dc_fallback.get("schema")
        == "phase5_pv_store_auto_dc_permission_v2"
        and candidate_live_dc_fallback.get("valid") is True
        and candidate_live_dc_fallback.get("plan_id") == plan_id
        and candidate_live_dc_fallback.get("slot_id") == slot_id
        and candidate_live_dc_fallback.get("action_id") == action_id
        and candidate_live_dc_fallback.get("window_id") == window_id
        and candidate_live_dc_fallback.get("segment_id") == segment_id
        and phase5.get("selected_source")
        == "canonical_phase5_pv_store_live_dc_fallback"
        and candidate.get("selection_source") == "canonical_live_dc_fallback"
        and phase5.get("pv_store_live_dc_fallback")
        == candidate_live_dc_fallback
        and candidate_live_dc_fallback.get("permission_only") is True
        and candidate_live_dc_fallback.get("grid_ac_allowed") is False
        and candidate_live_dc_fallback.get("ac_charge_commanded") is False
        and projected_runtime_cap_w is not None
        and candidate_runtime_cap_w is not None
        and candidate_hardware_cap_w is not None
        and candidate_charge_w is not None
        and candidate_power_w is not None
        and candidate_power_w >= 300.0
        and abs(candidate_runtime_cap_w - projected_runtime_cap_w) <= 1.0
        and abs(candidate_power_w - candidate_charge_w) <= 1.0
        and candidate_power_w <= candidate_runtime_cap_w
        and candidate_power_w <= candidate_hardware_cap_w
    )
    exact_candidate = bool(
        exact_planned_candidate or exact_pv_store_v2_runtime_candidate
    )
    safety_claim = _default_charge_guard_runtime_safety_claim(
        plan,
        slot,
        phase5,
        payload,
        plan_id=plan_id,
        slot_id=slot_id,
    )
    valid = bool(
        safety_claim.get("valid")
        if safety_claim.get("applicable")
        else not runtime_claim or exact_candidate
    )
    reason_code = (
        safety_claim.get("reason_code")
        if safety_claim.get("applicable")
        else None
        if valid
        else "PLAN_RUNTIME_SELECTION_INVARIANT_VIOLATION"
    )
    return {
        "valid": valid,
        "runtime_claim": runtime_claim,
        "plan_selected": plan_selected,
        "generation_match": generation_match,
        "exact_candidate": exact_candidate,
        "exact_planned_candidate": exact_planned_candidate,
        "exact_pv_store_v2_runtime_candidate": (
            exact_pv_store_v2_runtime_candidate
        ),
        "window_id": window_id if isinstance(window_id, str) else None,
        "source_action": source_action or None,
        "source_mode": source_mode or None,
        "plan_source_mode": plan_source_mode or None,
        "source_mode_matches_plan": source_mode_matches_plan,
        "planned_w": round(planned_w, 3) if planned_w is not None else None,
        "safety_claim": safety_claim,
        "reason_code": reason_code,
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
        payload,
        plan_id=validation.get("plan_id"),
        slot_id=validation.get("slot_id"),
    ) if explicit_phase5 else {
        "valid": True,
        "runtime_claim": False,
        "plan_selected": False,
        "generation_match": False,
        "exact_candidate": False,
        "safety_claim": {
            "applicable": False,
            "valid": False,
            "claim_type": None,
            "action": None,
            "reason_code": None,
        },
        "reason_code": None,
    }
    if explicit_phase5 and not direct_marketing_binding.get("valid"):
        phase5 = copy.deepcopy(phase5)
        phase5.update({
            "selected": False,
            "executable": False,
            "commands_allowed": False,
            "requested": False,
            "attempted": False,
            "acknowledged": False,
            "issued": False,
            "confirmed": False,
            "hardware_effect": False,
            "selected_action": None,
            "selected_power_w": 0.0,
            "block_reason_code": direct_marketing_binding.get("reason_code"),
            "technical_block_reason_code": direct_marketing_binding.get("reason_code"),
        })
        phase5["request_lifecycle"] = {
            "requested": False,
            "attempted": False,
            "acknowledged": False,
            "issued": False,
            "confirmed": False,
            "hardware_effect": False,
            "confirmation": None,
        }
        blockers = list(phase5.get("blockers") or [])
        if direct_marketing_binding.get("reason_code") not in blockers:
            blockers.insert(0, direct_marketing_binding.get("reason_code"))
        phase5["blockers"] = blockers
    default_charge_guard_bound = bool(
        direct_marketing_binding.get("valid") is True
        and isinstance(direct_marketing_binding.get("safety_claim"), dict)
        and direct_marketing_binding["safety_claim"].get("valid") is True
        and direct_marketing_binding["safety_claim"].get("claim_type")
        == "default_charge_guard"
    )
    evaluated_candidate: Optional[Dict[str, Any]] = None
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
        if default_charge_guard_bound:
            evaluated_candidate = copy.deepcopy(candidate)
            evaluated_candidate.update({
                "action": "GRID_CHARGE",
                "selected": False,
                "executable": False,
                "commands_allowed": False,
                "effect": None,
            })
            candidate.update({
                "action": "CHARGE_BLOCK_WAIT",
                "power_w": 0.0,
                "status": "phase5_default_charge_guard_bound",
                "candidate": True,
                "selected": True,
                "executable": True,
                "commands_allowed": True,
                "block_reason_code": None,
                "economic_export_gate": None,
                "headroom_gate": None,
            })
    else:
        candidate = slot.get("candidate") if isinstance(slot.get("candidate"), dict) else {"action": "HOLD", "power_w": 0.0, "status": "unavailable"}
    candidate_action = str(candidate.get("action") or "HOLD").upper()
    projection = (
        slot.get("projection")
        if isinstance(slot.get("projection"), dict)
        else {}
    )
    plan_action_roles = (
        projection.get("direct_marketing_action_roles")
        if isinstance(projection.get("direct_marketing_action_roles"), dict)
        else {}
    )
    candidate_role_action = (
        _runtime_storage_action(plan_action_roles.get("candidate_action"))
        or _runtime_storage_action(candidate_action)
        or "HOLD"
    )
    runtime_candidate_action = (
        "CHARGE_BLOCK_WAIT"
        if default_charge_guard_bound
        else candidate_role_action
    )
    plan_selected_action = _runtime_storage_action(
        plan_action_roles.get("plan_selected_action")
    )
    plan_executable_action = _runtime_storage_action(
        plan_action_roles.get("plan_executable_action")
    )
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

    runtime_selected_action = (
        _runtime_storage_action(phase5.get("selected_action"))
        if explicit_phase5 and selected
        else _runtime_storage_action(actual_action)
        if selected
        else None
    )
    effective_action = runtime_selected_action
    effective_contract = (
        storage_action_contract(effective_action)
        if effective_action is not None
        else None
    )
    effect = (
        effective_contract.get("effect")
        if isinstance(effective_contract, dict)
        and effective_contract.get("field_released") is True
        else None
    )
    runtime_effect_claim_allowed = bool(
        runtime_selected_action is not None
        and runtime_selected_action == effective_action
        and isinstance(effective_contract, dict)
        and effective_contract.get("field_released") is True
        and selected
        and executable
        and commands_allowed
    )
    charge_budget_w = value_w if commands_allowed and effective_action in {"PV_STORE", "GRID_CHARGE"} else 0
    export_budget_w = value_w if commands_allowed and effective_action in {"ECONOMIC_EXPORT", "HEADROOM_EXPORT"} else 0
    power_diag = payload.get("rscp_power_settings") if isinstance(payload.get("rscp_power_settings"), dict) else {}
    readback = power_diag.get("readback") if isinstance(power_diag.get("readback"), dict) else {}
    readback_ts_ms = _to_ts_ms(
        power_diag.get("readback_cycle_ts") or power_diag.get("ts")
    )
    readback_age_ms = now_value - readback_ts_ms if readback_ts_ms > 0 else None
    typed_readback = bool(
        power_diag.get("schema") == "rscp_power_settings_v1"
        and _safe_int(power_diag.get("contract_version"), 0) == 2
        and isinstance(readback.get("limits_used"), bool)
        and all(
            isinstance(readback.get(key), int)
            and not isinstance(readback.get(key), bool)
            and readback.get(key) >= 0
            for key in (
                "max_charge_w",
                "max_discharge_w",
                "discharge_start_w",
            )
        )
    )
    readback_evidence_source = bool(
        power_diag.get("readback_source") in {
            "canonical_live",
            "command_get_after_invalid_set_response",
            "command_verification",
        }
        or power_diag.get("stage") in {"live_reconciliation", "target"}
    )
    readback_fresh = bool(
        typed_readback
        and readback_evidence_source
        and readback_age_ms is not None
        and -5_000 <= readback_age_ms <= 30_000
    )
    if explicit_phase5:
        acknowledged = phase5.get("acknowledged")
        historical_confirmation = bool(phase5.get("confirmed"))
    elif "acknowledged" in power_diag:
        acknowledged = power_diag.get("acknowledged") is True
        historical_confirmation = bool(power_diag.get("confirmed"))
    else:
        acknowledged = bool(power_diag.get("response_codes") is not None or power_diag.get("confirmed"))
        historical_confirmation = bool(power_diag.get("confirmed"))
    charge_block_lifecycle = bool(
        explicit_phase5
        and str(phase5.get("selected_action") or "").upper() in {
            "DIRECT_MARKETING_CHARGE_BLOCK_WAIT",
            "DIRECT_MARKETING_CHARGE_BLOCK_WAIT_SAFE_FALLBACK",
        }
    )
    confirmed = bool(
        historical_confirmation
        and (readback_fresh if charge_block_lifecycle else True)
    )
    if charge_block_lifecycle and not readback_fresh:
        phase5 = copy.deepcopy(phase5)
        phase5["confirmed"] = False
        phase5["hardware_effect"] = False
        lifecycle = (
            copy.deepcopy(phase5.get("request_lifecycle"))
            if isinstance(phase5.get("request_lifecycle"), dict)
            else {}
        )
        lifecycle.update({
            "confirmed": False,
            "hardware_effect": False,
            "historical_confirmation": historical_confirmation,
            "confirmation_fresh": False,
        })
        phase5["request_lifecycle"] = lifecycle
    requested = {
        "mode": mode_name,
        "mode_value": _safe_int(payload.get("mode"), 0),
        "power_w": value_w,
        "rscp_path": payload.get("rscp_command_path"),
        "issued_by": "storage_manager",
        "requested": bool(phase5.get("requested")) if explicit_phase5 else bool(selected and executable),
        "attempted": bool(phase5.get("attempted")) if explicit_phase5 else bool(selected and executable),
        "acknowledged": acknowledged,
        "issued": bool(phase5.get("issued")) if explicit_phase5 else bool(selected and executable),
        "confirmed": confirmed,
        "historical_confirmation": historical_confirmation,
        "hardware_effect": (
            bool(phase5.get("hardware_effect") and confirmed)
            if charge_block_lifecycle
            else bool(phase5.get("hardware_effect"))
            if explicit_phase5
            else bool(selected and executable)
        ),
        "dispatch_authorized": bool(selected and executable and commands_allowed),
    }
    action_roles = {
        "schema_version": DIRECT_MARKETING_RUNTIME_ACTION_ROLES_SCHEMA,
        "plan_schema_version": plan_action_roles.get("schema_version"),
        "candidate_action": candidate_role_action,
        "candidate_only": bool(
            plan_action_roles.get("candidate_only") is True
            or (
                candidate_role_action not in {None, "HOLD"}
                and plan_executable_action is None
            )
        ),
        "plan_selected_action": plan_selected_action,
        "plan_executable_action": plan_executable_action,
        "runtime_selected_action": runtime_selected_action,
        "effective_action": effective_action,
        "effect": effect,
        "runtime_effect_claim_allowed": runtime_effect_claim_allowed,
        "hardware_effect": bool(requested.get("hardware_effect")),
        "slot_start_ts_ms": _safe_int(slot.get("start_ts_ms"), 0),
        "slot_end_ts_ms": _safe_int(slot.get("end_ts_ms"), 0),
    }
    candidate_projection = {
        "action": runtime_candidate_action,
        "power_w": _round_or_none(candidate.get("power_w"), 3) or 0.0,
        "status": candidate.get("status"),
        "candidate": bool(candidate.get("candidate", candidate_action != "HOLD")),
        "selected": selected,
        "executable": executable,
        "commands_allowed": commands_allowed,
        "block_reason_code": candidate.get("block_reason_code") or block_reason,
        "economic_export_gate": copy.deepcopy(candidate.get("economic_export_gate")),
        "headroom_gate": copy.deepcopy(candidate.get("headroom_gate")),
    }
    if default_charge_guard_bound:
        candidate_projection["effect"] = (
            effect if runtime_effect_claim_allowed else None
        )
    runtime = {
        "schema_version": RUNTIME_SCHEMA,
        "runtime_generated_at": _utc_iso(now_value),
        "runtime_generated_at_ts_ms": now_value,
        "plan_id": validation.get("plan_id"),
        "slot_id": validation.get("slot_id"),
        "plan_valid": bool(validation.get("valid")),
        "plan_age_s": validation.get("age_s"),
        "candidate": candidate_projection,
        **({
            "evaluated_candidate": copy.deepcopy(evaluated_candidate),
            "selected_action": (
                effective_action if runtime_effect_claim_allowed else None
            ),
        } if default_charge_guard_bound else {}),
        "actual_manager_action": actual_action,
        "action_roles": action_roles,
        "effective_action": effective_action,
        "effect": effect,
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
            "dispatch_acknowledged": bool(
                explicit_phase5
                and phase5.get("acknowledged") is True
                and phase5.get("issued") is True
            ),
            "settings_acknowledged": acknowledged,
            "acknowledgement_status": power_diag.get("acknowledgement_status"),
            "scope": "POWER_SETTINGS_ONLY_NO_SET_POWER_ACK",
            "status": power_diag.get("status"),
            "response_codes": power_diag.get("response_codes"),
            "ts_ms": _to_ts_ms(power_diag.get("ts")) or None,
        },
        "readback": {
            "confirmed": bool(power_diag.get("confirmed") and readback_fresh),
            "historical_confirmation": bool(power_diag.get("confirmed")),
            "fresh": readback_fresh,
            "age_s": (
                round(readback_age_ms / 1000.0, 3)
                if readback_age_ms is not None
                else None
            ),
            "status": power_diag.get("status"),
            "values": readback or None,
            "ts_ms": readback_ts_ms or None,
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
    runtime["effective_storage_plan"] = _effective_storage_plan_projection(
        plan,
        slot,
        payload,
        runtime,
    )
    return runtime


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
