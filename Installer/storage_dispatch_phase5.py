#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed Phase-5-Vertrag für den kanonischen Storage-Dispatch.

Das Modul bewertet ausschließlich Plan, Livezustand und Managerbaseline. Es
sendet keine RSCP-, Shelly-, Luox- oder sonstigen Hardwarebefehle.
"""

from __future__ import annotations

import copy
import math
import re
import time
from typing import Any, Dict, Iterable, List, Optional

try:
    from Installer.storage_dispatch_contract import validate_canonical_plan
except ModuleNotFoundError:  # pragma: no cover - direkter Installer-Start
    from storage_dispatch_contract import validate_canonical_plan  # type: ignore


PHASE5_SCHEMA = "storage_dispatch_phase5_v1"
SHADOW_GATE_NAME = "SHADOW_60_GATE"
FIELD_MODE = "field_active"
SHADOW_MODE = "shadow"
DISABLED_MODE = "disabled"
VALID_ACTIONS = {
    "HOLD",
    "PV_STORE",
    "GRID_CHARGE",
    "HOUSE_SUPPLY",
    "ECONOMIC_EXPORT",
    "HEADROOM_EXPORT",
}
CHARGE_ACTIONS = {"PV_STORE", "GRID_CHARGE"}
DISCHARGE_ACTIONS = {"HOUSE_SUPPLY", "ECONOMIC_EXPORT", "HEADROOM_EXPORT"}
REVISION_KEYS = {
    "price",
    "pv_ensemble",
    "load_ensemble",
    "state",
    "hardware_limits",
    "config",
    "policy",
}
DIGEST_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ACTIVATION_TRANSACTION_KEYS = {
    "storage_dispatch_runtime_mode",
    "storage_dispatch_activation_gate",
    "storage_dispatch_activation_evidence_sha256",
    "storage_dispatch_activation_candidate_fingerprint",
}
POWER_SETTINGS_CONFIRMED_STATUSES = frozenset({
    "confirmed",
    "confirmed_bounded_zero",
    "confirmed_from_live_readback",
    "confirmed_nonoptimal",
    "confirmed_unchanged",
})
POWER_SETTINGS_LIVE_READBACK_STATUS = "confirmed_from_live_readback"
POWER_SETTINGS_SCHEMA = "rscp_power_settings_v1"
POWER_SETTINGS_CONTRACT_VERSION = 2
SHADOW_INPUT_BINDING_SCHEMA = "storage_dispatch_shadow_input_binding_v2"
PRICE_HORIZON_SCHEMA = "storage_dispatch_price_horizon_v2"
ACTION_HORIZON_SCHEMA = "storage_dispatch_action_horizon_v1"
MARKET_TIMEZONE = "Europe/Berlin"
SLOT_DURATION_MS = 900_000
ECONOMIC_HOLD_THRESHOLD_BLOCKERS = frozenset({
    "ECONOMIC_EXPORT_MARGIN_BELOW_USER_MINIMUM",
    "ECONOMIC_EXPORT_WINDOW_PROFIT_BELOW_USER_MINIMUM",
})
ECONOMIC_HOLD_ALLOWED_BLOCKERS = frozenset({
    *ECONOMIC_HOLD_THRESHOLD_BLOCKERS,
    "ECONOMIC_EXPORT_POLICY_NOT_EXECUTABLE",
})


def _float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    number = _float(value, None)
    return int(round(number)) if number is not None else int(default)


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "ja"}:
        return True
    if text in {"0", "false", "no", "off", "nein"}:
        return False
    return default


def _normalized_direct_marketing_mode(value: Any) -> str:
    """Normalisiert den im Gesamtplan gebundenen DV-Modus."""

    mode = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if mode in {"eco+", "ecoplus"}:
        return "eco_plus"
    return mode


def action_direction(action: Any) -> str:
    value = str(action or "HOLD").upper()
    if value in CHARGE_ACTIONS:
        return "charge"
    if value in DISCHARGE_ACTIONS:
        return "discharge"
    return "hold"


def _direction_reversals(directions: Iterable[Any]) -> int:
    previous: Optional[str] = None
    reversals = 0
    for value in directions:
        direction = str(value or "hold")
        if direction not in {"charge", "discharge"}:
            continue
        if previous in {"charge", "discharge"} and previous != direction:
            reversals += 1
        previous = direction
    return reversals


def _owner_switches(owners: Iterable[Any]) -> int:
    previous: Optional[str] = None
    switches = 0
    for value in owners:
        owner = str(value or "").strip()
        if not owner:
            continue
        if previous is not None and previous != owner:
            switches += 1
        previous = owner
    return switches


def _immediate_direction_reversals(
    rows: Iterable[Dict[str, Any]],
    directions: Iterable[Any],
    *,
    hold_s: float = 45.0,
) -> int:
    previous: Optional[str] = None
    previous_ts_ms: Optional[int] = None
    count = 0
    for row, value in zip(rows, directions):
        direction = str(value or "hold")
        if direction not in {"charge", "discharge"}:
            continue
        ts_ms = _int(row.get("ts_ms"), 0)
        if (
            previous in {"charge", "discharge"}
            and previous != direction
            and previous_ts_ms is not None
            and ts_ms > 0
            and (ts_ms - previous_ts_ms) / 1000.0 < hold_s
        ):
            count += 1
        previous = direction
        previous_ts_ms = ts_ms if ts_ms > 0 else previous_ts_ms
    return count


def phase5_activation_contract(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Nur eine vollständig und exakt gebundene Konfiguration darf aktivieren."""

    cfg = cfg if isinstance(cfg, dict) else {}
    mode_present = "storage_dispatch_runtime_mode" in cfg
    requested = cfg.get("storage_dispatch_runtime_mode")
    requested_text = requested if isinstance(requested, str) else ""
    exact_mode = requested_text in {SHADOW_MODE, FIELD_MODE}
    gate = cfg.get("storage_dispatch_activation_gate")
    evidence = cfg.get("storage_dispatch_activation_evidence_sha256")
    candidate = cfg.get("storage_dispatch_activation_candidate_fingerprint")
    gate_bound = bool(
        gate == SHADOW_GATE_NAME
        and isinstance(evidence, str)
        and DIGEST_RE.fullmatch(evidence)
        and isinstance(candidate, str)
        and DIGEST_RE.fullmatch(candidate)
    )
    product_enabled = _bool(cfg.get("direct_marketing_enable"), False)
    blockers: List[str] = []
    if not product_enabled:
        blockers.append("DIRECT_MARKETING_DISABLED")
    elif requested_text == FIELD_MODE:
        if not gate_bound:
            blockers.append("SHADOW_60_GATE_NOT_EXACTLY_BOUND")
    elif requested_text == SHADOW_MODE or not mode_present:
        blockers.append("PHASE5_MODE_SHADOW")
    else:
        blockers.append("PHASE5_MODE_MISSING_OR_UNKNOWN")
    active = bool(requested_text == FIELD_MODE and exact_mode and gate_bound and product_enabled)
    return {
        "requested_mode": requested_text or None,
        "mode_source": "explicit" if mode_present else "default_missing",
        "effective_mode": (
            DISABLED_MODE
            if not product_enabled
            else FIELD_MODE
            if active
            else SHADOW_MODE
        ),
        "field_active": active,
        "gate_name": gate if isinstance(gate, str) else None,
        "gate_evidence_sha256": evidence if isinstance(evidence, str) else None,
        "candidate_fingerprint": candidate if isinstance(candidate, str) else None,
        "gate_exactly_bound": gate_bound,
        "product_enabled": product_enabled,
        "applicable": product_enabled,
        "status": (
            "FIELD_ACTIVE"
            if active
            else "SHADOW"
            if product_enabled
            else "NOT_APPLICABLE"
        ),
        "storage_parallel_required": False,
        "blockers": blockers,
        "self_activation_allowed": False,
    }


def _digest_body(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        return None
    return value.removeprefix("sha256:")


def phase5_activation_transaction_contract(
    current_cfg: Dict[str, Any],
    updates: Dict[str, Any],
    *,
    expected_evidence_sha256: str,
    expected_candidate_fingerprint: str,
) -> Dict[str, Any]:
    """Prüft die vier Bindungsfelder als eine atomare, schreibfreie Entscheidung."""

    current = current_cfg if isinstance(current_cfg, dict) else {}
    proposed_updates = updates if isinstance(updates, dict) else {}
    blockers: List[str] = []
    if set(proposed_updates) != ACTIVATION_TRANSACTION_KEYS:
        blockers.append("ACTIVATION_TRANSACTION_FIELDS_NOT_EXACT")
    expected_evidence = _digest_body(expected_evidence_sha256)
    expected_candidate = _digest_body(expected_candidate_fingerprint)
    proposed_evidence = _digest_body(proposed_updates.get("storage_dispatch_activation_evidence_sha256"))
    proposed_candidate = _digest_body(proposed_updates.get("storage_dispatch_activation_candidate_fingerprint"))
    if expected_evidence is None or proposed_evidence != expected_evidence:
        blockers.append("ACTIVATION_EVIDENCE_FINGERPRINT_MISMATCH")
    if expected_candidate is None or proposed_candidate != expected_candidate:
        blockers.append("ACTIVATION_CANDIDATE_FINGERPRINT_MISMATCH")
    if proposed_updates.get("storage_dispatch_runtime_mode") != FIELD_MODE:
        blockers.append("ACTIVATION_MODE_NOT_EXACT")
    if proposed_updates.get("storage_dispatch_activation_gate") != SHADOW_GATE_NAME:
        blockers.append("ACTIVATION_GATE_NOT_EXACT")
    proposed = copy.deepcopy(current)
    proposed.update(copy.deepcopy(proposed_updates))
    activation = phase5_activation_contract(proposed)
    for code in activation.get("blockers") or []:
        if code not in blockers:
            blockers.append(code)
    return {
        "valid": bool(not blockers and activation.get("field_active")),
        "blockers": blockers,
        "proposed_config": proposed if not blockers else None,
        "activation": activation,
        "atomic_fields": sorted(ACTIVATION_TRANSACTION_KEYS),
        "storage_parallel_enable_touched": "storage_parallel_enable" in proposed_updates,
        "self_activation_allowed": False,
        "write_effect": False,
    }


def resolve_current_shadow_slot(
    plan: Dict[str, Any],
    now_ms: int,
    *,
    max_age_s: int = 900,
) -> Dict[str, Any]:
    validation = validate_canonical_plan(plan, int(now_ms), max_age_s=max_age_s)
    result = {
        "valid": False,
        "not_applicable": False,
        "block_reason_code": validation.get("block_reason_code"),
        "plan_id": validation.get("plan_id"),
        "slot_id": validation.get("slot_id"),
        "plan_age_s": validation.get("age_s"),
        "plan_slot": validation.get("slot"),
        "shadow_slot": None,
    }
    if not validation.get("valid"):
        return result
    shadow = plan.get("shadow_dispatch") if isinstance(plan.get("shadow_dispatch"), dict) else {}
    if shadow.get("status") == "SHADOW_NOT_APPLICABLE" and shadow.get("applicable") is False:
        result.update({
            "not_applicable": True,
            "block_reason_code": None,
            "shadow_not_applicable_reason_code": str(
                shadow.get("not_applicable_reason_code")
                or "DIRECT_MARKETING_DISABLED"
            ),
        })
        return result
    if shadow.get("fallback") or shadow.get("status") not in {"SHADOW_OK", "SHADOW_HEADROOM_PARTIAL"}:
        fallback_reason = str(shadow.get("fallback_reason_code") or "").strip()
        result["block_reason_code"] = fallback_reason or "SHADOW_FALLBACK_OR_STATUS_INVALID"
        result["shadow_fallback_reason_code"] = fallback_reason or None
        return result
    plan_slot = validation.get("slot") if isinstance(validation.get("slot"), dict) else {}
    shadow_slot = next(
        (
            item
            for item in shadow.get("slots") or []
            if isinstance(item, dict)
            and item.get("slot_id") == validation.get("slot_id")
            and _int(item.get("start_ts_ms")) == _int(plan_slot.get("start_ts_ms"))
            and _int(item.get("end_ts_ms")) == _int(plan_slot.get("end_ts_ms"))
        ),
        None,
    )
    if not isinstance(shadow_slot, dict):
        result["block_reason_code"] = "CURRENT_SHADOW_SLOT_MISSING_OR_MISMATCH"
        return result
    result.update({"valid": True, "block_reason_code": None, "shadow_slot": shadow_slot})
    return result


def _complete_revisions(plan: Dict[str, Any]) -> bool:
    revisions = plan.get("input_revisions") if isinstance(plan.get("input_revisions"), dict) else {}
    return REVISION_KEYS.issubset(revisions) and all(
        isinstance(revisions.get(key), str) and REVISION_RE.fullmatch(revisions[key])
        for key in REVISION_KEYS
    )


def _price_horizon_activation_contract(shadow: Dict[str, Any]) -> Dict[str, Any]:
    """Validiert Marktgrenze und Ist-Horizont ohne starre 24-h-Annahme."""

    horizon = shadow.get("price_horizon_contract") if isinstance(shadow.get("price_horizon_contract"), dict) else {}
    decision = shadow.get("decision_horizon") if isinstance(shadow.get("decision_horizon"), dict) else {}
    blockers: List[str] = []

    def require(condition: bool, code: str) -> None:
        if not condition and code not in blockers:
            blockers.append(code)

    decision_slots = _int(decision.get("slots"), 0)
    decision_start_ms = _int(decision.get("start_ts_ms"), 0)
    decision_end_ms = _int(decision.get("end_ts_ms"), 0)
    effective_slots = _int(horizon.get("effective_decision_slots"), 0)
    required_slots = _int(horizon.get("required_slots_to_market_day_boundary"), 0)
    current_start_ms = _int(horizon.get("current_slot_start_ts_ms"), 0)
    day_start_ms = _int(horizon.get("local_market_day_start_ts_ms"), 0)
    boundary_ms = _int(horizon.get("next_local_market_day_boundary_ts_ms"), 0)
    market_day_slots = _int(horizon.get("market_day_total_slots"), 0)
    bound_end_ms = _int(horizon.get("bound_horizon_end_ts_ms"), 0)
    require(horizon.get("schema_version") == PRICE_HORIZON_SCHEMA, "PRICE_HORIZON_CONTRACT_INVALID")
    require(horizon.get("timezone") == MARKET_TIMEZONE, "PRICE_HORIZON_TIMEZONE_INVALID")
    require(_int(horizon.get("slot_duration_ms"), 0) == SLOT_DURATION_MS, "PRICE_HORIZON_SLOT_DURATION_INVALID")
    require(horizon.get("applicability_basis") == "NEXT_LOCAL_MARKET_DAY_BOUNDARY", "PRICE_HORIZON_APPLICABILITY_BASIS_INVALID")
    require(decision_slots > 0 and decision_slots == effective_slots, "PRICE_HORIZON_DECISION_COUNT_MISMATCH")
    require(decision_start_ms > 0 and decision_start_ms == current_start_ms, "PRICE_HORIZON_CURRENT_SLOT_MISMATCH")
    require(
        decision_end_ms > decision_start_ms
        and decision_end_ms == bound_end_ms
        and decision_end_ms - decision_start_ms == decision_slots * SLOT_DURATION_MS,
        "PRICE_HORIZON_BOUND_END_MISMATCH",
    )
    require(
        market_day_slots in {92, 96, 100}
        and boundary_ms - day_start_ms == market_day_slots * SLOT_DURATION_MS,
        "PRICE_HORIZON_MARKET_DAY_LENGTH_INVALID",
    )
    require(
        required_slots > 0
        and required_slots <= market_day_slots
        and boundary_ms - current_start_ms == required_slots * SLOT_DURATION_MS,
        "PRICE_HORIZON_MARKET_DAY_BOUNDARY_INVALID",
    )
    require(
        horizon.get("complete_to_next_local_market_day_boundary") is True
        and horizon.get("field_activation_horizon_complete") is True
        and effective_slots >= required_slots
        and bound_end_ms >= boundary_ms,
        "PRICE_HORIZON_MARKET_DAY_INCOMPLETE",
    )
    require(_int(horizon.get("unpriced_slots_imputed"), -1) == 0, "PRICE_HORIZON_IMPUTATION_FORBIDDEN")
    require(
        horizon.get("rolling_24h_complete") is (decision_slots >= 96),
        "PRICE_HORIZON_ROLLING_24H_DIAGNOSTIC_INVALID",
    )
    return {
        "valid": not blockers,
        "blockers": blockers,
        "schema_version": horizon.get("schema_version"),
        "timezone": horizon.get("timezone"),
        "next_local_market_day_boundary_ts_ms": boundary_ms or None,
        "required_slots_to_market_day_boundary": required_slots,
        "effective_decision_slots": effective_slots,
        "bound_horizon_end_ts_ms": bound_end_ms or None,
        "complete_to_next_local_market_day_boundary": horizon.get("complete_to_next_local_market_day_boundary") is True,
        "rolling_24h_complete": horizon.get("rolling_24h_complete") is True,
    }


def _shadow_input_binding_contract(
    plan: Dict[str, Any],
    shadow: Dict[str, Any],
    resolved: Dict[str, Any],
) -> Dict[str, Any]:
    """Validiert die im Planhash enthaltene Source-/Freshness-Bindung."""

    binding = shadow.get("input_binding_contract") if isinstance(shadow.get("input_binding_contract"), dict) else {}
    revisions = plan.get("input_revisions") if isinstance(plan.get("input_revisions"), dict) else {}
    slot = resolved.get("plan_slot") if isinstance(resolved.get("plan_slot"), dict) else {}
    price = binding.get("price") if isinstance(binding.get("price"), dict) else {}
    forecast = binding.get("forecast") if isinstance(binding.get("forecast"), dict) else {}
    reserve = binding.get("reserve") if isinstance(binding.get("reserve"), dict) else {}
    terminal = binding.get("terminal") if isinstance(binding.get("terminal"), dict) else {}
    decision = shadow.get("decision_horizon") if isinstance(shadow.get("decision_horizon"), dict) else {}
    decision_end_ms = _int(decision.get("end_ts_ms"), 0)
    reasons: List[str] = []

    def require(condition: bool, code: str) -> None:
        if not condition and code not in reasons:
            reasons.append(code)

    if resolved.get("not_applicable") is True or binding.get("applicable") is False:
        require(resolved.get("not_applicable") is True, "PLAN_NOT_APPLICABLE_STATUS_MISMATCH")
        require(binding.get("applicable") is False, "INPUT_BINDING_NOT_APPLICABLE_STATUS_MISMATCH")
        require(binding.get("schema_version") == SHADOW_INPUT_BINDING_SCHEMA, "INPUT_BINDING_SCHEMA_INVALID")
        require(binding.get("source_revisions") == revisions, "INPUT_BINDING_REVISIONS_MISMATCH")
        require(
            _int(binding.get("plan_generated_at_ts_ms"), 0) == _int(plan.get("generated_at_ts_ms"), 0),
            "INPUT_BINDING_GENERATION_MISMATCH",
        )
        require(
            _int(binding.get("current_slot_start_ts_ms"), 0) == _int(slot.get("start_ts_ms"), 0)
            and _int(binding.get("current_slot_end_ts_ms"), 0) == _int(slot.get("end_ts_ms"), 0),
            "INPUT_BINDING_SLOT_MISMATCH",
        )
        reason_code = str(binding.get("not_applicable_reason_code") or "")
        require(reason_code == "DIRECT_MARKETING_DISABLED", "NOT_APPLICABLE_REASON_INVALID")
        return {
            "valid": not reasons,
            "applicable": False,
            "not_applicable_reason_code": reason_code or None,
            "blockers": reasons,
            "schema_version": binding.get("schema_version"),
            "plan_id": resolved.get("plan_id"),
            "slot_id": resolved.get("slot_id"),
            "source_revisions": copy.deepcopy(binding.get("source_revisions")),
            "components": {
                "price": copy.deepcopy(price),
                "forecast": copy.deepcopy(forecast),
                "reserve": copy.deepcopy(reserve),
                "terminal": copy.deepcopy(terminal),
            },
        }

    require(resolved.get("valid") is True, "PLAN_OR_SLOT_NOT_VALIDATED")
    require(binding.get("schema_version") == SHADOW_INPUT_BINDING_SCHEMA, "INPUT_BINDING_SCHEMA_INVALID")
    require(binding.get("source_revisions") == revisions, "INPUT_BINDING_REVISIONS_MISMATCH")
    require(
        _int(binding.get("plan_generated_at_ts_ms"), 0) == _int(plan.get("generated_at_ts_ms"), 0),
        "INPUT_BINDING_GENERATION_MISMATCH",
    )
    require(
        _int(binding.get("current_slot_start_ts_ms"), 0) == _int(slot.get("start_ts_ms"), 0)
        and _int(binding.get("current_slot_end_ts_ms"), 0) == _int(slot.get("end_ts_ms"), 0),
        "INPUT_BINDING_SLOT_MISMATCH",
    )
    require(
        price.get("source_revision") == revisions.get("price")
        and price.get("freshness_contract") == "EXPLICIT_CONTIGUOUS_SLOT_FRESHNESS_NO_TAIL_IMPUTATION"
        and price.get("horizon_schema_version") == PRICE_HORIZON_SCHEMA
        and price.get("applicability_basis") == "NEXT_LOCAL_MARKET_DAY_BOUNDARY"
        and price.get("timezone") == MARKET_TIMEZONE
        and _int(price.get("required_slots_to_market_day_boundary"), 0) > 0
        and _int(price.get("bound_horizon_end_ts_ms"), 0) == decision_end_ms
        and _int(price.get("unpriced_slots_imputed"), -1) == 0
        and price.get("complete") is True,
        "PRICE_INPUT_BINDING_INVALID",
    )
    require(
        forecast.get("pv_source_revision") == revisions.get("pv_ensemble")
        and forecast.get("load_source_revision") == revisions.get("load_ensemble")
        and isinstance(forecast.get("source"), str)
        and bool(forecast.get("source"))
        and isinstance(forecast.get("trust"), str)
        and bool(forecast.get("trust"))
        and _int(forecast.get("horizon_end_ts_ms"), 0) == decision_end_ms
        and forecast.get("freshness_contract") == "BOUND_TO_CANONICAL_PLAN_VALIDITY_NO_PROVIDER_AGE_CLAIM"
        and forecast.get("complete") is True,
        "FORECAST_INPUT_BINDING_INVALID",
    )
    require(
        reserve.get("state_source_revision") == revisions.get("state")
        and reserve.get("hardware_source_revision") == revisions.get("hardware_limits")
        and reserve.get("config_source_revision") == revisions.get("config")
        and reserve.get("policy_source_revision") == revisions.get("policy")
        and isinstance(reserve.get("source"), str)
        and bool(reserve.get("source"))
        and _int(reserve.get("horizon_end_ts_ms"), 0) == decision_end_ms
        and reserve.get("complete") is True,
        "RESERVE_INPUT_BINDING_INVALID",
    )
    require(
        terminal.get("price_source_revision") == revisions.get("price")
        and terminal.get("pv_source_revision") == revisions.get("pv_ensemble")
        and terminal.get("load_source_revision") == revisions.get("load_ensemble")
        and terminal.get("state_source_revision") == revisions.get("state")
        and terminal.get("freshness_contract") == "RECOMPUTED_WITH_CANONICAL_PLAN_GENERATION"
        and _int(terminal.get("decision_horizon_end_ts_ms"), 0) == decision_end_ms
        and isinstance(terminal.get("source"), str)
        and bool(terminal.get("source"))
        and terminal.get("complete") is True,
        "TERMINAL_INPUT_BINDING_INVALID",
    )
    require(binding.get("field_activation_input_complete") is True, "INPUT_BINDING_INCOMPLETE")
    return {
        "valid": not reasons,
        "applicable": True,
        "not_applicable_reason_code": None,
        "blockers": reasons,
        "schema_version": binding.get("schema_version"),
        "plan_id": resolved.get("plan_id"),
        "slot_id": resolved.get("slot_id"),
        "source_revisions": copy.deepcopy(binding.get("source_revisions")),
        "components": {
            "price": copy.deepcopy(price),
            "forecast": copy.deepcopy(forecast),
            "reserve": copy.deepcopy(reserve),
            "terminal": copy.deepcopy(terminal),
        },
    }


def _forecast_complete(slot: Dict[str, Any]) -> bool:
    forecast = slot.get("forecast_w") if isinstance(slot.get("forecast_w"), dict) else {}
    pv = forecast.get("pv") if isinstance(forecast.get("pv"), dict) else {}
    load = forecast.get("load") if isinstance(forecast.get("load"), dict) else {}
    if all(_float(source.get(key), None) is not None for source in (pv, load) for key in ("p10", "p50", "p90")):
        return True
    soc = slot.get("soc_pct") if isinstance(slot.get("soc_pct"), dict) else {}
    return bool(
        slot.get("forecast_scenario_contract") == "legacy_p50_only_no_quantile_invention"
        and _float(pv.get("p50"), None) is not None
        and _float(load.get("p50"), None) is not None
        and _float(soc.get("reserve_floor"), None) is not None
        and _float(soc.get("notstrom_floor"), None) is not None
    )


def _live_contract(live: Dict[str, Any], legacy: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    live = live if isinstance(live, dict) else {}
    required = {
        "soc_pct": live.get("SOC", legacy.get("soc")),
        "pv_w": live.get("PV_Power", legacy.get("pv_w")),
        "home_w": live.get("Home_Power", legacy.get("home_w")),
        "grid_w": live.get("Grid_Power", legacy.get("grid_w")),
        "battery_w": live.get("Battery_Power", legacy.get("bat_w")),
    }
    source_ts_ms = _int(live.get("_ts", live.get("ts", legacy.get("ts"))), 0)
    if 0 < source_ts_ms < 100_000_000_000:
        source_ts_ms *= 1000
    age_s = (now_ms - source_ts_ms) / 1000.0 if source_ts_ms > 0 else None
    valid = bool(
        all(_float(value, None) is not None for value in required.values())
        and not legacy.get("safe_start")
        and not legacy.get("live_stale")
        and legacy.get("live_sample_valid", True)
        and legacy.get("grid_power_valid", True)
        and age_s is not None
        and -5.0 <= age_s <= 30.0
    )
    return {"valid": valid, "age_s": round(age_s, 3) if age_s is not None else None, **required}


def _power_settings_contract(settings: Dict[str, Any]) -> Dict[str, Any]:
    settings = settings if isinstance(settings, dict) else {}
    status = str(settings.get("status") or "")
    retry_value = settings.get("retry_remaining_s")
    pending_value = settings.get("pending_remaining_s")
    retry_s = (
        _float(retry_value, None)
        if isinstance(retry_value, (int, float)) and not isinstance(retry_value, bool)
        else None
    )
    pending_s = (
        _float(pending_value, None)
        if isinstance(pending_value, (int, float)) and not isinstance(pending_value, bool)
        else None
    )
    timers_valid = bool(
        retry_s is not None
        and pending_s is not None
        and retry_s == 0.0
        and pending_s == 0.0
    )
    live_readback_valid = True
    if status == POWER_SETTINGS_LIVE_READBACK_STATUS:
        readback = settings.get("readback") if isinstance(settings.get("readback"), dict) else {}
        live_readback_valid = bool(
            settings.get("schema") == POWER_SETTINGS_SCHEMA
            and settings.get("contract_version") == POWER_SETTINGS_CONTRACT_VERSION
            and settings.get("stage") == "live_reconciliation"
            and settings.get("readback_source") == "canonical_live"
            and ("fresh" not in settings or settings.get("fresh") is True)
            and ("valid" not in settings or settings.get("valid") is True)
            and isinstance(readback.get("limits_used"), bool)
            and all(
                isinstance(readback.get(key), int)
                and not isinstance(readback.get(key), bool)
                and readback.get(key) >= 0
                for key in ("max_charge_w", "max_discharge_w", "discharge_start_w")
            )
        )
    valid = bool(
        settings.get("confirmed") is True
        and status in POWER_SETTINGS_CONFIRMED_STATUSES
        and timers_valid
        and live_readback_valid
    )
    return {
        "valid": valid,
        "status": status or None,
        "retry_remaining_s": retry_s,
        "pending_remaining_s": pending_s,
        "timers_valid": timers_valid,
        "live_readback_valid": live_readback_valid,
    }


def _candidate_contract(plan: Dict[str, Any], shadow_slot: Dict[str, Any]) -> Dict[str, Any]:
    action = str(shadow_slot.get("planned_action") or "").upper()
    power_w = _float(shadow_slot.get("battery_w"), None)
    direction = action_direction(action)
    direction_ok = bool(
        power_w is not None
        and (
            (direction == "hold" and abs(power_w) <= 50.0)
            or (direction == "charge" and power_w > 50.0)
            or (direction == "discharge" and power_w < -50.0)
        )
    )
    power_abs_w = abs(power_w or 0.0)
    max_charge_w = _float(plan.get("max_charge_w"), 0.0) or 0.0
    max_discharge_w = _float(plan.get("max_discharge_w"), 0.0) or 0.0
    limit_w = max_charge_w if direction == "charge" else max_discharge_w if direction == "discharge" else 0.0
    power_ok = bool(direction == "hold" or (limit_w >= 300.0 and 300.0 <= power_abs_w <= limit_w + 1.0))
    return {
        "valid": action in VALID_ACTIONS and direction_ok and power_ok,
        "action": action or None,
        "direction": direction,
        "battery_w": round(power_w or 0.0, 3),
        "power_w": round(power_abs_w, 3),
        "reason_code": shadow_slot.get("reason_code"),
        "block_reason_code": shadow_slot.get("block_reason_code"),
        "shadow_selected": shadow_slot.get("selected") is True,
        "direction_valid": direction_ok,
        "power_valid": power_ok,
        "economics_ct": copy.deepcopy(shadow_slot.get("economics_ct")),
        "economic_export_gate": copy.deepcopy(shadow_slot.get("economic_export_gate")),
        "headroom_gate": copy.deepcopy(shadow_slot.get("headroom_gate")),
        "action_horizon_contract": copy.deepcopy(shadow_slot.get("action_horizon_contract")),
        "constraints": copy.deepcopy(shadow_slot.get("binding_constraints")),
        "headroom": copy.deepcopy(shadow_slot.get("headroom")),
    }


def _canonical_direct_marketing_slot_contract(
    plan: Dict[str, Any],
    plan_slot: Dict[str, Any],
) -> Dict[str, Any]:
    """Bindet eine DV-Auswahl ausschließlich an die kanonische Slotprojektion."""

    projection = (
        plan_slot.get("projection")
        if isinstance(plan_slot.get("projection"), dict)
        else {}
    )
    candidate = projection.get("direct_marketing_candidate") is True
    selected = projection.get("direct_marketing_selected") is True
    executable = projection.get("direct_marketing_plan_executable") is True
    commands_allowed = projection.get("direct_marketing_plan_commands_allowed") is True
    action = str(projection.get("direct_marketing_plan_action") or "").upper()
    source_action = str(projection.get("direct_marketing_plan_source_action") or "")
    source_mode = _normalized_direct_marketing_mode(
        projection.get("direct_marketing_plan_source_mode")
    )
    direct = plan.get("direct_marketing") if isinstance(plan.get("direct_marketing"), dict) else {}
    plan_source_mode = _normalized_direct_marketing_mode(direct.get("mode"))
    source_mode_matches_plan = bool(plan_source_mode and source_mode == plan_source_mode)
    action_id = projection.get("direct_marketing_plan_action_id")
    segment_id = projection.get("direct_marketing_plan_segment_id")
    planned_w = _float(projection.get("direct_marketing_planned_w"), None)
    window_id = projection.get("direct_marketing_window_id")
    window_valid = isinstance(window_id, str) and bool(window_id.strip())
    action_horizon_contract = (
        projection.get("direct_marketing_action_horizon_contract")
        if isinstance(projection.get("direct_marketing_action_horizon_contract"), dict)
        else {}
    )
    economic_export_gate = (
        projection.get("direct_marketing_economic_export_gate")
        if isinstance(projection.get("direct_marketing_economic_export_gate"), dict)
        else {}
    )
    action_horizon_valid = bool(
        action_horizon_contract.get("schema_version") == ACTION_HORIZON_SCHEMA
        and action_horizon_contract.get("action") == action
        and action_horizon_contract.get("complete") is True
    )
    economic_gate_valid = bool(
        action != "ECONOMIC_EXPORT"
        or (
            economic_export_gate.get("allowed") is True
            and not economic_export_gate.get("blockers")
            and _float(economic_export_gate.get("margin_ct_kwh"), None) is not None
            and _float(economic_export_gate.get("user_min_margin_ct"), None) is not None
            and _float(economic_export_gate.get("expected_profit_eur"), None) is not None
            and _float(economic_export_gate.get("min_window_profit_eur"), None) is not None
        )
    )
    expected_source_action = {
        "ECONOMIC_EXPORT": "eco_plus_export_candidate",
        "PV_STORE": "eco_plus_store_pv_candidate",
    }.get(action)
    source_mode_valid = bool(
        (action == "ECONOMIC_EXPORT" and source_mode == "eco_plus")
        or (action == "PV_STORE" and source_mode in {"eco", "eco_plus"})
    )
    selected_contract_valid = bool(
        candidate
        and selected
        and executable
        and commands_allowed
        and action in {"ECONOMIC_EXPORT", "PV_STORE"}
        and source_action == expected_source_action
        and source_mode_valid
        and source_mode_matches_plan
        and planned_w is not None
        and planned_w >= 300.0
        and window_valid
        and isinstance(action_id, str)
        and bool(action_id)
        and isinstance(segment_id, str)
        and bool(segment_id)
        and action_horizon_valid
        and economic_gate_valid
    )
    partial_selection = any((selected, executable, commands_allowed, (planned_w or 0.0) > 0.0, window_valid))
    reason_code = None
    if partial_selection and not selected_contract_valid:
        reason_code = "CANONICAL_DIRECT_MARKETING_SELECTION_INCOMPLETE"
    elif candidate and not selected_contract_valid:
        reason_code = "CANONICAL_DIRECT_MARKETING_SLOT_NOT_SELECTED"
    return {
        "candidate": candidate,
        "selected": selected,
        "plan_executable": executable,
        "plan_commands_allowed": commands_allowed,
        "action": action or None,
        "source_action": source_action or None,
        "source_mode": source_mode or None,
        "plan_source_mode": plan_source_mode or None,
        "source_mode_matches_plan": source_mode_matches_plan,
        "action_id": action_id if isinstance(action_id, str) and action_id else None,
        "segment_id": segment_id if isinstance(segment_id, str) and segment_id else None,
        "planned_w": round(planned_w, 3) if planned_w is not None else None,
        "window_id": window_id if window_valid else None,
        "window_start_ts_ms": _int(projection.get("direct_marketing_window_start_ts_ms"), 0) or None,
        "window_end_ts_ms": _int(projection.get("direct_marketing_window_end_ts_ms"), 0) or None,
        "action_horizon_contract": copy.deepcopy(action_horizon_contract) if action_horizon_contract else None,
        "economic_export_gate": copy.deepcopy(economic_export_gate) if economic_export_gate else None,
        "valid_selected_contract": selected_contract_valid,
        "reason_code": reason_code,
    }


def _append(blockers: List[str], condition: bool, code: str) -> None:
    if condition and code not in blockers:
        blockers.append(code)


def _economic_decision_only_hold_contract(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Erlaubt nur vollständig belegte Profitblocker als aktiven HOLD-Entscheid."""

    gate = candidate.get("economic_export_gate") if isinstance(candidate.get("economic_export_gate"), dict) else {}
    gate_blockers = [str(code) for code in gate.get("blockers") or []]
    blocker_set = set(gate_blockers)
    margin = _float(gate.get("margin_ct_kwh"), None)
    minimum_margin = _float(gate.get("user_min_margin_ct"), None)
    profit = _float(gate.get("expected_profit_eur"), None)
    minimum_profit = _float(gate.get("min_window_profit_eur"), None)
    margin_blocked = bool(
        margin is not None
        and minimum_margin is not None
        and margin + 0.000001 < minimum_margin
        and "ECONOMIC_EXPORT_MARGIN_BELOW_USER_MINIMUM" in blocker_set
    )
    window_blocked = bool(
        profit is not None
        and minimum_profit is not None
        and profit + 0.000001 < minimum_profit
        and "ECONOMIC_EXPORT_WINDOW_PROFIT_BELOW_USER_MINIMUM" in blocker_set
    )
    valid = bool(
        candidate.get("action") == "ECONOMIC_EXPORT"
        and gate.get("allowed") is False
        and blocker_set
        and blocker_set.issubset(ECONOMIC_HOLD_ALLOWED_BLOCKERS)
        and (margin_blocked or window_blocked)
    )
    return {
        "valid": valid,
        "reason_code": next(
            (code for code in gate_blockers if code in ECONOMIC_HOLD_THRESHOLD_BLOCKERS),
            None,
        ),
        "blockers": gate_blockers if valid else [],
        "margin_blocked": margin_blocked,
        "window_profit_blocked": window_blocked,
    }


def phase5_arbitration_contract(
    cfg: Dict[str, Any],
    plan: Dict[str, Any],
    legacy: Dict[str, Any],
    live: Dict[str, Any],
    power_settings: Dict[str, Any],
    path_contract: Dict[str, Any],
    previous_state: Optional[Dict[str, Any]] = None,
    *,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Bewertet den ersten Shadow-Slot; die Managerübersetzung erfolgt später."""

    now_value = int(now_ms if now_ms is not None else time.time() * 1000.0)
    activation = phase5_activation_contract(cfg)
    applicable = activation.get("applicable") is True
    resolved = resolve_current_shadow_slot(plan, now_value)
    shadow = plan.get("shadow_dispatch") if isinstance(plan.get("shadow_dispatch"), dict) else {}
    shadow_slot = resolved.get("shadow_slot") if isinstance(resolved.get("shadow_slot"), dict) else {}
    candidate = _candidate_contract(plan, shadow_slot) if shadow_slot else {
        "valid": False, "action": None, "direction": "hold", "battery_w": 0.0, "power_w": 0.0,
    }
    live_contract = _live_contract(live, legacy, now_value)
    settings_contract = _power_settings_contract(power_settings)
    price_horizon = shadow.get("price_horizon_contract") if isinstance(shadow.get("price_horizon_contract"), dict) else {}
    price_horizon_activation = _price_horizon_activation_contract(shadow)
    terminal = shadow.get("terminal_value") if isinstance(shadow.get("terminal_value"), dict) else {}
    current_plan_slot = resolved.get("plan_slot") if isinstance(resolved.get("plan_slot"), dict) else {}
    current_prices = current_plan_slot.get("prices_ct_kwh") if isinstance(current_plan_slot.get("prices_ct_kwh"), dict) else {}
    current_slot_available = bool(resolved.get("valid") and current_plan_slot and shadow_slot)
    direct_marketing_slot = _canonical_direct_marketing_slot_contract(plan, current_plan_slot)
    if direct_marketing_slot.get("valid_selected_contract"):
        plan_action = str(direct_marketing_slot.get("action") or "").upper()
        planned_w = float(direct_marketing_slot.get("planned_w") or 0.0)
        battery_w = planned_w if plan_action == "PV_STORE" else -planned_w
        candidate.update({
            "valid": True,
            "action": plan_action,
            "direction": "charge" if plan_action == "PV_STORE" else "discharge",
            "battery_w": round(battery_w, 3),
            "power_w": round(planned_w, 3),
            "direction_valid": True,
            "power_valid": True,
            "shadow_selected": False,
            "reason_code": "CANONICAL_DIRECT_MARKETING_PLAN_SELECTION",
            "block_reason_code": None,
            "window_id": direct_marketing_slot.get("window_id"),
            "action_id": direct_marketing_slot.get("action_id"),
            "segment_id": direct_marketing_slot.get("segment_id"),
            "window_start_ts_ms": direct_marketing_slot.get("window_start_ts_ms"),
            "window_end_ts_ms": direct_marketing_slot.get("window_end_ts_ms"),
            "selection_source": "canonical_slot_projection",
            "source_action": direct_marketing_slot.get("source_action"),
            "source_mode": direct_marketing_slot.get("source_mode"),
            "action_horizon_contract": copy.deepcopy(
                direct_marketing_slot.get("action_horizon_contract")
            ),
        })
        if plan_action == "ECONOMIC_EXPORT":
            candidate["economic_export_gate"] = copy.deepcopy(
                direct_marketing_slot.get("economic_export_gate")
            )
    reserve_contract = shadow.get("reserve_contract") if isinstance(shadow.get("reserve_contract"), dict) else {}
    input_binding = _shadow_input_binding_contract(plan, shadow, resolved)
    runtime_ms = _float(shadow.get("runtime_ms"), None)
    runtime_budget_ms = max(100.0, _float(cfg.get("storage_dispatch_runtime_budget_ms"), 2000.0) or 2000.0)
    max_charge_w = _float(plan.get("max_charge_w"), 0.0) or 0.0
    max_discharge_w = _float(plan.get("max_discharge_w"), 0.0) or 0.0
    capacity_wh = _float(plan.get("battery_capacity"), 0.0) or 0.0
    if 1.0 < capacity_wh < 500.0:
        capacity_wh *= 1000.0

    blockers: List[str] = list(activation.get("blockers") or [])
    economic_hold = _economic_decision_only_hold_contract(candidate)
    _append(blockers, not resolved.get("valid"), str(resolved.get("block_reason_code") or "PLAN_OR_SLOT_INVALID"))
    _append(blockers, not _complete_revisions(plan), "INPUT_REVISIONS_INCOMPLETE")
    _append(blockers, not input_binding.get("valid"), "SHADOW_INPUT_BINDING_INVALID")
    _append(blockers, capacity_wh <= 1000.0 or max_charge_w < 300.0 or max_discharge_w < 300.0, "HARDWARE_LIMITS_INCOMPLETE")
    for code in price_horizon_activation.get("blockers") or []:
        _append(blockers, True, str(code))
    _append(
        blockers,
        reserve_contract.get("field_activation_input_complete") is not True,
        "FORECAST_SCENARIO_OR_RESERVE_CONTRACT_INCOMPLETE",
    )
    _append(
        blockers,
        current_slot_available and current_prices.get("fresh") is not True,
        "CURRENT_PRICE_NOT_EXPLICITLY_FRESH",
    )
    _append(
        blockers,
        current_slot_available
        and (
            _float(current_prices.get("buy"), None) is None
            or _float(current_prices.get("net_sell"), None) is None
        ),
        "CURRENT_PRICE_MISSING",
    )
    _append(
        blockers,
        current_slot_available and not _forecast_complete(current_plan_slot),
        "CURRENT_FORECAST_SCENARIO_INCOMPLETE",
    )
    _append(blockers, terminal.get("fresh") is not True, "TERMINAL_VALUE_INVALID_OR_STALE")
    _append(blockers, runtime_ms is None or runtime_ms > runtime_budget_ms, "OPTIMIZER_RUNTIME_INVALID_OR_OVER_BUDGET")
    _append(blockers, not live_contract.get("valid"), "LIVE_PHYSICS_INVALID_OR_STALE")
    _append(blockers, not settings_contract.get("valid"), "POWER_SETTINGS_NOT_CONFIRMED_STABLE")
    _append(blockers, current_slot_available and not candidate.get("valid"), "CANONICAL_CANDIDATE_INVALID")
    _append(
        blockers,
        current_slot_available
        and candidate.get("action") in {"ECONOMIC_EXPORT", "PV_STORE"}
        and not direct_marketing_slot.get("valid_selected_contract")
        and not economic_hold.get("valid"),
        str(direct_marketing_slot.get("reason_code") or "CANONICAL_DIRECT_MARKETING_SLOT_NOT_SELECTED"),
    )
    _append(
        blockers,
        current_slot_available and candidate.get("action") == "GRID_CHARGE",
        "CANONICAL_GRID_CHARGE_SELECTION_CONTRACT_UNAVAILABLE",
    )
    action_horizon = (
        candidate.get("action_horizon_contract")
        if isinstance(candidate.get("action_horizon_contract"), dict)
        else {}
    )
    if candidate.get("action") != "HOLD":
        _append(
            blockers,
            not (
                action_horizon.get("schema_version") == ACTION_HORIZON_SCHEMA
                and action_horizon.get("complete") is True
                and _int(action_horizon.get("bound_horizon_end_ts_ms"), 0)
                == _int((shadow.get("decision_horizon") or {}).get("end_ts_ms"), 0)
            ),
            str(action_horizon.get("block_reason_code") or "ACTION_WINDOW_OUTSIDE_BOUND_HORIZON"),
        )
    profit_gate = candidate.get("economic_export_gate") if isinstance(candidate.get("economic_export_gate"), dict) else {}
    if candidate.get("action") == "ECONOMIC_EXPORT":
        profit_blockers = profit_gate.get("blockers") if isinstance(profit_gate.get("blockers"), list) else []
        if not profit_blockers and profit_gate.get("allowed") is not True:
            profit_blockers = ["ECONOMIC_EXPORT_PROFIT_CONTRACT_MISSING_OR_INVALID"]
        for code in profit_blockers:
            _append(blockers, True, str(code))
    headroom_gate = candidate.get("headroom_gate") if isinstance(candidate.get("headroom_gate"), dict) else {}
    if candidate.get("action") == "HEADROOM_EXPORT":
        _append(
            blockers,
            headroom_gate.get("allowed") is not True,
            str(headroom_gate.get("block_reason_code") or "HEADROOM_PRESSURE_NOT_CAUSALLY_BOUND"),
        )
    _append(blockers, bool(legacy.get("ems_budget_runtime_veto")), "EMS_BUDGET_RUNTIME_VETO")
    _append(blockers, bool(path_contract.get("veto_required")), "LEGACY_OWNER_PATH_CONFLICT")
    primary_path = str(path_contract.get("primary_path") or "")
    _append(blockers, primary_path in {"protection", "manual", "wallbox_support", "predump"}, "LEGACY_SAFETY_OR_MANUAL_OWNER_VETO")
    state_text = str(legacy.get("state") or "").lower()
    _append(blockers, state_text.startswith("manual_override") or bool(legacy.get("manual_override_active")), "MANUAL_OR_USER_OVERRIDE")
    _append(blockers, bool(legacy.get("abregel_active") or legacy.get("curtailment_protection_active")), "CURTAILMENT_PROTECTION_ACTIVE")
    _append(blockers, bool(legacy.get("ep_reserve_hold") or legacy.get("ep_reserve_discharge_hold")), "EMERGENCY_RESERVE_VETO")

    soc = _float(live_contract.get("soc_pct"), None)
    hard_floor = _float(shadow_slot.get("hard_floor_pct"), None)
    risk_floor = _float(shadow_slot.get("risk_floor_pct"), None)
    floor_values = [value for value in (hard_floor, risk_floor) if value is not None]
    floor = max(floor_values) if floor_values else None
    planned_soc = _float(shadow_slot.get("soc_start_pct"), None)
    soc_tolerance_pct = max(
        0.5,
        min(10.0, _float(cfg.get("storage_dispatch_live_plan_soc_tolerance_pct"), 5.0) or 5.0),
    )
    _append(
        blockers,
        current_slot_available and planned_soc is None,
        "CURRENT_PLANNED_SOC_MISSING",
    )
    _append(
        blockers,
        current_slot_available and hard_floor is None,
        "CURRENT_HARD_FLOOR_MISSING",
    )
    _append(
        blockers,
        current_slot_available and risk_floor is None,
        "CURRENT_RISK_FLOOR_MISSING",
    )
    _append(
        blockers,
        current_slot_available
        and soc is not None
        and planned_soc is not None
        and abs(soc - planned_soc) > soc_tolerance_pct,
        "LIVE_SOC_PLAN_DIVERGENCE",
    )
    _append(
        blockers,
        current_slot_available
        and soc is not None
        and hard_floor is not None
        and soc < hard_floor - 0.05,
        "LIVE_SOC_BELOW_HARD_FLOOR",
    )
    if candidate.get("direction") == "discharge" and current_slot_available:
        _append(
            blockers,
            soc is not None and floor is not None and soc <= floor + 0.05,
            "RESERVE_OR_RISK_FLOOR_VETO",
        )

    if not applicable:
        # Eine abgeschaltete DV-Capability ist kein Preis-/Forecast-/Runtimefehler.
        # Die typisierte Aktivierungssperre bleibt sichtbar, alle technischen
        # Shadowblocker sind für diese Anlage jedoch ausdrücklich nicht anwendbar.
        blockers = ["DIRECT_MARKETING_DISABLED"]

    activation_only = {
        "PHASE5_MODE_SHADOW",
        "PHASE5_MODE_MISSING_OR_UNKNOWN",
        "SHADOW_60_GATE_NOT_EXACTLY_BOUND",
        "DIRECT_MARKETING_DISABLED",
    }
    economic_hold_blockers = set(economic_hold.get("blockers") or [])
    non_economic_decision_blockers = [
        code
        for code in blockers
        if code not in activation_only and code not in economic_hold_blockers
    ]
    decision_only_economic_hold = bool(economic_hold.get("valid") and not non_economic_decision_blockers)
    selected_action = "HOLD" if decision_only_economic_hold else candidate.get("action") or "HOLD"
    selected_power_w = 0.0 if decision_only_economic_hold else float(candidate.get("power_w") or 0.0)
    selected_since_ts = now_value / 1000.0
    stability = {"active": False, "reason_code": None, "previous_direction": None}
    previous = previous_state if isinstance(previous_state, dict) else {}
    previous_phase5 = previous.get("storage_dispatch_phase5") if isinstance(previous.get("storage_dispatch_phase5"), dict) else {}
    previous_shelly = (
        previous.get("direct_marketing_aux_inverter_shelly")
        if isinstance(previous.get("direct_marketing_aux_inverter_shelly"), dict)
        else None
    )
    previous_shadow_selection = (
        previous_phase5.get("shadow_selection")
        if isinstance(previous_phase5.get("shadow_selection"), dict)
        else {}
    )
    previous_action = previous_phase5.get("selected_action") or previous_shadow_selection.get("action")
    previous_direction = action_direction(previous_action)
    current_direction = action_direction(selected_action)
    previous_since = _float(
        previous_phase5.get("selected_since_ts", previous_shadow_selection.get("selected_since_ts")),
        0.0,
    ) or 0.0
    previous_would_select = bool(
        previous_phase5.get("executable")
        or previous_shadow_selection.get("would_select")
    )
    owner_hold_s = max(0.0, _float(cfg.get("direct_marketing_owner_switch_hold_s"), 45.0) or 45.0)
    direction_deadband_w = max(
        300.0,
        _float(cfg.get("direct_marketing_netpoint_deadband_w"), 300.0) or 300.0,
    )
    live_battery_w = _float(live_contract.get("battery_w"), 0.0) or 0.0
    live_direction = (
        "charge"
        if live_battery_w >= direction_deadband_w
        else "discharge"
        if live_battery_w <= -direction_deadband_w
        else "hold"
    )
    decision_blockers = [code for code in blockers if code not in activation_only]
    stability["previous_direction"] = previous_direction
    stability["live_direction"] = live_direction
    if (
        not decision_blockers
        and current_direction in {"charge", "discharge"}
        and live_direction in {"charge", "discharge"}
        and current_direction != live_direction
        and abs(live_battery_w) >= direction_deadband_w
    ):
        selected_action = "HOLD"
        selected_power_w = 0.0
        stability.update({
            "active": True,
            "reason_code": "STABILITY_HOLD_LIVE_DIRECTION_REVERSAL",
            "live_battery_w": round(live_battery_w, 3),
            "deadband_w": direction_deadband_w,
        })
    elif (
        not decision_blockers
        and previous_would_select
        and previous_direction in {"charge", "discharge"}
        and current_direction in {"charge", "discharge"}
        and previous_direction != current_direction
        and now_value / 1000.0 - previous_since < owner_hold_s
    ):
        selected_action = "HOLD"
        selected_power_w = 0.0
        selected_since_ts = previous_since or now_value / 1000.0
        stability.update({"active": True, "reason_code": "STABILITY_HOLD_DIRECTION_REVERSAL", "hold_s": owner_hold_s})
    elif (
        not decision_blockers
        and previous_would_select
        and previous_direction == current_direction
        and current_direction in {"charge", "discharge"}
    ):
        previous_power_w = max(0.0, _float(previous_phase5.get("selected_power_w"), 0.0) or 0.0)
        deadband_w = max(0.0, _float(cfg.get("direct_marketing_netpoint_deadband_w"), 300.0) or 300.0)
        ramp_up_w = max(
            300.0,
            _float(
                cfg.get(
                    "direct_marketing_pv_store_ramp_step_w"
                    if current_direction == "charge"
                    else "direct_marketing_netpoint_ramp_up_w"
                ),
                300.0 if current_direction == "charge" else 1000.0,
            ) or 300.0,
        )
        if selected_power_w >= previous_power_w:
            if selected_power_w - previous_power_w <= deadband_w:
                selected_power_w = previous_power_w
                stability.update({"active": True, "reason_code": "STABILITY_DEADBAND", "deadband_w": deadband_w})
            elif selected_power_w > previous_power_w + ramp_up_w:
                selected_power_w = previous_power_w + ramp_up_w
                stability.update({"active": True, "reason_code": "STABILITY_RAMP_UP", "ramp_up_w": ramp_up_w})
        # Eine kleinere neue kanonische Grenze wird sofort eingehalten; ein
        # alter höherer Sollwert darf nie über frische Constraints gehalten werden.
        selected_since_ts = previous_since or now_value / 1000.0

    technical_blockers = [
        code
        for code in blockers
        if code not in activation_only
        and not (decision_only_economic_hold and code in economic_hold_blockers)
    ]
    if not applicable:
        selected_action = "HOLD"
        selected_power_w = 0.0
        technical_blockers = []
    field_selected = bool(
        activation.get("field_active")
        and (not blockers or decision_only_economic_hold)
    )
    field_executable = bool(field_selected and selected_action != "HOLD")
    decision_available = bool(applicable and resolved.get("valid") and candidate.get("valid"))
    shadow_would_select = bool(applicable and not technical_blockers)
    return {
        "schema_version": PHASE5_SCHEMA,
        "ts_ms": now_value,
        "applicable": applicable,
        "not_applicable_reason_code": None if applicable else "DIRECT_MARKETING_DISABLED",
        "activation": activation,
        "plan_id": resolved.get("plan_id"),
        "slot_id": resolved.get("slot_id"),
        "plan_age_s": resolved.get("plan_age_s"),
        "input_binding": input_binding,
        "decision_available": decision_available,
        "candidate": candidate,
        "canonical_direct_marketing_slot": direct_marketing_slot,
        "candidate_action": candidate.get("action"),
        "candidate_power_w": candidate.get("power_w"),
        "selected": field_selected,
        "executable": field_executable,
        "commands_allowed": field_executable,
        "selection_class": (
            "decision_only_hold"
            if field_selected and selected_action == "HOLD"
            else "command_action"
            if field_selected
            else "legacy_fallback"
        ),
        "decision_only_hold": {
            "active": bool(field_selected and selected_action == "HOLD"),
            "economic_policy_hold": decision_only_economic_hold,
            "reason_code": economic_hold.get("reason_code") if decision_only_economic_hold else None,
            "blockers": list(economic_hold.get("blockers") or []) if decision_only_economic_hold else [],
        },
        "selected_source": (
            "canonical_phase5_decision_only_hold"
            if field_selected and selected_action == "HOLD"
            else "canonical_phase5"
            if field_selected
            else "not_applicable"
            if not applicable
            else "legacy_fallback"
        ),
        "selected_action": selected_action if field_selected else None,
        "selected_power_w": round(selected_power_w, 3) if field_selected else 0.0,
        "selected_since_ts": selected_since_ts if field_selected else None,
        "block_reason_code": blockers[0] if blockers else None,
        "technical_block_reason_code": technical_blockers[0] if technical_blockers else None,
        "blockers": blockers,
        "technical_blockers": technical_blockers,
        "plan_resolution": {
            "valid": bool(resolved.get("valid")),
            "not_applicable": bool(resolved.get("not_applicable")),
            "block_reason_code": None if not applicable else resolved.get("block_reason_code"),
            "current_slot_available": current_slot_available,
        },
        "shadow_selection": {
            "would_select": shadow_would_select,
            "action": selected_action if shadow_would_select else None,
            "power_w": round(selected_power_w, 3) if shadow_would_select else 0.0,
            "direction": action_direction(selected_action) if shadow_would_select else "hold",
            "selected_since_ts": selected_since_ts if shadow_would_select else None,
            "hardware_effect": False,
        },
        "stability": stability,
        "price_horizon_contract": copy.deepcopy(price_horizon),
        "price_horizon_activation": price_horizon_activation,
        "runtime_ms": runtime_ms,
        "runtime_budget_ms": runtime_budget_ms,
        "live_contract": live_contract,
        "power_settings_contract": settings_contract,
        "legacy_baseline": {
            "state": legacy.get("state"),
            "mode": legacy.get("mode"),
            "mode_name": legacy.get("mode_name"),
            "power_w": _int(legacy.get("val"), 0),
            "priority": legacy.get("priority"),
            "primary_path": primary_path or None,
            "owner": (path_contract.get("owner_contract") or {}).get("control_owner")
            if isinstance(path_contract.get("owner_contract"), dict)
            else None,
            "physics_direction": live_direction,
        },
        "constraints": {
            "soc_pct": soc,
            "planned_soc_pct": planned_soc,
            "live_plan_soc_tolerance_pct": soc_tolerance_pct,
            "hard_floor_pct": hard_floor,
            "hard_floor_status": (
                "AVAILABLE"
                if hard_floor is not None
                else "UNAVAILABLE_CURRENT_SLOT"
                if not current_slot_available
                else "MISSING"
            ),
            "risk_floor_pct": risk_floor,
            "risk_floor_status": (
                "AVAILABLE"
                if risk_floor is not None
                else "UNAVAILABLE_CURRENT_SLOT"
                if not current_slot_available
                else "MISSING"
            ),
            "effective_floor_pct": floor,
            "effective_floor_status": "AVAILABLE" if floor is not None else "UNAVAILABLE_CURRENT_SLOT",
            "max_charge_w": max_charge_w,
            "max_discharge_w": max_discharge_w,
            "external_ac_pv_counting": "PLAN_TOTAL_PV_ONCE_SHELLY_AND_LUOX_CONTEXT_SEPARATE",
        },
        "external_context": {
            "pv_external_ac_w": _float(
                live.get("PV_External_AC_W", legacy.get("direct_marketing_pv_external_ac_w")),
                None,
            ),
            "pv_source": legacy.get("direct_marketing_pv_source"),
            "shelly_previous_cycle": copy.deepcopy(previous_shelly),
            "shelly_owner": "separate_existing_capability_not_storage_dispatch",
            "luox_active": bool(legacy.get("direct_marketing_external_derating_active")),
            "luox_source": legacy.get("direct_marketing_external_derating_source"),
            "luox_limit_w": _float(legacy.get("direct_marketing_external_derating_limit_w"), None),
            "luox_owner": "external_e3dc_luox_not_storage_dispatch",
            "counting_contract": "EXTERNAL_AC_PV_EXACTLY_ONCE_NO_SHELLY_OR_LUOX_COMMAND_EDGE",
        },
        "hardware_effect": False,
        "shelly_command_allowed": False,
        "luox_command_allowed": False,
    }


def shadow_60_gate_contract(cycles: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Bewertet Evidence; zählt oder aktiviert niemals selbstständig."""

    rows = [row for row in cycles if isinstance(row, dict)]
    applicable_cycles = sum(row.get("applicable") is True for row in rows)
    not_applicable_cycles = sum(row.get("applicable") is False for row in rows)
    coherent = sum(bool(row.get("plan_id") and row.get("slot_id")) for row in rows)
    fallback = sum(bool(row.get("technical_blockers", row.get("blockers"))) for row in rows)
    effectless = sum(not bool(row.get("hardware_effect")) for row in rows)
    runtime_green = sum(
        _float(row.get("runtime_ms"), float("inf")) <= _float(row.get("runtime_budget_ms"), 2000.0)
        for row in rows
    )
    violation_fields = (
        "safety_violation",
        "owner_conflict",
        "reserve_violation",
        "direction_collision",
        "shelly_competition",
        "luox_competition",
        "owner_switch_regression",
        "direction_switch_regression",
        "immediate_counter_cycle",
        "ack_readback_regression",
        "hardware_edge_added",
    )
    violation_cycles = sum(
        any(bool(row.get(field)) for field in violation_fields)
        for row in rows
    )
    canonical_directions = [
        ((row.get("shadow_selection") or {}).get("direction"))
        if isinstance(row.get("shadow_selection"), dict)
        else "hold"
        for row in rows
    ]
    legacy_directions = [
        ((row.get("legacy_baseline") or {}).get("physics_direction"))
        if isinstance(row.get("legacy_baseline"), dict)
        else "hold"
        for row in rows
    ]
    canonical_direction_reversals = _direction_reversals(canonical_directions)
    legacy_direction_reversals = _direction_reversals(legacy_directions)
    canonical_immediate_counter_cycles = _immediate_direction_reversals(rows, canonical_directions)
    legacy_immediate_counter_cycles = _immediate_direction_reversals(rows, legacy_directions)
    canonical_owner_switches = _owner_switches("storage_manager" for _row in rows)
    legacy_owner_switches = _owner_switches(
        ((row.get("legacy_baseline") or {}).get("owner"))
        if isinstance(row.get("legacy_baseline"), dict)
        else None
        for row in rows
    )
    stability_green = bool(
        canonical_direction_reversals <= legacy_direction_reversals
        and canonical_owner_switches <= legacy_owner_switches
        and canonical_immediate_counter_cycles == 0
    )
    pass_gate = bool(
        len(rows) == 60
        and applicable_cycles == 60
        and coherent == 60
        and fallback == 0
        and effectless == 60
        and runtime_green == 60
        and violation_cycles == 0
        and stability_green
    )
    return {
        "gate": SHADOW_GATE_NAME,
        "status": (
            "PASS"
            if pass_gate
            else "NOT_APPLICABLE"
            if rows and not_applicable_cycles == len(rows)
            else "FAIL"
        ),
        "not_applicable": bool(rows and not_applicable_cycles == len(rows)),
        "not_applicable_reason_code": (
            "DIRECT_MARKETING_DISABLED"
            if rows and not_applicable_cycles == len(rows)
            else None
        ),
        "cycles": len(rows),
        "applicable_cycles": applicable_cycles,
        "not_applicable_cycles": not_applicable_cycles,
        "coherent_cycles": coherent,
        "fallback_cycles": fallback,
        "effectless_cycles": effectless,
        "runtime_green_cycles": runtime_green,
        "violation_cycles": violation_cycles,
        "violation_fields": list(violation_fields),
        "canonical_direction_reversals": canonical_direction_reversals,
        "legacy_direction_reversals": legacy_direction_reversals,
        "canonical_immediate_counter_cycles": canonical_immediate_counter_cycles,
        "legacy_immediate_counter_cycles": legacy_immediate_counter_cycles,
        "canonical_owner_switches": canonical_owner_switches,
        "legacy_owner_switches": legacy_owner_switches,
        "stability_green": stability_green,
        "pass": pass_gate,
        "activation_effect": False,
    }
