#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reiner, befehlsfreier Shadow-Vertrag für zentrale Wärme-Intents.

Das Modul besitzt absichtlich keine Laufzeit- oder Treiberanbindung. Es bindet
einen fachlichen Wärme-Kandidaten deterministisch an Plan, Slot, Eingangs-
revisionen und konservative Prognoseevidenz. Auch ein vollständig validierter
Intent bleibt in dieser ersten Vertragsstufe wirkungslos:

``shadow_only=True``, ``commands_allowed=False``, ``executable=False``.

Ein P50-Punktwert ist keine konservative Absicherung. Prognoseevidenz wird nur
als ``COMPLETE`` anerkannt, wenn ein oberes Wärmebedarfsquantil und ein unteres
PV-Deckungsquantil vollständig, frisch und revisionsgebunden vorliegen.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional, Tuple


CANDIDATE_SCHEMA = "heat_intent_candidate_v1"
INTENT_SCHEMA = "heat_intent_v1"
VALIDATION_SCHEMA = "heat_intent_validation_v1"
CONSERVATIVE_EVIDENCE_SCHEMA = "heat_conservative_quantile_evidence_v1"

VALID_SCOPES = frozenset({"heating", "dhw", "both"})
VALID_TARGET_STATES = frozenset(
    {"NORMAL", "PV_SURPLUS", "PRE_DUMP", "BOOST", "BLOCKED", "PROTECTED"}
)
VALID_TRANSITIONS = frozenset({"start", "continue", "stop", "hold"})

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_EPSILON = 1e-9


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Nicht-endliche Zahlen sind im Heat-Intent unzulässig")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError("Heat-Intent enthält einen nicht JSON-kompatiblen Wert")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialisiert Vertragsmaterial deterministisch und ohne NaN/Infinity."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def revision_hash(value: Any) -> str:
    """Erzeugt die kanonische SHA-256-Revision eines Vertragswerts."""

    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _is_revision(value: Any) -> bool:
    return bool(_SHA256_PATTERN.fullmatch(str(value or "").strip()))


def _revision_or_none(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text if _is_revision(text) else None


def _strict_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _finite_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_number(value: Any) -> Optional[float]:
    number = _finite_number(value)
    return number if number is not None and number >= 0.0 else None


def _positive_int(value: Any) -> Optional[int]:
    number = _finite_number(value)
    if number is None or number <= 0.0 or not number.is_integer():
        return None
    return int(number)


def _reason_list(values: Any) -> Optional[List[str]]:
    if not isinstance(values, (list, tuple)):
        return None
    normalized: List[str] = []
    for value in values:
        text = str(value or "").strip().upper()
        if not text:
            return None
        if text not in normalized:
            normalized.append(text)
    return normalized


def _unique(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _normalize_binding(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    return {
        "plan_id": str(source.get("plan_id") or "").strip() or None,
        "plan_revision": str(source.get("plan_revision") or "").strip() or None,
        "slot_id": str(source.get("slot_id") or "").strip() or None,
        "slot_revision": str(source.get("slot_revision") or "").strip() or None,
        "slot_start_ts_ms": _positive_int(source.get("slot_start_ts_ms")),
        "slot_end_ts_ms": _positive_int(source.get("slot_end_ts_ms")),
    }


def _binding_contract(value: Any) -> Tuple[Dict[str, Any], List[str]]:
    binding = _normalize_binding(value)
    reasons: List[str] = []
    for key in ("plan_id", "plan_revision", "slot_id", "slot_revision"):
        if not _is_revision(binding.get(key)):
            reasons.append("BINDING_%s_INVALID" % key.upper())
    start_ms = binding.get("slot_start_ts_ms")
    end_ms = binding.get("slot_end_ts_ms")
    if start_ms is None or end_ms is None or end_ms <= start_ms:
        reasons.append("BINDING_SLOT_RANGE_INVALID")
    binding["binding_revision"] = (
        revision_hash(binding) if not reasons else None
    )
    return binding, reasons


def _normalize_request(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    return {
        "target_state": str(source.get("target_state") or "").strip().upper(),
        "scope": str(source.get("scope") or "").strip().lower(),
        "transition": str(source.get("transition") or "").strip().lower(),
        "selection_requested": _strict_bool(source.get("selection_requested")),
    }


def _normalize_user(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    return {
        "enabled": _strict_bool(source.get("enabled")),
        "source": str(source.get("source") or "user_configuration").strip(),
        "revision": str(source.get("revision") or "").strip() or None,
    }


def _normalize_capability(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    raw_scopes = source.get("allowed_scopes")
    scopes: Optional[List[str]]
    if isinstance(raw_scopes, (list, tuple, set)):
        scopes = sorted(
            {
                str(scope or "").strip().lower()
                for scope in raw_scopes
                if str(scope or "").strip()
            }
        )
    else:
        scopes = None
    return {
        "available": _strict_bool(source.get("available")),
        "controllable": _strict_bool(source.get("controllable")),
        "driver_class": str(source.get("driver_class") or "").strip() or None,
        "allowed_scopes": scopes,
        "revision": str(source.get("revision") or "").strip() or None,
    }


def _normalize_quantile_part(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    required_horizon_h = (
        _nonnegative_number(source.get("required_horizon_h"))
        if "required_horizon_h" in source
        else 24.0
    )
    return {
        "status": str(source.get("status") or "").strip().upper(),
        "quantile": _finite_number(source.get("quantile")),
        "value_kwh": _nonnegative_number(source.get("value_kwh")),
        "fresh": _strict_bool(source.get("fresh")),
        "required_horizon_h": required_horizon_h,
        "horizon_h": _nonnegative_number(source.get("horizon_h")),
        "coverage_h": _nonnegative_number(source.get("coverage_h")),
        "horizon_complete": _strict_bool(source.get("horizon_complete")),
        "source_revision": (
            str(source.get("source_revision") or "").strip() or None
        ),
    }


def _normalize_forecast_evidence(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    return {
        "schema_version": str(source.get("schema_version") or "").strip(),
        "status": str(source.get("status") or "").strip().upper(),
        "plan_id": str(source.get("plan_id") or "").strip() or None,
        "plan_revision": str(source.get("plan_revision") or "").strip() or None,
        "slot_id": str(source.get("slot_id") or "").strip() or None,
        "slot_revision": str(source.get("slot_revision") or "").strip() or None,
        "method_revision": (
            str(source.get("method_revision") or "").strip() or None
        ),
        "heat_need": _normalize_quantile_part(source.get("heat_need")),
        "pv_coverage": _normalize_quantile_part(source.get("pv_coverage")),
        "revision": str(source.get("revision") or "").strip() or None,
    }


def _evidence_revision_material(evidence: Mapping[str, Any]) -> Dict[str, Any]:
    material = copy.deepcopy(dict(evidence))
    material.pop("revision", None)
    return material


def seal_conservative_forecast_evidence(value: Any) -> Dict[str, Any]:
    """Versiegelt einen expliziten Quantilvertrag ohne ihn fachlich aufzuwerten.

    Die Funktion setzt lediglich die deterministische Revision. Ob Quantile,
    Frische, Horizont und Plan-/Slotbindung tatsächlich konservativ und
    vollständig sind, entscheidet erst der Candidate-Validator.
    """

    evidence = _normalize_forecast_evidence(value)
    evidence["revision"] = revision_hash(_evidence_revision_material(evidence))
    return evidence


def _evaluate_forecast_evidence(
    value: Any,
    binding: Mapping[str, Any],
) -> Tuple[Dict[str, Any], str, List[str]]:
    evidence = _normalize_forecast_evidence(value)
    reasons: List[str] = []
    if evidence.get("schema_version") != CONSERVATIVE_EVIDENCE_SCHEMA:
        reasons.append("FORECAST_EVIDENCE_SCHEMA_INVALID")
    if evidence.get("status") != "COMPLETE":
        reasons.append("FORECAST_EVIDENCE_INCOMPLETE")
    for key in ("plan_id", "plan_revision", "slot_id", "slot_revision"):
        if (
            not _is_revision(evidence.get(key))
            or evidence.get(key) != binding.get(key)
        ):
            reasons.append("FORECAST_%s_BINDING_INVALID" % key.upper())
    if not _is_revision(evidence.get("method_revision")):
        reasons.append("FORECAST_METHOD_REVISION_MISSING")
    if not _is_revision(evidence.get("revision")):
        reasons.append("FORECAST_EVIDENCE_REVISION_MISSING")
    else:
        expected_revision = revision_hash(
            _evidence_revision_material(evidence)
        )
        if evidence.get("revision") != expected_revision:
            reasons.append("FORECAST_EVIDENCE_REVISION_MISMATCH")

    heat = evidence["heat_need"]
    pv = evidence["pv_coverage"]
    for name, part in (("HEAT", heat), ("PV", pv)):
        if part.get("status") != "COMPLETE":
            reasons.append("%s_FORECAST_INCOMPLETE" % name)
        if part.get("value_kwh") is None:
            reasons.append("%s_FORECAST_VALUE_INVALID" % name)
        if part.get("fresh") is not True:
            reasons.append("%s_FORECAST_STALE" % name)
        if part.get("horizon_complete") is not True:
            reasons.append("%s_FORECAST_HORIZON_INCOMPLETE" % name)
        required_h = part.get("required_horizon_h")
        horizon_h = part.get("horizon_h")
        coverage_h = part.get("coverage_h")
        if required_h is None or required_h <= 0.0:
            reasons.append("%s_REQUIRED_HORIZON_INVALID" % name)
        elif horizon_h is None or horizon_h + _EPSILON < required_h:
            reasons.append("%s_FORECAST_HORIZON_TOO_SHORT" % name)
        if (
            horizon_h is None
            or coverage_h is None
            or coverage_h + _EPSILON < horizon_h
        ):
            reasons.append("%s_FORECAST_COVERAGE_INCOMPLETE" % name)
        if not _is_revision(part.get("source_revision")):
            reasons.append("%s_FORECAST_SOURCE_REVISION_MISSING" % name)

    heat_q = heat.get("quantile")
    pv_q = pv.get("quantile")
    p50_only = bool(
        (heat_q is not None and abs(heat_q - 0.5) <= _EPSILON)
        or (pv_q is not None and abs(pv_q - 0.5) <= _EPSILON)
    )
    if p50_only:
        reasons.append("P50_ONLY_NOT_SUFFICIENT")
    else:
        if heat_q is None or not (0.5 < heat_q <= 1.0):
            reasons.append("HEAT_UPPER_QUANTILE_MISSING")
        if pv_q is None or not (0.0 <= pv_q < 0.5):
            reasons.append("PV_LOWER_QUANTILE_MISSING")

    reasons = _unique(reasons)
    return evidence, ("COMPLETE" if not reasons else "EVIDENCE_LIMIT"), reasons


def _normalize_constraints(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    min_runtime_source = (
        source.get("minimum_runtime")
        if isinstance(source.get("minimum_runtime"), Mapping)
        else {}
    )
    restart_source = (
        source.get("restart")
        if isinstance(source.get("restart"), Mapping)
        else {}
    )
    protection_source = (
        source.get("protection")
        if isinstance(source.get("protection"), Mapping)
        else {}
    )
    return {
        "minimum_runtime": {
            "satisfied": _strict_bool(min_runtime_source.get("satisfied")),
            "remaining_s": _nonnegative_number(
                min_runtime_source.get("remaining_s")
            ),
            "revision": (
                str(min_runtime_source.get("revision") or "").strip() or None
            ),
        },
        "restart": {
            "allowed": _strict_bool(restart_source.get("allowed")),
            "remaining_s": _nonnegative_number(
                restart_source.get("remaining_s")
            ),
            "revision": (
                str(restart_source.get("revision") or "").strip() or None
            ),
        },
        "protection": {
            "clear": _strict_bool(protection_source.get("clear")),
            "reasons": _reason_list(protection_source.get("reasons")),
            "revision": (
                str(protection_source.get("revision") or "").strip() or None
            ),
        },
    }


def _evaluate_constraints(
    value: Any,
    transition: str,
) -> Tuple[Dict[str, Any], Dict[str, bool], List[str]]:
    constraints = _normalize_constraints(value)
    minimum = constraints["minimum_runtime"]
    restart = constraints["restart"]
    protection = constraints["protection"]
    reasons: List[str] = []

    minimum_complete = bool(
        minimum.get("satisfied") is not None
        and minimum.get("remaining_s") is not None
        and _is_revision(minimum.get("revision"))
    )
    if not minimum_complete:
        reasons.append("MINIMUM_RUNTIME_EVIDENCE_MISSING")
    elif minimum["satisfied"] != (minimum["remaining_s"] <= _EPSILON):
        reasons.append("MINIMUM_RUNTIME_EVIDENCE_INCONSISTENT")

    restart_complete = bool(
        restart.get("allowed") is not None
        and restart.get("remaining_s") is not None
        and _is_revision(restart.get("revision"))
    )
    if not restart_complete:
        reasons.append("RESTART_EVIDENCE_MISSING")
    elif restart["allowed"] != (restart["remaining_s"] <= _EPSILON):
        reasons.append("RESTART_EVIDENCE_INCONSISTENT")

    protection_complete = bool(
        protection.get("clear") is not None
        and protection.get("reasons") is not None
        and _is_revision(protection.get("revision"))
    )
    if not protection_complete:
        reasons.append("PROTECTION_EVIDENCE_MISSING")
    elif protection["clear"] != (len(protection["reasons"]) == 0):
        reasons.append("PROTECTION_EVIDENCE_INCONSISTENT")

    minimum_allowed = bool(
        minimum_complete
        and "MINIMUM_RUNTIME_EVIDENCE_INCONSISTENT" not in reasons
        and (transition != "stop" or minimum.get("satisfied") is True)
    )
    if (
        minimum_complete
        and transition == "stop"
        and minimum.get("satisfied") is False
    ):
        reasons.append("MINIMUM_RUNTIME_ACTIVE")

    restart_allowed = bool(
        restart_complete
        and "RESTART_EVIDENCE_INCONSISTENT" not in reasons
        and (transition != "start" or restart.get("allowed") is True)
    )
    if (
        restart_complete
        and transition == "start"
        and restart.get("allowed") is False
    ):
        reasons.append("RESTART_BLOCK_ACTIVE")

    protection_clear = bool(
        protection_complete
        and "PROTECTION_EVIDENCE_INCONSISTENT" not in reasons
        and protection.get("clear") is True
    )
    if (
        protection_complete
        and protection.get("clear") is False
        and "PROTECTION_EVIDENCE_INCONSISTENT" not in reasons
    ):
        reasons.append("PROTECTION_ACTIVE")

    return (
        constraints,
        {
            "minimum_runtime_allowed": minimum_allowed,
            "restart_allowed": restart_allowed,
            "protection_clear": protection_clear,
        },
        _unique(reasons),
    )


def _normalized_inputs(
    *,
    binding: Any,
    request: Any,
    user: Any,
    capability: Any,
    forecast_evidence: Any,
    constraints: Any,
) -> Dict[str, Any]:
    return {
        "binding": _normalize_binding(binding),
        "request": _normalize_request(request),
        "user": _normalize_user(user),
        "capability": _normalize_capability(capability),
        "forecast_evidence": _normalize_forecast_evidence(forecast_evidence),
        "constraints": _normalize_constraints(constraints),
    }


def _candidate_from_inputs(inputs: Mapping[str, Any]) -> Dict[str, Any]:
    binding, reasons = _binding_contract(inputs.get("binding"))
    request = _normalize_request(inputs.get("request"))
    user = _normalize_user(inputs.get("user"))
    capability = _normalize_capability(inputs.get("capability"))

    if request.get("target_state") not in VALID_TARGET_STATES:
        reasons.append("TARGET_STATE_INVALID")
    if request.get("scope") not in VALID_SCOPES:
        reasons.append("SCOPE_INVALID")
    if request.get("transition") not in VALID_TRANSITIONS:
        reasons.append("TRANSITION_INVALID")
    if request.get("selection_requested") is None:
        reasons.append("SELECTION_FLAG_INVALID")

    user_revision_valid = _is_revision(user.get("revision"))
    if user.get("enabled") is None:
        reasons.append("USER_ENABLE_STATE_MISSING")
    elif user.get("enabled") is False:
        reasons.append("USER_OFF")
    if not user_revision_valid:
        reasons.append("USER_REVISION_MISSING")
    user_gate = bool(user.get("enabled") is True and user_revision_valid)

    capability_revision_valid = _is_revision(capability.get("revision"))
    if capability.get("available") is None:
        reasons.append("CAPABILITY_AVAILABILITY_MISSING")
    elif capability.get("available") is False:
        reasons.append("CAPABILITY_UNAVAILABLE")
    if capability.get("controllable") is None:
        reasons.append("CAPABILITY_CONTROL_STATE_MISSING")
    elif capability.get("controllable") is False:
        reasons.append("CAPABILITY_NOT_CONTROLLABLE")
    if not capability_revision_valid:
        reasons.append("CAPABILITY_REVISION_MISSING")
    scopes = capability.get("allowed_scopes")
    if (
        not isinstance(scopes, list)
        or not scopes
        or any(scope not in VALID_SCOPES for scope in scopes)
    ):
        reasons.append("CAPABILITY_SCOPE_EVIDENCE_MISSING")
        scope_supported = False
    else:
        scope_supported = request.get("scope") in scopes
        if not scope_supported:
            reasons.append("SCOPE_UNSUPPORTED")
    capability_gate = bool(
        capability.get("available") is True
        and capability.get("controllable") is True
        and capability_revision_valid
    )

    evidence, evidence_status, evidence_reasons = _evaluate_forecast_evidence(
        inputs.get("forecast_evidence"),
        binding,
    )
    reasons.extend(evidence_reasons)

    constraints, constraint_gates, constraint_reasons = _evaluate_constraints(
        inputs.get("constraints"),
        request.get("transition") or "",
    )
    reasons.extend(constraint_reasons)
    heat_need_kwh = evidence["heat_need"].get("value_kwh")
    pv_coverage_kwh = evidence["pv_coverage"].get("value_kwh")
    remaining_need_kwh = (
        max(0.0, heat_need_kwh - pv_coverage_kwh)
        if heat_need_kwh is not None and pv_coverage_kwh is not None
        else None
    )
    remaining_need_positive = bool(
        evidence_status == "COMPLETE"
        and remaining_need_kwh is not None
        and remaining_need_kwh > _EPSILON
    )
    boost_needs_remaining_heat = bool(
        request.get("target_state") == "BOOST"
        and request.get("transition") in {"start", "continue"}
    )
    if (
        boost_needs_remaining_heat
        and evidence_status == "COMPLETE"
        and not remaining_need_positive
    ):
        reasons.append("FORECAST_HEAT_NEED_COVERED_BY_PV")
    reasons = _unique(reasons)

    eligible = not reasons and evidence_status == "COMPLETE"
    selection_requested = request.get("selection_requested") is True
    selected = bool(eligible and selection_requested)
    normalized = {
        "binding": _normalize_binding(inputs.get("binding")),
        "request": request,
        "user": user,
        "capability": capability,
        "forecast_evidence": evidence,
        "constraints": constraints,
    }
    input_revision = revision_hash(normalized)
    candidate: Dict[str, Any] = {
        "schema_version": CANDIDATE_SCHEMA,
        "shadow_only": True,
        "commands_allowed": False,
        "status": evidence_status,
        "eligibility_status": "ELIGIBLE" if eligible else "INELIGIBLE",
        "eligible": bool(eligible),
        "selection_requested": selection_requested,
        "selected": selected,
        "executable": False,
        "confirmed": False,
        "target_state": request.get("target_state"),
        "scope": request.get("scope"),
        "transition": request.get("transition"),
        "heat_need_kwh": heat_need_kwh,
        "pv_coverage_kwh": pv_coverage_kwh,
        "remaining_need_kwh": remaining_need_kwh,
        "binding": binding,
        "input_revision": input_revision,
        "gates": {
            "user_enabled": user_gate,
            "capability_available": capability_gate,
            "scope_supported": bool(scope_supported),
            "forecast_complete": evidence_status == "COMPLETE",
            "remaining_need_positive": (
                remaining_need_positive
                if boost_needs_remaining_heat
                else evidence_status == "COMPLETE"
            ),
            **constraint_gates,
        },
        "protection_reasons": (
            list(constraints["protection"]["reasons"] or [])
        ),
        "reason_codes": (
            reasons if reasons else ["ELIGIBLE_SHADOW_CANDIDATE"]
        ),
        "inputs": normalized,
    }
    candidate["candidate_id"] = revision_hash(candidate)
    return candidate


def build_heat_intent_candidate(
    *,
    binding: Any,
    request: Any,
    user: Any,
    capability: Any,
    forecast_evidence: Any,
    constraints: Any,
) -> Dict[str, Any]:
    """Erzeugt einen deterministischen, stets befehlsfreien Shadow-Kandidaten."""

    inputs = _normalized_inputs(
        binding=binding,
        request=request,
        user=user,
        capability=capability,
        forecast_evidence=forecast_evidence,
        constraints=constraints,
    )
    return _candidate_from_inputs(inputs)


def _invalid_intent(candidate: Any, reasons: Iterable[str]) -> Dict[str, Any]:
    source = candidate if isinstance(candidate, Mapping) else {}
    candidate_id = _revision_or_none(source.get("candidate_id"))
    validation_reasons = _unique(reasons) or ["CANDIDATE_INVALID"]
    intent: Dict[str, Any] = {
        "schema_version": INTENT_SCHEMA,
        "shadow_only": True,
        "commands_allowed": False,
        "status": "EVIDENCE_LIMIT",
        "eligibility_status": "INELIGIBLE",
        "eligible": False,
        "selection_requested": False,
        "selected": False,
        "executable": False,
        "confirmed": False,
        "target_state": "NORMAL",
        "scope": "both",
        "transition": "hold",
        "heat_need_kwh": None,
        "pv_coverage_kwh": None,
        "remaining_need_kwh": None,
        "candidate_id": candidate_id,
        "input_revision": None,
        "binding": {
            "plan_id": None,
            "plan_revision": None,
            "slot_id": None,
            "slot_revision": None,
            "slot_start_ts_ms": None,
            "slot_end_ts_ms": None,
            "binding_revision": None,
        },
        "gates": {
            "user_enabled": False,
            "capability_available": False,
            "scope_supported": False,
            "forecast_complete": False,
            "remaining_need_positive": False,
            "minimum_runtime_allowed": False,
            "restart_allowed": False,
            "protection_clear": False,
        },
        "protection_reasons": [],
        "reason_codes": validation_reasons,
        "validation": {
            "schema_version": VALIDATION_SCHEMA,
            "valid": False,
            "reason_codes": validation_reasons,
        },
    }
    intent["intent_id"] = revision_hash(intent)
    return intent


def validate_heat_intent_candidate(candidate: Any) -> Dict[str, Any]:
    """Validiert den Candidate vollständig neu und erzeugt ``heat_intent_v1``.

    Jede Schema-, Inhalts- oder Hashabweichung liefert einen effectless
    ``EVIDENCE_LIMIT``-Intent. Der Validator übernimmt keine abgeleiteten
    Candidate-Felder ungeprüft.
    """

    if not isinstance(candidate, Mapping):
        return _invalid_intent(candidate, ["CANDIDATE_NOT_A_MAPPING"])
    if candidate.get("schema_version") != CANDIDATE_SCHEMA:
        return _invalid_intent(candidate, ["CANDIDATE_SCHEMA_INVALID"])
    inputs = candidate.get("inputs")
    if not isinstance(inputs, Mapping):
        return _invalid_intent(candidate, ["CANDIDATE_INPUTS_MISSING"])
    try:
        expected = _candidate_from_inputs(inputs)
        candidate_bytes = canonical_json_bytes(candidate)
        expected_bytes = canonical_json_bytes(expected)
    except (TypeError, ValueError):
        return _invalid_intent(candidate, ["CANDIDATE_CANONICALIZATION_FAILED"])
    reasons: List[str] = []
    if candidate.get("candidate_id") != expected.get("candidate_id"):
        reasons.append("CANDIDATE_ID_MISMATCH")
    if candidate_bytes != expected_bytes:
        reasons.append("CANDIDATE_CONTENT_MISMATCH")
    if reasons:
        return _invalid_intent(candidate, reasons)

    intent: Dict[str, Any] = {
        "schema_version": INTENT_SCHEMA,
        "shadow_only": True,
        "commands_allowed": False,
        "status": expected["status"],
        "eligibility_status": expected["eligibility_status"],
        "eligible": expected["eligible"],
        "selection_requested": expected["selection_requested"],
        "selected": expected["selected"],
        "executable": False,
        "confirmed": False,
        "target_state": expected["target_state"],
        "scope": expected["scope"],
        "transition": expected["transition"],
        "heat_need_kwh": expected["heat_need_kwh"],
        "pv_coverage_kwh": expected["pv_coverage_kwh"],
        "remaining_need_kwh": expected["remaining_need_kwh"],
        "candidate_id": expected["candidate_id"],
        "input_revision": expected["input_revision"],
        "binding": copy.deepcopy(expected["binding"]),
        "gates": copy.deepcopy(expected["gates"]),
        "protection_reasons": list(expected["protection_reasons"]),
        "reason_codes": list(expected["reason_codes"]),
        "validation": {
            "schema_version": VALIDATION_SCHEMA,
            "valid": True,
            "reason_codes": ["VALIDATED_EFFECTLESS_SHADOW"],
        },
    }
    intent["intent_id"] = revision_hash(intent)
    return intent


def validate_heat_intent(intent: Any) -> Dict[str, Any]:
    """Prüft einen fertigen Intent; das Prüfergebnis erlaubt nie Befehle."""

    reasons: List[str] = []
    if not isinstance(intent, Mapping):
        reasons.append("INTENT_NOT_A_MAPPING")
        source: Mapping[str, Any] = {}
    else:
        source = intent
    if source.get("schema_version") != INTENT_SCHEMA:
        reasons.append("INTENT_SCHEMA_INVALID")
    if source.get("shadow_only") is not True:
        reasons.append("INTENT_NOT_SHADOW_ONLY")
    if source.get("commands_allowed") is not False:
        reasons.append("INTENT_COMMAND_PERMISSION_INVALID")
    if source.get("executable") is not False:
        reasons.append("INTENT_EXECUTABLE_INVALID")
    if source.get("confirmed") is not False:
        reasons.append("INTENT_CONFIRMATION_INVALID")
    if not _is_revision(source.get("candidate_id")):
        reasons.append("INTENT_CANDIDATE_BINDING_INVALID")
    if not _is_revision(source.get("intent_id")):
        reasons.append("INTENT_ID_INVALID")
    else:
        try:
            material = copy.deepcopy(dict(source))
            provided_id = material.pop("intent_id", None)
            if provided_id != revision_hash(material):
                reasons.append("INTENT_ID_MISMATCH")
        except (TypeError, ValueError):
            reasons.append("INTENT_CANONICALIZATION_FAILED")
    if source.get("selected") is True and source.get("eligible") is not True:
        reasons.append("INTENT_SELECTION_WITHOUT_ELIGIBILITY")
    if source.get("eligible") is True:
        gates = source.get("gates")
        if (
            source.get("status") != "COMPLETE"
            or not isinstance(gates, Mapping)
            or any(value is not True for value in gates.values())
        ):
            reasons.append("INTENT_ELIGIBILITY_GATES_INVALID")
    validation = source.get("validation")
    if (
        not isinstance(validation, Mapping)
        or validation.get("schema_version") != VALIDATION_SCHEMA
        or validation.get("valid") is not True
    ):
        reasons.append("INTENT_VALIDATION_BINDING_INVALID")

    reasons = _unique(reasons)
    return {
        "schema_version": VALIDATION_SCHEMA,
        "valid": not reasons,
        "shadow_only": True,
        "commands_allowed": False,
        "executable": False,
        "confirmed": False,
        "reason_codes": reasons or ["INTENT_VALID"],
    }


__all__ = [
    "CANDIDATE_SCHEMA",
    "INTENT_SCHEMA",
    "VALIDATION_SCHEMA",
    "CONSERVATIVE_EVIDENCE_SCHEMA",
    "build_heat_intent_candidate",
    "canonical_json_bytes",
    "revision_hash",
    "seal_conservative_forecast_evidence",
    "validate_heat_intent",
    "validate_heat_intent_candidate",
]
