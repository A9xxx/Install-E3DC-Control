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
    from Installer.storage_dispatch_contract import (
        DIRECT_MARKETING_ACTION_ROLES_SCHEMA,
        SHADOW_EXECUTION_READINESS_SCHEMA,
        SHADOW_EXECUTION_REVISION_KEYS,
        revision_hash,
        shadow_slot_forecast_complete,
        validate_canonical_plan,
    )
    from Installer.direct_marketing_actions import (
        DIRECT_MARKETING_CANONICAL_PLAN_ACTIONS,
        DIRECT_MARKETING_RUNTIME_PLAN_ACTIONS,
        STORAGE_ACTION_CONTRACTS,
        direct_marketing_action_contract,
        direct_marketing_export_gate_contract_valid,
        direct_marketing_plan_action_released,
        direct_marketing_source_action_mode_valid,
        direct_marketing_source_action_released,
        direct_marketing_target_for_plan_action,
        storage_action_transition_contract,
    )
    from Installer.storage_owner_paths import (
        PHASE5_COMPATIBLE_PRIMARY_PATHS,
        PHASE5_STRONGER_PRIMARY_PATHS,
        STORAGE_DECISION_PRIMARY_PATHS,
    )
except ModuleNotFoundError:  # pragma: no cover - direkter Installer-Start
    from storage_dispatch_contract import (  # type: ignore
        DIRECT_MARKETING_ACTION_ROLES_SCHEMA,
        SHADOW_EXECUTION_READINESS_SCHEMA,
        SHADOW_EXECUTION_REVISION_KEYS,
        revision_hash,
        shadow_slot_forecast_complete,
        validate_canonical_plan,
    )
    from direct_marketing_actions import (  # type: ignore
        DIRECT_MARKETING_CANONICAL_PLAN_ACTIONS,
        DIRECT_MARKETING_RUNTIME_PLAN_ACTIONS,
        STORAGE_ACTION_CONTRACTS,
        direct_marketing_action_contract,
        direct_marketing_export_gate_contract_valid,
        direct_marketing_plan_action_released,
        direct_marketing_source_action_mode_valid,
        direct_marketing_source_action_released,
        direct_marketing_target_for_plan_action,
        storage_action_transition_contract,
    )
    from storage_owner_paths import (  # type: ignore
        PHASE5_COMPATIBLE_PRIMARY_PATHS,
        PHASE5_STRONGER_PRIMARY_PATHS,
        STORAGE_DECISION_PRIMARY_PATHS,
    )


PHASE5_SCHEMA = "storage_dispatch_phase5_v1"
SHADOW_GATE_NAME = "SHADOW_60_GATE"
FIELD_MODE = "field_active"
SHADOW_MODE = "shadow"
DISABLED_MODE = "disabled"
VALID_ACTIONS = set(STORAGE_ACTION_CONTRACTS)
CHARGE_ACTIONS = {
    action
    for action, contract in STORAGE_ACTION_CONTRACTS.items()
    if contract.get("direction") == "charge"
}
DISCHARGE_ACTIONS = {
    action
    for action, contract in STORAGE_ACTION_CONTRACTS.items()
    if contract.get("direction") == "discharge"
}
REVISION_KEYS = {
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
    "confirmed_from_get_ack_unknown",
    "confirmed_from_live_readback",
    "confirmed_nonoptimal",
    "confirmed_unchanged",
})
POWER_SETTINGS_LIVE_READBACK_STATUS = "confirmed_from_live_readback"
POWER_SETTINGS_GET_ACK_UNKNOWN_STATUS = "confirmed_from_get_ack_unknown"
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
# Dieser Readinessblocker bleibt für die Kandidatenaktion selbst bindend. Nur
# wenn deren typisierter Wirtschaftsvertrag ausschließlich an freigegebenen
# Gewinnschwellen scheitert, darf Phase 5 daraus einen wirkungslosen HOLD
# auswählen. Er autorisiert weder Projektion noch Hardwarekommando.
ECONOMIC_HOLD_DERIVATIVE_BLOCKERS = frozenset({
    "CANONICAL_DIRECT_MARKETING_SLOT_NOT_SELECTED",
})
READY_NO_ACTION_INPUT_BLOCKERS = frozenset({
    "FORECAST_INPUT_BINDING_INVALID",
    "RESERVE_INPUT_BINDING_INVALID",
    "INPUT_BINDING_INCOMPLETE",
})
READY_NO_ACTION_SUPPRESSIBLE_BLOCKERS = frozenset({
    "SHADOW_INPUT_BINDING_INVALID",
    "FORECAST_SCENARIO_OR_RESERVE_CONTRACT_INCOMPLETE",
    "CURRENT_FORECAST_SCENARIO_INCOMPLETE",
    "CANONICAL_CANDIDATE_INVALID",
    "DIRECT_MARKETING_ACTION_NOT_EXECUTION_READY",
    "CANONICAL_DIRECT_MARKETING_SLOT_NOT_SELECTED",
    "CANONICAL_GRID_CHARGE_SELECTION_CONTRACT_UNAVAILABLE",
    "CANONICAL_HEADROOM_EXPORT_NOT_RELEASED",
})
READY_NO_ACTION_ALLOWED_BLOCKERS = frozenset({
    "PHASE5_MODE_SHADOW",
    *READY_NO_ACTION_SUPPRESSIBLE_BLOCKERS,
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
        "optimizer_status": None,
        "execution_readiness_status": None,
        "execution_ready": False,
        "execution_readiness_blockers": [],
    }
    if not validation.get("valid"):
        return result
    shadow = plan.get("shadow_dispatch") if isinstance(plan.get("shadow_dispatch"), dict) else {}
    optimizer_status = str(
        shadow.get("optimizer_status") or shadow.get("status") or ""
    )
    readiness = (
        shadow.get("execution_readiness_contract")
        if isinstance(shadow.get("execution_readiness_contract"), dict)
        else {}
    )
    result.update({
        "optimizer_status": optimizer_status or None,
        "execution_readiness_status": readiness.get("status"),
        "execution_ready": readiness.get("execution_ready") is True,
        "execution_readiness_blockers": list(
            readiness.get("blockers") or []
        ),
    })
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
    if shadow.get("fallback") or optimizer_status not in {
        "SHADOW_OK",
        "SHADOW_HEADROOM_PARTIAL",
    }:
        fallback_reason = str(
            shadow.get("fallback_reason_code")
            or ""
        ).strip()
        result["block_reason_code"] = fallback_reason or "SHADOW_FALLBACK_OR_STATUS_INVALID"
        result["shadow_fallback_reason_code"] = fallback_reason or None
        return result
    plan_slot = validation.get("slot") if isinstance(validation.get("slot"), dict) else {}
    shadow_slots = [
        item
        for item in shadow.get("slots") or []
        if (
            isinstance(item, dict)
            and item.get("slot_id") == validation.get("slot_id")
            and _int(item.get("start_ts_ms")) == _int(plan_slot.get("start_ts_ms"))
            and _int(item.get("end_ts_ms")) == _int(plan_slot.get("end_ts_ms"))
        )
    ]
    if len(shadow_slots) != 1:
        result["block_reason_code"] = (
            "CURRENT_SHADOW_SLOT_AMBIGUOUS"
            if len(shadow_slots) > 1
            else "CURRENT_SHADOW_SLOT_MISSING_OR_MISMATCH"
        )
        return result
    shadow_slot = shadow_slots[0]
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
        require(_complete_revisions(plan), "INPUT_BINDING_REVISIONS_INCOMPLETE")
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
    require(_complete_revisions(plan), "INPUT_BINDING_REVISIONS_INCOMPLETE")
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
        and forecast.get("pv_e3dc_dc_source_revision")
        == revisions.get("pv_e3dc_dc_ensemble")
        and forecast.get("pv_external_ac_source_revision")
        == revisions.get("pv_external_ac_ensemble")
        and forecast.get("load_source_revision") == revisions.get("load_ensemble")
        and forecast.get("calibration_source_revision")
        == revisions.get("forecast_calibration")
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
    return shadow_slot_forecast_complete(slot)


def _legacy_phase5_force_export_reentry_contract(
    legacy: Dict[str, Any],
    path_contract: Dict[str, Any],
    resolved: Dict[str, Any],
) -> Dict[str, Any]:
    """Erkennt ausschließlich den eigenen fail-closed Zustand desselben Slots.

    Der vorangehende Managerpfad darf einen von Phase 5 selbst erzeugten
    Ladeblock im Folgezyklus nicht als konkurrierenden Legacy-Owner werten.
    Plan- oder Slotwechsel, zusätzliche Pfade und jedes stärkere Veto bleiben
    ausdrücklich ausgeschlossen.
    """

    blocked = (
        legacy.get("force_export_blocked")
        if isinstance(legacy.get("force_export_blocked"), dict)
        else {}
    )
    auto_limit = (
        legacy.get("auto_limit")
        if isinstance(legacy.get("auto_limit"), dict)
        else {}
    )
    active_paths = {
        str(value)
        for value in path_contract.get("active_paths") or []
        if isinstance(value, str) and value
    }
    veto_reasons = {
        str(value)
        for value in path_contract.get("veto_reasons") or []
        if isinstance(value, str) and value
    }
    valid = bool(
        legacy.get("state") == "direct_marketing_phase5_force_export_blocked"
        and legacy.get("priority") == "direct_marketing"
        and legacy.get("protected") is True
        and _int(legacy.get("mode"), -1) == 0
        and _int(legacy.get("val"), -1) == 0
        and legacy.get("direct_marketing_action") == "FORCE_EXPORT_BLOCKED"
        and blocked.get("schema") == "phase5_force_export_blocked_v1"
        and blocked.get("valid") is True
        and blocked.get("single_output_owner") == "storage_manager"
        and blocked.get("plan_id") == resolved.get("plan_id")
        and blocked.get("slot_id") == resolved.get("slot_id")
        and _int(blocked.get("max_charge_w"), -1) == 0
        and _int(blocked.get("max_discharge_w"), -1) >= 300
        and auto_limit.get("enabled") is True
        and auto_limit.get("release") is False
        and _int(auto_limit.get("max_charge_w"), -1) == 0
        and _int(auto_limit.get("max_discharge_w"), -1) >= 300
        and path_contract.get("primary_path") == "direct_marketing"
        and active_paths == {"direct_marketing"}
        and veto_reasons.issubset({"direct_marketing:commands_blocked"})
    )
    return {
        "schema": "phase5_force_export_reentry_v1",
        "valid": valid,
        "plan_id": blocked.get("plan_id"),
        "slot_id": blocked.get("slot_id"),
        "active_paths": sorted(active_paths),
        "veto_reasons": sorted(veto_reasons),
    }


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


def _typed_power_settings_values(values: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(values, dict) or not isinstance(values.get("limits_used"), bool):
        return None
    numeric = {}
    for key in ("max_charge_w", "max_discharge_w", "discharge_start_w"):
        value = values.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        numeric[key] = value
    return {"limits_used": values["limits_used"], **numeric}


def _power_settings_values_match(
    requested: Any,
    readback: Any,
    bounded_zero_w: Any,
) -> bool:
    target = _typed_power_settings_values(requested)
    actual = _typed_power_settings_values(readback)
    if target is None or actual is None or actual["limits_used"] is not target["limits_used"]:
        return False
    if target["limits_used"] is False:
        return True
    bounded = (
        int(bounded_zero_w)
        if isinstance(bounded_zero_w, int)
        and not isinstance(bounded_zero_w, bool)
        and bounded_zero_w >= 0
        else 0
    )
    charge_matches = actual["max_charge_w"] == target["max_charge_w"]
    if target["max_charge_w"] == 0 and bounded > 0:
        charge_matches = 0 <= actual["max_charge_w"] <= bounded
    return bool(
        charge_matches
        and actual["max_discharge_w"] == target["max_discharge_w"]
        and actual["discharge_start_w"] == target["discharge_start_w"]
    )


def _power_settings_contract(
    settings: Dict[str, Any],
    *,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
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
    schema_valid = bool(
        settings.get("schema") == POWER_SETTINGS_SCHEMA
        and settings.get("contract_version") == POWER_SETTINGS_CONTRACT_VERSION
    )
    evidence_ts_ms = _int(
        settings.get("readback_cycle_ts", settings.get("ts")),
        0,
    )
    if 0 < evidence_ts_ms < 100_000_000_000:
        evidence_ts_ms *= 1000
    evidence_age_s = (
        (int(now_ms) - evidence_ts_ms) / 1000.0
        if now_ms is not None and evidence_ts_ms > 0
        else None
    )
    evidence_fresh = bool(
        evidence_ts_ms > 0
        and (
            evidence_age_s is None
            or -5.0 <= evidence_age_s <= 30.0
        )
    )
    readback = settings.get("readback")
    requested = settings.get("requested")
    readback_values_valid = _typed_power_settings_values(readback) is not None
    target_values_match = _power_settings_values_match(
        requested,
        readback,
        settings.get("bounded_zero_w"),
    )
    live_readback_valid = True
    if status == POWER_SETTINGS_LIVE_READBACK_STATUS:
        live_readback_valid = bool(
            schema_valid
            and settings.get("stage") == "live_reconciliation"
            and settings.get("readback_source") == "canonical_live"
            and ("fresh" not in settings or settings.get("fresh") is True)
            and ("valid" not in settings or settings.get("valid") is True)
            and readback_values_valid
            and evidence_fresh
        )
    get_ack_unknown_valid = True
    if status == POWER_SETTINGS_GET_ACK_UNKNOWN_STATUS:
        response_codes = settings.get("response_codes")
        set_response_unknown = bool(
            response_codes is None
            or (isinstance(response_codes, list) and len(response_codes) < 4)
        )
        get_ack_unknown_valid = bool(
            schema_valid
            and settings.get("stage") == "target"
            and settings.get("readback_source") == "command_get_after_invalid_set_response"
            and settings.get("acknowledged") is None
            and settings.get("acknowledgement_status") == "unknown_invalid_set_response"
            and set_response_unknown
            and target_values_match
            and evidence_fresh
        )
    direct_confirmed_status = status in {
        "confirmed",
        "confirmed_bounded_zero",
        "confirmed_nonoptimal",
    }
    direct_confirmation_valid = True
    if direct_confirmed_status:
        response_codes = settings.get("response_codes")
        direct_confirmation_valid = bool(
            schema_valid
            and settings.get("stage") == "target"
            and isinstance(response_codes, list)
            and len(response_codes) >= 4
            and all(
                isinstance(code, int)
                and not isinstance(code, bool)
                and code in (0, 1)
                for code in response_codes
            )
            and target_values_match
            and evidence_fresh
        )
    unchanged_valid = True
    if status == "confirmed_unchanged":
        unchanged_valid = bool(
            schema_valid
            and settings.get("readback_source") == "canonical_live"
            and settings.get("readback_cycle_ts") is not None
            and target_values_match
            and evidence_fresh
        )
    valid = bool(
        settings.get("confirmed") is True
        and status in POWER_SETTINGS_CONFIRMED_STATUSES
        and timers_valid
        and schema_valid
        and evidence_fresh
        and live_readback_valid
        and get_ack_unknown_valid
        and direct_confirmation_valid
        and unchanged_valid
    )
    return {
        "valid": valid,
        "status": status or None,
        "retry_remaining_s": retry_s,
        "pending_remaining_s": pending_s,
        "timers_valid": timers_valid,
        "schema_valid": schema_valid,
        "evidence_ts_ms": evidence_ts_ms or None,
        "evidence_age_s": round(evidence_age_s, 3) if evidence_age_s is not None else None,
        "evidence_fresh": evidence_fresh,
        "readback_values_valid": readback_values_valid,
        "target_values_match": target_values_match,
        "live_readback_valid": live_readback_valid,
        "get_ack_unknown_valid": get_ack_unknown_valid,
        "direct_confirmation_valid": direct_confirmation_valid,
        "unchanged_valid": unchanged_valid,
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


def _headroom_export_gate_valid(
    gate: Dict[str, Any],
    *,
    planned_w: Optional[float],
    action_horizon: Dict[str, Any],
    window_end_ts_ms: int,
) -> bool:
    """Validiert die vollständig materialisierte Headroom-Energiekante."""

    deficit_wh = _float(gate.get("headroom_deficit_wh"), None)
    sellable_wh = _float(gate.get("sellable_wh"), None)
    protected_reserve_wh = _float(gate.get("protected_reserve_wh"), None)
    energy_budget_wh = _float(
        gate.get("headroom_export_energy_budget_wh"),
        None,
    )
    planned_slot_energy_wh = _float(gate.get("planned_slot_energy_wh"), None)
    selected_slot_energy_wh = _float(gate.get("selected_slot_energy_wh"), None)
    selected_planned_w = _float(gate.get("selected_planned_w"), None)
    target_soc_pct = _float(gate.get("headroom_target_soc_pct"), None)
    next_charge_start_ms = _int(
        gate.get("next_charge_window_start_ts_ms"),
        0,
    )
    return bool(
        gate.get("schema_version")
        == "direct_marketing_headroom_export_gate_v1"
        and gate.get("allowed") is True
        and not gate.get("blockers")
        and planned_w is not None
        and planned_w >= 300.0
        and selected_planned_w is not None
        and abs(selected_planned_w - planned_w) <= 1.0
        and protected_reserve_wh is not None
        and protected_reserve_wh >= 0.0
        and sellable_wh is not None
        and sellable_wh > 0.0
        and deficit_wh is not None
        and 0.0 < deficit_wh <= sellable_wh + 1.0
        and energy_budget_wh is not None
        and energy_budget_wh > 0.0
        and planned_slot_energy_wh is not None
        and 0.0 < planned_slot_energy_wh <= energy_budget_wh + 1.0
        and selected_slot_energy_wh is not None
        and 0.0 < selected_slot_energy_wh <= energy_budget_wh + 1.0
        and selected_slot_energy_wh <= planned_slot_energy_wh + 1.0
        and target_soc_pct is not None
        and 0.0 <= target_soc_pct <= 100.0
        and next_charge_start_ms >= window_end_ts_ms > 0
        and gate.get("energy_accounting_contract")
        == "FINITE_HEADROOM_DEFICIT_NO_SLOT_DOUBLE_SPEND"
        and gate.get("action_horizon_contract") == action_horizon
    )


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
    action_lineage_id = projection.get(
        "direct_marketing_plan_action_lineage_id"
    )
    gate_lineage_id = projection.get("direct_marketing_gate_lineage_id")
    gate_generation = projection.get("direct_marketing_gate_generation")
    gate_generation_id = projection.get(
        "direct_marketing_gate_generation_id"
    )
    segment_id = projection.get("direct_marketing_plan_segment_id")
    planned_w = _float(projection.get("direct_marketing_planned_w"), None)
    window_id = projection.get("direct_marketing_window_id")
    window_valid = isinstance(window_id, str) and bool(window_id.strip())
    window_start_ts_ms = _int(
        projection.get("direct_marketing_window_start_ts_ms"),
        0,
    )
    window_end_ts_ms = _int(
        projection.get("direct_marketing_window_end_ts_ms"),
        0,
    )
    action_identity_material = {
            "action": action,
            "window_id": window_id,
            "window_start_ts_ms": window_start_ts_ms,
            "window_end_ts_ms": window_end_ts_ms,
    }
    if action == "ECONOMIC_EXPORT":
        action_identity_material.update({
            "gate_lineage_id": gate_lineage_id,
            "gate_generation": gate_generation,
            "gate_generation_id": gate_generation_id,
        })
    expected_action_id = (
        revision_hash(action_identity_material)
        if action and window_valid and window_start_ts_ms < window_end_ts_ms
        else None
    )
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
    economic_lineage = (
        economic_export_gate.get("export_window_gate_lineage")
        if isinstance(
            economic_export_gate.get("export_window_gate_lineage"),
            dict,
        )
        else {}
    )
    foreign_export_lineage_claim = bool(
        action != "ECONOMIC_EXPORT"
        and (
            gate_lineage_id is not None
            or gate_generation is not None
            or gate_generation_id is not None
            or economic_export_gate
        )
    )
    headroom_export_gate = (
        projection.get("direct_marketing_headroom_export_gate")
        if isinstance(
            projection.get("direct_marketing_headroom_export_gate"),
            dict,
        )
        else {}
    )
    live_dc_fallback = (
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
    fallback_claimed = bool(live_dc_fallback)
    slot_prices = (
        plan_slot.get("prices_ct_kwh")
        if isinstance(plan_slot.get("prices_ct_kwh"), dict)
        else {}
    )
    fallback_raw_price = _float(
        live_dc_fallback.get("raw_market_price_ct_kwh"),
        None,
    )
    fallback_target_soc = _float(
        live_dc_fallback.get("target_soc_pct"),
        None,
    )
    fallback_runtime_cap_w = _float(
        live_dc_fallback.get("runtime_cap_w"),
        None,
    )
    live_dc_fallback_valid = bool(
        not fallback_claimed
        or (
            action == "PV_STORE"
            and live_dc_fallback.get("schema_version")
            == "direct_marketing_pv_store_auto_dc_permission_v2"
            and live_dc_fallback.get("valid") is True
            and live_dc_fallback.get("forecast_imputed") is False
            and live_dc_fallback.get("soc_effect") is False
            and live_dc_fallback.get("execution_semantics")
            == "PV_STORE_E3DC_AUTO_DC_PERMISSION"
            and live_dc_fallback.get("runtime_measurement_required") is False
            and live_dc_fallback.get("runtime_source_contract")
            in {
                "E3DC_DC_AUTO_CAP_RAW_PRICE",
                "E3DC_DC_AUTO_CAP_LUOX_ZERO_EXPORT",
            }
            and live_dc_fallback.get("source") == "E3DC_DC"
            and live_dc_fallback.get("dc_only") is True
            and live_dc_fallback.get("aux_ac_allowed") is False
            and live_dc_fallback.get("grid_ac_allowed") is False
            and fallback_raw_price is not None
            and fallback_raw_price < 0.0
            and _float(slot_prices.get("gross_sell"), None) is not None
            and abs(
                fallback_raw_price
                - (_float(slot_prices.get("gross_sell"), 0.0) or 0.0)
            )
            <= 0.0001
            and slot_prices.get("fresh") is True
            and str(live_dc_fallback.get("tariff_revision") or "")
            == str(slot_prices.get("tariff_revision") or "")
            and bool(str(slot_prices.get("tariff_revision") or ""))
            and str(live_dc_fallback.get("market_window_id") or "")
            == str(window_id or "")
            and _int(
                live_dc_fallback.get("market_window_end_ts_ms"),
                0,
            )
            >= window_end_ts_ms
            and fallback_target_soc is not None
            and 0.0 < fallback_target_soc <= 100.0
            and fallback_runtime_cap_w is not None
            and fallback_runtime_cap_w >= 300.0
        )
    )
    action_horizon_valid = bool(
        action_horizon_contract.get("schema_version") == ACTION_HORIZON_SCHEMA
        and action_horizon_contract.get("action") == action
        and action_horizon_contract.get("complete") is True
    )
    economic_gate_valid = bool(
        (
            action != "ECONOMIC_EXPORT"
            and not foreign_export_lineage_claim
        )
        or (
            action == "ECONOMIC_EXPORT"
            and economic_export_gate.get("allowed") is True
            and not economic_export_gate.get("blockers")
            and _float(economic_export_gate.get("margin_ct_kwh"), None) is not None
            and _float(economic_export_gate.get("user_min_margin_ct"), None) is not None
            and _float(economic_export_gate.get("expected_profit_eur"), None) is not None
            and _float(economic_export_gate.get("min_window_profit_eur"), None) is not None
            and isinstance(gate_lineage_id, str)
            and bool(gate_lineage_id)
            and type(gate_generation) is int
            and gate_generation >= 1
            and isinstance(gate_generation_id, str)
            and bool(gate_generation_id)
            and economic_lineage.get("status") == "ACTIVE"
            and economic_lineage.get("gate_lineage_id") == gate_lineage_id
            and economic_lineage.get("current_generation")
            == gate_generation
            and economic_lineage.get("current_generation_id")
            == gate_generation_id
        )
    )
    headroom_gate_valid = bool(
        action != "HEADROOM_EXPORT"
        or _headroom_export_gate_valid(
            headroom_export_gate,
            planned_w=planned_w,
            action_horizon=action_horizon_contract,
            window_end_ts_ms=window_end_ts_ms,
        )
    )
    target_state = direct_marketing_target_for_plan_action(action)
    action_contract = direct_marketing_action_contract(target_state)
    action_released = direct_marketing_source_action_released(
        target_state,
        source_action,
    )
    expected_source_actions = set(
        (action_contract or {}).get("source_actions") or ()
    )
    source_mode_valid = bool(
        source_mode in set((action_contract or {}).get("source_modes") or ())
    )
    source_action_mode_valid = direct_marketing_source_action_mode_valid(
        target_state,
        source_action,
        source_mode,
    )
    budget_key = (action_contract or {}).get("budget_key")
    selected_contract_valid = bool(
        candidate
        and selected
        and executable
        and commands_allowed
        and action_released
        and action in DIRECT_MARKETING_CANONICAL_PLAN_ACTIONS
        and source_action in expected_source_actions
        and source_mode_valid
        and source_action_mode_valid
        and source_mode_matches_plan
        and planned_w is not None
        and (planned_w >= 300.0 if budget_key is not None else planned_w == 0.0)
        and window_valid
        and isinstance(action_id, str)
        and action_id == expected_action_id
        and action_lineage_id == action_id
        and projection.get(
            "direct_marketing_plan_source_action_execution_released"
        ) is True
        and isinstance(segment_id, str)
        and bool(segment_id)
        and action_horizon_valid
        and economic_gate_valid
        and headroom_gate_valid
        and live_dc_fallback_valid
    )
    partial_selection = any((selected, executable, commands_allowed, (planned_w or 0.0) > 0.0, window_valid))
    reason_code = None
    if foreign_export_lineage_claim:
        reason_code = "CANONICAL_DIRECT_MARKETING_FOREIGN_EXPORT_LINEAGE"
    elif partial_selection and not selected_contract_valid:
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
        "source_action_mode_valid": source_action_mode_valid,
        "plan_source_mode": plan_source_mode or None,
        "source_mode_matches_plan": source_mode_matches_plan,
        "action_id": action_id if isinstance(action_id, str) and action_id else None,
        "action_lineage_id": (
            action_lineage_id
            if isinstance(action_lineage_id, str) and action_lineage_id
            else None
        ),
        "gate_lineage_id": gate_lineage_id,
        "gate_generation": gate_generation,
        "gate_generation_id": gate_generation_id,
        "foreign_export_lineage_claim": foreign_export_lineage_claim,
        "segment_id": segment_id if isinstance(segment_id, str) and segment_id else None,
        "planned_w": round(planned_w, 3) if planned_w is not None else None,
        "window_id": window_id if window_valid else None,
        "window_start_ts_ms": window_start_ts_ms or None,
        "window_end_ts_ms": window_end_ts_ms or None,
        "action_horizon_contract": copy.deepcopy(action_horizon_contract) if action_horizon_contract else None,
        "economic_export_gate": copy.deepcopy(economic_export_gate) if economic_export_gate else None,
        "headroom_export_gate": copy.deepcopy(headroom_export_gate) if headroom_export_gate else None,
        "pv_store_live_dc_fallback_contract": copy.deepcopy(
            live_dc_fallback
        ) if live_dc_fallback else None,
        "valid_selected_contract": selected_contract_valid,
        "reason_code": reason_code,
    }


def _active_direct_marketing_policy_contract(
    plan: Dict[str, Any],
    resolved: Dict[str, Any],
    canonical_slot: Dict[str, Any],
    now_ms: int,
) -> Dict[str, Any]:
    """Bindet Owner, aktuelle Policy und Planfenster an genau einen Slot."""

    blockers: List[str] = []

    def block(condition: bool, code: str) -> None:
        if condition and code not in blockers:
            blockers.append(code)

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
    slot = (
        resolved.get("plan_slot")
        if isinstance(resolved.get("plan_slot"), dict)
        else {}
    )
    slot_start = _int(slot.get("start_ts_ms"), 0)
    slot_end = _int(slot.get("end_ts_ms"), 0)
    mode = _normalized_direct_marketing_mode(direct.get("mode"))
    source_mode = _normalized_direct_marketing_mode(
        canonical_slot.get("source_mode")
    )
    expected_plan_owner = f"direct_marketing:{mode}" if mode else ""

    block(direct.get("active") is not True, "DIRECT_MARKETING_ACTIVE_FLAG_INVALID")
    block(direct.get("shadow") is not False, "DIRECT_MARKETING_SHADOW_FLAG_INVALID")
    block(
        direct.get("controller_owner") != "storage_manager",
        "DIRECT_MARKETING_CONTROLLER_OWNER_MISMATCH",
    )
    block(
        not expected_plan_owner
        or direct.get("plan_owner") != expected_plan_owner,
        "DIRECT_MARKETING_PLAN_OWNER_MISMATCH",
    )
    block(
        type(direct.get("owner_contract_version")) is not int
        or direct.get("owner_contract_version") != 1,
        "DIRECT_MARKETING_OWNER_CONTRACT_VERSION_INVALID",
    )
    block(
        type(flags.get("owner_contract_version")) is not int
        or flags.get("owner_contract_version") != 1,
        "DIRECT_MARKETING_FLAGS_OWNER_CONTRACT_VERSION_INVALID",
    )
    block(
        flags.get("commands_allowed") is not True,
        "DIRECT_MARKETING_COMMANDS_NOT_ALLOWED",
    )
    block(
        not mode or not source_mode or source_mode != mode,
        "DIRECT_MARKETING_SOURCE_MODE_MISMATCH",
    )

    created_ts = _int(direct.get("created_ts"), 0)
    valid_until_ts = _int(direct.get("valid_until_ts"), 0)
    block(
        created_ts < 10_000_000_000
        or created_ts > int(now_ms) + 60_000
        or valid_until_ts < slot_end
        or not (created_ts <= int(now_ms) < valid_until_ts),
        "DIRECT_MARKETING_VALIDITY_INVALID",
    )

    overlapping_slots = [
        item
        for item in (
            plan.get("slots")
            if isinstance(plan.get("slots"), list)
            else []
        )
        if (
            isinstance(item, dict)
            and _int(item.get("start_ts_ms"), 0) < slot_end
            and _int(item.get("end_ts_ms"), 0) > slot_start
        )
    ]
    block(
        len(overlapping_slots) != 1,
        (
            "CURRENT_CANONICAL_PLAN_SLOT_AMBIGUOUS"
            if len(overlapping_slots) > 1
            else "CURRENT_CANONICAL_PLAN_SLOT_MISSING"
        ),
    )
    block(
        len(overlapping_slots) == 1
        and (
            overlapping_slots[0] != slot
            or overlapping_slots[0].get("slot_id") != resolved.get("slot_id")
        ),
        "CURRENT_CANONICAL_PLAN_SLOT_IDENTITY_MISMATCH",
    )

    timeline = (
        direct.get("policy_timeline")
        if isinstance(direct.get("policy_timeline"), list)
        else []
    )
    policy = (
        direct.get("policy_decision")
        if isinstance(direct.get("policy_decision"), dict)
        else {}
    )
    # Die Timeline enthält auch überlappende, unterdrückte Kandidaten. Nur die
    # exakt veröffentlichte Policy-Generation muss eindeutig vorkommen; ihr
    # Action-/Fenster-/Slotvertrag wird anschließend vollständig geprüft.
    matching_policies = [
        item
        for item in timeline
        if isinstance(item, dict) and item == policy
    ]
    block(
        len(matching_policies) != 1,
        (
            "CURRENT_POLICY_TIMELINE_AMBIGUOUS"
            if len(matching_policies) > 1
            else "CURRENT_POLICY_TIMELINE_MISSING"
        ),
    )
    block(
        len(matching_policies) != 1,
        "CURRENT_POLICY_TIMELINE_IDENTITY_MISMATCH",
    )

    selected = (
        policy.get("selected_window")
        if isinstance(policy.get("selected_window"), dict)
        else {}
    )
    execution = (
        policy.get("execution_window")
        if isinstance(policy.get("execution_window"), dict)
        else {}
    )
    budget = (
        policy.get("storage_budget")
        if isinstance(policy.get("storage_budget"), dict)
        else {}
    )
    action = str(canonical_slot.get("action") or "").upper()
    expected_target = direct_marketing_target_for_plan_action(action)
    action_contract = direct_marketing_action_contract(expected_target)
    action_released = bool(
        action_contract
        and action_contract.get("canonical_execution_released") is True
    )
    block(not action_released, "CURRENT_POLICY_ACTION_UNSUPPORTED")
    expected_source_actions = set(
        (action_contract or {}).get("source_actions") or ()
    )
    allowed_modes = set((action_contract or {}).get("source_modes") or ())
    expected_source_action = str(canonical_slot.get("source_action") or "")
    source_action_released = direct_marketing_source_action_released(
        expected_target,
        expected_source_action,
    )
    source_action_mode_valid = direct_marketing_source_action_mode_valid(
        expected_target,
        expected_source_action,
        source_mode,
    )
    policy_source_action = str(policy.get("source_action") or "")
    block(
        not (
            policy.get("schema") == "direct_marketing_policy_v1"
            and policy.get("blocked") is False
            and policy.get("commands_allowed") is True
            and str(policy.get("dv_target_state") or "").strip().upper()
            == expected_target
            and source_action_released
            and source_action_mode_valid
            and expected_source_action in expected_source_actions
            and policy_source_action == expected_source_action
            and str(policy.get("executable_action") or "")
            == expected_source_action
            and str(selected.get("action") or "")
            == expected_source_action
            and str(execution.get("action") or "")
            == expected_source_action
            and source_mode in allowed_modes
        ),
        "CURRENT_POLICY_ACTION_IDENTITY_MISMATCH",
    )
    block(
        type(execution.get("contract_version")) is not int
        or execution.get("contract_version") != 1
        or execution.get("source") != "active_plan_window",
        "CURRENT_POLICY_EXECUTION_CONTRACT_INVALID",
    )

    policy_start = _int(policy.get("start_ts"), 0)
    policy_end = _int(policy.get("end_ts"), 0)
    selected_start = _int(selected.get("start_ts"), 0)
    selected_end = _int(selected.get("end_ts"), 0)
    execution_start = _int(execution.get("start_ts"), 0)
    execution_end = _int(execution.get("end_ts"), 0)
    block(
        not (
            policy_start <= slot_start < slot_end <= policy_end
            and selected_start <= slot_start < slot_end <= selected_end
            and execution_start <= slot_start < slot_end <= execution_end
            and policy_start
            <= execution_start
            < execution_end
            <= policy_end
            and selected_start
            <= execution_start
            < execution_end
            <= selected_end
        ),
        "CURRENT_POLICY_SLOT_BOUNDS_MISMATCH",
    )

    # ``window_id`` ist die stabile Geschäftsfenster-ID der Policy-Lineage.
    # Das konkrete Fenster in ``direct.windows`` besitzt dagegen eine eigene
    # Planfenster-ID; beide IDs dürfen nach einem Replan bewusst verschieden
    # sein.
    business_window_id = str(execution.get("window_id") or "")
    plan_window_id = str(execution.get("plan_window_id") or "")
    plan_window_start = _int(execution.get("plan_window_start_ts"), 0)
    plan_window_end = _int(execution.get("plan_window_end_ts"), 0)
    canonical_window_start = _int(
        canonical_slot.get("window_start_ts_ms"),
        0,
    )
    canonical_window_end = _int(
        canonical_slot.get("window_end_ts_ms"),
        0,
    )
    block(
        not (
            business_window_id
            and canonical_slot.get("window_id") == business_window_id
            and plan_window_start == canonical_window_start
            and plan_window_end == canonical_window_end
            and plan_window_start <= execution_start
            < execution_end <= plan_window_end
        ),
        "CURRENT_POLICY_WINDOW_IDENTITY_MISMATCH",
    )
    matching_windows = [
        item
        for item in (
            direct.get("windows")
            if isinstance(direct.get("windows"), list)
            else []
        )
        if (
            isinstance(item, dict)
            and str(item.get("action") or "") == expected_source_action
            and _int(item.get("start_ts"), 0) == plan_window_start
            and _int(item.get("end_ts"), 0) == plan_window_end
            and (
                not plan_window_id
                or str(
                    item.get("export_plateau_id")
                    or item.get("market_window_id")
                    or item.get("window_id")
                    or ""
                )
                == plan_window_id
            )
        )
    ]
    block(
        len(matching_windows) != 1,
        "CURRENT_POLICY_PLAN_WINDOW_AMBIGUOUS_OR_MISSING",
    )

    def finite_number(value: Any) -> bool:
        return bool(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
        )

    protected_reserve_wh = budget.get("protected_reserve_wh")
    sellable_wh = budget.get("sellable_wh")
    block(
        not finite_number(protected_reserve_wh)
        or float(protected_reserve_wh) < 0.0
        or not finite_number(sellable_wh)
        or float(sellable_wh) < 0.0,
        "CURRENT_POLICY_RESERVE_BUDGET_INVALID",
    )
    budget_key = (action_contract or {}).get("budget_key")
    planned_w = canonical_slot.get("planned_w")
    if budget_key:
        budget_w = budget.get(str(budget_key))
        block(
            not finite_number(budget_w)
            or float(budget_w) <= 0.0
            or not finite_number(planned_w)
            or float(planned_w) < 300.0
            or float(planned_w) > float(budget_w) + 0.001,
            "CURRENT_POLICY_POWER_BUDGET_INVALID",
        )
    else:
        block(
            not finite_number(planned_w) or float(planned_w) != 0.0,
            "CURRENT_POLICY_WAIT_BUDGET_INVALID",
        )

    if action == "HEADROOM_EXPORT":
        headroom_gate = (
            canonical_slot.get("headroom_export_gate")
            if isinstance(canonical_slot.get("headroom_export_gate"), dict)
            else {}
        )
        matching_window = matching_windows[0] if len(matching_windows) == 1 else {}
        gate_deficit_wh = _float(headroom_gate.get("headroom_deficit_wh"), None)
        gate_required_wh = _float(headroom_gate.get("headroom_required_wh"), None)
        gate_free_wh = _float(headroom_gate.get("headroom_free_before_wh"), None)
        gate_absorption_wh = _float(headroom_gate.get("forecast_absorption_wh"), None)
        policy_budget_w = _float(headroom_gate.get("policy_export_budget_w"), None)
        block(
            not _headroom_export_gate_valid(
                headroom_gate,
                planned_w=_float(planned_w, None),
                action_horizon=(
                    canonical_slot.get("action_horizon_contract")
                    if isinstance(canonical_slot.get("action_horizon_contract"), dict)
                    else {}
                ),
                window_end_ts_ms=canonical_window_end,
            )
            or budget.get("headroom_hold_active") is not False
            or selected.get("headroom_export_selected") is not True
            or matching_window.get("headroom_export_selected") is not True
            or gate_deficit_wh is None
            or abs(
                gate_deficit_wh
                - (_float(budget.get("headroom_deficit_wh"), -1.0) or -1.0)
            ) > 1.0
            or gate_required_wh is None
            or abs(
                gate_required_wh
                - (_float(budget.get("headroom_required_wh"), -1.0) or -1.0)
            ) > 1.0
            or gate_free_wh is None
            or abs(
                gate_free_wh
                - (_float(budget.get("headroom_free_before_wh"), -1.0) or -1.0)
            ) > 1.0
            or gate_absorption_wh is None
            or abs(
                gate_absorption_wh
                - (
                    _float(
                        (
                            policy.get("economics")
                            if isinstance(policy.get("economics"), dict)
                            else {}
                        ).get("forecast_absorption_wh"),
                        -1.0,
                    )
                    or -1.0
                )
            ) > 1.0
            or policy_budget_w is None
            or not finite_number(budget.get("export_budget_w"))
            or policy_budget_w > float(budget.get("export_budget_w")) + 0.001
            or _int(headroom_gate.get("next_charge_window_start_ts_ms"), 0)
            != _int(selected.get("next_charge_window_start_ts"), -1),
            "CURRENT_POLICY_HEADROOM_GATE_INVALID",
        )

    return {
        "valid": not blockers,
        "evidence_status": "COMPLETE" if not blockers else "EVIDENCE_LIMIT",
        "blockers": blockers,
        "mode": mode or None,
        "plan_owner": direct.get("plan_owner"),
        "controller_owner": direct.get("controller_owner"),
        "policy": copy.deepcopy(policy) if policy else None,
        "selected_window": copy.deepcopy(selected) if selected else None,
        "execution_window": copy.deepcopy(execution) if execution else None,
        "plan_window": (
            copy.deepcopy(matching_windows[0])
            if len(matching_windows) == 1
            else None
        ),
        "target_state": expected_target or None,
        "source_action": policy_source_action or None,
        "window_id": business_window_id or None,
        "plan_window_id": plan_window_id or None,
    }


def active_direct_marketing_binding_contract(
    plan: Dict[str, Any],
    now_ms: int,
) -> Dict[str, Any]:
    """Prüft aktive DV-Evidenz identisch für Phase 5 und Legacy-Executor."""

    blockers: List[str] = []
    if not isinstance(plan, dict):
        return {
            "valid": False,
            "evidence_status": "EVIDENCE_LIMIT",
            "blockers": ["CANONICAL_PLAN_REQUIRED"],
            "plan_id": None,
            "slot_id": None,
            "canonical_slot": None,
        }

    resolved = resolve_current_shadow_slot(plan, int(now_ms))
    shadow = (
        plan.get("shadow_dispatch")
        if isinstance(plan.get("shadow_dispatch"), dict)
        else {}
    )
    slot = (
        resolved.get("plan_slot")
        if isinstance(resolved.get("plan_slot"), dict)
        else {}
    )
    prices = (
        slot.get("prices_ct_kwh")
        if isinstance(slot.get("prices_ct_kwh"), dict)
        else {}
    )
    reserve = (
        shadow.get("reserve_contract")
        if isinstance(shadow.get("reserve_contract"), dict)
        else {}
    )
    terminal = (
        shadow.get("terminal_value")
        if isinstance(shadow.get("terminal_value"), dict)
        else {}
    )
    decision_horizon = (
        shadow.get("decision_horizon")
        if isinstance(shadow.get("decision_horizon"), dict)
        else {}
    )
    input_binding = _shadow_input_binding_contract(
        plan,
        shadow,
        resolved,
    )
    price_horizon = _price_horizon_activation_contract(shadow)
    canonical_slot = _canonical_direct_marketing_slot_contract(plan, slot)
    active_policy = _active_direct_marketing_policy_contract(
        plan,
        resolved,
        canonical_slot,
        int(now_ms),
    )

    _append(
        blockers,
        resolved.get("valid") is not True,
        str(resolved.get("block_reason_code") or "PLAN_OR_SLOT_INVALID"),
    )
    _append(
        blockers,
        not _complete_revisions(plan),
        "INPUT_REVISIONS_INCOMPLETE",
    )
    for code in input_binding.get("blockers") or []:
        _append(blockers, True, str(code))
    _append(
        blockers,
        input_binding.get("valid") is not True
        or input_binding.get("applicable") is not True,
        "SHADOW_INPUT_BINDING_INVALID",
    )
    for code in price_horizon.get("blockers") or []:
        _append(blockers, True, str(code))
    _append(
        blockers,
        prices.get("fresh") is not True
        or _float(prices.get("buy"), None) is None
        or _float(prices.get("net_sell"), None) is None,
        "CURRENT_PRICE_MISSING_OR_NOT_FRESH",
    )
    _append(
        blockers,
        not _forecast_complete(slot),
        "CURRENT_FORECAST_SCENARIO_INCOMPLETE",
    )
    _append(
        blockers,
        reserve.get("field_activation_input_complete") is not True,
        "FORECAST_SCENARIO_OR_RESERVE_CONTRACT_INCOMPLETE",
    )
    _append(
        blockers,
        terminal.get("fresh") is not True,
        "TERMINAL_VALUE_INVALID_OR_STALE",
    )
    for code in active_policy.get("blockers") or []:
        _append(blockers, True, str(code))
    _append(
        blockers,
        canonical_slot.get("valid_selected_contract") is not True,
        str(
            canonical_slot.get("reason_code")
            or "CANONICAL_DIRECT_MARKETING_SELECTION_INCOMPLETE"
        ),
    )
    action_horizon = (
        canonical_slot.get("action_horizon_contract")
        if isinstance(canonical_slot.get("action_horizon_contract"), dict)
        else {}
    )
    slot_start = _int(slot.get("start_ts_ms"), 0)
    slot_end = _int(slot.get("end_ts_ms"), 0)
    window_start = _int(canonical_slot.get("window_start_ts_ms"), 0)
    window_end = _int(canonical_slot.get("window_end_ts_ms"), 0)
    _append(
        blockers,
        not (
            action_horizon.get("schema_version") == ACTION_HORIZON_SCHEMA
            and action_horizon.get("complete") is True
            and action_horizon.get("action") == canonical_slot.get("action")
            and _int(action_horizon.get("bound_horizon_end_ts_ms"), 0)
            == _int(decision_horizon.get("end_ts_ms"), 0)
            and _int(action_horizon.get("window_start_ts_ms"), 0)
            == window_start
            and _int(action_horizon.get("window_end_ts_ms"), 0)
            == window_end
            and window_start <= slot_start < slot_end <= window_end
        ),
        str(
            action_horizon.get("block_reason_code")
            or "ACTION_WINDOW_OUTSIDE_BOUND_HORIZON"
        ),
    )
    return {
        "valid": not blockers,
        "evidence_status": "COMPLETE" if not blockers else "EVIDENCE_LIMIT",
        "blockers": blockers,
        "plan_id": resolved.get("plan_id"),
        "slot_id": resolved.get("slot_id"),
        "canonical_slot": canonical_slot,
        "input_binding": input_binding,
        "price_horizon": price_horizon,
        "active_policy": active_policy,
        "action": canonical_slot.get("action"),
        "source_action": canonical_slot.get("source_action"),
        "source_mode": canonical_slot.get("source_mode"),
        "action_id": canonical_slot.get("action_id"),
        "window_id": canonical_slot.get("window_id"),
        "window_start_ts_ms": canonical_slot.get("window_start_ts_ms"),
        "window_end_ts_ms": canonical_slot.get("window_end_ts_ms"),
    }


def _append(blockers: List[str], condition: bool, code: str) -> None:
    if condition and code not in blockers:
        blockers.append(code)


def _live_soc_plan_corridor_contract(
    live_soc: Optional[float],
    shadow_slot: Dict[str, Any],
    plan_slot: Dict[str, Any],
    direct_marketing_slot: Dict[str, Any],
    tolerance_pct: float,
) -> Dict[str, Any]:
    """Bindet die SoC-Plausibilität an den gesamten wirksamen Slotkorridor.

    Der Slotanfang allein ist während einer geplanten Lade- oder Entladeaktion
    kein Sollwert. Für eine kanonisch ausgewählte DV-Aktion ist deren eigene
    SoC-Projektion maßgeblich; andernfalls gilt die Optimizer-Projektion.
    """

    start_soc = _float(shadow_slot.get("soc_start_pct"), None)
    shadow_end_soc = _float(shadow_slot.get("soc_end_pct"), None)
    projection = (
        plan_slot.get("projection")
        if isinstance(plan_slot.get("projection"), dict)
        else {}
    )
    direct_end_soc = _float(
        projection.get("direct_marketing_soc_pct"),
        None,
    )
    direct_projection_bound = bool(
        direct_marketing_slot.get("valid_selected_contract") is True
        and direct_end_soc is not None
    )
    end_soc = (
        direct_end_soc
        if direct_projection_bound
        else shadow_end_soc
        if shadow_end_soc is not None
        else start_soc
    )
    tolerance = max(0.0, float(tolerance_pct))
    bounds_available = start_soc is not None and end_soc is not None
    lower_soc = min(start_soc, end_soc) if bounds_available else None
    upper_soc = max(start_soc, end_soc) if bounds_available else None
    outside = bool(
        live_soc is not None
        and lower_soc is not None
        and upper_soc is not None
        and (
            live_soc < lower_soc - tolerance
            or live_soc > upper_soc + tolerance
        )
    )
    return {
        "schema_version": "phase5_live_soc_plan_corridor_v1",
        "start_soc_pct": start_soc,
        "end_soc_pct": end_soc,
        "shadow_end_soc_pct": shadow_end_soc,
        "direct_marketing_end_soc_pct": direct_end_soc,
        "lower_soc_pct": lower_soc,
        "upper_soc_pct": upper_soc,
        "tolerance_pct": tolerance,
        "bounds_available": bounds_available,
        "outside": outside,
        "source": (
            "canonical_direct_marketing_projection"
            if direct_projection_bound
            else "shadow_dispatch_projection"
            if shadow_end_soc is not None
            else "slot_start_fallback"
        ),
    }


_PV_STORE_DIAGNOSTIC_ONLY_BLOCKERS = {
    "SHADOW_INPUT_BINDING_NOT_EXECUTION_READY",
    "SHADOW_INPUT_BINDING_INVALID",
    "FORECAST_INPUT_BINDING_INVALID",
    "RESERVE_INPUT_BINDING_INVALID",
    "INPUT_BINDING_INCOMPLETE",
    "CURRENT_FORECAST_SCENARIO_INCOMPLETE",
    "FORECAST_SCENARIO_OR_RESERVE_CONTRACT_INCOMPLETE",
}

_ECONOMIC_EXPORT_DIAGNOSTIC_ONLY_BLOCKERS = frozenset({
    "SHADOW_INPUT_BINDING_NOT_EXECUTION_READY",
    "SHADOW_INPUT_BINDING_INVALID",
    "FORECAST_INPUT_BINDING_INVALID",
    "RESERVE_INPUT_BINDING_INVALID",
    "INPUT_BINDING_INCOMPLETE",
    "CURRENT_FORECAST_SCENARIO_INCOMPLETE",
    "FORECAST_SCENARIO_OR_RESERVE_CONTRACT_INCOMPLETE",
    "POWER_SETTINGS_NOT_CONFIRMED_STABLE",
})


def _economic_export_diagnostic_only_release_contract(
    candidate: Dict[str, Any],
    canonical_slot: Dict[str, Any],
    active_binding: Dict[str, Any],
    input_binding: Dict[str, Any],
    live_contract: Dict[str, Any],
    settings_contract: Dict[str, Any],
    readiness_validation: Dict[str, Any],
    blockers: List[str],
) -> Dict[str, Any]:
    """Typisiert die eng begrenzte Forecast-Ausnahme im einzigen Entscheider."""

    blocker_set = {str(code) for code in blockers if str(code)}
    binding_blockers = {
        str(code)
        for code in input_binding.get("blockers") or []
        if str(code)
    }
    active_blockers = {
        str(code)
        for code in active_binding.get("blockers") or []
        if str(code)
    }
    readiness_blockers = {
        str(code)
        for code in readiness_validation.get("readiness_blockers") or []
        if str(code)
    }
    active_policy = (
        active_binding.get("active_policy")
        if isinstance(active_binding.get("active_policy"), dict)
        else {}
    )
    gate = (
        candidate.get("economic_export_gate")
        if isinstance(candidate.get("economic_export_gate"), dict)
        else {}
    )
    settings_blockers = {
        str(code)
        for code in settings_contract.get("blockers") or []
        if str(code)
    }
    settings_valid = bool(
        settings_contract.get("valid") is True
        or (settings_blockers and settings_blockers.issubset(_ECONOMIC_EXPORT_DIAGNOSTIC_ONLY_BLOCKERS))
    )
    valid = bool(
        blocker_set
        and blocker_set.issubset(
            _ECONOMIC_EXPORT_DIAGNOSTIC_ONLY_BLOCKERS
        )
        and binding_blockers.issubset(
            _ECONOMIC_EXPORT_DIAGNOSTIC_ONLY_BLOCKERS
        )
        and active_blockers.issubset(
            _ECONOMIC_EXPORT_DIAGNOSTIC_ONLY_BLOCKERS
        )
        and readiness_validation.get("valid") is True
        and not readiness_validation.get("blockers")
        and readiness_validation.get("status") == "EVIDENCE_LIMIT"
        and readiness_validation.get("execution_ready") is False
        and readiness_validation.get("execution_class")
        == "CANONICAL_ACTION"
        and readiness_blockers
        and readiness_blockers.issubset(
            _ECONOMIC_EXPORT_DIAGNOSTIC_ONLY_BLOCKERS
        )
        and candidate.get("valid") is True
        and str(candidate.get("action") or "").upper()
        == "ECONOMIC_EXPORT"
        and candidate.get("direction") == "discharge"
        and candidate.get("direction_valid") is True
        and candidate.get("power_valid") is True
        and (_float(candidate.get("power_w"), 0.0) or 0.0) >= 300.0
        and canonical_slot.get("valid_selected_contract") is True
        and str(canonical_slot.get("action") or "").upper()
        == "ECONOMIC_EXPORT"
        and canonical_slot.get("source_action")
        == "eco_plus_export_candidate"
        and canonical_slot.get("action_lineage_id")
        == canonical_slot.get("action_id")
        and active_policy.get("valid") is True
        and not active_policy.get("blockers")
        and active_policy.get("target_state") == "FORCE_EXPORT"
        and active_policy.get("source_action")
        == "eco_plus_export_candidate"
        and gate.get("allowed") is True
        and not gate.get("blockers")
        and live_contract.get("valid") is True
        and settings_valid
    )
    return {
        "schema": "phase5_economic_export_diagnostic_only_release_v1",
        "valid": valid,
        "reason": (
            "canonical_economic_export_blocked_only_by_forecast_diagnostics"
            if valid
            else "contract_incomplete"
        ),
        "suppressed_blockers": sorted(blocker_set) if valid else [],
    }


def _pv_store_diagnostic_only_release_contract(
    candidate: Dict[str, Any],
    canonical_slot: Dict[str, Any],
    active_binding: Dict[str, Any],
    input_binding: Dict[str, Any],
    live_contract: Dict[str, Any],
    settings_contract: Dict[str, Any],
    readiness_validation: Dict[str, Any],
    blockers: List[str],
) -> Dict[str, Any]:
    """Lässt nur einen vollständig gebundenen PV_STORE-Slot Diagnosefelder übergehen."""

    blocker_set = {str(code) for code in blockers if str(code)}
    binding_blockers = {str(code) for code in input_binding.get("blockers") or [] if str(code)}
    active_blockers = {str(code) for code in active_binding.get("blockers") or [] if str(code)}
    active_policy = active_binding.get("active_policy") if isinstance(active_binding.get("active_policy"), dict) else {}
    readiness_blockers = {
        str(code)
        for code in readiness_validation.get("readiness_blockers") or []
        if str(code)
    }
    valid = bool(
        blocker_set
        and blocker_set.issubset(_PV_STORE_DIAGNOSTIC_ONLY_BLOCKERS)
        and binding_blockers.issubset(_PV_STORE_DIAGNOSTIC_ONLY_BLOCKERS)
        and active_blockers.issubset(_PV_STORE_DIAGNOSTIC_ONLY_BLOCKERS)
        and readiness_validation.get("valid") is True
        and not readiness_validation.get("blockers")
        and readiness_validation.get("status") == "EVIDENCE_LIMIT"
        and readiness_validation.get("execution_ready") is False
        and readiness_validation.get("execution_class") == "CANONICAL_ACTION"
        and readiness_blockers
        and readiness_blockers.issubset(_PV_STORE_DIAGNOSTIC_ONLY_BLOCKERS)
        and candidate.get("valid") is True
        and str(candidate.get("action") or "").upper() == "PV_STORE"
        and candidate.get("direction") == "charge"
        and candidate.get("direction_valid") is True
        and candidate.get("power_valid") is True
        and (_float(candidate.get("power_w"), 0.0) or 0.0) >= 300.0
        and canonical_slot.get("valid_selected_contract") is True
        and str(canonical_slot.get("action") or "").upper() == "PV_STORE"
        and canonical_slot.get("source_action") == "eco_plus_store_pv_candidate"
        and canonical_slot.get("action_lineage_id") == canonical_slot.get("action_id")
        and active_policy.get("valid") is True
        and not active_policy.get("blockers")
        and active_policy.get("target_state") == "FORCE_CHARGE_PV"
        and active_policy.get("source_action") == "eco_plus_store_pv_candidate"
        and live_contract.get("valid") is True
        and settings_contract.get("valid") is True
    )
    return {
        "schema": "phase5_pv_store_diagnostic_only_release_v1",
        "valid": valid,
        "reason": "canonical_pv_store_blocked_only_by_forecast_diagnostics" if valid else "contract_incomplete",
        "suppressed_blockers": sorted(blocker_set) if valid else [],
        "grid_ac_allowed": False,
        "runtime_source_recheck_required": True,
    }


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


def _shadow_execution_readiness_current_contract(
    plan: Dict[str, Any],
    shadow: Dict[str, Any],
    resolved: Dict[str, Any],
) -> Dict[str, Any]:
    """Bindet eine Readiness-Aussage erneut an den aktuellen Planinhalt.

    Der äußere Planhash beweist nur, dass die gelieferte JSON-Fläche in sich
    unverändert ist. Phase 5 prüft deshalb zusätzlich, ob die darin enthaltene
    Readiness noch genau zu Slot, Revisionssatz, Inputbindung und Aktionsrollen
    derselben Generation gehört. Ein formal neu versiegelter, aber veralteter
    READY-Claim bleibt damit fail-closed.
    """

    readiness = (
        shadow.get("execution_readiness_contract")
        if isinstance(shadow.get("execution_readiness_contract"), dict)
        else {}
    )
    slot = (
        resolved.get("plan_slot")
        if isinstance(resolved.get("plan_slot"), dict)
        else {}
    )
    projection = (
        slot.get("projection")
        if isinstance(slot.get("projection"), dict)
        else {}
    )
    action_roles = (
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
        plan.get("input_revisions")
        if isinstance(plan.get("input_revisions"), dict)
        else {}
    )
    expected_revisions = {
        key: revisions.get(key) for key in SHADOW_EXECUTION_REVISION_KEYS
    }
    blockers: List[str] = []

    def require(condition: bool, code: str) -> None:
        if not condition and code not in blockers:
            blockers.append(code)

    def optional_action(value: Any) -> Optional[str]:
        text = str(value or "").strip().upper()
        return text or None

    nested_blockers_raw = readiness.get("blockers")
    nested_blockers = (
        [str(code) for code in nested_blockers_raw]
        if isinstance(nested_blockers_raw, list)
        and all(isinstance(code, str) and code for code in nested_blockers_raw)
        else []
    )
    status = str(readiness.get("status") or "")
    execution_class = str(readiness.get("execution_class") or "")
    execution_ready = readiness.get("execution_ready") is True
    candidate_action = optional_action(action_roles.get("candidate_action"))
    selected_action = optional_action(
        action_roles.get("plan_selected_action")
    )
    executable_action = optional_action(
        action_roles.get("plan_executable_action")
    )

    require(
        readiness.get("schema_version")
        == SHADOW_EXECUTION_READINESS_SCHEMA,
        "SHADOW_EXECUTION_READINESS_SCHEMA_INVALID",
    )
    require(
        status in {"READY", "READY_NO_ACTION", "EVIDENCE_LIMIT"}
        and type(readiness.get("execution_ready")) is bool
        and isinstance(nested_blockers_raw, list)
        and len(nested_blockers) == len(nested_blockers_raw)
        and len(set(nested_blockers)) == len(nested_blockers),
        "SHADOW_EXECUTION_READINESS_STATE_INVALID",
    )
    optimizer_status = str(shadow.get("optimizer_status") or "")
    raw_optimizer_status = str(shadow.get("status") or "")
    require(
        bool(optimizer_status)
        and optimizer_status == raw_optimizer_status
        and readiness.get("optimizer_status") == optimizer_status
        and resolved.get("optimizer_status") == optimizer_status,
        "SHADOW_EXECUTION_READINESS_OPTIMIZER_MISMATCH",
    )
    require(
        resolved.get("valid") is True
        and _int(readiness.get("current_slot_start_ts_ms"), 0)
        == _int(slot.get("start_ts_ms"), 0)
        and _int(readiness.get("current_slot_end_ts_ms"), 0)
        == _int(slot.get("end_ts_ms"), 0),
        "SHADOW_EXECUTION_READINESS_SLOT_MISMATCH",
    )
    require(
        all(
            isinstance(expected_revisions.get(key), str)
            and REVISION_RE.fullmatch(expected_revisions[key])
            for key in SHADOW_EXECUTION_REVISION_KEYS
        )
        and readiness.get("source_revisions") == expected_revisions,
        "SHADOW_EXECUTION_READINESS_REVISIONS_MISMATCH",
    )
    require(
        isinstance(readiness.get("input_binding_revision"), str)
        and REVISION_RE.fullmatch(readiness["input_binding_revision"])
        and readiness.get("input_binding_revision")
        == revision_hash(binding),
        "SHADOW_EXECUTION_READINESS_INPUT_BINDING_REVISION_MISMATCH",
    )
    require(
        isinstance(readiness.get("action_roles_revision"), str)
        and REVISION_RE.fullmatch(readiness["action_roles_revision"])
        and readiness.get("action_roles_revision")
        == revision_hash(action_roles),
        "SHADOW_EXECUTION_READINESS_ACTION_ROLES_REVISION_MISMATCH",
    )
    require(
        action_roles.get("schema_version")
        == DIRECT_MARKETING_ACTION_ROLES_SCHEMA
        and action_roles.get("status") == "CONSISTENT"
        and action_roles.get("effective_action") is None
        and action_roles.get("runtime_effect_claim_allowed") is False
        and action_roles.get("candidate_only")
        is bool(candidate_action is not None and executable_action is None),
        "SHADOW_EXECUTION_READINESS_ACTION_ROLES_INVALID",
    )
    require(
        optional_action(readiness.get("candidate_action"))
        == candidate_action
        and optional_action(readiness.get("plan_selected_action"))
        == selected_action
        and optional_action(readiness.get("plan_executable_action"))
        == executable_action
        and optional_action(
            projection.get("direct_marketing_candidate_action")
        )
        == candidate_action
        and projection.get("direct_marketing_candidate")
        is (candidate_action is not None)
        and projection.get("direct_marketing_candidate_only")
        is action_roles.get("candidate_only")
        and optional_action(
            projection.get("direct_marketing_plan_selected_action")
        )
        == selected_action
        and projection.get("direct_marketing_selected")
        is (selected_action is not None)
        and optional_action(
            projection.get("direct_marketing_plan_executable_action")
        )
        == executable_action
        and optional_action(
            projection.get("direct_marketing_plan_action")
        )
        == executable_action
        and projection.get("direct_marketing_plan_executable")
        is (executable_action is not None)
        and projection.get("direct_marketing_plan_commands_allowed")
        is (executable_action is not None)
        and projection.get("direct_marketing_effective_action") is None,
        "SHADOW_EXECUTION_READINESS_ROLE_MISMATCH",
    )
    require(
        shadow.get("execution_readiness_status") == status
        and shadow.get("execution_ready") is execution_ready
        and shadow.get("execution_readiness_blockers") == nested_blockers,
        "SHADOW_EXECUTION_READINESS_MIRROR_MISMATCH",
    )

    if status == "READY":
        require(
            execution_ready
            and execution_class == "CANONICAL_ACTION"
            and not nested_blockers
            and candidate_action == selected_action == executable_action
            and executable_action in DIRECT_MARKETING_RUNTIME_PLAN_ACTIONS,
            "SHADOW_EXECUTION_READINESS_STATE_INVALID",
        )
    elif status == "READY_NO_ACTION":
        require(
            execution_ready
            and execution_class == "NO_ACTION"
            and not nested_blockers
            and candidate_action is None
            and selected_action is None
            and executable_action is None,
            "SHADOW_EXECUTION_READINESS_STATE_INVALID",
        )
    elif status == "EVIDENCE_LIMIT":
        canonical_action_known = bool(
            candidate_action == selected_action == executable_action
            and executable_action in DIRECT_MARKETING_RUNTIME_PLAN_ACTIONS
        )
        neutral_no_action_known = bool(
            candidate_action is None
            and selected_action is None
            and executable_action is None
        )
        expected_execution_class = (
            "CANONICAL_ACTION"
            if canonical_action_known
            else "NO_ACTION"
            if neutral_no_action_known
            else "EVIDENCE_LIMIT"
        )
        require(
            not execution_ready
            and bool(nested_blockers)
            and execution_class == expected_execution_class,
            "SHADOW_EXECUTION_READINESS_STATE_INVALID",
        )

    return {
        "valid": not blockers,
        "execution_ready": bool(not blockers and execution_ready),
        "status": status or None,
        "execution_class": execution_class or None,
        "blockers": blockers,
        "readiness_blockers": nested_blockers,
        "candidate_action": candidate_action,
        "plan_selected_action": selected_action,
        "plan_executable_action": executable_action,
        "source_revisions": copy.deepcopy(expected_revisions),
        "input_binding_revision": readiness.get("input_binding_revision"),
        "action_roles_revision": readiness.get("action_roles_revision"),
    }


def _ready_no_action_house_supply_contract(
    plan: Dict[str, Any],
    shadow_slot: Dict[str, Any],
    resolved: Dict[str, Any],
    candidate: Dict[str, Any],
    input_binding: Dict[str, Any],
    readiness: Dict[str, Any],
    live_contract: Dict[str, Any],
    settings_contract: Dict[str, Any],
    blockers: Iterable[str],
    now_ms: int,
) -> Dict[str, Any]:
    """Bindet einen wirkungslosen HOUSE_SUPPLY-Zyklus fail-closed.

    Fehlende Prognosequantile dürfen eine reale Speicheraktion nie freigeben.
    Wenn derselbe Plan aber ausdrücklich keinerlei DV-Aktionsrolle enthält,
    darf Phase 5 den bereits bestehenden Legacy-AUTO-Entscheid beibehalten.
    Der Vertrag autorisiert weder POWER_SETTINGS noch einen RSCP-Sollwert.
    """

    def finite_zero(value: Any) -> bool:
        return bool(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and abs(float(value)) <= 0.000001
        )

    slot = (
        resolved.get("plan_slot")
        if isinstance(resolved.get("plan_slot"), dict)
        else {}
    )
    projection = (
        slot.get("projection")
        if isinstance(slot.get("projection"), dict)
        else {}
    )
    roles = (
        projection.get("direct_marketing_action_roles")
        if isinstance(
            projection.get("direct_marketing_action_roles"), dict
        )
        else {}
    )
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
    policy = (
        direct.get("policy_decision")
        if isinstance(direct.get("policy_decision"), dict)
        else {}
    )
    timeline = (
        direct.get("policy_timeline")
        if isinstance(direct.get("policy_timeline"), list)
        else []
    )
    budget = (
        policy.get("storage_budget")
        if isinstance(policy.get("storage_budget"), dict)
        else {}
    )
    lineage = (
        policy.get("export_window_gate_lineage")
        if isinstance(policy.get("export_window_gate_lineage"), dict)
        else {}
    )
    passive_binding = (
        projection.get("direct_marketing_passive_normal_binding_v1")
        if isinstance(
            projection.get("direct_marketing_passive_normal_binding_v1"),
            dict,
        )
        else {}
    )
    input_blockers = {
        str(code) for code in input_binding.get("blockers") or [] if str(code)
    }
    input_components = (
        input_binding.get("components")
        if isinstance(input_binding.get("components"), dict)
        else {}
    )
    price_input = (
        input_components.get("price")
        if isinstance(input_components.get("price"), dict)
        else {}
    )
    terminal_input = (
        input_components.get("terminal")
        if isinstance(input_components.get("terminal"), dict)
        else {}
    )
    current_blockers = [str(code) for code in blockers if str(code)]
    unexpected_blockers = [
        code for code in current_blockers
        if code not in READY_NO_ACTION_ALLOWED_BLOCKERS
    ]
    slot_start_ms = _int(slot.get("start_ts_ms"), 0)
    slot_end_ms = _int(slot.get("end_ts_ms"), 0)
    generated_at_ms = _int(plan.get("generated_at_ts_ms"), 0)
    direct_created_ms = _int(direct.get("created_ts"), 0)
    direct_valid_until_ms = _int(direct.get("valid_until_ts"), 0)
    overlapping_policies = [
        item
        for item in timeline
        if isinstance(item, dict)
        and _int(item.get("start_ts"), 0) < slot_end_ms
        and _int(item.get("end_ts"), 0) > slot_start_ms
    ]
    mode = _normalized_direct_marketing_mode(direct.get("mode"))
    policy_lineage_valid = direct_marketing_export_gate_contract_valid(
        policy,
        policy.get("economics"),
        allowed_lineage_statuses={"SUSPENDED"},
        current_window_id=policy.get("window_id"),
    )
    input_identity_valid = bool(
        input_binding.get("applicable") is True
        and input_binding.get("schema_version")
        == SHADOW_INPUT_BINDING_SCHEMA
        and not (
            input_blockers - READY_NO_ACTION_INPUT_BLOCKERS
        )
        and (
            input_binding.get("valid") is True
            or bool(input_blockers)
        )
        and price_input.get("complete") is True
        and terminal_input.get("complete") is True
    )
    roles_neutral = bool(
        roles.get("schema_version")
        == DIRECT_MARKETING_ACTION_ROLES_SCHEMA
        and roles.get("status") == "CONSISTENT"
        and roles.get("candidate_action") is None
        and roles.get("candidate_only") is False
        and roles.get("plan_selected_action") is None
        and roles.get("plan_executable_action") is None
        and roles.get("effective_action") is None
        and roles.get("runtime_effect_claim_allowed") is False
    )
    projection_neutral = bool(
        (
            str(slot.get("planned_action") or "").upper()
            == "HOUSE_SUPPLY"
            and str(projection.get("market_action") or "").upper()
            == "HOUSE_SUPPLY"
            or passive_binding
            and str(slot.get("planned_action") or "").upper()
            in {"HOLD", "HOUSE_SUPPLY"}
            and str(projection.get("market_action") or "").upper()
            in {"HOLD", "HOUSE_SUPPLY"}
        )
        and projection.get("direct_marketing_candidate") is False
        and projection.get("direct_marketing_selected") is False
        and projection.get("direct_marketing_plan_executable") is False
        and projection.get("direct_marketing_plan_commands_allowed") is False
        and projection.get("direct_marketing_action") is None
        and projection.get("direct_marketing_plan_action") is None
        and projection.get("direct_marketing_effective_action") is None
        and finite_zero(projection.get("direct_marketing_candidate_w"))
        and finite_zero(projection.get("direct_marketing_planned_w"))
        and finite_zero(projection.get("direct_marketing_charge_w"))
        and finite_zero(projection.get("direct_marketing_export_w"))
    )
    shadow_comparison_no_effect = bool(
        shadow_slot.get("shadow_only") is True
        and shadow_slot.get("executable") is False
        and shadow_slot.get("commands_allowed") is False
        and shadow_slot.get("requested") is False
        and shadow_slot.get("acknowledged") is False
        and shadow_slot.get("readback_confirmed") is False
        and str(shadow_slot.get("block_reason_code") or "")
        in {
            "NO_STORAGE_ACTION_CANDIDATE",
            "SHADOW_ONLY_NOT_RUNTIME_AUTHORIZED",
        }
        and (
            shadow_slot.get("selected") is False
            and shadow_slot.get("candidate") is False
            or shadow_slot.get("selected") is True
            and shadow_slot.get("candidate") is True
            and shadow_slot.get("selection_scope")
            == "SHADOW_COMPARISON_ONLY"
        )
    )
    readiness_valid = bool(
        readiness.get("valid") is True
        and readiness.get("status") == "READY_NO_ACTION"
        and readiness.get("execution_ready") is True
        and readiness.get("execution_class") == "NO_ACTION"
        and not readiness.get("blockers")
        and not readiness.get("readiness_blockers")
        and readiness.get("candidate_action") is None
        and readiness.get("plan_selected_action") is None
        and readiness.get("plan_executable_action") is None
    )
    candidate_comparison_only = bool(
        candidate.get("valid") is True
        and str(candidate.get("action") or "").upper()
        == str(shadow_slot.get("planned_action") or "").upper()
        and abs(
            (_float(candidate.get("battery_w"), 0.0) or 0.0)
            - (_float(shadow_slot.get("battery_w"), 0.0) or 0.0)
        )
        <= 0.001
        and abs(
            (_float(candidate.get("power_w"), 0.0) or 0.0)
            - (_float(shadow_slot.get("selected_power_w"), 0.0) or 0.0)
        )
        <= 0.001
        and shadow_comparison_no_effect
    )
    suspended_owner_valid = bool(
        direct.get("active") is True
        and direct.get("shadow") is True
        and mode == "eco_plus"
        and direct.get("controller_owner") == "storage_manager"
        and direct.get("plan_owner") == "direct_marketing:eco_plus"
        and type(direct.get("owner_contract_version")) is int
        and direct.get("owner_contract_version") == 1
        and type(flags.get("owner_contract_version")) is int
        and flags.get("owner_contract_version") == 1
        and flags.get("commands_allowed") is False
        and generated_at_ms > 0
        and direct_created_ms > 0
        and abs(direct_created_ms - generated_at_ms) <= 60_000
        and direct_created_ms < direct_valid_until_ms
        and direct_valid_until_ms >= slot_end_ms
        and direct_created_ms <= int(now_ms) < direct_valid_until_ms
    )
    passive_owner_valid = bool(
        direct.get("active") is True
        and direct.get("shadow") is False
        and mode == "eco_plus"
        and direct.get("controller_owner") == "storage_manager"
        and direct.get("plan_owner") == "direct_marketing:eco_plus"
        and type(direct.get("owner_contract_version")) is int
        and direct.get("owner_contract_version") == 1
        and flags.get("commands_allowed") is True
        and generated_at_ms > 0
        and direct_created_ms > 0
        and abs(direct_created_ms - generated_at_ms) <= 60_000
        and direct_created_ms < direct_valid_until_ms
        and direct_valid_until_ms >= slot_end_ms
        and direct_created_ms <= int(now_ms) < direct_valid_until_ms
    )
    suspended_policy_valid = bool(
        policy.get("schema") == "direct_marketing_policy_v1"
        and str(policy.get("dv_target_state") or "").upper() == "HOLD"
        and policy.get("commands_allowed") is False
        and policy.get("blocked") is True
        and policy.get("executable_action") is None
        and policy.get("execution_window") is None
        and _int(policy.get("execution_window_match_count"), -1) == 0
        and policy.get("continuation_active") is False
        and str(policy.get("source_action") or "")
        == "eco_plus_export_candidate"
        and str(policy.get("block_reason") or "")
        == "suspended:SUSPENDED_INPUT_OR_FORECAST_EVIDENCE_INCOMPLETE"
        and abs(_float(budget.get("charge_budget_w"), -1.0) or 0.0)
        <= 0.000001
        and abs(_float(budget.get("export_budget_w"), -1.0) or 0.0)
        <= 0.000001
        and _int(policy.get("start_ts"), 0)
        <= slot_start_ms
        < slot_end_ms
        <= _int(policy.get("end_ts"), 0)
        and sum(1 for item in timeline if item == policy) == 1
        and len(overlapping_policies) == 1
        and overlapping_policies[0] == policy
        and lineage.get("schema") == "export_window_gate_lineage_v1"
        and lineage.get("status") == "SUSPENDED"
        and lineage.get("effect_contract")
        == "STATUS_ONLY_NO_EXECUTION_AUTHORITY"
        and lineage.get("transition_reason_codes")
        == ["SUSPENDED_INPUT_OR_FORECAST_EVIDENCE_INCOMPLETE"]
        and policy_lineage_valid
    )
    passive_policy_valid = bool(
        passive_binding.get("schema")
        == "direct_marketing_passive_normal_binding_v1"
        and passive_binding.get("policy_action_id")
        == policy.get("policy_action_id")
        and passive_binding.get("policy_slot_id")
        == policy.get("policy_slot_id")
        and policy.get("passive_normal_binding") == passive_binding
        and policy.get("schema") == "direct_marketing_policy_v1"
        and str(policy.get("dv_target_state") or "").upper() == "NORMAL"
        and policy.get("commands_allowed") is False
        and policy.get("blocked") is False
        and policy.get("executable_action") is None
        and policy.get("execution_window") is None
        and _int(policy.get("execution_window_match_count"), -1) == 0
        and str(policy.get("source_action") or "")
        == "eco_plus_house_supply"
        and abs(_float(budget.get("charge_budget_w"), -1.0) or 0.0)
        <= 0.000001
        and abs(_float(budget.get("export_budget_w"), -1.0) or 0.0)
        <= 0.000001
        and _int(policy.get("start_ts"), 0)
        <= slot_start_ms
        < slot_end_ms
        <= _int(policy.get("end_ts"), 0)
        and len(overlapping_policies) == 1
        and overlapping_policies[0] == policy
    )
    owner_valid = bool(suspended_owner_valid or passive_owner_valid)
    policy_valid = bool(suspended_policy_valid or passive_policy_valid)
    owner_policy_pair_valid = bool(
        suspended_owner_valid and suspended_policy_valid
        or passive_owner_valid and passive_policy_valid
    )
    valid = bool(
        resolved.get("valid") is True
        and slot_start_ms > 0
        and slot_end_ms - slot_start_ms == SLOT_DURATION_MS
        and not unexpected_blockers
        and input_identity_valid
        and roles_neutral
        and projection_neutral
        and shadow_comparison_no_effect
        and readiness_valid
        and candidate_comparison_only
        and owner_policy_pair_valid
        and live_contract.get("valid") is True
        and settings_contract.get("valid") is True
    )
    return {
        "schema": "phase5_ready_no_action_house_supply_v1",
        "valid": valid,
        "effect": "LEGACY_AUTO_UNCHANGED",
        "commands_allowed": False,
        "selected_action": "HOUSE_SUPPLY" if valid else None,
        "plan_id": resolved.get("plan_id"),
        "slot_id": resolved.get("slot_id"),
        "input_binding_diagnostic_blockers": sorted(input_blockers),
        "suppressed_blockers": (
            [
                code for code in current_blockers
                if code in READY_NO_ACTION_SUPPRESSIBLE_BLOCKERS
            ]
            if valid
            else []
        ),
        "unexpected_blockers": unexpected_blockers,
        "checks": {
            "input_identity_valid": input_identity_valid,
            "roles_neutral": roles_neutral,
            "projection_neutral": projection_neutral,
            "shadow_comparison_no_effect": shadow_comparison_no_effect,
            "readiness_valid": readiness_valid,
            "candidate_comparison_only": candidate_comparison_only,
            "owner_valid": owner_valid,
            "policy_valid": policy_valid,
            "owner_policy_pair_valid": owner_policy_pair_valid,
            "current_policy_unique": bool(
                len(overlapping_policies) == 1
                and overlapping_policies[0] == policy
            ),
            "live_valid": live_contract.get("valid") is True,
            "power_settings_valid": settings_contract.get("valid") is True,
        },
    }


def _restrictive_active_policy_hold_contract(
    plan: Dict[str, Any],
    candidate: Dict[str, Any],
    resolved: Dict[str, Any],
    readiness: Dict[str, Any],
    now_ms: int,
) -> Dict[str, Any]:
    """Typisiert einen gesperrten aktiven DV-Export als restriktiven HOLD."""

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
    policy = (
        direct.get("policy_decision")
        if isinstance(direct.get("policy_decision"), dict)
        else {}
    )
    timeline = (
        direct.get("policy_timeline")
        if isinstance(direct.get("policy_timeline"), list)
        else []
    )
    slot = (
        resolved.get("plan_slot")
        if isinstance(resolved.get("plan_slot"), dict)
        else {}
    )
    projection = (
        slot.get("projection")
        if isinstance(slot.get("projection"), dict)
        else {}
    )
    roles = (
        projection.get("direct_marketing_action_roles")
        if isinstance(
            projection.get("direct_marketing_action_roles"), dict
        )
        else {}
    )
    mode = _normalized_direct_marketing_mode(direct.get("mode"))
    readiness_blockers = set(readiness.get("readiness_blockers") or [])
    policy_valid = direct_marketing_export_gate_contract_valid(
        policy,
        policy.get("economics"),
        allowed_lineage_statuses={"SUSPENDED"},
        current_window_id=policy.get("window_id"),
    )
    valid = bool(
        resolved.get("valid") is True
        and readiness.get("valid") is True
        and readiness.get("status") == "EVIDENCE_LIMIT"
        and readiness.get("execution_ready") is False
        and readiness_blockers
        == {"CANONICAL_DIRECT_MARKETING_SLOT_NOT_SELECTED"}
        and candidate.get("valid") is True
        and str(candidate.get("action") or "").upper() == "HOLD"
        and str(candidate.get("direction") or "").lower() == "hold"
        and abs(_float(candidate.get("power_w"), 0.0) or 0.0) <= 0.000001
        and abs(_float(candidate.get("battery_w"), 0.0) or 0.0)
        <= 0.000001
        and candidate.get("reason_code") == "SHADOW_HOLD"
        and roles.get("schema_version")
        == DIRECT_MARKETING_ACTION_ROLES_SCHEMA
        and roles.get("status") == "CONSISTENT"
        and roles.get("candidate_action") == "ECONOMIC_EXPORT"
        and roles.get("candidate_only") is True
        and roles.get("plan_selected_action") is None
        and roles.get("plan_executable_action") is None
        and roles.get("effective_action") is None
        and direct.get("active") is True
        and direct.get("shadow") is False
        and mode in {"eco", "eco_plus", "arbitrage"}
        and direct.get("controller_owner") == "storage_manager"
        and direct.get("plan_owner") == f"direct_marketing:{mode}"
        and type(direct.get("owner_contract_version")) is int
        and direct.get("owner_contract_version") == 1
        and type(flags.get("owner_contract_version")) is int
        and flags.get("owner_contract_version") == 1
        and flags.get("commands_allowed") is True
        and _int(direct.get("created_ts"), 0) <= int(now_ms)
        < _int(direct.get("valid_until_ts"), 0)
        and sum(1 for item in timeline if item == policy) == 1
        and policy_valid
    )
    return {
        "valid": valid,
        "reason_code": (
            "SUSPENDED_ACTIVE_DIRECT_MARKETING_EXPORT_HOLD"
            if valid
            else None
        ),
        "blockers": (
            ["CANONICAL_DIRECT_MARKETING_SLOT_NOT_SELECTED"]
            if valid
            else []
        ),
        "policy_window_id": policy.get("window_id") if valid else None,
        "lineage_status": (
            (policy.get("export_window_gate_lineage") or {}).get("status")
            if valid
            else None
        ),
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
    legacy_force_export_reentry = (
        _legacy_phase5_force_export_reentry_contract(
            legacy,
            path_contract,
            resolved,
        )
    )
    shadow = plan.get("shadow_dispatch") if isinstance(plan.get("shadow_dispatch"), dict) else {}
    shadow_slot = resolved.get("shadow_slot") if isinstance(resolved.get("shadow_slot"), dict) else {}
    candidate = _candidate_contract(plan, shadow_slot) if shadow_slot else {
        "valid": False, "action": None, "direction": "hold", "battery_w": 0.0, "power_w": 0.0,
    }
    live_contract = _live_contract(live, legacy, now_value)
    settings_contract = _power_settings_contract(power_settings, now_ms=now_value)
    price_horizon = shadow.get("price_horizon_contract") if isinstance(shadow.get("price_horizon_contract"), dict) else {}
    price_horizon_activation = _price_horizon_activation_contract(shadow)
    terminal = shadow.get("terminal_value") if isinstance(shadow.get("terminal_value"), dict) else {}
    current_plan_slot = resolved.get("plan_slot") if isinstance(resolved.get("plan_slot"), dict) else {}
    current_prices = current_plan_slot.get("prices_ct_kwh") if isinstance(current_plan_slot.get("prices_ct_kwh"), dict) else {}
    current_slot_available = bool(resolved.get("valid") and current_plan_slot and shadow_slot)
    direct_marketing_slot = _canonical_direct_marketing_slot_contract(plan, current_plan_slot)
    active_direct_marketing_binding = active_direct_marketing_binding_contract(
        plan,
        now_value,
    )
    if direct_marketing_slot.get("valid_selected_contract"):
        plan_action = str(direct_marketing_slot.get("action") or "").upper()
        planned_w = float(direct_marketing_slot.get("planned_w") or 0.0)
        target_state = direct_marketing_target_for_plan_action(plan_action)
        action_contract = direct_marketing_action_contract(target_state) or {}
        direction = str(action_contract.get("direction") or "hold")
        battery_w = (
            planned_w
            if direction == "charge"
            else 0.0
            if direction == "hold"
            else -planned_w
        )
        candidate.update({
            "valid": True,
            "action": plan_action,
            "direction": direction,
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
            "pv_store_live_dc_fallback_contract": copy.deepcopy(
                direct_marketing_slot.get(
                    "pv_store_live_dc_fallback_contract"
                )
            ),
            "action_horizon_contract": copy.deepcopy(
                direct_marketing_slot.get("action_horizon_contract")
            ),
        })
        if plan_action == "ECONOMIC_EXPORT":
            candidate["economic_export_gate"] = copy.deepcopy(
                direct_marketing_slot.get("economic_export_gate")
            )
        elif plan_action == "HEADROOM_EXPORT":
            candidate["headroom_gate"] = copy.deepcopy(
                direct_marketing_slot.get("headroom_export_gate")
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
    # Die konkrete Plan-/Optimizerursache ist die Primärdiagnose. Abgeleitete
    # Readiness-Sammelblocker dürfen sie weder ersetzen noch davor einsortieren.
    _append(
        blockers,
        not resolved.get("valid"),
        str(resolved.get("block_reason_code") or "PLAN_OR_SLOT_INVALID"),
    )
    execution_readiness = (
        shadow.get("execution_readiness_contract")
        if isinstance(shadow.get("execution_readiness_contract"), dict)
        else {}
    )
    readiness_validation = _shadow_execution_readiness_current_contract(
        plan,
        shadow,
        resolved,
    )
    if applicable:
        if readiness_validation.get("valid") is not True:
            integrity_blockers = [
                str(code)
                for code in readiness_validation.get("blockers") or []
                if str(code)
            ]
            for code in integrity_blockers:
                _append(blockers, True, code)
            _append(
                blockers,
                not integrity_blockers,
                "SHADOW_EXECUTION_READINESS_BINDING_INVALID",
            )
        elif readiness_validation.get("execution_ready") is not True:
            readiness_blockers = [
                str(code)
                for code in readiness_validation.get(
                    "readiness_blockers"
                ) or []
                if str(code)
            ]
            for code in readiness_blockers:
                _append(blockers, True, code)
            _append(
                blockers,
                not readiness_blockers,
                "SHADOW_EXECUTION_READINESS_EVIDENCE_LIMIT",
            )
    restrictive_policy_hold = _restrictive_active_policy_hold_contract(
        plan,
        candidate,
        resolved,
        readiness_validation,
        now_value,
    )
    _append(blockers, not _complete_revisions(plan), "INPUT_REVISIONS_INCOMPLETE")
    _append(blockers, not input_binding.get("valid"), "SHADOW_INPUT_BINDING_INVALID")
    if direct_marketing_slot.get("valid_selected_contract") is True:
        for code in active_direct_marketing_binding.get("blockers") or []:
            _append(blockers, True, str(code))
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
    canonical_direct_marketing_selection_claimed = bool(
        direct_marketing_slot.get("selected") is True
        or direct_marketing_slot.get("plan_executable") is True
        or direct_marketing_slot.get("plan_commands_allowed") is True
    )
    _append(
        blockers,
        current_slot_available
        and not direct_marketing_slot.get("valid_selected_contract")
        and (
            canonical_direct_marketing_selection_claimed
            or (
                direct_marketing_plan_action_released(candidate.get("action"))
                and candidate.get("action") != "CHARGE_BLOCK_WAIT"
                and not economic_hold.get("valid")
            )
        ),
        str(direct_marketing_slot.get("reason_code") or "CANONICAL_DIRECT_MARKETING_SLOT_NOT_SELECTED"),
    )
    _append(
        blockers,
        bool(current_plan_slot)
        and str(direct_marketing_slot.get("action") or "").upper()
        == "CHARGE_BLOCK_WAIT"
        and not direct_marketing_slot.get("valid_selected_contract"),
        str(
            direct_marketing_slot.get("reason_code")
            or "CANONICAL_DIRECT_MARKETING_CHARGE_BLOCK_WAIT_INCOMPLETE"
        ),
    )
    _append(
        blockers,
        current_slot_available and candidate.get("action") == "GRID_CHARGE",
        "CANONICAL_GRID_CHARGE_SELECTION_CONTRACT_UNAVAILABLE",
    )
    _append(
        blockers,
        current_slot_available
        and candidate.get("action") == "HEADROOM_EXPORT"
        and not direct_marketing_plan_action_released("HEADROOM_EXPORT"),
        "CANONICAL_HEADROOM_EXPORT_NOT_RELEASED",
    )
    _append(
        blockers,
        current_slot_available
        and candidate.get("action") == "HEADROOM_EXPORT"
        and direct_marketing_plan_action_released("HEADROOM_EXPORT")
        and not direct_marketing_slot.get("valid_selected_contract"),
        str(
            direct_marketing_slot.get("reason_code")
            or "CANONICAL_HEADROOM_EXPORT_SELECTION_CONTRACT_INCOMPLETE"
        ),
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
    _append(
        blockers,
        bool(path_contract.get("veto_required"))
        and legacy_force_export_reentry.get("valid") is not True,
        "LEGACY_OWNER_PATH_CONFLICT",
    )
    _append(
        blockers,
        legacy.get("state")
        == "direct_marketing_phase5_force_export_blocked"
        and legacy_force_export_reentry.get("valid") is not True,
        "LEGACY_PHASE5_FORCE_EXPORT_REENTRY_INVALID",
    )
    primary_path = str(path_contract.get("primary_path") or "")
    primary_path_known = primary_path in STORAGE_DECISION_PRIMARY_PATHS
    _append(
        blockers,
        primary_path in PHASE5_STRONGER_PRIMARY_PATHS,
        "LEGACY_SAFETY_OR_MANUAL_OWNER_VETO",
    )
    _append(
        blockers,
        not primary_path_known,
        "LEGACY_OWNER_PRIMARY_PATH_UNKNOWN",
    )
    _append(
        blockers,
        primary_path_known
        and primary_path not in PHASE5_COMPATIBLE_PRIMARY_PATHS
        and primary_path not in PHASE5_STRONGER_PRIMARY_PATHS,
        "LEGACY_STORAGE_OWNER_NOT_PHASE5_COMPATIBLE",
    )
    state_text = str(legacy.get("state") or "").lower()
    _append(blockers, state_text.startswith("manual_override") or bool(legacy.get("manual_override_active")), "MANUAL_OR_USER_OVERRIDE")
    _append(blockers, bool(legacy.get("abregel_active") or legacy.get("curtailment_protection_active")), "CURTAILMENT_PROTECTION_ACTIVE")
    _append(blockers, bool(legacy.get("ep_reserve_hold") or legacy.get("ep_reserve_discharge_hold")), "EMERGENCY_RESERVE_VETO")

    soc = _float(live_contract.get("soc_pct"), None)
    hard_floor = _float(shadow_slot.get("hard_floor_pct"), None)
    risk_floor = _float(shadow_slot.get("risk_floor_pct"), None)
    current_projection = (
        current_plan_slot.get("projection")
        if isinstance(current_plan_slot.get("projection"), dict)
        else {}
    )
    predictive_floor_overridden = bool(
        candidate.get("action") == "ECONOMIC_EXPORT"
        and direct_marketing_slot.get("valid_selected_contract") is True
        and direct_marketing_slot.get("action") == "ECONOMIC_EXPORT"
        and current_projection.get("direct_marketing_selected") is True
        and current_projection.get("direct_marketing_plan_executable") is True
        and current_projection.get("direct_marketing_plan_commands_allowed")
        is True
        and current_projection.get("direct_marketing_plan_action")
        == "ECONOMIC_EXPORT"
        and current_projection.get(
            "direct_marketing_predictive_floor_overridden"
        )
        is True
    )
    # Der Shadow-Risikoboden ist eine prädiktive Optimierungsgröße, keine
    # physische Reserve. Ein vollständig kanonisch freigegebener
    # ECONOMIC_EXPORT darf ihn übersteuern; der harte Boden wird weiterhin
    # separat und unverändert geprüft.
    floor_values = [
        value
        for value in (
            hard_floor,
            None if predictive_floor_overridden else risk_floor,
        )
        if value is not None
    ]
    floor = max(floor_values) if floor_values else None
    soc_tolerance_pct = max(
        0.5,
        min(10.0, _float(cfg.get("storage_dispatch_live_plan_soc_tolerance_pct"), 5.0) or 5.0),
    )
    soc_plan_corridor = _live_soc_plan_corridor_contract(
        soc,
        shadow_slot,
        current_plan_slot,
        direct_marketing_slot,
        soc_tolerance_pct,
    )
    planned_soc = _float(soc_plan_corridor.get("start_soc_pct"), None)
    planned_soc_end = _float(soc_plan_corridor.get("end_soc_pct"), None)
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
        current_slot_available and soc_plan_corridor.get("outside") is True,
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

    economic_export_diagnostic_release = (
        _economic_export_diagnostic_only_release_contract(
            candidate,
            direct_marketing_slot,
            active_direct_marketing_binding,
            input_binding,
            live_contract,
            settings_contract,
            readiness_validation,
            blockers,
        )
    )
    if economic_export_diagnostic_release.get("valid") is True:
        suppressed = set(
            economic_export_diagnostic_release.get("suppressed_blockers")
            or []
        )
        blockers = [code for code in blockers if code not in suppressed]

    pv_store_diagnostic_release = _pv_store_diagnostic_only_release_contract(
        candidate,
        direct_marketing_slot,
        active_direct_marketing_binding,
        input_binding,
        live_contract,
        settings_contract,
        readiness_validation,
        blockers,
    )
    if pv_store_diagnostic_release.get("valid") is True:
        suppressed = set(pv_store_diagnostic_release.get("suppressed_blockers") or [])
        blockers = [code for code in blockers if code not in suppressed]

    ready_no_action_release = _ready_no_action_house_supply_contract(
        plan,
        shadow_slot,
        resolved,
        candidate,
        input_binding,
        readiness_validation,
        live_contract,
        settings_contract,
        blockers,
        now_value,
    )
    if ready_no_action_release.get("valid") is True:
        suppressed = set(
            ready_no_action_release.get("suppressed_blockers") or []
        )
        blockers = [code for code in blockers if code not in suppressed]

    if not applicable:
        # Eine abgeschaltete DV-Capability ist kein Preis-/Forecast-/Runtimefehler.
        # Die typisierte Aktivierungssperre bleibt sichtbar, alle technischen
        # Shadowblocker sind für diese Anlage jedoch ausdrücklich nicht
        # anwendbar. Strukturelle Revisions-/Bindingfehler bleiben trotzdem
        # fail-closed sichtbar, damit eine spätere Aktivierung nie auf einem
        # unvollständigen Planvertrag aufsetzt.
        structural_blockers = [
            code
            for code in blockers
            if code in {
                "INPUT_REVISIONS_INCOMPLETE",
                "SHADOW_INPUT_BINDING_INVALID",
            }
        ]
        blockers = ["DIRECT_MARKETING_DISABLED", *structural_blockers]

    activation_only = {
        "PHASE5_MODE_SHADOW",
        "PHASE5_MODE_MISSING_OR_UNKNOWN",
        "SHADOW_60_GATE_NOT_EXACTLY_BOUND",
        "DIRECT_MARKETING_DISABLED",
    }
    economic_hold_blockers = set(economic_hold.get("blockers") or [])
    economic_hold_derivative_blockers = (
        set(ECONOMIC_HOLD_DERIVATIVE_BLOCKERS)
        if economic_hold.get("valid")
        else set()
    )
    restrictive_policy_hold_blockers = set(
        restrictive_policy_hold.get("blockers") or []
    )
    non_economic_decision_blockers = [
        code
        for code in blockers
        if code not in activation_only
        and code not in economic_hold_blockers
        and code not in economic_hold_derivative_blockers
        and code not in restrictive_policy_hold_blockers
    ]
    decision_only_economic_hold = bool(economic_hold.get("valid") and not non_economic_decision_blockers)
    decision_only_restrictive_hold = bool(
        restrictive_policy_hold.get("valid")
        and not non_economic_decision_blockers
    )
    decision_only_hold = bool(
        decision_only_economic_hold or decision_only_restrictive_hold
    )
    selected_action = (
        "HOUSE_SUPPLY"
        if ready_no_action_release.get("valid") is True
        else "HOLD"
        if decision_only_hold
        else candidate.get("action") or "HOLD"
    )
    selected_power_w = (
        0.0
        if ready_no_action_release.get("valid") is True or decision_only_hold
        else float(candidate.get("power_w") or 0.0)
    )
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
        # Ein geplanter Headroom-Export darf bei noch laufender Ladung nicht
        # auf den wirkungslosen HOLD-/Legacypfad zurückfallen. Zuerst wird die
        # Ladung im selben kanonischen Slot einseitig gesperrt; erst nach
        # physikalisch bestätigtem Stillstand übernimmt der Export.
        selected_action = (
            "CHARGE_BLOCK_WAIT"
            if str(candidate.get("action") or "").upper() == "HEADROOM_EXPORT"
            and current_direction == "discharge"
            and live_direction == "charge"
            else "HOLD"
        )
        selected_power_w = 0.0
        stability.update({
            "active": True,
            "reason_code": (
                "HEADROOM_CHARGE_BLOCK_LIVE_DIRECTION_REVERSAL"
                if selected_action == "CHARGE_BLOCK_WAIT"
                else "STABILITY_HOLD_LIVE_DIRECTION_REVERSAL"
            ),
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
        and not (
            decision_only_economic_hold
            and code in economic_hold_derivative_blockers
        )
        and not (
            decision_only_restrictive_hold
            and code in restrictive_policy_hold_blockers
        )
    ]
    if not applicable:
        selected_action = "HOLD"
        selected_power_w = 0.0
        technical_blockers = [
            code for code in blockers if code not in activation_only
        ]
    field_selected = bool(
        activation.get("field_active")
        and (not blockers or decision_only_hold)
    )
    selected_action_contract = STORAGE_ACTION_CONTRACTS.get(
        str(selected_action or "HOLD").upper(),
        {},
    )
    field_executable = bool(
        field_selected
        and selected_action != "HOLD"
        and selected_action_contract.get("phase5_command") is True
    )
    decision_available = bool(
        applicable
        and resolved.get("valid")
        and (
            candidate.get("valid")
            or ready_no_action_release.get("valid") is True
        )
    )
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
        "execution_readiness": copy.deepcopy(execution_readiness),
        "execution_readiness_validation": readiness_validation,
        "decision_available": decision_available,
        "candidate": candidate,
        "canonical_direct_marketing_slot": direct_marketing_slot,
        "active_direct_marketing_binding": active_direct_marketing_binding,
        "legacy_force_export_reentry": legacy_force_export_reentry,
        "pv_store_diagnostic_release": pv_store_diagnostic_release,
        "economic_export_diagnostic_release": (
            economic_export_diagnostic_release
        ),
        "ready_no_action_release": ready_no_action_release,
        "candidate_action": candidate.get("action"),
        "candidate_power_w": candidate.get("power_w"),
        "selected": field_selected,
        "executable": field_executable,
        "commands_allowed": field_executable,
        "selection_class": (
            "ready_no_action"
            if field_selected
            and ready_no_action_release.get("valid") is True
            else "decision_only_hold"
            if field_selected and selected_action == "HOLD"
            else "passive_auto_effect"
            if field_selected and not field_executable
            else "command_action"
            if field_selected
            else "legacy_fallback"
        ),
        "decision_only_hold": {
            "active": bool(field_selected and selected_action == "HOLD"),
            "economic_policy_hold": decision_only_economic_hold,
            "restrictive_policy_hold": decision_only_restrictive_hold,
            "reason_code": (
                economic_hold.get("reason_code")
                if decision_only_economic_hold
                else restrictive_policy_hold.get("reason_code")
                if decision_only_restrictive_hold
                else None
            ),
            "blockers": (
                list(economic_hold.get("blockers") or [])
                if decision_only_economic_hold
                else list(restrictive_policy_hold.get("blockers") or [])
                if decision_only_restrictive_hold
                else []
            ),
        },
        "selected_source": (
            "canonical_phase5_ready_no_action"
            if field_selected
            and ready_no_action_release.get("valid") is True
            else "canonical_phase5_decision_only_hold"
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
            "planned_soc_end_pct": planned_soc_end,
            "live_plan_soc_tolerance_pct": soc_tolerance_pct,
            "live_soc_plan_corridor": soc_plan_corridor,
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
            "predictive_floor_overridden": predictive_floor_overridden,
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
